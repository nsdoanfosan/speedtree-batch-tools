import json
import os
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
sys.path.insert(0, str(SK_BATCH_DIR))

from blender_resume_receipt import (
    BlenderResumeReceiptError,
    build_blender_resume_receipt,
    validate_blender_resume_receipt,
)


def load_gui_module():
    path = SK_BATCH_DIR / "sk_batch_gui.pyw"
    loader = SourceFileLoader("sk_batch_gui_blender_resume_test", str(path))
    spec = spec_from_loader(loader.name, loader)
    module = module_from_spec(spec)
    loader.exec_module(module)
    return module


class BlenderResumeReceiptTests(unittest.TestCase):
    @staticmethod
    def write(path, payload=b"x"):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        return path

    def test_receipt_reuses_only_unchanged_bound_files_and_settings(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            spm = self.write(root / "SK_tree.spm", b"spm")
            blend = self.write(root / "SK_tree.blend", b"blend")
            settings = {"wind_override": "TREE"}
            state = {
                "current": True,
                "push_ready": True,
                "kind": "ready",
                "reason": "ready",
            }
            receipt = build_blender_resume_receipt(
                spm,
                tracked_paths=[blend],
                content_key_paths=[blend],
                settings=settings,
                repair_state=state,
            )

            self.assertEqual(
                validate_blender_resume_receipt(
                    json.loads(json.dumps(receipt)),
                    spm,
                    settings=settings,
                ),
                state,
            )

            original = blend.stat()
            blend.write_bytes(b"BLEND")
            os.utime(
                blend,
                ns=(original.st_atime_ns, original.st_mtime_ns),
            )
            with self.assertRaisesRegex(
                BlenderResumeReceiptError,
                "changed",
            ):
                validate_blender_resume_receipt(
                    receipt,
                    spm,
                    settings=settings,
                )

    def test_receipt_rejects_force_relevant_setting_drift(self):
        with tempfile.TemporaryDirectory() as temporary:
            spm = self.write(Path(temporary) / "SK_tree.spm")
            receipt = build_blender_resume_receipt(
                spm,
                tracked_paths=[],
                settings={"wind_override": "TREE"},
                repair_state={
                    "current": True,
                    "push_ready": True,
                    "kind": "ready",
                    "reason": "ready",
                },
            )

            with self.assertRaisesRegex(
                BlenderResumeReceiptError,
                "settings changed",
            ):
                validate_blender_resume_receipt(
                    receipt,
                    spm,
                    settings={"wind_override": "BUSH"},
                )

    def test_receipt_invalidates_when_a_bound_missing_artifact_appears(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            spm = self.write(root / "SK_tree.spm")
            optional = root / "T_tree_color.tga"
            settings = {"wind_override": "TREE"}
            receipt = build_blender_resume_receipt(
                spm,
                tracked_paths=[optional],
                settings=settings,
                repair_state={
                    "current": True,
                    "push_ready": True,
                    "kind": "ready",
                    "reason": "ready",
                },
            )

            self.write(optional, b"new texture")

            with self.assertRaisesRegex(
                BlenderResumeReceiptError,
                "changed",
            ):
                validate_blender_resume_receipt(
                    receipt,
                    spm,
                    settings=settings,
                )


class BlenderResumeQueueTests(unittest.TestCase):
    @staticmethod
    def make_app(gui):
        app = gui.App.__new__(gui.App)
        app.stop_flag = threading.Event()
        app.ui_queue = queue.Queue()
        app.state = {}
        app.state_lock = threading.RLock()
        app.procs_lock = threading.Lock()
        app.active_procs = set()
        app.cfg = {"blender_parallel_jobs": 1}
        app.log = mock.Mock()
        app.force_rerun = False
        app.items = {}
        app._active_batch_items = None
        app._active_batch_inventory = None
        return app

    def test_completed_receipts_are_removed_before_worker_progress(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            completed = root / "Tree_a" / "SK_tree_a.spm"
            pending = root / "Tree_b" / "SK_tree_b.spm"
            targets = [
                {"spm": completed, "wind_override": "auto"},
                {"spm": pending, "wind_override": "auto"},
            ]
            app.items = {
                str(item["spm"]): item for item in targets
            }
            app._validated_blender_resume_state = mock.Mock(
                side_effect=[
                    {
                        "current": True,
                        "push_ready": True,
                        "kind": "ready",
                        "reason": "ready",
                    },
                    BlenderResumeReceiptError("missing"),
                ]
            )
            app._publish_current_repair_skip = mock.Mock(return_value=True)
            app._job_blender = mock.Mock()

            with mock.patch.object(
                gui,
                "LOG_DIR",
                root / "logs",
            ), mock.patch.object(gui, "save_state"):
                result = app._run_batch(
                    "blender",
                    targets,
                    emit_done=False,
                )

        self.assertTrue(result)
        app._job_blender.assert_called_once_with(
            str(pending),
            pending,
            targets[1],
        )
        app._publish_current_repair_skip.assert_called_once_with(
            str(completed),
            completed,
            mock.ANY,
            validated_resume_receipt=None,
        )
        progress_events = [
            payload
            for event, payload in list(app.ui_queue.queue)
            if event == "batch_progress"
        ]
        self.assertEqual(progress_events[0], (0, 1))
        self.assertEqual(progress_events[-1], (1, 1))

    def test_force_rerun_bypasses_resume_prefilter(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        app.force_rerun = True
        target = {"spm": Path("SK_tree.spm")}
        app._validated_blender_resume_state = mock.Mock()

        runnable, skipped = app._prefilter_blender_resume_targets([target])

        self.assertEqual(runnable, [target])
        self.assertEqual(skipped, [])
        app._validated_blender_resume_state.assert_not_called()

    def test_second_identical_run_schedules_zero_blender_workers(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            spm = root / "Tree" / "SK_tree.spm"
            target = {"spm": spm, "wind_override": "auto"}
            app.items = {str(spm): target}
            receipt = {"receipt_sha256": "saved"}
            app.state[str(spm)] = {
                "blend_resume_receipt": receipt,
            }
            app._validated_blender_resume_state = mock.Mock(return_value={
                "current": True,
                "push_ready": True,
                "kind": "ready",
                "reason": "ready",
            })
            app._publish_current_repair_skip = mock.Mock(return_value=True)
            app._job_blender = mock.Mock(
                side_effect=AssertionError("completed row must not get a worker")
            )

            with mock.patch.object(
                gui,
                "LOG_DIR",
                root / "logs",
            ), mock.patch.object(gui, "save_state"):
                result = app._run_batch(
                    "blender",
                    [target],
                    emit_done=False,
                )

        self.assertTrue(result)
        app._job_blender.assert_not_called()
        app._publish_current_repair_skip.assert_called_once_with(
            str(spm),
            spm,
            mock.ANY,
            validated_resume_receipt=receipt,
        )
        progress_events = [
            payload
            for event, payload in list(app.ui_queue.queue)
            if event == "batch_progress"
        ]
        self.assertEqual(progress_events, [(0, 0)])

    def test_initial_live_audit_migrates_current_row_to_resume_receipt(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        spm = Path("SK_tree_existing.spm")
        iid = str(spm)
        signature = ((1, 2),)
        app._scan_generation = 3
        app._live_poll_active = True
        app.items = {iid: {"spm": spm}}
        app.state = {iid: {}}
        repair_state = {
            "current": True,
            "push_ready": True,
            "kind": "ready",
            "reason": "ready",
        }
        receipt = {"receipt_sha256": "migrated"}
        app._repair_output_state = mock.Mock(return_value=repair_state)
        app._blend_status_from_repair_state = mock.Mock(
            return_value="최신 ✓"
        )
        app._current_push_status_text = mock.Mock(return_value="준비됨 ✓")
        app._build_blender_resume_receipt = mock.Mock(return_value=receipt)
        snapshot = [
            (iid, spm, (), signature, None, {"wind_override": "auto"})
        ]

        with mock.patch.object(
            gui.App,
            "_live_status_signature",
            return_value=signature,
        ), mock.patch.object(
            gui.App,
            "_reported_texture_paths",
            return_value=(),
        ), mock.patch.object(gui, "save_state"), mock.patch.object(
            gui,
            "save_leaf_contract_cache",
        ):
            app._poll_live_file_status_worker(3, snapshot)

        app._repair_output_state.assert_called_once_with(spm)
        self.assertEqual(app.state[iid]["blend_resume_receipt"], receipt)
        self.assertEqual(app.items[iid]["blend_resume_receipt"], receipt)


if __name__ == "__main__":
    unittest.main()
