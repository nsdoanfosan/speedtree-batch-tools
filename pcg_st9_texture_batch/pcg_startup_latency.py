"""Phase receipts for PCG ST9 Texture usable-ready startup latency."""
from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    from pcg_startup_cache import atomic_write_json
    from pcg_texture_common import SHARED_CACHE_DIR
except ImportError:
    from .pcg_startup_cache import atomic_write_json
    from .pcg_texture_common import SHARED_CACHE_DIR


STARTUP_LATENCY_SCHEMA_VERSION = 2
STARTUP_LATENCY_RECEIPT_PATH = SHARED_CACHE_DIR / "pcg_startup_latency_latest.json"
STARTUP_PHASE_ORDER = (
    "cached_board_paint",
    "primary_live_audit",
    "blender_relations",
    "sync_migration",
)
# Runtime budgets are diagnostic (never permission to skip fail-closed work).
STARTUP_PHASE_BUDGET_SECONDS = {
    "cached_board_paint": 0.50,
    "primary_live_audit": 30.0,
    "blender_relations": 10.0,
    "sync_migration": 5.0,
}
PRODUCTION_FIXTURE_LATENCY_BUDGET_SECONDS = {
    # CI fixture mirrors the current 55-folder / 597-SPM fleet cardinality.
    # Files are intentionally tiny, so these budgets guard algorithmic/I/O
    # amplification rather than pretending to predict OneDrive wall time.
    "cold_total": 10.0,
    "warm_total": 6.0,
    "cold_usable_ready": 15.0,
    "warm_usable_ready": 8.0,
    "cached_board_paint": 0.25,
}
# Product acceptance adds this end-to-end ceiling without replacing any
# tighter phase/cardinality fixture budget above.
USABLE_READY_ACCEPTANCE_CAP_SECONDS = 30.0
STARTUP_TOTAL_INVOCATION_GUARD_SCHEMA_VERSION = 1
STARTUP_TOTAL_INVOCATION_RULES = {
    "atlas_manifest_resolution_calls": {
        "cardinality": "audit_scope_count",
        "per_item_limit": 1,
    },
    "spm_analysis_calls": {
        "cardinality": "spm_count",
        # The 597-SPM fixture currently uses 18,837 calls (31.55/file).
        # Keep enough headroom for ordinary call-graph refactors while still
        # rejecting the hundreds-per-file amplification this guard targets.
        "per_item_limit": 48,
    },
    "legacy_receipt_inspection_calls": {
        "cardinality": "spm_count",
        "per_item_limit": 1,
    },
}


class StartupAmplificationError(AssertionError):
    """A deterministic startup total-call bound was exceeded."""


def startup_total_invocation_guard(
    metrics,
    *,
    audit_scope_count,
    spm_count,
):
    """Compare expensive-operation totals with fleet-derived bounds.

    Counts are intentionally total invocations, not unique-path counts.  This
    makes per-call amplification visible even when every unique-file metric is
    unchanged.  The limits encode the current primary-audit call graph rather
    than elapsed time, so runner load cannot change the verdict.
    """
    cardinalities = {
        "audit_scope_count": int(audit_scope_count),
        "spm_count": int(spm_count),
    }
    if any(value < 0 for value in cardinalities.values()):
        raise ValueError("startup guard cardinalities must be non-negative")

    rules = {}
    violations = []
    for metric, rule in STARTUP_TOTAL_INVOCATION_RULES.items():
        cardinality = rule["cardinality"]
        actual = int((metrics or {}).get(metric, 0))
        if actual < 0:
            raise ValueError(
                f"startup guard metric must be non-negative: {metric}"
            )
        per_item_limit = int(rule["per_item_limit"])
        limit = cardinalities[cardinality] * per_item_limit
        within_limit = actual <= limit
        row = {
            "actual": actual,
            "limit": limit,
            "within_limit": within_limit,
            "cardinality": cardinality,
            "cardinality_count": cardinalities[cardinality],
            "per_item_limit": per_item_limit,
        }
        rules[metric] = row
        if not within_limit:
            violations.append({"metric": metric, **row})

    return {
        "schema_version": STARTUP_TOTAL_INVOCATION_GUARD_SCHEMA_VERSION,
        "kind": "pcg_primary_total_invocation_guard",
        "status": "ok" if not violations else "failed",
        "audit_scope_count": cardinalities["audit_scope_count"],
        "spm_count": cardinalities["spm_count"],
        "rules": rules,
        "violations": violations,
    }


def require_startup_total_invocation_guard(
    metrics,
    *,
    audit_scope_count,
    spm_count,
):
    """Raise with a stable receipt when any total-call rule is exceeded."""
    receipt = startup_total_invocation_guard(
        metrics,
        audit_scope_count=audit_scope_count,
        spm_count=spm_count,
    )
    if receipt["violations"]:
        details = ", ".join(
            f"{row['metric']}={row['actual']} > {row['limit']}"
            for row in receipt["violations"]
        )
        raise StartupAmplificationError(
            "PCG startup total-invocation amplification guard failed: "
            + details
        )
    return receipt


class StartupLatencyTracker:
    """Thread-safe monotonic tracker persisted outside production assets."""

    def __init__(
        self,
        *,
        selected_perf=None,
        clock=time.perf_counter,
        receipt_path=STARTUP_LATENCY_RECEIPT_PATH,
        source_provenance=None,
    ):
        self._clock = clock
        now = float(clock())
        self._selected_perf = (
            now if selected_perf is None else float(selected_perf)
        )
        self._last_perf = self._selected_perf
        self._receipt_path = Path(receipt_path)
        self._lock = threading.RLock()
        self._receipt = {
            "schema_version": STARTUP_LATENCY_SCHEMA_VERSION,
            "kind": "pcg_st9_startup_latency",
            "started_at": datetime.now(timezone.utc).astimezone().isoformat(
                timespec="milliseconds"
            ),
            "source_provenance": dict(source_provenance or {}),
            "phase_order": list(STARTUP_PHASE_ORDER),
            "phase_budgets_seconds": dict(STARTUP_PHASE_BUDGET_SECONDS),
            "phases": [],
            "milestones": {
                "tab_selected": {
                    "status": "ok",
                    "from_tab_selection_seconds": 0.0,
                },
            },
            "status": "running",
        }

    def milestone(self, name, *, status="ok", counts=None, details=None):
        """Record an absolute UI/compute readiness boundary once."""
        name = str(name)
        with self._lock:
            if name in self._receipt["milestones"]:
                return dict(self._receipt["milestones"][name])
            now = float(self._clock())
            row = {
                "status": str(status),
                "from_tab_selection_seconds": round(
                    max(0.0, now - self._selected_perf), 6
                ),
                "counts": dict(counts or {}),
            }
            if details:
                row["details"] = dict(details)
            self._receipt["milestones"][name] = row
            self._receipt["total_seconds"] = row[
                "from_tab_selection_seconds"
            ]
            atomic_write_json(self._receipt_path, self._receipt)
            return dict(row)

    def mark(self, phase, *, status="ok", counts=None, details=None):
        phase = str(phase)
        if phase not in STARTUP_PHASE_ORDER:
            raise ValueError(f"Unknown PCG startup phase: {phase}")
        with self._lock:
            if any(row["phase"] == phase for row in self._receipt["phases"]):
                raise ValueError(f"PCG startup phase already recorded: {phase}")
            now = float(self._clock())
            duration = max(0.0, now - self._last_perf)
            total = max(0.0, now - self._selected_perf)
            budget = STARTUP_PHASE_BUDGET_SECONDS[phase]
            row = {
                "phase": phase,
                "status": str(status),
                "duration_seconds": round(duration, 6),
                "from_tab_selection_seconds": round(total, 6),
                "budget_seconds": budget,
                "within_budget": duration <= budget,
                "counts": dict(counts or {}),
            }
            if details:
                row["details"] = dict(details)
            self._receipt["phases"].append(row)
            self._last_perf = now
            if phase == STARTUP_PHASE_ORDER[-1]:
                self._receipt["status"] = (
                    "complete"
                    if status in {"ok", "cached"}
                    else str(status)
                )
            self._receipt["total_seconds"] = round(total, 6)
            atomic_write_json(self._receipt_path, self._receipt)
            return dict(row)

    def finish_early(self, status, *, error=None):
        with self._lock:
            now = float(self._clock())
            self._receipt["status"] = str(status)
            self._receipt["total_seconds"] = round(
                max(0.0, now - self._selected_perf), 6
            )
            if error is not None:
                self._receipt["error"] = (
                    f"{type(error).__name__}: {error}"
                )
            atomic_write_json(self._receipt_path, self._receipt)
            return self.snapshot()

    def snapshot(self):
        with self._lock:
            return {
                **self._receipt,
                "phase_order": list(self._receipt["phase_order"]),
                "phase_budgets_seconds": dict(
                    self._receipt["phase_budgets_seconds"]
                ),
                "phases": [dict(row) for row in self._receipt["phases"]],
                "milestones": {
                    name: dict(row)
                    for name, row in self._receipt["milestones"].items()
                },
            }


__all__ = [
    "PRODUCTION_FIXTURE_LATENCY_BUDGET_SECONDS",
    "USABLE_READY_ACCEPTANCE_CAP_SECONDS",
    "STARTUP_TOTAL_INVOCATION_GUARD_SCHEMA_VERSION",
    "STARTUP_TOTAL_INVOCATION_RULES",
    "STARTUP_LATENCY_RECEIPT_PATH",
    "STARTUP_LATENCY_SCHEMA_VERSION",
    "STARTUP_PHASE_BUDGET_SECONDS",
    "STARTUP_PHASE_ORDER",
    "StartupAmplificationError",
    "StartupLatencyTracker",
    "require_startup_total_invocation_guard",
    "startup_total_invocation_guard",
]
