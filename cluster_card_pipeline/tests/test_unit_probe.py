import json
import tempfile
import unittest
from pathlib import Path

from cluster_card_pipeline.unit_probe import (
    UnitProbeError,
    select_unit_probe_contract,
)


class UnitProbeTests(unittest.TestCase):
    def measurement(self, root, generator_type, measured):
        evidence = root / (generator_type.replace(" ", "_") + ".xml")
        evidence.write_text("<SpeedTree />", encoding="utf-8")
        return {
            "generator_type": generator_type,
            "measured_extent_meters": measured,
            "evidence": str(evidence),
        }

    def candidate(self, root, name, geometry, asset, measured):
        return {
            "name": name,
            "mesh_geometry_scale": geometry,
            "mesh_asset_scale": asset,
            "generator_scale": 1.0,
            "measurements": [
                self.measurement(root, "Frond", measured),
                self.measurement(root, "Leaf Mesh", measured),
            ],
        }

    def test_selects_measured_candidate_not_hardcoded_location(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            result = select_unit_probe_contract(
                [
                    self.candidate(root, "geometry", 0.01, 1.0, 0.13),
                    self.candidate(root, "asset", 1.0, 0.01, 0.1),
                ],
                target_meters=0.1,
            )
            self.assertEqual(
                result["selected"]["scale_location"],
                "SPM_MESH_ASSET",
            )
            self.assertEqual(len(result["generator_results"]), 2)
            json.dumps(result)

    def test_rejects_duplicate_scale_even_when_measurement_matches(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaisesRegex(UnitProbeError, "No common"):
                select_unit_probe_contract(
                    [self.candidate(root, "double", 0.01, 0.01, 0.1)]
                )

    def test_requires_frond_and_leaf_mesh(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            value = self.candidate(root, "one-role", 1.0, 0.01, 0.1)
            value["measurements"] = value["measurements"][:1]
            with self.assertRaisesRegex(UnitProbeError, "No common"):
                select_unit_probe_contract([value])

    def test_response_probe_selects_one_uniform_scale_location(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            evidence = root / "actual_speedtree_measurement.json"
            evidence.write_text("{}", encoding="utf-8")

            def candidate(name, geometry, asset, leaf_response):
                return {
                    "name": name,
                    "mesh_geometry_scale": geometry,
                    "mesh_asset_scale": asset,
                    "generator_scale": 1.0,
                    "physical_import_multiplier": 100.0,
                    "response_measurements": [
                        {
                            "generator_type": "Frond",
                            "measurement_mode": (
                                "preserved_generator_dimension_invariance"
                            ),
                            "measured_response": 1.0,
                            "expected_response": 1.0,
                            "evidence": str(evidence),
                        },
                        {
                            "generator_type": "Leaf Mesh",
                            "measurement_mode": "actual_speedtree_scale_response",
                            "measured_response": leaf_response,
                            "expected_response": geometry * asset,
                            "evidence": str(evidence),
                        },
                    ],
                }

            result = select_unit_probe_contract(
                [
                    candidate("identity", 1.0, 1.0, 1.0),
                    candidate("geometry", 0.01, 1.0, 0.012),
                    candidate("asset", 1.0, 0.01, 0.01),
                ],
                target_meters=0.1,
                tolerance_ratio=0.05,
            )
            self.assertEqual(
                result["selected"]["scale_location"],
                "SPM_MESH_ASSET",
            )
            self.assertEqual(
                result["generator_results"][0]["measurement_mode"],
                "preserved_generator_dimension_invariance",
            )

    def test_user_can_select_measured_common_art_direction_scale(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            evidence = root / "actual_speedtree_measurement.json"
            evidence.write_text("{}", encoding="utf-8")
            candidate = {
                "name": "spm_asset_01_user",
                "mesh_geometry_scale": 1.0,
                "mesh_asset_scale": 0.1,
                "generator_scale": 1.0,
                "physical_import_multiplier": 100.0,
                "response_measurements": [
                    {
                        "generator_type": "Frond",
                        "measurement_mode": (
                            "preserved_generator_dimension_invariance"
                        ),
                        "measured_response": 1.0,
                        "expected_response": 1.0,
                        "evidence": str(evidence),
                    },
                    {
                        "generator_type": "Leaf Mesh",
                        "measurement_mode": (
                            "actual_speedtree_scale_response"
                        ),
                        "measured_response": 0.1,
                        "expected_response": 0.1,
                        "evidence": str(evidence),
                    },
                ],
            }
            result = select_unit_probe_contract(
                [candidate],
                target_meters=0.1,
                selected_candidate_name="spm_asset_01_user",
                selection_reason="Preserve user-authored generator dimensions.",
            )
            self.assertEqual(
                result["selected"]["mesh_asset_scale"],
                0.1,
            )
            self.assertEqual(
                result["selection_authority"],
                "user_art_direction",
            )
            self.assertFalse(result["physical_scale_match"])
            self.assertEqual(result["required_effective_scale"], 0.01)


if __name__ == "__main__":
    unittest.main()
