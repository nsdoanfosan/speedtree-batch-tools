"""Race and provenance regressions for stale Node-table recovery."""

import gzip
import json
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parents[2]
for candidate in (REPO_DIR, REPO_DIR / "pcg_st9_texture_batch"):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from pcg_cluster_assembly_contract import (  # noqa: E402
    _stale_node_table_recovery_contract,
)
from speedtree_pipeline_contract import (  # noqa: E402
    spm_authoring_graph_fingerprint,
)
from stale_node_table_recovery import (  # noqa: E402
    RECOVERY_CONTRACT,
    StaleNodeTableRecoveryError,
    StaleNodeTableRecoveryTimeout,
    _acquire_session_lock,
    _capture_immutable_snapshot,
    _ensure_preimage_artifacts,
    _release_session_lock,
    recover_stale_node_table,
)


TARGET_MESH_IDS = (130, 131, 132, 133)


def _node(guid):
    return (
        "<Node>"
        f"<GeneratorGUID>{guid}</GeneratorGUID>"
        "<ParentGUID></ParentGUID><Name>node</Name><GUID>node-guid</GUID>"
        "<Hidden>false</Hidden>"
        "<Extra><m_bDeleted>false</m_bDeleted><m_bCulled>false</m_bCulled></Extra>"
        "</Node>"
    )


def spm_text(
    *,
    stale,
    graph_property="1",
    link_source="root-guid",
    mesh_name_suffix="",
    volatile="one",
    missing_target_node=None,
):
    generators = [
        "<Generator Type=\"Tree\"><Name>Tree</Name><GUID>root-guid</GUID>"
        "<Hidden>false</Hidden><Properties></Properties></Generator>"
    ]
    links = []
    meshes = []
    for mesh_id in TARGET_MESH_IDS:
        generators.append(
            "<Generator Type=\"Frond\">"
            f"<Name>Leaf {mesh_id}</Name><GUID>g-{mesh_id}</GUID>"
            "<Hidden>false</Hidden><Properties>"
            "<Property><Name>Leaf:Material</Name><Value>10</Value></Property>"
            f"<Property><Name>Leaf:Mesh</Name><Value>{mesh_id}</Value></Property>"
            "<Property><Name>Custom:Density</Name>"
            f"<Value>{graph_property}</Value></Property>"
            "</Properties></Generator>"
        )
        links.append(
            "<Link>"
            f"<SourceGUID>{link_source}</SourceGUID>"
            f"<TargetGUID>g-{mesh_id}</TargetGUID>"
            "</Link>"
        )
        meshes.append(
            f"<Mesh ID=\"{mesh_id}\"><Name>mesh-{mesh_id}{mesh_name_suffix}</Name></Mesh>"
        )
    if stale:
        nodes = [_node("orphan-guid") for _ in TARGET_MESH_IDS]
    else:
        nodes = [
            _node(f"g-{mesh_id}")
            for mesh_id in TARGET_MESH_IDS
            if mesh_id != missing_target_node
        ]
    return (
        "<?xml version=\"1.0\" encoding=\"utf-8\"?>"
        "<SpeedTree>"
        f"<Thumbnail>{volatile}</Thumbnail><Preview>{volatile}</Preview>"
        f"<QuickSaveSettings2>{volatile}</QuickSaveSettings2>"
        f"<m_sTimelineData>{volatile}</m_sTimelineData>"
        f"<Generators>{''.join(generators)}</Generators>"
        f"<Links>{''.join(links)}</Links>"
        f"<Assets><Meshes>{''.join(meshes)}</Meshes></Assets>"
        f"<Nodes>{''.join(nodes)}</Nodes>"
        "</SpeedTree>"
    )


def write_spm(path, text):
    path.write_bytes(gzip.compress(text.encode("utf-8"), mtime=0))


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class ExitedProcess:
    pid = 1234

    def poll(self):
        return 0


def open_guards(state=None):
    if state is None:
        state = {}
    return {
        "is_cancelled": lambda: bool(state.get("cancelled")),
        "is_app_open": lambda: not bool(state.get("app_closed")),
        "is_job_current": lambda: not bool(state.get("job_stale")),
    }


class RecoveryTestCase(unittest.TestCase):
    def make_files(self, folder):
        spm = folder / "model.spm"
        executable = folder / "SpeedTree_Modeler.exe"
        write_spm(spm, spm_text(stale=True))
        executable.write_bytes(b"stub executable")
        recovery_root = folder / "recovery"
        return spm, executable, recovery_root

    def recover_with_save(
        self,
        spm,
        executable,
        recovery_root,
        *,
        after_text=None,
        retry=None,
        job_id=None,
        generation=None,
        guards=None,
        capture_fn=_capture_immutable_snapshot,
        launch_observer=None,
        timeout=10,
    ):
        clock = FakeClock()
        after_text = after_text or spm_text(stale=False, volatile="two")

        def launch(exe, path):
            if launch_observer:
                launch_observer(exe, path)
            write_spm(spm, after_text)
            return ExitedProcess()

        return recover_stale_node_table(
            spm,
            executable,
            TARGET_MESH_IDS,
            timeout=timeout,
            poll_interval=1,
            stable_reads=2,
            retry=retry,
            job_id=job_id,
            job_generation=generation,
            guards=guards,
            recovery_root=recovery_root,
            capture_fn=capture_fn,
            launch_fn=launch,
            sleep_fn=clock.sleep,
            monotonic_fn=clock.monotonic,
        )


class OriginalFailureAndProjectionTests(RecoveryTestCase):
    def test_original_stale_blackgum_failure_shape_is_deterministic(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            spm, _executable, _root = self.make_files(folder)
            first = _capture_immutable_snapshot(spm, TARGET_MESH_IDS)
            second = _capture_immutable_snapshot(spm, TARGET_MESH_IDS)

            for snapshot in (first, second):
                self.assertTrue(snapshot["delivery"]["node_table"]["stale"])
                self.assertEqual(
                    snapshot["delivery"]["node_table"]["orphan_node_count"],
                    4,
                )
                self.assertTrue(snapshot["target_projection"]["complete"])
                self.assertTrue(snapshot["regex_elementtree_parity"])
                self.assertEqual(
                    snapshot["normalization"]["delivery_reason"],
                    "live_export_evidence_unavailable_stale_node_table",
                )
                self.assertFalse(snapshot["normalization"]["complete"])
            self.assertEqual(first["raw_sha256"], second["raw_sha256"])
            self.assertEqual(
                first["authoring_graph_fingerprint"],
                second["authoring_graph_fingerprint"],
            )

    def test_authoring_projection_ignores_only_nodes_and_known_volatile_data(self):
        baseline = spm_text(stale=True, volatile="one")
        resaved = spm_text(stale=False, volatile="two")
        self.assertEqual(
            spm_authoring_graph_fingerprint(baseline),
            spm_authoring_graph_fingerprint(resaved),
        )
        for changed in (
            spm_text(stale=False, graph_property="2"),
            spm_text(stale=False, link_source="another-root"),
            spm_text(stale=False, mesh_name_suffix="-changed"),
        ):
            self.assertNotEqual(
                spm_authoring_graph_fingerprint(baseline),
                spm_authoring_graph_fingerprint(changed),
            )
        nested_nodes_a = baseline.replace(
            "</Assets>",
            "<StructuralGraph><Nodes><Point>A</Point></Nodes></StructuralGraph></Assets>",
        )
        nested_nodes_b = baseline.replace(
            "</Assets>",
            "<StructuralGraph><Nodes><Point>B</Point></Nodes></StructuralGraph></Assets>",
        )
        self.assertNotEqual(
            spm_authoring_graph_fingerprint(nested_nodes_a),
            spm_authoring_graph_fingerprint(nested_nodes_b),
        )

    def test_contract_publishes_every_forbidden_and_required_boundary(self):
        contract = _stale_node_table_recovery_contract()
        self.assertEqual(contract["schema_version"], 2)
        self.assertFalse(contract["modeler_auto_save"])
        self.assertFalse(contract["modeler_process_kill"])
        self.assertFalse(contract["direct_spm_xml_edit"])
        self.assertFalse(contract["ui_input_simulation"])
        self.assertFalse(contract["automatic_rollback"])
        self.assertFalse(contract["stale_false_alone_allows_retry"])
        self.assertTrue(contract["requires_exact_preimage_backup"])
        self.assertTrue(contract["requires_immutable_preimage_receipt"])
        self.assertTrue(contract["source_sha_rechecked_before_continuation"])
        self.assertTrue(contract["continuation_once_only"])
        self.assertFalse(contract["queue_or_manifest_mutation_before_continuation"])


class PreimageAndReceiptTests(RecoveryTestCase):
    def test_exact_backup_and_immutable_receipt_exist_before_modeler_launch(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            spm, executable, root = self.make_files(folder)
            preimage = spm.read_bytes()

            def assert_sealed(_exe, _path):
                backups = list(root.glob("*.preimage.spm"))
                receipts = list(root.glob("*.receipt.json"))
                self.assertEqual(len(backups), 1)
                self.assertEqual(len(receipts), 1)
                self.assertEqual(backups[0].read_bytes(), preimage)
                receipt_text = receipts[0].read_text(encoding="utf-8")
                receipt = json.loads(receipt_text)
                self.assertEqual(
                    receipt["authoring_graph_projection"]["version"], 1
                )
                self.assertEqual(receipt["generator_membership"]["version"], 1)
                self.assertEqual(receipt["required_target_bindings"]["version"], 1)
                self.assertNotIn(str(folder), receipt_text)
                self.assertNotIn("g-130", receipt_text)

            result = self.recover_with_save(
                spm,
                executable,
                root,
                launch_observer=assert_sealed,
            )
            self.assertEqual(result["contract"], RECOVERY_CONTRACT)
            self.assertEqual(result["status"], "repaired_reaudit_valid")
            self.assertTrue(result["reaudit"]["authoring_graph_continuity"])
            self.assertTrue(result["reaudit"]["regex_elementtree_parity"])
            self.assertTrue(result["reaudit"]["normalization"]["complete"])
            self.assertEqual(
                result["reaudit"]["normalization"]["live_snapshot_sha256"],
                result["after_sha256"],
            )

    def test_corrupt_backup_or_receipt_blocks_before_launch(self):
        for corrupt in ("backup", "receipt"):
            with self.subTest(corrupt=corrupt), tempfile.TemporaryDirectory() as temporary:
                folder = Path(temporary)
                spm, executable, root = self.make_files(folder)
                root.mkdir()
                baseline = _capture_immutable_snapshot(spm, TARGET_MESH_IDS)
                artifacts = _ensure_preimage_artifacts(
                    baseline, TARGET_MESH_IDS, root
                )
                if corrupt == "backup":
                    artifacts["backup_path"].write_bytes(b"corrupt")
                    token = "preimage_backup_verification_failed"
                else:
                    artifacts["receipt_path"].write_text("{}", encoding="utf-8")
                    token = "preimage_receipt_verification_failed"
                launched = []
                with self.assertRaises(StaleNodeTableRecoveryError) as caught:
                    recover_stale_node_table(
                        spm,
                        executable,
                        TARGET_MESH_IDS,
                        recovery_root=root,
                        launch_fn=lambda *_args: launched.append(True),
                    )
                self.assertEqual(caught.exception.reason_token, token)
                self.assertEqual(launched, [])

    def test_interrupted_backup_only_restart_rebuilds_receipt_without_overwrite(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            spm, executable, root = self.make_files(folder)
            root.mkdir()
            baseline = _capture_immutable_snapshot(spm, TARGET_MESH_IDS)
            artifacts = _ensure_preimage_artifacts(baseline, TARGET_MESH_IDS, root)
            backup_bytes = artifacts["backup_path"].read_bytes()
            artifacts["receipt_path"].unlink()

            result = self.recover_with_save(spm, executable, root)
            self.assertEqual(result["status"], "repaired_reaudit_valid")
            self.assertEqual(artifacts["backup_path"].read_bytes(), backup_bytes)
            self.assertTrue(artifacts["receipt_path"].is_file())

    def test_source_change_between_seal_and_launch_blocks(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            spm, executable, root = self.make_files(folder)
            calls = 0

            def racing_capture(path, expected):
                nonlocal calls
                calls += 1
                captured = _capture_immutable_snapshot(path, expected)
                if calls == 1:
                    write_spm(spm, spm_text(stale=True, graph_property="2"))
                return captured

            launched = []
            with self.assertRaises(StaleNodeTableRecoveryError) as caught:
                recover_stale_node_table(
                    spm,
                    executable,
                    TARGET_MESH_IDS,
                    recovery_root=root,
                    capture_fn=racing_capture,
                    launch_fn=lambda *_args: launched.append(True),
                )
            self.assertEqual(
                caught.exception.reason_token,
                "source_changed_before_modeler_launch",
            )
            self.assertEqual(launched, [])


class QuiescenceAndGraphGateTests(RecoveryTestCase):
    def test_graph_change_and_stale_false_alone_never_continue(self):
        for after_text, expected_reason in (
            (
                spm_text(stale=False, graph_property="2"),
                "authoring_graph_changed_during_resave",
            ),
            (
                spm_text(stale=False, missing_target_node=133),
                "target_binding_has_no_eligible_nodes",
            ),
        ):
            with self.subTest(reason=expected_reason), tempfile.TemporaryDirectory() as temporary:
                folder = Path(temporary)
                spm, executable, root = self.make_files(folder)
                with self.assertRaises(StaleNodeTableRecoveryTimeout) as caught:
                    self.recover_with_save(
                        spm,
                        executable,
                        root,
                        after_text=after_text,
                        timeout=3,
                    )
                self.assertEqual(
                    caught.exception.reason_token,
                    "valid_resave_quiescence_timeout",
                )
                self.assertIn(
                    expected_reason,
                    caught.exception.evidence["last_reason_tokens"],
                )
                events = list(root.glob("blocked.*.json"))
                self.assertEqual(len(events), 1)
                event_text = events[0].read_text(encoding="utf-8")
                event = json.loads(event_text)
                self.assertEqual(event["asset_name"], "model.spm")
                self.assertRegex(event["after_sha256"], r"^[0-9a-f]{64}$")
                self.assertNotIn(str(folder), event_text)
                self.assertNotIn("g-130", event_text)

    def test_transient_changed_snapshot_must_be_replaced_by_stable_valid_bytes(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            spm, executable, root = self.make_files(folder)
            capture_calls = 0

            def evolving_capture(path, expected):
                nonlocal capture_calls
                capture_calls += 1
                captured = _capture_immutable_snapshot(path, expected)
                if capture_calls == 3:
                    write_spm(spm, spm_text(stale=False, volatile="final"))
                return captured

            result = self.recover_with_save(
                spm,
                executable,
                root,
                after_text=spm_text(stale=False, graph_property="2"),
                capture_fn=evolving_capture,
            )
            self.assertEqual(result["status"], "repaired_reaudit_valid")
            self.assertTrue(result["reaudit"]["valid"])

    def test_process_exit_never_substitutes_for_a_saved_quiescent_file(self):
        for iteration in range(10):
            with self.subTest(iteration=iteration), tempfile.TemporaryDirectory() as temporary:
                folder = Path(temporary)
                spm, executable, root = self.make_files(folder)
                clock = FakeClock()
                retried = []
                with self.assertRaises(StaleNodeTableRecoveryTimeout) as caught:
                    recover_stale_node_table(
                        spm,
                        executable,
                        TARGET_MESH_IDS,
                        timeout=2,
                        poll_interval=1,
                        stable_reads=2,
                        retry=lambda value: retried.append(value),
                        job_id="job",
                        job_generation=1,
                        guards=open_guards(),
                        recovery_root=root,
                        launch_fn=lambda *_args: ExitedProcess(),
                        sleep_fn=clock.sleep,
                        monotonic_fn=clock.monotonic,
                    )
                self.assertEqual(
                    caught.exception.reason_token,
                    "valid_resave_quiescence_timeout",
                )
                self.assertEqual(retried, [])


class ContinuationAndRaceTests(RecoveryTestCase):
    def test_source_sha_is_rechecked_immediately_before_continuation(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            spm, executable, root = self.make_files(folder)
            calls = 0
            retried = []

            def last_moment_change(path, expected):
                nonlocal calls
                calls += 1
                if calls == 5:
                    write_spm(spm, spm_text(stale=False, graph_property="9"))
                return _capture_immutable_snapshot(path, expected)

            with self.assertRaises(StaleNodeTableRecoveryError) as caught:
                self.recover_with_save(
                    spm,
                    executable,
                    root,
                    retry=lambda value: retried.append(value),
                    job_id="job",
                    generation=7,
                    guards=open_guards(),
                    capture_fn=last_moment_change,
                )
            self.assertEqual(
                caught.exception.reason_token,
                "source_changed_before_continuation",
            )
            self.assertEqual(retried, [])
            self.assertRegex(
                caught.exception.evidence["after_sha256"],
                r"^[0-9a-f]{64}$",
            )

    def test_preimage_receipt_removed_after_save_blocks_continuation(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            spm, executable, root = self.make_files(folder)
            calls = 0
            retried = []

            def remove_receipt_after_quiescence(path, expected):
                nonlocal calls
                calls += 1
                captured = _capture_immutable_snapshot(path, expected)
                if calls == 4:
                    next(root.glob("*.receipt.json")).unlink()
                return captured

            with self.assertRaises(StaleNodeTableRecoveryError) as caught:
                self.recover_with_save(
                    spm,
                    executable,
                    root,
                    retry=lambda value: retried.append(value),
                    job_id="job",
                    generation=8,
                    guards=open_guards(),
                    capture_fn=remove_receipt_after_quiescence,
                )
            self.assertEqual(
                caught.exception.reason_token,
                "preimage_artifacts_missing_or_unreadable",
            )
            self.assertEqual(retried, [])

    def test_cancel_app_close_and_stale_generation_guards_block_callback(self):
        cases = (
            ("cancelled", "initiating_job_cancelled"),
            ("app_closed", "initiating_app_closed"),
            ("job_stale", "initiating_job_generation_stale"),
        )
        for state_key, reason in cases:
            with self.subTest(reason=reason), tempfile.TemporaryDirectory() as temporary:
                folder = Path(temporary)
                spm, executable, root = self.make_files(folder)
                state = {}
                retried = []

                def mark_guard_after_launch(_exe, _path):
                    state[state_key] = True

                with self.assertRaises(StaleNodeTableRecoveryError) as caught:
                    self.recover_with_save(
                        spm,
                        executable,
                        root,
                        retry=lambda value: retried.append(value),
                        job_id="job",
                        generation=2,
                        guards=open_guards(state),
                        launch_observer=mark_guard_after_launch,
                    )
                self.assertEqual(caught.exception.reason_token, reason)
                self.assertEqual(retried, [])

    def test_retry_requires_complete_job_generation_and_guard_context(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            spm, executable, root = self.make_files(folder)
            launched = []
            with self.assertRaises(StaleNodeTableRecoveryError) as caught:
                recover_stale_node_table(
                    spm,
                    executable,
                    TARGET_MESH_IDS,
                    retry=lambda _value: None,
                    recovery_root=root,
                    launch_fn=lambda *_args: launched.append(True),
                )
            self.assertEqual(
                caught.exception.reason_token,
                "continuation_context_incomplete",
            )
            self.assertEqual(launched, [])

    def test_same_job_generation_and_after_sha_is_claimed_exactly_once(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            spm, executable, root = self.make_files(folder)
            original = spm.read_bytes()
            retried = []
            first = self.recover_with_save(
                spm,
                executable,
                root,
                retry=lambda value: retried.append(value) or "ok",
                job_id="job-42",
                generation=3,
                guards=open_guards(),
            )
            self.assertEqual(first["status"], "repaired_reaudited_and_retried_once")
            self.assertEqual(len(retried), 1)

            spm.write_bytes(original)
            with self.assertRaises(StaleNodeTableRecoveryError) as caught:
                self.recover_with_save(
                    spm,
                    executable,
                    root,
                    retry=lambda value: retried.append(value),
                    job_id="job-42",
                    generation=3,
                    guards=open_guards(),
                )
            self.assertEqual(
                caught.exception.reason_token,
                "continuation_already_claimed",
            )
            self.assertEqual(len(retried), 1)

    def test_callback_failure_is_claimed_and_never_automatically_replayed(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            spm, executable, root = self.make_files(folder)
            original = spm.read_bytes()
            attempts = []

            def failing(value):
                attempts.append(value)
                raise RuntimeError("downstream failed")

            with self.assertRaises(StaleNodeTableRecoveryError) as caught:
                self.recover_with_save(
                    spm,
                    executable,
                    root,
                    retry=failing,
                    job_id="job",
                    generation=4,
                    guards=open_guards(),
                )
            self.assertEqual(
                caught.exception.reason_token,
                "continuation_callback_failed",
            )
            self.assertEqual(len(attempts), 1)
            spm.write_bytes(original)
            with self.assertRaises(StaleNodeTableRecoveryError) as second:
                self.recover_with_save(
                    spm,
                    executable,
                    root,
                    retry=failing,
                    job_id="job",
                    generation=4,
                    guards=open_guards(),
                )
            self.assertEqual(
                second.exception.reason_token,
                "continuation_already_claimed",
            )
            self.assertEqual(len(attempts), 1)

    def test_concurrent_or_interrupted_session_lock_fails_closed_repeatedly(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            spm, executable, root = self.make_files(folder)
            root.mkdir()
            identity = _capture_immutable_snapshot(
                spm, TARGET_MESH_IDS
            )["source_identity"]
            lock, token = _acquire_session_lock(root, identity)
            launched = []
            try:
                for _iteration in range(20):
                    with self.assertRaises(StaleNodeTableRecoveryError) as caught:
                        recover_stale_node_table(
                            spm,
                            executable,
                            TARGET_MESH_IDS,
                            recovery_root=root,
                            launch_fn=lambda *_args: launched.append(True),
                        )
                    self.assertEqual(
                        caught.exception.reason_token,
                        "recovery_session_already_active",
                    )
            finally:
                _release_session_lock(lock, token)
            self.assertEqual(launched, [])

    def test_simultaneous_lock_contenders_have_one_winner_repeatedly(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            spm, _executable, root = self.make_files(folder)
            root.mkdir()
            identity = _capture_immutable_snapshot(
                spm, TARGET_MESH_IDS
            )["source_identity"]
            for iteration in range(20):
                with self.subTest(iteration=iteration):
                    barrier = threading.Barrier(2)

                    def contender():
                        barrier.wait()
                        try:
                            lock, token = _acquire_session_lock(root, identity)
                            return "winner", lock, token
                        except StaleNodeTableRecoveryError as exc:
                            return "blocked", exc.reason_token, None

                    with ThreadPoolExecutor(max_workers=2) as executor:
                        results = list(executor.map(lambda _value: contender(), range(2)))
                    winners = [row for row in results if row[0] == "winner"]
                    blocked = [row for row in results if row[0] == "blocked"]
                    self.assertEqual(len(winners), 1)
                    self.assertEqual(len(blocked), 1)
                    self.assertEqual(
                        blocked[0][1],
                        "recovery_session_already_active",
                    )
                    _release_session_lock(winners[0][1], winners[0][2])

    def test_already_repaired_does_not_launch_backup_or_retry(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            spm, executable, root = self.make_files(folder)
            write_spm(spm, spm_text(stale=False))
            launched = []
            retried = []
            result = recover_stale_node_table(
                spm,
                executable,
                TARGET_MESH_IDS,
                retry=lambda value: retried.append(value),
                job_id="job",
                job_generation=1,
                guards=open_guards(),
                recovery_root=root,
                launch_fn=lambda *_args: launched.append(True),
            )
            self.assertEqual(result["status"], "already_repaired")
            self.assertEqual(result["closure_gate"], "operational_snapshot_valid_only")
            self.assertEqual(launched, [])
            self.assertEqual(retried, [])
            self.assertEqual(list(root.glob("*.preimage.spm")), [])


if __name__ == "__main__":
    unittest.main()
