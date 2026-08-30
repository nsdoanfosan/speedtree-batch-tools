from __future__ import annotations

import json
import sys
import types
import unittest
from copy import deepcopy
from pathlib import Path


SK_BATCH = Path(__file__).resolve().parents[1]
if str(SK_BATCH) not in sys.path:
    sys.path.insert(0, str(SK_BATCH))

from nanite_assembly_materials import (  # noqa: E402
    NaniteAssemblyMaterialError,
    audit_unreal_skeletal_mesh_material_sections,
    parse_nanite_assembly_part_remaps,
    plan_nanite_assembly_material_normalization,
    rewrite_nanite_assembly_part_remaps,
)


def slot(name, material):
    return {"slot_name": name, "material": material}


class SkeletalMeshMaterialSectionAuditTests(unittest.TestCase):
    MESH = "/Game/Trees/SK_Tree"

    @classmethod
    def payload(cls):
        return {
            "schema_version": 1,
            "audit": "skeletal_mesh_lod0_material_sections",
            "ok": True,
            "skeletal_mesh": cls.MESH,
            "target_compiling_before": True,
            "target_compiling_after": False,
            "lod_index": 0,
            "lod_count": 1,
            "material_count": 2,
            "section_count": 2,
            "materials": [
                {
                    "index": 0,
                    "slot_name": "M_Bark",
                    "imported_slot_name": "M_Bark",
                    "material": "/Game/MI/MI_Bark.MI_Bark",
                },
                {
                    "index": 1,
                    "slot_name": "M_Leaf",
                    "imported_slot_name": "M_Leaf",
                    "material": "/Game/MI/MI_Leaf.MI_Leaf",
                },
            ],
            "sections": [
                {
                    "section": 0,
                    "material_index": 0,
                    "base_index": 0,
                    "num_triangles": 12,
                    "base_vertex_index": 0,
                    "num_vertices": 24,
                },
                {
                    "section": 1,
                    "material_index": 1,
                    "base_index": 36,
                    "num_triangles": 8,
                    "base_vertex_index": 24,
                    "num_vertices": 16,
                },
            ],
        }

    @staticmethod
    def fake_unreal(raw, old_audit=None):
        return types.SimpleNamespace(
            CodexMaterialToolsLibrary=types.SimpleNamespace(
                audit_skeletal_mesh_lod0_material_sections=lambda _path: raw,
                audit_skeletal_mesh_lod0_streams=old_audit,
            )
        )

    def test_requires_lightweight_helper_and_never_calls_full_stream_audit(self):
        calls = []

        def lightweight(path):
            calls.append(path)
            return True, json.dumps(self.payload()), []

        def full_stream(_path):
            self.fail("material validation must not call the full-stream audit")

        fake_unreal = types.SimpleNamespace(
            CodexMaterialToolsLibrary=types.SimpleNamespace(
                audit_skeletal_mesh_lod0_material_sections=lightweight,
                audit_skeletal_mesh_lod0_streams=full_stream,
            )
        )

        result = audit_unreal_skeletal_mesh_material_sections(
            fake_unreal,
            self.MESH,
            slot_count=2,
        )

        self.assertEqual(calls, [self.MESH])
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["slot_count"], 2)
        self.assertEqual(
            result["sections"],
            [
                {"section": 0, "material_index": 0, "num_triangles": 12},
                {"section": 1, "material_index": 1, "num_triangles": 8},
            ],
        )

    def test_ue58_python_binding_without_native_bool_uses_validated_json(self):
        result = audit_unreal_skeletal_mesh_material_sections(
            self.fake_unreal((json.dumps(self.payload()), [])),
            self.MESH,
            slot_count=2,
        )

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["slot_count"], 2)

    def test_ue58_python_binding_without_bool_still_fails_closed(self):
        failed_payload = self.payload()
        failed_payload["ok"] = False
        cases = [
            (json.dumps(failed_payload), []),
            (json.dumps(self.payload()), ["native audit error"]),
        ]
        for raw in cases:
            with self.subTest(raw=raw):
                with self.assertRaisesRegex(
                    NaniteAssemblyMaterialError,
                    "material-section audit failed",
                ):
                    audit_unreal_skeletal_mesh_material_sections(
                        self.fake_unreal(raw),
                        self.MESH,
                        slot_count=2,
                    )

    def test_unexpected_python_binding_shapes_fail_closed(self):
        payload = json.dumps(self.payload())
        cases = [
            payload,
            (payload,),
            (0, payload, []),
            (payload, "not-an-error-array"),
            ("unexpected", payload, []),
        ]
        for raw in cases:
            with self.subTest(raw=raw):
                with self.assertRaisesRegex(
                    NaniteAssemblyMaterialError,
                    "unexpected Python binding shape",
                ):
                    audit_unreal_skeletal_mesh_material_sections(
                        self.fake_unreal(raw),
                        self.MESH,
                        slot_count=2,
                    )

    def test_missing_lightweight_helper_fails_closed(self):
        fake_unreal = types.SimpleNamespace(
            CodexMaterialToolsLibrary=types.SimpleNamespace(
                audit_skeletal_mesh_lod0_streams=lambda _path: self.fail(
                    "the old full-stream helper must not be used as a fallback"
                )
            )
        )

        with self.assertRaisesRegex(
            NaniteAssemblyMaterialError,
            "lightweight material-section audit is unavailable",
        ):
            audit_unreal_skeletal_mesh_material_sections(
                fake_unreal,
                self.MESH,
                slot_count=2,
            )

    def test_native_failure_or_returned_errors_fails_closed(self):
        cases = [
            (False, json.dumps(self.payload()), []),
            (True, json.dumps(self.payload()), ["native audit error"]),
        ]
        for raw in cases:
            with self.subTest(raw=raw):
                with self.assertRaisesRegex(
                    NaniteAssemblyMaterialError,
                    "material-section audit failed",
                ):
                    audit_unreal_skeletal_mesh_material_sections(
                        self.fake_unreal(raw),
                        self.MESH,
                        slot_count=2,
                    )

    def test_schema_counts_and_section_shape_fail_closed(self):
        cases = []

        wrong_schema = self.payload()
        wrong_schema["schema_version"] = 2
        cases.append(("schema mismatch", wrong_schema, 2))

        boolean_schema = self.payload()
        boolean_schema["schema_version"] = True
        cases.append(("schema mismatch", boolean_schema, 2))

        wrong_kind = self.payload()
        wrong_kind["audit"] = "skeletal_mesh_lod0_streams"
        cases.append(("kind mismatch", wrong_kind, 2))

        wrong_material_count = self.payload()
        wrong_material_count["material_count"] = 3
        cases.append(("count mismatch", wrong_material_count, 2))

        wrong_section_count = self.payload()
        wrong_section_count["section_count"] = 3
        cases.append(("count mismatch", wrong_section_count, 2))

        missing_section_field = self.payload()
        missing_section_field["sections"][0].pop("base_index")
        cases.append(("section row is missing", missing_section_field, 2))

        still_compiling = self.payload()
        still_compiling["target_compiling_after"] = True
        cases.append(("still compiling", still_compiling, 2))

        for message, payload, slot_count in cases:
            with self.subTest(message=message):
                raw = True, json.dumps(payload), []
                with self.assertRaisesRegex(
                    NaniteAssemblyMaterialError,
                    message,
                ):
                    audit_unreal_skeletal_mesh_material_sections(
                        self.fake_unreal(raw),
                        self.MESH,
                        slot_count=slot_count,
                    )

        with self.assertRaisesRegex(
            NaniteAssemblyMaterialError,
            "material count changed",
        ):
            audit_unreal_skeletal_mesh_material_sections(
                self.fake_unreal((True, json.dumps(self.payload()), [])),
                self.MESH,
                slot_count=3,
            )

    def test_out_of_range_material_index_fails_closed(self):
        payload = deepcopy(self.payload())
        payload["sections"][1]["material_index"] = 2

        with self.assertRaisesRegex(
            NaniteAssemblyMaterialError,
            "invalid section material indices",
        ):
            audit_unreal_skeletal_mesh_material_sections(
                self.fake_unreal((True, json.dumps(payload), [])),
                self.MESH,
                slot_count=2,
            )

    def test_boolean_material_row_index_fails_closed(self):
        payload = deepcopy(self.payload())
        payload["materials"][0]["index"] = False

        with self.assertRaisesRegex(
            NaniteAssemblyMaterialError,
            "material order changed",
        ):
            audit_unreal_skeletal_mesh_material_sections(
                self.fake_unreal((True, json.dumps(payload), [])),
                self.MESH,
                slot_count=2,
            )


class MaterialNormalizationPlanTests(unittest.TestCase):
    def test_missing_atlas_is_appended_and_leaf_remap_is_rebuilt(self):
        globals_before = [
            slot("M_bark_common_end_01", "/Game/MI/MI_bark_common_end_01"),
            slot("M_Bark_elm_01", "/Game/MI/MI_Bark_elm_01"),
            slot("M_branch_elm_01", "/Game/MI/MI_branch_elm_01"),
            slot("M_leaf_elm_01", "/Game/MI/MI_leaf_elm_01"),
            slot("M_leaf_elm_side_01", "/Game/MI/MI_leaf_elm_side_01"),
        ]
        result = plan_nanite_assembly_material_normalization(
            globals_before,
            [
                {
                    "mesh": "/Game/Tree/SK_leaf.SK_leaf",
                    "remap": [0, 1, 2, 3, 4],
                    "slots": [
                        slot("M_Bark_elm_01", "/Game/MI/MI_Bark_elm_01"),
                        slot("M_leaf_elm_atlas_01", "/Game/MI/MI_leaf_elm_atlas_01"),
                    ],
                }
            ],
        )
        self.assertEqual(result["parts"][0]["desired_remap"], [1, 5])
        self.assertEqual(len(result["global_slots"]), 6)
        self.assertEqual(
            result["appended_slots"][0]["slot_name"],
            "M_leaf_elm_atlas_01",
        )

    def test_same_slot_name_with_different_material_never_silently_matches(self):
        result = plan_nanite_assembly_material_normalization(
            [slot("M_leaf", "/Game/MI/MI_wrong")],
            [
                {
                    "mesh": "/Game/Tree/SK_leaf.SK_leaf",
                    "remap": [0],
                    "slots": [slot("M_leaf", "/Game/MI/MI_correct")],
                }
            ],
        )
        self.assertEqual(result["parts"][0]["desired_remap"], [1])
        self.assertEqual(len(result["appended_slots"]), 1)

    def test_plan_is_idempotent_when_table_and_remap_are_canonical(self):
        materials = [
            slot("M_bark", "/Game/MI/MI_bark"),
            slot("M_leaf", "/Game/MI/MI_leaf"),
        ]
        result = plan_nanite_assembly_material_normalization(
            materials,
            [
                {
                    "mesh": "/Game/Tree/SK_leaf.SK_leaf",
                    "remap": [0, 1],
                    "slots": materials,
                }
            ],
        )
        self.assertEqual(result["parts"][0]["desired_remap"], [0, 1])
        self.assertEqual(result["appended_slots"], [])

    def test_unassigned_part_material_fails_closed(self):
        with self.assertRaisesRegex(NaniteAssemblyMaterialError, "no assigned material"):
            plan_nanite_assembly_material_normalization(
                [slot("M_bark", "/Game/MI/MI_bark")],
                [
                    {
                        "mesh": "/Game/Tree/SK_leaf.SK_leaf",
                        "remap": [0],
                        "slots": [slot("M_leaf", None)],
                    }
                ],
            )

    def test_duplicate_reimported_part_slots_fail_closed(self):
        duplicate = slot("M_leaf", "/Game/MI/MI_leaf")
        with self.assertRaisesRegex(
            NaniteAssemblyMaterialError,
            "duplicate material identity after reimport",
        ):
            plan_nanite_assembly_material_normalization(
                [duplicate],
                [{
                    "mesh": "/Game/Tree/SK_leaf.SK_leaf",
                    "remap": [0, 0],
                    "slots": [duplicate, dict(duplicate)],
                }],
            )


class ProtectedRemapTextTests(unittest.TestCase):
    TEXT = (
        '(NaniteAssemblyData=(Parts=((MeshObjectPath="/Game/A.A",'
        'MaterialRemap=(0,1,2)),(MeshObjectPath="/Game/B.B",'
        'MaterialRemap=(3,4)))))'
    )

    def test_parse_and_rewrite_preserve_part_order(self):
        rows = parse_nanite_assembly_part_remaps(self.TEXT)
        self.assertEqual([row["mesh"] for row in rows], ["/Game/A.A", "/Game/B.B"])
        rewritten = rewrite_nanite_assembly_part_remaps(self.TEXT, [[1, 5], [2]])
        after = parse_nanite_assembly_part_remaps(rewritten)
        self.assertEqual([row["remap"] for row in after], [[1, 5], [2]])
        self.assertIn("NaniteAssemblyData", rewritten)

    def test_part_count_mismatch_fails_closed(self):
        with self.assertRaisesRegex(NaniteAssemblyMaterialError, "count changed"):
            rewrite_nanite_assembly_part_remaps(self.TEXT, [[0]])


if __name__ == "__main__":
    unittest.main()
