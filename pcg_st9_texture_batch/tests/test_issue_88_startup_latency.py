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
            self.assertEqual(payload["status"], "complete")
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

            def install(_cfg, session):
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

    def test_relation_calculation_honors_cancellation(self):
        with mock.patch.object(
            GUI, "BLENDER_RELATION_CACHE_PATH", self.cache_path
        ):
            with self.assertRaisesRegex(RuntimeError, "cancelled"):
                GUI.cache_blender_connection_rows(
                    {"items": [self.item()]},
                    cancel_check=lambda: True,
                )

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


class ProductionShapedLatencyFixtureTests(unittest.TestCase):
    def test_cold_and_warm_primary_audit_have_explicit_latency_budgets(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "Tree"
            cache = Path(temporary) / "cache"
            root.mkdir()
            for index in range(24):
                folder = root / f"tree_fixture_{index:02d}"
                folder.mkdir()
                write_minimal_spm(
                    folder / f"SK_tree_fixture_{index:02d}.spm",
                    marker=f"fixture_{index:02d}",
                )
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

            self.assertEqual(cold["summary"]["total"], 24)
            self.assertEqual(warm["summary"]["total"], 24)
            self.assertEqual(
                legacy_inspection.call_count,
                48,
                "each refresh must inspect current legacy evidence once per folder",
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


if __name__ == "__main__":
    unittest.main()
