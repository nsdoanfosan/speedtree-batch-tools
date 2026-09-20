import ast
import copy
import hashlib
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

SK_BATCH_DIR=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(SK_BATCH_DIR))
import wind_structure_freshness as freshness


class GuiCacheStructureTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name); self.path=self.root/'wind.json'; self.manifest=self.root/'manifest.json'
        self.expected={'schema_version':1,'recipe_id':'generic','recipe_sha256':'a'*64}
        hashes={'spm':'b'*64}; digest=hashlib.sha256(json.dumps(hashes,sort_keys=True,separators=(',',':')).encode()).hexdigest()
        self.wind={'SkeletonContract':{'BoneNameIndexParentSha1':'c'*40,'Bones':[{}]},
                   'WindStructureModifierContract':{'SchemaVersion':1,'RecipeId':'generic','RecipeSha256':'a'*64,
                    'Mode':'BoundedArtApproximation','ValidationOnly':True,'BendRateScale':1.,'TorsionGain':1.,'FlutterGain':1.,
                    'SourceHashes':hashes,'SourceDigest':digest,'BoneNameIndexParentSha1':'c'*40,'Bones':[{}]}}
        self.item={'queue_id':'plant','fingerprint':'immutable','wind_json':str(self.path),
                   'wind_file':{'path':str(self.path)},'wind_policy':{'requires_json':True},'exported_files':[]}

    def invoke_gui(self):
        self.path.write_text(json.dumps(self.wind),encoding='utf-8')
        self.manifest.write_text(json.dumps({'items':[self.item]}),encoding='utf-8')
        tree=ast.parse((SK_BATCH_DIR/'sk_batch_gui.pyw').read_text(encoding='utf-8'))
        app_class=next(node for node in tree.body if isinstance(node,ast.ClassDef) and node.name=='App')
        method=next(node for node in app_class.body if isinstance(node,ast.FunctionDef) and node.name=='_cached_manifest_item')
        namespace={'Path':Path,'json':json,'manifest_item_has_current_skeleton_root_export':lambda item:True,
                   'manifest_item_files_match':lambda item:True,'cached_manifest_structure_status':freshness.cached_manifest_structure_status}
        exec(compile(ast.Module(body=[method],type_ignores=[]),'actual_gui_cache_method','exec'),namespace)
        app=types.SimpleNamespace(force_rerun=False,state={'plant':{'push_export_cache':{'source_fingerprint':'source','manifest':str(self.manifest)}}},log=mock.Mock())
        original=self.manifest.read_bytes()
        with mock.patch.object(freshness,'installed_structure_identity',return_value=self.expected):
            result=namespace['_cached_manifest_item'](app,'plant','source')
        self.assertEqual(self.manifest.read_bytes(),original)
        return result,app

    def test_current_contract_retains_cached_item(self):
        result,app=self.invoke_gui(); self.assertEqual(result,self.item); app.log.assert_not_called()

    def test_missing_contract_invalidates_cache_with_repair_guidance(self):
        del self.wind['WindStructureModifierContract']
        result,app=self.invoke_gui(); self.assertIsNone(result)
        self.assertIn('Repair/export',app.log.call_args.args[0])

    def test_previous_recipe_invalidates_cache(self):
        self.wind['WindStructureModifierContract']['RecipeSha256']='d'*64
        result,app=self.invoke_gui(); self.assertIsNone(result); self.assertIn('recipe mismatch',app.log.call_args.args[0])

    def test_explicit_nonwind_prototype_does_not_require_recipe(self):
        self.item={'queue_id':'plant','wind_policy':{'requires_json':False}}
        result,app=self.invoke_gui(); self.assertEqual(result,self.item)

    def test_ambiguous_missing_wind_does_not_silently_pass(self):
        self.item={'queue_id':'plant'}
        result,app=self.invoke_gui(); self.assertIsNone(result); self.assertIn('Repair/export',app.log.call_args.args[0])

    def test_disagreeing_json_and_fingerprint_paths_rejected(self):
        self.item['wind_file']['path']=str(self.root/'other.json')
        result,app=self.invoke_gui(); self.assertIsNone(result); self.assertIn('paths disagree',app.log.call_args.args[0])

    def test_installed_identity_reads_literals_without_importing_code(self):
        recipe={'id':'recipe_v4','value':1.25}
        (self.root/'evaluate_export_loads.py').write_text('raise RuntimeError("must not execute")\nRECIPE='+repr(recipe),encoding='utf-8')
        (self.root/'wind_structure_contract.py').write_text('import bpy\nSCHEMA_VERSION=1\n',encoding='utf-8')
        expected=hashlib.sha256(json.dumps(recipe,sort_keys=True,separators=(',',':')).encode()).hexdigest()
        self.assertEqual(freshness.installed_structure_identity(self.root),{'schema_version':1,'recipe_id':'recipe_v4','recipe_sha256':expected})


if __name__=='__main__':unittest.main()
