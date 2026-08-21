import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from cluster_export_handoff_contract import (
    atomic_write_json,
    capture_cluster_export_snapshot,
    cluster_export_contract_issues,
    finalize_cluster_pipeline_payload,
    reconcile_transient_cluster_export_root,
    validate_pending_source_contract,
)


class FakeObject:
    def __init__(
        self,
        name,
        *,
        object_type="EMPTY",
        children=(),
        properties=None,
    ):
        self.name = name
        self.type = object_type
        self.parent = None
        self.children = list(children)
        self._properties = dict(properties or {})
        for child in self.children:
            if isinstance(child, FakeObject):
                child.parent = self

    def get(self, key, default=None):
        return self._properties.get(key, default)


class FakeObjectLinks(list):
    def link(self, obj):
        self.append(obj)

    def unlink(self, obj):
        self.remove(obj)


class FakeCollection:
    def __init__(self, objects=()):
        self.objects = FakeObjectLinks(objects)


def ordinal_pivots(stem):
    return [
        FakeObject(
            f"{stem}_{index:02d}",
            children=[object()],
            properties={
                "speedtree_cluster_generated": True,
                "speedtree_cluster_asset_role": "send2ue_pivot",
            },
        )
        for index in range(1, 3)
    ]


def transient_assembly_hierarchy(stem, source_fbx, source_identity, *, owned=True):
    properties = (
        {
            "codex_source_fbx": source_fbx,
            "codex_source_identity": source_identity,
        }
        if owned
        else {}
    )
    mesh = FakeObject(
        f"{stem}_Merged",
        object_type="MESH",
        properties=properties,
    )
    root = FakeObject(stem, children=[mesh], properties=properties)
    armature = FakeObject(
        f"{stem}_Armature",
        object_type="ARMATURE",
        children=[root],
        properties=properties,
    )
    return armature, root, mesh


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

    def test_sanitized_two_ordinal_providers_reconcile_current_assembly_root(self):
        stems = (
            "SK_cluster_densiflora_02",
            "SK_leaf_weeping_willow_side_01",
        )
        for stem in stems:
            with self.subTest(stem=stem):
                source_fbx = rf"C:\Sanitized\Cluster\fbx\{stem}.fbx"
                source_identity = rf"C:\Sanitized\Cluster\{stem}.spm"
                export = FakeCollection(ordinal_pivots(stem))
                source = FakeCollection()
                data = SimpleNamespace(collections={"Export": export})
                before = capture_cluster_export_snapshot(data, stem)
                hierarchy = transient_assembly_hierarchy(
                    stem,
                    source_fbx,
                    source_identity,
                )
                export.objects.extend(hierarchy)

                report = reconcile_transient_cluster_export_root(
                    data,
                    source,
                    cluster_source_stem=stem,
                    source_fbx_path=source_fbx,
                    source_identity_path=source_identity,
                    before_snapshot=before,
                )

                self.assertEqual(report["status"], "reconciled")
                self.assertEqual(
                    sorted(obj.name for obj in export.objects),
                    [f"{stem}_01", f"{stem}_02"],
                )
                self.assertEqual(set(source.objects), set(hierarchy))
                self.assertEqual(
                    cluster_export_contract_issues(data, stem),
                    [],
                )

    def test_new_unowned_unsuffixed_root_remains_fail_closed(self):
        stem = "SK_cluster_densiflora_02"
        source_fbx = rf"C:\Sanitized\Cluster\fbx\{stem}.fbx"
        source_identity = rf"C:\Sanitized\Cluster\{stem}.spm"
        export = FakeCollection(ordinal_pivots(stem))
        source = FakeCollection()
        data = SimpleNamespace(collections={"Export": export})
        before = capture_cluster_export_snapshot(data, stem)
        hierarchy = transient_assembly_hierarchy(
            stem,
            source_fbx,
            source_identity,
            owned=False,
        )
        export.objects.extend(hierarchy)

        report = reconcile_transient_cluster_export_root(
            data,
            source,
            cluster_source_stem=stem,
            source_fbx_path=source_fbx,
            source_identity_path=source_identity,
            before_snapshot=before,
        )

        self.assertEqual(report["status"], "blocked")
        self.assertEqual(
            report["reason"],
            "ambiguous_unsuffixed_export_hierarchy",
        )
        self.assertEqual(source.objects, [])
        self.assertIn(
            f"cluster_unsuffixed_export_unit:{stem}",
            cluster_export_contract_issues(data, stem),
        )

    def test_wrong_source_identity_cannot_authorize_reconciliation(self):
        stem = "SK_cluster_densiflora_02"
        source_fbx = rf"C:\Sanitized\Cluster\fbx\{stem}.fbx"
        source_identity = rf"C:\Sanitized\Cluster\{stem}.spm"
        export = FakeCollection(ordinal_pivots(stem))
        source = FakeCollection()
        data = SimpleNamespace(collections={"Export": export})
        before = capture_cluster_export_snapshot(data, stem)
        hierarchy = transient_assembly_hierarchy(
            stem,
            source_fbx,
            rf"C:\Sanitized\Other\{stem}.spm",
        )
        export.objects.extend(hierarchy)

        report = reconcile_transient_cluster_export_root(
            data,
            source,
            cluster_source_stem=stem,
            source_fbx_path=source_fbx,
            source_identity_path=source_identity,
            before_snapshot=before,
        )

        self.assertEqual(report["status"], "blocked")
        self.assertEqual(source.objects, [])
        self.assertEqual(
            report["issues"],
            [f"cluster_unsuffixed_export_unit:{stem}"],
        )

    def test_persisted_authored_unsuffixed_root_remains_fail_closed(self):
        stem = "SK_leaf_weeping_willow_side_01"
        source_fbx = rf"C:\Sanitized\Cluster\fbx\{stem}.fbx"
        source_identity = rf"C:\Sanitized\Cluster\{stem}.spm"
        hierarchy = transient_assembly_hierarchy(
            stem,
            source_fbx,
            source_identity,
            owned=False,
        )
        export = FakeCollection([*ordinal_pivots(stem), *hierarchy])
        source = FakeCollection()
        data = SimpleNamespace(collections={"Export": export})
        before = capture_cluster_export_snapshot(data, stem)

        report = reconcile_transient_cluster_export_root(
            data,
            source,
            cluster_source_stem=stem,
            source_fbx_path=source_fbx,
            source_identity_path=source_identity,
            before_snapshot=before,
        )

        self.assertEqual(report["status"], "blocked")
        self.assertEqual(report["reason"], "persisted_unsuffixed_export_root")
        self.assertEqual(
            report["issues"],
            [f"cluster_unsuffixed_export_unit:{stem}"],
        )
        self.assertEqual(source.objects, [])
        self.assertIn(
            f"cluster_unsuffixed_export_unit:{stem}",
            cluster_export_contract_issues(data, stem),
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

    def test_normalizer_publishes_final_export_postcondition(self):
        postcondition = {
            "kind": "sk_batch_export_object_postcondition",
            "coverage": "exact_export_collection_all_objects",
            "objects": [{"name": "SK_branch_elm_01_01"}],
        }
        finalized, changed = finalize_cluster_pipeline_payload(
            pending_payload(),
            export_issues=[],
            export_postcondition=postcondition,
        )

        self.assertTrue(changed)
        self.assertEqual(
            finalized["assembly_export_postcondition"],
            postcondition,
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

    def test_normalizer_rebinds_final_pipeline_to_saved_blend(self):
        payload = {
            "handoff_preflight": {
                "status": "ok",
                "unreal_push_ready": True,
            },
            "source_blend_identity": {"sha256": "before"},
        }
        identity = {
            "path": "C:/fixture/SK_branch_elm_01.blend",
            "exists": True,
            "size": 123,
            "mtime_ns": 456,
            "sha256": "a" * 64,
        }

        finalized, changed = finalize_cluster_pipeline_payload(
            payload,
            export_issues=[],
            source_blend_identity=identity,
        )

        self.assertTrue(changed)
        self.assertEqual(finalized["source_blend_identity"], identity)
        self.assertEqual(
            payload["source_blend_identity"], {"sha256": "before"}
        )

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
