import gzip
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


TOOL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOL_DIR))

from pcg_cluster_assembly_contract import (  # noqa: E402
    ClusterAssemblyInternalContractError,
    DELIVERY_MODE_CONNECTION_INCOMPLETE,
    DELIVERY_MODE_RENDER_CONNECTED,
    _delivery_binding_slot_identity,
    _finalize_normalized_generator_delivery,
    _normalized_generator_delivery,
)
import pcg_texture_audit as audit_module  # noqa: E402


GENERATOR_GUID = "blackgum-generator-guid"
SLOT_PREFIXES = (
    "Leaves:Type:0",
    "Leaves:Type:1",
    "Material:Frond:0",
    "Material:Frond:1",
)
SOURCE_MESH_IDS = (10, 11, 12, 13)
TARGET_MESH_IDS = (130, 131, 132, 133)


def _material_xml(material_id, name, mesh_ids):
    primary, *supplemental = mesh_ids
    return (
        f'<Material_v8 ID="{material_id}" Name="{name}">'
        f"<CutoutMeshID>{primary}</CutoutMeshID>"
        f'<SupplementalCutoutMeshIDs Count="{len(supplemental)}">'
        + "".join(
            f'<CutoutMesh ID="{mesh_id}"/>'
            for mesh_id in supplemental
        )
        + "</SupplementalCutoutMeshIDs>"
        "</Material_v8>"
    )


def write_blackgum_delivery_spm(
    path,
    connected,
    *,
    node_guid=None,
):
    material_id = 4 if connected else 1
    mesh_ids = TARGET_MESH_IDS if connected else SOURCE_MESH_IDS
    properties = []
    for slot_prefix, mesh_id in zip(SLOT_PREFIXES, mesh_ids):
        properties.extend((
            "<Property>"
            f"<Name>{slot_prefix}:Material</Name>"
            f"<Value>{material_id}</Value>"
            "</Property>",
            "<Property>"
            f"<Name>{slot_prefix}:Mesh</Name>"
            f"<Value>{mesh_id}</Value>"
            "</Property>",
        ))
    mesh_xml = "".join(
        f'<Mesh ID="{mesh_id}" Name="mesh-{mesh_id}"/>'
        for mesh_id in SOURCE_MESH_IDS + TARGET_MESH_IDS
    )
    node_guid = GENERATOR_GUID if node_guid is None else node_guid
    payload = (
        "<SpeedTree><Assets>"
        + _material_xml(
            1,
            "M_source_blackgum_01",
            SOURCE_MESH_IDS,
        )
        + _material_xml(
            4,
            "M_cluster_blackgum_01",
            TARGET_MESH_IDS,
        )
        + mesh_xml
        + "</Assets><Generators>"
        f'<Generator Type="Leaf Mesh"><Name>generator 20</Name>'
        f"<GUID>{GENERATOR_GUID}</GUID><Hidden>false</Hidden>"
        "<Properties>"
        + "".join(properties)
        + "</Properties></Generator></Generators><Nodes>"
        "<Node>"
        f"<GeneratorGUID>{node_guid}</GeneratorGUID>"
        "<ParentGUID></ParentGUID>"
        "<Name>blackgum leaf node</Name>"
        "<GUID>blackgum-node-guid</GUID>"
        "<Hidden>false</Hidden>"
        "<Extra>"
        "<m_bDeleted>false</m_bDeleted>"
        "<m_bCulled>false</m_bCulled>"
        "</Extra>"
        "</Node></Nodes></SpeedTree>"
    ).encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(gzip.compress(payload, mtime=0))


def write_unused_base_delivery_spm(path):
    def generator(guid, name, generator_type, properties=""):
        return (
            f'<Generator Type="{generator_type}"><Name>{name}</Name>'
            f"<GUID>{guid}</GUID><Hidden>false</Hidden>"
            f"<Properties>{properties}</Properties></Generator>"
        )

    def leaf_properties(slot_mesh_pairs):
        return "".join(
            "<Property>"
            f"<Name>{slot}:Material</Name><Value>4</Value>"
            "</Property>"
            "<Property>"
            f"<Name>{slot}:Mesh</Name><Value>{mesh_id}</Value>"
            "</Property>"
            for slot, mesh_id in slot_mesh_pairs
        )

    def link(source, target):
        return (
            "<Link>"
            f"<SourceGUID>{source}</SourceGUID>"
            f"<TargetGUID>{target}</TargetGUID>"
            "<Hidden>false</Hidden>"
            "</Link>"
        )

    def node(generator_guid, ordinal):
        return (
            "<Node>"
            f"<GeneratorGUID>{generator_guid}</GeneratorGUID>"
            "<ParentGUID></ParentGUID>"
            f"<Name>node-{ordinal}</Name><GUID>node-{ordinal}</GUID>"
            "<Hidden>false</Hidden>"
            "<Extra><m_bDeleted>false</m_bDeleted>"
            "<m_bCulled>false</m_bCulled></Extra>"
            "</Node>"
        )

    leaf_slots = (
        ("Leaves:Type:0", 130),
        ("Leaves:Type:1", 132),
    )
    generators = "".join((
        generator("tree-guid", "Tree", "Tree"),
        generator("unused-base-guid", "Leafbig", "Base"),
        generator("unused-branch-guid", "Branch 13", "Branch"),
        generator(
            "unused-leaf-7-guid",
            "Leaf 7",
            "Leaf Mesh",
            leaf_properties(leaf_slots),
        ),
        generator(
            "unused-main-leaf-guid",
            "main_leaf_02 3",
            "Leaf Mesh",
            leaf_properties(leaf_slots),
        ),
        generator("active-base-guid", "LeafSmall", "Base"),
        generator("active-branch-guid", "Branch 29", "Branch"),
        generator(
            "active-leaf-guid",
            "Leaf 8",
            "Leaf Mesh",
            leaf_properties(leaf_slots),
        ),
    ))
    links = "".join((
        link("tree-guid", "unused-base-guid"),
        link("unused-base-guid", "unused-branch-guid"),
        link("unused-branch-guid", "unused-leaf-7-guid"),
        link("unused-branch-guid", "unused-main-leaf-guid"),
        link("tree-guid", "active-base-guid"),
        link("active-base-guid", "active-branch-guid"),
        link("active-branch-guid", "active-leaf-guid"),
    ))
    nodes = "".join((
        node("tree-guid", 1),
        node("active-base-guid", 2),
        node("active-branch-guid", 3),
        node("active-leaf-guid", 4),
    ))
    mesh_xml = "".join(
        f'<Mesh ID="{mesh_id}" Name="mesh-{mesh_id}"/>'
        for mesh_id in TARGET_MESH_IDS
    )
    payload = (
        "<SpeedTree><Assets>"
        + _material_xml(4, "M_cluster_blackgum_01", TARGET_MESH_IDS)
        + mesh_xml
        + "</Assets><Generators>"
        + generators
        + "</Generators><Links>"
        + links
        + "</Links><Nodes>"
        + nodes
        + "</Nodes></SpeedTree>"
    ).encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(gzip.compress(payload, mtime=0))


def declared_bindings():
    return [
        {
            "state": "already_connected",
            # Deliberately differs from the live XML enumeration.  A GUID is
            # present, so array position must never become identity.
            "generator_index": 20,
            "generator_name": "generator 20",
            "generator_guid": GENERATOR_GUID.upper(),
            "generator_type": "Leaf Mesh",
            "slot_prefix": slot_prefix.upper(),
            "target_material_id": 4,
            "target_mesh_id": mesh_id,
        }
        for slot_prefix, mesh_id in zip(SLOT_PREFIXES, TARGET_MESH_IDS)
    ]


def delivery_payload():
    return {
        "generator_connection": {
            "requested": True,
            "complete": True,
            "generator_variant_policy": "ensure_all_material_cutouts",
            "bindings": declared_bindings(),
        },
    }


def delivery_variants():
    return [
        {"target_mesh_id": mesh_id}
        for mesh_id in TARGET_MESH_IDS
    ]


def snapshot_binding(
    slot_prefix,
    mesh_id,
    *,
    material_id=4,
    graph_visible=True,
    generated_node_count=1,
):
    export_participates = bool(
        graph_visible and generated_node_count > 0
    )
    return {
        "generator_index": 0,
        "generator_name": "generator 20",
        "generator_guid": GENERATOR_GUID,
        "generator_type": "Leaf Mesh",
        "slot_prefix": slot_prefix,
        "material_id": str(material_id),
        "mesh_id": str(mesh_id),
        "visible": export_participates,
        "graph_visible": graph_visible,
        "generated_node_count": generated_node_count,
        "export_participates": export_participates,
    }


def fake_snapshot(spm, bindings, mesh_ids, total_node_count=1):
    return {
        "contract": "speedtree_live_generator_delivery_snapshot_v1",
        "spm": str(Path(spm).resolve(strict=False)),
        "spm_text_sha256": "a" * 64,
        "total_node_count": total_node_count,
        "leaf_generator_bindings": bindings,
        "mesh_asset_ids": list(mesh_ids),
    }


class NormalizedGeneratorDeliverySnapshotTests(unittest.TestCase):
    def setUp(self):
        self.original_analysis_cache = audit_module._SPM_ANALYSIS_CACHE
        self.original_persistent_cache = (
            audit_module._PERSISTENT_SPM_ANALYSIS
        )
        self.original_persistent_dirty = (
            audit_module._PERSISTENT_SPM_ANALYSIS_DIRTY
        )
        audit_module._SPM_ANALYSIS_CACHE = {}
        audit_module._PERSISTENT_SPM_ANALYSIS = {}
        audit_module._PERSISTENT_SPM_ANALYSIS_DIRTY = False

    def tearDown(self):
        audit_module._SPM_ANALYSIS_CACHE = self.original_analysis_cache
        audit_module._PERSISTENT_SPM_ANALYSIS = (
            self.original_persistent_cache
        )
        audit_module._PERSISTENT_SPM_ANALYSIS_DIRTY = (
            self.original_persistent_dirty
        )

    def test_fresh_snapshot_closes_cached_pre_connection_reproduction(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "SK_bush_blackgum_02.spm"
            report_cache = {
                "file_cache_keys": {},
                "root_spms": {},
                "path_exists": {},
            }
            token = audit_module._REPORT_SCAN_CACHE.set(report_cache)
            try:
                write_blackgum_delivery_spm(target, connected=False)
                pre_connection = audit_module._spm_analysis(target)
                self.assertEqual(
                    {
                        row["material_id"]
                        for row in pre_connection[
                            "leaf_generator_bindings"
                        ]
                    },
                    {"1"},
                )

                # Reproduce the old contradiction: the Atlas writer has put
                # Material 4 / Mesh 130-133 in the file, but report-local cache
                # identity still resolves the pre-connection parsed object.
                write_blackgum_delivery_spm(target, connected=True)
                stale = audit_module._spm_analysis(target)
                self.assertIs(stale, pre_connection)
                self.assertEqual(
                    [
                        row for row in stale["leaf_generator_bindings"]
                        if row.get("material_id") == "4"
                    ],
                    [],
                )

                with mock.patch.object(
                    audit_module,
                    "read_pipeline_spm_text",
                    wraps=audit_module.read_pipeline_spm_text,
                ) as read_spm:
                    delivery = _normalized_generator_delivery(
                        audit_module,
                        target,
                        delivery_payload(),
                        {"material_id": 4},
                        delivery_variants(),
                    )
                self.assertEqual(read_spm.call_count, 1)
            finally:
                audit_module._REPORT_SCAN_CACHE.reset(token)

        self.assertEqual(
            delivery["delivery_mode"],
            DELIVERY_MODE_RENDER_CONNECTED,
        )
        self.assertEqual(delivery["delivery_decision"], "normalize_part")
        self.assertTrue(
            delivery["generator_connection_declared_complete"]
        )
        self.assertTrue(delivery["generator_connection_complete"])
        self.assertTrue(delivery["live_generator_delivery_complete"])
        self.assertEqual(
            delivery["live_export_participating_target_mesh_ids"],
            list(TARGET_MESH_IDS),
        )
        self.assertEqual(delivery["errors"], [])
        self.assertEqual(delivery["missing_live_bindings"], [])
        self.assertEqual(len(delivery["live_declared_slot_bindings"]), 4)
        self.assertTrue(all(
            row["generated_node_count"] == 1
            for row in delivery["live_declared_slot_bindings"]
        ))
        self.assertEqual(
            delivery["live_snapshot_contract"],
            "speedtree_live_generator_delivery_snapshot_v1",
        )
        self.assertEqual(delivery["live_snapshot_total_node_count"], 1)
        self.assertEqual(len(delivery["live_snapshot_sha256"]), 64)

    def test_guid_case_slot_case_and_manifest_index_share_one_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "SK_bush_blackgum_02.spm"
            write_blackgum_delivery_spm(
                target,
                connected=True,
                node_guid=GENERATOR_GUID.upper(),
            )

            snapshot = audit_module.live_generator_delivery_snapshot(target)
            delivery = _normalized_generator_delivery(
                audit_module,
                target,
                delivery_payload(),
                {"material_id": 4},
                delivery_variants(),
            )

        self.assertEqual(snapshot["total_node_count"], 1)
        self.assertEqual(
            {row["generated_node_count"] for row in snapshot[
                "leaf_generator_bindings"
            ]},
            {1},
        )
        self.assertTrue(all(
            row["export_participates"]
            for row in snapshot["leaf_generator_bindings"]
        ))
        self.assertEqual(
            delivery["delivery_mode"],
            DELIVERY_MODE_RENDER_CONNECTED,
        )
        self.assertEqual(delivery["errors"], [])
        self.assertEqual(
            {row["generator_index"] for row in declared_bindings()},
            {20},
        )
        self.assertEqual(
            {row["generator_index"] for row in snapshot[
                "leaf_generator_bindings"
            ]},
            {0},
        )

    def test_live_snapshot_marks_only_the_unused_base_path_inactive(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "SK_weed_blackgum_01.spm"
            write_unused_base_delivery_spm(target)
            snapshot = audit_module.live_generator_delivery_snapshot(target)

        self.assertFalse(snapshot["node_table"]["stale"])
        rows_by_name = {}
        for row in snapshot["leaf_generator_bindings"]:
            rows_by_name.setdefault(row["generator_name"], []).append(row)

        for name in ("Leaf 7", "main_leaf_02 3"):
            self.assertEqual(len(rows_by_name[name]), 2)
            self.assertTrue(all(
                row["causal_path_active"] is False
                for row in rows_by_name[name]
            ))
            self.assertTrue(all(
                row["causal_path_reason"]
                == "generator_causal_path_inactive_unused_base"
                for row in rows_by_name[name]
            ))
            self.assertEqual(
                {row["inactive_base"]["generator_name"]
                 for row in rows_by_name[name]},
                {"Leafbig"},
            )
            self.assertTrue(all(
                row["generated_node_count"] == 0
                and row["graph_visible"] is True
                and row["export_participates"] is False
                for row in rows_by_name[name]
            ))

        self.assertEqual(len(rows_by_name["Leaf 8"]), 2)
        self.assertTrue(all(
            row["causal_path_active"] is True
            and row["causal_path_reason"]
            == "generator_causal_path_active"
            and row["export_participates"] is True
            for row in rows_by_name["Leaf 8"]
        ))

    def test_unused_base_bindings_are_planned_inactive_not_failed(self):
        fixture_path = (
            Path(__file__).parent
            / "fixtures"
            / "issue18_unused_base_causal_path.json"
        )
        fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
        inactive_base = {
            "generator_index": 1,
            "generator_name": fixture["inactive_base"][
                "generator_name"
            ],
            "generator_type": "Base",
            "generated_node_count": 0,
        }

        specs = (
            (
                "unused-leaf-7-guid",
                "Leaf 7",
                (("Leaves:Type:0", 130), ("Leaves:Type:1", 132)),
                False,
            ),
            (
                "unused-main-leaf-guid",
                "main_leaf_02 3",
                (("Leaves:Type:0", 130), ("Leaves:Type:1", 132)),
                False,
            ),
            (
                "active-leaf-8-guid",
                "Leaf 8",
                (("Leaves:Type:0", 130), ("Leaves:Type:1", 132)),
                True,
            ),
            (
                "active-frond-6-guid",
                "Frond 6",
                (("Material:Frond:0", 133), ("Material:Frond:1", 131)),
                True,
            ),
        )
        declared = []
        live = []
        for generator_index, (guid, name, slots, active) in enumerate(specs):
            generator_type = "Frond" if name == "Frond 6" else "Leaf Mesh"
            for slot, mesh_id in slots:
                declared.append({
                    "state": "already_connected",
                    "generator_index": generator_index,
                    "generator_name": name,
                    "generator_guid": guid,
                    "generator_type": generator_type,
                    "slot_prefix": slot,
                    "target_material_id": 4,
                    "target_mesh_id": mesh_id,
                })
                live.append({
                    "generator_index": generator_index,
                    "generator_name": name,
                    "generator_guid": guid,
                    "generator_type": generator_type,
                    "slot_prefix": slot,
                    "material_id": "4",
                    "mesh_id": str(mesh_id),
                    "visible": active,
                    "graph_visible": True,
                    "generated_node_count": 1 if active else 0,
                    "export_participates": active,
                    "export_evidence": "node_table",
                    "node_table_stale": False,
                    "causal_path_active": active,
                    "causal_path_reason": (
                        "generator_causal_path_active"
                        if active
                        else "generator_causal_path_inactive_unused_base"
                    ),
                    "causal_path": [],
                    "inactive_base": None if active else inactive_base,
                })

        class UnusedBaseAudit:
            @staticmethod
            def live_generator_delivery_snapshot(spm):
                return fake_snapshot(
                    spm,
                    live,
                    TARGET_MESH_IDS,
                    total_node_count=4,
                )

        delivery = _normalized_generator_delivery(
            UnusedBaseAudit,
            "SK_weed_blackgum_01.spm",
            {
                "generator_connection": {
                    "requested": True,
                    "complete": True,
                    "generator_variant_policy": (
                        "ensure_all_material_cutouts"
                    ),
                    "bindings": declared,
                },
            },
            {"material_id": 4},
            delivery_variants(),
        )

        self.assertEqual(
            delivery["delivery_mode"],
            DELIVERY_MODE_RENDER_CONNECTED,
        )
        self.assertEqual(delivery["delivery_decision"], "normalize_part")
        self.assertEqual(delivery["errors"], [])
        self.assertEqual(
            delivery["planned_inactive_binding_count"],
            fixture["expected"]["planned_inactive_binding_count"],
        )
        self.assertEqual(delivery["active_required_binding_count"], 4)
        self.assertEqual(len(delivery["inactive_causal_bindings"]), 4)
        self.assertNotIn(
            fixture["expected"]["blocking_error_absent"],
            delivery["errors"],
        )
        inactive_outcomes = [
            row for row in delivery["binding_outcomes"]
            if row["status"] == "planned_inactive"
        ]
        self.assertEqual(len(inactive_outcomes), 4)
        self.assertEqual(
            {row["reason"] for row in inactive_outcomes},
            {fixture["expected"]["reason"]},
        )

        # Equivalent control: deleting the unused Base in an isolated model
        # and refreshing its declaration removes the same four slots.  The
        # current-required mesh set and completion result must be identical;
        # keeping the authoring Base must not change Push readiness.
        active_declared = [
            row for row in declared
            if row["generator_name"] not in {"Leaf 7", "main_leaf_02 3"}
        ]
        active_live = [
            row for row in live
            if row["generator_name"] not in {"Leaf 7", "main_leaf_02 3"}
        ]

        class RemovedUnusedBaseAudit:
            @staticmethod
            def live_generator_delivery_snapshot(spm):
                return fake_snapshot(
                    spm,
                    active_live,
                    TARGET_MESH_IDS,
                    total_node_count=4,
                )

        removed_delivery = _normalized_generator_delivery(
            RemovedUnusedBaseAudit,
            "SK_weed_blackgum_01.spm",
            {
                "generator_connection": {
                    "requested": True,
                    "complete": True,
                    "generator_variant_policy": (
                        "ensure_all_material_cutouts"
                    ),
                    "bindings": active_declared,
                },
            },
            {"material_id": 4},
            delivery_variants(),
        )
        self.assertEqual(removed_delivery["errors"], [])
        self.assertEqual(
            removed_delivery["delivery_mode"],
            delivery["delivery_mode"],
        )
        self.assertEqual(
            removed_delivery["current_required_target_mesh_ids"],
            delivery["current_required_target_mesh_ids"],
        )
        self.assertEqual(
            removed_delivery["live_export_participating_target_mesh_ids"],
            delivery["live_export_participating_target_mesh_ids"],
        )

    def test_named_fallback_never_uses_generator_array_position(self):
        base = {
            "generator_guid": "",
            "generator_type": "Leaf Mesh",
            "generator_name": "authored generator",
            "slot_prefix": "Leaves:Type:0",
        }
        first = _delivery_binding_slot_identity({
            **base,
            "generator_index": 1,
        })
        reordered = _delivery_binding_slot_identity({
            **base,
            "generator_index": 99,
        })

        self.assertEqual(first, reordered)
        self.assertEqual(first[0], "named")

    def test_present_non_exporting_slots_remain_a_strict_asset_defect(self):
        rows = [
            snapshot_binding(
                SLOT_PREFIXES[0],
                TARGET_MESH_IDS[0],
                graph_visible=False,
                generated_node_count=1,
            ),
            snapshot_binding(
                SLOT_PREFIXES[1],
                TARGET_MESH_IDS[1],
                graph_visible=False,
                generated_node_count=1,
            ),
            snapshot_binding(
                SLOT_PREFIXES[2],
                TARGET_MESH_IDS[2],
                graph_visible=True,
                generated_node_count=0,
            ),
            snapshot_binding(
                SLOT_PREFIXES[3],
                TARGET_MESH_IDS[3],
                graph_visible=True,
                generated_node_count=0,
            ),
        ]
        # This is the pre-fix evidence loss: visible_only removed every row,
        # so exact existing slots were reported as current=null/missing.
        legacy_visible_only = [
            row for row in rows if row["export_participates"]
        ]
        self.assertEqual(legacy_visible_only, [])

        class NonExportingAudit:
            @staticmethod
            def live_generator_delivery_snapshot(spm):
                return fake_snapshot(
                    spm,
                    rows,
                    TARGET_MESH_IDS,
                    total_node_count=2,
                )

        delivery = _normalized_generator_delivery(
            NonExportingAudit,
            "SK_bush_blackgum_02.spm",
            delivery_payload(),
            {"material_id": 4},
            delivery_variants(),
        )

        self.assertEqual(
            delivery["delivery_mode"],
            DELIVERY_MODE_CONNECTION_INCOMPLETE,
        )
        self.assertTrue(
            delivery["generator_connection_declared_complete"]
        )
        self.assertFalse(delivery["generator_connection_complete"])
        self.assertFalse(delivery["live_generator_delivery_complete"])
        self.assertEqual(
            delivery["live_export_participating_target_mesh_ids"],
            [],
        )
        self.assertIn(
            "normalized_and_live_target_mesh_sets_differ",
            delivery["errors"],
        )
        self.assertIn(
            "generator_not_export_participating",
            delivery["errors"],
        )
        self.assertNotIn(
            "visible_generator_slot_missing",
            delivery["errors"],
        )
        self.assertEqual(delivery["missing_live_bindings"], [])
        self.assertEqual(
            len(delivery["live_non_export_participating_bindings"]),
            4,
        )
        causes = {
            (
                row["graph_visible"],
                row["generated_node_count"],
            )
            for row in delivery[
                "live_non_export_participating_bindings"
            ]
        }
        self.assertEqual(causes, {(False, 1), (True, 0)})
        self.assertTrue(all(
            row["current"] is not None
            for row in delivery["binding_mismatches"]
            if "generator_not_export_participating" in row["errors"]
        ))

    def test_declared_complete_value_drift_is_a_precise_live_defect(self):
        rows = [
            snapshot_binding(
                slot_prefix,
                source_mesh_id,
                material_id=1,
            )
            for slot_prefix, source_mesh_id in zip(
                SLOT_PREFIXES,
                SOURCE_MESH_IDS,
            )
        ]

        class ContradictoryAudit:
            @staticmethod
            def live_generator_delivery_snapshot(spm):
                return fake_snapshot(
                    spm,
                    rows,
                    SOURCE_MESH_IDS + TARGET_MESH_IDS,
                    total_node_count=1,
                )

        delivery = _normalized_generator_delivery(
            ContradictoryAudit,
            "SK_bush_blackgum_02.spm",
            delivery_payload(),
            {"material_id": 4},
            delivery_variants(),
        )

        self.assertTrue(
            delivery["generator_connection_declared_complete"]
        )
        self.assertFalse(delivery["generator_connection_complete"])
        self.assertEqual(
            delivery["live_export_participating_target_mesh_ids"],
            [],
        )
        self.assertIn(
            "visible_generator_material_mismatch",
            delivery["errors"],
        )
        self.assertIn(
            "visible_generator_mesh_mismatch",
            delivery["errors"],
        )
        self.assertNotIn(
            "visible_generator_slot_missing",
            delivery["errors"],
        )
        self.assertTrue(all(
            row["current"] is not None
            for row in delivery["binding_mismatches"]
            if row.get("slot_identity")
        ))

    def test_completed_delivery_with_empty_live_set_is_internal(self):
        with self.assertRaisesRegex(
            ClusterAssemblyInternalContractError,
            "INTERNAL_NORMALIZED_GENERATOR_DELIVERY_CONFLICT",
        ):
            _finalize_normalized_generator_delivery({
                "generator_connection_complete": True,
                "live_generator_delivery_complete": True,
                "generator_bindings": declared_bindings(),
                "live_export_participating_target_mesh_ids": [],
                "delivery_mode": DELIVERY_MODE_RENDER_CONNECTED,
                "delivery_decision": "normalize_part",
                "errors": [],
            })

    def test_schema4_warm_cache_survives_while_live_stale_table_is_uncached(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "SK_bush_blackgum_02.spm"
            write_blackgum_delivery_spm(
                target,
                connected=True,
                node_guid="removed-generator-guid",
            )
            cache_key = audit_module._file_cache_key(target)
            path_key, size, mtime_ns = cache_key
            cached_binding = {
                "generator_guid": "cached-schema-4-generator",
                "material_id": "1",
                "mesh_id": "10",
            }
            audit_module._PERSISTENT_SPM_ANALYSIS[path_key] = {
                "size": size,
                "mtime_ns": mtime_ns,
                "material_rows": [],
                "material_names": ["cached-schema-4-material"],
                "referenced_material_ids": ["1"],
                "visible_material_ids": [],
                "leaf_generator_bindings": [cached_binding],
                "mesh_asset_ids": ["10"],
                "leaf_binding_schema": 4,
            }

            with mock.patch.object(
                audit_module,
                "read_maybe_gzip_text",
                side_effect=AssertionError("schema-4 warm cache was invalidated"),
            ):
                warm = audit_module._spm_analysis(target)

            self.assertEqual(
                warm["leaf_generator_bindings"],
                [cached_binding],
            )
            self.assertFalse(audit_module._PERSISTENT_SPM_ANALYSIS_DIRTY)

            with mock.patch.object(
                audit_module,
                "_spm_analysis",
                side_effect=AssertionError("live snapshot consulted audit cache"),
            ), mock.patch.object(
                audit_module,
                "read_pipeline_spm_text",
                wraps=audit_module.read_pipeline_spm_text,
            ) as read_spm:
                snapshot = audit_module.live_generator_delivery_snapshot(
                    target
                )

            read_spm.assert_called_once_with(target)
            self.assertIs(audit_module._SPM_ANALYSIS_CACHE[cache_key], warm)
            self.assertTrue(snapshot["node_table"]["stale"])
            self.assertEqual(
                snapshot["node_table"]["orphan_generator_guids"],
                ["removed-generator-guid"],
            )
            self.assertEqual(snapshot["node_table"]["orphan_node_count"], 1)
            live_binding = snapshot["leaf_generator_bindings"][0]
            self.assertEqual(live_binding["generator_guid"], GENERATOR_GUID)
            self.assertEqual(live_binding["generated_node_count"], 0)
            self.assertEqual(
                live_binding["export_evidence"],
                "node_table_stale",
            )

if __name__ == "__main__":
    unittest.main()
