"""Cluster source saves use the GUI transaction, not Blender .blend1.

The production job imports ``bpy`` at module load, so extract the small helper
from its AST and execute that exact function with a fake Blender module.
"""
import ast
import types
import unittest
from pathlib import Path


SK_BATCH_DIR = Path(__file__).resolve().parents[1]
JOB_PATH = SK_BATCH_DIR / "jobs" / "bwr_headless_job.py"


def load_save_helper():
    tree = ast.parse(JOB_PATH.read_text(encoding="utf-8"))
    function = next(
        node
        for node in tree.body
        if (
            isinstance(node, ast.FunctionDef)
            and node.name == "save_cluster_source_mainfile"
        )
    )
    module = ast.Module(body=[function], type_ignores=[])
    namespace = {}
    exec(compile(module, str(JOB_PATH), "exec"), namespace)
    return namespace["save_cluster_source_mainfile"]


def load_assembly_selection_helper():
    tree = ast.parse(JOB_PATH.read_text(encoding="utf-8"))
    function = next(
        node
        for node in tree.body
        if (
            isinstance(node, ast.FunctionDef)
            and node.name == "select_cluster_assembly_build_handoff"
        )
    )
    module = ast.Module(body=[function], type_ignores=[])
    namespace = {}
    exec(compile(module, str(JOB_PATH), "exec"), namespace)
    return namespace["select_cluster_assembly_build_handoff"]


class FakeBlender:
    def __init__(self, save_version, error=None):
        self.filepaths = types.SimpleNamespace(save_version=save_version)
        self.observed_save_versions = []
        self.error = error
        self.context = types.SimpleNamespace(
            preferences=types.SimpleNamespace(filepaths=self.filepaths)
        )
        self.ops = types.SimpleNamespace(
            wm=types.SimpleNamespace(save_as_mainfile=self.save_as_mainfile)
        )

    def save_as_mainfile(self, *, filepath):
        self.observed_save_versions.append(self.filepaths.save_version)
        if self.error is not None:
            raise self.error
        return {"FINISHED"}


class ClusterSavePolicyTests(unittest.TestCase):
    def test_ready_fbx_handoff_overrides_receipt_pass_through(self):
        helper = load_assembly_selection_helper()
        receipt_handoff = {"status": "pass_through", "source": "receipt"}
        inspected_handoff = {"status": "ready", "source": "fbx"}

        mode, selected = helper(
            {"handoff": receipt_handoff}, inspected_handoff
        )

        self.assertEqual(mode, "build")
        self.assertIs(selected, inspected_handoff)

    def test_receipt_pass_through_remains_without_ready_fbx_roles(self):
        helper = load_assembly_selection_helper()
        receipt_handoff = {"status": "pass_through", "source": "receipt"}

        mode, selected = helper({"handoff": receipt_handoff}, None)

        self.assertEqual(mode, "pass_through")
        self.assertIs(selected, receipt_handoff)

    def test_inspected_pass_through_is_not_misclassified_as_build(self):
        helper = load_assembly_selection_helper()
        inspected_handoff = {"status": "pass_through", "source": "fbx"}

        mode, selected = helper(None, inspected_handoff)

        self.assertEqual(mode, "pass_through")
        self.assertIs(selected, inspected_handoff)

    def test_current_manifest_overrides_legacy_inspected_pass_through(self):
        helper = load_assembly_selection_helper()
        inspected_handoff = {"status": "pass_through", "source": "fbx"}
        current_handoff = {"status": "ready", "source": "manifest"}

        mode, selected = helper(
            None,
            inspected_handoff,
            current_handoff,
        )

        self.assertEqual(mode, "build")
        self.assertIs(selected, current_handoff)

    def test_cluster_save_disables_version_backup_for_operator_only(self):
        helper = load_save_helper()
        blender = FakeBlender(save_version=3)
        report = {}

        result = helper(blender, r"D:\Tree\Cluster\SK_leaf.blend", report)

        self.assertEqual(result, {"FINISHED"})
        self.assertEqual(blender.observed_save_versions, [0])
        self.assertEqual(blender.filepaths.save_version, 3)
        policy = report["blend_save_policy"]
        self.assertEqual(policy["status"], "committed")
        self.assertEqual(policy["original_save_version"], 3)
        self.assertEqual(policy["observed_effective_save_version"], 0)
        self.assertEqual(policy["restored_save_version"], 3)
        self.assertTrue(policy["preference_restored"])
        self.assertFalse(policy["preference_persisted"])
        self.assertEqual(
            policy["transaction_backup"],
            "sk_batch_gui_pre_repair_copy_and_rollback",
        )

    def test_cluster_save_restores_preference_after_operator_failure(self):
        helper = load_save_helper()
        blender = FakeBlender(
            save_version=2,
            error=RuntimeError("Version backup failed"),
        )
        report = {}

        with self.assertRaisesRegex(RuntimeError, "Version backup failed"):
            helper(blender, r"D:\Tree\Cluster\SK_leaf.blend", report)

        self.assertEqual(blender.observed_save_versions, [0])
        self.assertEqual(blender.filepaths.save_version, 2)
        policy = report["blend_save_policy"]
        self.assertEqual(policy["status"], "operator_failed")
        self.assertEqual(policy["error_type"], "RuntimeError")
        self.assertEqual(policy["restored_save_version"], 2)
        self.assertTrue(policy["preference_restored"])

    def test_final_save_policy_is_scoped_to_cluster_sources(self):
        source = JOB_PATH.read_text(encoding="utf-8")
        self.assertIn(
            "if is_cluster_source:\n"
            "                save_result = save_cluster_source_mainfile(",
            source,
        )
        self.assertIn(
            'pipeline_data["blend_save_policy"] = report[',
            source,
        )

    def test_optional_handoff_diagnostics_do_not_prevent_blend_save(self):
        source = JOB_PATH.read_text(encoding="utf-8")
        self.assertIn(
            "# The repair result is the save authority.",
            source,
        )
        self.assertIn("if merged_object is not None:", source)
        self.assertNotIn("blocking_export_collection_issues", source)


if __name__ == "__main__":
    unittest.main()
