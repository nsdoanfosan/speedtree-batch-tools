import ast
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SK_BATCH_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = SK_BATCH_DIR.parent
sys.path.insert(0, str(SK_BATCH_DIR))

from code_compile_gate import (  # noqa: E402
    CompileGateError,
    GUI_PATH,
    PUSH_JOB_PATH,
    PRODUCTION_SOURCE_MANIFEST_VERSION,
    _compile_repository_sources,
    compile_repository_sources,
    production_source_manifest,
    production_source_revision_state,
    run_gate,
    validate_gui_contracts,
    validate_push_job_contracts,
    validate_production_source_manifest,
    validate_production_source_revision_report,
)


def gui_source():
    return GUI_PATH.read_text(encoding="utf-8")


class RenameMethodCall(ast.NodeTransformer):
    def __init__(self, old, new):
        self.old = old
        self.new = new

    def visit_Call(self, node):
        self.generic_visit(node)
        if isinstance(node.func, ast.Attribute) and node.func.attr == self.old:
            node.func.attr = self.new
        return node


class ReplaceFunctionReturn(ast.NodeTransformer):
    def __init__(self, function_name, replacement):
        self.function_name = function_name
        self.replacement = replacement

    def visit_FunctionDef(self, node):
        self.generic_visit(node)
        if node.name == self.function_name:
            node.body = [ast.Return(value=copy_ast(self.replacement))]
        return node


def copy_ast(node):
    return ast.parse(ast.unparse(node), mode="eval").body


class CodeCompileGateTests(unittest.TestCase):
    def test_current_repository_passes_without_importing_runtime_modules(self):
        result = run_gate(REPO_ROOT, GUI_PATH)
        self.assertGreater(result.source_count, 0)
        self.assertEqual(result.contract_count, 4)
        self.assertEqual(
            result.source_count,
            result.production_source_manifest.source_count,
        )
        self.assertEqual(
            len(result.production_source_manifest.content_hash),
            64,
        )
        self.assertGreaterEqual(result.elapsed_seconds, 0.0)

    def test_compile_scope_ignores_diagnostic_work_but_checks_production(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "app.py").write_text("answer = 42\n", encoding="utf-8")
            work = root / "work"
            work.mkdir()
            (work / "diagnostic.py").write_text(
                "def broken(:\n",
                encoding="utf-8",
            )
            self.assertEqual(compile_repository_sources(root), 1)

            (root / "broken.py").write_text(
                "def broken(:\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                CompileGateError,
                "Python compile failed: broken.py",
            ):
                compile_repository_sources(root)

    def test_production_source_manifest_hashes_exact_compile_scope(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            app = root / "app.py"
            app.write_text("answer = 42\n", encoding="utf-8")
            work = root / "work"
            work.mkdir()
            (work / "diagnostic.py").write_text(
                "answer = 99\n",
                encoding="utf-8",
            )

            first = production_source_manifest(root)
            self.assertEqual(
                first.schema_version,
                PRODUCTION_SOURCE_MANIFEST_VERSION,
            )
            self.assertEqual(first.source_count, 1)
            self.assertEqual(
                [record.path for record in first.files],
                ["app.py"],
            )
            self.assertEqual(first.files[0].size, app.stat().st_size)
            original_mtime = app.stat().st_mtime_ns

            app.write_text("answer = 43\n", encoding="utf-8")
            os.utime(app, ns=(original_mtime, original_mtime))
            second = production_source_manifest(root)
            self.assertNotEqual(first.content_hash, second.content_hash)
            self.assertNotEqual(first.files[0].sha256, second.files[0].sha256)

    def test_nested_worktree_and_git_ignored_sources_are_not_production(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            try:
                subprocess.run(
                    ["git", "init", "--quiet", str(root)],
                    check=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except (OSError, subprocess.CalledProcessError) as exc:
                self.skipTest(f"Git fixture setup unavailable: {exc}")

            app = root / "app.py"
            app.write_text("answer = 42\n", encoding="utf-8")
            subprocess.run(
                ["git", "-C", str(root), "add", "app.py"],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

            claude_worktree = (
                root
                / ".claude"
                / "worktrees"
                / "sanitized-ephemeral"
            )
            claude_worktree.mkdir(parents=True)
            claude_source = claude_worktree / "worker.py"
            claude_source.write_text("answer = 1\n", encoding="utf-8")

            nested_vcs_root = root / "tool-cache" / "sanitized-worktree"
            nested_vcs_root.mkdir(parents=True)
            (nested_vcs_root / ".git").write_text(
                "gitdir: sanitized-git-dir\n",
                encoding="utf-8",
            )
            nested_source = nested_vcs_root / "helper.pyw"
            nested_source.write_text("answer = 2\n", encoding="utf-8")

            ignored_root = root / "ignored-helper-root"
            ignored_root.mkdir()
            ignored_source = ignored_root / "ignored.py"
            ignored_source.write_text("answer = 3\n", encoding="utf-8")
            exclude = root / ".git" / "info" / "exclude"
            with exclude.open("a", encoding="utf-8") as handle:
                handle.write("\nignored-helper-root/\n")

            started = production_source_manifest(root)
            compiled = _compile_repository_sources(root)
            self.assertEqual(started, compiled)
            self.assertEqual(started.source_count, 1)
            self.assertEqual(
                [record.path for record in started.files],
                ["app.py"],
            )

            claude_source.write_text("def broken(:\n", encoding="utf-8")
            nested_source.write_text("def broken(:\n", encoding="utf-8")
            ignored_source.write_text("def broken(:\n", encoding="utf-8")
            after_nested_mutation = production_source_manifest(root)
            self.assertEqual(started, after_nested_mutation)
            self.assertEqual(compile_repository_sources(root), 1)
            self.assertEqual(
                validate_production_source_manifest(
                    started,
                    after_nested_mutation,
                    label="Parent production source",
                ),
                started,
            )

            app.write_text("answer = 43\n", encoding="utf-8")
            changed_production = production_source_manifest(root)
            self.assertNotEqual(
                started.content_hash,
                changed_production.content_hash,
            )
            with self.assertRaisesRegex(
                CompileGateError,
                "Parent production source revision mismatch",
            ):
                validate_production_source_manifest(
                    started,
                    changed_production,
                    label="Parent production source",
                )

    def test_production_source_revision_report_requires_exact_hash(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            app = root / "app.py"
            app.write_text("answer = 42\n", encoding="utf-8")
            started = production_source_manifest(root)
            state = production_source_revision_state(
                started.content_hash,
                started,
                started,
            )
            report = {"production_source_revision": state}
            validated = validate_production_source_revision_report(
                report,
                started,
            )
            self.assertEqual(
                validated["started"]["content_hash"],
                started.content_hash,
            )

            app.write_text("answer = 43\n", encoding="utf-8")
            finished = production_source_manifest(root)
            report["production_source_revision"] = (
                production_source_revision_state(
                    started.content_hash,
                    started,
                    finished,
                )
            )
            with self.assertRaisesRegex(
                CompileGateError,
                "Child-finish production source revision mismatch",
            ):
                validate_production_source_revision_report(
                    report,
                    started,
                )

    def test_false_child_revision_assertions_report_full_restart_details(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "app.py"
            source.write_text("answer = 42\n", encoding="utf-8")
            expected = production_source_manifest(root)
            state = production_source_revision_state(
                expected.content_hash,
                expected,
                expected,
            )
            state["matches_expected"] = False
            state["stable"] = False

            with self.assertRaises(CompileGateError) as raised:
                validate_production_source_revision_report(
                    {"production_source_revision": state},
                    expected,
                )

            details = raised.exception.details
            self.assertEqual(details["route"], "code_revision_restart_required")
            self.assertEqual(details["status"], "revision_assertion_mismatch")
            self.assertEqual(details["expected_revision"], expected.content_hash)
            self.assertEqual(details["actual_revision"], expected.content_hash)
            self.assertEqual(details["changed_paths"], [])
            self.assertEqual(
                details["assertion_deltas"],
                {
                    "matches_expected": {"expected": True, "actual": False},
                    "stable": {"expected": True, "actual": False},
                },
            )
            message = str(raised.exception)
            self.assertIn("exact changed paths", message)
            self.assertIn(expected.content_hash, message)

    def test_child_metadata_mismatch_preserves_reported_actual_revision(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "app.py"
            source.write_text("answer = 42\n", encoding="utf-8")
            expected = production_source_manifest(root)
            reported_revision = "f" * 64
            state = production_source_revision_state(
                expected.content_hash,
                expected,
                expected,
            )
            state["expected_content_hash"] = reported_revision

            with self.assertRaises(CompileGateError) as raised:
                validate_production_source_revision_report(
                    {"production_source_revision": state},
                    expected,
                )

            details = raised.exception.details
            self.assertEqual(details["route"], "code_revision_restart_required")
            self.assertEqual(details["status"], "revision_metadata_mismatch")
            self.assertEqual(details["expected_revision"], expected.content_hash)
            self.assertEqual(details["actual_revision"], reported_revision)
            self.assertEqual(
                details["reported_expected_revision"],
                reported_revision,
            )
            self.assertEqual(
                details["reported_started_revision"],
                expected.content_hash,
            )
            message = str(raised.exception)
            self.assertIn(f"actual {reported_revision}", message)
            self.assertNotIn(
                f"expected {expected.content_hash}, actual {expected.content_hash}",
                message,
            )

    def test_revision_mismatch_reports_every_exact_path_and_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = [root / f"source_{index}.py" for index in range(7)]
            for index, path in enumerate(paths):
                path.write_text(
                    f"revision = {index}\n",
                    encoding="utf-8",
                )
            expected = production_source_manifest(root)

            paths[0].unlink()
            for index, path in enumerate(paths[1:6], start=1):
                path.write_text(
                    f"revision = {index + 100}\n",
                    encoding="utf-8",
                )
            added = root / "source_7.py"
            added.write_text("revision = 7\n", encoding="utf-8")
            actual = production_source_manifest(root)

            with self.assertRaises(CompileGateError) as raised:
                validate_production_source_manifest(
                    expected,
                    actual,
                    label="Intentional production source",
                )

            details = raised.exception.details
            self.assertEqual(
                details["route"],
                "code_revision_restart_required",
            )
            self.assertEqual(
                details["expected_revision"],
                expected.content_hash,
            )
            self.assertEqual(
                details["actual_revision"],
                actual.content_hash,
            )
            self.assertEqual(len(details["changed_paths"]), 7)
            by_path = {
                row["path"]: row for row in details["changed_paths"]
            }
            self.assertEqual(by_path["source_0.py"]["status"], "removed")
            self.assertIsNotNone(by_path["source_0.py"]["expected"])
            self.assertIsNone(by_path["source_0.py"]["actual"])
            self.assertEqual(by_path["source_7.py"]["status"], "added")
            self.assertIsNone(by_path["source_7.py"]["expected"])
            self.assertIsNotNone(by_path["source_7.py"]["actual"])
            for index in range(1, 6):
                row = by_path[f"source_{index}.py"]
                self.assertEqual(row["status"], "modified")
                self.assertEqual(row["expected"]["path"], row["path"])
                self.assertEqual(row["actual"]["path"], row["path"])
                self.assertNotEqual(
                    row["expected"]["sha256"],
                    row["actual"]["sha256"],
                )
            rendered = str(raised.exception)
            for path in by_path:
                self.assertIn(path, rendered)
            self.assertNotIn(" more", rendered)

    def test_owner_atlas_guard_regression_fails_at_compile_gate(self):
        source = gui_source()
        source = source.replace(
            "if not should_refresh_canonical_atlas_manifests(spm):",
            "if False:",
            1,
        )
        with self.assertRaisesRegex(
            CompileGateError,
            "non-Cluster rows must return before",
        ):
            validate_gui_contracts(source)

    def test_nested_unreachable_owner_guard_return_fails_at_compile_gate(self):
        source = gui_source()
        guard_start = source.index(
            "        if not should_refresh_canonical_atlas_manifests(spm):"
        )
        guard_end = source.index("        try:\n", guard_start)
        guard_lines = source[guard_start:guard_end].splitlines(keepends=True)
        nested_guard = (
            guard_lines[0]
            + "            if False:\n"
            + "".join("    " + line for line in guard_lines[1:])
        )
        source = source[:guard_start] + nested_guard + source[guard_end:]
        with self.assertRaisesRegex(
            CompileGateError,
            "non-Cluster rows must return before",
        ):
            validate_gui_contracts(source)

    def test_non_cluster_bone_calibration_regression_fails_at_compile_gate(self):
        module = ast.parse(gui_source())
        changed = ReplaceFunctionReturn(
            "should_calibrate_spm",
            ast.Constant(value=True),
        ).visit(module)
        ast.fix_missing_locations(changed)
        with self.assertRaisesRegex(
            CompileGateError,
            "Bone calibration contract failed",
        ):
            validate_gui_contracts(ast.unparse(changed))

    def test_push_reaudit_regression_fails_at_compile_gate(self):
        module = ast.parse(gui_source())
        changed = RenameMethodCall(
            "_assembly_stage_contract",
            "_assembly_stage_contract_disabled",
        ).visit(module)
        ast.fix_missing_locations(changed)
        with self.assertRaisesRegex(
            CompileGateError,
            "Push does not read the job-scoped Repair result",
        ):
            validate_gui_contracts(ast.unparse(changed))

    def test_push_contract_values_must_be_consumed(self):
        source = gui_source()
        source = source.replace(
            'ok = bool(repair_contract["ready"])',
            "ok = False",
            1,
        ).replace(
            'why = str(repair_contract["reason"])',
            'why = "ignored"',
            1,
        )
        with self.assertRaisesRegex(
            CompileGateError,
            "does not consume Repair ready/reason values",
        ):
            validate_gui_contracts(source)

    def test_obsolete_same_generation_evidence_guard_fails_compile_gate(self):
        changed = gui_source().replace(
            "class App:",
            "class App:\n"
            "    def _validate_assembly_stage_contract(self):\n"
            "        return None\n",
            1,
        )
        with self.assertRaisesRegex(
            CompileGateError,
            "Obsolete Repair-to-Push evidence guard returned",
        ):
            validate_gui_contracts(changed)

    def test_push_worker_rejects_obsolete_evidence_cli(self):
        source = PUSH_JOB_PATH.read_text(encoding="utf-8").replace(
            "    return parser.parse_args(argv)",
            '    parser.add_argument("--repair-evidence")\n'
            "    return parser.parse_args(argv)",
            1,
        )
        with self.assertRaisesRegex(
            CompileGateError,
            "obsolete Repair evidence CLI",
        ):
            validate_push_job_contracts(source)

    def test_runtime_asset_wave_compiler_regression_fails_at_compile_gate(self):
        source = gui_source().replace(
            "class App:",
            "class App:\n"
            "    def _compile_blender_wave(self):\n"
            "        return None\n",
            1,
        )
        with self.assertRaisesRegex(
            CompileGateError,
            "Runtime asset-wave compiler is forbidden",
        ):
            validate_gui_contracts(source)

    def test_duplicate_assembly_status_validation_fails_at_compile_gate(self):
        original = gui_source()
        marker = """\
                assembly_state = self._cluster_assembly_inputs_current(
                    spm,
                    dependency_contract_out=assembly_dependency_contract,
                )
"""
        source = original.replace(
            marker,
            marker
            + "                self._cluster_assembly_inputs_current(spm)\n",
            1,
        )
        self.assertNotEqual(source, original)
        with self.assertRaisesRegex(
            CompileGateError,
            "must be validated exactly once",
        ):
            validate_gui_contracts(source)

    def test_blender_job_direct_repair_report_read_fails_at_compile_gate(self):
        source = gui_source().replace(
            "    def _job_blender(self, iid, spm, item):\n",
            "    def _job_blender(self, iid, spm, item):\n"
            "        _read_assembly_pipeline_json(Path('repair.json'))\n",
            1,
        )
        with self.assertRaisesRegex(
            CompileGateError,
            "reuse the Repair report projection",
        ):
            validate_gui_contracts(source)

    def test_pipeline_contract_cleanup_regression_fails_at_compile_gate(self):
        source = gui_source()
        helper_start = source.index("    def _run_full_pipeline_stages(")
        cleanup_line = '                "_active_assembly_stage_contracts",\n'
        cleanup_start = source.rindex(cleanup_line, 0, helper_start)
        source = (
            source[:cleanup_start]
            + source[cleanup_start + len(cleanup_line):]
        )
        with self.assertRaisesRegex(
            CompileGateError,
            "does not clear its job-scoped Repair result",
        ):
            validate_gui_contracts(source)


if __name__ == "__main__":
    unittest.main()
