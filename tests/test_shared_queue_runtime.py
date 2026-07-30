import tempfile
import threading
import time
import unittest
from pathlib import Path

from shared_queue_runtime import (
    RuntimeClosed,
    SharedQueueRuntime,
    WaitCancelled,
)


class SharedQueueRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.state_path = Path(self.temp_dir.name) / "runtime-queue.json"
        self.runtimes = []

    def runtime(self, app_id="same-app", **kwargs):
        runtime = SharedQueueRuntime(app_id, self.state_path, **kwargs)
        self.runtimes.append(runtime)
        return runtime

    def tearDown(self):
        for runtime in reversed(self.runtimes):
            runtime.shutdown()

    def test_two_same_app_runtimes_claim_only_their_exact_callbacks_fifo(self):
        first_runtime = self.runtime()
        second_runtime = self.runtime()
        first = first_runtime.enqueue("first callback", {"window": 1})
        second = second_runtime.enqueue("second callback", {"window": 2})
        wait_seen = threading.Event()
        observed = []
        outcome = {}

        def wait_second():
            outcome["lease"] = second_runtime.wait_for_turn(
                second["id"],
                on_wait=lambda status: (
                    observed.append(status),
                    wait_seen.set(),
                ),
                poll_interval=0.01,
            )

        thread = threading.Thread(target=wait_second)
        thread.start()
        self.assertTrue(wait_seen.wait(2))
        self.assertEqual(observed[0]["position"], 2)
        self.assertEqual(observed[0]["fifo_head"]["id"], first["id"])
        self.assertIsNone(observed[0]["running_head"])
        self.assertEqual(
            first_runtime.queue.get(first["id"])["status"],
            "queued",
        )

        first_lease = first_runtime.wait_for_turn(
            first["id"],
            poll_interval=0.01,
        )
        time.sleep(0.03)
        self.assertNotIn("lease", outcome)
        first_lease.finish(result={"callback": 1})
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(outcome["lease"].job_id, second["id"])
        outcome["lease"].finish(result={"callback": 2})

    def test_spm_pcg_and_sk_runtimes_share_one_fifo(self):
        spm = self.runtime("spm_generator_sync")
        pcg = self.runtime("pcg_st9_texture_batch")
        sk = self.runtime("sk_batch")
        spm_job = spm.enqueue("SPM", {"tool": "spm"})
        pcg_job = pcg.enqueue("PCG", {"tool": "pcg"})
        sk_job = sk.enqueue("SK", {"tool": "sk"})

        self.assertIsNone(
            pcg.queue.claim(
                pcg.owner_id,
                job_id=pcg_job["id"],
                accepted_apps={pcg.app_id},
            )
        )
        self.assertIsNone(
            sk.queue.claim(
                sk.owner_id,
                job_id=sk_job["id"],
                accepted_apps={sk.app_id},
            )
        )

        spm_lease = spm.wait_for_turn(spm_job["id"])
        spm_lease.finish()
        pcg_lease = pcg.wait_for_turn(pcg_job["id"])
        pcg_lease.finish()
        sk_lease = sk.wait_for_turn(sk_job["id"])
        sk_lease.finish()

        snapshot = sk.queue.snapshot()
        completed = [
            job["app_id"]
            for job in sorted(
                snapshot["jobs"],
                key=lambda job: job["sequence"],
            )
        ]
        self.assertEqual(completed, [
            "spm_generator_sync",
            "pcg_st9_texture_batch",
            "sk_batch",
        ])

    def test_wait_callback_reports_running_head_and_global_position(self):
        first_runtime = self.runtime("app-a")
        second_runtime = self.runtime("app-b")
        first = first_runtime.enqueue("running", {"value": 1})
        second = second_runtime.enqueue("waiting", {"value": 2})
        first_lease = first_runtime.wait_for_turn(first["id"])
        cancel = threading.Event()
        observed = []
        error = {}

        def wait_second():
            try:
                second_runtime.wait_for_turn(
                    second["id"],
                    on_wait=lambda status: (
                        observed.append(status),
                        cancel.set(),
                    ),
                    cancel_event=cancel,
                    poll_interval=0.01,
                )
            except BaseException as exc:
                error["value"] = exc

        thread = threading.Thread(target=wait_second)
        thread.start()
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertIsInstance(error["value"], WaitCancelled)
        self.assertEqual(observed[0]["position"], 2)
        self.assertEqual(observed[0]["running_head"]["id"], first["id"])
        self.assertEqual(observed[0]["queued_count"], 1)
        first_lease.finish()

    def test_cancel_event_cancels_waiting_ticket(self):
        first_runtime = self.runtime("app-a")
        waiting_runtime = self.runtime("app-b")
        first = first_runtime.enqueue("blocker", {})
        waiting = waiting_runtime.enqueue("cancel me", {})
        first_lease = first_runtime.wait_for_turn(first["id"])
        cancel = threading.Event()
        result = {}

        def waiter():
            try:
                waiting_runtime.wait_for_turn(
                    waiting["id"],
                    cancel_event=cancel,
                    poll_interval=0.01,
                )
            except BaseException as exc:
                result["error"] = exc

        thread = threading.Thread(target=waiter)
        thread.start()
        time.sleep(0.03)
        cancel.set()
        thread.join(2)
        self.assertIsInstance(result["error"], WaitCancelled)
        self.assertEqual(
            waiting_runtime.queue.get(waiting["id"])["status"],
            "cancelled",
        )
        first_lease.finish()

    def test_background_heartbeat_renews_live_lease(self):
        runtime = self.runtime(
            lease_seconds=0.18,
            heartbeat_interval=0.03,
        )
        job = runtime.enqueue("heartbeat", {"value": 1})
        lease = runtime.wait_for_turn(job["id"])
        before = runtime.queue.get(job["id"])["lease"]["heartbeat_at"]

        time.sleep(0.09)

        after = runtime.queue.get(job["id"])["lease"]["heartbeat_at"]
        self.assertGreater(after, before)
        self.assertIsNone(lease.heartbeat_error)
        lease.finish()

    def test_failed_lease_releases_fifo_and_finish_is_idempotent(self):
        runtime = self.runtime()
        first = runtime.enqueue("fails", {"value": 1})
        second = runtime.enqueue("continues", {"value": 2})
        first_lease = runtime.wait_for_turn(first["id"])

        failed = first_lease.finish(
            success=False,
            result={"error": "injected"},
        )
        again = first_lease.finish(
            success=True,
            result={"must": "not overwrite"},
        )
        self.assertEqual(failed, again)
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["result"], {"error": "injected"})

        second_lease = runtime.wait_for_turn(second["id"])
        self.assertEqual(second_lease.job_id, second["id"])
        second_lease.finish()

    def test_context_manager_records_failure_and_releases_next_job(self):
        runtime = self.runtime()
        first = runtime.enqueue("context failure", {})
        second = runtime.enqueue("after context", {})

        with self.assertRaisesRegex(ValueError, "bad callback"):
            with runtime.wait_for_turn(first["id"]):
                raise ValueError("bad callback")

        failed = runtime.queue.get(first["id"])
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["result"]["error_type"], "ValueError")
        with runtime.wait_for_turn(second["id"]):
            pass

    def test_cancel_all_pending_and_shutdown_are_idempotent(self):
        runtime = self.runtime()
        first = runtime.enqueue("one", {})
        second = runtime.enqueue("two", {})

        cancelled = runtime.cancel_all_pending(reason="window cleared")
        self.assertEqual(set(cancelled), {first["id"], second["id"]})
        self.assertTrue(
            all(row["status"] == "cancelled" for row in cancelled.values())
        )

        runtime.shutdown()
        runtime.shutdown()
        with self.assertRaises(RuntimeClosed):
            runtime.enqueue("late", {})

    def test_shutdown_keeps_active_lease_until_worker_finally_finishes(self):
        first_runtime = self.runtime(
            "app-a",
            lease_seconds=0.18,
            heartbeat_interval=0.03,
        )
        second_runtime = self.runtime("app-b")
        first = first_runtime.enqueue("active", {})
        second = second_runtime.enqueue("next", {})
        first_lease = first_runtime.wait_for_turn(first["id"])
        heartbeat_before = first_runtime.queue.get(first["id"])["lease"][
            "heartbeat_at"
        ]

        first_runtime.shutdown()

        still_running = first_runtime.queue.get(first["id"])
        self.assertEqual(still_running["status"], "running")
        self.assertIsNone(
            second_runtime.queue.claim(
                second_runtime.owner_id,
                job_id=second["id"],
                accepted_apps={"app-b"},
            )
        )
        time.sleep(0.04)
        heartbeat_after = first_runtime.queue.get(first["id"])["lease"][
            "heartbeat_at"
        ]
        self.assertGreater(heartbeat_after, heartbeat_before)

        # The caller has now stopped/joined its real worker and may release.
        first_lease.finish(
            success=False,
            result={"reason": "worker_stopped"},
        )
        next_lease = second_runtime.wait_for_turn(
            second["id"],
            poll_interval=0.01,
        )
        next_lease.finish()


if __name__ == "__main__":
    unittest.main()
