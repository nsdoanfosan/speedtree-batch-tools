import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from cluster_normalization_sync import (
    ClusterNormalizationSyncError,
    ClusterSourceBuildRequiredError,
    resolve_normalization_recipe,
)


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_spm(path, material_name, material_id):
    path.write_text(
        (
            "<SpeedTree><Assets>"
            f'<Material_v8 ID="{material_id}" Name="{material_name}" />'
            "</Assets></SpeedTree>"
        ),
        encoding="utf-8",
    )


def write_spm_with_frond_material(path, material_name, material_id):
    path.write_text(
        (
            "<SpeedTree><Assets>"
            f'<Material_v8 ID="{material_id}" Name="{material_name}" />'
            "</Assets><Generators><Generator Type=\"Frond\">"
            "<Name>Frond</Name><Hidden>false</Hidden><Properties><Property>"
            "<Name>Material:Frond:0:Material</Name>"
            f"<Value>{material_id}</Value>"
            "</Property></Properties></Generator></Generators></SpeedTree>"
        ),
        encoding="utf-8",
    )


def write_spm_with_frond_binding(
    path,
    material_name,
    material_id,
    mesh_id,
    *,
    generator_guid="stable-frond-guid",
    material_mesh_ids=None,
):
    material_mesh_ids = list(material_mesh_ids or [])
    cutout_xml = ""
    if material_mesh_ids:
        cutout_xml = (
            f"<CutoutMeshID>{material_mesh_ids[0]}</CutoutMeshID>"
            "<SupplementalCutoutMeshIDs>"
            + "".join(
                f'<CutoutMesh ID="{mesh_id}" />'
                for mesh_id in material_mesh_ids[1:]
            )
            + "</SupplementalCutoutMeshIDs>"
        )
    path.write_text(
        (
            "<SpeedTree><Assets>"
            f'<Material_v8 ID="{material_id}" Name="{material_name}">'
            f"{cutout_xml}</Material_v8>"
            "</Assets><Generators><Generator Type=\"Frond\">"
            f"<GUID>{generator_guid}</GUID>"
            "<Name>Frond</Name><Hidden>false</Hidden><Properties><Property>"
            "<Name>Material:Frond:0:Material</Name>"
            f"<Value>{material_id}</Value>"
            "</Property><Property>"
            "<Name>Material:Frond:0:Mesh</Name>"
            f"<Value>{mesh_id}</Value>"
            "</Property></Properties></Generator></Generators></SpeedTree>"
        ),
        encoding="utf-8",
    )


def write_unit_probe(path):
    path.write_text(
        json.dumps(
            {
                "kind": "speedtree_fbx_spm_unit_probe",
                "version": 1,
                "status": "verified",
                "physical_target_meters": 0.1,
                "selected": {
                    "mesh_geometry_scale": 1.0,
                    "mesh_asset_scale": 0.1,
                    "generator_scale": 1.0,
                    "scale_location": "SPM_MESH_ASSET",
                    "effective_scale": 0.1,
                },
            }
        ),
        encoding="utf-8",
    )


def write_capture_manifest(recipe):
    path = (
        Path(recipe["capture_output_dir"])
        / f"{recipe['capture_prefix']}_auto_capture_manifest.json"
    )
    path.write_bytes(b"verified-capture-manifest")
    return path


class ClusterNormalizationSyncTests(unittest.TestCase):
    def fixture(self, temporary):
        owner = Path(temporary) / "Tree_elm"
        cluster = owner / "Cluster"
        reports = cluster / "reports"
        xml_dir = cluster / "xml"
        reports.mkdir(parents=True)
        xml_dir.mkdir()
        blend = cluster / "SK_leaf_elm_01.blend"
        blend.write_bytes(b"raw-bwr-blend")
        source = cluster / "SK_leaf_elm_01.spm"
        write_spm(source, "M_source_unused", 1)
        target = owner / "SK_Tree_elm_01.spm"
        write_spm_with_frond_material(target, "M_leaf_elm_01", 6)
        source_xml = xml_dir / "SK_leaf_elm_01.xml"
        source_xml.write_text("<SpeedTreeRaw />", encoding="utf-8")
        report = reports / (
            "SK_leaf_elm_01_speedtree_repair_pipeline_report_codex.json"
        )
        report.write_text(
            json.dumps(
                {
                    "status": "done",
                    "paths": {
                        "merged_name": (
                            "SK_leaf_elm_01_Codex_MergedSkinned_WeightsFixed"
                        )
                    },
                    "speedtree_live_source_identity": {
                        "spm": {
                            "canonical_path": str(source.absolute()),
                            "sha256": sha256(source),
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        unit_probe = Path(temporary) / "unit_probe.json"
        write_unit_probe(unit_probe)
        return blend, source, target, unit_probe

    def test_resolves_leaf_recipe_from_current_bwr_and_target_material(self):
        with tempfile.TemporaryDirectory() as temporary:
            blend, source, target, unit_probe = self.fixture(temporary)

            recipe = resolve_normalization_recipe(
                blend,
                [target],
                canonical_spm=source,
                unit_probe_path=unit_probe,
                capture_resolution=1024,
            )

            self.assertTrue(recipe["normalization_required"])
            self.assertEqual(recipe["role"], "leaf")
            self.assertEqual(recipe["capture_plane"], "XY")
            self.assertEqual(recipe["plan_base"], "leaf_elm_01")
            self.assertEqual(recipe["skeletal_base"], "SK_leaf_elm_01")
            self.assertEqual(recipe["material_name"], "M_leaf_elm_01")
            self.assertEqual(recipe["source_material_name"], "M_leaf_elm_01")
            self.assertEqual(recipe["source_material_id"], 6)
            self.assertTrue(recipe["adopt_source_material"])
            self.assertEqual(recipe["plan_collection"], "Atlas_Cluster_Cards")

    def test_recipe_repairs_only_exact_backup_proven_minus9_sentinel(self):
        with tempfile.TemporaryDirectory() as temporary:
            blend, source, target, unit_probe = self.fixture(temporary)
            write_spm_with_frond_binding(
                target,
                "M_leaf_elm_01",
                6,
                -9,
            )
            backup_dir = (
                target.parent
                / "_spm_backups"
                / "generator_sync_20260727_010203"
            )
            backup_dir.mkdir(parents=True)
            backup = backup_dir / f"01_{target.name}"
            write_spm_with_frond_binding(
                backup,
                "M_leaf_elm_01",
                19,
                -10,
            )

            recipe = resolve_normalization_recipe(
                blend,
                [target],
                canonical_spm=source,
                unit_probe_path=unit_probe,
            )

            repairs = recipe["target_material_bindings"][0][
                "source_binding_repairs"
            ]
            self.assertEqual(len(repairs), 1)
            self.assertEqual(repairs[0]["from_mesh_id"], -9)
            self.assertEqual(repairs[0]["to_mesh_id"], -10)
            self.assertEqual(
                repairs[0]["generator_guid"],
                "stable-frond-guid",
            )
            self.assertEqual(repairs[0]["evidence"][0]["path"], str(backup))

            conflicting = backup_dir / f"02_{target.name}"
            write_spm_with_frond_binding(
                conflicting,
                "M_leaf_elm_01",
                21,
                -9,
            )
            conflicting_recipe = resolve_normalization_recipe(
                blend,
                [target],
                canonical_spm=source,
                unit_probe_path=unit_probe,
            )
            self.assertEqual(
                conflicting_recipe["target_material_bindings"][0][
                    "source_binding_repairs"
                ],
                [],
            )

    def test_recipe_repairs_atlas_managed_orphan_from_authored_backup(self):
        with tempfile.TemporaryDirectory() as temporary:
            blend, source, target, unit_probe = self.fixture(temporary)
            write_spm_with_frond_binding(
                target,
                "M_leaf_elm_01",
                6,
                107,
                material_mesh_ids=[2, 3, 4, 5, 6],
            )
            backup_dir = (
                target.parent
                / "_spm_backups"
                / "generator_sync_20260727_010203"
            )
            backup_dir.mkdir(parents=True)
            authored = backup_dir / f"01_{target.name}"
            write_spm_with_frond_binding(
                authored,
                "M_leaf_elm_01",
                19,
                5,
                material_mesh_ids=[2, 3, 4, 5, 6],
            )
            managed = backup_dir / f"02_{target.name}"
            write_spm_with_frond_binding(
                managed,
                "M_leaf_elm_01",
                20,
                107,
                material_mesh_ids=[106, 107, 108],
            )
            manifest_dir = target.parent / ".atlas_leaf_speedtree_targets"
            manifest_dir.mkdir()
            (manifest_dir / "target.json").write_text(
                json.dumps(
                    {
                        "spm": str(target),
                        "generator_connection": {
                            "complete": True,
                            "bindings": [
                                {
                                    "generator_guid": "stable-frond-guid",
                                    "slot_prefix": "Material:Frond:0",
                                    "source_mesh_id": None,
                                    "target_mesh_id": 107,
                                }
                            ],
                        },
                    }
                ),
                encoding="utf-8",
            )

            recipe = resolve_normalization_recipe(
                blend,
                [target],
                canonical_spm=source,
                unit_probe_path=unit_probe,
            )

            repairs = recipe["target_material_bindings"][0][
                "source_binding_repairs"
            ]
            self.assertEqual(len(repairs), 1)
            self.assertEqual(repairs[0]["from_mesh_id"], 107)
            self.assertEqual(repairs[0]["to_mesh_id"], 5)
            self.assertEqual(
                [item["path"] for item in repairs[0]["evidence"]],
                [str(authored)],
            )

    def test_pending_cluster_export_is_accepted_only_with_ready_source_contract(self):
        with tempfile.TemporaryDirectory() as temporary:
            blend, source, target, unit_probe = self.fixture(temporary)
            report_path = (
                blend.parent
                / "reports"
                / (
                    blend.stem
                    + "_speedtree_repair_pipeline_report_codex.json"
                )
            )
            report = json.loads(report_path.read_text(encoding="utf-8"))
            report["handoff_preflight"] = {
                "status": "cluster_export_pending",
                "unreal_push_ready": False,
            }
            report["cluster_source_build_contract"] = {
                "status": "ready",
                "mode": "raw_source_for_cluster_normalizer",
                "final_export_required": True,
                "deferred_export_issues": ["missing_export_collection"],
                "source_blend_committed": True,
                "source_object": report["paths"]["merged_name"],
            }
            report_path.write_text(json.dumps(report), encoding="utf-8")

            recipe = resolve_normalization_recipe(
                blend,
                [target],
                canonical_spm=source,
                unit_probe_path=unit_probe,
            )

            self.assertTrue(recipe["normalization_required"])

    def test_pending_cluster_export_without_ready_source_contract_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            blend, source, target, unit_probe = self.fixture(temporary)
            report_path = (
                blend.parent
                / "reports"
                / (
                    blend.stem
                    + "_speedtree_repair_pipeline_report_codex.json"
                )
            )
            report = json.loads(report_path.read_text(encoding="utf-8"))
            report["handoff_preflight"] = {
                "status": "cluster_export_pending",
            }
            report_path.write_text(json.dumps(report), encoding="utf-8")

            with self.assertRaises(ClusterSourceBuildRequiredError):
                resolve_normalization_recipe(
                    blend,
                    [target],
                    canonical_spm=source,
                    unit_probe_path=unit_probe,
                )

    def test_explicitly_blocked_handoff_is_not_reused_as_normalizer_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            blend, source, target, unit_probe = self.fixture(temporary)
            report_path = (
                blend.parent
                / "reports"
                / (
                    blend.stem
                    + "_speedtree_repair_pipeline_report_codex.json"
                )
            )
            report = json.loads(report_path.read_text(encoding="utf-8"))
            report["handoff_preflight"] = {
                "status": "blocked",
                "empty_material_slots": [
                    {
                        "object": report["paths"]["merged_name"],
                        "slot": 0,
                    }
                ],
            }
            report_path.write_text(json.dumps(report), encoding="utf-8")

            with self.assertRaises(ClusterSourceBuildRequiredError) as caught:
                resolve_normalization_recipe(
                    blend,
                    [target],
                    canonical_spm=source,
                    unit_probe_path=unit_probe,
                )

            self.assertEqual(caught.exception.reason, "source_handoff_blocked")

    def test_existing_material_spelling_is_preserved_for_in_place_adoption(self):
        with tempfile.TemporaryDirectory() as temporary:
            blend, source, target, unit_probe = self.fixture(temporary)
            write_spm_with_frond_material(target, "M_Leaf_elm_01", 6)

            recipe = resolve_normalization_recipe(
                blend,
                [target],
                canonical_spm=source,
                unit_probe_path=unit_probe,
            )

            self.assertEqual(recipe["material_name"], "M_Leaf_elm_01")
            self.assertEqual(recipe["source_material_name"], "M_Leaf_elm_01")
            self.assertEqual(
                recipe["target_material_bindings"][0][
                    "generated_material_name"
                ],
                "M_Leaf_elm_01",
            )
            self.assertTrue(recipe["adopt_source_material"])

    def test_missing_output_material_uses_existing_family_source_and_creates_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            owner = Path(temporary) / "tree_densiflora"
            cluster = owner / "cluster"
            reports = cluster / "reports"
            xml_dir = cluster / "xml"
            reports.mkdir(parents=True)
            xml_dir.mkdir()
            blend = cluster / "SK_cluster_densiflora_02.blend"
            blend.write_bytes(b"raw-bwr-blend")
            source = cluster / "SK_cluster_densiflora_02.spm"
            write_spm(source, "M_cluster_densiflora_atlas_02", 7)
            target = owner / "SK_tree_densiflora_01.spm"
            write_spm_with_frond_material(
                target,
                "M_cluster_densiflora_01",
                5,
            )
            source_xml = xml_dir / "SK_cluster_densiflora_02.xml"
            source_xml.write_text("<SpeedTreeRaw />", encoding="utf-8")
            report = reports / (
                "SK_cluster_densiflora_02_"
                "speedtree_repair_pipeline_report_codex.json"
            )
            report.write_text(
                json.dumps(
                    {
                        "status": "done",
                        "paths": {"merged_name": "MergedCluster"},
                        "speedtree_live_source_identity": {
                            "spm": {
                                "canonical_path": str(source.absolute()),
                                "sha256": sha256(source),
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            unit_probe = Path(temporary) / "unit_probe.json"
            write_unit_probe(unit_probe)

            recipe = resolve_normalization_recipe(
                blend,
                [target],
                canonical_spm=source,
                unit_probe_path=unit_probe,
            )

            self.assertEqual(
                recipe["material_name"],
                "M_cluster_densiflora_02",
            )
            self.assertEqual(
                recipe["source_material_name"],
                "M_cluster_densiflora_01",
            )
            self.assertEqual(recipe["source_material_id"], 5)
            self.assertFalse(recipe["adopt_source_material"])
            self.assertFalse(
                recipe["target_material_bindings"][0][
                    "connect_generators"
                ]
            )
            self.assertEqual(
                recipe["target_material_bindings"][0]["resolution"],
                "create_output_assets_only",
            )
    def test_current_content_addressed_receipt_skips_rebuild(self):
        with tempfile.TemporaryDirectory() as temporary:
            blend, source, target, unit_probe = self.fixture(temporary)
            first = resolve_normalization_recipe(
                blend,
                [target],
                canonical_spm=source,
                unit_probe_path=unit_probe,
            )
            capture_manifest = write_capture_manifest(first)
            receipt = Path(first["receipt_path"])
            receipt.write_text(
                json.dumps(
                    {
                        "kind": "speedtree_cluster_sync_normalization",
                        "status": "ready",
                        "recipe_sha256": first["recipe_sha256"],
                        "source_spm_sha256": first["source_spm_sha256"],
                        "unit_probe_sha256": first["unit_probe_sha256"],
                        "output_blend_sha256": sha256(blend),
                        "capture_manifest": str(capture_manifest.absolute()),
                        "capture_manifest_sha256": sha256(capture_manifest),
                    }
                ),
                encoding="utf-8",
            )

            current = resolve_normalization_recipe(
                blend,
                [target],
                canonical_spm=source,
                unit_probe_path=unit_probe,
            )

            self.assertFalse(current["normalization_required"])

    def test_texture_normalize_raw_drift_reuses_semantic_receipt_only_with_proof(self):
        with tempfile.TemporaryDirectory() as temporary:
            blend, source, target, unit_probe = self.fixture(temporary)
            first = resolve_normalization_recipe(
                blend,
                [target],
                canonical_spm=source,
                unit_probe_path=unit_probe,
            )
            capture_manifest = write_capture_manifest(first)
            receipt = Path(first["receipt_path"])
            receipt.write_text(
                json.dumps(
                    {
                        "kind": "speedtree_cluster_sync_normalization",
                        "status": "ready",
                        "normalization_contract_sha256": first[
                            "normalization_contract_sha256"
                        ],
                        "source_spm_sha256": first["source_spm_sha256"],
                        "source_spm_semantic_projection_version": first[
                            "source_spm_semantic_projection_version"
                        ],
                        "source_spm_semantic_fingerprint": first[
                            "source_spm_semantic_fingerprint"
                        ],
                        "unit_probe_sha256": first["unit_probe_sha256"],
                        "output_blend_sha256": sha256(blend),
                        "capture_manifest": str(capture_manifest.absolute()),
                        "capture_manifest_sha256": sha256(capture_manifest),
                    }
                ),
                encoding="utf-8",
            )
            backup_dir = (
                source.parent
                / "reports"
                / "texture_normalize_backups"
                / "texture_normalize_20260729_030505"
            )
            backup_dir.mkdir(parents=True)
            (backup_dir / f"0001_{source.name}").write_bytes(
                source.read_bytes()
            )
            (source.parent / "reports" / "normalize.json").write_text(
                json.dumps(
                    {
                        "status": "ok",
                        "normalization": {
                            "backup_dir": str(backup_dir),
                            "spms": [str(source)],
                        },
                    }
                ),
                encoding="utf-8",
            )

            write_spm(source, "M_source_texture_rebound", 1)
            current = resolve_normalization_recipe(
                blend,
                [target],
                canonical_spm=source,
                unit_probe_path=unit_probe,
            )
            self.assertEqual(
                current["normalization_contract_sha256"],
                first["normalization_contract_sha256"],
            )
            self.assertNotEqual(
                current["source_spm_sha256"],
                first["source_spm_sha256"],
            )
            self.assertFalse(current["normalization_required"])

            source.write_text(
                (
                    "<SpeedTree><Generators><Generator Type=\"Leaf\">"
                    "<Name>new-structural-generator</Name>"
                    "</Generator></Generators></SpeedTree>"
                ),
                encoding="utf-8",
            )
            with self.assertRaises(ClusterSourceBuildRequiredError):
                resolve_normalization_recipe(
                    blend,
                    [target],
                    canonical_spm=source,
                    unit_probe_path=unit_probe,
                )

    def test_report_diagnostic_metadata_does_not_rebuild_normalization(self):
        with tempfile.TemporaryDirectory() as temporary:
            blend, source, target, unit_probe = self.fixture(temporary)
            first = resolve_normalization_recipe(
                blend,
                [target],
                canonical_spm=source,
                unit_probe_path=unit_probe,
            )
            report_path = Path(first["bwr_report"])
            report = json.loads(report_path.read_text(encoding="utf-8"))
            report["artifact_validation"] = [{
                "status": "content_exact_metadata_drift",
                "metadata_drift": ["mtime_ns"],
                "observed_mtime_ns": 123456,
            }]
            report_path.write_text(
                json.dumps(report),
                encoding="utf-8",
            )

            second = resolve_normalization_recipe(
                blend,
                [target],
                canonical_spm=source,
                unit_probe_path=unit_probe,
            )

            self.assertEqual(
                second["normalization_contract_sha256"],
                first["normalization_contract_sha256"],
            )

    def test_adding_owner_target_does_not_rebuild_unchanged_normalization(self):
        with tempfile.TemporaryDirectory() as temporary:
            blend, source, target, unit_probe = self.fixture(temporary)
            first = resolve_normalization_recipe(
                blend,
                [target],
                canonical_spm=source,
                unit_probe_path=unit_probe,
            )
            capture_manifest = write_capture_manifest(first)
            receipt = Path(first["receipt_path"])
            receipt.write_text(
                json.dumps(
                    {
                        "kind": "speedtree_cluster_sync_normalization",
                        "status": "ready",
                        "normalization_contract_sha256": first[
                            "normalization_contract_sha256"
                        ],
                        "recipe_sha256": first["recipe_sha256"],
                        "source_spm_sha256": first["source_spm_sha256"],
                        "unit_probe_sha256": first["unit_probe_sha256"],
                        "output_blend_sha256": sha256(blend),
                        "capture_manifest": str(capture_manifest.absolute()),
                        "capture_manifest_sha256": sha256(capture_manifest),
                    }
                ),
                encoding="utf-8",
            )
            second_target = target.with_name("SK_Tree_elm_02.spm")
            write_spm_with_frond_material(
                second_target,
                "M_leaf_elm_01",
                6,
            )

            expanded = resolve_normalization_recipe(
                blend,
                [target, second_target],
                canonical_spm=source,
                unit_probe_path=unit_probe,
            )

            self.assertEqual(
                expanded["normalization_contract_sha256"],
                first["normalization_contract_sha256"],
            )
            self.assertFalse(expanded["normalization_required"])

    def test_version_one_receipt_migrates_without_target_list_rebuild(self):
        with tempfile.TemporaryDirectory() as temporary:
            blend, source, target, unit_probe = self.fixture(temporary)
            first = resolve_normalization_recipe(
                blend,
                [target],
                canonical_spm=source,
                unit_probe_path=unit_probe,
            )
            capture_manifest = write_capture_manifest(first)
            receipt = Path(first["receipt_path"])
            receipt.write_text(
                json.dumps(
                    {
                        "kind": "speedtree_cluster_sync_normalization",
                        "version": 1,
                        "status": "ready",
                        "blend": str(blend.absolute()),
                        "output_blend_sha256": sha256(blend),
                        "canonical_spm": str(source.absolute()),
                        "source_spm_sha256": first["source_spm_sha256"],
                        "source_xml": first["source_xml"],
                        "unit_probe_sha256": first["unit_probe_sha256"],
                        "recipe_sha256": "legacy-target-list-dependent-hash",
                        "capture_manifest": str(capture_manifest.absolute()),
                        "capture_manifest_sha256": sha256(capture_manifest),
                        "material": first["material_name"],
                        "build": {
                            "workflow_mode": "PHYSICAL_DIRECT_CAPTURE",
                            "source_object": first["source_object"],
                            "source_partition_mode": first[
                                "source_partition_mode"
                            ],
                            "plan_collection": first["plan_collection"],
                            "plan_refinement_levels": first[
                                "plan_refinement_levels"
                            ],
                            "source_3d_contract": {
                                "xml_sha256": first["source_xml_sha256"],
                            },
                            "physical_capture_contract": {
                                "workflow_mode": "PHYSICAL_DIRECT_CAPTURE",
                                "capture_resolution": [1024, 1024],
                                "frame": {
                                    "plane": first["capture_plane"],
                                    "padding_ratio": first[
                                        "capture_padding_ratio"
                                    ],
                                    "target_meters": [0.1, 0.1],
                                },
                            },
                        },
                    }
                ),
                encoding="utf-8",
            )
            second_target = target.with_name("SK_Tree_elm_02.spm")
            write_spm_with_frond_material(
                second_target,
                "M_leaf_elm_01",
                6,
            )

            migrated = resolve_normalization_recipe(
                blend,
                [target, second_target],
                canonical_spm=source,
                unit_probe_path=unit_probe,
            )

            self.assertFalse(migrated["normalization_required"])

    def test_changed_source_rejects_stale_bwr_before_blender(self):
        with tempfile.TemporaryDirectory() as temporary:
            blend, source, target, unit_probe = self.fixture(temporary)
            source.write_bytes(b"changed-after-bwr")

            with self.assertRaises(ClusterNormalizationSyncError) as caught:
                resolve_normalization_recipe(
                    blend,
                    [target],
                    canonical_spm=source,
                    unit_probe_path=unit_probe,
                )

            self.assertIn(
                "Rebuild the Cluster source blend first",
                str(caught.exception),
            )


if __name__ == "__main__":
    unittest.main()
