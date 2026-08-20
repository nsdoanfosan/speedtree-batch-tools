"""Headless exact-target Generator Sync and Cluster refresh adapter."""

from __future__ import annotations

import argparse
import copy
import importlib.util
import json
import os
import sys
from pathlib import Path
from typing import Mapping


TOOL_DIR = Path(__file__).resolve().parent
REPO_DIR = TOOL_DIR.parent
sys.path.insert(0, str(REPO_DIR))
sys.path.insert(0, str(TOOL_DIR))

from connected_run import (
    connected_settings,
    connected_unit_records,
    rebase_authorized_dependency_identities,
    scope_dependency_identities,
)
from cluster_blend_sync import (
    RelationValidationCache,
    inspect_cluster_relation_current_state,
)
from atlas_target_registry import load_target_registry, registry_path_for_blend
from exact_target_command import build_exact_target_request, run_exact_target_request
from atlas_slot_ownership import (
    AtlasSlotOwnershipError,
    apply_atlas_slot_ownership_reconciliation,
    plan_atlas_slot_ownership_reconciliation,
    validate_atlas_slot_ownership_plan,
)
from artifact_content_key import (
    LEGACY_FINGERPRINT_ALGORITHM,
    SHA256_ALGORITHM,
    file_content_key_snapshot,
)
from repair_orchestration import (
    ATLAS_SLOT_OWNERSHIP_RECONCILE,
    CLUSTER_REFRESH,
    GENERATOR_SYNC,
    GENERATOR_SYNC_AND_CLUSTER,
    GENERATOR_SYNC_TOOL,
    canonical_exact_spm,
)
from shared_queue_runtime import WaitCancelled


def _load_gui_module():
    name = "_speedtree_generator_exact_target_backend"
    path = TOOL_DIR / "spm_generator_sync_gui.pyw"
    loaded = sys.modules.get(name)
    if loaded is not None and Path(loaded.__file__).resolve() == path.resolve():
        return loaded
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load Generator Sync backend: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(name, None)
        raise
    return module


def _key(value) -> str:
    return os.path.normcase(os.path.abspath(str(value))).casefold()


def _validate_sealed_artifact(record, expected_path):
    if not isinstance(record, Mapping):
        raise ValueError("sealed Cluster relation artifact is missing")
    path = Path(record.get("path") or "").expanduser().absolute()
    expected = Path(expected_path).expanduser().absolute()
    if _key(path) != _key(expected) or not path.is_file():
        raise ValueError(f"sealed Cluster relation artifact changed: {expected}")
    if record.get("sha256"):
        algorithm = SHA256_ALGORITHM
        expected_digest = str(record["sha256"]).casefold()
    elif record.get("fingerprint"):
        algorithm = str(
            record.get("fingerprint_algorithm")
            or LEGACY_FINGERPRINT_ALGORITHM
        )
        expected_digest = str(record["fingerprint"]).casefold()
    else:
        raise ValueError("sealed Cluster relation artifact has no content key")
    current = file_content_key_snapshot(path, algorithm)
    if str(current["digest"]).casefold() != expected_digest:
        raise ValueError(f"sealed Cluster relation artifact changed: {path}")
    return path


def _sealed_cluster_relation_rows(
    scope,
    requested,
    board_root,
    relations,
):
    """Validate live-pair seals and return only their exact Cluster rows."""

    supplied = list(relations or ())
    if not supplied:
        return []
    requested_keys = {_key(path) for path in requested}
    existing_by_blend = {
        _key(row["blend"]): row
        for row in scope.get("cluster_rows") or ()
        if isinstance(row, Mapping) and row.get("blend")
    }
    rows = {}
    for relation in supplied:
        if not isinstance(relation, Mapping) or relation.get(
            "schema_version"
        ) != 1:
            raise ValueError("sealed Cluster provider relation is malformed")
        target = Path(relation.get("target_spm") or "").expanduser().absolute()
        provider = Path(
            relation.get("provider_spm") or ""
        ).expanduser().absolute()
        blend = Path(
            relation.get("provider_blend") or ""
        ).expanduser().absolute()
        if (
            _key(target) not in requested_keys
            or target.suffix.casefold() != ".spm"
            or provider.suffix.casefold() != ".spm"
            or blend.suffix.casefold() != ".blend"
            or _key(blend) != _key(provider.with_suffix(".blend"))
            or provider.parent.name.casefold() != "cluster"
            or _key(target.parent) != _key(provider.parent.parent)
        ):
            raise ValueError("sealed Cluster provider relation widened scope")
        for path in (target, provider, blend):
            try:
                path.relative_to(board_root)
            except ValueError as exc:
                raise ValueError(
                    "sealed Cluster provider is outside configured inventory"
                ) from exc

        status = str(relation.get("relation_status") or "")
        proof = relation.get("live_pair_proof") or {}
        if status == "explicit_on":
            if relation.get("relation_allowed") is not True:
                raise ValueError("sealed ON Cluster relation lost authority")
        elif status == "explicit_off":
            if not (
                relation.get("relation_allowed") is False
                and proof.get("current_live_pair_covered") is True
                and proof.get("spm_pair_status") == "complete_pair"
                and proof.get("fbx_pair_status") == "complete_pair"
                and proof.get("fbx_pair_decision") == "normalize_part"
            ):
                raise ValueError(
                    "OFF Cluster relation has no exact live-pair proof"
                )
        else:
            raise ValueError("sealed Cluster relation status is unsupported")

        artifacts = relation.get("artifacts") or {}
        _validate_sealed_artifact(artifacts.get("target_spm"), target)
        _validate_sealed_artifact(artifacts.get("provider_spm"), provider)
        _validate_sealed_artifact(artifacts.get("provider_blend"), blend)
        expected_fbx = target.parent / "fbx" / f"{target.stem}.fbx"
        _validate_sealed_artifact(artifacts.get("target_fbx"), expected_fbx)
        registry = blend.with_suffix(".atlas_leaf_targets.json")
        _validate_sealed_artifact(
            artifacts.get("target_registry"),
            registry,
        )

        existing = existing_by_blend.get(_key(blend))
        if status == "explicit_on":
            if existing is None or _key(target) not in {
                _key(path) for path in existing.get("on_target_spms") or ()
            }:
                raise ValueError("sealed ON Cluster relation is no longer ON")
            row = copy.deepcopy(existing)
        else:
            row = {
                "kind": "cluster_relation",
                "blend": blend,
                "source_spm": provider,
                "canonical_spm": provider,
                "registry_path": registry,
                "refresh_reasons": ["explicit_target_relation_off"],
                "refresh_reason_categories": ["derived_relation_registry"],
            }
        row["blend"] = blend
        row["on_target_spms"] = [target]
        row["target_spms"] = [target]
        relation_key = (_key(blend), _key(target))
        if relation_key in rows and rows[relation_key] != row:
            raise ValueError("conflicting sealed Cluster provider relations")
        rows[relation_key] = row
    return [rows[key] for key in sorted(rows)]


def exact_runtime_scope(
    target_spms,
    *,
    config=None,
    cluster_provider_relations=(),
    provider_blends=(),
):
    """Read the current board and retain only exact matching unit selectors."""

    module = _load_gui_module()
    cfg = dict(config or module.load_config())
    board_root = Path(cfg["tree_root"]).expanduser().absolute()
    requested = [Path(value).expanduser().absolute() for value in target_spms]
    for target in requested:
        if not target.is_file() or target.suffix.casefold() != ".spm":
            raise FileNotFoundError(f"exact target SPM does not exist: {target}")
        try:
            target.relative_to(board_root)
        except ValueError as exc:
            raise ValueError("exact target is outside the configured inventory") from exc

    direct_provider_blends = [
        Path(value).expanduser().absolute()
        for value in provider_blends or ()
    ]
    if direct_provider_blends:
        direct_rows = []
        seen_blends = set()
        for blend in direct_provider_blends:
            blend_key = _key(blend)
            if blend_key in seen_blends:
                continue
            seen_blends.add(blend_key)
            if not blend.is_file() or blend.suffix.casefold() != ".blend":
                raise FileNotFoundError(
                    f"exact Cluster provider blend does not exist: {blend}"
                )
            try:
                blend.relative_to(board_root)
            except ValueError as exc:
                raise ValueError(
                    "exact Cluster provider is outside the configured inventory"
                ) from exc
            owner = blend.parent.parent
            registry = load_target_registry(blend)
            registered_keys = {
                _key(value)
                for value in registry.get("target_spms") or ()
            }
            exact_targets = [
                target
                for target in requested
                if target.parent == owner and _key(target) in registered_keys
            ]
            if not exact_targets:
                raise RuntimeError(
                    "exact Cluster provider has no ON relation to the requested "
                    f"target: {blend}"
                )
            canonical = blend.with_suffix(".spm")
            if not canonical.is_file():
                raise FileNotFoundError(
                    f"exact Cluster provider SPM does not exist: {canonical}"
                )
            direct_rows.append({
                "kind": "cluster_normalized_blend",
                "owner_folder": owner,
                "cluster_folder": blend.parent,
                "source_spm": canonical,
                "canonical_spm": canonical,
                "blend": blend,
                "registry_path": registry_path_for_blend(blend),
                "registry_managed": True,
                "folder_relation": "on",
                "on_target_spms": exact_targets,
                "target_spms": exact_targets,
                "refresh_reasons": [],
                "refresh_reason_categories": [],
            })
        scope = {"groups": [], "cluster_rows": direct_rows}
    else:
        board = module.engine.scan_tree_folders(
            board_root,
            sk_only=bool(cfg.get("sk_only", True)),
            verify_physical=False,
        )
        scope = module.App._connected_scope_from_board(board)
    sealed_cluster_rows = _sealed_cluster_relation_rows(
        scope,
        requested,
        board_root,
        cluster_provider_relations,
    )
    inventory = []
    for sealed_row in sealed_cluster_rows:
        inventory.extend(sealed_row.get("on_target_spms") or ())
        for key in ("source_spm", "canonical_spm"):
            if sealed_row.get(key):
                inventory.append(Path(sealed_row[key]))
    groups = []
    requested_keys = {_key(path) for path in requested}
    requested_cluster_keys = {
        _key(path)
        for raw_row in (
            sealed_cluster_rows or scope["cluster_rows"]
        )
        for path in raw_row.get("on_target_spms") or ()
        if _key(path) in requested_keys
    }
    for raw_group in scope["groups"]:
        folder = Path(raw_group["folder"])
        master_path = folder / raw_group["master"]
        follower_paths = {
            _key(folder / name): name for name in raw_group.get("names") or ()
        }
        inventory.append(master_path)
        inventory.extend(folder / name for name in raw_group.get("names") or ())
        exact_names = [
            name for key, name in follower_paths.items() if key in requested_keys
        ]
        if exact_names:
            groups.append({
                "folder": folder,
                "master": raw_group["master"],
                "names": sorted(exact_names, key=str.casefold),
            })
        elif (
            _key(master_path) in requested_keys
            and _key(master_path) not in requested_cluster_keys
        ):
            # A master-only selector would fan out to unchecked followers.
            # Require callers to supply the exact follower target(s) instead.
            raise ValueError(
                "master-only Generator Sync would widen to sibling followers"
            )

    cluster_rows = []
    for raw_row in (sealed_cluster_rows or scope["cluster_rows"]):
        # The Cluster provider is an exact requested repair target too.  Live
        # board rows previously inventoried only their ON consumers, so a
        # valid ``[consumer, provider]`` Generator+Cluster plan rejected the
        # provider with "does not match a canonical inventory" before doing
        # any work.
        for key in ("source_spm", "canonical_spm"):
            if raw_row.get(key):
                inventory.append(Path(raw_row[key]))
        all_targets = [Path(path) for path in raw_row.get("on_target_spms") or ()]
        inventory.extend(all_targets)
        exact_targets = [path for path in all_targets if _key(path) in requested_keys]
        if exact_targets:
            cluster_rows.append({
                **raw_row,
                "on_target_spms": exact_targets,
                "target_spms": exact_targets,
            })

    canonical = [canonical_exact_spm(path, inventory) for path in requested]
    return module, cfg, board_root, groups, cluster_rows, canonical


def build_exact_runtime_plan(
    request: Mapping,
    *,
    include_identities: bool = True,
):
    module, cfg, board_root, groups, cluster_rows, canonical = exact_runtime_scope(
        request["target_spms"],
        cluster_provider_relations=(
            (request.get("provenance") or {}).get(
                "cluster_provider_relations"
            )
            or ()
        ),
        provider_blends=(
            (request.get("provenance") or {}).get("provider_blends")
            or ()
        ),
    )
    action = request["repair_action"]
    if action == GENERATOR_SYNC:
        cluster_rows = []
    elif action == CLUSTER_REFRESH:
        groups = []
    elif action != GENERATOR_SYNC_AND_CLUSTER:
        raise ValueError(f"unsupported Generator exact repair action: {action}")
    if action in {GENERATOR_SYNC, GENERATOR_SYNC_AND_CLUSTER} and not groups:
        raise RuntimeError("no exact Generator Sync selector matches the target")
    if action in {CLUSTER_REFRESH, GENERATOR_SYNC_AND_CLUSTER} and not cluster_rows:
        raise RuntimeError("no exact Cluster ON relation matches the target")
    units = connected_unit_records(groups, cluster_rows)
    if not units:
        raise RuntimeError("exact-target plan contains no executable units")
    runtime_objects = [*groups, *cluster_rows]
    settings = connected_settings(
        cfg,
        bool(cfg.get("verify_speedtree", True)),
        board_root,
        include_cluster_producer=bool(cluster_rows) and include_identities,
    )
    identities = (
        scope_dependency_identities(units, settings)
        if include_identities
        else {}
    )
    return module, cfg, units, runtime_objects, identities, settings, canonical


def _fast_current_cluster_result(row: Mapping, *, validation_cache=None):
    """Return a pure selected-relation no-op result, or ``None`` if stale."""

    blend = row.get("blend")
    targets = list(row.get("on_target_spms") or ())
    if not blend or not targets:
        return None
    state = inspect_cluster_relation_current_state(
        blend,
        targets,
        validation_cache=validation_cache,
    )
    if not state.get("current"):
        return None
    return {
        "status": "ok",
        "mode": "sync",
        "blend": str(Path(blend).expanduser().absolute()),
        "target_spms": [
            str(Path(target).expanduser().absolute()) for target in targets
        ],
        "folder_relation": "on",
        "no_change": True,
        "already_on": True,
        "skip_reason": "already_on_up_to_date",
        "fast_current_check": True,
        "verification": state.get("verification"),
        "planned_refresh_reasons": list(
            row.get("refresh_reasons") or ()
        ),
        "planned_refresh_reason_categories": list(
            row.get("refresh_reason_categories") or ()
        ),
        "refresh_reasons": [],
        "refresh_reason_categories": [],
        "source_content_identity": (
            state["targets"][0].get("source_content_identity")
            if state.get("targets")
            else None
        ),
    }


class _ProgressReport:
    def __init__(self, progress, cancel_event, lease, total):
        self._progress = progress
        self.cancel_event = cancel_event
        self.cancel_requested = cancel_event.is_set
        self.ownership_is_current = getattr(
            lease, "renew_and_check_current", lambda: True
        )
        self.total = total
        self.current = 0
        self.current_stage_code = ""

    def __call__(self, stage, percent):
        self._progress(
            str(stage),
            completed=self.current,
            remaining=max(0, self.total - self.current),
            percent=int(percent),
            unit_stage=self.current_stage_code,
        )

    def output(self, _channel, _line):
        return None

    def raise_if_cancelled(self):
        if self.cancel_event.is_set():
            raise WaitCancelled("exact-target Generator repair cancelled")


def _sealed_ownership_plan_from_request(request: Mapping):
    if request.get("repair_action") != ATLAS_SLOT_OWNERSHIP_RECONCILE:
        raise ValueError("request is not an Atlas slot ownership action")
    targets = list(request.get("target_spms") or ())
    if len(targets) != 1:
        raise ValueError(
            "Atlas slot ownership reconciliation requires one exact target"
        )
    target = Path(targets[0]).expanduser().absolute()
    if not target.is_file() or target.suffix.casefold() != ".spm":
        raise FileNotFoundError(
            f"exact Atlas ownership target does not exist: {target}"
        )
    provenance = request.get("provenance") or {}
    supplied = provenance.get("ownership_plan")
    if not isinstance(supplied, Mapping):
        raise AtlasSlotOwnershipError(
            "exact_live_spm_ownership_plan_missing",
            "Exact repair request has no sealed live-SPM ownership plan.",
        )
    sealed = validate_atlas_slot_ownership_plan(
        supplied,
        target_spm=target,
        require_repairable=True,
    )
    return target, sealed


def build_exact_atlas_slot_ownership_plan(request: Mapping):
    """Build and validate a fresh plan without mutating manifests or the SPM."""

    target, sealed = _sealed_ownership_plan_from_request(request)
    fresh = plan_atlas_slot_ownership_reconciliation(target)
    fresh = validate_atlas_slot_ownership_plan(
        fresh,
        target_spm=target,
        require_repairable=True,
    )
    if fresh["plan_sha256"] != sealed["plan_sha256"]:
        raise AtlasSlotOwnershipError(
            "exact_live_spm_ownership_plan_changed",
            "Live SPM or Atlas manifests changed after the repair plan was sealed.",
            evidence={
                "sealed_plan_sha256": sealed["plan_sha256"],
                "fresh_plan_sha256": fresh["plan_sha256"],
            },
        )
    return fresh


def execute_exact_atlas_slot_ownership_request(
    request: Mapping, *, progress, cancel_event, lease
):
    """Apply only a freshly reproduced, exact, CAS-protected ownership plan."""

    if cancel_event.is_set():
        raise WaitCancelled("Atlas slot ownership reconciliation cancelled")
    renew = getattr(lease, "renew_and_check_current", lambda: True)
    if not renew():
        raise RuntimeError("shared queue ownership is not current")
    progress(
        "Atlas slot ownership plan 검증",
        completed=0,
        remaining=1,
        unit_stage="atlas_slot_ownership_reconcile",
    )
    fresh = build_exact_atlas_slot_ownership_plan(request)
    if cancel_event.is_set():
        raise WaitCancelled("Atlas slot ownership reconciliation cancelled")
    if not renew():
        raise RuntimeError("shared queue ownership became stale")
    progress(
        "Atlas slot ownership 영수증 갱신",
        completed=0,
        remaining=1,
        unit_stage="atlas_slot_ownership_reconcile",
    )
    result = apply_atlas_slot_ownership_reconciliation(fresh)
    if result.get("apply_status") != "reconciled":
        return {
            "status": "failed",
            "outcome": "failed",
            "shared_queue_success": False,
            "reason": "ownership reconciliation did not commit",
            "result": result,
        }
    progress(
        "Atlas slot ownership 영수증 갱신",
        completed=1,
        remaining=0,
        unit_stage="atlas_slot_ownership_reconcile",
    )
    return {
        "status": "completed",
        "outcome": "completed",
        "shared_queue_success": True,
        "exact_targets": [fresh["target_spm"]],
        "plan_sha256": fresh["plan_sha256"],
        "apply_result": result,
    }


def execute_exact_generator_request(
    request: Mapping, *, progress, cancel_event, lease
):
    if request.get("repair_action") == ATLAS_SLOT_OWNERSHIP_RECONCILE:
        return execute_exact_atlas_slot_ownership_request(
            request,
            progress=progress,
            cancel_event=cancel_event,
            lease=lease,
        )
    (
        module,
        cfg,
        units,
        runtime_objects,
        identities,
        settings,
        canonical,
    ) = build_exact_runtime_plan(
        request,
        include_identities=(request.get("repair_action") != CLUSTER_REFRESH),
    )
    app = module.App.__new__(module.App)
    report = _ProgressReport(progress, cancel_event, lease, len(units))
    results = []
    verify = bool(cfg.get("verify_speedtree", True))
    if request.get("repair_action") == CLUSTER_REFRESH:
        fast_results = []
        validation_cache = RelationValidationCache()
        for index, (unit, runtime_unit) in enumerate(
            zip(units, runtime_objects),
            1,
        ):
            report.current = index - 1
            report.current_stage_code = str(unit["stage"])
            report.raise_if_cancelled()
            blend_name = Path(runtime_unit.get("blend") or "").name
            progress(
                f"선택 관계 확인 {index}/{len(units)} · {blend_name}",
                completed=index - 1,
                remaining=len(units) - index + 1,
                unit_id=unit["unit_id"],
                unit_stage=unit["stage"],
            )
            current_result = _fast_current_cluster_result(
                runtime_unit,
                validation_cache=validation_cache,
            )
            if current_result is None:
                break
            fast_results.append({
                "unit_id": unit["unit_id"],
                "stage": unit["stage"],
                "result": module.App._connected_result_summary(
                    current_result
                ),
            })
            report.current = index
            progress(
                f"선택 관계 최신 상태 {index}/{len(units)}",
                completed=index,
                remaining=len(units) - index,
                unit_id=unit["unit_id"],
                unit_stage=unit["stage"],
            )
        if len(fast_results) == len(units):
            return {
                "status": "completed",
                "outcome": "completed",
                "shared_queue_success": True,
                "exact_targets": canonical,
                "units": fast_results,
                "fast_current_check": True,
            }
        if not identities:
            settings = connected_settings(
                cfg,
                verify,
                settings.get("board_root"),
                include_cluster_producer=True,
            )
            identities = scope_dependency_identities(units, settings)
    for index, (unit, runtime_unit) in enumerate(zip(units, runtime_objects), 1):
        report.current = index - 1
        report.current_stage_code = str(unit["stage"])
        report.raise_if_cancelled()
        progress(
            (
                "Cluster 갱신 중"
                if unit["stage"] == "cluster_refresh"
                else "Generator Sync 중"
            ),
            completed=index - 1,
            remaining=len(units) - index + 1,
            unit_id=unit["unit_id"],
            unit_stage=unit["stage"],
        )
        attempt = app._execute_connected_runtime_unit(
            unit,
            runtime_unit,
            cfg,
            verify,
            settings,
            identities[unit["unit_id"]],
            report,
            report,
        )
        if not attempt.get("ok"):
            return {
                "status": "failed",
                "outcome": "failed",
                "shared_queue_success": False,
                "failed_unit": unit["unit_id"],
                "reason": attempt.get("reason"),
                "classification": attempt.get("classification"),
                "completed_units": len(results),
            }
        report.current = index
        rebase_updates = (
            {}
            if attempt["result"].get("no_change")
            else rebase_authorized_dependency_identities(
                unit,
                units[index:],
                identities,
                settings,
            )
        )
        rebase_receipts = []
        for unit_id, update in sorted(rebase_updates.items()):
            identities[unit_id] = copy.deepcopy(update["identity"])
            rebase_receipts.append({
                key: copy.deepcopy(value)
                for key, value in update.items()
                if key != "identity"
            })
        result_row = {
            "unit_id": unit["unit_id"],
            "stage": unit["stage"],
            "result": module.App._connected_result_summary(attempt["result"]),
        }
        if rebase_receipts:
            result_row["authorized_dependency_rebases"] = rebase_receipts
        results.append(result_row)
        progress(
            "Generator/Cluster exact repair",
            completed=index,
            remaining=len(units) - index,
            unit_id=unit["unit_id"],
            unit_stage=unit["stage"],
        )
    return {
        "status": "completed",
        "outcome": "completed",
        "shared_queue_success": True,
        "exact_targets": canonical,
        "units": results,
    }


def _parser():
    parser = argparse.ArgumentParser(
        description="SPM Generator Sync bounded exact-target repair"
    )
    parser.add_argument(
        "--repair-action",
        choices=[
            GENERATOR_SYNC,
            CLUSTER_REFRESH,
            GENERATOR_SYNC_AND_CLUSTER,
            ATLAS_SLOT_OWNERSHIP_RECONCILE,
        ],
        required=True,
    )
    parser.add_argument(
        "--ownership-plan",
        help="sealed Atlas slot ownership plan JSON (required for its action)",
    )
    parser.add_argument("--target-spm", action="append", required=True)
    parser.add_argument(
        "--provider-blend",
        action="append",
        default=[],
        help=(
            "exact Cluster provider blend; skips the full board inventory and "
            "refreshes only this provider/target relation"
        ),
    )
    parser.add_argument("--parent-retry-id", required=True)
    parser.add_argument("--request-id", required=True)
    parser.add_argument("--receipt", required=True)
    parser.add_argument("--reason-code", action="append", default=[])
    return parser


def main(argv=None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    provenance = {
        "source": "SPM_Generator_Sync.bat",
        "reason_codes": args.reason_code or ["public_exact_target_request"],
    }
    if args.provider_blend:
        if args.repair_action != CLUSTER_REFRESH:
            parser.error("--provider-blend is supported by cluster-refresh only")
        provenance["provider_blends"] = args.provider_blend
    if args.repair_action == ATLAS_SLOT_OWNERSHIP_RECONCILE:
        if not args.ownership_plan:
            parser.error("--ownership-plan is required for Atlas slot ownership")
        try:
            ownership_plan = json.loads(
                Path(args.ownership_plan).read_text(encoding="utf-8")
            )
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            parser.error(f"cannot read --ownership-plan: {exc}")
        if not isinstance(ownership_plan, dict):
            parser.error("--ownership-plan must contain a JSON object")
        provenance["ownership_plan"] = ownership_plan
    request = build_exact_target_request(
        tool=GENERATOR_SYNC_TOOL,
        repair_action=args.repair_action,
        target_spms=args.target_spm,
        repair_stage=args.repair_action,
        provenance=provenance,
        parent_retry_id=args.parent_retry_id,
        request_id=args.request_id,
        receipt=args.receipt,
    )
    terminal = run_exact_target_request(request, execute_exact_generator_request)
    return int(
        terminal.get(
            "exit_code",
            0 if terminal.get("status") == "completed" else 1,
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
