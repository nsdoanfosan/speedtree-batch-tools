from pathlib import Path
import sys


SK_BATCH_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SK_BATCH_DIR))

from failed_retry_eligibility import (  # noqa: E402
    BLENDER_REBUILD,
    BLOCKED,
    CURRENT_BLENDER_EXCLUDED,
    UNREAL_ONLY,
    UNREAL_PARENT_CURRENT,
    UNREAL_PARENT_INCOMPLETE,
    classify_failed_retry,
)


CURRENT_REPAIR = {
    "current": True,
    "push_ready": True,
    "kind": "ready",
    "reason": "current content-addressed Repair output",
}

AMBIGUOUS_REPAIR = {
    "current": False,
    "push_ready": False,
    "kind": "inspection_incomplete",
    "reason": "Blender Repair evidence is incomplete",
}


def test_stale_content_rebuilds_even_when_saved_labels_claim_success():
    decision = classify_failed_retry(
        {
            "blend_status": "최신 ✓",
            "push_status": "완료 (현재 최신)",
            "push_status_kind": "imported_ok",
        },
        {
            "current": False,
            "push_ready": False,
            "kind": "stale_content",
            "reason": "source fingerprint changed",
        },
    )

    assert decision.classification == BLENDER_REBUILD
    assert decision.reason_code == "blender_output_not_current"


def test_current_content_excludes_even_when_saved_label_claims_stale():
    decision = classify_failed_retry(
        {"blend_status": "Blender 갱신 필요 — saved text only"},
        CURRENT_REPAIR,
    )

    assert decision.classification == CURRENT_BLENDER_EXCLUDED
    assert decision.reason_code == "current_blender_success"


def test_explicit_force_rerun_rebuilds_current_blender_success():
    decision = classify_failed_retry(
        {"push_status_kind": "imported_ok"},
        CURRENT_REPAIR,
        force_rerun=True,
    )

    assert decision.classification == BLENDER_REBUILD
    assert decision.reason_code == "current_blender_success_forced_rebuild"


def test_blender_ready_but_never_pushed_is_not_a_current_success():
    """`ready` means Blender finished, not that Unreal ingested anything."""
    decision = classify_failed_retry(
        {"push_status": "준비됨 ✓", "push_status_kind": "ready"},
        CURRENT_REPAIR,
    )

    assert decision.classification == BLENDER_REBUILD
    assert decision.reason_code == "push_never_reached_unreal"


def test_export_pending_unreal_is_not_a_current_success():
    decision = classify_failed_retry(
        {
            "push_status": "export 완료 · Unreal 대기",
            "push_status_kind": "exported_pending_unreal",
            # A half-done push legitimately retains its export receipts; their
            # presence must not read as a completed run.
            "push_paths": {
                "manifest": "pending_parent.json",
                "export_report": "pending_export.json",
            },
        },
        CURRENT_REPAIR,
    )

    assert decision.classification == BLENDER_REBUILD
    assert decision.reason_code == "push_never_reached_unreal"


def test_imported_ok_is_still_excluded_as_a_current_success():
    """The fix must not turn a genuinely completed push into a retry."""
    decision = classify_failed_retry(
        {"push_status": "완료 (headless)", "push_status_kind": "imported_ok"},
        CURRENT_REPAIR,
    )

    assert decision.classification == CURRENT_BLENDER_EXCLUDED
    assert decision.reason_code == "current_blender_success"


def test_non_retryable_unreal_parent_blocks_without_an_explicit_force():
    decision = classify_failed_retry(
        {"push_status_kind": "imported_ok"},
        CURRENT_REPAIR,
        unreal_parent_status=UNREAL_PARENT_CURRENT,
    )

    assert decision.classification == BLOCKED
    assert decision.reason_code == "parent_not_retryable_unreal_failure"


def test_explicit_force_rerun_rebuilds_a_non_retryable_unreal_parent():
    decision = classify_failed_retry(
        {"push_status_kind": "imported_ok"},
        CURRENT_REPAIR,
        unreal_parent_status=UNREAL_PARENT_CURRENT,
        force_rerun=True,
    )

    assert decision.classification == BLENDER_REBUILD
    assert decision.reason_code == (
        "parent_not_retryable_unreal_failure_forced_rebuild"
    )


def test_ambiguous_retry_evidence_blocks_without_an_explicit_force():
    decision = classify_failed_retry({}, AMBIGUOUS_REPAIR)

    assert decision.classification == BLOCKED
    assert decision.reason_code == "retry_evidence_ambiguous"


def test_explicit_force_rerun_rebuilds_ambiguous_retry_evidence():
    decision = classify_failed_retry(
        {},
        AMBIGUOUS_REPAIR,
        force_rerun=True,
    )

    assert decision.classification == BLENDER_REBUILD
    assert decision.reason_code == "retry_evidence_ambiguous_forced_rebuild"


def test_current_immutable_unreal_failure_stays_unreal_only():
    decision = classify_failed_retry(
        {"push_status_kind": "data_error"},
        CURRENT_REPAIR,
        unreal_parent_status=UNREAL_PARENT_CURRENT,
    )

    assert decision.classification == UNREAL_ONLY
    assert decision.reason_code == "current_immutable_unreal_failure"


def test_incomplete_unreal_parent_uses_safe_full_rebuild_fallback():
    decision = classify_failed_retry(
        {"push_status_kind": "data_error"},
        CURRENT_REPAIR,
        unreal_parent_status=UNREAL_PARENT_INCOMPLETE,
        unreal_parent_diagnostic="manifest/checkpoint pair is incomplete",
    )

    assert decision.classification == BLENDER_REBUILD
    assert decision.reason_code == (
        "unreal_parent_evidence_incomplete_full_rebuild"
    )
    assert "manifest/checkpoint" in decision.diagnostic


def test_send2ue_report_routes_without_reading_localized_status_text():
    decision = classify_failed_retry(
        {
            "push_status_kind": "data_error",
            "push_status": "arbitrary localized text",
            "push_paths": {"report": "export_failed.json"},
        },
        CURRENT_REPAIR,
    )

    assert decision.classification == BLENDER_REBUILD
    assert decision.reason_code == "structured_send2ue_export_failure"


def test_bare_push_failure_kind_uses_safe_full_rebuild_fallback():
    decision = classify_failed_retry(
        {"push_status_kind": "data_error"},
        CURRENT_REPAIR,
    )

    assert decision.classification == BLENDER_REBUILD
    assert decision.reason_code == "push_phase_evidence_missing_full_rebuild"
