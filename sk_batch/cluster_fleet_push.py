"""Push every current production Cluster Assembly into the open Unreal Editor.

Target selection comes only from manifests directly under
``<root>/<asset>/assembly``. Historical logs, staging copies, backups, and old
Unreal results never select work. Birch Paper is sorted last by default.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from exact_push import DEFAULT_BLENDER, LOG_DIR, ExactPushError, build_exact_push_command


DEFAULT_ROOT = Path(r"D:\OneDrive\Forestportfolio\02_nature\Tree")


def discover_current_cluster_targets(root: Path) -> tuple[list[dict], list[dict]]:
    root = root.expanduser().resolve()
    targets = []
    missing = []
    for manifest_path in root.glob("*/assembly/*_cluster_assembly_bindings.json"):
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            missing.append({
                "manifest": str(manifest_path),
                "reason": f"unreadable_manifest: {exc}",
            })
            continue
        if manifest.get("status") != "ready" or manifest.get("content_decision") != "build":
            continue
        stem = str(manifest.get("full_asset_stem") or "").strip()
        parts = list(manifest.get("parts") or [])
        if not stem or not parts:
            missing.append({
                "manifest": str(manifest_path),
                "stem": stem,
                "reason": "ready_build_manifest_has_no_parts_or_stem",
            })
            continue
        spm = manifest_path.parents[1] / f"{stem}.spm"
        required = {
            "spm": spm,
            "blend": spm.with_suffix(".blend"),
            "wind_json": Path(
                str(((manifest.get("wind_contract") or {}).get("wind_json") or {}).get("path") or "")
            ),
        }
        absent = [name for name, path in required.items() if not str(path) or not path.is_file()]
        if absent:
            missing.append({
                "manifest": str(manifest_path),
                "stem": stem,
                "reason": "missing_required_current_data",
                "missing": absent,
                "paths": {name: str(path) for name, path in required.items()},
            })
            continue
        targets.append({
            "stem": stem,
            "spm": spm.resolve(),
            "manifest": manifest_path.resolve(),
            "expected_parts": len(parts),
            "expected_bindings": sum(len(part.get("bindings") or []) for part in parts),
            "birch_paper": "birch_paper" in stem.casefold(),
        })
    targets.sort(key=lambda row: (row["birch_paper"], row["stem"].casefold()))
    return targets, missing


def validate_live_result(report: dict, target: dict) -> dict:
    unreal_result = report.get("unreal_result") or {}
    assembly = unreal_result.get("cluster_assembly") or {}
    build = assembly.get("build") or {}
    built_parts = list(build.get("parts") or [])
    wind = build.get("dynamic_wind") or {}
    provenance = build.get("provenance") or {}
    problems = []
    if report.get("status") != "ok" or unreal_result.get("status") != "imported_ok":
        problems.append("unreal_import_not_ok")
    if build.get("status") != "ok":
        problems.append("assembly_build_not_ok")
    if len(built_parts) != target["expected_parts"]:
        problems.append(
            f"part_count:{len(built_parts)}!={target['expected_parts']}"
        )
    if int(build.get("binding_count") or 0) != target["expected_bindings"]:
        problems.append(
            f"binding_count:{int(build.get('binding_count') or 0)}!={target['expected_bindings']}"
        )
    if not built_parts or any(int(row.get("bindings") or 0) <= 0 for row in built_parts):
        problems.append("one_or_more_3d_parts_have_no_bindings")
    if wind.get("success") is not True:
        problems.append("assembly_wind_import_failed")
    if provenance.get("success") is not True:
        problems.append("assembly_provenance_missing")
    return {
        "ok": not problems,
        "problems": problems,
        "assembly": build.get("assembly"),
        "built_parts": len(built_parts),
        "binding_count": int(build.get("binding_count") or 0),
        "wind_success": wind.get("success") is True,
        "provenance_success": provenance.get("success") is True,
    }


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Push and verify all current production Cluster Assemblies",
    )
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--blender", type=Path, default=DEFAULT_BLENDER)
    parser.add_argument("--log-dir", type=Path, default=LOG_DIR)
    parser.add_argument("--only", action="append", default=[])
    parser.add_argument("--skip-birch", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    targets, missing = discover_current_cluster_targets(args.root)
    filters = [value.casefold() for value in args.only]
    if filters:
        targets = [
            row for row in targets
            if any(value in row["stem"].casefold() for value in filters)
        ]
    if args.skip_birch:
        targets = [row for row in targets if not row["birch_paper"]]

    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    fleet_report_path = args.log_dir / f"cluster_fleet_push_{run_id}.json"
    fleet = {
        "status": "running",
        "root": str(args.root.expanduser().resolve()),
        "birch_paper_order": "last",
        "targets": [str(row["spm"]) for row in targets],
        "missing_current_assembly_data": missing,
        "results": [],
    }
    args.log_dir.mkdir(parents=True, exist_ok=True)
    fleet_report_path.write_text(json.dumps(fleet, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"SK_CLUSTER_FLEET_TARGETS={len(targets)}")
    print(f"SK_CLUSTER_FLEET_REPORT={fleet_report_path}")
    if args.dry_run:
        fleet["status"] = "dry_run"
        fleet_report_path.write_text(json.dumps(fleet, ensure_ascii=False, indent=2), encoding="utf-8")
        return 0

    for index, target in enumerate(targets, 1):
        print(f"[{index}/{len(targets)}] PUSH {target['stem']}", flush=True)
        result = {
            "stem": target["stem"],
            "spm": str(target["spm"]),
            "manifest": str(target["manifest"]),
        }
        try:
            command, outputs = build_exact_push_command(
                target["spm"],
                blender=args.blender,
                log_dir=args.log_dir,
                run_id=f"fleet_{run_id}_{index:03d}",
            )
            completed = subprocess.run(command, check=False)
            result["returncode"] = completed.returncode
            result["report"] = str(outputs["report"])
            if completed.returncode:
                raise RuntimeError(f"production push exited {completed.returncode}")
            report = json.loads(outputs["report"].read_text(encoding="utf-8"))
            verification = validate_live_result(report, target)
            result["verification"] = verification
            if not verification["ok"]:
                raise RuntimeError("; ".join(verification["problems"]))
            result["status"] = "verified_in_unreal"
        except (ExactPushError, OSError, ValueError, RuntimeError) as exc:
            result["status"] = "failed"
            result["error"] = str(exc)
        fleet["results"].append(result)
        fleet_report_path.write_text(json.dumps(fleet, ensure_ascii=False, indent=2), encoding="utf-8")

    failed = [row for row in fleet["results"] if row["status"] != "verified_in_unreal"]
    fleet["status"] = "ok" if not failed else "failed"
    fleet["verified_count"] = len(fleet["results"]) - len(failed)
    fleet["failed_count"] = len(failed)
    fleet_report_path.write_text(json.dumps(fleet, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"SK_CLUSTER_FLEET_VERIFIED={fleet['verified_count']}")
    print(f"SK_CLUSTER_FLEET_FAILED={fleet['failed_count']}")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
