import os
import gzip
import hashlib
import json
import queue
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path
from unittest import mock

from speedtree_pipeline_contract import build_preflight_envelope, source_identity


SK_BATCH_DIR = Path(__file__).resolve().parents[1]


def write_empty_spm(path):
    path.write_bytes(gzip.compress(b"<SpeedTreeModel><Assets /></SpeedTreeModel>"))


def write_valid_wind(path):
    bones = [{"BoneName": "Root", "BoneIndex": 0, "ParentIndex": -1}]
    digest = hashlib.sha1(b"0\0Root\0-1\n").hexdigest()
    path.write_text(
        json.dumps(
            {
                "SkeletonContract": {
                    "SchemaVersion": 2,
                    "BoneCount": 1,
                    "BoneNameIndexParentSha1": digest,
                    "Bones": bones,
                    "ImportRoot": bones[0],
                }
            }
        ),
        encoding="utf-8",
    )


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


class DynamicWindSkeletonContractTests(unittest.TestCase):
    def test_missing_skeleton_contract_requires_blender_regeneration(self):
        gui = load_gui_module()
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "wind.json"
            path.write_text(
                json.dumps({"Joints": [], "SimulationGroups": []}),
                encoding="utf-8",
            )
            ready, reason = gui.dynamic_wind_skeleton_contract_ready(path)

        self.assertFalse(ready)
        self.assertIn("SkeletonContract", reason)

    def test_complete_skeleton_contract_is_content_validated(self):
        gui = load_gui_module()
        bones = [
            {"BoneName": "Root", "BoneIndex": 0, "ParentIndex": -1},
            {"BoneName": "Bone_1", "BoneIndex": 1, "ParentIndex": 0},
        ]
        digest = hashlib.sha1()
        for row in bones:
            digest.update(
                (
                    f"{row['BoneIndex']}\0{row['BoneName']}\0"
                    f"{row['ParentIndex']}\n"
                ).encode("utf-8")
            )
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "wind.json"
            path.write_text(
                json.dumps(
                    {
                        "SkeletonContract": {
                            "SchemaVersion": 2,
                            "BoneCount": 2,
                            "BoneNameIndexParentSha1": digest.hexdigest(),
                            "Bones": bones,
                            "ImportRoot": bones[0],
                        }
                    }
                ),
                encoding="utf-8",
            )
            ready, reason = gui.dynamic_wind_skeleton_contract_ready(path)

        self.assertTrue(ready, reason)


class SpmAuditResultContractTests(unittest.TestCase):
    def test_child_report_fingerprint_must_match_live_spm(self):
        gui = load_gui_module()
        with self.assertRaisesRegex(RuntimeError, "지문"):
            gui.validate_spm_audit_result(
                Path("SK_tree_test.spm"),
                {"final_spm_fingerprint": "reported"},
                {"fingerprint": "actual"},
            )

    def test_cluster_cache_requires_live_logical_postcondition(self):
        gui = load_gui_module()
        spm = Path("Tree") / "cluster" / "SK_branch_test_01.spm"
        report = {
            "final_spm_fingerprint": "same",
            "cluster_root_logical_postcondition": {"ok": True},
        }
        with mock.patch.object(
            gui,
            "current_cluster_root_postcondition",
            return_value={"ok": False, "mode": "not_fixed_point"},
        ), self.assertRaisesRegex(RuntimeError, "root-bone"):
            gui.validate_spm_audit_result(
                spm,
                report,
                {"fingerprint": "same"},
            )


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

    def test_spm_process_failure_kind_reaches_batch_item_error(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            spm = root / "SK_tree_process_failure.spm"
            write_empty_spm(spm)
            iid = str(spm)
            app.state[iid] = {}
            app.force_rerun = True
            app.cfg = {
                "spm_parallel_jobs": 1,
                "spm_verify_timeout": 120,
                "spm_job_timeout": 7200,
            }
            app.spm_calibration_signature = "settings"
            app.legacy_spm_calibration_signature = None
            app.log = mock.Mock()
            app._prepare_pair_for_job = mock.Mock(return_value=spm)
            app._batch_job_item = mock.Mock(
                return_value={"manual_bones_locked": False}
            )
            app._current_spm_snapshot = mock.Mock(
                return_value={"fingerprint": "before"}
            )

            def fake_run(command, log_name, *_args, **_kwargs):
                report_path = Path(
                    command[command.index("--report") + 1]
                )
                report_path.parent.mkdir(parents=True, exist_ok=True)
                report_path.write_text(
                    json.dumps([{
                        "spm": str(spm),
                        "status": "failed",
                        "failure_kind": "internal_error",
                        "error": "SpeedTree export stalled",
                        "diagnostic": {
                            "category": "speedtree_export_timeout",
                        },
                    }]),
                    encoding="utf-8",
                )
                return 1, root / log_name

            app._run_limited = fake_run
            with mock.patch.object(
                gui,
                "LOG_DIR",
                root / "logs",
            ), mock.patch.object(
                gui,
                "should_calibrate_spm",
                return_value=True,
            ), self.assertRaises(gui.BatchItemError) as caught:
                app._job_spm(iid, spm)

        self.assertEqual(caught.exception.kind, "internal_error")
        self.assertEqual(
            caught.exception.report["diagnostic"]["category"],
            "speedtree_export_timeout",
        )

    def test_malformed_spm_report_is_internal_error(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            spm = root / "SK_tree_bad_report.spm"
            write_empty_spm(spm)
            iid = str(spm)
            app.state[iid] = {}
            app.force_rerun = True
            app.cfg = {
                "spm_parallel_jobs": 1,
                "spm_verify_timeout": 120,
                "spm_job_timeout": 7200,
            }
            app.spm_calibration_signature = "settings"
            app.legacy_spm_calibration_signature = None
            app.log = mock.Mock()
            app._prepare_pair_for_job = mock.Mock(return_value=spm)
            app._batch_job_item = mock.Mock(
                return_value={"manual_bones_locked": False}
            )
            app._current_spm_snapshot = mock.Mock(
                return_value={"fingerprint": "before"}
            )

            def fake_run(command, log_name, *_args, **_kwargs):
                report_path = Path(
                    command[command.index("--report") + 1]
                )
                report_path.parent.mkdir(parents=True, exist_ok=True)
                report_path.write_text("{", encoding="utf-8")
                return 1, root / log_name

            app._run_limited = fake_run
            with mock.patch.object(
                gui,
                "LOG_DIR",
                root / "logs",
            ), mock.patch.object(
                gui,
                "should_calibrate_spm",
                return_value=True,
            ), self.assertRaises(gui.BatchItemError) as caught:
                app._job_spm(iid, spm)

        self.assertEqual(caught.exception.kind, "internal_error")
        self.assertIn("보고서 손상", str(caught.exception))

    def test_spm_worker_outer_timeout_is_internal_error(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            spm = root / "SK_tree_worker_timeout.spm"
            write_empty_spm(spm)
            iid = str(spm)
            app.state[iid] = {}
            app.force_rerun = True
            app.cfg = {
                "spm_parallel_jobs": 1,
                "spm_verify_timeout": 120,
                "spm_job_timeout": 7200,
            }
            app.spm_calibration_signature = "settings"
            app.legacy_spm_calibration_signature = None
            app.log = mock.Mock()
            app._prepare_pair_for_job = mock.Mock(return_value=spm)
            app._batch_job_item = mock.Mock(
                return_value={"manual_bones_locked": False}
            )
            app._current_spm_snapshot = mock.Mock(
                return_value={"fingerprint": "before"}
            )
            app._run_limited = mock.Mock(
                side_effect=RuntimeError("worker watchdog timeout")
            )

            with mock.patch.object(
                gui,
                "LOG_DIR",
                root / "logs",
            ), mock.patch.object(
                gui,
                "should_calibrate_spm",
                return_value=True,
            ), self.assertRaises(gui.BatchItemError) as caught:
                app._job_spm(iid, spm)

        self.assertEqual(caught.exception.kind, "internal_error")
        self.assertIn("watchdog timeout", str(caught.exception))

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
                    "speedtree_pipeline_contract": {},
                    "texture_normalization": {"status": "ok", "missing": []},
                    "handoff_preflight": {"status": "ok"},
                }),
                encoding="utf-8",
            )
            wind = root / "JSON" / (
                "SK_tree_test_01_dynamic_wind_import_"
                "from_megaplant_groups.json"
            )
            wind.parent.mkdir()
            write_valid_wind(wind)
            self.set_time(blend, 3_000_000_000)
            with mock.patch.object(gui, "validate_preflight_envelope"):
                self.assertEqual(app._blend_status_text(spm), "최신 ✓")

    def test_legacy_report_without_pipeline_contract_requires_repair(self):
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
            self.assertIn("공통 SpeedTree 계약 정보 없음", reason)
            self.assertIn("Repair 필요", app._blend_status_text(spm))

    def test_legacy_report_with_exact_source_identity_migrates_without_repair(
        self,
    ):
        gui = load_gui_module()
        app = self.make_app(gui)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            spm = root / "SK_tree_legacy_current_01.spm"
            blend = spm.with_suffix(".blend")
            write_empty_spm(spm)
            blend.write_bytes(b"blend")
            report = gui.repair_pipeline_report_path(spm)
            report.parent.mkdir()
            report.write_text(
                json.dumps({
                    "speedtree_live_source_identity": {
                        "spm": source_identity(spm),
                    },
                    "texture_normalization": {
                        "status": "ok",
                        "missing": [],
                        "materials": [],
                    },
                    "handoff_preflight": {"status": "ok"},
                }),
                encoding="utf-8",
            )
            self.set_time(blend, 1_000_000_000)
            self.set_time(report, 2_000_000_000)

            self.assertTrue(app._repair_contract_current(spm))
            migrated = json.loads(report.read_text(encoding="utf-8"))
            self.assertEqual(
                migrated["report_contract_migration"]["kind"],
                "legacy_content_identity_upgrade",
            )
            gui.validate_preflight_envelope(
                migrated["speedtree_pipeline_contract"],
                spm,
                require_ok=True,
            )

    def test_legacy_report_identity_mismatch_never_migrates(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            spm = root / "SK_tree_legacy_stale_01.spm"
            blend = spm.with_suffix(".blend")
            write_empty_spm(spm)
            blend.write_bytes(b"blend")
            stale_identity = source_identity(spm)
            spm.write_bytes(gzip.compress(
                b"<SpeedTreeModel><Assets><Changed /></Assets></SpeedTreeModel>"
            ))
            report = gui.repair_pipeline_report_path(spm)
            report.parent.mkdir()
            original = {
                "speedtree_live_source_identity": {"spm": stale_identity},
                "texture_normalization": {"status": "ok", "missing": []},
                "handoff_preflight": {"status": "ok"},
            }
            report.write_text(json.dumps(original), encoding="utf-8")
            self.set_time(blend, 1_000_000_000)
            self.set_time(report, 2_000_000_000)

            self.assertFalse(app._repair_contract_current(spm))
            self.assertNotIn(
                "speedtree_pipeline_contract",
                json.loads(report.read_text(encoding="utf-8")),
            )

    def test_isolated_material_handoff_loads_without_rewriting_report(self):
        gui = load_gui_module()
        with tempfile.TemporaryDirectory() as temporary:
            cluster = Path(temporary) / "Tree_test" / "Cluster"
            spm = cluster / "SK_branch_test_01.spm"
            isolated = (
                cluster / ".sk_batch_isolated_bark" / "scope"
                / "Tree_test" / "Cluster" / spm.name
            )
            spm.parent.mkdir(parents=True)
            isolated.parent.mkdir(parents=True)
            write_empty_spm(spm)
            write_empty_spm(isolated)
            production_identity = source_identity(spm)
            isolated_identity = source_identity(isolated)
            handoff = {
                "source": {
                    "spm": isolated_identity,
                    "stmat": [],
                },
            }
            report = gui.repair_pipeline_report_path(spm)
            report.parent.mkdir()
            payload = {
                "speedtree_pipeline_contract": {"legacy": True},
                "speedtree_material_handoff_contract": handoff,
                "cluster_bark_source_resolution": {
                    "status": "ready",
                    "source_spm": {
                        "path": production_identity["canonical_path"],
                        "size": production_identity["size"],
                        "sha256": production_identity["sha256"],
                    },
                    "speedtree_spm": {
                        "path": isolated_identity["canonical_path"],
                        "size": isolated_identity["size"],
                        "sha256": isolated_identity["sha256"],
                    },
                },
            }
            report.write_text(json.dumps(payload), encoding="utf-8")
            before = report.read_bytes()

            with mock.patch.object(
                gui,
                "validate_preflight_envelope",
            ) as validate:
                loaded = gui.load_current_repair_pipeline_report(spm)

            self.assertEqual(loaded, payload)
            self.assertEqual(report.read_bytes(), before)
            validate.assert_called_once_with(
                handoff,
                isolated.resolve(),
                require_ok=True,
            )

    def test_isolated_bark_report_without_exact_handoff_never_migrates(self):
        gui = load_gui_module()
        with tempfile.TemporaryDirectory() as temporary:
            cluster = Path(temporary) / "Tree_test" / "Cluster"
            spm = cluster / "SK_branch_test_01.spm"
            isolated = cluster / ".isolated" / spm.name
            spm.parent.mkdir(parents=True)
            isolated.parent.mkdir(parents=True)
            write_empty_spm(spm)
            write_empty_spm(isolated)
            production_identity = source_identity(spm)
            isolated_identity = source_identity(isolated)
            report = gui.repair_pipeline_report_path(spm)
            report.parent.mkdir()
            payload = {
                "speedtree_pipeline_contract": {"outcome": "ok"},
                "cluster_bark_source_resolution": {
                    "status": "ready",
                    "source_spm": {
                        "path": production_identity["canonical_path"],
                    },
                    "speedtree_spm": {
                        "path": isolated_identity["canonical_path"],
                    },
                },
            }
            report.write_text(json.dumps(payload), encoding="utf-8")
            before = report.read_bytes()

            with self.assertRaisesRegex(
                ValueError,
                "no exact material handoff contract",
            ):
                gui.load_current_repair_pipeline_report(spm)

            self.assertEqual(report.read_bytes(), before)

    def test_legacy_material_report_with_unresolved_bindings_never_migrates(
        self,
    ):
        gui = load_gui_module()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            spm = root / "SK_tree_legacy_material_01.spm"
            blend = spm.with_suffix(".blend")
            write_empty_spm(spm)
            blend.write_bytes(b"blend")
            report = gui.repair_pipeline_report_path(spm)
            report.parent.mkdir()
            payload = {
                "speedtree_live_source_identity": {
                    "spm": source_identity(spm),
                },
                "texture_normalization": {"status": "ok"},
                "handoff_preflight": {"status": "ok"},
            }
            report.write_text(json.dumps(payload), encoding="utf-8")
            self.set_time(blend, 1_000_000_000)
            self.set_time(report, 2_000_000_000)
            before = report.read_bytes()

            with mock.patch.object(
                gui,
                "build_preflight_envelope",
                return_value={
                    "material_intents": [{
                        "material_name": "M_Bark",
                        "texture_source_mode": "unresolved",
                    }],
                },
            ), self.assertRaisesRegex(
                ValueError,
                "cannot prove material texture bindings",
            ):
                gui.load_current_repair_pipeline_report(spm)

            self.assertEqual(report.read_bytes(), before)

    def test_cluster_contract_uses_canonical_sk_output_and_stmat(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        with tempfile.TemporaryDirectory() as temporary:
            cluster = Path(temporary) / "Tree_elm" / "Cluster"
            cluster.mkdir(parents=True)
            legacy = cluster / "branch_elm_01.spm"
            canonical = cluster / "SK_branch_elm_01.spm"
            write_empty_spm(legacy)
            canonical.write_bytes(legacy.read_bytes())
            stmat = cluster / "fbx" / "SK_branch_elm_01.stmat"
            stmat.parent.mkdir()
            stmat.write_text("<SpeedTreeMaterials />", encoding="utf-8")
            report = cluster / "reports" / (
                "SK_branch_elm_01_speedtree_repair_pipeline_report_codex.json"
            )
            report.parent.mkdir()
            report.write_text(
                json.dumps({
                    "speedtree_pipeline_contract": build_preflight_envelope(
                        canonical,
                        outcome="ok",
                        texture_readiness={"status": "not_applicable"},
                    ),
                    "texture_normalization": {
                        "status": "ok",
                        "missing": [],
                        "materials": [],
                    },
                    "handoff_preflight": {"status": "ok"},
                }),
                encoding="utf-8",
            )

            self.assertEqual(gui.speedtree_output_spm_for(canonical), canonical)
            self.assertEqual(
                app._texture_normalization_ready(canonical),
                (True, "텍스처 정규화 완료"),
            )

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
            wind = root / "JSON" / (
                "SK_tree_touch_only_dynamic_wind_import_"
                "from_megaplant_groups.json"
            )
            wind.parent.mkdir()
            write_valid_wind(wind)
            self.set_time(blend, 1_000_000_000)
            self.set_time(report, 1_000_000_000)
            self.set_time(spm, 2_000_000_000)

            with mock.patch.object(gui, "validate_preflight_envelope"):
                status = app._blend_status_text(spm)

            self.assertEqual(status, "최신 ✓")

    def test_source_review_is_current_blend_but_remains_push_blocked(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        with tempfile.TemporaryDirectory() as temporary:
            cluster = Path(temporary) / "Tree_elm" / "Cluster"
            cluster.mkdir(parents=True)
            spm = cluster / "branch_elm_01.spm"
            write_empty_spm(spm)
            blend = gui.blend_path_for(spm)
            blend.write_bytes(b"blend")
            report = cluster / "reports" / (
                "branch_elm_01_speedtree_repair_pipeline_report_codex.json"
            )
            report.parent.mkdir()
            report.write_text(
                json.dumps({
                    "speedtree_pipeline_contract": {},
                    "texture_normalization": {
                        "status": "preserved_cluster",
                        "missing": [],
                        "materials": [],
                    },
                    "handoff_preflight": {
                        "status": "source_review",
                        "unreal_push_ready": False,
                        "empty_material_slots": [
                            {"object": "branch_elm_01", "slot": 0}
                        ],
                    },
                }),
                encoding="utf-8",
            )
            self.set_time(spm, 1_000_000_000)
            self.set_time(blend, 2_000_000_000)
            self.set_time(report, 2_000_000_000)
            app._leaf_reference_ready = mock.Mock(return_value=(True, "정상"))

            with mock.patch.object(gui, "validate_preflight_envelope"):
                self.assertTrue(app._repair_contract_current(spm))
                self.assertEqual(
                    app._blend_status_text(spm),
                    "Blend 완료 · 원본 검토 필요 · Unreal Push 차단",
                )
                ready, _reason = app._texture_normalization_ready(spm)

            self.assertFalse(ready)

    def test_nonforced_blender_queue_skips_current_source_review(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        with tempfile.TemporaryDirectory() as temporary:
            spm = Path(temporary) / "SK_tree_review_01.spm"
            write_empty_spm(spm)
            app.force_rerun = False
            app.log = mock.Mock()
            app._leaf_reference_ready = mock.Mock(return_value=(True, "ok"))
            app._repair_output_state = mock.Mock(return_value={
                "current": True,
                "push_ready": False,
                "kind": "source_review",
                "reason": "review",
            })
            app._record_live_blend_status = mock.Mock()
            app._run_limited = mock.Mock(
                side_effect=AssertionError("current review must not rerun Repair")
            )

            app._job_blender(
                str(spm),
                spm,
                {"manual_bones_locked": False, "wind_override": "auto"},
            )

            app._run_limited.assert_not_called()
            app._record_live_blend_status.assert_called_once()

    def test_live_pass_through_invalidates_saved_ready_assembly_skip(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        with tempfile.TemporaryDirectory() as temporary:
            owner = Path(temporary) / "Tree_relation_off"
            owner.mkdir()
            spm = owner / "SK_Tree_relation_off_01.spm"
            write_empty_spm(spm)
            report = owner / "reports" / (
                "SK_Tree_relation_off_01_"
                "speedtree_repair_pipeline_report_codex.json"
            )
            report.parent.mkdir()
            report.write_text(
                json.dumps({
                    "cluster_assembly_manifest": {
                        "status": "ready",
                    },
                }),
                encoding="utf-8",
            )
            live_report = Path(temporary) / "live_audit.json"
            live_report.write_text(
                json.dumps({
                    "items": [{
                        "cluster_assembly": {
                            "tree_source_identities": [{
                                "target_spm": {"path": str(spm)},
                            }],
                            "handoff": {
                                "status": "pass_through",
                                "cluster_dependencies": [],
                                "roles": [],
                            },
                        },
                    }],
                }),
                encoding="utf-8",
            )
            app.force_rerun = False
            app.log = mock.Mock()
            app._leaf_reference_ready = mock.Mock(
                return_value=(True, "ok")
            )
            app._refresh_stale_cluster_receipt = mock.Mock(return_value={
                "policy": "live_audit_authoritative",
                "live_audit_report": str(live_report),
            })
            app._repair_output_state = mock.Mock(return_value={
                "current": True,
                "push_ready": True,
                "kind": "ready",
                "reason": "ready",
            })

            with mock.patch.object(
                gui,
                "blender_open_file_window_titles",
                side_effect=RuntimeError("rerun reached"),
            ), self.assertRaisesRegex(RuntimeError, "rerun reached"):
                app._job_blender(
                    str(spm),
                    spm,
                    {
                        "manual_bones_locked": False,
                        "wind_override": "auto",
                    },
                )

    def test_repair_code_newer_than_saved_outputs_does_not_force_rerun(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            spm = root / "SK_tree_runtime_stale.spm"
            blend = spm.with_suffix(".blend")
            report = root / "reports" / (
                "SK_tree_runtime_stale_speedtree_repair_pipeline_report_codex.json"
            )
            addon_dir = root / "speedtree_bone_weight_repair"
            fbx_ini = (
                addon_dir / "presets" / "speedtree_10_1" / "Options_MA_Fbx.ini"
            )
            core = addon_dir / "core.py"
            report.parent.mkdir()
            fbx_ini.parent.mkdir(parents=True)
            write_empty_spm(spm)
            blend.write_bytes(b"blend")
            report.write_text("{}", encoding="utf-8")
            fbx_ini.write_text("", encoding="utf-8")
            core.write_text("# newer repair runtime", encoding="utf-8")
            self.set_time(spm, 1_000_000_000)
            self.set_time(blend, 2_000_000_000)
            self.set_time(report, 2_000_000_000)
            self.set_time(core, 3_000_000_000)
            app.cfg = {"fbx_ini": str(fbx_ini)}
            app._leaf_reference_ready = mock.Mock(return_value=(True, "정상"))

            ready, reason = app._repair_runtime_fresh(spm)

            self.assertTrue(ready, reason)
            self.assertEqual(reason, "")

    def _runtime_stale_fixture(self, root):
        """A completed Repair result whose .blend predates the installed code."""
        spm = root / "SK_tree_runtime_stale.spm"
        blend = spm.with_suffix(".blend")
        report = root / "reports" / (
            "SK_tree_runtime_stale_speedtree_repair_pipeline_report_codex.json"
        )
        addon_dir = root / "speedtree_bone_weight_repair"
        fbx_ini = addon_dir / "presets" / "speedtree_10_1" / "Options_MA_Fbx.ini"
        core = addon_dir / "core.py"
        report.parent.mkdir(exist_ok=True)
        fbx_ini.parent.mkdir(parents=True)
        write_empty_spm(spm)
        blend.write_bytes(b"blend")
        report.write_text("{}", encoding="utf-8")
        fbx_ini.write_text("", encoding="utf-8")
        core.write_text("# newer repair runtime", encoding="utf-8")
        self.set_time(spm, 1_000_000_000)
        self.set_time(blend, 2_000_000_000)
        self.set_time(report, 2_000_000_000)
        self.set_time(core, 3_000_000_000)
        return spm, core, fbx_ini

    def test_missing_runtime_receipt_does_not_force_a_rerun(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            spm, _core, fbx_ini = self._runtime_stale_fixture(root)
            app.cfg = {"fbx_ini": str(fbx_ini)}
            app._leaf_reference_ready = mock.Mock(return_value=(True, "정상"))
            app.log = mock.Mock()

            self.assertTrue(app._repair_runtime_fresh(spm)[0])

            app._write_repair_runtime_receipt(spm)

            self.assertTrue(app._repair_runtime_receipt_path(spm).is_file())
            fresh, reason = app._repair_runtime_fresh(spm)
            self.assertTrue(fresh, reason)
            self.assertEqual(reason, "")

    def test_runtime_receipt_stays_current_when_addon_code_changes(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            spm, core, fbx_ini = self._runtime_stale_fixture(root)
            app.cfg = {"fbx_ini": str(fbx_ini)}
            app.log = mock.Mock()
            app._write_repair_runtime_receipt(spm)
            self.assertTrue(app._repair_runtime_fresh(spm)[0])

            core.write_text("# edited again", encoding="utf-8")

            fresh, reason = app._repair_runtime_fresh(spm)
            self.assertTrue(fresh, reason)
            self.assertEqual(reason, "")

    def test_runtime_receipt_does_not_gate_on_non_addon_producer_hash(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            spm, core, fbx_ini = self._runtime_stale_fixture(root)
            producer = root / "cluster_assembly_builder.py"
            producer.write_text("# producer v1", encoding="utf-8")
            app.cfg = {"fbx_ini": str(fbx_ini)}
            app.log = mock.Mock()
            app._repair_runtime_code_paths = mock.Mock(
                return_value=[core, producer]
            )
            app._write_repair_runtime_receipt(spm)
            self.assertTrue(app._repair_runtime_fresh(spm)[0])

            producer.write_text("# producer v2", encoding="utf-8")

            fresh, reason = app._repair_runtime_fresh(spm)
            self.assertTrue(fresh, reason)
            self.assertEqual(reason, "")

    def test_runtime_receipt_gates_on_explicit_output_contract_change(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            spm, _core, fbx_ini = self._runtime_stale_fixture(root)
            app.cfg = {"fbx_ini": str(fbx_ini)}
            app.log = mock.Mock()
            app._write_repair_runtime_receipt(spm)
            receipt_path = app._repair_runtime_receipt_path(spm)
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            receipt["output_contract_version"] = (
                gui.REPAIR_OUTPUT_CONTRACT_VERSION + 1
            )
            receipt_path.write_text(json.dumps(receipt), encoding="utf-8")

            fresh, reason = app._repair_runtime_fresh(spm)

            self.assertFalse(fresh)
            self.assertIn("Blender Repair 산출물 계약이 변경됨", reason)

    def test_version_one_runtime_receipt_migrates_without_rerun(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            spm, _core, fbx_ini = self._runtime_stale_fixture(root)
            app.cfg = {"fbx_ini": str(fbx_ini)}
            receipt_path = app._repair_runtime_receipt_path(spm)
            receipt_path.write_text(
                json.dumps({
                    "kind": "sk_repair_runtime",
                    "version": 1,
                    "code": {"addon/core.py": "legacy-diagnostic-hash"},
                }),
                encoding="utf-8",
            )
            app.log = mock.Mock()
            app._repair_contract_current = mock.Mock(return_value=True)
            app._repair_runtime_code_state = mock.Mock(
                side_effect=AssertionError(
                    "legacy receipt migration must not rehash producer code"
                )
            )

            fresh, reason = app._repair_runtime_fresh(spm)

            self.assertTrue(fresh, reason)
            self.assertEqual(reason, "")
            migrated = json.loads(receipt_path.read_text(encoding="utf-8"))
            self.assertEqual(
                migrated["version"],
                gui.REPAIR_RUNTIME_RECEIPT_VERSION,
            )
            self.assertEqual(
                migrated["output_contract_version"],
                gui.REPAIR_OUTPUT_CONTRACT_VERSION,
            )
            self.assertEqual(
                migrated["code"],
                {"addon/core.py": "legacy-diagnostic-hash"},
            )
            app._repair_runtime_code_state.assert_not_called()

    def test_invalid_runtime_receipt_is_rewritten_only_for_current_artifacts(
        self,
    ):
        gui = load_gui_module()
        app = self.make_app(gui)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            spm, _core, fbx_ini = self._runtime_stale_fixture(root)
            app.cfg = {"fbx_ini": str(fbx_ini)}
            app.log = mock.Mock()
            receipt_path = app._repair_runtime_receipt_path(spm)
            receipt_path.write_text("{broken", encoding="utf-8")
            app._repair_contract_current = mock.Mock(return_value=False)

            fresh, reason = app._repair_runtime_fresh(spm)

            self.assertTrue(fresh, reason)
            self.assertEqual(receipt_path.read_text(encoding="utf-8"), "{broken")

            app._repair_contract_current.return_value = True
            fresh, reason = app._repair_runtime_fresh(spm)

            self.assertTrue(fresh, reason)
            migrated = json.loads(receipt_path.read_text(encoding="utf-8"))
            self.assertEqual(
                migrated["output_contract_version"],
                gui.REPAIR_OUTPUT_CONTRACT_VERSION,
            )

    def test_runtime_receipt_tracks_its_shared_contract_module(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        with tempfile.TemporaryDirectory() as temporary:
            addon_dir = Path(temporary) / "addon"
            addon_dir.mkdir()

            paths = {
                Path(path).resolve()
                for path in app._repair_runtime_code_paths(addon_dir)
            }

        self.assertIn(
            (SK_BATCH_DIR / "repair_runtime_contract.py").resolve(),
            paths,
        )

    def test_cluster_assembly_input_change_invalidates_root_repair(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        from cluster_assembly_builder import (
            ClusterAssemblyBuildError,
            MANIFEST_KIND,
            file_fingerprint,
        )

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            spm = root / "SK_Tree_elm_01.spm"
            report = root / "reports" / (
                "SK_Tree_elm_01_speedtree_repair_pipeline_report_codex.json"
            )
            manifest_path = root / "assembly" / "manifest.json"
            report.parent.mkdir()
            manifest_path.parent.mkdir()
            write_empty_spm(spm)
            manifest_path.write_text(
                json.dumps({"kind": MANIFEST_KIND}),
                encoding="utf-8",
            )
            report.write_text(
                json.dumps({
                    "cluster_assembly_manifest": {
                        "manifest": file_fingerprint(manifest_path),
                    },
                }),
                encoding="utf-8",
            )

            with mock.patch(
                "cluster_assembly_builder.validate_manifest_artifacts",
                side_effect=ClusterAssemblyBuildError(
                    "source blend branch_normalized_01 changed"
                ),
            ):
                ready, reason = app._cluster_assembly_inputs_current(spm)

        self.assertFalse(ready)
        self.assertIn("Cluster Assembly input changed", reason)
        self.assertIn("branch_normalized_01", reason)

    def test_handoff_readiness_checks_cluster_assembly_inputs(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        with tempfile.TemporaryDirectory() as temporary:
            spm = Path(temporary) / "SK_Tree_elm_01.spm"
            blend = spm.with_suffix(".blend")
            write_empty_spm(spm)
            blend.write_bytes(b"blend")
            app._leaf_reference_ready = mock.Mock(return_value=(True, "ok"))
            app._repair_runtime_fresh = mock.Mock(return_value=(True, ""))
            app._repair_contract_current = mock.Mock(return_value=True)
            app._cluster_assembly_inputs_current = mock.Mock(
                return_value=(False, "Cluster Assembly input changed")
            )

            ready, reason = app._handoff_ready(spm)

        self.assertFalse(ready)
        self.assertEqual(reason, "Cluster Assembly input changed")

    def test_current_cluster_receipt_requires_embedded_assembly_manifest(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            spm = root / "SK_tree_clustered_01.spm"
            report = root / "reports" / (
                "SK_tree_clustered_01_"
                "speedtree_repair_pipeline_report_codex.json"
            )
            report.parent.mkdir()
            write_empty_spm(spm)
            report.write_text("{}", encoding="utf-8")

            with mock.patch.object(
                gui,
                "cluster_assembly_receipt_resolution",
                return_value={
                    "selected_receipt": str(root / "current_receipt.json")
                },
            ):
                ready, reason = app._cluster_assembly_inputs_current(spm)

        self.assertFalse(ready)
        self.assertIn("Assembly manifest", reason)

    def test_actionable_current_receipt_invalidates_old_pass_through_report(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        from cluster_assembly_builder import file_fingerprint

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            spm = root / "SK_tree_clustered_01.spm"
            report = root / "reports" / (
                "SK_tree_clustered_01_"
                "speedtree_repair_pipeline_report_codex.json"
            )
            old_receipt = root / "old_receipt.json"
            current_receipt = root / "current_receipt.json"
            report.parent.mkdir()
            write_empty_spm(spm)
            old_receipt.write_text("old", encoding="utf-8")
            current_receipt.write_text("current", encoding="utf-8")
            report.write_text(
                json.dumps({
                    "cluster_assembly_manifest": {
                        "status": "pass_through",
                    },
                    "cluster_assembly_handoff": {
                        "pcg_receipt": file_fingerprint(old_receipt),
                    },
                }),
                encoding="utf-8",
            )
            current_payload = {
                "cluster_assembly": {
                    "handoff": {
                        "status": "pending_export",
                        "roles": [{
                            "role": "cluster",
                            "name": "cluster_densiflora_01",
                        }],
                        "cluster_dependencies": [{
                            "role": "cluster",
                            "spm": "SK_cluster_densiflora_01.spm",
                        }],
                        "separate_nanite_assembly_requested": True,
                    },
                },
            }

            with mock.patch.object(
                gui,
                "cluster_assembly_receipt_resolution",
                return_value={
                    "selected_receipt": str(current_receipt),
                },
            ), mock.patch.object(
                gui,
                "load_cluster_assembly_receipt",
                return_value=current_payload,
            ) as load_receipt:
                ready, reason = app._cluster_assembly_inputs_current(spm)

        self.assertFalse(ready)
        self.assertIn("receipt is actionable", reason)
        load_receipt.assert_called_once_with(
            current_receipt,
            requested_spm=spm,
        )

    def test_run_specific_live_pass_through_ignores_old_persisted_relation(self):
        gui = load_gui_module()
        app = self.make_app(gui)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            spm = root / "SK_tree_lauraceae_10.spm"
            report = root / "reports" / (
                "SK_tree_lauraceae_10_"
                "speedtree_repair_pipeline_report_codex.json"
            )
            live_report = root / "live_material_contract.json"
            report.parent.mkdir()
            write_empty_spm(spm)
            live_report.write_text(
                json.dumps({
                    "cluster_assembly": {
                        "tree_source_identities": [{
                            "target_spm": {"path": str(spm)},
                            "authoritative_tree_source": {
                                "path": str(spm),
                            },
                        }],
                        "dependencies": [],
                        "handoff": {
                            "status": "pass_through",
                            "roles": [],
                            "cluster_dependencies": [],
                            "separate_nanite_assembly_requested": False,
                        },
                    },
                }),
                encoding="utf-8",
            )
            report.write_text(
                json.dumps({
                    "cluster_assembly_manifest": {
                        "status": "pass_through",
                    },
                    "cluster_assembly_receipt_resolution": {
                        "policy": "embedded_live_audit_authoritative",
                        "selected_receipt": str(live_report),
                    },
                }),
                encoding="utf-8",
            )

            with mock.patch.object(
                gui,
                "cluster_assembly_receipt_resolution",
                side_effect=AssertionError(
                    "old persisted receipt must not override the live run"
                ),
            ), mock.patch.object(
                gui,
                "load_cluster_assembly_receipt",
                side_effect=AssertionError(
                    "generic persisted receipt loader must not be used"
                ),
            ):
                ready, reason = app._cluster_assembly_inputs_current(spm)

        self.assertTrue(ready, reason)
        self.assertEqual(reason, "")

    def test_current_receipt_accepts_rendered_authority_prepared_only_report(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        from cluster_assembly_builder import file_fingerprint

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            spm = root / "SK_tree_clustered_01.spm"
            report = root / "reports" / (
                "SK_tree_clustered_01_"
                "speedtree_repair_pipeline_report_codex.json"
            )
            current_receipt = root / "current_receipt.json"
            report.parent.mkdir()
            write_empty_spm(spm)
            current_receipt.write_text("current", encoding="utf-8")
            report.write_text(
                json.dumps({
                    "cluster_assembly_manifest": {
                        "status": "pass_through",
                        "content_decision": "pass_through",
                        "reason": (
                            "normalized_roles_are_prepared_but_unused_by_"
                            "rendered_mesh"
                        ),
                        "rendered_role_count": 0,
                        "handoff_evidence": {
                            "pcg_receipt": file_fingerprint(current_receipt),
                        },
                    },
                }),
                encoding="utf-8",
            )
            current_payload = {
                "cluster_assembly": {
                    "handoff": {
                        "status": "pending_export",
                        "roles": [{"role": "cluster"}],
                        "cluster_dependencies": [{"role": "cluster"}],
                        "separate_nanite_assembly_requested": True,
                    },
                },
            }

            with mock.patch.object(
                gui,
                "cluster_assembly_receipt_resolution",
                return_value={
                    "selected_receipt": str(current_receipt),
                },
            ), mock.patch.object(
                gui,
                "load_cluster_assembly_receipt",
                return_value=current_payload,
            ):
                ready, reason = app._cluster_assembly_inputs_current(spm)

        self.assertTrue(ready)
        self.assertEqual(reason, "")

    def test_matching_prepared_unused_role_does_not_recur_on_new_live_report(self):
        gui = load_gui_module()

        def artifact(path, sha256):
            return {
                "path": path,
                "exists": True,
                "size": 10,
                "mtime_ns": 123,
                "sha256": sha256,
            }

        normalized = {
            "status": "ready",
            "contract": "atlas_normalized_plan_skeletal_pair_v1",
            "material": "M_cluster_densiflora_01",
            "material_id": 5,
            "manifest": artifact(
                r"D:\tree\.atlas\scope.json",
                "a" * 64,
            ),
            "source_blend": artifact(
                r"D:\tree\cluster\source.blend",
                "b" * 64,
            ),
            "variants": [{
                "ordinal": 1,
                "plan_name": "cluster_01",
                "skeletal_asset_name": "SK_cluster_01",
                "source_prototype_index": 1,
                "source_partition_mode": "PER_CONNECTED_DEFORM_CLUSTER",
                "target_mesh_id": 7,
                "physical_capture_contract_sha256": "c" * 64,
                "plan_fbx": artifact(
                    r"D:\tree\meshes\cluster_01.fbx",
                    "d" * 64,
                ),
            }],
        }
        saved_manifest = {
            "status": "pass_through",
            "content_decision": "pass_through",
            "reason": (
                "normalized_roles_are_prepared_but_unused_by_rendered_mesh"
            ),
            "rendered_role_count": 0,
            "prepared_unused_roles": {
                "cluster": {
                    "role": "cluster",
                    "normalized_variants": normalized,
                },
            },
        }
        live_contract = {
            "handoff": {
                "status": "ready",
                "roles": [{
                    "role": "cluster",
                    "normalized_variants": json.loads(
                        json.dumps(normalized)
                    ),
                }],
            },
        }

        self.assertTrue(
            gui.rendered_unused_pass_through_matches_live(
                saved_manifest,
                live_contract,
            )
        )
        live_contract["handoff"]["roles"][0]["normalized_variants"][
            "variants"
        ][0]["plan_fbx"]["sha256"] = "e" * 64
        self.assertFalse(
            gui.rendered_unused_pass_through_matches_live(
                saved_manifest,
                live_contract,
            )
        )

    def test_current_receipt_must_match_ready_report_receipt_evidence(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        from cluster_assembly_builder import MANIFEST_KIND, file_fingerprint

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            spm = root / "SK_tree_clustered_01.spm"
            report = root / "reports" / (
                "SK_tree_clustered_01_"
                "speedtree_repair_pipeline_report_codex.json"
            )
            manifest_path = root / "assembly" / "manifest.json"
            old_receipt = root / "old_receipt.json"
            current_receipt = root / "current_receipt.json"
            report.parent.mkdir()
            manifest_path.parent.mkdir()
            write_empty_spm(spm)
            old_receipt.write_text("old", encoding="utf-8")
            current_receipt.write_text("current", encoding="utf-8")
            old_fingerprint = file_fingerprint(old_receipt)
            manifest_path.write_text(
                json.dumps({
                    "kind": MANIFEST_KIND,
                    "status": "ready",
                    "handoff_evidence": {
                        "pcg_receipt": old_fingerprint,
                    },
                }),
                encoding="utf-8",
            )
            report.write_text(
                json.dumps({
                    "cluster_assembly_manifest": {
                        "status": "ready",
                        "manifest": file_fingerprint(manifest_path),
                    },
                    "cluster_assembly_handoff": {
                        "pcg_receipt": old_fingerprint,
                    },
                }),
                encoding="utf-8",
            )

            with mock.patch.object(
                gui,
                "cluster_assembly_receipt_resolution",
                return_value={
                    "selected_receipt": str(current_receipt),
                },
            ), mock.patch.object(
                gui,
                "load_cluster_assembly_receipt",
                return_value={
                    "cluster_assembly": {
                        "handoff": {
                            "status": "pending_export",
                            "roles": [{"role": "cluster"}],
                        },
                    },
                },
            ), mock.patch(
                "cluster_assembly_builder.validate_manifest_artifacts",
            ) as validate_artifacts:
                ready, reason = app._cluster_assembly_inputs_current(spm)

        self.assertFalse(ready)
        self.assertIn("receipt differs", reason)
        validate_artifacts.assert_not_called()

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
                "speedtree_pipeline_contract": {},
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

            with mock.patch.object(gui, "validate_preflight_envelope"):
                ready, reason = app._texture_normalization_ready(spm)
            self.assertFalse(ready)
            self.assertIn("빈 슬롯", reason)

            data["handoff_preflight"] = {"status": "ok"}
            report.write_text(json.dumps(data), encoding="utf-8")
            self.set_time(report, 3_000_000_000)
            texture.unlink()
            with mock.patch.object(gui, "validate_preflight_envelope"):
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
                    "speedtree_pipeline_contract": {},
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
            ) as live_check, mock.patch.object(
                gui, "validate_preflight_envelope"
            ):
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
                    "speedtree_pipeline_contract": {},
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

            with mock.patch.object(gui, "validate_preflight_envelope"):
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

    def test_scan_uses_non_running_placeholder_until_live_check_finishes(self):
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

            displayed = app.tree.rows[str(spm)]["values"][3]
            self.assertEqual(displayed, "상태 확인 대기…")
            self.assertNotIn("검증 중", displayed)
            # A scan placeholder is never persisted as an active job state.
            self.assertEqual(
                app.state[str(spm)]["blend_status"],
                "완료 (wind TREE)",
            )
            self.assertIn("Blender 갱신 필요", app._blend_status_text(spm))

    def test_scan_reuses_saved_live_status_with_matching_signature(self):
        gui = load_gui_module()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            spm = root / "SK_tree_cached_live_01.spm"
            blend = spm.with_suffix(".blend")
            write_empty_spm(spm)
            blend.write_bytes(b"blend")
            signature = gui.App._live_status_signature(spm, ())

            app = self.make_app(gui)
            app.root_var = FakeVar(str(root))
            app.cfg = {"root": str(root)}
            app._collect_cfg = lambda: dict(app.cfg)
            app.spm_calibration_signature = "test"
            app.tree = FakeTree()
            app.items = {}
            app.checked_rows = FakeCheckedRows()
            app.log = mock.Mock()
            app.state[str(spm)] = {
                "blend_status": "최신 ✓",
                # JSON reload converts tuples to lists.
                "live_status_signature": json.loads(json.dumps(signature)),
                "live_texture_paths": [],
            }

            with mock.patch.object(
                gui, "scan_sk_spms", return_value=[spm]
            ), mock.patch.object(gui, "save_config"), mock.patch.object(
                gui, "save_state"
            ):
                app.scan()

            displayed = app.tree.rows[str(spm)]["values"][3]
            self.assertEqual(displayed, "최신 ✓")
            self.assertEqual(
                app.items[str(spm)]["live_status_signature"],
                signature,
            )

    def test_scan_discards_signature_that_belongs_to_transient_status(self):
        gui = load_gui_module()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            spm = root / "SK_tree_transient_live_01.spm"
            blend = spm.with_suffix(".blend")
            write_empty_spm(spm)
            blend.write_bytes(b"blend")
            signature = gui.App._live_status_signature(spm, ())

            app = self.make_app(gui)
            app.root_var = FakeVar(str(root))
            app.cfg = {"root": str(root)}
            app._collect_cfg = lambda: dict(app.cfg)
            app.spm_calibration_signature = "test"
            app.tree = FakeTree()
            app.items = {}
            app.checked_rows = FakeCheckedRows()
            app.log = mock.Mock()
            app.state[str(spm)] = {
                "blend_status": "Blender Repair 중...",
                "live_status_signature": json.loads(json.dumps(signature)),
                "live_texture_paths": [],
            }

            with mock.patch.object(
                gui, "scan_sk_spms", return_value=[spm]
            ), mock.patch.object(gui, "save_config"), mock.patch.object(
                gui, "save_state"
            ):
                app.scan()

            self.assertEqual(
                app.tree.rows[str(spm)]["values"][3],
                "상태 확인 대기…",
            )
            self.assertIsNone(
                app.items[str(spm)]["live_status_signature"]
            )
            self.assertNotIn(
                "live_status_signature", app.state[str(spm)]
            )
            self.assertNotIn("blend_status", app.state[str(spm)])

    def test_live_status_worker_isolates_row_failure_and_retries_it(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        good = Path("SK_tree_good_01.spm")
        bad = Path("SK_tree_bad_01.spm")
        app._scan_generation = 7
        app._live_poll_active = True
        app.items = {
            str(good): {"spm": good},
            str(bad): {"spm": bad},
        }
        app.state = {
            str(good): {"live_status_error": "old failure"},
            str(bad): {},
        }
        app.log = mock.Mock()
        app._current_push_status_text = mock.Mock(return_value="준비됨 ✓")

        def blend_status(path):
            if Path(path) == bad:
                raise RuntimeError("damaged report")
            return "최신 ✓"

        app._blend_status_text = blend_status
        snapshot = [
            (str(good), good, (), None),
            (str(bad), bad, (), None),
        ]
        with mock.patch.object(
            gui.App,
            "_live_status_signature",
            return_value=("changed",),
        ), mock.patch.object(
            gui.App, "_reported_texture_paths", return_value=()
        ), mock.patch.object(gui, "save_state"), mock.patch.object(
            gui, "save_leaf_contract_cache"
        ):
            app._poll_live_file_status_worker(7, snapshot)

        self.assertEqual(
            app.state[str(good)]["blend_status"], "최신 ✓"
        )
        self.assertNotIn(
            "live_status_error", app.state[str(good)]
        )
        self.assertIn(
            "damaged report", app.state[str(bad)]["blend_status"]
        )
        self.assertIsNone(
            app.state[str(bad)]["live_status_signature"]
        )
        events = []
        while not app.ui_queue.empty():
            events.append(app.ui_queue.get_nowait())
        self.assertIn(
            ("live_status_done", (7, False, 1)),
            events,
        )
        self.assertFalse(app._live_poll_active)

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
            app._handoff_ready = mock.Mock(return_value=(True, "준비됨"))
            app._blend_status_text = mock.Mock(return_value="최신 ✓")
            item = {"manual_bones_locked": False, "wind_override": "auto"}
            app.state[str(spm)] = {
                "push_status": "건너뜀: Blender 갱신 필요",
                "push_status_kind": "preflight_skip",
                "push_status_error": {"kind": "preflight_skip"},
            }

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
            self.assertEqual(app.state[str(spm)]["push_status"], "준비됨 ✓")
            self.assertEqual(app.state[str(spm)]["push_status_kind"], "ready")
            self.assertNotIn("push_status_error", app.state[str(spm)])

    def test_stale_cluster_receipt_is_refreshed_and_revalidated(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        app.log = mock.Mock()
        app.cfg = {"cluster_receipt_refresh_timeout": 321}
        with tempfile.TemporaryDirectory() as temporary, mock.patch.object(
            gui, "LOG_DIR", Path(temporary) / "logs"
        ):
            spm = Path(temporary) / "Tree_elm" / "SK_Tree_elm_01.spm"
            spm.parent.mkdir()
            spm.write_bytes(b"spm")
            (spm.parent / "Cluster").mkdir()
            selected = Path(temporary) / "receipt.json"
            current = {"selected_receipt": str(selected)}

            def run_audit(command, *_args, **_kwargs):
                report = Path(command[command.index("--json") + 1])
                report.parent.mkdir(parents=True, exist_ok=True)
                report.write_text(
                    json.dumps({"items": [{"cluster_assembly": {
                        "tree_source_identities": [{
                            "target_spm": {"path": str(spm)},
                        }],
                        "dependencies": [{"role": "branch"}],
                        "handoff": {"errors": [], "issues": []},
                    }}]}),
                    encoding="utf-8",
                )
                return 0, Path(temporary) / "refresh.log"

            app._run_limited = mock.Mock(side_effect=run_audit)

            with mock.patch.object(
                gui,
                "cluster_assembly_receipt_resolution",
                side_effect=[
                    gui.ClusterAssemblyReceiptStaleError("stale"),
                    current,
                ],
            ):
                resolution = app._refresh_stale_cluster_receipt(
                    spm, "20260725_120000"
                )

            self.assertEqual(
                resolution["policy"],
                "live_audit_authoritative",
            )
            self.assertEqual(
                resolution["persisted_receipt"],
                current["selected_receipt"],
            )
            command = app._run_limited.call_args.args[0]
            self.assertTrue(
                str(command[1]).endswith("pcg_texture_audit.py")
            )
            self.assertEqual(
                command[command.index("--target") + 1], str(spm.parent)
            )
            self.assertEqual(
                command[command.index("--target-mesh") + 1],
                "Tree_elm_01",
            )
            self.assertEqual(app._run_limited.call_args.args[2], 321)
            self.assertTrue(any(
                "live audit 완료" in call.args[0]
                for call in app.log.call_args_list
            ))

    def test_cluster_live_audit_memo_reuses_only_current_batch_success(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        app.log = mock.Mock()
        app.cfg = {"cluster_receipt_refresh_timeout": 321}
        with tempfile.TemporaryDirectory() as temporary, mock.patch.object(
            gui, "LOG_DIR", Path(temporary) / "logs"
        ):
            spm = Path(temporary) / "Tree_elm" / "SK_Tree_elm_01.spm"
            spm.parent.mkdir()
            spm.write_bytes(b"spm")
            (spm.parent / "Cluster").mkdir()

            def run_audit(command, *_args, **_kwargs):
                report = Path(command[command.index("--json") + 1])
                report.parent.mkdir(parents=True, exist_ok=True)
                report.write_text(
                    json.dumps({"items": [{"cluster_assembly": {
                        "tree_source_identities": [{
                            "target_spm": {
                                "path": str(spm),
                                "exists": True,
                            },
                        }],
                        "dependencies": [{"role": "branch"}],
                        "handoff": {"errors": [], "issues": []},
                    }}]}),
                    encoding="utf-8",
                )
                return 0, Path(temporary) / "refresh.log"

            app._run_limited = mock.Mock(side_effect=run_audit)
            current = {
                "selected_receipt": str(
                    Path(temporary) / "receipt.json"
                )
            }
            with mock.patch.object(
                gui,
                "cluster_assembly_receipt_resolution",
                return_value=current,
            ):
                first = app._refresh_stale_cluster_receipt(
                    spm, "20260729_010101"
                )
                second = app._refresh_stale_cluster_receipt(
                    spm, "20260729_010102"
                )
                app._reset_cluster_receipt_refresh_memo()
                third = app._refresh_stale_cluster_receipt(
                    spm, "20260729_010103"
                )

        self.assertEqual(first["policy"], "live_audit_authoritative")
        self.assertEqual(second, first)
        self.assertEqual(third["policy"], "live_audit_authoritative")
        self.assertEqual(app._run_limited.call_count, 2)
        self.assertTrue(any(
            "memo hit" in call.args[0]
            for call in app.log.call_args_list
        ))

    def test_cluster_live_audit_memo_invalidates_spm_and_manifest_changes(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        app.log = mock.Mock()
        app.cfg = {"cluster_receipt_refresh_timeout": 321}
        with tempfile.TemporaryDirectory() as temporary, mock.patch.object(
            gui, "LOG_DIR", Path(temporary) / "logs"
        ):
            spm = Path(temporary) / "Tree_elm" / "SK_Tree_elm_01.spm"
            spm.parent.mkdir()
            spm.write_bytes(b"spm-v1")
            cluster = spm.parent / "Cluster"
            cluster.mkdir()
            manifest = cluster / "SK_cluster_elm_01.atlas_leaf_targets.json"
            manifest.write_text('{"version": 1}', encoding="utf-8")
            manifest_mtime_ns = manifest.stat().st_mtime_ns
            capture = cluster / "cluster_elm_01.tga"
            capture.write_bytes(b"capture-v1")

            def run_audit(command, *_args, **_kwargs):
                report = Path(command[command.index("--json") + 1])
                report.parent.mkdir(parents=True, exist_ok=True)
                report.write_text(
                    json.dumps({"items": [{"cluster_assembly": {
                        "tree_source_identities": [{
                            "target_spm": {
                                "path": str(spm),
                                "exists": True,
                            },
                        }],
                        "dependencies": [{
                            "role": "branch",
                            "texture_dependencies": [{
                                "path": str(capture),
                                "exists": True,
                                "size": capture.stat().st_size,
                                "mtime_ns": capture.stat().st_mtime_ns,
                            }],
                        }],
                        "handoff": {"errors": [], "issues": []},
                    }}]}),
                    encoding="utf-8",
                )
                return 0, Path(temporary) / "refresh.log"

            app._run_limited = mock.Mock(side_effect=run_audit)
            with mock.patch.object(
                gui,
                "cluster_assembly_receipt_resolution",
                return_value={
                    "selected_receipt": str(
                        Path(temporary) / "receipt.json"
                    )
                },
            ):
                app._refresh_stale_cluster_receipt(
                    spm, "20260729_020101"
                )
                app._refresh_stale_cluster_receipt(
                    spm, "20260729_020102"
                )
                capture.write_bytes(b"capture-version-two")
                app._refresh_stale_cluster_receipt(
                    spm, "20260729_020103"
                )
                manifest.write_text('{"version": 2}', encoding="utf-8")
                os.utime(
                    manifest,
                    ns=(manifest_mtime_ns, manifest_mtime_ns),
                )
                app._refresh_stale_cluster_receipt(
                    spm, "20260729_020104"
                )
                spm.write_bytes(b"spm-v2")
                app._refresh_stale_cluster_receipt(
                    spm, "20260729_020105"
                )

        self.assertEqual(app._run_limited.call_count, 4)

    def test_cluster_live_audit_retries_when_input_changes_mid_audit(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        app.log = mock.Mock()
        app.cfg = {"cluster_receipt_refresh_timeout": 321}
        with tempfile.TemporaryDirectory() as temporary, mock.patch.object(
            gui, "LOG_DIR", Path(temporary) / "logs"
        ):
            owner = Path(temporary) / "Tree_elm"
            cluster = owner / "Cluster"
            cluster.mkdir(parents=True)
            spm = owner / "SK_Tree_elm_01.spm"
            spm.write_bytes(b"tree")
            manifest = cluster / "SK_cluster_elm_01.atlas_leaf_targets.json"
            manifest.write_text('{"version": 1}', encoding="utf-8")
            calls = {"count": 0}

            def run_audit(command, *_args, **_kwargs):
                calls["count"] += 1
                report = Path(command[command.index("--json") + 1])
                report.parent.mkdir(parents=True, exist_ok=True)
                if calls["count"] == 1:
                    manifest.write_text('{"version": 2}', encoding="utf-8")
                report.write_text(
                    json.dumps({
                        "attempt": calls["count"],
                        "items": [{"cluster_assembly": {
                            "tree_source_identities": [{
                                "target_spm": {
                                    "path": str(spm),
                                    "exists": True,
                                },
                            }],
                            "dependencies": [{"role": "branch"}],
                            "handoff": {"errors": [], "issues": []},
                        }}],
                    }),
                    encoding="utf-8",
                )
                return 0, Path(temporary) / "refresh.log"

            app._run_limited = mock.Mock(side_effect=run_audit)
            with mock.patch.object(
                gui,
                "cluster_assembly_receipt_resolution",
                return_value={
                    "selected_receipt": str(
                        Path(temporary) / "receipt.json"
                    )
                },
            ):
                first = app._refresh_stale_cluster_receipt(
                    spm,
                    "20260729_025101",
                )
                second = app._refresh_stale_cluster_receipt(
                    spm,
                    "20260729_025102",
                )

        self.assertEqual(calls["count"], 2)
        self.assertEqual(first["live_audit_payload"]["attempt"], 2)
        self.assertEqual(second["live_audit_payload"]["attempt"], 2)
        self.assertTrue(any(
            "retrying once" in call.args[0]
            for call in app.log.call_args_list
        ))

    def test_cluster_live_audit_ignores_new_bwr_runtime_report(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        app.log = mock.Mock()
        app.cfg = {"cluster_receipt_refresh_timeout": 321}
        with tempfile.TemporaryDirectory() as temporary, mock.patch.object(
            gui, "LOG_DIR", Path(temporary) / "logs"
        ):
            owner = Path(temporary) / "Tree_elm"
            cluster = owner / "Cluster"
            reports = cluster / "reports"
            reports.mkdir(parents=True)
            spm = owner / "SK_Tree_elm_01.spm"
            spm.write_bytes(b"tree")

            def run_audit(command, *_args, **_kwargs):
                report = Path(command[command.index("--json") + 1])
                report.parent.mkdir(parents=True, exist_ok=True)
                report.write_text(
                    json.dumps({"items": [{"cluster_assembly": {
                        "tree_source_identities": [{
                            "target_spm": {
                                "path": str(spm),
                                "exists": True,
                            },
                        }],
                        "dependencies": [{"role": "branch"}],
                        "handoff": {"errors": [], "issues": []},
                    }}]}),
                    encoding="utf-8",
                )
                return 0, Path(temporary) / "refresh.log"

            app._run_limited = mock.Mock(side_effect=run_audit)
            with mock.patch.object(
                gui,
                "cluster_assembly_receipt_resolution",
                return_value={
                    "selected_receipt": str(
                        Path(temporary) / "receipt.json"
                    )
                },
            ):
                app._refresh_stale_cluster_receipt(
                    spm,
                    "20260729_026101",
                )
                (reports / (
                    "SK_Tree_elm_01_"
                    "speedtree_repair_pipeline_report_codex.json"
                )).write_text('{"status":"ok"}', encoding="utf-8")
                backups = owner / "_spm_backups"
                backups.mkdir()
                (backups / "SK_Tree_elm_01.spm").write_bytes(b"backup")
                isolated = cluster / ".sk_batch_isolated_bark" / "run"
                isolated.mkdir(parents=True)
                (isolated / "SK_cluster_elm_01.spm").write_bytes(
                    b"isolated"
                )
                app._refresh_stale_cluster_receipt(
                    spm,
                    "20260729_026102",
                )

        self.assertEqual(app._run_limited.call_count, 1)

    def test_cluster_live_audit_report_shape_failures_are_internal_errors(
        self,
    ):
        gui = load_gui_module()
        variants = {
            "missing": None,
            "corrupt": "{",
            "empty_object": "{}",
            "empty_items": '{"items":[]}',
            "identity_unbound": json.dumps({
                "items": [{"cluster_assembly": {
                    "dependencies": [{"role": "branch"}],
                    "handoff": {"errors": [], "issues": []},
                }}],
            }),
        }
        for label, report_text in variants.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as temporary:
                app = self.make_app(gui)
                app.log = mock.Mock()
                app.cfg = {"cluster_receipt_refresh_timeout": 321}
                owner = Path(temporary) / "Tree_elm"
                (owner / "Cluster").mkdir(parents=True)
                spm = owner / "SK_Tree_elm_01.spm"
                spm.write_bytes(b"tree")

                def run_audit(command, *_args, **_kwargs):
                    report = Path(command[command.index("--json") + 1])
                    if report_text is not None:
                        report.parent.mkdir(parents=True, exist_ok=True)
                        report.write_text(report_text, encoding="utf-8")
                    return 0, Path(temporary) / "refresh.log"

                app._run_limited = mock.Mock(side_effect=run_audit)
                with mock.patch.object(
                    gui,
                    "LOG_DIR",
                    Path(temporary) / "logs",
                ), mock.patch.object(
                    gui,
                    "cluster_assembly_receipt_resolution",
                    return_value={
                        "selected_receipt": str(
                            Path(temporary) / "receipt.json"
                        )
                    },
                ), self.assertRaises(gui.BatchItemError) as raised:
                    app._refresh_stale_cluster_receipt(
                        spm,
                        "20260729_027101",
                    )

                self.assertEqual(raised.exception.kind, "internal_error")
                self.assertFalse(app._cluster_receipt_refresh_memo)

    def test_cluster_live_audit_rejects_wrong_singleton_tree_identity(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        app.log = mock.Mock()
        app.cfg = {"cluster_receipt_refresh_timeout": 321}
        with tempfile.TemporaryDirectory() as temporary, mock.patch.object(
            gui, "LOG_DIR", Path(temporary) / "logs"
        ):
            owner = Path(temporary) / "Tree_elm"
            (owner / "Cluster").mkdir(parents=True)
            requested = owner / "SK_Tree_elm_01.spm"
            wrong = owner / "SK_Tree_elm_02.spm"
            requested.write_bytes(b"requested")
            wrong.write_bytes(b"wrong")

            def run_audit(command, *_args, **_kwargs):
                report = Path(command[command.index("--json") + 1])
                report.parent.mkdir(parents=True, exist_ok=True)
                report.write_text(
                    json.dumps({"items": [{"cluster_assembly": {
                        "tree_source_identities": [{
                            "target_spm": {
                                "path": str(wrong),
                                "exists": True,
                            },
                        }],
                        "dependencies": [{"role": "branch"}],
                        "handoff": {"errors": [], "issues": []},
                    }}]}),
                    encoding="utf-8",
                )
                return 0, Path(temporary) / "refresh.log"

            app._run_limited = mock.Mock(side_effect=run_audit)
            with mock.patch.object(
                gui,
                "cluster_assembly_receipt_resolution",
                return_value={
                    "selected_receipt": str(
                        Path(temporary) / "receipt.json"
                    )
                },
            ), self.assertRaises(gui.BatchItemError) as raised:
                app._refresh_stale_cluster_receipt(
                    requested,
                    "20260729_027151",
                )

        self.assertEqual(raised.exception.kind, "internal_error")
        self.assertIn("strict identity-bound", str(raised.exception))
        self.assertFalse(app._cluster_receipt_refresh_memo)

    def test_cluster_live_audit_unique_report_and_immutable_return_payload(
        self,
    ):
        gui = load_gui_module()
        app = self.make_app(gui)
        app.log = mock.Mock()
        app.cfg = {"cluster_receipt_refresh_timeout": 321}
        with tempfile.TemporaryDirectory() as temporary, mock.patch.object(
            gui, "LOG_DIR", Path(temporary) / "logs"
        ):
            owners = [
                Path(temporary) / "owner_a" / "Tree_elm",
                Path(temporary) / "owner_b" / "Tree_elm",
            ]
            spms = []
            for owner in owners:
                (owner / "Cluster").mkdir(parents=True)
                spm = owner / "SK_Tree_elm_01.spm"
                spm.write_bytes(str(owner).encode("utf-8"))
                spms.append(spm)

            def run_audit(command, *_args, **_kwargs):
                target = Path(command[command.index("--target") + 1])
                requested = target / "SK_Tree_elm_01.spm"
                marker = target.parent.name
                report = Path(command[command.index("--json") + 1])
                report.parent.mkdir(parents=True, exist_ok=True)
                report.write_text(
                    json.dumps({
                        "marker": marker,
                        "items": [{"cluster_assembly": {
                            "tree_source_identities": [{
                                "target_spm": {
                                    "path": str(requested),
                                    "exists": True,
                                },
                            }],
                            "dependencies": [{"role": "branch"}],
                            "handoff": {"errors": [], "issues": []},
                        }}],
                    }),
                    encoding="utf-8",
                )
                return 0, Path(temporary) / f"{marker}.log"

            app._run_limited = mock.Mock(side_effect=run_audit)
            with mock.patch.object(
                gui,
                "cluster_assembly_receipt_resolution",
                return_value={
                    "selected_receipt": str(
                        Path(temporary) / "receipt.json"
                    )
                },
            ):
                first = app._refresh_stale_cluster_receipt(
                    spms[0],
                    "20260729_027201",
                )
                second = app._refresh_stale_cluster_receipt(
                    spms[1],
                    "20260729_027201",
                )

            first_report = Path(first["live_audit_report"])
            second_report = Path(second["live_audit_report"])
            self.assertNotEqual(first_report, second_report)
            first_report.write_text(
                json.dumps(second["live_audit_payload"]),
                encoding="utf-8",
            )

        self.assertEqual(first["live_audit_payload"]["marker"], "owner_a")
        self.assertEqual(
            Path(first["selected_contract"][
                "tree_source_identities"
            ][0]["target_spm"]["path"]),
            spms[0],
        )
        self.assertEqual(second["live_audit_payload"]["marker"], "owner_b")

    def test_cluster_live_audit_memo_single_flight_shares_one_result(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        app.log = mock.Mock()
        with tempfile.TemporaryDirectory() as temporary, mock.patch.object(
            gui, "LOG_DIR", Path(temporary) / "logs"
        ):
            spm = Path(temporary) / "Tree_elm" / "SK_Tree_elm_01.spm"
            spm.parent.mkdir()
            spm.write_bytes(b"spm")
            (spm.parent / "Cluster").mkdir()
            report = Path(temporary) / "live.json"
            report.write_text(
                json.dumps({"items": [{"cluster_assembly": {
                    "tree_source_identities": [{
                        "target_spm": {
                            "path": str(spm),
                            "exists": True,
                        },
                    }],
                }}]}),
                encoding="utf-8",
            )
            started = threading.Event()
            release = threading.Event()
            waiter_started = threading.Event()
            calls = {"count": 0}

            class ObservableFuture(gui.Future):
                def result(self, *args, **kwargs):
                    waiter_started.set()
                    return super().result(*args, **kwargs)

            def run_uncached(*_args, **_kwargs):
                calls["count"] += 1
                started.set()
                self.assertTrue(release.wait(5))
                return {
                    "requested_spm": str(spm),
                    "payload": {"items": [{"cluster_assembly": {
                        "tree_source_identities": [{
                            "target_spm": {
                                "path": str(spm),
                                "exists": True,
                            },
                        }],
                        "dependencies": [{"role": "branch"}],
                        "handoff": {"errors": [], "issues": []},
                    }}]},
                    "audit_report": str(report),
                    "log_file": str(Path(temporary) / "live.log"),
                    "persistence": {},
                }

            app._refresh_stale_cluster_receipt_uncached = run_uncached
            with mock.patch.object(
                gui,
                "Future",
                ObservableFuture,
            ), ThreadPoolExecutor(max_workers=2) as pool:
                first_future = pool.submit(
                    app._refresh_stale_cluster_receipt,
                    spm,
                    "20260729_030101",
                )
                self.assertTrue(started.wait(5))
                second_future = pool.submit(
                    app._refresh_stale_cluster_receipt,
                    spm,
                    "20260729_030102",
                )
                self.assertTrue(waiter_started.wait(5))
                release.set()
                first = first_future.result(timeout=5)
                second = second_future.result(timeout=5)

        self.assertEqual(calls["count"], 1)
        self.assertEqual(first, second)
        self.assertEqual(first["policy"], "live_audit_authoritative")

    def test_cluster_live_audit_different_owners_run_concurrently(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        app.log = mock.Mock()
        app.cfg = {"cluster_receipt_refresh_timeout": 321}
        with tempfile.TemporaryDirectory() as temporary, mock.patch.object(
            gui, "LOG_DIR", Path(temporary) / "logs"
        ):
            spms = []
            for owner_name in ("Tree_elm_a", "Tree_elm_b"):
                owner = Path(temporary) / owner_name
                (owner / "Cluster").mkdir(parents=True)
                spm = owner / "SK_Tree_elm_01.spm"
                spm.write_bytes(owner_name.encode("utf-8"))
                spms.append(spm)
            both_entered = threading.Barrier(2)

            def run_audit(command, *_args, **_kwargs):
                target = Path(command[command.index("--target") + 1])
                requested = target / "SK_Tree_elm_01.spm"
                both_entered.wait(timeout=5)
                report = Path(command[command.index("--json") + 1])
                report.parent.mkdir(parents=True, exist_ok=True)
                report.write_text(
                    json.dumps({"items": [{"cluster_assembly": {
                        "tree_source_identities": [{
                            "target_spm": {
                                "path": str(requested),
                                "exists": True,
                            },
                        }],
                        "dependencies": [{"role": "branch"}],
                        "handoff": {"errors": [], "issues": []},
                    }}]}),
                    encoding="utf-8",
                )
                return 0, Path(temporary) / f"{target.name}.log"

            app._run_limited = mock.Mock(side_effect=run_audit)
            with mock.patch.object(
                gui,
                "cluster_assembly_receipt_resolution",
                return_value={
                    "selected_receipt": str(
                        Path(temporary) / "receipt.json"
                    )
                },
            ), ThreadPoolExecutor(max_workers=2) as pool:
                futures = [
                    pool.submit(
                        app._refresh_stale_cluster_receipt,
                        spm,
                        "20260729_030201",
                    )
                    for spm in spms
                ]
                resolutions = [
                    future.result(timeout=10) for future in futures
                ]

        self.assertEqual(app._run_limited.call_count, 2)
        self.assertTrue(all(
            row["policy"] == "live_audit_authoritative"
            for row in resolutions
        ))

    def test_cluster_live_audit_different_owner_hashes_are_not_serialized(
        self,
    ):
        gui = load_gui_module()
        app = self.make_app(gui)
        app.log = mock.Mock()
        with tempfile.TemporaryDirectory() as temporary:
            spms = []
            for owner_name in ("Tree_elm_a", "Tree_elm_b"):
                owner = Path(temporary) / owner_name
                (owner / "Cluster").mkdir(parents=True)
                spm = owner / "SK_Tree_elm_01.spm"
                spm.write_bytes(owner_name.encode("utf-8"))
                spms.append(spm)
            hash_barrier = threading.Barrier(2)
            thread_state = threading.local()

            def fingerprint(spm, **_kwargs):
                count = getattr(thread_state, "count", 0)
                thread_state.count = count + 1
                if count == 0:
                    hash_barrier.wait(timeout=5)
                return f"stable:{gui.normalized_folder_key(spm)}"

            def run_uncached(spm, *_args, **_kwargs):
                contract = {
                    "tree_source_identities": [{
                        "target_spm": {
                            "path": str(spm),
                            "exists": True,
                        },
                    }],
                    "dependencies": [{"role": "branch"}],
                    "handoff": {"errors": [], "issues": []},
                }
                return {
                    "requested_spm": str(spm),
                    "payload": {"items": [{
                        "cluster_assembly": contract,
                    }]},
                    "selected_contract": contract,
                    "audit_report": str(
                        Path(temporary) / f"{spm.parent.name}.json"
                    ),
                    "log_file": str(
                        Path(temporary) / f"{spm.parent.name}.log"
                    ),
                    "persistence": {},
                }

            app._cluster_receipt_refresh_input_fingerprint = fingerprint
            app._refresh_stale_cluster_receipt_uncached = run_uncached
            with mock.patch.object(
                gui,
                "cluster_assembly_receipt_resolution",
                return_value={
                    "selected_receipt": str(
                        Path(temporary) / "receipt.json"
                    )
                },
            ), ThreadPoolExecutor(max_workers=2) as pool:
                futures = [
                    pool.submit(
                        app._refresh_stale_cluster_receipt,
                        spm,
                        "20260729_030251",
                    )
                    for spm in spms
                ]
                resolutions = [
                    future.result(timeout=10) for future in futures
                ]

        self.assertEqual(len(resolutions), 2)

    def test_cluster_live_audit_single_flight_keeps_caller_policy_separate(
        self,
    ):
        gui = load_gui_module()
        app = self.make_app(gui)
        app.log = mock.Mock()
        app.cfg = {"cluster_receipt_refresh_timeout": 321}
        with tempfile.TemporaryDirectory() as temporary, mock.patch.object(
            gui, "LOG_DIR", Path(temporary) / "logs"
        ):
            owner = Path(temporary) / "Tree_elm"
            cluster = owner / "Cluster"
            cluster.mkdir(parents=True)
            spm = owner / "SK_Tree_elm_01.spm"
            spm.write_bytes(b"tree")
            producer = cluster / "SK_cluster_elm_02.spm"
            producer.write_bytes(b"cluster")
            started = threading.Event()
            release = threading.Event()
            waiter_started = threading.Event()

            class ObservableFuture(gui.Future):
                def result(self, *args, **kwargs):
                    waiter_started.set()
                    return super().result(*args, **kwargs)

            def run_audit(command, *_args, **_kwargs):
                report = Path(command[command.index("--json") + 1])
                report.parent.mkdir(parents=True, exist_ok=True)
                started.set()
                self.assertTrue(release.wait(5))
                report.write_text(
                    json.dumps({"items": [{"cluster_assembly": {
                        "tree_source_identities": [{
                            "target_spm": {
                                "path": str(spm),
                                "exists": True,
                            },
                        }],
                        "dependencies": [{
                            "role": "cluster",
                            "spm": str(producer),
                        }],
                        "handoff": {"errors": [{
                            "code": "NORMALIZED_VARIANTS_REQUIRED",
                            "role": "cluster",
                            "spm": str(producer),
                        }]},
                    }}]}),
                    encoding="utf-8",
                )
                return 0, Path(temporary) / "refresh.log"

            app._run_limited = mock.Mock(side_effect=run_audit)
            persisted = {
                "selected_receipt": str(
                    Path(temporary) / "receipt.json"
                )
            }
            with mock.patch.object(
                gui,
                "cluster_assembly_receipt_resolution",
                return_value=persisted,
            ), mock.patch.object(
                gui,
                "Future",
                ObservableFuture,
            ), ThreadPoolExecutor(max_workers=2) as pool:
                producer_future = pool.submit(
                    app._refresh_stale_cluster_receipt,
                    spm,
                    "20260729_035101",
                    producer_spm=producer,
                )
                self.assertTrue(started.wait(5))
                owner_future = pool.submit(
                    app._refresh_stale_cluster_receipt,
                    spm,
                    "20260729_035102",
                )
                self.assertTrue(waiter_started.wait(5))
                release.set()
                producer_resolution = producer_future.result(timeout=5)
                with self.assertRaises(gui.BatchItemError) as raised:
                    owner_future.result(timeout=5)

        self.assertEqual(app._run_limited.call_count, 1)
        self.assertTrue(
            producer_resolution["producer_repair_issue_tolerated"]
        )
        self.assertEqual(raised.exception.kind, "data_error")
        self.assertIn(
            "NORMALIZED_VARIANTS_REQUIRED",
            str(raised.exception),
        )

    def test_cluster_live_audit_allows_planned_producers_but_owner_is_strict(
        self,
    ):
        gui = load_gui_module()
        app = self.make_app(gui)
        app.log = mock.Mock()
        app.cfg = {"cluster_receipt_refresh_timeout": 321}
        with tempfile.TemporaryDirectory() as temporary, mock.patch.object(
            gui, "LOG_DIR", Path(temporary) / "logs"
        ):
            owner = Path(temporary) / "Tree_elm"
            cluster = owner / "Cluster"
            cluster.mkdir(parents=True)
            spm = owner / "SK_Tree_elm_01.spm"
            spm.write_bytes(b"tree")
            producer_a = cluster / "SK_cluster_elm_01.spm"
            producer_b = cluster / "SK_cluster_elm_02.spm"
            producer_a.write_bytes(b"a")
            producer_b.write_bytes(b"b")
            started = threading.Event()
            release = threading.Event()
            waiters_ready = threading.Event()
            waiter_count = {"value": 0}
            waiter_lock = threading.Lock()

            class ObservableFuture(gui.Future):
                def result(self, *args, **kwargs):
                    with waiter_lock:
                        waiter_count["value"] += 1
                        if waiter_count["value"] == 2:
                            waiters_ready.set()
                    return super().result(*args, **kwargs)

            def run_audit(command, *_args, **_kwargs):
                report = Path(command[command.index("--json") + 1])
                report.parent.mkdir(parents=True, exist_ok=True)
                started.set()
                self.assertTrue(release.wait(5))
                report.write_text(
                    json.dumps({"items": [{"cluster_assembly": {
                        "tree_source_identities": [{
                            "target_spm": {
                                "path": str(spm),
                                "exists": True,
                            },
                        }],
                        "dependencies": [
                            {"role": "cluster", "spm": str(producer_a)},
                            {"role": "cluster", "spm": str(producer_b)},
                        ],
                        "handoff": {"errors": [
                            {
                                "code": "NORMALIZED_VARIANTS_REQUIRED",
                                "role": "cluster",
                                "spm": str(producer_a),
                            },
                            {
                                "code": "NORMALIZED_VARIANTS_REQUIRED",
                                "role": "cluster",
                                "spm": str(producer_b),
                            },
                        ]},
                    }}]}),
                    encoding="utf-8",
                )
                return 0, Path(temporary) / "refresh.log"

            app._run_limited = mock.Mock(side_effect=run_audit)
            allowed = (producer_a, producer_b)
            with mock.patch.object(
                gui,
                "cluster_assembly_receipt_resolution",
                return_value={
                    "selected_receipt": str(
                        Path(temporary) / "receipt.json"
                    )
                },
            ), mock.patch.object(
                gui,
                "Future",
                ObservableFuture,
            ), ThreadPoolExecutor(max_workers=3) as pool:
                future_a = pool.submit(
                    app._refresh_stale_cluster_receipt,
                    spm,
                    "20260729_036101",
                    producer_spm=producer_a,
                    allowed_producer_spms=allowed,
                )
                self.assertTrue(started.wait(5))
                future_b = pool.submit(
                    app._refresh_stale_cluster_receipt,
                    spm,
                    "20260729_036102",
                    producer_spm=producer_b,
                    allowed_producer_spms=allowed,
                )
                owner_future = pool.submit(
                    app._refresh_stale_cluster_receipt,
                    spm,
                    "20260729_036103",
                )
                self.assertTrue(waiters_ready.wait(5))
                release.set()
                resolution_a = future_a.result(timeout=5)
                resolution_b = future_b.result(timeout=5)
                with self.assertRaises(gui.BatchItemError) as raised:
                    owner_future.result(timeout=5)

        self.assertEqual(app._run_limited.call_count, 1)
        self.assertTrue(resolution_a["producer_repair_issue_tolerated"])
        self.assertTrue(resolution_b["producer_repair_issue_tolerated"])
        self.assertEqual(raised.exception.kind, "data_error")

    def test_cluster_live_audit_bookkeeping_error_releases_waiter(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        app.log = mock.Mock()
        app.stop_flag = threading.Event()
        with tempfile.TemporaryDirectory() as temporary:
            owner = Path(temporary) / "Tree_elm"
            (owner / "Cluster").mkdir(parents=True)
            spm = owner / "SK_Tree_elm_01.spm"
            spm.write_bytes(b"tree")
            started = threading.Event()
            release = threading.Event()
            waiter_started = threading.Event()
            audit_finished = threading.Event()
            completions = {"result": 0, "exception": 0}

            class ObservableFuture(gui.Future):
                def result(self, *args, **kwargs):
                    waiter_started.set()
                    return super().result(*args, **kwargs)

                def set_result(self, result):
                    completions["result"] += 1
                    return super().set_result(result)

                def set_exception(self, exception):
                    completions["exception"] += 1
                    return super().set_exception(exception)

            def fingerprint(*_args, **_kwargs):
                if audit_finished.is_set():
                    raise RuntimeError("post-audit bookkeeping failed")
                return "stable"

            def run_uncached(*_args, **_kwargs):
                started.set()
                self.assertTrue(release.wait(5))
                audit_finished.set()
                return {
                    "requested_spm": str(spm),
                    "payload": {"items": [{"cluster_assembly": {
                        "tree_source_identities": [{
                            "target_spm": {
                                "path": str(spm),
                                "exists": True,
                            },
                        }],
                        "dependencies": [{"role": "branch"}],
                        "handoff": {"errors": [], "issues": []},
                    }}]},
                    "selected_contract": {
                        "tree_source_identities": [{"target_spm": {
                            "path": str(spm),
                        }}],
                        "dependencies": [{"role": "branch"}],
                    },
                    "audit_report": str(Path(temporary) / "live.json"),
                    "log_file": str(Path(temporary) / "live.log"),
                    "persistence": {},
                }

            app._cluster_receipt_refresh_input_fingerprint = fingerprint
            app._refresh_stale_cluster_receipt_uncached = run_uncached
            with mock.patch.object(
                gui,
                "Future",
                ObservableFuture,
            ), ThreadPoolExecutor(max_workers=2) as pool:
                first_future = pool.submit(
                    app._refresh_stale_cluster_receipt,
                    spm,
                    "20260729_037101",
                )
                self.assertTrue(started.wait(5))
                second_future = pool.submit(
                    app._refresh_stale_cluster_receipt,
                    spm,
                    "20260729_037102",
                )
                self.assertTrue(waiter_started.wait(5))
                release.set()
                for future in (first_future, second_future):
                    with self.assertRaises(RuntimeError):
                        future.result(timeout=5)

        self.assertEqual(completions, {"result": 0, "exception": 1})
        self.assertFalse(app._cluster_receipt_refresh_flights)

    def test_cluster_live_audit_waiter_honors_stop_request(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        app.log = mock.Mock()
        app.stop_flag = threading.Event()
        with tempfile.TemporaryDirectory() as temporary:
            owner = Path(temporary) / "Tree_elm"
            (owner / "Cluster").mkdir(parents=True)
            spm = owner / "SK_Tree_elm_01.spm"
            spm.write_bytes(b"tree")
            started = threading.Event()
            release = threading.Event()
            waiter_started = threading.Event()
            report = Path(temporary) / "live.json"

            class ObservableFuture(gui.Future):
                def result(self, *args, **kwargs):
                    waiter_started.set()
                    return super().result(*args, **kwargs)

            def run_uncached(*_args, **_kwargs):
                started.set()
                self.assertTrue(release.wait(5))
                return {
                    "requested_spm": str(spm),
                    "payload": {"items": [{"cluster_assembly": {
                        "tree_source_identities": [{
                            "target_spm": {
                                "path": str(spm),
                                "exists": True,
                            },
                        }],
                        "dependencies": [{"role": "branch"}],
                        "handoff": {"errors": [], "issues": []},
                    }}]},
                    "selected_contract": {
                        "tree_source_identities": [{"target_spm": {
                            "path": str(spm),
                        }}],
                        "dependencies": [{"role": "branch"}],
                    },
                    "audit_report": str(report),
                    "log_file": str(Path(temporary) / "live.log"),
                    "persistence": {},
                }

            app._refresh_stale_cluster_receipt_uncached = run_uncached
            with mock.patch.object(
                gui,
                "Future",
                ObservableFuture,
            ), ThreadPoolExecutor(max_workers=2) as pool:
                owner_future = pool.submit(
                    app._refresh_stale_cluster_receipt,
                    spm,
                    "20260729_038101",
                )
                self.assertTrue(started.wait(5))
                waiter_future = pool.submit(
                    app._refresh_stale_cluster_receipt,
                    spm,
                    "20260729_038102",
                )
                self.assertTrue(waiter_started.wait(5))
                app.stop_flag.set()
                with self.assertRaises(gui.BatchItemError) as raised:
                    waiter_future.result(timeout=2)
                release.set()
                owner_future.result(timeout=5)

        self.assertIn("wait stopped", str(raised.exception))

    def test_cluster_live_audit_memo_shares_but_does_not_cache_failure(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        app.log = mock.Mock()
        with tempfile.TemporaryDirectory() as temporary:
            spm = Path(temporary) / "Tree_elm" / "SK_Tree_elm_01.spm"
            spm.parent.mkdir()
            spm.write_bytes(b"spm")
            (spm.parent / "Cluster").mkdir()
            started = threading.Event()
            release = threading.Event()
            waiter_started = threading.Event()
            calls = {"count": 0}

            class ObservableFuture(gui.Future):
                def result(self, *args, **kwargs):
                    waiter_started.set()
                    return super().result(*args, **kwargs)

            def run_uncached(*_args, **_kwargs):
                calls["count"] += 1
                if calls["count"] == 1:
                    started.set()
                    self.assertTrue(release.wait(5))
                raise gui.BatchItemError(
                    "live audit failed",
                    kind="internal_error",
                )

            app._refresh_stale_cluster_receipt_uncached = run_uncached
            with mock.patch.object(
                gui,
                "Future",
                ObservableFuture,
            ), ThreadPoolExecutor(max_workers=2) as pool:
                first_future = pool.submit(
                    app._refresh_stale_cluster_receipt,
                    spm,
                    "20260729_040101",
                )
                self.assertTrue(started.wait(5))
                second_future = pool.submit(
                    app._refresh_stale_cluster_receipt,
                    spm,
                    "20260729_040102",
                )
                self.assertTrue(waiter_started.wait(5))
                release.set()
                for future in (first_future, second_future):
                    with self.assertRaises(gui.BatchItemError):
                        future.result(timeout=5)

            with self.assertRaises(gui.BatchItemError):
                app._refresh_stale_cluster_receipt(
                    spm,
                    "20260729_040103",
                )

        self.assertEqual(calls["count"], 2)

    def test_receipt_refresh_reports_live_audit_data_error(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        app.log = mock.Mock()
        app.cfg = {"cluster_receipt_refresh_timeout": 321}
        with tempfile.TemporaryDirectory() as temporary, mock.patch.object(
            gui, "LOG_DIR", Path(temporary) / "logs"
        ):
            spm = Path(temporary) / "Tree_elm" / "SK_Tree_elm_01.spm"
            spm.parent.mkdir()
            spm.write_bytes(b"spm")

            def run_audit(command, *_args, **_kwargs):
                report = Path(command[command.index("--json") + 1])
                report.parent.mkdir(parents=True, exist_ok=True)
                report.write_text(
                    json.dumps({
                        "items": [{
                            "cluster_assembly": {
                                "tree_source_identities": [{
                                    "target_spm": {
                                        "path": str(spm),
                                        "exists": True,
                                    },
                                }],
                                "dependencies": [{"role": "branch"}],
                                "handoff": {
                                    "errors": [{
                                        "code": "CLUSTER_TGA_BASENAME_INVALID",
                                        "role": "branch",
                                        "details": {
                                            "status": "missing",
                                            "missing": ["missing.tga"],
                                        },
                                    }]
                                }
                            }
                        }]
                    }),
                    encoding="utf-8",
                )
                # The live report, rather than the subprocess exit code or a
                # persisted receipt, is authoritative for real data failures.
                return 0, Path(temporary) / "refresh.log"

            app._run_limited = mock.Mock(side_effect=run_audit)
            with mock.patch.object(
                gui,
                "cluster_assembly_receipt_resolution",
                side_effect=gui.ClusterAssemblyReceiptStaleError("stale"),
            ), self.assertRaises(gui.BatchItemError) as raised:
                app._refresh_stale_cluster_receipt(
                    spm, "20260725_120000"
                )

            self.assertEqual(raised.exception.kind, "data_error")
            self.assertIn(
                "CLUSTER_TGA_BASENAME_INVALID",
                str(raised.exception),
            )
            self.assertIn("missing.tga", str(raised.exception))

    def _run_producer_variant_receipt_refresh(
        self,
        gui,
        *,
        exit_code,
        issue_spm_kind="producer",
        extra_issues=None,
    ):
        app = self.make_app(gui)
        app.log = mock.Mock()
        app.cfg = {"cluster_receipt_refresh_timeout": 321}
        with tempfile.TemporaryDirectory() as temporary, mock.patch.object(
            gui, "LOG_DIR", Path(temporary) / "logs"
        ):
            owner = Path(temporary) / "Tree_elm"
            cluster = owner / "Cluster"
            cluster.mkdir(parents=True)
            spm = owner / "SK_Tree_elm_01.spm"
            spm.write_bytes(b"tree")
            producer = cluster / "SK_cluster_elm_02.spm"
            producer.write_bytes(b"cluster")
            other_producer = cluster / "SK_cluster_elm_03.spm"
            other_producer.write_bytes(b"other cluster")
            selected = Path(temporary) / "receipt.json"
            issue_spm = (
                producer
                if issue_spm_kind == "producer"
                else other_producer
            )

            def run_audit(command, *_args, **_kwargs):
                report = Path(command[command.index("--json") + 1])
                report.parent.mkdir(parents=True, exist_ok=True)
                issues = [{
                    "code": "NORMALIZED_VARIANTS_REQUIRED",
                    "role": "cluster",
                    "spm": str(issue_spm),
                }]
                issues.extend(extra_issues or [])
                report.write_text(
                    json.dumps({
                        "items": [{
                            "cluster_assembly": {
                                "tree_source_identities": [{
                                    "target_spm": {"path": str(spm)},
                                }],
                                "dependencies": [{
                                    "role": "cluster",
                                    "spm": str(producer),
                                }],
                                "handoff": {
                                    "errors": issues,
                                },
                            }
                        }]
                    }),
                    encoding="utf-8",
                )
                return exit_code, Path(temporary) / "refresh.log"

            app._run_limited = mock.Mock(side_effect=run_audit)
            with mock.patch.object(
                gui,
                "cluster_assembly_receipt_resolution",
                side_effect=[
                    gui.ClusterAssemblyReceiptStaleError("stale"),
                    {"selected_receipt": str(selected)},
                ],
            ):
                resolution = app._refresh_stale_cluster_receipt(
                    spm,
                    "20260725_120000",
                    producer_spm=producer,
                )

            return app, resolution, producer

    def test_receipt_refresh_allows_exact_producer_variant_bootstrap(self):
        gui = load_gui_module()
        app, resolution, producer = (
            self._run_producer_variant_receipt_refresh(
                gui,
                exit_code=0,
            )
        )

        self.assertEqual(
            resolution["policy"],
            "live_audit_authoritative",
        )
        self.assertTrue(
            resolution["producer_repair_issue_tolerated"]
        )
        self.assertEqual(
            Path(resolution["producer_spm"]),
            producer.resolve(),
        )
        self.assertTrue(any(
            "producer bootstrap allowed" in call.args[0]
            for call in app.log.call_args_list
        ))

    def test_producer_variant_bootstrap_nonzero_exit_is_internal_error(self):
        gui = load_gui_module()
        with self.assertRaises(gui.BatchItemError) as raised:
            self._run_producer_variant_receipt_refresh(
                gui,
                exit_code=1,
            )

        self.assertEqual(raised.exception.kind, "internal_error")
        self.assertIn("live audit process failed", str(raised.exception))

    def test_producer_variant_bootstrap_rejects_mixed_data_issues(self):
        gui = load_gui_module()
        with self.assertRaises(gui.BatchItemError) as raised:
            self._run_producer_variant_receipt_refresh(
                gui,
                exit_code=0,
                extra_issues=[{
                    "code": "CLUSTER_TGA_BASENAME_INVALID",
                    "role": "branch",
                    "details": {
                        "status": "missing",
                        "missing": ["missing.tga"],
                    },
                }],
            )

        self.assertEqual(raised.exception.kind, "data_error")
        self.assertIn(
            "CLUSTER_TGA_BASENAME_INVALID",
            str(raised.exception),
        )

    def test_producer_variant_bootstrap_rejects_wrong_producer(self):
        gui = load_gui_module()
        with self.assertRaises(gui.BatchItemError) as raised:
            self._run_producer_variant_receipt_refresh(
                gui,
                exit_code=0,
                issue_spm_kind="other",
            )

        self.assertEqual(raised.exception.kind, "data_error")
        self.assertIn(
            "NORMALIZED_VARIANTS_REQUIRED",
            str(raised.exception),
        )

    def test_receipt_self_stale_after_clean_audit_uses_live_contract(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        app.log = mock.Mock()
        app.cfg = {"cluster_receipt_refresh_timeout": 321}
        with tempfile.TemporaryDirectory() as temporary, mock.patch.object(
            gui, "LOG_DIR", Path(temporary) / "logs"
        ):
            spm = Path(temporary) / "Tree_elm" / "SK_Tree_elm_01.spm"
            spm.parent.mkdir()
            spm.write_bytes(b"spm")

            def run_audit(command, *_args, **_kwargs):
                report = Path(command[command.index("--json") + 1])
                report.parent.mkdir(parents=True, exist_ok=True)
                report.write_text(
                    json.dumps({"items": [{"cluster_assembly": {
                        "tree_source_identities": [{
                            "target_spm": {"path": str(spm)},
                        }],
                        "dependencies": [{"role": "branch"}],
                        "handoff": {"errors": [], "issues": []},
                    }}]}),
                    encoding="utf-8",
                )
                return 0, Path(temporary) / "refresh.log"

            app._run_limited = mock.Mock(side_effect=run_audit)
            with mock.patch.object(
                gui,
                "cluster_assembly_receipt_resolution",
                side_effect=[
                    gui.ClusterAssemblyReceiptStaleError("old"),
                    gui.ClusterAssemblyReceiptStaleError("self-stale"),
                ],
            ):
                resolution = app._refresh_stale_cluster_receipt(
                    spm, "20260725_120000"
                )

            self.assertEqual(
                resolution["policy"],
                "live_audit_authoritative",
            )
            report_name = Path(resolution["live_audit_report"]).name
            self.assertTrue(report_name.startswith("SK_Tree_elm_01_"))
            self.assertIn("_jobadhoc_20260725_120000_", report_name)
            self.assertTrue(report_name.endswith(".json"))
            self.assertIn(
                "self-stale",
                resolution["receipt_persistence_warning"],
            )

    def test_receipt_persistence_only_nonzero_exit_uses_live_contract(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        app.log = mock.Mock()
        app.cfg = {"cluster_receipt_refresh_timeout": 321}
        with tempfile.TemporaryDirectory() as temporary, mock.patch.object(
            gui, "LOG_DIR", Path(temporary) / "logs"
        ):
            spm = Path(temporary) / "Tree_elm" / "SK_Tree_elm_01.spm"
            spm.parent.mkdir()
            spm.write_bytes(b"spm")

            def run_audit(command, *_args, **_kwargs):
                report = Path(command[command.index("--json") + 1])
                report.parent.mkdir(parents=True, exist_ok=True)
                report.write_text(
                    json.dumps({
                        "cluster_assembly_receipt_persistence": {
                            "status": "warning",
                            "stage": "receipt_persistence",
                            "code": "RECEIPT_PERSISTENCE_FAILED",
                            "live_audit_complete": True,
                            "error": "read-only receipt directory",
                        },
                        "items": [{"cluster_assembly": {
                            "tree_source_identities": [{
                                "target_spm": {"path": str(spm)},
                            }],
                            "dependencies": [{"role": "branch"}],
                            "handoff": {"errors": [], "issues": []},
                        }}],
                    }),
                    encoding="utf-8",
                )
                return 1, Path(temporary) / "refresh.log"

            app._run_limited = mock.Mock(side_effect=run_audit)
            with mock.patch.object(
                gui,
                "cluster_assembly_receipt_resolution",
                side_effect=[
                    gui.ClusterAssemblyReceiptStaleError("old"),
                    gui.ClusterAssemblyReceiptStaleError("still stale"),
                ],
            ):
                resolution = app._refresh_stale_cluster_receipt(
                    spm, "20260725_120000"
                )

            self.assertEqual(
                resolution["policy"],
                "live_audit_authoritative",
            )
            self.assertIn(
                "read-only receipt directory",
                resolution["receipt_persistence_warning"],
            )

    def test_nonzero_live_audit_process_crash_remains_internal_error(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        app.log = mock.Mock()
        app.cfg = {"cluster_receipt_refresh_timeout": 321}
        with tempfile.TemporaryDirectory() as temporary:
            spm = Path(temporary) / "Tree_elm" / "SK_Tree_elm_01.spm"
            spm.parent.mkdir()
            spm.write_bytes(b"spm")
            app._run_limited = mock.Mock(
                return_value=(1, Path(temporary) / "refresh.log")
            )
            with mock.patch.object(
                gui,
                "cluster_assembly_receipt_resolution",
                side_effect=gui.ClusterAssemblyReceiptStaleError("old"),
            ), self.assertRaises(gui.BatchItemError) as raised:
                app._refresh_stale_cluster_receipt(
                    spm, "20260725_120000"
                )

            self.assertEqual(raised.exception.kind, "internal_error")
            self.assertIn("live audit process failed", str(raised.exception))

    def test_blender_job_reuses_immutable_live_audit_after_preflight(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        with tempfile.TemporaryDirectory() as temporary, mock.patch.object(
            gui, "LOG_DIR", Path(temporary) / "logs"
        ):
            root = Path(temporary) / "Tree_elm"
            root.mkdir()
            spm = root / "SK_Tree_elm_01.spm"
            write_empty_spm(spm)
            app.force_rerun = True
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
            live_report = Path(temporary) / "live_audit.json"
            live_payload = {"items": [{"cluster_assembly": {
                    "tree_source_identities": [{
                        "target_spm": {"path": str(spm)},
                    }],
                    "dependencies": [{"role": "branch"}],
                    "handoff": {"errors": [], "issues": []},
                }}]}
            selected_contract = live_payload["items"][0][
                "cluster_assembly"
            ]
            live_report.write_text(
                json.dumps(live_payload),
                encoding="utf-8",
            )
            embedded_live_audit = {
                "policy": "live_audit_authoritative",
                "live_audit_report": str(live_report),
                "live_audit_payload": live_payload,
                "selected_contract": selected_contract,
                "receipt_persistence_warning": "read-only",
            }
            app._refresh_stale_cluster_receipt = mock.Mock(
                return_value=embedded_live_audit
            )
            app._leaf_reference_ready = mock.Mock(
                return_value=(True, "ok")
            )
            app._handoff_ready = mock.Mock(return_value=(True, "ready"))
            app._blend_status_text = mock.Mock(return_value="current")
            app.log = mock.Mock()

            def fake_run(command, log_name, _timeout, **_kwargs):
                report = Path(command[command.index("--report") + 1])
                report.parent.mkdir(parents=True, exist_ok=True)
                report.write_text(
                    json.dumps({"status": "ok", "warnings": []}),
                    encoding="utf-8",
                )
                return 0, Path(temporary) / log_name

            app._run_limited = fake_run
            item = {
                "manual_bones_locked": False,
                "wind_override": "auto",
            }
            app.state[str(spm)] = {}
            with mock.patch(
                "spm_audit.audit_spm",
                return_value={},
            ), mock.patch(
                "spm_audit.sk_readiness",
                return_value={"ready": True},
            ), mock.patch.object(gui, "save_state"):
                app._job_blender(str(spm), spm, item)

            self.assertEqual(
                app._refresh_stale_cluster_receipt.call_count,
                1,
            )
            material_reports = list(
                (Path(temporary) / "logs").glob(
                    "SK_Tree_elm_01_material_preflight_*.json"
                )
            )
            self.assertEqual(len(material_reports), 1)
            material_payload = json.loads(
                material_reports[0].read_text(encoding="utf-8")
            )
            self.assertIn("cluster_assembly", material_payload)

    def test_missing_cluster_receipt_is_audited_and_recovered(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        app.log = mock.Mock()
        app.cfg = {"cluster_receipt_refresh_timeout": 321}
        with tempfile.TemporaryDirectory() as temporary, mock.patch.object(
            gui, "LOG_DIR", Path(temporary) / "logs"
        ):
            spm = Path(temporary) / "Tree_elm" / "SK_Tree_elm_01.spm"
            spm.parent.mkdir()
            spm.write_bytes(b"spm")
            (spm.parent / "Cluster").mkdir()
            selected = Path(temporary) / "receipt.json"
            current = {"selected_receipt": str(selected)}

            def run_audit(command, *_args, **_kwargs):
                report = Path(command[command.index("--json") + 1])
                report.parent.mkdir(parents=True, exist_ok=True)
                report.write_text(
                    json.dumps({"items": [{"cluster_assembly": {
                        "tree_source_identities": [{
                            "target_spm": {"path": str(spm)},
                        }],
                        "dependencies": [{"role": "branch"}],
                        "handoff": {"errors": [], "issues": []},
                    }}]}),
                    encoding="utf-8",
                )
                return 0, Path(temporary) / "refresh.log"

            app._run_limited = mock.Mock(side_effect=run_audit)

            with mock.patch.object(
                gui,
                "cluster_assembly_receipt_resolution",
                side_effect=[FileNotFoundError("missing"), current],
            ):
                resolution = app._refresh_stale_cluster_receipt(
                    spm, "20260725_120000"
                )

            self.assertEqual(
                resolution["policy"],
                "live_audit_authoritative",
            )
            self.assertEqual(
                resolution["persisted_receipt"],
                current["selected_receipt"],
            )
            self.assertEqual(app._run_limited.call_count, 1)
            command = app._run_limited.call_args.args[0]
            self.assertEqual(
                command[command.index("--target") + 1], str(spm.parent)
            )
            self.assertEqual(
                command[command.index("--target-mesh") + 1],
                "Tree_elm_01",
            )

    def test_missing_non_cluster_receipt_remains_pass_through_without_audit(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        app.log = mock.Mock()
        app.cfg = {"cluster_receipt_refresh_timeout": 321}
        with tempfile.TemporaryDirectory() as temporary, mock.patch.object(
            gui, "LOG_DIR", Path(temporary) / "logs"
        ):
            spm = Path(temporary) / "Tree_plain" / "SK_Tree_plain_01.spm"
            spm.parent.mkdir()
            spm.write_bytes(b"spm")
            app._run_limited = mock.Mock(
                return_value=(0, Path(temporary) / "refresh.log")
            )

            with mock.patch.object(
                gui,
                "cluster_assembly_receipt_resolution",
                side_effect=[
                    FileNotFoundError("missing"),
                    FileNotFoundError("still missing"),
                ],
            ):
                resolution = app._refresh_stale_cluster_receipt(
                    spm, "20260725_120000"
                )

            self.assertIsNone(resolution)
            app._run_limited.assert_not_called()

    def test_missing_cluster_owner_audit_report_fails_closed(
        self,
    ):
        gui = load_gui_module()
        app = self.make_app(gui)
        app.log = mock.Mock()
        app.cfg = {"cluster_receipt_refresh_timeout": 321}
        with tempfile.TemporaryDirectory() as temporary, mock.patch.object(
            gui, "LOG_DIR", Path(temporary) / "logs"
        ):
            spm = Path(temporary) / "Tree_plain" / "SK_Tree_plain_01.spm"
            spm.parent.mkdir()
            spm.write_bytes(b"spm")
            (spm.parent / "Cluster").mkdir()
            app._run_limited = mock.Mock(
                return_value=(0, Path(temporary) / "refresh.log")
            )

            with mock.patch.object(
                gui,
                "cluster_assembly_receipt_resolution",
                side_effect=[
                    FileNotFoundError("missing"),
                    FileNotFoundError("still missing"),
                ],
            ), self.assertRaises(gui.BatchItemError) as raised:
                app._refresh_stale_cluster_receipt(
                    spm, "20260725_120000"
                )

            self.assertEqual(raised.exception.kind, "internal_error")
            self.assertIn("missing, corrupt, or empty", str(raised.exception))
            self.assertEqual(app._run_limited.call_count, 1)

    def test_identity_bound_live_pass_through_overrides_old_embedded_contract(
        self,
    ):
        gui = load_gui_module()
        app = self.make_app(gui)
        app.log = mock.Mock()
        app.cfg = {"cluster_receipt_refresh_timeout": 321}
        with tempfile.TemporaryDirectory() as temporary, mock.patch.object(
            gui, "LOG_DIR", Path(temporary) / "logs"
        ):
            spm = Path(temporary) / "tree_lauraceae" / "SK_tree_lauraceae_10.spm"
            spm.parent.mkdir()
            spm.write_bytes(b"spm")
            (spm.parent / "Cluster").mkdir()

            def run_audit(command, *_args, **_kwargs):
                report = Path(command[command.index("--json") + 1])
                report.parent.mkdir(parents=True, exist_ok=True)
                report.write_text(
                    json.dumps({"items": [{"cluster_assembly": {
                        "tree_source_identities": [{
                            "target_spm": {"path": str(spm)},
                            "authoritative_tree_source": {"path": str(spm)},
                        }],
                        "dependencies": [],
                        "handoff": {
                            "status": "pass_through",
                            "errors": [],
                            "issues": [],
                            "roles": [],
                        },
                    }}]}),
                    encoding="utf-8",
                )
                return 0, Path(temporary) / "refresh.log"

            app._run_limited = mock.Mock(side_effect=run_audit)
            with mock.patch.object(
                gui,
                "cluster_assembly_receipt_resolution",
                side_effect=[
                    FileNotFoundError("missing"),
                    FileNotFoundError("still missing"),
                ],
            ):
                resolution = app._refresh_stale_cluster_receipt(
                    spm, "20260728_121500"
                )

            self.assertEqual(
                resolution["policy"],
                "live_audit_authoritative_pass_through",
            )
            self.assertEqual(
                resolution["selected_receipt"],
                resolution["live_audit_report"],
            )
            self.assertIsNone(resolution["persisted_receipt"])

    def test_blender_job_accepts_source_review_and_leaves_push_blocked(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            owner = root / "Tree_elm"
            owner.mkdir(parents=True)
            spm = owner / "SK_Tree_elm_01.spm"
            write_empty_spm(spm)
            app.force_rerun = True
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
                    payload = {"status": "ok"}
                else:
                    payload = {
                        "status": "ok",
                        "warnings": [],
                        "source_review_required": True,
                        "unreal_push_ready": False,
                        "handoff_preflight": {
                            "status": "source_review",
                            "unreal_push_ready": False,
                        },
                    }
                report.write_text(json.dumps(payload), encoding="utf-8")
                return 0, root / log_name

            app._run_limited = fake_run
            app._leaf_reference_ready = mock.Mock(return_value=(True, "정상"))
            app._handoff_ready = mock.Mock(
                side_effect=[(False, "생성 필요"), (False, "원본 검토 필요")]
            )
            app._blend_status_text = mock.Mock(
                return_value="Blend 완료 · 원본 검토 필요 · Unreal Push 차단"
            )
            item = {"manual_bones_locked": False, "wind_override": "auto"}

            with mock.patch("spm_audit.audit_spm", return_value={}), mock.patch(
                "spm_audit.sk_readiness", return_value={"ready": True}
            ), mock.patch.object(gui, "save_state"):
                app._job_blender(str(spm), spm, item)

            self.assertTrue(any(
                "Unreal Push 차단" in call.args[0]
                for call in app.log.call_args_list
            ))

    def test_cluster_blender_job_builds_raw_then_runs_normalizer_transaction(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            owner = root / "Tree_elm"
            cluster = owner / "Cluster"
            cluster.mkdir(parents=True)
            spm = cluster / "SK_branch_elm_01.spm"
            target = owner / "SK_Tree_elm_01.spm"
            source_target = owner / "Tree_elm_01.spm"
            write_empty_spm(spm)
            write_empty_spm(target)
            write_empty_spm(source_target)
            other_producer = cluster / "SK_cluster_elm_02.spm"
            write_empty_spm(other_producer)
            blend = spm.with_suffix(".blend")
            blend.write_bytes(b"old-normalized-blend")
            from atlas_target_registry import save_target_registry

            save_target_registry(blend, [target])
            pipeline_report = cluster / "reports" / (
                "SK_branch_elm_01_"
                "speedtree_repair_pipeline_report_codex.json"
            )
            pipeline_report.parent.mkdir()
            pipeline_report.write_text(
                json.dumps({"handoff_preflight": {"status": "ok"}}),
                encoding="utf-8",
            )
            app.force_rerun = True
            app.cfg = {
                "speedtree_exe": "SpeedTree.exe",
                "fbx_ini": str(
                    root / "speedtree_bone_weight_repair"
                    / "presets" / "speedtree_10_1"
                    / "Options_MA_Fbx.ini"
                ),
                "blender_exe": str(root / "blender.exe"),
                "blender_parallel_jobs": 1,
                "blender_job_timeout": 3600,
                "speedtree_material_preflight_timeout": 900,
                "cluster_unit_probe": str(root / "unit.json"),
                "cluster_capture_resolution": 1024,
            }
            speedtree_cli = (
                Path(app.cfg["fbx_ini"]).resolve().parents[2]
                / "speedtree_cli.py"
            )
            speedtree_cli.parent.mkdir(parents=True, exist_ok=True)
            speedtree_cli.write_text("# test", encoding="utf-8")
            commands = []

            def fake_run(cmd, log_name, _timeout, **_kwargs):
                del _kwargs
                commands.append([str(value) for value in cmd])
                report = Path(cmd[cmd.index("--report") + 1])
                report.parent.mkdir(parents=True, exist_ok=True)
                if any(
                    str(value).endswith("speedtree_material_preflight.py")
                    for value in cmd
                ):
                    payload = {"status": "ok"}
                else:
                    payload = {
                        "status": "ok",
                        "cluster_source_build_contract": {
                            "status": "ready",
                        },
                        "handoff_preflight": {
                            "status": "cluster_export_pending",
                        },
                    }
                report.write_text(json.dumps(payload), encoding="utf-8")
                return 0, root / log_name

            def fake_relation(*_args, **_kwargs):
                pipeline_report.write_text(
                    json.dumps({
                        "handoff_preflight": {"status": "ok"},
                        "source_review_required": False,
                    }),
                    encoding="utf-8",
                )
                return {"status": "ok"}

            app._run_limited = fake_run
            app._refresh_stale_cluster_receipt = mock.Mock(
                return_value=None
            )
            app._active_blender_planned_cluster_producers_by_owner = {
                gui.normalized_folder_key(target): (
                    spm,
                    other_producer,
                )
            }
            app._leaf_reference_ready = mock.Mock(return_value=(True, "ok"))
            app._handoff_ready = mock.Mock(return_value=(True, "ready"))
            app._blend_status_text = mock.Mock(return_value="latest")
            app._write_repair_runtime_receipt = mock.Mock()
            item = {
                "spm": spm,
                "manual_bones_locked": False,
                "wind_override": "auto",
                # Dependency provenance contains both the editable source and
                # the final output.  Only the final SK output is a relationship
                # mutation target.
                "referenced_by_spms": (source_target, spm, target, target),
            }

            with mock.patch(
                "spm_audit.audit_spm", return_value={}
            ), mock.patch(
                "spm_audit.sk_readiness", return_value={"ready": True}
            ), mock.patch(
                "cluster_blend_sync.run_cluster_relation_transaction",
                side_effect=fake_relation,
            ) as relation, mock.patch.object(gui, "save_state"):
                app._job_blender(str(spm), spm, item)

            bwr_command = next(
                command
                for command in commands
                if any(
                    value.endswith("bwr_headless_job.py")
                    for value in command
                )
            )
            self.assertIn("--cluster-source-build-only", bwr_command)
            relation.assert_called_once()
            self.assertEqual(
                relation.call_args.args[1],
                [target.absolute()],
            )
            refresh_kwargs = next(
                call.kwargs
                for call in (
                    app._refresh_stale_cluster_receipt.call_args_list
                )
                if call.kwargs.get("producer_spm")
            )
            self.assertEqual(
                refresh_kwargs["producer_spm"],
                spm.resolve(),
            )
            self.assertEqual(
                tuple(refresh_kwargs["allowed_producer_spms"]),
                (spm, other_producer),
            )

    def test_relation_off_cluster_runs_standalone_final_handoff(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            owner = root / "Tree_blackgum"
            cluster = owner / "Cluster"
            cluster.mkdir(parents=True)
            spm = cluster / "SK_cluster_blackgum_01.spm"
            write_empty_spm(spm)
            app.force_rerun = True
            app.cfg = {
                "speedtree_exe": "SpeedTree.exe",
                "fbx_ini": str(
                    root / "speedtree_bone_weight_repair"
                    / "presets" / "speedtree_10_1"
                    / "Options_MA_Fbx.ini"
                ),
                "blender_exe": str(root / "blender.exe"),
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
            commands = []

            def fake_run(cmd, log_name, _timeout, **_kwargs):
                del _kwargs
                commands.append([str(value) for value in cmd])
                report = Path(cmd[cmd.index("--report") + 1])
                report.parent.mkdir(parents=True, exist_ok=True)
                if any(
                    str(value).endswith("speedtree_material_preflight.py")
                    for value in cmd
                ):
                    payload = {"status": "ok"}
                else:
                    payload = {
                        "status": "ok",
                        "warnings": [],
                        "cluster_source_build_contract": {
                            "status": "not_applicable",
                        },
                        "handoff_preflight": {"status": "ok"},
                        "source_review_required": False,
                    }
                report.write_text(
                    json.dumps(payload),
                    encoding="utf-8",
                )
                return 0, root / log_name

            app._run_limited = fake_run
            app._leaf_reference_ready = mock.Mock(
                return_value=(True, "ok")
            )
            app._handoff_ready = mock.Mock(
                return_value=(True, "ready")
            )
            app._blend_status_text = mock.Mock(return_value="latest")
            app._write_repair_runtime_receipt = mock.Mock()
            item = {
                "spm": spm,
                "manual_bones_locked": False,
                "wind_override": "auto",
                "referenced_by_spms": (
                    owner / "SK_Tree_blackgum_01.spm",
                ),
            }

            with mock.patch(
                "spm_audit.audit_spm", return_value={}
            ), mock.patch(
                "spm_audit.sk_readiness",
                return_value={"ready": True},
            ), mock.patch(
                "cluster_blend_sync.run_cluster_relation_transaction",
            ) as relation, mock.patch.object(
                gui, "LOG_DIR", root / "logs"
            ), mock.patch.object(gui, "save_state"):
                app._job_blender(str(spm), spm, item)

            bwr_command = next(
                command
                for command in commands
                if any(
                    value.endswith("bwr_headless_job.py")
                    for value in command
                )
            )
            self.assertNotIn("--cluster-source-build-only", bwr_command)
            relation.assert_not_called()
            job_report = next(
                (root / "logs").glob(
                    "SK_cluster_blackgum_01_bwr_*.json"
                )
            )
            persisted = json.loads(
                job_report.read_text(encoding="utf-8")
            )
            self.assertEqual(
                persisted["cluster_relation_sync"],
                {
                    "status": "pass_through",
                    "reason": "no_explicit_owner_relation",
                    "targets": [],
                    "repair_mode": "standalone_final_handoff",
                },
            )

    def test_cluster_normalizer_failure_keeps_committed_raw_source(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            owner = root / "Tree_elm"
            cluster = owner / "Cluster"
            cluster.mkdir(parents=True)
            spm = cluster / "SK_branch_elm_01.spm"
            target = owner / "SK_Tree_elm_01.spm"
            write_empty_spm(spm)
            write_empty_spm(target)
            blend = spm.with_suffix(".blend")
            blend.write_bytes(b"old-normalized-blend")
            from atlas_target_registry import save_target_registry

            save_target_registry(blend, [target])
            reports = cluster / "reports"
            reports.mkdir()
            pipeline_report = reports / (
                "SK_branch_elm_01_"
                "speedtree_repair_pipeline_report_codex.json"
            )
            previous_pipeline = b'{"handoff_preflight":{"status":"ok"}}'
            pipeline_report.write_bytes(previous_pipeline)
            committed_pipeline = (
                b'{"status":"done","handoff_preflight":'
                b'{"status":"cluster_export_pending"}}'
            )
            runtime_receipt = app._repair_runtime_receipt_path(spm)
            previous_runtime = b'{"code":{"producer":"old"}}'
            runtime_receipt.write_bytes(previous_runtime)
            app.force_rerun = True
            app.cfg = {
                "speedtree_exe": "SpeedTree.exe",
                "fbx_ini": str(
                    root / "speedtree_bone_weight_repair"
                    / "presets" / "speedtree_10_1"
                    / "Options_MA_Fbx.ini"
                ),
                "blender_exe": str(root / "blender.exe"),
                "blender_parallel_jobs": 1,
                "blender_job_timeout": 3600,
                "speedtree_material_preflight_timeout": 900,
                "cluster_unit_probe": str(root / "unit.json"),
                "cluster_capture_resolution": 1024,
            }
            speedtree_cli = (
                Path(app.cfg["fbx_ini"]).resolve().parents[2]
                / "speedtree_cli.py"
            )
            speedtree_cli.parent.mkdir(parents=True, exist_ok=True)
            speedtree_cli.write_text("# test", encoding="utf-8")

            def fake_run(cmd, log_name, _timeout, **_kwargs):
                del _kwargs
                report = Path(cmd[cmd.index("--report") + 1])
                report.parent.mkdir(parents=True, exist_ok=True)
                if any(
                    str(value).endswith("speedtree_material_preflight.py")
                    for value in cmd
                ):
                    payload = {"status": "ok"}
                else:
                    blend.write_bytes(b"new-partial-blend")
                    pipeline_report.write_bytes(committed_pipeline)
                    payload = {
                        "status": "ok",
                        "cluster_source_build_contract": {
                            "status": "ready",
                        },
                        "handoff_preflight": {
                            "status": "cluster_export_pending",
                        },
                    }
                report.write_text(json.dumps(payload), encoding="utf-8")
                return 0, root / log_name

            def fake_relation(*_args, **_kwargs):
                raise RuntimeError("atlas transaction failed")

            app._run_limited = fake_run
            app._refresh_stale_cluster_receipt = mock.Mock(
                return_value=None
            )
            app._leaf_reference_ready = mock.Mock(return_value=(True, "ok"))
            app._handoff_ready = mock.Mock(return_value=(False, "stale"))
            app._blend_status_text = mock.Mock(return_value="stale")
            app._write_repair_runtime_receipt = mock.Mock()
            item = {
                "spm": spm,
                "manual_bones_locked": False,
                "wind_override": "auto",
                "referenced_by_spms": (target,),
            }

            with mock.patch(
                "spm_audit.audit_spm", return_value={}
            ), mock.patch(
                "spm_audit.sk_readiness", return_value={"ready": True}
            ), mock.patch(
                "cluster_blend_sync.run_cluster_relation_transaction",
                side_effect=fake_relation,
            ), mock.patch.object(gui, "save_state"):
                with self.assertRaises(gui.BatchItemError):
                    app._job_blender(str(spm), spm, item)

            self.assertEqual(blend.read_bytes(), b"new-partial-blend")
            self.assertEqual(pipeline_report.read_bytes(), committed_pipeline)
            self.assertEqual(runtime_receipt.read_bytes(), previous_runtime)

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
                    '{"status":"blocked","error":"preflight blocked"}',
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


    def test_cluster_relation_targets_map_unprefixed_owner_source(self):
        gui = load_gui_module()
        with tempfile.TemporaryDirectory() as temporary:
            owner = Path(temporary) / "Tree_elm"
            cluster = owner / "Cluster"
            cluster.mkdir(parents=True)
            cluster_spm = cluster / "SK_branch_elm_01.spm"
            cluster_spm.touch()
            cluster_blend = cluster_spm.with_suffix(".blend")
            cluster_blend.touch()
            raw_02 = owner / "Tree_elm_02.spm"
            canonical_02 = owner / "SK_Tree_elm_02.spm"
            canonical_01 = owner / "SK_Tree_elm_01.spm"
            missing_raw = owner / "Tree_elm_03.spm"
            external = Path(temporary) / "Other" / "Tree_elm_04.spm"
            for path in (raw_02, canonical_02, canonical_01):
                path.touch()
            external.parent.mkdir()
            external.touch()
            from atlas_target_registry import save_target_registry

            save_target_registry(
                cluster_blend,
                [raw_02, canonical_01],
            )

            targets = gui.cluster_relation_output_targets(
                cluster_spm,
                [
                    raw_02,
                    canonical_02,
                    canonical_01,
                    external,
                    missing_raw,
                ],
            )

            self.assertEqual(
                targets,
                [canonical_02.absolute(), canonical_01.absolute()],
            )

    def test_cluster_relation_targets_ignore_references_when_registry_is_off(
        self,
    ):
        gui = load_gui_module()
        with tempfile.TemporaryDirectory() as temporary:
            owner = Path(temporary) / "Tree_raw"
            cluster = owner / "Cluster"
            cluster.mkdir(parents=True)
            source = cluster / "SK_cluster_raw_01.spm"
            target = owner / "SK_Tree_raw_01.spm"
            source.touch()
            target.touch()

            self.assertEqual(
                gui.cluster_relation_output_targets(source, [target]),
                [],
            )

    def test_current_cluster_bwr_refreshes_only_stale_relation(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        with tempfile.TemporaryDirectory() as temporary:
            owner = Path(temporary) / "Tree_elm"
            cluster = owner / "Cluster"
            cluster.mkdir(parents=True)
            spm = cluster / "SK_branch_elm_01.spm"
            target = owner / "SK_Tree_elm_01.spm"
            blend = spm.with_suffix(".blend")
            for path in (spm, target, blend):
                path.touch()
            from atlas_target_registry import save_target_registry

            save_target_registry(blend, [target])
            blender = Path(temporary) / "blender.exe"
            unit_probe = Path(temporary) / "unit.json"
            blender.touch()
            unit_probe.touch()
            app.force_rerun = False
            app.cfg = {
                "blender_exe": str(blender),
                "cluster_unit_probe": str(unit_probe),
                "cluster_capture_resolution": 1024,
                "blender_job_timeout": 30,
            }
            app._leaf_reference_ready = mock.Mock(
                return_value=(True, "ok")
            )
            app._repair_output_state = mock.Mock(return_value={
                "current": True,
                "push_ready": True,
                "kind": "current",
                "reason": "",
            })
            app._record_live_blend_status = mock.Mock()
            app._run_limited = mock.Mock(
                side_effect=AssertionError("BWR must not run")
            )
            app._refresh_stale_cluster_receipt = mock.Mock(
                return_value=None
            )
            states = [
                {
                    "current": False,
                    "reason": (
                        "SK_Tree_elm_01.spm:refresh_required"
                        "(target_scope_changed)"
                    ),
                    "targets": [],
                },
                {"current": True, "reason": "", "targets": []},
            ]
            item = {
                "spm": spm,
                "wind_override": "auto",
                "referenced_by_spms": (target,),
            }

            with mock.patch.object(
                gui,
                "cluster_relation_refresh_state",
                side_effect=states,
            ) as refresh_state, mock.patch(
                "cluster_blend_sync.run_cluster_relation_transaction",
                return_value={"status": "ok"},
            ) as relation:
                app._job_blender(str(spm), spm, item)

            self.assertEqual(refresh_state.call_count, 2)
            relation.assert_called_once()
            self.assertEqual(relation.call_args.args[1], [target.absolute()])
            app._run_limited.assert_not_called()


class ClusterBarkRepairSkipGateTests(unittest.TestCase):
    @staticmethod
    def artifact(path, digest):
        return {
            "path": str(path),
            "exists": True,
            "size": 10,
            "sha256": digest,
        }

    def test_matching_isolated_bark_capture_can_keep_current_blend(self):
        gui = load_gui_module()
        root = Path("C:/contract")
        source = root / "SK_leaf_elm_01.spm"
        isolated = root / "isolated" / "SK_leaf_elm_01.spm"
        manifest = root / "isolated" / "bark_normalization_manifest.json"
        fingerprints = {
            str(source): self.artifact(source, "source"),
            str(isolated): self.artifact(isolated, "isolated"),
            str(manifest): self.artifact(manifest, "manifest"),
        }
        resolution = {
            "status": "cached",
            "source_spm": str(source),
            "speedtree_spm": str(isolated),
            "manifest": str(manifest),
            "normalization": {"canonical_material": "M_bark_elm_01"},
        }
        pipeline = {
            "cluster_bark_source_resolution": {
                "status": "ready",
                "manifest": fingerprints[str(manifest)],
                "source_spm": fingerprints[str(source)],
                "speedtree_spm": fingerprints[str(isolated)],
                "canonical_material": "M_bark_elm_01",
            },
            "cluster_bark_export_validation": {
                "status": "ready_for_downstream_blender_mapping",
                "production_sources_mutated": False,
            },
        }

        self.assertTrue(
            gui.cluster_bark_pipeline_matches_resolution(
                source,
                resolution,
                pipeline,
                lambda path: fingerprints[str(path)],
            )
        )

    def test_cached_source_without_bwr_capture_invalidates_current_blend(self):
        gui = load_gui_module()
        resolution = {
            "status": "cached",
            "source_spm": "C:/contract/SK_leaf_elm_01.spm",
            "speedtree_spm": "C:/contract/isolated/SK_leaf_elm_01.spm",
            "manifest": (
                "C:/contract/isolated/bark_normalization_manifest.json"
            ),
            "normalization": {"canonical_material": "M_bark_elm_01"},
        }

        self.assertFalse(
            gui.cluster_bark_pipeline_matches_resolution(
                resolution["source_spm"],
                resolution,
                {},
                lambda path: self.artifact(path, "current"),
            )
        )

    def test_live_audit_policy_is_authoritative_even_with_cache(self):
        gui = load_gui_module()
        self.assertTrue(
            gui.cluster_receipt_resolution_uses_live_audit({
                "policy": "live_audit_authoritative",
                "live_audit_report": "C:/reports/live.json",
                "persisted_receipt": "C:/reports/cache.json",
            })
        )
        self.assertFalse(
            gui.cluster_receipt_resolution_uses_live_audit({
                "policy": "newest_hash_current_receipt",
                "selected_receipt": "C:/reports/cache.json",
            })
        )


if __name__ == "__main__":
    unittest.main()
