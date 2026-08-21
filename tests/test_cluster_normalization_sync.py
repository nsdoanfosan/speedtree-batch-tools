import hashlib
import json
import tempfile
import unittest
from pathlib import Path

import cluster_normalization_sync as normalization_sync
from cluster_atlas_source_index import (
    COLLECTION_CONTENT_KEY_ALGORITHM,
    COLLECTION_PROJECTION_VERSION,
    MESH_CONTENT_KEY_ALGORITHM,
    SOURCE_INDEX_KIND,
    SOURCE_INDEX_VERSION,
    canonical_sha256,
)
from cluster_normalization_sync import (
    ClusterNormalizationSyncError,
    ClusterSourceBuildRequiredError,
    inspect_normalization_source_identity,
    normalization_receipt_path,
    resolve_normalization_recipe,
    validate_isolated_bark_recipe_bundle,
)
from cluster_bark_source_resolution import _provider_identity
from generator_delivery_scope import (
    CONTINUITY_ONLY_POLICY,
    INTENT_KIND,
    RUNTIME_INACTIVE_POLICY,
    canonical_sha256,
)


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def persisted_source_index(
    blend,
    *,
    collection="Atlas_Cluster_Cards",
    export_scope_id="test-cluster-scope",
    state="populated",
):
    rows = [] if state == "empty" else [{
        "object_name": f"{blend.stem}_Card",
        "mesh_data_name": f"{blend.stem}_Card_Mesh",
        "group_collection": collection,
        "group_material": f"M_{blend.stem}",
        "user_collections": [collection],
        "stable_source_identity": {
            "kind": "fixture",
            "digest": hashlib.sha256(b"fixture-object").hexdigest(),
        },
        "mesh_content_key": {
            "algorithm": MESH_CONTENT_KEY_ALGORITHM,
            "digest": hashlib.sha256(b"fixture-mesh").hexdigest(),
        },
        "vertices": 4,
        "edges": 4,
        "loops": 4,
        "polygons": 1,
    }]
    projection = {
        "projection_version": COLLECTION_PROJECTION_VERSION,
        "collection_name": collection,
        "export_scope_id": export_scope_id,
        "state": state,
        "mesh_object_count": len(rows),
        "mesh_objects": rows,
    }
    blend_hash = sha256(blend)
    return {
        "kind": SOURCE_INDEX_KIND,
        "version": SOURCE_INDEX_VERSION,
        "status": "ok",
        "indexed_by_blender": True,
        "blend": str(Path(blend).absolute()),
        "blend_sha256": blend_hash,
        "atlas_source_index": {
            "schema_version": 1,
            "status": "ok",
            "indexed_by_blender": True,
            "blend": str(Path(blend).absolute()),
            "blend_sha256": blend_hash,
            "image_count": 0,
            "images": [],
        },
        "authoritative_collection": {
            **projection,
            "content_key": {
                "algorithm": COLLECTION_CONTENT_KEY_ALGORITHM,
                "digest": canonical_sha256(projection),
            },
        },
        "refresh_reasons": [],
        "publication": {
            "status": "bound",
            "target_count": 1,
            "targets": [{
                "target_spm": str(blend.with_suffix(".spm")),
                "export_scope_id": export_scope_id,
                "mesh_count": len(rows),
                "state": state,
                "manifest": None,
            }],
        },
    }


def seal_receipt_source_identity(blend, receipt_path, recipe):
    receipt_path = Path(receipt_path)
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["blend"] = str(Path(blend).absolute())
    receipt["source_blender_index"] = persisted_source_index(
        blend,
        collection=recipe["plan_collection"],
    )
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")


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


def write_spm_with_managed_duplicate(
    path,
    material_name,
    source_material_id,
    managed_material_id,
):
    marker = json.dumps(
        {
            "generator": "Atlas Leaf Mesh Builder",
            "scope": "legacy-output-scope",
            "kind": "material",
            "group": "Atlas_Cluster_Cards",
        }
    )
    path.write_text(
        (
            "<SpeedTree><Assets>"
            f'<Material_v8 ID="{source_material_id}" '
            f'Name="{material_name}" />'
            f'<Material_v8 ID="{managed_material_id}" '
            f'Name="{material_name}"><UserData>{marker}</UserData>'
            "</Material_v8>"
            "</Assets><Generators><Generator Type=\"Frond\">"
            "<Name>Frond</Name><Hidden>false</Hidden><Properties><Property>"
            "<Name>Material:Frond:0:Material</Name>"
            f"<Value>{source_material_id}</Value>"
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


def report_fingerprint(path):
    path = Path(path).resolve()
    return {
        "path": str(path),
        "exists": True,
        "size": path.stat().st_size,
        "mtime_ns": path.stat().st_mtime_ns,
        "sha256": sha256(path),
    }


def delivery_scope_intent(target, provider_blend, material_id=6):
    identity = ["named", "frond", "frond", "material:frond:0"]
    intent = {
        "kind": INTENT_KIND,
        "schema_version": 1,
        "authority": {
            "kind": "operator_recipe",
            "id": "sanitized-issue-96",
            "provenance": {"review": "explicit"},
        },
        "target": {
            "spm": str(Path(target).resolve()),
            "provider_blend": str(Path(provider_blend).resolve()),
            "provider_scope_id": "sanitized-cluster-scope",
            "material_id": material_id,
        },
        "authored_slots": [{
            "slot_identity": identity,
            "target_material_id": material_id,
            "target_mesh_id": 93,
        }],
        "required_live_slot_identities": [],
        "continuity_only_slots": [{
            "slot_identity": identity,
            "reason": "operator-authored continuity",
            "policy": CONTINUITY_ONLY_POLICY,
            "provenance": {"review": "explicit"},
        }],
        "runtime_inactive_policy": RUNTIME_INACTIVE_POLICY,
    }
    intent["intent_sha256"] = canonical_sha256(intent)
    return intent


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
            "SK_leaf_elm_01_speedtree_assembly_pipeline_report_codex.json"
        )
        report.write_text(
            json.dumps(
                {
                    "status": "done",
                    "paths": {
                        "merged_name": (
                            "SK_leaf_elm_01_Codex_Assembled"
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

    def attach_isolated_bark_bundle(self, blend, source):
        cache = blend.parent / ".sk_batch_isolated_bark" / "neutral"
        isolated = cache / "Tree_neutral" / "Cluster" / source.name
        isolated.parent.mkdir(parents=True)
        isolated.write_bytes(source.read_bytes())
        manifest = cache / "bark_normalization_manifest.json"
        provider_identity = _provider_identity(source)
        manifest.write_text(
            json.dumps({
                "schema_version": 2,
                "kind": "cluster_isolated_canonical_bark_source",
                "status": "ready",
                "signature": "neutral-provider-signature",
                "provider_identity": provider_identity,
                "output_filename": source.name,
                "source_spm": str(source.resolve()),
                "source_spm_sha256": sha256(source),
                "speedtree_spm": str(isolated.resolve()),
                "isolated_spm_sha256": sha256(isolated),
                "identity": {
                    "provider_identity": provider_identity,
                },
                "copied_source_tree_textures": [],
                "copied_source_external_meshes": [],
                "copied_source_external_textures": [],
                "copied_canonical_textures": [],
                "production_source_mutated": False,
            }),
            encoding="utf-8",
        )
        source_xml = blend.parent / "xml" / f"{blend.stem}.xml"
        source_xml.write_text(
            f"<SpeedTreeRaw><SourceTree>{isolated}</SourceTree></SpeedTreeRaw>",
            encoding="utf-8",
        )
        report_path = blend.parent / "reports" / (
            f"{blend.stem}_speedtree_assembly_pipeline_report_codex.json"
        )
        report = json.loads(report_path.read_text(encoding="utf-8"))
        report["cluster_bark_source_resolution"] = {
            "status": "ready",
            "manifest": report_fingerprint(manifest),
            "source_spm": report_fingerprint(source),
            "speedtree_spm": report_fingerprint(isolated),
            "production_source_mutated": False,
        }
        report_path.write_text(json.dumps(report), encoding="utf-8")
        return source_xml, isolated, manifest

    def test_isolated_bark_recipe_seals_exact_xml_manifest_and_spm(self):
        with tempfile.TemporaryDirectory() as temporary:
            blend, source, target, unit_probe = self.fixture(temporary)
            _source_xml, isolated, manifest = (
                self.attach_isolated_bark_bundle(blend, source)
            )

            recipe = resolve_normalization_recipe(
                blend,
                [target],
                canonical_spm=source,
                unit_probe_path=unit_probe,
            )

            bundle = recipe["isolated_bark_bundle"]
            self.assertEqual(bundle["speedtree_spm"], str(isolated.resolve()))
            self.assertEqual(bundle["manifest"], str(manifest.resolve()))
            self.assertEqual(bundle["output_filename"], source.name)
            self.assertIs(
                validate_isolated_bark_recipe_bundle(recipe),
                bundle,
            )

    def test_explicit_delivery_scope_is_validated_and_passed_through_verbatim(self):
        with tempfile.TemporaryDirectory() as temporary:
            blend, source, target, unit_probe = self.fixture(temporary)
            intent = delivery_scope_intent(target, blend)
            legacy_recipe = resolve_normalization_recipe(
                blend,
                [target],
                canonical_spm=source,
                unit_probe_path=unit_probe,
            )

            recipe = resolve_normalization_recipe(
                blend,
                [target],
                canonical_spm=source,
                unit_probe_path=unit_probe,
                delivery_scope_intents={str(target): intent},
            )

            self.assertEqual(
                recipe["target_material_bindings"][0][
                    "generator_delivery_scope_intent"
                ],
                intent,
            )
            self.assertEqual(
                recipe["normalization_contract_sha256"],
                legacy_recipe["normalization_contract_sha256"],
            )

    def test_tampered_delivery_scope_is_rejected_before_recipe_emission(self):
        with tempfile.TemporaryDirectory() as temporary:
            blend, source, target, unit_probe = self.fixture(temporary)
            intent = delivery_scope_intent(target, blend)
            intent["continuity_only_slots"] = []

            with self.assertRaisesRegex(
                ClusterNormalizationSyncError,
                "Explicit Generator delivery scope is invalid",
            ):
                resolve_normalization_recipe(
                    blend,
                    [target],
                    canonical_spm=source,
                    unit_probe_path=unit_probe,
                    delivery_scope_intents={str(target): intent},
                )

    def test_isolated_bark_source_tree_mismatch_fails_before_recipe(self):
        with tempfile.TemporaryDirectory() as temporary:
            blend, source, target, unit_probe = self.fixture(temporary)
            source_xml, _isolated, _manifest = (
                self.attach_isolated_bark_bundle(blend, source)
            )
            different = source.parent / "SK_branch_different_provider_01.spm"
            different.write_bytes(source.read_bytes())
            source_xml.write_text(
                f"<SpeedTreeRaw><SourceTree>{different}</SourceTree></SpeedTreeRaw>",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                ClusterNormalizationSyncError,
                "SourceTree does not name the exact",
            ):
                resolve_normalization_recipe(
                    blend,
                    [target],
                    canonical_spm=source,
                    unit_probe_path=unit_probe,
                )

    def test_missing_exact_isolated_spm_fails_before_recipe(self):
        with tempfile.TemporaryDirectory() as temporary:
            blend, source, target, unit_probe = self.fixture(temporary)
            _source_xml, isolated, _manifest = (
                self.attach_isolated_bark_bundle(blend, source)
            )
            isolated.unlink()

            with self.assertRaisesRegex(
                ClusterNormalizationSyncError,
                "exact isolated provider SPM is missing",
            ):
                resolve_normalization_recipe(
                    blend,
                    [target],
                    canonical_spm=source,
                    unit_probe_path=unit_probe,
                )

    def test_isolated_bark_recipe_rejects_spm_changed_after_preflight(self):
        with tempfile.TemporaryDirectory() as temporary:
            blend, source, target, unit_probe = self.fixture(temporary)
            _source_xml, isolated, _manifest = (
                self.attach_isolated_bark_bundle(blend, source)
            )
            recipe = resolve_normalization_recipe(
                blend,
                [target],
                canonical_spm=source,
                unit_probe_path=unit_probe,
            )
            isolated.write_bytes(isolated.read_bytes() + b"changed")

            with self.assertRaisesRegex(
                ClusterNormalizationSyncError,
                "manifest is stale|isolated_spm_sha256",
            ):
                validate_isolated_bark_recipe_bundle(recipe)

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
                                    "target_material_id": 6,
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
                    + "_speedtree_assembly_pipeline_report_codex.json"
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
                    + "_speedtree_assembly_pipeline_report_codex.json"
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
                    + "_speedtree_assembly_pipeline_report_codex.json"
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

    def test_live_generated_output_recovers_exact_provider_source_lineage(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            blend = root / "Cluster" / "SK_cluster_tree_07.blend"
            blend.parent.mkdir()
            blend.write_bytes(b"provider")
            target = root / "SK_tree_22.spm"
            target.write_text(
                (
                    "<SpeedTree><Assets>"
                    '<Material_v8 ID="3" Name="M_leaf_source">'
                    "<CutoutMeshID>2</CutoutMeshID></Material_v8>"
                    '<Material_v8 ID="17" Name="M_cluster_tree_07">'
                    "<CutoutMeshID>49</CutoutMeshID></Material_v8>"
                    "</Assets><Generators><Generator Type=\"Leaf Mesh\">"
                    "<GUID>stable-leaf-guid</GUID><Name>Leaf 10</Name>"
                    "<Hidden>false</Hidden><Properties><Property>"
                    "<Name>Leaves:Type:0:Material</Name><Value>17</Value>"
                    "</Property><Property><Name>Leaves:Type:0:Mesh</Name>"
                    "<Value>49</Value></Property></Properties></Generator>"
                    "</Generators></SpeedTree>"
                ),
                encoding="utf-8",
            )
            payload = {
                "speedtree_material_groups": [{
                    "material": "M_cluster_tree_07",
                    "material_id": 17,
                    "mesh_ids": [49],
                }],
                "generator_connection": {
                    "requested": False,
                    "complete": False,
                    "bindings": [],
                    "authored_bindings": [{
                        "generator_guid": "stable-leaf-guid",
                        "slot_prefix": "Leaves:Type:0",
                        "source_material_id": 3,
                        "source_material_name": "M_leaf_source",
                        "source_mesh_id": 2,
                        "target_material_id": 17,
                        "target_mesh_id": 49,
                    }],
                },
            }
            original_resolver = normalization_sync.resolve_atlas_manifests

            def resolved(*_args, **_kwargs):
                return {
                    "mutation_authorized": True,
                    "selected": [{
                        "path": str(root / "provider-scope.json"),
                        "payload": payload,
                    }],
                }

            normalization_sync.resolve_atlas_manifests = resolved
            try:
                binding = normalization_sync._resolve_target_role_material(
                    target,
                    "M_cluster_tree_07",
                    provider_blend=blend,
                )
                authored = payload["generator_connection"][
                    "authored_bindings"
                ][0]
                authored["source_material_id"] = 17
                authored["source_material_name"] = "M_cluster_tree_07"
                authored["source_mesh_id"] = 49
                adopted = normalization_sync._resolve_target_role_material(
                    target,
                    "M_cluster_tree_07",
                    provider_blend=blend,
                )
                payload["generator_connection"]["authored_bindings"][0][
                    "generator_guid"
                ] = "different-guid"
                unproven = normalization_sync._resolve_target_role_material(
                    target,
                    "M_cluster_tree_07",
                    provider_blend=blend,
                )
            finally:
                normalization_sync.resolve_atlas_manifests = original_resolver

            self.assertEqual(binding["source_material_name"], "M_leaf_source")
            self.assertEqual(binding["source_material_id"], 3)
            self.assertFalse(binding["adopt_source_material"])
            self.assertTrue(binding["connect_generators"])
            self.assertEqual(
                binding["resolution"],
                "refresh_connected_output_from_authored_lineage",
            )
            self.assertEqual(
                binding["authored_source_lineage"]["binding_count"],
                1,
            )
            self.assertEqual(adopted["source_material_id"], 17)
            self.assertTrue(adopted["adopt_source_material"])
            self.assertEqual(
                adopted["resolution"],
                "overwrite_connected_output_material",
            )
            self.assertEqual(unproven["source_material_id"], 17)
            self.assertTrue(unproven["adopt_source_material"])
            self.assertEqual(
                unproven["resolution"],
                "overwrite_connected_output_material",
            )

    def test_managed_unreferenced_same_name_duplicate_migrates_to_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            blend, source, target, unit_probe = self.fixture(temporary)
            write_spm_with_managed_duplicate(
                target,
                "M_leaf_elm_01",
                6,
                19,
            )

            recipe = resolve_normalization_recipe(
                blend,
                [target],
                canonical_spm=source,
                unit_probe_path=unit_probe,
            )

            binding = recipe["target_material_bindings"][0]
            self.assertEqual(binding["source_material_id"], 6)
            self.assertTrue(binding["adopt_source_material"])
            self.assertTrue(binding["connect_generators"])
            self.assertEqual(
                binding["legacy_managed_duplicate"],
                {
                    "material_id": 19,
                    "material_name": "M_leaf_elm_01",
                    "atlas_scope": "legacy-output-scope",
                    "resolution": (
                        "migrate_managed_output_into_connected_source"
                    ),
                },
            )

    def test_unowned_same_name_duplicate_remains_ambiguous(self):
        with tempfile.TemporaryDirectory() as temporary:
            blend, source, target, unit_probe = self.fixture(temporary)
            target.write_text(
                (
                    "<SpeedTree><Assets>"
                    '<Material_v8 ID="6" Name="M_leaf_elm_01" />'
                    '<Material_v8 ID="19" Name="M_leaf_elm_01" />'
                    "</Assets><Generators><Generator Type=\"Frond\">"
                    "<Name>Frond</Name><Hidden>false</Hidden><Properties>"
                    "<Property><Name>Material:Frond:0:Material</Name>"
                    "<Value>6</Value></Property></Properties></Generator>"
                    "</Generators></SpeedTree>"
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                ClusterNormalizationSyncError,
                "ambiguous duplicate output materials",
            ):
                resolve_normalization_recipe(
                    blend,
                    [target],
                    canonical_spm=source,
                    unit_probe_path=unit_probe,
                )

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
                "speedtree_assembly_pipeline_report_codex.json"
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
            seal_receipt_source_identity(blend, receipt, first)

            current = resolve_normalization_recipe(
                blend,
                [target],
                canonical_spm=source,
                unit_probe_path=unit_probe,
            )

            self.assertFalse(current["normalization_required"])

    def test_changed_source_fbx_rebuilds_stale_physical_receipt_once(self):
        with tempfile.TemporaryDirectory() as temporary:
            blend, source, target, unit_probe = self.fixture(temporary)
            source_fbx = blend.parent / "fbx" / f"{blend.stem}.fbx"
            source_fbx.parent.mkdir()
            source_fbx.write_bytes(b"bwr-export-before-cleanup")
            first = resolve_normalization_recipe(
                blend,
                [target],
                canonical_spm=source,
                unit_probe_path=unit_probe,
            )
            capture_manifest = write_capture_manifest(first)
            receipt = Path(first["receipt_path"])

            def write_current_receipt(recipe):
                receipt.write_text(
                    json.dumps({
                        "kind": "speedtree_cluster_sync_normalization",
                        "status": "ready",
                        "normalization_contract_sha256": recipe[
                            "normalization_contract_sha256"
                        ],
                        "source_spm_sha256": recipe["source_spm_sha256"],
                        "source_spm_semantic_projection_version": recipe[
                            "source_spm_semantic_projection_version"
                        ],
                        "source_spm_semantic_fingerprint": recipe[
                            "source_spm_semantic_fingerprint"
                        ],
                        "unit_probe_sha256": recipe["unit_probe_sha256"],
                        "capture_manifest": str(capture_manifest.absolute()),
                        "capture_manifest_sha256": sha256(capture_manifest),
                        "build": {
                            "source_3d_contract": {
                                "source_fbx": recipe[
                                    "source_fbx_identity"
                                ]["path"],
                                "source_fbx_sha256": recipe[
                                    "source_fbx_identity"
                                ]["sha256"],
                            },
                        },
                    }),
                    encoding="utf-8",
                )
                seal_receipt_source_identity(blend, receipt, recipe)

            write_current_receipt(first)
            unchanged = resolve_normalization_recipe(
                blend,
                [target],
                canonical_spm=source,
                unit_probe_path=unit_probe,
            )
            self.assertFalse(unchanged["normalization_required"])

            source_fbx.write_bytes(b"bwr-export-after-zero-face-cleanup")
            changed = resolve_normalization_recipe(
                blend,
                [target],
                canonical_spm=source,
                unit_probe_path=unit_probe,
            )
            self.assertTrue(changed["normalization_required"])
            self.assertNotEqual(
                changed["normalization_contract_sha256"],
                first["normalization_contract_sha256"],
            )

            write_current_receipt(changed)
            rebuilt = resolve_normalization_recipe(
                blend,
                [target],
                canonical_spm=source,
                unit_probe_path=unit_probe,
            )
            self.assertFalse(rebuilt["normalization_required"])

    def test_changed_bwr_material_assignment_rebuilds_normalized_prototypes_once(self):
        with tempfile.TemporaryDirectory() as temporary:
            blend, source, target, unit_probe = self.fixture(temporary)
            report = blend.parent / "reports" / (
                f"{blend.stem}_speedtree_assembly_pipeline_report_codex.json"
            )
            payload = json.loads(report.read_text(encoding="utf-8"))
            payload["speedtree_material_intents"] = {
                "status": "applied",
                "materials": [{
                    "match_mode": "exact_material_key",
                    "material_name": "M_leaf_elm_atlas_01_green",
                    "material_key": "mleafelmatlas01green",
                    "material_instance_base": "leaf_elm_atlas_01_green",
                    "tree_part": "leaf",
                    "tree_shading": "foliage",
                    "source_materials": [
                        "M_leaf_elm_atlas_01_green_Mat"
                    ],
                }],
                "unmatched_materials": [],
            }
            report.write_text(json.dumps(payload), encoding="utf-8")
            first = resolve_normalization_recipe(
                blend,
                [target],
                canonical_spm=source,
                unit_probe_path=unit_probe,
            )
            self.assertFalse(
                normalization_sync.inspect_bwr_material_assignment_freshness(
                    blend
                )["current"]
            )
            capture_manifest = write_capture_manifest(first)
            receipt = Path(first["receipt_path"])

            def write_current_receipt(recipe):
                receipt.write_text(
                    json.dumps({
                        "kind": "speedtree_cluster_sync_normalization",
                        "status": "ready",
                        "normalization_contract_sha256": recipe[
                            "normalization_contract_sha256"
                        ],
                        "bwr_material_assignment_sha256": recipe.get(
                            "bwr_material_assignment_sha256"
                        ),
                        "source_spm_sha256": recipe["source_spm_sha256"],
                        "source_spm_semantic_projection_version": recipe[
                            "source_spm_semantic_projection_version"
                        ],
                        "source_spm_semantic_fingerprint": recipe[
                            "source_spm_semantic_fingerprint"
                        ],
                        "unit_probe_sha256": recipe["unit_probe_sha256"],
                        "capture_manifest": str(capture_manifest.absolute()),
                        "capture_manifest_sha256": sha256(capture_manifest),
                        "build": {},
                    }),
                    encoding="utf-8",
                )
                seal_receipt_source_identity(blend, receipt, recipe)

            write_current_receipt(first)
            unchanged = resolve_normalization_recipe(
                blend,
                [target],
                canonical_spm=source,
                unit_probe_path=unit_probe,
            )
            self.assertFalse(unchanged["normalization_required"])
            self.assertTrue(
                normalization_sync.inspect_bwr_material_assignment_freshness(
                    blend
                )["current"]
            )

            payload["speedtree_material_intents"] = {
                "status": "applied",
                "materials": [{
                    "match_mode": "production_group_base",
                    "material_name": "M_leaf_elm_atlas_01",
                    "material_key": "mleafelmatlas01",
                    "material_instance_base": "leaf_elm_atlas_01",
                    "tree_part": "leaf",
                    "tree_shading": "foliage",
                    "source_materials": [
                        "M_leaf_elm_atlas_01_green_Mat"
                    ],
                }],
                "unmatched_materials": [],
            }
            payload["import"] = {
                "material_consolidation": {
                    "groups": [{
                        "mode": "production_group_suffix",
                        "target_material": "M_leaf_elm_atlas_01",
                        "source_materials": [
                            "M_leaf_elm_atlas_01_green"
                        ],
                        "group_tokens": ["green"],
                        "provenance_type": "material_intent",
                        "readiness_mode": "material_intent",
                    }],
                },
            }
            report.write_text(json.dumps(payload), encoding="utf-8")
            changed = resolve_normalization_recipe(
                blend,
                [target],
                canonical_spm=source,
                unit_probe_path=unit_probe,
            )
            self.assertTrue(changed["normalization_required"])
            self.assertFalse(
                normalization_sync.inspect_bwr_material_assignment_freshness(
                    blend
                )["current"]
            )
            self.assertNotEqual(
                changed["normalization_contract_sha256"],
                first["normalization_contract_sha256"],
            )

            write_current_receipt(changed)
            rebuilt = resolve_normalization_recipe(
                blend,
                [target],
                canonical_spm=source,
                unit_probe_path=unit_probe,
            )
            self.assertFalse(rebuilt["normalization_required"])
            self.assertTrue(
                normalization_sync.inspect_bwr_material_assignment_freshness(
                    blend
                )["current"]
            )

    def test_saved_atlas_collection_change_refreshes_without_normalizer_rebuild(self):
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
                json.dumps({
                    "kind": "speedtree_cluster_sync_normalization",
                    "status": "ready",
                    "recipe_sha256": first["recipe_sha256"],
                    "source_spm_sha256": first["source_spm_sha256"],
                    "unit_probe_sha256": first["unit_probe_sha256"],
                    "output_blend_sha256": sha256(blend),
                    "capture_manifest": str(capture_manifest.absolute()),
                    "capture_manifest_sha256": sha256(capture_manifest),
                }),
                encoding="utf-8",
            )
            seal_receipt_source_identity(blend, receipt, first)
            blend.write_bytes(b"saved-atlas-collection-edited")

            freshness = inspect_normalization_source_identity(blend)
            recipe = resolve_normalization_recipe(
                blend,
                [target],
                canonical_spm=source,
                unit_probe_path=unit_probe,
            )

            self.assertFalse(freshness["current"])
            self.assertIn(
                "blender_source_content_changed",
                freshness["refresh_reasons"],
            )
            self.assertFalse(recipe["normalization_required"])

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
            seal_receipt_source_identity(blend, receipt, first)
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
            seal_receipt_source_identity(blend, receipt, first)
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
            seal_receipt_source_identity(blend, receipt, first)
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

    def test_source_identity_inspection_is_current_then_detects_saved_change(self):
        with tempfile.TemporaryDirectory() as temporary:
            cluster = Path(temporary) / "Cluster"
            cluster.mkdir()
            blend = cluster / "SK_cluster.blend"
            blend.write_bytes(b"saved-blend-v1")
            receipt_path = normalization_receipt_path(blend)
            receipt_path.parent.mkdir()
            receipt_path.write_text(
                json.dumps({
                    "kind": "speedtree_cluster_sync_normalization",
                    "status": "ready",
                    "blend": str(blend.absolute()),
                    "output_blend_sha256": sha256(blend),
                    "source_blender_index": persisted_source_index(blend),
                }),
                encoding="utf-8",
            )

            current = inspect_normalization_source_identity(blend)
            blend.write_bytes(b"saved-blend-v2")
            changed = inspect_normalization_source_identity(blend)

            self.assertTrue(current["current"])
            self.assertFalse(changed["current"])
            self.assertIn(
                "blender_source_content_changed",
                changed["refresh_reasons"],
            )

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

    def test_provider_disagreement_disables_only_optional_binding_authority(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "SK_tree_01.spm"
            target.write_bytes(b"spm")
            authority = {
                "atlas_manifest_schema_version": 1,
                "spm": str(target),
                "blend_file": str(root / "provider_a.blend"),
                "source_collection": "Provider A",
                "export_scope_id": "provider-a",
                "material_groups": [{
                    "material": "M_shared",
                    "material_id": 7,
                    "mesh_ids": [20],
                }],
                "generator_connection": {
                    "complete": True,
                    "bindings": [],
                },
            }
            target_dir = root / ".atlas_leaf_speedtree_targets"
            target_dir.mkdir()
            (target_dir / f"{target.stem}.json").write_text(
                json.dumps(authority), encoding="utf-8"
            )
            competing = json.loads(json.dumps(authority))
            competing["blend_file"] = str(root / "provider_b.blend")
            competing["source_collection"] = "Provider B"
            competing["export_scope_id"] = "provider-b"
            competing["material_groups"][0]["mesh_ids"] = [99]
            (root / "speedtree_import_manifest.json").write_text(
                json.dumps(competing), encoding="utf-8"
            )

            selected = normalization_sync._atlas_target_relation_manifest(
                target
            )

            self.assertEqual(selected, {})


if __name__ == "__main__":
    unittest.main()
