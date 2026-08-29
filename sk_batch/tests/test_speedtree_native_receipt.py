from __future__ import annotations

import base64
import json
import random
import sys
import tempfile
import unittest
from pathlib import Path


SK_BATCH = Path(__file__).resolve().parents[1]
if str(SK_BATCH) not in sys.path:
    sys.path.insert(0, str(SK_BATCH))

from speedtree_native_receipt import (  # noqa: E402
    NativeReceiptError,
    _native_runtime_owner_key,
    build_exact_native_receipt_index,
    exact_generated_instance,
    load_native_export_receipt,
    native_position_to_blender_world,
    native_tangent_to_blender_world,
)


def _linear_exact_generated_instance(receipt, geometry_ordinal, vertex_indices):
    """Frozen pre-index oracle used to prove exact behavioral parity."""
    vertices = sorted({int(value) for value in vertex_indices})
    if not vertices:
        raise NativeReceiptError("target component has no vertices")
    matches_by_owner = {}
    for row_index, row in enumerate(receipt.get("generated_instances") or []):
        if int(row["geometry_ordinal"]) != int(geometry_ordinal):
            continue
        matched = [
            vertex
            for vertex in vertices
            if any(
                first <= vertex <= last
                for first, last in row["vertex_ranges"]
            )
        ]
        if not matched:
            continue
        owner_key = _native_runtime_owner_key(
            row,
            row_index,
            geometry_ordinal,
        )
        owner = matches_by_owner.setdefault(owner_key, {
            "row": row,
            "matched": set(),
            "record_indices": [],
            "native_instance_ids": set(),
            "matched_rows": [],
        })
        owner["matched"].update(matched)
        owner["record_indices"].append(row_index)
        owner["matched_rows"].append(row)
        if row.get("native_instance_id") is not None:
            owner["native_instance_ids"].add(
                int(row["native_instance_id"])
            )
    matches = list(matches_by_owner.values())
    if len(matches) != 1:
        raise NativeReceiptError(
            "target component has no sole intersecting native runtime owner: "
            f"geometry={geometry_ordinal}, matches={len(matches)}"
        )
    owner = matches[0]
    matched_rows = owner["matched_rows"]
    stable_fields = (
        "geometry_ordinal",
        "parent_guid",
        "generator_guid",
        "source_rtti",
        "authored_position_native",
        "authored_tangent_native_unit",
    )
    for field in stable_fields:
        values = {
            json.dumps(row.get(field), sort_keys=True, ensure_ascii=False)
            for row in matched_rows
        }
        if len(values) > 1:
            raise NativeReceiptError(
                "one native runtime owner has inconsistent attachment "
                f"metadata: field={field}"
            )
    influence_values = {
        json.dumps(
            row.get("authored_position_influences") or [],
            sort_keys=True,
            ensure_ascii=False,
        )
        for row in matched_rows
    }
    if len(influence_values) > 1:
        raise NativeReceiptError(
            "one native runtime owner has ambiguous authored attachment "
            "influences for the queried vertices"
        )
    row = matched_rows[0]
    matched = sorted(owner["matched"])
    matched_set = set(matched)
    return {
        **row,
        "matched_native_vertex_indices": matched,
        "queried_native_vertex_count": len(vertices),
        "unowned_native_vertex_count": sum(
            vertex not in matched_set for vertex in vertices
        ),
        "native_instance_ids": sorted(owner["native_instance_ids"]),
        "native_serializer_record_indices": list(owner["record_indices"]),
        "native_serializer_record_count": len(owner["record_indices"]),
        "owner_selection_policy": (
            "sole_exact_native_runtime_owner_range_intersection_v3"
        ),
    }


def _exact_outcome(function):
    try:
        return (
            "ok",
            json.dumps(
                function(),
                sort_keys=True,
                ensure_ascii=False,
            ),
        )
    except NativeReceiptError as exc:
        return "error", str(exc)


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
            "sole_exact_native_runtime_owner_range_intersection_v3",
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
        self.assertEqual(instance["native_serializer_record_indices"], [0, 1])
        self.assertEqual(instance["native_serializer_record_count"], 2)

    @staticmethod
    def _overlapping_exact_receipt():
        def row(owner, ranges, native_instance_id):
            return {
                "geometry_ordinal": 0,
                "native_instance_id": native_instance_id,
                "node_guid": owner,
                "parent_guid": f"{owner}-parent",
                "generator_guid": f"{owner}-generator",
                "source_rtti": "leaf",
                "source_bone_id": native_instance_id,
                "authored_position_native": [1.0, 2.0, 3.0],
                "authored_tangent_native_unit": [0.0, 1.0, 0.0],
                "vertex_ranges": ranges,
                "authored_position_influences": [{
                    "bone_id": 7,
                    "mapping_node": "start",
                    "exported_cluster_name": "CapturedNode",
                    "native_root": False,
                    "weight": 1.0,
                }],
            }

        return {
            "geometries": [{"ordinal": 0, "vertex_count": 40}],
            "generated_instances": [
                row("owner-a", [(0, 5), (15, 18)], 100),
                row("owner-a", [(4, 9), (30, 31)], 101),
                row("owner-b", [(8, 14)], 200),
                row("owner-b", [(18, 22)], 201),
                row("owner-c", [(25, 27)], 300),
            ],
        }

    def test_overlapping_rows_use_csr_without_losing_record_order(self):
        receipt = self._overlapping_exact_receipt()
        index = build_exact_native_receipt_index(receipt)

        self.assertEqual(index.storage_modes, {0: "csr_multiple_rows"})
        instance = exact_generated_instance(
            receipt,
            0,
            [4, 5],
            receipt_index=index,
        )

        self.assertEqual(instance["matched_native_vertex_indices"], [4, 5])
        self.assertEqual(instance["native_serializer_record_indices"], [0, 1])
        self.assertEqual(instance["native_instance_ids"], [100, 101])
        with self.assertRaisesRegex(NativeReceiptError, "sole intersecting"):
            exact_generated_instance(
                receipt,
                0,
                [8],
                receipt_index=index,
            )

    def test_same_owner_stable_metadata_conflict_remains_fail_closed(self):
        receipt = self._overlapping_exact_receipt()
        receipt["generated_instances"][1][
            "authored_tangent_native_unit"
        ] = [1.0, 0.0, 0.0]
        index = build_exact_native_receipt_index(receipt)
        expected = _exact_outcome(
            lambda: _linear_exact_generated_instance(receipt, 0, [4])
        )

        self.assertEqual(
            _exact_outcome(
                lambda: exact_generated_instance(
                    receipt,
                    0,
                    [4],
                    receipt_index=index,
                )
            ),
            expected,
        )
        self.assertEqual(
            expected,
            (
                "error",
                "one native runtime owner has inconsistent attachment "
                "metadata: field=authored_tangent_native_unit",
            ),
        )

    def test_disjoint_ranges_use_one_packed_row_id_per_native_vertex(self):
        receipt = self._overlapping_exact_receipt()
        receipt["generated_instances"] = [
            receipt["generated_instances"][0],
            receipt["generated_instances"][4],
        ]
        receipt["generated_instances"][0]["vertex_ranges"] = [(0, 18)]
        receipt["generated_instances"][1]["vertex_ranges"] = [(25, 27)]
        index = build_exact_native_receipt_index(receipt)

        self.assertEqual(index.storage_modes, {0: "dense_single_row"})
        self.assertEqual(index.persistent_storage_bytes, 40 * 4)
        self.assertEqual(index.records_at(0, 0), (0,))
        self.assertEqual(index.records_at(0, 23), ())
        self.assertEqual(index.records_at(0, 26), (1,))

    def test_index_and_memory_bounded_interval_match_frozen_linear_oracle(self):
        receipt = self._overlapping_exact_receipt()
        csr_index = build_exact_native_receipt_index(receipt)
        interval_index = build_exact_native_receipt_index(
            receipt,
            storage_budget_bytes=0,
        )
        self.assertEqual(csr_index.storage_modes, {0: "csr_multiple_rows"})
        self.assertEqual(interval_index.storage_modes, {0: "interval"})
        randomizer = random.Random(739)
        queries = [
            [randomizer.randrange(-3, 44) for _value in range(
                randomizer.randrange(0, 8)
            )]
            for _query in range(500)
        ]
        queries.extend((
            [4, 5],
            [8],
            [18],
            [23, 24],
            [27, 25, 27],
            [],
        ))

        for vertices in queries:
            expected = _exact_outcome(
                lambda vertices=vertices: _linear_exact_generated_instance(
                    receipt,
                    0,
                    vertices,
                )
            )
            self.assertEqual(
                _exact_outcome(
                    lambda vertices=vertices: exact_generated_instance(
                        receipt,
                        0,
                        vertices,
                        receipt_index=csr_index,
                    )
                ),
                expected,
            )
            self.assertEqual(
                _exact_outcome(
                    lambda vertices=vertices: exact_generated_instance(
                        receipt,
                        0,
                        vertices,
                        receipt_index=interval_index,
                    )
                ),
                expected,
            )

    def test_zero_guid_and_reused_instance_id_do_not_merge_distinct_nodes(self):
        with tempfile.TemporaryDirectory() as temporary:
            spm, receipt_path = self._write(Path(temporary))
            receipt = load_native_export_receipt(
                receipt_path,
                source_spm=spm,
            )
        first = receipt["generated_instances"][0]
        first.update({
            "node_guid": "AAAAAAAAAAAAAAAAAAAAAA==",
            "native_instance_id": 311040,
            "parent_guid": "parent-a",
            "generator_guid": "generator-a",
            "source_rtti": "branch",
            "vertex_ranges": [(2, 2)],
        })
        receipt["generated_instances"].append({
            **first,
            "parent_guid": "parent-b",
            "generator_guid": "generator-b",
            "authored_position_native": (9.0, 8.0, 7.0),
            "vertex_ranges": [(7, 7)],
        })

        with self.assertRaisesRegex(NativeReceiptError, "sole intersecting"):
            exact_generated_instance(receipt, 0, [2, 7])

    def test_runtime_source_object_id_separates_identical_fallback_metadata(self):
        with tempfile.TemporaryDirectory() as temporary:
            spm, receipt_path = self._write(Path(temporary))
            receipt = load_native_export_receipt(
                receipt_path,
                source_spm=spm,
            )
        first = receipt["generated_instances"][0]
        first.update({
            "node_guid": "AAAAAAAAAAAAAAAAAAAAAA==",
            "native_instance_id": 311040,
            "native_source_object_id": 1001,
            "vertex_ranges": [(2, 2)],
        })
        receipt["generated_instances"].append({
            **first,
            "native_source_object_id": 1002,
            "vertex_ranges": [(7, 7)],
        })

        with self.assertRaisesRegex(NativeReceiptError, "sole intersecting"):
            exact_generated_instance(receipt, 0, [2, 7])

    def test_owner_never_collapses_ambiguous_attachment_influences(self):
        with tempfile.TemporaryDirectory() as temporary:
            spm, receipt_path = self._write(Path(temporary))
            receipt = load_native_export_receipt(
                receipt_path,
                source_spm=spm,
            )
        first = receipt["generated_instances"][0]
        first["vertex_ranges"] = [(2, 2)]
        receipt["generated_instances"].append({
            **first,
            "source_bone_id": 8,
            "vertex_ranges": [(2, 2)],
            "authored_position_influences": [{
                "bone_id": 8,
                "mapping_node": "start",
                "exported_cluster_name": "DifferentBone",
                "native_root": False,
                "weight": 1.0,
            }],
        })

        with self.assertRaisesRegex(
            NativeReceiptError,
            "ambiguous authored attachment influences",
        ):
            exact_generated_instance(receipt, 0, [2])

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
