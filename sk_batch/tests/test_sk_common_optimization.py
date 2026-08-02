import json
import multiprocessing
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock


SK_BATCH_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SK_BATCH_DIR))

import sk_common
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


def _configure_state_process(state_path, recovery_log):
    sk_common.STATE_PATH = Path(state_path)
    sk_common.STATE_RECOVERY_LOG_PATH = Path(recovery_log)


def _save_state_process(state_path, recovery_log, state, started, output):
    _configure_state_process(state_path, recovery_log)
    started.set()
    try:
        sk_common.save_state(state)
        output.put({"ok": True})
    except BaseException as exc:
        output.put({"ok": False, "error": repr(exc)})


def _load_state_pause_after_failed_read(
    state_path,
    recovery_log,
    failed_read,
    resume,
    output,
):
    _configure_state_process(state_path, recovery_log)
    original_loads = sk_common.json.loads

    def controlled_loads(value):
        try:
            return original_loads(value)
        except (UnicodeError, json.JSONDecodeError):
            failed_read.set()
            resume.wait(10)
            raise

    sk_common.json.loads = controlled_loads
    try:
        output.put({"ok": True, "state": sk_common.load_state()})
    except BaseException as exc:
        output.put({"ok": False, "error": repr(exc)})


def _load_state_pause_after_prune(
    state_path,
    recovery_log,
    pruned,
    resume,
    output,
):
    _configure_state_process(state_path, recovery_log)
    original_prune = sk_common._prune_state_entries

    def controlled_prune(state):
        result = original_prune(state)
        pruned.set()
        resume.wait(10)
        return result

    sk_common._prune_state_entries = controlled_prune
    try:
        output.put({"ok": True, "state": sk_common.load_state()})
    except BaseException as exc:
        output.put({"ok": False, "error": repr(exc)})


class SkCommonOptimizationTests(unittest.TestCase):
    def test_job_attachment_shim_only_accepts_preassigned_shared_job(self):
        proc = mock.Mock()
        proc.speedtree_lifecycle_tree_job = None
        self.assertFalse(attach_process_kill_job(proc))
        self.assertIsNone(proc.sk_job_handle)
        proc.speedtree_lifecycle_tree_job = shared_job = object()
        self.assertTrue(attach_process_kill_job(proc))
        self.assertIs(proc.sk_job_handle, shared_job)

    def test_job_finalization_routes_through_shared_receipt_contract(self):
        proc = mock.Mock()
        proc.speedtree_lifecycle_launch_id = "launch-1"
        with mock.patch(
            "sk_common.complete_owned_process", return_value="process_tree_clean"
        ) as complete:
            self.assertTrue(close_process_kill_job(proc))
        complete.assert_called_once_with(proc, reason="sk_worker_complete")
        self.assertIsNone(proc.sk_job_handle)

    def test_tree_termination_uses_registered_exact_owned_tree(self):
        proc = mock.Mock()
        proc.pid = 1234
        proc.speedtree_lifecycle_launch_id = "launch-1"
        proc.poll.return_value = 1
        with mock.patch(
            "sk_common.terminate_owned_process", return_value="sk_stop_forced"
        ) as terminate, mock.patch("sk_common.subprocess.run") as run_mock:
            self.assertTrue(terminate_process_tree(proc, wait_seconds=0.1))
        terminate.assert_called_once_with(
            proc,
            reason="sk_stop",
            terminate_grace=0.1,
            kill_grace=0.1,
        )
        run_mock.assert_not_called()

    def test_unregistered_fallback_signals_only_retained_parent_handle(self):
        proc = mock.Mock()
        proc.pid = 5678
        proc.speedtree_lifecycle_launch_id = None
        proc.poll.side_effect = [None, None]
        proc.wait.side_effect = [subprocess.TimeoutExpired("wait", 0.1), 1]

        with mock.patch("sk_common.subprocess.run") as run_mock:
            self.assertFalse(terminate_process_tree(proc, wait_seconds=0.1))
        run_mock.assert_not_called()
        proc.terminate.assert_called_once_with()
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

    def test_unreadable_state_is_quarantined_and_reported_without_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_path = root / "sk_batch_state.json"
            recovery_log = root / "logs" / "state_recovery.log"
            original = b'{"broken": "\xff"'
            state_path.write_bytes(original)

            with mock.patch.object(
                sk_common, "STATE_PATH", state_path
            ), mock.patch.object(
                sk_common, "STATE_RECOVERY_LOG_PATH", recovery_log
            ), self.assertWarns(RuntimeWarning):
                self.assertEqual(sk_common.load_state(), {})

            self.assertFalse(state_path.exists())
            quarantined = list(root.glob("sk_batch_state.unreadable-*.json"))
            self.assertEqual(len(quarantined), 1)
            self.assertEqual(quarantined[0].read_bytes(), original)
            notice = recovery_log.read_text(encoding="utf-8")
            self.assertIn("state_unreadable_quarantined", notice)
            self.assertIn(quarantined[0].name, notice)
            self.assertNotIn(str(root), notice)

    def test_unreadable_state_is_not_emptied_if_quarantine_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "sk_batch_state.json"
            original = b'{"incomplete":'
            state_path.write_bytes(original)

            with mock.patch.object(
                sk_common, "STATE_PATH", state_path
            ), mock.patch.object(
                sk_common.os,
                "replace",
                side_effect=PermissionError("injected"),
            ):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "state_quarantine_failed.*could not be quarantined",
                ):
                    sk_common.load_state()

            self.assertEqual(state_path.read_bytes(), original)

    def test_quarantine_does_not_move_concurrent_valid_replace(self):
        context = multiprocessing.get_context("spawn")
        for iteration in range(3):
            with self.subTest(iteration=iteration), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                state_path = root / "sk_batch_state.json"
                recovery_log = root / "logs" / "state_recovery.log"
                invalid = b'{"broken":'
                state_path.write_bytes(invalid)
                live = root / "tree" / f"SK_new_{iteration}.spm"
                live.parent.mkdir(parents=True)
                live.write_bytes(b"live")
                replacement = {str(live): {"writer": iteration}}

                failed_read = context.Event()
                resume = context.Event()
                loader_output = context.Queue()
                loader = context.Process(
                    target=_load_state_pause_after_failed_read,
                    args=(
                        str(state_path),
                        str(recovery_log),
                        failed_read,
                        resume,
                        loader_output,
                    ),
                )
                loader.start()
                self.assertTrue(failed_read.wait(5))

                writer_started = context.Event()
                writer_output = context.Queue()
                writer = context.Process(
                    target=_save_state_process,
                    args=(
                        str(state_path),
                        str(recovery_log),
                        replacement,
                        writer_started,
                        writer_output,
                    ),
                )
                writer.start()
                self.assertTrue(writer_started.wait(5))
                time.sleep(0.1)
                resume.set()
                loader.join(10)
                writer.join(10)
                self.assertEqual(loader.exitcode, 0)
                self.assertEqual(writer.exitcode, 0)
                self.assertTrue(loader_output.get(timeout=2)["ok"])
                self.assertTrue(writer_output.get(timeout=2)["ok"])

                persisted = json.loads(state_path.read_text(encoding="utf-8"))
                self.assertEqual(persisted, replacement)
                quarantined = list(
                    root.glob("sk_batch_state.unreadable-*.json")
                )
                self.assertTrue(quarantined)
                self.assertTrue(
                    all(path.read_bytes() == invalid for path in quarantined)
                )

    def test_load_time_prune_cannot_overwrite_concurrent_new_row(self):
        context = multiprocessing.get_context("spawn")
        for iteration in range(3):
            with self.subTest(iteration=iteration), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                state_path = root / "sk_batch_state.json"
                recovery_log = root / "logs" / "state_recovery.log"
                live = root / "tree" / f"SK_live_{iteration}.spm"
                added = root / "tree" / f"SK_added_{iteration}.spm"
                dead = root / "tree" / f"SK_dead_{iteration}.spm"
                live.parent.mkdir(parents=True)
                live.write_bytes(b"live")
                added.write_bytes(b"added")
                initial = {
                    str(live): {"source": "initial"},
                    str(dead): {"source": "dead"},
                }
                state_path.write_text(json.dumps(initial), encoding="utf-8")

                pruned = context.Event()
                resume = context.Event()
                loader_output = context.Queue()
                loader = context.Process(
                    target=_load_state_pause_after_prune,
                    args=(
                        str(state_path),
                        str(recovery_log),
                        pruned,
                        resume,
                        loader_output,
                    ),
                )
                loader.start()
                self.assertTrue(pruned.wait(5))

                writer_started = context.Event()
                writer_output = context.Queue()
                writer = context.Process(
                    target=_save_state_process,
                    args=(
                        str(state_path),
                        str(recovery_log),
                        {str(added): {"source": "concurrent"}},
                        writer_started,
                        writer_output,
                    ),
                )
                writer.start()
                self.assertTrue(writer_started.wait(5))
                time.sleep(0.1)
                resume.set()
                loader.join(10)
                writer.join(10)
                self.assertEqual(loader.exitcode, 0)
                self.assertEqual(writer.exitcode, 0)
                self.assertTrue(loader_output.get(timeout=2)["ok"])
                self.assertTrue(writer_output.get(timeout=2)["ok"])

                persisted = json.loads(state_path.read_text(encoding="utf-8"))
                self.assertEqual(
                    set(persisted),
                    {str(live), str(added)},
                )
                self.assertNotIn(str(dead), persisted)

    def test_state_load_and_save_prune_dead_and_backup_spm_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_path = root / "sk_batch_state.json"
            live = root / "tree" / "SK_live.spm"
            dead = root / "tree" / "SK_dead.spm"
            backup = (
                root
                / "tree"
                / "_atlas_cluster_normalization_backups"
                / "SK_backup.spm"
            )
            live.parent.mkdir(parents=True)
            backup.parent.mkdir(parents=True)
            live.write_bytes(b"live")
            backup.write_bytes(b"backup")
            state = {
                str(live): {"spm_status": "ok"},
                str(dead): {"spm_status": "missing"},
                str(backup): {"spm_status": "backup"},
                "_format": {"version": 1},
            }

            with mock.patch.object(sk_common, "STATE_PATH", state_path):
                sk_common.save_state(state)
                stored = json.loads(state_path.read_text(encoding="utf-8"))
                self.assertEqual(set(stored), {str(live), "_format"})

                state_path.write_text(json.dumps(state), encoding="utf-8")
                loaded = sk_common.load_state()
                self.assertEqual(set(loaded), {str(live), "_format"})
                persisted = json.loads(
                    state_path.read_text(encoding="utf-8")
                )
                self.assertEqual(persisted, loaded)

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

    def test_settings_signature_ignores_non_semantic_export_dependencies(self):
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
            for path in (xml_ini, fbx_ini):
                stat = path.stat()
                os.utime(
                    path,
                    ns=(stat.st_atime_ns + 1_000_000, stat.st_mtime_ns + 1_000_000),
                )

            self.assertEqual(calibration_settings_signature(cfg), content_signature)
            fbx_ini.write_text("fbx-b", encoding="utf-8")
            xml_ini.write_text("xml-b", encoding="utf-8")
            exe.write_bytes(b"new-exporter")
            cfg["spm_verify_timeout"] = 999
            cfg["probe_cache_enabled"] = False
            cfg["rename_materials"] = False
            cfg["tree_leaf_parent_red_gradient"] = False
            self.assertEqual(calibration_settings_signature(cfg), content_signature)

            cfg["cluster_root_only_bones"] = False
            self.assertNotEqual(calibration_settings_signature(cfg), content_signature)

    def test_settings_signature_changes_only_on_explicit_contract_version_bump(self):
        cfg = {
            "target_bones_per_branch": 2.0,
            "max_total_bones": 1500,
            "cluster_root_only_bones": True,
        }
        current = calibration_settings_signature(cfg)
        with mock.patch.object(
            sk_common,
            "SPM_BONE_CONTRACT_VERSION",
            sk_common.SPM_BONE_CONTRACT_VERSION + 1,
        ):
            bumped = calibration_settings_signature(cfg)
        self.assertNotEqual(current, bumped)

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
