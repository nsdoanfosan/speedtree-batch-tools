import copy
import json
import queue
import sys
import threading
import time
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path
from unittest import mock

import pytest


SK_BATCH_DIR = Path(__file__).resolve().parents[1]
REPO_DIR = SK_BATCH_DIR.parent
TOOLS_DIR = REPO_DIR / "tools"
FIXTURE = (
    Path(__file__).resolve().parent
    / "fixtures"
    / "issue137_retry_planning_benchmark.json"
)
sys.path.insert(0, str(SK_BATCH_DIR))
sys.path.insert(0, str(REPO_DIR))

from retry_planning import (  # noqa: E402
    RetryPlanningCancelled,
    RetryPlanningContext,
    RetryPlanningSnapshotError,
    StableJsonCache,
    cheap_durable_candidate,
)
from retry_progress import RetryProgressReceipt  # noqa: E402


def load_source(name, path):
    loader = SourceFileLoader(name, str(path))
    spec = spec_from_loader(loader.name, loader)
    module = module_from_spec(spec)
    loader.exec_module(module)
    return module


def load_gui_module():
    return load_source("issue137_retry_planning_gui_test", SK_BATCH_DIR / "sk_batch_gui.pyw")


def load_benchmark_module():
    return load_source(
        "issue137_retry_planning_benchmark_test",
        TOOLS_DIR / "benchmark_issue137_retry_planning.py",
    )


def test_cheap_candidate_requires_current_live_identity_and_fails_closed():
    current = {"push_status_kind": "imported_ok"}
    assert cheap_durable_candidate(
        current,
        live_identity_current=False,
    )[0] is True
    assert cheap_durable_candidate(
        current,
        live_identity_current=True,
    ) == (False, "durable_current_success")

    stale_signal = {
        "push_status_kind": "imported_ok",
        "blend_status_kind": "data_error",
        "blend_status_error": {
            "kind": "data_error",
            "message": "structured stale signal",
        },
    }
    assert cheap_durable_candidate(
        stale_signal,
        live_identity_current=True,
    )[0] is True
    assert cheap_durable_candidate({}, live_identity_current=True)[0] is True


def test_stable_json_cache_dedupes_by_path_stat_and_content_identity(tmp_path):
    path = tmp_path / "shared.json"
    path.write_text('{"version":1}', encoding="utf-8")
    counters = {}

    class Counts(dict):
        def __missing__(self, key):
            return 0

    counters = Counts()
    cache = StableJsonCache(counters)
    first = cache.load(path, namespace="parent_manifest")
    second = cache.load(path, namespace="parent_manifest")
    assert first is second
    assert counters["json_parses"] == 1
    assert counters["file_reads"] == 1
    assert counters["parent_manifest_cache_hits"] == 1

    path.write_text('{"version":2,"changed":true}', encoding="utf-8")
    third = cache.load(path, namespace="parent_manifest")
    assert third["version"] == 2
    assert counters["json_parses"] == 2
    assert counters["parent_manifest_cache_misses"] == 2


def test_state_snapshot_wait_is_bounded_and_truthful(tmp_path):
    lock = threading.Lock()
    lock.acquire()
    try:
        with pytest.raises(RetryPlanningSnapshotError, match="bounded timeout"):
            RetryPlanningContext.capture(
                target_ids=[str(tmp_path / "SK_wait.spm")],
                state={},
                state_lock=lock,
                cfg_snapshot={},
                inventory_snapshot={},
                wait_seconds=0.06,
            )
    finally:
        lock.release()


def test_sanitized_154_target_benchmark_preserves_classification_and_dedupes_io(
    tmp_path,
):
    benchmark = load_benchmark_module()
    gui = benchmark.load_gui_module()
    descriptor = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert descriptor["target_count"] == 154
    assert sum(descriptor["categories"].values()) == 154
    result = benchmark.run_once(
        gui,
        tmp_path / "sanitized",
        descriptor,
        label="regression",
    )

    assert result["classification_signature_sha256"] == (
        "1f272cf0a48da46e5e9dc96968e434440c6311472d30fa84f1912125396aed54"
    )
    assert result["partition_counts"] == {
        "unreal_ingest": 10,
        "blender_export": 34,
    }
    counters = result["counters"]
    assert counters["repair_state_calls"] == 54
    assert counters["spm_contract_parses"] == 54
    assert counters["parent_manifest_reads"] == 1
    assert counters["parent_checkpoint_reads"] == 1
    assert counters["file_reads"] < 338
    assert counters["bytes_read"] < 10_554_674
    assert counters["json_parses"] < 184

    planner = result["planner_diagnostics"]["counters"]
    assert planner["durable_current_excluded"] == 100
    assert planner["repair_state_validations"] == 54
    assert planner["durable_report_cache_misses"] == 2
    assert planner["durable_report_cache_hits"] == 8
    assert planner["parent_manifest_cache_misses"] == 1
    assert planner["parent_manifest_cache_hits"] == 9
    assert planner["parent_checkpoint_cache_misses"] == 1
    assert planner["parent_checkpoint_cache_hits"] == 9
    assert planner["parent_manifest_validations"] == 1
    assert planner["parent_manifest_validation_cache_hits"] == 9


def test_slow_planning_advances_real_receipt_and_cancels_between_units(tmp_path):
    gui = load_gui_module()
    app = gui.App.__new__(gui.App)
    app.stop_flag = threading.Event()
    app.ui_queue = queue.Queue()
    app.state_lock = threading.RLock()
    app._retry_thread_context = threading.local()
    app.log = mock.Mock()
    targets = [str(tmp_path / f"SK_slow_{index:02d}.spm") for index in range(8)]
    for target in targets:
        Path(target).write_bytes(b"sanitized")
    app.state = {
        target: {
            "push_status_kind": "data_error",
            "push_status_error": {
                "kind": "data_error",
                "message": "sanitized slow failure",
            },
        }
        for target in targets
    }
    inventory = {
        target: {"spm": Path(target), "checked": index % 2 == 0}
        for index, target in enumerate(targets)
    }
    app._snapshot_batch_request = mock.Mock(
        return_value=(copy.deepcopy(inventory), list(copy.deepcopy(inventory).values()))
    )
    entered = threading.Event()
    calls = []

    def slow_state(iid):
        calls.append(iid)
        entered.set()
        time.sleep(0.025)
        return {
            "current": False,
            "push_ready": False,
            "kind": "stale_content",
            "reason": "sanitized slow authoritative validation",
        }

    app._failed_retry_repair_state = mock.Mock(side_effect=slow_state)
    app._set_failed_retry_automatic_status = mock.Mock()
    tracker = RetryProgressReceipt.create(
        targets,
        receipt_dir=tmp_path / "receipts",
        stall_warning_seconds=5,
        owner_lost_seconds=5,
    )
    failures = []

    def plan():
        try:
            with mock.patch.object(
                gui,
                "has_repair_contract_evidence",
                return_value=False,
            ):
                app._build_failed_retry_plan(
                    targets,
                    {"push_transport": "headless"},
                    tracker=tracker,
                    inventory_snapshot=inventory,
                )
        except Exception as exc:
            failures.append(exc)

    worker = threading.Thread(target=plan, daemon=True)
    worker.start()
    assert entered.wait(1)
    deadline = time.monotonic() + 1
    while len(calls) < 2 and time.monotonic() < deadline:
        time.sleep(0.005)
    before_cancel = tracker.snapshot(evaluate=False)
    current = next(
        row
        for row in before_cancel["targets"]
        if row["target_id"] == before_cancel["current_target_id"]
    )
    assert "scanned" in current["latest_diagnostic"]
    assert current["last_heartbeat_at"] is not None
    assert len(calls) >= 2

    app.stop_flag.set()
    worker.join(2)
    assert not worker.is_alive()
    assert len(calls) < len(targets)
    assert len(failures) == 1
    assert isinstance(failures[0], RetryPlanningCancelled)


def test_planner_scope_never_touches_production_drive_or_launches_dcc():
    source = (SK_BATCH_DIR / "retry_planning.py").read_text(encoding="utf-8")
    benchmark = (TOOLS_DIR / "benchmark_issue137_retry_planning.py").read_text(
        encoding="utf-8"
    )
    combined = source + benchmark
    assert "D:\\" not in combined
    assert "subprocess" not in combined
    assert ".bat" not in combined.casefold()
