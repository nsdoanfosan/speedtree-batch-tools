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
import sys
import time
import traceback
from pathlib import Path

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


def find_export_unit_name():
    """The Empty under the Export collection names the FBX -> the UE asset."""
    coll = bpy.data.collections.get("Export")
    if not coll:
        return None
    for obj in coll.objects:
        if obj.type == "EMPTY" and obj.children:
            return obj.name
    return None


def main():
    args = parse_args()
    blend_path = bpy.data.filepath
    report = {"blend": blend_path, "status": "failed"}
    try:
        import addon_utils

        loaded, enabled = addon_utils.check("send2ue")
        if not enabled:
            bpy.ops.preferences.addon_enable(module="send2ue")

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

        result = bpy.ops.wm.send2ue("EXEC_DEFAULT")
        if "FINISHED" not in result:
            raise RuntimeError(f"send2ue returned {result}")
        report["send2ue"] = "FINISHED"

        blend_dir = Path(blend_path).parent
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
            mesh_path = folder.rstrip("/") + "/" + unit_name
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
        report["error"] = str(exc)
        report["traceback"] = traceback.format_exc()
    write_report(args.report, report)
    if report["status"] != "ok":
        sys.exit(1)


main()
