"""Issue #88 regressions for bounded, usable-ready PCG startup."""
from __future__ import annotations

import importlib.machinery
import importlib.util
import copy
import hashlib
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock


REPO_DIR = Path(__file__).resolve().parents[2]
TOOL_DIR = REPO_DIR / "pcg_st9_texture_batch"
sys.path.insert(0, str(REPO_DIR))
sys.path.insert(0, str(TOOL_DIR))

import pcg_texture_audit as audit  # noqa: E402
import pcg_board_snapshot as board  # noqa: E402
import cluster_blend_sync as cluster_sync  # noqa: E402
import pcg_cluster_assembly_contract as assembly  # noqa: E402
import speedtree_texture_contract as texture_contract  # noqa: E402
import mutation_plan_authority as mutation_authority  # noqa: E402
from artifact_content_key import ConcurrentContentDigestMemo  # noqa: E402
from blend_source_index import lookup_blend_source_images  # noqa: E402
from pcg_startup_cache import (  # noqa: E402
    BoundedDiscoveryError,
    ContentAddressedJsonCache,
    bounded_recursive_files,
    canonical_json_sha256,
)
from pcg_startup_latency import (  # noqa: E402
    PRODUCTION_FIXTURE_LATENCY_BUDGET_SECONDS,
    StartupAmplificationError,
    StartupLatencyTracker,
    USABLE_READY_ACCEPTANCE_CAP_SECONDS,
    require_startup_total_invocation_guard,
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
    def test_mutation_authority_spm_analysis_ignores_memory_and_disk_rows(self):
        with tempfile.TemporaryDirectory() as temporary:
            spm = Path(temporary) / "SK_tree.spm"
            spm.write_text(
                '<?xml version="1.0"?><SpeedTree><Materials>'
                '<Material_v8 ID="1" Name="M_current"></Material_v8>'
                '</Materials><Generators></Generators><Nodes></Nodes>'
                '</SpeedTree>',
                encoding="utf-8",
            )
            cache_path = Path(temporary) / "spm-cache.json"

            @audit._report_scan_cached
            def scan(*, mutation_authority=False):
                return copy.deepcopy(audit._spm_analysis(spm))

            with mock.patch.object(
                audit, "SPM_ANALYSIS_CACHE_PATH", cache_path
            ):
                audit._SPM_ANALYSIS_CACHE.clear()
                audit._PERSISTENT_SPM_ANALYSIS = {}
                normal = scan()
                self.assertIn("M_current", normal["material_names"])
                for row in audit._SPM_ANALYSIS_CACHE.values():
                    row["material_names"] = ["M_poison"]
                for row in audit._PERSISTENT_SPM_ANALYSIS.values():
                    row["material_names"] = ["M_poison"]

                authoritative = scan(mutation_authority=True)

            self.assertIn("M_current", authoritative["material_names"])
            self.assertNotIn("M_poison", authoritative["material_names"])

    def test_refresh_digest_memo_single_flights_shared_physical_reads(self):
        memo = ConcurrentContentDigestMemo()
        started = threading.Event()
        release = threading.Event()
        calls = []

        def compute():
            calls.append(threading.get_ident())
            started.set()
            self.assertTrue(release.wait(timeout=5.0))
            return "a" * 64

        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = [
                executor.submit(
                    memo.get_or_compute,
                    ("c:/shared.fbx", 1024, 7),
                    compute,
                )
                for _index in range(8)
            ]
            self.assertTrue(started.wait(timeout=5.0))
            release.set()
            results = [future.result(timeout=5.0) for future in futures]

        self.assertEqual(results, ["a" * 64] * 8)
        self.assertEqual(len(calls), 1)
        self.assertEqual(memo.metrics()["misses"], 1)
        self.assertGreaterEqual(memo.metrics()["waits"], 1)

    def test_refresh_digest_memo_rejects_pending_seed_conflict(self):
        memo = ConcurrentContentDigestMemo()
        started = threading.Event()
        release = threading.Event()
        key = ("c:/shared.fbx", 4, 7)

        def compute():
            started.set()
            self.assertTrue(release.wait(timeout=5.0))
            return "a" * 64

        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(memo.get_or_compute, key, compute)
            self.assertTrue(started.wait(timeout=5.0))
            memo.seed(key, "b" * 64)
            release.set()
            with self.assertRaisesRegex(ValueError, "Conflicting exact"):
                future.result(timeout=5.0)

        self.assertNotIn(key, memo)

    def test_authority_digest_rejects_restored_mtime_replacement(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "source.fbx"
            path.write_bytes(b"AAAA")
            original = path.stat()
            memo = ConcurrentContentDigestMemo()
            first = texture_contract._file_sha256(path, memo=memo)
            self.assertEqual(first, hashlib.sha256(b"AAAA").hexdigest())

            path.write_bytes(b"BBBB")
            os.utime(
                path,
                ns=(original.st_atime_ns, original.st_mtime_ns),
            )
            with self.assertRaisesRegex(OSError, "identity change"):
                texture_contract._file_sha256(path, memo=memo)

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

    def test_mutation_authority_provider_pass_bypasses_valid_poison(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "Tree"
            cluster = root / "tree_elm" / "Cluster"
            cluster.mkdir(parents=True)
            provider = cluster / "SK_cluster_elm_01.spm"
            provider.write_bytes(b"AAAA")
            cache_path = Path(temporary) / "provider-cache.json"
            with mock.patch.object(
                audit, "PROVIDER_MAP_CACHE_PATH", cache_path
            ):
                audit.canonical_cluster_provider_map(root)
                payload = json.loads(cache_path.read_text(encoding="utf-8"))
                row = next(iter(payload["entries"].values()))
                row["value"] = {}
                row["value_sha256"] = canonical_json_sha256({})
                cache_path.write_text(
                    json.dumps(payload), encoding="utf-8"
                )
                metrics = {}
                current = audit.canonical_cluster_provider_map(
                    root,
                    metrics=metrics,
                    read_cache=False,
                )

            owner_key = str(provider.parent.parent.resolve()).casefold()
            self.assertEqual(current[owner_key], [provider.resolve()])
            self.assertFalse(metrics["cache_hit"])

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

            def terminate(process, **_kwargs):
                self.assertIs(process, child)
                process.returncode = -9

            with mock.patch.object(
                audit,
                "BLEND_IMAGE_CACHE_PATH",
                root / "cache" / "blend.json",
            ), mock.patch.object(
                audit, "owned_popen", return_value=child
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

            terminate_tree.assert_called_once_with(
                child,
                reason="blend_source_index_cancelled",
            )
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

    def test_mutation_relation_pass_bypasses_checksum_valid_poisoned_rows(self):
        poisoned = [{"blend": "C:/spoof.blend", "spms": []}]
        trusted = [{"blend": "C:/current.blend", "spms": []}]
        with mock.patch.object(
            GUI, "BLENDER_RELATION_CACHE_PATH", self.cache_path
        ), mock.patch.object(
            GUI, "blender_connection_rows", return_value=poisoned
        ):
            GUI.cache_blender_connection_rows({"items": [self.item()]})

        current = self.item()
        metrics = {}
        with mock.patch.object(
            GUI, "BLENDER_RELATION_CACHE_PATH", self.cache_path
        ), mock.patch.object(
            GUI, "blender_connection_rows", return_value=trusted
        ) as calculate:
            GUI.cache_blender_connection_rows(
                {"items": [current]},
                read_cache=False,
                verify_physical=True,
                metrics=metrics,
            )

        calculate.assert_called_once()
        self.assertEqual(
            current["_gui_blender_connection_rows"], trusted
        )
        self.assertEqual(metrics["cache_hits"], 0)
        self.assertEqual(
            metrics["persisted_cache_policy"], "fresh_projection"
        )

    def test_session_evidence_never_authorizes_stale_relation_cache_hit(self):
        original = self.spm.stat()
        first = self.item()
        stale_session = {
            "schema_version": 1,
            "kind": "pcg_refresh_exact_content_evidence",
            "exact_content_rows": {
                GUI.startup_path_key(self.spm): {
                    "path": str(self.spm),
                    "fingerprint_algorithm": "sha256-full-v1",
                    "fingerprint": hashlib.sha256(b"AAAA").hexdigest(),
                    "size": 4,
                },
            },
        }
        with mock.patch.object(
            GUI, "BLENDER_RELATION_CACHE_PATH", self.cache_path
        ), mock.patch.object(
            GUI, "blender_connection_rows", return_value=[]
        ) as calculate:
            GUI.cache_blender_connection_rows({"items": [first]})
            self.spm.write_bytes(b"BBBB")
            os.utime(
                self.spm,
                ns=(original.st_atime_ns, original.st_mtime_ns),
            )
            metrics = {}
            changed = self.item()
            GUI.cache_blender_connection_rows(
                {"items": [changed]},
                session_evidence=stale_session,
                metrics=metrics,
            )

        self.assertEqual(calculate.call_count, 2)
        self.assertEqual(metrics["cache_hits"], 0)
        self.assertEqual(metrics["cache_misses"], 1)
        self.assertEqual(metrics["reused_exact_content_file_count"], 0)
        self.assertEqual(metrics["session_exact_candidate_count"], 1)

    def test_deferred_relation_evidence_cannot_seal_mutation(self):
        item = self.item()
        with mock.patch.object(
            GUI, "BLENDER_RELATION_CACHE_PATH", self.cache_path
        ), mock.patch.object(
            GUI, "blender_connection_rows", return_value=[]
        ):
            GUI.cache_blender_connection_rows(
                {"items": [item]}, verify_physical=False
            )

        self.assertTrue(GUI.item_has_current_live_evidence(item))
        with self.assertRaisesRegex(RuntimeError, "display-only"):
            GUI.seal_exact_mutation_baseline([item], action="test")

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
        self.assertEqual(
            metrics["mutation_authority_algorithm"],
            "sha256-of-full-content-keys-v1",
        )
        self.assertFalse(GUI.item_has_current_live_evidence(item))
        with self.assertRaises(RuntimeError):
            GUI.require_current_live_evidence(item)

    def test_exact_baseline_rejects_bulk_image_off_window_tamper_before_write(self):
        image = self.folder / "leaf_color.tga"
        image.write_bytes(b"A" * (2 * 1024 * 1024))
        original = image.stat()
        item = self.item()
        item["cluster_items"] = [{"source_refs": [str(image)]}]
        with mock.patch.object(
            GUI, "BLENDER_RELATION_CACHE_PATH", self.cache_path
        ), mock.patch.object(
            GUI, "blender_connection_rows", return_value=[]
        ):
            GUI.cache_blender_connection_rows({"items": [item]})

        baseline = GUI.seal_exact_mutation_baseline(
            [item],
            action="test",
            plan_payload={"target": "leaf"},
        )
        self.assertEqual(
            item["_gui_exact_mutation_evidence"]["algorithm"],
            "sha256-of-full-content-keys-v1",
        )
        self.assertTrue(GUI.require_exact_mutation_baseline(baseline))

        changed = bytearray(image.read_bytes())
        changed[100 * 1024] = ord("B")
        image.write_bytes(changed)
        os.utime(
            image,
            ns=(original.st_atime_ns, original.st_mtime_ns),
        )

        # Startup/readiness sampling can legitimately miss this pixel.  The
        # selected-plan exact seal must still block before the first write.
        self.assertTrue(GUI.item_has_current_live_evidence(item))
        with self.assertRaisesRegex(RuntimeError, "production 파일을 쓰지"):
            GUI.require_exact_mutation_baseline(baseline)

    def test_current_semantic_reaudit_rejects_old_plan_before_exact_seal(self):
        stale = self.item()
        current = copy.deepcopy(stale)
        current["cluster_items"] = [{"texture_contract_state": "blocked"}]
        app = GUI.App.__new__(GUI.App)
        app.cfg = {}
        with mock.patch.object(
            GUI,
            "make_report",
            return_value={"items": [current]},
        ) as make_report, mock.patch.object(
            GUI,
            "cache_blender_connection_rows",
        ) as relations, mock.patch.object(
            GUI,
            "seal_exact_mutation_baseline",
        ) as seal:
            with self.assertRaisesRegex(RuntimeError, "current affected-folder"):
                app._reaudit_and_seal_mutation_items(
                    [stale],
                    action="test",
                    plan_payload={"target": "leaf"},
                )
        seal.assert_not_called()
        self.assertIs(
            make_report.call_args.kwargs["mutation_authority"], True
        )
        self.assertIs(relations.call_args.kwargs["read_cache"], False)
        self.assertIs(
            relations.call_args.kwargs["verify_physical"], True
        )

    def test_nonempty_mutation_plan_cannot_use_empty_item_authority(self):
        app = GUI.App.__new__(GUI.App)
        app.cfg = {}
        with mock.patch.object(GUI, "make_report") as make_report:
            with self.assertRaisesRegex(RuntimeError, "scope is empty"):
                app._reaudit_and_seal_mutation_items(
                    [],
                    action="atlas_target_add",
                    plan_payload={"target_spm": str(self.spm)},
                )
        make_report.assert_not_called()

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

        def change_during_relation(
            _item, _validation_cache=None, **_kwargs,
        ):
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
        (self.folder / "unreferenced.png").write_bytes(b"image")
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
        self.assertGreater(
            metrics["unique_membership_file_count"],
            metrics["unique_content_file_count"],
        )
        self.assertEqual(
            metrics["content_identity_scope"],
            "exact-relation-semantic-dependencies-plus-bounded-"
            "membership-v2",
        )

    def test_relation_projection_excludes_mutation_only_fields(self):
        original = self.item()
        changed = copy.deepcopy(original)
        changed["cluster_items"] = [{"source_refs": ["other.tga"]}]
        changed["target_spm_statuses"] = [{"status": "needs_sk"}]

        first = GUI._relation_item_content_identity(original)
        second = GUI._relation_item_content_identity(changed)

        self.assertEqual(first["sha256"], second["sha256"])
        self.assertNotEqual(
            GUI.mutation_semantic_digest(original),
            GUI.mutation_semantic_digest(changed),
        )

    def test_relation_registry_same_size_restored_mtime_invalidates_cache(self):
        blend = self.folder / "leaf.blend"
        blend.write_bytes(b"blend bytes are path-only relation evidence")
        registry = GUI.registry_path_for_blend(blend)
        registry.write_text('{"value":"AAAA"}', encoding="utf-8")
        original = registry.stat()
        item = self.item()
        item["leaf_mesh_sources"] = [{
            "atlas_blends": [str(blend)],
            "targets": [{"spm": str(self.spm)}],
        }]

        with mock.patch.object(
            GUI, "BLENDER_RELATION_CACHE_PATH", self.cache_path
        ), mock.patch.object(
            GUI, "blender_connection_rows", return_value=[]
        ) as calculate:
            GUI.cache_blender_connection_rows({"items": [copy.deepcopy(item)]})
            GUI.cache_blender_connection_rows({"items": [copy.deepcopy(item)]})
            self.assertEqual(calculate.call_count, 1)

            registry.write_text('{"value":"BBBB"}', encoding="utf-8")
            os.utime(
                registry,
                ns=(original.st_atime_ns, original.st_mtime_ns),
            )
            GUI.cache_blender_connection_rows({"items": [copy.deepcopy(item)]})

        self.assertEqual(calculate.call_count, 2)

    def test_derived_cluster_blend_addition_invalidates_relation_cache(self):
        cluster = self.folder / "Cluster"
        cluster.mkdir()
        source = cluster / "SK_cluster.spm"
        source.write_bytes(b"cluster")
        derived = cluster / "SK_cluster.blend"

        with mock.patch.object(
            GUI, "BLENDER_RELATION_CACHE_PATH", self.cache_path
        ), mock.patch.object(
            GUI, "blender_connection_rows", return_value=[]
        ) as calculate:
            GUI.cache_blender_connection_rows({"items": [self.item()]})
            GUI.cache_blender_connection_rows({"items": [self.item()]})
            self.assertEqual(calculate.call_count, 1)
            derived.write_bytes(b"blend")
            GUI.cache_blender_connection_rows({"items": [self.item()]})

        self.assertEqual(calculate.call_count, 2)

    def test_registry_external_target_content_invalidates_relation_cache(self):
        cluster = self.folder / "Cluster"
        cluster.mkdir()
        source = cluster / "SK_cluster.spm"
        source.write_bytes(b"cluster")
        blend = cluster / "SK_cluster.blend"
        blend.write_bytes(b"blend")
        external = self.root / "external.spm"
        external.write_bytes(b"AAAA")
        original = external.stat()
        registry = GUI.registry_path_for_blend(blend)
        registry.write_text(
            json.dumps({"target_spms": [str(external)]}),
            encoding="utf-8",
        )

        with mock.patch.object(
            GUI, "BLENDER_RELATION_CACHE_PATH", self.cache_path
        ), mock.patch.object(
            GUI, "blender_connection_rows", return_value=[]
        ) as calculate:
            GUI.cache_blender_connection_rows({"items": [self.item()]})
            GUI.cache_blender_connection_rows({"items": [self.item()]})
            self.assertEqual(calculate.call_count, 1)
            external.write_bytes(b"BBBB")
            os.utime(
                external,
                ns=(original.st_atime_ns, original.st_mtime_ns),
            )
            GUI.cache_blender_connection_rows({"items": [self.item()]})

        self.assertEqual(calculate.call_count, 2)

    def test_exact_baseline_binds_registry_bytes(self):
        blend = self.folder / "leaf.blend"
        blend.write_bytes(b"blend")
        registry = GUI.registry_path_for_blend(blend)
        registry.write_text('{"value":"AAAA"}', encoding="utf-8")
        original = registry.stat()
        item = self.item()
        item["leaf_mesh_sources"] = [{
            "atlas_blends": [str(blend)],
            "targets": [{"spm": str(self.spm)}],
        }]
        with mock.patch.object(
            GUI, "BLENDER_RELATION_CACHE_PATH", self.cache_path
        ), mock.patch.object(
            GUI, "blender_connection_rows", return_value=[]
        ):
            GUI.cache_blender_connection_rows({"items": [item]})
        baseline = GUI.seal_exact_mutation_baseline(
            [item], action="test"
        )
        registry.write_text('{"value":"BBBB"}', encoding="utf-8")
        os.utime(
            registry,
            ns=(original.st_atime_ns, original.st_mtime_ns),
        )

        with self.assertRaises(RuntimeError):
            GUI.require_exact_mutation_baseline(baseline)

    def test_relation_scope_manifest_restored_mtime_invalidates_cache(self):
        blend = self.folder / "leaf.blend"
        blend.write_bytes(b"blend")
        scope_dir = self.folder / ".atlas_leaf_speedtree_scopes"
        scope_dir.mkdir()
        scope = scope_dir / "leaf__SK_tree_elm.json"
        scope.write_text('{"value":"AAAA"}', encoding="utf-8")
        original = scope.stat()
        item = self.item()
        item["leaf_mesh_sources"] = [{
            "atlas_blends": [str(blend)],
            "targets": [{"spm": str(self.spm)}],
        }]

        with mock.patch.object(
            GUI, "BLENDER_RELATION_CACHE_PATH", self.cache_path
        ), mock.patch.object(
            GUI, "blender_connection_rows", return_value=[]
        ) as calculate:
            GUI.cache_blender_connection_rows({"items": [copy.deepcopy(item)]})
            GUI.cache_blender_connection_rows({"items": [copy.deepcopy(item)]})
            self.assertEqual(calculate.call_count, 1)

            scope.write_text('{"value":"BBBB"}', encoding="utf-8")
            os.utime(
                scope,
                ns=(original.st_atime_ns, original.st_mtime_ns),
            )
            GUI.cache_blender_connection_rows({"items": [copy.deepcopy(item)]})

        self.assertEqual(calculate.call_count, 2)

    def test_relation_scope_manifest_mtime_order_invalidates_cache(self):
        blend = self.folder / "leaf.blend"
        blend.write_bytes(b"blend")
        scope_dir = self.folder / ".atlas_leaf_speedtree_scopes"
        scope_dir.mkdir()
        first_scope = scope_dir / "a__SK_tree_elm.json"
        second_scope = scope_dir / "b__SK_tree_elm.json"
        for scope, value in ((first_scope, "first"), (second_scope, "second")):
            scope.write_text(
                json.dumps({
                    "blend_file": str(blend),
                    "spm": str(self.spm),
                    "value": value,
                }),
                encoding="utf-8",
            )
        base = time.time_ns() - 10_000_000_000
        os.utime(first_scope, ns=(base, base))
        os.utime(second_scope, ns=(base + 1_000_000, base + 1_000_000))
        item = self.item()
        item["leaf_mesh_sources"] = [{
            "atlas_blends": [str(blend)],
            "targets": [{"spm": str(self.spm)}],
        }]

        with mock.patch.object(
            GUI, "BLENDER_RELATION_CACHE_PATH", self.cache_path
        ), mock.patch.object(
            GUI, "blender_connection_rows", return_value=[]
        ) as calculate:
            GUI.cache_blender_connection_rows({"items": [copy.deepcopy(item)]})
            GUI.cache_blender_connection_rows({"items": [copy.deepcopy(item)]})
            self.assertEqual(calculate.call_count, 1)

            os.utime(
                first_scope,
                ns=(base + 2_000_000, base + 2_000_000),
            )
            os.utime(second_scope, ns=(base, base))
            GUI.cache_blender_connection_rows({"items": [copy.deepcopy(item)]})

        self.assertEqual(calculate.call_count, 2)

    def test_physical_receipt_memo_rejects_restored_mtime_source_change(self):
        source_spm = self.folder / "physical_source.spm"
        source_fbx = self.folder / "physical_source.fbx"
        source_spm.write_bytes(b"SPM1")
        source_fbx.write_bytes(b"AAAA")
        fbx_stat = source_fbx.stat()
        semantic = "c" * 64
        source_contract = {
            "source_spm": str(source_spm),
            "source_spm_sha256": hashlib.sha256(b"SPM1").hexdigest(),
            "source_spm_semantic_projection_version": (
                assembly.SPM_STRUCTURAL_SEMANTIC_PROJECTION_VERSION
            ),
            "source_spm_semantic_fingerprint": semantic,
            "source_fbx": str(source_fbx),
            "source_fbx_sha256": hashlib.sha256(b"AAAA").hexdigest(),
        }
        receipt = {
            "variants": [{
                "plan": "leaf",
                "plan_uv_transfer": {
                    "source_3d_contract": source_contract,
                },
            }],
        }
        memo = ConcurrentContentDigestMemo()
        first = assembly._physical_source_3d_artifacts(
            receipt,
            validation_cache=memo,
        )
        self.assertFalse(first["source_fbx"]["raw_sha256_drift"])

        source_fbx.write_bytes(b"BBBB")
        os.utime(
            source_fbx,
            ns=(fbx_stat.st_atime_ns, fbx_stat.st_mtime_ns),
        )
        with self.assertRaises(assembly.ClusterAssemblyReceiptStaleError):
            assembly._physical_source_3d_artifacts(
                receipt,
                validation_cache=memo,
            )

    def test_deferred_and_physical_relation_caches_never_cross_authority(self):
        with mock.patch.object(
            GUI, "BLENDER_RELATION_CACHE_PATH", self.cache_path
        ), mock.patch.object(
            GUI, "blender_connection_rows", return_value=[]
        ) as calculate:
            physical_metrics = {}
            GUI.cache_blender_connection_rows(
                {"items": [self.item()]}, metrics=physical_metrics
            )
            deferred_metrics = {}
            deferred = self.item()
            GUI.cache_blender_connection_rows(
                {"items": [deferred]},
                metrics=deferred_metrics,
                verify_physical=False,
            )
            GUI.cache_blender_connection_rows(
                {"items": [self.item()]},
                verify_physical=False,
            )

        self.assertEqual(calculate.call_count, 2)
        self.assertEqual(physical_metrics["physical_validation"], "full")
        self.assertEqual(deferred_metrics["physical_validation"], "deferred")
        self.assertEqual(
            deferred["_gui_live_evidence"]["relation_validation_mode"],
            "deferred",
        )

    def test_physical_relation_validation_memo_reuses_exact_artifacts(self):
        canonical = self.folder / "SK_cluster.spm"
        source_fbx = self.folder / "cluster.fbx"
        blend = self.folder / "SK_cluster.blend"
        canonical.write_bytes(b"spm")
        source_fbx.write_bytes(b"fbx")
        blend.write_bytes(b"blend")
        capture = self.folder / "cluster_auto_capture_manifest.json"
        capture.write_text(json.dumps({
            "workflow_mode": "PHYSICAL_DIRECT_CAPTURE",
            "direct_uv_source": "same_blender_physical_capture_projection",
            "physical_capture_contract_sha256": "capture-v1",
        }), encoding="utf-8")
        semantic = "c" * 64
        receipt = {
            "workflow_mode": "PHYSICAL_DIRECT_CAPTURE",
            "source_spm": str(canonical),
            "source_spm_sha256": "a" * 64,
            "source_spm_semantic_projection_version": (
                cluster_sync.SPM_STRUCTURAL_SEMANTIC_PROJECTION_VERSION
            ),
            "source_spm_semantic_fingerprint": semantic,
            "source_fbx": str(source_fbx),
            "source_fbx_sha256": "b" * 64,
            "physical_capture_contract_sha256": "capture-v1",
        }
        payload = {
            "normalized_prototype_receipt": receipt,
            "physical_capture_contract_sha256": "capture-v1",
        }
        validation_cache = {}

        def digest(path):
            return "b" * 64 if Path(path) == source_fbx else "a" * 64

        with mock.patch.object(
            cluster_sync, "_sha256_file", side_effect=digest
        ) as sha256_file, mock.patch.object(
            cluster_sync,
            "spm_file_structural_semantic_fingerprint",
            return_value=semantic,
        ) as structural, mock.patch.object(
            cluster_sync,
            "inspect_normalization_source_identity",
            return_value={"refresh_reasons": []},
        ):
            first = cluster_sync._physical_refresh_state(
                payload,
                canonical,
                blend,
                validation_cache=validation_cache,
            )
            second = cluster_sync._physical_refresh_state(
                payload,
                canonical,
                blend,
                validation_cache=validation_cache,
            )

        self.assertFalse(first["refresh_required"])
        self.assertEqual(first, second)
        self.assertEqual(sha256_file.call_count, 2)
        self.assertEqual(structural.call_count, 1)

    def test_relation_directory_membership_change_invalidates_cache(self):
        rows = [{"blend": self.folder / "leaf.blend", "spms": []}]
        with mock.patch.object(
            GUI, "BLENDER_RELATION_CACHE_PATH", self.cache_path
        ), mock.patch.object(
            GUI, "blender_connection_rows", return_value=rows
        ) as calculate:
            GUI.cache_blender_connection_rows({"items": [self.item()]})
            GUI.cache_blender_connection_rows({"items": [self.item()]})
            self.assertEqual(calculate.call_count, 1)

            (self.folder / "new_input.spm").write_bytes(b"new")
            GUI.cache_blender_connection_rows({"items": [self.item()]})

        self.assertEqual(calculate.call_count, 2)

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
        relation_kwargs = {}

        class ImmediateThread:
            def __init__(self, target, daemon=True):
                self.target = target
                self.daemon = daemon

            def start(self):
                self.target()

        def fail_relation(*_args, **kwargs):
            order.append("relation")
            relation_kwargs.update(kwargs)
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
        self.assertIs(relation_kwargs["verify_physical"], False)
        self.assertTrue(app._display_only_snapshot)
        app._lock_mutation_controls.assert_called()


class MutationPlanAuthorityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.first = self.root / "first.spm"
        self.second = self.root / "second.spm"
        self.first.write_bytes(b"AAAA")
        self.second.write_bytes(b"CCCC")

    def tearDown(self):
        self.temporary.cleanup()

    def capture(self, units, **kwargs):
        return mutation_authority.capture_manifest(
            action="test",
            logical_plan={"units": [row["unit_id"] for row in units]},
            units=units,
            receipt_path=self.root / "receipt.json",
            **kwargs,
        )

    def test_plan_only_same_size_restored_mtime_tamper_is_blocked(self):
        original = self.first.stat()
        manifest = self.capture([{
            "unit_id": "one",
            "payload": {"source": str(self.first)},
            "paths": [self.first],
        }])
        self.first.write_bytes(b"BBBB")
        os.utime(
            self.first,
            ns=(original.st_atime_ns, original.st_mtime_ns),
        )

        with self.assertRaises(mutation_authority.MutationAuthorityError) as caught:
            mutation_authority.require_unit(
                manifest,
                "one",
                current_payload={"source": str(self.first)},
            )
        self.assertEqual(caught.exception.receipt["blocked_unit"], "one")
        self.assertEqual(caught.exception.receipt["writes_before_block"], 0)

    def test_missing_output_occupancy_and_directory_order_are_bound(self):
        output = self.root / "planned.blend"
        scopes = self.root / ".atlas_leaf_speedtree_scopes"
        scopes.mkdir()
        old = scopes / "leaf__SK_tree.json"
        old.write_text("{}", encoding="utf-8")
        manifest = self.capture([{
            "unit_id": "one",
            "payload": {"output": str(output)},
            "paths": [output],
            "memberships": [scopes],
        }])
        output.write_bytes(b"occupied")
        newer = scopes / "new__SK_tree.json"
        newer.write_text("{}", encoding="utf-8")

        with self.assertRaisesRegex(
                mutation_authority.MutationAuthorityError,
                "planned path state changed"):
            mutation_authority.require_unit(
                manifest,
                "one",
                current_payload={"output": str(output)},
            )

    def test_live_payload_and_config_are_not_detached_self_checks(self):
        manifest = self.capture(
            [{
                "unit_id": "one",
                "payload": {"source": str(self.first), "mode": "safe"},
                "paths": [self.first],
            }],
            config_projection={"timeout": 10},
        )
        with self.assertRaisesRegex(
                mutation_authority.MutationAuthorityError,
                "live execution payload changed"):
            mutation_authority.require_unit(
                manifest,
                "one",
                current_payload={
                    "source": str(self.first),
                    "mode": "changed",
                },
                current_config={"timeout": 10},
            )

        manifest = self.capture(
            [{
                "unit_id": "one",
                "payload": {"source": str(self.first)},
                "paths": [self.first],
            }],
            config_projection={"timeout": 10},
        )
        with self.assertRaisesRegex(
                mutation_authority.MutationAuthorityError,
                "config changed"):
            mutation_authority.require_unit(
                manifest,
                "one",
                current_payload={"source": str(self.first)},
                current_config={"timeout": 11},
            )

    def test_second_unit_drift_blocks_with_durable_partial_receipt(self):
        units = [
            {
                "unit_id": "one",
                "payload": {"source": str(self.first)},
                "paths": [self.first],
            },
            {
                "unit_id": "two",
                "payload": {"source": str(self.second)},
                "paths": [self.second],
            },
        ]
        manifest = self.capture(units)
        mutation_authority.require_unit(
            manifest, "one", current_payload=units[0]["payload"]
        )
        mutation_authority.complete_unit(
            manifest, "one", post_paths=[self.first]
        )
        original = self.second.stat()
        self.second.write_bytes(b"DDDD")
        os.utime(
            self.second,
            ns=(original.st_atime_ns, original.st_mtime_ns),
        )

        with self.assertRaises(mutation_authority.MutationAuthorityError) as caught:
            mutation_authority.require_unit(
                manifest, "two", current_payload=units[1]["payload"]
            )
        receipt = json.loads(
            (self.root / "receipt.json").read_text(encoding="utf-8")
        )["receipt"]
        self.assertEqual(receipt["blocked_unit"], "two")
        self.assertEqual(receipt["writes_before_block"], 1)
        self.assertEqual(receipt["completed_units"][0]["unit_id"], "one")
        self.assertEqual(caught.exception.receipt, receipt)

    def test_completed_shared_postcondition_advances_only_shared_inputs(self):
        scope = self.root / "scope"
        scope.mkdir()
        shared = scope / "shared.spm"
        shared.write_bytes(b"initial")
        units = [
            {
                "unit_id": "one",
                "payload": {"row": 1},
                "paths": [shared],
                "write_paths": [shared],
                "memberships": [scope],
                "write_memberships": [scope],
            },
            {
                "unit_id": "two",
                "payload": {"row": 2},
                "paths": [shared, self.second],
                "memberships": [scope],
            },
        ]
        manifest = self.capture(units)
        mutation_authority.require_unit(
            manifest, "one", current_payload=units[0]["payload"]
        )
        shared.write_bytes(b"authorized-post-state")
        mutation_authority.complete_unit(
            manifest, "one", post_paths=[shared]
        )
        mutation_authority.require_unit(
            manifest, "two", current_payload=units[1]["payload"]
        )

        manifest = self.capture(units)
        mutation_authority.require_unit(
            manifest, "one", current_payload=units[0]["payload"]
        )
        shared.write_bytes(b"next-authorized-post-state")
        mutation_authority.complete_unit(
            manifest, "one", post_paths=[shared]
        )
        self.second.write_bytes(b"unrelated-drift")
        with self.assertRaisesRegex(
                mutation_authority.MutationAuthorityError,
                "planned path state changed"):
            mutation_authority.require_unit(
                manifest, "two", current_payload=units[1]["payload"]
            )

    def test_completed_unit_does_not_advance_undeclared_shared_write(self):
        shared = self.root / "read-only-shared.spm"
        shared.write_bytes(b"initial")
        units = [
            {
                "unit_id": "one",
                "payload": {"row": 1},
                "paths": [shared],
            },
            {
                "unit_id": "two",
                "payload": {"row": 2},
                "paths": [shared],
            },
        ]
        manifest = self.capture(units)
        mutation_authority.require_unit(
            manifest, "one", current_payload=units[0]["payload"]
        )
        shared.write_bytes(b"unplanned-change")
        with self.assertRaisesRegex(
                mutation_authority.MutationAuthorityError,
                "unplanned path changed during execution"):
            mutation_authority.complete_unit(manifest, "one")

    def test_tool_tamper_and_empty_units_fail_closed(self):
        tool = self.root / "tool.py"
        tool.write_bytes(b"AAAA")
        original = tool.stat()
        manifest = self.capture(
            [{
                "unit_id": "one",
                "payload": {"source": str(self.first)},
                "paths": [self.first],
            }],
            tool_paths=[tool],
        )
        tool.write_bytes(b"BBBB")
        os.utime(tool, ns=(original.st_atime_ns, original.st_mtime_ns))
        with self.assertRaisesRegex(
                mutation_authority.MutationAuthorityError,
                "tool/package input changed"):
            mutation_authority.require_unit(
                manifest,
                "one",
                current_payload={"source": str(self.first)},
            )
        with self.assertRaisesRegex(RuntimeError, "at least one"):
            self.capture([])

    def test_child_authority_rejects_atlas_scope_tamper(self):
        scopes = self.root / ".atlas_leaf_speedtree_scopes"
        scopes.mkdir()
        (scopes / "old__SK_tree.json").write_text("{}", encoding="utf-8")
        manifest = self.capture([{
            "unit_id": "atlas-remove",
            "payload": {"target_spms": [str(self.first)]},
            "paths": [self.first],
            "memberships": [scopes],
        }])
        child_path = self.root / "child-authority.json"
        document_sha256 = mutation_authority.write_child_authority(
            manifest, "atlas-remove", child_path
        )
        (scopes / "new__SK_tree.json").write_text("{}", encoding="utf-8")

        with self.assertRaisesRegex(
                RuntimeError, "directory membership changed"):
            mutation_authority.validate_child_authority(
                child_path, document_sha256
            )


class MutationOperationBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    @staticmethod
    def app():
        app = GUI.App.__new__(GUI.App)
        app._ui = lambda callback: callback()
        app.log = mock.Mock()
        app.tree = mock.Mock()
        app.status_var = mock.Mock()
        app._prepare_finished = mock.Mock()
        app._batch_finished = mock.Mock()
        return app

    def test_step1_second_row_drift_blocks_before_second_write(self):
        folders = [self.root / "tree_a", self.root / "tree_b"]
        rows = []
        for folder in folders:
            folder.mkdir()
            (folder / "SK_tree.spm").write_bytes(b"AAAA")
            rows.append({
                "item": {"folder": str(folder), "name": folder.name},
                "mesh": "",
                "exclude": [],
            })
        units = GUI.step1_authority_units(rows)
        baseline = GUI.seal_exact_mutation_baseline(
            [],
            action="step1_prepare",
            plan_payload={
                "rows": [GUI.step1_unit_payload(row) for row in rows]
            },
            authority_units=units,
            tool_paths=[GUI.mutation_source_path(GUI.prepare_sk)],
        )
        app = self.app()
        app._reaudit_and_seal_mutation_items = mock.Mock(
            return_value=baseline
        )

        def first_then_drift(*_args, **_kwargs):
            (folders[1] / "late.spm").write_bytes(b"drift")
            return {"targets": [{"status": "up_to_date"}]}

        with mock.patch.object(
            GUI, "prepare_sk", side_effect=first_then_drift
        ) as prepare:
            with self.assertRaises(GUI.MutationAuthorityError) as caught:
                app._run_prepare(rows)

        self.assertEqual(prepare.call_count, 1)
        self.assertEqual(caught.exception.receipt["blocked_unit"], units[1]["unit_id"])
        self.assertEqual(caught.exception.receipt["writes_before_block"], 1)

    def test_step1_same_folder_rows_chain_exact_completed_postcondition(self):
        folder = self.root / "tree"
        folder.mkdir()
        item = {"folder": str(folder), "name": folder.name}
        rows = [
            {"item": item, "mesh": "leaf_a", "exclude": []},
            {"item": item, "mesh": "leaf_b", "exclude": []},
        ]
        units = GUI.step1_authority_units(rows)
        baseline = GUI.seal_exact_mutation_baseline(
            [],
            action="step1_prepare",
            plan_payload={
                "rows": [GUI.step1_unit_payload(row) for row in rows]
            },
            authority_units=units,
            tool_paths=[GUI.mutation_source_path(GUI.prepare_sk)],
        )
        app = self.app()
        app._reaudit_and_seal_mutation_items = mock.Mock(
            return_value=baseline
        )

        def write_planned_target(_folder, mesh_names, **_kwargs):
            mesh = mesh_names[0]
            target = folder / f"SK_{mesh}.spm"
            target.write_bytes(mesh.encode("utf-8"))
            return {
                "targets": [{
                    "status": "prepared",
                    "mesh_name": mesh,
                    "created": str(target),
                    "patch": {"renames": []},
                }]
            }

        with mock.patch.object(
            GUI, "prepare_sk", side_effect=write_planned_target
        ) as prepare:
            result = app._run_prepare(rows)

        self.assertEqual(prepare.call_count, 2)
        self.assertEqual(result["shared_queue_result"]["completed"], 2)

    def test_step1_same_folder_unrelated_drift_after_completion_blocks_next(self):
        folder = self.root / "tree"
        folder.mkdir()
        item = {"folder": str(folder), "name": folder.name}
        rows = [
            {"item": item, "mesh": "leaf_a", "exclude": []},
            {"item": item, "mesh": "leaf_b", "exclude": []},
        ]
        units = GUI.step1_authority_units(rows)
        baseline = GUI.seal_exact_mutation_baseline(
            [],
            action="step1_prepare",
            plan_payload={
                "rows": [GUI.step1_unit_payload(row) for row in rows]
            },
            authority_units=units,
            tool_paths=[GUI.mutation_source_path(GUI.prepare_sk)],
        )
        app = self.app()
        app._reaudit_and_seal_mutation_items = mock.Mock(
            return_value=baseline
        )
        original_complete = GUI.complete_exact_mutation_unit

        def complete_then_drift(*args, **kwargs):
            receipt = original_complete(*args, **kwargs)
            (folder / "unrelated.tmp").write_bytes(b"drift")
            return receipt

        with mock.patch.object(
            GUI,
            "prepare_sk",
            return_value={"targets": [{"status": "up_to_date"}]},
        ) as prepare, mock.patch.object(
            GUI,
            "complete_exact_mutation_unit",
            side_effect=complete_then_drift,
        ):
            with self.assertRaises(GUI.MutationAuthorityError):
                app._run_prepare(rows)

        self.assertEqual(prepare.call_count, 1)

    def test_step3_second_render_drift_blocks_before_second_external_call(self):
        out_dir = self.root / "texture"
        out_dir.mkdir()
        inputs = []
        jobs = []
        for index in range(2):
            source = self.root / f"source_{index}.tga"
            source.write_bytes(b"AAAA")
            inputs.append(source)
            jobs.append({
                "base": f"M_leaf_{index}",
                "texture_base": f"T_leaf_{index}",
                "out_dir": str(out_dir),
                "inputs": {"basecolor": str(source)},
                "item": {
                    "folder": str(self.root / f"tree_{index}"),
                    "name": f"tree_{index}",
                },
            })
        plan = {"jobs": jobs, "pending_manifest_rows": []}
        units = GUI.step3_authority_units(plan)
        cfg = {
            "sbsrender_timeout": 10,
            "unreal_texture_sync_enabled": False,
        }
        config_keys = (
            "designer_dir",
            "cluster_sbsar",
            "cluster_sbsar_normal_behavior",
            "sbsrender_timeout",
            "tree_root",
            "unreal_texture_sync_enabled",
            "unreal_project",
            "unreal_editor_cmd",
            "unreal_texture_destination",
            "unreal_texture_sync_timeout",
        )
        baseline = GUI.seal_exact_mutation_baseline(
            [],
            action="step3_texture",
            plan_payload=GUI.step3_exact_plan_payload(plan),
            authority_units=units,
            config_projection=GUI.mutation_config_projection(
                cfg, config_keys
            ),
        )
        app = self.app()
        app.cfg = cfg

        def first_then_drift(job, _cfg, _timeout):
            original = inputs[1].stat()
            inputs[1].write_bytes(b"BBBB")
            os.utime(
                inputs[1],
                ns=(original.st_atime_ns, original.st_mtime_ns),
            )
            return {
                "texture_base": job["texture_base"],
                "files": [out_dir / f"{job['texture_base']}_color.tga"],
            }

        with mock.patch.object(
            GUI, "run_texture_job", side_effect=first_then_drift
        ) as render:
            with self.assertRaises(GUI.MutationAuthorityError) as caught:
                app._run_step3(
                    jobs,
                    [],
                    sync_files=[],
                    exact_mutation_baseline=baseline,
                )

        self.assertEqual(render.call_count, 1)
        self.assertEqual(caught.exception.receipt["blocked_unit"], units[1]["unit_id"])

    def test_step2_second_job_drift_blocks_before_second_child_call(self):
        jobs = []
        albedos = []
        for index in range(2):
            folder = self.root / f"tree_{index}"
            folder.mkdir()
            albedo = folder / f"leaf_{index}_color.tga"
            alpha = folder / f"leaf_{index}_opacity.tga"
            albedo.write_bytes(b"AAAA")
            alpha.write_bytes(b"alpha")
            albedos.append(albedo)
            jobs.append({
                "base": f"M_leaf_{index}",
                "albedo": str(albedo),
                "alpha": str(alpha),
                "blend_out": str(self.root / f"leaf_{index}.blend"),
                "reuse_existing_blend": False,
                "target_spms": [],
                "target_details": [],
                "item": {"folder": str(folder), "name": folder.name},
            })
        cfg = {"blender_exe": "", "atlas_job_timeout": 10}
        config_keys = ("blender_exe", "atlas_job_timeout")
        units = GUI.step2_authority_units(jobs, False)
        baseline = GUI.seal_exact_mutation_baseline(
            [],
            action="step2_atlas",
            plan_payload=GUI.step2_exact_plan_payload(jobs, False),
            authority_units=units,
            config_projection=GUI.mutation_config_projection(
                cfg, config_keys
            ),
            tool_paths=[GUI.TOOL_DIR / "jobs" / "atlas_blend_job.py"],
        )
        app = self.app()
        app.cfg = cfg
        app._reaudit_and_seal_mutation_items = mock.Mock(
            return_value=baseline
        )

        def first_then_drift(command, **_kwargs):
            report_path = Path(command[command.index("--report") + 1])
            report_path.write_text(
                json.dumps({"status": "ok", "meshes": 1}),
                encoding="utf-8",
            )
            original = albedos[1].stat()
            albedos[1].write_bytes(b"BBBB")
            os.utime(
                albedos[1],
                ns=(original.st_atime_ns, original.st_mtime_ns),
            )
            return mock.Mock(returncode=0, stderr="", stdout="")

        with mock.patch.object(
            GUI, "owned_run", side_effect=first_then_drift
        ) as child, mock.patch.object(
            GUI, "register_blend_source_index"
        ), mock.patch.object(GUI, "save_spm_analysis_cache"):
            with self.assertRaises(GUI.MutationAuthorityError) as caught:
                app._run_step2(jobs, False)

        self.assertEqual(child.call_count, 1)
        self.assertEqual(caught.exception.receipt["blocked_unit"], units[1]["unit_id"])

    def test_atlas_add_rechecks_target_adjacent_to_registry_write(self):
        blend = self.root / "leaf.blend"
        target = self.root / "SK_tree.spm"
        blend.write_bytes(b"blend")
        target.write_bytes(b"AAAA")
        item = {"folder": str(self.root), "name": "tree"}
        plan_payload = {
            "blend": str(blend),
            "target_spm": str(target),
            "target_registry": str(GUI.registry_path_for_blend(blend)),
            "capture_manifest": str(
                GUI._relation_capture_manifest_path(blend)
            ),
        }
        unit_id = "atlas-target-add"
        baseline = GUI.seal_exact_mutation_baseline(
            [],
            action="atlas_target_add",
            plan_payload=plan_payload,
            authority_units=[{
                "unit_id": unit_id,
                "payload": plan_payload,
                "item_keys": [],
                "paths": [
                    blend,
                    target,
                    GUI.registry_path_for_blend(blend),
                    GUI._relation_capture_manifest_path(blend),
                ],
                "memberships": [
                    target.parent / ".atlas_leaf_speedtree_scopes"
                ],
            }],
        )
        app = self.app()
        app._reaudit_and_seal_mutation_items = mock.Mock(
            return_value=baseline
        )

        def drift_target(_blend):
            original = target.stat()
            target.write_bytes(b"BBBB")
            os.utime(
                target,
                ns=(original.st_atime_ns, original.st_mtime_ns),
            )
            return []

        app._current_targets_for_blend = mock.Mock(
            side_effect=drift_target
        )
        with mock.patch.object(GUI, "save_target_registry") as save:
            with self.assertRaises(GUI.MutationAuthorityError):
                app._run_add_blend_target_spm(
                    blend, target, evidence_items=[item]
                )
        save.assert_not_called()


class ProductionShapedLatencyFixtureTests(unittest.TestCase):
    def test_known_per_spm_manifest_amplification_fails_total_call_guard(self):
        """The pre-fix resolver shape fails while unique counters still pass."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "Tree"
            cache = Path(temporary) / "cache"
            root.mkdir()
            for folder_index in range(2):
                folder = root / f"tree_fixture_{folder_index:02d}"
                folder.mkdir()
                for spm_index in range(2):
                    path = folder / (
                        f"SK_tree_fixture_{folder_index:02d}_{spm_index:02d}.spm"
                    )
                    path.write_text(
                        '<?xml version="1.0"?><SpeedTree><Materials>'
                        '<Material_v8 ID="1" Name="M_fixture_atlas_01">'
                        '</Material_v8></Materials><Generators></Generators>'
                        '<Nodes></Nodes></SpeedTree>',
                        encoding="utf-8",
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

            # Negative control for the exact PR #61 integration regression:
            # a manifest-free folder used to send every sibling SPM through
            # resolve_atlas_manifests().  The fixture is deliberately tiny,
            # but preserves that per-SPM/per-scope cardinality mismatch.  The
            # unmodified 597-SPM fixture below has no Atlas carrier and records
            # zero resolver calls, so this injection is the load-bearing proof
            # that the manifest rule catches more than the trivial 0 <= 55.
            def legacy_per_spm_manifest_targets(folder):
                return sorted(Path(folder).glob("*.spm"))

            with mock.patch.object(
                audit, "SPM_ANALYSIS_CACHE_PATH", cache / "spm.json"
            ), mock.patch.object(
                audit, "SBS_GRAPH_CACHE_PATH", cache / "sbs.json"
            ), mock.patch.object(
                audit, "BLEND_IMAGE_CACHE_PATH", cache / "blend.json"
            ), mock.patch.object(
                audit, "PROVIDER_MAP_CACHE_PATH", cache / "provider.json"
            ), mock.patch.object(
                audit,
                "_atlas_manifest_targets",
                side_effect=legacy_per_spm_manifest_targets,
            ):
                audit._SPM_ANALYSIS_CACHE.clear()
                audit._PERSISTENT_SPM_ANALYSIS = None
                audit._PERSISTENT_SBS_GRAPHS = None
                audit._PERSISTENT_BLEND_IMAGES = None
                report = audit.make_report(cfg)

            metrics = report["startup_timing"]["session_cache_metrics"]
            guard = report["startup_timing"]["total_invocation_guard"]
            self.assertEqual(
                metrics.get("spm_decode_misses_unique_files"), 4
            )
            self.assertEqual(
                metrics.get("legacy_receipt_inspection_calls_unique_files"),
                4,
            )
            self.assertEqual(
                metrics.get("atlas_manifest_resolution_calls"), 8
            )
            self.assertEqual(guard["status"], "failed")
            self.assertEqual(
                guard["rules"]["atlas_manifest_resolution_calls"]["limit"],
                2,
            )
            with self.assertRaisesRegex(
                StartupAmplificationError,
                r"atlas_manifest_resolution_calls=8 > 2",
            ):
                require_startup_total_invocation_guard(
                    metrics,
                    audit_scope_count=2,
                    spm_count=4,
                )

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
                    cold_evidence = {}
                    cold_started = time.perf_counter()
                    cold = audit.make_report(
                        cfg, session_evidence=cold_evidence
                    )
                    cold_elapsed = time.perf_counter() - cold_started
                    audit.save_spm_analysis_cache()
                    warm_evidence = {}
                    warm_started = time.perf_counter()
                    warm = audit.make_report(
                        cfg, session_evidence=warm_evidence
                    )
                    warm_elapsed = time.perf_counter() - warm_started
                    base_legacy_call_count = legacy_inspection.call_count

                    invalidated_spm = (
                        root / "tree_fixture_00"
                        / "SK_tree_fixture_00_00.spm"
                    )
                    original = invalidated_spm.stat()
                    original_text = invalidated_spm.read_text(
                        encoding="utf-8"
                    )
                    changed = original_text.replace(
                        "fixture_00_00", "changed_00_00"
                    )
                    invalidated_spm.write_text(changed, encoding="utf-8")
                    os.utime(
                        invalidated_spm,
                        ns=(original.st_atime_ns, original.st_mtime_ns),
                    )
                    invalidated_evidence = {}
                    invalidated_started = time.perf_counter()
                    invalidated = audit.make_report(
                        cfg, session_evidence=invalidated_evidence
                    )
                    invalidated_elapsed = (
                        time.perf_counter() - invalidated_started
                    )

                    scoped_evidence = {}
                    scoped = audit.make_report(
                        cfg,
                        targets=[root / "tree_fixture_00"],
                        session_evidence=scoped_evidence,
                    )
                    invalidated_spm.write_text(
                        original_text, encoding="utf-8"
                    )
                    os.utime(
                        invalidated_spm,
                        ns=(original.st_atime_ns, original.st_mtime_ns),
                    )

            self.assertEqual(spm_count, 597)
            self.assertEqual(cold["summary"]["total"], 55)
            self.assertEqual(warm["summary"]["total"], 55)
            self.assertEqual(
                base_legacy_call_count,
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
            for report, metrics in (
                (cold, cold_cache),
                (warm, warm_cache),
            ):
                guard = require_startup_total_invocation_guard(
                    metrics,
                    audit_scope_count=55,
                    spm_count=597,
                )
                self.assertEqual(guard["status"], "ok")
                self.assertEqual(
                    report["startup_timing"]["total_invocation_guard"],
                    guard,
                )
            self.assertEqual(
                cold_cache.get("spm_analysis_calls"),
                sum(
                    cold_cache.get(name, 0)
                    for name in (
                        "spm_decode_misses",
                        "spm_memory_hits",
                        "spm_persistent_hits",
                        "spm_report_hits",
                    )
                ),
            )
            self.assertEqual(
                warm_cache.get("spm_analysis_calls"),
                sum(
                    warm_cache.get(name, 0)
                    for name in (
                        "spm_decode_misses",
                        "spm_memory_hits",
                        "spm_persistent_hits",
                        "spm_report_hits",
                    )
                ),
            )
            self.assertEqual(
                cold_cache.get("legacy_receipt_inspection_calls"), 597
            )
            self.assertEqual(
                warm_cache.get("legacy_receipt_inspection_calls"), 597
            )
            self.assertEqual(
                cold_cache.get("atlas_manifest_resolution_calls", 0), 0
            )
            self.assertEqual(
                warm_cache.get("atlas_manifest_resolution_calls", 0), 0
            )
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
                    cold,
                    metrics=cold_relation_metrics,
                    session_evidence=cold_evidence,
                    verify_physical=False,
                )
                cold_relation_elapsed = (
                    time.perf_counter() - cold_relation_started
                )
                warm_relation_metrics = {}
                warm_relation_started = time.perf_counter()
                GUI.cache_blender_connection_rows(
                    warm,
                    metrics=warm_relation_metrics,
                    session_evidence=warm_evidence,
                    verify_physical=False,
                )
                warm_relation_elapsed = (
                    time.perf_counter() - warm_relation_started
                )
                invalidated_relation_metrics = {}
                invalidated_spm.write_text(changed, encoding="utf-8")
                os.utime(
                    invalidated_spm,
                    ns=(original.st_atime_ns, original.st_mtime_ns),
                )
                invalidated_relation_started = time.perf_counter()
                GUI.cache_blender_connection_rows(
                    invalidated,
                    metrics=invalidated_relation_metrics,
                    session_evidence=invalidated_evidence,
                    verify_physical=False,
                )
                invalidated_relation_elapsed = (
                    time.perf_counter() - invalidated_relation_started
                )

            self.assertEqual(cold_relation_metrics["cache_misses"], 55)
            self.assertEqual(warm_relation_metrics["cache_hits"], 55)
            self.assertEqual(
                cold_relation_metrics["session_exact_candidate_count"],
                597,
            )
            self.assertEqual(
                cold_relation_metrics["reused_exact_content_file_count"],
                0,
            )
            self.assertGreater(
                warm_relation_metrics["first_pass_physical_content_reads"],
                0,
            )
            self.assertEqual(
                invalidated["startup_timing"]["session_cache_metrics"].get(
                    "spm_decode_misses_unique_files"
                ),
                1,
            )
            self.assertEqual(invalidated_relation_metrics["cache_hits"], 54)
            self.assertEqual(invalidated_relation_metrics["cache_misses"], 1)
            self.assertEqual(
                invalidated_relation_metrics[
                    "session_exact_candidate_count"
                ],
                597,
            )
            self.assertEqual(scoped["summary"]["total"], 1)
            self.assertEqual(
                scoped["startup_timing"]["provider_metrics"][
                    "inventory_file_count"
                ],
                11,
            )
            scoped_prefetch = next(
                phase
                for phase in scoped["startup_timing"]["phases"]
                if phase["phase"] == "spm_content_identity_prefetch"
            )
            self.assertEqual(scoped_prefetch["counts"]["file_count"], 11)
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
            self.assertLess(
                invalidated_elapsed + invalidated_relation_elapsed,
                PRODUCTION_FIXTURE_LATENCY_BUDGET_SECONDS[
                    "warm_usable_ready"
                ],
            )
            # This is an additional product ceiling, not a replacement for
            # the tighter 15s/8s cardinality-fixture budgets above.
            self.assertLessEqual(
                cold_elapsed + cold_relation_elapsed,
                USABLE_READY_ACCEPTANCE_CAP_SECONDS,
            )
            self.assertLessEqual(
                warm_elapsed + warm_relation_elapsed,
                USABLE_READY_ACCEPTANCE_CAP_SECONDS,
            )
            limitation_receipt = {
                "workload_equivalence": "cardinality_only",
                "folder_count": 55,
                "spm_count": 597,
                "production_assets_touched": False,
                "live_apps_controlled": False,
                "limitations": (
                    "tiny local XML fixtures; same-process warm cache; "
                    "no production-size files, OneDrive, or live D:/apps"
                ),
            }
            self.assertEqual(
                limitation_receipt["workload_equivalence"],
                "cardinality_only",
            )


if __name__ == "__main__":
    unittest.main()
