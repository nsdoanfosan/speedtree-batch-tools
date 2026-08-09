"""Durable provenance for one automatic repair attempt.

A repair plan knows its root reason codes before any stage runs.  When a stage
fails, that knowledge has to reach the row the operator reads -- not only the
process-local memo, which a restart discards.  #167 found nine targets sitting
on `automatic_repair_failed` with no reason code at all, under an operator
action that told them to regenerate the codes with a fresh audit nothing ran.

The wrapper tokens are the whole problem.  `automatic_repair_failed` says a
repair failed; it does not say what the repair was for.  A row carrying only
wrappers has lost its provenance even though it looks populated, so this module
strips them before deciding whether a root reason survived.

It owns three things:

* the failure receipt's schema, written before the first stage runs,
* how a root reason is recovered from a durable row after a restart, and
* the bounded, single-shot re-audit gate that runs when it cannot be.

The re-audit budget is exactly one, recorded on the row itself rather than in
memory, because the failure this repairs is precisely that in-process state did
not survive.  A second empty result is terminal: re-auditing a target whose
audit just came back empty cannot become productive by repetition.
"""
from __future__ import annotations

import copy
from typing import Any, Mapping

from repair_reason_registry import normalize_reason_code

REPAIR_FAILURE_SCHEMA_VERSION = 1

REPAIR_FAILURE_KEY = "repair_failure"

# Tokens that describe a repair attempt's own outcome.  They are legitimate
# reason codes -- they are registered, and they route the operator message --
# but none of them names the defect the repair was built to fix.  A row whose
# only codes are these has no root reason.
REPAIR_WRAPPER_CODES = frozenset({
    "automatic_repair_cancelled",
    "automatic_repair_failed",
    "automatic_repair_reaudit_failed",
    "automatic_repair_unsupported",
    "dependency_root_reason_missing",
    "exact_relation_repair_failed",
    "exact_target_plan_invalid",
    "initiating_job_context_missing",
    "recovery_blocked",
    "registered_reason_has_no_exact_action",
})

_CODE_KEYS = ("root_reason_codes", "reason_codes")

# One row nests plan metadata under evidence under a repair attempt.  Four
# levels reaches every shape this pipeline writes; an unbounded walk would
# happily follow a payload that embedded an entire audit report.
_MAX_SEARCH_DEPTH = 4


def _normalized_codes(values) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        values = [values]
    codes = []
    for value in values or ():
        code = normalize_reason_code(value)
        if code and code not in codes:
            codes.append(code)
    return tuple(codes)


def plan_provenance(plan_metadata) -> dict[str, tuple[str, ...]]:
    """Snapshot what a plan is for, before any stage can fail and lose it."""
    metadata = plan_metadata if isinstance(plan_metadata, Mapping) else {}
    actions = []
    for stage in metadata.get("stages") or ():
        action = str((stage or {}).get("repair_action") or "").strip()
        if action and action not in actions:
            actions.append(action)
    for action in metadata.get("actions") or ():
        action = str(action or "").strip()
        if action and action not in actions:
            actions.append(action)
    return {
        "root_reason_codes": _normalized_codes(
            metadata.get("reason_codes")
        ),
        "planned_actions": tuple(actions),
    }


def build_repair_failure(
    *,
    request_id="",
    plan_metadata=None,
    root_reason_codes=(),
    planned_actions=(),
    attempted_stages=(),
    failed_stage="",
    failure_code="",
    failure_report="",
    error="",
    fresh_reaudit_attempted=False,
) -> dict[str, Any]:
    """Build the durable payload a failed repair must leave behind."""
    provenance = plan_provenance(plan_metadata)
    codes = _normalized_codes(root_reason_codes) or provenance[
        "root_reason_codes"
    ]
    actions = tuple(planned_actions) or provenance["planned_actions"]

    attempted = []
    completed = []
    for row in attempted_stages or ():
        if not isinstance(row, Mapping):
            continue
        stage = str(row.get("stage") or "")
        if not stage:
            continue
        attempted.append(stage)
        if str(row.get("status") or "") == "completed":
            completed.append(stage)

    return {
        "schema_version": REPAIR_FAILURE_SCHEMA_VERSION,
        "request_id": str(request_id or ""),
        "root_reason_codes": list(codes),
        "planned_actions": list(actions),
        "attempted_stages": attempted,
        "completed_stages": completed,
        "failed_stage": str(
            failed_stage or (attempted[-1] if attempted else "")
        ),
        "failure_code": str(failure_code or ""),
        "failure_report": str(failure_report or ""),
        "error": str(error or ""),
        "fresh_reaudit_attempted": bool(fresh_reaudit_attempted),
    }


def _collect_codes(node, depth, found):
    if depth > _MAX_SEARCH_DEPTH:
        return
    if isinstance(node, Mapping):
        for key in _CODE_KEYS:
            for code in _normalized_codes(node.get(key)):
                if code not in found:
                    found.append(code)
        for value in node.values():
            if isinstance(value, (Mapping, list, tuple)):
                _collect_codes(value, depth + 1, found)
    elif isinstance(node, (list, tuple)):
        for value in node:
            if isinstance(value, (Mapping, list, tuple)):
                _collect_codes(value, depth + 1, found)


def root_reason_codes(row) -> tuple[str, ...]:
    """Recover the codes a repair was built from, wrappers excluded.

    Works on a durable state row after a restart, where the in-process memo
    that also held them is gone.
    """
    found: list[str] = []
    _collect_codes(row, 0, found)
    return tuple(
        code for code in found if code not in REPAIR_WRAPPER_CODES
    )


def repair_failure_record(row) -> dict[str, Any]:
    record = (row or {}).get(REPAIR_FAILURE_KEY) if isinstance(
        row, Mapping
    ) else None
    return dict(record) if isinstance(record, Mapping) else {}


def _wrapper_tokens(node, depth, found):
    if depth > _MAX_SEARCH_DEPTH:
        return
    if isinstance(node, Mapping):
        for key in ("reason_token", "kind", "failure_code", *_CODE_KEYS):
            for code in _normalized_codes(node.get(key)):
                if code in REPAIR_WRAPPER_CODES and code not in found:
                    found.append(code)
        for value in node.values():
            if isinstance(value, (Mapping, list, tuple)):
                _wrapper_tokens(value, depth + 1, found)
    elif isinstance(node, (list, tuple)):
        for value in node:
            if isinstance(value, (Mapping, list, tuple)):
                _wrapper_tokens(value, depth + 1, found)


def repair_was_attempted(row) -> bool:
    """True when a row records a repair outcome, whatever it was."""
    found: list[str] = []
    _wrapper_tokens(row, 0, found)
    return bool(found)


def needs_fresh_reaudit(row) -> bool:
    """A row that lost its root reason gets exactly one fresh audit, ever.

    Both halves matter.  A row with no reason and no repair wrapper simply has
    nothing to recover -- it was never repaired, and auditing it again would
    just double the work on every ordinary candidate.  The defect is narrower
    than that: a repair ran, and the row cannot say what for.

    The budget lives on the row, not in memory, because the state this
    recovers from is exactly what a crash leaves behind.
    """
    if not repair_was_attempted(row):
        return False
    if root_reason_codes(row):
        return False
    record = repair_failure_record(row)
    if record and record.get("fresh_reaudit_attempted"):
        return False
    return True


def mark_fresh_reaudit_attempted(row, *, request_id="") -> dict[str, Any]:
    """Spend the single re-audit budget durably, before running it."""
    updated = copy.deepcopy(row) if isinstance(row, Mapping) else {}
    record = repair_failure_record(updated)
    if not record:
        record = build_repair_failure(request_id=request_id)
    record["fresh_reaudit_attempted"] = True
    if request_id and not record.get("request_id"):
        record["request_id"] = str(request_id)
    updated[REPAIR_FAILURE_KEY] = record
    return updated
