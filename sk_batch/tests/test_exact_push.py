from __future__ import annotations

import hashlib
import json
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
    def test_planned_headless_process_yield_does_not_spend_crash_budget(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            project = root / "MyProject.uproject"
            editor_cmd = root / "UnrealEditor-Cmd.exe"
            manifest = root / "manifest.json"
            checkpoint = root / "checkpoint.json"
            report = root / "report.json"
            project.write_text("{}", encoding="utf-8")
            editor_cmd.write_bytes(b"exe")
            manifest.write_text(
                json.dumps({"schema_version": 1, "items": [{"queue_id": "a"}]}),
                encoding="utf-8",
            )
            launches = []

            def fake_run(*_args, **kwargs):
                launches.append(kwargs["env"])
                if len(launches) == 1:
                    checkpoint.write_text(
                        json.dumps({"complete": False, "process_yield": {"processed": 1}}),
                        encoding="utf-8",
                    )
                else:
                    checkpoint.write_text(
                        json.dumps({"complete": True}),
                        encoding="utf-8",
                    )
                    report.write_text(
                        json.dumps({"status": "complete"}),
                        encoding="utf-8",
                    )
                return mock.Mock(returncode=0)

            with mock.patch.object(exact_push, "owned_run", side_effect=fake_run):
                result = exact_push.run_headless_manifest(
                    manifest,
                    checkpoint,
                    report,
                    unreal_project=project,
                    unreal_editor_cmd=editor_cmd,
                    max_restarts=0,
                )

            self.assertEqual(result["status"], "complete")
            self.assertEqual(len(launches), 2)
            self.assertEqual(launches[0]["SK_BATCH_MAX_ITEMS_PER_PROCESS"], "6")
            self.assertEqual(launches[0]["SK_BATCH_REQUIRE_NULL_RHI"], "1")

    def test_headless_process_limit_cannot_disable_safety_recycle(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            project = root / "MyProject.uproject"
            editor_cmd = root / "UnrealEditor-Cmd.exe"
            manifest = root / "manifest.json"
            project.write_text("{}", encoding="utf-8")
            editor_cmd.write_bytes(b"exe")
            manifest.write_text("{}", encoding="utf-8")

            with mock.patch.dict(
                exact_push.os.environ,
                {"SK_BATCH_MAX_ITEMS_PER_PROCESS": "0"},
            ):
                with self.assertRaisesRegex(ExactPushError, "between 1 and 6"):
                    exact_push.run_headless_manifest(
                        manifest,
                        root / "checkpoint.json",
                        root / "report.json",
                        unreal_project=project,
                        unreal_editor_cmd=editor_cmd,
                    )

    def test_promotes_assembly_live_material_contract_into_push_command(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            stale = root / "stale.json"
            live = root / "live.json"
            stale.write_text("{}", encoding="utf-8")
            live.write_text('{"status":"ok"}', encoding="utf-8")
            payload = live.read_bytes()
            command = [
                "blender.exe",
                "--material-contract",
                str(stale),
            ]
            outputs = {"material_contract": stale}

            promoted = exact_push._promote_live_material_contract(
                command,
                outputs,
                {
                    "live_material_contract": {
                        "canonical_path": str(live),
                        "size": len(payload),
                        "sha256": hashlib.sha256(payload).hexdigest(),
                    }
                },
            )

            self.assertEqual(promoted, live.resolve())
            self.assertEqual(
                command[command.index("--material-contract") + 1],
                str(live.resolve()),
            )
            self.assertEqual(outputs["material_contract"], live.resolve())

    def test_rejects_changed_assembly_live_material_contract(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            live = root / "live.json"
            live.write_text('{"status":"ok"}', encoding="utf-8")

            with self.assertRaisesRegex(ExactPushError, "fingerprint changed"):
                exact_push._promote_live_material_contract(
                    ["blender.exe", "--material-contract", "stale.json"],
                    {"material_contract": Path("stale.json")},
                    {
                        "live_material_contract": {
                            "canonical_path": str(live),
                            "size": live.stat().st_size,
                            "sha256": "0" * 64,
                        }
                    },
                )

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
            self.assertTrue(str(outputs["report"]).endswith("_exact_push_test.json"))
            self.assertEqual(
                outputs["export_root"],
                d_export / "exact" / spm.stem / "test",
            )
            self.assertNotIn("backup", str(outputs["export_root"]).casefold())

            generated_config = (
                root / "Saved" / "Config" / "WindowsEditor" / "Engine.ini"
            )
            generated_config.parent.mkdir(parents=True)
            generated_config.write_text(
                "\n".join([
                    "[/Script/PythonScriptPlugin.PythonScriptPluginSettings]",
                    "bRemoteExecution=True",
                    "RemoteExecutionMulticastBindAddress=127.0.0.1",
                ]),
                encoding="utf-8",
            )
            with mock.patch.object(
                exact_push,
                "send2ue_export_cache_root",
                return_value=d_export,
            ):
                rpc_command, rpc_outputs = build_exact_push_command(
                    spm,
                    blender=blender,
                    log_dir=logs,
                    run_id="rpc_test",
                    material_contract=material,
                    unreal_project=project,
                    transport="rpc",
                )

            self.assertEqual(
                rpc_command[rpc_command.index("--transport") + 1],
                "rpc",
            )
            self.assertEqual(
                rpc_command[
                    rpc_command.index("--rpc-multicast-bind-address") + 1
                ],
                "127.0.0.1",
            )
            self.assertEqual(
                rpc_command[
                    rpc_command.index("--rpc-multicast-group-endpoint") + 1
                ],
                "239.0.0.1:6766",
            )
            self.assertEqual(
                rpc_command[rpc_command.index("--rpc-multicast-ttl") + 1],
                "1",
            )
            self.assertIn("--send2ue-unreal-py", rpc_command)
            self.assertEqual(rpc_outputs["transport"], "rpc")
            self.assertEqual(
                rpc_outputs["export_root"],
                d_export / "rpc" / spm.stem / "rpc_test",
            )

    def test_missing_assembled_blend_fails_before_launch(self):
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
