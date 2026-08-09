from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


SK_BATCH = Path(__file__).resolve().parents[1]
if str(SK_BATCH) not in sys.path:
    sys.path.insert(0, str(SK_BATCH))

from exact_push import ExactPushError, build_exact_push_command  # noqa: E402


class ExactPushCommandTests(unittest.TestCase):
    def test_builds_production_rpc_push_from_latest_exact_evidence(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            spm = root / "SK_tree_sample_01.spm"
            blend = spm.with_suffix(".blend")
            blender = root / "blender.exe"
            logs = root / "logs"
            logs.mkdir()
            for path in (spm, blend, blender):
                path.write_bytes(b"current")
            old_material = logs / "SK_tree_sample_01_push_material_contract_old.json"
            new_material = logs / "SK_tree_sample_01_push_material_contract_new.json"
            evidence = logs / "SK_tree_sample_01_repair_push_evidence_new.json"
            old_material.write_text("{}", encoding="utf-8")
            new_material.write_text("{}", encoding="utf-8")
            evidence.write_text("{}", encoding="utf-8")
            new_material.touch()

            command, outputs = build_exact_push_command(
                spm,
                blender=blender,
                log_dir=logs,
                run_id="test",
            )

            self.assertEqual(command[0], str(blender.resolve()))
            self.assertIn("--require-green-signal", command)
            self.assertIn("--dependency-orchestrated", command)
            self.assertEqual(
                outputs["material_contract"],
                new_material.resolve(),
            )
            self.assertEqual(outputs["repair_evidence"], evidence.resolve())
            self.assertTrue(str(outputs["report"]).endswith("_exact_push_test.json"))

    def test_missing_repaired_blend_fails_before_launch(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            spm = root / "SK_tree_sample_01.spm"
            blender = root / "blender.exe"
            spm.write_bytes(b"spm")
            blender.write_bytes(b"exe")
            with self.assertRaisesRegex(ExactPushError, "Blender source"):
                build_exact_push_command(
                    spm,
                    blender=blender,
                    log_dir=root,
                    run_id="test",
                )


if __name__ == "__main__":
    unittest.main()
