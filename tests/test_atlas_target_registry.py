import importlib.machinery
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


REPO = Path(__file__).resolve().parents[1]
PCG_DIR = REPO / "pcg_st9_texture_batch"
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(PCG_DIR))

from atlas_target_registry import (  # noqa: E402
    TargetRegistryPublishError,
    capture_target_registry_preimage,
    load_target_registry,
    registry_path_for_blend,
    restore_target_registry_preimage,
    save_target_registry,
)
from mutation_plan_authority import path_state  # noqa: E402


def load_pcg_gui():
    path = PCG_DIR / "pcg_texture_gui.pyw"
    loader = importlib.machinery.SourceFileLoader("_pcg_gui_registry_test", str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[loader.name] = module
    loader.exec_module(module)
    return module


class AtlasTargetRegistryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.gui = load_pcg_gui()

    def test_registry_round_trip_deduplicates_targets(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            blend = root / "M_leaf_elm_atlas_01.blend"
            blend.touch()
            first = root / "SK_Tree_elm_01.spm"
            second = root / "Cluster" / "leaf_elm_01.spm"
            payload = save_target_registry(blend, [first, first, second])

            self.assertEqual(len(payload["target_spms"]), 2)
            self.assertEqual(
                registry_path_for_blend(blend).name,
                "M_leaf_elm_atlas_01.atlas_leaf_targets.json",
            )
            self.assertEqual(
                load_target_registry(blend)["target_spms"],
                [str(first.absolute()), str(second.absolute())],
            )

    def test_publish_lock_is_structured_precommit_and_cleans_unique_temp(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            blend = root / "M_leaf_elm_atlas_01.blend"
            blend.touch()
            first = root / "SK_Tree_elm_01.spm"
            second = root / "SK_Tree_elm_02.spm"
            save_target_registry(blend, [first])
            before = registry_path_for_blend(blend).read_bytes()

            with mock.patch(
                "atlas_target_registry.os.replace",
                side_effect=PermissionError(13, "publish locked"),
            ):
                with self.assertRaises(TargetRegistryPublishError) as raised:
                    save_target_registry(blend, [second])

            contract = raised.exception.connected_retry_contract
            self.assertEqual(contract["operation_phase"], "registry_publish")
            self.assertFalse(contract["committed"])
            self.assertFalse(contract["rollback_succeeded"])
            self.assertTrue(contract["temporary_output_isolated"])
            self.assertEqual(contract["error_code"], 13)
            self.assertEqual(registry_path_for_blend(blend).read_bytes(), before)
            self.assertEqual(list(root.glob(".*.tmp")), [])

    def test_compare_and_swap_rejects_registry_drift_before_replace(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            blend = root / "M_leaf_elm_atlas_01.blend"
            blend.touch()
            first = root / "SK_Tree_elm_01.spm"
            second = root / "SK_Tree_elm_02.spm"
            third = root / "SK_Tree_elm_03.spm"
            save_target_registry(blend, [first])
            expected = path_state(registry_path_for_blend(blend))
            save_target_registry(blend, [second])

            with self.assertRaises(TargetRegistryPublishError) as caught:
                save_target_registry(
                    blend,
                    [third],
                    expected_registry_state=expected,
                )

            self.assertEqual(
                caught.exception.connected_retry_contract[
                    "operation_phase"
                ],
                "registry_compare_and_swap",
            )
            self.assertEqual(
                load_target_registry(blend)["target_spms"],
                [str(second.absolute())],
            )

    def test_exact_preimage_rollback_restores_original_registry_bytes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            blend = root / "M_leaf_elm_atlas_01.blend"
            blend.touch()
            first = root / "SK_Tree_elm_01.spm"
            second = root / "SK_Tree_elm_02.spm"
            save_target_registry(blend, [first])
            registry_path = registry_path_for_blend(blend)
            original_bytes = registry_path.read_bytes()
            preimage = capture_target_registry_preimage(blend)
            published = save_target_registry(
                blend,
                [second],
                expected_registry_state=preimage["state"],
            )

            restored = restore_target_registry_preimage(
                preimage,
                expected_registry_state=published["registry_state"],
            )

            self.assertEqual(restored["status"], "restored")
            self.assertEqual(registry_path.read_bytes(), original_bytes)
            self.assertEqual(
                load_target_registry(blend)["target_spms"],
                [str(first.absolute())],
            )

    def test_exact_preimage_rollback_refuses_external_registry_edit(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            blend = root / "M_leaf_elm_atlas_01.blend"
            blend.touch()
            first = root / "SK_Tree_elm_01.spm"
            second = root / "SK_Tree_elm_02.spm"
            external = root / "SK_Tree_elm_03.spm"
            save_target_registry(blend, [first])
            preimage = capture_target_registry_preimage(blend)
            published = save_target_registry(
                blend,
                [second],
                expected_registry_state=preimage["state"],
            )
            save_target_registry(
                blend,
                [external],
                expected_registry_state=published["registry_state"],
            )

            with self.assertRaises(TargetRegistryPublishError) as raised:
                restore_target_registry_preimage(
                    preimage,
                    expected_registry_state=published["registry_state"],
                )

            self.assertEqual(
                raised.exception.connected_retry_contract[
                    "operation_phase"
                ],
                "registry_compare_and_swap",
            )
            self.assertEqual(
                load_target_registry(blend)["target_spms"],
                [str(external.absolute())],
            )

    def test_exact_registry_replaces_inferred_one_target_statistics(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            blend = root / "M_leaf_elm_atlas_01.blend"
            blend.touch()
            first = root / "SK_Tree_elm_01.spm"
            second = root / "SK_Tree_elm_02.spm"
            missing = root / "Cluster" / "leaf_elm_sdie_01.spm"
            first.touch()
            second.touch()
            save_target_registry(blend, [first, second, missing])
            item = {
                "leaf_mesh_sources": [{
                    "atlas_blends": [str(blend)],
                    "targets": [{
                        "spm": str(first),
                        "generator_connection_complete": True,
                    }],
                    "export_participating": True,
                }],
            }

            rows = self.gui.blender_connection_rows(item)

            self.assertEqual(len(rows), 1)
            self.assertEqual([target["spm"] for target in rows[0]["spms"]], [
                missing.absolute(), first.absolute(), second.absolute(),
            ])
            self.assertEqual(
                {target["spm"].name: target["connected"] for target in rows[0]["spms"]},
                {
                    "leaf_elm_sdie_01.spm": None,
                    "SK_Tree_elm_01.spm": True,
                    "SK_Tree_elm_02.spm": None,
                },
            )
            self.assertIn("연결 SPM 3개", self.gui.blender_connection_summary(rows[0]))
            self.assertIn("파일 없음 1개", self.gui.blender_connection_summary(rows[0]))

    def test_pcg_lists_cluster_normalizer_blend_with_owner_sk_on_off_rows(self):
        with tempfile.TemporaryDirectory() as temporary:
            owner = Path(temporary) / "Tree_elm"
            cluster = owner / "Cluster"
            cluster.mkdir(parents=True)
            (cluster / "branch_elm_01.spm").touch()
            blend = cluster / "SK_branch_elm_01.blend"
            blend.touch()
            first = owner / "SK_Tree_elm_01.spm"
            second = owner / "SK_Tree_elm_02.spm"
            first.touch()
            second.touch()
            save_target_registry(blend, [first])
            scope = owner / ".atlas_leaf_speedtree_scopes"
            scope.mkdir()
            (scope / f"scope__{first.stem}.json").write_text(
                json.dumps({
                    "blend_file": str(blend),
                    "spm": str(first),
                    "material": "M_branch_elm_01",
                    "generator_connection": {"complete": True},
                }),
                encoding="utf-8",
            )

            rows = self.gui.blender_connection_rows({"folder": str(owner)})

            self.assertEqual(len(rows), 1)
            self.assertTrue(rows[0]["cluster_normalized"])
            targets = {target["spm"].name: target for target in rows[0]["spms"]}
            self.assertTrue(targets[first.name]["relation_on"])
            self.assertTrue(targets[first.name]["connected"])
            self.assertFalse(targets[second.name]["relation_on"])
            self.assertIsNone(targets[second.name]["connected"])
            self.assertEqual(
                self.gui.blender_connection_summary(rows[0]),
                "관계 PARTIAL 1/2 · Generator Sync에서 ON/OFF 정규화 필요",
            )


if __name__ == "__main__":
    unittest.main()
