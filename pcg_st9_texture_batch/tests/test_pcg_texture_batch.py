import gzip
import importlib.machinery
import importlib.util
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image


TOOL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOL_DIR))

import sbs_auto
import pcg_texture_audit
from export_texture_plan import build_texture_plan_from_report, extract_material_image_refs
from pcg_texture_audit import (
    assign_leaf_atlas_bases,
    atlas_base_from_cluster_stem,
    canonicalize_leaf_sources,
    canonical_material_name,
    cluster_spms,
    discover_leaf_mesh_sources,
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
            status = pcg_texture_audit.target_spm_status(root, "test")
            self.assertEqual(status["materials_missing_m_prefix"], [])
            self.assertEqual(status["material_renames_needed"], [])
            self.assertEqual(status["status"], "needs_blend")

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
                    pcg_texture_audit.active_material_ids(spm), {"0", "1"})
                self.assertEqual(
                    pcg_texture_audit.active_material_names(spm), ["M_shown"])
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
            spm_jobs = jobs_from_texture_plan({"items": items})
            self.assertEqual(len(spm_jobs), 1)
            self.assertEqual(
                set(spm_jobs[0]["materials"]),
                {"m_leaf_chestnut_atlas_01_green_light",
                 "m_leaf_chestnut_atlas_01_yellow"},
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

    def test_target_column_uses_readable_source_names(self):
        self.assertEqual(
            self.gui.App._target_source_text({
                "pcg_mesh_names": ["a", "b"],
                "level_mesh_names": ["c"],
            }),
            "PCG 2 / 레벨 1",
        )

    def test_prefixed_default_material_is_not_a_step1_warning(self):
        normal, generic = self.gui.split_generic(["M_Material 2"])
        self.assertEqual(normal, [])
        self.assertEqual(generic, [])

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
            self.gui.App.step2_text(None, complete), "잎 매쉬 2개 완료 ✓")
        self.assertEqual(
            self.gui.App.step2_text(None, partial),
            "잎 매쉬 1/2개 완료 · 1개 만들기",
        )

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


class SafetyTests(unittest.TestCase):
    def test_export_params_disable_color_mask_baking(self):
        params = sbs_auto.normalized_export_params({
            "AO_blend": ("constantValueFloat1", "0.5"),
            "Height_blend": ("constantValueFloat1", "0.25"),
            "Leaf_hue": ("constantValueFloat1", "0.6"),
        })
        for name in sbs_auto.COLOR_PASSTHROUGH_PARAMS:
            self.assertEqual(params[name], ("constantValueFloat1", "0"))
        self.assertEqual(params["Leaf_hue"], ("constantValueFloat1", "0.6"))

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

    def test_output_transaction_restores_old_files(self):
        with tempfile.TemporaryDirectory() as temp:
            out = Path(temp)
            produced = [out / f"T_test_{name}.tga" for name in ("color", "normal")]
            for path in produced:
                path.write_bytes(b"old")
            existing, backup_dir = sbs_auto._prepare_output_transaction(produced, "M_test")
            self.assertTrue(backup_dir.exists())
            self.assertTrue(all(not path.exists() for path in produced))
            produced[0].write_bytes(b"partial")
            sbs_auto._restore_output_transaction(produced, existing, backup_dir)
            self.assertEqual([path.read_bytes() for path in produced], [b"old", b"old"])

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
            backup_sets = list((temp / "_pcgtex_backups").iterdir())
            self.assertEqual(len(backup_sets), 1)
            self.assertEqual(
                len(list(backup_sets[0].glob("*.tga"))),
                len(sbs_auto.RENDER_MAPS),
            )

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


if __name__ == "__main__":
    unittest.main()
