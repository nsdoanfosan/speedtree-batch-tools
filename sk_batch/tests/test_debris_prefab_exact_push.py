from __future__ import annotations

import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock


SK_BATCH = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SK_BATCH))
import exact_push


class ExactPrefabCompletionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.original = self.root / "original_push.json"
        self.original.write_text(json.dumps({"status": "ok"}), encoding="utf-8")
        self.outputs = {
            "report": self.original, "queue_id": "source",
            "unreal_project": self.root / "Project.uproject",
            "material_contract": self.root / "material.json", "transport": "headless",
            "debris_terrain_prefab": True,
            "debris_prefab_context": {
                "spm": str(self.root / "Ground_cover.spm"), "blender": "blender.exe",
                "send2ue_dir": "send2ue", "unreal_editor_cmd": "Editor-Cmd.exe",
            },
        }
        self.runner = mock.Mock(return_value={"status": "ok", "changelist": 42})
        pipeline = types.ModuleType("sk_batch.debris_prefab_pipeline")
        pipeline.run_pipeline = self.runner
        patcher = mock.patch.dict(sys.modules, {"sk_batch.debris_prefab_pipeline": pipeline})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_cli_flag_is_explicit_and_defaults_off(self):
        self.assertFalse(exact_push.parse_args(["--spm", "A.spm"]).debris_terrain_prefab)
        self.assertTrue(exact_push.parse_args(["--spm", "A.spm", "--debris-terrain-prefab"]).debris_terrain_prefab)

    def test_off_and_unsuccessful_original_never_invoke_pipeline(self):
        self.outputs["debris_terrain_prefab"] = False
        exact_push._complete_optional_debris_prefab(self.outputs, {"status": "ok"})
        self.outputs["debris_terrain_prefab"] = True
        exact_push._complete_optional_debris_prefab(self.outputs, {"status": "failed"})
        self.runner.assert_not_called()

    def test_deferred_context_preserves_boolean_and_completes_after_import(self):
        prepared = json.loads(json.dumps(exact_push._serialized_outputs(self.outputs)))
        self.assertIs(prepared["debris_terrain_prefab"], True)
        result = exact_push.merge_unreal_result(prepared, {"items": {"source": {"status": "imported_ok"}}})
        self.runner.assert_called_once()
        self.assertEqual(result["debris_prefab_pipeline"]["status"], "ok")
        self.assertEqual(self.runner.call_args.kwargs["source_push_report"], self.original)
        original = json.loads(self.original.read_text(encoding="utf-8"))
        self.assertEqual(original["status"], "ok")
        self.assertNotIn("debris_prefab_pipeline", original)

    def test_failure_is_not_reported_as_success_and_original_bytes_remain(self):
        before = self.original.read_bytes()
        self.runner.side_effect = RuntimeError("geometry mismatch")
        with self.assertRaisesRegex(exact_push.ExactPushError, "original Push succeeded"):
            exact_push._complete_optional_debris_prefab(self.outputs, {"status": "ok"})
        self.assertEqual(self.original.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
