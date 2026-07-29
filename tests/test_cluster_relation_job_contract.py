import unittest
from pathlib import Path


JOB_PATH = (
    Path(__file__).resolve().parents[1]
    / "spm_generator_sync"
    / "jobs"
    / "cluster_relation_job.py"
)


class ClusterRelationJobContractTests(unittest.TestCase):
    def test_physical_handoff_reads_the_cluster_key_with_legacy_fallback(self):
        source = JOB_PATH.read_text(encoding="utf-8")
        self.assertIn('build.get("cluster_handoff")', source)
        self.assertIn('build.get("atlas_handoff")', source)

    def test_cluster_export_preserves_the_explicit_cluster_material_name(self):
        source = JOB_PATH.read_text(encoding="utf-8")
        self.assertIn(
            "preserve_explicit_material_name=True",
            source,
        )

    def test_existing_relation_is_detached_before_idempotent_rebuild(self):
        source = JOB_PATH.read_text(encoding="utf-8")
        cleanup = source.index("pre_export_relation_cleanup = [")
        export = source.index("export_or_update_speedtree_spm_targets(", cleanup)
        self.assertIn(
            "cleanup_existing_relation_for_rebuild(",
            source[cleanup:export],
        )

    def test_plain_export_scope_is_not_removed_before_first_adoption(self):
        source = JOB_PATH.read_text(encoding="utf-8")
        helper = source[
            source.index("def relation_manifest_requires_pre_export_cleanup("):
            source.index("\ndef sync_targets(", source.index(
                "def relation_manifest_requires_pre_export_cleanup("
            ))
        ]
        self.assertIn('manifest.get("source_material_adoption")', helper)
        self.assertIn('manifest.get("generator_connection")', helper)
        self.assertIn('connection.get("requested") is True', helper)
        self.assertIn('"not_required_no_active_relation"', helper)
        self.assertIn("preserve_scope_history=True", helper)

    def test_current_receipt_rehydrates_the_plan_collection_before_export(self):
        source = JOB_PATH.read_text(encoding="utf-8")
        self.assertIn(
            "configure_cluster_export_properties(",
            source,
        )
        self.assertIn(
            'collection_name=recipe["plan_collection"]',
            source,
        )
        self.assertIn(
            'first_binding.get("connect_generators") is True',
            source,
        )

    def test_effective_registry_requires_one_explicit_binding_per_target(self):
        source = JOB_PATH.read_text(encoding="utf-8")
        self.assertIn("validate_recipe_registry_contract(", source)
        self.assertIn("set(binding_targets) != set(registered)", source)
        self.assertIn(
            'binding.get("connect_generators") not in {True, False}',
            source,
        )
        self.assertNotIn(
            'first_binding.get("connect_generators", True)',
            source,
        )

    def test_final_export_is_verified_before_pending_handoff_is_promoted(self):
        source = JOB_PATH.read_text(encoding="utf-8")
        contract_source = (
            JOB_PATH.parents[2] / "cluster_export_handoff_contract.py"
        ).read_text(encoding="utf-8")
        self.assertIn("cluster_export_contract_issues(", source)
        self.assertIn("finalize_cluster_source_pipeline(", source)
        self.assertIn("finalize_cluster_pipeline_payload(", source)
        self.assertIn('handoff["status"] = final_status', contract_source)
        self.assertIn('"cluster_export_pending"', contract_source)

    def test_worker_failure_persists_traceback_before_parent_rollback(self):
        source = JOB_PATH.read_text(encoding="utf-8")
        self.assertIn(
            "_persist_cluster_relation_failure(",
            source,
        )
        self.assertIn(
            'phase="blender_worker_exception"',
            source,
        )
        self.assertIn(
            '"persistent_failure_report"',
            source,
        )


if __name__ == "__main__":
    unittest.main()
