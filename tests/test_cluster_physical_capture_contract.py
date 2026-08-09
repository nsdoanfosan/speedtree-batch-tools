import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from cluster_physical_capture_contract import (
    CAPTURE_CONTRACT_KIND,
    CAPTURE_KIND,
    CAPTURE_WORKFLOW,
    DIRECT_UV_SOURCE,
    FRAME_POLICY,
    PLANE_BASES,
    PhysicalCaptureValidationError,
    REQUIRED_MAP_ROLES,
    canonical_sha256,
    validate_normalization_receipt,
    validate_physical_capture_manifest,
)
from tools.deliver_cluster_physical_capture import (
    build_delivery_receipt,
    persist_delivery_receipt,
)


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class PhysicalCaptureContractTests(unittest.TestCase):
    def fixture(self, temporary, *, plane="YZ"):
        root = Path(temporary)
        blend = root / "SK_branch_test_side_01.blend"
        blend.write_bytes(b"blend-v1")
        manifest = root / "branch_test_side_01_auto_capture_manifest.json"
        basis = PLANE_BASES[plane]
        raw_min = [0.0, 0.0, 0.0]
        raw_max = [2.0, 4.0, 8.0]
        right_axis, up_axis, normal_axis = basis["axis_indices"]
        raw_width = raw_max[right_axis] - raw_min[right_axis]
        raw_height = raw_max[up_axis] - raw_min[up_axis]
        raw_depth = raw_max[normal_axis] - raw_min[normal_axis]
        padding = 0.04
        width = 0.1
        fit_scale = width / (max(raw_width, raw_height) * (1.0 + 2.0 * padding))
        center = [
            (raw_min[index] + raw_max[index]) * 0.5 for index in range(3)
        ]
        fitted_extents = [
            (raw_max[index] - raw_min[index]) * fit_scale for index in range(3)
        ]
        fitted_min = [
            center[index] - fitted_extents[index] * 0.5 for index in range(3)
        ]
        fitted_max = [
            center[index] + fitted_extents[index] * 0.5 for index in range(3)
        ]
        camera_distance = raw_depth * fit_scale * 0.5 + width
        camera = [
            center[index] + basis["normal"][index] * camera_distance
            for index in range(3)
        ]
        frame = {
            "policy": FRAME_POLICY,
            "workflow_mode": CAPTURE_WORKFLOW,
            "plane": plane,
            "right": list(basis["right"]),
            "up": list(basis["up"]),
            "normal": list(basis["normal"]),
            "view_direction": list(basis["view_direction"]),
            "center": center,
            "camera_location": camera,
            "width": width,
            "height": width,
            "content_width": raw_width * fit_scale,
            "content_height": raw_height * fit_scale,
            "raw_content_width": raw_width,
            "raw_content_height": raw_height,
            "padding_ratio": padding,
            "fit_scale": fit_scale,
            "unit_system": "METRIC",
            "scale_length": 1.0,
            "meters_per_blender_unit": 1.0,
            "target_meters": [0.1, 0.1],
            "target_blender_units": [0.1, 0.1],
            "raw_world_bounds_min": raw_min,
            "raw_world_bounds_max": raw_max,
            "fitted_world_bounds_min": fitted_min,
            "fitted_world_bounds_max": fitted_max,
            "raw_depth_min": raw_min[normal_axis],
            "raw_depth_max": raw_max[normal_axis],
            "fitted_depth": raw_depth * fit_scale,
            "fit_matrix_world": [
                [fit_scale, 0.0, 0.0, 0.0],
                [0.0, fit_scale, 0.0, 0.0],
                [0.0, 0.0, fit_scale, 0.0],
                [0.0, 0.0, 0.0, 1.0],
            ],
            "orthogonality_error": 0.0,
            "handedness": 1.0,
            "rotation_degrees": basis["rotation_degrees"],
            "direct_uv_source": DIRECT_UV_SOURCE,
        }
        maps = []
        for index, role in enumerate(REQUIRED_MAP_ROLES):
            path = root / f"map_{index}_{role}.tga"
            path.write_bytes(f"{role}-production-map".encode("ascii"))
            maps.append({
                "role": role,
                "path": str(path.absolute()),
                "size": path.stat().st_size,
                "sha256": sha256(path),
            })
        contract = {
            "kind": CAPTURE_CONTRACT_KIND,
            "version": 1,
            "workflow_mode": CAPTURE_WORKFLOW,
            "direct_uv_source": DIRECT_UV_SOURCE,
            "source_blend": str(blend.absolute()),
            "source_collection": "SpeedTree_Source",
            "source_objects": [{
                "name": "Source",
                "vertices": 8,
                "polygons": 6,
                "evaluated_sha256": "a" * 64,
            }],
            "attachment_pivots": [{
                "prototype_index": 1,
                "prototype_asset": "SK_branch_test_side_01_01",
                "xml_bone_id": 0,
                "source_world": [0.0, 0.0, 0.0],
                "fitted_capture_world": [0.0, 0.0, 0.0],
                "normalized_local": [0.0, 0.0, 0.0],
            }],
            "frame": frame,
            "capture_manifest": str(manifest.absolute()),
            "capture_resolution": [1024, 1024],
            "capture_maps": maps,
        }
        contract["contract_sha256"] = canonical_sha256(contract)
        for row in maps:
            row["physical_capture_contract_sha256"] = contract[
                "contract_sha256"
            ]
        # The final add-on contract hash includes map rows after their per-map
        # contract binding has been attached.
        contract.pop("contract_sha256")
        contract["contract_sha256"] = canonical_sha256(contract)
        for row in maps:
            row["physical_capture_contract_sha256"] = contract[
                "contract_sha256"
            ]
        # Recompute once more because capture_maps are part of the contract.
        contract.pop("contract_sha256")
        contract["contract_sha256"] = canonical_sha256(contract)
        for row in maps:
            row["physical_capture_contract_sha256"] = contract[
                "contract_sha256"
            ]
        # The real add-on hashes the enriched contract before it stamps the
        # top-level manifest rows.  Contract capture-map rows themselves do not
        # carry that top-level stamp, so keep them in canonical final form.
        for row in contract["capture_maps"]:
            row.pop("physical_capture_contract_sha256", None)
        contract.pop("contract_sha256")
        contract["contract_sha256"] = canonical_sha256(contract)
        manifest_maps = []
        for row in maps:
            manifest_row = dict(row)
            manifest_row["physical_capture_contract_sha256"] = contract[
                "contract_sha256"
            ]
            manifest_maps.append(manifest_row)
        payload = {
            "kind": CAPTURE_KIND,
            "version": 2,
            "workflow_mode": CAPTURE_WORKFLOW,
            "blend": str(blend.absolute()),
            "source_objects": [{
                "name": "Source",
                "vertices": 8,
                "polygons": 6,
            }],
            "frame": frame,
            "physical_capture_contract": contract,
            "direct_uv_source": DIRECT_UV_SOURCE,
            "resolution": [1024, 1024],
            "maps": manifest_maps,
            "physical_capture_contract_sha256": contract["contract_sha256"],
            "normalization_status": "finalized",
        }
        manifest.write_text(json.dumps(payload), encoding="utf-8")
        receipt = root / "normalization_receipt.json"
        receipt.write_text(json.dumps({
            "kind": "speedtree_cluster_sync_normalization",
            "version": 3,
            "status": "ready",
            "blend": str(blend.absolute()),
            "capture_manifest": str(manifest.absolute()),
            "capture_manifest_sha256": sha256(manifest),
            "normalization_contract_sha256": "recipe-hash",
            "output_blend_sha256": sha256(blend),
            "build": {
                "workflow_mode": CAPTURE_WORKFLOW,
                "physical_capture_contract": contract,
            },
        }), encoding="utf-8")
        target = root / "SK_tree_01.spm"
        target.write_bytes(b"target-v1")
        return blend, target, manifest, receipt

    def test_valid_yz_delivery_proves_orientation_extent_coverage_and_receipt(self):
        with tempfile.TemporaryDirectory() as temporary:
            blend, _target, manifest, receipt = self.fixture(temporary)
            evidence = validate_physical_capture_manifest(
                manifest,
                expected_blend=blend,
                expected_plane="YZ",
                expected_resolution=1024,
                expected_padding_ratio=0.04,
            )
            normalization = validate_normalization_receipt(
                receipt,
                manifest_evidence=evidence,
                expected_blend=blend,
                expected_normalization_contract_sha256="recipe-hash",
            )
            self.assertEqual(evidence["orientation"]["plane"], "YZ")
            self.assertEqual(
                evidence["orientation"]["view_direction"], [-1.0, 0.0, 0.0]
            )
            self.assertEqual(evidence["map_roles"], list(REQUIRED_MAP_ROLES))
            self.assertLessEqual(evidence["extent"]["content_width"], 0.1)
            self.assertEqual(normalization["status"], "ready")

    def test_side_delivery_rejects_top_facing_xy_orientation(self):
        with tempfile.TemporaryDirectory() as temporary:
            blend, _target, manifest, _receipt = self.fixture(
                temporary, plane="XY"
            )
            with self.assertRaisesRegex(
                PhysicalCaptureValidationError, "does not match required YZ"
            ) as caught:
                validate_physical_capture_manifest(
                    manifest,
                    expected_blend=blend,
                    expected_plane="YZ",
                )
            self.assertEqual(caught.exception.code, "orientation_mismatch")

    def test_extent_and_map_coverage_fail_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            blend, _target, manifest, _receipt = self.fixture(temporary)
            payload = json.loads(manifest.read_text(encoding="utf-8"))
            payload["physical_capture_contract"]["frame"][
                "content_width"
            ] = 0.2
            payload["frame"] = payload["physical_capture_contract"]["frame"]
            contract = payload["physical_capture_contract"]
            contract.pop("contract_sha256")
            contract["contract_sha256"] = canonical_sha256(contract)
            payload["physical_capture_contract_sha256"] = contract[
                "contract_sha256"
            ]
            for row in payload["maps"]:
                row["physical_capture_contract_sha256"] = contract[
                    "contract_sha256"
                ]
            manifest.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaises(PhysicalCaptureValidationError) as caught:
                validate_physical_capture_manifest(
                    manifest, expected_blend=blend, expected_plane="YZ"
                )
            self.assertEqual(caught.exception.code, "extent_invalid")

            blend, _target, manifest, _receipt = self.fixture(temporary)
            payload = json.loads(manifest.read_text(encoding="utf-8"))
            payload["maps"].pop()
            manifest.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaises(PhysicalCaptureValidationError) as caught:
                validate_physical_capture_manifest(
                    manifest, expected_blend=blend, expected_plane="YZ"
                )
            self.assertEqual(caught.exception.code, "coverage_invalid")

    def test_changed_map_bytes_invalidate_immutable_fingerprint(self):
        with tempfile.TemporaryDirectory() as temporary:
            blend, _target, manifest, _receipt = self.fixture(temporary)
            payload = json.loads(manifest.read_text(encoding="utf-8"))
            Path(payload["maps"][0]["path"]).write_bytes(b"changed")
            with self.assertRaises(PhysicalCaptureValidationError) as caught:
                validate_physical_capture_manifest(
                    manifest, expected_blend=blend, expected_plane="YZ"
                )
            self.assertEqual(caught.exception.code, "fingerprint_mismatch")

    def test_malformed_version_fails_with_structured_schema_reason(self):
        with tempfile.TemporaryDirectory() as temporary:
            blend, _target, manifest, _receipt = self.fixture(temporary)
            payload = json.loads(manifest.read_text(encoding="utf-8"))
            payload["version"] = "not-an-integer"
            manifest.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaises(PhysicalCaptureValidationError) as caught:
                validate_physical_capture_manifest(
                    manifest, expected_blend=blend, expected_plane="YZ"
                )
            self.assertEqual(caught.exception.code, "schema_invalid")

    def test_normalization_receipt_rejects_changed_output_blend(self):
        with tempfile.TemporaryDirectory() as temporary:
            blend, _target, manifest, receipt = self.fixture(temporary)
            capture = validate_physical_capture_manifest(
                manifest, expected_blend=blend, expected_plane="YZ"
            )
            blend.write_bytes(b"blend-changed-after-receipt")
            with self.assertRaises(PhysicalCaptureValidationError) as caught:
                validate_normalization_receipt(
                    receipt,
                    manifest_evidence=capture,
                    expected_blend=blend,
                )
            self.assertEqual(caught.exception.code, "fingerprint_mismatch")

    def test_delivery_receipt_is_content_addressed_and_rerunnable(self):
        with tempfile.TemporaryDirectory() as temporary:
            blend, target, manifest, receipt = self.fixture(temporary)
            capture = validate_physical_capture_manifest(
                manifest, expected_blend=blend, expected_plane="YZ"
            )
            normalization = validate_normalization_receipt(
                receipt,
                manifest_evidence=capture,
                expected_blend=blend,
            )
            payload = build_delivery_receipt(
                blend, [target], capture, normalization
            )
            first, first_written = persist_delivery_receipt(
                payload, Path(temporary) / "reports", blend
            )
            second, second_written = persist_delivery_receipt(
                payload, Path(temporary) / "reports", blend
            )
            self.assertEqual(first, second)
            self.assertTrue(first_written)
            self.assertFalse(second_written)
            self.assertIn(payload["delivery_sha256"][:16], first.name)


if __name__ == "__main__":
    unittest.main()
