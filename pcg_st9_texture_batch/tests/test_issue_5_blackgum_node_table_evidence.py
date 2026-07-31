"""Validate the issue #5 blackgum stale-node-table evidence fixture.

This fixture records the *asset* evidence for issue #5 (stale saved
``<Node>`` tables in ``SK_bush_blackgum_02/03.spm``): the confirmed
before-resave counts, empty pending after-resave slots, and a reproduction
checklist. It intentionally holds no tool/diagnostic code -- that lives in
issue #4 (``pcg_texture_audit._node_table_state`` /
``pcg_cluster_assembly_contract``) and must not be edited from here.

These tests check the fixture is internally consistent and, separately,
that the *current* node-table staleness policy (owned by #4, read-only
here) would classify synthetic documents built from the fixture's "before"
numbers exactly the way the fixture claims. That ties the checked-in
evidence to the real parser instead of letting it drift into an unverified
claim.
"""

import json
import sys
import unittest
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parents[2]
for candidate in (REPO_DIR, REPO_DIR / "pcg_st9_texture_batch"):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

import pcg_texture_audit as audit  # noqa: E402

FIXTURE_PATH = (
    Path(__file__).resolve().parent
    / "fixtures"
    / "issue_5_blackgum_node_table_evidence.json"
)

# Local-machine leakage this fixture must never contain: absolute drive
# paths, the configured external asset root, or an OS username.
FORBIDDEN_SUBSTRINGS = (
    "C:\\Users",
    "C:/Users",
    "D:\\OneDrive",
    "D:/OneDrive",
    "PARK",
)


def load_fixture():
    with open(FIXTURE_PATH, "r", encoding="utf-8") as handle:
        return json.load(handle)


def synthetic_node_table_document(
    *, current_generator_count, valid_guid_node_counts, orphan_guid_node_counts
):
    """Build a minimal SPM-like document matching given node-table counts.

    ``valid_guid_node_counts`` maps a subset of the current Generator GUIDs
    to how many nodes they own; ``orphan_guid_node_counts`` maps GUIDs that
    own nodes but have no matching Generator in the document.
    """
    generator_guids = list(valid_guid_node_counts) + [
        f"live-only-{i}"
        for i in range(current_generator_count - len(valid_guid_node_counts))
    ]
    assert len(generator_guids) == current_generator_count

    generators = [
        (
            f'<Generator Type="Leaf Mesh"><Name>g{i}</Name>'
            f"<GUID>{guid}</GUID><Hidden>false</Hidden>"
            "<Properties>"
            "<Property><Name>Leaves:Material</Name><Value>4</Value></Property>"
            "<Property><Name>Leaves:Mesh</Name><Value>130</Value></Property>"
            "</Properties></Generator>"
        )
        for i, guid in enumerate(generator_guids)
    ]

    nodes = []
    index = 0
    for guid, count in {
        **valid_guid_node_counts,
        **orphan_guid_node_counts,
    }.items():
        for _ in range(count):
            nodes.append(
                f"<Node><GeneratorGUID>{guid}</GeneratorGUID>"
                f"<ParentGUID></ParentGUID><Name>n{index}</Name>"
                f"<GUID>node{index}</GUID><Hidden>false</Hidden>"
                "<Extra></Extra></Node>"
            )
            index += 1

    return (
        '<?xml version="1.0"?><SpeedTree>'
        f"<Generators>{''.join(generators)}</Generators>"
        f"<Nodes>{''.join(nodes)}</Nodes>"
        "</SpeedTree>"
    )


def node_table_state_for(document_text):
    counts, total = audit._export_node_counts_from_text(document_text)
    return audit._node_table_state(
        counts, total, audit._generator_guid_keys_from_text(document_text)
    )


class FixtureLoadsTests(unittest.TestCase):
    def test_fixture_is_valid_json_with_expected_top_level_shape(self):
        data = load_fixture()
        self.assertEqual(data["schema_version"], 1)
        self.assertEqual(
            data["contract"], "speedtree_blackgum_node_table_evidence_v1"
        )
        self.assertEqual(data["issue_number"], 5)
        for key in (
            "parser",
            "delivery_context",
            "stale_assets",
            "coherent_reference_assets",
            "resave_tool",
            "reproduction_checklist",
            "completion_checklist",
        ):
            self.assertIn(key, data)

    def test_fixture_has_no_local_machine_leakage(self):
        raw = FIXTURE_PATH.read_text(encoding="utf-8")
        for needle in FORBIDDEN_SUBSTRINGS:
            self.assertNotIn(
                needle, raw, f"fixture leaks local-machine detail: {needle!r}"
            )

    def test_asset_paths_are_relative_filenames_only(self):
        data = load_fixture()
        assets = data["stale_assets"] + data["coherent_reference_assets"]
        for asset in assets:
            path = asset["asset_relative_path"]
            self.assertNotIn(":", path)
            self.assertNotIn("\\", path)
            self.assertFalse(path.startswith("/"))
            self.assertTrue(path.endswith(".spm"))


class StaleAssetInvariantTests(unittest.TestCase):
    def test_each_stale_asset_before_block_is_internally_consistent(self):
        data = load_fixture()
        for asset in data["stale_assets"]:
            before = asset["before"]
            self.assertGreater(before["orphan_node_count"], 0)
            self.assertLessEqual(
                before["orphan_node_count"], before["total_node_count"]
            )
            self.assertTrue(before["stale"])
            if before["orphan_generator_guid_count"] is not None:
                self.assertLessEqual(
                    before["orphan_generator_guid_count"],
                    before["node_table_generator_count"],
                )

    def test_each_stale_asset_after_slot_stays_pending_not_fabricated(self):
        data = load_fixture()
        for asset in data["stale_assets"]:
            after = asset["after"]
            self.assertEqual(after["status"], "pending")
            self.assertFalse(after["resaved"])
            for field in (
                "captured_utc",
                "current_generator_count",
                "node_table_generator_count",
                "orphan_generator_guid_count",
                "orphan_node_count",
                "total_node_count",
                "stale",
                "spm_text_sha256",
            ):
                self.assertIsNone(
                    after[field],
                    f"{asset['asset_id']}.after.{field} must stay null until a"
                    " real SpeedTree Modeler resave has happened",
                )
            self.assertEqual(
                asset["audit_outcome"], "blocked_pending_manual_modeler_resave"
            )

    def test_resave_tool_has_not_actually_run(self):
        data = load_fixture()
        tool = data["resave_tool"]
        self.assertFalse(tool["executed"])
        self.assertIsNone(tool["executed_utc"])
        self.assertEqual(data["audit_outcome"], "blocked_pending_manual_resave")


class CoherentReferenceAssetTests(unittest.TestCase):
    def test_coherent_reference_assets_report_zero_orphans(self):
        data = load_fixture()
        for asset in data["coherent_reference_assets"]:
            self.assertFalse(asset["stale"])
            self.assertEqual(asset["orphan_node_count"], 0)
            self.assertEqual(asset["orphan_generator_guid_count"], 0)
            self.assertEqual(
                asset["current_generator_count"],
                asset["node_table_generator_count"],
            )


class DeliveryContextTests(unittest.TestCase):
    def test_declared_target_mesh_ids_match_issue_evidence(self):
        data = load_fixture()
        self.assertEqual(
            data["delivery_context"]["declared_target_mesh_ids"],
            [130, 131, 132, 133],
        )
        self.assertEqual(
            data["delivery_context"][
                "live_export_participating_target_mesh_ids_before_resave"
            ],
            [],
        )


class CompletionChecklistTests(unittest.TestCase):
    def test_checklist_mirrors_issue_acceptance_criteria_and_stays_pending(self):
        data = load_fixture()
        items = {row["item"]: row for row in data["completion_checklist"]}
        self.assertEqual(len(items), 5)
        pending_items = (
            "both_affected_spms_report_zero_orphan_node_ownership",
            "required_current_generator_guids_receive_valid_live_export_evidence",
            "live_export_participating_target_mesh_ids_equals_normalized_declared_set",
            "stale_node_table_validation_block_no_longer_occurs_after_resave",
        )
        for name in pending_items:
            self.assertEqual(items[name]["status"], "pending")
        # The no-bypass criterion is #4's tool-code concern; #5 stays asset-only.
        self.assertEqual(
            items["no_validation_bypass_or_fail_open_special_case_introduced"][
                "status"
            ],
            "not_applicable_to_this_asset_only_fixture",
        )


class ParserCrossCheckTests(unittest.TestCase):
    """Confirm the fixture's before-resave numbers really parse as stale.

    This does not need the real (external, not checked in) SPM files -- it
    reconstructs a document with the same GUID/node-count shape and runs it
    through the actual, currently-committed policy function.
    """

    def test_blackgum_02_before_counts_parse_as_stale(self):
        data = load_fixture()
        before = next(
            a for a in data["stale_assets"] if a["asset_id"] == "SK_bush_blackgum_02"
        )["before"]

        orphan_guid_count = before["orphan_generator_guid_count"]
        valid_guid_count = before["node_table_generator_count"] - orphan_guid_count
        orphan_nodes = before["orphan_node_count"]
        valid_nodes = before["total_node_count"] - orphan_nodes

        valid_guid_node_counts = {
            f"valid-{i}": 1 for i in range(valid_guid_count - 1)
        }
        valid_guid_node_counts[f"valid-{valid_guid_count - 1}"] = (
            valid_nodes - (valid_guid_count - 1)
        )
        orphan_guid_node_counts = {
            f"orphan-{i}": 1 for i in range(orphan_guid_count - 1)
        }
        orphan_guid_node_counts[f"orphan-{orphan_guid_count - 1}"] = (
            orphan_nodes - (orphan_guid_count - 1)
        )

        text = synthetic_node_table_document(
            current_generator_count=before["current_generator_count"],
            valid_guid_node_counts=valid_guid_node_counts,
            orphan_guid_node_counts=orphan_guid_node_counts,
        )
        state = node_table_state_for(text)

        self.assertEqual(state["generator_count"], before["current_generator_count"])
        self.assertEqual(
            state["node_table_generator_count"], before["node_table_generator_count"]
        )
        self.assertEqual(state["orphan_node_count"], before["orphan_node_count"])
        self.assertEqual(state["total_node_count"], before["total_node_count"])
        self.assertEqual(len(state["orphan_generator_guids"]), orphan_guid_count)
        self.assertTrue(state["stale"])
        self.assertEqual(state["stale"], before["stale"])

    def test_coherent_reference_counts_parse_as_not_stale(self):
        data = load_fixture()
        for asset in data["coherent_reference_assets"]:
            count = asset["current_generator_count"]
            valid_guid_node_counts = {f"g-{i}": 1 for i in range(count)}
            text = synthetic_node_table_document(
                current_generator_count=count,
                valid_guid_node_counts=valid_guid_node_counts,
                orphan_guid_node_counts={},
            )
            state = node_table_state_for(text)
            self.assertFalse(state["stale"])
            self.assertEqual(state["orphan_node_count"], 0)
            self.assertEqual(
                state["node_table_generator_count"], asset["node_table_generator_count"]
            )


if __name__ == "__main__":
    unittest.main()
