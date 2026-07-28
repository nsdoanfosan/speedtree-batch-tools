import argparse
import gzip
import json
import os
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest import mock


REPO = Path(__file__).resolve().parents[2]
SK_DIR = REPO / "sk_batch"
JOBS_DIR = SK_DIR / "jobs"
for path in (REPO, SK_DIR, JOBS_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import speedtree_material_preflight as preflight
from speedtree_texture_contract import REQUIRED_TEXTURE_ROLES


def write_spm(path, mesh_filenames=()):
    model = ET.Element("SpeedTreeModel")
    assets = ET.SubElement(model, "Assets")
    for material_id, name, mesh_id in (
        (1, "M_leaf_grass_dead", 11),
        (2, "M_stem_common_01", 12),
    ):
        material = ET.SubElement(
            assets, "Material_v8", ID=str(material_id), Name=name
        )
        ET.SubElement(material, "CutoutMeshID").text = str(mesh_id)
        ET.SubElement(assets, "Mesh", ID=str(mesh_id), Name=f"mesh_{mesh_id}")
    for index, filename in enumerate(mesh_filenames, 90):
        mesh = ET.SubElement(
            assets, "Mesh", ID=str(index), Name=f"plate_{index}"
        )
        ET.SubElement(mesh, "Filename").text = filename
        ET.SubElement(mesh, "Embedded").text = "false"

    tree = ET.SubElement(model, "Generator", Type="Tree")
    properties = ET.SubElement(tree, "Properties")
    prop = ET.SubElement(properties, "Property")
    ET.SubElement(prop, "Name").text = "SpeedTree SDK:User data"
    ET.SubElement(prop, "Value").text = ""

    for generator_type, guid, property_name, material_id, mesh_id in (
        ("Leaf Mesh", "leaf-guid", "Leaves:Type:0", 1, 11),
        ("Branch", "stem-guid", "Branches", 2, None),
    ):
        generator = ET.SubElement(model, "Generator", Type=generator_type)
        ET.SubElement(generator, "GUID").text = guid
        ET.SubElement(generator, "Name").text = guid
        ET.SubElement(generator, "Hidden").text = "false"
        properties = ET.SubElement(generator, "Properties")
        values = [("Material", material_id)]
        if mesh_id is not None:
            values.append(("Mesh", mesh_id))
        for suffix, value in values:
            prop = ET.SubElement(properties, "Property")
            ET.SubElement(prop, "Name").text = f"{property_name}:{suffix}"
            ET.SubElement(prop, "Value").text = str(value)
        node = ET.SubElement(model, "Node", Type=generator_type)
        ET.SubElement(node, "GeneratorGUID").text = guid
        ET.SubElement(node, "ParentGUID").text = "parent"
        ET.SubElement(node, "Name").text = guid + "-node"
        ET.SubElement(node, "GUID").text = guid + "-node"
        ET.SubElement(node, "Hidden").text = "false"
        extra = ET.SubElement(node, "Extra")
        ET.SubElement(extra, "m_bDeleted").text = "false"
        ET.SubElement(extra, "m_bCulled").text = "false"

    path.write_bytes(gzip.compress(ET.tostring(model, encoding="utf-8")))


def write_stmat(spm, material_names):
    texture_dir = spm.parent / "texture"
    texture_dir.mkdir(parents=True, exist_ok=True)
    sources = {}
    for role in REQUIRED_TEXTURE_ROLES:
        source = texture_dir / f"T_grass_shared_{role}.tga"
        source.write_bytes(role.encode("ascii"))
        sources[role] = source
    root = ET.Element("SpeedTreeMaterials")
    for index, name in enumerate(material_names, 1):
        material = ET.SubElement(root, "Material", ID=str(index), Name=name)
        for role, source in sources.items():
            ET.SubElement(material, "Map", Name=role, Source=str(source))
    stmat = spm.parent / "fbx" / f"{spm.stem}.stmat"
    stmat.parent.mkdir(parents=True, exist_ok=True)
    ET.ElementTree(root).write(stmat, encoding="utf-8", xml_declaration=True)
    now = max(spm.stat().st_mtime_ns, stmat.stat().st_mtime_ns)
    os.utime(spm, ns=(now, now))
    os.utime(stmat, ns=(now + 1, now + 1))
    return stmat


class SpeedTreeMaterialPreflightTests(unittest.TestCase):
    def test_marker_only_atlas_ownership_emits_the_shadow_issue(self):
        issues = preflight.preflight_contract_issues({
            "spm": "SK_atlas.spm",
            "leaf_reference_contract": {
                "status": "managed_connected",
                "managed_ownership_provenance": {
                    "status": "marker_only",
                    "material_names": ["M_leaf_atlas_green"],
                    "reason": "strict ownership was not proven",
                },
            },
        })

        self.assertIn(
            {
                "code": "ATLAS_OWNERSHIP_PROVENANCE_MISMATCH",
                "severity": "warning",
            },
            [
                {"code": issue["code"], "severity": issue["severity"]}
                for issue in issues
            ],
        )

    def run_preflight(self, spm, report_path):
        args = argparse.Namespace(
            spm=str(spm),
            speedtree_exe="SpeedTree.exe",
            fbx_ini="Options.ini",
            speedtree_cli="speedtree_cli.py",
            report=str(report_path),
            timeout=30,
        )
        exited = False
        with mock.patch.object(preflight, "parse_args", return_value=args), mock.patch.object(
            preflight, "load_speedtree_cli", return_value=object()
        ), mock.patch.object(
            preflight,
            "run_export",
            return_value={"status": "cached", "exists": True, "size": 1},
        ) as export_mock:
            try:
                preflight.main()
            except SystemExit:
                exited = True
        return exited, export_mock

    def test_report_contains_versioned_sources_and_authoritative_bindings(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            spm = root / "SK_grass.spm"
            report_path = root / "report.json"
            write_spm(spm)
            write_stmat(
                spm,
                ["M_leaf_grass_dead_Mat", "M_stem_common_01_Mat"],
            )

            self.run_preflight(spm, report_path)

            report = json.loads(report_path.read_text(encoding="utf-8"))
            envelope = report["speedtree_pipeline_contract"]
            self.assertEqual(report["status"], "ok")
            self.assertEqual(envelope["outcome"], "ok")
            self.assertRegex(envelope["source"]["spm"]["sha256"], r"^[0-9a-f]{64}$")
            self.assertRegex(
                envelope["source"]["stmat"][0]["sha256"], r"^[0-9a-f]{64}$"
            )
            self.assertEqual(len(envelope["material_intents"]), 2)
            self.assertTrue(
                all(
                    intent["texture_binding"]["texture_base"]
                    == "T_grass_shared"
                    for intent in envelope["material_intents"]
                )
            )
            self.assertTrue(
                all(
                    "stmat_roles" in intent["texture_binding"]
                    for intent in envelope["material_intents"]
                )
            )

    def test_missing_mesh_file_blocks_before_speedtree_export(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            spm = root / "SK_fern_missing_mesh.spm"
            report_path = root / "report.json"
            mesh_dir = root / "meshes"
            mesh_dir.mkdir()
            (mesh_dir / "01_leaf_present.fbx").write_bytes(b"fbx")
            write_spm(
                spm,
                mesh_filenames=(
                    "meshes/01_leaf_present.fbx",
                    "meshes/18_leaf_gone.fbx",
                ),
            )
            write_stmat(
                spm,
                ["M_leaf_grass_dead_Mat", "M_stem_common_01_Mat"],
            )

            exited, export_mock = self.run_preflight(spm, report_path)

            self.assertTrue(exited)
            export_mock.assert_not_called()
            report = json.loads(report_path.read_text(encoding="utf-8"))
            envelope = report["speedtree_pipeline_contract"]
            self.assertEqual(report["status"], "blocked")
            self.assertEqual(envelope["outcome"], "blocked")
            self.assertIn("18_leaf_gone.fbx", report["error"])
            self.assertEqual(
                report["classification"],
                "asset_external_mesh_path_missing",
            )
            self.assertIn("relink", report["remediation"].casefold())
            self.assertEqual(
                report["missing_external_meshes"][0]["filename"],
                "meshes/18_leaf_gone.fbx",
            )
            self.assertNotIn("speedtree_export", report)
            self.assertIn(
                "SPM_MESH_FILE_MISSING",
                {issue["code"] for issue in envelope["issues"]},
            )
            contract = report["mesh_file_reference_contract"]
            self.assertEqual(contract["status"], "missing_mesh_files")
            self.assertEqual(
                [row["filename"] for row in contract["missing"]],
                ["meshes/18_leaf_gone.fbx"],
            )

    def test_existing_mesh_files_do_not_block_the_export(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            spm = root / "SK_fern_meshes_ok.spm"
            report_path = root / "report.json"
            mesh_dir = root / "meshes"
            mesh_dir.mkdir()
            (mesh_dir / "01_leaf_present.fbx").write_bytes(b"fbx")
            write_spm(spm, mesh_filenames=("meshes/01_leaf_present.fbx",))
            write_stmat(
                spm,
                ["M_leaf_grass_dead_Mat", "M_stem_common_01_Mat"],
            )

            exited, export_mock = self.run_preflight(spm, report_path)

            self.assertFalse(exited)
            export_mock.assert_called_once()
            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(report["status"], "ok")
            self.assertEqual(
                report["mesh_file_reference_contract"]["status"], "ok"
            )

    def test_missing_visible_stem_blocks_with_all_export_issue(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            spm = root / "SK_grass_missing_stem.spm"
            report_path = root / "report.json"
            write_spm(spm)
            write_stmat(spm, ["M_leaf_grass_dead_Mat"])
            source_before = spm.read_bytes()

            exited, _export_mock = self.run_preflight(spm, report_path)

            self.assertTrue(exited)
            report = json.loads(report_path.read_text(encoding="utf-8"))
            envelope = report["speedtree_pipeline_contract"]
            self.assertEqual(report["status"], "blocked")
            self.assertEqual(envelope["outcome"], "blocked")
            self.assertIn(
                "ALL_EXPORT_MATERIAL_MISSING",
                {issue["code"] for issue in envelope["issues"]},
            )
            self.assertEqual(
                report["all_export_material_contract"]["missing_materials"],
                ["M_stem_common_01"],
            )
            self.assertEqual(
                report["classification"],
                "asset_export_material_missing",
            )
            self.assertEqual(
                report["missing_export_materials"],
                ["M_stem_common_01"],
            )
            self.assertIn("assign", report["remediation"].casefold())
            self.assertEqual(spm.read_bytes(), source_before)
            self.assertFalse(report["problem_node_marker"]["changed"])
            self.assertEqual(
                report["problem_node_marker"]["status"], "reported_only"
            )

    def test_textureless_stmat_remains_blocked_after_material_name_match(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            spm = root / "SK_reed_textureless.spm"
            report_path = root / "report.json"
            write_spm(spm)
            stmat = root / "fbx" / "SK_reed_textureless.stmat"
            stmat.parent.mkdir(parents=True, exist_ok=True)
            document = ET.Element("SpeedTreeMaterials")
            for index, name in enumerate(
                ("M_leaf_grass_dead_Mat", "M_stem_common_01_Mat"), 1
            ):
                ET.SubElement(
                    document, "Material", ID=str(index), Name=name
                )
            ET.ElementTree(document).write(
                stmat, encoding="utf-8", xml_declaration=True
            )
            now = max(spm.stat().st_mtime_ns, stmat.stat().st_mtime_ns)
            os.utime(spm, ns=(now, now))
            os.utime(stmat, ns=(now + 1, now + 1))

            exited, export_mock = self.run_preflight(spm, report_path)

            self.assertTrue(exited)
            export_mock.assert_called_once()
            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(report["status"], "blocked")
            self.assertEqual(
                report["speedtree_pipeline_contract"]["outcome"], "blocked"
            )
            self.assertEqual(
                report["texture_source_contract"]["status"],
                "missing_sources",
            )
            self.assertEqual(
                report["classification"],
                "asset_texture_source_undeclared",
            )
            self.assertIn("M_leaf_grass_dead_Mat", report["error"])
            self.assertIn("<Source 미지정>", report["error"])
            self.assertIn(
                "TEXTURE_SOURCE_MISSING",
                {
                    issue["code"]
                    for issue in report["speedtree_pipeline_contract"]["issues"]
                },
            )

if __name__ == "__main__":
    unittest.main()
