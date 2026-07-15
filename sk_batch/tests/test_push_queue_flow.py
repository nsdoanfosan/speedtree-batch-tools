import queue
import json
import sys
import tempfile
import threading
import unittest
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path
from unittest import mock


SK_BATCH_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SK_BATCH_DIR))


def load_gui_module():
    path = SK_BATCH_DIR / "sk_batch_gui.pyw"
    loader = SourceFileLoader("sk_batch_gui_queue_test", str(path))
    spec = spec_from_loader(loader.name, loader)
    module = module_from_spec(spec)
    loader.exec_module(module)
    return module


class PushQueueFlowTests(unittest.TestCase):
    def make_app(self, gui):
        app = gui.App.__new__(gui.App)
        app.stop_flag = threading.Event()
        app.ui_queue = queue.Queue()
        app.state = {}
        app.state_lock = threading.RLock()
        app.cfg = {}
        app.log = mock.Mock()
        return app

    @staticmethod
    def targets(*names):
        return [{"spm": Path(name), "checked": True} for name in names]

    def test_data_and_manual_failures_do_not_stop_later_push_items(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        targets = self.targets("SK_bad.spm", "SK_manual.spm", "SK_after.spm")
        attempted = []

        def fake_push(_iid, spm):
            attempted.append(spm.name)
            if spm.name == "SK_bad.spm":
                raise gui.BatchItemError("missing material", kind="data_error")
            if spm.name == "SK_manual.spm":
                raise gui.BatchItemError(
                    "Unreal Push 수동 처리 필요: 본 1,800 > 1,500",
                    kind="manual_required",
                )

        with mock.patch.object(app, "_push_preflight", return_value=(targets, None)), mock.patch.object(
            app, "_job_push", side_effect=fake_push
        ), mock.patch.object(gui, "save_state"):
            result = app._run_batch("push", targets, emit_done=False)

        self.assertTrue(result)
        self.assertEqual(
            attempted, ["SK_bad.spm", "SK_manual.spm", "SK_after.spm"]
        )
        self.assertEqual(app.state["SK_bad.spm"]["push_status_kind"], "data_error")
        self.assertEqual(
            app.state["SK_manual.spm"]["push_status_kind"], "manual_required"
        )

    def test_rpc_timeout_stops_safely_and_marks_remaining_items_not_run(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        targets = self.targets("SK_timeout.spm", "SK_after_1.spm", "SK_after_2.spm")
        attempted = []

        def fake_push(_iid, spm):
            attempted.append(spm.name)
            raise gui.BatchItemError(
                "Unreal RPC 시간 초과 — 큐 응답 정지", kind="rpc_timeout"
            )

        with mock.patch.object(app, "_push_preflight", return_value=(targets, None)), mock.patch.object(
            app, "_job_push", side_effect=fake_push
        ), mock.patch.object(gui, "save_state"):
            result = app._run_batch("push", targets, emit_done=False)

        self.assertFalse(result)
        self.assertEqual(attempted, ["SK_timeout.spm"])
        self.assertEqual(
            app.state["SK_timeout.spm"]["push_status_kind"], "rpc_timeout"
        )
        self.assertEqual(
            app.state["SK_after_1.spm"]["push_status_kind"], "not_run_unreal"
        )
        self.assertIn("미실행", app.state["SK_after_2.spm"]["push_status"])

    def test_preflight_unreal_off_is_a_failure_not_false_completion(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        targets = self.targets("SK_first.spm", "SK_second.spm")

        with mock.patch.object(app, "_unreal_running", return_value=False), mock.patch.object(
            gui, "save_state"
        ):
            ready, reason = app._push_preflight(targets)

        self.assertEqual(ready, [])
        self.assertIn("Unreal Editor", reason)
        self.assertEqual(
            app.state["SK_first.spm"]["push_status_kind"], "unreal_unavailable"
        )

    def test_headless_preflight_allows_unreal_to_be_off(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        app.active_push_transport = "headless"
        targets = self.targets("SK_first.spm", "SK_second.spm")

        with mock.patch.object(app, "_unreal_running", return_value=False), mock.patch.object(
            app, "_handoff_ready", return_value=(True, "")
        ), mock.patch.object(gui, "save_state"):
            ready, reason = app._push_preflight(targets)

        self.assertEqual(ready, targets)
        self.assertIsNone(reason)

    def test_headless_preflight_blocks_when_editor_is_open(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        app.active_push_transport = "headless"
        targets = self.targets("SK_first.spm")

        with mock.patch.object(app, "_unreal_running", return_value=True), mock.patch.object(
            gui, "save_state"
        ):
            ready, reason = app._push_preflight(targets)

        self.assertEqual(ready, [])
        self.assertIn("Unreal Editor", reason)
        self.assertEqual(
            app.state["SK_first.spm"]["push_status_kind"], "unreal_unavailable"
        )

    def test_push_batch_routes_headless_without_using_rpc_job(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        app.active_push_transport = "headless"
        targets = self.targets("SK_first.spm")

        with mock.patch.object(
            app, "_push_preflight", return_value=(targets, None)
        ), mock.patch.object(
            app, "_run_headless_push_batch", return_value=True
        ) as headless, mock.patch.object(app, "_job_push") as rpc_job:
            result = app._run_batch("push", targets, emit_done=False)

        self.assertTrue(result)
        headless.assert_called_once_with(targets, emit_done=False)
        rpc_job.assert_not_called()

    def test_watchdog_records_crash_in_checkpoint_before_restart(self):
        gui = load_gui_module()
        with tempfile.TemporaryDirectory() as temp_dir:
            checkpoint = Path(temp_dir) / "checkpoint.json"
            checkpoint.write_text(
                json.dumps(
                    {
                        "current_item": "item-a",
                        "items": {
                            "item-a": {
                                "status": "importing",
                                "crash_count": 0,
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            result = gui.App._record_headless_process_exit(checkpoint, 2)

        self.assertIsNone(result["current_item"])
        self.assertEqual(result["items"]["item-a"]["status"], "unreal_crash")
        self.assertEqual(result["items"]["item-a"]["crash_count"], 1)

    def test_headless_exports_all_items_then_uses_one_commandlet_session(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        app.force_rerun = False
        app._phase_abort_reason = ""
        app.cfg = {
            "unreal_editor_cmd": "UnrealEditor-Cmd.exe",
            "unreal_project": "MyProject2.uproject",
            "headless_item_crash_retries": 2,
            "headless_batch_max_restarts": 0,
            "headless_job_timeout": 100,
        }
        targets = self.targets("SK_first.spm", "SK_second.spm")
        exported = [
            {
                "queue_id": "SK_first.spm",
                "fingerprint": "first-v1",
                "report_path": "first-report.json",
            },
            {
                "queue_id": "SK_second.spm",
                "fingerprint": "second-v1",
                "report_path": "second-report.json",
            },
        ]

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)

            def fake_commandlet(_cmd, log_name, _timeout, **kwargs):
                manifest_path = Path(kwargs["env"]["SK_BATCH_MANIFEST_PATH"])
                checkpoint_path = Path(kwargs["env"]["SK_BATCH_CHECKPOINT_PATH"])
                report_path = Path(kwargs["env"]["SK_BATCH_REPORT_PATH"])
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                states = {
                    item["queue_id"]: {
                        "status": "imported_ok",
                        "fingerprint": item["fingerprint"],
                    }
                    for item in manifest["items"]
                }
                checkpoint_path.write_text(
                    json.dumps(
                        {
                            "complete": True,
                            "current_item": None,
                            "items": states,
                        }
                    ),
                    encoding="utf-8",
                )
                report_path.write_text(
                    json.dumps({"status": "complete", "items": states}),
                    encoding="utf-8",
                )
                kwargs["progress_callback"](1.0, "done")
                return 0, temp_root / log_name

            with mock.patch.object(gui, "LOG_DIR", temp_root), mock.patch.object(
                app, "_export_manifest_item", side_effect=exported
            ) as export_item, mock.patch.object(
                app, "_run_limited", side_effect=fake_commandlet
            ) as commandlet, mock.patch.object(gui, "save_state"):
                result = app._run_headless_push_batch(targets, emit_done=False)

        self.assertTrue(result)
        self.assertEqual(export_item.call_count, 2)
        commandlet.assert_called_once()
        self.assertEqual(
            app.state["SK_first.spm"]["push_status_kind"], "imported_ok"
        )
        self.assertEqual(
            app.state["SK_second.spm"]["push_status_kind"], "imported_ok"
        )


if __name__ == "__main__":
    unittest.main()
