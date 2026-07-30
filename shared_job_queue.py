"""Small persistent FIFO queue shared by the batch-tool GUI processes.

The queue deliberately stores data only; it never imports or executes an
application job.  Callers enqueue a JSON payload and the application that can
handle the job claims it when the job reaches the head of the global FIFO.

State changes are serialized with a Windows named mutex (or ``flock`` on
POSIX) and committed with ``os.replace``.  A claimed job is protected by a
short renewable lease so a process that exits without completing the job
cannot block the queue forever.
"""

from __future__ import annotations

import contextlib
import copy
import hashlib
import json
import os
import socket
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Iterator, Optional


SCHEMA_VERSION = 1
TERMINAL_STATUSES = frozenset(
    ("completed", "failed", "cancelled", "abandoned")
)
VALID_STATUSES = TERMINAL_STATUSES | frozenset(("queued", "running"))


def default_queue_state_path() -> Path:
    """Return the shared per-user queue path without creating it."""

    if os.name == "nt":
        local_app_data = os.environ.get("LOCALAPPDATA")
        root = (
            Path(local_app_data)
            if local_app_data
            else Path.home() / "AppData" / "Local"
        )
    else:
        state_home = os.environ.get("XDG_STATE_HOME")
        root = Path(state_home) if state_home else Path.home() / ".local" / "state"
    return root / "SpeedTreeBatchTools" / "shared_job_queue.json"


class QueueError(RuntimeError):
    """Base class for persistent queue errors."""


class QueueLockTimeout(QueueError):
    """The process-wide queue lock could not be acquired in time."""


class QueueStateError(QueueError):
    """The on-disk queue state is malformed or internally inconsistent."""


class JobNotFound(QueueError):
    """The requested job does not exist."""


class LeaseConflict(QueueError):
    """The caller does not own the job's current live lease."""


class JobNotCancellable(QueueError):
    """Only a job that is still waiting in the FIFO can be cancelled."""


def _json_copy(value: Any, *, field: str) -> Any:
    """Validate and detach caller-owned JSON data."""

    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False)
        return json.loads(encoded)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must contain only JSON values: {exc}") from exc


def _require_text(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must not be empty")
    return value.strip()


def _process_marker(pid: int) -> Optional[str]:
    """Return an OS creation marker that distinguishes a reused PID."""

    if pid <= 0:
        return None
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        class FILETIME(ctypes.Structure):
            _fields_ = (
                ("low", wintypes.DWORD),
                ("high", wintypes.DWORD),
            )

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        open_process = kernel32.OpenProcess
        open_process.argtypes = (
            wintypes.DWORD,
            wintypes.BOOL,
            wintypes.DWORD,
        )
        open_process.restype = wintypes.HANDLE
        get_process_times = kernel32.GetProcessTimes
        get_process_times.argtypes = (
            wintypes.HANDLE,
            ctypes.POINTER(FILETIME),
            ctypes.POINTER(FILETIME),
            ctypes.POINTER(FILETIME),
            ctypes.POINTER(FILETIME),
        )
        get_process_times.restype = wintypes.BOOL
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = (wintypes.HANDLE,)
        close_handle.restype = wintypes.BOOL

        handle = open_process(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
        if not handle:
            return None
        try:
            creation = FILETIME()
            exit_time = FILETIME()
            kernel_time = FILETIME()
            user_time = FILETIME()
            if not get_process_times(
                handle,
                ctypes.byref(creation),
                ctypes.byref(exit_time),
                ctypes.byref(kernel_time),
                ctypes.byref(user_time),
            ):
                return None
            return f"win:{(creation.high << 32) | creation.low}"
        finally:
            close_handle(handle)

    try:
        # Linux /proc field 22 is the process start clock tick.
        fields = Path(f"/proc/{pid}/stat").read_text(encoding="ascii").split()
        return f"proc:{fields[21]}"
    except (OSError, IndexError, UnicodeError):
        return None


def _local_process_alive(
    hostname: str,
    pid: int,
    expected_marker: Optional[str],
) -> Optional[bool]:
    """Return True/False for a local process, or None when unknowable."""

    if not isinstance(hostname, str) or (
        hostname.casefold() != socket.gethostname().casefold()
    ):
        return None
    if not isinstance(pid, int) or pid <= 0:
        return False

    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        open_process = kernel32.OpenProcess
        open_process.argtypes = (
            wintypes.DWORD,
            wintypes.BOOL,
            wintypes.DWORD,
        )
        open_process.restype = wintypes.HANDLE
        get_exit_code = kernel32.GetExitCodeProcess
        get_exit_code.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        get_exit_code.restype = wintypes.BOOL
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = (wintypes.HANDLE,)
        close_handle.restype = wintypes.BOOL

        handle = open_process(0x1000, False, pid)
        if not handle:
            error = ctypes.get_last_error()
            if error == 5:  # Access denied proves a protected process exists.
                return True
            if error == 87:  # Invalid parameter: the PID no longer exists.
                return False
            return None
        try:
            exit_code = wintypes.DWORD()
            if not get_exit_code(handle, ctypes.byref(exit_code)):
                return None
            if exit_code.value != 259:  # STILL_ACTIVE
                return False
        finally:
            close_handle(handle)
    else:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return None

    current_marker = _process_marker(pid)
    if expected_marker and current_marker:
        return current_marker == expected_marker
    return True


class _InterprocessMutex:
    """An abandoned-safe mutex scoped to one canonical state path."""

    def __init__(self, state_path: Path, timeout: float):
        self.state_path = state_path
        self.timeout = float(timeout)
        if self.timeout <= 0:
            raise ValueError("lock_timeout must be greater than zero")
        identity = os.path.normcase(str(state_path.resolve(strict=False)))
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32]
        self.windows_name = f"Local\\SpeedTreeBatchToolsQueue_{digest}"
        self.posix_path = state_path.with_name(f".{state_path.name}.lock")

    @contextlib.contextmanager
    def acquire(self) -> Iterator[None]:
        if os.name == "nt":
            with self._acquire_windows():
                yield
        else:
            with self._acquire_posix():
                yield

    @contextlib.contextmanager
    def _acquire_windows(self) -> Iterator[None]:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create_mutex = kernel32.CreateMutexW
        create_mutex.argtypes = (wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR)
        create_mutex.restype = wintypes.HANDLE
        wait_for_single = kernel32.WaitForSingleObject
        wait_for_single.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        wait_for_single.restype = wintypes.DWORD
        release_mutex = kernel32.ReleaseMutex
        release_mutex.argtypes = (wintypes.HANDLE,)
        release_mutex.restype = wintypes.BOOL
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = (wintypes.HANDLE,)
        close_handle.restype = wintypes.BOOL

        handle = create_mutex(None, False, self.windows_name)
        if not handle:
            raise OSError(
                ctypes.get_last_error(),
                f"CreateMutexW failed for {self.windows_name}",
            )

        acquired = False
        try:
            timeout_ms = max(1, min(round(self.timeout * 1000), 0xFFFFFFFE))
            result = wait_for_single(handle, timeout_ms)
            # WAIT_ABANDONED means the previous owning process ended without
            # releasing the mutex.  The current thread owns it and may safely
            # continue because queue files are atomically replaced.
            if result in (0x00000000, 0x00000080):
                acquired = True
            elif result == 0x00000102:
                raise QueueLockTimeout(
                    f"timed out locking shared queue: {self.state_path}"
                )
            else:
                raise OSError(
                    ctypes.get_last_error(),
                    f"WaitForSingleObject failed with status 0x{result:08x}",
                )
            yield
        finally:
            if acquired:
                release_mutex(handle)
            close_handle(handle)

    @contextlib.contextmanager
    def _acquire_posix(self) -> Iterator[None]:
        # The production target is Windows.  This fallback keeps the state
        # contract testable and usable by repository tooling on POSIX.
        import fcntl

        self.posix_path.parent.mkdir(parents=True, exist_ok=True)
        with self.posix_path.open("a+b") as handle:
            deadline = time.monotonic() + self.timeout
            while True:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise QueueLockTimeout(
                            f"timed out locking shared queue: {self.state_path}"
                        )
                    time.sleep(0.01)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class SharedJobQueue:
    """Persistent global FIFO with one renewable job lease at a time."""

    def __init__(
        self,
        state_path: os.PathLike[str] | str,
        *,
        lease_seconds: float = 30.0,
        lock_timeout: float = 10.0,
        clock: Callable[[], float] = time.time,
        process_alive: Callable[
            [str, int, Optional[str]], Optional[bool]
        ] = _local_process_alive,
    ):
        self.state_path = Path(state_path)
        self.lease_seconds = float(lease_seconds)
        if self.lease_seconds <= 0:
            raise ValueError("lease_seconds must be greater than zero")
        self._clock = clock
        self._process_alive = process_alive
        self._hostname = socket.gethostname()
        self._pid = os.getpid()
        self._process_marker = _process_marker(self._pid)
        self._mutex = _InterprocessMutex(self.state_path, lock_timeout)
        # Also prevents a same-thread recursive transaction from observing and
        # then replacing an intermediate state through one queue instance.
        self._thread_lock = threading.RLock()

    def enqueue(
        self,
        app_id: str,
        payload: Any,
        *,
        label: Optional[str] = None,
        metadata: Any = None,
        job_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Append an application job and return its detached stored record."""

        app_id = _require_text(app_id, field="app_id")
        job_id = _require_text(job_id or uuid.uuid4().hex, field="job_id")
        payload = _json_copy(payload, field="payload")
        metadata = _json_copy(metadata, field="metadata")
        label_value = None if label is None else str(label)

        def mutate(state: Dict[str, Any], now: float) -> Dict[str, Any]:
            if any(job["id"] == job_id for job in state["jobs"]):
                raise QueueError(f"job_id already exists: {job_id}")
            sequence = state["next_sequence"]
            state["next_sequence"] += 1
            job = {
                "id": job_id,
                "sequence": sequence,
                "app_id": app_id,
                "label": label_value,
                "payload": payload,
                "metadata": metadata,
                "status": "queued",
                "created_at": now,
                "origin": {
                    "pid": self._pid,
                    "hostname": self._hostname,
                    "process_marker": self._process_marker,
                },
                "attempts": 0,
                "recovery_count": 0,
                "lease": None,
            }
            state["jobs"].append(job)
            return job

        return self._change(mutate)

    def claim(
        self,
        owner_id: str,
        *,
        job_id: Optional[str] = None,
        accepted_apps: Optional[Iterable[str]] = None,
    ) -> Optional[Dict[str, Any]]:
        """Lease the FIFO head, or return ``None`` when it cannot be claimed.

        ``job_id`` binds a window's waiter to the exact job for which it owns
        the in-memory callback.  A different FIFO head is never skipped.

        ``accepted_apps`` is a routing guard for application-specific workers.
        It never skips an incompatible FIFO head; skipping would allow a later
        job to run concurrently or out of order.
        """

        owner_id = _require_text(owner_id, field="owner_id")
        expected_job_id = (
            None
            if job_id is None
            else _require_text(job_id, field="job_id")
        )
        accepted = None
        if accepted_apps is not None:
            accepted = {
                _require_text(value, field="accepted_apps item")
                for value in accepted_apps
            }

        def mutate(
            state: Dict[str, Any], now: float
        ) -> Optional[Dict[str, Any]]:
            if any(job["status"] == "running" for job in state["jobs"]):
                return None
            job = self._next_live_fifo_head(state, now)
            if job is None:
                return None
            if expected_job_id is not None and job["id"] != expected_job_id:
                return None
            if accepted is not None and job["app_id"] not in accepted:
                return None
            job["status"] = "running"
            job["attempts"] += 1
            job["lease"] = {
                "token": uuid.uuid4().hex,
                "owner_id": owner_id,
                "pid": self._pid,
                "hostname": self._hostname,
                "process_marker": self._process_marker,
                "claimed_at": now,
                "heartbeat_at": now,
                "expires_at": now + self.lease_seconds,
            }
            return job

        return self._change(mutate)

    def heartbeat(
        self,
        job_id: str,
        lease_token: str,
        *,
        owner_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Renew a live lease and return the updated job."""

        job_id = _require_text(job_id, field="job_id")
        lease_token = _require_text(lease_token, field="lease_token")
        expected_owner = (
            None
            if owner_id is None
            else _require_text(owner_id, field="owner_id")
        )

        def mutate(state: Dict[str, Any], now: float) -> Dict[str, Any]:
            job = self._find_job(state, job_id)
            lease = self._require_lease(job, lease_token, expected_owner)
            lease["heartbeat_at"] = now
            lease["expires_at"] = now + self.lease_seconds
            return job

        return self._change(mutate)

    def complete(
        self,
        job_id: str,
        lease_token: str,
        *,
        result: Any = None,
        success: bool = True,
        owner_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Finish a leased job and release the global execution slot."""

        job_id = _require_text(job_id, field="job_id")
        lease_token = _require_text(lease_token, field="lease_token")
        result = _json_copy(result, field="result")
        expected_owner = (
            None
            if owner_id is None
            else _require_text(owner_id, field="owner_id")
        )

        def mutate(state: Dict[str, Any], now: float) -> Dict[str, Any]:
            job = self._find_job(state, job_id)
            lease = self._require_lease(job, lease_token, expected_owner)
            job["status"] = "completed" if success else "failed"
            job["finished_at"] = now
            job["result"] = result
            job["last_lease"] = {
                key: value for key, value in lease.items() if key != "token"
            }
            job["lease"] = None
            return job

        return self._change(mutate)

    def cancel(
        self,
        job_id: str,
        *,
        reason: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Cancel a waiting job.  A running or terminal job is not changed."""

        job_id = _require_text(job_id, field="job_id")
        reason_value = None if reason is None else str(reason)

        def mutate(state: Dict[str, Any], now: float) -> Dict[str, Any]:
            job = self._find_job(state, job_id)
            if job["status"] != "queued":
                raise JobNotCancellable(
                    f"job {job_id} is {job['status']}; only queued jobs can cancel"
                )
            job["status"] = "cancelled"
            job["cancelled_at"] = now
            job["cancel_reason"] = reason_value
            return job

        return self._change(mutate)

    def get(self, job_id: str) -> Dict[str, Any]:
        """Return one job, recovering an expired lease first if necessary."""

        job_id = _require_text(job_id, field="job_id")

        def read(state: Dict[str, Any], _now: float) -> Dict[str, Any]:
            return self._find_job(state, job_id)

        return self._change(read)

    def snapshot(self) -> Dict[str, Any]:
        """Return a detached full queue snapshot."""

        return self._change(lambda state, _now: state)

    def _change(self, operation: Callable[[Dict[str, Any], float], Any]) -> Any:
        with self._thread_lock:
            with self._mutex.acquire():
                state = self._load_state()
                now = float(self._clock())
                before = copy.deepcopy(state)
                recovered = self._recover_expired_lease(state, now)
                result = operation(state, now)
                self._validate_state(state)
                if recovered or state != before:
                    state["revision"] += 1
                    state["updated_at"] = now
                    self._write_state(state)
                return copy.deepcopy(result)

    def _load_state(self) -> Dict[str, Any]:
        if not self.state_path.exists():
            return {
                "schema_version": SCHEMA_VERSION,
                "revision": 0,
                "next_sequence": 1,
                "updated_at": None,
                "jobs": [],
            }
        try:
            with self.state_path.open("r", encoding="utf-8") as handle:
                state = json.load(handle)
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise QueueStateError(
                f"could not read queue state {self.state_path}: {exc}"
            ) from exc
        self._validate_state(state)
        return state

    def _write_state(self, state: Dict[str, Any]) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        encoded = (
            json.dumps(
                state,
                ensure_ascii=False,
                allow_nan=False,
                indent=2,
                sort_keys=True,
            )
            + "\n"
        )
        temporary = self.state_path.with_name(
            f".{self.state_path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
        )
        try:
            with temporary.open("x", encoding="utf-8", newline="\n") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            self._replace_state(temporary)
            self._fsync_parent()
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    def _replace_state(self, temporary: Path) -> None:
        """Replace state, tolerating brief Windows scanner/reader locks."""

        deadline = time.monotonic() + 2.0
        delay = 0.005
        while True:
            try:
                os.replace(temporary, self.state_path)
                return
            except PermissionError as exc:
                # Windows can briefly reject replacement when an antivirus,
                # indexer, or just-closing reader still has the old snapshot
                # open.  The queue mutex prevents another writer, so retrying
                # this same complete temporary snapshot remains atomic.
                if (
                    os.name != "nt"
                    or getattr(exc, "winerror", None) not in (5, 32)
                    or time.monotonic() >= deadline
                ):
                    raise
                time.sleep(delay)
                delay = min(delay * 2, 0.1)

    def _fsync_parent(self) -> None:
        if os.name == "nt":
            return
        descriptor = os.open(self.state_path.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @staticmethod
    def _find_job(state: Dict[str, Any], job_id: str) -> Dict[str, Any]:
        for job in state["jobs"]:
            if job["id"] == job_id:
                return job
        raise JobNotFound(f"unknown queue job: {job_id}")

    @staticmethod
    def _require_lease(
        job: Dict[str, Any],
        lease_token: str,
        owner_id: Optional[str],
    ) -> Dict[str, Any]:
        lease = job.get("lease")
        if (
            job.get("status") != "running"
            or not isinstance(lease, dict)
            or lease.get("token") != lease_token
            or (owner_id is not None and lease.get("owner_id") != owner_id)
        ):
            raise LeaseConflict(
                f"job {job.get('id')} is not owned by this live lease"
            )
        return lease

    def _next_live_fifo_head(
        self,
        state: Dict[str, Any],
        now: float,
    ) -> Optional[Dict[str, Any]]:
        """Return the FIFO head after abandoning dead-origin waiting jobs."""

        waiting = sorted(
            (job for job in state["jobs"] if job["status"] == "queued"),
            key=lambda job: job["sequence"],
        )
        for job in waiting:
            origin = job["origin"]
            alive = self._process_alive(
                origin["hostname"],
                origin["pid"],
                origin.get("process_marker"),
            )
            if alive is not False:
                return job
            job["status"] = "abandoned"
            job["abandoned_at"] = now
            job["abandon_reason"] = "origin_process_exited"
        return None

    def _recover_expired_lease(
        self,
        state: Dict[str, Any],
        now: float,
    ) -> bool:
        """Finish an expired job only after its owning process is confirmed dead."""

        recovered = False
        for job in state["jobs"]:
            if job.get("status") != "running":
                continue
            lease = job.get("lease")
            if not isinstance(lease, dict) or lease.get("expires_at", 0) > now:
                continue
            alive = self._process_alive(
                lease["hostname"],
                lease["pid"],
                lease.get("process_marker"),
            )
            # Unknown liveness is treated conservatively as still owned.
            if alive is not False:
                continue
            audit = {
                key: value
                for key, value in (lease or {}).items()
                if key != "token"
            }
            audit["recovered_at"] = now
            job["last_expired_lease"] = audit
            job["recovery_count"] = int(job.get("recovery_count", 0)) + 1
            job["status"] = "failed"
            job["finished_at"] = now
            job["failure_reason"] = "owner_lost"
            job["result"] = None
            job["lease"] = None
            recovered = True
        return recovered

    @staticmethod
    def _validate_state(state: Any) -> None:
        if not isinstance(state, dict):
            raise QueueStateError("queue state root must be an object")
        if state.get("schema_version") != SCHEMA_VERSION:
            raise QueueStateError(
                f"unsupported queue schema: {state.get('schema_version')!r}"
            )
        if not isinstance(state.get("revision"), int) or state["revision"] < 0:
            raise QueueStateError("revision must be a non-negative integer")
        if (
            not isinstance(state.get("next_sequence"), int)
            or state["next_sequence"] < 1
        ):
            raise QueueStateError("next_sequence must be a positive integer")
        jobs = state.get("jobs")
        if not isinstance(jobs, list):
            raise QueueStateError("jobs must be an array")

        ids = set()
        sequences = set()
        running = 0
        for job in jobs:
            if not isinstance(job, dict):
                raise QueueStateError("each job must be an object")
            job_id = job.get("id")
            sequence = job.get("sequence")
            status = job.get("status")
            if not isinstance(job_id, str) or not job_id:
                raise QueueStateError("every job needs a non-empty id")
            if job_id in ids:
                raise QueueStateError(f"duplicate job id: {job_id}")
            ids.add(job_id)
            if not isinstance(sequence, int) or sequence < 1:
                raise QueueStateError(f"invalid sequence for job {job_id}")
            if sequence in sequences:
                raise QueueStateError(f"duplicate sequence: {sequence}")
            sequences.add(sequence)
            if status not in VALID_STATUSES:
                raise QueueStateError(f"invalid status for job {job_id}: {status}")
            if not isinstance(job.get("app_id"), str) or not job["app_id"]:
                raise QueueStateError(f"invalid app_id for job {job_id}")
            origin = job.get("origin")
            if (
                not isinstance(origin, dict)
                or not isinstance(origin.get("pid"), int)
                or origin["pid"] <= 0
                or not isinstance(origin.get("hostname"), str)
                or not origin["hostname"]
            ):
                raise QueueStateError(f"invalid origin for job {job_id}")
            if status == "running":
                running += 1
                lease = job.get("lease")
                if not isinstance(lease, dict):
                    raise QueueStateError(f"running job {job_id} has no lease")
                if not isinstance(lease.get("token"), str) or not lease["token"]:
                    raise QueueStateError(f"running job {job_id} has no token")
                if not isinstance(lease.get("expires_at"), (int, float)):
                    raise QueueStateError(
                        f"running job {job_id} has invalid expiry"
                    )
                if (
                    not isinstance(lease.get("pid"), int)
                    or lease["pid"] <= 0
                    or not isinstance(lease.get("hostname"), str)
                    or not lease["hostname"]
                ):
                    raise QueueStateError(
                        f"running job {job_id} has invalid owner process"
                    )
            elif job.get("lease") is not None:
                raise QueueStateError(f"non-running job {job_id} has a lease")
        if running > 1:
            raise QueueStateError("more than one job is running")
        if sequences and state["next_sequence"] <= max(sequences):
            raise QueueStateError("next_sequence does not follow stored jobs")


__all__ = [
    "JobNotCancellable",
    "JobNotFound",
    "LeaseConflict",
    "QueueError",
    "QueueLockTimeout",
    "QueueStateError",
    "SharedJobQueue",
    "default_queue_state_path",
]
