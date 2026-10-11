import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest import mock

from blender_production_file import (
    ProductionBlendState, capture_ui, install_session_safety,
    load_production_file,
    verify_saved_ui,
)


def rna(**values):
    return NS(**values, bl_rna=NS(properties=[
        NS(identifier=key, is_readonly=False) for key in values
    ]))


def fake_blender(path):
    space = rna(clip_end=1000., show_region_toolbar=True)
    space.shading = rna(type="SOLID")
    area = NS(type="VIEW_3D", ui_type="VIEW_3D", x=0, y=0, width=600,
              height=400, spaces=NS(active=space))
    workspace = NS(name="Modeling")
    preferences = NS(save_version=3)
    saved = []
    data = NS(filepath=str(path), screens=[NS(name="Modeling", areas=[area])],
              workspaces={"Modeling": workspace})
    def save(**kwargs):
        saved.append({"kwargs": kwargs, "version": preferences.save_version,
                      "ui": capture_ui(blender)})
        return {"FINISHED"}
    def open_file(**kwargs):
        data.filepath = kwargs["filepath"]
        return {"FINISHED"}
    blender = NS(data=data, context=NS(window=NS(workspace=workspace),
                 preferences=NS(filepaths=preferences)),
                 ops=NS(wm=NS(save_as_mainfile=save, open_mainfile=mock.Mock(side_effect=open_file),
                              save_userpref=mock.Mock())))
    return blender, area, saved


class ProductionSaveTests(unittest.TestCase):
    def test_restores_original_editor_settings_before_exact_save(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "source.blend"
            path.write_bytes(b"baseline")
            bpy, area, saved = fake_blender(path)
            state = ProductionBlendState(bpy, path)
            original = state.ui
            area.spaces.active.clip_end = 500.
            area.spaces.active.shading.type = "MATERIAL"
            self.assertEqual(state.save(check_existing=False), {"FINISHED"})
            self.assertEqual(saved[0]["ui"], original)
            self.assertEqual(saved[0]["version"], 0)
            self.assertEqual(bpy.context.preferences.filepaths.save_version, 3)
            self.assertTrue(state.receipt()["ui_preserved"])
            self.assertFalse(state.receipt()["preferences_persisted"])
            self.assertTrue(verify_saved_ui(bpy, state.receipt())["saved_ui_preserved_after_reload"])
            area.spaces.active.clip_end = 10.
            with self.assertRaisesRegex(RuntimeError, "after reload"):
                verify_saved_ui(bpy, state.receipt())

    def test_restores_preference_when_actual_save_raises(self):
        bpy, _, _ = fake_blender("new.blend")
        state = ProductionBlendState(bpy, "new.blend")
        bpy.ops.wm.save_as_mainfile = mock.Mock(side_effect=RuntimeError("disk write"))
        with self.assertRaisesRegex(RuntimeError, "disk write"):
            state.save()
        self.assertEqual(bpy.context.preferences.filepaths.save_version, 3)
        self.assertFalse(state.saved)

    def test_layout_or_unsafe_region_drift_fails_before_disk_write(self):
        for drift in ("layout", "region"):
            bpy, area, saved = fake_blender("new.blend")
            state = ProductionBlendState(bpy, "new.blend")
            if drift == "layout":
                area.width += 1
            else:
                area.spaces.active.show_region_toolbar = False
            with self.assertRaises(RuntimeError):
                state.save()
            self.assertEqual(saved, [])
            self.assertEqual(bpy.context.preferences.filepaths.save_version, 3)

    def test_existing_file_must_load_its_ui_and_same_path_is_required(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "source.blend"
            path.write_bytes(b"baseline")
            bpy, _, saved = fake_blender("other.blend")
            with self.assertRaisesRegex(RuntimeError, "must be loaded"):
                ProductionBlendState(bpy, path)
            state = load_production_file(bpy, path)
            bpy.ops.wm.open_mainfile.assert_called_once_with(filepath=str(path.resolve()), load_ui=True)
            with self.assertRaisesRegex(RuntimeError, "escaped"):
                state.save(filepath=path.with_name("other.blend"))
            self.assertEqual(saved, [])

    def test_factory_and_persistent_preferences_are_blocked(self):
        bpy, _, _ = fake_blender("new.blend")
        addon_utils = NS(enable=mock.Mock(return_value=True))
        with mock.patch.dict(sys.modules, {"addon_utils": addon_utils}):
            with self.assertRaisesRegex(RuntimeError, "factory-startup"):
                install_session_safety(bpy, ["blender", "--factory-startup"])
            original_enable = addon_utils.enable
            install_session_safety(bpy, ["blender", "--background"])
            with self.assertRaisesRegex(RuntimeError, "user preferences"):
                bpy.ops.wm.save_userpref()
            addon_utils.enable("required_addon", default_set=True)
            original_enable.assert_called_once_with("required_addon", default_set=False)
            with self.assertRaises(TypeError):
                addon_utils.enable("required_addon", True)


if __name__ == "__main__":
    unittest.main()
