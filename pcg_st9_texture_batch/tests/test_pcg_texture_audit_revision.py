import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


PACKAGE_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = PACKAGE_DIR.parent
sys.path.insert(0, str(REPO_ROOT))

from pcg_st9_texture_batch import pcg_texture_audit as audit  # noqa: E402
from sk_batch.code_compile_gate import production_source_manifest  # noqa: E402


class PCGTextureAuditProductionRevisionTests(unittest.TestCase):
    def _run_main(self, *arguments):
        with mock.patch.object(
            sys,
            "argv",
            ["pcg_texture_audit.py", *arguments],
        ):
            return audit.main()

    def test_mismatched_batch_revision_fails_before_asset_audit(self):
        with tempfile.TemporaryDirectory() as temporary:
            report_path = Path(temporary) / "audit.json"
            with mock.patch.object(
                audit,
                "load_config",
            ) as load_config, mock.patch.object(
                audit,
                "make_report",
            ) as make_report:
                with self.assertRaises(SystemExit) as raised:
                    self._run_main(
                        "--expected-production-source-revision",
                        "0" * 64,
                        "--json",
                        str(report_path),
                    )
            self.assertEqual(raised.exception.code, 2)
            load_config.assert_not_called()
            make_report.assert_not_called()
            payload = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["stage"], "production_source_revision")
            self.assertFalse(
                payload["production_source_revision"]["matches_expected"]
            )

    def test_matching_batch_revision_is_written_to_child_report(self):
        with tempfile.TemporaryDirectory() as temporary:
            report_path = Path(temporary) / "audit.json"
            expected = (
                audit._PROCESS_PRODUCTION_SOURCE_MANIFEST.content_hash
            )
            report = {
                "generated_at": "test",
                "config": {},
                "summary": {"total": 1, "by_status": {"ok": 1}},
                "items": [{"name": "tree", "status": "ok"}],
            }
            with mock.patch.object(
                audit,
                "load_config",
                return_value={},
            ), mock.patch.object(
                audit,
                "make_report",
                return_value=report,
            ), mock.patch.object(
                audit,
                "save_spm_analysis_cache",
            ):
                self._run_main(
                    "--expected-production-source-revision",
                    expected,
                    "--no-receipt",
                    "--json",
                    str(report_path),
                )
            payload = json.loads(report_path.read_text(encoding="utf-8"))
            revision = payload["production_source_revision"]
            self.assertEqual(
                revision["started"]["content_hash"],
                expected,
            )
            self.assertEqual(
                revision["finished"]["content_hash"],
                expected,
            )
            self.assertEqual(
                revision["started"]["files"],
                revision["finished"]["files"],
            )
            self.assertTrue(revision["matches_expected"])
            self.assertTrue(revision["stable"])

    def test_source_change_during_audit_fails_before_receipt_persistence(self):
        with tempfile.TemporaryDirectory() as temporary:
            report_path = Path(temporary) / "audit.json"
            started = audit._PROCESS_PRODUCTION_SOURCE_MANIFEST
            changed_root = Path(temporary) / "changed_code"
            changed_root.mkdir()
            (changed_root / "worker.py").write_text(
                "revision = 2\n",
                encoding="utf-8",
            )
            finished = production_source_manifest(changed_root)
            report = {
                "generated_at": "test",
                "config": {},
                "summary": {"total": 1, "by_status": {"ok": 1}},
                "items": [{"name": "tree", "status": "ok"}],
            }
            with mock.patch.object(
                audit,
                "load_config",
                return_value={},
            ), mock.patch.object(
                audit,
                "make_report",
                return_value=report,
            ), mock.patch.object(
                audit,
                "production_source_manifest",
                return_value=finished,
            ), mock.patch.object(
                audit,
                "persist_cluster_assembly_receipts_safely",
            ) as persist, mock.patch.object(
                audit,
                "save_spm_analysis_cache",
            ) as save_cache:
                with self.assertRaises(SystemExit) as raised:
                    self._run_main(
                        "--expected-production-source-revision",
                        started.content_hash,
                        "--json",
                        str(report_path),
                    )
            self.assertEqual(raised.exception.code, 2)
            persist.assert_not_called()
            save_cache.assert_not_called()
            payload = json.loads(report_path.read_text(encoding="utf-8"))
            revision = payload["production_source_revision"]
            self.assertEqual(
                revision["started"]["content_hash"],
                started.content_hash,
            )
            self.assertEqual(
                revision["finished"]["content_hash"],
                finished.content_hash,
            )
            self.assertFalse(revision["stable"])


if __name__ == "__main__":
    unittest.main()
