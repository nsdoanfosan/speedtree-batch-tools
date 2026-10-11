"""Preserve production .blend UI and process-local preferences during saves.

Callers verify the exact Perforce revision/shelf before starting mutations.
This module creates no source copies and never persists user preferences.
"""

import hashlib
import json
import sys
from pathlib import Path


def install_session_safety(bpy_module, argv=None):
    """Disallow factory production saves and persistent add-on preferences."""
    arguments = sys.argv if argv is None else argv
    if "--factory-startup" in arguments:
        raise RuntimeError("Production Blender jobs cannot use --factory-startup")
    wm = bpy_module.ops.wm
    # Blender creates operator submodule proxies on access. Keep this proxy so
    # the process-local guard also protects nested producer operations.
    bpy_module.ops.wm = wm

    def forbid_preferences(*args, **kwargs):
        raise RuntimeError("Production jobs cannot save user preferences")

    wm.save_userpref = forbid_preferences
    import addon_utils
    original_enable = addon_utils.enable
    if getattr(original_enable, "_production_session_only", False) is not True:
        # addon_utils.enable makes default_set keyword-only in Blender 5.2.
        def enable(module_name, *, default_set=False, **kwargs):
            kwargs["default_set"] = False
            return original_enable(module_name, **kwargs)
        enable._production_session_only = True
        addon_utils.enable = enable


def _primitive(value):
    if isinstance(value, (str, bool, int, float)):
        return value
    if type(value).__name__ in (
        "Vector", "Euler", "Quaternion", "Color", "bpy_prop_array",
    ):
        values = list(value)
        if len(values) <= 64 and all(isinstance(x, (str, bool, int, float)) for x in values):
            return values
    return None


def _properties(value):
    values = {}
    for prop in value.bl_rna.properties:
        if prop.identifier == "rna_type" or prop.is_readonly:
            continue
        try:
            current = _primitive(getattr(value, prop.identifier))
            if current is not None:
                values[prop.identifier] = current
        except (AttributeError, TypeError, ValueError, RuntimeError):
            continue
    return values


def capture_ui(bpy_module):
    window = bpy_module.context.window
    result = {"workspace": window.workspace.name if window else None, "screens": {}}
    for screen in bpy_module.data.screens:
        areas = []
        for area in screen.areas:
            space = area.spaces.active
            nested = {}
            for key in ("region_3d", "shading", "overlay", "uv_editor"):
                target = getattr(space, key, None)
                if target is not None and hasattr(target, "bl_rna"):
                    nested[key] = _properties(target)
            areas.append({
                "type": area.type, "ui_type": area.ui_type,
                "layout": [area.x, area.y, area.width, area.height],
                "space": _properties(space), "nested": nested,
            })
        result["screens"][screen.name] = areas
    return result


def _apply_properties(target, values):
    for key, value in values.items():
        if _primitive(getattr(target, key)) == value:
            continue
        # Headless region visibility changes can crash Blender. A layout drift
        # fails before disk mutation rather than applying these unsafe setters.
        if key.startswith("show_region_"):
            raise RuntimeError("Unsafe background UI region drift: " + key)
        setattr(target, key, value)
        if _primitive(getattr(target, key)) != value:
            raise RuntimeError("Blender UI property restoration failed: " + key)


def restore_ui(bpy_module, state):
    screens = {screen.name: screen for screen in bpy_module.data.screens}
    if set(screens) != set(state["screens"]):
        raise RuntimeError("Production Blender screen set changed")
    for name, rows in state["screens"].items():
        screen = screens[name]
        if len(screen.areas) != len(rows):
            raise RuntimeError("Production Blender screen split changed: " + name)
        for area, row in zip(screen.areas, rows):
            if [area.x, area.y, area.width, area.height] != row["layout"]:
                raise RuntimeError("Production Blender area layout changed: " + name)
            if area.type != row["type"]:
                area.type = row["type"]
            if area.ui_type != row["ui_type"]:
                area.ui_type = row["ui_type"]
            _apply_properties(area.spaces.active, row["space"])
            for key, values in row["nested"].items():
                _apply_properties(getattr(area.spaces.active, key), values)
    window = bpy_module.context.window
    if state["workspace"] is not None:
        if window is None:
            raise RuntimeError("Production Blender workspace window disappeared")
        if window.workspace.name != state["workspace"]:
            window.workspace = bpy_module.data.workspaces[state["workspace"]]
    if capture_ui(bpy_module) != state:
        raise RuntimeError("Production Blender UI restoration mismatch")


class ProductionBlendState:
    def __init__(self, bpy_module, path):
        if "--factory-startup" in sys.argv:
            raise RuntimeError("Factory Blender cannot save production sources")
        self.bpy = bpy_module
        self.path = Path(path).expanduser().resolve()
        if self.path.is_file() and Path(bpy_module.data.filepath).resolve() != self.path:
            raise RuntimeError("Existing production source UI must be loaded first")
        self.ui = capture_ui(bpy_module)
        self.saved = False
        self.preference_restored = True
        self.ui_sha256 = hashlib.sha256(
            json.dumps(self.ui, sort_keys=True, allow_nan=False).encode("utf-8")
        ).hexdigest()

    def save(self, **kwargs):
        path = Path(kwargs.get("filepath", self.path)).expanduser().resolve()
        if path != self.path:
            raise RuntimeError("Production save escaped its original UI path")
        restore_ui(self.bpy, self.ui)
        preferences = self.bpy.context.preferences.filepaths
        original = preferences.save_version
        try:
            preferences.save_version = 0
            result = self.bpy.ops.wm.save_as_mainfile(**{**kwargs, "filepath": str(path)})
            if "FINISHED" not in result:
                raise RuntimeError("Production Blender save did not finish")
        finally:
            preferences.save_version = original
            self.preference_restored = preferences.save_version == original
        if capture_ui(self.bpy) != self.ui:
            raise RuntimeError("Production Blender save changed its UI")
        self.saved = True
        return result

    def receipt(self):
        return {"path": str(self.path), "ui_sha256": self.ui_sha256,
                "ui": self.ui,
                "ui_preserved": capture_ui(self.bpy) == self.ui,
                "save_version_restored": self.preference_restored,
                "preferences_persisted": False, "saved": self.saved,
                "recovery_policy": "caller_verified_perforce_revision_or_shelf"}


def load_production_file(bpy_module, path):
    path = Path(path).expanduser().resolve()
    if path.is_file() and Path(bpy_module.data.filepath).resolve() != path:
        result = bpy_module.ops.wm.open_mainfile(filepath=str(path), load_ui=True)
        if "FINISHED" not in result or Path(bpy_module.data.filepath).resolve() != path:
            raise RuntimeError("Production source failed to load with its saved UI")
    return ProductionBlendState(bpy_module, path)


def verify_saved_ui(bpy_module, receipt):
    """Verify a fresh, read-only load against a producer's persisted UI receipt.

    Run after the pipeline ends, in a non-factory worker with the exact saved
    file loaded with load_ui=True; do not reload active producer object handles.
    """
    path = Path(receipt["path"]).resolve()
    if not receipt.get("saved") or Path(bpy_module.data.filepath).resolve() != path:
        raise RuntimeError("Saved UI QA requires the exact committed source")
    actual = capture_ui(bpy_module)
    actual_hash = hashlib.sha256(
        json.dumps(actual, sort_keys=True, allow_nan=False).encode("utf-8")
    ).hexdigest()
    if actual != receipt["ui"] or actual_hash != receipt["ui_sha256"]:
        raise RuntimeError("Saved production file UI differs after reload")
    return {"path": str(path), "saved_ui_sha256": actual_hash,
            "saved_ui_preserved_after_reload": True, "read_only_qa": True}
