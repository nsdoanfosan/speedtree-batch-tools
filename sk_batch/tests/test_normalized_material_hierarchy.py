"""Exercise inheritance preservation and rejection before the first asset write."""
import ast
import hashlib
import json
import sys
import unittest
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

SK_BATCH = Path(__file__).resolve().parents[1]
if str(SK_BATCH) not in sys.path:
    sys.path.insert(0, str(SK_BATCH))
import normalized_assembly_bend as bend
import assembly_wpo_contract as wpo


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class Struct:
    def __init__(self, **values):
        self.values = values

    def get_editor_property(self, name):
        return self.values[name]

    def set_editor_property(self, name, value):
        self.values[name] = value


class Row(Struct):
    def __init__(self, name, value, guid, association=0):
        super().__init__(parameter_info=Struct(name=name, association=association), value=value, guid=guid)


class Base(Struct):
    def __init__(self):
        super().__init__(override_max_world_position_offset_displacement=False,
                         max_world_position_offset_displacement=27.0, blend_mode="Masked",
                         opacity_mask_clip_value=0.42, two_sided=True)

    def export_text(self):
        v = self.values
        return ("(bOverride_MaxWorldPositionOffsetDisplacement=" + str(v["override_max_world_position_offset_displacement"])
                + ",MaxWorldPositionOffsetDisplacement=" + str(v["max_world_position_offset_displacement"])
                + ",BlendMode=" + v["blend_mode"] + ",OpacityMaskClipValue=" + str(v["opacity_mask_clip_value"])
                + ",TwoSided=" + str(v["two_sided"]) + ")")


class Asset:
    def __init__(self, path):
        self.path, self.tags, self.harness = path, {}, None

    def get_path_name(self):
        return self.path + "." + self.path.rsplit("/", 1)[-1]

    def get_outermost(self):
        return SimpleNamespace(get_path_name=lambda: self.path)

    def modify(self):
        self.harness.writes.append(("modify", self.path))
        self.harness.dirty.add(self.path)


class Material(Asset):
    def __init__(self, path):
        super().__init__(path)
        self.scalars = {"Roughness": 0.7, "DetailScale": 0.2}
        self.vectors = {"Tint": [1.0, 1.0, 1.0, 1.0]}
        self.textures = {"Albedo": "/Game/Tex/Root"}
        self.switches = {"UseDetail": True, wpo.WPO_SWITCH: False}
        self.tags[wpo.WPO_GATE_TAG] = wpo.WPO_GATE_OWNER
        self.wpo = "27.0"
        self.graph = {"expressions": [{"name": "Original", "defaults": deepcopy(self.scalars)}]}
        self.roots = {"root_inputs": [{"property_name": "BaseColor", "source_name": "Original"}]}

    def get_editor_property(self, name):
        if name == "enable_tessellation":
            return getattr(self, "tessellation", True)
        return getattr(self, name)

    def set_editor_property(self, name, value):
        setattr(self, 'tessellation' if name == 'enable_tessellation' else name, value)


class MIC(Asset):
    def __init__(self, path, parent):
        super().__init__(path)
        self.parent, self.scalars, self.vectors, self.textures, self.switches = parent, [], {}, {}, {}
        self.base, self.layers = Base(), {"layer_guid": "unchanged-layer", "blend_asset": "/Game/Layer/Custom"}

    def get_editor_property(self, name):
        if name == "parent":
            return self.parent
        if name == "scalar_parameter_values":
            return self.scalars
        if name == "vector_parameter_values":
            return [Row(name, value, "vector-"+name) for name,value in self.vectors.items()]
        if name == "base_property_overrides":
            return deepcopy(self.base)
        raise KeyError(name)

    def set_editor_property(self, name, value):
        if name == "base_property_overrides":
            self.base = deepcopy(value)
        else:
            raise KeyError(name)


class Harness:
    def __init__(self):
        self.assets, self.dirty, self.writes, self.compiled = {}, set(), [], []
        self.function = self.add(Asset(bend.FUNCTION))
        self.unreal = SimpleNamespace(Material=Material, MaterialInstanceConstant=MIC,
            MaterialParameterAssociation=SimpleNamespace(GLOBAL_PARAMETER=0),
            EditorAssetLibrary=SimpleNamespace(load_asset=self.load, does_asset_exist=lambda path: path in self.assets,
                duplicate_asset=self.duplicate, get_metadata_tag=lambda asset, name: asset.tags.get(name, ""),
                set_metadata_tag=self.tag),
            EditorLoadingAndSavingUtils=SimpleNamespace(get_dirty_content_packages=lambda:
                [SimpleNamespace(get_path_name=lambda path=path: path) for path in self.dirty]),
            MaterialEditingLibrary=SimpleNamespace(set_material_instance_parent=self.set_parent,
                get_material_default_static_switch_parameter_value=lambda asset, name: asset.switches.get(name),
                set_material_instance_static_switch_parameter_value=self.set_switch,
                get_material_instance_static_switch_parameter_value=lambda asset, name: self.effective(asset, "switches", name),
                get_scalar_parameter_names=lambda asset: list(self.scalar_names(asset)),
                set_material_instance_scalar_parameter_value=self.set_scalar,
                get_material_instance_scalar_parameter_value=lambda asset, name: self.effective(asset, "scalars", name),
                is_material_instance_parameter_overridden=self.overridden,
                update_material_instance=lambda asset: None))
        self.helpers = {"digest": digest, "mi_signature": self.mi_signature, "stable_value": self.stable,
            "SWITCH": "AssemblyPartBendEnabled", "graph": lambda asset: deepcopy(asset.graph),
            "roots": lambda asset: deepcopy(asset.roots), "property_text": lambda asset, name: asset.wpo,
            "owned_existing": self.owned, "make_master": self.master, "compile_resource": self.compile,
            "validate_master_contract": self.validate_master,
            "graph_signature": lambda asset: digest(asset.graph), "GRAPH_TAG": "GraphSHA",
            "OWNER_TAG": "MasterOwner", "SOURCE_TAG": "MasterSourceSHA", "STATE_TAG": bend.STATE_TAG}

    def add(self, asset):
        asset.harness = self
        self.assets[asset.path] = asset
        return asset

    def load(self, path):
        return self.assets.get(bend.package_path(path))

    def duplicate(self, source_path, path):
        source = self.load(source_path)
        target = Material(path) if isinstance(source, Material) else MIC(path, source.parent)
        for key, value in vars(source).items():
            if key not in {"path", "harness", "parent"}:
                setattr(target, key, deepcopy(value))
        self.add(target)
        self.writes.append(("duplicate", path))
        self.dirty.add(path)
        return target

    def tag(self, asset, name, value):
        self.writes.append(("tag", asset.path, name))
        asset.tags[name] = value
        self.dirty.add(asset.path)

    def set_parent(self, asset, parent):
        self.writes.append(("parent", asset.path, parent.path))
        asset.parent = parent

    def set_switch(self, asset, name, value):
        asset.switches[name] = value

    def set_scalar(self, asset, name, value):
        for row in asset.scalars:
            info = row.get_editor_property("parameter_info")
            if info.get_editor_property("name") == name and info.get_editor_property("association") == 0:
                row.set_editor_property("value", value)
                return
        asset.scalars.append(Row(name, value, "new-" + name))

    @staticmethod
    def overridden(asset, name):
        return name in asset.switches or any(row.values["parameter_info"].values == {"name": name, "association": 0}
                                               for row in asset.scalars)

    def scalar_names(self, asset):
        if isinstance(asset, Material):
            return set(asset.scalars)
        return self.scalar_names(asset.parent) | {row.values["parameter_info"].values["name"] for row in asset.scalars}

    def effective(self, asset, kind, name):
        values = getattr(asset, kind)
        if isinstance(asset, MIC) and kind == "scalars":
            for row in values:
                if row.values["parameter_info"].values == {"name": name, "association": 0}:
                    return row.values["value"]
        elif name in values:
            return values[name]
        if isinstance(asset, MIC):
            return self.effective(asset.parent, kind, name)
        return None

    @staticmethod
    def stable(value):
        if isinstance(value, Struct):
            return {key: Harness.stable(item) for key, item in value.values.items()}
        return deepcopy(value)

    def mi_signature(self, asset):
        current, switch_names = asset, set()
        while current:
            switch_names.update(current.switches)
            current = current.parent if isinstance(current, MIC) else None
        return {"parent": asset.parent.get_path_name() if asset.parent else None,
                "scalar_parameter_values": [self.stable(row) for row in asset.scalars],
                "vector_parameter_values": deepcopy(asset.vectors), "texture_parameter_values": deepcopy(asset.textures),
                "base_property_overrides": asset.base.export_text(),
                "layers": {"material_interface": asset.get_path_name(), **deepcopy(asset.layers)},
                "effective_static_switches": {name: self.effective(asset, "switches", name) for name in switch_names}}

    def owned(self, path, source_hash="", allow_building=False):
        asset = self.assets.get(path)
        if asset:
            bend.require(isinstance(asset, Material) and asset.tags.get("MasterOwner") == "root-owner"
                and asset.tags.get("MasterSourceSHA") == source_hash
                and asset.tags.get("GraphSHA") == digest(asset.graph), "stale root wrapper")
        return asset

    def master(self, source, path, function, graph, roots):
        existing = self.owned(path, digest({"graph": graph, "roots": roots}), allow_building=True)
        if existing:
            return existing, False
        target = self.duplicate(source.get_path_name(), path)
        target.scalars.update({**bend.DEFAULT_SCALARS, **bend.EXTRA_SCALARS})
        target.switches[self.helpers["SWITCH"]] = False
        target.graph["expressions"].append({"name": "BendWrapper", "function": "Shared"})
        target.tags.update(MasterOwner="root-owner", MasterSourceSHA=digest({"graph": graph, "roots": roots}))
        return target, True

    @staticmethod
    def validate_master(target, function, graph, roots):
        bend.require(target.graph["expressions"] == graph["expressions"]
            + [{"name": "BendWrapper", "function": "Shared"}], "malformed root wrapper")

    def compile(self, asset):
        self.compiled.append(asset.path)
        return {"asset": asset.get_path_name(), "native_finish_compilation_waited": True,
                "shader_count": 1, "recompile_errors": []}

    def chain(self):
        root = self.add(Material("/Game/Trees/M_Root"))
        common = self.add(MIC("/Game/Trees/MI_Common", root))
        common.scalars = [Row("Roughness", 0.35, "common-scalar-guid"),
                          Row("AssemblyBranchBendCm", 87.0, "custom-layer-guid", association=1)]
        common.vectors = {"Tint": [0.1, 0.35, 0.08, 1.0]}
        common.textures = {"Albedo": "/Game/Tex/Willow"}
        common.switches = {"UseDetail": False}
        species = self.add(MIC("/Game/Trees/MI_Species", common))
        species.scalars = [Row("DetailScale", 0.63, "species-guid")]
        tint = self.add(MIC("/Game/Trees/MI_WillowTint", species))
        tint.vectors = {"HighlightTint": [0.2, 0.4, 0.0, 1.0]}
        return root, common, species, tint


class MaterialHierarchyTests(unittest.TestCase):
    def setUp(self):
        self.h = Harness()
        self.root, self.common, self.species, self.tint = self.h.chain()
        self.patch = patch.object(bend, "material_helpers", return_value=self.h.helpers)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.gate_patch = patch.object(wpo, 'install_wpo_gate', side_effect=lambda u, target, **kw:
            target.switches.update({wpo.WPO_SWITCH: kw['enabled']}))
        self.gate_patch.start()
        self.addCleanup(self.gate_patch.stop)

    def make(self, sources=None, inspection=None, admission=None):
        return bend.make_material_variants(self.h.unreal, sources or [self.tint.path], self.h.function,
                                          inspection=inspection, admission=admission)

    def test_original_wpo_policy_is_checked_before_any_duplicate_or_checkout(self):
        self.root.switches[wpo.WPO_SWITCH]=True
        self.assert_prewrite_rejection(lambda:self.make())

    def test_equal_short_names_stop_with_both_original_paths_before_writes(self):
        other=self.h.add(MIC('/Game/Other/MI_WillowTint',self.root))
        with self.assertRaisesRegex(bend.NormalizedAssemblyBendError,'Canonical NoTess material name collision') as error:
            self.make([self.tint.path,other.path])
        self.assertIn(self.tint.path,str(error.exception))
        self.assertIn(other.path,str(error.exception))
        self.assertEqual(self.h.writes,[])

    def test_reimport_of_canonical_wind_uses_original_identity_and_no_intermediate_variants(self):
        first=self.completed();target=self.h.load(first['variants'][0])
        before={path for path in self.h.assets}
        second=self.make([target.path])
        self.assertEqual(second['variants'],first['variants'])
        self.assertEqual(set(self.h.assets),before)
        self.assertTrue(all(path.endswith('_notess') for path in first['packages']))
        self.assertFalse(any('_CArc' in path or '_AspectBend' in path for path in first['packages']))

    def completed(self):
        result = self.make()
        self.h.dirty.clear()
        self.h.writes.clear()
        self.h.compiled.clear()
        return result

    def assert_prewrite_rejection(self, operation):
        before = list(self.h.writes)
        with self.assertRaises(bend.NormalizedAssemblyBendError):
            operation()
        self.assertEqual(self.h.writes, before)

    def test_four_level_plan_retains_leaf_slot_identity_and_deduplicates_shared_ancestors(self):
        second = self.h.add(MIC("/Game/Trees/MI_OtherTint", self.species))
        plan = bend.plan_material_packages(self.h.unreal, [self.tint.path, second.path, self.tint.path])
        self.assertEqual(plan["source_materials"], [self.tint.path, second.path, self.tint.path])
        self.assertEqual(plan["source_chains"][0], [self.tint.path, self.species.path, self.common.path, self.root.path])
        self.assertEqual(plan["source_instance_layers"], [self.common.path, self.species.path, self.tint.path, second.path])
        self.assertEqual(plan["materials"], [bend.variant_path(self.root.path, master=True)]
            + [bend.variant_path(path) for path in plan["source_instance_layers"]])
        self.assertEqual(self.h.writes, [])

    def test_active_two_of_five_preserves_source_order_and_clones_only_two_direct_chains(self):
        foliage_root = self.h.add(Material("/Game/Trees/M_Foliage"))
        bark = self.h.add(MIC("/Game/Trees/MI_Bark1", self.root))
        atlas = self.h.add(MIC("/Game/Trees/MI_Atlas", foliage_root))
        bark2 = self.h.add(MIC("/Game/Trees/MI_Bark2", self.root))
        debris = self.h.add(MIC("/Game/Trees/MI_Debris", self.root))
        paths = [self.tint.path, bark.path, bark2.path, debris.path, atlas.path]
        original = {path: deepcopy(self.h.mi_signature(self.h.load(path))) for path in paths}
        plan = bend.plan_material_packages(self.h.unreal, paths, active_source_indices=[1, 4])
        self.assertEqual(plan["source_materials"], paths)
        self.assertEqual(plan["active_source_materials"], [bark.path, atlas.path])
        self.assertEqual(plan["source_chains"], [[], [bark.path, self.root.path], [], [], [atlas.path, foliage_root.path]])
        self.assertEqual(plan["source_instance_layers"], [bark.path, atlas.path])
        result = bend.make_material_variants(self.h.unreal, paths, self.h.function, active_source_indices=[1, 4])
        self.assertEqual(len(result["packages"]), 4)
        self.assertEqual(result["variants"], [self.tint.path, bend.variant_path(bark.path), bark2.path, debris.path, bend.variant_path(atlas.path)])
        self.assertFalse(any(self.h.load(bend.variant_path(path)) for path in (self.tint.path, self.common.path, self.species.path, bark2.path, debris.path)))
        self.assertEqual({path: self.h.mi_signature(self.h.load(path)) for path in paths}, original)
        self.h.dirty.clear()
        self.h.writes.clear()
        repeated = bend.make_material_variants(self.h.unreal, paths, self.h.function, active_source_indices=[1, 4])
        self.assertEqual(repeated["packages"], result["packages"])
        self.assertEqual(repeated["variants"], result["variants"])
        self.assertFalse(any(row[0] == "duplicate" for row in self.h.writes))

    def test_inactive_existing_derivative_is_not_inspected_or_mutated(self):
        direct = self.h.add(MIC("/Game/Trees/MI_Direct", self.root))
        inactive = self.h.add(MIC(bend.variant_path(self.tint.path), self.root))
        inactive.tags[bend.OWNER_TAG] = "another-owner"
        self.h.dirty.add(inactive.path)
        result = bend.make_material_variants(self.h.unreal, [self.tint.path, direct.path], self.h.function,
                                            active_source_indices=[1])
        self.assertNotIn(inactive.path, result["packages"])
        self.assertFalse(any(row[1] == inactive.path for row in self.h.writes))

    def test_active_custom_chain_keeps_ancestors_but_inactive_index_of_ancestor_is_identity(self):
        paths = [self.common.path, self.tint.path]
        result = bend.make_material_variants(self.h.unreal, paths, self.h.function, active_source_indices=[1])
        common = self.h.load(bend.variant_path(self.common.path))
        self.assertEqual(result["variants"], [self.common.path, bend.variant_path(self.tint.path)])
        self.assertEqual(bend.inherited_layer_signature(self.h.helpers, common), bend.inherited_layer_signature(self.h.helpers, self.common))
        self.assertFalse(self.h.overridden(common, "AssemblyBranchBendCm"))

    def test_invalid_or_changed_active_indices_reject_before_any_write(self):
        for indices in ([], [True], [1], [-1], [0, 0], "0"):
            self.assert_prewrite_rejection(lambda indices=indices: bend.make_material_variants(
                self.h.unreal, [self.tint.path], self.h.function, active_source_indices=indices))
        direct = self.h.add(MIC("/Game/Trees/MI_Direct", self.root))
        paths = [self.tint.path, direct.path]
        inspection = bend.inspect_material_variants(self.h.unreal, paths, active_source_indices=[1])
        self.assert_prewrite_rejection(lambda: bend.make_material_variants(self.h.unreal, paths, self.h.function,
            inspection=inspection, active_source_indices=[0]))

    def test_v2_generation_remains_neutral_without_response_inputs(self):
        result = self.make()
        target = self.h.load(result["variants"][0])
        for name in bend.RETURN_SCALARS:
            self.assertFalse(self.h.overridden(target, name))
        self.assertEqual(bend.material_scalar_defaults(self.h.unreal, self.h.function),
                         {name: value for name, value in {**bend.DEFAULT_SCALARS, **bend.EXTRA_SCALARS}.items() if name not in bend.RETURN_SCALARS})

    def test_exact_v3_generation_writes_neutral_response_defaults_and_unknown_tag_rejects(self):
        self.h.function.tags[bend.RETURN_TAG] = "unknown-policy"
        self.assert_prewrite_rejection(lambda: self.make())
        self.h.function.tags[bend.RETURN_TAG] = bend.RETURN_OWNER
        result = self.make()
        target = self.h.load(result["variants"][0])
        for name, value in (("AssemblyWindRestThreshold", 0.0), ("AssemblyWindResponseScale", 1.0)):
            self.assertTrue(self.h.overridden(target, name))
            self.assertEqual(self.h.effective(target, "scalars", name), value)

    def test_response_tuning_only_ignores_two_global_fields_and_preserves_raw_chain(self):
        before_raw = deepcopy(self.h.mi_signature(self.tint))
        before_inherited = bend.inherited_layer_signature(self.h.helpers, self.tint)
        before = bend.protected_mi_signature(self.h.unreal, self.h.helpers, self.tint)
        self.tint.scalars.extend([Row("AssemblyWindRestThreshold", 0.23, "rest"), Row("AssemblyWindResponseScale", 1.8, "response")])
        self.assertEqual(bend.protected_mi_signature(self.h.unreal, self.h.helpers, self.tint), before)
        self.assertNotEqual(self.h.mi_signature(self.tint), before_raw)
        self.assertNotEqual(bend.inherited_layer_signature(self.h.helpers, self.tint), before_inherited)
        for name in bend.RETURN_SCALARS:
            self.tint.scalars.append(Row(name, 9.0, "layer", association=1))
            self.assertNotEqual(bend.protected_mi_signature(self.h.unreal, self.h.helpers, self.tint), before)
            self.tint.scalars.pop()
        self.tint.scalars.append(Row("Roughness", 0.1, "protected"))
        self.assertNotEqual(bend.protected_mi_signature(self.h.unreal, self.h.helpers, self.tint), before)

    def test_v3_tuned_clone_reuse_preserves_raw_source_and_source_chain_guards(self):
        self.h.function.tags[bend.RETURN_TAG] = bend.RETURN_OWNER
        source_before = deepcopy(self.h.mi_signature(self.tint))
        result = self.completed()
        target = self.h.load(result["variants"][0])
        chain_before = target.tags[bend.CHAIN_SIGNATURE_TAG]
        self.h.set_scalar(target, "AssemblyWindRestThreshold", 0.24)
        self.h.set_scalar(target, "AssemblyWindResponseScale", 1.7)
        target.tags[bend.SIGNATURE_TAG] = digest(self.h.mi_signature(target))
        wpo.stamp_wind(self.h.unreal, target, self.tint, helpers=self.h.helpers)
        self.h.dirty.clear()
        bend.inspect_material_variants(self.h.unreal, [self.tint.path], function=self.h.function)
        self.assertEqual(self.h.mi_signature(self.tint), source_before)
        self.assertEqual(target.tags[bend.CHAIN_SIGNATURE_TAG], chain_before)
        target.scalars.append(Row("AssemblyWindRestThreshold", 0.24, "forbidden-layer", association=2))
        target.tags[bend.SIGNATURE_TAG] = digest(self.h.mi_signature(target))
        self.assert_prewrite_rejection(lambda: bend.inspect_material_variants(self.h.unreal, [self.tint.path], function=self.h.function))
        target.scalars.pop()
        target.tags[bend.SIGNATURE_TAG] = digest(self.h.mi_signature(target))
        self.tint.scalars.append(Row("AssemblyWindRestThreshold", 0.24, "new-source-value"))
        self.assert_prewrite_rejection(lambda: bend.inspect_material_variants(self.h.unreal, [self.tint.path], function=self.h.function))

    def test_custom_inherited_tint_texture_scalar_switch_guid_base_and_layers_survive(self):
        original = {path: deepcopy(self.h.mi_signature(asset)) for path, asset in self.h.assets.items() if isinstance(asset, MIC)}
        result = self.make()
        tint = self.h.load(result["variants"][0])
        species, common = tint.parent, tint.parent.parent
        self.assertEqual([tint.path, species.path, common.path, common.parent.path],
                         [bend.variant_path(self.tint.path), bend.variant_path(self.species.path),
                          bend.variant_path(self.common.path), bend.variant_path(self.root.path, master=True)])
        for kind, name in (("vectors", "Tint"), ("vectors", "HighlightTint"), ("textures", "Albedo"),
                           ("scalars", "Roughness"), ("scalars", "DetailScale"), ("switches", "UseDetail")):
            self.assertEqual(self.h.effective(tint, kind, name), self.h.effective(self.tint, kind, name))
        for source, clone in ((self.common, common), (self.species, species)):
            self.assertEqual(bend.inherited_layer_signature(self.h.helpers, clone),
                             bend.inherited_layer_signature(self.h.helpers, source))
            self.assertNotIn("AssemblyBranchBendCm", [r.values["parameter_info"].values["name"]
                for r in clone.scalars if r.values["parameter_info"].values["association"] == 0])
        self.assertTrue(self.h.overridden(tint, "AssemblyBranchBendCm"))
        self.assertEqual(self.h.effective(tint, "scalars", "AssemblyBranchBendCm"), bend.DEFAULT_SCALARS["AssemblyBranchBendCm"])
        self.assertEqual([r for r in common.scalars if r.values["guid"] == "custom-layer-guid"][0].values["value"], 87.0)
        self.assertEqual(result["packages"], self.h.compiled)
        self.assertEqual(len(result["packages"]), 4)
        self.assertEqual({path: self.h.mi_signature(self.h.load(path)) for path in original}, original)

    def test_direct_parent_destinations_and_values_stay_compatible(self):
        source = self.h.add(MIC("/Game/Trees/MI_Direct", self.root))
        source.vectors = {"Tint": [0.4, 0.6, 0.2, 1]}
        result = self.make([source.path, source.path])
        self.assertEqual(result["variants"], [bend.variant_path(source.path)] * 2)
        self.assertEqual(result["packages"], [bend.variant_path(self.root.path, master=True), bend.variant_path(source.path)])
        self.assertEqual(self.h.effective(self.h.load(result["variants"][0]), "vectors", "Tint"), source.vectors["Tint"])
        self.h.dirty.clear()
        target = self.h.load(result["variants"][0])
        del target.tags[bend.CHAIN_SIGNATURE_TAG]
        self.make([source.path])  # Pre-chain direct derivatives remain admissible.

    def test_completed_chain_can_be_inspected_and_reused_without_duplicate_packages(self):
        result = self.completed()
        again = self.make()
        self.assertEqual(again["packages"], result["packages"])
        self.assertFalse(any(row[0] == "duplicate" for row in self.h.writes))

    def test_cycle_is_rejected_before_any_write(self):
        self.common.parent = self.tint
        self.assert_prewrite_rejection(lambda: self.make())

    def test_missing_and_unsupported_parent_are_rejected_before_any_write(self):
        for parent in (None, Asset("/Game/Trees/Unsupported")):
            self.common.parent = parent
            self.assert_prewrite_rejection(lambda: self.make())

    def test_unavailable_or_external_ancestor_is_rejected_before_any_write(self):
        del self.h.assets[self.common.path]
        self.assert_prewrite_rejection(lambda: self.make())
        self.h.add(self.common)
        external = self.h.add(Material("/Engine/Root"))
        self.common.parent = external
        self.assert_prewrite_rejection(lambda: self.make())

    def test_source_ancestor_changed_after_inspection_is_rejected_prewrite(self):
        inspection = bend.inspect_material_variants(self.h.unreal, [self.tint.path])
        self.common.vectors["Tint"][0] = 0.95
        self.assert_prewrite_rejection(lambda: self.make(inspection=inspection))

    def test_source_root_changed_after_inspection_is_rejected_prewrite(self):
        inspection = bend.inspect_material_variants(self.h.unreal, [self.tint.path])
        self.root.graph["expressions"][0]["defaults"]["Roughness"] = 0.12
        self.assert_prewrite_rejection(lambda: self.make(inspection=inspection))

    def test_source_topology_changed_after_inspection_is_rejected_prewrite(self):
        inspection = bend.inspect_material_variants(self.h.unreal, [self.tint.path])
        self.tint.parent = self.common
        self.assert_prewrite_rejection(lambda: self.make(inspection=inspection))

    def test_existing_clone_is_rejected_when_source_ancestor_changed(self):
        self.completed()
        self.common.scalars[0].values["value"] = 0.9
        self.assert_prewrite_rejection(lambda: self.make())

    def test_existing_flattened_clone_is_rejected_even_with_valid_local_signature(self):
        result = self.completed()
        target = self.h.load(result["variants"][0])
        target.parent = self.h.load(bend.variant_path(self.root.path, master=True))
        target.tags[bend.SIGNATURE_TAG] = digest(self.h.mi_signature(target))
        self.assert_prewrite_rejection(lambda: self.make())

    def test_dirty_intermediate_destination_is_rejected_before_root_mutation(self):
        self.completed()
        self.h.dirty.add(bend.variant_path(self.common.path))
        self.assert_prewrite_rejection(lambda: self.make())

    def test_dirty_intermediate_source_is_rejected_before_root_mutation(self):
        self.h.dirty.add(self.common.path)
        self.assert_prewrite_rejection(lambda: self.make())

    def test_dirty_root_wrapper_is_rejected_even_if_marked_building(self):
        self.completed()
        path = bend.variant_path(self.root.path, master=True)
        self.h.load(path).tags[bend.STATE_TAG] = "building"
        self.h.dirty.add(path)
        self.assert_prewrite_rejection(lambda: self.make())

    def test_new_collision_or_dirty_target_after_checkout_is_rejected_prewrite(self):
        for dirty in (False, True):
            with self.subTest(dirty=dirty):
                inspection = bend.inspect_material_variants(self.h.unreal, [self.tint.path])
                path = bend.variant_path(self.species.path)
                if dirty:
                    self.h.dirty.add(path)
                else:
                    self.h.add(MIC(path, self.root))
                self.assert_prewrite_rejection(lambda: self.make(inspection=inspection))
                self.h.dirty.clear()
                self.h.assets.pop(path, None)

    def test_checkout_omitting_intermediate_clone_is_rejected_prewrite(self):
        plan = bend.plan_material_packages(self.h.unreal, [self.tint.path])
        admission = {"verified": True, "materials": [path for path in plan["materials"]
                     if path != bend.variant_path(self.common.path)]}
        self.assert_prewrite_rejection(lambda: self.make(admission=admission))

    def test_full_chain_compile_receipts_drive_parent_first_durable_dependency_save(self):
        from sk_batch.tests import test_normalized_assembly_bend as delivery_tests
        result = self.make()
        fixture = delivery_tests.NormalizedDependencySaveTests(methodName="test_materials_then_seven_prototypes_saved_with_original_owner")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.materials = result["packages"]
        fixture.paths = result["packages"] + fixture.prototypes
        fixture.assets = {path: SimpleNamespace(path=path) for path in fixture.paths}
        fixture.frozen["materials"] = result["packages"]
        fixture.runner._NORMALIZED_BEND_DEPENDENCY_SCOPES[id(fixture.item)] = deepcopy(fixture.frozen)
        fixture.report.update(dependency_checkout=deepcopy(fixture.frozen),
                              dependent_packages_to_save=fixture.paths, material_compile_proofs=result["compile"])
        fixture.report["normalized_part_bend_policy"]["material_packages"] = result["packages"]
        fixture.report["normalized_part_bend_policy"]["contract"]["variant_materials"] = result["variants"]
        fixture.dirty.update(fixture.paths)
        receipt = fixture.runner._save_normalized_part_bend_dependencies(fixture.item, fixture.report,
                                                                         durable_saves=fixture.ledger)
        self.assertEqual(fixture.saved, fixture.paths)
        self.assertEqual(receipt["status"], "persisted_verified_clean")
        self.assertTrue(fixture.runner._validate_durable_save_ledger(fixture.ledger))
        self.assertEqual(len(result["compile"]), 4)

    def test_unowned_or_changed_intermediate_clone_is_rejected_prewrite(self):
        self.completed()
        clone = self.h.load(bend.variant_path(self.common.path))
        clone.vectors["Tint"][1] = 0.8
        self.assert_prewrite_rejection(lambda: self.make())
        clone.tags[bend.SIGNATURE_TAG] = digest(self.h.mi_signature(clone))
        self.assert_prewrite_rejection(lambda: self.make())

    def test_actual_helper_building_second_root_rejects_bad_wiring_before_new_first_root_write(self):
        # Run the actual production ownership and graph validators, rather than
        # an ownership stub that already rejects malformed building graphs.
        source_file = Path("C:/UnrealProjects/MyProject2/Scripts/NaniteAssemblyPartBend/materials.py")
        if not source_file.is_file():
            self.skipTest("production material helper source is unavailable")
        tree = ast.parse(source_file.read_text(encoding="utf-8"), filename=str(source_file))
        names = {"owned_existing", "load", "dirty_packages", "package_path", "normalize_expression",
                 "assert_source_nodes_preserved", "input_signature", "validate_master_contract"}
        definitions = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
        self.assertEqual({node.name for node in definitions}, names)
        constants = {target.id: ast.literal_eval(node.value) for node in tree.body if isinstance(node, ast.Assign)
                     for target in node.targets if isinstance(target, ast.Name)
                     and target.id in {"OWNER", "OWNER_TAG", "SOURCE_TAG", "GRAPH_TAG", "STATE_TAG", "SWITCH"}}
        make_master = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "make_master")
        self.assertTrue(any(isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                            and node.func.id == "validate_master_contract" for node in ast.walk(make_master)))
        scope = {**constants, "unreal": self.h.unreal, "A": self.h.unreal.EditorAssetLibrary,
                 "E": self.h.unreal.MaterialEditingLibrary, "require": bend.require, "digest": digest,
                 "graph": self.h.helpers["graph"], "roots": self.h.helpers["roots"],
                 "graph_signature": lambda asset: digest(asset.graph["expressions"])}
        self.h.unreal.MaterialFunction = type("MaterialFunction", (Asset,), {})
        scope["E"].get_material_expressions = lambda asset: asset.nodes
        exec(compile(ast.Module(body=definitions, type_ignores=[]), str(source_file), "exec"), scope)

        class Node(Struct):
            def __init__(self, name, kind, **values):
                super().__init__(desc=constants["OWNER"], **values)
                self.name, self.kind = name, kind

            def get_name(self):
                return self.name

            def get_class(self):
                return SimpleNamespace(get_name=lambda: self.kind)

        second_root = self.h.add(Material("/Game/Trees/M_SecondRoot"))
        second_source = self.h.add(MIC("/Game/Trees/MI_SecondRoot", second_root))
        original_pin = {"connected": True, "source_name": "Original", "source_class": "MaterialExpressionConstant3Vector",
                        "output_index": 0, "mask": False, "mask_r": False, "mask_g": False, "mask_b": False, "mask_a": False}
        second_root.roots = {"root_inputs": [{"property_name": "MP_WorldPositionOffset", **original_pin},
                                             {"property_name": "MP_BaseColor", **original_pin}]}
        wrapper = self.h.duplicate(second_root.get_path_name(), bend.variant_path(second_root.path, master=True))
        wrapper.nodes = [Node("Original", "MaterialExpressionConstant3Vector"),
                         Node("Call", "MaterialExpressionMaterialFunctionCall", material_function=self.h.function),
                         Node("Add", "MaterialExpressionAdd"),
                         Node("Switch", "MaterialExpressionStaticSwitchParameter", parameter_name=constants["SWITCH"], default_value=False)]
        wrapper.graph["expressions"].extend([
            {"name": "Call", "inputs": []},
            {"name": "Add", "inputs": [{"name": "A", **original_pin}, {"name": "B", "source_name": "Call", "output_index": 0}]},
            {"name": "Switch", "inputs": [{"name": "True", "source_name": "Add"}, {"name": "False", **original_pin}]}])
        wrapper.roots["root_inputs"][0]["source_name"] = "Switch"
        wrapper.tags = {constants["OWNER_TAG"]: constants["OWNER"], constants["STATE_TAG"]: "building",
                        constants["SOURCE_TAG"]: digest({"graph": second_root.graph, "roots": second_root.roots})}
        self.h.dirty.clear()
        self.h.writes.clear()
        actual_owned, actual_validate = scope["owned_existing"], scope["validate_master_contract"]
        actual_validate(wrapper, self.h.function, second_root.graph, second_root.roots)
        wrapper.graph["expressions"][2]["inputs"][1]["source_name"] = "Original"  # malformed Add.B
        self.assertIs(actual_owned(wrapper.path, wrapper.tags[constants["SOURCE_TAG"]], allow_building=True), wrapper)
        with self.assertRaisesRegex(bend.NormalizedAssemblyBendError, "enabled bend connections"):
            actual_validate(wrapper, self.h.function, second_root.graph, second_root.roots)
        self.h.helpers.update(owned_existing=actual_owned, validate_master_contract=actual_validate,
                              OWNER_TAG=constants["OWNER_TAG"], SOURCE_TAG=constants["SOURCE_TAG"], GRAPH_TAG=constants["GRAPH_TAG"])
        old_master = self.h.helpers["make_master"]
        def late_master(source, path, function, graph, roots):
            existing = actual_owned(path, digest({"graph": graph, "roots": roots}), allow_building=True)
            if existing:
                actual_validate(existing, function, graph, roots)
                return existing, False
            return old_master(source, path, function, graph, roots)
        self.h.helpers["make_master"] = late_master
        self.assert_prewrite_rejection(lambda: self.make([self.tint.path, second_source.path]))
        self.assertNotIn(bend.variant_path(self.root.path, master=True), self.h.assets)

    def test_root_wrapper_inspection_loads_function_only_when_needed_and_binds_generation(self):
        original_function = self.h.assets.pop(bend.FUNCTION)
        inspection = bend.inspect_material_variants(self.h.unreal, [self.tint.path])
        self.assertIsNone(inspection["function"])
        self.h.add(original_function)
        self.completed()
        inspection = bend.inspect_material_variants(self.h.unreal, [self.tint.path])
        self.assertIs(inspection["function"], original_function)
        other = Asset("/Game/OtherFunction")
        self.assert_prewrite_rejection(lambda: bend.make_material_variants(self.h.unreal, [self.tint.path], other,
                                                                          inspection=inspection))
        self.h.assets.pop(bend.FUNCTION)
        self.assert_prewrite_rejection(lambda: bend.inspect_material_variants(self.h.unreal, [self.tint.path]))


class LegacyNoTessWindTuningTests(unittest.TestCase):
    setUp = MaterialHierarchyTests.setUp
    make = MaterialHierarchyTests.make
    def test_existing_notess_leaf_parent_retains_arc_and_tier_tuning(self):
        source = self.h.add(MIC("/Game/Trees/MI_LeafWind", self.root))
        self.make([source.path]); self.h.dirty.clear()
        target = self.h.load(bend.variant_path(source.path))
        expected = self.h.load(bend.variant_path(self.root.path, master=True))
        expected.tessellation = False
        self.h.set_scalar(target, "AssemblyBranchFlexibility", 0.5)
        self.h.set_scalar(target, "AssemblyLeafArcRadiusCm", 240.0)
        target.vectors["AssemblyLeafTierCalmRange"] = [0.6,0.75,1.12,1.0]
        wpo.stamp_wind(self.h.unreal, target, source, helpers=self.h.helpers)
        self.h.dirty.clear()
        self.assertTrue(bend.verified_legacy_notess_parent(self.h.unreal,self.h.helpers,target,expected.path))
        self.make([source.path])
        self.assertEqual(self.h.effective(target,"scalars","AssemblyBranchFlexibility"),0.5)
        self.assertEqual(self.h.effective(target,"scalars","AssemblyLeafArcRadiusCm"),240.0)
        self.assertEqual(target.vectors["AssemblyLeafTierCalmRange"],[0.6,0.75,1.12,1.0])
        self.h.dirty.clear()
        target.vectors["UNRELATED_TINT"] = [1.0,0.0,0.0,1.0]
        with self.assertRaisesRegex(bend.NormalizedAssemblyBendError,"changed more than"):
            bend.verified_legacy_notess_parent(self.h.unreal,self.h.helpers,target,expected.path)

    def test_arc_vector_in_material_layer_remains_protected(self):
        source = self.h.add(MIC("/Game/Trees/MI_LayerWind", self.root))
        previous = bend.protected_mi_signature(self.h.unreal,self.h.helpers,source)
        original_get = source.get_editor_property
        source.get_editor_property = lambda name: [Row("AssemblyLeafTierCalmRange",[1,2,3,4],"layer",association=1)] if name=="vector_parameter_values" else original_get(name)
        self.assertNotEqual(bend.protected_mi_signature(self.h.unreal,self.h.helpers,source),previous)
    def test_adopted_off_parent_and_artist_wind_values_survive_reimport(self):
        source = self.h.add(MIC("/Game/Trees/MI_LegacyWillow", self.root))
        self.make([source.path]);self.h.dirty.clear()
        target = self.h.load(bend.variant_path(source.path))
        expected = self.h.load(bend.variant_path(self.root.path, master=True))
        off = expected
        self.h.set_scalar(target, "AssemblyBranchFlexibility", 0.5)
        self.h.set_scalar(target, "AssemblyBranchOscillationCm", 8.0)
        wpo.stamp_wind(self.h.unreal, target, source, helpers=self.h.helpers)
        self.h.dirty.clear()
        self.assertTrue(bend.verified_legacy_notess_parent(self.h.unreal,self.h.helpers,target,expected.path))
        self.make([source.path])
        self.assertIs(target.parent,off)
        self.assertEqual(self.h.effective(target,"scalars","AssemblyBranchFlexibility"),0.5)
        self.assertEqual(self.h.effective(target,"scalars","AssemblyBranchOscillationCm"),8.0)
        self.h.dirty.clear()
        target.vectors["UNRELATED_TINT"] = [1.0,0.0,0.0,1.0]
        with self.assertRaisesRegex(bend.NormalizedAssemblyBendError,"changed more than"):
            bend.verified_legacy_notess_parent(self.h.unreal,self.h.helpers,target,expected.path)


if __name__ == "__main__":
    unittest.main()
