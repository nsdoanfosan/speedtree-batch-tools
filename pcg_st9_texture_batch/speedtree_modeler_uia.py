"""Exact-PID semantic UIA adapter for bounded stale-Node-table recovery.

This module never discovers or adopts a Modeler process.  A session owns the
exact process returned by its launcher and the UIA bridge is allowed to see
only elements whose UIA ``ProcessId`` equals that PID.  It uses semantic
``InvokePattern`` operations only; coordinates and keyboard input are absent.
"""

from __future__ import annotations

import json
import math
import os
import subprocess
import time
from pathlib import Path

from process_lifecycle import (
    _process_start_identity,
    external_handoff_popen,
    process_identity_is_alive,
)
from speedtree_pipeline_contract import canonical_path_key


SEMANTIC_UIA_CONTRACT = "speedtree_modeler_owned_semantic_uia_v1"
_ALLOWED_OPERATIONS = frozenset({"save", "close"})
DEFAULT_ELEMENT_WAIT_TIMEOUTS = {
    "save": 300.0,
    "close": 120.0,
}
DEFAULT_SUBPROCESS_TIMEOUTS = {
    "save": 315.0,
    "close": 135.0,
}


class SemanticModelerUIAError(RuntimeError):
    """A semantic Modeler action was unavailable or ambiguous."""

    def __init__(self, reason_token, message, evidence=None):
        super().__init__(message)
        self.reason_token = str(reason_token)
        self.evidence = dict(evidence or {})


def _document_accessible_name(spm_path):
    spm = Path(spm_path)
    if (
        spm.name != str(spm_path).replace("\\", "/").rsplit("/", 1)[-1]
        or spm.suffix.casefold() != ".spm"
        or not spm.name
    ):
        raise SemanticModelerUIAError(
            "uia_document_identity_invalid",
            "the recovery document must be one exact SPM basename",
        )
    return spm.name


def _owned_modeler_launch(executable, spm_path):
    return external_handoff_popen(
        [str(executable), str(spm_path)],
        source="pcg_st9_texture_batch.speedtree_modeler_uia.owned_session",
        ownership="semantic_modeler_recovery_session",
        cwd=str(Path(spm_path).parent),
        stdin=subprocess.DEVNULL,
    )


def _owned_modeler_open_forward(executable, spm_path):
    return external_handoff_popen(
        [str(executable), str(spm_path)],
        source="pcg_st9_texture_batch.speedtree_modeler_uia.document_forward",
        ownership="semantic_modeler_recovery_document_forward",
        cwd=str(Path(spm_path).parent),
        stdin=subprocess.DEVNULL,
    )


def _stream_text(value):
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value or "")


def _latest_bridge_progress(value):
    latest = None
    for line in _stream_text(value).splitlines():
        try:
            payload = json.loads(line)
        except (TypeError, ValueError):
            continue
        if isinstance(payload, dict) and payload.get("kind") == "uia_bridge_progress":
            latest = payload
    return latest or {}


def _positive_timeout(value, label):
    timeout = float(value)
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError(f"{label} must be a finite positive number")
    return timeout


def _request_owned_process_close(process, process_start_identity):
    """Ask exact owned top-level windows to close; never terminate or kill."""
    pid = int(getattr(process, "pid", 0) or 0)
    if os.name != "nt":
        return {
            "graceful_close_requested": False,
            "graceful_close_reason": "windows_only",
            "window_count": 0,
        }
    if pid <= 0 or process.poll() is not None:
        return {
            "graceful_close_requested": False,
            "graceful_close_reason": "owned_process_not_alive",
            "window_count": 0,
        }
    if not process_start_identity:
        return {
            "graceful_close_requested": False,
            "graceful_close_reason": "owned_process_start_identity_unavailable",
            "window_count": 0,
        }
    if not process_identity_is_alive(pid, process_start_identity):
        return {
            "graceful_close_requested": False,
            "graceful_close_reason": "owned_process_identity_changed",
            "window_count": 0,
        }

    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(
        wintypes.BOOL,
        wintypes.HWND,
        wintypes.LPARAM,
    )
    windows = []

    @callback_type
    def collect(hwnd, _lparam):
        window_pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(window_pid))
        if int(window_pid.value) == pid:
            windows.append(hwnd)
        return True

    user32.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
    user32.EnumWindows.restype = wintypes.BOOL
    user32.GetWindowThreadProcessId.argtypes = [
        wintypes.HWND,
        ctypes.POINTER(wintypes.DWORD),
    ]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.PostMessageW.argtypes = [
        wintypes.HWND,
        wintypes.UINT,
        wintypes.WPARAM,
        wintypes.LPARAM,
    ]
    user32.PostMessageW.restype = wintypes.BOOL
    if not user32.EnumWindows(collect, 0):
        raise OSError(ctypes.get_last_error(), "EnumWindows failed")

    posted = 0
    wm_close = 0x0010
    for hwnd in windows:
        # Re-check the PID creation identity immediately before each request.
        # This prevents a PID-reuse race from targeting an unrelated process.
        if process.poll() is not None or not process_identity_is_alive(
            pid,
            process_start_identity,
        ):
            break
        if user32.PostMessageW(hwnd, wm_close, 0, 0):
            posted += 1
    return {
        "graceful_close_requested": bool(posted),
        "graceful_close_reason": (
            "wm_close_posted" if posted else "owned_top_level_window_missing"
        ),
        "window_count": len(windows),
        "window_close_request_count": posted,
    }


class PowerShellUIABridge:
    """Invoke the checked-in .NET UIAutomation bridge and validate its receipt."""

    def __init__(
        self,
        *,
        script_path=None,
        runner=subprocess.run,
        timeout=None,
        element_wait_timeouts=None,
        subprocess_timeouts=None,
        platform_name=None,
        monotonic_fn=time.monotonic,
    ):
        self.script_path = Path(
            script_path
            or Path(__file__).with_name("speedtree_modeler_uia.ps1")
        ).resolve(strict=False)
        self.runner = runner
        self.platform_name = os.name if platform_name is None else platform_name
        self.monotonic_fn = monotonic_fn
        if timeout is not None:
            legacy_wait = _positive_timeout(timeout, "timeout")
            waits = {operation: legacy_wait for operation in _ALLOWED_OPERATIONS}
            caps = {
                operation: legacy_wait + 5.0
                for operation in _ALLOWED_OPERATIONS
            }
        else:
            waits = dict(DEFAULT_ELEMENT_WAIT_TIMEOUTS)
            caps = dict(DEFAULT_SUBPROCESS_TIMEOUTS)
        for operation, value in dict(element_wait_timeouts or {}).items():
            if operation not in _ALLOWED_OPERATIONS:
                raise ValueError(f"unsupported UIA timeout operation: {operation}")
            waits[operation] = _positive_timeout(
                value,
                f"element_wait_timeouts[{operation}]",
            )
        for operation, value in dict(subprocess_timeouts or {}).items():
            if operation not in _ALLOWED_OPERATIONS:
                raise ValueError(f"unsupported UIA timeout operation: {operation}")
            caps[operation] = _positive_timeout(
                value,
                f"subprocess_timeouts[{operation}]",
            )
        for operation in _ALLOWED_OPERATIONS:
            waits[operation] = _positive_timeout(
                waits[operation],
                f"element_wait_timeouts[{operation}]",
            )
            caps[operation] = _positive_timeout(
                caps[operation],
                f"subprocess_timeouts[{operation}]",
            )
            if caps[operation] <= waits[operation]:
                raise ValueError(
                    "the subprocess cap must exceed the UIA operation wait budget"
                )
        self.element_wait_timeouts = waits
        self.subprocess_timeouts = caps

    def invoke(
        self,
        *,
        owned_process_id,
        executable,
        document_name,
        operation,
    ):
        operation = str(operation or "").casefold()
        if self.platform_name != "nt":
            raise SemanticModelerUIAError(
                "uia_windows_only",
                "semantic Modeler recovery is available only on Windows",
            )
        if type(owned_process_id) is not int or owned_process_id <= 0:
            raise SemanticModelerUIAError(
                "uia_owned_process_invalid",
                "the semantic session has no exact positive owned PID",
            )
        if operation not in _ALLOWED_OPERATIONS:
            raise SemanticModelerUIAError(
                "uia_operation_invalid",
                "only exact semantic Save and Close are supported",
            )
        document_name = _document_accessible_name(document_name)
        executable = Path(executable).expanduser().resolve(strict=False)
        if not self.script_path.is_file():
            raise SemanticModelerUIAError(
                "uia_bridge_missing",
                "the checked-in semantic UIA bridge is unavailable",
            )
        operation_timeout = self.element_wait_timeouts[operation]
        subprocess_timeout = self.subprocess_timeouts[operation]
        command = [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(self.script_path),
            "-OwnedProcessId",
            str(owned_process_id),
            "-ExecutablePath",
            str(executable),
            "-DocumentName",
            document_name,
            "-Operation",
            operation,
            "-OperationTimeoutSeconds",
            str(max(1, int(math.ceil(operation_timeout)))),
        ]
        started = self.monotonic_fn()
        base_evidence = {
            "contract": SEMANTIC_UIA_CONTRACT,
            "owned_process_id": owned_process_id,
            "document_accessible_name": document_name,
            "operation": operation,
            "menu_path": ["File", "Save" if operation == "save" else "Close"],
            "semantic_pattern": "InvokePattern",
            "operation_timeout_seconds": operation_timeout,
            "subprocess_timeout_seconds": subprocess_timeout,
        }
        try:
            completed = self.runner(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=subprocess_timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            elapsed = max(0.0, self.monotonic_fn() - started)
            progress = _latest_bridge_progress(
                getattr(exc, "stdout", None) or getattr(exc, "output", None)
            )
            try:
                phase_elapsed = max(
                    0.0,
                    elapsed - float(progress["elapsed_seconds"]),
                )
            except (KeyError, TypeError, ValueError):
                phase_elapsed = progress.get("phase_elapsed_seconds")
            raise SemanticModelerUIAError(
                "uia_bridge_timed_out",
                "the semantic UIA bridge exceeded its bounded subprocess cap",
                {
                    **base_evidence,
                    "pending_phase": progress.get("phase") or "bridge_execution",
                    "elapsed_seconds": round(elapsed, 3),
                    "phase_elapsed_seconds": (
                        round(phase_elapsed, 3)
                        if phase_elapsed is not None
                        else None
                    ),
                },
            ) from exc
        except OSError as exc:
            elapsed = max(0.0, self.monotonic_fn() - started)
            raise SemanticModelerUIAError(
                "uia_bridge_spawn_failed",
                "PowerShell could not be spawned for the semantic UIA bridge",
                {
                    **base_evidence,
                    "pending_phase": "bridge_spawn",
                    "elapsed_seconds": round(elapsed, 3),
                    "spawn_error_type": type(exc).__name__,
                    "spawn_errno": getattr(exc, "errno", None),
                    "spawn_winerror": getattr(exc, "winerror", None),
                },
            ) from exc
        except subprocess.SubprocessError as exc:
            elapsed = max(0.0, self.monotonic_fn() - started)
            raise SemanticModelerUIAError(
                "uia_bridge_process_failed",
                "the semantic UIA bridge process failed before returning a receipt",
                {
                    **base_evidence,
                    "pending_phase": "bridge_execution",
                    "elapsed_seconds": round(elapsed, 3),
                    "bridge_error_type": type(exc).__name__,
                },
            ) from exc
        elapsed = max(0.0, self.monotonic_fn() - started)
        lines = [line for line in str(completed.stdout or "").splitlines() if line.strip()]
        try:
            payload = json.loads(lines[-1])
        except (IndexError, TypeError, ValueError) as exc:
            raise SemanticModelerUIAError(
                "uia_bridge_receipt_invalid",
                "the semantic UIA bridge returned no valid receipt",
                {
                    **base_evidence,
                    "bridge_exit_code": int(completed.returncode),
                    "pending_phase": "bridge_receipt",
                    "elapsed_seconds": round(elapsed, 3),
                    "bridge_stdout_line_count": len(lines),
                },
            ) from exc
        evidence = {
            **base_evidence,
            "bridge_exit_code": int(completed.returncode),
            "pending_phase": payload.get("pending_phase") or "complete",
            "elapsed_seconds": payload.get("elapsed_seconds", round(elapsed, 3)),
            "phase_elapsed_seconds": payload.get("phase_elapsed_seconds"),
        }
        if completed.returncode != 0 or payload.get("ok") is not True:
            raise SemanticModelerUIAError(
                payload.get("reason_token") or "uia_semantic_invoke_failed",
                "the exact semantic Modeler action failed closed",
                evidence,
            )
        valid = bool(
            payload.get("contract") == SEMANTIC_UIA_CONTRACT
            and payload.get("owned_process_id") == owned_process_id
            and payload.get("document_accessible_name") == document_name
            and payload.get("operation") == operation
            and payload.get("menu_path") == evidence["menu_path"]
            and payload.get("semantic_pattern") == "InvokePattern"
        )
        if not valid:
            raise SemanticModelerUIAError(
                "uia_bridge_receipt_identity_mismatch",
                "the UIA receipt is not bound to the exact PID, document, and menu",
                evidence,
            )
        return evidence


class SpeedTreeModelerRecoverySession:
    """Keep one exact owned Modeler alive across sequential recovery documents."""

    def __init__(
        self,
        executable,
        *,
        bridge=None,
        launcher=_owned_modeler_launch,
        document_opener=_owned_modeler_open_forward,
        process_start_identity_fn=_process_start_identity,
        graceful_process_closer=_request_owned_process_close,
        shutdown_grace_seconds=10.0,
    ):
        self.executable = Path(executable).expanduser().resolve(strict=False)
        self.bridge = bridge or PowerShellUIABridge()
        self.launcher = launcher
        self.document_opener = document_opener
        self.process_start_identity_fn = process_start_identity_fn
        self.graceful_process_closer = graceful_process_closer
        self.shutdown_grace_seconds = _positive_timeout(
            shutdown_grace_seconds,
            "shutdown_grace_seconds",
        )
        self._owned_process = None
        self._owned_process_start_identity = None
        self._active_document_key = None

    @property
    def owned_process_id(self):
        return int(getattr(self._owned_process, "pid", 0) or 0)

    @property
    def owned_process_start_identity(self):
        return self._owned_process_start_identity

    def is_compatible(self, executable):
        return canonical_path_key(executable) == canonical_path_key(self.executable)

    def _is_alive(self):
        process = self._owned_process
        return bool(process is not None and process.poll() is None)

    def _open_exact_document(self, spm):
        reused = self._is_alive()
        if not reused:
            self._owned_process = self.launcher(self.executable, spm)
            if not self._is_alive() or self.owned_process_id <= 0:
                self._owned_process = None
                self._owned_process_start_identity = None
                raise SemanticModelerUIAError(
                    "uia_owned_modeler_not_alive",
                    "the dedicated Modeler process did not remain alive",
                    {"document_accessible_name": spm.name},
                )
            self._owned_process_start_identity = self.process_start_identity_fn(
                self._owned_process
            )
        else:
            if self._active_document_key is not None:
                raise SemanticModelerUIAError(
                    "uia_previous_document_not_closed",
                    "the exact previous recovery document is still active",
                    {"owned_process_id": self.owned_process_id},
                )
            self.document_opener(self.executable, spm)
            if not self._is_alive():
                raise SemanticModelerUIAError(
                    "uia_owned_modeler_exited_during_open",
                    "the dedicated Modeler exited while opening the next document",
                    {"document_accessible_name": spm.name},
                )
        return reused

    def save_document(self, executable, spm_path):
        spm = Path(spm_path).expanduser().resolve(strict=False)
        if not self.is_compatible(executable):
            raise SemanticModelerUIAError(
                "uia_executable_identity_mismatch",
                "the recovery executable differs from the owned session",
                {"document_accessible_name": spm.name},
            )
        if not spm.is_file() or spm.suffix.casefold() != ".spm":
            raise SemanticModelerUIAError(
                "uia_document_identity_invalid",
                "the semantic recovery target is not one exact existing SPM",
            )
        reused = self._open_exact_document(spm)
        # Once opening is requested, preserve the exact document identity even
        # if UIA fails.  A failed/ambiguous Save must poison reuse instead of
        # silently forwarding a different document into the same session.
        self._active_document_key = canonical_path_key(spm)
        evidence = self.bridge.invoke(
            owned_process_id=self.owned_process_id,
            executable=self.executable,
            document_name=spm.name,
            operation="save",
        )
        if not self._is_alive():
            raise SemanticModelerUIAError(
                "uia_owned_modeler_exited_after_save",
                "the dedicated Modeler exited after semantic Save",
                {
                    "owned_process_id": self.owned_process_id,
                    "document_accessible_name": spm.name,
                },
            )
        return {
            **evidence,
            "session_reused": reused,
            "owned_process_alive_after_invoke": self._is_alive(),
        }

    def close_document(self, spm_path):
        spm = Path(spm_path).expanduser().resolve(strict=False)
        if (
            not self._is_alive()
            or self._active_document_key != canonical_path_key(spm)
        ):
            raise SemanticModelerUIAError(
                "uia_close_document_identity_mismatch",
                "only the exact verified active recovery document may be closed",
                {
                    "owned_process_id": self.owned_process_id,
                    "document_accessible_name": spm.name,
                },
            )
        evidence = self.bridge.invoke(
            owned_process_id=self.owned_process_id,
            executable=self.executable,
            document_name=spm.name,
            operation="close",
        )
        self._active_document_key = None
        if not self._is_alive():
            raise SemanticModelerUIAError(
                "uia_owned_modeler_exited_after_close",
                "the dedicated Modeler did not remain alive after exact Close",
                {
                    "owned_process_id": self.owned_process_id,
                    "document_accessible_name": spm.name,
                },
            )
        return {
            **evidence,
            "exact_document_closed": True,
            "owned_process_alive_after_close": self._is_alive(),
        }

    def cleanup_after_failure(self, spm_path):
        """Close the exact failed document and request graceful owned-process exit.

        The method deliberately has no ``terminate``/``kill`` fallback.  If the
        exact document cannot be closed or the owned process ignores WM_CLOSE,
        the evidence says so and the process is left untouched.
        """
        spm = Path(spm_path).expanduser().resolve(strict=False)
        process = self._owned_process
        evidence = {
            "contract": SEMANTIC_UIA_CONTRACT,
            "cleanup": "failed_semantic_recovery",
            "owned_process_id": self.owned_process_id,
            "owned_process_start_identity": self.owned_process_start_identity,
            "document_accessible_name": spm.name,
            "exact_document_close_attempted": False,
            "exact_document_closed": False,
            "graceful_process_exit_requested": False,
            "force_termination_used": False,
        }
        if process is None or not self._is_alive():
            self._owned_process = None
            self._owned_process_start_identity = None
            self._active_document_key = None
            evidence["cleanup_status"] = "owned_process_already_exited"
            evidence["owned_process_alive_after_cleanup"] = False
            return evidence

        active_key = self._active_document_key
        if active_key is not None:
            if active_key != canonical_path_key(spm):
                evidence["cleanup_status"] = "active_document_identity_mismatch"
                evidence["owned_process_alive_after_cleanup"] = True
                return evidence
            evidence["exact_document_close_attempted"] = True
            try:
                close_evidence = self.bridge.invoke(
                    owned_process_id=self.owned_process_id,
                    executable=self.executable,
                    document_name=spm.name,
                    operation="close",
                )
            except SemanticModelerUIAError as exc:
                evidence["cleanup_status"] = "exact_document_close_failed"
                evidence["cleanup_reason_token"] = exc.reason_token
                evidence["cleanup_diagnostics"] = dict(exc.evidence)
                evidence["owned_process_alive_after_cleanup"] = self._is_alive()
                return evidence
            evidence["exact_document_closed"] = True
            evidence["exact_document_close_evidence"] = close_evidence
            self._active_document_key = None

        try:
            request = self.graceful_process_closer(
                process,
                self._owned_process_start_identity,
            )
        except Exception as exc:
            evidence["cleanup_status"] = "graceful_process_exit_request_failed"
            evidence["graceful_process_exit_error_type"] = type(exc).__name__
            evidence["owned_process_alive_after_cleanup"] = self._is_alive()
            return evidence
        evidence["graceful_process_exit"] = dict(request or {})
        evidence["graceful_process_exit_requested"] = bool(
            (request or {}).get("graceful_close_requested")
        )
        if evidence["graceful_process_exit_requested"]:
            try:
                process.wait(timeout=self.shutdown_grace_seconds)
            except subprocess.TimeoutExpired:
                evidence["cleanup_status"] = "graceful_process_exit_timed_out"
            else:
                evidence["cleanup_status"] = "owned_process_exited_gracefully"
        else:
            evidence["cleanup_status"] = "graceful_process_exit_not_requested"
        alive = self._is_alive()
        evidence["owned_process_alive_after_cleanup"] = alive
        if not alive:
            self._owned_process = None
            self._owned_process_start_identity = None
        return evidence
