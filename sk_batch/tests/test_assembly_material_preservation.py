from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

SK_BATCH = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SK_BATCH))
import cluster_assembly_builder as builder
import nanite_assembly_materials as materials
import assembly_part_notess as notess


def slot(name, material):
    return {"slot_name": name, "imported_slot_name": name, "material": material}


class Material:
    def __init__(self, path):
        self.path = path
        self.parameters = {"vector": [0.2, 0.3, 0.4], "texture": "original"}

    def get_path_name(self):
        return self.path + "." + self.path.rsplit("/", 1)[-1]


class MIC(Material):
    def __init__(self, path, parent):
        super().__init__(path)
        self.parent = parent

    def get_editor_property(self, key):
        return getattr(self, key)


class Slot:
    registry = {}

    def __init__(self, name=None, interface=None):
        self.values = {"material_slot_name": name, "imported_material_slot_name": name,
                       "material_interface": interface, "uv_channel_data": [1.5, 2.5]}

    def get_editor_property(self, key):
        return self.values[key]

    def set_editor_property(self, key, value):
        self.values[key] = value

    def export_text(self):
        return json.dumps({**self.values, "material_interface": self.values["material_interface"].path})

    def import_text(self, text):
        self.values = json.loads(text)
        self.values["material_interface"] = self.registry[self.values["material_interface"]]


class Settings:
    def __init__(self, text=""):
        self.text = text

    def export_text(self):
        return self.text

    def import_text(self, text):
        self.text = text


class Mesh:
    def __init__(self, path, slots, parts=()):
        self.path = path
        self.values = {"materials": slots, "nanite_settings": Settings(
            "(ProtectedGeometry=Keep,Parts=(" + ",".join(
                '(MeshObjectPath="' + mesh + '.' + mesh.rsplit('/', 1)[-1] + '",MaterialRemap=(' + ','.join(map(str, remap)) + '))'
                for mesh, remap in parts) + "),ProtectedBindings=Keep)")}
        self.modify_count = 0

    def get_path_name(self):
        return self.path + "." + self.path.rsplit("/", 1)[-1]

    def get_editor_property(self, key):
        return self.values[key]

    def set_editor_property(self, key, value):
        self.values[key] = value

    def modify(self):
        self.modify_count += 1

    def get_outermost(self):
        return SimpleNamespace(get_path_name=lambda: self.path)


class AssemblyMaterialPreservationTests(unittest.TestCase):
    def fixture(self):
        master = Material("/Game/M/M_Master")
        common = MIC("/Game/M/MI_Common", master)
        tint = MIC("/Game/M/MI_Common_Tint", common)
        bark = MIC("/Game/M/MI_Bark", master)
        leaf = MIC("/Game/M/MI_Leaf", master)
        registry = {row.path: row for row in (master, common, tint, bark, leaf)}
        base = Mesh("/Game/Trees/SK_Any_NA_Base", [Slot("Common", common), Slot("Bark", bark)])
        branch = Mesh("/Game/Trees/SK_CustomBranch", [Slot("Common", common), Slot("Bark", bark)])
        foliage = Mesh("/Game/Trees/SK_CustomLeaf", [Slot("Bark", bark), Slot("Leaf", leaf)])
        old = Mesh("/Game/Trees/SK_Any_Assembly", [Slot("Common", tint), Slot("Bark", bark), Slot("Leaf", leaf)],
                   [(branch.path, [0, 1]), (foliage.path, [1, 2])])
        registry.update({mesh.path: mesh for mesh in (base, branch, foliage, old)})
        Slot.registry = registry
        library = SimpleNamespace(load_asset=lambda path: registry.get(path.split(".", 1)[0]),
                                  get_metadata_tag=lambda _asset, _key: "")
        unreal = SimpleNamespace(EditorAssetLibrary=library, Material=Material, MaterialInstanceConstant=MIC,
                                 SkeletalMesh=Mesh)
        # MIC and Material are disjoint reflected classes in Unreal.
        unreal.Material = type("RootMaterial", (), {})
        master.__class__ = unreal.Material
        unreal.Material.get_path_name = Material.get_path_name
        ordered = [{"mesh": branch.path, "asset": branch, "bend_role": 0},
                   {"mesh": foliage.path, "asset": foliage, "bend_role": 1}]
        return unreal, registry, base, branch, foliage, old, ordered

    def test_canonical_prototype_matches_exact_original_source_during_reimport(self):
        unreal, registry, base, branch, foliage, old, ordered = self.fixture()
        source = registry['/Game/M/MI_Leaf']
        canonical = MIC(notess.destination(source), source.parent)
        registry[canonical.path] = canonical
        foliage.values['materials'][1].values['material_interface'] = canonical
        tags = {notess.OWNER_TAG: notess.OWNER, notess.SOURCE_TAG: source.path.swapcase()}
        unreal.EditorAssetLibrary.get_metadata_tag = lambda asset, key: tags.get(key, '') if asset is canonical else ''
        unreal.EditorAssetLibrary.load_asset = lambda path: next(
            (asset for key, asset in registry.items() if notess.identity(key) == notess.identity(path)), None)
        before = [row.export_text() for row in foliage.values['materials']]
        plan = builder.plan_unreal_assembly_material_preservation(unreal, old.path, base, ordered)
        self.assertEqual(plan['source_materials'], ['/Game/M/MI_Common_Tint', '/Game/M/MI_Bark', source.path])
        self.assertEqual([row['desired_remap'] for row in plan['parts']], [[0, 1], [1, 2]])
        self.assertNotIn(canonical.path, plan['source_materials'])
        self.assertEqual(before, [row.export_text() for row in foliage.values['materials']])
        self.assertEqual(foliage.modify_count, 0)

    def test_readonly_snapshot_preserves_root_and_existing_branch_effective_tint(self):
        unreal, registry, base, branch, foliage, old, ordered = self.fixture()
        before = {key: (value.get_editor_property("nanite_settings").export_text(),
                       [slot.export_text() for slot in value.get_editor_property("materials")])
                  for key, value in registry.items() if isinstance(value, Mesh)}
        plan = builder.plan_unreal_assembly_material_preservation(unreal, old.path, base, ordered)
        self.assertEqual(plan["source_materials"], ["/Game/M/MI_Common_Tint", "/Game/M/MI_Bark", "/Game/M/MI_Leaf"])
        self.assertEqual([row["desired_remap"] for row in plan["parts"]], [[0, 1], [1, 2]])
        self.assertEqual([row["scope"] for row in plan["overrides"]], ["base", branch.path])
        self.assertEqual(plan["overrides"][0]["source_chain"], ["/Game/M/MI_Common_Tint", "/Game/M/MI_Common", "/Game/M/M_Master"])
        for key, snapshot in before.items():
            value = registry[key]
            self.assertEqual(snapshot, (value.get_editor_property("nanite_settings").export_text(),
                                       [slot.export_text() for slot in value.get_editor_property("materials")]))
            self.assertEqual(value.modify_count, 0)

    def test_actual_willow_nine_part_remaps_keep_branch_tint_and_leaf_assignments(self):
        names = ["M_bark_common_end_01", "M_bark_weeping_willow_01", "M_bark_weeping_willow_02",
                 "M_debris_weeping_willow_01", "M_leaf_Weeping_Willow_atlas_01"]
        paths = ["/Game/M/MI_Common", "/Game/M/MI_Bark1", "/Game/M/MI_Bark2", "/Game/M/MI_Debris", "/Game/M/MI_Leaf"]
        incoming = [slot(name, path) for name, path in zip(names, paths)]
        previous = deepcopy(incoming)
        previous[0]["material"] = "/Game/M/MI_Common_Tint"
        old_parts, parts = [], []
        for index in range(9):
            mesh = "/Game/Trees/SK_Prototype_" + str(index)
            remap = [0, 1] if index < 2 else [1, 4]
            # UE serializes an existing FName's casing, which can differ from
            # the freshly exported contract without changing the same asset.
            old_parts.append({"mesh": mesh.replace("/Trees/", "/trees/"), "remap": remap})
            parts.append({"mesh": mesh, "bend_role": 0 if index < 2 else 1,
                          "slots": [deepcopy(incoming[value]) for value in remap]})
        result = builder.plan_assembly_material_preservation(previous, old_parts, incoming[:4], parts,
            {"/Game/M/MI_Common_Tint": ["/Game/M/MI_Common_Tint", "/Game/M/MI_Common", "/Game/M/M_Master"]})
        self.assertEqual(len(result["source_materials"]), 5)
        self.assertEqual(result["source_materials"][0], "/Game/M/MI_Common_Tint")
        self.assertEqual([row["desired_remap"] for row in result["parts"]], [[0, 1]] * 2 + [[1, 4]] * 7)
        self.assertEqual([row["mesh"] for row in result["parts"]], [row["mesh"] for row in parts])

    def test_case_only_native_paths_preserve_tint_original_settings_and_plain_consumption(self):
        unreal, registry, base, branch, foliage, old, ordered = self.fixture()
        unreal.EditorAssetLibrary.load_asset = lambda path: next(
            (value for key, value in registry.items() if materials.unreal_asset_identity(key) == materials.unreal_asset_identity(path)), None)
        ordered[0]["mesh"] = ordered[0]["mesh"].replace("/Game/", "/game/")
        old.values["nanite_settings"].text = old.values["nanite_settings"].text.replace("/Trees/", "/trees/")
        source_bytes = {mesh.path: [row.export_text() for row in mesh.values["materials"]]
                        for mesh in (base, branch, foliage, old)}
        plan = builder.plan_unreal_assembly_material_preservation(unreal, old.path, base, ordered)
        self.assertEqual(plan["source_materials"], ["/Game/M/MI_Common_Tint", "/Game/M/MI_Bark", "/Game/M/MI_Leaf"])
        self.assertEqual([row["desired_remap"] for row in plan["parts"]], [[0, 1], [1, 2]])
        self.assertEqual([row["scope"] for row in plan["overrides"]], ["base", ordered[0]["mesh"]])
        fresh = Mesh(old.path, [Slot("Common", registry["/Game/M/MI_Common"]), Slot("Bark", registry["/Game/M/MI_Bark"]),
                                Slot("Leaf", registry["/Game/M/MI_Leaf"])],
                     [(branch.path.replace("/Trees/", "/trees/"), [0, 1]), (foliage.path.replace("/Trees/", "/trees/"), [1, 2])])
        serialized = fresh.values["nanite_settings"].export_text()
        result = builder.apply_unreal_assembly_material_preservation(unreal, fresh, plan)
        self.assertTrue(result["verified_after_build"])
        self.assertEqual(fresh.values["nanite_settings"].export_text(), serialized)
        self.assertIs(fresh.values["materials"][0].get_editor_property("material_interface"), registry["/Game/M/MI_Common_Tint"])
        with patch.object(materials, "audit_unreal_skeletal_mesh_material_sections", return_value={"status": "ok"}):
            result = materials.normalize_unreal_nanite_assembly_materials(unreal, fresh, material_preservation=plan)
        self.assertFalse(result["would_change"])
        self.assertEqual([row["desired_remap"] for row in result["parts"]], [[0, 1], [1, 2]])
        for mesh in (base, branch, foliage, old):
            self.assertEqual(source_bytes[mesh.path], [row.export_text() for row in mesh.values["materials"]])
            self.assertEqual(mesh.modify_count, 0)
        fresh.values["nanite_settings"].text = serialized.replace("MaterialRemap=(0,1)", "MaterialRemap=(1,0)")
        with self.assertRaisesRegex(materials.NaniteAssemblyMaterialError, "prototype/remap changed"):
            materials.normalize_unreal_nanite_assembly_materials(unreal, fresh, material_preservation=plan)

    def test_case_alias_duplicates_and_case_alias_material_cycle_reject(self):
        unreal, registry, base, branch, foliage, old, ordered = self.fixture()
        plan = builder.plan_unreal_assembly_material_preservation(unreal, old.path, base, ordered)
        for kind in ("old", "incoming", "chain"):
            with self.subTest(kind=kind):
                inputs = deepcopy(plan["inputs"])
                if kind == "old":
                    inputs["old_parts"].append({"mesh": branch.path.replace("/Trees/", "/trees/"), "remap": [0, 1]})
                    message = "duplicate prototype"
                elif kind == "incoming":
                    duplicate = deepcopy(inputs["parts"][0])
                    duplicate["mesh"] = duplicate["mesh"].replace("/Trees/", "/trees/")
                    inputs["parts"].append(duplicate)
                    message = "duplicate prototype"
                else:
                    inputs["material_chains"]["/Game/M/MI_Common_Tint"] = ["/Game/M/MI_Common_Tint", "/Game/M/mi_common_tint", "/Game/M/M_Master"]
                    message = "inheritance proof"
                with self.assertRaisesRegex(builder.ClusterAssemblyBuildError, message):
                    builder.plan_assembly_material_preservation(**inputs)
        self.assertTrue(all(value.modify_count == 0 for value in registry.values() if isinstance(value, Mesh)))

    def test_case_matching_does_not_authorize_different_objects_or_part_order(self):
        for kind in ("order", "different_path", "different_object", "wrong_input_object"):
            with self.subTest(kind=kind):
                unreal, registry, base, branch, foliage, old, ordered = self.fixture()
                plan = builder.plan_unreal_assembly_material_preservation(unreal, old.path, base, ordered)
                native_parts = [(branch.path, [0, 1]), (foliage.path, [1, 2])]
                message = "prototype order"
                if kind == "order":
                    native_parts.reverse()
                elif kind == "different_path":
                    native_parts[0] = ("/Game/Trees/SK_Unrelated", [0, 1])
                elif kind == "different_object":
                    impostor = Mesh(branch.path, branch.values["materials"])
                    original_load = unreal.EditorAssetLibrary.load_asset
                    unreal.EditorAssetLibrary.load_asset = lambda path: impostor if path.startswith(branch.path + ".") else original_load(path)
                    message = "loaded preflight"
                else:
                    ordered[0]["asset"] = foliage
                    with self.assertRaisesRegex(builder.ClusterAssemblyBuildError, "loaded SkeletalMesh"):
                        builder.plan_unreal_assembly_material_preservation(unreal, old.path, base, ordered)
                    self.assertEqual(old.modify_count, 0)
                    continue
                fresh = Mesh(old.path, [Slot("Common", registry["/Game/M/MI_Common"]), Slot("Bark", registry["/Game/M/MI_Bark"]),
                                        Slot("Leaf", registry["/Game/M/MI_Leaf"])], native_parts)
                before = [row.export_text() for row in fresh.values["materials"]]
                with self.assertRaisesRegex(builder.ClusterAssemblyBuildError, message):
                    builder.apply_unreal_assembly_material_preservation(unreal, fresh, plan)
                self.assertEqual(fresh.modify_count, 0)
                self.assertEqual(before, [row.export_text() for row in fresh.values["materials"]])

    def test_new_part_retains_bare_input_instead_of_inheriting_old_root_tint(self):
        unreal, registry, base, branch, foliage, old, ordered = self.fixture()
        new = Mesh("/Game/Trees/SK_New", [Slot("Common", registry["/Game/M/MI_Common"])])
        registry[new.path] = new
        ordered.append({"mesh": new.path, "asset": new, "bend_role": 0})
        result = builder.plan_unreal_assembly_material_preservation(unreal, old.path, base, ordered)
        self.assertEqual(result["source_materials"], ["/Game/M/MI_Common_Tint", "/Game/M/MI_Bark", "/Game/M/MI_Leaf", "/Game/M/MI_Common"])
        self.assertEqual(result["parts"][-1]["desired_remap"], [3])

    def test_matching_uses_slot_identity_when_local_order_changes(self):
        unreal, registry, base, branch, foliage, old, ordered = self.fixture()
        branch.values["materials"].reverse()
        result = builder.plan_unreal_assembly_material_preservation(unreal, old.path, base, ordered)
        self.assertEqual(result["parts"][0]["desired_remap"], [1, 0])

    def test_legacy_notess_aliases_without_normalized_source_count_preserve_base_and_parts(self):
        unreal, registry, base, branch, foliage, old, ordered = self.fixture()
        plan = builder.plan_unreal_assembly_material_preservation(unreal, old.path, base, ordered)
        inputs = deepcopy(plan["inputs"])
        for source in (0, 1):
            alias = deepcopy(inputs["old_slots"][source])
            alias["part_notess_alias"] = True
            inputs["old_slots"].append(alias)
        inputs["old_parts"][0]["remap"] = [3, 4]
        self.assertIsNone(inputs["old_source_count"])
        result = builder.plan_assembly_material_preservation(**inputs)
        self.assertEqual(result["source_materials"], plan["source_materials"])
        self.assertEqual(result["parts"], plan["parts"])
        self.assertEqual(len(result["inputs"]["old_slots"]), 5)

    def test_unrelated_parent_cycle_slot_ambiguity_and_active_override_reject_before_write(self):
        for change, message in (("unrelated", "descendant"), ("cycle", "cyclic"),
                                ("ambiguous", "Ambiguous"), ("active", "unsupported"),
                                ("slotchange", "identities changed")):
            with self.subTest(change=change):
                unreal, registry, base, branch, foliage, old, ordered = self.fixture()
                tint = registry["/Game/M/MI_Common_Tint"]
                if change == "unrelated":
                    tint.parent = registry["/Game/M/MI_Bark"]
                elif change == "cycle":
                    tint.parent = tint
                elif change == "ambiguous":
                    old.values["materials"].append(Slot("Common", tint))
                elif change == "active":
                    ordered[0]["bend_role"] = 1
                else:
                    branch.values["materials"][0].values["imported_material_slot_name"] = "Different"
                with self.assertRaisesRegex(builder.ClusterAssemblyBuildError, message):
                    builder.plan_unreal_assembly_material_preservation(unreal, old.path, base, ordered)
                self.assertTrue(all(value.modify_count == 0 for value in registry.values() if isinstance(value, Mesh)))

    def test_apply_and_plain_normalizer_preserve_source_bytes_and_protected_settings(self):
        unreal, registry, base, branch, foliage, old, ordered = self.fixture()
        plan = builder.plan_unreal_assembly_material_preservation(unreal, old.path, base, ordered)
        source_bytes = {mesh.path: [slot.export_text() for slot in mesh.values["materials"]] for mesh in (base, branch, foliage, old)}
        source_parameters = {path: deepcopy(value.parameters) for path, value in registry.items() if isinstance(value, MIC)}
        fresh = Mesh(old.path, [Slot("Common", registry["/Game/M/MI_Common"]), Slot("Bark", registry["/Game/M/MI_Bark"]),
                                Slot("Leaf", registry["/Game/M/MI_Leaf"])], [(branch.path, [0, 1]), (foliage.path, [1, 2])])
        settings_before = fresh.values["nanite_settings"].export_text()
        report = builder.apply_unreal_assembly_material_preservation(unreal, fresh, plan)
        self.assertTrue(report["verified_after_build"])
        self.assertEqual(fresh.values["nanite_settings"].export_text(), settings_before)
        self.assertIs(fresh.values["materials"][0].values["material_interface"], registry["/Game/M/MI_Common_Tint"])
        with patch.object(materials, "audit_unreal_skeletal_mesh_material_sections", return_value={"status": "ok"}):
            normalized = materials.normalize_unreal_nanite_assembly_materials(unreal, fresh, material_preservation=plan)
        self.assertFalse(normalized["would_change"])
        self.assertEqual([row["desired_remap"] for row in normalized["parts"]], [[0, 1], [1, 2]])
        for mesh in (base, branch, foliage, old):
            self.assertEqual(source_bytes[mesh.path], [slot.export_text() for slot in mesh.values["materials"]])
            self.assertEqual(mesh.modify_count, 0)
        self.assertEqual(source_parameters, {path: value.parameters for path, value in registry.items() if isinstance(value, MIC)})

    def test_corrupted_plan_and_fresh_source_mismatch_reject_before_mutation(self):
        unreal, registry, base, branch, foliage, old, ordered = self.fixture()
        plan = builder.plan_unreal_assembly_material_preservation(unreal, old.path, base, ordered)
        broken = deepcopy(plan)
        broken["source_materials"][0] = "/Game/M/MI_Common"
        with self.assertRaisesRegex(builder.ClusterAssemblyBuildError, "changed after preflight"):
            builder.apply_unreal_assembly_material_preservation(unreal, old, broken)
        with self.assertRaisesRegex(builder.ClusterAssemblyBuildError, "base materials changed"):
            builder.apply_unreal_assembly_material_preservation(unreal, old, plan)
        self.assertEqual(old.modify_count, 0)

    def test_fresh_assembly_has_no_preserved_overrides(self):
        unreal, registry, base, branch, foliage, old, ordered = self.fixture()
        result = builder.plan_unreal_assembly_material_preservation(unreal, "/Game/Trees/SK_Fresh", base, ordered)
        self.assertEqual(result["overrides"], [])
        self.assertEqual(result["source_materials"][0], "/Game/M/MI_Common")

    def test_existing_owned_bend_aliases_resolve_to_the_original_appearance(self):
        unreal, registry, base, branch, foliage, old, ordered = self.fixture()
        metadata = {}
        sources = ["/Game/M/MI_Common_Tint", "/Game/M/MI_Bark", "/Game/M/MI_Leaf"]
        for index, source in enumerate(sources):
            alias = MIC("/Game/AssemblyBend/MI_Alias" + str(index), registry["/Game/M/MI_Bark"].parent)
            registry[alias.path] = alias
            old.values["materials"].append(Slot(["Common", "Bark", "Leaf"][index], alias))
            metadata[alias.path] = {"CodexNormalizedAssemblyBendOwner": "NormalizedAssemblyPartBend/20261010/v2",
                                  "CodexNormalizedAssemblyBendSourceMaterial": source}
        metadata[old.path] = {"CodexNormalizedAssemblyBendContract": json.dumps({
            "schema_version": 2, "assembly": old.path, "source_materials": sources})}
        old.values["nanite_settings"] = Settings(old.values["nanite_settings"].export_text().replace("MaterialRemap=(1,2)", "MaterialRemap=(4,5)"))
        unreal.EditorAssetLibrary.get_metadata_tag = lambda asset, key: metadata.get(asset.path, {}).get(key, "")
        result = builder.plan_unreal_assembly_material_preservation(unreal, old.path, base, ordered)
        self.assertEqual(result["source_materials"], sources)
        self.assertEqual([row["desired_remap"] for row in result["parts"]], [[0, 1], [1, 2]])
        metadata["/Game/AssemblyBend/MI_Alias1"]["CodexNormalizedAssemblyBendOwner"] = "foreign"
        with self.assertRaisesRegex(builder.ClusterAssemblyBuildError, "ownership"):
            builder.plan_unreal_assembly_material_preservation(unreal, old.path, base, ordered)
        self.assertEqual(old.modify_count, 0)

    def test_consumption_rejects_changed_global_table_before_generic_writes(self):
        unreal, registry, base, branch, foliage, old, ordered = self.fixture()
        plan = builder.plan_unreal_assembly_material_preservation(unreal, old.path, base, ordered)
        old.values["materials"][0].values["material_interface"] = registry["/Game/M/MI_Common"]
        with patch("normalized_assembly_bend.normalize_assembly") as native_route:
            with self.assertRaisesRegex(materials.NaniteAssemblyMaterialError, "source table changed"):
                materials.normalize_unreal_nanite_assembly_materials(unreal, old, manifest={"parts": []}, material_preservation=plan)
            native_route.assert_not_called()
        self.assertEqual(old.modify_count, 0)

    def test_runtime_preflight_rejects_override_before_native_begin(self):
        unreal, registry, base, branch, foliage, old, ordered = self.fixture()
        registry["/Game/M/MI_Common_Tint"].parent = registry["/Game/M/MI_Bark"]
        skeleton = SimpleNamespace(get_path_name=lambda: "/Game/Skeleton")
        full = Mesh("/Game/Trees/SK_Full", [])
        full.values["skeleton"] = base.values["skeleton"] = skeleton
        registry[full.path] = full
        unreal.NaniteAssemblySkeletalMeshBuilder = SimpleNamespace(begin_new_skeletal_mesh_assembly_build=lambda *_args: self.fail("native overwrite must not start"))
        manifest = {"parts": [{"prototype_id": "branch", "role": "branch"}, {"prototype_id": "leaf", "role": "leaf"}],
                    "final_skeleton": {"bones": [{"name": "Root"}]}, "base": {},
                    "wind_contract": {"wind_json": {"path": str(Path(__file__))}}}
        paths = {"full_skeletal_mesh": full.path, "base_skeletal_mesh": base.path, "assembly": old.path,
                 "parts": {"branch": branch.path, "leaf": foliage.path}}
        patches = {"validate_manifest_artifacts": None, "validate_unreal_asset_contract": paths,
            "validate_unreal_normalized_prototype_bounds": {}, "_unreal_mesh_bounds_record": {},
            "validate_unreal_bounds_contract": None, "_unreal_bone_names": ["Root"],
            "_current_unreal_skeleton_diagnostic": {}, "derive_exact_unreal_import_bone_name_map": ({"Root": "Root"}, {}),
            "_dynamic_wind_user_data": True, "_skeleton_maps": ([], {}), "_base_weighted_bone_manifest_diagnostic": {}}
        from contextlib import ExitStack
        with ExitStack() as stack:
            for name, value in patches.items():
                stack.enter_context(patch.object(builder, name, return_value=value))
            with self.assertRaisesRegex(builder.ClusterAssemblyBuildError, "descendant"):
                builder._build_unreal_nanite_assembly_synchronous(unreal, manifest, paths)
        self.assertEqual(old.modify_count, 0)


if __name__ == "__main__":
    unittest.main()
