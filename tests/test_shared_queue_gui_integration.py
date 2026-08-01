import importlib.machinery
import importlib.util
import queue
import threading
import unittest
from collections import deque
from pathlib import Path
from unittest import mock


REPO_DIR = Path(__file__).resolve().parents[1]


def load_pyw(name, path):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


SPM_GUI = load_pyw(
    "shared_queue_spm_gui_test",
    REPO_DIR / "spm_generator_sync" / "spm_generator_sync_gui.pyw",
)
SK_GUI = load_pyw(
    "shared_queue_sk_gui_test",
    REPO_DIR / "sk_batch" / "sk_batch_gui.pyw",
)
PCG_GUI = load_pyw(
    "shared_queue_pcg_gui_test",
    REPO_DIR / "pcg_st9_texture_batch" / "pcg_texture_gui.pyw",
)


class FakeVar:
    def __init__(self, value=None):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


class FakeRuntime:
    def __init__(self, events):
        self.events = events

    def enqueue(self, label, payload):
        self.events.append(("enqueue", label, payload))
        return {"id": f"shared-{len(self.events)}", "sequence": len(self.events)}


class FakeLease:
    def __init__(self, events):
        self.events = events
        self.finished = False

    def finish(self, success=True, result=None):
        self.events.append(("finish", success, result["outcome"]))
        self.finished = True


class WaitingRuntime(FakeRuntime):
    def wait_for_turn(
        self, job_id, on_wait=None, cancel_event=None
    ):
        self.events.append(("wait", job_id))
        if on_wait:
            on_wait({"position": 1, "queued_count": 1})
        return FakeLease(self.events)


class SharedQueueGuiIntegrationTests(unittest.TestCase):
    def test_spm_click_defers_global_ticket_until_worker_start(self):
        events = []
        app = SPM_GUI.App.__new__(SPM_GUI.App)
        app.shared_queue_runtime = FakeRuntime(events)
        app.pending_jobs = deque()
        app.active_job = None
        app.job_sequence = 0
        app.job_failures = []
        app.queue_run_total = 0
        app.queue_run_completed = 0
        app._job_has_followup = False
        app._start_next_job = mock.Mock(
            side_effect=lambda: events.append(("local_start",))
        )

        job_id = app._start_job(
            "SPM apply",
            mock.Mock(),
            mock.Mock(),
            queue_label="SPM queue label",
        )

        self.assertEqual(job_id, 1)
        self.assertEqual(events, [("local_start",)])
        queued = app.pending_jobs[0]
        self.assertNotIn("shared_queue_job_id", queued)

    def test_sk_click_registers_global_ticket_before_local_start(self):
        events = []
        app = SK_GUI.App.__new__(SK_GUI.App)
        app.shared_queue_runtime = FakeRuntime(events)
        app.pending_batch_jobs = deque()
        app.active_batch_job = None
        app.batch_job_sequence = 0
        app.batch_job_failures = []
        app._set_batch_queue_controls = mock.Mock()
        app._start_next_batch_job = mock.Mock(
            side_effect=lambda: events.append(("local_start",))
        )
        job = {
            "label": "SK Blender",
            "mode": "pipeline",
            "phase": "blender",
            "targets": [{"spm": Path("SK_tree.spm")}],
        }

        job_id = app._enqueue_batch_job(job)

        self.assertEqual(job_id, 1)
        self.assertEqual(events[0][0], "enqueue")
        self.assertEqual(events[1], ("local_start",))
        queued = app.pending_batch_jobs[0]
        self.assertEqual(queued["shared_queue_job_id"], "shared-1")

    def test_spm_worker_enqueues_then_waits_before_callback(self):
        events = []

        class Root:
            @staticmethod
            def after(_delay, _callback):
                return None

        app = SPM_GUI.App.__new__(SPM_GUI.App)
        app.shared_queue_runtime = WaitingRuntime(events)
        app.root = Root()
        app.pending_jobs = deque([{
            "id": 1,
            "label": "SPM apply",
            "queue_label": "SPM apply",
            "func": lambda _report: events.append(("callback",)) or "ok",
            "on_success": mock.Mock(),
            "shared_queue": True,
        }])
        app.active_job = None
        app.job_queue = queue.Queue()
        app.worker = None
        app.job_started_at = None
        app.job_stage = "대기"
        app.status_var = FakeVar()
        app.progress_text_var = FakeVar()
        app.progress_bar = mock.Mock()

        app._start_next_job()
        app.worker.join(timeout=2)

        self.assertEqual(
            [event[0] for event in events],
            ["enqueue", "wait", "callback", "finish"],
        )
        done = next(
            event
            for event in list(app.job_queue.queue)
            if event[0] == "done"
        )
        self.assertEqual(done[:3], ("done", 1, True))

    def test_sk_worker_waits_for_global_turn_before_batch(self):
        events = []
        app = SK_GUI.App.__new__(SK_GUI.App)
        app.shared_queue_runtime = WaitingRuntime(events)
        app.stop_flag = threading.Event()
        app.ui_queue = queue.Queue()
        app._phase_failed_items = set()
        app.log = mock.Mock()
        app._run_batch = mock.Mock(
            side_effect=lambda *_args, **_kwargs:
            events.append(("callback",)) or True
        )
        job = {
            "id": 1,
            "label": "SK check",
            "mode": "phase",
            "phase": "check",
            "targets": [],
            "shared_queue_job_id": "shared-1",
        }

        app._run_queued_batch_job(job)

        self.assertEqual(
            [event[0] for event in events],
            ["wait", "callback", "finish"],
        )
        terminal = [
            payload
            for kind, payload in list(app.ui_queue.queue)
            if kind == "batch_job_done"
        ]
        self.assertEqual(terminal[0]["status"], "completed")

    def test_pcg_step3_registers_ticket_before_planning_thread(self):
        events = []

        class Root:
            @staticmethod
            def update_idletasks():
                return None

        app = PCG_GUI.App.__new__(PCG_GUI.App)
        app.shared_queue_runtime = FakeRuntime(events)
        app.report = {"items": []}
        app.items = {
            "D:/Tree": {"checked": True, "item": {"folder": "D:/Tree"}},
        }
        app.root = Root()
        app.status_var = FakeVar()
        app._busy = False
        app._set_busy = mock.Mock()
        app.log = mock.Mock()
        app._run_step3_planning = mock.Mock()
        app.items["D:/Tree"]["item"]["_gui_live_evidence"] = {
            "sha256": "current-test-evidence",
        }
        fake_thread = mock.Mock()
        fake_thread.start.side_effect = lambda: events.append(("thread_start",))

        with mock.patch.object(
            PCG_GUI.threading, "Thread", return_value=fake_thread
        ) as thread_class:
            app.start_step3()

        self.assertEqual(events[0][0], "enqueue")
        self.assertEqual(events[1], ("thread_start",))
        self.assertEqual(
            thread_class.call_args.kwargs["args"][0]["id"],
            "shared-1",
        )

    def test_pcg_force_step3_defers_full_plan_until_global_turn(self):
        events = []

        class Root:
            @staticmethod
            def update_idletasks():
                return None

        app = PCG_GUI.App.__new__(PCG_GUI.App)
        app.shared_queue_runtime = FakeRuntime(events)
        app.report = {"items": []}
        app.root = Root()
        app.status_var = FakeVar()
        app._busy = False
        app._set_busy = mock.Mock()
        app.log = mock.Mock()
        app._all_texplan_rows = mock.Mock()
        app._step3_jobs = mock.Mock()
        app._run_step3_force_planning = mock.Mock()
        fake_thread = mock.Mock()
        fake_thread.start.side_effect = lambda: events.append(("thread_start",))

        with mock.patch.object(
            PCG_GUI.messagebox, "askyesno", return_value=True
        ), mock.patch.object(
            PCG_GUI.threading, "Thread", return_value=fake_thread
        ) as thread_class:
            app.start_step3_force()

        app._all_texplan_rows.assert_not_called()
        app._step3_jobs.assert_not_called()
        self.assertEqual(events[0][0], "enqueue")
        self.assertEqual(events[1], ("thread_start",))
        self.assertIs(
            thread_class.call_args.kwargs["target"],
            app._run_step3_force_planning,
        )

    def test_pcg_shared_wrapper_waits_then_releases_after_callback(self):
        events = []

        app = PCG_GUI.App.__new__(PCG_GUI.App)
        app.shared_queue_runtime = WaitingRuntime(events)
        app.status_var = FakeVar()
        app._ui = lambda callback: callback()
        app._shared_execution_failed = mock.Mock()

        result = app._run_shared_execution(
            {"id": "job-1"},
            "PCG action",
            lambda: events.append(("callback",)) or "done",
        )

        self.assertEqual(result, "done")
        self.assertEqual(
            [event[0] for event in events],
            ["wait", "callback", "finish"],
        )


if __name__ == "__main__":
    unittest.main()
