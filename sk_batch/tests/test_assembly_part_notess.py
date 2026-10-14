"""Part-only defaults, real chain copying, repeat ingest, and owned-save order."""
from copy import copy, deepcopy
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import assembly_part_notess as nt
import normalized_assembly_bend as bend
import nanite_assembly_materials as nm
import cluster_assembly_builder as builder


class Value:
    def __init__(self, **values): self.values = values
    def get_editor_property(self, key): return self.values[key]
    def set_editor_property(self, key, value): self.values[key] = value
    def copy(self): return Value(**deepcopy(self.values))
    def export_text(self):
        return "(bOverride_bEnableTessellation=" + str(self.values.get('override_b_enable_tessellation', False)) + ",bEnableTessellation=" + str(self.values.get('enable_tessellation', False)) + ",MaxWorldPositionOffsetDisplacement=300.0,TwoSided=True)"


class Material:
    def __init__(self, path):
        self.path, self.tags = path, {}
        self.values = {"enable_tessellation": True}
        self.graph = {"material": self.get_path_name(), "expressions": ["WPO", "UV3", "Masked", "Height"]}
        self.roots = {"material": self.get_path_name(), "inputs": ["WPO", "Displacement", "BaseColor"]}
    def get_path_name(self): return self.path + "." + self.path.rsplit('/', 1)[-1]
    def get_editor_property(self, key): return self.values[key]
    def set_editor_property(self, key, value): self.values[key] = value
    def modify(self): pass


class MIC:
    def __init__(self, path, parent):
        self.path, self.tags = path, {}
        self.values = {"parent": parent, "base_property_overrides": Value(override_b_enable_tessellation=False, enable_tessellation=False)}
        self.params = {"static": {"Wind": True}, "tint": [0.2, 0.3, 0.4], "texture": "OriginalBark"}
    get_path_name = Material.get_path_name
    get_editor_property = Material.get_editor_property
    set_editor_property = Material.set_editor_property
    modify = Material.modify


class Slot(Value):
    registry = {}
    def __init__(self, name=None, mat=None):
        super().__init__(material_slot_name=name, imported_material_slot_name=name, material_interface=mat, uv_channel_data=[1.5, 2.5])
    def export_text(self): return json.dumps({**self.values, 'material_interface': self.values['material_interface'].path})
    def import_text(self, text):
        self.values = json.loads(text); self.values['material_interface'] = self.registry[self.values['material_interface']]


class Settings:
    def __init__(self, text=''): self.text=text
    def export_text(self): return self.text
    def import_text(self, text): self.text=text


class Mesh(Material):
    def __init__(self, path, slots, parts=()):
        self.path, self.tags=path, {}
        self.values={'materials':slots, 'nanite_settings':Settings('(Other=Protected,Parts=('+','.join(
            '(MeshObjectPath="'+p+'.'+p.rsplit('/',1)[-1]+'",MaterialRemap=('+','.join(map(str, r))+'))' for p,r in parts)+'))')}


class Harness:
    def __init__(self):
        self.assets={};self.writes=[]
        lib=SimpleNamespace(load_asset=self.load, does_asset_exist=lambda p:p in self.assets,
            get_metadata_tag=lambda a,k:a.tags.get(k,''), set_metadata_tag=lambda a,k,v:a.tags.update({k:v}))
        self.u=SimpleNamespace(Material=Material,MaterialInstanceConstant=MIC,SkeletalMesh=Mesh,EditorAssetLibrary=lib,
            EditorLoadingAndSavingUtils=SimpleNamespace(get_dirty_content_packages=lambda:[]),
            MaterialEditingLibrary=SimpleNamespace(set_material_instance_parent=lambda a,p:a.set_editor_property('parent',p)),
            AssetToolsHelpers=SimpleNamespace(get_asset_tools=lambda:SimpleNamespace(duplicate_asset=self.duplicate)),
            CodexMaterialToolsLibrary=SimpleNamespace(dump_nanite_displacement_runtime=self.runtime),
            CodexAssemblyPartBendUvLibrary=SimpleNamespace(configure_normalized_assembly_bend=self.native))
        self.helpers={'graph':lambda a:deepcopy(a.graph),'roots':lambda a:deepcopy(a.roots),
            'property_text':lambda a,k:'300.0','mi_signature':self.signature,
            'compile_resource':lambda a:{'asset':a.path,'shader_count':4,'native_finish_compilation_waited':True}}
    def add(self, obj): self.assets[obj.path]=obj;Slot.registry=self.assets;return obj
    def load(self, p): return self.assets.get(nt.package(p))
    def duplicate(self, name, folder, source):
        obj=copy(source);obj.path=folder+'/'+name;obj.values=deepcopy(source.values);obj.tags={}
        if isinstance(obj,MIC): obj.params=deepcopy(source.params);obj.values['parent']=source.values['parent']
        else:
            obj.graph=deepcopy(source.graph);obj.graph['material']=obj.get_path_name()
            obj.roots=deepcopy(source.roots);obj.roots['material']=obj.get_path_name()
        self.writes.append(obj.path);return self.add(obj)
    def signature(self, a):
        return {'parent':a.values['parent'].get_path_name(), 'base_property_overrides':a.values['base_property_overrides'].export_text(),
            'layers':{'material_interface':a.get_path_name()}, 'authored':deepcopy(a.params)}
    def runtime(self, paths):
        rows=[]
        for p in paths:
            a=self.load(p);root=a
            while isinstance(root,MIC):root=root.get_editor_property('parent')
            tess=root.get_editor_property('enable_tessellation')
            rows.append({'path':a.get_path_name(),'loaded':True,'interface_tessellation_enabled':tess,
                'base_tessellation_enabled':tess,'relevance_uses_displacement':tess,'relevance_uses_wpo':True,
                'interface_max_wpo_displacement':300.0,'interface_displacement_scaling':{'magnitude':10.0}})
        return json.dumps({'materials':rows})
    def native(self, p, text):
        a=self.load(p);cfg=json.loads(text);settings=a.get_editor_property('nanite_settings')
        parts=nm.parse_nanite_assembly_part_remaps(settings.export_text())
        snap={'status':'native_part_remap_snapshot_read_only','current_materials':[nt.package(s.values['material_interface']) for s in a.values['materials']],
            'current_parts':[{'index':i,'mesh':nt.package(r['mesh']),'expected_remap':r['remap']} for i,r in enumerate(parts)],
            'root_source_mesh_description_sha1':'BODY','nodes_binary_sha1':'NODES','reference_skeleton_sha1':'BONES','materials_authored_sha1':'SLOTS'}
        if 'parts' in cfg:
            assert cfg['validate_only'] is True
            assert cfg['expected_materials']==snap['current_materials']
            snap['status']='native_part_notess_plan_verified_read_only'
        return json.dumps(snap)
    @staticmethod
    def admit(plan): return {'verified':True,'materials':plan['materials']}


class PartNoTessTests(unittest.TestCase):
    def fixture(self):
        h=Harness();root=h.add(Material('/Game/M/M_Master'));common=h.add(MIC('/Game/M/MI_Common',root))
        tint=h.add(MIC('/Game/M/MI_Tint',common));tint.params['tint']=[.8,.6,.4]
        proto=h.add(Mesh('/Game/Trees/Part',[Slot('Bark',tint)]))
        assembly=h.add(Mesh('/Game/Trees/Assembly',[Slot('Bark',tint)],[(proto.path,[0])]))
        return h,root,common,tint,proto,assembly
    def test_all_parts_including_no_wind_share_deduplicated_part_only_alias(self):
        snap={'current_materials':['/Game/M/MI_Bark','/Game/M/MI_Off'], 'current_parts':[
            {'index':0,'mesh':'P','expected_remap':[0,1]},{'index':1,'mesh':'P','expected_remap':[0,1]}]}
        plan=nt.plan_remaps(snap,{'/Game/M/MI_Bark':'/Game/NoTess','/Game/M/MI_Off':'/Game/M/MI_Off'},{'P':2})
        self.assertEqual(plan['append_materials'],[{'source_slot':0,'material':'/Game/NoTess'}])
        self.assertEqual([r['target_remap'] for r in plan['parts']],[[2,1],[2,1]])
        self.assertEqual(snap['current_materials'],['/Game/M/MI_Bark','/Game/M/MI_Off'])
    def test_complete_chain_wpo_parameters_base_and_prototype_preserved_and_second_run_reuses(self):
        h,root,common,tint,proto,assembly=self.fixture()
        original=assembly.values['materials'][0].export_text();source=deepcopy(tint.params)
        with patch.object(bend,'material_helpers',return_value=h.helpers):
            report=nt.apply_default_part_notess(h.u,assembly,prepare_dependencies=h.admit)
            self.assertEqual(len(report['created_packages']),3)
            target=h.load(report['bindings'][tint.path]);parent=target.values['parent']
            self.assertEqual(target.params,source);self.assertEqual(parent.params,common.params)
            self.assertEqual(parent.values['parent'].values['enable_tessellation'],False)
            self.assertTrue(root.values['enable_tessellation'])
            self.assertIs(proto.values['materials'][0].values['material_interface'],tint)
            self.assertEqual(assembly.values['materials'][0].export_text(),original)
            self.assertEqual(assembly.values['materials'][1].values['uv_channel_data'],[1.5,2.5])
            self.assertEqual(nm.parse_nanite_assembly_part_remaps(assembly.values['nanite_settings'].export_text())[0]['remap'],[1])
            second=nt.apply_default_part_notess(h.u,assembly,prepare_dependencies=h.admit)
            self.assertFalse(second['changed']);self.assertEqual(second['created_packages'],[])
            self.assertEqual(len(assembly.values['materials']),2)
            unwrapped=nt.unwrap_previous_no_tess_slots(h.u,assembly.values['materials'])
            self.assertIs(unwrapped[0],unwrapped[1]);self.assertEqual(tint.params,source)
    def test_legacy_derivative_adopted_only_if_unchanged_and_collision_rejected(self):
        h,root,common,tint,proto,assembly=self.fixture()
        with patch.object(bend,'material_helpers',return_value=h.helpers):
            first=nt.apply_default_part_notess(h.u,assembly,prepare_dependencies=h.admit)
            for p in first['created_packages']:h.load(p).tags.clear()
            inspected=nt.inspect_materials(h.u,[tint.path])
            self.assertEqual(len(inspected['write_packages']),3)
            h.load(nt.destination(tint)).params['texture']='USER_CHANGED'
            with self.assertRaisesRegex(RuntimeError,'differs from its source'):
                nt.inspect_materials(h.u,[tint.path])
            self.assertEqual(len(h.writes),3)
    def test_forged_alias_cannot_replace_base_or_an_unrelated_material(self):
        h,root,common,tint,proto,assembly=self.fixture()
        fake=h.add(MIC(nt.destination(tint),root));fake.values['parent']=root
        slots=[Slot('Bark',tint),Slot('Bark_AssemblyNoTess_0',fake)]
        with patch.object(bend,'material_helpers',return_value=h.helpers):
            with self.assertRaisesRegex(RuntimeError,'parent chain|differs from its source|TessON'):
                nt.unwrap_previous_no_tess_slots(h.u,slots)
        self.assertTrue(root.values['enable_tessellation'])
    def test_runtime_mismatch_cannot_claim_no_tess_or_change_wpo(self):
        off={'interface_tessellation_enabled':False,'base_tessellation_enabled':False,'relevance_uses_displacement':False,'relevance_uses_wpo':True}
        with self.assertRaisesRegex(RuntimeError,'relevance_uses_wpo'):
            nt._assert_equivalent_runtime({'relevance_uses_wpo':False},off)
    def test_derived_uv_density_cache_can_change_but_authored_override_cannot(self):
        template='(MaterialInterface="Keep",UVChannelData=(bInitialized={0},bOverrideDensities={1},LocalUVDensities=({2})))'
        self.assertEqual(nt.authored_slot_export(template.format('False','False','0,0,0,0')),
                         nt.authored_slot_export(template.format('True','False','2,3,4,5')))
        self.assertNotEqual(nt.authored_slot_export(template.format('True','True','0,0,0,0')),
                            nt.authored_slot_export(template.format('True','True','2,3,4,5')))
    def test_legacy_willow_off_parent_is_retained_only_for_an_exact_copy(self):
        h=Harness();expected=h.add(Material('/Game/Material/Tree/AssetTree/AssemblyBend/M_Bark_AspectBend_Master'))
        leaf=h.add(MIC('/Game/Material/Tree/AssetTree/AssemblyBend/MI_Bark_AspectBend',expected))
        helpers={**h.helpers,'digest':nt.digest}
        leaf.tags[bend.SIGNATURE_TAG]=nt.digest(helpers['mi_signature'](leaf))
        off=h.duplicate(expected.path.rsplit('/',1)[-1]+'_NoTess',expected.path.rsplit('/',1)[0],expected)
        off.values['enable_tessellation']=False;leaf.values['parent']=off
        self.assertTrue(bend.verified_legacy_notess_parent(h.u,helpers,leaf,expected.path))
        self.assertIs(leaf.values['parent'],off)
        leaf.params['tint']=[1,0,0]
        with self.assertRaisesRegex(bend.NormalizedAssemblyBendError,'changed more than'):
            bend.verified_legacy_notess_parent(h.u,helpers,leaf,expected.path)
        off.graph['expressions'].append('CHANGED_WPO')
        with self.assertRaisesRegex(bend.NormalizedAssemblyBendError,'original graph/WPO'):
            bend.verified_legacy_notess_parent(h.u,helpers,leaf,expected.path)
    def test_canonical_existing_derivative_names_match_the_adopted_production_paths(self):
        self.assertEqual(nt.destination('/Game/Material/Tree/AssetTree/Master/M_TreeAsset_Master',master=True),
            '/Game/Material/Tree/AssetTree/AssemblyNoTess/M_TreeAsset_Master_PartNoTess_1b362b0a33a5')
    def test_reed_three_stem_slots_use_stem_master_and_keep_base_stem_on(self):
        h=Harness()
        root=h.add(Material('/Game/Material/Tree/AssetTree/AssemblyBend/CArc/M_TreeAsset_Stem_Master_6664166816ef_CArc_Master'))
        atlas_root=h.add(Material('/Game/M/M_Atlas'));atlas_root.values['enable_tessellation']=False
        atlas=h.add(MIC('/Game/M/MI_Atlas',atlas_root))
        source_paths=[
            '/Game/Material/Tree/AssetTree/AssemblyBend/CArc/MI_stem_common_01_23b355ad2f70_CArc',
            '/Game/Material/Tree/AssetTree/AssemblyBend/CArc/MI_stem_common_end_01_571b64f8aa6d_CArc',
            '/Game/Material/Tree/AssetTree/AssemblyBend/CArc/MI_stem_common_05_07235339a85d_CArc']
        stems=[h.add(MIC(p,root)) for p in source_paths]
        base_stem=h.add(MIC('/Game/Material/Tree/AssetTree/MI/MI_stem_common_02',root))
        proto=h.add(Mesh('/Game/Trees/SK_cluster_reed_01_01',
            [Slot('Stem01',stems[0]),Slot('StemEnd',stems[1]),Slot('Stem05',stems[2])]))
        assembly=h.add(Mesh('/Game/Trees/SK_weed_reed_NaniteAssembly',
            [Slot('AtlasA',atlas),Slot('AtlasB',atlas),Slot('Stem01',stems[0]),Slot('BaseStem02',base_stem),
             Slot('StemEnd',stems[1]),Slot('Stem05',stems[2])],[(proto.path,[2,4,5])]))
        original=[s.export_text() for s in assembly.values['materials']]
        with patch.object(bend,'material_helpers',return_value=h.helpers):
            report=nt.apply_default_part_notess(h.u,assembly,prepare_dependencies=h.admit)
        stem_master=nt.destination(root,master=True)
        self.assertEqual(stem_master,'/Game/Material/Tree/AssetTree/AssemblyNoTess/M_TreeAsset_Stem_Master_6664166816ef_CArc_Master_PartNoTess_9744956b76d9')
        self.assertEqual(len(report['created_packages']),4)
        for stem in stems:
            target=h.load(report['bindings'][stem.path])
            self.assertEqual(target.values['parent'].path,stem_master)
            self.assertFalse(target.values['parent'].values['enable_tessellation'])
            self.assertEqual(target.params,stem.params)
        self.assertEqual([s.export_text() for s in assembly.values['materials'][:6]],original)
        self.assertTrue(root.values['enable_tessellation'])
        self.assertIs(assembly.values['materials'][3].values['material_interface'],base_stem)
        self.assertEqual(nm.parse_nanite_assembly_part_remaps(assembly.values['nanite_settings'].export_text())[0]['remap'],[6,7,8])
    def test_dirty_source_can_be_inspected_readonly_but_cannot_be_used_for_writes(self):
        h,root,common,tint,proto,assembly=self.fixture()
        with patch.object(bend,'material_helpers',return_value=h.helpers):
            nt.apply_default_part_notess(h.u,assembly,prepare_dependencies=h.admit)
            h.u.EditorLoadingAndSavingUtils.get_dirty_content_packages=lambda:[root]
            with self.assertRaisesRegex(RuntimeError,'source material is dirty'):
                nt.inspect_materials(h.u,[tint.path])
            result=nt.inspect_materials(h.u,[tint.path],read_only=True)
            self.assertTrue(result['read_only']);self.assertEqual(result['dirty_inputs_observed'],[root.path])


if __name__=='__main__':unittest.main()
