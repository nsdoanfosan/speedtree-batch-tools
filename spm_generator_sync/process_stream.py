"""Deadlock-safe subprocess output streaming with explicit cancellation.

The worker threads in this module only drain OS pipes.  Callbacks run on the
calling worker thread, never on a Tk thread, so GUI callers can forward events
through their existing queue without touching widgets here.
"""

from __future__ import annotations

import codecs
import locale
import os
import queue
import re
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable


_LINE_END = re.compile(r"\r\n|\r|\n")


@dataclass(frozen=True)
class ProcessResult:
    """Final state and captured output for one exact launched process."""

    status: str
    returncode: int | None
    pid: int | None
    stdout: str
    stderr: str
    elapsed: float


class ProcessCancelled(RuntimeError):
    """Raised after a cancellation request has reached a safe final state."""

    def __init__(self, result: ProcessResult):
        self.result = result
        labels = {
            "cancelled_before_launch": "before launch",
            "cancelled_after_exit": "after process exit",
            "cancelled_terminated": "after terminate",
            "cancelled_killed": "after forced kill fallback",
        }
        super().__init__(f"process cancelled {labels.get(result.status, result.status)}")


def _reader(
    stream,
    channel: str,
    events: queue.Queue,
    *,
    encoding: str,
) -> None:
    """Decode chunks and preserve a final non-newline-terminated partial line."""

    decoder = codecs.getincrementaldecoder(encoding)(errors="replace")
    pending = ""

    def emit_complete_lines(*, final: bool = False) -> None:
        nonlocal pending
        while True:
            match = _LINE_END.search(pending)
            if match is None:
                break
            # A Windows CRLF can be split across two pipe reads. Hold a
            # trailing CR until the next chunk instead of emitting a phantom
            # blank line when the following LF arrives.
            if (
                not final
                and match.group(0) == "\r"
                and match.end() == len(pending)
            ):
                break
            events.put(("line", channel, pending[: match.start()]))
            pending = pending[match.end() :]

    try:
        while True:
            chunk = stream.read(4096)
            if not chunk:
                break
            pending += decoder.decode(chunk)
            emit_complete_lines()
        pending += decoder.decode(b"", final=True)
        emit_complete_lines(final=True)
        if pending:
            events.put(("line", channel, pending))
    except BaseException as exc:  # surfaced on the owning worker thread
        events.put(("reader_error", channel, exc))
    finally:
        try:
            stream.close()
        except OSError:
            pass
        events.put(("eof", channel, None))


def _stop_exact_process(
    process: subprocess.Popen,
    *,
    reason: str,
    terminate_grace: float,
    kill_grace: float,
) -> str:
    """Stop only ``process`` and report whether terminate or kill was needed."""

    if process.poll() is not None:
        return f"{reason}_after_exit"
    process.terminate()
    try:
        process.wait(timeout=max(0.0, float(terminate_grace)))
        return f"{reason}_terminated"
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=max(0.0, float(kill_grace)))
        return f"{reason}_killed"


def run_streaming_process(
    command: Iterable[str | os.PathLike[str]],
    *,
    cwd: str | os.PathLike[str] | None = None,
    timeout: float | None = None,
    output_callback: Callable[[str, str], None] | None = None,
    cancel_requested: Callable[[], bool] | None = None,
    terminate_grace: float = 1.0,
    kill_grace: float = 2.0,
    poll_interval: float = 0.03,
    encoding: str | None = None,
    creationflags: int = 0,
    popen_factory=subprocess.Popen,
) -> ProcessResult:
    """Run a child while streaming both pipes and honoring explicit cancel.

    Cancellation is checked before launch and throughout execution.  A request
    first sends ``terminate`` to the exact ``Popen`` instance created here and
    uses ``kill`` only when that process misses the bounded grace period.
    """

    args = [str(value) for value in command]
    if not args:
        raise ValueError("command must not be empty")
    requested = cancel_requested or (lambda: False)
    encoding = encoding or locale.getpreferredencoding(False) or "utf-8"
    started = time.monotonic()
    if requested():
        result = ProcessResult(
            "cancelled_before_launch", None, None, "", "", 0.0
        )
        if output_callback is not None:
            output_callback("system", "Cancellation accepted before process launch.")
        raise ProcessCancelled(result)

    process = popen_factory(
        args,
        cwd=str(Path(cwd)) if cwd is not None else None,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0,
        creationflags=int(creationflags),
    )
    if process.stdout is None or process.stderr is None:
        raise RuntimeError("subprocess pipes were not created")

    events: queue.Queue = queue.Queue()
    readers = [
        threading.Thread(
            target=_reader,
            args=(stream, channel, events),
            kwargs={"encoding": encoding},
            name=f"process-{channel}-{getattr(process, 'pid', 'unknown')}",
            daemon=True,
        )
        for channel, stream in (
            ("stdout", process.stdout),
            ("stderr", process.stderr),
        )
    ]
    for thread in readers:
        thread.start()

    captured = {"stdout": [], "stderr": []}
    eof_channels: set[str] = set()
    reader_error: BaseException | None = None
    callback_error: BaseException | None = None
    final_status: str | None = None

    def consume(event) -> None:
        nonlocal reader_error, callback_error
        kind, channel, payload = event
        if kind == "line":
            captured[channel].append(payload)
            if output_callback is not None and callback_error is None:
                try:
                    output_callback(channel, payload)
                except BaseException as exc:
                    callback_error = exc
        elif kind == "reader_error" and reader_error is None:
            reader_error = payload
        elif kind == "eof":
            eof_channels.add(channel)

    try:
        while True:
            try:
                consume(events.get(timeout=max(0.001, float(poll_interval))))
            except queue.Empty:
                pass
            # Never let an infinite producer starve cancellation/timeout
            # checks. Reader threads keep both OS pipes drained concurrently;
            # this loop forwards a bounded batch before checking control state.
            for _index in range(1000):
                try:
                    consume(events.get_nowait())
                except queue.Empty:
                    break

            if callback_error is not None or reader_error is not None:
                final_status = _stop_exact_process(
                    process,
                    reason="callback_error",
                    terminate_grace=terminate_grace,
                    kill_grace=kill_grace,
                )
                break
            if requested():
                if output_callback is not None:
                    output_callback("system", "Cancellation requested; stopping SpeedTree.")
                final_status = _stop_exact_process(
                    process,
                    reason="cancelled",
                    terminate_grace=terminate_grace,
                    kill_grace=kill_grace,
                )
                break
            elapsed = time.monotonic() - started
            if timeout is not None and elapsed >= float(timeout):
                final_status = _stop_exact_process(
                    process,
                    reason="timed_out",
                    terminate_grace=terminate_grace,
                    kill_grace=kill_grace,
                )
                break
            if process.poll() is not None and len(eof_channels) == 2:
                final_status = "completed"
                break
    finally:
        if process.poll() is None:
            _stop_exact_process(
                process,
                reason="cleanup",
                terminate_grace=terminate_grace,
                kill_grace=kill_grace,
            )
        for thread in readers:
            thread.join(timeout=max(0.1, float(kill_grace)))
        while True:
            try:
                consume(events.get_nowait())
            except queue.Empty:
                break

    elapsed = max(0.0, time.monotonic() - started)
    result = ProcessResult(
        status=final_status or "completed",
        returncode=process.poll(),
        pid=getattr(process, "pid", None),
        stdout="\n".join(captured["stdout"]),
        stderr="\n".join(captured["stderr"]),
        elapsed=elapsed,
    )
    if callback_error is not None:
        raise callback_error
    if reader_error is not None:
        raise RuntimeError(f"subprocess output reader failed: {reader_error}") from reader_error
    if result.status.startswith("cancelled_"):
        raise ProcessCancelled(result)
    if result.status.startswith("timed_out_"):
        exc = subprocess.TimeoutExpired(
            args,
            timeout,
            output=result.stdout,
            stderr=result.stderr,
        )
        exc.termination_state = result.status
        exc.pid = result.pid
        raise exc
    return result


__all__ = [
    "ProcessCancelled",
    "ProcessResult",
    "run_streaming_process",
]
