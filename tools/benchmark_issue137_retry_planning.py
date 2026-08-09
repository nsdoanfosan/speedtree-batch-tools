"""Sanitized 154-target benchmark for Issue #137 retry planning.

The fixture creates only temporary synthetic files.  It deliberately exercises
the production planner's complete-inventory loop, repeated durable report JSON
loads, and repeated shared parent manifest/checkpoint loads.  It never invokes
a DCC executable or BAT file and never discovers production asset roots.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import queue
import sys
import tempfile
import threading
import time
import tracemalloc
from collections import Counter
from contextlib import ExitStack
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
SK_BATCH_DIR = REPO_ROOT / "sk_batch"
FIXTURE_PATH = (
    SK_BATCH_DIR
    / "tests"
    / "fixtures"
    / "issue137_retry_planning_benchmark.json"
)
sys.path.insert(0, str(SK_BATCH_DIR))
sys.path.insert(0, str(REPO_ROOT))


def load_gui_module():
    path = SK_BATCH_DIR / "sk_batch_gui.pyw"
    loader = SourceFileLoader("issue137_retry_planning_benchmark", str(path))
    spec = spec_from_loader(loader.name, loader)
    module = module_from_spec(spec)
    loader.exec_module(module)
    return module


def _payload_of_size(size):
    prefix = {"schema_version": 1, "sanitized": True, "padding": ""}
    encoded = json.dumps(prefix, separators=(",", ":"))
    prefix["padding"] = "x" * max(0, int(size) - len(encoded))
    return prefix


def _write_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )


def build_fixture(gui, root, descriptor):
    categories = descriptor["categories"]
    report_paths = []
    for index in range(int(descriptor["shared_report_count"])):
        path = root / "reports" / f"shared_failure_{index}.json"
        _write_json(path, _payload_of_size(descriptor["large_report_bytes"]))
        report_paths.append(path)

    rows = []
    state = {}
    category_by_id = {}
    ordinal = 0

    def add(category, count):
        nonlocal ordinal
        for _ in range(int(count)):
            ordinal += 1
            spm = root / "inventory" / f"SK_sanitized_{ordinal:03d}.spm"
            spm.parent.mkdir(parents=True, exist_ok=True)
            spm.write_bytes((f"sanitized-spm-{ordinal}\n" * 4).encode("ascii"))
            iid = str(spm)
            rows.append(iid)
            category_by_id[iid] = category
            state[iid] = {}

    for category, count in categories.items():
        add(category, count)

    repair_reports = {}
    for iid in rows:
        report = root / "repair" / (Path(iid).stem + ".json")
        _write_json(report, {"schema_version": 1, "target": Path(iid).name})
        repair_reports[iid] = report
        category = category_by_id[iid]
        if category == "current":
            state[iid] = {
                "push_status_kind": "imported_ok",
                "live_status_signature": ["sanitized-current", Path(iid).name],
            }
        elif category == "stale_repair":
            state[iid] = {
                "push_status_kind": "imported_ok",
                "blend_status_kind": "data_error",
                "blend_status_error": {
                    "kind": "data_error",
                    "message": "sanitized stale Repair output",
                },
            }
        elif category == "send2ue_failure":
            report = report_paths[ordinal % len(report_paths)]
            ordinal += 1
            state[iid] = {
                "push_status_kind": "data_error",
                "push_status_error": {
                    "kind": "data_error",
                    "message": "sanitized Send2UE failure",
                    "report": str(report),
                },
                "push_paths": {"report": str(report)},
            }

    unreal_ids = [
        iid
        for iid in rows
        if category_by_id[iid] == "shared_parent_unreal_failure"
    ]
    manifest_path = root / "parent" / "shared_manifest.json"
    checkpoint_path = root / "parent" / "shared_checkpoint.json"
    manifest_items = []
    for index, iid in enumerate(unreal_ids):
        manifest_items.append(
            {
                "schema_version": gui.PUSH_MANIFEST_SCHEMA_VERSION,
                "queue_id": iid,
                "source_fingerprint": f"source-{index:03d}",
                "fingerprint": f"item-{index:03d}",
                "blend": str(root / "blend" / f"source_{index:03d}.blend"),
                "depends_on_queue_ids": [],
            }
        )
        state[iid] = {
            "push_status_kind": "data_error",
            "push_status_error": {
                "kind": "data_error",
                "message": "sanitized Unreal ingest failure",
            },
            "push_paths": {
                "manifest": str(manifest_path),
                "checkpoint": str(checkpoint_path),
            },
        }
    _write_json(
        manifest_path,
        {
            "schema_version": gui.PUSH_MANIFEST_SCHEMA_VERSION,
            "report_path": str(root / "parent" / "shared_report.json"),
            "items": manifest_items,
        },
    )
    _write_json(
        checkpoint_path,
        {"items": {iid: {"status": "data_error"} for iid in unreal_ids}},
    )
    return {
        "rows": rows,
        "state": state,
        "category_by_id": category_by_id,
        "repair_reports": repair_reports,
        "manifest_path": manifest_path,
        "checkpoint_path": checkpoint_path,
    }


def make_app(gui, fixture, counters):
    app = gui.App.__new__(gui.App)
    app.stop_flag = threading.Event()
    app.ui_queue = queue.Queue()
    app.state_lock = threading.RLock()
    app.state = copy.deepcopy(fixture["state"])
    app.items = {
        iid: {
            "spm": Path(iid),
            "checked": index % 2 == 0,
        }
        for index, iid in enumerate(fixture["rows"])
    }
    app.log = mock.Mock()
    app._set_failed_retry_automatic_status = mock.Mock()
    app._snapshot_batch_request = mock.Mock(
        side_effect=lambda ids: (
            copy.deepcopy(app.items),
            [copy.deepcopy(app.items[iid]) for iid in ids if iid in app.items],
        )
    )
    app._failed_retry_parent_source_record = mock.Mock(
        side_effect=lambda _iid, item: {
            "fingerprint": item["source_fingerprint"],
            "snapshot": {},
        }
    )
    app._validate_failed_retry_unreal_item_current = mock.Mock(return_value={})
    app._live_status_signature = mock.Mock(
        side_effect=lambda spm, _texture_paths=(): (
            "sanitized-current",
            Path(spm).name,
        )
    )

    def authoritative_repair_state(iid):
        started = time.perf_counter()
        counters["repair_state_calls"] += 1
        counters["spm_contract_parses"] += 1
        Path(iid).read_bytes()
        json.loads(fixture["repair_reports"][iid].read_text(encoding="utf-8"))
        category = fixture["category_by_id"][iid]
        if category == "stale_repair":
            result = {
                "current": False,
                "push_ready": False,
                "kind": "stale_content",
                "reason": "sanitized current SPM differs from Repair output",
            }
        elif category == "ambiguous":
            result = {
                "current": False,
                "push_ready": False,
                "kind": "inspection_incomplete",
                "reason": "sanitized evidence intentionally incomplete",
            }
        else:
            result = {
                "current": True,
                "push_ready": True,
                "kind": "ready",
                "reason": "sanitized authoritative current contract",
            }
        counters["repair_state_ns"] += int(
            (time.perf_counter() - started) * 1_000_000_000
        )
        return result

    app._failed_retry_repair_state = authoritative_repair_state
    return app


def _classification_signature(plan):
    def target_name(value):
        return Path(str(value)).name

    partitions = []
    for job in plan.get("jobs") or []:
        metadata = job.get("retry_metadata") or {}
        eligibility = copy.deepcopy(metadata.get("eligibility") or {})
        for item in eligibility.get("items") or ():
            if item.get("queue_id"):
                item["queue_id"] = target_name(item["queue_id"])
        partitions.append(
            {
                "partition": metadata.get("partition"),
                "execution_path": metadata.get("execution_path"),
                "selected_queue_ids": [
                    target_name(value)
                    for value in metadata.get("selected_queue_ids") or ()
                ],
                "eligibility": eligibility,
            }
        )
    return {
        "partitions": partitions,
        "skipped": list(plan.get("skipped") or ()),
    }


def run_once(gui, root, descriptor, *, label):
    fixture = build_fixture(gui, root, descriptor)
    counters = Counter()
    app = make_app(gui, fixture, counters)
    original_durable_evidence = app._failed_retry_durable_evidence
    original_parent_loader = gui.load_unreal_recovery_parent_manifest
    original_read_text = Path.read_text
    original_read_bytes = Path.read_bytes
    original_json_loads = json.loads

    def count_named_read(path):
        normalized = os.path.normcase(str(Path(path).resolve()))
        if normalized == os.path.normcase(str(fixture["manifest_path"].resolve())):
            counters["parent_manifest_reads"] += 1
        if normalized == os.path.normcase(str(fixture["checkpoint_path"].resolve())):
            counters["parent_checkpoint_reads"] += 1

    def measured_read_text(path, *args, **kwargs):
        value = original_read_text(path, *args, **kwargs)
        counters["file_reads"] += 1
        counters["bytes_read"] += len(value.encode(kwargs.get("encoding") or "utf-8"))
        count_named_read(path)
        return value

    def measured_read_bytes(path, *args, **kwargs):
        value = original_read_bytes(path, *args, **kwargs)
        counters["file_reads"] += 1
        counters["bytes_read"] += len(value)
        count_named_read(path)
        return value

    def measured_json_loads(value, *args, **kwargs):
        counters["json_parses"] += 1
        return original_json_loads(value, *args, **kwargs)

    def measured_durable_evidence(*args, **kwargs):
        started = time.perf_counter()
        try:
            return original_durable_evidence(*args, **kwargs)
        finally:
            counters["durable_evidence_calls"] += 1
            counters["durable_evidence_ns"] += int(
                (time.perf_counter() - started) * 1_000_000_000
            )

    def measured_parent_loader(*args, **kwargs):
        started = time.perf_counter()
        try:
            return original_parent_loader(*args, **kwargs)
        finally:
            counters["parent_manifest_load_calls"] += 1
            counters["parent_manifest_load_ns"] += int(
                (time.perf_counter() - started) * 1_000_000_000
            )

    app._failed_retry_durable_evidence = measured_durable_evidence

    tracemalloc.start()
    started = time.perf_counter()
    with ExitStack() as stack:
        stack.enter_context(mock.patch.object(Path, "read_text", measured_read_text))
        stack.enter_context(mock.patch.object(Path, "read_bytes", measured_read_bytes))
        stack.enter_context(mock.patch.object(json, "loads", measured_json_loads))
        stack.enter_context(
            mock.patch.object(
                gui,
                "load_unreal_recovery_parent_manifest",
                measured_parent_loader,
            )
        )
        stack.enter_context(
            mock.patch.object(gui, "has_repair_contract_evidence", return_value=False)
        )
        plan = app._build_failed_retry_plan(
            fixture["rows"],
            {"push_transport": "headless"},
        )
    wall = time.perf_counter() - started
    _current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    signature = _classification_signature(plan)
    signature_sha256 = hashlib.sha256(
        json.dumps(
            signature,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    partition_counts = {
        row["partition"]: len(row["selected_queue_ids"])
        for row in signature["partitions"]
    }
    return {
        "label": label,
        "wall_seconds": wall,
        "peak_memory_bytes": peak,
        "counters": dict(sorted(counters.items())),
        "spans_seconds": {
            "repair_state_validation": counters["repair_state_ns"] / 1e9,
            "durable_evidence_load": counters["durable_evidence_ns"] / 1e9,
            "parent_manifest_load": counters["parent_manifest_load_ns"] / 1e9,
            "unattributed_planner_work": max(
                0.0,
                wall
                - counters["repair_state_ns"] / 1e9
                - counters["durable_evidence_ns"] / 1e9
                - counters["parent_manifest_load_ns"] / 1e9,
            ),
        },
        "partition_counts": partition_counts,
        "classification_signature_sha256": signature_sha256,
        "planner_diagnostics": copy.deepcopy(
            plan.get("planning_diagnostics") or {}
        ),
        "classification_signature": signature,
    }


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    parser.add_argument("--runs", type=int, default=2)
    args = parser.parse_args(argv)
    descriptor = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    if sum(descriptor["categories"].values()) != descriptor["target_count"]:
        raise SystemExit("fixture category count does not equal target_count")
    gui = load_gui_module()
    results = []
    with tempfile.TemporaryDirectory(prefix="issue137_retry_benchmark_") as temp:
        root = Path(temp)
        for index in range(max(1, args.runs)):
            run_root = root / f"run_{index + 1}"
            results.append(
                run_once(
                    gui,
                    run_root,
                    descriptor,
                    label="cold" if index == 0 else f"warm_{index}",
                )
            )
    receipt = {
        "schema_version": 1,
        "issue": 137,
        "sanitized": True,
        "target_count": descriptor["target_count"],
        "fixture": descriptor,
        "results": results,
    }
    encoded = json.dumps(receipt, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded + "\n", encoding="utf-8")
    print(encoded)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
