import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

from cluster_card_pipeline.contract import ContractError
from cluster_card_pipeline.speedtree_verify import _material_geometry, _number_list


PROTOTYPE = {
    "vertex_count": 4,
    "triangle_count": 2,
    "vertices": [[-0.5, 0, 0], [0.5, 0, 0], [0.5, 1, 0], [-0.5, 1, 0]],
    "uvs": [[0, 0], [1, 0], [1, 1], [0, 1]],
    "faces": [[0, 1, 2], [0, 2, 3]],
}


def write_xml(path, second_triangle="0 2 3", z_values="0 0 0 0"):
    root = ET.Element("SpeedTreeRaw")
    materials = ET.SubElement(root, "Materials")
    ET.SubElement(materials, "Material", {"ID": "8", "Name": "M_branch_elm_01_Mat"})
    objects = ET.SubElement(root, "Objects")
    obj = ET.SubElement(objects, "Object", {"Name": "fixture"})
    points = ET.SubElement(obj, "Points")
    ET.SubElement(points, "X").text = "-0.5 0.5 0.5 -0.5 "
    ET.SubElement(points, "Y").text = "0 0 1 1 "
    ET.SubElement(points, "Z").text = z_values + " "
    vertices = ET.SubElement(obj, "Vertices")
    ET.SubElement(vertices, "TexcoordU").text = "0 1 1 0 "
    ET.SubElement(vertices, "TexcoordV").text = "0 0 1 1 "
    triangles = ET.SubElement(obj, "Triangles", {"Material": "8", "Count": "2"})
    ET.SubElement(triangles, "PointIndices").text = f"0 1 2 {second_triangle} "
    ET.SubElement(triangles, "VertexIndices").text = f"0 1 2 {second_triangle} "
    ET.ElementTree(root).write(path, encoding="utf-8", xml_declaration=True)


class SpeedTreeVerifyTests(unittest.TestCase):
    def test_decimal_comma_export_payload_is_supported(self):
        self.assertEqual(_number_list("-0,6311436 0,25"), [-0.6311436, 0.25])

    def test_component_uv_topology_matches_prototype(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "fixture.xml"
            write_xml(path)
            result = _material_geometry(path, "M_branch_elm_01", PROTOTYPE)
            self.assertEqual(result["component_count"], 1)
            self.assertTrue(result["all_component_uv_topology_matches"])

    def test_same_triangle_count_with_wrong_topology_blocks(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "fixture.xml"
            write_xml(path, second_triangle="0 1 3")
            with self.assertRaisesRegex(ContractError, "UV/topology"):
                _material_geometry(path, "M_branch_elm_01", PROTOTYPE)

    def test_generator_deformation_is_diagnostic_not_prototype_drift(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "fixture.xml"
            write_xml(path, z_values="0 0 0.2 0")
            result = _material_geometry(path, "M_branch_elm_01", PROTOTYPE)
            self.assertGreater(
                result["component_summary"]["max_relative_planarity_error"], 0.0
            )


if __name__ == "__main__":
    unittest.main()
