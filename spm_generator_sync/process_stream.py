"""Bounded subprocess streaming with owned Windows process-tree cancellation.

Reader threads only drain OS pipes. Callbacks run on the owning worker thread,
never on a Tk thread. On Windows, real ``subprocess.Popen`` launches are
suspended, assigned to a private kill-on-close Job Object, and only then
resumed so descendants cannot escape through the spawn/assign race.
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
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable


_LINE_END = re.compile(r"\r\n|\r|\n")
_DEFAULT_POPEN = subprocess.Popen
DEFAULT_CAPTURE_LIMIT_CHARS = 256 * 1024
DEFAULT_MAX_LINE_CHARS = 16 * 1024
DEFAULT_EVENT_QUEUE_SIZE = 512
# WinBase.h CREATE_SUSPENDED. Python 3.10 exposes CREATE_NO_WINDOW but not this
# flag through subprocess/_winapi, so keep the platform name next to its value.
WINDOWS_CREATE_SUSPENDED = 0x00000004


@dataclass(frozen=True)
class ProcessResult:
    """Final state and bounded diagnostic tails for one owned launch."""

    status: str
    returncode: int | None
    pid: int | None
    stdout: str
    stderr: str
    elapsed: float
    stdout_omitted_chars: int = 0
    stderr_omitted_chars: int = 0
    stdout_omitted_lines: int = 0
    stderr_omitted_lines: int = 0
    oversized_line_fragments: int = 0
    cleanup_state: str = "process_tree_clean"


class ProcessCancelled(RuntimeError):
    """Raised after a cancellation request has reached a safe final state."""

    def __init__(self, result: ProcessResult):
        self.result = result
        labels = {
            "cancelled_before_launch": "before launch",
            "cancelled_after_exit": "after process exit",
            "cancelled_terminated": "after terminate",
            "cancelled_killed": "after owned-tree kill fallback",
        }
        super().__init__(f"process cancelled {labels.get(result.status, result.status)}")


class ProcessTerminationError(RuntimeError):
    """Fail-closed process ownership or termination failure with evidence."""

    def __init__(self, reason_token: str, *, pid=None, evidence=None):
        self.reason_token = str(reason_token)
        self.pid = pid
        self.evidence = dict(evidence or {})
        details = ", ".join(
            f"{key}={value}" for key, value in sorted(self.evidence.items())
        )
        message = f"[{self.reason_token}] pid={self.pid}"
        if details:
            message += f"; {details}"
        super().__init__(message)


class _BoundedTextTail:
    """Keep a character-bounded line tail without quadratic concatenation."""

    def __init__(self, limit: int):
        self.limit = max(1, int(limit))
        self._parts: deque[str] = deque()
        self._cost = 0
        self.omitted_chars = 0
        self.omitted_lines = 0

    def append(self, value: str) -> None:
        value = str(value)
        maximum_value = max(0, self.limit - 1)
        if len(value) > maximum_value:
            self.omitted_chars += len(value) - maximum_value
            value = value[-maximum_value:] if maximum_value else ""
        cost = len(value) + 1
        while self._parts and self._cost + cost > self.limit:
            removed = self._parts.popleft()
            removed_cost = len(removed) + 1
            self._cost -= removed_cost
            self.omitted_chars += removed_cost
            self.omitted_lines += 1
        self._parts.append(value)
        self._cost += cost

    def text(self) -> str:
        return "\n".join(self._parts)


class _WindowsJobOwner:
    """One private Job Object assigned before the root process is resumed."""

    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
    JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION = 1
    JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9

    def __init__(self):
        import ctypes
        from ctypes import wintypes

        self.ctypes = ctypes
        self.wintypes = wintypes
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.ntdll = ctypes.WinDLL("ntdll", use_last_error=True)
        self._closed = False

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_ulonglong),
                ("WriteOperationCount", ctypes.c_ulonglong),
                ("OtherOperationCount", ctypes.c_ulonglong),
                ("ReadTransferCount", ctypes.c_ulonglong),
                ("WriteTransferCount", ctypes.c_ulonglong),
                ("OtherTransferCount", ctypes.c_ulonglong),
            ]

        class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
                ("IoInfo", IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        class JOBOBJECT_BASIC_ACCOUNTING_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("TotalUserTime", ctypes.c_longlong),
                ("TotalKernelTime", ctypes.c_longlong),
                ("ThisPeriodTotalUserTime", ctypes.c_longlong),
                ("ThisPeriodTotalKernelTime", ctypes.c_longlong),
                ("TotalPageFaultCount", wintypes.DWORD),
                ("TotalProcesses", wintypes.DWORD),
                ("ActiveProcesses", wintypes.DWORD),
                ("TotalTerminatedProcesses", wintypes.DWORD),
            ]

        self._accounting_type = JOBOBJECT_BASIC_ACCOUNTING_INFORMATION
        self.kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        self.kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        self.kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        self.kernel32.SetInformationJobObject.restype = wintypes.BOOL
        self.kernel32.AssignProcessToJobObject.argtypes = [
            wintypes.HANDLE,
            wintypes.HANDLE,
        ]
        self.kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        self.kernel32.QueryInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.c_void_p,
        ]
        self.kernel32.QueryInformationJobObject.restype = wintypes.BOOL
        self.kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        self.kernel32.TerminateJobObject.restype = wintypes.BOOL
        self.kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        self.kernel32.CloseHandle.restype = wintypes.BOOL
        self.ntdll.NtResumeProcess.argtypes = [wintypes.HANDLE]
        self.ntdll.NtResumeProcess.restype = ctypes.c_long

        self.handle = self.kernel32.CreateJobObjectW(None, None)
        if not self.handle:
            self._raise_last_error("process_tree_job_create_failed")
        limits = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        limits.BasicLimitInformation.LimitFlags = (
            self.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        )
        if not self.kernel32.SetInformationJobObject(
            self.handle,
            self.JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(limits),
            ctypes.sizeof(limits),
        ):
            error = ctypes.get_last_error()
            self.close()
            raise ProcessTerminationError(
                "process_tree_job_limit_failed",
                evidence={"winerror": error},
            )

    def _raise_last_error(self, token: str, *, pid=None) -> None:
        raise ProcessTerminationError(
            token,
            pid=pid,
            evidence={"winerror": self.ctypes.get_last_error()},
        )

    def creationflags(self, requested_flags: int) -> int:
        return int(requested_flags) | WINDOWS_CREATE_SUSPENDED

    def assign_and_resume(self, process: subprocess.Popen) -> None:
        process_handle = self.wintypes.HANDLE(int(process._handle))
        if not self.kernel32.AssignProcessToJobObject(self.handle, process_handle):
            self._raise_last_error(
                "process_tree_job_assign_failed",
                pid=getattr(process, "pid", None),
            )
        status = int(self.ntdll.NtResumeProcess(process_handle))
        if status != 0:
            raise ProcessTerminationError(
                "process_tree_resume_failed",
                pid=getattr(process, "pid", None),
                evidence={"ntstatus": f"0x{status & 0xFFFFFFFF:08x}"},
            )

    def active_process_count(self) -> int:
        accounting = self._accounting_type()
        if not self.kernel32.QueryInformationJobObject(
            self.handle,
            self.JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION,
            self.ctypes.byref(accounting),
            self.ctypes.sizeof(accounting),
            None,
        ):
            self._raise_last_error("process_tree_query_failed")
        return int(accounting.ActiveProcesses)

    def is_empty(self) -> bool:
        return self.active_process_count() == 0

    def wait_empty(self, timeout: float) -> bool:
        deadline = time.monotonic() + max(0.0, float(timeout))
        while True:
            if self.is_empty():
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            time.sleep(min(0.01, remaining))

    def terminate(self, *, pid=None, exit_code: int = 1) -> None:
        if self.is_empty():
            return
        if not self.kernel32.TerminateJobObject(self.handle, int(exit_code)):
            self._raise_last_error("process_tree_terminate_failed", pid=pid)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        handle = getattr(self, "handle", None)
        if handle:
            self.kernel32.CloseHandle(handle)
            self.handle = None


def _reader(
    stream,
    channel: str,
    events: queue.Queue,
    *,
    encoding: str,
    max_line_chars: int = DEFAULT_MAX_LINE_CHARS,
) -> None:
    """Decode chunks while bounding a newline-free pending line."""

    decoder = codecs.getincrementaldecoder(encoding)(errors="replace")
    pending = ""
    line_limit = max(1, int(max_line_chars))

    def emit_complete_lines(*, final: bool = False) -> None:
        nonlocal pending
        while True:
            match = _LINE_END.search(pending)
            if match is None:
                break
            if not final and match.group(0) == "\r" and match.end() == len(pending):
                break
            events.put(("line", channel, pending[: match.start()]))
            pending = pending[match.end() :]
        while len(pending) > line_limit:
            events.put(("line_fragment", channel, pending[:line_limit]))
            pending = pending[line_limit:]

    try:
        while True:
            chunk = stream.read(4096)
            if not chunk:
                break
            pending += decoder.decode(chunk)
            emit_complete_lines()
        pending += decoder.decode(b"", final=True)
        emit_complete_lines(final=True)
        while len(pending) > line_limit:
            events.put(("line_fragment", channel, pending[:line_limit]))
            pending = pending[line_limit:]
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


def _poll_after_signal_error(process, exc, *, token: str):
    returncode = process.poll()
    if returncode is not None:
        return returncode
    raise ProcessTerminationError(
        token,
        pid=getattr(process, "pid", None),
        evidence={"error_type": type(exc).__name__, "error": str(exc)},
    ) from exc


def _stop_exact_process(
    process: subprocess.Popen,
    *,
    reason: str,
    terminate_grace: float,
    kill_grace: float,
    owner: _WindowsJobOwner | None = None,
) -> str:
    """Idempotently stop the exact owned unit and absorb genuine exit races."""

    pid = getattr(process, "pid", None)
    if process.poll() is not None:
        if owner is not None and not owner.is_empty():
            owner.terminate(pid=pid)
            if not owner.wait_empty(kill_grace):
                raise ProcessTerminationError(
                    "process_tree_kill_grace_expired",
                    pid=pid,
                    evidence={"phase": reason, "kill_grace": kill_grace},
                )
        return f"{reason}_after_exit"

    try:
        process.terminate()
    except (ProcessLookupError, OSError) as exc:
        _poll_after_signal_error(process, exc, token="process_terminate_failed")
        if owner is not None and not owner.is_empty():
            owner.terminate(pid=pid)
            if not owner.wait_empty(kill_grace):
                raise ProcessTerminationError(
                    "process_tree_kill_grace_expired",
                    pid=pid,
                    evidence={"phase": reason, "kill_grace": kill_grace},
                )
        return f"{reason}_after_exit"

    terminate_started = time.monotonic()
    try:
        process.wait(timeout=max(0.0, float(terminate_grace)))
    except subprocess.TimeoutExpired:
        pass
    remaining = max(
        0.0,
        float(terminate_grace) - (time.monotonic() - terminate_started),
    )
    if process.poll() is not None and (
        owner is None or owner.wait_empty(remaining)
    ):
        return f"{reason}_terminated"

    if owner is not None:
        owner.terminate(pid=pid)
    else:
        if process.poll() is not None:
            return f"{reason}_terminated"
        try:
            process.kill()
        except (ProcessLookupError, OSError) as exc:
            _poll_after_signal_error(process, exc, token="process_kill_failed")
            return f"{reason}_killed"

    kill_started = time.monotonic()
    if owner is not None and not owner.wait_empty(kill_grace):
        raise ProcessTerminationError(
            "process_tree_kill_grace_expired",
            pid=pid,
            evidence={"phase": reason, "kill_grace": kill_grace},
        )
    remaining = max(0.0, float(kill_grace) - (time.monotonic() - kill_started))
    try:
        process.wait(timeout=remaining)
    except subprocess.TimeoutExpired as exc:
        if process.poll() is None:
            raise ProcessTerminationError(
                "process_kill_grace_expired",
                pid=pid,
                evidence={"phase": reason, "kill_grace": kill_grace},
            ) from exc
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
    exit_pipe_grace: float = 1.0,
    poll_interval: float = 0.03,
    encoding: str | None = None,
    creationflags: int = 0,
    capture_limit_chars: int = DEFAULT_CAPTURE_LIMIT_CHARS,
    max_line_chars: int = DEFAULT_MAX_LINE_CHARS,
    event_queue_size: int = DEFAULT_EVENT_QUEUE_SIZE,
    popen_factory=_DEFAULT_POPEN,
) -> ProcessResult:
    """Stream an owned process with bounded tails and deterministic outcomes."""

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

    owner = None
    if os.name == "nt" and popen_factory is _DEFAULT_POPEN:
        owner = _WindowsJobOwner()
        creationflags = owner.creationflags(creationflags)

    process = None
    try:
        process = popen_factory(
            args,
            cwd=str(Path(cwd)) if cwd is not None else None,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
            creationflags=int(creationflags),
        )
        if owner is not None:
            owner.assign_and_resume(process)
    except BaseException:
        if process is not None and process.poll() is None:
            try:
                process.kill()
                process.wait(timeout=max(0.1, float(kill_grace)))
            except BaseException:
                pass
        if owner is not None:
            owner.close()
        raise

    if process.stdout is None or process.stderr is None:
        if owner is not None:
            owner.close()
        raise RuntimeError("subprocess pipes were not created")

    events: queue.Queue = queue.Queue(maxsize=max(8, int(event_queue_size)))
    readers = [
        threading.Thread(
            target=_reader,
            args=(stream, channel, events),
            kwargs={"encoding": encoding, "max_line_chars": max_line_chars},
            name=f"process-{channel}-{getattr(process, 'pid', 'unknown')}",
            # A reader parked in read() on a pipe whose write end is held
            # outside the owned tree is already reported as
            # process_pipe_eof_timeout; it must not additionally block
            # interpreter shutdown.  A non-daemon reader kept pythonw.exe alive
            # after its window closed, which also keeps the shared queue's
            # liveness probe returning True so the lease never recovers.
            daemon=True,
        )
        for channel, stream in (
            ("stdout", process.stdout),
            ("stderr", process.stderr),
        )
    ]
    for thread in readers:
        thread.start()

    captured = {
        "stdout": _BoundedTextTail(capture_limit_chars),
        "stderr": _BoundedTextTail(capture_limit_chars),
    }
    eof_channels: set[str] = set()
    reader_error: BaseException | None = None
    callback_error: BaseException | None = None
    final_status: str | None = None
    cleanup_state = "process_tree_clean"
    oversized_line_fragments = 0
    root_exit_observed_at = None

    def consume(event) -> None:
        nonlocal reader_error, callback_error, oversized_line_fragments
        kind, channel, payload = event
        if kind in {"line", "line_fragment"}:
            if kind == "line_fragment":
                oversized_line_fragments += 1
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

    primary_error = None
    cleanup_error = None
    try:
        while True:
            try:
                consume(events.get(timeout=max(0.001, float(poll_interval))))
            except queue.Empty:
                pass
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
                    owner=owner,
                )
                break

            returncode = process.poll()
            if returncode is not None:
                if root_exit_observed_at is None:
                    root_exit_observed_at = time.monotonic()
                ownership_done = owner is None or owner.is_empty()
                if len(eof_channels) == 2 and ownership_done:
                    final_status = "completed"
                    break
                if (
                    owner is not None
                    and not ownership_done
                    and time.monotonic() - root_exit_observed_at
                    >= max(0.0, float(exit_pipe_grace))
                ):
                    owner.terminate(pid=getattr(process, "pid", None))
                    if not owner.wait_empty(kill_grace):
                        raise ProcessTerminationError(
                            "process_tree_kill_grace_expired",
                            pid=getattr(process, "pid", None),
                            evidence={
                                "phase": "root_exit_pipe_cleanup",
                                "kill_grace": kill_grace,
                            },
                        )
                    cleanup_state = "process_tree_forced_after_root_exit"
                elif time.monotonic() - root_exit_observed_at >= max(
                    0.0, float(exit_pipe_grace)
                ):
                    # Root gone, nothing left in the tree to terminate, but a
                    # pipe still has no EOF: the write end is held outside the
                    # owned tree, or there is no tree to own (POSIX, or an
                    # injected popen_factory).  Without this the loop spins on
                    # `continue` forever -- the timeout and cancellation checks
                    # below are unreachable once the root has exited -- so a
                    # finished export never returns and cannot be cancelled.
                    # Stop waiting for the missing EOF and let the cleanup
                    # block raise process_pipe_eof_timeout if a reader is still
                    # parked after kill_grace.
                    cleanup_state = "process_pipe_open_after_root_exit"
                    break
                continue

            if requested():
                if output_callback is not None:
                    try:
                        output_callback(
                            "system",
                            "Cancellation requested; stopping owned process tree.",
                        )
                    except BaseException as exc:
                        callback_error = exc
                        continue
                stop_status = _stop_exact_process(
                    process,
                    reason="cancelled",
                    terminate_grace=terminate_grace,
                    kill_grace=kill_grace,
                    owner=owner,
                )
                if (
                    stop_status == "cancelled_after_exit"
                    and process.poll() not in {None, 0}
                ):
                    final_status = "completed"
                else:
                    final_status = stop_status
                break
            elapsed = time.monotonic() - started
            if timeout is not None and elapsed >= float(timeout):
                final_status = _stop_exact_process(
                    process,
                    reason="timed_out",
                    terminate_grace=terminate_grace,
                    kill_grace=kill_grace,
                    owner=owner,
                )
                break
    except BaseException as exc:
        primary_error = exc

    try:
        ownership_active = owner is not None and not owner.is_empty()
        if process.poll() is None or ownership_active:
            _stop_exact_process(
                process,
                reason="cleanup",
                terminate_grace=terminate_grace,
                kill_grace=kill_grace,
                owner=owner,
            )

        join_deadline = time.monotonic() + max(0.1, float(kill_grace))
        while any(thread.is_alive() for thread in readers):
            remaining = join_deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                consume(events.get(timeout=min(0.02, remaining)))
            except queue.Empty:
                pass
        while True:
            try:
                consume(events.get_nowait())
            except queue.Empty:
                break
        for thread in readers:
            thread.join(timeout=0)
        alive_readers = [thread.name for thread in readers if thread.is_alive()]
        if alive_readers:
            raise ProcessTerminationError(
                "process_pipe_eof_timeout",
                pid=getattr(process, "pid", None),
                evidence={"readers": ",".join(alive_readers)},
            )
    except BaseException as exc:
        cleanup_error = exc
    finally:
        if owner is not None:
            owner.close()

    if cleanup_error is not None:
        if primary_error is not None:
            raise cleanup_error from primary_error
        raise cleanup_error
    if primary_error is not None:
        raise primary_error

    elapsed = max(0.0, time.monotonic() - started)
    result = ProcessResult(
        status=final_status or "completed",
        returncode=process.poll(),
        pid=getattr(process, "pid", None),
        stdout=captured["stdout"].text(),
        stderr=captured["stderr"].text(),
        elapsed=elapsed,
        stdout_omitted_chars=captured["stdout"].omitted_chars,
        stderr_omitted_chars=captured["stderr"].omitted_chars,
        stdout_omitted_lines=captured["stdout"].omitted_lines,
        stderr_omitted_lines=captured["stderr"].omitted_lines,
        oversized_line_fragments=oversized_line_fragments,
        cleanup_state=cleanup_state,
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
    "ProcessTerminationError",
    "run_streaming_process",
]
