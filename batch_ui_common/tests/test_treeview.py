import tempfile
import unittest
from pathlib import Path

from batch_ui_common import (
    CheckedRowController,
    clipboard_text,
    copy_paths_to_clipboard,
    copy_selected_row_paths,
    selected_row_paths,
)


class CheckedRowControllerTests(unittest.TestCase):
    def make_controller(self, entries):
        redraws = []

        def redraw(iid, entry):
            redraws.append((iid, entry["checked"]))

        return CheckedRowController(entries, redraw), redraws

    def test_initial_all_checked_click_keeps_only_clicked_row(self):
        entries = {name: {"checked": True} for name in ("a", "b", "c")}
        controller, redraws = self.make_controller(entries)

        self.assertTrue(controller.sync_after_reload())
        self.assertTrue(controller.click("b"))

        self.assertEqual(
            {name: row["checked"] for name, row in entries.items()},
            {"a": False, "b": True, "c": False},
        )
        self.assertEqual(redraws, [("a", False), ("b", True), ("c", False)])
        self.assertFalse(controller.armed)

    def test_clicks_after_exclusive_click_toggle_individual_rows(self):
        entries = {name: {"checked": True} for name in ("a", "b", "c")}
        controller, _redraws = self.make_controller(entries)
        controller.sync_after_reload()
        controller.click("b")

        controller.click("c")
        controller.click("b")

        self.assertEqual(
            {name: row["checked"] for name, row in entries.items()},
            {"a": False, "b": False, "c": True},
        )
        self.assertFalse(controller.click("missing"))

    def test_set_all_true_rearms_exclusive_click(self):
        entries = {name: {"checked": False} for name in ("a", "b", "c")}
        controller, _redraws = self.make_controller(entries)

        controller.set_all(True)
        self.assertTrue(controller.armed)
        controller.click("a")

        self.assertEqual(
            {name: row["checked"] for name, row in entries.items()},
            {"a": True, "b": False, "c": False},
        )
        controller.set_all(False)
        self.assertFalse(controller.armed)

    def test_dynamic_mapping_reload_uses_current_entries(self):
        entries = {"old": {"checked": False}}
        controller, redraws = self.make_controller(entries)
        self.assertFalse(controller.sync_after_reload())

        entries.clear()
        entries.update({"new-a": {"checked": True}, "new-b": {"checked": True}})

        self.assertTrue(controller.sync_after_reload())
        controller.click("new-b")
        self.assertEqual(
            entries,
            {"new-a": {"checked": False}, "new-b": {"checked": True}},
        )
        self.assertEqual(redraws, [("new-a", False), ("new-b", True)])


class FakeRoot:
    def __init__(self):
        self.value = "existing"
        self.clear_calls = 0
        self.append_calls = []
        self.update_calls = 0

    def clipboard_clear(self):
        self.clear_calls += 1
        self.value = ""

    def clipboard_append(self, value):
        self.append_calls.append(value)
        self.value += value

    def update_idletasks(self):
        self.update_calls += 1


class FakeTree:
    def __init__(self, selection):
        self._selection = tuple(selection)

    def selection(self):
        return self._selection


class ClipboardTests(unittest.TestCase):
    def test_clipboard_text_is_raw_absolute_and_case_insensitive_deduplicated(self):
        with tempfile.TemporaryDirectory() as temporary:
            first = Path(temporary) / "Tree_01.spm"
            duplicate = Path(temporary) / "TREE_01.SPM"
            second = Path(temporary) / "Tree 02.spm"

            text = clipboard_text([first, "", None, duplicate, second])
            values = text.splitlines()

            self.assertEqual(values, [str(first.resolve()), str(second.resolve())])
            self.assertTrue(all(Path(value).is_absolute() for value in values))
            self.assertNotIn('"', text)

    def test_copy_paths_to_clipboard_returns_count_and_leaves_clipboard_on_empty(self):
        root = FakeRoot()
        self.assertEqual(copy_paths_to_clipboard(root, []), 0)
        self.assertEqual(root.value, "existing")
        self.assertEqual(root.clear_calls, 0)

        with tempfile.TemporaryDirectory() as temporary:
            paths = [Path(temporary) / "a.spm", Path(temporary) / "b.spm"]
            self.assertEqual(copy_paths_to_clipboard(root, paths), 2)
            self.assertEqual(root.value, clipboard_text(paths))
            self.assertEqual(root.clear_calls, 1)
            self.assertEqual(root.update_calls, 1)

    def test_selected_rows_are_flattened_and_copied_with_fake_objects(self):
        with tempfile.TemporaryDirectory() as temporary:
            first = Path(temporary) / "a.spm"
            second = Path(temporary) / "b.spm"
            rows = {
                "row-a": {"paths": first},
                "row-b": {"paths": [second, first]},
            }
            tree = FakeTree(("row-b", "missing", "row-a"))
            get_paths = lambda row: row["paths"]

            selected = selected_row_paths(tree, rows, get_paths)
            self.assertEqual(selected, [second, first, first])

            root = FakeRoot()
            count = copy_selected_row_paths(root, tree, rows, get_paths)
            self.assertEqual(count, 2)
            self.assertEqual(root.value, clipboard_text([second, first]))


if __name__ == "__main__":
    unittest.main()
