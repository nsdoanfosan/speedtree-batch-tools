"""First-run stage batching and memory-bounded worker policy.

This module contains no cache decisions.  It only controls concurrent work
that is going to run in the current invocation, using current available RAM.
"""

from __future__ import annotations

import ctypes
import os
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


def stage_worker_policy(
    stage,
    requested_workers,
    item_count,
    *,
    available_bytes=None,
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
    available = (
        available_physical_memory_bytes()
        if available_bytes is None
        else max(0, int(available_bytes))
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
        "reserve_bytes": reserve,
        "per_worker_peak_bytes": per_worker,
        "memory_worker_limit": memory_limit,
        "memory_limited": selected < min(requested, max(1, items)),
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
    "available_physical_memory_bytes",
    "policy_comparison",
    "stage_worker_policy",
]
