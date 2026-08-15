import sys
import gzip
import tempfile
import unittest
from pathlib import Path


SK_BATCH_DIR = Path(__file__).resolve().parents[1]
if str(SK_BATCH_DIR) not in sys.path:
    sys.path.insert(0, str(SK_BATCH_DIR))

import spm_audit


class CollisionPruningNormalizationTests(unittest.TestCase):
    def test_normalizes_existing_global_values_and_is_idempotent(self):
        source = (
            "<Window><Extra>\n"
            "\t<m_eCollisionQuality>0</m_eCollisionQuality>\n"
            "\t<m_bShadePruning>false</m_bShadePruning>\n"
            "\t<GrowthCurve></GrowthCurve>\n"
            "</Extra></Window>"
        )

        normalized, report = spm_audit.normalize_collision_pruning_settings(
            source
        )

        self.assertIn(
            "<m_eCollisionQuality>3</m_eCollisionQuality>", normalized
        )
        self.assertIn("<m_bShadePruning>true</m_bShadePruning>", normalized)
        self.assertEqual(report["quality_before"], ["0"])
        self.assertEqual(report["shade_pruning_before"], ["false"])
        self.assertTrue(report["changed"])

        second, second_report = (
            spm_audit.normalize_collision_pruning_settings(normalized)
        )
        self.assertEqual(second, normalized)
        self.assertFalse(second_report["changed"])

    def test_inserts_missing_settings_before_global_growth_curve(self):
        source = (
            "<Window><Extra>\n"
            "\t\t<GrowthCurve><Value>1</Value></GrowthCurve>\n"
            "</Extra></Window>"
        )

        normalized, report = spm_audit.normalize_collision_pruning_settings(
            source
        )

        self.assertLess(
            normalized.index("<m_eCollisionQuality>"),
            normalized.index("<GrowthCurve>"),
        )
        self.assertEqual(
            report["inserted"],
            ["m_eCollisionQuality", "m_bShadePruning"],
        )

    def test_file_normalization_backs_up_once_and_then_is_a_noop(self):
        source = (
            "<SpeedTree><Window><Extra>\n"
            "\t<m_eCollisionQuality>1</m_eCollisionQuality>\n"
            "\t<m_bShadePruning>false</m_bShadePruning>\n"
            "\t<GrowthCurve />\n"
            "</Extra></Window></SpeedTree>"
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            spm = Path(temp_dir) / "tree.spm"
            spm.write_bytes(gzip.compress(source.encode("utf-8")))

            first = spm_audit.normalize_collision_pruning_file(spm)
            second = spm_audit.normalize_collision_pruning_file(spm)

            self.assertEqual(first["status"], "normalized")
            self.assertTrue(Path(first["backup"]).is_file())
            self.assertEqual(second["status"], "already-ok")
            self.assertIsNone(second["backup"])
            normalized = spm_audit.read_spm(spm)
            self.assertIn(
                "<m_eCollisionQuality>3</m_eCollisionQuality>", normalized
            )
            self.assertIn(
                "<m_bShadePruning>true</m_bShadePruning>", normalized
            )

    def test_recursive_scan_excludes_pipeline_and_history_copies(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            live = root / "tree" / "SK_tree_01.spm"
            hidden = root / "tree" / ".sk_batch_isolated" / "copy.spm"
            backup = root / "tree" / "_spm_backups" / "copy.spm"
            probe = root / "tree" / "_codex_probe_tree.spm"
            for path in (live, hidden, backup, probe):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"spm")

            discovered = list(spm_audit.recursive_live_spm_paths(root))

            self.assertEqual(discovered, [live])


if __name__ == "__main__":
    unittest.main()
