import json
import hashlib
import os
import sys
import tempfile
import threading
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
    refresh_atlas_manifests_for_spm,
    record_canonical_output,
    validate_manifest,
)
from exact_target_repair import execute_step3_standard
from repair_orchestration import ATLAS_MANIFEST_MIRROR_REPAIR
import migrate_current_sk_textures
from pcg_texture_audit import (
    _atlas_manifest_targets,
    _unsafe_provisional_source,
    atlas_provisional_source_declarations,
    texture_output_contract_state,
)
from spm_texture_normalize import cleanup_preserved_cluster_outputs


class CanonicalOutputManifestTests(unittest.TestCase):
    @staticmethod
    def _write_cluster_pair_receipt(canonical, legacy):
        keys = sorted(
            os.path.normcase(os.path.abspath(str(path))).casefold()
            for path in (canonical, legacy)
        )
        pair_id = hashlib.sha256(
            "\n".join(keys).encode("utf-8")
        ).hexdigest()
        reports = canonical.parent / "reports"
        reports.mkdir(exist_ok=True)
        receipt = reports / f"{canonical.stem}_cluster_spm_pair.json"
        receipt.write_text(json.dumps({
            "receipt_kind": "cluster_spm_output_name_normalization",
            "schema_version": 2,
            "status": "complete",
            "pair_id": pair_id,
            "invariants": {
                "after_content_equal": True,
                "canonical_output_authoritative": True,
                "source_unchanged_during_copy": True,
            },
            "paths": {
                "canonical_output": str(canonical),
                "legacy_unprefixed_input": str(legacy),
            },
        }), encoding="utf-8")
        return receipt

    def test_exact_bat_dispatches_atlas_mirror_repair_then_canonical_refresh(self):
        class Lease:
            @staticmethod
            def renew_and_check_current():
                return True

        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "SK_cluster_test.spm"
            target.write_bytes(b"sanitized")
            progress = []
            with mock.patch(
                "exact_target_repair.repair_atlas_manifest_mirrors",
                return_value={"status": "repaired"},
            ) as repair, mock.patch(
                "exact_target_repair.refresh_atlas_manifests_for_spm",
                return_value={"status": "current"},
            ) as refresh:
                result = execute_step3_standard(
                    {
                        "repair_action": ATLAS_MANIFEST_MIRROR_REPAIR,
                        "target_spms": [str(target)],
                    },
                    progress=lambda stage, **data: progress.append((stage, data)),
                    cancel_event=threading.Event(),
                    lease=Lease(),
                )

            self.assertTrue(result["shared_queue_success"])
            repair.assert_called_once_with(target.absolute())
            refresh.assert_called_once_with(
                target.absolute(), require_complete=True
            )
            self.assertEqual(progress[-1][1]["completed"], 1)

    def test_manifest_free_folder_has_no_atlas_resolution_targets(self):
        with tempfile.TemporaryDirectory() as temporary:
            asset = Path(temporary) / "tree_test"
            asset.mkdir()
            for index in range(3):
                (asset / f"SK_tree_test_{index}.spm").write_bytes(b"spm")

            self.assertEqual(_atlas_manifest_targets(asset), [])

    def test_target_receipt_preserves_full_fail_closed_fleet(self):
        with tempfile.TemporaryDirectory() as temporary:
            asset = Path(temporary) / "tree_test"
            asset.mkdir()
            target = asset / "SK_tree_test_1.spm"
            for index in range(3):
                (asset / f"SK_tree_test_{index}.spm").write_bytes(b"spm")
            receipts = asset / ".atlas_leaf_speedtree_targets"
            receipts.mkdir()
            (receipts / "stable-receipt.json").write_text(
                json.dumps({"spm": str(target)}),
                encoding="utf-8",
            )

            self.assertEqual(
                _atlas_manifest_targets(asset),
                sorted(
                    [
                        (asset / f"SK_tree_test_{index}.spm").resolve()
                        for index in range(3)
                    ] + [(asset / "stable-receipt.spm").resolve()],
                    key=lambda path: str(path).casefold(),
                ),
            )

    def test_target_receipt_does_not_promote_copy_or_backup_siblings(self):
        with tempfile.TemporaryDirectory() as temporary:
            asset = Path(temporary) / "tree_test"
            asset.mkdir()
            live = asset / "SK_tree_test_1.spm"
            live.write_bytes(b"spm")
            manual_copy = asset / "SK_tree_test_1 - Copy.spm"
            manual_copy.write_bytes(b"spm")
            rollback = asset / "SK_tree_test_1.apply_rollback_backup.spm"
            rollback.write_bytes(b"spm")
            receipts = asset / ".atlas_leaf_speedtree_targets"
            receipts.mkdir()
            (receipts / "stable-receipt.json").write_text(
                json.dumps({"spm": str(live)}),
                encoding="utf-8",
            )

            self.assertEqual(
                _atlas_manifest_targets(asset),
                sorted(
                    [live.resolve(), (asset / "stable-receipt.spm").resolve()],
                    key=lambda path: str(path).casefold(),
                ),
            )

    def test_unreadable_target_receipt_falls_back_to_spm_fleet(self):
        with tempfile.TemporaryDirectory() as temporary:
            asset = Path(temporary) / "tree_test"
            asset.mkdir()
            spms = []
            for index in range(2):
                spm = asset / f"SK_tree_test_{index}.spm"
                spm.write_bytes(b"spm")
                spms.append(spm.resolve())
            receipts = asset / ".atlas_leaf_speedtree_targets"
            receipts.mkdir()
            (receipts / "unreadable.json").write_text(
                "{not-json",
                encoding="utf-8",
            )

            self.assertEqual(
                _atlas_manifest_targets(asset),
                sorted(
                    spms + [(asset / "unreadable.spm").resolve()],
                    key=lambda path: str(path).casefold(),
                ),
            )

    def _outputs(self, asset, texture_base="T_leaf_test"):
        texture = asset / "texture"
        texture.mkdir(parents=True, exist_ok=True)
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
            "spm": str(target_spm),
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

    def test_canonical_commit_regenerates_complete_atlas_scope_before_consumer(self):
        with tempfile.TemporaryDirectory() as temporary:
            asset = Path(temporary) / "tree_test"
            cluster = asset / "cluster"
            cluster.mkdir(parents=True)
            spm = cluster / "SK_cluster_test.spm"
            spm.write_bytes(b"spm")
            scope_dir = cluster / ".atlas_leaf_speedtree_scopes"
            scope_dir.mkdir()
            scope = scope_dir / "scope__SK_cluster_test.json"
            fallback_status = "source_fallback_needs_pcg_generation"
            fallback = {
                "texture_contract_status": fallback_status,
                "material": "M_leaf_test",
                "source_paths": {
                    "albedo": str(asset / "source" / "leaf.tif"),
                },
            }
            scope.write_text(json.dumps({
                "spm": str(spm),
                "texture_contract_status": fallback_status,
                "canonical_texture_outputs": [],
                "source_texture_fallbacks": [fallback],
                "source_texture_origins": {
                    "M_leaf_test": "atlas_mesh_build_source",
                },
                "material_groups": [{
                    "material": "M_leaf_test",
                    "material_id": 17,
                    "texture_contract_status": fallback_status,
                    "source_texture_fallback": fallback,
                    "texture_warning": "canonical output is absent",
                }],
                "speedtree_material_groups": [{
                    "material": "M_leaf_test",
                    "material_id": 17,
                    "texture_contract_status": fallback_status,
                    "texture_provisional_receipt": {"status": fallback_status},
                }],
                "notes": [
                    "keep this note",
                    "WARNING: canonical T_* output is absent.",
                ],
            }), encoding="utf-8")

            manifest = record_canonical_output(
                {
                    "folder": str(asset),
                    "texture_base": "T_leaf_test",
                    "material_targets": [{
                        "spm": str(spm),
                        "material_id": "17",
                        "material_name": "M_leaf_test",
                    }],
                },
                self._outputs(asset),
                producer_source="test.sbs#T_leaf_test",
            )

            promoted = json.loads(scope.read_text(encoding="utf-8"))
            self.assertEqual(
                promoted["texture_contract_status"],
                "canonical_pcg_output",
            )
            self.assertEqual(promoted["source_texture_fallbacks"], [])
            self.assertEqual(promoted["source_texture_origins"], {})
            self.assertEqual(
                promoted["canonical_texture_outputs"][0]["manifest"],
                str(manifest),
            )
            self.assertEqual(
                promoted["canonical_texture_outputs"][0]["files"]["color"],
                str(asset / "texture" / "T_leaf_test_color.tga"),
            )
            self.assertEqual(
                promoted["material_groups"][0]["texture_contract_status"],
                "canonical_pcg_output",
            )
            self.assertNotIn(
                "source_texture_fallback",
                promoted["material_groups"][0],
            )
            self.assertEqual(promoted["notes"], ["keep this note"])
            second = refresh_atlas_manifests_for_spm(
                spm,
                manifest,
                require_complete=True,
            )
            self.assertEqual(second["status"], "current")
            self.assertEqual(second["updated"], [])

    def test_receipt_proven_cluster_pair_resolves_canonical_material_target(self):
        with tempfile.TemporaryDirectory() as temporary:
            asset = Path(temporary) / "bush_silky_dogwood"
            cluster = asset / "cluster"
            cluster.mkdir(parents=True)
            canonical = cluster / "SK_cluster_silky_dogwood_01.spm"
            legacy = cluster / "cluster_silky_dogwood_01.spm"
            canonical.write_bytes(b"same-cluster")
            legacy.write_bytes(b"same-cluster")
            receipt = self._write_cluster_pair_receipt(canonical, legacy)
            scope_dir = cluster / ".atlas_leaf_speedtree_scopes"
            scope_dir.mkdir()
            scope = scope_dir / f"scope__{canonical.stem}.json"
            scope.write_text(json.dumps({
                "spm": str(canonical),
                "texture_contract_status":
                    "source_fallback_needs_pcg_generation",
                "material_groups": [{
                    "material": "M_cluster_silky_dogwood_atlas_01",
                    "material_id": 5,
                }],
            }), encoding="utf-8")
            manifest = record_canonical_output(
                {
                    "folder": str(asset),
                    "texture_base": "T_cluster_silky_dogwood_atlas_01",
                    "material_targets": [{
                        "spm": str(legacy),
                        "material_id": "5",
                        "material_name":
                            "M_cluster_silky_dogwood_atlas_01",
                    }],
                },
                self._outputs(
                    asset, "T_cluster_silky_dogwood_atlas_01"
                ),
                producer_source="silky.sbs#atlas",
            )

            result = refresh_atlas_manifests_for_spm(
                canonical,
                manifest,
                require_complete=True,
            )

            self.assertIn(result["status"], {"updated", "current"})
            self.assertEqual(
                result["cluster_pair_identity"]["counterpart_spm"],
                str(legacy),
            )
            self.assertEqual(
                result["cluster_pair_identity"]["receipt_path"],
                str(receipt),
            )
            promoted = json.loads(scope.read_text(encoding="utf-8"))
            self.assertEqual(
                promoted["texture_contract_status"],
                "canonical_pcg_output",
            )
            self.assertEqual(
                promoted["canonical_texture_outputs"][0]["material_id"],
                "5",
            )

    def test_similar_cluster_name_without_pair_receipt_is_not_equivalent(self):
        with tempfile.TemporaryDirectory() as temporary:
            asset = Path(temporary) / "bush_silky_dogwood"
            cluster = asset / "cluster"
            cluster.mkdir(parents=True)
            canonical = cluster / "SK_cluster_silky_dogwood_01.spm"
            legacy = cluster / "cluster_silky_dogwood_01.spm"
            canonical.write_bytes(b"same-cluster")
            legacy.write_bytes(b"same-cluster")
            scope_dir = cluster / ".atlas_leaf_speedtree_scopes"
            scope_dir.mkdir()
            scope = scope_dir / f"scope__{canonical.stem}.json"
            scope.write_text(json.dumps({
                "spm": str(canonical),
                "material_groups": [{
                    "material": "M_cluster_silky_dogwood_atlas_01",
                    "material_id": 5,
                }],
            }), encoding="utf-8")
            manifest = record_canonical_output(
                {
                    "folder": str(asset),
                    "texture_base": "T_cluster_silky_dogwood_atlas_01",
                    "material_targets": [{
                        "spm": str(legacy),
                        "material_id": "5",
                        "material_name":
                            "M_cluster_silky_dogwood_atlas_01",
                    }],
                },
                self._outputs(
                    asset, "T_cluster_silky_dogwood_atlas_01"
                ),
                producer_source="silky.sbs#atlas",
            )

            with self.assertRaises(CanonicalOutputManifestError) as caught:
                refresh_atlas_manifests_for_spm(
                    canonical,
                    manifest,
                    require_complete=True,
                )

            self.assertEqual(
                caught.exception.report["reason_token"],
                "canonical_material_mapping_incomplete",
            )
            self.assertIsNone(
                caught.exception.report["cluster_pair_identity"]
            )
            self.assertNotIn(
                "canonical_texture_outputs",
                json.loads(scope.read_text(encoding="utf-8")),
            )

    def test_canonical_promotion_updates_selected_records_not_legacy_shadows(self):
        with tempfile.TemporaryDirectory() as temporary:
            asset = Path(temporary) / "tree_test"
            cluster = asset / "cluster"
            cluster.mkdir(parents=True)
            spm = cluster / "SK_cluster_test.spm"
            spm.write_bytes(b"spm")
            payload = {
                "spm": str(spm),
                "material_groups": [{
                    "material": "M_leaf_test",
                    "material_id": 17,
                    "texture_contract_status":
                        "source_fallback_needs_pcg_generation",
                }],
                "texture_contract_status":
                    "source_fallback_needs_pcg_generation",
            }
            scope_dir = cluster / ".atlas_leaf_speedtree_scopes"
            scope_dir.mkdir()
            scope = scope_dir / f"scope__{spm.stem}.json"
            scope.write_text(json.dumps(payload), encoding="utf-8")
            legacy = cluster / "speedtree_import_manifest_M_leaf_test.json"
            legacy.write_text(json.dumps(payload), encoding="utf-8")
            legacy_before = legacy.read_bytes()

            canonical = record_canonical_output(
                {
                    "folder": str(asset),
                    "texture_base": "T_leaf_test",
                    "material_targets": [{
                        "spm": str(spm),
                        "material_id": "17",
                        "material_name": "M_leaf_test",
                    }],
                },
                self._outputs(asset),
                producer_source="test.sbs#T_leaf_test",
            )

            self.assertEqual(
                json.loads(scope.read_text(encoding="utf-8"))[
                    "texture_contract_status"
                ],
                "canonical_pcg_output",
            )
            self.assertEqual(legacy.read_bytes(), legacy_before)
            result = refresh_atlas_manifests_for_spm(spm, canonical)
            self.assertEqual(result["status"], "current")
            self.assertEqual(
                [row["path"] for row in result[
                    "atlas_manifest_resolution"
                ]["shadowed"]],
                [str(legacy.resolve())],
            )

    def test_atlas_scope_is_not_partially_promoted_between_texture_sets(self):
        with tempfile.TemporaryDirectory() as temporary:
            asset = Path(temporary) / "tree_test"
            cluster = asset / "cluster"
            cluster.mkdir(parents=True)
            spm = cluster / "SK_cluster_test.spm"
            spm.write_bytes(b"spm")
            scope_dir = cluster / ".atlas_leaf_speedtree_scopes"
            scope_dir.mkdir()
            scope = scope_dir / "scope__SK_cluster_test.json"
            fallback_status = "source_fallback_needs_pcg_generation"
            groups = [
                {
                    "material": "M_leaf_a",
                    "material_id": 8,
                    "texture_contract_status": fallback_status,
                },
                {
                    "material": "M_leaf_b",
                    "material_id": 9,
                    "texture_contract_status": fallback_status,
                },
            ]
            original = {
                "spm": str(spm),
                "texture_contract_status": fallback_status,
                "material_groups": groups,
                "speedtree_material_groups": groups,
                "source_texture_fallbacks": [{"material": "M_leaf_a"}],
            }
            scope.write_text(json.dumps(original), encoding="utf-8")

            record_canonical_output(
                {
                    "folder": str(asset),
                    "texture_base": "T_leaf_a",
                    "material_targets": [{
                        "spm": str(spm),
                        "material_id": "8",
                        "material_name": "M_leaf_a",
                    }],
                },
                self._outputs(asset, "T_leaf_a"),
                producer_source="test.sbs#T_leaf_a",
            )
            after_first = json.loads(scope.read_text(encoding="utf-8"))
            self.assertEqual(
                after_first["texture_contract_status"],
                fallback_status,
            )

            record_canonical_output(
                {
                    "folder": str(asset),
                    "texture_base": "T_leaf_b",
                    "material_targets": [{
                        "spm": str(spm),
                        "material_id": "9",
                        "material_name": "M_leaf_b",
                    }],
                },
                self._outputs(asset, "T_leaf_b"),
                producer_source="test.sbs#T_leaf_b",
            )
            promoted = json.loads(scope.read_text(encoding="utf-8"))
            self.assertEqual(
                promoted["texture_contract_status"],
                "canonical_pcg_output",
            )
            self.assertEqual(
                {
                    row["texture_base"]
                    for row in promoted["canonical_texture_outputs"]
                },
                {"T_leaf_a", "T_leaf_b"},
            )

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

    def test_records_verified_output_in_asset_texture_subdirectory(self):
        with tempfile.TemporaryDirectory() as temporary:
            asset = Path(temporary) / "tree_test"
            output_dir = asset / "texture" / "substance"
            output_dir.mkdir(parents=True)
            paths = []
            for role in REQUIRED_ROLES:
                path = output_dir / f"T_leaf_test_{role}.tga"
                path.write_bytes(role.encode())
                paths.append(path)

            manifest = record_canonical_output(
                {
                    "folder": str(asset),
                    "texture_dir": str(output_dir),
                    "texture_base": "T_leaf_test",
                },
                paths,
                producer_source=output_dir / "tree_test.sbs",
            )

            self.assertEqual(manifest, asset / "texture" / MANIFEST_NAME)
            payload = json.loads(manifest.read_text(encoding="utf-8"))
            self.assertEqual(
                payload["outputs"][0]["files"],
                {
                    role: f"substance/T_leaf_test_{role}.tga"
                    for role in REQUIRED_ROLES
                },
            )
            validate_manifest(payload, manifest)

    def test_rejects_asset_local_derived_cache_subdirectory(self):
        with tempfile.TemporaryDirectory() as temporary:
            asset = Path(temporary) / "tree_test"
            output_dir = asset / "texture" / ".sk_batch_isolated_bark"
            output_dir.mkdir(parents=True)
            paths = []
            for role in REQUIRED_ROLES:
                path = output_dir / f"T_leaf_test_{role}.tga"
                path.write_bytes(role.encode())
                paths.append(path)

            with self.assertRaisesRegex(
                CanonicalOutputManifestError,
                "derived cache directory",
            ):
                record_canonical_output(
                    {
                        "folder": str(asset),
                        "texture_dir": str(output_dir),
                        "texture_base": "T_leaf_test",
                    },
                    paths,
                    producer_source="test.sbs#T_leaf_test",
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
