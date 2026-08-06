import ast
import copy
import hashlib
import queue
import json
import sys
import tempfile
import threading
import unittest
from collections import deque
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path
from unittest import mock


SK_BATCH_DIR = Path(__file__).resolve().parents[1]
FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures"
sys.path.insert(0, str(SK_BATCH_DIR))


def load_gui_module():
    path = SK_BATCH_DIR / "sk_batch_gui.pyw"
    loader = SourceFileLoader("sk_batch_gui_queue_test", str(path))
    spec = spec_from_loader(loader.name, loader)
    module = module_from_spec(spec)
    loader.exec_module(module)
    return module


class PushQueueFlowTests(unittest.TestCase):
    def make_app(self, gui):
        app = gui.App.__new__(gui.App)
        app.stop_flag = threading.Event()
        app.ui_queue = queue.Queue()
        app.state = {}
        app.state_lock = threading.RLock()
        app.procs_lock = threading.Lock()
        app.active_procs = set()
        app.cfg = {}
        app.log = mock.Mock()
        return app

    def test_queue_snapshot_drops_ineligible_backup_board_row(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        live = Path("Weed_reed") / "SK_weed_reed_02.spm"
        rollback = (
            Path("Weed_reed")
            / "SK_weed_reed_02.texture_slot_backup_20260801_010203_123456.spm"
        )
        app.items = {
            str(live): {"spm": live, "checked": True},
            str(rollback): {"spm": rollback, "checked": True},
        }

        inventory, targets = app._snapshot_batch_request(
            [str(rollback), str(live)]
        )

        self.assertEqual(set(inventory), {str(live)})
        self.assertEqual([item["spm"] for item in targets], [live])

    @staticmethod
    def issue16_fixture():
        return json.loads(
            (FIXTURE_DIR / "issue16_blackgum_source_run.json").read_text(
                encoding="utf-8"
            )
        )

    @staticmethod
    def dependency_artifact_fixture():
        return json.loads(
            (
                FIXTURE_DIR
                / "issue138_dependency_artifact_verdicts.json"
            ).read_text(encoding="utf-8")
        )

    @staticmethod
    def sealed_modeler_scope(target):
        scope = {
            "schema_version": 2,
            "available": True,
            "mode": "owned_semantic_uia_modeler_save_watch",
            "scope_policy": "explicit_sealed_delivery_scopes_v1",
            "target_spm": str(Path(target).resolve()),
            "target_preimage_raw_sha256": "a" * 64,
            "authoring_mesh_ids": [1, 2],
            "required_live_mesh_ids": [1],
        }
        scope["scope_sha256"] = hashlib.sha256((json.dumps(
            scope,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ) + "\n").encode("utf-8")).hexdigest()
        return scope

    def test_issue16_captured_live_delivery_is_four_runnable_one_blocked(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        fixture = self.issue16_fixture()
        provider = Path("blackgum") / "cluster" / "SK_cluster_blackgum_01.spm"
        runnable = []
        blocked = []

        for row in fixture["targets"]:
            target = Path("blackgum") / row["target"]
            blocked_targets = []
            handoff_errors = []
            if row["delivery_decision"] == "blocked":
                blocked_targets.append({
                    "spm": str(target),
                    "delivery_reason": row["delivery_reason"],
                    "errors": row["delivery_errors"],
                })
                handoff_errors.append({
                    "code": "NORMALIZED_GENERATOR_DELIVERY_INCOMPLETE",
                    "role": "cluster",
                    "spm": str(provider),
                    "delivery_mode": row["delivery_mode"],
                    "errors": row["delivery_errors"],
                })
            contract = {
                "tree_source_identities": [{"spm": str(target)}],
                "dependencies": [{
                    "spm": str(provider),
                    "normalized_delivery_mode": row["delivery_mode"],
                    "normalized_delivery_blocked": bool(blocked_targets),
                    "normalized_variants_required": True,
                    "normalized_variants": {
                        "status": "current",
                        "delivery_mode": row["delivery_mode"],
                        "delivery_errors": row["delivery_errors"],
                        "delivery_blocked_targets": blocked_targets,
                        "variants": [{"name": "Cluster"}],
                    },
                }],
                "handoff": {
                    "status": "blocked" if handoff_errors else "ready",
                    "errors": handoff_errors,
                },
                # Operational Sync completed, but that is not Push readiness.
                "relationship_sync": {"outcome": "completed"},
            }
            raw_audit = {
                "selected_contract": contract,
                "audit_report": Path("captured_live_audit.json"),
                "payload": {"items": [{}]},
            }
            with mock.patch.object(
                app,
                "_refresh_stale_cluster_receipt_uncached",
                return_value=raw_audit,
            ):
                if row["delivery_decision"] == "blocked":
                    with self.assertRaises(
                        gui.TargetPlannedExclusionError
                    ) as caught:
                        app._cluster_normalization_stage_observation(
                            target,
                            "captured",
                            provider,
                            require_normalized=False,
                        )
                    blocked.append(caught.exception.reason_token)
                else:
                    observation = (
                        app._cluster_normalization_stage_observation(
                            target,
                            "captured",
                            provider,
                            require_normalized=False,
                        )
                    )
                    runnable.append(Path(observation["target_spm"]).name)

        self.assertEqual(len(runnable), 4)
        self.assertEqual(blocked, [
            fixture["expected_result"]["blocked_reason_token"]
        ])

    @staticmethod
    def densiflora_stale_contract(target, provider, *, bind_target=True):
        blocked_row = {
            "spm": str(target),
            "delivery_reason": (
                "live_export_evidence_unavailable_stale_node_table"
            ),
            "delivery_remedy": (
                "Open the target SPM in SpeedTree Modeler, regenerate/save "
                "the Node table, then re-audit."
            ),
            "errors": [
                "generator_export_evidence_stale_node_table",
                "normalized_and_live_target_mesh_sets_differ",
            ],
            "stale_node_table_target_mesh_ids": [16, 17, 18, 19],
            "live_node_table": {
                "stale": True,
                "generator_count": 48,
                "node_table_generator_count": 43,
                "orphan_generator_guids": ["orphan-a", "orphan-b"],
                "orphan_node_count": 216495,
                "total_node_count": 223675,
            },
        }
        issue = {
            "code": "NORMALIZED_GENERATOR_NODE_TABLE_STALE",
            "role": "cluster",
            "spm": str(provider),
            "errors": list(blocked_row["errors"]),
            "remedy": blocked_row["delivery_remedy"],
            "blocked_targets": [blocked_row],
        }
        return {
            "tree_source_identities": [{"target_spm": str(target)}],
            "dependencies": [{
                "spm": str(provider),
                "normalized_delivery_mode": "connection_incomplete",
                "normalized_delivery_blocked": True,
                "normalized_variants_required": True,
                "normalized_variants": {
                    "status": "ready",
                    "delivery_mode": "connection_incomplete",
                    "delivery_errors": list(blocked_row["errors"]),
                    "delivery_blocked_targets": (
                        [blocked_row] if bind_target else []
                    ),
                    "variants": [{"name": "Cluster"}],
                },
            }],
            "handoff": {
                "status": "blocked",
                "errors": [issue],
            },
        }

    @staticmethod
    def recoverable_multi_role_contract(
        target,
        providers,
        *,
        omit_second=False,
        second_independent_error=False,
    ):
        target = Path(target).resolve()
        dependencies = []
        for index, (
            role,
            provider,
            authoring_mesh_ids,
            required_live_mesh_ids,
        ) in enumerate(providers):
            errors = [
                "generator_export_evidence_stale_node_table",
                "normalized_and_live_target_mesh_sets_differ",
            ]
            reason = "live_export_evidence_unavailable_stale_node_table"
            if index == 1 and second_independent_error:
                errors.append("declared_generator_binding_missing")
                reason = "generator_connection_contract_incomplete"
            intent_sha256 = f"{index + 1:064x}"
            recovery_target_scope = {
                "contract": "speedtree_stale_node_recovery_target_scope",
                "schema_version": 1,
                "policy": "explicit_sealed_scopes_v1",
                "delivery_scope_intent_sha256": intent_sha256,
                "authoring_mesh_ids": list(authoring_mesh_ids),
                "required_live_mesh_ids": list(required_live_mesh_ids),
            }
            recovery_target_scope["scope_sha256"] = hashlib.sha256(
                json.dumps(
                    recovery_target_scope,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            target_deliveries = [] if index == 1 and omit_second else [{
                "spm": str(target),
                "delivery_decision": "blocked",
                "delivery_reason": reason,
                "generator_variant_policy": "ensure_all_material_cutouts",
                "delivery_scope_mode": "explicit_sealed_v1",
                "delivery_scope_intent_sha256": intent_sha256,
                "recovery_target_scope": recovery_target_scope,
                "delivery_scope_required_live_slot_count": len(
                    required_live_mesh_ids
                ),
                "delivery_scope_continuity_only_slot_count": (
                    len(authoring_mesh_ids) - len(required_live_mesh_ids)
                ),
                "normalized_target_mesh_ids": list(authoring_mesh_ids),
                "declared_target_mesh_ids": list(authoring_mesh_ids),
                "current_required_target_mesh_ids": list(
                    required_live_mesh_ids
                ),
                "declared_binding_count": len(authoring_mesh_ids),
                "active_required_binding_count": len(required_live_mesh_ids),
                "planned_inactive_binding_count": 0,
                "stale_node_table_target_mesh_ids": list(
                    required_live_mesh_ids
                ),
                "live_node_table": {
                    "stale": True,
                    "orphan_generator_guids": ["sanitized-orphan-001"],
                    "orphan_node_count": 1,
                },
                "errors": errors,
            }]
            dependencies.append({
                "role": role,
                "spm": str(Path(provider).resolve()),
                "normalized_variants_required": True,
                "normalized_variants": {
                    "status": "ready",
                    "target_deliveries": target_deliveries,
                },
            })
        return {
            "tree_source_identities": [{
                "target_spm": {
                    "path": str(target),
                    "sha256": "a" * 64,
                },
            }],
            "dependencies": dependencies,
            "handoff": {"status": "blocked", "errors": []},
        }

    def test_modeler_recovery_scope_unions_all_target_provider_roles(self):
        gui = load_gui_module()
        target = Path("black_locast") / "SK_tree_black_locast_02.spm"
        providers = (
            (
                "branch",
                Path("black_locast/cluster/SK_branch_black_locast_01.spm"),
                [88],
                [88],
            ),
            (
                "cluster",
                Path("black_locast/cluster/SK_cluster_black_locast_01.spm"),
                [89, 90, 91, 92],
                [89],
            ),
        )
        contract = self.recoverable_multi_role_contract(target, providers)

        scope = gui.cluster_stale_node_table_recovery_scope(
            contract,
            target.resolve(),
            Path("live_audit.json"),
        )

        self.assertTrue(scope["available"])
        self.assertEqual(scope["schema_version"], 2)
        self.assertEqual(scope["authoring_mesh_ids"], [88, 89, 90, 91, 92])
        self.assertEqual(scope["required_live_mesh_ids"], [88, 89])
        self.assertNotIn("expected_mesh_ids", scope)
        self.assertEqual(
            [row["authoring_mesh_ids"] for row in scope["provider_slices"]],
            [[88], [89, 90, 91, 92]],
        )
        self.assertEqual(
            [row["required_live_mesh_ids"] for row in scope["provider_slices"]],
            [[88], [89]],
        )
        self.assertRegex(scope["scope_sha256"], r"^[0-9a-f]{64}$")

    def test_modeler_recovery_scope_rejects_partial_and_mixed_evidence(self):
        gui = load_gui_module()
        target = (Path("black_locast") / "SK_tree_black_locast_02.spm").resolve()
        providers = (
            ("branch", Path("provider_branch.spm"), [88], [88]),
            (
                "cluster",
                Path("provider_cluster.spm"),
                [89, 90, 91, 92],
                [89],
            ),
        )
        partial = gui.cluster_stale_node_table_recovery_scope(
            self.recoverable_multi_role_contract(
                target,
                providers,
                omit_second=True,
            ),
            target,
            Path("partial.json"),
        )
        mixed = gui.cluster_stale_node_table_recovery_scope(
            self.recoverable_multi_role_contract(
                target,
                providers,
                second_independent_error=True,
            ),
            target,
            Path("mixed.json"),
        )

        self.assertFalse(partial["available"])
        self.assertEqual(
            partial["reason_token"],
            "target_delivery_missing_or_ambiguous",
        )
        self.assertFalse(mixed["available"])
        self.assertEqual(
            mixed["reason_token"],
            "target_delivery_not_stale_only",
        )

    def test_modeler_recovery_scope_rejects_legacy_and_observed_scope_misuse(self):
        gui = load_gui_module()
        target = (Path("densiflora") / "SK_tree_densiflora_02.spm").resolve()
        providers = ((
            "cluster",
            Path("provider_cluster.spm"),
            [14, 15, 16, 17],
            [14],
        ),)
        authoritative = self.recoverable_multi_role_contract(
            target,
            providers,
        )

        legacy = copy.deepcopy(authoritative)
        legacy_delivery = legacy["dependencies"][0]["normalized_variants"][
            "target_deliveries"
        ][0]
        legacy_delivery["delivery_scope_mode"] = "legacy_strict"
        legacy_delivery["current_required_target_mesh_ids"] = [
            14,
            15,
            16,
            17,
        ]

        observed_only = copy.deepcopy(authoritative)
        observed_delivery = observed_only["dependencies"][0][
            "normalized_variants"
        ]["target_deliveries"][0]
        observed_delivery.pop("recovery_target_scope")
        observed_delivery.pop("current_required_target_mesh_ids")
        observed_delivery["live_export_participating_target_mesh_ids"] = [14]
        observed_delivery["live_generator_bindings"] = [{
            "mesh_id": 14,
            "visible": True,
            "generated_node_count": 1419,
        }]

        mixed = copy.deepcopy(authoritative)
        mixed_delivery = mixed["dependencies"][0]["normalized_variants"][
            "target_deliveries"
        ][0]
        mixed_scope = mixed_delivery["recovery_target_scope"]
        mixed_scope["required_live_mesh_ids"] = [999]
        mixed_scope["scope_sha256"] = hashlib.sha256(
            json.dumps(
                {
                    key: value
                    for key, value in mixed_scope.items()
                    if key != "scope_sha256"
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()

        cases = (
            (legacy, "target_delivery_scope_not_explicit"),
            (observed_only, "authoritative_recovery_target_scope_missing"),
            (mixed, "required_live_scope_not_authoring_subset"),
        )
        for contract, reason_token in cases:
            with self.subTest(reason_token=reason_token):
                scope = gui.cluster_stale_node_table_recovery_scope(
                    contract,
                    target,
                    Path("live_audit.json"),
                )
                self.assertFalse(scope["available"])
                self.assertEqual(scope["reason_token"], reason_token)

    def test_nonblocking_stale_node_table_never_starts_recovery(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        target = (Path("black_locast") / "SK_tree_black_locast_04.spm").resolve()
        provider = Path("black_locast/cluster/SK_cluster_black_locast_01.spm").resolve()
        contract = {
            "tree_source_identities": [{
                "target_spm": {"path": str(target), "sha256": "c" * 64},
            }],
            "dependencies": [{
                "role": "cluster",
                "spm": str(provider),
                "normalized_variants_required": True,
                "normalized_variants": {
                    "status": "ready",
                    "delivery_mode": "render_connected",
                    "delivery_errors": [],
                    "delivery_blocked_targets": [],
                    "target_deliveries": [{
                        "spm": str(target),
                        "delivery_decision": "normalize_part",
                        "generator_variant_policy": (
                            "ensure_all_material_cutouts"
                        ),
                        "normalized_target_mesh_ids": [70, 71],
                        "live_generator_delivery_complete": True,
                        "live_node_table": {
                            "stale": True,
                            "orphan_node_count": 970,
                            "total_node_count": 21249,
                        },
                        "errors": [],
                    }],
                },
            }],
            "handoff": {"status": "ready", "errors": []},
        }
        raw_audit = {
            "selected_contract": contract,
            "audit_report": Path("tree04_live_audit.json"),
            "payload": {"items": [{}]},
        }

        with mock.patch.object(
            app,
            "_refresh_stale_cluster_receipt_uncached",
            return_value=raw_audit,
        ), mock.patch.object(
            app,
            "_attempt_stale_node_table_recovery",
        ) as recovery_attempt:
            result = app._cluster_normalization_stage_with_recovery(
                target,
                "tree04",
                provider,
                require_normalized=False,
            )

        self.assertEqual(result["status"], "current")
        recovery_attempt.assert_not_called()

    def test_stop_after_resume_claim_reports_the_committed_boundary(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        app.pending_batch_jobs = deque()
        app.active_batch_job = {"id": 95}
        app.shared_queue_runtime = None
        app._ensure_batch_queue_state()
        with app._recovery_commit_lock:
            app._recovery_resume_commit = {
                "job_id": 95,
                "job_generation": 95,
                "verified_after_raw_sha256": "b" * 64,
            }

        app.stop_batch()

        self.assertTrue(app.stop_flag.is_set())
        self.assertIn(
            "재개 commit 뒤에 도착",
            "\n".join(str(call.args[0]) for call in app.log.call_args_list),
        )

    def test_registered_relation_repair_reaches_runnable_once_without_fanout(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "SK_bush_blackgum_02.spm"
            provider = root / "cluster" / "SK_cluster_blackgum_01.spm"
            sibling = root / "SK_bush_blackgum_03.spm"
            provider.parent.mkdir(parents=True)
            for path in (target, provider, sibling):
                path.write_bytes(b"sanitized-spm")
            app.items = {
                str(path): {"spm": path, "checked": True}
                for path in (target, provider, sibling)
            }
            app.active_batch_job = {
                "id": 108,
                "shared_queue_job_id": "shared-108",
                "shared_queue_sequence": 12,
            }
            app._active_retry_metadata = {}
            lease = mock.Mock()
            lease.job_id = "shared-108"
            lease.finished = False
            lease.renew_and_check_current.return_value = True
            app._active_shared_queue_lease = lease
            app.root = mock.Mock()
            exclusion = gui.TargetPlannedExclusionError(
                "Generator connection is incomplete",
                reason_token="generator_connection_contract_incomplete",
                target_spm=target,
                producer_spm=provider,
                evidence={
                    "issue_codes": [
                        "NORMALIZED_GENERATOR_DELIVERY_INCOMPLETE"
                    ],
                    "normalization_postcondition": "not_run",
                },
            )
            fresh = {
                "status": "current",
                "target_spm": str(target),
                "live_audit_report": str(root / "fresh.json"),
                "selected_contract": {"handoff": {"status": "ready"}},
            }
            observe = mock.Mock(
                side_effect=[exclusion, fresh, exclusion, fresh]
            )
            execute = mock.Mock(return_value={
                "status": "completed",
                "terminal_status": "completed",
                "result": {"outcome": "completed"},
            })
            app._record_pipeline_planned_exclusion = mock.Mock()

            with mock.patch.object(
                app,
                "_cluster_normalization_stage_observation",
                side_effect=observe,
            ), mock.patch.object(
                app,
                "_execute_exact_repair_stage",
                side_effect=execute,
            ), mock.patch.object(gui, "LOG_DIR", root):
                first = app._cluster_relation_input_plan(
                    [target], "blackgum_first", provider
                )
                second = app._cluster_relation_input_plan(
                    [target], "blackgum_second", provider
                )

        self.assertEqual(first[0], [target])
        self.assertEqual(second[0], [target])
        self.assertEqual(execute.call_count, 1)
        plan, stage = execute.call_args.args[:2]
        self.assertEqual(
            set(stage["target_spms"]),
            {str(target.resolve()), str(provider.resolve())},
        )
        self.assertNotIn(str(sibling.resolve()), stage["target_spms"])
        self.assertEqual(
            tuple(plan["reason_codes"]),
            (
                "generator_connection_contract_incomplete",
                "normalized_generator_delivery_incomplete",
            ),
        )
        app._record_pipeline_planned_exclusion.assert_not_called()
        self.assertEqual(observe.call_count, 4)
        self.assertEqual(app.root.method_calls, [])

    def test_unclassified_relation_reason_is_recorded_once_not_silently_dropped(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "SK_bush_blackgum_02.spm"
            provider = root / "cluster" / "SK_cluster_blackgum_01.spm"
            provider.parent.mkdir(parents=True)
            target.write_bytes(b"sanitized-target")
            provider.write_bytes(b"sanitized-provider")
            app.items = {
                str(target): {"spm": target},
                str(provider): {"spm": provider},
            }
            app.active_batch_job = {"id": 109}
            app._active_retry_metadata = {}
            exclusion = gui.TargetPlannedExclusionError(
                "Mesh owner is ambiguous",
                reason_token="managed_mesh_owner_ambiguous",
                target_spm=target,
                producer_spm=provider,
                evidence={"issue_codes": ["MANAGED_MESH_OWNER_AMBIGUOUS"]},
            )
            app._record_pipeline_planned_exclusion = mock.Mock()
            with mock.patch.object(
                app,
                "_cluster_normalization_stage_observation",
                side_effect=exclusion,
            ), mock.patch.object(
                app, "_execute_exact_repair_stage"
            ) as execute:
                runnable, contracts = app._cluster_relation_input_plan(
                    [target], "unsupported", provider
                )

        self.assertEqual(runnable, [])
        self.assertEqual(contracts, [])
        execute.assert_not_called()
        app._record_pipeline_planned_exclusion.assert_called_once_with(
            target, exclusion
        )
        self.assertEqual(
            exclusion.evidence["repair_attempt"]["status"],
            "unsupported",
        )

    def test_registered_stale_node_reason_selects_modeler_action(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "SK_tree_blackgum_01.spm"
            provider = root / "cluster" / "SK_cluster_blackgum_01.spm"
            provider.parent.mkdir(parents=True)
            target.write_bytes(b"sanitized-target")
            provider.write_bytes(b"sanitized-provider")
            app.items = {
                str(target): {"spm": target},
                str(provider): {"spm": provider},
            }
            app.active_batch_job = {
                "id": 112,
                "shared_queue_job_id": "shared-112",
            }
            app._active_retry_metadata = {}
            lease = mock.Mock()
            lease.job_id = "shared-112"
            lease.finished = False
            lease.renew_and_check_current.return_value = True
            app._active_shared_queue_lease = lease
            exclusion = gui.TargetPlannedExclusionError(
                "Node table is stale",
                reason_token="normalized_generator_node_table_stale",
                target_spm=target,
                producer_spm=provider,
                evidence={
                    "issue_codes": [
                        "NORMALIZED_GENERATOR_NODE_TABLE_STALE"
                    ],
                    "stale_node_table_recovery": (
                        self.sealed_modeler_scope(target)
                    ),
                },
            )
            fresh = {
                "status": "current",
                "target_spm": str(target),
                "live_audit_report": str(root / "fresh.json"),
                "selected_contract": {"handoff": {"status": "ready"}},
            }
            execute = mock.Mock(return_value={
                "status": "completed",
                "terminal_status": "completed",
                "result": {"outcome": "completed"},
            })
            with mock.patch.object(
                app,
                "_cluster_normalization_stage_observation",
                side_effect=[exclusion, fresh],
            ), mock.patch.object(
                app,
                "_execute_exact_repair_stage",
                side_effect=execute,
            ), mock.patch.object(gui, "LOG_DIR", root):
                runnable, _contracts = app._cluster_relation_input_plan(
                    [target], "node_table", provider
                )

        self.assertEqual(runnable, [target])
        _plan, stage = execute.call_args.args[:2]
        self.assertEqual(stage["tool"], gui.MODELER_RECOVERY_TOOL)
        self.assertEqual(
            stage["repair_action"], gui.MODELER_NODE_TABLE_RECOVERY
        )
        self.assertNotEqual(stage["repair_action"], "cluster-refresh")

    def test_stale_node_gate_never_falls_back_outside_registry_action(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        target = Path("missing") / "SK_tree_blackgum_01.spm"
        provider = Path("missing") / "cluster" / "SK_cluster_blackgum_01.spm"
        app.active_batch_job = {"id": 113}
        app._active_retry_metadata = {}
        exclusion = gui.TargetPlannedExclusionError(
            "Node table is stale",
            reason_token="normalized_generator_node_table_stale",
            target_spm=target,
            producer_spm=provider,
            evidence={
                "issue_codes": ["NORMALIZED_GENERATOR_NODE_TABLE_STALE"],
                "stale_node_table_recovery": {
                    "available": True,
                    "target_spm": str(target.resolve()),
                    "target_preimage_raw_sha256": "a" * 64,
                },
            },
        )

        with mock.patch.object(
            app,
            "_cluster_normalization_stage_observation",
            side_effect=exclusion,
        ), mock.patch.object(
            app,
            "_attempt_stale_node_table_recovery",
        ) as private_recovery:
            with self.assertRaises(gui.TargetPlannedExclusionError):
                app._cluster_normalization_stage_with_recovery(
                    target,
                    "missing_scope",
                    provider,
                    require_normalized=False,
                )

        private_recovery.assert_not_called()
        self.assertEqual(
            exclusion.evidence["repair_attempt"]["reason_token"],
            "exact_target_plan_invalid",
        )

    def test_modeler_registry_stage_executes_sealed_recovery_under_exact_receipt(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "SK_tree_blackgum_01.spm"
            provider = root / "cluster" / "SK_cluster_blackgum_01.spm"
            provider.parent.mkdir(parents=True)
            target.write_bytes(b"sanitized-target")
            provider.write_bytes(b"sanitized-provider")
            evidence = {
                "reason_token": "normalized_generator_node_table_stale",
                "target_spm": str(target),
                "producer_spm": str(provider),
                "stale_node_table_recovery": self.sealed_modeler_scope(
                    target
                ),
            }
            plan = gui.build_exact_target_repair_plan(
                target,
                evidence,
                inventory_paths=[target, provider],
                parent_retry_id="retry-modeler",
                request_id="request-modeler",
            ).metadata()
            stage = plan["stages"][0]
            receipt = root / "modeler-exact-receipt.json"
            lease = mock.Mock()
            lease.finished = False
            lease.renew_and_check_current.return_value = True
            resolution = {
                "status": "current",
                "target_spm": str(target),
                "selected_contract": {"handoff": {"status": "ready"}},
            }
            with mock.patch.object(
                app,
                "_attempt_stale_node_table_recovery",
                return_value=resolution,
            ) as recover:
                terminal = app._execute_exact_repair_stage(
                    plan,
                    stage,
                    lease,
                    stage_index=1,
                    receipt=receipt,
                    provenance_source="sanitized.test",
                )

            payload = json.loads(receipt.read_text(encoding="utf-8"))

        self.assertEqual(terminal["terminal_status"], "completed")
        self.assertEqual(payload["terminal_status"], "completed")
        self.assertEqual(
            payload["result"]["live_resolution"], resolution
        )
        synthetic = recover.call_args.args[0]
        self.assertEqual(
            synthetic.reason_token,
            "normalized_generator_node_table_stale",
        )
        self.assertEqual(synthetic.target_spm, target)
        self.assertEqual(synthetic.producer_spm, provider)

    def test_failed_registered_relation_repair_is_not_enqueued_twice(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "SK_bush_blackgum_02.spm"
            provider = root / "cluster" / "SK_cluster_blackgum_01.spm"
            provider.parent.mkdir(parents=True)
            target.write_bytes(b"sanitized-target")
            provider.write_bytes(b"sanitized-provider")
            app.items = {
                str(target): {"spm": target},
                str(provider): {"spm": provider},
            }
            app.active_batch_job = {
                "id": 110,
                "shared_queue_job_id": "shared-110",
            }
            app._active_retry_metadata = {}
            lease = mock.Mock()
            lease.job_id = "shared-110"
            lease.finished = False
            lease.renew_and_check_current.return_value = True
            app._active_shared_queue_lease = lease
            exclusion = gui.TargetPlannedExclusionError(
                "Generator connection is incomplete",
                reason_token="generator_connection_contract_incomplete",
                target_spm=target,
                producer_spm=provider,
                evidence={
                    "issue_codes": [
                        "NORMALIZED_GENERATOR_DELIVERY_INCOMPLETE"
                    ]
                },
            )
            execute = mock.Mock(return_value={
                "status": "failed",
                "terminal_status": "failed",
                "error": "sanitized exact failure",
            })
            with mock.patch.object(
                app,
                "_execute_exact_repair_stage",
                side_effect=execute,
            ), mock.patch.object(gui, "LOG_DIR", root):
                with mock.patch.object(
                    app,
                    "_cluster_normalization_stage_observation",
                    side_effect=[exclusion, exclusion],
                ):
                    for stamp in ("failed_first", "failed_second"):
                        with self.assertRaises(
                            gui.TargetPlannedExclusionError
                        ):
                            app._cluster_normalization_stage_with_recovery(
                                target,
                                stamp,
                                provider,
                                require_normalized=False,
                            )

        self.assertEqual(execute.call_count, 1)
        self.assertEqual(
            exclusion.evidence["repair_attempt"]["reason_token"],
            "exact_relation_repair_failed",
        )

    def test_cancelled_registered_relation_repair_never_becomes_exclusion(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "SK_bush_blackgum_02.spm"
            provider = root / "cluster" / "SK_cluster_blackgum_01.spm"
            provider.parent.mkdir(parents=True)
            target.write_bytes(b"sanitized-target")
            provider.write_bytes(b"sanitized-provider")
            app.items = {
                str(target): {"spm": target},
                str(provider): {"spm": provider},
            }
            app.active_batch_job = {
                "id": 111,
                "shared_queue_job_id": "shared-111",
            }
            app._active_retry_metadata = {}
            lease = mock.Mock()
            lease.job_id = "shared-111"
            lease.finished = False
            lease.renew_and_check_current.return_value = True
            app._active_shared_queue_lease = lease
            exclusion = gui.TargetPlannedExclusionError(
                "Generator connection is incomplete",
                reason_token="generator_connection_contract_incomplete",
                target_spm=target,
                producer_spm=provider,
                evidence={
                    "issue_codes": [
                        "NORMALIZED_GENERATOR_DELIVERY_INCOMPLETE"
                    ]
                },
            )
            app._record_pipeline_planned_exclusion = mock.Mock()
            with mock.patch.object(
                app,
                "_cluster_normalization_stage_observation",
                side_effect=exclusion,
            ), mock.patch.object(
                app,
                "_execute_exact_repair_stage",
                return_value={
                    "status": "cancelled",
                    "terminal_status": "cancelled",
                },
            ), mock.patch.object(gui, "LOG_DIR", root):
                with self.assertRaises(gui.BatchItemError) as caught:
                    app._cluster_relation_input_plan(
                        [target], "cancelled", provider
                    )

        self.assertEqual(caught.exception.kind, "cancelled")
        self.assertEqual(
            caught.exception.report["reason_token"], "operator_cancelled"
        )
        app._record_pipeline_planned_exclusion.assert_not_called()

    def test_relation_plan_uses_owned_semantic_session_and_resumes_stage_once(self):
        gui = load_gui_module()
        from pcg_st9_texture_batch import stale_node_table_recovery as recovery
        from pcg_st9_texture_batch import speedtree_modeler_uia as semantic_uia

        app = self.make_app(gui)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "SK_tree_black_locast_02.spm"
            provider = root / "cluster" / "SK_branch_black_locast_01.spm"
            cluster_provider = (
                root / "cluster" / "SK_cluster_black_locast_01.spm"
            )
            provider.parent.mkdir(parents=True)
            for path in (target, provider, cluster_provider):
                path.write_bytes(b"sanitized-spm")
            providers = (
                ("branch", provider, [88], [88]),
                ("cluster", cluster_provider, [89, 90, 91, 92], [89]),
            )
            scope = gui.cluster_stale_node_table_recovery_scope(
                self.recoverable_multi_role_contract(target, providers),
                target,
                root / "live_audit.json",
            )
            exclusion = gui.TargetPlannedExclusionError(
                "stale target",
                reason_token=(
                    "live_export_evidence_unavailable_stale_node_table"
                ),
                target_spm=target,
                producer_spm=provider,
                evidence={
                    "issue_codes": [
                        "NORMALIZED_GENERATOR_NODE_TABLE_STALE"
                    ],
                    "stale_node_table_recovery": scope,
                },
            )
            resumed = {
                "target_spm": str(target),
                "live_audit_report": str(root / "fresh.json"),
                "selected_contract": {"handoff": {"status": "ready"}},
            }
            app.items = {
                str(path): {"spm": path}
                for path in (target, provider, cluster_provider)
            }
            app.active_batch_job = {
                "id": 95,
                "shared_queue_job_id": "shared-95",
                "shared_queue_sequence": 7,
            }
            app._active_retry_metadata = {}
            active_lease = mock.Mock()
            active_lease.job_id = "shared-95"
            active_lease.finished = False
            active_lease.renew_and_check_current.return_value = True
            app._active_shared_queue_lease = active_lease
            app.cfg = {"speedtree_exe": "SpeedTree_Modeler.exe"}
            app._app_open = True
            app._record_pipeline_planned_exclusion = mock.Mock()
            observe = mock.Mock(side_effect=[exclusion, resumed])
            captured = {}

            def fake_recover(*args, **kwargs):
                captured["args"] = args
                captured["kwargs"] = kwargs
                kwargs["on_continuation_claimed"]({
                    "verified_after_raw_sha256": "b" * 64,
                })
                return {
                    "status": "repaired_reaudited_and_retried_once",
                    "after_raw_sha256": "b" * 64,
                    "retry_result": kwargs["retry"]({"verified": True}),
                }

            semantic_session = mock.Mock()
            semantic_session.is_compatible.return_value = True
            with mock.patch.object(
                app,
                "_cluster_normalization_stage_observation",
                side_effect=observe,
            ), mock.patch.object(
                recovery,
                "recover_stale_node_table",
                side_effect=fake_recover,
            ), mock.patch.object(
                semantic_uia,
                "SpeedTreeModelerRecoverySession",
                return_value=semantic_session,
            ) as session_factory, mock.patch.object(gui, "LOG_DIR", root):
                runnable, contracts = app._cluster_relation_input_plan(
                    [target],
                    "captured",
                    provider,
                )

        self.assertEqual(runnable, [target])
        self.assertEqual(len(contracts), 1)
        self.assertEqual(observe.call_count, 2)
        self.assertEqual(captured["args"], (target, "SpeedTree_Modeler.exe"))
        self.assertEqual(
            captured["kwargs"]["authoring_mesh_ids"],
            [88, 89, 90, 91, 92],
        )
        self.assertEqual(
            captured["kwargs"]["required_live_mesh_ids"],
            [88, 89],
        )
        self.assertNotIn("expected_mesh_ids", captured["kwargs"])
        self.assertEqual(
            captured["kwargs"]["expected_preimage_raw_sha256"],
            "a" * 64,
        )
        self.assertTrue(captured["kwargs"]["guards"]["is_job_current"]())
        self.assertTrue(captured["kwargs"]["guards"]["is_queue_current"]())
        self.assertGreaterEqual(
            active_lease.renew_and_check_current.call_count,
            2,
        )
        self.assertEqual(
            app._recovery_resume_commit["verified_after_raw_sha256"],
            "b" * 64,
        )
        session_factory.assert_called_once_with("SpeedTree_Modeler.exe")
        self.assertIs(captured["kwargs"]["modeler_session"], semantic_session)
        self.assertIs(app._stale_node_table_modeler_session, semantic_session)
        queued_payloads = []
        while not app.ui_queue.empty():
            queued_payloads.append(app.ui_queue.get_nowait())
        modeler_payload = next(
            payload
            for kind, payload in queued_payloads
            if kind == "modeler_recovery"
        )
        self.assertEqual(
            modeler_payload["authoring_mesh_ids"],
            [88, 89, 90, 91, 92],
        )
        self.assertEqual(modeler_payload["required_live_mesh_ids"], [88, 89])
        self.assertNotIn("expected_mesh_ids", modeler_payload)
        app._record_pipeline_planned_exclusion.assert_not_called()

    def test_issue16_stale_node_target_is_excluded_with_operator_evidence(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        target = (
            Path("densiflora") / "SK_tree_densiflora_03.spm"
        ).resolve()
        provider = (
            Path("densiflora")
            / "cluster"
            / "SK_cluster_densiflora_01.spm"
        ).resolve()
        contract = self.densiflora_stale_contract(target, provider)
        raw_audit = {
            "selected_contract": contract,
            "audit_report": Path("densiflora_stale_live_audit.json"),
            "log_file": Path("densiflora_stale_live_audit.log"),
            "payload": {"items": [{}]},
        }

        with mock.patch.object(
            app,
            "_refresh_stale_cluster_receipt_uncached",
            return_value=raw_audit,
        ):
            with self.assertRaises(
                gui.TargetPlannedExclusionError
            ) as caught:
                app._cluster_normalization_stage_observation(
                    target,
                    "densiflora",
                    provider,
                    require_normalized=False,
                )

        error = caught.exception
        evidence = error.evidence
        self.assertEqual(
            error.reason_token,
            "live_export_evidence_unavailable_stale_node_table",
        )
        self.assertEqual(evidence["target_name"], target.name)
        self.assertEqual(
            evidence["issue_codes"],
            ["NORMALIZED_GENERATOR_NODE_TABLE_STALE"],
        )
        self.assertEqual(
            evidence["stale_node_table_target_mesh_ids"],
            [16, 17, 18, 19],
        )
        self.assertEqual(
            evidence["live_node_table"],
            {
                "stale": True,
                "generator_count": 48,
                "node_table_generator_count": 43,
                "orphan_node_count": 216495,
                "total_node_count": 223675,
                "orphan_generator_guid_count": 2,
            },
        )
        self.assertNotIn("orphan-a", json.dumps(evidence))

        with mock.patch.object(gui, "save_state"):
            app._record_pipeline_planned_exclusion(target, error)
        entry = app.state[str(target)]
        for column in ("blend_status", "push_status"):
            status = entry[column]
            self.assertIn(target.name, status)
            self.assertIn("최종 차단", status)
            self.assertIn("Node table 오래됨", status)
            self.assertIn("고아 Generator GUID 2개", status)
            self.assertIn("고아 Node 216495/223675", status)
            self.assertIn("대상 Mesh ID 16,17,18,19", status)
            self.assertIn("복구 범위를 다시 확정", status)
            self.assertNotIn("Sync excluded", status)
        self.assertEqual(
            entry["push_status_error"]["evidence"]["delivery_remedy"],
            (
                "Open the target SPM in SpeedTree Modeler, regenerate/save "
                "the Node table, then re-audit."
            ),
        )
        self.assertNotIn(str(provider), app.state)

    def test_issue16_unbound_stale_node_issue_remains_shared_and_diagnostic(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        target = (
            Path("densiflora") / "SK_tree_densiflora_03.spm"
        ).resolve()
        provider = (
            Path("densiflora")
            / "cluster"
            / "SK_cluster_densiflora_01.spm"
        ).resolve()
        contract = self.densiflora_stale_contract(
            target,
            provider,
            bind_target=False,
        )
        raw_audit = {
            "selected_contract": contract,
            "audit_report": Path("unbound_stale_live_audit.json"),
            "payload": {"items": [{}]},
        }

        with mock.patch.object(
            app,
            "_refresh_stale_cluster_receipt_uncached",
            return_value=raw_audit,
        ):
            with self.assertRaises(gui.BatchItemError) as caught:
                app._cluster_normalization_stage_observation(
                    target,
                    "densiflora",
                    provider,
                    require_normalized=False,
                )

        message = str(caught.exception)
        self.assertIn(target.name, message)
        self.assertIn("최종 차단", message)
        self.assertIn("Node table이 오래되었지만", message)
        self.assertIn("복구 범위를 다시 확정", message)
        self.assertNotIn("validation failed", message)
        self.assertNotIn("role=cluster", message)

    def test_issue16_planned_exclusion_does_not_fan_out_shared_provider(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        fixture = self.issue16_fixture()
        roots = [
            Path("blackgum") / row["target"]
            for row in fixture["targets"]
        ]
        weed = next(path for path in roots if "weed" in path.name.casefold())
        provider = (
            Path("blackgum") / "cluster" / "SK_cluster_blackgum_01.spm"
        )
        provider_item = {
            "spm": provider,
            "checked": False,
            "referenced_by_spms": tuple(roots),
        }
        root_items = [{"spm": path, "checked": True} for path in roots]
        expanded = [provider_item, *root_items]
        dependency_map = {
            str(path): (str(provider),) for path in roots
        }
        calls = []

        def fake_batch(phase, phase_targets, emit_done=False):
            names = [item["spm"].name for item in phase_targets]
            calls.append((phase, names))
            if phase == "blender" and names == [provider.name]:
                def observe(target, *_args, **_kwargs):
                    if target == weed:
                        raise gui.TargetPlannedExclusionError(
                            "current live delivery blocks only the weed target",
                            reason_token=(
                                fixture["expected_result"][
                                    "blocked_reason_token"
                                ]
                            ),
                            target_spm=weed,
                            producer_spm=provider,
                            evidence={
                                "sync_outcome": "completed",
                                "sync_outcome_authoritative": False,
                                "normalization_postcondition": "not_run",
                            },
                        )
                    return {
                        "target_spm": str(target),
                        "live_audit_report": "captured.json",
                        "selected_contract": {"handoff": {"status": "ready"}},
                    }

                with mock.patch.object(
                    app,
                    "_cluster_normalization_stage_observation",
                    side_effect=observe,
                ):
                    runnable, contracts = app._cluster_relation_input_plan(
                        roots,
                        "captured",
                        provider,
                    )
                self.assertEqual(
                    list(runnable),
                    [path for path in roots if path != weed],
                )
                self.assertEqual(len(contracts), 4)
            app._phase_failed_items = set()
            app._phase_abort_reason = None
            return True

        app._run_batch = mock.Mock(side_effect=fake_batch)
        with mock.patch.object(
            gui,
            "expand_blender_repair_targets",
            return_value=(expanded, dependency_map, {str(provider)}),
        ), mock.patch.object(gui, "save_state"):
            result = app._run_full_pipeline(root_items)

        self.assertFalse(result)
        downstream_blender = [
            names for phase, names in calls
            if phase == "blender" and names != [provider.name]
        ]
        self.assertEqual(
            downstream_blender,
            [[path.name for path in roots if path != weed]],
        )
        summary = app._phase_result_summary
        self.assertEqual(
            {
                key: summary[key]
                for key in (
                    "completed_count",
                    "blocked_count",
                    "planned_excluded_count",
                    "dependency_blocked_count",
                    "failed_count",
                )
            },
            {
                key: fixture["expected_result"][key]
                for key in (
                    "completed_count",
                    "blocked_count",
                    "planned_excluded_count",
                    "dependency_blocked_count",
                    "failed_count",
                )
            },
        )
        weed_outcome = next(
            row for row in summary["target_outcomes"]
            if row["target_name"] == weed.name
        )
        self.assertEqual(weed_outcome["outcome"], "planned_excluded")
        self.assertEqual(
            weed_outcome["reason_token"],
            fixture["expected_result"]["blocked_reason_token"],
        )
        # 자동 복구 대상 is a promise that a repair is coming, so it may not
        # sit on a row that is not being repaired (#160).  The exclusion is
        # still visible, still target-local, and still names the same reason.
        self.assertNotIn(
            "자동 복구 대상",
            app.state[str(weed)]["blend_status"],
        )
        self.assertIn(
            "Generator/Cluster Sync",
            app.state[str(weed)]["blend_status"],
        )
        self.assertIn("Push 대기", app.state[str(weed)]["push_status"])
        self.assertNotIn(
            "Sync excluded",
            app.state[str(weed)]["blend_status"],
        )
        self.assertEqual(
            app.state[str(weed)]["push_status_error"]["reason_token"],
            fixture["expected_result"]["blocked_reason_token"],
        )

    def test_issue16_shared_queue_result_persists_causal_counts_and_token(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        fixture = self.issue16_fixture()
        targets = [
            {"spm": Path("blackgum") / row["target"], "checked": True}
            for row in fixture["targets"]
        ]
        blocked_name = "SK_weed_blackgum_01.spm"
        target_outcomes = [
            {
                "target": str(item["spm"]),
                "target_name": item["spm"].name,
                "outcome": (
                    "planned_excluded"
                    if item["spm"].name == blocked_name
                    else "completed"
                ),
                "reason_token": (
                    fixture["expected_result"]["blocked_reason_token"]
                    if item["spm"].name == blocked_name
                    else None
                ),
                "evidence": (
                    {"sync_outcome_authoritative": False}
                    if item["spm"].name == blocked_name
                    else {}
                ),
            }
            for item in targets
        ]
        expected = {
            "selected_count": 5,
            "completed_count": 4,
            "blocked_count": 1,
            "planned_excluded_count": 1,
            "dependency_blocked_count": 0,
            "failed_count": 0,
            "target_outcomes": target_outcomes,
            "shared_failures": [],
        }

        class Lease:
            def __init__(self):
                self.finished = False
                self.result = None
                self.success = None

            def finish(self, success=True, result=None):
                self.finished = True
                self.success = success
                self.result = result

        lease = Lease()
        app.shared_queue_runtime = mock.Mock(
            wait_for_turn=mock.Mock(return_value=lease)
        )

        def run_pipeline(*_args, **_kwargs):
            app._phase_result_summary = expected
            app._phase_failed_items = {
                str(item["spm"])
                for item in targets
                if item["spm"].name == blocked_name
            }
            return False

        app._run_full_pipeline = mock.Mock(side_effect=run_pipeline)
        job = {
            "id": 16,
            "label": "Issue 16 replay",
            "mode": "pipeline",
            "terminal_phase": "push",
            "selected_scope": True,
            "targets": targets,
            "shared_queue_job_id": "source-run-replay",
        }
        with mock.patch.object(
            app,
            "_freeze_batch_production_source_manifest",
        ):
            app._run_queued_batch_job(job)

        self.assertFalse(lease.success)
        self.assertEqual(lease.result["outcome"], "partial")
        for key, value in fixture["expected_result"].items():
            if key == "blocked_reason_token":
                continue
            self.assertEqual(lease.result[key], value)
        blocked = next(
            row for row in lease.result["target_outcomes"]
            if row["outcome"] == "planned_excluded"
        )
        self.assertEqual(
            blocked["reason_token"],
            fixture["expected_result"]["blocked_reason_token"],
        )
        self.assertIn(
            fixture["expected_result"]["blocked_reason_token"],
            lease.result["error"],
        )

    def test_latest_terminal_kinds_do_not_become_failures(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        kinds = {
            "imported.spm": ("imported_ok", "완료"),
            "ready.spm": ("ready", "준비됨"),
            "pending.spm": (
                "exported_pending_unreal",
                "export 완료 · Unreal 대기",
            ),
            "cancelled.spm": (
                "internal_error",
                "본 세팅 실행 실패: 사용자 중지",
            ),
            "failed.spm": ("data_error", "실제 import 실패"),
        }
        targets = []
        for name, (kind, message) in kinds.items():
            target = Path(name)
            iid = str(target)
            targets.append({"spm": target})
            app.state[iid] = {
                "push_status": message,
                "push_status_kind": kind,
                "push_status_error": {
                    "kind": kind,
                    "message": message,
                },
            }
        app._phase_failed_items = set(kinds)

        summary = app._summarize_phase_targets(targets, phase="push")
        outcomes = {
            row["target_name"]: row["outcome"]
            for row in summary["target_outcomes"]
        }

        self.assertEqual(summary["completed_count"], 2)
        self.assertEqual(summary["pending_count"], 1)
        self.assertEqual(summary["cancelled_count"], 1)
        self.assertEqual(summary["failed_count"], 1)
        self.assertEqual(outcomes["imported.spm"], "completed")
        self.assertEqual(outcomes["ready.spm"], "completed")
        self.assertEqual(outcomes["pending.spm"], "pending_unreal")
        self.assertEqual(outcomes["cancelled.spm"], "cancelled")
        self.assertEqual(outcomes["failed.spm"], "failed")

    def test_all_completed_summary_overrides_late_stop_flag(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        app.stop_flag.set()
        targets = [
            {"spm": Path(f"completed_{index}.spm")}
            for index in range(29)
        ]
        summary = {
            "selected_count": 29,
            "completed_count": 29,
            "pending_count": 0,
            "cancelled_count": 0,
            "blocked_count": 0,
            "owner_lost_count": 0,
            "planned_excluded_count": 0,
            "dependency_blocked_count": 0,
            "failed_count": 0,
            "target_outcomes": [
                {
                    "target": str(item["spm"]),
                    "target_name": item["spm"].name,
                    "outcome": "completed",
                    "reason_token": None,
                    "evidence": {},
                }
                for item in targets
            ],
            "shared_failures": [],
        }

        class Lease:
            def __init__(self):
                self.finished = False
                self.success = None
                self.result = None
                self.terminal_status = None

            def finish(
                self,
                success=True,
                result=None,
                terminal_status=None,
            ):
                self.finished = True
                self.success = success
                self.result = result
                self.terminal_status = terminal_status

        lease = Lease()
        app.shared_queue_runtime = mock.Mock(
            wait_for_turn=mock.Mock(return_value=lease)
        )

        def run_pipeline(*_args, **_kwargs):
            app._phase_result_summary = summary
            return False

        app._run_full_pipeline = mock.Mock(side_effect=run_pipeline)
        job = {
            "id": 83,
            "label": "sequence 83 replay",
            "mode": "pipeline",
            "terminal_phase": "push",
            "selected_scope": True,
            "targets": targets,
            "shared_queue_job_id": "sequence-83",
        }
        with mock.patch.object(
            app,
            "_freeze_batch_production_source_manifest",
        ):
            app._run_queued_batch_job(job)

        self.assertTrue(lease.success)
        self.assertIsNone(lease.terminal_status)
        self.assertEqual(lease.result["outcome"], "completed")
        self.assertEqual(lease.result["completed_count"], 29)
        self.assertEqual(lease.result["failed_count"], 0)

    def test_operator_cancel_is_durable_result_not_status_error(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        app._retry_transition = mock.Mock()
        iid = "cancelled.spm"
        with mock.patch.object(gui, "save_state"):
            app._record_phase_status(
                iid,
                "spm_status",
                "중지: 사용자 중지",
                "cancelled",
                "사용자 중지",
            )

        entry = app.state[iid]
        self.assertEqual(entry["spm_status_kind"], "cancelled")
        self.assertNotIn("spm_status_error", entry)
        self.assertEqual(
            entry["spm_status_result"]["outcome"],
            "cancelled",
        )
        app._phase_failed_items = set()
        summary = app._summarize_phase_targets(
            [{"spm": Path(iid)}],
            phase="spm",
        )
        self.assertEqual(summary["cancelled_count"], 1)
        self.assertEqual(summary["failed_count"], 0)

    def test_truly_shared_failure_is_recorded_once_with_affected_targets(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        provider = Path("cluster") / "SK_shared_cluster.spm"
        roots = [Path("SK_tree_a.spm"), Path("SK_tree_b.spm")]
        app._active_blender_dependency_map = {
            str(root): (str(provider),) for root in roots
        }
        app.state[str(provider)] = {
            "blend_status_kind": "data_error",
            "blend_status_error": {
                "kind": "data_error",
                "message": "shared provider failed its own postcondition",
            },
        }
        for root in roots:
            app.state[str(root)] = {
                "blend_status_kind": "dependency_blocked",
                "blend_status_error": {
                    "kind": "dependency_blocked",
                    "message": "provider output stale",
                    "reason_token": "shared_dependency_failed",
                    "blocked_by": [str(provider)],
                    "dependency_artifacts": {
                        str(provider): {
                            "status": "stale",
                            "reason": "saved output key changed",
                        }
                    },
                },
            }

        summary = app._build_pipeline_result_summary(
            [{"spm": root} for root in roots],
            {str(provider)},
            {str(root) for root in roots},
            None,
        )

        self.assertEqual(summary["completed_count"], 0)
        self.assertEqual(summary["blocked_count"], 2)
        self.assertEqual(summary["failed_count"], 0)
        self.assertEqual(len(summary["shared_failures"]), 1)
        self.assertEqual(
            summary["shared_failures"][0]["affected_targets"],
            [str(root) for root in roots],
        )
        self.assertTrue(all(
            row["reason_token"] == "dependency_output_stale"
            and row["evidence"]["blocked_by"] == [str(provider)]
            for row in summary["target_outcomes"]
        ))

    def test_failure_record_is_bound_to_target_content_and_code_revision(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        app._retry_transition = mock.Mock()
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "SK_bound_failure.spm"
            target.write_bytes(b"source-v1")
            iid = str(target)
            with mock.patch.object(gui, "save_state"):
                app._record_phase_status(
                    iid,
                    "blend_status",
                    "실패: test",
                    "data_error",
                    "구조화된 테스트 실패",
                    details={"reason_token": "data_error"},
                )
            error = copy.deepcopy(
                app.state[iid]["blend_status_error"]
            )

            self.assertEqual(
                app._failure_record_freshness(iid, error)["status"],
                "current",
            )
            self.assertEqual(len(error["evidence_sha256"]), 64)
            self.assertEqual(len(error["provenance_sha256"]), 64)
            self.assertEqual(
                error["production_source_revision"],
                gui._PROCESS_PRODUCTION_SOURCE_MANIFEST.content_hash,
            )
            target.touch()
            self.assertEqual(
                app._failure_record_freshness(iid, error)["status"],
                "current",
            )
            target.write_bytes(b"source-v2-changed")
            changed = app._failure_record_freshness(iid, error)

        self.assertEqual(changed["status"], "invalid")
        self.assertIn("content key", changed["reason"])

    def test_failure_record_from_old_code_revision_is_invalidated(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        app._retry_transition = mock.Mock()
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "SK_old_revision.spm"
            target.write_bytes(b"source")
            iid = str(target)
            with mock.patch.object(gui, "save_state"):
                app._record_phase_status(
                    iid,
                    "push_status",
                    "실패: old code",
                    "internal_error",
                    "old code failure",
                )
            error = copy.deepcopy(app.state[iid]["push_status_error"])
            error["failure_provenance"][
                "production_source_revision"
            ] = "0" * 64
            error["provenance_sha256"] = app._canonical_receipt_sha256(
                error["failure_provenance"]
            )
            verdict = app._failure_record_freshness(iid, error)

        self.assertEqual(verdict["status"], "invalid")
        self.assertIn("revision", verdict["reason"])

    def test_dependency_record_is_invalidated_when_producer_artifact_changes(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            consumer = root / "SK_consumer.spm"
            provider = root / "Cluster" / "SK_provider.spm"
            provider.parent.mkdir()
            consumer.write_bytes(b"consumer")
            provider.write_bytes(b"provider")
            provider_blend = gui.blend_path_for(provider)
            provider_blend.write_bytes(b"provider-output-v1")
            artifact_identity = app._dependency_artifact_identity(
                provider,
                "blender",
                {},
            )
            details = {
                "blocked_by": [str(provider)],
                "dependency_artifacts": {
                    str(provider): {
                        "status": "stale",
                        "phase": "blender",
                        "reason": "saved producer failure",
                        "artifact_identity": artifact_identity,
                    },
                },
            }
            binding = app._bind_failure_record(
                str(consumer),
                "dependency_blocked",
                "producer output stale",
                details,
            )
            error = {
                "kind": "dependency_blocked",
                "message": "producer output stale",
                **copy.deepcopy(details),
                **binding,
            }
            self.assertEqual(
                app._failure_record_freshness(str(consumer), error)[
                    "status"
                ],
                "current",
            )
            provider_blend.write_bytes(b"provider-output-v2-changed")
            verdict = app._failure_record_freshness(
                str(consumer), error
            )

        self.assertEqual(verdict["status"], "invalid")
        self.assertIn("producer artifact content key", verdict["reason"])

    def test_dependency_record_is_invalidated_when_import_status_changes(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            consumer = root / "SK_consumer.spm"
            provider = root / "SK_provider.spm"
            consumer.write_bytes(b"consumer")
            provider.write_bytes(b"provider")
            details = {
                "blocked_by": [str(provider)],
                "dependency_artifacts": {
                    str(provider): {
                        "status": "waiting",
                        "phase": "push",
                        "reason": "Unreal import receipt pending",
                        "artifact_identity": app._dependency_artifact_identity(
                            provider,
                            "push",
                            {},
                        ),
                    },
                },
            }
            error = {
                "kind": "dependency_blocked",
                "message": "provider import pending",
                **copy.deepcopy(details),
                **app._bind_failure_record(
                    str(consumer),
                    "dependency_blocked",
                    "provider import pending",
                    details,
                ),
            }
            with mock.patch.object(
                app,
                "_dependency_artifact_verdict",
                return_value={"status": "current", "phase": "push"},
            ):
                verdict = app._failure_record_freshness(
                    str(consumer), error
                )

        self.assertEqual(verdict["status"], "invalid")
        self.assertIn("artifact 상태", verdict["reason"])

    def test_legacy_failure_without_content_key_is_removed_before_routing(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        iid = "legacy_failure.spm"
        effective, verdicts = app._effective_failure_entry(iid, {
            "push_status_kind": "data_error",
            "push_status_error": {
                "kind": "data_error",
                "message": "old unbound failure",
            },
        })

        self.assertEqual(effective["push_status_kind"], "data_error")
        self.assertNotIn("push_status_error", effective)
        self.assertEqual(verdicts["push_status"]["status"], "invalid")
        self.assertIn("content key", verdicts["push_status"]["reason"])

    def test_blender_dependency_verdict_reads_saved_output_truth(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        app._repair_output_state = mock.Mock(
            side_effect=AssertionError("dependency gate must not run full audit")
        )
        with tempfile.TemporaryDirectory() as temporary:
            dependency = Path(temporary) / "Cluster" / "SK_provider.spm"
            dependency.parent.mkdir()
            dependency.write_bytes(b"producer")
            blend = gui.blend_path_for(dependency)
            blend.write_bytes(b"saved blend")
            with mock.patch.object(
                app,
                "_saved_dependency_blender_receipt_current",
                return_value=True,
            ) as current_receipt:
                current = app._dependency_artifact_verdict(
                    dependency,
                    phase="blender",
                )
            app._pipeline_dependency_artifact_cache.clear()
            with mock.patch.object(
                app,
                "_saved_dependency_blender_receipt_current",
                return_value=False,
            ):
                stale = app._dependency_artifact_verdict(
                    dependency,
                    phase="blender",
                )
            app._pipeline_dependency_artifact_cache.clear()
            blend.unlink()
            missing = app._dependency_artifact_verdict(
                dependency,
                phase="blender",
            )

        self.assertEqual(current["status"], "current")
        self.assertEqual(stale["status"], "stale")
        self.assertEqual(missing["status"], "missing")
        self.assertIn("Blender 산출물이 없습니다", missing["reason"])
        current_receipt.assert_called_once_with(dependency)
        app._repair_output_state.assert_not_called()

    def test_dependency_receipt_check_never_migrates_saved_reports(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        dependency = Path("sanitized") / "SK_provider.spm"
        with mock.patch.object(
            gui,
            "load_current_repair_pipeline_report",
            return_value={"handoff_preflight": {"status": "ok"}},
        ) as load_report:
            current = app._saved_dependency_blender_receipt_current(
                dependency
            )

        self.assertTrue(current)
        load_report.assert_called_once_with(
            dependency,
            migrate_legacy=False,
        )

    def test_push_dependency_verdict_reuses_current_import_receipt(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            dependency = root / "Cluster" / "SK_provider.spm"
            dependency.parent.mkdir()
            dependency.write_bytes(b"spm")
            manifest = root / "push_manifest.json"
            manifest.write_text(json.dumps({
                "items": [{
                    "queue_id": str(dependency),
                    "fingerprint": "export-current",
                    "exported_files": [],
                    "handoff_files": [],
                    "code_files": [],
                }],
            }), encoding="utf-8")
            snapshot = {"blend": {"size": 5}}
            app.state[str(dependency)] = {
                "push_status_kind": "data_error",
                "push_status": "실패: 이번 실행의 RPC 오류",
                "push_source_fingerprint_cache": {
                    "version": gui.PUSH_SOURCE_FINGERPRINT_CACHE_VERSION,
                    "fingerprint": "source-current",
                    "snapshot": snapshot,
                },
                "push_export_cache": {
                    "source_fingerprint": "source-current",
                    "manifest": str(manifest),
                    "fingerprint": "export-current",
                },
                "push_import_fingerprint": "export-current",
            }
            app._push_source_dependency_paths = mock.Mock(return_value=[])
            with mock.patch.object(
                gui,
                "push_source_snapshot",
                return_value=snapshot,
            ):
                verdict = app._current_push_output_artifact_state(
                    dependency
                )

        self.assertEqual(verdict["status"], "current")
        self.assertIn("Unreal import 영수증", verdict["reason"])

    def test_dependency_waiting_is_pending_not_terminal_failure(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        app._retry_transition = mock.Mock()
        iid = str(Path("SK_waiting_consumer.spm"))
        with mock.patch.object(gui, "save_state"):
            app._set_push_state(
                iid,
                "dependency_waiting",
                "대기: producer Unreal import 완료 영수증 대기",
                details={"blocked_by": ["SK_provider.spm"]},
            )

        entry = app.state[iid]
        self.assertEqual(entry["push_status_kind"], "dependency_waiting")
        self.assertNotIn("push_status_error", entry)
        outcome = app._target_authoritative_result(iid, "push")
        self.assertEqual(outcome["outcome"], "pending_unreal")
        transition = app._retry_transition.call_args
        self.assertEqual(transition.args[1], gui.RETRY_STAGE_POST_CHECK)
        self.assertNotIn("terminal_reason", transition.kwargs)

    def test_dependency_wrapper_without_exact_root_is_loud_provenance_failure(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        provider = Path("cluster") / "SK_missing_root_reason.spm"
        target = Path("SK_bush_blackgum_02.spm")
        app._active_blender_dependency_map = {}

        summary = app._build_pipeline_result_summary(
            [{"spm": target}],
            {str(provider)},
            {str(target)},
            None,
        )

        row = summary["target_outcomes"][0]
        self.assertEqual(row["outcome"], "blocked")
        self.assertEqual(
            row["reason_token"], "dependency_root_reason_missing"
        )
        self.assertEqual(row["evidence"]["blocked_by"], [])
        self.assertNotEqual(row["reason_token"], "shared_dependency_failed")

    def test_blender_window_guard_skips_own_tk_windows_before_title_query(self):
        gui = load_gui_module()
        blend = Path(r"C:\assets\tree.blend")
        queried = []

        class FakeUser32:
            def EnumWindows(self, callback, _extra):
                callback(101, 0)
                callback(202, 0)

            def GetWindowThreadProcessId(self, window, pid_pointer):
                pid_pointer._obj.value = (
                    gui.os.getpid() if window == 101 else gui.os.getpid() + 1
                )
                return 1

            def GetWindowTextLengthW(self, window):
                queried.append(window)
                return len(f"{blend.resolve()} - Blender")

            def GetWindowTextW(self, _window, buffer, _length):
                buffer.value = f"{blend.resolve()} - Blender"
                return len(buffer.value)

        with mock.patch.object(
            gui.ctypes,
            "windll",
            mock.Mock(user32=FakeUser32()),
        ):
            titles = gui.blender_open_file_window_titles(blend)

        self.assertEqual(queried, [202])
        self.assertEqual(titles, [f"{blend.resolve()} - Blender"])

    def test_repair_report_is_read_once_inside_one_status_scope(self):
        gui = load_gui_module()
        with tempfile.TemporaryDirectory() as temporary:
            report = Path(temporary) / "repair.json"
            report.write_text('{"status":"ok"}', encoding="utf-8")
            original_read_text = gui.Path.read_text
            reads = []

            def tracked_read_text(path, *args, **kwargs):
                reads.append(path)
                return original_read_text(path, *args, **kwargs)

            with mock.patch.object(gui.Path, "read_text", tracked_read_text):
                with gui.repair_report_read_scope():
                    first = gui._read_repair_pipeline_json(report)
                    second = gui._read_repair_pipeline_json(report)
                third = gui._read_repair_pipeline_json(report)

        self.assertIs(first, second)
        self.assertEqual(first, third)
        self.assertEqual(reads, [report, report])

    def test_repair_state_projection_reuses_the_scoped_report_read(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        with tempfile.TemporaryDirectory() as temporary:
            report = Path(temporary) / "repair.json"
            report.write_text(
                json.dumps({
                    "cluster_assembly_manifest": {"status": "ready"},
                }),
                encoding="utf-8",
            )
            original_read_text = gui.Path.read_text
            reads = []

            def tracked_read_text(path, *args, **kwargs):
                reads.append(path)
                return original_read_text(path, *args, **kwargs)

            app._repair_output_state_scoped = mock.Mock(
                side_effect=lambda _spm: (
                    gui._read_repair_pipeline_json(report)
                    and {
                        "current": True,
                        "push_ready": True,
                        "kind": "ready",
                        "reason": "준비됨 ✓",
                    }
                )
            )
            projection = {}
            with mock.patch.object(
                gui,
                "repair_pipeline_report_path",
                return_value=report,
            ), mock.patch.object(gui.Path, "read_text", tracked_read_text):
                state = app._repair_output_state(
                    Path(temporary) / "SK_tree_test_01.spm",
                    pipeline_projection_out=projection,
                )

        self.assertTrue(state["current"])
        self.assertEqual(
            projection,
            {"cluster_assembly_manifest": {"status": "ready"}},
        )
        self.assertEqual(reads, [report])

    def test_unreal_process_probe_uses_locale_independent_bytes(self):
        gui = load_gui_module()
        completed = mock.Mock(
            stdout=b"UnrealEditor.exe   1234 Console   \xc1"
        )
        with mock.patch.object(
            gui.subprocess,
            "run",
            return_value=completed,
        ) as run:
            self.assertTrue(gui.App._unreal_running())

        self.assertNotIn("text", run.call_args.kwargs)

    def test_process_runner_cleans_up_child_when_progress_callback_raises(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        app.cfg = {"process_poll_interval": 0.05}
        proc = mock.Mock()
        proc.poll.return_value = None
        proc.sk_log_handle = None

        def broken_progress(_elapsed, _line):
            raise RuntimeError("UI callback failed")

        with tempfile.TemporaryDirectory() as temp_dir, mock.patch.object(
            gui, "LOG_DIR", Path(temp_dir)
        ), mock.patch.object(
            gui, "launch_limited", return_value=proc
        ), mock.patch.object(
            gui, "terminate_process_tree", return_value=True
        ) as terminate, mock.patch.object(
            gui, "close_process_kill_job", return_value=True
        ) as close_job:
            with self.assertRaisesRegex(RuntimeError, "UI callback failed"):
                app._run_limited(
                    ["worker.exe"], "worker.log", timeout=5,
                    progress_callback=broken_progress,
                )

        terminate.assert_called_once_with(proc)
        close_job.assert_called_once_with(proc)
        self.assertNotIn(proc, app.active_procs)

    def test_process_runner_closes_job_after_parent_already_exited(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        app.cfg = {"process_poll_interval": 0.05}
        proc = mock.Mock()
        proc.poll.return_value = 7
        proc.returncode = 7
        proc.sk_log_handle = None

        with tempfile.TemporaryDirectory() as temp_dir, mock.patch.object(
            gui, "LOG_DIR", Path(temp_dir)
        ), mock.patch.object(
            gui, "launch_limited", return_value=proc
        ), mock.patch.object(
            gui, "terminate_process_tree"
        ) as terminate, mock.patch.object(
            gui, "close_process_kill_job", return_value=True
        ) as close_job:
            code, _log_file = app._run_limited(
                ["worker.exe"], "worker.log", timeout=5
            )

        self.assertEqual(code, 7)
        terminate.assert_not_called()
        close_job.assert_called_once_with(proc)
        self.assertNotIn(proc, app.active_procs)

    def test_process_runner_accepts_child_authoritative_timeout(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        app.cfg = {"process_poll_interval": 0.05}

        class TimedProcess:
            def __init__(self):
                self.started = gui.time.monotonic()
                self.returncode = None
                self.sk_log_handle = None

            def poll(self):
                elapsed = gui.time.monotonic() - self.started
                if elapsed >= 0.15:
                    self.returncode = 0
                return self.returncode

        with tempfile.TemporaryDirectory() as temp_dir, mock.patch.object(
            gui, "LOG_DIR", Path(temp_dir)
        ), mock.patch.object(
            gui,
            "launch_limited",
            return_value=TimedProcess(),
        ), mock.patch.object(
            gui, "terminate_process_tree"
        ) as terminate, mock.patch.object(
            gui, "close_process_kill_job", return_value=True
        ):
            code, _log_file = app._run_limited(
                ["worker.exe"],
                "worker.log",
                timeout=None,
            )

        self.assertEqual(code, 0)
        terminate.assert_not_called()

    def test_process_runner_none_timeout_still_honors_stop(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        app.cfg = {"process_poll_interval": 0.05}
        app.stop_flag.set()
        proc = mock.Mock()
        proc.poll.return_value = None
        proc.sk_log_handle = None

        with tempfile.TemporaryDirectory() as temp_dir, mock.patch.object(
            gui, "LOG_DIR", Path(temp_dir)
        ), mock.patch.object(
            gui, "launch_limited", return_value=proc
        ), mock.patch.object(
            gui, "terminate_process_tree", return_value=True
        ), mock.patch.object(
            gui, "close_process_kill_job", return_value=True
        ):
            with self.assertRaisesRegex(RuntimeError, "사용자 중지") as caught:
                app._run_limited(
                    ["worker.exe"],
                    "worker.log",
                    timeout=None,
                )
        self.assertEqual(caught.exception.kind, "cancelled")

    def test_process_runner_resets_inactivity_only_on_progress_marker(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        app.cfg = {"process_poll_interval": 0.05}
        clock = {"now": 0.0}

        class TimedProcess:
            def __init__(self, log_file):
                self.log_file = Path(log_file)
                self.returncode = None
                self.sk_log_handle = None
                self.wrote_progress = False

            def poll(self):
                if clock["now"] >= 0.06 and not self.wrote_progress:
                    self.log_file.write_text("PHASE_TWO\n", encoding="utf-8")
                    self.wrote_progress = True
                if clock["now"] >= 0.14:
                    self.returncode = 0
                return self.returncode

        def launch(_cmd, _cfg, **kwargs):
            return TimedProcess(kwargs["log_file"])

        def sleep(seconds):
            clock["now"] += seconds

        with tempfile.TemporaryDirectory() as temp_dir, mock.patch.object(
            gui, "LOG_DIR", Path(temp_dir)
        ), mock.patch.object(
            gui, "launch_limited", side_effect=launch
        ), mock.patch.object(
            gui.time, "monotonic", side_effect=lambda: clock["now"]
        ), mock.patch.object(
            gui.time, "sleep", side_effect=sleep
        ), mock.patch.object(
            gui, "terminate_process_tree"
        ) as terminate, mock.patch.object(
            gui, "close_process_kill_job", return_value=True
        ):
            code, _log_file = app._run_limited(
                ["worker.exe"],
                "worker.log",
                timeout=None,
                inactivity_timeout=0.08,
                inactivity_timeout_by_marker={"PHASE_TWO": 0.08},
            )

        self.assertEqual(code, 0)
        terminate.assert_not_called()

    def test_process_runner_heartbeat_text_does_not_reset_inactivity(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        app.cfg = {"process_poll_interval": 0.05}
        clock = {"now": 0.0}

        class StalledProcess:
            def __init__(self, log_file):
                self.log_file = Path(log_file)
                self.returncode = None
                self.sk_log_handle = None
                self.sequence = 0

            def poll(self):
                if self.returncode is None:
                    self.sequence += 1
                    with self.log_file.open("a", encoding="utf-8") as handle:
                        handle.write(f"HEARTBEAT REAL_PROGRESS {self.sequence}\n")
                return self.returncode

        process_holder = {}

        def launch(_cmd, _cfg, **kwargs):
            process_holder["value"] = StalledProcess(kwargs["log_file"])
            return process_holder["value"]

        def sleep(seconds):
            clock["now"] += seconds

        def terminate(process):
            process.returncode = -9
            return True

        with tempfile.TemporaryDirectory() as temp_dir, mock.patch.object(
            gui, "LOG_DIR", Path(temp_dir)
        ), mock.patch.object(
            gui, "launch_limited", side_effect=launch
        ), mock.patch.object(
            gui.time, "monotonic", side_effect=lambda: clock["now"]
        ), mock.patch.object(
            gui.time, "sleep", side_effect=sleep
        ), mock.patch.object(
            gui, "terminate_process_tree", side_effect=terminate
        ), mock.patch.object(
            gui, "close_process_kill_job", return_value=True
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "진행 없음 시간 초과.*process_start",
            ) as raised:
                app._run_limited(
                    ["worker.exe"],
                    "worker.log",
                    timeout=None,
                    inactivity_timeout=0.08,
                    inactivity_timeout_by_marker={"REAL_PROGRESS": 0.08},
                )

        self.assertEqual(
            raised.exception.timeout_kind,
            "child_progress_inactivity",
        )

    def test_blender_job_has_one_live_audit_call_after_static_mesh_gate(self):
        tree = ast.parse(
            (SK_BATCH_DIR / "sk_batch_gui.pyw").read_text(encoding="utf-8")
        )
        app_class = next(
            node
            for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == "App"
        )
        job = next(
            node
            for node in app_class.body
            if isinstance(node, ast.FunctionDef) and node.name == "_job_blender"
        )
        # The single entry point is the recovery wrapper: the direct run
        # admits a registered target block to repair before it terminates
        # (#160).  It still has to be one call, still after the mesh gate.
        refresh_calls = [
            node
            for node in ast.walk(job)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "_cluster_receipt_with_recovery"
        ]
        mesh_calls = [
            node
            for node in ast.walk(job)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "material_preflight_mesh_reference_block"
        ]
        self.assertEqual(len(refresh_calls), 1)
        self.assertEqual(len(mesh_calls), 1)
        self.assertLess(mesh_calls[0].lineno, refresh_calls[0].lineno)

    def test_next_queued_job_keeps_live_audit_memo_generation(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        app.pending_batch_jobs = deque()
        app.active_batch_job = None
        app.batch_progress = mock.Mock()
        app.batch_progress_var = mock.Mock()
        app._set_batch_queue_controls = mock.Mock()
        app.spm_calibration_signature = None
        app.legacy_spm_calibration_signature = None
        app._reset_cluster_receipt_refresh_memo()
        app._cluster_receipt_refresh_memo["validated"] = {"result": True}
        app.pending_batch_jobs.append({
            "id": 2,
            "cfg": {},
            "force_rerun": False,
            "push_transport": "rpc",
            "inventory": {},
            "targets": [],
            "label": "queued",
        })

        class FakeThread:
            def start(self):
                return None

        with mock.patch.object(
            gui, "calibration_settings_signature", return_value="current"
        ), mock.patch.object(
            gui, "legacy_calibration_settings_signature", return_value=None
        ), mock.patch.object(
            gui.threading, "Thread", return_value=FakeThread()
        ):
            app._start_next_batch_job()

        self.assertIn("validated", app._cluster_receipt_refresh_memo)

    @staticmethod
    def targets(*names):
        return [{"spm": Path(name), "checked": True} for name in names]

    def test_legacy_cluster_bootstraps_after_missing_canonical_schedule(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        app.cfg = {"spm_parallel_jobs": 1}
        app.force_rerun = False
        app.spm_calibration_signature = "current"
        app.legacy_spm_calibration_signature = None
        attempted = []

        with tempfile.TemporaryDirectory() as temp_dir:
            cluster = Path(temp_dir) / "cluster"
            cluster.mkdir()
            legacy_spm = cluster / "branch_tree_test_01.spm"
            canonical_spm = cluster / "SK_branch_tree_test_01.spm"
            legacy_spm.write_bytes(b"legacy cluster spm")
            item = {
                "spm": canonical_spm,
                "checked": True,
                # Scan aliases the live legacy snapshot onto the canonical row
                # that stage ① will create.
                "spm_snapshot": gui.file_content_snapshot(legacy_spm),
            }

            def fake_spm(_iid, spm):
                attempted.append(app._prepare_pair_for_job(spm))

            app._job_spm = mock.Mock(side_effect=fake_spm)
            with mock.patch.object(
                gui, "LOG_DIR", Path(temp_dir) / "logs"
            ), mock.patch.object(gui, "save_state"):
                result = app._run_batch(
                    "spm", [item], emit_done=False
                )

            self.assertTrue(result)
            self.assertEqual(app._phase_failed_items, set())
            self.assertEqual(len(attempted), 1)
            self.assertEqual(attempted[0], canonical_spm.resolve())
            self.assertEqual(
                canonical_spm.read_bytes(), legacy_spm.read_bytes()
            )

    def test_missing_spm_during_scheduling_fails_only_that_row(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        app.cfg = {"spm_parallel_jobs": 1}
        app.force_rerun = False
        app.spm_calibration_signature = "current"
        app.legacy_spm_calibration_signature = None
        attempted = []

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            missing_spm = root / "SK_missing.spm"
            existing_spm = root / "SK_existing.spm"
            existing_spm.write_bytes(b"spm")
            targets = [
                {"spm": missing_spm, "checked": True},
                {"spm": existing_spm, "checked": True},
            ]

            def fake_spm(_iid, spm):
                attempted.append(spm)
                if not spm.exists():
                    raise FileNotFoundError(spm)

            app._job_spm = mock.Mock(side_effect=fake_spm)
            with mock.patch.object(
                gui, "LOG_DIR", root / "logs"
            ), mock.patch.object(gui, "save_state"):
                result = app._run_batch(
                    "spm", targets, emit_done=False
                )

        self.assertTrue(result)
        self.assertCountEqual(attempted, [missing_spm, existing_spm])
        self.assertEqual(app._phase_failed_items, {str(missing_spm)})
        self.assertEqual(
            app.state[str(missing_spm)]["spm_status_kind"],
            "data_error",
        )

    def test_blender_failure_persists_structured_repair_report(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        app.cfg = {"blender_parallel_jobs": 1}
        report = {
            "reason_token": "atlas_manifest_mirror_conflict_repairable",
            "evidence": {
                "status": "repairable",
                "target_spm": "sanitized-target.spm",
            },
            "repair_disposition": gui.REPAIR_UI_AUTOMATIC,
            "reason_ko": "동일 원본의 낡은 Atlas manifest 미러 충돌",
            "action_ko": "exact BAT로 낡은 미러를 갱신",
            "stage": "canonical_atlas_manifest_preflight",
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            spm = Path(temp_dir) / "Cluster" / "SK_leaf_test_01.spm"
            spm.parent.mkdir(parents=True)
            spm.write_bytes(b"spm")
            app._job_blender = mock.Mock(side_effect=gui.BatchItemError(
                "sanitized automatic repair request",
                kind="automatic_repair_pending",
                report=report,
            ))
            app._publish_repair_stage_contract = mock.Mock()
            with mock.patch.object(
                gui, "LOG_DIR", Path(temp_dir) / "logs"
            ), mock.patch.object(gui, "save_state"):
                app._run_batch(
                    "blender",
                    [{"spm": spm, "checked": True}],
                    emit_done=False,
                )

        saved = app.state[str(spm)]["blend_status_error"]
        self.assertEqual(saved["kind"], "automatic_repair_pending")
        self.assertEqual(saved["reason_token"], report["reason_token"])
        self.assertEqual(saved["evidence"], report["evidence"])
        self.assertEqual(saved["failure_report"], report)
        self.assertIn(
            "자동 복구 대기",
            app.state[str(spm)]["blend_status"],
        )

    def test_blender_repair_waits_for_cluster_sources_before_root_assets(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        app.cfg = {"blender_parallel_jobs": 2}
        completed_clusters = set()
        downstream_snapshots = []
        lock = threading.Lock()

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "Tree_elm"
            cluster = root / "Cluster"
            cluster_targets = [
                cluster / "SK_branch_elm_01.spm",
                cluster / "SK_leaf_elm_01.spm",
            ]
            root_targets = [
                root / "SK_Tree_elm_01.spm",
                root / "SK_Tree_elm_02.spm",
            ]
            targets = [
                {"spm": root_targets[0], "checked": True},
                {"spm": cluster_targets[0], "checked": True},
                {"spm": root_targets[1], "checked": True},
                {"spm": cluster_targets[1], "checked": True},
            ]

            def fake_blender(_iid, spm, _item):
                with lock:
                    if gui.is_cluster_source_spm(spm):
                        completed_clusters.add(spm)
                    else:
                        downstream_snapshots.append(set(completed_clusters))

            app._job_blender = mock.Mock(side_effect=fake_blender)
            with mock.patch.object(gui, "LOG_DIR", Path(temp_dir) / "logs"), \
                    mock.patch.object(gui, "save_state"):
                result = app._run_batch(
                    "blender", targets, emit_done=False
                )

        self.assertTrue(result)
        self.assertEqual(len(downstream_snapshots), len(root_targets))
        self.assertTrue(
            all(
                snapshot == set(cluster_targets)
                for snapshot in downstream_snapshots
            )
        )

    def test_cluster_producers_are_serial_per_owner_and_parallel_across_owners(
        self,
    ):
        gui = load_gui_module()
        app = self.make_app(gui)
        app.cfg = {"blender_parallel_jobs": 2}
        owner_a_started = threading.Event()
        owner_a_first_done = threading.Event()
        owner_b_started = threading.Event()
        order = []
        lock = threading.Lock()

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            cluster_a = root / "Tree_a" / "Cluster"
            cluster_b = root / "Tree_b" / "Cluster"
            a_first = cluster_a / "SK_cluster_a_01.spm"
            a_second = cluster_a / "SK_cluster_a_02.spm"
            b_first = cluster_b / "SK_cluster_b_01.spm"
            targets = [
                {"spm": a_first, "checked": True},
                {"spm": a_second, "checked": True},
                {"spm": b_first, "checked": True},
            ]

            def fake_blender(_iid, spm, _item):
                if spm == a_first:
                    with lock:
                        order.append("a1_start")
                    owner_a_started.set()
                    self.assertTrue(owner_b_started.wait(5))
                    owner_a_first_done.set()
                    with lock:
                        order.append("a1_done")
                elif spm == a_second:
                    self.assertTrue(owner_a_first_done.is_set())
                    with lock:
                        order.append("a2")
                else:
                    self.assertTrue(owner_a_started.wait(5))
                    owner_b_started.set()
                    with lock:
                        order.append("b1")

            app._job_blender = mock.Mock(side_effect=fake_blender)
            with mock.patch.object(
                gui, "LOG_DIR", root / "logs"
            ), mock.patch.object(gui, "save_state"):
                result = app._run_batch(
                    "blender", targets, emit_done=False
                )

        self.assertTrue(result)
        self.assertLess(order.index("a1_done"), order.index("a2"))
        self.assertIn("b1", order)

    def test_failed_cluster_repair_does_not_suppress_dependent_roots(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        app.cfg = {"blender_parallel_jobs": 1}
        calls = []

        with tempfile.TemporaryDirectory() as temp_dir:
            first_root = Path(temp_dir) / "Tree_elm"
            failed_cluster = (
                first_root / "Cluster" / "SK_branch_elm_01.spm"
            )
            blocked_root = first_root / "SK_Tree_elm_01.spm"
            independent_root = first_root / "SK_Tree_elm_02.spm"
            targets = [
                {
                    "spm": failed_cluster,
                    "checked": True,
                    "referenced_by_spms": (blocked_root,),
                },
                {"spm": blocked_root, "checked": True},
                {"spm": independent_root, "checked": True},
            ]

            def fake_blender(_iid, spm, _item):
                calls.append(spm)
                if spm == failed_cluster:
                    raise RuntimeError("cluster repair failed")

            app._job_blender = mock.Mock(side_effect=fake_blender)
            with mock.patch.object(
                gui, "LOG_DIR", Path(temp_dir) / "logs"
            ), mock.patch.object(
                gui,
                "cluster_relation_output_targets",
                return_value=(blocked_root,),
            ), mock.patch.object(gui, "save_state"):
                result = app._run_batch(
                    "blender", targets, emit_done=False
                )

        self.assertTrue(result)
        self.assertIn(failed_cluster, calls)
        self.assertIn(blocked_root, calls)
        self.assertIn(independent_root, calls)
        self.assertNotIn(str(blocked_root), app._phase_failed_items)
        self.assertNotEqual(
            app.state.get(str(blocked_root), {}).get("blend_status_kind"),
            "dependency_blocked",
        )

    def test_blender_repair_blocks_an_interactively_open_target(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        app.force_rerun = True
        app._prepare_pair_for_job = mock.Mock(side_effect=lambda spm: spm)
        app._leaf_reference_ready = mock.Mock(return_value=(True, "ok"))
        app._handoff_ready = mock.Mock(return_value=(False, "stale"))
        spm = Path("D:/asset/SK_branch_test_01.spm")
        blend = spm.with_suffix(".blend")

        with mock.patch.object(
            gui, "speedtree_output_spm_for", return_value=spm
        ), mock.patch.object(
            gui, "blend_path_for", return_value=blend
        ), mock.patch.object(
            gui,
            "blender_open_file_window_titles",
            return_value=[f"* SK_branch_test_01 [{blend}] - Blender 5.1"],
        ):
            with self.assertRaises(gui.BatchItemError) as raised:
                app._job_blender(
                    str(spm), spm, {"spm": spm, "checked": True}
                )

        self.assertEqual(raised.exception.kind, "manual_required")
        self.assertIn("대화형 Blender", str(raised.exception))

    def test_push_preflight_blocks_unsaved_interactive_blender_changes(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        app.active_push_transport = "headless"
        app._unreal_running = mock.Mock(return_value=False)
        app._handoff_ready = mock.Mock(return_value=(True, "ready"))
        spm = Path("D:/asset/SK_branch_test_01.spm")
        target = {"spm": spm, "checked": True}

        with mock.patch.object(
            gui,
            "blender_open_file_window_titles",
            return_value=["* SK_branch_test_01 [...] - Blender 5.1"],
        ), mock.patch.object(gui, "save_state"):
            ready, fatal = app._push_preflight([target])

        self.assertEqual(ready, [])
        self.assertIsNone(fatal)
        app._handoff_ready.assert_not_called()
        self.assertEqual(
            app.state[str(spm)]["push_status_kind"], "manual_required"
        )

    def test_push_preflight_consumes_same_pipeline_repair_contract(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        app.active_push_transport = "headless"
        app._unreal_running = mock.Mock(return_value=False)
        app._active_repair_stage_contracts = {}
        spm = Path("D:/asset/SK_tree_test_01.spm")
        target = {"spm": spm, "checked": True}
        app._handoff_ready = mock.Mock(
            side_effect=AssertionError(
                "same-pipeline Push must not repeat the ② handoff audit"
            )
        )
        app._validate_repair_stage_contract = mock.Mock(
            return_value=True
        )
        app._publish_repair_stage_contract(
            spm,
            ready=True,
            reason="준비됨 ✓",
        )

        with mock.patch.object(
            gui,
            "blender_open_file_window_titles",
            return_value=[],
        ), mock.patch.object(gui, "save_state"):
            ready, fatal = app._push_preflight([target])

        self.assertEqual(ready, [target])
        self.assertIsNone(fatal)
        app._handoff_ready.assert_not_called()
        app._validate_repair_stage_contract.assert_called_once_with(
            spm,
            mock.ANY,
        )
        self.assertIn("② 결과 재사용 1개", app.log.call_args.args[0])

    def test_standalone_push_still_runs_full_handoff_preflight(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        app.active_push_transport = "headless"
        app._unreal_running = mock.Mock(return_value=False)
        spm = Path("D:/asset/SK_tree_test_01.spm")
        target = {"spm": spm, "checked": True}
        app._handoff_ready = mock.Mock(
            return_value=(True, "준비됨 ✓")
        )

        with mock.patch.object(
            gui,
            "blender_open_file_window_titles",
            return_value=[],
        ), mock.patch.object(gui, "save_state"):
            ready, fatal = app._push_preflight([target])

        self.assertEqual(ready, [target])
        self.assertIsNone(fatal)
        app._handoff_ready.assert_called_once_with(spm)

    def test_stale_same_pipeline_contract_freezes_without_standalone_fallback(
        self,
    ):
        gui = load_gui_module()
        app = self.make_app(gui)
        app.active_push_transport = "headless"
        app._unreal_running = mock.Mock(return_value=False)
        app._active_repair_stage_contracts = {}
        spm = Path("D:/asset/SK_tree_stale_01.spm")
        target = {"spm": spm, "checked": True}
        app._handoff_ready = mock.Mock(
            side_effect=AssertionError(
                "stale same-generation evidence must not become standalone"
            )
        )
        app._publish_repair_stage_contract(
            spm,
            ready=True,
            reason="ready",
        )

        with mock.patch.object(
            gui,
            "blender_open_file_window_titles",
            return_value=[],
        ), mock.patch.object(gui, "save_state"):
            ready, fatal = app._push_preflight([target])

        self.assertEqual(ready, [])
        self.assertIsNone(fatal)
        app._handoff_ready.assert_not_called()
        state = app.state[str(spm)]
        self.assertEqual(
            state["push_status_kind"],
            "stale_execution_freeze",
        )
        self.assertIn(
            "STALE_EXECUTION_FREEZE",
            state["push_status_error"]["message"],
        )

    def test_same_pipeline_source_review_contract_blocks_without_reaudit(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        app.active_push_transport = "headless"
        app._unreal_running = mock.Mock(return_value=False)
        app._active_repair_stage_contracts = {}
        spm = Path("D:/asset/SK_tree_review_01.spm")
        target = {"spm": spm, "checked": True}
        app._handoff_ready = mock.Mock(
            side_effect=AssertionError("source review must not be re-audited")
        )
        app._publish_repair_stage_contract(
            spm,
            ready=False,
            reason="원본/재질 검토 필요 — Unreal Push 차단",
            kind="source_review",
        )

        with mock.patch.object(
            gui,
            "blender_open_file_window_titles",
            return_value=[],
        ), mock.patch.object(gui, "save_state"):
            ready, fatal = app._push_preflight([target])

        self.assertEqual(ready, [])
        self.assertIsNone(fatal)
        app._handoff_ready.assert_not_called()
        self.assertEqual(
            app.state[str(spm)]["push_status_kind"],
            "source_review",
        )

    def test_full_pipeline_keeps_repair_contract_until_push_then_clears_it(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        spm = Path("D:/asset/SK_tree_test_01.spm")
        targets = [{"spm": spm, "checked": True}]
        app._batch_job_inventory = mock.Mock(
            return_value={str(spm): targets[0]}
        )

        def run_phase(phase, _targets, emit_done=False):
            self.assertFalse(emit_done)
            app._phase_failed_items = set()
            app._phase_abort_reason = None
            if phase == "blender":
                app._publish_repair_stage_contract(
                    spm,
                    ready=True,
                    reason="준비됨 ✓",
                )
            if phase == "push":
                self.assertEqual(
                    app._repair_stage_contract(spm),
                    {
                        "ready": True,
                        "reason": "준비됨 ✓",
                        "kind": "ready",
                    },
                )
            return True

        app._run_batch = mock.Mock(side_effect=run_phase)
        with mock.patch.object(
            gui,
            "expand_blender_repair_targets",
            return_value=(targets, {}, set()),
        ):
            completed = app._run_full_pipeline(
                targets,
                terminal_phase="push",
                selected_scope=True,
                emit_done=False,
            )

        self.assertTrue(completed)
        self.assertEqual(
            [call.args[0] for call in app._run_batch.call_args_list],
            ["blender", "push"],
        )
        self.assertNotIn("_active_repair_stage_contracts", app.__dict__)

    def test_repair_stage_contract_uses_canonical_cluster_pair_key(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        app._active_repair_stage_contracts = {}
        mirror = Path(
            "D:/asset/Tree_test/cluster/branch_tree_test_01.spm"
        )
        canonical = mirror.with_name("SK_" + mirror.name)

        app._publish_repair_stage_contract(
            canonical,
            ready=True,
            reason="준비됨 ✓",
        )

        self.assertEqual(
            app._repair_stage_contract(mirror),
            {
                "ready": True,
                "reason": "준비됨 ✓",
                "kind": "ready",
            },
        )

    def test_repair_stage_contract_preserves_verified_push_dependencies(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        app._active_repair_stage_contracts = {}
        spm = Path("D:/asset/Tree_test/SK_tree_test_01.spm")
        dependency_contract = {
            "kind": "sk_batch_verified_push_dependencies",
            "schema_version": 1,
            "root_spm": str(spm),
            "assembly_status": "ready",
            "dependency_spms": [
                "D:/asset/Tree_test/Cluster/SK_cluster_test_01.spm"
            ],
            "evidence": "validated_cluster_assembly_manifest",
        }

        app._publish_repair_stage_contract(
            spm,
            ready=True,
            reason="ready",
            push_dependency_contract=dependency_contract,
        )
        dependency_contract["dependency_spms"].clear()

        published = app._repair_stage_contract(spm)
        self.assertEqual(
            published["push_dependency_contract"]["dependency_spms"],
            ["D:/asset/Tree_test/Cluster/SK_cluster_test_01.spm"],
        )

    def test_full_pipeline_clears_repair_contract_after_unexpected_error(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        target = {
            "spm": Path("D:/asset/SK_tree_test_01.spm"),
            "checked": True,
        }
        app._run_full_pipeline_stages = mock.Mock(
            side_effect=RuntimeError("unexpected")
        )

        with self.assertRaisesRegex(RuntimeError, "unexpected"):
            app._run_full_pipeline([target], emit_done=False)

        self.assertNotIn("_active_repair_stage_contracts", app.__dict__)

    def test_push_contract_wrapper_requires_and_preserves_current_envelope(self):
        gui = load_gui_module()
        with tempfile.TemporaryDirectory() as temp_dir, mock.patch.object(
            gui, "LOG_DIR", Path(temp_dir) / "logs"
        ), mock.patch.object(gui, "validate_preflight_envelope") as validate:
            root = Path(temp_dir) / "asset"
            spm = root / "SK_tree_contract_01.spm"
            spm.parent.mkdir(parents=True)
            spm.write_bytes(b"spm")
            envelope = {"source_fingerprint": "a" * 64}
            report = root / "reports" / (
                "SK_tree_contract_01_speedtree_repair_pipeline_report_codex.json"
            )
            report.parent.mkdir()
            report.write_text(
                json.dumps({
                    "speedtree_pipeline_contract": envelope,
                    "handoff_preflight": {"status": "ok"},
                }),
                encoding="utf-8",
            )

            contract_path = gui.App._push_material_contract(spm)

            payload = json.loads(contract_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["status"], "ok")
            self.assertEqual(payload["speedtree_pipeline_contract"], envelope)
            self.assertTrue(contract_path.name.endswith(f"{'a' * 16}.json"))
            validate.assert_called_once_with(envelope, spm, require_ok=True)

    def test_push_contract_uses_exact_isolated_repair_material_source(self):
        gui = load_gui_module()
        with tempfile.TemporaryDirectory() as temp_dir, mock.patch.object(
            gui, "LOG_DIR", Path(temp_dir) / "logs"
        ), mock.patch.object(gui, "validate_preflight_envelope") as validate:
            root = Path(temp_dir) / "tree"
            cluster = root / "cluster"
            spm = cluster / "SK_branch_test_01.spm"
            isolated = (
                cluster / ".sk_batch_isolated_bark" / "scope"
                / "tree" / "cluster" / spm.name
            )
            spm.parent.mkdir(parents=True)
            isolated.parent.mkdir(parents=True)
            spm.write_bytes(b"authored-production")
            isolated.write_bytes(b"isolated-normalized")
            production_identity = gui.source_identity(spm)
            isolated_identity = gui.source_identity(isolated)
            envelope = {
                "source_fingerprint": "b" * 64,
                "source": {
                    "spm": isolated_identity,
                    "stmat": [],
                },
            }
            report = cluster / "reports" / (
                "SK_branch_test_01_"
                "speedtree_repair_pipeline_report_codex.json"
            )
            report.parent.mkdir()
            report.write_text(
                json.dumps({
                    "speedtree_pipeline_contract": {
                        "source_fingerprint": "c" * 64,
                    },
                    "speedtree_material_handoff_contract": envelope,
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
                    "handoff_preflight": {"status": "ok"},
                }),
                encoding="utf-8",
            )

            contract_path = gui.App._push_material_contract(spm)

            payload = json.loads(contract_path.read_text(encoding="utf-8"))
            self.assertEqual(
                payload["speedtree_pipeline_contract"],
                envelope,
            )
            self.assertEqual(
                Path(payload["material_source_spm"]),
                isolated.resolve(),
            )
            validate.assert_called_once_with(
                envelope,
                isolated.resolve(),
                require_ok=True,
            )
            mismatched = json.loads(report.read_text(encoding="utf-8"))
            mismatched["speedtree_material_handoff_contract"]["source"][
                "spm"
            ] = production_identity
            with self.assertRaisesRegex(
                RuntimeError,
                "not bound to the current canonical SPM",
            ):
                gui._material_handoff_envelope_for_push(
                    mismatched,
                    spm,
                )

    def test_push_entrypoints_forward_strict_contract_to_blender_job(self):
        gui_source = (SK_BATCH_DIR / "sk_batch_gui.pyw").read_text(encoding="utf-8")
        gui_tree = ast.parse(gui_source)
        gui_functions = {
            node.name: node
            for node in ast.walk(gui_tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        for function_name in ("_export_manifest_item", "_job_push"):
            strings = {
                node.value
                for node in ast.walk(gui_functions[function_name])
                if isinstance(node, ast.Constant) and isinstance(node.value, str)
            }
            self.assertIn("--spm", strings)
            self.assertIn("--material-contract", strings)
            self.assertIn("--repair-evidence", strings)
            self.assertIn("--dependency-orchestrated", strings)

        blender_job_strings = set()
        for function_name in (
            "_job_blender",
            "_execute_material_preflight",
        ):
            blender_job_strings.update(
                node.value
                for node in ast.walk(gui_functions[function_name])
                if isinstance(node, ast.Constant)
                and isinstance(node.value, str)
            )
        self.assertIn("--speedtree-spm", blender_job_strings)
        self.assertIn("--canonical-spm", blender_job_strings)

        bwr_source = (
            SK_BATCH_DIR / "jobs" / "bwr_headless_job.py"
        ).read_text(encoding="utf-8")
        self.assertIn("name_stem=speedtree_spm.stem", bwr_source)
        self.assertIn("settings.name_stem = canonical_spm.stem", bwr_source)
        self.assertNotIn("canonical_spm.name[3:]", bwr_source)
        self.assertIn('report["cluster_source_skin_contract"]', bwr_source)
        self.assertIn(
            'repair_settings["cluster_source_skin_contract"] = is_cluster_source',
            bwr_source,
        )
        self.assertIn(
            'repair_settings["defer_cluster_export_to_normalizer"] = bool(',
            bwr_source,
        )
        self.assertNotIn("cluster_single_bone_rigid_binding", bwr_source)
        self.assertNotIn("rigid_existing_single_bone", bwr_source)
        self.assertIn("export_collection_contract_issues(", bwr_source)
        cluster_export_contract_source = (
            SK_BATCH_DIR.parent / "cluster_export_handoff_contract.py"
        ).read_text(encoding="utf-8")
        self.assertIn(
            "orphan_owned_export_empty:",
            cluster_export_contract_source,
        )
        self.assertIn(
            "cluster_unsuffixed_export_unit:",
            cluster_export_contract_source,
        )
        self.assertIn(
            "cluster_missing_normalized_export_pivot",
            cluster_export_contract_source,
        )

        push_source = (
            SK_BATCH_DIR / "jobs" / "send2ue_push_job.py"
        ).read_text(encoding="utf-8")
        self.assertNotIn(
            "PCG ST9 Texture Batch 산출물 누락; Unreal Push 중단",
            push_source,
        )
        self.assertNotIn(
            'texture_normalization.get("missing")\n                or export_collection_issues',
            push_source,
        )
        self.assertNotIn(
            'texture_normalization.get("missing")\n            or empty_material_slots',
            bwr_source,
        )
        self.assertIn("resolve_cluster_spm_pair(spm_path)", push_source)
        self.assertIn(
            'parser.add_argument("--dependency-orchestrated", action="store_true")',
            push_source,
        )
        self.assertIn(
            "Tree Push with Cluster Assembly must run through the SK Batch",
            push_source,
        )
        self.assertIn(
            "actionable_cluster_manifest",
            push_source,
        )
        self.assertIn(
            "not args.dependency_orchestrated",
            push_source,
        )
        self.assertIn(
            '"dependency_orchestrated": bool(args.dependency_orchestrated)',
            push_source,
        )
        self.assertIn(
            'parser.add_argument("--repair-evidence")',
            push_source,
        )
        self.assertIn(
            "validate_repair_push_evidence_bundle(",
            push_source,
        )
        self.assertIn(
            "validate_export_object_postcondition(",
            push_source,
        )
        self.assertIn(
            '"push_mutators_skipped": True',
            push_source,
        )
        sync_call = push_source.index("utilities.sync_unreal_mesh_folder_path()")
        folder_read = push_source.index(
            "folder = scene_props.unreal_mesh_folder_path", sync_call
        )
        self.assertLess(sync_call, folder_read)
        self.assertIn("orphan_owned_export_empty:", push_source)
        self.assertIn("wind_source_stem = spm_path.stem", push_source)
        self.assertIn(
            'f"{wind_source_stem}_dynamic_wind_import_from_megaplant_groups.json"',
            push_source,
        )
        self.assertNotIn(
            'f"{unit_name}_dynamic_wind_import_from_megaplant_groups.json"',
            push_source,
        )
        self.assertIn("resolve_dynamic_wind_policy(", push_source)
        self.assertIn(
            "unit_name = find_export_unit_name() or spm_path.stem",
            push_source,
        )
        self.assertNotIn("find_export_unit_name(spm_path.stem)", push_source)
        self.assertIn(
            '"wind_json": str(wind_json) if wind_json_enabled else None',
            push_source,
        )
        push_tree = ast.parse(push_source)
        calls = [node for node in ast.walk(push_tree) if isinstance(node, ast.Call)]

        def call_name(call):
            if isinstance(call.func, ast.Name):
                return call.func.id
            if isinstance(call.func, ast.Attribute):
                return call.func.attr
            return ""

        for function_name in (
            "load_speedtree_texture_readiness_contract",
            "consolidate_speedtree_group_materials",
            "normalize_speedtree_material_textures",
        ):
            matches = [call for call in calls if call_name(call) == function_name]
            self.assertEqual(len(matches), 1)
            keywords = {item.arg for item in matches[0].keywords}
            if function_name == "load_speedtree_texture_readiness_contract":
                self.assertTrue({"spm_path", "source_fbx_path"} <= keywords)
            else:
                self.assertIn("texture_contract", keywords)

    def test_data_and_manual_failures_do_not_stop_later_push_items(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        targets = self.targets("SK_bad.spm", "SK_manual.spm", "SK_after.spm")
        attempted = []

        def fake_push(_iid, spm):
            attempted.append(spm.name)
            if spm.name == "SK_bad.spm":
                raise gui.BatchItemError("missing material", kind="data_error")
            if spm.name == "SK_manual.spm":
                raise gui.BatchItemError(
                    "Unreal Push 수동 처리 필요: 본 1,800 > 1,500",
                    kind="manual_required",
                )

        with mock.patch.object(app, "_push_preflight", return_value=(targets, None)), mock.patch.object(
            app, "_job_push", side_effect=fake_push
        ), mock.patch.object(gui, "save_state"):
            result = app._run_batch("push", targets, emit_done=False)

        self.assertTrue(result)
        self.assertEqual(
            attempted, ["SK_bad.spm", "SK_manual.spm", "SK_after.spm"]
        )
        self.assertEqual(app.state["SK_bad.spm"]["push_status_kind"], "data_error")
        self.assertEqual(
            app.state["SK_manual.spm"]["push_status_kind"], "manual_required"
        )

    def test_push_scheduler_receives_same_pipeline_dependency_contract(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        root = Path("Tree_elm") / "SK_Tree_elm.spm"
        target = {"spm": root, "checked": True}
        dependency_contract = {
            "kind": "sk_batch_verified_push_dependencies",
            "schema_version": 1,
            "root_spm": str(root),
            "assembly_status": "pass_through",
            "dependency_spms": [],
            "evidence": "validated_cluster_assembly_manifest",
        }
        app.items = {str(root): target}
        app._active_repair_stage_contracts = {}
        app._validate_repair_stage_contract = mock.Mock(
            return_value=True
        )
        app._publish_repair_stage_contract(
            root,
            ready=True,
            reason="ready",
            push_dependency_contract=dependency_contract,
        )

        with mock.patch.object(
            gui,
            "expand_push_targets",
            return_value=([target], {str(root): ()}, set()),
        ) as expand, mock.patch.object(
            app, "_push_preflight", return_value=([target], None)
        ), mock.patch.object(
            app, "_job_push"
        ), mock.patch.object(gui, "save_state"):
            result = app._run_batch("push", [target], emit_done=False)

        self.assertTrue(result)
        forwarded = expand.call_args.kwargs["stage_dependency_contracts"]
        self.assertEqual(len(forwarded), 1)
        self.assertEqual(
            next(iter(forwarded.values())),
            dependency_contract,
        )

    def test_rpc_push_continues_roots_when_auto_cluster_failed(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        cluster = Path("Tree_elm") / "Cluster" / "SK_any_cluster.spm"
        blocked_root = Path("Tree_elm") / "SK_Tree_elm.spm"
        independent_root = Path("Tree_maple") / "SK_Tree_maple.spm"
        cluster_item = {"spm": cluster, "checked": False}
        blocked_item = {"spm": blocked_root, "checked": True}
        independent_item = {"spm": independent_root, "checked": True}
        expanded = [cluster_item, blocked_item, independent_item]
        app.items = {
            str(item["spm"]): item for item in expanded
        }
        attempted = []

        def fake_push(_iid, spm):
            attempted.append(spm)
            if spm == cluster:
                raise gui.BatchItemError(
                    "cluster import failed", kind="data_error"
                )

        with mock.patch.object(
            gui,
            "expand_push_targets",
            return_value=(
                expanded,
                {
                    str(blocked_root): (str(cluster),),
                    str(independent_root): (),
                },
                {str(cluster)},
            ),
        ), mock.patch.object(
            app, "_push_preflight", return_value=(expanded, None)
        ), mock.patch.object(
            app, "_job_push", side_effect=fake_push
        ), mock.patch.object(gui, "save_state"):
            result = app._run_batch(
                "push",
                [blocked_item, independent_item],
                emit_done=False,
            )

        self.assertTrue(result)
        self.assertEqual(
            attempted,
            [cluster, blocked_root, independent_root],
        )
        self.assertNotEqual(
            app.state.get(str(blocked_root), {}).get("push_status_kind"),
            "dependency_blocked",
        )

    def test_rpc_push_continues_when_failed_provider_import_is_current(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        cluster = Path("Tree_elm") / "Cluster" / "SK_cluster_current.spm"
        root = Path("Tree_elm") / "SK_Tree_elm.spm"
        cluster_item = {"spm": cluster, "checked": False}
        root_item = {"spm": root, "checked": True}
        expanded = [cluster_item, root_item]
        app.items = {str(item["spm"]): item for item in expanded}
        attempted = []

        def fake_push(_iid, spm):
            attempted.append(spm)
            if spm == cluster:
                raise gui.BatchItemError(
                    "이번 실행의 producer RPC 실패",
                    kind="data_error",
                )

        with mock.patch.object(
            gui,
            "expand_push_targets",
            return_value=(
                expanded,
                {str(root): (str(cluster),)},
                {str(cluster)},
            ),
        ), mock.patch.object(
            app, "_push_preflight", return_value=(expanded, None)
        ), mock.patch.object(
            app,
            "_dependency_artifact_verdict",
            return_value={
                "status": "current",
                "phase": "push",
                "reason": "current Unreal import receipt",
            },
        ), mock.patch.object(
            app, "_job_push", side_effect=fake_push
        ), mock.patch.object(gui, "save_state"):
            result = app._run_batch(
                "push",
                [root_item],
                emit_done=False,
            )

        self.assertTrue(result)
        self.assertEqual(attempted, [cluster, root])
        self.assertNotEqual(
            app.state.get(str(root), {}).get("push_status_kind"),
            "dependency_blocked",
        )

    def test_full_pipeline_forwards_item_failures_to_later_phases(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        cluster_spm = Path("Tree_test") / "Cluster" / "SK_bad_spm.spm"
        bad_blend = Path("Tree_test") / "SK_bad_blend.spm"
        good = Path("Tree_test") / "SK_good.spm"
        targets = [
            {"spm": cluster_spm, "checked": True},
            {"spm": bad_blend, "checked": True},
            {"spm": good, "checked": True},
        ]
        calls = []

        def fake_batch(phase, phase_targets, emit_done=False):
            calls.append((phase, [item["spm"].name for item in phase_targets]))
            if phase == "spm":
                app._phase_failed_items = {str(cluster_spm)}
            elif phase == "blender" and any(
                item["spm"] == bad_blend for item in phase_targets
            ):
                app._phase_failed_items = {str(bad_blend)}
            else:
                app._phase_failed_items = set()
            app._phase_abort_reason = None
            return True

        app._run_batch = mock.Mock(side_effect=fake_batch)
        with mock.patch.object(
            gui,
            "expand_blender_repair_targets",
            return_value=(targets, {}, set()),
        ):
            app._run_full_pipeline(targets)

        self.assertEqual(calls[0][1], ["SK_bad_spm.spm"])
        self.assertEqual(calls[1][1], ["SK_bad_spm.spm"])
        self.assertEqual(calls[2][1], ["SK_bad_blend.spm", "SK_good.spm"])
        self.assertEqual(
            calls[3][1],
            ["SK_bad_spm.spm", "SK_bad_blend.spm", "SK_good.spm"],
        )
        final_progress = [
            payload for kind, payload in list(app.ui_queue.queue)
            if kind == "progress"
        ][-1]
        self.assertIn("completed 1", final_progress)
        self.assertIn("blocked 0", final_progress)
        self.assertIn("failed 2", final_progress)

    def test_full_pipeline_cluster_failure_does_not_suppress_mapped_consumer(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        app.cfg = {
            "spm_parallel_jobs": 1,
            "blender_parallel_jobs": 1,
            "push_parallel_jobs": 1,
        }
        app.force_rerun = True
        failed_cluster = (
            Path("Tree_oak") / "Cluster" / "SK_branch_oak_01.spm"
        )
        blocked_tree = Path("Tree_oak") / "SK_Tree_oak_01.spm"
        unrelated_tree = Path("Tree_maple") / "SK_Tree_maple_01.spm"
        cluster_item = {"spm": failed_cluster, "checked": False}
        blocked_item = {"spm": blocked_tree, "checked": True}
        unrelated_item = {"spm": unrelated_tree, "checked": True}
        expanded = [cluster_item, blocked_item, unrelated_item]
        app.items = {
            str(item["spm"]): item for item in expanded
        }
        blender_attempts = []
        push_attempts = []
        preflight_inputs = []
        blocked_contracts_at_push_expand = []

        def fail_cluster_spm(_iid, spm):
            if spm == failed_cluster:
                raise gui.BatchItemError(
                    "cluster SPM failed",
                    kind="data_error",
                )

        def record_blender(_iid, spm, _item):
            blender_attempts.append(spm)

        def record_push(_iid, spm):
            push_attempts.append(spm)

        def record_preflight(targets):
            preflight_inputs.append(
                [item["spm"] for item in targets]
            )
            return list(targets), None

        def expand_for_push(
            targets,
            _all_items,
            stage_dependency_contracts=None,
        ):
            blocked_contracts_at_push_expand.append(
                app._repair_stage_contract(blocked_tree)
            )
            self.assertIsNone(stage_dependency_contracts)
            return (
                expanded,
                {
                    str(blocked_tree): (str(failed_cluster),),
                    str(unrelated_tree): (),
                },
                {str(failed_cluster)},
            )

        with tempfile.TemporaryDirectory() as temp_dir, mock.patch.object(
            gui,
            "LOG_DIR",
            Path(temp_dir) / "logs",
        ), mock.patch.object(
            gui,
            "expand_blender_repair_targets",
            return_value=(
                expanded,
                {
                    str(blocked_tree): (str(failed_cluster),),
                    str(unrelated_tree): (),
                },
                {str(failed_cluster)},
            ),
        ), mock.patch.object(
            gui,
            "expand_push_targets",
            side_effect=expand_for_push,
        ) as expand_push, mock.patch.object(
            app,
            "_push_preflight",
            side_effect=record_preflight,
        ), mock.patch.object(
            app,
            "_job_spm",
            side_effect=fail_cluster_spm,
        ), mock.patch.object(
            app,
            "_job_blender",
            side_effect=record_blender,
        ), mock.patch.object(
            app,
            "_job_push",
            side_effect=record_push,
        ), mock.patch.object(gui, "save_state"):
            result = app._run_full_pipeline(expanded)

        self.assertFalse(result)
        self.assertEqual(
            blender_attempts,
            [failed_cluster, blocked_tree, unrelated_tree],
        )
        self.assertEqual(
            push_attempts,
            [failed_cluster, blocked_tree, unrelated_tree],
        )
        self.assertEqual(
            preflight_inputs,
            [[failed_cluster, blocked_tree, unrelated_tree]],
        )
        self.assertEqual(len(blocked_contracts_at_push_expand), 1)
        self.assertIsNone(blocked_contracts_at_push_expand[0])
        self.assertEqual(
            [item["spm"] for item in expand_push.call_args.args[0]],
            [failed_cluster, blocked_tree, unrelated_tree],
        )
        self.assertEqual(
            app._active_push_auto_added_ids,
            {str(failed_cluster)},
        )
        self.assertEqual(
            app.state[str(failed_cluster)]["spm_status_kind"],
            "data_error",
        )
        self.assertNotEqual(
            app.state.get(str(blocked_tree), {}).get("blend_status_kind"),
            "dependency_blocked",
        )
        self.assertEqual(
            app._phase_failed_items,
            {str(failed_cluster)},
        )
        final_progress = [
            payload for kind, payload in list(app.ui_queue.queue)
            if kind == "progress"
        ][-1]
        self.assertIn("completed 2", final_progress)
        self.assertIn("blocked 0", final_progress)
        self.assertIn("failed 1", final_progress)

    def test_full_pipeline_starts_consumer_without_provider_admission_check(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        provider = Path("Tree_blackgum") / "Cluster" / "SK_cluster_blackgum.spm"
        consumer = Path("Tree_blackgum") / "SK_bush_blackgum.spm"
        provider_item = {"spm": provider, "checked": False}
        consumer_item = {"spm": consumer, "checked": True}
        app.items = {
            str(provider): provider_item,
            str(consumer): consumer_item,
        }
        calls = []

        def fake_batch(phase, phase_targets, emit_done=False):
            del emit_done
            calls.append((phase, [item["spm"] for item in phase_targets]))
            app._phase_failed_items = (
                {str(provider)}
                if phase == "spm" and provider_item in phase_targets
                else set()
            )
            app._phase_abort_reason = None
            return not app._phase_failed_items

        app._run_batch = mock.Mock(side_effect=fake_batch)
        with mock.patch.object(
            gui,
            "expand_blender_repair_targets",
            return_value=(
                [provider_item, consumer_item],
                {str(consumer): (str(provider),)},
                {str(provider)},
            ),
        ), mock.patch.object(
            app,
            "_dependency_artifact_verdict",
            return_value={
                "status": "current",
                "phase": "blender",
                "reason": "current saved provider output",
            },
        ) as verdict:
            result = app._run_full_pipeline(
                [consumer_item],
                terminal_phase="blender",
            )

        downstream = [
            names for phase, names in calls
            if phase == "blender" and consumer in names
        ]
        self.assertEqual(downstream, [[consumer]])
        self.assertTrue(result)
        self.assertEqual(
            app._phase_result_summary["dependency_blocked_count"],
            0,
        )
        row = app._phase_result_summary["target_outcomes"][0]
        self.assertEqual(row["outcome"], "completed")
        self.assertNotIn("dependency_resolution", row["evidence"])
        verdict.assert_not_called()

    def test_full_pipeline_finishes_cluster_before_tree_spm_and_blender(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        owner = Path(r"D:\Trees\Tree_elm")
        cluster_spm = (
            owner / "Cluster" / "SK_branch_elm_01.spm"
        )
        tree_spm = owner / "SK_Tree_elm_01.spm"
        cluster_item = {
            "spm": cluster_spm,
            "checked": True,
            "referenced_by_spms": (tree_spm,),
        }
        tree_item = {"spm": tree_spm, "checked": True}
        app.items = {
            str(cluster_spm): cluster_item,
            str(tree_spm): tree_item,
        }
        calls = []

        def fake_batch(phase, phase_targets, emit_done=False):
            del emit_done
            calls.append(
                (
                    phase,
                    [item["spm"] for item in phase_targets],
                )
            )
            app._phase_failed_items = set()
            app._phase_abort_reason = None
            return True

        app._run_batch = mock.Mock(side_effect=fake_batch)
        with mock.patch.object(
            gui,
            "cluster_relation_output_targets",
            return_value=(tree_spm,),
        ):
            app._run_full_pipeline(
                [tree_item, cluster_item],
                terminal_phase="push",
            )

        self.assertEqual(
            calls,
            [
                ("spm", [cluster_spm]),
                ("blender", [cluster_spm]),
                ("blender", [tree_spm]),
                ("push", [cluster_spm, tree_spm]),
            ],
        )

    def test_blackgum_cluster_finishes_before_all_captured_consumers(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        fixture = self.issue16_fixture()

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "blackgum"
            cluster_spm = (
                root / "cluster" / "SK_cluster_blackgum_01.spm"
            )
            consumers = [
                root / row["target"] for row in fixture["targets"]
            ]
            cluster_item = {
                "spm": cluster_spm,
                "checked": True,
                "referenced_by_spms": tuple(consumers),
            }
            consumer_items = [
                {"spm": target, "checked": True}
                for target in consumers
            ]
            app.items = {
                str(item["spm"]): item
                for item in [cluster_item, *consumer_items]
            }
            calls = []

            def fake_batch(phase, phase_targets, emit_done=False):
                del emit_done
                calls.append((
                    phase,
                    [item["spm"] for item in phase_targets],
                ))
                app._phase_failed_items = set()
                app._phase_abort_reason = None
                return True

            app._run_batch = mock.Mock(side_effect=fake_batch)
            with mock.patch.object(
                gui,
                "cluster_relation_output_targets",
                return_value=tuple(consumers),
            ):
                app._run_full_pipeline(
                    [*consumer_items, cluster_item],
                    terminal_phase="push",
                )

        self.assertEqual(
            calls,
            [
                ("spm", [cluster_spm]),
                ("blender", [cluster_spm]),
                ("blender", consumers),
                ("push", [cluster_spm, *consumers]),
            ],
        )

    def test_rpc_timeout_stops_safely_and_marks_remaining_items_not_run(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        targets = self.targets("SK_timeout.spm", "SK_after_1.spm", "SK_after_2.spm")
        attempted = []

        def fake_push(_iid, spm):
            attempted.append(spm.name)
            raise gui.BatchItemError(
                "Unreal RPC 시간 초과 — 큐 응답 정지", kind="rpc_timeout"
            )

        with mock.patch.object(app, "_push_preflight", return_value=(targets, None)), mock.patch.object(
            app, "_job_push", side_effect=fake_push
        ), mock.patch.object(gui, "save_state"):
            result = app._run_batch("push", targets, emit_done=False)

        self.assertFalse(result)
        self.assertEqual(attempted, ["SK_timeout.spm"])
        self.assertEqual(
            app.state["SK_timeout.spm"]["push_status_kind"], "rpc_timeout"
        )
        self.assertEqual(
            app.state["SK_after_1.spm"]["push_status_kind"], "not_run_unreal"
        )
        self.assertIn("미실행", app.state["SK_after_2.spm"]["push_status"])

    def test_preflight_unreal_off_is_a_failure_not_false_completion(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        targets = self.targets("SK_first.spm", "SK_second.spm")

        with mock.patch.object(app, "_unreal_running", return_value=False), mock.patch.object(
            gui, "save_state"
        ):
            ready, reason = app._push_preflight(targets)

        self.assertEqual(ready, [])
        self.assertIn("Unreal Editor", reason)
        self.assertEqual(
            app.state["SK_first.spm"]["push_status_kind"], "unreal_unavailable"
        )

    def test_all_preflight_excluded_is_an_explicit_push_failure(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        targets = self.targets("SK_missing_texture.spm", "SK_stale_blend.spm")

        with mock.patch.object(
            app, "_push_preflight", return_value=([], None)
        ):
            result = app._run_batch("push", targets, emit_done=True)

        self.assertFalse(result)
        self.assertEqual(app._phase_failed_items, {
            "SK_missing_texture.spm", "SK_stale_blend.spm",
        })
        self.assertIn("2개 제외", app._phase_abort_reason)
        progress = [
            payload for kind, payload in list(app.ui_queue.queue)
            if kind == "progress"
        ][-1]
        self.assertIn("Unreal Push 중단", progress)
        self.assertIn("2개 제외", progress)

    def test_headless_preflight_allows_unreal_to_be_off(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        app.active_push_transport = "headless"
        targets = self.targets("SK_first.spm", "SK_second.spm")

        with mock.patch.object(app, "_unreal_running", return_value=False), mock.patch.object(
            app, "_handoff_ready", return_value=(True, "")
        ), mock.patch.object(gui, "save_state"):
            ready, reason = app._push_preflight(targets)

        self.assertEqual(ready, targets)
        self.assertIsNone(reason)

    def test_headless_preflight_blocks_when_editor_is_open(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        app.active_push_transport = "headless"
        targets = self.targets("SK_first.spm")

        with mock.patch.object(app, "_unreal_running", return_value=True), mock.patch.object(
            gui, "save_state"
        ):
            ready, reason = app._push_preflight(targets)

        self.assertEqual(ready, [])
        self.assertIn("Unreal Editor", reason)
        self.assertEqual(
            app.state["SK_first.spm"]["push_status_kind"], "unreal_unavailable"
        )

    def test_push_batch_routes_headless_without_using_rpc_job(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        app.active_push_transport = "headless"
        targets = self.targets("SK_first.spm")

        with mock.patch.object(
            app, "_push_preflight", return_value=(targets, None)
        ), mock.patch.object(
            app, "_run_headless_push_batch", return_value=True
        ) as headless, mock.patch.object(app, "_job_push") as rpc_job:
            result = app._run_batch("push", targets, emit_done=False)

        self.assertTrue(result)
        headless.assert_called_once_with(targets, emit_done=False)
        rpc_job.assert_not_called()

    def test_watchdog_records_crash_in_checkpoint_before_restart(self):
        gui = load_gui_module()
        with tempfile.TemporaryDirectory() as temp_dir:
            checkpoint = Path(temp_dir) / "checkpoint.json"
            checkpoint.write_text(
                json.dumps(
                    {
                        "current_item": "item-a",
                        "items": {
                            "item-a": {
                                "status": "importing",
                                "crash_count": 0,
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            result = gui.App._record_headless_process_exit(checkpoint, 2)

        self.assertIsNone(result["current_item"])
        self.assertEqual(result["items"]["item-a"]["status"], "unreal_crash")
        self.assertEqual(result["items"]["item-a"]["crash_count"], 1)

    def test_headless_checkpoint_replaces_stale_blender_progress(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        item_by_id = {
            "item-a": {"report_path": "a.json", "fingerprint": "a-v1"},
            "item-b": {"report_path": "b.json", "fingerprint": "b-v1"},
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            checkpoint = Path(temp_dir) / "checkpoint.json"
            checkpoint.write_text(
                json.dumps(
                    {
                        "current_item": "item-b",
                        "items": {
                            "item-a": {"status": "imported_ok"},
                            "item-b": {"status": "importing"},
                        },
                    }
                ),
                encoding="utf-8",
            )
            with mock.patch.object(gui, "save_state"):
                app._sync_headless_checkpoint(checkpoint, item_by_id)
                app._sync_headless_checkpoint(checkpoint, item_by_id)

        progress = [
            payload
            for kind, payload in list(app.ui_queue.queue)
            if kind == "progress"
        ]
        self.assertEqual(
            progress,
            ["Unreal Push 1/2 · headless 처리 중 1개"],
        )

    def test_headless_export_failures_are_not_reported_as_cache_success(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        app.force_rerun = False
        app._phase_failed_items = set()
        app.cfg = {"blender_parallel_jobs": 1}
        targets = self.targets("SK_broken.spm")

        with mock.patch.object(
            app, "_export_manifest_item", side_effect=RuntimeError("export failed")
        ), mock.patch.object(gui, "save_state"):
            result = app._run_headless_push_batch(targets, emit_done=True)

        self.assertFalse(result)
        progress = [
            payload for kind, payload in list(app.ui_queue.queue)
            if kind == "progress"
        ][-1]
        self.assertIn("실패/준비 제외 1개", progress)
        self.assertNotIn("완료 (cache)", progress)

    def test_rpc_push_skips_when_source_export_and_import_receipts_match(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        app.force_rerun = False
        spm = Path("SK_current.spm")
        iid = str(spm)
        app.state[iid] = {"push_import_fingerprint": "item-v1"}
        app._source_push_fingerprint = mock.Mock(return_value="source-v1")
        app._cached_manifest_item = mock.Mock(
            return_value={"queue_id": iid, "fingerprint": "item-v1"}
        )
        app._run_limited = mock.Mock(
            side_effect=AssertionError("current RPC Push must not run Blender")
        )

        with mock.patch.object(gui, "save_state"):
            app._job_push(iid, spm)

        app._run_limited.assert_not_called()
        self.assertEqual(app.state[iid]["push_status_kind"], "imported_ok")
        self.assertIn("건너뜀", app.log.call_args.args[0])

    def test_ready_assembly_manifest_never_rpc_skips_live_verification(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        app.force_rerun = False
        app.cfg = {"send2ue_dir": "send2ue"}
        spm = Path("SK_tree_assembly_01.spm")
        iid = str(spm)
        app.state[iid] = {"push_import_fingerprint": "item-v1"}
        app._source_push_fingerprint = mock.Mock(return_value="source-v1")
        app._cached_manifest_item = mock.Mock(
            return_value={
                "queue_id": iid,
                "fingerprint": "item-v1",
                "cluster_assembly": {
                    "ingest_plan": {
                        "status": "ready",
                        "asset_contract": {
                            "full_skeletal_mesh": "/Game/Tree/SK_Tree",
                            "base_skeletal_mesh": "/Game/Tree/Base",
                            "parts": {},
                            "assembly": "/Game/Tree/Assembly",
                        },
                    }
                },
            }
        )
        app._push_material_contract = mock.Mock(
            side_effect=RuntimeError("continued to live verification")
        )

        with self.assertRaisesRegex(
            RuntimeError, "continued to live verification"
        ):
            app._job_push(iid, spm)

        self.assertFalse(
            any(
                "건너뜀" in str(call.args[0])
                for call in app.log.call_args_list
            )
        )

    def test_headless_cache_hit_tree_with_ready_assembly_is_verified(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        app.force_rerun = False
        app._phase_failed_items = set()
        app._active_push_dependency_map = {}
        tree = Path("Tree") / "SK_tree_assembly_01.spm"
        iid = str(tree)
        app.state[iid] = {"push_import_fingerprint": "tree-v1"}
        app.cfg = {
            "blender_parallel_jobs": 1,
            "unreal_editor_cmd": "UnrealEditor-Cmd.exe",
            "unreal_project": "MyProject2.uproject",
            "headless_item_crash_retries": 2,
            "headless_batch_max_restarts": 0,
            "headless_job_timeout": 100,
        }
        exported = {
            "queue_id": iid,
            "fingerprint": "tree-v1",
            "report_path": "tree-report.json",
            "assets": [],
            "cluster_assembly": {
                "ingest_plan": {
                    "status": "ready",
                    "asset_contract": {
                        "full_skeletal_mesh": "/Game/Tree/SK_Tree",
                        "base_skeletal_mesh": "/Game/Tree/Base",
                        "parts": {},
                        "assembly": "/Game/Tree/Assembly",
                    },
                }
            },
        }
        captured = {}

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)

            def fake_commandlet(_cmd, log_name, _timeout, **kwargs):
                manifest_path = Path(
                    kwargs["env"]["SK_BATCH_MANIFEST_PATH"]
                )
                checkpoint_path = Path(
                    kwargs["env"]["SK_BATCH_CHECKPOINT_PATH"]
                )
                report_path = Path(kwargs["env"]["SK_BATCH_REPORT_PATH"])
                payload = json.loads(
                    manifest_path.read_text(encoding="utf-8")
                )
                captured["item"] = payload["items"][0]
                states = {
                    iid: {
                        "status": "imported_ok",
                        "fingerprint": "tree-v1",
                    }
                }
                checkpoint_path.write_text(
                    json.dumps(
                        {
                            "complete": True,
                            "current_item": None,
                            "items": states,
                        }
                    ),
                    encoding="utf-8",
                )
                report_path.write_text(
                    json.dumps({"status": "complete", "items": states}),
                    encoding="utf-8",
                )
                return 0, temp_root / log_name

            with mock.patch.object(
                gui, "LOG_DIR", temp_root
            ), mock.patch.object(
                app, "_export_manifest_item", return_value=exported
            ), mock.patch.object(
                app, "_run_limited", side_effect=fake_commandlet
            ), mock.patch.object(gui, "save_state"):
                result = app._run_headless_push_batch(
                    [{"spm": tree, "checked": True}],
                    emit_done=False,
                )

        self.assertTrue(result)
        self.assertTrue(captured["item"]["verify_existing_assets"])

    def test_rpc_cli_uses_target_project_remote_execution_adapter(self):
        gui = load_gui_module()
        with tempfile.TemporaryDirectory() as temp_dir:
            project_dir = Path(temp_dir) / "MyProject"
            config_dir = project_dir / "Config"
            config_dir.mkdir(parents=True)
            project_path = project_dir / "MyProject.uproject"
            project_path.write_text("{}", encoding="utf-8")
            (config_dir / "DefaultEngine.ini").write_text(
                "\n".join(
                    [
                        "[/Script/PythonScriptPlugin.PythonScriptPluginSettings]",
                        "bRemoteExecution=True",
                        "RemoteExecutionMulticastGroupEndpoint=239.1.2.3:6766",
                        "RemoteExecutionMulticastBindAddress=192.168.50.9",
                        "RemoteExecutionMulticastTtl=1",
                    ]
                ),
                encoding="utf-8",
            )

            args = gui.send2ue_rpc_cli_args(project_path)

        self.assertEqual(
            args,
            [
                "--rpc-multicast-bind-address",
                "192.168.50.9",
                "--rpc-multicast-group-endpoint",
                "239.1.2.3:6766",
                "--rpc-multicast-ttl",
                "1",
            ],
        )

    def test_rpc_cli_does_not_invent_adapter_without_project_contract(self):
        gui = load_gui_module()
        with tempfile.TemporaryDirectory() as temp_dir:
            project_path = Path(temp_dir) / "Missing.uproject"
            args = gui.send2ue_rpc_cli_args(project_path)

        self.assertEqual(args, [])

    def test_rpc_cli_respects_disabled_unreal_remote_execution(self):
        gui = load_gui_module()
        with tempfile.TemporaryDirectory() as temp_dir:
            project_dir = Path(temp_dir) / "DisabledProject"
            config_dir = project_dir / "Config"
            config_dir.mkdir(parents=True)
            project_path = project_dir / "DisabledProject.uproject"
            project_path.write_text("{}", encoding="utf-8")
            (config_dir / "DefaultEngine.ini").write_text(
                "\n".join(
                    [
                        "[/Script/PythonScriptPlugin.PythonScriptPluginSettings]",
                        "bRemoteExecution=False",
                        "RemoteExecutionMulticastBindAddress=192.168.50.9",
                    ]
                ),
                encoding="utf-8",
            )

            args = gui.send2ue_rpc_cli_args(project_path)

        self.assertEqual(args, [])

    def test_rpc_preferences_are_applied_before_unreal_discovery(self):
        source = (
            SK_BATCH_DIR / "jobs" / "send2ue_push_job.py"
        ).read_text(encoding="utf-8")

        configure = source.index(
            'report["rpc_configuration"] = configure_send2ue_rpc_preferences(args)'
        )
        discover = source.index("if not utilities.is_unreal_connected():")
        self.assertLess(configure, discover)
        self.assertIn(
            'preferences.command_endpoint = f"{bind_address}:{int(port_text)}"',
            source,
        )

    def test_saved_preflight_skip_is_rechecked_against_current_handoff(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        spm = Path("branch_elm_01.spm")
        iid = str(spm)
        app.state[iid] = {
            "push_status": "건너뜀: Blender 갱신 필요",
            "push_status_kind": "preflight_skip",
        }
        app._handoff_ready = mock.Mock(return_value=(True, "준비됨 ✓"))

        text = app._current_push_status_text(iid, spm)

        self.assertEqual(text, "준비됨 ✓")
        app._handoff_ready.assert_called_once_with(spm)

    def test_saved_push_completion_is_not_current_after_input_change(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        spm = Path("SK_changed.spm")
        iid = str(spm)
        app.state[iid] = {
            "push_status": "완료 (headless)",
            "push_status_kind": "imported_ok",
            "push_source_fingerprint_cache": {
                "version": gui.PUSH_SOURCE_FINGERPRINT_CACHE_VERSION,
                "fingerprint": "source-v1",
                "snapshot": {"blend": {"mtime_ns": 1}},
            },
            "push_export_cache": {
                "source_fingerprint": "source-v1",
                "manifest": "manifest.json",
                "fingerprint": "item-v1",
            },
            "push_import_fingerprint": "item-v1",
        }
        app._push_dependency_paths = mock.Mock(return_value=[])

        with mock.patch.object(
            gui,
            "push_source_snapshot",
            return_value={"blend": {"mtime_ns": 2}},
        ):
            text = app._current_push_status_text(iid, spm)

        self.assertIn("Push 재확인 필요", text)
        self.assertNotIn("완료", text)

    def test_push_fingerprint_tracks_direct_export_and_ingest_modules(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        app.cfg = {"send2ue_dir": Path("C:/send2ue")}

        paths = {
            Path(path).resolve()
            for path in app._push_dependency_paths()
        }

        self.assertIn(
            (SK_BATCH_DIR / "nanite_assembly_materials.py").resolve(),
            paths,
        )
        self.assertIn(
            (
                SK_BATCH_DIR.parent
                / "cluster_spm_pair_contract.py"
            ).resolve(),
            paths,
        )
        self.assertIn(
            (
                SK_BATCH_DIR.parent
                / "speedtree_pipeline_contract.py"
            ).resolve(),
            paths,
        )

    def test_push_fingerprint_tracks_item_repair_assembly_report(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        app.cfg = {"send2ue_dir": Path("C:/send2ue")}
        spm = Path("C:/Tree/SK_tree_test_01.spm")

        paths = {
            Path(path)
            for path in app._push_source_dependency_paths(spm)
        }

        self.assertIn(gui.repair_pipeline_report_path(spm), paths)

    def test_headless_exports_all_items_then_uses_one_commandlet_session(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        app.force_rerun = False
        app._phase_abort_reason = ""
        app.cfg = {
            "unreal_editor_cmd": "UnrealEditor-Cmd.exe",
            "unreal_project": "MyProject2.uproject",
            "headless_item_crash_retries": 2,
            "headless_batch_max_restarts": 0,
            "headless_job_timeout": 100,
        }
        targets = self.targets("SK_first.spm", "SK_second.spm")
        exported = [
            {
                "queue_id": "SK_first.spm",
                "fingerprint": "first-v1",
                "report_path": "first-report.json",
            },
            {
                "queue_id": "SK_second.spm",
                "fingerprint": "second-v1",
                "report_path": "second-report.json",
            },
        ]

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)

            def fake_commandlet(_cmd, log_name, _timeout, **kwargs):
                manifest_path = Path(kwargs["env"]["SK_BATCH_MANIFEST_PATH"])
                checkpoint_path = Path(kwargs["env"]["SK_BATCH_CHECKPOINT_PATH"])
                report_path = Path(kwargs["env"]["SK_BATCH_REPORT_PATH"])
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                states = {
                    item["queue_id"]: {
                        "status": "imported_ok",
                        "fingerprint": item["fingerprint"],
                    }
                    for item in manifest["items"]
                }
                checkpoint_path.write_text(
                    json.dumps(
                        {
                            "complete": True,
                            "current_item": None,
                            "items": states,
                        }
                    ),
                    encoding="utf-8",
                )
                report_path.write_text(
                    json.dumps({"status": "complete", "items": states}),
                    encoding="utf-8",
                )
                kwargs["progress_callback"](1.0, "done")
                return 0, temp_root / log_name

            with mock.patch.object(gui, "LOG_DIR", temp_root), mock.patch.object(
                app, "_export_manifest_item", side_effect=exported
            ) as export_item, mock.patch.object(
                app, "_run_limited", side_effect=fake_commandlet
            ) as commandlet, mock.patch.object(gui, "save_state"):
                result = app._run_headless_push_batch(targets, emit_done=False)

        self.assertTrue(result)
        self.assertEqual(export_item.call_count, 2)
        commandlet.assert_called_once()
        self.assertEqual(
            app.state["SK_first.spm"]["push_status_kind"], "imported_ok"
        )
        self.assertEqual(
            app.state["SK_second.spm"]["push_status_kind"], "imported_ok"
        )

    def test_headless_verifies_cached_cluster_before_dependent_tree(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        app.force_rerun = False
        app._phase_failed_items = set()
        cluster = Path("Tree_elm") / "Cluster" / "SK_cluster_any.spm"
        root = Path("Tree_elm") / "SK_Tree_elm.spm"
        app._active_push_dependency_map = {
            str(root): (str(cluster),)
        }
        app.cfg = {
            "blender_parallel_jobs": 1,
            "unreal_editor_cmd": "UnrealEditor-Cmd.exe",
            "unreal_project": "MyProject2.uproject",
            "headless_item_crash_retries": 2,
            "headless_batch_max_restarts": 0,
            "headless_job_timeout": 100,
        }
        targets = [
            {"spm": cluster, "checked": False},
            {"spm": root, "checked": True},
        ]
        exported = [
            {
                "queue_id": str(cluster),
                "fingerprint": "cluster-v1",
                "report_path": "cluster-report.json",
                "assets": [
                    {
                        "asset_data": {
                            "asset_path": "/Game/Tree/Cluster/SK_cluster_any_01"
                        }
                    }
                ],
            },
            {
                "queue_id": str(root),
                "fingerprint": "root-v1",
                "report_path": "root-report.json",
                "assets": [],
            },
        ]
        app.state[str(cluster)] = {
            "push_import_fingerprint": "cluster-v1"
        }
        captured = {}

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)

            def fake_commandlet(_cmd, log_name, _timeout, **kwargs):
                manifest_path = Path(
                    kwargs["env"]["SK_BATCH_MANIFEST_PATH"]
                )
                checkpoint_path = Path(
                    kwargs["env"]["SK_BATCH_CHECKPOINT_PATH"]
                )
                report_path = Path(kwargs["env"]["SK_BATCH_REPORT_PATH"])
                manifest = json.loads(
                    manifest_path.read_text(encoding="utf-8")
                )
                captured["items"] = manifest["items"]
                states = {
                    item["queue_id"]: {
                        "status": "imported_ok",
                        "fingerprint": item["fingerprint"],
                    }
                    for item in manifest["items"]
                }
                checkpoint_path.write_text(
                    json.dumps(
                        {
                            "complete": True,
                            "current_item": None,
                            "items": states,
                        }
                    ),
                    encoding="utf-8",
                )
                report_path.write_text(
                    json.dumps({"status": "complete", "items": states}),
                    encoding="utf-8",
                )
                kwargs["progress_callback"](1.0, "done")
                return 0, temp_root / log_name

            with mock.patch.object(
                gui, "LOG_DIR", temp_root
            ), mock.patch.object(
                app, "_export_manifest_item", side_effect=exported
            ), mock.patch.object(
                app, "_run_limited", side_effect=fake_commandlet
            ), mock.patch.object(gui, "save_state"):
                result = app._run_headless_push_batch(
                    targets, emit_done=False
                )

        self.assertTrue(result)
        self.assertEqual(
            [item["queue_id"] for item in captured["items"]],
            [str(cluster), str(root)],
        )
        self.assertTrue(captured["items"][0]["verify_existing_assets"])
        self.assertEqual(
            captured["items"][1]["depends_on_queue_ids"],
            [str(cluster)],
        )

    def test_headless_queues_tree_when_cluster_export_failed(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        app.force_rerun = False
        app._phase_failed_items = set()
        cluster = Path("Tree_elm") / "Cluster" / "SK_leaf_elm_side_01.spm"
        root = Path("Tree_elm") / "SK_Tree_elm_01.spm"
        app._active_push_dependency_map = {
            str(root): (str(cluster),)
        }
        app.cfg = {"blender_parallel_jobs": 1}
        targets = [
            {"spm": cluster, "checked": False},
            {"spm": root, "checked": True},
        ]
        root_export = {
            "queue_id": str(root),
            "fingerprint": "root-v1",
            "report_path": "root-report.json",
            "assets": [],
        }
        captured = {}

        def export_item(iid, _spm, _stamp):
            if iid == str(cluster):
                raise gui.BatchItemError(
                    "Blender에 저장되지 않은 변경이 있음",
                    kind="manual_required",
                )
            return root_export

        def capture_import(pending, *_args, **_kwargs):
            captured["pending"] = copy.deepcopy(pending)
            return True

        with mock.patch.object(
            app, "_export_manifest_item", side_effect=export_item
        ), mock.patch.object(
            app,
            "_run_headless_import_items",
            side_effect=capture_import,
        ) as importer, mock.patch.object(gui, "save_state"):
            result = app._run_headless_push_batch(
                targets,
                emit_done=False,
            )

        self.assertTrue(result)
        importer.assert_called_once()
        self.assertEqual(
            app.state[str(cluster)]["push_status_kind"],
            "manual_required",
        )
        self.assertEqual(
            [item["queue_id"] for item in captured["pending"]],
            [str(root)],
        )
        self.assertEqual(
            captured["pending"][0]["depends_on_queue_ids"],
            [],
        )
        self.assertNotEqual(
            app.state.get(str(root), {}).get("push_status_kind"),
            "dependency_blocked",
        )

    def test_headless_continues_consumer_without_provider_admission_check(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        app.force_rerun = False
        app._phase_failed_items = set()
        provider = Path("Tree") / "Cluster" / "SK_provider.spm"
        consumer = Path("Tree") / "SK_consumer.spm"
        app._active_push_dependency_map = {
            str(consumer): (str(provider),)
        }
        app.cfg = {"blender_parallel_jobs": 1}
        targets = [
            {"spm": provider, "checked": False},
            {"spm": consumer, "checked": True},
        ]
        consumer_export = {
            "queue_id": str(consumer),
            "fingerprint": "consumer-v1",
            "report_path": "consumer-report.json",
            "assets": [],
        }

        def export_item(iid, _spm, _stamp):
            if iid == str(provider):
                raise gui.BatchItemError(
                    "이번 실행의 provider export 오판",
                    kind="data_error",
                )
            return consumer_export

        with mock.patch.object(
            app, "_export_manifest_item", side_effect=export_item
        ), mock.patch.object(
            app,
            "_dependency_artifact_verdict",
            return_value={
                "status": "current",
                "phase": "push",
                "reason": "기존 Unreal import 영수증 current",
            },
        ) as verdict, mock.patch.object(
            app, "_run_headless_import_items", return_value=True
        ) as run_import, mock.patch.object(gui, "save_state"):
            app._run_headless_push_batch(targets, emit_done=False)

        run_import.assert_called_once()
        pending = run_import.call_args.args[0]
        self.assertEqual([row["queue_id"] for row in pending], [str(consumer)])
        self.assertEqual(pending[0]["depends_on_queue_ids"], [])
        verdict.assert_not_called()

    def test_headless_queues_consumer_without_provider_wait_gate(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        app.force_rerun = False
        app._phase_failed_items = set()
        provider = Path("Tree") / "Cluster" / "SK_provider_wait.spm"
        consumer = Path("Tree") / "SK_consumer_wait.spm"
        app._active_push_dependency_map = {
            str(consumer): (str(provider),)
        }
        app.cfg = {"blender_parallel_jobs": 1}
        targets = [
            {"spm": provider, "checked": False},
            {"spm": consumer, "checked": True},
        ]

        def export_item(iid, _spm, _stamp):
            if iid == str(provider):
                raise gui.BatchItemError("provider unavailable", kind="data_error")
            return {
                "queue_id": str(consumer),
                "fingerprint": "consumer-v1",
                "report_path": "consumer-report.json",
                "assets": [],
            }

        with mock.patch.object(
            app, "_export_manifest_item", side_effect=export_item
        ), mock.patch.object(
            app,
            "_dependency_artifact_verdict",
            return_value={
                "status": "waiting",
                "phase": "push",
                "reason": "export current, Unreal import 영수증 대기",
            },
        ) as verdict, mock.patch.object(
            app,
            "_run_headless_import_items",
            return_value=True,
        ) as importer, mock.patch.object(gui, "save_state"):
            result = app._run_headless_push_batch(targets, emit_done=False)

        self.assertTrue(result)
        importer.assert_called_once()
        pending = importer.call_args.args[0]
        self.assertEqual([row["queue_id"] for row in pending], [str(consumer)])
        self.assertEqual(pending[0]["depends_on_queue_ids"], [])
        self.assertNotEqual(
            app.state.get(str(consumer), {}).get("push_status_kind"),
            "dependency_waiting",
        )
        verdict.assert_not_called()

    def test_cached_manifest_item_finds_recovered_item_after_provider(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        app.force_rerun = False
        with tempfile.TemporaryDirectory() as temp_dir:
            manifest_path = Path(temp_dir) / "recovery.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "items": [
                            {"queue_id": "provider", "fingerprint": "provider-v2"},
                            {"queue_id": "tree", "fingerprint": "tree-v2"},
                        ],
                    }
                ),
                encoding="utf-8",
            )
            app.state["tree"] = {
                "push_export_cache": {
                    "source_fingerprint": "source-v2",
                    "manifest": str(manifest_path),
                    "fingerprint": "tree-v2",
                }
            }
            with mock.patch.object(
                gui, "manifest_item_files_match", return_value=True
            ):
                item = app._cached_manifest_item("tree", "source-v2")

        self.assertEqual(item["queue_id"], "tree")

    def configure_failed_retry_start(
        self,
        app,
        candidate_ids,
        *,
        checked_ids=None,
    ):
        checked_ids = set(
            candidate_ids if checked_ids is None else checked_ids
        )
        app.items = {
            iid: {"spm": Path(iid), "checked": iid in checked_ids}
            for iid in candidate_ids
        }
        app._close_cell_editor = mock.Mock()
        app._collect_cfg = mock.Mock(return_value={"push_transport": "rpc"})
        app._snapshot_batch_request = mock.Mock(
            side_effect=lambda target_ids: (
                dict(app.items),
                [
                    {
                        "spm": Path(iid),
                        "checked": app.items[iid]["checked"],
                    }
                    for iid in target_ids
                ],
            )
        )
        jobs = []
        app._enqueue_batch_job = mock.Mock(
            side_effect=lambda job: jobs.append(job)
        )
        app._failed_retry_repair_state = mock.Mock(return_value={
            "current": True,
            "push_ready": True,
            "kind": "ready",
            "reason": "current content-addressed Repair output",
        })
        app._validate_failed_retry_unreal_item_current = mock.Mock(
            return_value={}
        )
        return jobs

    def test_failed_results_retry_ui_declares_complete_inventory_scope(self):
        source = (SK_BATCH_DIR / "sk_batch_gui.pyw").read_text(
            encoding="utf-8"
        )

        self.assertIn('text="↻ 전체 실패 이력 재시도"', source)
        self.assertIn("체크 상태와 무관하게 현재 목록 전체", source)
        self.assertIn('text="↻ 체크 항목 실패 재시도"', source)
        self.assertIn("체크하지 않은 항목은 계획·검증·실행 대상에 포함하지 않습니다", source)
        self.assertNotIn("체크된 최근 실패", source)

    def test_checked_failed_retry_snapshots_and_enqueues_only_checked_rows(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        checked = "checked_failed.spm"
        unchecked = "unchecked_failed.spm"
        jobs = self.configure_failed_retry_start(
            app,
            [checked, unchecked],
            checked_ids=[checked],
        )
        app.state = {
            iid: {
                "blend_status_kind": "data_error",
                "blend_status": "실패: Blender Repair failed",
                "blend_status_error": {
                    "kind": "data_error",
                    "message": "Blender Repair failed",
                },
            }
            for iid in (checked, unchecked)
        }
        for iid in (checked, unchecked):
            app.state[iid]["blend_status_error"].update(
                app._bind_failure_record(
                    iid,
                    "data_error",
                    "Blender Repair failed",
                )
            )
        app._failed_retry_repair_state.return_value = {
            "current": False,
            "push_ready": False,
            "kind": "inspection_incomplete",
            "reason": "failed Blender attempt has no current output",
        }

        with mock.patch.object(gui, "save_config"):
            app.start_checked_failed_results_retry()

        self.assertEqual(len(jobs), 1)
        self.assertEqual(
            [str(item["spm"]) for item in jobs[0]["targets"]],
            [checked],
        )
        self.assertEqual(
            jobs[0]["retry_metadata"]["selected_queue_ids"],
            [checked],
        )
        app._snapshot_batch_request.assert_called_once_with([checked])

    def test_checked_retry_force_rebuilds_current_success_without_checkbox(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        checked = "checked_current_success.spm"
        jobs = self.configure_failed_retry_start(app, [checked])
        app.force_var = mock.Mock()
        app.force_var.get.return_value = False
        app.state[checked] = {
            "blend_status": "current",
            "push_status": "completed previously",
            "push_status_kind": "imported_ok",
        }

        with mock.patch.object(gui, "save_config") as save_config:
            app.start_checked_failed_results_retry()

        self.assertEqual(len(jobs), 1)
        self.assertTrue(jobs[0]["force_rerun"])
        self.assertEqual(
            jobs[0]["retry_metadata"]["eligibility"]["items"][0][
                "reason_code"
            ],
            "current_blender_success_forced_rebuild",
        )
        app.force_var.get.assert_called_once_with()
        self.assertNotIn("_retry_force_rerun", save_config.call_args.args[0])
        self.assertNotIn("_retry_force_rerun", jobs[0]["cfg"])

    def test_revision_mismatch_prechecks_before_retry_tracker_and_dedupes(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        checked = "revision_changed.spm"
        self.configure_failed_retry_start(app, [checked])
        app._async_retry_planning_enabled = True
        app._new_retry_progress = mock.Mock()
        app._record_phase_status = mock.Mock()
        app.force_var = mock.Mock()
        original_state = copy.deepcopy(app.state)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sources = [root / f"changed_{index}.py" for index in range(6)]
            for index, path in enumerate(sources):
                path.write_text(f"value = {index}\n", encoding="utf-8")
            expected = gui.production_source_manifest(root)
            for index, path in enumerate(sources):
                path.write_text(
                    f"value = {index + 100}\n",
                    encoding="utf-8",
                )
            actual = gui.production_source_manifest(root)

            with mock.patch.object(
                gui,
                "_PROCESS_PRODUCTION_SOURCE_MANIFEST",
                expected,
            ), mock.patch.object(
                gui,
                "production_source_manifest",
                return_value=actual,
            ) as manifest, mock.patch.object(
                gui.messagebox,
                "showwarning",
            ) as warning:
                first = app.start_checked_failed_results_retry()
                second = app.start_checked_failed_results_retry()

        self.assertEqual(
            first["route"],
            "code_revision_restart_required",
        )
        self.assertEqual(second, first)
        self.assertEqual(first["expected_revision"], expected.content_hash)
        self.assertEqual(first["actual_revision"], actual.content_hash)
        self.assertEqual(len(first["changed_paths"]), 6)
        manifest.assert_called_once_with(gui.REPO_DIR)
        warning.assert_called_once()
        warning_text = warning.call_args.args[1]
        for path in sources:
            self.assertIn(path.name, warning_text)
        self.assertIn(expected.content_hash, warning_text)
        self.assertIn(actual.content_hash, warning_text)
        app._new_retry_progress.assert_not_called()
        app._collect_cfg.assert_not_called()
        app._snapshot_batch_request.assert_not_called()
        app._enqueue_batch_job.assert_not_called()
        app._record_phase_status.assert_not_called()
        app.force_var.get.assert_not_called()
        self.assertEqual(app.state, original_state)

    def test_retry_click_has_one_revision_precheck_before_receipt_creation(self):
        module = ast.parse(
            (SK_BATCH_DIR / "sk_batch_gui.pyw").read_text(encoding="utf-8")
        )
        app_class = next(
            node
            for node in module.body
            if isinstance(node, ast.ClassDef) and node.name == "App"
        )
        method = next(
            node
            for node in app_class.body
            if isinstance(node, ast.FunctionDef)
            and node.name == "_start_failed_results_retry"
        )

        def calls(name):
            return [
                node
                for node in ast.walk(method)
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == name
            ]

        prechecks = calls("_production_source_revision_precheck")
        self.assertEqual(len(prechecks), 1)
        self.assertLess(prechecks[0].lineno, calls("_collect_cfg")[0].lineno)
        self.assertLess(
            prechecks[0].lineno,
            calls("_new_retry_progress")[0].lineno,
        )

    def test_revision_restart_escapes_item_worker_without_asset_failure(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        target = Path("worker_revision_changed.spm")
        details = {
            "route": "code_revision_restart_required",
            "expected_revision": "a" * 64,
            "actual_revision": "b" * 64,
            "changed_paths": [{
                "path": "sk_batch/worker.py",
                "status": "modified",
                "expected": {"sha256": "a" * 64},
                "actual": {"sha256": "b" * 64},
            }],
        }
        restart = gui.CodeRevisionRestartRequired(
            gui.CompileGateError("intentional mismatch", details=details),
            context="worker revision test",
        )
        app.cfg = {"check_parallel_jobs": 1}
        app._job_check = mock.Mock(side_effect=restart)
        app._record_phase_status = mock.Mock()
        app._retry_transition = mock.Mock()
        original_state = copy.deepcopy(app.state)

        with tempfile.TemporaryDirectory() as temporary, mock.patch.object(
            gui,
            "LOG_DIR",
            Path(temporary) / "logs",
        ), self.assertRaises(gui.CodeRevisionRestartRequired):
            app._run_batch(
                "check",
                [{"spm": target}],
                emit_done=False,
            )

        app._record_phase_status.assert_not_called()
        self.assertEqual(app._phase_failed_items, set())
        self.assertEqual(app.state, original_state)

    def test_revision_restart_is_structured_job_route_not_asset_failure(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        target = Path("top_level_revision_changed.spm")
        details = {
            "route": "code_revision_restart_required",
            "expected_revision": "c" * 64,
            "actual_revision": "d" * 64,
            "changed_paths": [{
                "path": "sk_batch/job.py",
                "status": "modified",
                "expected": {"sha256": "c" * 64},
                "actual": {"sha256": "d" * 64},
            }],
        }
        restart = gui.CodeRevisionRestartRequired(
            gui.CompileGateError("intentional mismatch", details=details),
            context="top-level revision test",
        )
        app._freeze_batch_production_source_manifest = mock.Mock(
            side_effect=restart
        )
        app._run_full_pipeline = mock.Mock()
        app._record_phase_status = mock.Mock()
        app._set_push_state = mock.Mock()
        app._set_failed_retry_automatic_status = mock.Mock()
        job = {
            "id": 178,
            "label": "revision fence test",
            "mode": "pipeline",
            "terminal_phase": "push",
            "selected_scope": True,
            "targets": [{"spm": target}],
            "retry_metadata": {},
        }

        app._run_queued_batch_job(job)

        done = next(
            payload
            for event, payload in iter(app.ui_queue.get_nowait, None)
            if event == "batch_job_done"
        )
        self.assertEqual(
            done["status"],
            "code_revision_restart_required",
        )
        self.assertEqual(done["failed_count"], 0)
        self.assertEqual(done["target_outcomes"], [])
        self.assertEqual(
            done["code_revision_restart_required"]["expected_revision"],
            "c" * 64,
        )
        app._run_full_pipeline.assert_not_called()
        app._record_phase_status.assert_not_called()
        app._set_push_state.assert_not_called()
        app._set_failed_retry_automatic_status.assert_not_called()
        self.assertNotIn(str(target), app.state)

    def test_revision_restart_survives_queue_receipt_finalization_failure(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        target = Path("revision_changed_before_queue_finish.spm")
        details = {
            "route": "code_revision_restart_required",
            "expected_revision": "e" * 64,
            "actual_revision": "f" * 64,
            "changed_paths": [],
        }
        restart = gui.CodeRevisionRestartRequired(
            gui.CompileGateError("intentional mismatch", details=details),
            context="queue finalization revision test",
        )
        app._freeze_batch_production_source_manifest = mock.Mock(
            side_effect=restart
        )
        lease = mock.Mock()
        lease.finished = False
        lease.heartbeat_error = RuntimeError("lease owner unavailable")
        lease.finish.side_effect = RuntimeError("queue receipt write failed")
        app.shared_queue_runtime = mock.Mock()
        app.shared_queue_runtime.wait_for_turn.return_value = lease
        app._run_full_pipeline = mock.Mock()
        app._record_phase_status = mock.Mock()
        job = {
            "id": 179,
            "label": "revision fence queue finalization test",
            "mode": "pipeline",
            "terminal_phase": "push",
            "selected_scope": True,
            "targets": [{"spm": target}],
            "shared_queue_job_id": "shared-revision-179",
            "retry_metadata": {},
        }

        app._run_queued_batch_job(job)

        done = next(
            payload
            for event, payload in iter(app.ui_queue.get_nowait, None)
            if event == "batch_job_done"
        )
        self.assertEqual(
            done["status"],
            "code_revision_restart_required",
        )
        self.assertEqual(done["failed_count"], 0)
        self.assertEqual(done["target_outcomes"], [])
        self.assertEqual(done["shared_failures"], [])
        self.assertEqual(
            done["queue_finalization_error"],
            "queue receipt write failed",
        )
        self.assertTrue(done["job_diagnostics"][0]["owner_lost"])
        self.assertFalse(done["job_diagnostics"][0]["asset_failure"])
        self.assertEqual(
            lease.finish.call_args.kwargs["terminal_status"],
            "cancelled",
        )
        app._run_full_pipeline.assert_not_called()
        app._record_phase_status.assert_not_called()
        self.assertNotIn(str(target), app.state)

    def test_revision_restart_cancels_pending_shared_queue_tickets(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        tracker = mock.Mock()
        pending_job = {
            "id": 180,
            "label": "pending rebuild",
            "shared_queue_job_id": "shared-revision-180",
            "retry_metadata": {"partition": "blender_rebuild"},
            "_retry_progress_tracker": tracker,
        }
        app.active_batch_job = {
            "id": 179,
            "label": "active revision fence",
            "retry_metadata": {},
        }
        app.pending_batch_jobs = deque([pending_job])
        app.batch_job_failures = []
        app.worker = mock.Mock()
        app.shared_queue_runtime = mock.Mock()
        app._present_code_revision_restart_required = mock.Mock()
        app._reset_cluster_receipt_refresh_memo = mock.Mock()
        app._set_batch_queue_controls = mock.Mock()
        app.progress_var = mock.Mock()
        app._start_next_batch_job = mock.Mock()

        app._finish_batch_job({
            "id": 179,
            "status": gui.CODE_REVISION_RESTART_ROUTE,
            "error": "intentional source revision change",
            gui.CODE_REVISION_RESTART_ROUTE: {
                "expected_revision": "a" * 64,
                "actual_revision": "b" * 64,
                "changed_paths": [],
            },
        })

        app.shared_queue_runtime.cancel.assert_called_once_with(
            "shared-revision-180",
            reason=gui.CODE_REVISION_RESTART_ROUTE,
        )
        tracker.mark_partition_terminal.assert_called_once_with(
            "blender_rebuild",
            gui.RETRY_STAGE_CANCELLED,
            "code revision changed before shared queue claim; restart required",
        )
        self.assertEqual(list(app.pending_batch_jobs), [])
        app._start_next_batch_job.assert_not_called()

    def test_checked_failed_retry_with_no_checks_does_not_plan(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        jobs = self.configure_failed_retry_start(
            app,
            ["unchecked.spm"],
            checked_ids=[],
        )

        with mock.patch.object(gui, "messagebox") as messages:
            app.start_checked_failed_results_retry()

        self.assertEqual(jobs, [])
        app._collect_cfg.assert_not_called()
        app._snapshot_batch_request.assert_not_called()
        title, body = messages.showinfo.call_args.args
        self.assertEqual(title, "체크 항목 실패 재시도")
        self.assertEqual(body, "체크한 재시도 대상이 없습니다.")

    def test_slow_complete_inventory_retry_planning_keeps_tk_responsive(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        candidate_ids = ["checked.spm", "unchecked.spm"]
        self.configure_failed_retry_start(
            app,
            candidate_ids,
            checked_ids=[candidate_ids[0]],
        )
        app._async_retry_planning_enabled = True
        app._retry_planning_workers = set()
        app.active_batch_job = None
        app.pending_batch_jobs = gui.deque()
        ui_thread_ident = threading.get_ident()
        app._ui_thread_ident = ui_thread_ident
        events = []
        force_reads = []

        class ThreadCheckedForceVar:
            def get(self):
                if threading.get_ident() != ui_thread_ident:
                    raise AssertionError("worker touched the force Tk variable")
                force_reads.append(threading.get_ident())
                return False

        app.force_var = ThreadCheckedForceVar()

        class ThreadCheckedVar:
            value = ""

            def set(self, value):
                if threading.get_ident() != ui_thread_ident:
                    raise AssertionError("worker touched a Tk variable")
                self.value = str(value)
                events.append(("progress", self.value))

        class ThreadCheckedRoot:
            def update_idletasks(self):
                if threading.get_ident() != ui_thread_ident:
                    raise AssertionError("worker touched the Tk root")
                events.append(("paint",))

            def after(self, _delay, _callback):
                if threading.get_ident() != ui_thread_ident:
                    raise AssertionError("worker scheduled Tk work directly")
                events.append(("after",))

        app.progress_var = ThreadCheckedVar()
        app.root = ThreadCheckedRoot()
        app._set_batch_queue_controls = mock.Mock(
            side_effect=lambda _busy: events.append(("controls",))
        )
        tracker = mock.Mock()
        tracker.run_id = "slow-complete-inventory"
        tracker.path = Path("retry-progress.json")
        app._new_retry_progress = mock.Mock(
            side_effect=lambda *_args, **_kwargs: (
                events.append(("tracker",)) or tracker
            )
        )

        planning_entered = threading.Event()
        release_planning = threading.Event()
        repair_threads = []

        def slow_repair_state(_iid):
            repair_threads.append(threading.get_ident())
            planning_entered.set()
            if not release_planning.wait(5):
                raise AssertionError("test did not release slow planning")
            return {
                "current": False,
                "push_ready": False,
                "kind": "stale_content",
                "reason": "deterministic slow complete-inventory planning",
            }

        app._failed_retry_repair_state = mock.Mock(
            side_effect=slow_repair_state
        )
        enqueued = []

        def enqueue_on_tk_thread(job):
            if threading.get_ident() != ui_thread_ident:
                raise AssertionError("worker committed a queue job")
            enqueued.append(job)
            events.append(("enqueue",))
            return len(enqueued)

        app._enqueue_batch_job = mock.Mock(side_effect=enqueue_on_tk_thread)

        with mock.patch.object(gui, "save_config"):
            run_id = app.start_failed_results_retry()
            self.assertTrue(planning_entered.wait(1))
            self.assertEqual(run_id, tracker.run_id)
            self.assertEqual(
                events[:4],
                [
                    ("controls",),
                    ("progress", "retry stage=planning · 대상 2개"),
                    ("paint",),
                    ("tracker",),
                ],
            )
            self.assertIn("대상 2개", app.progress_var.value)
            self.assertFalse(enqueued)

            worker = next(iter(app._retry_planning_workers))
            release_planning.set()
            worker.join(5)
            self.assertFalse(worker.is_alive())
            app._drain_ui_queue()

        self.assertEqual(
            app._failed_retry_repair_state.call_args_list,
            [mock.call(iid) for iid in candidate_ids],
        )
        self.assertTrue(repair_threads)
        self.assertTrue(
            all(ident != ui_thread_ident for ident in repair_threads)
        )
        self.assertEqual(len(enqueued), 1)
        self.assertEqual(force_reads, [ui_thread_ident])
        self.assertEqual(
            [str(item["spm"]) for item in enqueued[0]["targets"]],
            candidate_ids,
        )
        self.assertIn(("enqueue",), events)

        off_thread_errors = []

        def attempt_off_thread_commit():
            try:
                app._commit_failed_retry_plan({})
            except Exception as exc:
                off_thread_errors.append(exc)

        off_thread = threading.Thread(target=attempt_off_thread_commit)
        off_thread.start()
        off_thread.join(5)
        self.assertFalse(off_thread.is_alive())
        self.assertEqual(len(off_thread_errors), 1)
        self.assertIsInstance(off_thread_errors[0], RuntimeError)
        self.assertIn("Tk owner thread", str(off_thread_errors[0]))

    @staticmethod
    def write_unreal_retry_parent(gui, root, queue_id, status="data_error"):
        manifest_path = root / "parent.json"
        checkpoint_path = root / "checkpoint.json"
        manifest_path.write_text(
            json.dumps({
                "schema_version": gui.PUSH_MANIFEST_SCHEMA_VERSION,
                "report_path": str(root / "parent_report.json"),
                "items": [{
                    "schema_version": gui.PUSH_MANIFEST_SCHEMA_VERSION,
                    "queue_id": queue_id,
                    "source_fingerprint": "source-v1",
                    "fingerprint": "item-v1",
                    "depends_on_queue_ids": [],
                }],
            }),
            encoding="utf-8",
        )
        checkpoint_path.write_text(
            json.dumps({
                "items": {queue_id: {"status": status}},
            }),
            encoding="utf-8",
        )
        return manifest_path, checkpoint_path

    def test_failed_results_retry_unchecked_unreal_uses_immutable_job(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        iid = "unreal_failed.spm"
        jobs = self.configure_failed_retry_start(
            app,
            [iid],
            checked_ids=[],
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            manifest, checkpoint = self.write_unreal_retry_parent(
                gui, root, iid
            )
            app.state[iid] = {
                "push_status_kind": "data_error",
                "push_paths": {
                    "manifest": str(manifest),
                    "checkpoint": str(checkpoint),
                },
                "push_source_fingerprint_cache": {
                    "fingerprint": "source-v1",
                    "snapshot": {},
                },
            }
            with mock.patch.object(gui, "save_config"):
                app.start_failed_results_retry()

        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["mode"], "unreal_recovery")
        self.assertFalse(jobs[0]["force_rerun"])
        self.assertEqual(
            jobs[0]["retry_metadata"]["execution_path"],
            "immutable_unreal_only",
        )
        app._validate_failed_retry_unreal_item_current.assert_called_once()
        eligibility = jobs[0]["retry_metadata"]["eligibility"]
        self.assertEqual(
            eligibility["items"][0]["classification"], "unreal_only"
        )

    def test_failed_results_retry_is_independent_of_checkbox_state(self):
        gui = load_gui_module()
        first = "first_repair_failed.spm"
        second = "second_repair_failed.spm"
        candidates = [first, second]

        def run_with_checked(checked_ids):
            app = self.make_app(gui)
            jobs = self.configure_failed_retry_start(
                app,
                candidates,
                checked_ids=checked_ids,
            )
            app.state = {iid: {} for iid in candidates}
            app._failed_retry_repair_state.return_value = {
                "current": False,
                "push_ready": False,
                "kind": "stale_content",
                "reason": "current source fingerprint differs from Repair report",
            }

            with mock.patch.object(gui, "save_config"):
                app.start_failed_results_retry()

            self.assertEqual(len(jobs), 1)
            job = jobs[0]
            return (
                [str(item["spm"]) for item in job["targets"]],
                job["retry_metadata"]["eligibility"],
            )

        first_checked = run_with_checked([first])
        second_checked = run_with_checked([second])

        self.assertEqual(first_checked, second_checked)
        self.assertEqual(first_checked[0], candidates)
        self.assertEqual(len(first_checked[0]), len(set(first_checked[0])))
        self.assertEqual(
            [row["queue_id"] for row in first_checked[1]["items"]],
            candidates,
        )

    def test_failed_results_retry_blender_only_rebuilds_then_pushes(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        iid = "export_failed.spm"
        jobs = self.configure_failed_retry_start(app, [iid])
        app.state[iid] = {
            "blend_status_kind": "data_error",
            "blend_status": "실패: Blender Repair failed",
            "blend_status_error": {
                "kind": "data_error",
                "message": "Blender Repair failed",
            },
            # A previous Push receipt must not override the current visible
            # Blender failure.
            "push_status_kind": "imported_ok",
            "push_paths": {
                "manifest": "previous.json",
                "checkpoint": "previous_checkpoint.json",
            },
        }
        app._failed_retry_repair_state.return_value = {
            "current": False,
            "push_ready": False,
            "kind": "inspection_incomplete",
            "reason": "failed Blender attempt has no current output",
        }

        with mock.patch.object(gui, "save_config"):
            app.start_failed_results_retry()

        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["mode"], "pipeline")
        self.assertEqual(jobs[0]["terminal_phase"], "push")
        self.assertTrue(jobs[0]["force_rerun"])
        self.assertEqual(jobs[0]["push_transport"], "headless")
        self.assertEqual(
            jobs[0]["retry_metadata"]["execution_path"],
            "blender_send2ue_then_unreal",
        )

    def test_failed_results_retry_send2ue_export_uses_blender_pipeline(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        iid = "send2ue_export_failed.spm"
        jobs = self.configure_failed_retry_start(app, [iid])
        app.state[iid] = {
            "push_status_kind": "data_error",
            "push_status": "실패: Send2UE export failed",
            "push_paths": {"report": "export_failed.json"},
        }

        with mock.patch.object(gui, "save_config"):
            app.start_failed_results_retry()

        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["mode"], "pipeline")
        self.assertTrue(jobs[0]["force_rerun"])

    def test_failed_results_retry_stale_blender_forces_full_pipeline(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        iid = "stale_blender.spm"
        jobs = self.configure_failed_retry_start(app, [iid])
        # Saved success labels are deliberately contradictory. The current
        # report/source fingerprint decision is the retry authority.
        app.state[iid] = {
            "blend_status": "최신 ✓",
            "push_status_kind": "imported_ok",
            "push_status": "완료 (현재 최신)",
            "push_paths": {
                "manifest": "successful_parent.json",
                "checkpoint": "successful_parent_checkpoint.json",
            },
        }
        app._failed_retry_repair_state.return_value = {
            "current": False,
            "push_ready": False,
            "kind": "stale_content",
            "reason": "current source fingerprint differs from Repair report",
        }

        with mock.patch.object(gui, "save_config"):
            app.start_failed_results_retry()

        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["mode"], "pipeline")
        self.assertTrue(jobs[0]["force_rerun"])
        eligibility = jobs[0]["retry_metadata"]["eligibility"]
        self.assertEqual(
            eligibility["items"][0]["reason_code"],
            "blender_output_not_current",
        )

    def test_failed_results_retry_current_blender_success_is_excluded(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        iid = "current_blender.spm"
        jobs = self.configure_failed_retry_start(app, [iid])
        app.state[iid] = {
            # Text must not turn a content-current result into a retry.
            "blend_status": "Blender 갱신 필요 — stale saved label",
            "push_status_kind": "imported_ok",
        }

        with mock.patch.object(gui, "messagebox") as messages:
            app.start_failed_results_retry()

        self.assertEqual(jobs, [])
        messages.showinfo.assert_called_once()
        self.assertIn(".blend가 SPM보다 최신 · 제외", app.log.call_args.args[0])
        title, body = messages.showinfo.call_args.args
        self.assertEqual(title, "전체 실패 이력 재시도")
        self.assertIn("현재 목록 전체", body)
        self.assertNotIn("선택", body)

    def test_current_success_exclusion_completes_retry_receipt(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        iid = "current_blender_receipt.spm"
        self.configure_failed_retry_start(app, [iid])
        app.state[iid] = {"push_status_kind": "imported_ok"}

        with tempfile.TemporaryDirectory() as temp_dir:
            tracker = gui.RetryProgressReceipt.create(
                [iid],
                receipt_dir=Path(temp_dir),
            )
            built = app._build_failed_retry_plan(
                [iid],
                {"push_transport": "rpc"},
                tracker=tracker,
                inventory_snapshot=dict(app.items),
            )
            receipt = tracker.snapshot(evaluate=False)

        self.assertEqual(built["jobs"], [])
        self.assertEqual(receipt["terminal_outcome"], "complete")
        self.assertEqual(receipt["targets"][0]["stage"], "complete")
        self.assertEqual(
            receipt["targets"][0]["terminal_reason"],
            "current_blender_success",
        )

    def test_failed_results_retry_force_reruns_current_blender_success(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        app.force_var = mock.Mock()
        app.force_var.get.return_value = True
        iid = "current_blender.spm"
        jobs = self.configure_failed_retry_start(app, [iid])
        app.state[iid] = {"push_status_kind": "imported_ok"}

        with mock.patch.object(gui, "save_config"):
            app.start_failed_results_retry()

        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["mode"], "pipeline")
        self.assertTrue(jobs[0]["force_rerun"])
        eligibility = jobs[0]["retry_metadata"]["eligibility"]
        self.assertEqual(
            eligibility["items"][0]["reason_code"],
            "current_blender_success_forced_rebuild",
        )

    def test_failed_results_retry_imported_ok_parent_is_not_a_failure(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        iid = "current_imported_ok.spm"
        jobs = self.configure_failed_retry_start(app, [iid])
        app.state[iid] = {
            "push_status_kind": "imported_ok",
            # A completed run legitimately retains these receipt paths. Their
            # presence must not turn success into an invalid failure parent.
            "push_paths": {
                "manifest": "successful_parent.json",
                "checkpoint": "successful_parent_checkpoint.json",
            },
        }

        with mock.patch.object(gui, "messagebox") as messages:
            app.start_failed_results_retry()

        self.assertEqual(jobs, [])
        messages.showinfo.assert_called_once()
        self.assertIn(".blend가 SPM보다 최신 · 제외", app.log.call_args.args[0])
        app._validate_failed_retry_unreal_item_current.assert_not_called()

    def test_failed_results_retry_empty_inventory_does_not_request_selection(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        jobs = self.configure_failed_retry_start(app, [], checked_ids=[])

        with mock.patch.object(gui, "messagebox") as messages:
            app.start_failed_results_retry()

        self.assertEqual(jobs, [])
        app._collect_cfg.assert_not_called()
        messages.showinfo.assert_called_once()
        title, body = messages.showinfo.call_args.args
        self.assertEqual(title, "전체 실패 이력 재시도")
        self.assertIn("현재 목록 전체", body)
        self.assertNotIn("선택", body)

    def test_failed_results_retry_mixed_inventory_routes_without_duplicates(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        unreal_iid = "unreal_failed.spm"
        export_iid = "export_failed.spm"
        jobs = self.configure_failed_retry_start(
            app, [export_iid, unreal_iid]
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            manifest, checkpoint = self.write_unreal_retry_parent(
                gui, root, unreal_iid
            )
            app.state = {
                export_iid: {
                    "push_status_kind": "data_error",
                    "push_paths": {"report": "export_failed.json"},
                },
                unreal_iid: {
                    "push_status_kind": "data_error",
                    "push_paths": {
                        "manifest": str(manifest),
                        "checkpoint": str(checkpoint),
                    },
                    "push_source_fingerprint_cache": {
                        "fingerprint": "source-v1",
                        "snapshot": {},
                    },
                },
            }
            with mock.patch.object(gui, "save_config"):
                app.start_failed_results_retry()

        self.assertEqual(
            [job["mode"] for job in jobs],
            ["unreal_recovery", "pipeline"],
        )
        routed = [
            str(item["spm"])
            for job in jobs
            for item in job["targets"]
        ]
        self.assertCountEqual(routed, [unreal_iid, export_iid])
        self.assertEqual(len(routed), len(set(routed)))

    def test_failed_results_retry_dependency_overlap_promotes_to_rebuild(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        provider = "provider.spm"
        tree = "tree.spm"
        jobs = self.configure_failed_retry_start(app, [provider, tree])

        def repair_state(iid):
            if iid == provider:
                return {
                    "current": False,
                    "push_ready": False,
                    "kind": "stale_content",
                    "reason": "provider source fingerprint changed",
                }
            return {
                "current": True,
                "push_ready": True,
                "kind": "ready",
                "reason": "current",
            }

        app._failed_retry_repair_state.side_effect = repair_state
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            manifest = root / "parent.json"
            checkpoint = root / "checkpoint.json"
            manifest.write_text(
                json.dumps({
                    "schema_version": gui.PUSH_MANIFEST_SCHEMA_VERSION,
                    "report_path": str(root / "parent_report.json"),
                    "items": [
                        {
                            "schema_version": gui.PUSH_MANIFEST_SCHEMA_VERSION,
                            "queue_id": provider,
                            "source_fingerprint": "provider-source-v1",
                            "fingerprint": "provider-item-v1",
                            "depends_on_queue_ids": [],
                        },
                        {
                            "schema_version": gui.PUSH_MANIFEST_SCHEMA_VERSION,
                            "queue_id": tree,
                            "source_fingerprint": "tree-source-v1",
                            "fingerprint": "tree-item-v1",
                            "depends_on_queue_ids": [provider],
                        },
                    ],
                }),
                encoding="utf-8",
            )
            checkpoint.write_text(
                json.dumps({"items": {tree: {"status": "data_error"}}}),
                encoding="utf-8",
            )
            app.state = {
                provider: {"push_status_kind": "imported_ok"},
                tree: {
                    "push_status_kind": "data_error",
                    "push_paths": {
                        "manifest": str(manifest),
                        "checkpoint": str(checkpoint),
                    },
                },
            }

            with mock.patch.object(gui, "save_config"):
                app.start_failed_results_retry()

        self.assertEqual([job["mode"] for job in jobs], ["pipeline"])
        routed = [str(item["spm"]) for item in jobs[0]["targets"]]
        self.assertEqual(routed, [provider, tree])
        self.assertEqual(len(routed), len(set(routed)))
        app._validate_failed_retry_unreal_item_current.assert_not_called()
        eligibility = jobs[0]["retry_metadata"]["eligibility"]["items"]
        self.assertEqual(
            {row["queue_id"]: row["reason_code"] for row in eligibility},
            {
                provider: "blender_output_not_current",
                tree: "unreal_dependency_requires_rebuild",
            },
        )

    def test_failed_results_retry_incomplete_unreal_evidence_rebuilds(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        iid = "incomplete_parent.spm"
        jobs = self.configure_failed_retry_start(app, [iid])
        app.state[iid] = {
            "push_status_kind": "data_error",
            "push_paths": {"manifest": "parent.json"},
        }

        with mock.patch.object(gui, "save_config"):
            app.start_failed_results_retry()

        self.assertEqual([job["mode"] for job in jobs], ["pipeline"])
        self.assertTrue(jobs[0]["force_rerun"])
        self.assertEqual(
            jobs[0]["retry_metadata"]["eligibility"]["items"][0][
                "reason_code"
            ],
            "unreal_parent_evidence_incomplete_full_rebuild",
        )

    def test_failed_results_retry_invalid_parent_rebuilds_exact_dependency(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        provider = "provider.spm"
        tree = "tree.spm"
        jobs = self.configure_failed_retry_start(app, [provider, tree])

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            manifest = root / "parent.json"
            checkpoint = root / "checkpoint.json"
            manifest.write_text(
                json.dumps({
                    "schema_version": gui.PUSH_MANIFEST_SCHEMA_VERSION,
                    "report_path": str(root / "parent_report.json"),
                    "items": [
                        {
                            "schema_version": gui.PUSH_MANIFEST_SCHEMA_VERSION,
                            "queue_id": provider,
                            "source_fingerprint": "provider-source-v1",
                            "fingerprint": "provider-item-v1",
                            "depends_on_queue_ids": [],
                        },
                        {
                            "schema_version": gui.PUSH_MANIFEST_SCHEMA_VERSION,
                            "queue_id": tree,
                            "source_fingerprint": "tree-source-v1",
                            "fingerprint": "tree-item-v1",
                            "depends_on_queue_ids": [provider],
                        },
                    ],
                }),
                encoding="utf-8",
            )
            checkpoint.write_text(
                json.dumps({"items": {tree: {"status": "data_error"}}}),
                encoding="utf-8",
            )
            app.state = {
                provider: {"push_status_kind": "imported_ok"},
                tree: {
                    "push_status_kind": "data_error",
                    "push_paths": {
                        "manifest": str(manifest),
                        "checkpoint": str(checkpoint),
                    },
                },
            }

            with mock.patch.object(gui, "save_config"):
                app.start_failed_results_retry()

        self.assertEqual([job["mode"] for job in jobs], ["pipeline"])
        self.assertEqual(
            [str(item["spm"]) for item in jobs[0]["targets"]],
            [provider, tree],
        )
        eligibility = {
            row["queue_id"]: row
            for row in jobs[0]["retry_metadata"]["eligibility"]["items"]
        }
        self.assertEqual(
            eligibility[provider]["reason_code"],
            "unreal_dependency_full_rebuild_fallback",
        )
        self.assertTrue(eligibility[provider]["scheduled_as_dependency"])
        self.assertEqual(
            eligibility[tree]["reason_code"],
            "unreal_parent_evidence_invalid_full_rebuild",
        )

    def test_partial_retry_job_keeps_other_partition_in_fifo(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        app.active_batch_job = {
            "id": 1,
            "label": "Unreal-only retry",
        }
        app.pending_batch_jobs = deque([{
            "id": 2,
            "label": "Blender export retry",
        }])
        app.batch_job_failures = []
        app.worker = mock.Mock()
        app._start_next_batch_job = mock.Mock()
        app._ensure_batch_queue_state = mock.Mock()

        app._finish_batch_job({
            "id": 1,
            "status": "partial",
            "error": "failed=1",
            "completed_count": 0,
            "blocked_count": 0,
            "failed_count": 1,
            "target_outcomes": [],
            "shared_failures": [],
        })

        self.assertEqual(len(app.batch_job_failures), 1)
        self.assertEqual(len(app.pending_batch_jobs), 1)
        app._start_next_batch_job.assert_called_once_with()

    def test_failed_unreal_recovery_never_calls_blender_export(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        app.cfg = {}
        app._active_production_source_manifest = mock.Mock(
            content_hash="production-v2"
        )
        provider = {
            "schema_version": 1,
            "queue_id": "provider.spm",
            "source_fingerprint": "provider-source-v1",
            "fingerprint": "provider-item-v1",
            "blend": "provider.blend",
            "depends_on_queue_ids": [],
        }
        tree = {
            "schema_version": 1,
            "queue_id": "tree.spm",
            "source_fingerprint": "tree-source-v1",
            "fingerprint": "tree-item-v1",
            "blend": "tree.blend",
            "depends_on_queue_ids": ["provider.spm"],
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            parent_path = Path(temp_dir) / "parent.json"
            parent_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "report_path": str(Path(temp_dir) / "parent_report.json"),
                        "items": [provider, tree],
                    }
                ),
                encoding="utf-8",
            )
            request = {
                "parent_manifest": str(parent_path),
                "parent_report": str(Path(temp_dir) / "parent_report.json"),
                "selected_queue_ids": ["tree.spm"],
                "source_records": {
                    "provider.spm": {
                        "fingerprint": "provider-source-v1",
                        "snapshot": {},
                    },
                    "tree.spm": {
                        "fingerprint": "tree-source-v1",
                        "snapshot": {},
                    },
                },
            }

            def recovered(parent_item, **kwargs):
                result = dict(parent_item)
                result.update(
                    {
                        "source_fingerprint": "current-source",
                        "fingerprint": parent_item["queue_id"] + "-v2",
                        "verify_existing_assets": not kwargs["selected"],
                        "recovery": {
                            "parent_manifest": str(parent_path),
                            "old_code_revision": "old",
                            "new_code_revision": "new",
                        },
                    }
                )
                return result

            app._push_unreal_code_paths = mock.Mock(return_value=[])
            app._push_rebindable_unreal_code_paths = mock.Mock(return_value=[])
            app._source_push_fingerprint = mock.Mock(return_value="current-source")
            app._run_headless_import_items = mock.Mock(return_value=True)
            app._export_manifest_item = mock.Mock(
                side_effect=AssertionError("recovery must not run Blender export")
            )
            app.state = {
                "provider.spm": {
                    "push_source_fingerprint_cache": {
                        "fingerprint": "current-source",
                        "snapshot": {},
                    }
                },
                "tree.spm": {
                    "push_source_fingerprint_cache": {
                        "fingerprint": "current-source",
                        "snapshot": {},
                    }
                },
            }
            with mock.patch.object(
                gui, "recover_manifest_item", side_effect=recovered
            ), mock.patch.object(gui, "save_state"):
                result = app._run_failed_unreal_recovery(
                    [{"spm": Path("tree.spm"), "checked": True}],
                    [request],
                    emit_done=False,
                )

        self.assertTrue(result)
        app._export_manifest_item.assert_not_called()
        pending = app._run_headless_import_items.call_args.args[0]
        self.assertEqual(
            [item["queue_id"] for item in pending],
            ["provider.spm", "tree.spm"],
        )
        self.assertTrue(pending[0]["verify_existing_assets"])
        self.assertFalse(pending[1]["verify_existing_assets"])

    def test_retry_liveness_panel_renders_required_operator_fields(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        app.retry_target_var = mock.Mock()
        app.retry_liveness_var = mock.Mock()
        app.retry_outcome_var = mock.Mock()
        app.retry_diagnostic_var = mock.Mock()
        app._render_retry_progress({
            "current_target_id": "C:/sanitized/SK_tree.spm",
            "targets": [
                {
                    "target_id": "C:/sanitized/already_done.spm",
                    "target_name": "already_done.spm",
                    "stage": "complete",
                    "terminal_at": 9.0,
                },
                {
                    "target_id": "C:/sanitized/SK_tree.spm",
                    "target_name": "SK_tree.spm",
                    "partition": "blender_export",
                    "partition_ordinal": 2,
                    "partition_total": 3,
                    "stage": "send2ue",
                    "terminal_at": None,
                    "elapsed_seconds": 125,
                    "last_progress_age_seconds": 8,
                    "last_output_age_seconds": 32,
                    "last_heartbeat_age_seconds": 1,
                    "latest_diagnostic": "bounded sanitized diagnostic",
                },
            ],
        })
        target_text = app.retry_target_var.set.call_args.args[0]
        live_text = app.retry_liveness_var.set.call_args.args[0]
        outcome_text = app.retry_outcome_var.set.call_args.args[0]
        diagnostic_text = app.retry_diagnostic_var.set.call_args.args[0]
        self.assertIn("current target: SK_tree.spm", target_text)
        self.assertIn("1/2 finished", target_text)
        self.assertIn("partition=blender_export 2/3", target_text)
        self.assertIn("current target stage=send2ue", live_text)
        self.assertIn("elapsed 2m 05s", live_text)
        self.assertIn("progress age 8s", live_text)
        self.assertIn("output age 32s", live_text)
        self.assertIn("heartbeat age 1s", live_text)
        self.assertIn("retry scope: historical failed/stale selection", outcome_text)
        self.assertIn("current state: running", outcome_text)
        self.assertIn("success 1", outcome_text)
        self.assertIn("failed 0", outcome_text)
        self.assertIn("remaining 1", outcome_text)
        self.assertIn("terminal outcome: pending", outcome_text)
        self.assertEqual(
            diagnostic_text, "latest: bounded sanitized diagnostic"
        )

    def test_retry_liveness_panel_keeps_individual_failure_nonterminal(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        app.retry_target_var = mock.Mock()
        app.retry_liveness_var = mock.Mock()
        app.retry_outcome_var = mock.Mock()
        app.retry_diagnostic_var = mock.Mock()
        app._render_retry_progress({
            # The per-target/root stage may say failed while another target
            # runs; only terminal_at/run_state is a batch outcome.
            "stage": "failed",
            "terminal_at": None,
            "current_target_id": "C:/sanitized/active.spm",
            "targets": [
                {
                    "target_id": "C:/sanitized/complete.spm",
                    "target_name": "complete.spm",
                    "stage": "complete",
                    "terminal_at": 1.0,
                },
                {
                    "target_id": "C:/sanitized/failed.spm",
                    "target_name": "failed.spm",
                    "stage": "failed",
                    "terminal_at": 2.0,
                },
                {
                    "target_id": "C:/sanitized/active.spm",
                    "target_name": "active.spm",
                    "stage": "blender",
                    "terminal_at": None,
                    "elapsed_seconds": 3,
                    "last_progress_age_seconds": 1,
                    "last_output_age_seconds": None,
                    "last_heartbeat_age_seconds": 1,
                },
            ],
        })
        outcome_text = app.retry_outcome_var.set.call_args.args[0]
        self.assertIn("current state: running", outcome_text)
        self.assertIn("success 1", outcome_text)
        self.assertIn("failed 1", outcome_text)
        self.assertIn("remaining 1", outcome_text)
        self.assertIn("terminal outcome: pending", outcome_text)
        self.assertIn("current run continues after individual failures", outcome_text)

    def test_retry_liveness_panel_does_not_call_elapsed_only_running(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        app.retry_target_var = mock.Mock()
        app.retry_liveness_var = mock.Mock()
        app.retry_outcome_var = mock.Mock()
        app.retry_diagnostic_var = mock.Mock()
        app._render_retry_progress({
            "evidence_state": "stalled",
            "current_target_id": "C:/sanitized/planning.spm",
            "targets": [{
                "target_id": "C:/sanitized/planning.spm",
                "target_name": "planning.spm",
                "stage": "planning",
                "terminal_at": None,
                "wall_elapsed_seconds": 865,
                "last_progress_age_seconds": 865,
                "last_output_age_seconds": None,
                "last_heartbeat_age_seconds": 865,
            }],
        })

        outcome = app.retry_outcome_var.set.call_args.args[0]
        liveness = app.retry_liveness_var.set.call_args.args[0]
        self.assertIn("current state: stalled", outcome)
        self.assertNotIn("current state: running", outcome)
        self.assertIn("evidence state=stalled", liveness)
        self.assertIn("wall elapsed 14m 25s", liveness)

    def test_retry_planning_panel_renders_real_counts_not_just_wall_time(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        app.retry_target_var = mock.Mock()
        app.retry_liveness_var = mock.Mock()
        app.retry_outcome_var = mock.Mock()
        app.retry_diagnostic_var = mock.Mock()
        app.progress_var = mock.Mock()
        app.batch_progress = mock.Mock()
        app.batch_progress_var = mock.Mock()
        app._render_retry_progress({
            "run_state": "running",
            "evidence_state": "heartbeat_live",
            "current_target_id": "C:/sanitized/asset-17.spm",
            "planning": {
                "status": "active",
                "progress": {
                    "substage": "classification",
                    "completed_count": 17,
                    "total_count": 154,
                    "classified_count": 17,
                    "validated_count": 54,
                    "cache_status": "miss",
                },
            },
            "targets": [{
                "target_id": "C:/sanitized/asset-17.spm",
                "target_name": "asset-17.spm",
                "stage": "planning",
                "terminal_at": None,
                "wall_elapsed_seconds": 42,
                "last_progress_age_seconds": 0,
                "last_output_age_seconds": None,
                "last_heartbeat_age_seconds": 0,
            }],
        })

        target = app.retry_target_var.set.call_args.args[0]
        progress = app.progress_var.set.call_args.args[0]
        self.assertIn("classification · 17/154", target)
        self.assertIn("cache=miss", target)
        self.assertIn("classified 17", progress)
        self.assertIn("validated 54", progress)
        app.batch_progress.configure.assert_called_once_with(
            value=17 / 154 * 100.0
        )
        self.assertEqual(
            app.batch_progress_var.set.call_args.args[0], "17/154 (11%)"
        )

    def test_retry_liveness_panel_shows_terminal_outcome_only_after_terminal(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        app.retry_target_var = mock.Mock()
        app.retry_liveness_var = mock.Mock()
        app.retry_outcome_var = mock.Mock()
        app.retry_diagnostic_var = mock.Mock()
        app._render_retry_progress({
            "run_state": "terminal",
            "stage": "failed",
            "terminal_at": 9.0,
            "terminal_outcome": "failed",
            "terminal_reason": "all_retry_targets_failed",
            "current_target_id": "C:/sanitized/failed.spm",
            "targets": [
                {
                    "target_id": "C:/sanitized/failed.spm",
                    "target_name": "failed.spm",
                    "stage": "failed",
                    "terminal_at": 9.0,
                    "elapsed_seconds": 9,
                    "last_progress_age_seconds": 1,
                    "last_output_age_seconds": 1,
                    "last_heartbeat_age_seconds": 1,
                },
            ],
        })
        outcome_text = app.retry_outcome_var.set.call_args.args[0]
        self.assertIn("current state: terminal", outcome_text)
        self.assertIn("terminal outcome: failed", outcome_text)
        self.assertIn("all_retry_targets_failed", outcome_text)

    def test_push_receipt_completion_follows_durable_target_state(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        sequence = []

        def save_then_record(_state):
            sequence.append("state_saved")

        def record_retry(*args, **kwargs):
            sequence.append(("receipt", args[1]))
            return True

        app._retry_transition = mock.Mock(side_effect=record_retry)
        with mock.patch.object(gui, "save_state", side_effect=save_then_record):
            app._set_push_state(
                "C:/sanitized/tree.spm",
                "imported_ok",
                "Unreal imported",
            )

        self.assertEqual(
            sequence,
            [
                "state_saved",
                ("receipt", gui.RETRY_STAGE_POST_CHECK),
                ("receipt", gui.RETRY_STAGE_COMPLETE),
            ],
        )

    def test_push_state_persist_failure_cannot_seal_retry_receipt(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        app._retry_transition = mock.Mock()
        with mock.patch.object(
            gui, "save_state", side_effect=OSError("state disk unavailable")
        ):
            with self.assertRaises(OSError):
                app._set_push_state(
                    "C:/sanitized/tree.spm",
                    "imported_ok",
                    "Unreal imported",
                )
        app._retry_transition.assert_not_called()


if __name__ == "__main__":
    unittest.main()
