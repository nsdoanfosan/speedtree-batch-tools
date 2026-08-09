import json
import tempfile
import threading
import unittest
from pathlib import Path

from exact_target_command import build_exact_target_request, run_exact_target_request
from shared_queue_runtime import WaitCancelled


class FakeLease:
    def __init__(self, events):
        self.events = events
        self.finished = False

    def renew_and_check_current(self):
        self.events.append("owner_ack")
        return True

    def finish(self, success, result, terminal_status=None):
        self.events.append((
            "finish",
            success,
            result["status"],
            terminal_status,
        ))
        self.finished = True


class FakeRuntime:
    def __init__(self):
        self.events = []
        self.owner_id = "owner"

    def enqueue(self, label, payload):
        self.events.append(("enqueue", label, payload))
        return {"id": "queue-1", "sequence": 7}

    def wait_for_turn(self, job_id, cancel_event=None):
        self.events.append(("wait", job_id, cancel_event.is_set()))
        return FakeLease(self.events)


class ExactTargetCommandTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.target = root / "한 글 target.spm"
        self.target.write_bytes(b"spm")
        self.receipt = root / "receipt 폴더" / "result.json"
        self.request = build_exact_target_request(
            tool="tool",
            repair_action="action",
            target_spms=[self.target, self.target],
            repair_stage="stage",
            provenance={"reason_codes": ["reason"]},
            parent_retry_id="parent",
            request_id="request",
            receipt=self.receipt,
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_queue_owner_ack_precedes_executor_and_terminal_receipt(self):
        runtime = FakeRuntime()

        def executor(request, *, progress, cancel_event, lease):
            runtime.events.append("executor")
            progress("working", completed=0, remaining=1)
            return {"status": "completed", "shared_queue_success": True}

        terminal = run_exact_target_request(
            self.request,
            executor,
            runtime=runtime,
            cancel_event=threading.Event(),
        )
        self.assertEqual(terminal["status"], "completed")
        self.assertLess(runtime.events.index("owner_ack"), runtime.events.index("executor"))
        payload = json.loads(self.receipt.read_text(encoding="utf-8"))
        self.assertEqual(payload["status"], "completed")
        self.assertEqual(payload["exit_code"], 0)
        self.assertEqual(payload["target_spms"], [str(self.target)])
        self.assertEqual(payload["provenance"]["reason_codes"], ["reason"])
        enqueue_payload = runtime.events[0][2]
        self.assertEqual(
            enqueue_payload["provenance"]["reason_codes"],
            ["reason"],
        )
        self.assertIn(
            ("finish", True, "completed", "completed"),
            runtime.events,
        )

    def test_inherited_lease_is_not_finished_by_nested_tool(self):
        events = []
        lease = FakeLease(events)
        terminal = run_exact_target_request(
            self.request,
            lambda *_args, **_kwargs: {
                "status": "completed", "shared_queue_success": True
            },
            inherited_lease=lease,
        )
        self.assertEqual(terminal["status"], "completed")
        self.assertFalse(lease.finished)

    def test_legacy_owned_lease_without_optional_terminal_status_still_finishes(self):
        runtime = FakeRuntime()
        events = runtime.events

        class LegacyLease:
            finished = False

            def renew_and_check_current(self):
                events.append("legacy_owner_ack")
                return True

            def finish(self, success, result):
                events.append(("legacy_finish", success, result["status"]))
                self.finished = True

        runtime.wait_for_turn = lambda *_args, **_kwargs: LegacyLease()
        terminal = run_exact_target_request(
            self.request,
            lambda *_args, **_kwargs: {
                "status": "completed",
                "shared_queue_success": True,
            },
            runtime=runtime,
        )
        self.assertEqual(terminal["terminal_status"], "completed")
        self.assertIn(("legacy_finish", True, "completed"), events)

    def test_executor_failure_is_terminal_and_sanitized_to_receipt(self):
        runtime = FakeRuntime()

        def fail(*_args, **_kwargs):
            raise ValueError("bad exact plan")

        terminal = run_exact_target_request(self.request, fail, runtime=runtime)
        self.assertEqual(terminal["status"], "failed")
        self.assertEqual(terminal["exit_code"], 1)
        self.assertIn("ValueError", terminal["error"])
        self.assertEqual(
            json.loads(self.receipt.read_text(encoding="utf-8"))["status"],
            "failed",
        )

    def test_executor_failure_preserves_lifecycle_kind(self):
        runtime = FakeRuntime()

        class OwnerLostError(RuntimeError):
            kind = "owner_lost"

        def fail(*_args, **_kwargs):
            raise OwnerLostError("exact lease expired")

        terminal = run_exact_target_request(self.request, fail, runtime=runtime)
        payload = json.loads(self.receipt.read_text(encoding="utf-8"))
        self.assertEqual(terminal["status"], "failed")
        self.assertEqual(terminal["failure_kind"], "owner_lost")
        self.assertEqual(payload["failure_kind"], "owner_lost")

    def test_cancel_is_terminal_130_and_not_a_failure(self):
        runtime = FakeRuntime()

        def cancel(*_args, **_kwargs):
            raise WaitCancelled("operator stopped exact repair")

        terminal = run_exact_target_request(self.request, cancel, runtime=runtime)
        self.assertEqual(terminal["status"], "cancelled")
        self.assertEqual(terminal["exit_code"], 130)
        self.assertIn(
            ("finish", False, "cancelled", "cancelled"),
            runtime.events,
        )
        self.assertEqual(terminal["result"]["outcome"], "stopped")
        self.assertEqual(terminal["result"]["failed_count"], 0)
        self.assertEqual(
            terminal["result"]["target_outcomes"][0]["outcome"],
            "cancelled",
        )
        self.assertEqual(
            json.loads(self.receipt.read_text(encoding="utf-8"))["status"],
            "cancelled",
        )


if __name__ == "__main__":
    unittest.main()
