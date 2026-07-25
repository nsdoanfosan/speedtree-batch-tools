import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from cluster_normalization_sync import (
    ClusterNormalizationSyncError,
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

            self.assertIn("Run Blender Repair first", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
