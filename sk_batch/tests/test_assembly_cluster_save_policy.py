"""Cluster source saves use the GUI transaction, not Blender .blend1.

The production job imports ``bpy`` at module load, so extract the small helper
from its AST and execute that exact function with a fake Blender module.
"""
import ast
import tempfile
import types
import unittest
from pathlib import Path


SK_BATCH_DIR = Path(__file__).resolve().parents[1]
JOB_PATH = SK_BATCH_DIR / "jobs" / "assembly_headless_job.py"


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


def load_job_functions(function_names, extra_namespace=None):
    tree = ast.parse(JOB_PATH.read_text(encoding="utf-8"))
    requested = set(function_names)
    functions = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name in requested
    ]
    if {node.name for node in functions} != requested:
        raise AssertionError("Requested job helper is missing")
    module = ast.Module(body=functions, type_ignores=[])
    namespace = {"os": __import__("os"), "Path": Path}
    namespace.update(extra_namespace or {})
    exec(compile(module, str(JOB_PATH), "exec"), namespace)
    return namespace


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
    def test_single_import_requires_the_same_exact_resolved_fbx(self):
        helper = load_job_functions(
            ["same_resolved_fbx_source"]
        )["same_resolved_fbx_source"]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            expected = root / "tree.fbx"

            self.assertTrue(helper(expected, root / "." / "tree.fbx"))
            self.assertFalse(helper(expected, root / "other.fbx"))
            self.assertFalse(helper(expected, None))

    def test_raw_import_observer_builds_handoff_from_same_objects(self):
        imported = [object(), object()]
        handoff = {"status": "ready", "parts": ["exact"]}
        calls = []

        def build(
            receipt,
            spm,
            source_fbx,
            observed_objects,
            **_contract_snapshot,
        ):
            calls.append(
                (receipt, spm, source_fbx, observed_objects)
            )
            return handoff

        def require_ready(observed_handoff):
            self.assertIs(observed_handoff, handoff)

        namespace = load_job_functions(
            [
                "same_resolved_fbx_source",
                "make_cluster_assembly_raw_import_observer",
            ],
            {
                "build_cluster_assembly_handoff_from_imported": build,
                "require_cluster_assembly_handoff_ready": require_ready,
            },
        )
        factory = namespace[
            "make_cluster_assembly_raw_import_observer"
        ]
        expected = Path("C:/exact/tree.fbx")
        observer, state = factory("receipt", "tree.spm", expected)

        result = observer(imported, Path("C:/exact/./tree.fbx"))

        self.assertIs(result, handoff)
        self.assertIs(state["handoff"], handoff)
        self.assertEqual(len(calls), 1)
        self.assertIs(calls[0][3], imported)
        self.assertEqual(calls[0][2], expected)

    def test_raw_import_observer_rejects_a_different_fbx_before_build(self):
        calls = []
        namespace = load_job_functions(
            [
                "same_resolved_fbx_source",
                "make_cluster_assembly_raw_import_observer",
            ],
            {
                "build_cluster_assembly_handoff_from_imported": (
                    lambda *args, **kwargs: calls.append((args, kwargs))
                ),
                "require_cluster_assembly_handoff_ready": lambda value: None,
            },
        )
        observer, state = namespace[
            "make_cluster_assembly_raw_import_observer"
        ]("receipt", "tree.spm", Path("C:/exact/tree.fbx"))

        with self.assertRaisesRegex(RuntimeError, "different FBX"):
            observer([], Path("C:/other/tree.fbx"))

        self.assertEqual(calls, [])
        self.assertEqual(state, {})

    def test_raw_import_observer_block_does_not_publish_state(self):
        handoff = {"status": "blocked"}

        def reject(_handoff):
            raise RuntimeError("exact handoff blocked")

        namespace = load_job_functions(
            [
                "same_resolved_fbx_source",
                "make_cluster_assembly_raw_import_observer",
            ],
            {
                "build_cluster_assembly_handoff_from_imported": (
                    lambda *args, **kwargs: handoff
                ),
                "require_cluster_assembly_handoff_ready": reject,
            },
        )
        observer, state = namespace[
            "make_cluster_assembly_raw_import_observer"
        ]("receipt", "tree.spm", Path("C:/exact/tree.fbx"))

        with self.assertRaisesRegex(RuntimeError, "exact handoff blocked"):
            observer([], Path("C:/exact/tree.fbx"))

        self.assertEqual(state, {})

    def test_headless_job_passes_optional_observer_to_addon(self):
        tree = ast.parse(JOB_PATH.read_text(encoding="utf-8"))
        calls = [
            node
            for node in ast.walk(tree)
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "run_import_and_assemble"
            )
        ]
        self.assertEqual(len(calls), 1)
        keyword = next(
            value.value
            for value in calls[0].keywords
            if value.arg == "raw_import_observer"
        )
        self.assertIsInstance(keyword, ast.Name)
        self.assertEqual(
            keyword.id,
            "cluster_assembly_raw_import_observer",
        )

    def test_validated_contract_snapshot_prevents_receipt_reload(self):
        payload = {"items": [{"cluster_assembly": {}}]}
        contract = payload["items"][0]["cluster_assembly"]
        calls = []

        def unexpected_reload(*_args, **_kwargs):
            self.fail("validated receipt snapshot must not be reloaded")

        def build_handoff(receipt, spm, inventory, **kwargs):
            calls.append((receipt, spm, inventory, kwargs))
            return {"status": "ready"}

        helper = load_job_functions(
            ["build_cluster_assembly_handoff_from_imported"],
            {
                "load_cluster_contract": unexpected_reload,
                "role_identity_aliases_from_contract": (
                    lambda selected, spm: {"branch": ["branch"]}
                ),
                "build_blender_fbx_inventory": (
                    lambda objects, source, roles: {
                        "objects": objects,
                        "source": source,
                        "roles": roles,
                    }
                ),
                "build_assembly_handoff": build_handoff,
            },
        )["build_cluster_assembly_handoff_from_imported"]
        imported = [object()]

        result = helper(
            "receipt.json",
            "tree.spm",
            "tree.fbx",
            imported,
            receipt_payload=payload,
            selected_contract=contract,
        )

        self.assertEqual(result, {"status": "ready"})
        self.assertIs(calls[0][3]["receipt_payload"], payload)
        self.assertIs(calls[0][3]["selected_contract"], contract)

    def test_explicit_live_pass_through_overrides_saved_build(self):
        helper = load_assembly_selection_helper()
        live_handoff = {"status": "pass_through", "source": "live"}
        saved_handoff = {"status": "ready", "source": "manifest"}

        mode, selected = helper(
            {"handoff": live_handoff},
            None,
            saved_handoff,
            explicit_live_authority=True,
        )

        self.assertEqual(mode, "pass_through")
        self.assertIs(selected, live_handoff)

    def test_persisted_pass_through_does_not_change_manifest_priority(self):
        helper = load_assembly_selection_helper()
        receipt_handoff = {
            "status": "pass_through",
            "source": "persisted",
        }
        saved_handoff = {"status": "ready", "source": "manifest"}

        mode, selected = helper(
            {"handoff": receipt_handoff},
            None,
            saved_handoff,
            explicit_live_authority=False,
        )

        self.assertEqual(mode, "build")
        self.assertIs(selected, saved_handoff)

    def test_source_only_rejects_explicit_assembly_contract(self):
        helper = load_job_functions(
            ["validate_cluster_job_mode_arguments"]
        )["validate_cluster_job_mode_arguments"]

        with self.assertRaisesRegex(
            RuntimeError,
            "cannot be combined",
        ):
            helper(True, "live_assembly.json")

        helper(True, "")
        helper(False, "live_assembly.json")

    def test_contract_snapshot_rechecks_live_authority(self):
        contract = {"handoff": {"status": "pass_through"}}
        payload = {
            "cluster_assembly_receipt_persistence": {
                "live_audit_complete": False,
            }
        }
        helper = load_job_functions(
            ["cluster_assembly_contract_from_material_contract"],
            {
                "load_cluster_contract": (
                    lambda *args, **kwargs: (payload, contract)
                ),
            },
        )["cluster_assembly_contract_from_material_contract"]

        with self.assertRaisesRegex(ValueError, "lost completed live"):
            helper(
                "live.json",
                "tree.spm",
                require_exact=True,
                require_live_audit=True,
                include_payload=True,
            )

        payload["cluster_assembly_receipt_persistence"][
            "live_audit_complete"
        ] = True
        selected_payload, selected_contract = helper(
            "live.json",
            "tree.spm",
            require_exact=True,
            require_live_audit=True,
            include_payload=True,
        )
        self.assertIs(selected_payload, payload)
        self.assertIs(selected_contract, contract)

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

    def test_current_inspected_pass_through_overrides_old_build_manifest(self):
        helper = load_assembly_selection_helper()
        inspected_handoff = {"status": "pass_through", "source": "fbx"}
        current_handoff = {"status": "ready", "source": "manifest"}

        mode, selected = helper(
            None,
            inspected_handoff,
            current_handoff,
        )

        self.assertEqual(mode, "pass_through")
        self.assertIs(selected, inspected_handoff)

    def test_current_manifest_is_only_used_without_conclusive_inspection(self):
        helper = load_assembly_selection_helper()
        current_handoff = {"status": "ready", "source": "manifest"}

        mode, selected = helper(None, None, current_handoff)

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
            "sk_batch_gui_pre_assembly_copy_and_rollback",
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
            "# The Assembly result is the save authority.",
            source,
        )
        self.assertIn(
            "if merged_object is not None or empty_after_dummy_cleanup:",
            source,
        )
        self.assertNotIn("blocking_export_collection_issues", source)

    def test_authorized_dummy_only_asset_is_not_a_native_skin_failure(self):
        source = JOB_PATH.read_text(encoding="utf-8")
        self.assertIn(
            'result.get("status") == "empty_after_dummy_cleanup"',
            source,
        )
        self.assertIn(
            'handoff_status = "empty_after_dummy_cleanup"',
            source,
        )
        self.assertIn(
            'result["native_skin_passthrough"] = False',
            source,
        )


if __name__ == "__main__":
    unittest.main()
