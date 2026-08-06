import copy
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


def load_gui_module():
    path = SK_BATCH_DIR / "sk_batch_gui.pyw"
    loader = SourceFileLoader("sk_batch_gui_state_save_test", str(path))
    spec = spec_from_loader(loader.name, loader)
    module = module_from_spec(spec)
    loader.exec_module(module)
    return module


class StateSaveBatchingTests(unittest.TestCase):
    @staticmethod
    def make_app(gui):
        app = gui.App.__new__(gui.App)
        app.stop_flag = threading.Event()
        app.ui_queue = queue.Queue()
        app.state = {}
        app.state_lock = threading.RLock()
        app.procs_lock = threading.Lock()
        app.active_procs = set()
        app.cfg = {"spm_parallel_jobs": 1}
        app.log = mock.Mock()
        return app

    def test_spm_skip_updates_save_once_at_phase_boundary(self):
        gui = load_gui_module()
        app = self.make_app(gui)

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            targets = [
                {"spm": root / "Tree_a" / "SK_tree_a.spm", "checked": True},
                {"spm": root / "Tree_b" / "SK_tree_b.spm", "checked": True},
            ]
            app.items = {str(item["spm"]): item for item in targets}
            saved_states = []

            def capture_state(state):
                saved_states.append(copy.deepcopy(state))

            with mock.patch.object(
                gui, "LOG_DIR", root / "logs"
            ), mock.patch.object(
                gui, "save_state", side_effect=capture_state
            ):
                result = app._run_batch("spm", targets, emit_done=False)

        self.assertTrue(result)
        self.assertEqual(len(saved_states), 1)
        for item in targets:
            self.assertIn(
                "건너뜀",
                saved_states[0][str(item["spm"])]["spm_status"],
            )

    def test_failure_is_saved_immediately_while_routine_updates_are_batched(self):
        gui = load_gui_module()
        app = self.make_app(gui)

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            routine = root / "Tree_a" / "SK_tree_a.spm"
            failed = root / "Tree_b" / "SK_tree_b.spm"
            for spm in (routine, failed):
                spm.parent.mkdir(parents=True, exist_ok=True)
                spm.write_bytes(b"spm")
            targets = [
                {"spm": routine, "checked": True},
                {"spm": failed, "checked": True},
            ]
            app.items = {str(item["spm"]): item for item in targets}
            saved_states = []

            def fake_spm_job(iid, spm):
                with app.state_lock:
                    app.state.setdefault(iid, {})["spm_status"] = "routine update"
                    app._save_state_after_phase_update()
                if spm == failed:
                    raise RuntimeError("intentional failure")

            def capture_state(state):
                saved_states.append(copy.deepcopy(state))

            app._job_spm = mock.Mock(side_effect=fake_spm_job)
            with mock.patch.object(
                gui, "LOG_DIR", root / "logs"
            ), mock.patch.object(
                gui, "save_state", side_effect=capture_state
            ):
                result = app._run_batch("spm", targets, emit_done=False)

        self.assertTrue(result)
        self.assertEqual(len(saved_states), 2)
        self.assertEqual(
            saved_states[0][str(failed)]["spm_status_error"]["kind"],
            "data_error",
        )
        self.assertEqual(
            saved_states[-1][str(failed)]["spm_status_error"]["kind"],
            "data_error",
        )

    def test_cancelled_unstarted_rows_are_saved_at_phase_exit(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        app.stop_flag.set()

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            spm = root / "Tree" / "SK_tree.spm"
            target = {"spm": spm, "checked": True}
            app.items = {str(spm): target}
            saved_states = []

            def capture_state(state):
                saved_states.append(copy.deepcopy(state))

            with mock.patch.object(
                gui, "LOG_DIR", root / "logs"
            ), mock.patch.object(
                gui, "save_state", side_effect=capture_state
            ):
                result = app._run_batch("spm", [target], emit_done=False)

        self.assertTrue(result)
        self.assertEqual(len(saved_states), 1)
        result_record = saved_states[0][str(spm)]["spm_status_result"]
        self.assertEqual(result_record["kind"], "cancelled")
        self.assertEqual(result_record["outcome"], "cancelled")


if __name__ == "__main__":
    unittest.main()
