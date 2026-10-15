import sys
from pathlib import Path
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from normalized_assembly_bend import align_verified_function_call_names, NormalizedAssemblyBendError

class FunctionNameCorrespondenceTests(unittest.TestCase):
    def test_exact_function_asset_maps_names_and_connected_inputs(self):
        source={'expressions':[{'name':'Call_2','class':'MaterialExpressionMaterialFunctionCall','function':'/Game/F/Field'},
            {'name':'Custom','inputs':[{'source_name':'Call_2','output_index':0}]}]}
        roots={'root_inputs':[{'source_name':'Call_2','mask_r':True}]}
        target={'expressions':[{'name':'Call_4','class':'MaterialExpressionMaterialFunctionCall','function':'/Game/F/Field'},
            {'name':'Call_2','class':'MaterialExpressionMaterialFunctionCall','function':'/Game/F/Bend'}]}
        graph,actual_roots=align_verified_function_call_names(source,roots,target)
        self.assertEqual(graph['expressions'][0]['name'],'Call_4')
        self.assertEqual(graph['expressions'][1]['inputs'][0]['source_name'],'Call_4')
        self.assertEqual(actual_roots['root_inputs'][0]['source_name'],'Call_4')
        self.assertEqual(source['expressions'][0]['name'],'Call_2')
    def test_missing_or_ambiguous_function_is_not_guessed(self):
        source={'expressions':[{'name':'Call','class':'MaterialExpressionMaterialFunctionCall','function':'/Game/F/Field'}]}
        for target in [{'expressions':[]},{'expressions':[{'name':'A','class':'MaterialExpressionMaterialFunctionCall','function':'/Game/F/Field'},{'name':'B','class':'MaterialExpressionMaterialFunctionCall','function':'/Game/F/Field'}]}]:
            with self.assertRaises(NormalizedAssemblyBendError): align_verified_function_call_names(source,{},target)
    def test_parameter_values_and_channel_masks_are_retained_for_strict_validation(self):
        source={'expressions':[{'name':'Parameter','default_value':0.7,'inputs':[{'source_name':'Other','mask_a':True}]}]}
        graph,_=align_verified_function_call_names(source,{}, {'expressions':[]})
        self.assertEqual(graph,source)

if __name__=='__main__': unittest.main()
