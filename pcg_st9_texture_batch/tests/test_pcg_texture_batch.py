import gzip
import importlib.machinery
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest import mock

from PIL import Image


TOOL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOL_DIR))

import sbs_auto
import pcg_texture_audit
import migrate_current_sk_textures
from pcg_texture_common import is_backup_path
from export_texture_plan import build_texture_plan_from_report, extract_material_image_refs
from pcg_texture_audit import (
    assign_leaf_atlas_bases,
    atlas_base_from_cluster_stem,
    canonicalize_leaf_sources,
    canonical_material_name,
    cluster_spms,
    discover_leaf_mesh_sources,
    focus_pcg_targets,
    is_cluster_render_material,
    material_color_alpha_refs,
    material_texture_items,
    preserved_cluster_materials,
    target_mesh_source_map,
    texture_base_for_material,
)
from refresh_pcg_targets import payload
from spm_texture_normalize import (
    SLOT_SPECS,
    build_spm_patch,
    inspect_material_slots,
    jobs_from_texture_plan,
    normalize_spms_transactionally,
)


class TargetCollectionTests(unittest.TestCase):
    def test_physical_cluster_capture_receipt_tracks_color_opacity_and_resolution(self):
        with tempfile.TemporaryDirectory() as temp:
            cluster = Path(temp) / "Cluster"
            cluster.mkdir()
            canonical = cluster / "SK_branch_test_01.spm"
            canonical.touch()
            color = cluster / "branch_test_01.tga"
            opacity = cluster / "branch_test_01_Opacity.tga"
            color.touch()
            opacity.touch()
            manifest = cluster / "branch_test_01_auto_capture_manifest.json"
            manifest.write_text(json.dumps({
                "workflow_mode": "PHYSICAL_DIRECT_CAPTURE",
                "direct_uv_source": (
                    "same_blender_physical_capture_projection"
                ),
                "resolution": [1024, 1024],
                "physical_capture_contract_sha256": "capture-hash",
                "maps": [
                    {"role": "Color", "path": str(color)},
                    {"role": "Opacity", "path": str(opacity)},
                ],
            }), encoding="utf-8")

            status = pcg_texture_audit.physical_cluster_capture_status(
                canonical
            )

            self.assertTrue(status["physical_capture_core_complete"])
            self.assertEqual(
                status["normalization_workflow_mode"],
                "PHYSICAL_DIRECT_CAPTURE",
            )
            self.assertEqual(
                status["physical_capture_resolution"],
                [1024, 1024],
            )

    def test_root_level_repair_and_probe_spms_are_not_pipeline_sources(self):
        self.assertTrue(is_backup_path("SK_Tree_elm_01.pre_xml_root_fix_20260724.spm"))
        self.assertTrue(is_backup_path("SK_Tree_elm_01_frond_probe.spm"))
        self.assertFalse(is_backup_path("SK_Tree_elm_01.spm"))

    def test_local_folder_targets_include_every_numbered_spm_variant(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp) / "Tree_elm"
            folder.mkdir()
            for name in (
                "Tree_elm.spm",
                "Tree_elm_01.spm",
                "Tree_elm_02.spm",
                "Tree_elm_02_baked.spm",
                "Tree_elm_03_back.spm",
                "Tree_UlmusDavidiana_01.spm",
                "SK_Tree_elm_01.spm",
                "SK)Tree_elm_02.spm",
            ):
                (folder / name).write_bytes(b"SPM")

            names = pcg_texture_audit.local_target_mesh_names(folder)

        self.assertEqual(names, [
            "tree_elm",
            "tree_elm_01",
            "tree_elm_02",
            "tree_ulmusdavidiana_01",
        ])

    def test_default_report_audits_all_local_spm_targets(self):
        folder = Path(r"D:\Trees\Tree_elm")
        local_names = ["tree_elm_01", "tree_elm_02", "tree_elm_03"]
        audit_item = {
            "folder": str(folder),
            "name": "Tree_elm",
            "status": "ready",
            "actions": [],
        }

        with mock.patch.object(
                pcg_texture_audit, "ensure_blend_source_index"), \
                mock.patch.object(
                    pcg_texture_audit, "candidate_folders",
                    return_value=[folder]), \
                mock.patch.object(
                    pcg_texture_audit, "local_target_mesh_names",
                    return_value=local_names), \
                mock.patch.object(
                    pcg_texture_audit, "audit_folder",
                    return_value=audit_item) as audit, \
                mock.patch.object(
                    pcg_texture_audit, "attach_global_m_graphs"), \
                mock.patch.object(
                    pcg_texture_audit, "resolve_shared_atlas_entries"), \
                mock.patch.object(
                    pcg_texture_audit, "target_spm_status",
                    side_effect=lambda _folder, name: {
                        "mesh_name": name,
                        "status": "ready",
                        "actions": [],
                    }):
            report = pcg_texture_audit.make_report(
                {"pcg_focus_data_assets": [], "pcg_positive_weight_only": True},
                pcg_targets=None,
            )

        self.assertEqual(
            audit.call_args.kwargs["target_mesh_names"], local_names)
        self.assertEqual(
            [row["mesh_name"] for row in report["items"][0]["target_spm_statuses"]],
            local_names,
        )

    def test_pcg_and_level_provenance_stay_separate(self):
        targets = {
            "meshes": [
                {
                    "static_mesh": "/Game/Meshes/Tree/st9/tree_pcg.tree_pcg",
                    "sections": ["Tree"],
                    "data_assets": ["/Game/PCG/DataBase/DA_Test"],
                },
                {
                    "static_mesh": "/Game/Meshes/Tree/st9/tree_level.tree_level",
                    "sections": [],
                    "data_assets": [],
                    "level_instances": [{
                        "level": "/Game/Level/Cliff_final_01",
                        "actor": "TreeActor",
                        "instance_count": 3,
                    }],
                },
                {
                    "static_mesh": "/Game/Meshes/Tree/st9/tree_both.tree_both",
                    "sections": ["Tree"],
                    "data_assets": ["/Game/PCG/DataBase/DA_Test"],
                    "level_instances": [{
                        "level": "/Game/Level/Cliff_final_01",
                        "actor": "BothActor",
                        "instance_count": 2,
                    }],
                },
            ],
            "data_assets": [],
        }
        sources = target_mesh_source_map(targets)
        self.assertTrue(sources["tree_pcg"]["pcg"])
        self.assertFalse(sources["tree_pcg"]["levels"])
        self.assertFalse(sources["tree_level"]["pcg"])
        self.assertEqual(sources["tree_level"]["levels"], {"/Game/Level/Cliff_final_01"})
        self.assertTrue(sources["tree_both"]["pcg"])
        self.assertEqual(sources["tree_both"]["level_instances"][0]["instance_count"], 2)

    def test_focus_keeps_active_db05_db06_and_direct_level_placements(self):
        db05 = "/Game/PCG/DataBase/landscape/DA_Base_05"
        db01 = "/Game/PCG/DataBase/landscape/DA_Base_01"
        targets = {
            "meshes": [
                {"static_mesh": "/Game/st9/pcg_active.pcg_active", "data_assets": [db05]},
                {"static_mesh": "/Game/st9/pcg_zero.pcg_zero", "data_assets": [db05]},
                {"static_mesh": "/Game/st9/old_pcg.old_pcg", "data_assets": [db01]},
                {
                    "static_mesh": "/Game/st9/level_only.level_only",
                    "data_assets": [db01],
                    "level_instances": [{"level": "/Game/Level/Cliff_final_01"}],
                },
                {
                    "static_mesh": "/Game/st9/both.both",
                    "data_assets": [db05],
                    "level_instances": [{"level": "/Game/Level/Cliff_final_01"}],
                },
            ],
            "data_assets": [
                {"asset": db05, "sections": {"Tree": [
                    {"static_mesh": "/Game/st9/pcg_active.pcg_active", "weight": 1},
                    {"static_mesh": "/Game/st9/pcg_zero.pcg_zero", "weight": 0},
                    {"static_mesh": "/Game/st9/both.both", "weight": 2},
                ]}},
                {"asset": db01, "sections": {"Tree": [
                    {"static_mesh": "/Game/st9/old_pcg.old_pcg", "weight": 1},
                ]}},
            ],
        }

        focused = focus_pcg_targets(targets, [db05], positive_weight_only=True)
        sources = target_mesh_source_map(focused)

        self.assertEqual(set(sources), {"pcg_active", "level_only", "both"})
        self.assertTrue(sources["pcg_active"]["pcg"])
        self.assertFalse(sources["level_only"]["pcg"])
        self.assertTrue(sources["level_only"]["levels"])
        self.assertTrue(sources["both"]["pcg"])
        self.assertTrue(sources["both"]["levels"])
        self.assertEqual(focused["focus_data_assets"], [db05])
        self.assertEqual(len(focused["data_assets"][0]["sections"]["Tree"]), 2)
        self.assertEqual(len(targets["data_assets"][0]["sections"]["Tree"]), 3)

    def test_remote_payload_reads_explicit_level_without_switching_maps(self):
        script = payload(
            Path(r"C:\Temp\targets.json"),
            "/Game/PCG/PCG_01",
            ["/Game/Level/Cliff_final_01"],
        )
        self.assertIn("LEVEL_PATHS = ['/Game/Level/Cliff_final_01']", script)
        self.assertIn("EditorAssetLibrary.load_asset(level_path)", script)
        self.assertIn("GameplayStatics.get_all_actors_of_class(world, unreal.Actor)", script)
        self.assertNotIn("load_level(", script)


class SourceSelectionTests(unittest.TestCase):
    def test_step1_checks_only_active_unprefixed_materials(self):
        xml = b'''<SpeedTree><Materials>
<Material_v8 ID="1" Name="M_Material 2"><TexFilename>leaf.tga</TexFilename></Material_v8>
<Material_v8 ID="2" Name="Material 3"><TexFilename>unused.tga</TexFilename></Material_v8>
</Materials><Generator><Property><Name>Leaves:Material</Name><Value>1</Value>
</Property></Generator></SpeedTree>'''
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for name in ("test.spm", "SK_test.spm"):
                with gzip.open(root / name, "wb") as handle:
                    handle.write(xml)
            repair_blend = root / "SK_test.blend"
            repair_blend.write_bytes(b"BLENDER")
            os.utime(repair_blend, (1, 1))
            os.utime(root / "SK_test.spm", (2, 2))
            status = pcg_texture_audit.target_spm_status(root, "test")
            self.assertEqual(status["materials_missing_m_prefix"], [])
            self.assertEqual(status["material_renames_needed"], [])
            self.assertEqual(status["status"], "ready")
            self.assertEqual(status["actions"], [])
            self.assertNotIn("blend", status)
            self.assertNotIn("blend_stale", status)

    def test_prepare_existing_sk_without_changes_reports_up_to_date(self):
        xml = b'''<SpeedTree><Materials>
<Material_v8 ID="1" Name="M_leaf_test"><TexFilename>leaf.tga</TexFilename></Material_v8>
</Materials><Generator><Property><Name>Leaves:Material</Name><Value>1</Value>
</Property></Generator></SpeedTree>'''
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            spm = root / "SK_tree_test.spm"
            with gzip.open(spm, "wb") as handle:
                handle.write(xml)
            original = spm.read_bytes()
            original_mtime = spm.stat().st_mtime_ns

            preview = pcg_texture_audit.prepare_sk(
                root, ["tree_test"], dry_run=True)
            result = pcg_texture_audit.prepare_sk(
                root, ["tree_test"], dry_run=False)
            target = result["targets"][0]

            self.assertEqual(preview["targets"][0]["status"], "up_to_date")
            self.assertEqual(target["status"], "up_to_date")
            self.assertFalse(target["patch"]["changed"])
            self.assertIsNone(target["created"])
            self.assertEqual(spm.read_bytes(), original)
            self.assertEqual(spm.stat().st_mtime_ns, original_mtime)
            self.assertFalse((root / "_spm_backups").exists())

    def test_lowercase_m_prefix_is_normalized_instead_of_becoming_a_noop(self):
        xml = b'''<SpeedTree><Materials>
<Material_v8 ID="1" Name="m_leaf_test"><TexFilename>leaf.tga</TexFilename></Material_v8>
</Materials><Generator><Property><Name>Leaves:Material</Name><Value>1</Value>
</Property></Generator></SpeedTree>'''
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            spm = root / "SK_tree_test.spm"
            with gzip.open(spm, "wb") as handle:
                handle.write(xml)

            status = pcg_texture_audit.target_spm_status(root, "tree_test")
            result = pcg_texture_audit.prepare_sk(
                root, ["tree_test"], dry_run=False)["targets"][0]

            self.assertEqual(
                status["material_renames_needed"],
                [["m_leaf_test", "M_leaf_test"]],
            )
            self.assertEqual(result["status"], "prepared")
            self.assertTrue(result["patch"]["changed"])
            self.assertEqual(
                result["patch"]["renames"],
                [["m_leaf_test", "M_leaf_test"]],
            )
            with gzip.open(spm, "rt", encoding="utf-8") as handle:
                self.assertIn('Name="M_leaf_test"', handle.read())

    def test_repair_blend_is_not_part_of_folder_status(self):
        item = {
            "sk_spms": [r"D:\Trees\oak\SK_oak.spm"],
            "chosen_spm": r"D:\Trees\oak\SK_oak.spm",
            "materials_missing_m_prefix": [],
            "material_renames_needed": [],
            "cluster_items": [],
            "leaf_mesh_sources": [],
            "sbs_files": [],
        }
        pcg_texture_audit.derive_status_actions(item)
        self.assertEqual(item["status"], "ready")
        self.assertEqual(item["actions"], [])

    def test_relative_image_resolve_cache_is_shared_by_spms_in_one_folder(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            image = root / "textures" / "leaf.tga"
            image.parent.mkdir()
            image.write_bytes(b"image")
            cached = pcg_texture_audit._resolve_spm_image_ref_cached
            cached.cache_clear()
            try:
                first = pcg_texture_audit.resolve_spm_image_ref(
                    root / "tree.spm", r"textures\leaf.tga")
                after_first = cached.cache_info()
                second = pcg_texture_audit.resolve_spm_image_ref(
                    root / "SK_tree.spm", r"textures\leaf.tga")
                after_second = cached.cache_info()
                self.assertEqual(first, image.resolve())
                self.assertEqual(second, first)
                self.assertEqual(after_second.misses, after_first.misses)
                self.assertEqual(after_second.hits, after_first.hits + 1)
            finally:
                cached.cache_clear()

    def test_spm_image_resolve_is_lexical(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            cached = pcg_texture_audit._resolve_spm_image_ref_cached
            cached.cache_clear()
            try:
                with mock.patch.object(
                        Path, "resolve",
                        side_effect=AssertionError("filesystem resolve is not expected")):
                    result = pcg_texture_audit.resolve_spm_image_ref(
                        root / "models" / "tree.spm",
                        r"..\textures\.\missing.tga",
                    )
                expected = Path(os.path.abspath(os.path.normpath(
                    root / "models" / r"..\textures\.\missing.tga")))
                self.assertEqual(result, expected)
            finally:
                cached.cache_clear()

    def test_report_scan_cache_is_fresh_between_reports(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            first_spm = root / "first.spm"
            first_spm.write_bytes(b"one")

            @pcg_texture_audit._report_scan_cached
            def snapshot():
                first_key = pcg_texture_audit._file_cache_key(first_spm)
                second_key = pcg_texture_audit._file_cache_key(first_spm)
                first_paths = pcg_texture_audit.root_spms(root)
                second_paths = pcg_texture_audit.root_spms(root)
                cache = pcg_texture_audit._REPORT_SCAN_CACHE.get()
                return first_key, second_key, first_paths, second_paths, {
                    name: len(values) for name, values in cache.items()
                }

            first = snapshot()
            self.assertEqual(first[0], first[1])
            self.assertEqual(first[2], first[3])
            self.assertEqual(first[4]["file_cache_keys"], 1)
            self.assertEqual(first[4]["root_spms"], 1)

            first_spm.write_bytes(b"longer")
            second_spm = root / "second.spm"
            second_spm.write_bytes(b"two")
            second = snapshot()
            self.assertNotEqual(first[0][1], second[0][1])
            self.assertEqual(second[2], [first_spm, second_spm])

    def test_leaf_blend_is_found_by_embedded_source_image(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            atlas = root / "atlas"
            folder = root / "tree_test"
            atlas.mkdir()
            folder.mkdir()
            blend = atlas / "M_leaf_user_friendly_01.blend"
            blend.write_bytes(
                b"BLENDER-v300DATA\x00qgvkS_4K_Albedo.jpg\x00END")
            found = pcg_texture_audit.find_atlas_blends(
                atlas, folder, "M_qgvkS_atlas_01",
                source_images=[root / "qgvkS_4K_Albedo.jpg"],
            )
            self.assertEqual(found, [str(blend)])

    def test_shared_source_prefers_blend_named_for_current_folder(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            atlas = root / "atlas"
            folder = root / "bush_blackgum"
            atlas.mkdir()
            folder.mkdir()
            blackgum = atlas / "M_Leaf_blackgum_01.blend"
            dogwood = atlas / "M_Leaf_silky_dogwood_01.blend"
            payload = b"BLENDER\x00qgvkS_4K_Albedo.jpg\x00END"
            blackgum.write_bytes(payload)
            dogwood.write_bytes(payload)
            found = pcg_texture_audit.find_atlas_blends(
                atlas, folder, "M_qgvkS_atlas_01",
                source_images=[root / "qgvkS_4K_Albedo.jpg"],
            )
            self.assertEqual(found, [str(blackgum)])

    def test_leaf_blend_source_match_ignores_resolution_variant(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            atlas = root / "atlas"
            folder = root / "weed_violet"
            atlas.mkdir()
            folder.mkdir()
            blend = atlas / "M_leaf_violet_atlas_02.blend"
            blend.write_bytes(
                b"BLENDER\x00TCom_Leaves_ForestViolet01_4K_albedo.tif\x00END")
            found = pcg_texture_audit.find_atlas_blends(
                atlas, folder, "M_forest_violet_atlas_01",
                source_images=[
                    root / "TCom_Leaves_ForestViolet01_512_albedo.tif"],
            )
            self.assertEqual(found, [str(blend)])

    def test_source_named_tcom_blend_is_not_a_completed_canonical_atlas(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            atlas = root / "atlas"
            folder = root / "tree_chestnut"
            atlas.mkdir()
            folder.mkdir()
            wrong = atlas / "M_TCom_Leaves_SweetChestnut01_atlas_01.blend"
            wrong.write_bytes(
                b"BLENDER\x00TCom_Leaves_SweetChestnut01_4K_albedo.tif\x00END")
            found = pcg_texture_audit.find_atlas_blends(
                atlas, folder, "M_leaf_chestnut_atlas_01",
                source_images=[root / "TCom_Leaves_SweetChestnut01_4K_albedo.tif"],
            )
            self.assertEqual(found, [])

    def test_exact_canonical_atlas_name_wins_over_sibling_index(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            atlas = root / "atlas"
            folder = root / "tree_chestnut"
            atlas.mkdir()
            folder.mkdir()
            first = atlas / "M_leaf_chestnut_atlas_01.blend"
            second = atlas / "M_leaf_chestnut_atlas_02.blend"
            first.write_bytes(b"BLENDER")
            second.write_bytes(b"BLENDER")
            found = pcg_texture_audit.find_atlas_blends(
                atlas, folder, "M_leaf_chestnut_atlas_01")
            self.assertEqual(found, [str(first)])

    def test_spm_analysis_is_shared_and_invalidated_by_file_signature(self):
        def write_spm(path, material_id, material_name):
            xml = (
                '<?xml version="1.0"?><SpeedTree><Materials>'
                f'<Material_v8 ID="{material_id}" Name="{material_name}">'
                '<TexFilename>leaf_color.tga</TexFilename>'
                '<TexFilename>leaf_opacity.tga</TexFilename>'
                '</Material_v8></Materials><Generators><Generator><Properties>'
                '<Property><Name>Leaves:Type:0:Material</Name>'
                f'<Value>{material_id}</Value></Property>'
                '</Properties></Generator></Generators></SpeedTree>'
            ).encode()
            with gzip.open(path, "wb") as handle:
                handle.write(xml)

        with tempfile.TemporaryDirectory() as temp:
            spm = Path(temp) / "test.spm"
            write_spm(spm, "7", "leaf_test")
            old_memory = pcg_texture_audit._SPM_ANALYSIS_CACHE
            old_persistent = pcg_texture_audit._PERSISTENT_SPM_ANALYSIS
            old_dirty = pcg_texture_audit._PERSISTENT_SPM_ANALYSIS_DIRTY
            pcg_texture_audit._SPM_ANALYSIS_CACHE = {}
            pcg_texture_audit._PERSISTENT_SPM_ANALYSIS = {}
            pcg_texture_audit._PERSISTENT_SPM_ANALYSIS_DIRTY = False
            try:
                with mock.patch.object(
                        pcg_texture_audit, "read_maybe_gzip_text",
                        wraps=pcg_texture_audit.read_maybe_gzip_text) as reader:
                    self.assertEqual(
                        pcg_texture_audit.extract_material_names(spm), ["leaf_test"])
                    self.assertEqual(
                        pcg_texture_audit.active_material_ids(spm), {"7"})
                    self.assertEqual(
                        pcg_texture_audit.extract_material_image_refs(spm)[0]["refs"],
                        ["leaf_color.tga", "leaf_opacity.tga"],
                    )
                    self.assertEqual(reader.call_count, 1)

                    write_spm(spm, "88", "leaf_changed_name")
                    self.assertEqual(
                        pcg_texture_audit.active_material_ids(spm), {"88"})
                    self.assertEqual(reader.call_count, 2)
            finally:
                pcg_texture_audit._SPM_ANALYSIS_CACHE = old_memory
                pcg_texture_audit._PERSISTENT_SPM_ANALYSIS = old_persistent
                pcg_texture_audit._PERSISTENT_SPM_ANALYSIS_DIRTY = old_dirty

    def test_hidden_generators_do_not_count_as_active_materials(self):
        def generator(name, guid, hidden, material_id):
            return (
                f"<Generator Type=\"Leaf Mesh\"><Name>{name}</Name>"
                f"<GUID>{guid}</GUID><Hidden>{hidden}</Hidden><Properties>"
                f"<Property><Name>Leaves:Type:0:Material</Name>"
                f"<Value>{material_id}</Value></Property>"
                "</Properties></Generator>"
            )

        def link(source, target):
            return (
                f"<Link><SourceGUID>{source}</SourceGUID>"
                f"<TargetGUID>{target}</TargetGUID><Hidden>false</Hidden></Link>"
            )

        xml = (
            '<?xml version="1.0"?><SpeedTree><Materials>'
            '<Material_v8 ID="1" Name="M_shown"></Material_v8>'
            '<Material_v8 ID="2" Name="M_eye_off"></Material_v8>'
            '<Material_v8 ID="3" Name="M_under_hidden_parent"></Material_v8>'
            "</Materials><Generators>"
            + generator("Tree", "g-tree", "false", "0")
            + generator("Leaf 1", "g-shown", "false", "1")
            + generator("Leaf 2", "g-hidden", "true", "2")
            + generator("Branch", "g-hidden-parent", "true", "0")
            + generator("Leaf 3", "g-shown-child", "false", "3")
            + "</Generators><Links>"
            + link("g-tree", "g-shown")
            + link("g-tree", "g-hidden")
            + link("g-tree", "g-hidden-parent")
            + link("g-hidden-parent", "g-shown-child")
            + "</Links></SpeedTree>"
        ).encode()
        with tempfile.TemporaryDirectory() as temp:
            spm = Path(temp) / "SK_hidden_test.spm"
            with gzip.open(spm, "wb") as handle:
                handle.write(xml)
            old_memory = pcg_texture_audit._SPM_ANALYSIS_CACHE
            old_persistent = pcg_texture_audit._PERSISTENT_SPM_ANALYSIS
            old_dirty = pcg_texture_audit._PERSISTENT_SPM_ANALYSIS_DIRTY
            pcg_texture_audit._SPM_ANALYSIS_CACHE = {}
            pcg_texture_audit._PERSISTENT_SPM_ANALYSIS = {}
            pcg_texture_audit._PERSISTENT_SPM_ANALYSIS_DIRTY = False
            try:
                self.assertEqual(
                    pcg_texture_audit.active_material_ids(spm),
                    {"0", "1", "2", "3"})
                self.assertEqual(
                    pcg_texture_audit.active_material_names(spm),
                    ["M_shown", "M_eye_off", "M_under_hidden_parent"])
                self.assertEqual(
                    pcg_texture_audit.visible_material_ids(spm), {"0", "1"})
                self.assertEqual(
                    pcg_texture_audit.visible_material_names(spm), ["M_shown"])
            finally:
                pcg_texture_audit._SPM_ANALYSIS_CACHE = old_memory
                pcg_texture_audit._PERSISTENT_SPM_ANALYSIS = old_persistent
                pcg_texture_audit._PERSISTENT_SPM_ANALYSIS_DIRTY = old_dirty

    def _image(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (2, 2), (128, 128, 128)).save(path)
        return str(path)

    def test_source_family_key_removes_map_and_resolution_suffixes(self):
        path = Path(r"D:\Texture\Elm\TCom_Leaves_Elm01_4K_albedo.tif")
        self.assertEqual(sbs_auto.source_family_key(path), "tcom_leaves_elm01")

    def test_ambiguous_cluster_leaf_names_use_asset_context(self):
        sources = [
            {"albedo": r"D:\Texture\SweetChestnut02_albedo.tif", "alpha": "b", "targets": []},
            {"albedo": r"D:\Texture\SweetChestnut01_albedo.tif", "alpha": "a", "targets": []},
        ]
        assign_leaf_atlas_bases(sources, Path(r"D:\Trees\tree_chestnut"))
        self.assertEqual(
            [row["atlas_base"] for row in sorted(sources, key=lambda row: row["albedo"])],
            ["M_leaf_chestnut_atlas_01", "M_leaf_chestnut_atlas_02"],
        )

    def test_cluster_number_becomes_unique_atlas_number(self):
        self.assertEqual(
            atlas_base_from_cluster_stem("branch_birch_paper_02"),
            "M_branch_birch_paper_atlas_02",
        )

    def test_material_and_texture_prefixes_are_separate(self):
        self.assertEqual(texture_base_for_material("M_leaf_test"), "T_leaf_test")
        self.assertEqual(texture_base_for_material("leaf_test"), "T_leaf_test")

    def test_user_collection_suffix_uses_numeric_production_base(self):
        self.assertEqual(
            pcg_texture_audit.derived_material_base(
                "M_Leaf_common_grass_01_Winter_Dry"
            ),
            "M_Leaf_common_grass_01",
        )
        self.assertIsNone(
            pcg_texture_audit.derived_material_base("M_stem_common_01")
        )

        sources = [{
            "albedo": r"D:\Texture\common_grass_color.tif",
            "alpha": r"D:\Texture\common_grass_opacity.tif",
            "targets": [{
                "material_names": [
                    "M_Leaf_common_grass_01_dead",
                    "M_Leaf_common_grass_01_UserCollection",
                ],
            }],
        }]
        assign_leaf_atlas_bases(
            sources, Path(r"D:\Trees\Weed_Common_grass")
        )
        self.assertEqual(sources[0]["atlas_base"], "M_Leaf_common_grass_01")

    def test_user_collection_aliases_share_job_only_for_same_source(self):
        def material(material_id, name, color, alpha):
            return (
                f'<Material_v8 ID="{material_id}" Name="{name}">'
                f'<TexFilename>{color}</TexFilename>'
                f'<TexFilename>{alpha}</TexFilename>'
                '</Material_v8>'
            )

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            texture = root / "texture"
            texture.mkdir()
            color = self._image(root / "source" / "common_grass_color.png")
            alpha = self._image(root / "source" / "common_grass_alpha.png")
            xml = (
                "<SpeedTree><Materials>"
                + material("1", "M_Leaf_common_grass_01_dead", color, alpha)
                + material(
                    "2", "M_Leaf_common_grass_01_UserCollection", color, alpha
                )
                + "</Materials><Generators>"
                  "<Generator><Property><Name>Leaves:Material</Name><Value>1</Value>"
                  "</Property></Generator>"
                  "<Generator><Property><Name>Leaves:Material</Name><Value>2</Value>"
                  "</Property></Generator>"
                  "</Generators></SpeedTree>"
            ).encode()
            with gzip.open(root / "SK_Weed_Common_grass_01.spm", "wb") as handle:
                handle.write(xml)

            items = material_texture_items(
                root,
                {
                    "atlas_root": str(root / "atlas"),
                    "required_export_maps": list(sbs_auto.RENDER_MAPS),
                },
                [texture],
                {},
            )

            self.assertEqual(len(items), 1)
            self.assertEqual(items[0]["atlas_base"], "M_Leaf_common_grass_01")
            self.assertEqual(items[0]["texture_base"], "T_Leaf_common_grass_01")
            self.assertEqual(
                set(items[0]["material_names"]),
                {
                    "M_Leaf_common_grass_01_dead",
                    "M_Leaf_common_grass_01_UserCollection",
                },
            )

    def test_user_collection_aliases_with_different_sources_stay_separate(self):
        def material(material_id, name, color, alpha):
            return (
                f'<Material_v8 ID="{material_id}" Name="{name}">'
                f'<TexFilename>{color}</TexFilename>'
                f'<TexFilename>{alpha}</TexFilename>'
                '</Material_v8>'
            )

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            texture = root / "texture"
            texture.mkdir()
            color_a = self._image(root / "source_a" / "color.png")
            alpha_a = self._image(root / "source_a" / "alpha.png")
            color_b = self._image(root / "source_b" / "color.png")
            alpha_b = self._image(root / "source_b" / "alpha.png")
            xml = (
                "<SpeedTree><Materials>"
                + material(
                    "1", "M_Leaf_common_grass_01_FirstCustom", color_a, alpha_a
                )
                + material(
                    "2", "M_Leaf_common_grass_01_SecondCustom", color_b, alpha_b
                )
                + "</Materials><Generators>"
                  "<Generator><Property><Name>Leaves:Material</Name><Value>1</Value>"
                  "</Property></Generator>"
                  "<Generator><Property><Name>Leaves:Material</Name><Value>2</Value>"
                  "</Property></Generator>"
                  "</Generators></SpeedTree>"
            ).encode()
            with gzip.open(root / "SK_Weed_Common_grass_01.spm", "wb") as handle:
                handle.write(xml)

            items = material_texture_items(
                root,
                {
                    "atlas_root": str(root / "atlas"),
                    "required_export_maps": list(sbs_auto.RENDER_MAPS),
                },
                [texture],
                {},
            )

            self.assertEqual(len(items), 2)
            self.assertEqual(
                {item["atlas_base"] for item in items},
                {
                    "M_Leaf_common_grass_01_FirstCustom",
                    "M_Leaf_common_grass_01_SecondCustom",
                },
            )

    def test_common_bark_end_aliases_share_one_material_name(self):
        self.assertEqual(
            canonical_material_name("M_bark_common_locast_end_01"),
            "M_bark_common_end_01",
        )
        self.assertEqual(
            canonical_material_name("M_bark_common_dogWood_end_01"),
            "M_bark_common_end_01",
        )
        self.assertEqual(
            canonical_material_name("M_stem_common_01_dead"),
            "M_stem_common_01_dead",
        )

    def test_opaque_material_can_render_without_alpha(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            albedo = self._image(root / "Bark_4K_albedo.png")
            inputs, notes = sbs_auto.plan_inputs_from_row(
                {"folder": str(root), "source_albedo": [albedo]},
                require_alpha=False,
            )
            self.assertEqual(inputs["Base_Color"], Path(albedo))
            self.assertNotIn("Opacity", inputs)
            self.assertTrue(any("흰색" in note for note in notes))
            white = Image.open(sbs_auto.neutral_image("white")).convert("RGB")
            self.assertEqual(white.getpixel((0, 0)), (255, 255, 255))

    def test_speedtree_color_slot_is_structural_not_filename_guessing(self):
        color, opacity = material_color_alpha_refs([
            r"cluster\leaf_birch_paper_01.tga",
            r"cluster\leaf_birch_paper_01_Opacity.tga",
            r"cluster\leaf_birch_paper_01_Normal.tga",
        ])
        self.assertEqual(color, [r"cluster\leaf_birch_paper_01.tga"])
        self.assertEqual(opacity, [r"cluster\leaf_birch_paper_01_Opacity.tga"])

    def test_pixel_identical_local_copy_uses_external_original(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            folder = root / "Tree" / "weed_test"
            external = root / "Texture" / "Grass" / "SetA"
            local_albedo = self._image(folder / "textures" / "SetA_4K_albedo.png")
            local_alpha = self._image(folder / "textures" / "SetA_4K_alpha.png")
            external_albedo = self._image(external / "SetA_4K_albedo.tif")
            external_alpha = self._image(external / "SetA_4K_alpha.tif")
            source = {
                "source_family": "SetA",
                "albedo": local_albedo,
                "alpha": local_alpha,
                "source_refs": [local_albedo, local_alpha],
            }
            original = {
                "source_family": "SetA",
                "albedo": external_albedo,
                "alpha": external_alpha,
                "source_refs": [external_albedo, external_alpha],
            }
            canonicalize_leaf_sources(
                [source], [source, original],
                {"source_texture_roots": [str(root / "Texture")]}, folder)
            self.assertEqual(source["albedo"], external_albedo)
            self.assertEqual(source["alpha"], external_alpha)
            self.assertEqual(source["canonicalized_from"], [local_albedo, local_alpha])

    def test_spm_material_refs_are_scoped_to_the_requested_material(self):
        xml = b'''<?xml version="1.0" encoding="utf-8"?>
<SpeedTree><Materials>
  <Material_v8 ID="1" Name="M_leaf_target_green">
    <Textures><TexFilename>textures/target_4K_Albedo.png</TexFilename>
    <TexFilename>textures/target_4K_Opacity.png</TexFilename></Textures>
  </Material_v8>
  <Material_v8 ID="2" Name="M_leaf_other">
    <Textures><TexFilename>textures/other_4K_Albedo.png</TexFilename>
    <TexFilename>textures/other_4K_Opacity.png</TexFilename></Textures>
  </Material_v8>
</Materials></SpeedTree>'''
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "sample.spm"
            with gzip.open(path, "wb") as handle:
                handle.write(xml)
            refs = extract_material_image_refs(path, ["M_leaf_target_green"])
            self.assertEqual(
                refs["m_leaf_target_green"],
                ["textures\\target_4K_Albedo.png", "textures\\target_4K_Opacity.png"],
            )

    def test_texture_ref_parser_does_not_span_speedtree_backslash_tags(self):
        xml = b'''<?xml version="1.0"?><SpeedTree><Materials>
<Material_v8 ID="1" Name="M_test">
<TexFilename>source_color.jpg<\\TexFilename><TexSource>0<\\TexSource>
<TexFilename\\><TexSource>1<\\TexSource>
<TexFilename>preview.png<\\TexFilename>
</Material_v8></Materials></SpeedTree>'''
        with tempfile.TemporaryDirectory() as temp:
            spm = Path(temp) / "test.spm"
            with gzip.open(spm, "wb") as handle:
                handle.write(xml)
            from pcg_texture_audit import extract_material_image_refs as audit_refs
            self.assertEqual(
                audit_refs(spm)[0]["refs"],
                ["source_color.jpg", "preview.png"],
            )

    def test_texture_jobs_only_include_generator_used_materials(self):
        xml = b'''<?xml version="1.0" encoding="utf-8"?>
<SpeedTree><Materials>
  <Material_v8 ID="1" Name="M_used"><TexFilename>used_albedo.png</TexFilename></Material_v8>
  <Material_v8 ID="2" Name="M_unused_test"><TexFilename>unused_albedo.png</TexFilename></Material_v8>
</Materials><Generators><Generator><Properties><Property>
  <Name>Branches:Material</Name><Value>1</Value>
</Property></Properties></Generator></Generators></SpeedTree>'''
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            spm = root / "SK_test.spm"
            with gzip.open(spm, "wb") as handle:
                handle.write(xml)
            items = material_texture_items(
                root,
                {"atlas_root": str(root / "atlas"),
                 "required_export_maps": list(sbs_auto.RENDER_MAPS)},
                [root],
                {},
            )
            self.assertEqual([item["atlas_base"] for item in items], ["M_used"])
            self.assertEqual(items[0]["texture_base"], "T_used")

    def test_active_collection_classifications_share_one_atlas_texture_job(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            texture = root / "texture"
            texture.mkdir()
            atlas_01_color = self._image(root / "source" / "Chestnut01_albedo.png")
            atlas_01_alpha = self._image(root / "source" / "Chestnut01_alpha.png")
            atlas_02_color = self._image(root / "source" / "Chestnut02_albedo.png")
            atlas_02_alpha = self._image(root / "source" / "Chestnut02_alpha.png")

            def material(material_id, name, color, alpha):
                return (
                    f'<Material_v8 ID="{material_id}" Name="{name}">'
                    f'<TexFilename>{color}</TexFilename>'
                    f'<TexFilename>{alpha}</TexFilename>'
                    '</Material_v8>'
                )

            xml = (
                '<SpeedTree><Materials>'
                + material("1", "M_leaf_chestnut_atlas_01_green_light",
                           atlas_01_color, atlas_01_alpha)
                + material("2", "M_leaf_chestnut_atlas_01_yellow",
                           atlas_01_color, atlas_01_alpha)
                + material("3", "M_leaf_chestnut_atlas_02_unused",
                           atlas_02_color, atlas_02_alpha)
                + '</Materials><Generators>'
                  '<Generator><Property><Name>Leaves:Material</Name><Value>1</Value></Property></Generator>'
                  '<Generator><Property><Name>Leaves:Material</Name><Value>2</Value></Property></Generator>'
                  '</Generators></SpeedTree>'
            ).encode()
            spm = root / "SK_tree_chestnut_01.spm"
            with gzip.open(spm, "wb") as handle:
                handle.write(xml)
            leaf_sources = [
                {
                    "atlas_base": "M_leaf_chestnut_atlas_01",
                    "albedo": atlas_01_color, "alpha": atlas_01_alpha,
                    "source_refs": [atlas_01_color, atlas_01_alpha],
                    "atlas_blends": [str(root / "atlas" / "M_leaf_chestnut_atlas_01.blend")],
                },
                {
                    "atlas_base": "M_leaf_chestnut_atlas_02",
                    "albedo": atlas_02_color, "alpha": atlas_02_alpha,
                    "source_refs": [atlas_02_color, atlas_02_alpha],
                    "atlas_blends": [str(root / "atlas" / "M_leaf_chestnut_atlas_02.blend")],
                },
            ]
            items = material_texture_items(
                root,
                {"atlas_root": str(root / "atlas"),
                 "required_export_maps": list(sbs_auto.RENDER_MAPS)},
                [texture], {}, leaf_mesh_sources=leaf_sources,
            )

            self.assertEqual(len(items), 1)
            self.assertEqual(items[0]["atlas_base"], "M_leaf_chestnut_atlas_01")
            self.assertEqual(items[0]["texture_base"], "T_leaf_chestnut_atlas_01")
            self.assertEqual(
                set(items[0]["material_names"]),
                {"M_leaf_chestnut_atlas_01_green_light",
                 "M_leaf_chestnut_atlas_01_yellow"},
            )
            self.assertNotIn("M_leaf_chestnut_atlas_02_unused",
                             items[0]["material_names"])
            self.assertEqual(
                {target["material_id"] for target in items[0]["material_targets"]},
                {"1", "2"},
            )
            spm_jobs = jobs_from_texture_plan({"items": items})
            self.assertEqual(len(spm_jobs), 1)
            self.assertEqual(
                set(spm_jobs[0]["materials"]),
                {"@id:1", "@id:2"},
            )
            self.assertEqual(
                {row["texture_base"] for row in spm_jobs[0]["materials"].values()},
                {"T_leaf_chestnut_atlas_01"},
            )

    def test_managed_outputs_recover_the_same_active_atlas_group(self):
        xml = b'''<SpeedTree><Materials>
<Material_v8 ID="1" Name="M_leaf_chestnut_atlas_01_green_light">
<TexFilename>texture/T_leaf_chestnut_atlas_01_color.tga</TexFilename>
<TexFilename>texture/T_leaf_chestnut_atlas_01_opacity.tga</TexFilename>
<TexFilename>texture/T_leaf_chestnut_atlas_01_normal.tga</TexFilename>
<TexFilename>texture/T_leaf_chestnut_atlas_01_extra.tga</TexFilename>
<TexFilename>texture/T_leaf_chestnut_atlas_01_height.tga</TexFilename>
<TexFilename>texture/T_leaf_chestnut_atlas_01_subsurface.tga</TexFilename>
</Material_v8></Materials><Generator><Property>
<Name>Leaves:Material</Name><Value>1</Value>
</Property></Generator></SpeedTree>'''
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            texture = root / "texture"
            texture.mkdir()
            spm = root / "SK_tree_chestnut_01.spm"
            with gzip.open(spm, "wb") as handle:
                handle.write(xml)
            source_color = self._image(root / "source" / "Chestnut01_albedo.png")
            source_alpha = self._image(root / "source" / "Chestnut01_alpha.png")
            source_normal = self._image(root / "source" / "Chestnut01_normal.png")
            source_roughness = self._image(root / "source" / "Chestnut01_roughness.png")
            source_subsurface = self._image(root / "source" / "Chestnut01_translucency.png")
            leaf_source = {
                "atlas_base": "M_leaf_chestnut_atlas_01",
                "albedo": source_color, "alpha": source_alpha,
                "source_refs": [source_color, source_alpha, source_normal,
                                source_roughness, source_subsurface],
                "atlas_blends": [],
            }
            items = material_texture_items(
                root,
                {"atlas_root": str(root / "atlas"),
                 "required_export_maps": list(sbs_auto.RENDER_MAPS)},
                [texture], {},
                leaf_mesh_sources=[leaf_source],
            )
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0]["atlas_base"], "M_leaf_chestnut_atlas_01")
            self.assertEqual(items[0]["texture_base"], "T_leaf_chestnut_atlas_01")
            self.assertEqual(
                items[0]["material_names"],
                ["M_leaf_chestnut_atlas_01_green_light"],
            )
            plan = build_texture_plan_from_report({
                "config": {},
                "items": [{
                    "name": "tree_chestnut", "folder": str(root),
                    "chosen_spm": str(spm), "sbs_files": [],
                    "cluster_items": items, "leaf_mesh_sources": [leaf_source],
                    "normal_convention": "OpenGL",
                }],
            })
            row = plan["items"][0]
            self.assertTrue(row["canonical_source_provenance"])
            self.assertEqual(row["source_scope"], "leaf_atlas_source")
            self.assertEqual(row["source_normal"], [source_normal])
            self.assertEqual(row["source_roughness"], [source_roughness])
            self.assertEqual(row["source_subsurface"], [source_subsurface])

    def test_managed_aliases_use_complete_texture_set_referenced_outside_asset(self):
        def material(material_id, name, refs):
            filenames = "".join(
                f"<TexFilename>{ref}</TexFilename>" for ref in refs
            )
            return (
                f'<Material_v8 ID="{material_id}" Name="{name}">'
                f"{filenames}</Material_v8>"
            )

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            asset = root / "Weed_Common_grass"
            local_texture = asset / "texture" / "substance"
            shared_texture = root / "weed_velvet_grass" / "texture"
            local_texture.mkdir(parents=True)
            shared_texture.mkdir(parents=True)
            texture_base = "T_Leaf_Grass_atlas_01"
            for role in sbs_auto.RENDER_MAPS:
                (shared_texture / f"{texture_base}_{role}.tga").write_bytes(
                    role.encode("ascii")
                )
            # SpeedTree 10.1 may omit opacity/subsurface in the exported
            # material references. The referenced directory must still prove
            # that the complete six-map set exists.
            current_refs = [
                os.path.relpath(
                    shared_texture / f"{texture_base}_{role}.tga", asset
                )
                for role in ("color", "normal", "extra", "height")
            ]
            xml = (
                "<SpeedTree><Materials>"
                + material("1", "M_Leaf_Grass_atlas_01_green", current_refs)
                + material("2", "M_Leaf_Grass_atlas_01_dead", current_refs)
                + "</Materials><Generators>"
                  "<Generator><Property><Name>Leaves:Material</Name><Value>1</Value>"
                  "</Property></Generator>"
                  "<Generator><Property><Name>Leaves:Material</Name><Value>2</Value>"
                  "</Property></Generator>"
                  "</Generators></SpeedTree>"
            ).encode()
            with gzip.open(asset / "SK_Weed_Common_grass_c_01.spm", "wb") as handle:
                handle.write(xml)

            items = material_texture_items(
                asset,
                {
                    "atlas_root": str(asset / "atlas"),
                    "required_export_maps": list(sbs_auto.RENDER_MAPS),
                },
                [local_texture],
                {},
            )

            self.assertEqual(len(items), 1)
            row = items[0]
            self.assertEqual(row["atlas_base"], "M_Leaf_Grass_atlas_01")
            self.assertEqual(row["texture_base"], texture_base)
            self.assertEqual(
                set(row["material_names"]),
                {
                    "M_Leaf_Grass_atlas_01_green",
                    "M_Leaf_Grass_atlas_01_dead",
                },
            )
            self.assertEqual(row["missing_export_maps"], [])
            self.assertEqual(Path(row["texture_dir"]), shared_texture.resolve())
            self.assertTrue(
                all(
                    Path(path).parent == shared_texture.resolve()
                    for path in row["export_maps"].values()
                )
            )

    def test_visible_auto_split_aliases_share_one_source_texture_set(self):
        def material(material_id, name, refs):
            filenames = "".join(
                f"<TexFilename>{ref}</TexFilename>" for ref in refs)
            return (
                f'<Material_v8 ID="{material_id}" Name="{name}">'
                f"{filenames}</Material_v8>"
            )

        def generator(guid, hidden, material_id):
            return (
                f'<Generator Type="Leaf Mesh"><GUID>{guid}</GUID>'
                f'<Hidden>{str(hidden).lower()}</Hidden><Properties><Property>'
                f'<Name>Leaves:Material</Name><Value>{material_id}</Value>'
                f'</Property></Properties></Generator>'
            )

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            texture = root / "texture"
            atlas = root / "atlas"
            texture.mkdir()
            atlas.mkdir()
            (atlas / "M_cluster_fern_Atlas_01.blend").write_bytes(b"BLENDER")
            source_refs = [
                self._image(root / "source" / f"fern_{role}.png")
                for role in ("albedo", "opacity", "normal", "roughness", "height", "translucency")
            ]
            source_xml = (
                "<SpeedTree><Materials>"
                + material("1", "M_cluster_fern_Atlas_01_green", source_refs)
                + material("2", "M_cluster_fern_Atlas_01_stem", source_refs)
                + material("3", "M_cluster_fern_Atlas_01_yellow", source_refs)
                + "</Materials></SpeedTree>"
            ).encode()
            managed = lambda suffix: [
                f"texture/T_cluster_fern_Atlas_01_{suffix}_{role}.tga"
                for role in sbs_auto.RENDER_MAPS
            ]
            sk_xml = (
                "<SpeedTree><Materials>"
                + material("1", "M_cluster_fern_Atlas_01_green", managed("green"))
                + material("2", "M_cluster_fern_Atlas_01_stem", managed("stem"))
                + material("3", "M_cluster_fern_Atlas_01_yellow", managed("yellow"))
                + "</Materials><Generators>"
                + generator("visible-green", False, "1")
                + generator("visible-stem", False, "2")
                + generator("hidden-yellow", True, "3")
                + "</Generators></SpeedTree>"
            ).encode()
            for path, payload in (
                    (root / "SM_weed_fern.spm", source_xml),
                    (root / "SK_weed_fern.spm", sk_xml)):
                with gzip.open(path, "wb") as handle:
                    handle.write(payload)

            items = material_texture_items(
                root,
                {"atlas_root": str(atlas),
                 "required_export_maps": list(sbs_auto.RENDER_MAPS)},
                [texture], {},
            )

            self.assertEqual(len(items), 1)
            row = items[0]
            self.assertEqual(row["atlas_base"], "M_cluster_fern_Atlas_01")
            self.assertEqual(row["texture_base"], "T_cluster_fern_Atlas_01")
            self.assertEqual(
                set(row["material_names"]),
                {"M_cluster_fern_Atlas_01_green",
                 "M_cluster_fern_Atlas_01_stem"},
            )
            self.assertNotIn("M_cluster_fern_Atlas_01_yellow",
                             row["material_names"])
            self.assertEqual(set(row["source_refs"]), set(source_refs))
            self.assertTrue(row["connection_update_needed"])
            self.assertEqual(set(row["connection_materials"]),
                             set(row["material_names"]))

    def test_auto_split_names_with_different_sources_do_not_merge(self):
        def material(material_id, name, prefix):
            return (
                f'<Material_v8 ID="{material_id}" Name="{name}">'
                f'<TexFilename>{prefix}_albedo.png</TexFilename>'
                f'<TexFilename>{prefix}_opacity.png</TexFilename>'
                f'</Material_v8>'
            )

        xml = (
            "<SpeedTree><Materials>"
            + material("1", "M_leaf_test_atlas_01_green", "source/green")
            + material("2", "M_leaf_test_atlas_01_yellow", "source/yellow")
            + "</Materials><Generators>"
              "<Generator><Property><Name>Leaves:Material</Name><Value>1</Value></Property></Generator>"
              "<Generator><Property><Name>Leaves:Material</Name><Value>2</Value></Property></Generator>"
              "</Generators></SpeedTree>"
        ).encode()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "texture").mkdir()
            with gzip.open(root / "SK_test.spm", "wb") as handle:
                handle.write(xml)
            items = material_texture_items(
                root,
                {"atlas_root": str(root / "atlas"),
                 "required_export_maps": list(sbs_auto.RENDER_MAPS)},
                [root / "texture"], {},
            )
            self.assertEqual(len(items), 2)
            self.assertEqual(
                {item["atlas_base"] for item in items},
                {"M_leaf_test_atlas_01_green", "M_leaf_test_atlas_01_yellow"},
            )
            self.assertEqual(len({tuple(item["source_signature"]) for item in items}), 2)

    def test_auto_split_managed_alias_recovers_suffix_free_leaf_source(self):
        xml = b'''<SpeedTree><Materials>
<Material_v8 ID="1" Name="M_leaf_nothofagus_atlas_01_green">
<TexFilename>texture/T_leaf_nothofagus_atlas_01_green_color.tga</TexFilename>
<TexFilename>texture/T_leaf_nothofagus_atlas_01_green_opacity.tga</TexFilename>
<TexFilename>texture/T_leaf_nothofagus_atlas_01_green_normal.tga</TexFilename>
<TexFilename>texture/T_leaf_nothofagus_atlas_01_green_extra.tga</TexFilename>
<TexFilename>texture/T_leaf_nothofagus_atlas_01_green_height.tga</TexFilename>
<TexFilename>texture/T_leaf_nothofagus_atlas_01_green_subsurface.tga</TexFilename>
</Material_v8></Materials><Generator><Property>
<Name>Leaves:Material</Name><Value>1</Value>
</Property></Generator></SpeedTree>'''
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            texture = root / "texture"
            texture.mkdir()
            with gzip.open(root / "SK_tree_nothofagus.spm", "wb") as handle:
                handle.write(xml)
            source_refs = [
                self._image(root / "source" / f"nothofagus_{role}.png")
                for role in ("albedo", "opacity", "normal", "roughness", "height", "translucency")
            ]
            leaf_source = {
                "atlas_base": "M_leaf_nothofagus_atlas_01",
                "albedo": source_refs[0],
                "alpha": source_refs[1],
                "source_refs": source_refs,
                "atlas_blends": [],
            }
            items = material_texture_items(
                root,
                {"atlas_root": str(root / "atlas"),
                 "required_export_maps": list(sbs_auto.RENDER_MAPS)},
                [texture], {}, leaf_mesh_sources=[leaf_source],
            )
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0]["atlas_base"],
                             "M_leaf_nothofagus_atlas_01")
            self.assertEqual(items[0]["source_refs"], source_refs)
            self.assertTrue(items[0]["leaf_source_provenance"])

    def test_direct_neutral_subsurface_requires_source_repair(self):
        with tempfile.TemporaryDirectory() as temp:
            desired = Path(temp) / "leaf_translucency.png"
            Image.new("RGB", (2, 2), (80, 120, 30)).save(desired)
            job = {
                "mode": "direct",
                "sbs": str(Path(temp) / "test.sbs"),
                "graph": "T_leaf_test",
                "row": {"canonical_source_provenance": True},
            }
            with mock.patch.object(
                    sbs_auto, "parse_m_graph", return_value={
                        "inputs": {"Subsurface": sbs_auto.neutral_image("white")},
                    }), mock.patch.object(
                        sbs_auto, "plan_inputs_from_row",
                        return_value=({"Subsurface": desired}, [])):
                self.assertTrue(
                    migrate_current_sk_textures.job_needs_source_repair(job))

            with mock.patch.object(
                    sbs_auto, "parse_m_graph", return_value={
                        "inputs": {"Subsurface": desired},
                    }), mock.patch.object(
                        sbs_auto, "plan_inputs_from_row",
                        return_value=({"Subsurface": desired}, [])):
                self.assertFalse(
                    migrate_current_sk_textures.job_needs_source_repair(job))

    def test_failed_direct_render_rolls_back_subsurface_repair(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            sbs = root / "test.sbs"
            backup = root / "before_subsurface_patch.sbs"
            desired = root / "leaf_translucency.png"
            sbs.write_bytes(b"original")
            Image.new("RGB", (2, 2), (80, 120, 30)).save(desired)
            job = {
                "base": "M_leaf_test",
                "texture_base": "T_leaf_test",
                "mode": "direct",
                "graph": "T_leaf_test",
                "sbs": str(sbs),
                "out_dir": str(root),
                "normal_opengl": True,
                "direct_maps": sbs_auto.RENDER_MAPS,
                "size_log2": (1, 1),
                "row": {
                    "canonical_source_provenance": True,
                    "legacy_export_maps": {},
                },
            }

            def patch_subsurface(*_args, **_kwargs):
                shutil.copy2(sbs, backup)
                sbs.write_bytes(b"patched")
                return {"backup": str(backup), "resource": "isolated"}

            with mock.patch.object(
                    sbs_auto, "parse_m_graph", return_value={
                        "inputs": {"Subsurface": sbs_auto.neutral_image("white")},
                    }), mock.patch.object(
                        sbs_auto, "plan_inputs_from_row",
                        return_value=({"Subsurface": desired}, [])), \
                    mock.patch.object(
                        sbs_auto, "patch_m_graph_input_resource",
                        side_effect=patch_subsurface) as patcher, \
                    mock.patch.object(
                        sbs_auto, "set_managed_graph_resolution", return_value={
                            "changed": False, "backup": None, "size_log2": (1, 1),
                        }), mock.patch.object(
                            sbs_auto, "render_sbs_graph_maps",
                            side_effect=RuntimeError("render failed")):
                with self.assertRaisesRegex(RuntimeError, "render failed"):
                    migrate_current_sk_textures.run_job(job, {}, timeout=10)

            patcher.assert_called_once_with(
                str(sbs), "T_leaf_test", "Subsurface", desired)
            self.assertEqual(sbs.read_bytes(), b"original")

    def test_direct_graph_fills_every_missing_standard_output(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            out = root / "texture"
            out.mkdir()
            job = {
                "base": "M_test",
                "texture_base": "T_test",
                "mode": "direct",
                "graph": "T_test",
                "sbs": str(root / "test.sbs"),
                "out_dir": str(out),
                "normal_opengl": True,
                "direct_maps": ("color", "normal", "extra", "height"),
                "size_log2": (1, 1),
                "row": {
                    "texture_dir": str(out),
                    "texture_base": "T_test",
                    "legacy_export_maps": {},
                },
            }

            def write_maps(texture_base, output_dir, maps):
                paths = []
                for role in maps:
                    path = Path(output_dir) / f"{texture_base}_{role}.tga"
                    Image.new("RGB", (2, 2), (128, 128, 128)).save(path)
                    paths.append(path)
                return paths

            def render_direct(_sbs, _graph, texture_base, output_dir, **kwargs):
                files = write_maps(texture_base, output_dir, kwargs["maps"])
                return {
                    "files": files, "size_log2": (1, 1),
                    "pixel_size": (2, 2), "backup_dir": None,
                    "changed_files": files, "unchanged_files": [],
                    "created_files": files,
                }

            def render_fallback(texture_base, _inputs, _params, output_dir, **kwargs):
                files = write_maps(texture_base, output_dir, kwargs["maps"])
                return {
                    "files": files, "backup_dir": None,
                    "changed_files": files, "unchanged_files": [],
                    "created_files": files,
                }

            with mock.patch.object(
                    sbs_auto, "render_sbs_graph_maps", side_effect=render_direct), \
                    mock.patch.object(
                        sbs_auto, "render_maps", side_effect=render_fallback) as fallback, \
                    mock.patch.object(
                        sbs_auto, "plan_inputs_from_row", return_value=({}, [])), \
                    mock.patch.object(
                        sbs_auto, "set_managed_graph_resolution", return_value={
                            "changed": False, "backup": None, "size_log2": (1, 1),
                        }), \
                    mock.patch.object(
                        sbs_auto, "delete_legacy_m_outputs", return_value=[]):
                result = migrate_current_sk_textures.run_job(job, {}, timeout=10)

            self.assertEqual(
                fallback.call_args.kwargs["maps"], ("opacity", "subsurface"))
            self.assertTrue(fallback.call_args.kwargs["return_info"])
            self.assertEqual(len(result["files"]), len(sbs_auto.RENDER_MAPS))
            self.assertEqual(len(result["changed_files"]), len(sbs_auto.RENDER_MAPS))
            self.assertEqual(len(result["created_files"]), len(sbs_auto.RENDER_MAPS))
            self.assertEqual(result["unchanged_files"], [])
            self.assertTrue(all(Path(path).is_file() for path in result["files"]))

    def test_active_generic_material_with_existing_managed_graph_is_included(self):
        xml = b'''<SpeedTree><Materials>
<Material_v8 ID="1" Name="M_Material 2">
<TexFilename>texture/T_Material 2_color.tga</TexFilename>
<TexFilename>texture/T_Material 2_opacity.tga</TexFilename>
<TexFilename>texture/T_Material 2_normal.tga</TexFilename>
<TexFilename>texture/T_Material 2_extra.tga</TexFilename>
<TexFilename>texture/T_Material 2_height.tga</TexFilename>
</Material_v8></Materials><Generator><Property>
<Name>Leaves:Material</Name><Value>1</Value>
</Property></Generator></SpeedTree>'''
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            texture = root / "texture"
            texture.mkdir()
            spm = root / "SK_test.spm"
            with gzip.open(spm, "wb") as handle:
                handle.write(xml)
            items = material_texture_items(
                root,
                {"atlas_root": str(root / "atlas"),
                 "required_export_maps": list(sbs_auto.RENDER_MAPS)},
                [texture],
                {"t_material 2": ("T_Material 2", str(texture / "test.sbs"))},
            )
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0]["atlas_base"], "M_Material 2")
            self.assertEqual(items[0]["texture_base"], "T_Material 2")

    def test_active_generic_source_material_with_managed_graph_is_included(self):
        def xml(refs):
            filenames = "".join(
                f"<TexFilename>{ref}</TexFilename>" for ref in refs)
            return f'''<SpeedTree><Materials>
<Material_v8 ID="2" Name="M_Material 2">{filenames}</Material_v8>
</Materials><Generator><Property>
<Name>Leaves:Material</Name><Value>2</Value>
</Property></Generator></SpeedTree>'''.encode()

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            texture = root / "texture"
            texture.mkdir()
            source_refs = [
                "source/leaf_Albedo.jpg", "source/leaf_Opacity.jpg",
                "source/leaf_Normal.jpg", "source/leaf_Roughness.jpg",
            ]
            managed_refs = [
                f"texture/T_Material 2_{map_name}.tga"
                for map_name in sbs_auto.RENDER_MAPS
            ]
            for path, payload in (
                    (root / "SM_bush_test.spm", xml(source_refs)),
                    (root / "SK_bush_test.spm", xml(managed_refs)),
                    (root / "SK_weed_test.spm", xml(source_refs))):
                with gzip.open(path, "wb") as handle:
                    handle.write(payload)
            leaf_source = {
                "atlas_base": "M_leaf_test_atlas_01",
                "albedo": root / source_refs[0],
                "alpha": root / source_refs[1],
                "source_refs": [root / ref for ref in source_refs],
                "atlas_blends": [],
            }
            items = material_texture_items(
                root,
                {"atlas_root": str(root / "atlas"),
                 "required_export_maps": list(sbs_auto.RENDER_MAPS)},
                [texture],
                {"t_material 2": ("T_Material 2", str(texture / "test.sbs"))},
                leaf_mesh_sources=[leaf_source],
            )
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0]["atlas_base"], "M_Material 2")
            self.assertEqual(items[0]["texture_base"], "T_Material 2")
            self.assertEqual(items[0]["material_spms"], [
                str(root / "SK_bush_test.spm"),
                str(root / "SK_weed_test.spm"),
            ])
            self.assertEqual(
                items[0]["source_refs"], [str(root / ref) for ref in source_refs])
            self.assertTrue(items[0]["connection_update_needed"])

    def test_sk_managed_outputs_fall_back_to_same_material_in_original_spm(self):
        def xml(name, refs):
            filenames = "".join(f"<TexFilename>{ref}</TexFilename>" for ref in refs)
            return f'''<SpeedTree><Materials><Material_v8 ID="1" Name="{name}">
{filenames}</Material_v8></Materials><Generator><Property>
<Name>Branches:Material</Name><Value>1</Value></Property></Generator></SpeedTree>'''.encode()

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with gzip.open(root / "tree_test.spm", "wb") as handle:
                handle.write(xml("leaf_test", [
                    "source/leaf_Albedo.png", "source/leaf_Opacity.png",
                    "source/leaf_Translucency.png",
                ]))
            with gzip.open(root / "SK_tree_test.spm", "wb") as handle:
                handle.write(xml("M_leaf_test", [
                    "texture/M_leaf_test_color.tga", "texture/M_leaf_test_opacity.tga",
                    "texture/M_leaf_test_normal.tga", "texture/M_leaf_test_extra.tga",
                    "texture/M_leaf_test_height.tga",
                ]))
            items = material_texture_items(
                root,
                {"atlas_root": str(root / "atlas"),
                 "required_export_maps": list(sbs_auto.RENDER_MAPS)},
                [root / "texture"],
                {},
            )
            self.assertEqual(items[0]["source_refs"], [
                r"source\leaf_Albedo.png", r"source\leaf_Opacity.png",
                r"source\leaf_Translucency.png",
            ])

    def test_mesh_asset_spm_without_material_properties_uses_all_materials(self):
        xml = b'''<SpeedTree><Materials>
<Material_v8 ID="1" Name="M_leaf_green"><TexFilename>green_albedo.png</TexFilename></Material_v8>
<Material_v8 ID="2" Name="M_leaf_dead"><TexFilename>dead_albedo.png</TexFilename></Material_v8>
</Materials><Generators><Generator><Properties /></Generator></Generators></SpeedTree>'''
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "texture").mkdir()
            with gzip.open(root / "SK_mesh_asset.spm", "wb") as handle:
                handle.write(xml)
            items = material_texture_items(
                root,
                {"atlas_root": str(root / "atlas"),
                 "required_export_maps": list(sbs_auto.RENDER_MAPS)},
                [root / "texture"],
                {},
            )
            self.assertEqual(
                {item["atlas_base"] for item in items},
                {"M_leaf_green", "M_leaf_dead"},
            )

    def test_spm_normalization_uses_slot_channel_selection_without_repacking(self):
        map_template = '''<Map Name="{name}">
<ColorX>1</ColorX><ColorY>1</ColorY><ColorZ>1</ColorZ>
<TexFilename>old_{name}.png</TexFilename><TexSource>0</TexSource>
<TexBrightness>0</TexBrightness><TexContrast>0</TexContrast><TexSaturation>0</TexSaturation>
<TexRed>0</TexRed><TexGreen>0</TexGreen><TexBlue>0</TexBlue>
<TexInvert>false</TexInvert><TexInvertRed>false</TexInvertRed>
<TexInvertGreen>false</TexInvertGreen><TexInvertBlue>false</TexInvertBlue>
<TexEnabled>true</TexEnabled><Srgb>false</Srgb></Map>'''
        maps = "".join(map_template.format(name=name) for name in ("Color", "Opacity", "Normal", "Custom"))
        maps += map_template.format(name="AO").replace(
            "<TexFilename>old_AO.png</TexFilename>", "<TexFilename />")
        xml = f'''<?xml version="1.0"?><SpeedTree><Materials>
<Material_v8 ID="1" Name="bark_test">{maps}</Material_v8>
<Material_v8 ID="2" Name="unused_test">{maps}</Material_v8>
</Materials><Generators><Generator><Properties><Property>
<Name>Branches:Material</Name><Value>1</Value>
</Property></Properties></Generator></Generators></SpeedTree>'''.encode()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            spm = root / "SK_test.spm"
            texture_dir = root / "texture"
            texture_dir.mkdir()
            for role in ("color", "normal", "extra", "height", "opacity", "subsurface"):
                (texture_dir / f"T_bark_test_{role}.tga").write_bytes(role.encode())
            with gzip.open(spm, "wb") as handle:
                handle.write(xml)
            patch = build_spm_patch(spm, {
                "m_bark_test": {
                    "texture_dir": str(texture_dir),
                    "texture_base": "T_bark_test",
                    "subsurface_enabled": False,
                }
            })
            inspected = inspect_material_slots(patch["text"])["1"]
            self.assertEqual(inspected["name"], "M_bark_test")
            self.assertEqual(list(inspected["slots"]), [spec[0].lower() for spec in SLOT_SPECS])
            self.assertEqual(inspected["slots"]["ao"]["filename"], "texture/T_bark_test_extra.tga")
            self.assertEqual(inspected["slots"]["ao"]["source"], "1")
            self.assertEqual(inspected["slots"]["gloss"]["filename"], "texture/T_bark_test_extra.tga")
            self.assertEqual(inspected["slots"]["gloss"]["source"], "2")
            self.assertEqual(inspected["slots"]["gloss"]["invert"], "true")
            self.assertEqual(inspected["slots"]["opacity"]["enabled"], "false")
            self.assertEqual(inspected["slots"]["opacity"]["color_x"], "1")
            self.assertEqual(inspected["slots"]["subsurfacecolor"]["enabled"], "false")
            self.assertEqual(
                [inspected["slots"]["subsurfacecolor"][field]
                 for field in ("color_x", "color_y", "color_z")],
                ["0", "0", "0"],
            )
            self.assertEqual(inspected["slots"]["subsurfaceamount"]["enabled"], "false")
            self.assertEqual(inspected["slots"]["subsurfaceamount"]["color_x"], "0")
            self.assertEqual(inspected["slots"]["height"]["filename"], "texture/T_bark_test_height.tga")
            self.assertNotIn("T_bark_test", inspect_material_slots(patch["text"])["2"]["slots"]["color"]["filename"])

    def test_spm_normalization_writes_backup_and_preserves_six_images(self):
        map_template = '''<Map Name="{name}"><ColorX>1</ColorX><ColorY>1</ColorY><ColorZ>1</ColorZ>
<TexFilename>old.png</TexFilename>
<TexSource>0</TexSource><TexInvert>false</TexInvert><TexInvertRed>false</TexInvertRed>
<TexInvertGreen>false</TexInvertGreen><TexInvertBlue>false</TexInvertBlue>
<TexEnabled>true</TexEnabled></Map>'''
        maps = "".join(map_template.format(name=name) for name in ("Color", "Opacity", "Normal", "Custom"))
        xml = f'''<SpeedTree><Materials><Material_v8 ID="1" Name="M_test">{maps}</Material_v8>
</Materials><Generator><Property><Name>Branches:Frequency</Name><Value>1</Value></Property></Generator></SpeedTree>'''.encode()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            spm = root / "SK_test.spm"
            out = root / "texture"
            out.mkdir()
            original_bytes = {}
            for role in ("color", "normal", "extra", "height", "opacity", "subsurface"):
                path = out / f"T_test_{role}.tga"
                path.write_bytes(f"original-{role}".encode())
                original_bytes[path] = path.read_bytes()
            with gzip.open(spm, "wb") as handle:
                handle.write(xml)
            result = normalize_spms_transactionally([{
                "spm": str(spm),
                "materials": {"m_test": {
                    "texture_dir": str(out),
                    "texture_base": "T_test",
                    "subsurface_enabled": True,
                }},
            }], backup_root=root / "backups")
            self.assertEqual(result["materials"], 1)
            self.assertTrue(Path(result["backup_dir"]).is_dir())
            self.assertEqual({path: path.read_bytes() for path in original_bytes}, original_bytes)
            slots = inspect_material_slots(gzip.open(spm, "rt", encoding="utf-8").read())["1"]["slots"]
            self.assertFalse("custom" in slots)
            self.assertEqual(slots["opacity"]["enabled"], "false")
            self.assertEqual(slots["subsurfacecolor"]["enabled"], "true")
            self.assertEqual(slots["subsurfaceamount"]["enabled"], "false")
            self.assertEqual(slots["subsurfaceamount"]["color_x"], "1")

            # Running the same normalization again must be a true no-op. In
            # particular, do not recompress the SPM: gzip header/mtime churn
            # would invalidate Repair and force an otherwise redundant Push.
            before_bytes = spm.read_bytes()
            before_mtime = spm.stat().st_mtime_ns
            backup_entries = sorted(
                str(path.relative_to(root))
                for path in (root / "backups").rglob("*"))
            second = normalize_spms_transactionally([{
                "spm": str(spm),
                "materials": {"m_test": {
                    "texture_dir": str(out),
                    "texture_base": "T_test",
                    "subsurface_enabled": True,
                }},
            }], backup_root=root / "backups")
            self.assertEqual(second["spms"], [])
            self.assertEqual(second["materials"], 0)
            self.assertIsNone(second["backup_dir"])
            self.assertEqual(second["unchanged_spms"], [str(spm)])
            self.assertEqual(spm.read_bytes(), before_bytes)
            self.assertEqual(spm.stat().st_mtime_ns, before_mtime)
            self.assertEqual(
                sorted(str(path.relative_to(root))
                       for path in (root / "backups").rglob("*")),
                backup_entries,
            )

    def test_spm_normalization_skips_unbuildable_spm_and_commits_the_rest(self):
        map_template = '''<Map Name="{name}"><ColorX>1</ColorX><ColorY>1</ColorY><ColorZ>1</ColorZ>
<TexFilename>old.png</TexFilename>
<TexSource>0</TexSource><TexInvert>false</TexInvert><TexInvertRed>false</TexInvertRed>
<TexInvertGreen>false</TexInvertGreen><TexInvertBlue>false</TexInvertBlue>
<TexEnabled>true</TexEnabled></Map>'''
        maps = "".join(map_template.format(name=name) for name in ("Color", "Opacity", "Normal"))
        xml = f'''<SpeedTree><Materials><Material_v8 ID="1" Name="M_test">{maps}</Material_v8>
</Materials><Generator><Property><Name>Branches:Frequency</Name><Value>1</Value></Property></Generator></SpeedTree>'''.encode()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            out = root / "texture"
            out.mkdir()
            for role in ("color", "normal", "extra", "height", "opacity", "subsurface"):
                (out / f"T_test_{role}.tga").write_bytes(role.encode())
            good = root / "SK_good.spm"
            legacy = root / "SK_legacy.spm"
            for spm in (good, legacy):
                with gzip.open(spm, "wb") as handle:
                    handle.write(xml)
            jobs = [
                {"spm": str(legacy), "materials": {"m_test": {
                    # No outputs on disk for this base -> patch cannot be built.
                    "texture_dir": str(out),
                    "texture_base": "T_missing",
                    "subsurface_enabled": False,
                }}},
                {"spm": str(good), "materials": {"m_test": {
                    "texture_dir": str(out),
                    "texture_base": "T_test",
                    "subsurface_enabled": False,
                }}},
            ]
            with self.assertRaises(RuntimeError):
                normalize_spms_transactionally(jobs, backup_root=root / "backups")
            result = normalize_spms_transactionally(
                jobs, backup_root=root / "backups", skip_unbuildable=True)
            self.assertEqual(result["spms"], [str(good)])
            self.assertEqual(len(result["skipped"]), 1)
            self.assertIn("T_missing", result["skipped"][0]["reason"])
            slots = inspect_material_slots(gzip.open(good, "rt", encoding="utf-8").read())["1"]["slots"]
            self.assertEqual(slots["color"]["filename"], "texture/T_test_color.tga")
            with self.assertRaises(RuntimeError):
                normalize_spms_transactionally(
                    jobs[:1], backup_root=root / "backups", skip_unbuildable=True)

    def test_non_square_source_keeps_ratio_with_long_edge_capped_to_4k(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "bark.png"
            Image.new("RGB", (1024, 8192), (128, 128, 128)).save(path)
            size = sbs_auto.render_size_log2({"Base_Color": path})
            self.assertEqual(size, (9, 12))
            self.assertEqual(sbs_auto.size_log2_pixels(size), (512, 4096))

    def test_leaf_provenance_is_not_overwritten_by_local_spm_copy(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            owner = root / "SK_weed_test.spm"
            local = root / "textures"
            external = root / "Texture" / "Grass" / "original"
            local_albedo = self._image(local / "family_4K_Albedo.png")
            local_opacity = self._image(local / "family_4K_Opacity.png")
            external_refs = [
                self._image(external / f"family_4K_{suffix}.jpg")
                for suffix in ("Albedo", "Opacity", "Normal", "Roughness", "Displacement", "AO", "Translucency")
            ]
            xml = f'''<SpeedTree><Materials><Material_v8 ID="1" Name="M_leaf_atlas_01">
<TexFilename>{local_albedo}</TexFilename><TexFilename>{local_opacity}</TexFilename>
</Material_v8></Materials></SpeedTree>'''.encode()
            with gzip.open(owner, "wb") as handle:
                handle.write(xml)
            report = {"items": [{
                "name": "weed_test", "folder": str(root), "status": "needs_texture_work",
                "chosen_spm": str(owner), "sbs_files": [], "texture_dir": str(root / "texture"),
                "normal_convention": "unknown", "ao_policy": "", "sdf_policy": "", "actions": [],
                "leaf_mesh_sources": [{
                    "source_refs": external_refs,
                    "targets": [{"material_names": ["M_leaf_atlas_01"]}],
                    "trace_sources": [],
                }],
                "cluster_items": [{
                    "name": "M_leaf_atlas_01", "source": "material", "cluster_spm": None,
                    "material_names": ["M_leaf_atlas_01"], "material_spms": [str(owner)],
                    "needs_leaf_mesh": True, "atlas_base": "M_leaf_atlas_01",
                    "texture_base": "T_leaf_atlas_01", "atlas_blends": [],
                    "texture_dir": str(root / "texture"), "export_maps": {},
                    "missing_export_maps": list(sbs_auto.RENDER_MAPS),
                    "source_albedo": [local_albedo], "source_alpha": [local_opacity],
                    "m_graph": None, "m_graph_sbs": None,
                }],
            }], "pcg_targets": {}}
            row = build_texture_plan_from_report(report)["items"][0]
            self.assertTrue(row["canonical_source_provenance"])
            self.assertEqual(row["source_albedo"], [external_refs[0]])
            self.assertEqual(row["source_alpha"], [external_refs[1]])
            self.assertEqual(row["source_normal"], [external_refs[2]])

    def test_cluster_render_material_is_preserved_from_non_sk_source(self):
        map_template = '''<Map Name="{name}"><ColorX>{value}</ColorX><ColorY>0</ColorY><ColorZ>0</ColorZ>
<TexFilename>{filename}</TexFilename><TexSource>0</TexSource><TexInvert>false</TexInvert>
<TexInvertRed>false</TexInvertRed><TexInvertGreen>false</TexInvertGreen>
<TexInvertBlue>false</TexInvertBlue><TexEnabled>true</TexEnabled></Map>'''
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "cluster").mkdir()
            source_maps = "".join([
                map_template.format(name="Color", value="1", filename="cluster/branch_01.tga"),
                map_template.format(name="Opacity", value="1", filename="cluster/branch_01_Opacity.tga"),
                map_template.format(name="Normal", value="1", filename="cluster/branch_01_Normal.tga"),
            ])
            managed_maps = "".join(
                map_template.format(name=name, value="1", filename=f"texture/T_branch_01_{role}.tga")
                for name, role in (("Color", "color"), ("Opacity", "opacity"),
                                   ("Normal", "normal"), ("Custom", "extra"))
            )
            def xml(name, maps):
                return f'''<SpeedTree><Materials><Material_v8 ID="1" Name="{name}">{maps}</Material_v8>
<Generator><Property><Name>Branches:Material</Name><Value>1</Value></Property></Generator></SpeedTree>'''.encode()
            source = root / "tree_test.spm"
            sk = root / "SK_tree_test.spm"
            with gzip.open(source, "wb") as handle:
                handle.write(xml("Branch_01", source_maps))
            with gzip.open(sk, "wb") as handle:
                handle.write(xml("M_Branch_01", managed_maps))
            sk_without_matching_source = root / "SK_tree_test_b.spm"
            with gzip.open(sk_without_matching_source, "wb") as handle:
                handle.write(xml("M_Branch_01", managed_maps))
            self.assertEqual(material_texture_items(
                root,
                {"atlas_root": str(root / "atlas"),
                 "required_export_maps": list(sbs_auto.RENDER_MAPS)},
                [root / "texture"], {},
            ), [])
            preserved = preserved_cluster_materials(root)
            self.assertEqual(len(preserved), 2)
            self.assertEqual(
                {Path(row["spm"]).name for row in preserved},
                {sk.name, sk_without_matching_source.name},
            )
            jobs = jobs_from_texture_plan({
                "items": [], "preserved_cluster_materials": preserved,
            })
            job = next(row for row in jobs if Path(row["spm"]) == sk)
            patch = build_spm_patch(sk, job["materials"], require_outputs=False)
            slots = inspect_material_slots(patch["text"])["1"]["slots"]
            self.assertEqual(slots["color"]["filename"], "cluster/branch_01.tga")
            self.assertEqual(slots["opacity"]["filename"], "cluster/branch_01_Opacity.tga")
            self.assertTrue(slots["opacity"]["enabled"] == "true")
            self.assertTrue(is_cluster_render_material(
                root,
                sk,
                [r"..\shared_asset\cluster\branch_shared_01.tga"],
            ))

    def test_normalized_subsurface_activation_is_idempotent(self):
        xml = b'''<SpeedTree><Materials><Material_v8 ID="1" Name="M_leaf_test">
<Map Name="SubsurfaceColor"><ColorX>1</ColorX><TexFilename>texture/T_leaf_test_subsurface.tga</TexFilename>
<TexSource>0</TexSource><TexEnabled>true</TexEnabled><TexInvert>false</TexInvert></Map>
</Material_v8></Materials></SpeedTree>'''
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            spm = root / "SK_test.spm"
            with gzip.open(spm, "wb") as handle:
                handle.write(xml)
            plan = {"items": [{
                "atlas_base": "M_leaf_test",
                "texture_base": "T_leaf_test",
                "texture_dir": str(root / "texture"),
                "material_names": ["M_leaf_test"],
                "material_spms": [str(spm)],
                "source_subsurface": [str(root / "texture" / "T_leaf_test_subsurface.tga")],
            }]}
            jobs = jobs_from_texture_plan(plan)
            self.assertTrue(jobs[0]["materials"]["m_leaf_test"]["subsurface_enabled"])

    def test_texture_plan_reads_the_spm_that_owns_the_material(self):
        xml = b'''<?xml version="1.0" encoding="utf-8"?>
<SpeedTree><Materials><Material_v8 ID="1" Name="M_leaf_target_atlas_01">
<TexFilename>textures/target_4K_Albedo.png</TexFilename>
<TexFilename>textures/target_4K_Opacity.png</TexFilename>
</Material_v8></Materials></SpeedTree>'''
        with tempfile.TemporaryDirectory() as temp:
            temp = Path(temp)
            owner_spm = temp / "owner.spm"
            with gzip.open(owner_spm, "wb") as handle:
                handle.write(xml)
            report = {
                "items": [{
                    "name": "Tree_test", "folder": str(temp), "status": "needs_texture_work",
                    "chosen_spm": str(temp / "different.spm"), "sbs_files": [],
                    "texture_dir": str(temp), "normal_convention": "unknown",
                    "ao_policy": "", "sdf_policy": "", "actions": [],
                    "cluster_items": [{
                        "name": "M_leaf_target_atlas_01", "source": "material",
                        "cluster_spm": None, "material_names": ["M_leaf_target_atlas_01"],
                        "material_spms": [str(owner_spm)], "needs_leaf_mesh": True,
                        "atlas_base": "M_leaf_target_atlas_01", "atlas_blends": [],
                        "texture_dir": str(temp), "export_maps": {},
                        "missing_export_maps": list(sbs_auto.RENDER_MAPS),
                        "m_graph": None, "m_graph_sbs": None,
                    }],
                }],
                "pcg_targets": {},
            }
            row = build_texture_plan_from_report(report)["items"][0]
            self.assertEqual(row["source_scope"], "material_spm")
            self.assertEqual(row["source_albedo"], ["textures\\target_4K_Albedo.png"])
            self.assertEqual(row["source_alpha"], ["textures\\target_4K_Opacity.png"])

    def test_select_source_set_keeps_maps_in_one_family(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            elm = root / "Texture" / "Elm" / "TCom_Leaves_Elm01"
            other = root / "Texture" / "Nothofagus" / "Other_Set"
            row = {
                "folder_name": "Tree_elm",
                "cluster_name": "leaf_elm_01",
                "atlas_base": "M_leaf_elm_atlas_01",
                "source_albedo": [
                    self._image(other / "Other_4K_albedo.png"),
                    self._image(elm / "TCom_Leaves_Elm01_4K_albedo.png"),
                ],
                "source_alpha": [
                    self._image(other / "Other_4K_alpha.png"),
                    self._image(elm / "TCom_Leaves_Elm01_4K_alpha.png"),
                ],
                "source_normal": [
                    self._image(other / "Other_4K_normal.png"),
                    self._image(elm / "TCom_Leaves_Elm01_4K_normal.png"),
                ],
            }
            selected = sbs_auto.select_source_set(row)
            self.assertEqual(selected["parent"], elm)
            self.assertEqual(selected["paths"]["albedo"].parent, elm)
            self.assertEqual(selected["paths"]["alpha"].parent, elm)

    def test_select_source_set_blocks_equal_candidates(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "Texture" / "anamone"
            row = {
                "folder_name": "weed_anamone",
                "cluster_name": "leaf_anamone_01",
                "atlas_base": "M_leaf_anamone_atlas_01",
                "source_albedo": [],
                "source_alpha": [],
            }
            for family in ("set_a", "set_b"):
                row["source_albedo"].append(self._image(root / family / f"{family}_albedo.png"))
                row["source_alpha"].append(self._image(root / family / f"{family}_alpha.png"))
            with self.assertRaisesRegex(RuntimeError, "여러 개"):
                sbs_auto.select_source_set(row)
            candidates = sbs_auto.source_set_candidates(row)
            selected = sbs_auto.select_source_set(row, preferred=candidates[1]["label"])
            self.assertEqual(selected["label"], candidates[1]["label"])

    def test_cluster_leaf_sources_dedupe_but_only_final_sk_is_targeted(self):
        def write_spm(path, materials):
            blocks = []
            generators = []
            for index, (name, refs) in enumerate(materials, 1):
                tex = "".join(f"<TexFilename>{ref}</TexFilename>" for ref in refs)
                blocks.append(
                    f'<Material_v8 ID="{index}" Name="{name}">{tex}</Material_v8>')
                generators.append(
                    '<Generator Type="Leaf Mesh"><Properties><Property>'
                    f'<Name>Leaves:Type:0:Material</Name><Value>{index}</Value>'
                    '</Property></Properties></Generator>')
            xml = ("<?xml version=\"1.0\"?><SpeedTree><Materials>"
                   + "".join(blocks) + "</Materials><Generators>"
                   + "".join(generators) + "</Generators></SpeedTree>").encode()
            path.parent.mkdir(parents=True, exist_ok=True)
            with gzip.open(path, "wb") as handle:
                handle.write(xml)

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            cluster_dir = root / "Cluster"
            texture_dir = root / "texture" / "Leaf"
            common_albedo = self._image(texture_dir / "Common_4K_albedo.tif")
            common_alpha = self._image(texture_dir / "Common_4K_alpha.tif")
            dry_albedo = self._image(texture_dir / "Dry_4K_albedo.tif")
            dry_alpha = self._image(texture_dir / "Dry_4K_alpha.tif")
            rel_common = [
                str(Path(common_albedo).relative_to(cluster_dir.parent)),
                str(Path(common_alpha).relative_to(cluster_dir.parent)),
            ]
            rel_dry = [
                str(Path(dry_albedo).relative_to(cluster_dir.parent)),
                str(Path(dry_alpha).relative_to(cluster_dir.parent)),
            ]
            # Cluster SPM refs are relative to Cluster/, so prefix one parent.
            rel_common = [str(Path("..") / value) for value in rel_common]
            rel_dry = [str(Path("..") / value) for value in rel_dry]
            cluster_a = cluster_dir / "leaf_test_01.spm"
            cluster_b = cluster_dir / "leaf_test_side_01.spm"
            write_spm(cluster_a, [("Material", rel_common), ("Material 2", rel_dry)])
            write_spm(cluster_b, [("Material", rel_common)])
            final_spm = root / "SK_tree_test_01.spm"
            write_spm(final_spm, [("M_leaf_test", [
                r"Cluster\leaf_test_01.tga", r"Cluster\leaf_test_01_Opacity.tga",
                r"Cluster\leaf_test_side_01.tga", r"Cluster\leaf_test_side_01_Opacity.tga",
            ])])
            cfg = {"atlas_root": str(root / "atlas")}
            sources, referenced = discover_leaf_mesh_sources(
                root, cfg, [final_spm], cluster_spms(root))
            self.assertEqual(len(referenced), 2)
            self.assertEqual(len(sources), 2)
            common = next(row for row in sources if row["source_family"].lower() == "common")
            self.assertEqual(
                {Path(target["spm"]).name for target in common["targets"]},
                {final_spm.name},
            )
            self.assertEqual(
                {Path(trace["spm"]).name for trace in common["trace_sources"]},
                {cluster_a.name, cluster_b.name},
            )

            # The same trace must work after an SK was already converted to
            # managed T_ outputs.  Its exact non-SK source remains authoritative
            # for discovering which Cluster SPM produced the rendered card.
            source_spm = root / "tree_test_02.spm"
            converted_spm = root / "SK_tree_test_02.spm"
            write_spm(source_spm, [("leaf_test", [
                r"Cluster\leaf_test_01.tga", r"Cluster\leaf_test_01_Opacity.tga",
                r"Cluster\leaf_test_side_01.tga", r"Cluster\leaf_test_side_01_Opacity.tga",
            ])])
            write_spm(converted_spm, [("M_leaf_test", [
                r"texture\T_leaf_test_color.tga", r"texture\T_leaf_test_opacity.tga",
                r"texture\T_leaf_test_normal.tga", r"texture\T_leaf_test_extra.tga",
                r"texture\T_leaf_test_height.tga",
            ])])
            converted_sources, converted_referenced = discover_leaf_mesh_sources(
                root, cfg, [converted_spm], cluster_spms(root))
            self.assertEqual(len(converted_referenced), 2)
            self.assertEqual(
                {row["source_family"].lower() for row in converted_sources},
                {"common", "dry"},
            )


class MaterialTargetNormalizationTests(unittest.TestCase):
    MAP_TEMPLATE = '''<Map Name="{name}"><ColorX>1</ColorX><ColorY>1</ColorY><ColorZ>1</ColorZ>
<TexFilename>old.png</TexFilename><TexSource>0</TexSource>
<TexInvert>false</TexInvert><TexInvertRed>false</TexInvertRed>
<TexInvertGreen>false</TexInvertGreen><TexInvertBlue>false</TexInvertBlue>
<TexEnabled>true</TexEnabled></Map>'''

    def make_duplicate_name_spm(self, root):
        maps = "".join(
            self.MAP_TEMPLATE.format(name=name)
            for name in ("Color", "Opacity", "Normal", "Custom")
        )
        xml = f'''<SpeedTree><Assets>
<Material_v8 ID="1" Name="M_leaf_shared">{maps}</Material_v8>
<Material_v8 ID="2" Name="M_leaf_shared">{maps}</Material_v8>
</Assets><Generator Type="Branch"><Properties>
<Property><Name>Leaves:Type:0:Material</Name><Value>1</Value></Property>
<Property><Name>Leaves:Type:1:Material</Name><Value>2</Value></Property>
</Properties></Generator></SpeedTree>'''.encode()
        spm = root / "SK_duplicate_names.spm"
        with gzip.open(spm, "wb") as handle:
            handle.write(xml)
        return spm

    def make_outputs(self, root, texture_base):
        texture_dir = root / "texture"
        texture_dir.mkdir(exist_ok=True)
        for role in ("color", "normal", "extra", "height", "opacity", "subsurface"):
            (texture_dir / f"{texture_base}_{role}.tga").write_bytes(role.encode())
        return texture_dir

    def test_build_spm_patch_prefers_exact_id_over_name_fallback(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            spm = self.make_duplicate_name_spm(root)
            texture_dir = self.make_outputs(root, "T_leaf_exact")
            self.make_outputs(root, "T_leaf_fallback")

            patch = build_spm_patch(spm, {
                "@id:1": {
                    "texture_dir": str(texture_dir),
                    "texture_base": "T_leaf_exact",
                    "subsurface_enabled": False,
                },
                "m_leaf_shared": {
                    "texture_dir": str(texture_dir),
                    "texture_base": "T_leaf_fallback",
                    "subsurface_enabled": False,
                },
            })

            slots = inspect_material_slots(patch["text"])
            self.assertIn("T_leaf_exact_color.tga", slots["1"]["slots"]["color"]["filename"])
            self.assertIn("T_leaf_fallback_color.tga", slots["2"]["slots"]["color"]["filename"])

    def test_exact_material_targets_split_duplicate_names_by_id(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            spm = self.make_duplicate_name_spm(root)
            texture_dir = self.make_outputs(root, "T_leaf_one")
            self.make_outputs(root, "T_leaf_two")
            plan = {"items": [
                {
                    "texture_dir": str(texture_dir),
                    "texture_base": "T_leaf_one",
                    "material_names": ["M_leaf_shared"],
                    "material_targets": [{"spm": str(spm), "material_id": "1"}],
                },
                {
                    "texture_dir": str(texture_dir),
                    "texture_base": "T_leaf_two",
                    "material_names": ["M_leaf_shared"],
                    "material_targets": [{"spm": str(spm), "material_id": "2"}],
                },
            ]}

            jobs = jobs_from_texture_plan(plan)
            self.assertEqual(set(jobs[0]["materials"]), {"@id:1", "@id:2"})
            patch = build_spm_patch(spm, jobs[0]["materials"])
            slots = inspect_material_slots(patch["text"])
            self.assertIn("T_leaf_one_color.tga", slots["1"]["slots"]["color"]["filename"])
            self.assertIn("T_leaf_two_color.tga", slots["2"]["slots"]["color"]["filename"])

    def test_exact_material_target_conflict_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            spm = self.make_duplicate_name_spm(root)
            plan = {"items": [
                {
                    "texture_dir": str(root / "texture"),
                    "texture_base": texture_base,
                    "material_targets": [{"spm": str(spm), "material_id": 1}],
                }
                for texture_base in ("T_leaf_one", "T_leaf_two")
            ]}
            with self.assertRaisesRegex(
                    RuntimeError, r"conflicting managed output mapping for @id:1"):
                jobs_from_texture_plan(plan)

    def test_legacy_name_conflict_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            spm = self.make_duplicate_name_spm(root)
            plan = {"items": [
                {
                    "texture_dir": str(root / "texture"),
                    "texture_base": texture_base,
                    "material_names": ["M_leaf_shared"],
                    "material_spms": [str(spm)],
                }
                for texture_base in ("T_leaf_one", "T_leaf_two")
            ]}
            with self.assertRaisesRegex(
                    RuntimeError, r"conflicting managed output mapping for m_leaf_shared"):
                jobs_from_texture_plan(plan)


class Step3ExactTargetTests(unittest.TestCase):
    def test_texture_plan_jobs_only_include_exact_allowed_sk_variant(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            selected = root / "SK_weed_ladyfern_01.spm"
            sibling = root / "SK_weed_ladyfern_b_01.spm"
            selected.write_text("<SpeedTree/>", encoding="utf-8")
            sibling.write_text("<SpeedTree/>", encoding="utf-8")
            plan = {"items": [{
                "atlas_base": "M_leaf_ladyfern",
                "texture_base": "T_leaf_ladyfern",
                "texture_dir": str(root / "texture"),
                "material_names": ["M_leaf_ladyfern"],
                "material_spms": [str(selected), str(sibling)],
            }]}

            jobs = jobs_from_texture_plan(
                plan, allowed_spms=[selected]
            )

        self.assertEqual([Path(job["spm"]).name for job in jobs], [
            "SK_weed_ladyfern_01.spm"
        ])


class GuiLabelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = TOOL_DIR / "pcg_texture_gui.pyw"
        loader = importlib.machinery.SourceFileLoader("pcg_texture_gui_test", str(path))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        cls.gui = importlib.util.module_from_spec(spec)
        loader.exec_module(cls.gui)

    def test_generic_material_summary_names_exact_spm_and_material(self):
        item = {"target_spm_statuses": [{
            "mesh_name": "weed_blackgum_01",
            "sk_spm": r"D:\Tree\SK_weed_blackgum_01.spm",
            "materials_missing_m_prefix": ["Material 2"],
        }]}
        self.assertEqual(
            self.gui.generic_material_summary(item),
            "⚠ SK_weed_blackgum_01.spm → Material 2",
        )

    def test_pcg_board_does_not_expose_sk_repair_blend_status(self):
        source = (TOOL_DIR / "pcg_texture_gui.pyw").read_text(encoding="utf-8")
        self.assertFalse(hasattr(self.gui.App, "step4_text"))
        self.assertNotIn("④ SK Blend", source)
        self.assertIn("④ Blend ↔ SPM 확인", source)
        self.assertIn("blender_connection_overview", source)

    def test_step3_uses_exact_target_status_not_folder_sibling_variants(self):
        selected = r"D:\Trees\ladyfern\SK_weed_ladyfern_01.spm"
        sibling = r"D:\Trees\ladyfern\SK_weed_ladyfern_b_01.spm"
        entries = {"ladyfern": {
            "checked": True,
            "item": {
                "target_spm_statuses": [{"sk_spm": selected}],
                "sk_spms": [selected, sibling],
            },
        }}

        self.assertEqual(self.gui.checked_step3_spms(entries), [selected])

    def test_step3_never_normalizes_raw_cluster_sources(self):
        raw_cluster = r"D:\Trees\Tree_elm\Cluster\branch_elm_01.spm"
        full_sk = r"D:\Trees\Tree_elm\SK_Tree_elm_01.spm"
        entries = {
            "tree_elm": {
                "checked": True,
                "item": {
                    "target_spm_statuses": [
                        {"sk_spm": full_sk},
                        {"sk_spm": raw_cluster},
                    ],
                },
            },
        }

        self.assertEqual(self.gui.checked_step3_spms(entries), [full_sk])

    def test_initial_refresh_runs_in_worker_and_applies_via_root_after(self):
        class FakeRoot:
            def __init__(self):
                self.callbacks = []

            def after(self, delay, callback):
                self.callbacks.append((delay, callback))

            def update_idletasks(self):
                pass

        class FakeVar:
            def __init__(self, value):
                self.value = value
                self.values = []

            def get(self):
                return self.value

            def set(self, value):
                self.value = value
                self.values.append(value)

        threads = []

        class FakeThread:
            def __init__(self, target, daemon):
                self.target = target
                self.daemon = daemon
                self.started = False
                threads.append(self)

            def start(self):
                self.started = True

        report = {"items": []}
        app = self.gui.App.__new__(self.gui.App)
        app.root = FakeRoot()
        app.cfg = {"tree_root": "old"}
        app.root_var = FakeVar("new")
        app.use_pcg_targets_var = FakeVar(True)
        app.status_var = FakeVar("대기")
        app.report = None
        app.sync_state = {"migration_complete": True, "entries": {}}
        app.texplan_cache = {"stale": []}
        app.worker = None
        app._busy = False
        app._manual_refreshing = False
        app._set_busy = mock.Mock()
        app.populate = mock.Mock()
        app._update_summary = mock.Mock()

        with mock.patch.object(self.gui, "save_config") as save_config, \
                mock.patch.object(self.gui, "load_pcg_targets", return_value={"meshes": []}), \
                mock.patch.object(self.gui, "make_report", return_value=report) as make_report, \
                mock.patch.object(self.gui, "save_spm_analysis_cache"), \
                mock.patch.object(
                    self.gui, "load_sync_state",
                    return_value={"migration_complete": True, "entries": {}}), \
                mock.patch.object(self.gui.threading, "Thread", FakeThread):
            app._start_initial_refresh()
            self.assertEqual(len(threads), 1)
            self.assertTrue(threads[0].started)
            make_report.assert_not_called()
            self.assertIsNone(app.report)
            app.refresh()
            make_report.assert_not_called()
            self.assertEqual(app.status_var.value, "초기 검사 중... (끝나면 자동으로 다시 검사)")

            threads[0].target()
            make_report.assert_called_once_with(
                {"tree_root": "new"}, pcg_targets={"meshes": []})
            save_config.assert_called_once_with({"tree_root": "new"})
            self.assertIsNone(app.report)
            self.assertEqual(len(app.root.callbacks), 1)

            delay, callback = app.root.callbacks.pop()
            self.assertEqual(delay, 0)
            callback()

            # The refresh that arrived mid-initial-scan must run after the
            # initial result lands instead of being silently dropped (race:
            # target-refresh buttons finishing during the first audit).
            self.assertEqual(make_report.call_count, 1)
            self.assertEqual(len(threads), 2)
            self.assertTrue(threads[1].started)
            self.assertFalse(app._pending_refresh)

            threads[1].target()
            self.assertEqual(make_report.call_count, 2)
            self.assertEqual(len(app.root.callbacks), 1)
            delay, callback = app.root.callbacks.pop()
            self.assertEqual(delay, 0)
            callback()

        self.assertIs(app.report, report)
        self.assertEqual(app.texplan_cache, {})
        self.assertEqual(app._set_busy.call_args_list, [
            mock.call(True), mock.call(False),
            mock.call(True), mock.call(False),
        ])
        self.assertEqual(app.populate.call_count, 2)
        self.assertEqual(app._update_summary.call_count, 2)
        self.assertIsNone(app.worker)

    def test_manual_refresh_runs_audit_in_worker(self):
        class FakeRoot:
            def __init__(self):
                self.callbacks = []

            def after(self, delay, callback):
                self.callbacks.append((delay, callback))

        class FakeVar:
            def __init__(self, value):
                self.value = value

            def get(self):
                return self.value

            def set(self, value):
                self.value = value

        threads = []

        class FakeThread:
            def __init__(self, target, daemon):
                self.target = target
                self.daemon = daemon
                threads.append(self)

            def start(self):
                pass

        report = {"items": []}
        sync_state = {"migration_complete": True, "entries": {}}
        app = self.gui.App.__new__(self.gui.App)
        app.root = FakeRoot()
        app.cfg = {"tree_root": "old"}
        app.root_var = FakeVar("new")
        app.use_pcg_targets_var = FakeVar(True)
        app.status_var = FakeVar("대기")
        app.report = {"items": ["stale"]}
        app.sync_state = {"entries": {"stale": {}}}
        app.texplan_cache = {"stale": []}
        app.texplan_errors = {"stale": "error"}
        app._busy = False
        app._initial_refreshing = False
        app._manual_refreshing = False
        app._sync_state_migrating = False
        app._pending_refresh = False
        app._set_busy = mock.Mock()
        app.populate = mock.Mock()
        app._update_summary = mock.Mock()

        with mock.patch.object(self.gui, "save_config"), \
                mock.patch.object(
                    self.gui, "load_pcg_targets", return_value={"meshes": []}), \
                mock.patch.object(
                    self.gui, "make_report", return_value=report) as make_report, \
                mock.patch.object(self.gui, "save_spm_analysis_cache"), \
                mock.patch.object(
                    self.gui, "load_sync_state", return_value=sync_state), \
                mock.patch.object(self.gui.threading, "Thread", FakeThread):
            self.assertTrue(app.refresh())
            make_report.assert_not_called()
            self.assertEqual(len(threads), 1)

            threads[0].target()
            make_report.assert_called_once_with(
                {"tree_root": "new"}, pcg_targets={"meshes": []}
            )
            delay, callback = app.root.callbacks.pop()
            self.assertEqual(delay, 0)
            callback()

        self.assertIs(app.report, report)
        self.assertIs(app.sync_state, sync_state)
        self.assertFalse(app._manual_refreshing)
        self.assertEqual(app.texplan_cache, {})
        self.assertEqual(app.texplan_errors, {})
        app.populate.assert_called_once_with()
        app._update_summary.assert_called_once_with()
        self.assertEqual(app._set_busy.call_args_list, [
            mock.call(True), mock.call(False),
        ])

    def test_busy_disables_refresh_target_and_selection_controls(self):
        control_names = (
            "btn_prepare", "btn_step2", "btn_refresh", "btn_pick_root",
            "btn_select_all", "btn_clear_all", "btn_live_targets",
            "btn_saved_targets", "root_entry", "chk_pcg_targets",
            "chk_force", "chk_spm_push",
        )
        app = self.gui.App.__new__(self.gui.App)
        controls = {}
        for name in control_names:
            controls[name] = mock.Mock()
            setattr(app, name, controls[name])
        app.items = {}
        app._sync_state_migrating = False
        app.btn_step3 = mock.Mock()

        app._set_busy(True)
        app._set_busy(False)

        for control in controls.values():
            self.assertEqual(control.configure.call_args_list, [
                mock.call(state="disabled"), mock.call(state="normal"),
            ])

    def test_busy_click_selects_copyable_row_without_toggling_check(self):
        app = self.gui.App.__new__(self.gui.App)
        app._busy = True
        app.tree = mock.Mock()
        app.tree.identify_row.return_value = "row"
        app.row_copy_paths = {"row": [Path("SK_tree_test.spm")]}
        app.checked_rows = mock.Mock()
        event = type("Event", (), {"x": 10, "y": 20})()

        self.assertEqual(app._on_click(event), "break")

        app.tree.selection_set.assert_called_once_with("row")
        app.tree.focus.assert_called_once_with("row")
        app.tree.focus_set.assert_called_once_with()
        app.checked_rows.click.assert_not_called()

    def test_target_refresh_rejects_duplicate_and_times_out(self):
        class FakeRoot:
            def __init__(self):
                self.callbacks = []

            def after(self, delay, callback):
                self.callbacks.append((delay, callback))

        class FakeVar:
            def __init__(self):
                self.value = None

            def set(self, value):
                self.value = value

        threads = []

        class FakeThread:
            def __init__(self, target, daemon):
                self.target = target
                threads.append(self)

            def start(self):
                pass

        app = self.gui.App.__new__(self.gui.App)
        app.root = FakeRoot()
        app.cfg = {"pcg_target_refresh_timeout": 7}
        app.status_var = FakeVar()
        app._busy = False
        app._target_refresh_active = False
        app._set_busy = mock.Mock()

        with mock.patch.object(self.gui.threading, "Thread", FakeThread), \
                mock.patch.object(
                    self.gui.subprocess, "run",
                    side_effect=self.gui.subprocess.TimeoutExpired("cmd", 7),
                ) as run, \
                mock.patch.object(self.gui.messagebox, "showerror") as showerror:
            self.assertTrue(app.refresh_pcg_targets())
            self.assertFalse(app.import_saved_pcg_report())
            self.assertEqual(len(threads), 1)

            threads[0].target()
            self.assertEqual(run.call_args.kwargs["timeout"], 7)
            delay, callback = app.root.callbacks.pop()
            self.assertEqual(delay, 0)
            callback()

        self.assertFalse(app._target_refresh_active)
        self.assertIn("실패", app.status_var.value)
        self.assertIn("7초", showerror.call_args.args[1])
        self.assertEqual(app._set_busy.call_args_list, [
            mock.call(True), mock.call(False),
        ])

    def test_target_refresh_success_releases_guard_and_starts_async_audit(self):
        app = self.gui.App.__new__(self.gui.App)
        app.worker = mock.Mock()
        app._target_refresh_active = True
        app._busy = True
        app._pending_refresh = True
        app._set_busy = mock.Mock()
        app.use_pcg_targets_var = mock.Mock()
        app.log = mock.Mock()
        app.refresh = mock.Mock(return_value=True)
        result = mock.Mock(returncode=0, stdout="targets updated", stderr="")

        app._pcg_targets_done(result)

        self.assertIsNone(app.worker)
        self.assertFalse(app._target_refresh_active)
        self.assertFalse(app._pending_refresh)
        app._set_busy.assert_called_once_with(False)
        app.use_pcg_targets_var.set.assert_called_once_with(True)
        app.refresh.assert_called_once_with()

    def test_step3_normalization_filters_plan_to_exact_selected_spm(self):
        selected = r"D:\Trees\ladyfern\SK_weed_ladyfern_01.spm"
        sibling = r"D:\Trees\ladyfern\SK_weed_ladyfern_b_01.spm"
        app = self.gui.App.__new__(self.gui.App)
        app.cfg = {
            "tree_root": r"D:\Trees",
            "sbsrender_timeout": 10,
            "unreal_texture_sync_enabled": False,
        }
        app.status_var = mock.Mock()
        app.log = mock.Mock()
        app._ui = lambda callback: callback()
        app._step3_finished = mock.Mock()
        plan = {
            "items": [],
            "preserved_cluster_materials": [
                {"spm": selected, "material_name": "M_leaf_a"},
                {"spm": sibling, "material_name": "M_leaf_b"},
            ],
        }

        with mock.patch.object(
                self.gui, "make_report", return_value={}
        ) as make_report, mock.patch.object(
                self.gui, "save_spm_analysis_cache"
        ), mock.patch.object(
                self.gui, "build_texture_plan_from_report", return_value=plan
        ), mock.patch.object(
                self.gui, "jobs_from_texture_plan", return_value=[]
        ) as build_jobs, mock.patch.object(
                self.gui, "normalize_spms_transactionally",
                return_value={
                    "spms": [], "materials": 0,
                    "backup_dir": None, "skipped": [],
                },
        ), mock.patch.object(
                self.gui, "cleanup_preserved_cluster_outputs",
                return_value={"cleaned": [], "conflicts": []},
        ) as cleanup:
            app._run_step3([], [selected], sync_files=[])

        make_report.assert_called_once_with(
            app.cfg, targets=[r"D:\Trees\ladyfern"]
        )
        exact_plan = build_jobs.call_args.args[0]
        self.assertEqual(
            [row["spm"] for row in exact_plan["preserved_cluster_materials"]],
            [selected],
        )
        self.assertEqual(build_jobs.call_args.kwargs["allowed_spms"], [selected])
        self.assertIs(cleanup.call_args.args[0], exact_plan)

    def test_atlas_generation_loads_user_startup_with_clean_preferences(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            blender = root / "Blender 5.1" / "blender.exe"
            startup = (root / "AppData" / "Blender Foundation" / "Blender" /
                       "5.1" / "config" / "startup.blend")
            startup.parent.mkdir(parents=True)
            startup.touch()
            with mock.patch.dict("os.environ", {"APPDATA": str(root / "AppData")}):
                command = self.gui.atlas_blender_command(str(blender))
        self.assertIn("--factory-startup", command)
        self.assertIn("--background", command)
        self.assertIn(str(startup), command)
        self.assertLess(command.index(str(startup)), command.index("--python"))

    def test_atlas_generation_uses_factory_startup_without_user_startup_file(self):
        with tempfile.TemporaryDirectory() as temp:
            blender = Path(temp) / "Blender 5.1" / "blender.exe"
            with mock.patch.dict("os.environ", {"APPDATA": ""}):
                command = self.gui.atlas_blender_command(str(blender))

        self.assertIn("--factory-startup", command)
        self.assertIn("--background", command)
        self.assertLess(command.index("--factory-startup"), command.index("--python"))

    def test_target_column_uses_readable_source_names(self):
        self.assertEqual(
            self.gui.App._target_source_text({
                "pcg_mesh_names": ["a", "b"],
                "level_mesh_names": ["c"],
            }),
            "PCG 2 / 레벨 1",
        )

    def test_target_row_color_distinguishes_pcg_level_and_overlap(self):
        self.assertEqual(
            self.gui.App._target_row_tag({"pcg_mesh_names": ["a"]}),
            "target_pcg",
        )
        self.assertEqual(
            self.gui.App._target_row_tag({"level_mesh_names": ["a"]}),
            "target_level",
        )
        self.assertEqual(
            self.gui.App._target_row_tag({
                "pcg_mesh_names": ["a"], "level_mesh_names": ["a"],
            }),
            "target_both",
        )

    def test_prefixed_default_material_is_not_a_step1_warning(self):
        normal, generic = self.gui.split_generic(["M_Material 2"])
        self.assertEqual(normal, [])
        self.assertEqual(generic, [])

    def test_lowercase_m_prefix_is_not_double_counted_as_unprefixed(self):
        normal, generic = self.gui.split_generic(["m_leaf_test"])
        self.assertEqual(normal, [])
        self.assertEqual(generic, [])

    def test_current_cluster_pair_is_rendered_as_complete(self):
        self.assertEqual(
            self.gui.cluster_pair_step1_text({
                "pair_status": "current",
                "pair_action": "none",
                "pair_conflicts": [],
                "target_status": {"status": "ready"},
            }),
            "완료 ✓",
        )

    def test_physical_cluster_capture_hides_legacy_eight_map_count(self):
        physical = {
            "normalization_workflow_mode": "PHYSICAL_DIRECT_CAPTURE",
            "physical_capture_resolution": [1024, 1024],
            "physical_capture_core_missing": [],
        }
        paths = [f"map_{index}.tga" for index in range(8)]

        self.assertEqual(
            self.gui.cluster_texture_status_text(physical, paths, []),
            "Blender 촬영 1024² · 완료 ✓",
        )
        self.assertEqual(
            self.gui.cluster_texture_status_text({}, paths, []),
            "Cluster 출력 TGA 연결 8장",
        )

    def test_step2_label_distinguishes_complete_and_partial(self):
        complete = {"leaf_mesh_sources": [
            {"atlas_blends": ["a.blend"], "targets": []},
            {"atlas_blends": ["b.blend"], "targets": []},
        ]}
        partial = {"leaf_mesh_sources": [
            {"atlas_blends": ["a.blend"], "targets": []},
            {"atlas_blends": [], "targets": []},
        ]}
        self.assertEqual(
            self.gui.App.step2_text(None, complete),
            "현재 잎 매쉬 2세트 · 연결 완료 ✓")
        self.assertEqual(
            self.gui.App.step2_text(None, partial),
            "작업 필요 · 현재 연결 완료 1세트 · 새로 만들기 1세트",
        )

    def test_step2_label_keeps_managed_connected_output_truthful(self):
        item = {
            "leaf_mesh_sources": [],
            "managed_leaf_outputs": [
                {"spm": "SK_weed_ladyfern_01.spm", "material_id": "5"},
                {"spm": "SK_weed_ladyfern_01.spm", "material_id": "6"},
            ],
        }

        self.assertEqual(
            self.gui.App.step2_text(None, item),
            "현재 연결된 잎 매쉬 2개 ✓",
        )

    def test_step2_label_counts_read_only_current_atlas_inventory(self):
        complete = {
            "leaf_mesh_sources": [],
            "leaf_source_provenance": [{"source_family": "Susan"}],
            "leaf_atlas_inventory": [{
                "atlas_base": "M_leaf_susan_atlas_01",
                "atlas_blends": ["M_leaf_susan_atlas_01.blend"],
                "generator_connection_complete": True,
                "complete": True,
            }],
        }
        partial = {
            "leaf_mesh_sources": [],
            "leaf_atlas_inventory": [
                {
                    "atlas_base": "M_leaf_velvet_grass_atlas_01",
                    "atlas_blends": ["M_leaf_velvet_grass_atlas_01.blend"],
                    "generator_connection_complete": True,
                    "complete": True,
                },
                {
                    "atlas_base": "M_leaf_grass_atlas_01",
                    "atlas_blends": [],
                    "generator_connection_complete": True,
                    "complete": False,
                },
            ],
        }

        self.assertEqual(
            self.gui.App.step2_text(None, complete),
            "현재 잎 매쉬 1세트 · 연결 완료 ✓",
        )
        self.assertEqual(
            self.gui.App.step2_text(None, partial),
            "현재 잎 매쉬 2세트 · 연결 완료 1 · 제작 파일 없음 1",
        )

    def test_step2_label_separates_export_active_from_inactive_blender_history(self):
        mixed = {
            "leaf_mesh_sources": [],
            "leaf_atlas_inventory": [
                {
                    "atlas_base": "M_leaf_elm_atlas_01",
                    "atlas_blends": ["M_leaf_elm_atlas_01.blend"],
                    "generator_connection_complete": True,
                    "complete": True,
                    "export_participating": True,
                },
                {
                    "atlas_base": "M_branch_elm_atlas_01",
                    "atlas_blends": ["M_branch_elm_atlas_01.blend"],
                    "generator_connection_complete": True,
                    "complete": True,
                    "export_participating": False,
                },
            ],
        }
        inactive_only = {
            "leaf_mesh_sources": [],
            "leaf_atlas_inventory": [mixed["leaf_atlas_inventory"][1]],
        }

        self.assertEqual(
            self.gui.App.step2_text(None, mixed),
            "현재 잎 매쉬 1세트 · 연결 완료 ✓ · 비활성 기록 1",
        )
        self.assertEqual(
            self.gui.App.step2_text(None, inactive_only),
            "현재 잎 매쉬 없음 · 비활성 Blender 기록 1세트",
        )

    def test_step2_label_separates_work_from_model_scope(self):
        item = {"leaf_mesh_sources": [{
            "atlas_blends": [],
            "targets": [
                {"spm": "SK_tree_01.spm"},
                {"spm": "SK_tree_02.spm"},
            ],
        }]}

        self.assertEqual(
            self.gui.App.step2_text(None, item),
            "작업 필요 · 새로 만들기 1세트 · 적용 모델 2개",
        )

    def test_read_only_current_inventory_never_schedules_spm_reconnect(self):
        app = self.gui.App.__new__(self.gui.App)
        app.cfg = {"atlas_root": r"D:\Trees\atlas"}
        app.items = {"weed_susan": {
            "checked": True,
            "item": {
                "name": "weed_susan",
                "leaf_mesh_sources": [],
                "leaf_source_provenance": [{
                    "source_family": "Susan",
                    "actionable": False,
                }],
                "leaf_atlas_inventory": [{
                    "atlas_base": "M_leaf_susan_atlas_01",
                    "complete": True,
                    "actionable": False,
                }],
            },
        }}

        jobs, skipped = app._step2_jobs(connect_spm=True)

        self.assertEqual(jobs, [])
        self.assertEqual(skipped, [])

    def test_step3_label_reports_sets_exact_map_progress_and_connection(self):
        pending = {"cluster_items": [
            {"missing_export_maps": ["opacity", "subsurface"]},
            {"missing_export_maps": []},
        ]}
        connected = {"cluster_items": [{
            "missing_export_maps": [],
            "connection_update_needed": True,
        }]}
        complete = {"cluster_items": [{"missing_export_maps": []}]}

        self.assertEqual(
            self.gui.App.step3_text(None, pending),
            "연결 텍스처 2세트 · 10/12장 · 2장 생성",
        )
        self.assertEqual(
            self.gui.App.step3_text(None, connected),
            "연결 텍스처 1세트 · 6장 완료 · 연결 정리",
        )
        self.assertEqual(
            self.gui.App.step3_text(None, complete),
            "연결 텍스처 1세트 · 6장 완료 ✓",
        )

    def test_step3_button_switches_to_unreal_sync_when_texture_sets_are_complete(self):
        def entries(row):
            return {"tree": {
                "checked": True,
                "item": {"cluster_items": [row]},
            }}

        pending = self.gui.step3_selection_state(entries({
            "missing_export_maps": ["subsurface"],
        }))
        connection = self.gui.step3_selection_state(entries({
            "missing_export_maps": [],
            "connection_update_needed": True,
        }))
        complete = self.gui.step3_selection_state(entries({
            "missing_export_maps": [],
        }))

        self.assertEqual(pending["state"], "normal")
        self.assertIn("누락 1장", pending["text"])
        self.assertEqual(connection["state"], "normal")
        self.assertIn("연결 정리", connection["text"])
        self.assertEqual(complete, {
            "text": "③ Unreal 동기화 — 완료 텍스처 확인 (1세트)",
            "state": "normal",
        })

    def test_detail_rows_use_audit_data_without_building_full_plan(self):
        item = {
            "name": "tree_test",
            "folder": r"D:\Tree\tree_test",
            "normal_convention": "OpenGL",
            "sbs_files": [r"D:\Tree\tree_test\tree_test.sbs"],
            "cluster_items": [{
                "atlas_base": "M_leaf_test",
                "texture_base": "T_leaf_test",
                "source_refs": [r"D:\Source\odd_name.tif"],
                "source_albedo": [r"D:\Source\exact_color_slot.tif"],
                "source_alpha": [r"D:\Source\exact_opacity_slot.tif"],
                "missing_export_maps": [],
            }],
        }
        with mock.patch.object(
                self.gui, "build_texture_plan_from_report",
                side_effect=AssertionError("selection must not build a full plan")):
            rows = self.gui.App._detail_texture_rows(item)

        self.assertEqual(rows[0]["source_albedo"], item["cluster_items"][0]["source_albedo"])
        self.assertEqual(rows[0]["source_alpha"], item["cluster_items"][0]["source_alpha"])
        self.assertEqual(rows[0]["normal_convention"], "OpenGL")
        self.assertEqual(rows[0]["sbs_files"], item["sbs_files"])

    def test_first_row_click_after_select_all_keeps_only_that_row(self):
        class FakeTree:
            def __init__(self):
                self.labels = {}

            @staticmethod
            def identify_region(_x, _y):
                return "tree"

            @staticmethod
            def identify_row(y):
                return y

            def item(self, iid, **values):
                self.labels[iid] = values.get("text")

        app = self.gui.App.__new__(self.gui.App)
        app.tree = FakeTree()
        app.items = {
            name: {"item": {"name": name}, "checked": True}
            for name in ("a", "b", "c")
        }
        app.checked_rows = self.gui.CheckedRowController(
            app.items, app._redraw_checked_row
        )
        app.checked_rows.sync_after_reload()
        event = type("Event", (), {"x": 0, "y": "b"})()

        app._on_click(event)
        self.assertEqual(
            {name: row["checked"] for name, row in app.items.items()},
            {"a": False, "b": True, "c": False},
        )
        self.assertFalse(app.checked_rows.armed)

        event.y = "c"
        app._on_click(event)
        self.assertTrue(app.items["b"]["checked"])
        self.assertTrue(app.items["c"]["checked"])

        app._set_all(True)
        self.assertTrue(app.checked_rows.armed)
        event.y = "a"
        app._on_click(event)
        self.assertEqual(
            {name: row["checked"] for name, row in app.items.items()},
            {"a": True, "b": False, "c": False},
        )

    def test_first_spm_click_after_select_all_keeps_only_exact_spm(self):
        class FakeTree:
            def __init__(self):
                self.labels = {}

            @staticmethod
            def identify_region(_x, _y):
                return "tree"

            @staticmethod
            def identify_row(y):
                return y

            def item(self, iid, **values):
                self.labels[iid] = values.get("text")

            def selection_set(self, _iid):
                pass

            def focus(self, _iid):
                pass

            def focus_set(self):
                pass

        app = self.gui.App.__new__(self.gui.App)
        app.tree = FakeTree()
        app.items = {
            "folder_a": {"item": {"name": "a"}, "checked": True},
            "folder_b": {"item": {"name": "b"}, "checked": True},
        }
        app.target_items = {
            "a1": {
                "folder_iid": "folder_a", "display_name": "SK_a1.spm",
                "checked": True,
            },
            "a2": {
                "folder_iid": "folder_a", "display_name": "SK_a2.spm",
                "checked": True,
            },
            "b1": {
                "folder_iid": "folder_b", "display_name": "SK_b1.spm",
                "checked": True,
            },
        }
        app.checked_rows = self.gui.CheckedRowController(
            app.items, app._redraw_checked_row)
        app.target_checked_rows = self.gui.CheckedRowController(
            app.target_items, app._redraw_target_checked_row)
        app.checked_rows.sync_after_reload()
        app.target_checked_rows.sync_after_reload()
        event = type("Event", (), {"x": 0, "y": "a2"})()

        app._on_click(event)

        self.assertEqual(
            {name: row["checked"] for name, row in app.target_items.items()},
            {"a1": False, "a2": True, "b1": False},
        )
        self.assertTrue(app.items["folder_a"]["checked"])
        self.assertFalse(app.items["folder_b"]["checked"])

    def test_folder_click_selects_all_spms_in_only_that_folder(self):
        class FakeTree:
            def __init__(self):
                self.labels = {}

            @staticmethod
            def identify_region(_x, _y):
                return "tree"

            @staticmethod
            def identify_row(y):
                return y

            def item(self, iid, **values):
                self.labels[iid] = values.get("text")

        app = self.gui.App.__new__(self.gui.App)
        app.tree = FakeTree()
        app.items = {
            "folder_a": {"item": {"name": "a"}, "checked": True},
            "folder_b": {"item": {"name": "b"}, "checked": True},
        }
        app.target_items = {
            "a1": {
                "folder_iid": "folder_a", "display_name": "SK_a1.spm",
                "checked": True,
            },
            "a2": {
                "folder_iid": "folder_a", "display_name": "SK_a2.spm",
                "checked": True,
            },
            "b1": {
                "folder_iid": "folder_b", "display_name": "SK_b1.spm",
                "checked": True,
            },
        }
        app.checked_rows = self.gui.CheckedRowController(
            app.items, app._redraw_checked_row)
        app.target_checked_rows = self.gui.CheckedRowController(
            app.target_items, app._redraw_target_checked_row)
        app.checked_rows.sync_after_reload()
        app.target_checked_rows.sync_after_reload()
        event = type("Event", (), {"x": 0, "y": "folder_a"})()

        app._on_click(event)

        self.assertEqual(
            {name: row["checked"] for name, row in app.target_items.items()},
            {"a1": True, "a2": True, "b1": False},
        )

    def test_folder_row_exposes_concrete_spm_paths_for_copy(self):
        item = {
            "target_spm_statuses": [
                {"sk_spm": r"D:\Trees\oak\SK_oak_01.spm"},
                {
                    "sk_spm": "",
                    "source_spm": r"D:\Trees\oak\oak_02.spm",
                },
            ],
            "sk_spms": [r"D:\Trees\oak\ignored_when_status_exists.spm"],
        }
        self.assertEqual(
            self.gui.spm_paths_for_item(item),
            [r"D:\Trees\oak\SK_oak_01.spm", r"D:\Trees\oak\oak_02.spm"],
        )

    def test_step2_blend_is_not_complete_until_generators_are_connected(self):
        pending = {"leaf_mesh_sources": [{
            "atlas_blends": ["M_leaf_test_atlas_01.blend"],
            "generator_connection_complete": False,
            "targets": [{
                "spm": "SK_test.spm",
                "source_material_names": ["M_leaf_test_01"],
                "generator_connection_complete": False,
            }],
        }]}
        complete = {"leaf_mesh_sources": [{
            **pending["leaf_mesh_sources"][0],
            "generator_connection_complete": True,
            "targets": [{
                **pending["leaf_mesh_sources"][0]["targets"][0],
                "generator_connection_complete": True,
            }],
        }]}

        self.assertEqual(
            self.gui.App.step2_text(None, pending),
            "작업 필요 · 기존 매쉬 연결 1세트 · 적용 모델 1개",
        )
        self.assertEqual(
            self.gui.App.step2_text(None, complete),
            "현재 잎 매쉬 1세트 · 연결 완료 ✓",
        )

    def test_current_inventory_overrides_stale_blend_target_connection(self):
        blend = r"D:\Trees\atlas\M_leaf_test_atlas_01.blend"
        spm = r"D:\Trees\tree_test\SK_tree_test.spm"
        item = {
            "leaf_mesh_sources": [{
                "atlas_blends": [blend],
                "targets": [{
                    "spm": spm,
                    "generator_connection_complete": False,
                }],
            }],
            "leaf_atlas_inventory": [{
                "atlas_blends": [blend],
                "export_participating": True,
                "targets": [{
                    "spm": spm,
                    "generator_connection_complete": True,
                }],
            }],
        }

        rows = self.gui.blender_connection_rows(item)

        self.assertEqual(len(rows), 1)
        self.assertEqual(len(rows[0]["spms"]), 1)
        self.assertTrue(rows[0]["spms"][0]["connected"])
        self.assertEqual(
            self.gui.blender_connection_summary(rows[0]),
            "연결 SPM 1개 · 연결 완료 ✓",
        )

    def test_populate_inserts_collapsed_real_blend_with_spm_children(self):
        class FakeTree:
            def __init__(self):
                self.insertions = []

            def get_children(self):
                return ()

            def delete(self, _iid):
                raise AssertionError("empty fake tree must not delete rows")

            def insert(self, parent, index, **kwargs):
                self.insertions.append((parent, index, kwargs))
                return kwargs.get("iid")

        blend = Path(r"D:\Trees\atlas\M_leaf_test_atlas_01.blend")
        spm = Path(r"D:\Trees\tree_test\SK_tree_test.spm")
        item = {
            "folder": r"D:\Trees\tree_test",
            "name": "tree_test",
            "actions": [],
            "target_spm_statuses": [{
                "mesh_name": "tree_test",
                "source_spm": r"D:\Trees\tree_test\tree_test.spm",
                "sk_spm": str(spm),
                "status": "ready",
            }],
            "leaf_mesh_sources": [{
                "atlas_blends": [str(blend)],
                "targets": [{
                    "spm": str(spm),
                    "generator_connection_complete": True,
                }],
            }],
        }
        app = self.gui.App.__new__(self.gui.App)
        app.report = {"items": [item]}
        app.items = {}
        app.row_copy_paths = {}
        app.tree = FakeTree()
        app.checked_rows = self.gui.CheckedRowController(
            app.items, lambda _iid, _entry: None
        )
        app._annotate_unreal_sync = lambda _item: None
        app._target_row_tag = lambda _item: ""
        app._target_source_text = lambda _item: ""
        app.step1_text = lambda _item: ""
        app.step2_text = lambda _item: ""
        app.step3_text = lambda _item: ""
        app._update_step3_button = lambda: None

        app.populate()

        by_iid = {
            kwargs["iid"]: (parent, kwargs)
            for parent, _index, kwargs in app.tree.insertions
        }
        blend_iid = item["folder"] + "::blend::0"
        spm_iid = blend_iid + "::spm::0"
        target_iid = item["folder"] + "::target::0"
        self.assertEqual(by_iid[target_iid][0], item["folder"])
        self.assertEqual(by_iid[target_iid][1]["text"], f"☑ {spm.name}")
        self.assertEqual(app.target_items[target_iid]["mesh"], "tree_test")
        self.assertEqual(app.row_copy_paths[target_iid], [spm])
        self.assertEqual(by_iid[blend_iid][1]["text"], f"◆ {blend.name}")
        self.assertFalse(by_iid[blend_iid][1]["open"])
        self.assertEqual(
            by_iid[item["folder"]][1]["values"][4],
            "blend 1개 · 연결 완료 ✓",
        )
        self.assertEqual(
            by_iid[blend_iid][1]["values"][4],
            "연결 SPM 1개 · 연결 완료 ✓",
        )
        self.assertEqual(by_iid[spm_iid][0], blend_iid)
        self.assertEqual(by_iid[spm_iid][1]["text"], f"↳ {spm.name}")
        self.assertNotIn(
            "Blender", [kwargs["text"] for _, _, kwargs in app.tree.insertions]
        )
        self.assertEqual(app.row_copy_paths[blend_iid], [blend])
        self.assertEqual(app.row_copy_paths[spm_iid], [spm])

    def test_step2_explicit_complete_targets_override_stale_source_aggregate(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            blend = root / "M_leaf_test_atlas_01.blend"
            blend.write_bytes(b"BLENDER")
            source = {
                "atlas_base": "M_leaf_test_atlas_01",
                "atlas_blends": [str(blend)],
                "generator_connection_complete": False,
                "generator_connection_update_needed": True,
                "targets": [{
                    "spm": str(root / "SK_tree_test.spm"),
                    "source_material_names": ["M_leaf_test"],
                    "generator_connection_complete": True,
                }],
            }
            app = self.gui.App.__new__(self.gui.App)
            app.cfg = {"atlas_root": str(root)}
            app.items = {"tree_test": {
                "checked": True,
                "item": {"name": "tree_test", "leaf_mesh_sources": [source]},
            }}

            state = self.gui.leaf_source_step2_state(source)
            pending = self.gui.pending_leaf_targets(source)
            jobs, skipped = app._step2_jobs(connect_spm=True)

        self.assertTrue(state["connection_complete"])
        self.assertTrue(state["complete"])
        self.assertEqual(pending, [])
        self.assertEqual(jobs, [])
        self.assertEqual(skipped, [])

    def test_prepare_noop_is_displayed_as_no_change(self):
        class FakeTree:
            def __init__(self):
                self.values = []

            def set(self, iid, column, value):
                self.values.append((iid, column, value))

        app = self.gui.App.__new__(self.gui.App)
        app.tree = FakeTree()
        app.log = mock.Mock()
        app._prepare_finished = mock.Mock()
        app._ui = lambda fn: fn()
        row = {
            "item": {"folder": r"D:\Trees\tree_test", "name": "tree_test"},
            "mesh": "tree_test",
        }
        result = {"targets": [{
            "status": "up_to_date",
            "created": None,
            "patch": {"changed": False, "renames": []},
        }]}

        with mock.patch.object(self.gui, "prepare_sk", return_value=result):
            app._run_prepare([row])

        self.assertEqual(app.tree.values, [(
            r"D:\Trees\tree_test", "step1", "완료 ✓ (변경 없음)",
        )])
        app.log.assert_called_once_with(
            "[① 변경 없음] tree_test: 이미 최신입니다.")
        app._prepare_finished.assert_called_once_with(1, 0)

    def test_prepare_rows_drop_stale_audit_when_preview_is_up_to_date(self):
        app = self.gui.App.__new__(self.gui.App)
        app.items = {"tree_test": {
            "checked": True,
            "item": {
                "folder": r"D:\Trees\tree_test",
                "name": "tree_test",
                "target_spm_statuses": [{
                    "mesh_name": "tree_test",
                    "status": "needs_m_prefix",
                }],
            },
        }}
        preview = {"targets": [{
            "status": "up_to_date",
            "would_create": None,
            "patch": {"dry_run": True, "renames": []},
        }]}

        with mock.patch.object(self.gui, "prepare_sk", return_value=preview):
            rows = app._build_prepare_rows()

        self.assertEqual(rows, [])

    def test_prepare_rows_use_only_checked_exact_spm_targets(self):
        item = {
            "folder": r"D:\Trees\tree_elm",
            "name": "tree_elm",
            "duplicate_target_mesh_names": [],
        }
        first_status = {"mesh_name": "tree_elm_01", "status": "needs_sk"}
        second_status = {"mesh_name": "tree_elm_02", "status": "needs_sk"}
        app = self.gui.App.__new__(self.gui.App)
        app.items = {item["folder"]: {"checked": True, "item": item}}
        app.target_items = {
            "first": {
                "checked": False,
                "item": item,
                "target_status": first_status,
            },
            "second": {
                "checked": True,
                "item": item,
                "target_status": second_status,
            },
        }
        preview = {"targets": [{
            "status": "dry-run",
            "would_create": r"D:\Trees\tree_elm\SK_Tree_elm_02.spm",
            "patch": {"dry_run": True, "renames": []},
        }]}

        with mock.patch.object(
                self.gui, "prepare_sk", return_value=preview) as prepare:
            rows = app._build_prepare_rows()

        self.assertEqual([row["mesh"] for row in rows], ["tree_elm_02"])
        prepare.assert_called_once_with(
            item["folder"], ["tree_elm_02"], dry_run=True)

    def test_step2_reuses_existing_blend_and_passes_exact_target_mapping(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            atlas = root / "atlas"
            atlas.mkdir()
            blend = atlas / "M_leaf_parsley_atlas_02.blend"
            blend.write_bytes(b"BLENDER")
            spm = root / "SK_weed_parsley_01.spm"
            spm.write_bytes(b"SPM")
            item = {
                "name": "weed_parsley",
                "leaf_mesh_sources": [{
                    "atlas_base": "M_leaf_parsley_atlas_02",
                    "albedo": root / "missing_albedo.tga",
                    "alpha": root / "missing_alpha.tga",
                    "atlas_blends": [str(blend)],
                    "generator_connection_complete": False,
                    "targets": [{
                        "spm": str(spm),
                        "material_names": ["legacy_name_is_not_used"],
                        "source_material_names": ["M_leaf_parsley_02"],
                        "source_material_ids": [4],
                        "generator_bindings": [{"material_id": 4, "mesh_id": 6}],
                        "generator_connection_complete": False,
                    }],
                }],
            }
            app = self.gui.App.__new__(self.gui.App)
            app.cfg = {"atlas_root": str(atlas)}
            app.items = {"weed_parsley": {"checked": True, "item": item}}

            jobs, skipped = app._step2_jobs(connect_spm=True)

        self.assertEqual(skipped, [])
        self.assertEqual(len(jobs), 1)
        self.assertTrue(jobs[0]["reuse_existing_blend"])
        self.assertEqual(jobs[0]["blend_out"], blend)
        payload = self.gui.step2_target_payload(jobs[0])
        self.assertEqual(payload["version"], 1)
        self.assertEqual(payload["targets"], [{
            "spm": str(spm),
            "source_material_names": ["M_leaf_parsley_02"],
            "source_material_ids": [4],
            "generator_bindings": [{"material_id": 4, "mesh_id": 6}],
        }])

    def test_step2_preflight_reports_missing_source_before_blender(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            atlas = root / "atlas"
            atlas.mkdir()
            item = {
                "name": "weed_ladyfern",
                "leaf_mesh_sources": [{
                    "atlas_base": "M_leaf_ladyfern_atlas_01",
                    "albedo": root / "missing_albedo.tga",
                    "alpha": root / "missing_alpha.tga",
                    "atlas_blends": [],
                    "targets": [],
                }],
            }
            app = self.gui.App.__new__(self.gui.App)
            app.cfg = {"atlas_root": str(atlas)}
            app.items = {"weed_ladyfern": {"checked": True, "item": item}}

            jobs, skipped = app._step2_jobs(connect_spm=True)

        self.assertEqual(jobs, [])
        self.assertEqual(len(skipped), 1)
        self.assertIn("알베도/알파 원본 파일 없음", skipped[0][2])

    def test_step2_does_not_count_unverified_generator_result_as_success(self):
        with self.assertRaisesRegex(RuntimeError, "Generator Material/Mesh 연결 검증"):
            self.gui.validate_step2_job_report({
                "status": "ok",
                "spm_built": True,
                "generator_connections_complete": False,
            }, require_generator_connections=True)
        self.gui.validate_step2_job_report({
            "status": "ok",
            "spm_built": True,
            "generator_connections_complete": True,
        }, require_generator_connections=True)

    def test_step2_copy_no_longer_claims_generators_are_not_connected(self):
        source = (TOOL_DIR / "pcg_texture_gui.pyw").read_text(encoding="utf-8")
        self.assertNotIn("Leaf Mesh Generator에는 자동 연결하지 않습니다", source)
        self.assertIn("Generator 연결", source)


class SafetyTests(unittest.TestCase):
    def _pre_cluster_normalization_fixture(self, source, graphs, *, broken=False):
        """Use the transactional backup when the live integration source is normalized."""
        candidates = sorted(source.parent.glob(
            f"{source.stem}.pcgtex_backup_before_cluster_normalize_*.sbs"
        ))
        candidates.append(source)
        for candidate in candidates:
            try:
                states = [
                    sbs_auto.graph_cluster_normalization_state(candidate, graph)
                    for graph in graphs
                ]
            except Exception:
                continue
            if broken:
                if all(not state["integrity"]["valid"] for state in states):
                    return candidate
            elif all(not state["fully_normalized"] for state in states):
                return candidate
        self.skipTest("No pre-Cluster-normalization integration fixture is available")

    def test_export_params_disable_color_mask_baking(self):
        params = sbs_auto.normalized_export_params({
            "AO_blend": ("constantValueFloat1", "0.5"),
            "Height_blend": ("constantValueFloat1", "0.25"),
            "Leaf_hue": ("constantValueFloat1", "0.6"),
        })
        for name in sbs_auto.COLOR_PASSTHROUGH_PARAMS:
            self.assertEqual(params[name], ("constantValueFloat1", "0"))
        self.assertEqual(params["Leaf_hue"], ("constantValueFloat1", "0.6"))

    def test_managed_resolution_normalizes_final_cluster_wrapper_size(self):
        graph = ET.fromstring('''<graph>
<compNodes>
  <compNode><compImplementation><compInstance>
    <path v="pkg:///Cluster_System_01?dependency=1" />
    <parameters><parameter><name v="outputsize" /><relativeTo v="2" />
      <paramValue><constantValueInt2 v="-2 -2" /></paramValue>
    </parameter></parameters>
  </compInstance></compImplementation></compNode>
  <compNode><compImplementation><compFilter><filter v="bitmap" />
    <parameters><parameter><name v="outputsize" /><relativeTo v="2" />
      <paramValue><constantValueInt2 v="10 10" /></paramValue>
    </parameter></parameters>
  </compFilter></compImplementation></compNode>
</compNodes>
<options><option><name v="defaultParentSize" /><value v="10x10" /></option></options>
</graph>''')

        self.assertTrue(sbs_auto._set_graph_resolution(graph, (12, 11)))
        output_sizes = [
            (
                param.find("relativeTo").get("v"),
                list(param.find("paramValue"))[0].get("v"),
            )
            for param in graph.iter("parameter")
            if param.find("name").get("v") == "outputsize"
        ]
        self.assertEqual(output_sizes, [("0", "12 11"), ("0", "12 11")])
        self.assertEqual(
            graph.find("options/option/value").get("v"), "12x11")

    def test_success_cleanup_deletes_only_exact_legacy_m_outputs(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            expected = []
            for map_name in sbs_auto.RENDER_MAPS:
                path = root / f"M_test_{map_name}.tga"
                path.write_bytes(b"legacy")
                expected.append(path)
            unrelated = root / "M_other_color.tga"
            derived = root / "M_test_hbao_from_height.tga"
            current = root / "T_test_color.tga"
            for path in (unrelated, derived, current):
                path.write_bytes(b"keep")
            deleted = sbs_auto.delete_legacy_m_outputs("M_test", root)
            self.assertEqual({path.name for path in deleted}, {path.name for path in expected})
            self.assertTrue(all(not path.exists() for path in expected))
            self.assertTrue(unrelated.exists())
            self.assertTrue(derived.exists())
            self.assertTrue(current.exists())

    def test_cleanup_accepts_explicit_common_alias_outputs(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            alias = root / "M_bark_common_locast_end_01_color.tga"
            alias.write_bytes(b"legacy")
            deleted = sbs_auto.delete_legacy_m_outputs(
                "M_bark_common_end_01", root,
                legacy_maps={"color": str(alias)})
            self.assertEqual(deleted, [alias])
            self.assertFalse(alias.exists())

    def test_output_transaction_identical_files_are_noop(self):
        with tempfile.TemporaryDirectory() as temp:
            out = Path(temp)
            produced = [out / f"T_test_{name}.tga" for name in ("color", "normal")]
            for path in produced:
                path.write_bytes(b"same")
            original_mtimes = [path.stat().st_mtime_ns for path in produced]
            transaction = sbs_auto._prepare_output_transaction(produced, "T_test")
            try:
                for staged in transaction["staged_files"]:
                    staged.write_bytes(b"same")
                info = sbs_auto._commit_output_transaction(transaction)
            finally:
                sbs_auto._restore_output_transaction(transaction)

            self.assertEqual(info["files"], produced)
            self.assertEqual(info["changed_files"], [])
            self.assertEqual(info["unchanged_files"], produced)
            self.assertEqual(info["created_files"], [])
            self.assertIsNone(info["backup_dir"])
            self.assertEqual(
                [path.stat().st_mtime_ns for path in produced], original_mtimes)
            self.assertFalse(transaction["staging_dir"].exists())
            self.assertFalse((out / "_pcgtex_backups").exists())

    def test_rendered_map_content_rejects_all_zero_normal(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            black_normal = root / "T_test_normal.tga"
            flat_normal = root / "T_test_flat_normal.tga"
            black_extra = root / "T_test_extra.tga"
            Image.new("RGB", (2, 2), (0, 0, 0)).save(black_normal)
            Image.new("RGB", (2, 2), (128, 128, 255)).save(flat_normal)
            Image.new("RGB", (2, 2), (0, 0, 0)).save(black_extra)

            self.assertIn(
                "all-zero RGB normal output",
                sbs_auto.rendered_map_content_error(black_normal, "normal"),
            )
            self.assertIsNone(
                sbs_auto.rendered_map_content_error(flat_normal, "normal"))
            self.assertIsNone(
                sbs_auto.rendered_map_content_error(black_extra, "extra"))

    def test_complete_output_set_rejects_existing_black_normal(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            texture_base = "T_test"
            for role in sbs_auto.RENDER_MAPS:
                color = (0, 0, 0) if role == "normal" else (128, 128, 128)
                Image.new("RGB", (2, 2), color).save(
                    root / f"{texture_base}_{role}.tga")
            row = {"texture_dir": str(root), "texture_base": texture_base}

            self.assertFalse(
                migrate_current_sk_textures.complete_output_set(
                    row, expected_pixels=(2, 2)))
            with self.assertRaisesRegex(
                    RuntimeError, "all-zero RGB normal output"):
                migrate_current_sk_textures.verify_complete_output_set(
                    root, texture_base, expected_pixels=(2, 2))

    def test_output_transaction_replaces_only_changed_and_new_files(self):
        with tempfile.TemporaryDirectory() as temp:
            out = Path(temp)
            same = out / "T_test_color.tga"
            changed = out / "T_test_normal.tga"
            created = out / "T_test_height.tga"
            same.write_bytes(b"same")
            changed.write_bytes(b"old")
            same_mtime = same.stat().st_mtime_ns
            transaction = sbs_auto._prepare_output_transaction(
                [same, changed, created], "T_test")
            try:
                for staged, payload in zip(
                        transaction["staged_files"], (b"same", b"new", b"created")):
                    staged.write_bytes(payload)
                info = sbs_auto._commit_output_transaction(transaction)
            finally:
                sbs_auto._restore_output_transaction(transaction)

            self.assertEqual(info["changed_files"], [changed, created])
            self.assertEqual(info["unchanged_files"], [same])
            self.assertEqual(info["created_files"], [created])
            self.assertEqual(same.stat().st_mtime_ns, same_mtime)
            self.assertEqual([same.read_bytes(), changed.read_bytes(), created.read_bytes()],
                             [b"same", b"new", b"created"])
            self.assertEqual(
                [path.name for path in info["backup_dir"].iterdir()],
                [changed.name],
            )

    def test_output_transaction_rolls_back_partial_commit(self):
        with tempfile.TemporaryDirectory() as temp:
            out = Path(temp)
            existing = out / "T_test_color.tga"
            created = out / "T_test_normal.tga"
            existing.write_bytes(b"old")
            transaction = sbs_auto._prepare_output_transaction(
                [existing, created], "T_test")
            for staged, payload in zip(
                    transaction["staged_files"], (b"new", b"created")):
                staged.write_bytes(payload)
            path_type = type(transaction["staged_files"][0])
            real_replace = path_type.replace
            calls = 0

            def fail_second(path, target):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("synthetic replace failure")
                return real_replace(path, target)

            try:
                with mock.patch.object(path_type, "replace", new=fail_second):
                    with self.assertRaisesRegex(OSError, "synthetic replace failure"):
                        sbs_auto._commit_output_transaction(transaction)
            finally:
                sbs_auto._restore_output_transaction(transaction)

            self.assertEqual(existing.read_bytes(), b"old")
            self.assertFalse(created.exists())

    def test_failed_renderer_restores_all_existing_outputs(self):
        real_sbsar = sbs_auto.cluster_sbsar()
        where_exe = Path(r"C:\Windows\System32\where.exe")
        if not real_sbsar.exists() or not where_exe.exists():
            self.skipTest("Windows renderer rollback fixture is unavailable")
        with tempfile.TemporaryDirectory() as temp:
            temp = Path(temp)
            fake_designer = temp / "designer"
            fake_designer.mkdir()
            shutil.copy2(where_exe, fake_designer / "sbsrender.exe")
            produced = [temp / f"T_fail_{name}.tga" for name in sbs_auto.RENDER_MAPS]
            for path in produced:
                path.write_bytes(b"old")
            cfg = {
                "designer_dir": str(fake_designer),
                "cluster_sbsar": str(real_sbsar),
                "cluster_sbsar_normal_behavior": "opengl_to_directx",
            }
            with self.assertRaisesRegex(RuntimeError, "sbsrender 실패"):
                sbs_auto.render_maps("T_fail", {}, {}, temp, cfg=cfg, size_log2=4, timeout=10)
            self.assertEqual(
                [path.read_bytes() for path in produced],
                [b"old"] * len(sbs_auto.RENDER_MAPS),
            )
            self.assertFalse((temp / "_pcgtex_backups").exists())
            self.assertFalse(any(temp.glob(".pcgtx_*")))

    def test_directx_normal_green_correction(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "normal.tga"
            Image.new("RGB", (1, 1), (100, 10, 200)).save(path)
            sbs_auto._invert_normal_green(path)
            self.assertEqual(Image.open(path).convert("RGB").getpixel((0, 0)), (100, 245, 200))

    def test_installed_hbao_package_cooks(self):
        if not sbs_auto.hbao_source_sbs().exists():
            self.skipTest("Designer HBAO package is not installed")
        self.assertTrue(sbs_auto.ensure_hbao_sbsar(timeout=60).exists())

    def test_hbao_graph_input_patch_uses_backup(self):
        source_sbs = Path(
            r"D:\OneDrive\Forestportfolio\02_nature\Tree\Tree_elm\texture\tree_elm_set_01.sbs"
        )
        if not source_sbs.exists():
            self.skipTest("Elm reference SBS is unavailable")
        with tempfile.TemporaryDirectory() as temp:
            temp = Path(temp)
            copied = temp / source_sbs.name
            shutil.copy2(source_sbs, copied)
            graph = (sbs_auto.find_m_graph_name(copied, "T_leaf_elm_atlas_01")
                     or sbs_auto.find_m_graph_name(copied, "M_Leaf_elm_atlas_01"))
            if not graph:
                self.skipTest("Elm managed leaf graph is unavailable")
            hbao = temp / "generated_hbao.png"
            Image.new("L", (2, 2), 128).save(hbao)
            result = sbs_auto.patch_m_graph_input_resource(
                copied, graph, "Ambient_Occlusion", hbao)
            self.assertTrue(Path(result["backup"]).exists())
            parsed = sbs_auto.parse_m_graph(copied, graph)
            self.assertEqual(parsed["inputs"]["Ambient_Occlusion"].resolve(), hbao.resolve())

    def test_graph_input_patch_does_not_redirect_shared_height_depth(self):
        source_dir = Path(
            r"D:\OneDrive\Forestportfolio\02_nature\Tree\weed_anamone\texture"
        )
        backups = sorted(source_dir.glob(
            "weed_anamone_set_01.pcgtex_backup_before_set_"
            "T_leaf_anamone_atlas_01_Ambient_Occlusion_*.sbs"
        ))
        if not backups:
            self.skipTest("Anamone shared-resource reference SBS is unavailable")
        with tempfile.TemporaryDirectory() as temp:
            temp = Path(temp)
            copied = temp / "shared_resource_test.sbs"
            shutil.copy2(backups[0], copied)
            graph = "T_leaf_anamone_atlas_01"
            before = sbs_auto.parse_m_graph(copied, graph)["inputs"]
            hbao = temp / "generated_hbao.png"
            Image.new("L", (2, 2), 128).save(hbao)

            result = sbs_auto.patch_m_graph_input_resource(
                copied, graph, "Ambient_Occlusion", hbao)
            after = sbs_auto.parse_m_graph(copied, graph)["inputs"]

            self.assertTrue(result["isolated"])
            self.assertEqual(after["Ambient_Occlusion"].resolve(), hbao.resolve())
            self.assertEqual(after["Height"].resolve(), before["Height"].resolve())
            self.assertEqual(after["Depth"].resolve(), before["Depth"].resolve())

    def test_legacy_m_graph_renames_to_t_graph_with_backup(self):
        source_sbs = Path(
            r"D:\OneDrive\Forestportfolio\Texture\bark\bark_common_end_01\bark_common_end_01.sbs"
        )
        if not source_sbs.exists():
            self.skipTest("Shared bark SBS is unavailable")
        with tempfile.TemporaryDirectory() as temp:
            copied = Path(temp) / source_sbs.name
            shutil.copy2(source_sbs, copied)
            legacy = next(
                (name for name in sbs_auto.list_m_graphs(copied)
                 if name.lower().startswith("m_")),
                None,
            )
            if not legacy:
                self.skipTest("No legacy M_ graph remains in shared bark SBS")
            target = "T_test_legacy_rename"
            result = sbs_auto.rename_managed_graph(
                copied, legacy, target,
            )
            self.assertTrue(Path(result["backup"]).exists())
            self.assertIn(target, sbs_auto.list_m_graphs(copied))
            self.assertNotIn(legacy, sbs_auto.list_m_graphs(copied))

    def test_procedural_authoring_graph_replaces_generated_t_clone(self):
        xml = '''<?xml version="1.0"?><package><content>
<graph><identifier v="M_test"/><compNodes /></graph>
<graph><identifier v="T_test"/><compNodes><compNode><path v="pkg:///Resources/T_test_albedo?dependency=1"/></compNode></compNodes></graph>
<group><identifier v="Resources"/><content>
<resource><identifier v="M_test_albedo"/><filepath v="source.png"/></resource>
<resource><identifier v="T_test_albedo"/><filepath v="clone.png"/></resource>
</content></group></content></package>'''
        with tempfile.TemporaryDirectory() as temp:
            sbs = Path(temp) / "test.sbs"
            sbs.write_text(xml, encoding="utf-8")
            result = sbs_auto.promote_authoring_graph(sbs, "M_test", "T_test")
            self.assertTrue(Path(result["backup"]).exists())
            self.assertEqual(sbs_auto.list_m_graphs(sbs), ["T_test"])
            text = sbs.read_text(encoding="utf-8")
            self.assertIn("T_test_albedo", text)
            self.assertNotIn("M_test_albedo", text)
            self.assertNotIn("clone.png", text)

    def test_broken_authoring_graph_never_replaces_intact_managed_graph(self):
        with mock.patch.object(
                sbs_auto, "exact_graph_name",
                side_effect=["M_test", "T_test"]), \
                mock.patch.object(
                    sbs_auto, "graph_cluster_normalization_state",
                    return_value={"integrity": {"valid": False}}), \
                mock.patch.object(
                    sbs_auto, "inspect_graph_sources") as inspect_sources:
            candidate = sbs_auto.authoring_graph_promotion_candidate(
                "test.sbs", "M_test", "T_test")

        self.assertIsNone(candidate)
        inspect_sources.assert_not_called()

    def test_procedural_graph_routes_hidden_alpha_through_cluster(self):
        source = Path(
            r"D:\OneDrive\Forestportfolio\02_nature\Tree\weed_deadleaves\texture\substance\weed_deadleaves_texture_set_01.sbs"
        )
        graph = "T_Leaf_deadleaves_atlas_01"
        if not source.exists():
            self.skipTest("Deadleaves procedural SBS is unavailable")
        fixture = self._pre_cluster_normalization_fixture(source, [graph])
        with tempfile.TemporaryDirectory() as temp:
            copied = Path(temp) / source.name
            shutil.copy2(fixture, copied)
            result = sbs_auto.normalize_graph_through_cluster(copied, graph)
            graph_result = result["graphs"][0]
            self.assertTrue(result["changed"])
            self.assertEqual(
                graph_result["input_sources"]["Opacity"]["source_kind"],
                "color_merge_alpha",
            )
            state = sbs_auto.graph_cluster_normalization_state(copied, graph)
            self.assertTrue(state["fully_normalized"])
            self.assertTrue(state["wrapped_direct"])
            self.assertTrue(all(state["standard_outputs_through_cluster"].values()))
            self.assertNotIn(
                "normal", sbs_auto.parse_m_graph(copied, graph)["params"])
            second = sbs_auto.normalize_graph_through_cluster(copied, graph)
            self.assertFalse(second["changed"])

    def test_broken_stem_clones_rebuild_and_normalize_as_one_sbs_transaction(self):
        source = Path(r"D:\OneDrive\Forestportfolio\Texture\stem\stem_common_01.sbs")
        graphs = ["T_stem_01", "T_stem_common_03", "T_stem_common_04"]
        if not source.exists():
            self.skipTest("Shared stem procedural SBS is unavailable")
        fixture = self._pre_cluster_normalization_fixture(
            source, graphs, broken=True)
        with tempfile.TemporaryDirectory() as temp:
            copied = Path(temp) / source.name
            shutil.copy2(fixture, copied)
            self.assertFalse(
                sbs_auto.graph_cluster_normalization_state(
                    copied, graphs[0])["integrity"]["valid"])

            result = sbs_auto.normalize_graphs_through_cluster(copied, graphs)

            self.assertEqual(
                [row["repaired_from"] for row in result["graphs"]],
                ["stem_common_01", "stem_common_03", "stem_common_04"],
            )
            for graph in graphs:
                state = sbs_auto.graph_cluster_normalization_state(copied, graph)
                self.assertTrue(state["integrity"]["valid"])
                self.assertTrue(state["fully_normalized"])
                self.assertTrue(state["wrapped_direct"])
            by_graph = {row["graph"]: row for row in result["graphs"]}
            self.assertEqual(
                by_graph["T_stem_common_03"]["input_sources"]["Subsurface"]["identifier"],
                "scatteringcolor",
            )
            self.assertEqual(
                by_graph["T_stem_common_04"]["input_sources"]["Subsurface_Amount"]["identifier"],
                "translucency",
            )
            self.assertFalse(
                sbs_auto.normalize_graphs_through_cluster(copied, graphs)["changed"])

    def test_legacy_packed_bark_outputs_are_unpacked_into_cluster_inputs(self):
        source = Path(
            r"D:\OneDrive\Forestportfolio\02_nature\Tree\tree_densiflora\texture\tree_densiflora_set_01.sbs"
        )
        graph = "T_bark_densiflora_01"
        if not source.exists():
            self.skipTest("Densiflora procedural SBS is unavailable")
        fixture = self._pre_cluster_normalization_fixture(source, [graph])
        with tempfile.TemporaryDirectory() as temp:
            copied = Path(temp) / source.name
            shutil.copy2(fixture, copied)

            result = sbs_auto.normalize_graph_through_cluster(copied, graph)

            graph_result = result["graphs"][0]
            sources = graph_result["input_sources"]
            self.assertEqual(sources["Base_Color"]["identifier"], "color")
            self.assertEqual(sources["Ambient_Occlusion"]["identifier"], "extra.R")
            self.assertEqual(sources["Roughness"]["identifier"], "extra.G")
            self.assertEqual(sources["Height"]["identifier"], "extra.B")
            self.assertEqual(
                sources["Normal"]["source_kind"], "unique_height_to_normal")
            self.assertEqual(
                graph_result["removed_duplicate_outputs"], ["color_1", "extra_1"])
            state = sbs_auto.graph_cluster_normalization_state(copied, graph)
            self.assertTrue(state["fully_normalized"])
            self.assertTrue(state["integrity"]["valid"])


class SharedTextureContractConsumptionTests(unittest.TestCase):
    REQUIRED = ("color", "normal", "extra", "height", "opacity", "subsurface")

    def _write_set(self, directory, texture_base, roles=None):
        directory.mkdir(parents=True, exist_ok=True)
        for role in roles or self.REQUIRED:
            (directory / f"{texture_base}_{role}.png").write_bytes(
                role.encode("ascii")
            )

    def test_find_export_maps_multi_prefers_later_complete_t_set(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            partial = root / "a_partial"
            complete = root / "z_complete"
            self._write_set(partial, "T_Shared_01", ("color", "normal"))
            self._write_set(complete, "T_Shared_01")

            selected_dir, maps = pcg_texture_audit.find_export_maps_multi(
                [partial, complete], "T_Shared_01", self.REQUIRED
            )

            self.assertEqual(Path(selected_dir), complete.resolve())
            self.assertEqual(list(maps), list(self.REQUIRED))
            self.assertTrue(all(maps.values()))
            self.assertTrue(
                all(Path(path).parent == complete.resolve() for path in maps.values())
            )

    def test_material_labels_do_not_infer_managed_texture_or_instance_variants(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._write_set(root, "T_Shared_01")

            for material_label in ("Green", "Yellow", "M_Shared_01"):
                selected_dir, maps = pcg_texture_audit.find_export_maps_multi(
                    [root], material_label, self.REQUIRED
                )
                self.assertEqual(Path(selected_dir), root)
                self.assertEqual(list(maps), list(self.REQUIRED))
                self.assertTrue(all(path is None for path in maps.values()))


if __name__ == "__main__":
    unittest.main()
