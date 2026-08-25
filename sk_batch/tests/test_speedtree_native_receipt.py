from __future__ import annotations

import base64
import json
import sys
import tempfile
import unittest
from pathlib import Path


SK_BATCH = Path(__file__).resolve().parents[1]
if str(SK_BATCH) not in sys.path:
    sys.path.insert(0, str(SK_BATCH))

from speedtree_native_receipt import (  # noqa: E402
    NativeReceiptError,
    exact_generated_instance,
    load_native_export_receipt,
    native_position_to_blender_world,
    native_tangent_to_blender_world,
)


class NativeSpeedTreeReceiptTests(unittest.TestCase):
    def _write(self, root):
        spm = root / "tree.spm"
        spm.write_bytes(b"runtime source")
        stat = spm.stat()
        guid = base64.b64encode(bytes(range(16))).decode("ascii")
        payload = {
            "schema_version": 5,
            "kind": "speedtree_native_export_receipt",
            "status": "ready",
            "identity_policy": (
                "modeler_runtime_pose_tangent_and_fbx_serializer_records_v5"
            ),
            "coordinate_contract": {
                "native_unit_to_meter": 0.3048,
                "native_unit_to_solver": 30.48,
                "blender_xyz_from_native_xyz": [
                    "x*0.3048",
                    "y*0.3048",
                    "z*0.3048",
                ],
            },
            "source": {
                "path": str(spm.resolve()),
                "size": stat.st_size,
                "last_write_time_100ns": (
                    stat.st_mtime_ns // 100 + 116444736000000000
                ),
            },
            "geometry_count": 1,
            "geometries": [{"ordinal": 0, "vertex_count": 8}],
            "bones": [{
                "id": 7,
                "parent_id": 0,
                "start_native": [1.0, 2.0, 3.0],
                "end_native": [4.0, 5.0, 6.0],
            }],
            "generated_instances": [{
                "geometry_ordinal": 0,
                "source_bone_id": 7,
                "node_guid": guid,
                "authored_position_native": [1.0, 2.0, 3.0],
                "authored_tangent_native_unit": [0.0, 1.0, 0.0],
                "vertex_ranges": [[2, 5]],
                "authored_position_influences": [{
                    "bone_id": 7,
                    "mapping_node": "start",
                    "exported_cluster_name": "CapturedNode",
                    "native_root": False,
                    "weight": 1.0,
                }],
            }],
        }
        receipt = root / "tree.speedtree_native_receipt.json"
        receipt.write_text(json.dumps(payload), encoding="utf-8")
        return spm, receipt

    def test_loads_current_native_runtime_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            spm, receipt_path = self._write(Path(temporary))
            receipt = load_native_export_receipt(
                receipt_path,
                source_spm=spm,
            )

        instance = exact_generated_instance(receipt, 0, [2, 3, 5])
        self.assertEqual(instance["source_bone_id"], 7)
        self.assertEqual(
            instance["authored_position_influences"][0][
                "exported_cluster_name"
            ],
            "CapturedNode",
        )
        self.assertEqual(
            instance["authored_tangent_native_unit"],
            (0.0, 1.0, 0.0),
        )

    def test_native_position_preserves_xyz_and_only_converts_units(self):
        with tempfile.TemporaryDirectory() as temporary:
            spm, receipt_path = self._write(Path(temporary))
            receipt = load_native_export_receipt(
                receipt_path,
                source_spm=spm,
            )

        actual = native_position_to_blender_world(
            receipt,
            (12.700318336486816, 10.592374801635742, 31.500131607055664),
        )
        expected = (
            3.8710570289611816,
            3.228555839538574,
            9.601240113830566,
        )
        for observed, wanted in zip(actual, expected):
            self.assertAlmostEqual(observed, wanted, places=12)

    def test_native_tangent_preserves_matching_xyz_without_unit_scale(self):
        with tempfile.TemporaryDirectory() as temporary:
            spm, receipt_path = self._write(Path(temporary))
            receipt = load_native_export_receipt(
                receipt_path,
                source_spm=spm,
            )

        self.assertEqual(
            native_tangent_to_blender_world(receipt, (0.0, 0.6, 0.8)),
            (0.0, 0.6, 0.8),
        )

    def test_current_receipt_rejects_missing_runtime_tangent(self):
        with tempfile.TemporaryDirectory() as temporary:
            spm, receipt_path = self._write(Path(temporary))
            payload = json.loads(receipt_path.read_text(encoding="utf-8"))
            payload["generated_instances"][0].pop(
                "authored_tangent_native_unit"
            )
            receipt_path.write_text(json.dumps(payload), encoding="utf-8")

            with self.assertRaisesRegex(NativeReceiptError, "tangent"):
                load_native_export_receipt(receipt_path, source_spm=spm)

    def test_previous_v3_receipt_remains_readable_without_runtime_tangent(self):
        with tempfile.TemporaryDirectory() as temporary:
            spm, receipt_path = self._write(Path(temporary))
            payload = json.loads(receipt_path.read_text(encoding="utf-8"))
            payload["schema_version"] = 3
            payload["identity_policy"] = (
                "modeler_parsed_runtime_and_fbx_serializer_records_v3"
            )
            payload["generated_instances"][0].pop(
                "authored_tangent_native_unit"
            )
            receipt_path.write_text(json.dumps(payload), encoding="utf-8")

            receipt = load_native_export_receipt(
                receipt_path,
                source_spm=spm,
            )

        self.assertNotIn(
            "authored_tangent_native_unit",
            receipt["generated_instances"][0],
        )

    def test_legacy_receipt_uses_raw_native_xyz_not_declared_axis_swap(self):
        with tempfile.TemporaryDirectory() as temporary:
            spm, receipt_path = self._write(Path(temporary))
            payload = json.loads(receipt_path.read_text(encoding="utf-8"))
            payload["schema_version"] = 2
            payload.pop("identity_policy", None)
            payload["generated_instances"][0].pop(
                "authored_tangent_native_unit"
            )
            payload["coordinate_contract"][
                "blender_xyz_from_native_xyz"
            ] = ["x*0.3048", "z*0.3048", "-y*0.3048"]
            receipt_path.write_text(json.dumps(payload), encoding="utf-8")
            receipt = load_native_export_receipt(
                receipt_path,
                source_spm=spm,
            )

        self.assertEqual(
            native_position_to_blender_world(receipt, (1.0, 2.0, 3.0)),
            (0.3048, 0.6096, 0.9144000000000001),
        )
        self.assertTrue(
            receipt["coordinate_contract_interpretation"]
            ["legacy_declared_axis_map_corrected"]
        )

    def test_rejects_unrecognized_coordinate_contract(self):
        with tempfile.TemporaryDirectory() as temporary:
            spm, receipt_path = self._write(Path(temporary))
            payload = json.loads(receipt_path.read_text(encoding="utf-8"))
            payload["coordinate_contract"][
                "blender_xyz_from_native_xyz"
            ] = ["x", "y", "z"]
            receipt_path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(
                NativeReceiptError,
                "coordinate contract is unsupported",
            ):
                load_native_export_receipt(receipt_path, source_spm=spm)

    def test_loads_explicit_boneless_export_with_zero_geometries(self):
        with tempfile.TemporaryDirectory() as temporary:
            spm, receipt_path = self._write(Path(temporary))
            payload = json.loads(receipt_path.read_text(encoding="utf-8"))
            payload["id_zero_cluster_write"] = (
                "not_applicable_boneless_export"
            )
            payload["geometry_count"] = 0
            payload["geometries"] = []
            payload["bones"] = []
            payload["generated_instances"] = []
            receipt_path.write_text(json.dumps(payload), encoding="utf-8")

            receipt = load_native_export_receipt(
                receipt_path,
                source_spm=spm,
            )

        self.assertEqual(receipt["geometry_count"], 0)
        self.assertEqual(
            receipt["id_zero_cluster_write"],
            "not_applicable_boneless_export",
        )

    def test_clipped_subset_requires_one_exact_intersecting_owner(self):
        with tempfile.TemporaryDirectory() as temporary:
            spm, receipt_path = self._write(Path(temporary))
            receipt = load_native_export_receipt(
                receipt_path,
                source_spm=spm,
            )

        instance = exact_generated_instance(receipt, 0, [0, 2, 7])
        self.assertEqual(instance["matched_native_vertex_indices"], [2])
        self.assertEqual(instance["unowned_native_vertex_count"], 2)
        self.assertEqual(
            instance["owner_selection_policy"],
            "sole_exact_native_node_guid_range_intersection_v2",
        )

    def test_same_runtime_node_guid_coalesces_split_serializer_records(self):
        with tempfile.TemporaryDirectory() as temporary:
            spm, receipt_path = self._write(Path(temporary))
            receipt = load_native_export_receipt(
                receipt_path,
                source_spm=spm,
            )
        first = receipt["generated_instances"][0]
        first["vertex_ranges"] = [(2, 3)]
        first["native_instance_id"] = 2305
        receipt["generated_instances"].append({
            **first,
            "native_instance_id": 3585,
            "record_type": -371195900,
            "vertex_ranges": [(4, 5)],
        })

        instance = exact_generated_instance(receipt, 0, [2, 3, 4, 5])

        self.assertEqual(instance["matched_native_vertex_indices"], [2, 3, 4, 5])
        self.assertEqual(instance["native_instance_ids"], [2305, 3585])
        self.assertEqual(instance["native_serializer_record_count"], 2)

    def test_clipped_subset_never_ranks_multiple_intersecting_owners(self):
        with tempfile.TemporaryDirectory() as temporary:
            spm, receipt_path = self._write(Path(temporary))
            receipt = load_native_export_receipt(
                receipt_path,
                source_spm=spm,
            )
        receipt["generated_instances"].append({
            **receipt["generated_instances"][0],
            "node_guid": "second-node",
            "vertex_ranges": [(7, 7)],
        })

        with self.assertRaisesRegex(
            NativeReceiptError,
            "sole intersecting",
        ):
            exact_generated_instance(receipt, 0, [2, 7])

    def test_rejects_stale_source_without_reparsing_spm(self):
        with tempfile.TemporaryDirectory() as temporary:
            spm, receipt_path = self._write(Path(temporary))
            spm.write_bytes(b"changed runtime source")
            with self.assertRaisesRegex(NativeReceiptError, "stale"):
                load_native_export_receipt(receipt_path, source_spm=spm)


if __name__ == "__main__":
    unittest.main()
