"""Cluster source saves use the GUI transaction, not Blender .blend1.

The production job imports ``bpy`` at module load, so extract the small helper
from its AST and execute that exact function with a fake Blender module.
"""
import ast
import hashlib
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
    def test_speedtree_export_timeout_layering_is_fail_closed(self):
        helper = load_job_functions(
            ["validate_speedtree_export_timeout_layering"]
        )["validate_speedtree_export_timeout_layering"]

        policy = helper(1200, "840000")
        self.assertTrue(policy["layering_validated"])
        self.assertEqual(policy["wrapper_timeout_ms"], 840000)
        with self.assertRaisesRegex(RuntimeError, "smaller"):
            helper(840, "840000")
        with self.assertRaisesRegex(RuntimeError, "positive integer"):
            helper(1200, "not-a-number")
        with self.assertRaisesRegex(RuntimeError, "positive integer"):
            helper(0, "840000")

    def test_normal_collision_export_rejects_any_verification_fallback(self):
        helper = load_job_functions(
            ["validate_normal_collision_export_result"]
        )["validate_normal_collision_export_result"]

        def export_row():
            return {
                "exists": True,
                "returncode": 0,
                "cache_hit": False,
                "force_reexport_requested": True,
                "verification_only": False,
                "bundled_process": True,
                "bundle_fallback": False,
                "stdout": "Post-collision export completed.",
                "export_attempts": [{"attempt": 1, "returncode": 0}],
            }

        result = {
            "force_reexport_requested": True,
            "exports": {"fbx": export_row(), "xml": export_row()},
        }
        policy = helper(result)
        self.assertEqual(policy["status"], "validated")

        for field, value in (
            ("verification_only", True),
            ("verification_only", None),
            ("bundle_fallback", True),
            ("bundle_fallback", None),
            ("bundled_process", False),
            ("cache_hit", True),
        ):
            with self.subTest(field=field, value=value):
                broken = {
                    "force_reexport_requested": True,
                    "exports": {"fbx": export_row(), "xml": export_row()},
                }
                if value is None:
                    broken["exports"]["fbx"].pop(field)
                else:
                    broken["exports"]["fbx"][field] = value
                with self.assertRaisesRegex(
                    RuntimeError,
                    "evidence is invalid",
                ):
                    helper(broken)

    def test_normal_collision_export_requires_one_success_and_marker(self):
        helper = load_job_functions(
            ["validate_normal_collision_export_result"]
        )["validate_normal_collision_export_result"]
        row = {
            "exists": True,
            "returncode": 0,
            "cache_hit": False,
            "force_reexport_requested": True,
            "verification_only": False,
            "bundled_process": True,
            "bundle_fallback": False,
            "stdout": "Post-collision export completed.",
            "export_attempts": [{"attempt": 1, "returncode": 0}],
        }
        result = {
            "force_reexport_requested": True,
            "exports": {"fbx": dict(row), "xml": dict(row)},
        }
        result["exports"]["fbx"]["export_attempts"] = []
        with self.assertRaisesRegex(RuntimeError, "one exact successful"):
            helper(result)

        result["exports"]["fbx"] = dict(row)
        result["exports"]["fbx"]["stdout"] = ""
        with self.assertRaisesRegex(RuntimeError, "completion marker"):
            helper(result)

    def test_fresh_verification_mode_requires_force_and_exact_pair(self):
        helper = load_job_functions(
            ["validate_native_export_mode_arguments"]
        )["validate_native_export_mode_arguments"]

        helper(True, True, export_fbx=True, export_xml=True)
        helper(False, False, export_fbx=False, export_xml=False)
        with self.assertRaisesRegex(
            RuntimeError,
            "requires --force-native-export",
        ):
            helper(False, True)
        with self.assertRaisesRegex(RuntimeError, "exact FBX and XML"):
            helper(True, True, export_fbx=True, export_xml=False)

    def test_fresh_verification_result_proves_zero_normal_attempts_and_exact_files(self):
        marker = "SPEEDTREE_FRESH_VERIFICATION_EXPORT_SEALED=1"
        helper = load_job_functions(
            ["validate_fresh_verification_export_result"],
            {
                "hashlib": hashlib,
                "FRESH_VERIFICATION_SEALED_MARKER": marker,
            },
        )["validate_fresh_verification_export_result"]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fbx = root / "fbx" / "tree.fbx"
            stmat = fbx.with_suffix(".stmat")
            receipt = root / "fbx" / "tree.speedtree_native_receipt.json"
            xml = root / "xml" / "tree.xml"
            for path, payload in (
                (fbx, b"exact-fbx"),
                (stmat, b"exact-stmat"),
                (receipt, b'{"exact":true}'),
                (xml, b"<SpeedTree />"),
            ):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(payload)

            def record(path):
                return {
                    "path": str(path.resolve()),
                    "size": path.stat().st_size,
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                }

            attempts = [{"attempt": 1, "returncode": 0}]

            def export_row(path):
                return {
                    "path": str(path.resolve()),
                    "exists": True,
                    "cache_hit": False,
                    "force_reexport_requested": True,
                    "verification_only": True,
                    "bundled_process": True,
                    "bundle_fallback": False,
                    "stdout": marker,
                    "export_attempts": attempts,
                }

            evidence = {
                "status": "sealed",
                "policy": "fresh_verification_only_as_sole_export_v1",
                "explicit_opt_in_required": True,
                "force_reexport": True,
                "collision_prune_bundle_attempt_count": 0,
                "verification_bundle_attempt_count": 1,
                "independent_fallback_attempt_count": 0,
                "launcher_sealed_completion": {
                    "status": "observed",
                    "marker": marker,
                },
                "native_receipt": str(receipt.resolve()),
                "sealed_artifacts": {
                    "fbx": [record(fbx), record(stmat), record(receipt)],
                    "xml": [record(xml)],
                },
            }
            result = {
                "force_reexport_requested": True,
                "native_receipt": str(receipt.resolve()),
                "fresh_verification_only_export": evidence,
                "exports": {
                    "fbx": export_row(fbx),
                    "xml": export_row(xml),
                },
            }

            self.assertIs(helper(result), evidence)
            fbx.write_bytes(b"drifted-fbx")
            with self.assertRaisesRegex(RuntimeError, "artifact drifted"):
                helper(result)

    def test_fresh_verification_result_fails_without_launcher_marker(self):
        marker = "SPEEDTREE_FRESH_VERIFICATION_EXPORT_SEALED=1"
        helper = load_job_functions(
            ["validate_fresh_verification_export_result"],
            {
                "hashlib": hashlib,
                "FRESH_VERIFICATION_SEALED_MARKER": marker,
            },
        )["validate_fresh_verification_export_result"]
        result = {
            "force_reexport_requested": True,
            "fresh_verification_only_export": {
                "status": "sealed",
                "policy": "fresh_verification_only_as_sole_export_v1",
                "explicit_opt_in_required": True,
                "force_reexport": True,
                "collision_prune_bundle_attempt_count": 0,
                "verification_bundle_attempt_count": 1,
                "independent_fallback_attempt_count": 0,
                "launcher_sealed_completion": {"status": "missing"},
            },
        }

        with self.assertRaisesRegex(RuntimeError, "launcher-sealed"):
            helper(result)

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

    def test_provider_no_owner_receipt_mode_is_fail_closed_and_distinct(self):
        helpers = load_job_functions([
            "is_cluster_normalization_spm",
            "validate_cluster_job_mode_arguments",
        ])
        helper = helpers["validate_cluster_job_mode_arguments"]
        provider = Path("C:/trees/tree/cluster/SK_branch_01.spm")

        helper(False, "", True, provider)
        with self.assertRaisesRegex(
            RuntimeError,
            "cannot be combined with --cluster-assembly-contract",
        ):
            helper(False, "live_assembly.json", True, provider)
        with self.assertRaisesRegex(
            RuntimeError,
            "cannot be combined with --cluster-source-build-only",
        ):
            helper(True, "", True, provider)
        with self.assertRaisesRegex(
            RuntimeError,
            "requires a canonical Cluster dependency-provider SPM",
        ):
            helper(
                False,
                "",
                True,
                Path("C:/trees/tree/SK_tree_01.spm"),
            )

    def test_provider_no_owner_receipt_never_calls_global_resolution(self):
        calls = []

        def resolve(*args, **kwargs):
            calls.append((args, kwargs))
            return Path("C:/receipt.json"), {"policy": "resolved"}

        helper = load_job_functions(
            ["resolve_job_cluster_receipt_path"],
            {"resolve_cluster_receipt_path": resolve},
        )["resolve_job_cluster_receipt_path"]
        provider = Path("C:/trees/tree/cluster/SK_branch_01.spm")

        path, evidence = helper(
            provider,
            "material.json",
            provider_no_owner_receipt=True,
        )

        self.assertIsNone(path)
        self.assertEqual(calls, [])
        self.assertEqual(
            evidence["policy"],
            "fleet_provider_dependency_no_owner_receipt",
        )
        self.assertEqual(
            evidence["skipped_global_discovery"],
            {
                "status": "not_run",
                "operation": "cluster_assembly_receipt_resolution",
                "authority": "cluster_fleet_provider_dependency_v1",
                "reason": (
                    "provider invocation has no root-owner receipt contract"
                ),
                "candidate_files_read": 0,
            },
        )

        source_path, source_evidence = helper(
            provider,
            "material.json",
            cluster_source_build_only=True,
        )
        self.assertIsNone(source_path)
        self.assertEqual(
            source_evidence["policy"],
            "cluster_source_provider_no_owner_receipt",
        )
        self.assertNotIn("skipped_global_discovery", source_evidence)
        self.assertEqual(calls, [])

        resolved_path, resolved_evidence = helper(
            provider,
            "live.json",
            require_embedded_live_audit=True,
        )
        self.assertEqual(resolved_path, Path("C:/receipt.json"))
        self.assertEqual(resolved_evidence, {"policy": "resolved"})
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], (provider, "live.json"))
        self.assertEqual(
            calls[0][1],
            {
                "include_resolution": True,
                "require_embedded_live_audit": True,
            },
        )

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
