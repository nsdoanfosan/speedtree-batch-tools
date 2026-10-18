"""Part-only defaults, real chain copying, repeat ingest, and owned-save order."""
from copy import copy, deepcopy
import json
import re
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
    def set_editor_property(self,key,value):
        if key=='imported_material_slot_name':
            raise RuntimeError('ImportedMaterialSlotName is read-only')
        super().set_editor_property(key,value)
    def export_text(self):
        return '(MaterialSlotName='+json.dumps(self.values['material_slot_name'])+',ImportedMaterialSlotName='+json.dumps(self.values['imported_material_slot_name'])+',MaterialInterface='+json.dumps(self.values['material_interface'].path)+',UVChannelData=(bOverrideDensities=True,bInitialized=True,LocalUVDensities=('+','.join(map(str,self.values['uv_channel_data']))+')))'
    def import_text(self, text):
        fields={key:json.loads(re.search(key+r'=("(?:\\.|[^"\\])*")',text).group(1)) for key in ['MaterialSlotName','ImportedMaterialSlotName','MaterialInterface']}
        self.values={'material_slot_name':fields['MaterialSlotName'],'imported_material_slot_name':fields['ImportedMaterialSlotName'],'material_interface':self.registry[fields['MaterialInterface']],'uv_channel_data':list(map(float,re.search(r'LocalUVDensities=\(([^)]*)\)',text).group(1).split(',')))}


class Settings:
    def __init__(self, text=''): self.text=text
    def export_text(self): return self.text
    def import_text(self, text): self.text=text
    def copy(self):return deepcopy(self)


class Mesh(Material):
    def __init__(self, path, slots, parts=()):
        self.path, self.tags=path, {}
        self.values={'materials':slots, 'nanite_settings':Settings('(Other=Protected,Parts=('+','.join(
            '(MeshObjectPath="'+p+'.'+p.rsplit('/',1)[-1]+'",MaterialRemap=('+','.join(map(str, r))+'))' for p,r in parts)+'))')}


class Harness:
    def __init__(self):
        self.assets={};self.writes=[]
        lib=SimpleNamespace(load_asset=self.load, does_asset_exist=lambda p:self.load(p) is not None,
            get_metadata_tag=lambda a,k:a.tags.get(k,''), set_metadata_tag=lambda a,k,v:a.tags.update({k:v}))
        self.u=SimpleNamespace(Material=Material,MaterialInstanceConstant=MIC,SkeletalMesh=Mesh,EditorAssetLibrary=lib,
            EditorLoadingAndSavingUtils=SimpleNamespace(get_dirty_content_packages=lambda:[]),
            MaterialEditingLibrary=SimpleNamespace(set_material_instance_parent=lambda a,p:a.set_editor_property('parent',p),
                get_material_default_static_switch_parameter_value=lambda a,n:a.values.get('wpo_enabled',False),
                get_material_instance_static_switch_parameter_value=lambda a,n:self.wpo_enabled(a)),
            AssetToolsHelpers=SimpleNamespace(get_asset_tools=lambda:SimpleNamespace(duplicate_asset=self.duplicate)),
            CodexMaterialToolsLibrary=SimpleNamespace(dump_nanite_displacement_runtime=self.runtime),
            CodexAssemblyPartBendUvLibrary=SimpleNamespace(configure_normalized_assembly_bend=self.native))
        self.helpers={'graph':lambda a:deepcopy(a.graph),'roots':lambda a:deepcopy(a.roots),
            'property_text':lambda a,k:'300.0','mi_signature':self.signature,
            'compile_resource':lambda a:{'asset':a.path,'shader_count':4,'native_finish_compilation_waited':True}}
    @staticmethod
    def wpo_enabled(a):
        while isinstance(a,MIC):a=a.values['parent']
        return a.values.get('wpo_enabled',False)
    def add(self, obj): self.assets[obj.path]=obj;Slot.registry=self.assets;return obj
    def load(self, p):
        return next((asset for key,asset in self.assets.items() if nt.identity(key)==nt.identity(p)),None)
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
            assert cfg['expected_materials']==snap['current_materials']
            if cfg['validate_only']:
                snap['status']='native_part_notess_plan_verified_read_only'
            else:
                slots=list(a.values['materials'])
                for row in cfg['append_materials']:
                    slot=deepcopy(slots[row['source_slot']])
                    name=str(slot.values['material_slot_name'])+'_AssemblyNoTess_'+str(row['source_slot'])
                    slot.values['material_slot_name']=name;slot.values['imported_material_slot_name']=name
                    slot.values['material_interface']=self.load(row['material']);slots.append(slot)
                a.values['materials']=slots
                preserved=settings.copy()
                preserved.text=nm.rewrite_nanite_assembly_part_remaps(settings.export_text(),[r['target_remap'] for r in cfg['parts']])
                a.values['nanite_settings']=preserved
                snap['status']='native_part_notess_remap_applied_unsaved'
        return json.dumps(snap)
    @staticmethod
    def admit(plan): return {'verified':True,'materials':plan['materials']}
    @staticmethod
    def admit_prototypes(paths): return True


class PartNoTessTests(unittest.TestCase):
    def test_authored_canonical_wind_is_final_delivery_and_is_not_cloned_again(self):
        import assembly_wpo_contract as wpo
        h,root,common,tint,proto,assembly=self.fixture()
        with patch.object(bend,'material_helpers',return_value=h.helpers):
            first=nt.apply_default_part_notess(h.u,assembly,prepare_dependencies=h.admit,prepare_prototypes=h.admit_prototypes)
            wind_root=h.load(nt.destination(root,master=True))
            wind_root.values['wpo_enabled']=True
            wind_root.tags[wpo.WPO_GATE_TAG]=wpo.WPO_GATE_OWNER
            wind_root.graph['expressions'].append('AuthoredArcWPO')
            wind_leaf=h.load(nt.destination(tint))
            wind_leaf.params['artist_arc_radius']=240.0
            for source in (root,common,tint):
                target=h.load(nt.destination(source,master=isinstance(source,Material)))
                wpo.stamp_wind(h.u,target,source,helpers=h.helpers)
            before=list(h.writes)
            repeated=nt.apply_default_part_notess(h.u,assembly,
                prepare_dependencies=lambda p:self.fail('canonical wind was cloned again'))
            self.assertEqual(h.writes,before)
            self.assertEqual(repeated['material_packages'],[])
            self.assertEqual(wind_leaf.params['artist_arc_radius'],240.0)
            self.assertIn('AuthoredArcWPO',wind_root.graph['expressions'])
            self.assertIs(nt.original_interface(h.u,wind_leaf),tint)

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
            report=nt.apply_default_part_notess(h.u,assembly,prepare_dependencies=h.admit,prepare_prototypes=h.admit_prototypes)
            self.assertEqual(len(report['created_packages']),3)
            target=h.load(report['bindings'][tint.path]);parent=target.values['parent']
            self.assertEqual(target.params,source);self.assertEqual(parent.params,common.params)
            self.assertEqual(parent.values['parent'].values['enable_tessellation'],False)
            self.assertTrue(root.values['enable_tessellation'])
            self.assertIs(proto.values['materials'][0].values['material_interface'],target)
            self.assertEqual(report['prototype_packages'],[proto.path])
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
            first=nt.apply_default_part_notess(h.u,assembly,prepare_dependencies=h.admit,prepare_prototypes=h.admit_prototypes)
            for p in first['created_packages']:h.load(p).tags.clear()
            inspected=nt.inspect_materials(h.u,[tint.path])
            self.assertEqual(len(inspected['write_packages']),3)
            h.load(nt.destination(tint)).params['texture']='USER_CHANGED'
            with self.assertRaisesRegex(RuntimeError,'differs from its source'):
                nt.inspect_materials(h.u,[tint.path])
            self.assertEqual(len(h.writes),3)
    def test_preserved_legacy_derivatives_remain_readonly_but_changed_graph_rejects(self):
        h,root,common,tint,proto,assembly=self.fixture()
        with patch.object(bend,'material_helpers',return_value=h.helpers):
            first=nt.apply_default_part_notess(h.u,assembly,prepare_dependencies=h.admit,prepare_prototypes=h.admit_prototypes)
            existing_before={p:deepcopy(h.load(p).tags) for p in first['material_packages']}
            inspected=nt.inspect_materials(h.u,[tint.path],preserve_verified_legacy=True)
            self.assertEqual(inspected['write_packages'],[])
            second=nt.apply_default_part_notess(h.u,assembly,prepare_dependencies=lambda p:self.fail('read-only references need no checkout'),
                                               preserve_verified_legacy=True)
            self.assertEqual(second['material_packages'],[])
            self.assertEqual(second['created_packages'],[])
            self.assertEqual(existing_before,{p:h.load(p).tags for p in existing_before})
            h.load(nt.destination(tint)).params['texture']='USER_CHANGED'
            with self.assertRaisesRegex(RuntimeError,'differs from its source'):
                nt.inspect_materials(h.u,[tint.path],preserve_verified_legacy=True)

    def test_unowned_canonical_requires_source_stamp_even_when_legacy_preservation_is_enabled(self):
        h,root,common,tint,proto,assembly=self.fixture()
        with patch.object(bend,'material_helpers',return_value=h.helpers):
            first=nt.apply_default_part_notess(h.u,assembly,prepare_dependencies=h.admit,prepare_prototypes=h.admit_prototypes)
            for path in first['created_packages']:h.load(path).tags.clear()
            fresh_proto=h.add(Mesh('/Game/Trees/OtherPart',[Slot('Bark',tint)]))
            fresh_root=h.add(Mesh('/Game/Trees/OtherAssembly',[Slot('Bark',tint)],[(fresh_proto.path,[0])]))
            adopted=nt.apply_default_part_notess(h.u,fresh_root,prepare_dependencies=h.admit,
                prepare_prototypes=h.admit_prototypes,preserve_verified_legacy=True)
            self.assertEqual(set(adopted['material_packages']),set(first['created_packages']))
            self.assertEqual(adopted['created_packages'],[])
            target=h.load(adopted['bindings'][tint.path])
            self.assertEqual(target.tags[nt.OWNER_TAG],nt.OWNER)
            self.assertEqual(target.tags[nt.SOURCE_TAG],tint.path)
            repeated=nt.apply_default_part_notess(h.u,fresh_root,
                prepare_dependencies=lambda p:self.fail('canonical metadata was not retained'),
                preserve_verified_legacy=True)
            self.assertFalse(repeated['changed']);self.assertEqual(repeated['material_packages'],[])

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
    def test_already_off_foliage_still_gets_dedicated_named_mi_and_master(self):
        h,root,common,tint,proto,assembly=self.fixture()
        root.values['enable_tessellation']=False
        with patch.object(bend,'material_helpers',return_value=h.helpers):
            result=nt.apply_default_part_notess(h.u,assembly,prepare_dependencies=h.admit,prepare_prototypes=h.admit_prototypes)
            self.assertEqual(len(result['created_packages']),3)
            self.assertTrue(all(p.endswith('_notess') for p in result['created_packages']))
            self.assertIs(proto.values['materials'][0].values['material_interface'],h.load(nt.destination(tint)))
            repeated=nt.apply_default_part_notess(h.u,assembly,prepare_dependencies=lambda p:self.fail('duplicate creation'),
                                                prepare_prototypes=lambda p:self.fail('duplicate prototype mutation'))
            self.assertFalse(repeated['changed']);self.assertFalse(repeated['prototype_packages'])
    def test_same_short_name_from_distinct_sources_is_rejected_before_creation(self):
        for name in ('MI_Tint','MI_tint'):
            with self.subTest(name=name):
                h,root,common,tint,proto,assembly=self.fixture()
                collision=h.add(MIC('/Game/Other/'+name,root))
                with patch.object(bend,'material_helpers',return_value=h.helpers):
                    with self.assertRaisesRegex(RuntimeError,'canonical name collision') as failure:
                        nt.inspect_materials(h.u,[tint.path,collision.path])
                details=json.loads(str(failure.exception).partition(': ')[2])
                self.assertEqual({row['source'] for row in details},{tint.path,collision.path})
                self.assertEqual({row['source_parent'] for row in details},{common.path,root.path})
                self.assertEqual({row['expected_target'].casefold() for row in details},{nt.destination(tint).casefold()})
                self.assertEqual(h.writes,[])
    def test_case_variant_references_and_metadata_resolve_to_the_same_actual_asset(self):
        h,root,common,tint,proto,assembly=self.fixture()
        with patch.object(bend,'material_helpers',return_value=h.helpers):
            first=nt.apply_default_part_notess(h.u,assembly,prepare_dependencies=h.admit,prepare_prototypes=h.admit_prototypes)
            original_paths={source:target for source,target in first['bindings'].items()}
            target=h.load(original_paths[tint.path]);target.tags[nt.SOURCE_TAG]=tint.path.swapcase()
            inspected=nt.inspect_materials(h.u,[tint.path.lower(),tint.path.upper(),tint.path])
            self.assertEqual(inspected['write_packages'],[])
            self.assertEqual(set(inspected['bindings'].values()),{original_paths[tint.path]})
            self.assertEqual(set(inspected['sources']),{root.path,common.path,tint.path})
            self.assertIs(nt.original_interface(h.u,target),tint)
            canonical=nt.inspect_materials(h.u,[target.path.upper()])
            self.assertEqual(canonical['bindings'][target.path.upper()],target.path)
            self.assertEqual(canonical['write_packages'],[])
            self.assertEqual(nt.destination(target),target.path)
            before={p:deepcopy(a.tags) for p,a in h.assets.items()}
            repeated=nt.apply_default_part_notess(h.u,assembly,prepare_dependencies=lambda p:self.fail('case-only change created packages'))
            self.assertFalse(repeated['changed']);self.assertFalse(repeated['prototypes_edited'])
            self.assertEqual(before,{p:a.tags for p,a in h.assets.items()})
    def test_alias_fname_casing_is_identity_but_unrelated_source_names_are_rejected(self):
        h,root,common,tint,proto,assembly=self.fixture()
        with patch.object(bend,'material_helpers',return_value=h.helpers):
            nt.apply_default_part_notess(h.u,assembly,prepare_dependencies=h.admit,prepare_prototypes=h.admit_prototypes)
            alias=assembly.values['materials'][1]
            alias.values['material_slot_name']='bArK_aSsEmBlYnOtEsS_0'
            unwrapped=nt.unwrap_previous_no_tess_slots(h.u,assembly.values['materials'])
            self.assertIs(unwrapped[1],unwrapped[0])
            alias.values['material_slot_name']='Unrelated_AssemblyNoTess_0'
            with self.assertRaisesRegex(RuntimeError,'source name differs'):
                nt.unwrap_previous_no_tess_slots(h.u,assembly.values['materials'])
    def test_legacy_alias_accepts_mixed_case_package_and_fname_without_rewriting_it(self):
        h,root,common,tint,proto,assembly=self.fixture()
        with patch.object(bend,'material_helpers',return_value=h.helpers):
            initial=nt.inspect_materials(h.u,[tint.path],legacy=True)
            clones={}
            for source,asset in initial['sources'].items():
                path=initial['targets'][source]
                clone=h.duplicate(path.rsplit('/',1)[-1],path.rsplit('/',1)[0],asset)
                if isinstance(asset,Material):
                    clone.values['enable_tessellation']=False
                else:
                    clone.values['parent']=clones[nt.package(asset.values['parent'])]
                clone.tags={nt.OWNER_TAG:nt.LEGACY_OWNER,nt.SOURCE_TAG:source.swapcase(),
                            nt.SIGNATURE_TAG:nt.digest(initial['signatures'][source])}
                clones[source]=clone
            target=clones[tint.path]
            target.path=target.path.swapcase()
            slots=[Slot('Bark',tint),Slot('bArK_aSsEmBlYnOtEsS_0',target)]
            before=[slot.export_text() for slot in slots]
            writes=list(h.writes)
            unwrapped=nt.unwrap_previous_no_tess_slots(h.u,slots)
            self.assertIs(unwrapped[1],slots[0])
            self.assertEqual(before,[slot.export_text() for slot in slots])
            self.assertEqual(writes,h.writes)
    def test_collision_order_does_not_change_diagnostic_or_write_any_assets(self):
        h,root,common,tint,proto,assembly=self.fixture()
        collision=h.add(MIC('/Game/Other/MI_Tint',root))
        messages=[]
        for paths in ([tint.path,collision.path],[collision.path,tint.path]):
            with patch.object(bend,'material_helpers',return_value=h.helpers):
                with self.assertRaisesRegex(RuntimeError,'canonical name collision') as failure:
                    nt.inspect_materials(h.u,paths)
            messages.append(str(failure.exception))
        self.assertEqual(messages[0],messages[1]);self.assertEqual(h.writes,[])
    def test_existing_target_owned_by_another_source_stops_before_creation(self):
        h,root,common,tint,proto,assembly=self.fixture()
        occupied=h.add(MIC(nt.destination(tint),root))
        occupied.tags={nt.OWNER_TAG:nt.OWNER,nt.SOURCE_TAG:'/Game/Other/MI_Tint'}
        before=deepcopy(occupied.tags)
        with patch.object(bend,'material_helpers',return_value=h.helpers):
            with self.assertRaisesRegex(RuntimeError,'canonical name collision') as failure:
                nt.apply_default_part_notess(h.u,assembly,prepare_dependencies=lambda p:self.fail('collision admitted writes'))
        details=json.loads(str(failure.exception).partition(': ')[2])
        self.assertEqual(details[0]['source'],tint.path)
        self.assertEqual(details[0]['expected_target'],occupied.path)
        self.assertEqual(details[0]['existing_owner'],nt.OWNER)
        self.assertEqual(details[0]['existing_source'],'/Game/Other/MI_Tint')
        self.assertEqual(details[0]['existing_parent'],root.path)
        self.assertEqual(occupied.tags,before);self.assertEqual(h.writes,[])
        self.assertEqual(len(assembly.values['materials']),1)
        self.assertIs(proto.values['materials'][0].values['material_interface'],tint)
    def test_casefold_does_not_hide_authored_parameter_or_owner_changes(self):
        h,root,common,tint,proto,assembly=self.fixture()
        with patch.object(bend,'material_helpers',return_value=h.helpers):
            first=nt.apply_default_part_notess(h.u,assembly,prepare_dependencies=h.admit,prepare_prototypes=h.admit_prototypes)
            target=h.load(first['bindings'][tint.path])
            target.tags[nt.OWNER_TAG]='DifferentOwner'
            with self.assertRaisesRegex(RuntimeError,'another owner'):
                nt.inspect_materials(h.u,[tint.path.upper()])
            target.tags[nt.OWNER_TAG]=nt.OWNER
            target.params['texture']=target.params['texture'].lower()
            with self.assertRaisesRegex(RuntimeError,'differs from its source'):
                nt.inspect_materials(h.u,[tint.path.lower()])
    def test_native_remap_preserves_non_exported_node_data_without_text_roundtrip(self):
        h,root,common,tint,proto,assembly=self.fixture()
        settings=assembly.values['nanite_settings']
        settings.hidden_node_transform=[1.23456789012345,987.6543219876543]
        settings.import_text=lambda text:self.fail('Nanite settings text import loses native node precision')
        with patch.object(bend,'material_helpers',return_value=h.helpers):
            nt.apply_default_part_notess(h.u,assembly,prepare_dependencies=h.admit,prepare_prototypes=h.admit_prototypes)
        self.assertEqual(assembly.values['nanite_settings'].hidden_node_transform,settings.hidden_node_transform)
    def test_unadmitted_prototypes_cannot_change_root_or_prototype_assignments(self):
        h,root,common,tint,proto,assembly=self.fixture()
        with patch.object(bend,'material_helpers',return_value=h.helpers):
            with self.assertRaisesRegex(RuntimeError,'prototype admission failed'):
                nt.apply_default_part_notess(h.u,assembly,prepare_dependencies=h.admit,prepare_prototypes=lambda paths:False)
        self.assertEqual(len(assembly.values['materials']),1)
        self.assertIs(proto.values['materials'][0].values['material_interface'],tint)
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
            '/Game/Material/Tree/AssetTree/AssemblyNoTess/M_TreeAsset_Master_notess')
    def test_legacy_off_master_does_not_collide_with_its_new_canonical_derivative(self):
        source='/Game/AssemblyBend/M_Bark_Master_NoTess'
        target=nt.destination(source,master=True)
        self.assertEqual(target,nt.DESTINATION+'/M_Bark_Master_Assembly_notess')
        self.assertNotEqual(source.rsplit('/',1)[-1].casefold(),target.rsplit('/',1)[-1].casefold())
    def test_reed_three_stem_slots_use_stem_master_and_keep_base_stem_on(self):
        h=Harness()
        root=h.add(Material('/Game/Material/Tree/AssetTree/Master/M_TreeAsset_Stem_Master'))
        atlas_root=h.add(Material('/Game/M/M_Atlas'));atlas_root.values['enable_tessellation']=False
        atlas=h.add(MIC('/Game/M/MI_Atlas',atlas_root))
        source_paths=[
            '/Game/Material/Tree/AssetTree/MI/MI_stem_common_01',
            '/Game/Material/Tree/AssetTree/MI/MI_stem_common_end_01',
            '/Game/Material/Tree/AssetTree/MI/MI_stem_common_05']
        stems=[h.add(MIC(p,root)) for p in source_paths]
        base_stem=h.add(MIC('/Game/Material/Tree/AssetTree/MI/MI_stem_common_02',root))
        proto=h.add(Mesh('/Game/Trees/SK_cluster_reed_01_01',
            [Slot('Stem01',stems[0]),Slot('StemEnd',stems[1]),Slot('Stem05',stems[2])]))
        assembly=h.add(Mesh('/Game/Trees/SK_weed_reed_NaniteAssembly',
            [Slot('AtlasA',atlas),Slot('AtlasB',atlas),Slot('Stem01',stems[0]),Slot('BaseStem02',base_stem),
             Slot('StemEnd',stems[1]),Slot('Stem05',stems[2])],[(proto.path,[2,4,5])]))
        original=[s.export_text() for s in assembly.values['materials']]
        with patch.object(bend,'material_helpers',return_value=h.helpers):
            report=nt.apply_default_part_notess(h.u,assembly,prepare_dependencies=h.admit,prepare_prototypes=h.admit_prototypes)
        stem_master=nt.destination(root,master=True)
        self.assertEqual(stem_master,'/Game/Material/Tree/AssetTree/AssemblyNoTess/M_TreeAsset_Stem_Master_notess')
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
            nt.apply_default_part_notess(h.u,assembly,prepare_dependencies=h.admit,prepare_prototypes=h.admit_prototypes)
            h.u.EditorLoadingAndSavingUtils.get_dirty_content_packages=lambda:[root]
            with self.assertRaisesRegex(RuntimeError,'source material is dirty'):
                nt.inspect_materials(h.u,[tint.path])
            result=nt.inspect_materials(h.u,[tint.path],read_only=True)
            self.assertTrue(result['read_only']);self.assertEqual(result['dirty_inputs_observed'],[root.path])


if __name__=='__main__':unittest.main()
