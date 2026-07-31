import io
import queue
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path


TOOL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOL_DIR))

from process_stream import ProcessCancelled, _reader, run_streaming_process


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


class _ExitedRaceProcess(_ForcedKillProcess):
    pid = 4343

    def poll(self):
        return 0


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

    def test_process_exit_race_is_recorded_as_cancelled_after_exit(self):
        checks = iter((False, True))
        created = []

        def factory(*args, **kwargs):
            process = _ExitedRaceProcess(*args, **kwargs)
            created.append(process)
            return process

        with self.assertRaises(ProcessCancelled) as raised:
            run_streaming_process(
                ["fake"],
                cancel_requested=lambda: next(checks, True),
                popen_factory=factory,
            )

        self.assertEqual(raised.exception.result.status, "cancelled_after_exit")
        self.assertEqual(created[0].terminate_calls, 0)
        self.assertEqual(created[0].kill_calls, 0)


if __name__ == "__main__":
    unittest.main()
