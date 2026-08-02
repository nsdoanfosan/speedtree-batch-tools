import hashlib
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from cluster_atlas_source_index import (
    ClusterAtlasSourceIndexError,
    bind_index_to_export_results,
    build_current_atlas_source_index,
    inspect_persisted_source_index,
    resolve_authoritative_collection,
)


class Collections(list):
    def get(self, name):
        return next((item for item in self if item.name == name), None)


class Collection(dict):
    def __init__(self, name, scope):
        super().__init__(atlas_leaf_speedtree_scope_id=scope)
        self.name = name
        self.groups = []


class Vertex:
    def __init__(self, *co):
        self.co = co


class Loop:
    def __init__(self, vertex_index, edge_index):
        self.vertex_index = vertex_index
        self.edge_index = edge_index


class Edge:
    def __init__(self, first, second):
        self.vertices = (first, second)


class Polygon:
    def __init__(self, loop_start, loop_total, material_index=0):
        self.loop_start = loop_start
        self.loop_total = loop_total
        self.material_index = material_index
        self.use_smooth = False


class Mesh:
    def __init__(self, name, *, offset=0.0):
        self.name = name
        self.vertices = [
            Vertex(offset, 0.0, 0.0),
            Vertex(1.0, 0.0, 0.0),
            Vertex(0.0, 1.0, 0.0),
        ]
        self.edges = [Edge(0, 1), Edge(1, 2), Edge(2, 0)]
        self.loops = [Loop(0, 0), Loop(1, 1), Loop(2, 2)]
        self.polygons = [Polygon(0, 3)]
        self.uv_layers = []
        self.color_attributes = []
        self.corner_normals = []


class Object(dict):
    def __init__(self, name, collection, *, prototype_index, offset=0.0):
        source_contract = {
            "source_spm": "SK_cluster.spm",
            "source_spm_sha256": hashlib.sha256(b"spm").hexdigest(),
            "source_fbx": "SK_cluster.fbx",
            "source_fbx_sha256": hashlib.sha256(b"fbx").hexdigest(),
        }
        super().__init__(
            speedtree_cluster_prototype_index=prototype_index,
            speedtree_cluster_prototype_asset=f"SK_cluster_{prototype_index:02d}",
            speedtree_cluster_source_object="Cluster_Source",
            speedtree_cluster_source_partition_mode="PER_CONNECTED_DEFORM_CLUSTER",
            speedtree_cluster_source_bone=f"Bone.{prototype_index:03d}",
            speedtree_cluster_source_3d_contract=__import__("json").dumps(
                source_contract, sort_keys=True
            ),
        )
        self.name = name
        self.data = Mesh(name + "_Mesh", offset=offset)
        self.matrix_world = (
            (1.0, 0.0, 0.0, 0.0),
            (0.0, 1.0, 0.0, 0.0),
            (0.0, 0.0, 1.0, 0.0),
            (0.0, 0.0, 0.0, 1.0),
        )
        self.material_slots = []
        self.users_collection = [collection]


class ClusterAtlasSourceIndexTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.blend = self.root / "SK_cluster.blend"
        self.blend.write_bytes(b"saved-blender-source")
        self.scope = "scope-cluster-001"
        self.collection = Collection("Atlas_Branch_Plans", self.scope)
        self.bpy = types.SimpleNamespace(
            data=types.SimpleNamespace(
                filepath=str(self.blend),
                is_dirty=False,
                collections=Collections([self.collection]),
                meshes=types.SimpleNamespace(),
            ),
            context=types.SimpleNamespace(),
        )
        self.index_calls = []

    def tearDown(self):
        self.temporary.cleanup()

    def object(self, name, prototype_index, *, offset=0.0):
        return Object(
            name,
            self.collection,
            prototype_index=prototype_index,
            offset=offset,
        )

    def modules(self):
        package = types.ModuleType("atlas_leaf_mesh_builder")
        package.__path__ = []
        source_index = types.ModuleType(
            "atlas_leaf_mesh_builder.source_index"
        )
        speedtree = types.ModuleType("atlas_leaf_mesh_builder.speedtree")

        def current_blend_source_index(
            *, expected_blend_path=None, bpy_module=None
        ):
            self.index_calls.append(
                (Path(expected_blend_path), bpy_module)
            )
            digest = hashlib.sha256(self.blend.read_bytes()).hexdigest()
            return {
                "schema_version": 1,
                "status": "ok",
                "indexed_by_blender": True,
                "blend": str(self.blend),
                "blend_sha256": digest,
                "image_count": 0,
                "images": [],
            }

        def grouped_source_objects(
            collection,
            atlas_asset_name=None,
            *,
            preserve_explicit_material_name=False,
        ):
            self.assertEqual(atlas_asset_name, "M_cluster")
            self.assertTrue(preserve_explicit_material_name)
            return [
                {
                    "collection": group_name,
                    "material": material_name,
                    "objects": list(objects),
                }
                for group_name, material_name, objects
                in collection.groups
            ]

        source_index.current_blend_source_index = (
            current_blend_source_index
        )
        speedtree.grouped_source_objects = grouped_source_objects
        return {
            "atlas_leaf_mesh_builder": package,
            "atlas_leaf_mesh_builder.source_index": source_index,
            "atlas_leaf_mesh_builder.speedtree": speedtree,
        }

    def build(self):
        with mock.patch.dict(sys.modules, self.modules()):
            return build_current_atlas_source_index(
                self.blend,
                "Atlas_Branch_Plans",
                atlas_asset_name="M_cluster",
                expected_scope_id=self.scope,
                bpy_module=self.bpy,
            )

    def test_uses_atlas_saved_source_index_and_binds_actual_export(self):
        first = self.object("Card_A", 1)
        second = self.object("Card_B", 2)
        self.collection.groups = [
            ("Atlas_Branch_Plans", "M_cluster", [first, second])
        ]

        index = self.build()

        self.assertEqual(self.index_calls, [(self.blend, self.bpy)])
        self.assertEqual(index["status"], "ok")
        collection = index["authoritative_collection"]
        self.assertEqual(collection["mesh_object_count"], 2)
        self.assertEqual(collection["state"], "populated")
        self.assertTrue(all(
            row["stable_source_identity"]["digest"]
            for row in collection["mesh_objects"]
        ))
        target = self.root / "SK_tree.spm"
        exported_meshes = [
            {
                "source_object": "Card_A",
                "source_collection": "Atlas_Branch_Plans",
                "material": "M_cluster",
            },
            {
                "source_object": "Card_B",
                "source_collection": "Atlas_Branch_Plans",
                "material": "M_cluster",
            },
        ]
        manifest_path = self.root / "target_manifest.json"
        manifest_path.write_text(json.dumps({
            "spm": str(target),
            "blend_file": str(self.blend),
            "source_collection": "Atlas_Branch_Plans",
            "export_scope_id": self.scope,
            "meshes": exported_meshes,
        }), encoding="utf-8")
        bound = bind_index_to_export_results(index, [{
            "spm_path": str(target),
            "manifest_path": str(manifest_path),
            "exported_meshes": exported_meshes,
        }])
        inspected = inspect_persisted_source_index(self.blend, bound)
        self.assertTrue(inspected["current"])
        self.assertEqual(
            bound["publication"]["status"], "bound"
        )

    def test_collection_key_tracks_add_delete_rename_regroup_and_geometry(self):
        first = self.object("Card_A", 1)
        second = self.object("Card_B", 2)
        self.collection.groups = [
            ("Atlas_Branch_Plans", "M_cluster", [first, second])
        ]
        baseline = self.build()["authoritative_collection"][
            "content_key"
        ]["digest"]

        scenarios = {}
        third = self.object("Card_C", 3)
        scenarios["add"] = [
            ("Atlas_Branch_Plans", "M_cluster", [first, second, third])
        ]
        scenarios["delete"] = [
            ("Atlas_Branch_Plans", "M_cluster", [first])
        ]
        renamed = self.object("Card_A_Renamed", 1)
        scenarios["rename"] = [
            ("Atlas_Branch_Plans", "M_cluster", [renamed, second])
        ]
        scenarios["regroup"] = [
            ("Green", "M_cluster_green", [first]),
            ("Stem", "M_cluster_stem", [second]),
        ]
        changed_geometry = self.object("Card_A", 1, offset=0.25)
        scenarios["geometry"] = [
            (
                "Atlas_Branch_Plans",
                "M_cluster",
                [changed_geometry, second],
            )
        ]
        for label, groups in scenarios.items():
            with self.subTest(label=label):
                self.collection.groups = groups
                changed = self.build()["authoritative_collection"]
                self.assertNotEqual(
                    changed["content_key"]["digest"], baseline
                )

    def test_mesh_key_tracks_edge_topology_even_when_counts_match(self):
        source = self.object("Card_A", 1)
        self.collection.groups = [
            ("Atlas_Branch_Plans", "M_cluster", [source])
        ]
        baseline = self.build()["authoritative_collection"][
            "mesh_objects"
        ][0]["mesh_content_key"]["digest"]
        source.data.edges = [
            Edge(0, 2), Edge(2, 1), Edge(1, 0)
        ]

        changed = self.build()["authoritative_collection"][
            "mesh_objects"
        ][0]["mesh_content_key"]["digest"]

        self.assertNotEqual(changed, baseline)

    def test_final_delete_publishes_explicit_empty_tombstone(self):
        self.collection.groups = [
            ("Atlas_Branch_Plans", "M_cluster", [])
        ]
        index = self.build()
        collection = index["authoritative_collection"]
        self.assertEqual(collection["state"], "empty")
        self.assertEqual(collection["mesh_object_count"], 0)

        target = self.root / "SK_tree.spm"
        manifest_path = self.root / "empty_target_manifest.json"
        manifest_path.write_text(json.dumps({
            "spm": str(target),
            "blend_file": str(self.blend),
            "source_collection": "Atlas_Branch_Plans",
            "export_scope_id": self.scope,
            "meshes": [],
            "texture_contract_status": "collection_tombstone",
            "collection_tombstone": {
                "state": "empty",
                "reason": "no_live_mesh_objects",
            },
        }), encoding="utf-8")
        bound = bind_index_to_export_results(index, [{
            "spm_path": str(target),
            "manifest_path": str(manifest_path),
            "exported_meshes": [],
        }])
        self.assertTrue(
            inspect_persisted_source_index(self.blend, bound)["current"]
        )

    def test_empty_result_without_tombstone_fails_closed(self):
        self.collection.groups = []
        index = self.build()
        with self.assertRaises(
            ClusterAtlasSourceIndexError,
            msg="empty publication must carry a tombstone",
        ):
            bind_index_to_export_results(index, [{
                "spm_path": str(self.root / "SK_tree.spm"),
                "export_scope_id": self.scope,
                "meshes": [],
            }])

    def test_committed_manifest_must_match_export_result(self):
        source = self.object("Card_A", 1)
        self.collection.groups = [
            ("Atlas_Branch_Plans", "M_cluster", [source])
        ]
        index = self.build()
        target = self.root / "SK_tree.spm"
        manifest_path = self.root / "mismatched_manifest.json"
        manifest_path.write_text(json.dumps({
            "spm": str(target),
            "blend_file": str(self.blend),
            "source_collection": "Atlas_Branch_Plans",
            "export_scope_id": self.scope,
            "meshes": [],
        }), encoding="utf-8")

        with self.assertRaises(ClusterAtlasSourceIndexError):
            bind_index_to_export_results(index, [{
                "spm_path": str(target),
                "manifest_path": str(manifest_path),
                "exported_meshes": [{
                    "source_object": "Card_A",
                    "source_collection": "Atlas_Branch_Plans",
                    "material": "M_cluster",
                }],
            }])

    def test_missing_stable_object_identity_is_ambiguous(self):
        source = self.object("Card_A", 1)
        source.clear()
        self.collection.groups = [
            ("Atlas_Branch_Plans", "M_cluster", [source])
        ]
        index = self.build()
        self.assertEqual(index["status"], "ambiguous")
        self.assertIn(
            "stable_source_object_identity_missing:Card_A",
            index["refresh_reasons"],
        )
        with self.assertRaises(ClusterAtlasSourceIndexError):
            bind_index_to_export_results(index, [])

    def test_cloned_object_cannot_reuse_stable_source_identity(self):
        first = self.object("Card_A", 1)
        clone = self.object("Card_A_Clone", 1)
        self.collection.groups = [
            ("Atlas_Branch_Plans", "M_cluster", [first, clone])
        ]

        index = self.build()

        self.assertEqual(index["status"], "ambiguous")
        self.assertTrue(any(
            reason.startswith(
                "stable_source_object_identity_duplicate:"
            )
            for reason in index["refresh_reasons"]
        ))
        with self.assertRaises(ClusterAtlasSourceIndexError):
            bind_index_to_export_results(index, [])

    def test_collection_rename_uses_unique_scope_and_duplicate_scope_fails(self):
        self.collection.name = "Renamed_Plans"
        found = resolve_authoritative_collection(
            self.bpy,
            "Atlas_Branch_Plans",
            expected_scope_id=self.scope,
        )
        self.assertIs(found, self.collection)
        duplicate = Collection("Copied_Plans", self.scope)
        self.bpy.data.collections.append(duplicate)
        with self.assertRaises(ClusterAtlasSourceIndexError):
            resolve_authoritative_collection(
                self.bpy,
                "Atlas_Branch_Plans",
                expected_scope_id=self.scope,
            )

    def test_copied_blend_receipt_cannot_authorize_noop(self):
        source = self.object("Card_A", 1)
        self.collection.groups = [
            ("Atlas_Branch_Plans", "M_cluster", [source])
        ]
        index = self.build()
        bound = bind_index_to_export_results(index, [{
            "spm_path": str(self.root / "SK_tree.spm"),
            "export_scope_id": self.scope,
            "meshes": [{
                "source_object": "Card_A",
                "source_collection": "Atlas_Branch_Plans",
                "material": "M_cluster",
            }],
        }])
        copied = self.root / "Copy" / self.blend.name
        copied.parent.mkdir()
        copied.write_bytes(self.blend.read_bytes())

        inspected = inspect_persisted_source_index(copied, bound)

        self.assertFalse(inspected["current"])
        self.assertIn(
            "blender_source_path_changed",
            inspected["refresh_reasons"],
        )
        self.assertIn(
            "atlas_blender_source_index_invalid",
            inspected["refresh_reasons"],
        )


if __name__ == "__main__":
    unittest.main()
