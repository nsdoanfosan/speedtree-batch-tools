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
    build_plan_cache_artifact,
    cheap_durable_candidate,
    hydrate_plan_cache_artifact,
    planning_input_signature,
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
        "87699b9d06e1f56749d9d819fe83dd0464e8449933fec1b136d580a983681a38"
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
    assert planner["invalid_failure_records_reaudited"] == 54
    assert planner.get("durable_report_cache_misses", 0) == 0
    assert planner.get("durable_report_cache_hits", 0) == 0
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


def test_plan_receipt_reuses_unchanged_assets_and_invalidates_changed_asset(
    tmp_path,
):
    gui = load_gui_module()
    first = str(tmp_path / "SK_first.spm")
    second = str(tmp_path / "SK_second.spm")
    Path(first).write_bytes(b"stable-first")
    Path(second).write_bytes(b"stable-second")
    targets = [first, second]
    state = {
        first: {"push_status_kind": "data_error"},
        second: {"push_status_kind": "imported_ok"},
    }
    inventory = {
        first: {"spm": Path(first), "checked": True},
        second: {"spm": Path(second), "checked": False},
    }
    cfg = {
        "push_transport": "headless",
        "_planning_production_source_revision": (
            gui._PROCESS_PRODUCTION_SOURCE_MANIFEST.content_hash
        ),
    }
    signature = planning_input_signature(targets, state, cfg, inventory)

    old_tracker = RetryProgressReceipt.create(
        targets, receipt_dir=tmp_path / "old-receipts"
    )
    old_tracker.start_planning({
        "owner_id": "old-owner",
        "hostname": "test",
        "pid": 123,
        "planning_session_id": "old-session",
    })
    old_tracker.assign_partition("blender_export", [first], "pipeline")
    old_tracker.transition(
        second,
        "blocked",
        diagnostic="current success",
        terminal_reason="current_success",
        outcome="blocked",
    )
    old_plan = {
        "jobs": [{
            "label": "cached pipeline",
            "mode": "pipeline",
            "phase": "push",
            "terminal_phase": "push",
            "selected_scope": True,
            "targets": [inventory[first]],
            "inventory": inventory,
            "cfg": cfg,
            "force_rerun": True,
            "push_transport": "headless",
            "resume_after_repairs": {
                second: [first],
            },
            "retry_metadata": {
                "partition": "blender_export",
                "execution_path": "pipeline",
                "progress_run_id": old_tracker.run_id,
            },
            "_retry_progress_tracker": old_tracker,
        }],
        "selected_iids": targets,
        "skipped": ["second is current"],
        "deferred_status_updates": [],
        "deferred_logs": [],
    }
    artifact = build_plan_cache_artifact(
        old_plan, old_tracker.snapshot(evaluate=False)
    )
    cache = {
        "schema_version": 1,
        "input_signature": signature,
        "artifact": artifact,
        "side_effects_committed": True,
    }

    app = gui.App.__new__(gui.App)
    app.stop_flag = threading.Event()
    app.state_lock = threading.RLock()
    app.state = copy.deepcopy(state)
    app._retry_thread_context = threading.local()
    app._build_failed_retry_plan_scoped = mock.Mock(
        side_effect=AssertionError("classification must not run on cache hit")
    )
    new_tracker = RetryProgressReceipt.create(
        targets, receipt_dir=tmp_path / "new-receipts"
    )
    new_tracker.start_planning({
        "owner_id": "new-owner",
        "hostname": "test",
        "pid": 456,
        "planning_session_id": "new-session",
    })
    reused = app._build_failed_retry_plan(
        targets,
        cfg,
        tracker=new_tracker,
        inventory_snapshot=inventory,
        planning_session_id="new-session",
        cached_plan_cache=cache,
    )

    assert reused["_planning_cache_reused"] is True
    assert app._build_failed_retry_plan_scoped.call_count == 0
    assert reused["jobs"][0]["targets"][0]["spm"] == Path(first)
    assert reused["jobs"][0]["resume_after_repairs"] == {
        second: [first],
    }
    metadata = reused["jobs"][0]["retry_metadata"]
    assert metadata["progress_run_id"] == new_tracker.run_id
    assert metadata["plan_cache_reused"] is True
    assert new_tracker.snapshot(evaluate=False)["planning"]["progress"][
        "cache_status"
    ] == "hit"

    time.sleep(0.002)
    Path(first).write_bytes(b"changed-first")
    changed = planning_input_signature(targets, state, cfg, inventory)
    assert changed["snapshot_sha256"] == signature["snapshot_sha256"]
    assert changed["file_identities_sha256"] != (
        signature["file_identities_sha256"]
    )
    old_revision = planning_input_signature(
        targets,
        state,
        {
            **cfg,
            "_planning_production_source_revision": "0" * 64,
        },
        inventory,
    )
    assert old_revision["snapshot_sha256"] != signature["snapshot_sha256"]


def test_plan_cache_hydration_skips_committed_deferred_side_effects(tmp_path):
    target = str(tmp_path / "SK_cached.spm")
    Path(target).write_bytes(b"cached")
    inventory = {target: {"spm": Path(target), "checked": True}}
    tracker = RetryProgressReceipt.create(
        [target], receipt_dir=tmp_path / "receipts"
    )
    tracker.start_planning({
        "owner_id": "owner",
        "hostname": "test",
        "pid": 123,
        "planning_session_id": "session",
    })
    artifact = {
        "schema_version": 1,
        "selected_iids": [target],
        "jobs": [{
            "target_ids": [target],
            "fields": {
                "label": "cached",
                "mode": "pipeline",
                "phase": "push",
                "terminal_phase": "push",
                "selected_scope": True,
                "force_rerun": True,
                "push_transport": "headless",
                "retry_metadata": {
                    "partition": "blender_export",
                    "execution_path": "pipeline",
                },
            },
        }],
        "terminal_results": [],
        "skipped": [],
        "deferred_status_updates": [{"iid": target, "status": "pending"}],
        "deferred_logs": ["old planning log"],
    }
    hydrated = hydrate_plan_cache_artifact(
        artifact,
        inventory_snapshot=inventory,
        cfg_snapshot={"push_transport": "headless"},
        tracker=tracker,
        side_effects_committed=True,
    )
    assert hydrated["deferred_status_updates"] == []
    assert hydrated["deferred_logs"] == []


def test_plan_signature_tracks_atlas_operational_mirrors(tmp_path):
    target = tmp_path / "SK_cluster_test.spm"
    target.write_bytes(b"spm")
    target_dir = tmp_path / ".atlas_leaf_speedtree_targets"
    target_dir.mkdir()
    manifest = target_dir / f"{target.stem}.json"
    manifest.write_text('{"generation":1}', encoding="utf-8")
    inventory = {str(target): {"spm": target}}

    before = planning_input_signature(
        [str(target)], {}, {}, inventory
    )
    manifest.write_text('{"generation":22}', encoding="utf-8")
    after = planning_input_signature(
        [str(target)], {}, {}, inventory
    )

    assert before["snapshot_sha256"] == after["snapshot_sha256"]
    assert before["file_identities_sha256"] != (
        after["file_identities_sha256"]
    )


def test_planner_scope_never_touches_production_drive_or_launches_dcc():
    source = (SK_BATCH_DIR / "retry_planning.py").read_text(encoding="utf-8")
    benchmark = (TOOLS_DIR / "benchmark_issue137_retry_planning.py").read_text(
        encoding="utf-8"
    )
    combined = source + benchmark
    assert "D:\\" not in combined
    assert "subprocess" not in combined
    assert ".bat" not in combined.casefold()
