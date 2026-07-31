"""Shared child progress markers and parent inactivity policy.

This module does not implement a watchdog.  ``App._run_limited`` remains the
single parent-side inactivity implementation; these helpers only describe when
that existing watchdog is active.  ``None`` delegates the phase timeout to the
child operation that already owns it.
"""
from __future__ import annotations


SEND2UE_JOB_START_MARKER = "SK_BATCH_SEND2UE_JOB_START"
SEND2UE_DISK_EXPORT_START_MARKER = "SK_BATCH_SEND2UE_DISK_EXPORT_START"
SEND2UE_DISK_EXPORT_DONE_MARKER = "SK_BATCH_SEND2UE_DISK_EXPORT_DONE"
SEND2UE_RPC_OWNED_START_MARKER = "SK_BATCH_SEND2UE_RPC_OWNED_START"
SEND2UE_RPC_OWNED_DONE_MARKER = "SK_BATCH_SEND2UE_RPC_OWNED_DONE"
SEND2UE_JOB_FAILED_MARKER = "SK_BATCH_SEND2UE_JOB_FAILED"
SEND2UE_JOB_DONE_MARKER = "SK_BATCH_SEND2UE_JOB_DONE"


MATERIAL_PREFLIGHT_START_MARKER = "SK_BATCH_MATERIAL_PREFLIGHT_START"
MATERIAL_PREFLIGHT_STATIC_DONE_MARKER = "SK_BATCH_MATERIAL_PREFLIGHT_STATIC_DONE"
SPEEDTREE_SLOT_WAIT_MARKER = "SK_BATCH_SPEEDTREE_SLOT_WAIT"
SPEEDTREE_SLOT_ACQUIRED_MARKER = "SK_BATCH_SPEEDTREE_SLOT_ACQUIRED"
MATERIAL_PREFLIGHT_EXPORT_DONE_MARKER = "SK_BATCH_MATERIAL_PREFLIGHT_EXPORT_DONE"
MATERIAL_PREFLIGHT_INSPECTION_DONE_MARKER = (
    "SK_BATCH_MATERIAL_PREFLIGHT_INSPECTION_DONE"
)
MATERIAL_PREFLIGHT_CONTRACT_DONE_MARKER = (
    "SK_BATCH_MATERIAL_PREFLIGHT_CONTRACT_DONE"
)
MATERIAL_PREFLIGHT_FAILED_MARKER = "SK_BATCH_MATERIAL_PREFLIGHT_FAILED"
MATERIAL_PREFLIGHT_DONE_MARKER = "SK_BATCH_MATERIAL_PREFLIGHT_DONE"


def emit_progress_marker(marker, **fields):
    """Best-effort observer output; never changes child behavior."""
    payload = " ".join(f"{key}={value}" for key, value in fields.items())
    try:
        print(f"{marker} {payload}".rstrip(), flush=True)
    except (OSError, ValueError):
        pass


def material_preflight_inactivity_rules(stage_timeout, queue_timeout):
    """Delegate SpeedTree execution timeout to the child CLI only."""
    return {
        MATERIAL_PREFLIGHT_START_MARKER: stage_timeout,
        MATERIAL_PREFLIGHT_STATIC_DONE_MARKER: stage_timeout,
        SPEEDTREE_SLOT_WAIT_MARKER: queue_timeout,
        # speedtree_material_preflight.py passes its exact --timeout to
        # speedtree_cli.export_target.  A second parent 900+grace deadline
        # would race that authoritative child result.
        SPEEDTREE_SLOT_ACQUIRED_MARKER: None,
        MATERIAL_PREFLIGHT_EXPORT_DONE_MARKER: stage_timeout,
        MATERIAL_PREFLIGHT_INSPECTION_DONE_MARKER: stage_timeout,
        MATERIAL_PREFLIGHT_CONTRACT_DONE_MARKER: stage_timeout,
        MATERIAL_PREFLIGHT_FAILED_MARKER: stage_timeout,
        MATERIAL_PREFLIGHT_DONE_MARKER: stage_timeout,
    }


def send2ue_inactivity_rules(stage_timeout, disk_export_timeout):
    """Keep one parent inactivity policy while RPC keeps its own timeout."""
    return {
        # Blender can spend minutes loading a large source file before the
        # disk-export marker.  Preserve the former 1800-second allowance as
        # phase inactivity, not as a whole-job wall clock.
        SEND2UE_JOB_START_MARKER: disk_export_timeout,
        SEND2UE_DISK_EXPORT_START_MARKER: disk_export_timeout,
        SEND2UE_DISK_EXPORT_DONE_MARKER: stage_timeout,
        # send2ue's RPC_TIME_OUT is the single timeout owner for this phase.
        SEND2UE_RPC_OWNED_START_MARKER: None,
        SEND2UE_RPC_OWNED_DONE_MARKER: stage_timeout,
        SEND2UE_JOB_FAILED_MARKER: stage_timeout,
        SEND2UE_JOB_DONE_MARKER: stage_timeout,
    }
