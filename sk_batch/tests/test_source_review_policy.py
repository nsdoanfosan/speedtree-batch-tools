"""② source-review policy must stay reachable.

Cluster rows are normalized to their canonical ``SK_`` name before the Blender
job starts, so ``--spm`` and ``--speedtree-spm`` always name the same file.
Any policy keyed on "this is a raw, unprefixed Cluster source" is therefore
unreachable: it silently mislabels the report while the strict gate is what
actually runs.  These tests pin the two policies that can occur.

The job imports ``bpy``, so it is inspected as source rather than imported.
"""
import ast
import sys
import unittest
from pathlib import Path


SK_BATCH_DIR = Path(__file__).resolve().parents[1]
if str(SK_BATCH_DIR) not in sys.path:
    sys.path.insert(0, str(SK_BATCH_DIR))

JOB_PATH = SK_BATCH_DIR / "jobs" / "bwr_headless_job.py"


def job_tree():
    return ast.parse(JOB_PATH.read_text(encoding="utf-8"))


def assigned_string_values(tree, target_name):
    """Every string literal that can be assigned to *target_name*."""
    values = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        names = {
            target.id
            for target in node.targets
            if isinstance(target, ast.Name)
        }
        if target_name not in names:
            continue
        for child in ast.walk(node.value):
            if isinstance(child, ast.Constant) and isinstance(child.value, str):
                values.add(child.value)
    return values


class SourceReviewPolicyTests(unittest.TestCase):
    def test_only_reachable_policies_are_declared(self):
        policies = assigned_string_values(job_tree(), "source_review_policy")
        self.assertEqual(policies, {"strict", "legacy_cluster_receipt"})

    def test_no_policy_keys_off_a_raw_unprefixed_cluster_name(self):
        source = JOB_PATH.read_text(encoding="utf-8")
        # These were unreachable: the GUI never passes an unprefixed name.
        self.assertNotIn("cluster_source_read_only", source)
        self.assertNotIn("cluster_pair_strict", source)
        self.assertNotIn("is_cluster_source_spm", source)

    def test_the_source_gate_is_relaxed_only_by_legacy_receipt_lineage(self):
        tree = job_tree()
        gate = None
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Assign)
                and any(
                    isinstance(target, ast.Name)
                    and target.id == "source_review_allowed"
                    for target in node.targets
                )
            ):
                gate = node.value
        self.assertIsNotNone(gate, "source_review_allowed assignment is missing")
        names = {
            child.id for child in ast.walk(gate) if isinstance(child, ast.Name)
        }
        self.assertEqual(names, {"legacy_cluster_origin"})

    def test_the_two_spm_identities_are_still_plumbed_separately(self):
        """The pair contract can still hand the job two different files."""
        source = JOB_PATH.read_text(encoding="utf-8")
        self.assertIn('parser.add_argument("--speedtree-spm"', source)
        self.assertIn("canonical_spm = Path(args.spm)", source)
        self.assertIn(
            "speedtree_spm = Path(args.speedtree_spm or args.spm)", source
        )

    def test_bwr_receipt_records_the_live_canonical_spm_identity(self):
        source = JOB_PATH.read_text(encoding="utf-8")
        self.assertIn(
            'pipeline_data["speedtree_live_source_identity"]',
            source,
        )
        self.assertIn('"spm": source_identity(canonical_spm)', source)


if __name__ == "__main__":
    unittest.main()
