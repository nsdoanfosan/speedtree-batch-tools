"""Windows lifecycle acceptance tests using only sanitized Python helpers."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock


REPO_DIR = Path(__file__).resolve().parents[1]
HELPER = Path(__file__).with_name("process_lifecycle_helper.py")
GUARD = REPO_DIR / "launch_guard.pyw"
sys.path.insert(0, str(REPO_DIR))

import process_lifecycle
from process_lifecycle import (
    ProcessLifecycleError,
    WINDOWS_CREATE_BREAKAWAY_FROM_JOB,
    WINDOWS_CREATE_NO_WINDOW,
    WINDOWS_CREATE_SUSPENDED,
    _process_start_identity,
    external_handoff_popen,
    owned_popen,
    owned_run,
    process_identity_is_alive,
    recover_incomplete_receipts,
    shutdown_process_supervisor,
    start_process_supervisor,
    terminate_owned_process,
)


def _wait_for_json(path, timeout=10.0):
    deadline = time.monotonic() + float(timeout)
    while time.monotonic() < deadline:
        try:
            return json.loads(Path(path).read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            time.sleep(0.02)
    raise AssertionError(f"timed out waiting for JSON: {path}")


def _wait_identity_gone(pid, identity, timeout=10.0):
    deadline = time.monotonic() + float(timeout)
    while time.monotonic() < deadline:
        if not process_identity_is_alive(pid, identity):
            return True
        time.sleep(0.02)
    return not process_identity_is_alive(pid, identity)


def _tree_command(ready, *, mode="sleep", stop=None, seconds=60):
    command = [
        sys.executable,
        str(HELPER),
        "tree",
        "--mode",
        mode,
        "--ready",
        str(ready),
        "--seconds",
        str(seconds),
    ]
    if stop is not None:
        command.extend(["--stop", str(stop)])
    return command


def _hidden_flags():
    return getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


def _stop_exact_sanitized_process(pid, identity):
    """Last-resort cleanup for a test-owned, identity-proven process only."""

    if os.name != "nt" or not process_identity_is_alive(pid, identity):
        return
    import ctypes
    from ctypes import wintypes

    process_terminate = 0x0001
    process_query_limited_information = 0x1000
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel32.TerminateProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel32.OpenProcess(
        process_terminate | process_query_limited_information,
        False,
        int(pid),
    )
    if not handle:
        return
    try:
        class _OpenedProcess:
            _handle = handle

        if process_lifecycle._process_start_identity(_OpenedProcess()) == str(identity):
            kernel32.TerminateProcess(handle, 91)
    finally:
        kernel32.CloseHandle(handle)


@unittest.skipUnless(os.name == "nt", "Windows Job Object lifecycle contract")
class ProcessLifecycleWindowsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.receipts = self.root / "receipts"

    def tearDown(self):
        try:
            shutdown_process_supervisor(
                "test_teardown",
                terminate_grace=0.05,
                kill_grace=3.0,
            )
        finally:
            self.temporary.cleanup()

    def _start(self, source="sanitized:unittest"):
        return start_process_supervisor(source, receipt_dir=self.receipts)

    def test_normal_exit_leaves_zero_owned_descendants_and_receipt(self):
        supervisor = self._start()
        ready = self.root / "normal.json"
        stop = self.root / "normal.stop"
        completed = owned_run(
            _tree_command(ready, mode="normal", stop=stop, seconds=10),
            source="tests.normal_exit",
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=_hidden_flags(),
            timeout=15,
        )
        evidence = _wait_for_json(ready)
        self.assertEqual(completed.returncode, 0)
        self.assertTrue(
            _wait_identity_gone(
                evidence["parent_pid"], evidence["parent_start_identity"]
            )
        )
        self.assertTrue(
            _wait_identity_gone(
                evidence["grandchild_pid"],
                evidence["grandchild_start_identity"],
            )
        )
        receipt = supervisor.receipt()
        self.assertEqual(receipt["survivors"], [])
        self.assertEqual(
            receipt["owned_processes"][0]["cleanup_state"],
            "process_tree_clean",
        )

    def test_explicit_stop_kills_only_owned_parent_and_grandchild(self):
        supervisor = self._start()
        sibling = external_handoff_popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            source="tests.external_python_handoff",
            ownership="external_preexisting_fixture",
            creationflags=_hidden_flags(),
        )
        sibling_identity = _process_start_identity(sibling)
        ready = self.root / "stop.json"
        process = owned_popen(
            _tree_command(ready),
            source="tests.explicit_stop",
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=_hidden_flags(),
        )
        evidence = _wait_for_json(ready)
        try:
            result = terminate_owned_process(
                process,
                reason="explicit_stop",
                terminate_grace=0.05,
                kill_grace=3.0,
            )
            self.assertIn(result, {"explicit_stop_graceful", "explicit_stop_forced"})
            self.assertTrue(
                _wait_identity_gone(
                    evidence["parent_pid"], evidence["parent_start_identity"]
                )
            )
            self.assertTrue(
                _wait_identity_gone(
                    evidence["grandchild_pid"],
                    evidence["grandchild_start_identity"],
                )
            )
            self.assertTrue(
                process_identity_is_alive(sibling.pid, sibling_identity),
                "external/manual-handoff Python was collateral-killed",
            )
            receipt = supervisor.receipt()
            self.assertEqual(len(receipt["external_handoffs"]), 1)
            self.assertEqual(receipt["survivors"], [])
        finally:
            if sibling.poll() is None:
                sibling.terminate()
                try:
                    sibling.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    sibling.kill()
                    sibling.wait(timeout=3)

    def test_cooperative_hook_exits_tree_before_forced_job_fallback(self):
        supervisor = self._start()
        ready = self.root / "cooperative.json"
        stop = self.root / "cooperative.stop"
        process = owned_popen(
            _tree_command(ready, stop=stop),
            source="tests.cooperative_stop",
            cooperative_cancel=lambda: stop.touch(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=_hidden_flags(),
        )
        evidence = _wait_for_json(ready)
        result = terminate_owned_process(
            process,
            reason="cooperative_stop",
            terminate_grace=2.0,
            kill_grace=3.0,
        )
        self.assertEqual(result, "cooperative_stop_graceful")
        self.assertTrue(
            _wait_identity_gone(
                evidence["grandchild_pid"],
                evidence["grandchild_start_identity"],
            )
        )
        entry = supervisor.receipt()["owned_processes"][0]
        self.assertEqual(
            entry["cooperative_result"], "cooperative_stop_requested"
        )
        self.assertIsNone(entry["forced_result"])

    def test_stopping_one_concurrent_tree_never_stops_the_other(self):
        supervisor = self._start()
        ready_a = self.root / "concurrent-a.json"
        ready_b = self.root / "concurrent-b.json"
        process_a = owned_popen(
            _tree_command(ready_a),
            source="tests.concurrent.a",
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=_hidden_flags(),
        )
        process_b = owned_popen(
            _tree_command(ready_b),
            source="tests.concurrent.b",
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=_hidden_flags(),
        )
        evidence_a = _wait_for_json(ready_a)
        evidence_b = _wait_for_json(ready_b)
        terminate_owned_process(
            process_a,
            reason="stop_a",
            terminate_grace=0.02,
            kill_grace=3.0,
        )
        self.assertTrue(
            _wait_identity_gone(
                evidence_a["grandchild_pid"],
                evidence_a["grandchild_start_identity"],
            )
        )
        self.assertTrue(
            process_identity_is_alive(
                evidence_b["parent_pid"], evidence_b["parent_start_identity"]
            )
        )
        self.assertTrue(
            process_identity_is_alive(
                evidence_b["grandchild_pid"],
                evidence_b["grandchild_start_identity"],
            )
        )
        receipt = supervisor.shutdown(
            reason="session_stop",
            terminate_grace=0.02,
            kill_grace=3.0,
        )
        self.assertEqual(receipt["survivors"], [])
        self.assertTrue(
            _wait_identity_gone(
                evidence_b["grandchild_pid"],
                evidence_b["grandchild_start_identity"],
            )
        )

    def test_child_can_create_nested_jobs_inside_owned_parent_tree(self):
        self._start()
        ready = self.root / "nested.json"
        process = owned_popen(
            [
                sys.executable,
                str(HELPER),
                "nested-supervisor",
                "--ready",
                str(ready),
                "--receipt-dir",
                str(self.root / "nested-receipts"),
                "--seconds",
                "60",
            ],
            source="tests.nested_job_parent",
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=_hidden_flags(),
        )
        evidence = _wait_for_json(ready)
        terminate_owned_process(
            process,
            reason="nested_stop",
            terminate_grace=0.02,
            kill_grace=3.0,
        )
        self.assertTrue(
            _wait_identity_gone(
                evidence["parent_pid"], evidence["parent_start_identity"]
            )
        )
        self.assertTrue(
            _wait_identity_gone(
                evidence["grandchild_pid"],
                evidence["grandchild_start_identity"],
            )
        )

    def test_launch_after_shutdown_transition_is_rejected(self):
        supervisor = self._start()
        supervisor.shutdown(
            reason="close_before_launch",
            terminate_grace=0,
            kill_grace=0,
        )
        with self.assertRaises(ProcessLifecycleError) as raised:
            owned_popen(
                [sys.executable, "-c", "pass"],
                source="tests.launch_after_shutdown",
                creationflags=_hidden_flags(),
            )
        self.assertEqual(
            raised.exception.reason_token, "process_supervisor_not_running"
        )

    def test_launch_in_progress_is_registered_before_shutdown_snapshot(self):
        supervisor = self._start()
        entered_registration = threading.Event()
        release_registration = threading.Event()
        original_register = supervisor._register_owned
        launch_box = {}
        shutdown_box = {}

        def blocked_register(*args, **kwargs):
            entered_registration.set()
            if not release_registration.wait(5):
                raise RuntimeError("test did not release registration")
            return original_register(*args, **kwargs)

        def launch():
            try:
                launch_box["process"] = owned_popen(
                    [sys.executable, "-c", "import time; time.sleep(60)"],
                    source="tests.launch_shutdown_race",
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=_hidden_flags(),
                )
            except BaseException as exc:
                launch_box["error"] = exc

        def shutdown():
            try:
                shutdown_box["receipt"] = supervisor.shutdown(
                    reason="concurrent_shutdown",
                    terminate_grace=0.02,
                    kill_grace=3.0,
                )
            except BaseException as exc:
                shutdown_box["error"] = exc

        with mock.patch.object(
            supervisor, "_register_owned", side_effect=blocked_register
        ):
            launch_thread = threading.Thread(target=launch)
            launch_thread.start()
            self.assertTrue(entered_registration.wait(5))
            shutdown_thread = threading.Thread(target=shutdown)
            shutdown_thread.start()
            time.sleep(0.05)
            self.assertTrue(
                shutdown_thread.is_alive(),
                "shutdown bypassed the launch gate while child was suspended",
            )
            release_registration.set()
            launch_thread.join(8)
            shutdown_thread.join(8)

        self.assertNotIn("error", launch_box)
        self.assertNotIn("error", shutdown_box)
        self.assertFalse(launch_thread.is_alive())
        self.assertFalse(shutdown_thread.is_alive())
        launch_box["process"].wait(timeout=3)
        self.assertIsNotNone(launch_box["process"].returncode)
        receipt = shutdown_box["receipt"]
        self.assertEqual(len(receipt["owned_processes"]), 1)
        self.assertEqual(receipt["survivors"], [])

    def test_job_handle_is_non_inheritable_and_breakaway_is_forbidden(self):
        import ctypes
        from ctypes import wintypes

        job = process_lifecycle._WindowsJob()
        try:
            flags = wintypes.DWORD()
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.GetHandleInformation.argtypes = [
                wintypes.HANDLE,
                ctypes.POINTER(wintypes.DWORD),
            ]
            kernel32.GetHandleInformation.restype = wintypes.BOOL
            self.assertTrue(
                kernel32.GetHandleInformation(job._handle, ctypes.byref(flags))
            )
            self.assertEqual(flags.value & 0x00000001, 0)
            with self.assertRaises(ProcessLifecycleError) as raised:
                job.creationflags(WINDOWS_CREATE_BREAKAWAY_FROM_JOB)
            self.assertEqual(
                raised.exception.reason_token,
                "process_job_breakaway_forbidden",
            )
        finally:
            job.close()

    def test_owned_windows_launches_are_hidden_by_default(self):
        self.assertEqual(
            process_lifecycle._WindowsJob.creationflags(),
            WINDOWS_CREATE_SUSPENDED | WINDOWS_CREATE_NO_WINDOW,
        )
        requested = 0x00000200  # CREATE_NEW_PROCESS_GROUP
        self.assertEqual(
            process_lifecycle._WindowsJob.creationflags(requested),
            requested | WINDOWS_CREATE_SUSPENDED | WINDOWS_CREATE_NO_WINDOW,
        )

    def test_owned_windows_launches_forbid_visible_or_detached_consoles(self):
        for requested in (0x00000008, 0x00000010):
            with self.subTest(requested=requested):
                with self.assertRaises(ProcessLifecycleError) as raised:
                    process_lifecycle._WindowsJob.creationflags(requested)
                self.assertEqual(
                    raised.exception.reason_token,
                    "process_console_window_forbidden",
                )

    def test_windows_7_nested_job_contract_fails_before_job_creation(self):
        unsupported = mock.Mock(major=6, minor=1)
        with mock.patch.object(
            process_lifecycle.os.sys,
            "getwindowsversion",
            return_value=unsupported,
        ):
            with self.assertRaises(ProcessLifecycleError) as raised:
                process_lifecycle._WindowsJob()
        self.assertEqual(
            raised.exception.reason_token, "nested_process_jobs_unsupported"
        )

    def test_root_crash_forces_silent_grandchild_after_bounded_grace(self):
        self._start()
        ready = self.root / "crash.json"
        started = time.monotonic()
        completed = owned_run(
            _tree_command(ready, mode="root-exit"),
            source="tests.root_crash",
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=_hidden_flags(),
            timeout=10,
            owned_descendant_grace=0.05,
            owned_kill_grace=3.0,
        )
        evidence = _wait_for_json(ready)
        self.assertEqual(completed.returncode, 23)
        self.assertLess(time.monotonic() - started, 5.0)
        self.assertTrue(
            _wait_identity_gone(
                evidence["grandchild_pid"],
                evidence["grandchild_start_identity"],
            )
        )
        receipt = process_lifecycle.get_process_supervisor().receipt()
        self.assertEqual(
            receipt["owned_processes"][0]["cleanup_state"],
            "process_tree_forced_after_root_exit",
        )

    def test_forced_supervisor_termination_kills_owned_tree_not_external(self):
        sibling = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            creationflags=_hidden_flags(),
        )
        sibling_identity = _process_start_identity(sibling)
        ready = self.root / "forced-supervisor.json"
        supervisor_process = subprocess.Popen(
            [
                sys.executable,
                str(HELPER),
                "supervisor",
                "--ready",
                str(ready),
                "--receipt-dir",
                str(self.receipts),
                "--seconds",
                "60",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=_hidden_flags(),
        )
        evidence = _wait_for_json(ready)
        try:
            supervisor_process.terminate()
            supervisor_process.wait(timeout=5)
            self.assertTrue(
                _wait_identity_gone(
                    evidence["parent_pid"], evidence["parent_start_identity"]
                )
            )
            self.assertTrue(
                _wait_identity_gone(
                    evidence["grandchild_pid"],
                    evidence["grandchild_start_identity"],
                )
            )
            self.assertTrue(
                process_identity_is_alive(sibling.pid, sibling_identity),
                "unrelated Python was killed with the sanitized supervisor",
            )
            recovered = recover_incomplete_receipts(self.receipts)
            self.assertEqual(len(recovered), 1)
            forced_receipt = _wait_for_json(recovered[0])
            self.assertEqual(
                forced_receipt["status"], "recovered_forced_owner_exit"
            )
            self.assertIsNone(forced_receipt["survivors"])
            self.assertEqual(
                forced_receipt["survivor_observation"],
                "unavailable_after_owner_exit",
            )
            self.assertEqual(
                forced_receipt["owned_processes"][0]["forced_result"],
                "session_job_closed_with_owner",
            )
        finally:
            if supervisor_process.poll() is None:
                supervisor_process.kill()
                supervisor_process.wait(timeout=3)
            if sibling.poll() is None:
                sibling.terminate()
                sibling.wait(timeout=3)

    def test_repeated_stop_cycles_do_not_accumulate_survivors(self):
        supervisor = self._start()
        for index in range(3):
            ready = self.root / f"cycle-{index}.json"
            process = owned_popen(
                _tree_command(ready),
                source=f"tests.repeated_stop.{index}",
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=_hidden_flags(),
            )
            evidence = _wait_for_json(ready)
            terminate_owned_process(
                process,
                reason="cycle_stop",
                terminate_grace=0.02,
                kill_grace=3.0,
            )
            self.assertTrue(
                _wait_identity_gone(
                    evidence["grandchild_pid"],
                    evidence["grandchild_start_identity"],
                )
            )
        receipt = supervisor.shutdown(
            reason="repeated_cycles_complete",
            terminate_grace=0.02,
            kill_grace=3.0,
        )
        self.assertEqual(receipt["survivors"], [])
        self.assertEqual(len(receipt["owned_processes"]), 3)

    def test_bat_returns_only_after_durable_guard_handoff(self):
        pythonw = Path(sys.executable).with_name("pythonw.exe")
        self.assertTrue(pythonw.is_file())
        ready = self.root / "bat-ready.json"
        stop = self.root / "bat-stop"
        gui = self.root / "sanitized_gui.pyw"
        gui.write_text(
            "import subprocess,sys,time\n"
            "from pathlib import Path\n"
            f"sys.path.insert(0, {str(REPO_DIR)!r})\n"
            "from process_lifecycle import owned_popen\n"
            "ready, stop = Path(sys.argv[1]), Path(sys.argv[2])\n"
            f"owned_popen([{sys.executable!r}, {str(HELPER)!r}, 'tree', "
            "'--mode', 'sleep', '--ready', str(ready), '--seconds', '60'], "
            "source='tests.bat_guarded_gui', stdin=subprocess.DEVNULL, "
            "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, "
            f"creationflags={_hidden_flags()!r})\n"
            "while not stop.exists(): time.sleep(0.02)\n",
            encoding="utf-8",
        )
        launcher = self.root / "sanitized_launch.bat"
        launcher.write_text(
            "@echo off\n"
            "setlocal\n"
            "set \"SPEEDTREE_BATCH_LAUNCH_SOURCE=bat:sanitized\"\n"
            f"set \"SPEEDTREE_PROCESS_RECEIPT_DIR={self.receipts}\"\n"
            f"start \"\" /D \"{REPO_DIR}\" \"{pythonw}\" \"{GUARD}\" "
            f"\"{gui}\" \"{ready}\" \"{stop}\"\n"
            "exit /b 0\n",
            encoding="utf-8",
        )
        result = subprocess.run(
            ["cmd.exe", "/d", "/c", str(launcher)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0)
        evidence = _wait_for_json(ready)
        receipt_path = None
        receipt = None
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            for candidate in self.receipts.glob("*.json"):
                payload = json.loads(candidate.read_text(encoding="utf-8"))
                if payload.get("launch_source") == "bat:sanitized":
                    receipt_path, receipt = candidate, payload
                    break
            if receipt is not None:
                break
            time.sleep(0.02)
        self.assertIsNotNone(receipt, "BAT never handed off to a durable guard")
        owner = receipt["owner"]
        try:
            self.assertTrue(
                process_identity_is_alive(
                    owner["pid"], owner["process_start_identity"]
                )
            )
            self.assertTrue(
                process_identity_is_alive(
                    evidence["grandchild_pid"],
                    evidence["grandchild_start_identity"],
                )
            )
            stop.touch()
            self.assertTrue(
                _wait_identity_gone(
                    owner["pid"], owner["process_start_identity"], timeout=12
                )
            )
            self.assertTrue(
                _wait_identity_gone(
                    evidence["parent_pid"], evidence["parent_start_identity"]
                )
            )
            self.assertTrue(
                _wait_identity_gone(
                    evidence["grandchild_pid"],
                    evidence["grandchild_start_identity"],
                )
            )
            final_receipt = _wait_for_json(receipt_path)
            self.assertEqual(final_receipt["status"], "complete")
            self.assertEqual(final_receipt["survivors"], [])
        finally:
            stop.touch(exist_ok=True)
            if receipt is not None:
                _stop_exact_sanitized_process(
                    owner["pid"], owner["process_start_identity"]
                )


class ProcessLifecycleRaceTests(unittest.TestCase):
    def test_receipt_recovery_fails_closed_when_owner_liveness_is_uncertain(self):
        with tempfile.TemporaryDirectory() as temporary:
            receipt = Path(temporary) / "uncertain.json"
            receipt.write_text(
                json.dumps(
                    {
                        "schema_version": process_lifecycle.RECEIPT_SCHEMA_VERSION,
                        "status": "running",
                        "owner": {
                            "pid": 12345,
                            "process_start_identity": "67890",
                        },
                        "owned_processes": [],
                    }
                ),
                encoding="utf-8",
            )
            with mock.patch.object(
                process_lifecycle,
                "process_identity_is_alive",
                return_value=True,
            ):
                self.assertEqual(recover_incomplete_receipts(temporary), [])
            self.assertEqual(
                json.loads(receipt.read_text(encoding="utf-8"))["status"],
                "running",
            )

    def test_exit_between_identity_check_and_terminate_never_kills_reused_pid(self):
        class ExitRaceProcess:
            pid = 424242
            args = ["sanitized-exit-race"]

            def __init__(self, *_args, **_kwargs):
                self.returncode = None
                self.kill_calls = 0

            def poll(self):
                return self.returncode

            def terminate(self):
                self.returncode = 0
                raise ProcessLookupError("already exited")

            def kill(self):
                self.kill_calls += 1

            def wait(self, timeout=None):
                return self.returncode

        with tempfile.TemporaryDirectory() as tmp:
            supervisor = start_process_supervisor(
                "sanitized:pid-reuse-race",
                receipt_dir=tmp,
            )
            process = owned_popen(
                ["sanitized-exit-race"],
                source="tests.pid_reuse_race",
                popen_factory=ExitRaceProcess,
                register_injected=True,
            )
            result = terminate_owned_process(
                process,
                reason="race_cancel",
                terminate_grace=0,
                kill_grace=0,
            )
            self.assertEqual(result, "race_cancel_graceful")
            self.assertEqual(process.kill_calls, 0)
            receipt = supervisor.shutdown(reason="race_complete")
            self.assertEqual(receipt["survivors"], [])


if __name__ == "__main__":
    unittest.main()
