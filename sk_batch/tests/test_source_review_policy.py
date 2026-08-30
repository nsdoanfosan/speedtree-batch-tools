"""BWR source observations remain diagnostic after repair completes.

Cluster rows are normalized to their canonical ``SK_`` name before the Blender
job starts, so ``--spm`` and ``--speedtree-spm`` always name the same file.
Legacy marker/GUID receipts and source issues remain reportable, but neither
may discard a completed repair result.

The job imports ``bpy``, so it is inspected as source rather than imported.
"""
import ast
import sys
import unittest
from pathlib import Path


SK_BATCH_DIR = Path(__file__).resolve().parents[1]
if str(SK_BATCH_DIR) not in sys.path:
    sys.path.insert(0, str(SK_BATCH_DIR))

JOB_PATH = SK_BATCH_DIR / "jobs" / "assembly_headless_job.py"


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


def call_lines(tree, function_name):
    return sorted(
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and (
            (
                isinstance(node.func, ast.Name)
                and node.func.id == function_name
            )
            or (
                isinstance(node.func, ast.Attribute)
                and node.func.attr == function_name
            )
        )
    )


class SourceReviewPolicyTests(unittest.TestCase):
    def test_material_contract_is_required_before_blender_mutation(self):
        material_arguments = [
            node
            for node in ast.walk(job_tree())
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "add_argument"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and node.args[0].value == "--material-contract"
            )
        ]
        self.assertEqual(len(material_arguments), 1)
        required = next(
            (
                keyword.value.value
                for keyword in material_arguments[0].keywords
                if (
                    keyword.arg == "required"
                    and isinstance(keyword.value, ast.Constant)
                )
            ),
            None,
        )
        self.assertIs(required, True)

    def test_only_reachable_policies_are_declared(self):
        policies = assigned_string_values(job_tree(), "source_review_policy")
        self.assertEqual(policies, {"diagnostic_only"})

    def test_no_policy_keys_off_a_raw_unprefixed_cluster_name(self):
        source = JOB_PATH.read_text(encoding="utf-8")
        # These were unreachable: the GUI never passes an unprefixed name.
        self.assertNotIn("cluster_source_read_only", source)
        self.assertNotIn("cluster_pair_strict", source)
        self.assertNotIn("is_cluster_source_spm", source)

    def test_legacy_receipt_lineage_does_not_create_a_source_gate(self):
        source = JOB_PATH.read_text(encoding="utf-8")
        self.assertNotIn("source_review_allowed", source)
        self.assertNotIn("inspect_legacy_cluster_state", source)
        self.assertIn(
            "legacy_lineage_is_not_an_assembly_or_export_input",
            source,
        )

    def test_legacy_marker_drift_scan_is_not_on_the_repair_hot_path(self):
        source = JOB_PATH.read_text(encoding="utf-8")
        self.assertNotIn("marker_drift_non_blocking", source)
        self.assertIn('"marker_drift_guids": []', source)

    def test_the_two_spm_identities_are_still_plumbed_separately(self):
        """The pair contract can still hand the job two different files."""
        source = JOB_PATH.read_text(encoding="utf-8")
        self.assertIn('parser.add_argument("--speedtree-spm"', source)
        self.assertIn("canonical_spm = Path(args.spm)", source)
        self.assertIn(
            "speedtree_spm = Path(args.speedtree_spm or args.spm)", source
        )

    def test_assembly_receipt_records_the_live_canonical_spm_identity(self):
        source = JOB_PATH.read_text(encoding="utf-8")
        self.assertIn(
            'pipeline_data["speedtree_live_source_identity"]',
            source,
        )
        self.assertIn('"spm": source_identity(canonical_spm)', source)

    def test_live_contract_is_refreshed_without_a_second_validator(self):
        tree = job_tree()
        export_lines = call_lines(tree, "run_selected_speedtree_export")
        material_validation_lines = call_lines(
            tree, "validate_preflight_report"
        )
        exact_export_refresh_lines = call_lines(
            tree, "refresh_preflight_report_after_exact_export"
        )
        assembly_inspection_lines = call_lines(
            tree, "inspect_cluster_assembly_fbx"
        )
        assembly_lines = call_lines(tree, "run_import_and_assemble")

        self.assertEqual(len(export_lines), 2)
        self.assertEqual(len(material_validation_lines), 1)
        self.assertEqual(len(exact_export_refresh_lines), 1)
        self.assertLess(material_validation_lines[0], export_lines[0])
        self.assertGreater(exact_export_refresh_lines[0], max(export_lines))
        self.assertEqual(len(assembly_inspection_lines), 1)
        self.assertGreater(assembly_inspection_lines[0], max(export_lines))
        self.assertEqual(len(assembly_lines), 1)
        self.assertLess(
            max(exact_export_refresh_lines[0], assembly_inspection_lines[0]),
            assembly_lines[0],
        )

    def test_all_assembly_exports_use_native_bones(self):
        calls = sorted(
            (
                node
                for node in ast.walk(job_tree())
                if isinstance(node, ast.Call)
                and (
                    (
                        isinstance(node.func, ast.Attribute)
                        and node.func.attr == "run_selected_speedtree_export"
                    )
                    or (
                        isinstance(node.func, ast.Name)
                        and node.func.id == "run_selected_speedtree_export"
                    )
                )
            ),
            key=lambda node: node.lineno,
        )
        self.assertEqual(len(calls), 2)
        for call in calls:
            keywords = {item.arg: item.value for item in call.keywords}
            self.assertNotIn("allow_boneless", keywords)
            self.assertIn("force_reexport", keywords)
            self.assertEqual(
                ast.dump(keywords["force_reexport"]),
                ast.dump(
                    ast.Attribute(
                        value=ast.Name(id="args", ctx=ast.Load()),
                        attr="force_native_export",
                        ctx=ast.Load(),
                    )
                ),
            )

    def test_runtime_gateway_selects_only_explicit_export_modes(self):
        source = JOB_PATH.read_text(encoding="utf-8")
        self.assertIn('"run_speedtree_cli_export"', source)
        self.assertIn('"run_fresh_verification_only_export"', source)
        self.assertIn("run_selected_speedtree_export =", source)


if __name__ == "__main__":
    unittest.main()
