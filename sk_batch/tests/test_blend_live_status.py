import os
import queue
import tempfile
import threading
import unittest
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path
from unittest import mock


SK_BATCH_DIR = Path(__file__).resolve().parents[1]


def load_gui_module():
    path = SK_BATCH_DIR / "sk_batch_gui.pyw"
    loader = SourceFileLoader("sk_batch_gui_blend_status_test", str(path))
    spec = spec_from_loader(loader.name, loader)
    module = module_from_spec(spec)
    loader.exec_module(module)
    return module


class FakeVar:
    def __init__(self, value):
        self.value = value

    def get(self):
        return self.value


class FakeTree:
    def __init__(self):
        self.rows = {}

    @staticmethod
    def get_children():
        return ()

    @staticmethod
    def delete(_iid):
        return None

    def insert(self, _parent, _where, iid, text, values):
        self.rows[iid] = {"text": text, "values": values}


class FakeCheckedRows:
    @staticmethod
    def sync_after_reload():
        return None


class BlendLiveStatusTests(unittest.TestCase):
    def make_app(self, gui):
        app = gui.App.__new__(gui.App)
        app.state = {}
        app.state_lock = threading.RLock()
        app.ui_queue = queue.Queue()
        return app

    @staticmethod
    def set_time(path, nanoseconds):
        os.utime(path, ns=(nanoseconds, nanoseconds))

    def test_live_status_distinguishes_missing_stale_and_current(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            spm = root / "SK_tree_test_01.spm"
            blend = spm.with_suffix(".blend")
            spm.write_bytes(b"spm")

            self.assertIn("생성 필요", app._blend_status_text(spm))

            blend.write_bytes(b"blend")
            self.set_time(blend, 1_000_000_000)
            self.set_time(spm, 2_000_000_000)
            self.assertIn("교체 필요", app._blend_status_text(spm))

            report = root / "reports" / (
                "SK_tree_test_01_speedtree_repair_pipeline_report_codex.json"
            )
            report.parent.mkdir()
            report.write_text(
                '{"texture_normalization": {"status": "ok", "missing": []}}',
                encoding="utf-8",
            )
            self.set_time(blend, 3_000_000_000)
            self.assertEqual(app._blend_status_text(spm), "최신 ✓")

    def test_scan_replaces_saved_complete_label_with_live_stale_status(self):
        gui = load_gui_module()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            spm = root / "SK_tree_test_02.spm"
            blend = spm.with_suffix(".blend")
            spm.write_bytes(b"new spm")
            blend.write_bytes(b"old blend")
            self.set_time(blend, 1_000_000_000)
            self.set_time(spm, 2_000_000_000)

            app = self.make_app(gui)
            app.root_var = FakeVar(str(root))
            app.cfg = {"root": str(root)}
            app._collect_cfg = lambda: dict(app.cfg)
            app.spm_calibration_signature = "test"
            app.tree = FakeTree()
            app.items = {}
            app.checked_rows = FakeCheckedRows()
            app.log = mock.Mock()
            app.state[str(spm)] = {"blend_status": "완료 (wind TREE)"}

            with mock.patch.object(gui, "scan_sk_spms", return_value=[spm]), mock.patch.object(
                gui, "save_config"
            ), mock.patch.object(gui, "save_state"):
                app.scan()

            status = app.state[str(spm)]["blend_status"]
            self.assertIn("교체 필요", status)
            self.assertIn("② Blender Repair 다시 실행", status)


if __name__ == "__main__":
    unittest.main()
