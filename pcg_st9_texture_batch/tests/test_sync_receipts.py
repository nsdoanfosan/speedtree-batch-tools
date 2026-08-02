import ast
import importlib.machinery
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


TOOL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOL_DIR))

import unreal_texture_sync


def load_gui_module():
    path = TOOL_DIR / "pcg_texture_gui.pyw"
    loader = importlib.machinery.SourceFileLoader(
        "pcg_texture_gui_sync_receipt_test", str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class SyncReceiptTests(unittest.TestCase):
    def test_invalid_unreal_name_is_rejected_before_remote_execution(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "T_Material 2_color.tga"
            source.write_bytes(b"pixels")
            cfg = {
                "unreal_project": r"C:\UnrealProjects\MyProject2",
                "unreal_texture_sync_enabled": True,
                "unreal_texture_commandlet_fallback": True,
                "unreal_texture_destination": "/Game/Textures",
            }
            with mock.patch.object(
                    unreal_texture_sync, "_editor_is_running",
                    return_value=True), mock.patch.object(
                        unreal_texture_sync, "_run_remote") as remote:
                with self.assertRaisesRegex(ValueError, "invalid characters"):
                    unreal_texture_sync.sync_texture_files([source], cfg=cfg)
            remote.assert_not_called()

    def test_sync_writes_atomic_receipt_and_reports_progress(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "T_tree_color.tga"
            source.write_bytes(b"pixels")
            events = []

            def fake_commandlet(script_path, _cfg):
                script = Path(script_path).read_text(encoding="utf-8")
                output_line = next(
                    line for line in script.splitlines()
                    if line.startswith("OUTPUT_PATH = "))
                entries_line = next(
                    line for line in script.splitlines()
                    if line.startswith("ENTRIES = "))
                output = Path(ast.literal_eval(output_line.split(" = ", 1)[1]))
                entries = ast.literal_eval(entries_line.split(" = ", 1)[1])
                entry = entries[0]
                output.write_text(json.dumps({
                    "entries": [{
                        "source": entry["source"],
                        "asset_path": entry["asset_path"],
                        "role": entry["role"],
                        "source_md5": entry["md5"],
                        "imported_md5_after": entry["md5"],
                        "status": "unchanged",
                    }],
                    "counts": {"unchanged": 1},
                    "errors": [],
                }), encoding="utf-8")

            cfg = {
                "unreal_project": r"C:\UnrealProjects\MyProject2",
                "unreal_texture_sync_enabled": True,
                "unreal_texture_commandlet_fallback": True,
                "unreal_texture_destination": "/Game/Textures",
            }
            with mock.patch.object(
                    unreal_texture_sync, "REPORT_DIR", root), mock.patch.object(
                        unreal_texture_sync, "_editor_is_running",
                        return_value=False), mock.patch.object(
                            unreal_texture_sync, "_run_commandlet",
                            side_effect=fake_commandlet):
                result = unreal_texture_sync.sync_texture_files(
                    [source], cfg=cfg, progress=events.append)
                state = unreal_texture_sync.load_sync_state(migrate=False)
                self.assertTrue(unreal_texture_sync.is_texture_synced(
                    source, state=state))
                state_path = root / unreal_texture_sync.SYNC_STATE_FILENAME
                self.assertTrue(state_path.is_file())
                stored = json.loads(state_path.read_text(encoding="utf-8"))
                receipt = next(iter(stored["entries"].values()))
                self.assertEqual(receipt["size"], source.stat().st_size)
                self.assertEqual(receipt["mtime_ns"], source.stat().st_mtime_ns)
                self.assertEqual(receipt["status"], "unchanged")
                self.assertEqual(
                    receipt["asset_path"], "/Game/Textures/T_tree_color")
                self.assertTrue(receipt["md5"])
                self.assertTrue(receipt["sha256"])
                self.assertFalse(list(root.glob("*.tmp")))

            self.assertEqual(result["mode"], "headless_commandlet")
            self.assertEqual(result["receipt_count"], 1)
            self.assertEqual([event["phase"] for event in events], [
                "hashing", "unreal",
            ])
            self.assertIn("1/1", events[0]["message"])
            self.assertIn("1장", events[1]["message"])
            persisted = json.loads(
                Path(result["report_path"]).read_text(encoding="utf-8"))
            self.assertEqual(persisted["mode"], "headless_commandlet")
            self.assertIn("duration_seconds", persisted)
            self.assertIn("receipt_state_path", persisted)

    def test_receipt_accepts_same_content_when_size_or_mtime_differs(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "T_tree_height.tga"
            source.write_bytes(b"same pixels")
            entry = unreal_texture_sync.canonical_texture_entries([source])[0]
            report = {
                "mode": "test",
                "entries": [{
                    "source": entry["source"],
                    "asset_path": entry["asset_path"],
                    "source_md5": entry["md5"],
                    "imported_md5_before": entry["md5"],
                    "status": "unchanged",
                }],
            }
            with mock.patch.object(unreal_texture_sync, "REPORT_DIR", root):
                state = unreal_texture_sync._record_sync_receipts(
                    report, [entry], report_path=root / "report.json")

            receipt = next(iter(state["entries"].values()))
            initial = source.stat()
            os.utime(source, ns=(
                initial.st_atime_ns,
                initial.st_mtime_ns + 2_000_000_000,
            ))
            with mock.patch.object(
                    unreal_texture_sync, "file_hashes",
                    wraps=unreal_texture_sync.file_hashes) as hashes:
                self.assertTrue(unreal_texture_sync.is_texture_synced(
                    source, state=state))
                receipt["mtime_ns"] = source.stat().st_mtime_ns
                receipt["size"] += 1
                self.assertTrue(unreal_texture_sync.is_texture_synced(
                    source, state=state))
            self.assertEqual(hashes.call_count, 2)

    def test_receipt_is_invalidated_when_source_content_changes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "T_tree_normal.tga"
            source.write_bytes(b"first")
            entry = unreal_texture_sync.canonical_texture_entries([source])[0]
            report = {
                "mode": "test",
                "entries": [{
                    "source": entry["source"],
                    "asset_path": entry["asset_path"],
                    "source_md5": entry["md5"],
                    "imported_md5_before": entry["md5"],
                    "status": "unchanged",
                }],
            }
            with mock.patch.object(unreal_texture_sync, "REPORT_DIR", root):
                state = unreal_texture_sync._record_sync_receipts(
                    report, [entry], report_path=root / "report.json")
                self.assertTrue(unreal_texture_sync.is_texture_synced(
                    source, state=state))
                source.write_bytes(b"other")
                changed = source.stat()
                os.utime(source, ns=(
                    changed.st_atime_ns,
                    entry["mtime_ns"] + 2_000_000_000,
                ))
                self.assertEqual(entry["size"], source.stat().st_size)
                self.assertFalse(unreal_texture_sync.is_texture_synced(
                    source, state=state))

    def test_receipt_is_invalidated_when_sync_settings_change(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "T_tree_subsurface.tga"
            source.write_bytes(b"pixels")
            entry = unreal_texture_sync.canonical_texture_entries([source])[0]
            report = {
                "mode": "test",
                "entries": [{
                    "source": entry["source"],
                    "asset_path": entry["asset_path"],
                    "source_md5": entry["md5"],
                    "imported_md5_before": entry["md5"],
                    "status": "unchanged",
                }],
            }
            with mock.patch.object(unreal_texture_sync, "REPORT_DIR", root):
                state = unreal_texture_sync._record_sync_receipts(
                    report, [entry], report_path=root / "report.json")

            with mock.patch.object(
                    unreal_texture_sync, "file_hashes",
                    side_effect=AssertionError("settings must fail before hash")):
                self.assertFalse(unreal_texture_sync.is_texture_synced(
                    source, state=state, destination="/Game/OtherTextures"))
                with mock.patch.object(
                        unreal_texture_sync, "POLICY_VERSION",
                        entry["policy_version"] + 1):
                    self.assertFalse(unreal_texture_sync.is_texture_synced(
                        source, state=state))

    def test_requested_source_missing_from_report_loses_old_receipt(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "T_tree_opacity.tga"
            source.write_bytes(b"pixels")
            entry = unreal_texture_sync.canonical_texture_entries([source])[0]
            success = {
                "mode": "test",
                "entries": [{
                    "source": entry["source"],
                    "asset_path": entry["asset_path"],
                    "source_md5": entry["md5"],
                    "imported_md5_before": entry["md5"],
                    "status": "unchanged",
                }],
            }
            with mock.patch.object(unreal_texture_sync, "REPORT_DIR", root):
                state = unreal_texture_sync._record_sync_receipts(
                    success, [entry], report_path=root / "success.json")
                self.assertTrue(unreal_texture_sync.is_texture_synced(
                    source, state=state))
                state = unreal_texture_sync._record_sync_receipts(
                    {"mode": "test", "entries": []}, [entry],
                    report_path=root / "incomplete.json")
                self.assertFalse(unreal_texture_sync.is_texture_synced(
                    source, state=state))

    def test_failed_full_recheck_invalidates_old_receipt(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "T_tree_subsurface.tga"
            source.write_bytes(b"pixels")
            entry = unreal_texture_sync.canonical_texture_entries([source])[0]
            success = {
                "mode": "test",
                "entries": [{
                    "source": entry["source"],
                    "asset_path": entry["asset_path"],
                    "source_md5": entry["md5"],
                    "imported_md5_before": entry["md5"],
                    "status": "unchanged",
                }],
            }
            failed = {
                "mode": "test",
                "entries": [{
                    "source": entry["source"],
                    "asset_path": entry["asset_path"],
                    "source_md5": entry["md5"],
                    "status": "error",
                    "error": "asset disappeared and recreation failed",
                }],
            }
            with mock.patch.object(unreal_texture_sync, "REPORT_DIR", root):
                state = unreal_texture_sync._record_sync_receipts(
                    success, [entry], report_path=root / "success.json")
                self.assertTrue(unreal_texture_sync.is_texture_synced(
                    source, state=state))
                state = unreal_texture_sync._record_sync_receipts(
                    failed, [entry], report_path=root / "forced_verify.json")
                self.assertFalse(unreal_texture_sync.is_texture_synced(
                    source, state=state))

    def test_legacy_report_migrates_only_matching_current_bytes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "T_tree_extra.tga"
            source.write_bytes(b"same")
            md5, _sha256 = unreal_texture_sync.file_hashes(source)
            report_path = root / "unreal_texture_sync_20260717_120000_000000.json"
            report_path.write_text(json.dumps({
                "entries": [{
                    "source": str(source),
                    "asset_path": "/Game/Textures/T_tree_extra",
                    "source_md5": md5,
                    "imported_md5_before": md5,
                    "status": "unchanged",
                }],
                "counts": {"unchanged": 1},
                "errors": [],
            }), encoding="utf-8")
            with mock.patch.object(unreal_texture_sync, "REPORT_DIR", root):
                state = unreal_texture_sync.load_sync_state()
                self.assertTrue(unreal_texture_sync.is_texture_synced(
                    source, state=state))

    def test_legacy_report_does_not_migrate_changed_source(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "T_tree_height.tga"
            source.write_bytes(b"old")
            old_md5, _sha256 = unreal_texture_sync.file_hashes(source)
            source.write_bytes(b"changed after sync")
            report_path = root / "unreal_texture_sync_20260717_120000_000000.json"
            report_path.write_text(json.dumps({
                "entries": [{
                    "source": str(source),
                    "asset_path": "/Game/Textures/T_tree_height",
                    "source_md5": old_md5,
                    "imported_md5_before": old_md5,
                    "status": "unchanged",
                }],
                "counts": {"unchanged": 1},
                "errors": [],
            }), encoding="utf-8")
            with mock.patch.object(unreal_texture_sync, "REPORT_DIR", root):
                state = unreal_texture_sync.load_sync_state()
                self.assertFalse(unreal_texture_sync.is_texture_synced(
                    source, state=state))

    def test_receipt_requires_unreal_imported_md5_proof(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "T_tree_color.tga"
            source.write_bytes(b"pixels")
            entry = unreal_texture_sync.canonical_texture_entries([source])[0]
            report = {
                "mode": "test",
                "entries": [{
                    "source": entry["source"],
                    "asset_path": entry["asset_path"],
                    "source_md5": entry["md5"],
                    "imported_md5_after": "0" * 32,
                    "status": "reimported",
                }],
            }
            with mock.patch.object(unreal_texture_sync, "REPORT_DIR", root):
                state = unreal_texture_sync._record_sync_receipts(
                    report, [entry], report_path=root / "mismatch.json")
            self.assertFalse(unreal_texture_sync.is_texture_synced(
                source, state=state))

    def test_newer_failed_report_blocks_older_success_migration(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "T_tree_normal.tga"
            source.write_bytes(b"pixels")
            md5, _sha256 = unreal_texture_sync.file_hashes(source)
            older = root / "unreal_texture_sync_20260717_120000_000000.json"
            newer = root / "unreal_texture_sync_20260717_130000_000000.json"
            older.write_text(json.dumps({"entries": [{
                "source": str(source),
                "asset_path": "/Game/Textures/T_tree_normal",
                "source_md5": md5,
                "imported_md5_before": md5,
                "status": "unchanged",
            }]}), encoding="utf-8")
            newer.write_text(json.dumps({"entries": [{
                "source": str(source),
                "asset_path": "/Game/Textures/T_tree_normal",
                "source_md5": md5,
                "status": "error",
            }]}), encoding="utf-8")
            os.utime(older, ns=(1_000_000_000, 1_000_000_000))
            os.utime(newer, ns=(2_000_000_000, 2_000_000_000))
            with mock.patch.object(unreal_texture_sync, "REPORT_DIR", root):
                state = unreal_texture_sync.load_sync_state()
            self.assertFalse(unreal_texture_sync.is_texture_synced(
                source, state=state))

    def test_newer_dry_run_does_not_hide_older_success_migration(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "T_tree_opacity.tga"
            source.write_bytes(b"pixels")
            md5, _sha256 = unreal_texture_sync.file_hashes(source)
            older = root / "unreal_texture_sync_20260717_120000_000000.json"
            newer = root / "unreal_texture_sync_20260717_130000_000000.json"
            older.write_text(json.dumps({"entries": [{
                "source": str(source),
                "asset_path": "/Game/Textures/T_tree_opacity",
                "source_md5": md5,
                "imported_md5_before": md5,
                "status": "unchanged",
            }]}), encoding="utf-8")
            newer.write_text(json.dumps({
                "dry_run": True,
                "entries": [{
                    "source": str(source),
                    "asset_path": "/Game/Textures/T_tree_opacity",
                    "source_md5": md5,
                    "status": "would_reimport",
                }],
            }), encoding="utf-8")
            os.utime(older, ns=(1_000_000_000, 1_000_000_000))
            os.utime(newer, ns=(2_000_000_000, 2_000_000_000))
            with mock.patch.object(unreal_texture_sync, "REPORT_DIR", root):
                state = unreal_texture_sync.load_sync_state()
            self.assertTrue(unreal_texture_sync.is_texture_synced(
                source, state=state))

    def test_empty_state_does_not_stat_one_drive_texture(self):
        path = Path(r"D:\OneDrive\Tree\T_tree_color.tga")
        with mock.patch.object(
                Path, "stat", side_effect=AssertionError("unexpected stat")):
            self.assertFalse(unreal_texture_sync.is_texture_synced(
                path, state={"entries": {}}))


class GuiSyncStateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.gui = load_gui_module()

    def test_complete_receipted_set_offers_full_unreal_recheck(self):
        item = {"cluster_items": [{
            "missing_export_maps": [],
            "unreal_synced": True,
        }]}
        state = self.gui.step3_selection_state({
            "row": {"checked": True, "item": item},
        })
        self.assertEqual(state["state"], "normal")
        self.assertTrue(state["force_unreal_verify"])
        self.assertIn("Unreal 전체 재확인", state["text"])
        self.assertIn("Unreal 최신", self.gui.App.step3_text(None, item))

    def test_texture_plan_exception_is_recorded_as_a_blocker(self):
        app = self.gui.App.__new__(self.gui.App)
        app.texplan_cache = {}
        app.texplan_errors = {}
        app.report = {"pcg_targets": {}}
        item = {"folder": r"D:\Tree\broken", "cluster_items": [{}]}
        with mock.patch.object(
                self.gui, "build_texture_plan_from_report",
                side_effect=RuntimeError("plan exploded")):
            self.assertEqual(app._texplan_rows(item), [])
        self.assertEqual(app.texplan_errors[item["folder"]], "plan exploded")

    def test_skipped_texture_plan_blocks_step3_before_worker_starts(self):
        class Root:
            @staticmethod
            def update_idletasks():
                return None

        class Value:
            def __init__(self):
                self.value = None

            def set(self, value):
                self.value = value

        item = {"name": "broken_tree", "folder": r"D:\Tree\broken"}
        app = self.gui.App.__new__(self.gui.App)
        app.report = {"items": []}
        app.root = Root()
        app.status_var = Value()
        app.log = mock.Mock()
        app._set_busy = mock.Mock()
        app._step3_jobs = mock.Mock(return_value=(
            [], [(item, "T_broken", "missing source")]))
        app._step3_sync_files = mock.Mock(return_value=[])
        plan = app._build_step3_execution_plan()
        with mock.patch.object(self.gui.messagebox, "showerror") as showerror:
            app._step3_planning_done(plan)
        showerror.assert_called_once()
        self.assertIn("실행 차단", app.status_var.value)
        self.assertIsNone(app.worker)

    def test_skipped_texture_plan_can_exclude_errors_and_run_valid_work(self):
        class Root:
            @staticmethod
            def update_idletasks():
                return None

        class Value:
            def __init__(self):
                self.value = None

            def set(self, value):
                self.value = value

        good = {
            "name": "good_tree",
            "folder": r"D:\Tree\good",
            "sk_spms": [r"D:\Tree\good\SK_good.spm"],
        }
        broken = {
            "name": "broken_tree",
            "folder": r"D:\Tree\broken",
            "sk_spms": [r"D:\Tree\broken\SK_broken.spm"],
        }
        good_job = {
            "item": good,
            "base": "M_leaf_good",
            "texture_base": "T_leaf_good",
            "mode": "render",
        }
        skipped = [(broken, "M_leaf_broken", "missing albedo")]
        sync_files = [r"D:\Tree\good\texture\T_leaf_good_color.tga"]
        app = self.gui.App.__new__(self.gui.App)
        app.report = {"items": [good, broken]}
        app.root = Root()
        app.status_var = Value()
        app.items = {
            good["folder"]: {"checked": True, "item": good},
            broken["folder"]: {"checked": True, "item": broken},
        }
        app.log = mock.Mock()
        app._set_busy = mock.Mock()
        app._step3_jobs = mock.Mock(return_value=([good_job], skipped))
        app._step3_sync_files = mock.Mock(return_value=sync_files)
        app._checked_texplan_rows = mock.Mock(return_value=[
            (good, {"atlas_base": "M_leaf_good"}),
            (broken, {"atlas_base": "M_leaf_broken"}),
        ])
        app._step3_unreal_name_errors = mock.Mock(return_value=[])
        app._begin_step3_run_report = mock.Mock(
            return_value=(None, None)
        )
        fake_worker = mock.Mock()

        with mock.patch.object(
                self.gui, "step3_selection_state",
                return_value={"force_unreal_verify": False}
        ), mock.patch.object(
                self.gui.messagebox, "askyesno", return_value=True
        ) as askyesno, mock.patch.object(
                self.gui.threading, "Thread", return_value=fake_worker
        ) as thread_class:
            def invoke_worker_target():
                call = thread_class.call_args.kwargs
                call["target"](
                    *call.get("args", ()),
                    **call.get("kwargs", {}),
                )

            app._run_step3 = mock.Mock()
            fake_worker.start.side_effect = invoke_worker_target
            plan = app._build_step3_execution_plan()
            app._step3_planning_done(plan)

        askyesno.assert_not_called()
        excluded = app._step3_exclusion_keys(skipped)
        app._step3_sync_files.assert_called_once_with(excluded)
        thread_kwargs = thread_class.call_args.kwargs
        self.assertEqual(thread_kwargs["args"], (
            [good_job],
            [good["sk_spms"][0]],
            sync_files,
            False,
        ))
        self.assertEqual(
            thread_kwargs["kwargs"],
            {
                "planned_skipped": 1,
                "allowed_step3_row_keys": (
                    self.gui.step3_texture_row_key(
                        good,
                        "M_leaf_good",
                    ),
                ),
            },
        )
        self.assertIn("오류 항목 제외 1개", app.status_var.value)
        self.assertTrue(any(
            "missing albedo" in call.args[0]
            for call in app.log.call_args_list
        ))
        fake_worker.start.assert_called_once_with()
        app._run_step3.assert_called_once_with(
            [good_job],
            [good["sk_spms"][0]],
            sync_files,
            False,
            planned_skipped=1,
            allowed_step3_row_keys=(
                self.gui.step3_texture_row_key(
                    good,
                    "M_leaf_good",
                ),
            ),
        )

    def test_invalid_unreal_name_excludes_only_its_row_and_runs_valid_work(self):
        class Root:
            @staticmethod
            def update_idletasks():
                return None

        class Value:
            def __init__(self):
                self.value = None

            def set(self, value):
                self.value = value

        good = {
            "name": "good_tree",
            "folder": r"D:\Tree\good",
            "sk_spms": [r"D:\Tree\good\SK_good.spm"],
        }
        broken = {
            "name": "copy_bark_tree",
            "folder": r"D:\Tree\copy_bark",
            "sk_spms": [r"D:\Tree\copy_bark\SK_copy_bark.spm"],
        }
        good_job = {
            "item": good,
            "base": "M_leaf_good",
            "texture_base": "T_leaf_good",
            "out_dir": r"D:\Tree\good\texture",
            "mode": "render",
        }
        broken_job = {
            "item": broken,
            "base": "M_Bark Copy",
            "texture_base": "T_Bark Copy",
            "out_dir": r"D:\Tree\copy_bark\texture",
            "mode": "render",
        }
        app = self.gui.App.__new__(self.gui.App)
        app.report = {"items": [good, broken]}
        app.root = Root()
        app.status_var = Value()
        app.items = {
            good["folder"]: {"checked": True, "item": good},
            broken["folder"]: {"checked": True, "item": broken},
        }
        app.log = mock.Mock()
        app._set_busy = mock.Mock()
        app._step3_jobs = mock.Mock(
            return_value=([good_job, broken_job], [])
        )
        app._step3_sync_files = mock.Mock(return_value=[])
        app._checked_texplan_rows = mock.Mock(return_value=[
            (good, {"atlas_base": good_job["base"]}),
            (broken, {"atlas_base": broken_job["base"]}),
        ])
        app._begin_step3_run_report = mock.Mock(
            return_value=(None, None)
        )
        fake_worker = mock.Mock()

        with mock.patch.object(
                self.gui, "step3_selection_state",
                return_value={"force_unreal_verify": False}
        ), mock.patch.object(
                self.gui.messagebox, "askyesno", return_value=True
        ) as askyesno, mock.patch.object(
                self.gui.threading, "Thread", return_value=fake_worker
        ) as thread_class:
            plan = app._build_step3_execution_plan()
            app._step3_planning_done(plan)

        askyesno.assert_not_called()
        self.assertEqual(
            thread_class.call_args.kwargs["args"],
            (
                [good_job],
                [good["sk_spms"][0]],
                [],
                False,
            ),
        )
        self.assertEqual(
            thread_class.call_args.kwargs["kwargs"],
            {
                "planned_skipped": 1,
                "allowed_step3_row_keys": (
                    self.gui.step3_texture_row_key(
                        good,
                        good_job["base"],
                    ),
                ),
            },
        )
        self.assertIn("오류 항목 제외 1개", app.status_var.value)
        self.assertTrue(any(
            "T_Bark Copy_color" in call.args[0]
            for call in app.log.call_args_list
        ))
        fake_worker.start.assert_called_once_with()

    def test_checked_shared_row_builds_unchecked_owner_render_job(self):
        app = self.gui.App.__new__(self.gui.App)
        owner_item = {
            "name": "tree_owner",
            "folder": r"D:\Tree\owner",
        }
        selected_item = {
            "name": "tree_consumer",
            "folder": r"D:\Tree\consumer",
        }
        owner_row = {
            "folder": owner_item["folder"],
            "atlas_base": "M_leaf_shared",
            "texture_base": "T_leaf_shared",
            "source_signature": ["D:/Texture/leaf_color.tif"],
        }
        selected_row = {
            "folder": selected_item["folder"],
            "atlas_base": "M_leaf_shared",
            "texture_base": "T_leaf_shared",
            "source_signature": ["D:/Texture/leaf_color.tif"],
            "shared_from": owner_item["name"],
        }
        app.items = {
            owner_item["folder"]: {
                "checked": False,
                "item": owner_item,
            },
            selected_item["folder"]: {
                "checked": True,
                "item": selected_item,
            },
        }
        app.texplan_errors = {}
        app._checked_texplan_rows = mock.Mock(return_value=[
            (selected_item, selected_row),
        ])
        app._texplan_rows = mock.Mock(side_effect=lambda item: (
            [owner_row] if item is owner_item else [selected_row]
        ))
        owner_job = {
            "base": owner_row["atlas_base"],
            "texture_base": owner_row["texture_base"],
            "mode": "render_only",
            "out_dir": r"D:\Tree\owner\texture",
            "inputs": {},
        }

        with mock.patch.object(
                self.gui, "build_texture_job", return_value=owner_job
        ) as build_job, mock.patch.object(
                self.gui, "expected_job_size", return_value=(1, 1)
        ), mock.patch.object(
                self.gui, "complete_output_set", return_value=False
        ), mock.patch.object(
                self.gui, "job_needs_source_repair", return_value=False
        ):
            jobs, skipped = app._step3_jobs()

        self.assertEqual(skipped, [])
        self.assertEqual(len(jobs), 1)
        build_job.assert_called_once_with(owner_row)
        self.assertIs(jobs[0]["item"], owner_item)
        selected_key = self.gui.step3_texture_row_key(
            selected_item,
            selected_row["atlas_base"],
        )
        self.assertEqual(
            jobs[0]["step3_consumer_row_keys"],
            (selected_key,),
        )
        self.assertEqual(
            app._step3_render_required_row_keys,
            {selected_key},
        )

    def test_checked_shared_row_syncs_fresh_outputs_from_owner_folder(self):
        app = self.gui.App.__new__(self.gui.App)
        owner_item = {
            "name": "tree_owner",
            "folder": r"D:\Tree\owner",
        }
        selected_item = {
            "name": "tree_consumer",
            "folder": r"D:\Tree\consumer",
        }
        owner_row = {
            "folder": owner_item["folder"],
            "atlas_base": "M_leaf_shared",
            "texture_base": "T_leaf_shared",
            "source_signature": ["D:/Texture/leaf_color.tif"],
        }
        selected_row = {
            "folder": selected_item["folder"],
            "atlas_base": "M_leaf_shared",
            "texture_base": "T_leaf_shared",
            "source_signature": ["D:/Texture/leaf_color.tif"],
            "shared_from": owner_item["name"],
        }
        app.items = {
            owner_item["folder"]: {
                "checked": False,
                "item": owner_item,
            },
            selected_item["folder"]: {
                "checked": True,
                "item": selected_item,
            },
        }
        app.texplan_errors = {}
        app._checked_texplan_rows = mock.Mock(return_value=[
            (selected_item, selected_row),
        ])
        app._texplan_rows = mock.Mock(side_effect=lambda item: (
            [owner_row] if item is owner_item else [selected_row]
        ))
        owner_job = {
            "base": owner_row["atlas_base"],
            "texture_base": owner_row["texture_base"],
            "mode": "render_only",
            "out_dir": r"D:\Tree\owner\texture",
            "inputs": {},
        }

        def fake_paths(texture_dir, texture_base):
            return {
                role: Path(texture_dir) / f"{texture_base}_{role}.tga"
                for role in self.gui.sbs_auto.RENDER_MAPS
            }

        with mock.patch.object(
                self.gui, "build_texture_job", return_value=owner_job
        ), mock.patch.object(
                self.gui, "expected_job_size", return_value=(1, 1)
        ), mock.patch.object(
                self.gui, "complete_output_set", return_value=True
        ), mock.patch.object(
                self.gui, "job_needs_source_repair", return_value=False
        ), mock.patch.object(
                self.gui, "step3_existing_output_freshness",
                return_value={"fresh": True, "reason": "outputs_current"},
        ), mock.patch.object(
                self.gui, "canonical_texture_root",
                side_effect=lambda folder: Path(folder) / "texture",
        ), mock.patch.object(
                self.gui, "output_paths", side_effect=fake_paths
        ):
            jobs, skipped = app._step3_jobs()
            files = app._step3_sync_files()

        self.assertEqual(jobs, [])
        self.assertEqual(skipped, [])
        self.assertEqual(len(files), len(self.gui.sbs_auto.RENDER_MAPS))
        self.assertTrue(all(
            str(Path(owner_item["folder"]) / "texture") in path
            for path in files
        ))
        self.assertEqual(
            [row for row, _paths in app._pending_step3_manifest_rows],
            [owner_row],
        )

    def test_step3_sync_files_omits_excluded_plan_rows(self):
        app = self.gui.App.__new__(self.gui.App)
        good = {"folder": r"D:\Tree\good"}
        broken = {"folder": r"D:\Tree\broken"}
        good_row = {
            "folder": good["folder"],
            "atlas_base": "M_leaf_good",
            "texture_base": "T_leaf_good",
            "texture_dir": r"D:\Tree\good\texture",
        }
        broken_row = {
            "folder": broken["folder"],
            "atlas_base": "M_leaf_broken",
            "texture_base": "T_leaf_broken",
            "texture_dir": r"D:\Tree\broken\texture",
        }
        app._checked_texplan_rows = mock.Mock(return_value=[
            (good, good_row),
            (broken, broken_row),
        ])
        excluded = {
            self.gui.step3_texture_row_key(
                broken,
                broken_row["atlas_base"],
            )
        }

        def fake_paths(_folder, texture_base):
            return {
                role: Path(r"D:\Output") / f"{texture_base}_{role}.tga"
                for role in self.gui.sbs_auto.RENDER_MAPS
            }

        with mock.patch.object(
                self.gui, "canonical_texture_root",
                side_effect=lambda folder: Path(folder) / "texture"
        ), mock.patch.object(
                self.gui, "output_paths", side_effect=fake_paths
        ), mock.patch.object(
                self.gui, "complete_output_set", return_value=True
        ):
            files = app._step3_sync_files(excluded)

        self.assertTrue(files)
        self.assertTrue(
            all("T_leaf_good" in path for path in files)
        )
        self.assertEqual(
            [row["atlas_base"]
             for row, _files in app._pending_step3_manifest_rows],
            ["M_leaf_good"],
        )

    def test_folder_plan_error_excludes_every_real_row_in_folder(self):
        app = self.gui.App.__new__(self.gui.App)
        item = {
            "name": "broken_tree",
            "folder": r"D:\Tree\broken",
        }
        row = {
            "folder": item["folder"],
            "atlas_base": "M_leaf_real",
            "texture_base": "T_leaf_real",
            "texture_dir": r"D:\Tree\broken\texture",
        }
        app._checked_texplan_rows = mock.Mock(return_value=[(item, row)])
        row_key = self.gui.step3_texture_row_key(
            item,
            row["atlas_base"],
        )
        app._step3_existing_output_row_keys = {row_key}
        excluded = app._step3_exclusion_keys([
            (item, "텍스처 계획", "folder plan exploded"),
        ])

        with mock.patch.object(
                self.gui, "complete_output_set", return_value=True
        ):
            files = app._step3_sync_files(excluded)

        self.assertEqual(
            excluded,
            {(row_key[0], "")},
        )
        self.assertEqual(files, [])
        self.assertEqual(app._pending_step3_manifest_rows, [])
        self.assertEqual(app._step3_eligible_row_keys(excluded), set())

    def test_folder_plan_error_removes_its_prebuilt_render_job(self):
        item = {
            "name": "broken_tree",
            "folder": r"D:\Tree\broken",
        }
        row_key = self.gui.step3_texture_row_key(
            item,
            "M_leaf_real",
        )
        job = {
            "item": item,
            "base": "M_leaf_real",
            "step3_consumer_row_keys": (row_key,),
        }
        excluded = self.gui.App._step3_exclusion_keys([
            (item, "텍스처 계획", "folder plan exploded"),
        ])

        filtered = self.gui.App._step3_jobs_excluding(
            [job],
            excluded,
        )

        self.assertEqual(filtered, [])

    def test_step3_normalization_omits_excluded_row_in_same_spm(self):
        selected = r"D:\Trees\ladyfern\SK_weed_ladyfern_01.spm"
        folder = r"D:\Trees\ladyfern"
        app = self.gui.App.__new__(self.gui.App)
        app.cfg = {
            "tree_root": r"D:\Trees",
            "sbsrender_timeout": 10,
            "unreal_texture_sync_enabled": False,
        }
        app.status_var = mock.Mock()
        app.log = mock.Mock()
        app._ui = lambda callback: callback()
        app._step3_finished = mock.Mock()
        plan = {
            "items": [
                {
                    "folder": folder,
                    "atlas_base": "M_leaf_valid",
                },
                {
                    "folder": folder,
                    "atlas_base": "M_leaf_excluded",
                },
            ],
            "preserved_cluster_materials": [],
        }
        allowed = {
            self.gui.step3_texture_row_key(
                {"folder": folder},
                "M_leaf_valid",
            )
        }

        with mock.patch.object(
                self.gui, "make_report", return_value={}
        ), mock.patch.object(
                self.gui, "persist_cluster_assembly_receipts_safely"
        ), mock.patch.object(
                self.gui, "save_spm_analysis_cache"
        ), mock.patch.object(
                self.gui, "build_texture_plan_from_report", return_value=plan
        ), mock.patch.object(
                self.gui, "jobs_from_texture_plan", return_value=[]
        ) as build_jobs, mock.patch.object(
                self.gui, "normalize_spms_transactionally",
                return_value={
                    "spms": [],
                    "materials": 0,
                    "backup_dir": None,
                    "skipped": [],
                },
        ), mock.patch.object(
                self.gui, "cleanup_preserved_cluster_outputs",
                return_value={"cleaned": [], "conflicts": []},
        ):
            baseline = self.gui.seal_exact_mutation_baseline(
                [], action="unit_test_step3_cleanup"
            )
            app._run_step3(
                [],
                [selected],
                sync_files=[],
                allowed_step3_row_keys=allowed,
                exact_mutation_baseline=baseline,
            )

        exact_plan = build_jobs.call_args.args[0]
        self.assertEqual(
            [row["atlas_base"] for row in exact_plan["items"]],
            ["M_leaf_valid"],
        )
        self.assertEqual(
            build_jobs.call_args.kwargs["allowed_spms"],
            [selected],
        )

    def test_partial_step3_run_writes_run_specific_success_report(self):
        with tempfile.TemporaryDirectory() as temp:
            report_root = Path(temp)
            app = self.gui.App.__new__(self.gui.App)
            good_item = {
                "name": "good_tree",
                "folder": r"D:\Tree\good",
            }
            broken_item = {
                "name": "broken_tree",
                "folder": r"D:\Tree\broken",
            }
            good_row = {
                "folder": good_item["folder"],
                "atlas_base": "M_leaf_good",
                "texture_base": "T_leaf_good",
            }
            broken_row = {
                "folder": broken_item["folder"],
                "atlas_base": "M_leaf_broken",
                "texture_base": "T_leaf_broken",
            }
            good_key = self.gui.step3_texture_row_key(
                good_item,
                good_row["atlas_base"],
            )
            job = {
                "item": good_item,
                "base": good_row["atlas_base"],
                "texture_base": good_row["texture_base"],
                "mode": "render_only",
                "step3_consumer_row_keys": (good_key,),
            }
            skipped = [(
                broken_item,
                broken_row["atlas_base"],
                "missing albedo",
            )]
            app._checked_texplan_rows = mock.Mock(return_value=[
                (good_item, good_row),
                (broken_item, broken_row),
            ])
            app._pending_step3_manifest_rows = []
            app.cfg = {
                "sbsrender_timeout": 10,
                "unreal_texture_sync_enabled": False,
            }
            app.status_var = mock.Mock()
            app.tree = mock.Mock()
            app.log = mock.Mock()
            app._step3_finished = mock.Mock()
            app._ui = lambda callback: callback()
            render_result = {
                "texture_base": good_row["texture_base"],
                "files": [r"D:\Tree\good\texture\T_leaf_good_color.tga"],
                "canonical_manifest": (
                    r"D:\Tree\good\texture\pcg_canonical_outputs.json"
                ),
            }

            with mock.patch.object(
                    self.gui, "REPORT_DIR", report_root
            ), mock.patch.object(
                    self.gui, "run_texture_job", return_value=render_result
            ):
                report_path, payload = app._new_step3_run_report(
                    [job],
                    skipped,
                    affected_spms=[],
                    sync_files=[],
                )
                baseline = self.gui.seal_exact_mutation_baseline(
                    [], action="unit_test_step3_report"
                )
                app._run_step3(
                    [job],
                    affected_spms=[],
                    sync_files=[],
                    planned_skipped=1,
                    allowed_step3_row_keys={good_key},
                    step3_run_report_path=report_path,
                    step3_run_report=payload,
                    exact_mutation_baseline=baseline,
                )

            saved = json.loads(
                Path(report_path).read_text(encoding="utf-8")
            )

        self.assertEqual(saved["status"], "partial_success")
        self.assertEqual(saved["counts"]["excluded"], 1)
        self.assertEqual(saved["counts"]["render_succeeded"], 1)
        self.assertEqual(saved["counts"]["render_failed"], 0)
        self.assertEqual(saved["render"][0]["status"], "succeeded")
        self.assertEqual(
            saved["normalization"]["status"],
            "no_targets",
        )
        self.assertEqual(saved["unreal_sync"]["status"], "disabled")
        self.assertEqual(saved["final"]["planned_skipped"], 1)
        self.assertTrue(any(
            report_path in call.args[0]
            for call in app.log.call_args_list
        ))

    def test_invalid_planned_unreal_name_is_blocked_before_render(self):
        with tempfile.TemporaryDirectory() as temp:
            errors = self.gui.App._step3_unreal_name_errors(
                [{
                    "out_dir": temp,
                    "texture_base": "T_Material 2",
                }],
                [],
            )
        self.assertTrue(errors)
        self.assertEqual(errors[0][0], "T_Material 2_color")

    def test_full_recheck_button_starts_force_rpc_without_spm_normalize(self):
        class Root:
            @staticmethod
            def update_idletasks():
                return None

        class Value:
            def __init__(self):
                self.value = None

            def set(self, value):
                self.value = value

        item = {
            "name": "tree",
            "folder": r"D:\Tree\tree",
            "sk_spms": [r"D:\Tree\tree\SK_tree.spm"],
            "cluster_items": [{
                "missing_export_maps": [],
                "unreal_synced": True,
            }],
        }
        app = self.gui.App.__new__(self.gui.App)
        app.report = {"items": [item]}
        app.root = Root()
        app.status_var = Value()
        app.items = {
            item["folder"]: {"checked": True, "item": item},
        }
        app.log = mock.Mock()
        app._set_busy = mock.Mock()
        app._step3_jobs = mock.Mock(return_value=([], []))
        sync_files = [r"D:\Tree\tree\T_tree_color.tga"]
        app._step3_sync_files = mock.Mock(return_value=sync_files)
        app._step3_unreal_name_errors = mock.Mock(return_value=[])
        app._checked_texplan_rows = mock.Mock(return_value=[])
        app._begin_step3_run_report = mock.Mock(
            return_value=(None, None)
        )
        fake_worker = mock.Mock()

        with mock.patch.object(
                self.gui.messagebox, "askyesno", return_value=True
        ) as askyesno, mock.patch.object(
                self.gui.threading, "Thread", return_value=fake_worker
        ) as thread_class:
            plan = app._build_step3_execution_plan()
            app._step3_planning_done(plan)

        askyesno.assert_not_called()
        thread_kwargs = thread_class.call_args.kwargs
        self.assertEqual(thread_kwargs["args"], (
            [], [], sync_files, True,
        ))
        self.assertEqual(
            app.status_var.value,
            "③ Unreal 동기화 시작 · 대상 1장",
        )
        fake_worker.start.assert_called_once_with()

    def test_start_step3_dispatches_expensive_planning_to_worker(self):
        class Root:
            @staticmethod
            def update_idletasks():
                return None

        class Value:
            def __init__(self):
                self.value = None

            def set(self, value):
                self.value = value

        app = self.gui.App.__new__(self.gui.App)
        app.report = {"items": []}
        app.root = Root()
        app.status_var = Value()
        app._busy = False
        app.items = {}
        app._set_busy = mock.Mock()
        app._run_step3_planning = mock.Mock()
        app._step3_jobs = mock.Mock()
        fake_worker = mock.Mock()

        with mock.patch.object(
                self.gui.threading, "Thread", return_value=fake_worker
        ) as thread_class:
            app.start_step3()

        app._step3_jobs.assert_not_called()
        app._set_busy.assert_called_once_with(True)
        self.assertEqual(
            thread_class.call_args.kwargs["target"],
            app._run_step3_planning,
        )
        self.assertTrue(thread_class.call_args.kwargs["daemon"])
        fake_worker.start.assert_called_once_with()
        self.assertIn("공용 대기열 등록", app.status_var.value)

    def test_receipt_current_files_skip_hash_and_unreal_rpc(self):
        app = self.gui.App.__new__(self.gui.App)
        app.cfg = {"unreal_texture_destination": "/Game/Textures"}
        files = ["current.tga", "pending.tga"]
        remote_report = {
            "mode": "remote_editor",
            "counts": {"unchanged": 1},
            "entries": [],
            "errors": [],
        }
        with mock.patch.object(
                self.gui, "load_sync_state", return_value={"entries": {}}
        ), mock.patch.object(
                self.gui, "is_texture_synced", side_effect=[True, False]
        ), mock.patch.object(
                self.gui, "sync_texture_files", return_value=remote_report
        ) as sync:
            result = app._sync_pending_texture_files(files)

        sync.assert_called_once_with(
            ["pending.tga"], cfg=app.cfg, progress=None
        )
        self.assertEqual(result["receipt_current"], 1)
        self.assertEqual(result["pending_count"], 1)

    def test_full_recheck_sends_receipt_current_files_to_unreal_rpc(self):
        app = self.gui.App.__new__(self.gui.App)
        app.cfg = {"unreal_texture_destination": "/Game/Textures"}
        files = ["first_current.tga", "second_current.tga"]
        remote_report = {
            "mode": "remote_editor",
            "counts": {"unchanged": 2},
            "entries": [],
            "errors": [],
        }
        with mock.patch.object(
                self.gui, "load_sync_state", return_value={"entries": {}}
        ), mock.patch.object(
                self.gui, "is_texture_synced"
        ) as is_synced, mock.patch.object(
                self.gui, "sync_texture_files", return_value=remote_report
        ) as sync:
            result = app._sync_pending_texture_files(
                files, force_verify=True)

        is_synced.assert_not_called()
        sync.assert_called_once_with(
            files, cfg=app.cfg, progress=None
        )
        self.assertEqual(result["receipt_current"], 0)
        self.assertEqual(result["pending_count"], 2)
        self.assertEqual(result["forced_verify_count"], 2)

    def test_pure_sync_finish_reports_latest_changed_failed_and_path(self):
        class Value:
            def __init__(self):
                self.value = None

            def set(self, value):
                self.value = value

        app = self.gui.App.__new__(self.gui.App)
        app.status_var = Value()
        app._start_completion_refresh = mock.Mock()
        app.log = mock.Mock()
        report_path = r"C:\reports\sync.json"
        app._step3_finished(
            0, 0, {"latest": 42, "changed": 3, "failed": 1},
            report_path=report_path,
            planned_skipped=17,
        )
        messages = [call.args[0] for call in app.log.call_args_list]
        self.assertTrue(any(
            "최신 42장" in message and "변경 3장" in message
            and "실패 1장" in message for message in messages))
        final_status = app._start_completion_refresh.call_args.args[0]
        self.assertIn(report_path, final_status)
        self.assertIn("오류 항목 제외 17개", final_status)
        app._start_completion_refresh.assert_called_once_with(final_status)

    def test_completion_refresh_audits_in_worker_and_restores_final_status(self):
        class Root:
            def __init__(self):
                self.callbacks = []

            def after(self, delay, callback):
                self.callbacks.append((delay, callback))

        class Value:
            def __init__(self, value):
                self.value = value

            def get(self):
                return self.value

            def set(self, value):
                self.value = value

        threads = []

        class Thread:
            def __init__(self, target, daemon):
                self.target = target
                self.daemon = daemon
                self.started = False
                threads.append(self)

            def start(self):
                self.started = True

        report = {"items": []}
        sync_state = {"version": 1, "entries": {}}
        app = self.gui.App.__new__(self.gui.App)
        app.root = Root()
        app.cfg = {"tree_root": "old"}
        app.root_var = Value("new")
        app.use_pcg_targets_var = Value(True)
        app.status_var = Value("working")
        app.report = {"items": ["stale"]}
        app.sync_state = {"entries": {"stale": {}}}
        app.texplan_cache = {"stale": []}
        app.texplan_errors = {"stale": "error"}
        app._busy = True
        app._set_busy = mock.Mock()
        app.populate = mock.Mock()
        app._update_summary = mock.Mock()
        app.log = mock.Mock()

        with mock.patch.object(self.gui, "save_config") as save_config, \
                mock.patch.object(
                    self.gui, "load_pcg_targets", return_value={"meshes": []}), \
                mock.patch.object(
                    self.gui, "make_report", return_value=report) as make_report, \
                mock.patch.object(self.gui, "save_spm_analysis_cache"), \
                mock.patch.object(
                    self.gui, "load_sync_state", return_value=sync_state), \
                mock.patch.object(self.gui.threading, "Thread", Thread):
            final_status = "③ 완료: Unreal 최신 42장"
            app._start_completion_refresh(final_status)
            self.assertEqual(len(threads), 1)
            self.assertTrue(threads[0].started)
            make_report.assert_not_called()
            self.assertIn("표 재검사 중", app.status_var.value)

            threads[0].target()
            make_report.assert_called_once_with(
                {"tree_root": "new"}, pcg_targets={"meshes": []})
            save_config.assert_called_once_with({"tree_root": "new"})
            self.assertEqual(len(app.root.callbacks), 1)
            delay, callback = app.root.callbacks.pop()
            self.assertEqual(delay, 0)
            callback()

        self.assertIs(app.report, report)
        self.assertIs(app.sync_state, sync_state)
        self.assertEqual(app.texplan_cache, {})
        self.assertEqual(app.texplan_errors, {})
        app.populate.assert_called_once_with()
        app._update_summary.assert_called_once_with()
        app._set_busy.assert_called_once_with(False)
        self.assertEqual(app.status_var.value, final_status)


if __name__ == "__main__":
    unittest.main()
