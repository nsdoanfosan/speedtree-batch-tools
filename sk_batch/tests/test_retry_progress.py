import ast
import json
import subprocess
import sys
import time
from pathlib import Path


REPO_DIR = Path(__file__).resolve().parents[2]
SK_BATCH_DIR = REPO_DIR / "sk_batch"
for value in (str(REPO_DIR), str(SK_BATCH_DIR)):
    if value not in sys.path:
        sys.path.insert(0, value)

from process_lifecycle import ProcessSupervisor
from retry_progress import (
    BLENDER,
    BLOCKED,
    CANCELLED,
    CLAIMED,
    COMPLETE,
    FAILED,
    OWNER_LOST,
    PENDING_UNREAL,
    PLANNING,
    POST_CHECK,
    SEND2UE,
    SHARED_QUEUE_WAIT,
    STALLED,
    UNREAL,
    RetryProgressReceipt,
    stage_for_send2ue_marker,
)
from shared_job_queue import SharedJobQueue


HELPER = Path(__file__).with_name("retry_progress_helper.py")
GUI_SOURCE = SK_BATCH_DIR / "sk_batch_gui.pyw"


class FakeClock:
    def __init__(self, value=1000.0):
        self.value = float(value)

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += float(seconds)


def _target(snapshot, target_id):
    return next(
        row for row in snapshot["targets"] if row["target_id"] == target_id
    )


def test_planning_queue_claim_stage_ages_and_bounded_diagnostic_are_durable(
    tmp_path,
):
    clock = FakeClock()
    first = str(tmp_path / "SK_tree.spm")
    second = str(tmp_path / "SK_grass.spm")
    tracker = RetryProgressReceipt.create(
        [first, second, first],
        receipt_dir=tmp_path / "receipts",
        clock=clock,
        stall_warning_seconds=30,
        owner_lost_seconds=45,
    )
    initial = tracker.snapshot()
    assert len(initial["targets"]) == 2
    assert all(row["stage"] == PLANNING for row in initial["targets"])
    tracker.assign_partition(
        "blender_export", [first], "blender_send2ue_then_unreal"
    )
    tracker.assign_partition(
        "unreal_ingest", [second], "immutable_unreal_only"
    )
    tracker.register_queue_job("blender_export", "job-a", 7, local_job_id=1)
    tracker.queue_wait(
        "blender_export",
        position=2,
        queued_count=1,
        running_head={
            "lease": {
                "owner_id": "other-window:host:run",
                "hostname": "host",
                "pid": 404,
                "heartbeat_at": clock(),
            }
        },
    )
    waiting = _target(tracker.snapshot(), first)
    assert waiting["stage"] == SHARED_QUEUE_WAIT
    assert "other-window:host:run" in waiting["latest_diagnostic"]

    tracker.claimed(
        "blender_export",
        {
            "lease": {
                "owner_id": "sk_batch:host:ours",
                "hostname": "host",
                "pid": 505,
                "heartbeat_at": clock(),
            }
        },
    )
    assert _target(tracker.snapshot(), first)["stage"] == CLAIMED
    tracker.transition(
        first,
        BLENDER,
        diagnostic="B" * 1000 + "\nnot-a-second-line",
        progress=True,
        heartbeat=True,
    )
    clock.advance(8)
    tracker.observe_process(
        first,
        stage=SEND2UE,
        diagnostic="SK_BATCH_SEND2UE_DISK_EXPORT_START",
        output=True,
        progress=True,
    )
    clock.advance(3)
    live = _target(tracker.snapshot(), first)
    assert live["stage"] == SEND2UE
    assert live["elapsed_seconds"] == 11
    assert live["last_progress_age_seconds"] == 3
    assert live["last_output_age_seconds"] == 3
    assert live["last_heartbeat_age_seconds"] == 3
    assert "\n" not in live["latest_diagnostic"]
    assert len(live["latest_diagnostic"]) <= 240

    tracker.transition(first, POST_CHECK, diagnostic="post-check", progress=True)
    tracker.transition(
        first,
        COMPLETE,
        diagnostic="done",
        terminal_reason="completed",
        outcome=COMPLETE,
    )
    tracker.transition(
        second,
        BLOCKED,
        diagnostic="dependency block",
        terminal_reason="dependency_blocked",
        outcome=BLOCKED,
    )
    tracker.finalize()

    reopened = RetryProgressReceipt.load_latest(
        tmp_path / "receipts", clock=clock
    )
    assert reopened is not None
    restored = reopened.snapshot()
    assert _target(restored, first)["stage"] == COMPLETE
    assert _target(restored, second)["stage"] == BLOCKED
    assert restored["terminal_reason"] == "blocked"
    assert json.loads(tracker.path.read_text(encoding="utf-8"))["run_id"] == (
        tracker.run_id
    )


def test_send2ue_marker_stage_contract():
    assert stage_for_send2ue_marker(
        "SK_BATCH_SEND2UE_DISK_EXPORT_START unit=Tree", BLENDER
    ) == SEND2UE
    assert stage_for_send2ue_marker(
        "SK_BATCH_SEND2UE_RPC_OWNED_START phase=manifest_ingest", SEND2UE
    ) == UNREAL
    assert stage_for_send2ue_marker(
        "SK_BATCH_SEND2UE_RPC_OWNED_DONE phase=manifest_ingest", UNREAL
    ) == POST_CHECK


def test_retry_planner_worker_contract_contains_no_tk_operation():
    tree = ast.parse(GUI_SOURCE.read_text(encoding="utf-8"))
    build = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "_build_failed_retry_plan"
    )
    attributes = {
        node.attr for node in ast.walk(build) if isinstance(node, ast.Attribute)
    }
    names = {
        node.id for node in ast.walk(build) if isinstance(node, ast.Name)
    }
    assert names.isdisjoint({"messagebox", "save_config"})
    assert attributes.isdisjoint(
        {
            "root",
            "after",
            "messagebox",
            "progress_var",
            "retry_target_var",
            "retry_liveness_var",
            "retry_diagnostic_var",
            "_collect_cfg",
            "_enqueue_batch_job",
            "_set_batch_queue_controls",
        }
    )


def test_owner_lost_queue_reconciliation_preserves_completed_targets(tmp_path):
    clock = FakeClock()
    alive = {"value": True}
    queue = SharedJobQueue(
        tmp_path / "queue.json",
        lease_seconds=1,
        clock=clock,
        process_alive=lambda _host, _pid, _marker: alive["value"],
    )
    first = str(tmp_path / "done.spm")
    second = str(tmp_path / "lost.spm")
    tracker = RetryProgressReceipt.create(
        [first, second], receipt_dir=tmp_path / "receipts", clock=clock
    )
    tracker.assign_partition("already_done", [first], "immutable_unreal_only")
    tracker.assign_partition("lost_partition", [second], "immutable_unreal_only")
    tracker.transition(
        first,
        COMPLETE,
        diagnostic="done before owner loss",
        terminal_reason="completed",
        outcome=COMPLETE,
    )
    job = queue.enqueue("sk_batch", {"sanitized": True})
    tracker.register_queue_job("lost_partition", job["id"], job["sequence"])
    claimed = queue.claim("test-owner", job_id=job["id"])
    tracker.claimed("lost_partition", claimed)
    running = queue.snapshot()
    assert next(row for row in running["jobs"] if row["id"] == job["id"])[
        "status"
    ] == "running"
    assert tracker.reconcile_queue(running) is False
    tracker.record_operator_close("operator closed the SK Batch window")

    alive["value"] = False
    clock.advance(2)
    recovered = queue.snapshot()
    assert next(row for row in recovered["jobs"] if row["id"] == job["id"])[
        "failure_reason"
    ] == OWNER_LOST
    assert tracker.reconcile_queue(recovered) is True
    snapshot = tracker.snapshot()
    assert _target(snapshot, first)["stage"] == COMPLETE
    assert _target(snapshot, second)["stage"] == OWNER_LOST
    assert snapshot["terminal_reason"] == "owner_lost"
    events = snapshot["lifecycle_events"]
    close_index = next(
        index
        for index, event in enumerate(events)
        if event["kind"] == "operator_app_close"
    )
    owner_lost = next(
        event for event in events if event["kind"] == "owner_lost_reconciled"
    )
    assert owner_lost["after_operator_app_close"] is True
    assert events.index(owner_lost) > close_index
    reopened = RetryProgressReceipt.load_latest(
        tmp_path / "receipts", clock=clock
    )
    assert [event["kind"] for event in reopened.snapshot()["lifecycle_events"]][-2:] == [
        "operator_app_close",
        "owner_lost_reconciled",
    ]


def test_restart_reconstructs_exact_per_target_queue_result_without_duplicates(
    tmp_path,
):
    first = str(tmp_path / "first.spm")
    second = str(tmp_path / "second.spm")
    tracker = RetryProgressReceipt.create(
        [first, second, first], receipt_dir=tmp_path / "receipts"
    )
    tracker.assign_partition(
        "unreal_ingest", [first, second], "immutable_unreal_only"
    )
    tracker.register_queue_job("unreal_ingest", "exact-job", 11)
    snapshot = {
        "jobs": [
            {
                "id": "exact-job",
                "status": "failed",
                "result": {
                    "outcome": "partial",
                    "target_outcomes": [
                        {"target": first, "outcome": "completed"},
                        {
                            "target": second,
                            "outcome": "blocked",
                            "reason_token": "dependency_blocked",
                        },
                    ],
                },
            }
        ]
    }
    assert tracker.reconcile_queue(snapshot) is True
    restored = RetryProgressReceipt.load_latest(tmp_path / "receipts")
    rows = restored.snapshot()["targets"]
    assert len(rows) == 2
    assert _target({"targets": rows}, first)["stage"] == COMPLETE
    assert _target({"targets": rows}, second)["stage"] == BLOCKED


def test_restart_reconciles_queue_commit_before_receipt_completion(tmp_path):
    target = str(tmp_path / "committed.spm")
    tracker = RetryProgressReceipt.create(
        [target], receipt_dir=tmp_path / "receipts"
    )
    tracker.assign_partition("unreal_ingest", [target], "immutable_unreal_only")
    tracker.register_queue_job("unreal_ingest", "committed-job", 12)

    # Simulate process loss after the validated target state committed but
    # before its local receipt transition. Restart learns only from this exact
    # queue job; it has no mechanism to enqueue/replay the target itself.
    queue_snapshot = {
        "jobs": [
            {
                "id": "committed-job",
                "status": "completed",
                "result": {
                    "outcome": "completed",
                    "target_outcomes": [
                        {"target": target, "outcome": "completed"}
                    ],
                },
            }
        ]
    }
    assert tracker.reconcile_queue(queue_snapshot) is True
    restored = RetryProgressReceipt.load_latest(tmp_path / "receipts")
    snapshot = restored.snapshot()
    assert _target(snapshot, target)["stage"] == COMPLETE
    assert snapshot["terminal_outcome"] == COMPLETE


def test_restart_keeps_success_wait_and_cancel_distinct(tmp_path):
    completed = str(tmp_path / "completed.spm")
    pending = str(tmp_path / "pending.spm")
    cancelled = str(tmp_path / "cancelled.spm")
    tracker = RetryProgressReceipt.create(
        [completed, pending, cancelled],
        receipt_dir=tmp_path / "receipts",
    )
    tracker.assign_partition(
        "unreal_ingest",
        [completed, pending, cancelled],
        "immutable_unreal_only",
    )
    tracker.register_queue_job("unreal_ingest", "terminal-semantics", 13)
    queue_snapshot = {
        "jobs": [
            {
                "id": "terminal-semantics",
                "status": "completed",
                "result": {
                    "outcome": "waiting",
                    "failed_count": 0,
                    "target_outcomes": [
                        {"target": completed, "outcome": "completed"},
                        {"target": pending, "outcome": "pending_unreal"},
                        {"target": cancelled, "outcome": "cancelled"},
                    ],
                },
            }
        ]
    }

    assert tracker.reconcile_queue(queue_snapshot) is True
    restored = tracker.snapshot(evaluate=False)
    assert _target(restored, completed)["stage"] == COMPLETE
    assert _target(restored, pending)["stage"] == PENDING_UNREAL
    assert _target(restored, cancelled)["stage"] == CANCELLED
    assert restored["run_state"] == "waiting"
    assert restored["terminal_outcome"] is None


def test_failed_stopped_queue_shell_does_not_override_completed_targets(tmp_path):
    targets = [str(tmp_path / f"completed_{index}.spm") for index in range(29)]
    tracker = RetryProgressReceipt.create(
        targets,
        receipt_dir=tmp_path / "receipts",
    )
    tracker.assign_partition("blender_export", targets, "full_pipeline")
    tracker.register_queue_job("blender_export", "sequence-83", 83)
    queue_snapshot = {
        "jobs": [
            {
                "id": "sequence-83",
                "status": "failed",
                "result": {
                    "outcome": "stopped",
                    "completed_count": 29,
                    "failed_count": 0,
                    "blocked_count": 0,
                    "target_outcomes": [
                        {"target": target, "outcome": "completed"}
                        for target in targets
                    ],
                },
            }
        ]
    }

    assert tracker.reconcile_queue(queue_snapshot) is True
    restored = tracker.snapshot(evaluate=False)
    assert all(row["stage"] == COMPLETE for row in restored["targets"])
    assert restored["run_state"] == "terminal"
    assert restored["terminal_outcome"] == COMPLETE


def test_push_state_commits_before_retry_receipt_transitions():
    tree = ast.parse(GUI_SOURCE.read_text(encoding="utf-8"))
    push_state = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "_set_push_state"
    )
    save_lines = [
        node.lineno
        for node in ast.walk(push_state)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "save_state"
    ]
    receipt_lines = [
        node.lineno
        for node in ast.walk(push_state)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "_retry_transition"
    ]
    assert save_lines
    assert receipt_lines
    assert max(save_lines) < min(receipt_lines)


def test_slow_silent_and_hung_helpers_have_distinct_liveness_and_safe_cancel(
    tmp_path,
):
    supervisor = ProcessSupervisor(
        "test:retry-progress", receipt_dir=tmp_path / "process_receipts"
    )
    external = None
    try:
        slow_id = str(tmp_path / "slow.spm")
        slow = RetryProgressReceipt.create(
            [slow_id],
            receipt_dir=tmp_path / "slow_receipts",
            stall_warning_seconds=0.5,
            owner_lost_seconds=1.0,
        )
        slow.assign_partition("blender_export", [slow_id], "test")
        slow.transition(
            slow_id, BLENDER, diagnostic="slow helper", progress=True, heartbeat=True
        )
        proc = supervisor.spawn_owned(
            [
                sys.executable,
                str(HELPER),
                "slow",
                "--duration",
                "0.22",
                "--interval",
                "0.04",
            ],
            source="test:retry-progress:slow",
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        for line in proc.stdout:
            slow.observe_process(
                slow_id,
                stage=BLENDER,
                diagnostic=line,
                output=True,
                progress=True,
            )
        assert proc.wait(timeout=3) == 0
        supervisor.complete_owned(proc)
        slow_live = _target(slow.snapshot(), slow_id)
        assert slow_live["stage"] == BLENDER
        assert slow_live["last_output_age_seconds"] is not None

        silent_id = str(tmp_path / "silent.spm")
        silent = RetryProgressReceipt.create(
            [silent_id],
            receipt_dir=tmp_path / "silent_receipts",
            stall_warning_seconds=0.5,
            owner_lost_seconds=1.0,
        )
        silent.assign_partition("blender_export", [silent_id], "test")
        silent.transition(
            silent_id,
            BLENDER,
            diagnostic="silent helper started",
            progress=True,
            heartbeat=True,
        )
        proc = supervisor.spawn_owned(
            [sys.executable, str(HELPER), "silent", "--duration", "0.22"],
            source="test:retry-progress:silent",
        )
        while proc.poll() is None:
            silent.observe_process(silent_id, stage=BLENDER)
            time.sleep(0.02)
        supervisor.complete_owned(proc)
        silent_live = _target(silent.snapshot(), silent_id)
        assert silent_live["stage"] == BLENDER
        assert silent_live["last_output_age_seconds"] is None
        assert silent_live["last_heartbeat_age_seconds"] < 0.2

        failed_id = str(tmp_path / "failed.spm")
        failed = RetryProgressReceipt.create(
            [failed_id], receipt_dir=tmp_path / "failed_receipts"
        )
        failed.assign_partition("blender_export", [failed_id], "test")
        proc = supervisor.spawn_owned(
            [sys.executable, str(HELPER), "fail"],
            source="test:retry-progress:nonzero",
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        output = proc.communicate(timeout=3)[0]
        supervisor.complete_owned(proc)
        assert proc.returncode == 7
        failed.transition(
            failed_id,
            FAILED,
            diagnostic=output,
            terminal_reason="process_nonzero_exit:7",
            outcome=FAILED,
        )
        failed_row = _target(failed.snapshot(), failed_id)
        assert failed_row["stage"] == FAILED
        assert failed_row["terminal_reason"] == "process_nonzero_exit:7"

        hung_id = str(tmp_path / "hung.spm")
        hung = RetryProgressReceipt.create(
            [hung_id],
            receipt_dir=tmp_path / "hung_receipts",
            stall_warning_seconds=0.12,
            owner_lost_seconds=1.0,
        )
        hung.assign_partition("blender_export", [hung_id], "test")
        hung.transition(
            hung_id,
            BLENDER,
            diagnostic="hung helper started",
            progress=True,
            heartbeat=True,
        )
        proc = supervisor.spawn_owned(
            [sys.executable, str(HELPER), "hung"],
            source="test:retry-progress:hung",
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        assert proc.stdout.readline().startswith("CHILD_PID=")
        external = subprocess.Popen(
            [sys.executable, str(HELPER), "silent", "--duration", "0.8"]
        )
        deadline = time.monotonic() + 0.35
        while time.monotonic() < deadline:
            hung.observe_process(hung_id, stage=BLENDER)
            time.sleep(0.03)
        stalled = _target(hung.snapshot(), hung_id)
        assert stalled["stage"] == STALLED
        assert "safe cancel available" in stalled["latest_diagnostic"]
        assert proc.poll() is None

        supervisor.terminate_owned(
            proc,
            reason="operator_cancel",
            terminate_grace=0.05,
            kill_grace=2.0,
        )
        hung.transition(
            hung_id,
            CANCELLED,
            diagnostic="operator cancelled exact owned tree",
            terminal_reason="operator_cancelled",
            outcome=CANCELLED,
        )
        assert proc.poll() is not None
        assert external.poll() is None
        assert _target(hung.snapshot(), hung_id)["stage"] == CANCELLED
        entry = supervisor.entry_for(proc)
        assert entry["cleanup_state"] == "process_tree_clean"
    finally:
        if external is not None and external.poll() is None:
            external.terminate()
            external.wait(timeout=3)
        supervisor.shutdown(
            reason="test_complete", terminate_grace=0.05, kill_grace=2.0
        )
