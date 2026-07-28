import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from cluster_export_handoff_contract import (
    atomic_write_json,
    cluster_export_contract_issues,
    finalize_cluster_pipeline_payload,
    validate_pending_source_contract,
)


class FakeObject:
    def __init__(self, name, *, children=(), properties=None):
        self.name = name
        self.type = "EMPTY"
        self.children = list(children)
        self._properties = dict(properties or {})

    def get(self, key, default=None):
        return self._properties.get(key, default)


def pending_payload():
    return {
        "paths": {"merged_name": "SK_branch_elm_01"},
        "handoff_preflight": {
            "status": "cluster_export_pending",
            "export_collection_issues": ["missing_export_collection"],
            "unreal_push_ready": False,
        },
        "cluster_source_build_contract": {
            "status": "ready",
            "mode": "raw_source_for_cluster_normalizer",
            "deferred_export_issues": ["missing_export_collection"],
            "final_export_required": True,
            "post_normalization_handoff_status": "ok",
            "source_blend_committed": True,
            "source_object": "SK_branch_elm_01",
        },
        "unreal_push_ready": False,
    }


class ClusterExportHandoffContractTests(unittest.TestCase):
    def test_missing_export_is_a_structural_issue_before_normalization(self):
        data = SimpleNamespace(
            collections={"Other": SimpleNamespace(objects=[])}
        )
        self.assertEqual(
            cluster_export_contract_issues(data, "SK_branch_elm_01"),
            ["missing_export_collection"],
        )

    def test_final_export_accepts_any_consecutive_generated_ordinal_count(self):
        pivots = [
            FakeObject(
                f"SK_branch_elm_01_{index:02d}",
                children=[object()],
                properties={
                    "speedtree_cluster_generated": True,
                    "speedtree_cluster_asset_role": "send2ue_pivot",
                },
            )
            for index in range(1, 6)
        ]
        data = SimpleNamespace(
            collections={
                "Export": SimpleNamespace(objects=pivots),
            }
        )
        self.assertEqual(
            cluster_export_contract_issues(data, "SK_branch_elm_01"),
            [],
        )

    def test_pending_source_is_never_accepted_as_unreal_ready(self):
        payload = pending_payload()
        payload["handoff_preflight"]["unreal_push_ready"] = True
        with self.assertRaises(ValueError):
            validate_pending_source_contract(payload)

    def test_only_valid_pending_contract_is_promoted_after_empty_final_issues(self):
        finalized, changed = finalize_cluster_pipeline_payload(
            pending_payload(),
            export_issues=[],
            expected_source_object="SK_branch_elm_01",
        )
        self.assertTrue(changed)
        self.assertEqual(
            finalized["handoff_preflight"]["status"],
            "ok",
        )
        self.assertTrue(finalized["unreal_push_ready"])
        self.assertEqual(
            finalized["cluster_source_build_contract"]["status"],
            "normalized",
        )
        self.assertTrue(
            finalized["cluster_source_build_contract"][
                "final_export_verified"
            ]
        )

    def test_final_export_issue_keeps_pending_report_unpromoted(self):
        payload = pending_payload()
        with self.assertRaises(ValueError):
            finalize_cluster_pipeline_payload(
                payload,
                export_issues=["cluster_missing_normalized_export_pivot"],
            )
        self.assertEqual(
            payload["handoff_preflight"]["status"],
            "cluster_export_pending",
        )

    def test_existing_final_report_remains_backward_compatible(self):
        payload = {
            "handoff_preflight": {
                "status": "ok",
                "unreal_push_ready": True,
            }
        }
        finalized, changed = finalize_cluster_pipeline_payload(
            payload,
            export_issues=[],
        )
        self.assertFalse(changed)
        self.assertEqual(finalized, payload)

    def test_atomic_report_write_replaces_complete_json(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "report.json"
            atomic_write_json(path, {"status": "ok"})
            self.assertEqual(
                path.read_text(encoding="utf-8"),
                '{\n  "status": "ok"\n}\n',
            )


if __name__ == "__main__":
    unittest.main()
