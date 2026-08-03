"""The repair contract must cover every reason code the pipeline can emit.

These assertions are the whole point of the registry.  Each one failed
silently before it existed, and each failure mode had already reached
production: a blocked target with no recovery path, and no record that a
recovery path was ever possible.
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parents[1]
if str(REPO_DIR) not in sys.path:
    sys.path.insert(0, str(REPO_DIR))

import repair_orchestration as orchestration  # noqa: E402
from repair_reason_registry import (  # noqa: E402
    DISPOSITIONS,
    INFORMATIONAL,
    REASON_REGISTRY,
    REPAIRABLE,
    UNSUPPORTED,
    UNCLASSIFIED,
    UNCLASSIFIED_CEILING,
    UNEMITTED_PLANNER_CODES,
    codes_with,
    disposition_of,
)
from repair_reason_scan import emitted_reason_codes, scan_module  # noqa: E402


OBSERVED_TOKEN_FIXTURE = (
    REPO_DIR
    / "sk_batch"
    / "tests"
    / "fixtures"
    / "issue138_observed_reason_tokens.json"
)
OBSERVED_INTEGRITY_FIXTURE = (
    REPO_DIR
    / "sk_batch"
    / "tests"
    / "fixtures"
    / "issue138_observed_integrity_reasons.json"
)


class ReasonRegistryCoverageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.emitted = emitted_reason_codes(REPO_DIR)
        queue_payload = json.loads(
            OBSERVED_TOKEN_FIXTURE.read_text(encoding="utf-8")
        )
        integrity_payload = json.loads(
            OBSERVED_INTEGRITY_FIXTURE.read_text(encoding="utf-8")
        )
        cls.observed = set(queue_payload["tokens"])
        cls.observed.update(
            integrity_payload["integrity_issue_occurrences"]
        )
        cls.observed.update(integrity_payload["blocking_target_counts"])

    def test_every_emitted_reason_code_is_registered(self):
        """A new block cannot ship without a disposition.

        This is the assertion that ends the discovery loop.  On 2026-08-03 the
        planner owned 31 codes while production emitted 226, and the only way
        to learn that a block was unrepairable was to watch a batch stop.
        """
        unregistered = sorted(set(self.emitted) - set(REASON_REGISTRY))
        detail = "\n".join(
            f"  {code}  ({self.emitted[code][0]})" for code in unregistered
        )
        self.assertEqual(
            unregistered,
            [],
            "reason codes emitted by production code but absent from "
            "repair_reason_registry.REASON_REGISTRY. Add each one with a "
            "disposition -- 'unsupported' is a valid, honest answer:\n"
            + detail,
        )

    def test_registered_dispositions_are_valid(self):
        invalid = sorted(
            code for code, row in REASON_REGISTRY.items()
            if row.disposition not in DISPOSITIONS
        )
        self.assertEqual(invalid, [])

    def test_repairable_codes_are_emitted_or_observed(self):
        """No dead vocabulary, including reason fields the AST cannot name.

        `repair_orchestration` mapped 20+ repairable codes that no module ever
        emitted, so the planner could not fire on them however correct the
        mapping was. A repair path that nothing can trigger is not a repair
        path.
        """
        dead = sorted(
            set(codes_with(REPAIRABLE))
            - set(self.emitted)
            - self.observed
        )
        self.assertEqual(
            dead,
            [],
            "codes registered as repairable that no production module emits "
            "and no sanitized runtime snapshot observed; either wire/observe "
            "the emitter or drop the mapping",
        )

    def test_unclassified_debt_never_grows(self):
        """The ceiling is a ratchet, not a budget."""
        unclassified = codes_with(UNCLASSIFIED)
        self.assertLessEqual(
            len(unclassified),
            UNCLASSIFIED_CEILING,
            "more unclassified reason codes than the recorded ceiling. "
            "Classify the new code instead of raising UNCLASSIFIED_CEILING.",
        )

    def test_planner_vocabulary_is_registered_or_declared_unemitted(self):
        """Every code the planner claims must be real or admitted as not real."""
        claimed = set(orchestration.ALL_REPAIR_CONTRACT_CODES)
        unaccounted = sorted(
            claimed - set(REASON_REGISTRY) - set(UNEMITTED_PLANNER_CODES)
        )
        self.assertEqual(
            unaccounted,
            [],
            "repair_orchestration claims reason codes that are neither "
            "registered nor listed in UNEMITTED_PLANNER_CODES",
        )

    def test_unemitted_planner_codes_stay_unemitted_until_resolved(self):
        """When an emitter appears, the declaration has to be retired."""
        now_emitted = sorted(set(UNEMITTED_PLANNER_CODES) & set(self.emitted))
        self.assertEqual(
            now_emitted,
            [],
            "these codes now have a real emitter; remove them from "
            "UNEMITTED_PLANNER_CODES and register them instead",
        )

    def test_disposition_of_is_fail_closed_for_unknown_codes(self):
        self.assertEqual(disposition_of("no_such_reason_code"), UNCLASSIFIED)
        self.assertEqual(
            disposition_of("generator_connection_contract_incomplete"),
            REPAIRABLE,
        )

    def test_informational_codes_cannot_enter_repair_admission(self):
        """A quiet fact/wrapper can never become a target verdict by itself."""
        admitted = sorted(
            code for code in codes_with(INFORMATIONAL)
            if orchestration.has_repair_contract_evidence({
                "reason_token": code,
            })
        )
        self.assertEqual(
            admitted,
            [],
            "informational-only evidence entered target repair/final planning",
        )

    def test_codes_emitted_by_terminal_fallbacks_are_not_informational(self):
        terminal_fallbacks = {
            "pcg_cluster_handoff_not_ready",
            "preflight_error",
            "dependency_root_reason_missing",
        }
        quiet = sorted(
            code for code in terminal_fallbacks
            if disposition_of(code) == INFORMATIONAL
        )
        self.assertEqual(quiet, [])

    def test_observed_standalone_terminal_failures_are_not_informational(self):
        payload = json.loads(
            OBSERVED_TOKEN_FIXTURE.read_text(encoding="utf-8")
        )
        terminal = payload["standalone_terminal_failure_tokens"]

        self.assertEqual(
            terminal,
            {
                "data_error": 24,
                "unreal_unavailable": 4,
                "internal_error": 3,
            },
        )
        self.assertEqual(
            sorted(
                token for token in terminal
                if disposition_of(token) == INFORMATIONAL
            ),
            [],
            "a standalone terminal failure cannot use the informational bucket",
        )
        self.assertTrue(
            all(disposition_of(token) == UNSUPPORTED for token in terminal)
        )

    def test_generic_terminal_failure_is_visible_but_does_not_mask_repair(self):
        generic = {"reason_token": "data_error"}
        self.assertTrue(orchestration.has_repair_contract_evidence(generic))
        decision = orchestration.repair_ui_decision(generic)
        self.assertEqual(decision["status"], orchestration.REPAIR_UI_BLOCKED)
        self.assertIn("실패", decision["reason"])

        target = str(REPO_DIR / "sanitized" / "SK_target.spm")
        plan = orchestration.build_exact_target_repair_plan(
            target,
            {
                "reason_codes": [
                    "data_error",
                    "canonical_output_missing",
                ],
            },
            inventory_paths=[target],
            parent_retry_id="parent",
            request_id="request",
            require_exists=False,
        )
        self.assertTrue(plan.supported)
        self.assertEqual(plan.stages[0]["stage"], "pcg_texture")

    def test_sanitized_observed_tokens_are_registered_and_decided(self):
        payload = json.loads(
            OBSERVED_TOKEN_FIXTURE.read_text(encoding="utf-8")
        )
        tokens = set(payload["tokens"])

        self.assertEqual(len(tokens), 20)
        self.assertEqual(tokens - set(REASON_REGISTRY), set())
        self.assertEqual(
            sorted(
                token for token in tokens
                if disposition_of(token) == UNCLASSIFIED
            ),
            [],
        )

    def test_sanitized_integrity_reasons_are_registered_and_decided(self):
        payload = json.loads(
            OBSERVED_INTEGRITY_FIXTURE.read_text(encoding="utf-8")
        )
        tokens = set(payload["integrity_issue_occurrences"])
        tokens.update(payload["blocking_target_counts"])

        self.assertEqual(len(tokens), 4)
        self.assertEqual(tokens - set(REASON_REGISTRY), set())
        self.assertEqual(
            sorted(
                token for token in tokens
                if disposition_of(token) == UNCLASSIFIED
            ),
            [],
        )


class ReasonScanTests(unittest.TestCase):
    def test_scan_finds_codes_through_every_supported_shape(self):
        module = REPO_DIR / "sk_batch" / "atlas_consumer_integrity.py"
        codes = emitted_reason_codes(REPO_DIR)
        self.assertIn("managed_mesh_owner_ambiguous", codes)
        self.assertIn(
            "sk_batch/atlas_consumer_integrity.py",
            codes["managed_mesh_owner_ambiguous"],
        )
        self.assertIn("normalized_generator_delivery_incomplete", codes)
        self.assertIn(
            "pcg_st9_texture_batch/pcg_cluster_assembly_contract.py",
            codes["normalized_generator_delivery_incomplete"],
        )
        self.assertTrue(module.is_file())

    def test_scan_ignores_prose_and_paths(self):
        from repair_reason_scan import CODE_TOKEN

        self.assertIsNone(CODE_TOKEN.match("Cluster is not ready"))
        self.assertIsNone(CODE_TOKEN.match("D:/Assets/SK_x.spm"))
        self.assertIsNotNone(CODE_TOKEN.match("cluster_relation_stale"))

    def test_scan_descends_dynamic_literals_and_reason_parameters(self):
        source = '''
UNREAL_RECOVERY_FAILURE_KINDS = frozenset({"data_error"})

def unavailable(reason_token):
    return {"reason_token": reason_token}

def result(classification, reason_code):
    return {"classification": classification, "reason": reason_code}

def emit(flag, first):
    unavailable("stale_target_mesh_scope_missing")
    result("blocked", "structured_blender_failure")
    return {
        "reason_token": (
            "automatic_repair_reaudit_failed"
            if flag else "automatic_repair_failed"
        ),
        "reason": first or "retry_evidence_ambiguous",
    }
'''
        with tempfile.TemporaryDirectory() as temporary:
            module = Path(temporary) / "synthetic_reasons.py"
            module.write_text(source, encoding="utf-8")
            codes = scan_module(module)

        self.assertTrue({
            "data_error",
            "stale_target_mesh_scope_missing",
            "structured_blender_failure",
            "automatic_repair_reaudit_failed",
            "automatic_repair_failed",
            "retry_evidence_ambiguous",
        }.issubset(codes))


if __name__ == "__main__":
    unittest.main()
