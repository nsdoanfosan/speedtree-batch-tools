from copy import deepcopy
import sys
from pathlib import Path
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cluster_assembly_builder as builder
from sk_batch.tests.test_assembly_material_preservation import AssemblyMaterialPreservationTests, MIC, Slot, Mesh


class BendImportedSlotIdentityTests(unittest.TestCase):
    def rows(self):
        return [
            {"slot_name":"Bark", "imported_slot_name":"Bark", "material":"/Game/M/MI_Bark"},
            {"slot_name":"Bark", "imported_slot_name":"Bark_1", "material":"/Game/M/MI_Bark"},
            {"slot_name":"Bark", "imported_slot_name":"Bark", "material":"/Game/M/MI_Bark", "part_notess_alias":True},
        ]

    def test_owned_alias_suffix_uses_unique_source_despite_notess_alias(self):
        rows = self.rows()
        expected = deepcopy(rows)
        expected[1]["imported_slot_name"] = "Bark"
        self.assertEqual(builder._canonicalize_previous_bend_imported_slot_names(rows, [1]), expected)

    def test_unowned_and_authored_names_stay_strict(self):
        for owned, name in [([], "Bark_1"), ([1], "ArtistBark"), ([1], "Bark_Artist")]:
            rows = self.rows()
            rows[1]["imported_slot_name"] = name
            before = deepcopy(rows)
            self.assertEqual(builder._canonicalize_previous_bend_imported_slot_names(rows, owned), before)

    def test_ambiguous_or_different_source_is_not_guessed(self):
        rows = self.rows()
        rows.append(deepcopy(rows[0]))
        before = deepcopy(rows)
        self.assertEqual(builder._canonicalize_previous_bend_imported_slot_names(rows, [1]), before)

    def test_native_fresh_alias_assignment_is_verified_before_source_preservation(self):
        unreal, registry, base, branch, foliage, old, ordered = AssemblyMaterialPreservationTests().fixture()
        metadata = {}
        for index, source in enumerate(['/Game/M/MI_Bark','/Game/M/MI_Leaf']):
            alias = MIC('/Game/Bend/MI_Alias'+str(index), registry[source].parent)
            registry[alias.path]=alias
            foliage.values['materials'][index].values['material_interface']=alias
            metadata[alias.path]={'CodexNormalizedAssemblyBendOwner':'NormalizedAssemblyPartBend/20261010/v2',
                                  'CodexNormalizedAssemblyBendSourceMaterial':source}
        unreal.EditorAssetLibrary.get_metadata_tag=lambda asset,key: metadata.get(asset.path,{}).get(key,'')
        plan=builder.plan_unreal_assembly_material_preservation(unreal,old.path,base,ordered)
        fresh=Mesh('/Game/Trees/Fresh',[Slot('Common',registry['/Game/M/MI_Common']),Slot('Bark',registry['/Game/M/MI_Bark']),
                   Slot('Bark',registry['/Game/Bend/MI_Alias0']),Slot('Leaf',registry['/Game/Bend/MI_Alias1'])],
                   [(branch.path,[0,1]),(foliage.path,[2,3])])
        good=builder.apply_unreal_assembly_material_preservation(unreal,fresh,plan)
        self.assertTrue(good['verified_after_build'])
        self.assertEqual([row.values['material_interface'].path for row in fresh.values['materials']],
                         ['/Game/M/MI_Common_Tint','/Game/M/MI_Bark','/Game/M/MI_Leaf'])
        fresh=Mesh('/Game/Trees/Changed',[Slot('Common',registry['/Game/M/MI_Common']),Slot('Bark',registry['/Game/M/MI_Bark']),
                   Slot('Bark',registry['/Game/M/MI_Leaf']),Slot('Leaf',registry['/Game/Bend/MI_Alias1'])],
                   [(branch.path,[0,1]),(foliage.path,[2,3])])
        with self.assertRaisesRegex(builder.ClusterAssemblyBuildError,'assignment changed'):
            builder.apply_unreal_assembly_material_preservation(unreal,fresh,plan)
        rows = self.rows()
        rows[1]["material"] = "/Game/M/MI_Other"
        before = deepcopy(rows)
        self.assertEqual(builder._canonicalize_previous_bend_imported_slot_names(rows, [1]), before)


if __name__ == '__main__':
    unittest.main()
