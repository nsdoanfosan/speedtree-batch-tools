import importlib.machinery
import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


REPO_DIR = Path(__file__).resolve().parents[1]
LAUNCHER_PATH = REPO_DIR / "speedtree_batch_tools_gui.pyw"


def load_launcher_module():
    loader = importlib.machinery.SourceFileLoader(
        "_speedtree_batch_tools_test_launcher", str(LAUNCHER_PATH)
    )
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[loader.name] = module
    loader.exec_module(module)
    return module


class IntegratedLauncherTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.launcher = load_launcher_module()

    def test_all_tool_entry_points_exist(self):
        self.assertEqual(
            [tool.label for tool in self.launcher.TOOLS],
            ["SK Batch", "SPM Generator Sync", "PCG ST9 Texture"],
        )
        for tool in self.launcher.TOOLS:
            with self.subTest(tool=tool.label):
                self.assertTrue(tool.script.is_file())
                self.assertTrue(tool.launcher.is_file())

    def test_each_tool_module_exposes_app(self):
        for tool in self.launcher.TOOLS:
            with self.subTest(tool=tool.label):
                module = self.launcher.load_tool_module(tool)
                self.assertTrue(callable(module.App))

    def test_tab_adapter_absorbs_window_setters(self):
        adapter = self.launcher.ToolTab
        dummy = object()
        self.assertIsNone(adapter.title(dummy, "ignored"))
        self.assertEqual(adapter.geometry(dummy, "100x100"), "")
        self.assertIsNone(adapter.minsize(dummy, 800, 600))

    def test_batch_file_launches_integrated_gui(self):
        text = (REPO_DIR / "SpeedTree_Batch_Tools.bat").read_text(encoding="utf-8")
        self.assertIn("speedtree_batch_tools_gui.pyw", text)
        self.assertNotIn("call \"SK_Batch.bat\"", text)

    def test_integrated_app_icon_assets_exist(self):
        self.assertTrue(self.launcher.ICON_PNG.is_file())
        self.assertTrue(self.launcher.ICON_ICO.is_file())
        self.assertEqual(
            self.launcher.APP_USER_MODEL_ID,
            "PARK.SpeedTree.BatchTools",
        )

    def test_find_matches_tree_text_and_columns_case_insensitively(self):
        class FakeTree:
            children = {"": ("folder",), "folder": ("file",), "file": ()}
            items = {
                "folder": {"text": "Broadleaf", "values": ("폴더",)},
                "file": {"text": "Tree_01.spm", "values": ("MASTER", "최신")},
            }

            def get_children(self, parent=""):
                return self.children[parent]

            def item(self, iid):
                return self.items[iid]

        tree = FakeTree()
        self.assertEqual(self.launcher.tree_rows(tree), ["folder", "file"])
        self.assertEqual(
            self.launcher.matching_tree_rows(tree, "tree_01"), ["file"]
        )
        self.assertEqual(
            self.launcher.matching_tree_rows(tree, "master"), ["file"]
        )
        self.assertEqual(self.launcher.matching_tree_rows(tree, "  "), [])

    def test_delete_candidates_include_file_rows_but_not_folders_or_aggregates(self):
        class FakeTree:
            def __init__(self, selected):
                self.selected = selected

            def selection(self):
                return self.selected

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sk_file = root / "SK_Tree.spm"
            sync_file = root / "Tree.spm"
            child_file = root / "Cluster.spm"
            for path in (sk_file, sync_file, child_file):
                path.touch()

            sk_app = type("App", (), {})()
            sk_app.tree = FakeTree(("folder", "sk"))
            sk_app.items = {"sk": {"spm": sk_file}}
            sk_app.row_copy_paths = {"folder": [root], "sk": [sk_file]}
            self.assertEqual(
                self.launcher.selected_file_paths_for_app(sk_app), [sk_file]
            )

            sync_app = type("App", (), {})()
            sync_app.tree = FakeTree(("folder", "sync"))
            sync_app.item_meta = {
                "folder": {"kind": "folder", "folder": root},
                "sync": {"kind": "spm", "folder": root, "file": sync_file.name},
            }
            self.assertEqual(
                self.launcher.selected_file_paths_for_app(sync_app), [sync_file]
            )

            pcg_app = type("App", (), {})()
            pcg_app.tree = FakeTree(("aggregate", "child", "helper"))
            pcg_app.items = {"aggregate": {"item": {"name": "Tree"}}}
            pcg_app.row_copy_paths = {
                "aggregate": [sk_file, child_file],
                "child": [child_file],
                "helper": [root],
            }
            self.assertEqual(
                self.launcher.selected_file_paths_for_app(pcg_app), [child_file]
            )

    def test_confirmed_delete_unlinks_once_and_refreshes_the_active_tool(self):
        class FakeTree:
            def __init__(self):
                self.deleted = []

            def selection(self):
                return ("file",)

            def delete(self, iid):
                self.deleted.append(iid)

        class FakeVar:
            def __init__(self):
                self.value = ""

            def set(self, value):
                self.value = value

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "delete_me.spm"
            path.touch()
            app = type("App", (), {})()
            app.tree = FakeTree()
            app.items = {"file": {"spm": path}}
            app.refresh_calls = 0

            def refresh():
                app.refresh_calls += 1

            app.refresh = refresh
            owner = object.__new__(self.launcher.IntegratedApp)
            owner.root = object()
            owner.status_var = FakeVar()
            owner.current_app = lambda: app

            with mock.patch.object(
                self.launcher.messagebox, "askyesno", return_value=True
            ) as warning:
                result = owner.delete_selected_files()

            self.assertEqual(result, "break")
            warning.assert_called_once()
            self.assertFalse(path.exists())
            self.assertEqual(app.refresh_calls, 1)
            self.assertEqual(app.tree.deleted, ["file"])
            self.assertEqual(owner.status_var.value, "파일 삭제 완료 · 1개")

    def test_delete_delegates_atlas_target_rows_before_file_deletion(self):
        tree = object()
        app = type("App", (), {})()
        app.tree = tree
        app.handle_delete_key = mock.Mock(return_value=True)
        owner = object.__new__(self.launcher.IntegratedApp)
        owner.root = type("Root", (), {"focus_get": lambda self: tree})()
        owner.current_app = lambda: app

        with mock.patch.object(
            self.launcher, "selected_file_paths_for_app"
        ) as file_delete_candidates:
            result = owner.delete_selected_files(event=object())

        self.assertEqual(result, "break")
        app.handle_delete_key.assert_called_once_with()
        file_delete_candidates.assert_not_called()

    def test_delete_is_reported_failed_when_file_still_exists_after_unlink(self):
        class StubbornPath:
            def unlink(self):
                return None

            def exists(self):
                return True

            def __str__(self):
                return r"D:\Trees\Cluster\Cluster_Tree_elm_01.spm"

        app = type("App", (), {})()
        app.tree = type("Tree", (), {"selection": lambda self: ("file",)})()
        app.refresh = mock.Mock()
        owner = object.__new__(self.launcher.IntegratedApp)
        owner.root = object()
        owner.status_var = type(
            "Var", (), {"set": lambda self, value: setattr(self, "value", value)}
        )()
        owner.current_app = lambda: app

        with mock.patch.object(
            self.launcher, "selected_file_paths_for_app",
            return_value=[StubbornPath()],
        ), mock.patch.object(
            self.launcher.messagebox, "askyesno", return_value=True
        ), mock.patch.object(
            self.launcher.messagebox, "showerror"
        ) as error_dialog:
            result = owner.delete_selected_files()

        self.assertEqual(result, "break")
        app.refresh.assert_not_called()
        error_dialog.assert_called_once()
        self.assertIn(
            "삭제 호출 후에도 파일이 남아 있습니다",
            error_dialog.call_args.args[1],
        )
        self.assertEqual(
            owner.status_var.value,
            "파일 삭제 실패 · 파일이 그대로 보존되었습니다",
        )



if __name__ == "__main__":
    unittest.main()
