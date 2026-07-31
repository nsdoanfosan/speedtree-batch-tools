import gzip
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


if __name__ == "__main__":
    unittest.main()
