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
import json
import math
import os
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
    return parser.parse_args(argv)


def write_report(path, data):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


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
        return result.returncode == 0 and "UnrealEditor.exe" in result.stdout
    except (OSError, subprocess.SubprocessError):
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
    blend_path = bpy.data.filepath
    report = {"blend": blend_path, "status": "failed"}
    try:
        import addon_utils

        # --factory-startup keeps unrelated UI/GPU add-ons out of background
        # Blender, so required handoff registrations must be explicit.
        enable_required_addon(addon_utils, "speedtree_bone_weight_repair")
        enable_required_addon(addon_utils, "ue_unique_export_names_addon")
        enable_required_addon(addon_utils, "send2ue")

        from speedtree_bone_weight_repair.core import normalize_speedtree_material_textures

        texture_normalization = normalize_speedtree_material_textures(bpy.context.scene.objects)
        report["texture_normalization"] = texture_normalization
        if texture_normalization.get("material_count"):
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

        complexity = export_complexity()
        report["complexity"] = complexity
        exceeded = []
        if (
            args.max_push_polygons > 0
            and complexity["polygon_count"] > args.max_push_polygons
        ):
            exceeded.append(
                f"폴리곤 {complexity['polygon_count']:,} > {args.max_push_polygons:,}"
            )
        if args.max_push_bones > 0 and complexity["bone_count"] > args.max_push_bones:
            exceeded.append(f"본 {complexity['bone_count']:,} > {args.max_push_bones:,}")
        if exceeded:
            report["status"] = "manual_required"
            report["manual_required"] = True
            raise RuntimeError(
                "Unreal Push 수동 처리 필요: "
                + ", ".join(exceeded)
                + ". Unreal RPC를 막지 않고 이 항목을 건너뜁니다."
            )

        from send2ue.core import utilities
        from send2ue.dependencies.unreal import run_commands, set_rpc_env

        if not utilities.is_unreal_connected():
            raise RuntimeError("Unreal editor is not running / RPC not reachable. Open the project first.")

        # Successful calls return immediately; this only controls how long an
        # unattended run tolerates a legitimately slow synchronous import.
        # The same polygon/bone limits used by the manual guard provide one
        # continuous load ratio, so light assets get the minimum and assets
        # near the automatic-processing ceiling get the maximum.
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

        scene_props = bpy.context.scene.send2ue
        if hasattr(scene_props, "skip_animation_export"):
            scene_props.skip_animation_export = True

        unit_name = find_export_unit_name() or Path(blend_path).stem
        folder = scene_props.unreal_mesh_folder_path
        report["unit_name"] = unit_name
        report["unreal_folder"] = folder

        blend_dir = Path(blend_path).parent
        mesh_path = folder.rstrip("/") + "/" + unit_name

        # The post-import material/wind pipeline saves the skeletal mesh without
        # an interactive checkout prompt. Check out existing generated assets
        # up front so a Perforce read-only package cannot fail after import.
        checkout_result_path = (
            blend_dir / "reports" / f"__skbatch_ue_checkout_{unit_name}.json"
        )
        checkout_result_path.parent.mkdir(parents=True, exist_ok=True)
        if checkout_result_path.exists():
            checkout_result_path.unlink()
        checkout_lines = [
            "import unreal, json, traceback",
            "res = {'ok': False}",
            "try:",
            "    subsystem = unreal.get_editor_subsystem(unreal.EditorAssetSubsystem)",
            f"    candidates = {[mesh_path, mesh_path + '_Skeleton', mesh_path + '_PhysicsAsset']!r}",
            "    existing = [path for path in candidates if unreal.EditorAssetLibrary.does_asset_exist(path)]",
            "    failed = [path for path in existing if not subsystem.checkout_asset(path)]",
            "    if failed:",
            "        raise RuntimeError('source-control checkout failed: ' + ', '.join(failed))",
            "    res = {'ok': True, 'existing': existing, 'checked_out': existing}",
            "except Exception as e:",
            "    res = {'ok': False, 'error': str(e), 'trace': traceback.format_exc()}",
            f"open({str(checkout_result_path)!r}, 'w', encoding='utf-8').write(json.dumps(res))",
        ]
        run_commands(checkout_lines)
        checkout_result = wait_for_json(
            checkout_result_path, args.ue_timeout, "Unreal source-control checkout"
        )
        report["checkout"] = checkout_result
        if not checkout_result.get("ok"):
            raise RuntimeError(
                f"Unreal source-control checkout failed: {checkout_result.get('error')}"
            )

        unreal_log_path, unreal_log_offset = unreal_log_checkpoint()
        result = bpy.ops.wm.send2ue("EXEC_DEFAULT")
        if "FINISHED" not in result:
            raise RuntimeError(f"send2ue returned {result}")
        report["send2ue"] = "FINISHED"

        material_result_path = (
            blend_dir / "reports" / f"__skbatch_ue_material_{unit_name}.json"
        )
        material_result_path.parent.mkdir(parents=True, exist_ok=True)
        if material_result_path.exists():
            material_result_path.unlink()
        material_lines = [
            "import unreal, json, traceback",
            "res = {'ok': False}",
            "try:",
            f"    mesh_path = {mesh_path!r}",
            "    mesh = unreal.EditorAssetLibrary.load_asset(mesh_path)",
            "    if not mesh:",
            "        raise RuntimeError('mesh not found: ' + mesh_path)",
            "    slots = list(mesh.get_editor_property('materials') or [])",
            "    details = []",
            "    missing = []",
            "    for index, slot in enumerate(slots):",
            "        slot_name = str(slot.get_editor_property('material_slot_name'))",
            "        material = slot.get_editor_property('material_interface')",
            "        material_path = material.get_path_name() if material else ''",
            "        details.append({'index': index, 'slot': slot_name, 'material': material_path})",
            "        if not material:",
            "            missing.append('%s[%d]' % (slot_name, index))",
            "    if not slots:",
            "        raise RuntimeError('skeletal mesh has no material slots')",
            "    if missing:",
            "        raise RuntimeError('unassigned material slots: ' + ', '.join(missing))",
            "    res = {'ok': True, 'mesh': mesh_path, 'slots': details}",
            "except Exception as e:",
            "    res = {'ok': False, 'error': str(e), 'trace': traceback.format_exc()}",
            f"open({str(material_result_path)!r}, 'w', encoding='utf-8').write(json.dumps(res))",
        ]
        run_commands(material_lines)
        material_result = wait_for_json(
            material_result_path, args.ue_timeout, "Unreal material validation"
        )
        report["materials"] = material_result
        if not material_result.get("ok"):
            raise RuntimeError(
                f"Unreal material validation failed: {material_result.get('error')}"
            )
        material_names = [
            item.get("material", "")
            for item in texture_normalization.get("materials", [])
        ]
        compile_failures = target_material_compile_failures(
            unreal_log_path, unreal_log_offset, material_names
        )
        report["material_compile_failures"] = compile_failures
        if compile_failures:
            raise RuntimeError(
                "Unreal material compile failed after import: "
                + compile_failures[0].splitlines()[0]
            )

        wind_json = blend_dir / "JSON" / f"{unit_name}_dynamic_wind_import_from_megaplant_groups.json"
        if args.skip_wind:
            report["wind"] = "skipped (flag)"
        elif not wind_json.exists():
            report["wind"] = f"skipped (no JSON at {wind_json})"
        else:
            ue_result_path = blend_dir / "reports" / f"__skbatch_ue_wind_{unit_name}.json"
            ue_result_path.parent.mkdir(parents=True, exist_ok=True)
            if ue_result_path.exists():
                ue_result_path.unlink()
            # run_commands wraps these lines in a try/except on the Unreal side;
            # we write our own result file for ground truth either way.
            lines = [
                "import unreal, json, traceback",
                "res = {'ok': False}",
                "try:",
                f"    mesh_path = {mesh_path!r}",
                "    mesh = unreal.EditorAssetLibrary.load_asset(mesh_path)",
                "    if not mesh:",
                f"        data = unreal.EditorAssetLibrary.list_assets({folder!r}, recursive=False)",
                "        raise RuntimeError('mesh not found: %s (folder has: %s)' % (mesh_path, list(data)[:20]))",
                "    if not hasattr(unreal, 'CodexDynamicWindImportLibrary'):",
                "        raise RuntimeError('CodexDynamicWindImportLibrary missing (Codex plugin not loaded)')",
                "    r = unreal.CodexDynamicWindImportLibrary.import_dynamic_wind_json_to_skeletal_mesh(",
                f"        mesh, {str(wind_json)!r})",
                "    unreal.EditorAssetLibrary.save_asset(mesh_path)",
                f"    unreal.EditorAssetLibrary.save_directory({folder!r}, only_if_is_dirty=True)",
                "    res = {'ok': True, 'mesh': mesh_path, 'import_result': str(r)}",
                "except Exception as e:",
                "    res = {'ok': False, 'error': str(e), 'trace': traceback.format_exc()}",
                f"open({str(ue_result_path)!r}, 'w', encoding='utf-8').write(json.dumps(res))",
            ]
            run_commands(lines)
            ue_result = wait_for_json(
                ue_result_path, args.ue_timeout, "Unreal wind import"
            )
            report["wind"] = ue_result
            if not ue_result.get("ok"):
                raise RuntimeError(f"Unreal wind import failed: {ue_result.get('error')}")
        report["status"] = "ok"
    except Exception as exc:
        editor_running = unreal_editor_running()
        report["unreal_editor_running_after_failure"] = editor_running
        if editor_running is False:
            report["error"] = f"Unreal Editor crashed or exited during push: {exc}"
        else:
            report["error"] = str(exc)
        report["traceback"] = traceback.format_exc()
    write_report(args.report, report)
    if report["status"] != "ok":
        sys.exit(1)


main()
