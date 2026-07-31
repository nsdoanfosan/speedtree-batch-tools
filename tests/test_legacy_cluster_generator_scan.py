"""Guard the narrowed Generator scan against the block walk it replaced."""

import gzip
import re
import sys
import tempfile
import unittest
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parents[1]
if str(REPO_DIR) not in sys.path:
    sys.path.insert(0, str(REPO_DIR))

import speedtree_legacy_cluster_contract as contract  # noqa: E402


def reference_foregrounds(text):
    """The former implementation: materialize each block, search inside it."""
    rows = {}
    duplicate_guids = set()
    for match in contract.GENERATOR_BLOCK_RE.finditer(text):
        block = match.group(0)
        guid_match = contract.GUID_RE.search(block)
        guid = guid_match.group(1).strip() if guid_match else ""
        if not guid:
            continue
        if guid in rows:
            duplicate_guids.add(guid)
            continue
        rows[guid] = {
            tag: contract._tag_value(block, tag)
            for tag in contract.FOREGROUND_TAGS
        }
    return rows, duplicate_guids


def generator_xml(guid, *, marked=False, name="Leaf", extra=""):
    color = ""
    if marked:
        color = (
            "<m_bSetForegroundIconColor>true</m_bSetForegroundIconColor>"
            "<m_vecForegroundIconColor_r>1</m_vecForegroundIconColor_r>"
            "<m_vecForegroundIconColor_g>0</m_vecForegroundIconColor_g>"
            "<m_vecForegroundIconColor_b>1</m_vecForegroundIconColor_b>"
            "<m_vecForegroundIconColor_a>1</m_vecForegroundIconColor_a>"
        )
    return (
        f'<Generator Type="Leaf Mesh"><Name>{name}</Name>'
        f"<GUID>{guid}</GUID>{color}{extra}</Generator>"
    )


def document(generators_section, tail=""):
    return (
        '<?xml version="1.0"?><SpeedTree>'
        f"<Generators>{generators_section}</Generators>"
        f"{tail}"
        "</SpeedTree>"
    )


class GeneratorSectionScanTests(unittest.TestCase):
    def assert_matches_reference(self, text):
        section = contract._generator_section_bytes(text.encode("utf-8"))
        self.assertEqual(
            contract._generator_foregrounds_from_text(
                section.decode("utf-8")
            ),
            reference_foregrounds(text),
        )

    def test_matches_reference_for_marked_and_plain_generators(self):
        self.assert_matches_reference(document(
            generator_xml("aaa", marked=True)
            + generator_xml("bbb")
            + generator_xml("ccc", marked=True, name="Leaf 2")
        ))

    def test_duplicate_guid_is_reported_once(self):
        text = document(
            generator_xml("dup", marked=True) + generator_xml("dup")
        )
        rows, duplicates = contract._generator_foregrounds_from_text(
            contract._generator_section_bytes(
                text.encode("utf-8")
            ).decode("utf-8")
        )
        self.assertEqual(duplicates, {"dup"})
        self.assertEqual(
            rows["dup"]["m_bSetForegroundIconColor"], "true"
        )
        self.assert_matches_reference(text)

    def test_only_first_guid_of_a_block_identifies_the_generator(self):
        self.assert_matches_reference(document(
            generator_xml(
                "outer",
                marked=True,
                extra="<Child><GUID>inner</GUID></Child>",
            )
        ))

    def test_nodes_section_is_not_scanned_but_stays_equivalent(self):
        nodes = "<Nodes>" + "".join(
            f"<Node><GeneratorGUID>g{index}</GeneratorGUID>"
            f"<ParentGUID></ParentGUID><Name>n</Name><GUID>x{index}</GUID>"
            "<Hidden>false</Hidden><Extra></Extra></Node>"
            for index in range(200)
        ) + "</Nodes>"
        text = document(generator_xml("aaa", marked=True), tail=nodes)
        section = contract._generator_section_bytes(text.encode("utf-8"))
        self.assertLess(len(section), len(text.encode("utf-8")))
        self.assert_matches_reference(text)

    def test_generator_outside_the_container_forces_a_full_scan(self):
        # Production tree_Weeping_Willow_01_back.spm keeps 258 of its 259
        # Generators after </Generators>; narrowing must not lose them.
        text = document(
            generator_xml("inside", marked=True),
            tail=generator_xml("outside", marked=True),
        )
        raw = text.encode("utf-8")
        self.assertEqual(contract._generator_section_bytes(raw), raw)
        rows, _ = contract._generator_foregrounds_from_text(text)
        self.assertEqual(set(rows), {"inside", "outside"})
        self.assert_matches_reference(text)

    def test_missing_container_falls_back_to_the_whole_document(self):
        text = (
            '<?xml version="1.0"?><SpeedTree>'
            + generator_xml("loose", marked=True)
            + "</SpeedTree>"
        )
        raw = text.encode("utf-8")
        self.assertEqual(contract._generator_section_bytes(raw), raw)
        self.assert_matches_reference(text)

    def test_case_variant_tags_are_still_read(self):
        text = (
            '<?xml version="1.0"?><SPEEDTREE><GENERATORS>'
            '<GENERATOR Type="Frond"><guid>Mixed</guid>'
            "<M_BSETFOREGROUNDICONCOLOR>true</M_BSETFOREGROUNDICONCOLOR>"
            "</GENERATOR></GENERATORS></SPEEDTREE>"
        )
        rows, _ = contract._generator_foregrounds_from_text(
            contract._generator_section_bytes(
                text.encode("utf-8")
            ).decode("utf-8")
        )
        self.assertEqual(
            rows["Mixed"]["m_bSetForegroundIconColor"], "true"
        )
        self.assert_matches_reference(text)

    def test_gzipped_file_reads_the_same_rows(self):
        text = document(
            generator_xml("aaa", marked=True) + generator_xml("bbb")
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            plain = Path(temp_dir) / "plain.spm"
            packed = Path(temp_dir) / "packed.spm"
            plain.write_text(text, encoding="utf-8")
            packed.write_bytes(gzip.compress(text.encode("utf-8")))
            self.assertEqual(
                contract._generator_foregrounds(plain),
                contract._generator_foregrounds(packed),
            )
            self.assertEqual(
                contract._generator_foregrounds(packed),
                reference_foregrounds(text),
            )

    def test_generators_container_is_not_mistaken_for_a_generator(self):
        text = document(generator_xml("aaa", marked=True))
        self.assertIsNone(
            contract.GENERATOR_BOUNDARY_RE.match("<Generators>")
        )
        self.assertIsNone(
            contract.GENERATOR_BOUNDARY_RE.match("</Generators>")
        )
        rows, _ = contract._generator_foregrounds_from_text(text)
        self.assertEqual(set(rows), {"aaa"})

    def test_unclosed_block_is_dropped_like_the_reference(self):
        text = (
            '<?xml version="1.0"?><SpeedTree><Generators>'
            '<Generator Type="Frond"><GUID>never_closed</GUID>'
        )
        self.assertEqual(
            contract._generator_foregrounds_from_text(text),
            reference_foregrounds(text),
        )


class LegacyStateSurfaceTests(unittest.TestCase):
    def test_inspect_reports_marker_drift_without_a_receipt_read_of_nodes(self):
        text = document(
            generator_xml("kept", marked=True) + generator_xml("drifted")
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            spm = Path(temp_dir) / "cluster.spm"
            spm.write_text(text, encoding="utf-8")
            receipt = contract.marker_receipt_path(spm)
            receipt.parent.mkdir(parents=True, exist_ok=True)
            receipt.write_text(
                '{"kind": "%s", "version": %d, "status": "applied",'
                ' "spm": %s, "generator_guids": ["kept", "drifted"]}'
                % (
                    contract.RECEIPT_KIND,
                    contract.RECEIPT_VERSION,
                    repr(str(spm)).replace("'", '"').replace("\\", "\\\\"),
                ),
                encoding="utf-8",
            )
            state = contract.inspect_legacy_cluster_state(spm)
        self.assertTrue(state["receipt_valid"])
        self.assertEqual(
            state["classified_generator_guids"], ["drifted", "kept"]
        )
        self.assertEqual(state["marker_drift_guids"], ["drifted"])
        self.assertEqual(state["missing_generator_guids"], [])


if __name__ == "__main__":
    unittest.main()
