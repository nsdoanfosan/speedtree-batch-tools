"""A failed repair has to say what it was repairing.

#167 found nine production targets on `automatic_repair_failed` carrying no
reason code at all, under an operator action telling them to run a fresh audit
that nothing ran.  These assertions cover the two halves of that: provenance
captured before a stage can fail, and a bounded recovery when a row still
arrives without it.
"""
import sys
import unittest
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parents[1]
if str(REPO_DIR) not in sys.path:
    sys.path.insert(0, str(REPO_DIR))

from repair_failure_provenance import (  # noqa: E402
    REPAIR_FAILURE_KEY,
    REPAIR_FAILURE_SCHEMA_VERSION,
    build_repair_failure,
    mark_fresh_reaudit_attempted,
    needs_fresh_reaudit,
    plan_provenance,
    repair_was_attempted,
    root_reason_codes,
)
from repair_reason_registry import (  # noqa: E402
    FATAL,
    UNSUPPORTED,
    disposition_of,
    reason_row,
)


PLAN = {
    "reason_codes": [
        "canonical_bark_normalization_required",
        "normalized_variants_required",
    ],
    "stages": [
        {"stage": "cluster_refresh", "repair_action": "cluster-refresh"},
        {"stage": "pcg_texture", "repair_action": "step3-standard"},
    ],
}


class PlanProvenanceTests(unittest.TestCase):
    def test_provenance_is_known_before_any_stage_runs(self):
        provenance = plan_provenance(PLAN)

        self.assertEqual(
            provenance["root_reason_codes"],
            (
                "canonical_bark_normalization_required",
                "normalized_variants_required",
            ),
        )
        self.assertEqual(
            provenance["planned_actions"],
            ("cluster-refresh", "step3-standard"),
        )

    def test_failure_receipt_keeps_the_plan_reasons_of_a_mid_stage_failure(self):
        receipt = build_repair_failure(
            request_id="cluster-relation-abc",
            plan_metadata=PLAN,
            attempted_stages=[
                {"stage": "cluster_refresh", "status": "completed"},
                {"stage": "pcg_texture", "status": "failed"},
            ],
            failed_stage="pcg_texture",
            failure_code="exact_relation_repair_failed",
            failure_report="logs/exact_repair_1.json",
            error="stage 2 exited 1",
        )

        self.assertEqual(
            receipt["schema_version"],
            REPAIR_FAILURE_SCHEMA_VERSION,
        )
        self.assertEqual(
            receipt["root_reason_codes"],
            list(plan_provenance(PLAN)["root_reason_codes"]),
        )
        self.assertEqual(receipt["completed_stages"], ["cluster_refresh"])
        self.assertEqual(
            receipt["attempted_stages"],
            ["cluster_refresh", "pcg_texture"],
        )
        self.assertEqual(receipt["failed_stage"], "pcg_texture")
        self.assertFalse(receipt["fresh_reaudit_attempted"])

    def test_prose_and_paths_never_become_root_reasons(self):
        receipt = build_repair_failure(
            root_reason_codes=[
                "canonical_bark_normalization_required",
                "Cluster is not ready",
                "D:/Assets/SK_x.spm",
                "",
            ],
        )

        self.assertEqual(
            receipt["root_reason_codes"],
            ["canonical_bark_normalization_required"],
        )


class RootReasonRecoveryTests(unittest.TestCase):
    def test_reasons_are_recovered_from_a_durable_row_after_restart(self):
        row = {
            "kind": "automatic_repair_failed",
            "evidence": {
                "repair_attempt": {
                    "status": "failed",
                    "reason_codes": ["normalized_variants_stale"],
                },
            },
        }

        self.assertEqual(
            root_reason_codes(row),
            ("normalized_variants_stale",),
        )
        self.assertFalse(needs_fresh_reaudit(row))

    def test_a_row_carrying_only_wrappers_has_no_root_reason(self):
        """This is the exact production shape #167 reported."""
        row = {
            "kind": "automatic_repair_failed",
            "reason_codes": [
                "automatic_repair_failed",
                "dependency_root_reason_missing",
            ],
        }

        self.assertEqual(root_reason_codes(row), ())
        self.assertTrue(repair_was_attempted(row))
        self.assertTrue(needs_fresh_reaudit(row))

    def test_a_row_that_was_never_repaired_has_nothing_to_recover(self):
        """Otherwise every ordinary candidate pays for a second audit."""
        self.assertFalse(needs_fresh_reaudit({}))
        self.assertFalse(needs_fresh_reaudit({"kind": "data_error"}))
        self.assertFalse(
            needs_fresh_reaudit({"current_repair_state": {"current": True}})
        )

    def test_the_fresh_reaudit_budget_is_exactly_one_and_durable(self):
        row = {"kind": "automatic_repair_failed"}
        self.assertTrue(needs_fresh_reaudit(row))

        spent = mark_fresh_reaudit_attempted(row, request_id="target")

        self.assertTrue(spent[REPAIR_FAILURE_KEY]["fresh_reaudit_attempted"])
        self.assertFalse(needs_fresh_reaudit(spent))
        # Spending it again cannot reopen the budget: a second empty audit is
        # terminal, not an invitation to loop.
        self.assertFalse(
            needs_fresh_reaudit(mark_fresh_reaudit_attempted(spent))
        )
        # The original row is untouched, so a caller cannot spend a budget by
        # merely inspecting it.
        self.assertNotIn(REPAIR_FAILURE_KEY, row)

    def test_a_recovered_reason_ends_the_recovery(self):
        spent = mark_fresh_reaudit_attempted({"kind": "automatic_repair_failed"})
        spent["reason_codes"] = ["canonical_bark_normalization_required"]

        self.assertEqual(
            root_reason_codes(spent),
            ("canonical_bark_normalization_required",),
        )
        self.assertFalse(needs_fresh_reaudit(spent))


class LostProvenanceDispositionTests(unittest.TestCase):
    def test_lost_provenance_is_not_classified_as_data_damage(self):
        """`fatal` is reserved for damage automatic recovery would hide.

        Nine targets were sent to that state for a defect in our own
        bookkeeping, which no automatic path was then allowed to touch.
        """
        row = reason_row("dependency_root_reason_missing")

        self.assertEqual(disposition_of("dependency_root_reason_missing"), UNSUPPORTED)
        self.assertNotEqual(row.disposition, FATAL)
        self.assertTrue(row.friendly_cause)
        self.assertTrue(row.operator_action)
        self.assertFalse(row.repair_action)

    def test_the_operator_action_names_a_step_the_operator_can_take(self):
        row = reason_row("dependency_root_reason_missing")

        # The reported defect was a self-referential action: it asked for a
        # fresh audit that no owner ran.  The automatic single-shot re-audit
        # now runs first, so this text is the answer after it came back empty.
        self.assertIn("다시 검사", row.operator_action)


if __name__ == "__main__":
    unittest.main()
