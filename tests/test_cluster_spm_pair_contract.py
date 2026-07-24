from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import cluster_spm_pair_contract as contract  # noqa: E402


class ClusterSpmOutputNameNormalizationTests(unittest.TestCase):
    def make_legacy_output(self, root, payload=b"legacy-output"):
        cluster = Path(root) / "Tree_elm" / "Cluster"
        cluster.mkdir(parents=True)
        legacy = cluster / "branch_elm_01.spm"
        canonical = cluster / "SK_branch_elm_01.spm"
        legacy.write_bytes(payload)
        return legacy, canonical

    def test_resolve_accepts_legacy_and_canonical_names(self):
        with tempfile.TemporaryDirectory() as temporary:
            legacy, canonical = self.make_legacy_output(temporary)
            from_legacy = contract.resolve_cluster_spm_pair(legacy)
            from_canonical = contract.resolve_cluster_spm_pair(canonical)

            self.assertEqual(from_legacy["canonical_spm"], canonical.resolve())
            self.assertEqual(from_legacy["mirror_spm"], legacy.resolve())
            self.assertEqual(from_legacy["pair_id"], from_canonical["pair_id"])
            self.assertEqual(
                from_legacy["receipt_path"].name,
                "SK_branch_elm_01_cluster_spm_pair.json",
            )
            with self.assertRaises(contract.ClusterSpmPairPathError):
                contract.resolve_cluster_spm_pair(Path(temporary) / "x.spm")
            with self.assertRaises(contract.ClusterSpmPairPathError):
                contract.resolve_cluster_spm_pair(legacy.with_name("~branch_elm_01.spm"))

    def test_legacy_output_is_normalized_once_to_canonical_sk_name(self):
        with tempfile.TemporaryDirectory() as temporary:
            legacy, canonical = self.make_legacy_output(temporary, b"generation-1")
            preview = contract.inspect_cluster_spm_pair(legacy)
            self.assertEqual(preview["status"], "normalization_ready")
            self.assertTrue(preview["can_bootstrap"])

            result = contract.bootstrap_cluster_authoring(legacy)

            self.assertEqual(result["status"], "applied")
            self.assertEqual(result["operation"], "normalize_legacy_output_to_canonical")
            self.assertEqual(canonical.read_bytes(), b"generation-1")
            self.assertEqual(legacy.read_bytes(), b"generation-1")
            receipt = json.loads(result["receipt_path"].read_text(encoding="utf-8"))
            self.assertEqual(
                receipt["operation"], "normalize_legacy_output_to_canonical"
            )
            self.assertEqual(
                contract.inspect_cluster_spm_pair(canonical)["status"], "current"
            )

    def test_dry_run_does_not_create_canonical_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            legacy, canonical = self.make_legacy_output(temporary)
            result = contract.bootstrap_cluster_authoring(legacy, dry_run=True)

            self.assertEqual(result["status"], "would_apply")
            self.assertFalse(canonical.exists())
            self.assertFalse(result["receipt_path"].exists())

    def test_existing_canonical_is_idempotent_without_a_receipt(self):
        with tempfile.TemporaryDirectory() as temporary:
            legacy, canonical = self.make_legacy_output(temporary)
            canonical.write_bytes(b"canonical")
            result = contract.bootstrap_cluster_authoring(legacy)

            self.assertEqual(result["status"], "up_to_date")
            self.assertEqual(canonical.read_bytes(), b"canonical")
            self.assertEqual(legacy.read_bytes(), b"legacy-output")

    def test_canonical_changes_never_publish_back_to_legacy_name(self):
        with tempfile.TemporaryDirectory() as temporary:
            legacy, canonical = self.make_legacy_output(temporary)
            contract.bootstrap_cluster_authoring(legacy)
            legacy_before = legacy.read_bytes()
            canonical.write_bytes(b"canonical-v2")

            inspection = contract.inspect_cluster_spm_pair(canonical)
            self.assertEqual(inspection["status"], "current")
            self.assertIn("intentionally ignored", " ".join(inspection["warnings"]))
            with self.assertRaisesRegex(
                contract.ClusterSpmPairConflictError, "not publication targets"
            ):
                contract.publish_cluster_atlas_mirror(canonical)
            self.assertEqual(legacy.read_bytes(), legacy_before)

    def test_legacy_drift_never_changes_canonical_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            legacy, canonical = self.make_legacy_output(temporary)
            contract.bootstrap_cluster_authoring(legacy)
            canonical_before = canonical.read_bytes()
            legacy.write_bytes(b"late-legacy-edit")

            inspection = contract.inspect_cluster_spm_pair(legacy)

            self.assertEqual(inspection["status"], "current")
            self.assertFalse(inspection["can_publish"])
            self.assertEqual(canonical.read_bytes(), canonical_before)

    def test_canonical_only_output_is_current(self):
        with tempfile.TemporaryDirectory() as temporary:
            legacy, canonical = self.make_legacy_output(temporary)
            legacy.unlink()
            canonical.write_bytes(b"canonical")

            inspection = contract.inspect_cluster_spm_pair(canonical)

            self.assertEqual(inspection["status"], "current")
            self.assertEqual(inspection["warnings"], [])

    def test_malformed_old_receipt_is_nonblocking_when_canonical_exists(self):
        with tempfile.TemporaryDirectory() as temporary:
            legacy, canonical = self.make_legacy_output(temporary)
            first = contract.bootstrap_cluster_authoring(legacy)
            first["receipt_path"].write_text("not-json", encoding="utf-8")

            inspection = contract.inspect_cluster_spm_pair(canonical)

            self.assertEqual(inspection["status"], "current")
            self.assertTrue(inspection["warnings"])

    def test_atomic_copy_rejects_source_change_and_leaves_no_target(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            source = folder / "source.spm"
            target = folder / "target.spm"
            source.write_bytes(b"before")
            expected = contract._snapshot(source, required=True)
            real_snapshot = contract._snapshot
            source_snapshot_calls = 0

            def mutate_on_second_source_snapshot(path, *, required=False):
                nonlocal source_snapshot_calls
                if Path(path) == source:
                    source_snapshot_calls += 1
                    if source_snapshot_calls == 2:
                        source.write_bytes(b"changed")
                return real_snapshot(path, required=required)

            with mock.patch.object(
                contract, "_snapshot", side_effect=mutate_on_second_source_snapshot
            ):
                with self.assertRaises(contract.ClusterSpmPairSourceChangedError):
                    contract._atomic_copy(source, target, expected)

            self.assertFalse(target.exists())
            self.assertEqual(list(folder.glob(".*.tmp")), [])

    def test_receipt_failure_rolls_back_new_canonical_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            legacy, canonical = self.make_legacy_output(temporary)
            with mock.patch.object(
                contract, "_atomic_write_json", side_effect=OSError("receipt locked")
            ):
                with self.assertRaises(OSError):
                    contract.bootstrap_cluster_authoring(legacy)

            self.assertFalse(canonical.exists())
            self.assertEqual(legacy.read_bytes(), b"legacy-output")

    def test_existing_lock_blocks_normalization(self):
        with tempfile.TemporaryDirectory() as temporary:
            legacy, canonical = self.make_legacy_output(temporary)
            pair = contract.resolve_cluster_spm_pair(legacy)
            lock = pair["receipt_path"].with_suffix(
                pair["receipt_path"].suffix + ".lock"
            )
            lock.parent.mkdir(parents=True)
            lock.write_text("busy", encoding="utf-8")

            with self.assertRaises(contract.ClusterSpmPairBusyError):
                contract.bootstrap_cluster_authoring(legacy)

            self.assertTrue(lock.exists())
            self.assertFalse(canonical.exists())


if __name__ == "__main__":
    unittest.main()
