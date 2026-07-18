import gzip
import json
import os
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path


SK_BATCH_DIR = Path(__file__).resolve().parents[1]
import sys

if str(SK_BATCH_DIR) not in sys.path:
    sys.path.insert(0, str(SK_BATCH_DIR))

from spm_leaf_handoff_contract import (  # noqa: E402
    inspect_all_speedtree_material_export,
    inspect_speedtree_material_export,
    inspect_speedtree_texture_sources,
    inspect_spm_leaf_contract,
)


def add_material(assets, material_id, name, mesh_ids, managed=False):
    material = ET.SubElement(
        assets, "Material_v8", ID=str(material_id), Name=name
    )
    ET.SubElement(material, "CutoutMeshID").text = str(mesh_ids[0])
    supplemental = ET.SubElement(material, "SupplementalCutoutMeshIDs")
    for mesh_id in mesh_ids[1:]:
        ET.SubElement(supplemental, "CutoutMesh", ID=str(mesh_id))
    if managed:
        ET.SubElement(material, "UserData").text = json.dumps({
            "generator": "Atlas Leaf Mesh Builder",
            "kind": "material",
            "scope": name,
        })
    for mesh_id in mesh_ids:
        ET.SubElement(assets, "Mesh", ID=str(mesh_id), Name=f"mesh_{mesh_id}")


def add_generator(
    model, material_id, mesh_id, guid="leaf-guid", hidden=False,
    generator_type="Leaf Mesh",
):
    generator = ET.SubElement(model, "Generator", Type=generator_type)
    ET.SubElement(generator, "GUID").text = guid
    ET.SubElement(generator, "Name").text = guid
    ET.SubElement(generator, "Hidden").text = "true" if hidden else "false"
    properties = ET.SubElement(generator, "Properties")
    for suffix, value in (("Material", material_id), ("Mesh", mesh_id)):
        prop = ET.SubElement(properties, "Property")
        ET.SubElement(prop, "Name").text = f"Leaves:Type:0:{suffix}"
        ET.SubElement(prop, "Value").text = str(value)


def add_node(model, generator_guid, node_guid, hidden=False, deleted=False, culled=False):
    node = ET.SubElement(model, "Node", Type="Leaf")
    ET.SubElement(node, "GeneratorGUID").text = generator_guid
    ET.SubElement(node, "ParentGUID").text = "parent-guid"
    ET.SubElement(node, "Name").text = node_guid
    ET.SubElement(node, "GUID").text = node_guid
    ET.SubElement(node, "Hidden").text = "true" if hidden else "false"
    extra = ET.SubElement(node, "Extra")
    ET.SubElement(extra, "m_bDeleted").text = "true" if deleted else "false"
    ET.SubElement(extra, "m_bCulled").text = "true" if culled else "false"


def write_spm(path, materials, generators=(), nodes=(), compressed=True):
    model = ET.Element("SpeedTreeModel")
    assets = ET.SubElement(model, "Assets")
    for material in materials:
        add_material(assets, *material)
    for generator in generators:
        add_generator(model, *generator)
    for node in nodes:
        add_node(model, *node)
    payload = ET.tostring(model, encoding="utf-8")
    path.write_bytes(gzip.compress(payload) if compressed else payload)


def write_stmat(path, material_names):
    root = ET.Element("SpeedTreeMaterials")
    for index, name in enumerate(material_names, 1):
        ET.SubElement(root, "Material", ID=str(index), Name=name)
    path.parent.mkdir(parents=True, exist_ok=True)
    ET.ElementTree(root).write(path, encoding="utf-8", xml_declaration=True)


def write_stmat_with_source(path, material_name, source):
    root = ET.Element("SpeedTreeMaterials")
    material = ET.SubElement(root, "Material", ID="1", Name=material_name)
    ET.SubElement(material, "Map", Name="Color", Source=str(source))
    path.parent.mkdir(parents=True, exist_ok=True)
    ET.ElementTree(root).write(path, encoding="utf-8", xml_declaration=True)


class SpmLeafHandoffContractTests(unittest.TestCase):
    def test_plain_xml_spm_uses_the_same_read_only_leaf_contract(self):
        with tempfile.TemporaryDirectory() as temporary:
            spm = Path(temporary) / "SK_plain_leaf.spm"
            write_spm(
                spm,
                [(5, "M_leaf_atlas_green", [11], True)],
                [(5, 11)],
                compressed=False,
            )

            contract = inspect_spm_leaf_contract(spm)

            self.assertEqual(contract["status"], "managed_connected")
            self.assertEqual(contract["visible_managed_slot_count"], 1)

    def test_managed_connections_are_complete_even_with_unused_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            spm = Path(temporary) / "SK_weed_test_01.spm"
            write_spm(
                spm,
                [
                    (5, "M_leaf_atlas_green", [11], True),
                    (6, "M_leaf_atlas_unused", [12], True),
                ],
                [(5, 11)],
            )

            contract = inspect_spm_leaf_contract(spm)

            self.assertEqual(contract["status"], "managed_connected")
            self.assertEqual(contract["managed_slot_count"], 1)
            self.assertEqual(contract["source_slot_count"], 0)
            self.assertEqual(contract["managed_material_count"], 2)
            self.assertEqual(
                contract["managed_ownership_provenance"]["status"],
                "marker_only",
            )

    def test_source_slots_with_managed_outputs_require_replacement(self):
        with tempfile.TemporaryDirectory() as temporary:
            spm = Path(temporary) / "SK_tree_test_01.spm"
            write_spm(
                spm,
                [
                    (2, "M_cluster_source", [2], False),
                    (8, "M_cluster_atlas_01", [18], True),
                ],
                [(2, 2)],
            )

            contract = inspect_spm_leaf_contract(spm)

            self.assertEqual(contract["status"], "replacement_needed")
            self.assertTrue(contract["replacement_needed"])
            self.assertEqual(contract["source_slot_count"], 1)
            self.assertEqual(contract["managed_slot_count"], 0)

    def test_managed_assets_without_semantic_slots_require_connection(self):
        with tempfile.TemporaryDirectory() as temporary:
            spm = Path(temporary) / "SK_weed_test_02.spm"
            write_spm(
                spm,
                [(8, "M_leaf_atlas_01", [18], True)],
            )

            contract = inspect_spm_leaf_contract(spm)

            self.assertEqual(contract["status"], "replacement_needed")
            self.assertEqual(contract["semantic_slot_count"], 0)

    def test_mesh_must_be_owned_by_the_same_material(self):
        with tempfile.TemporaryDirectory() as temporary:
            spm = Path(temporary) / "SK_weed_test_03.spm"
            write_spm(
                spm,
                [
                    (5, "M_leaf_atlas_01", [11], True),
                    (6, "M_leaf_atlas_02", [12], True),
                ],
                [(5, 12)],
            )

            contract = inspect_spm_leaf_contract(spm)

            self.assertEqual(contract["status"], "invalid_references")
            self.assertEqual(contract["invalid_slot_count"], 1)
            self.assertIn("소유 cutout", contract["issues"][0])

    def test_stmat_coverage_accepts_speedtree_mat_suffix_and_reports_missing(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            spm = root / "SK_weed_test_04.spm"
            write_spm(
                spm,
                [(5, "M_leaf_atlas_green", [11], True)],
                [(5, 11)],
            )
            stmat = root / "fbx" / "SK_weed_test_04.stmat"
            write_stmat(stmat, ["M_bark_Mat"])
            now = max(spm.stat().st_mtime_ns, stmat.stat().st_mtime_ns)
            os.utime(spm, ns=(now, now))
            os.utime(stmat, ns=(now + 1, now + 1))

            contract = inspect_spm_leaf_contract(spm)
            missing = inspect_speedtree_material_export(spm, contract)
            self.assertEqual(missing["status"], "missing_materials")
            self.assertEqual(missing["missing_materials"], ["M_leaf_atlas_green"])

            write_stmat(stmat, ["M_leaf_atlas_green_Mat"])
            os.utime(stmat, ns=(now + 2, now + 2))
            ready = inspect_speedtree_material_export(spm, contract)
            self.assertEqual(ready["status"], "ok")
            self.assertEqual(ready["missing_materials"], [])

    def test_all_export_coverage_blocks_missing_visible_stem(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            spm = root / "SK_grass_with_stem.spm"
            model = ET.Element("SpeedTreeModel")
            assets = ET.SubElement(model, "Assets")
            add_material(assets, 5, "M_leaf_grass", [11], True)
            add_material(assets, 6, "M_stem_common_01", [12], False)
            add_generator(model, 5, 11, guid="leaf-guid")
            branch = ET.SubElement(model, "Generator", Type="Branch")
            ET.SubElement(branch, "GUID").text = "stem-guid"
            ET.SubElement(branch, "Name").text = "Stem"
            ET.SubElement(branch, "Hidden").text = "false"
            properties = ET.SubElement(branch, "Properties")
            prop = ET.SubElement(properties, "Property")
            ET.SubElement(prop, "Name").text = "Branches:Material"
            ET.SubElement(prop, "Value").text = "6"
            add_node(model, "leaf-guid", "leaf-node")
            add_node(model, "stem-guid", "stem-node")
            spm.write_bytes(gzip.compress(ET.tostring(model, encoding="utf-8")))

            stmat = root / "fbx" / "SK_grass_with_stem.stmat"
            write_stmat(stmat, ["M_leaf_grass_Mat"])
            now = max(spm.stat().st_mtime_ns, stmat.stat().st_mtime_ns)
            os.utime(spm, ns=(now, now))
            os.utime(stmat, ns=(now + 1, now + 1))

            leaf = inspect_spm_leaf_contract(spm)
            leaf_export = inspect_speedtree_material_export(spm, leaf)
            all_export = inspect_all_speedtree_material_export(spm)

            self.assertEqual(leaf_export["status"], "ok")
            self.assertEqual(all_export["status"], "missing_materials")
            self.assertEqual(all_export["missing_materials"], ["M_stem_common_01"])

    def test_all_export_coverage_does_not_require_unreferenced_materials(self):
        with tempfile.TemporaryDirectory() as temporary:
            spm = Path(temporary) / "SK_unused_material_definition.spm"
            write_spm(
                spm,
                [(5, "M_unused_experiment", [11], False)],
            )

            all_export = inspect_all_speedtree_material_export(spm)

            self.assertEqual(all_export["status"], "not_applicable")
            self.assertEqual(all_export["expected_materials"], [])
            self.assertEqual(
                all_export["warning_code"],
                "ALL_EXPORT_REFERENCE_EVIDENCE_MISSING",
            )
            self.assertEqual(
                all_export["coverage_confidence"],
                "no_generator_material_references",
            )

    def test_hidden_source_and_ordinary_branch_frond_do_not_request_atlas_replacement(self):
        with tempfile.TemporaryDirectory() as temporary:
            spm = Path(temporary) / "SK_tree_visible_leaf_only.spm"
            write_spm(
                spm,
                [
                    (2, "M_leaf_source", [2], False),
                    (3, "M_branch_tree_01", [3], False),
                    (8, "M_leaf_tree_atlas_01_green", [18], True),
                ],
                [
                    (2, 2, "hidden-leaf", True, "Leaf Mesh"),
                    (3, 3, "visible-branch-frond", False, "Frond"),
                    (8, 18, "visible-managed-leaf", False, "Leaf Mesh"),
                ],
                [
                    ("hidden-leaf", "node-hidden-source"),
                    ("visible-branch-frond", "node-branch-frond"),
                    ("visible-managed-leaf", "node-managed-leaf"),
                ],
            )

            contract = inspect_spm_leaf_contract(spm)

            self.assertEqual(contract["status"], "managed_connected")
            self.assertEqual(contract["replacement_source_slot_count"], 0)
            self.assertEqual(contract["replacement_connected_slot_count"], 1)
            self.assertFalse(contract["replacement_needed"])

    def test_eye_enabled_generator_with_zero_generated_nodes_is_not_exported(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            spm = root / "SK_tree_detached_frond.spm"
            write_spm(
                spm,
                [(5, "M_cluster_detached_01", [5], False)],
                [(5, -10, "detached-frond", False, "Frond")],
                # Any real Node enables node-evidence mode, but none belongs
                # to the detached Frond Generator.
                [("other-generator", "node-other")],
            )
            write_stmat(root / "fbx" / "SK_tree_detached_frond.stmat", ["M_bark_Mat"])

            contract = inspect_spm_leaf_contract(spm)
            exported = inspect_speedtree_material_export(spm, contract)

            self.assertEqual(contract["status"], "no_leaf_slots")
            self.assertEqual(contract["visible_source_slot_count"], 0)
            self.assertEqual(contract["expected_visible_material_names"], [])
            self.assertEqual(exported["status"], "not_applicable")

    def test_visible_leaf_with_generated_node_still_blocks_when_stmat_omits_it(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            spm = root / "SK_tree_real_missing_leaf.spm"
            write_spm(
                spm,
                [(5, "M_leaf_real_01", [5], False)],
                [(5, 5, "visible-leaf")],
                [("visible-leaf", "node-visible-leaf")],
            )
            stmat = root / "fbx" / "SK_tree_real_missing_leaf.stmat"
            write_stmat(stmat, ["M_bark_Mat"])
            now = max(spm.stat().st_mtime_ns, stmat.stat().st_mtime_ns)
            os.utime(spm, ns=(now, now))
            os.utime(stmat, ns=(now + 1, now + 1))

            contract = inspect_spm_leaf_contract(spm)
            exported = inspect_speedtree_material_export(spm, contract)

            self.assertEqual(contract["visible_source_slot_count"], 1)
            self.assertEqual(contract["expected_visible_material_names"], ["M_leaf_real_01"])
            self.assertEqual(exported["status"], "missing_materials")

    def test_stmat_declared_texture_sources_are_checked_before_blender(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            spm = root / "SK_tree_texture_source.spm"
            write_spm(spm, [])
            stmat = root / "fbx" / "SK_tree_texture_source.stmat"
            texture = root / "texture" / "T_leaf_color.tga"
            write_stmat_with_source(stmat, "M_leaf_Mat", texture)
            now = max(spm.stat().st_mtime_ns, stmat.stat().st_mtime_ns)
            os.utime(spm, ns=(now, now))
            os.utime(stmat, ns=(now + 1, now + 1))

            missing = inspect_speedtree_texture_sources(spm)
            self.assertEqual(missing["status"], "missing_sources")
            self.assertEqual(missing["missing_sources"][0]["map"], "Color")

            texture.parent.mkdir()
            texture.write_bytes(b"pixels")
            ready = inspect_speedtree_texture_sources(spm)
            self.assertEqual(ready["status"], "ok")


if __name__ == "__main__":
    unittest.main()
