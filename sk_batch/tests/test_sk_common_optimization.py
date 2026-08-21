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
    attach_process_kill_job,
    cached_file_content_snapshot,
    cached_push_source_fingerprint,
    close_process_kill_job,
    file_content_snapshot,
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
    def test_affinity_reservations_spread_two_eight_core_jobs(self):
        first = mock.Mock()
        second = mock.Mock()
        first.poll.return_value = None
        second.poll.return_value = None
        with sk_common._AFFINITY_RESERVATION_LOCK:
            sk_common._AFFINITY_RESERVATIONS.clear()
            sk_common._AFFINITY_CANDIDATE_CURSOR = 0

        first_mask = sk_common._reserve_process_affinity(first, 8, total=16)
        second_mask = sk_common._reserve_process_affinity(second, 8, total=16)

        self.assertEqual(first_mask.bit_count(), 8)
        self.assertEqual(second_mask.bit_count(), 8)
        self.assertEqual(first_mask & second_mask, 0)
        self.assertEqual(first_mask | second_mask, (1 << 16) - 1)

    def test_affinity_reservation_reuses_finished_job_slice(self):
        first = mock.Mock()
        second = mock.Mock()
        first.poll.return_value = None
        second.poll.return_value = None
        with sk_common._AFFINITY_RESERVATION_LOCK:
            sk_common._AFFINITY_RESERVATIONS.clear()
            sk_common._AFFINITY_CANDIDATE_CURSOR = 0

        first_mask = sk_common._reserve_process_affinity(first, 8, total=16)
        first.poll.return_value = 0
        second_mask = sk_common._reserve_process_affinity(second, 8, total=16)

        self.assertEqual(second_mask.bit_count(), 8)
        self.assertIn(second_mask, sk_common._affinity_candidate_masks(16, 8))
        self.assertEqual(len(sk_common._AFFINITY_RESERVATIONS), 1)
        self.assertNotEqual(first_mask, 0)

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

    def test_state_load_and_save_drop_nested_artifact_report_copies(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_path = root / "sk_batch_state.json"
            live = root / "SK_live.spm"
            live.write_bytes(b"live")
            state = {
                str(live): {
                    "blend_status_error": {
                        "message": "assembly failed",
                        "failure_report": {
                            "status": "failed",
                            "error": "assembly failed",
                            "reason_token": "assembly_failed",
                            "cluster_assembly_handoff": {
                                "assembly": {"huge": [1, 2, 3]},
                            },
                            "cluster_assembly_manifest": {
                                "bindings": [1, 2, 3],
                            },
                        },
                    },
                },
            }

            with mock.patch.object(sk_common, "STATE_PATH", state_path):
                sk_common.save_state(state)
                stored = json.loads(state_path.read_text(encoding="utf-8"))
                failure = stored[str(live)]["blend_status_error"][
                    "failure_report"
                ]
                self.assertEqual(failure, {
                    "status": "failed",
                    "error": "assembly failed",
                    "reason_token": "assembly_failed",
                })

                loaded = sk_common.load_state()
                self.assertEqual(
                    loaded[str(live)]["blend_status_error"]["failure_report"],
                    failure,
                )

    def test_state_save_publishes_only_pending_unreal_references(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_path = root / "sk_batch_state.json"
            pending = root / "SK_pending.spm"
            completed = root / "SK_completed.spm"
            pending.write_bytes(b"pending")
            completed.write_bytes(b"completed")
            waiting_manifest = root / "logs" / "unreal_wait.json"
            completed_manifest = root / "logs" / "headless_done.json"
            state = {
                str(pending): {
                    "push_status_kind": "exported_pending_unreal",
                    "push_paths": {
                        "waiting_manifest": str(waiting_manifest),
                    },
                    "push_export_cache": {
                        "manifest": str(root / "logs" / "export.json"),
                    },
                },
                str(completed): {
                    "push_status_kind": "imported_ok",
                    "push_paths": {"manifest": str(completed_manifest)},
                },
            }

            with mock.patch.object(sk_common, "STATE_PATH", state_path):
                sk_common.save_state(state)
                reference_path = root / sk_common.UNREAL_WAIT_REFERENCE_FILENAME
                receipt = json.loads(reference_path.read_text(encoding="utf-8"))
                self.assertEqual(receipt["kind"], "sk_batch_unreal_wait_references")
                self.assertEqual(len(receipt["items"]), 1)
                self.assertEqual(receipt["items"][0]["queue_id"], str(pending))
                self.assertEqual(
                    receipt["items"][0]["push_paths"]["waiting_manifest"],
                    str(waiting_manifest),
                )

                state[str(pending)]["push_status_kind"] = "imported_ok"
                sk_common.save_state(state)
                receipt = json.loads(reference_path.read_text(encoding="utf-8"))
                self.assertEqual(receipt["items"], [])

    def test_pending_reference_remains_protective_if_final_receipt_write_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_path = root / "sk_batch_state.json"
            pending = root / "SK_pending.spm"
            pending.write_bytes(b"pending")
            state = {
                str(pending): {
                    "push_status_kind": "exported_pending_unreal",
                    "push_paths": {
                        "waiting_manifest": str(root / "logs" / "wait.json")
                    },
                }
            }

            with mock.patch.object(sk_common, "STATE_PATH", state_path):
                sk_common.save_state(state)
                reference_path = root / sk_common.UNREAL_WAIT_REFERENCE_FILENAME
                state[str(pending)]["push_status_kind"] = "imported_ok"
                original_write = sk_common._atomic_write_json
                reference_writes = 0

                def fail_final_reference(path, data):
                    nonlocal reference_writes
                    if Path(path) == reference_path:
                        reference_writes += 1
                        if reference_writes == 2:
                            raise OSError("injected final receipt failure")
                    return original_write(path, data)

                with mock.patch.object(
                    sk_common,
                    "_atomic_write_json",
                    side_effect=fail_final_reference,
                ):
                    with self.assertRaisesRegex(OSError, "final receipt failure"):
                        sk_common.save_state(state)

                stored = json.loads(state_path.read_text(encoding="utf-8"))
                self.assertEqual(
                    stored[str(pending)]["push_status_kind"],
                    "imported_ok",
                )
                receipt = json.loads(reference_path.read_text(encoding="utf-8"))
                self.assertEqual(len(receipt["items"]), 1)
                self.assertEqual(receipt["items"][0]["queue_id"], str(pending))

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

    def test_legacy_code_inclusive_cache_migrates_without_new_fingerprint(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            blend = root / "large.blend"
            content_contract = root / "repair_report.json"
            exporter_code = root / "exporter.py"
            blend.write_bytes(b"blend-content")
            content_contract.write_text("content-v1", encoding="utf-8")
            exporter_code.write_text("code-v1", encoding="utf-8")
            old_fingerprint, legacy_cache, _hit = (
                cached_push_source_fingerprint(
                    blend, [content_contract, exporter_code]
                )
            )
            legacy_cache.pop("fingerprint_contract")

            exporter_code.write_text("code-v2", encoding="utf-8")
            migrated, migrated_cache, cache_hit = (
                cached_push_source_fingerprint(
                    blend, [content_contract], cache=legacy_cache
                )
            )

        self.assertTrue(cache_hit)
        self.assertEqual(migrated, old_fingerprint)
        self.assertEqual(
            migrated_cache["fingerprint_contract"], "content_only_v2"
        )
        self.assertEqual(
            migrated_cache["snapshot"]["dependencies"],
            [legacy_cache["snapshot"]["dependencies"][0]],
        )

    def test_legacy_cache_does_not_migrate_after_content_contract_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            blend = root / "large.blend"
            content_contract = root / "repair_report.json"
            exporter_code = root / "exporter.py"
            blend.write_bytes(b"blend-content")
            content_contract.write_text("content-v1", encoding="utf-8")
            exporter_code.write_text("code-v1", encoding="utf-8")
            old_fingerprint, legacy_cache, _hit = (
                cached_push_source_fingerprint(
                    blend, [content_contract, exporter_code]
                )
            )
            legacy_cache.pop("fingerprint_contract")

            content_contract.write_text("content-v2", encoding="utf-8")
            changed, _changed_cache, cache_hit = (
                cached_push_source_fingerprint(
                    blend, [content_contract], cache=legacy_cache
                )
            )

        self.assertFalse(cache_hit)
        self.assertNotEqual(changed, old_fingerprint)

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

    def test_manifest_code_drift_does_not_invalidate_immutable_export(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            exported = root / "mesh.fbx"
            code = root / "exporter.py"
            exported.write_bytes(b"fbx")
            code.write_text("version = 2", encoding="utf-8")
            export_stat = exported.stat()
            item = {
                "fingerprint": "manifest",
                "exported_files": [{
                    "path": str(exported),
                    "size": export_stat.st_size,
                    "mtime_ns": export_stat.st_mtime_ns,
                    "fingerprint": "not-needed",
                }],
                "code_files": [{
                    "path": str(code),
                    "size": 1,
                    "mtime_ns": 1,
                    "fingerprint": "old-code",
                }],
            }

            self.assertTrue(manifest_item_files_match(item))


if __name__ == "__main__":
    unittest.main()
