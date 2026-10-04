"""Boundaries for opt-in row settings and post-original-Push completion."""
from __future__ import annotations

import copy
import json
import queue
import sys
import tempfile
import threading
import types
import unittest
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path
from unittest import mock


SK_BATCH = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SK_BATCH))


def load_gui():
    loader = SourceFileLoader("debris_prefab_gui_test", str(SK_BATCH / "sk_batch_gui.pyw"))
    module = module_from_spec(spec_from_loader(loader.name, loader))
    loader.exec_module(module)
    return module


GUI = load_gui()


class RowPrefabTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "Ground_cover_A.spm"
        self.other = self.root / "Other.spm"
        self.report = self.root / "original.json"
        self.report.write_text(json.dumps({"status": "imported_ok", "assets": []}), encoding="utf-8")
        self.app = GUI.App.__new__(GUI.App)
        self.app.items = {
            str(self.source): {"spm": self.source, "debris_terrain_prefab": True, "wind_override": "TREE"},
            str(self.other): {"spm": self.other, "debris_terrain_prefab": False},
        }
        self.app.cfg = {
            "blender_exe": "blender.exe", "unreal_project": "Project.uproject",
            "send2ue_dir": "send2ue", "push_transport": "rpc",
            "debris_terrain_prefab_spms": [str(self.source)],
        }
        self.app.state = {
            str(self.source): {
                "push_status_kind": "imported_ok", "push_status": "완료",
                "push_paths": {"report": str(self.report), "manifest": "original_manifest.json"},
                "push_import_fingerprint": "original-fingerprint",
            },
            str(self.other): {"push_status_kind": "imported_ok"},
        }
        self.app.state_lock = threading.RLock()
        self.app.stop_flag = threading.Event()
        self.app.ui_queue = queue.Queue()
        self.app.log = mock.Mock()
        self.app._push_material_contract = mock.Mock(return_value=self.root / "materials.json")
        self.app._retry_transition = mock.Mock()
        self.app._bind_failure_record = mock.Mock(return_value={})
        self.app._phase_failed_items = set()
        self.app._failed_retry_planning_context = mock.Mock(return_value=None)
        self.job = {
            "mode": "pipeline", "terminal_phase": "push", "push_transport": "rpc",
            "cfg": copy.deepcopy(self.app.cfg),
            "targets": copy.deepcopy(list(self.app.items.values())),
        }
        self.runner = mock.Mock(return_value={"status": "ok", "changelist": 42})
        pipeline = types.ModuleType("sk_batch.debris_prefab_pipeline")
        pipeline.run_pipeline = self.runner
        self.addCleanup(mock.patch.stopall)
        mock.patch.dict(sys.modules, {"sk_batch.debris_prefab_pipeline": pipeline}).start()
        mock.patch.object(GUI, "save_state").start()

    def test_config_retains_path_selection_after_restart(self):
        config_path = self.root / "config.json"
        with mock.patch.dict(GUI.load_config.__globals__, {"CONFIG_PATH": config_path}):
            GUI.save_config({**GUI.load_config(), "debris_terrain_prefab_spms": [str(self.source)]})
            loaded = GUI.load_config()
        self.assertEqual(loaded["debris_terrain_prefab_spms"], [str(self.source)])
        self.assertNotIn("debris_terrain_prefab", loaded)

    def test_collect_cfg_preserves_unscanned_choices_and_freezes_current_rows(self):
        hidden = self.root / "hidden" / "Elsewhere.spm"
        self.app.cfg["debris_terrain_prefab_spms"] += [str(hidden), str(self.other)]
        captured = self.app._collect_cfg()
        self.assertEqual(set(captured["debris_terrain_prefab_spms"]), {str(hidden), str(self.source)})
        self.app.items[str(self.source)]["debris_terrain_prefab"] = False
        self.assertIn(str(self.source), captured["debris_terrain_prefab_spms"])

    def test_row_toggle_is_independent_of_wind_and_execution_selection(self):
        item = self.app.items[str(self.source)]
        item["checked"] = True
        self.app.tree = mock.Mock()
        self.app.tree.identify_region.return_value = "cell"
        self.app.tree.identify_row.return_value = str(self.source)
        self.app.tree.identify_column.return_value = "#5"
        self.app.cell_editor = None
        self.app.active_batch_job = self.job
        with mock.patch.object(GUI, "save_config"):
            result = self.app._on_click(types.SimpleNamespace(x=1, y=1))
        self.assertEqual(result, "break")
        self.assertFalse(item["debris_terrain_prefab"])
        self.assertTrue(item["checked"])
        self.assertEqual(item["wind_override"], "TREE")
        self.assertIn(str(self.source), self.job["cfg"]["debris_terrain_prefab_spms"])

    def test_scan_retains_legacy_status_and_folder_indices_with_group_action_appended(self):
        source = self.root / "SK_Any_Test.spm"
        iid = str(source)
        self.app.root_var = types.SimpleNamespace(get=lambda: str(self.root))
        self.app.tree = mock.Mock()
        self.app.tree.get_children.return_value = ()
        self.app.checked_rows = mock.Mock()
        self.app.cfg["debris_terrain_prefab_spms"] = [iid]
        self.app.state = {iid: {
            "spm_status": "Source ready", "blend_status": "Latest",
            "live_status_signature": [0], "push_status": "Original ready",
            "wind_override": "TREE",
        }}
        self.app.scan(prepared={"spms": [source], "cluster_sources": []}, generation=0)
        rows = {call.kwargs["iid"]: call.kwargs["values"]
                for call in self.app.tree.insert.call_args_list}
        values = rows[iid]
        self.assertEqual(values[:5], (
            self.app._wind_label(iid), "Source ready", "Latest", "Original ready",
            str(self.root),
        ))
        self.assertEqual(values[5], self.app._terrain_group_label(iid))
        folder_values = next(value for key, value in rows.items() if key != iid)
        self.assertEqual(folder_values, ("", "", "", "", str(self.root), ""))

    def test_only_captured_selected_original_success_runs_without_name_or_wind_gate(self):
        # A subsequent UI edit must not alter the captured request.
        self.app.items[str(self.source)]["debris_terrain_prefab"] = False
        self.app.cfg["debris_terrain_prefab_spms"] = []
        self.assertEqual(self.app._run_optional_debris_prefabs(self.job), set())
        self.runner.assert_called_once()
        self.assertEqual(self.runner.call_args.args[0], self.source)
        self.assertEqual(self.runner.call_args.kwargs["source_push_report"], self.report)
        self.assertEqual(self.app.state[str(self.source)]["push_import_fingerprint"], "original-fingerprint")
        self.assertEqual(self.job["debris_prefab_changelist"], 42)

    def test_off_is_noop_including_material_discovery(self):
        self.job["cfg"]["debris_terrain_prefab_spms"] = []
        before = copy.deepcopy(self.app.state)
        self.assertFalse(self.app._run_optional_debris_prefabs(self.job))
        self.runner.assert_not_called()
        self.app._push_material_contract.assert_not_called()
        self.assertEqual(self.app.state, before)

    def test_no_group_step_before_original_import_or_for_assembly(self):
        for original_status in ("exported_pending_unreal", "data_error", "importing"):
            self.app.state[str(self.source)]["push_status_kind"] = original_status
            self.app._run_optional_debris_prefabs(self.job)
        self.app.state[str(self.source)]["push_status_kind"] = "imported_ok"
        self.job["terminal_phase"] = "blender"
        self.app._run_optional_debris_prefabs(self.job)
        self.runner.assert_not_called()

    def test_recovery_waiting_retry_and_import_cache_all_finish_optional_stage(self):
        for mode in ("unreal_recovery", "waiting_import", "failed_retry_repair", "phase"):
            with self.subTest(mode=mode):
                self.runner.reset_mock()
                self.job.update(mode=mode, phase="push")
                self.app._run_optional_debris_prefabs(self.job)
                self.runner.assert_called_once()
                expected_transport = "headless" if mode == "waiting_import" else "rpc"
                self.assertEqual(self.runner.call_args.kwargs["transport"], expected_transport)

    def test_pipeline_failure_preserves_original_receipt_and_other_authoritative_outcome(self):
        self.runner.side_effect = RuntimeError("native geometry gate failed")
        original_paths = copy.deepcopy(self.app.state[str(self.source)]["push_paths"])
        self.app._phase_result_summary = {
            "selected_count": 2,
            "target_outcomes": [
                {"target": str(self.source), "outcome": "completed"},
                {"target": str(self.other), "outcome": "planned_excluded", "reason_token": "existing-rule"},
            ],
        }
        failed = self.app._run_optional_debris_prefabs(self.job)
        self.assertEqual(failed, {str(self.source)})
        entry = self.app.state[str(self.source)]
        self.assertEqual(entry["push_paths"], original_paths)
        self.assertEqual(entry["push_import_fingerprint"], "original-fingerprint")
        self.assertEqual(entry["debris_prefab_pipeline"]["original_push"]["paths"], original_paths)
        self.assertEqual(entry["push_status_kind"], "debris_prefab_error")
        summary = self.app._phase_result_summary
        self.assertEqual(summary["failed_count"], 1)
        self.assertEqual(summary["blocked_count"], 1)
        self.assertEqual(summary["target_outcomes"][1]["reason_token"], "existing-rule")

    def test_authoritatively_failed_original_is_not_reused(self):
        self.app._phase_result_summary = {
            "target_outcomes": [{"target": str(self.source), "outcome": "failed"}]
        }
        self.app._run_optional_debris_prefabs(self.job)
        self.runner.assert_not_called()

    def test_skipped_stage_keeps_original_success_and_clears_running_label(self):
        self.runner.return_value = {"status": "skipped", "reason": "unsupported_source"}
        self.app._run_optional_debris_prefabs(self.job)
        entry = self.app.state[str(self.source)]
        self.assertEqual(entry["push_status_kind"], "imported_ok")
        self.assertIn("건너뜀", entry["push_status"])
        self.assertEqual(entry["debris_prefab_pipeline"]["status"], "skipped")

    def test_cancellation_before_stage_does_not_start_worker(self):
        self.app.stop_flag.set()
        self.app._run_optional_debris_prefabs(self.job)
        self.runner.assert_not_called()
        self.assertEqual(self.app.state[str(self.source)]["push_status_kind"], "cancelled")
        self.assertEqual(self.app.state[str(self.source)]["debris_prefab_pipeline"]["original_push"]["status"], "imported_ok")

    def test_one_changelist_is_reused_across_selected_batch_targets(self):
        self.job["cfg"]["debris_terrain_prefab_spms"].append(str(self.other))
        self.app._run_optional_debris_prefabs(self.job)
        self.assertEqual([call.kwargs["changelist"] for call in self.runner.call_args_list], [None, 42])


if __name__ == "__main__":
    unittest.main()
