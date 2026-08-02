"""Exact-PID semantic UIA adapter for bounded stale-Node-table recovery.

This module never discovers or adopts a Modeler process.  A session owns the
exact process returned by its launcher and the UIA bridge is allowed to see
only elements whose UIA ``ProcessId`` equals that PID.  It uses semantic
``InvokePattern`` operations only; coordinates and keyboard input are absent.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from process_lifecycle import external_handoff_popen
from speedtree_pipeline_contract import canonical_path_key


SEMANTIC_UIA_CONTRACT = "speedtree_modeler_owned_semantic_uia_v1"
_ALLOWED_OPERATIONS = frozenset({"save", "close"})


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


class PowerShellUIABridge:
    """Invoke the checked-in .NET UIAutomation bridge and validate its receipt."""

    def __init__(
        self,
        *,
        script_path=None,
        runner=subprocess.run,
        timeout=45.0,
        platform_name=None,
    ):
        self.script_path = Path(
            script_path
            or Path(__file__).with_name("speedtree_modeler_uia.ps1")
        ).resolve(strict=False)
        self.runner = runner
        self.timeout = float(timeout)
        self.platform_name = os.name if platform_name is None else platform_name

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
            "-TimeoutSeconds",
            str(max(1, int(self.timeout))),
        ]
        try:
            completed = self.runner(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=self.timeout + 5.0,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise SemanticModelerUIAError(
                "uia_bridge_execution_failed",
                "the semantic UIA bridge could not be executed",
                {
                    "owned_process_id": owned_process_id,
                    "document_accessible_name": document_name,
                    "operation": operation,
                },
            ) from exc
        lines = [line for line in str(completed.stdout or "").splitlines() if line.strip()]
        try:
            payload = json.loads(lines[-1])
        except (IndexError, TypeError, ValueError) as exc:
            raise SemanticModelerUIAError(
                "uia_bridge_receipt_invalid",
                "the semantic UIA bridge returned no valid receipt",
                {
                    "owned_process_id": owned_process_id,
                    "document_accessible_name": document_name,
                    "operation": operation,
                    "bridge_exit_code": int(completed.returncode),
                },
            ) from exc
        evidence = {
            "contract": SEMANTIC_UIA_CONTRACT,
            "owned_process_id": owned_process_id,
            "document_accessible_name": document_name,
            "operation": operation,
            "menu_path": ["File", "Save" if operation == "save" else "Close"],
            "semantic_pattern": "InvokePattern",
            "bridge_exit_code": int(completed.returncode),
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
    ):
        self.executable = Path(executable).expanduser().resolve(strict=False)
        self.bridge = bridge or PowerShellUIABridge()
        self.launcher = launcher
        self.document_opener = document_opener
        self._owned_process = None
        self._active_document_key = None

    @property
    def owned_process_id(self):
        return int(getattr(self._owned_process, "pid", 0) or 0)

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
                raise SemanticModelerUIAError(
                    "uia_owned_modeler_not_alive",
                    "the dedicated Modeler process did not remain alive",
                    {"document_accessible_name": spm.name},
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
