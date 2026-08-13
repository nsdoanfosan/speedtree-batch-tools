from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SK_BATCH = Path(__file__).resolve().parents[1]
if str(SK_BATCH) not in sys.path:
    sys.path.insert(0, str(SK_BATCH))

import exact_push  # noqa: E402
from exact_push import ExactPushError, build_exact_push_command  # noqa: E402


class ExactPushCommandTests(unittest.TestCase):
    def test_builds_production_headless_push_from_latest_exact_evidence(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            spm = root / "SK_tree_sample_01.spm"
            blend = spm.with_suffix(".blend")
            blender = root / "blender.exe"
            logs = root / "logs"
            project = root / "MyProject.uproject"
            config = root / "Config" / "DefaultEngine.ini"
            logs.mkdir()
            config.parent.mkdir()
            project.write_text("{}", encoding="utf-8")
            config.write_text(
                "\n".join([
                    "[/Script/PythonScriptPlugin.PythonScriptPluginSettings]",
                    "bRemoteExecution=True",
                    "RemoteExecutionMulticastBindAddress=192.168.0.4",
                    "RemoteExecutionMulticastGroupEndpoint=239.0.0.1:6766",
                    "RemoteExecutionMulticastTtl=1",
                ]),
                encoding="utf-8",
            )
            for path in (spm, blend, blender):
                path.write_bytes(b"current")
            material = logs / "SK_tree_sample_01_push_material_contract_current.json"
            material.write_text("{}", encoding="utf-8")

            d_export = root / "D_drive_send2ue_fbx"
            with mock.patch.object(
                exact_push,
                "send2ue_export_cache_root",
                return_value=d_export,
            ):
                command, outputs = build_exact_push_command(
                    spm,
                    blender=blender,
                    log_dir=logs,
                    run_id="test",
                    material_contract=material,
                    unreal_project=project,
                )

            self.assertEqual(command[0], str(blender.resolve()))
            self.assertNotIn("--require-green-signal", command)
            self.assertIn("--dependency-orchestrated", command)
            self.assertEqual(
                command[command.index("--transport") + 1],
                "headless_export",
            )
            self.assertNotIn("--rpc-multicast-bind-address", command)
            self.assertNotIn("--rpc-multicast-group-endpoint", command)
            self.assertNotIn("--rpc-multicast-ttl", command)
            self.assertIn("--item-import-report", command)
            self.assertIn("--export-root", command)
            self.assertIn("--unreal-ingest", command)
            self.assertEqual(
                outputs["material_contract"],
                material.resolve(),
            )
            self.assertNotIn("--repair-evidence", command)
            self.assertNotIn("repair_evidence", outputs)
            self.assertTrue(str(outputs["report"]).endswith("_exact_push_test.json"))
            self.assertEqual(
                outputs["export_root"],
                d_export / "exact" / spm.stem / "test",
            )
            self.assertNotIn("backup", str(outputs["export_root"]).casefold())

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
                    material_contract=root / "missing.json",
                )


if __name__ == "__main__":
    unittest.main()
