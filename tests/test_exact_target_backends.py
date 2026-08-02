import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from pcg_st9_texture_batch import exact_target_repair as pcg_exact
from spm_generator_sync import exact_target_repair as generator_exact


class ExactTargetBackendTests(unittest.TestCase):
    def test_pcg_inventory_selection_requires_one_exact_identity(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            target = root / "SK_exact.spm"
            sibling = root / "SK_sibling.spm"
            target.write_bytes(b"target")
            sibling.write_bytes(b"sibling")

            class Module:
                @staticmethod
                def spm_paths_for_item(item):
                    return item["paths"]

            selected, inventory = pcg_exact._exact_item(
                Module,
                {"items": [
                    {"folder": str(root), "paths": [str(target)]},
                    {"folder": str(root / "other"), "paths": [str(sibling)]},
                ]},
                target,
            )
            self.assertEqual(selected["paths"], [str(target)])
            self.assertEqual(set(inventory), {str(target), str(sibling)})

    def test_pcg_consumer_rows_do_not_fan_out_to_sibling_spms(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            target = root / "SK_exact.spm"
            sibling = root / "SK_sibling.spm"
            rows = [{
                "atlas_base": "T_Exact",
                "material_spms": [str(target)],
                "material_targets": [{"spm": str(target), "material_id": "4"}],
            }, {
                "atlas_base": "T_Sibling",
                "material_spms": [str(sibling)],
                "material_targets": [{"spm": str(sibling), "material_id": "7"}],
            }]
            selected = pcg_exact._rows_for_exact_target(rows, target)
            self.assertEqual([row["atlas_base"] for row in selected], ["T_Exact"])
            self.assertEqual(
                selected[0]["material_targets"],
                [{"spm": str(target), "material_id": "4"}],
            )

    def test_generator_scope_filters_sibling_followers_and_cluster_targets(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            asset = root / "asset"
            cluster = asset / "cluster"
            cluster.mkdir(parents=True)
            master = asset / "SK_master.spm"
            follower = asset / "SK_exact.spm"
            sibling = asset / "SK_sibling.spm"
            cluster_target = cluster / "SK_cluster.spm"
            other_cluster = cluster / "SK_other_cluster.spm"
            for path in (master, follower, sibling, cluster_target, other_cluster):
                path.write_bytes(path.name.encode())

            scope = {
                "groups": [{
                    "folder": asset,
                    "master": master.name,
                    "names": [follower.name, sibling.name],
                }, {
                    "folder": cluster,
                    "master": cluster_target.name,
                    "names": [other_cluster.name],
                }],
                "cluster_rows": [{
                    "blend": cluster / "SK_cluster.blend",
                    "on_target_spms": [cluster_target, other_cluster],
                }],
                "skipped": [],
            }

            class Engine:
                @staticmethod
                def scan_tree_folders(*_args, **_kwargs):
                    return ["board"]

            class App:
                @staticmethod
                def _connected_scope_from_board(_board):
                    return scope

            module = SimpleNamespace(
                engine=Engine(),
                App=App,
                load_config=lambda: {"tree_root": str(root), "sk_only": True},
            )

            with mock.patch.object(
                generator_exact, "_load_gui_module", return_value=module
            ):
                _module, _cfg, _root, groups, rows, canonical = (
                    generator_exact.exact_runtime_scope(
                        [follower, cluster_target]
                    )
                )

            self.assertEqual(groups[0]["names"], [follower.name])
            self.assertEqual(rows[0]["on_target_spms"], [cluster_target])
            self.assertNotIn(sibling.name, groups[0]["names"])
            self.assertNotIn(other_cluster, rows[0]["on_target_spms"])
            self.assertEqual(canonical, [str(follower), str(cluster_target)])

    def test_generator_master_only_request_fails_instead_of_fanning_out(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            master = root / "SK_master.spm"
            follower = root / "SK_follower.spm"
            master.write_bytes(b"master")
            follower.write_bytes(b"follower")

            class Engine:
                @staticmethod
                def scan_tree_folders(*_args, **_kwargs):
                    return []

            class App:
                @staticmethod
                def _connected_scope_from_board(_board):
                    return {
                        "groups": [{
                            "folder": root,
                            "master": master.name,
                            "names": [follower.name],
                        }],
                        "cluster_rows": [],
                    }

            module = SimpleNamespace(
                engine=Engine(),
                App=App,
                load_config=lambda: {"tree_root": str(root), "sk_only": True},
            )

            with mock.patch.object(
                generator_exact, "_load_gui_module", return_value=module
            ):
                with self.assertRaisesRegex(ValueError, "sibling followers"):
                    generator_exact.exact_runtime_scope([master])


if __name__ == "__main__":
    unittest.main()
