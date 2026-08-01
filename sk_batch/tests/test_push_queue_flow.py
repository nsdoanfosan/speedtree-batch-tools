import ast
import queue
import json
import sys
import tempfile
import threading
import unittest
from collections import deque
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path
from unittest import mock


SK_BATCH_DIR = Path(__file__).resolve().parents[1]
FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures"
sys.path.insert(0, str(SK_BATCH_DIR))


def load_gui_module():
    path = SK_BATCH_DIR / "sk_batch_gui.pyw"
    loader = SourceFileLoader("sk_batch_gui_queue_test", str(path))
    spec = spec_from_loader(loader.name, loader)
    module = module_from_spec(spec)
    loader.exec_module(module)
    return module


class PushQueueFlowTests(unittest.TestCase):
    def make_app(self, gui):
        app = gui.App.__new__(gui.App)
        app.stop_flag = threading.Event()
        app.ui_queue = queue.Queue()
        app.state = {}
        app.state_lock = threading.RLock()
        app.procs_lock = threading.Lock()
        app.active_procs = set()
        app.cfg = {}
        app.log = mock.Mock()
        return app

    def test_queue_snapshot_drops_ineligible_backup_board_row(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        live = Path("Weed_reed") / "SK_weed_reed_02.spm"
        rollback = (
            Path("Weed_reed")
            / "SK_weed_reed_02.texture_slot_backup_20260801_010203_123456.spm"
        )
        app.items = {
            str(live): {"spm": live, "checked": True},
            str(rollback): {"spm": rollback, "checked": True},
        }

        inventory, targets = app._snapshot_batch_request(
            [str(rollback), str(live)]
        )

        self.assertEqual(set(inventory), {str(live)})
        self.assertEqual([item["spm"] for item in targets], [live])

    @staticmethod
    def issue16_fixture():
        return json.loads(
            (FIXTURE_DIR / "issue16_blackgum_source_run.json").read_text(
                encoding="utf-8"
            )
        )

    def test_issue16_captured_live_delivery_is_four_runnable_one_blocked(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        fixture = self.issue16_fixture()
        provider = Path("blackgum") / "cluster" / "SK_cluster_blackgum_01.spm"
        runnable = []
        blocked = []

        for row in fixture["targets"]:
            target = Path("blackgum") / row["target"]
            blocked_targets = []
            handoff_errors = []
            if row["delivery_decision"] == "blocked":
                blocked_targets.append({
                    "spm": str(target),
                    "delivery_reason": row["delivery_reason"],
                    "errors": row["delivery_errors"],
                })
                handoff_errors.append({
                    "code": "NORMALIZED_GENERATOR_DELIVERY_INCOMPLETE",
                    "role": "cluster",
                    "spm": str(provider),
                    "delivery_mode": row["delivery_mode"],
                    "errors": row["delivery_errors"],
                })
            contract = {
                "tree_source_identities": [{"spm": str(target)}],
                "dependencies": [{
                    "spm": str(provider),
                    "normalized_delivery_mode": row["delivery_mode"],
                    "normalized_delivery_blocked": bool(blocked_targets),
                    "normalized_variants_required": True,
                    "normalized_variants": {
                        "status": "current",
                        "delivery_mode": row["delivery_mode"],
                        "delivery_errors": row["delivery_errors"],
                        "delivery_blocked_targets": blocked_targets,
                        "variants": [{"name": "Cluster"}],
                    },
                }],
                "handoff": {
                    "status": "blocked" if handoff_errors else "ready",
                    "errors": handoff_errors,
                },
                # Operational Sync completed, but that is not Push readiness.
                "relationship_sync": {"outcome": "completed"},
            }
            raw_audit = {
                "selected_contract": contract,
                "audit_report": Path("captured_live_audit.json"),
                "payload": {"items": [{}]},
            }
            with mock.patch.object(
                app,
                "_refresh_stale_cluster_receipt_uncached",
                return_value=raw_audit,
            ):
                if row["delivery_decision"] == "blocked":
                    with self.assertRaises(
                        gui.TargetPlannedExclusionError
                    ) as caught:
                        app._cluster_normalization_stage_observation(
                            target,
                            "captured",
                            provider,
                            require_normalized=False,
                        )
                    blocked.append(caught.exception.reason_token)
                else:
                    observation = (
                        app._cluster_normalization_stage_observation(
                            target,
                            "captured",
                            provider,
                            require_normalized=False,
                        )
                    )
                    runnable.append(Path(observation["target_spm"]).name)

        self.assertEqual(len(runnable), 4)
        self.assertEqual(blocked, [
            fixture["expected_result"]["blocked_reason_token"]
        ])

    def test_issue16_planned_exclusion_does_not_fan_out_shared_provider(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        fixture = self.issue16_fixture()
        roots = [
            Path("blackgum") / row["target"]
            for row in fixture["targets"]
        ]
        weed = next(path for path in roots if "weed" in path.name.casefold())
        provider = (
            Path("blackgum") / "cluster" / "SK_cluster_blackgum_01.spm"
        )
        provider_item = {
            "spm": provider,
            "checked": False,
            "referenced_by_spms": tuple(roots),
        }
        root_items = [{"spm": path, "checked": True} for path in roots]
        expanded = [provider_item, *root_items]
        dependency_map = {
            str(path): (str(provider),) for path in roots
        }
        calls = []

        def fake_batch(phase, phase_targets, emit_done=False):
            names = [item["spm"].name for item in phase_targets]
            calls.append((phase, names))
            if phase == "blender" and names == [provider.name]:
                def observe(target, *_args, **_kwargs):
                    if target == weed:
                        raise gui.TargetPlannedExclusionError(
                            "current live delivery blocks only the weed target",
                            reason_token=(
                                fixture["expected_result"][
                                    "blocked_reason_token"
                                ]
                            ),
                            target_spm=weed,
                            producer_spm=provider,
                            evidence={
                                "sync_outcome": "completed",
                                "sync_outcome_authoritative": False,
                                "normalization_postcondition": "not_run",
                            },
                        )
                    return {
                        "target_spm": str(target),
                        "live_audit_report": "captured.json",
                        "selected_contract": {"handoff": {"status": "ready"}},
                    }

                with mock.patch.object(
                    app,
                    "_cluster_normalization_stage_observation",
                    side_effect=observe,
                ):
                    runnable, contracts = app._cluster_relation_input_plan(
                        roots,
                        "captured",
                        provider,
                    )
                self.assertEqual(
                    list(runnable),
                    [path for path in roots if path != weed],
                )
                self.assertEqual(len(contracts), 4)
            app._phase_failed_items = set()
            app._phase_abort_reason = None
            return True

        app._run_batch = mock.Mock(side_effect=fake_batch)
        with mock.patch.object(
            gui,
            "expand_blender_repair_targets",
            return_value=(expanded, dependency_map, {str(provider)}),
        ), mock.patch.object(gui, "save_state"):
            result = app._run_full_pipeline(root_items)

        self.assertFalse(result)
        downstream_blender = [
            names for phase, names in calls
            if phase == "blender" and names != [provider.name]
        ]
        self.assertEqual(
            downstream_blender,
            [[path.name for path in roots if path != weed]],
        )
        summary = app._phase_result_summary
        self.assertEqual(
            {
                key: summary[key]
                for key in (
                    "completed_count",
                    "blocked_count",
                    "planned_excluded_count",
                    "dependency_blocked_count",
                    "failed_count",
                )
            },
            {
                key: fixture["expected_result"][key]
                for key in (
                    "completed_count",
                    "blocked_count",
                    "planned_excluded_count",
                    "dependency_blocked_count",
                    "failed_count",
                )
            },
        )
        weed_outcome = next(
            row for row in summary["target_outcomes"]
            if row["target_name"] == weed.name
        )
        self.assertEqual(weed_outcome["outcome"], "planned_excluded")
        self.assertEqual(
            weed_outcome["reason_token"],
            fixture["expected_result"]["blocked_reason_token"],
        )
        self.assertIn("Sync excluded", app.state[str(weed)]["blend_status"])
        self.assertIn("Push blocked", app.state[str(weed)]["push_status"])
        self.assertEqual(
            app.state[str(weed)]["push_status_error"]["reason_token"],
            fixture["expected_result"]["blocked_reason_token"],
        )

    def test_issue16_shared_queue_result_persists_causal_counts_and_token(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        fixture = self.issue16_fixture()
        targets = [
            {"spm": Path("blackgum") / row["target"], "checked": True}
            for row in fixture["targets"]
        ]
        blocked_name = "SK_weed_blackgum_01.spm"
        target_outcomes = [
            {
                "target": str(item["spm"]),
                "target_name": item["spm"].name,
                "outcome": (
                    "planned_excluded"
                    if item["spm"].name == blocked_name
                    else "completed"
                ),
                "reason_token": (
                    fixture["expected_result"]["blocked_reason_token"]
                    if item["spm"].name == blocked_name
                    else None
                ),
                "evidence": (
                    {"sync_outcome_authoritative": False}
                    if item["spm"].name == blocked_name
                    else {}
                ),
            }
            for item in targets
        ]
        expected = {
            "selected_count": 5,
            "completed_count": 4,
            "blocked_count": 1,
            "planned_excluded_count": 1,
            "dependency_blocked_count": 0,
            "failed_count": 0,
            "target_outcomes": target_outcomes,
            "shared_failures": [],
        }

        class Lease:
            def __init__(self):
                self.finished = False
                self.result = None
                self.success = None

            def finish(self, success=True, result=None):
                self.finished = True
                self.success = success
                self.result = result

        lease = Lease()
        app.shared_queue_runtime = mock.Mock(
            wait_for_turn=mock.Mock(return_value=lease)
        )

        def run_pipeline(*_args, **_kwargs):
            app._phase_result_summary = expected
            app._phase_failed_items = {
                str(item["spm"])
                for item in targets
                if item["spm"].name == blocked_name
            }
            return False

        app._run_full_pipeline = mock.Mock(side_effect=run_pipeline)
        job = {
            "id": 16,
            "label": "Issue 16 replay",
            "mode": "pipeline",
            "terminal_phase": "push",
            "selected_scope": True,
            "targets": targets,
            "shared_queue_job_id": "source-run-replay",
        }
        with mock.patch.object(
            app,
            "_freeze_batch_production_source_manifest",
        ):
            app._run_queued_batch_job(job)

        self.assertFalse(lease.success)
        self.assertEqual(lease.result["outcome"], "partial")
        for key, value in fixture["expected_result"].items():
            if key == "blocked_reason_token":
                continue
            self.assertEqual(lease.result[key], value)
        blocked = next(
            row for row in lease.result["target_outcomes"]
            if row["outcome"] == "planned_excluded"
        )
        self.assertEqual(
            blocked["reason_token"],
            fixture["expected_result"]["blocked_reason_token"],
        )
        self.assertIn(
            fixture["expected_result"]["blocked_reason_token"],
            lease.result["error"],
        )

    def test_truly_shared_failure_is_recorded_once_with_affected_targets(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        provider = Path("cluster") / "SK_shared_cluster.spm"
        roots = [Path("SK_tree_a.spm"), Path("SK_tree_b.spm")]
        app._active_blender_dependency_map = {
            str(root): (str(provider),) for root in roots
        }
        app.state[str(provider)] = {
            "blend_status_kind": "data_error",
            "blend_status_error": {
                "kind": "data_error",
                "message": "shared provider failed its own postcondition",
            },
        }

        summary = app._build_pipeline_result_summary(
            [{"spm": root} for root in roots],
            {str(provider)},
            {str(root) for root in roots},
            None,
        )

        self.assertEqual(summary["completed_count"], 0)
        self.assertEqual(summary["blocked_count"], 2)
        self.assertEqual(summary["failed_count"], 0)
        self.assertEqual(len(summary["shared_failures"]), 1)
        self.assertEqual(
            summary["shared_failures"][0]["affected_targets"],
            [str(root) for root in roots],
        )

    def test_blender_window_guard_skips_own_tk_windows_before_title_query(self):
        gui = load_gui_module()
        blend = Path(r"C:\assets\tree.blend")
        queried = []

        class FakeUser32:
            def EnumWindows(self, callback, _extra):
                callback(101, 0)
                callback(202, 0)

            def GetWindowThreadProcessId(self, window, pid_pointer):
                pid_pointer._obj.value = (
                    gui.os.getpid() if window == 101 else gui.os.getpid() + 1
                )
                return 1

            def GetWindowTextLengthW(self, window):
                queried.append(window)
                return len(f"{blend.resolve()} - Blender")

            def GetWindowTextW(self, _window, buffer, _length):
                buffer.value = f"{blend.resolve()} - Blender"
                return len(buffer.value)

        with mock.patch.object(
            gui.ctypes,
            "windll",
            mock.Mock(user32=FakeUser32()),
        ):
            titles = gui.blender_open_file_window_titles(blend)

        self.assertEqual(queried, [202])
        self.assertEqual(titles, [f"{blend.resolve()} - Blender"])

    def test_repair_report_is_read_once_inside_one_status_scope(self):
        gui = load_gui_module()
        with tempfile.TemporaryDirectory() as temporary:
            report = Path(temporary) / "repair.json"
            report.write_text('{"status":"ok"}', encoding="utf-8")
            original_read_text = gui.Path.read_text
            reads = []

            def tracked_read_text(path, *args, **kwargs):
                reads.append(path)
                return original_read_text(path, *args, **kwargs)

            with mock.patch.object(gui.Path, "read_text", tracked_read_text):
                with gui.repair_report_read_scope():
                    first = gui._read_repair_pipeline_json(report)
                    second = gui._read_repair_pipeline_json(report)
                third = gui._read_repair_pipeline_json(report)

        self.assertIs(first, second)
        self.assertEqual(first, third)
        self.assertEqual(reads, [report, report])

    def test_repair_state_projection_reuses_the_scoped_report_read(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        with tempfile.TemporaryDirectory() as temporary:
            report = Path(temporary) / "repair.json"
            report.write_text(
                json.dumps({
                    "cluster_assembly_manifest": {"status": "ready"},
                }),
                encoding="utf-8",
            )
            original_read_text = gui.Path.read_text
            reads = []

            def tracked_read_text(path, *args, **kwargs):
                reads.append(path)
                return original_read_text(path, *args, **kwargs)

            app._repair_output_state_scoped = mock.Mock(
                side_effect=lambda _spm: (
                    gui._read_repair_pipeline_json(report)
                    and {
                        "current": True,
                        "push_ready": True,
                        "kind": "ready",
                        "reason": "준비됨 ✓",
                    }
                )
            )
            projection = {}
            with mock.patch.object(
                gui,
                "repair_pipeline_report_path",
                return_value=report,
            ), mock.patch.object(gui.Path, "read_text", tracked_read_text):
                state = app._repair_output_state(
                    Path(temporary) / "SK_tree_test_01.spm",
                    pipeline_projection_out=projection,
                )

        self.assertTrue(state["current"])
        self.assertEqual(
            projection,
            {"cluster_assembly_manifest": {"status": "ready"}},
        )
        self.assertEqual(reads, [report])

    def test_unreal_process_probe_uses_locale_independent_bytes(self):
        gui = load_gui_module()
        completed = mock.Mock(
            stdout=b"UnrealEditor.exe   1234 Console   \xc1"
        )
        with mock.patch.object(
            gui.subprocess,
            "run",
            return_value=completed,
        ) as run:
            self.assertTrue(gui.App._unreal_running())

        self.assertNotIn("text", run.call_args.kwargs)

    def test_process_runner_cleans_up_child_when_progress_callback_raises(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        app.cfg = {"process_poll_interval": 0.05}
        proc = mock.Mock()
        proc.poll.return_value = None
        proc.sk_log_handle = None

        def broken_progress(_elapsed, _line):
            raise RuntimeError("UI callback failed")

        with tempfile.TemporaryDirectory() as temp_dir, mock.patch.object(
            gui, "LOG_DIR", Path(temp_dir)
        ), mock.patch.object(
            gui, "launch_limited", return_value=proc
        ), mock.patch.object(
            gui, "terminate_process_tree", return_value=True
        ) as terminate, mock.patch.object(
            gui, "close_process_kill_job", return_value=True
        ) as close_job:
            with self.assertRaisesRegex(RuntimeError, "UI callback failed"):
                app._run_limited(
                    ["worker.exe"], "worker.log", timeout=5,
                    progress_callback=broken_progress,
                )

        terminate.assert_called_once_with(proc)
        close_job.assert_called_once_with(proc)
        self.assertNotIn(proc, app.active_procs)

    def test_process_runner_closes_job_after_parent_already_exited(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        app.cfg = {"process_poll_interval": 0.05}
        proc = mock.Mock()
        proc.poll.return_value = 7
        proc.returncode = 7
        proc.sk_log_handle = None

        with tempfile.TemporaryDirectory() as temp_dir, mock.patch.object(
            gui, "LOG_DIR", Path(temp_dir)
        ), mock.patch.object(
            gui, "launch_limited", return_value=proc
        ), mock.patch.object(
            gui, "terminate_process_tree"
        ) as terminate, mock.patch.object(
            gui, "close_process_kill_job", return_value=True
        ) as close_job:
            code, _log_file = app._run_limited(
                ["worker.exe"], "worker.log", timeout=5
            )

        self.assertEqual(code, 7)
        terminate.assert_not_called()
        close_job.assert_called_once_with(proc)
        self.assertNotIn(proc, app.active_procs)

    def test_process_runner_accepts_child_authoritative_timeout(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        app.cfg = {"process_poll_interval": 0.05}

        class TimedProcess:
            def __init__(self):
                self.started = gui.time.monotonic()
                self.returncode = None
                self.sk_log_handle = None

            def poll(self):
                elapsed = gui.time.monotonic() - self.started
                if elapsed >= 0.15:
                    self.returncode = 0
                return self.returncode

        with tempfile.TemporaryDirectory() as temp_dir, mock.patch.object(
            gui, "LOG_DIR", Path(temp_dir)
        ), mock.patch.object(
            gui,
            "launch_limited",
            return_value=TimedProcess(),
        ), mock.patch.object(
            gui, "terminate_process_tree"
        ) as terminate, mock.patch.object(
            gui, "close_process_kill_job", return_value=True
        ):
            code, _log_file = app._run_limited(
                ["worker.exe"],
                "worker.log",
                timeout=None,
            )

        self.assertEqual(code, 0)
        terminate.assert_not_called()

    def test_process_runner_none_timeout_still_honors_stop(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        app.cfg = {"process_poll_interval": 0.05}
        app.stop_flag.set()
        proc = mock.Mock()
        proc.poll.return_value = None
        proc.sk_log_handle = None

        with tempfile.TemporaryDirectory() as temp_dir, mock.patch.object(
            gui, "LOG_DIR", Path(temp_dir)
        ), mock.patch.object(
            gui, "launch_limited", return_value=proc
        ), mock.patch.object(
            gui, "terminate_process_tree", return_value=True
        ), mock.patch.object(
            gui, "close_process_kill_job", return_value=True
        ):
            with self.assertRaisesRegex(RuntimeError, "사용자 중지"):
                app._run_limited(
                    ["worker.exe"],
                    "worker.log",
                    timeout=None,
                )

    def test_process_runner_resets_inactivity_only_on_progress_marker(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        app.cfg = {"process_poll_interval": 0.05}
        clock = {"now": 0.0}

        class TimedProcess:
            def __init__(self, log_file):
                self.log_file = Path(log_file)
                self.returncode = None
                self.sk_log_handle = None
                self.wrote_progress = False

            def poll(self):
                if clock["now"] >= 0.06 and not self.wrote_progress:
                    self.log_file.write_text("PHASE_TWO\n", encoding="utf-8")
                    self.wrote_progress = True
                if clock["now"] >= 0.14:
                    self.returncode = 0
                return self.returncode

        def launch(_cmd, _cfg, **kwargs):
            return TimedProcess(kwargs["log_file"])

        def sleep(seconds):
            clock["now"] += seconds

        with tempfile.TemporaryDirectory() as temp_dir, mock.patch.object(
            gui, "LOG_DIR", Path(temp_dir)
        ), mock.patch.object(
            gui, "launch_limited", side_effect=launch
        ), mock.patch.object(
            gui.time, "monotonic", side_effect=lambda: clock["now"]
        ), mock.patch.object(
            gui.time, "sleep", side_effect=sleep
        ), mock.patch.object(
            gui, "terminate_process_tree"
        ) as terminate, mock.patch.object(
            gui, "close_process_kill_job", return_value=True
        ):
            code, _log_file = app._run_limited(
                ["worker.exe"],
                "worker.log",
                timeout=None,
                inactivity_timeout=0.08,
                inactivity_timeout_by_marker={"PHASE_TWO": 0.08},
            )

        self.assertEqual(code, 0)
        terminate.assert_not_called()

    def test_process_runner_heartbeat_text_does_not_reset_inactivity(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        app.cfg = {"process_poll_interval": 0.05}
        clock = {"now": 0.0}

        class StalledProcess:
            def __init__(self, log_file):
                self.log_file = Path(log_file)
                self.returncode = None
                self.sk_log_handle = None
                self.sequence = 0

            def poll(self):
                if self.returncode is None:
                    self.sequence += 1
                    with self.log_file.open("a", encoding="utf-8") as handle:
                        handle.write(f"HEARTBEAT REAL_PROGRESS {self.sequence}\n")
                return self.returncode

        process_holder = {}

        def launch(_cmd, _cfg, **kwargs):
            process_holder["value"] = StalledProcess(kwargs["log_file"])
            return process_holder["value"]

        def sleep(seconds):
            clock["now"] += seconds

        def terminate(process):
            process.returncode = -9
            return True

        with tempfile.TemporaryDirectory() as temp_dir, mock.patch.object(
            gui, "LOG_DIR", Path(temp_dir)
        ), mock.patch.object(
            gui, "launch_limited", side_effect=launch
        ), mock.patch.object(
            gui.time, "monotonic", side_effect=lambda: clock["now"]
        ), mock.patch.object(
            gui.time, "sleep", side_effect=sleep
        ), mock.patch.object(
            gui, "terminate_process_tree", side_effect=terminate
        ), mock.patch.object(
            gui, "close_process_kill_job", return_value=True
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "진행 없음 시간 초과.*process_start",
            ) as raised:
                app._run_limited(
                    ["worker.exe"],
                    "worker.log",
                    timeout=None,
                    inactivity_timeout=0.08,
                    inactivity_timeout_by_marker={"REAL_PROGRESS": 0.08},
                )

        self.assertEqual(
            raised.exception.timeout_kind,
            "child_progress_inactivity",
        )

    def test_blender_job_has_one_live_audit_call_after_static_mesh_gate(self):
        tree = ast.parse(
            (SK_BATCH_DIR / "sk_batch_gui.pyw").read_text(encoding="utf-8")
        )
        app_class = next(
            node
            for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == "App"
        )
        job = next(
            node
            for node in app_class.body
            if isinstance(node, ast.FunctionDef) and node.name == "_job_blender"
        )
        refresh_calls = [
            node
            for node in ast.walk(job)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "_refresh_stale_cluster_receipt"
        ]
        mesh_calls = [
            node
            for node in ast.walk(job)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "material_preflight_mesh_reference_block"
        ]
        self.assertEqual(len(refresh_calls), 1)
        self.assertEqual(len(mesh_calls), 1)
        self.assertLess(mesh_calls[0].lineno, refresh_calls[0].lineno)

    def test_next_queued_job_keeps_live_audit_memo_generation(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        app.pending_batch_jobs = deque()
        app.active_batch_job = None
        app.batch_progress = mock.Mock()
        app.batch_progress_var = mock.Mock()
        app._set_batch_queue_controls = mock.Mock()
        app.spm_calibration_signature = None
        app.legacy_spm_calibration_signature = None
        app._reset_cluster_receipt_refresh_memo()
        app._cluster_receipt_refresh_memo["validated"] = {"result": True}
        app.pending_batch_jobs.append({
            "id": 2,
            "cfg": {},
            "force_rerun": False,
            "push_transport": "rpc",
            "inventory": {},
            "targets": [],
            "label": "queued",
        })

        class FakeThread:
            def start(self):
                return None

        with mock.patch.object(
            gui, "calibration_settings_signature", return_value="current"
        ), mock.patch.object(
            gui, "legacy_calibration_settings_signature", return_value=None
        ), mock.patch.object(
            gui.threading, "Thread", return_value=FakeThread()
        ):
            app._start_next_batch_job()

        self.assertIn("validated", app._cluster_receipt_refresh_memo)

    @staticmethod
    def targets(*names):
        return [{"spm": Path(name), "checked": True} for name in names]

    def test_legacy_cluster_bootstraps_after_missing_canonical_schedule(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        app.cfg = {"spm_parallel_jobs": 1}
        app.force_rerun = False
        app.spm_calibration_signature = "current"
        app.legacy_spm_calibration_signature = None
        attempted = []

        with tempfile.TemporaryDirectory() as temp_dir:
            cluster = Path(temp_dir) / "cluster"
            cluster.mkdir()
            legacy_spm = cluster / "branch_tree_test_01.spm"
            canonical_spm = cluster / "SK_branch_tree_test_01.spm"
            legacy_spm.write_bytes(b"legacy cluster spm")
            item = {
                "spm": canonical_spm,
                "checked": True,
                # Scan aliases the live legacy snapshot onto the canonical row
                # that stage ① will create.
                "spm_snapshot": gui.file_content_snapshot(legacy_spm),
            }

            def fake_spm(_iid, spm):
                attempted.append(app._prepare_pair_for_job(spm))

            app._job_spm = mock.Mock(side_effect=fake_spm)
            with mock.patch.object(
                gui, "LOG_DIR", Path(temp_dir) / "logs"
            ), mock.patch.object(gui, "save_state"):
                result = app._run_batch(
                    "spm", [item], emit_done=False
                )

            self.assertTrue(result)
            self.assertEqual(app._phase_failed_items, set())
            self.assertEqual(len(attempted), 1)
            self.assertEqual(attempted[0], canonical_spm.resolve())
            self.assertEqual(
                canonical_spm.read_bytes(), legacy_spm.read_bytes()
            )

    def test_missing_spm_during_scheduling_fails_only_that_row(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        app.cfg = {"spm_parallel_jobs": 1}
        app.force_rerun = False
        app.spm_calibration_signature = "current"
        app.legacy_spm_calibration_signature = None
        attempted = []

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            missing_spm = root / "SK_missing.spm"
            existing_spm = root / "SK_existing.spm"
            existing_spm.write_bytes(b"spm")
            targets = [
                {"spm": missing_spm, "checked": True},
                {"spm": existing_spm, "checked": True},
            ]

            def fake_spm(_iid, spm):
                attempted.append(spm)
                if not spm.exists():
                    raise FileNotFoundError(spm)

            app._job_spm = mock.Mock(side_effect=fake_spm)
            with mock.patch.object(
                gui, "LOG_DIR", root / "logs"
            ), mock.patch.object(gui, "save_state"):
                result = app._run_batch(
                    "spm", targets, emit_done=False
                )

        self.assertTrue(result)
        self.assertCountEqual(attempted, [missing_spm, existing_spm])
        self.assertEqual(app._phase_failed_items, {str(missing_spm)})
        self.assertEqual(
            app.state[str(missing_spm)]["spm_status_kind"],
            "data_error",
        )

    def test_blender_repair_waits_for_cluster_sources_before_root_assets(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        app.cfg = {"blender_parallel_jobs": 2}
        completed_clusters = set()
        downstream_snapshots = []
        lock = threading.Lock()

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "Tree_elm"
            cluster = root / "Cluster"
            cluster_targets = [
                cluster / "SK_branch_elm_01.spm",
                cluster / "SK_leaf_elm_01.spm",
            ]
            root_targets = [
                root / "SK_Tree_elm_01.spm",
                root / "SK_Tree_elm_02.spm",
            ]
            targets = [
                {"spm": root_targets[0], "checked": True},
                {"spm": cluster_targets[0], "checked": True},
                {"spm": root_targets[1], "checked": True},
                {"spm": cluster_targets[1], "checked": True},
            ]

            def fake_blender(_iid, spm, _item):
                with lock:
                    if gui.is_cluster_source_spm(spm):
                        completed_clusters.add(spm)
                    else:
                        downstream_snapshots.append(set(completed_clusters))

            app._job_blender = mock.Mock(side_effect=fake_blender)
            with mock.patch.object(gui, "LOG_DIR", Path(temp_dir) / "logs"), \
                    mock.patch.object(gui, "save_state"):
                result = app._run_batch(
                    "blender", targets, emit_done=False
                )

        self.assertTrue(result)
        self.assertEqual(len(downstream_snapshots), len(root_targets))
        self.assertTrue(
            all(
                snapshot == set(cluster_targets)
                for snapshot in downstream_snapshots
            )
        )

    def test_cluster_producers_are_serial_per_owner_and_parallel_across_owners(
        self,
    ):
        gui = load_gui_module()
        app = self.make_app(gui)
        app.cfg = {"blender_parallel_jobs": 2}
        owner_a_started = threading.Event()
        owner_a_first_done = threading.Event()
        owner_b_started = threading.Event()
        order = []
        lock = threading.Lock()

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            cluster_a = root / "Tree_a" / "Cluster"
            cluster_b = root / "Tree_b" / "Cluster"
            a_first = cluster_a / "SK_cluster_a_01.spm"
            a_second = cluster_a / "SK_cluster_a_02.spm"
            b_first = cluster_b / "SK_cluster_b_01.spm"
            targets = [
                {"spm": a_first, "checked": True},
                {"spm": a_second, "checked": True},
                {"spm": b_first, "checked": True},
            ]

            def fake_blender(_iid, spm, _item):
                if spm == a_first:
                    with lock:
                        order.append("a1_start")
                    owner_a_started.set()
                    self.assertTrue(owner_b_started.wait(5))
                    owner_a_first_done.set()
                    with lock:
                        order.append("a1_done")
                elif spm == a_second:
                    self.assertTrue(owner_a_first_done.is_set())
                    with lock:
                        order.append("a2")
                else:
                    self.assertTrue(owner_a_started.wait(5))
                    owner_b_started.set()
                    with lock:
                        order.append("b1")

            app._job_blender = mock.Mock(side_effect=fake_blender)
            with mock.patch.object(
                gui, "LOG_DIR", root / "logs"
            ), mock.patch.object(gui, "save_state"):
                result = app._run_batch(
                    "blender", targets, emit_done=False
                )

        self.assertTrue(result)
        self.assertLess(order.index("a1_done"), order.index("a2"))
        self.assertIn("b1", order)

    def test_failed_cluster_repair_blocks_only_its_dependent_roots(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        app.cfg = {"blender_parallel_jobs": 1}
        calls = []

        with tempfile.TemporaryDirectory() as temp_dir:
            first_root = Path(temp_dir) / "Tree_elm"
            failed_cluster = (
                first_root / "Cluster" / "SK_branch_elm_01.spm"
            )
            blocked_root = first_root / "SK_Tree_elm_01.spm"
            independent_root = first_root / "SK_Tree_elm_02.spm"
            targets = [
                {
                    "spm": failed_cluster,
                    "checked": True,
                    "referenced_by_spms": (blocked_root,),
                },
                {"spm": blocked_root, "checked": True},
                {"spm": independent_root, "checked": True},
            ]

            def fake_blender(_iid, spm, _item):
                calls.append(spm)
                if spm == failed_cluster:
                    raise RuntimeError("cluster repair failed")

            app._job_blender = mock.Mock(side_effect=fake_blender)
            with mock.patch.object(
                gui, "LOG_DIR", Path(temp_dir) / "logs"
            ), mock.patch.object(
                gui,
                "cluster_relation_output_targets",
                return_value=(blocked_root,),
            ), mock.patch.object(gui, "save_state"):
                result = app._run_batch(
                    "blender", targets, emit_done=False
                )

        self.assertTrue(result)
        self.assertIn(failed_cluster, calls)
        self.assertNotIn(blocked_root, calls)
        self.assertIn(independent_root, calls)
        self.assertIn(str(blocked_root), app._phase_failed_items)
        self.assertEqual(
            app.state[str(blocked_root)]["blend_status_kind"],
            "dependency_blocked",
        )
        self.assertIn(
            failed_cluster.name,
            app.state[str(blocked_root)]["blend_status"],
        )

    def test_blender_repair_blocks_an_interactively_open_target(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        app.force_rerun = True
        app._prepare_pair_for_job = mock.Mock(side_effect=lambda spm: spm)
        app._leaf_reference_ready = mock.Mock(return_value=(True, "ok"))
        app._handoff_ready = mock.Mock(return_value=(False, "stale"))
        spm = Path("D:/asset/SK_branch_test_01.spm")
        blend = spm.with_suffix(".blend")

        with mock.patch.object(
            gui, "speedtree_output_spm_for", return_value=spm
        ), mock.patch.object(
            gui, "blend_path_for", return_value=blend
        ), mock.patch.object(
            gui,
            "blender_open_file_window_titles",
            return_value=[f"* SK_branch_test_01 [{blend}] - Blender 5.1"],
        ):
            with self.assertRaises(gui.BatchItemError) as raised:
                app._job_blender(
                    str(spm), spm, {"spm": spm, "checked": True}
                )

        self.assertEqual(raised.exception.kind, "manual_required")
        self.assertIn("대화형 Blender", str(raised.exception))

    def test_push_preflight_blocks_unsaved_interactive_blender_changes(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        app.active_push_transport = "headless"
        app._unreal_running = mock.Mock(return_value=False)
        app._handoff_ready = mock.Mock(return_value=(True, "ready"))
        spm = Path("D:/asset/SK_branch_test_01.spm")
        target = {"spm": spm, "checked": True}

        with mock.patch.object(
            gui,
            "blender_open_file_window_titles",
            return_value=["* SK_branch_test_01 [...] - Blender 5.1"],
        ), mock.patch.object(gui, "save_state"):
            ready, fatal = app._push_preflight([target])

        self.assertEqual(ready, [])
        self.assertIsNone(fatal)
        app._handoff_ready.assert_not_called()
        self.assertEqual(
            app.state[str(spm)]["push_status_kind"], "manual_required"
        )

    def test_push_preflight_consumes_same_pipeline_repair_contract(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        app.active_push_transport = "headless"
        app._unreal_running = mock.Mock(return_value=False)
        app._active_repair_stage_contracts = {}
        spm = Path("D:/asset/SK_tree_test_01.spm")
        target = {"spm": spm, "checked": True}
        app._handoff_ready = mock.Mock(
            side_effect=AssertionError(
                "same-pipeline Push must not repeat the ② handoff audit"
            )
        )
        app._validate_repair_stage_contract = mock.Mock(
            return_value=True
        )
        app._publish_repair_stage_contract(
            spm,
            ready=True,
            reason="준비됨 ✓",
        )

        with mock.patch.object(
            gui,
            "blender_open_file_window_titles",
            return_value=[],
        ), mock.patch.object(gui, "save_state"):
            ready, fatal = app._push_preflight([target])

        self.assertEqual(ready, [target])
        self.assertIsNone(fatal)
        app._handoff_ready.assert_not_called()
        app._validate_repair_stage_contract.assert_called_once_with(
            spm,
            mock.ANY,
        )
        self.assertIn("② 결과 재사용 1개", app.log.call_args.args[0])

    def test_standalone_push_still_runs_full_handoff_preflight(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        app.active_push_transport = "headless"
        app._unreal_running = mock.Mock(return_value=False)
        spm = Path("D:/asset/SK_tree_test_01.spm")
        target = {"spm": spm, "checked": True}
        app._handoff_ready = mock.Mock(
            return_value=(True, "준비됨 ✓")
        )

        with mock.patch.object(
            gui,
            "blender_open_file_window_titles",
            return_value=[],
        ), mock.patch.object(gui, "save_state"):
            ready, fatal = app._push_preflight([target])

        self.assertEqual(ready, [target])
        self.assertIsNone(fatal)
        app._handoff_ready.assert_called_once_with(spm)

    def test_stale_same_pipeline_contract_freezes_without_standalone_fallback(
        self,
    ):
        gui = load_gui_module()
        app = self.make_app(gui)
        app.active_push_transport = "headless"
        app._unreal_running = mock.Mock(return_value=False)
        app._active_repair_stage_contracts = {}
        spm = Path("D:/asset/SK_tree_stale_01.spm")
        target = {"spm": spm, "checked": True}
        app._handoff_ready = mock.Mock(
            side_effect=AssertionError(
                "stale same-generation evidence must not become standalone"
            )
        )
        app._publish_repair_stage_contract(
            spm,
            ready=True,
            reason="ready",
        )

        with mock.patch.object(
            gui,
            "blender_open_file_window_titles",
            return_value=[],
        ), mock.patch.object(gui, "save_state"):
            ready, fatal = app._push_preflight([target])

        self.assertEqual(ready, [])
        self.assertIsNone(fatal)
        app._handoff_ready.assert_not_called()
        state = app.state[str(spm)]
        self.assertEqual(
            state["push_status_kind"],
            "stale_execution_freeze",
        )
        self.assertIn(
            "STALE_EXECUTION_FREEZE",
            state["push_status_error"]["message"],
        )

    def test_same_pipeline_source_review_contract_blocks_without_reaudit(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        app.active_push_transport = "headless"
        app._unreal_running = mock.Mock(return_value=False)
        app._active_repair_stage_contracts = {}
        spm = Path("D:/asset/SK_tree_review_01.spm")
        target = {"spm": spm, "checked": True}
        app._handoff_ready = mock.Mock(
            side_effect=AssertionError("source review must not be re-audited")
        )
        app._publish_repair_stage_contract(
            spm,
            ready=False,
            reason="원본/재질 검토 필요 — Unreal Push 차단",
            kind="source_review",
        )

        with mock.patch.object(
            gui,
            "blender_open_file_window_titles",
            return_value=[],
        ), mock.patch.object(gui, "save_state"):
            ready, fatal = app._push_preflight([target])

        self.assertEqual(ready, [])
        self.assertIsNone(fatal)
        app._handoff_ready.assert_not_called()
        self.assertEqual(
            app.state[str(spm)]["push_status_kind"],
            "source_review",
        )

    def test_full_pipeline_keeps_repair_contract_until_push_then_clears_it(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        spm = Path("D:/asset/SK_tree_test_01.spm")
        targets = [{"spm": spm, "checked": True}]
        app._batch_job_inventory = mock.Mock(
            return_value={str(spm): targets[0]}
        )

        def run_phase(phase, _targets, emit_done=False):
            self.assertFalse(emit_done)
            app._phase_failed_items = set()
            app._phase_abort_reason = None
            if phase == "blender":
                app._publish_repair_stage_contract(
                    spm,
                    ready=True,
                    reason="준비됨 ✓",
                )
            if phase == "push":
                self.assertEqual(
                    app._repair_stage_contract(spm),
                    {
                        "ready": True,
                        "reason": "준비됨 ✓",
                        "kind": "ready",
                    },
                )
            return True

        app._run_batch = mock.Mock(side_effect=run_phase)
        with mock.patch.object(
            gui,
            "expand_blender_repair_targets",
            return_value=(targets, {}, set()),
        ):
            completed = app._run_full_pipeline(
                targets,
                terminal_phase="push",
                selected_scope=True,
                emit_done=False,
            )

        self.assertTrue(completed)
        self.assertEqual(
            [call.args[0] for call in app._run_batch.call_args_list],
            ["blender", "push"],
        )
        self.assertNotIn("_active_repair_stage_contracts", app.__dict__)

    def test_repair_stage_contract_uses_canonical_cluster_pair_key(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        app._active_repair_stage_contracts = {}
        mirror = Path(
            "D:/asset/Tree_test/cluster/branch_tree_test_01.spm"
        )
        canonical = mirror.with_name("SK_" + mirror.name)

        app._publish_repair_stage_contract(
            canonical,
            ready=True,
            reason="준비됨 ✓",
        )

        self.assertEqual(
            app._repair_stage_contract(mirror),
            {
                "ready": True,
                "reason": "준비됨 ✓",
                "kind": "ready",
            },
        )

    def test_repair_stage_contract_preserves_verified_push_dependencies(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        app._active_repair_stage_contracts = {}
        spm = Path("D:/asset/Tree_test/SK_tree_test_01.spm")
        dependency_contract = {
            "kind": "sk_batch_verified_push_dependencies",
            "schema_version": 1,
            "root_spm": str(spm),
            "assembly_status": "ready",
            "dependency_spms": [
                "D:/asset/Tree_test/Cluster/SK_cluster_test_01.spm"
            ],
            "evidence": "validated_cluster_assembly_manifest",
        }

        app._publish_repair_stage_contract(
            spm,
            ready=True,
            reason="ready",
            push_dependency_contract=dependency_contract,
        )
        dependency_contract["dependency_spms"].clear()

        published = app._repair_stage_contract(spm)
        self.assertEqual(
            published["push_dependency_contract"]["dependency_spms"],
            ["D:/asset/Tree_test/Cluster/SK_cluster_test_01.spm"],
        )

    def test_full_pipeline_clears_repair_contract_after_unexpected_error(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        target = {
            "spm": Path("D:/asset/SK_tree_test_01.spm"),
            "checked": True,
        }
        app._run_full_pipeline_stages = mock.Mock(
            side_effect=RuntimeError("unexpected")
        )

        with self.assertRaisesRegex(RuntimeError, "unexpected"):
            app._run_full_pipeline([target], emit_done=False)

        self.assertNotIn("_active_repair_stage_contracts", app.__dict__)

    def test_push_contract_wrapper_requires_and_preserves_current_envelope(self):
        gui = load_gui_module()
        with tempfile.TemporaryDirectory() as temp_dir, mock.patch.object(
            gui, "LOG_DIR", Path(temp_dir) / "logs"
        ), mock.patch.object(gui, "validate_preflight_envelope") as validate:
            root = Path(temp_dir) / "asset"
            spm = root / "SK_tree_contract_01.spm"
            spm.parent.mkdir(parents=True)
            spm.write_bytes(b"spm")
            envelope = {"source_fingerprint": "a" * 64}
            report = root / "reports" / (
                "SK_tree_contract_01_speedtree_repair_pipeline_report_codex.json"
            )
            report.parent.mkdir()
            report.write_text(
                json.dumps({
                    "speedtree_pipeline_contract": envelope,
                    "handoff_preflight": {"status": "ok"},
                }),
                encoding="utf-8",
            )

            contract_path = gui.App._push_material_contract(spm)

            payload = json.loads(contract_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["status"], "ok")
            self.assertEqual(payload["speedtree_pipeline_contract"], envelope)
            self.assertTrue(contract_path.name.endswith(f"{'a' * 16}.json"))
            validate.assert_called_once_with(envelope, spm, require_ok=True)

    def test_push_contract_uses_exact_isolated_repair_material_source(self):
        gui = load_gui_module()
        with tempfile.TemporaryDirectory() as temp_dir, mock.patch.object(
            gui, "LOG_DIR", Path(temp_dir) / "logs"
        ), mock.patch.object(gui, "validate_preflight_envelope") as validate:
            root = Path(temp_dir) / "tree"
            cluster = root / "cluster"
            spm = cluster / "SK_branch_test_01.spm"
            isolated = (
                cluster / ".sk_batch_isolated_bark" / "scope"
                / "tree" / "cluster" / spm.name
            )
            spm.parent.mkdir(parents=True)
            isolated.parent.mkdir(parents=True)
            spm.write_bytes(b"authored-production")
            isolated.write_bytes(b"isolated-normalized")
            production_identity = gui.source_identity(spm)
            isolated_identity = gui.source_identity(isolated)
            envelope = {
                "source_fingerprint": "b" * 64,
                "source": {
                    "spm": isolated_identity,
                    "stmat": [],
                },
            }
            report = cluster / "reports" / (
                "SK_branch_test_01_"
                "speedtree_repair_pipeline_report_codex.json"
            )
            report.parent.mkdir()
            report.write_text(
                json.dumps({
                    "speedtree_pipeline_contract": {
                        "source_fingerprint": "c" * 64,
                    },
                    "speedtree_material_handoff_contract": envelope,
                    "cluster_bark_source_resolution": {
                        "status": "ready",
                        "source_spm": {
                            "path": production_identity["canonical_path"],
                            "size": production_identity["size"],
                            "sha256": production_identity["sha256"],
                        },
                        "speedtree_spm": {
                            "path": isolated_identity["canonical_path"],
                            "size": isolated_identity["size"],
                            "sha256": isolated_identity["sha256"],
                        },
                    },
                    "handoff_preflight": {"status": "ok"},
                }),
                encoding="utf-8",
            )

            contract_path = gui.App._push_material_contract(spm)

            payload = json.loads(contract_path.read_text(encoding="utf-8"))
            self.assertEqual(
                payload["speedtree_pipeline_contract"],
                envelope,
            )
            self.assertEqual(
                Path(payload["material_source_spm"]),
                isolated.resolve(),
            )
            validate.assert_called_once_with(
                envelope,
                isolated.resolve(),
                require_ok=True,
            )
            mismatched = json.loads(report.read_text(encoding="utf-8"))
            mismatched["speedtree_material_handoff_contract"]["source"][
                "spm"
            ] = production_identity
            with self.assertRaisesRegex(
                RuntimeError,
                "not bound to the current canonical SPM",
            ):
                gui._material_handoff_envelope_for_push(
                    mismatched,
                    spm,
                )

    def test_push_entrypoints_forward_strict_contract_to_blender_job(self):
        gui_source = (SK_BATCH_DIR / "sk_batch_gui.pyw").read_text(encoding="utf-8")
        gui_tree = ast.parse(gui_source)
        gui_functions = {
            node.name: node
            for node in ast.walk(gui_tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        for function_name in ("_export_manifest_item", "_job_push"):
            strings = {
                node.value
                for node in ast.walk(gui_functions[function_name])
                if isinstance(node, ast.Constant) and isinstance(node.value, str)
            }
            self.assertIn("--spm", strings)
            self.assertIn("--material-contract", strings)
            self.assertIn("--repair-evidence", strings)
            self.assertIn("--dependency-orchestrated", strings)

        blender_job_strings = set()
        for function_name in (
            "_job_blender",
            "_execute_material_preflight",
        ):
            blender_job_strings.update(
                node.value
                for node in ast.walk(gui_functions[function_name])
                if isinstance(node, ast.Constant)
                and isinstance(node.value, str)
            )
        self.assertIn("--speedtree-spm", blender_job_strings)
        self.assertIn("--canonical-spm", blender_job_strings)

        bwr_source = (
            SK_BATCH_DIR / "jobs" / "bwr_headless_job.py"
        ).read_text(encoding="utf-8")
        self.assertIn("name_stem=speedtree_spm.stem", bwr_source)
        self.assertIn("settings.name_stem = canonical_spm.stem", bwr_source)
        self.assertNotIn("canonical_spm.name[3:]", bwr_source)
        self.assertIn('report["cluster_source_skin_contract"]', bwr_source)
        self.assertIn(
            'repair_settings["cluster_source_skin_contract"] = is_cluster_source',
            bwr_source,
        )
        self.assertIn(
            'repair_settings["defer_cluster_export_to_normalizer"] = bool(',
            bwr_source,
        )
        self.assertNotIn("cluster_single_bone_rigid_binding", bwr_source)
        self.assertNotIn("rigid_existing_single_bone", bwr_source)
        self.assertIn("export_collection_contract_issues(", bwr_source)
        cluster_export_contract_source = (
            SK_BATCH_DIR.parent / "cluster_export_handoff_contract.py"
        ).read_text(encoding="utf-8")
        self.assertIn(
            "orphan_owned_export_empty:",
            cluster_export_contract_source,
        )
        self.assertIn(
            "cluster_unsuffixed_export_unit:",
            cluster_export_contract_source,
        )
        self.assertIn(
            "cluster_missing_normalized_export_pivot",
            cluster_export_contract_source,
        )

        push_source = (
            SK_BATCH_DIR / "jobs" / "send2ue_push_job.py"
        ).read_text(encoding="utf-8")
        self.assertIn("resolve_cluster_spm_pair(spm_path)", push_source)
        self.assertIn(
            'parser.add_argument("--dependency-orchestrated", action="store_true")',
            push_source,
        )
        self.assertIn(
            "Tree Push with Cluster Assembly must run through the SK Batch",
            push_source,
        )
        self.assertIn(
            "actionable_cluster_manifest",
            push_source,
        )
        self.assertIn(
            "not args.dependency_orchestrated",
            push_source,
        )
        self.assertIn(
            '"dependency_orchestrated": bool(args.dependency_orchestrated)',
            push_source,
        )
        self.assertIn(
            'parser.add_argument("--repair-evidence")',
            push_source,
        )
        self.assertIn(
            "validate_repair_push_evidence_bundle(",
            push_source,
        )
        self.assertIn(
            "validate_export_object_postcondition(",
            push_source,
        )
        self.assertIn(
            '"push_mutators_skipped": True',
            push_source,
        )
        sync_call = push_source.index("utilities.sync_unreal_mesh_folder_path()")
        folder_read = push_source.index(
            "folder = scene_props.unreal_mesh_folder_path", sync_call
        )
        self.assertLess(sync_call, folder_read)
        self.assertIn("orphan_owned_export_empty:", push_source)
        self.assertIn("wind_source_stem = spm_path.stem", push_source)
        self.assertIn(
            'f"{wind_source_stem}_dynamic_wind_import_from_megaplant_groups.json"',
            push_source,
        )
        self.assertNotIn(
            'f"{unit_name}_dynamic_wind_import_from_megaplant_groups.json"',
            push_source,
        )
        self.assertIn("resolve_dynamic_wind_policy(", push_source)
        self.assertIn(
            "unit_name = find_export_unit_name() or spm_path.stem",
            push_source,
        )
        self.assertNotIn("find_export_unit_name(spm_path.stem)", push_source)
        self.assertIn(
            '"wind_json": str(wind_json) if wind_json_enabled else None',
            push_source,
        )
        push_tree = ast.parse(push_source)
        calls = [node for node in ast.walk(push_tree) if isinstance(node, ast.Call)]

        def call_name(call):
            if isinstance(call.func, ast.Name):
                return call.func.id
            if isinstance(call.func, ast.Attribute):
                return call.func.attr
            return ""

        for function_name in (
            "load_speedtree_texture_readiness_contract",
            "consolidate_speedtree_group_materials",
            "normalize_speedtree_material_textures",
        ):
            matches = [call for call in calls if call_name(call) == function_name]
            self.assertEqual(len(matches), 1)
            keywords = {item.arg for item in matches[0].keywords}
            if function_name == "load_speedtree_texture_readiness_contract":
                self.assertTrue({"spm_path", "source_fbx_path"} <= keywords)
            else:
                self.assertIn("texture_contract", keywords)

    def test_data_and_manual_failures_do_not_stop_later_push_items(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        targets = self.targets("SK_bad.spm", "SK_manual.spm", "SK_after.spm")
        attempted = []

        def fake_push(_iid, spm):
            attempted.append(spm.name)
            if spm.name == "SK_bad.spm":
                raise gui.BatchItemError("missing material", kind="data_error")
            if spm.name == "SK_manual.spm":
                raise gui.BatchItemError(
                    "Unreal Push 수동 처리 필요: 본 1,800 > 1,500",
                    kind="manual_required",
                )

        with mock.patch.object(app, "_push_preflight", return_value=(targets, None)), mock.patch.object(
            app, "_job_push", side_effect=fake_push
        ), mock.patch.object(gui, "save_state"):
            result = app._run_batch("push", targets, emit_done=False)

        self.assertTrue(result)
        self.assertEqual(
            attempted, ["SK_bad.spm", "SK_manual.spm", "SK_after.spm"]
        )
        self.assertEqual(app.state["SK_bad.spm"]["push_status_kind"], "data_error")
        self.assertEqual(
            app.state["SK_manual.spm"]["push_status_kind"], "manual_required"
        )

    def test_push_scheduler_receives_same_pipeline_dependency_contract(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        root = Path("Tree_elm") / "SK_Tree_elm.spm"
        target = {"spm": root, "checked": True}
        dependency_contract = {
            "kind": "sk_batch_verified_push_dependencies",
            "schema_version": 1,
            "root_spm": str(root),
            "assembly_status": "pass_through",
            "dependency_spms": [],
            "evidence": "validated_cluster_assembly_manifest",
        }
        app.items = {str(root): target}
        app._active_repair_stage_contracts = {}
        app._validate_repair_stage_contract = mock.Mock(
            return_value=True
        )
        app._publish_repair_stage_contract(
            root,
            ready=True,
            reason="ready",
            push_dependency_contract=dependency_contract,
        )

        with mock.patch.object(
            gui,
            "expand_push_targets",
            return_value=([target], {str(root): ()}, set()),
        ) as expand, mock.patch.object(
            app, "_push_preflight", return_value=([target], None)
        ), mock.patch.object(
            app, "_job_push"
        ), mock.patch.object(gui, "save_state"):
            result = app._run_batch("push", [target], emit_done=False)

        self.assertTrue(result)
        forwarded = expand.call_args.kwargs["stage_dependency_contracts"]
        self.assertEqual(len(forwarded), 1)
        self.assertEqual(
            next(iter(forwarded.values())),
            dependency_contract,
        )

    def test_rpc_push_blocks_only_roots_whose_auto_cluster_failed(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        cluster = Path("Tree_elm") / "Cluster" / "SK_any_cluster.spm"
        blocked_root = Path("Tree_elm") / "SK_Tree_elm.spm"
        independent_root = Path("Tree_maple") / "SK_Tree_maple.spm"
        cluster_item = {"spm": cluster, "checked": False}
        blocked_item = {"spm": blocked_root, "checked": True}
        independent_item = {"spm": independent_root, "checked": True}
        expanded = [cluster_item, blocked_item, independent_item]
        app.items = {
            str(item["spm"]): item for item in expanded
        }
        attempted = []

        def fake_push(_iid, spm):
            attempted.append(spm)
            if spm == cluster:
                raise gui.BatchItemError(
                    "cluster import failed", kind="data_error"
                )

        with mock.patch.object(
            gui,
            "expand_push_targets",
            return_value=(
                expanded,
                {
                    str(blocked_root): (str(cluster),),
                    str(independent_root): (),
                },
                {str(cluster)},
            ),
        ), mock.patch.object(
            app, "_push_preflight", return_value=(expanded, None)
        ), mock.patch.object(
            app, "_job_push", side_effect=fake_push
        ), mock.patch.object(gui, "save_state"):
            result = app._run_batch(
                "push",
                [blocked_item, independent_item],
                emit_done=False,
            )

        self.assertTrue(result)
        self.assertEqual(attempted, [cluster, independent_root])
        self.assertEqual(
            app.state[str(blocked_root)]["push_status_kind"],
            "dependency_blocked",
        )

    def test_full_pipeline_does_not_forward_failed_items_to_later_phases(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        cluster_spm = Path("Tree_test") / "Cluster" / "SK_bad_spm.spm"
        bad_blend = Path("Tree_test") / "SK_bad_blend.spm"
        good = Path("Tree_test") / "SK_good.spm"
        targets = [
            {"spm": cluster_spm, "checked": True},
            {"spm": bad_blend, "checked": True},
            {"spm": good, "checked": True},
        ]
        calls = []

        def fake_batch(phase, phase_targets, emit_done=False):
            calls.append((phase, [item["spm"].name for item in phase_targets]))
            if phase == "spm":
                app._phase_failed_items = {str(cluster_spm)}
            elif phase == "blender":
                app._phase_failed_items = {str(bad_blend)}
            else:
                app._phase_failed_items = set()
            app._phase_abort_reason = None
            return True

        app._run_batch = mock.Mock(side_effect=fake_batch)
        with mock.patch.object(
            gui,
            "expand_blender_repair_targets",
            return_value=(targets, {}, set()),
        ):
            app._run_full_pipeline(targets)

        self.assertEqual(calls[0][1], ["SK_bad_spm.spm"])
        self.assertEqual(calls[1][1], ["SK_bad_blend.spm", "SK_good.spm"])
        self.assertEqual(calls[2][1], ["SK_good.spm"])
        final_progress = [
            payload for kind, payload in list(app.ui_queue.queue)
            if kind == "progress"
        ][-1]
        self.assertIn("completed 1", final_progress)
        self.assertIn("blocked 0", final_progress)
        self.assertIn("failed 2", final_progress)

    def test_full_pipeline_cluster_failure_blocks_only_mapped_consumer(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        app.cfg = {
            "spm_parallel_jobs": 1,
            "blender_parallel_jobs": 1,
            "push_parallel_jobs": 1,
        }
        app.force_rerun = True
        failed_cluster = (
            Path("Tree_oak") / "Cluster" / "SK_branch_oak_01.spm"
        )
        blocked_tree = Path("Tree_oak") / "SK_Tree_oak_01.spm"
        unrelated_tree = Path("Tree_maple") / "SK_Tree_maple_01.spm"
        cluster_item = {"spm": failed_cluster, "checked": False}
        blocked_item = {"spm": blocked_tree, "checked": True}
        unrelated_item = {"spm": unrelated_tree, "checked": True}
        expanded = [cluster_item, blocked_item, unrelated_item]
        app.items = {
            str(item["spm"]): item for item in expanded
        }
        blender_attempts = []
        push_attempts = []
        preflight_inputs = []
        blocked_contracts_at_push_expand = []

        def fail_cluster_spm(_iid, spm):
            if spm == failed_cluster:
                raise gui.BatchItemError(
                    "cluster SPM failed",
                    kind="data_error",
                )

        def record_blender(_iid, spm, _item):
            blender_attempts.append(spm)

        def record_push(_iid, spm):
            push_attempts.append(spm)

        def record_preflight(targets):
            preflight_inputs.append(
                [item["spm"] for item in targets]
            )
            return list(targets), None

        def expand_for_push(
            targets,
            _all_items,
            stage_dependency_contracts=None,
        ):
            blocked_contracts_at_push_expand.append(
                app._repair_stage_contract(blocked_tree)
            )
            self.assertIsNone(stage_dependency_contracts)
            return (
                expanded,
                {
                    str(blocked_tree): (str(failed_cluster),),
                    str(unrelated_tree): (),
                },
                {str(failed_cluster)},
            )

        with tempfile.TemporaryDirectory() as temp_dir, mock.patch.object(
            gui,
            "LOG_DIR",
            Path(temp_dir) / "logs",
        ), mock.patch.object(
            gui,
            "expand_blender_repair_targets",
            return_value=(
                expanded,
                {
                    str(blocked_tree): (str(failed_cluster),),
                    str(unrelated_tree): (),
                },
                {str(failed_cluster)},
            ),
        ), mock.patch.object(
            gui,
            "expand_push_targets",
            side_effect=expand_for_push,
        ) as expand_push, mock.patch.object(
            app,
            "_push_preflight",
            side_effect=record_preflight,
        ), mock.patch.object(
            app,
            "_job_spm",
            side_effect=fail_cluster_spm,
        ), mock.patch.object(
            app,
            "_job_blender",
            side_effect=record_blender,
        ), mock.patch.object(
            app,
            "_job_push",
            side_effect=record_push,
        ), mock.patch.object(gui, "save_state"):
            result = app._run_full_pipeline(expanded)

        self.assertFalse(result)
        self.assertEqual(blender_attempts, [unrelated_tree])
        self.assertEqual(push_attempts, [unrelated_tree])
        self.assertEqual(preflight_inputs, [[unrelated_tree]])
        self.assertEqual(
            blocked_contracts_at_push_expand,
            [{
                "ready": False,
                "reason": (
                    "required Cluster stage failed: "
                    f"{failed_cluster.name} — 원인: cluster SPM failed"
                ),
                "kind": "dependency_blocked",
            }],
        )
        self.assertEqual(
            [item["spm"] for item in expand_push.call_args.args[0]],
            [unrelated_tree],
        )
        self.assertEqual(app._active_push_auto_added_ids, set())
        self.assertEqual(
            app.state[str(failed_cluster)]["spm_status_kind"],
            "data_error",
        )
        self.assertEqual(
            app.state[str(blocked_tree)]["blend_status_kind"],
            "dependency_blocked",
        )
        self.assertEqual(
            app.state[str(blocked_tree)]["push_status_kind"],
            "dependency_blocked",
        )
        self.assertEqual(
            app._phase_failed_items,
            {str(failed_cluster), str(blocked_tree)},
        )
        final_progress = [
            payload for kind, payload in list(app.ui_queue.queue)
            if kind == "progress"
        ][-1]
        self.assertIn("completed 1", final_progress)
        self.assertIn("blocked 1", final_progress)
        self.assertIn("failed 1", final_progress)

    def test_full_pipeline_finishes_cluster_before_tree_spm_and_blender(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        owner = Path(r"D:\Trees\Tree_elm")
        cluster_spm = (
            owner / "Cluster" / "SK_branch_elm_01.spm"
        )
        tree_spm = owner / "SK_Tree_elm_01.spm"
        cluster_item = {
            "spm": cluster_spm,
            "checked": True,
            "referenced_by_spms": (tree_spm,),
        }
        tree_item = {"spm": tree_spm, "checked": True}
        app.items = {
            str(cluster_spm): cluster_item,
            str(tree_spm): tree_item,
        }
        calls = []

        def fake_batch(phase, phase_targets, emit_done=False):
            del emit_done
            calls.append(
                (
                    phase,
                    [item["spm"] for item in phase_targets],
                )
            )
            app._phase_failed_items = set()
            app._phase_abort_reason = None
            return True

        app._run_batch = mock.Mock(side_effect=fake_batch)
        with mock.patch.object(
            gui,
            "cluster_relation_output_targets",
            return_value=(tree_spm,),
        ):
            app._run_full_pipeline(
                [tree_item, cluster_item],
                terminal_phase="push",
            )

        self.assertEqual(
            calls,
            [
                ("spm", [cluster_spm]),
                ("blender", [cluster_spm]),
                ("blender", [tree_spm]),
                ("push", [cluster_spm, tree_spm]),
            ],
        )

    def test_rpc_timeout_stops_safely_and_marks_remaining_items_not_run(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        targets = self.targets("SK_timeout.spm", "SK_after_1.spm", "SK_after_2.spm")
        attempted = []

        def fake_push(_iid, spm):
            attempted.append(spm.name)
            raise gui.BatchItemError(
                "Unreal RPC 시간 초과 — 큐 응답 정지", kind="rpc_timeout"
            )

        with mock.patch.object(app, "_push_preflight", return_value=(targets, None)), mock.patch.object(
            app, "_job_push", side_effect=fake_push
        ), mock.patch.object(gui, "save_state"):
            result = app._run_batch("push", targets, emit_done=False)

        self.assertFalse(result)
        self.assertEqual(attempted, ["SK_timeout.spm"])
        self.assertEqual(
            app.state["SK_timeout.spm"]["push_status_kind"], "rpc_timeout"
        )
        self.assertEqual(
            app.state["SK_after_1.spm"]["push_status_kind"], "not_run_unreal"
        )
        self.assertIn("미실행", app.state["SK_after_2.spm"]["push_status"])

    def test_preflight_unreal_off_is_a_failure_not_false_completion(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        targets = self.targets("SK_first.spm", "SK_second.spm")

        with mock.patch.object(app, "_unreal_running", return_value=False), mock.patch.object(
            gui, "save_state"
        ):
            ready, reason = app._push_preflight(targets)

        self.assertEqual(ready, [])
        self.assertIn("Unreal Editor", reason)
        self.assertEqual(
            app.state["SK_first.spm"]["push_status_kind"], "unreal_unavailable"
        )

    def test_all_preflight_excluded_is_an_explicit_push_failure(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        targets = self.targets("SK_missing_texture.spm", "SK_stale_blend.spm")

        with mock.patch.object(
            app, "_push_preflight", return_value=([], None)
        ):
            result = app._run_batch("push", targets, emit_done=True)

        self.assertFalse(result)
        self.assertEqual(app._phase_failed_items, {
            "SK_missing_texture.spm", "SK_stale_blend.spm",
        })
        self.assertIn("2개 제외", app._phase_abort_reason)
        progress = [
            payload for kind, payload in list(app.ui_queue.queue)
            if kind == "progress"
        ][-1]
        self.assertIn("Unreal Push 중단", progress)
        self.assertIn("2개 제외", progress)

    def test_headless_preflight_allows_unreal_to_be_off(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        app.active_push_transport = "headless"
        targets = self.targets("SK_first.spm", "SK_second.spm")

        with mock.patch.object(app, "_unreal_running", return_value=False), mock.patch.object(
            app, "_handoff_ready", return_value=(True, "")
        ), mock.patch.object(gui, "save_state"):
            ready, reason = app._push_preflight(targets)

        self.assertEqual(ready, targets)
        self.assertIsNone(reason)

    def test_headless_preflight_blocks_when_editor_is_open(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        app.active_push_transport = "headless"
        targets = self.targets("SK_first.spm")

        with mock.patch.object(app, "_unreal_running", return_value=True), mock.patch.object(
            gui, "save_state"
        ):
            ready, reason = app._push_preflight(targets)

        self.assertEqual(ready, [])
        self.assertIn("Unreal Editor", reason)
        self.assertEqual(
            app.state["SK_first.spm"]["push_status_kind"], "unreal_unavailable"
        )

    def test_push_batch_routes_headless_without_using_rpc_job(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        app.active_push_transport = "headless"
        targets = self.targets("SK_first.spm")

        with mock.patch.object(
            app, "_push_preflight", return_value=(targets, None)
        ), mock.patch.object(
            app, "_run_headless_push_batch", return_value=True
        ) as headless, mock.patch.object(app, "_job_push") as rpc_job:
            result = app._run_batch("push", targets, emit_done=False)

        self.assertTrue(result)
        headless.assert_called_once_with(targets, emit_done=False)
        rpc_job.assert_not_called()

    def test_watchdog_records_crash_in_checkpoint_before_restart(self):
        gui = load_gui_module()
        with tempfile.TemporaryDirectory() as temp_dir:
            checkpoint = Path(temp_dir) / "checkpoint.json"
            checkpoint.write_text(
                json.dumps(
                    {
                        "current_item": "item-a",
                        "items": {
                            "item-a": {
                                "status": "importing",
                                "crash_count": 0,
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            result = gui.App._record_headless_process_exit(checkpoint, 2)

        self.assertIsNone(result["current_item"])
        self.assertEqual(result["items"]["item-a"]["status"], "unreal_crash")
        self.assertEqual(result["items"]["item-a"]["crash_count"], 1)

    def test_headless_checkpoint_replaces_stale_blender_progress(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        item_by_id = {
            "item-a": {"report_path": "a.json", "fingerprint": "a-v1"},
            "item-b": {"report_path": "b.json", "fingerprint": "b-v1"},
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            checkpoint = Path(temp_dir) / "checkpoint.json"
            checkpoint.write_text(
                json.dumps(
                    {
                        "current_item": "item-b",
                        "items": {
                            "item-a": {"status": "imported_ok"},
                            "item-b": {"status": "importing"},
                        },
                    }
                ),
                encoding="utf-8",
            )
            with mock.patch.object(gui, "save_state"):
                app._sync_headless_checkpoint(checkpoint, item_by_id)
                app._sync_headless_checkpoint(checkpoint, item_by_id)

        progress = [
            payload
            for kind, payload in list(app.ui_queue.queue)
            if kind == "progress"
        ]
        self.assertEqual(
            progress,
            ["Unreal Push 1/2 · headless 처리 중 1개"],
        )

    def test_headless_export_failures_are_not_reported_as_cache_success(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        app.force_rerun = False
        app._phase_failed_items = set()
        app.cfg = {"blender_parallel_jobs": 1}
        targets = self.targets("SK_broken.spm")

        with mock.patch.object(
            app, "_export_manifest_item", side_effect=RuntimeError("export failed")
        ), mock.patch.object(gui, "save_state"):
            result = app._run_headless_push_batch(targets, emit_done=True)

        self.assertFalse(result)
        progress = [
            payload for kind, payload in list(app.ui_queue.queue)
            if kind == "progress"
        ][-1]
        self.assertIn("실패/준비 제외 1개", progress)
        self.assertNotIn("완료 (cache)", progress)

    def test_rpc_push_skips_when_source_export_and_import_receipts_match(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        app.force_rerun = False
        spm = Path("SK_current.spm")
        iid = str(spm)
        app.state[iid] = {"push_import_fingerprint": "item-v1"}
        app._source_push_fingerprint = mock.Mock(return_value="source-v1")
        app._cached_manifest_item = mock.Mock(
            return_value={"queue_id": iid, "fingerprint": "item-v1"}
        )
        app._run_limited = mock.Mock(
            side_effect=AssertionError("current RPC Push must not run Blender")
        )

        with mock.patch.object(gui, "save_state"):
            app._job_push(iid, spm)

        app._run_limited.assert_not_called()
        self.assertEqual(app.state[iid]["push_status_kind"], "imported_ok")
        self.assertIn("건너뜀", app.log.call_args.args[0])

    def test_ready_assembly_manifest_never_rpc_skips_live_verification(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        app.force_rerun = False
        app.cfg = {"send2ue_dir": "send2ue"}
        spm = Path("SK_tree_assembly_01.spm")
        iid = str(spm)
        app.state[iid] = {"push_import_fingerprint": "item-v1"}
        app._source_push_fingerprint = mock.Mock(return_value="source-v1")
        app._cached_manifest_item = mock.Mock(
            return_value={
                "queue_id": iid,
                "fingerprint": "item-v1",
                "cluster_assembly": {
                    "ingest_plan": {
                        "status": "ready",
                        "asset_contract": {
                            "full_skeletal_mesh": "/Game/Tree/SK_Tree",
                            "base_skeletal_mesh": "/Game/Tree/Base",
                            "parts": {},
                            "assembly": "/Game/Tree/Assembly",
                        },
                    }
                },
            }
        )
        app._push_material_contract = mock.Mock(
            side_effect=RuntimeError("continued to live verification")
        )

        with self.assertRaisesRegex(
            RuntimeError, "continued to live verification"
        ):
            app._job_push(iid, spm)

        self.assertFalse(
            any(
                "건너뜀" in str(call.args[0])
                for call in app.log.call_args_list
            )
        )

    def test_headless_cache_hit_tree_with_ready_assembly_is_verified(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        app.force_rerun = False
        app._phase_failed_items = set()
        app._active_push_dependency_map = {}
        tree = Path("Tree") / "SK_tree_assembly_01.spm"
        iid = str(tree)
        app.state[iid] = {"push_import_fingerprint": "tree-v1"}
        app.cfg = {
            "blender_parallel_jobs": 1,
            "unreal_editor_cmd": "UnrealEditor-Cmd.exe",
            "unreal_project": "MyProject2.uproject",
            "headless_item_crash_retries": 2,
            "headless_batch_max_restarts": 0,
            "headless_job_timeout": 100,
        }
        exported = {
            "queue_id": iid,
            "fingerprint": "tree-v1",
            "report_path": "tree-report.json",
            "assets": [],
            "cluster_assembly": {
                "ingest_plan": {
                    "status": "ready",
                    "asset_contract": {
                        "full_skeletal_mesh": "/Game/Tree/SK_Tree",
                        "base_skeletal_mesh": "/Game/Tree/Base",
                        "parts": {},
                        "assembly": "/Game/Tree/Assembly",
                    },
                }
            },
        }
        captured = {}

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)

            def fake_commandlet(_cmd, log_name, _timeout, **kwargs):
                manifest_path = Path(
                    kwargs["env"]["SK_BATCH_MANIFEST_PATH"]
                )
                checkpoint_path = Path(
                    kwargs["env"]["SK_BATCH_CHECKPOINT_PATH"]
                )
                report_path = Path(kwargs["env"]["SK_BATCH_REPORT_PATH"])
                payload = json.loads(
                    manifest_path.read_text(encoding="utf-8")
                )
                captured["item"] = payload["items"][0]
                states = {
                    iid: {
                        "status": "imported_ok",
                        "fingerprint": "tree-v1",
                    }
                }
                checkpoint_path.write_text(
                    json.dumps(
                        {
                            "complete": True,
                            "current_item": None,
                            "items": states,
                        }
                    ),
                    encoding="utf-8",
                )
                report_path.write_text(
                    json.dumps({"status": "complete", "items": states}),
                    encoding="utf-8",
                )
                return 0, temp_root / log_name

            with mock.patch.object(
                gui, "LOG_DIR", temp_root
            ), mock.patch.object(
                app, "_export_manifest_item", return_value=exported
            ), mock.patch.object(
                app, "_run_limited", side_effect=fake_commandlet
            ), mock.patch.object(gui, "save_state"):
                result = app._run_headless_push_batch(
                    [{"spm": tree, "checked": True}],
                    emit_done=False,
                )

        self.assertTrue(result)
        self.assertTrue(captured["item"]["verify_existing_assets"])

    def test_rpc_cli_uses_target_project_remote_execution_adapter(self):
        gui = load_gui_module()
        with tempfile.TemporaryDirectory() as temp_dir:
            project_dir = Path(temp_dir) / "MyProject"
            config_dir = project_dir / "Config"
            config_dir.mkdir(parents=True)
            project_path = project_dir / "MyProject.uproject"
            project_path.write_text("{}", encoding="utf-8")
            (config_dir / "DefaultEngine.ini").write_text(
                "\n".join(
                    [
                        "[/Script/PythonScriptPlugin.PythonScriptPluginSettings]",
                        "bRemoteExecution=True",
                        "RemoteExecutionMulticastGroupEndpoint=239.1.2.3:6766",
                        "RemoteExecutionMulticastBindAddress=192.168.50.9",
                        "RemoteExecutionMulticastTtl=1",
                    ]
                ),
                encoding="utf-8",
            )

            args = gui.send2ue_rpc_cli_args(project_path)

        self.assertEqual(
            args,
            [
                "--rpc-multicast-bind-address",
                "192.168.50.9",
                "--rpc-multicast-group-endpoint",
                "239.1.2.3:6766",
                "--rpc-multicast-ttl",
                "1",
            ],
        )

    def test_rpc_cli_does_not_invent_adapter_without_project_contract(self):
        gui = load_gui_module()
        with tempfile.TemporaryDirectory() as temp_dir:
            project_path = Path(temp_dir) / "Missing.uproject"
            args = gui.send2ue_rpc_cli_args(project_path)

        self.assertEqual(args, [])

    def test_rpc_cli_respects_disabled_unreal_remote_execution(self):
        gui = load_gui_module()
        with tempfile.TemporaryDirectory() as temp_dir:
            project_dir = Path(temp_dir) / "DisabledProject"
            config_dir = project_dir / "Config"
            config_dir.mkdir(parents=True)
            project_path = project_dir / "DisabledProject.uproject"
            project_path.write_text("{}", encoding="utf-8")
            (config_dir / "DefaultEngine.ini").write_text(
                "\n".join(
                    [
                        "[/Script/PythonScriptPlugin.PythonScriptPluginSettings]",
                        "bRemoteExecution=False",
                        "RemoteExecutionMulticastBindAddress=192.168.50.9",
                    ]
                ),
                encoding="utf-8",
            )

            args = gui.send2ue_rpc_cli_args(project_path)

        self.assertEqual(args, [])

    def test_rpc_preferences_are_applied_before_unreal_discovery(self):
        source = (
            SK_BATCH_DIR / "jobs" / "send2ue_push_job.py"
        ).read_text(encoding="utf-8")

        configure = source.index(
            'report["rpc_configuration"] = configure_send2ue_rpc_preferences(args)'
        )
        discover = source.index("if not utilities.is_unreal_connected():")
        self.assertLess(configure, discover)
        self.assertIn(
            'preferences.command_endpoint = f"{bind_address}:{int(port_text)}"',
            source,
        )

    def test_saved_preflight_skip_is_rechecked_against_current_handoff(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        spm = Path("branch_elm_01.spm")
        iid = str(spm)
        app.state[iid] = {
            "push_status": "건너뜀: Blender 갱신 필요",
            "push_status_kind": "preflight_skip",
        }
        app._handoff_ready = mock.Mock(return_value=(True, "준비됨 ✓"))

        text = app._current_push_status_text(iid, spm)

        self.assertEqual(text, "준비됨 ✓")
        app._handoff_ready.assert_called_once_with(spm)

    def test_saved_push_completion_is_not_current_after_input_change(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        spm = Path("SK_changed.spm")
        iid = str(spm)
        app.state[iid] = {
            "push_status": "완료 (headless)",
            "push_status_kind": "imported_ok",
            "push_source_fingerprint_cache": {
                "version": gui.PUSH_SOURCE_FINGERPRINT_CACHE_VERSION,
                "fingerprint": "source-v1",
                "snapshot": {"blend": {"mtime_ns": 1}},
            },
            "push_export_cache": {
                "source_fingerprint": "source-v1",
                "manifest": "manifest.json",
                "fingerprint": "item-v1",
            },
            "push_import_fingerprint": "item-v1",
        }
        app._push_dependency_paths = mock.Mock(return_value=[])

        with mock.patch.object(
            gui,
            "push_source_snapshot",
            return_value={"blend": {"mtime_ns": 2}},
        ):
            text = app._current_push_status_text(iid, spm)

        self.assertIn("Push 재확인 필요", text)
        self.assertNotIn("완료", text)

    def test_push_fingerprint_tracks_direct_export_and_ingest_modules(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        app.cfg = {"send2ue_dir": Path("C:/send2ue")}

        paths = {
            Path(path).resolve()
            for path in app._push_dependency_paths()
        }

        self.assertIn(
            (SK_BATCH_DIR / "nanite_assembly_materials.py").resolve(),
            paths,
        )
        self.assertIn(
            (
                SK_BATCH_DIR.parent
                / "cluster_spm_pair_contract.py"
            ).resolve(),
            paths,
        )
        self.assertIn(
            (
                SK_BATCH_DIR.parent
                / "speedtree_pipeline_contract.py"
            ).resolve(),
            paths,
        )

    def test_push_fingerprint_tracks_item_repair_assembly_report(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        app.cfg = {"send2ue_dir": Path("C:/send2ue")}
        spm = Path("C:/Tree/SK_tree_test_01.spm")

        paths = {
            Path(path)
            for path in app._push_source_dependency_paths(spm)
        }

        self.assertIn(gui.repair_pipeline_report_path(spm), paths)

    def test_headless_exports_all_items_then_uses_one_commandlet_session(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        app.force_rerun = False
        app._phase_abort_reason = ""
        app.cfg = {
            "unreal_editor_cmd": "UnrealEditor-Cmd.exe",
            "unreal_project": "MyProject2.uproject",
            "headless_item_crash_retries": 2,
            "headless_batch_max_restarts": 0,
            "headless_job_timeout": 100,
        }
        targets = self.targets("SK_first.spm", "SK_second.spm")
        exported = [
            {
                "queue_id": "SK_first.spm",
                "fingerprint": "first-v1",
                "report_path": "first-report.json",
            },
            {
                "queue_id": "SK_second.spm",
                "fingerprint": "second-v1",
                "report_path": "second-report.json",
            },
        ]

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)

            def fake_commandlet(_cmd, log_name, _timeout, **kwargs):
                manifest_path = Path(kwargs["env"]["SK_BATCH_MANIFEST_PATH"])
                checkpoint_path = Path(kwargs["env"]["SK_BATCH_CHECKPOINT_PATH"])
                report_path = Path(kwargs["env"]["SK_BATCH_REPORT_PATH"])
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                states = {
                    item["queue_id"]: {
                        "status": "imported_ok",
                        "fingerprint": item["fingerprint"],
                    }
                    for item in manifest["items"]
                }
                checkpoint_path.write_text(
                    json.dumps(
                        {
                            "complete": True,
                            "current_item": None,
                            "items": states,
                        }
                    ),
                    encoding="utf-8",
                )
                report_path.write_text(
                    json.dumps({"status": "complete", "items": states}),
                    encoding="utf-8",
                )
                kwargs["progress_callback"](1.0, "done")
                return 0, temp_root / log_name

            with mock.patch.object(gui, "LOG_DIR", temp_root), mock.patch.object(
                app, "_export_manifest_item", side_effect=exported
            ) as export_item, mock.patch.object(
                app, "_run_limited", side_effect=fake_commandlet
            ) as commandlet, mock.patch.object(gui, "save_state"):
                result = app._run_headless_push_batch(targets, emit_done=False)

        self.assertTrue(result)
        self.assertEqual(export_item.call_count, 2)
        commandlet.assert_called_once()
        self.assertEqual(
            app.state["SK_first.spm"]["push_status_kind"], "imported_ok"
        )
        self.assertEqual(
            app.state["SK_second.spm"]["push_status_kind"], "imported_ok"
        )

    def test_headless_verifies_cached_cluster_before_dependent_tree(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        app.force_rerun = False
        app._phase_failed_items = set()
        cluster = Path("Tree_elm") / "Cluster" / "SK_cluster_any.spm"
        root = Path("Tree_elm") / "SK_Tree_elm.spm"
        app._active_push_dependency_map = {
            str(root): (str(cluster),)
        }
        app.cfg = {
            "blender_parallel_jobs": 1,
            "unreal_editor_cmd": "UnrealEditor-Cmd.exe",
            "unreal_project": "MyProject2.uproject",
            "headless_item_crash_retries": 2,
            "headless_batch_max_restarts": 0,
            "headless_job_timeout": 100,
        }
        targets = [
            {"spm": cluster, "checked": False},
            {"spm": root, "checked": True},
        ]
        exported = [
            {
                "queue_id": str(cluster),
                "fingerprint": "cluster-v1",
                "report_path": "cluster-report.json",
                "assets": [
                    {
                        "asset_data": {
                            "asset_path": "/Game/Tree/Cluster/SK_cluster_any_01"
                        }
                    }
                ],
            },
            {
                "queue_id": str(root),
                "fingerprint": "root-v1",
                "report_path": "root-report.json",
                "assets": [],
            },
        ]
        app.state[str(cluster)] = {
            "push_import_fingerprint": "cluster-v1"
        }
        captured = {}

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)

            def fake_commandlet(_cmd, log_name, _timeout, **kwargs):
                manifest_path = Path(
                    kwargs["env"]["SK_BATCH_MANIFEST_PATH"]
                )
                checkpoint_path = Path(
                    kwargs["env"]["SK_BATCH_CHECKPOINT_PATH"]
                )
                report_path = Path(kwargs["env"]["SK_BATCH_REPORT_PATH"])
                manifest = json.loads(
                    manifest_path.read_text(encoding="utf-8")
                )
                captured["items"] = manifest["items"]
                states = {
                    item["queue_id"]: {
                        "status": "imported_ok",
                        "fingerprint": item["fingerprint"],
                    }
                    for item in manifest["items"]
                }
                checkpoint_path.write_text(
                    json.dumps(
                        {
                            "complete": True,
                            "current_item": None,
                            "items": states,
                        }
                    ),
                    encoding="utf-8",
                )
                report_path.write_text(
                    json.dumps({"status": "complete", "items": states}),
                    encoding="utf-8",
                )
                kwargs["progress_callback"](1.0, "done")
                return 0, temp_root / log_name

            with mock.patch.object(
                gui, "LOG_DIR", temp_root
            ), mock.patch.object(
                app, "_export_manifest_item", side_effect=exported
            ), mock.patch.object(
                app, "_run_limited", side_effect=fake_commandlet
            ), mock.patch.object(gui, "save_state"):
                result = app._run_headless_push_batch(
                    targets, emit_done=False
                )

        self.assertTrue(result)
        self.assertEqual(
            [item["queue_id"] for item in captured["items"]],
            [str(cluster), str(root)],
        )
        self.assertTrue(captured["items"][0]["verify_existing_assets"])
        self.assertEqual(
            captured["items"][1]["depends_on_queue_ids"],
            [str(cluster)],
        )

    def test_headless_blocks_tree_before_unreal_when_cluster_export_failed(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        app.force_rerun = False
        app._phase_failed_items = set()
        cluster = Path("Tree_elm") / "Cluster" / "SK_leaf_elm_side_01.spm"
        root = Path("Tree_elm") / "SK_Tree_elm_01.spm"
        app._active_push_dependency_map = {
            str(root): (str(cluster),)
        }
        app.cfg = {"blender_parallel_jobs": 1}
        targets = [
            {"spm": cluster, "checked": False},
            {"spm": root, "checked": True},
        ]
        root_export = {
            "queue_id": str(root),
            "fingerprint": "root-v1",
            "report_path": "root-report.json",
            "assets": [],
        }

        def export_item(iid, _spm, _stamp):
            if iid == str(cluster):
                raise gui.BatchItemError(
                    "Blender에 저장되지 않은 변경이 있음",
                    kind="manual_required",
                )
            return root_export

        with mock.patch.object(
            app, "_export_manifest_item", side_effect=export_item
        ), mock.patch.object(
            app,
            "_run_limited",
            side_effect=AssertionError(
                "Unreal must not start for a dependency-blocked tree"
            ),
        ) as commandlet, mock.patch.object(gui, "save_state"):
            result = app._run_headless_push_batch(
                targets,
                emit_done=False,
            )

        self.assertFalse(result)
        commandlet.assert_not_called()
        self.assertEqual(
            app.state[str(cluster)]["push_status_kind"],
            "manual_required",
        )
        self.assertEqual(
            app.state[str(root)]["push_status_kind"],
            "dependency_blocked",
        )
        self.assertIn(
            cluster.name,
            app.state[str(root)]["push_status_error"]["message"],
        )

    def test_cached_manifest_item_finds_recovered_item_after_provider(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        app.force_rerun = False
        with tempfile.TemporaryDirectory() as temp_dir:
            manifest_path = Path(temp_dir) / "recovery.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "items": [
                            {"queue_id": "provider", "fingerprint": "provider-v2"},
                            {"queue_id": "tree", "fingerprint": "tree-v2"},
                        ],
                    }
                ),
                encoding="utf-8",
            )
            app.state["tree"] = {
                "push_export_cache": {
                    "source_fingerprint": "source-v2",
                    "manifest": str(manifest_path),
                    "fingerprint": "tree-v2",
                }
            }
            with mock.patch.object(
                gui, "manifest_item_files_match", return_value=True
            ):
                item = app._cached_manifest_item("tree", "source-v2")

        self.assertEqual(item["queue_id"], "tree")

    def test_failed_unreal_recovery_never_calls_blender_export(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        app.cfg = {}
        app._active_production_source_manifest = mock.Mock(
            content_hash="production-v2"
        )
        provider = {
            "schema_version": 1,
            "queue_id": "provider.spm",
            "source_fingerprint": "provider-source-v1",
            "fingerprint": "provider-item-v1",
            "blend": "provider.blend",
            "depends_on_queue_ids": [],
        }
        tree = {
            "schema_version": 1,
            "queue_id": "tree.spm",
            "source_fingerprint": "tree-source-v1",
            "fingerprint": "tree-item-v1",
            "blend": "tree.blend",
            "depends_on_queue_ids": ["provider.spm"],
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            parent_path = Path(temp_dir) / "parent.json"
            parent_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "report_path": str(Path(temp_dir) / "parent_report.json"),
                        "items": [provider, tree],
                    }
                ),
                encoding="utf-8",
            )
            request = {
                "parent_manifest": str(parent_path),
                "parent_report": str(Path(temp_dir) / "parent_report.json"),
                "selected_queue_ids": ["tree.spm"],
                "source_records": {
                    "provider.spm": {
                        "fingerprint": "provider-source-v1",
                        "snapshot": {},
                    },
                    "tree.spm": {
                        "fingerprint": "tree-source-v1",
                        "snapshot": {},
                    },
                },
            }

            def recovered(parent_item, **kwargs):
                result = dict(parent_item)
                result.update(
                    {
                        "source_fingerprint": "current-source",
                        "fingerprint": parent_item["queue_id"] + "-v2",
                        "verify_existing_assets": not kwargs["selected"],
                        "recovery": {
                            "parent_manifest": str(parent_path),
                            "old_code_revision": "old",
                            "new_code_revision": "new",
                        },
                    }
                )
                return result

            app._push_unreal_code_paths = mock.Mock(return_value=[])
            app._push_rebindable_unreal_code_paths = mock.Mock(return_value=[])
            app._source_push_fingerprint = mock.Mock(return_value="current-source")
            app._run_headless_import_items = mock.Mock(return_value=True)
            app._export_manifest_item = mock.Mock(
                side_effect=AssertionError("recovery must not run Blender export")
            )
            app.state = {
                "provider.spm": {
                    "push_source_fingerprint_cache": {
                        "fingerprint": "current-source",
                        "snapshot": {},
                    }
                },
                "tree.spm": {
                    "push_source_fingerprint_cache": {
                        "fingerprint": "current-source",
                        "snapshot": {},
                    }
                },
            }
            with mock.patch.object(
                gui, "recover_manifest_item", side_effect=recovered
            ), mock.patch.object(gui, "save_state"):
                result = app._run_failed_unreal_recovery(
                    [{"spm": Path("tree.spm"), "checked": True}],
                    [request],
                    emit_done=False,
                )

        self.assertTrue(result)
        app._export_manifest_item.assert_not_called()
        pending = app._run_headless_import_items.call_args.args[0]
        self.assertEqual(
            [item["queue_id"] for item in pending],
            ["provider.spm", "tree.spm"],
        )
        self.assertTrue(pending[0]["verify_existing_assets"])
        self.assertFalse(pending[1]["verify_existing_assets"])


if __name__ == "__main__":
    unittest.main()
