"""A pythonw GUI that dies during import must not look like a no-op launch."""
import os
import multiprocessing
import runpy
import subprocess
import sys
import tempfile
import time
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


def _record_error_process(
    guard_path,
    log_path,
    max_bytes,
    backup_count,
    marker,
    start,
    output,
    active_rotators,
    max_active_rotators,
):
    module = runpy.run_path(
        str(guard_path),
        run_name=f"_launch_guard_process_{os.getpid()}",
    )
    globals_map = module["record_error"].__globals__
    globals_map["ERROR_LOG"] = Path(log_path)
    globals_map["ERROR_LOG_MAX_BYTES"] = int(max_bytes)
    globals_map["ERROR_LOG_BACKUP_COUNT"] = int(backup_count)
    original_rotate = globals_map["_rotate_error_log_if_needed"]

    def observed_rotate(incoming_bytes):
        with active_rotators.get_lock():
            active_rotators.value += 1
            current = active_rotators.value
        with max_active_rotators.get_lock():
            max_active_rotators.value = max(
                max_active_rotators.value,
                current,
            )
        try:
            # Widen the exact old stat->rotate->append race window.  With the
            # path mutex this probe is still entered by only one process.
            time.sleep(0.08)
            return original_rotate(incoming_bytes)
        finally:
            with active_rotators.get_lock():
                active_rotators.value -= 1

    globals_map["_rotate_error_log_if_needed"] = observed_rotate
    start.wait(10)
    try:
        ok = module["record_error"](
            marker,
            f"marker={marker} " + ("x" * 120),
        )
        output.put({"marker": marker, "ok": ok})
    except BaseException as exc:
        output.put({"marker": marker, "ok": False, "error": repr(exc)})


class LaunchGuardTests(unittest.TestCase):
    def test_collision_cli_preflight_builds_and_diagnoses_without_a_console(self):
        module = load_guard_module()
        target = REPO_DIR / "sk_batch" / "sk_batch_gui.pyw"
        completed = [
            subprocess.CompletedProcess(["powershell.exe"], 0, "built", ""),
            subprocess.CompletedProcess(
                [str(module["COLLISION_CLI"]), "--diagnose"],
                0,
                module["COLLISION_CLI_CONTRACT"] + "\n",
                "",
            ),
        ]
        calls = []

        def run(command, **kwargs):
            calls.append((list(command), dict(kwargs)))
            return completed.pop(0)

        with mock.patch.dict(
            module["run_collision_cli_preflight"].__globals__,
            {"owned_run": run},
        ), mock.patch.dict(os.environ, {}, clear=False):
            result = module["run_collision_cli_preflight"](target)
            observed_cli = os.environ["SPEEDTREE_COLLISION_CLI_EXE"]

        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][0][0], "powershell.exe")
        self.assertIn("-WindowStyle", calls[0][0])
        self.assertIn("Hidden", calls[0][0])
        self.assertEqual(calls[1][0], [str(module["COLLISION_CLI"]), "--diagnose"])
        self.assertEqual(result["contract"], module["COLLISION_CLI_CONTRACT"])
        self.assertEqual(observed_cli, str(module["COLLISION_CLI"]))

    def test_persistent_session_host_retries_early_exit_then_succeeds(self):
        module = load_guard_module()
        target = REPO_DIR / "sk_batch" / "sk_batch_gui.pyw"
        replacement = {"process": object(), "replacement": True}
        start_once = mock.Mock(
            side_effect=[
                RuntimeError("host exited before ready"),
                replacement,
            ]
        )
        with mock.patch.dict(
            os.environ,
            {
                "SPEEDTREE_COLLISION_PERSISTENT": "1",
                "SPEEDTREE_COLLISION_ISOLATED_WINDOW": "0",
                "SPEEDTREE_COLLISION_SESSION_START_ATTEMPTS": "3",
            },
        ), mock.patch.dict(
            module["start_speedtree_session_host"].__globals__,
            {
                "_start_speedtree_session_host_once": start_once,
                "record_error": (record_error := mock.Mock()),
            },
        ), mock.patch.object(module["time"], "sleep") as sleep:
            result = module["start_speedtree_session_host"](target)

        self.assertIs(result, replacement)
        self.assertEqual(start_once.call_count, 2)
        record_error.assert_called_once()
        sleep.assert_called_once_with(
            module["SPEEDTREE_SESSION_RETRY_DELAY_SECONDS"]
        )

    def test_persistent_session_host_retry_is_bounded(self):
        module = load_guard_module()
        target = REPO_DIR / "sk_batch" / "sk_batch_gui.pyw"
        start_once = mock.Mock(side_effect=RuntimeError("unexpected close"))
        with mock.patch.dict(
            os.environ,
            {
                "SPEEDTREE_COLLISION_PERSISTENT": "1",
                "SPEEDTREE_COLLISION_ISOLATED_WINDOW": "0",
                "SPEEDTREE_COLLISION_SESSION_START_ATTEMPTS": "3",
            },
        ), mock.patch.dict(
            module["start_speedtree_session_host"].__globals__,
            {
                "_start_speedtree_session_host_once": start_once,
                "record_error": mock.Mock(),
            },
        ), mock.patch.object(module["time"], "sleep"):
            with self.assertRaisesRegex(RuntimeError, "after 3 attempts"):
                module["start_speedtree_session_host"](target)

        self.assertEqual(start_once.call_count, 3)

    def test_private_desktop_mode_does_not_start_a_persistent_host(self):
        module = load_guard_module()
        target = REPO_DIR / "sk_batch" / "sk_batch_gui.pyw"
        with mock.patch.dict(
            os.environ,
            {
                "SPEEDTREE_COLLISION_PERSISTENT": "1",
                "SPEEDTREE_COLLISION_ISOLATED_WINDOW": "1",
            },
        ), mock.patch.dict(
            module["start_speedtree_session_host"].__globals__,
            {"_start_speedtree_session_host_once": (start_once := mock.Mock())},
        ):
            result = module["start_speedtree_session_host"](target)

        self.assertIsNone(result)
        start_once.assert_not_called()

    def test_guard_exists_and_every_launcher_routes_through_it(self):
        self.assertTrue(GUARD.is_file(), f"missing launch guard: {GUARD}")
        headless_launchers = {
            "SK_Affected_Headless_Refresh.bat",
            "SK_Cluster_Fleet_Push.bat",
            "SK_Exact_Push.bat",
        }
        launchers = sorted(REPO_DIR.glob("*.bat")) + sorted(
            REPO_DIR.glob("*/*.bat")
        )
        self.assertTrue(launchers, "no .bat launchers were found")
        for launcher in launchers:
            text = launcher.read_text(encoding="utf-8", errors="replace")
            with self.subTest(launcher=launcher.name):
                if launcher.name in headless_launchers:
                    self.assertIn("python", text.casefold())
                    self.assertNotRegex(text, r"(?im)^\s*start\b")
                    continue
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

    def test_concurrent_launch_guards_rotate_without_loss_or_oversize(self):
        context = multiprocessing.get_context("spawn")
        for iteration in range(3):
            with self.subTest(iteration=iteration), tempfile.TemporaryDirectory() as tmp:
                log = Path(tmp) / "launch-errors.log"
                max_bytes = 512
                backup_count = 4
                markers = [f"round-{iteration}-process-{index}" for index in range(8)]
                start = context.Event()
                output = context.Queue()
                active_rotators = context.Value("i", 0)
                max_active_rotators = context.Value("i", 0)
                processes = [
                    context.Process(
                        target=_record_error_process,
                        args=(
                            str(GUARD),
                            str(log),
                            max_bytes,
                            backup_count,
                            marker,
                            start,
                            output,
                            active_rotators,
                            max_active_rotators,
                        ),
                    )
                    for marker in markers
                ]
                for process in processes:
                    process.start()
                start.set()
                for process in processes:
                    process.join(15)
                    self.assertEqual(process.exitcode, 0)
                results = [output.get(timeout=2) for _ in processes]
                self.assertTrue(all(item["ok"] for item in results), results)
                self.assertEqual(max_active_rotators.value, 1)

                retained = sorted(Path(tmp).glob("launch-errors.log*"))
                self.assertLessEqual(len(retained), backup_count + 1)
                self.assertTrue(
                    all(path.stat().st_size <= max_bytes for path in retained)
                )
                combined = "\n".join(
                    path.read_text(encoding="utf-8", errors="replace")
                    for path in retained
                )
                for marker in markers:
                    self.assertIn(marker, combined)

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

    def test_system_exit_integer_codes_follow_cpython_semantics(self):
        cases = (
            ("empty", "sys.exit()\n", 0),
            ("zero", "sys.exit(0)\n", 0),
            ("three", "sys.exit(3)\n", 3),
            ("true", "sys.exit(True)\n", 1),
        )
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            for name, exit_statement, expected in cases:
                with self.subTest(name=name):
                    target = tmp_path / f"{name}_gui.pyw"
                    target.write_text(
                        "import sys\n" + exit_statement,
                        encoding="utf-8",
                    )
                    log = tmp_path / f"{name}-launch-errors.log"

                    result = run_guard(target, error_log=log)

                    self.assertEqual(result.returncode, expected)
                    self.assertEqual(result.stderr, "")
                    self.assertFalse(log.exists())

    def test_system_exit_messages_are_logged_reported_and_return_one(self):
        cases = (
            (
                "string",
                "import sys\nsys.exit('requested launch stop')\n",
                "requested launch stop",
            ),
            (
                "object",
                "import sys\n"
                "class ExitReason:\n"
                "    def __str__(self):\n"
                "        return 'structured launch stop'\n"
                "sys.exit(ExitReason())\n",
                "structured launch stop",
            ),
        )
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            for name, source, diagnostic in cases:
                with self.subTest(name=name):
                    target = tmp_path / f"{name}_gui.pyw"
                    target.write_text(source, encoding="utf-8")
                    log = tmp_path / f"{name}-launch-errors.log"

                    result = run_guard(target, error_log=log)

                    self.assertEqual(result.returncode, 1)
                    self.assertIn(diagnostic, result.stderr)
                    self.assertNotIn("ValueError", result.stderr)
                    appended = log.read_text(encoding="utf-8", errors="replace")
                    self.assertIn(target.name, appended)
                    self.assertIn(diagnostic, appended)
                    self.assertNotIn("ValueError", appended)

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
                ), mock.patch.dict(
                    module["main"].__globals__,
                    {
                        "run_collision_cli_preflight": lambda selected: calls.append(
                            ("collision", Path(selected).name)
                        ),
                        "run_startup_retention": lambda: calls.append(
                            ("retention", "ran")
                        ),
                    },
                ):
                    result = module["main"](["launch_guard.pyw", str(target)])

                self.assertEqual(result, 0)
                self.assertEqual(calls[0], ("collision", target.name))
                self.assertEqual(calls[1], ("retention", "ran"))
                self.assertEqual(calls[2][0], module["CODE_COMPILE_GATE"])
                self.assertEqual(calls[3], ("gate", "ran"))
                self.assertEqual(calls[4][0], target)

    def test_non_sk_batch_gui_skips_compile_gate(self):
        module = load_guard_module()
        target = REPO_DIR / "pcg_st9_texture_batch" / "pcg_texture_gui.pyw"
        with mock.patch.object(
            module["runpy"], "run_path", return_value={}
        ) as run, mock.patch.dict(
            module["main"].__globals__, {"run_startup_retention": mock.Mock()}
        ):
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
            {
                "report": (reporter := mock.Mock()),
                "run_collision_cli_preflight": mock.Mock(),
                "run_startup_retention": mock.Mock(),
            },
        ):
            result = module["main"](["launch_guard.pyw", str(target)])

        self.assertEqual(result, 1)
        reporter.assert_called_once()
        self.assertIn("코드 컴파일 검사를 통과하지 못했습니다", reporter.call_args.args[1])
        self.assertIn("RuntimeError: contract regression", reporter.call_args.args[2])

    def test_retention_capacity_failure_blocks_producer_before_compile_or_import(self):
        module = load_guard_module()
        target = REPO_DIR / "sk_batch" / "sk_batch_gui.pyw"
        failure = module["RetentionCapacityError"]("at hard limit")
        with mock.patch.object(module["runpy"], "run_path") as run, mock.patch.dict(
            module["main"].__globals__,
            {
                "run_collision_cli_preflight": mock.Mock(),
                "run_startup_retention": mock.Mock(side_effect=failure),
                "report": (reporter := mock.Mock()),
            },
        ):
            result = module["main"](["launch_guard.pyw", str(target)])

        self.assertEqual(result, 1)
        run.assert_not_called()
        reporter.assert_called_once()
        self.assertIn("artifact retention failed", reporter.call_args.args[1])


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
