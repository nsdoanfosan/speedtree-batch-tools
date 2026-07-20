import threading
import unittest
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path
from unittest import mock


SK_BATCH_DIR = Path(__file__).resolve().parents[1]


def load_gui_module():
    path = SK_BATCH_DIR / "sk_batch_gui.pyw"
    loader = SourceFileLoader("sk_batch_gui_full_scope_test", str(path))
    spec = spec_from_loader(loader.name, loader)
    module = module_from_spec(spec)
    loader.exec_module(module)
    return module


class FakeVar:
    def __init__(self, value=None):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


class FullPipelineScopeTests(unittest.TestCase):
    def test_full_pipeline_ignores_checkmarks_and_queues_the_whole_list(self):
        gui = load_gui_module()
        app = gui.App.__new__(gui.App)
        checked = {"spm": Path("SK_checked.spm"), "checked": True}
        unchecked = {"spm": Path("SK_unchecked.spm"), "checked": False}
        app.items = {"checked": checked, "unchecked": unchecked}
        app._close_cell_editor = mock.Mock()
        app._collect_cfg = mock.Mock(return_value={
            "night_headless": True,
            "push_transport": "rpc",
        })
        app.force_var = FakeVar(False)
        app.stop_flag = threading.Event()
        app.batch_progress = mock.Mock()
        app.batch_progress_var = FakeVar()
        app.btn_check = mock.Mock()
        app.btn_spm = mock.Mock()
        app.btn_blender = mock.Mock()
        app.btn_push = mock.Mock()
        app.btn_all = mock.Mock()
        app.btn_pick_root = mock.Mock()
        app.btn_scan = mock.Mock()
        app.btn_select_all = mock.Mock()
        app.btn_clear_all = mock.Mock()
        app.btn_stop = mock.Mock()
        app.log = mock.Mock()
        worker = mock.Mock()

        with mock.patch.object(
            gui, "calibration_settings_signature", return_value="current"
        ), mock.patch.object(
            gui, "legacy_calibration_settings_signature", return_value="legacy"
        ), mock.patch.object(gui, "save_config"), mock.patch.object(
            gui.threading, "Thread", return_value=worker
        ) as thread, mock.patch.object(gui.messagebox, "showinfo") as showinfo:
            app.start_full_pipeline()

        showinfo.assert_not_called()
        queued_targets = thread.call_args.kwargs["args"][0]
        self.assertEqual(queued_targets, [checked, unchecked])
        worker.start.assert_called_once_with()
        self.assertIn("2개", app.log.call_args.args[0])

    def test_full_pipeline_reports_an_empty_list_not_an_empty_selection(self):
        gui = load_gui_module()
        app = gui.App.__new__(gui.App)
        app.items = {}
        app._close_cell_editor = mock.Mock()

        with mock.patch.object(gui.messagebox, "showinfo") as showinfo:
            app.start_full_pipeline()

        showinfo.assert_called_once_with(
            "SK Batch", "현재 목록에 항목이 없습니다."
        )


if __name__ == "__main__":
    unittest.main()
