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

    def test_existing_relation_is_normalized_in_place(self):
        source = JOB_PATH.read_text(encoding="utf-8")
        sync_source = source[
            source.index("def sync_targets("):
            source.index("\ndef remove_targets(")
        ]
        self.assertIn(
            "Normalize each requested relation in place.",
            sync_source,
        )
        self.assertNotIn("pre_export_relation_cleanup", sync_source)
        self.assertNotIn("cleanup_existing_relation_for_rebuild", sync_source)
        self.assertNotIn("remove_blend_target_from_spm", sync_source)

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

    def test_requested_live_slice_requires_one_explicit_binding_per_target(self):
        source = JOB_PATH.read_text(encoding="utf-8")
        self.assertIn("validate_recipe_registry_contract(", source)
        self.assertIn(
            "not set(effective).issubset(set(recipe_targets))",
            source,
        )
        self.assertIn(
            'binding.get("connect_generators") not in {True, False}',
            source,
        )
        self.assertIn(
            "for path in requested",
            source[source.index("props.speedtree_spm_items.clear()"):],
        )
        self.assertNotIn(
            "save_spm_target_registry(props)",
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

    def test_exact_isolated_bark_bundle_is_revalidated_before_map_bake(self):
        source = JOB_PATH.read_text(encoding="utf-8")
        normalize_source = source[
            source.index("def normalize_cluster_blend("):
            source.index("\ndef configure_cluster_export_properties(")
        ]
        validation = normalize_source.index(
            "validate_isolated_bark_recipe_bundle(recipe)"
        )
        bake = normalize_source.index(
            "bpy.ops.speedtree_cluster.bake_capture_maps()"
        )
        self.assertLess(validation, bake)


if __name__ == "__main__":
    unittest.main()
