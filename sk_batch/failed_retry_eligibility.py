"""Explicit eligibility contract for failed Blender/Unreal retries.

The retry button must not infer pipeline ownership from translated table text.
Callers provide the live, content-addressed Repair state plus the independently
validated immutable Unreal parent state; this module makes the routing decision
without performing I/O.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass


RETRY_ELIGIBILITY_SCHEMA_VERSION = 1

BLENDER_REBUILD = "blender_rebuild"
UNREAL_ONLY = "unreal_only"
CURRENT_BLENDER_EXCLUDED = "current_blender_excluded"
BLOCKED = "blocked"
PENDING_UNREAL_VALIDATION = "pending_unreal_validation"

UNREAL_PARENT_ABSENT = "absent"
UNREAL_PARENT_CANDIDATE = "candidate"
UNREAL_PARENT_CURRENT = "current"
UNREAL_PARENT_INCOMPLETE = "incomplete"
UNREAL_PARENT_INVALID = "invalid"
UNREAL_PARENT_DEPENDENCY_REBUILD = "dependency_rebuild_required"

UNREAL_RECOVERY_FAILURE_KINDS = frozenset({
    "data_error",
    "manual_required",
    "unreal_crash",
    "not_run",
})
# Durable push states that are stamped *before* Unreal ingest is confirmed.
# `ready` lands when Blender Repair finishes and the row is handed to the push
# phase; `exported_pending_unreal` lands when Send2UE export finishes and the
# Unreal result has not been observed. Neither is a failure and neither is a
# finished pipeline -- if the push phase stops in between, the row keeps a
# current .blend and never reaches Unreal on its own.
PUSH_INCOMPLETE_KINDS = frozenset({
    "exported_pending_unreal",
    "ready",
})
# These are orchestration-history wrappers, not current asset verdicts.  They
# may explain why an earlier automatic route stopped, but only a fresh content
# audit may decide whether the asset can Push.
AUTOMATION_WRAPPER_RETRY_KINDS = frozenset({
    "automatic_repair",
    "automatic_repair_failed",
    "automatic_repair_reaudit_failed",
    "planned_excluded",
    "preflight_skip",
})
BLENDER_EXPORT_RETRY_FAILURE_KINDS = frozenset({
    "data_error",
    "internal_error",
    "not_run",
    "not_run_unreal",
    "process",
    "process_timeout",
    "push_timeout",
    "rpc_timeout",
    "unreal_crash",
    "unreal_unavailable",
})

# These states come from App._repair_output_state, whose positive decisions
# validate the current SPM/report/material/assembly/wind fingerprint contracts.
# Every listed negative state is repairable by a full forced pipeline. Unknown
# states fail closed instead of being silently treated as stale.
BLENDER_REBUILD_REPAIR_KINDS = frozenset({
    "assembly_stale",
    "cluster_bark_capture",
    "material",
    "missing_blend",
    "missing_wind",
    "output_contract",
    "stale_content",
    "texture",
    "wind_contract",
})


@dataclass(frozen=True)
class RetryEligibility:
    """One auditable retry routing decision."""

    classification: str
    reason_code: str
    diagnostic: str
    repair_kind: str
    unreal_parent_status: str
    schema_version: int = RETRY_ELIGIBILITY_SCHEMA_VERSION

    def metadata(self):
        return asdict(self)


def _result(classification, reason_code, diagnostic, repair_kind, parent):
    return RetryEligibility(
        classification=classification,
        reason_code=reason_code,
        diagnostic=str(diagnostic),
        repair_kind=repair_kind,
        unreal_parent_status=parent,
    )


def _structured_blender_failure(entry):
    kind = str((entry or {}).get("blend_status_kind") or "")
    error = (entry or {}).get("blend_status_error") or {}
    return bool(
        kind in BLENDER_EXPORT_RETRY_FAILURE_KINDS
        and isinstance(error, dict)
        and error.get("kind") == kind
        and error.get("message")
    )


def _structured_send2ue_failure(entry):
    entry = entry or {}
    kind = str(entry.get("push_status_kind") or "")
    paths = entry.get("push_paths") or {}
    if kind not in BLENDER_EXPORT_RETRY_FAILURE_KINDS:
        return False
    if paths.get("manifest") or paths.get("checkpoint"):
        return False
    return any(
        paths.get(key)
        for key in ("report", "export_report", "export_log", "log")
    )


def classify_failed_retry(
    entry,
    repair_state,
    *,
    unreal_parent_status=UNREAL_PARENT_ABSENT,
    unreal_parent_diagnostic="",
    force_rerun=False,
    force_full_rebuild=False,
):
    """Classify one inventory candidate from structured current evidence."""

    entry = entry if isinstance(entry, dict) else {}
    repair_state = (
        repair_state if isinstance(repair_state, dict) else {}
    )
    repair_kind = str(repair_state.get("kind") or "inspection_incomplete")
    repair_current = repair_state.get("current") is True
    repair_reason = str(
        repair_state.get("reason") or "Blender Repair evidence is incomplete"
    )
    parent = str(unreal_parent_status or UNREAL_PARENT_ABSENT)
    push_kind = str(entry.get("push_status_kind") or "")

    # A content-proven stale/not-current Blender result always requires a new
    # Blender -> Send2UE -> Unreal generation, even if old Unreal evidence is
    # still present. This also prevents the same dependency from entering both
    # retry partitions.
    if not repair_current and repair_kind in BLENDER_REBUILD_REPAIR_KINDS:
        return _result(
            BLENDER_REBUILD,
            "blender_output_not_current",
            repair_reason,
            repair_kind,
            parent,
        )
    if not repair_current and _structured_blender_failure(entry):
        return _result(
            BLENDER_REBUILD,
            "structured_blender_failure",
            str(
                (entry.get("blend_status_error") or {}).get("message")
                or repair_reason
            ),
            repair_kind,
            parent,
        )

    # These are unfinished Push states, regardless of whether an older parent
    # manifest is still current.  Parent evidence may safely authorize an
    # Unreal-only retry after a recorded Unreal failure; it must not convert a
    # run that never reached Unreal into a terminal success/exclusion.
    if push_kind in PUSH_INCOMPLETE_KINDS:
        return _result(
            BLENDER_REBUILD,
            "push_never_reached_unreal",
            (
                "Blender output is current but the push phase never produced a "
                f"terminal Unreal result ({push_kind}); regenerate it through "
                "the full Blender pipeline"
            ),
            repair_kind,
            parent,
        )
    if push_kind in AUTOMATION_WRAPPER_RETRY_KINDS:
        return _result(
            BLENDER_REBUILD,
            "automation_wrapper_fresh_pipeline",
            (
                "Saved automation status is historical orchestration evidence, "
                f"not current asset authority ({push_kind}); run the fresh full "
                "pipeline and let its exact content audit decide"
            ),
            repair_kind,
            parent,
        )

    if parent == UNREAL_PARENT_DEPENDENCY_REBUILD:
        return _result(
            BLENDER_REBUILD,
            "unreal_dependency_requires_rebuild",
            unreal_parent_diagnostic
            or "Immutable Unreal dependency is already in the rebuild partition",
            repair_kind,
            parent,
        )
    if parent == UNREAL_PARENT_CURRENT:
        if force_full_rebuild:
            return _result(
                BLENDER_REBUILD,
                "current_unreal_parent_forced_rebuild",
                (
                    "An explicit force-full-rebuild request authorizes the Blender "
                    "pipeline even when a current immutable Unreal parent "
                    f"exists ({push_kind or 'missing'})"
                ),
                repair_kind,
                parent,
            )
        if push_kind in UNREAL_RECOVERY_FAILURE_KINDS:
            return _result(
                UNREAL_ONLY,
                "current_immutable_unreal_failure",
                "Immutable export/source evidence is current",
                repair_kind,
                parent,
            )
        if force_rerun:
            return _result(
                BLENDER_REBUILD,
                "current_unreal_parent_forced_rebuild",
                (
                    "The selected current result has no retryable Unreal "
                    "failure, so its explicit rerun uses the full Blender "
                    f"pipeline ({push_kind or 'missing'})"
                ),
                repair_kind,
                parent,
            )
        return _result(
            BLOCKED,
            "parent_not_retryable_unreal_failure",
            f"Unreal parent is current but status is not retryable: {push_kind or 'missing'}",
            repair_kind,
            parent,
        )
    if parent == UNREAL_PARENT_CANDIDATE:
        return _result(
            PENDING_UNREAL_VALIDATION,
            "unreal_parent_validation_pending",
            "Immutable Unreal parent requires source/artifact validation",
            repair_kind,
            parent,
        )
    if parent in {UNREAL_PARENT_INCOMPLETE, UNREAL_PARENT_INVALID}:
        return _result(
            BLENDER_REBUILD,
            "unreal_parent_evidence_" + parent + "_full_rebuild",
            unreal_parent_diagnostic
            or (
                "Unreal parent evidence cannot authorize immutable recovery; "
                "regenerate it through the full Blender pipeline"
            ),
            repair_kind,
            parent,
        )

    # Send2UE failures are identified by structured export paths. A bare saved
    # failure kind has no trustworthy phase provenance and therefore blocks.
    if _structured_send2ue_failure(entry):
        return _result(
            BLENDER_REBUILD,
            "structured_send2ue_export_failure",
            str(
                (entry.get("push_status_error") or {}).get("message")
                or "Send2UE export failed"
            ),
            repair_kind,
            parent,
        )

    if push_kind in BLENDER_EXPORT_RETRY_FAILURE_KINDS:
        return _result(
            BLENDER_REBUILD,
            "push_phase_evidence_missing_full_rebuild",
            (
                "Push failure has no export report or complete Unreal "
                "manifest/checkpoint evidence; regenerate it through the "
                "full Blender pipeline"
            ),
            repair_kind,
            parent,
        )

    if repair_current:
        # An explicit operator force request is an authorization to rebuild, not
        # a claim that a failure exists. A current success stays ineligible for
        # the ordinary failed-export route, but must not dead-end when the
        # operator has already asked for a forced rerun.
        if force_rerun:
            return _result(
                BLENDER_REBUILD,
                "current_blender_success_forced_rebuild",
                (
                    "Current Blender success is rebuilt because an explicit "
                    "force rerun was requested"
                ),
                repair_kind,
                parent,
            )
        return _result(
            CURRENT_BLENDER_EXCLUDED,
            "current_blender_success",
            "Current Blender success has no eligible failed export",
            repair_kind,
            parent,
        )

    if force_rerun:
        return _result(
            BLENDER_REBUILD,
            "retry_evidence_ambiguous_forced_rebuild",
            (
                "Retry evidence is incomplete but an explicit force rerun "
                f"authorizes a full Blender rebuild · {repair_reason}"
            ),
            repair_kind,
            parent,
        )
    return _result(
        BLOCKED,
        "retry_evidence_ambiguous",
        repair_reason,
        repair_kind,
        parent,
    )


__all__ = (
    "AUTOMATION_WRAPPER_RETRY_KINDS",
    "BLENDER_EXPORT_RETRY_FAILURE_KINDS",
    "BLENDER_REBUILD",
    "BLOCKED",
    "CURRENT_BLENDER_EXCLUDED",
    "PENDING_UNREAL_VALIDATION",
    "RETRY_ELIGIBILITY_SCHEMA_VERSION",
    "UNREAL_ONLY",
    "UNREAL_PARENT_ABSENT",
    "UNREAL_PARENT_CANDIDATE",
    "UNREAL_PARENT_CURRENT",
    "UNREAL_PARENT_DEPENDENCY_REBUILD",
    "UNREAL_PARENT_INCOMPLETE",
    "UNREAL_PARENT_INVALID",
    "UNREAL_RECOVERY_FAILURE_KINDS",
    "RetryEligibility",
    "classify_failed_retry",
)
