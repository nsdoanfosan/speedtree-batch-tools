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
    BLOCKING_DISPOSITIONS,
    FATAL,
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
    reason_row,
)
from repair_reason_scan import (  # noqa: E402
    emitted_reason_codes,
    production_sources,
    scan_module,
)


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
        sources = tuple(production_sources(REPO_DIR))
        if len(sources) < 100:
            raise AssertionError(
                "repair reason coverage scanned only "
                f"{len(sources)} production sources under {REPO_DIR}; "
                "expected at least 100. Check production_sources() path "
                "filtering before changing the reason registry."
            )
        cls.emitted = emitted_reason_codes(REPO_DIR)
        if "managed_mesh_owner_ambiguous" not in cls.emitted:
            raise AssertionError(
                "repair reason coverage is missing the known production "
                "sentinel managed_mesh_owner_ambiguous; the source scan is "
                "empty, partial, or filtering the wrong checkout scope."
            )
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
        """The domain disposition work is complete, not merely ratcheted."""
        unclassified = codes_with(UNCLASSIFIED)
        self.assertEqual(UNCLASSIFIED_CEILING, 0)
        self.assertEqual(unclassified, ())

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
        self.assertEqual(UNEMITTED_PLANNER_CODES, frozenset())

    def test_disposition_of_is_fail_closed_for_unknown_codes(self):
        self.assertEqual(disposition_of("no_such_reason_code"), UNSUPPORTED)
        unknown = reason_row("no_such_reason_code")
        self.assertEqual(unknown.owner, "unregistered_runtime_reason")
        self.assertTrue(unknown.friendly_cause)
        self.assertTrue(unknown.operator_action)
        self.assertTrue(orchestration.has_repair_contract_evidence({
            "reason_token": "no_such_reason_code",
        }))
        self.assertEqual(
            disposition_of("generator_connection_contract_incomplete"),
            REPAIRABLE,
        )

    def test_role_handoff_wrapper_defers_to_inner_repairable_reason(self):
        evidence = {
            "issues": [{
                "code": "CLUSTER_ROLE_HANDOFF_BLOCKED",
                "reason": "normalized_variants_required",
            }],
        }
        decision = orchestration.repair_ui_decision(evidence)
        self.assertEqual(decision["status"], orchestration.REPAIR_UI_AUTOMATIC)
        self.assertEqual(
            decision["reason_codes"],
            ("cluster_role_handoff_blocked", "normalized_variants_required"),
        )
        self.assertEqual(
            disposition_of("cluster_role_handoff_blocked"),
            INFORMATIONAL,
        )

    def test_declared_owner_is_one_of_each_static_codes_emitters(self):
        mismatches = []
        for code, emitters in self.emitted.items():
            row = REASON_REGISTRY.get(code)
            if row is None:
                continue
            owner = row.owner
            if owner not in emitters:
                mismatches.append((code, owner, tuple(emitters)))
        self.assertEqual(
            mismatches,
            [],
            "registry owner must be one of the modules that statically emits "
            "the code; update the owner or canonicalize the emitter token",
        )

    def test_every_blocking_row_has_one_complete_domain_contract(self):
        invalid = []
        for code, row in REASON_REGISTRY.items():
            if row.disposition == INFORMATIONAL:
                continue
            if row.disposition not in BLOCKING_DISPOSITIONS:
                invalid.append((code, "disposition"))
            if not row.owner or not row.policy:
                invalid.append((code, "owner/policy"))
            if not row.friendly_cause or not row.operator_action:
                invalid.append((code, "operator presentation"))
            if row.disposition == REPAIRABLE:
                if not row.repair_action or not row.evidence_requirements:
                    invalid.append((code, "repair action/evidence"))
            elif row.repair_action or row.evidence_requirements:
                invalid.append((code, "terminal row advertises repair"))
            if row.phase_routed and not (
                row.disposition == UNSUPPORTED and row.fallback_only
            ):
                invalid.append((code, "phase route is not fallback terminal"))
        self.assertEqual(invalid, [])

    def test_representative_domain_dispositions_are_explicit(self):
        texture_availability = reason_row("output_set_incomplete")
        unsupported = reason_row("managed_mesh_owner_ambiguous")
        fatal = reason_row("duplicate_material_id")

        self.assertEqual(texture_availability.disposition, INFORMATIONAL)
        self.assertEqual(texture_availability.repair_action, "")
        self.assertEqual(texture_availability.evidence_requirements, ())
        self.assertEqual(unsupported.disposition, UNSUPPORTED)
        self.assertEqual(unsupported.owner, "sk_batch/atlas_consumer_integrity.py")
        self.assertTrue(unsupported.operator_action)
        self.assertEqual(fatal.disposition, FATAL)
        self.assertEqual(fatal.owner, "sk_batch/atlas_consumer_integrity.py")
        self.assertTrue(fatal.operator_action)

    def test_texture_omission_reasons_are_informational(self):
        reasons = {
            "canonical_material_has_no_maps",
            "copied_candidate_unavailable",
            "same_authority_candidates_disagree",
            "unreadable",
            "write_verification_mismatch",
        }

        self.assertTrue(all(
            reason_row(code).disposition == INFORMATIONAL
            for code in reasons
        ))
        self.assertTrue(all(
            reason_row(code).policy == "texture_availability"
            for code in reasons
        ))
        self.assertTrue(all(
            not orchestration.has_repair_contract_evidence({
                "reason_token": code,
            })
            for code in reasons
        ))

    def test_historical_planner_only_aliases_are_not_contract_rows(self):
        aliases = {
            "cluster_stale",
            "managed_texture_set_stale",
            "generator_slot_pair_drift",
            "canonical_texture_output_unmapped",
            "atlas_strict_material_binding_conflict",
            "blender_cluster_bake_map_role_mismatch",
        }
        self.assertEqual(aliases & set(REASON_REGISTRY), set())
        self.assertTrue(all(
            reason_row(code).disposition == UNSUPPORTED for code in aliases
        ))

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

    def test_generic_terminal_failure_is_not_masked_by_texture_telemetry(self):
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
        self.assertFalse(plan.supported)
        self.assertEqual(plan.stages, ())

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
    def test_production_source_skips_are_scoped_to_checkout_relative_paths(self):
        with tempfile.TemporaryDirectory() as temporary:
            checkout = (
                Path(temporary)
                / "work"
                / ".claude"
                / "tests"
                / "speedtree-batch-tools"
            )
            production = checkout / "package" / "module.py"
            repository_test = checkout / "tests" / "test_module.py"
            vendored = checkout / "work" / "vendor.py"
            for path in (production, repository_test, vendored):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("VALUE = 1\n", encoding="utf-8")

            found = {
                path.relative_to(checkout).as_posix()
                for path in production_sources(checkout)
            }

        self.assertEqual(found, {"package/module.py"})

    def test_scan_resolves_reason_values_through_scope_local_names(self):
        with tempfile.TemporaryDirectory() as temporary:
            module = Path(temporary) / "local_reason.py"
            module.write_text(
                "def first(flag):\n"
                "    evidence = 'first_local_reason'\n"
                "    if flag:\n"
                "        evidence = 'second_local_reason'\n"
                "    return {'reason': evidence}\n"
                "\n"
                "def unrelated():\n"
                "    evidence = 'must_not_leak_between_scopes'\n"
                "    return {'message': evidence}\n",
                encoding="utf-8",
            )
            found = scan_module(module)
        self.assertEqual(
            found,
            {"first_local_reason", "second_local_reason"},
        )

    def test_role_handoff_emits_the_canonical_generator_connection_reason(self):
        module = (
            REPO_DIR
            / "sk_batch"
            / "cluster_assembly_handoff_contract.py"
        )
        codes = scan_module(module)
        self.assertIn("generator_connection_contract_incomplete", codes)
        self.assertNotIn("generator_connection_incomplete", codes)

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

    def test_new_helper_emission_is_unknown_to_registry_and_fails_closed(self):
        source = '''
def add_blocking_issue(code, details):
    return {"code": code, "details": details}

def gate():
    return add_blocking_issue("brand_new_blocking_code", {})
'''
        with tempfile.TemporaryDirectory() as temporary:
            module = Path(temporary) / "synthetic_gate.py"
            module.write_text(source, encoding="utf-8")
            emitted = scan_module(module)

        self.assertIn("brand_new_blocking_code", emitted)
        self.assertNotIn("brand_new_blocking_code", REASON_REGISTRY)
        self.assertEqual(
            reason_row("brand_new_blocking_code").disposition,
            UNSUPPORTED,
        )

    def test_scan_finds_reason_defaults_and_keyword_overrides(self):
        source = '''
def target_failure(iid, default_token="item_failed"):
    return {"reason_token": default_token}

def route(aborted):
    return target_failure(
        "target",
        default_token=(
            "pipeline_aborted" if aborted else "dependency_root_reason_missing"
        ),
    )

def terminal(*, default_reason_token="owner_lost"):
    return {"terminal_reason": default_reason_token}
'''
        with tempfile.TemporaryDirectory() as temporary:
            module = Path(temporary) / "synthetic_default_gate.py"
            module.write_text(source, encoding="utf-8")
            emitted = scan_module(module)

        self.assertTrue({
            "item_failed",
            "pipeline_aborted",
            "dependency_root_reason_missing",
            "owner_lost",
        }.issubset(emitted))


if __name__ == "__main__":
    unittest.main()
