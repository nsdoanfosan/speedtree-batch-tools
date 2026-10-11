import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cluster_assembly_builder import ClusterAssemblyBuildError, _exact_normalized_authored_line


class ExactCaptureLineTests(unittest.TestCase):
    def contract(self):
        return {"production_normalization": {"physical_capture_contract": {
            "frame": {"fit_scale": 0.25},
            "attachment_pivots": [{"prototype_asset": "SK_cluster_01", "prototype_index": 1,
                "attachment_tangent_projection": {
                    "aligned_capture_plane_world": [0.0, 2.0, 0.0],
                    "normalized_local_xy": [0.0, 1.0],
                    "direction_policy": "capture_up_for_capture_normal_attachment",
                    "capture_plane_world": [0.0, 0.0, 0.0],
                    "projection_tolerance": 0.0001,
                }}],
        }}}

    def test_consumes_normalizer_authored_capture_normal_line(self):
        result = _exact_normalized_authored_line(self.contract(), {"source_prototype_index": 1}, "SK_cluster_01")
        self.assertEqual(result["length"], 0.5)
        self.assertEqual(result["direction_policy"], "capture_up_for_capture_normal_attachment")

    def test_rejects_unproven_or_nonpositive_y_fallback(self):
        for change in [{"capture_plane_world": [0, 0.01, 0]}, {"projection_tolerance": -1},
                       {"capture_plane_world": None}, {"normalized_local_xy": [1, 0]},
                       {"direction_policy": "guessed"}, {"aligned_capture_plane_world": [0, 0, 0]}]:
            contract = self.contract()
            contract["production_normalization"]["physical_capture_contract"]["attachment_pivots"][0]["attachment_tangent_projection"].update(change)
            with self.subTest(change=change), self.assertRaises(ClusterAssemblyBuildError):
                _exact_normalized_authored_line(contract, {"source_prototype_index": 1}, "SK_cluster_01")
