import hashlib
import json
import runpy
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock


TOOL_DIR = Path(__file__).resolve().parents[1]
WORKER = TOOL_DIR / "jobs" / "index_leaf_blend_sources.py"


class BlendSourceIndexWorkerTests(unittest.TestCase):
    def test_addon_side_effects_are_removed_before_source_files_open(self):
        events = []

        addon = types.ModuleType("atlas_leaf_mesh_builder")
        addon.__path__ = []

        def initialize_scene_items():
            raise AssertionError("the UI timer must never run in the worker")

        addon.initialize_scene_items = initialize_scene_items

        source_index = types.ModuleType(
            "atlas_leaf_mesh_builder.source_index"
        )

        def current_blend_source_index(**kwargs):
            events.append(("index", kwargs["expected_blend_path"]))
            return {"status": "indexed"}

        source_index.current_blend_source_index = current_blend_source_index

        gateway = types.ModuleType("blender_addon_gateway")

        class Runtime:
            receipt = {"status": "ready", "addons": []}

            def operation(self, addon_id, operation_name):
                self.assert_contract(addon_id, operation_name)
                return current_blend_source_index

            def assert_contract(self, addon_id, operation_name):
                if addon_id != "atlas_leaf_mesh_builder":
                    raise AssertionError(addon_id)
                if operation_name != "current_blend_source_index":
                    raise AssertionError(operation_name)

            def detach_timer(self, addon_id, callback_name):
                self.assert_contract(
                    addon_id, "current_blend_source_index"
                )
                if callback_name != "initialize_scene_items":
                    raise AssertionError(callback_name)
                events.append(("unregister", initialize_scene_items))

            def disable(self, addon_id):
                if addon_id != "atlas_leaf_mesh_builder":
                    raise AssertionError(addon_id)
                events.append(("disable", None))

        def prepare_runtime(*_args, **_kwargs):
            events.append(("enable", None))
            return Runtime()

        gateway.prepare_runtime = prepare_runtime

        timers = types.SimpleNamespace(
            is_registered=lambda callback: callback is initialize_scene_items,
            unregister=lambda callback: events.append(("unregister", callback)),
        )
        bpy = types.ModuleType("bpy")
        bpy.app = types.SimpleNamespace(timers=timers)
        bpy.ops = types.SimpleNamespace(
            wm=types.SimpleNamespace(
                open_mainfile=lambda filepath, load_ui: events.append(
                    ("open", Path(filepath))
                )
            )
        )

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            blend = root / "leaf source.blend"
            blend.write_bytes(b"blend source")
            request = root / "request.json"
            request.write_text(
                json.dumps({
                    "schema_version": 1,
                    "requests": [{
                        "blend": str(blend),
                        "blend_sha256": hashlib.sha256(
                            blend.read_bytes()
                        ).hexdigest(),
                    }],
                }),
                encoding="utf-8",
            )
            report = root / "report.json"
            modules = {
                "blender_addon_gateway": gateway,
                "bpy": bpy,
                "atlas_leaf_mesh_builder": addon,
                "atlas_leaf_mesh_builder.source_index": source_index,
            }
            argv = [
                "blender",
                "--",
                "--request",
                str(request),
                "--out",
                str(report),
            ]
            with mock.patch.dict(sys.modules, modules), mock.patch.object(
                sys, "argv", argv
            ):
                runpy.run_path(str(WORKER), run_name="__main__")

            payload = json.loads(report.read_text(encoding="utf-8"))

        self.assertEqual(payload["status"], "ok")
        self.assertEqual(
            [event[0] for event in events],
            ["enable", "unregister", "disable", "open", "index"],
        )


if __name__ == "__main__":
    unittest.main()
