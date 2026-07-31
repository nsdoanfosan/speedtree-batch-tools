import gzip
import json
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path


SK_BATCH_DIR = Path(__file__).resolve().parents[1]
import sys

if str(SK_BATCH_DIR) not in sys.path:
    sys.path.insert(0, str(SK_BATCH_DIR))

from spm_problem_node_marker import (  # noqa: E402
    MARKER_VALUES,
    mark_problem_generators,
    marker_receipt_path,
    restore_problem_generator_markers,
)


FOREGROUND = {
    "m_bSetForegroundIconColor": "true",
    "m_vecForegroundIconColor_r": "0",
    "m_vecForegroundIconColor_g": "0",
    "m_vecForegroundIconColor_b": "0",
    "m_vecForegroundIconColor_a": "1",
}
BACKGROUND = {
    "m_bSetBackgroundIconColor": "true",
    "m_vecBackgroundIconColor_r": "0",
    "m_vecBackgroundIconColor_g": "1",
    "m_vecBackgroundIconColor_b": "0.05",
    "m_vecBackgroundIconColor_a": "1",
}


def write_spm(path):
    root = ET.Element("SpeedTree")
    generator = ET.SubElement(root, "Generator", Type="Leaf Mesh")
    ET.SubElement(generator, "Name").text = "Leaf Problem"
    ET.SubElement(generator, "GUID").text = "problem-guid"
    ET.SubElement(generator, "Hidden").text = "false"
    extra = ET.SubElement(generator, "Extra")
    for tag, value in {**FOREGROUND, **BACKGROUND}.items():
        ET.SubElement(extra, tag).text = value
    path.write_bytes(gzip.compress(ET.tostring(root, encoding="utf-8")))


def fields(path):
    root = ET.fromstring(gzip.decompress(path.read_bytes()))
    extra = root.find("./Generator/Extra")
    return {child.tag: child.text for child in extra}


class ProblemNodeMarkerTests(unittest.TestCase):
    def test_marker_preserves_background_and_restores_exact_foreground(self):
        with tempfile.TemporaryDirectory() as temporary:
            spm = Path(temporary) / "SK_tree_marker.spm"
            write_spm(spm)
            problem = [{
                "generator_guid": "problem-guid",
                "generator_name": "Leaf Problem",
                "material_name": "M_leaf_missing",
            }]

            marked = mark_problem_generators(spm, problem)
            after_mark = fields(spm)
            second = mark_problem_generators(spm, problem)
            restored = restore_problem_generator_markers(spm)
            after_restore = fields(spm)

            self.assertEqual(marked["status"], "marked")
            self.assertTrue(marked["changed"])
            self.assertFalse(second["changed"])
            self.assertEqual(
                {tag: after_mark[tag] for tag in BACKGROUND}, BACKGROUND
            )
            self.assertEqual(
                {tag: after_mark[tag] for tag in MARKER_VALUES}, MARKER_VALUES
            )
            self.assertEqual(restored["status"], "restored")
            self.assertEqual(
                {tag: after_restore[tag] for tag in FOREGROUND}, FOREGROUND
            )
            receipt = json.loads(
                marker_receipt_path(spm).read_text(encoding="utf-8")
            )
            self.assertEqual(receipt["status"], "restored")
            self.assertGreaterEqual(len(receipt["backups"]), 2)

    def test_restore_does_not_overwrite_user_foreground_edit(self):
        with tempfile.TemporaryDirectory() as temporary:
            spm = Path(temporary) / "SK_tree_marker_conflict.spm"
            write_spm(spm)
            mark_problem_generators(spm, [{"generator_guid": "problem-guid"}])

            root = ET.fromstring(gzip.decompress(spm.read_bytes()))
            root.find(
                "./Generator/Extra/m_vecForegroundIconColor_r"
            ).text = "0.25"
            spm.write_bytes(gzip.compress(ET.tostring(root, encoding="utf-8")))

            result = restore_problem_generator_markers(spm)

            self.assertEqual(result["status"], "restore_conflict")
            self.assertFalse(result["changed"])
            self.assertEqual(
                fields(spm)["m_vecForegroundIconColor_r"], "0.25"
            )


if __name__ == "__main__":
    unittest.main()
