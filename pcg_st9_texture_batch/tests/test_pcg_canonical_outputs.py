import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

TOOL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOL_DIR))

from pcg_canonical_outputs import (
    MANIFEST_KIND,
    MANIFEST_NAME,
    REQUIRED_ROLES,
    CanonicalOutputManifestError,
    record_canonical_output,
    validate_manifest,
)
import migrate_current_sk_textures
from pcg_texture_audit import (
    _unsafe_provisional_source,
    atlas_provisional_source_declarations,
    texture_output_contract_state,
)
from spm_texture_normalize import cleanup_preserved_cluster_outputs


class CanonicalOutputManifestTests(unittest.TestCase):
    def _outputs(self, asset, texture_base="T_leaf_test"):
        texture = asset / "texture"
        texture.mkdir(parents=True)
        paths = []
        for role in REQUIRED_ROLES:
            path = texture / f"{texture_base}_{role}.tga"
            path.write_bytes(role.encode("ascii"))
            paths.append(path)
        return paths

    def _atlas_provisional_manifest(
        self,
        asset,
        source_paths,
        *,
        material="M_leaf_test",
    ):
        target_spm = asset / "SK_tree_test.spm"
        expected_base = "T_" + material[2:]
        warning = (
            "Canonical T_* output is absent; PCG ST9 Texture generation "
            "is required."
        )
        remediation = "Generate and export this set in PCG ST9 Texture."
        fallback = {
            "texture_contract_status":
                "source_fallback_needs_pcg_generation",
            "material": material,
            "source_origin": "atlas_mesh_build_source",
            "source_paths": {
                role: str(path) for role, path in source_paths.items()
            },
            "source_roles": sorted(source_paths),
            "expected_t_paths": {
                role: str(
                    asset
                    / "texture"
                    / f"{expected_base}_{role}.tga"
                )
                for role in REQUIRED_ROLES
            },
            "expected_texture_base": expected_base,
            "remediation": remediation,
            "warning": warning,
            "provisional_receipt": {
                "kind": "speedtree_texture_provisional_receipt",
                "version": 1,
                "status": "source_fallback_needs_pcg_generation",
                "source_origin": "atlas_mesh_build_source",
                "material": material,
                "target_spm": str(target_spm),
                "source_roles": sorted(source_paths),
                "warning": warning,
                "remediation": remediation,
                "canonical_promotion_required": True,
            },
        }
        manifest = asset / "speedtree_import_manifest.json"
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text(json.dumps({
            "texture_contract_status":
                "source_fallback_needs_pcg_generation",
            "source_texture_fallbacks": [fallback],
        }), encoding="utf-8")
        return manifest

    def test_writes_asset_local_six_role_manifest_atomically(self):
        with tempfile.TemporaryDirectory() as temporary:
            asset = Path(temporary) / "tree_test"
            paths = self._outputs(asset)
            spm = asset / "SK_tree_test.spm"
            spm.write_bytes(b"spm")
            row = {
                "folder": str(asset),
                "texture_base": "T_leaf_test",
                "material_targets": [{
                    "spm": str(spm),
                    "material_id": "17",
                    "material_name": "M_leaf_test",
                }],
            }

            manifest = record_canonical_output(
                row,
                paths,
                producer_source=r"D:\Source\leaf_test.sbs#T_leaf_test",
            )

            self.assertEqual(manifest, asset / "texture" / MANIFEST_NAME)
            payload = json.loads(manifest.read_text(encoding="utf-8"))
            self.assertEqual(payload["kind"], MANIFEST_KIND)
            self.assertEqual(payload["schema_version"], 1)
            output = payload["outputs"][0]
            self.assertEqual(output["required_roles"], list(REQUIRED_ROLES))
            self.assertEqual(
                output["files"],
                {
                    role: f"T_leaf_test_{role}.tga"
                    for role in REQUIRED_ROLES
                },
            )
            self.assertEqual(output["material_targets"], [{
                "spm": "SK_tree_test.spm",
                "material_id": "17",
                "material_name": "M_leaf_test",
            }])
            self.assertEqual(output["producer"]["tool"], "PCG ST9 Texture")
            validate_manifest(payload, manifest)
            self.assertFalse(any(manifest.parent.glob(f".{MANIFEST_NAME}.*.tmp")))

    def test_records_only_generated_ao_below_generated_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            asset = Path(temporary) / "tree_test"
            paths = self._outputs(asset)
            generated = asset / "texture" / "_pcgtex_generated"
            generated.mkdir()
            ao = generated / "T_leaf_test_ao_from_height.png"
            ao.write_bytes(b"ao")

            manifest = record_canonical_output(
                {"folder": str(asset), "texture_base": "T_leaf_test"},
                paths,
                producer_source="test.sbs#T_leaf_test",
            )

            payload = json.loads(manifest.read_text(encoding="utf-8"))
            self.assertEqual(
                payload["outputs"][0]["files"]["ao"],
                "_pcgtex_generated/T_leaf_test_ao_from_height.png",
            )

    def test_rejects_source_or_cache_path_as_canonical_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            asset = root / "tree_test"
            paths = self._outputs(asset)
            external = root / "Texture" / "T_leaf_test_color.tga"
            external.parent.mkdir()
            external.write_bytes(b"source")
            paths[0] = external

            with self.assertRaisesRegex(
                CanonicalOutputManifestError,
                "outside asset texture root",
            ):
                record_canonical_output(
                    {"folder": str(asset), "texture_base": "T_leaf_test"},
                    paths,
                    producer_source="test.sbs#T_leaf_test",
                )

    def test_rejects_missing_or_role_mismatched_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            asset = Path(temporary) / "tree_test"
            paths = self._outputs(asset)
            wrong = asset / "texture" / "T_leaf_test_albedo.tga"
            wrong.write_bytes(b"wrong")
            paths[0] = wrong

            with self.assertRaisesRegex(
                CanonicalOutputManifestError,
                "expected exactly one T_leaf_test_color.tga",
            ):
                record_canonical_output(
                    {"folder": str(asset), "texture_base": "T_leaf_test"},
                    paths,
                    producer_source="test.sbs#T_leaf_test",
                )

    def test_preflight_seeds_manifest_for_verified_existing_set(self):
        row = {
            "folder": r"D:\Asset\tree_test",
            "texture_dir": r"D:\Asset\tree_test\texture",
            "texture_base": "T_leaf_test",
            "material_spms": [r"D:\Asset\tree_test\SK_tree_test.spm"],
        }
        job = {
            "row": row,
            "mode": "render_only",
            "out_dir": row["texture_dir"],
            "texture_base": row["texture_base"],
        }
        files = [
            Path(row["texture_dir"]) / f"T_leaf_test_{role}.tga"
            for role in REQUIRED_ROLES
        ]
        manifest = Path(row["texture_dir"]) / MANIFEST_NAME
        with mock.patch.object(
                migrate_current_sk_textures, "build_job",
                return_value=job), mock.patch.object(
                migrate_current_sk_textures, "expected_job_size",
                return_value=(2, 2)), mock.patch.object(
                migrate_current_sk_textures.sbs_auto, "size_log2_pixels",
                return_value=(4, 4)), mock.patch.object(
                migrate_current_sk_textures, "job_needs_source_repair",
                return_value=False), mock.patch.object(
                migrate_current_sk_textures, "complete_output_set",
                return_value=True), mock.patch.object(
                migrate_current_sk_textures, "verify_complete_output_set",
                return_value=files), mock.patch.object(
                migrate_current_sk_textures, "_record_job_manifest",
                return_value=manifest) as record:
            jobs, complete, errors = migrate_current_sk_textures.preflight(
                {"items": [row]},
                record_manifests=True,
            )

        self.assertEqual(jobs, [])
        self.assertEqual(errors, [])
        self.assertEqual(complete[0]["canonical_manifest"], str(manifest))
        record.assert_called_once_with(job, files)

    def test_dry_preflight_does_not_seed_existing_manifest(self):
        row = {
            "folder": r"D:\Asset\tree_test",
            "texture_dir": r"D:\Asset\tree_test\texture",
            "texture_base": "T_leaf_test",
            "material_spms": [r"D:\Asset\tree_test\SK_tree_test.spm"],
        }
        job = {
            "row": row,
            "mode": "render_only",
            "out_dir": row["texture_dir"],
            "texture_base": row["texture_base"],
        }
        with mock.patch.object(
                migrate_current_sk_textures, "build_job",
                return_value=job), mock.patch.object(
                migrate_current_sk_textures, "expected_job_size",
                return_value=(2, 2)), mock.patch.object(
                migrate_current_sk_textures.sbs_auto, "size_log2_pixels",
                return_value=(4, 4)), mock.patch.object(
                migrate_current_sk_textures, "job_needs_source_repair",
                return_value=False), mock.patch.object(
                migrate_current_sk_textures, "complete_output_set",
                return_value=True), mock.patch.object(
                migrate_current_sk_textures, "_record_job_manifest") as record:
            jobs, complete, errors = migrate_current_sk_textures.preflight(
                {"items": [row]},
                record_manifests=False,
            )

        self.assertEqual(jobs, [])
        self.assertEqual(errors, [])
        self.assertNotIn("canonical_manifest", complete[0])
        record.assert_not_called()

    def test_original_source_is_provisional_until_pcg_outputs_exist(self):
        with tempfile.TemporaryDirectory() as temporary:
            asset = Path(temporary) / "tree_test"
            source = Path(temporary) / "Texture" / "TCom_leaf_albedo.tif"
            source.parent.mkdir()
            source.write_bytes(b"source")
            entry = {
                "texture_base": "T_leaf_test",
                "missing_export_maps": list(REQUIRED_ROLES),
                "source_refs": [str(source)],
            }

            self.assertEqual(
                texture_output_contract_state(
                    entry,
                    asset,
                    [source.parent],
                ),
                "source_fallback_needs_pcg_generation",
            )

    def test_complete_outputs_promote_only_after_manifest(self):
        with tempfile.TemporaryDirectory() as temporary:
            asset = Path(temporary) / "tree_test"
            paths = self._outputs(asset)
            entry = {
                "texture_base": "T_leaf_test",
                "missing_export_maps": [],
            }
            self.assertEqual(
                texture_output_contract_state(entry, asset),
                "canonical_outputs_need_manifest",
            )

            record_canonical_output(
                {"folder": str(asset), "texture_base": "T_leaf_test"},
                paths,
                producer_source="test.sbs#T_leaf_test",
            )
            self.assertEqual(
                texture_output_contract_state(entry, asset),
                "canonical",
            )

    def test_missing_cache_and_generated_sources_are_not_provisional(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            asset = root / "tree_test"
            missing = root / "Texture" / "missing_albedo.tif"
            entry = {
                "texture_base": "T_leaf_test",
                "missing_export_maps": list(REQUIRED_ROLES),
                "source_refs": [str(missing)],
            }
            self.assertEqual(
                texture_output_contract_state(entry, asset),
                "blocked_source_missing",
            )

            cached = root / ".sk_batch_isolated_bark" / "raw_albedo.tif"
            cached.parent.mkdir()
            cached.write_bytes(b"cache")
            entry["source_refs"] = [str(cached)]
            self.assertEqual(
                texture_output_contract_state(entry, asset),
                "blocked_cache_source",
            )

            generated = root / "Texture" / "T_leaf_test_color.tga"
            generated.parent.mkdir(exist_ok=True)
            generated.write_bytes(b"generated")
            entry["source_refs"] = [str(generated)]
            self.assertEqual(
                texture_output_contract_state(entry, asset),
                "blocked_generated_source",
            )

    def test_provisional_source_provenance_uses_configured_root_boundary(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_root = root / "Texture"
            original = source_root / "TCom" / "leaf_albedo.tif"
            original.parent.mkdir(parents=True)
            original.write_bytes(b"original")
            asset = root / "tree_test"

            self.assertIsNone(
                _unsafe_provisional_source(
                    original,
                    asset_root=asset,
                    source_texture_roots=[source_root],
                )
            )
            # The operating-system temporary directory is an ancestor of this
            # fixture, but is outside the configured source-root boundary.
            self.assertNotIn(
                "temp",
                {
                    part.casefold()
                    for part in original.relative_to(source_root).parts
                },
            )
            authoritative_named_cache = (
                source_root / "cache" / "leaf_albedo.tif"
            )
            authoritative_named_cache.parent.mkdir()
            authoritative_named_cache.write_bytes(b"original")
            self.assertIsNone(
                _unsafe_provisional_source(
                    authoritative_named_cache,
                    asset_root=asset,
                    source_texture_roots=[source_root],
                )
            )

            for component in (
                ".sk_batch_anything",
                "cache",
                "temp",
                "copied",
                "export",
                "fbx",
            ):
                copied = asset / component / "TCom_leaf_albedo.tif"
                copied.parent.mkdir(parents=True, exist_ok=True)
                copied.write_bytes(b"copied")
                with self.subTest(component=component):
                    self.assertIn(
                        _unsafe_provisional_source(
                            copied,
                            asset_root=asset,
                            source_texture_roots=[source_root],
                        ),
                        {
                            "blocked_cache_source",
                            "blocked_unproven_source",
                        },
                    )

            entry = {
                "texture_base": "T_leaf_test",
                "missing_export_maps": list(REQUIRED_ROLES),
                "source_refs": [str(original)],
            }
            self.assertEqual(
                texture_output_contract_state(
                    entry,
                    asset,
                    [source_root],
                ),
                "source_fallback_needs_pcg_generation",
            )
            entry["source_refs"] = [
                str(asset / "fbx" / "TCom_leaf_albedo.tif")
            ]
            self.assertEqual(
                texture_output_contract_state(
                    entry,
                    asset,
                    [source_root],
                ),
                "blocked_cache_source",
            )

    def test_self_declared_blender_cluster_bake_is_not_trusted(self):
        self.assertEqual(
            texture_output_contract_state(
                {
                    "origin_kind": "blender_cluster_bake",
                    "missing_export_maps": list(REQUIRED_ROLES),
                    "source_refs": [],
                },
                r"D:\Asset\tree_test",
            ),
            "blocked_source_missing",
        )

    def test_structured_atlas_manifest_allows_asset_local_original(self):
        with tempfile.TemporaryDirectory() as temporary:
            asset = Path(temporary) / "tree_test"
            source = asset / "source" / "TCom_leaf_albedo.tif"
            source.parent.mkdir(parents=True)
            source.write_bytes(b"original")
            self._atlas_provisional_manifest(
                asset,
                {"albedo": source},
            )

            declarations = atlas_provisional_source_declarations(asset)
            self.assertIn(str(source.resolve()).casefold(), declarations)
            entry = {
                "texture_base": "T_leaf_test",
                "missing_export_maps": list(REQUIRED_ROLES),
                "source_refs": [str(source)],
            }
            self.assertEqual(
                texture_output_contract_state(
                    entry,
                    asset,
                    [],
                    declarations,
                ),
                "source_fallback_needs_pcg_generation",
            )

    def test_missing_source_root_and_external_unproven_source_fail_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            asset = root / "tree_test"
            source = root / "external" / "leaf_albedo.tif"
            source.parent.mkdir()
            source.write_bytes(b"original")
            entry = {
                "texture_base": "T_leaf_test",
                "missing_export_maps": list(REQUIRED_ROLES),
                "source_refs": [str(source)],
            }

            self.assertEqual(
                texture_output_contract_state(entry, asset, []),
                "blocked_unproven_source",
            )

    def test_manifest_cannot_bless_asset_cache_or_generated_source(self):
        components = (
            "cache",
            "export",
            "fbx",
            ".sk_batch_isolated_bark",
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for index, component in enumerate(components):
                with self.subTest(component=component):
                    asset = root / f"tree_test_{index}"
                    source = asset / component / "TCom_leaf_albedo.tif"
                    source.parent.mkdir(parents=True)
                    source.write_bytes(b"derived")
                    self._atlas_provisional_manifest(
                        asset,
                        {"albedo": source},
                    )
                    declarations = atlas_provisional_source_declarations(
                        asset
                    )
                    self.assertNotIn(
                        str(source.resolve()).casefold(),
                        declarations,
                    )
                    self.assertEqual(
                        _unsafe_provisional_source(
                            source,
                            asset_root=asset,
                            declared_source_paths={
                                str(source.resolve()).casefold(): [{}]
                            },
                        ),
                        "blocked_cache_source",
                    )

            asset = root / "tree_generated"
            generated = asset / "source" / "T_leaf_test_color.tga"
            generated.parent.mkdir(parents=True)
            generated.write_bytes(b"generated")
            self._atlas_provisional_manifest(
                asset,
                {"albedo": generated},
            )
            declarations = atlas_provisional_source_declarations(asset)
            self.assertNotIn(
                str(generated.resolve()).casefold(),
                declarations,
            )
            self.assertEqual(
                _unsafe_provisional_source(
                    generated,
                    asset_root=asset,
                    declared_source_paths={
                        str(generated.resolve()).casefold(): [{}]
                    },
                ),
                "blocked_generated_source",
            )

    def test_blender_cluster_bake_never_becomes_an_sbs_render_owner(self):
        canonical = {
            "origin_kind": None,
            "texture_base": "T_leaf_test",
            "atlas_base": "M_leaf_test",
            "material_spms": [r"D:\Asset\SK_tree_test.spm"],
        }
        cluster_bake = {
            "origin_kind": "blender_cluster_bake",
            "texture_base": "",
            "atlas_base": "M_cluster_test",
            "material_spms": [r"D:\Asset\cluster\SK_cluster_test.spm"],
        }

        owners = migrate_current_sk_textures.current_sk_owner_rows(
            {"items": [cluster_bake, canonical]}
        )

        self.assertEqual(owners, [canonical])

    def test_shared_texture_owner_merges_every_material_target(self):
        primary = {
            "folder": r"D:\Asset",
            "texture_base": "T_leaf_shared",
            "material_spms": [r"D:\Asset\SK_tree_01.spm"],
            "material_targets": [{
                "spm": r"D:\Asset\SK_tree_01.spm",
                "material_id": "4",
                "material_name": "M_leaf_primary",
            }],
        }
        shared = {
            "folder": r"D:\Asset",
            "texture_base": "T_leaf_shared",
            "shared_from": "M_leaf_primary",
            "material_spms": [r"D:\Asset\SK_tree_02.spm"],
            "material_targets": [{
                "spm": r"D:\Asset\SK_tree_02.spm",
                "material_id": "9",
                "material_name": "M_leaf_shared",
            }],
        }

        owners = migrate_current_sk_textures.current_sk_owner_rows(
            {"items": [shared, primary]}
        )

        self.assertEqual(len(owners), 1)
        self.assertEqual(
            {
                (
                    Path(row["spm"]).name,
                    row["material_id"],
                    row["material_name"],
                )
                for row in owners[0]["material_targets"]
            },
            {
                ("SK_tree_01.spm", "4", "M_leaf_primary"),
                ("SK_tree_02.spm", "9", "M_leaf_shared"),
            },
        )

    def test_cluster_preservation_never_deletes_canonical_t_outputs(self):
        with tempfile.TemporaryDirectory() as temporary:
            asset = Path(temporary) / "tree_test"
            texture = asset / "texture"
            texture.mkdir(parents=True)
            canonical = texture / "T_cluster_test_color.tga"
            canonical.write_bytes(b"canonical")
            sbs = texture / "tree_test_set_01.sbs"
            sbs.write_bytes(b"authoring")
            plan = {
                "preserved_cluster_materials": [{
                    "spm": str(asset / "SK_tree_test.spm"),
                    "source_spm": str(asset / "cluster" / "SK_cluster_test.spm"),
                    "material_name": "M_cluster_test",
                    "source_refs": [
                        r"cluster\cluster_test.tga",
                        r"cluster\cluster_test_Normal.tga",
                    ],
                    "reason": "verified Blender physical capture",
                }]
            }

            result = cleanup_preserved_cluster_outputs(plan)

            self.assertEqual(result["status"], "preserved_no_mutation")
            self.assertEqual(result["cleaned"], [])
            self.assertEqual(len(result["preserved"]), 1)
            self.assertEqual(canonical.read_bytes(), b"canonical")
            self.assertEqual(sbs.read_bytes(), b"authoring")


if __name__ == "__main__":
    unittest.main()
