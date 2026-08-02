"""Durable progress and liveness receipts for failed-result retries.

The retry classifier and executors deliberately remain elsewhere.  This module
is an observer contract: it records which exact target/partition is active,
keeps process/output/queue liveness timestamps distinct, and reconstructs a
trustworthy terminal state after the GUI or queue owner is restarted.
"""
from __future__ import annotations

import copy
import json
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path


SCHEMA_VERSION = 1
RECEIPT_KIND = "failed_retry_progress_receipt"
LATEST_KIND = "failed_retry_progress_latest"
DIAGNOSTIC_LIMIT = 240
LIFECYCLE_EVENT_LIMIT = 64

PLANNING = "planning"
SHARED_QUEUE_WAIT = "shared_queue_wait"
CLAIMED = "claimed"
BLENDER = "blender"
SEND2UE = "send2ue"
UNREAL = "unreal"
POST_CHECK = "post_check"
PENDING_UNREAL = "pending_unreal"
BLOCKED = "blocked"
STALLED = "stalled"
OWNER_LOST = "owner_lost"
CANCELLED = "cancelled"
COMPLETE = "complete"
FAILED = "failed"

TERMINAL_STAGES = frozenset(
    {BLOCKED, OWNER_LOST, CANCELLED, COMPLETE, FAILED}
)
LIVE_STAGES = frozenset(
    {CLAIMED, BLENDER, SEND2UE, UNREAL, POST_CHECK, STALLED}
)
ALL_STAGES = frozenset(
    {
        PLANNING,
        SHARED_QUEUE_WAIT,
        CLAIMED,
        BLENDER,
        SEND2UE,
        UNREAL,
        POST_CHECK,
        PENDING_UNREAL,
        BLOCKED,
        STALLED,
        OWNER_LOST,
        CANCELLED,
        COMPLETE,
        FAILED,
    }
)


def _utc_iso(timestamp):
    return datetime.fromtimestamp(
        float(timestamp), tz=timezone.utc
    ).isoformat(timespec="milliseconds")


def _bounded_diagnostic(value, limit=DIAGNOSTIC_LIMIT):
    text = " ".join(str(value or "").replace("\x00", " ").split())
    limit = max(16, int(limit))
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _atomic_write_json(path, payload):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(
        f".{target.name}.{os.getpid()}.{threading.get_ident()}."
        f"{uuid.uuid4().hex}.tmp"
    )
    encoded = (
        json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )
    try:
        temporary.write_text(encoded, encoding="utf-8")
        os.replace(temporary, target)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def default_receipt_dir():
    local = os.environ.get("LOCALAPPDATA")
    if local:
        return Path(local) / "SpeedTreeBatchTools" / "retry_progress"
    return Path.cwd() / ".speedtree_retry_progress"


def _target_record(target_id, ordinal, total, now):
    target_id = str(target_id)
    return {
        "target_id": target_id,
        "target_name": Path(target_id).name,
        "global_ordinal": int(ordinal),
        "global_total": int(total),
        "partition": "unclassified",
        "partition_ordinal": None,
        "partition_total": None,
        "execution_path": None,
        "stage": PLANNING,
        "resume_stage": None,
        "started_at": now,
        "stage_started_at": now,
        "updated_at": now,
        "last_progress_at": now,
        "last_output_at": None,
        "last_heartbeat_at": None,
        "latest_diagnostic": "retry planning",
        "outcome": None,
        "terminal_reason": None,
        "terminal_at": None,
    }


class RetryProgressReceipt:
    """Thread-safe, atomic per-target retry progress receipt."""

    def __init__(
        self,
        path,
        payload,
        *,
        latest_path=None,
        clock=None,
        stall_warning_seconds=120.0,
        owner_lost_seconds=45.0,
        notify=None,
    ):
        self.path = Path(path)
        self.latest_path = (
            Path(latest_path)
            if latest_path is not None
            else self.path.parent / "latest.json"
        )
        self._payload = copy.deepcopy(payload)
        self._clock = clock or time.time
        self.stall_warning_seconds = max(
            0.05, float(stall_warning_seconds)
        )
        self.owner_lost_seconds = max(0.05, float(owner_lost_seconds))
        self._notify = notify
        self._lock = threading.RLock()

    @classmethod
    def create(
        cls,
        target_ids,
        *,
        receipt_dir=None,
        clock=None,
        stall_warning_seconds=120.0,
        owner_lost_seconds=45.0,
        notify=None,
    ):
        clock = clock or time.time
        now = float(clock())
        run_id = uuid.uuid4().hex
        directory = Path(receipt_dir or default_receipt_dir())
        path = directory / (
            datetime.fromtimestamp(now, tz=timezone.utc).strftime(
                "retry_%Y%m%dT%H%M%S"
            )
            + f"_{run_id}.json"
        )
        ordered = []
        seen = set()
        for value in target_ids:
            target_id = str(value)
            if target_id in seen:
                continue
            seen.add(target_id)
            ordered.append(target_id)
        payload = {
            "schema_version": SCHEMA_VERSION,
            "kind": RECEIPT_KIND,
            "run_id": run_id,
            # This describes why the operator selected the targets.  It is
            # deliberately separate from the outcome of this retry run: a
            # historical failure must never make a currently running retry
            # look terminal.
            "selection_context": "historical_failed_or_stale_retry_targets",
            "created_at": now,
            "created_at_iso": _utc_iso(now),
            "updated_at": now,
            "updated_at_iso": _utc_iso(now),
            "run_state": "running",
            "terminal_outcome": None,
            "stage": PLANNING,
            "current_target_id": ordered[0] if ordered else None,
            "planning": {
                "schema_version": 1,
                "status": "unowned",
                "owner": None,
                "started_at": now,
                "heartbeat_at": None,
                "progress_at": now,
                "plan_ready_at": None,
                "commit_started_at": None,
                "committed_at": None,
                "terminal_reason": None,
                "progress": {
                    "schema_version": 1,
                    "substage": "created",
                    "completed_count": 0,
                    "total_count": len(ordered),
                    "classified_count": 0,
                    "validated_count": 0,
                    "cache_hits": 0,
                    "cache_misses": 0,
                    "cache_status": "unchecked",
                    "current_target_id": ordered[0] if ordered else None,
                    "last_completed_unit": "",
                    "updated_at": now,
                },
                "plan_cache": None,
            },
            "queue_jobs": {},
            "lifecycle_events": [
                {
                    "at": now,
                    "at_iso": _utc_iso(now),
                    "kind": "retry_receipt_created",
                    "detail": "historical failed/stale retry selection",
                }
            ],
            "terminal_reason": None,
            "terminal_at": None,
            "targets": [
                _target_record(target_id, index, len(ordered), now)
                for index, target_id in enumerate(ordered, start=1)
            ],
        }
        receipt = cls(
            path,
            payload,
            clock=clock,
            stall_warning_seconds=stall_warning_seconds,
            owner_lost_seconds=owner_lost_seconds,
            notify=notify,
        )
        with receipt._lock:
            receipt._write_locked(write_latest=True)
        receipt._notify_snapshot()
        return receipt

    @classmethod
    def open(
        cls,
        path,
        *,
        clock=None,
        stall_warning_seconds=120.0,
        owner_lost_seconds=45.0,
        notify=None,
    ):
        path = Path(path)
        payload = json.loads(path.read_text(encoding="utf-8"))
        cls._validate_payload(payload)
        return cls(
            path,
            payload,
            clock=clock,
            stall_warning_seconds=stall_warning_seconds,
            owner_lost_seconds=owner_lost_seconds,
            notify=notify,
        )

    @classmethod
    def load_latest(
        cls,
        receipt_dir=None,
        *,
        clock=None,
        stall_warning_seconds=120.0,
        owner_lost_seconds=45.0,
        notify=None,
    ):
        directory = Path(receipt_dir or default_receipt_dir()).resolve()
        latest_path = directory / "latest.json"
        try:
            latest = json.loads(latest_path.read_text(encoding="utf-8"))
            if latest.get("kind") != LATEST_KIND:
                return None
            candidate = Path(str(latest.get("receipt_path") or ""))
            if not candidate.is_absolute():
                candidate = directory / candidate
            candidate = candidate.resolve()
            directory_prefix = str(directory).rstrip("\\/") + os.sep
            if not str(candidate).startswith(directory_prefix):
                return None
            receipt = cls.open(
                candidate,
                clock=clock,
                stall_warning_seconds=stall_warning_seconds,
                owner_lost_seconds=owner_lost_seconds,
                notify=notify,
            )
            if receipt.run_id != str(latest.get("run_id") or ""):
                return None
            return receipt
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return None

    @staticmethod
    def _validate_payload(payload):
        if not isinstance(payload, dict):
            raise ValueError("retry receipt is not an object")
        if payload.get("schema_version") != SCHEMA_VERSION:
            raise ValueError("unsupported retry receipt schema")
        if payload.get("kind") != RECEIPT_KIND:
            raise ValueError("unexpected retry receipt kind")
        if not isinstance(payload.get("run_id"), str):
            raise ValueError("retry receipt run_id is invalid")
        targets = payload.get("targets")
        if not isinstance(targets, list):
            raise ValueError("retry receipt targets are invalid")
        seen = set()
        for row in targets:
            if not isinstance(row, dict):
                raise ValueError("retry receipt target is invalid")
            target_id = row.get("target_id")
            if not isinstance(target_id, str) or not target_id:
                raise ValueError("retry receipt target_id is invalid")
            if target_id in seen:
                raise ValueError("retry receipt target_id is duplicated")
            seen.add(target_id)
            if row.get("stage") not in ALL_STAGES:
                raise ValueError("retry receipt target stage is invalid")
        events = payload.get("lifecycle_events", [])
        if not isinstance(events, list):
            raise ValueError("retry receipt lifecycle events are invalid")

    @property
    def run_id(self):
        return self._payload["run_id"]

    def _now(self):
        return float(self._clock())

    def _targets_locked(self):
        return self._payload["targets"]

    def _target_locked(self, target_id):
        target_id = str(target_id)
        for row in self._targets_locked():
            if row["target_id"] == target_id:
                return row
        return None

    def _partition_targets_locked(self, partition):
        return [
            row
            for row in self._targets_locked()
            if row.get("partition") == str(partition)
        ]

    def _touch_locked(self, now):
        self._payload["updated_at"] = now
        self._payload["updated_at_iso"] = _utc_iso(now)

    def _append_lifecycle_event_locked(self, kind, now, *, detail=None, **data):
        """Append a small chronological audit event without exposing paths."""
        event = {
            "at": now,
            "at_iso": _utc_iso(now),
            "kind": str(kind),
        }
        if detail:
            event["detail"] = _bounded_diagnostic(detail)
        for key, value in data.items():
            if value is None:
                continue
            if isinstance(value, str):
                event[str(key)] = _bounded_diagnostic(value)
            elif isinstance(value, (bool, int, float)):
                event[str(key)] = value
        events = self._payload.setdefault("lifecycle_events", [])
        events.append(event)
        if len(events) > LIFECYCLE_EVENT_LIMIT:
            del events[:-LIFECYCLE_EVENT_LIMIT]

    def record_operator_close(self, detail=None):
        """Persist a window-close observation without inventing a root cause."""
        now = self._now()
        with self._lock:
            if self._payload.get("terminal_at") is not None:
                return False
            current_id = self._payload.get("current_target_id")
            current = self._target_locked(current_id) if current_id else None
            self._append_lifecycle_event_locked(
                "operator_app_close",
                now,
                detail=detail or "operator closed the SK Batch window",
                current_target_id=current_id,
                current_stage=(current or {}).get("stage"),
                run_stage=self._payload.get("stage"),
                run_state=self._payload.get("run_state", "running"),
            )
            self._touch_locked(now)
            self._write_locked(write_latest=True)
        self._notify_snapshot()
        return True

    def _write_locked(self, *, write_latest=False):
        self._validate_payload(self._payload)
        _atomic_write_json(self.path, self._payload)
        if write_latest:
            _atomic_write_json(
                self.latest_path,
                {
                    "schema_version": SCHEMA_VERSION,
                    "kind": LATEST_KIND,
                    "run_id": self.run_id,
                    "receipt_path": self.path.name,
                    "updated_at": self._payload["updated_at"],
                    "updated_at_iso": self._payload["updated_at_iso"],
                },
            )

    def _notify_snapshot(self):
        if self._notify is None:
            return
        try:
            self._notify(self.snapshot(evaluate=False))
        except Exception:
            # Progress observation cannot change retry execution semantics.
            pass

    def set_notify(self, notify):
        """Install observation only after startup reconciliation is complete."""

        with self._lock:
            self._notify = notify

    def _planning_locked(self):
        planning = self._payload.get("planning")
        return planning if isinstance(planning, dict) else None

    def _planning_rows_locked(self):
        return [
            row
            for row in self._targets_locked()
            if row.get("stage") == PLANNING
            or (
                row.get("stage") == STALLED
                and row.get("resume_stage") == PLANNING
            )
        ]

    def start_planning(self, owner):
        """Bind pre-enqueue planning to one exact process/session owner."""

        if not isinstance(owner, dict):
            raise ValueError("planning owner must be an object")
        required = ("owner_id", "hostname", "pid", "planning_session_id")
        if any(owner.get(key) in (None, "") for key in required):
            raise ValueError("planning owner identity is incomplete")
        now = self._now()
        with self._lock:
            if self._payload.get("terminal_at") is not None:
                return False
            planning = self._planning_locked()
            if planning is None:
                planning = {"schema_version": 1, "started_at": now}
                self._payload["planning"] = planning
            status = str(planning.get("status") or "unowned")
            existing = planning.get("owner") or {}
            if status != "unowned" and existing.get(
                "planning_session_id"
            ) != owner.get("planning_session_id"):
                return False
            planning.update({
                "status": "active",
                "owner": copy.deepcopy(owner),
                "started_at": float(planning.get("started_at") or now),
                "heartbeat_at": now,
                "progress_at": float(planning.get("progress_at") or now),
                "plan_ready_at": None,
                "commit_started_at": None,
                "committed_at": None,
                "terminal_reason": None,
                "owner_alive": True,
                "owner_checked_at": now,
            })
            current = self._target_locked(
                self._payload.get("current_target_id")
            )
            if current is not None and current in self._planning_rows_locked():
                current["last_heartbeat_at"] = now
            self._append_lifecycle_event_locked(
                "planning_owner_started",
                now,
                owner_id=str(owner.get("owner_id")),
                pid=int(owner.get("pid")),
                planning_session_id=str(owner.get("planning_session_id")),
            )
            self._touch_locked(now)
            self._write_locked(write_latest=True)
        self._notify_snapshot()
        return True

    def planning_heartbeat(self, planning_session_id, *, thread_ident=None):
        """Renew only the exact active planner without inventing progress."""

        now = self._now()
        with self._lock:
            planning = self._planning_locked()
            if planning is None or planning.get("status") != "active":
                return False
            owner = planning.get("owner") or {}
            if owner.get("planning_session_id") != str(planning_session_id):
                return False
            existing_thread = owner.get("thread_ident")
            if (
                existing_thread is not None
                and thread_ident is not None
                and int(existing_thread) != int(thread_ident)
            ):
                return False
            if thread_ident is not None:
                owner["thread_ident"] = int(thread_ident)
            planning["heartbeat_at"] = now
            planning["owner_alive"] = True
            planning["owner_checked_at"] = now
            current = self._target_locked(
                self._payload.get("current_target_id")
            )
            if current is not None and current in self._planning_rows_locked():
                current["last_heartbeat_at"] = now
                current["updated_at"] = now
            self._touch_locked(now)
            self._write_locked(write_latest=True)
        return True

    def planning_progress(
        self,
        planning_session_id,
        *,
        substage,
        completed_count,
        total_count,
        current_target_id=None,
        last_completed_unit=None,
        classified_count=0,
        validated_count=0,
        cache_hits=0,
        cache_misses=0,
        cache_status=None,
        progress=True,
    ):
        """Persist structured planner work for the exact active session.

        Heartbeat and progress are deliberately separate: a live but slow
        planner may renew its heartbeat without moving these counters.
        """

        now = self._now()
        with self._lock:
            planning = self._planning_locked()
            if planning is None or planning.get("status") != "active":
                return False
            owner = planning.get("owner") or {}
            if owner.get("planning_session_id") != str(planning_session_id):
                return False
            total = max(0, int(total_count))
            completed = max(0, min(total, int(completed_count)))
            target_id = (
                None
                if current_target_id in (None, "")
                else str(current_target_id)
            )
            planning_progress = planning.setdefault("progress", {})
            planning_progress.update({
                "schema_version": 1,
                "substage": str(substage),
                "completed_count": completed,
                "total_count": total,
                "classified_count": max(0, int(classified_count)),
                "validated_count": max(0, int(validated_count)),
                "cache_hits": max(0, int(cache_hits)),
                "cache_misses": max(0, int(cache_misses)),
                "current_target_id": target_id,
                "last_completed_unit": _bounded_diagnostic(
                    Path(str(last_completed_unit)).name
                    if last_completed_unit
                    else ""
                ),
                "updated_at": now,
            })
            if cache_status is not None:
                planning_progress["cache_status"] = str(cache_status)
            planning["heartbeat_at"] = now
            planning["owner_alive"] = True
            planning["owner_checked_at"] = now
            if progress:
                planning["progress_at"] = now
            if target_id is not None:
                row = self._target_locked(target_id)
                if row is not None and row in self._planning_rows_locked():
                    self._payload["current_target_id"] = target_id
                    row["last_heartbeat_at"] = now
                    if progress:
                        row["last_progress_at"] = now
                    row["updated_at"] = now
                    row["latest_diagnostic"] = _bounded_diagnostic(
                        f"planning {substage} · {completed}/{total}"
                        f" · cache hit {max(0, int(cache_hits))}"
                        f"/miss {max(0, int(cache_misses))}"
                    )
            self._touch_locked(now)
            self._write_locked(write_latest=True)
        self._notify_snapshot()
        return True

    def store_planning_cache(
        self,
        planning_session_id,
        *,
        input_signature,
        artifact,
        side_effects_committed=False,
    ):
        """Atomically attach a reusable immutable plan to this receipt."""

        now = self._now()
        with self._lock:
            planning = self._planning_locked()
            if planning is None:
                return False
            owner = planning.get("owner") or {}
            if owner.get("planning_session_id") != str(planning_session_id):
                return False
            if str(planning.get("status") or "") in {
                "cancelled",
                "failed",
                "owner_lost",
            }:
                return False
            # Round-trip here so a cache can never make the receipt unwritable.
            cache = json.loads(json.dumps({
                "schema_version": 1,
                "input_signature": input_signature,
                "artifact": artifact,
                "side_effects_committed": bool(side_effects_committed),
                "stored_at": now,
            }, ensure_ascii=False, allow_nan=False))
            planning["plan_cache"] = cache
            self._touch_locked(now)
            self._write_locked(write_latest=True)
        return True

    def planning_cache(self):
        """Return the durable cache without exposing mutable receipt state."""

        with self._lock:
            planning = self._planning_locked()
            cache = (planning or {}).get("plan_cache")
            return copy.deepcopy(cache) if isinstance(cache, dict) else None

    def planning_ready(self, planning_session_id):
        """Persist that a complete plan awaits the single UI-thread commit."""

        now = self._now()
        with self._lock:
            planning = self._planning_locked()
            if planning is None or planning.get("status") != "active":
                return False
            owner = planning.get("owner") or {}
            if owner.get("planning_session_id") != str(planning_session_id):
                return False
            planning["status"] = "ready"
            planning["plan_ready_at"] = now
            planning["heartbeat_at"] = now
            self._append_lifecycle_event_locked(
                "planning_ready",
                now,
                planning_session_id=str(planning_session_id),
            )
            self._touch_locked(now)
            self._write_locked(write_latest=True)
        self._notify_snapshot()
        return True

    def claim_planning_commit(self):
        """Durably claim the only allowed plan commit/enqueue attempt."""

        now = self._now()
        with self._lock:
            planning = self._planning_locked()
            if planning is None or planning.get("status") != "ready":
                return False
            planning["status"] = "committing"
            planning["commit_started_at"] = now
            self._append_lifecycle_event_locked("planning_commit_started", now)
            self._touch_locked(now)
            self._write_locked(write_latest=True)
        return True

    def complete_planning_commit(self):
        """Record that every planned partition was handled exactly once."""

        now = self._now()
        with self._lock:
            planning = self._planning_locked()
            if planning is None or planning.get("status") != "committing":
                return False
            planning["status"] = "committed"
            planning["committed_at"] = now
            planning["heartbeat_at"] = now
            self._append_lifecycle_event_locked("planning_commit_completed", now)
            self._touch_locked(now)
            self._finalize_if_terminal_locked(now)
            self._write_locked(write_latest=True)
        self._notify_snapshot()
        return True

    def _terminalize_planning_locked(self, stage, reason, now):
        changed = False
        for row in self._planning_rows_locked():
            if row.get("stage") in TERMINAL_STAGES:
                continue
            self._transition_target_locked(
                row,
                stage,
                now,
                diagnostic=reason,
                terminal_reason=reason,
                outcome=stage,
            )
            changed = True
        return changed

    def finish_planning(self, stage, reason):
        """Truthfully end an uncommitted plan as cancel/fail/owner loss."""

        if stage not in {CANCELLED, FAILED, OWNER_LOST}:
            raise ValueError("planning terminal stage is invalid")
        now = self._now()
        changed = False
        with self._lock:
            planning = self._planning_locked()
            status = str((planning or {}).get("status") or "unowned")
            if status in {"committed", "cancelled", "failed", "owner_lost"}:
                return False
            changed = self._terminalize_planning_locked(stage, reason, now)
            if planning is None:
                planning = {"schema_version": 1, "started_at": now}
                self._payload["planning"] = planning
            planning["status"] = stage
            planning["terminal_reason"] = _bounded_diagnostic(reason)
            planning["terminal_at"] = now
            self._append_lifecycle_event_locked(
                "planning_" + stage,
                now,
                detail=reason,
            )
            self._touch_locked(now)
            self._finalize_if_terminal_locked(now, reason=reason)
            self._write_locked(write_latest=True)
            changed = True
        if changed:
            self._notify_snapshot()
        return changed

    def reconcile_planning_owner(self, owner_alive):
        """Reconcile an uncommitted plan against its exact persisted owner."""

        now = self._now()
        with self._lock:
            if not self._planning_rows_locked():
                return False
            planning = self._planning_locked()
            if planning is None or not isinstance(planning.get("owner"), dict):
                stage = FAILED
                reason = "planning owner identity missing on restore"
            else:
                status = str(planning.get("status") or "unowned")
                if status in {
                    "committed",
                    "cancelled",
                    "failed",
                    "owner_lost",
                }:
                    return False
                planning["owner_alive"] = owner_alive
                planning["owner_checked_at"] = now
                heartbeat_at = planning.get("heartbeat_at")
                heartbeat_age = (
                    float("inf")
                    if not isinstance(heartbeat_at, (int, float))
                    else max(0.0, now - float(heartbeat_at))
                )
                if owner_alive is False:
                    stage = OWNER_LOST
                    reason = "exact planning owner process is absent"
                elif status == "unowned":
                    stage = FAILED
                    reason = "planning owner was never established"
                elif (
                    status in {"active", "ready", "committing"}
                    and heartbeat_age >= self.owner_lost_seconds
                ):
                    stage = FAILED
                    reason = (
                        "planning heartbeat expired while owner process "
                        "remained present"
                        if owner_alive is True
                        else "planning heartbeat expired and owner is unverifiable"
                    )
                else:
                    return False
        return self.finish_planning(stage, reason)

    def assign_partition(self, partition, target_ids, execution_path):
        partition = str(partition)
        now = self._now()
        ordered = [str(value) for value in target_ids]
        with self._lock:
            total = len(ordered)
            for index, target_id in enumerate(ordered, start=1):
                row = self._target_locked(target_id)
                if row is None:
                    continue
                row.update(
                    {
                        "partition": partition,
                        "partition_ordinal": index,
                        "partition_total": total,
                        "execution_path": str(execution_path),
                        "updated_at": now,
                    }
                )
            self._touch_locked(now)
            self._write_locked(write_latest=True)
        self._notify_snapshot()

    def register_queue_job(
        self,
        partition,
        job_id,
        sequence,
        *,
        local_job_id=None,
    ):
        now = self._now()
        partition = str(partition)
        with self._lock:
            self._payload.setdefault("queue_jobs", {})[partition] = {
                "job_id": str(job_id),
                "sequence": int(sequence),
                "local_job_id": local_job_id,
                "status": "queued",
                "position": None,
                "queued_count": None,
                "owner": None,
                "updated_at": now,
            }
            self._transition_partition_locked(
                partition,
                SHARED_QUEUE_WAIT,
                now,
                diagnostic=f"shared queue #{sequence} registered",
                progress=True,
            )
            self._append_lifecycle_event_locked(
                "shared_queue_registered",
                now,
                partition=partition,
                job_id=str(job_id),
                sequence=int(sequence),
            )
            self._touch_locked(now)
            self._write_locked(write_latest=True)
        self._notify_snapshot()

    @staticmethod
    def _owner_from_queue_record(record):
        if not isinstance(record, dict):
            return None
        lease = record.get("lease")
        if not isinstance(lease, dict):
            lease = record.get("last_lease")
        if not isinstance(lease, dict):
            return None
        return {
            key: lease.get(key)
            for key in (
                "owner_id",
                "hostname",
                "pid",
                "process_marker",
                "claimed_at",
                "heartbeat_at",
                "expires_at",
            )
            if lease.get(key) is not None
        }

    def queue_wait(
        self,
        partition,
        *,
        position=None,
        queued_count=None,
        running_head=None,
    ):
        now = self._now()
        partition = str(partition)
        with self._lock:
            job = self._payload.setdefault("queue_jobs", {}).setdefault(
                partition, {}
            )
            job.update(
                {
                    "status": "queued",
                    "position": position,
                    "queued_count": queued_count,
                    "owner": self._owner_from_queue_record(running_head),
                    "updated_at": now,
                }
            )
            owner = job.get("owner") or {}
            owner_text = ""
            if owner:
                owner_text = (
                    " · owner="
                    + str(owner.get("owner_id") or owner.get("pid") or "?")
                )
            diagnostic = (
                f"queue position {position or '?'}; waiting {queued_count or 0}"
                + owner_text
            )
            self._transition_partition_locked(
                partition,
                SHARED_QUEUE_WAIT,
                now,
                diagnostic=diagnostic,
                progress=True,
            )
            self._touch_locked(now)
            self._write_locked(write_latest=True)
        self._notify_snapshot()

    def claimed(self, partition, queue_record=None):
        now = self._now()
        partition = str(partition)
        with self._lock:
            job = self._payload.setdefault("queue_jobs", {}).setdefault(
                partition, {}
            )
            owner = self._owner_from_queue_record(queue_record)
            job.update(
                {
                    "status": "running",
                    "position": 1,
                    "owner": owner,
                    "updated_at": now,
                }
            )
            owner_label = str(
                (owner or {}).get("owner_id")
                or (owner or {}).get("pid")
                or "unknown"
            )
            self._transition_partition_locked(
                partition,
                CLAIMED,
                now,
                diagnostic=f"shared queue lease claimed; owner={owner_label}",
                progress=True,
                heartbeat=True,
            )
            self._append_lifecycle_event_locked(
                "shared_queue_claimed",
                now,
                partition=partition,
                job_id=(self._payload.get("queue_jobs", {}).get(partition, {})
                        .get("job_id")),
                owner_id=owner_label,
            )
            self._touch_locked(now)
            self._write_locked(write_latest=True)
        self._notify_snapshot()

    def _transition_partition_locked(
        self,
        partition,
        stage,
        now,
        *,
        diagnostic=None,
        progress=False,
        output=False,
        heartbeat=False,
    ):
        candidates = self._partition_targets_locked(partition)
        row = next(
            (item for item in candidates if item["stage"] not in TERMINAL_STAGES),
            None,
        )
        if row is None:
            return
        self._transition_target_locked(
            row,
            stage,
            now,
            diagnostic=diagnostic,
            progress=progress,
            output=output,
            heartbeat=heartbeat,
        )

    def transition(
        self,
        target_id,
        stage,
        *,
        diagnostic=None,
        progress=False,
        output=False,
        heartbeat=False,
        terminal_reason=None,
        outcome=None,
    ):
        if stage not in ALL_STAGES:
            raise ValueError(f"unsupported retry progress stage: {stage}")
        now = self._now()
        changed = False
        with self._lock:
            row = self._target_locked(target_id)
            if row is None:
                return False
            if row["stage"] in TERMINAL_STAGES:
                return False
            self._transition_target_locked(
                row,
                stage,
                now,
                diagnostic=diagnostic,
                progress=progress,
                output=output,
                heartbeat=heartbeat,
                terminal_reason=terminal_reason,
                outcome=outcome,
            )
            self._touch_locked(now)
            self._write_locked(write_latest=True)
            changed = True
        if changed:
            self._notify_snapshot()
        return changed

    def _transition_target_locked(
        self,
        row,
        stage,
        now,
        *,
        diagnostic=None,
        progress=False,
        output=False,
        heartbeat=False,
        terminal_reason=None,
        outcome=None,
    ):
        previous = row.get("stage")
        if stage == STALLED and previous != STALLED:
            row["resume_stage"] = previous
        elif previous == STALLED and stage != STALLED:
            row["resume_stage"] = None
        if previous != stage:
            row["stage"] = stage
            row["stage_started_at"] = now
        row["updated_at"] = now
        if progress:
            row["last_progress_at"] = now
        if output:
            row["last_output_at"] = now
        if heartbeat:
            row["last_heartbeat_at"] = now
        if diagnostic is not None:
            row["latest_diagnostic"] = _bounded_diagnostic(diagnostic)
        if stage in TERMINAL_STAGES:
            row["terminal_at"] = now
            row["terminal_reason"] = _bounded_diagnostic(
                terminal_reason or diagnostic or stage
            )
            row["outcome"] = str(outcome or stage)
        elif stage == PENDING_UNREAL:
            row["terminal_at"] = None
            row["terminal_reason"] = None
            row["outcome"] = str(outcome or PENDING_UNREAL)
        self._payload["current_target_id"] = row["target_id"]
        self._payload["stage"] = stage

    def observe_process(
        self,
        target_id,
        *,
        stage=None,
        diagnostic=None,
        output=False,
        progress=False,
    ):
        now = self._now()
        with self._lock:
            row = self._target_locked(target_id)
            if row is None or row["stage"] in TERMINAL_STAGES:
                return False
            next_stage = stage or row["stage"]
            if row["stage"] == STALLED and progress:
                next_stage = row.get("resume_stage") or stage or CLAIMED
            self._transition_target_locked(
                row,
                next_stage,
                now,
                diagnostic=diagnostic,
                progress=progress,
                output=output,
                heartbeat=True,
            )
            self._touch_locked(now)
            self._write_locked(write_latest=True)
        self._notify_snapshot()
        return True

    def mark_partition_terminal(
        self,
        partition,
        stage,
        reason,
        *,
        outcome=None,
        include_unstarted=True,
    ):
        if stage not in TERMINAL_STAGES:
            raise ValueError("partition terminal stage must be terminal")
        now = self._now()
        with self._lock:
            rows = self._partition_targets_locked(partition)
            for row in rows:
                if row["stage"] in TERMINAL_STAGES:
                    continue
                if not include_unstarted and row["stage"] == PLANNING:
                    continue
                self._transition_target_locked(
                    row,
                    stage,
                    now,
                    diagnostic=reason,
                    terminal_reason=reason,
                    outcome=outcome or stage,
                )
            self._touch_locked(now)
            self._finalize_if_terminal_locked(now)
            self._write_locked(write_latest=True)
        self._notify_snapshot()

    def mark_unclassified_terminal(self, target_ids, stage, reason):
        for target_id in target_ids:
            self.transition(
                target_id,
                stage,
                diagnostic=reason,
                terminal_reason=reason,
                outcome=stage,
            )

    def finalize(self, reason=None):
        now = self._now()
        with self._lock:
            self._finalize_if_terminal_locked(now, reason=reason)
            self._touch_locked(now)
            self._write_locked(write_latest=True)
        self._notify_snapshot()

    def _finalize_if_terminal_locked(self, now, reason=None):
        rows = self._targets_locked()
        waiting = [row for row in rows if row.get("stage") == PENDING_UNREAL]
        if waiting and all(
            row.get("stage") in TERMINAL_STAGES or row.get("stage") == PENDING_UNREAL
            for row in rows
        ):
            self._payload["stage"] = PENDING_UNREAL
            self._payload["run_state"] = "waiting"
            self._payload["terminal_outcome"] = None
            self._payload["terminal_reason"] = _bounded_diagnostic(
                reason or PENDING_UNREAL
            )
            self._payload["terminal_at"] = None
            return True
        if rows and not all(row["stage"] in TERMINAL_STAGES for row in rows):
            return False
        outcomes = {row.get("outcome") or row.get("stage") for row in rows}
        if outcomes == {COMPLETE}:
            stage = COMPLETE
        elif OWNER_LOST in outcomes:
            stage = OWNER_LOST
        elif FAILED in outcomes:
            stage = FAILED
        elif BLOCKED in outcomes:
            stage = BLOCKED
        elif CANCELLED in outcomes:
            stage = CANCELLED
        else:
            stage = COMPLETE if not rows else FAILED
        self._payload["stage"] = stage
        self._payload["run_state"] = "terminal"
        self._payload["terminal_outcome"] = stage
        self._payload["terminal_reason"] = _bounded_diagnostic(reason or stage)
        self._payload["terminal_at"] = now
        return True

    def _evaluate_liveness_locked(self, now):
        changed = False
        current_id = self._payload.get("current_target_id")
        current = self._target_locked(current_id) if current_id else None
        for row in (() if current is None else (current,)):
            stage = row.get("stage")
            if stage not in LIVE_STAGES:
                continue
            heartbeat_at = row.get("last_heartbeat_at")
            progress_at = row.get("last_progress_at") or row.get("stage_started_at")
            heartbeat_age = (
                None if heartbeat_at is None else max(0.0, now - heartbeat_at)
            )
            progress_age = max(0.0, now - float(progress_at or now))
            if (
                heartbeat_age is not None
                and heartbeat_age >= self.owner_lost_seconds
            ):
                self._transition_target_locked(
                    row,
                    OWNER_LOST,
                    now,
                    diagnostic=(
                        "owner heartbeat expired; exact queue/process owner lost"
                    ),
                    terminal_reason="owner_lost",
                    outcome=OWNER_LOST,
                )
                changed = True
                continue
            if stage != STALLED and progress_age >= self.stall_warning_seconds:
                self._transition_target_locked(
                    row,
                    STALLED,
                    now,
                    diagnostic=(
                        f"no progress for {int(progress_age)}s; owned process "
                        "still heartbeating; safe cancel available"
                    ),
                    heartbeat=False,
                )
                changed = True
        if changed:
            self._touch_locked(now)
            self._finalize_if_terminal_locked(now)
            self._write_locked(write_latest=True)
        return changed

    def reconcile_queue(self, queue_or_snapshot):
        try:
            snapshot = (
                queue_or_snapshot.snapshot()
                if hasattr(queue_or_snapshot, "snapshot")
                else copy.deepcopy(queue_or_snapshot)
            )
        except Exception:
            return False
        jobs_by_id = {
            str(job.get("id")): job
            for job in (snapshot or {}).get("jobs", [])
            if isinstance(job, dict) and job.get("id")
        }
        changed = False
        with self._lock:
            for partition, receipt_job in (
                self._payload.get("queue_jobs") or {}
            ).items():
                job = jobs_by_id.get(str(receipt_job.get("job_id") or ""))
                if job is None:
                    continue
                queue_status = job.get("status")
                queue_owner = self._owner_from_queue_record(job)
                if (
                    receipt_job.get("status") != queue_status
                    or receipt_job.get("owner") != queue_owner
                ):
                    changed = True
                receipt_job["status"] = queue_status
                receipt_job["owner"] = queue_owner
                receipt_job["updated_at"] = self._now()
                if queue_status == "running" and queue_owner:
                    heartbeat_at = queue_owner.get("heartbeat_at")
                    candidates = self._partition_targets_locked(partition)
                    row = next(
                        (
                            item
                            for item in candidates
                            if item.get("stage") not in TERMINAL_STAGES
                        ),
                        None,
                    )
                    if row is not None:
                        if row.get("stage") == SHARED_QUEUE_WAIT:
                            self._transition_target_locked(
                                row,
                                CLAIMED,
                                self._now(),
                                diagnostic="restored exact shared queue lease",
                            )
                            changed = True
                        if isinstance(heartbeat_at, (int, float)) and (
                            row.get("last_heartbeat_at") is None
                            or float(heartbeat_at)
                            > float(row.get("last_heartbeat_at"))
                        ):
                            row["last_heartbeat_at"] = float(heartbeat_at)
                            row["updated_at"] = self._now()
                            changed = True
                if job.get("status") == "failed" and (
                    job.get("failure_reason") == OWNER_LOST
                    or isinstance(job.get("last_expired_lease"), dict)
                ):
                    partition_changed = False
                    for row in self._partition_targets_locked(partition):
                        if row["stage"] in TERMINAL_STAGES:
                            continue
                        self._transition_target_locked(
                            row,
                            OWNER_LOST,
                            self._now(),
                            diagnostic="shared queue lease owner lost",
                            terminal_reason=OWNER_LOST,
                            outcome=OWNER_LOST,
                        )
                        changed = True
                        partition_changed = True
                    if partition_changed:
                        self._append_lifecycle_event_locked(
                            "owner_lost_reconciled",
                            self._now(),
                            detail=(
                                "shared queue recorded owner_lost after the "
                                "last durable retry observation"
                            ),
                            partition=partition,
                            job_id=str(receipt_job.get("job_id") or ""),
                            after_operator_app_close=any(
                                event.get("kind") == "operator_app_close"
                                for event in self._payload.get(
                                    "lifecycle_events", []
                                )
                                if isinstance(event, dict)
                            ),
                        )
                elif job.get("status") == "cancelled":
                    for row in self._partition_targets_locked(partition):
                        if row["stage"] in TERMINAL_STAGES:
                            continue
                        self._transition_target_locked(
                            row,
                            CANCELLED,
                            self._now(),
                            diagnostic=str(
                                job.get("cancel_reason") or "queue cancelled"
                            ),
                            terminal_reason=CANCELLED,
                            outcome=CANCELLED,
                        )
                        changed = True
                elif job.get("status") in {"completed", "failed"}:
                    result = job.get("result")
                    if not isinstance(result, dict):
                        continue
                    result_rows = {
                        str(row.get("target")): row
                        for row in result.get("target_outcomes") or []
                        if isinstance(row, dict) and row.get("target")
                    }
                    for row in self._partition_targets_locked(partition):
                        if row["stage"] in TERMINAL_STAGES:
                            continue
                        outcome_row = result_rows.get(row["target_id"])
                        outcome = str(
                            (outcome_row or {}).get("outcome") or ""
                        )
                        reason = str(
                            (outcome_row or {}).get("reason_token")
                            or result.get("error")
                            or outcome
                            or result.get("outcome")
                            or job.get("status")
                        )
                        if outcome == "completed" or (
                            not outcome
                            and result.get("outcome") == "completed"
                        ):
                            self._transition_target_locked(
                                row,
                                COMPLETE,
                                self._now(),
                                diagnostic="restored from shared queue result",
                                terminal_reason="completed",
                                outcome=COMPLETE,
                            )
                        elif outcome in {
                            "pending_unreal",
                            "exported_pending_unreal",
                        }:
                            self._transition_target_locked(
                                row,
                                PENDING_UNREAL,
                                self._now(),
                                diagnostic=reason,
                                outcome=PENDING_UNREAL,
                            )
                        elif outcome in {"cancelled", "stopped"}:
                            self._transition_target_locked(
                                row,
                                CANCELLED,
                                self._now(),
                                diagnostic=reason,
                                terminal_reason=reason,
                                outcome=CANCELLED,
                            )
                        elif outcome == "owner_lost":
                            self._transition_target_locked(
                                row,
                                OWNER_LOST,
                                self._now(),
                                diagnostic=reason,
                                terminal_reason=reason,
                                outcome=OWNER_LOST,
                            )
                        elif outcome in {"blocked", "planned_excluded"}:
                            self._transition_target_locked(
                                row,
                                BLOCKED,
                                self._now(),
                                diagnostic=reason,
                                terminal_reason=reason,
                                outcome=BLOCKED,
                            )
                        elif result.get("outcome") == "stopped":
                            self._transition_target_locked(
                                row,
                                CANCELLED,
                                self._now(),
                                diagnostic=reason,
                                terminal_reason=reason,
                                outcome=CANCELLED,
                            )
                        else:
                            self._transition_target_locked(
                                row,
                                FAILED,
                                self._now(),
                                diagnostic=reason,
                                terminal_reason=reason,
                                outcome=FAILED,
                            )
                        changed = True
            if changed:
                now = self._now()
                self._touch_locked(now)
                self._finalize_if_terminal_locked(now)
                self._write_locked(write_latest=True)
        if changed:
            self._notify_snapshot()
        return changed

    def snapshot(self, *, evaluate=True):
        now = self._now()
        notify = False
        with self._lock:
            if evaluate:
                notify = self._evaluate_liveness_locked(now)
            payload = copy.deepcopy(self._payload)
        planning = payload.get("planning")
        if isinstance(planning, dict):
            plan_cache = planning.get("plan_cache")
            if isinstance(plan_cache, dict):
                artifact = plan_cache.get("artifact") or {}
                planning["plan_cache"] = {
                    key: copy.deepcopy(plan_cache.get(key))
                    for key in (
                        "schema_version",
                        "input_signature",
                        "side_effects_committed",
                        "stored_at",
                    )
                }
                planning["plan_cache"]["job_count"] = len(
                    artifact.get("jobs") or ()
                )
            planning_started = planning.get("started_at")
            planning_end = (
                planning.get("committed_at")
                or planning.get("terminal_at")
                or planning.get("plan_ready_at")
                if planning.get("status") in {
                    "committed",
                    "cancelled",
                    "failed",
                    "owner_lost",
                    "ready",
                }
                else now
            )
            planning["wall_elapsed_seconds"] = (
                None
                if not isinstance(planning_started, (int, float))
                else max(0.0, float(planning_end or now) - float(planning_started))
            )
            heartbeat_at = planning.get("heartbeat_at")
            heartbeat_age = (
                None
                if not isinstance(heartbeat_at, (int, float))
                else max(0.0, now - float(heartbeat_at))
            )
            progress_at = planning.get("progress_at")
            progress_age = (
                None
                if not isinstance(progress_at, (int, float))
                else max(0.0, now - float(progress_at))
            )
            planning["heartbeat_age_seconds"] = heartbeat_age
            planning["progress_age_seconds"] = progress_age
            status = str(planning.get("status") or "unowned")
            if status in {"cancelled", "failed", "owner_lost"}:
                liveness_state = status
            elif status == "ready":
                liveness_state = "plan_ready"
            elif status == "committing":
                liveness_state = "commit_in_progress"
            elif status == "committed":
                liveness_state = "committed"
            elif planning.get("owner_alive") is False:
                liveness_state = OWNER_LOST
            elif heartbeat_age is None:
                liveness_state = "owner_unknown"
            elif heartbeat_age >= self.owner_lost_seconds:
                liveness_state = STALLED
            else:
                liveness_state = "heartbeat_live"
            planning["liveness_state"] = liveness_state
            planning["progress_state"] = (
                "unknown"
                if progress_age is None
                else STALLED
                if progress_age >= self.stall_warning_seconds
                else "recent"
            )
        for row in payload.get("targets", []):
            row_end = row.get("terminal_at")
            if not isinstance(row_end, (int, float)):
                row_end = now
            row["wall_elapsed_seconds"] = max(
                0.0, float(row_end) - float(row.get("started_at") or now)
            )
            # Schema-1 readers keep the old field; the UI labels it as wall
            # time and never treats its increase as execution evidence.
            row["elapsed_seconds"] = row["wall_elapsed_seconds"]
            for field, output in (
                ("last_progress_at", "last_progress_age_seconds"),
                ("last_output_at", "last_output_age_seconds"),
                ("last_heartbeat_at", "last_heartbeat_age_seconds"),
            ):
                value = row.get(field)
                row[output] = (
                    None if value is None else max(0.0, now - float(value))
                )
            if (
                isinstance(planning, dict)
                and (
                    row.get("stage") == PLANNING
                    or (
                        row.get("stage") == STALLED
                        and row.get("resume_stage") == PLANNING
                    )
                )
            ):
                row["last_heartbeat_age_seconds"] = planning.get(
                    "heartbeat_age_seconds"
                )
                row["last_progress_age_seconds"] = planning.get(
                    "progress_age_seconds"
                )
        current = next(
            (
                row
                for row in payload.get("targets", [])
                if row.get("target_id") == payload.get("current_target_id")
            ),
            None,
        )
        if payload.get("terminal_at") is not None:
            payload["evidence_state"] = "terminal"
        elif current is None:
            payload["evidence_state"] = "idle"
        elif (
            isinstance(planning, dict)
            and (
                current.get("stage") == PLANNING
                or current.get("resume_stage") == PLANNING
            )
        ):
            payload["evidence_state"] = planning.get("liveness_state")
        elif current.get("stage") == STALLED:
            payload["evidence_state"] = STALLED
        elif current.get("last_heartbeat_age_seconds") is None:
            payload["evidence_state"] = "heartbeat_unknown"
        elif (
            current.get("last_heartbeat_age_seconds")
            >= self.owner_lost_seconds
        ):
            payload["evidence_state"] = OWNER_LOST
        else:
            payload["evidence_state"] = "heartbeat_live"
        if notify:
            self._notify_snapshot()
        return payload


def stage_for_send2ue_marker(line, current_stage=None):
    """Map existing bounded child markers to operator retry stages."""
    value = str(line or "")
    if value.startswith("SK_BATCH_SEND2UE_DISK_EXPORT_START"):
        return SEND2UE
    if value.startswith("SK_BATCH_SEND2UE_DISK_EXPORT_DONE"):
        return POST_CHECK
    if value.startswith("SK_BATCH_SEND2UE_RPC_OWNED_START"):
        return UNREAL
    if value.startswith("SK_BATCH_SEND2UE_RPC_OWNED_DONE"):
        return POST_CHECK
    if value.startswith("SK_BATCH_SEND2UE_JOB_DONE"):
        return POST_CHECK
    return current_stage


__all__ = [
    "ALL_STAGES",
    "BLENDER",
    "BLOCKED",
    "CANCELLED",
    "CLAIMED",
    "COMPLETE",
    "FAILED",
    "OWNER_LOST",
    "PENDING_UNREAL",
    "PLANNING",
    "POST_CHECK",
    "RetryProgressReceipt",
    "SEND2UE",
    "SHARED_QUEUE_WAIT",
    "STALLED",
    "TERMINAL_STAGES",
    "UNREAL",
    "default_receipt_dir",
    "stage_for_send2ue_marker",
]
