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
            'first_binding.get("connect_generators", True)',
            source,
        )

    def test_final_export_is_verified_before_pending_handoff_is_promoted(self):
        source = JOB_PATH.read_text(encoding="utf-8")
        self.assertIn("cluster_export_contract_issues(", source)
        self.assertIn("finalize_cluster_source_pipeline(", source)
        self.assertIn('handoff["status"] = final_status', source)
        self.assertIn('"cluster_export_pending"', source)


if __name__ == "__main__":
    unittest.main()
