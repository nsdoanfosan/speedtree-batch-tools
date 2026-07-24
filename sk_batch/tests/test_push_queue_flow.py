import ast
import queue
import json
import sys
import tempfile
import threading
import unittest
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path
from unittest import mock


SK_BATCH_DIR = Path(__file__).resolve().parents[1]
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

    @staticmethod
    def targets(*names):
        return [{"spm": Path(name), "checked": True} for name in names]

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

        blender_job_strings = {
            node.value
            for node in ast.walk(gui_functions["_job_blender"])
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        }
        self.assertIn("--speedtree-spm", blender_job_strings)
        self.assertIn("--canonical-spm", blender_job_strings)

        bwr_source = (
            SK_BATCH_DIR / "jobs" / "bwr_headless_job.py"
        ).read_text(encoding="utf-8")
        self.assertIn("name_stem=speedtree_spm.stem", bwr_source)
        self.assertIn("settings.name_stem = canonical_spm.stem", bwr_source)
        self.assertNotIn("canonical_spm.name[3:]", bwr_source)
        self.assertIn("export_collection_contract_issues()", bwr_source)
        self.assertIn("orphan_owned_export_empty:", bwr_source)

        push_source = (
            SK_BATCH_DIR / "jobs" / "send2ue_push_job.py"
        ).read_text(encoding="utf-8")
        self.assertIn("resolve_cluster_spm_pair(spm_path)", push_source)
        sync_call = push_source.index("utilities.sync_unreal_mesh_folder_path()")
        folder_read = push_source.index(
            "folder = scene_props.unreal_mesh_folder_path", sync_call
        )
        self.assertLess(sync_call, folder_read)
        self.assertIn("orphan_owned_export_empty:", push_source)
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

    def test_full_pipeline_does_not_forward_failed_items_to_later_phases(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        targets = self.targets("SK_bad_spm.spm", "SK_bad_blend.spm", "SK_good.spm")
        calls = []

        def fake_batch(phase, phase_targets, emit_done=False):
            calls.append((phase, [item["spm"].name for item in phase_targets]))
            if phase == "spm":
                app._phase_failed_items = {"SK_bad_spm.spm"}
            elif phase == "blender":
                app._phase_failed_items = {"SK_bad_blend.spm"}
            else:
                app._phase_failed_items = set()
            app._phase_abort_reason = None
            return True

        app._run_batch = mock.Mock(side_effect=fake_batch)
        app._run_full_pipeline(targets)

        self.assertEqual(calls[0][1], ["SK_bad_spm.spm", "SK_bad_blend.spm", "SK_good.spm"])
        self.assertEqual(calls[1][1], ["SK_bad_blend.spm", "SK_good.spm"])
        self.assertEqual(calls[2][1], ["SK_good.spm"])
        final_progress = [
            payload for kind, payload in list(app.ui_queue.queue)
            if kind == "progress"
        ][-1]
        self.assertIn("실패/준비 제외 2개", final_progress)

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


if __name__ == "__main__":
    unittest.main()
