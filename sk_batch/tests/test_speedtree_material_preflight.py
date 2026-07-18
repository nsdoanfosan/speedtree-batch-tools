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


def write_spm(path):
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
        with mock.patch.object(preflight, "parse_args", return_value=args), mock.patch.object(
            preflight, "load_speedtree_cli", return_value=object()
        ), mock.patch.object(
            preflight, "run_export", return_value={"status": "cached"}
        ):
            preflight.main()

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

    def test_missing_visible_stem_blocks_with_all_export_issue(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            spm = root / "SK_grass_missing_stem.spm"
            report_path = root / "report.json"
            write_spm(spm)
            write_stmat(spm, ["M_leaf_grass_dead_Mat"])
            source_before = spm.read_bytes()

            with self.assertRaises(SystemExit):
                self.run_preflight(spm, report_path)

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
            self.assertEqual(spm.read_bytes(), source_before)
            self.assertFalse(report["problem_node_marker"]["changed"])
            self.assertEqual(
                report["problem_node_marker"]["status"], "reported_only"
            )


if __name__ == "__main__":
    unittest.main()
