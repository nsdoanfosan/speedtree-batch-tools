import gzip
import importlib.machinery
import importlib.util
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from PIL import Image


TOOL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOL_DIR))

import sbs_auto
from export_texture_plan import build_texture_plan_from_report, extract_material_image_refs
from pcg_texture_audit import (
    cluster_spms,
    discover_leaf_mesh_sources,
    target_mesh_source_map,
)
from refresh_pcg_targets import payload


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
    def _image(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (2, 2), (128, 128, 128)).save(path)
        return str(path)

    def test_source_family_key_removes_map_and_resolution_suffixes(self):
        path = Path(r"D:\Texture\Elm\TCom_Leaves_Elm01_4K_albedo.tif")
        self.assertEqual(sbs_auto.source_family_key(path), "tcom_leaves_elm01")

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

    def test_cluster_leaf_sources_are_deduplicated_across_spms(self):
        def write_spm(path, materials):
            blocks = []
            for index, (name, refs) in enumerate(materials, 1):
                tex = "".join(f"<TexFilename>{ref}</TexFilename>" for ref in refs)
                blocks.append(
                    f'<Material_v8 ID="{index}" Name="{name}">{tex}</Material_v8>')
            xml = ("<?xml version=\"1.0\"?><SpeedTree><Materials>"
                   + "".join(blocks) + "</Materials></SpeedTree>").encode()
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
                {cluster_a.name, cluster_b.name},
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


class SafetyTests(unittest.TestCase):
    def test_output_transaction_restores_old_files(self):
        with tempfile.TemporaryDirectory() as temp:
            out = Path(temp)
            produced = [out / f"M_test_{name}.tga" for name in ("color", "normal")]
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
            produced = [temp / f"M_fail_{name}.tga" for name in sbs_auto.RENDER_MAPS]
            for path in produced:
                path.write_bytes(b"old")
            cfg = {
                "designer_dir": str(fake_designer),
                "cluster_sbsar": str(real_sbsar),
                "cluster_sbsar_normal_behavior": "opengl_to_directx",
            }
            with self.assertRaisesRegex(RuntimeError, "sbsrender 실패"):
                sbs_auto.render_maps("M_fail", {}, {}, temp, cfg=cfg, size_log2=4, timeout=10)
            self.assertEqual([path.read_bytes() for path in produced], [b"old"] * 5)
            backup_sets = list((temp / "_pcgtex_backups").iterdir())
            self.assertEqual(len(backup_sets), 1)
            self.assertEqual(len(list(backup_sets[0].glob("*.tga"))), 5)

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
            hbao = temp / "generated_hbao.png"
            Image.new("L", (2, 2), 128).save(hbao)
            result = sbs_auto.patch_m_graph_input_resource(
                copied, "M_Leaf_elm_atlas_01", "Ambient_Occlusion", hbao)
            self.assertTrue(Path(result["backup"]).exists())
            parsed = sbs_auto.parse_m_graph(copied, "M_Leaf_elm_atlas_01")
            self.assertEqual(parsed["inputs"]["Ambient_Occlusion"].resolve(), hbao.resolve())


if __name__ == "__main__":
    unittest.main()
