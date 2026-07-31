import contextlib
import io
import json
import inspect
import multiprocessing
import os
import tempfile
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

    def test_force_release_requires_age_exact_confirmation_and_live_owner(self):
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
        with self.assertRaises(ForceReleaseRejected):
            queue.force_release(
                running["id"],
                confirm_owner_stopped=running["id"],
            )
        clock_value[0] = 161.0
        with self.assertRaises(ForceReleaseRejected):
            queue.force_release(
                running["id"],
                confirm_owner_stopped="wrong-job",
            )

        released = queue.force_release(
            running["id"],
            confirm_owner_stopped=running["id"],
        )
        self.assertEqual(released["status"], "failed")
        self.assertEqual(
            released["failure_reason"],
            "owner_released_by_operator",
        )
        self.assertTrue(
            released["operator_release"]["owner_worker_stopped_confirmed"]
        )
        self.assertNotIn("username", released["operator_release"])
        with self.assertRaises(LeaseConflict):
            queue.complete(running["id"], claimed["lease"]["token"])
        claimed_next = queue.claim("next-worker", job_id=waiting["id"])
        self.assertEqual(claimed_next["id"], waiting["id"])

    def test_force_release_rejects_an_owner_not_confirmed_alive(self):
        clock_value = [0.0]
        queue = self.queue(
            lease_seconds=1000,
            force_release_min_age_seconds=10,
            clock=lambda: clock_value[0],
            process_alive=lambda _host, _pid, _marker: False,
        )
        job = queue.enqueue("pcg-st9", {})
        queue.claim("worker", job_id=job["id"])
        clock_value[0] = 11.0
        with self.assertRaises(ForceReleaseRejected):
            queue.force_release(
                job["id"],
                confirm_owner_stopped=job["id"],
            )

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
