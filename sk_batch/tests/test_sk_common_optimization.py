import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SK_BATCH_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SK_BATCH_DIR))

from sk_common import (
    CALIBRATION_CACHE_VERSION,
    calibration_cache_matches,
    calibration_settings_signature,
    file_content_snapshot,
    scan_sk_spms,
    terminate_process_tree,
)


class SkCommonOptimizationTests(unittest.TestCase):
    def test_windows_tree_termination_uses_taskkill_for_descendants(self):
        proc = mock.Mock()
        proc.pid = 1234
        proc.poll.side_effect = [None, None, 1]
        proc.wait.return_value = 1

        with mock.patch("sk_common.os.name", "nt"):
            with mock.patch("sk_common.subprocess.run") as run_mock:
                run_mock.return_value.returncode = 0
                self.assertTrue(terminate_process_tree(proc, wait_seconds=0.1))

        self.assertEqual(
            run_mock.call_args.args[0],
            ["taskkill", "/PID", "1234", "/T", "/F"],
        )
        proc.kill.assert_not_called()

    def test_tree_termination_falls_back_to_parent_kill_when_taskkill_fails(self):
        proc = mock.Mock()
        proc.pid = 5678
        proc.poll.side_effect = [None, None, None, 1]
        proc.wait.side_effect = [subprocess.TimeoutExpired("wait", 0.1), 1]

        with mock.patch("sk_common.os.name", "nt"):
            with mock.patch("sk_common.subprocess.run") as run_mock:
                run_mock.return_value.returncode = 1
                self.assertFalse(terminate_process_tree(proc, wait_seconds=0.1))

        proc.kill.assert_called_once_with()

    def test_scan_prunes_backup_directories_without_losing_live_spms(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            live = root / "tree" / "SK_live.spm"
            backup = root / "tree" / "_spm_backups" / "SK_live.spm"
            legacy = root / "tree" / "_skbatch_backup" / "SK_old.spm"
            live.parent.mkdir(parents=True)
            backup.parent.mkdir(parents=True)
            legacy.parent.mkdir(parents=True)
            live.write_bytes(b"live")
            backup.write_bytes(b"backup")
            legacy.write_bytes(b"legacy")

            self.assertEqual(scan_sk_spms(root), [live])

    def test_content_cache_requires_same_file_and_settings_signature(self):
        cache = {
            "version": CALIBRATION_CACHE_VERSION,
            "spm_fingerprint": "file-a",
            "settings_signature": "settings-a",
        }
        self.assertTrue(calibration_cache_matches(cache, "file-a", "settings-a"))
        self.assertFalse(calibration_cache_matches(cache, "file-b", "settings-a"))
        self.assertFalse(calibration_cache_matches(cache, "file-a", "settings-b"))

    def test_snapshot_and_settings_signature_invalidate_on_real_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            spm = root / "SK_test.spm"
            xml_ini = root / "Options.xml.ini"
            fbx_ini = root / "Options.fbx.ini"
            exe = root / "SpeedTree.exe"
            spm.write_bytes(b"one")
            xml_ini.write_text("xml-a", encoding="utf-8")
            fbx_ini.write_text("fbx-a", encoding="utf-8")
            exe.write_bytes(b"exe")
            cfg = {
                "target_bones_per_branch": 3.0,
                "max_total_bones": 2000,
                "probe_cache_enabled": True,
                "xml_ini": str(xml_ini),
                "fbx_ini": str(fbx_ini),
                "speedtree_exe": str(exe),
            }

            first_snapshot = file_content_snapshot(spm)
            first_signature = calibration_settings_signature(cfg)
            spm.write_bytes(b"two")
            cfg["target_bones_per_branch"] = 4.0
            second_snapshot = file_content_snapshot(spm)
            second_signature = calibration_settings_signature(cfg)

            self.assertNotEqual(first_snapshot["fingerprint"], second_snapshot["fingerprint"])
            self.assertNotEqual(first_signature, second_signature)


if __name__ == "__main__":
    unittest.main()
