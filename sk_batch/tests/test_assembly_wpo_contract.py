"""Exercise complete WPO gating and exact original/canonical migration identity."""
import json
import sys
import unittest
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

SK_BATCH = Path(__file__).resolve().parents[1]
if str(SK_BATCH) not in sys.path:
    sys.path.insert(0, str(SK_BATCH))
import assembly_wpo_contract as wpo
import assembly_part_notess as nt
from sk_batch.tests.test_normalized_material_hierarchy import Harness, Material, MIC


class Node:
    def __init__(self, name, kind, **values):
        self.name, self.kind, self.values, self.inputs = name, kind, values, {}
    def get_name(self): return self.name
    def get_class(self): return SimpleNamespace(get_name=lambda: self.kind)
    def get_editor_property(self, key): return self.values[key]
    def set_editor_property(self, key, value): self.values[key] = value


class GateHarness:
    def __init__(self):
        self.material = SimpleNamespace(tags={}, nodes=[], modify=lambda: None,
            get_path_name=lambda: '/Game/M/M_Foliage.M_Foliage')
        self.old = Node('OriginalWind', 'MaterialExpressionAdd')
        self.material.nodes.append(self.old)
        self.material.roots = {'BaseColor': ('Color', 0), 'Displacement': ('Height', 0),
                               'MP_WorldPositionOffset': (self.old.name, 0)}
        self.u = SimpleNamespace(Material=SimpleNamespace, MaterialExpressionStaticSwitchParameter='MaterialExpressionStaticSwitchParameter',
            MaterialExpressionConstant3Vector='MaterialExpressionConstant3Vector', LinearColor=lambda *args: list(args),
            MaterialProperty=SimpleNamespace(MP_WORLD_POSITION_OFFSET='MP_WorldPositionOffset'),
            EditorAssetLibrary=SimpleNamespace(get_metadata_tag=lambda a,k:a.tags.get(k,''),
                set_metadata_tag=lambda a,k,v:a.tags.update({k:v})),
            MaterialEditingLibrary=SimpleNamespace(get_material_expressions=lambda a:a.nodes,
                create_material_expression=self.create, connect_material_expressions=self.connect,
                connect_material_property=lambda node,output,prop:self.material.roots.update({prop:(node.name,0)}) or True,
                get_material_expression_output_names=lambda node:[''],
                get_material_default_static_switch_parameter_value=lambda a,name:
                    next(node.values['default_value'] for node in a.nodes if node.values.get('parameter_name')==name)))
        self.helpers = {'roots':self.roots}
    def create(self, material, kind, x, y):
        node=Node('New'+str(len(material.nodes)),kind)
        material.nodes.append(node)
        return node
    @staticmethod
    def connect(source, output, target, pin):
        target.inputs[pin]=source
        return True
    @staticmethod
    def roots(material):
        return {'root_inputs':[{'property_name':prop,'connected':True,'source_name':source,'output_index':index}
            for prop,(source,index) in material.roots.items()]}


class AssemblyWpoContractTests(unittest.TestCase):
    def test_false_blocks_the_complete_existing_wpo_and_true_preserves_its_graph(self):
        h=GateHarness(); before=deepcopy(h.material.roots)
        wpo.install_wpo_gate(h.u,h.material,helpers=h.helpers)
        gate=next(node for node in h.material.nodes if node.values.get('parameter_name')==wpo.WPO_SWITCH)
        self.assertFalse(gate.values['default_value'])
        self.assertIs(gate.inputs['True'],h.old)
        self.assertEqual(gate.inputs['False'].values['constant'],[0,0,0,0])
        self.assertEqual(h.material.roots['BaseColor'],before['BaseColor'])
        self.assertEqual(h.material.roots['Displacement'],before['Displacement'])
        count=len(h.material.nodes)
        h.material.get_path_name=lambda:nt.DESTINATION+'/M_Foliage_notess.M_Foliage_notess'
        wpo.install_wpo_gate(h.u,h.material,enabled=True,helpers=h.helpers)
        self.assertEqual(len(h.material.nodes),count)
        self.assertTrue(gate.values['default_value'])

    def test_canonical_outer_gate_enables_copied_source_gate_without_modifying_source(self):
        h=GateHarness()
        wpo.install_wpo_gate(h.u,h.material,helpers=h.helpers)
        original_gate=next(node for node in h.material.nodes if node.values.get('parameter_name')==wpo.WPO_SWITCH)
        # The native normalized wrapper inserts its bend output outside the
        # copied source gate. Canonical gating must cover that whole output.
        normalized=Node('NormalizedBendAdd','MaterialExpressionAdd')
        h.material.nodes.append(normalized)
        h.material.roots['MP_WorldPositionOffset']=(normalized.name,0)
        h.material.get_path_name=lambda:nt.DESTINATION+'/M_Foliage_notess.M_Foliage_notess'
        wpo.install_wpo_gate(h.u,h.material,enabled=True,helpers=h.helpers)
        gates=[node for node in h.material.nodes if node.values.get('parameter_name')==wpo.WPO_SWITCH]
        self.assertEqual(len(gates),2)
        self.assertTrue(all(node.values['default_value'] for node in gates))
        outer=next(node for node in gates if node.name==h.material.roots['MP_WorldPositionOffset'][0])
        self.assertIs(outer.inputs['True'],normalized)
        self.assertEqual(outer.inputs['False'].values['constant'],[0,0,0,0])

    def test_original_source_follows_owned_carc_and_notess_and_rejects_unowned_legacy(self):
        h=Harness();source=h.add(Material('/Game/M/M_Foliage'))
        carc=h.add(Material('/Game/Material/Tree/AssetTree/AssemblyBend/CArc/M_Foliage_hash_CArc_Master'))
        carc.tags.update(CodexAssemblyLeafCArcOwner='AssemblyLeafCArcRootBinding/20261013/v1',
                         CodexAssemblyLeafCArcSourceMaterial=source.path)
        off=h.add(Material(nt.destination(source,master=True)))
        off.tags.update({nt.OWNER_TAG:nt.OWNER,nt.SOURCE_TAG:carc.path})
        self.assertIs(wpo.original_source(h.unreal,off),source)
        with self.assertRaisesRegex(RuntimeError,'requires canonical _notess migration'):
            wpo.original_source(h.unreal,off,reject_unmigrated_wind=True,helpers=h.helpers)
        legacy=h.add(Material('/Game/Material/Tree/AssetTree/AssemblyNoTess/M_Foliage_CArc_PartNoTess_123456789abc'))
        with self.assertRaisesRegex(RuntimeError,'lacks exact original'):
            wpo.original_source(h.unreal,legacy)

    def test_authored_canonical_proof_retains_graph_and_exact_original_and_detects_edits(self):
        h=Harness();source=h.add(Material('/Game/M/M_Foliage'))
        target=h.add(Material(nt.destination(source,master=True)));target.tessellation=False
        target.graph['expressions'].append({'name':'AuthoredArc','radius':240})
        target.switches[wpo.WPO_SWITCH]=True
        proof=wpo.stamp_wind(h.unreal,target,source,helpers=h.helpers)
        self.assertEqual(proof['source'],source.path)
        self.assertEqual(wpo.verify_wind(h.unreal,target,helpers=h.helpers),proof)
        self.assertFalse(source.switches[wpo.WPO_SWITCH])
        target.graph['expressions'][-1]['radius']=300
        with self.assertRaisesRegex(RuntimeError,'appearance changed'):
            wpo.verify_wind(h.unreal,target,helpers=h.helpers)
        target.graph['expressions'][-1]['radius']=240
        source.graph['expressions'][0]['defaults']['Roughness']=.2
        with self.assertRaisesRegex(RuntimeError,'original source changed'):
            wpo.verify_wind(h.unreal,target,helpers=h.helpers)

    def test_stamping_refuses_carc_names_or_noncanonical_target(self):
        h=Harness();source=h.add(Material('/Game/M/M_Foliage'))
        bad=h.add(Material('/Game/Material/Tree/AssetTree/AssemblyNoTess/M_Foliage_hash_CArc_notess'))
        bad.tessellation=False
        with self.assertRaisesRegex(RuntimeError,'canonical _notess'):
            wpo.stamp_wind(h.unreal,bad,source,helpers=h.helpers)

    def test_global_gate_cannot_enable_wpo_on_a_non_notess_master(self):
        h=GateHarness()
        with self.assertRaisesRegex(RuntimeError,'only be enabled'):
            wpo.install_wpo_gate(h.u,h.material,enabled=True,helpers=h.helpers)
        self.assertEqual(h.material.nodes,[h.old])

    def test_stamping_does_not_merge_different_originals_with_one_short_name(self):
        h=Harness();source=h.add(Material('/Game/A/M_Foliage'))
        other=h.add(Material('/Game/B/M_Foliage'))
        target=h.add(Material(nt.destination(source,master=True)))
        target.tessellation=False;target.switches[wpo.WPO_SWITCH]=True
        wpo.stamp_wind(h.unreal,target,source,helpers=h.helpers)
        before=deepcopy(target.tags)
        with self.assertRaisesRegex(RuntimeError,'different original source'):
            wpo.stamp_wind(h.unreal,target,other,helpers=h.helpers)
        self.assertEqual(target.tags,before)


if __name__=='__main__':
    unittest.main()
