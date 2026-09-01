"""Headless PCG Step 3 exact-target adapter used by BAT and SK retry."""

from __future__ import annotations

import argparse
import copy
import hashlib
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

from exact_target_command import build_exact_target_request, run_exact_target_request
from atlas_producer_rebind import (
    AtlasProducerRebindProofError,
    apply_atlas_producer_registry_rebind,
    build_atlas_producer_rebind_proof,
    plan_atlas_producer_registry_rebind,
    validate_atlas_producer_rebind_proof,
    validate_atlas_producer_refresh_receipt,
)
from atlas_target_registry import (
    capture_target_registry_preimage,
    load_target_registry,
    registry_path_for_blend,
    restore_target_registry_preimage,
)
from atlas_manifest_resolver import repair_atlas_manifest_mirrors
from pcg_canonical_outputs import refresh_atlas_manifests_for_spm
from repair_orchestration import (
    ATLAS_MANIFEST_MIRROR_REPAIR,
    ATLAS_PRODUCER_REFRESH,
    PCG_TEXTURE_TOOL,
    STEP3_STANDARD,
    canonical_exact_spm,
)
from shared_queue_runtime import WaitCancelled


class AtlasProducerRefreshRollbackError(RuntimeError):
    """Producer failed and the exact registry rollback also failed."""

    def __init__(self, original_error, rollback_error, *, evidence):
        self.original_error = original_error
        self.rollback_error = rollback_error
        self.evidence = copy.deepcopy(evidence)
        super().__init__(
            "Atlas producer refresh failed and exact registry rollback was "
            f"blocked: producer={original_error}; rollback={rollback_error}"
        )


class AtlasProducerRefreshCommittedError(RuntimeError):
    """Child reporting failed after the canonical producer had committed."""

    def __init__(self, original_error, *, receipt):
        self.original_error = original_error
        self.canonical_receipt = copy.deepcopy(receipt)
        super().__init__(
            "Atlas producer child/reporting failed after the canonical "
            "receipt committed; registry remains canonical: "
            f"{original_error}"
        )


def _load_gui_module():
    name = "_speedtree_pcg_texture_exact_target_backend"
    path = TOOL_DIR / "pcg_texture_gui.pyw"
    loaded = sys.modules.get(name)
    if loaded is not None and Path(loaded.__file__).resolve() == path.resolve():
        return loaded
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load PCG texture backend: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(name, None)
        raise
    return module


def _same_path(left, right) -> bool:
    if os.path.normcase(os.path.abspath(str(left))).casefold() == os.path.normcase(
        os.path.abspath(str(right))
    ).casefold():
        return True
    try:
        return os.path.samefile(left, right)
    except OSError:
        return False


def _sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _producer_relation_from_request(request: Mapping):
    if request.get("repair_action") != ATLAS_PRODUCER_REFRESH:
        raise ValueError("request is not an Atlas producer refresh action")
    targets = list(request.get("target_spms") or ())
    if len(targets) != 1:
        raise ValueError("Atlas producer refresh requires one exact target")
    canonical = Path(targets[0]).expanduser().absolute()
    if not canonical.is_file() or canonical.suffix.casefold() != ".spm":
        raise FileNotFoundError(
            f"exact Atlas producer target does not exist: {canonical}"
        )
    relation = (request.get("provenance") or {}).get("producer_relation")
    if not isinstance(relation, Mapping):
        raise AtlasProducerRebindProofError(
            "exact Atlas producer relation proof is missing"
        )
    proof = validate_atlas_producer_rebind_proof(
        relation,
        canonical_spm=canonical,
    )
    if proof["producer"]["connection_mode"] != "assets_only":
        raise AtlasProducerRebindProofError(
            "exact Atlas producer refresh currently supports assets-only producers"
        )
    return canonical, proof


def build_exact_atlas_producer_refresh_plan(request: Mapping):
    """Revalidate proof/registry state and return a non-mutating exact plan."""

    canonical, proof = _producer_relation_from_request(request)
    blend = proof["producer"]["blend"]["path"]
    legacy = proof["legacy_spm"]["path"]
    registry = load_target_registry(blend)
    if not registry:
        raise AtlasProducerRebindProofError(
            "Atlas producer registry disappeared after audit"
        )
    targets = registry["target_spms"]
    legacy_on = any(_same_path(value, legacy) for value in targets)
    canonical_on = any(_same_path(value, canonical) for value in targets)
    if legacy_on:
        fresh = build_atlas_producer_rebind_proof(
            canonical,
            proof["legacy_manifest"]["path"],
            inventory_paths=[canonical],
        )
        if fresh["proof_sha256"] != proof["proof_sha256"]:
            raise AtlasProducerRebindProofError(
                "Atlas producer relation changed after audit"
            )
        registry_status = "rebind_required"
    elif canonical_on:
        for record in (
            proof["canonical_spm"],
            proof["legacy_manifest"],
            proof["pair"]["receipt"],
            proof["producer"]["blend"],
        ):
            path = Path(record["path"])
            if not path.is_file() or _sha256_file(path) != record["sha256"]:
                raise AtlasProducerRebindProofError(
                    "Atlas producer authority changed after registry rebind"
                )
        registry_status = "already_rebound"
    else:
        raise AtlasProducerRebindProofError(
            "Atlas producer registry contains neither exact legacy nor canonical target"
        )
    return {
        "status": "ready",
        "canonical_spm": str(canonical),
        "producer_blend": blend,
        "registry_status": registry_status,
        "proof": proof,
        "registry_plan": plan_atlas_producer_registry_rebind(proof),
    }


def _exact_item(module, report, target: Path) -> tuple[dict, list[str]]:
    inventory = []
    matches = []
    for raw_item in report.get("items") or ():
        paths = [str(path) for path in module.spm_paths_for_item(raw_item)]
        inventory.extend(paths)
        if any(_same_path(path, target) for path in paths):
            matches.append(raw_item)
    canonical = canonical_exact_spm(target, inventory)
    if len(matches) != 1:
        raise RuntimeError(
            "exact target must resolve to exactly one current PCG inventory row"
        )
    item = copy.deepcopy(matches[0])
    return item, inventory


class _Value:
    def __init__(self):
        self.value = ""

    def set(self, value):
        self.value = str(value)


class _Tree:
    def set(self, *_args, **_kwargs):
        return None


def _rows_for_exact_target(rows, canonical):
    """Keep only active material consumers owned by the exact SPM."""

    selected = []
    for raw_row in rows:
        row = copy.deepcopy(raw_row)
        material_targets = [
            target
            for target in row.get("material_targets") or ()
            if target.get("spm") and _same_path(target["spm"], canonical)
        ]
        material_spms = [
            path for path in row.get("material_spms") or ()
            if _same_path(path, canonical)
        ]
        cluster_owned = bool(
            row.get("cluster_spm")
            and _same_path(row["cluster_spm"], canonical)
        )
        if not material_targets and not material_spms and not cluster_owned:
            continue
        row["material_targets"] = material_targets
        row["material_spms"] = material_spms
        if row.get("_material_current_refs") is not None:
            row["_material_current_refs"] = [
                current
                for current in row.get("_material_current_refs") or ()
                if current.get("spm")
                and _same_path(current["spm"], canonical)
            ]
        selected.append(row)
    return selected


def _make_authoritative_step3_report(module, cfg, audit_folder):
    """Re-audit one Step 3 scope with the same physical mutation evidence as UI."""

    session_evidence = {}
    report = module.make_report(
        cfg,
        targets=[str(audit_folder)],
        mutation_authority=True,
        session_evidence=session_evidence,
    )
    module.cache_blender_connection_rows(
        report,
        verify_physical=True,
        read_cache=False,
        session_evidence=session_evidence,
    )
    return report


def _bind_authoritative_step3_scope(app, report, item):
    """Keep the full live scope available to the normal Step 3 normalizer."""

    app.report = {**report, "items": [item]}
    app._step3_live_scope_report = report


def build_step3_standard_plan(target_spm: str | Path, *, config=None):
    """Re-audit and build the normal Step 3 plan for one canonical SPM."""

    module = _load_gui_module()
    target = Path(target_spm).expanduser().absolute()
    cfg = dict(config or module.load_config())
    audit_folder = module.step3_audit_folder_for_spm(target)
    report = _make_authoritative_step3_report(
        module,
        cfg,
        audit_folder,
    )
    item, inventory = _exact_item(module, report, target)
    canonical = canonical_exact_spm(target, inventory)

    # Build the full active-consumer plan first, then constrain only mutation
    # targets.  This preserves shared texture-owner and canonical role logic.
    mini = {"items": [item], "pcg_targets": report.get("pcg_targets", {})}
    texture_plan = module.build_texture_plan_from_report(
        mini, "<exact-target-step3>"
    )
    rows = _rows_for_exact_target(
        texture_plan.get("items") or [],
        canonical,
    )
    if not rows:
        raise RuntimeError(
            "exact target has no active PCG Step 3 material consumer rows"
        )

    app = module.App.__new__(module.App)
    app.cfg = cfg
    app.cfg["unreal_texture_sync_enabled"] = False
    _bind_authoritative_step3_scope(app, report, item)
    app.items = {str(item["folder"]): {"item": item, "checked": True}}
    app.target_items = {}
    app.texplan_cache = {item["folder"]: rows}
    app.texplan_errors = {}
    app.sync_state = {"entries": {}}
    app.status_var = _Value()
    app.tree = _Tree()
    app.root = None
    app.logs = []
    app.log = app.logs.append
    app._ui = lambda callback: callback()
    app._start_completion_refresh = lambda _status: None
    app._step3_finished = lambda *_args, **_kwargs: None

    plan = app._build_step3_execution_plan()
    planned_targets = list(plan.get("exact_step3_spms") or ())
    if not any(_same_path(path, canonical) for path in planned_targets):
        raise RuntimeError(
            "PCG Step 3 active-consumer plan does not own the exact target"
        )
    # The full item remains untouched for the semantic re-audit above, while
    # the mutation projection is narrowed to the one requested real file.
    plan["exact_step3_spms"] = [canonical]
    plan["force_unreal_verify"] = False
    app._validate_step3_plan_live_evidence(plan)
    return module, app, plan, canonical


def _run_exact_atlas_producer_refresh(
    request: Mapping,
    refresh_plan: Mapping,
):
    """Run the direct one-path assets-only Blender producer."""

    module = _load_gui_module()
    cfg = dict(module.load_config())
    proof = refresh_plan["proof"]
    canonical = Path(refresh_plan["canonical_spm"])
    blend = Path(refresh_plan["producer_blend"])
    legacy_manifest_path = Path(proof["legacy_manifest"]["path"])
    if _sha256_file(legacy_manifest_path) != proof["legacy_manifest"]["sha256"]:
        raise AtlasProducerRebindProofError(
            "legacy Atlas receipt changed before Blender producer launch"
        )
    legacy_manifest = json.loads(
        legacy_manifest_path.read_text(encoding="utf-8")
    )
    textures = legacy_manifest.get("textures") or {}
    albedo = Path(str(textures.get("albedo") or ""))
    alpha = Path(str(textures.get("alpha") or ""))
    if not albedo.is_file() or not alpha.is_file():
        raise AtlasProducerRebindProofError(
            "assets-only producer source Albedo/Alpha is missing"
        )
    material_name = str(
        legacy_manifest.get("atlas_asset_name")
        or legacy_manifest.get("material_name")
        or proof["producer"]["source_collection"]
    ).strip()
    if not material_name:
        raise AtlasProducerRebindProofError(
            "assets-only producer material identity is missing"
        )

    module.REPORT_DIR.mkdir(parents=True, exist_ok=True)
    stem = "exact_atlas_producer_" + "".join(
        character if character.isalnum() or character in "-_" else "_"
        for character in str(request.get("request_id") or "request")
    )
    report_path = module.REPORT_DIR / f"{stem}.json"
    proof_path = module.REPORT_DIR / f"{stem}.proof.json"
    authority_path = module.REPORT_DIR / f"{stem}.authority.json"
    proof_path.write_text(
        json.dumps(proof, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    canonical_manifest = (
        canonical.parent
        / ".atlas_leaf_speedtree_targets"
        / f"{canonical.stem}.json"
    )
    global_manifest = canonical.parent / "speedtree_import_manifest.json"
    import_readme = canonical.parent / "README_SPEEDTREE_IMPORT.md"
    child_payload = {
        "albedo": str(albedo),
        "alpha": str(alpha),
        "material_name": material_name,
        "blend_out": str(blend),
        "spms": [str(canonical)],
        "target_map_json": "",
        "build_spm": True,
        "reuse_existing_blend": True,
        "registry_managed_externally": True,
        "producer_refresh_proof_json": str(proof_path),
        "quality": "SPEEDTREE_LOW",
        "plate_mode": "SINGLE",
    }
    blender_exe = cfg.get("blender_exe", "")
    config_keys = ("blender_exe", "atlas_job_timeout")
    config_projection = module.mutation_config_projection(cfg, config_keys)
    target_memberships = [
        canonical.parent / "meshes",
        canonical.parent / ".atlas_leaf_speedtree_targets",
        canonical.parent / ".atlas_leaf_speedtree_scopes",
    ]
    observed_memberships = [
        *target_memberships,
        blend.parent / "_atlas_job_work",
    ]
    authority = module.capture_runtime_mutation_authority(
        action="atlas_producer_refresh",
        unit_id="atlas-producer-refresh",
        payload=child_payload,
        paths=[
            albedo,
            alpha,
            blend,
            canonical,
            canonical_manifest,
            global_manifest,
            import_readme,
            registry_path_for_blend(blend),
            proof_path,
        ],
        write_paths=[
            blend,
            canonical,
            canonical_manifest,
            global_manifest,
            import_readme,
        ],
        memberships=observed_memberships,
        write_memberships=target_memberships,
        config_projection=config_projection,
        tool_paths=[
            blender_exe,
            module.blender_user_startup_path(blender_exe),
            TOOL_DIR / "jobs" / "atlas_blend_job.py",
        ],
    )
    module.require_mutation_authority_unit(
        authority,
        "atlas-producer-refresh",
        current_payload=child_payload,
        current_config=config_projection,
    )
    authority_sha256 = module.write_child_mutation_authority(
        authority,
        "atlas-producer-refresh",
        authority_path,
    )
    command = module.atlas_blender_command(blender_exe) + [
        "--albedo", str(albedo),
        "--alpha", str(alpha),
        "--material-name", material_name,
        "--blend-out", str(blend),
        "--report", str(report_path),
        "--quality", "SPEEDTREE_LOW",
        "--plate-mode", "SINGLE",
        "--reuse-existing-blend",
        "--registry-managed-externally",
        "--producer-refresh-proof-json", str(proof_path),
        "--spm", str(canonical),
        "--build-spm",
        "--authority-json", str(authority_path),
        "--authority-sha256", authority_sha256,
    ]
    result = module.owned_run(
        command,
        source="pcg_st9_texture_batch.exact_target_repair.atlas_producer",
        run_factory=module.subprocess.run,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=cfg.get("atlas_job_timeout", 1800),
        creationflags=0x08000000,
    )
    data = (
        json.loads(report_path.read_text(encoding="utf-8"))
        if report_path.is_file()
        else {}
    )
    if result.returncode != 0:
        raise RuntimeError(
            data.get("error") or (result.stderr or result.stdout)[-400:]
        )
    module.validate_step2_job_report(data, require_generator_connections=False)
    receipt = validate_atlas_producer_refresh_receipt(
        proof,
        manifest_path=canonical_manifest,
    )
    module.complete_mutation_authority_unit(
        authority,
        "atlas-producer-refresh",
        post_paths=[
            blend,
            canonical,
            canonical_manifest,
            global_manifest,
            import_readme,
        ],
    )
    return {
        "report_path": str(report_path),
        "blender_report": data,
        "canonical_receipt": receipt,
    }


def execute_exact_atlas_producer_refresh(
    request: Mapping, *, progress, cancel_event, lease
):
    if cancel_event.is_set():
        raise WaitCancelled("exact Atlas producer refresh cancelled")
    renew = getattr(lease, "renew_and_check_current", lambda: True)
    if not renew():
        raise RuntimeError("shared queue lease is no longer current")
    progress("Atlas producer 관계 검증", completed=0, remaining=1)
    refresh_plan = build_exact_atlas_producer_refresh_plan(request)
    registry_preimage = capture_target_registry_preimage(
        refresh_plan["producer_blend"]
    )
    registry_result = None
    producer_started = False
    producer_committed = False
    producer_result = None
    try:
        registry_result = apply_atlas_producer_registry_rebind(
            refresh_plan["registry_plan"]
        )
        if cancel_event.is_set():
            raise WaitCancelled("exact Atlas producer refresh cancelled")
        if not renew():
            raise RuntimeError("shared queue lease became stale")
        progress(
            "Atlas producer canonical 영수증 생성",
            completed=0,
            remaining=1,
        )
        producer_started = True
        producer_result = _run_exact_atlas_producer_refresh(
            request,
            refresh_plan,
        )
        producer_committed = True
        progress(
            "Atlas producer canonical 영수증 생성",
            completed=1,
            remaining=0,
        )
    except Exception as original_error:
        rebind_committed = bool(
            isinstance(registry_result, Mapping)
            and registry_result.get("committed") is True
        )
        committed_receipt = None
        if producer_committed:
            committed_receipt = (
                (producer_result or {}).get("canonical_receipt")
                if isinstance(producer_result, Mapping)
                else None
            )
        elif producer_started:
            canonical = Path(refresh_plan["canonical_spm"])
            canonical_manifest = (
                canonical.parent
                / ".atlas_leaf_speedtree_targets"
                / f"{canonical.stem}.json"
            )
            try:
                committed_receipt = validate_atlas_producer_refresh_receipt(
                    refresh_plan["proof"],
                    manifest_path=canonical_manifest,
                )
            except Exception:
                committed_receipt = None

        if committed_receipt is not None:
            # The child can lose its report/nonzero handshake after the
            # staged target transaction committed.  Rolling only the registry
            # back here would create the inverse split-brain state.
            raise AtlasProducerRefreshCommittedError(
                original_error,
                receipt=committed_receipt,
            ) from original_error

        if rebind_committed:
            expected_post_state = registry_result.get("registry_state")
            try:
                rollback = restore_target_registry_preimage(
                    registry_preimage,
                    expected_registry_state=expected_post_state,
                )
            except Exception as rollback_error:
                raise AtlasProducerRefreshRollbackError(
                    original_error,
                    rollback_error,
                    evidence={
                        "registry_preimage_state": registry_preimage.get(
                            "state"
                        ),
                        "expected_rebound_state": expected_post_state,
                        "rollback_contract": getattr(
                            rollback_error,
                            "connected_retry_contract",
                            None,
                        ),
                    },
                ) from original_error
            try:
                original_error.add_note(
                    "Atlas producer registry rebind was rolled back to its "
                    "exact byte preimage after producer failure "
                    f"({rollback['registry_state']['sha256']})."
                )
            except AttributeError:
                pass
        raise
    return {
        "status": "completed",
        "outcome": "completed",
        "shared_queue_success": True,
        "exact_target": refresh_plan["canonical_spm"],
        "registry_rebind": registry_result,
        "producer": producer_result,
    }


def execute_step3_standard(request: Mapping, *, progress, cancel_event, lease):
    action = request.get("repair_action")
    if action == ATLAS_PRODUCER_REFRESH:
        return execute_exact_atlas_producer_refresh(
            request,
            progress=progress,
            cancel_event=cancel_event,
            lease=lease,
        )
    if action not in {STEP3_STANDARD, ATLAS_MANIFEST_MIRROR_REPAIR}:
        raise ValueError("PCG exact-target adapter repair action is unsupported")
    if len(request.get("target_spms") or ()) != 1:
        raise ValueError("PCG exact-target repair requires exactly one SPM")
    if cancel_event.is_set():
        raise WaitCancelled("exact-target texture repair cancelled")
    if action == ATLAS_MANIFEST_MIRROR_REPAIR:
        canonical = Path(request["target_spms"][0]).expanduser().absolute()
        progress("Atlas manifest 충돌 검증", completed=0, remaining=1)
        if getattr(lease, "renew_and_check_current", lambda: True)() is not True:
            raise RuntimeError("shared queue lease is no longer current")
        repair = repair_atlas_manifest_mirrors(canonical)
        refreshed = refresh_atlas_manifests_for_spm(
            canonical,
            require_complete=True,
        )
        progress(
            "Atlas manifest 복구 완료",
            completed=1,
            remaining=0,
            target=canonical,
        )
        return {
            "status": "completed",
            "outcome": "completed",
            "exact_target": canonical,
            "repair": repair,
            "canonical_refresh": refreshed,
            "shared_queue_success": True,
        }
    progress("PCG 텍스처 계획", completed=0, remaining=1)
    module, app, plan, canonical = build_step3_standard_plan(
        request["target_spms"][0]
    )
    if getattr(lease, "renew_and_check_current", lambda: True)() is not True:
        raise RuntimeError("shared queue lease is no longer current")
    progress("PCG 텍스처 복구", completed=0, remaining=1, target=canonical)
    report_path, run_report = app._begin_step3_run_report(
        plan["jobs"],
        plan["skipped"],
        plan["exact_step3_spms"],
        plan["sync_files"],
        force_unreal_verify=False,
        selected_rows=plan["selected_rows"],
    )
    result = app._run_step3(
        plan["jobs"],
        plan["exact_step3_spms"],
        plan["sync_files"],
        False,
        planned_skipped=len(plan["skipped"]),
        allowed_step3_row_keys=plan["eligible_row_keys"],
        step3_run_report_path=report_path,
        step3_run_report=run_report,
        exact_mutation_baseline=plan["_exact_mutation_baseline"],
    )
    shared = result.get("shared_queue_result") or {}
    progress("PCG 텍스처 복구 완료", completed=1, remaining=0, target=canonical)
    return {
        "status": "completed" if result.get("shared_queue_success") else "failed",
        "outcome": "completed" if result.get("shared_queue_success") else "failed",
        "exact_target": canonical,
        "force_rerender": False,
        "step3": shared,
        "report_path": str(report_path) if report_path else None,
        "shared_queue_success": bool(result.get("shared_queue_success")),
    }


def _parser():
    parser = argparse.ArgumentParser(
        description="PCG ST9 Texture bounded exact-target repair"
    )
    parser.add_argument(
        "--repair-action",
        choices=[
            STEP3_STANDARD,
            ATLAS_MANIFEST_MIRROR_REPAIR,
            ATLAS_PRODUCER_REFRESH,
        ],
        required=True,
    )
    parser.add_argument(
        "--producer-relation",
        help="sealed Atlas producer relation JSON (required for its action)",
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
        "source": "PCG_ST9_Texture_Batch.bat",
        "reason_codes": args.reason_code or ["public_exact_target_request"],
    }
    if args.repair_action == ATLAS_PRODUCER_REFRESH:
        if not args.producer_relation:
            parser.error("--producer-relation is required for Atlas producer refresh")
        try:
            producer_relation = json.loads(
                Path(args.producer_relation).read_text(encoding="utf-8")
            )
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            parser.error(f"cannot read --producer-relation: {exc}")
        if not isinstance(producer_relation, dict):
            parser.error("--producer-relation must contain a JSON object")
        provenance["producer_relation"] = producer_relation
    request = build_exact_target_request(
        tool=PCG_TEXTURE_TOOL,
        repair_action=args.repair_action,
        target_spms=args.target_spm,
        repair_stage="pcg_texture",
        provenance=provenance,
        parent_retry_id=args.parent_retry_id,
        request_id=args.request_id,
        receipt=args.receipt,
    )
    terminal = run_exact_target_request(request, execute_step3_standard)
    return int(
        terminal.get(
            "exit_code",
            0 if terminal.get("status") == "completed" else 1,
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
