"""Sanitized acceptance evidence for issue #10 Lauraceae recovery."""

import json
import re
import unittest
from pathlib import Path


FIXTURE = Path(__file__).parent / "fixtures" / (
    "issue_10_lauraceae_node_table_evidence.json"
)
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
WINDOWS_PATH_RE = re.compile(
    r"(?i)(?:(?<![a-z])[a-z]:[\\/]|users[\\/][^\\/]+)"
)
RAW_GUID_RE = re.compile(r"(?i)[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def load_fixture():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


class LauraceaeEvidenceTests(unittest.TestCase):
    def test_fixture_is_sanitized_and_issue_scoped(self):
        raw = FIXTURE.read_text(encoding="utf-8")
        data = json.loads(raw)
        self.assertEqual(data["schema_version"], 1)
        self.assertEqual(data["issue_number"], 10)
        self.assertEqual(data["result_status"], "resave_reaudit_valid")
        self.assertIsNone(WINDOWS_PATH_RE.search(raw))
        self.assertIsNone(RAW_GUID_RE.search(raw))
        self.assertEqual(
            [asset["asset_name"] for asset in data["assets"]],
            ["SK_tree_Lauraceae_11.spm", "SK_tree_Lauraceae_12.spm"],
        )

    def test_before_counts_match_the_issue_baseline(self):
        expected = {
            "SK_tree_Lauraceae_11.spm": (34, 33, 60103, 4, 29986),
            "SK_tree_Lauraceae_12.spm": (31, 30, 60101, 4, 30034),
        }
        for asset in load_fixture()["assets"]:
            before = asset["before"]
            observed = (
                before["generator_count"],
                before["node_table_owner_count"],
                before["total_node_count"],
                before["orphan_owner_count"],
                before["orphan_node_count"],
            )
            self.assertEqual(observed, expected[asset["asset_name"]])
            self.assertTrue(before["stale"])
            self.assertTrue(before["regex_elementtree_parity"])

    def test_hashes_change_and_exact_preimages_are_receipted(self):
        for asset in load_fixture()["assets"]:
            before = asset["before"]
            after = asset["after"]
            for block in (before, after):
                self.assertRegex(block["raw_sha256"], SHA256_RE)
                self.assertRegex(block["text_sha256"], SHA256_RE)
            self.assertNotEqual(before["raw_sha256"], after["raw_sha256"])
            self.assertNotEqual(before["text_sha256"], after["text_sha256"])
            seal = asset["sealed_preimage"]
            self.assertRegex(seal["receipt_sha256"], SHA256_RE)
            self.assertTrue(seal["exact_backup_verified"])
            self.assertIn(seal["receipt_schema_version"], {2, 3})

    def test_post_save_gates_and_target_delivery_are_complete(self):
        expected_meshes = {
            "SK_tree_Lauraceae_11.spm": [43, 45, 46, 47],
            "SK_tree_Lauraceae_12.spm": [53, 55, 56, 57],
        }
        for asset in load_fixture()["assets"]:
            self.assertEqual(
                asset["required_mesh_ids"],
                expected_meshes[asset["asset_name"]],
            )
            after = asset["after"]
            self.assertEqual(after["orphan_owner_count"], 0)
            self.assertEqual(after["orphan_node_count"], 0)
            self.assertFalse(after["stale"])
            self.assertGreater(after["target_binding_count"], 0)
            self.assertEqual(after["material_scope_count"], 2)
            gates = asset["gates"]
            self.assertTrue(gates["valid"])
            self.assertTrue(gates["regex_elementtree_parity"])
            self.assertTrue(gates["authoring_graph_core_continuity"])
            self.assertFalse(gates["authoring_graph_exact_projection_continuity"])
            self.assertTrue(gates["generator_membership_continuity"])
            self.assertTrue(gates["required_target_binding_continuity"])
            self.assertTrue(gates["normalization_evidence_complete"])

    def test_modeler_action_and_remaining_boundary_are_explicit(self):
        data = load_fixture()
        self.assertEqual(
            data["authoring_graph_contract"]["core_projection_version"],
            2,
        )
        self.assertIn(
            "graph-visible bindings",
            data["target_scope_contract"]["authoring_continuity"],
        )
        self.assertIn(
            "export participation",
            data["target_scope_contract"]["live_delivery"],
        )
        self.assertEqual(data["resave_tool"]["version"], "10.1.0")
        self.assertEqual(data["resave_tool"]["interaction"], "Computer Use")
        self.assertIn("Save once", data["resave_tool"]["action"])
        self.assertEqual(len(data["remaining_external_verification"]), 1)
        self.assertIn(
            "Unreal push",
            data["remaining_external_verification"][0],
        )
        self.assertTrue(data["validation"]["focused_test_result"].startswith("passed"))
        self.assertTrue(data["validation"]["compile_gate_result"].startswith("passed"))


if __name__ == "__main__":
    unittest.main()
