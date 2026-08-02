import queue
import sys
import tempfile
import threading
import unittest
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path
from unittest import mock


SK_BATCH_DIR = Path(__file__).resolve().parents[1]
REPO_DIR = SK_BATCH_DIR.parent
sys.path.insert(0, str(SK_BATCH_DIR))
sys.path.insert(0, str(REPO_DIR))


def load_gui_module():
    path = SK_BATCH_DIR / "sk_batch_gui.pyw"
    loader = SourceFileLoader("sk_batch_failed_retry_orchestration_test", str(path))
    spec = spec_from_loader(loader.name, loader)
    module = module_from_spec(spec)
    loader.exec_module(module)
    return module


class Lease:
    finished = False

    @staticmethod
    def renew_and_check_current():
        return True


class FailedRetryOrchestrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.first = self.root / "SK_first.spm"
        self.second = self.root / "SK_second.spm"
        self.first.write_bytes(b"first")
        self.second.write_bytes(b"second")

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def app(gui):
        app = gui.App.__new__(gui.App)
        app.stop_flag = threading.Event()
        app.ui_queue = queue.Queue()
        app.state = {}
        app.state_lock = threading.RLock()
        app.log = mock.Mock()
        app._job_check = mock.Mock()
        app._execute_material_preflight = mock.Mock(return_value={
            "code": 0,
            "result": {"status": "ok"},
            "report": "fresh_material_preflight.json",
        })
        app._cluster_normalization_stage_observation = mock.Mock(return_value={
            "status": "normalized",
        })
        app._failed_retry_repair_state = mock.Mock(return_value={
            "current": False,
            "kind": "stale_content",
            "reason": "Blender rebuild follows BAT repair",
        })
        return app

    @staticmethod
    def plan(path, request_id):
        return {
            "schema_version": 1,
            "request_id": request_id,
            "parent_retry_id": "parent-118",
            "exact_spm": str(path),
            "evidence_sha256": "a" * 64,
            "reason_codes": ["managed_texture_set_incomplete"],
            "stages": [{
                "stage": "pcg_texture",
                "tool": "pcg_st9_texture_batch",
                "repair_action": "step3-standard",
                "target_spms": [str(path)],
            }],
            "supported": True,
            "initial_status": "automatic_repair_pending",
        }

    @staticmethod
    def generator_cluster_plan(path, provider, request_id):
        return {
            "schema_version": 1,
            "request_id": request_id,
            "parent_retry_id": "parent-125",
            "exact_spm": str(path),
            "evidence_sha256": "b" * 64,
            "reason_codes": ["generator_connection_contract_incomplete"],
            "stages": [{
                "stage": "generator_sync_and_cluster",
                "tool": "spm_generator_sync",
                "repair_action": "generator-sync-and-cluster",
                "target_spms": [str(path), str(provider)],
            }],
            "supported": True,
            "initial_status": "automatic_repair_pending",
        }

    def test_success_transitions_through_reaudit_and_pipeline(self):
        gui = load_gui_module()
        app = self.app(gui)
        target = {"spm": self.first}
        app._run_full_pipeline = mock.Mock(side_effect=lambda *_args, **_kwargs: setattr(
            app,
            "_phase_result_summary",
            {
                "target_outcomes": [{
                    "target": str(self.first),
                    "target_name": self.first.name,
                    "outcome": "completed",
                    "reason_token": None,
                    "evidence": {},
                }],
                "completed_count": 1,
                "blocked_count": 0,
                "failed_count": 0,
            },
        ))
        job = {
            "targets": [target],
            "repair_plans": [self.plan(self.first, "one")],
        }
        with mock.patch.object(gui, "save_state"), mock.patch.object(
            gui,
            "run_exact_target_request",
            return_value={"status": "completed", "result": {}},
        ) as run:
            ok = app._run_failed_retry_repair_job(job, Lease())

        self.assertTrue(ok)
        run.assert_called_once()
        app._job_check.assert_called_once_with(str(self.first), self.first)
        app._run_full_pipeline.assert_called_once()
        automation = app.state[str(self.first)]["failed_retry_automation"]
        self.assertEqual(automation["status"], "automatic_repair_completed")
        self.assertEqual(app._phase_result_summary["completed_count"], 1)

    def test_live_atlas_conflict_enters_structured_repair_evidence(self):
        gui = load_gui_module()
        app = self.app(gui)
        app._failed_retry_repair_state = mock.Mock(return_value={
            "current": False,
            "push_ready": False,
            "kind": "material",
            "reason": "current Atlas conflict",
        })
        app.state[str(self.first)] = {
            "blend_status_kind": "data_error",
            "blend_status_error": {
                "kind": "data_error",
                "message": "sanitized prior preflight failure",
            },
        }
        repair = {
            "status": "repairable",
            "reason_code": "atlas_manifest_mirror_conflict_repairable",
            "target_spm": str(self.first),
            "authority": str(self.root / "authority.json"),
            "mirrors": [str(self.root / "stale.json")],
        }

        with mock.patch.object(
            gui, "atlas_manifest_mirror_repair_plan", return_value=repair
        ):
            evidence = app._failed_retry_durable_evidence(
                str(self.first),
                repair_state=app._failed_retry_repair_state(str(self.first)),
            )

        self.assertEqual(evidence["current_atlas_manifest_repair"], repair)
        self.assertTrue(gui.has_repair_contract_evidence(evidence))
        plan = gui.build_exact_target_repair_plan(
            self.first,
            evidence,
            inventory_paths=[self.first, self.second],
            parent_retry_id="parent-atlas",
            request_id="request-atlas",
        )
        self.assertTrue(plan.supported)
        self.assertEqual(
            plan.stages[0]["repair_action"],
            "atlas-manifest-mirror-repair",
        )

    def test_generator_cluster_reaudit_uses_only_evidence_provider(self):
        gui = load_gui_module()
        app = self.app(gui)
        target = {"spm": self.first}
        app._run_full_pipeline = mock.Mock(side_effect=lambda *_args, **_kwargs: setattr(
            app,
            "_phase_result_summary",
            {
                "target_outcomes": [{
                    "target": str(self.first),
                    "target_name": self.first.name,
                    "outcome": "ready",
                    "reason_token": None,
                    "evidence": {},
                }],
                "completed_count": 1,
                "blocked_count": 0,
                "failed_count": 0,
            },
        ))
        job = {
            "targets": [target],
            "repair_plans": [self.generator_cluster_plan(
                self.first,
                self.second,
                "generator-cluster",
            )],
        }

        def complete_with_unit_progress(*_args, **kwargs):
            callback = kwargs["on_progress"]
            callback({
                "current_stage": "Generator Sync 중",
                "unit_stage": "generator_sync",
            })
            callback({
                "current_stage": "Cluster 갱신 중",
                "unit_stage": "cluster_refresh",
            })
            return {"status": "completed", "result": {}}

        with mock.patch.object(gui, "save_state"), mock.patch.object(
            gui,
            "run_exact_target_request",
            side_effect=complete_with_unit_progress,
        ):
            ok = app._run_failed_retry_repair_job(job, Lease())

        self.assertTrue(ok)
        app._cluster_normalization_stage_observation.assert_called_once()
        args = app._cluster_normalization_stage_observation.call_args.args
        self.assertEqual(args[0], self.first)
        self.assertEqual(args[2], self.second)
        self.assertTrue(
            app._cluster_normalization_stage_observation.call_args.kwargs[
                "require_normalized"
            ]
        )
        status_cells = [
            payload[2]
            for kind, payload in list(app.ui_queue.queue)
            if kind == "cell" and payload[1] == "push_status"
        ]
        self.assertIn("Generator Sync 중", status_cells)
        self.assertIn("Cluster 갱신 중", status_cells)

    def test_partial_stage_failure_does_not_enter_pipeline_or_hide_final_failure(self):
        gui = load_gui_module()
        app = self.app(gui)
        app._run_full_pipeline = mock.Mock()
        job = {
            "targets": [{"spm": self.first}, {"spm": self.second}],
            "repair_plans": [
                self.plan(self.first, "one"),
                self.plan(self.second, "two"),
            ],
        }
        terminals = [
            {"status": "failed", "error": "render failed"},
            {"status": "failed", "error": "queue failed"},
        ]
        with mock.patch.object(gui, "save_state"), mock.patch.object(
            gui, "run_exact_target_request", side_effect=terminals
        ):
            ok = app._run_failed_retry_repair_job(job, Lease())

        self.assertFalse(ok)
        app._run_full_pipeline.assert_not_called()
        self.assertEqual(app._phase_result_summary["failed_count"], 2)
        for path in (self.first, self.second):
            automation = app.state[str(path)]["failed_retry_automation"]
            self.assertEqual(automation["status"], "final_failed")
            self.assertIn("reason_codes", app.state[str(path)]["push_status_error"])

    def test_cancel_is_resumable_and_not_promoted_to_final_failed(self):
        gui = load_gui_module()
        app = self.app(gui)
        app.stop_flag.set()
        app._run_full_pipeline = mock.Mock()
        job = {
            "targets": [{"spm": self.first}],
            "repair_plans": [self.plan(self.first, "cancel")],
        }
        with mock.patch.object(gui, "save_state"), mock.patch.object(
            gui, "run_exact_target_request"
        ) as run:
            ok = app._run_failed_retry_repair_job(job, Lease())

        self.assertFalse(ok)
        run.assert_not_called()
        self.assertEqual(
            app.state[str(self.first)]["failed_retry_automation"]["status"],
            "cancelled",
        )
        self.assertNotEqual(
            app.state[str(self.first)]["push_status_kind"],
            "automatic_repair_failed",
        )

    def test_downstream_cancel_is_resumable_and_filtered_from_failure(self):
        gui = load_gui_module()
        app = self.app(gui)

        def cancel_pipeline(*_args, **_kwargs):
            app.stop_flag.set()
            app._phase_result_summary = {
                "target_outcomes": [{
                    "target": str(self.first),
                    "target_name": self.first.name,
                    "outcome": "cancelled",
                    "reason_token": "operator_stopped",
                    "evidence": {},
                }],
                "completed_count": 0,
                "blocked_count": 0,
                "failed_count": 0,
            }

        app._run_full_pipeline = mock.Mock(side_effect=cancel_pipeline)
        job = {
            "targets": [{"spm": self.first}],
            "repair_plans": [self.plan(self.first, "downstream-cancel")],
        }
        with mock.patch.object(gui, "save_state"), mock.patch.object(
            gui,
            "run_exact_target_request",
            return_value={"status": "completed", "result": {}},
        ):
            ok = app._run_failed_retry_repair_job(job, Lease())

        self.assertFalse(ok)
        automation = app.state[str(self.first)]["failed_retry_automation"]
        self.assertEqual(automation["status"], "cancelled")
        self.assertEqual(app._phase_result_summary["failed_count"], 0)
        self.assertEqual(app._phase_result_summary["cancelled_count"], 1)

    def test_downstream_pending_unreal_is_waiting_not_final_failure(self):
        gui = load_gui_module()
        app = self.app(gui)
        app._run_full_pipeline = mock.Mock(side_effect=lambda *_args, **_kwargs: setattr(
            app,
            "_phase_result_summary",
            {
                "target_outcomes": [{
                    "target": str(self.first),
                    "target_name": self.first.name,
                    "outcome": "pending_unreal",
                    "reason_token": "exported_pending_unreal",
                    "evidence": {},
                }],
                "completed_count": 0,
                "pending_count": 1,
                "blocked_count": 0,
                "failed_count": 0,
            },
        ))
        job = {
            "targets": [{"spm": self.first}],
            "repair_plans": [self.plan(self.first, "downstream-wait")],
        }
        with mock.patch.object(gui, "save_state"), mock.patch.object(
            gui,
            "run_exact_target_request",
            return_value={
                "status": "completed",
                "terminal_status": "completed",
                "result": {},
            },
        ):
            ok = app._run_failed_retry_repair_job(job, Lease())

        self.assertTrue(ok)
        automation = app.state[str(self.first)]["failed_retry_automation"]
        self.assertEqual(automation["status"], "blender_unreal_retry_running")
        self.assertEqual(app._phase_result_summary["pending_count"], 1)
        self.assertEqual(app._phase_result_summary["failed_count"], 0)

    def test_normalized_current_outcomes_do_not_reuse_stale_failure_evidence(self):
        gui = load_gui_module()
        for normalized, disposition in (
            ("completed", "current_success"),
            ("pending_unreal", "current_wait"),
            ("cancelled", "current_cancelled"),
        ):
            with self.subTest(normalized=normalized):
                app = self.app(gui)
                app._target_authoritative_result = mock.Mock(return_value={
                    "outcome": normalized,
                })
                app.state[str(self.first)] = {
                    "push_status_kind": "legacy raw state is #107-owned",
                    "push_status_error": {
                        "kind": "data_error",
                        "reason_code": "managed_texture_set_incomplete",
                        "time": "2026-07-01T00:00:00",
                    },
                }
                evidence = app._failed_retry_durable_evidence(
                    str(self.first),
                    {"current": True, "kind": "ready", "reason": "current"},
                )
                self.assertEqual(
                    evidence["terminal_disposition"],
                    disposition,
                )
                self.assertEqual(evidence["current_phase_errors"], {})
                self.assertEqual(
                    evidence["selected_failure"]["reason_token"],
                    disposition,
                )

    def test_exact_repair_plan_is_returned_then_committed_on_ui_thread(self):
        gui = load_gui_module()
        app = self.app(gui)
        iid = str(self.first)
        app._failed_retry_durable_evidence = mock.Mock(return_value={
            "reason_code": "managed_texture_set_incomplete",
            "canonical_spm": iid,
        })
        app._snapshot_batch_request = mock.Mock(return_value=(
            {iid: {"spm": self.first}},
            [{"spm": self.first}],
        ))
        app._enqueue_batch_job = mock.Mock(return_value=1)
        app._retry_planning_workers = set()
        app._ui_thread_ident = threading.get_ident()
        app.active_batch_job = None
        app.pending_batch_jobs = gui.deque()
        app._set_batch_queue_controls = mock.Mock()
        tracker = mock.Mock()
        tracker.run_id = "exact-repair-progress"
        tracker.path = self.root / "retry-progress.json"
        tracker.claim_planning_commit.side_effect = [True, False]
        repair_plan = mock.Mock()
        repair_plan.supported = True
        repair_plan.metadata.return_value = self.plan(self.first, "planned")

        with mock.patch.object(gui, "save_state"), mock.patch.object(
            gui,
            "has_repair_contract_evidence",
            return_value=True,
        ), mock.patch.object(
            gui,
            "build_exact_target_repair_plan",
            return_value=repair_plan,
        ):
            built = app._build_failed_retry_plan(
                [iid],
                {"push_transport": "rpc"},
                tracker=tracker,
            )

        app._enqueue_batch_job.assert_not_called()
        self.assertEqual(len(built["jobs"]), 1)
        job = built["jobs"][0]
        self.assertEqual(job["mode"], "failed_retry_repair")
        self.assertEqual(job["targets"], [{"spm": self.first}])
        self.assertEqual(
            job["retry_metadata"]["partition"],
            "exact_bat_repair",
        )
        self.assertIs(job["_retry_progress_tracker"], tracker)
        tracker.assign_partition.assert_called_once_with(
            "exact_bat_repair",
            [iid],
            "exact_bat_then_fresh_reaudit_then_blender_unreal",
        )

        with mock.patch.object(gui, "save_config"):
            committed = app._commit_failed_retry_plan(built)
            duplicate = app._commit_failed_retry_plan(built)

        self.assertEqual(committed, [1])
        self.assertIsNone(duplicate)
        app._enqueue_batch_job.assert_called_once_with(job)
        tracker.complete_planning_commit.assert_called_once_with()

    def test_planning_cancel_wins_before_commit_without_enqueue(self):
        gui = load_gui_module()
        app = self.app(gui)
        app._ui_thread_ident = threading.get_ident()
        app.active_batch_job = None
        app.pending_batch_jobs = gui.deque()
        app._retry_planning_workers = set()
        app._set_batch_queue_controls = mock.Mock()
        app._enqueue_batch_job = mock.Mock()
        app.stop_flag.set()
        tracker = mock.Mock()
        plan = {
            "tracker": tracker,
            "selected_iids": [str(self.first)],
            "cfg": {},
            "jobs": [{"label": "must not enqueue"}],
        }

        committed = app._commit_failed_retry_plan(plan)

        self.assertIsNone(committed)
        app._enqueue_batch_job.assert_not_called()
        tracker.finish_planning.assert_called_once_with(
            gui.RETRY_STAGE_CANCELLED,
            "operator cancelled during retry planning",
        )
        tracker.claim_planning_commit.assert_not_called()

    def test_invalid_exact_identity_is_final_fail_closed_not_legacy_retry(self):
        gui = load_gui_module()
        app = self.app(gui)
        missing = self.root / "missing exact.spm"
        app.items = {str(missing): {}}
        app._close_cell_editor = mock.Mock()
        app._collect_cfg = mock.Mock(return_value={})
        app._set_batch_queue_controls = mock.Mock()
        app.active_batch_job = None
        app.pending_batch_jobs = gui.deque()
        app._enqueue_batch_job = mock.Mock()
        app._failed_retry_repair_state = mock.Mock(return_value={
            "current": False,
            "kind": "stale_content",
            "reason": "repair evidence",
        })
        app._failed_retry_durable_evidence = mock.Mock(return_value={
            "reason_code": "managed_texture_set_incomplete",
            "canonical_spm": str(missing),
        })

        with mock.patch.object(gui, "save_state"), mock.patch.object(
            gui, "save_config"
        ), mock.patch.object(
            gui.messagebox,
            "showinfo",
        ):
            app.start_failed_results_retry()

        app._enqueue_batch_job.assert_not_called()
        entry = app.state[str(missing)]
        self.assertEqual(
            entry["failed_retry_automation"]["status"],
            "final_failed",
        )
        self.assertEqual(entry["push_status_kind"], "automatic_repair_failed")
        self.assertIn(
            "managed_texture_set_incomplete",
            entry["push_status_error"]["reason_codes"],
        )


if __name__ == "__main__":
    unittest.main()
