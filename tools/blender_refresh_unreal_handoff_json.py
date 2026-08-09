"""Refresh Unreal material sidecars from one existing Blender source file.

Run with Blender:
  blender -b source.blend --factory-startup --python this_file.py -- \
      --report result.json
"""

from __future__ import annotations

import argparse
import json
import sys
import traceback
from pathlib import Path

import bpy


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from blender_addon_gateway import prepare_runtime


def _parse_args():
    argv = sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else []
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", required=True)
    return parser.parse_args(argv)


def _write_report(path, payload):
    report = Path(path).expanduser().resolve()
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def main():
    args = _parse_args()
    result = {
        "blend": str(Path(bpy.data.filepath).resolve()) if bpy.data.filepath else "",
        "status": "failed",
        "errors": [],
        "json_paths": [],
    }
    exit_code = 1
    try:
        addon_runtime = prepare_runtime(
            "tools.blender_refresh_unreal_handoff_json",
            {
                "ue_unique_export_names_addon": (
                    "unreal_handoff_json_v1",
                ),
            },
        )
        refresh_handoff_json = addon_runtime.operation(
            "ue_unique_export_names_addon", "refresh_handoff_json"
        )
        refreshed = refresh_handoff_json(bpy.context)
        result["blender_addon_runtime"] = addon_runtime.receipt
        result["errors"] = list(refreshed.get("errors") or [])
        result["json_paths"] = list(refreshed.get("json_paths") or [])
        result["export_dir"] = str(refreshed.get("export_dir") or "")
        if result["errors"]:
            result["status"] = "blocked"
        elif not result["json_paths"]:
            result["errors"].append("JSON refresh produced no files")
        else:
            result["status"] = "ok"
            exit_code = 0
    except Exception as exc:
        result["errors"].append(str(exc))
        result["traceback"] = traceback.format_exc()
    _write_report(args.report, result)
    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
