"""Window-local runtime adapter for :mod:`shared_job_queue`.

The persistent backend knows ordering and process ownership.  This module adds
the in-memory behavior a GUI window needs: it waits only for its own exact job
ID, reports queue progress, maintains a heartbeat, and releases its lease only
after the owning worker reports success or failure.  It never executes
application payloads itself.
"""

from __future__ import annotations

import copy
import socket
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from shared_job_queue import (
    JobNotCancellable,
    LeaseConflict,
    QueueError,
    SharedJobQueue,
    default_queue_state_path,
)


class RuntimeErrorBase(QueueError):
    """Base class for window-local queue runtime errors."""


class RuntimeClosed(RuntimeErrorBase):
    """The runtime has begun shutdown and accepts no more work."""


class WaitCancelled(RuntimeErrorBase):
    """A queued wait was cancelled before it acquired the execution lease."""


class JobNotOwned(RuntimeErrorBase):
    """The job ID was not enqueued by this runtime instance."""


class WaitAlreadyActive(RuntimeErrorBase):
    """This runtime already has a waiter for the exact job ID."""


def _brief(job: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if job is None:
        return None
    return {
        "id": job["id"],
        "sequence": job["sequence"],
        "app_id": job["app_id"],
        "label": job.get("label"),
        "status": job["status"],
    }


class SharedQueueLease:
    """One claimed job plus its renewable background heartbeat."""

    def __init__(
        self,
        runtime: "SharedQueueRuntime",
        record: Dict[str, Any],
        *,
        heartbeat_interval: float,
    ):
        self._runtime = runtime
        self.record = copy.deepcopy(record)
        self.job_id = record["id"]
        self.token = record["lease"]["token"]
        self._heartbeat_interval = float(heartbeat_interval)
        self._stop_event = threading.Event()
        self._finish_lock = threading.Lock()
        self._finished_record: Optional[Dict[str, Any]] = None
        self._heartbeat_error: Optional[BaseException] = None
        self._thread = threading.Thread(
            target=self._heartbeat_loop,
            name=f"shared-queue-heartbeat-{self.job_id[:8]}",
            daemon=True,
        )
        self._thread.start()

    @property
    def finished(self) -> bool:
        return self._finished_record is not None

    @property
    def heartbeat_error(self) -> Optional[BaseException]:
        return self._heartbeat_error

    def finish(
        self,
        success: bool = True,
        result: Any = None,
    ) -> Dict[str, Any]:
        """Stop heartbeating and complete the job exactly once locally."""

        with self._finish_lock:
            if self._finished_record is not None:
                return copy.deepcopy(self._finished_record)

            self._stop_event.set()
            if threading.current_thread() is not self._thread:
                self._thread.join(
                    timeout=max(1.0, self._heartbeat_interval * 2)
                )
            completed = self._runtime.queue.complete(
                self.job_id,
                self.token,
                result=result,
                success=bool(success),
                owner_id=self._runtime.owner_id,
            )
            self._finished_record = copy.deepcopy(completed)
            self.record = copy.deepcopy(completed)
            self._runtime._lease_finished(self)
            return copy.deepcopy(completed)

    def _heartbeat_loop(self) -> None:
        while not self._stop_event.wait(self._heartbeat_interval):
            try:
                renewed = self._runtime.queue.heartbeat(
                    self.job_id,
                    self.token,
                    owner_id=self._runtime.owner_id,
                )
                self.record = copy.deepcopy(renewed)
            except BaseException as exc:
                # The foreground owner observes this via ``heartbeat_error``.
                # Do not spin if the token is lost or persistence is unhealthy.
                self._heartbeat_error = exc
                self._stop_event.set()
                return

    def __enter__(self) -> "SharedQueueLease":
        return self

    def __exit__(self, exc_type, exc, _traceback) -> bool:
        if exc is None:
            self.finish(success=True)
        else:
            self.finish(
                success=False,
                result={
                    "error_type": getattr(exc_type, "__name__", str(exc_type)),
                    "error": str(exc),
                },
            )
        return False


class SharedQueueRuntime:
    """Per-window coordinator over the process-safe persistent FIFO."""

    def __init__(
        self,
        app_id: str,
        state_path: Optional[str | Path] = None,
        *,
        lease_seconds: float = 30.0,
        heartbeat_interval: Optional[float] = None,
    ):
        app_id = str(app_id).strip()
        if not app_id:
            raise ValueError("app_id must not be empty")
        self.app_id = app_id
        self.state_path = Path(state_path or default_queue_state_path())
        self.queue = SharedJobQueue(
            self.state_path,
            lease_seconds=lease_seconds,
        )
        if heartbeat_interval is None:
            heartbeat_interval = min(5.0, lease_seconds / 3.0)
        self.heartbeat_interval = float(heartbeat_interval)
        if (
            self.heartbeat_interval <= 0
            or self.heartbeat_interval >= lease_seconds
        ):
            raise ValueError(
                "heartbeat_interval must be greater than zero and shorter "
                "than lease_seconds"
            )

        self.runtime_id = uuid.uuid4().hex
        self.owner_id = (
            f"{self.app_id}:{socket.gethostname()}:{self.runtime_id}"
        )
        self._lock = threading.RLock()
        self._pending: Dict[str, Dict[str, Any]] = {}
        self._waiting = set()
        self._active: Dict[str, SharedQueueLease] = {}
        self._shutdown_event = threading.Event()
        self._closed = False

    def enqueue(self, label: str, payload: Any) -> Dict[str, Any]:
        """Append one callback-owned ticket to the shared global FIFO."""

        with self._lock:
            self._ensure_open()
            record = self.queue.enqueue(
                self.app_id,
                payload,
                label=str(label),
                metadata={"runtime_id": self.runtime_id},
            )
            self._pending[record["id"]] = copy.deepcopy(record)
            return copy.deepcopy(record)

    def wait_for_turn(
        self,
        job_id: str,
        on_wait: Optional[Callable[[Dict[str, Any]], None]] = None,
        cancel_event: Optional[threading.Event] = None,
        poll_interval: float = 0.2,
    ) -> SharedQueueLease:
        """Block until this runtime's exact job reaches the FIFO head."""

        job_id = str(job_id).strip()
        if not job_id:
            raise ValueError("job_id must not be empty")
        poll_interval = float(poll_interval)
        if poll_interval <= 0:
            raise ValueError("poll_interval must be greater than zero")

        with self._lock:
            self._ensure_open()
            if job_id not in self._pending:
                raise JobNotOwned(
                    f"job {job_id} was not enqueued by this runtime"
                )
            if job_id in self._waiting:
                raise WaitAlreadyActive(f"job {job_id} already has a waiter")
            self._waiting.add(job_id)

        try:
            while True:
                if self._shutdown_event.is_set() or (
                    cancel_event is not None and cancel_event.is_set()
                ):
                    self._cancel_waiting_ticket(job_id, "wait_cancelled")
                    raise WaitCancelled(f"wait cancelled for job {job_id}")

                # One snapshot answers own status, FIFO position, and whether
                # a claim is worth attempting.  Only the transition tick does
                # a second locked read so claim() can re-check atomically.
                snapshot = self.queue.snapshot()
                wait_status = self._wait_status(job_id, snapshot=snapshot)
                state = next(
                    (
                        job
                        for job in snapshot["jobs"]
                        if job["id"] == job_id
                    ),
                    None,
                )
                if state is None:
                    with self._lock:
                        self._pending.pop(job_id, None)
                    raise RuntimeErrorBase(
                        f"job {job_id} disappeared before claim"
                    )
                if state["status"] in (
                    "cancelled",
                    "abandoned",
                    "failed",
                    "completed",
                ):
                    with self._lock:
                        self._pending.pop(job_id, None)
                    if state["status"] == "cancelled":
                        raise WaitCancelled(f"wait cancelled for job {job_id}")
                    raise RuntimeErrorBase(
                        f"job {job_id} became {state['status']} before claim"
                    )

                fifo_head = wait_status["fifo_head"]
                if fifo_head is not None and fifo_head["id"] == job_id:
                    # Claim and local registration share the runtime lock so
                    # shutdown cannot miss a just-acquired lease.
                    with self._lock:
                        if self._closed:
                            self._cancel_waiting_ticket(
                                job_id,
                                "runtime_shutdown",
                            )
                            raise WaitCancelled(
                                "runtime shut down while waiting for job "
                                f"{job_id}"
                            )
                        claimed = self.queue.claim(
                            self.owner_id,
                            job_id=job_id,
                            accepted_apps={self.app_id},
                        )
                        if claimed is not None:
                            lease = SharedQueueLease(
                                self,
                                claimed,
                                heartbeat_interval=self.heartbeat_interval,
                            )
                            self._pending.pop(job_id, None)
                            self._active[job_id] = lease
                            return lease

                if on_wait is not None:
                    on_wait(wait_status)

                if cancel_event is not None:
                    cancel_event.wait(poll_interval)
                else:
                    self._shutdown_event.wait(poll_interval)
        finally:
            with self._lock:
                self._waiting.discard(job_id)

    def cancel(
        self,
        job_id: str,
        *,
        reason: str = "cancelled_by_runtime",
    ) -> Dict[str, Any]:
        """Cancel one still-queued ticket owned by this runtime."""

        job_id = str(job_id).strip()
        with self._lock:
            if job_id not in self._pending:
                raise JobNotOwned(
                    f"job {job_id} is not pending in this runtime"
                )
            record = self.queue.cancel(job_id, reason=reason)
            self._pending.pop(job_id, None)
            return copy.deepcopy(record)

    cancel_pending = cancel

    def cancel_all_pending(
        self,
        *,
        reason: str = "runtime_cancel_all",
    ) -> Dict[str, Dict[str, Any]]:
        """Cancel every queued ticket created by this runtime."""

        with self._lock:
            job_ids = list(self._pending)
        cancelled: Dict[str, Dict[str, Any]] = {}
        for job_id in job_ids:
            try:
                record = self.cancel(job_id, reason=reason)
            except (JobNotCancellable, JobNotOwned):
                continue
            cancelled[job_id] = record
        return cancelled

    def shutdown(self) -> None:
        """Close this runtime and cancel queued tickets, idempotently.

        Active leases deliberately keep heartbeating.  Only the code that has
        actually stopped or joined the asset worker may call ``lease.finish``;
        releasing here could let another GUI overlap a worker that is still
        mutating shared applications or files.
        """

        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._shutdown_event.set()
            pending_ids = list(self._pending)

        for job_id in pending_ids:
            try:
                self.queue.cancel(job_id, reason="runtime_shutdown")
            except (JobNotCancellable, LeaseConflict):
                pass
            finally:
                with self._lock:
                    self._pending.pop(job_id, None)

    def _wait_status(
        self,
        job_id: str,
        *,
        snapshot: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        if snapshot is None:
            snapshot = self.queue.snapshot()
        queued = sorted(
            (
                job
                for job in snapshot["jobs"]
                if job["status"] == "queued"
            ),
            key=lambda job: job["sequence"],
        )
        running = next(
            (
                job
                for job in snapshot["jobs"]
                if job["status"] == "running"
            ),
            None,
        )
        ordered = ([running] if running is not None else []) + queued
        position = next(
            (
                index
                for index, job in enumerate(ordered, start=1)
                if job["id"] == job_id
            ),
            None,
        )
        fifo_head = running or (queued[0] if queued else None)
        return {
            "job_id": job_id,
            "position": position,
            "queued_count": len(queued),
            "queued": [_brief(job) for job in queued],
            "running_head": _brief(running),
            "fifo_head": _brief(fifo_head),
        }

    def _cancel_waiting_ticket(self, job_id: str, reason: str) -> None:
        with self._lock:
            if job_id not in self._pending:
                return
            try:
                self.queue.cancel(job_id, reason=reason)
            except JobNotCancellable:
                return
            self._pending.pop(job_id, None)

    def _lease_finished(self, lease: SharedQueueLease) -> None:
        with self._lock:
            if self._active.get(lease.job_id) is lease:
                self._active.pop(lease.job_id, None)

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeClosed(f"queue runtime {self.runtime_id} is closed")


__all__ = [
    "JobNotOwned",
    "RuntimeClosed",
    "RuntimeErrorBase",
    "SharedQueueLease",
    "SharedQueueRuntime",
    "WaitAlreadyActive",
    "WaitCancelled",
]
