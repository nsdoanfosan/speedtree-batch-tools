import sys
import unittest
from pathlib import Path


SK_BATCH_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SK_BATCH_DIR))

from dynamic_wind_handoff_policy import (  # noqa: E402
    WIND_MODE_DEFERRED_ASSEMBLY,
    WIND_MODE_DISABLED,
    WIND_MODE_FINAL_SKELETON,
    resolve_dynamic_wind_policy,
)


def normalized_part_objects():
    return [
        {
            "name": "SK_leaf_elm_01_01_Armature",
            "type": "ARMATURE",
            "cluster_generated": True,
            "asset_role": "skeletal_armature",
            "bone_names": ["part_root"],
        },
        {
            "name": "SK_leaf_elm_01_01_Mesh",
            "type": "MESH",
            "cluster_generated": True,
            "asset_role": "skeletal_mesh",
            "vertex_groups": ["part_root"],
        },
    ]


class DynamicWindHandoffPolicyTests(unittest.TestCase):
    def test_normalized_one_bone_prototype_defers_wind_to_final_assembly(self):
        policy = resolve_dynamic_wind_policy(normalized_part_objects())

        self.assertEqual(policy["mode"], WIND_MODE_DEFERRED_ASSEMBLY)
        self.assertFalse(policy["requires_json"])
        self.assertTrue(policy["target"]["normalized_cluster_prototype"])

    def test_final_assembly_always_requires_full_skeleton_wind(self):
        policy = resolve_dynamic_wind_policy(
            normalized_part_objects(),
            cluster_assembly_status="ready",
        )

        self.assertEqual(policy["mode"], WIND_MODE_FINAL_SKELETON)
        self.assertTrue(policy["requires_json"])

    def test_regular_final_skeleton_requires_wind(self):
        policy = resolve_dynamic_wind_policy(
            [
                {
                    "name": "Root",
                    "type": "ARMATURE",
                    "cluster_generated": False,
                    "asset_role": None,
                    "bone_names": ["Bone_1_End", "Bone_2_Start"],
                },
                {
                    "name": "SK_Tree_elm_01",
                    "type": "MESH",
                    "cluster_generated": False,
                    "asset_role": None,
                    "vertex_groups": ["Bone_1_End", "Bone_2_Start"],
                },
            ]
        )

        self.assertEqual(policy["mode"], WIND_MODE_FINAL_SKELETON)
        self.assertTrue(policy["requires_json"])

    def test_generated_multibone_export_is_not_misclassified_as_rigid_part(self):
        objects = normalized_part_objects()
        objects[0]["bone_names"] = ["part_root", "part_tip"]
        objects[1]["vertex_groups"] = ["part_root", "part_tip"]

        policy = resolve_dynamic_wind_policy(objects)

        self.assertEqual(policy["mode"], WIND_MODE_FINAL_SKELETON)
        self.assertTrue(policy["requires_json"])

    def test_explicit_skip_remains_explicit(self):
        policy = resolve_dynamic_wind_policy([], explicit_skip=True)

        self.assertEqual(policy["mode"], WIND_MODE_DISABLED)
        self.assertFalse(policy["requires_json"])


if __name__ == "__main__":
    unittest.main()
