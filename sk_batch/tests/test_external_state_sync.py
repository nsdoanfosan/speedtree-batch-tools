import json
import queue
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

SK_BATCH = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SK_BATCH))

import sk_common
from push_state_sync import capture_push_source, prepare_push_state, publish_push_result
from sk_batch.tests.test_blend_live_status import (
    FakeCheckedRows, FakeTree, FakeVar, load_gui_module,
)


class ExternalStateSyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.spm = self.root / "SK_tree.spm"
        self.spm.write_bytes(b"spm")
        self.iid = str(self.spm)
        patch = mock.patch.object(sk_common, "STATE_PATH", self.root / "state.json")
        patch.start()
        self.addCleanup(patch.stop)

    def test_stale_window_save_preserves_new_push_and_other_local_fields(self):
        sk_common.save_state({self.iid: {"push_status": "old", "wind_override": "auto"}})
        window = sk_common.load_state()
        retained_row = window[self.iid]
        command = sk_common.load_state()
        command[self.iid]["push_status"] = "완료"
        command[self.iid]["push_import_fingerprint"] = "new receipt"
        sk_common.save_state(command)
        window[self.iid]["wind_override"] = "strong"
        sk_common.save_state(window)
        persisted = sk_common.load_state()[self.iid]
        self.assertEqual(persisted["push_status"], "완료")
        self.assertEqual(persisted["push_import_fingerprint"], "new receipt")
        self.assertEqual(persisted["wind_override"], "strong")
        self.assertEqual(window[self.iid], persisted)
        self.assertIs(window[self.iid], retained_row)

    def test_refresh_rebases_unsaved_edits_and_does_not_resurrect_deleted_error(self):
        sk_common.save_state({self.iid: {"push_status_error": "old error", "blend_status": "old"}})
        window = sk_common.load_state()
        command = sk_common.load_state()
        command[self.iid].pop("push_status_error")
        command[self.iid]["push_status"] = "완료"
        sk_common.save_state(command)
        window[self.iid]["blend_status"] = "new local audit"
        self.assertEqual(sk_common.refresh_state(window), {self.iid})
        self.assertEqual(window[self.iid]["blend_status"], "new local audit")
        self.assertNotIn("push_status_error", window[self.iid])
        sk_common.save_state(window)
        self.assertNotIn("push_status_error", sk_common.load_state()[self.iid])

    def fixture_push(self):
        self.spm.with_suffix(".blend").write_bytes(b"blend")
        outputs = {
            "queue_id": self.iid,
            "push_source_fingerprint_cache": capture_push_source(self.spm),
            **{key: self.root / f"{key}.json" for key in (
                "manifest", "report", "checkpoint", "item_import_report", "batch_report",
            )},
        }
        outputs["manifest"].write_text(json.dumps({"items": [{
            "queue_id": self.iid, "fingerprint": "export-1",
            "source_fingerprint": outputs["push_source_fingerprint_cache"]["fingerprint"],
        }]}), encoding="utf-8")
        return outputs

    def test_command_export_then_import_reaches_current_gui_status_without_restart(self):
        gui = load_gui_module()
        outputs = self.fixture_push()
        app = gui.App.__new__(gui.App)
        app.state = sk_common.load_state()
        pending = {"status": "exported_pending_unreal", "manifest_fingerprint": "export-1"}
        self.assertTrue(publish_push_result(outputs, pending))
        sk_common.refresh_state(app.state)
        self.assertEqual(app._current_push_status_text(self.iid, self.spm), "export 완료 · Unreal 대기")
        imported = {**pending, "status": "ok", "unreal_result": {"status": "imported_ok"}}
        self.assertTrue(publish_push_result(outputs, imported))
        sk_common.refresh_state(app.state)
        self.assertEqual(app._current_push_status_text(self.iid, self.spm), "완료 (현재 최신)")
        outputs["manifest"].unlink()
        self.assertIn("manifest 없음", app._current_push_status_text(self.iid, self.spm))

    def test_source_changed_after_export_cannot_be_marked_current(self):
        outputs = self.fixture_push()
        self.spm.with_suffix(".blend").write_bytes(b"changed blend")
        with self.assertRaisesRegex(ValueError, "source changed"):
            publish_push_result(outputs, {
                "status": "ok", "manifest_fingerprint": "export-1",
                "unreal_result": {"status": "imported_ok"},
            })
        self.assertEqual(sk_common.load_state(), {})

    def test_mismatched_or_incomplete_receipt_is_not_promoted(self):
        outputs = self.fixture_push()
        for report in (
            {"status": "ok", "manifest_fingerprint": "wrong", "unreal_result": {"status": "imported_ok"}},
            {"status": "ok", "manifest_fingerprint": "export-1"},
            {"status": "ok", "manifest_fingerprint": "export-1", "unreal_result": {"status": "imported_ok", "fingerprint": "another export"}},
        ):
            with self.subTest(report=report), self.assertRaises(ValueError):
                publish_push_result(outputs, report)
        self.assertEqual(sk_common.load_state(), {})

    def test_periodic_poll_reads_external_push_even_with_unchanged_blend_signature(self):
        gui = load_gui_module()
        outputs = self.fixture_push()
        app = gui.App.__new__(gui.App)
        app.state = sk_common.load_state()
        app.state_lock = threading.RLock()
        app.items = {self.iid: {"spm": self.spm}}
        app._scan_generation = 1
        app.ui_queue = queue.Queue()
        app.log = mock.Mock()
        signature = app._live_status_signature(self.spm)
        publish_push_result(outputs, {
            "status": "ok", "manifest_fingerprint": "export-1",
            "unreal_result": {"status": "imported_ok"},
        })
        app._blend_status_text = mock.Mock(side_effect=AssertionError("unchanged Blend should not be re-audited"))
        app._poll_live_file_status_worker(1, [(self.iid, self.spm, (), signature)])
        events = list(app.ui_queue.queue)
        self.assertIn(("cell", (self.iid, "push_status", "완료 (현재 최신)")), events)
        self.assertEqual(app.state[self.iid]["push_status_kind"], "imported_ok")
        self.assertEqual(signature, app._live_status_signature(self.spm))

    def test_explicit_scan_reads_command_result_before_rendering_and_saving(self):
        gui = load_gui_module()
        outputs = self.fixture_push()
        app = gui.App.__new__(gui.App)
        app.state = sk_common.load_state()
        app.state_lock = threading.RLock()
        app.items = {}
        app.tree = FakeTree()
        app.checked_rows = FakeCheckedRows()
        app.root_var = FakeVar(str(self.root))
        app._scan_generation = 1
        app.cfg = {}
        app.log = mock.Mock()
        publish_push_result(outputs, {
            "status": "ok", "manifest_fingerprint": "export-1",
            "unreal_result": {"status": "imported_ok"},
        })
        app.scan(prepared={"spms": [self.spm], "cluster_sources": []}, generation=1)
        self.assertEqual(app.tree.rows[self.iid]["values"][3], "완료 (현재 최신)")
        self.assertEqual(sk_common.load_state()[self.iid]["push_status_kind"], "imported_ok")

    def test_deferred_receipt_retains_proof_for_another_process(self):
        outputs = self.fixture_push()
        command = ["blender", "--source-fingerprint", "old"]
        prepare_push_state(command, outputs)
        self.assertEqual(command.count("--source-fingerprint"), 1)
        publish_push_result(outputs, {
            "status": "exported_pending_unreal", "manifest_fingerprint": "export-1",
        })
        import exact_push
        resumed = json.loads(json.dumps(exact_push._serialized_outputs(outputs)))
        self.assertIsInstance(resumed["push_source_fingerprint_cache"], dict)
        resumed.pop("push_source_fingerprint_cache")
        report = json.loads(outputs["report"].read_text(encoding="utf-8"))
        report.update(status="ok", unreal_result={"status": "imported_ok"})
        self.assertTrue(publish_push_result(resumed, report))
        self.assertEqual(sk_common.load_state()[self.iid]["push_status_kind"], "imported_ok")


if __name__ == "__main__":
    unittest.main()
