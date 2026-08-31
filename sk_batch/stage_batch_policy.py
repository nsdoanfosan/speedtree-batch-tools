"""First-run stage batching and memory-bounded worker policy.

This module contains no cache decisions.  It only controls concurrent work
that is going to run in the current invocation, using current available RAM.
"""

from __future__ import annotations

import ctypes
import os
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from ctypes import wintypes


GIB = 1024 ** 3
DEFAULT_RESERVE_BYTES = 8 * GIB
DEFAULT_STAGE_PEAK_BYTES = {
    # Conservative production observations for large vegetation assets.  The
    # configured worker count remains the upper bound; memory can only reduce
    # it, never create surprise concurrency.
    "assembly": 6 * GIB,
    "blender_export": 4 * GIB,
    "unreal_ingest": 10 * GIB,
}


class _MemoryStatusEx(ctypes.Structure):
    _fields_ = (
        ("length", wintypes.DWORD),
        ("memory_load", wintypes.DWORD),
        ("total_phys", ctypes.c_ulonglong),
        ("avail_phys", ctypes.c_ulonglong),
        ("total_page_file", ctypes.c_ulonglong),
        ("avail_page_file", ctypes.c_ulonglong),
        ("total_virtual", ctypes.c_ulonglong),
        ("avail_virtual", ctypes.c_ulonglong),
        ("avail_extended_virtual", ctypes.c_ulonglong),
    )


class _PerformanceInformation(ctypes.Structure):
    _fields_ = (
        ("cb", wintypes.DWORD),
        ("commit_total", ctypes.c_size_t),
        ("commit_limit", ctypes.c_size_t),
        ("commit_peak", ctypes.c_size_t),
        ("physical_total", ctypes.c_size_t),
        ("physical_available", ctypes.c_size_t),
        ("system_cache", ctypes.c_size_t),
        ("kernel_total", ctypes.c_size_t),
        ("kernel_paged", ctypes.c_size_t),
        ("kernel_nonpaged", ctypes.c_size_t),
        ("page_size", ctypes.c_size_t),
        ("handle_count", wintypes.DWORD),
        ("process_count", wintypes.DWORD),
        ("thread_count", wintypes.DWORD),
    )


def available_physical_memory_bytes():
    """Return immediately available physical RAM, or ``None`` if unknown."""
    if os.name != "nt":
        return None
    status = _MemoryStatusEx()
    status.length = ctypes.sizeof(status)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    query = kernel32.GlobalMemoryStatusEx
    query.argtypes = (ctypes.POINTER(_MemoryStatusEx),)
    query.restype = wintypes.BOOL
    if not query(ctypes.byref(status)):
        return None
    return int(status.avail_phys)


def available_commit_memory_bytes():
    """Return system-wide commit headroom, or ``None`` if unknown."""
    if os.name != "nt":
        return None
    information = _PerformanceInformation()
    information.cb = ctypes.sizeof(information)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    query = psapi.GetPerformanceInfo
    query.argtypes = (
        ctypes.POINTER(_PerformanceInformation),
        wintypes.DWORD,
    )
    query.restype = wintypes.BOOL
    if not query(ctypes.byref(information), ctypes.sizeof(information)):
        return None
    available_pages = max(
        0,
        int(information.commit_limit) - int(information.commit_total),
    )
    return available_pages * int(information.page_size)


def available_memory_snapshot():
    """Sample the two Windows memory limits relevant to a new worker."""
    physical = available_physical_memory_bytes()
    commit = available_commit_memory_bytes()
    candidates = [value for value in (physical, commit) if value is not None]
    effective = min(candidates) if candidates else None
    return {
        "available_physical_bytes": physical,
        "available_commit_bytes": commit,
        "effective_available_bytes": effective,
        "limiting_resource": (
            "physical"
            if effective is not None and effective == physical
            else "commit"
            if effective is not None and effective == commit
            else None
        ),
    }


def stage_worker_policy(
    stage,
    requested_workers,
    item_count,
    *,
    available_bytes=None,
    available_commit_bytes=None,
    per_worker_peak_bytes=None,
    reserve_bytes=DEFAULT_RESERVE_BYTES,
):
    """Select a worker count without exceeding the current RAM envelope."""
    requested = max(1, int(requested_workers))
    items = max(0, int(item_count))
    stage = str(stage)
    per_worker = int(
        per_worker_peak_bytes
        if per_worker_peak_bytes is not None
        else DEFAULT_STAGE_PEAK_BYTES[stage]
    )
    reserve = max(0, int(reserve_bytes))
    if available_bytes is None and available_commit_bytes is None:
        memory = available_memory_snapshot()
        physical_available = memory["available_physical_bytes"]
        commit_available = memory["available_commit_bytes"]
        available = memory["effective_available_bytes"]
        limiting_resource = memory["limiting_resource"]
    else:
        physical_available = (
            None if available_bytes is None else max(0, int(available_bytes))
        )
        commit_available = (
            None
            if available_commit_bytes is None
            else max(0, int(available_commit_bytes))
        )
        candidates = [
            value
            for value in (physical_available, commit_available)
            if value is not None
        ]
        available = min(candidates) if candidates else None
        limiting_resource = (
            "physical"
            if available is not None and available == physical_available
            else "commit"
            if available is not None and available == commit_available
            else None
        )
    selected = max(1, min(requested, max(1, items)))
    memory_limit = None
    if available is not None and per_worker > 0:
        usable = max(0, available - reserve)
        memory_limit = max(1, usable // per_worker)
        selected = min(selected, memory_limit)
    return {
        "stage": stage,
        "requested_workers": requested,
        "selected_workers": selected,
        "item_count": items,
        "available_memory_bytes": available,
        "available_physical_bytes": physical_available,
        "available_commit_bytes": commit_available,
        "limiting_resource": limiting_resource,
        "reserve_bytes": reserve,
        "per_worker_peak_bytes": per_worker,
        "memory_worker_limit": memory_limit,
        "memory_limited": selected < min(requested, max(1, items)),
    }


def run_memory_bounded_stage(
    stage,
    items,
    requested_workers,
    worker,
    *,
    on_complete=None,
    memory_snapshot_fn=available_memory_snapshot,
    per_worker_peak_bytes=None,
    reserve_bytes=DEFAULT_RESERVE_BYTES,
):
    """Run independent process jobs with admission-time memory checks.

    ``GlobalMemoryStatusEx`` is deliberately sampled before every launch.  A
    worker already in flight retains one full peak reservation, so a process
    that has not reached its peak cannot make a later launch look safer than
    it is.  One worker is always admitted to guarantee forward progress.
    """
    items = list(items)
    count = len(items)
    if count == 0:
        return [], {
            "stage": str(stage),
            "requested_workers": max(1, int(requested_workers)),
            "selected_worker_ceiling": 0,
            "max_concurrent_workers": 0,
            "per_worker_peak_bytes": int(
                per_worker_peak_bytes
                if per_worker_peak_bytes is not None
                else DEFAULT_STAGE_PEAK_BYTES[str(stage)]
            ),
            "reserve_bytes": max(0, int(reserve_bytes)),
            "admission_checks": [],
            "memory_limited": False,
        }

    stage = str(stage)
    requested = max(1, int(requested_workers))
    ceiling = min(requested, count)
    per_worker = int(
        per_worker_peak_bytes
        if per_worker_peak_bytes is not None
        else DEFAULT_STAGE_PEAK_BYTES[stage]
    )
    reserve = max(0, int(reserve_bytes))
    results = [None] * count
    admission_checks = []
    running = {}
    next_index = 0
    max_concurrent = 0
    memory_limited = False

    def can_launch():
        nonlocal memory_limited
        snapshot = dict(memory_snapshot_fn() or {})
        available = snapshot.get("effective_available_bytes")
        reserved = len(running) * per_worker
        usable = (
            None
            if available is None
            else max(0, int(available) - reserve - reserved)
        )
        admitted = not running or usable is None or usable >= per_worker
        if not admitted:
            memory_limited = True
        admission_checks.append({
            **snapshot,
            "running_workers": len(running),
            "reserved_running_bytes": reserved,
            "usable_for_new_worker_bytes": usable,
            "admitted": admitted,
        })
        return admitted

    with ThreadPoolExecutor(max_workers=ceiling) as pool:
        while next_index < count or running:
            while next_index < count and len(running) < ceiling:
                if not can_launch():
                    break
                future = pool.submit(worker, items[next_index])
                running[future] = next_index
                next_index += 1
                max_concurrent = max(max_concurrent, len(running))
            if not running:
                # ``can_launch`` admits an empty pool, so this is defensive.
                raise RuntimeError("memory-bounded stage made no progress")
            completed, _pending = wait(
                tuple(running),
                return_when=FIRST_COMPLETED,
            )
            for future in completed:
                index = running.pop(future)
                result = future.result()
                results[index] = result
                if on_complete is not None:
                    on_complete(index, items[index], result)

    return results, {
        "stage": stage,
        "requested_workers": requested,
        "selected_worker_ceiling": ceiling,
        "max_concurrent_workers": max_concurrent,
        "per_worker_peak_bytes": per_worker,
        "reserve_bytes": reserve,
        "admission_checks": admission_checks,
        "memory_limited": memory_limited or max_concurrent < ceiling,
    }


def policy_comparison(
    *,
    item_count,
    assembly_seconds,
    blender_export_seconds,
    unreal_ingest_seconds,
    assembly_workers,
    blender_export_workers,
    unreal_process_capacity,
    unreal_startup_seconds,
):
    """Compare the two concrete pipeline shapes with measured stage samples.

    Durations are per-item medians.  This is deliberately a first-run model:
    every item executes and no receipt or artifact cache is represented.
    """
    count = max(0, int(item_count))
    if count == 0:
        raise ValueError("item_count must be positive")
    assembly_workers = max(1, int(assembly_workers))
    export_workers = max(1, int(blender_export_workers))
    capacity = max(1, int(unreal_process_capacity))
    serial = count * (
        float(assembly_seconds)
        + float(blender_export_seconds)
        + float(unreal_ingest_seconds)
        + float(unreal_startup_seconds)
    )
    stage_batched = (
        ((count + assembly_workers - 1) // assembly_workers)
        * float(assembly_seconds)
        + ((count + export_workers - 1) // export_workers)
        * float(blender_export_seconds)
        + count * float(unreal_ingest_seconds)
        + ((count + capacity - 1) // capacity)
        * float(unreal_startup_seconds)
    )
    return {
        "item_serial": {
            "estimated_wall_seconds": round(serial, 6),
            "unreal_process_launches": count,
            "failure_isolation": "asset_boundary",
        },
        "stage_batched": {
            "estimated_wall_seconds": round(stage_batched, 6),
            "unreal_process_launches": (
                (count + capacity - 1) // capacity
            ),
            "failure_isolation": (
                "item_checkpoint_plus_bounded_process_chunk"
            ),
        },
        "estimated_wall_reduction_seconds": round(
            serial - stage_batched,
            6,
        ),
        "estimated_speedup": round(serial / stage_batched, 6),
    }


__all__ = [
    "DEFAULT_RESERVE_BYTES",
    "DEFAULT_STAGE_PEAK_BYTES",
    "available_commit_memory_bytes",
    "available_memory_snapshot",
    "available_physical_memory_bytes",
    "policy_comparison",
    "run_memory_bounded_stage",
    "stage_worker_policy",
]
