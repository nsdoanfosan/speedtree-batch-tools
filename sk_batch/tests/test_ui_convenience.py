import tempfile
import unittest
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path


SK_BATCH_DIR = Path(__file__).resolve().parents[1]


def load_gui_module():
    path = SK_BATCH_DIR / "sk_batch_gui.pyw"
    loader = SourceFileLoader("sk_batch_gui_convenience_test", str(path))
    spec = spec_from_loader(loader.name, loader)
    module = module_from_spec(spec)
    loader.exec_module(module)
    return module


class FakeRoot:
    def __init__(self):
        self.clipboard = ""

    def clipboard_clear(self):
        self.clipboard = ""

    def clipboard_append(self, value):
        self.clipboard += value

    @staticmethod
    def update_idletasks():
        return None


class FakeVar:
    def __init__(self):
        self.value = ""

    def set(self, value):
        self.value = value


class FakeTree:
    def __init__(self):
        self.labels = {}
        self.selected = ()
        self.focused = None
        self.has_keyboard_focus = False

    @staticmethod
    def identify_region(_x, _y):
        return "tree"

    @staticmethod
    def identify_row(y):
        return y

    def item(self, iid, **values):
        if "text" in values:
            self.labels[iid] = values["text"]

    def selection_set(self, iid):
        self.selected = (iid,)

    def selection(self):
        return self.selected

    def focus(self, iid):
        self.focused = iid

    def focus_set(self):
        self.has_keyboard_focus = True


class SkBatchUiConvenienceTests(unittest.TestCase):
    def test_first_click_isolates_row_and_ctrl_c_copies_that_spm_path(self):
        gui = load_gui_module()
        with tempfile.TemporaryDirectory() as temporary:
            spms = [Path(temporary) / f"SK_tree_{index}.spm" for index in range(3)]
            app = gui.App.__new__(gui.App)
            app.root = FakeRoot()
            app.tree = FakeTree()
            app.worker = None
            app.cell_editor = None
            app.progress_var = FakeVar()
            app.items = {
                str(spm): {
                    "spm": spm,
                    "checked": True,
                    "manual_bones_locked": False,
                }
                for spm in spms
            }
            app.checked_rows = gui.CheckedRowController(
                app.items, app._redraw_checked_row
            )
            app.checked_rows.sync_after_reload()

            clicked = str(spms[1])
            event = type("Event", (), {"x": 0, "y": clicked})()
            self.assertEqual(app._on_click(event), "break")

            self.assertEqual(
                [entry["checked"] for entry in app.items.values()],
                [False, True, False],
            )
            self.assertEqual(app.tree.selection(), (clicked,))
            self.assertEqual(app.tree.focused, clicked)
            self.assertTrue(app.tree.has_keyboard_focus)

            self.assertEqual(app.copy_selected_paths(), "break")
            self.assertEqual(app.root.clipboard, str(spms[1].resolve()))
            self.assertIn("1개", app.progress_var.value)


if __name__ == "__main__":
    unittest.main()
