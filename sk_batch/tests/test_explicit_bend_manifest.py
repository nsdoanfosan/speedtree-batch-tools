"""Independent authoring roles reach native plans without changing Atlas roles."""
from copy import deepcopy
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

SK_BATCH = Path(__file__).resolve().parents[1]
if str(SK_BATCH) not in sys.path:
    sys.path.insert(0, str(SK_BATCH))
import cluster_assembly_builder as builder
import normalized_assembly_bend as bend
from assembly_part_bend_policy import normalization_part_bend_policy
import cluster_blend_sync as sync
from tests import test_cluster_normalization_sync as fixtures
from tests.test_cluster_relation_job_contract import load_job_module
from sk_batch.tests import test_assembly_part_bend_policy as material_fixtures
import nanite_assembly_materials as materials


def normalized_contract(names, role="cluster"):
    policy = normalization_part_bend_policy(names[0], role=role or "NONE")
    bounds = {"minimum": [-1, 0, 0], "maximum": [1, 8, 0], "size": [2, 8, 0], "center": [0, 4, 0]}
    prototypes = []
    for index, name in enumerate(names, 1):
        payload = None if policy is None else {**deepcopy(policy), "vertex_count": 12, "loop_count": 30,
            "normalized_bounds": {key: deepcopy(bounds[key]) for key in ("minimum", "maximum", "size")}, "aspect_ratio": 4, "aspect_gain": 4,
            "aspect_correction": 2, "protected_uv_sha256": {str(i): "a" * 64 for i in range(3)}}
        prototypes.append({"prototype_index": index, "skeletal_asset": name,
            "normalized_bounds": deepcopy(bounds), "part_bend_payload": payload})
    return {"status": "ready", "source_blend": {"path": "D:/Sources/CustomFlow.blend",
        "exists": True, "sha256": "b" * 64, "size": 123},
        "source_3d_artifacts": {"source_fbx": {"sha256": "c" * 64}},
        "production_normalization": {"part_bend_role": role, "prototype_count": len(prototypes),
            "prototypes": prototypes, "workflow_mode": "PHYSICAL_DIRECT_CAPTURE",
            "physical_capture_contract_sha256": "d" * 64,
            "physical_capture_contract": {"contract_sha256": "d" * 64}}}


def manifest_part(contract, name, index=1, capture_role="branch"):
    prototype = contract["production_normalization"]["prototypes"][index - 1]
    variant = {"source_prototype_index": index, "normalized_bounds": deepcopy(prototype["normalized_bounds"])}
    external = {"kind": "send_to_unreal_normalized_skeletal_part",
        "pivot_contract": "normalized_attachment_origin_0_0_0",
        "source_prototype_index": index, "source_blend": deepcopy(contract["source_blend"]),
        "expected_normalized_bounds": deepcopy(prototype["normalized_bounds"])}
    external.update(builder._normalized_part_bend_metadata(contract, variant, name))
    return {"prototype_id": name, "asset_name": name, "role": capture_role, "external_source": external}


def plan(parts):
    mapping = {part["prototype_id"]: "/Game/Trees/Cluster/" + part["asset_name"] for part in parts}
    return bend.plan_manifest_parts({"parts": parts}, {"parts": mapping}, [{"mesh": path} for path in mapping.values()])


class ExplicitManifestTests(unittest.TestCase):
    def test_noncanonical_recipe_worker_prototype_manifest_native_plan(self):
        with tempfile.TemporaryDirectory() as temporary:
            blend, _source, target, probe = fixtures.ClusterNormalizationSyncTests().fixture(temporary, "SK_CustomFlow")
            recipe, rebuilt = sync._resolve_normalization_recipe_with_source_rebuild(
                blend, [target], blender_exe="unused", unit_probe_path=probe,
                capture_resolution=1024, progress_callback=None, part_bend_role="cluster")
            self.assertIsNone(rebuilt)
            props = SimpleNamespace(part_bend_role="NONE")
            load_job_module().configure_normalized_part_bend_role(props, recipe)
            names = ["SK_CustomFlow_%02d" % index for index in range(1, 5)]
            contract = normalized_contract(names, props.part_bend_role)
            parts = [manifest_part(contract, name, index) for index, name in enumerate(names, 1)]
            before = deepcopy(parts)
            native = plan(parts)
            self.assertEqual(parts, before)
            self.assertEqual(recipe["role"], "branch")
            self.assertEqual(recipe["plan_collection"], "Atlas_Branch_Plans")
            self.assertEqual([part["source_role"] for part in native], ["branch"] * 4)
            self.assertEqual([part["bend_role"] for part in native], ["cluster"] * 4)
            self.assertEqual([part["role"] for part in native], [1] * 4)
            self.assertEqual([part["external_source"]["part_bend_source_proof"]["prototype_index"] for part in parts], [1, 2, 3, 4])

    def test_explicit_none_disables_canonical_leaf_without_changing_capture_role(self):
        contract = normalized_contract(["SK_leaf_willow_01_01"], None)
        part = manifest_part(contract, "SK_leaf_willow_01_01", capture_role="leaf")
        native = plan([part])[0]
        self.assertEqual((native["source_role"], native["bend_role"], native["role"]), ("leaf", "NONE", 0))
        self.assertIsNone(part["external_source"]["part_bend_payload"])
        stale = deepcopy(contract)
        stale["production_normalization"]["prototypes"][0].pop("part_bend_payload")
        with self.assertRaisesRegex(builder.ClusterAssemblyBuildError, "disabled"):
            manifest_part(stale, "SK_leaf_willow_01_01", capture_role="leaf")

    def test_legacy_metadata_absence_keeps_existing_willow_and_branch_plans(self):
        contract = normalized_contract(["SK_leaf_willow_01_01"])
        contract["production_normalization"].pop("part_bend_role")
        part = manifest_part(contract, "SK_leaf_willow_01_01", capture_role="leaf")
        self.assertFalse(any(key.startswith("part_bend_") for key in part["external_source"]))
        self.assertEqual(plan([part])[0]["role"], 1)
        part["role"] = "branch"
        self.assertEqual(plan([part])[0]["role"], 0)

    def test_wrong_prototype_and_stale_payload_are_rejected_by_builder(self):
        contract = normalized_contract(["SK_CustomFlow_01", "SK_CustomFlow_02"])
        for mutation in (lambda value: value["production_normalization"]["prototypes"][1]["part_bend_payload"].update(schema_version=1),
                         lambda value: value["production_normalization"]["prototypes"][0].update(prototype_index=2),
                         lambda value: value["production_normalization"]["prototypes"][0].update(skeletal_asset="SK_Other"),
                         lambda value: value["source_blend"].update(sha256="")):
            bad = deepcopy(contract)
            mutation(bad)
            with self.assertRaises(builder.ClusterAssemblyBuildError):
                manifest_part(bad, "SK_CustomFlow_01")

    def test_source_schema_pivot_policy_bounds_and_semantic_conflicts_fail_before_native_writes(self):
        part = manifest_part(normalized_contract(["SK_CustomFlow_01"]), "SK_CustomFlow_01")
        for mutation in (lambda source: source.pop("part_bend_policy"),
                         lambda source: source.update(kind="raw_source"),
                         lambda source: source.update(pivot_contract="centered_bbox"),
                         lambda source: source["part_bend_source_proof"].update(schema_version=1),
                         lambda source: source["part_bend_source_proof"].update(prototype_asset="SK_Other"),
                         lambda source: source["part_bend_source_proof"]["source_blend"].update(sha256="e" * 64),
                         lambda source: source["part_bend_policy"].update(schema_version=1),
                         lambda source: source["part_bend_policy"].update(role_code=True),
                         lambda source: source["part_bend_payload"].update(role_code=True),
                         lambda source: source["expected_normalized_bounds"]["size"].__setitem__(1, 7),
                         lambda source: source.update(part_bend_role="unknown")):
            bad = deepcopy(part)
            mutation(bad["external_source"])
            with self.assertRaises(bend.NormalizedAssemblyBendError):
                plan([bad])
        other = manifest_part(normalized_contract(["SK_CustomFlow_01"], "leaf"), "SK_CustomFlow_01")
        mapping = {part["prototype_id"]: "/Game/Trees/Cluster/SK_CustomFlow_01"}
        with self.assertRaisesRegex(bend.NormalizedAssemblyBendError, "conflicting"):
            bend.plan_manifest_parts({"parts": [part, other]}, {"parts": mapping}, [{"mesh": next(iter(mapping.values()))}])

    def test_exact_canonical_elm_all_none_never_reactivates_legacy_wind(self):
        unreal, root, assets, events = material_fixtures.NativePolicyTests().fixture()
        paths = root.values["nanite_settings"].paths
        names = [path.rsplit("/", 1)[-1] for path in paths[3:]]
        contract = normalized_contract(names, None)
        parts = [{"prototype_id": path, "asset_name": path.rsplit("/", 1)[-1], "role": "branch"} for path in paths[:3]]
        parts += [manifest_part(contract, name, index, "leaf" if index == 1 else "leaf_side")
                  for index, name in enumerate(names, 1)]
        mapping = {part["prototype_id"]: path for part, path in zip(parts, paths)}
        original_slots = list(root.values["materials"])
        original_remaps = deepcopy(root.values["nanite_settings"].remaps)
        with patch.object(materials, "_normalize_elm_assembly_bend") as legacy, \
             patch.object(materials, "audit_unreal_skeletal_mesh_material_sections", return_value={"status": "ok"}):
            result = materials.normalize_unreal_nanite_assembly_materials(unreal, root,
                manifest={"parts": parts}, asset_contract={"assembly": root.path, "parts": mapping})
        legacy.assert_not_called()
        self.assertNotIn("elm_part_bend_policy", result)
        self.assertFalse(result["would_change"])
        self.assertEqual(root.values["materials"], original_slots)
        self.assertEqual(root.values["nanite_settings"].remaps, original_remaps)
        self.assertEqual(events, [])
        self.assertTrue(all(asset.modifications == 0 for asset in assets.values()))

    def test_manual_canonical_elm_without_descriptor_retains_legacy_hook(self):
        unreal, root, _assets, _events = material_fixtures.NativePolicyTests().fixture()
        with patch.object(materials, "_normalize_elm_assembly_bend", return_value={"legacy": True}) as legacy:
            result = materials.normalize_unreal_nanite_assembly_materials(unreal, root)
        self.assertEqual(result, {"legacy": True})
        legacy.assert_called_once()

    def test_metadata_absent_willow_branch_manifest_keeps_plain_material_path(self):
        unreal, root, _assets, events = material_fixtures.NativePolicyTests().fixture()
        root.path = "/Game/Trees/Willow/Assembly/SK_Willow_NaniteAssembly"
        paths = root.values["nanite_settings"].paths
        parts = [{"prototype_id": path, "role": "branch"} for path in paths]
        with patch.object(materials, "_normalize_elm_assembly_bend") as legacy, \
             patch.object(materials, "audit_unreal_skeletal_mesh_material_sections", return_value={"status": "ok"}):
            result = materials.normalize_unreal_nanite_assembly_materials(unreal, root,
                manifest={"parts": parts}, asset_contract={"assembly": root.path, "parts": {path: path for path in paths}})
        legacy.assert_not_called()
        self.assertFalse(result["would_change"])
        self.assertEqual(events, [])


if __name__ == "__main__":
    unittest.main()
