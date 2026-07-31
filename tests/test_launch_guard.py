"""A pythonw GUI that dies during import must not look like a no-op launch."""
import os
import runpy
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


REPO_DIR = Path(__file__).resolve().parents[1]
GUARD = REPO_DIR / "launch_guard.pyw"


def run_guard(*args, cwd=None, error_log=None):
    if error_log is None:
        with tempfile.TemporaryDirectory() as tmp:
            return run_guard(
                *args,
                cwd=cwd,
                error_log=Path(tmp) / "launch-errors.log",
            )
    env = os.environ.copy()
    env["SPEEDTREE_BATCH_TOOLS_NO_DIALOG"] = "1"
    env["SPEEDTREE_BATCH_TOOLS_ERROR_LOG"] = str(error_log)
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


def load_guard_module():
    return runpy.run_path(
        str(GUARD),
        run_name="_speedtree_batch_launch_guard_under_test",
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
            log = Path(tmp) / "launch-errors.log"
            repo_log = REPO_DIR / "speedtree_batch_tools_error.log"
            before = repo_log.stat().st_size if repo_log.is_file() else 0

            result = run_guard(broken, error_log=log)

            self.assertEqual(result.returncode, 1)
            self.assertIn("tkinter is unavailable", result.stderr)
            self.assertTrue(log.is_file())
            appended = log.read_text(encoding="utf-8", errors="replace")
            self.assertIn("broken_gui.pyw", appended)
            self.assertIn("RuntimeError: tkinter is unavailable", appended)
            after = repo_log.stat().st_size if repo_log.is_file() else 0
            self.assertEqual(after, before)

    def test_error_log_rotation_has_a_fixed_total_bound(self):
        module = load_guard_module()
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "launch-errors.log"
            globals_patch = {
                "ERROR_LOG": log,
                "ERROR_LOG_MAX_BYTES": 300,
                "ERROR_LOG_BACKUP_COUNT": 2,
            }
            with mock.patch.dict(
                module["record_error"].__globals__,
                globals_patch,
            ):
                for index in range(20):
                    self.assertTrue(
                        module["record_error"](
                            f"failure-{index}",
                            "diagnostic " + ("x" * 80),
                        )
                    )

            retained = sorted(Path(tmp).glob("launch-errors.log*"))
            self.assertLessEqual(len(retained), 3)
            self.assertTrue(all(path.stat().st_size <= 300 for path in retained))
            self.assertIn(
                "failure-19",
                log.read_text(encoding="utf-8", errors="replace"),
            )

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

    def test_integrated_and_direct_sk_batch_launches_run_compile_gate_first(self):
        for target in (
            REPO_DIR / "speedtree_batch_tools_gui.pyw",
            REPO_DIR / "sk_batch" / "sk_batch_gui.pyw",
        ):
            with self.subTest(target=target.name):
                module = load_guard_module()
                calls = []

                def fake_run_path(path, run_name):
                    calls.append((Path(path), run_name))
                    if Path(path) == module["CODE_COMPILE_GATE"]:
                        return {"run_gate": lambda: calls.append(("gate", "ran"))}
                    return {}

                with mock.patch.object(
                    module["runpy"], "run_path", side_effect=fake_run_path
                ):
                    result = module["main"](["launch_guard.pyw", str(target)])

                self.assertEqual(result, 0)
                self.assertEqual(calls[0][0], module["CODE_COMPILE_GATE"])
                self.assertEqual(calls[1], ("gate", "ran"))
                self.assertEqual(calls[2][0], target)

    def test_non_sk_batch_gui_skips_compile_gate(self):
        module = load_guard_module()
        target = REPO_DIR / "pcg_st9_texture_batch" / "pcg_texture_gui.pyw"
        with mock.patch.object(module["runpy"], "run_path", return_value={}) as run:
            result = module["main"](["launch_guard.pyw", str(target)])

        self.assertEqual(result, 0)
        run.assert_called_once_with(str(target), run_name="__main__")

    def test_compile_gate_failure_uses_existing_launch_error_reporter(self):
        module = load_guard_module()
        target = REPO_DIR / "sk_batch" / "sk_batch_gui.pyw"
        failure = RuntimeError("contract regression")
        with mock.patch.object(
            module["runpy"],
            "run_path",
            return_value={"run_gate": mock.Mock(side_effect=failure)},
        ), mock.patch.dict(
            module["main"].__globals__,
            {"report": (reporter := mock.Mock())},
        ):
            result = module["main"](["launch_guard.pyw", str(target)])

        self.assertEqual(result, 1)
        reporter.assert_called_once()
        self.assertIn("코드 컴파일 검사를 통과하지 못했습니다", reporter.call_args.args[1])
        self.assertIn("RuntimeError: contract regression", reporter.call_args.args[2])


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
