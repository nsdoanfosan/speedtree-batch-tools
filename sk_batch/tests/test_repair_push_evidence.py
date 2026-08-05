import hashlib
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path


SK_BATCH_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SK_BATCH_DIR))

from repair_push_evidence import (  # noqa: E402
    RepairPushEvidenceError,
    build_repair_push_evidence_bundle,
    export_object_postcondition,
    validate_export_object_postcondition,
    validate_repair_push_evidence_bundle,
)


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def identity(path):
    candidate = Path(path)
    return {
        "canonical_path": str(candidate.resolve()),
        "size": candidate.stat().st_size,
        "sha256": sha256(candidate),
    }


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
        export = types.SimpleNamespace(
            all_objects=list(objects),
            objects=list(objects),
        )
        self.collections = {"Export": export}


class RepairPushEvidenceTests(unittest.TestCase):
    def make_bundle(self, root, blender_data):
        root = Path(root)
        queue_spm = root / "SK_Tree.spm"
        blend = root / "SK_Tree.blend"
        stmat = root / "fbx" / "SK_Tree.stmat"
        dependency = root / "Cluster" / "SK_Branch.spm"
        assembly_fbx = root / "assembly" / "SK_Tree_Full.fbx"
        assembly_manifest = root / "assembly" / "assembly.json"
        report = root / "reports" / (
            "SK_Tree_speedtree_repair_pipeline_report_codex.json"
        )
        for path, payload in (
            (queue_spm, b"root-spm"),
            (blend, b"blend-bytes"),
            (stmat, b"stmat-bytes"),
            (dependency, b"dependency-spm"),
            (assembly_fbx, b"assembly-fbx"),
        ):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(payload)
        assembly_manifest.write_text(
            json.dumps({
                "kind": "sk_batch_cluster_nanite_assembly_inputs",
                "full_fbx": {
                    "path": str(assembly_fbx.resolve()),
                    "size": assembly_fbx.stat().st_size,
                    "sha256": sha256(assembly_fbx),
                },
            }),
            encoding="utf-8",
        )
        postcondition = export_object_postcondition(blender_data)
        pipeline = {
            "speedtree_pipeline_contract": {
                "kind": "speedtree_preflight",
                "source": {
                    "spm": identity(queue_spm),
                    "stmat": [identity(stmat)],
                },
            },
            "cluster_assembly_manifest": {
                "status": "ready",
                "manifest": {
                    "path": str(assembly_manifest.resolve()),
                    "size": assembly_manifest.stat().st_size,
                    "sha256": sha256(assembly_manifest),
                },
            },
            "repair_push_export_postcondition": postcondition,
        }
        report.parent.mkdir(parents=True)
        report.write_text(
            json.dumps(pipeline, indent=2),
            encoding="utf-8",
        )
        bundle = build_repair_push_evidence_bundle(
            queue_spm=queue_spm,
            speedtree_spm=queue_spm,
            blend=blend,
            repair_report=report,
            pipeline=pipeline,
            push_dependency_contract={
                "dependency_spms": [str(dependency)],
            },
        )
        return bundle, {
            "queue_spm": queue_spm,
            "blend": blend,
            "material": stmat,
            "dependency": dependency,
            "assembly_fbx": assembly_fbx,
            "assembly_manifest": assembly_manifest,
            "repair_report": report,
        }

    def test_bundle_rehashes_root_dependency_material_and_assembly_bytes(self):
        with tempfile.TemporaryDirectory() as temporary:
            for role in (
                "queue_spm",
                "blend",
                "repair_report",
                "dependency",
                "material",
                "assembly_fbx",
                "assembly_manifest",
            ):
                with self.subTest(role=role):
                    scene = FakeBlenderData([FakeObject("SK_Tree")])
                    bundle, paths = self.make_bundle(
                        Path(temporary) / role,
                        scene,
                    )
                    target = paths[role]
                    original_bytes = target.read_bytes()
                    original_stat = target.stat()
                    changed_bytes = bytearray(original_bytes)
                    changed_bytes[0] ^= 1
                    target.write_bytes(changed_bytes)
                    os.utime(
                        target,
                        ns=(
                            original_stat.st_atime_ns,
                            original_stat.st_mtime_ns,
                        ),
                    )

                    with self.assertRaisesRegex(
                        RepairPushEvidenceError,
                        "content changed after Repair",
                    ):
                        validate_repair_push_evidence_bundle(
                            bundle,
                            expected_queue_spm=paths["queue_spm"],
                        )

                    target.write_bytes(original_bytes)
                validate_repair_push_evidence_bundle(
                    bundle,
                    expected_queue_spm=paths["queue_spm"],
                )

    def test_bundle_contains_assembly_artifact_descriptors(self):
        with tempfile.TemporaryDirectory() as temporary:
            scene = FakeBlenderData([FakeObject("SK_Tree")])
            bundle, paths = self.make_bundle(temporary, scene)

        assembly_paths = {
            Path(row["path"])
            for row in bundle["assembly"]["files"]
        }
        self.assertIn(paths["assembly_fbx"].resolve(), assembly_paths)

    def test_export_postcondition_requires_exact_actual_scene_coverage(self):
        scene = FakeBlenderData([
            FakeObject("SK_Tree_01"),
            FakeObject("SK_Tree_02"),
        ])
        expected = export_object_postcondition(scene)

        validate_export_object_postcondition(expected, scene)
        changed = FakeBlenderData([
            FakeObject("SK_Tree_01"),
            FakeObject("SK_Tree_02", material_name="M_Changed"),
        ])
        with self.assertRaisesRegex(
            RepairPushEvidenceError,
            "does not exactly match",
        ):
            validate_export_object_postcondition(expected, changed)

    def test_export_postcondition_rejects_partial_object_coverage(self):
        scene = FakeBlenderData([
            FakeObject("SK_Tree_01"),
            FakeObject("SK_Tree_02"),
        ])
        expected = export_object_postcondition(scene)
        actual = FakeBlenderData([FakeObject("SK_Tree_01")])

        with self.assertRaisesRegex(
            RepairPushEvidenceError,
            "does not exactly match",
        ):
            validate_export_object_postcondition(expected, actual)

    def test_export_postcondition_rejects_invalid_face_material_assignment(self):
        scene = FakeBlenderData([FakeObject("SK_Tree")])
        scene.collections["Export"].all_objects[0].data.polygons[0].material_index = 1
        expected = export_object_postcondition(scene)

        with self.assertRaisesRegex(
            RepairPushEvidenceError,
            "material assignment is invalid",
        ):
            validate_export_object_postcondition(expected, scene)


if __name__ == "__main__":
    unittest.main()
