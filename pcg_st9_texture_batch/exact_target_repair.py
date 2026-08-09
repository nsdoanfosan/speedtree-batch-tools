"""Headless PCG Step 3 exact-target adapter used by BAT and SK retry."""

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

from exact_target_command import build_exact_target_request, run_exact_target_request
from atlas_manifest_resolver import repair_atlas_manifest_mirrors
from pcg_canonical_outputs import refresh_atlas_manifests_for_spm
from repair_orchestration import (
    ATLAS_MANIFEST_MIRROR_REPAIR,
    PCG_TEXTURE_TOOL,
    STEP3_STANDARD,
    canonical_exact_spm,
)
from shared_queue_runtime import WaitCancelled


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


def build_step3_standard_plan(target_spm: str | Path, *, config=None):
    """Re-audit and build the normal Step 3 plan for one canonical SPM."""

    module = _load_gui_module()
    target = Path(target_spm).expanduser().absolute()
    cfg = dict(config or module.load_config())
    audit_folder = module.step3_audit_folder_for_spm(target)
    report = module.make_report(cfg, targets=[str(audit_folder)])
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
    app.report = {**report, "items": [item]}
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


def execute_step3_standard(request: Mapping, *, progress, cancel_event, lease):
    action = request.get("repair_action")
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
        choices=[STEP3_STANDARD, ATLAS_MANIFEST_MIRROR_REPAIR],
        required=True,
    )
    parser.add_argument("--target-spm", action="append", required=True)
    parser.add_argument("--parent-retry-id", required=True)
    parser.add_argument("--request-id", required=True)
    parser.add_argument("--receipt", required=True)
    parser.add_argument("--reason-code", action="append", default=[])
    return parser


def main(argv=None) -> int:
    args = _parser().parse_args(argv)
    provenance = {
        "source": "PCG_ST9_Texture_Batch.bat",
        "reason_codes": args.reason_code or ["public_exact_target_request"],
    }
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
