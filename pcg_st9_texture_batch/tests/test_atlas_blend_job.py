import importlib.machinery
import importlib.util
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock


TOOL_DIR = Path(__file__).resolve().parents[1]


class AtlasBlendJobTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = TOOL_DIR / "jobs" / "atlas_blend_job.py"
        loader = importlib.machinery.SourceFileLoader(
            "atlas_blend_job_test", str(path))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        cls.job = importlib.util.module_from_spec(spec)
        with mock.patch.dict(sys.modules, {
            "addon_utils": types.ModuleType("addon_utils"),
            "bpy": types.ModuleType("bpy"),
        }):
            loader.exec_module(cls.job)

    def test_target_map_requires_exact_spm_and_source_material_names(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "targets.json"
            path.write_text(json.dumps({
                "version": 1,
                "targets": [{
                    "spm": str(Path(temp) / "SK_test.spm"),
                    "source_material_names": ["M_leaf_test_01"],
                    "source_material_ids": [4],
                    "generator_bindings": [{"material_id": 4}],
                }],
            }), encoding="utf-8")
            targets = self.job.load_target_map(path)

        self.assertEqual(targets[0]["source_material_names"], ["M_leaf_test_01"])
        self.assertEqual(targets[0]["source_material_ids"], [4])

    def test_target_map_rejects_late_unlocatable_generator_work(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "targets.json"
            path.write_text(json.dumps({
                "version": 1,
                "targets": [{"spm": str(Path(temp) / "SK_test.spm")}],
            }), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "원본 머티리얼 식별 정보 없음"):
                self.job.load_target_map(path)

    def test_target_map_and_cli_spm_lists_must_match(self):
        targets = [{
            "spm": r"D:\Tree\SK_a.spm",
            "source_material_names": ["M_leaf_a"],
        }]
        with self.assertRaisesRegex(RuntimeError, "최종 SK 목록이 다름"):
            self.job.validate_target_paths([r"D:\Tree\SK_b.spm"], targets)

    def test_addon_manifest_must_explicitly_confirm_generator_connection(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manifest_path = root / "speedtree_import_manifest.json"
            manifest_path.write_text(json.dumps({
                "generator_connection": {"requested": True, "complete": False},
            }), encoding="utf-8")

            speedtree = types.ModuleType("atlas_leaf_mesh_builder.speedtree")
            def export_target(
                    _props, _target, *, atlas_asset_name=None,
                    source_material_names=None, source_material_ids=None,
                    allow_create=False):
                return (None, manifest_path, [], "updated", "1", [], [], {})
            speedtree.export_or_update_speedtree_spm_path = export_target
            package = types.ModuleType("atlas_leaf_mesh_builder")
            package.__path__ = []
            target = {
                "spm": str(root / "SK_test.spm"),
                "source_material_names": ["M_leaf_test"],
                "source_material_ids": [4],
                "generator_bindings": [],
            }
            with mock.patch.dict(sys.modules, {
                "atlas_leaf_mesh_builder": package,
                "atlas_leaf_mesh_builder.speedtree": speedtree,
            }):
                with self.assertRaisesRegex(RuntimeError, "Generator 연결 검증 실패"):
                    self.job.apply_mapped_targets(object(), [target], "M_leaf_test_atlas_01")


if __name__ == "__main__":
    unittest.main()
