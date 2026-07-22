from __future__ import annotations

import json
import math
import sys
import tempfile
import unittest
from collections import namedtuple
from pathlib import Path
from unittest import mock


SK_BATCH = Path(__file__).resolve().parents[1]
if str(SK_BATCH) not in sys.path:
    sys.path.insert(0, str(SK_BATCH))

from cluster_assembly_builder import (  # noqa: E402
    ClusterAssemblyBuildError,
    build_unreal_ingest_plan,
    content_build_decision,
    file_fingerprint,
    fit_trs_transform,
    lowest_common_ancestor,
    make_skeleton_snapshot,
    scope_material_pipeline_to_codex_tests,
    _export_selected_fbx,
    _strip_fbx_scene_textures,
    validate_binding_hierarchy,
    validate_unreal_asset_contract,
    validate_wind_json_against_skeleton,
)


def skeleton_snapshot():
    identity = [
        [1.0, 0.0, 0.0, 0.0],
        [0.0, 1.0, 0.0, 0.0],
        [0.0, 0.0, 1.0, 0.0],
        [0.0, 0.0, 0.0, 1.0],
    ]
    return make_skeleton_snapshot(
        [
            {
                "index": 0,
                "name": "Root",
                "parent_index": -1,
                "parent_name": None,
                "bind_matrix": identity,
            },
            {
                "index": 1,
                "name": "Trunk",
                "parent_index": 0,
                "parent_name": "Root",
                "bind_matrix": identity,
            },
            {
                "index": 2,
                "name": "Branch_A",
                "parent_index": 1,
                "parent_name": "Trunk",
                "bind_matrix": identity,
            },
            {
                "index": 3,
                "name": "Leaf_A",
                "parent_index": 2,
                "parent_name": "Branch_A",
                "bind_matrix": identity,
            },
        ]
    )


def ready_handoff():
    return {
        "status": "ready",
        "full_skeletal_mesh": {"preserved": True},
        "assembly": {
            "requested": True,
            "part_builder_inputs": [
                {
                    "role": "branch",
                    "role_identity": "branch_elm_01",
                    "assignments": [{"object": "tree", "polygon_indices": [1]}],
                },
                {
                    "role": "leaf",
                    "role_identity": "leaf_elm_01",
                    "assignments": [{"object": "tree", "polygon_indices": [2]}],
                },
            ],
        },
    }


class ContentDecisionTests(unittest.TestCase):
    def test_ready_is_automatic_content_driven_build(self):
        self.assertEqual(content_build_decision(ready_handoff()), "build")

    def test_absent_roles_pass_through_without_a_toggle(self):
        handoff = {
            "status": "pass_through",
            "full_skeletal_mesh": {"preserved": True},
            "assembly": {"requested": False, "part_builder_inputs": []},
        }
        self.assertEqual(content_build_decision(handoff), "pass_through")

    def test_partial_or_blocked_handoff_fails_closed(self):
        with self.assertRaisesRegex(ClusterAssemblyBuildError, "blocked"):
            content_build_decision(
                {
                    "status": "blocked",
                    "issues": [{"role": "leaf", "reason": "material_without_mesh"}],
                }
            )

    def test_ready_requires_actual_polygon_assignment_evidence(self):
        handoff = ready_handoff()
        handoff["assembly"]["part_builder_inputs"][0]["assignments"] = []
        with self.assertRaisesRegex(ClusterAssemblyBuildError, "actual FBX polygon"):
            content_build_decision(handoff)


class FinalSkeletonHierarchyTests(unittest.TestCase):
    def test_snapshot_hash_covers_count_order_parent_and_bind_pose(self):
        snapshot = skeleton_snapshot()
        self.assertEqual(snapshot["contract"], "final_skeleton_v2")
        self.assertEqual(snapshot["bone_count"], 4)
        changed = json.loads(json.dumps(snapshot["bones"]))
        changed[3]["bind_matrix"][3][0] = 2.0
        self.assertNotEqual(snapshot["sha256"], make_skeleton_snapshot(changed)["sha256"])

    def test_snapshot_rejects_parent_order_or_name_mismatch(self):
        bones = skeleton_snapshot()["bones"]
        bones[2]["parent_name"] = "Root"
        with self.assertRaisesRegex(ClusterAssemblyBuildError, "parent name/index"):
            make_skeleton_snapshot(bones)

    def test_binding_records_real_common_ancestor(self):
        snapshot = skeleton_snapshot()
        binding = {
            "bone_influences": [
                {"bone": "Branch_A", "weight": 0.25},
                {"bone": "Leaf_A", "weight": 0.75},
            ]
        }
        report = validate_binding_hierarchy(binding, snapshot)
        self.assertEqual(report["anchor_bone"], "Branch_A")
        self.assertEqual(
            lowest_common_ancestor(["Branch_A", "Leaf_A"], snapshot),
            "Branch_A",
        )

    def test_binding_missing_bone_or_bad_weights_is_blocked(self):
        snapshot = skeleton_snapshot()
        with self.assertRaisesRegex(ClusterAssemblyBuildError, "missing"):
            validate_binding_hierarchy(
                {"bone_influences": [{"bone": "ProductionOnly", "weight": 1.0}]},
                snapshot,
            )
        with self.assertRaisesRegex(ClusterAssemblyBuildError, "sum to one"):
            validate_binding_hierarchy(
                {"bone_influences": [{"bone": "Leaf_A", "weight": 0.5}]},
                snapshot,
            )


class DynamicWindContractTests(unittest.TestCase):
    def wind_payload(self):
        snapshot = skeleton_snapshot()
        return {
            "SkeletonContract": {
                "SchemaVersion": 2,
                "BoneCount": snapshot["bone_count"],
                "BoneNameIndexParentSha1": snapshot[
                    "bone_name_index_parent_sha1"
                ],
                "Bones": [
                    {
                        "BoneName": row["name"],
                        "BoneIndex": row["index"],
                        "ParentIndex": row["parent_index"],
                    }
                    for row in snapshot["bones"]
                ],
                "ImportRoot": {
                    "BoneName": snapshot["bones"][0]["name"],
                    "BoneIndex": 0,
                    "ParentIndex": -1,
                },
            },
            "Joints": [
                {
                    "JointName": "Trunk",
                    "BoneIndex": 1,
                    "ParentIndex": 0,
                    "SimulationGroupIndex": 0,
                },
                {
                    "JointName": "Branch_A",
                    "BoneIndex": 2,
                    "ParentIndex": 1,
                    "SimulationGroupIndex": 0,
                },
                {
                    "JointName": "Leaf_A",
                    "BoneIndex": 3,
                    "ParentIndex": 2,
                    "SimulationGroupIndex": 1,
                },
            ],
            "SimulationGroups": [{"Name": "branch"}, {"Name": "leaf"}],
        }

    def test_names_resolve_to_final_indices_and_binding_wind_hierarchy(self):
        binding = {
            "anchor_bone": "Branch_A",
            "bone_influences": [
                {"bone": "Branch_A", "weight": 0.2},
                {"bone": "Leaf_A", "weight": 0.8},
            ],
        }
        report = validate_wind_json_against_skeleton(
            self.wind_payload(),
            skeleton_snapshot(),
            [binding],
        )
        self.assertEqual(report["status"], "ok")
        self.assertEqual(
            [(row["joint_name"], row["bone_index"]) for row in report["resolved_joints"]],
            [("Trunk", 1), ("Branch_A", 2), ("Leaf_A", 3)],
        )
        self.assertEqual(
            report["binding_hierarchy"][0]["anchor_bone"],
            "Branch_A",
        )

    def test_declared_wind_index_must_match_final_skeleton(self):
        payload = self.wind_payload()
        payload["Joints"][1]["BoneIndex"] = 99
        with self.assertRaisesRegex(ClusterAssemblyBuildError, "index mismatch"):
            validate_wind_json_against_skeleton(payload, skeleton_snapshot())

    def test_parent_and_complete_skeleton_contract_are_mandatory(self):
        payload = self.wind_payload()
        del payload["Joints"][1]["ParentIndex"]
        with self.assertRaisesRegex(ClusterAssemblyBuildError, "index/parent"):
            validate_wind_json_against_skeleton(payload, skeleton_snapshot())

        payload = self.wind_payload()
        payload["SkeletonContract"]["Bones"][2]["ParentIndex"] = 0
        with self.assertRaisesRegex(ClusterAssemblyBuildError, "name/index/parent"):
            validate_wind_json_against_skeleton(payload, skeleton_snapshot())

    def test_missing_joint_duplicate_and_group_overflow_fail_closed(self):
        payload = self.wind_payload()
        payload["Joints"][0]["JointName"] = "ProductionOnly"
        with self.assertRaisesRegex(ClusterAssemblyBuildError, "missing"):
            validate_wind_json_against_skeleton(payload, skeleton_snapshot())

        payload = self.wind_payload()
        payload["Joints"][1]["JointName"] = "Trunk"
        with self.assertRaisesRegex(ClusterAssemblyBuildError, "duplicated"):
            validate_wind_json_against_skeleton(payload, skeleton_snapshot())

        payload = self.wind_payload()
        payload["Joints"][2]["SimulationGroupIndex"] = 2
        with self.assertRaisesRegex(ClusterAssemblyBuildError, "out of range"):
            validate_wind_json_against_skeleton(payload, skeleton_snapshot())

    def test_binding_bone_not_in_new_wind_json_is_blocked(self):
        binding = {
            "bone_influences": [{"bone": "Root", "weight": 1.0}],
        }
        with self.assertRaisesRegex(ClusterAssemblyBuildError, "wind JSON"):
            validate_wind_json_against_skeleton(
                self.wind_payload(),
                skeleton_snapshot(),
                [binding],
            )

    def test_file_contract_includes_exact_wind_hash(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "wind.json"
            path.write_text(json.dumps(self.wind_payload()), encoding="utf-8")
            report = validate_wind_json_against_skeleton(path, skeleton_snapshot())
            self.assertTrue(report["wind_json"]["exists"])
            self.assertEqual(len(report["wind_json"]["sha256"]), 64)


class TransformAndUnrealPlanTests(unittest.TestCase):
    def test_assembly_fbx_scene_data_keeps_materials_but_strips_textures(self):
        SceneData = namedtuple(
            "SceneData",
            (
                "templates",
                "templates_users",
                "connections",
                "data_textures",
                "data_videos",
                "data_materials",
            ),
        )
        scene_data = SceneData(
            templates={
                b"Material": mock.Mock(nbr_users=1),
                b"TextureFile": mock.Mock(nbr_users=2),
                b"Video": mock.Mock(nbr_users=3),
            },
            templates_users=6,
            connections=[
                (b"OO", 10, 20, None),
                (b"OO", 30, 40, None),
                (b"OP", 50, 60, b"DiffuseColor"),
            ],
            data_textures={"color": (("texture", 10), object())},
            data_videos={"color": (("video", 30), object())},
            data_materials={"M_leaf_elm_01": object()},
        )

        stripped = _strip_fbx_scene_textures(
            scene_data,
            lambda key: key[1],
        )

        self.assertEqual(stripped.data_textures, {})
        self.assertEqual(stripped.data_videos, {})
        self.assertEqual(stripped.data_materials, scene_data.data_materials)
        self.assertEqual(stripped.templates_users, 1)
        self.assertEqual(set(stripped.templates), {b"Material"})
        self.assertEqual(stripped.connections, [(b"OP", 50, 60, b"DiffuseColor")])

    def test_assembly_fbx_uses_the_full_sk_stock_export_contract(self):
        class DummyObject:
            mode = "OBJECT"
            hide_viewport = False

            def hide_get(self):
                return False

            def hide_set(self, value):
                del value

            def select_set(self, value):
                del value

        class DummyBpy:
            def __init__(self, target):
                self.context = mock.Mock()
                self.context.object = None
                self.context.view_layer.objects.active = None
                self.ops = mock.Mock()

                def export(**kwargs):
                    target.write_bytes(b"fbx")
                    return {"FINISHED"}

                self.ops.export_scene.fbx.side_effect = export

        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "BASE_SK_Tree_elm_01.fbx"
            bpy = DummyBpy(target)
            _export_selected_fbx(bpy, target, [DummyObject(), DummyObject()])
            kwargs = bpy.ops.export_scene.fbx.call_args.kwargs

        self.assertFalse(kwargs["use_mesh_modifiers"])
        self.assertEqual(kwargs["mesh_smooth_type"], "FACE")
        self.assertFalse(kwargs["use_custom_props"])
        self.assertEqual(kwargs["primary_bone_axis"], "Y")
        self.assertEqual(kwargs["secondary_bone_axis"], "X")
        self.assertEqual(kwargs["armature_nodetype"], "NULL")
        self.assertFalse(kwargs["add_leaf_bones"])
        self.assertFalse(kwargs["bake_anim"])
        self.assertNotIn("apply_unit_scale", kwargs)
        self.assertNotIn("axis_forward", kwargs)
        self.assertNotIn("axis_up", kwargs)

    def test_ingest_plan_reuses_full_send2ue_contract_and_final_skeleton_marker(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            base = root / "BASE_SK_Tree_elm_01.fbx"
            branch = root / "PART_branch_a.fbx"
            full = root / "Full.fbx"
            wind = root / "wind.json"
            sidecar = root / "Full.json"
            base.write_bytes(b"base")
            branch.write_bytes(b"branch")
            full.write_bytes(b"full")
            wind.write_text("{}", encoding="utf-8")
            sidecar.write_text(
                json.dumps(
                    {
                        "mesh_name": "SK_Tree_elm_01",
                        "speedtree_handoff_contract": {
                            "kind": "speedtree",
                            "version": 1,
                            "fingerprint": "contract",
                            "asset_kind": "speedtree",
                            "mesh_name": "SK_Tree_elm_01",
                        },
                        "materials": [],
                    }
                ),
                encoding="utf-8",
            )
            manifest = {
                "status": "ready",
                "full_fbx": file_fingerprint(full),
                "base": {"fbx": file_fingerprint(base)},
                "wind_contract": {"wind_json": file_fingerprint(wind)},
                "parts": [{
                    "prototype_id": "branch_a",
                    "fbx": file_fingerprint(branch),
                }],
            }
            template = {
                "asset_id": "full",
                "asset_data": {
                    "_asset_type": "SkeletalMesh",
                    "asset_path": "/Game/Codex/Tests/Elm/SK_Tree_elm_01",
                    "file_path": str(full),
                    "_material_pipeline_json_path": str(sidecar),
                },
                "pre_import_commands": [[
                    "_asset_path = '/Game/Codex/Tests/Elm/SK_Tree_elm_01'",
                    f"json_path='{sidecar.as_posix()}'",
                ]],
                "post_import_commands": [],
            }
            plan = build_unreal_ingest_plan(
                manifest,
                template,
                "/Game/Codex/Tests/Elm/SK_Tree_elm_01",
                "/Game/Codex/Tests/Elm",
            )
            self.assertEqual(plan["status"], "ready")
            self.assertEqual(
                plan["assets"][0]["asset_data"]["skeleton_asset_path"],
                "__FULL_FINAL_SKELETON__",
            )
            self.assertEqual(plan["assets"][1]["asset_data"]["skeleton_asset_path"], "")
            self.assertIn("/Assembly/BASE_SK_Tree_elm_01", plan["asset_contract"]["base_skeletal_mesh"])
            self.assertIn(
                "/Assembly/BASE_SK_Tree_elm_01",
                plan["assets"][0]["pre_import_commands"][0][0],
            )
            generated_sidecar = Path(
                plan["assets"][0]["asset_data"][
                    "_material_pipeline_json_path"
                ]
            )
            generated_payload = json.loads(
                generated_sidecar.read_text(encoding="utf-8")
            )
            self.assertEqual(
                generated_payload["mesh_name"], "BASE_SK_Tree_elm_01"
            )
            self.assertEqual(
                generated_payload["speedtree_handoff_contract"]["mesh_name"],
                "BASE_SK_Tree_elm_01",
            )
            self.assertIn(
                generated_sidecar.as_posix(),
                plan["assets"][0]["pre_import_commands"][0][1],
            )

            branch.write_bytes(b"tampered")
            with self.assertRaisesRegex(
                ClusterAssemblyBuildError, "changed after the BWR receipt"
            ):
                build_unreal_ingest_plan(
                    manifest,
                    template,
                    "/Game/Codex/Tests/Elm/SK_Tree_elm_01",
                    "/Game/Codex/Tests/Elm",
                )

    def test_trs_fit_recovers_translation_rotation_and_scale(self):
        source = [
            [-1.0, -1.0, 0.0],
            [1.0, -1.0, 0.0],
            [1.0, 1.0, 0.0],
            [-1.0, 1.0, 0.0],
        ]
        # Row-vector transform: rotate 90 degrees around Z, scale (2, 3, 1),
        # then translate.
        target = [
            [5.0 + (-point[1]) * 2.0, -2.0 + point[0] * 3.0, 7.0]
            for point in source
        ]
        transform = fit_trs_transform(source, target)
        self.assertLess(transform["trs_relative_rms"], 1.0e-10)
        self.assertTrue(all(math.isfinite(value) for value in transform["rotation_xyzw"]))
        self.assertAlmostEqual(transform["translation"][0], 5.0, places=8)
        self.assertAlmostEqual(transform["translation"][1], -2.0, places=8)
        self.assertAlmostEqual(transform["translation"][2], 7.0, places=8)

    def test_unreal_plan_keeps_full_and_assembly_separate(self):
        manifest = {
            "status": "ready",
            "parts": [
                {"prototype_id": "branch_a"},
                {"prototype_id": "leaf_a"},
            ],
        }
        contract = {
            "full_skeletal_mesh": "/Game/Codex/Tests/Elm/Full/SK_Elm_Full",
            "base_skeletal_mesh": "/Game/Codex/Tests/Elm/Base/SK_Elm_Base",
            "parts": {
                "branch_a": "/Game/Codex/Tests/Elm/Parts/SK_Branch",
                "leaf_a": "/Game/Codex/Tests/Elm/Parts/SK_Leaf",
            },
            "assembly": "/Game/Codex/Tests/Elm/Assembly/SK_Elm_Assembly",
        }
        result = validate_unreal_asset_contract(manifest, contract)
        self.assertNotEqual(result["full_skeletal_mesh"], result["assembly"])

    def test_unreal_plan_requires_every_generated_prototype(self):
        manifest = {
            "status": "ready",
            "parts": [{"prototype_id": "branch_a"}],
        }
        contract = {
            "full_skeletal_mesh": "/Game/Test/Full",
            "base_skeletal_mesh": "/Game/Test/Base",
            "parts": {},
            "assembly": "/Game/Test/Assembly",
        }
        with self.assertRaisesRegex(ClusterAssemblyBuildError, "exactly match"):
            validate_unreal_asset_contract(manifest, contract)


class CodexTestMaterialScopeTests(unittest.TestCase):
    def test_material_pipeline_outputs_are_rewritten_under_test_root(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sidecar = root / "full.json"
            sidecar.write_text(
                json.dumps(
                    {
                        "mesh_name": "Full",
                        "material_master": "tree",
                        "materials": [
                            {
                                "name": "M_leaf_elm_01",
                                "master_preset": "tree",
                                "speedtree_intent": {
                                    "material_instance_base": "leaf_elm_01",
                                },
                                "material_layer": {
                                    "instance_path": (
                                        "/Game/Material/Tree/AssetTree/MYI/"
                                        "MYI_leaf_elm_01"
                                    ),
                                },
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            module_load = "_spec.loader.exec_module(_p)"
            assets = [
                {
                    "asset_data": {
                        "asset_path": "/Game/Codex/Tests/Elm/Full",
                        "_material_pipeline_json_path": str(sidecar),
                    },
                    "pre_import_commands": [[module_load, "preflight()"]],
                    "post_import_commands": [[module_load, "process()"]],
                }
            ]
            report = scope_material_pipeline_to_codex_tests(
                assets,
                "/Game/Codex/Tests/Elm",
                root / "out",
            )
            self.assertEqual(report["status"], "scoped_to_codex_tests")
            scoped_path = Path(
                assets[0]["asset_data"]["_material_pipeline_json_path"]
            )
            payload = json.loads(scoped_path.read_text(encoding="utf-8"))
            self.assertEqual(
                payload["materials"][0]["target_material_path"],
                "/Game/Codex/Tests/Elm/_MaterialPipeline/MI/MI_leaf_elm_01",
            )
            self.assertEqual(
                payload["materials"][0]["material_layer"]["instance_path"],
                "/Game/Codex/Tests/Elm/_MaterialPipeline/MYI/MYI_leaf_elm_01",
            )
            commands = "\n".join(
                assets[0]["pre_import_commands"][0]
                + assets[0]["post_import_commands"][0]
            )
            self.assertIn(
                "/Game/Codex/Tests/Elm/_MaterialPipeline/Textures",
                commands,
            )
            self.assertIn(
                "/Game/Codex/Tests/Elm/_MaterialPipeline/MI",
                commands,
            )
            self.assertNotIn("/Game/Material/Tree/AssetTree/MYI", commands)

    def test_non_test_destination_is_rejected(self):
        with self.assertRaisesRegex(ClusterAssemblyBuildError, "Codex/Tests"):
            scope_material_pipeline_to_codex_tests(
                [],
                "/Game/Meshes/Tree",
                ".",
            )

    def test_manifest_asset_outside_test_destination_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sidecar = root / "full.json"
            sidecar.write_text(
                json.dumps(
                    {
                        "material_master": "tree",
                        "materials": [
                            {"name": "M_leaf_elm_01", "master_preset": "tree"}
                        ],
                    }
                ),
                encoding="utf-8",
            )
            assets = [
                {
                    "asset_data": {
                        "asset_path": "/Game/Meshes/Tree/Full",
                        "_material_pipeline_json_path": str(sidecar),
                    }
                }
            ]
            with self.assertRaisesRegex(ClusterAssemblyBuildError, "every manifest asset"):
                scope_material_pipeline_to_codex_tests(
                    assets,
                    "/Game/Codex/Tests/Elm",
                    root / "out",
                )


if __name__ == "__main__":
    unittest.main()
