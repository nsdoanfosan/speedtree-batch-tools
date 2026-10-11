"""Exercise the real export/resume file boundary with a supplied RPC sender."""
import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from sk_batch import prepared_rpc_resume as resume


class PreparedRpcResumeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.metadata = dict(phase_id="p_existing", ticket_id="ticket_existing", provider="Codex",
            session_id="owner", resource="unreal:MyProject2", pipeline="speedtree_batch_send2ue",
            request_id="same-request", target="main01.blend", project="unreal:MyProject2",
            action="send2ue_manifest_export_rpc_ingest")
        self.bridge = SimpleNamespace(
            require_active_phase=mock.Mock(return_value={"workload": "heavy", "provider": "Codex", "session_id": "owner"}),
            require_scopes=mock.Mock(), can_execute_handoff=mock.Mock(return_value=True),
            heartbeat_handoff=mock.Mock(return_value=True), complete_handoff=mock.Mock(return_value=True),
            fail_handoff=mock.Mock(), bind_handoff=mock.Mock(side_effect=AssertionError("must not create a request")))

    def file(self, name, content):
        path = self.root / name
        path.write_bytes(content)
        return path

    def prepared(self, *, final=False, historical=False):
        blend = self.file("source.blend", b"source-ui-geometry")
        if final:
            self.metadata["target"] = str(blend)
        spm = self.file("source.spm", b"source-spm")
        material = self.file("material.json", b"material-input")
        fbx = self.file("native.fbx", b"native-export")
        texture = self.file("actual_texture.tga", b"material-texture-bytes")
        layer_texture = self.file("actual_layer_texture.tga", b"layer-texture-bytes")
        sidecar = self.file("native_materials.json", json.dumps({"materials": [
            {"textures": [{"file": str(texture)}]},
            {"layers": [{"textures": [{"file": str(layer_texture)}]}]}]}).encode("utf-8"))
        runner = self.file("unreal_ingest.py", b"runner-code")
        source_files = [blend, spm, material]
        report_path = self.root / "export_report.json"
        manifest_path = self.root / "manifest.json"
        checkpoint = self.root / "checkpoint.json"
        batch = self.root / "batch.json"
        contract = dict(schema_version=1, source_fingerprint="source-contract", blend=str(blend),
            unit_name="SK_provider_01", unreal_folder="/Game/Tree", mesh_path="/Game/Tree/SK_provider_01",
            assets=[{"asset_data": {"file_path": str(fbx), "_material_pipeline_json_fingerprint": resume._file_record(sidecar)}}],
            exported_files=[resume._file_record(fbx)], handoff_files=[resume._file_record(sidecar)],
            wind_file=None, code_files=[resume._file_record(runner)], cluster_assembly=None,
            workstation_phase_id=self.metadata["phase_id"], workstation_handoff=self.metadata)
        item = {**contract, "queue_id": "one", "send2ue_unreal_py": str(runner), "wind_json": None,
            "checkout_asset_paths": [], "report_path": str(self.root / "item_result.json"),
            "export_report_path": str(report_path)}
        if historical:
            item["diagnostic_history"] = {"authoring_source": {"path": str(self.root / "removed_old_source.spm"), "sha256": "0" * 64}}
        item["fingerprint"] = resume._contract_fingerprint(item)
        if final:
            item["cluster_assembly"] = {"manifest": {"parts": [{"asset_name": "part"}], "full_asset_stem": blend.stem},
                "ingest_plan": {"status": "ready", "asset_contract": {"full_skeletal_mesh": item["mesh_path"],
                    "base_skeletal_mesh": "/Game/Tree/Base", "assembly": "/Game/Tree/Assembly", "parts": {"part": "/Game/Tree/Part"}}}}
            item["fingerprint"] = resume._contract_fingerprint(item)
        self.current_item = item
        manifest = {"schema_version": 1, "items": [item], "checkpoint_path": str(checkpoint), "report_path": str(batch)}
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        report = dict(status="exported_pending_unreal", stage="exported_pending_unreal", transport="rpc",
            blend=str(blend), send2ue="FINISHED", manifest=str(manifest_path),
            manifest_fingerprint=item["fingerprint"], checkpoint=str(checkpoint), batch_report=str(batch),
            workstation_handoff=self.metadata, native_deferred_export_receipt={
                "metadata": self.metadata, "source_blend": str(blend), "stage": "exported_pending_unreal",
                "phase_completed": False, "native_export_jobs": 1,
                "exported_files": [{"path": str(fbx), "bytes": fbx.stat().st_size, "sha256": resume.file_sha256(fbx)}]})
        report["prepared_rpc"] = resume.prepare_rpc_pause(report, manifest_path, item, runner,
            source_paths=source_files, final=final)
        report_path.write_text(json.dumps(report), encoding="utf-8")
        return report_path, resume.file_sha256(report_path), item, [blend, spm, material, fbx, sidecar, runner, texture, layer_texture]

    def sender(self, report_path, item, results):
        calls = []
        report = json.loads(report_path.read_text(encoding="utf-8"))
        def send(lines):
            calls.append(lines)
            result = copy.deepcopy(results[min(len(calls) - 1, len(results) - 1)])
            result["fingerprint"] = item["fingerprint"]
            batch = {"manifest": report["manifest"], "items": {item["queue_id"]: result}}
            Path(report["batch_report"]).write_text(json.dumps(batch), encoding="utf-8")
            Path(report["checkpoint"]).write_text(json.dumps({**batch, "process_lifetime_policy": {"transport": "rpc"}}), encoding="utf-8")
        return send, calls

    def run_resume(self, report, digest, sender, *, final=False):
        return resume.resume_prepared_rpc(report, rpc_sender=sender, handoff_bridge=self.bridge,
            expected_handoff=self.metadata, expected_export_report_sha256=digest, final=final, sleep=lambda _: None)

    def saved(self):
        packages = ["/Game/Tree/SK_provider_01"]
        if self.current_item.get("cluster_assembly"):
            packages += ["/Game/Tree/Base", "/Game/Tree/Assembly", "/Game/Tree/Part"]
        records = []
        for package in packages:
            package_file = self.file(package.rsplit("/", 1)[-1] + ".uasset", b"native-package")
            records.append({"sequence": len(records), "package": package, "asset": package,
                           "owner": "final", "role": "mesh", "saved": True, "dirty_after_save": False,
                           "package_file": str(package_file), "size": package_file.stat().st_size,
                           "mtime_ns": package_file.stat().st_mtime_ns})
        return {"status": "imported_ok", "durable_saves": {"schema_version": 1, "records": records},
                "cluster_assembly": ({"status": "ok", "runtime": {"status": "passed",
                    "begin": {"success": True, "status": "pending", "probe_token": "token"},
                    "finish": {"success": True, "status": "passed"}}}
                    if self.current_item.get("cluster_assembly") else {"status": "skipped"})}

    def test_provider_resumes_exact_manifest_without_new_request_or_completion(self):
        path, digest, item, _ = self.prepared()
        before = path.read_bytes()
        sender, calls = self.sender(path, item, [self.saved()])
        result = self.run_resume(path, digest, sender)
        self.assertEqual(result["status"], "ok")
        self.assertFalse(result["phase_completed"])
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(len(calls), 1)
        self.assertIn("_runner.run_manifest(", calls[0][-1])
        self.bridge.complete_handoff.assert_not_called()
        self.bridge.bind_handoff.assert_not_called()

    def test_main_finishes_actual_two_frame_probe_before_exact_completion(self):
        path, digest, item, _ = self.prepared(final=True)
        pending = {"status": "runtime_pending"}
        passed = self.saved()
        passed["cluster_assembly"] = {"status": "ok", "runtime": {"status": "passed",
            "begin": {"success": True, "status": "pending", "probe_token": "native-token"},
            "finish": {"success": True, "status": "passed"}}}
        sender, calls = self.sender(path, item, [pending, passed])
        result = self.run_resume(path, digest, sender, final=True)
        self.assertEqual(result["runtime_finish_attempts"], 1)
        self.assertIn("_runner.finish_runtime_probe(", calls[1][-1])
        self.bridge.complete_handoff.assert_called_once_with(self.metadata, "same-request", item["blend"])
        self.bridge.bind_handoff.assert_not_called()

    def test_changed_sidecar_source_export_or_code_fails_before_rpc(self):
        path, digest, _, files = self.prepared()
        sender = mock.Mock()
        for file in files:
            with self.subTest(file=file.name):
                before = file.read_bytes()
                file.write_bytes(bytes([before[0] ^ 1]) + before[1:])
                with self.assertRaisesRegex(RuntimeError, "digest changed"):
                    self.run_resume(path, digest, sender)
                file.write_bytes(before)
        sender.assert_not_called()
        self.bridge.complete_handoff.assert_not_called()

    def test_changed_report_manifest_or_binding_cannot_resume(self):
        path, digest, _, _ = self.prepared()
        sender = mock.Mock()
        changed = {**self.metadata, "request_id": "replacement"}
        with self.assertRaisesRegex(RuntimeError, "same native handoff"):
            resume.resume_prepared_rpc(path, rpc_sender=sender, handoff_bridge=self.bridge,
                expected_handoff=changed, expected_export_report_sha256=digest)
        report = json.loads(path.read_text(encoding="utf-8"))
        manifest = Path(report["manifest"])
        manifest.write_text(manifest.read_text(encoding="utf-8") + " ", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "size changed"):
            self.run_resume(path, digest, sender)
        path.write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "report digest changed"):
            self.run_resume(path, digest, sender)
        sender.assert_not_called()
        self.bridge.complete_handoff.assert_not_called()

    def test_provider_cannot_be_promoted_to_final_and_active_binding_is_required(self):
        path, digest, item, _ = self.prepared()
        sender, _ = self.sender(path, item, [self.saved()])
        with self.assertRaisesRegex(RuntimeError, "final handoff intent"):
            self.run_resume(path, digest, sender, final=True)
        self.bridge.can_execute_handoff.return_value = False
        with self.assertRaisesRegex(RuntimeError, "active native request"):
            self.run_resume(path, digest, sender)
        self.bridge.complete_handoff.assert_not_called()

    def test_dirty_final_save_or_incomplete_runtime_cannot_complete(self):
        path, digest, item, _ = self.prepared(final=True)
        bad = self.saved()
        bad["durable_saves"]["records"][0]["dirty_after_save"] = True
        sender, _ = self.sender(path, item, [bad])
        with self.assertRaisesRegex(RuntimeError, "validation"):
            self.run_resume(path, digest, sender, final=True)
        self.bridge.complete_handoff.assert_not_called()
        self.bridge.fail_handoff.assert_called_once()

    def test_missing_final_package_and_saved_source_ledger_change_are_rejected(self):
        path, digest, item, _ = self.prepared(final=True)
        saved = self.saved()
        saved["durable_saves"]["records"][0]["package"] = "/Game/Tree/Unrelated"
        saved["durable_saves"]["records"][0]["asset"] = "/Game/Tree/Unrelated"
        sender, _ = self.sender(path, item, [saved])
        with self.assertRaisesRegex(RuntimeError, "misses exact packages"):
            self.run_resume(path, digest, sender, final=True)
        self.bridge.complete_handoff.assert_not_called()
        ledger = self.file("source_ledger.json", b"immutable-source-ledger")
        expected = resume._file_record(ledger)
        ledger.write_bytes(b"modified-source-ledger")
        not_called = mock.Mock()
        with self.assertRaises(RuntimeError):
            resume.resume_prepared_rpc(path, rpc_sender=not_called, handoff_bridge=self.bridge,
                expected_handoff=self.metadata, expected_export_report_sha256=digest,
                expected_source_fingerprints=[expected], final=True)
        not_called.assert_not_called()

    def test_historical_authoring_identity_does_not_override_current_outputs(self):
        path, digest, item, _ = self.prepared(historical=True)
        sender, _ = self.sender(path, item, [self.saved()])
        self.assertEqual(self.run_resume(path, digest, sender)["status"], "ok")
        self.bridge.complete_handoff.assert_not_called()

    def test_checkpoint_must_match_batch_and_headless_deferred_is_not_runtime_proof(self):
        path, digest, item, _ = self.prepared(final=True)
        sender, _ = self.sender(path, item, [self.saved()])
        report = json.loads(path.read_text(encoding="utf-8"))
        def changed_checkpoint(lines):
            sender(lines)
            checkpoint = Path(report["checkpoint"])
            value = json.loads(checkpoint.read_text(encoding="utf-8"))
            value["items"][item["queue_id"]]["fingerprint"] = "other-import"
            checkpoint.write_text(json.dumps(value), encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "exact manifest"):
            self.run_resume(path, digest, changed_checkpoint, final=True)
        deferred = self.saved()
        deferred["cluster_assembly"]["runtime"] = {"success": True, "status": "headless_deferred", "render_frame_validation_performed": False}
        sender, _ = self.sender(path, item, [deferred])
        with self.assertRaisesRegex(RuntimeError, "actual passed editor runtime"):
            self.run_resume(path, digest, sender, final=True)
        self.bridge.complete_handoff.assert_not_called()

    def test_provider_cannot_seal_a_main_completion_flag(self):
        path, _, item, _ = self.prepared()
        report = json.loads(path.read_text(encoding="utf-8"))
        self.metadata["target"] = item["blend"]
        with self.assertRaisesRegex(RuntimeError, "exact main Assembly"):
            resume.prepare_rpc_pause(report, report["manifest"], item, report["prepared_rpc"]["runner_path"],
                source_paths=[row["path"] for row in report["prepared_rpc"]["source_files"]], final=True)
        self.bridge.complete_handoff.assert_not_called()

    def test_relative_material_texture_is_rejected_before_sealing(self):
        path, _, item, files = self.prepared()
        sidecar = files[4]
        sidecar.write_text(json.dumps({"materials": [{"textures": [{"file": "ambiguous.tga"}]}]}), encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "texture path must be absolute"):
            resume.prepare_rpc_pause(json.loads(path.read_text(encoding="utf-8")),
                json.loads(path.read_text(encoding="utf-8"))["manifest"], item, files[5],
                source_paths=files[:3], final=False)
        self.bridge.complete_handoff.assert_not_called()


if __name__ == "__main__":
    unittest.main()
