"""A pythonw GUI that dies during import must not look like a no-op launch."""
import os
import runpy
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO_DIR = Path(__file__).resolve().parents[1]
GUARD = REPO_DIR / "launch_guard.pyw"


def run_guard(*args, cwd=None):
    env = os.environ.copy()
    env["SPEEDTREE_BATCH_TOOLS_NO_DIALOG"] = "1"
    return subprocess.run(
        [sys.executable, str(GUARD), *[str(value) for value in args]],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(cwd or REPO_DIR),
        env=env,
        timeout=120,
    )


class LaunchGuardTests(unittest.TestCase):
    def test_guard_exists_and_every_launcher_routes_through_it(self):
        self.assertTrue(GUARD.is_file(), f"missing launch guard: {GUARD}")
        launchers = sorted(REPO_DIR.glob("*.bat")) + sorted(
            REPO_DIR.glob("*/*.bat")
        )
        self.assertTrue(launchers, "no .bat launchers were found")
        for launcher in launchers:
            text = launcher.read_text(encoding="utf-8", errors="replace")
            with self.subTest(launcher=launcher.name):
                self.assertIn("launch_guard.pyw", text)
                # `start` returns before the child runs, so an errorlevel test
                # on it always passes and hides a dead GUI.
                self.assertNotRegex(
                    text,
                    r"(?im)^\s*start\b.*\r?\n\s*if\s+errorlevel",
                )

    def test_import_time_crash_is_logged_and_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            broken = Path(tmp) / "broken_gui.pyw"
            broken.write_text(
                "raise RuntimeError('tkinter is unavailable')\n", encoding="utf-8"
            )
            log = REPO_DIR / "speedtree_batch_tools_error.log"
            before = log.stat().st_size if log.is_file() else 0

            result = run_guard(broken)

            self.assertEqual(result.returncode, 1)
            self.assertIn("tkinter is unavailable", result.stderr)
            self.assertTrue(log.is_file())
            with log.open("rb") as handle:
                handle.seek(before)
                appended = handle.read().decode(
                    "utf-8",
                    errors="replace",
                )
            self.assertIn("broken_gui.pyw", appended)
            self.assertIn("RuntimeError: tkinter is unavailable", appended)

    def test_missing_target_is_reported_instead_of_silently_succeeding(self):
        result = run_guard(REPO_DIR / "no_such_gui.pyw")
        self.assertEqual(result.returncode, 2)
        self.assertIn("no_such_gui.pyw", result.stderr)

    def test_clean_exit_is_passed_through_with_gui_argv(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "ok_gui.pyw"
            target.write_text(
                "import sys\n"
                "print(sys.argv[0])\n"
                "print(sys.argv[1:])\n"
                "sys.exit(0)\n",
                encoding="utf-8",
            )
            result = run_guard(target, "--flag", "value")
            self.assertEqual(result.returncode, 0)
            self.assertIn("ok_gui.pyw", result.stdout)
            self.assertIn("['--flag', 'value']", result.stdout)

    def test_guard_runs_the_target_as_main(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "main_gui.pyw"
            target.write_text(
                "ran = False\n"
                "if __name__ == '__main__':\n"
                "    ran = True\n"
                "    print('entered main')\n",
                encoding="utf-8",
            )
            result = run_guard(target)
            self.assertEqual(result.returncode, 0)
            self.assertIn("entered main", result.stdout)


class LauncherModuleTests(unittest.TestCase):
    def test_integrated_launcher_reports_its_own_startup_failure(self):
        module = runpy.run_path(
            str(REPO_DIR / "speedtree_batch_tools_gui.pyw"),
            run_name="speedtree_batch_tools_gui_under_test",
        )
        self.assertIn("report_fatal_startup_error", module)
        self.assertIn("record_error", module)


if __name__ == "__main__":
    unittest.main()
