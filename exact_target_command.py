"""Durable shared-queue execution for public and in-process repair commands."""

from __future__ import annotations

import copy
import inspect
import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from repair_orchestration import REPAIR_RECEIPT_SCHEMA_VERSION
from shared_queue_runtime import SharedQueueRuntime, WaitCancelled


def atomic_write_receipt(path: str | Path, payload: Mapping[str, Any]) -> Path:
    destination = Path(path).expanduser().absolute()
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(
        f".{destination.name}.{os.getpid()}.{threading.get_ident()}.tmp"
    )
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, destination)
    finally:
        if temporary.exists():
            temporary.unlink()
    return destination


def build_exact_target_request(
    *,
    tool: str,
    repair_action: str,
    target_spms,
    repair_stage: str,
    provenance: Mapping[str, Any],
    parent_retry_id: str,
    request_id: str,
    receipt: str | Path,
) -> dict:
    targets = []
    seen = set()
    for value in target_spms:
        path = Path(value).expanduser()
        if not path.is_absolute() or path.suffix.casefold() != ".spm":
            raise ValueError("--target-spm must be an absolute .spm path")
        key = os.path.normcase(os.path.abspath(str(path))).casefold()
        if key not in seen:
            seen.add(key)
            targets.append(str(path.absolute()))
    if not targets:
        raise ValueError("at least one exact --target-spm is required")
    if not str(parent_retry_id).strip() or not str(request_id).strip():
        raise ValueError("parent retry ID and request ID are required")
    if not isinstance(provenance, Mapping) or not provenance:
        raise ValueError("repair provenance is required")
    return {
        "schema_version": 1,
        "tool": str(tool),
        "repair_action": str(repair_action),
        "repair_stage": str(repair_stage),
        "target_spms": targets,
        "provenance": copy.deepcopy(dict(provenance)),
        "parent_retry_id": str(parent_retry_id),
        "request_id": str(request_id),
        "receipt": str(Path(receipt).expanduser().absolute()),
    }


def run_exact_target_request(
    request: Mapping[str, Any],
    executor: Callable[..., Mapping[str, Any]],
    *,
    runtime: SharedQueueRuntime | None = None,
    inherited_lease=None,
    cancel_event=None,
    on_progress: Callable[[Mapping[str, Any]], None] | None = None,
) -> dict:
    """Execute after queue ownership is proven and persist every boundary."""

    request = copy.deepcopy(dict(request))
    receipt_path = Path(request["receipt"])
    cancel_event = cancel_event or threading.Event()
    own_runtime = runtime is None and inherited_lease is None
    if own_runtime:
        runtime = SharedQueueRuntime(str(request["tool"]))
    lease = inherited_lease
    queued = None
    started = datetime.now(timezone.utc).isoformat()

    def publish(status, **extra):
        payload = {
            "schema_version": REPAIR_RECEIPT_SCHEMA_VERSION,
            "tool": request["tool"],
            "repair_action": request["repair_action"],
            "repair_stage": request["repair_stage"],
            "target_spms": list(request["target_spms"]),
            "provenance": copy.deepcopy(request["provenance"]),
            "parent_retry_id": request["parent_retry_id"],
            "request_id": request["request_id"],
            "status": str(status),
            "started_at": started,
            **extra,
        }
        atomic_write_receipt(receipt_path, payload)
        if on_progress is not None:
            on_progress(copy.deepcopy(payload))
        return payload

    def finish_owned_lease(terminal_status, terminal):
        if inherited_lease is not None or lease is None or lease.finished:
            return
        options = {
            "success": terminal_status == "completed",
            "result": terminal,
        }
        # #107 adds this optional parameter.  Inspecting the owned lease keeps
        # this consumer backward compatible without copying queue semantics.
        if "terminal_status" in inspect.signature(lease.finish).parameters:
            options["terminal_status"] = terminal_status
        lease.finish(**options)

    try:
        if lease is None:
            queued = runtime.enqueue(
                f"exact repair · {request['repair_action']} · "
                f"{len(request['target_spms'])} target(s)",
                {
                    "tool": request["tool"],
                    "operation": "exact_target_repair",
                    "repair_action": request["repair_action"],
                    "repair_stage": request["repair_stage"],
                    "target_spms": list(request["target_spms"]),
                    "provenance": copy.deepcopy(request["provenance"]),
                    "parent_retry_id": request["parent_retry_id"],
                    "request_id": request["request_id"],
                },
            )
            publish(
                "queued",
                shared_queue_job_id=queued["id"],
                shared_queue_sequence=queued.get("sequence"),
            )
            lease = runtime.wait_for_turn(
                queued["id"],
                cancel_event=cancel_event,
            )
        renew = getattr(lease, "renew_and_check_current", None)
        if renew is not None and not renew():
            raise RuntimeError("shared queue ownership is not current")
        publish(
            "running",
            shared_queue_job_id=(queued or {}).get("id"),
            queue_owner_acknowledged=True,
        )

        def progress(stage, completed=0, remaining=0, **details):
            if cancel_event.is_set():
                raise WaitCancelled("exact repair cancelled")
            if renew is not None and not renew():
                raise RuntimeError("shared queue lease became stale")
            publish(
                "running",
                current_stage=str(stage),
                completed=int(completed),
                remaining=int(remaining),
                **details,
            )

        result = dict(executor(
            request,
            progress=progress,
            cancel_event=cancel_event,
            lease=lease,
        ) or {})
        outcome = str(result.get("outcome") or result.get("status") or "completed").casefold()
        success = bool(
            result.get("shared_queue_success", outcome in {
                "ok", "success", "succeeded", "completed",
            })
        )
        terminal = publish(
            "completed" if success else "failed",
            terminal_status="completed" if success else "failed",
            completed_at=datetime.now(timezone.utc).isoformat(),
            exit_code=0 if success else 1,
            result=result,
        )
        finish_owned_lease("completed" if success else "failed", terminal)
        return terminal
    except WaitCancelled as exc:
        result = {
            "outcome": "stopped",
            "failed_count": 0,
            "cancelled_count": len(request["target_spms"]),
            "target_outcomes": [
                {
                    "target": target,
                    "outcome": "cancelled",
                    "reason_token": "operator_cancelled",
                }
                for target in request["target_spms"]
            ],
        }
        terminal = publish(
            "cancelled",
            terminal_status="cancelled",
            completed_at=datetime.now(timezone.utc).isoformat(),
            exit_code=130,
            error=str(exc),
            result=result,
        )
        finish_owned_lease("cancelled", terminal)
        return terminal
    except Exception as exc:
        terminal = publish(
            "failed",
            terminal_status="failed",
            completed_at=datetime.now(timezone.utc).isoformat(),
            exit_code=1,
            error=f"{type(exc).__name__}: {exc}",
        )
        finish_owned_lease("failed", terminal)
        return terminal
    finally:
        if own_runtime and runtime is not None:
            runtime.shutdown()


__all__ = (
    "atomic_write_receipt",
    "build_exact_target_request",
    "run_exact_target_request",
)
