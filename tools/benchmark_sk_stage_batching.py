"""Small first-run benchmark for SK Batch execution shapes.

The parent launches identical synthetic CPU/RAM work through the production
process supervisor.  No production asset is opened or regenerated.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from process_lifecycle import owned_run, shutdown_process_supervisor  # noqa: E402
from sk_batch.stage_batch_policy import policy_comparison  # noqa: E402


WORKLOADS = {
    "assembly": {"memory_mib": 12, "hash_rounds": 30},
    "blender_export": {"memory_mib": 8, "hash_rounds": 20},
    "unreal_ingest": {"memory_mib": 16, "hash_rounds": 40},
}


def _child_work(stage, units):
    workload = WORKLOADS[stage]
    size = int(workload["memory_mib"]) * 1024 * 1024
    payload = bytearray(size)
    view = memoryview(payload)
    for page in range(0, size, 4096):
        payload[page] = (page // 4096) & 0xFF
    digest = b""
    chunk = view[: min(size, 1024 * 1024)]
    for _unit in range(max(1, int(units))):
        for _round in range(int(workload["hash_rounds"])):
            digest = hashlib.blake2b(chunk, digest_size=32).digest()
            payload[-1] ^= digest[0]
    print(digest.hex())
    return 0


def _run_child(stage, units=1):
    started = time.perf_counter()
    completed = owned_run(
        [
            sys.executable,
            str(Path(__file__).resolve()),
            "--child-stage",
            stage,
            "--child-units",
            str(units),
        ],
        source=f"tools.benchmark_sk_stage_batching.{stage}",
        capture_output=True,
        check=True,
    )
    ended = time.perf_counter()
    return {
        "stage": stage,
        "units": int(units),
        "started": started,
        "ended": ended,
        "wall_seconds": ended - started,
        "resource_usage": completed.resource_usage or {},
    }


def _aggregate_peak_bytes(records):
    events = []
    for record in records:
        peak = int(
            (record.get("resource_usage") or {}).get(
                "peak_job_memory_bytes",
                0,
            )
        )
        events.append((float(record["started"]), 1, peak))
        events.append((float(record["ended"]), -1, peak))
    current = 0
    peak = 0
    # End events precede start events at an equal timestamp.
    for _timestamp, direction, value in sorted(events, key=lambda row: (row[0], row[1])):
        current += direction * value
        peak = max(peak, current)
    return peak


def _summarize(name, started, ended, records, launches):
    usage_rows = [record.get("resource_usage") or {} for record in records]
    return {
        "name": name,
        "wall_seconds": round(ended - started, 6),
        "process_launches": int(launches),
        "aggregate_peak_job_memory_bytes": _aggregate_peak_bytes(records),
        "summed_user_cpu_seconds": round(
            sum(float(row.get("user_cpu_seconds", 0.0)) for row in usage_rows),
            6,
        ),
        "summed_kernel_cpu_seconds": round(
            sum(float(row.get("kernel_cpu_seconds", 0.0)) for row in usage_rows),
            6,
        ),
        "records": records,
    }


def _item_serial(item_count):
    records = []
    started = time.perf_counter()
    for _index in range(item_count):
        for stage in WORKLOADS:
            records.append(_run_child(stage))
    ended = time.perf_counter()
    return _summarize(
        "item_serial_assembly_export_unreal",
        started,
        ended,
        records,
        len(records),
    )


def _parallel_stage(stage, item_count, workers):
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(lambda _index: _run_child(stage), range(item_count)))


def _stage_batched(item_count, workers, unreal_capacity):
    records = []
    started = time.perf_counter()
    records.extend(_parallel_stage("assembly", item_count, workers))
    records.extend(_parallel_stage("blender_export", item_count, workers))
    remaining = item_count
    while remaining:
        units = min(unreal_capacity, remaining)
        records.append(_run_child("unreal_ingest", units=units))
        remaining -= units
    ended = time.perf_counter()
    return _summarize(
        "stage_batched_with_bounded_unreal_processes",
        started,
        ended,
        records,
        len(records),
    )


def _stage_medians(serial):
    medians = {}
    for stage in WORKLOADS:
        values = [
            float(record["wall_seconds"])
            for record in serial["records"]
            if record["stage"] == stage
        ]
        medians[stage] = statistics.median(values)
    return medians


def run_benchmark(item_count=6, workers=2, unreal_capacity=3):
    item_count = max(1, int(item_count))
    workers = max(1, min(int(workers), item_count))
    unreal_capacity = max(1, int(unreal_capacity))
    serial = _item_serial(item_count)
    batched = _stage_batched(item_count, workers, unreal_capacity)
    medians = _stage_medians(serial)
    startup_proxy = min(medians.values()) * 0.25
    model = policy_comparison(
        item_count=item_count,
        assembly_seconds=medians["assembly"],
        blender_export_seconds=medians["blender_export"],
        unreal_ingest_seconds=max(
            0.0,
            medians["unreal_ingest"] - startup_proxy,
        ),
        assembly_workers=workers,
        blender_export_workers=workers,
        unreal_process_capacity=unreal_capacity,
        unreal_startup_seconds=startup_proxy,
    )
    return {
        "schema_version": 1,
        "kind": "sk_batch_first_run_stage_benchmark",
        "production_assets_touched": False,
        "cache_reuse": False,
        "item_count": item_count,
        "workers": workers,
        "unreal_process_capacity": unreal_capacity,
        "workloads": WORKLOADS,
        "item_serial": serial,
        "stage_batched": batched,
        "observed": {
            "wall_reduction_seconds": round(
                serial["wall_seconds"] - batched["wall_seconds"],
                6,
            ),
            "speedup": round(
                serial["wall_seconds"] / batched["wall_seconds"],
                6,
            ),
            "process_launch_reduction": (
                serial["process_launches"] - batched["process_launches"]
            ),
        },
        "measured_stage_medians_seconds": medians,
        "first_run_model": model,
        "failure_isolation": {
            "item_serial": "one asset per process chain",
            "stage_batched": (
                "item-local stage result plus Unreal per-item checkpoint; "
                "process recycled only at the configured capacity"
            ),
        },
    }


def parse_args(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--items", type=int, default=6)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--unreal-capacity", type=int, default=3)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--child-stage", choices=tuple(WORKLOADS))
    parser.add_argument("--child-units", type=int, default=1)
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    if args.child_stage:
        return _child_work(args.child_stage, args.child_units)
    try:
        report = run_benchmark(
            item_count=args.items,
            workers=args.workers,
            unreal_capacity=args.unreal_capacity,
        )
        payload = json.dumps(report, ensure_ascii=False, indent=2)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(payload + "\n", encoding="utf-8")
        print(payload)
        return 0
    finally:
        shutdown_process_supervisor("benchmark_complete")


if __name__ == "__main__":
    raise SystemExit(main())
