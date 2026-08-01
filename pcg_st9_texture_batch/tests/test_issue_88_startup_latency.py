"""Issue #88 regressions for bounded, usable-ready PCG startup."""
from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock


REPO_DIR = Path(__file__).resolve().parents[2]
TOOL_DIR = REPO_DIR / "pcg_st9_texture_batch"
sys.path.insert(0, str(REPO_DIR))
sys.path.insert(0, str(TOOL_DIR))

import pcg_texture_audit as audit  # noqa: E402
import pcg_board_snapshot as board  # noqa: E402
from blend_source_index import lookup_blend_source_images  # noqa: E402
from pcg_startup_cache import (  # noqa: E402
    BoundedDiscoveryError,
    ContentAddressedJsonCache,
    bounded_recursive_files,
)
from pcg_startup_latency import (  # noqa: E402
    PRODUCTION_FIXTURE_LATENCY_BUDGET_SECONDS,
    StartupLatencyTracker,
)


def load_gui_module():
    name = "_pcg_issue88_gui"
    existing = sys.modules.get(name)
    if existing is not None:
        return existing
    path = TOOL_DIR / "pcg_texture_gui.pyw"
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    loader.exec_module(module)
    return module


GUI = load_gui_module()


def write_minimal_spm(path, marker="one"):
    Path(path).write_text(
        '<?xml version="1.0"?><SpeedTree><Materials>'
        f'<Material_v8 ID="1" Name="M_{marker}" />'
        '</Materials><Generators></Generators><Nodes></Nodes></SpeedTree>',
        encoding="utf-8",
    )


class StartupLatencyReceiptTests(unittest.TestCase):
    def test_phase_receipt_records_selection_wall_time_counts_and_budgets(self):
        with tempfile.TemporaryDirectory() as temporary:
            ticks = iter((10.1, 10.4, 11.0, 11.3, 11.5))
            receipt = Path(temporary) / "latency.json"
            tracker = StartupLatencyTracker(
                selected_perf=10.0,
                clock=lambda: next(ticks),
                receipt_path=receipt,
                source_provenance={
                    "path": "C:/repo/pcg_texture_gui.pyw",
                    "sha256": "a" * 64,
                    "process_id": 123,
                },
            )
            tracker.mark(
                "cached_board_paint",
                counts={"row_count": 24},
            )
            tracker.mark(
                "primary_live_audit",
                counts={"folder_count": 24},
            )
            tracker.mark(
                "blender_relations",
                counts={"cache_hits": 20, "cache_misses": 4},
            )
            tracker.mark(
                "sync_migration",
                counts={"entry_count": 144},
            )

            payload = json.loads(receipt.read_text(encoding="utf-8"))
            self.assertEqual(payload["schema_version"], 2)
            self.assertEqual(payload["status"], "complete")
            self.assertEqual(
                payload["source_provenance"]["sha256"], "a" * 64
            )
            self.assertEqual(
                payload["milestones"]["tab_selected"][
                    "from_tab_selection_seconds"
                ],
                0.0,
            )
            self.assertEqual(
                [row["phase"] for row in payload["phases"]],
                [
                    "cached_board_paint",
                    "primary_live_audit",
                    "blender_relations",
                    "sync_migration",
                ],
            )
            self.assertEqual(payload["phases"][0]["counts"]["row_count"], 24)
            self.assertIn("budget_seconds", payload["phases"][0])
            self.assertAlmostEqual(
                payload["phases"][-1]["from_tab_selection_seconds"],
                1.5,
            )

    def test_cached_sync_tail_still_completes_startup_receipt(self):
        with tempfile.TemporaryDirectory() as temporary:
            ticks = iter((1.05, 1.1, 1.2, 1.3, 1.4))
            receipt = Path(temporary) / "latency.json"
            tracker = StartupLatencyTracker(
                selected_perf=1.0,
                clock=lambda: next(ticks),
                receipt_path=receipt,
            )
            tracker.mark("cached_board_paint", status="painted")
            tracker.mark("primary_live_audit")
            tracker.mark("blender_relations")
            tracker.mark("sync_migration", status="cached")

            payload = json.loads(receipt.read_text(encoding="utf-8"))
            self.assertEqual(payload["status"], "complete")
            self.assertEqual(payload["phases"][-1]["status"], "cached")


class ContentIdentityCacheTests(unittest.TestCase):
    def test_corrupt_cached_value_is_never_authority(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "cache.json"
            cache = ContentAddressedJsonCache(path, "test-cache")
            cache.put("row", "a" * 64, {"safe": True})
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["entries"]["row"]["value"] = {"safe": False}
            path.write_text(json.dumps(payload), encoding="utf-8")

            self.assertIsNone(cache.get("row", "a" * 64))

    def test_same_size_restored_mtime_invalidates_cache_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "input.spm"
            source.write_bytes(b"AAAA")
            original = source.stat()
            cache = ContentAddressedJsonCache(
                root / "cache.json", "test-cache"
            )
            first = audit.content_identity([source])
            cache.put("row", first["sha256"], {"value": 1})

            source.write_bytes(b"BBBB")
            os.utime(
                source,
                ns=(original.st_atime_ns, original.st_mtime_ns),
            )
            second = audit.content_identity([source])

            self.assertNotEqual(first["sha256"], second["sha256"])
            self.assertIsNone(cache.get("row", second["sha256"]))

    def test_recursive_discovery_is_bounded_and_fail_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for index in range(4):
                folder = root / f"nested_{index}"
                folder.mkdir()
                (folder / f"graph_{index}.sbs").write_text(
                    "<package />", encoding="utf-8"
                )
            with self.assertRaises(BoundedDiscoveryError):
                bounded_recursive_files(
                    [root], suffix=".sbs", max_directories=2, max_files=20
                )

    def test_spm_semantic_cache_rejects_same_size_restored_mtime_tamper(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "SK_tree.spm"
            cache_path = root / "spm.json"
            source.write_text(
                '<?xml version="1.0"?><SpeedTree><Materials>'
                '<Material_v8 ID="1" Name="M_one">'
                '<TexFilename Value="C:\\\\leaf_color.tga" />'
                '</Material_v8></Materials><Generators></Generators>'
                '<Nodes></Nodes></SpeedTree>',
                encoding="utf-8",
            )
            original = source.stat()

            with mock.patch.object(
                audit, "SPM_ANALYSIS_CACHE_PATH", cache_path
            ):
                audit._SPM_ANALYSIS_CACHE.clear()
                audit._PERSISTENT_SPM_ANALYSIS = None
                first = audit._spm_analysis(source)
                audit.save_spm_analysis_cache()

                replacement = source.read_text(encoding="utf-8").replace(
                    "M_one", "M_two"
                )
                self.assertEqual(
                    len(replacement.encode("utf-8")), original.st_size
                )
                source.write_text(replacement, encoding="utf-8")
                os.utime(
                    source,
                    ns=(original.st_atime_ns, original.st_mtime_ns),
                )
                audit._SPM_ANALYSIS_CACHE.clear()
                audit._PERSISTENT_SPM_ANALYSIS = None
                second = audit._spm_analysis(source)

            self.assertEqual(first["material_names"], ["M_one"])
            self.assertEqual(second["material_names"], ["M_two"])

    def test_spm_semantic_cache_detects_tamper_between_sample_windows(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "SK_large.spm"
            cache_path = root / "spm.json"
            prefix = (
                '<?xml version="1.0"?><SpeedTree><!--'
                + "x" * (100 * 1024)
                + '--><Materials><Material_v8 ID="1" Name="M_one">'
                '<TexFilename Value="C:\\\\leaf_color.tga" />'
                '</Material_v8></Materials><Generators></Generators>'
                '<Nodes></Nodes><!--'
            )
            source.write_text(
                prefix + "y" * (1024 * 1024) + "--></SpeedTree>",
                encoding="utf-8",
            )
            original = source.stat()
            with mock.patch.object(
                audit, "SPM_ANALYSIS_CACHE_PATH", cache_path
            ):
                audit._SPM_ANALYSIS_CACHE.clear()
                audit._PERSISTENT_SPM_ANALYSIS = None
                first = audit._spm_analysis(source)
                audit.save_spm_analysis_cache()
                changed = source.read_text(encoding="utf-8").replace(
                    "M_one", "M_two"
                )
                source.write_text(changed, encoding="utf-8")
                os.utime(
                    source,
                    ns=(original.st_atime_ns, original.st_mtime_ns),
                )
                audit._SPM_ANALYSIS_CACHE.clear()
                audit._PERSISTENT_SPM_ANALYSIS = None
                second = audit._spm_analysis(source)

            self.assertEqual(first["material_names"], ["M_one"])
            self.assertEqual(second["material_names"], ["M_two"])

    def test_cold_spm_decode_is_handed_to_slot_inspection_and_persisted(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "SK_tree.spm"
            source.write_text(
                '<?xml version="1.0"?><SpeedTree><Materials>'
                '<Material_v8 ID="1" Name="M_leaf">'
                '<TexFilename Value="C:\\\\leaf_color.tga" />'
                '</Material_v8></Materials><Generators></Generators>'
                '<Nodes></Nodes></SpeedTree>',
                encoding="utf-8",
            )
            cache_path = root / "spm.json"
            with mock.patch.object(
                audit, "SPM_ANALYSIS_CACHE_PATH", cache_path
            ), mock.patch.object(
                audit,
                "read_maybe_gzip_text",
                wraps=audit.read_maybe_gzip_text,
            ) as decode, mock.patch.object(
                audit,
                "inspect_spm_texture_slots_from_text",
                wraps=audit.inspect_spm_texture_slots_from_text,
            ) as inspect_text:
                audit._SPM_ANALYSIS_CACHE.clear()
                audit._PERSISTENT_SPM_ANALYSIS = None
                token = audit._REPORT_SCAN_CACHE.set(
                    audit._new_report_scan_cache()
                )
                try:
                    audit._spm_analysis(source)
                    cold_slots = audit._cached_spm_texture_slots(source)
                finally:
                    audit._REPORT_SCAN_CACHE.reset(token)
                audit.save_spm_analysis_cache()
                audit._SPM_ANALYSIS_CACHE.clear()
                audit._PERSISTENT_SPM_ANALYSIS = None
                warm_slots = audit._cached_spm_texture_slots(source)

            self.assertEqual(
                decode.call_count,
                0,
                "the stable full-SHA bytes must feed semantic decode directly",
            )
            self.assertEqual(inspect_text.call_count, 1)
            self.assertEqual(warm_slots, cold_slots)

    def test_canceled_generation_does_not_publish_analysis_cache(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "SK_tree.spm"
            cache_path = root / "spm.json"
            write_minimal_spm(source)
            with mock.patch.object(
                audit, "SPM_ANALYSIS_CACHE_PATH", cache_path
            ):
                audit._SPM_ANALYSIS_CACHE.clear()
                audit._PERSISTENT_SPM_ANALYSIS = None
                audit._PERSISTENT_SPM_ANALYSIS_DIRTY = False
                audit._spm_analysis(source)
                written = audit.save_spm_analysis_cache(
                    publish_check=lambda: False
                )

            self.assertIsNone(written)
            self.assertFalse(cache_path.exists())
            self.assertTrue(audit._PERSISTENT_SPM_ANALYSIS_DIRTY)


class ProviderAndAuditTests(unittest.TestCase):
    def test_provider_discovery_is_pair_inventory_cold_and_content_cached_warm(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "Tree"
            cluster = root / "tree_elm" / "Cluster"
            cluster.mkdir(parents=True)
            provider = cluster / "SK_cluster_elm_01.spm"
            provider.write_bytes(b"AAAA")
            cache_path = Path(temporary) / "provider-cache.json"
            original = provider.stat()

            with mock.patch.object(
                audit, "PROVIDER_MAP_CACHE_PATH", cache_path
            ):
                cold_metrics = {}
                cold = audit.canonical_cluster_provider_map(
                    root, metrics=cold_metrics
                )
                warm_metrics = {}
                warm = audit.canonical_cluster_provider_map(
                    root, metrics=warm_metrics
                )
                provider.write_bytes(b"BBBB")
                os.utime(
                    provider,
                    ns=(original.st_atime_ns, original.st_mtime_ns),
                )
                invalidated_metrics = {}
                audit.canonical_cluster_provider_map(
                    root, metrics=invalidated_metrics
                )

            owner_key = str(provider.parent.parent.resolve()).casefold()
            self.assertEqual(cold[owner_key], [provider.resolve()])
            self.assertEqual(warm, cold)
            self.assertFalse(cold_metrics["cache_hit"])
            self.assertEqual(
                cold_metrics["discovery_strategy"],
                "bounded_canonical_pair_inventory",
            )
            self.assertTrue(warm_metrics["cache_hit"])
            self.assertFalse(invalidated_metrics["cache_hit"])

    def test_blend_index_miss_reaudits_only_affected_folder(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            folders = [root / "tree_a", root / "tree_b"]
            for folder in folders:
                folder.mkdir()
            blend = folders[0] / "leaf.blend"
            blend.write_bytes(b"blend-current")
            calls = []

            def audit_folder(folder, _cfg, **_kwargs):
                calls.append(Path(folder).name)
                if Path(folder) == folders[0]:
                    lookup_blend_source_images(blend, {})
                return {
                    "folder": str(folder),
                    "name": Path(folder).name,
                    "cluster_items": [],
                    "status": "ready",
                    "actions": [],
                }

            def install(_cfg, session, **kwargs):
                requests = session.pending_requests()
                session.install_report(
                    {
                        "schema_version": 1,
                        "status": "ok",
                        "rows": [
                            {
                                "schema_version": 1,
                                "status": "ok",
                                "indexed_by_blender": True,
                                "blend": request["blend"],
                                "blend_sha256": request["blend_sha256"],
                                "images": [],
                            }
                            for request in requests
                        ],
                    },
                    requests,
                )
                metrics = kwargs.get("metrics")
                if metrics is not None:
                    metrics.update({
                        "request_count": len(requests),
                        "cache_hit": False,
                        "status": "ok",
                    })

            cfg = {
                "tree_root": str(root),
                "source_texture_roots": [],
                "required_export_maps": [],
            }
            with mock.patch.object(
                audit, "candidate_folders", return_value=folders
            ), mock.patch.object(
                audit, "canonical_cluster_provider_map", return_value={}
            ), mock.patch.object(
                audit, "audit_folder", side_effect=audit_folder
            ), mock.patch.object(
                audit, "ensure_blend_source_index", side_effect=install
            ), mock.patch.object(
                audit, "attach_global_m_graphs", return_value={}
            ), mock.patch.object(
                audit, "resolve_shared_atlas_entries", return_value=[]
            ), mock.patch.object(
                audit, "refresh_texture_output_contract_states",
                return_value=None,
            ):
                report = audit.make_report(cfg)

            self.assertEqual(calls.count("tree_a"), 2)
            self.assertEqual(calls.count("tree_b"), 1)
            timing = report["startup_timing"]
            self.assertEqual(timing["revalidated_folder_count"], 1)
            bounded = next(
                row for row in timing["phases"]
                if row["phase"] == "bounded_folder_revalidation"
            )
            self.assertFalse(bounded["counts"]["full_fleet_repeat"])

    def test_blend_index_child_is_terminated_when_generation_is_canceled(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            blend = root / "leaf.blend"
            blend.write_bytes(b"blend")
            session = audit.BlendSourceIndexSession({})
            session.lookup(blend)
            metrics = {}

            class FakeChild:
                pid = 43210
                returncode = None

                def poll(self):
                    return self.returncode

                def communicate(self, timeout=None):
                    return "", ""

            child = FakeChild()

            def terminate(process):
                self.assertIs(process, child)
                process.returncode = -9

            with mock.patch.object(
                audit,
                "BLEND_IMAGE_CACHE_PATH",
                root / "cache" / "blend.json",
            ), mock.patch.object(
                audit.subprocess, "Popen", return_value=child
            ), mock.patch.object(
                audit,
                "_terminate_owned_process_tree",
                side_effect=terminate,
            ) as terminate_tree:
                with self.assertRaisesRegex(
                    audit.BlendSourceIndexError, "cancelled"
                ):
                    audit.ensure_blend_source_index(
                        {"blender_exe": sys.executable},
                        session,
                        cancel_check=lambda: True,
                        metrics=metrics,
                    )

            terminate_tree.assert_called_once_with(child)
            self.assertEqual(metrics["status"], "canceled")
            self.assertTrue(metrics["child_tree_terminated"])
            self.assertEqual(metrics["request_count"], 1)
            self.assertEqual(
                list((root / "cache").glob(".blend_source_index_*.json")),
                [],
            )


class RelationAndAuthorityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.cache_path = self.root / "relations.json"
        self.folder = self.root / "tree_elm"
        self.folder.mkdir()
        self.spm = self.folder / "SK_tree_elm.spm"
        self.spm.write_bytes(b"AAAA")

    def tearDown(self):
        self.temporary.cleanup()

    def item(self):
        return {
            "folder": str(self.folder),
            "name": "tree_elm",
            "chosen_spm": str(self.spm),
            "leaf_mesh_sources": [],
            "leaf_atlas_inventory": [],
            "cluster_items": [],
            "target_spm_statuses": [],
        }

    def test_relation_cache_is_warm_and_stale_content_fails_closed(self):
        rows = [{"blend": self.folder / "leaf.blend", "spms": []}]
        original = self.spm.stat()
        with mock.patch.object(
            GUI, "BLENDER_RELATION_CACHE_PATH", self.cache_path
        ), mock.patch.object(
            GUI, "blender_connection_rows", return_value=rows
        ) as calculate:
            cold_item = self.item()
            cold_metrics = {}
            GUI.cache_blender_connection_rows(
                {"items": [cold_item]}, metrics=cold_metrics
            )
            warm_item = self.item()
            warm_metrics = {}
            GUI.cache_blender_connection_rows(
                {"items": [warm_item]}, metrics=warm_metrics
            )

            self.assertEqual(calculate.call_count, 1)
            self.assertEqual(cold_metrics["cache_misses"], 1)
            self.assertEqual(warm_metrics["cache_hits"], 1)
            self.assertTrue(GUI.item_has_current_live_evidence(warm_item))

            self.spm.write_bytes(b"BBBB")
            os.utime(
                self.spm,
                ns=(original.st_atime_ns, original.st_mtime_ns),
            )
            self.assertFalse(GUI.item_has_current_live_evidence(warm_item))
            with self.assertRaises(RuntimeError):
                GUI.require_current_live_evidence(warm_item)

            invalidated_item = self.item()
            GUI.cache_blender_connection_rows({"items": [invalidated_item]})
            self.assertEqual(calculate.call_count, 2)

    def test_mutation_evidence_detects_large_unsampled_restored_mtime_tamper(self):
        self.spm.write_bytes(b"A" * (2 * 1024 * 1024))
        original = self.spm.stat()
        item = self.item()
        metrics = {}
        with mock.patch.object(
            GUI, "BLENDER_RELATION_CACHE_PATH", self.cache_path
        ), mock.patch.object(
            GUI, "blender_connection_rows", return_value=[]
        ):
            GUI.cache_blender_connection_rows(
                {"items": [item]}, metrics=metrics
            )

        changed = bytearray(self.spm.read_bytes())
        # This offset is outside the bounded 8x64KiB sample windows for a
        # 2MiB file; mutation authority must still reject it.
        changed[100 * 1024] = ord("B")
        self.spm.write_bytes(changed)
        os.utime(
            self.spm,
            ns=(original.st_atime_ns, original.st_mtime_ns),
        )

        self.assertEqual(
            metrics["content_identity_algorithm"],
            "sha256-of-full-content-keys-v1",
        )
        self.assertFalse(GUI.item_has_current_live_evidence(item))
        with self.assertRaises(RuntimeError):
            GUI.require_current_live_evidence(item)

    def test_relation_calculation_honors_cancellation(self):
        with mock.patch.object(
            GUI, "BLENDER_RELATION_CACHE_PATH", self.cache_path
        ):
            with self.assertRaisesRegex(RuntimeError, "cancelled"):
                GUI.cache_blender_connection_rows(
                    {"items": [self.item()]},
                    cancel_check=lambda: True,
                )

    def test_relation_failure_receipt_keeps_counts_and_fails_closed(self):
        metrics = {}
        original = self.spm.stat()

        def change_during_relation(_item):
            self.spm.write_bytes(b"BBBB")
            os.utime(
                self.spm,
                ns=(original.st_atime_ns, original.st_mtime_ns),
            )
            return []

        with mock.patch.object(
            GUI, "BLENDER_RELATION_CACHE_PATH", self.cache_path
        ), mock.patch.object(
            GUI,
            "blender_connection_rows",
            side_effect=change_during_relation,
        ):
            with self.assertRaisesRegex(RuntimeError, "inputs changed"):
                GUI.cache_blender_connection_rows(
                    {"items": [self.item()]}, metrics=metrics
                )

        self.assertEqual(metrics["status"], "changed_during_scan")
        self.assertEqual(metrics["item_count"], 1)
        self.assertEqual(metrics["cache_misses"], 1)
        self.assertEqual(metrics["changed_during_scan"], 1)
        self.assertGreater(metrics["unique_content_file_count"], 0)
        self.assertFalse(self.cache_path.exists())

    def test_relation_first_pass_deduplicates_shared_content_and_directories(self):
        first = self.item()
        second = self.item()
        second["name"] = "tree_elm_variant"
        metrics = {}
        with mock.patch.object(
            GUI, "BLENDER_RELATION_CACHE_PATH", self.cache_path
        ), mock.patch.object(
            GUI, "blender_connection_rows", return_value=[]
        ):
            GUI.cache_blender_connection_rows(
                {"items": [first, second]}, metrics=metrics
            )

        self.assertEqual(metrics["status"], "ok")
        self.assertEqual(metrics["item_count"], 2)
        self.assertGreater(
            metrics["content_file_count"],
            metrics["first_pass_physical_content_reads"],
        )
        self.assertEqual(metrics["first_pass_directory_enumerations"], 1)

    def test_refresh_generation_rejects_stale_callbacks(self):
        app = GUI.App.__new__(GUI.App)
        first_generation, first_cancel = app._begin_refresh_generation()
        second_generation, second_cancel = app._begin_refresh_generation()

        self.assertTrue(first_cancel.is_set())
        self.assertFalse(
            app._refresh_generation_is_current(
                first_generation, first_cancel
            )
        )
        self.assertTrue(
            app._refresh_generation_is_current(
                second_generation, second_cancel
            )
        )

    def test_refresh_requests_coalesce_while_initial_audit_is_running(self):
        app = GUI.App.__new__(GUI.App)
        app._sync_state_migrating = False
        app._initial_refreshing = True
        app._manual_refreshing = False
        app._busy = True
        app._pending_refresh = False
        app.status_var = mock.Mock()

        first = app.refresh()
        second = app.refresh()

        self.assertIs(first, False)
        self.assertIs(second, False)
        self.assertTrue(app._pending_refresh)
        self.assertEqual(app.status_var.set.call_count, 2)

    def test_relation_and_sync_migration_workers_start_without_serial_wait(self):
        app = GUI.App.__new__(GUI.App)
        generation, cancel = app._begin_refresh_generation()
        app.root = mock.Mock()
        app.status_var = mock.Mock()
        app.startup_latency = mock.Mock()
        app.texplan_cache = {}
        app.texplan_errors = {}
        app.sync_state = {"entries": {}}
        app.sync_migration_worker = None
        app._sync_state_migrating = False
        app._initial_refreshing = True
        app._initial_relation_finished = False
        app._initial_relation_ready = False
        app._pending_initial_sync_result = None
        app.populate = mock.Mock()
        app._update_summary = mock.Mock()
        app._set_busy = mock.Mock()
        app._lock_mutation_controls = mock.Mock()
        created = []

        class CapturedThread:
            def __init__(self, target, daemon=True):
                self.target = target
                self.daemon = daemon
                self.started = False
                created.append(self)

            def start(self):
                self.started = True

            def is_alive(self):
                return self.started

        report = {
            "items": [self.item()],
            "startup_timing": {"wall_seconds": 1.0, "phases": []},
        }
        with mock.patch.object(GUI.threading, "Thread", CapturedThread):
            app._initial_primary_refresh_done(
                report,
                {},
                None,
                generation=generation,
                cancel_event=cancel,
            )

        self.assertEqual(len(created), 2)
        self.assertTrue(all(worker.started for worker in created))
        self.assertIs(app.worker, created[0])
        self.assertIs(app.sync_migration_worker, created[1])

    def test_early_sync_result_coalesces_with_relation_tree_projection(self):
        app = GUI.App.__new__(GUI.App)
        state = {"entries": {"one": {}}, "migration_complete": True}
        app.worker = mock.Mock()
        app.status_var = mock.Mock()
        app.startup_latency = mock.Mock()
        app.texplan_cache = {}
        app.texplan_errors = {}
        app.sync_state = {"entries": {}}
        app._initial_refreshing = True
        app._initial_relation_finished = False
        app._initial_relation_ready = False
        app._pending_initial_sync_result = (state, None, None)
        app._pending_refresh = False
        app.populate = mock.Mock()
        app._update_summary = mock.Mock()
        app._set_busy = mock.Mock()
        app.log = mock.Mock()
        report = {"items": [self.item()]}

        app._initial_refresh_done(
            report,
            stage="relation",
            relation_metrics={"item_count": 1},
        )

        self.assertIs(app.sync_state, state)
        app.populate.assert_called_once_with()
        phase_names = [
            call.args[0] for call in app.startup_latency.mark.call_args_list
        ]
        self.assertEqual(
            phase_names, ["blender_relations", "sync_migration"]
        )
        sync_view = next(
            call for call in app.startup_latency.milestone.call_args_list
            if call.args[0] == "sync_view_applied"
        )
        self.assertTrue(
            sync_view.kwargs["details"]["coalesced_with_relation_view"]
        )

    def test_sync_migration_failure_keeps_step3_fail_closed(self):
        app = GUI.App.__new__(GUI.App)
        app._initial_relation_ready = True
        app._pending_refresh_after_sync_migration = False
        app.startup_latency = mock.Mock()
        app.status_var = mock.Mock()
        app.log = mock.Mock()
        app._update_step3_button = mock.Mock()

        app._apply_sync_state_migration(
            None, RuntimeError("receipt verification failed")
        )

        self.assertTrue(app._sync_state_migration_failed)
        app._update_step3_button.assert_called_once_with()
        self.assertFalse(any(
            call.args[0] == "usable_ready_all"
            for call in app.startup_latency.milestone.call_args_list
        ))

    def test_first_cold_live_row_unlocks_read_only_review(self):
        app = GUI.App.__new__(GUI.App)
        generation, cancel = app._begin_refresh_generation()
        app._had_initial_snapshot = False
        app.populate = mock.Mock()
        app._update_summary = mock.Mock()
        app._set_busy = mock.Mock()
        app._lock_mutation_controls = mock.Mock()
        app.status_var = mock.Mock()
        item = {
            "folder": str(self.folder),
            "name": "tree_elm",
            "status": "ready",
        }

        app._initial_partial_live_item(
            item,
            generation=generation,
            cancel_event=cancel,
        )

        self.assertTrue(app.report["partial_live_audit"])
        self.assertTrue(app._display_only_snapshot)
        app.populate.assert_called_once_with()
        app._set_busy.assert_called_once_with(False)
        app._lock_mutation_controls.assert_called_once_with()

    def test_primary_memoization_persists_before_relation_failure(self):
        app = GUI.App.__new__(GUI.App)
        generation, cancel = app._begin_refresh_generation()
        app.root = mock.Mock()
        app.root.after.side_effect = lambda _delay, callback: callback()
        app.status_var = mock.Mock()
        app.startup_latency = mock.Mock()
        app.texplan_cache = {}
        app.texplan_errors = {}
        app.populate = mock.Mock()
        app._update_summary = mock.Mock()
        app._set_busy = mock.Mock()
        app._lock_mutation_controls = mock.Mock()
        app.log = mock.Mock()
        app._initial_refreshing = True
        app._initial_relation_finished = False
        app._initial_relation_ready = False
        app._pending_initial_sync_result = None
        app.sync_state = {"entries": {}}
        app.sync_migration_worker = None
        report = {
            "items": [self.item()],
            "startup_timing": {"wall_seconds": 1.0, "phases": []},
        }
        order = []

        class ImmediateThread:
            def __init__(self, target, daemon=True):
                self.target = target
                self.daemon = daemon

            def start(self):
                self.target()

        def fail_relation(*_args, **_kwargs):
            order.append("relation")
            raise RuntimeError("changed during relation")

        with mock.patch.object(
            GUI,
            "save_spm_analysis_cache",
            side_effect=lambda **_kwargs: order.append("cache") or [
                self.root / "spm.json"
            ],
        ), mock.patch.object(
            GUI,
            "write_board_display_snapshot",
            side_effect=lambda *_args, **_kwargs:
                order.append("projection") or self.root / "board.json",
        ), mock.patch.object(
            GUI,
            "cache_blender_connection_rows",
            side_effect=fail_relation,
        ), mock.patch.object(
            GUI.threading, "Thread", ImmediateThread
        ), mock.patch.object(
            GUI.messagebox, "showerror"
        ), mock.patch.object(
            GUI, "record_exception", return_value=False
        ):
            app._initial_primary_refresh_done(
                report,
                {},
                None,
                generation=generation,
                cancel_event=cancel,
            )

        self.assertEqual(order, ["cache", "projection", "relation"])
        self.assertTrue(app._display_only_snapshot)
        app._lock_mutation_controls.assert_called()


class ProductionShapedLatencyFixtureTests(unittest.TestCase):
    def test_55_folder_597_spm_primary_paint_and_usable_latency_budgets(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "Tree"
            cache = Path(temporary) / "cache"
            root.mkdir()
            spm_count = 0
            for index in range(55):
                folder = root / f"tree_fixture_{index:02d}"
                folder.mkdir()
                # 55 real production folders currently contain 597 SPMs:
                # 47 folders with 11, then 8 folders with 10.
                folder_spm_count = 11 if index < 47 else 10
                for spm_index in range(folder_spm_count):
                    write_minimal_spm(
                        folder / (
                            f"SK_tree_fixture_{index:02d}_{spm_index:02d}.spm"
                        ),
                        marker=f"fixture_{index:02d}_{spm_index:02d}",
                    )
                    spm_count += 1
            cfg = {
                "tree_root": str(root),
                "atlas_root": str(root / "atlas"),
                "source_texture_roots": [],
                "required_export_maps": [
                    "color", "normal", "extra", "height",
                    "opacity", "subsurface",
                ],
            }
            cache_patches = (
                mock.patch.object(
                    audit, "SPM_ANALYSIS_CACHE_PATH", cache / "spm.json"
                ),
                mock.patch.object(
                    audit, "SBS_GRAPH_CACHE_PATH", cache / "sbs.json"
                ),
                mock.patch.object(
                    audit, "BLEND_IMAGE_CACHE_PATH", cache / "blend.json"
                ),
                mock.patch.object(
                    audit, "PROVIDER_MAP_CACHE_PATH", cache / "provider.json"
                ),
            )
            with cache_patches[0], cache_patches[1], cache_patches[2], cache_patches[3]:
                audit._SPM_ANALYSIS_CACHE.clear()
                audit._PERSISTENT_SPM_ANALYSIS = None
                audit._PERSISTENT_SBS_GRAPHS = None
                audit._PERSISTENT_BLEND_IMAGES = None
                with mock.patch.object(
                    audit,
                    "inspect_legacy_cluster_state",
                    wraps=audit.inspect_legacy_cluster_state,
                ) as legacy_inspection:
                    cold_started = time.perf_counter()
                    cold = audit.make_report(cfg)
                    cold_elapsed = time.perf_counter() - cold_started
                    audit.save_spm_analysis_cache()
                    warm_started = time.perf_counter()
                    warm = audit.make_report(cfg)
                    warm_elapsed = time.perf_counter() - warm_started

            self.assertEqual(spm_count, 597)
            self.assertEqual(cold["summary"]["total"], 55)
            self.assertEqual(warm["summary"]["total"], 55)
            self.assertEqual(
                legacy_inspection.call_count,
                1194,
                "each refresh must inspect current legacy evidence once per SPM",
            )
            self.assertLess(
                cold_elapsed,
                PRODUCTION_FIXTURE_LATENCY_BUDGET_SECONDS["cold_total"],
            )
            self.assertLess(
                warm_elapsed,
                PRODUCTION_FIXTURE_LATENCY_BUDGET_SECONDS["warm_total"],
            )
            self.assertFalse(
                cold["startup_timing"]["provider_metrics"]["cache_hit"]
            )
            self.assertTrue(
                warm["startup_timing"]["provider_metrics"]["cache_hit"]
            )
            self.assertEqual(
                {
                    key: value for key, value in cold.items()
                    if key not in {"generated_at", "startup_timing"}
                },
                {
                    key: value for key, value in warm.items()
                    if key not in {"generated_at", "startup_timing"}
                },
                "warm memoization must preserve the full semantic report",
            )
            cold_cache = cold["startup_timing"]["session_cache_metrics"]
            warm_cache = warm["startup_timing"]["session_cache_metrics"]
            self.assertEqual(
                cold_cache.get("spm_decode_misses_unique_files"), 597
            )
            self.assertEqual(
                warm_cache.get("spm_memory_hits_unique_files"), 597
            )

            snapshot_path = cache / "board.json"
            snapshot_metrics = {}
            written = board.write_board_display_snapshot(
                warm,
                cfg,
                path=snapshot_path,
                metrics=snapshot_metrics,
            )
            self.assertEqual(written, snapshot_path)
            paint_started = time.perf_counter()
            displayed = board.read_board_display_snapshot(
                cfg, path=snapshot_path
            )
            paint_elapsed = time.perf_counter() - paint_started
            self.assertTrue(displayed["can_display"])
            self.assertEqual(
                len(displayed["display_report"]["items"]), 55
            )
            self.assertLess(
                paint_elapsed,
                PRODUCTION_FIXTURE_LATENCY_BUDGET_SECONDS[
                    "cached_board_paint"
                ],
            )

            relation_path = cache / "relations.json"
            with mock.patch.object(
                GUI, "BLENDER_RELATION_CACHE_PATH", relation_path
            ), mock.patch.object(
                GUI, "blender_connection_rows", return_value=[]
            ):
                cold_relation_metrics = {}
                cold_relation_started = time.perf_counter()
                GUI.cache_blender_connection_rows(
                    cold, metrics=cold_relation_metrics
                )
                cold_relation_elapsed = (
                    time.perf_counter() - cold_relation_started
                )
                warm_relation_metrics = {}
                warm_relation_started = time.perf_counter()
                GUI.cache_blender_connection_rows(
                    warm, metrics=warm_relation_metrics
                )
                warm_relation_elapsed = (
                    time.perf_counter() - warm_relation_started
                )

            self.assertEqual(cold_relation_metrics["cache_misses"], 55)
            self.assertEqual(warm_relation_metrics["cache_hits"], 55)
            self.assertEqual(
                warm_relation_metrics["content_identity_algorithm"],
                "sha256-of-full-content-keys-v1",
            )
            self.assertLess(
                cold_elapsed + cold_relation_elapsed,
                PRODUCTION_FIXTURE_LATENCY_BUDGET_SECONDS[
                    "cold_usable_ready"
                ],
            )
            self.assertLess(
                warm_elapsed + warm_relation_elapsed,
                PRODUCTION_FIXTURE_LATENCY_BUDGET_SECONDS[
                    "warm_usable_ready"
                ],
            )


if __name__ == "__main__":
    unittest.main()
