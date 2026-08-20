"""A row may not say 자동 복구 대상 and then terminate the target.

#160: both direct-run gates computed the registry decision, printed
`자동 복구 대상`, and raised a terminal `BatchItemError`.  Repair only ever
happened if an operator later pressed the failed-retry button.  The
normalization gate admitted repair for exactly two hard-coded codes; the
live-audit gate never consulted the planner at all.

These assertions pin the admission rule (the registry decides, not a per-gate
allowlist), the isolation rule that must survive it (#16), and the display
invariant that the label is a promise.
"""
import hashlib
import json
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
sys.path.insert(0, str(SK_BATCH_DIR.parent))


def load_gui_module():
    path = SK_BATCH_DIR / "sk_batch_gui.pyw"
    loader = SourceFileLoader("sk_batch_gui_direct_repair_test", str(path))
    spec = spec_from_loader(loader.name, loader)
    module = module_from_spec(spec)
    loader.exec_module(module)
    return module


PROVIDER = Path("blackgum") / "cluster" / "SK_cluster_blackgum_01.spm"
TARGET = Path("blackgum") / "SK_bush_blackgum_02.spm"


def contract_with(delivery_reason, handoff_errors, *, blocked=True):
    blocked_targets = []
    if blocked:
        blocked_targets.append({
            "spm": str(TARGET),
            "delivery_reason": delivery_reason,
            "errors": ["current Generator delivery is incomplete"],
        })
    return {
        "tree_source_identities": [{"spm": str(TARGET)}],
        "dependencies": [{
            "spm": str(PROVIDER),
            "normalized_delivery_mode": "connection_incomplete",
            "normalized_delivery_blocked": bool(blocked_targets),
            "normalized_variants_required": True,
            "normalized_variants": {
                "status": "current",
                "delivery_mode": "connection_incomplete",
                "delivery_errors": ["current Generator delivery is incomplete"],
                "delivery_blocked_targets": blocked_targets,
                "variants": [{"name": "Cluster"}],
            },
        }],
        "handoff": {
            "status": "blocked" if handoff_errors else "ready",
            "errors": list(handoff_errors),
        },
        "relationship_sync": {"outcome": "completed"},
    }


class AppHarness:
    def make_app(self, gui):
        app = gui.App.__new__(gui.App)
        app.stop_flag = threading.Event()
        app.ui_queue = queue.Queue()
        app.state = {}
        app.state_lock = threading.RLock()
        app.log = mock.Mock()
        return app


class DirectClusterRepairAdmissionTests(AppHarness, unittest.TestCase):
    def observe(self, gui, app, contract):
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
            return app._cluster_normalization_stage_observation(
                TARGET,
                "captured",
                PROVIDER,
                require_normalized=False,
            )

    def test_a_repairable_bark_block_now_admits_repair(self):
        """The reported case: this code could never satisfy the allowlist."""
        gui = load_gui_module()
        app = self.make_app(gui)
        contract = contract_with(
            "normalized_generator_delivery_incomplete",
            [
                {
                    "code": "NORMALIZED_GENERATOR_DELIVERY_INCOMPLETE",
                    "role": "cluster",
                    "spm": str(PROVIDER),
                },
                {
                    "code": "CANONICAL_BARK_NORMALIZATION_REQUIRED",
                    "role": "bark",
                    "spm": str(PROVIDER),
                },
            ],
        )

        with self.assertRaises(gui.TargetPlannedExclusionError) as caught:
            self.observe(gui, app, contract)

        exclusion = caught.exception
        self.assertEqual(
            exclusion.reason_token,
            "normalized_generator_delivery_incomplete",
        )
        # The planner reads the codes out of the evidence, so the bark reason
        # has to reach it -- not only the delivery token the block is named for.
        self.assertIn(
            "CANONICAL_BARK_NORMALIZATION_REQUIRED",
            exclusion.evidence["issue_codes"],
        )
        self.assertIn(
            "canonical_bark_normalization_required",
            gui.evidence_reason_codes({
                "reason_token": exclusion.reason_token,
                "evidence": exclusion.evidence,
            }),
        )

    def test_an_unsupported_block_stays_terminal(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        contract = contract_with(
            "normalized_generator_delivery_incomplete",
            [{
                "code": "CLUSTER_CANONICAL_SPM_MISSING",
                "role": "cluster",
                "spm": str(PROVIDER),
            }],
            blocked=False,
        )

        with self.assertRaises(gui.BatchItemError) as caught:
            self.observe(gui, app, contract)

        self.assertNotIsInstance(
            caught.exception,
            gui.TargetPlannedExclusionError,
        )

    def test_a_terminal_row_never_claims_a_repair_is_coming(self):
        """`자동 복구 대상` on a terminal row is the defect, not the message."""
        gui = load_gui_module()
        app = self.make_app(gui)
        contract = contract_with(
            "normalized_generator_delivery_incomplete",
            [{
                "code": "CLUSTER_ROLE_CONFLICT",
                "role": "cluster",
                "spm": str(PROVIDER),
            }],
            blocked=False,
        )

        with self.assertRaises(gui.BatchItemError) as caught:
            self.observe(gui, app, contract)

        self.assertNotIn("자동 복구 대상", str(caught.exception))
        self.assertIn("최종 차단", str(caught.exception))

    def test_a_global_issue_keeps_the_shared_provider_terminal(self):
        """Target-local isolation survives the wider admission (#16)."""
        gui = load_gui_module()
        app = self.make_app(gui)
        contract = contract_with(
            "normalized_generator_delivery_incomplete",
            [{
                # No `spm`, so the issue is provider-global, not target-local.
                "code": "NORMALIZED_GENERATOR_DELIVERY_INCOMPLETE",
                "role": "cluster",
            }],
        )

        with self.assertRaises(gui.BatchItemError) as caught:
            self.observe(gui, app, contract)

        self.assertNotIsInstance(
            caught.exception,
            gui.TargetPlannedExclusionError,
        )


class LiveAuditGateAdmissionTests(AppHarness, unittest.TestCase):
    def evaluate(self, app, contract, issues, *, target=TARGET):
        raw_audit = {
            "selected_contract": contract,
            "audit_report": Path("live_audit.json"),
            "payload": {
                "items": [{
                    "cluster_assembly": {"handoff": {"issues": issues}},
                }],
            },
        }
        return app._evaluate_cluster_receipt_live_audit(target, raw_audit)

    def normalized_contract(self, gui, root, *, rendered=True, count=1):
        owner = Path(root) / "tree"
        cluster = owner / "cluster"
        fbx_dir = owner / "fbx"
        cluster.mkdir(parents=True)
        fbx_dir.mkdir()
        target = owner / "SK_tree.spm"
        target.write_bytes(b"target-spm")
        fbx = fbx_dir / "SK_tree.fbx"
        fbx.write_bytes(b"current-full-fbx")
        dependencies = []
        issues = []
        for index in range(1, count + 1):
            provider = cluster / f"SK_cluster_{index:02d}.spm"
            blend = provider.with_suffix(".blend")
            registry = blend.with_suffix(".atlas_leaf_targets.json")
            provider.write_bytes(f"provider-{index}".encode())
            blend.write_bytes(f"blend-{index}".encode())
            registry.write_text(
                json.dumps({"target_spms": []}),
                encoding="utf-8",
            )
            registry_bytes = registry.read_bytes()
            dependencies.append({
                "spm": str(provider),
                "output_spm": str(provider),
                "current_live_pair_covered": rendered,
                "targets": [{
                    "spm": str(target),
                    "spm_material_mesh_pair": {
                        "status": "complete_pair" if rendered else "missing",
                    },
                    "fbx_material_mesh_pair": {
                        "status": "complete_pair" if rendered else "missing",
                        "decision": (
                            "normalize_part" if rendered else "blocked"
                        ),
                    },
                    "export_bundle": {
                        "fbx": {
                            "path": str(fbx),
                            **gui.sampled_file_content_snapshot(fbx),
                        },
                    },
                }],
                "target_relation": {
                    "status": "explicit_off",
                    "allowed": False,
                    "source_blend": str(blend),
                    "registry": {
                        "path": str(registry),
                        "sha256": hashlib.sha256(
                            registry_bytes
                        ).hexdigest(),
                        "size": len(registry_bytes),
                    },
                    "registered_target_spms": [],
                    "matched_target_spms": [],
                },
            })
            issues.append({
                "code": "NORMALIZED_VARIANTS_REQUIRED",
                "role": "cluster",
                "spm": str(provider),
            })
        return target, {
            "tree_source_identities": [{
                "target_spm": {"path": str(target)},
            }],
            "dependencies": dependencies,
        }, issues

    def test_live_audit_gate_admits_a_registered_target_block(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        contract = contract_with(
            "normalized_generator_delivery_incomplete",
            [],
        )

        with self.assertRaises(gui.TargetPlannedExclusionError) as caught:
            self.evaluate(app, contract, [{
                "code": "NORMALIZED_GENERATOR_DELIVERY_INCOMPLETE",
                "role": "cluster",
                "spm": str(PROVIDER),
            }])

        exclusion = caught.exception
        self.assertEqual(
            exclusion.reason_token,
            "normalized_generator_delivery_incomplete",
        )
        self.assertEqual(
            Path(exclusion.producer_spm).name,
            PROVIDER.name,
        )
        self.assertEqual(
            exclusion.evidence["gate"],
            "cluster_assembly_live_audit",
        )

    def test_live_audit_gate_stays_terminal_without_a_target_local_block(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        contract = contract_with(
            "normalized_generator_delivery_incomplete",
            [],
            blocked=False,
        )

        with self.assertRaises(gui.BatchItemError) as caught:
            self.evaluate(app, contract, [{
                "code": "NORMALIZED_GENERATOR_DELIVERY_INCOMPLETE",
                "role": "cluster",
            }])

        self.assertNotIsInstance(
            caught.exception,
            gui.TargetPlannedExclusionError,
        )
        self.assertNotIn("자동 복구 대상", str(caught.exception))


    def test_missing_variants_do_not_restore_explicit_off_relations(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        with tempfile.TemporaryDirectory() as folder:
            target, contract, issues = self.normalized_contract(
                gui,
                folder,
                rendered=True,
                count=2,
            )

            result = self.evaluate(
                app,
                contract,
                issues,
                target=target,
            )

        self.assertEqual(result["policy"], "live_audit_authoritative")
        self.assertEqual(
            [row["code"] for row in result["nonblocking_maintenance_issues"]],
            ["NORMALIZED_VARIANTS_REQUIRED"] * 2,
        )

    def test_missing_variants_without_live_pair_proof_are_still_nonblocking(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        with tempfile.TemporaryDirectory() as folder:
            target, contract, issues = self.normalized_contract(
                gui,
                folder,
                rendered=False,
            )

            result = self.evaluate(
                app,
                contract,
                issues,
                target=target,
            )

        self.assertEqual(result["policy"], "live_audit_authoritative")


class DirectRunRecoveryTests(AppHarness, unittest.TestCase):
    """The direct Cluster Assembly run had no repair frame at all."""

    def exclusion(self, gui):
        return gui.TargetPlannedExclusionError(
            "blocked",
            reason_token="normalized_generator_delivery_incomplete",
            target_spm=TARGET,
            producer_spm=PROVIDER,
            evidence={"issue_codes": [
                "NORMALIZED_GENERATOR_DELIVERY_INCOMPLETE"
            ]},
        )

    def test_a_repairable_block_runs_the_repair_and_re_resolves(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        resolved = {"policy": "live_audit_authoritative_pass_through"}
        refresh = mock.Mock(side_effect=[self.exclusion(gui), resolved])
        app._refresh_stale_cluster_receipt = refresh
        # The executor is the shared one; its re-audit is the caller's shape.
        app._attempt_registered_relation_repair = mock.Mock(
            side_effect=lambda exc, stamp, producer, **kw: kw["reaudit"]()
        )

        result = app._cluster_receipt_with_recovery(TARGET, "stamp")

        self.assertEqual(result, resolved)
        self.assertEqual(refresh.call_count, 2)
        attempt = app._attempt_registered_relation_repair.call_args
        self.assertEqual(Path(attempt.args[2]).name, PROVIDER.name)
        self.assertTrue(attempt.kwargs["require_normalized"])

    def test_a_repair_that_declines_leaves_the_exclusion_visible(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        app._refresh_stale_cluster_receipt = mock.Mock(
            side_effect=self.exclusion(gui)
        )
        app._attempt_registered_relation_repair = mock.Mock(return_value=None)

        with self.assertRaises(gui.TargetPlannedExclusionError):
            app._cluster_receipt_with_recovery(TARGET, "stamp")

    def test_cancellation_is_not_relabelled_as_damaged_target_data(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        app._refresh_stale_cluster_receipt = mock.Mock(
            side_effect=self.exclusion(gui)
        )
        app._attempt_registered_relation_repair = mock.Mock(
            side_effect=AssertionError("must not repair after a stop")
        )
        app.stop_flag.set()

        with self.assertRaises(gui.BatchItemError) as caught:
            app._cluster_receipt_with_recovery(TARGET, "stamp")

        self.assertEqual(caught.exception.kind, "cancelled")


class PlannedExclusionLabelTests(AppHarness, unittest.TestCase):
    def record(self, gui, app, evidence):
        error = gui.TargetPlannedExclusionError(
            "blocked",
            reason_token="normalized_generator_delivery_incomplete",
            target_spm=TARGET,
            producer_spm=PROVIDER,
            evidence=evidence,
        )
        app._record_phase_status = mock.Mock()
        app._record_pipeline_planned_exclusion(TARGET, error)
        return [
            call.args[2]
            for call in app._record_phase_status.call_args_list
        ]

    def test_a_row_awaiting_repair_says_planned_not_promised(self):
        gui = load_gui_module()
        app = self.make_app(gui)

        labels = self.record(gui, app, {})

        self.assertTrue(labels)
        for label in labels:
            self.assertNotIn("자동 복구 대상", label)
            self.assertIn("복구 계획됨", label)

    def test_a_row_whose_repair_failed_says_so(self):
        gui = load_gui_module()
        app = self.make_app(gui)

        labels = self.record(
            gui,
            app,
            {"repair_attempt": {"status": "failed"}},
        )

        self.assertTrue(labels)
        for label in labels:
            self.assertIn("자동 복구 실패", label)

    def test_an_unsupported_row_stays_terminal(self):
        gui = load_gui_module()
        app = self.make_app(gui)

        labels = self.record(
            gui,
            app,
            {"repair_attempt": {"status": "unsupported"}},
        )

        self.assertTrue(labels)
        for label in labels:
            self.assertIn("최종 차단", label)


if __name__ == "__main__":
    unittest.main()
