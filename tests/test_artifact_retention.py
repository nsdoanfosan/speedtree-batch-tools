import contextlib
import io
import json
import os
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest import mock

import artifact_retention as retention
from artifact_retention import (
    DEFAULT_RETENTION_POLICIES,
    LOG_SCOPE,
    PRODUCTION_BACKUP_SCOPE,
    RETRY_SCOPE,
    RetentionPolicy,
    apply_retention_plan,
    generated_production_backup_kind,
    is_manual_copy_inventory_artifact,
    main,
    plan_retention,
    seal_backup_transaction,
    verify_backup_transaction_manifest,
)


class ArtifactRetentionTests(unittest.TestCase):
    def _write(self, path, payload=b"0123456789", age_seconds=1_000):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        stamp = time.time() - age_seconds
        os.utime(path, (stamp, stamp))
        return path

    @contextlib.contextmanager
    def _log_scope(self, root):
        with mock.patch.object(
            retention, "REPOSITORY_LOG_ROOT", root
        ), mock.patch.dict(
            os.environ,
            {"LOCALAPPDATA": str(Path(root).parent / "_fixture_local")},
            clear=False,
        ):
            yield

    @contextlib.contextmanager
    def _production_scope(self, root):
        with mock.patch.object(
            retention, "PRODUCTION_TREE_ROOT", root
        ), mock.patch.dict(
            os.environ,
            {"LOCALAPPDATA": str(Path(root).parent / "_fixture_local")},
            clear=False,
        ):
            yield

    def _retry_environment(self, local_app_data):
        return mock.patch.dict(
            os.environ, {"LOCALAPPDATA": str(local_app_data)}, clear=False
        )

    def test_defaults_are_dry_run_and_manual_copy_cleanup_is_off(self):
        self.assertTrue(DEFAULT_RETENTION_POLICIES)
        for scope, policy in DEFAULT_RETENTION_POLICIES.items():
            with self.subTest(scope=scope):
                self.assertTrue(policy.dry_run)
        self.assertFalse(
            DEFAULT_RETENTION_POLICIES[PRODUCTION_BACKUP_SCOPE].include_manual_copies
        )
        self.assertFalse(
            DEFAULT_RETENTION_POLICIES[
                PRODUCTION_BACKUP_SCOPE
            ].include_retained_recovery_archives
        )

    def test_every_timestamped_backup_format_shares_its_asset_series(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "Tree"
            owner = root / "tree"
            backup_dir = owner / "_spm_backups"
            for index in range(3):
                stamp = f"2026080{index + 1}_01010{index}"
                self._write(
                    owner / f"SK_tree.pcgtex_backup_{stamp}.spm",
                    age_seconds=1_000 + index,
                )
                self._write(
                    owner / f"SK_tree.skbatch_backup_{stamp}.spm",
                    age_seconds=2_000 + index,
                )
                self._write(
                    backup_dir / f"SK_tree.codex_backup_{stamp}.spm",
                    age_seconds=3_000 + index,
                )
                self._write(
                    backup_dir / f"__spm_sync_verify_{index}.spm",
                    age_seconds=4_000 + index,
                )
            with self._production_scope(root), mock.patch.object(
                retention,
                "_creation_evidence_ns",
                side_effect=lambda file_stat: file_stat.st_mtime_ns,
            ):
                plan = plan_retention(
                    root,
                    scope=PRODUCTION_BACKUP_SCOPE,
                    policy=RetentionPolicy(keep_count=2, min_age_seconds=0, max_bytes=0),
                )

            delete_rows = [row for row in plan["entries"] if row["action"] == "delete"]
            self.assertEqual(len(delete_rows), 6)
            series = {
                row["series_id"]
                for row in plan["entries"]
                if row["kind"] in {"backup_sibling", "backup_directory:_spm_backups"}
            }
            self.assertEqual(len(series), 3)

    def test_scope_roots_are_exact_not_arbitrary_parent_or_similar_name(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            logs = base / "repo" / "sk_batch" / "logs"
            logs.mkdir(parents=True)
            with self._log_scope(logs):
                plan_retention(logs, scope=LOG_SCOPE)
                with self.assertRaisesRegex(ValueError, "exact logs root"):
                    plan_retention(logs.parent, scope=LOG_SCOPE)
                with self.assertRaisesRegex(ValueError, "exact logs root"):
                    plan_retention(base / "logs", scope=LOG_SCOPE)

            local = base / "LocalAppData"
            retry = local / "SpeedTreeBatchTools" / "retry_progress"
            retry.mkdir(parents=True)
            with self._retry_environment(local):
                plan_retention(retry, scope=RETRY_SCOPE)
                with self.assertRaisesRegex(ValueError, "exact retry_progress root"):
                    plan_retention(local, scope=RETRY_SCOPE)

    def test_manual_copy_is_backup_inventory_and_needs_explicit_policy(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "Tree"
            manual_ko = self._write(root / "SK_tree_02 - 복사본.spm")
            manual_en = self._write(root / "SK_tree_02 - Copy (2).spm")
            authored = self._write(root / "SK_copy_habitat_01.spm")
            with self._production_scope(root):
                plan = plan_retention(
                    root,
                    scope=PRODUCTION_BACKUP_SCOPE,
                    policy=RetentionPolicy(0, 0, 0),
                )

            self.assertTrue(is_manual_copy_inventory_artifact(manual_ko))
            self.assertEqual(
                generated_production_backup_kind(manual_en), "manual_copy_backup"
            )
            self.assertFalse(is_manual_copy_inventory_artifact(authored))
            self.assertEqual(plan["planned_delete_count"], 0)
            self.assertEqual(
                {Path(row["path"]) for row in plan["manual_copy_inventory"]},
                {manual_ko.resolve(), manual_en.resolve()},
            )
            self.assertTrue(
                all(
                    row["retention_basis"]
                    == "manual_copy_requires_explicit_backup_policy"
                    for row in plan["manual_copy_inventory"]
                )
            )
            self.assertNotIn(authored.resolve(), {Path(row["path"]) for row in plan["entries"]})

    def test_explicit_manual_copy_policy_can_plan_but_apply_needs_tree_ack(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "Tree"
            manual = self._write(root / "SK_tree_02 - Copy.spm")
            policy = RetentionPolicy(
                keep_count=0,
                min_age_seconds=0,
                max_bytes=0,
                include_manual_copies=True,
            )
            with self._production_scope(root):
                plan = plan_retention(
                    root, scope=PRODUCTION_BACKUP_SCOPE, policy=policy
                )
                with self.assertRaisesRegex(ValueError, "acknowledgement"):
                    apply_retention_plan(plan, apply=True)
                result = apply_retention_plan(
                    plan,
                    apply=True,
                    root_acknowledgement=root,
                )

            self.assertEqual(result["deleted"], [str(manual.resolve())])
            self.assertFalse(manual.exists())

    def test_copy2_preserved_mtime_does_not_make_new_backup_old(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "Tree"
            source = self._write(Path(temporary) / "source.spm", age_seconds=100_000)
            copy = root / "SK_tree_02 - Copy.spm"
            copy.parent.mkdir(parents=True)
            import shutil

            shutil.copy2(source, copy)
            policy = RetentionPolicy(
                keep_count=0,
                min_age_seconds=60,
                max_bytes=0,
                include_manual_copies=True,
            )
            with self._production_scope(root):
                plan = plan_retention(
                    root, scope=PRODUCTION_BACKUP_SCOPE, policy=policy
                )
            row = next(row for row in plan["entries"] if Path(row["path"]) == copy.resolve())
            self.assertEqual(row["retention_basis"], "younger_than_min_age")
            self.assertEqual(plan["planned_delete_count"], 0)

    def test_zero_min_age_does_not_depend_on_clock_rounding(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "Tree"
            backup = self._write(
                root
                / "_spm_backups"
                / "SK_tree.skbatch_backup_20260806_010101.spm"
            )
            with self._production_scope(root):
                plan = plan_retention(
                    root,
                    scope=PRODUCTION_BACKUP_SCOPE,
                    policy=RetentionPolicy(0, 0, 0),
                    now=0,
                )

            row = next(
                row
                for row in plan["entries"]
                if Path(row["path"]) == backup.resolve()
            )
            self.assertEqual(row["action"], "delete")
            self.assertNotEqual(
                row["retention_basis"],
                "younger_than_min_age",
            )

    def test_uncertain_nested_production_bundle_is_never_partially_planned(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "Tree"
            bundle = root / "tree" / "_spm_backups" / "transaction_20260805"
            first = self._write(bundle / "SK_tree.spm")
            second = self._write(bundle / "receipt.json")
            direct = self._write(
                root
                / "tree"
                / "_spm_backups"
                / "SK_tree.skbatch_backup_20260805_010101.spm"
            )
            with self._production_scope(root):
                plan = plan_retention(
                    root,
                    scope=PRODUCTION_BACKUP_SCOPE,
                    policy=RetentionPolicy(0, 0, 0),
                )
            rows = {Path(row["path"]): row for row in plan["entries"]}

            self.assertEqual(
                rows[first.resolve()]["retention_basis"],
                "uncertain_multi_file_recovery_bundle",
            )
            self.assertEqual(
                rows[second.resolve()]["retention_basis"],
                "uncertain_multi_file_recovery_bundle",
            )
            self.assertEqual(rows[direct.resolve()]["action"], "delete")
            self.assertGreater(plan["budget_unmet_bytes"], 0)

    def test_production_recovery_receipt_automatically_protects_its_backup(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "Tree"
            backup_dir = root / "tree" / "_spm_backups"
            backup = self._write(
                backup_dir / "SK_tree.skbatch_backup_20260805_010101.spm"
            )
            self._write(
                backup_dir / "calibration_recovery.json",
                json.dumps({"backup": backup.name}).encode("utf-8"),
            )
            with self._production_scope(root):
                plan = plan_retention(
                    root,
                    scope=PRODUCTION_BACKUP_SCOPE,
                    policy=RetentionPolicy(0, 0, 0),
                )
            rows = {Path(row["path"]): row for row in plan["entries"]}
            self.assertEqual(
                rows[backup.resolve()]["retention_basis"],
                "protected_current_active_or_referenced",
            )
            self.assertEqual(rows[backup.resolve()]["action"], "keep")

    def test_manifest_sealed_nested_bundle_is_planned_and_removed_as_one_set(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "Tree"
            bundle = root / "tree" / "_spm_backups" / "generator_sync_tx_01"
            spm = self._write(bundle / "TreeRoot" / "SK_tree.spm")
            receipt = self._write(bundle / "receipt.json", payload=b'{"ok": true}')
            seal_backup_transaction(
                bundle,
                (spm, receipt),
                producer="spm_generator_sync",
                transaction_id="tx-01",
            )
            verified = verify_backup_transaction_manifest(bundle)
            self.assertTrue(verified["valid"], verified)

            with self._production_scope(root):
                plan = plan_retention(
                    root,
                    scope=PRODUCTION_BACKUP_SCOPE,
                    policy=RetentionPolicy(0, 0, 0),
                )
                bundle_row = next(
                    row for row in plan["bundles"] if row["seal_valid"]
                )
                self.assertEqual(
                    bundle_row["atomic_mode"],
                    "sealed_directory_archive_bridge",
                )
                self.assertEqual(plan["manifest_sealed_bundle_count"], 1)
                self.assertEqual(plan["planned_delete_count"], 3)
                result = apply_retention_plan(
                    plan,
                    apply=True,
                    root_acknowledgement=root,
                )

            self.assertEqual(result["skipped"], [])
            self.assertEqual(len(result["deleted"]), 3)
            self.assertEqual(result["retained_recovery_archives"], [])
            self.assertFalse(bundle.exists())
            self.assertEqual(
                list((root / "tree" / "_spm_backups").glob("*.zip")), []
            )

    def test_manifest_with_new_undeclared_member_stays_budget_unmet(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "Tree"
            bundle = root / "tree" / "_spm_backups" / "texture_normalize_tx_01"
            spm = self._write(bundle / "SK_tree.spm")
            seal_backup_transaction(
                bundle,
                (spm,),
                producer="texture_normalize",
            )
            extra = self._write(bundle / "late_receipt.json")
            verification = verify_backup_transaction_manifest(bundle)
            self.assertFalse(verification["valid"])

            with self._production_scope(root):
                plan = plan_retention(
                    root,
                    scope=PRODUCTION_BACKUP_SCOPE,
                    policy=RetentionPolicy(0, 0, 0),
                )
            rows = {Path(row["path"]): row for row in plan["entries"]}
            self.assertEqual(plan["planned_delete_count"], 0)
            self.assertEqual(plan["uncertain_backup_bundle_count"], 1)
            self.assertEqual(
                rows[extra.resolve()]["retention_basis"],
                "uncertain_multi_file_recovery_bundle",
            )
            self.assertGreater(plan["budget_unmet_bytes"], 0)

    def test_external_recovery_receipt_protects_a_sealed_bundle(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "Tree"
            backup_dir = root / "tree" / "_spm_backups"
            bundle = backup_dir / "generator_sync_tx_03"
            spm = self._write(bundle / "SK_tree.spm")
            seal_backup_transaction(
                bundle,
                (spm,),
                producer="spm_generator_sync",
            )
            self._write(
                backup_dir / "current_recovery.json",
                json.dumps({"restore_bundle": str(bundle)}).encode("utf-8"),
            )
            with self._production_scope(root):
                plan = plan_retention(
                    root,
                    scope=PRODUCTION_BACKUP_SCOPE,
                    policy=RetentionPolicy(0, 0, 0),
                )
            bundle_row = next(row for row in plan["bundles"] if row["seal_valid"])
            self.assertEqual(
                bundle_row["retention_basis"],
                "protected_current_active_or_referenced",
            )
            self.assertEqual(
                [
                    row["action"]
                    for row in plan["entries"]
                    if Path(row["bundle_root"]) == bundle.resolve()
                ],
                ["keep", "keep"],
            )

    def test_manifest_bundle_change_after_plan_skips_whole_bundle(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "Tree"
            bundle = root / "tree" / "_spm_backups" / "atlas_tx_01"
            first = self._write(bundle / "first.spm", payload=b"first")
            second = self._write(bundle / "second.spm", payload=b"second")
            seal_backup_transaction(
                bundle,
                (first, second),
                producer="atlas_cluster_normalization",
            )
            with self._production_scope(root):
                plan = plan_retention(
                    root,
                    scope=PRODUCTION_BACKUP_SCOPE,
                    policy=RetentionPolicy(0, 0, 0),
                )
                second.write_bytes(b"changed")
                result = apply_retention_plan(
                    plan,
                    apply=True,
                    root_acknowledgement=root,
                )

            self.assertTrue(first.exists())
            self.assertTrue(second.exists())
            self.assertTrue(
                all(
                    row["retention_basis"] == "bundle_manifest_changed"
                    for row in result["skipped"]
                )
            )

    def test_partial_or_tampered_plan_cannot_apply_a_manifest_subset(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "Tree"
            bundle = root / "tree" / "_spm_backups" / "atlas_tx_02"
            first = self._write(bundle / "first.spm")
            second = self._write(bundle / "second.spm")
            seal_backup_transaction(
                bundle,
                (first, second),
                producer="atlas_cluster_normalization",
            )
            with self._production_scope(root):
                plan = plan_retention(
                    root,
                    scope=PRODUCTION_BACKUP_SCOPE,
                    policy=RetentionPolicy(0, 0, 0),
                )
                plan["entries"] = [
                    row
                    for row in plan["entries"]
                    if Path(row["path"]).name != "second.spm"
                ]
                result = apply_retention_plan(
                    plan,
                    apply=True,
                    root_acknowledgement=root,
                )

            self.assertTrue(first.exists())
            self.assertTrue(second.exists())
            self.assertTrue(
                all(
                    row["retention_basis"] == "bundle_plan_membership_mismatch"
                    for row in result["skipped"]
                )
            )

    def test_manual_copy_named_member_holds_whole_sealed_bundle_without_opt_in(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "Tree"
            bundle = root / "tree" / "_spm_backups" / "generator_sync_tx_04"
            ordinary = self._write(bundle / "SK_tree.spm")
            manual_named = self._write(bundle / "SK_tree - Copy.spm")
            seal_backup_transaction(
                bundle,
                (ordinary, manual_named),
                producer="spm_generator_sync",
            )
            with self._production_scope(root):
                plan = plan_retention(
                    root,
                    scope=PRODUCTION_BACKUP_SCOPE,
                    policy=RetentionPolicy(0, 0, 0),
                )
            bundle_row = next(row for row in plan["bundles"] if row["seal_valid"])
            self.assertEqual(
                bundle_row["retention_basis"],
                "manual_copy_requires_explicit_backup_policy",
            )
            self.assertEqual(
                {row["action"] for row in plan["entries"]}, {"keep"}
            )

    def test_bundle_root_active_reference_protects_every_sealed_member_at_apply(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "Tree"
            bundle = root / "tree" / "_spm_backups" / "generator_sync_tx_05"
            spm = self._write(bundle / "SK_tree.spm")
            seal_backup_transaction(
                bundle,
                (spm,),
                producer="spm_generator_sync",
            )
            with self._production_scope(root):
                plan = plan_retention(
                    root,
                    scope=PRODUCTION_BACKUP_SCOPE,
                    policy=RetentionPolicy(0, 0, 0),
                )
                result = apply_retention_plan(
                    plan,
                    apply=True,
                    root_acknowledgement=root,
                    active_paths=(bundle,),
                )

            self.assertTrue(spm.exists())
            self.assertTrue(
                all(
                    row["retention_basis"] == "newly_active_or_referenced"
                    for row in result["skipped"]
                )
            )

    def test_sealed_bundle_partial_unlink_failure_retains_complete_archive(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "Tree"
            bundle = root / "tree" / "_spm_backups" / "generator_sync_tx_02"
            first = self._write(bundle / "a.spm", payload=b"first")
            second = self._write(bundle / "b.spm", payload=b"second")
            seal_backup_transaction(
                bundle,
                (first, second),
                producer="spm_generator_sync",
            )
            real_unlink = retention._unlink_bundle_member
            calls = {"count": 0}

            def fail_after_one(path):
                calls["count"] += 1
                if calls["count"] == 2:
                    raise PermissionError("fixture locked member")
                return real_unlink(path)

            with self._production_scope(root):
                plan = plan_retention(
                    root,
                    scope=PRODUCTION_BACKUP_SCOPE,
                    policy=RetentionPolicy(0, 0, 0),
                )
                with mock.patch.object(
                    retention, "_unlink_bundle_member", side_effect=fail_after_one
                ):
                    result = apply_retention_plan(
                        plan,
                        apply=True,
                        root_acknowledgement=root,
                    )

            self.assertEqual(len(result["retained_recovery_archives"]), 1)
            archive_path = Path(result["retained_recovery_archives"][0])
            self.assertTrue(archive_path.is_file())
            with zipfile.ZipFile(archive_path, "r") as archive:
                names = set(archive.namelist())
                self.assertIn("a.spm", names)
                self.assertIn("b.spm", names)
                self.assertIn(retention.BACKUP_BUNDLE_MANIFEST_FILENAME, names)
                self.assertEqual(archive.read("a.spm"), b"first")
                self.assertEqual(archive.read("b.spm"), b"second")
            self.assertTrue(result["skipped"])

            with self._production_scope(root):
                default_plan = plan_retention(
                    root,
                    scope=PRODUCTION_BACKUP_SCOPE,
                    policy=RetentionPolicy(0, 0, 0),
                )
                archive_row = next(
                    row
                    for row in default_plan["entries"]
                    if Path(row["path"]) == archive_path.resolve()
                )
                self.assertEqual(
                    archive_row["retention_basis"],
                    "retained_recovery_archive_requires_explicit_policy",
                )
                authorized_plan = plan_retention(
                    root,
                    scope=PRODUCTION_BACKUP_SCOPE,
                    policy=RetentionPolicy(
                        0,
                        0,
                        0,
                        include_retained_recovery_archives=True,
                    ),
                )
                authorized_result = apply_retention_plan(
                    authorized_plan,
                    apply=True,
                    root_acknowledgement=root,
                )

            self.assertFalse(archive_path.exists())
            self.assertFalse(bundle.exists())
            self.assertIn(str(archive_path), authorized_result["deleted"])

    def test_unregistered_forged_manifest_never_becomes_deletion_authority(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "Tree"
            bundle = root / "tree" / "_spm_backups" / "foreign_tx_01"
            spm = self._write(bundle / "SK_tree.spm")
            seal_backup_transaction(
                bundle,
                (spm,),
                producer="spm_generator_sync",
            )
            manifest = bundle / retention.BACKUP_BUNDLE_MANIFEST_FILENAME
            payload = json.loads(manifest.read_text(encoding="utf-8"))
            payload["producer"] = "foreign_hand_authored_tool"
            manifest.write_text(json.dumps(payload), encoding="utf-8")

            verification = verify_backup_transaction_manifest(bundle)
            self.assertFalse(verification["valid"])
            self.assertIn(
                "producer_unregistered", verification["retention_basis"]
            )
            with self._production_scope(root):
                plan = plan_retention(
                    root,
                    scope=PRODUCTION_BACKUP_SCOPE,
                    policy=RetentionPolicy(0, 0, 0),
                )
            self.assertEqual(plan["planned_delete_count"], 0)
            self.assertEqual(plan["uncertain_backup_bundle_count"], 1)

    def test_dotdot_recovery_archive_name_cannot_escape_backup_namespace(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "Tree"
            namespace = root / "tree" / "_spm_backups"
            namespace.mkdir(parents=True)
            outside = self._write(root / "tree" / "must_not_delete.spm")
            forged = namespace / (
                "...retention_delete_" + "a" * 32 + ".zip"
            )
            payload = {
                "schema_version": retention.BACKUP_BUNDLE_MANIFEST_SCHEMA_VERSION,
                "kind": retention.BACKUP_BUNDLE_MANIFEST_KIND,
                "complete_membership": True,
                "producer": "spm_generator_sync",
                "producer_schema_version": (
                    retention.BACKUP_BUNDLE_PRODUCER_SCHEMA_VERSION
                ),
                "transaction_id": "forged-dotdot",
                "bundle_basename": "..",
                "directories": [],
                "members": [],
            }
            with zipfile.ZipFile(forged, "w") as archive:
                archive.writestr(
                    retention.BACKUP_BUNDLE_MANIFEST_FILENAME,
                    json.dumps(payload).encode("utf-8"),
                )

            verification = retention.verify_retained_recovery_archive(forged)
            self.assertFalse(verification["valid"])
            self.assertIn(
                "unsafe recovery archive transaction",
                verification["retention_basis"],
            )
            with self._production_scope(root):
                plan = plan_retention(
                    root,
                    scope=PRODUCTION_BACKUP_SCOPE,
                    policy=RetentionPolicy(
                        0,
                        0,
                        0,
                        include_retained_recovery_archives=True,
                    ),
                )
            archive_row = next(
                row for row in plan["entries"] if Path(row["path"]) == forged.resolve()
            )
            self.assertEqual(
                archive_row["retention_basis"],
                "retained_recovery_archive_invalid",
            )
            self.assertEqual(archive_row["action"], "keep")
            self.assertTrue(outside.exists())

    def test_budget_respects_age_keep_active_and_receipt_reference(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "repo" / "sk_batch" / "logs"
            old_delete = self._write(root / "old_delete.log", age_seconds=1_000)
            referenced = self._write(root / "referenced.fbx", age_seconds=900)
            active = self._write(root / "active.log", age_seconds=800)
            recent = self._write(root / "recent.json", age_seconds=1)
            newest = self._write(root / "newest.log", age_seconds=700)
            receipt = self._write(
                base / "receipts" / "current.json",
                json.dumps({"report": str(referenced.resolve())}).encode("utf-8"),
            )
            # This test isolates budget ordering; copy2/creation-time safety is
            # covered separately above.
            with self._log_scope(root), mock.patch.object(
                retention,
                "_creation_evidence_ns",
                side_effect=lambda file_stat: file_stat.st_mtime_ns,
            ):
                plan = plan_retention(
                    root,
                    scope=LOG_SCOPE,
                    policy=RetentionPolicy(keep_count=1, min_age_seconds=100, max_bytes=10),
                    active_paths=(active,),
                    receipt_paths=(receipt,),
                )
            rows = {Path(row["path"]): row for row in plan["entries"]}

            self.assertEqual(rows[old_delete.resolve()]["action"], "delete")
            self.assertEqual(
                rows[referenced.resolve()]["retention_basis"],
                "protected_current_active_or_referenced",
            )
            self.assertEqual(
                rows[active.resolve()]["retention_basis"],
                "protected_current_active_or_referenced",
            )
            self.assertEqual(
                rows[recent.resolve()]["retention_basis"],
                "younger_than_min_age",
            )
            self.assertEqual(
                rows[newest.resolve()]["retention_basis"],
                "newest_keep_count",
            )
            self.assertGreater(plan["budget_unmet_bytes"], 0)
            self.assertEqual(apply_retention_plan(plan)["status"], "dry_run")
            self.assertTrue(old_delete.exists())

    def test_relative_latest_receipt_is_protected_at_plan_and_rechecked_at_apply(self):
        with tempfile.TemporaryDirectory() as temporary:
            local = Path(temporary) / "LocalAppData"
            root = local / "SpeedTreeBatchTools" / "retry_progress"
            first = self._write(
                root / ("retry_19700101T000001_" + "a" * 32 + ".json")
            )
            second = self._write(
                root / ("retry_19700101T000002_" + "b" * 32 + ".json")
            )
            latest = self._write(
                root / "latest.json",
                json.dumps({"receipt": first.name}).encode("utf-8"),
            )
            policy = RetentionPolicy(0, 0, 0)
            with self._retry_environment(local):
                plan = plan_retention(root, scope=RETRY_SCOPE, policy=policy)
                rows = {Path(row["path"]): row for row in plan["entries"]}
                self.assertEqual(
                    rows[first.resolve()]["retention_basis"],
                    "protected_current_active_or_referenced",
                )
                self.assertEqual(rows[second.resolve()]["action"], "delete")

                # The live pointer changes after planning.  Apply must reread
                # it and retain the newly referenced receipt.
                latest.write_text(json.dumps({"receipt": second.name}), encoding="utf-8")
                result = apply_retention_plan(plan, apply=True)

            self.assertTrue(first.exists())
            self.assertTrue(second.exists())
            self.assertEqual(
                result["skipped"],
                [
                    {
                        "path": str(second.resolve()),
                        "retention_basis": "newly_active_or_referenced",
                    }
                ],
            )

    def test_apply_rechecks_active_and_sha256_not_only_size_and_mtime(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "repo" / "sk_batch" / "logs"
            changed = self._write(root / "changed.log", payload=b"abcdefghij")
            active = self._write(root / "active.log", payload=b"klmnopqrst")
            with self._log_scope(root):
                plan = plan_retention(
                    root,
                    scope=LOG_SCOPE,
                    policy=RetentionPolicy(0, 0, 0),
                )
                changed_stat = changed.stat()
                changed.write_bytes(b"0123456789")
                os.utime(
                    changed,
                    ns=(changed_stat.st_atime_ns, changed_stat.st_mtime_ns),
                )
                result = apply_retention_plan(
                    plan, apply=True, active_paths=(active,)
                )

            skipped = {
                Path(row["path"]): row["retention_basis"]
                for row in result["skipped"]
            }
            self.assertIn(skipped[changed.resolve()], {"identity_changed", "content_hash_changed"})
            self.assertEqual(skipped[active.resolve()], "newly_active_or_referenced")
            self.assertTrue(changed.exists())
            self.assertTrue(active.exists())

    def test_sha256_rejects_changed_content_even_if_plan_identity_is_refreshed(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "repo" / "sk_batch" / "logs"
            changed = self._write(root / "changed.log", payload=b"abcdefghij")
            with self._log_scope(root):
                plan = plan_retention(
                    root,
                    scope=LOG_SCOPE,
                    policy=RetentionPolicy(0, 0, 0),
                )
                row = plan["entries"][0]
                original_sha256 = row["sha256"]
                old = changed.stat()
                changed.write_bytes(b"0123456789")
                os.utime(changed, ns=(old.st_atime_ns, old.st_mtime_ns))
                # Even if all mutable stat fields in a serialized plan were
                # refreshed, the planned content identity remains binding.
                current = changed.stat()
                row.update(
                    {
                        "bytes": current.st_size,
                        "mtime_ns": current.st_mtime_ns,
                        "ctime_ns": current.st_ctime_ns,
                        "device": current.st_dev,
                        "inode": current.st_ino,
                    }
                )
                result = apply_retention_plan(plan, apply=True)

            self.assertEqual(row["sha256"], original_sha256)
            self.assertEqual(
                result["skipped"],
                [
                    {
                        "path": str(changed.resolve()),
                        "retention_basis": "content_hash_changed",
                    }
                ],
            )
            self.assertTrue(changed.exists())

    def test_apply_refuses_a_forged_multi_file_bundle_without_partial_delete(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "repo" / "sk_batch" / "logs"
            first = self._write(root / "first.log")
            second = self._write(root / "second.log")
            with self._log_scope(root):
                plan = plan_retention(
                    root,
                    scope=LOG_SCOPE,
                    policy=RetentionPolicy(0, 0, 0),
                )
                self.assertEqual(plan["planned_delete_count"], 2)
                for row in plan["entries"]:
                    row["bundle_id"] = "forged-shared-bundle"
                result = apply_retention_plan(plan, apply=True)

            self.assertTrue(first.exists())
            self.assertTrue(second.exists())
            self.assertEqual(
                {row["retention_basis"] for row in result["skipped"]},
                {"non_atomic_bundle_refused"},
            )

    @unittest.skipUnless(hasattr(os, "symlink"), "symlink support unavailable")
    def test_reparse_candidate_is_not_followed_or_deleted(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "repo" / "sk_batch" / "logs"
            outside = self._write(base / "outside" / "outside.log")
            root.mkdir(parents=True)
            link = root / "linked.log"
            try:
                link.symlink_to(outside)
            except OSError:
                self.skipTest("symlink creation is not permitted")
            with self._log_scope(root):
                plan = plan_retention(
                    root,
                    scope=LOG_SCOPE,
                    policy=RetentionPolicy(0, 0, 0),
                )
            self.assertNotIn(link, {Path(row["path"]) for row in plan["entries"]})
            self.assertTrue(outside.exists())

    def test_cli_writes_full_dry_run_json_and_never_deletes_by_default(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            logs = base / "repo" / "sk_batch" / "logs"
            local = base / "LocalAppData"
            retry = local / "SpeedTreeBatchTools" / "retry_progress"
            log_file = self._write(logs / "old.log")
            retry_file = self._write(
                retry / ("retry_19700101T000001_" + "a" * 32 + ".json")
            )
            output = base / "maintenance" / "plan.json"
            stdout = io.StringIO()
            with self._log_scope(logs), self._retry_environment(local), contextlib.redirect_stdout(stdout):
                code = main(
                    [
                        "--scope",
                        LOG_SCOPE,
                        "--scope",
                        RETRY_SCOPE,
                        "--keep-count",
                        "0",
                        "--min-age-days",
                        "0",
                        "--max-gib",
                        "0",
                        "--output",
                        str(output),
                    ]
                )

            payload = json.loads(output.read_text(encoding="utf-8"))
            console = json.loads(stdout.getvalue())
            self.assertEqual(code, 0)
            self.assertEqual(payload["status"], "dry_run")
            self.assertTrue(payload["push_independent"])
            self.assertFalse(payload["pipeline_gate"])
            self.assertEqual(console["output"], str(output.resolve()))
            self.assertEqual(payload["summary"]["planned_delete_count"], 2)
            self.assertTrue(log_file.exists())
            self.assertTrue(retry_file.exists())

    def test_cli_apply_operates_logs_without_production_ack(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            logs = base / "repo" / "sk_batch" / "logs"
            old = self._write(logs / "old.log")
            output = base / "applied.json"
            with self._log_scope(logs), contextlib.redirect_stdout(io.StringIO()):
                code = main(
                    [
                        "--scope",
                        LOG_SCOPE,
                        "--apply",
                        "--keep-count",
                        "0",
                        "--min-age-days",
                        "0",
                        "--max-gib",
                        "0",
                        "--output",
                        str(output),
                    ]
                )

            payload = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(code, 0)
            self.assertEqual(payload["status"], "applied")
            self.assertFalse(payload["plans"][0]["dry_run"])
            self.assertEqual(payload["summary"]["deleted_count"], 1)
            self.assertFalse(old.exists())

    def test_cli_manual_copy_apply_requires_exact_tree_ack_before_mutation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "Tree"
            manual = self._write(root / "SK_tree_02 - Copy.spm")
            common = [
                "--scope",
                PRODUCTION_BACKUP_SCOPE,
                "--apply",
                "--include-manual-copies",
                "--keep-count",
                "0",
                "--min-age-days",
                "0",
                "--max-gib",
                "0",
                "--output",
                str(Path(temporary) / "result.json"),
            ]
            with self._production_scope(root), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as raised:
                    main(common)
                self.assertEqual(raised.exception.code, 2)
                self.assertTrue(manual.exists())
                with contextlib.redirect_stdout(io.StringIO()):
                    code = main([*common, "--tree-root-ack", str(root)])

            self.assertEqual(code, 0)
            self.assertFalse(manual.exists())

    def test_bat_launcher_defaults_to_the_non_apply_cli(self):
        launcher = Path(retention.__file__).with_name(
            "SpeedTree_Artifact_Maintenance.bat"
        )
        text = launcher.read_text(encoding="utf-8")
        self.assertIn('set "GUARD=%~dp0launch_guard.pyw"', text)
        self.assertIn('python "%GUARD%" "%MAINTENANCE%" %*', text)
        self.assertNotIn("--apply", text)


if __name__ == "__main__":
    unittest.main()
