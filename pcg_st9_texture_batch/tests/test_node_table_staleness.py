"""A stale saved <Node> table must be named, not read as a disconnection.

Production evidence this encodes: ``SK_bush_blackgum_02.spm`` keeps 4197 nodes
of which 3784 belong to 19 GUIDs that have no Generator, while its five
attached, non-hidden leaf Generators own zero nodes each.  Reading that as
"these Generators do not participate in export" blocked the whole blackgum
chain with the wrong cause.  ``SK_bush_blackgum_01.spm`` has a coherent table
(32 Generators, 32 node GUIDs, no orphans) and must keep its current verdict.
"""

import sys
import unittest
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parents[2]
for candidate in (REPO_DIR, REPO_DIR / "pcg_st9_texture_batch"):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

import pcg_texture_audit as audit  # noqa: E402
from pcg_cluster_assembly_contract import (  # noqa: E402
    STALE_NODE_TABLE_REASON,
    _classify_stale_node_table_block,
    _stale_node_table_evidence,
)


def generator(guid, *, mesh_id, material_id="4", name="Leaf"):
    return (
        f'<Generator Type="Leaf Mesh"><Name>{name}</Name>'
        f"<GUID>{guid}</GUID><Hidden>false</Hidden>"
        "<Properties>"
        "<Property><Name>Leaves:Material</Name>"
        f"<Value>{material_id}</Value></Property>"
        "<Property><Name>Leaves:Mesh</Name>"
        f"<Value>{mesh_id}</Value></Property>"
        "</Properties></Generator>"
    )


def node(generator_guid, index):
    return (
        f"<Node><GeneratorGUID>{generator_guid}</GeneratorGUID>"
        f"<ParentGUID></ParentGUID><Name>n{index}</Name>"
        f"<GUID>node{index}</GUID><Hidden>false</Hidden>"
        "<Extra></Extra></Node>"
    )


def document(generators, nodes):
    return (
        '<?xml version="1.0"?><SpeedTree>'
        f"<Generators>{''.join(generators)}</Generators>"
        f"<Nodes>{''.join(nodes)}</Nodes>"
        "</SpeedTree>"
    )


def bindings_for(text):
    counts, total = audit._export_node_counts_from_text(text)
    return audit._leaf_generator_bindings_from_text(
        text, export_node_counts=counts, total_nodes=total
    )


class NodeTableStateTests(unittest.TestCase):
    def test_coherent_table_reports_no_staleness(self):
        text = document(
            [generator("leaf", mesh_id="130")],
            [node("leaf", 0), node("leaf", 1)],
        )
        counts, total = audit._export_node_counts_from_text(text)
        state = audit._node_table_state(
            counts, total, audit._generator_guid_keys_from_text(text)
        )
        self.assertFalse(state["stale"])
        self.assertEqual(state["orphan_generator_guids"], [])
        self.assertEqual(state["orphan_node_count"], 0)
        self.assertEqual(state["generator_count"], 1)

    def test_nodes_owned_by_a_deleted_generator_mark_the_table_stale(self):
        text = document(
            [generator("leaf", mesh_id="130")],
            [node("removed", 0), node("removed", 1)],
        )
        counts, total = audit._export_node_counts_from_text(text)
        state = audit._node_table_state(
            counts, total, audit._generator_guid_keys_from_text(text)
        )
        self.assertTrue(state["stale"])
        self.assertEqual(state["orphan_generator_guids"], ["removed"])
        self.assertEqual(state["orphan_node_count"], 2)


class LeafBindingEvidenceTests(unittest.TestCase):
    def test_zero_nodes_in_a_coherent_table_still_means_no_export(self):
        text = document(
            [
                generator("leaf", mesh_id="130"),
                generator("other", mesh_id="131", name="Leaf 2"),
            ],
            [node("other", 0)],
        )
        rows = {row["generator_guid"]: row for row in bindings_for(text)}
        self.assertFalse(rows["leaf"]["export_participates"])
        self.assertEqual(rows["leaf"]["export_evidence"], "node_table")
        self.assertFalse(rows["leaf"]["node_table_stale"])
        self.assertTrue(rows["other"]["export_participates"])

    def test_zero_nodes_in_a_stale_table_is_unavailable_evidence(self):
        text = document(
            [generator("leaf", mesh_id="130")],
            [node("removed", 0), node("removed", 1)],
        )
        row = bindings_for(text)[0]
        # Still fails closed ...
        self.assertFalse(row["export_participates"])
        self.assertFalse(row["visible"])
        # ... but is no longer reported as a disconnected Generator.
        self.assertEqual(row["export_evidence"], "node_table_stale")
        self.assertTrue(row["node_table_stale"])
        self.assertTrue(row["graph_visible"])
        self.assertTrue(_stale_node_table_evidence(row))

    def test_generators_that_own_nodes_keep_positive_evidence(self):
        text = document(
            [generator("leaf", mesh_id="130")],
            [node("leaf", 0), node("removed", 1)],
        )
        row = bindings_for(text)[0]
        self.assertTrue(row["export_participates"])
        self.assertEqual(row["export_evidence"], "node_table")
        self.assertTrue(row["node_table_stale"])
        self.assertFalse(_stale_node_table_evidence(row))

    def test_hidden_generator_is_not_excused_by_a_stale_table(self):
        hidden = generator("leaf", mesh_id="130").replace(
            "<Hidden>false</Hidden>", "<Hidden>true</Hidden>"
        )
        text = document([hidden], [node("removed", 0)])
        row = bindings_for(text)[0]
        self.assertFalse(row["graph_visible"])
        self.assertFalse(row["export_participates"])
        self.assertFalse(_stale_node_table_evidence(row))

    def test_snapshot_publishes_the_node_table_state(self):
        text = document(
            [generator("leaf", mesh_id="130")],
            [node("removed", 0)],
        )
        counts, total = audit._export_node_counts_from_text(text)
        state = audit._node_table_state(
            counts, total, audit._generator_guid_keys_from_text(text)
        )
        self.assertEqual(state["node_table_generator_count"], 1)
        self.assertTrue(state["stale"])


class DeliveryClassificationTests(unittest.TestCase):
    @staticmethod
    def stale_row(mesh_id):
        return {
            "mesh_id": mesh_id,
            "graph_visible": True,
            "generated_node_count": 0,
            "export_participates": False,
            "export_evidence": "node_table_stale",
        }

    def test_stale_only_block_is_renamed_and_carries_a_remedy(self):
        evidence = {
            "errors": [
                "generator_export_evidence_stale_node_table",
                "normalized_and_live_target_mesh_sets_differ",
            ],
            "delivery_reason": "generator_connection_contract_incomplete",
        }
        _classify_stale_node_table_block(
            evidence,
            [self.stale_row("130"), self.stale_row("131")],
            [130, 131],
            [],
        )
        self.assertEqual(evidence["delivery_reason"], STALE_NODE_TABLE_REASON)
        self.assertTrue(evidence["delivery_remedy"])
        self.assertEqual(
            evidence["stale_node_table_target_mesh_ids"], [130, 131]
        )

    def test_an_independent_fault_keeps_the_original_reason(self):
        evidence = {
            "errors": [
                "generator_export_evidence_stale_node_table",
                "visible_generator_material_mismatch",
            ],
            "delivery_reason": "generator_connection_contract_incomplete",
        }
        _classify_stale_node_table_block(
            evidence, [self.stale_row("130")], [130], []
        )
        self.assertEqual(
            evidence["delivery_reason"],
            "generator_connection_contract_incomplete",
        )
        self.assertIsNone(evidence.get("delivery_remedy"))

    def test_unexplained_missing_mesh_id_keeps_the_original_reason(self):
        evidence = {
            "errors": [
                "generator_export_evidence_stale_node_table",
                "normalized_and_live_target_mesh_sets_differ",
            ],
            "delivery_reason": "generator_connection_contract_incomplete",
        }
        # 132 is missing from live evidence without a stale row to explain it.
        _classify_stale_node_table_block(
            evidence, [self.stale_row("130")], [130, 132], []
        )
        self.assertEqual(
            evidence["delivery_reason"],
            "generator_connection_contract_incomplete",
        )

    def test_a_real_disconnection_is_untouched(self):
        evidence = {
            "errors": ["generator_not_export_participating"],
            "delivery_reason": "generator_connection_contract_incomplete",
        }
        _classify_stale_node_table_block(evidence, [], [130], [])
        self.assertEqual(
            evidence["delivery_reason"],
            "generator_connection_contract_incomplete",
        )
        self.assertNotIn("stale_node_table_target_mesh_ids", evidence)


if __name__ == "__main__":
    unittest.main()
