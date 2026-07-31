"""Literal compatibility-matrix and sanitized real-evidence tests for #41."""

import json
import re
import sys
import unittest
from pathlib import Path


REPO_DIR = Path(__file__).resolve().parents[2]
for candidate in (REPO_DIR, REPO_DIR / "pcg_st9_texture_batch"):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from stale_node_table_recovery import (  # noqa: E402
    StaleNodeTableRecoveryError,
    _resolve_receipt_dialect,
)


FIXTURE = Path(__file__).parent / "fixtures" / (
    "issue_41_receipt_compatibility.json"
)
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
WINDOWS_PATH_RE = re.compile(
    r"(?i)(?:(?<![a-z])[a-z]:[\\/]|users[\\/][^\\/]+)"
)
RAW_GUID_RE = re.compile(
    r"(?i)[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
)


def load_fixture():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def receipt_for(case):
    versions = case["projections"]
    receipt = {
        "schema_version": case["receipt_schema_version"],
        "authoring_graph_projection": {
            "contract": "speedtree_spm_authoring_graph_projection",
            "version": versions["authoring_graph"],
        },
        "generator_membership": {
            "contract": "speedtree_generator_membership_projection",
            "version": versions["generator_membership"],
        },
        "required_target_bindings": {
            "contract": "speedtree_required_target_binding_projection",
            "version": versions["required_target_bindings"],
        },
    }
    if versions["authoring_graph_core"] is not None:
        receipt["authoring_graph_core_projection"] = {
            "contract": "speedtree_spm_authoring_graph_core_projection",
            "version": versions["authoring_graph_core"],
        }
    if versions["target_requirements"] is not None:
        receipt["target_requirements"] = {
            "contract": "speedtree_stale_node_target_requirements",
            "version": versions["target_requirements"],
        }
    return receipt


class ReceiptCompatibilityFixtureTests(unittest.TestCase):
    def test_fixture_is_sanitized_and_issue_scoped(self):
        raw = FIXTURE.read_text(encoding="utf-8")
        data = json.loads(raw)
        self.assertEqual(data["schema_version"], 1)
        self.assertEqual(data["issue_number"], 41)
        self.assertIsNone(WINDOWS_PATH_RE.search(raw))
        self.assertIsNone(RAW_GUID_RE.search(raw))

    def test_literal_matrix_matches_the_runtime_registry(self):
        for case in load_fixture()["compatibility_matrix"]:
            with self.subTest(case=case["name"]):
                receipt = receipt_for(case)
                if case["result"] == "supported":
                    self.assertEqual(
                        _resolve_receipt_dialect(receipt),
                        case["name"],
                    )
                else:
                    with self.assertRaises(
                        StaleNodeTableRecoveryError
                    ) as caught:
                        _resolve_receipt_dialect(receipt)
                    self.assertEqual(
                        caught.exception.reason_token,
                        case["reason_token"],
                    )

    def test_real_core_v1_probe_is_explicit_and_byte_preserving(self):
        probe = load_fixture()["sanitized_real_legacy_probe"]
        self.assertEqual(
            probe["projection_versions"],
            [3, 1, 1, 1, 1, None],
        )
        self.assertEqual(
            probe["reason_token"],
            "preimage_receipt_projection_version_unsupported",
        )
        self.assertTrue(probe["receipt_bytes_unchanged"])
        self.assertEqual(
            probe["receipt_sha256_before"],
            probe["receipt_sha256_after"],
        )
        for field in (
            "receipt_sha256_before",
            "receipt_sha256_after",
            "backup_raw_sha256",
            "authoring_graph_fingerprint",
            "authoring_graph_core_fingerprint",
            "generator_membership_fingerprint",
            "required_target_binding_fingerprint",
        ):
            self.assertRegex(probe[field], SHA256_RE)


if __name__ == "__main__":
    unittest.main()
