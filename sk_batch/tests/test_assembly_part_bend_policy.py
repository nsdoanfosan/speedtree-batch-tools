from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch


SK_BATCH = Path(__file__).resolve().parents[1]
if str(SK_BATCH) not in sys.path:
    sys.path.insert(0, str(SK_BATCH))

from assembly_part_bend_policy import (
    DEFAULT_SCALARS, ELM_ASSEMBLIES, ELM_PROTOTYPES, PRODUCTION_OWNER,
    SOURCE_MATERIALS, UV_WRITER, VARIANT_MATERIALS, elm_leaf_role,
    normalization_part_bend_policy, part_bend_build_matches_policy,
    production_assembly_bend_policy,
)
import nanite_assembly_materials as materials


class PolicyTests(unittest.TestCase):
    def test_auto_applies_only_exact_elm_species_and_leaf_roles(self):
        for name in ("SK_leaf_elm_01", "leaf_elm_03"):
            self.assertEqual(elm_leaf_role(name), "leaf")
        for name in ("SK_leaf_elm_side_01", "SK_leaf_side_elm_01"):
            self.assertEqual(elm_leaf_role(name), "leaf_side")
        for name in ("SK_branch_elm_01", "SK_leaf_oak_01", "SK_leaf_elmwood_01",
                     "SK_leaf_red_elm_01", "SK_Tree_elm_01"):
            self.assertIsNone(normalization_part_bend_policy(name))
        self.assertEqual(DEFAULT_SCALARS["AssemblyBranchIndependence"], 0.75)
        self.assertEqual(DEFAULT_SCALARS["AssemblyBranchSpeed"], 0.025)

    @staticmethod
    def valid_build(policy):
        payload = {key: deepcopy(policy[key]) for key in (
            "schema_version", "role", "role_code", "uv_name", "uv_index",
            "blender_encoding", "unreal_encoding", "growth_axis_blender",
            "growth_axis_unreal", "anchor_local")}
        payload.update({"vertex_count": 12, "loop_count": 30,
                        "protected_uv_sha256": {str(i): "a" * 64 for i in range(3)}})
        return {"part_bend_role": policy["role"], "prototype_count": 1,
                "prototypes": [{"part_bend_payload": payload}]}

    def test_old_or_corrupted_receipt_cannot_claim_delivered_uv3(self):
        policy = normalization_part_bend_policy("SK_leaf_elm_01")
        valid = self.valid_build(policy)
        self.assertTrue(part_bend_build_matches_policy(valid, policy))
        self.assertFalse(part_bend_build_matches_policy({}, policy))
        corruptions = [
            lambda x: x["prototypes"][0]["part_bend_payload"].update(uv_index=2),
            lambda x: x["prototypes"][0]["part_bend_payload"].update(role_code=2),
            lambda x: x["prototypes"][0]["part_bend_payload"].update(protected_uv_sha256={}),
            lambda x: x.update(prototype_count=2),
        ]
        for corrupt in corruptions:
            candidate = deepcopy(valid)
            corrupt(candidate)
            self.assertFalse(part_bend_build_matches_policy(candidate, policy))

    def test_root_scope_and_original_mapping_are_stable(self):
        for root in ELM_ASSEMBLIES:
            policy = production_assembly_bend_policy(root + "." + root.rsplit("/", 1)[-1])
            self.assertEqual(policy["prototypes"], list(ELM_PROTOTYPES))
            self.assertEqual(policy["preserved_branch_parts"], [0, 1, 2])
            self.assertEqual(policy["source_materials"], list(SOURCE_MATERIALS))
            self.assertEqual(policy["variant_materials"], list(VARIANT_MATERIALS))
        self.assertIsNone(production_assembly_bend_policy("/Game/Trees/SK_Tree_elm_01"))
        self.assertIsNone(production_assembly_bend_policy(ELM_ASSEMBLIES[0].replace("elm", "oak")))


class FakeAsset:
    def __init__(self, path):
        self.path = path
        self.metadata = {}
        self.modifications = 0

    def get_path_name(self):
        return self.path

    def modify(self):
        self.modifications += 1
        return True


class FakeSlot:
    def __init__(self, index, interface):
        self.values = {"material_interface": interface,
                       "material_slot_name": f"M_slot_{index}",
                       "imported_material_slot_name": f"M_slot_{index}"}

    def get_editor_property(self, name):
        return self.values[name]


class FakeSettings:
    def __init__(self, paths, remaps):
        self.paths, self.remaps = paths, remaps

    def export_text(self):
        return ",".join('(MeshObjectPath="' + path + '",MaterialRemap=(' +
                        ",".join(map(str, values)) + '))'
                        for path, values in zip(self.paths, self.remaps))


class FakeMesh(FakeAsset):
    def __init__(self, path, slots, settings=None):
        super().__init__(path)
        self.values = {"materials": slots, "nanite_settings": settings, "asset_import_data": None}

    def get_editor_property(self, name):
        return self.values[name]

    def get_outermost(self):
        return self


class NativePolicyTests(unittest.TestCase):
    def fixture(self):
        assets = {path: FakeAsset(path) for path in SOURCE_MATERIALS + VARIANT_MATERIALS}
        slots = lambda: [FakeSlot(i, assets[path]) for i, path in enumerate(SOURCE_MATERIALS)]
        branch = [f"/Game/Elm/branch_{i}" for i in range(3)]
        paths = branch + list(ELM_PROTOTYPES)
        remaps = [[0, 1, 2] for _ in paths]
        root = FakeMesh(ELM_ASSEMBLIES[0], slots(), FakeSettings(paths, remaps))
        assets[root.path] = root
        for path in paths:
            mesh = FakeMesh(path, slots())
            mesh.metadata["CodexElmAssemblyBendUv3Writer"] = UV_WRITER
            assets[path] = mesh
        events = []

        def verify(path, role):
            events.append(("verify", path, role))
            return json.dumps({"ok": True, "changed": False,
                               "production": True, "writer": UV_WRITER,
                               "production_uv3_mode": "verify_imported_payload", "uv_channel_count_before": 4,
                               "all_existing_mesh_description_attributes_preserved": True,
                               "protected_mesh_description_sha1_before": "a" * 40,
                               "protected_mesh_description_sha1_after": "a" * 40}), []

        def configure(path, prototypes, finalize, variants):
            self.assertEqual(path, root.path)
            self.assertEqual(prototypes, list(ELM_PROTOTYPES))
            self.assertEqual(variants, list(VARIANT_MATERIALS))
            self.assertTrue(finalize)
            self.assertEqual([row[0] for row in events], ["verify"] * 4)
            events.append(("configure", path))
            changed = len(root.values["materials"]) == 3
            if changed:
                root.values["materials"].extend(FakeSlot(i, assets[m]) for i, m in enumerate(variants))
                root.values["nanite_settings"].remaps[3:] = [[3, 4, 5] for _ in range(4)]
            return json.dumps({"ok": True, "changed": changed,
                               "payload_checked_prototype_count": 4,
                               "payload_verified_before_material_mutation": True,
                               "production_prototype_materials_directly_applied": True,
                               "root_source_mesh_description_preserved": True}), []

        unreal = types.SimpleNamespace(
            SkeletalMesh=FakeMesh,
            EditorAssetLibrary=types.SimpleNamespace(
                load_asset=lambda path: assets.get(path),
                get_metadata_tag=lambda asset, key: asset.metadata.get(key, ""),
                set_metadata_tag=lambda asset, key, value: asset.metadata.__setitem__(key, value),
            ),
            EditorLoadingAndSavingUtils=types.SimpleNamespace(get_dirty_content_packages=lambda: []),
            CodexAssemblyPartBendUvLibrary=types.SimpleNamespace(
                append_test_prototype_uv3=verify, configure_test_assembly_parts=configure),
        )
        return unreal, root, assets, events

    def test_all_payloads_verified_before_remap_and_original_indices_preserved(self):
        unreal, root, assets, events = self.fixture()
        original_slots = list(root.values["materials"])
        original_branch_remaps = deepcopy(root.values["nanite_settings"].remaps[:3])
        with patch.object(materials, "audit_unreal_skeletal_mesh_material_sections",
                          side_effect=lambda _u, path, count: {"status": "ok", "mesh": path, "slot_count": count}):
            result = materials.normalize_unreal_nanite_assembly_materials(unreal, root, apply=True)
        self.assertEqual(root.values["materials"][:3], original_slots)
        self.assertEqual(root.values["nanite_settings"].remaps[:3], original_branch_remaps)
        self.assertEqual([row[0] for row in events], ["verify"] * 4 + ["configure"])
        self.assertEqual(result["after_material_count"], 6)
        self.assertEqual(len(result["part_section_audits"]), 7)
        self.assertEqual(root.metadata["CodexElmAssemblyBendProductionOptIn"], PRODUCTION_OWNER)
        self.assertEqual(root.modifications, 1)
        self.assertTrue(all(assets[path].modifications == 2 for path in ELM_PROTOTYPES))

    def test_missing_mi_fails_before_any_metadata_or_payload_operation(self):
        unreal, root, assets, events = self.fixture()
        del assets[VARIANT_MATERIALS[1]]
        with self.assertRaisesRegex(materials.NaniteAssemblyMaterialError, "material is missing"):
            materials.normalize_unreal_nanite_assembly_materials(unreal, root, apply=True)
        self.assertEqual(events, [])
        self.assertEqual(root.metadata, {})
        self.assertEqual(len(root.values["materials"]), 3)

    def test_native_bad_payload_stops_before_material_mutation(self):
        unreal, root, _assets, events = self.fixture()
        unreal.CodexAssemblyPartBendUvLibrary.append_test_prototype_uv3 = (
            lambda path, role: (json.dumps({"ok": False, "errors": ["UV3 source payload mismatch"]}), []))
        with self.assertRaisesRegex(materials.NaniteAssemblyMaterialError, "imported UV3 verification failed"):
            materials.normalize_unreal_nanite_assembly_materials(unreal, root, apply=True)
        self.assertEqual(events, [])
        self.assertEqual(len(root.values["materials"]), 3)

    def test_audit_mode_never_writes_metadata_or_calls_native_authoring(self):
        unreal, root, assets, events = self.fixture()
        snapshots = {path: deepcopy(asset.metadata) for path, asset in assets.items()}
        with patch.object(materials, "audit_unreal_skeletal_mesh_material_sections",
                          return_value={"status": "ok"}):
            result = materials.normalize_unreal_nanite_assembly_materials(unreal, root, apply=False)
        self.assertTrue(result["would_change"])
        self.assertFalse(result["changed"])
        self.assertEqual(events, [])
        self.assertEqual(snapshots, {path: asset.metadata for path, asset in assets.items()})

    def test_success_without_native_preservation_proof_cannot_bless_uv3(self):
        unreal, root, _assets, events = self.fixture()
        unreal.CodexAssemblyPartBendUvLibrary.append_test_prototype_uv3 = (
            lambda path, role: (json.dumps({"ok": True, "changed": False}), []))
        with self.assertRaisesRegex(materials.NaniteAssemblyMaterialError, "proof is incomplete"):
            materials.normalize_unreal_nanite_assembly_materials(unreal, root, apply=True)
        self.assertEqual(events, [])
        self.assertEqual(len(root.values["materials"]), 3)


if __name__ == "__main__":
    unittest.main()
