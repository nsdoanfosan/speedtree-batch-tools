from __future__ import annotations

import sys
import unittest
from pathlib import Path


SK_BATCH = Path(__file__).resolve().parents[1]
if str(SK_BATCH) not in sys.path:
    sys.path.insert(0, str(SK_BATCH))

from nanite_assembly_materials import (  # noqa: E402
    NaniteAssemblyMaterialError,
    parse_nanite_assembly_part_remaps,
    plan_nanite_assembly_material_normalization,
    rewrite_nanite_assembly_part_remaps,
)


def slot(name, material):
    return {"slot_name": name, "material": material}


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
