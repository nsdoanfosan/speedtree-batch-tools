"""Capture limits are measured in the Normalizer's rigid authored frame."""
from copy import deepcopy
import math
from pathlib import Path
import sys
import unittest

SK_BATCH = Path(__file__).resolve().parents[1]
if str(SK_BATCH) not in sys.path:
    sys.path.insert(0, str(SK_BATCH))
import cluster_assembly_builder as builder
from sk_batch.tests import test_cluster_assembly_builder as fixtures


def capture_contract(direction=(0.0, 1.30295991897583, -2.846209764480591)):
    return {"frame": {"right": [0, 1, 0], "up": [0, 0, 1], "normal": [1, 0, 0]},
        "attachment_pivots": [{"prototype_asset": "SK_CustomFlow_01", "prototype_index": 1,
            "attachment_tangent_projection": {"source_world": list(direction),
                "capture_plane_world": list(direction), "aligned_capture_plane_world": list(direction),
                "normalized_local_xy": [0, 1],
                "direction_policy": "project_xml_attachment_into_capture_plane"}}]}


def limit(capture):
    return builder._normalized_capture_bounds_frame(capture,
        {"skeletal_asset": "SK_CustomFlow_01", "prototype_index": 1}, [0.1, 0.1])


class CaptureBoundsFrameTests(unittest.TestCase):
    def test_exact_side_tangent_rotates_capture_rectangle_without_rescaling(self):
        report = limit(capture_contract())
        self.assertEqual(report["policy"], "capture_rectangle_in_authored_positive_y_frame")
        self.assertAlmostEqual(report["limits"][0], 0.1325497333582828)
        self.assertAlmostEqual(report["limits"][1], 0.1325497333582828)
        self.assertGreater(report["limits"][1], 0.10014469921588898)
        tangent = report["tangent_capture_right_up"]
        self.assertAlmostEqual(math.hypot(*tangent), 1)

    def test_rectangle_width_and_height_follow_the_rotated_basis(self):
        capture = capture_contract((0, 1, 1))
        result = builder._normalized_capture_bounds_frame(capture,
            {"skeletal_asset": "SK_CustomFlow_01", "prototype_index": 1}, [0.1, 0.2])
        self.assertAlmostEqual(result["limits"][0], 0.3 / math.sqrt(2))
        self.assertAlmostEqual(result["limits"][1], 0.3 / math.sqrt(2))
        capture = capture_contract((0, 1, 0))
        result = builder._normalized_capture_bounds_frame(capture,
            {"skeletal_asset": "SK_CustomFlow_01", "prototype_index": 1}, [0.1, 0.2])
        self.assertEqual(result["limits"], [0.2, 0.1])

    def test_legacy_missing_tangent_retains_strict_capture_axes(self):
        capture = capture_contract()
        capture["attachment_pivots"][0].pop("attachment_tangent_projection")
        capture["frame"] = {}
        self.assertEqual(limit(capture), {"prototype_asset": "SK_CustomFlow_01", "prototype_index": 1,
            "policy": "legacy_capture_axes", "limits": [0.1, 0.1]})

    def test_capture_normal_fallback_requires_authored_capture_up_evidence(self):
        capture = capture_contract()
        tangent = capture["attachment_pivots"][0]["attachment_tangent_projection"]
        tangent.update(source_world=[1, 0, 0], capture_plane_world=[0, 0, 0], aligned_capture_plane_world=[0, 0, 1],
            projection_tolerance=0.001, direction_policy="capture_up_for_capture_normal_attachment")
        self.assertEqual(limit(capture)["limits"], [0.1, 0.1])
        for values in ({"capture_plane_world": [0, 0.1, 0]}, {"aligned_capture_plane_world": [0, 1, 0]},
                       {"projection_tolerance": -1}):
            bad = deepcopy(capture)
            bad["attachment_pivots"][0]["attachment_tangent_projection"].update(values)
            with self.assertRaises(builder.ClusterAssemblyBuildError):
                limit(bad)

    def test_invalid_present_metadata_cannot_widen_capture_limits(self):
        mutations = (lambda value: value["frame"].pop("right"),
            lambda value: value["frame"].update(up=[0, 1, 0]),
            lambda value: value["frame"].update(normal=[-1, 0, 0]),
            lambda value: value["attachment_pivots"][0].update(prototype_index=2),
            lambda value: value["attachment_pivots"][0].update(attachment_tangent_projection={}),
            lambda value: value["attachment_pivots"][0]["attachment_tangent_projection"].update(aligned_capture_plane_world=[math.nan, 1, 0]),
            lambda value: value["attachment_pivots"][0]["attachment_tangent_projection"].update(aligned_capture_plane_world=[1, 1, 1]),
            lambda value: value["attachment_pivots"][0]["attachment_tangent_projection"].update(aligned_capture_plane_world=[0, 0, 0]),
            lambda value: value["attachment_pivots"][0]["attachment_tangent_projection"].update(source_world=[1e308, 0, 0]),
            lambda value: value["attachment_pivots"][0]["attachment_tangent_projection"].update(direction_policy="unknown"),
            lambda value: value["attachment_pivots"][0]["attachment_tangent_projection"].update(normalized_local_xy=[1, 0]))
        for mutate in mutations:
            bad = capture_contract()
            mutate(bad)
            with self.assertRaises(builder.ClusterAssemblyBuildError):
                limit(bad)

    def test_full_contract_accepts_rotated_bounds_and_rejects_axis_or_oversize(self):
        manifest = fixtures.physical_production_manifest()
        receipt = next(row["receipt"] for row in manifest["normalized_variant_receipts"] if row["role"] == "leaf_side")
        capture = receipt["physical_capture_contract"]
        capture["frame"].update(capture_contract()["frame"])
        tangent = capture_contract()["attachment_pivots"][0]["attachment_tangent_projection"]
        for pivot in capture["attachment_pivots"]:
            pivot["attachment_tangent_projection"] = deepcopy(tangent)
        receipt["prototypes"][0]["normalized_bounds"]["size"] = [0.0537823885679245, 0.10014469921588898, 0.033933643251657486]
        before = deepcopy(receipt["prototypes"])
        self.assertEqual(builder.validate_normalized_prototype_unit_contract(manifest)["status"], "verified")
        self.assertEqual(receipt["prototypes"], before)
        # Bounds validation uses capture geometry, independent of UV wind policy.
        receipt["part_bend_role"] = "leaf_side"
        receipt["prototypes"][0]["part_bend_payload"] = {"aspect_ratio": 123}
        self.assertEqual(builder.validate_normalized_prototype_unit_contract(manifest)["status"], "verified")
        oversized = deepcopy(manifest)
        side = next(row["receipt"] for row in oversized["normalized_variant_receipts"] if row["role"] == "leaf_side")
        side["prototypes"][0]["normalized_bounds"]["size"][1] = 0.133
        with self.assertRaisesRegex(builder.ClusterAssemblyBuildError, "physical bounds exceed"):
            builder.validate_normalized_prototype_unit_contract(oversized)
        for pivot in capture["attachment_pivots"]:
            pivot["attachment_tangent_projection"] = capture_contract((0, 0, 1))["attachment_pivots"][0]["attachment_tangent_projection"]
        with self.assertRaisesRegex(builder.ClusterAssemblyBuildError, "physical bounds exceed"):
            builder.validate_normalized_prototype_unit_contract(manifest)


if __name__ == "__main__":
    unittest.main()
