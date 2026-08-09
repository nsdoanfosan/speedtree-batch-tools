"""Issue #3: share _spm_analysis's decoded SPM with the legacy marker audit.

Guards the fix for the redundant read/decompress/regex pass that
speedtree_legacy_cluster_contract._generator_foregrounds used to run on every
receipt-bearing SPM pcg_texture_audit._spm_analysis had already decoded, and
for the schema-4 cache invalidation regression called out on the issue
(bumping leaf_binding_schema forced a full re-decode of every previously
cached SPM).
"""
import gzip
import json
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

TOOL_DIR = Path(__file__).resolve().parents[1]
REPO_DIR = TOOL_DIR.parent
for candidate in (TOOL_DIR, REPO_DIR):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

import pcg_texture_audit as audit  # noqa: E402
import speedtree_legacy_cluster_contract as contract  # noqa: E402


LEGACY_GUID = "legacy-marked-guid"
PLAIN_GUID = "plain-generator-guid"


def material(material_id, name, refs):
    refs_xml = "".join(f"<TexFilename>{value}</TexFilename>" for value in refs)
    return f'<Material_v8 ID="{material_id}" Name="{name}">{refs_xml}</Material_v8>'


def generator(guid, material_id, *, marked=False):
    color = ""
    if marked:
        color = (
            "<m_bSetForegroundIconColor>true</m_bSetForegroundIconColor>"
            "<m_vecForegroundIconColor_r>1</m_vecForegroundIconColor_r>"
            "<m_vecForegroundIconColor_g>0</m_vecForegroundIconColor_g>"
            "<m_vecForegroundIconColor_b>1</m_vecForegroundIconColor_b>"
            "<m_vecForegroundIconColor_a>1</m_vecForegroundIconColor_a>"
        )
    return (
        f'<Generator Type="Leaf Mesh"><Name>{guid}</Name>'
        f"<GUID>{guid}</GUID><Hidden>false</Hidden>{color}<Properties>"
        f"<Property><Name>Leaves:Type:0:Material</Name>"
        f"<Value>{material_id}</Value></Property>"
        "</Properties></Generator>"
    )


def write_spm(
        path, *, legacy_marked=True, extra_generators="", padding_size=0):
    payload = (
        "<?xml version=\"1.0\"?><SpeedTree><Materials>"
        + material("1", "M_leaf_cluster", ["leaf_color.tga"])
        + "</Materials><Generators>"
        + generator(LEGACY_GUID, "1", marked=legacy_marked)
        + generator(PLAIN_GUID, "1", marked=False)
        + extra_generators
        + "</Generators><Nodes>"
        + ("x" * padding_size)
        + "</Nodes></SpeedTree>"
    ).encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(gzip.compress(payload, mtime=0))


def write_marker_receipt(spm, guids):
    receipt = contract.marker_receipt_path(spm)
    receipt.parent.mkdir(parents=True, exist_ok=True)
    receipt.write_text(json.dumps({
        "kind": contract.RECEIPT_KIND,
        "version": contract.RECEIPT_VERSION,
        "status": "applied",
        "spm": str(spm.resolve()),
        "generator_guids": list(guids),
    }), encoding="utf-8")
    return receipt


def write_problem_receipt(spm, guids):
    receipt = contract.problem_marker_receipt_path(spm)
    receipt.parent.mkdir(parents=True, exist_ok=True)
    receipt.write_text(json.dumps({
        "status": "active",
        "active_problem_guids": list(guids),
    }), encoding="utf-8")
    return receipt


class SharedSnapshotTestCase(unittest.TestCase):
    """Fully isolate every in-process cache the two modules keep."""

    def setUp(self):
        self._old_memory = audit._SPM_ANALYSIS_CACHE
        self._old_persistent = audit._PERSISTENT_SPM_ANALYSIS
        self._old_dirty = audit._PERSISTENT_SPM_ANALYSIS_DIRTY
        audit._SPM_ANALYSIS_CACHE = {}
        audit._PERSISTENT_SPM_ANALYSIS = {}
        audit._PERSISTENT_SPM_ANALYSIS_DIRTY = False
        self._pending_token = audit._PENDING_DECODED_HANDOFF.set(None)
        self._old_shared = contract._SHARED_GENERATOR_SNAPSHOTS
        contract._SHARED_GENERATOR_SNAPSHOTS = {}
        self._handoff_token = contract._ACTIVE_DECODED_HANDOFF.set(None)
        contract._inspect_cached.cache_clear()

    def tearDown(self):
        audit._SPM_ANALYSIS_CACHE = self._old_memory
        audit._PERSISTENT_SPM_ANALYSIS = self._old_persistent
        audit._PERSISTENT_SPM_ANALYSIS_DIRTY = self._old_dirty
        audit._PENDING_DECODED_HANDOFF.reset(self._pending_token)
        contract._SHARED_GENERATOR_SNAPSHOTS = self._old_shared
        contract._ACTIVE_DECODED_HANDOFF.reset(self._handoff_token)
        contract._inspect_cached.cache_clear()

    def reset_in_process_caches(self):
        """Simulate a brand-new process that only has the disk cache."""
        audit._SPM_ANALYSIS_CACHE = {}
        audit._PENDING_DECODED_HANDOFF.set(None)
        contract._SHARED_GENERATOR_SNAPSHOTS = {}
        contract._inspect_cached.cache_clear()


class ColdPassDedupTests(SharedSnapshotTestCase):
    def test_cold_pass_decodes_and_narrows_the_spm_only_once(self):
        with tempfile.TemporaryDirectory() as temp:
            spm = Path(temp) / "SK_tree_marker.spm"
            write_spm(spm)
            write_marker_receipt(spm, [LEGACY_GUID])

            with mock.patch.object(
                    audit, "read_maybe_gzip_text",
                    wraps=audit.read_maybe_gzip_text) as reader, \
                mock.patch.object(
                    contract, "_generator_section_bytes",
                    wraps=contract._generator_section_bytes) as raw_narrow:
                bindings = audit.leaf_generator_bindings(spm)

            self.assertEqual(reader.call_count, 0)
            # The full-SHA identity read owns the same raw bytes that are
            # decoded for semantic analysis; the path-based decoder must not
            # reopen the SPM.
            # The legacy audit never falls back to its own read/decompress:
            # _generator_section_bytes (the raw-read narrowing helper) must
            # not run at all when the shared snapshot was available.
            self.assertEqual(raw_narrow.call_count, 0)

            by_guid = {row["generator_guid"]: row for row in bindings}
            self.assertTrue(by_guid[LEGACY_GUID]["legacy_cluster_origin"])
            self.assertFalse(by_guid[PLAIN_GUID]["legacy_cluster_origin"])

    def test_no_receipt_never_reads_generators_a_second_time_either(self):
        with tempfile.TemporaryDirectory() as temp:
            spm = Path(temp) / "SK_tree_no_receipt.spm"
            write_spm(spm)
            # No marker receipt: legacy audit's early return must still never
            # touch the file.
            with mock.patch.object(
                    contract, "_generator_section_bytes",
                    wraps=contract._generator_section_bytes) as raw_narrow:
                bindings = audit.leaf_generator_bindings(spm)
            self.assertEqual(raw_narrow.call_count, 0)
            self.assertFalse(
                any(row["legacy_cluster_origin"] for row in bindings))

    def test_four_cold_workers_keep_each_decoded_spm_handoff_owned(self):
        """Reproduce the original module-global single-slot overwrite.

        Hold all four workers immediately after their cold analysis has
        decoded a different SPM.  A single shared pending slot can retain only
        the last decode, forcing the other three legacy audits to re-read
        their files.  The handoff contract must instead keep every decode
        bound to its owning path/stat and consume all four without fallback.
        """
        with tempfile.TemporaryDirectory() as temp:
            original_analysis = audit._spm_analysis
            for iteration in range(10):
                spms = []
                expected_guids = []
                for index in range(4):
                    guid = f"round-{iteration}-worker-{index}-only-guid"
                    spm = Path(temp) / (
                        f"SK_tree_round_{iteration}_worker_{index}.spm")
                    write_spm(
                        spm,
                        legacy_marked=False,
                        extra_generators=generator(guid, "1", marked=True),
                    )
                    write_marker_receipt(spm, [guid])
                    spms.append(spm)
                    expected_guids.append(guid)

                barrier = threading.Barrier(4)

                def synchronized_analysis(*args, **kwargs):
                    result = original_analysis(*args, **kwargs)
                    barrier.wait(timeout=10)
                    return result

                with mock.patch.object(
                        audit, "_spm_analysis",
                        side_effect=synchronized_analysis), \
                    mock.patch.object(
                        contract, "_generator_section_bytes",
                        wraps=contract._generator_section_bytes) as raw_narrow, \
                    ThreadPoolExecutor(max_workers=4) as executor:
                    results = list(executor.map(
                        audit.leaf_generator_bindings, spms))

                self.assertEqual(
                    raw_narrow.call_count,
                    0,
                    "a worker lost its owned decode and fell back to an SPM read",
                )
                for rows, expected_guid in zip(results, expected_guids):
                    origins = {
                        row["generator_guid"]
                        for row in rows
                        if row["legacy_cluster_origin"]
                    }
                    self.assertEqual(origins, {expected_guid})

    def test_four_material_first_workers_do_not_overwrite_each_other(self):
        """Mirror the production order: material rows, then legacy bindings."""
        with tempfile.TemporaryDirectory() as temp:
            for iteration in range(10):
                work = []
                for index in range(4):
                    guid = f"material-first-{iteration}-{index}"
                    spm = Path(temp) / (
                        f"SK_material_first_{iteration}_{index}.spm")
                    write_spm(
                        spm,
                        legacy_marked=False,
                        extra_generators=generator(guid, "1", marked=True),
                    )
                    write_marker_receipt(spm, [guid])
                    work.append((spm, guid))

                barrier = threading.Barrier(4)

                def material_then_legacy(item):
                    spm, expected_guid = item
                    rows = audit.extract_material_image_refs(spm)
                    self.assertTrue(rows)
                    barrier.wait(timeout=10)
                    bindings = audit.leaf_generator_bindings(spm)
                    origins = {
                        row["generator_guid"]
                        for row in bindings
                        if row["legacy_cluster_origin"]
                    }
                    return origins, expected_guid

                with mock.patch.object(
                        contract, "_generator_section_bytes",
                        wraps=contract._generator_section_bytes) as raw_narrow, \
                    ThreadPoolExecutor(max_workers=4) as executor:
                    results = list(executor.map(material_then_legacy, work))

                self.assertEqual(raw_narrow.call_count, 0)
                for origins, expected_guid in results:
                    self.assertEqual(origins, {expected_guid})


class SchemaFourCompatibilityTests(SharedSnapshotTestCase):
    def _seed_schema_four_entry(self, spm):
        """Populate the persistent cache the way a pre-fix run would have,
        with leaf_binding_schema 4 and no legacy-marker fields at all."""
        analysis = audit._spm_analysis(spm)
        path_key, size, mtime_ns = audit._file_cache_key(spm)
        audit._PERSISTENT_SPM_ANALYSIS[path_key] = {
            "size": size,
            "mtime_ns": mtime_ns,
            "material_rows": analysis["material_rows"],
            "material_names": analysis["material_names"],
            "referenced_material_ids": sorted(analysis["active_material_ids"]),
            "visible_material_ids": sorted(analysis["visible_material_ids"]),
            "leaf_generator_bindings": analysis["leaf_generator_bindings"],
            "mesh_asset_ids": sorted(analysis["mesh_asset_ids"]),
            "leaf_binding_schema": 4,
        }
        audit._PERSISTENT_SPM_ANALYSIS_DIRTY = False
        self.reset_in_process_caches()

    def test_schema_four_disk_entry_is_reparsed_and_content_bound(self):
        with tempfile.TemporaryDirectory() as temp:
            spm = Path(temp) / "SK_tree_schema4.spm"
            write_spm(spm)
            self._seed_schema_four_entry(spm)

            with mock.patch.object(
                    audit, "read_maybe_gzip_text",
                    wraps=audit.read_maybe_gzip_text) as reader:
                analysis = audit._spm_analysis(spm)

            self.assertEqual(reader.call_count, 0)
            self.assertEqual(analysis["material_names"], ["M_leaf_cluster"])
            self.assertIsNone(analysis["generator_foregrounds_snapshot"])
            path_key, _, _ = audit._file_cache_key(spm)
            upgraded = audit._PERSISTENT_SPM_ANALYSIS[path_key]
            self.assertEqual(
                upgraded["content_identity_algorithm"],
                audit.SPM_CONTENT_IDENTITY_ALGORITHM,
            )
            self.assertEqual(len(upgraded["content_identity_sha256"]), 64)
            self.assertTrue(audit._PERSISTENT_SPM_ANALYSIS_DIRTY)

    def test_missing_snapshot_is_backfilled_lazily_then_never_reread(self):
        with tempfile.TemporaryDirectory() as temp:
            spm = Path(temp) / "SK_tree_backfill.spm"
            write_spm(spm)
            write_marker_receipt(spm, [LEGACY_GUID])
            self._seed_schema_four_entry(spm)

            with mock.patch.object(
                    contract, "_generator_section_bytes",
                    wraps=contract._generator_section_bytes) as raw_narrow:
                bindings = audit.leaf_generator_bindings(spm)
            self.assertEqual(raw_narrow.call_count, 0)
            self.assertTrue(
                next(row for row in bindings
                     if row["generator_guid"] == LEGACY_GUID)[
                    "legacy_cluster_origin"])

            path_key, _, _ = audit._file_cache_key(spm)
            entry = audit._PERSISTENT_SPM_ANALYSIS[path_key]
            self.assertEqual(entry["legacy_marker_schema"], 1)
            self.assertIn(LEGACY_GUID, entry["generator_foregrounds"])
            self.assertTrue(audit._PERSISTENT_SPM_ANALYSIS_DIRTY)
            # The stat-only row is upgraded while the same decoded handoff
            # supplies legacy-marker provenance.
            self.assertEqual(entry["leaf_binding_schema"], 6)
            self.assertEqual(
                entry["content_identity_algorithm"],
                audit.SPM_CONTENT_IDENTITY_ALGORITHM,
            )

            # A brand-new process reusing only the disk cache must not read
            # the SPM again for either half of the analysis.
            self.reset_in_process_caches()
            with mock.patch.object(
                    audit, "read_maybe_gzip_text",
                    wraps=audit.read_maybe_gzip_text) as reader, \
                mock.patch.object(
                    contract, "_generator_section_bytes",
                    wraps=contract._generator_section_bytes) as raw_narrow:
                warm_bindings = audit.leaf_generator_bindings(spm)
            self.assertEqual(reader.call_count, 0)
            self.assertEqual(raw_narrow.call_count, 0)
            self.assertEqual(
                {row["generator_guid"]: row["legacy_cluster_origin"]
                 for row in warm_bindings},
                {row["generator_guid"]: row["legacy_cluster_origin"]
                 for row in bindings},
            )


class HandoffLifecycleTests(SharedSnapshotTestCase):
    def test_aborted_folder_task_releases_unconsumed_decode(self):
        with tempfile.TemporaryDirectory() as temp:
            spm = Path(temp) / "SK_tree_aborted_folder.spm"
            write_spm(spm)

            def abort_after_material_parse(_folder):
                audit.extract_material_image_refs(spm)
                self.assertIsNotNone(
                    audit._PENDING_DECODED_HANDOFF.get())
                raise RuntimeError("forced folder cancellation")

            with self.assertRaisesRegex(
                    RuntimeError, "forced folder cancellation"):
                audit._audit_report_folders(
                    [Path(temp)], abort_after_material_parse)

            self.assertIsNone(audit._PENDING_DECODED_HANDOFF.get())

    def test_path_stat_change_rejects_handoff_with_stable_evidence(self):
        with tempfile.TemporaryDirectory() as temp:
            spm = Path(temp) / "SK_tree_changed_during_handoff.spm"
            write_spm(spm)
            write_marker_receipt(spm, [LEGACY_GUID])
            analysis, handoff = audit._spm_analysis(
                spm, include_decoded_handoff=True)

            # Simulate an external writer replacing the SPM after analysis but
            # before legacy inspection. The old decoded text and old binding
            # rows must not be paired with the new file's stat generation.
            write_spm(
                spm,
                extra_generators=generator(
                    "replacement-generation-guid", "1", marked=True),
            )

            state = contract.inspect_legacy_cluster_state(
                spm, decoded_handoff=handoff)
            self.assertEqual(
                state["reason_tokens"],
                [contract.HANDOFF_STAT_MISMATCH_REASON],
            )
            self.assertFalse(state["receipt_valid"])
            self.assertEqual(state["classified_generator_guids"], [])
            self.assertEqual(
                state["handoff_evidence"]["expected_spm_key"],
                list(handoff.spm_key),
            )
            self.assertNotEqual(
                state["handoff_evidence"]["actual_spm_key"],
                list(handoff.spm_key),
            )

            with mock.patch.object(
                    audit, "_spm_analysis",
                    return_value=(analysis, handoff)):
                with self.assertRaisesRegex(
                        RuntimeError,
                        contract.HANDOFF_STAT_MISMATCH_REASON):
                    audit.leaf_generator_bindings(spm)

            self.assertIsNone(contract._ACTIVE_DECODED_HANDOFF.get())
            self.assertIsNone(contract.peek_generator_foregrounds(spm))

    def test_interrupted_scan_releases_text_and_restart_recovers(self):
        with tempfile.TemporaryDirectory() as temp:
            spm = Path(temp) / "SK_tree_interrupted_handoff.spm"
            write_spm(spm)
            write_marker_receipt(spm, [LEGACY_GUID])
            _, handoff = audit._spm_analysis(
                spm, include_decoded_handoff=True)

            with mock.patch.object(
                    contract,
                    "generator_foregrounds_from_decoded_text",
                    side_effect=ValueError("forced interrupted snapshot scan")):
                state = contract.inspect_legacy_cluster_state(
                    spm, decoded_handoff=handoff)

            self.assertEqual(
                state["reason_tokens"],
                [contract.SNAPSHOT_SCAN_FAILED_REASON],
            )
            self.assertFalse(state["receipt_valid"])
            self.assertEqual(state["classified_generator_guids"], [])
            self.assertEqual(
                state["failure_evidence"]["exception_type"], "ValueError")
            self.assertIsNone(contract._ACTIVE_DECODED_HANDOFF.get())
            self.assertIsNone(contract.peek_generator_foregrounds(spm))

            # A restarted process has no transient handoff. Clearing only the
            # process caches must allow the authoritative file to be read and
            # classified normally, without inheriting the interrupted result.
            self.reset_in_process_caches()
            recovered = contract.inspect_legacy_cluster_state(spm)
            self.assertTrue(recovered["receipt_valid"])
            self.assertEqual(
                recovered["classified_generator_guids"], [LEGACY_GUID])
            self.assertEqual(recovered["reason_tokens"], [])


class WarmCachePerformanceTests(SharedSnapshotTestCase):
    def test_schema_six_warm_cache_has_zero_reads_and_faster_mean(self):
        with tempfile.TemporaryDirectory() as temp:
            spm = Path(temp) / "SK_tree_warm_gate.spm"
            write_spm(spm, padding_size=8 * 1024 * 1024)
            write_marker_receipt(spm, [LEGACY_GUID])

            cold_started = time.perf_counter()
            cold_bindings = audit.leaf_generator_bindings(spm)
            cold_elapsed = time.perf_counter() - cold_started

            path_key, _, _ = audit._file_cache_key(spm)
            entry = audit._PERSISTENT_SPM_ANALYSIS[path_key]
            self.assertEqual(entry["leaf_binding_schema"], 6)
            self.assertEqual(entry["legacy_marker_schema"], 1)

            self.reset_in_process_caches()
            with mock.patch.object(
                    audit, "read_maybe_gzip_text",
                    wraps=audit.read_maybe_gzip_text) as reader, \
                mock.patch.object(
                    contract, "_generator_section_bytes",
                    wraps=contract._generator_section_bytes) as raw_narrow:
                warm_started = time.perf_counter()
                warm_results = [
                    audit.leaf_generator_bindings(spm)
                    for _ in range(20)
                ]
                warm_elapsed = time.perf_counter() - warm_started

            self.assertEqual(reader.call_count, 0)
            self.assertEqual(raw_narrow.call_count, 0)
            self.assertTrue(all(result == cold_bindings
                                for result in warm_results))
            self.assertLess(
                warm_elapsed / 20,
                cold_elapsed,
                f"warm_mean={warm_elapsed / 20:.6f}s "
                f"cold={cold_elapsed:.6f}s",
            )


class PersistentCacheExitRaceTests(SharedSnapshotTestCase):
    def test_interrupted_atomic_replace_preserves_last_restart_authority(self):
        """Model process exit at the temp-file/replace boundary."""
        with tempfile.TemporaryDirectory() as temp:
            cache_path = Path(temp) / "spm_analysis_v5.json"
            previous_payload = {
                "version": 1,
                "entries": {"last-good": {"leaf_binding_schema": 5}},
            }
            cache_path.write_text(
                json.dumps(previous_payload), encoding="utf-8")

            old_path = audit.SPM_ANALYSIS_CACHE_PATH
            old_sbs = audit._PERSISTENT_SBS_GRAPHS
            old_sbs_dirty = audit._PERSISTENT_SBS_GRAPHS_DIRTY
            old_blend = audit._PERSISTENT_BLEND_IMAGES
            old_blend_dirty = audit._PERSISTENT_BLEND_IMAGES_DIRTY
            try:
                audit.SPM_ANALYSIS_CACHE_PATH = cache_path
                audit._PERSISTENT_SPM_ANALYSIS = {
                    "new-uncommitted": {"leaf_binding_schema": 5}
                }
                audit._PERSISTENT_SPM_ANALYSIS_DIRTY = True
                audit._PERSISTENT_SBS_GRAPHS = {}
                audit._PERSISTENT_SBS_GRAPHS_DIRTY = False
                audit._PERSISTENT_BLEND_IMAGES = {}
                audit._PERSISTENT_BLEND_IMAGES_DIRTY = False

                with mock.patch.object(
                        Path, "replace",
                        side_effect=OSError("forced process-exit boundary")):
                    with self.assertRaisesRegex(
                            OSError, "forced process-exit boundary"):
                        audit.save_spm_analysis_cache()

                self.assertTrue(audit._PERSISTENT_SPM_ANALYSIS_DIRTY)
                self.assertEqual(
                    json.loads(cache_path.read_text(encoding="utf-8")),
                    previous_payload,
                )

                # A restart ignores the abandoned temp file and reloads the
                # last atomically committed cache rather than partial data.
                audit._PERSISTENT_SPM_ANALYSIS = None
                self.assertEqual(
                    audit._persistent_spm_analysis(),
                    previous_payload["entries"],
                )
            finally:
                audit.SPM_ANALYSIS_CACHE_PATH = old_path
                audit._PERSISTENT_SBS_GRAPHS = old_sbs
                audit._PERSISTENT_SBS_GRAPHS_DIRTY = old_sbs_dirty
                audit._PERSISTENT_BLEND_IMAGES = old_blend
                audit._PERSISTENT_BLEND_IMAGES_DIRTY = old_blend_dirty


class InvalidationTests(SharedSnapshotTestCase):
    def test_receipt_change_reclassifies_without_rereading_the_spm(self):
        with tempfile.TemporaryDirectory() as temp:
            spm = Path(temp) / "SK_tree_receipt_change.spm"
            write_spm(spm)
            write_marker_receipt(spm, [LEGACY_GUID])
            first = audit.leaf_generator_bindings(spm)
            self.assertTrue(
                next(row for row in first
                     if row["generator_guid"] == LEGACY_GUID)[
                    "legacy_cluster_origin"])

            # Widen the receipt to also claim the plain generator's GUID.
            # mtime_ns must advance for the receipt's stat key to change.
            import time
            time.sleep(0.01)
            write_marker_receipt(spm, [LEGACY_GUID, PLAIN_GUID])

            with mock.patch.object(
                    contract, "_generator_section_bytes",
                    wraps=contract._generator_section_bytes) as raw_narrow:
                second = audit.leaf_generator_bindings(spm)
            self.assertEqual(raw_narrow.call_count, 0)
            self.assertTrue(
                next(row for row in second
                     if row["generator_guid"] == PLAIN_GUID)[
                    "legacy_cluster_origin"])

    def test_problem_receipt_change_updates_ambiguous_guids(self):
        with tempfile.TemporaryDirectory() as temp:
            spm = Path(temp) / "SK_tree_problem_marker.spm"
            # Magenta on a generator the marker receipt never classified:
            # ambiguous unless explained by an active problem-marker receipt.
            write_spm(spm, legacy_marked=True, extra_generators=generator(
                "ambiguous-guid", "1", marked=True))
            write_marker_receipt(spm, [LEGACY_GUID])
            state = contract.inspect_legacy_cluster_state(spm)
            self.assertIn("ambiguous-guid", state["ambiguous_marker_guids"])

            write_problem_receipt(spm, ["ambiguous-guid"])
            with mock.patch.object(
                    contract, "_generator_section_bytes",
                    wraps=contract._generator_section_bytes) as raw_narrow:
                updated = contract.inspect_legacy_cluster_state(spm)
            self.assertEqual(raw_narrow.call_count, 0)
            self.assertIn("ambiguous-guid", updated["problem_marker_guids"])
            self.assertNotIn(
                "ambiguous-guid", updated["ambiguous_marker_guids"])

    def test_spm_content_change_is_a_genuine_miss_and_reparses_once(self):
        with tempfile.TemporaryDirectory() as temp:
            spm = Path(temp) / "SK_tree_edited.spm"
            write_spm(spm)
            write_marker_receipt(spm, [LEGACY_GUID, "new-guid"])
            audit.leaf_generator_bindings(spm)

            import time
            time.sleep(0.01)
            write_spm(spm, extra_generators=generator(
                "new-guid", "1", marked=True))

            with mock.patch.object(
                    audit, "read_maybe_gzip_text",
                    wraps=audit.read_maybe_gzip_text) as reader:
                bindings = audit.leaf_generator_bindings(spm)
            self.assertEqual(reader.call_count, 0)
            self.assertTrue(
                next(row for row in bindings
                     if row["generator_guid"] == "new-guid")[
                    "legacy_cluster_origin"])


class EquivalenceTests(SharedSnapshotTestCase):
    def test_shared_path_matches_the_standalone_contract_result(self):
        with tempfile.TemporaryDirectory() as temp:
            spm = Path(temp) / "SK_tree_equivalence.spm"
            write_spm(spm, extra_generators=generator(
                "dup-guid", "1", marked=True) + generator(
                "dup-guid", "1", marked=True))
            write_marker_receipt(spm, [LEGACY_GUID, PLAIN_GUID, "dup-guid"])
            write_problem_receipt(spm, [PLAIN_GUID])

            audit.leaf_generator_bindings(spm)  # populate via the shared path
            via_shared_path = contract.inspect_legacy_cluster_state(spm)

            self.reset_in_process_caches()
            audit._PERSISTENT_SPM_ANALYSIS = {}
            standalone = contract.inspect_legacy_cluster_state(spm)

            for key in (
                "classified_generator_guids",
                "marker_drift_guids",
                "duplicate_generator_guids",
                "problem_marker_guids",
                "ambiguous_marker_guids",
                "missing_generator_guids",
                "receipt_valid",
            ):
                self.assertEqual(
                    via_shared_path[key], standalone[key], key)


if __name__ == "__main__":
    unittest.main()
