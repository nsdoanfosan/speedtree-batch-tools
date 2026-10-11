"""Verify the actual disk-export/RPC boundary without importing Blender."""
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

SCRIPT = Path(__file__).resolve().parents[1] / "jobs/send2ue_push_job.py"
from sk_batch.prepared_rpc_resume import finish_batch_handoff, verify_deferred_export_receipt


def helpers():
    return {"verify_deferred_export_receipt": verify_deferred_export_receipt,
            "finish_batch_handoff": finish_batch_handoff}


class BatchDeferredHandoffTests(unittest.TestCase):
    def test_actual_export_receipt_digest_and_request_are_required(self):
        verify = helpers()["verify_deferred_export_receipt"]
        with tempfile.TemporaryDirectory() as temporary:
            file = Path(temporary) / "part.fbx"
            file.write_bytes(b"actual-native-fbx")
            blend = str(Path(temporary) / "source.blend")
            metadata = {"phase_id": "p_one", "request_id": "exact-request"}
            receipt = {"metadata": metadata, "source_blend": blend,
                "stage": "exported_pending_unreal", "phase_completed": False,
                "native_export_jobs": 1, "exported_files": [{"path": str(file), "bytes": file.stat().st_size,
                    "sha256": hashlib.sha256(file.read_bytes()).hexdigest()}]}
            self.assertEqual(verify(receipt, metadata, blend), receipt)
            with self.assertRaisesRegex(RuntimeError, "exact"):
                verify(receipt, {"phase_id": "p_two"}, blend)
            file.write_bytes(b"changed-fbx-bytes")
            with self.assertRaises(RuntimeError):
                verify(receipt, metadata, blend)

    def test_export_or_partial_rpc_cannot_complete_native_phase(self):
        finish = helpers()["finish_batch_handoff"]
        completed = []
        bridge = SimpleNamespace(can_execute_handoff=lambda metadata: True,
            complete_handoff=lambda *args: completed.append(args) or True)
        metadata = {"request_id": "exact-request", "target": "main01.blend"}
        good = {"status": "imported_ok", "durable_saves": {"records": [{"saved": True, "dirty_after_save": False}]},
                "cluster_assembly": {"status": "ok", "runtime": {"status": "passed",
                    "begin": {"success": True, "status": "pending", "probe_token": "token"},
                    "finish": {"success": True, "status": "passed"}}}}
        finish(bridge, metadata, good, final=False)
        self.assertEqual(completed, [])
        for bad in ({"status": "exported_pending_unreal"},
                    {**good, "durable_saves": {"records": [{"saved": True, "dirty_after_save": True}]}},
                    {**good, "cluster_assembly": {"status": "ok", "runtime": {"success": False}}}):
            with self.assertRaisesRegex(RuntimeError, "validation"):
                finish(bridge, metadata, bad, final=True)
        self.assertEqual(completed, [])
        finish(bridge, metadata, good, final=True)
        self.assertEqual(completed, [(metadata, "exact-request", "main01.blend")])

    def test_actual_runtime_finish_receipt_matches_completion_guard(self):
        finish = helpers()["finish_batch_handoff"]
        bridge = SimpleNamespace(can_execute_handoff=lambda _: True, complete_handoff=unittest.mock.Mock(return_value=True))
        metadata = {"request_id": "request", "target": "main"}
        runtime = {"status": "passed", "begin": {"success": True, "status": "pending", "probe_token": "token"},
                   "finish": {"success": True, "status": "passed"}}
        result = {"status": "imported_ok", "durable_saves": {"records": [{"saved": True, "dirty_after_save": False}]},
                  "cluster_assembly": {"status": "ok", "runtime": runtime}}
        finish(bridge, metadata, result, final=True)
        bridge.complete_handoff.assert_called_once()
        runtime["finish"]["success"] = False
        with self.assertRaisesRegex(RuntimeError, "validation"):
            finish(bridge, metadata, result, final=True)


if __name__ == "__main__":
    unittest.main()
