"""Cluster issue summaries must name the real cause, not just the file.

Covers two omissions found while reviewing the stale-node-table diagnostics:

* ``cluster_issue_summary`` silently dropped the ``stale_node_table_targets``
  / ``stale_node_table_remedy`` fields a partially-stale
  ``NORMALIZED_GENERATOR_DELIVERY_INCOMPLETE`` issue publishes, so an
  operator never saw that part of a block was already explained and fixable.
* ``App._recorded_failure_reason`` stopped at the first ``dependency_blocked``
  row in a multi-hop Cluster -> Repair -> Push chain, so a consumer two hops
  from the real failure only ever reported the generic wrapper text.
"""

import sys
import threading
import unittest
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path


SK_BATCH_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SK_BATCH_DIR))


def load_gui_module():
    path = SK_BATCH_DIR / "sk_batch_gui.pyw"
    loader = SourceFileLoader("sk_batch_gui_cluster_issue_test", str(path))
    spec = spec_from_loader(loader.name, loader)
    module = module_from_spec(spec)
    loader.exec_module(module)
    return module


class ClusterIssueSummaryTests(unittest.TestCase):
    def test_fully_stale_issue_still_shows_targets_and_remedy(self):
        gui = load_gui_module()
        issue = {
            "code": "NORMALIZED_GENERATOR_NODE_TABLE_STALE",
            "role": "leaf",
            "remedy": "재저장하세요",
            "blocked_targets": [{"spm": "a.spm"}, {"spm": "b.spm"}],
        }
        summary = gui.cluster_issue_summary([issue])
        self.assertIn("targets=a.spm, b.spm", summary)
        self.assertIn("재저장하세요", summary)

    def test_partially_stale_issue_surfaces_the_stale_subset_and_remedy(self):
        gui = load_gui_module()
        issue = {
            "code": "NORMALIZED_GENERATOR_DELIVERY_INCOMPLETE",
            "role": "leaf",
            "errors": [
                "generator_export_evidence_stale_node_table",
                "visible_generator_material_mismatch",
            ],
            "stale_node_table_targets": [{"spm": "a.spm"}],
            "stale_node_table_remedy": "노드 테이블을 재생성하세요",
        }
        summary = gui.cluster_issue_summary([issue])
        self.assertIn("NORMALIZED_GENERATOR_DELIVERY_INCOMPLETE", summary)
        self.assertIn("stale_node_table=a.spm", summary)
        self.assertIn("노드 테이블을 재생성하세요", summary)

    def test_issue_with_neither_field_is_unaffected(self):
        gui = load_gui_module()
        issue = {"code": "CLUSTER_ROLE_CONFLICT", "role": "leaf"}
        summary = gui.cluster_issue_summary([issue])
        self.assertEqual(summary, "CLUSTER_ROLE_CONFLICT role=leaf")

    def test_generic_delivery_issue_keeps_missing_slot_cause_visible(self):
        gui = load_gui_module()
        issue = {
            "code": "NORMALIZED_GENERATOR_DELIVERY_INCOMPLETE",
            "role": "leaf",
            "errors": ["visible_generator_slot_missing"],
        }
        summary = gui.cluster_issue_summary([issue])
        self.assertIn("errors=visible_generator_slot_missing", summary)

    def test_generic_delivery_issue_keeps_ambiguous_slot_cause_visible(self):
        gui = load_gui_module()
        issue = {
            "code": "NORMALIZED_GENERATOR_DELIVERY_INCOMPLETE",
            "role": "leaf",
            "errors": ["visible_generator_slot_ambiguous"],
        }
        summary = gui.cluster_issue_summary([issue])
        self.assertIn("errors=visible_generator_slot_ambiguous", summary)

    def test_generic_delivery_issue_keeps_audit_unavailable_cause_visible(self):
        gui = load_gui_module()
        issue = {
            "code": "NORMALIZED_GENERATOR_DELIVERY_INCOMPLETE",
            "role": "leaf",
            "errors": ["production_spm_live_audit_unavailable"],
        }
        summary = gui.cluster_issue_summary([issue])
        self.assertIn(
            "errors=production_spm_live_audit_unavailable",
            summary,
        )

    def test_audit_failure_summary_uses_stable_token_without_exception_details(
        self,
    ):
        gui = load_gui_module()
        issue = {
            "code": "NORMALIZED_GENERATOR_DELIVERY_INCOMPLETE",
            "role": "leaf",
            "errors": [
                "production_spm_live_audit_failed:C:\\private\\tree.spm"
            ],
        }
        summary = gui.cluster_issue_summary([issue])
        self.assertIn("errors=production_spm_live_audit_failed", summary)
        self.assertNotIn("C:\\private", summary)


class RecordedFailureReasonChainTests(unittest.TestCase):
    def make_app(self, gui):
        app = gui.App.__new__(gui.App)
        app.state = {}
        app.state_lock = threading.RLock()
        return app

    @staticmethod
    def real_failure_entry(message):
        return {
            "blend_status": f"실패: {message}",
            "blend_status_kind": "data_error",
            "blend_status_error": {
                "time": "2026-07-31T00:00:00",
                "kind": "data_error",
                "message": message,
            },
        }

    @staticmethod
    def dependency_blocked_entry(reason, blocked_by):
        return {
            "push_status": f"차단: {reason}",
            "push_status_kind": "dependency_blocked",
            "push_status_error": {
                "time": "2026-07-31T00:00:00",
                "kind": "dependency_blocked",
                "message": reason,
                "blocked_by": list(blocked_by),
            },
        }

    def test_single_hop_reason_is_unchanged(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        app.state["C.spm"] = self.real_failure_entry("Substance bake failed")
        self.assertEqual(
            app._recorded_failure_reason("C.spm"),
            "Substance bake failed",
        )

    def test_two_hop_chain_reports_the_real_root_cause(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        app.state["C.spm"] = self.real_failure_entry("Substance bake failed")
        app.state["B.spm"] = self.dependency_blocked_entry(
            "required Cluster stage failed: C.spm", ["C.spm"]
        )
        app.state["A.spm"] = self.dependency_blocked_entry(
            "required Cluster stage failed: B.spm", ["B.spm"]
        )
        # Before the fix this returned "" because the walk stopped at B.spm's
        # own dependency_blocked wrapper instead of following blocked_by.
        self.assertEqual(
            app._recorded_failure_reason("A.spm"),
            "Substance bake failed",
        )

    def test_sibling_failures_are_sorted_and_preserved(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        app.state["B.spm"] = self.real_failure_entry("Zulu failure")
        app.state["C.spm"] = self.real_failure_entry("Alpha failure")
        app.state["A.spm"] = self.dependency_blocked_entry(
            "required Cluster stage failed: B.spm, C.spm",
            ["B.spm", "C.spm"],
        )
        self.assertEqual(
            app._recorded_failure_reason("A.spm"),
            "Alpha failure | Zulu failure",
        )

    def test_diamond_dependency_reports_shared_root_once(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        app.state["D.spm"] = self.real_failure_entry("Shared root failure")
        app.state["B.spm"] = self.dependency_blocked_entry(
            "required Cluster stage failed: D.spm", ["D.spm"]
        )
        app.state["C.spm"] = self.dependency_blocked_entry(
            "required Cluster stage failed: D.spm", ["D.spm"]
        )
        app.state["A.spm"] = self.dependency_blocked_entry(
            "required Cluster stage failed: B.spm, C.spm",
            ["B.spm", "C.spm"],
        )
        self.assertEqual(
            app._recorded_failure_reason("A.spm"),
            "Shared root failure",
        )

    def test_cycle_with_an_escape_path_still_reports_the_root(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        app.state["Root.spm"] = self.real_failure_entry("Actual failure")
        app.state["B.spm"] = self.dependency_blocked_entry(
            "required Cluster stage failed: A.spm, Root.spm",
            ["A.spm", "Root.spm"],
        )
        app.state["A.spm"] = self.dependency_blocked_entry(
            "required Cluster stage failed: B.spm", ["B.spm"]
        )
        self.assertEqual(
            app._recorded_failure_reason("A.spm"),
            "Actual failure",
        )

    def test_combined_sibling_reason_honors_max_chars(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        app.state["B.spm"] = self.real_failure_entry("B" * 40)
        app.state["C.spm"] = self.real_failure_entry("C" * 40)
        app.state["A.spm"] = self.dependency_blocked_entry(
            "required Cluster stage failed: B.spm, C.spm",
            ["B.spm", "C.spm"],
        )
        reason = app._recorded_failure_reason("A.spm", max_chars=32)
        self.assertLessEqual(len(reason), 32)
        self.assertTrue(reason.endswith("…"))

    def test_long_dependency_chain_reports_root_without_recursion(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        depth = 1100
        for index in range(depth):
            app.state[f"{index}.spm"] = self.dependency_blocked_entry(
                f"required Cluster stage failed: {index + 1}.spm",
                [f"{index + 1}.spm"],
            )
        app.state[f"{depth}.spm"] = self.real_failure_entry("Deep root")
        self.assertEqual(
            app._recorded_failure_reason("0.spm"),
            "Deep root",
        )

    def test_malformed_blocked_by_falls_back_to_local_recorded_failure(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        for malformed in ("B.spm", 7):
            with self.subTest(blocked_by=malformed):
                app.state["A.spm"] = {
                    "blend_status_kind": "dependency_blocked",
                    "blend_status_error": {
                        "kind": "dependency_blocked",
                        "message": "required Cluster stage failed",
                        "blocked_by": malformed,
                    },
                    "spm_status_kind": "data_error",
                    "spm_status_error": {
                        "kind": "data_error",
                        "message": "Local fallback failure",
                    },
                }
                self.assertEqual(
                    app._recorded_failure_reason("A.spm"),
                    "Local fallback failure",
                )

    def test_cycle_guard_does_not_hang_or_raise(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        app.state["A.spm"] = self.dependency_blocked_entry(
            "required Cluster stage failed: B.spm", ["B.spm"]
        )
        app.state["B.spm"] = self.dependency_blocked_entry(
            "required Cluster stage failed: A.spm", ["A.spm"]
        )
        self.assertEqual(app._recorded_failure_reason("A.spm"), "")

    def test_unresolved_dependency_block_falls_back_to_empty(self):
        gui = load_gui_module()
        app = self.make_app(gui)
        app.state["A.spm"] = self.dependency_blocked_entry(
            "required Cluster stage failed: B.spm", ["B.spm"]
        )
        # B.spm has no recorded state at all yet.
        self.assertEqual(app._recorded_failure_reason("A.spm"), "")


if __name__ == "__main__":
    unittest.main()
