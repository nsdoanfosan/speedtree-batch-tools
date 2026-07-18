import json
import os
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
    attach_process_kill_job,
    calibration_cache_matches,
    calibration_settings_signature,
    cached_file_content_snapshot,
    cached_push_source_fingerprint,
    close_process_kill_job,
    file_content_snapshot,
    legacy_calibration_settings_signature,
    manifest_item_files_match,
    scan_sk_spms,
    terminate_process_tree,
)


class SkCommonOptimizationTests(unittest.TestCase):
    def test_job_assignment_failure_keeps_tree_cleanup_fallback_available(self):
        proc = mock.Mock()
        with mock.patch(
            "sk_common._create_kill_on_close_job",
            side_effect=OSError("job assignment denied"),
        ):
            self.assertFalse(attach_process_kill_job(proc))

        self.assertIsNone(proc.sk_job_handle)

    def test_job_handle_is_closed_at_most_once(self):
        proc = mock.Mock()
        proc.sk_job_handle = 123
        with mock.patch("sk_common.os.name", "nt"), mock.patch(
            "sk_common._close_windows_handle", return_value=True
        ) as close_handle:
            self.assertTrue(close_process_kill_job(proc))
            self.assertFalse(close_process_kill_job(proc))

        close_handle.assert_called_once_with(123)

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
            pcg = root / "tree" / "_pcgtex_backups" / "SK_pcg_old.spm"
            live.parent.mkdir(parents=True)
            backup.parent.mkdir(parents=True)
            legacy.parent.mkdir(parents=True)
            pcg.parent.mkdir(parents=True)
            live.write_bytes(b"live")
            backup.write_bytes(b"backup")
            legacy.write_bytes(b"legacy")
            pcg.write_bytes(b"pcg backup")

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
        self.assertTrue(
            calibration_cache_matches(
                cache,
                "file-a",
                "settings-b",
                legacy_settings_signature="settings-a",
            )
        )

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

    def test_settings_signature_ignores_touch_for_content_hashed_dependencies(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            xml_ini = root / "Options.xml.ini"
            fbx_ini = root / "Options.fbx.ini"
            exe = root / "SpeedTree.exe"
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

            content_signature = calibration_settings_signature(cfg)
            legacy_signature = legacy_calibration_settings_signature(cfg)
            for path in (xml_ini, fbx_ini):
                stat = path.stat()
                os.utime(
                    path,
                    ns=(stat.st_atime_ns + 1_000_000, stat.st_mtime_ns + 1_000_000),
                )

            self.assertEqual(calibration_settings_signature(cfg), content_signature)
            self.assertNotEqual(
                legacy_calibration_settings_signature(cfg), legacy_signature
            )

            fbx_ini.write_text("fbx-b", encoding="utf-8")
            self.assertNotEqual(calibration_settings_signature(cfg), content_signature)

    def test_push_source_fingerprint_reuses_unchanged_stat_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            blend = root / "large.blend"
            dependency = root / "pipeline.py"
            blend.write_bytes(b"blend-content")
            dependency.write_text("version = 1", encoding="utf-8")

            first, cache, hit = cached_push_source_fingerprint(
                blend, [dependency]
            )
            second, second_cache, second_hit = cached_push_source_fingerprint(
                blend, [dependency], cache=cache
            )

        self.assertFalse(hit)
        self.assertTrue(second_hit)
        self.assertEqual(first, second)
        self.assertEqual(cache, second_cache)

    def test_push_source_fingerprint_ignores_touch_only_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            blend = root / "large.blend"
            dependency = root / "pipeline.py"
            blend.write_bytes(b"blend-content")
            dependency.write_text("version = 1", encoding="utf-8")

            first, cache, _hit = cached_push_source_fingerprint(
                blend, [dependency]
            )
            for path in (blend, dependency):
                stat = path.stat()
                os.utime(
                    path,
                    ns=(
                        stat.st_atime_ns + 1_000_000,
                        stat.st_mtime_ns + 1_000_000,
                    ),
                )
            second, refreshed_cache, second_hit = cached_push_source_fingerprint(
                blend, [dependency], cache=cache
            )

            dependency.write_text("version = 2", encoding="utf-8")
            changed, _changed_cache, _changed_hit = cached_push_source_fingerprint(
                blend, [dependency], cache=refreshed_cache
            )

        self.assertFalse(second_hit)
        self.assertEqual(first, second)
        self.assertNotEqual(second, changed)

    def test_file_snapshot_reuses_unchanged_stat_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            spm = Path(tmp) / "SK_tree.spm"
            spm.write_bytes(b"speedtree")
            first = file_content_snapshot(spm)
            with mock.patch(
                "sk_common.file_content_snapshot",
                side_effect=AssertionError("unchanged SPM was rehashed"),
            ):
                second, cache_hit = cached_file_content_snapshot(spm, first)

        self.assertTrue(cache_hit)
        self.assertEqual(first, second)

    def test_manifest_stat_match_skips_rehash(self):
        with tempfile.TemporaryDirectory() as tmp:
            exported = Path(tmp) / "mesh.fbx"
            exported.write_bytes(b"fbx")
            stat = exported.stat()
            item = {
                "fingerprint": "manifest",
                "exported_files": [{
                    "path": str(exported),
                    "size": stat.st_size,
                    "mtime_ns": stat.st_mtime_ns,
                    "fingerprint": "not-needed",
                }],
            }
            with mock.patch(
                "sk_common.file_content_fingerprint",
                side_effect=AssertionError("unchanged export was rehashed"),
            ):
                self.assertTrue(manifest_item_files_match(item))


if __name__ == "__main__":
    unittest.main()
