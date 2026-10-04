import importlib.util
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch

PATH=Path(__file__).resolve().parents[1]/"jobs/debris_terrain_group_job.py"
with patch.dict(sys.modules,{"bpy":types.ModuleType("bpy")}):
    spec=importlib.util.spec_from_file_location("debris_worker",PATH)
    worker=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(worker)


class DebrisTerrainWorkerTests(unittest.TestCase):
    def parse(self,*args):
        with patch.object(sys,"argv",["blender","--",*args]):
            return worker.parse_args()

    def test_generic_groups_only_negotiates_new_provider_without_unreal_or_send2ue(self):
        args,forward=self.parse("--source-path","D:/Art/SK_oak.blend","--group-report","build.json",
                                "--groups-only","--max-groups","12")
        self.assertEqual(args.max_groups,12)
        self.assertEqual(forward,[])
        requirements=worker._runtime_requirements(args)
        self.assertEqual(set(requirements),{"speedtree_bone_weight_repair"})
        self.assertIn("debris_terrain_prefab_v1",requirements["speedtree_bone_weight_repair"])

    def test_full_generic_keeps_wrapper_arguments_out_of_existing_push(self):
        args,forward=self.parse("--source-path","D:/Art/SK_oak.blend","--group-report","build.json",
                                "--source-skeletal-mesh","/Game/Leaves/SK_oak","--max-groups","32",
                                "--prefab-manifest","prefab.json","--report","push.json","--skip-wind")
        self.assertEqual(forward,["--report","push.json","--skip-wind"])
        requirements=worker._runtime_requirements(args)
        self.assertIn("headless_export_v1",requirements["send2ue"])
        self.assertIn("material_handoff_v1",requirements["speedtree_bone_weight_repair"])

    def test_legacy_evidence_and_control_modes_remain_available(self):
        args,forward=self.parse("--group-evidence","evidence.json","--asset-suffix","arbitrary",
                                "--group-report","build.json","--groups-only")
        self.assertNotIn("debris_terrain_prefab_v1",worker._runtime_requirements(args)["speedtree_bone_weight_repair"])
        args,forward=self.parse("--group-evidence","evidence.json","--asset-suffix","arbitrary",
                                "--group-report","build.json","--control-only",
                                "--control-asset-name","SM_oak_Control","--skip-wind")
        self.assertIn("debris_terrain_control_v1",worker._runtime_requirements(args)["speedtree_bone_weight_repair"])

    def test_invalid_generic_budget_source_contract_and_modes_fail_before_build(self):
        base=["--source-path","D:/Art/SK_oak.blend","--group-report","build.json"]
        for extra in (["--groups-only","--max-groups","257"],
                      ["--skip-wind"], # No authoritative source SK for full prefab.
                      ["--source-skeletal-mesh","/Game/Leaves/SK_oak"], # No explicit skip-wind.
                      ["--groups-only","--prefab-manifest","prefab.json"],
                      ["--control-only","--control-asset-name","SM_oak_Control","--save-groups","--skip-wind"]):
            with self.assertRaises(SystemExit):
                self.parse(*(base+extra))

    def test_static_triangle_rna_preflight_rejects_missing_or_readonly_setting_without_mutation(self):
        def owner(readonly=False):
            prop=types.SimpleNamespace(type="BOOLEAN",is_readonly=readonly)
            return types.SimpleNamespace(remove_degenerates=True,
                bl_rna=types.SimpleNamespace(properties={"remove_degenerates":prop}))
        import_owner=owner()
        lod_owner=owner()
        properties=types.SimpleNamespace(unreal=types.SimpleNamespace(
            import_method=types.SimpleNamespace(fbx=types.SimpleNamespace(static_mesh_import_data=import_owner)),
            editor_static_mesh_library=types.SimpleNamespace(lod_build_settings=lod_owner)))
        fake_context=types.SimpleNamespace(scene=types.SimpleNamespace(send2ue=properties))
        with patch.object(worker.bpy,"context",fake_context,create=True):
            self.assertEqual(set(worker._static_triangle_properties()),{"static_import","static_lod"})
            lod_owner.bl_rna.properties["remove_degenerates"].is_readonly=True
            with self.assertRaisesRegex(RuntimeError,"static_lod"):
                worker._static_triangle_properties()
            self.assertTrue(import_owner.remove_degenerates)
            self.assertTrue(lod_owner.remove_degenerates)


if __name__=="__main__":
    unittest.main()
