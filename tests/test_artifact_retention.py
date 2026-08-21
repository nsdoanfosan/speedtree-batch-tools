import contextlib
import io
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import artifact_retention as retention
from artifact_retention import (
    DEFAULT_MAX_AGE_SECONDS,
    DEFAULT_RETENTION_POLICIES,
    DEFAULT_TARGET_BYTES,
    HARD_MAX_BYTES,
    LOG_SCOPE,
    PCG_REPORT_SCOPE,
    PRODUCTION_BACKUP_SCOPE,
    QUEUE_STATE_SCOPE,
    RetentionCapacityError,
    RetentionPolicy,
    apply_retention_plan,
    generated_log_kind,
    generated_production_backup_kind,
    main,
    managed_output_reservation,
    plan_global_retention,
    plan_retention,
)


class ArtifactRetentionTests(unittest.TestCase):
    def _write(self, path, payload=b"0123456789", age_seconds=0):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        stamp = time.time() - age_seconds
        os.utime(path, (stamp, stamp))
        return path

    @contextlib.contextmanager
    def _log_scope(self, root):
        with mock.patch.object(retention, "REPOSITORY_LOG_ROOT", Path(root)), mock.patch.dict(
            os.environ,
            {"LOCALAPPDATA": str(Path(root).parent / "LocalAppData")},
            clear=False,
        ):
            yield

    @contextlib.contextmanager
    def _production_scope(self, root):
        with mock.patch.object(retention, "PRODUCTION_TREE_ROOT", Path(root)), mock.patch.dict(
            os.environ,
            {"LOCALAPPDATA": str(Path(root).parent / "LocalAppData")},
            clear=False,
        ):
            yield

    def _mtime_is_generation_time(self):
        return mock.patch.object(
            retention,
            "_creation_evidence_ns",
            side_effect=lambda file_stat: file_stat.st_mtime_ns,
        )

    def test_defaults_are_automatic_three_days_and_below_ten_gib(self):
        self.assertEqual(DEFAULT_MAX_AGE_SECONDS, 3 * 24 * 60 * 60)
        self.assertLess(DEFAULT_TARGET_BYTES, HARD_MAX_BYTES)
        self.assertEqual(HARD_MAX_BYTES, 10 * 1024**3)
        for policy in DEFAULT_RETENTION_POLICIES.values():
            self.assertFalse(policy.dry_run)
            self.assertEqual(policy.max_age_seconds, DEFAULT_MAX_AGE_SECONDS)
            self.assertLess(policy.max_bytes, HARD_MAX_BYTES)

    def test_three_day_boundary_is_strictly_older_not_equal(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "repo" / "sk_batch" / "logs"
            now = time.time()
            at_boundary = self._write(
                root / "at_boundary.log", age_seconds=DEFAULT_MAX_AGE_SECONDS
            )
            older = self._write(
                root / "older.log", age_seconds=DEFAULT_MAX_AGE_SECONDS + 1
            )
            with self._log_scope(root), self._mtime_is_generation_time():
                plan = plan_retention(
                    root,
                    scope=LOG_SCOPE,
                    policy=RetentionPolicy(0, DEFAULT_MAX_AGE_SECONDS, HARD_MAX_BYTES),
                    now=now,
                )
            rows = {Path(row["path"]): row for row in plan["entries"]}
            self.assertEqual(rows[at_boundary.resolve()]["action"], "keep")
            self.assertEqual(rows[older.resolve()]["action"], "delete")
            self.assertEqual(
                rows[older.resolve()]["retention_basis"], "older_than_max_age"
            )

    def test_exact_capacity_boundary_is_forbidden(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "repo" / "sk_batch" / "logs"
            oldest = self._write(root / "oldest.log", b"a" * 40, age_seconds=30)
            self._write(root / "middle.log", b"b" * 30, age_seconds=20)
            self._write(root / "newest.log", b"c" * 30, age_seconds=10)
            with self._log_scope(root), self._mtime_is_generation_time():
                plan = plan_retention(
                    root,
                    scope=LOG_SCOPE,
                    policy=RetentionPolicy(0, DEFAULT_MAX_AGE_SECONDS, 100),
                )
            self.assertEqual(plan["generated_bytes"], 100)
            self.assertLess(plan["projected_bytes"], 100)
            self.assertEqual(
                next(row for row in plan["entries"] if Path(row["path"]) == oldest.resolve())["action"],
                "delete",
            )

    def test_capacity_deletes_oldest_first(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "repo" / "sk_batch" / "logs"
            paths = [
                self._write(root / f"{index}.log", bytes([index]) * 30, age_seconds=30 - index)
                for index in range(4)
            ]
            with self._log_scope(root), self._mtime_is_generation_time():
                plan = plan_retention(
                    root,
                    scope=LOG_SCOPE,
                    policy=RetentionPolicy(0, DEFAULT_MAX_AGE_SECONDS, 61),
                )
            deleted = [
                Path(row["path"])
                for row in plan["entries"]
                if row["action"] == "delete"
            ]
            self.assertEqual(deleted, [paths[0].resolve(), paths[1].resolve()])
            self.assertEqual(plan["projected_bytes"], 60)

    def test_global_capacity_is_shared_across_scopes(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            logs = base / "repo" / "sk_batch" / "logs"
            reports = base / "repo" / "pcg_st9_texture_batch" / "reports"
            old = self._write(logs / "old.log", b"a" * 60, age_seconds=20)
            self._write(reports / "new.json", b"b" * 60, age_seconds=10)
            with mock.patch.object(retention, "REPOSITORY_LOG_ROOT", logs), mock.patch.object(
                retention, "PCG_REPORT_ROOT", reports
            ), mock.patch.dict(
                os.environ, {"LOCALAPPDATA": str(base / "local")}, clear=False
            ), self._mtime_is_generation_time():
                plan = plan_global_retention(
                    (LOG_SCOPE, PCG_REPORT_SCOPE),
                    max_bytes=100,
                )
            self.assertEqual(plan["generated_bytes"], 120)
            self.assertEqual(plan["projected_bytes"], 60)
            self.assertTrue(plan["target_satisfied"])
            old_row = next(
                row
                for scope_plan in plan["plans"]
                for row in scope_plan["entries"]
                if Path(row["path"]) == old.resolve()
            )
            self.assertEqual(old_row["action"], "delete")

    def test_age_pass_precedes_capacity_pass(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "repo" / "sk_batch" / "logs"
            expired = self._write(
                root / "expired.log",
                b"a" * 10,
                age_seconds=DEFAULT_MAX_AGE_SECONDS + 1,
            )
            within = self._write(root / "within.log", b"b" * 80, age_seconds=10)
            with self._log_scope(root), self._mtime_is_generation_time():
                plan = plan_retention(
                    root,
                    scope=LOG_SCOPE,
                    policy=RetentionPolicy(0, DEFAULT_MAX_AGE_SECONDS, 50),
                )
            rows = {Path(row["path"]): row for row in plan["entries"]}
            self.assertEqual(rows[expired.resolve()]["retention_basis"], "older_than_max_age")
            self.assertEqual(
                rows[within.resolve()]["retention_basis"],
                "over_max_bytes_oldest_eligible",
            )

    def test_log_scope_includes_cache_fbx_pre_repair_and_partial_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "logs"
            paths = [
                self._write(root / "send2ue_export_cache" / "x" / "mesh.fbx"),
                self._write(root / "tree_pre_repair_20260810_010101.blend"),
                self._write(root / "batch_manifest.json"),
                self._write(root / (".batch.1.2." + "a" * 32 + ".tmp")),
            ]
            for path in paths:
                self.assertIsNotNone(generated_log_kind(path, root))

    def test_backup_requires_live_original_and_never_inventories_original(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "Forestportfolio"
            owner = root / "02_nature" / "Tree" / "tree"
            original = self._write(owner / "SK_tree.spm", b"live")
            backup = self._write(
                owner / "_spm_backups" / "SK_tree.skbatch_backup_20260801_010101.spm",
                b"backup",
            )
            with self._production_scope(root):
                plan = plan_retention(
                    root,
                    scope=PRODUCTION_BACKUP_SCOPE,
                    policy=RetentionPolicy(0, 0, HARD_MAX_BYTES),
                )
            rows = {Path(row["path"]): row for row in plan["entries"]}
            self.assertNotIn(original.resolve(), rows)
            self.assertTrue(rows[backup.resolve()]["original_verified"])
            self.assertEqual(rows[backup.resolve()]["action"], "delete")

            original.unlink()
            with self._production_scope(root):
                blocked = plan_retention(
                    root,
                    scope=PRODUCTION_BACKUP_SCOPE,
                    policy=RetentionPolicy(0, 0, HARD_MAX_BYTES),
                )
            row = next(row for row in blocked["entries"] if Path(row["path"]) == backup.resolve())
            self.assertEqual(row["action"], "keep")
            self.assertEqual(row["retention_basis"], "backup_original_unverified")

    def test_sbs_and_named_backup_patterns_require_their_originals(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "Forestportfolio"
            folder = root / "substance"
            original = self._write(folder / "material.sbs", b"live")
            backup = self._write(
                folder / "material.pcgtex_backup_before_graph_20260801_010101.sbs",
                b"backup",
            )
            self.assertEqual(generated_production_backup_kind(backup), "backup_sibling")
            with self._production_scope(root):
                plan = plan_retention(
                    root,
                    scope=PRODUCTION_BACKUP_SCOPE,
                    policy=RetentionPolicy(0, 0, HARD_MAX_BYTES),
                )
            row = next(row for row in plan["entries"] if Path(row["path"]) == backup.resolve())
            self.assertEqual(Path(row["original_path"]), original.resolve())

    def test_apply_rechecks_original_before_unlink(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "Forestportfolio"
            owner = root / "tree"
            original = self._write(owner / "SK_tree.spm", b"live")
            backup = self._write(
                owner / "_spm_backups" / "SK_tree.codex_backup_20260801_010101.spm",
                b"backup",
            )
            with self._production_scope(root):
                plan = plan_retention(
                    root,
                    scope=PRODUCTION_BACKUP_SCOPE,
                    policy=RetentionPolicy(0, 0, HARD_MAX_BYTES),
                )
                original.unlink()
                result = apply_retention_plan(
                    plan, apply=True, root_acknowledgement=root
                )
            self.assertTrue(backup.exists())
            self.assertEqual(
                result["skipped"][0]["retention_basis"],
                "backup_original_missing_or_changed",
            )

    def test_legacy_backup_manifest_maps_only_to_existing_original(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "Forestportfolio"
            tree = root / "02_nature" / "Tree"
            original = self._write(tree / "atlas" / "M_leaf.blend", b"live")
            bundle = tree / "_atlas_cluster_normalization_backups" / "final_20260801_010101"
            backup = self._write(bundle / "files" / "atlas" / "M_leaf.blend", b"copy")
            manifest = {
                "status": "ok",
                "backup_root": str(bundle.resolve()),
                "count": 1,
                "files": [
                    {
                        "source": str(original.resolve()),
                        "backup": str(backup.resolve()),
                        "size": backup.stat().st_size,
                        "sha256": "not-trusted-as-identity",
                    }
                ],
            }
            self._write(
                bundle / "backup_manifest.json",
                json.dumps(manifest).encode("utf-8"),
            )
            with self._production_scope(root):
                plan = plan_retention(
                    root,
                    scope=PRODUCTION_BACKUP_SCOPE,
                    policy=RetentionPolicy(0, 0, HARD_MAX_BYTES),
                )
                row = next(
                    row for row in plan["entries"] if Path(row["path"]) == backup.resolve()
                )
                self.assertTrue(row["original_verified"])
                self.assertEqual(Path(row["original_path"]), original.resolve())
                self.assertEqual(row["atomic_mode"], "single_file")
                result = apply_retention_plan(
                    plan, apply=True, root_acknowledgement=root
                )
            self.assertIn(str(backup.resolve()), result["deleted"], result)
            self.assertFalse(backup.exists())
            self.assertTrue(original.exists())

    def test_verified_multi_file_backup_bundle_applies_as_one_safe_unit(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "Forestportfolio"
            texture = root / "02_nature" / "Tree" / "tree" / "texture"
            originals = [
                self._write(texture / "T_leaf_color.tga", b"live-color"),
                self._write(texture / "T_leaf_normal.tga", b"live-normal"),
            ]
            bundle = texture / "_pcgtex_backups" / "T_leaf_20260801_010101_000001"
            backups = [
                self._write(bundle / original.name, b"backup")
                for original in originals
            ]
            with self._production_scope(root):
                plan = plan_retention(
                    root,
                    scope=PRODUCTION_BACKUP_SCOPE,
                    policy=RetentionPolicy(0, 0, HARD_MAX_BYTES),
                )
                rows = [
                    row
                    for row in plan["entries"]
                    if Path(row["path"]) in {path.resolve() for path in backups}
                ]
                self.assertEqual(len(rows), 2)
                self.assertTrue(all(row["action"] == "delete" for row in rows))
                self.assertTrue(
                    all(
                        row["atomic_mode"] == "verified_original_bundle"
                        for row in rows
                    )
                )
                result = apply_retention_plan(
                    plan, apply=True, root_acknowledgement=root
                )
            self.assertEqual(set(result["deleted"]), {str(path.resolve()) for path in backups})
            self.assertTrue(all(not path.exists() for path in backups))
            self.assertTrue(all(path.exists() for path in originals))

    def test_live_queue_counts_toward_budget_but_is_never_deleted(self):
        with tempfile.TemporaryDirectory() as temporary:
            local = Path(temporary) / "LocalAppData"
            root = local / "SpeedTreeBatchTools"
            queue = self._write(root / "shared_job_queue.json", b"q" * 50)
            abandoned = self._write(
                root / (".shared_job_queue.json.1.2." + "a" * 32 + ".tmp"),
                b"t" * 10,
                age_seconds=10,
            )
            with mock.patch.dict(
                os.environ, {"LOCALAPPDATA": str(local)}, clear=False
            ), self._mtime_is_generation_time():
                plan = plan_retention(
                    root,
                    scope=QUEUE_STATE_SCOPE,
                    policy=RetentionPolicy(0, DEFAULT_MAX_AGE_SECONDS, 55),
                )
            rows = {Path(row["path"]): row for row in plan["entries"]}
            self.assertEqual(rows[queue.resolve()]["action"], "keep")
            self.assertEqual(
                rows[queue.resolve()]["retention_basis"],
                "protected_current_active_or_referenced",
            )
            self.assertEqual(rows[abandoned.resolve()]["action"], "delete")
            self.assertEqual(plan["generated_bytes"], 60)

    def test_pending_unreal_manifest_referenced_by_receipt_is_never_deleted(self):
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary) / "repo"
            root = repo / "sk_batch" / "logs"
            waiting_fbx = self._write(
                root / "SK_tree_waiting.fbx",
                age_seconds=DEFAULT_MAX_AGE_SECONDS + 1,
            )
            waiting_manifest = self._write(
                root / "unreal_wait_20260811_120000.json",
                json.dumps(
                    {
                        "kind": "sk_batch_unreal_wait_queue",
                        "items": [{"files": [str(waiting_fbx)]}],
                    }
                ).encode("utf-8"),
                age_seconds=DEFAULT_MAX_AGE_SECONDS + 1,
            )
            completed_manifest = self._write(
                root / "headless_queue_completed.json",
                json.dumps({"kind": "completed"}).encode("utf-8"),
                age_seconds=DEFAULT_MAX_AGE_SECONDS + 1,
            )
            wait_references = repo / "sk_batch" / "unreal_wait_references.json"
            self._write(
                wait_references,
                json.dumps(
                    {
                        "schema_version": 1,
                        "kind": "sk_batch_unreal_wait_references",
                        "items": [
                            {
                                "queue_id": "tree.spm",
                                "push_status_kind": "exported_pending_unreal",
                                "push_paths": {
                                    "waiting_manifest": str(waiting_manifest)
                                },
                            },
                            {
                                "queue_id": "completed.spm",
                                "push_status_kind": "imported_ok",
                                "push_paths": {
                                    "manifest": str(completed_manifest)
                                },
                            },
                        ],
                    }
                ).encode("utf-8"),
            )

            with mock.patch.object(
                retention, "REPOSITORY_LOG_ROOT", root
            ), mock.patch.object(
                retention,
                "REPOSITORY_UNREAL_WAIT_REFERENCES",
                wait_references,
            ), self._mtime_is_generation_time():
                plan = plan_retention(
                    root,
                    scope=LOG_SCOPE,
                    policy=RetentionPolicy(0, 0, HARD_MAX_BYTES),
                )

            rows = {Path(entry["path"]): entry for entry in plan["entries"]}
            for pending_path in (waiting_manifest, waiting_fbx):
                self.assertEqual(rows[pending_path.resolve()]["action"], "keep")
                self.assertEqual(
                    rows[pending_path.resolve()]["retention_basis"],
                    "protected_current_active_or_referenced",
                )
            self.assertEqual(rows[completed_manifest.resolve()]["action"], "delete")

    def test_locked_unlink_is_reported_and_file_remains(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "repo" / "sk_batch" / "logs"
            locked = self._write(root / "locked.log", age_seconds=10)
            with self._log_scope(root), self._mtime_is_generation_time():
                plan = plan_retention(
                    root,
                    scope=LOG_SCOPE,
                    policy=RetentionPolicy(0, 0, HARD_MAX_BYTES),
                )
                original_unlink = Path.unlink

                def fail_locked(path, *args, **kwargs):
                    if Path(path) == locked:
                        raise PermissionError("locked")
                    return original_unlink(path, *args, **kwargs)

                with mock.patch.object(Path, "unlink", fail_locked):
                    result = apply_retention_plan(plan, apply=True)
            self.assertTrue(locked.exists())
            self.assertEqual(result["skipped"][0]["retention_basis"], "unlink_failed:PermissionError")

    def test_changed_content_with_same_size_and_mtime_is_not_deleted(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "repo" / "sk_batch" / "logs"
            changed = self._write(root / "changed.log", b"abcdefghij", age_seconds=10)
            with self._log_scope(root), self._mtime_is_generation_time():
                plan = plan_retention(
                    root,
                    scope=LOG_SCOPE,
                    policy=RetentionPolicy(0, 0, HARD_MAX_BYTES),
                )
                before = changed.stat()
                changed.write_bytes(b"0123456789")
                os.utime(changed, ns=(before.st_atime_ns, before.st_mtime_ns))
                result = apply_retention_plan(plan, apply=True)
            self.assertTrue(changed.exists())
            self.assertIn(
                result["skipped"][0]["retention_basis"],
                {"identity_changed", "content_hash_changed"},
            )

    @unittest.skipUnless(hasattr(os, "symlink"), "symlink support unavailable")
    def test_reparse_path_escape_is_not_inventoried(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "repo" / "sk_batch" / "logs"
            outside = self._write(base / "outside.log")
            root.mkdir(parents=True)
            link = root / "linked.log"
            try:
                link.symlink_to(outside)
            except OSError:
                self.skipTest("symlink creation is not permitted")
            with self._log_scope(root):
                plan = plan_retention(root, scope=LOG_SCOPE)
            self.assertNotIn(link, {Path(row["path"]) for row in plan["entries"]})
            self.assertTrue(outside.exists())

    def test_reservations_are_concurrent_and_cleaned_after_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            logs = base / "repo" / "sk_batch" / "logs"
            logs.mkdir(parents=True)
            barrier = threading.Barrier(2)
            receipts_seen = []
            errors = []

            def worker(index):
                try:
                    with managed_output_reservation(
                        logs / f"output_{index}.fbx", 1024
                    ):
                        barrier.wait(timeout=10)
                        receipts_seen.append(
                            len(list(retention._reservation_directory().glob("*.json")))
                        )
                        # Do not let either context clean its reservation until
                        # both workers have observed the concurrent state. The
                        # first barrier only proves both contexts were entered;
                        # without this one, the failing worker can exit and
                        # remove its receipt before its peer performs the glob.
                        barrier.wait(timeout=10)
                        if index == 0:
                            raise RuntimeError("producer failed")
                except RuntimeError as exc:
                    if str(exc) != "producer failed":
                        errors.append(exc)
                except BaseException as exc:  # pragma: no cover - diagnostic
                    errors.append(exc)

            patches = (
                mock.patch.object(retention, "REPOSITORY_LOG_ROOT", logs),
                mock.patch.object(retention, "PCG_REPORT_ROOT", base / "pcg_reports"),
                mock.patch.object(retention, "SPM_REPORT_ROOT", base / "spm_reports"),
                mock.patch.object(retention, "SK_CACHE_ROOT", base / "sk_cache"),
                mock.patch.object(retention, "REPOSITORY_ROOT", base / "repo"),
                mock.patch.object(retention, "PRODUCTION_TREE_ROOT", base / "Forestportfolio"),
                mock.patch.dict(os.environ, {"LOCALAPPDATA": str(base / "local")}, clear=False),
            )
            with contextlib.ExitStack() as stack:
                for patcher in patches:
                    stack.enter_context(patcher)
                threads = [threading.Thread(target=worker, args=(index,)) for index in range(2)]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(timeout=30)
                remaining = list(retention._reservation_directory().glob("*.json"))
            self.assertFalse(errors)
            self.assertEqual(receipts_seen, [2, 2])
            self.assertEqual(remaining, [])

    def test_reserved_bytes_participate_in_strict_global_boundary(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "logs"
            self._write(root / "new.log", b"x" * 50)
            with self._log_scope(root), self._mtime_is_generation_time():
                plan = plan_global_retention(
                    (LOG_SCOPE,), max_bytes=100, reserved_bytes=50
                )
            self.assertTrue(plan["target_satisfied"])
            self.assertLess(plan["projected_with_reservations_bytes"], 100)
            self.assertEqual(plan["planned_delete_bytes"], 50)

    def test_single_reservation_at_hard_limit_is_rejected(self):
        with self.assertRaises(RetentionCapacityError):
            retention._begin_output_reservation((Path("x.fbx"),), HARD_MAX_BYTES)

    def test_cli_applies_by_default_and_dry_run_is_explicit(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "repo" / "sk_batch" / "logs"
            old = self._write(root / "old.log", age_seconds=10)
            output = Path(temporary) / "result.json"
            with self._log_scope(root), self._mtime_is_generation_time(), contextlib.redirect_stdout(io.StringIO()):
                code = main(
                    [
                        "--scope",
                        LOG_SCOPE,
                        "--max-age-days",
                        "0",
                        "--max-gib",
                        "1",
                        "--output",
                        str(output),
                    ]
                )
            self.assertEqual(code, 0)
            self.assertFalse(old.exists())
            self.assertEqual(json.loads(output.read_text(encoding="utf-8"))["status"], "applied")

            preview = self._write(root / "preview.log", age_seconds=10)
            with self._log_scope(root), self._mtime_is_generation_time(), contextlib.redirect_stdout(io.StringIO()):
                code = main(
                    [
                        "--scope",
                        LOG_SCOPE,
                        "--dry-run",
                        "--max-age-days",
                        "0",
                        "--max-gib",
                        "1",
                        "--output",
                        str(output),
                    ]
                )
            self.assertEqual(code, 0)
            self.assertTrue(preview.exists())
            self.assertEqual(json.loads(output.read_text(encoding="utf-8"))["status"], "dry_run")


if __name__ == "__main__":
    unittest.main()
