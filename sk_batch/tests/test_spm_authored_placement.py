from __future__ import annotations

import gzip
import sys
import tempfile
import unittest
from pathlib import Path


SK_BATCH = Path(__file__).resolve().parents[1]
REPO_DIR = SK_BATCH.parent
if str(REPO_DIR) not in sys.path:
    sys.path.insert(0, str(REPO_DIR))
if str(SK_BATCH) not in sys.path:
    sys.path.insert(0, str(SK_BATCH))

from repair_orchestration import REASON_KEYS  # noqa: E402
from spm_authored_placement import (  # noqa: E402
    SpmAuthoredPlacementError,
    assign_authored_nodes_to_components,
    match_authored_node,
    parse_spm_authored_placement,
)


def node_xml(
    guid,
    position,
    *,
    generator_guid="generator-a",
    parent_guid="parent-a",
    anchor=-1,
    hidden=False,
    valid=True,
    deleted=False,
    culled=False,
):
    x, y, z = position
    return f"""
    <Node>
      <GUID>{guid}</GUID>
      <GeneratorGUID>{generator_guid}</GeneratorGUID>
      <ParentGUID>{parent_guid}</ParentGUID>
      <Name>Leaf_node_(X: {x}, Y: {y}, Z: {z})</Name>
      <Hidden>{str(hidden).lower()}</Hidden>
      <Extra>
        <m_bValidPosition>{str(valid).lower()}</m_bValidPosition>
        <m_bDeleted>{str(deleted).lower()}</m_bDeleted>
        <m_bCulled>{str(culled).lower()}</m_bCulled>
        <m_nAnchorIndex>{anchor}</m_nAnchorIndex>
      </Extra>
    </Node>
    """


def spm_xml(nodes):
    return f"""<?xml version="1.0" encoding="utf-8"?>
    <Model>
      <Generators>
        <Generator Type="Leaf Mesh">
          <GUID>generator-a</GUID>
          <Properties>
            <Property><Name>Leaves:Type:0:Mesh</Name><Value>132</Value></Property>
          </Properties>
        </Generator>
      </Generators>
      <Nodes>{''.join(nodes)}</Nodes>
    </Model>
    """


class SpmAuthoredPlacementTests(unittest.TestCase):
    def _write(self, root, text, compressed=False):
        path = Path(root) / "asset.spm"
        if compressed:
            with gzip.open(path, "wb") as handle:
                handle.write(text.encode("utf-8"))
        else:
            path.write_text(text, encoding="utf-8")
        return path

    def test_plain_and_gzip_parse_absolute_meter_position_parent_and_anchor(self):
        text = spm_xml([
            node_xml(
                "node-a",
                ("+1.0e0", "-2.0", ".5"),
                parent_guid="parent-is-lineage-not-a-position",
                anchor=7,
            )
        ])
        for compressed in (False, True):
            with self.subTest(compressed=compressed), tempfile.TemporaryDirectory() as root:
                table = parse_spm_authored_placement(
                    self._write(root, text, compressed)
                )
                self.assertTrue(table["available"])
                node = table["nodes"][0]
                self.assertEqual(node["parent_guid"], "parent-is-lineage-not-a-position")
                self.assertEqual(node["anchor_index"], 7)
                self.assertEqual(node["position_meters"], [0.3048, -0.6096, 0.1524])

    def test_culled_deleted_hidden_and_invalid_are_filtered_before_matching(self):
        text = spm_xml([
            node_xml("active", (1, 2, 3)),
            node_xml("culled", (1, 2, 3), culled=True),
            node_xml("deleted", (1, 2, 3), deleted=True),
            node_xml("hidden", (1, 2, 3), hidden=True),
            node_xml("invalid", (1, 2, 3), valid=False),
        ])
        with tempfile.TemporaryDirectory() as root:
            table = parse_spm_authored_placement(self._write(root, text))
            self.assertEqual(table["active_node_count"], 1)
            self.assertEqual(table["excluded_node_count"], 4)
            match = match_authored_node(
                table,
                [0.3048, 0.6096, 0.9144],
                132,
            )
            self.assertEqual(match["node_guid"], "active")

    def test_only_true_absence_allows_legacy_mode(self):
        with tempfile.TemporaryDirectory() as root:
            table = parse_spm_authored_placement(
                self._write(root, "<Model><Nodes /></Model>")
            )
            self.assertFalse(table["available"])
            self.assertEqual(table["status"], "legacy_authored_node_data_absent")

    def test_partial_authored_table_fails_closed(self):
        partial = """
        <Model><Nodes><Node>
          <GUID>node-a</GUID><GeneratorGUID>generator-a</GeneratorGUID>
          <Name>Leaf_node_(X: 1, Y: 2, Z: 3)</Name>
          <Extra><m_bValidPosition>true</m_bValidPosition></Extra>
        </Node></Nodes></Model>
        """
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaisesRegex(
                SpmAuthoredPlacementError, "m_bDeleted"
            ):
                parse_spm_authored_placement(self._write(root, partial))

    def test_duplicate_guid_fails_and_spatial_tie_is_deterministic(self):
        with tempfile.TemporaryDirectory() as root:
            duplicate = spm_xml([
                node_xml("same", (1, 2, 3)),
                node_xml("same", (4, 5, 6)),
            ])
            with self.assertRaisesRegex(
                SpmAuthoredPlacementError, "duplicated"
            ):
                parse_spm_authored_placement(self._write(root, duplicate))

        with tempfile.TemporaryDirectory() as root:
            tied = spm_xml([
                node_xml("left", (-1, 0, 0)),
                node_xml("right", (1, 0, 0)),
            ])
            table = parse_spm_authored_placement(self._write(root, tied))
            matched = match_authored_node(
                table,
                [0.0, 0.0, 0.0],
                132,
                tolerance_meters=1.0,
            )
            self.assertEqual(matched["node_guid"], "left")
            self.assertEqual(
                matched["match_evidence"]["equal_nearest_tie_policy"],
                "lexicographic_guid_then_first_unclaimed_v1",
            )

    def test_claimed_nearest_node_uses_bounded_deterministic_second_candidate(self):
        text = spm_xml([
            node_xml("near", (1, 0, 0)),
            node_xml("far", (1.0001, 0, 0)),
        ])
        with tempfile.TemporaryDirectory() as root:
            table = parse_spm_authored_placement(self._write(root, text))
            matched = match_authored_node(
                table,
                [0.3048, 0.0, 0.0],
                132,
                claimed_node_guids={"near"},
            )
            self.assertEqual(matched["node_guid"], "far")
            self.assertEqual(
                matched["match_evidence"]["claimed_nearer_node_count"], 1
            )

    def test_global_assignment_is_one_to_one_and_unmatched_is_degraded_evidence(self):
        text = spm_xml([
            node_xml("a", (0, 0, 0)),
            node_xml("b", (0.01, 0, 0)),
            node_xml("invalid", (0.02, 0, 0), valid=False),
        ])
        with tempfile.TemporaryDirectory() as root:
            table = parse_spm_authored_placement(self._write(root, text))
            result = assign_authored_nodes_to_components(
                table,
                [
                    {
                        "component_id": "component-b",
                        "target_mesh_id": 132,
                        "position_meters": [0.003048, 0.0, 0.0],
                    },
                    {
                        "component_id": "component-a",
                        "target_mesh_id": 132,
                        "position_meters": [0.0, 0.0, 0.0],
                    },
                    {
                        "component_id": "component-unmatched",
                        "target_mesh_id": 999,
                        "position_meters": [1.0, 0.0, 0.0],
                    },
                ],
            )
            self.assertEqual(result["assigned_count"], 2)
            self.assertEqual(result["unmatched_count"], 1)
            self.assertEqual(
                result["assignments"]["component-a"]["node_guid"], "a"
            )
            self.assertEqual(
                result["assignments"]["component-b"]["node_guid"], "b"
            )
            self.assertEqual(
                result["unmatched"][0]["match_diagnostic"],
                "no_state_mesh_candidate",
            )

            def emitted_mapping_keys(value):
                if isinstance(value, dict):
                    for key, child in value.items():
                        yield key
                        yield from emitted_mapping_keys(child)
                elif isinstance(value, list):
                    for child in value:
                        yield from emitted_mapping_keys(child)

            self.assertTrue(
                REASON_KEYS.isdisjoint(emitted_mapping_keys(result)),
                "placement diagnostics must not masquerade as repair reasons",
            )

    def test_global_assignment_preserves_maximum_bounded_cardinality(self):
        text = spm_xml([
            node_xml("only-for-constrained", (0, 0, 0)),
            node_xml("flexible-alternative", (0.02, 0, 0)),
        ])
        with tempfile.TemporaryDirectory() as root:
            table = parse_spm_authored_placement(self._write(root, text))
            result = assign_authored_nodes_to_components(
                table,
                [
                    {
                        "component_id": "flexible",
                        "target_mesh_id": 132,
                        "position_meters": [0.001, 0.0, 0.0],
                    },
                    {
                        "component_id": "constrained",
                        "target_mesh_id": 132,
                        "position_meters": [-0.009, 0.0, 0.0],
                    },
                ],
            )
            self.assertEqual(result["assigned_count"], 2)
            self.assertEqual(result["unmatched_count"], 0)
            self.assertEqual(
                result["assignments"]["constrained"]["node_guid"],
                "only-for-constrained",
            )
            self.assertEqual(
                result["assignments"]["flexible"]["node_guid"],
                "flexible-alternative",
            )


if __name__ == "__main__":
    unittest.main()
