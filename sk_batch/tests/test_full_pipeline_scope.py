import queue
import threading
import unittest
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path
from unittest import mock


SK_BATCH_DIR = Path(__file__).resolve().parents[1]


def load_gui_module():
    path = SK_BATCH_DIR / "sk_batch_gui.pyw"
    loader = SourceFileLoader("sk_batch_gui_full_scope_test", str(path))
    spec = spec_from_loader(loader.name, loader)
    module = module_from_spec(spec)
    loader.exec_module(module)
    return module


class FakeVar:
    def __init__(self, value=None):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


class FullPipelineScopeTests(unittest.TestCase):
    @staticmethod
    def make_start_app(gui):
        app = gui.App.__new__(gui.App)
        checked = {"spm": Path("SK_checked.spm"), "checked": True}
        unchecked = {"spm": Path("SK_unchecked.spm"), "checked": False}
        app.items = {"checked": checked, "unchecked": unchecked}
        app._close_cell_editor = mock.Mock()
        app._collect_cfg = mock.Mock(return_value={
            "night_headless": True,
            "push_transport": "rpc",
        })
        app.force_var = FakeVar(False)
        app.stop_flag = threading.Event()
        app.batch_progress = mock.Mock()
        app.batch_progress_var = FakeVar()
        app.progress_var = FakeVar()
        app.btn_check = mock.Mock()
        app.btn_spm = mock.Mock()
        app.btn_blender = mock.Mock()
        app.btn_push = mock.Mock()
        app.btn_all = mock.Mock()
        app.btn_pick_root = mock.Mock()
        app.btn_scan = mock.Mock()
        app.root_entry = mock.Mock()
        app.btn_select_all = mock.Mock()
        app.btn_clear_all = mock.Mock()
        app.btn_recent_24h = mock.Mock()
        app.btn_stop = mock.Mock()
        app.log = mock.Mock()
        return app, checked, unchecked

    def test_full_pipeline_ignores_checkmarks_and_queues_the_whole_list(self):
        gui = load_gui_module()
        app, checked, unchecked = self.make_start_app(gui)
        worker = mock.Mock()

        with mock.patch.object(
            gui, "calibration_settings_signature", return_value="current"
        ), mock.patch.object(
            gui, "legacy_calibration_settings_signature", return_value="legacy"
        ), mock.patch.object(gui, "save_config"), mock.patch.object(
            gui.threading, "Thread", return_value=worker
        ) as thread, mock.patch.object(gui.messagebox, "showinfo") as showinfo:
            app.start_full_pipeline()

        showinfo.assert_not_called()
        queued_job = thread.call_args.kwargs["args"][0]
        self.assertEqual(
            [item["spm"] for item in queued_job["targets"]],
            [checked["spm"], unchecked["spm"]],
        )
        self.assertIsNot(queued_job["targets"][0], checked)
        self.assertEqual(queued_job["mode"], "pipeline")
        self.assertEqual(queued_job["terminal_phase"], "push")
        self.assertFalse(queued_job["selected_scope"])
        self.assertEqual(queued_job["push_transport"], "headless")
        worker.start.assert_called_once_with()
        self.assertIn("2개", app.log.call_args.args[0])

    def test_numbered_buttons_route_to_their_required_phase_chain(self):
        gui = load_gui_module()
        for phase, expected_mode, chained in (
            ("spm", "phase", False),
            ("blender", "pipeline", True),
            ("push", "pipeline", True),
        ):
            with self.subTest(phase=phase):
                app, checked, _unchecked = self.make_start_app(gui)
                worker = mock.Mock()
                with mock.patch.object(
                    gui, "calibration_settings_signature", return_value="current"
                ), mock.patch.object(
                    gui,
                    "legacy_calibration_settings_signature",
                    return_value="legacy",
                ), mock.patch.object(gui, "save_config"), mock.patch.object(
                    gui.threading, "Thread", return_value=worker
                ) as thread:
                    app.start_batch(phase)

                call = thread.call_args
                self.assertIs(
                    call.kwargs["target"].__func__,
                    gui.App._run_queued_batch_job,
                )
                queued_job = call.kwargs["args"][0]
                self.assertEqual(queued_job["mode"], expected_mode)
                self.assertEqual(queued_job["phase"], phase)
                self.assertEqual(
                    queued_job["terminal_phase"], phase
                )
                self.assertEqual(
                    queued_job["selected_scope"], chained
                )
                self.assertEqual(
                    [item["spm"] for item in queued_job["targets"]],
                    [checked["spm"]],
                )
                worker.start.assert_called_once_with()

    def test_jobs_queue_fifo_with_click_time_snapshots_and_continue_after_error(self):
        gui = load_gui_module()
        app, _checked, _unchecked = self.make_start_app(gui)
        paths = [
            Path("SK_cluster_a.spm"),
            Path("SK_cluster_b.spm"),
            Path("SK_cluster_c.spm"),
        ]
        app.items = {
            str(path): {
                "spm": path,
                "checked": index == 0,
                "wind_override": "auto",
            }
            for index, path in enumerate(paths)
        }
        cfg_values = [
            {"push_transport": "rpc", "night_headless": False, "tag": "A"},
            {"push_transport": "rpc", "night_headless": False, "tag": "B"},
            {"push_transport": "headless", "night_headless": False, "tag": "C"},
        ]
        app._collect_cfg = mock.Mock(side_effect=cfg_values)
        workers = [mock.Mock(), mock.Mock(), mock.Mock()]

        with mock.patch.object(
            gui, "calibration_settings_signature", return_value="current"
        ), mock.patch.object(
            gui, "legacy_calibration_settings_signature", return_value="legacy"
        ), mock.patch.object(gui, "save_config"), mock.patch.object(
            gui.threading, "Thread", side_effect=workers
        ) as thread:
            app.start_batch("spm")
            first = app.active_batch_job

            app.items[str(paths[0])]["checked"] = False
            app.items[str(paths[1])]["checked"] = True
            app.start_batch("blender")

            app.items[str(paths[1])]["checked"] = False
            app.items[str(paths[2])]["checked"] = True
            app.start_batch("push")

            self.assertEqual(thread.call_count, 1)
            self.assertEqual(
                [item["spm"] for item in first["targets"]], [paths[0]]
            )
            self.assertEqual(first["cfg"]["tag"], "A")
            self.assertEqual(
                [
                    [item["spm"] for item in job["targets"]]
                    for job in app.pending_batch_jobs
                ],
                [[paths[1]], [paths[2]]],
            )
            self.assertEqual(
                [job["cfg"]["tag"] for job in app.pending_batch_jobs],
                ["B", "C"],
            )
            app.btn_spm.configure.assert_any_call(state="normal")
            app.btn_select_all.configure.assert_any_call(state="normal")
            app.btn_scan.configure.assert_any_call(state="disabled")
            app.root_entry.configure.assert_any_call(state="disabled")

            app._finish_batch_job({"id": first["id"], "error": "A failed"})
            second = app.active_batch_job
            self.assertEqual(thread.call_count, 2)
            self.assertEqual(second["cfg"]["tag"], "B")

            app._finish_batch_job({"id": second["id"], "error": None})
            third = app.active_batch_job
            self.assertEqual(thread.call_count, 3)
            self.assertEqual(third["cfg"]["tag"], "C")

            app._finish_batch_job({"id": third["id"], "error": None})

        self.assertIsNone(app.active_batch_job)
        self.assertFalse(app.pending_batch_jobs)
        self.assertEqual(len(app.batch_job_failures), 1)
        app.btn_scan.configure.assert_called_with(state="normal")
        app.root_entry.configure.assert_called_with(state="normal")
        app.btn_stop.configure.assert_called_with(state="disabled")

    def test_batch_request_snapshot_recursively_freezes_nested_item_data(self):
        gui = load_gui_module()
        app, _checked, _unchecked = self.make_start_app(gui)
        source = {
            "spm": Path("SK_nested.spm"),
            "checked": True,
            "audit": {
                "issues": [{"code": "before"}],
                "paths": [Path("before.json")],
            },
        }
        app.items = {"nested": source}

        inventory, targets = app._snapshot_batch_request(["nested"])
        source["audit"]["issues"][0]["code"] = "after"
        source["audit"]["paths"].append(Path("after.json"))

        self.assertEqual(
            inventory["nested"]["audit"]["issues"],
            [{"code": "before"}],
        )
        self.assertEqual(
            targets[0]["audit"]["paths"],
            [Path("before.json")],
        )

    def test_stop_cancels_pending_jobs_but_leaves_current_job_to_stop_safely(self):
        gui = load_gui_module()
        app, _checked, _unchecked = self.make_start_app(gui)
        app.pending_batch_jobs = gui.deque([
            {"id": 2, "label": "B"},
            {"id": 3, "label": "C"},
        ])
        app.active_batch_job = {"id": 1, "label": "A"}

        app.stop_batch()

        self.assertTrue(app.stop_flag.is_set())
        self.assertFalse(app.pending_batch_jobs)
        self.assertIn("대기 작업 2개 취소", app.log.call_args.args[0])

    def test_queued_worker_reports_item_failures_instead_of_success(self):
        gui = load_gui_module()
        app, checked, _unchecked = self.make_start_app(gui)
        app.ui_queue = queue.Queue()

        def partial_batch(*_args, **_kwargs):
            app._phase_failed_items = {str(checked["spm"])}
            return True

        app._run_batch = mock.Mock(side_effect=partial_batch)
        job = {
            "id": 8,
            "label": "① SPM",
            "mode": "phase",
            "phase": "spm",
            "targets": [checked],
        }

        app._run_queued_batch_job(job)

        kind, payload = app.ui_queue.get_nowait()
        self.assertEqual(kind, "batch_job_done")
        self.assertEqual(payload["id"], 8)
        self.assertEqual(payload["status"], "partial")
        self.assertEqual(payload["completed_count"], 0)
        self.assertEqual(payload["blocked_count"], 0)
        self.assertEqual(payload["failed_count"], 1)
        self.assertIn("completed=0 blocked=0 failed=1", payload["error"])
        self.assertEqual(
            payload["target_outcomes"][0]["outcome"],
            "failed",
        )

    def test_queued_worker_reports_stop_without_marking_completion(self):
        gui = load_gui_module()
        app, checked, _unchecked = self.make_start_app(gui)
        app.ui_queue = queue.Queue()

        def stopped_batch(*_args, **_kwargs):
            app.stop_flag.set()
            app._phase_failed_items = set()
            return True

        app._run_batch = mock.Mock(side_effect=stopped_batch)
        job = {
            "id": 9,
            "label": "② Blender",
            "mode": "phase",
            "phase": "blender",
            "targets": [checked],
        }

        app._run_queued_batch_job(job)

        kind, payload = app.ui_queue.get_nowait()
        self.assertEqual(kind, "batch_job_done")
        self.assertEqual(payload["id"], 9)
        self.assertEqual(payload["status"], "stopped")
        self.assertEqual(payload["completed_count"], 1)
        self.assertEqual(payload["blocked_count"], 0)
        self.assertEqual(payload["failed_count"], 0)

    def test_queued_worker_uses_one_terminal_event_and_suppresses_legacy_done(self):
        gui = load_gui_module()
        app, checked, _unchecked = self.make_start_app(gui)
        app.ui_queue = queue.Queue()
        app._run_full_pipeline = mock.Mock()
        job = {
            "id": 7,
            "label": "② B",
            "mode": "pipeline",
            "terminal_phase": "blender",
            "selected_scope": True,
            "targets": [checked],
        }

        app._run_queued_batch_job(job)

        app._run_full_pipeline.assert_called_once_with(
            [checked],
            terminal_phase="blender",
            selected_scope=True,
            emit_done=False,
        )
        kind, payload = app.ui_queue.get_nowait()
        self.assertEqual(kind, "batch_job_done")
        self.assertEqual(payload["id"], 7)
        self.assertEqual(payload["status"], "completed")
        self.assertEqual(payload["completed_count"], 1)
        self.assertEqual(payload["blocked_count"], 0)
        self.assertEqual(payload["failed_count"], 0)
        self.assertTrue(app.ui_queue.empty())

    def test_full_pipeline_reports_an_empty_list_not_an_empty_selection(self):
        gui = load_gui_module()
        app = gui.App.__new__(gui.App)
        app.items = {}
        app._close_cell_editor = mock.Mock()

        with mock.patch.object(gui.messagebox, "showinfo") as showinfo:
            app.start_full_pipeline()

        showinfo.assert_called_once_with(
            "SK Batch", "현재 목록에 항목이 없습니다."
        )


if __name__ == "__main__":
    unittest.main()
