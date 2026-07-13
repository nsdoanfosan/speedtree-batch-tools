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


def enable_required_addon(addon_utils, module):
    """Enable only the add-ons that the isolated Push pipeline requires."""
    _loaded, enabled = addon_utils.check(module)
    if not enabled:
        bpy.ops.preferences.addon_enable(module=module)
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

        from send2ue.core import utilities
        from send2ue.dependencies.unreal import run_commands

        if not utilities.is_unreal_connected():
            raise RuntimeError("Unreal editor is not running / RPC not reachable. Open the project first.")

        scene_props = bpy.context.scene.send2ue
        if hasattr(scene_props, "skip_animation_export"):
            scene_props.skip_animation_export = True

        unit_name = find_export_unit_name() or Path(blend_path).stem
        folder = scene_props.unreal_mesh_folder_path
        report["unit_name"] = unit_name
        report["unreal_folder"] = folder

        unreal_log_path, unreal_log_offset = unreal_log_checkpoint()
        result = bpy.ops.wm.send2ue("EXEC_DEFAULT")
        if "FINISHED" not in result:
            raise RuntimeError(f"send2ue returned {result}")
        report["send2ue"] = "FINISHED"

        blend_dir = Path(blend_path).parent
        mesh_path = folder.rstrip("/") + "/" + unit_name
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
        deadline = time.time() + args.ue_timeout
        while time.time() < deadline and not material_result_path.exists():
            time.sleep(1.0)
        if not material_result_path.exists():
            raise RuntimeError("Unreal material validation: no result file (timed out)")
        material_result = json.loads(material_result_path.read_text(encoding="utf-8"))
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
            deadline = time.time() + args.ue_timeout
            while time.time() < deadline and not ue_result_path.exists():
                time.sleep(1.0)
            if ue_result_path.exists():
                ue_result = json.loads(ue_result_path.read_text(encoding="utf-8"))
                report["wind"] = ue_result
                if not ue_result.get("ok"):
                    raise RuntimeError(f"Unreal wind import failed: {ue_result.get('error')}")
            else:
                raise RuntimeError("Unreal wind import: no result file (timed out)")
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
