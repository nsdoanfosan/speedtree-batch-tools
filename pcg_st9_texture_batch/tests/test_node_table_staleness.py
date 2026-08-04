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
    DELIVERY_MODE_CONNECTION_INCOMPLETE,
    STALE_NODE_TABLE_REASON,
    _classify_stale_node_table_block,
    _normalized_generator_delivery,
    _normalized_delivery_blocked_issue,
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


def link(source_guid, target_guid):
    return (
        "<Link>"
        f"<SourceGUID>{source_guid}</SourceGUID>"
        f"<TargetGUID>{target_guid}</TargetGUID>"
        "</Link>"
    )


def document(generators, nodes, links=()):
    return (
        '<?xml version="1.0"?><SpeedTree>'
        f"<Generators>{''.join(generators)}</Generators>"
        f"<Links>{''.join(links)}</Links>"
        f"<Nodes>{''.join(nodes)}</Nodes>"
        "</SpeedTree>"
    )


def bindings_for(text):
    counts, total = audit._export_node_counts_from_text(text)
    return audit._leaf_generator_bindings_from_text(
        text, export_node_counts=counts, total_nodes=total
    )


def delivery_binding():
    return {
        "state": "already_connected",
        "generator_index": 0,
        "generator_name": "Leaf",
        "generator_guid": "leaf",
        "generator_type": "Leaf Mesh",
        "slot_prefix": "Leaves",
        "target_material_id": 4,
        "target_mesh_id": 130,
    }


def delivery_payload():
    return {
        "generator_connection": {
            "requested": True,
            "complete": True,
            "generator_variant_policy": "ensure_all_material_cutouts",
            "bindings": [delivery_binding()],
        },
    }


def live_binding(*, stale=False):
    return {
        "generator_index": 0,
        "generator_name": "Leaf",
        "generator_guid": "leaf",
        "generator_type": "Leaf Mesh",
        "slot_prefix": "Leaves",
        "material_id": "4",
        "mesh_id": "130",
        "visible": not stale,
        "graph_visible": True,
        "generated_node_count": 0 if stale else 1,
        "export_participates": not stale,
        "export_evidence": "node_table_stale" if stale else "node_table",
        "node_table_stale": stale,
    }


def live_snapshot(spm, rows, *, stale=False):
    return {
        "contract": "speedtree_live_generator_delivery_snapshot_v1",
        "spm": str(Path(spm).resolve(strict=False)),
        "spm_text_sha256": "a" * 64,
        "total_node_count": 1,
        "leaf_generator_bindings": list(rows),
        "mesh_asset_ids": [130],
        "node_table": {
            "stale": stale,
            "orphan_generator_guids": ["removed"] if stale else [],
            "orphan_node_count": 1 if stale else 0,
        },
    }


def classify_delivery(audit):
    return _normalized_generator_delivery(
        audit,
        "SK_test.spm",
        delivery_payload(),
        {"material_id": 4},
        [{"target_mesh_id": 130}],
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

    def test_disconnected_orphan_does_not_taint_unconnected_generator(self):
        text = document(
            [generator("leaf", mesh_id="130")],
            [node("removed", 0), node("removed", 1)],
        )
        row = bindings_for(text)[0]
        self.assertFalse(row["export_participates"])
        self.assertFalse(row["visible"])
        self.assertEqual(row["export_evidence"], "node_table")
        self.assertFalse(row["node_table_stale"])
        self.assertTrue(row["node_table_document_stale"])
        self.assertEqual(
            row["causal_path_reason"],
            "generator_causal_path_unconnected",
        )
        self.assertTrue(row["graph_visible"])
        self.assertFalse(_stale_node_table_evidence(row))

    def test_orphan_ancestor_keeps_descendant_evidence_unavailable(self):
        text = document(
            [generator("leaf", mesh_id="130")],
            [node("removed", 0), node("removed", 1)],
            [link("removed", "leaf")],
        )
        row = bindings_for(text)[0]
        self.assertEqual(row["export_evidence"], "node_table_stale")
        self.assertTrue(row["node_table_stale"])
        self.assertEqual(row["orphan_ancestor_guids"], ["removed"])
        self.assertEqual(
            row["causal_path_reason"],
            "generator_causal_path_evidence_unavailable",
        )
        self.assertTrue(_stale_node_table_evidence(row))

    def test_generators_that_own_nodes_keep_positive_evidence(self):
        text = document(
            [generator("leaf", mesh_id="130")],
            [node("leaf", 0), node("removed", 1)],
        )
        row = bindings_for(text)[0]
        self.assertTrue(row["export_participates"])
        self.assertEqual(row["export_evidence"], "node_table")
        self.assertFalse(row["node_table_stale"])
        self.assertTrue(row["node_table_document_stale"])
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
        recovery = evidence["stale_node_table_recovery"]
        self.assertEqual(
            recovery["mode"],
            "owned_semantic_uia_modeler_save_watch",
        )
        self.assertTrue(recovery["modeler_auto_save"])
        self.assertFalse(recovery["direct_spm_xml_edit"])
        self.assertFalse(recovery["requires_user_save"])
        self.assertTrue(recovery["requires_node_table_stale"])
        self.assertTrue(recovery["requires_nonzero_orphan_owners"])
        self.assertTrue(recovery["requires_nonzero_orphan_nodes"])
        self.assertTrue(recovery["requires_complete_sealed_scope"])
        self.assertTrue(recovery["automatic_reaudit"])

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
        self.assertEqual(
            evidence["stale_node_table_recovery"]["mode"],
            "owned_semantic_uia_modeler_save_watch",
        )

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


class ExistingFailClosedPathTests(unittest.TestCase):
    def assert_generic_block(self, delivery, expected_error):
        self.assertEqual(
            delivery["delivery_mode"],
            DELIVERY_MODE_CONNECTION_INCOMPLETE,
        )
        self.assertEqual(delivery["delivery_decision"], "blocked")
        self.assertEqual(
            delivery["delivery_reason"],
            "generator_connection_contract_incomplete",
        )
        self.assertIn(expected_error, delivery["errors"])
        self.assertNotIn(
            "generator_export_evidence_stale_node_table",
            delivery["errors"],
        )
        self.assertIsNone(delivery["delivery_remedy"])
        self.assertNotIn("stale_node_table_target_mesh_ids", delivery)
        issue = _normalized_delivery_blocked_issue({
            "role": "leaf",
            "spm": delivery["spm"],
            "normalized_delivery_mode": delivery["delivery_mode"],
            "normalized_variants": {
                "delivery_blocked_targets": [delivery],
                "delivery_errors": delivery["errors"],
            },
        })
        self.assertEqual(
            issue["code"],
            "NORMALIZED_GENERATOR_DELIVERY_INCOMPLETE",
        )
        self.assertIn(expected_error, issue["errors"])
        self.assertNotIn("remedy", issue)
        self.assertNotIn("stale_node_table_targets", issue)

    def test_missing_live_slot_keeps_the_missing_cause_even_if_table_is_stale(
        self,
    ):
        class MissingSlotAudit:
            @staticmethod
            def live_generator_delivery_snapshot(spm):
                return live_snapshot(spm, [], stale=True)

        delivery = classify_delivery(MissingSlotAudit)

        self.assert_generic_block(delivery, "visible_generator_slot_missing")
        self.assertEqual(len(delivery["missing_live_bindings"]), 1)
        mismatch = next(
            row for row in delivery["binding_mismatches"]
            if "visible_generator_slot_missing" in row.get("errors", ())
        )
        self.assertIsNone(mismatch["current"])
        self.assertTrue(delivery["live_node_table"]["stale"])

    def test_ambiguous_live_slot_keeps_the_ambiguous_cause_under_stale_evidence(
        self,
    ):
        duplicate = live_binding(stale=True)

        class AmbiguousSlotAudit:
            @staticmethod
            def live_generator_delivery_snapshot(spm):
                return live_snapshot(
                    spm,
                    [duplicate, dict(duplicate)],
                    stale=True,
                )

        delivery = classify_delivery(AmbiguousSlotAudit)

        self.assert_generic_block(delivery, "visible_generator_slot_ambiguous")
        self.assertEqual(delivery["missing_live_bindings"], [])
        mismatch = next(
            row for row in delivery["binding_mismatches"]
            if "visible_generator_slot_ambiguous" in row.get("errors", ())
        )
        self.assertIsNone(mismatch["current"])
        self.assertTrue(delivery["live_node_table"]["stale"])

    def test_unavailable_audit_keeps_its_cause_and_fails_closed(self):
        delivery = classify_delivery(None)

        self.assert_generic_block(
            delivery,
            "production_spm_live_audit_unavailable",
        )
        self.assertEqual(
            delivery["errors"],
            ["production_spm_live_audit_unavailable"],
        )
        self.assertIsNone(delivery["live_node_table"])


class NormalizedDeliveryBlockedIssueTests(unittest.TestCase):
    @staticmethod
    def blocked_target(spm, *, stale):
        return {
            "spm": spm,
            "delivery_reason": (
                STALE_NODE_TABLE_REASON if stale else "some_other_reason"
            ),
        }

    def dependency(self, blocked_targets, errors=()):
        return {
            "role": "leaf",
            "spm": "SK_bush_blackgum_02.spm",
            "normalized_delivery_mode": "connection_incomplete",
            "normalized_variants": {
                "delivery_blocked_targets": blocked_targets,
                "delivery_errors": list(errors),
            },
        }

    def test_fully_stale_block_is_renamed_with_a_remedy(self):
        dependency = self.dependency([
            self.blocked_target("a.spm", stale=True),
            self.blocked_target("b.spm", stale=True),
        ])
        issue = _normalized_delivery_blocked_issue(dependency)
        self.assertEqual(issue["code"], "NORMALIZED_GENERATOR_NODE_TABLE_STALE")
        self.assertTrue(issue["remedy"])
        self.assertEqual(len(issue["blocked_targets"]), 2)
        self.assertNotIn("stale_node_table_targets", issue)

    def test_partially_stale_block_keeps_its_code_but_still_reports_the_remedy(
        self,
    ):
        # Production shape: one target is blocked by a stale node table, a
        # second is blocked by an independent, real fault. The code must not
        # be renamed to the stale-only reason, but the stale subset and its
        # fix must not silently disappear either.
        dependency = self.dependency([
            self.blocked_target("a.spm", stale=True),
            self.blocked_target("b.spm", stale=False),
        ])
        issue = _normalized_delivery_blocked_issue(dependency)
        self.assertEqual(issue["code"], "NORMALIZED_GENERATOR_DELIVERY_INCOMPLETE")
        self.assertNotIn("remedy", issue)
        self.assertEqual(len(issue["stale_node_table_targets"]), 1)
        self.assertEqual(
            issue["stale_node_table_targets"][0]["spm"], "a.spm"
        )
        self.assertTrue(issue["stale_node_table_remedy"])

    def test_no_stale_target_reports_neither_stale_field(self):
        dependency = self.dependency([
            self.blocked_target("a.spm", stale=False),
        ])
        issue = _normalized_delivery_blocked_issue(dependency)
        self.assertEqual(issue["code"], "NORMALIZED_GENERATOR_DELIVERY_INCOMPLETE")
        self.assertNotIn("remedy", issue)
        self.assertNotIn("stale_node_table_targets", issue)
        self.assertNotIn("stale_node_table_remedy", issue)

    def test_one_target_with_mixed_faults_keeps_stale_subset_and_remedy(self):
        target = self.blocked_target("a.spm", stale=False)
        target["stale_node_table_target_mesh_ids"] = [130]
        target["errors"] = [
            "generator_export_evidence_stale_node_table",
            "visible_generator_slot_missing",
        ]
        issue = _normalized_delivery_blocked_issue(
            self.dependency([target], errors=target["errors"])
        )
        self.assertEqual(
            issue["code"],
            "NORMALIZED_GENERATOR_DELIVERY_INCOMPLETE",
        )
        self.assertNotIn("remedy", issue)
        self.assertEqual(issue["stale_node_table_targets"], [target])
        self.assertTrue(issue["stale_node_table_remedy"])


if __name__ == "__main__":
    unittest.main()
