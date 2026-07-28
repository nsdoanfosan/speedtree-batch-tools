import gzip
import hashlib
import shutil
import sys
import tempfile
import unittest
from pathlib import Path


TOOL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOL_DIR))

from pcg_cluster_bark_normalization import (
    BarkNormalizationError,
    apply_isolated_bark_normalization,
    build_isolated_bark_normalization_plan,
    normalize_isolated_canonical_bark_name,
    validate_canonical_bark_export_bundle,
)
from pcg_texture_audit import (
    active_material_ids,
    extract_material_image_refs,
    prepare_sk,
    read_maybe_gzip_text,
)


REAL_ELM_ROOT = Path(
    r"D:\OneDrive\Forestportfolio\02_nature\Tree\Tree_elm")
REAL_CANONICAL_SPM = REAL_ELM_ROOT / "SK_Tree_elm_01.spm"
REAL_CLUSTER_SPMS = (
    REAL_ELM_ROOT / "Cluster" / "branch_elm_01.spm",
    REAL_ELM_ROOT / "Cluster" / "leaf_elm_01.spm",
    REAL_ELM_ROOT / "Cluster" / "leaf_elm_side_01.spm",
)


def map_block(name, texture):
    return (
        f'<Map Name="{name}"><ColorX>1</ColorX><ColorY>1</ColorY>'
        '<ColorZ>1</ColorZ>'
        f'<TexFilename>{texture}</TexFilename><TexSource>0</TexSource>'
        '<TexEnabled>true</TexEnabled><TexInvert>false</TexInvert></Map>'
    )


def write_spm(path, material_name, texture_refs, cutout_ids=(), compressed=True):
    maps = "".join(
        map_block(name, texture)
        for name, texture in texture_refs
    )
    cutouts = "".join(
        f'<CutoutMesh ID="{value}"/>' for value in cutout_ids)
    meshes = "".join(
        f'<Mesh ID="{value}" Name="mesh-{value}"/>' for value in cutout_ids)
    payload = (
        '<SpeedTree><Materials>'
        f'<Material_v8 ID="1" Name="{material_name}">{maps}'
        f'<SupplementalCutoutMeshIDs>{cutouts}</SupplementalCutoutMeshIDs>'
        '</Material_v8></Materials>'
        f'<Meshes>{meshes}</Meshes>'
        '<Generators><Generator Type="Branch"><Properties><Property>'
        '<Name>Branches:Material</Name><Value>1</Value>'
        '</Property></Properties></Generator></Generators></SpeedTree>'
    ).encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    if compressed:
        with gzip.open(path, "wb") as handle:
            handle.write(payload)
    else:
        path.write_bytes(payload)


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_ascii_fbx(path, material_name="M_bark_elm_01_Mat", with_pair=True,
                    with_uv=True):
    connections = [
        '    C: "OO",201,200',
    ]
    if with_pair:
        connections.append('    C: "OO",100,200')
    path.write_text(
        '; FBX 7.4.0 project file\nObjects: {\n'
        f'    Material: 100, "Material::{material_name}", "" {{}}\n'
        '    Model: 200, "Model::bark_part", "Mesh" {}\n'
        '    Geometry: 201, "Geometry::bark_part", "Mesh" {\n'
        + ('      LayerElementUV: 0 {}\n' if with_uv else '')
        + '    }\n}\nConnections: {\n'
        + "\n".join(connections)
        + '\n}\n',
        encoding="utf-8",
    )


def write_export_xml(path, material="M_bark_elm_01_Mat",
                     textures=("T_bark_elm_01_color.tga",
                               "T_bark_elm_01_normal.tga")):
    maps = "".join(
        f'<Map Name="Map{index}" Source="X:/{value}"/>'
        for index, value in enumerate(textures)
    )
    path.write_text(
        f'<SpeedTree><Materials><Material Name="{material}">{maps}'
        '</Material></Materials></SpeedTree>',
        encoding="utf-8",
    )


class BarkNormalizationTests(unittest.TestCase):
    def test_final_isolated_sk_exact_bark_name_preserves_texture_references(self):
        target = self.copy_root / "SK_Tree_elm_01.spm"
        shutil.copy2(self.canonical, target)
        source_hash = sha256(self.canonical)

        result = normalize_isolated_canonical_bark_name(
            self.contract,
            target,
            self.isolation,
        )

        self.assertEqual(result["status"], "normalized")
        self.assertEqual(result["canonical_material"], "M_bark_elm_01")
        self.assertTrue(result["texture_references_preserved"])
        self.assertEqual(sha256(self.canonical), source_hash)
        rows = extract_material_image_refs(target)
        self.assertEqual(rows[0]["material_name"], "M_bark_elm_01")

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.original = self.root / "authoring" / "Tree_elm"
        self.isolation = self.root / "isolated"
        self.copy_root = self.isolation / "Tree_elm"
        self.canonical = self.original / "SK_Tree_elm_01.spm"
        self.cluster = self.original / "Cluster" / "branch_elm_01.spm"
        self.cluster_copy = (
            self.copy_root / "Cluster" / "branch_elm_01.spm")

        source_texture = self.original / "texture"
        isolated_texture = self.copy_root / "texture"
        source_texture.mkdir(parents=True)
        isolated_texture.mkdir(parents=True)
        for name, payload in (
                ("T_bark_elm_01_color.tga", b"color"),
                ("T_bark_elm_01_normal.tga", b"normal")):
            (source_texture / name).write_bytes(payload)
            (isolated_texture / name).write_bytes(payload)
        write_spm(
            self.canonical,
            "M_Bark_elm_01",
            (("Color", "texture/T_bark_elm_01_color.tga"),
             ("Normal", "texture/T_bark_elm_01_normal.tga")),
        )
        write_spm(
            self.cluster,
            "Bark_tree_NothofagusSolandri_01",
            (("Color", "../../Nothofagus/bark_color.jpg"),
             ("Normal", "../../Nothofagus/bark_normal.jpg")),
            cutout_ids=("7", "8"),
        )
        self.cluster_copy.parent.mkdir(parents=True)
        shutil.copy2(self.cluster, self.cluster_copy)
        self.original_hash = sha256(self.cluster)
        self.contract = {
            "handoff": {
                "canonical_bark": {
                    "status": "replacement_required",
                    "canonical_material": "M_bark_elm_01",
                    "canonical_sources": [{
                        "spm": str(self.canonical),
                        "material_id": "1",
                        "material_name": "M_Bark_elm_01",
                    }],
                    "cluster_bark_sources": [{
                        "cluster_spm": str(self.cluster),
                        "material_name": "Bark_tree_NothofagusSolandri_01",
                        "replacement": "required",
                    }],
                },
            },
        }

    def tearDown(self):
        self.temp.cleanup()

    def normalize(self):
        plan = build_isolated_bark_normalization_plan(
            self.contract,
            {str(self.cluster): str(self.cluster_copy)},
            self.isolation,
        )
        return plan, apply_isolated_bark_normalization(plan)

    def test_isolated_spm_gets_canonical_slot_without_structure_or_source_mutation(self):
        plan, report = self.normalize()

        self.assertEqual(plan["status"], "ready")
        self.assertTrue(report["applied"])
        self.assertEqual(sha256(self.cluster), self.original_hash)
        self.assertNotEqual(sha256(self.cluster_copy), self.original_hash)
        rows = extract_material_image_refs(self.cluster_copy)
        self.assertEqual(rows[0]["material_name"], "M_bark_elm_01")
        self.assertEqual(rows[0]["material_id"], "1")
        self.assertEqual(rows[0]["cutout_mesh_ids"], ["7", "8"])
        self.assertEqual(
            {value.replace("\\", "/") for value in rows[0]["refs"]},
            {
                "../texture/T_bark_elm_01_color.tga",
                "../texture/T_bark_elm_01_normal.tga",
            },
        )
        output = report["outputs"][0]
        self.assertEqual(output["mesh_asset_ids"], ["7", "8"])
        self.assertTrue(output["uv_mesh_generator_payload_preserved"])
        self.assertFalse(report["production_sources_mutated"])

    def test_self_closing_texfilename_does_not_consume_the_next_map(self):
        text = read_maybe_gzip_text(self.canonical)
        text = text.replace(
            '<Map Name="Color">',
            '<Map Name="Unused"><TexFilename/></Map><Map Name="Color">',
            1,
        )
        with gzip.open(self.canonical, "wb") as handle:
            handle.write(text.encode("utf-8"))

        _plan, report = self.normalize()

        self.assertEqual(report["status"], "normalized")
        row = extract_material_image_refs(self.cluster_copy)[0]
        self.assertEqual(
            {Path(value).name.casefold() for value in row["refs"]},
            {
                "t_bark_elm_01_color.tga",
                "t_bark_elm_01_normal.tga",
            },
        )

    def test_rejects_source_path_as_write_target(self):
        with self.assertRaisesRegex(
                BarkNormalizationError, "production/source SPM"):
            build_isolated_bark_normalization_plan(
                self.contract,
                {str(self.cluster): str(self.cluster)},
                self.original,
            )

    def test_rejects_copy_outside_isolation_root(self):
        with self.assertRaisesRegex(
                BarkNormalizationError, "outside the isolation root"):
            build_isolated_bark_normalization_plan(
                self.contract,
                {str(self.cluster): str(self.cluster_copy)},
                self.root / "different-root",
            )

    def test_rejects_canonical_texture_copy_with_different_bytes(self):
        (self.copy_root / "texture" / "T_bark_elm_01_color.tga").write_bytes(
            b"not-canonical")
        with self.assertRaisesRegex(
                BarkNormalizationError, "texture hash mismatch"):
            build_isolated_bark_normalization_plan(
                self.contract,
                {str(self.cluster): str(self.cluster_copy)},
                self.isolation,
            )

    def test_plan_does_not_write(self):
        before = sha256(self.cluster_copy)
        build_isolated_bark_normalization_plan(
            self.contract,
            {str(self.cluster): str(self.cluster_copy)},
            self.isolation,
        )
        self.assertEqual(sha256(self.cluster_copy), before)

    def test_export_bundle_requires_material_mesh_uv_and_metadata_textures(self):
        _plan, report = self.normalize()
        export = self.root / "export"
        export.mkdir()
        fbx = export / "branch_elm_01.fbx"
        stmat = export / "branch_elm_01.stmat"
        xml = export / "branch_elm_01.xml"
        write_ascii_fbx(fbx)
        write_export_xml(stmat)
        write_export_xml(xml)

        result = validate_canonical_bark_export_bundle(
            fbx, stmat, xml, report)

        self.assertEqual(
            result["status"], "ready_for_downstream_blender_mapping")
        self.assertTrue(result["material_slot_propagated"])
        self.assertTrue(result["texture_set_propagated"])
        self.assertTrue(result["uv_preserved"])

    def test_export_bundle_allows_disabled_opaque_maps_to_be_omitted(self):
        _plan, report = self.normalize()
        report["outputs"][0]["canonical_textures"].extend([
            {
                "map": "Opacity",
                "isolated": str(
                    self.copy_root / "texture" / "T_bark_elm_01_opacity.tga"
                ),
                "export_enabled": False,
            },
            {
                "map": "SubsurfaceColor",
                "isolated": str(
                    self.copy_root / "texture" / "T_bark_elm_01_subsurface.tga"
                ),
                "export_enabled": False,
            },
        ])
        export = self.root / "export"
        export.mkdir()
        fbx = export / "branch_elm_01.fbx"
        stmat = export / "branch_elm_01.stmat"
        xml = export / "branch_elm_01.xml"
        write_ascii_fbx(fbx)
        write_export_xml(stmat)
        write_export_xml(xml)

        result = validate_canonical_bark_export_bundle(
            fbx, stmat, xml, report
        )

        self.assertEqual(
            result["status"], "ready_for_downstream_blender_mapping"
        )

    def test_export_bundle_allows_same_species_canonical_alias_slots(self):
        _plan, report = self.normalize()
        export = self.root / "export"
        export.mkdir()
        fbx = export / "branch_elm_01.fbx"
        stmat = export / "branch_elm_01.stmat"
        xml = export / "branch_elm_01.xml"
        write_ascii_fbx(fbx)
        expected = (
            '<Material Name="M_bark_elm_01_Mat">'
            '<Map Source="X:/T_bark_elm_01_color.tga"/>'
            '<Map Source="X:/T_bark_elm_01_normal.tga"/>'
            '</Material>'
        )
        alias = (
            '<Material Name="M_Bark_elm_01_Mat">'
            '<Map Source="X:/legacy/T_Bark_elm_01_color.tga"/>'
            '<Map Source="X:/legacy/T_Bark_elm_01_normal.tga"/>'
            '</Material>'
        )
        payload = (
            f"<SpeedTree><Materials>{alias}{expected}"
            "</Materials></SpeedTree>"
        )
        stmat.write_text(payload, encoding="utf-8")
        xml.write_text(payload, encoding="utf-8")

        result = validate_canonical_bark_export_bundle(
            fbx, stmat, xml, report
        )

        self.assertEqual(result["stmat"]["material_count"], 2)
        self.assertTrue(any(
            row["exact_normalized_set"]
            for row in result["stmat"]["materials"]
        ))

    def test_export_bundle_fails_closed_on_unbound_material(self):
        _plan, report = self.normalize()
        export = self.root / "export"
        export.mkdir()
        fbx = export / "branch_elm_01.fbx"
        stmat = export / "branch_elm_01.stmat"
        xml = export / "branch_elm_01.xml"
        write_ascii_fbx(fbx, with_pair=False)
        write_export_xml(stmat)
        write_export_xml(xml)

        with self.assertRaisesRegex(
                BarkNormalizationError, "not connected to a mesh"):
            validate_canonical_bark_export_bundle(fbx, stmat, xml, report)

    def test_export_bundle_fails_closed_on_missing_uv(self):
        _plan, report = self.normalize()
        export = self.root / "export"
        export.mkdir()
        fbx = export / "branch_elm_01.fbx"
        stmat = export / "branch_elm_01.stmat"
        xml = export / "branch_elm_01.xml"
        write_ascii_fbx(fbx, with_uv=False)
        write_export_xml(stmat)
        write_export_xml(xml)

        with self.assertRaisesRegex(BarkNormalizationError, "no UV layer"):
            validate_canonical_bark_export_bundle(fbx, stmat, xml, report)

    def test_export_bundle_rejects_old_unprefixed_bark_slot(self):
        _plan, report = self.normalize()
        export = self.root / "export"
        export.mkdir()
        fbx = export / "branch_elm_01.fbx"
        stmat = export / "branch_elm_01.stmat"
        xml = export / "branch_elm_01.xml"
        write_ascii_fbx(fbx, material_name="Bark_elm_01_Mat")
        write_export_xml(stmat, material="Bark_elm_01_Mat")
        write_export_xml(xml, material="Bark_elm_01_Mat")

        with self.assertRaisesRegex(
                BarkNormalizationError, "not connected to a mesh"):
            validate_canonical_bark_export_bundle(fbx, stmat, xml, report)

    def test_export_bundle_fails_closed_on_stale_stmat_texture(self):
        _plan, report = self.normalize()
        export = self.root / "export"
        export.mkdir()
        fbx = export / "branch_elm_01.fbx"
        stmat = export / "branch_elm_01.stmat"
        xml = export / "branch_elm_01.xml"
        write_ascii_fbx(fbx)
        write_export_xml(stmat, textures=("Nothofagus_bark.tga",))
        write_export_xml(xml)

        with self.assertRaisesRegex(
                BarkNormalizationError, "another texture family"):
            validate_canonical_bark_export_bundle(fbx, stmat, xml, report)

    @unittest.skipUnless(
        REAL_CANONICAL_SPM.is_file()
        and all(path.is_file() for path in REAL_CLUSTER_SPMS),
        "Tree Elm authoring sources unavailable",
    )
    def test_real_elm_bark_receipt_normalizes_only_temporary_copies(self):
        source_hashes = {path: sha256(path) for path in REAL_CLUSTER_SPMS}
        canonical_row = next(
            row for row in extract_material_image_refs(REAL_CANONICAL_SPM)
            if row["material_id"] == "1"
        )
        copy_root = self.isolation / "Tree_elm"
        texture_dir = copy_root / "texture"
        cluster_dir = copy_root / "Cluster"
        texture_dir.mkdir(parents=True, exist_ok=True)
        cluster_dir.mkdir(parents=True, exist_ok=True)
        for ref in canonical_row["refs"]:
            source = REAL_CANONICAL_SPM.parent / ref.replace("\\", "/")
            shutil.copy2(source, texture_dir / source.name)
        isolated = {}
        cluster_rows = []
        for source in REAL_CLUSTER_SPMS:
            target = cluster_dir / source.name
            shutil.copy2(source, target)
            isolated[str(source)] = str(target)
            bark_row = next(
                row for row in extract_material_image_refs(source)
                if "bark" in row["material_name"].casefold()
            )
            cluster_rows.append({
                "cluster_spm": str(source),
                "material_name": bark_row["material_name"],
                "replacement": "required",
            })
        receipt = {
            "canonical_bark": {
                "status": "replacement_required",
                "canonical_material": "M_bark_elm_01",
                "canonical_sources": [{
                    "spm": str(REAL_CANONICAL_SPM),
                    "material_id": canonical_row["material_id"],
                    "material_name": canonical_row["material_name"],
                }],
                "cluster_bark_sources": cluster_rows,
            },
        }

        plan = build_isolated_bark_normalization_plan(
            receipt, isolated, self.isolation)
        report = apply_isolated_bark_normalization(plan)

        self.assertEqual(len(report["outputs"]), 3)
        for source, original_hash in source_hashes.items():
            self.assertEqual(sha256(source), original_hash)
            target_rows = extract_material_image_refs(Path(isolated[str(source)]))
            bark = next(
                row for row in target_rows
                if row["material_name"].casefold() == "m_bark_elm_01"
            )
            self.assertEqual(
                {Path(value).name.casefold() for value in bark["refs"]},
                {Path(value).name.casefold() for value in canonical_row["refs"]},
            )

    @unittest.skipUnless(
        REAL_CANONICAL_SPM.is_file()
        and all(path.is_file() for path in REAL_CLUSTER_SPMS),
        "Tree Elm authoring sources unavailable",
    )
    def test_real_elm_m_only_stage_is_compatible_with_canonical_bark_stage(self):
        production_hashes = {
            path: sha256(path) for path in REAL_CLUSTER_SPMS
        }
        m_only_root = self.root / "m-only-source" / "Tree_elm"
        target_root = self.root / "canonical-target" / "Tree_elm"
        m_only_cluster = m_only_root / "Cluster"
        target_cluster = target_root / "Cluster"
        m_only_cluster.mkdir(parents=True)
        target_cluster.mkdir(parents=True)

        canonical_row = next(
            row for row in extract_material_image_refs(REAL_CANONICAL_SPM)
            if row["material_id"] == "1"
        )
        for ref in canonical_row["refs"]:
            source = REAL_CANONICAL_SPM.parent / ref.replace("\\", "/")
            destination = target_root / ref.replace("\\", "/")
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)

        isolated = {}
        cluster_rows = []
        for production in REAL_CLUSTER_SPMS:
            m_only = m_only_cluster / production.name
            target = target_cluster / production.name
            shutil.copy2(production, m_only)
            prepared = prepare_sk(
                m_only_cluster, [m_only.stem], dry_run=False
            )["targets"][0]
            self.assertEqual(prepared["status"], "prepared")
            shutil.copy2(m_only, target)
            isolated[str(m_only)] = str(target)
            bark = next(
                row for row in extract_material_image_refs(m_only)
                if "bark" in row["material_name"].casefold()
            )
            cluster_rows.append({
                "cluster_spm": str(m_only),
                "material_name": bark["material_name"],
                "replacement": "required",
            })

            active = active_material_ids(m_only)
            active_names = [
                row["material_name"]
                for row in extract_material_image_refs(m_only)
                if row["material_id"] in active
            ]
            self.assertTrue(active_names)
            self.assertTrue(all(
                name.casefold().startswith("m_")
                for name in active_names
            ))

        receipt = {
            "canonical_bark": {
                "status": "replacement_required",
                "canonical_material": "M_bark_elm_01",
                "canonical_sources": [{
                    "spm": str(REAL_CANONICAL_SPM),
                    "material_id": canonical_row["material_id"],
                    "material_name": canonical_row["material_name"],
                }],
                "cluster_bark_sources": cluster_rows,
            },
        }
        plan = build_isolated_bark_normalization_plan(
            receipt,
            isolated,
            self.root / "canonical-target",
        )
        report = apply_isolated_bark_normalization(plan)

        self.assertEqual(report["status"], "normalized")
        self.assertEqual(len(report["outputs"]), len(REAL_CLUSTER_SPMS))
        for production, expected_hash in production_hashes.items():
            self.assertEqual(sha256(production), expected_hash)
            target = Path(isolated[str(m_only_cluster / production.name)])
            bark = next(
                row for row in extract_material_image_refs(target)
                if "bark" in row["material_name"].casefold()
            )
            self.assertEqual(bark["material_name"], "M_bark_elm_01")


if __name__ == "__main__":
    unittest.main()
