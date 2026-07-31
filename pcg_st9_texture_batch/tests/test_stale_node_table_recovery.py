"""Fail-closed tests for the interactive stale Node-table recovery flow."""

import sys
import tempfile
import unittest
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parents[2]
for candidate in (REPO_DIR, REPO_DIR / "pcg_st9_texture_batch"):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from pcg_cluster_assembly_contract import (  # noqa: E402
    _stale_node_table_recovery_contract,
)
from stale_node_table_recovery import (  # noqa: E402
    StaleNodeTableRecoveryTimeout,
    recover_stale_node_table,
    validate_repaired_snapshot,
    wait_for_valid_resave,
)


BASELINE_SHA = "a" * 64
AFTER_SHA = "b" * 64
SECOND_AFTER_SHA = "c" * 64
TARGET_MESH_IDS = (130, 131, 132, 133)


def snapshot(sha256, *, stale, valid_targets):
    rows = []
    for mesh_id in TARGET_MESH_IDS:
        rows.append({
            "mesh_id": str(mesh_id),
            "graph_visible": True,
            "generated_node_count": 1 if valid_targets else 0,
            "export_participates": bool(valid_targets),
            "export_evidence": (
                "node_table" if valid_targets else "node_table_stale"
            ),
            "node_table_stale": bool(stale),
        })
    return {
        "contract": "speedtree_live_generator_delivery_snapshot_v1",
        "spm_text_sha256": sha256,
        "leaf_generator_bindings": rows,
        "node_table": {
            "generator_count": 4,
            "node_table_generator_count": 4,
            "orphan_generator_guids": ["orphan"] if stale else [],
            "orphan_node_count": 1 if stale else 0,
            "total_node_count": 4,
            "stale": bool(stale),
        },
    }


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class SnapshotValidationTests(unittest.TestCase):
    def test_current_node_table_and_all_target_bindings_are_required(self):
        valid = validate_repaired_snapshot(
            snapshot(AFTER_SHA, stale=False, valid_targets=True),
            TARGET_MESH_IDS,
        )
        self.assertTrue(valid["valid"])
        self.assertEqual(valid["errors"], [])
        self.assertEqual(
            valid["live_export_participating_target_mesh_ids"],
            list(TARGET_MESH_IDS),
        )

        stale = validate_repaired_snapshot(
            snapshot(AFTER_SHA, stale=True, valid_targets=False),
            TARGET_MESH_IDS,
        )
        self.assertFalse(stale["valid"])
        self.assertIn("node_table_still_stale", stale["errors"])
        self.assertIn("live_target_mesh_set_incomplete", stale["errors"])

    def test_contract_explicitly_forbids_automatic_save_and_ui_input(self):
        contract = _stale_node_table_recovery_contract()
        self.assertEqual(contract["mode"], "interactive_modeler_save_watch")
        self.assertFalse(contract["modeler_auto_save"])
        self.assertFalse(contract["direct_spm_xml_edit"])
        self.assertFalse(contract["ui_input_simulation"])
        self.assertTrue(contract["requires_user_save"])
        self.assertTrue(contract["automatic_reaudit"])
        self.assertTrue(contract["retry_only_after_valid_reaudit"])


class SaveWatcherTests(unittest.TestCase):
    def test_changed_hash_must_be_stable_and_valid(self):
        clock = FakeClock()
        snapshots = iter([
            snapshot(AFTER_SHA, stale=False, valid_targets=True),
            snapshot(AFTER_SHA, stale=False, valid_targets=True),
        ])
        result, verdict = wait_for_valid_resave(
            "model.spm",
            BASELINE_SHA,
            TARGET_MESH_IDS,
            timeout=10,
            poll_interval=1,
            stable_reads=2,
            snapshot_fn=lambda _path: next(snapshots),
            sleep_fn=clock.sleep,
            monotonic_fn=clock.monotonic,
        )
        self.assertEqual(result["spm_text_sha256"], AFTER_SHA)
        self.assertTrue(verdict["valid"])

    def test_invalid_changed_save_does_not_trigger_success(self):
        clock = FakeClock()

        def unchanged(_path):
            return snapshot(BASELINE_SHA, stale=True, valid_targets=False)

        with self.assertRaisesRegex(
            StaleNodeTableRecoveryTimeout, "file_content_not_changed"
        ):
            wait_for_valid_resave(
                "model.spm",
                BASELINE_SHA,
                TARGET_MESH_IDS,
                timeout=3,
                poll_interval=1,
                stable_reads=2,
                snapshot_fn=unchanged,
                sleep_fn=clock.sleep,
                monotonic_fn=clock.monotonic,
            )

    def test_later_valid_save_can_replace_an_invalid_changed_save(self):
        clock = FakeClock()
        snapshots = iter([
            snapshot(AFTER_SHA, stale=True, valid_targets=False),
            snapshot(AFTER_SHA, stale=True, valid_targets=False),
            snapshot(SECOND_AFTER_SHA, stale=False, valid_targets=True),
            snapshot(SECOND_AFTER_SHA, stale=False, valid_targets=True),
        ])
        result, verdict = wait_for_valid_resave(
            "model.spm",
            BASELINE_SHA,
            TARGET_MESH_IDS,
            timeout=10,
            poll_interval=1,
            stable_reads=2,
            snapshot_fn=lambda _path: next(snapshots),
            sleep_fn=clock.sleep,
            monotonic_fn=clock.monotonic,
        )
        self.assertEqual(result["spm_text_sha256"], SECOND_AFTER_SHA)
        self.assertTrue(verdict["valid"])


class RecoveryOrchestrationTests(unittest.TestCase):
    def test_retry_runs_only_after_changed_stable_valid_reaudit(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            spm = folder / "model.spm"
            executable = folder / "SpeedTree_Modeler.exe"
            spm.write_text("original bytes stay owned by Modeler", encoding="utf-8")
            executable.write_bytes(b"stub")
            original_bytes = spm.read_bytes()
            snapshots = iter([
                snapshot(BASELINE_SHA, stale=True, valid_targets=False),
                snapshot(AFTER_SHA, stale=False, valid_targets=True),
                snapshot(AFTER_SHA, stale=False, valid_targets=True),
            ])
            launched = []
            retried = []
            clock = FakeClock()

            result = recover_stale_node_table(
                spm,
                executable,
                TARGET_MESH_IDS,
                timeout=10,
                poll_interval=1,
                stable_reads=2,
                snapshot_fn=lambda _path: next(snapshots),
                launch_fn=lambda exe, path: launched.append((exe, path)) or 1234,
                retry=lambda after: retried.append(after) or {"status": "retried"},
                sleep_fn=clock.sleep,
                monotonic_fn=clock.monotonic,
            )

            self.assertEqual(result["status"], "repaired_reaudited_and_retried")
            self.assertTrue(result["modeler_launched"])
            self.assertTrue(result["retry_invoked"])
            self.assertEqual(len(launched), 1)
            self.assertEqual(len(retried), 1)
            self.assertEqual(spm.read_bytes(), original_bytes)

    def test_already_repaired_file_is_read_only_and_does_not_launch(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            spm = folder / "model.spm"
            executable = folder / "SpeedTree_Modeler.exe"
            spm.write_text("already repaired", encoding="utf-8")
            executable.write_bytes(b"stub")
            launched = []

            result = recover_stale_node_table(
                spm,
                executable,
                TARGET_MESH_IDS,
                snapshot_fn=lambda _path: snapshot(
                    AFTER_SHA, stale=False, valid_targets=True
                ),
                launch_fn=lambda exe, path: launched.append((exe, path)),
            )

            self.assertEqual(result["status"], "already_repaired")
            self.assertFalse(result["modeler_launched"])
            self.assertEqual(launched, [])


if __name__ == "__main__":
    unittest.main()
