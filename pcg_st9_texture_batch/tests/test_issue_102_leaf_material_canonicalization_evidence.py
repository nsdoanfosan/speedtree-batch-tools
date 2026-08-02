import json
import unittest
from pathlib import Path


FIXTURE = (
    Path(__file__).resolve().parent
    / "fixtures"
    / "issue_102_leaf_material_canonicalization_evidence.json"
)


class Issue102EvidenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.raw = FIXTURE.read_text(encoding="utf-8")
        cls.data = json.loads(cls.raw)

    def test_fixture_is_sanitized_and_scoped_to_issue_102(self):
        self.assertEqual(
            self.data["contract"],
            "speedtree_issue_102_leaf_material_canonicalization_evidence_v1",
        )
        self.assertEqual(self.data["schema_version"], 1)
        self.assertEqual(self.data["source_issue"], 102)
        self.assertTrue(all(
            value is False for value in self.data["sanitization"].values()
        ))
        for forbidden in (
            "PARK",
            "OneDrive",
            "Forestportfolio",
            "D:\\",
            "C:\\Users",
            "<SpeedTree",
        ):
            self.assertNotIn(forbidden.casefold(), self.raw.casefold())

    def test_production_observation_pins_the_exact_fail_closed_shape(self):
        observation = self.data["production_observation"]
        self.assertEqual(observation["modeler_root_metadata"], {
            "title": "Modeler 10.1.0",
            "version": "8",
            "version_string": "10.1.0",
        })
        self.assertTrue(observation["namespace_free"])
        self.assertEqual(observation["generator_type"], "Leaf Mesh")
        self.assertEqual(observation["type_indices"], [0, 1, 2, 3])
        self.assertEqual(observation["property_shape"], {
            "property_attributes": 0,
            "ordered_children": ["Name", "Value"],
            "name_cardinality": 1,
            "value_cardinality": 1,
        })
        self.assertEqual(observation["material_transition"], {
            "before": "-1",
            "after": "0",
            "exact_change_count": 4,
        })
        self.assertEqual(observation["paired_mesh"], {
            "before": "-10",
            "after": "-10",
            "exact_pair_count": 4,
        })
        self.assertEqual(
            observation["core_v4_uncovered_changes"],
            [f"Leaves:Type:{index}:Material" for index in range(4)],
        )

    def test_material_table_claim_depends_only_on_existing_core_v4(self):
        table = self.data["production_observation"]["material_table"]
        self.assertEqual(table["count_before"], 5)
        self.assertEqual(table["count_after"], 5)
        self.assertTrue(table["identity_order_equal"])
        self.assertTrue(table["existing_core_v4_projection_equal"])
        self.assertFalse(table["raw_subtree_byte_equal"])
        self.assertTrue(
            table["raw_differences_covered_only_by_existing_core_v4_rules"]
        )
        self.assertFalse(table["contains_material_id_zero"])

    def test_synthetic_material_zero_control_is_not_ui_proof(self):
        control = self.data["synthetic_negative_control"]
        self.assertEqual(
            control["provenance"],
            "sanitized_test_only_not_supported_ui_proof",
        )
        self.assertTrue(control["material_table_contains_id_zero"])
        self.assertEqual(control["material_value"], "0")
        self.assertNotEqual(control["mesh_value"], "-10")
        self.assertTrue(control["material_zero_is_core_v4_observable"])
        self.assertFalse(control["global_minus_one_zero_collapse_allowed"])

    def test_missing_supported_ui_proof_cannot_authorize_implementation(self):
        proof = self.data["required_proof_status"]
        self.assertTrue(all(value is False for value in proof.values()))
        disposition = self.data["disposition"]
        self.assertEqual(disposition["status"], "intended_fail_closed_blocker")
        self.assertFalse(disposition["product_contract_changed"])
        self.assertFalse(disposition["asset_specific_exception_added"])


if __name__ == "__main__":
    unittest.main()
