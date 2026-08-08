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

        addon_utils = types.ModuleType("addon_utils")

        def enable(*_args, **_kwargs):
            events.append(("enable", None))
            return addon

        def disable(*_args, **_kwargs):
            events.append(("disable", None))

        addon_utils.enable = enable
        addon_utils.disable = disable

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
                "addon_utils": addon_utils,
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
