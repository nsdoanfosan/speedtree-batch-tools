import hashlib
import json
import tempfile
import unittest
from pathlib import Path

import pytest

from sk_batch.send2ue_manifest_contract import (
    is_actionable_cluster_assembly_manifest,
    manifest_checkout_asset_paths,
    normalize_manifest_handoff_sidecars,
    primary_mesh_asset_path,
    strict_material_handoff_source_paths,
    validate_material_handoff_wrapper,
)


def test_content_driven_pass_through_does_not_require_dependency_orchestrator():
    assert not is_actionable_cluster_assembly_manifest(None)
    assert not is_actionable_cluster_assembly_manifest({
        "status": "pass_through",
        "reason": "normalized_roles_are_prepared_but_unused_by_rendered_mesh",
    })
    assert is_actionable_cluster_assembly_manifest({
        "status": "ready",
        "parts": [{"role": "cluster"}],
    })


def test_strict_material_handoff_uses_recorded_isolated_spm_bundle():
    source = strict_material_handoff_source_paths({
        "source": {
            "spm": {
                "canonical_path": r"D:\tree\.isolated\SK_branch_01.spm",
            },
            "stmat": [{
                "canonical_path": (
                    r"D:\tree\.isolated\fbx\SK_branch_01.stmat"
                ),
            }],
        },
    })

    assert source["spm"].endswith(
        r"\.isolated\SK_branch_01.spm"
    )
    assert source["fbx"].endswith(
        r"\.isolated\fbx\SK_branch_01.fbx"
    )


def test_strict_material_handoff_rejects_mixed_spm_stmat_bundle():
    with pytest.raises(
        ValueError,
        match="different export bundles",
    ):
        strict_material_handoff_source_paths({
            "source": {
                "spm": {
                    "canonical_path": r"D:\tree\SK_branch_01.spm",
                },
                "stmat": [{
                    "canonical_path": (
                        r"D:\other\fbx\SK_branch_01.stmat"
                    ),
                }],
            },
        })


def test_material_handoff_wrapper_binds_canonical_and_exact_source():
    canonical = r"D:\tree\cluster\SK_branch_01.spm"
    isolated = r"D:\tree\.isolated\cluster\SK_branch_01.spm"
    payload = {
        "canonical_spm": canonical,
        "material_source_spm": isolated,
        "speedtree_pipeline_contract": {
            "source": {
                "spm": {"canonical_path": isolated},
                "stmat": [{
                    "canonical_path": (
                        r"D:\tree\.isolated\cluster\fbx"
                        r"\SK_branch_01.stmat"
                    ),
                }],
            },
        },
    }

    source = validate_material_handoff_wrapper(payload, canonical)
    assert source["spm"].endswith(
        r"\.isolated\cluster\SK_branch_01.spm"
    )

    with pytest.raises(ValueError, match="different canonical SPM"):
        validate_material_handoff_wrapper(
            payload,
            r"D:\tree\cluster\SK_other.spm",
        )

    payload["material_source_spm"] = canonical
    with pytest.raises(ValueError, match="source does not match"):
        validate_material_handoff_wrapper(payload, canonical)


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

    def test_missing_descriptor_uses_source_bound_wrapper_cache_copy(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            export_root = root / "export"
            mesh_name = "SK_weed_deadbranches_a_01"
            source = root / f"{mesh_name}.json"
            source.write_text(
                json.dumps({
                    "schema_version": 3,
                    "mesh_name": mesh_name,
                    "materials": [{"master_preset": "tree"}],
                }),
                encoding="utf-8",
            )
            source_before = source.read_bytes()
            source_identity = {
                "spm": {
                    "canonical_path": rf"D:\tree\{mesh_name}.spm",
                    "sha256": "strict-spm",
                },
                "stmat": [{
                    "canonical_path": rf"D:\tree\fbx\{mesh_name}.stmat",
                    "sha256": "strict-stmat",
                }],
            }
            authoritative_pipeline = {
                "source": source_identity,
                "speedtree_handoff_contract": descriptor(
                    mesh_name,
                    source=source_identity,
                ),
            }
            manifest_asset = self._asset(
                f"/Game/Tree/{mesh_name}_Mesh",
                source,
            )
            source_sha256 = hashlib.sha256(source_before).hexdigest()
            manifest_asset["asset_data"][
                "_material_pipeline_json_sha256"
            ] = source_sha256
            for groups_key in ("pre_import_commands", "post_import_commands"):
                for commands in manifest_asset[groups_key]:
                    for index, command in enumerate(commands):
                        if "json_path=" in command:
                            commands[index] = command[:-1] + (
                                f", sidecar_sha256={source_sha256!r})"
                            )

            result = normalize_manifest_handoff_sidecars(
                [manifest_asset],
                export_root,
                sidecar_descriptor_builder=descriptor,
                authoritative_pipeline_contract=authoritative_pipeline,
            )

            normalized_path = Path(
                manifest_asset["asset_data"][
                    "_material_pipeline_json_path"
                ]
            )
            normalized = json.loads(
                normalized_path.read_text(encoding="utf-8")
            )
            self.assertEqual(source.read_bytes(), source_before)
            self.assertNotEqual(normalized_path, source)
            self.assertEqual(
                normalized_path.parent,
                (export_root / "handoff").resolve(),
            )
            self.assertEqual(normalized["mesh_name"], mesh_name)
            self.assertEqual(
                normalized["speedtree_handoff_contract"]["mesh_name"],
                mesh_name,
            )
            self.assertEqual(
                normalized["speedtree_handoff_contract"]["source"],
                source_identity,
            )
            normalized_sha256 = hashlib.sha256(
                normalized_path.read_bytes()
            ).hexdigest()
            self.assertEqual(
                manifest_asset["asset_data"][
                    "_material_pipeline_json_sha256"
                ],
                normalized_sha256,
            )
            commands = sum(
                manifest_asset["pre_import_commands"]
                + manifest_asset["post_import_commands"],
                [],
            )
            json_commands = [line for line in commands if "json_path=" in line]
            self.assertTrue(json_commands)
            self.assertTrue(
                all(normalized_path.as_posix() in line for line in json_commands)
            )
            self.assertTrue(
                all(source.as_posix() not in line for line in json_commands)
            )
            self.assertTrue(
                all(normalized_sha256 in line for line in json_commands)
            )
            self.assertTrue(
                all(source_sha256 not in line for line in json_commands)
            )
            self.assertTrue(result[0]["identity_changed"])

    def test_stale_or_foreign_complete_descriptor_is_reissued_in_cache(self):
        for mismatch in ("version", "source"):
            with self.subTest(mismatch=mismatch), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                export_root = root / "export"
                mesh_name = "SK_weed_deadbranches_a_01"
                source_identity = {
                    "spm": {
                        "canonical_path": rf"D:\tree\{mesh_name}.spm",
                        "sha256": "strict-spm",
                    },
                    "stmat": [{
                        "canonical_path": rf"D:\tree\fbx\{mesh_name}.stmat",
                        "sha256": "strict-stmat",
                    }],
                }
                source = self._sidecar(root, mesh_name)
                source_data = json.loads(source.read_text(encoding="utf-8"))
                source_data["speedtree_handoff_contract"] = descriptor(
                    mesh_name,
                    source=source_identity,
                )
                if mismatch == "version":
                    source_data["speedtree_handoff_contract"]["version"] = 98
                else:
                    source_data["speedtree_handoff_contract"]["source"] = {
                        "spm": {"sha256": "foreign-source"}
                    }
                source.write_text(
                    json.dumps(source_data),
                    encoding="utf-8",
                )
                source_before = source.read_bytes()
                authoritative_pipeline = {
                    "source": source_identity,
                    "speedtree_handoff_contract": descriptor(
                        mesh_name,
                        source=source_identity,
                    ),
                }
                manifest_asset = self._asset(
                    f"/Game/Tree/{mesh_name}_Mesh",
                    source,
                )

                result = normalize_manifest_handoff_sidecars(
                    [manifest_asset],
                    export_root,
                    sidecar_descriptor_builder=descriptor,
                    authoritative_pipeline_contract=authoritative_pipeline,
                )

                normalized_path = Path(
                    manifest_asset["asset_data"][
                        "_material_pipeline_json_path"
                    ]
                )
                normalized = json.loads(
                    normalized_path.read_text(encoding="utf-8")
                )
                self.assertEqual(source.read_bytes(), source_before)
                self.assertNotEqual(normalized_path, source)
                self.assertEqual(
                    normalized["speedtree_handoff_contract"],
                    descriptor(mesh_name, source=source_identity),
                )
                self.assertTrue(result[0]["identity_changed"])

    def test_missing_descriptor_reissues_each_live_wrapper_unit(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            wrapper_name = "SK_cluster_blackgum_01"
            wrapper_source = {"spm": {"sha256": "strict-source"}}
            authoritative_pipeline = {
                "source": wrapper_source,
                "speedtree_handoff_contract": descriptor(
                    wrapper_name,
                    source=wrapper_source,
                ),
            }
            assets = []
            source_bytes = {}
            for ordinal in range(1, 4):
                mesh_name = f"{wrapper_name}_{ordinal:02d}"
                source = root / f"{mesh_name}.json"
                source.write_text(
                    json.dumps({
                        "mesh_name": mesh_name,
                        "materials": [],
                    }),
                    encoding="utf-8",
                )
                source_bytes[source] = source.read_bytes()
                assets.append(
                    self._asset(f"/Game/Tree/{mesh_name}_Mesh", source)
                )

            result = normalize_manifest_handoff_sidecars(
                assets,
                root / "export",
                sidecar_descriptor_builder=descriptor,
                authoritative_pipeline_contract=authoritative_pipeline,
            )

            self.assertEqual(len(result), 3)
            for ordinal, asset in enumerate(assets, start=1):
                mesh_name = f"{wrapper_name}_{ordinal:02d}"
                source = root / f"{mesh_name}.json"
                self.assertEqual(source.read_bytes(), source_bytes[source])
                normalized_path = Path(
                    asset["asset_data"]["_material_pipeline_json_path"]
                )
                normalized = json.loads(
                    normalized_path.read_text(encoding="utf-8")
                )
                self.assertNotEqual(normalized_path, source)
                self.assertEqual(normalized["mesh_name"], mesh_name)
                self.assertEqual(
                    normalized["speedtree_handoff_contract"],
                    descriptor(mesh_name, source=wrapper_source),
                )
                self.assertEqual(
                    asset["asset_data"]["asset_path"],
                    f"/Game/Tree/{mesh_name}",
                )

    def test_missing_descriptor_rejects_wrapper_source_mismatch(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            mesh_name = "SK_weed_deadbranches_a_01"
            source = root / f"{mesh_name}.json"
            source.write_text(
                json.dumps({"mesh_name": mesh_name, "materials": []}),
                encoding="utf-8",
            )
            authoritative_pipeline = {
                "source": {"spm": {"sha256": "current-source"}},
                "speedtree_handoff_contract": descriptor(
                    mesh_name,
                    source={"spm": {"sha256": "unrelated-source"}},
                ),
            }

            with self.assertRaisesRegex(RuntimeError, "not bound"):
                normalize_manifest_handoff_sidecars(
                    [self._asset(f"/Game/Tree/{mesh_name}_Mesh", source)],
                    root / "export",
                    sidecar_descriptor_builder=descriptor,
                    authoritative_pipeline_contract=authoritative_pipeline,
                )

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
