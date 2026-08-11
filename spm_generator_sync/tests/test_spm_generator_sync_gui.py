import gc
import importlib.machinery
import importlib.util
import queue
import sys
import tempfile
import threading
import time
import unittest
import tkinter as tk
from tkinter import ttk
from collections import OrderedDict, deque
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
    def tearDown(self):
        # Tk variables participate in reference cycles.  Force their finalizers
        # on the test runner's main thread before a later background-job test
        # can become the thread that happens to trigger cyclic collection.
        gc.collect()

    def test_connected_report_checkpoint_is_fsynced_and_atomic_on_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            report_dir = Path(temporary)
            payload = {"schema_version": 2, "status": "running"}
            with mock.patch.object(GUI, "REPORT_DIR", report_dir), mock.patch.object(
                GUI.os, "fsync", wraps=GUI.os.fsync
            ) as fsync:
                path = GUI.write_connected_run_report(payload)
            self.assertGreaterEqual(fsync.call_count, 1)
            before = path.read_bytes()

            with mock.patch.object(GUI, "REPORT_DIR", report_dir), mock.patch.object(
                GUI.os, "replace", side_effect=OSError("publish failed")
            ):
                with self.assertRaisesRegex(OSError, "publish failed"):
                    GUI.write_connected_run_report(
                        {"schema_version": 2, "status": "partial"},
                        path,
                    )
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(list(report_dir.glob(".*.tmp")), [])

    def test_initial_fast_refresh_defers_physical_validation(self):
        app = GUI.App.__new__(GUI.App)
        app.persist_config = mock.Mock()
        app.root_var = mock.Mock()
        app.root_var.get.return_value = r"D:\Trees"
        app.sk_only_var = mock.Mock()
        app.sk_only_var.get.return_value = True
        app.render_board = mock.Mock()
        app.status_var = mock.Mock()
        app._start_job = mock.Mock(return_value=17)

        with mock.patch.object(
            GUI.engine,
            "scan_tree_folders",
            return_value=[],
        ) as scan:
            job_id = app.refresh(fast=True)
            self.assertEqual(job_id, 17)
            scan.assert_not_called()
            _label, work, done = app._start_job.call_args.args
            report = mock.Mock()
            result = work(report)
            scan.assert_called_once_with(
                Path(r"D:\Trees"),
                sk_only=True,
                verify_physical=False,
                progress_callback=report,
            )
        self.assertEqual(result["board"], [])
        self.assertIsNone(result["render_analysis"])
        done(result)
        app.render_board.assert_called_once_with(
            fast=True,
            prepared_analysis=None,
        )
        self.assertFalse(app._start_job.call_args.kwargs["shared_queue"])

    def test_fast_status_stays_nonblocking_without_running_analysis(self):
        app = GUI.App.__new__(GUI.App)
        app.cached_master_signature = mock.Mock(
            side_effect=AssertionError("fast status ran precise analysis")
        )
        group = {
            "master": "tree_01.spm",
            "followers": [{
                "file": "tree_02.spm",
                "base_map_confirmed": False,
                "last_sync": "2026-08-07T16:00:00",
                "last_master_hash": "recorded-master",
            }],
        }

        status, signature = app.master_status(
            Path(r"D:\Trees"),
            group,
            fast=True,
        )

        self.assertEqual(status, "연결됨")
        self.assertEqual(signature, "recorded-master")
        app.cached_master_signature.assert_not_called()

        source = (TOOL_DIR / "spm_generator_sync_gui.pyw").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("정밀 검사 대기", source)
        self.assertNotIn("동기화 점검 필요", source)

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

    def test_connected_board_scope_does_not_gate_on_confirmation_metadata(self):
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
            [
                "SK_Tree_elm_02.spm",
                "SK_Tree_elm_03.spm",
                "SK_Tree_elm_04.spm",
            ],
        )
        self.assertEqual(len(scope["cluster_rows"]), 1)
        self.assertEqual(
            Path(scope["cluster_rows"][0]["blend"]).name,
            "SK_branch_elm_01.blend",
        )
        self.assertEqual(scope["skipped"], [])

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
                "refresh_reasons": [
                    "blender_source_content_changed"
                ],
                "refresh_reason_categories": [
                    "geometry_ownership"
                ],
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
            return_value={
                "status": "ok",
                "mode": "sync",
                "source_content_identity": {
                    "kind": "speedtree_cluster_atlas_blender_source_index",
                    "status": "ok",
                },
            },
        ) as refresh, mock.patch.object(
            GUI,
            "write_connected_run_report",
            return_value=Path(r"C:\reports\connected.json"),
        ), mock.patch.object(
            GUI,
            "report_file_identity",
            return_value={
                "path": r"C:\reports\connected.json",
                "sha256": "a" * 64,
                "size": 100,
            },
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
        cluster_report = captured["cluster_refresh"][0]
        self.assertEqual(
            cluster_report["refresh_reasons"],
            ["blender_source_content_changed"],
        )
        self.assertEqual(
            cluster_report["refresh_reason_categories"],
            ["geometry_ownership"],
        )
        self.assertEqual(
            cluster_report["result"]["planned_refresh_reasons"],
            ["blender_source_content_changed"],
        )
        self.assertEqual(
            cluster_report["result"]["refresh_reasons"],
            ["blender_source_content_changed"],
        )
        self.assertEqual(
            cluster_report["result"]["source_content_identity"][
                "status"
            ],
            "ok",
        )
        cluster_unit = next(
            entry
            for entry in captured["unit_results"]
            if entry["stage"] == "cluster_refresh"
        )
        self.assertEqual(cluster_unit["outcome"], "succeeded")
        self.assertEqual(
            cluster_unit["result"]["planned_refresh_reasons"],
            ["blender_source_content_changed"],
        )
        self.assertEqual(
            cluster_unit["result"]["refresh_reason_categories"],
            ["geometry_ownership"],
        )
        app.refresh.assert_called_once()
        self.assertIn(
            "실패 1",
            app.status_var.set.call_args.args[0],
        )

    def test_connected_board_rebases_shared_manifest_before_later_group(self):
        with tempfile.TemporaryDirectory() as temporary:
            owner = Path(temporary)
            manifest = owner / "spm_generator_sync.json"
            manifest.write_text("before", encoding="utf-8")
            for name in ("master_a.spm", "a.spm", "master_b.spm", "b.spm"):
                (owner / name).write_bytes(name.encode("utf-8"))
            scope = {
                "groups": [
                    {
                        "folder": owner,
                        "master": "master_a.spm",
                        "names": ["a.spm"],
                    },
                    {
                        "folder": owner,
                        "master": "master_b.spm",
                        "names": ["b.spm"],
                    },
                ],
                "cluster_rows": [],
                "skipped": [],
            }
            app = GUI.App.__new__(GUI.App)
            app.root = None
            app.config = {}
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
            execution_digests = []

            def run_now(_label, work, done, **_kwargs):
                result = work(lambda *_args: None)
                captured.update(result)
                done(result)

            def execute(
                unit,
                _runtime,
                _job_config,
                _verify,
                settings,
                expected_identity,
                _report,
                _progress,
                _attempt_event,
            ):
                current = GUI.dependency_identity(unit, settings)
                execution_digests.append((
                    expected_identity["digest"],
                    current["digest"],
                ))
                if len(execution_digests) == 1:
                    manifest.write_text("after first group", encoding="utf-8")
                return {
                    "ok": True,
                    "result": {
                        "status": "up_to_date",
                        "changed_files": [],
                        "master_hash": "a" * 64,
                        "scale_skipped": [],
                    },
                    "attempts": [],
                    "attempt_count": 1,
                }

            app._start_job = run_now
            with mock.patch.object(
                GUI.messagebox,
                "askyesno",
                return_value=True,
            ), mock.patch.object(
                GUI.engine,
                "scan_tree_folders",
                return_value=[],
            ), mock.patch.object(
                app,
                "_execute_connected_runtime_unit",
                side_effect=execute,
            ), mock.patch.object(
                GUI,
                "write_connected_run_report",
                return_value=owner / "connected.json",
            ), mock.patch.object(
                GUI,
                "report_file_identity",
                return_value={
                    "path": str(owner / "connected.json"),
                    "sha256": "b" * 64,
                    "size": 100,
                },
            ):
                app.apply_connected_board()

            self.assertEqual(captured["status"], "ok")
            self.assertEqual(execution_digests[0][0], execution_digests[0][1])
            self.assertEqual(execution_digests[1][0], execution_digests[1][1])
            second = captured["unit_results"][1]
            receipts = second["authorized_dependency_rebases"]
            self.assertEqual(len(receipts), 1)
            self.assertEqual(
                [
                    Path(row["path"]).name
                    for row in receipts[0]["changed_resources"]
                ],
                [manifest.name],
            )

    def test_connected_cluster_failure_preserves_planned_refresh_evidence(self):
        owner = Path(r"D:\Trees\Tree_elm")
        blend = owner / "Cluster" / "SK_branch_elm_01.blend"
        target = owner / "SK_Tree_elm_01.spm"
        scope = {
            "groups": [],
            "cluster_rows": [{
                "kind": "cluster_relation",
                "folder_relation": "on",
                "blend": blend,
                "on_target_spms": [target],
                "target_spms": [target],
                "refresh_reasons": [
                    "blender_source_content_changed"
                ],
                "refresh_reason_categories": [
                    "geometry_ownership"
                ],
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
        attempt = {
            "ok": False,
            "reason": "cluster fixture failed",
            "classification": {"category": "product"},
            "attempts": [],
            "retry_exhausted": False,
        }
        with mock.patch.object(
            GUI.messagebox, "askyesno", return_value=True
        ), mock.patch.object(
            GUI.engine, "scan_tree_folders", return_value=[]
        ), mock.patch.object(
            app,
            "_execute_connected_runtime_unit",
            return_value=attempt,
        ), mock.patch.object(
            GUI,
            "write_connected_run_report",
            return_value=Path(r"C:\reports\connected.json"),
        ), mock.patch.object(
            GUI,
            "report_file_identity",
            return_value={
                "path": r"C:\reports\connected.json",
                "sha256": "a" * 64,
                "size": 100,
            },
        ):
            app.apply_connected_board()

        self.assertEqual(captured["status"], "failed")
        self.assertEqual(captured["summary"]["cluster"]["failed"], 1)
        failure = captured["failures"][0]
        self.assertEqual(
            failure["refresh_reasons"],
            ["blender_source_content_changed"],
        )
        self.assertEqual(
            failure["refresh_reason_categories"],
            ["geometry_ownership"],
        )
        cluster_unit = next(
            entry
            for entry in captured["unit_results"]
            if entry["stage"] == "cluster_refresh"
        )
        self.assertEqual(cluster_unit["outcome"], "failed")
        self.assertEqual(
            cluster_unit["failure"]["refresh_reasons"],
            ["blender_source_content_changed"],
        )
        self.assertEqual(
            cluster_unit["failure"]["refresh_reason_categories"],
            ["geometry_ownership"],
        )

    def test_initial_checkpoint_failure_blocks_every_connected_mutation(self):
        owner = Path(r"D:\Trees\Tree_elm")
        scope = {
            "groups": [{
                "folder": owner,
                "master": "SK_Tree_elm_01.spm",
                "names": ["SK_Tree_elm_02.spm"],
            }],
            "cluster_rows": [],
            "skipped": [],
        }
        app = GUI.App.__new__(GUI.App)
        app.root = None
        app.config = {}
        app.verify_var = mock.Mock()
        app.verify_var.get.return_value = False
        app.root_var = mock.Mock()
        app.root_var.get.return_value = str(owner)
        app.connected_board_scope = mock.Mock(return_value=scope)
        app._connected_scope_from_board = mock.Mock(return_value=scope)

        def run_now(_label, work, _done, **_kwargs):
            with self.assertRaisesRegex(OSError, "checkpoint unavailable"):
                work(lambda *_args: None)

        app._start_job = run_now
        with mock.patch.object(
            GUI.messagebox, "askyesno", return_value=True
        ), mock.patch.object(
            GUI.engine, "scan_tree_folders", return_value=[]
        ), mock.patch.object(
            GUI, "write_connected_run_report", side_effect=OSError(
                "checkpoint unavailable"
            )
        ), mock.patch.object(
            GUI.engine, "apply_group_transaction"
        ) as mutation:
            app.apply_connected_board()

        mutation.assert_not_called()

    def test_retry_helper_uses_callers_exact_validated_baseline(self):
        app = GUI.App.__new__(GUI.App)
        app._execute_cluster_refresh_rows = mock.Mock()
        unit = {
            "unit_id": "cluster_refresh:fixture",
            "stage": "cluster_refresh",
            "selector": {
                "blend": r"D:\Trees\Tree_elm\Cluster\SK_branch_elm_01.blend",
                "targets": [r"D:\Trees\Tree_elm\SK_Tree_elm_01.spm"],
            },
        }
        runtime = {
            "blend": Path(unit["selector"]["blend"]),
            "on_target_spms": [Path(unit["selector"]["targets"][0])],
        }
        expected = {"digest": "validated", "stable": True}
        current = {"digest": "changed", "stable": True}

        with mock.patch.object(
            GUI,
            "dependency_identity",
            return_value=current,
        ):
            outcome = app._execute_connected_runtime_unit(
                unit,
                runtime,
                {},
                False,
                {},
                expected,
                lambda *_args: None,
                lambda *_args: None,
            )

        self.assertFalse(outcome["ok"])
        self.assertIn("content_drift", outcome["reason"])
        app._execute_cluster_refresh_rows.assert_not_called()

    def test_latest_retry_report_ignores_newer_unanchored_report(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)

            def queue_job(name, sequence, *, shared):
                report_path = root / f"{name}.json"
                payload = {
                    "schema_version": 2,
                    "run_id": name,
                    "root": str(root),
                    "status": "failed",
                    "queue_identity": {
                        "mode": "shared" if shared else "local_unanchored",
                        "job_id": name,
                        "sequence": sequence,
                        "owner_id": "owner-fixture",
                    },
                    "unit_results": [{
                        "unit_id": f"generator_sync:{name}",
                        "stage": "generator_sync",
                        "outcome": "failed",
                        "failure": {"reason": "fixture"},
                    }],
                }
                payload["summary"] = GUI.summarize_unit_results(
                    payload["unit_results"]
                )
                report_path.write_text(
                    GUI.json.dumps(payload),
                    encoding="utf-8",
                )
                identity = GUI.report_file_identity(report_path)
                memory_payload = dict(payload, report_identity=identity)
                return {
                    "id": name,
                    "sequence": sequence,
                    "terminal_at": float(sequence),
                    "status": "failed",
                    "last_lease": {"owner_id": "owner-fixture"},
                    "result": GUI.shared_queue_result(
                        memory_payload,
                        sequence,
                    ),
                }

            valid = queue_job("valid", 1, shared=True)
            orphan = queue_job("orphan", 2, shared=False)

            class QueueBackend:
                def snapshot(self):
                    return {"jobs": [valid, orphan]}

            app = GUI.App.__new__(GUI.App)
            app.shared_queue_runtime = type(
                "Runtime",
                (),
                {"queue": QueueBackend()},
            )()
            payload, anchor = app._latest_connected_retry_report()

            self.assertEqual(payload["run_id"], "valid")
            self.assertEqual(anchor["queue_job_id"], "valid")

    def test_connected_partial_ui_shows_exact_generator_and_cluster_counts(self):
        fixture = (
            Path(__file__).parent
            / "fixtures"
            / "issue101_connected_partial_run.json"
        )
        payload = GUI.json.loads(fixture.read_text(encoding="utf-8"))
        payload["report_path"] = r"C:\reports\connected.json"
        app = GUI.App.__new__(GUI.App)
        app.refresh = mock.Mock()
        app.status_var = mock.Mock()
        app._show_job_info = mock.Mock()

        app._show_connected_result(payload)

        status = app.status_var.set.call_args.args[0]
        self.assertIn("부분 완료", status)
        self.assertIn("Generator 11/12", status)
        self.assertIn("Cluster 31/39", status)
        self.assertIn("실패 9", status)
        detail = app._show_job_info.call_args.args[1]
        self.assertIn("Generator Sync 성공: 11/12", detail)
        self.assertIn("Cluster 갱신 성공: 31/39", detail)

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
        ), mock.patch.object(
            GUI,
            "report_file_identity",
            return_value={
                "path": r"C:\reports\connected.json",
                "sha256": "a" * 64,
                "size": 100,
            },
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

    def test_slow_refresh_runs_off_tk_thread_and_keeps_events_flowing(self):
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
            app.root_var = tk.StringVar(root, value=r"D:\Trees")
            app.sk_only_var = tk.BooleanVar(root, value=True)
            app.persist_config = mock.Mock()
            app.tree = ttk.Treeview(
                root,
                columns=("role", "bases", "structure", "status", "last"),
            )
            app.item_meta = {}
            app.document_cache = OrderedDict()
            app.signature_cache = OrderedDict()
            app.analysis_cache = OrderedDict()
            entered = threading.Event()
            release = threading.Event()
            tk_event = threading.Event()
            worker_thread_ids = []

            def slow_scan(
                _root,
                *,
                sk_only,
                verify_physical,
                progress_callback,
            ):
                self.assertTrue(sk_only)
                self.assertTrue(verify_physical)
                worker_thread_ids.append(threading.get_ident())
                progress_callback("폴더 검사 1/2 · Tree_elm", 10)
                entered.set()
                release.wait(2)
                return []

            with mock.patch.object(
                GUI.engine,
                "scan_tree_folders",
                side_effect=slow_scan,
            ), mock.patch.object(GUI, "save_analysis_cache"):
                app.refresh()
                root.after(0, tk_event.set)
                deadline = time.monotonic() + 2
                while (
                    (not entered.is_set() or not tk_event.is_set())
                    and time.monotonic() < deadline
                ):
                    root.update()
                    time.sleep(0.01)

                self.assertTrue(entered.is_set())
                self.assertTrue(tk_event.is_set())
                self.assertNotEqual(
                    worker_thread_ids,
                    [threading.get_ident()],
                )
                deadline = time.monotonic() + 1
                while (
                    "마지막 진행" not in app.progress_text_var.get()
                    and time.monotonic() < deadline
                ):
                    root.update()
                    time.sleep(0.01)
                self.assertIn("마지막 진행", app.progress_text_var.get())
                release.set()
                deadline = time.monotonic() + 2
                while (
                    app.active_job is not None
                    and time.monotonic() < deadline
                ):
                    root.update()
                    time.sleep(0.01)

            self.assertIsNone(app.active_job)
            self.assertEqual(app.board, [])
            self.assertEqual(app.tree.get_children(), ())
        finally:
            release.set()
            root.destroy()

    def test_full_refresh_prepares_spm_analysis_before_real_tk_render(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            master = folder / "SK_tree_01.spm"
            follower = folder / "SK_tree_02.spm"
            master.write_bytes(b"master")
            follower.write_bytes(b"follower")
            board = [{
                "folder": str(folder),
                "spms": [master.name, follower.name],
                "manifest": {
                    "version": 1,
                    "independent": [],
                    "groups": [{
                        "master": master.name,
                        "base_categories": {},
                        "followers": [{
                            "file": follower.name,
                            "base_map": {},
                            "base_map_confirmed": True,
                            "last_sync": None,
                            "last_master_hash": None,
                            "last_target_hash": None,
                        }],
                    }],
                },
                "master_candidates": [],
                "cluster_blends": [],
            }]
            root = tk.Tk()
            root.withdraw()
            analysis_entered = threading.Event()
            analysis_release = threading.Event()
            tk_event = threading.Event()
            analysis_threads = []
            try:
                app = GUI.App.__new__(GUI.App)
                app.root = root
                app.job_queue = queue.Queue()
                app.worker = None
                app.job_started_at = None
                app.job_stage = "대기"
                app.status_var = tk.StringVar(root, value="대기")
                app.progress_text_var = tk.StringVar(
                    root, value="작업 대기"
                )
                app.progress_bar = ttk.Progressbar(root, maximum=100)
                app.root_var = tk.StringVar(root, value=str(folder))
                app.sk_only_var = tk.BooleanVar(root, value=True)
                app.persist_config = mock.Mock()
                app.tree = ttk.Treeview(
                    root,
                    columns=(
                        "role", "bases", "structure", "status", "last",
                    ),
                )
                app.item_meta = {}
                app.board = []
                app.document_cache = OrderedDict()
                app.signature_cache = OrderedDict()
                app.analysis_cache = OrderedDict()
                app.master_status = mock.Mock(
                    side_effect=AssertionError(
                        "full master analysis returned to Tk"
                    )
                )
                app.cached_follower_analysis = mock.Mock(
                    side_effect=AssertionError(
                        "full follower analysis returned to Tk"
                    )
                )

                def from_path(_path, *, full):
                    self.assertFalse(full)
                    analysis_threads.append(threading.get_ident())
                    if not analysis_entered.is_set():
                        analysis_entered.set()
                        analysis_release.wait(2)
                    return object()

                delta = {
                    "missing": 0,
                    "master_sync": 0,
                    "missing_bases": 0,
                    "target_local": 0,
                    "remove": 0,
                    "missing_details": [],
                    "master_sync_details": [],
                    "target_local_details": [],
                    "remove_details": [],
                }
                with mock.patch.object(
                    GUI.engine, "scan_tree_folders", return_value=board
                ), mock.patch.object(
                    GUI.engine.SPMDocument,
                    "from_path",
                    side_effect=from_path,
                ), mock.patch.object(
                    GUI.engine,
                    "base_sync_signature",
                    return_value="master-hash",
                ), mock.patch.object(
                    GUI.engine,
                    "compare_base_structure",
                    return_value=delta,
                ), mock.patch.object(
                    GUI.engine, "assess_scale_risk", return_value={}
                ), mock.patch.object(
                    GUI.engine,
                    "target_sync_signature",
                    return_value="target-hash",
                ), mock.patch.object(GUI, "save_analysis_cache"):
                    app.refresh()
                    root.after(0, tk_event.set)
                    deadline = time.monotonic() + 2
                    while (
                        (
                            not analysis_entered.is_set()
                            or not tk_event.is_set()
                            or not app.job_stage.startswith("보드 분석")
                        )
                        and time.monotonic() < deadline
                    ):
                        root.update()
                        time.sleep(0.01)

                    self.assertTrue(analysis_entered.is_set())
                    self.assertTrue(tk_event.is_set())
                    self.assertEqual(
                        app.job_stage.split(" · ", 1)[0],
                        "보드 분석 1/2",
                    )
                    self.assertLess(
                        float(app.progress_bar.cget("value")),
                        100.0,
                    )
                    self.assertIsNotNone(app.job_last_progress_at)
                    analysis_release.set()
                    deadline = time.monotonic() + 3
                    while (
                        (app.active_job is not None or app.pending_jobs)
                        and time.monotonic() < deadline
                    ):
                        root.update()
                        time.sleep(0.01)

                self.assertIsNone(app.active_job)
                self.assertTrue(analysis_threads)
                self.assertNotIn(threading.get_ident(), analysis_threads)
                self.assertEqual(
                    float(app.progress_bar.cget("value")),
                    100.0,
                )
                self.assertEqual(len(app.item_meta), 3)
                app.master_status.assert_not_called()
                app.cached_follower_analysis.assert_not_called()
            finally:
                analysis_release.set()
                root.destroy()

    def test_completion_popup_waits_for_its_own_refresh_job(self):
        """Post-Sync refresh runs before its deferred completion modal."""
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
            app.root_var = tk.StringVar(root, value=r"D:\Trees")
            app.sk_only_var = tk.BooleanVar(root, value=True)
            app.persist_config = mock.Mock()
            app.render_board = mock.Mock()
            app.item_meta = {}
            app.board = []
            popup_states = []

            with mock.patch.object(
                GUI.engine, "scan_tree_folders", return_value=[]
            ), mock.patch.object(
                GUI.messagebox, "showinfo"
            ) as showinfo:
                showinfo.side_effect = lambda *_args, **_kwargs: (
                    popup_states.append(
                        (app.active_job, bool(app.pending_jobs))
                    )
                )

                def done(_result):
                    app.refresh()
                    app._show_job_info("작업 완료", "상세 내용")

                app._start_job(
                    "작업 실행 중",
                    lambda _report: {"status": "ok"},
                    done,
                    queue_label="테스트 작업",
                )

                deadline = time.monotonic() + 3
                while (
                    (app.active_job is not None or app.pending_jobs)
                    and time.monotonic() < deadline
                ):
                    root.update()
                    time.sleep(0.01)

            showinfo.assert_called_once()
            self.assertEqual(popup_states, [(None, False)])
        finally:
            root.destroy()

    def test_deferred_info_flush_is_single_shot_and_rearmed_after_busy(self):
        app = GUI.App.__new__(GUI.App)
        app.root = mock.Mock()
        app.active_job = {"id": 1}
        app.pending_jobs = deque()
        app.deferred_job_infos = deque([("완료", "상세")])
        app._deferred_job_info_flush_scheduled = False

        app._schedule_deferred_job_infos()
        app._schedule_deferred_job_infos()

        app.root.after_idle.assert_called_once()
        first_flush = app.root.after_idle.call_args.args[0]
        with mock.patch.object(GUI.messagebox, "showinfo") as showinfo:
            first_flush()
            showinfo.assert_not_called()
            self.assertEqual(len(app.deferred_job_infos), 1)
            self.assertFalse(app._deferred_job_info_flush_scheduled)

            app.active_job = None
            app._schedule_deferred_job_infos()
            self.assertEqual(app.root.after_idle.call_count, 2)
            app.root.after_idle.call_args.args[0]()

            showinfo.assert_called_once_with(
                "완료", "상세", parent=app.root
            )
            self.assertFalse(app.deferred_job_infos)

    def test_completion_popup_waits_for_real_queued_job_and_refresh(self):
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
            app.root_var = tk.StringVar(root, value=r"D:\Trees")
            app.sk_only_var = tk.BooleanVar(root, value=True)
            app.persist_config = mock.Mock()
            app.render_board = mock.Mock()
            app.item_meta = {}
            app.board = []
            gate = threading.Event()

            with mock.patch.object(
                GUI.engine, "scan_tree_folders", return_value=[]
            ), mock.patch.object(
                GUI.messagebox, "showinfo"
            ) as showinfo:

                def done(_result):
                    app.refresh()
                    app._show_job_info("작업 완료", "상세 내용")

                app._start_job(
                    "작업 실행 중",
                    lambda _report: gate.wait(2) or {"status": "ok"},
                    done,
                    queue_label="테스트 작업 A",
                )
                app._start_job(
                    "두 번째 작업",
                    lambda _report: {"status": "ok"},
                    lambda _result: None,
                    queue_label="테스트 작업 B",
                )
                showinfo.assert_not_called()
                gate.set()

                deadline = time.monotonic() + 3
                while (
                    (app.active_job is not None or app.pending_jobs)
                    and time.monotonic() < deadline
                ):
                    root.update()
                    time.sleep(0.01)

            showinfo.assert_called_once()
        finally:
            root.destroy()

    def test_stale_refresh_generation_is_discarded_without_rendering(self):
        """When refresh() is re-triggered before the first scan finishes,
        the stale first scan's result must not overwrite the board with
        out-of-date data once the newer refresh has already superseded it.
        """
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
            app.root_var = tk.StringVar(root, value=r"D:\Trees")
            app.sk_only_var = tk.BooleanVar(root, value=True)
            app.persist_config = mock.Mock()
            app.render_board = mock.Mock()
            app.item_meta = {}
            app.board = []
            first_entered = threading.Event()
            release_first = threading.Event()

            stale_board = [{"folder": "stale", "spms": [], "cluster_blends": []}]
            fresh_board = [{"folder": "fresh", "spms": [], "cluster_blends": []}]

            def scan_side_effect(_root, *, sk_only, verify_physical, progress_callback):
                if not first_entered.is_set():
                    first_entered.set()
                    release_first.wait(2)
                    return stale_board
                return fresh_board

            with mock.patch.object(
                GUI.engine, "scan_tree_folders", side_effect=scan_side_effect
            ):
                app.refresh(fast=True)
                deadline = time.monotonic() + 2
                while (
                    not first_entered.is_set()
                    and time.monotonic() < deadline
                ):
                    root.update()
                    time.sleep(0.01)
                self.assertTrue(first_entered.is_set())

                app.refresh(fast=True)
                release_first.set()

                deadline = time.monotonic() + 3
                while (
                    (app.active_job is not None or app.pending_jobs)
                    and time.monotonic() < deadline
                ):
                    root.update()
                    time.sleep(0.01)

            self.assertEqual(app.board, fresh_board)
            self.assertEqual(app.render_board.call_count, 1)
        finally:
            root.destroy()

    def test_callback_failure_rechecks_live_followup_before_error_modal(self):
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
            app.progress_text_var = tk.StringVar(
                root, value="작업 대기"
            )
            app.progress_bar = ttk.Progressbar(root, maximum=100)
            followup_completed = []

            def callback(_payload):
                app._start_job(
                    "후속 refresh",
                    lambda _report: "refreshed",
                    followup_completed.append,
                    shared_queue=False,
                )
                raise RuntimeError("callback failed after queueing refresh")

            with mock.patch.object(
                GUI.messagebox, "showerror"
            ) as showerror:
                app._start_job(
                    "mutation",
                    lambda _report: "mutated",
                    callback,
                    shared_queue=False,
                )
                deadline = time.monotonic() + 3
                while (
                    (app.active_job is not None or app.pending_jobs)
                    and time.monotonic() < deadline
                ):
                    root.update()
                    time.sleep(0.01)

            self.assertEqual(followup_completed, ["refreshed"])
            self.assertEqual(len(app.job_failures), 1)
            showerror.assert_not_called()
        finally:
            root.destroy()

    def test_shared_ticket_is_deferred_until_local_refresh_finishes(self):
        root = tk.Tk()
        root.withdraw()
        gate = threading.Event()
        entered = threading.Event()
        try:
            app = GUI.App.__new__(GUI.App)
            app.root = root
            app.job_queue = queue.Queue()
            app.worker = None
            app.job_started_at = None
            app.job_stage = "대기"
            app.status_var = tk.StringVar(root, value="대기")
            app.progress_text_var = tk.StringVar(
                root, value="작업 대기"
            )
            app.progress_bar = ttk.Progressbar(root, maximum=100)
            lease = mock.Mock()
            lease.finished = False
            runtime = mock.Mock()
            enqueue_threads = []

            def enqueue(_label, _payload):
                enqueue_threads.append(threading.get_ident())
                return {"id": "shared-1", "sequence": 17}

            runtime.enqueue.side_effect = enqueue
            runtime.wait_for_turn.return_value = lease
            app.shared_queue_runtime = runtime
            order = []

            def local_refresh(_report):
                order.append("refresh-start")
                entered.set()
                gate.wait(2)
                order.append("refresh-end")
                return "refresh"

            app._start_job(
                "local refresh",
                local_refresh,
                lambda _payload: None,
                shared_queue=False,
            )
            deadline = time.monotonic() + 2
            while not entered.is_set() and time.monotonic() < deadline:
                root.update()
                time.sleep(0.01)
            self.assertTrue(entered.is_set())

            app._start_job(
                "mutation",
                lambda _report: order.append("mutation") or "done",
                lambda _payload: None,
                queue_label="mutation",
                shared_queue=True,
            )
            runtime.enqueue.assert_not_called()

            gate.set()
            deadline = time.monotonic() + 3
            while (
                (app.active_job is not None or app.pending_jobs)
                and time.monotonic() < deadline
            ):
                root.update()
                time.sleep(0.01)

            runtime.enqueue.assert_called_once()
            runtime.wait_for_turn.assert_called_once_with(
                "shared-1",
                on_wait=mock.ANY,
                cancel_event=mock.ANY,
            )
            self.assertNotIn(threading.get_ident(), enqueue_threads)
            self.assertEqual(
                order,
                ["refresh-start", "refresh-end", "mutation"],
            )
        finally:
            gate.set()
            root.destroy()

    def test_deferred_enqueue_failure_counts_and_starts_next_local_job(self):
        root = tk.Tk()
        root.withdraw()
        gate = threading.Event()
        try:
            app = GUI.App.__new__(GUI.App)
            app.root = root
            app.job_queue = queue.Queue()
            app.worker = None
            app.job_started_at = None
            app.job_stage = "대기"
            app.status_var = tk.StringVar(root, value="대기")
            app.progress_text_var = tk.StringVar(
                root, value="작업 대기"
            )
            app.progress_bar = ttk.Progressbar(root, maximum=100)
            runtime = mock.Mock()
            runtime.enqueue.side_effect = RuntimeError("queue unavailable")
            app.shared_queue_runtime = runtime
            completed = []

            app._start_job(
                "local refresh",
                lambda _report: gate.wait(2) and "refresh",
                completed.append,
                shared_queue=False,
            )
            app._start_job(
                "blocked mutation",
                lambda _report: "must not run",
                completed.append,
                shared_queue=True,
            )
            app._start_job(
                "next local refresh",
                lambda _report: "next-local",
                completed.append,
                shared_queue=False,
            )

            with mock.patch.object(
                GUI.messagebox, "showerror"
            ) as showerror:
                gate.set()
                deadline = time.monotonic() + 3
                while (
                    (app.active_job is not None or app.pending_jobs)
                    and time.monotonic() < deadline
                ):
                    root.update()
                    time.sleep(0.01)

            self.assertEqual(completed, ["refresh", "next-local"])
            self.assertEqual(app.queue_run_total, 3)
            self.assertEqual(app.queue_run_completed, 3)
            self.assertEqual(len(app.job_failures), 1)
            showerror.assert_not_called()
        finally:
            gate.set()
            root.destroy()

    def test_wait_failure_cancels_unclaimed_shared_ticket(self):
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
            app.progress_text_var = tk.StringVar(
                root, value="작업 대기"
            )
            app.progress_bar = ttk.Progressbar(root, maximum=100)
            runtime = mock.Mock()
            runtime.enqueue.side_effect = [
                {"id": "shared-wait-failure", "sequence": 18},
                {"id": "shared-after-failure", "sequence": 19},
            ]
            lease = mock.Mock()
            lease.finished = False
            runtime.wait_for_turn.side_effect = [
                RuntimeError("queue wait failed"),
                lease,
            ]
            app.shared_queue_runtime = runtime
            failed_func = mock.Mock(return_value="must not run")
            next_completed = []

            with mock.patch.object(GUI.messagebox, "showerror"):
                app._start_job(
                    "mutation",
                    failed_func,
                    lambda _payload: None,
                    shared_queue=True,
                )
                app._start_job(
                    "next mutation",
                    lambda _report: "continued",
                    next_completed.append,
                    shared_queue=True,
                )
                deadline = time.monotonic() + 3
                while (
                    (app.active_job is not None or app.pending_jobs)
                    and time.monotonic() < deadline
                ):
                    root.update()
                    time.sleep(0.01)

            runtime.cancel.assert_called_once_with(
                "shared-wait-failure",
                reason="worker_wait_failed",
            )
            failed_func.assert_not_called()
            runtime.wait_for_turn.assert_has_calls([
                mock.call(
                    "shared-wait-failure",
                    on_wait=mock.ANY,
                    cancel_event=mock.ANY,
                ),
                mock.call(
                    "shared-after-failure",
                    on_wait=mock.ANY,
                    cancel_event=mock.ANY,
                ),
            ])
            lease.finish.assert_called_once()
            self.assertEqual(next_completed, ["continued"])
            self.assertEqual(app.queue_run_completed, 2)
            self.assertEqual(len(app.job_failures), 1)
        finally:
            root.destroy()

    def test_lone_enqueue_failure_reports_once_and_balances_counters(self):
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
            app.progress_text_var = tk.StringVar(
                root, value="작업 대기"
            )
            app.progress_bar = ttk.Progressbar(root, maximum=100)
            runtime = mock.Mock()
            runtime.enqueue.side_effect = RuntimeError("queue unavailable")
            app.shared_queue_runtime = runtime

            with mock.patch.object(
                GUI.messagebox, "showerror"
            ) as showerror:
                job_id = app._start_job(
                    "mutation",
                    lambda _report: "must not run",
                    lambda _payload: None,
                    shared_queue=True,
                )
                deadline = time.monotonic() + 3
                while (
                    app.active_job is not None
                    and time.monotonic() < deadline
                ):
                    root.update()
                    time.sleep(0.01)

            self.assertEqual(job_id, 1)
            self.assertIsNone(app.active_job)
            self.assertFalse(app.pending_jobs)
            self.assertEqual(app.queue_run_total, 1)
            self.assertEqual(app.queue_run_completed, 1)
            self.assertEqual(len(app.job_failures), 1)
            showerror.assert_called_once()
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

    def test_process_output_streams_through_tk_queue_without_blocking(self):
        root = tk.Tk()
        root.withdraw()
        gate = threading.Event()
        try:
            app = GUI.App.__new__(GUI.App)
            app.root = root
            app.job_queue = queue.Queue()
            app.worker = None
            app.status_var = tk.StringVar(root, value="대기")
            app.progress_text_var = tk.StringVar(root, value="작업 대기")
            app.progress_bar = ttk.Progressbar(root, maximum=100)
            app.cancel_job_button = ttk.Button(root)
            app.process_output = tk.Text(root, state="disabled")
            for tag in ("stdout", "stderr", "system"):
                app.process_output.tag_configure(tag)
            app.process_output_line_count = 0
            entered = threading.Event()
            tk_event = threading.Event()
            completed = []

            def work(report):
                report.output("stdout", "first line")
                report.output("stderr", "warning line")
                entered.set()
                gate.wait(2)
                return "ok"

            app._start_job("stream", work, completed.append)
            root.after(0, tk_event.set)
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                root.update()
                text = app.process_output.get("1.0", "end")
                if entered.is_set() and tk_event.is_set() and "warning line" in text:
                    break
                time.sleep(0.01)

            self.assertTrue(entered.is_set())
            self.assertTrue(tk_event.is_set())
            self.assertTrue(app.worker.is_alive())
            text = app.process_output.get("1.0", "end")
            self.assertIn("[stdout] first line", text)
            self.assertIn("[stderr] warning line", text)
            self.assertIn("마지막 출력", app.progress_text_var.get())
            self.assertLess(
                float(app.progress_bar.cget("value")),
                100.0,
            )

            gate.set()
            deadline = time.monotonic() + 2
            while not completed and time.monotonic() < deadline:
                root.update()
                time.sleep(0.01)
            self.assertEqual(completed, ["ok"])
            self.assertEqual(
                float(app.progress_bar.cget("value")),
                100.0,
            )
        finally:
            gate.set()
            root.destroy()

    def test_cancelled_fifo_job_is_not_reported_as_failure(self):
        root = tk.Tk()
        root.withdraw()
        try:
            app = GUI.App.__new__(GUI.App)
            app.root = root
            app.job_queue = queue.Queue()
            app.worker = None
            app.status_var = tk.StringVar(root, value="대기")
            app.progress_text_var = tk.StringVar(root, value="작업 대기")
            app.progress_bar = ttk.Progressbar(root, maximum=100)
            app.cancel_job_button = ttk.Button(root)
            entered = threading.Event()
            shared_results = []

            class Lease:
                finished = False

                def finish(self, *, success, result):
                    self.finished = True
                    shared_results.append((success, result))

            class Runtime:
                def enqueue(self, _label, _payload):
                    return {"id": "shared-1", "sequence": 1}

                def wait_for_turn(self, _job_id, *, on_wait, cancel_event):
                    self.cancel_event = cancel_event
                    return Lease()

            app.shared_queue_runtime = Runtime()

            def work(report):
                entered.set()
                while not report.cancel_requested():
                    time.sleep(0.01)
                report.raise_if_cancelled()

            with mock.patch.object(GUI.messagebox, "showerror") as showerror:
                app._start_job("cancel me", work, lambda _result: None)
                self.assertTrue(entered.wait(2))
                self.assertTrue(app.request_active_job_cancel())
                deadline = time.monotonic() + 3
                while app.active_job is not None and time.monotonic() < deadline:
                    root.update()
                    time.sleep(0.01)

            self.assertIsNone(app.active_job)
            self.assertEqual(app.job_failures, [])
            self.assertEqual(
                app.job_cancellations[0][1],
                "cancelled_at_safe_boundary",
            )
            self.assertIn("취소됨", app.status_var.get())
            self.assertEqual(shared_results[0][0], False)
            self.assertEqual(shared_results[0][1]["outcome"], "cancelled")
            self.assertEqual(
                shared_results[0][1]["termination_state"],
                "cancelled_at_safe_boundary",
            )
            showerror.assert_not_called()
        finally:
            root.destroy()

    def test_committed_success_wins_over_late_cancel_request(self):
        root = tk.Tk()
        root.withdraw()
        try:
            app = GUI.App.__new__(GUI.App)
            app.root = root
            app.job_queue = queue.Queue()
            app.worker = None
            app.status_var = tk.StringVar(root, value="대기")
            app.progress_text_var = tk.StringVar(root, value="작업 대기")
            app.progress_bar = ttk.Progressbar(root, maximum=100)
            app.cancel_job_button = ttk.Button(root)
            completed = []
            shared_results = []

            class Lease:
                finished = False

                def finish(self, *, success, result):
                    self.finished = True
                    shared_results.append((success, result))

            class Runtime:
                def enqueue(self, _label, _payload):
                    return {"id": "shared-commit", "sequence": 1}

                def wait_for_turn(self, _job_id, *, on_wait, cancel_event):
                    return Lease()

            app.shared_queue_runtime = Runtime()

            def commit_then_late_cancel(report):
                report.cancel_event.set()
                return {"commit_marker": "manifest_committed"}

            app._start_job(
                "commit race",
                commit_then_late_cancel,
                completed.append,
            )
            deadline = time.monotonic() + 3
            while app.active_job is not None and time.monotonic() < deadline:
                root.update()
                time.sleep(0.01)

            self.assertEqual(
                completed,
                [{"commit_marker": "manifest_committed"}],
            )
            self.assertEqual(app.job_cancellations, [])
            self.assertEqual(app.job_failures, [])
            self.assertEqual(shared_results[0][0], True)
            self.assertEqual(shared_results[0][1]["outcome"], "completed")
        finally:
            root.destroy()

    def test_partial_payload_keeps_counts_and_report_identity_in_shared_result(self):
        root = tk.Tk()
        root.withdraw()
        try:
            app = GUI.App.__new__(GUI.App)
            app.root = root
            app.job_queue = queue.Queue()
            app.worker = None
            app.status_var = tk.StringVar(root, value="대기")
            app.progress_text_var = tk.StringVar(root, value="작업 대기")
            app.progress_bar = ttk.Progressbar(root, maximum=100)
            app.cancel_job_button = ttk.Button(root)
            shared_results = []
            completed = []

            class Lease:
                finished = False

                def finish(self, *, success, result):
                    self.finished = True
                    shared_results.append((success, result))

            class Runtime:
                def enqueue(self, _label, _payload):
                    return {"id": "shared-partial", "sequence": 1}

                def wait_for_turn(self, _job_id, *, on_wait, cancel_event):
                    return Lease()

            app.shared_queue_runtime = Runtime()
            payload = {
                "status": "partial",
                "summary": {
                    "generator": {
                        "succeeded": 11,
                        "failed": 1,
                        "pending": 0,
                        "total": 12,
                    },
                    "cluster": {
                        "succeeded": 31,
                        "failed": 8,
                        "pending": 0,
                        "total": 39,
                    },
                    "failures": 9,
                },
                "report_identity": {
                    "path": r"C:\reports\connected.json",
                    "sha256": "b" * 64,
                    "size": 4096,
                },
            }
            app._start_job(
                "partial receipt",
                lambda _report: payload,
                completed.append,
            )
            deadline = time.monotonic() + 3
            while app.active_job is not None and time.monotonic() < deadline:
                root.update()
                time.sleep(0.01)

            self.assertEqual(completed, [payload])
            self.assertEqual(shared_results[0][0], False)
            receipt = shared_results[0][1]
            self.assertEqual(receipt["outcome"], "partial")
            self.assertEqual(receipt["counts"]["generator"]["succeeded"], 11)
            self.assertEqual(receipt["counts"]["cluster"]["succeeded"], 31)
            self.assertEqual(receipt["counts"]["failures"], 9)
            self.assertEqual(receipt["report"]["sha256"], "b" * 64)
        finally:
            root.destroy()

    def test_cancel_event_does_not_mask_rollback_failure(self):
        root = tk.Tk()
        root.withdraw()
        try:
            app = GUI.App.__new__(GUI.App)
            app.root = root
            app.job_queue = queue.Queue()
            app.worker = None
            app.status_var = tk.StringVar(root, value="대기")
            app.progress_text_var = tk.StringVar(root, value="작업 대기")
            app.progress_bar = ttk.Progressbar(root, maximum=100)
            app.cancel_job_button = ttk.Button(root)
            shared_results = []

            class Lease:
                finished = False

                def finish(self, *, success, result):
                    self.finished = True
                    shared_results.append((success, result))

            class Runtime:
                def enqueue(self, _label, _payload):
                    return {"id": "shared-rollback", "sequence": 1}

                def wait_for_turn(self, _job_id, *, on_wait, cancel_event):
                    return Lease()

            app.shared_queue_runtime = Runtime()

            def rollback_fails(report):
                report.cancel_event.set()
                raise RuntimeError(
                    "[transaction_rollback_failed] restore denied"
                )

            with mock.patch.object(GUI.messagebox, "showerror"):
                app._start_job("rollback race", rollback_fails, lambda _value: None)
                deadline = time.monotonic() + 3
                while app.active_job is not None and time.monotonic() < deadline:
                    root.update()
                    time.sleep(0.01)

            self.assertEqual(app.job_cancellations, [])
            self.assertEqual(len(app.job_failures), 1)
            self.assertIn("transaction_rollback_failed", app.job_failures[0][1])
            self.assertEqual(shared_results[0][0], False)
            self.assertEqual(shared_results[0][1]["outcome"], "failed")
        finally:
            root.destroy()

    def test_shared_wait_cancel_has_before_launch_reason_and_never_runs_func(self):
        root = tk.Tk()
        root.withdraw()
        try:
            app = GUI.App.__new__(GUI.App)
            app.root = root
            app.job_queue = queue.Queue()
            app.worker = None
            app.status_var = tk.StringVar(root, value="대기")
            app.progress_text_var = tk.StringVar(root, value="작업 대기")
            app.progress_bar = ttk.Progressbar(root, maximum=100)
            app.cancel_job_button = ttk.Button(root)
            ran = []

            class Runtime:
                def enqueue(self, _label, _payload):
                    return {"id": "shared-wait", "sequence": 1}

                def wait_for_turn(self, _job_id, *, on_wait, cancel_event):
                    cancel_event.set()
                    raise GUI.WaitCancelled("cancelled while queued")

                def cancel(self, _job_id, *, reason):
                    return None

            app.shared_queue_runtime = Runtime()

            app._start_job(
                "wait cancel",
                lambda _report: ran.append(True),
                lambda _value: None,
            )
            deadline = time.monotonic() + 3
            while app.active_job is not None and time.monotonic() < deadline:
                root.update()
                time.sleep(0.01)

            self.assertEqual(ran, [])
            self.assertEqual(app.job_failures, [])
            self.assertEqual(
                app.job_cancellations,
                [("wait cancel", "cancelled_before_launch")],
            )
        finally:
            root.destroy()

    def test_chatty_output_is_rendered_in_bounded_tk_batches(self):
        root = tk.Tk()
        root.withdraw()
        try:
            app = GUI.App.__new__(GUI.App)
            app.root = root
            app.job_queue = queue.Queue()
            app.worker = None
            app.status_var = tk.StringVar(root, value="대기")
            app.progress_text_var = tk.StringVar(root, value="작업 대기")
            app.progress_bar = ttk.Progressbar(root, maximum=100)
            app.process_output = tk.Text(root, state="disabled")
            for tag in ("stdout", "stderr", "system"):
                app.process_output.tag_configure(tag)
            app.process_output_line_count = 0
            completed = []
            ticks = []
            tick_after = [None]

            def work(report):
                for index in range(4000):
                    report.output(
                        "stderr" if index % 2 else "stdout",
                        f"line {index}",
                    )
                return "ok"

            def tick():
                ticks.append(time.monotonic())
                if app.active_job is not None:
                    tick_after[0] = root.after(1, tick)
                else:
                    tick_after[0] = None

            app._start_job("chatty", work, completed.append)
            tick_after[0] = root.after(0, tick)
            deadline = time.monotonic() + 5
            while not completed and time.monotonic() < deadline:
                root.update()
                time.sleep(0.001)

            self.assertEqual(completed, ["ok"])
            self.assertGreater(len(ticks), 2)
            self.assertLessEqual(app.process_output_line_count, 3200)
            self.assertIn("line 3999", app.process_output.get("1.0", "end"))
        finally:
            if "tick_after" in locals() and tick_after[0] is not None:
                try:
                    root.after_cancel(tick_after[0])
                except tk.TclError:
                    pass
            root.destroy()

    def test_one_hundred_thousand_producer_lines_use_bounded_coalesced_buffer(self):
        root = tk.Tk()
        root.withdraw()
        try:
            app = GUI.App.__new__(GUI.App)
            app.root = root
            app.job_queue = queue.Queue()
            app.worker = None
            app.status_var = tk.StringVar(root, value="대기")
            app.progress_text_var = tk.StringVar(root, value="작업 대기")
            app.progress_bar = ttk.Progressbar(root, maximum=100)
            app.process_output = tk.Text(root, state="disabled")
            for tag in ("stdout", "stderr", "system"):
                app.process_output.tag_configure(tag)
            app.process_output_line_count = 0
            completed = []

            def work(report):
                for index in range(100_000):
                    report.output("stdout", f"producer-line-{index}")
                report.output("stderr", "final-stderr-evidence")
                return "ok"

            app._start_job("100k producer", work, completed.append)
            app.worker.join(timeout=10)
            self.assertFalse(app.worker.is_alive())
            self.assertLessEqual(
                len(app.active_job["output_buffer"]),
                GUI.PROCESS_OUTPUT_BUFFER_LINES,
            )
            self.assertLessEqual(app.job_queue.qsize(), 4)

            deadline = time.monotonic() + 8
            while app.active_job is not None and time.monotonic() < deadline:
                root.update()
                time.sleep(0.001)

            self.assertEqual(completed, ["ok"])
            rendered = app.process_output.get("1.0", "end")
            self.assertIn("process_output_omitted", rendered)
            self.assertIn("final-stderr-evidence", rendered)
        finally:
            root.destroy()

    def test_shutdown_completion_does_not_expire_after_virtual_five_seconds(self):
        scheduled = []
        completed = []

        class Root:
            def after(self, delay, callback):
                scheduled.append((delay, callback))
                return len(scheduled)

            def after_idle(self, callback):
                return self.after(0, callback)

        class Worker:
            alive = True

            def is_alive(self):
                return self.alive

        app = GUI.App.__new__(GUI.App)
        app.root = Root()
        app.worker = Worker()
        app.active_job = {"cancel_event": threading.Event()}
        pending_ran = []
        app.pending_jobs = deque([
            {
                "cancel_event": threading.Event(),
                "func": lambda _report: pending_ran.append(True),
            }
        ])
        app.shared_queue_runtime = mock.Mock()

        app.shutdown_shared_queue(on_complete=lambda: completed.append("done"))
        self.assertTrue(app.active_job["cancel_event"].is_set())
        self.assertEqual(pending_ran, [])

        for _poll in range(121):
            _delay, callback = scheduled.pop(0)
            callback()
        self.assertEqual(completed, [], "virtual 6.05s must not force close")

        app.worker.alive = False
        _delay, callback = scheduled.pop(0)
        callback()
        self.assertEqual(completed, ["done"])
        app.shared_queue_runtime.shutdown.assert_called_once_with()

    def test_shutdown_surfaces_fail_closed_termination_reason_before_completion(self):
        root = tk.Tk()
        root.withdraw()
        try:
            app = GUI.App.__new__(GUI.App)
            app.root = root
            app.worker = None
            app.pending_jobs = deque()
            app.active_job = {
                "cancel_event": threading.Event(),
                "worker_terminal_payload": RuntimeError(
                    "[process_kill_grace_expired] pid=4242"
                ),
            }
            app.shared_queue_runtime = mock.Mock()
            completed = []

            with mock.patch.object(GUI.messagebox, "showerror") as showerror:
                app.shutdown_shared_queue(
                    on_complete=lambda: completed.append(True)
                )
                deadline = time.monotonic() + 2
                while not completed and time.monotonic() < deadline:
                    root.update()
                    time.sleep(0.01)

            self.assertEqual(completed, [True])
            showerror.assert_called_once()
            self.assertIn(
                "process_kill_grace_expired",
                showerror.call_args.args[1],
            )
        finally:
            root.destroy()

    def test_window_shutdown_terminates_active_speedtree_process(self):
        root = tk.Tk()
        root.withdraw()
        try:
            app = GUI.App.__new__(GUI.App)
            app.root = root
            app.job_queue = queue.Queue()
            app.worker = None
            app.status_var = tk.StringVar(root, value="대기")
            app.progress_text_var = tk.StringVar(root, value="작업 대기")
            app.progress_bar = ttk.Progressbar(root, maximum=100)
            app.cancel_job_button = ttk.Button(root)
            ready = threading.Event()

            with tempfile.TemporaryDirectory() as temp:
                folder = Path(temp)
                script = folder / "slow.spm"
                options = folder / "Options_HI_Xml.ini"
                script.write_text(
                    "import time\nprint('ready', flush=True)\ntime.sleep(30)\n",
                    encoding="utf-8",
                )
                options.write_text(
                    "[Options]\nTextureSkipWriting=true\n",
                    encoding="utf-8",
                )

                def work(report):
                    def output(channel, line):
                        report.output(channel, line)
                        if line == "ready":
                            ready.set()

                    return GUI.engine.verify_speedtree_export(
                        script,
                        Path(sys.executable),
                        options,
                        output_callback=output,
                        cancel_requested=report.cancel_requested,
                    )

                app._start_job("slow SpeedTree", work, lambda _result: None)
                self.assertTrue(ready.wait(3))
                closing_job = app.active_job
                shutdown_completed = []
                app.shutdown_shared_queue(
                    on_complete=lambda: shutdown_completed.append(True)
                )
                deadline = time.monotonic() + 8
                while not shutdown_completed and time.monotonic() < deadline:
                    root.update()
                    time.sleep(0.01)

            self.assertEqual(shutdown_completed, [True])
            self.assertTrue(app.worker is None or not app.worker.is_alive())
            done_events = []
            while not app.job_queue.empty():
                event = app.job_queue.get_nowait()
                if event[0] == "done":
                    done_events.append(event)
            cancelled = (
                done_events[0][3]
                if done_events
                else closing_job.get("terminal_payload")
            )
            self.assertIsInstance(cancelled, GUI.engine.SyncCancelled)
            self.assertIn(
                cancelled.termination_state,
                {"cancelled_terminated", "cancelled_killed"},
            )
            self.assertIsNotNone(cancelled.pid)
            self.assertIsNotNone(cancelled.returncode)
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
