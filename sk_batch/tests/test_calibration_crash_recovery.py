"""A killed ① run must not leave probe bones in the source SPM.

Calibration rewrites the SPM in place and restores it on every exception path,
but Stop and the watchdog timeout both use ``taskkill /T /F`` and run no
handler at all.  The in-progress marker makes that state detectable, and the
next run repairs it from the recorded backup before reading the source.
"""
import ctypes
import hashlib
import gzip
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock


SK_BATCH_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SK_BATCH_DIR))

import spm_audit


SOURCE_XML = "<SpeedTreeRaw><Generator Type=\"Tree\"/></SpeedTreeRaw>"
PROBE_XML = "<SpeedTreeRaw><Generator Type=\"Tree\" probe=\"1\"/></SpeedTreeRaw>"


class CalibrationCrashRecoveryTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.spm = self.root / "SK_crash_test.spm"
        spm_audit.write_spm(self.spm, SOURCE_XML)
        self.source_bytes = self.spm.read_bytes()
        self.source_sha = hashlib.sha256(self.source_bytes).hexdigest()

    def tearDown(self):
        self._tmp.cleanup()

    def _simulate_kill_during_calibration(self, backup=True):
        """Reproduce a taskkill: backup + marker exist, source left mid-write."""
        backup_path = spm_audit.backup_spm(self.spm) if backup else None
        spm_audit.write_calibration_marker(self.spm, backup_path, self.source_sha)
        spm_audit.write_spm(self.spm, PROBE_XML)
        return backup_path

    def _make_marker_legacy(self, *, version=1):
        marker = spm_audit.calibration_marker_path(self.spm)
        payload = json.loads(marker.read_text(encoding="utf-8"))
        payload["version"] = version
        payload.pop("last_pipeline_sha256", None)
        marker.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        return marker

    def test_identical_content_always_writes_identical_bytes(self):
        """Otherwise a no-op ① rewrite invalidates every content fingerprint."""
        first = self.spm.read_bytes()
        spm_audit.write_spm(self.spm, PROBE_XML)
        spm_audit.write_spm(self.spm, SOURCE_XML)
        self.assertEqual(self.spm.read_bytes(), first)
        self.assertEqual(spm_audit.read_spm(self.spm), SOURCE_XML)

    def test_write_leaves_no_temporary_file_and_keeps_the_container(self):
        spm_audit.write_spm(self.spm, PROBE_XML)
        siblings = sorted(
            path.name for path in self.root.iterdir() if path.is_file()
        )
        self.assertEqual(siblings, [self.spm.name])
        from speedtree_pipeline_contract import spm_container_format

        self.assertEqual(spm_container_format(self.spm), "gzip")

    def test_plain_xml_spm_stays_plain_xml(self):
        plain = self.root / "SK_plain.spm"
        plain.write_text(SOURCE_XML, encoding="utf-8")
        spm_audit.write_spm(plain, PROBE_XML)
        from speedtree_pipeline_contract import spm_container_format

        self.assertEqual(spm_container_format(plain), "plain_xml")
        self.assertEqual(spm_audit.read_spm(plain), PROBE_XML)

    def test_marker_lives_in_the_backup_folder_and_is_never_scanned(self):
        marker = spm_audit.calibration_marker_path(self.spm)
        self.assertEqual(marker.parent.name, spm_audit.BACKUP_SUBDIR)
        from speedtree_pipeline_contract import BACKUP_DIRECTORY_NAMES

        self.assertIn(marker.parent.name, BACKUP_DIRECTORY_NAMES)

    def test_interrupted_source_is_detected(self):
        self._simulate_kill_during_calibration()
        state = spm_audit.inspect_interrupted_calibration(self.spm)
        self.assertEqual(state["status"], "interrupted")
        self.assertTrue(state["backup_available"])
        self.assertFalse(state["spm_matches_source"])

    def test_recovery_restores_the_exact_source_bytes(self):
        self._simulate_kill_during_calibration()
        result = spm_audit.recover_interrupted_calibration(self.spm)
        self.assertTrue(result["recovered"])
        self.assertEqual(self.spm.read_bytes(), self.source_bytes)
        self.assertFalse(spm_audit.calibration_marker_path(self.spm).exists())

    def test_recovery_preserves_external_edit_after_pipeline_probe(self):
        self._simulate_kill_during_calibration()
        external_bytes = b"external SpeedTree edit"
        self.spm.write_bytes(external_bytes)

        result = spm_audit.recover_interrupted_calibration(self.spm)

        self.assertFalse(result["recovered"])
        self.assertEqual(result["status"], "concurrent_spm_modification")
        self.assertEqual(self.spm.read_bytes(), external_bytes)
        self.assertTrue(spm_audit.calibration_marker_path(self.spm).exists())

    def test_legacy_marker_logical_match_clears_without_rewriting_spm(self):
        backup_path = spm_audit.backup_spm(self.spm)
        spm_audit.write_calibration_marker(
            self.spm,
            backup_path,
            self.source_sha,
        )
        marker = self._make_marker_legacy()
        # A legacy SpeedTree/gzip rewrite may change container bytes while
        # preserving the exact logical SPM XML.
        self.spm.write_bytes(
            gzip.compress(SOURCE_XML.encode("utf-8"), mtime=1)
        )
        before_bytes = self.spm.read_bytes()
        before_mtime_ns = self.spm.stat().st_mtime_ns
        self.assertNotEqual(before_bytes, self.source_bytes)

        result = spm_audit.recover_interrupted_calibration(self.spm)

        self.assertFalse(result["recovered"])
        self.assertTrue(result["cleared"])
        self.assertEqual(result["status"], "legacy_marker_logically_intact")
        self.assertEqual(
            result["legacy_marker_migration"]["policy"],
            "clear_marker_without_spm_write",
        )
        self.assertEqual(self.spm.read_bytes(), before_bytes)
        self.assertEqual(self.spm.stat().st_mtime_ns, before_mtime_ns)
        self.assertFalse(marker.exists())

    def test_legacy_cluster_marker_requires_live_root_postcondition(self):
        backup_path = spm_audit.backup_spm(self.spm)
        spm_audit.write_calibration_marker(
            self.spm,
            backup_path,
            self.source_sha,
        )
        marker = self._make_marker_legacy()
        self.spm.write_bytes(
            gzip.compress(SOURCE_XML.encode("utf-8"), mtime=2)
        )
        before_bytes = self.spm.read_bytes()
        postcondition = {
            "ok": True,
            "policy": "cluster_first_renderable_root_absolute_1",
        }

        with mock.patch.object(
            spm_audit,
            "is_cluster_normalization_spm",
            return_value=True,
        ), mock.patch.object(
            spm_audit,
            "cluster_root_logical_postcondition",
            return_value=postcondition,
        ) as check:
            result = spm_audit.recover_interrupted_calibration(self.spm)

        check.assert_called_once_with(SOURCE_XML)
        self.assertTrue(result["cleared"])
        self.assertEqual(
            result["legacy_marker_migration"]["cluster_postcondition"],
            postcondition,
        )
        self.assertEqual(self.spm.read_bytes(), before_bytes)
        self.assertFalse(marker.exists())

    def test_legacy_cluster_marker_fails_closed_on_bad_postcondition(self):
        backup_path = spm_audit.backup_spm(self.spm)
        spm_audit.write_calibration_marker(
            self.spm,
            backup_path,
            self.source_sha,
        )
        marker = self._make_marker_legacy()
        self.spm.write_bytes(
            gzip.compress(SOURCE_XML.encode("utf-8"), mtime=3)
        )
        before_bytes = self.spm.read_bytes()

        with mock.patch.object(
            spm_audit,
            "is_cluster_normalization_spm",
            return_value=True,
        ), mock.patch.object(
            spm_audit,
            "cluster_root_logical_postcondition",
            return_value={"ok": False, "error": "bad cluster root"},
        ):
            result = spm_audit.recover_interrupted_calibration(self.spm)

        self.assertFalse(result["recovered"])
        self.assertFalse(result["cleared"])
        self.assertEqual(result["failure_kind"], "interrupted_calibration")
        self.assertEqual(
            result["diagnostic"]["category"],
            "legacy_marker_cluster_postcondition_failed",
        )
        self.assertEqual(self.spm.read_bytes(), before_bytes)
        self.assertTrue(marker.exists())

    def test_legacy_marker_fails_closed_on_logical_mismatch(self):
        self._simulate_kill_during_calibration()
        marker = self._make_marker_legacy()
        before_bytes = self.spm.read_bytes()

        result = spm_audit.recover_interrupted_calibration(self.spm)

        self.assertFalse(result["recovered"])
        self.assertFalse(result["cleared"])
        self.assertEqual(
            result["failure_kind"],
            "concurrent_spm_modification",
        )
        self.assertEqual(
            result["diagnostic"]["category"],
            "legacy_marker_logical_mismatch",
        )
        self.assertEqual(self.spm.read_bytes(), before_bytes)
        self.assertTrue(marker.exists())

    def test_legacy_marker_restores_only_probe_bones_after_external_edit(self):
        source = """<SpeedTreeRaw>
<Generator Type="Branch"><Name>Branch</Name><GUID>branch-guid</GUID>
<Properties>
<Property><Name>Physics:Bone style</Name><Value>1</Value></Property>
<Property><Name>Physics:Bones</Name><Value>2.5</Value></Property>
</Properties></Generator>
</SpeedTreeRaw>"""
        probed_with_external_edit = """<SpeedTreeRaw>
<Generator Type="Branch"><Name>Branch</Name><GUID>branch-guid</GUID>
<Properties>
<Property><Name>Physics:Bone style</Name><Value>0</Value></Property>
<Property><Name>Physics:Bones</Name><Value>1</Value></Property>
</Properties></Generator>
<Materials><Material_v8 ID="9" Name="M_external_edit"/></Materials>
</SpeedTreeRaw>"""
        spm_audit.write_spm(self.spm, source)
        source_sha = hashlib.sha256(self.spm.read_bytes()).hexdigest()
        backup_path = spm_audit.backup_spm(self.spm)
        spm_audit.write_calibration_marker(
            self.spm,
            backup_path,
            source_sha,
        )
        spm_audit.write_spm(self.spm, probed_with_external_edit)
        marker = self._make_marker_legacy()

        result = spm_audit.recover_interrupted_calibration(self.spm)
        restored = spm_audit.read_spm(self.spm)

        self.assertTrue(result["recovered"])
        self.assertTrue(result["cleared"])
        self.assertEqual(
            result["status"],
            "legacy_marker_calibration_values_restored",
        )
        self.assertEqual(
            result["legacy_marker_migration"]["policy"],
            "restore_only_interrupted_branch_calibration_values",
        )
        self.assertIn('Name="M_external_edit"', restored)
        self.assertIn(
            "<Name>Physics:Bone style</Name><Value>1</Value>",
            restored,
        )
        self.assertIn(
            "<Name>Physics:Bones</Name><Value>2.5</Value>",
            restored,
        )
        self.assertFalse(marker.exists())

    def test_only_version_one_marker_may_use_legacy_no_write_migration(self):
        backup_path = spm_audit.backup_spm(self.spm)
        spm_audit.write_calibration_marker(
            self.spm,
            backup_path,
            self.source_sha,
        )
        marker = self._make_marker_legacy(version=2)
        self.spm.write_bytes(
            gzip.compress(SOURCE_XML.encode("utf-8"), mtime=4)
        )
        before_bytes = self.spm.read_bytes()

        result = spm_audit.recover_interrupted_calibration(self.spm)

        self.assertFalse(result["recovered"])
        self.assertFalse(result["cleared"])
        self.assertEqual(
            result["diagnostic"]["category"],
            "unsupported_marker_without_pipeline_hash",
        )
        self.assertEqual(self.spm.read_bytes(), before_bytes)
        self.assertTrue(marker.exists())

    def test_untouched_spm_reports_clean_and_recovers_nothing(self):
        state = spm_audit.inspect_interrupted_calibration(self.spm)
        self.assertEqual(state["status"], "clean")
        result = spm_audit.recover_interrupted_calibration(self.spm)
        self.assertFalse(result["recovered"])
        self.assertEqual(self.spm.read_bytes(), self.source_bytes)

    def test_kill_that_happened_to_leave_the_source_intact_only_clears(self):
        backup_path = spm_audit.backup_spm(self.spm)
        spm_audit.write_calibration_marker(self.spm, backup_path, self.source_sha)
        state = spm_audit.inspect_interrupted_calibration(self.spm)
        self.assertEqual(state["status"], "interrupted_but_intact")
        result = spm_audit.recover_interrupted_calibration(self.spm)
        self.assertFalse(result["recovered"])
        self.assertTrue(result["cleared"])
        self.assertFalse(spm_audit.calibration_marker_path(self.spm).exists())

    def test_intact_recovery_reports_marker_clear_lock(self):
        backup_path = spm_audit.backup_spm(self.spm)
        spm_audit.write_calibration_marker(
            self.spm,
            backup_path,
            self.source_sha,
        )
        diagnostic = spm_audit._operation_diagnostic(
            "process_file_lock",
            operation="clear_calibration_marker",
            target=spm_audit.calibration_marker_path(self.spm),
            attempts=3,
            error=PermissionError(13, "marker locked"),
        )
        with mock.patch.object(
            spm_audit,
            "_unlink_with_backoff",
            side_effect=spm_audit.SPMAtomicOperationError(diagnostic),
        ):
            result = spm_audit.recover_interrupted_calibration(self.spm)

        self.assertFalse(result["recovered"])
        self.assertFalse(result["cleared"])
        self.assertEqual(result["failure_kind"], "process_file_lock")
        self.assertEqual(
            result["diagnostic"]["category"],
            "process_file_lock",
        )
        self.assertTrue(spm_audit.calibration_marker_path(self.spm).exists())

    def test_backup_that_does_not_match_the_marker_is_refused(self):
        backup_path = self._simulate_kill_during_calibration()
        # A stale/unrelated backup must never overwrite the working file.
        spm_audit.write_spm(Path(backup_path), "<SpeedTreeRaw><Other/></SpeedTreeRaw>")
        result = spm_audit.recover_interrupted_calibration(self.spm)
        self.assertFalse(result["recovered"])
        self.assertIn("does not match", result["error"])
        self.assertEqual(spm_audit.read_spm(self.spm), PROBE_XML)

    def test_missing_backup_is_reported_without_touching_the_source(self):
        self._simulate_kill_during_calibration(backup=False)
        result = spm_audit.recover_interrupted_calibration(self.spm)
        self.assertFalse(result["recovered"])
        self.assertFalse(result["backup_available"])
        self.assertEqual(spm_audit.read_spm(self.spm), PROBE_XML)

    def _write_cluster_normalization_receipt(
        self,
        *,
        source_sha256=None,
        canonical_spm=None,
        status="ready",
    ):
        receipt = (
            self.spm.parent
            / "reports"
            / f"{self.spm.stem}_cluster_normalization_sync_receipt.json"
        )
        receipt.parent.mkdir(parents=True, exist_ok=True)
        receipt.write_text(
            json.dumps(
                {
                    "kind": "speedtree_cluster_sync_normalization",
                    "version": 3,
                    "status": status,
                    "canonical_spm": str(canonical_spm or self.spm),
                    "source_spm_sha256": source_sha256
                    or hashlib.sha256(self.spm.read_bytes()).hexdigest(),
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        return receipt

    def test_newer_cluster_receipt_supersedes_missing_backup_marker(self):
        self._simulate_kill_during_calibration(backup=False)
        before_bytes = self.spm.read_bytes()
        marker = spm_audit.calibration_marker_path(self.spm)
        marker_mtime_ns = marker.stat().st_mtime_ns
        receipt = self._write_cluster_normalization_receipt()
        os.utime(
            receipt,
            ns=(marker_mtime_ns + 2_000_000_000,) * 2,
        )

        result = spm_audit.recover_interrupted_calibration(self.spm)

        self.assertFalse(result["recovered"])
        self.assertTrue(result["cleared"])
        self.assertEqual(
            result["status"],
            "superseded_by_cluster_normalization_receipt",
        )
        self.assertEqual(self.spm.read_bytes(), before_bytes)
        self.assertFalse(marker.exists())
        self.assertEqual(
            result["superseding_receipt"]["source_spm_sha256"],
            hashlib.sha256(before_bytes).hexdigest(),
        )

    def test_older_cluster_receipt_cannot_supersede_missing_backup_marker(self):
        self._simulate_kill_during_calibration(backup=False)
        marker = spm_audit.calibration_marker_path(self.spm)
        marker_mtime_ns = marker.stat().st_mtime_ns
        receipt = self._write_cluster_normalization_receipt()
        os.utime(
            receipt,
            ns=(marker_mtime_ns - 2_000_000_000,) * 2,
        )

        result = spm_audit.recover_interrupted_calibration(self.spm)

        self.assertFalse(result["recovered"])
        self.assertFalse(result["cleared"])
        self.assertEqual(
            result["diagnostic"]["category"],
            "calibration_backup_missing",
        )
        self.assertTrue(marker.exists())

    def test_cluster_receipt_hash_mismatch_cannot_supersede_marker(self):
        self._simulate_kill_during_calibration(backup=False)
        marker = spm_audit.calibration_marker_path(self.spm)
        marker_mtime_ns = marker.stat().st_mtime_ns
        receipt = self._write_cluster_normalization_receipt(
            source_sha256="0" * 64,
        )
        os.utime(
            receipt,
            ns=(marker_mtime_ns + 2_000_000_000,) * 2,
        )

        result = spm_audit.recover_interrupted_calibration(self.spm)

        self.assertFalse(result["recovered"])
        self.assertFalse(result["cleared"])
        self.assertEqual(
            result["diagnostic"]["category"],
            "calibration_backup_missing",
        )
        self.assertTrue(marker.exists())

    def test_process_spm_repairs_before_reading_the_source(self):
        self._simulate_kill_during_calibration()
        seen = {}

        def fake_audit(path, text=None, analyze_bone_graph=False):
            seen["text"] = text
            return {"generators": [], "materials": [], "bone_graph": {}}

        with mock.patch.object(spm_audit, "audit_spm", side_effect=fake_audit):
            with mock.patch.object(
                spm_audit,
                "sk_readiness",
                return_value={
                    "ready": False,
                    "error": "stub",
                    "mode": "stub",
                    "disabled_generators": [],
                },
            ):
                report = spm_audit.process_spm(
                    self.spm, {"backup_spm": True}, log=lambda _m: None
                )

        # The recovered source, not the probe state, is what ① audits.
        self.assertEqual(seen["text"], SOURCE_XML)
        self.assertTrue(report["interrupted_calibration_recovery"]["recovered"])
        self.assertEqual(self.spm.read_bytes(), self.source_bytes)

    def test_unrecoverable_marker_blocks_before_audit(self):
        self._simulate_kill_during_calibration(backup=False)
        with mock.patch.object(spm_audit, "audit_spm") as audit:
            with self.assertRaisesRegex(
                RuntimeError, "cannot be recovered safely"
            ):
                spm_audit.process_spm(
                    self.spm, {"backup_spm": False}, log=lambda _m: None
                )
        audit.assert_not_called()
        self.assertTrue(
            spm_audit.calibration_marker_path(self.spm).is_file()
        )
        self.assertEqual(spm_audit.read_spm(self.spm), PROBE_XML)

    def test_canonical_spm_lock_serializes_another_process(self):
        acquired = self.root / "child_acquired.txt"
        code = "\n".join(
            (
                "import sys",
                "from pathlib import Path",
                "import spm_audit",
                "with spm_audit.spm_exclusive_lock(Path(sys.argv[1])):",
                "    Path(sys.argv[2]).write_text('acquired', encoding='utf-8')",
            )
        )
        env = dict(os.environ)
        repo_root = str(SK_BATCH_DIR.parent)
        env["PYTHONPATH"] = os.pathsep.join(
            filter(
                None,
                (
                    str(SK_BATCH_DIR),
                    repo_root,
                    env.get("PYTHONPATH", ""),
                ),
            )
        )
        with spm_audit.spm_exclusive_lock(self.spm):
            child = subprocess.Popen(
                [sys.executable, "-c", code, str(self.spm), str(acquired)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
            )
            time.sleep(0.25)
            self.assertIsNone(child.poll())
            self.assertFalse(acquired.exists())
        stdout, stderr = child.communicate(timeout=10)
        self.assertEqual(child.returncode, 0, (stdout, stderr))
        self.assertEqual(acquired.read_text(encoding="utf-8"), "acquired")

    def test_successful_run_leaves_no_marker_behind(self):
        cfg = {"backup_spm": True, "rename_materials": False}

        def fake_calibrate(spm_path, cfg, log=print, source_text=None, source_audit=None):
            marker = spm_audit.calibration_marker_path(spm_path)
            # The marker must already exist while the SPM is being rewritten.
            self.assertTrue(marker.is_file())
            spm_audit.write_spm(spm_path, PROBE_XML)
            spm_audit.write_spm(spm_path, source_text)
            return {}, [], 0, {"mode": "stub"}, [], [], False

        with mock.patch.object(spm_audit, "calibrate_bones", side_effect=fake_calibrate):
            with mock.patch.object(
                spm_audit, "audit_spm", return_value={"generators": []}
            ):
                with mock.patch.object(
                    spm_audit,
                    "sk_readiness",
                    return_value={"ready": True, "mode": "ok", "disabled_generators": []},
                ):
                    with mock.patch.object(
                        spm_audit, "plan_material_renames", return_value=([], [])
                    ):
                        with mock.patch.object(
                            spm_audit, "classify_asset_kind", return_value="plant"
                        ):
                            spm_audit.process_spm(
                                self.spm, cfg, log=lambda _m: None
                            )

        self.assertFalse(spm_audit.calibration_marker_path(self.spm).exists())
        self.assertEqual(self.spm.read_bytes(), self.source_bytes)

    def test_successful_run_surfaces_marker_clear_lock(self):
        cfg = {"backup_spm": True, "rename_materials": False}

        def fake_calibrate(
            spm_path,
            cfg,
            log=print,
            source_text=None,
            source_audit=None,
        ):
            spm_audit.write_spm(spm_path, PROBE_XML)
            spm_audit.write_spm(spm_path, source_text)
            return {}, [], 0, {"mode": "stub"}, [], [], False

        original_unlink = spm_audit._unlink_with_backoff

        def fail_marker_clear(path, *, operation, missing_ok=True):
            if operation == "clear_calibration_marker":
                raise spm_audit.SPMAtomicOperationError(
                    spm_audit._operation_diagnostic(
                        "process_file_lock",
                        operation=operation,
                        target=path,
                        attempts=3,
                        error=PermissionError(13, "marker locked"),
                    )
                )
            return original_unlink(
                path,
                operation=operation,
                missing_ok=missing_ok,
            )

        with mock.patch.object(
            spm_audit,
            "calibrate_bones",
            side_effect=fake_calibrate,
        ), mock.patch.object(
            spm_audit, "audit_spm", return_value={"generators": []}
        ), mock.patch.object(
            spm_audit,
            "sk_readiness",
            return_value={
                "ready": True,
                "mode": "ok",
                "disabled_generators": [],
            },
        ), mock.patch.object(
            spm_audit, "plan_material_renames", return_value=([], [])
        ), mock.patch.object(
            spm_audit, "classify_asset_kind", return_value="plant"
        ), mock.patch.object(
            spm_audit,
            "_unlink_with_backoff",
            side_effect=fail_marker_clear,
        ):
            with self.assertRaises(
                spm_audit.SPMAtomicOperationError
            ) as caught:
                spm_audit.process_spm(
                    self.spm,
                    cfg,
                    log=lambda _m: None,
                )

        self.assertEqual(
            caught.exception.diagnostic["failure_kind"],
            "process_file_lock",
        )
        self.assertTrue(spm_audit.calibration_marker_path(self.spm).exists())
        self.assertEqual(self.spm.read_bytes(), self.source_bytes)

    def test_invalid_active_marker_update_is_not_ignored(self):
        marker = spm_audit.calibration_marker_path(self.spm)
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_bytes(b"{")

        with self.assertRaises(
            spm_audit.SPMAtomicOperationError
        ) as caught:
            spm_audit._update_calibration_marker_last_pipeline_sha(
                self.spm,
                self.source_sha,
            )

        self.assertEqual(
            caught.exception.diagnostic["category"],
            "concurrent_spm_modification",
        )

    def test_failing_run_still_clears_the_marker_after_restoring(self):
        cfg = {"backup_spm": True, "rename_materials": False}
        original_mtime_ns = self.spm.stat().st_mtime_ns

        def fail_after_write(spm_path, *_args, **_kwargs):
            spm_audit.write_spm(spm_path, PROBE_XML)
            raise RuntimeError("boom")

        with mock.patch.object(
            spm_audit, "calibrate_bones", side_effect=fail_after_write
        ):
            with mock.patch.object(
                spm_audit, "audit_spm", return_value={"generators": []}
            ):
                with mock.patch.object(
                    spm_audit,
                    "sk_readiness",
                    return_value={"ready": True, "mode": "ok", "disabled_generators": []},
                ):
                    with mock.patch.object(
                        spm_audit, "plan_material_renames", return_value=([], [])
                    ):
                        with mock.patch.object(
                            spm_audit, "classify_asset_kind", return_value="plant"
                        ):
                            with self.assertRaises(RuntimeError):
                                spm_audit.process_spm(
                                    self.spm, cfg, log=lambda _m: None
                                )

        self.assertFalse(spm_audit.calibration_marker_path(self.spm).exists())
        self.assertEqual(self.spm.read_bytes(), self.source_bytes)
        self.assertEqual(self.spm.stat().st_mtime_ns, original_mtime_ns)

    def test_failing_run_without_backup_restores_bytes_and_timestamp(self):
        cfg = {"backup_spm": False, "rename_materials": False}
        original_mtime_ns = self.spm.stat().st_mtime_ns

        def fail_after_write(spm_path, *_args, **_kwargs):
            spm_audit.write_spm(spm_path, PROBE_XML)
            raise RuntimeError("boom")

        with mock.patch.object(
            spm_audit, "calibrate_bones", side_effect=fail_after_write
        ), mock.patch.object(
            spm_audit, "audit_spm", return_value={"generators": []}
        ), mock.patch.object(
            spm_audit,
            "sk_readiness",
            return_value={
                "ready": True,
                "mode": "ok",
                "disabled_generators": [],
            },
        ), mock.patch.object(
            spm_audit, "plan_material_renames", return_value=([], [])
        ), mock.patch.object(
            spm_audit, "classify_asset_kind", return_value="plant"
        ):
            with self.assertRaisesRegex(RuntimeError, "boom"):
                spm_audit.process_spm(
                    self.spm, cfg, log=lambda _m: None
                )

        self.assertFalse(spm_audit.calibration_marker_path(self.spm).exists())
        self.assertEqual(self.spm.read_bytes(), self.source_bytes)
        self.assertEqual(self.spm.stat().st_mtime_ns, original_mtime_ns)

    def test_rollback_refuses_external_edit_and_keeps_marker(self):
        cfg = {"backup_spm": False, "rename_materials": False}
        external_bytes = b"external SpeedTree edit during calibration"

        def fail_after_external_write(spm_path, *_args, **_kwargs):
            spm_audit.write_spm(spm_path, PROBE_XML)
            Path(spm_path).write_bytes(external_bytes)
            raise RuntimeError("pipeline failure")

        with mock.patch.object(
            spm_audit,
            "calibrate_bones",
            side_effect=fail_after_external_write,
        ), mock.patch.object(
            spm_audit, "audit_spm", return_value={"generators": []}
        ), mock.patch.object(
            spm_audit,
            "sk_readiness",
            return_value={
                "ready": True,
                "mode": "ok",
                "disabled_generators": [],
            },
        ), mock.patch.object(
            spm_audit, "plan_material_renames", return_value=([], [])
        ), mock.patch.object(
            spm_audit, "classify_asset_kind", return_value="plant"
        ):
            with self.assertRaises(
                spm_audit.SPMAtomicOperationError
            ) as caught:
                spm_audit.process_spm(
                    self.spm,
                    cfg,
                    log=lambda _m: None,
                )

        self.assertEqual(
            caught.exception.diagnostic["category"],
            "concurrent_spm_modification",
        )
        self.assertEqual(self.spm.read_bytes(), external_bytes)
        self.assertTrue(spm_audit.calibration_marker_path(self.spm).exists())

    def test_write_spm_retries_a_bounded_sharing_violation(self):
        original_replace = spm_audit._replace_file_with_rescue
        calls = {"count": 0}

        def fail_once(temporary, target, **kwargs):
            calls["count"] += 1
            if calls["count"] == 1:
                raise PermissionError(13, "sharing violation")
            return original_replace(temporary, target, **kwargs)

        with mock.patch.object(
            spm_audit,
            "_replace_file_with_rescue",
            side_effect=fail_once,
        ), mock.patch.object(spm_audit.time, "sleep"):
            spm_audit.write_spm(self.spm, PROBE_XML)

        self.assertEqual(calls["count"], 2)
        self.assertEqual(spm_audit.read_spm(self.spm), PROBE_XML)

    def test_write_spm_reports_process_file_lock_after_bound(self):
        with mock.patch.object(
            spm_audit,
            "SPM_REPLACE_MAX_ATTEMPTS",
            3,
        ), mock.patch.object(
            spm_audit,
            "_replace_file_with_rescue",
            side_effect=PermissionError(13, "sharing violation"),
        ), mock.patch.object(spm_audit.time, "sleep"):
            with self.assertRaises(
                spm_audit.SPMAtomicOperationError
            ) as caught:
                spm_audit.write_spm(self.spm, PROBE_XML)

        self.assertEqual(
            caught.exception.diagnostic["category"],
            "process_file_lock",
        )
        self.assertEqual(
            caught.exception.diagnostic["failure_kind"],
            "process_file_lock",
        )
        self.assertEqual(caught.exception.diagnostic["attempts"], 3)
        self.assertEqual(spm_audit.read_spm(self.spm), SOURCE_XML)

    @unittest.skipUnless(os.name == "nt", "ReplaceFileW rescue is Windows-only")
    def test_replace_file_retries_transient_scanner_lock(self):
        """WinError 1175 is a held handle, not a verdict.

        CI failed three runs on 2026-08-02 -- two of them merge commits on
        main -- inside this swap, with ERROR_UNABLE_TO_REMOVE_REPLACED. Every
        neighbouring file operation already backs off on a transient lock.
        """
        temporary = self.root / "replacement.bin"
        temporary.write_bytes(b"pipeline replacement")
        self.spm.write_bytes(b"live target")
        attempts = {"count": 0}

        def flaky_replace(target, replacement, backup, flags, a, b):
            attempts["count"] += 1
            if attempts["count"] < 3:
                ctypes.set_last_error(1175)
                return False
            os.replace(replacement, target)
            Path(backup).write_bytes(b"displaced")
            return True

        with self._patched_replace_file(flaky_replace), mock.patch.object(
            spm_audit, "_sleep_file_backoff"
        ) as slept:
            rescue = spm_audit._replace_file_with_rescue(
                temporary,
                self.spm,
                require_existing=True,
            )

        self.assertEqual(attempts["count"], 3)
        self.assertEqual(slept.call_count, 2)
        self.assertEqual(self.spm.read_bytes(), b"pipeline replacement")
        self.assertEqual(Path(rescue).read_bytes(), b"displaced")

    @unittest.skipUnless(os.name == "nt", "ReplaceFileW rescue is Windows-only")
    def test_replace_file_does_not_retry_a_non_transient_error(self):
        temporary = self.root / "replacement.bin"
        temporary.write_bytes(b"pipeline replacement")
        self.spm.write_bytes(b"live target")
        attempts = {"count": 0}

        def always_denied(target, replacement, backup, flags, a, b):
            attempts["count"] += 1
            # ERROR_FILE_NOT_FOUND: nothing about it improves by waiting.
            ctypes.set_last_error(2)
            return False

        with self._patched_replace_file(always_denied), mock.patch.object(
            spm_audit, "_sleep_file_backoff"
        ) as slept:
            with self.assertRaises(OSError) as caught:
                spm_audit._replace_file_with_rescue(
                    temporary,
                    self.spm,
                    require_existing=True,
                )

        self.assertEqual(caught.exception.winerror, 2)
        self.assertEqual(attempts["count"], 1)
        slept.assert_not_called()
        self.assertEqual(self.spm.read_bytes(), b"live target")

    @unittest.skipUnless(os.name == "nt", "ReplaceFileW rescue is Windows-only")
    def test_replace_file_gives_up_after_the_shared_attempt_budget(self):
        temporary = self.root / "replacement.bin"
        temporary.write_bytes(b"pipeline replacement")
        self.spm.write_bytes(b"live target")
        attempts = {"count": 0}

        def always_locked(target, replacement, backup, flags, a, b):
            attempts["count"] += 1
            ctypes.set_last_error(1175)
            return False

        with self._patched_replace_file(always_locked), mock.patch.object(
            spm_audit, "_sleep_file_backoff"
        ):
            with self.assertRaises(OSError) as caught:
                spm_audit._replace_file_with_rescue(
                    temporary,
                    self.spm,
                    require_existing=True,
                )

        self.assertEqual(caught.exception.winerror, 1175)
        self.assertEqual(
            attempts["count"], spm_audit.SPM_REPLACE_MAX_ATTEMPTS
        )
        self.assertEqual(self.spm.read_bytes(), b"live target")

    def _patched_replace_file(self, implementation):
        """Swap ReplaceFileW for a callable without touching the real DLL."""
        real_windll = spm_audit.ctypes.WinDLL

        class _Kernel32:
            pass

        def fake_windll(name, *args, **kwargs):
            if name == "kernel32":
                kernel32 = _Kernel32()
                kernel32.ReplaceFileW = implementation
                return kernel32
            return real_windll(name, *args, **kwargs)

        return mock.patch.object(
            spm_audit.ctypes, "WinDLL", side_effect=fake_windll
        )

    @unittest.skipUnless(os.name == "nt", "ReplaceFileW rescue is Windows-only")
    def test_atomic_swap_restores_external_writer_that_wins_first_race(self):
        external_bytes = b"external writer one"
        desired_bytes = b"pipeline replacement"
        original_swap = spm_audit._replace_file_with_rescue
        calls = {"count": 0}

        def inject_external_before_swap(temporary, target, **kwargs):
            calls["count"] += 1
            if calls["count"] == 1:
                Path(target).write_bytes(external_bytes)
            return original_swap(temporary, target, **kwargs)

        token = spm_audit._SPM_WRITE_TRANSACTION.set({})
        try:
            spm_audit._seed_spm_transaction(self.spm, self.source_bytes)
            with mock.patch.object(
                spm_audit,
                "_replace_file_with_rescue",
                side_effect=inject_external_before_swap,
            ):
                with self.assertRaises(
                    spm_audit.SPMAtomicOperationError
                ) as caught:
                    spm_audit._atomic_replace_payload(
                        self.spm,
                        desired_bytes,
                        operation="test_first_race",
                    )
        finally:
            spm_audit._SPM_WRITE_TRANSACTION.reset(token)

        self.assertEqual(
            caught.exception.diagnostic["category"],
            "concurrent_spm_modification",
        )
        self.assertEqual(
            caught.exception.diagnostic["failure_kind"],
            "concurrent_spm_modification",
        )
        self.assertEqual(self.spm.read_bytes(), external_bytes)

    @unittest.skipUnless(os.name == "nt", "ReplaceFileW rescue is Windows-only")
    def test_second_race_keeps_newest_live_and_isolates_first_artifact(self):
        external_one = b"external writer one"
        external_two = b"external writer two"
        desired_bytes = b"pipeline replacement"
        original_swap = spm_audit._replace_file_with_rescue
        original_restore = spm_audit._restore_conflict_rescue
        swaps = {"count": 0}

        def inject_first_external(temporary, target, **kwargs):
            swaps["count"] += 1
            if swaps["count"] == 1:
                Path(target).write_bytes(external_one)
            return original_swap(temporary, target, **kwargs)

        def inject_second_external(rescue, target, **kwargs):
            Path(target).write_bytes(external_two)
            return original_restore(rescue, target, **kwargs)

        token = spm_audit._SPM_WRITE_TRANSACTION.set({})
        try:
            spm_audit._seed_spm_transaction(self.spm, self.source_bytes)
            with mock.patch.object(
                spm_audit,
                "_replace_file_with_rescue",
                side_effect=inject_first_external,
            ), mock.patch.object(
                spm_audit,
                "_restore_conflict_rescue",
                side_effect=inject_second_external,
            ):
                with self.assertRaises(
                    spm_audit.SPMAtomicOperationError
                ) as caught:
                    spm_audit._atomic_replace_payload(
                        self.spm,
                        desired_bytes,
                        operation="test_second_race",
                    )
        finally:
            spm_audit._SPM_WRITE_TRANSACTION.reset(token)

        artifact = Path(
            caught.exception.diagnostic["conflict_artifact"]
        )
        self.assertEqual(self.spm.read_bytes(), external_two)
        self.assertTrue(artifact.is_file())
        self.assertEqual(artifact.read_bytes(), external_one)
        self.assertEqual(artifact.parent.name, "conflicts")
        self.assertEqual(artifact.parent.parent.name, spm_audit.BACKUP_SUBDIR)
        from speedtree_pipeline_contract import is_live_spm

        self.assertFalse(is_live_spm(artifact))

    @unittest.skipUnless(os.name == "nt", "ReplaceFileW rescue is Windows-only")
    def test_existing_target_never_falls_back_to_unconditional_replace(self):
        external_bytes = b"external recreate after delete"
        original_swap = spm_audit._replace_file_with_rescue
        calls = {"count": 0}

        def delete_then_recreate(temporary, target, **kwargs):
            calls["count"] += 1
            if calls["count"] == 1:
                Path(target).unlink()
                try:
                    return original_swap(temporary, target, **kwargs)
                except FileNotFoundError:
                    Path(target).write_bytes(external_bytes)
                    raise
            return original_swap(temporary, target, **kwargs)

        token = spm_audit._SPM_WRITE_TRANSACTION.set({})
        try:
            spm_audit._seed_spm_transaction(self.spm, self.source_bytes)
            with mock.patch.object(
                spm_audit,
                "_replace_file_with_rescue",
                side_effect=delete_then_recreate,
            ):
                with self.assertRaises(
                    spm_audit.SPMAtomicOperationError
                ) as caught:
                    spm_audit._atomic_replace_payload(
                        self.spm,
                        b"pipeline replacement",
                        operation="test_delete_recreate_race",
                    )
        finally:
            spm_audit._SPM_WRITE_TRANSACTION.reset(token)

        self.assertEqual(
            caught.exception.diagnostic["category"],
            "concurrent_spm_modification",
        )
        self.assertEqual(self.spm.read_bytes(), external_bytes)


if __name__ == "__main__":
    unittest.main()
