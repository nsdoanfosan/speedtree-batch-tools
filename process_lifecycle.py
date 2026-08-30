"""Exact-handle process ownership for every SpeedTree Batch Tools launch.

The GUI process is the durable lifecycle supervisor.  Tool-owned children are
created suspended on Windows, assigned to both a session Job Object and a
private per-launch Job Object, and resumed only after both assignments and the
    durable receipt succeed.  Closing the GUI normally gives each root a bounded
    cooperative-cancellation window.  Killing the GUI closes the last Job handles and
therefore terminates only processes explicitly assigned by this module.

Manual handoffs (a visible Modeler, Explorer, or another standalone GUI) are
recorded but deliberately never assigned to either Job.  No executable-name
lookup or ``taskkill`` call is used anywhere in this contract.
"""

from __future__ import annotations

import atexit
import copy
import hashlib
import json
import os
import subprocess
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


WINDOWS_CREATE_SUSPENDED = 0x00000004
WINDOWS_DETACHED_PROCESS = 0x00000008
WINDOWS_CREATE_NEW_CONSOLE = 0x00000010
WINDOWS_CREATE_NO_WINDOW = 0x08000000
WINDOWS_CREATE_BREAKAWAY_FROM_JOB = 0x01000000
WINDOWS_STARTF_USESHOWWINDOW = 0x00000001
WINDOWS_SW_HIDE = 0
DEFAULT_GRACE_SECONDS = 1.0
DEFAULT_KILL_GRACE_SECONDS = 3.0
DEFAULT_DESCENDANT_GRACE_SECONDS = 0.2
DEFAULT_RUN_TIMEOUT_SECONDS = 6 * 60 * 60
RECEIPT_SCHEMA_VERSION = 1
ACTIVE_RECEIPT_INDEX_VERSION = 1
ACTIVE_RECEIPT_DIRECTORY_NAME = "active_v1"
ACTIVE_RECEIPT_MIGRATION_MARKER = ".active_index_v1_complete"
MAX_TERMINAL_RECEIPTS = 512
_DEFAULT_POPEN = subprocess.Popen
_DEFAULT_RUN = subprocess.run
_SUPERVISOR_LOCK = threading.RLock()
_SUPERVISOR = None


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class ProcessLifecycleError(RuntimeError):
    """Fail-closed ownership, cleanup, or receipt error."""

    def __init__(self, reason_token, *, pid=None, evidence=None):
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


class _WindowsJob:
    """A kill-on-close Job Object queried and terminated only by handle."""

    JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION = 1
    JOB_OBJECT_BASIC_PROCESS_ID_LIST = 3
    JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000

    def __init__(self):
        import ctypes
        from ctypes import wintypes

        version = os.sys.getwindowsversion()
        if (int(version.major), int(version.minor)) < (6, 2):
            raise ProcessLifecycleError(
                "nested_process_jobs_unsupported",
                evidence={"windows_version": f"{version.major}.{version.minor}"},
            )
        self.ctypes = ctypes
        self.wintypes = wintypes
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.ntdll = ctypes.WinDLL("ntdll", use_last_error=True)
        self._closed = False
        self._handle_lock = threading.RLock()

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
        self._extended_type = JOBOBJECT_EXTENDED_LIMIT_INFORMATION
        self.kernel32.CreateJobObjectW.argtypes = [
            ctypes.c_void_p,
            wintypes.LPCWSTR,
        ]
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
        self.kernel32.TerminateJobObject.argtypes = [
            wintypes.HANDLE,
            wintypes.UINT,
        ]
        self.kernel32.TerminateJobObject.restype = wintypes.BOOL
        self.kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        self.kernel32.CloseHandle.restype = wintypes.BOOL
        self.kernel32.SetHandleInformation.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.DWORD,
        ]
        self.kernel32.SetHandleInformation.restype = wintypes.BOOL
        self.ntdll.NtResumeProcess.argtypes = [wintypes.HANDLE]
        self.ntdll.NtResumeProcess.restype = ctypes.c_long

        self._handle = self.kernel32.CreateJobObjectW(None, None)
        if not self._handle:
            self._raise_last_error("process_job_create_failed")
        # CreateJobObject receives no SECURITY_ATTRIBUTES, which is already
        # non-inheritable.  Clear HANDLE_FLAG_INHERIT explicitly so the
        # kill-on-last-handle-close invariant is visible and testable.
        if not self.kernel32.SetHandleInformation(self._handle, 0x00000001, 0):
            error = ctypes.get_last_error()
            self.close()
            raise ProcessLifecycleError(
                "process_job_inherit_clear_failed",
                evidence={"winerror": error},
            )
        limits = self._extended_type()
        limits.BasicLimitInformation.LimitFlags = (
            self.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        )
        if not self.kernel32.SetInformationJobObject(
            self._handle,
            self.JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(limits),
            ctypes.sizeof(limits),
        ):
            error = ctypes.get_last_error()
            self.close()
            raise ProcessLifecycleError(
                "process_job_limit_failed",
                evidence={"winerror": error},
            )

    def _raise_last_error(self, token, *, pid=None):
        raise ProcessLifecycleError(
            token,
            pid=pid,
            evidence={"winerror": self.ctypes.get_last_error()},
        )

    @staticmethod
    def creationflags(requested_flags=0):
        requested_flags = int(requested_flags)
        if requested_flags & WINDOWS_CREATE_BREAKAWAY_FROM_JOB:
            raise ProcessLifecycleError("process_job_breakaway_forbidden")
        if requested_flags & (
            WINDOWS_DETACHED_PROCESS | WINDOWS_CREATE_NEW_CONSOLE
        ):
            raise ProcessLifecycleError("process_console_window_forbidden")
        return (
            requested_flags
            | WINDOWS_CREATE_SUSPENDED
            | WINDOWS_CREATE_NO_WINDOW
        )

    @staticmethod
    def startupinfo(requested=None):
        """Hide the first top-level window without mutating caller state.

        ``CREATE_NO_WINDOW`` prevents console allocation for console-subsystem
        executables.  Some of our background tools are GUI-subsystem programs,
        though, and can still show/activate their first window.  Supplying
        ``SW_HIDE`` closes that gap while manual/user-owned handoffs continue
        to use the separate external launch path unchanged.
        """

        startupinfo = (
            copy.copy(requested)
            if requested is not None
            else subprocess.STARTUPINFO()
        )
        startupinfo.dwFlags |= WINDOWS_STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = WINDOWS_SW_HIDE
        return startupinfo

    def assign(self, process):
        with self._handle_lock:
            if self._closed or not self._handle:
                raise ProcessLifecycleError("process_job_already_closed")
            handle = self.wintypes.HANDLE(int(process._handle))
            if not self.kernel32.AssignProcessToJobObject(self._handle, handle):
                self._raise_last_error(
                    "process_job_assign_failed",
                    pid=getattr(process, "pid", None),
                )

    def resume(self, process):
        with self._handle_lock:
            if self._closed or not self._handle:
                raise ProcessLifecycleError("process_job_already_closed")
            handle = self.wintypes.HANDLE(int(process._handle))
            status = int(self.ntdll.NtResumeProcess(handle))
            if status != 0:
                raise ProcessLifecycleError(
                    "process_resume_failed",
                    pid=getattr(process, "pid", None),
                    evidence={"ntstatus": f"0x{status & 0xFFFFFFFF:08x}"},
                )

    def active_process_count(self):
        with self._handle_lock:
            if self._closed or not self._handle:
                return 0
            accounting = self._accounting_type()
            if not self.kernel32.QueryInformationJobObject(
                self._handle,
                self.JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION,
                self.ctypes.byref(accounting),
                self.ctypes.sizeof(accounting),
                None,
            ):
                self._raise_last_error("process_job_query_failed")
            return int(accounting.ActiveProcesses)

    def process_ids(self):
        with self._handle_lock:
            active = self.active_process_count()
            if active <= 0:
                return []
            capacity = max(8, active + 4)
            pointer_type = self.ctypes.c_size_t
            header_size = self.ctypes.sizeof(self.wintypes.DWORD) * 2
            for _attempt in range(8):
                buffer = self.ctypes.create_string_buffer(
                    header_size + self.ctypes.sizeof(pointer_type) * capacity
                )
                if not self.kernel32.QueryInformationJobObject(
                    self._handle,
                    self.JOB_OBJECT_BASIC_PROCESS_ID_LIST,
                    buffer,
                    self.ctypes.sizeof(buffer),
                    None,
                ):
                    error = self.ctypes.get_last_error()
                    if error == 234:  # ERROR_MORE_DATA during membership churn
                        capacity *= 2
                        continue
                    self._raise_last_error("process_job_pid_query_failed")
                assigned = self.wintypes.DWORD.from_buffer(buffer, 0).value
                listed = self.wintypes.DWORD.from_buffer(
                    buffer, self.ctypes.sizeof(self.wintypes.DWORD)
                ).value
                if assigned > capacity:
                    capacity = max(capacity * 2, int(assigned) + 4)
                    continue
                offset = header_size
                return [
                    int(pointer_type.from_buffer(
                        buffer,
                        offset + index * self.ctypes.sizeof(pointer_type),
                    ).value)
                    for index in range(min(int(listed), capacity))
                ]
            raise ProcessLifecycleError("process_job_pid_query_unstable")

    def is_empty(self):
        return self.active_process_count() == 0

    def resource_usage(self):
        """Return cumulative CPU and peak commit for this exact process tree."""
        with self._handle_lock:
            if self._closed or not self._handle:
                return None
            accounting = self._accounting_type()
            if not self.kernel32.QueryInformationJobObject(
                self._handle,
                self.JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION,
                self.ctypes.byref(accounting),
                self.ctypes.sizeof(accounting),
                None,
            ):
                self._raise_last_error("process_job_accounting_query_failed")
            extended = self._extended_type()
            if not self.kernel32.QueryInformationJobObject(
                self._handle,
                self.JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                self.ctypes.byref(extended),
                self.ctypes.sizeof(extended),
                None,
            ):
                self._raise_last_error("process_job_memory_query_failed")
            return {
                "user_cpu_seconds": round(
                    int(accounting.TotalUserTime) / 10_000_000.0,
                    6,
                ),
                "kernel_cpu_seconds": round(
                    int(accounting.TotalKernelTime) / 10_000_000.0,
                    6,
                ),
                "page_fault_count": int(accounting.TotalPageFaultCount),
                "total_processes": int(accounting.TotalProcesses),
                "peak_process_memory_bytes": int(
                    extended.PeakProcessMemoryUsed
                ),
                "peak_job_memory_bytes": int(extended.PeakJobMemoryUsed),
            }

    def wait_empty(self, timeout):
        deadline = time.monotonic() + max(0.0, float(timeout))
        while True:
            if self.is_empty():
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            time.sleep(min(0.01, remaining))

    def terminate(self, *, pid=None, exit_code=1):
        with self._handle_lock:
            if self.is_empty():
                return False
            if not self.kernel32.TerminateJobObject(self._handle, int(exit_code)):
                self._raise_last_error("process_job_terminate_failed", pid=pid)
            return True

    def close(self):
        with self._handle_lock:
            if self._closed:
                return
            self._closed = True
            handle = getattr(self, "_handle", None)
            if handle:
                self.kernel32.CloseHandle(handle)
                self._handle = None


def _process_start_identity(process):
    if os.name != "nt":
        return None
    if getattr(process, "_handle", None) is None:
        return None
    import ctypes
    from ctypes import wintypes

    creation = wintypes.FILETIME()
    exit_time = wintypes.FILETIME()
    kernel = wintypes.FILETIME()
    user = wintypes.FILETIME()
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetProcessTimes.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
    ]
    kernel32.GetProcessTimes.restype = wintypes.BOOL
    handle = wintypes.HANDLE(int(process._handle))
    if not kernel32.GetProcessTimes(
        handle,
        ctypes.byref(creation),
        ctypes.byref(exit_time),
        ctypes.byref(kernel),
        ctypes.byref(user),
    ):
        return None
    return str((int(creation.dwHighDateTime) << 32) | int(creation.dwLowDateTime))


def _current_process_start_identity():
    if os.name != "nt":
        return None
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetCurrentProcess.argtypes = []
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE

    class _CurrentProcess:
        _handle = kernel32.GetCurrentProcess()

    return _process_start_identity(_CurrentProcess())


def process_identity_is_alive(pid, process_start_identity):
    """Read-only liveness check guarded by PID + creation-time identity."""

    if os.name != "nt":
        try:
            os.kill(int(pid), 0)
        except (OSError, ValueError):
            return False
        return True
    import ctypes
    from ctypes import wintypes

    process_query_limited_information = 0x1000
    still_active = 259
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetExitCodeProcess.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.OpenProcess(
        process_query_limited_information,
        False,
        int(pid),
    )
    if not handle:
        # ERROR_INVALID_PARAMETER is the documented result for a PID that does
        # not identify a process.  Access denied and all other query failures
        # are observational uncertainty, not proof of absence.  Recovery must
        # therefore fail closed and leave the running receipt untouched.
        return int(ctypes.get_last_error()) != 87
    try:
        class _OpenedProcess:
            _handle = handle

        observed_identity = _process_start_identity(_OpenedProcess())
        if observed_identity is None:
            return True
        if (
            process_start_identity is not None
            and observed_identity != str(process_start_identity)
        ):
            return False
        exit_code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return True
        return int(exit_code.value) == still_active
    finally:
        kernel32.CloseHandle(handle)


def _active_receipt_directory(directory):
    return Path(directory) / ACTIVE_RECEIPT_DIRECTORY_NAME


def _active_receipt_marker(directory, run_id):
    return _active_receipt_directory(directory) / f"{run_id}.active"


def _publish_active_receipt_marker(directory, run_id):
    marker = _active_receipt_marker(directory, run_id)
    if marker.is_file():
        return marker
    marker.parent.mkdir(parents=True, exist_ok=True)
    temporary = marker.with_name(
        f".{marker.name}.{os.getpid()}.{threading.get_ident()}.tmp"
    )
    try:
        temporary.write_text(
            f"active_receipt_index_v{ACTIVE_RECEIPT_INDEX_VERSION}\n",
            encoding="ascii",
        )
        os.replace(temporary, marker)
    finally:
        try:
            temporary.unlink()
        except OSError:
            pass
    return marker


def _remove_active_receipt_marker(directory, run_id):
    try:
        _active_receipt_marker(directory, run_id).unlink()
    except OSError:
        pass


def _receipt_candidates(directory):
    """Return active receipts plus one legacy migration scan."""
    directory = Path(directory)
    active_directory = _active_receipt_directory(directory)
    candidates = {
        directory / f"{marker.stem}.json"
        for marker in active_directory.glob("*.active")
    } if active_directory.is_dir() else set()
    migration_marker = directory / ACTIVE_RECEIPT_MIGRATION_MARKER
    legacy_migration = not migration_marker.is_file()
    if legacy_migration:
        candidates.update(directory.glob("*.json"))
    return candidates, legacy_migration, migration_marker


def _complete_active_receipt_migration(marker):
    temporary = marker.with_name(
        f".{marker.name}.{os.getpid()}.{threading.get_ident()}.tmp"
    )
    try:
        temporary.write_text(
            f"active_receipt_index_v{ACTIVE_RECEIPT_INDEX_VERSION}\n",
            encoding="ascii",
        )
        os.replace(temporary, marker)
    finally:
        try:
            temporary.unlink()
        except OSError:
            pass


def _receipt_mtime_ns(path):
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return 0


def recover_incomplete_receipts(receipt_dir=None):
    """Seal receipts whose exact owner identity is no longer alive.

    A forcibly terminated supervisor cannot execute Python cleanup code, but
    Windows closes every Job handle in that process.  The next launcher (or a
    diagnostic test) can therefore record the already-enforced kill-on-close
    outcome without discovering or signalling any PID.
    """

    directory = Path(receipt_dir or _default_receipt_dir())
    if not directory.is_dir():
        return []
    recovered = []
    candidates, legacy_migration, migration_marker = _receipt_candidates(
        directory
    )
    terminal_receipts = []
    for path in sorted(candidates):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            _remove_active_receipt_marker(directory, path.stem)
            continue
        if payload.get("schema_version") != RECEIPT_SCHEMA_VERSION:
            _remove_active_receipt_marker(directory, path.stem)
            continue
        if payload.get("status") not in {"running", "shutting_down"}:
            terminal_receipts.append(path)
            _remove_active_receipt_marker(directory, path.stem)
            continue
        owner = payload.get("owner") or {}
        pid = owner.get("pid")
        identity = owner.get("process_start_identity")
        if (
            not isinstance(pid, int)
            or not identity
            or process_identity_is_alive(pid, identity)
        ):
            _publish_active_receipt_marker(directory, path.stem)
            continue
        completed_at = _utc_now()
        payload["status"] = "recovered_forced_owner_exit"
        payload["shutdown_reason"] = "owner_process_terminated"
        payload["completed_at"] = completed_at
        payload["survivors"] = None
        payload["survivor_observation"] = "unavailable_after_owner_exit"
        payload["cleanup_guarantee"] = (
            "windows_kill_on_job_close_enforced_but_exit_not_observed"
            if os.name == "nt"
            else "owner_identity_absent_without_descendant_observation"
        )
        for entry in payload.get("owned_processes") or []:
            if entry.get("state") in {"completed", "cancelled"}:
                continue
            entry["state"] = "terminated_with_owner"
            entry["ended_at"] = completed_at
            entry["graceful_result"] = entry.get("graceful_result") or (
                "owner_terminated_before_graceful_completion"
            )
            entry["forced_result"] = "session_job_closed_with_owner"
            entry["cleanup_state"] = "owner_exit_job_cleanup_not_observed"
        temporary = path.with_name(
            f".{path.name}.{os.getpid()}.{threading.get_ident()}.recover.tmp"
        )
        try:
            temporary.write_text(
                json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True)
                + "\n",
                encoding="utf-8",
            )
            os.replace(temporary, path)
        except OSError:
            try:
                temporary.unlink()
            except OSError:
                pass
            continue
        recovered.append(path)
        terminal_receipts.append(path)
        _remove_active_receipt_marker(directory, path.stem)
    if legacy_migration:
        for path in sorted(
            terminal_receipts,
            key=_receipt_mtime_ns,
            reverse=True,
        )[MAX_TERMINAL_RECEIPTS:]:
            try:
                path.unlink()
            except OSError:
                pass
        try:
            _complete_active_receipt_migration(migration_marker)
        except OSError:
            # A failed marker write is safe: the next process repeats the
            # legacy scan instead of ever skipping a possibly active receipt.
            pass
    return recovered


def _command_evidence(command):
    values = [str(value) for value in command]
    encoded = json.dumps(values, ensure_ascii=False).encode("utf-8")
    return {
        "executable": values[0] if values else "",
        "argv_sha256": hashlib.sha256(encoded).hexdigest(),
        "argv": [value[:512] for value in values[:32]],
        "argv_truncated": len(values) > 32,
    }


def _default_receipt_dir():
    configured = os.environ.get("SPEEDTREE_PROCESS_RECEIPT_DIR")
    if configured:
        return Path(configured).expanduser()
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / "SpeedTreeBatchTools" / "process_receipts"
    return Path.cwd() / ".speedtree_process_receipts"


class ProcessSupervisor:
    """One GUI/CLI session and its exact set of owned process trees."""

    def __init__(self, launch_source=None, *, receipt_dir=None):
        self.run_id = uuid.uuid4().hex
        self.launch_source = str(
            launch_source
            or os.environ.get("SPEEDTREE_BATCH_LAUNCH_SOURCE")
            or f"direct:{Path(os.sys.argv[0]).name}"
        )
        self.owner_pid = os.getpid()
        self.owner_start_identity = _current_process_start_identity()
        if os.name == "nt" and self.owner_start_identity is None:
            raise ProcessLifecycleError("process_owner_identity_unavailable")
        self.started_at = _utc_now()
        self.completed_at = None
        self.status = "running"
        self.shutdown_reason = None
        self.grace_seconds = None
        self.kill_grace_seconds = None
        self.survivors = []
        self.entries = []
        self._entry_by_process_id = {}
        self._lock = threading.RLock()
        self._launch_gate = threading.RLock()
        self._closed = False
        self._session_job = _WindowsJob() if os.name == "nt" else None
        self.receipt_dir = Path(receipt_dir or _default_receipt_dir())
        self.receipt_path = self.receipt_dir / f"{self.run_id}.json"
        try:
            self._write_receipt()
        except BaseException:
            if self._session_job is not None:
                self._session_job.close()
            raise

    def _public_entry(self, entry):
        return {
            key: value
            for key, value in entry.items()
            if not key.startswith("_")
        }

    def receipt(self):
        with self._lock:
            return {
                "schema_version": RECEIPT_SCHEMA_VERSION,
                "run_id": self.run_id,
                "launch_source": self.launch_source,
                "owner": {
                    "pid": self.owner_pid,
                    "process_start_identity": self.owner_start_identity,
                },
                "started_at": self.started_at,
                "completed_at": self.completed_at,
                "status": self.status,
                "shutdown_reason": self.shutdown_reason,
                "grace_seconds": self.grace_seconds,
                "kill_grace_seconds": self.kill_grace_seconds,
                "contract": {
                    "windows_pre_resume_assignment": os.name == "nt",
                    "session_kill_on_close": os.name == "nt",
                    "pid_based_termination": False,
                },
                "owned_processes": [
                    self._public_entry(entry)
                    for entry in self.entries
                    if entry["ownership"] == "tool_owned"
                ],
                "external_handoffs": [
                    self._public_entry(entry)
                    for entry in self.entries
                    if entry["ownership"] != "tool_owned"
                ],
                "survivors": list(self.survivors),
            }

    def _write_receipt(self):
        payload = self.receipt()
        try:
            self.receipt_dir.mkdir(parents=True, exist_ok=True)
            temporary = self.receipt_path.with_name(
                f".{self.receipt_path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
            )
            temporary.write_text(
                json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True)
                + "\n",
                encoding="utf-8",
            )
            os.replace(temporary, self.receipt_path)
            if self.status in {"running", "shutting_down"}:
                _publish_active_receipt_marker(
                    self.receipt_dir,
                    self.run_id,
                )
            else:
                _remove_active_receipt_marker(
                    self.receipt_dir,
                    self.run_id,
                )
        except OSError as exc:
            raise ProcessLifecycleError(
                "process_receipt_write_failed",
                evidence={"path": str(self.receipt_path), "error": str(exc)},
            ) from exc

    def _register_owned(
        self,
        process,
        tree_job,
        command,
        source,
        *,
        identity_required=True,
        cooperative_cancel=None,
    ):
        process_start_identity = _process_start_identity(process)
        if identity_required and os.name == "nt" and process_start_identity is None:
            raise ProcessLifecycleError(
                "process_child_identity_unavailable",
                pid=getattr(process, "pid", None),
            )
        entry = {
            "launch_id": uuid.uuid4().hex,
            "ownership": "tool_owned",
            "source": str(source),
            "pid": int(process.pid),
            "process_start_identity": process_start_identity,
            "started_at": _utc_now(),
            "ended_at": None,
            "state": "suspended_owned",
            "returncode": None,
            "cooperative_result": None,
            "graceful_result": None,
            "forced_result": None,
            "cleanup_state": "active",
            "command": _command_evidence(command),
            "_process": process,
            "_tree_job": tree_job,
            "_cooperative_cancel": cooperative_cancel,
        }
        with self._lock:
            self.entries.append(entry)
            self._entry_by_process_id[id(process)] = entry
            self._write_receipt()
        process.speedtree_lifecycle_launch_id = entry["launch_id"]
        process.speedtree_lifecycle_tree_job = tree_job
        return entry

    def _register_external(self, process, command, source, ownership):
        entry = {
            "launch_id": uuid.uuid4().hex,
            "ownership": str(ownership),
            "source": str(source),
            "pid": int(process.pid) if process is not None else None,
            "process_start_identity": (
                _process_start_identity(process) if process is not None else None
            ),
            "started_at": _utc_now(),
            "ended_at": None,
            "state": "handed_off",
            "returncode": None,
            "intentional_survival": True,
            "command": _command_evidence(command),
            "_process": process,
            "_tree_job": None,
        }
        with self._lock:
            self.entries.append(entry)
            if process is not None:
                self._entry_by_process_id[id(process)] = entry
            self._write_receipt()
        return entry

    def entry_for(self, process):
        with self._lock:
            return self._entry_by_process_id.get(id(process))

    def spawn_owned(
        self,
        command,
        *,
        source,
        popen_factory=_DEFAULT_POPEN,
        cooperative_cancel=None,
        **kwargs,
    ):
        with self._launch_gate:
            return self._spawn_owned_locked(
                command,
                source=source,
                popen_factory=popen_factory,
                cooperative_cancel=cooperative_cancel,
                **kwargs,
            )

    def _spawn_owned_locked(
        self,
        command,
        *,
        source,
        popen_factory=_DEFAULT_POPEN,
        cooperative_cancel=None,
        **kwargs,
    ):
        if self._closed or self.status != "running":
            raise ProcessLifecycleError("process_supervisor_not_running")
        args = [str(value) for value in command]
        if not args:
            raise ValueError("command must not be empty")
        tree_job = None
        process = None
        real_windows_launch = os.name == "nt" and popen_factory is _DEFAULT_POPEN
        if real_windows_launch:
            tree_job = _WindowsJob()
            kwargs["creationflags"] = _WindowsJob.creationflags(
                kwargs.get("creationflags", 0)
            )
            kwargs["startupinfo"] = _WindowsJob.startupinfo(
                kwargs.get("startupinfo")
            )
        try:
            process = popen_factory(args, **kwargs)
            if real_windows_launch:
                self._session_job.assign(process)
                tree_job.assign(process)
            entry = self._register_owned(
                process,
                tree_job,
                args,
                source,
                identity_required=real_windows_launch,
                cooperative_cancel=cooperative_cancel,
            )
            if real_windows_launch:
                tree_job.resume(process)
            entry["state"] = "running"
            # The pre-resume receipt written by ``_register_owned`` is already
            # the durable ownership proof.  Rewriting the entire cumulative
            # session receipt immediately after resume adds O(processes^2)
            # JSON and ReplaceFile work to large first-run batches without
            # strengthening recovery: every nonterminal recorded state is
            # terminated with the owner.  Keep the live state in memory and
            # persist it with the next meaningful transition/completion.
            return process
        except BaseException:
            if process is not None and process.poll() is None:
                try:
                    process.kill()
                    process.wait(timeout=2)
                except BaseException:
                    pass
            if tree_job is not None:
                tree_job.close()
            raise

    def register_external_process(
        self, process, command, *, source, ownership="manual_handoff"
    ):
        return self._register_external(process, command, source, ownership)

    def spawn_external_process(
        self, command, *, source, ownership="manual_handoff", **kwargs
    ):
        args = [str(value) for value in command]
        with self._launch_gate:
            if self._closed or self.status != "running":
                raise ProcessLifecycleError("process_supervisor_not_running")
            process = _DEFAULT_POPEN(args, **kwargs)
            self._register_external(process, args, source, ownership)
            return process

    def register_external_target(
        self, command, *, source, ownership="manual_handoff"
    ):
        return self._register_external(None, command, source, ownership)

    def startfile_external_target(
        self, path, *, source, ownership="manual_handoff"
    ):
        with self._launch_gate:
            if self._closed or self.status != "running":
                raise ProcessLifecycleError("process_supervisor_not_running")
            os.startfile(path)
            return self._register_external(None, [str(path)], source, ownership)

    def _refresh_entry(self, entry):
        process = entry.get("_process")
        if process is None:
            return
        returncode = process.poll()
        entry["returncode"] = returncode
        if returncode is not None and entry.get("ended_at") is None:
            entry["ended_at"] = _utc_now()
        tree_job = entry.get("_tree_job")
        if entry["ownership"] != "tool_owned":
            if returncode is not None:
                entry["state"] = "external_exited"
            return
        if returncode is None:
            entry["state"] = "running"
            return
        if tree_job is not None and not tree_job.is_empty():
            entry["state"] = "root_exited_descendants_active"
        elif entry.get("cleanup_state") == "active":
            entry["state"] = "completed"
            entry["cleanup_state"] = "process_tree_clean"

    def _request_cooperative_cancel(self, entry, reason):
        if entry.get("cooperative_result") is not None:
            return entry["cooperative_result"]
        callback = entry.get("_cooperative_cancel")
        if callback is None:
            entry["cooperative_result"] = "not_available"
            return entry["cooperative_result"]
        try:
            callback()
            entry["cooperative_result"] = f"{reason}_requested"
        except BaseException as exc:
            entry["cooperative_result"] = (
                f"{reason}_request_failed:{type(exc).__name__}:{exc}"
            )
        return entry["cooperative_result"]

    def complete_owned(
        self,
        process,
        *,
        reason="completed",
        descendant_grace=DEFAULT_DESCENDANT_GRACE_SECONDS,
        kill_grace=DEFAULT_KILL_GRACE_SECONDS,
    ):
        entry = self.entry_for(process)
        if entry is None or entry["ownership"] != "tool_owned":
            raise ProcessLifecycleError(
                "process_not_owned",
                pid=getattr(process, "pid", None),
            )
        if process.poll() is None:
            raise ProcessLifecycleError(
                "process_completion_before_exit",
                pid=getattr(process, "pid", None),
            )
        tree_job = entry.get("_tree_job")
        forced = False
        if tree_job is not None and not tree_job.wait_empty(descendant_grace):
            forced = tree_job.terminate(pid=process.pid)
            if not tree_job.wait_empty(kill_grace):
                survivors = tree_job.process_ids()
                entry["cleanup_state"] = "survivors"
                self._write_receipt()
                raise ProcessLifecycleError(
                    "process_tree_kill_grace_expired",
                    pid=process.pid,
                    evidence={"survivors": survivors},
                )
        entry["returncode"] = process.poll()
        entry["ended_at"] = entry.get("ended_at") or _utc_now()
        entry["state"] = "completed"
        entry["cleanup_state"] = (
            "process_tree_forced_after_root_exit" if forced else "process_tree_clean"
        )
        if forced:
            entry["forced_result"] = str(reason)
        if tree_job is not None:
            resource_usage = getattr(tree_job, "resource_usage", None)
            if callable(resource_usage):
                entry["resource_usage"] = resource_usage()
            tree_job.close()
        self._write_receipt()
        return entry["cleanup_state"]

    def terminate_owned(
        self,
        process,
        *,
        reason="cancelled",
        terminate_grace=DEFAULT_GRACE_SECONDS,
        kill_grace=DEFAULT_KILL_GRACE_SECONDS,
    ):
        entry = self.entry_for(process)
        if entry is None or entry["ownership"] != "tool_owned":
            raise ProcessLifecycleError(
                "process_not_owned",
                pid=getattr(process, "pid", None),
            )
        tree_job = entry.get("_tree_job")
        windows_job_owned = os.name == "nt" and tree_job is not None
        if process.poll() is None:
            self._request_cooperative_cancel(entry, reason)
        else:
            entry["cooperative_result"] = (
                entry.get("cooperative_result") or f"{reason}_after_exit"
            )
        if process.poll() is None and not windows_job_owned:
            try:
                process.terminate()
                entry["graceful_result"] = f"{reason}_terminate_signal_sent"
            except (ProcessLookupError, OSError) as exc:
                if process.poll() is None:
                    raise ProcessLifecycleError(
                        "process_terminate_failed",
                        pid=process.pid,
                        evidence={"error": str(exc)},
                    ) from exc
                entry["graceful_result"] = f"{reason}_terminate_exit_race"
        deadline = time.monotonic() + max(0.0, float(terminate_grace))
        while True:
            root_done = process.poll() is not None
            tree_done = tree_job is None or tree_job.is_empty()
            if root_done and tree_done:
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(0.01, remaining))
        forced = False
        if tree_job is not None and not tree_job.is_empty():
            forced = tree_job.terminate(pid=process.pid)
            entry["forced_result"] = f"{reason}_job_terminated"
            if not tree_job.wait_empty(kill_grace):
                survivors = tree_job.process_ids()
                entry["cleanup_state"] = "survivors"
                self._write_receipt()
                raise ProcessLifecycleError(
                    "process_tree_kill_grace_expired",
                    pid=process.pid,
                    evidence={"survivors": survivors},
                )
        elif process.poll() is None:
            try:
                process.kill()
                entry["forced_result"] = f"{reason}_process_killed"
                forced = True
            except (ProcessLookupError, OSError) as exc:
                if process.poll() is None:
                    raise ProcessLifecycleError(
                        "process_kill_failed",
                        pid=process.pid,
                        evidence={"error": str(exc)},
                    ) from exc
        try:
            process.wait(timeout=max(0.0, float(kill_grace)))
        except subprocess.TimeoutExpired as exc:
            if process.poll() is None:
                raise ProcessLifecycleError(
                    "process_kill_grace_expired",
                    pid=process.pid,
                ) from exc
        entry["returncode"] = process.poll()
        entry["ended_at"] = _utc_now()
        entry["state"] = "cancelled"
        entry["cleanup_state"] = "process_tree_clean"
        if entry.get("graceful_result") is None:
            entry["graceful_result"] = (
                f"{reason}_not_used_on_windows_job"
                if windows_job_owned
                else f"{reason}_after_exit"
            )
        if tree_job is not None:
            tree_job.close()
        self._write_receipt()
        return f"{reason}_{'forced' if forced else 'graceful'}"

    def shutdown(
        self,
        *,
        reason="normal_exit",
        terminate_grace=DEFAULT_GRACE_SECONDS,
        kill_grace=DEFAULT_KILL_GRACE_SECONDS,
    ):
        with self._launch_gate:
            with self._lock:
                if self._closed:
                    return self.receipt()
                self.status = "shutting_down"
                self.shutdown_reason = str(reason)
                self.grace_seconds = float(terminate_grace)
                self.kill_grace_seconds = float(kill_grace)
                self._write_receipt()
                owned = [
                    entry for entry in self.entries
                    if entry["ownership"] == "tool_owned"
                ]

        for entry in owned:
            process = entry["_process"]
            tree_job = entry.get("_tree_job")
            if process.poll() is None:
                self._request_cooperative_cancel(entry, "shutdown")
                if os.name != "nt" or tree_job is None:
                    try:
                        process.terminate()
                        entry["graceful_result"] = (
                            "shutdown_terminate_signal_sent"
                        )
                    except (ProcessLookupError, OSError) as exc:
                        if process.poll() is None:
                            entry["graceful_result"] = (
                                f"shutdown_terminate_signal_failed:{exc}"
                            )
                else:
                    entry["graceful_result"] = (
                        "shutdown_not_used_on_windows_job"
                    )
            elif tree_job is not None and not tree_job.is_empty():
                entry["graceful_result"] = "root_already_exited"
            else:
                entry["graceful_result"] = "already_exited"

        deadline = time.monotonic() + max(0.0, float(terminate_grace))
        while time.monotonic() < deadline:
            if all(
                entry["_process"].poll() is not None
                and (
                    entry.get("_tree_job") is None
                    or entry["_tree_job"].is_empty()
                )
                for entry in owned
            ):
                break
            time.sleep(0.01)

        for entry in owned:
            process = entry["_process"]
            tree_job = entry.get("_tree_job")
            if tree_job is not None and not tree_job.is_empty():
                tree_job.terminate(pid=process.pid)
                entry["forced_result"] = "shutdown_job_terminated"
            elif tree_job is None and process.poll() is None:
                try:
                    process.kill()
                    entry["forced_result"] = "shutdown_process_killed"
                except (ProcessLookupError, OSError):
                    pass

        if self._session_job is not None and not self._session_job.wait_empty(
            kill_grace
        ):
            self._session_job.terminate(exit_code=1)
            self._session_job.wait_empty(kill_grace)

        survivors = []
        if self._session_job is not None and not self._session_job.is_empty():
            survivors = self._session_job.process_ids()

        for entry in owned:
            self._refresh_entry(entry)
            tree_job = entry.get("_tree_job")
            if tree_job is not None:
                tree_job.close()
            if entry.get("returncode") is not None:
                entry["state"] = "completed"
                entry["cleanup_state"] = "process_tree_clean"

        if self._session_job is not None:
            self._session_job.close()
        with self._lock:
            self.survivors = survivors
            self.status = "complete" if not survivors else "survivors"
            self.completed_at = _utc_now()
            self._closed = True
            self._write_receipt()
            return self.receipt()


def start_process_supervisor(launch_source=None, *, receipt_dir=None):
    global _SUPERVISOR
    with _SUPERVISOR_LOCK:
        if _SUPERVISOR is None or _SUPERVISOR._closed:
            recover_incomplete_receipts(receipt_dir)
            _SUPERVISOR = ProcessSupervisor(
                launch_source=launch_source,
                receipt_dir=receipt_dir,
            )
        return _SUPERVISOR


def get_process_supervisor():
    with _SUPERVISOR_LOCK:
        supervisor = _SUPERVISOR
    if supervisor is None:
        return start_process_supervisor()
    # A closed session may be replaced only by an explicit
    # start_process_supervisor() call.  Lazy callers racing GUI shutdown must
    # fail instead of silently creating a new uncoordinated ownership domain.
    return supervisor


def shutdown_process_supervisor(
    reason="normal_exit",
    *,
    terminate_grace=DEFAULT_GRACE_SECONDS,
    kill_grace=DEFAULT_KILL_GRACE_SECONDS,
):
    with _SUPERVISOR_LOCK:
        supervisor = _SUPERVISOR
    if supervisor is None:
        return None
    return supervisor.shutdown(
        reason=reason,
        terminate_grace=terminate_grace,
        kill_grace=kill_grace,
    )


def owned_popen(
    command: Iterable,
    *,
    source,
    popen_factory=_DEFAULT_POPEN,
    register_injected=False,
    cooperative_cancel=None,
    **kwargs,
):
    """Launch one exact tool-owned tree under the current supervisor."""

    if popen_factory is not _DEFAULT_POPEN and not register_injected:
        return popen_factory([str(value) for value in command], **kwargs)
    return get_process_supervisor().spawn_owned(
        command,
        source=source,
        popen_factory=popen_factory,
        cooperative_cancel=cooperative_cancel,
        **kwargs,
    )


def complete_owned_process(
    process,
    *,
    reason="completed",
    descendant_grace=DEFAULT_DESCENDANT_GRACE_SECONDS,
    kill_grace=DEFAULT_KILL_GRACE_SECONDS,
):
    return get_process_supervisor().complete_owned(
        process,
        reason=reason,
        descendant_grace=descendant_grace,
        kill_grace=kill_grace,
    )


def terminate_owned_process(
    process,
    *,
    reason="cancelled",
    terminate_grace=DEFAULT_GRACE_SECONDS,
    kill_grace=DEFAULT_KILL_GRACE_SECONDS,
):
    return get_process_supervisor().terminate_owned(
        process,
        reason=reason,
        terminate_grace=terminate_grace,
        kill_grace=kill_grace,
    )


def owned_run(*popenargs, source, input=None, capture_output=False, timeout=None,
              check=False, run_factory=None, **kwargs):
    """``subprocess.run`` semantics with exact owned-tree cleanup."""

    # Preserve existing injected/mocked subprocess seams without allowing a
    # real production launch to bypass ownership.
    if run_factory is not None and run_factory is not _DEFAULT_RUN:
        return run_factory(
            *popenargs,
            input=input,
            capture_output=capture_output,
            timeout=timeout,
            check=check,
            **kwargs,
        )

    if input is not None:
        if kwargs.get("stdin") is not None:
            raise ValueError("stdin and input arguments may not both be used")
        kwargs["stdin"] = subprocess.PIPE
    if capture_output:
        if kwargs.get("stdout") is not None or kwargs.get("stderr") is not None:
            raise ValueError(
                "stdout and stderr arguments may not be used with capture_output"
            )
        kwargs["stdout"] = subprocess.PIPE
        kwargs["stderr"] = subprocess.PIPE
    terminate_grace = float(kwargs.pop("owned_terminate_grace", DEFAULT_GRACE_SECONDS))
    kill_grace = float(kwargs.pop("owned_kill_grace", DEFAULT_KILL_GRACE_SECONDS))
    descendant_grace = float(
        kwargs.pop("owned_descendant_grace", DEFAULT_DESCENDANT_GRACE_SECONDS)
    )
    cooperative_cancel = kwargs.pop("owned_cooperative_cancel", None)
    effective_timeout = (
        DEFAULT_RUN_TIMEOUT_SECONDS if timeout is None else float(timeout)
    )
    process = owned_popen(
        *popenargs,
        source=source,
        cooperative_cancel=cooperative_cancel,
        **kwargs,
    )
    try:
        stdout, stderr = process.communicate(input, timeout=effective_timeout)
    except subprocess.TimeoutExpired as exc:
        terminate_owned_process(
            process,
            reason="timeout",
            terminate_grace=terminate_grace,
            kill_grace=kill_grace,
        )
        stdout, stderr = process.communicate()
        exc.output = stdout
        exc.stderr = stderr
        raise
    except BaseException:
        terminate_owned_process(
            process,
            reason="communication_error",
            terminate_grace=terminate_grace,
            kill_grace=kill_grace,
        )
        raise
    returncode = process.poll()
    complete_owned_process(
        process,
        reason="root_exit",
        descendant_grace=descendant_grace,
        kill_grace=kill_grace,
    )
    completed = subprocess.CompletedProcess(
        process.args,
        returncode,
        stdout,
        stderr,
    )
    entry = get_process_supervisor().entry_for(process)
    completed.resource_usage = copy.deepcopy(
        (entry or {}).get("resource_usage")
    )
    if check:
        completed.check_returncode()
    return completed


def external_handoff_popen(
    command: Iterable,
    *,
    source,
    ownership="manual_handoff",
    **kwargs,
):
    """Launch a visible/user-owned process that intentionally survives us."""

    return get_process_supervisor().spawn_external_process(
        command,
        source=source,
        ownership=ownership,
        **kwargs,
    )


def external_handoff_startfile(path, *, source, ownership="manual_handoff"):
    """Record and execute a Windows shell handoff without claiming its PID."""

    if os.name != "nt":
        raise OSError("os.startfile is available only on Windows")
    get_process_supervisor().startfile_external_target(
        path,
        source=source,
        ownership=ownership,
    )


def _atexit_shutdown():
    try:
        shutdown_process_supervisor("interpreter_exit")
    except BaseException:
        # Job handles still close during interpreter teardown.  Receipt errors
        # must not keep pythonw alive or broaden cleanup beyond owned Jobs.
        pass


atexit.register(_atexit_shutdown)


__all__ = [
    "DEFAULT_DESCENDANT_GRACE_SECONDS",
    "DEFAULT_GRACE_SECONDS",
    "DEFAULT_KILL_GRACE_SECONDS",
    "ProcessLifecycleError",
    "ProcessSupervisor",
    "WINDOWS_CREATE_NO_WINDOW",
    "WINDOWS_CREATE_SUSPENDED",
    "complete_owned_process",
    "external_handoff_popen",
    "external_handoff_startfile",
    "get_process_supervisor",
    "owned_popen",
    "owned_run",
    "process_identity_is_alive",
    "recover_incomplete_receipts",
    "shutdown_process_supervisor",
    "start_process_supervisor",
    "terminate_owned_process",
]
