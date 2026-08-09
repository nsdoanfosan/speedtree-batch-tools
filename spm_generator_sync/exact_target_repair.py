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
    dependency_identity,
)
from exact_target_command import build_exact_target_request, run_exact_target_request
from atlas_slot_ownership import (
    AtlasSlotOwnershipError,
    apply_atlas_slot_ownership_reconciliation,
    plan_atlas_slot_ownership_reconciliation,
    validate_atlas_slot_ownership_plan,
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


def exact_runtime_scope(target_spms, *, config=None):
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

    board = module.engine.scan_tree_folders(
        board_root,
        sk_only=bool(cfg.get("sk_only", True)),
        verify_physical=False,
    )
    scope = module.App._connected_scope_from_board(board)
    inventory = []
    groups = []
    requested_keys = {_key(path) for path in requested}
    requested_cluster_keys = {
        _key(path)
        for raw_row in scope["cluster_rows"]
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
    for raw_row in scope["cluster_rows"]:
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


def build_exact_runtime_plan(request: Mapping):
    module, cfg, board_root, groups, cluster_rows, canonical = exact_runtime_scope(
        request["target_spms"]
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
        include_cluster_producer=bool(cluster_rows),
    )
    identities = {
        unit["unit_id"]: dependency_identity(unit, settings)
        for unit in units
    }
    return module, cfg, units, runtime_objects, identities, settings, canonical


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
    ) = build_exact_runtime_plan(request)
    app = module.App.__new__(module.App)
    report = _ProgressReport(progress, cancel_event, lease, len(units))
    results = []
    verify = bool(cfg.get("verify_speedtree", True))
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
        results.append({
            "unit_id": unit["unit_id"],
            "stage": unit["stage"],
            "result": module.App._connected_result_summary(attempt["result"]),
        })
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
