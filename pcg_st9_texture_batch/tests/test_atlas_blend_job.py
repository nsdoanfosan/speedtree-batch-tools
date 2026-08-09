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
                runtime = mock.Mock()
                runtime.operation.return_value = export_target
                with self.assertRaisesRegex(RuntimeError, "Generator 연결 검증 실패"):
                    self.job.apply_mapped_targets(
                        object(),
                        [target],
                        "M_leaf_test_atlas_01",
                        runtime,
                    )
                runtime.operation.assert_called_once_with(
                    "atlas_leaf_mesh_builder",
                    "export_or_update_speedtree_spm_path",
                )

    def test_assets_only_refresh_stages_one_exact_path_without_props_registry(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            target = (root / "SK_cluster_leaf.spm").absolute()
            target.write_bytes(b"spm")
            calls = []

            def target_manifest_path(path):
                path = Path(path)
                return path.parent / ".atlas_leaf_speedtree_targets" / f"{path.stem}.json"

            def export_target(_props, staged, **kwargs):
                calls.append((Path(staged), kwargs))
                manifest = target_manifest_path(staged)
                manifest.parent.mkdir(parents=True, exist_ok=True)
                manifest.write_text("{}", encoding="utf-8")
                return (staged, manifest, [], "updated", 5, [3], [], {})

            validate_targets = mock.Mock(return_value={})
            cleanup_transactions = mock.Mock()

            def execute(targets, build, validate, *, allow_create=False):
                self.assertEqual(targets, [target])
                self.assertFalse(allow_create)
                staged = root / "private-stage" / target.name
                staged.parent.mkdir()
                staged.write_bytes(target.read_bytes())
                result = build(staged, target)
                validate([staged], [{"production_root": target.parent}])
                return [result]

            execute_transaction = mock.Mock(side_effect=execute)
            producer_contract = types.ModuleType("atlas_producer_rebind")
            producer_contract.validate_atlas_producer_refresh_manifest = (
                mock.Mock(return_value={"status": "validated"})
            )
            proof = {"proof_sha256": "sealed"}
            operations = {
                "export_or_update_speedtree_spm_path_impl": export_target,
                "validate_staged_speedtree_targets": validate_targets,
                "target_manifest_path": target_manifest_path,
                "cleanup_pending_transaction_roots": cleanup_transactions,
                "execute_atomic_target_update": execute_transaction,
            }
            runtime = mock.Mock()
            runtime.operation.side_effect = (
                lambda addon_id, name: operations[name]
                if addon_id == "atlas_leaf_mesh_builder"
                else None
            )

            with mock.patch.dict(sys.modules, {
                "atlas_producer_rebind": producer_contract,
            }):
                result = self.job.apply_exact_assets_only_target(
                    object(), target, "M_leaf", proof, runtime
                )

        self.assertEqual(result[0], calls[0][0])
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1]["production_target_spm"], target)
        self.assertIsNone(calls[0][1]["source_material_names"])
        self.assertIsNone(calls[0][1]["source_material_ids"])
        producer_contract.validate_atlas_producer_refresh_manifest.assert_called_once()
        cleanup_transactions.assert_called_once()
        self.assertEqual(runtime.operation.call_count, 5)


if __name__ == "__main__":
    unittest.main()
