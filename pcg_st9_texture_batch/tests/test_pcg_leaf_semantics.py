import gzip
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path


TOOL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOL_DIR))

import pcg_texture_audit as audit
from speedtree_legacy_cluster_contract import (
    RECEIPT_KIND,
    RECEIPT_VERSION,
    marker_receipt_path,
)


LADYFERN = Path(
    r"D:\OneDrive\Forestportfolio\02_nature\Tree\weed_ladyfern")
PARSLEY = Path(
    r"D:\OneDrive\Forestportfolio\02_nature\Tree\weed_parsley")


def material(material_id, name, refs, meshes=(), managed=False):
    refs_xml = "".join(
        f"<TexFilename>{value}</TexFilename>" for value in refs)
    mesh_xml = ""
    if meshes:
        mesh_xml = f"<CutoutMeshID>{meshes[0]}</CutoutMeshID>"
        mesh_xml += '<SupplementalCutoutMeshIDs Count="{}">{}</SupplementalCutoutMeshIDs>'.format(
            max(0, len(meshes) - 1),
            "".join(f'<CutoutMesh ID="{value}"/>' for value in meshes[1:]),
        )
    marker = (
        '<UserData>{"generator":"Atlas Leaf Mesh Builder",'
        '"kind":"material","scope":"test"}</UserData>'
        if managed else ""
    )
    return (
        f'<Material_v8 ID="{material_id}" Name="{name}">'
        f"{refs_xml}{mesh_xml}{marker}</Material_v8>"
    )


def generator(generator_type, material_id, mesh_id, index=0, hidden=False):
    return (
        f'<Generator Type="{generator_type}"><Name>Generator {index}</Name>'
        f'<GUID>guid-{index}</GUID><Hidden>{str(hidden).lower()}</Hidden>'
        '<Properties>'
        f'<Property><Name>Leaves:Type:{index}:Material</Name>'
        f'<Value>{material_id}</Value></Property>'
        f'<Property><Name>Leaves:Type:{index}:Mesh</Name>'
        f'<Value>{mesh_id}</Value></Property>'
        '</Properties></Generator>'
    )


def write_spm(path, materials, generators, mesh_ids=None):
    if mesh_ids is None:
        mesh_ids = re.findall(
            r'(?:<CutoutMeshID>|<CutoutMesh\s+ID=")(-?\d+)',
            "".join(materials),
        )
    meshes = "".join(
        f'<Mesh ID="{value}" Name="mesh-{value}"/>'
        for value in dict.fromkeys(str(value) for value in mesh_ids)
        if value not in {"", "-1", "-10"}
    )
    payload = (
        "<SpeedTree><Materials>" + "".join(materials)
        + "</Materials><Meshes>" + meshes
        + "</Meshes><Generators>" + "".join(generators)
        + "</Generators></SpeedTree>"
    ).encode()
    with gzip.open(path, "wb") as handle:
        handle.write(payload)


def image(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"fixture")
    return str(path)


def write_connection_manifest(spm, atlas_base, bindings,
                              source_ids=("4",),
                              source_names=("M_leaf_source",)):
    scopes = spm.parent / ".atlas_leaf_speedtree_scopes"
    scopes.mkdir(exist_ok=True)
    payload = {
        "spm": str(spm),
        "atlas_asset_name": atlas_base,
        "generator_connection": {
            "requested": True,
            "complete": True,
            "source_material_ids": list(source_ids),
            "source_material_names": list(source_names),
            "bindings": list(bindings),
        },
    }
    path = scopes / f"test__{spm.stem}.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def write_legacy_receipt(spm, guids):
    receipt = marker_receipt_path(spm)
    receipt.parent.mkdir(exist_ok=True)
    receipt.write_text(json.dumps({
        "kind": RECEIPT_KIND,
        "version": RECEIPT_VERSION,
        "status": "applied",
        "spm": str(spm.resolve()),
        "generator_guids": list(guids),
        "entries": {},
    }), encoding="utf-8")
    return receipt


class SemanticLeafSourceTests(unittest.TestCase):
    def test_frond_material_without_leaf_word_is_detected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            color = image(root / "source" / "LadyFern06.tga")
            alpha = image(root / "source" / "LadyFern06_Opacity.tga")
            spm = root / "SK_weed_ladyfern_01.spm"
            write_spm(
                spm,
                [material("9", "cluster_ladyfern_02", [color, alpha], (1, 2))],
                [generator("Frond", "9", "1")],
            )

            sources = audit.leaf_sources_from_spm(
                spm, "direct", active_only=False)

            self.assertEqual(len(sources), 1)
            self.assertEqual(sources[0]["material_ids"], ["9"])
            self.assertEqual(sources[0]["source_family"], "LadyFern06")

    def test_leaf_named_branch_material_is_not_a_semantic_source(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            color = image(root / "leaf_color.tga")
            alpha = image(root / "leaf_opacity.tga")
            spm = root / "branch.spm"
            write_spm(
                spm,
                [material("1", "M_leaf_but_branch", [color, alpha], (1,))],
                [generator("Branch", "1", "1")],
            )
            self.assertEqual(
                audit.leaf_sources_from_spm(spm, "direct", active_only=False),
                [],
            )

    def test_builder_output_is_not_rediscovered_when_connected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            color = image(root / "LadyFern03.tga")
            alpha = image(root / "LadyFern03_Opacity.tga")
            spm = root / "SK_weed_ladyfern_01.spm"
            write_spm(
                spm,
                [material(
                    "10", "M_leaf_ladyfern_atlas_01", [color, alpha],
                    (14, 15), managed=True)],
                [generator("Leaf Mesh", "10", "14")],
            )
            self.assertEqual(
                audit.leaf_sources_from_spm(spm, "direct", active_only=False),
                [],
            )

    def test_connected_builder_cluster_output_does_not_expand_internal_sources(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "weed_ladyfern"
            cluster_dir = root / "Cluster"
            cluster_dir.mkdir(parents=True)
            render_color = image(cluster_dir / "cluster_ladyfern_01.tga")
            render_alpha = image(
                cluster_dir / "cluster_ladyfern_01_Opacity.tga")
            fern03_color = image(root / "source" / "LadyFern03.tga")
            fern03_alpha = image(root / "source" / "LadyFern03_Opacity.tga")
            fern07_color = image(root / "source" / "LadyFern07.tga")
            fern07_alpha = image(root / "source" / "LadyFern07_Opacity.tga")

            cluster_spm = cluster_dir / "cluster_ladyfern_01.spm"
            write_spm(
                cluster_spm,
                [
                    material("1", "Material", [fern03_color, fern03_alpha], (1,)),
                    material("2", "Material 2", [fern07_color, fern07_alpha], (2,)),
                ],
                [
                    generator("Leaf Mesh", "1", "1", 0),
                    generator("Leaf Mesh", "2", "2", 1),
                ],
            )
            final_spm = root / "SK_weed_ladyfern_01.spm"
            write_spm(
                final_spm,
                [
                    material(
                        "3", "M_cluster_ladyfern_01",
                        [render_color, render_alpha], (3,)),
                    material(
                        "10", "M_leaf_ladyfern_atlas_01",
                        [render_color, render_alpha], (14, 15), managed=True),
                ],
                [
                    generator("Leaf Mesh", "10", "14"),
                    generator("Leaf Mesh", "3", "3", 1, hidden=True),
                ],
            )
            atlas = root.parent / "atlas"
            atlas.mkdir()
            (atlas / "M_leaf_ladyfern_atlas_01.blend").write_bytes(b"BLENDER")
            cfg = {
                "atlas_root": str(atlas),
                "required_export_maps": ["color", "opacity"],
                "source_texture_roots": [],
            }

            sources, referenced = audit.discover_leaf_mesh_sources(
                root, cfg, [final_spm], audit.cluster_spms(root))
            self.assertEqual(sources, [])
            self.assertEqual(referenced, {})

            item = audit.audit_folder(root, cfg)
            self.assertEqual(item["leaf_mesh_sources"], [])
            self.assertNotIn(
                "Blender 잎 매쉬 Generator 연결 필요", item["actions"])

    def test_visible_receipt_owned_cluster_is_provenance_not_a_new_job(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "weed_velvet_grass"
            cluster_dir = root / "cluster"
            cluster_dir.mkdir(parents=True)
            render_color = image(
                root / "texture" / "cluster_velvet_grass_01.tga")
            render_alpha = image(
                root / "texture" / "cluster_velvet_grass_01_Opacity.tga")
            source_color = image(cluster_dir / "velvet_source.tga")
            source_alpha = image(
                cluster_dir / "velvet_source_Opacity.tga")
            cluster_spm = cluster_dir / "cluster_velvet_grass_01.spm"
            write_spm(
                cluster_spm,
                [material(
                    "1", "Material", [source_color, source_alpha], (1,))],
                [generator("Leaf Mesh", "1", "1")],
            )
            target_spm = root / "SK_weed_velvet_grass_03.spm"
            write_spm(
                target_spm,
                [material(
                    "3", "M_cluster_velvet_grass_01",
                    [render_color, render_alpha], (21, 22, 23))],
                [generator("Leaf Mesh", "3", "21")],
            )
            write_legacy_receipt(target_spm, ["guid-0"])
            cfg = {
                "atlas_root": str(root.parent / "atlas"),
                "required_export_maps": ["color", "opacity"],
                "source_texture_roots": [],
            }

            lineage = audit.resolve_leaf_atlas_lineage(
                root, cfg, [target_spm], [cluster_spm])

            self.assertEqual(lineage["schema_version"], 2)
            self.assertEqual(lineage["actionable_sources"], [])
            self.assertEqual(lineage["referenced_clusters"], {})
            self.assertEqual(len(lineage["source_provenance"]), 1)
            provenance = lineage["source_provenance"][0]
            self.assertEqual(
                provenance["resolution_status"],
                "legacy_cluster_source_preserved",
            )
            self.assertEqual(
                provenance["legacy_cluster_generator_guids"], ["guid-0"])

    def test_complete_current_atlas_material_suppresses_old_source_job(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "weed_velvet_grass"
            root.mkdir()
            color = image(root / "source" / "velvet_source.tga")
            alpha = image(
                root / "source" / "velvet_source_Opacity.tga")
            target_spm = root / "SK_weed_velvet_grass_03.spm"
            write_spm(
                target_spm,
                [material(
                    "1", "M_leaf_velvet_grass_01",
                    [color, alpha], (21, 22, 23))],
                [generator("Leaf Mesh", "1", "-10")],
            )
            atlas = root.parent / "atlas"
            atlas.mkdir()
            blend = atlas / "M_Leaf_velvet_grass_01.blend"
            blend.write_bytes(b"BLENDER")
            cfg = {
                "atlas_root": str(atlas),
                "required_export_maps": ["color", "opacity"],
                "source_texture_roots": [],
            }

            lineage = audit.resolve_leaf_atlas_lineage(
                root, cfg, [target_spm], [])

            self.assertEqual(lineage["actionable_sources"], [])
            self.assertEqual(len(lineage["source_provenance"]), 1)
            self.assertEqual(
                lineage["source_provenance"][0]["resolution_status"],
                "current_atlas_material_connected",
            )
            self.assertEqual(len(lineage["current_atlases"]), 1)
            self.assertTrue(lineage["current_atlases"][0]["complete"])
            self.assertEqual(
                lineage["current_atlases"][0]["atlas_blends"], [str(blend)])

    def test_lineage_separates_inactive_source_from_split_current_atlas(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "weed_susan"
            root.mkdir()
            source_color = image(root / "source" / "Susan_albedo.tif")
            source_alpha = image(root / "source" / "Susan_alpha.tif")
            current_color = image(
                root / "texture" / "T_leaf_susan_atlas_01_color.tga")
            current_alpha = image(
                root / "texture" / "T_leaf_susan_atlas_01_opacity.tga")
            source_spm = root / "weed_flower_susan_01.spm"
            target_spm = root / "SK_weed_flower_susan_01.spm"
            write_spm(
                source_spm,
                [material(
                    "1", "leaf_susan_01",
                    [source_color, source_alpha], (1,))],
                [generator("Leaf Mesh", "1", "1")],
            )
            write_spm(
                target_spm,
                [
                    material(
                        "1", "M_leaf_susan_01",
                        [source_color, source_alpha], (1,)),
                    material(
                        "7", "M_leaf_susan_atlas_01_green",
                        [current_color, current_alpha], (20, 21),
                        managed=True),
                    material(
                        "8", "M_leaf_susan_atlas_01_flower",
                        [source_color, source_alpha], (30,), managed=True),
                ],
                [
                    generator("Leaf Mesh", "7", "-10", 0),
                    generator("Leaf Mesh", "8", "30", 1),
                    # The authored pre-atlas Generator stays hidden for
                    # lineage/debugging. It must not become a fresh build job.
                    generator("Leaf Mesh", "1", "1", 2, hidden=True),
                ],
            )
            atlas = root.parent / "atlas"
            atlas.mkdir()
            blend = atlas / "M_leaf_susan_atlas_01.blend"
            blend.write_bytes(b"BLENDER")
            cfg = {
                "atlas_root": str(atlas),
                "required_export_maps": ["color", "opacity"],
                "source_texture_roots": [],
            }
            before = (target_spm.read_bytes(), target_spm.stat().st_mtime_ns)

            lineage = audit.resolve_leaf_atlas_lineage(
                root, cfg, [target_spm], [])
            sources, _referenced = audit.discover_leaf_mesh_sources(
                root, cfg, [target_spm], [])

            self.assertEqual(sources, [])
            self.assertEqual(lineage["actionable_sources"], [])
            self.assertEqual(len(lineage["source_provenance"]), 1)
            provenance = lineage["source_provenance"][0]
            self.assertEqual(provenance["albedo"], source_color)
            self.assertEqual(provenance["alpha"], source_alpha)
            self.assertEqual(
                provenance["targets"][0]["source_material_ids"], ["1"])
            self.assertNotIn("T_leaf_susan_atlas_01", provenance["albedo"])
            self.assertEqual(len(lineage["current_atlases"]), 1)
            current = lineage["current_atlases"][0]
            self.assertEqual(current["atlas_base"],
                             "M_leaf_susan_atlas_01")
            self.assertEqual(current["atlas_blends"], [str(blend)])
            self.assertEqual(
                set(current["material_ids"]), {"7", "8"})
            self.assertTrue(current["generator_connection_complete"])
            self.assertTrue(current["complete"])
            self.assertEqual(
                (target_spm.read_bytes(), target_spm.stat().st_mtime_ns),
                before,
            )

    def test_lineage_does_not_promote_unmapped_generated_target_to_source(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "weed_thistle"
            root.mkdir()
            source_color = image(root / "source" / "Thistle_albedo.tif")
            source_alpha = image(root / "source" / "Thistle_alpha.tif")
            generated_color = image(
                root / "texture" / "T_leaf_thistle_atlas_01_color.tga")
            generated_alpha = image(
                root / "texture" / "T_leaf_thistle_atlas_01_opacity.tga")
            source_spm = root / "weed_thistle_01.spm"
            target_spm = root / "SK_weed_thistle_01.spm"
            write_spm(
                source_spm,
                [material(
                    "1", "leaf_thistle_01",
                    [source_color, source_alpha], (1,))],
                [generator("Leaf Mesh", "1", "1")],
            )
            # The source row was deleted entirely. A coherent generated T_*
            # pair in the final SK is current-state evidence, not provenance.
            write_spm(
                target_spm,
                [material(
                    "7", "M_leaf_thistle_atlas_01",
                    [generated_color, generated_alpha], (20,))],
                [generator("Leaf Mesh", "7", "20")],
            )
            atlas = root.parent / "atlas"
            atlas.mkdir()
            (atlas / "M_leaf_thistle_atlas_01.blend").write_bytes(b"BLENDER")
            cfg = {
                "atlas_root": str(atlas),
                "required_export_maps": ["color", "opacity"],
                "source_texture_roots": [],
            }

            lineage = audit.resolve_leaf_atlas_lineage(
                root, cfg, [target_spm], [])

            self.assertEqual(lineage["actionable_sources"], [])
            self.assertEqual(lineage["source_provenance"], [])
            self.assertEqual(len(lineage["current_atlases"]), 1)
            self.assertEqual(
                lineage["target_resolutions"][0]["rejections"][0]["reason"],
                "target_material_lineage_missing",
            )

    def test_untagged_normalized_maps_still_expose_parsley_mesh_source(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            texture = root / "texture"
            color = image(texture / "T_leaf_parsley_atlas_02_color.tga")
            alpha = image(texture / "T_leaf_parsley_atlas_02_opacity.tga")
            spm = root / "SK_weed_parsley_01.spm"
            write_spm(
                spm,
                [material("4", "M_leaf_parsley_02", [color, alpha], (6, 7))],
                [generator("Leaf Mesh", "4", "6")],
            )
            sources = audit.leaf_sources_from_spm(
                spm, "direct", active_only=False)
            self.assertEqual(len(sources), 1)
            self.assertEqual(sources[0]["source_family"],
                             "T_leaf_parsley_atlas_02")

    def test_ladyfern_hidden_pair_is_provenance_not_direct_target(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "weed_ladyfern"
            root.mkdir()
            cluster_color = image(root / "cluster" / "cluster_ladyfern_01.tga")
            cluster_alpha = image(
                root / "cluster" / "cluster_ladyfern_01_Opacity.tga")
            fern_color = image(root / "source" / "LadyFern06.tga")
            fern_alpha = image(root / "source" / "LadyFern06_Opacity.tga")
            materials = [
                material("4", "cluster_ladyfern_01",
                         [cluster_color, cluster_alpha], (7, 8, 9)),
                material("9", "cluster_ladyfern_02",
                         [fern_color, fern_alpha], (1, 2, 3, 13)),
            ]
            generators = [
                generator("Leaf Mesh", "4", "7", 0, hidden=True),
                generator("Frond", "9", "1", 1),
            ]
            source_spm = root / "weed_ladyfern_01.spm"
            target_spm = root / "SK_weed_ladyfern_01.spm"
            sibling_spm = root / "SK_weed_ladyfern_b_01.spm"
            write_spm(source_spm, materials, generators)
            write_spm(target_spm, materials, generators)
            write_spm(sibling_spm, materials, generators)
            atlas = root.parent / "atlas"
            atlas.mkdir()

            cfg = {
                "atlas_root": str(atlas),
                "required_export_maps": ["color", "opacity"],
                "source_texture_roots": [],
            }
            sources, _referenced = audit.discover_leaf_mesh_sources(
                root, cfg, [target_spm], [])
            lineage = audit.resolve_leaf_atlas_lineage(
                root, cfg, [target_spm], [])

            by_id = {
                target["source_material_ids"][0]: source
                for source in sources for target in source["targets"]
            }
            self.assertEqual(set(by_id), {"9"})
            self.assertEqual(by_id["9"]["source_family"], "LadyFern06")
            self.assertEqual(by_id["9"]["atlas_base"],
                             "M_leaf_ladyfern_atlas_02")
            self.assertEqual(len(lineage["source_provenance"]), 1)
            self.assertEqual(
                lineage["source_provenance"][0]["targets"][0][
                    "source_material_ids"
                ],
                ["4"],
            )
            self.assertTrue(all(
                source["generator_connection_update_needed"]
                for source in sources))

            folder_item = audit.audit_folder(root, cfg)
            self.assertEqual(folder_item["leaf_mesh_target_spms"],
                             [str(target_spm)])
            self.assertEqual(
                {Path(target["spm"]).name
                 for source in folder_item["leaf_mesh_sources"]
                 for target in source["targets"]},
                {target_spm.name},
            )
            self.assertNotIn(
                "M_leaf_ladyfern_atlas_03",
                {source["atlas_base"]
                 for source in folder_item["leaf_mesh_sources"]},
            )


class AtlasNameAllocationTests(unittest.TestCase):
    @staticmethod
    def source(root, suffix, material_name="M_cluster_ladyfern_01"):
        return {
            "albedo": str(root / f"LadyFern{suffix}_albedo.tif"),
            "alpha": str(root / f"LadyFern{suffix}_alpha.tif"),
            "targets": [{"material_names": [material_name]}],
        }

    def test_same_material_different_pairs_receive_stable_next_free_numbers(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            sources = [self.source(root, "07"), self.source(root, "03")]
            audit.assign_leaf_atlas_bases(sources, root / "weed_ladyfern")
            by_source = {Path(row["albedo"]).stem: row["atlas_base"]
                         for row in sources}
            self.assertEqual(by_source, {
                "LadyFern03_albedo": "M_leaf_ladyfern_atlas_01",
                "LadyFern07_albedo": "M_leaf_ladyfern_atlas_02",
            })

    def test_existing_other_pairs_and_blend_backup_reserve_their_bases(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            folder = root / "weed_ladyfern"
            atlas = root / "atlas"
            scopes = folder / ".atlas_leaf_speedtree_scopes"
            atlas.mkdir()
            scopes.mkdir(parents=True)
            (atlas / "M_leaf_ladyfern_atlas_01.blend").write_bytes(b"BLENDER")
            (atlas / "M_leaf_ladyfern_atlas_02.blend1").write_bytes(b"BACKUP")
            for index in (1, 2):
                payload = {
                    "blend_file": str(
                        atlas / f"M_leaf_ladyfern_atlas_{index:02d}.blend"),
                    "textures": {
                        "albedo": str(root / f"Other{index}_albedo.tif"),
                        "alpha": str(root / f"Other{index}_alpha.tif"),
                    },
                }
                (scopes / f"scope{index}.json").write_text(
                    json.dumps(payload), encoding="utf-8")
            source = self.source(root, "03")

            audit.assign_leaf_atlas_bases(
                [source], folder, atlas_root=atlas)

            self.assertEqual(source["atlas_base"],
                             "M_leaf_ladyfern_atlas_03")

    def test_matching_live_legacy_cluster_blend_is_preserved_not_renamed(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            folder = root / "weed_ladyfern"
            atlas = root / "atlas"
            scopes = folder / ".atlas_leaf_speedtree_scopes"
            atlas.mkdir()
            scopes.mkdir(parents=True)
            source = self.source(root, "03")
            legacy = atlas / "M_cluster_ladyfern_atlas_01.blend"
            legacy.write_bytes(b"BLENDER")
            (scopes / "scope.json").write_text(json.dumps({
                "blend_file": str(legacy),
                "textures": {
                    "albedo": source["albedo"], "alpha": source["alpha"],
                },
            }), encoding="utf-8")

            audit.assign_leaf_atlas_bases(
                [source], folder, atlas_root=atlas)

            self.assertEqual(source["atlas_base"], legacy.stem)
            self.assertTrue(source["legacy_atlas_base_preserved"])

    def test_unique_asset_scope_manifest_preserves_authored_atlas_base(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            folder = root / "Weed_Common_grass"
            atlas = folder / "atlas"
            scopes = folder / ".atlas_leaf_speedtree_scopes"
            atlas.mkdir(parents=True)
            scopes.mkdir()
            source = self.source(
                root, "03", material_name="M_Leaf_Grass_01"
            )
            authored = atlas / "M_Leaf_common_grass_01.blend"
            authored.write_bytes(b"BLENDER")
            (scopes / "scope.json").write_text(json.dumps({
                "blend_file": str(authored),
                "textures": {
                    "albedo": source["albedo"],
                    "alpha": source["alpha"],
                },
                "material_groups": [
                    {"group": "Green", "material": "M_Leaf_common_grass_01_green"},
                    {"group": "Dead", "material": "M_Leaf_common_grass_01_dead"},
                ],
            }), encoding="utf-8")

            audit.assign_leaf_atlas_bases([source], folder)

            self.assertEqual(source["atlas_base"], authored.stem)
            self.assertTrue(source["scoped_atlas_base_preserved"])

    def test_ambiguous_asset_scope_source_pair_is_not_chosen_by_name_guess(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            folder = root / "Weed_Common_grass"
            atlas = folder / "atlas"
            scopes = folder / ".atlas_leaf_speedtree_scopes"
            atlas.mkdir(parents=True)
            scopes.mkdir()
            source = self.source(
                root, "03", material_name="M_Leaf_Grass_01"
            )
            for index in (1, 2):
                authored = atlas / f"M_Leaf_common_grass_{index:02d}.blend"
                authored.write_bytes(b"BLENDER")
                (scopes / f"scope{index}.json").write_text(json.dumps({
                    "blend_file": str(authored),
                    "textures": {
                        "albedo": source["albedo"],
                        "alpha": source["alpha"],
                    },
                }), encoding="utf-8")

            audit.assign_leaf_atlas_bases([source], folder)

            self.assertEqual(source["atlas_base"], "M_Leaf_Grass_atlas_01")
            self.assertNotIn("scoped_atlas_base_preserved", source)


class GeneratorConnectionTests(unittest.TestCase):
    def test_manifest_connection_audit_is_read_only(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "tree_test"
            root.mkdir()
            target = root / "SK_tree_test.spm"
            target.write_bytes(b"spm")
            manifest = write_connection_manifest(
                target,
                "M_leaf_test",
                [],
            )
            before = (manifest.read_bytes(), manifest.stat().st_mtime_ns)

            payloads = audit._scoped_connection_payloads(target)

            self.assertEqual(len(payloads), 1)
            self.assertEqual(payloads[0]["_manifest_path"], str(manifest.resolve()))
            self.assertEqual(
                (manifest.read_bytes(), manifest.stat().st_mtime_ns),
                before,
            )

    def test_audit_lists_only_tagged_connected_outputs_in_exact_target_spm(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "weed_parsley"
            root.mkdir()
            color = image(root / "texture" / "color.tga")
            alpha = image(root / "texture" / "opacity.tga")
            target_spm = root / "SK_weed_parsley_01.spm"
            sibling_spm = root / "SK_weed_parsley_b_01.spm"
            write_spm(
                target_spm,
                [
                    material("5", "M_leaf_parsley_atlas_01",
                             [color, alpha], (11, 12), managed=True),
                    material("6", "M_leaf_parsley_atlas_02",
                             [color, alpha], (21,), managed=True),
                    material("7", "M_leaf_parsley_atlas_03",
                             [color, alpha], (31,)),
                    material("8", "M_leaf_parsley_atlas_04",
                             [color, alpha], (41,), managed=True),
                ],
                [
                    generator("Frond", "5", "11", 0),
                    generator("Leaf Mesh", "6", "99", 1),
                    generator("Leaf Mesh", "7", "31", 2),
                    generator("Branch", "8", "41", 3),
                ],
            )
            write_spm(
                sibling_spm,
                [material("9", "M_leaf_parsley_atlas_05",
                          [color, alpha], (51,), managed=True)],
                [generator("Leaf Mesh", "9", "51", 4)],
            )
            atlas = root.parent / "atlas"
            atlas.mkdir()
            cfg = {
                "atlas_root": str(atlas),
                "required_export_maps": ["color", "opacity"],
                "source_texture_roots": [],
            }
            write_connection_manifest(
                target_spm,
                "M_leaf_parsley_atlas_01",
                [{
                    "generator_index": 0,
                    "slot_prefix": "Leaves:Type:0",
                    "source_material_id": "4",
                    "source_material_name": "M_leaf_parsley_source",
                    "source_mesh_id": "6",
                    "leaf_ordinal": 1,
                    "target_material_id": "5",
                    "target_mesh_id": "11",
                }],
                source_names=("M_leaf_parsley_source",),
            )

            item = audit.audit_folder(root, cfg)

            self.assertEqual(item["leaf_mesh_target_spms"], [str(target_spm)])
            self.assertEqual(len(item["managed_leaf_outputs"]), 1)
            output = item["managed_leaf_outputs"][0]
            self.assertEqual(output["spm"], str(target_spm))
            self.assertEqual(output["material_id"], "5")
            self.assertEqual(output["material_name"],
                             "M_leaf_parsley_atlas_01")
            self.assertEqual(output["cutout_mesh_ids"], ["11", "12"])
            self.assertEqual(
                output["generator_bindings"][0]["generator_type"], "Frond")
            self.assertTrue(output["generator_bindings"][0]
                            ["mesh_belongs_to_material"])

    def test_source_id_status_requires_material_and_mesh_connection(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            color = image(root / "color.tga")
            alpha = image(root / "opacity.tga")
            spm = root / "SK_weed_parsley_01.spm"
            materials = [
                material("4", "M_leaf_parsley_02", [color, alpha], (6, 7)),
                material("5", "M_leaf_parsley_atlas_02", [color, alpha],
                         (11, 12), managed=True),
            ]
            write_spm(spm, materials, [generator("Leaf Mesh", "4", "6")])
            expected = [{
                "generator_index": 0,
                "slot_prefix": "Leaves:Type:0",
                "source_material_id": "4",
                "source_mesh_id": "6",
                "leaf_ordinal": 1,
                "target_material_id": "5",
                "target_mesh_id": "11",
            }]
            pending = audit.inspect_leaf_generator_connection(
                spm, ["M_leaf_parsley_02"], ["4"],
                "M_leaf_parsley_atlas_02",
                expected_generator_bindings=expected)
            self.assertFalse(pending["generator_connection_complete"])
            self.assertEqual(pending["source_material_statuses"][0]["material_id"],
                             "4")
            self.assertEqual(len(pending["source_generator_bindings"]), 1)

            write_spm(spm, materials, [generator("Leaf Mesh", "5", "11")])
            complete = audit.inspect_leaf_generator_connection(
                spm, ["M_leaf_parsley_02"], ["4"],
                "M_leaf_parsley_atlas_02",
                expected_generator_bindings=expected)
            self.assertTrue(complete["generator_connection_complete"])
            self.assertTrue(
                complete["managed_generator_bindings"][0]["mesh_belongs_to_material"])

    def test_partial_two_slot_repair_is_not_complete(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            spm = root / "SK_partial.spm"
            materials = [
                material("4", "M_leaf_source", [], (6, 7)),
                material("5", "M_leaf_atlas_01", [], (11, 12), managed=True),
            ]
            write_spm(spm, materials, [
                generator("Leaf Mesh", "5", "11", 0),
                generator("Leaf Mesh", "4", "7", 1),
            ])
            expected = [
                {"generator_index": 0, "slot_prefix": "Leaves:Type:0",
                 "source_material_id": "4", "source_mesh_id": "6",
                 "leaf_ordinal": 1, "target_material_id": "5",
                 "target_mesh_id": "11"},
                {"generator_index": 1, "slot_prefix": "Leaves:Type:1",
                 "source_material_id": "4", "source_mesh_id": "7",
                 "leaf_ordinal": 2, "target_material_id": "5",
                 "target_mesh_id": "12"},
            ]

            status = audit.inspect_leaf_generator_connection(
                spm, ["M_leaf_source"], ["4"], "M_leaf_atlas_01",
                expected_generator_bindings=expected)

            self.assertFalse(status["generator_connection_complete"])
            self.assertEqual(status["expected_slot_count"], 2)
            self.assertEqual(status["connected_slot_count"], 1)
            self.assertIn("source_material_still_connected",
                          status["generator_connection_reason"])

    def test_missing_mesh_asset_is_not_complete(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            spm = root / "SK_missing_mesh.spm"
            write_spm(
                spm,
                [material("5", "M_leaf_atlas_01", [], (11,), managed=True)],
                [generator("Leaf Mesh", "5", "11", 0)],
                mesh_ids=[],
            )
            expected = [{
                "generator_index": 0, "slot_prefix": "Leaves:Type:0",
                "source_material_id": "4", "source_mesh_id": "6",
                "leaf_ordinal": 1, "target_material_id": "5",
                "target_mesh_id": "11",
            }]

            status = audit.inspect_leaf_generator_connection(
                spm, ["M_leaf_source"], ["4"], "M_leaf_atlas_01",
                expected_generator_bindings=expected)

            self.assertFalse(status["generator_connection_complete"])
            self.assertEqual(status["generator_connection_reason"],
                             "managed_mesh_asset_missing")

    def test_same_name_source_ids_are_audited_independently(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            spm = root / "SK_same_name.spm"
            write_spm(
                spm,
                [
                    material("4", "M_leaf_shared", [], (6,)),
                    material("6", "M_leaf_shared", [], (7,)),
                    material("10", "M_leaf_atlas_01", [], (11,), managed=True),
                    material("11", "M_leaf_atlas_01_yellow", [], (12,), managed=True),
                ],
                [
                    generator("Leaf Mesh", "10", "11", 0),
                    generator("Leaf Mesh", "6", "7", 1),
                ],
            )
            expected = [
                {"generator_index": 0, "slot_prefix": "Leaves:Type:0",
                 "source_material_id": "4", "source_mesh_id": "6",
                 "leaf_ordinal": 1, "target_material_id": "10",
                 "target_mesh_id": "11"},
                {"generator_index": 1, "slot_prefix": "Leaves:Type:1",
                 "source_material_id": "6", "source_mesh_id": "7",
                 "leaf_ordinal": 1, "target_material_id": "11",
                 "target_mesh_id": "12"},
            ]

            status = audit.inspect_leaf_generator_connection(
                spm, ["M_leaf_shared"], None, "M_leaf_atlas_01",
                expected_generator_bindings=expected)

            self.assertEqual(status["source_material_ids"], ["4", "6"])
            by_id = {row["material_id"]: row for row in
                     status["source_material_statuses"]}
            self.assertTrue(by_id["4"]["complete"])
            self.assertFalse(by_id["6"]["complete"])
            self.assertFalse(status["generator_connection_complete"])

    def test_cache_schema_is_v4_and_exposes_semantic_bindings(self):
        self.assertEqual(audit.SPM_ANALYSIS_CACHE_PATH.name,
                         "spm_analysis_v5.json")
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            spm = root / "test.spm"
            write_spm(
                spm,
                [material("4", "cluster_test", [], (7,))],
                [generator("Leaf Mesh", "4", "7")],
            )
            self.assertEqual(audit.leaf_generator_material_ids(spm), {"4"})


@unittest.skipUnless(
    (LADYFERN / "SK_weed_ladyfern_01.spm").is_file(),
    "local Ladyfern fixture is unavailable")
class RealLadyfernReadOnlyTests(unittest.TestCase):
    def test_current_spm_exposes_multiple_semantic_leaf_materials(self):
        bindings = audit.leaf_generator_bindings(
            LADYFERN / "SK_weed_ladyfern_01.spm")
        self.assertGreaterEqual(
            len({row["material_id"] for row in bindings}), 2)
        self.assertTrue(any(
            audit._normalized_generator_type(row["generator_type"]) == "frond"
            for row in bindings))


@unittest.skipUnless(
    (PARSLEY / "SK_weed_parsley_01.spm").is_file(),
    "local Parsley fixture is unavailable")
class RealParsleyReadOnlyTests(unittest.TestCase):
    def test_current_spm_exposes_semantic_material_mesh_pairs(self):
        bindings = audit.leaf_generator_bindings(
            PARSLEY / "SK_weed_parsley_01.spm")
        self.assertTrue(bindings)
        self.assertTrue(all(
            audit._is_leaf_mesh_generator_type(row["generator_type"])
            for row in bindings))
        self.assertTrue(any(row.get("mesh_property") for row in bindings))


if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0]])
