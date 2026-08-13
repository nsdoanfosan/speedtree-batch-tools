import sys
import types
import unittest
from pathlib import Path


SK_BATCH_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SK_BATCH_DIR))

from repair_push_evidence import export_object_postcondition  # noqa: E402


class FakeMaterial:
    def __init__(self, name):
        self.name = name
        self.use_nodes = False
        self.node_tree = None


class FakeMesh:
    def __init__(self, material_name="M_Bark"):
        self.materials = [FakeMaterial(material_name)]
        self.vertices = [object(), object(), object()]
        self.edges = [object(), object(), object()]
        self.polygons = [
            types.SimpleNamespace(vertices=(0, 1, 2), material_index=0)
        ]
        self.uv_layers = []
        self.color_attributes = []


class FakeObject(dict):
    def __init__(self, name, *, material_name="M_Bark"):
        super().__init__()
        self.name = name
        self.type = "MESH"
        self.parent = None
        self.children = []
        self.data = FakeMesh(material_name)


class FakeBlenderData:
    def __init__(self, objects):
        self.collections = {
            "Export": types.SimpleNamespace(all_objects=list(objects))
        }


class ExportPostconditionTests(unittest.TestCase):
    def test_snapshot_is_deterministic_diagnostic_data(self):
        scene = FakeBlenderData([
            FakeObject("SK_Tree_02", material_name="M_Leaf"),
            FakeObject("SK_Tree_01"),
        ])

        first = export_object_postcondition(scene)
        second = export_object_postcondition(scene)

        self.assertEqual(first, second)
        self.assertEqual(
            [row["name"] for row in first["objects"]],
            ["SK_Tree_01", "SK_Tree_02"],
        )
        self.assertEqual(first["empty_material_slots"], [])
        self.assertEqual(len(first["content_sha256"]), 64)

    def test_missing_export_collection_is_a_repair_error(self):
        scene = types.SimpleNamespace(collections={})

        with self.assertRaisesRegex(RuntimeError, "missing Export collection"):
            export_object_postcondition(scene)


if __name__ == "__main__":
    unittest.main()
