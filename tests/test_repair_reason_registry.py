"""The repair contract must cover every reason code the pipeline can emit.

These four assertions are the whole point of the registry.  Each one failed
silently before it existed, and each failure mode had already reached
production: a blocked target with no recovery path, and no record that a
recovery path was ever possible.
"""
import sys
import unittest
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parents[1]
if str(REPO_DIR) not in sys.path:
    sys.path.insert(0, str(REPO_DIR))

import repair_orchestration as orchestration  # noqa: E402
from repair_reason_registry import (  # noqa: E402
    DISPOSITIONS,
    REASON_REGISTRY,
    REPAIRABLE,
    UNCLASSIFIED,
    UNCLASSIFIED_CEILING,
    UNEMITTED_PLANNER_CODES,
    codes_with,
    disposition_of,
)
from repair_reason_scan import emitted_reason_codes  # noqa: E402


class ReasonRegistryCoverageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.emitted = emitted_reason_codes(REPO_DIR)

    def test_every_emitted_reason_code_is_registered(self):
        """A new block cannot ship without a disposition.

        This is the assertion that ends the discovery loop.  On 2026-08-03 the
        planner owned 31 codes while production emitted 174, and the only way
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

    def test_repairable_codes_are_actually_emitted(self):
        """No dead vocabulary.

        `repair_orchestration` mapped 20+ repairable codes that no module ever
        emitted, so the planner could not fire on them however correct the
        mapping was. A repair path that nothing can trigger is not a repair
        path.
        """
        dead = sorted(set(codes_with(REPAIRABLE)) - set(self.emitted))
        self.assertEqual(
            dead,
            [],
            "codes registered as repairable that no production module emits; "
            "either wire the emitter or drop the mapping",
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


class ReasonScanTests(unittest.TestCase):
    def test_scan_finds_codes_through_every_supported_shape(self):
        module = REPO_DIR / "sk_batch" / "atlas_consumer_integrity.py"
        codes = emitted_reason_codes(REPO_DIR)
        self.assertIn("managed_mesh_owner_ambiguous", codes)
        self.assertIn(
            "sk_batch/atlas_consumer_integrity.py",
            codes["managed_mesh_owner_ambiguous"],
        )
        self.assertTrue(module.is_file())

    def test_scan_ignores_prose_and_paths(self):
        from repair_reason_scan import CODE_TOKEN

        self.assertIsNone(CODE_TOKEN.match("Cluster is not ready"))
        self.assertIsNone(CODE_TOKEN.match("D:/Assets/SK_x.spm"))
        self.assertIsNotNone(CODE_TOKEN.match("cluster_relation_stale"))


if __name__ == "__main__":
    unittest.main()
