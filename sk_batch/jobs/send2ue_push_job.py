"""Headless Blender job: push a repaired SK .blend to Unreal via send2ue, then
wire the dynamic-wind JSON into the imported skeletal mesh and save to disk.

Run (Unreal editor must be open):
  blender.exe -b X.blend --python send2ue_push_job.py -- --report result.json

Steps:
  1. verify the send2ue <-> Unreal RPC connection
  2. bpy.ops.wm.send2ue('EXEC_DEFAULT')  (skip_animation_export forced on)
  3. remote-run Unreal python: CodexDynamicWindImportLibrary
     .import_dynamic_wind_json_to_skeletal_mesh(mesh, <stem>_dynamic_wind_...json)
     + save_asset/save_directory (send2ue imports are memory-only otherwise).
     The Unreal side writes a result JSON we read back for ground truth.
"""
import argparse
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import time
import traceback
from pathlib import Path

# send2ue normally reports connection failures through a Blender popup. In
# background mode popup_menu() can crash Blender before our JSON report is
# written. Development/error mode turns those UI reports into catchable Python
# exceptions instead.
os.environ.setdefault("SEND2UE_DEV", "1")

import bpy


def parse_args():
    argv = sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else []
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", required=True)
    parser.add_argument("--ue-timeout", type=float, default=180.0)
    parser.add_argument("--rpc-timeout-min", type=float, default=180.0)
    parser.add_argument("--rpc-timeout-max", type=float, default=900.0)
    parser.add_argument("--max-push-polygons", type=int, default=2_000_000)
    parser.add_argument("--max-push-bones", type=int, default=1_500)
    parser.add_argument("--skip-wind", action="store_true")
    parser.add_argument("--transport", choices=("rpc", "headless_export"), default="rpc")
    parser.add_argument("--manifest")
    parser.add_argument("--checkpoint")
    parser.add_argument("--batch-report")
    parser.add_argument("--export-root")
    parser.add_argument("--queue-id")
    parser.add_argument("--source-fingerprint", default="")
    parser.add_argument("--item-import-report")
    parser.add_argument("--unreal-ingest")
    parser.add_argument("--send2ue-unreal-py")
    return parser.parse_args(argv)


def write_report(path, data):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(data, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    os.replace(temporary, target)


def file_fingerprint(path):
    candidate = Path(path)
    hasher = hashlib.blake2b(digest_size=16)
    with candidate.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    stat = candidate.stat()
    return {
        "path": str(candidate.resolve()),
        "size": stat.st_size,
        "fingerprint": hasher.hexdigest(),
    }


def manifest_asset_files(manifest_assets):
    paths = []
    for manifest_asset in manifest_assets:
        asset_data = manifest_asset.get("asset_data") or {}
        for value in (
            asset_data.get("file_path"),
            asset_data.get("fcurve_file_path"),
            *(asset_data.get("lods") or {}).values(),
        ):
            if value and value not in paths:
                paths.append(value)
    missing = [str(path) for path in paths if not Path(path).is_file()]
    if missing:
        raise RuntimeError("Send2UE exported file missing: " + ", ".join(missing))
    return [file_fingerprint(path) for path in paths]


def command_json_files(manifest_assets):
    """Return absolute JSON dependencies embedded in extension command payloads."""
    pattern = re.compile(r'''(?i)(?:r)?["']([a-z]:[\\/][^"']+\.json)["']''')
    paths = []
    for manifest_asset in manifest_assets:
        command_groups = (
            list(manifest_asset.get("pre_import_commands") or [])
            + list(manifest_asset.get("post_import_commands") or [])
        )
        for commands in command_groups:
            for line in commands:
                for match in pattern.findall(str(line)):
                    path = Path(match)
                    if path.is_file() and path not in paths:
                        paths.append(path)
    return paths


def stable_fingerprint(data):
    encoded = json.dumps(
        data,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.blake2b(encoded, digest_size=16).hexdigest()


def unreal_log_checkpoint():
    log_path = Path(r"C:\UnrealProjects\MyProject2\Saved\Logs\MyProject2.log")
    try:
        return log_path, log_path.stat().st_size
    except OSError:
        return log_path, 0


def target_material_compile_failures(log_path, offset, material_names):
    try:
        with log_path.open("rb") as handle:
            handle.seek(offset)
            text = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return []

    targets = {
        "MI_" + name[2:] if name.startswith("M_") else name
        for name in material_names
        if name
    }
    lines = text.splitlines()
    failures = []
    for index, line in enumerate(lines):
        if "Failed to compile Material Instance" not in line:
            continue
        context = "\n".join(lines[index : index + 16])
        if targets and not any(target in context for target in targets):
            continue
        failures.append(context)
    return failures


def find_export_unit_name():
    """The Empty under the Export collection names the FBX -> the UE asset."""
    coll = bpy.data.collections.get("Export")
    if not coll:
        return None
    for obj in coll.objects:
        if obj.type == "EMPTY" and obj.children:
            return obj.name
    return None


def unreal_editor_running():
    """Best-effort distinction between an RPC error and an editor crash/exit."""
    if os.name != "nt":
        return None
    try:
        result = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq UnrealEditor.exe", "/NH"],
            capture_output=True,
            text=True,
            timeout=5,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        # result.stdout has been observed as None under background Blender
        # even with capture_output=True; a crash here would skip the report.
        return result.returncode == 0 and "UnrealEditor.exe" in (result.stdout or "")
    except Exception:
        return None


def enable_required_addon(addon_utils, module):
    """Enable only the add-ons that the isolated Push pipeline requires."""
    _loaded, enabled = addon_utils.check(module)
    if not enabled:
        bpy.ops.preferences.addon_enable(module=module)


def wait_for_json(path, timeout, label):
    deadline = time.time() + timeout
    while time.time() < deadline and not path.exists():
        time.sleep(1.0)
    if not path.exists():
        raise RuntimeError(f"{label}: no result file (timed out after {timeout:g}s)")
    return json.loads(path.read_text(encoding="utf-8"))


def export_complexity():
    """Return cheap counts for the exact mesh unit that Send2UE will export."""
    export_collection = bpy.data.collections.get("Export")
    objects = list(export_collection.all_objects) if export_collection else []
    meshes = [obj for obj in objects if obj.type == "MESH" and obj.data]
    armatures = set()
    for mesh in meshes:
        armature = mesh.find_armature()
        if armature and armature.data:
            armatures.add(armature)
    return {
        "mesh_count": len(meshes),
        "polygon_count": sum(len(mesh.data.polygons) for mesh in meshes),
        "bone_count": sum(len(armature.data.bones) for armature in armatures),
        "meshes": [
            {"name": mesh.name, "polygons": len(mesh.data.polygons)}
            for mesh in meshes
        ],
    }


def adaptive_rpc_timeout(complexity, polygon_limit, bone_limit, minimum, maximum):
    """Scale the RPC wait continuously from the export's relative complexity."""
    minimum = max(60, float(minimum))
    maximum = max(minimum, float(maximum))
    ratios = []
    if polygon_limit > 0:
        ratios.append(complexity["polygon_count"] / polygon_limit)
    if bone_limit > 0:
        ratios.append(complexity["bone_count"] / bone_limit)
    load_ratio = min(1.0, max(ratios, default=0.0))
    seconds = math.ceil(minimum + (maximum - minimum) * load_ratio)
    return seconds, load_ratio


def main():
    args = parse_args()
    report_path = Path(args.report).resolve()
    if not args.manifest:
        args.manifest = str(report_path.with_name(f"{report_path.stem}_manifest.json"))
    if not args.export_root:
        args.export_root = str(report_path.with_name(f"{report_path.stem}_export"))
    if not args.unreal_ingest:
        args.unreal_ingest = str(Path(__file__).resolve().parents[1] / "unreal_ingest.py")
    blend_path = bpy.data.filepath
    report = {
        "blend": blend_path,
        "status": "failed",
        "stage": "startup",
        "unreal_rpc_started": False,
    }
    try:
        import addon_utils

        # --factory-startup keeps unrelated UI/GPU add-ons out of background
        # Blender, so required handoff registrations must be explicit.
        report["stage"] = "addon_setup"
        enable_required_addon(addon_utils, "speedtree_bone_weight_repair")
        enable_required_addon(addon_utils, "ue_unique_export_names_addon")
        enable_required_addon(addon_utils, "send2ue")

        from speedtree_bone_weight_repair.core import (
            consolidate_speedtree_group_materials,
            normalize_speedtree_material_textures,
        )

        report["stage"] = "texture_validation"
        material_consolidation = consolidate_speedtree_group_materials(
            bpy.context.scene.objects
        )
        report["material_consolidation"] = material_consolidation
        texture_normalization = normalize_speedtree_material_textures(bpy.context.scene.objects)
        report["texture_normalization"] = texture_normalization
        # Save only when this pass actually mutated the file: verified large
        # blends should not be rewritten on every push just because their
        # materials were examined.
        consolidation_changed = material_consolidation.get("status") == "applied"
        normalization_changed = bool(texture_normalization.get("changed_count"))
        report["blend_resaved"] = bool(consolidation_changed or normalization_changed)
        if report["blend_resaved"]:
            bpy.ops.wm.save_as_mainfile(filepath=blend_path)
        missing_textures = texture_normalization.get("missing", [])
        if missing_textures:
            details = []
            for item in missing_textures:
                roles = ", ".join(item.get("missing_roles", [])) or "대응 세트"
                details.append(
                    f"{item.get('material', '?')} -> "
                    f"{item.get('expected_texture_base', 'T_?')} ({roles})"
                )
            raise RuntimeError(
                "PCG ST9 Texture Batch 산출물 누락; Unreal Push 중단: "
                + " | ".join(details)
            )

        # The polygon/bone values are scaling references for the adaptive RPC
        # timeout, not hard limits: over-reference exports proceed at the
        # maximum timeout instead of being skipped as manual_required.
        report["stage"] = "complexity_guard"
        complexity = export_complexity()
        report["complexity"] = complexity
        over_reference = []
        if (
            args.max_push_polygons > 0
            and complexity["polygon_count"] > args.max_push_polygons
        ):
            over_reference.append(
                f"폴리곤 {complexity['polygon_count']:,} > {args.max_push_polygons:,}"
            )
        if args.max_push_bones > 0 and complexity["bone_count"] > args.max_push_bones:
            over_reference.append(
                f"본 {complexity['bone_count']:,} > {args.max_push_bones:,}"
            )
        if over_reference:
            report["complexity_over_reference"] = over_reference
            print(
                "[SK Batch] 복잡도 기준 초과 (최대 RPC 타임아웃으로 계속 진행): "
                + ", ".join(over_reference)
            )

        from send2ue.constants import PathModes
        from send2ue.core import ingest, utilities
        from send2ue.dependencies import unreal as send2ue_unreal_dependency
        from send2ue.dependencies.unreal import run_commands, set_rpc_env

        if not args.send2ue_unreal_py:
            args.send2ue_unreal_py = str(Path(send2ue_unreal_dependency.__file__).resolve())

        if args.transport == "rpc":
            report["stage"] = "unreal_preflight"
            if not utilities.is_unreal_connected():
                raise RuntimeError(
                    "Unreal editor is not running / RPC not reachable. Open the project first."
                )
            report["unreal_rpc_started"] = True

            rpc_timeout, rpc_load_ratio = adaptive_rpc_timeout(
                complexity,
                args.max_push_polygons,
                args.max_push_bones,
                args.rpc_timeout_min,
                args.rpc_timeout_max,
            )
            set_rpc_env("RPC_TIME_OUT", rpc_timeout)
            report["rpc_timeout_seconds"] = rpc_timeout
            report["rpc_timeout"] = {
                "seconds": rpc_timeout,
                "minimum": max(60, float(args.rpc_timeout_min)),
                "maximum": max(
                    max(60, float(args.rpc_timeout_min)),
                    float(args.rpc_timeout_max),
                ),
                "load_ratio": round(rpc_load_ratio, 6),
                "polygon_ratio": round(
                    complexity["polygon_count"] / args.max_push_polygons, 6
                ) if args.max_push_polygons > 0 else None,
                "bone_ratio": round(
                    complexity["bone_count"] / args.max_push_bones, 6
                ) if args.max_push_bones > 0 else None,
            }
            print(
                f"[SK Batch] RPC timeout: {rpc_timeout}s "
                f"(load {rpc_load_ratio:.1%}, range "
                f"{args.rpc_timeout_min:g}-{args.rpc_timeout_max:g}s)"
            )
        else:
            rpc_timeout = 0

        scene_props = bpy.context.scene.send2ue
        if hasattr(scene_props, "skip_animation_export"):
            scene_props.skip_animation_export = True

        unit_name = find_export_unit_name() or Path(blend_path).stem
        folder = scene_props.unreal_mesh_folder_path
        report["unit_name"] = unit_name
        report["unreal_folder"] = folder
        report["transport"] = args.transport

        export_root = Path(args.export_root).resolve()
        export_root.mkdir(parents=True, exist_ok=True)
        scene_props.path_mode = PathModes.SEND_TO_DISK.value
        scene_props.disk_mesh_folder_path = str(export_root)
        scene_props.disk_animation_folder_path = str(export_root / "animations")
        scene_props.disk_groom_folder_path = str(export_root / "groom")

        blend_dir = Path(blend_path).parent
        mesh_path = folder.rstrip("/") + "/" + unit_name
        wind_json = (
            blend_dir
            / "JSON"
            / f"{unit_name}_dynamic_wind_import_from_megaplant_groups.json"
        )

        report["stage"] = "send2ue_export"
        result = bpy.ops.wm.send2ue("EXEC_DEFAULT")
        if "FINISHED" not in result:
            raise RuntimeError(f"send2ue returned {result}")
        report["send2ue"] = "FINISHED"

        report["stage"] = "manifest"
        manifest_assets = ingest.build_manifest_items(scene_props)
        if not manifest_assets:
            raise RuntimeError("Send2UE produced no manifest assets")

        exported_files = manifest_asset_files(manifest_assets)
        json_dir = blend_dir / "JSON"
        handoff_paths = []
        if json_dir.is_dir():
            handoff_paths = [
                path
                for path in sorted(json_dir.glob("*.json"))
                if unit_name.casefold() in path.stem.casefold()
            ]
        for path in command_json_files(manifest_assets):
            if path not in handoff_paths:
                handoff_paths.append(path)
        handoff_files = [file_fingerprint(path) for path in handoff_paths]
        wind_file = (
            None
            if args.skip_wind or not wind_json.is_file()
            else file_fingerprint(wind_json)
        )
        code_files = [
            file_fingerprint(args.unreal_ingest),
            file_fingerprint(args.send2ue_unreal_py),
        ]
        material_pipeline = (
            Path(args.send2ue_unreal_py).resolve().parents[1]
            / "resources"
            / "pipeline"
            / "ue_material_setup.py"
        )
        if material_pipeline.is_file():
            code_files.append(file_fingerprint(material_pipeline))

        contract = {
            "schema_version": 1,
            "source_fingerprint": args.source_fingerprint,
            "blend": str(Path(blend_path).resolve()),
            "unit_name": unit_name,
            "unreal_folder": folder,
            "mesh_path": mesh_path,
            "assets": manifest_assets,
            "exported_files": exported_files,
            "handoff_files": handoff_files,
            "wind_file": wind_file,
            "code_files": code_files,
        }
        fingerprint = stable_fingerprint(contract)
        queue_id = args.queue_id or str(Path(blend_path).resolve())
        item_import_report = args.item_import_report or str(
            Path(args.report).with_name(
                f"{Path(args.report).stem}_unreal.json"
            )
        )
        item = {
            **contract,
            "queue_id": queue_id,
            "fingerprint": fingerprint,
            "send2ue_unreal_py": str(Path(args.send2ue_unreal_py).resolve()),
            "wind_json": None if args.skip_wind else str(wind_json),
            "checkout_asset_paths": [
                mesh_path,
                mesh_path + "_Skeleton",
                mesh_path + "_PhysicsAsset",
            ],
            "report_path": str(Path(item_import_report).resolve()),
            "export_report_path": str(Path(args.report).resolve()),
        }
        manifest_path = Path(args.manifest).resolve()
        checkpoint_path = Path(
            args.checkpoint
            or manifest_path.with_name(f"{manifest_path.stem}_checkpoint.json")
        ).resolve()
        batch_report_path = Path(
            args.batch_report
            or manifest_path.with_name(f"{manifest_path.stem}_report.json")
        ).resolve()
        manifest = {
            "schema_version": 1,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "checkpoint_path": str(checkpoint_path),
            "report_path": str(batch_report_path),
            "max_item_crash_retries": 2,
            "items": [item],
        }
        write_report(manifest_path, manifest)
        report["manifest"] = str(manifest_path)
        report["manifest_fingerprint"] = fingerprint
        report["exported_files"] = exported_files
        report["handoff_files"] = handoff_files
        report["checkpoint"] = str(checkpoint_path)
        report["batch_report"] = str(batch_report_path)
        report["item_import_report"] = str(Path(item_import_report).resolve())

        if args.transport == "headless_export":
            report["stage"] = "exported_pending_unreal"
            report["status"] = "exported_pending_unreal"
            report["failure_kind"] = None
        else:
            if batch_report_path.exists():
                batch_report_path.unlink()
            runner_path = str(Path(args.unreal_ingest).resolve())
            rpc_lines = [
                "import importlib.util, sys",
                f"_runner_path = {runner_path!r}",
                "_spec = importlib.util.spec_from_file_location('sk_batch_unreal_ingest_rpc', _runner_path)",
                "if _spec is None or _spec.loader is None:",
                "\traise RuntimeError('Could not load SK Batch Unreal ingest: ' + _runner_path)",
                "_runner = importlib.util.module_from_spec(_spec)",
                "sys.modules[_spec.name] = _runner",
                "_spec.loader.exec_module(_runner)",
                (
                    f"_runner.run_manifest({str(manifest_path)!r}, "
                    f"checkpoint_path={str(checkpoint_path)!r}, "
                    f"report_path={str(batch_report_path)!r})"
                ),
            ]
            report["stage"] = "rpc_ingest"
            run_commands(rpc_lines)
            batch_result = wait_for_json(
                batch_report_path,
                max(args.ue_timeout, rpc_timeout + 60),
                "SK Batch RPC manifest ingest",
            )
            item_result = (batch_result.get("items") or {}).get(queue_id, {})
            report["unreal_result"] = item_result
            report["wind"] = item_result.get("wind")
            report["materials"] = item_result.get("materials")
            report["checkout"] = item_result.get("checkout")
            if item_result.get("status") != "imported_ok":
                report["status"] = item_result.get("status", "failed")
                report["failure_kind"] = item_result.get("status", "data_error")
                raise RuntimeError(
                    item_result.get("message") or "Unreal manifest ingest failed"
                )
            report["stage"] = "completed"
            report["status"] = "ok"
            report["failure_kind"] = None
    except Exception as exc:
        # Record the error before any classification helper runs: a crash in
        # this handler must never leave the report unwritten ("job report was
        # not created" hides the real failure from the GUI).
        error_text = str(exc)
        report["error"] = error_text
        report["traceback"] = traceback.format_exc()
        try:
            classify_failure(report, error_text)
        except Exception:
            report.setdefault("failure_kind", "data_error")
    write_report(args.report, report)
    if report["status"] not in {"ok", "exported_pending_unreal"}:
        sys.exit(1)


def classify_failure(report, error_text):
    editor_running = unreal_editor_running()
    report["unreal_editor_running_after_failure"] = editor_running
    lowered = error_text.lower()
    if report.get("status") == "manual_required":
        failure_kind = "manual_required"
    elif (
        report.get("stage") != "unreal_preflight"
        and report.get("unreal_rpc_started")
        and editor_running is False
    ):
        failure_kind = "unreal_crash"
    elif any(
        token in lowered
        for token in (
            'the call "import_asset" timed out',
            "rpc timeout",
            "rpc_time_out",
            "no result file (timed out",
        )
    ):
        failure_kind = "rpc_timeout"
    elif any(
        token in lowered
        for token in (
            "could not find an open unreal editor instance",
            "rpc not reachable",
            "unreal editor is not running",
            "connectionreseterror",
            "winerror 10054",
        )
    ):
        failure_kind = "unreal_unavailable"
    else:
        failure_kind = "data_error"
    report["failure_kind"] = failure_kind
    if failure_kind == "unreal_crash":
        report["error"] = f"Unreal Editor crashed or exited during push: {error_text}"


main()
