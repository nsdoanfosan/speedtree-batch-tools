import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
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

    def test_mismatched_batch_revision_warns_and_runs_asset_audit(self):
        with tempfile.TemporaryDirectory() as temporary:
            report_path = Path(temporary) / "audit.json"
            report = {
                "generated_at": "test",
                "status": "ok",
                "summary": {"total": 1, "by_status": {"ready": 1}},
                "items": [{"name": "tree", "status": "ready"}],
            }
            with mock.patch.object(
                audit,
                "load_config",
                return_value={},
            ) as load_config, mock.patch.object(
                audit,
                "make_report",
                return_value=report,
            ) as make_report, mock.patch.object(
                audit,
                "save_spm_analysis_cache",
            ):
                self._run_main(
                    "--expected-production-source-revision",
                    "0" * 64,
                    "--no-receipt",
                    "--json",
                    str(report_path),
                )
            load_config.assert_called_once()
            make_report.assert_called_once()
            payload = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["status"], "ok")
            self.assertFalse(
                payload["production_source_revision"]["matches_expected"]
            )
            warning = payload["production_source_revision_warning"]
            self.assertEqual(warning["severity"], "warning")
            self.assertFalse(warning["asset_failure"])

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

    def test_source_change_during_audit_warns_and_keeps_results(self):
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
                self._run_main(
                    "--expected-production-source-revision",
                    started.content_hash,
                    "--json",
                    str(report_path),
                )
            persist.assert_called_once()
            save_cache.assert_called_once()
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
            warning = payload["production_source_revision_warning"]
            self.assertEqual(warning["severity"], "warning")
            self.assertFalse(warning["asset_failure"])

    def test_asset_audit_failure_keeps_revision_and_exact_repair_evidence(self):
        with tempfile.TemporaryDirectory() as temporary:
            report_path = Path(temporary) / "audit.json"
            expected = audit._PROCESS_PRODUCTION_SOURCE_MANIFEST.content_hash
            target = Path(temporary) / "Cluster" / "SK_leaf_test_01.spm"
            failure = audit.AtlasManifestResolutionError(
                "sanitized stale mirror conflict",
                {"target_spm": str(target)},
            )
            repair_plan = {
                "status": "repairable",
                "reason_code": "atlas_manifest_mirror_conflict_repairable",
                "target_spm": str(target),
                "authority": str(Path(temporary) / "authority.json"),
                "mirrors": [str(Path(temporary) / "stale.json")],
            }

            with mock.patch.object(
                audit,
                "load_config",
                return_value={},
            ), mock.patch.object(
                audit,
                "make_report",
                side_effect=failure,
            ), mock.patch.object(
                audit,
                "atlas_manifest_mirror_repair_plan",
                return_value=repair_plan,
            ):
                with self.assertRaises(audit.AtlasManifestResolutionError):
                    self._run_main(
                        "--expected-production-source-revision",
                        expected,
                        "--no-receipt",
                        "--json",
                        str(report_path),
                    )

            payload = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["status"], "failed")
            self.assertEqual(payload["stage"], "asset_audit")
            self.assertTrue(
                payload["production_source_revision"]["matches_expected"]
            )
            self.assertEqual(
                payload["failure"]["reason_token"],
                "atlas_manifest_mirror_conflict_repairable",
            )
            self.assertEqual(payload["failure"]["evidence"], repair_plan)

    def test_cli_publishes_folder_progress_and_stage_markers(self):
        with tempfile.TemporaryDirectory() as temporary:
            report_path = Path(temporary) / "audit.json"
            expected = (
                audit._PROCESS_PRODUCTION_SOURCE_MANIFEST.content_hash
            )
            report = {
                "generated_at": "test",
                "config": {},
                "summary": {"total": 2, "by_status": {"ok": 2}},
                "items": [
                    {"name": "tree_a", "status": "ok"},
                    {"name": "tree_b", "status": "ok"},
                ],
            }

            def make_report(*_args, **kwargs):
                progress = kwargs["progress_callback"]
                progress(1, 2, Path("Tree_A"))
                progress(2, 2, Path("Tree_B"))
                return report

            output = io.StringIO()
            with mock.patch.object(
                audit, "load_config", return_value={}
            ), mock.patch.object(
                audit, "make_report", side_effect=make_report
            ), mock.patch.object(
                audit, "save_spm_analysis_cache"
            ), redirect_stdout(output):
                self._run_main(
                    "--expected-production-source-revision",
                    expected,
                    "--no-receipt",
                    "--json",
                    str(report_path),
                )

            progress = output.getvalue()
            self.assertIn(audit.CLUSTER_LIVE_AUDIT_START_MARKER, progress)
            self.assertIn(
                audit.CLUSTER_LIVE_AUDIT_REVISION_OK_MARKER,
                progress,
            )
            self.assertIn(
                audit.CLUSTER_LIVE_AUDIT_REPORT_START_MARKER,
                progress,
            )
            self.assertIn(
                f"{audit.CLUSTER_LIVE_AUDIT_FOLDER_DONE_MARKER} "
                "completed=1 total=2 folder=Tree_A",
                progress,
            )
            self.assertIn(
                audit.CLUSTER_LIVE_AUDIT_RECEIPT_DONE_MARKER,
                progress,
            )
            self.assertIn(audit.CLUSTER_LIVE_AUDIT_DONE_MARKER, progress)

    def test_completed_single_folder_failure_keeps_exact_reason_and_exit_code(self):
        with tempfile.TemporaryDirectory() as temporary:
            report_path = Path(temporary) / "audit.json"
            expected = audit._PROCESS_PRODUCTION_SOURCE_MANIFEST.content_hash
            failure = {
                "scope": "folder",
                "reason_token": "atlas_manifest_ownership_conflict",
                "evidence": {
                    "folder": str(Path(temporary) / "Tree_failed"),
                    "target_spm": str(
                        Path(temporary) / "Tree_failed" / "SK_failed.spm"
                    ),
                },
            }
            report = {
                "generated_at": "test",
                "status": "failed",
                "stage": "asset_audit",
                "failure": failure,
                "failures": [failure],
                "summary": {
                    "total": 1,
                    "by_status": {"audit_failed": 1},
                    "failed_folder_count": 1,
                    "completed_folder_count": 0,
                },
                "items": [{
                    "name": "Tree_failed",
                    "status": "audit_failed",
                    "audit_complete": False,
                    "failure": failure,
                }],
            }
            output = io.StringIO()
            with mock.patch.object(
                audit, "load_config", return_value={}
            ), mock.patch.object(
                audit, "make_report", return_value=report
            ), mock.patch.object(
                audit, "save_spm_analysis_cache"
            ), redirect_stdout(output):
                with self.assertRaises(SystemExit) as raised:
                    self._run_main(
                        "--expected-production-source-revision",
                        expected,
                        "--no-receipt",
                        "--json",
                        str(report_path),
                    )

            self.assertEqual(raised.exception.code, 2)
            payload = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["failure"], failure)
            self.assertFalse(
                payload["cluster_assembly_receipt_persistence"]
                ["live_audit_complete"]
            )
            progress = output.getvalue()
            self.assertIn(audit.CLUSTER_LIVE_AUDIT_FAILED_MARKER, progress)
            self.assertIn(
                f"{audit.CLUSTER_LIVE_AUDIT_DONE_MARKER} status=failed",
                progress,
            )

    def test_partial_report_finishes_after_recording_failed_folder(self):
        with tempfile.TemporaryDirectory() as temporary:
            report_path = Path(temporary) / "audit.json"
            expected = audit._PROCESS_PRODUCTION_SOURCE_MANIFEST.content_hash
            failure = {
                "scope": "folder",
                "reason_token": "atlas_manifest_ownership_conflict",
                "evidence": {"folder": "Tree_failed"},
            }
            report = {
                "generated_at": "test",
                "status": "partial",
                "failures": [failure],
                "summary": {
                    "total": 2,
                    "by_status": {"ready": 1, "audit_failed": 1},
                    "failed_folder_count": 1,
                    "completed_folder_count": 1,
                },
                "items": [
                    {"name": "Tree_ready", "status": "ready"},
                    {
                        "name": "Tree_failed",
                        "status": "audit_failed",
                        "audit_complete": False,
                        "failure": failure,
                    },
                ],
            }
            output = io.StringIO()
            with mock.patch.object(
                audit, "load_config", return_value={}
            ), mock.patch.object(
                audit, "make_report", return_value=report
            ), mock.patch.object(
                audit, "save_spm_analysis_cache"
            ), redirect_stdout(output):
                self._run_main(
                    "--expected-production-source-revision",
                    expected,
                    "--no-receipt",
                    "--json",
                    str(report_path),
                )

            payload = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["status"], "partial")
            self.assertFalse(
                payload["cluster_assembly_receipt_persistence"]
                ["live_audit_complete"]
            )
            self.assertIn(
                f"{audit.CLUSTER_LIVE_AUDIT_DONE_MARKER} status=partial",
                output.getvalue(),
            )


if __name__ == "__main__":
    unittest.main()
