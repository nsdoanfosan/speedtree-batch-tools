"""Bounded, instrumented snapshot support for failed-retry planning.

This module owns only planner throughput and observability.  Retry execution
liveness and dead-owner reconciliation remain in :mod:`retry_progress`.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import threading
import time
from collections import OrderedDict, defaultdict
from contextlib import contextmanager
from pathlib import Path


MAX_REPORT_BYTES = 64 * 1024 * 1024
MAX_CACHED_JSON_BYTES = 96 * 1024 * 1024
MAX_CACHED_JSON_ENTRIES = 64
STATE_SNAPSHOT_WAIT_SECONDS = 2.0
STATE_SNAPSHOT_POLL_SECONDS = 0.05
PLAN_CACHE_SCHEMA_VERSION = 1


def _cache_json_value(value):
    """Return a deterministic JSON value without losing container types."""

    if hasattr(value, "metadata") and callable(value.metadata):
        return _cache_json_value(value.metadata())
    if isinstance(value, Path):
        return {"__retry_cache_type__": "path", "value": str(value)}
    if isinstance(value, dict):
        return {
            str(key): _cache_json_value(child)
            for key, child in sorted(value.items(), key=lambda item: str(item[0]))
        }
    if isinstance(value, tuple):
        return {
            "__retry_cache_type__": "tuple",
            "items": [_cache_json_value(child) for child in value],
        }
    if isinstance(value, set):
        items = [_cache_json_value(child) for child in value]
        items.sort(key=lambda item: json.dumps(item, sort_keys=True, default=str))
        return {"__retry_cache_type__": "set", "items": items}
    if isinstance(value, list):
        return [_cache_json_value(child) for child in value]
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if value != value or value in {float("inf"), float("-inf")}:
            return {"__retry_cache_type__": "float", "value": repr(value)}
        return value
    raise TypeError(f"unsupported retry plan cache value: {type(value).__name__}")


def _restore_cache_json_value(value):
    if isinstance(value, list):
        return [_restore_cache_json_value(child) for child in value]
    if not isinstance(value, dict):
        return value
    kind = value.get("__retry_cache_type__")
    if kind == "path" and set(value) == {"__retry_cache_type__", "value"}:
        return Path(str(value["value"]))
    if kind in {"tuple", "set"} and set(value) == {
        "__retry_cache_type__",
        "items",
    }:
        items = [_restore_cache_json_value(child) for child in value["items"]]
        return tuple(items) if kind == "tuple" else set(items)
    if kind == "float" and set(value) == {"__retry_cache_type__", "value"}:
        return float(str(value["value"]))
    return {
        str(key): _restore_cache_json_value(child)
        for key, child in value.items()
    }


def _collect_file_paths(value, paths):
    if isinstance(value, Path):
        paths.add(_canonical_path(value))
        return
    if isinstance(value, dict):
        for child in value.values():
            _collect_file_paths(child, paths)
        return
    if isinstance(value, (list, tuple, set)):
        for child in value:
            _collect_file_paths(child, paths)
        return
    if isinstance(value, str) and os.path.isabs(value):
        paths.add(_canonical_path(value))


def _collect_atlas_planning_paths(target_ids, paths):
    """Include every resolver/canonical sibling that can change a decision."""

    for value in target_ids:
        target = Path(value).expanduser().absolute()
        target_dir = target.parent / ".atlas_leaf_speedtree_targets"
        scope_dir = target.parent / ".atlas_leaf_speedtree_scopes"
        candidates = []
        if target_dir.is_dir():
            candidates.extend(target_dir.glob("*.json"))
        if scope_dir.is_dir():
            candidates.extend(scope_dir.glob(f"*__{target.stem}.json"))
        candidates.extend((
            target.parent / "speedtree_import_manifest.json",
            target.parent / "texture" / "pcg_st9_canonical_outputs.json",
            target.parent / "textures" / "pcg_st9_canonical_outputs.json",
        ))
        for candidate in candidates:
            if candidate.is_file():
                paths.add(_canonical_path(candidate))


def planning_input_signature(
    target_ids,
    state_snapshot,
    cfg_snapshot,
    inventory_snapshot,
):
    """Fingerprint the exact planner inputs and referenced file identities.

    This intentionally uses metadata only for assets. Planning still performs
    its authoritative content reads on a cache miss.
    """

    ordered_ids = tuple(str(value) for value in target_ids)
    state_rows = {
        target_id: copy.deepcopy((state_snapshot or {}).get(target_id, {}))
        for target_id in ordered_ids
    }
    payload = _cache_json_value({
        "target_ids": ordered_ids,
        "state": state_rows,
        "cfg": dict(cfg_snapshot or {}),
        "inventory": dict(inventory_snapshot or {}),
    })
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    paths = set()
    _collect_file_paths(state_rows, paths)
    _collect_file_paths(inventory_snapshot or {}, paths)
    _collect_atlas_planning_paths(ordered_ids, paths)
    identities = []
    for path in sorted(paths):
        try:
            identities.append(list(_stat_identity(path)))
        except OSError:
            identities.append([path, None, None, None, None])
    identities_encoded = json.dumps(
        identities,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return {
        "schema_version": PLAN_CACHE_SCHEMA_VERSION,
        "snapshot_sha256": hashlib.sha256(encoded).hexdigest(),
        "file_identities_sha256": hashlib.sha256(
            identities_encoded
        ).hexdigest(),
        "referenced_file_count": len(identities),
    }


_PLAN_JOB_FIELDS = frozenset({
    "label",
    "mode",
    "phase",
    "terminal_phase",
    "selected_scope",
    "force_rerun",
    "push_transport",
    "recovery_requests",
    "repair_plans",
    "resume_after_repairs",
    "retry_metadata",
})


def build_plan_cache_artifact(plan, tracker_snapshot):
    """Reduce one executable plan to a receipt-safe immutable blueprint."""

    jobs = []
    scheduled = set()
    for job in plan.get("jobs") or ():
        target_ids = [str(item["spm"]) for item in job.get("targets") or ()]
        scheduled.update(target_ids)
        fields = {
            key: copy.deepcopy(job[key])
            for key in _PLAN_JOB_FIELDS
            if key in job
        }
        jobs.append({
            "target_ids": target_ids,
            "fields": _cache_json_value(fields),
        })
    terminal_results = []
    for row in (tracker_snapshot or {}).get("targets") or ():
        target_id = str(row.get("target_id") or "")
        if not target_id or target_id in scheduled:
            continue
        stage = str(row.get("stage") or "")
        if stage not in {"blocked", "owner_lost", "cancelled", "complete", "failed"}:
            raise ValueError(
                f"unscheduled retry target is not terminal: {target_id}: {stage}"
            )
        terminal_results.append({
            "target_id": target_id,
            "stage": stage,
            "diagnostic": str(row.get("latest_diagnostic") or ""),
            "terminal_reason": str(row.get("terminal_reason") or ""),
            "outcome": str(row.get("outcome") or stage),
        })
    return {
        "schema_version": PLAN_CACHE_SCHEMA_VERSION,
        "selected_iids": [str(value) for value in plan.get("selected_iids") or ()],
        "jobs": jobs,
        "terminal_results": terminal_results,
        "skipped": _cache_json_value(list(plan.get("skipped") or ())),
        "deferred_status_updates": _cache_json_value(
            list(plan.get("deferred_status_updates") or ())
        ),
        "deferred_logs": _cache_json_value(
            list(plan.get("deferred_logs") or ())
        ),
    }


def hydrate_plan_cache_artifact(
    artifact,
    *,
    inventory_snapshot,
    cfg_snapshot,
    tracker,
    side_effects_committed,
):
    """Rebind a validated blueprint to a new tracker and live snapshot."""

    if not isinstance(artifact, dict) or artifact.get("schema_version") != 1:
        raise ValueError("unsupported retry plan cache artifact")
    inventory = copy.deepcopy(dict(inventory_snapshot or {}))
    jobs = []
    partitions = []
    for blueprint in artifact.get("jobs") or ():
        target_ids = [str(value) for value in blueprint.get("target_ids") or ()]
        missing = [target_id for target_id in target_ids if target_id not in inventory]
        if missing:
            raise ValueError("retry plan cache target disappeared")
        fields = _restore_cache_json_value(blueprint.get("fields") or {})
        if not isinstance(fields, dict):
            raise ValueError("retry plan cache job fields are invalid")
        job = dict(fields)
        job.update({
            "targets": [inventory[target_id] for target_id in target_ids],
            "inventory": inventory,
            "cfg": dict(cfg_snapshot or {}),
            "_retry_progress_tracker": tracker,
        })
        metadata = dict(job.get("retry_metadata") or {})
        partition = str(metadata.get("partition") or "")
        execution_path = str(metadata.get("execution_path") or "")
        if not partition or not execution_path:
            raise ValueError("retry plan cache partition is incomplete")
        metadata.update({
            "progress_run_id": tracker.run_id,
            "progress_receipt_path": str(tracker.path),
            "plan_cache_reused": True,
        })
        job["retry_metadata"] = metadata
        partitions.append((partition, target_ids, execution_path))
        jobs.append(job)
    terminal_results = list(artifact.get("terminal_results") or ())
    for result in terminal_results:
        if str(result.get("target_id") or "") not in inventory:
            raise ValueError("retry plan cache terminal target disappeared")
        if str(result.get("stage") or "") not in {
            "blocked", "owner_lost", "cancelled", "complete", "failed"
        }:
            raise ValueError("retry plan cache terminal result is invalid")
    for partition, target_ids, execution_path in partitions:
        tracker.assign_partition(partition, target_ids, execution_path)
    for result in terminal_results:
        tracker.transition(
            str(result["target_id"]),
            str(result["stage"]),
            diagnostic=str(result.get("diagnostic") or "cached classification"),
            terminal_reason=str(result.get("terminal_reason") or "cached classification"),
            outcome=str(result.get("outcome") or result["stage"]),
        )
    return {
        "jobs": jobs,
        "skipped": _restore_cache_json_value(artifact.get("skipped") or []),
        "selected_iids": [str(value) for value in artifact.get("selected_iids") or ()],
        "cfg": dict(cfg_snapshot or {}),
        "tracker": tracker,
        "deferred_status_updates": (
            []
            if side_effects_committed
            else _restore_cache_json_value(
                artifact.get("deferred_status_updates") or []
            )
        ),
        "deferred_logs": (
            []
            if side_effects_committed
            else _restore_cache_json_value(artifact.get("deferred_logs") or [])
        ),
    }


class RetryPlanningCancelled(RuntimeError):
    """The operator cancelled between bounded planning units."""


class RetryPlanningSnapshotError(RuntimeError):
    """A stable bounded planning snapshot could not be captured."""


def _canonical_path(path):
    return os.path.normcase(os.path.abspath(os.path.normpath(str(path)))).casefold()


def _stat_identity(path):
    candidate = Path(path)
    stat = candidate.stat()
    return (
        _canonical_path(candidate),
        int(stat.st_size),
        int(stat.st_mtime_ns),
        int(getattr(stat, "st_ctime_ns", 0)),
        int(getattr(stat, "st_ino", 0)),
    )


class StableJsonCache:
    """LRU JSON cache keyed by stable path/stat/content identity."""

    def __init__(
        self,
        counters,
        *,
        max_entries=MAX_CACHED_JSON_ENTRIES,
        max_cached_bytes=MAX_CACHED_JSON_BYTES,
        evict=True,
    ):
        self._counters = counters
        self.max_entries = max(1, int(max_entries))
        self.max_cached_bytes = max(1, int(max_cached_bytes))
        self.evict = bool(evict)
        self._entries = OrderedDict()
        self._stat_index = {}
        self._cached_bytes = 0

    def _record(self, name, amount=1):
        self._counters[str(name)] += int(amount)

    def load(self, path, *, namespace="json", max_bytes=MAX_REPORT_BYTES):
        candidate = Path(path).expanduser().absolute()
        before = _stat_identity(candidate)
        if before[1] > int(max_bytes):
            raise RetryPlanningSnapshotError(
                f"planning JSON exceeds {int(max_bytes)} bytes: {candidate}"
            )
        existing_key = self._stat_index.get((str(namespace), before))
        if existing_key is not None and existing_key in self._entries:
            entry = self._entries.pop(existing_key)
            self._entries[existing_key] = entry
            self._record("cache_hits")
            self._record(f"{namespace}_cache_hits")
            return entry["payload"]

        self._record("cache_misses")
        self._record(f"{namespace}_cache_misses")
        raw = candidate.read_bytes()
        after = _stat_identity(candidate)
        if before != after or len(raw) != before[1]:
            raise RetryPlanningSnapshotError(
                f"planning JSON changed while being read: {candidate}"
            )
        digest = hashlib.sha256(raw).hexdigest()
        identity = (str(namespace), before, digest)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise RetryPlanningSnapshotError(
                f"planning JSON could not be parsed: {candidate}: {exc}"
            ) from exc
        self._record("file_reads")
        self._record("bytes_read", len(raw))
        self._record("json_parses")
        if not self.evict and (
            len(self._entries) + 1 > self.max_entries
            or self._cached_bytes + len(raw) > self.max_cached_bytes
        ):
            self._record("cache_budget_rejections")
            raise RetryPlanningSnapshotError(
                f"planning JSON cache budget exceeded: {candidate}"
            )
        self._entries[identity] = {
            "payload": payload,
            "size": len(raw),
            "path": str(candidate),
        }
        self._stat_index[(str(namespace), before)] = identity
        self._cached_bytes += len(raw)
        while self.evict and (
            len(self._entries) > self.max_entries
            or self._cached_bytes > self.max_cached_bytes
        ):
            evicted_key, evicted = self._entries.popitem(last=False)
            self._cached_bytes -= int(evicted["size"])
            stat_key = (evicted_key[0], evicted_key[1])
            if self._stat_index.get(stat_key) == evicted_key:
                self._stat_index.pop(stat_key, None)
            self._record("cache_evictions")
        return payload

    def identity(self, path, *, namespace="json"):
        stat_key = (str(namespace), _stat_identity(Path(path).expanduser().absolute()))
        return self._stat_index.get(stat_key)


def _has_structured_failure(entry):
    for column in ("push_status", "blend_status", "spm_status"):
        error = entry.get(f"{column}_error")
        if isinstance(error, dict) and error:
            return True
    automation = entry.get("failed_retry_automation")
    if isinstance(automation, dict):
        status = str(automation.get("status") or "")
        if status and status != "automatic_repair_completed":
            return True
    return False


def cheap_durable_candidate(entry, *, live_identity_current=False):
    """Exclude only an unambiguous durable success; ambiguity stays eligible.

    This is intentionally conservative.  A current Push success with any
    structured phase failure still receives fresh authoritative validation.
    Missing, legacy, cancelled, pending-Unreal, and unknown rows also remain in
    the candidate set, preserving complete-inventory fail-closed behavior.
    """

    entry = entry if isinstance(entry, dict) else {}
    automation = entry.get("failed_retry_automation")
    automation_status = str(
        automation.get("status") if isinstance(automation, dict) else ""
    )
    push_kind = str(entry.get("push_status_kind") or "").casefold()
    completed = (
        automation_status == "automatic_repair_completed"
        or push_kind in {"completed", "imported_ok", "ready"}
    )
    if (
        completed
        and live_identity_current is True
        and not _has_structured_failure(entry)
    ):
        return False, "durable_current_success"
    return True, "fresh_authoritative_validation_required"


class RetryPlanningContext:
    """One immutable state generation, bounded caches, and planner metrics."""

    def __init__(
        self,
        *,
        target_ids,
        state_snapshot,
        cfg_snapshot,
        inventory_snapshot,
        cancel_event=None,
        tracker=None,
        planning_session_id=None,
        clock=None,
    ):
        self.target_ids = tuple(str(value) for value in target_ids)
        self.state_snapshot = state_snapshot
        self.cfg_snapshot = copy.deepcopy(dict(cfg_snapshot or {}))
        self.inventory_snapshot = copy.deepcopy(dict(inventory_snapshot or {}))
        self.input_signature = planning_input_signature(
            self.target_ids,
            self.state_snapshot,
            self.cfg_snapshot,
            self.inventory_snapshot,
        )
        self.cancel_event = cancel_event
        self.tracker = tracker
        self.planning_session_id = (
            None
            if planning_session_id in (None, "")
            else str(planning_session_id)
        )
        self._clock = clock or time.perf_counter
        self.started_at = self._clock()
        self.counters = defaultdict(int)
        self.spans = defaultdict(float)
        self.report_cache = StableJsonCache(
            self.counters,
            max_entries=64,
            max_cached_bytes=MAX_CACHED_JSON_BYTES,
            evict=True,
        )
        # At most two parent JSON files per inventory row are addressable.
        # Refuse an oversized snapshot instead of evicting and reparsing the
        # same immutable parent identity later in the run.
        self.parent_cache = StableJsonCache(
            self.counters,
            max_entries=max(2, len(self.target_ids) * 2),
            max_cached_bytes=MAX_CACHED_JSON_BYTES,
            evict=False,
        )
        self.current_substage = "snapshot"
        self.scanned_count = 0
        self.substage_total = len(self.target_ids)
        self.last_completed_unit = ""
        self._last_published_substage = ""
        self._last_published_scanned = -1
        self._last_publish_at = 0.0

    @classmethod
    def capture(
        cls,
        *,
        target_ids,
        state,
        state_lock,
        cfg_snapshot,
        inventory_snapshot,
        cancel_event=None,
        tracker=None,
        planning_session_id=None,
        wait_seconds=STATE_SNAPSHOT_WAIT_SECONDS,
    ):
        target_ids = tuple(str(value) for value in target_ids)
        started = time.perf_counter()
        deadline = started + max(0.05, float(wait_seconds))
        acquired = False
        while not acquired:
            if cancel_event is not None and cancel_event.is_set():
                raise RetryPlanningCancelled("operator cancelled retry planning")
            remaining = deadline - time.perf_counter()
            if remaining <= 0:
                raise RetryPlanningSnapshotError(
                    "retry planning state snapshot wait exceeded bounded timeout"
                )
            if tracker is not None and target_ids:
                tracker.transition(
                    target_ids[0],
                    "planning",
                    diagnostic=(
                        "planning snapshot_wait · scanned 0/"
                        f"{len(target_ids)} · cache hit 0/miss 0"
                    ),
                    progress=False,
                    heartbeat=True,
                )
            acquired = state_lock.acquire(
                timeout=min(STATE_SNAPSHOT_POLL_SECONDS, remaining)
            )
        try:
            snapshot = copy.deepcopy(dict(state or {}))
        finally:
            state_lock.release()
        context = cls(
            target_ids=target_ids,
            state_snapshot=snapshot,
            cfg_snapshot=cfg_snapshot,
            inventory_snapshot=inventory_snapshot,
            cancel_event=cancel_event,
            tracker=tracker,
            planning_session_id=planning_session_id,
        )
        context.spans["snapshot_commit"] += time.perf_counter() - started
        context.counters["snapshot_rows"] = len(snapshot)
        context.publish("snapshot", scanned=0, last_completed="state snapshot")
        return context

    def entry(self, target_id):
        value = self.state_snapshot.get(str(target_id), {})
        return value if isinstance(value, dict) else {}

    def check_cancel(self):
        if self.cancel_event is not None and self.cancel_event.is_set():
            raise RetryPlanningCancelled("operator cancelled retry planning")

    @contextmanager
    def span(self, name):
        self.check_cancel()
        started = self._clock()
        try:
            yield
        finally:
            self.spans[str(name)] += max(0.0, self._clock() - started)

    def load_json(self, path, *, namespace="json", max_bytes=MAX_REPORT_BYTES):
        self.check_cancel()
        cache = (
            self.parent_cache
            if str(namespace).startswith("parent_")
            else self.report_cache
        )
        return cache.load(
            path,
            namespace=namespace,
            max_bytes=max_bytes,
        )

    def publish(
        self,
        substage,
        *,
        current_target=None,
        scanned=None,
        last_completed=None,
        progress=True,
        force=False,
        total=None,
        cache_status=None,
    ):
        self.check_cancel()
        self.current_substage = str(substage)
        if scanned is not None:
            self.scanned_count = max(0, min(len(self.target_ids), int(scanned)))
        if total is not None:
            self.substage_total = max(0, int(total))
        else:
            self.substage_total = len(self.target_ids)
        if last_completed is not None:
            self.last_completed_unit = Path(str(last_completed)).name
        current = str(
            current_target
            or (
                self.target_ids[min(self.scanned_count, len(self.target_ids) - 1)]
                if self.target_ids
                else ""
            )
        )
        diagnostic = (
            f"planning {self.current_substage} · scanned "
            f"{self.scanned_count}/{len(self.target_ids)} · cache hit "
            f"{self.counters['cache_hits']}/miss {self.counters['cache_misses']}"
        )
        if self.last_completed_unit:
            diagnostic += f" · last {self.last_completed_unit}"
        now = self._clock()
        should_publish = bool(
            force
            or self.current_substage != self._last_published_substage
            or self.scanned_count >= len(self.target_ids)
            or self.scanned_count - self._last_published_scanned >= 8
            or now - self._last_publish_at >= 0.25
        )
        if self.tracker is not None and current and should_publish:
            if self.planning_session_id is not None:
                self.tracker.planning_progress(
                    self.planning_session_id,
                    substage=self.current_substage,
                    completed_count=self.scanned_count,
                    total_count=self.substage_total,
                    current_target_id=current,
                    last_completed_unit=self.last_completed_unit,
                    classified_count=self.counters["classifications"],
                    validated_count=self.counters["repair_state_validations"],
                    cache_hits=self.counters["cache_hits"],
                    cache_misses=self.counters["cache_misses"],
                    cache_status=cache_status,
                    progress=bool(progress),
                )
            else:
                self.tracker.transition(
                    current,
                    "planning",
                    diagnostic=diagnostic,
                    progress=bool(progress),
                    heartbeat=True,
                )
            self._last_published_substage = self.current_substage
            self._last_published_scanned = self.scanned_count
            self._last_publish_at = now

    def diagnostics(self):
        return {
            "schema_version": 1,
            "substage": self.current_substage,
            "scanned_count": self.scanned_count,
            "target_count": len(self.target_ids),
            "substage_total": self.substage_total,
            "last_completed_unit": self.last_completed_unit,
            "wall_seconds": max(0.0, self._clock() - self.started_at),
            "counters": {
                key: int(value)
                for key, value in sorted(self.counters.items())
            },
            "spans_seconds": {
                key: round(float(value), 6)
                for key, value in sorted(self.spans.items())
            },
        }


__all__ = (
    "MAX_REPORT_BYTES",
    "PLAN_CACHE_SCHEMA_VERSION",
    "RetryPlanningCancelled",
    "RetryPlanningContext",
    "RetryPlanningSnapshotError",
    "StableJsonCache",
    "build_plan_cache_artifact",
    "cheap_durable_candidate",
    "hydrate_plan_cache_artifact",
    "planning_input_signature",
)
