"""Validate the public, sanitized Issue #23 asset-repair evidence."""

import json
import re
import unittest
from pathlib import Path


FIXTURE_PATH = (
    Path(__file__).resolve().parent
    / "fixtures"
    / "issue_23_blackgum_main_leaf_mesh_evidence.json"
)
SHA1_RE = re.compile(r"[0-9a-f]{40}")
SHA256_RE = re.compile(r"[0-9a-f]{64}")
RAW_UUID_RE = re.compile(
    r"(?i)(?<![0-9a-f])[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
    r"[0-9a-f]{4}-[0-9a-f]{12}(?![0-9a-f])"
)
WINDOWS_ABSOLUTE_PATH_RE = re.compile(r"(?i)(?<![a-z])[a-z]:[\\/]")
USER_HOME_PATH_RE = re.compile(r"(?i)(?:[\\/](?:users|home)[\\/])")


def load_fixture():
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


class EvidenceSanitizationTests(unittest.TestCase):
    def test_fixture_is_public_and_sanitized(self):
        data = load_fixture()
        self.assertEqual(data["schema_version"], 1)
        self.assertEqual(
            data["contract"],
            "speedtree_issue_23_blackgum_main_leaf_mesh_evidence_v1",
        )
        serialized = json.dumps(data, sort_keys=True)
        self.assertIsNone(RAW_UUID_RE.search(serialized))
        self.assertIsNone(WINDOWS_ABSOLUTE_PATH_RE.search(serialized))
        self.assertIsNone(USER_HOME_PATH_RE.search(serialized))
        self.assertNotRegex(serialized.casefold(), r'"generator_guid"\s*:')
        self.assertNotRegex(serialized.casefold(), r'"guid_token"\s*:')

    def test_commits_and_hashes_have_expected_shapes(self):
        data = load_fixture()
        repository = data["audit_repository"]
        self.assertRegex(repository["origin_main_commit"], rf"^{SHA1_RE.pattern}$")
        self.assertRegex(
            repository["issue_19_merge_commit"], rf"^{SHA1_RE.pattern}$"
        )
        for key in (
            "before_raw_sha256",
            "after_raw_sha256",
            "before_text_sha256",
            "after_text_sha256",
        ):
            self.assertRegex(data["asset"][key], rf"^{SHA256_RE.pattern}$")
        receipt = data["preimage_receipt"]
        self.assertRegex(receipt["backup_raw_sha256"], rf"^{SHA256_RE.pattern}$")
        self.assertRegex(receipt["receipt_sha256"], rf"^{SHA256_RE.pattern}$")
        self.assertEqual(
            receipt["backup_raw_sha256"], data["asset"]["before_raw_sha256"]
        )


class ExactRepairEvidenceTests(unittest.TestCase):
    def test_only_two_projected_leaf_mesh_bindings_changed(self):
        data = load_fixture()
        before = {
            row["slot_prefix"]: row for row in data["before"]["target_bindings"]
        }
        after = {
            row["slot_prefix"]: row for row in data["after"]["target_bindings"]
        }
        self.assertEqual(set(before), {"Leaves:Type:0", "Leaves:Type:1"})
        self.assertEqual(set(after), set(before))
        expected_meshes = {"Leaves:Type:0": 130, "Leaves:Type:1": 132}
        changed_fields = []
        for slot in sorted(before):
            self.assertEqual(before[slot]["mesh_id"], -10)
            self.assertEqual(after[slot]["mesh_id"], expected_meshes[slot])
            for field in before[slot]:
                if before[slot][field] != after[slot][field]:
                    changed_fields.append(f"{slot}:{field}")
        self.assertEqual(
            changed_fields,
            ["Leaves:Type:0:mesh_id", "Leaves:Type:1:mesh_id"],
        )
        scope = data["change_scope"]
        self.assertEqual(scope["changed_leaf_binding_count"], 2)
        self.assertEqual(
            scope["changed_leaf_binding_fields"],
            ["Leaves:Type:0:Mesh", "Leaves:Type:1:Mesh"],
        )

    def test_material_node_counts_and_node_table_are_preserved(self):
        data = load_fixture()
        for phase in ("before", "after"):
            for row in data[phase]["target_bindings"]:
                self.assertEqual(row["material_id"], 7)
                self.assertEqual(row["generated_node_count"], 440)
                self.assertTrue(row["graph_visible"])
                self.assertTrue(row["export_participates"])
                self.assertFalse(row["node_table_stale"])
        self.assertEqual(data["before"]["node_table"], data["after"]["node_table"])
        node_table = data["after"]["node_table"]
        self.assertEqual(node_table["generator_count"], 22)
        self.assertEqual(node_table["node_table_generator_count"], 18)
        self.assertEqual(node_table["orphan_generator_guid_count"], 0)
        self.assertEqual(node_table["orphan_node_count"], 0)
        self.assertEqual(node_table["total_node_count"], 2506)
        self.assertFalse(node_table["stale"])

    def test_negative_mesh_id_is_preimage_only_and_never_validated(self):
        data = load_fixture()
        self.assertEqual(
            {row["mesh_id"] for row in data["before"]["target_bindings"]}, {-10}
        )
        after_meshes = {
            row["mesh_id"] for row in data["after"]["target_bindings"]
        }
        self.assertEqual(after_meshes, {130, 132})
        self.assertTrue(all(mesh_id > 0 for mesh_id in after_meshes))


class LatestMainDeliveryEvidenceTests(unittest.TestCase):
    def test_issue_19_old_identity_tokens_are_absent(self):
        data = load_fixture()
        identity = data["issue_19_identity_regression"]
        self.assertEqual(
            set(identity["old_identity_tokens"]),
            {
                "visible_generator_slot_missing",
                "live_generator_slot_not_declared_exactly_once",
            },
        )
        self.assertEqual(
            identity["tokens_present_in_fresh_latest_main_delivery_errors"], []
        )
        self.assertNotIn(
            "visible_generator_slot_missing", data["before"]["delivery_errors"]
        )
        self.assertNotIn(
            "live_generator_slot_not_declared_exactly_once",
            data["before"]["delivery_errors"],
        )

    def test_repaired_delivery_matches_the_normalized_declared_set(self):
        data = load_fixture()
        delivery = data["after"]["delivery"]
        expected = data["declaration"]["required_target_mesh_ids"]
        self.assertEqual(delivery["delivery_mode"], "render_connected")
        self.assertTrue(delivery["delivery_complete"])
        self.assertEqual(delivery["errors"], [])
        self.assertEqual(delivery["missing_live_binding_count"], 0)
        self.assertEqual(delivery["live_export_participating_target_mesh_ids"], expected)
        self.assertEqual(delivery["current_required_target_mesh_ids"], expected)

    def test_continuity_caveat_keeps_issue_23_closure_incomplete(self):
        data = load_fixture()
        scope = data["change_scope"]
        status = data["result_status"]
        self.assertFalse(
            scope[
                "broad_authoring_graph_equal_after_reverting_only_intended_mesh_values"
            ]
        )
        self.assertEqual(status["issue_19_delivery_identity"], "closure_ready")
        self.assertEqual(status["closure"], "incomplete")
        self.assertEqual(len(status["closure_blockers"]), 2)


class UiReleaseEvidenceTests(unittest.TestCase):
    def test_exact_window_save_and_release_are_recorded(self):
        ui = load_fixture()["ui_execution"]
        self.assertTrue(ui["exact_asset_window_only"])
        self.assertTrue(ui["saved"])
        self.assertTrue(ui["speedtree_window_released"])
        self.assertTrue(ui["recovery_lock_released"])


if __name__ == "__main__":
    unittest.main()
