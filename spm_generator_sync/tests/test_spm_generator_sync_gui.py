import importlib.machinery
import importlib.util
import queue
import tempfile
import threading
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


class ClipboardRecorder:
    """Stand-in for the Tk root that records the clipboard handoff.

    ``copy_paths_to_clipboard`` only needs these three calls, so this keeps the
    assertion on what the app produced instead of on the shared OS clipboard.
    """

    def __init__(self):
        self.appends = []
        self.clear_count = 0
        self.cleared_before_append = False

    def clipboard_clear(self):
        self.clear_count += 1
        if not self.appends:
            self.cleared_before_append = True

    def clipboard_append(self, value):
        self.appends.append(value)

    def update_idletasks(self):
        pass

    @property
    def text(self):
        return "".join(self.appends)


class GeneratorSyncGuiCacheTests(unittest.TestCase):
    def test_initial_fast_refresh_defers_physical_validation(self):
        app = GUI.App.__new__(GUI.App)
        app.persist_config = mock.Mock()
        app.root_var = mock.Mock()
        app.root_var.get.return_value = r"D:\Trees"
        app.sk_only_var = mock.Mock()
        app.sk_only_var.get.return_value = True
        app.render_board = mock.Mock()
        app.status_var = mock.Mock()

        with mock.patch.object(
            GUI.engine,
            "scan_tree_folders",
            return_value=[],
        ) as scan:
            app.refresh(fast=True)

        scan.assert_called_once_with(
            Path(r"D:\Trees"),
            sk_only=True,
            verify_physical=False,
        )
        app.render_board.assert_called_once_with(fast=True)

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
            # The Windows clipboard is a shared OS resource whose ownership any
            # process can take between the append and a read-back, so asserting
            # through clipboard_get() is inherently racy.  Record what the app
            # hands to Tk instead; delivering it is Tk's job, not ours.
            app.root = ClipboardRecorder()
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

            expected = str(
                Path(r"D:\Trees\black_locust\tree_04.spm").resolve()
            )
            self.assertEqual(app.root.text, expected)
            self.assertTrue(app.root.cleared_before_append)
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
        app._start_job = lambda _label, work, done, **_kwargs: done(
            work(lambda *_args: None)
        )

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

    def test_cluster_folder_selection_expands_to_unique_child_relations_for_refresh(self):
        root = tk.Tk()
        root.withdraw()
        try:
            app = GUI.App.__new__(GUI.App)
            app.root = root
            app.tree = ttk.Treeview(root)
            cluster_folder = app.tree.insert("", "end", text="Cluster")
            first = app.tree.insert(cluster_folder, "end", text="branch")
            second = app.tree.insert(cluster_folder, "end", text="leaf")
            branch_blend = Path(r"D:\Trees\Tree_elm\Cluster\SK_branch_elm_01.blend")
            leaf_blend = Path(r"D:\Trees\Tree_elm\Cluster\SK_leaf_elm_01.blend")
            app.item_meta = {
                cluster_folder: {
                    "kind": "folder",
                    "folder": branch_blend.parent,
                    "role": "cluster_folder",
                },
                first: {
                    "kind": "cluster_relation",
                    "blend": branch_blend,
                },
                second: {
                    "kind": "cluster_relation",
                    "blend": leaf_blend,
                },
            }
            app.tree.selection_set(cluster_folder, first)

            self.assertEqual(
                app.selected_cluster_relations(
                    include_cluster_folders=True
                ),
                [app.item_meta[first], app.item_meta[second]],
            )
            self.assertEqual(
                app.selected_cluster_relations(),
                [app.item_meta[first]],
            )
        finally:
            root.destroy()

    def test_cluster_folder_on_expands_selection_and_runs_folder_transaction(self):
        owner = Path(r"D:\Trees\bush_Silky_Dogwood")
        blend = (
            owner
            / "Cluster"
            / "SK_cluster_Silky_Dogwood_01.blend"
        )
        targets = [
            owner / f"SK_bush_Silky_Dogwood_0{index}.spm"
            for index in (1, 2, 3)
        ]
        app = GUI.App.__new__(GUI.App)
        app.root = None
        app.config = {"blender_exe": r"C:\Blender\blender.exe"}
        app.status_var = mock.Mock()
        app.refresh = mock.Mock()
        app.selected_cluster_relations = mock.Mock(return_value=[{
            "blend": blend,
            "folder_relation": "partial",
            "target_count": 3,
            "on_target_spms": [targets[0]],
            "target_spms": targets,
            "all_synced": False,
        }])

        def run_now(_label, work, done, **_kwargs):
            done(work(lambda *_args: None))

        app._start_job = run_now
        with mock.patch.object(
            GUI.messagebox, "askyesno", return_value=True
        ), mock.patch.object(
            GUI.messagebox, "showinfo"
        ), mock.patch.object(
            GUI, "run_cluster_relation_transaction"
        ) as refresh, mock.patch.object(
            GUI,
            "run_cluster_folder_relation_transaction",
            return_value={"status": "ok", "mode": "sync"},
        ) as normalize:
            app.set_selected_cluster_relation(True)

        app.selected_cluster_relations.assert_called_once_with(
            include_cluster_folders=True
        )
        normalize.assert_called_once_with(
            blend,
            enabled=True,
            blender_exe=Path(r"C:\Blender\blender.exe"),
            unit_probe_path=GUI.DEFAULT_CLUSTER_UNIT_PROBE,
            capture_resolution=1024,
            repair_runtime_config=app.config,
            progress_callback=mock.ANY,
        )
        refresh.assert_not_called()

    def test_follower_extra_structure_is_shown_as_one_way_removal(self):
        root = tk.Tk()
        root.withdraw()
        try:
            folder = Path(r"D:\Trees\Tree_elm")
            app = GUI.App.__new__(GUI.App)
            app.root = root
            app.root_var = tk.StringVar(root, value=str(folder))
            app.tree = ttk.Treeview(
                root,
                columns=("role", "bases", "structure", "status", "last"),
            )
            app.item_meta = {}
            app.signature_cache = OrderedDict()
            app.analysis_cache = OrderedDict()
            app.board = [{
                "folder": str(folder),
                "spms": ["SK_Tree_elm_01.spm", "SK_Tree_elm_02.spm"],
                "manifest": {
                    "version": 1,
                    "groups": [{
                        "master": "SK_Tree_elm_01.spm",
                        "base_categories": {"Leaf": "leaf"},
                        "followers": [{
                            "file": "SK_Tree_elm_02.spm",
                            "base_map": {"Leaf 2": "Leaf"},
                            "base_map_confirmed": True,
                            "last_sync": "2026-07-25T10:00:00",
                            "last_master_hash": "master-hash",
                            "last_target_hash": "old-target-hash",
                        }],
                    }],
                    "independent": [],
                },
                "master_candidates": [],
                "cluster_blends": [],
            }]
            app.master_status = mock.Mock(
                return_value=("최신", "master-hash")
            )
            app.cached_follower_analysis = mock.Mock(return_value=(
                {
                    "common": 2,
                    "missing": 0,
                    "master_sync": 0,
                    "target_local": 0,
                    "remove": 1,
                    "missing_bases": 0,
                    "missing_details": [],
                    "master_sync_details": [],
                    "target_local_details": [],
                    "remove_details": [{
                        "base": "Leaf 2",
                        "name": "Knot unique",
                        "type": "Knot",
                        "path": "Branch > Knot unique",
                    }],
                    "scale_risk": {},
                },
                "new-target-hash",
            ))

            with mock.patch.object(GUI, "save_analysis_cache"):
                app.render_board()

            follower_iid = next(
                iid for iid, row in app.item_meta.items()
                if row.get("role") == "follower"
            )
            self.assertEqual(
                app.tree.set(follower_iid, "structure"),
                "마스터→자식 반영 0 · 초과삭제 1",
            )
            self.assertEqual(
                app.tree.set(follower_iid, "status"),
                "정규화 필요",
            )
            self.assertIn(
                "follower_master_sync",
                app.tree.item(follower_iid, "tags"),
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
        app.selected_cluster_relations = lambda **_kwargs: [{
            "blend": blend,
            "folder_relation": "on",
            "target_count": 2,
            "on_target_spms": [current],
            "target_spms": [current, unrelated],
            "all_synced": False,
        }]
        app._connected_scope_from_board = mock.Mock(return_value={
            "groups": [],
            "cluster_rows": [{
                "blend": blend,
                "folder_relation": "on",
                "on_target_spms": [current],
            }],
            "skipped": [],
        })

        def run_now(_label, work, done, **_kwargs):
            done(work(lambda *_args: None))

        app._start_job = run_now
        with mock.patch.object(
            GUI.messagebox, "askyesno", return_value=True
        ), mock.patch.object(
            GUI.messagebox, "showinfo"
        ), mock.patch.object(
            GUI,
            "prepare_cluster_source_if_required",
            return_value={"status": "current"},
        ) as prepare, mock.patch.object(
            GUI,
            "run_cluster_relation_transaction",
            return_value={"status": "ok", "mode": "sync"},
        ) as refresh, mock.patch.object(
            GUI, "run_cluster_folder_relation_transaction"
        ) as normalize, mock.patch.object(
            GUI.engine, "scan_tree_folders", return_value=[]
        ):
            app.refresh_selected_cluster_relation()

        refresh.assert_called_once_with(
            blend,
            [current],
            enabled=True,
            blender_exe=Path(r"C:\Blender\blender.exe"),
            unit_probe_path=GUI.DEFAULT_CLUSTER_UNIT_PROBE,
            capture_resolution=1024,
            repair_runtime_config=app.config,
            force_refresh=True,
            progress_callback=mock.ANY,
        )
        prepare.assert_called_once_with(
            blend,
            [current],
            blender_exe=Path(r"C:\Blender\blender.exe"),
            unit_probe_path=GUI.DEFAULT_CLUSTER_UNIT_PROBE,
            capture_resolution=1024,
            progress_callback=mock.ANY,
        )
        normalize.assert_not_called()

    def test_cluster_folder_refresh_updates_each_on_relation_and_skips_off(self):
        owner = Path(r"D:\Trees\Tree_elm")
        branch_blend = owner / "Cluster" / "SK_branch_elm_01.blend"
        leaf_blend = owner / "Cluster" / "SK_leaf_elm_01.blend"
        off_blend = owner / "Cluster" / "SK_leaf_elm_02.blend"
        tree_01 = owner / "SK_Tree_elm_01.spm"
        tree_02 = owner / "SK_Tree_elm_02.spm"
        app = GUI.App.__new__(GUI.App)
        app.root = None
        app.config = {"blender_exe": r"C:\Blender\blender.exe"}
        app.status_var = mock.Mock()
        app.refresh = mock.Mock()
        app.selected_cluster_relations = lambda **_kwargs: [
            {
                "blend": branch_blend,
                "folder_relation": "on",
                "target_count": 2,
                "on_target_spms": [tree_01, tree_02],
                "all_synced": False,
            },
            {
                "blend": leaf_blend,
                "folder_relation": "on",
                "target_count": 2,
                "on_target_spms": [tree_01],
                # The GUI snapshot may still say current after the source was
                # saved externally. The transaction performs the authoritative
                # receipt/hash check, so explicit refresh must still call it.
                "all_synced": True,
            },
            {
                "blend": off_blend,
                "folder_relation": "off",
                "target_count": 2,
                "on_target_spms": [],
                "all_synced": False,
            },
        ]
        app._connected_scope_from_board = mock.Mock(return_value={
            "groups": [],
            "cluster_rows": [
                {
                    "blend": branch_blend,
                    "folder_relation": "on",
                    "on_target_spms": [tree_01, tree_02],
                },
                {
                    "blend": leaf_blend,
                    "folder_relation": "on",
                    "on_target_spms": [tree_01],
                },
            ],
            "skipped": [],
        })

        def run_now(_label, work, done, **_kwargs):
            done(work(lambda *_args: None))

        app._start_job = run_now
        with mock.patch.object(
            GUI.messagebox, "askyesno", return_value=True
        ), mock.patch.object(
            GUI.messagebox, "showinfo"
        ), mock.patch.object(
            GUI,
            "prepare_cluster_source_if_required",
            return_value={"status": "current"},
        ), mock.patch.object(
            GUI,
            "run_cluster_relation_transaction",
            side_effect=[
                {"status": "ok", "mode": "sync"},
                {"status": "ok", "mode": "sync"},
            ],
        ) as refresh, mock.patch.object(
            GUI, "run_cluster_folder_relation_transaction"
        ) as normalize, mock.patch.object(
            GUI.engine, "scan_tree_folders", return_value=[]
        ):
            app.refresh_selected_cluster_relation()

        self.assertEqual(
            refresh.call_args_list,
            [
                mock.call(
                    branch_blend,
                    [tree_01, tree_02],
                    enabled=True,
                    blender_exe=Path(r"C:\Blender\blender.exe"),
                    unit_probe_path=GUI.DEFAULT_CLUSTER_UNIT_PROBE,
                    capture_resolution=1024,
                    repair_runtime_config=app.config,
                    force_refresh=True,
                    progress_callback=mock.ANY,
                ),
                mock.call(
                    leaf_blend,
                    [tree_01],
                    enabled=True,
                    blender_exe=Path(r"C:\Blender\blender.exe"),
                    unit_probe_path=GUI.DEFAULT_CLUSTER_UNIT_PROBE,
                    capture_resolution=1024,
                    repair_runtime_config=app.config,
                    force_refresh=True,
                    progress_callback=mock.ANY,
                ),
            ],
        )
        normalize.assert_not_called()
        self.assertIn(
            "Cluster 2개",
            app.status_var.set.call_args.args[0],
        )

    def test_partial_cluster_relation_blocks_refresh_until_normalized(self):
        owner = Path(r"D:\Trees\Tree_elm")
        blend = owner / "Cluster" / "SK_branch_elm_01.blend"
        tree_01 = owner / "SK_Tree_elm_01.spm"
        app = GUI.App.__new__(GUI.App)
        app.root = None
        app.config = {"blender_exe": r"C:\Blender\blender.exe"}
        app.status_var = mock.Mock()
        app.selected_cluster_relations = lambda **_kwargs: [{
            "blend": blend,
            "folder_relation": "partial",
            "target_count": 2,
            "on_target_spms": [tree_01],
            "all_synced": False,
        }]
        app._start_job = mock.Mock()

        with mock.patch.object(
            GUI.messagebox, "showinfo"
        ) as showinfo, mock.patch.object(
            GUI, "run_cluster_relation_transaction"
        ) as refresh:
            app.refresh_selected_cluster_relation()

        app._start_job.assert_not_called()
        refresh.assert_not_called()
        self.assertEqual(
            showinfo.call_args.args[0],
            "Cluster 관계 불일치",
        )

    def test_connected_board_scope_uses_confirmed_followers_and_on_clusters(self):
        owner = Path(r"D:\Trees\Tree_elm")
        master = "SK_Tree_elm_01.spm"
        app = GUI.App.__new__(GUI.App)
        app.board = [{
            "folder": str(owner),
            "manifest": {
                "groups": [{
                    "master": master,
                    "followers": [
                        {
                            "file": "SK_Tree_elm_02.spm",
                            "base_map_confirmed": True,
                        },
                        {
                            "file": "SK_Tree_elm_03.spm",
                            "base_map_confirmed": False,
                        },
                        {
                            "file": "SK_Tree_elm_04.spm",
                            "base_map_confirmed": True,
                        },
                    ],
                }],
                "independent": ["SK_Tree_elm_05.spm"],
            },
            "cluster_blends": [
                {
                    "folder_relation": "on",
                    "blend": str(
                        owner / "Cluster" / "SK_branch_elm_01.blend"
                    ),
                    "targets": [{
                        "owner_target": True,
                        "relation_on": True,
                        "target_spm": str(
                            owner / "SK_Tree_elm_01.spm"
                        ),
                    }],
                },
                {
                    "folder_relation": "off",
                    "blend": str(
                        owner / "Cluster" / "SK_leaf_elm_01.blend"
                    ),
                    "targets": [{
                        "owner_target": True,
                        "relation_on": False,
                        "target_spm": str(
                            owner / "SK_Tree_elm_01.spm"
                        ),
                    }],
                },
                {
                    "folder_relation": "partial",
                    "blend": str(
                        owner / "Cluster" / "SK_leaf_elm_02.blend"
                    ),
                    "targets": [{
                        "owner_target": True,
                        "relation_on": True,
                        "target_spm": str(
                            owner / "SK_Tree_elm_01.spm"
                        ),
                    }],
                },
            ],
        }]

        scope = app.connected_board_scope()

        self.assertEqual(len(scope["groups"]), 1)
        self.assertEqual(
            scope["groups"][0]["names"],
            ["SK_Tree_elm_02.spm", "SK_Tree_elm_04.spm"],
        )
        self.assertEqual(len(scope["cluster_rows"]), 1)
        self.assertEqual(
            Path(scope["cluster_rows"][0]["blend"]).name,
            "SK_branch_elm_01.blend",
        )
        self.assertEqual(
            {entry["reason"] for entry in scope["skipped"]},
            {"Base 매핑 미확정"},
        )

    def test_connected_board_batch_continues_to_cluster_after_sync_failure(self):
        owner = Path(r"D:\Trees\Tree_elm")
        blend = owner / "Cluster" / "SK_branch_elm_01.blend"
        target = owner / "SK_Tree_elm_01.spm"
        scope = {
            "groups": [{
                "folder": owner,
                "master": "SK_Tree_elm_01.spm",
                "names": ["SK_Tree_elm_02.spm"],
            }],
            "cluster_rows": [{
                "kind": "cluster_relation",
                "folder_relation": "on",
                "blend": blend,
                "on_target_spms": [target],
                "target_spms": [target],
            }],
            "skipped": [],
        }
        app = GUI.App.__new__(GUI.App)
        app.root = None
        app.config = {"blender_exe": r"C:\Blender\blender.exe"}
        app.verify_var = mock.Mock()
        app.verify_var.get.return_value = False
        app.root_var = mock.Mock()
        app.root_var.get.return_value = str(owner)
        app.status_var = mock.Mock()
        app.refresh = mock.Mock()
        app._show_job_info = mock.Mock()
        app.connected_board_scope = mock.Mock(return_value=scope)
        app._connected_scope_from_board = mock.Mock(return_value=scope)
        captured = {}

        def run_now(_label, work, done, **_kwargs):
            result = work(lambda *_args: None)
            captured.update(result)
            done(result)

        app._start_job = run_now
        with mock.patch.object(
            GUI.messagebox, "askyesno", return_value=True
        ), mock.patch.object(
            GUI.engine, "scan_tree_folders", return_value=[]
        ), mock.patch.object(
            GUI.engine,
            "apply_group_transaction",
            side_effect=RuntimeError("generator failed"),
        ) as sync, mock.patch.object(
            GUI,
            "prepare_cluster_source_if_required",
            return_value={"status": "current"},
        ) as prepare, mock.patch.object(
            GUI,
            "run_cluster_relation_transaction",
            return_value={"status": "ok", "mode": "sync"},
        ) as refresh, mock.patch.object(
            GUI,
            "write_connected_run_report",
            return_value=Path(r"C:\reports\connected.json"),
        ):
            app.apply_connected_board()

        sync.assert_called_once()
        self.assertTrue(
            sync.call_args.kwargs["skip_blocked_scale"]
        )
        prepare.assert_called_once()
        refresh.assert_called_once_with(
            blend,
            [target],
            enabled=True,
            blender_exe=Path(r"C:\Blender\blender.exe"),
            unit_probe_path=GUI.DEFAULT_CLUSTER_UNIT_PROBE,
            capture_resolution=1024,
            repair_runtime_config=app.config,
            force_refresh=True,
            progress_callback=mock.ANY,
        )
        self.assertEqual(captured["status"], "partial")
        self.assertEqual(len(captured["failures"]), 1)
        self.assertEqual(len(captured["cluster_refresh"]), 1)
        app.refresh.assert_called_once()
        self.assertIn(
            "실패 1",
            app.status_var.set.call_args.args[0],
        )

    def test_connected_board_batch_rechecks_scope_when_its_queue_turn_starts(self):
        owner = Path(r"D:\Trees\Tree_elm")
        old_blend = owner / "Cluster" / "SK_branch_elm_01.blend"
        initial_scope = {
            "groups": [],
            "cluster_rows": [{
                "blend": old_blend,
                "folder_relation": "on",
                "on_target_spms": [owner / "SK_Tree_elm_01.spm"],
            }],
            "skipped": [],
        }
        runtime_scope = {
            "groups": [],
            "cluster_rows": [],
            "skipped": [],
        }
        app = GUI.App.__new__(GUI.App)
        app.root = None
        app.config = {"sk_only": True}
        app.verify_var = mock.Mock()
        app.verify_var.get.return_value = False
        app.root_var = mock.Mock()
        app.root_var.get.return_value = str(owner)
        app.status_var = mock.Mock()
        app.refresh = mock.Mock()
        app._show_job_info = mock.Mock()
        app.connected_board_scope = mock.Mock(return_value=initial_scope)
        app._connected_scope_from_board = mock.Mock(
            return_value=runtime_scope
        )
        captured = {}

        def run_now(_label, work, done, **_kwargs):
            result = work(lambda *_args: None)
            captured.update(result)
            done(result)

        app._start_job = run_now
        with mock.patch.object(
            GUI.messagebox, "askyesno", return_value=True
        ), mock.patch.object(
            GUI.engine, "scan_tree_folders", return_value=[]
        ) as scan, mock.patch.object(
            GUI, "run_cluster_relation_transaction"
        ) as refresh, mock.patch.object(
            GUI,
            "write_connected_run_report",
            return_value=Path(r"C:\reports\connected.json"),
        ):
            app.apply_connected_board()

        scan.assert_called_once_with(
            owner,
            sk_only=True,
            verify_physical=False,
        )
        refresh.assert_not_called()
        self.assertEqual(
            captured["queued_scope"]["cluster_relation_count"],
            1,
        )
        self.assertEqual(
            captured["scope"]["cluster_relation_count"],
            0,
        )

    def test_connected_board_batch_can_queue_behind_relation_change(self):
        owner = Path(r"D:\Trees\Tree_elm")
        app = GUI.App.__new__(GUI.App)
        app.root = None
        app.active_job = {"id": 1}
        app.pending_jobs = []
        app.config = {"sk_only": True}
        app.verify_var = mock.Mock()
        app.verify_var.get.return_value = False
        app.root_var = mock.Mock()
        app.root_var.get.return_value = str(owner)
        app.connected_board_scope = mock.Mock(return_value={
            "groups": [],
            "cluster_rows": [],
            "skipped": [],
        })
        app._start_job = mock.Mock()

        with mock.patch.object(
            GUI.messagebox, "askyesno", return_value=True
        ) as confirm, mock.patch.object(
            GUI.messagebox, "showinfo"
        ) as showinfo:
            app.apply_connected_board()

        showinfo.assert_not_called()
        app._start_job.assert_called_once()
        self.assertIn(
            "실제 시작 시 연결 범위를 다시 검사",
            confirm.call_args.args[1],
        )

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

    def test_background_jobs_queue_fifo_and_continue_after_failure(self):
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
            app.cluster_on_button = ttk.Button(root)
            app.cluster_refresh_button = ttk.Button(root)
            app.cluster_off_button = ttk.Button(root)
            gate = threading.Event()
            order = []
            completed = []

            def first(report):
                order.append("a-start")
                report("A 실행", 25)
                gate.wait(2)
                order.append("a-end")
                return "A"

            def second(_report):
                order.append("b")
                raise RuntimeError("expected B failure")

            def third(_report):
                order.append("c")
                return "C"

            with mock.patch.object(GUI.messagebox, "showerror") as showerror:
                app._start_job("A", first, completed.append, queue_label="A")
                app._start_job("B", second, completed.append, queue_label="B")
                app._start_job("C", third, completed.append, queue_label="C")

                self.assertEqual(len(app.pending_jobs), 2)
                self.assertEqual(
                    str(app.cluster_refresh_button.cget("state")),
                    "normal",
                )
                gate.set()
                deadline = time.monotonic() + 3
                while (
                    (
                        app.active_job is not None
                        or app.pending_jobs
                        or app.worker is not None
                    )
                    and time.monotonic() < deadline
                ):
                    root.update()
                    time.sleep(0.01)

            self.assertEqual(order, ["a-start", "a-end", "b", "c"])
            self.assertEqual(completed, ["A", "C"])
            self.assertEqual(len(app.job_failures), 1)
            self.assertIn("대기열 완료", app.status_var.get())
            showerror.assert_not_called()
        finally:
            root.destroy()

    def test_analysis_cache_survives_restart_and_rejects_old_version(self):
        with tempfile.TemporaryDirectory() as temp:
            cache_path = Path(temp) / "cache.json"
            signatures = OrderedDict([("master-key", "master-hash")])
            analyses = OrderedDict([
                ("pair-key", ({"missing": 2, "master_sync": 1}, "target-hash")),
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
