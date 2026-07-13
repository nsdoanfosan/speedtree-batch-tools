"""Headless Blender job: SPM -> SpeedTree CLI export -> BWR import/repair -> .blend.

Run:
  blender.exe -b --python bwr_headless_job.py -- --spm X.spm --blend X.blend
      --wind TREE|BUSH|GRASS|NONE --report result.json

Relies on user prefs (NO --factory-startup) so the junction-installed
speedtree_bone_weight_repair add-on registers itself. Re-running on an existing
.blend is a clean idempotent update (the operator wipes its previous build).
"""
import argparse
import json
import os
import sys
import traceback
from pathlib import Path

import bpy


def parse_args():
    argv = sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else []
    parser = argparse.ArgumentParser()
    parser.add_argument("--spm", required=True)
    parser.add_argument("--blend", required=True)
    parser.add_argument("--wind", default="GRASS", choices=["TREE", "BUSH", "GRASS", "NONE"])
    parser.add_argument("--report", required=True)
    return parser.parse_args(argv)


def write_report(path, data):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def main():
    args = parse_args()
    report = {"spm": args.spm, "blend": args.blend, "wind": args.wind, "status": "failed"}
    try:
        import addon_utils

        loaded, enabled = addon_utils.check("speedtree_bone_weight_repair")
        if not enabled:
            bpy.ops.preferences.addon_enable(module="speedtree_bone_weight_repair")

        # Reject authored-but-disabled Branch skeletons before creating an
        # empty .blend. BranchMesh-only assets remain valid and use the rigid
        # one-bone fallback inside the add-on.
        from speedtree_bone_weight_repair.core import require_spm_sk_ready

        require_spm_sk_ready(os.path.abspath(args.spm))

        blend_path = os.path.abspath(args.blend)
        if os.path.exists(blend_path):
            bpy.ops.wm.open_mainfile(filepath=blend_path)
        else:
            bpy.ops.wm.read_homefile(use_empty=True)

        settings = bpy.context.scene.speedtree_bwr_settings
        settings.spm_path = os.path.abspath(args.spm)
        if args.wind == "NONE":
            # Dead vegetation: keep the JSON contract but zero all sway.
            settings.wind_preset = "CUSTOM"
            settings.dynamic_wind_flexibility = 0.0
            settings.dynamic_wind_gust_attenuation = 0.0
            settings.dynamic_wind_ground_cover = False
        else:
            settings.wind_preset = args.wind
        settings.write_unreal_json = True
        settings.write_dynamic_wind_json = True

        # Save FIRST so default_paths anchors out_dir/JSON next to the SPM.
        Path(blend_path).parent.mkdir(parents=True, exist_ok=True)
        bpy.ops.wm.save_as_mainfile(filepath=blend_path)

        result = bpy.ops.speedtree_bwr.export_from_speedtree()
        if "FINISHED" not in result:
            raise RuntimeError(f"export_from_speedtree returned {result}")

        bpy.ops.wm.save_as_mainfile(filepath=blend_path)

        stem = Path(args.spm).stem
        blend_dir = str(Path(blend_path).parent)
        json_dir = os.path.join(blend_dir, "JSON")
        report.update(
            {
                "status": "ok",
                "name_stem": stem,
                "megaplant_json": os.path.join(json_dir, f"{stem}_megaplant_tree_groups.json"),
                "dynamic_wind_json": os.path.join(
                    json_dir, f"{stem}_dynamic_wind_import_from_megaplant_groups.json"
                ),
                "pipeline_report": os.path.join(
                    blend_dir, "reports", f"{stem}_speedtree_repair_pipeline_report_codex.json"
                ),
            }
        )
        pipeline_path = Path(report["pipeline_report"])
        if pipeline_path.is_file():
            pipeline_data = json.loads(pipeline_path.read_text(encoding="utf-8"))
            texture_normalization = pipeline_data.get("texture_normalization") or {}
            report["texture_normalization"] = texture_normalization
            for missing in texture_normalization.get("missing", []):
                roles = ", ".join(missing.get("missing_roles", [])) or "대응 세트"
                report.setdefault("warnings", []).append(
                    f"{missing.get('material', '?')}: "
                    f"{missing.get('expected_texture_base', 'T_?')} ({roles}) 누락"
                )
        for key in ("megaplant_json", "dynamic_wind_json", "pipeline_report"):
            if not os.path.exists(report[key]):
                report.setdefault("warnings", []).append(f"expected output missing: {report[key]}")
    except Exception as exc:
        report["error"] = str(exc)
        report["traceback"] = traceback.format_exc()
    write_report(args.report, report)
    if report["status"] != "ok":
        sys.exit(1)


main()
