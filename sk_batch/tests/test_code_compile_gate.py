import ast
import os
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
    compile_repository_sources,
    production_source_manifest,
    production_source_revision_state,
    run_gate,
    validate_gui_contracts,
    validate_push_job_contracts,
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
            "_repair_stage_contract",
            "_repair_stage_contract_disabled",
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
            '                    ok = bool(repair_contract["ready"])\n'
            '                    why = str(repair_contract["reason"])\n',
            '                    ok = False\n'
            '                    why = "ignored"\n',
            1,
        )
        with self.assertRaisesRegex(
            CompileGateError,
            "does not consume Repair ready/reason values",
        ):
            validate_gui_contracts(source)

    def test_same_generation_evidence_validation_is_a_compile_contract(self):
        module = ast.parse(gui_source())
        changed = RenameMethodCall(
            "_validate_repair_stage_contract",
            "_validate_repair_stage_contract_disabled",
        ).visit(module)
        ast.fix_missing_locations(changed)
        with self.assertRaisesRegex(
            CompileGateError,
            "does not validate same-generation evidence",
        ):
            validate_gui_contracts(ast.unparse(changed))

    def test_push_worker_evidence_cli_is_static_compile_contract(self):
        source = PUSH_JOB_PATH.read_text(encoding="utf-8").replace(
            'parser.add_argument("--repair-evidence")',
            'parser.add_argument("--repair-evidence-disabled")',
            1,
        )
        with self.assertRaisesRegex(
            CompileGateError,
            "no --repair-evidence contract",
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
            "        _read_repair_pipeline_json(Path('repair.json'))\n",
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
        cleanup_line = '                "_active_repair_stage_contracts",\n'
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
