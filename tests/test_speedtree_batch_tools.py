import importlib.machinery
import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

import speedtree_error_log


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

    def test_top_level_pcg_tab_navigation_initializes_real_entry_point(self):
        pcg_index = 2
        pcg_spec = self.launcher.TOOLS[pcg_index]
        pcg_module = self.launcher.load_tool_module(pcg_spec)
        embedded_app = object()

        class FakeRoot:
            def __init__(self):
                self.idle_callbacks = []

            def after_idle(self, callback):
                self.idle_callbacks.append(callback)

            def update_idletasks(self):
                pass

        class FakeNotebook:
            def __init__(self):
                self.selected = 0

            def select(self, index=None):
                if index is None:
                    return self.selected
                self.selected = index

        class FakeVar:
            def set(self, value):
                self.value = value

        owner = object.__new__(self.launcher.IntegratedApp)
        owner.root = FakeRoot()
        owner.notebook = FakeNotebook()
        owner.tabs = [object(), object(), object()]
        owner.apps = {}
        owner.load_states = ["loaded", "pending", "pending"]
        owner.status_var = FakeVar()
        owner._clear_tab = mock.Mock()

        self.assertEqual(owner.select_tab(pcg_index), "break")
        self.assertEqual(owner.notebook.selected, pcg_index)
        self.assertEqual(owner.load_states[pcg_index], "scheduled")
        self.assertEqual(len(owner.root.idle_callbacks), 1)

        loading_label = mock.Mock()
        with mock.patch.object(
            self.launcher,
            "load_tool_module",
            return_value=pcg_module,
        ) as load_module, mock.patch.object(
            pcg_module,
            "App",
            return_value=embedded_app,
        ) as app_class, mock.patch.object(
            self.launcher.ttk,
            "Label",
            return_value=loading_label,
        ):
            owner.root.idle_callbacks.pop()()

        load_module.assert_called_once_with(pcg_spec)
        app_class.assert_called_once_with(owner.tabs[pcg_index])
        self.assertIs(owner.apps[pcg_index], embedded_app)
        self.assertEqual(owner.load_states[pcg_index], "loaded")

    def test_failed_pcg_tab_initialization_keeps_parent_navigation_ready(self):
        pcg_index = 2
        pcg_spec = self.launcher.TOOLS[pcg_index]
        failure = RuntimeError("embedded App construction failed")
        failing_module = type(
            "PCGModule",
            (),
            {"App": mock.Mock(side_effect=failure)},
        )()

        class FakeRoot:
            def update_idletasks(self):
                pass

            def after_idle(self, callback):
                self.callback = callback

        class FakeNotebook:
            def select(self, index=None):
                if index is not None:
                    self.selected = index
                return getattr(self, "selected", 0)

        class FakeVar:
            def set(self, value):
                self.value = value

        owner = object.__new__(self.launcher.IntegratedApp)
        owner.root = FakeRoot()
        owner.notebook = FakeNotebook()
        owner.tabs = [object(), object(), object()]
        owner.apps = {}
        owner.load_states = ["loaded", "pending", "scheduled"]
        owner.status_var = FakeVar()
        owner._clear_tab = mock.Mock()
        owner._show_load_error = mock.Mock()

        with mock.patch.object(
            self.launcher,
            "load_tool_module",
            return_value=failing_module,
        ), mock.patch.object(
            self.launcher,
            "record_error",
            return_value=True,
        ) as record_error, mock.patch.object(
            self.launcher.ttk,
            "Label",
            return_value=mock.Mock(),
        ):
            owner._load_tool(pcg_index)

        self.assertEqual(owner.load_states[pcg_index], "failed")
        self.assertNotIn(pcg_index, owner.apps)
        record_error.assert_called_once_with(pcg_spec.label, failure)
        owner._show_load_error.assert_called_once_with(pcg_index, failure)
        self.assertEqual(owner.select_tab(0), "break")
        self.assertEqual(owner.notebook.selected, 0)

    def test_shared_ui_error_formatter_retains_complete_traceback(self):
        try:
            raise RuntimeError("pcg-tab-init-regression")
        except RuntimeError as exc:
            detail = speedtree_error_log.format_exception_traceback(exc)

        self.assertIn("Traceback (most recent call last)", detail)
        self.assertIn(
            "test_shared_ui_error_formatter_retains_complete_traceback",
            detail,
        )
        self.assertIn("RuntimeError: pcg-tab-init-regression", detail)

    def test_shared_ui_error_recorder_forwards_complete_traceback(self):
        captured = {}

        def recorder(label, detail):
            captured["label"] = label
            captured["detail"] = detail
            return True

        try:
            raise RuntimeError("async-pcg-init-failure")
        except RuntimeError as exc:
            with mock.patch.object(
                speedtree_error_log,
                "_bounded_error_recorder",
                return_value=recorder,
            ):
                result = speedtree_error_log.record_exception(
                    "PCG ST9 Texture initial refresh",
                    exc,
                )

        self.assertTrue(result)
        self.assertEqual(
            captured["label"],
            "PCG ST9 Texture initial refresh",
        )
        self.assertIn("Traceback (most recent call last)", captured["detail"])
        self.assertIn("RuntimeError: async-pcg-init-failure", captured["detail"])

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
        self.assertNotIn("%ComSpec%", text)
        self.assertNotRegex(text, r'(?im)^start "" /min')

    def test_integrated_app_icon_assets_exist(self):
        self.assertTrue(self.launcher.ICON_PNG.is_file())
        self.assertTrue(self.launcher.ICON_ICO.is_file())
        self.assertEqual(
            self.launcher.APP_USER_MODEL_ID,
            "PARK.SpeedTree.BatchTools",
        )

    def test_activity_snapshot_reads_each_embedded_worker_model(self):
        class FakeVar:
            def __init__(self, value):
                self.value = value

            def get(self):
                return self.value

        class FakeThread:
            def __init__(self, alive):
                self.alive = alive

            def is_alive(self):
                return self.alive

        sk = type("App", (), {})()
        sk.worker = FakeThread(True)
        sk.progress_var = FakeVar("Blender Repair 1/6 · 실행 중 2개")
        self.assertEqual(
            self.launcher.app_activity_snapshot(sk),
            (True, "Blender Repair 1/6 · 실행 중 2개"),
        )

        sync = type("App", (), {})()
        sync.worker = FakeThread(True)
        sync.progress_text_var = FakeVar("자식 2/5 저장 중 · 00:14")
        self.assertEqual(
            self.launcher.app_activity_snapshot(sync),
            (True, "자식 2/5 저장 중 · 00:14"),
        )

        pcg = type("App", (), {})()
        pcg._busy = True
        pcg.status_var = FakeVar("③ Unreal 동기화 중...")
        self.assertEqual(
            self.launcher.app_activity_snapshot(pcg),
            (True, "③ Unreal 동기화 중..."),
        )

    def test_activity_snapshot_treats_scan_as_visible_work(self):
        class FinishedThread:
            def is_alive(self):
                return False

        class RunningThread:
            def is_alive(self):
                return True

        class FakeVar:
            def get(self):
                return "SK SPM 스캔 중…"

        app = type("App", (), {})()
        app.worker = FinishedThread()
        app.scan_worker = RunningThread()
        app.progress_var = FakeVar()
        self.assertEqual(
            self.launcher.app_activity_snapshot(app),
            (True, "SK SPM 스캔 중…"),
        )

    def test_activity_snapshot_is_idle_without_busy_workers(self):
        app = type("App", (), {})()
        app._busy = False
        app.status_var = type("Var", (), {"get": lambda self: "대기"})()
        self.assertEqual(
            self.launcher.app_activity_snapshot(app),
            (False, "대기"),
        )

    def test_completion_detail_replaces_stale_running_status(self):
        class FakeVar:
            def __init__(self, value):
                self.value = value

            def get(self):
                return self.value

        finished = type("App", (), {})()
        finished.progress_var = FakeVar("스캔 완료 · SPM 145개")
        self.assertEqual(
            self.launcher.activity_completion_detail(
                finished,
                "SK SPM 스캔 중…",
            ),
            "스캔 완료 · SPM 145개",
        )

        idle = type("App", (), {})()
        idle.progress_var = FakeVar("대기")
        self.assertEqual(
            self.launcher.activity_completion_detail(
                idle,
                "Blender Repair 6/6 · 실행 중 0개",
            ),
            "Blender Repair 6/6 · 완료",
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

    def test_integrated_close_waits_for_async_tool_shutdown_completion(self):
        class Root:
            def __init__(self):
                self.withdraw_calls = 0
                self.destroy_calls = 0

            def withdraw(self):
                self.withdraw_calls += 1

            def destroy(self):
                self.destroy_calls += 1

        class AsyncApp:
            def __init__(self):
                self.callback = None
                self.persist_calls = 0

            def shutdown_shared_queue(self, *, on_complete):
                self.callback = on_complete

            def persist_config(self):
                self.persist_calls += 1

        class SyncApp:
            def __init__(self):
                self.shutdown_calls = 0
                self.persist_calls = 0

            def shutdown_shared_queue(self):
                self.shutdown_calls += 1

            def persist_config(self):
                self.persist_calls += 1

        root = Root()
        async_app = AsyncApp()
        sync_app = SyncApp()
        owner = object.__new__(self.launcher.IntegratedApp)
        owner.root = root
        owner.apps = {0: async_app, 1: sync_app}
        owner.find_dialog = None

        with mock.patch.object(
            self.launcher,
            "shutdown_process_supervisor",
            return_value={"survivors": []},
        ) as supervisor_shutdown:
            owner.close()

            self.assertEqual(root.withdraw_calls, 1)
            self.assertEqual(root.destroy_calls, 0)
            self.assertIsNotNone(async_app.callback)
            self.assertEqual(sync_app.shutdown_calls, 1)
            self.assertEqual(async_app.persist_calls, 0)
            self.assertEqual(sync_app.persist_calls, 0)
            supervisor_shutdown.assert_not_called()

            async_app.callback()
            supervisor_shutdown.assert_called_once_with(
                "integrated_gui_close",
                terminate_grace=1.0,
                kill_grace=5.0,
            )
            self.assertEqual(root.destroy_calls, 1)
            self.assertEqual(async_app.persist_calls, 1)
            self.assertEqual(sync_app.persist_calls, 1)

            owner.close()
        self.assertEqual(root.destroy_calls, 1)

    def test_integrated_close_fails_closed_when_tool_shutdown_cannot_start(self):
        class Root:
            def __init__(self):
                self.destroy_calls = 0
                self.deiconify_calls = 0

            def withdraw(self):
                pass

            def deiconify(self):
                self.deiconify_calls += 1

            def destroy(self):
                self.destroy_calls += 1

        class Status:
            def set(self, value):
                self.value = value

        class FailingApp:
            def shutdown_shared_queue(self, *, on_complete):
                raise RuntimeError("[queue_shutdown_failed] state locked")

        root = Root()
        owner = object.__new__(self.launcher.IntegratedApp)
        owner.root = root
        owner.apps = {0: FailingApp()}
        owner.find_dialog = None
        owner.status_var = Status()

        owner.close()

        self.assertEqual(root.destroy_calls, 0)
        self.assertEqual(root.deiconify_calls, 1)
        self.assertIn("종료 정리 실패", owner.status_var.value)
        self.assertEqual(len(owner._close_errors), 1)



if __name__ == "__main__":
    unittest.main()
