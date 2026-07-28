import json
import tempfile
import unittest
from pathlib import Path

from sk_batch.send2ue_manifest_contract import (
    manifest_checkout_asset_paths,
    normalize_manifest_handoff_sidecars,
    primary_mesh_asset_path,
)


def descriptor(mesh_name, source=None):
    value = {
        "kind": "speedtree",
        "version": 99,
        "fingerprint": "test",
        "asset_kind": "speedtree",
        "mesh_name": mesh_name,
    }
    if source:
        value["source"] = source
    return value


class Send2ueManifestContractTests(unittest.TestCase):
    def _sidecar(self, root, mesh_name):
        path = root / f"{mesh_name}.json"
        path.write_text(
            json.dumps(
                {
                    "schema_version": 3,
                    "mesh_name": mesh_name,
                    "materials": [{"master_preset": "tree"}],
                    "speedtree_handoff_contract": descriptor(
                        mesh_name,
                        source={"spm": {"sha256": "source"}},
                    ),
                }
            ),
            encoding="utf-8",
        )
        return path

    def _asset(self, asset_path, sidecar, file_path=None):
        sidecar_argument = sidecar.as_posix()
        result = {
            "asset_data": {
                "_asset_type": "SkeletalMesh",
                "_mesh_object_name": asset_path.rsplit("/", 1)[-1],
                "asset_path": asset_path,
                "_material_pipeline_json_path": str(sidecar),
            },
            "pre_import_commands": [[
                f'_asset_path = r"{asset_path.removesuffix("_Mesh")}".split(".")[0]',
                (
                    "_p.preflight_mesh_materials("
                    f"_asset_path, json_path={sidecar_argument!r})"
                ),
            ]],
            "post_import_commands": [[
                f'_asset_path = r"{asset_path}".split(".")[0]',
                (
                    "_p.process_mesh("
                    f"_asset_path, json_path={sidecar_argument!r})"
                ),
            ]],
        }
        if file_path is not None:
            result["asset_data"]["file_path"] = str(file_path)
        return result

    def test_normalizes_manifest_path_to_export_empty_without_source_edit(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = self._sidecar(root, "SK_cluster_any_01_01")
            source_before = source.read_bytes()
            asset_path = "/Game/Tree/Cluster/SK_cluster_any_01_01_Mesh"
            manifest_asset = self._asset(asset_path, source)

            result = normalize_manifest_handoff_sidecars(
                [manifest_asset],
                root / "export",
                sidecar_descriptor_builder=descriptor,
            )

            self.assertEqual(source.read_bytes(), source_before)
            normalized_path = Path(
                manifest_asset["asset_data"][
                    "_material_pipeline_json_path"
                ]
            )
            self.assertEqual(normalized_path, source)
            normalized = json.loads(
                normalized_path.read_text(encoding="utf-8")
            )
            self.assertEqual(
                normalized["mesh_name"],
                "SK_cluster_any_01_01",
            )
            self.assertEqual(
                normalized["speedtree_handoff_contract"]["mesh_name"],
                "SK_cluster_any_01_01",
            )
            self.assertEqual(
                normalized["speedtree_handoff_contract"]["source"],
                {"spm": {"sha256": "source"}},
            )
            commands = sum(
                manifest_asset["pre_import_commands"]
                + manifest_asset["post_import_commands"],
                [],
            )
            public_path = "/Game/Tree/Cluster/SK_cluster_any_01_01"
            self.assertEqual(
                manifest_asset["asset_data"]["asset_path"],
                public_path,
            )
            self.assertTrue(
                all(
                    public_path in line
                    for line in commands
                    if "_asset_path =" in line
                )
            )
            self.assertTrue(
                all(
                    normalized_path.as_posix() in line
                    for line in commands
                    if "json_path=" in line
                )
            )
            self.assertFalse(result[0]["identity_changed"])
            self.assertTrue(result[0]["asset_path_changed"])

    def test_distinct_empty_sidecars_preserve_public_asset_ordinals(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            assets = [
                self._asset(
                    f"/Game/Tree/Cluster/SK_cluster_any_01_{index:02d}_Mesh",
                    self._sidecar(
                        root,
                        f"SK_cluster_any_01_{index:02d}",
                    ),
                )
                for index in range(1, 4)
            ]

            results = normalize_manifest_handoff_sidecars(
                assets,
                root / "export",
                sidecar_descriptor_builder=descriptor,
            )

            normalized_paths = {
                item["asset_data"]["_material_pipeline_json_path"]
                for item in assets
            }
            self.assertEqual(len(results), 3)
            self.assertEqual(len(normalized_paths), 3)
            self.assertEqual(
                [
                    item["asset_data"]["asset_path"]
                    for item in assets
                ],
                [
                    f"/Game/Tree/Cluster/SK_cluster_any_01_{index:02d}"
                    for index in range(1, 4)
                ],
            )

    def test_imports_cache_local_fbx_under_public_empty_name(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            export_root = root / "export"
            export_root.mkdir()
            source_fbx = export_root / "SK_cluster_any_01_01_Mesh.fbx"
            source_fbx.write_bytes(b"fbx payload")
            source = self._sidecar(root, "SK_cluster_any_01_01")
            manifest_asset = self._asset(
                "/Game/Tree/Cluster/SK_cluster_any_01_01_Mesh",
                source,
                source_fbx,
            )

            result = normalize_manifest_handoff_sidecars(
                [manifest_asset],
                export_root,
                sidecar_descriptor_builder=descriptor,
            )

            public_fbx = export_root / "SK_cluster_any_01_01.fbx"
            self.assertEqual(
                Path(manifest_asset["asset_data"]["file_path"]),
                public_fbx,
            )
            self.assertEqual(public_fbx.read_bytes(), b"fbx payload")
            self.assertEqual(source_fbx.read_bytes(), b"fbx payload")
            self.assertTrue(result[0]["file_path_changed"])

    def test_primary_and_checkout_paths_come_from_manifest_assets(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            asset_paths = [
                "/Game/Tree/Cluster/SK_cluster_any_01_01_Mesh",
                "/Game/Tree/Cluster/SK_cluster_any_01_02_Mesh",
            ]
            assets = [
                self._asset(
                    path,
                    self._sidecar(
                        root,
                        f"SK_cluster_any_01_{index:02d}",
                    ),
                )
                for index, path in enumerate(asset_paths, 1)
            ]
            normalize_manifest_handoff_sidecars(
                assets,
                root / "export",
                sidecar_descriptor_builder=descriptor,
            )

            primary = primary_mesh_asset_path(
                assets,
                preferred_path="/Game/Tree/Cluster/SK_cluster_any_01_01",
            )
            checkout = manifest_checkout_asset_paths(assets)

            public_paths = [
                "/Game/Tree/Cluster/SK_cluster_any_01_01",
                "/Game/Tree/Cluster/SK_cluster_any_01_02",
            ]
            self.assertEqual(primary, public_paths[0])
            for asset_path in public_paths:
                self.assertIn(asset_path, checkout)
                self.assertIn(asset_path + "_Skeleton", checkout)
                self.assertIn(asset_path + "_PhysicsAsset", checkout)
            self.assertNotIn(asset_paths[0], checkout)

    def test_matching_tree_asset_preserves_preferred_primary(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = self._sidecar(root, "SK_tree_any_01")
            path = "/Game/Tree/SK_tree_any_01"
            asset = self._asset(path, source)

            self.assertEqual(
                primary_mesh_asset_path([asset], preferred_path=path),
                path,
            )


if __name__ == "__main__":
    unittest.main()
