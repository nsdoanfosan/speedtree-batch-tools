import contextlib
import io
import json
import inspect
import multiprocessing
import os
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import shared_job_queue
from shared_job_queue import (
    ForceReleaseRejected,
    JobNotCancellable,
    LeaseConflict,
    QueueStateError,
    SharedJobQueue,
    default_queue_state_path,
)


def _enqueue_in_child(state_path, index, output):
    queue = SharedJobQueue(state_path)
    job = queue.enqueue(
        "child-app",
        {"index": index, "nested": {"source": f"asset-{index}"}},
    )
    output.put(job["id"])


def _claim_in_child(state_path, start, output):
    queue = SharedJobQueue(state_path, lease_seconds=5)
    start.wait(10)
    job = queue.claim(f"worker-{os.getpid()}")
    output.put(None if job is None else job["id"])


def _claim_and_exit(state_path, output):
    queue = SharedJobQueue(state_path, lease_seconds=0.2)
    job = queue.claim(f"dead-worker-{os.getpid()}")
    output.put((job["id"], job["lease"]["token"]))


def _request_release_in_child(state_path, job_id, start, output):
    queue = SharedJobQueue(
        state_path,
        force_release_min_age_seconds=0.01,
    )
    start.wait(10)
    try:
        record = queue.request_release(
            job_id,
            confirm_job_id=job_id,
        )
        output.put({
            "ok": True,
            "request_id": record["release_request"]["id"],
            "requester_pid": os.getpid(),
        })
    except BaseException as exc:
        output.put({"ok": False, "error": repr(exc)})


def _lease_owner_ack_release(state_path, ready, stop_worker, output):
    queue = SharedJobQueue(
        state_path,
        lease_seconds=0.4,
        force_release_min_age_seconds=0.01,
        max_terminal_jobs=3,
    )
    owner_id = f"owner-process-{os.getpid()}"
    claimed = queue.claim(owner_id)
    token = claimed["lease"]["token"]
    worker_stopped = threading.Event()

    def worker():
        stop_worker.wait(10)
        worker_stopped.set()

    worker_thread = threading.Thread(target=worker)
    worker_thread.start()
    output.put({"phase": "claimed", "job_id": claimed["id"]})
    ready.set()
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            renewed = queue.heartbeat(
                claimed["id"],
                token,
                owner_id=owner_id,
            )
        except BaseException as exc:
            output.put({"phase": "error", "error": repr(exc)})
            return
        request = renewed.get("release_request")
        if request and stop_worker.is_set():
            worker_thread.join(5)
            if not worker_stopped.is_set():
                output.put({"phase": "error", "error": "worker_not_joined"})
                return
            acknowledged = queue.acknowledge_release(
                claimed["id"],
                token,
                request_id=request["id"],
                owner_id=owner_id,
            )
            try:
                queue.complete(claimed["id"], token, owner_id=owner_id)
            except LeaseConflict:
                late_complete = "lease_conflict"
            else:
                late_complete = "unexpected_success"
            output.put({
                "phase": "acknowledged",
                "record": acknowledged,
                "late_complete": late_complete,
            })
            return
        time.sleep(0.02)
    output.put({"phase": "error", "error": "release_request_timeout"})


def _lease_owner_exit_without_ack(state_path, ready, exit_owner, output):
    queue = SharedJobQueue(
        state_path,
        lease_seconds=0.15,
        force_release_min_age_seconds=0.01,
        max_terminal_jobs=3,
    )
    claimed = queue.claim(f"owner-exit-{os.getpid()}")
    output.put({"phase": "claimed", "job_id": claimed["id"]})
    ready.set()
    exit_owner.wait(10)


def _lease_owner_complete_after_signal(
    state_path,
    ready,
    finish,
    output,
    max_terminal_jobs,
):
    queue = SharedJobQueue(
        state_path,
        lease_seconds=10,
        max_terminal_jobs=max_terminal_jobs,
    )
    owner_id = f"late-owner-{os.getpid()}"
    claimed = queue.claim(owner_id)
    output.put({"phase": "claimed", "job_id": claimed["id"]})
    ready.set()
    finish.wait(10)
    completed = queue.complete(
        claimed["id"],
        claimed["lease"]["token"],
        owner_id=owner_id,
    )
    output.put({"phase": "completed", "record": completed})


class SharedJobQueueTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.state_path = Path(self.temp_dir.name) / "shared-queue.json"

    def queue(self, **kwargs):
        return SharedJobQueue(self.state_path, **kwargs)

    def test_fifo_and_application_payload_are_preserved(self):
        queue = self.queue()
        original = {
            "paths": [r"C:\Tree\가나다.spm"],
            "options": {"stage": 3, "enabled": True},
        }
        first = queue.enqueue(
            "pcg-st9",
            original,
            label="PCG ③",
            metadata={"window": "pcg-main"},
        )
        second = queue.enqueue("sk-batch", {"targets": ["oak", "pine"]})

        original["paths"].append("mutated-after-enqueue")
        first["payload"]["options"]["stage"] = 99

        claimed_first = queue.claim("pcg-worker", accepted_apps={"pcg-st9"})
        self.assertEqual(claimed_first["id"], first["id"])
        self.assertEqual(
            claimed_first["payload"],
            {
                "paths": [r"C:\Tree\가나다.spm"],
                "options": {"stage": 3, "enabled": True},
            },
        )
        queue.complete(
            first["id"],
            claimed_first["lease"]["token"],
            result={"receipt": "ok"},
        )
        claimed_second = queue.claim("sk-worker", accepted_apps={"sk-batch"})
        self.assertEqual(claimed_second["id"], second["id"])

    def test_application_filter_never_skips_the_fifo_head(self):
        queue = self.queue()
        first = queue.enqueue("pcg-st9", {"stage": 3})
        queue.enqueue("sk-batch", {"stage": "push"})

        self.assertIsNone(
            queue.claim("sk-worker", accepted_apps={"sk-batch"})
        )
        claimed = queue.claim("pcg-worker", accepted_apps={"pcg-st9"})
        self.assertEqual(claimed["id"], first["id"])

    def test_exact_job_guard_never_lets_another_same_app_window_skip_head(self):
        queue = self.queue()
        first_window = queue.enqueue("pcg-st9", {"window": "first"})
        second_window = queue.enqueue("pcg-st9", {"window": "second"})

        self.assertIsNone(
            queue.claim(
                "second-window-worker",
                job_id=second_window["id"],
                accepted_apps={"pcg-st9"},
            )
        )
        snapshot = queue.snapshot()
        self.assertEqual(
            [job["status"] for job in snapshot["jobs"]],
            ["queued", "queued"],
        )

        claimed = queue.claim(
            "first-window-worker",
            job_id=first_window["id"],
            accepted_apps={"pcg-st9"},
        )
        self.assertEqual(claimed["id"], first_window["id"])
        self.assertIsNone(
            queue.claim(
                "second-window-worker",
                job_id=second_window["id"],
                accepted_apps={"pcg-st9"},
            )
        )
        queue.complete(first_window["id"], claimed["lease"]["token"])
        claimed_second = queue.claim(
            "second-window-worker",
            job_id=second_window["id"],
            accepted_apps={"pcg-st9"},
        )
        self.assertEqual(claimed_second["id"], second_window["id"])

    def test_default_path_is_shared_local_app_data_without_creating_it(self):
        local_app_data = Path(self.temp_dir.name) / "LocalAppData"
        with mock.patch.dict(
            os.environ,
            {"LOCALAPPDATA": str(local_app_data)},
        ):
            path = default_queue_state_path()

        if os.name == "nt":
            self.assertEqual(
                path,
                local_app_data
                / "SpeedTreeBatchTools"
                / "shared_job_queue.json",
            )
            self.assertFalse(local_app_data.exists())

    def test_heartbeat_extends_lease_and_only_token_owner_can_complete(self):
        clock_value = [100.0]
        queue = self.queue(lease_seconds=10, clock=lambda: clock_value[0])
        job = queue.enqueue("spm-sync", {"groups": ["A"]})
        claimed = queue.claim("sync-window")
        token = claimed["lease"]["token"]

        clock_value[0] = 106.0
        renewed = queue.heartbeat(job["id"], token, owner_id="sync-window")
        self.assertEqual(renewed["lease"]["expires_at"], 116.0)
        with self.assertRaises(LeaseConflict):
            queue.complete(job["id"], "wrong-token")

        finished = queue.complete(
            job["id"],
            token,
            result={"count": 1},
            owner_id="sync-window",
        )
        self.assertEqual(finished["status"], "completed")
        self.assertEqual(finished["result"], {"count": 1})
        self.assertIsNone(finished["lease"])

    def test_expired_lease_stays_running_while_owner_process_is_alive(self):
        clock_value = [10.0]
        queue = self.queue(lease_seconds=5, clock=lambda: clock_value[0])
        first = queue.enqueue("pcg-st9", {"name": "first"})
        queue.enqueue("sk-batch", {"name": "second"})
        old = queue.claim("old-worker")
        old_token = old["lease"]["token"]

        clock_value[0] = 16.0
        self.assertIsNone(queue.claim("new-worker"))
        still_running = queue.get(first["id"])
        self.assertEqual(still_running["status"], "running")
        self.assertEqual(still_running["lease"]["token"], old_token)

        renewed = queue.heartbeat(first["id"], old_token)
        self.assertEqual(renewed["lease"]["expires_at"], 21.0)

    def test_cancel_applies_only_to_waiting_job(self):
        queue = self.queue()
        running = queue.enqueue("pcg-st9", {"name": "running"})
        waiting = queue.enqueue("sk-batch", {"name": "waiting"})
        claimed = queue.claim("worker")

        cancelled = queue.cancel(waiting["id"], reason="user removed it")
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertEqual(cancelled["cancel_reason"], "user removed it")
        with self.assertRaises(JobNotCancellable):
            queue.cancel(running["id"])

        queue.complete(running["id"], claimed["lease"]["token"])
        self.assertIsNone(queue.claim("worker"))

    def test_terminal_history_is_bounded_without_pruning_live_jobs(self):
        queue = self.queue(max_terminal_jobs=3)
        for index in range(40):
            job = queue.enqueue("pcg-st9", {"fixed": "x" * 50, "i": index})
            claimed = queue.claim("worker", job_id=job["id"])
            queue.complete(job["id"], claimed["lease"]["token"])

        waiting = queue.enqueue("sk-batch", {"still": "queued"})
        snapshot = queue.snapshot()
        terminal = [
            job for job in snapshot["jobs"] if job["status"] == "completed"
        ]
        self.assertEqual(len(terminal), 3)
        self.assertEqual(
            [job["sequence"] for job in terminal],
            [38, 39, 40],
        )
        self.assertIn(waiting["id"], {job["id"] for job in snapshot["jobs"]})
        self.assertEqual(snapshot["next_sequence"], 42)
        self.assertLess(self.state_path.stat().st_size, 10_000)

    def test_terminal_history_three_day_boundary_is_strict(self):
        clock_value = [1_000.0]
        max_age = 3 * 24 * 60 * 60
        queue = self.queue(
            max_terminal_jobs=10,
            max_terminal_age_seconds=max_age,
            clock=lambda: clock_value[0],
        )
        job = queue.enqueue("pcg-st9", {"audit": "bounded"})
        claimed = queue.claim("worker", job_id=job["id"])
        queue.complete(job["id"], claimed["lease"]["token"])

        clock_value[0] = 1_000.0 + max_age
        self.assertEqual(
            [row["id"] for row in queue.snapshot()["jobs"]],
            [job["id"]],
        )
        clock_value[0] += 0.001
        self.assertEqual(queue.snapshot()["jobs"], [])

    def test_release_request_does_not_unblock_and_requires_owner_token_ack(self):
        clock_value = [100.0]
        queue = self.queue(
            lease_seconds=1000,
            force_release_min_age_seconds=60,
            clock=lambda: clock_value[0],
            process_alive=lambda _host, _pid, _marker: True,
        )
        running = queue.enqueue("pcg-st9", {"value": 1})
        waiting = queue.enqueue("sk-batch", {"value": 2})
        claimed = queue.claim("worker", job_id=running["id"])

        clock_value[0] = 130.0
        with self.assertRaisesRegex(
            ForceReleaseRejected,
            "release_min_age_not_met",
        ):
            queue.request_release(
                running["id"],
                confirm_job_id=running["id"],
            )
        clock_value[0] = 161.0
        with self.assertRaisesRegex(
            ForceReleaseRejected,
            "release_confirmation_mismatch",
        ):
            queue.request_release(
                running["id"],
                confirm_job_id="wrong-job",
            )

        requested = queue.request_release(
            running["id"],
            confirm_job_id=running["id"],
        )
        request_id = requested["release_request"]["id"]
        self.assertEqual(requested["status"], "running")
        self.assertEqual(requested["lease"]["token"], claimed["lease"]["token"])
        self.assertNotIn(
            "token",
            requested["release_request"]["original_lease"],
        )
        self.assertEqual(
            set(requested["release_request"]["requester"]),
            {"pid", "hostname", "process_marker"},
        )
        self.assertNotIn("username", json.dumps(requested))
        self.assertIsNone(queue.claim("next-worker", job_id=waiting["id"]))
        with self.assertRaisesRegex(LeaseConflict, "lease_conflict"):
            queue.acknowledge_release(
                running["id"],
                "wrong-token",
                request_id=request_id,
                owner_id="worker",
            )

        released = queue.acknowledge_release(
            running["id"],
            claimed["lease"]["token"],
            request_id=request_id,
            owner_id="worker",
        )
        self.assertEqual(
            released["failure_reason"],
            "owner_released_by_operator",
        )
        self.assertNotIn("token", released["last_lease"])
        self.assertEqual(released["release_ack"]["request_id"], request_id)
        with self.assertRaises(LeaseConflict):
            queue.complete(running["id"], claimed["lease"]["token"])
        claimed_next = queue.claim("next-worker", job_id=waiting["id"])
        self.assertEqual(claimed_next["id"], waiting["id"])

    def test_release_request_rejects_an_owner_not_confirmed_alive(self):
        clock_value = [0.0]
        process_alive = [True]
        queue = self.queue(
            lease_seconds=1000,
            force_release_min_age_seconds=10,
            clock=lambda: clock_value[0],
            process_alive=lambda _host, _pid, _marker: process_alive[0],
        )
        job = queue.enqueue("pcg-st9", {})
        queue.claim("worker", job_id=job["id"])
        clock_value[0] = 11.0
        process_alive[0] = False
        with self.assertRaisesRegex(
            ForceReleaseRejected,
            "release_owner_not_confirmed_alive",
        ):
            queue.request_release(
                job["id"],
                confirm_job_id=job["id"],
            )

    def test_two_phase_release_process_race_is_fail_closed_and_audited(self):
        context = multiprocessing.get_context("spawn")
        for iteration in range(3):
            with self.subTest(iteration=iteration):
                state_path = Path(self.temp_dir.name) / f"release-{iteration}.json"
                queue = SharedJobQueue(
                    state_path,
                    lease_seconds=0.4,
                    force_release_min_age_seconds=0.01,
                    max_terminal_jobs=3,
                )
                running = queue.enqueue("pcg-st9", {"iteration": iteration})
                ready = context.Event()
                stop_worker = context.Event()
                owner_output = context.Queue()
                owner = context.Process(
                    target=_lease_owner_ack_release,
                    args=(str(state_path), ready, stop_worker, owner_output),
                )
                owner.start()
                self.assertTrue(ready.wait(5))
                self.assertEqual(owner_output.get(timeout=2)["phase"], "claimed")

                for index in range(5):
                    row = queue.enqueue("cancelled", {"index": index})
                    queue.cancel(row["id"], reason="retention_pressure")
                waiting = queue.enqueue("sk-batch", {"next": True})
                time.sleep(0.03)

                request_start = context.Event()
                request_output = context.Queue()
                requester = context.Process(
                    target=_request_release_in_child,
                    args=(str(state_path), running["id"], request_start, request_output),
                )
                requester.start()
                request_start.set()
                request_result = request_output.get(timeout=5)
                requester.join(10)
                self.assertEqual(requester.exitcode, 0)
                self.assertTrue(request_result["ok"], request_result)

                requested = queue.get(running["id"])
                self.assertEqual(requested["status"], "running")
                self.assertEqual(
                    requested["release_request"]["id"],
                    request_result["request_id"],
                )
                self.assertEqual(
                    requested["release_request"]["requester"]["pid"],
                    request_result["requester_pid"],
                )
                self.assertIsNone(
                    queue.claim("must-stay-blocked", job_id=waiting["id"])
                )
                with self.assertRaises(LeaseConflict):
                    queue.acknowledge_release(
                        running["id"],
                        "not-owner-token",
                        request_id=request_result["request_id"],
                    )

                stop_worker.set()
                owner.join(10)
                self.assertEqual(owner.exitcode, 0)
                owner_result = owner_output.get(timeout=2)
                self.assertEqual(owner_result["phase"], "acknowledged")
                self.assertEqual(owner_result["late_complete"], "lease_conflict")
                released = owner_result["record"]
                self.assertEqual(
                    released["failure_reason"],
                    "owner_released_by_operator",
                )
                self.assertEqual(
                    released["release_ack"]["request_id"],
                    request_result["request_id"],
                )
                self.assertNotIn("token", released["last_lease"])
                self.assertIn("requester", released["release_request"])
                self.assertIn("acknowledger", released["release_ack"])
                snapshot = queue.snapshot()
                retained = {job["id"] for job in snapshot["jobs"]}
                self.assertIn(running["id"], retained)
                claimed_next = queue.claim("next-owner", job_id=waiting["id"])
                self.assertEqual(claimed_next["id"], waiting["id"])

    def test_release_request_owner_exit_uses_owner_lost_without_ack(self):
        context = multiprocessing.get_context("spawn")
        for iteration in range(3):
            with self.subTest(iteration=iteration):
                state_path = Path(self.temp_dir.name) / f"owner-lost-{iteration}.json"
                queue = SharedJobQueue(
                    state_path,
                    lease_seconds=0.15,
                    force_release_min_age_seconds=0.01,
                    max_terminal_jobs=3,
                )
                running = queue.enqueue("pcg-st9", {})
                ready = context.Event()
                exit_owner = context.Event()
                owner_output = context.Queue()
                owner = context.Process(
                    target=_lease_owner_exit_without_ack,
                    args=(str(state_path), ready, exit_owner, owner_output),
                )
                owner.start()
                self.assertTrue(ready.wait(5))
                self.assertEqual(owner_output.get(timeout=2)["phase"], "claimed")
                for index in range(5):
                    row = queue.enqueue("cancelled", {"index": index})
                    queue.cancel(row["id"], reason="retention_pressure")
                waiting = queue.enqueue("next", {})
                time.sleep(0.03)
                requested = queue.request_release(
                    running["id"],
                    confirm_job_id=running["id"],
                )
                request_id = requested["release_request"]["id"]
                exit_owner.set()
                owner.join(10)
                self.assertEqual(owner.exitcode, 0)
                time.sleep(0.2)

                claimed_next = queue.claim("replacement", job_id=waiting["id"])
                failed = queue.get(running["id"])
                self.assertEqual(failed["failure_reason"], "owner_lost")
                self.assertEqual(failed["release_request"]["id"], request_id)
                self.assertNotIn("release_ack", failed)
                self.assertIn("last_expired_lease", failed)
                self.assertNotIn("token", failed["last_expired_lease"])
                self.assertEqual(claimed_next["id"], waiting["id"])

    def test_operator_close_then_owner_loss_has_ordered_durable_receipt(self):
        clock_value = [0.0]
        process_alive = [True]
        queue = self.queue(
            lease_seconds=1,
            clock=lambda: clock_value[0],
            process_alive=lambda _host, _pid, _marker: process_alive[0],
        )
        job = queue.enqueue("sk-batch", {"retry": "seq81-shape"})
        claimed = queue.claim("sk-owner", job_id=job["id"])

        recorded = queue.record_operator_close_request(
            job["id"],
            claimed["lease"]["token"],
            owner_id="sk-owner",
        )

        self.assertEqual(recorded["status"], "running")
        self.assertNotIn("failure_reason", recorded)
        close_event = recorded["termination_audit"]["events"][0]
        self.assertEqual(close_event["kind"], "operator_close_requested")
        self.assertEqual(close_event["batch_outcome_at_event"], "running")
        self.assertNotIn("token", close_event["owner"])

        repeated = queue.record_operator_close_request(
            job["id"],
            claimed["lease"]["token"],
            owner_id="sk-owner",
        )
        self.assertEqual(len(repeated["termination_audit"]["events"]), 1)

        process_alive[0] = False
        clock_value[0] = 2.0
        failed = queue.get(job["id"])

        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["failure_reason"], "owner_lost")
        self.assertIsNone(failed["result"])
        audit = failed["termination_audit"]
        self.assertEqual(
            [event["kind"] for event in audit["events"]],
            ["operator_close_requested", "owner_lost_recovered"],
        )
        self.assertEqual(
            [event["sequence"] for event in audit["events"]],
            [1, 2],
        )
        recovery = audit["events"][1]
        self.assertEqual(recovery["trigger"], "operator_close_requested")
        self.assertEqual(recovery["original_batch_outcome"], "unknown")
        self.assertEqual(
            audit["terminal_interpretation"],
            {
                "terminal_reason": "owner_lost",
                "trigger": "operator_close_requested",
                "original_batch_outcome": "unknown",
            },
        )

    def test_late_finishing_old_sequence_is_retained_by_terminal_time(self):
        context = multiprocessing.get_context("spawn")
        for iteration in range(3):
            with self.subTest(iteration=iteration):
                state_path = Path(self.temp_dir.name) / f"terminal-{iteration}.json"
                queue = SharedJobQueue(state_path, max_terminal_jobs=3)
                first = queue.enqueue("long-running", {})
                ready = context.Event()
                finish = context.Event()
                output = context.Queue()
                owner = context.Process(
                    target=_lease_owner_complete_after_signal,
                    args=(str(state_path), ready, finish, output, 3),
                )
                owner.start()
                self.assertTrue(ready.wait(5))
                self.assertEqual(output.get(timeout=2)["phase"], "claimed")
                cancelled = []
                for index in range(5):
                    row = queue.enqueue("cancelled", {"index": index})
                    queue.cancel(row["id"], reason="retention_pressure")
                    cancelled.append(row["id"])
                    time.sleep(0.01)
                finish.set()
                owner.join(10)
                self.assertEqual(owner.exitcode, 0)
                self.assertEqual(output.get(timeout=2)["phase"], "completed")

                snapshot = queue.snapshot()
                terminal = [
                    job for job in snapshot["jobs"]
                    if job["status"] in shared_job_queue.TERMINAL_STATUSES
                ]
                retained = {job["id"] for job in terminal}
                self.assertEqual(len(terminal), 3)
                self.assertIn(first["id"], retained)
                self.assertNotIn(cancelled[0], retained)
                completed = next(job for job in terminal if job["id"] == first["id"])
                self.assertEqual(completed["terminal_at"], completed["finished_at"])

    def test_windows_liveness_api_is_bound_once_at_module_load(self):
        source = inspect.getsource(shared_job_queue)
        self.assertEqual(source.count('WinDLL("kernel32"'), 1)
        self.assertNotIn(
            "WinDLL",
            inspect.getsource(shared_job_queue._local_process_alive),
        )
        self.assertNotIn(
            "WinDLL",
            inspect.getsource(shared_job_queue._process_marker),
        )

    def test_pid_reuse_marker_mismatch_is_not_treated_as_same_process(self):
        with mock.patch(
            "shared_job_queue._process_marker",
            return_value="actual-process-marker",
        ):
            self.assertFalse(
                shared_job_queue._local_process_alive(
                    socket.gethostname(),
                    os.getpid(),
                    "stale-process-marker",
                )
            )

    def test_remote_process_liveness_is_unknown_and_stale_lease_stays_owned(self):
        self.assertIsNone(
            shared_job_queue._local_process_alive(
                "definitely-remote-host",
                12345,
                "remote-marker",
            )
        )
        clock_value = [0.0]
        queue = self.queue(
            lease_seconds=5,
            clock=lambda: clock_value[0],
            process_alive=lambda _host, _pid, _marker: None,
        )
        running = queue.enqueue("remote-owner", {})
        waiting = queue.enqueue("waiting", {})
        claimed = queue.claim("remote-owner", job_id=running["id"])
        clock_value[0] = 10.0

        poll = queue.poll_for_turn(
            "waiting-owner",
            job_id=waiting["id"],
            accepted_apps={"waiting"},
        )
        current = next(
            job for job in poll["snapshot"]["jobs"]
            if job["id"] == running["id"]
        )
        self.assertFalse(poll["claimed"])
        self.assertEqual(current["status"], "running")
        self.assertEqual(current["lease"]["token"], claimed["lease"]["token"])
        self.assertNotIn("failure_reason", current)

    def test_operator_status_omits_payloads_and_machine_paths(self):
        queue = self.queue()
        sensitive_path = r"C:\Users\operator\private\SK_tree.spm"
        queue.enqueue("pcg-st9", {"path": sensitive_path})
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            self.assertEqual(shared_job_queue._cli_status(queue), 0)

        rendered = output.getvalue()
        self.assertNotIn(sensitive_path, rendered)
        self.assertNotIn("payload", rendered)

    def test_failed_atomic_replace_keeps_last_complete_snapshot(self):
        queue = self.queue()
        first = queue.enqueue("pcg-st9", {"value": 1})
        before = self.state_path.read_bytes()

        with mock.patch(
            "shared_job_queue.os.replace", side_effect=OSError("injected")
        ):
            with self.assertRaises(OSError):
                queue.enqueue("sk-batch", {"value": 2})

        self.assertEqual(self.state_path.read_bytes(), before)
        snapshot = queue.snapshot()
        self.assertEqual([job["id"] for job in snapshot["jobs"]], [first["id"]])
        self.assertFalse(
            list(self.state_path.parent.glob(f".{self.state_path.name}.*.tmp"))
        )

    def test_malformed_state_is_never_silently_overwritten(self):
        self.state_path.write_text('{"schema_version": 1, "jobs":', encoding="utf-8")
        with self.assertRaises(QueueStateError):
            self.queue().enqueue("pcg-st9", {"value": 1})
        self.assertEqual(
            self.state_path.read_text(encoding="utf-8"),
            '{"schema_version": 1, "jobs":',
        )

    def test_multiple_processes_enqueue_without_lost_updates(self):
        context = multiprocessing.get_context("spawn")
        output = context.Queue()
        processes = [
            context.Process(
                target=_enqueue_in_child,
                args=(str(self.state_path), index, output),
            )
            for index in range(8)
        ]
        for process in processes:
            process.start()
        for process in processes:
            process.join(15)
            self.assertEqual(process.exitcode, 0)

        returned_ids = {output.get(timeout=2) for _ in processes}
        snapshot = self.queue().snapshot()
        stored_ids = {job["id"] for job in snapshot["jobs"]}
        self.assertEqual(stored_ids, returned_ids)
        self.assertEqual(
            sorted(job["sequence"] for job in snapshot["jobs"]),
            list(range(1, 9)),
        )
        # Atomic snapshots are ordinary parseable JSON even after contention.
        self.assertEqual(
            json.loads(self.state_path.read_text(encoding="utf-8"))["revision"],
            8,
        )

    def test_competing_processes_can_create_only_one_live_lease(self):
        queue = self.queue()
        job = queue.enqueue("pcg-st9", {"value": 1})
        context = multiprocessing.get_context("spawn")
        start = context.Event()
        output = context.Queue()
        processes = [
            context.Process(
                target=_claim_in_child,
                args=(str(self.state_path), start, output),
            )
            for _ in range(2)
        ]
        for process in processes:
            process.start()
        start.set()
        for process in processes:
            process.join(15)
            self.assertEqual(process.exitcode, 0)

        claims = [output.get(timeout=2) for _ in processes]
        self.assertEqual(claims.count(job["id"]), 1)
        self.assertEqual(claims.count(None), 1)
        running = [
            row
            for row in queue.snapshot()["jobs"]
            if row["status"] == "running"
        ]
        self.assertEqual(len(running), 1)

    def test_dead_process_running_job_fails_instead_of_reexecuting(self):
        queue = self.queue(lease_seconds=0.2)
        job = queue.enqueue("pcg-st9", {"value": 1})
        next_job = queue.enqueue("sk-batch", {"value": 2})
        context = multiprocessing.get_context("spawn")
        output = context.Queue()
        process = context.Process(
            target=_claim_and_exit,
            args=(str(self.state_path), output),
        )
        process.start()
        claimed_id, dead_token = output.get(timeout=5)
        process.join(10)
        self.assertEqual(process.exitcode, 0)
        self.assertEqual(claimed_id, job["id"])

        time.sleep(0.3)
        claimed_next = queue.claim("replacement-worker")
        failed = queue.get(job["id"])
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["failure_reason"], "owner_lost")
        self.assertEqual(failed["recovery_count"], 1)
        self.assertIsNone(failed["lease"])
        self.assertEqual(claimed_next["id"], next_job["id"])
        self.assertNotEqual(claimed_next["lease"]["token"], dead_token)

    def test_poll_transaction_recovers_running_cleans_dead_head_and_claims(self):
        context = multiprocessing.get_context("spawn")
        for iteration in range(3):
            with self.subTest(iteration=iteration):
                state_path = Path(self.temp_dir.name) / f"poll-all-{iteration}.json"
                queue = SharedJobQueue(state_path, lease_seconds=0.2)
                expired = queue.enqueue("expired-running", {})
                owner_output = context.Queue()
                owner = context.Process(
                    target=_claim_and_exit,
                    args=(str(state_path), owner_output),
                )
                owner.start()
                self.assertEqual(owner_output.get(timeout=5)[0], expired["id"])
                owner.join(10)
                self.assertEqual(owner.exitcode, 0)

                dead_output = context.Queue()
                dead_origin = context.Process(
                    target=_enqueue_in_child,
                    args=(str(state_path), iteration, dead_output),
                )
                dead_origin.start()
                dead_head_id = dead_output.get(timeout=5)
                dead_origin.join(10)
                self.assertEqual(dead_origin.exitcode, 0)
                live = queue.enqueue("live", {"iteration": iteration})
                revision_before = json.loads(
                    state_path.read_text(encoding="utf-8")
                )["revision"]

                time.sleep(0.3)
                poll = queue.poll_for_turn(
                    "live-owner",
                    job_id=live["id"],
                    accepted_apps={"live"},
                )

                self.assertTrue(poll["claimed"])
                rows = {job["id"]: job for job in poll["snapshot"]["jobs"]}
                self.assertEqual(rows[expired["id"]]["failure_reason"], "owner_lost")
                self.assertEqual(
                    rows[dead_head_id]["abandon_reason"],
                    "origin_process_exited",
                )
                self.assertEqual(rows[live["id"]]["status"], "running")
                revision_after = json.loads(
                    state_path.read_text(encoding="utf-8")
                )["revision"]
                self.assertEqual(revision_after, revision_before + 1)

    def test_dead_origin_queued_head_is_abandoned_and_fifo_continues(self):
        context = multiprocessing.get_context("spawn")
        output = context.Queue()
        process = context.Process(
            target=_enqueue_in_child,
            args=(str(self.state_path), 1, output),
        )
        process.start()
        abandoned_id = output.get(timeout=5)
        process.join(10)
        self.assertEqual(process.exitcode, 0)

        queue = self.queue()
        live = queue.enqueue("pcg-st9", {"value": "live"})
        claimed = queue.claim("live-worker")

        abandoned = queue.get(abandoned_id)
        self.assertEqual(abandoned["status"], "abandoned")
        self.assertEqual(
            abandoned["abandon_reason"],
            "origin_process_exited",
        )
        self.assertEqual(claimed["id"], live["id"])


if __name__ == "__main__":
    unittest.main()
