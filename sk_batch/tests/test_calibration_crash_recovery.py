"""A killed ① run must not leave probe bones in the source SPM.

Calibration rewrites the SPM in place and restores it on every exception path,
but Stop and the watchdog timeout both use ``taskkill /T /F`` and run no
handler at all.  The in-progress marker makes that state detectable, and the
next run repairs it from the recorded backup before reading the source.
"""
import hashlib
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

    def test_failing_run_still_clears_the_marker_after_restoring(self):
        cfg = {"backup_spm": True, "rename_materials": False}

        with mock.patch.object(
            spm_audit, "calibrate_bones", side_effect=RuntimeError("boom")
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


if __name__ == "__main__":
    unittest.main()
