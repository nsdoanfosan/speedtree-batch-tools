import io
import os
import queue
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path


TOOL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOL_DIR))

from process_stream import (
    ProcessCancelled,
    ProcessTerminationError,
    _reader,
    run_streaming_process,
)


class _ChunkedStream:
    def __init__(self, chunks):
        self.chunks = iter(chunks)

    def read(self, _size):
        return next(self.chunks, b"")

    def close(self):
        pass


class _ForcedKillProcess:
    pid = 4242

    def __init__(self, *_args, **_kwargs):
        self.stdout = io.BytesIO(b"")
        self.stderr = io.BytesIO(b"")
        self.returncode = None
        self.terminate_calls = 0
        self.kill_calls = 0

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminate_calls += 1

    def kill(self):
        self.kill_calls += 1
        self.returncode = -9

    def wait(self, timeout=None):
        if self.returncode is None:
            raise subprocess.TimeoutExpired(["fake"], timeout)
        return self.returncode


class _ExitedNonzeroProcess(_ForcedKillProcess):
    pid = 4343

    def poll(self):
        return 7


class _NonzeroBetweenControlChecksProcess(_ForcedKillProcess):
    pid = 4393

    def __init__(self, *_args, **_kwargs):
        super().__init__(*_args, **_kwargs)
        self.poll_calls = 0

    def poll(self):
        self.poll_calls += 1
        return None if self.poll_calls == 1 else 7


class _ExitBetweenPollAndTerminateProcess(_ForcedKillProcess):
    pid = 4444

    def terminate(self):
        self.terminate_calls += 1
        self.returncode = 0
        raise ProcessLookupError("already exited")


class _ExitBeforeKillProcess(_ForcedKillProcess):
    pid = 4545

    def wait(self, timeout=None):
        if self.terminate_calls and self.returncode is None:
            self.returncode = 0
            raise subprocess.TimeoutExpired(["fake"], timeout)
        return self.returncode


class _NeverDiesProcess(_ForcedKillProcess):
    pid = 4646

    def kill(self):
        self.kill_calls += 1


class _BlockingStream:
    """A pipe whose write end is held open outside the owned tree."""

    def __init__(self):
        self.released = threading.Event()

    def read(self, _size):
        self.released.wait()
        return b""

    def close(self):
        self.released.set()


class _RootExitedWithOpenPipeProcess:
    """Root already exited; one channel never reaches EOF."""

    pid = 4747

    def __init__(self, *_args, **_kwargs):
        self.stdout = io.BytesIO(b"")
        self.stderr = _BlockingStream()

    def poll(self):
        return 0

    def wait(self, timeout=None):
        return 0

    def terminate(self):
        pass

    def kill(self):
        pass


def _windows_process_is_alive(pid):
    if os.name != "nt":
        return False
    import ctypes

    process_query_limited_information = 0x1000
    still_active = 259
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = kernel32.OpenProcess(
        process_query_limited_information,
        False,
        int(pid),
    )
    if not handle:
        return False
    try:
        exit_code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return False
        return exit_code.value == still_active
    finally:
        kernel32.CloseHandle(handle)


class RootExitedWithOpenPipeTests(unittest.TestCase):
    """A missing EOF after root exit must not disable timeout or cancellation.

    Both checks used to sit below the `continue` taken on every post-exit
    iteration, so a finished export whose pipe stayed open never returned and
    could not be cancelled.  Reproduced with the module's own popen_factory
    seam; each case must finish well inside its own wait budget.
    """

    def _run_in_thread(self, **kwargs):
        outcome = {}
        finished = threading.Event()

        def run():
            try:
                outcome["result"] = run_streaming_process(
                    ["fake.exe"],
                    popen_factory=_RootExitedWithOpenPipeProcess,
                    terminate_grace=0.2,
                    kill_grace=0.2,
                    exit_pipe_grace=0.2,
                    **kwargs,
                )
            except BaseException as exc:  # noqa: BLE001 - recorded for assertion
                outcome["error"] = exc
            finished.set()

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        self.assertTrue(
            finished.wait(15.0),
            "run_streaming_process never returned after root exit",
        )
        return outcome

    def test_timeout_still_fires_when_a_pipe_never_reaches_eof(self):
        outcome = self._run_in_thread(timeout=0.3)
        error = outcome.get("error")
        self.assertIsInstance(error, ProcessTerminationError)
        self.assertEqual(error.reason_token, "process_pipe_eof_timeout")

    def test_cancellation_after_root_exit_is_still_observed(self):
        started = time.monotonic()
        outcome = self._run_in_thread(
            timeout=None,
            cancel_requested=lambda: time.monotonic() - started > 0.1,
        )
        error = outcome.get("error")
        self.assertIsInstance(error, ProcessTerminationError)
        self.assertEqual(error.reason_token, "process_pipe_eof_timeout")

    def test_readers_do_not_block_interpreter_shutdown(self):
        process = _RootExitedWithOpenPipeProcess()
        try:
            run_streaming_process(
                ["fake.exe"],
                popen_factory=lambda *_a, **_k: process,
                timeout=0.3,
                terminate_grace=0.2,
                kill_grace=0.2,
                exit_pipe_grace=0.2,
            )
        except ProcessTerminationError:
            pass
        parked = [
            thread for thread in threading.enumerate()
            if thread.name.startswith("process-stderr-")
        ]
        self.assertTrue(parked, "expected the blocked reader to still exist")
        for thread in parked:
            self.assertTrue(
                thread.daemon,
                f"{thread.name} is non-daemon and would hang shutdown",
            )
        process.stderr.close()


class ProcessStreamTests(unittest.TestCase):
    def test_crlf_split_between_chunks_does_not_create_a_blank_line(self):
        events = queue.Queue()
        _reader(
            _ChunkedStream([b"one\r", b"\ntwo\r", b"\npartial"]),
            "stdout",
            events,
            encoding="utf-8",
        )
        lines = []
        while not events.empty():
            kind, _channel, payload = events.get_nowait()
            if kind == "line":
                lines.append(payload)

        self.assertEqual(lines, ["one", "two", "partial"])

    def test_stdout_stderr_stream_before_exit_and_partial_line_is_kept(self):
        received = []
        first_line = threading.Event()
        result_box = []

        def callback(channel, line):
            received.append((channel, line))
            if line == "first":
                first_line.set()

        def run():
            result_box.append(run_streaming_process(
                [
                    sys.executable,
                    "-u",
                    "-c",
                    (
                        "import sys,time; "
                        "sys.stdout.write('first\\n'); sys.stdout.flush(); "
                        "time.sleep(0.35); "
                        "sys.stderr.write('warning\\n'); sys.stderr.flush(); "
                        "sys.stdout.write('partial'); sys.stdout.flush()"
                    ),
                ],
                output_callback=callback,
                timeout=5,
            ))

        thread = threading.Thread(target=run)
        thread.start()
        self.assertTrue(first_line.wait(2))
        self.assertTrue(thread.is_alive(), "first line should arrive before process exit")
        thread.join(5)

        self.assertFalse(thread.is_alive())
        self.assertEqual(result_box[0].status, "completed")
        self.assertIn(("stderr", "warning"), received)
        self.assertIn(("stdout", "partial"), received)
        self.assertTrue(result_box[0].stdout.endswith("partial"))

    def test_chatty_stdout_and_stderr_are_drained_without_deadlock(self):
        received = []
        count = 2500
        result = run_streaming_process(
            [
                sys.executable,
                "-u",
                "-c",
                (
                    "import sys; "
                    f"[(sys.stdout.write(f'o{{i}}\\n'), "
                    f"sys.stderr.write(f'e{{i}}\\n')) for i in range({count})]"
                ),
            ],
            output_callback=lambda channel, line: received.append((channel, line)),
            timeout=10,
        )

        self.assertEqual(result.returncode, 0)
        self.assertEqual(
            sum(channel == "stdout" for channel, _line in received), count
        )
        self.assertEqual(
            sum(channel == "stderr" for channel, _line in received), count
        )

    def test_high_output_cancel_latency_stays_bounded(self):
        cancel = threading.Event()
        observed = 0

        def callback(_channel, _line):
            nonlocal observed
            observed += 1
            if observed >= 2000:
                cancel.set()

        started = time.monotonic()
        with self.assertRaises(ProcessCancelled) as raised:
            run_streaming_process(
                [
                    sys.executable,
                    "-u",
                    "-c",
                    (
                        "import sys; i=0; "
                        "exec(\"while True:\\n "
                        " sys.stdout.write(f'spam-{i}\\\\n'); "
                        "sys.stderr.write(f'warn-{i}\\\\n'); i += 1\")"
                    ),
                ],
                output_callback=callback,
                cancel_requested=cancel.is_set,
                terminate_grace=0.1,
                kill_grace=3,
                timeout=20,
            )

        self.assertLess(time.monotonic() - started, 5)
        self.assertGreaterEqual(observed, 2000)
        self.assertIn(
            raised.exception.result.status,
            {"cancelled_terminated", "cancelled_killed"},
        )

    def test_cancel_before_launch_does_not_construct_process(self):
        launched = []

        with self.assertRaises(ProcessCancelled) as raised:
            run_streaming_process(
                ["never-launched"],
                cancel_requested=lambda: True,
                popen_factory=lambda *_args, **_kwargs: launched.append(True),
            )

        self.assertEqual(raised.exception.result.status, "cancelled_before_launch")
        self.assertEqual(launched, [])

    def test_cancel_during_real_process_terminates_the_exact_child(self):
        cancel = threading.Event()
        ready = threading.Event()
        raised = []

        def callback(_channel, line):
            if line == "ready":
                ready.set()

        def run():
            try:
                run_streaming_process(
                    [
                        sys.executable,
                        "-u",
                        "-c",
                        "import time; print('ready', flush=True); time.sleep(30)",
                    ],
                    output_callback=callback,
                    cancel_requested=cancel.is_set,
                    timeout=35,
                )
            except ProcessCancelled as exc:
                raised.append(exc)

        thread = threading.Thread(target=run)
        thread.start()
        self.assertTrue(ready.wait(3))
        cancel.set()
        thread.join(5)

        self.assertFalse(thread.is_alive())
        self.assertEqual(len(raised), 1)
        self.assertEqual(raised[0].result.status, "cancelled_terminated")
        self.assertIsNotNone(raised[0].result.pid)
        self.assertIsNotNone(raised[0].result.returncode)

    def test_unresponsive_process_uses_forced_kill_fallback(self):
        checks = iter((False, True))
        created = []

        def factory(*args, **kwargs):
            process = _ForcedKillProcess(*args, **kwargs)
            created.append(process)
            return process

        with self.assertRaises(ProcessCancelled) as raised:
            run_streaming_process(
                ["fake"],
                cancel_requested=lambda: next(checks, True),
                terminate_grace=0,
                kill_grace=0,
                popen_factory=factory,
            )

        self.assertEqual(raised.exception.result.status, "cancelled_killed")
        self.assertEqual(created[0].terminate_calls, 1)
        self.assertEqual(created[0].kill_calls, 1)

    def test_exited_nonzero_process_wins_over_simultaneous_cancel(self):
        checks = iter((False, True))
        created = []

        def factory(*args, **kwargs):
            process = _ExitedNonzeroProcess(*args, **kwargs)
            created.append(process)
            return process

        result = run_streaming_process(
            ["fake"],
            cancel_requested=lambda: next(checks, True),
            popen_factory=factory,
        )

        self.assertEqual(result.status, "completed")
        self.assertEqual(result.returncode, 7)
        self.assertEqual(created[0].terminate_calls, 0)
        self.assertEqual(created[0].kill_calls, 0)

    def test_exit_between_poll_and_terminate_is_absorbed_as_a_race(self):
        checks = iter((False, True))

        with self.assertRaises(ProcessCancelled) as raised:
            run_streaming_process(
                ["fake"],
                cancel_requested=lambda: next(checks, True),
                popen_factory=_ExitBetweenPollAndTerminateProcess,
            )

        self.assertEqual(raised.exception.result.status, "cancelled_after_exit")
        self.assertEqual(raised.exception.result.returncode, 0)

    def test_nonzero_exit_between_control_checks_still_wins_over_cancel(self):
        checks = iter((False, True))

        result = run_streaming_process(
            ["fake"],
            cancel_requested=lambda: next(checks, True),
            popen_factory=_NonzeroBetweenControlChecksProcess,
        )

        self.assertEqual(result.status, "completed")
        self.assertEqual(result.returncode, 7)

    def test_exit_before_kill_does_not_signal_a_reused_process(self):
        checks = iter((False, True))
        created = []

        def factory(*args, **kwargs):
            process = _ExitBeforeKillProcess(*args, **kwargs)
            created.append(process)
            return process

        with self.assertRaises(ProcessCancelled) as raised:
            run_streaming_process(
                ["fake"],
                cancel_requested=lambda: next(checks, True),
                terminate_grace=0,
                popen_factory=factory,
            )

        self.assertEqual(raised.exception.result.status, "cancelled_terminated")
        self.assertEqual(created[0].kill_calls, 0)

    def test_kill_grace_timeout_is_a_fail_closed_termination_error(self):
        checks = iter((False, True))

        with self.assertRaises(ProcessTerminationError) as raised:
            run_streaming_process(
                ["fake"],
                cancel_requested=lambda: next(checks, True),
                terminate_grace=0,
                kill_grace=0,
                popen_factory=_NeverDiesProcess,
            )

        self.assertEqual(
            raised.exception.reason_token,
            "process_kill_grace_expired",
        )
        self.assertIn("process_kill_grace_expired", str(raised.exception))

    def test_capture_tail_is_bounded_for_one_hundred_thousand_lines(self):
        count = 100_000
        result = run_streaming_process(
            [
                sys.executable,
                "-u",
                "-c",
                (
                    "import sys; "
                    f"[sys.stdout.write(f'line-{{i:06d}}-xxxxxxxx\\n') for i in range({count})]; "
                    "sys.stderr.write('final-stderr-evidence\\n')"
                ),
            ],
            timeout=20,
            capture_limit_chars=8192,
        )

        self.assertEqual(result.returncode, 0)
        self.assertLessEqual(len(result.stdout), 8192)
        self.assertGreater(result.stdout_omitted_chars, 0)
        self.assertIn("line-099999", result.stdout)
        self.assertIn("final-stderr-evidence", result.stderr)

    def test_oversized_unterminated_line_is_chunked_without_unbounded_pending(self):
        result = run_streaming_process(
            [
                sys.executable,
                "-u",
                "-c",
                "import sys; sys.stdout.write('z' * 2000000); sys.stdout.flush()",
            ],
            timeout=20,
            capture_limit_chars=8192,
            max_line_chars=4096,
        )

        self.assertEqual(result.returncode, 0)
        self.assertLessEqual(len(result.stdout), 8192)
        self.assertGreater(result.stdout_omitted_chars, 0)
        self.assertGreater(result.oversized_line_fragments, 0)
        self.assertTrue(result.stdout.endswith("z" * 1024))

    @unittest.skipUnless(os.name == "nt", "Windows Job Object ownership")
    def test_windows_cancel_kills_owned_parent_and_inheriting_grandchild_only(self):
        cancel = threading.Event()
        ready = threading.Event()
        pids = {}
        raised = []
        sibling = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            creationflags=subprocess.CREATE_NO_WINDOW,
        )

        parent_code = (
            "import os,subprocess,sys,time; "
            "child=subprocess.Popen([sys.executable,'-u','-c',"
            "'import time; print(\\\"grandchild-ready\\\",flush=True); time.sleep(60)']); "
            "print(f'PIDS {os.getpid()} {child.pid}',flush=True); time.sleep(60)"
        )

        def callback(_channel, line):
            if line.startswith("PIDS "):
                _label, parent_pid, child_pid = line.split()
                pids.update(parent=int(parent_pid), child=int(child_pid))
                ready.set()

        def run():
            try:
                run_streaming_process(
                    [sys.executable, "-u", "-c", parent_code],
                    output_callback=callback,
                    cancel_requested=cancel.is_set,
                    terminate_grace=0.2,
                    kill_grace=3,
                    timeout=70,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
            except ProcessCancelled as exc:
                raised.append(exc)

        thread = threading.Thread(target=run)
        try:
            thread.start()
            self.assertTrue(ready.wait(5))
            cancel.set()
            thread.join(8)

            self.assertFalse(thread.is_alive())
            self.assertEqual(len(raised), 1)
            self.assertIn(
                raised[0].result.status,
                {"cancelled_terminated", "cancelled_killed"},
            )
            self.assertFalse(_windows_process_is_alive(pids["parent"]))
            self.assertFalse(_windows_process_is_alive(pids["child"]))
            self.assertTrue(_windows_process_is_alive(sibling.pid))
        finally:
            cancel.set()
            if sibling.poll() is None:
                sibling.terminate()
                try:
                    sibling.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    sibling.kill()
                    sibling.wait(timeout=3)

    @unittest.skipUnless(os.name == "nt", "Windows Job Object ownership")
    def test_windows_root_exit_with_inherited_pipe_has_bounded_tree_cleanup(self):
        grandchild_pid = []
        parent_code = (
            "import subprocess,sys; "
            "child=subprocess.Popen([sys.executable,'-u','-c',"
            "'import time; time.sleep(60)']); "
            "print(f'CHILD {child.pid}',flush=True)"
        )

        started = time.monotonic()
        result = run_streaming_process(
            [sys.executable, "-u", "-c", parent_code],
            output_callback=lambda _channel, line: (
                grandchild_pid.append(int(line.split()[1]))
                if line.startswith("CHILD ") else None
            ),
            terminate_grace=0.15,
            kill_grace=3,
            exit_pipe_grace=0.15,
            timeout=10,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )

        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(
            result.cleanup_state,
            "process_tree_forced_after_root_exit",
        )
        self.assertEqual(len(grandchild_pid), 1)
        self.assertFalse(_windows_process_is_alive(grandchild_pid[0]))


if __name__ == "__main__":
    unittest.main()
