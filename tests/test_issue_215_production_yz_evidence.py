import json
import math
import unittest
from pathlib import Path


FIXTURE = (
    Path(__file__).resolve().parent
    / "fixtures"
    / "issue_215_production_yz_evidence.json"
)
EXPECTED_ROLES = [
    "Color",
    "Opacity",
    "Normal",
    "Gloss",
    "SubsurfaceColor",
    "SubsurfaceAmount",
    "AO",
    "Height",
]


class Issue215ProductionYZEvidenceTests(unittest.TestCase):
    def test_real_delivery_proves_yz_extent_coverage_and_idempotence(self):
        evidence = json.loads(FIXTURE.read_text(encoding="utf-8"))
        self.assertEqual(evidence["kind"], "issue_215_production_yz_delivery_evidence")
        self.assertEqual(evidence["source_issue"], 215)

        capture = evidence["capture"]
        self.assertEqual(capture["plane"], "YZ")
        self.assertEqual(capture["right"], [0.0, 1.0, 0.0])
        self.assertEqual(capture["up"], [0.0, 0.0, 1.0])
        self.assertEqual(capture["normal"], [1.0, 0.0, 0.0])
        self.assertEqual(capture["view_direction"], [-1.0, 0.0, 0.0])
        self.assertEqual(capture["rotation_degrees"], 90.0)
        self.assertEqual(capture["resolution"], [1024, 1024])
        self.assertEqual(capture["map_roles"], EXPECTED_ROLES)
        self.assertTrue(
            all(math.isclose(value, 0.1, abs_tol=2.0e-9) for value in capture["target_meters"])
        )
        self.assertLessEqual(capture["content_width"], capture["target_meters"][0])
        self.assertLessEqual(capture["content_height"], capture["target_meters"][1])
        expected_coverage = 1.0 / (1.0 + 2.0 * capture["padding_ratio"])
        self.assertTrue(
            math.isclose(capture["coverage_ratio"], expected_coverage, abs_tol=2.0e-9)
        )

        delivery = evidence["delivery"]
        first = evidence["forced_refresh_run"]
        second = evidence["second_identical_run"]
        self.assertEqual(len(delivery["targets"]), 2)
        self.assertTrue(all(len(row["sha256"]) == 64 for row in delivery["targets"]))
        self.assertEqual(first["status"], "ready")
        self.assertTrue(first["force_refresh"])
        self.assertFalse(first["transaction_no_change"])
        self.assertFalse(first["delivery_receipt_written"])
        self.assertEqual(second["status"], "ready")
        self.assertTrue(second["transaction_no_change"])
        self.assertFalse(second["delivery_receipt_written"])
        self.assertEqual(second["refresh_reasons"], [])
        self.assertEqual(
            first["delivery_sha256"],
            second["delivery_sha256"],
        )
        self.assertEqual(first["delivery_sha256"], delivery["delivery_sha256"])
        self.assertIn(
            delivery["delivery_sha256"][:16],
            delivery["receipt_name"],
        )


if __name__ == "__main__":
    unittest.main()
