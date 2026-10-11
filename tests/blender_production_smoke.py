"""Non-factory scratch .blend save/reload QA; never open production files."""

import json
import sys
from pathlib import Path

import bpy

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from blender_production_file import (
    install_session_safety, load_production_file, verify_saved_ui,
)

arguments = sys.argv[sys.argv.index("--") + 1:]
scratch = Path(arguments[0]).resolve()
assert scratch.is_dir() and scratch.name.startswith("speedtree_ui_smoke_")
target = scratch / "scratch.blend"
assert not target.exists() and not bpy.data.filepath
install_session_safety(bpy)
state = load_production_file(bpy, target)
for screen in bpy.data.screens:
    for area in screen.areas:
        if area.type == "VIEW_3D":
            area.spaces.active.clip_end *= 0.5
original_version = bpy.context.preferences.filepaths.save_version
state.save(check_existing=False)
receipt = state.receipt()
assert bpy.context.preferences.filepaths.save_version == original_version
assert not target.with_suffix(".blend1").exists()
result = bpy.ops.wm.open_mainfile(filepath=str(target), load_ui=True)
assert result == {"FINISHED"}
qa = verify_saved_ui(bpy, receipt)
print("PRODUCTION_UI_SMOKE=" + json.dumps({**qa, "temporary_version_restored": True,
      "factory_startup": False, "user_preferences_saved": False}, sort_keys=True))
