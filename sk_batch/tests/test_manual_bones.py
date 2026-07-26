import copy
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

from sk_common import (
    CALIBRATION_CACHE_VERSION,
    file_content_snapshot,
    is_manual_bones_locked,
    set_manual_bones_marker,
)


def load_gui_module():
    path = SK_BATCH_DIR / "sk_batch_gui.pyw"
    loader = SourceFileLoader("sk_batch_gui_test", str(path))
    spec = spec_from_loader(loader.name, loader)
    module = module_from_spec(spec)
    loader.exec_module(module)
    return module


class FakeTree:
    def __init__(self):
        self.values = {}
        self.labels = {}

    def set(self, iid, column, value):
        self.values[(iid, column)] = value

    def item(self, iid, **kwargs):
        if "text" in kwargs:
            self.labels[iid] = kwargs["text"]


class ManualBonesTests(unittest.TestCase):
    def test_full_pipeline_runs_three_phases_in_order_and_emits_one_done(self):
        gui = load_gui_module()
        app = gui.App.__new__(gui.App)
        app.stop_flag = threading.Event()
        app.ui_queue = queue.Queue()
        app.log = mock.Mock()
        app._run_batch = mock.Mock()
        targets = [{"spm": Path("SK_test.spm"), "checked": True}]

        app._run_full_pipeline(targets)

        self.assertEqual(
            app._run_batch.call_args_list,
            [
                mock.call("spm", targets, emit_done=False),
                mock.call("blender", targets, emit_done=False),
                mock.call("push", targets, emit_done=False),
            ],
        )
        queued = []
        while not app.ui_queue.empty():
            queued.append(app.ui_queue.get_nowait())
        self.assertEqual(sum(kind == "done" for kind, _payload in queued), 1)
        self.assertIn(("progress", "전체 자동 완료"), queued)

    def test_blender_button_chain_stops_after_spm_and_blender(self):
        gui = load_gui_module()
        app = gui.App.__new__(gui.App)
        app.stop_flag = threading.Event()
        app.ui_queue = queue.Queue()
        app.log = mock.Mock()
        app._run_batch = mock.Mock()
        targets = [{"spm": Path("SK_test.spm"), "checked": True}]

        app._run_full_pipeline(
            targets,
            terminal_phase="blender",
            selected_scope=True,
        )

        self.assertEqual(
            app._run_batch.call_args_list,
            [
                mock.call("spm", targets, emit_done=False),
                mock.call("blender", targets, emit_done=False),
            ],
        )
        queued = list(app.ui_queue.queue)
        self.assertEqual(sum(kind == "done" for kind, _payload in queued), 1)
        self.assertIn(
            ("progress", "② Blender Repair 연계 실행 완료"),
            queued,
        )

    def test_manual_marker_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            spm = Path(tmp) / "SK_manual_test.spm"
            spm.write_bytes(b"test")
            self.assertFalse(is_manual_bones_locked(spm))
            set_manual_bones_marker(spm, True)
            self.assertTrue(is_manual_bones_locked(spm))
            set_manual_bones_marker(spm, False)
            self.assertFalse(is_manual_bones_locked(spm))

    def test_state_lock_is_recognized_without_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            spm = Path(tmp) / "SK_manual_test.spm"
            self.assertTrue(is_manual_bones_locked(spm, {"manual_bones_locked": True}))

    def test_locked_spm_job_returns_before_launching_any_process(self):
        gui = load_gui_module()
        spm = Path("SK_manual_test.spm")
        iid = str(spm)
        app = gui.App.__new__(gui.App)
        app.items = {iid: {"spm": spm, "manual_bones_locked": True}}
        app.state = {iid: {"spm_summary": "본 282 / 가지 90"}}
        app.state_lock = threading.Lock()
        app.ui_queue = queue.Queue()
        app.log = lambda _message: None

        with mock.patch.object(gui, "save_state"), mock.patch.object(
            app, "_run_limited", side_effect=AssertionError("process must not launch")
        ):
            app._job_spm(iid, spm)

        self.assertIn("① 전체 건너뜀", app.state[iid]["spm_status"])
        self.assertEqual(app.state[iid]["spm_summary"], "본 282 / 가지 90")

    def test_manual_check_uses_verified_count_and_does_not_persist_or_touch_spm(self):
        gui = load_gui_module()
        with tempfile.TemporaryDirectory() as tmp:
            spm = Path(tmp) / "SK_tree_birch_paper_03.spm"
            spm.write_bytes(b"stable-spm")
            iid = str(spm)
            app = gui.App.__new__(gui.App)
            app.items = {iid: {"spm": spm, "manual_bones_locked": True}}
            app.state = {
                iid: {
                    "spm_summary": "SK 미제작",
                    "calibration_cache": {"summary": "SK 미제작"},
                }
            }
            app.state_lock = threading.RLock()
            app.ui_queue = queue.Queue()
            app._blend_status_text = mock.Mock(return_value="최신 ✓")
            app._handoff_ready = mock.Mock(return_value=(True, "준비됨 ✓"))
            app._current_push_status_text = mock.Mock(return_value="미전송")
            before_state = copy.deepcopy(app.state)
            before_source = (
                spm.read_bytes(),
                spm.stat().st_size,
                spm.stat().st_mtime_ns,
            )

            with mock.patch.object(
                gui,
                "manual_bone_status_text",
                return_value=(
                    "수동 본 유지 🔒 · SpeedTree 본 282개 "
                    "(현재 SPM과 일치하는 XML)"
                ),
            ), mock.patch("spm_audit.audit_spm", return_value={}), mock.patch(
                "spm_audit.sk_readiness", return_value={"ready": False}
            ), mock.patch.object(gui, "save_state") as save_mock:
                app._job_check(iid, spm)

            queued = []
            while not app.ui_queue.empty():
                queued.append(app.ui_queue.get_nowait())
            spm_text = next(
                payload[2]
                for kind, payload in queued
                if kind == "cell" and payload[1] == "spm_status"
            )
            self.assertIn("282개", spm_text)
            self.assertNotIn("현재 본 0", spm_text)
            self.assertNotIn("SK 미제작", spm_text)
            self.assertEqual(app.state, before_state)
            self.assertEqual(
                before_source,
                (spm.read_bytes(), spm.stat().st_size, spm.stat().st_mtime_ns),
            )
            save_mock.assert_not_called()

    def test_check_batch_does_not_persist_state_or_dispatch_processing_jobs(self):
        gui = load_gui_module()
        app = gui.App.__new__(gui.App)
        spm = Path("SK_read_only_check.spm")
        target = {"spm": spm, "checked": True}
        app.stop_flag = threading.Event()
        app.state_lock = threading.RLock()
        app.state = {str(spm): {"calibration_cache": {"summary": "kept"}}}
        app.ui_queue = queue.Queue()
        app.cfg = {"check_parallel_jobs": 1}
        app.log = mock.Mock()
        app._job_check = mock.Mock()
        app._job_spm = mock.Mock(side_effect=AssertionError("must not run"))
        app._job_blender = mock.Mock(side_effect=AssertionError("must not run"))
        app._job_push = mock.Mock(side_effect=AssertionError("must not run"))
        before_state = copy.deepcopy(app.state)

        with mock.patch.object(gui, "save_state") as save_mock:
            result = app._run_batch("check", [target], emit_done=False)

        self.assertTrue(result)
        app._job_check.assert_called_once_with(str(spm), spm)
        app._job_spm.assert_not_called()
        app._job_blender.assert_not_called()
        app._job_push.assert_not_called()
        self.assertEqual(app.state, before_state)
        save_mock.assert_not_called()

    def test_unchanged_cached_spm_returns_before_launching_any_process(self):
        gui = load_gui_module()
        with tempfile.TemporaryDirectory() as tmp:
            spm = Path(tmp) / "SK_cached_test.spm"
            spm.write_bytes(b"stable-spm")
            snapshot = file_content_snapshot(spm)
            iid = str(spm)
            signature = "settings-signature"
            app = gui.App.__new__(gui.App)
            app.items = {
                iid: {
                    "spm": spm,
                    "manual_bones_locked": False,
                    "spm_snapshot": snapshot,
                }
            }
            app.state = {
                iid: {
                    "calibration_cache": {
                        "version": CALIBRATION_CACHE_VERSION,
                        "spm_fingerprint": snapshot["fingerprint"],
                        "settings_signature": signature,
                        "status": "calibrated",
                        "summary": "본 12 / 가지 4",
                    }
                }
            }
            app.state_lock = threading.RLock()
            app.ui_queue = queue.Queue()
            app.log = lambda _message: None
            app.force_rerun = False
            app.spm_calibration_signature = signature

            with mock.patch.object(gui, "save_state"), mock.patch.object(
                app, "_run_limited", side_effect=AssertionError("process must not launch")
            ):
                app._job_spm(iid, spm)

            self.assertIn("변경 없음", app.state[iid]["spm_status"])

    def test_successful_job_records_cache_then_next_run_skips(self):
        gui = load_gui_module()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            spm = root / "SK_cache_write_test.spm"
            spm.write_bytes(b"stable-spm")
            iid = str(spm)
            app = gui.App.__new__(gui.App)
            app.items = {
                iid: {
                    "spm": spm,
                    "manual_bones_locked": False,
                    "spm_snapshot": file_content_snapshot(spm),
                }
            }
            app.state = {iid: {}}
            app.state_lock = threading.RLock()
            app.ui_queue = queue.Queue()
            app.log = lambda _message: None
            app.force_rerun = False
            app.spm_calibration_signature = "settings-signature"
            app.cfg = {"spm_parallel_jobs": 1, "spm_verify_timeout": 10}

            def fake_run(
                cmd, _log_name, _timeout, affinity=True, progress_callback=None
            ):
                del affinity
                report_path = Path(cmd[cmd.index("--report") + 1])
                report_path.parent.mkdir(parents=True, exist_ok=True)
                report_path.write_text(
                    json.dumps(
                        [
                            {
                                "status": "calibrated",
                                "total_bones": 12,
                                "warnings": [],
                                "calibration": {
                                    "total_branches": 4,
                                    "capped": False,
                                    "probe_cache_hit": False,
                                },
                            }
                        ]
                    ),
                    encoding="utf-8",
                )
                return 0, root / "job.log"

            with mock.patch.object(gui, "LOG_DIR", root), mock.patch.object(
                gui, "save_state"
            ), mock.patch.object(app, "_run_limited", side_effect=fake_run) as run_mock:
                app._job_spm(iid, spm)
                self.assertEqual(run_mock.call_count, 1)
                command = run_mock.call_args.args[0]
                self.assertEqual(command[1:4], ["-X", "utf8", "-u"])
                app._job_spm(iid, spm)
                self.assertEqual(run_mock.call_count, 1)

            cache = app.state[iid]["calibration_cache"]
            self.assertEqual(cache["status"], "calibrated")
            self.assertEqual(cache["settings_signature"], "settings-signature")

    def test_bone_mode_dropdown_changes_only_the_target_row(self):
        gui = load_gui_module()
        with tempfile.TemporaryDirectory() as tmp:
            first = Path(tmp) / "SK_first.spm"
            second = Path(tmp) / "SK_second.spm"
            first.write_bytes(b"first")
            second.write_bytes(b"second")
            first_iid = str(first)
            second_iid = str(second)
            app = gui.App.__new__(gui.App)
            app.items = {
                first_iid: {
                    "spm": first,
                    "checked": True,
                    "bone_mode": "auto",
                    "manual_bones_locked": False,
                },
                second_iid: {
                    "spm": second,
                    "checked": True,
                    "bone_mode": "auto",
                    "manual_bones_locked": False,
                },
            }
            app.state = {
                first_iid: {"spm_summary": "본 282 / 가지 90"},
                second_iid: {},
            }
            app.tree = FakeTree()
            app.log = lambda _message: None

            with mock.patch.object(gui, "save_state"):
                app._set_bone_mode(first_iid, "manual")

            self.assertTrue(app.items[first_iid]["manual_bones_locked"])
            self.assertEqual(app.items[first_iid]["bone_mode"], "manual")
            self.assertFalse(app.items[second_iid]["manual_bones_locked"])
            self.assertNotIn("manual_bones_locked", app.state[second_iid])
            self.assertEqual(
                app.state[first_iid]["spm_summary"], "본 282 / 가지 90"
            )
            self.assertIn("수동 본 유지", app.tree.values[(first_iid, "bone_mode")])

    def test_wind_dropdown_sets_explicit_value_for_only_the_target_row(self):
        gui = load_gui_module()
        first_iid = "SK_first.spm"
        second_iid = "SK_second.spm"
        app = gui.App.__new__(gui.App)
        app.items = {
            first_iid: {
                "spm": Path(first_iid),
                "wind_override": "auto",
            },
            second_iid: {
                "spm": Path(second_iid),
                "wind_override": "auto",
            },
        }
        app.state = {first_iid: {}, second_iid: {}}
        app.tree = FakeTree()
        app.log = lambda _message: None

        with mock.patch.object(gui, "save_state"):
            app._set_wind_override(first_iid, "TREE")

        self.assertEqual(app.items[first_iid]["wind_override"], "TREE")
        self.assertEqual(app.items[second_iid]["wind_override"], "auto")
        self.assertEqual(app.state[first_iid]["wind_override"], "TREE")
        self.assertNotIn("wind_override", app.state[second_iid])


if __name__ == "__main__":
    unittest.main()
