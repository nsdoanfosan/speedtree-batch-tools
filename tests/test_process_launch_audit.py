"""Static guardrails for the repository-wide process ownership inventory."""

from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path


REPO_DIR = Path(__file__).resolve().parents[1]
LIFECYCLE = REPO_DIR / "process_lifecycle.py"
MANUAL_HANDOFF_CALLS = {
    ("speedtree_batch_tools_gui.pyw", "external_handoff_popen"),
    ("speedtree_batch_tools_gui.pyw", "external_handoff_startfile"),
    (
        "pcg_st9_texture_batch/pcg_texture_gui.pyw",
        "external_handoff_popen",
    ),
    (
        "pcg_st9_texture_batch/stale_node_table_recovery.py",
        "external_handoff_popen",
    ),
    (
        "spm_generator_sync/spm_generator_sync_gui.pyw",
        "external_handoff_startfile",
    ),
}


def _production_sources():
    for suffix in ("*.py", "*.pyw"):
        for path in REPO_DIR.rglob(suffix):
            relative = path.relative_to(REPO_DIR)
            if path == LIFECYCLE or "tests" in relative.parts:
                continue
            yield path


def _call_name(node):
    function = node.func
    if isinstance(function, ast.Name):
        return function.id
    if isinstance(function, ast.Attribute):
        if isinstance(function.value, ast.Name):
            return f"{function.value.id}.{function.attr}"
        return function.attr
    return None


class ProcessLaunchAuditTests(unittest.TestCase):
    def test_raw_process_and_shell_launches_exist_only_in_shared_contract(self):
        forbidden = {"subprocess.run", "subprocess.Popen", "os.startfile"}
        failures = []
        for path in _production_sources():
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and _call_name(node) in forbidden:
                    failures.append(
                        f"{path.relative_to(REPO_DIR)}:{node.lineno}:"
                        f"{_call_name(node)}"
                    )
        self.assertEqual(failures, [])

    def test_windows_job_api_is_centralized(self):
        tokens = (
            "CreateJobObject",
            "AssignProcessToJobObject",
            "TerminateJobObject",
        )
        failures = []
        for path in _production_sources():
            text = path.read_text(encoding="utf-8", errors="replace")
            for token in tokens:
                if token in text:
                    failures.append(f"{path.relative_to(REPO_DIR)}:{token}")
        self.assertEqual(failures, [])

    def test_manual_handoffs_are_an_explicit_closed_inventory(self):
        observed = set()
        for path in _production_sources():
            relative = path.relative_to(REPO_DIR).as_posix()
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                name = _call_name(node)
                if name in {"external_handoff_popen", "external_handoff_startfile"}:
                    observed.add((relative, name))
        self.assertEqual(observed, MANUAL_HANDOFF_CALLS)

    def test_bat_launchers_record_source_and_document_durable_handoff(self):
        launchers = sorted(REPO_DIR.glob("*.bat")) + sorted(
            REPO_DIR.glob("*/*.bat")
        )
        self.assertEqual(len(launchers), 4)
        for launcher in launchers:
            text = launcher.read_text(encoding="utf-8", errors="replace")
            with self.subTest(launcher=launcher.name):
                self.assertIn("SPEEDTREE_BATCH_LAUNCH_SOURCE", text)
                self.assertIn("launch_guard.pyw", text)
                self.assertRegex(text, r"(?im)^\s*start\s+\"\"")
                self.assertNotRegex(text, r"(?i)taskkill")


if __name__ == "__main__":
    unittest.main()
