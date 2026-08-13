import json
import os
import queue
import sys
import tempfile
import threading
import unittest
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path
from unittest import mock


SK_BATCH_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SK_BATCH_DIR))

from blender_resume_receipt import (
    BlenderResumeReceiptError,
    build_blender_resume_receipt,
    validate_blender_resume_receipt,
)


def load_gui_module():
    path = SK_BATCH_DIR / "sk_batch_gui.pyw"
    loader = SourceFileLoader("sk_batch_gui_blender_resume_test", str(path))
    spec = spec_from_loader(loader.name, loader)
    module = module_from_spec(spec)
    loader.exec_module(module)
    return module


class BlenderResumeReceiptTests(unittest.TestCase):
    @staticmethod
    def write(path, payload=b"x"):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        return path

    def test_receipt_reuses_only_unchanged_bound_files_and_settings(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            spm = self.write(root / "SK_tree.spm", b"spm")
            blend = self.write(root / "SK_tree.blend", b"blend")
            settings = {"wind_override": "TREE"}
            state = {
                "current": True,
                "push_ready": True,
                "kind": "ready",
                "reason": "ready",
            }
            receipt = build_blender_resume_receipt(
                spm,
                tracked_paths=[blend],
                settings=settings,
                repair_state=state,
            )

            self.assertEqual(
                validate_blender_resume_receipt(
                    json.loads(json.dumps(receipt)),
                    spm,
                    settings=settings,
                ),
                state,
            )

            blend.write_bytes(b"changed blend")
            with self.assertRaisesRegex(
                BlenderResumeReceiptError,
                "changed",
            ) as caught:
                validate_blender_resume_receipt(
                    receipt,
                    spm,
                    settings=settings,
                )
            self.assertEqual(
                caught.exception.resume_action,
                "rebuild_required",
            )

    def test_receipt_rejects_force_relevant_setting_drift(self):
        with tempfile.TemporaryDirectory() as temporary:
            spm = self.write(Path(temporary) / "SK_tree.spm")
            receipt = build_blender_resume_receipt(
                spm,
                tracked_paths=[],
                settings={"wind_override": "TREE"},
                repair_state={
                    "current": True,
                    "push_ready": True,
                    "kind": "ready",
                    "reason": "ready",
                },
            )

            with self.assertRaisesRegex(
                BlenderResumeReceiptError,
                "settings changed",
            ) as caught:
                validate_blender_resume_receipt(
                    receipt,
                    spm,
                    settings={"wind_override": "BUSH"},
                )
            self.assertEqual(
                caught.exception.resume_action,
                "rebuild_required",
            )

    def test_receipt_invalidates_when_a_bound_missing_artifact_appears(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            spm = self.write(root / "SK_tree.spm")
            optional = root / "T_tree_color.tga"
            settings = {"wind_override": "TREE"}
            receipt = build_blender_resume_receipt(
                spm,
                tracked_paths=[optional],
                settings=settings,
                repair_state={
                    "current": True,
                    "push_ready": True,
                    "kind": "ready",
                    "reason": "ready",
                },
            )

            self.write(optional, b"new texture")

            with self.assertRaisesRegex(
                BlenderResumeReceiptError,
                "changed",
            ):
                validate_blender_resume_receipt(
                    receipt,
                    spm,
                    settings=settings,
                )

    def test_relation_file_drift_requests_live_validation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            spm = self.write(root / "SK_tree.spm")
            dependency = self.write(root / "SK_branch.spm", b"old")
            settings = {"wind_override": "TREE"}
            relation_signature = {
                "status": "current",
                "contract_sha256": "same-contract",
            }
            receipt = build_blender_resume_receipt(
                spm,
                tracked_paths=[],
                relation_paths=[dependency],
                relation_signature=relation_signature,
                settings=settings,
                repair_state={
                    "current": True,
                    "push_ready": True,
                    "kind": "ready",
                    "reason": "ready",
                },
            )

            dependency.write_bytes(b"new relationship")

            with self.assertRaises(BlenderResumeReceiptError) as caught:
                validate_blender_resume_receipt(
                    receipt,
                    spm,
                    settings=settings,
                    relation_signature=relation_signature,
                )
            self.assertEqual(
                caught.exception.resume_action,
                "relation_changed",
            )

    def test_relation_signature_drift_requests_live_validation(self):
        with tempfile.TemporaryDirectory() as temporary:
            spm = self.write(Path(temporary) / "SK_tree.spm")
            settings = {"wind_override": "TREE"}
            receipt = build_blender_resume_receipt(
                spm,
                tracked_paths=[],
                relation_signature={"status": "absent"},
                settings=settings,
                repair_state={
                    "current": True,
                    "push_ready": True,
                    "kind": "ready",
                    "reason": "ready",
                },
            )

            with self.assertRaises(BlenderResumeReceiptError) as caught:
                validate_blender_resume_receipt(
                    receipt,
                    spm,
                    settings=settings,
                    relation_signature={
                        "status": "current",
                        "contract_sha256": "new-contract",
                    },
                )
            self.assertEqual(
                caught.exception.resume_action,
                "relation_changed",
            )

    def test_core_role_wins_when_relation_list_repeats_same_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            spm = self.write(root / "SK_tree.spm")
            blend = self.write(root / "SK_tree.blend", b"old")
            settings = {"wind_override": "TREE"}
            receipt = build_blender_resume_receipt(
                spm,
                tracked_paths=[blend],
                relation_paths=[blend],
                settings=settings,
                repair_state={
                    "current": True,
                    "push_ready": True,
                    "kind": "ready",
                    "reason": "ready",
                },
            )

            blend.write_bytes(b"new output")

            with self.assertRaises(BlenderResumeReceiptError) as caught:
                validate_blender_resume_receipt(
                    receipt,
                    spm,
                    settings=settings,
                )
            self.assertEqual(
                caught.exception.resume_action,
                "rebuild_required",
            )


class BlenderResumeQueueTests(unittest.TestCase):
    @staticmethod
    def make_app(gui):
        app = gui.App.__new__(gui.App)
        app.stop_flag = threading.Event()
        app.ui_queue = queue.Queue()
        app.state = {}
        app.state_lock = threading.RLock()
        app.procs_lock = threading.Lock()
        app.active_procs = set()
        app.cfg = {"blender_parallel_jobs": 1}
        app.log = mock.Mock()
        app.force_rerun = False
        app.items = {}
        app._active_batch_items = None
        app._active_batch_inventory = None
        return app

    def test_completed_receipts_are_removed_before_worker_progress(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            completed = root / "Tree_a" / "SK_tree_a.spm"
            pending = root / "Tree_b" / "SK_tree_b.spm"
            targets = [
                {"spm": completed, "wind_override": "auto"},
                {"spm": pending, "wind_override": "auto"},
            ]
            app.items = {
                str(item["spm"]): item for item in targets
            }
            app._validated_blender_resume_state = mock.Mock(
                side_effect=[
                    {
                        "current": True,
                        "push_ready": True,
                        "kind": "ready",
                        "reason": "ready",
                    },
                    BlenderResumeReceiptError("missing"),
                ]
            )
            app._publish_current_repair_skip = mock.Mock(return_value=True)
            app._job_blender = mock.Mock()

            with mock.patch.object(
                gui,
                "LOG_DIR",
                root / "logs",
            ), mock.patch.object(gui, "save_state"):
                result = app._run_batch(
                    "blender",
                    targets,
                    emit_done=False,
                )

        self.assertTrue(result)
        app._job_blender.assert_called_once_with(
            str(pending),
            pending,
            targets[1],
        )
        app._publish_current_repair_skip.assert_called_once_with(
            str(completed),
            completed,
            mock.ANY,
            validated_resume_receipt=None,
        )
        progress_events = [
            payload
            for event, payload in list(app.ui_queue.queue)
            if event == "batch_progress"
        ]
        self.assertEqual(progress_events[0], (0, 1))
        self.assertEqual(progress_events[-1], (1, 1))

    def test_force_rerun_bypasses_resume_prefilter(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        app.force_rerun = True
        target = {"spm": Path("SK_tree.spm")}
        app._validated_blender_resume_state = mock.Mock()

        runnable, skipped = app._prefilter_blender_resume_targets([target])

        self.assertEqual(runnable, [target])
        self.assertEqual(skipped, [])
        app._validated_blender_resume_state.assert_not_called()

    def test_prefilter_routes_receipt_drift_without_failing_rows(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        relation_item = {"spm": Path("SK_relation.spm")}
        core_item = {"spm": Path("SK_core.spm")}
        app._validated_blender_resume_state = mock.Mock(
            side_effect=[
                BlenderResumeReceiptError(
                    "relationship changed",
                    resume_action="relation_changed",
                ),
                BlenderResumeReceiptError(
                    "SPM changed",
                    resume_action="rebuild_required",
                ),
            ]
        )

        runnable, skipped = app._prefilter_blender_resume_targets(
            [relation_item, core_item]
        )

        self.assertEqual(runnable, [relation_item, core_item])
        self.assertEqual(skipped, [])
        self.assertEqual(
            relation_item["_blender_resume_policy"],
            "live_validation",
        )
        self.assertEqual(core_item["_blender_resume_policy"], "rebuild")

    def test_owner_with_cluster_needs_live_audit_before_fast_skip(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        with tempfile.TemporaryDirectory() as temporary:
            owner = Path(temporary) / "Tree_elm"
            (owner / "Cluster").mkdir(parents=True)
            spm = owner / "SK_tree_elm_01.spm"
            spm.write_bytes(b"spm")
            app._cluster_receipt_refresh_input_fingerprint = mock.Mock(
                side_effect=AssertionError(
                    "resume check must not hash owner SPM contents"
                )
            )
            signature = app._blender_resume_relation_signature(
                spm,
                item={"referenced_by_spms": ()},
            )
            with self.assertRaises(BlenderResumeReceiptError) as caught:
                app._build_blender_resume_receipt(
                    str(spm),
                    spm,
                    {
                        "current": True,
                        "push_ready": True,
                        "kind": "ready",
                        "reason": "ready",
                    },
                    item={"spm": spm, "referenced_by_spms": ()},
                )

        self.assertEqual(signature, {"status": "tracked", "relations": []})
        self.assertEqual(
            caught.exception.resume_action,
            "relation_changed",
        )
        app._cluster_receipt_refresh_input_fingerprint.assert_not_called()

    def test_cluster_provider_target_set_is_part_of_skip_signature(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        from atlas_target_registry import save_target_registry

        with tempfile.TemporaryDirectory() as temporary:
            owner = Path(temporary) / "Tree_elm"
            cluster = owner / "Cluster"
            cluster.mkdir(parents=True)
            spm = cluster / "SK_branch_elm_01.spm"
            blend = spm.with_suffix(".blend")
            target_a = owner / "SK_tree_elm_01.spm"
            target_b = owner / "SK_tree_elm_02.spm"
            for path in (spm, blend, target_a, target_b):
                path.write_bytes(b"x")
            save_target_registry(blend, [target_a, target_b])

            first = app._blender_resume_relation_signature(
                spm,
                item={"referenced_by_spms": (target_a,)},
            )
            second = app._blender_resume_relation_signature(
                spm,
                item={"referenced_by_spms": (target_a, target_b)},
            )

        self.assertEqual(first["status"], "tracked")
        self.assertEqual(
            len(first["relations"][0]["registered_targets"]),
            1,
        )
        self.assertEqual(
            len(second["relations"][0]["registered_targets"]),
            2,
        )
        self.assertNotEqual(first, second)

    def test_valid_resume_check_never_reads_spm_or_blend_contents(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "Tree_elm"
            root.mkdir(parents=True)
            spm = root / "SK_tree_elm_01.spm"
            blend = spm.with_suffix(".blend")
            spm.write_bytes(b"large SPM placeholder")
            blend.write_bytes(b"large Blend placeholder")
            item = {
                "spm": spm,
                "wind_override": "auto",
                "manual_bones_locked": False,
                "referenced_by_spms": (),
            }
            app.items = {str(spm): item}
            settings = app._blender_resume_settings(str(spm), item=item)
            relation_signature = app._blender_resume_relation_signature(
                spm,
                item=item,
            )
            repair_state = {
                "current": True,
                "push_ready": True,
                "kind": "ready",
                "reason": "ready",
            }
            receipt = build_blender_resume_receipt(
                spm,
                tracked_paths=[blend],
                relation_signature=relation_signature,
                settings=settings,
                repair_state=repair_state,
            )

            with mock.patch.object(
                Path,
                "read_bytes",
                side_effect=AssertionError("fast resume must not read bytes"),
            ), mock.patch.object(
                Path,
                "read_text",
                side_effect=AssertionError("fast resume must not parse files"),
            ), mock.patch.object(
                gui,
                "cluster_assembly_receipt_resolution",
                side_effect=AssertionError(
                    "fast resume must not scan global relation receipts"
                ),
            ):
                validated = app._validated_blender_resume_state(
                    str(spm),
                    spm,
                    item,
                    receipt,
                )

        self.assertEqual(validated, repair_state)

    def test_second_identical_run_schedules_zero_blender_workers(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            spm = root / "Tree" / "SK_tree.spm"
            target = {"spm": spm, "wind_override": "auto"}
            app.items = {str(spm): target}
            receipt = {"receipt_sha256": "saved"}
            app.state[str(spm)] = {
                "blend_resume_receipt": receipt,
            }
            app._validated_blender_resume_state = mock.Mock(return_value={
                "current": True,
                "push_ready": True,
                "kind": "ready",
                "reason": "ready",
            })
            app._publish_current_repair_skip = mock.Mock(return_value=True)
            app._job_blender = mock.Mock(
                side_effect=AssertionError("completed row must not get a worker")
            )

            with mock.patch.object(
                gui,
                "LOG_DIR",
                root / "logs",
            ), mock.patch.object(gui, "save_state"):
                result = app._run_batch(
                    "blender",
                    [target],
                    emit_done=False,
                )

        self.assertTrue(result)
        app._job_blender.assert_not_called()
        app._publish_current_repair_skip.assert_called_once_with(
            str(spm),
            spm,
            mock.ANY,
            validated_resume_receipt=receipt,
        )
        progress_events = [
            payload
            for event, payload in list(app.ui_queue.queue)
            if event == "batch_progress"
        ]
        self.assertEqual(progress_events, [(0, 0)])

    def test_missing_receipt_migrates_from_current_stat_state_without_reads(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "Tree_elm"
            root.mkdir(parents=True)
            spm = root / "SK_tree_elm_01.spm"
            blend = spm.with_suffix(".blend")
            report = gui.repair_pipeline_report_path(spm)
            wind = (
                root
                / "JSON"
                / f"{spm.stem}_dynamic_wind_import_from_megaplant_groups.json"
            )
            for path in (spm, blend, report, wind):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"x")
            source_time = 1_700_000_000_000_000_000
            output_time = source_time + 1_000_000_000
            os.utime(spm, ns=(source_time, source_time))
            for path in (blend, report, wind):
                os.utime(path, ns=(output_time, output_time))
            iid = str(spm)
            item = {
                "spm": spm,
                "wind_override": "auto",
                "manual_bones_locked": False,
                "referenced_by_spms": (),
            }
            app.items = {iid: item}
            app.state[iid] = {
                "blend_status": "최신 ✓",
                "blend_status_kind": "ok",
                "live_texture_paths": [],
            }
            app._repair_output_state = mock.Mock(
                side_effect=AssertionError("stat migration must not audit SPM")
            )
            app._publish_current_repair_skip = mock.Mock(return_value=True)

            with mock.patch.object(
                Path,
                "read_bytes",
                side_effect=AssertionError("stat migration must not read bytes"),
            ), mock.patch.object(
                Path,
                "read_text",
                side_effect=AssertionError("stat migration must not parse files"),
            ), mock.patch.object(
                gui,
                "cluster_assembly_receipt_resolution",
                side_effect=AssertionError(
                    "stat migration must not scan global receipts"
                ),
            ):
                runnable, skipped = app._prefilter_blender_resume_targets(
                    [item]
                )

        self.assertEqual(runnable, [])
        self.assertEqual(skipped, [item])
        app._repair_output_state.assert_not_called()
        published = app._publish_current_repair_skip.call_args
        self.assertEqual(published.args[:2], (iid, spm))
        self.assertIsInstance(
            published.kwargs["validated_resume_receipt"],
            dict,
        )

    def test_unchanged_live_poll_never_parses_spm_to_migrate_receipt(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        spm = Path("SK_tree_existing.spm")
        iid = str(spm)
        signature = ((1, 2),)
        app._scan_generation = 3
        app._live_poll_active = True
        app.items = {iid: {"spm": spm}}
        app.state = {iid: {}}
        app._repair_output_state = mock.Mock(
            side_effect=AssertionError("unchanged poll must not parse SPM")
        )
        snapshot = [
            (iid, spm, (), signature, None, {"wind_override": "auto"})
        ]

        with mock.patch.object(
            gui.App,
            "_live_status_signature",
            return_value=signature,
        ), mock.patch.object(gui, "save_state"), mock.patch.object(
            gui,
            "save_leaf_contract_cache",
        ):
            app._poll_live_file_status_worker(3, snapshot)

        app._repair_output_state.assert_not_called()
        self.assertNotIn("blend_resume_receipt", app.state[iid])
        self.assertNotIn("blend_resume_receipt", app.items[iid])


if __name__ == "__main__":
    unittest.main()
