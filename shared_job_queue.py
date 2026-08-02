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
DEFAULT_MAX_TERMINAL_JOBS = 100
DEFAULT_FORCE_RELEASE_MIN_AGE_SECONDS = 15 * 60
TERMINAL_STATUSES = frozenset(
    ("completed", "failed", "cancelled", "abandoned")
)
VALID_STATUSES = TERMINAL_STATUSES | frozenset(("queued", "running"))


if os.name == "nt":
    # Bind the small kernel32 surface once.  Claim polling probes process
    # liveness frequently, so rebuilding the DLL wrapper and prototypes for
    # every queued job turns a cheap check into repeated setup work.
    import ctypes as _ctypes
    from ctypes import wintypes as _wintypes

    class _FILETIME(_ctypes.Structure):
        _fields_ = (
            ("low", _wintypes.DWORD),
            ("high", _wintypes.DWORD),
        )

    _KERNEL32 = _ctypes.WinDLL("kernel32", use_last_error=True)

    _OPEN_PROCESS = _KERNEL32.OpenProcess
    _OPEN_PROCESS.argtypes = (
        _wintypes.DWORD,
        _wintypes.BOOL,
        _wintypes.DWORD,
    )
    _OPEN_PROCESS.restype = _wintypes.HANDLE

    _GET_PROCESS_TIMES = _KERNEL32.GetProcessTimes
    _GET_PROCESS_TIMES.argtypes = (
        _wintypes.HANDLE,
        _ctypes.POINTER(_FILETIME),
        _ctypes.POINTER(_FILETIME),
        _ctypes.POINTER(_FILETIME),
        _ctypes.POINTER(_FILETIME),
    )
    _GET_PROCESS_TIMES.restype = _wintypes.BOOL

    _GET_EXIT_CODE_PROCESS = _KERNEL32.GetExitCodeProcess
    _GET_EXIT_CODE_PROCESS.argtypes = (
        _wintypes.HANDLE,
        _ctypes.POINTER(_wintypes.DWORD),
    )
    _GET_EXIT_CODE_PROCESS.restype = _wintypes.BOOL

    _CREATE_MUTEX = _KERNEL32.CreateMutexW
    _CREATE_MUTEX.argtypes = (
        _wintypes.LPVOID,
        _wintypes.BOOL,
        _wintypes.LPCWSTR,
    )
    _CREATE_MUTEX.restype = _wintypes.HANDLE

    _WAIT_FOR_SINGLE_OBJECT = _KERNEL32.WaitForSingleObject
    _WAIT_FOR_SINGLE_OBJECT.argtypes = (
        _wintypes.HANDLE,
        _wintypes.DWORD,
    )
    _WAIT_FOR_SINGLE_OBJECT.restype = _wintypes.DWORD

    _RELEASE_MUTEX = _KERNEL32.ReleaseMutex
    _RELEASE_MUTEX.argtypes = (_wintypes.HANDLE,)
    _RELEASE_MUTEX.restype = _wintypes.BOOL

    _CLOSE_HANDLE = _KERNEL32.CloseHandle
    _CLOSE_HANDLE.argtypes = (_wintypes.HANDLE,)
    _CLOSE_HANDLE.restype = _wintypes.BOOL


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


class ForceReleaseRejected(QueueError):
    """A running job did not satisfy the manual-release safety contract."""


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
        handle = _OPEN_PROCESS(
            0x1000, False, pid
        )  # QUERY_LIMITED_INFORMATION
        if not handle:
            return None
        try:
            creation = _FILETIME()
            exit_time = _FILETIME()
            kernel_time = _FILETIME()
            user_time = _FILETIME()
            if not _GET_PROCESS_TIMES(
                handle,
                _ctypes.byref(creation),
                _ctypes.byref(exit_time),
                _ctypes.byref(kernel_time),
                _ctypes.byref(user_time),
            ):
                return None
            return f"win:{(creation.high << 32) | creation.low}"
        finally:
            _CLOSE_HANDLE(handle)

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
        handle = _OPEN_PROCESS(0x1000, False, pid)
        if not handle:
            error = _ctypes.get_last_error()
            if error == 5:  # Access denied proves a protected process exists.
                return True
            if error == 87:  # Invalid parameter: the PID no longer exists.
                return False
            return None
        try:
            exit_code = _wintypes.DWORD()
            if not _GET_EXIT_CODE_PROCESS(
                handle, _ctypes.byref(exit_code)
            ):
                return None
            if exit_code.value != 259:  # STILL_ACTIVE
                return False
        finally:
            _CLOSE_HANDLE(handle)
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


class InterprocessMutex:
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
        handle = _CREATE_MUTEX(None, False, self.windows_name)
        if not handle:
            raise OSError(
                _ctypes.get_last_error(),
                f"CreateMutexW failed for {self.windows_name}",
            )

        acquired = False
        try:
            timeout_ms = max(1, min(round(self.timeout * 1000), 0xFFFFFFFE))
            result = _WAIT_FOR_SINGLE_OBJECT(handle, timeout_ms)
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
                    _ctypes.get_last_error(),
                    f"WaitForSingleObject failed with status 0x{result:08x}",
                )
            yield
        finally:
            if acquired:
                _RELEASE_MUTEX(handle)
            _CLOSE_HANDLE(handle)

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
        max_terminal_jobs: int = DEFAULT_MAX_TERMINAL_JOBS,
        force_release_min_age_seconds: float = (
            DEFAULT_FORCE_RELEASE_MIN_AGE_SECONDS
        ),
        clock: Callable[[], float] = time.time,
        process_alive: Callable[
            [str, int, Optional[str]], Optional[bool]
        ] = _local_process_alive,
    ):
        self.state_path = Path(state_path)
        self.lease_seconds = float(lease_seconds)
        if self.lease_seconds <= 0:
            raise ValueError("lease_seconds must be greater than zero")
        if isinstance(max_terminal_jobs, bool) or not isinstance(
            max_terminal_jobs, int
        ):
            raise ValueError("max_terminal_jobs must be a non-negative integer")
        if max_terminal_jobs < 0:
            raise ValueError("max_terminal_jobs must be a non-negative integer")
        self.max_terminal_jobs = max_terminal_jobs
        self.force_release_min_age_seconds = float(
            force_release_min_age_seconds
        )
        if self.force_release_min_age_seconds <= 0:
            raise ValueError(
                "force_release_min_age_seconds must be greater than zero"
            )
        self._clock = clock
        self._process_alive = process_alive
        self._hostname = socket.gethostname()
        self._pid = os.getpid()
        self._process_marker = _process_marker(self._pid)
        self._mutex = InterprocessMutex(self.state_path, lock_timeout)
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
            job = self._next_live_fifo_head(state, now)
            if any(row["status"] == "running" for row in state["jobs"]):
                return None
            if job is None:
                return None
            if expected_job_id is not None and job["id"] != expected_job_id:
                return None
            if accepted is not None and job["app_id"] not in accepted:
                return None
            return self._start_lease(job, owner_id, now)

        return self._change(mutate)

    def poll_for_turn(
        self,
        owner_id: str,
        *,
        job_id: str,
        accepted_apps: Optional[Iterable[str]] = None,
    ) -> Dict[str, Any]:
        """Recover, clean, and optionally exact-claim in one transaction.

        A waiting runtime must not decide from a stale read that a dead-origin
        FIFO head is ineligible forever.  This operation performs expired
        running recovery in ``_change()``, abandons every confirmed-dead queued
        head, then exact-claims ``job_id`` or returns the resulting snapshot.
        """

        owner_id = _require_text(owner_id, field="owner_id")
        expected_job_id = _require_text(job_id, field="job_id")
        accepted = None
        if accepted_apps is not None:
            accepted = {
                _require_text(value, field="accepted_apps item")
                for value in accepted_apps
            }

        def mutate(state: Dict[str, Any], now: float) -> Dict[str, Any]:
            head = self._next_live_fifo_head(state, now)
            running = next(
                (
                    row
                    for row in state["jobs"]
                    if row["status"] == "running"
                ),
                None,
            )
            claimed = False
            if (
                running is None
                and head is not None
                and head["id"] == expected_job_id
                and (accepted is None or head["app_id"] in accepted)
            ):
                self._start_lease(head, owner_id, now)
                claimed = True
            return {"claimed": claimed, "snapshot": state}

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

    def record_operator_close_request(
        self,
        job_id: str,
        lease_token: str,
        *,
        owner_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Persist that the owning UI was closed while the job was live.

        This is deliberately non-terminal.  The worker may still publish a
        normal result before its process exits; otherwise ordinary lease
        recovery later records ``owner_lost``.  Keeping these as two ordered
        events prevents the recovery reason from being mistaken for the
        batch's original outcome.
        """

        job_id = _require_text(job_id, field="job_id")
        lease_token = _require_text(lease_token, field="lease_token")
        expected_owner = (
            None if owner_id is None else _require_text(owner_id, field="owner_id")
        )

        def mutate(state: Dict[str, Any], now: float) -> Dict[str, Any]:
            job = self._find_job(state, job_id)
            lease = self._require_lease(job, lease_token, expected_owner)
            audit = job.get("termination_audit")
            if not isinstance(audit, dict):
                audit = {"schema_version": 1, "events": []}
                job["termination_audit"] = audit
            events = audit.get("events")
            if not isinstance(events, list):
                raise QueueStateError(
                    f"invalid termination audit for job {job_id}"
                )
            if any(
                isinstance(event, dict)
                and event.get("kind") == "operator_close_requested"
                for event in events
            ):
                return job
            events.append({
                "sequence": len(events) + 1,
                "id": uuid.uuid4().hex,
                "kind": "operator_close_requested",
                "at": now,
                "owner": {
                    key: value
                    for key, value in lease.items()
                    if key not in {"token", "expires_at", "heartbeat_at"}
                },
                "batch_outcome_at_event": "running",
            })
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
            job["terminal_at"] = now
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
            job["terminal_at"] = now
            job["cancel_reason"] = reason_value
            return job

        return self._change(mutate)

    def request_release(
        self,
        job_id: str,
        *,
        confirm_job_id: str,
    ) -> Dict[str, Any]:
        """Request that the live token owner stop its worker and release.

        The request is intentionally non-terminal and cannot unblock the FIFO.
        Only the current lease token owner can acknowledge it after stopping
        and joining the real worker.  If that owner exits, ordinary owner-lost
        recovery remains the only fallback.
        """

        job_id = _require_text(job_id, field="job_id")
        confirmation = _require_text(
            confirm_job_id,
            field="confirm_job_id",
        )
        if confirmation != job_id:
            raise ForceReleaseRejected(
                "release_confirmation_mismatch: confirmation must exactly "
                "match the running job ID"
            )

        def mutate(state: Dict[str, Any], now: float) -> Dict[str, Any]:
            job = self._find_job(state, job_id)
            lease = job.get("lease")
            if job.get("status") != "running" or not isinstance(lease, dict):
                raise ForceReleaseRejected(
                    f"release_job_not_running: job {job_id} is not a "
                    "running leased job"
                )
            claimed_at = lease.get("claimed_at")
            if not isinstance(claimed_at, (int, float)):
                raise ForceReleaseRejected(
                    f"release_claim_time_invalid: job {job_id} has no "
                    "valid claim timestamp"
                )
            running_seconds = max(0.0, now - float(claimed_at))
            if running_seconds < self.force_release_min_age_seconds:
                remaining = self.force_release_min_age_seconds - running_seconds
                raise ForceReleaseRejected(
                    f"release_min_age_not_met: job {job_id} is too new for "
                    "operator release; "
                    f"wait at least {remaining:.0f} more seconds"
                )
            owner_alive = self._process_alive(
                lease["hostname"],
                lease["pid"],
                lease.get("process_marker"),
            )
            if owner_alive is not True:
                raise ForceReleaseRejected(
                    f"release_owner_not_confirmed_alive: job {job_id} owner "
                    "is not confirmed alive; use the normal owner-lost "
                    "recovery path"
                )
            existing = job.get("release_request")
            if isinstance(existing, dict):
                return job

            job["release_request"] = {
                "id": uuid.uuid4().hex,
                "reason": "operator_release_requested",
                "requested_at": now,
                "running_seconds": running_seconds,
                "minimum_age_seconds": self.force_release_min_age_seconds,
                "owner_process_alive": True,
                "original_lease": {
                    key: value for key, value in lease.items() if key != "token"
                },
                "requester": {
                    "pid": self._pid,
                    "hostname": self._hostname,
                    "process_marker": self._process_marker,
                },
            }
            return job

        return self._change(mutate)

    def acknowledge_release(
        self,
        job_id: str,
        lease_token: str,
        *,
        request_id: str,
        owner_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Release only when the current token owner acknowledges a request."""

        job_id = _require_text(job_id, field="job_id")
        lease_token = _require_text(lease_token, field="lease_token")
        request_id = _require_text(request_id, field="request_id")
        expected_owner = (
            None if owner_id is None else _require_text(owner_id, field="owner_id")
        )

        def mutate(state: Dict[str, Any], now: float) -> Dict[str, Any]:
            job = self._find_job(state, job_id)
            lease = self._require_lease(job, lease_token, expected_owner)
            request = job.get("release_request")
            if not isinstance(request, dict) or request.get("id") != request_id:
                raise ForceReleaseRejected(
                    f"release_request_mismatch for job {job_id}"
                )

            job["status"] = "failed"
            job["finished_at"] = now
            job["terminal_at"] = now
            job["failure_reason"] = "owner_released_by_operator"
            job["result"] = None
            job["last_lease"] = {
                key: value for key, value in lease.items() if key != "token"
            }
            job["release_ack"] = {
                "request_id": request_id,
                "reason": "owner_release_acknowledged",
                "acknowledged_at": now,
                "acknowledger": {
                    "owner_id": lease["owner_id"],
                    "pid": lease["pid"],
                    "hostname": lease["hostname"],
                    "process_marker": lease.get("process_marker"),
                },
            }
            job["lease"] = None
            return job

        return self._change(mutate)

    def force_release(
        self,
        job_id: str,
        *,
        confirm_owner_stopped: str,
    ) -> Dict[str, Any]:
        """Compatibility alias that now creates a non-terminal request only."""

        return self.request_release(
            job_id,
            confirm_job_id=confirm_owner_stopped,
        )

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
                self._recover_expired_lease(state, now)
                self._prune_terminal_jobs(state)
                result = operation(state, now)
                self._prune_terminal_jobs(state)
                self._validate_state(state)
                if state != before:
                    state["revision"] += 1
                    state["updated_at"] = now
                    self._write_state(state)
                return copy.deepcopy(result)

    def _prune_terminal_jobs(self, state: Dict[str, Any]) -> bool:
        """Retain only the newest bounded terminal audit history.

        Queued and running rows are never candidates, regardless of age.  The
        monotonically increasing ``next_sequence`` keeps future FIFO identity
        independent of removed history.  Completion time, not enqueue order,
        determines recency so a long-running old sequence keeps its fresh
        terminal audit.
        """

        def terminal_key(job: Dict[str, Any]) -> tuple[float, int]:
            value = None
            for field in (
                "terminal_at",
                "finished_at",
                "cancelled_at",
                "abandoned_at",
            ):
                candidate = job.get(field)
                if isinstance(candidate, (int, float)):
                    value = float(candidate)
                    break
            return (0.0 if value is None else value, job["sequence"])

        terminal = sorted(
            (
                job
                for job in state["jobs"]
                if job.get("status") in TERMINAL_STATUSES
            ),
            key=terminal_key,
            reverse=True,
        )
        retained_ids = {
            job["id"] for job in terminal[: self.max_terminal_jobs]
        }
        original_count = len(state["jobs"])
        state["jobs"] = [
            job
            for job in state["jobs"]
            if job.get("status") not in TERMINAL_STATUSES
            or job["id"] in retained_ids
        ]
        return len(state["jobs"]) != original_count

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
                f"lease_conflict: job {job.get('id')} is not owned by this "
                "live lease"
            )
        return lease

    def _start_lease(
        self,
        job: Dict[str, Any],
        owner_id: str,
        now: float,
    ) -> Dict[str, Any]:
        job["status"] = "running"
        job["attempts"] = int(job.get("attempts", 0)) + 1
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
            job["terminal_at"] = now
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
            job["terminal_at"] = now
            job["failure_reason"] = "owner_lost"
            job["result"] = None
            job["lease"] = None
            termination_audit = job.get("termination_audit")
            if not isinstance(termination_audit, dict):
                termination_audit = {"schema_version": 1, "events": []}
                job["termination_audit"] = termination_audit
            events = termination_audit.get("events")
            if not isinstance(events, list):
                raise QueueStateError(
                    f"invalid termination audit for job {job['id']}"
                )
            operator_close = any(
                isinstance(event, dict)
                and event.get("kind") == "operator_close_requested"
                for event in events
            )
            events.append({
                "sequence": len(events) + 1,
                "id": uuid.uuid4().hex,
                "kind": "owner_lost_recovered",
                "at": now,
                "owner": {
                    key: value
                    for key, value in audit.items()
                    if key not in {"recovered_at", "expires_at", "heartbeat_at"}
                },
                "trigger": (
                    "operator_close_requested"
                    if operator_close
                    else "owner_process_disappeared"
                ),
                "terminal_reason": "owner_lost",
                "original_batch_outcome": "unknown",
            })
            termination_audit["terminal_interpretation"] = {
                "terminal_reason": "owner_lost",
                "trigger": (
                    "operator_close_requested"
                    if operator_close
                    else "owner_process_disappeared"
                ),
                "original_batch_outcome": "unknown",
            }
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


def _cli_status(queue: SharedJobQueue) -> int:
    """Print an operator-safe summary without payloads or machine paths."""

    snapshot = queue.snapshot()
    counts = {
        status: sum(1 for job in snapshot["jobs"] if job["status"] == status)
        for status in sorted(VALID_STATUSES)
    }
    print(
        "queue "
        + " ".join(f"{status}={count}" for status, count in counts.items())
    )
    running = next(
        (job for job in snapshot["jobs"] if job["status"] == "running"),
        None,
    )
    if running is None:
        print("running none")
        return 0

    lease = running["lease"]
    claimed_at = float(lease.get("claimed_at", time.time()))
    age = max(0.0, time.time() - claimed_at)
    print(
        f"running id={running['id']} sequence={running['sequence']} "
        f"app={running['app_id']} age_seconds={age:.0f}"
    )
    request = running.get("release_request")
    if isinstance(request, dict):
        print(
            "release_requested "
            f"request_id={request.get('id')} "
            "awaiting=current_lease_owner_ack"
        )
        return 0
    if age >= queue.force_release_min_age_seconds:
        print(
            "request-release available; repeat the exact job ID with "
            "--confirm-job-id. This does not unblock the queue until the "
            "current lease owner stops its worker and acknowledges."
        )
    return 0


def _main(argv: Optional[Iterable[str]] = None) -> int:
    import argparse
    import sys

    parser = argparse.ArgumentParser(
        description="Inspect or request release of the shared job queue."
    )
    parser.add_argument(
        "--state-path",
        type=Path,
        default=default_queue_state_path(),
        help="queue state override for diagnostics/tests",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser(
        "status",
        help="show counts and the running job without payloads or paths",
    )
    release = commands.add_parser(
        "request-release",
        aliases=["force-release"],
        help="ask an old live lease owner to stop and acknowledge release",
    )
    release.add_argument("job_id")
    release.add_argument(
        "--confirm-job-id",
        "--confirm-owner-stopped",
        dest="confirm_job_id",
        required=True,
        metavar="JOB_ID",
        help="repeat the exact running job ID (legacy option name is accepted)",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    queue = SharedJobQueue(args.state_path)
    try:
        if args.command == "status":
            return _cli_status(queue)
        requested = queue.request_release(
            args.job_id,
            confirm_job_id=args.confirm_job_id,
        )
    except QueueError as exc:
        print(f"queue operation rejected: {exc}", file=sys.stderr)
        return 2
    print(
        f"release_requested id={requested['id']} "
        f"request_id={requested['release_request']['id']} "
        "awaiting=current_lease_owner_ack"
    )
    return 0


__all__ = [
    "DEFAULT_FORCE_RELEASE_MIN_AGE_SECONDS",
    "DEFAULT_MAX_TERMINAL_JOBS",
    "ForceReleaseRejected",
    "JobNotCancellable",
    "JobNotFound",
    "InterprocessMutex",
    "LeaseConflict",
    "QueueError",
    "QueueLockTimeout",
    "QueueStateError",
    "SharedJobQueue",
    "default_queue_state_path",
]


if __name__ == "__main__":
    raise SystemExit(_main())
