import os
import gzip
import json
import queue
import tempfile
import threading
import unittest
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path
from unittest import mock


SK_BATCH_DIR = Path(__file__).resolve().parents[1]


def write_empty_spm(path):
    path.write_bytes(gzip.compress(b"<SpeedTreeModel><Assets /></SpeedTreeModel>"))


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
            write_empty_spm(spm)

            self.assertIn("생성 필요", app._blend_status_text(spm))

            blend.write_bytes(b"blend")
            self.set_time(blend, 1_000_000_000)
            self.set_time(spm, 2_000_000_000)
            self.assertIn("Blender 갱신 필요", app._blend_status_text(spm))

            report = root / "reports" / (
                "SK_tree_test_01_speedtree_repair_pipeline_report_codex.json"
            )
            report.parent.mkdir()
            report.write_text(
                json.dumps({
                    "texture_normalization": {"status": "ok", "missing": []},
                    "handoff_preflight": {"status": "ok"},
                }),
                encoding="utf-8",
            )
            self.set_time(blend, 3_000_000_000)
            self.assertEqual(app._blend_status_text(spm), "최신 ✓")

    def test_legacy_report_without_handoff_preflight_requires_repair(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            spm = root / "SK_tree_legacy_01.spm"
            blend = spm.with_suffix(".blend")
            write_empty_spm(spm)
            blend.write_bytes(b"blend")
            report = root / "reports" / (
                "SK_tree_legacy_01_speedtree_repair_pipeline_report_codex.json"
            )
            report.parent.mkdir()
            report.write_text(
                json.dumps({
                    "texture_normalization": {"status": "ok", "missing": []},
                }),
                encoding="utf-8",
            )
            self.set_time(spm, 1_000_000_000)
            self.set_time(blend, 2_000_000_000)
            self.set_time(report, 2_000_000_000)

            ready, reason = app._texture_normalization_ready(spm)

            self.assertFalse(ready)
            self.assertIn("사전검사 정보 없음", reason)
            self.assertIn("Repair 필요", app._blend_status_text(spm))

    def test_content_receipt_keeps_touch_only_spm_current(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            spm = root / "SK_tree_touch_only.spm"
            blend = spm.with_suffix(".blend")
            write_empty_spm(spm)
            blend.write_bytes(b"blend")
            report = root / "reports" / (
                "SK_tree_touch_only_speedtree_repair_pipeline_report_codex.json"
            )
            report.parent.mkdir()
            report.write_text(
                json.dumps({
                    "speedtree_pipeline_contract": {},
                    "texture_normalization": {"status": "ok", "missing": []},
                    "handoff_preflight": {"status": "ok"},
                }),
                encoding="utf-8",
            )
            self.set_time(blend, 1_000_000_000)
            self.set_time(report, 1_000_000_000)
            self.set_time(spm, 2_000_000_000)

            with mock.patch.object(gui, "validate_preflight_envelope"):
                status = app._blend_status_text(spm)

            self.assertEqual(status, "최신 ✓")

    def test_live_status_explains_unconnected_managed_atlas(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        contract = {
            "status": "replacement_needed",
            "managed_slot_count": 0,
            "source_slot_count": 39,
        }

        with mock.patch.object(
            gui, "inspect_spm_leaf_contract", return_value=contract
        ):
            status = app._blend_status_text(Path("SK_tree_lauraceae_10.spm"))

        self.assertIn("Atlas 연결 확인 필요", status)
        self.assertIn("기존 재질을 사용 중", status)
        self.assertIn("연결 후", status)

    def test_live_status_reports_missing_speedtree_export_materials(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            spm = root / "SK_tree_export_01.spm"
            blend = spm.with_suffix(".blend")
            write_empty_spm(spm)
            blend.write_bytes(b"blend")
            self.set_time(spm, 1_000_000_000)
            self.set_time(blend, 2_000_000_000)
            contract = {
                "status": "managed_connected",
                "expected_visible_material_names": ["M_leaf_atlas_01"],
            }
            exported = {
                "status": "missing_materials",
                "missing_materials": ["M_leaf_atlas_01"],
            }

            with mock.patch.object(
                gui, "inspect_spm_leaf_contract", return_value=contract
            ), mock.patch.object(
                gui, "inspect_speedtree_material_export", return_value=exported
            ):
                status = app._blend_status_text(spm)

            self.assertIn("Repair 필요", status)
            self.assertIn("재질이 SpeedTree FBX에서 빠짐", status)
            self.assertIn("M_leaf_atlas_01", status)

    def test_texture_preflight_rechecks_recorded_files_and_handoff_slots(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            spm = root / "SK_tree_texture_01.spm"
            write_empty_spm(spm)
            texture = root / "T_leaf_color.tga"
            texture.write_bytes(b"pixels")
            report = root / "reports" / (
                "SK_tree_texture_01_speedtree_repair_pipeline_report_codex.json"
            )
            report.parent.mkdir()
            data = {
                "texture_normalization": {
                    "status": "ok",
                    "missing": [],
                    "materials": [{
                        "material": "M_leaf",
                        "status": "ok",
                        "texture_base": "T_leaf",
                        "files": {"color": str(texture)},
                    }],
                },
                "handoff_preflight": {
                    "status": "blocked",
                    "empty_material_slots": [{"object": "SK_tree", "slot": 1}],
                    "missing_outputs": [],
                },
            }
            report.write_text(json.dumps(data), encoding="utf-8")
            self.set_time(spm, 1_000_000_000)
            self.set_time(report, 2_000_000_000)

            ready, reason = app._texture_normalization_ready(spm)
            self.assertFalse(ready)
            self.assertIn("빈 슬롯", reason)

            data["handoff_preflight"] = {"status": "ok"}
            report.write_text(json.dumps(data), encoding="utf-8")
            self.set_time(report, 3_000_000_000)
            texture.unlink()
            ready, reason = app._texture_normalization_ready(spm)
            self.assertFalse(ready)
            self.assertIn("텍스처 준비 안 됨", reason)

    def test_legacy_material_failure_is_not_waived_by_a_different_live_check(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            spm = root / "SK_tree_detached_frond.spm"
            write_empty_spm(spm)
            report = root / "reports" / (
                "SK_tree_detached_frond_speedtree_repair_pipeline_report_codex.json"
            )
            report.parent.mkdir()
            report.write_text(
                json.dumps({
                    "texture_normalization": {"status": "ok", "missing": []},
                    "handoff_preflight": {
                        "status": "blocked",
                        "empty_material_slots": [],
                        "missing_outputs": [],
                        "missing_materials": ["M_cluster_detached"],
                        "vertex_color_contract": {"status": "ok"},
                        "vertex_payload_contract": {"status": "ok"},
                    },
                }),
                encoding="utf-8",
            )
            self.set_time(spm, 1_000_000_000)
            self.set_time(report, 2_000_000_000)

            with mock.patch.object(
                gui.App, "_material_export_ready", return_value=(True, "정상")
            ) as live_check:
                ready, reason = app._texture_normalization_ready(spm)

            self.assertFalse(ready)
            self.assertIn("M_cluster_detached", reason)
            live_check.assert_not_called()

    def test_new_report_marker_requires_the_versioned_envelope(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            spm = root / "SK_tree_new_contract.spm"
            write_empty_spm(spm)
            report = root / "reports" / (
                "SK_tree_new_contract_speedtree_repair_pipeline_report_codex.json"
            )
            report.parent.mkdir()
            report.write_text(
                json.dumps({
                    "speedtree_pipeline_contract_required": True,
                    "texture_normalization": {"status": "ok", "missing": []},
                    "handoff_preflight": {"status": "ok"},
                }),
                encoding="utf-8",
            )
            self.set_time(spm, 1_000_000_000)
            self.set_time(report, 2_000_000_000)

            ready, reason = app._texture_normalization_ready(spm)

            self.assertFalse(ready)
            self.assertIn("공통 SpeedTree 계약 정보 없음", reason)

    def test_preserved_cluster_files_are_a_ready_texture_contract(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            spm = root / "SK_tree_cluster_01.spm"
            write_empty_spm(spm)
            cluster_file = root / "cluster" / "leaf_color.tga"
            cluster_file.parent.mkdir()
            cluster_file.write_bytes(b"pixels")
            report = root / "reports" / (
                "SK_tree_cluster_01_speedtree_repair_pipeline_report_codex.json"
            )
            report.parent.mkdir()
            report.write_text(
                json.dumps({
                    "texture_normalization": {
                        "status": "preserved_cluster",
                        "missing": [],
                        "materials": [{
                            "material": "M_leaf",
                            "status": "preserved_cluster",
                            "source_maps": {"color": str(cluster_file)},
                        }],
                    },
                    "handoff_preflight": {"status": "ok"},
                }),
                encoding="utf-8",
            )
            self.set_time(spm, 1_000_000_000)
            self.set_time(report, 2_000_000_000)

            self.assertEqual(
                app._texture_normalization_ready(spm),
                (True, "텍스처 준비 완료 · 보존 Cluster 1세트"),
            )

    def test_live_signature_tracks_reported_texture_deletion(self):
        gui = load_gui_module()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            spm = root / "SK_tree_live_texture_01.spm"
            write_empty_spm(spm)
            texture = root / "cluster" / "leaf_color.tga"
            texture.parent.mkdir()
            texture.write_bytes(b"pixels")
            report = root / "reports" / (
                "SK_tree_live_texture_01_speedtree_repair_pipeline_report_codex.json"
            )
            report.parent.mkdir()
            report.write_text(
                json.dumps({
                    "texture_normalization": {
                        "materials": [{
                            "status": "preserved_cluster",
                            "source_maps": {"color": str(texture)},
                        }],
                    },
                }),
                encoding="utf-8",
            )

            paths = gui.App._reported_texture_paths(spm)
            before = gui.App._live_status_signature(spm, paths)
            texture.unlink()
            after = gui.App._live_status_signature(spm, paths)

            self.assertEqual(paths, (str(texture),))
            self.assertNotEqual(before, after)

    def test_scan_replaces_saved_complete_label_until_live_check_finishes(self):
        gui = load_gui_module()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            spm = root / "SK_tree_test_02.spm"
            blend = spm.with_suffix(".blend")
            write_empty_spm(spm)
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
            self.assertEqual(status, "검증 중…")
            self.assertNotIn("완료", status)
            self.assertIn("Blender 갱신 필요", app._blend_status_text(spm))

    def test_scan_migrates_current_legacy_calibration_signature(self):
        gui = load_gui_module()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            spm = root / "SK_tree_cache_migration.spm"
            write_empty_spm(spm)

            app = self.make_app(gui)
            app.root_var = FakeVar(str(root))
            app.cfg = {"root": str(root)}
            app._collect_cfg = lambda: dict(app.cfg)
            app.spm_calibration_signature = "content-signature"
            app.legacy_spm_calibration_signature = "legacy-stat-signature"
            app.tree = FakeTree()
            app.items = {}
            app.checked_rows = FakeCheckedRows()
            app.log = mock.Mock()
            app.state[str(spm)] = {
                "calibration_cache": {
                    "version": gui.CALIBRATION_CACHE_VERSION,
                    "spm_fingerprint": "spm-content",
                    "settings_signature": "legacy-stat-signature",
                    "status": "calibrated",
                }
            }
            prepared = {
                "spms": [spm],
                "snapshots": {
                    str(spm): {
                        "fingerprint": "spm-content",
                        "size": spm.stat().st_size,
                        "mtime_ns": spm.stat().st_mtime_ns,
                    }
                },
                "errors": [],
                "snapshot_cache_hits": 1,
            }

            with mock.patch.object(
                app, "_collect_scan_result", return_value=prepared
            ), mock.patch.object(gui, "save_config"), mock.patch.object(
                gui, "save_state"
            ), mock.patch.object(
                gui,
                "calibration_settings_signature",
                return_value="content-signature",
            ), mock.patch.object(
                gui,
                "legacy_calibration_settings_signature",
                return_value="legacy-stat-signature",
            ):
                app.scan()

            self.assertEqual(
                app.state[str(spm)]["calibration_cache"]["settings_signature"],
                "content-signature",
            )

    def test_scan_migrates_cache_from_owned_marker_restore(self):
        gui = load_gui_module()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            spm = root / "SK_tree_marker_restore.spm"
            original_backup = root / "original" / spm.name
            marked_backup = root / "marked" / spm.name
            original_backup.parent.mkdir()
            marked_backup.parent.mkdir()
            spm.write_bytes(b"original-spm")
            original_backup.write_bytes(spm.read_bytes())
            marked_backup.write_bytes(b"marked-spm")
            report_dir = root / "reports"
            report_dir.mkdir()
            (report_dir / f"{spm.stem}_material_problem_node_markers.json").write_text(
                json.dumps({
                    "status": "restored",
                    "spm": str(spm),
                    "restore_source": str(original_backup),
                    "restore_preserved_original_timestamp": True,
                    "backups": [{
                        "path": str(marked_backup),
                        "reason": "before_exact_marker_cleanup_restore",
                    }],
                }),
                encoding="utf-8",
            )
            current_snapshot = gui.file_content_snapshot(spm)
            marked_snapshot = gui.file_content_snapshot(marked_backup)
            cache = {
                "version": gui.CALIBRATION_CACHE_VERSION,
                "spm_fingerprint": marked_snapshot["fingerprint"],
                "settings_signature": "legacy-stat-signature",
                "status": "already-ok",
            }

            migrated = gui.App._migrate_restored_marker_calibration_cache(
                spm, cache, current_snapshot
            )

            self.assertTrue(migrated)
            self.assertEqual(
                cache["spm_fingerprint"], current_snapshot["fingerprint"]
            )
            self.assertIn("marker_restore_cache_migrated_at", cache)

    def test_blender_job_runs_material_preflight_before_starting_blender(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            spm = root / "SK_tree_preflight_order.spm"
            write_empty_spm(spm)
            app.force_rerun = True
            app.cfg = {
                "speedtree_exe": r"C:\Program Files\SpeedTree\SpeedTree.exe",
                "fbx_ini": str(
                    root / "speedtree_bone_weight_repair"
                    / "presets" / "speedtree_10_1" / "Options_MA_Fbx.ini"
                ),
                "blender_exe": r"C:\Program Files\Blender\blender.exe",
                "blender_parallel_jobs": 1,
                "blender_job_timeout": 3600,
                "speedtree_material_preflight_timeout": 900,
            }
            speedtree_cli = Path(app.cfg["fbx_ini"]).resolve().parents[2] / "speedtree_cli.py"
            speedtree_cli.parent.mkdir(parents=True, exist_ok=True)
            speedtree_cli.write_text("# test", encoding="utf-8")
            app.log = mock.Mock()
            commands = []

            def fake_run(cmd, log_name, _timeout, **_kwargs):
                commands.append(list(cmd))
                report = Path(cmd[cmd.index("--report") + 1])
                report.parent.mkdir(parents=True, exist_ok=True)
                payload = {"status": "ok", "warnings": []}
                report.write_text(json.dumps(payload), encoding="utf-8")
                return 0, root / log_name

            app._run_limited = fake_run
            app._leaf_reference_ready = mock.Mock(return_value=(True, "정상"))
            app._handoff_ready = mock.Mock(
                side_effect=[(False, "갱신 필요"), (True, "준비됨")]
            )
            app._blend_status_text = mock.Mock(return_value="최신 ✓")
            item = {"manual_bones_locked": False, "wind_override": "auto"}

            with mock.patch("spm_audit.audit_spm", return_value={}), mock.patch(
                "spm_audit.sk_readiness", return_value={"ready": True}
            ), mock.patch.object(gui, "save_state"):
                app._job_blender(str(spm), spm, item)

            self.assertEqual(len(commands), 2)
            self.assertIn("speedtree_material_preflight.py", commands[0][1])
            self.assertTrue(any(
                str(value).endswith("bwr_headless_job.py")
                for value in commands[1]
            ))
            self.assertIn("--material-contract", commands[1])
            contract_path = commands[1][commands[1].index("--material-contract") + 1]
            first_report = commands[0][commands[0].index("--report") + 1]
            self.assertEqual(contract_path, first_report)

    def test_failed_blender_job_restores_previous_pipeline_report(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            spm = root / "SK_tree_preserve_report.spm"
            write_empty_spm(spm)
            pipeline_report = root / "reports" / (
                "SK_tree_preserve_report_speedtree_repair_pipeline_report_codex.json"
            )
            pipeline_report.parent.mkdir(parents=True)
            previous = b'{"handoff_preflight":{"status":"ok"}}'
            pipeline_report.write_bytes(previous)
            app.force_rerun = False
            app.cfg = {
                "speedtree_exe": "SpeedTree.exe",
                "fbx_ini": str(
                    root / "speedtree_bone_weight_repair"
                    / "presets" / "speedtree_10_1" / "Options_MA_Fbx.ini"
                ),
                "blender_exe": "blender.exe",
                "blender_parallel_jobs": 1,
                "blender_job_timeout": 3600,
                "speedtree_material_preflight_timeout": 900,
            }
            speedtree_cli = (
                Path(app.cfg["fbx_ini"]).resolve().parents[2]
                / "speedtree_cli.py"
            )
            speedtree_cli.parent.mkdir(parents=True, exist_ok=True)
            speedtree_cli.write_text("# test", encoding="utf-8")
            app.log = mock.Mock()

            def fake_run(cmd, log_name, _timeout, **_kwargs):
                report = Path(cmd[cmd.index("--report") + 1])
                report.parent.mkdir(parents=True, exist_ok=True)
                if any(
                    str(value).endswith("speedtree_material_preflight.py")
                    for value in cmd
                ):
                    report.write_text('{"status":"ok"}', encoding="utf-8")
                    return 0, root / log_name
                pipeline_report.write_text(
                    '{"status":"done"}', encoding="utf-8"
                )
                report.write_text(
                    '{"status":"failed","error":"Blender crashed"}',
                    encoding="utf-8",
                )
                return 1, root / log_name

            app._run_limited = fake_run
            app._leaf_reference_ready = mock.Mock(return_value=(True, "정상"))
            app._handoff_ready = mock.Mock(return_value=(False, "갱신 필요"))
            item = {"manual_bones_locked": False, "wind_override": "auto"}

            with mock.patch("spm_audit.audit_spm", return_value={}), mock.patch(
                "spm_audit.sk_readiness", return_value={"ready": True}
            ):
                with self.assertRaises(gui.BatchItemError):
                    app._job_blender(str(spm), spm, item)

            self.assertEqual(pipeline_report.read_bytes(), previous)
            self.assertTrue(any(
                "최신성 보고서 보존" in call.args[0]
                for call in app.log.call_args_list
            ))


if __name__ == "__main__":
    unittest.main()
