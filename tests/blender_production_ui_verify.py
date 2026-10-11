"""Read-only disk UI QA after production workers finish and release handles.

Run a normal non-factory Blender with --background --python this_file --
one_or_more_worker_reports.json. It loads each exact source with load_ui=True,
checks the producer's full UI receipt, and never saves a .blend or preferences.
"""

import json
import sys
from pathlib import Path

import bpy

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from blender_production_file import install_session_safety, verify_saved_ui


def verify_report(report_path):
    report = json.loads(Path(report_path).read_text(encoding="utf-8"))
    receipt = report.get("production_blend_save", report)
    if not isinstance(receipt, dict) or "ui_sha256" not in receipt:
        raise RuntimeError("Worker report has no production UI save receipt: " + report_path)
    target = Path(receipt["path"]).resolve()
    if not target.is_file():
        raise RuntimeError("Exact saved source is missing: " + str(target))
    result = bpy.ops.wm.open_mainfile(filepath=str(target), load_ui=True)
    if result != {"FINISHED"}:
        raise RuntimeError("Saved production source did not load with its UI")
    return verify_saved_ui(bpy, receipt)


install_session_safety(bpy)
arguments = sys.argv[sys.argv.index("--") + 1:]
if not arguments:
    raise RuntimeError("At least one producer receipt is required")
for report_path in arguments:
    print("PRODUCTION_UI_DISK_QA=" + json.dumps(verify_report(report_path), sort_keys=True))
