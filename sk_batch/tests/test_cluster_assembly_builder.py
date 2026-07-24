from __future__ import annotations

import json
import math
import sys
import tempfile
import unittest
from collections import namedtuple
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


SK_BATCH = Path(__file__).resolve().parents[1]
if str(SK_BATCH) not in sys.path:
    sys.path.insert(0, str(SK_BATCH))

from cluster_assembly_builder import (  # noqa: E402
    ClusterAssemblyBuildError,
    build_unreal_ingest_plan,
    compose_similarity_with_relative_matrix,
    content_build_decision,
    file_fingerprint,
    fit_trs_transform,
    fit_uniform_similarity_transform,
    lowest_common_ancestor,
    make_skeleton_snapshot,
    scope_material_pipeline_for_destination,
    scope_material_pipeline_to_codex_tests,
    _build_unreal_assembly_provenance_payload,
    _coalesce_normalized_external_parts,
    _component_groups,
    _component_signature,
    _expected_normalized_bounds_for_variant,
    _export_selected_fbx,
    _normalized_prototype_for_component,
    _ordered_cross_object_correspondence,
    _strip_fbx_scene_textures,
    _vertex_descriptors,
    validate_binding_hierarchy,
    validate_manifest_artifacts,
    validate_normalized_prototype_unit_contract,
    validate_unreal_asset_contract,
    validate_unreal_bounds_contract,
    validate_unreal_normalized_prototype_bounds,
    validate_wind_json_against_skeleton,
)


def seam_split_test_mesh(split_second_face=False):
    coordinates = [
        (0.0, 0.0, 0.0),
        (1.0, 0.0, 0.0),
        (1.0, 1.0, 0.0),
        (0.0, 1.0, 0.0),
    ]
    faces = [(0, 1, 2), (0, 2, 3)]
    if split_second_face:
        coordinates.extend([coordinates[0], coordinates[2], coordinates[3]])
        faces[1] = (4, 5, 6)
    vertices = [SimpleNamespace(co=value) for value in coordinates]
    loops = []
    uv_data = []
    polygons = []
    uv_by_position = {
        (0.0, 0.0, 0.0): (0.0, 0.0),
        (1.0, 0.0, 0.0): (1.0, 0.0),
        (1.0, 1.0, 0.0): (1.0, 1.0),
        (0.0, 1.0, 0.0): (0.0, 1.0),
    }
    for polygon_index, face in enumerate(faces):
        loop_indices = []
        for vertex_index in face:
            loop_indices.append(len(loops))
            loops.append(SimpleNamespace(vertex_index=vertex_index))
            uv_data.append(SimpleNamespace(
                uv=SimpleNamespace(
                    x=uv_by_position[coordinates[vertex_index]][0],
                    y=uv_by_position[coordinates[vertex_index]][1],
                )
            ))
        polygons.append(SimpleNamespace(
            index=polygon_index,
            vertices=face,
            loop_indices=loop_indices,
        ))
    return SimpleNamespace(
        vertices=vertices,
        polygons=polygons,
        loops=loops,
        uv_layers=SimpleNamespace(active=SimpleNamespace(data=uv_data)),
    )


class ComponentTopologyTests(unittest.TestCase):
    def test_position_weld_preserves_card_topology_across_seam_split(self):
        source = seam_split_test_mesh(False)
        split = seam_split_test_mesh(True)
        source_component = _component_groups(source, [0, 1])
        split_component = _component_groups(split, [0, 1])
        self.assertEqual(len(source_component), 1)
        self.assertEqual(len(split_component), 1)
        self.assertEqual(len(split_component[0]["vertices"]), 6)
        self.assertEqual(
            _component_signature(source, source_component[0]),
            _component_signature(split, split_component[0]),
        )
        source_descriptors = _vertex_descriptors(
            SimpleNamespace(data=source), source_component[0]
        )
        split_descriptors = _vertex_descriptors(
            SimpleNamespace(data=split), split_component[0]
        )
        self.assertEqual(
            [row[0] for row in source_descriptors],
            [row[0] for row in split_descriptors],
        )

    def test_unique_uv_face_subset_uses_the_normalized_prototype(self):
        source = seam_split_test_mesh(False)
        target = seam_split_test_mesh(True)
        source_component = _component_groups(source, [0, 1])[0]
        target_component = _component_groups(target, [0])[0]
        source_obj = SimpleNamespace(data=source)
        target_obj = SimpleNamespace(data=target)
        prototype = {
            "object": source_obj,
            "component": source_component,
        }
        selected = _normalized_prototype_for_component(
            {"source": prototype}, target, target_component
        )
        self.assertIs(selected, prototype)
        source_indices, target_indices = _ordered_cross_object_correspondence(
            source_obj,
            source_component,
            target_obj,
            target_component,
        )
        self.assertEqual(len(source_indices), 3)
        self.assertEqual(len(target_indices), 3)


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
                    "normalized_variants": {
                        "status": "ready",
                        "variants": [{"ordinal": 1}],
                    },
                },
                {
                    "role": "leaf",
                    "role_identity": "leaf_elm_01",
                    "assignments": [{"object": "tree", "polygon_indices": [2]}],
                    "normalized_variants": {
                        "status": "ready",
                        "variants": [{"ordinal": 1}],
                    },
                },
            ],
        },
    }


def physical_production_manifest():
    role_assets = {
        "branch": ["SK_branch_sample_01_01", "SK_branch_sample_01_02"],
        "leaf": ["SK_leaf_sample_01_01"],
        "leaf_side": [
            "SK_leaf_sample_side_01_01",
            "SK_leaf_sample_side_01_02",
        ],
    }

    def capture_contract(role, assets):
        side = role == "leaf_side"
        contract = {
            "kind": "speedtree_cluster_physical_capture_fit",
            "version": 1,
            "workflow_mode": "PHYSICAL_DIRECT_CAPTURE",
            "source_blend": f"C:/source/{role}.blend",
            "source_collection": "Cluster_Source",
            "source_objects": [],
            "excluded_exact_duplicates": [],
            "frame": {
                "policy": "physical_target_uniform_whole_source_fit",
                "workflow_mode": "PHYSICAL_DIRECT_CAPTURE",
                "plane": "XZ" if side else "XY",
                "center": [1.0, 2.0, 3.0],
                "width": 0.1,
                "height": 0.1,
                "content_width": 0.09,
                "content_height": 0.08,
                "fit_scale": 0.025,
                "unit_system": "METRIC",
                "scale_length": 1.0,
                "target_meters": [0.1, 0.1],
                "target_blender_units": [0.1, 0.1],
                "rotation_degrees": 90.0 if side else 0.0,
                "direct_uv_source": "same_blender_physical_capture_projection",
            },
            "direct_uv_source": "same_blender_physical_capture_projection",
            "attachment_pivots": [
                {
                    "prototype_index": ordinal,
                    "prototype_asset": asset,
                    "xml_bone_id": ordinal,
                    "source_world": [float(ordinal), 0.0, 0.0],
                    "fitted_capture_world": [0.01 * ordinal, 0.0, 0.0],
                    "normalized_local": [0.0, 0.0, 0.0],
                }
                for ordinal, asset in enumerate(assets, 1)
            ],
        }
        contract["contract_sha256"] = __import__("hashlib").sha256(
            json.dumps(
                contract,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        return contract

    normalized_receipts = []
    parts = []
    registered_variants = []
    for role, assets in role_assets.items():
        capture = capture_contract(role, assets)
        receipt = {
            "workflow_mode": "PHYSICAL_DIRECT_CAPTURE",
            "size_policy": "uniform_whole_source_physical_target_meters",
            "plan_uv_policy": "direct_physical_capture_projection",
            "direct_uv_source": "same_blender_physical_capture_projection",
            "generator_size_policy": (
                "preserve_user_authored_leaf_and_frond_dimensions"
            ),
            "physical_capture_contract": capture,
            "physical_capture_contract_sha256": capture["contract_sha256"],
            "prototypes": [
                {
                    "prototype_index": ordinal,
                    "skeletal_asset": asset,
                    "normalized_bounds": {
                        "size": [0.08, 0.09, 0.025],
                    },
                }
                for ordinal, asset in enumerate(assets, 1)
            ],
            "variants": [
                {
                    "index": ordinal,
                    "card_index": ordinal,
                    "skeletal_asset": asset,
                    "plan": f"{role}_sample_01_{ordinal:02d}",
                    "object_transforms_identity": True,
                    "plan_covers_projection": True,
                    "plan_uv_transfer": {
                        "policy": "direct_physical_capture_projection",
                        "direct_uv_source": (
                            "same_blender_physical_capture_projection"
                        ),
                    },
                }
                for ordinal, asset in enumerate(assets, 1)
            ],
        }
        normalized_receipts.append({"role": role, "receipt": receipt})
        for ordinal, asset in enumerate(assets, 1):
            parts.append({
                "prototype_id": f"{role}_normalized_{ordinal:02d}",
                "role": role,
                "asset_name": asset,
                "logical_subpart_index": ordinal,
                "external_source": {
                    "kind": "send_to_unreal_normalized_skeletal_part",
                    "ordinal": ordinal,
                    "pivot_contract": "normalized_attachment_origin_0_0_0",
                },
                "bindings": [{
                    "instance": 0,
                    "transform": {
                        "fit_mode": "uniform_similarity_3d",
                        "scale": [1.25, 1.25, 1.25],
                    },
                }],
            })
            registered_variants.append({
                "role": role,
                "ordinal": ordinal,
                "card_name": f"{role}_sample_01_{ordinal:02d}",
                "skeletal_asset_name": asset,
                "pivot_contract": "normalized_attachment_origin_0_0_0",
                "instanced": True,
            })
    return {
        "status": "ready",
        "parts": parts,
        "registered_variants": registered_variants,
        "normalized_variant_receipts": normalized_receipts,
        "unit_probe_contract": {
            "kind": "speedtree_fbx_spm_unit_probe",
            "version": 1,
            "status": "verified",
            "physical_target_meters": 0.1,
            "blender_units": {
                "system": "METRIC",
                "scale_length": 1.0,
                "target_blender_units": 0.1,
            },
            "selected": {
                "mesh_geometry_scale": 1.0,
                "mesh_asset_scale": 1.0,
                "generator_scale": 1.0,
                "scale_location": "IDENTITY",
                "effective_scale": 1.0,
            },
            "generator_results": [
                {
                    "generator_type": "Frond",
                    "status": "verified",
                    "same_unit_contract": True,
                },
                {
                    "generator_type": "Leaf Mesh",
                    "status": "verified",
                    "same_unit_contract": True,
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

    def test_ready_accepts_three_independent_normalized_roles(self):
        handoff = ready_handoff()
        handoff["assembly"]["part_builder_inputs"].append({
            "role": "leaf_side",
            "role_identity": "leaf_elm_side_01",
            "assignments": [{"object": "tree", "polygon_indices": [3]}],
            "normalized_variants": {
                "status": "ready",
                "variants": [{"ordinal": 1}, {"ordinal": 2}],
            },
        })
        self.assertEqual(content_build_decision(handoff), "build")

    def test_component_fallback_is_rejected_without_normalized_variants(self):
        handoff = ready_handoff()
        handoff["assembly"]["part_builder_inputs"][1][
            "normalized_variants"
        ] = None
        with self.assertRaisesRegex(
            ClusterAssemblyBuildError,
            "component-derived Assembly parts are forbidden",
        ):
            content_build_decision(handoff)


class PhysicalProductionContractTests(unittest.TestCase):
    def test_generic_role_counts_and_uniform_similarity_contract_pass(self):
        report = validate_normalized_prototype_unit_contract(
            physical_production_manifest()
        )
        self.assertEqual(report["status"], "verified")
        self.assertEqual(report["native_prototype_count"], 5)
        self.assertEqual(
            report["roles"],
            {"branch": 2, "leaf": 1, "leaf_side": 2},
        )
        self.assertEqual(report["instance_fit"], "uniform_similarity_3d")
        self.assertFalse(report["role_specific_scale_patch"])

    def test_registered_variants_must_be_unique_and_exactly_match_parts(self):
        manifest = physical_production_manifest()
        manifest["registered_variants"].append(
            dict(manifest["registered_variants"][0])
        )
        with self.assertRaisesRegex(
            ClusterAssemblyBuildError,
            "invalid or duplicated",
        ):
            validate_normalized_prototype_unit_contract(manifest)

    def test_role_ordinals_are_derived_from_input_and_must_be_consecutive(self):
        manifest = physical_production_manifest()
        branch_two = next(
            row for row in manifest["parts"]
            if row["role"] == "branch" and row["logical_subpart_index"] == 2
        )
        branch_two["logical_subpart_index"] = 3
        branch_two["external_source"]["ordinal"] = 3
        with self.assertRaisesRegex(
            ClusterAssemblyBuildError,
            "ordinals are not consecutive from input",
        ):
            validate_normalized_prototype_unit_contract(manifest)

    def test_every_registered_variant_must_be_instanced(self):
        manifest = physical_production_manifest()
        manifest["registered_variants"][0]["instanced"] = False
        with self.assertRaisesRegex(
            ClusterAssemblyBuildError,
            "never instanced",
        ):
            validate_normalized_prototype_unit_contract(manifest)

    def test_artifact_boundary_runs_contract_before_any_file_consumption(self):
        manifest = physical_production_manifest()
        manifest["registered_variants"][0]["instanced"] = False
        with self.assertRaisesRegex(
            ClusterAssemblyBuildError,
            "never instanced",
        ):
            validate_manifest_artifacts(manifest)

    def test_role_specific_scale_patch_is_rejected(self):
        manifest = physical_production_manifest()
        manifest["parts"][0]["external_source"][
            "role_scale_multiplier"
        ] = 0.01
        with self.assertRaisesRegex(
            ClusterAssemblyBuildError,
            "role-specific scale patch is forbidden",
        ):
            validate_normalized_prototype_unit_contract(manifest)

    def test_prototype_bounds_must_fit_declared_physical_frame(self):
        manifest = physical_production_manifest()
        branch_receipt = next(
            row["receipt"] for row in manifest["normalized_variant_receipts"]
            if row["role"] == "branch"
        )
        branch_receipt["prototypes"][0]["normalized_bounds"]["size"][0] = 0.11
        with self.assertRaisesRegex(
            ClusterAssemblyBuildError,
            "physical bounds exceed",
        ):
            validate_normalized_prototype_unit_contract(manifest)

    def test_duplicate_downstream_scale_locations_are_rejected(self):
        manifest = physical_production_manifest()
        selected = manifest["unit_probe_contract"]["selected"]
        selected.update({
            "mesh_geometry_scale": 0.01,
            "mesh_asset_scale": 0.01,
            "scale_location": "FBX_GEOMETRY",
            "effective_scale": 0.0001,
        })
        with self.assertRaisesRegex(
            ClusterAssemblyBuildError,
            "duplicated across FBX geometry and SPM Mesh Scale",
        ):
            validate_normalized_prototype_unit_contract(manifest)

    def test_variant_build_metadata_supplies_expected_prototype_bounds(self):
        bounds = {
            "minimum": [-0.04, -0.045, -0.01],
            "maximum": [0.04, 0.045, 0.01],
            "size": [0.08, 0.09, 0.02],
        }
        resolved = _expected_normalized_bounds_for_variant(
            {
                "build": {
                    "variants": [{
                        "card_index": 1,
                        "skeletal_asset": "SK_branch_sample_01_01",
                        "normalized_bounds": bounds,
                    }]
                }
            },
            {
                "ordinal": 1,
                "skeletal_asset_name": "SK_branch_sample_01_01",
            },
            "SK_branch_sample_01_01",
            1,
        )
        self.assertEqual(resolved["size"], [0.08, 0.09, 0.02])

    def test_composite_prototype_bounds_override_whole_card_coverage(self):
        prototype_bounds = {"size": [0.03, 0.06, 0.01]}
        whole_card_bounds = {"size": [0.09, 0.09, 0.03]}
        resolved = _expected_normalized_bounds_for_variant(
            {
                "production_normalization": {
                    "prototypes": [{
                        "prototype_index": 1,
                        "skeletal_asset": "SK_side_subpart_01",
                        "normalized_bounds": prototype_bounds,
                    }],
                    "variants": [{
                        "card_index": 1,
                        "skeletal_asset": "SK_side_subpart_01",
                        "plan": "side_card_01",
                        "normalized_bounds": whole_card_bounds,
                    }],
                }
            },
            {
                "subpart_index": 1,
                "skeletal_asset_name": "SK_side_subpart_01",
                "normalized_bounds": prototype_bounds,
            },
            "SK_side_subpart_01",
            1,
        )
        self.assertEqual(resolved["size"], [0.03, 0.06, 0.01])

    def test_live_prototype_bounds_preflight_accepts_receipt_scale(self):
        manifest = physical_production_manifest()
        manifest["coordinate_contract"] = {
            "centimeters_per_blender_unit": 100.0,
        }
        receipt_bounds = {}
        for row in manifest["normalized_variant_receipts"]:
            for prototype in row["receipt"]["prototypes"]:
                receipt_bounds[prototype["skeletal_asset"]] = (
                    prototype["normalized_bounds"]["size"]
                )

        def fake_mesh(size_m):
            return SimpleNamespace(
                get_bounds=lambda: SimpleNamespace(
                    origin=SimpleNamespace(x=0.0, y=0.0, z=0.0),
                    box_extent=SimpleNamespace(
                        x=size_m[0] * 50.0,
                        y=size_m[1] * 50.0,
                        z=size_m[2] * 50.0,
                    ),
                )
            )

        part_assets = {
            part["prototype_id"]: fake_mesh(receipt_bounds[part["asset_name"]])
            for part in manifest["parts"]
        }
        report = validate_unreal_normalized_prototype_bounds(
            manifest,
            part_assets,
        )
        self.assertEqual(report["status"], "verified")
        self.assertTrue(report["physical_production_contract"])
        self.assertEqual(report["prototype_count"], len(manifest["parts"]))

    def test_live_prototype_bounds_preflight_rejects_stale_asset(self):
        manifest = physical_production_manifest()
        manifest["coordinate_contract"] = {
            "centimeters_per_blender_unit": 100.0,
        }
        part_assets = {}
        for part in manifest["parts"]:
            size_m = [0.08, 0.09, 0.025]
            multiplier = 66.0 if not part_assets else 1.0
            part_assets[part["prototype_id"]] = SimpleNamespace(
                get_bounds=lambda size=size_m, scale=multiplier: SimpleNamespace(
                    origin=SimpleNamespace(x=0.0, y=0.0, z=0.0),
                    box_extent=SimpleNamespace(
                        x=size[0] * 50.0 * scale,
                        y=size[1] * 50.0 * scale,
                        z=size[2] * 50.0 * scale,
                    ),
                )
            )
        with self.assertRaisesRegex(
            ClusterAssemblyBuildError,
            "regular Blender Send to Unreal",
        ):
            validate_unreal_normalized_prototype_bounds(
                manifest,
                part_assets,
            )


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
    def test_composite_transform_applies_relative_part_after_card_fit(self):
        half = math.sqrt(0.5)
        card = {
            "translation": [10.0, 0.0, 0.0],
            "rotation_xyzw": [0.0, 0.0, half, half],
            "scale": [2.0, 2.0, 2.0],
            "trs_relative_rms": 0.01,
            "affine_relative_rms": 0.01,
        }
        relative = [
            [1.0, 0.0, 0.0, 1.0],
            [0.0, 1.0, 0.0, 0.0],
            [0.0, 0.0, 1.0, 0.0],
            [0.0, 0.0, 0.0, 1.0],
        ]

        result = compose_similarity_with_relative_matrix(card, relative)

        self.assertAlmostEqual(result["translation"][0], 10.0, places=6)
        self.assertAlmostEqual(result["translation"][1], 2.0, places=6)
        self.assertAlmostEqual(result["translation"][2], 0.0, places=6)
        self.assertEqual(result["fit_mode"], "uniform_similarity_3d_composite_subpart")
        for value in result["scale"]:
            self.assertAlmostEqual(value, 2.0, places=6)

    def test_composite_transform_rejects_mirrored_relative_part(self):
        card = {
            "translation": [0.0, 0.0, 0.0],
            "rotation_xyzw": [0.0, 0.0, 0.0, 1.0],
            "scale": [1.0, 1.0, 1.0],
        }
        mirrored = [
            [-1.0, 0.0, 0.0, 0.0],
            [0.0, 1.0, 0.0, 0.0],
            [0.0, 0.0, 1.0, 0.0],
            [0.0, 0.0, 0.0, 1.0],
        ]
        with self.assertRaisesRegex(ClusterAssemblyBuildError, "mirrored"):
            compose_similarity_with_relative_matrix(card, mirrored)

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

            def __init__(self, object_type):
                self.type = object_type
                self.scale = [1.0, 1.0, 1.0]
                self.parent = None
                self.matrix_world = mock.MagicMock()
                self.matrix_world.copy.return_value = self.matrix_world
                self.matrix_world.to_scale.return_value = (1.0, 1.0, 1.0)
                self.matrix_parent_inverse = mock.MagicMock()
                self.matrix_parent_inverse.copy.return_value = (
                    self.matrix_parent_inverse
                )

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
                self.context.scene.unit_settings.system = "METRIC"
                self.context.scene.unit_settings.scale_length = 1.0
                self.ops = mock.Mock()

                def export(**kwargs):
                    target.write_bytes(b"fbx")
                    return {"FINISHED"}

                self.ops.export_scene.fbx.side_effect = export

        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "BASE_SK_Tree_elm_01.fbx"
            bpy = DummyBpy(target)
            _export_selected_fbx(
                bpy,
                target,
                [DummyObject("ARMATURE"), DummyObject("MESH")],
            )
            kwargs = bpy.ops.export_scene.fbx.call_args.kwargs

        self.assertFalse(kwargs["use_mesh_modifiers"])
        self.assertEqual(kwargs["mesh_smooth_type"], "FACE")
        self.assertFalse(kwargs["use_custom_props"])
        self.assertEqual(kwargs["primary_bone_axis"], "Y")
        self.assertEqual(kwargs["secondary_bone_axis"], "X")
        self.assertEqual(kwargs["armature_nodetype"], "NULL")
        self.assertFalse(kwargs["add_leaf_bones"])
        self.assertFalse(kwargs["bake_anim"])
        self.assertTrue(kwargs["apply_unit_scale"])
        self.assertEqual(kwargs["apply_scale_options"], "FBX_SCALE_NONE")
        self.assertEqual(kwargs["global_scale"], 1.0)
        self.assertEqual(kwargs["axis_forward"], "Y")
        self.assertEqual(kwargs["axis_up"], "Z")
        self.assertFalse(kwargs["bake_space_transform"])

    def test_ingest_plan_reuses_full_send2ue_contract_and_final_skeleton_marker(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            base = root / "SK_Tree_elm_01_NA_Base.fbx"
            branch = root / "SK_Tree_elm_01_NA_Branch_01.fbx"
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
                "full_asset_stem": "SK_Tree_elm_01",
                "full_fbx": file_fingerprint(full),
                "base": {
                    "asset_name": "SK_Tree_elm_01_NA_Base",
                    "export_stem": "SK_Tree_elm_01_NA_Base",
                    "fbx": file_fingerprint(base),
                },
                "wind_contract": {"wind_json": file_fingerprint(wind)},
                "parts": [{
                    "prototype_id": "branch_31c784af525266e7",
                    "asset_name": "SK_Tree_elm_01_NA_Branch_01",
                    "export_stem": "SK_Tree_elm_01_NA_Branch_01",
                    "role": "branch",
                    "role_identity": "branch_elm_01",
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
                "property_data": {
                    "unreal": {
                        "import_method": {
                            "fbx": {
                                "skeletal_mesh_import_data": {
                                    "convert_scene": {"value": False},
                                    "convert_scene_unit": {"value": False},
                                    "force_front_x_axis": {"value": False},
                                    "import_rotation": {
                                        "value": [0.0, 0.0, 0.0]
                                    },
                                }
                            }
                        }
                    }
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
            self.assertEqual(
                plan["asset_contract"]["base_skeletal_mesh"],
                "/Game/Codex/Tests/Elm/Assembly/SK_Tree_elm_01_NA_Base",
            )
            self.assertEqual(
                plan["asset_contract"]["parts"]["branch_31c784af525266e7"],
                "/Game/Codex/Tests/Elm/Assembly/SK_Tree_elm_01_NA_Branch_01",
            )
            self.assertNotIn(
                "/Cluster/SK_branch_elm_01",
                json.dumps(plan["asset_contract"]),
            )
            self.assertTrue(all(
                "31c784af525266e7" not in path
                for path in plan["asset_contract"]["parts"].values()
            ))
            self.assertIn(
                "/Assembly/SK_Tree_elm_01_NA_Base",
                plan["assets"][0]["pre_import_commands"][0][0],
            )
            self.assertEqual(
                plan["assets"][0]["asset_data"]["_mesh_object_name"],
                "SK_Tree_elm_01_NA_Base",
            )
            self.assertNotIn("PART_", json.dumps(plan["asset_contract"]))
            self.assertNotIn("BASE_", json.dumps(plan["asset_contract"]))
            generated_import = plan["assets"][0]["property_data"]["unreal"][
                "import_method"
            ]["fbx"]["skeletal_mesh_import_data"]
            self.assertFalse(generated_import["convert_scene"]["value"])
            self.assertFalse(generated_import["convert_scene_unit"]["value"])
            self.assertEqual(
                generated_import["import_rotation"]["value"],
                [0.0, 0.0, 0.0],
            )
            self.assertFalse(
                template["property_data"]["unreal"]["import_method"]["fbx"][
                    "skeletal_mesh_import_data"
                ]["convert_scene"]["value"]
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
                generated_payload["mesh_name"], "SK_Tree_elm_01_NA_Base"
            )
            self.assertEqual(
                generated_payload["speedtree_handoff_contract"]["mesh_name"],
                "SK_Tree_elm_01_NA_Base",
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

    def test_ingest_plan_reuses_external_normalized_send2ue_part(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            files = {
                name: root / filename
                for name, filename in {
                    "base": "SK_Tree_sample_01_NA_Base.fbx",
                    "full": "SK_Tree_sample_01.fbx",
                    "wind": "wind.json",
                    "blend": "SK_branch_sample_01.blend",
                    "plan": "branch_sample_01_01.fbx",
                    "sidecar": "SK_Tree_sample_01.json",
                }.items()
            }
            for key, path in files.items():
                path.write_bytes(b"{}" if key == "wind" else key.encode())
            files["sidecar"].write_text(
                json.dumps({
                    "mesh_name": "SK_Tree_sample_01",
                    "speedtree_handoff_contract": {
                        "kind": "speedtree",
                        "version": 1,
                        "fingerprint": "contract",
                        "asset_kind": "speedtree",
                        "mesh_name": "SK_Tree_sample_01",
                    },
                    "materials": [],
                }),
                encoding="utf-8",
            )
            manifest = {
                "status": "ready",
                "full_asset_stem": "SK_Tree_sample_01",
                "full_fbx": file_fingerprint(files["full"]),
                "base": {
                    "asset_name": "SK_Tree_sample_01_NA_Base",
                    "export_stem": "SK_Tree_sample_01_NA_Base",
                    "fbx": file_fingerprint(files["base"]),
                },
                "wind_contract": {"wind_json": file_fingerprint(files["wind"])},
                "parts": [
                    {
                        "prototype_id": "branch_normalized_01_signature_a",
                        "asset_name": "SK_branch_sample_shared_01",
                        "export_stem": "SK_branch_sample_shared_01",
                        "role": "branch",
                        "role_identity": "branch_sample_01",
                        "topology_signature": "signature_a",
                        "external_source": {
                            "kind": "send_to_unreal_normalized_skeletal_part",
                            "unreal_relative_folder": "Cluster",
                            "source_blend": file_fingerprint(files["blend"]),
                            "plan_fbx": file_fingerprint(files["plan"]),
                            "plan_name": "branch_sample_01_01",
                            "ordinal": 1,
                            "pivot_contract": "normalized_attachment_origin_0_0_0",
                            "expected_normalized_bounds": {
                                "size": [0.08, 0.09, 0.025],
                            },
                        },
                    },
                    {
                        "prototype_id": "branch_normalized_02_signature_b",
                        "asset_name": "SK_branch_sample_shared_01",
                        "export_stem": "SK_branch_sample_shared_01",
                        "role": "branch",
                        "role_identity": "branch_sample_01",
                        "topology_signature": "signature_b",
                        "external_source": {
                            "kind": "send_to_unreal_normalized_skeletal_part",
                            "unreal_relative_folder": "Cluster",
                            "source_blend": file_fingerprint(files["blend"]),
                            "plan_fbx": file_fingerprint(files["plan"]),
                            "plan_name": "branch_sample_01_02",
                            "ordinal": 2,
                            "pivot_contract": "normalized_attachment_origin_0_0_0",
                        },
                    },
                ],
            }
            template = {
                "asset_id": "full",
                "asset_data": {
                    "_asset_type": "SkeletalMesh",
                    "asset_path": "/Game/Trees/Tree_sample/SK_Tree_sample_01",
                    "file_path": str(files["full"]),
                    "_material_pipeline_json_path": str(files["sidecar"]),
                },
                "property_data": {
                    "unreal": {"import_method": {"fbx": {
                        "skeletal_mesh_import_data": {}
                    }}}
                },
                "pre_import_commands": [[]],
                "post_import_commands": [],
            }
            plan = build_unreal_ingest_plan(
                manifest,
                template,
                "/Game/Trees/Tree_sample/SK_Tree_sample_01",
                "/Game/Trees/Tree_sample",
            )
            self.assertEqual(len(plan["assets"]), 1)
            self.assertEqual(len(plan["external_assets"]), 2)
            self.assertEqual(plan["external_assets"][0]["ordinal"], 1)
            self.assertEqual(
                plan["external_assets"][0]["expected_normalized_bounds"]["size"],
                [0.08, 0.09, 0.025],
            )
            self.assertEqual(
                set(plan["asset_contract"]["parts"].values()),
                {"/Game/Trees/Tree_sample/Cluster/SK_branch_sample_shared_01"},
            )
            self.assertNotIn("Plan", json.dumps(plan))

    def test_uniform_similarity_fit_is_stable_for_planar_cards(self):
        source = [
            [-1.0, -1.0, 0.0],
            [1.0, -1.0, 0.0],
            [1.0, 1.0, 0.0],
            [-1.0, 1.0, 0.0],
        ]
        target = [
            [5.0 - point[1] * 2.5, -2.0 + point[0] * 2.5, 7.0]
            for point in source
        ]
        transform = fit_uniform_similarity_transform(source, target)
        self.assertEqual(transform["fit_mode"], "uniform_similarity_3d")
        self.assertLess(transform["trs_relative_rms"], 1.0e-10)
        self.assertEqual(transform["scale"], [2.5, 2.5, 2.5])
        self.assertAlmostEqual(transform["translation"][0], 5.0, places=8)
        self.assertAlmostEqual(transform["translation"][1], -2.0, places=8)
        self.assertAlmostEqual(transform["translation"][2], 7.0, places=8)

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

    def test_bounds_gate_rejects_the_observed_unreal_yz_axis_swap(self):
        full = {
            "origin": [113.2, 5.4, 1020.6],
            "size": [1650.1, 1534.1, 2318.3],
        }
        correct_base = {
            "origin": [90.6, 1.0, 993.5],
            "size": [1592.3, 1523.9, 2264.1],
        }
        report = validate_unreal_bounds_contract(full, correct_base, full)
        self.assertEqual(report["status"], "complete")

        sideways_base = {
            "origin": [90.6, -993.5, 1.0],
            "size": [1592.3, 2264.1, 1523.9],
        }
        with self.assertRaisesRegex(ClusterAssemblyBuildError, "Y/Z-swapped"):
            validate_unreal_bounds_contract(full, sideways_base)

    def test_bounds_gate_reports_observed_100x_error_as_absolute_unit_mismatch(self):
        full = {
            "origin": [0.0, 0.0, 0.0],
            "size": [170813.21875, 189040.46875, 172657.625],
        }
        base = {
            "origin": [0.0, 0.0, 0.0],
            "size": [1458.84, 1328.95, 2174.39],
        }
        with self.assertRaisesRegex(
            ClusterAssemblyBuildError,
            "absolute unit scale mismatch",
        ):
            validate_unreal_bounds_contract(full, base)

    def test_normalized_prototype_dominance_defers_to_final_assembly_bounds(self):
        full = {
            "origin": [113.2, 5.4, 1020.6],
            "size": [170813.21875, 189040.46875, 172657.625],
        }
        base = {
            "origin": [90.6, 1.0, 993.5],
            "size": [1458.84, 1328.95, 2174.39],
        }
        pending = validate_unreal_bounds_contract(
            full,
            base,
            allow_normalized_prototype_dominance=True,
        )
        self.assertEqual(
            pending["status"],
            "normalized_prototype_dominance_pending_final_validation",
        )
        self.assertTrue(pending["base_scale_outside_limit"])

        complete = validate_unreal_bounds_contract(
            full,
            base,
            full,
            allow_normalized_prototype_dominance=True,
        )
        self.assertEqual(complete["status"], "complete")

        with self.assertRaisesRegex(
            ClusterAssemblyBuildError,
            "do not reconstruct the Full SK",
        ):
            validate_unreal_bounds_contract(
                full,
                base,
                {
                    "origin": [0.0, 0.0, 0.0],
                    "size": [1458.84, 1328.95, 2174.39],
                },
                allow_normalized_prototype_dominance=True,
            )

    def test_bounds_completion_requires_final_assembly_to_match_full(self):
        full = {"origin": [0.0, 0.0, 1000.0], "size": [1600.0, 1500.0, 2200.0]}
        base = {"origin": [0.0, 0.0, 950.0], "size": [1550.0, 1450.0, 2100.0]}
        displaced = {
            "origin": [0.0, 0.0, 1700.0],
            "size": [1600.0, 1500.0, 3600.0],
        }
        with self.assertRaisesRegex(
            ClusterAssemblyBuildError,
            "do not reconstruct the Full SK",
        ):
            validate_unreal_bounds_contract(full, base, displaced)

    def test_unreal_plan_keeps_full_and_assembly_separate(self):
        manifest = {
            "status": "ready",
            "full_asset_stem": "SK_Elm_Full",
            "base": {
                "asset_name": "SK_Elm_Full_NA_Base",
                "export_stem": "SK_Elm_Full_NA_Base",
                "fbx": {"path": "SK_Elm_Full_NA_Base.fbx"},
            },
            "parts": [
                {
                    "prototype_id": "branch_a",
                    "role": "branch",
                    "role_identity": "branch_elm_01",
                    "asset_name": "SK_Elm_Full_NA_Branch_01",
                    "export_stem": "SK_Elm_Full_NA_Branch_01",
                    "fbx": {"path": "SK_Elm_Full_NA_Branch_01.fbx"},
                },
                {
                    "prototype_id": "leaf_a",
                    "role": "leaf",
                    "role_identity": "leaf_elm_01",
                    "asset_name": "SK_Elm_Full_NA_Leaf_01",
                    "export_stem": "SK_Elm_Full_NA_Leaf_01",
                    "fbx": {"path": "SK_Elm_Full_NA_Leaf_01.fbx"},
                },
            ],
        }
        contract = {
            "full_skeletal_mesh": "/Game/Codex/Tests/Elm/Full/SK_Elm_Full",
            "base_skeletal_mesh": "/Game/Codex/Tests/Elm/Assembly/SK_Elm_Full_NA_Base",
            "parts": {
                "branch_a": "/Game/Codex/Tests/Elm/Assembly/SK_Elm_Full_NA_Branch_01",
                "leaf_a": "/Game/Codex/Tests/Elm/Assembly/SK_Elm_Full_NA_Leaf_01",
            },
            "assembly": "/Game/Codex/Tests/Elm/Assembly/SK_Elm_Full_NaniteAssembly",
        }
        result = validate_unreal_asset_contract(manifest, contract)
        self.assertNotEqual(result["full_skeletal_mesh"], result["assembly"])

    def test_unreal_plan_requires_every_generated_prototype(self):
        manifest = {
            "status": "ready",
            "full_asset_stem": "Full",
            "base": {
                "asset_name": "Full_NA_Base",
                "export_stem": "Full_NA_Base",
                "fbx": {"path": "Full_NA_Base.fbx"},
            },
            "parts": [{
                "prototype_id": "branch_a",
                "role": "branch",
                "role_identity": "branch_elm_01",
                "asset_name": "Full_NA_Branch_01",
                "export_stem": "Full_NA_Branch_01",
                "fbx": {"path": "Full_NA_Branch_01.fbx"},
            }],
        }
        contract = {
            "full_skeletal_mesh": "/Game/Test/Full",
            "base_skeletal_mesh": "/Game/Test/Full_NA_Base",
            "parts": {},
            "assembly": "/Game/Test/Full_NaniteAssembly",
        }
        with self.assertRaisesRegex(ClusterAssemblyBuildError, "exactly match"):
            validate_unreal_asset_contract(manifest, contract)

    def test_provenance_payload_labels_native_part_order_without_copying_paths(self):
        manifest = {
            "manifest": {
                "path": "C:/handoff/assembly_manifest.json",
                "sha256": "abc123",
            },
            "registered_variants": [
                {
                    "role": "leaf_side",
                    "ordinal": ordinal,
                    "card_name": f"leaf_elm_side_01_{ordinal:02d}",
                    "skeletal_asset_name": "SK_leaf_elm_side_01_01",
                    "source_prototype_index": 1,
                    "source_partition_mode": "WHOLE_MESH",
                }
                for ordinal in range(1, 4)
            ],
            "parts": [
                {"prototype_id": "branch_a", "role": "branch"},
                {"prototype_id": "leaf_a", "role": "leaf"},
            ],
        }
        paths = {
            "full_skeletal_mesh": "/Game/Test/SK_Elm_Full",
            "base_skeletal_mesh": "/Game/Test/SK_Elm_Full_NA_Base",
            "parts": {
                "branch_a": "/Game/Test/SK_Elm_Full_NA_Branch_01",
                "leaf_a": "/Game/Test/SK_Elm_Full_NA_Leaf_01",
            },
            "assembly": "/Game/Test/SK_Elm_Full_NaniteAssembly",
        }

        payload = _build_unreal_assembly_provenance_payload(manifest, paths)

        self.assertEqual(payload["full_source"], paths["full_skeletal_mesh"])
        self.assertEqual(payload["base_source"], paths["base_skeletal_mesh"])
        self.assertEqual(payload["manifest_sha256"], "abc123")
        self.assertEqual(
            [
                {
                    "part_index": row["part_index"],
                    "role": row["role"],
                    "prototype_id": row["prototype_id"],
                }
                for row in payload["parts"]
            ],
            [
                {"part_index": 0, "role": "branch", "prototype_id": "branch_a"},
                {"part_index": 1, "role": "leaf", "prototype_id": "leaf_a"},
            ],
        )
        self.assertEqual(
            [
                (
                    row["card_name"],
                    row["skeletal_asset_name"],
                    row["source_prototype_index"],
                    row["source_partition_mode"],
                )
                for row in payload["registered_variants"]
            ],
            [
                (
                    f"leaf_elm_side_01_{ordinal:02d}",
                    "SK_leaf_elm_side_01_01",
                    1,
                    "WHOLE_MESH",
                )
                for ordinal in range(1, 4)
            ],
        )
        self.assertNotIn("part_asset_paths", payload)

    def test_normalized_external_parts_collapse_by_asset_and_sort_logically(self):
        def part(role, ordinal, binding_count, signature):
            asset_role = {
                "branch": "branch_elm_01",
                "leaf": "leaf_elm_01",
                "leaf_side": "leaf_elm_side_01",
            }[role]
            asset_name = f"SK_{asset_role}_{ordinal:02d}"
            return {
                "prototype_id": f"{role}_{signature}",
                "asset_name": asset_name,
                "export_stem": asset_name,
                "role": role,
                "role_identity": f"M_{asset_role}",
                "topology_signature": signature,
                "external_source": {
                    "kind": "send_to_unreal_normalized_skeletal_part",
                    "unreal_relative_folder": "Cluster",
                    "ordinal": ordinal,
                    "pivot_contract": "normalized_attachment_origin_0_0_0",
                },
                "template": {},
                "bindings": [
                    {
                        "instance": index,
                        "transform": {
                            "trs_relative_rms": 0.0,
                            "affine_relative_rms": 0.0,
                        },
                    }
                    for index in range(binding_count)
                ],
            }

        parts = [
            part("branch", 2, 3, "branch02_b"),
            part("leaf_side", 2, 71, "side02"),
            part("branch", 3, 75, "branch03"),
            part("branch", 2, 5, "branch02_a"),
            part("leaf", 1, 884, "leaf01"),
            part("leaf_side", 1, 214, "side01"),
            part("branch", 1, 75, "branch01"),
            part("leaf_side", 3, 143, "side03"),
        ]

        collapsed = _coalesce_normalized_external_parts(parts)

        self.assertEqual(
            [row["asset_name"] for row in collapsed],
            [
                "SK_branch_elm_01_01",
                "SK_branch_elm_01_02",
                "SK_branch_elm_01_03",
                "SK_leaf_elm_01_01",
                "SK_leaf_elm_side_01_01",
                "SK_leaf_elm_side_01_02",
                "SK_leaf_elm_side_01_03",
            ],
        )
        self.assertEqual(
            [len(row["bindings"]) for row in collapsed],
            [75, 8, 75, 884, 214, 71, 143],
        )
        branch_two = collapsed[1]
        self.assertEqual(
            branch_two["source_topology_signatures"],
            ["branch02_b", "branch02_a"],
        )
        self.assertEqual(
            [row["instance"] for row in branch_two["bindings"]],
            list(range(8)),
        )
        self.assertEqual(
            [
                (row["logical_group_index"], row["logical_subpart_index"])
                for row in collapsed
            ],
            [(0, 1), (0, 2), (0, 3), (1, 1), (2, 1), (2, 2), (2, 3)],
        )

    def test_provenance_aggregates_topology_groups_for_one_native_part(self):
        manifest = {
            "parts": [
                {
                    "prototype_id": f"side_{index}",
                    "role": "leaf_side",
                    "asset_name": "SK_leaf_elm_side_01_01",
                    "external_source": {
                        "plan_name": f"leaf_elm_side_01_0{index}",
                        "ordinal": index,
                        "pivot_contract": "normalized_attachment_origin_0_0_0",
                    },
                    "bindings": [{} for _ in range(index)],
                }
                for index in (1, 2, 3)
            ],
        }
        shared_path = "/Game/Test/Cluster/SK_leaf_elm_side_01_01"
        paths = {
            "full_skeletal_mesh": "/Game/Test/SK_Elm_Full",
            "base_skeletal_mesh": "/Game/Test/SK_Elm_Full_NA_Base",
            "parts": {
                f"side_{index}": shared_path for index in (1, 2, 3)
            },
            "assembly": "/Game/Test/SK_Elm_Full_NaniteAssembly",
        }

        payload = _build_unreal_assembly_provenance_payload(manifest, paths)

        self.assertEqual(len(payload["parts"]), 1)
        part = payload["parts"][0]
        self.assertEqual(part["role"], "leaf_side")
        self.assertEqual(part["asset_name"], "SK_leaf_elm_side_01_01")
        self.assertEqual(part["expected_instance_count"], 6)
        self.assertEqual(part["ordinal"], 0)
        self.assertEqual(
            part["card_name"],
            "leaf_elm_side_01_01, leaf_elm_side_01_02, leaf_elm_side_01_03",
        )

    def test_provenance_exposes_role_group_and_composite_subpart(self):
        manifest = {
            "parts": [
                {
                    "prototype_id": f"side_subpart_{index}",
                    "role": "leaf_side",
                    "asset_name": f"SK_leaf_elm_side_01_{index:02d}",
                    "logical_group_index": 2,
                    "logical_subpart_index": index,
                    "external_source": {
                        "ordinal": 1,
                        "card_ordinals": [1, 2, 3],
                        "pivot_contract": "normalized_attachment_origin_0_0_0",
                    },
                    "bindings": [{} for _ in range(428)],
                }
                for index in (1, 2)
            ],
        }
        paths = {
            "full_skeletal_mesh": "/Game/Test/SK_Elm_Full",
            "base_skeletal_mesh": "/Game/Test/SK_Elm_Full_NA_Base",
            "parts": {
                f"side_subpart_{index}": f"/Game/Test/Cluster/SK_leaf_elm_side_01_{index:02d}"
                for index in (1, 2)
            },
            "assembly": "/Game/Test/SK_Elm_Full_NaniteAssembly",
        }

        payload = _build_unreal_assembly_provenance_payload(manifest, paths)

        self.assertEqual(
            [
                (
                    row["group_index"],
                    row["subpart_ordinal"],
                    row["card_ordinals"],
                    row["expected_instance_count"],
                )
                for row in payload["parts"]
            ],
            [
                (2, 1, [1, 2, 3], 428),
                (2, 2, [1, 2, 3], 428),
            ],
        )


class CodexTestMaterialScopeTests(unittest.TestCase):
    def test_production_destination_preserves_manifest_and_sidecar(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sidecar = root / "full.json"
            sidecar.write_text(
                '{"materials": [{"name": "M_leaf_elm_01"}]}',
                encoding="utf-8",
            )
            assets = [
                {
                    "asset_data": {
                        "asset_path": "/Game/Meshes/Tree/Full",
                        "_material_pipeline_json_path": str(sidecar),
                    },
                    "pre_import_commands": [["original pre-import command"]],
                    "post_import_commands": [["original post-import command"]],
                }
            ]
            before_assets = json.loads(json.dumps(assets))
            before_sidecar = sidecar.read_bytes()
            output = root / "out"

            report = scope_material_pipeline_for_destination(
                assets,
                "/Game/Meshes/Tree/",
                output,
            )

            self.assertEqual(report["status"], "production_preserved")
            self.assertTrue(report["production_materials_preserved"])
            self.assertEqual(assets, before_assets)
            self.assertEqual(sidecar.read_bytes(), before_sidecar)
            self.assertFalse(output.exists())

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
            report = scope_material_pipeline_for_destination(
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
