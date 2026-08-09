import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


TOOL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOL_DIR))

from connected_run import (  # noqa: E402
    RetryPlanInvalid,
    classify_failure,
    connected_unit_records,
    execute_with_bounded_publish_retry,
    legacy_or_current_summary,
    load_exact_report,
    new_unit_results,
    probe_cluster_producer_identity,
    report_file_identity,
    scope_dependency_identities,
    selected_failed_units,
    shared_queue_result,
    update_unit_result,
    validate_failed_retry_plan,
    validate_preserved_unit_identities,
    validate_queue_anchored_report,
)


FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "issue101_connected_partial_run.json"
)


def retryable_publish_error(message="Permission denied: registry.json.tmp"):
    error = PermissionError(message)
    error.connected_retry_contract = {
        "operation_phase": "atomic_json_publish",
        "committed": False,
        "rollback_succeeded": True,
        "temporary_output_isolated": True,
        "error_code": 13,
    }
    return error


class ConnectedRunContractTests(unittest.TestCase):
    def test_production_shaped_fixture_preserves_partial_counts_and_nine_units(self):
        payload = json.loads(FIXTURE.read_text(encoding="utf-8"))

        self.assertEqual(payload["status"], "partial")
        self.assertEqual(
            legacy_or_current_summary(payload),
            {
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
        )
        failed = selected_failed_units(payload)
        self.assertEqual(len(failed), 9)
        self.assertEqual(
            {unit["unit_id"] for unit in failed},
            {"generator_sync:g12"}
            | {f"cluster_refresh:c{index}" for index in range(32, 40)},
        )

    def test_fixture_failure_classes_map_to_existing_issues_without_duplicates(self):
        payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
        classes = [
            classify_failure(failure["reason"])
            for failure in payload["failures"]
        ]

        self.assertEqual(
            [item["category"] for item in classes],
            [
                "asset_manifest_repair_required",
                "transient_retryable_publish_lock",
                "existing_software_issue_recurrence",
                "existing_software_issue_recurrence",
                "asset_manifest_repair_required",
                "existing_software_issue_recurrence",
                "existing_software_issue_recurrence",
                "existing_software_issue_recurrence",
                "transient_retryable_publish_lock",
            ],
        )
        mapped = [
            item["mapping"].get("issue", {}).get("number")
            for item in classes
        ]
        self.assertEqual(mapped.count(58), 3)
        self.assertEqual(mapped.count(69), 2)
        self.assertEqual(mapped.count(101), 2)
        self.assertTrue(
            all(
                item["mapping"].get("duplicate_issue_required") is False
                for item in (classes[0], classes[4])
            )
        )

    def test_json_permission_publish_retries_bounded_then_succeeds(self):
        attempts = []
        sleeps = []

        def action():
            attempts.append(len(attempts) + 1)
            if len(attempts) < 3:
                raise retryable_publish_error(
                    "Permission denied: X:/fixture/registry.json.tmp"
                )
            return {"status": "ok"}

        outcome = execute_with_bounded_publish_retry(
            action,
            capture_identity=lambda: {"digest": "same"},
            ownership_is_current=lambda: True,
            sleep=sleeps.append,
        )

        self.assertTrue(outcome["ok"])
        self.assertEqual(attempts, [1, 2, 3])
        self.assertEqual(sleeps, [0.2, 0.5])

    def test_every_retry_attempt_has_durable_boundary_events_and_owner_probe(self):
        actions = []
        owners = []
        events = []

        def action():
            actions.append(len(actions) + 1)
            if len(actions) == 1:
                raise retryable_publish_error()
            return {"status": "ok"}

        outcome = execute_with_bounded_publish_retry(
            action,
            capture_identity=lambda: {"digest": "same", "stable": True},
            ownership_is_current=lambda: owners.append(True) or True,
            sleep=lambda _delay: None,
            on_attempt_event=events.append,
        )

        self.assertTrue(outcome["ok"])
        self.assertEqual(actions, [1, 2])
        self.assertEqual(owners, [True, True, True, True, True, True])
        self.assertEqual(
            [event["event"] for event in events],
            ["attempt_started", "attempt_failed", "attempt_started"],
        )
        self.assertEqual(events[0]["pre_attempt_dependency_identity"]["digest"], "same")

    def test_persistent_json_permission_denial_remains_actionable(self):
        attempts = []
        outcome = execute_with_bounded_publish_retry(
            lambda: (
                attempts.append(1),
                (_ for _ in ()).throw(
                    retryable_publish_error(
                        "registry publish Permission denied: manifest.json"
                    )
                ),
            )[-1],
            capture_identity=lambda: {"digest": "same"},
            ownership_is_current=lambda: True,
            sleep=lambda _delay: None,
        )

        self.assertFalse(outcome["ok"])
        self.assertEqual(len(attempts), 3)
        self.assertEqual(len(outcome["attempts"]), 3)
        self.assertTrue(outcome["retry_exhausted"])
        self.assertEqual(
            outcome["classification"]["category"],
            "transient_retryable_publish_lock",
        )

    def test_json_read_permission_without_publish_context_is_not_retried(self):
        attempts = []

        def action():
            attempts.append(1)
            raise PermissionError("Permission denied while reading input.json")

        outcome = execute_with_bounded_publish_retry(
            action,
            capture_identity=lambda: {"digest": "same"},
            ownership_is_current=lambda: True,
            sleep=lambda _delay: self.fail("read denial must not retry"),
        )

        self.assertFalse(outcome["ok"])
        self.assertEqual(attempts, [1])
        self.assertEqual(
            outcome["classification"]["category"],
            "non_retryable_internal_contract_failure",
        )

    def test_publish_words_without_structured_contract_never_authorize_retry(self):
        attempts = []

        def action():
            attempts.append(1)
            raise PermissionError(
                "Permission denied while publishing registry.json.tmp"
            )

        outcome = execute_with_bounded_publish_retry(
            action,
            capture_identity=lambda: {"digest": "same", "stable": True},
            ownership_is_current=lambda: True,
            sleep=lambda _delay: self.fail("message text must not authorize retry"),
        )

        self.assertFalse(outcome["ok"])
        self.assertEqual(attempts, [1])
        self.assertFalse(outcome["classification"]["automatic_retry"])

    def test_lease_loss_during_backoff_prevents_the_next_attempt(self):
        attempts = []
        owners = iter((True, True, True, False))

        def action():
            attempts.append(1)
            raise retryable_publish_error()

        outcome = execute_with_bounded_publish_retry(
            action,
            capture_identity=lambda: {"digest": "same", "stable": True},
            ownership_is_current=lambda: next(owners),
            sleep=lambda _delay: None,
        )

        self.assertFalse(outcome["ok"])
        self.assertEqual(attempts, [1])
        self.assertIn("ownership_lost", outcome["reason"])

    def test_lease_loss_during_identity_capture_blocks_mutation(self):
        attempts = []
        owner = {"current": True}

        def capture():
            owner["current"] = False
            return {"digest": "same", "stable": True}

        outcome = execute_with_bounded_publish_retry(
            lambda: attempts.append(1),
            capture_identity=capture,
            ownership_is_current=lambda: owner["current"],
            expected_identity={"digest": "same", "stable": True},
        )

        self.assertFalse(outcome["ok"])
        self.assertEqual(attempts, [])
        self.assertIn("ownership_lost", outcome["reason"])

    def test_lease_loss_during_running_checkpoint_blocks_mutation(self):
        attempts = []
        owner = {"current": True}

        outcome = execute_with_bounded_publish_retry(
            lambda: attempts.append(1),
            capture_identity=lambda: {"digest": "same", "stable": True},
            ownership_is_current=lambda: owner["current"],
            expected_identity={"digest": "same", "stable": True},
            on_attempt_event=lambda _event: owner.update(current=False),
        )

        self.assertFalse(outcome["ok"])
        self.assertEqual(attempts, [])
        self.assertIn("ownership_lost", outcome["reason"])

    def test_dependency_drift_during_backoff_blocks_next_attempt(self):
        attempts = []
        state = {"digest": "same"}

        def action():
            attempts.append(1)
            raise retryable_publish_error()

        outcome = execute_with_bounded_publish_retry(
            action,
            capture_identity=lambda: {
                "digest": state["digest"],
                "stable": True,
            },
            ownership_is_current=lambda: True,
            sleep=lambda _delay: state.update(digest="changed"),
        )

        self.assertFalse(outcome["ok"])
        self.assertEqual(attempts, [1])
        self.assertIn("content_drift", outcome["reason"])

    def test_rollback_failure_is_never_retried_even_with_publish_permission_text(self):
        attempts = []

        def action():
            attempts.append(1)
            raise RuntimeError(
                "[transaction_rollback_failed] registry publish Permission "
                "denied: registry.json.tmp"
            )

        outcome = execute_with_bounded_publish_retry(
            action,
            capture_identity=lambda: {"digest": "same", "stable": True},
            ownership_is_current=lambda: True,
            sleep=lambda _delay: self.fail("rollback failure must not retry"),
        )

        self.assertFalse(outcome["ok"])
        self.assertEqual(attempts, [1])
        self.assertEqual(
            outcome["classification"]["mapping"]["kind"],
            "rollback_integrity_failure",
        )

    def test_unstable_dependency_identity_blocks_before_action(self):
        attempts = []
        outcome = execute_with_bounded_publish_retry(
            lambda: attempts.append(1),
            capture_identity=lambda: {"digest": "unstable", "stable": False},
            ownership_is_current=lambda: True,
        )

        self.assertFalse(outcome["ok"])
        self.assertEqual(attempts, [])
        self.assertIn("dependency_unstable", outcome["reason"])

    def test_publish_retry_stops_if_content_changes(self):
        identities = iter(({"digest": "before"}, {"digest": "after"}))
        outcome = execute_with_bounded_publish_retry(
            lambda: (_ for _ in ()).throw(
                retryable_publish_error(
                    "Permission denied: registry.json.tmp"
                )
            ),
            capture_identity=lambda: next(identities),
            ownership_is_current=lambda: True,
            sleep=lambda _delay: self.fail("drift must stop before sleep"),
        )

        self.assertFalse(outcome["ok"])
        self.assertIn("content_drift", outcome["reason"])
        self.assertEqual(
            outcome["classification"]["category"],
            "stale_or_superseded_evidence",
        )

    def test_retry_plan_selects_only_failed_unit_and_any_content_drift_invalidates(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = root / "first"
            second = root / "second"
            first.mkdir()
            second.mkdir()
            for folder, name, content in (
                (first, "master_a.spm", b"master-a"),
                (first, "follower_a.spm", b"follower-a"),
                (first, "spm_generator_sync.json", b"{}"),
                (second, "master_b.spm", b"master-b"),
                (second, "follower_b.spm", b"follower-b"),
                (second, "spm_generator_sync.json", b"{}"),
            ):
                (folder / name).write_bytes(content)
            groups = [
                {
                    "folder": first,
                    "master": "master_a.spm",
                    "names": ["follower_a.spm"],
                },
                {
                    "folder": second,
                    "master": "master_b.spm",
                    "names": ["follower_b.spm"],
                },
            ]
            units = connected_unit_records(groups, [])
            identities = scope_dependency_identities(units, {"verify": False})
            unit_results = new_unit_results(units, identities)
            update_unit_result(
                unit_results,
                units[0]["unit_id"],
                outcome="succeeded",
            )
            update_unit_result(
                unit_results,
                units[1]["unit_id"],
                outcome="failed",
            )
            for entry in unit_results:
                entry["dependency_identity"] = identities[entry["unit_id"]]
            report = {"schema_version": 2, "unit_results": unit_results}

            plan = validate_failed_retry_plan(
                report,
                units,
                {"verify": False},
            )
            self.assertEqual(
                [unit["unit_id"] for unit in plan["units"]],
                [units[1]["unit_id"]],
            )

            (first / "master_a.spm").write_bytes(b"successful-unit-drift")
            with self.assertRaisesRegex(
                RetryPlanInvalid,
                "dependency identity changed",
            ):
                validate_failed_retry_plan(
                    report,
                    units,
                    {"verify": False},
                )

    def test_retry_plan_rejects_changed_order_even_with_same_unit_ids(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name in (
                "master_a.spm",
                "follower_a.spm",
                "master_b.spm",
                "follower_b.spm",
                "spm_generator_sync.json",
            ):
                (root / name).write_bytes(name.encode("utf-8"))
            groups = [
                {"folder": root, "master": "master_a.spm", "names": ["follower_a.spm"]},
                {"folder": root, "master": "master_b.spm", "names": ["follower_b.spm"]},
            ]
            units = connected_unit_records(groups, [])
            identities = scope_dependency_identities(units, {})
            results = new_unit_results(units, identities)
            for entry in results:
                entry["outcome"] = "failed"
                entry["dependency_identity"] = identities[entry["unit_id"]]
            report = {"schema_version": 2, "unit_results": results}

            with self.assertRaisesRegex(RetryPlanInvalid, "ordered unit plan"):
                validate_failed_retry_plan(
                    report,
                    connected_unit_records(list(reversed(groups)), []),
                    {},
                )

    def test_post_attempt_validation_protects_prior_success_from_overlap(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name in (
                "master_a.spm",
                "follower_a.spm",
                "master_b.spm",
                "follower_b.spm",
                "spm_generator_sync.json",
            ):
                (root / name).write_bytes(name.encode("utf-8"))
            groups = [
                {"folder": root, "master": "master_a.spm", "names": ["follower_a.spm"]},
                {"folder": root, "master": "master_b.spm", "names": ["follower_b.spm"]},
            ]
            units = connected_unit_records(groups, [])
            identities = scope_dependency_identities(units, {})
            protected = {units[0]["unit_id"]: identities[units[0]["unit_id"]]}

            validate_preserved_unit_identities(
                units,
                [unit["unit_id"] for unit in units],
                protected,
                {},
            )
            (root / "follower_a.spm").write_bytes(b"overlap-drift")
            with self.assertRaisesRegex(RetryPlanInvalid, "protected successful unit changed"):
                validate_preserved_unit_identities(
                    units,
                    [unit["unit_id"] for unit in units],
                    protected,
                    {},
                )

    def test_cluster_manifest_or_scope_receipt_drift_invalidates_retry(self):
        with tempfile.TemporaryDirectory() as temporary:
            owner = Path(temporary)
            cluster = owner / "cluster"
            cluster.mkdir()
            blend = cluster / "source.blend"
            target = owner / "target.spm"
            manifest = owner / "speedtree_import_manifest.json"
            scope_dir = owner / ".atlas_leaf_speedtree_scopes"
            scope_dir.mkdir()
            scope_receipt = scope_dir / "scope.json"
            blend.write_bytes(b"blend")
            target.write_bytes(b"target")
            manifest.write_text('{"generation":1}', encoding="utf-8")
            scope_receipt.write_text('{"scope":1}', encoding="utf-8")
            row = {
                "blend": blend,
                "on_target_spms": [target],
                "registry_path": blend.with_suffix(
                    ".atlas_leaf_targets.json"
                ),
            }
            units = connected_unit_records([], [row])
            identities = scope_dependency_identities(units, {})
            unit_results = new_unit_results(units, identities)
            update_unit_result(
                unit_results,
                units[0]["unit_id"],
                outcome="failed",
            )
            unit_results[0]["dependency_identity"] = identities[
                units[0]["unit_id"]
            ]
            report = {"schema_version": 2, "unit_results": unit_results}

            validate_failed_retry_plan(report, units, {})
            scope_receipt.write_text('{"scope":2}', encoding="utf-8")
            with self.assertRaisesRegex(
                RetryPlanInvalid,
                "dependency identity changed",
            ):
                validate_failed_retry_plan(report, units, {})

    def test_cluster_fbx_transaction_artifact_drift_invalidates_retry(self):
        with tempfile.TemporaryDirectory() as temporary:
            owner = Path(temporary)
            cluster = owner / "cluster"
            meshes = owner / "meshes"
            cluster.mkdir()
            meshes.mkdir()
            blend = cluster / "source.blend"
            target = owner / "target.spm"
            fbx = meshes / "source_01.fbx"
            blend.write_bytes(b"blend")
            target.write_bytes(b"target")
            fbx.write_bytes(b"mesh-v1")
            row = {"blend": blend, "on_target_spms": [target]}
            units = connected_unit_records([], [row])
            identities = scope_dependency_identities(units, {})
            unit_results = new_unit_results(units, identities)
            update_unit_result(
                unit_results,
                units[0]["unit_id"],
                outcome="failed",
            )
            unit_results[0]["dependency_identity"] = identities[
                units[0]["unit_id"]
            ]
            report = {"schema_version": 2, "unit_results": unit_results}

            validate_failed_retry_plan(report, units, {})
            fbx.write_bytes(b"mesh-v2")
            with self.assertRaisesRegex(
                RetryPlanInvalid,
                "dependency identity changed",
            ):
                validate_failed_retry_plan(report, units, {})

    def test_cluster_inventory_addition_invalidates_retry(self):
        with tempfile.TemporaryDirectory() as temporary:
            owner = Path(temporary)
            cluster = owner / "cluster"
            meshes = owner / "meshes"
            cluster.mkdir()
            meshes.mkdir()
            blend = cluster / "source.blend"
            target = owner / "target.spm"
            blend.write_bytes(b"blend")
            target.write_bytes(b"target")
            units = connected_unit_records(
                [],
                [{"blend": blend, "on_target_spms": [target]}],
            )
            identities = scope_dependency_identities(units, {})
            results = new_unit_results(units, identities)
            results[0]["outcome"] = "failed"
            results[0]["dependency_identity"] = identities[units[0]["unit_id"]]
            report = {"schema_version": 2, "unit_results": results}

            validate_failed_retry_plan(report, units, {})
            (meshes / "new_generated.fbx").write_bytes(b"new")
            with self.assertRaisesRegex(RetryPlanInvalid, "dependency identity changed"):
                validate_failed_retry_plan(report, units, {})

    def test_inventory_addition_during_capture_marks_identity_unstable(self):
        with tempfile.TemporaryDirectory() as temporary:
            owner = Path(temporary)
            cluster = owner / "Cluster"
            cluster.mkdir()
            blend = cluster / "SK_branch_elm_01.blend"
            target = owner / "SK_Tree_elm_01.spm"
            blend.write_bytes(b"blend")
            target.write_bytes(b"target")
            unit = connected_unit_records(
                [],
                [{"blend": blend, "on_target_spms": [target]}],
            )[0]
            import connected_run as contract

            original = contract.file_content_identity
            injected = False

            def add_file_after_inventory(path):
                nonlocal injected
                if not injected:
                    injected = True
                    (cluster / "late_capture.tga").write_bytes(b"late")
                return original(path)

            with mock.patch.object(
                contract,
                "file_content_identity",
                side_effect=add_file_after_inventory,
            ):
                identity = contract.dependency_identity(unit, {})

            self.assertFalse(identity["stable"])
            self.assertIn(
                "InventoryChangedDuringCapture",
                {entry.get("error_type") for entry in identity["inventories"]},
            )

    def test_blender_producer_probe_seals_addon_manifest(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            blender = root / "blender.exe"
            blender.write_bytes(b"fixture blender")

            def run_process(command, **_kwargs):
                report = Path(command[command.index("--report") + 1])
                report.write_text(json.dumps({
                    "schema_version": 1,
                    "kind": "connected_cluster_producer_identity",
                    "provider_available": True,
                    "stable": True,
                    "producer_manifest_sha256": "a" * 64,
                    "addons": [
                        {"module": "atlas_leaf_mesh_builder", "stable": True},
                        {"module": "speedtree_cluster_normalizer", "stable": True},
                    ],
                }), encoding="utf-8")
                return SimpleNamespace(returncode=0, stdout="", stderr="")

            identity = probe_cluster_producer_identity(
                blender,
                run_process=run_process,
            )

            self.assertTrue(identity["stable"])
            self.assertEqual(identity["producer_manifest_sha256"], "a" * 64)
            self.assertEqual(identity["blender_exe"], str(blender.absolute()))
            self.assertEqual(len(identity["report_sha256"]), 64)

    def test_blender_addon_manifest_change_invalidates_cluster_retry(self):
        with tempfile.TemporaryDirectory() as temporary:
            owner = Path(temporary)
            blend = owner / "SK_branch_elm_01.blend"
            target = owner / "SK_Tree_elm_01.spm"
            blend.write_bytes(b"blend")
            target.write_bytes(b"target")
            units = connected_unit_records(
                [],
                [{"blend": blend, "on_target_spms": [target]}],
            )
            settings = {
                "cluster_producer_identity": {
                    "stable": True,
                    "producer_manifest_sha256": "a" * 64,
                },
            }
            identities = scope_dependency_identities(units, settings)
            results = new_unit_results(units, identities)
            results[0]["outcome"] = "failed"
            results[0]["dependency_identity"] = identities[units[0]["unit_id"]]
            report = {"schema_version": 2, "unit_results": results}

            changed = {
                "cluster_producer_identity": {
                    "stable": True,
                    "producer_manifest_sha256": "b" * 64,
                },
            }
            with self.assertRaisesRegex(RetryPlanInvalid, "dependency identity changed"):
                validate_failed_retry_plan(report, units, changed)

    def test_blender_addon_drift_after_plan_blocks_before_cluster_attempt(self):
        with tempfile.TemporaryDirectory() as temporary:
            owner = Path(temporary)
            blend = owner / "SK_branch_elm_01.blend"
            target = owner / "SK_Tree_elm_01.spm"
            blender = owner / "blender.exe"
            blend.write_bytes(b"blend")
            target.write_bytes(b"target")
            blender.write_bytes(b"blender")
            unit = connected_unit_records(
                [],
                [{"blend": blend, "on_target_spms": [target]}],
            )[0]
            settings = {
                "blender_exe": str(blender),
                "cluster_producer_identity": {
                    "stable": True,
                    "producer_manifest_sha256": "a" * 64,
                },
            }
            expected = scope_dependency_identities([unit], settings)[
                unit["unit_id"]
            ]
            attempts = []
            import connected_run as contract

            with mock.patch.object(
                contract,
                "probe_cluster_producer_identity",
                return_value={
                    "stable": True,
                    "producer_manifest_sha256": "b" * 64,
                },
            ):
                outcome = execute_with_bounded_publish_retry(
                    lambda: attempts.append(1),
                    capture_identity=lambda: contract.dependency_identity(
                        unit,
                        settings,
                        refresh_execution_identity=True,
                    ),
                    ownership_is_current=lambda: True,
                    expected_identity=expected,
                )

            self.assertFalse(outcome["ok"])
            self.assertEqual(attempts, [])
            self.assertIn("content_drift", outcome["reason"])

    def test_repository_code_drift_after_plan_blocks_before_mutation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            master = root / "master.spm"
            follower = root / "follower.spm"
            manifest = root / "spm_generator_sync.json"
            master.write_bytes(b"master")
            follower.write_bytes(b"follower")
            manifest.write_bytes(b"{}")
            unit = connected_unit_records([{
                "folder": root,
                "master": master.name,
                "names": [follower.name],
            }], [])[0]
            settings = {
                "production_code_identity": {
                    "stable": True,
                    "manifest_sha256": "a" * 64,
                },
            }
            expected = scope_dependency_identities([unit], settings)[
                unit["unit_id"]
            ]
            attempts = []
            import connected_run as contract

            with mock.patch.object(
                contract,
                "production_code_identity",
                return_value={
                    "stable": True,
                    "manifest_sha256": "b" * 64,
                },
            ):
                outcome = execute_with_bounded_publish_retry(
                    lambda: attempts.append(1),
                    capture_identity=lambda: contract.dependency_identity(
                        unit,
                        settings,
                        refresh_execution_identity=True,
                    ),
                    ownership_is_current=lambda: True,
                    expected_identity=expected,
                )

            self.assertFalse(outcome["ok"])
            self.assertEqual(attempts, [])
            self.assertIn("content_drift", outcome["reason"])

    def test_repository_drift_during_cluster_probe_blocks_mutation(self):
        with tempfile.TemporaryDirectory() as temporary:
            owner = Path(temporary)
            blend = owner / "SK_branch_elm_01.blend"
            target = owner / "SK_Tree_elm_01.spm"
            blend.write_bytes(b"blend")
            target.write_bytes(b"target")
            unit = connected_unit_records(
                [],
                [{"blend": blend, "on_target_spms": [target]}],
            )[0]
            producer = {
                "stable": True,
                "producer_manifest_sha256": "c" * 64,
            }
            settings = {
                "blender_exe": str(owner / "blender.exe"),
                "production_code_identity": {
                    "stable": True,
                    "manifest_sha256": "a" * 64,
                },
                "cluster_producer_identity": producer,
            }
            expected = scope_dependency_identities([unit], settings)[
                unit["unit_id"]
            ]
            attempts = []
            import connected_run as contract

            with mock.patch.object(
                contract,
                "production_code_identity",
                side_effect=(
                    {"stable": True, "manifest_sha256": "a" * 64},
                    {"stable": True, "manifest_sha256": "b" * 64},
                ),
            ), mock.patch.object(
                contract,
                "probe_cluster_producer_identity",
                return_value=producer,
            ):
                outcome = execute_with_bounded_publish_retry(
                    lambda: attempts.append(1),
                    capture_identity=lambda: contract.dependency_identity(
                        unit,
                        settings,
                        refresh_execution_identity=True,
                    ),
                    ownership_is_current=lambda: True,
                    expected_identity=expected,
                )

            self.assertFalse(outcome["ok"])
            self.assertEqual(attempts, [])
            self.assertIn("dependency_unstable", outcome["reason"])

    def test_failed_unit_overlapping_prior_success_is_ineligible_before_mutation(self):
        with tempfile.TemporaryDirectory() as temporary:
            owner = Path(temporary)
            cluster = owner / "Cluster"
            cluster.mkdir()
            target = owner / "SK_Tree_elm_01.spm"
            first_blend = cluster / "SK_branch_elm_01.blend"
            second_blend = cluster / "SK_branch_elm_02.blend"
            target.write_bytes(b"target")
            first_blend.write_bytes(b"first")
            second_blend.write_bytes(b"second")
            units = connected_unit_records([], [
                {"blend": first_blend, "on_target_spms": [target]},
                {"blend": second_blend, "on_target_spms": [target]},
            ])
            identities = scope_dependency_identities(units, {})
            results = new_unit_results(units, identities)
            update_unit_result(results, units[0]["unit_id"], outcome="succeeded")
            update_unit_result(results, units[1]["unit_id"], outcome="failed")
            for entry in results:
                entry["dependency_identity"] = identities[entry["unit_id"]]
            report = {"schema_version": 2, "unit_results": results}

            plan = validate_failed_retry_plan(report, units, {})

            self.assertEqual(plan["units"], [])
            self.assertIn(units[1]["unit_id"], plan["ineligible_overlaps"])
            resources = {
                resource
                for edge in plan["ineligible_overlaps"][units[1]["unit_id"]]
                for resource in edge["resources"]
            }
            self.assertIn(str(target.absolute()), resources)

    def test_exact_report_identity_rejects_tampering(self):
        with tempfile.TemporaryDirectory() as temporary:
            copied = Path(temporary) / "partial.json"
            copied.write_bytes(FIXTURE.read_bytes())
            identity = report_file_identity(copied)

            payload, loaded_identity = load_exact_report(copied, identity)
            self.assertEqual(payload["status"], "partial")
            self.assertEqual(loaded_identity, identity)

            copied.write_bytes(copied.read_bytes() + b"\n")
            with self.assertRaisesRegex(
                RetryPlanInvalid,
                "source report sha256 changed",
            ):
                load_exact_report(copied, identity)

    def test_partial_shared_queue_receipt_keeps_counts_and_exact_report_identity(self):
        payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
        payload["report_identity"] = report_file_identity(FIXTURE)

        result = shared_queue_result(payload, 41)

        self.assertEqual(result["outcome"], "partial")
        self.assertEqual(result["counts"]["generator"]["succeeded"], 11)
        self.assertEqual(result["counts"]["cluster"]["succeeded"], 31)
        self.assertEqual(result["counts"]["failures"], 9)
        self.assertEqual(result["report"]["sha256"], report_file_identity(FIXTURE)["sha256"])

    def test_retry_source_requires_exact_terminal_queue_anchor(self):
        with tempfile.TemporaryDirectory() as temporary:
            report_path = Path(temporary) / "partial.json"
            payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
            payload.update({
                "schema_version": 2,
                "run_id": "run-101",
                "root": str(Path(temporary)),
                "queue_identity": {
                    "mode": "shared",
                    "job_id": "job-101",
                    "sequence": 17,
                    "owner_id": "owner-101",
                },
            })
            report_path.write_text(json.dumps(payload), encoding="utf-8")
            identity = report_file_identity(report_path)
            payload["report_identity"] = identity
            result = shared_queue_result(payload, 17)
            job = {
                "id": "job-101",
                "sequence": 17,
                "status": "failed",
                "last_lease": {"owner_id": "owner-101"},
                "result": result,
            }

            anchor = validate_queue_anchored_report(job, payload, identity)
            self.assertEqual(anchor["queue_job_id"], "job-101")
            orphan = dict(job)
            orphan["result"] = dict(result, report=dict(identity, sha256="0" * 64))
            with self.assertRaisesRegex(RetryPlanInvalid, "exact bytes"):
                validate_queue_anchored_report(orphan, payload, identity)


if __name__ == "__main__":
    unittest.main()
