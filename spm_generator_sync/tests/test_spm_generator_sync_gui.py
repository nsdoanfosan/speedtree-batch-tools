import importlib.machinery
import importlib.util
import queue
import tempfile
import time
import unittest
import tkinter as tk
from tkinter import ttk
from collections import OrderedDict
from pathlib import Path
from unittest import mock


TOOL_DIR = Path(__file__).resolve().parents[1]
LOADER = importlib.machinery.SourceFileLoader(
    "spm_generator_sync_gui_test",
    str(TOOL_DIR / "spm_generator_sync_gui.pyw"),
)
SPEC = importlib.util.spec_from_loader(LOADER.name, LOADER)
GUI = importlib.util.module_from_spec(SPEC)
LOADER.exec_module(GUI)


class GeneratorSyncGuiCacheTests(unittest.TestCase):
    def test_gui_uses_full_sibling_engine_module(self):
        self.assertEqual(
            Path(GUI.engine.__file__).resolve(),
            (TOOL_DIR / "spm_generator_sync.py").resolve(),
        )
        for name in (
            "SPMDocument", "base_role_color", "set_master", "promote_master",
            "save_manifest",
        ):
            self.assertTrue(hasattr(GUI.engine, name), name)

    def test_selected_spm_full_path_is_copied_for_everything(self):
        root = tk.Tk()
        root.withdraw()
        try:
            app = GUI.App.__new__(GUI.App)
            app.root = root
            app.status_var = tk.StringVar(root, value="")
            app.tree = ttk.Treeview(root)
            iid = app.tree.insert("", "end", text="tree_04.spm")
            app.item_meta = {
                iid: {
                    "kind": "spm",
                    "folder": Path(r"D:\Trees\black_locust"),
                    "file": "tree_04.spm",
                }
            }
            app.tree.selection_set(iid)

            result = app.copy_selected_paths()
            # Commit the clipboard selection claim before reading it back;
            # without this the read is timing-dependent under a full test run.
            root.update()
            expected = str(
                Path(r"D:\Trees\black_locust\tree_04.spm").resolve()
            )
            self.assertEqual(root.clipboard_get(), expected)
            self.assertEqual(result, "break")
            self.assertIn("1", app.status_var.get())
        finally:
            root.destroy()

    def test_master_button_runs_immediate_master_promotion_transaction(self):
        folder = Path(r"D:\Trees\oak")
        app = GUI.App.__new__(GUI.App)
        app.root = None
        app.selected_items = lambda: [{
            "folder": folder,
            "file": "SK_tree_oak_01.spm",
            "role": "candidate",
        }]
        app.refresh = mock.Mock()
        app.status_var = mock.Mock()

        result = {"color_updates": 7}
        with mock.patch.object(GUI.engine, "promote_master", return_value=result) as promote:
            app.set_selected_master()

        promote.assert_called_once_with(folder, "SK_tree_oak_01.spm")
        app.refresh.assert_called_once_with(
            reveal=(folder, "SK_tree_oak_01.spm")
        )
        self.assertIn("7", app.status_var.set.call_args.args[0])

    def test_cluster_blend_is_one_folder_relation_row_not_one_row_per_sk(self):
        root = tk.Tk()
        root.withdraw()
        try:
            owner = Path(r"D:\Trees\Tree_elm")
            cluster = owner / "Cluster"
            blend = cluster / "SK_branch_elm_01.blend"
            app = GUI.App.__new__(GUI.App)
            app.root = root
            app.root_var = tk.StringVar(root, value=str(owner))
            app.tree = ttk.Treeview(
                root,
                columns=("role", "bases", "structure", "status", "last"),
            )
            app.item_meta = {}
            app.signature_cache = OrderedDict()
            app.analysis_cache = OrderedDict()
            app.board = [{
                "folder": str(owner),
                "spms": ["SK_Tree_elm_01.spm", "SK_Tree_elm_02.spm"],
                "manifest": {"version": 1, "groups": [], "independent": []},
                "master_candidates": [],
                "cluster_blends": [{
                    "cluster_folder": str(cluster),
                    "source_spm": str(cluster / "branch_elm_01.spm"),
                    "blend": str(blend),
                    "folder_relation": "partial",
                    "owner_target_count": 2,
                    "owner_on_count": 1,
                    "targets": [
                        {
                            "owner_target": True,
                            "target_spm": str(owner / "SK_Tree_elm_01.spm"),
                            "relation_on": True,
                            "status": "synced",
                            "material": "M_branch_elm_01",
                        },
                        {
                            "owner_target": True,
                            "target_spm": str(owner / "SK_Tree_elm_02.spm"),
                            "relation_on": False,
                            "status": "off",
                            "material": None,
                        },
                    ],
                }],
            }]

            with mock.patch.object(GUI, "save_analysis_cache"):
                app.render_board()

            relations = [
                (iid, row) for iid, row in app.item_meta.items()
                if row.get("kind") == "cluster_relation"
            ]
            self.assertEqual(len(relations), 1)
            iid, relation = relations[0]
            self.assertEqual(relation["folder_relation"], "partial")
            self.assertEqual(relation["target_count"], 2)
            self.assertEqual(len(relation["target_spms"]), 2)
            self.assertEqual(app.tree.set(iid, "role"), "PARTIAL")
            self.assertEqual(app.tree.get_children(iid), ())
        finally:
            root.destroy()

    def test_changed_cluster_source_is_shown_as_refresh_required(self):
        root = tk.Tk()
        root.withdraw()
        try:
            owner = Path(r"D:\Trees\Tree_elm")
            cluster = owner / "Cluster"
            canonical = cluster / "SK_branch_elm_01.spm"
            blend = cluster / "SK_branch_elm_01.blend"
            app = GUI.App.__new__(GUI.App)
            app.root = root
            app.root_var = tk.StringVar(root, value=str(owner))
            app.tree = ttk.Treeview(
                root,
                columns=("role", "bases", "structure", "status", "last"),
            )
            app.item_meta = {}
            app.signature_cache = OrderedDict()
            app.analysis_cache = OrderedDict()
            app.board = [{
                "folder": str(owner),
                "spms": ["SK_Tree_elm_01.spm"],
                "manifest": {"version": 1, "groups": [], "independent": []},
                "master_candidates": [],
                "cluster_blends": [{
                    "cluster_folder": str(cluster),
                    "source_spm": str(canonical),
                    "canonical_spm": str(canonical),
                    "mirror_spm": str(cluster / "branch_elm_01.spm"),
                    "blend": str(blend),
                    "folder_relation": "on",
                    "owner_target_count": 1,
                    "owner_on_count": 1,
                    "refresh_required_count": 1,
                    "refresh_reasons": ["canonical_source_changed"],
                    "targets": [{
                        "owner_target": True,
                        "target_spm": str(owner / "SK_Tree_elm_01.spm"),
                        "relation_on": True,
                        "status": "refresh_required",
                        "material": "M_branch_elm_01",
                    }],
                }],
            }]

            with mock.patch.object(GUI, "save_analysis_cache"):
                app.render_board()

            iid, relation = next(
                (iid, row) for iid, row in app.item_meta.items()
                if row.get("kind") == "cluster_relation"
            )
            self.assertFalse(relation["all_synced"])
            self.assertEqual(relation["refresh_required_count"], 1)
            self.assertEqual(
                app.tree.set(iid, "status"),
                "Cluster 원본 변경 · 폴더 SK 1개 갱신 필요",
            )
        finally:
            root.destroy()

    def test_cluster_refresh_reapplies_only_current_on_targets(self):
        owner = Path(r"D:\Trees\Tree_elm")
        blend = owner / "Cluster" / "SK_branch_elm_01.blend"
        current = owner / "SK_Tree_elm_01.spm"
        unrelated = owner / "SK_Tree_elm_02.spm"
        app = GUI.App.__new__(GUI.App)
        app.root = None
        app.config = {"blender_exe": r"C:\Blender\blender.exe"}
        app.status_var = mock.Mock()
        app.refresh = mock.Mock()
        app.selected_cluster_relations = lambda: [{
            "blend": blend,
            "folder_relation": "partial",
            "target_count": 2,
            "on_target_spms": [current],
            "target_spms": [current, unrelated],
            "all_synced": False,
        }]

        def run_now(_label, work, done):
            done(work(lambda *_args: None))

        app._start_job = run_now
        with mock.patch.object(
            GUI.messagebox, "askyesno", return_value=True
        ), mock.patch.object(
            GUI.messagebox, "showinfo"
        ), mock.patch.object(
            GUI,
            "run_cluster_relation_transaction",
            return_value={"status": "ok", "mode": "sync"},
        ) as refresh, mock.patch.object(
            GUI, "run_cluster_folder_relation_transaction"
        ) as normalize:
            app.refresh_selected_cluster_relation()

        refresh.assert_called_once_with(
            blend,
            [current],
            enabled=True,
            blender_exe=Path(r"C:\Blender\blender.exe"),
        )
        normalize.assert_not_called()

    def test_clipboard_rows_are_deduplicated_and_folder_rows_are_supported(self):
        rows = [
            {"kind": "spm", "folder": Path(r"D:\Trees"), "file": "a.spm"},
            {"kind": "spm", "folder": Path(r"D:\Trees"), "file": "a.spm"},
            {"kind": "folder", "folder": Path(r"D:\Trees\oak")},
        ]
        query = GUI.clipboard_text_for_rows(rows)
        self.assertEqual(
            query,
            r'"D:\Trees\a.spm"|"D:\Trees\oak"',
        )

    def test_background_job_shows_stage_percent_elapsed_and_completion(self):
        root = tk.Tk()
        root.withdraw()
        try:
            app = GUI.App.__new__(GUI.App)
            app.root = root
            app.job_queue = queue.Queue()
            app.worker = None
            app.job_started_at = None
            app.job_stage = "대기"
            app.status_var = tk.StringVar(root, value="대기")
            app.progress_text_var = tk.StringVar(root, value="작업 대기")
            app.progress_bar = ttk.Progressbar(root, maximum=100)
            app.preview_button = ttk.Button(root)
            app.apply_button = ttk.Button(root)
            app.apply_all_button = ttk.Button(root)
            completed = []

            def work(report):
                report("패치 계산 중", 25)
                time.sleep(0.03)
                report("SpeedTree 사전검사 중", 70)
                return "ok"

            app._start_job("동기화 시작", work, completed.append)
            deadline = time.monotonic() + 2
            while not completed and time.monotonic() < deadline:
                root.update()
                time.sleep(0.01)

            self.assertEqual(completed, ["ok"])
            self.assertEqual(float(app.progress_bar.cget("value")), 100.0)
            self.assertTrue(app.progress_text_var.get().startswith("완료 · "))
            self.assertEqual(str(app.apply_button.cget("state")), "normal")
        finally:
            root.destroy()

    def test_analysis_cache_survives_restart_and_rejects_old_version(self):
        with tempfile.TemporaryDirectory() as temp:
            cache_path = Path(temp) / "cache.json"
            signatures = OrderedDict([("master-key", "master-hash")])
            analyses = OrderedDict([
                ("pair-key", ({"missing": 2, "target_only": 1}, "target-hash")),
            ])
            with mock.patch.object(GUI, "CACHE_PATH", cache_path):
                GUI.save_analysis_cache(signatures, analyses)
                loaded = GUI.load_analysis_cache()
                self.assertEqual(loaded["signatures"]["master-key"], "master-hash")
                self.assertEqual(loaded["analyses"]["pair-key"][0]["missing"], 2)

                cache_path.write_text(
                    '{"version":0,"signatures":{"bad":"value"},"analyses":{}}',
                    encoding="utf-8",
                )
                rejected = GUI.load_analysis_cache()
                self.assertEqual(rejected["signatures"], {})
                self.assertEqual(rejected["analyses"], {})


if __name__ == "__main__":
    unittest.main()
