"""Isolated parallel job for the three large #42 XML acceptance pairs."""

import gzip
import hashlib
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
    _authoring_graph_core_projection,
    _legacy_authoring_graph_core_v4_projection,
)


REAL_FIXTURES = (
    Path(__file__).parent / "fixtures" / "issue_42" / "real"
)
PRIVATE_PATH_RE = re.compile(
    r"(?i)(PARK|OneDrive|Forestportfolio|[A-Z]:[\\/])"
)


def project(text):
    return _authoring_graph_core_projection(text)["fingerprint"]


def project_v4(text):
    return _legacy_authoring_graph_core_v4_projection(text)["fingerprint"]


class RealAuthoredTreeProjectionV4Tests(unittest.TestCase):
    def _assert_sanitized_real_xml_pair(self, pair_id):
        manifest_text = (REAL_FIXTURES / "manifest.json").read_text(
            encoding="utf-8"
        )
        manifest = json.loads(manifest_text)
        self.assertEqual(manifest["issue_number"], 42)
        self.assertEqual(len(manifest["pairs"]), 3)
        self.assertNotRegex(manifest_text, PRIVATE_PATH_RE)
        matches = [
            pair for pair in manifest["pairs"] if pair["pair_id"] == pair_id
        ]
        self.assertEqual(len(matches), 1)
        pair = matches[0]
        before_bytes = gzip.decompress(
            (REAL_FIXTURES / pair["before_fixture"]).read_bytes()
        )
        after_bytes = gzip.decompress(
            (REAL_FIXTURES / pair["after_fixture"]).read_bytes()
        )
        self.assertEqual(
            hashlib.sha256(before_bytes).hexdigest(),
            pair["sanitized_before_xml_sha256"],
        )
        self.assertEqual(
            hashlib.sha256(after_bytes).hexdigest(),
            pair["sanitized_after_xml_sha256"],
        )
        before_text = before_bytes.decode("utf-8")
        after_text = after_bytes.decode("utf-8")
        self.assertNotRegex(before_text + after_text, PRIVATE_PATH_RE)
        # The three 78-93 MB pairs are separate tests so the acceptance job can
        # start one on each worker instead of leaving one 160-second serial
        # tail. Assertions and fixture coverage are unchanged.
        before_core_v4 = project_v4(before_text)
        self.assertEqual(
            before_core_v4,
            pair["expected_core_fingerprint"],
        )
        self.assertEqual(before_core_v4, project_v4(after_text))
        self.assertEqual(project(before_text), project(after_text))

    def test_sanitized_lauraceae_11_pair_executes_the_projector(self):
        self._assert_sanitized_real_xml_pair("lauraceae_11")

    def test_sanitized_lauraceae_12_pair_executes_the_projector(self):
        self._assert_sanitized_real_xml_pair("lauraceae_12")

    def test_sanitized_densiflora_02_pair_executes_the_projector(self):
        self._assert_sanitized_real_xml_pair("densiflora_02")
