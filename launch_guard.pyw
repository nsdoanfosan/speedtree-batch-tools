"""Make a pythonw GUI launch failure visible instead of silent.

``pythonw`` has no console and ``start`` cannot report a child's exit code, so
a double-clicked .bat whose GUI dies during import looks exactly like a .bat
that did nothing.  Every launcher routes through this guard, which runs the
requested GUI as ``__main__`` and turns any escaping exception into a logged
traceback plus a native message box.

Usage:
  pythonw launch_guard.pyw <gui script> [args...]
"""

import ctypes
import os
import runpy
import subprocess
import sys
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path

from process_lifecycle import (
    ProcessLifecycleError,
    owned_popen,
    shutdown_process_supervisor,
    start_process_supervisor,
)
from shared_job_queue import InterprocessMutex, QueueError
from artifact_retention import RetentionCapacityError, enforce_retention


REPO_DIR = Path(__file__).resolve().parent
ERROR_LOG = Path(
    os.environ.get(
        "SPEEDTREE_BATCH_TOOLS_ERROR_LOG",
        str(REPO_DIR / "speedtree_batch_tools_error.log"),
    )
).expanduser()
ERROR_LOG_MAX_BYTES = 256 * 1024
ERROR_LOG_BACKUP_COUNT = 2
_ERROR_LOG_LOCK = threading.RLock()
MB_ICONERROR = 0x10
CODE_COMPILE_GATE = REPO_DIR / "sk_batch" / "code_compile_gate.py"
CODE_COMPILE_GATE_TARGETS = {
    (REPO_DIR / "speedtree_batch_tools_gui.pyw").resolve(),
    (REPO_DIR / "sk_batch" / "sk_batch_gui.pyw").resolve(),
}
SPEEDTREE_SESSION_TARGETS = {
    (REPO_DIR / "speedtree_batch_tools_gui.pyw").resolve(),
    (REPO_DIR / "sk_batch" / "sk_batch_gui.pyw").resolve(),
}


def _rotated_log_path(index):
    return ERROR_LOG.with_name(f"{ERROR_LOG.name}.{index}")


def _trim_log_file(path):
    if path.stat().st_size <= ERROR_LOG_MAX_BYTES:
        return
    with path.open("rb") as handle:
        handle.seek(-ERROR_LOG_MAX_BYTES, os.SEEK_END)
        tail = handle.read()
    # Drop an incomplete UTF-8 prefix and begin at the next whole line when
    # possible.  This file is already a rotated diagnostic backup.
    text = tail.decode("utf-8", errors="ignore")
    newline = text.find("\n")
    if newline >= 0:
        text = text[newline + 1:]
    path.write_text(text, encoding="utf-8")


def _rotate_error_log_if_needed(incoming_bytes):
    try:
        current_bytes = ERROR_LOG.stat().st_size
    except FileNotFoundError:
        return
    if current_bytes + incoming_bytes <= ERROR_LOG_MAX_BYTES:
        return
    oldest = _rotated_log_path(ERROR_LOG_BACKUP_COUNT)
    try:
        oldest.unlink()
    except FileNotFoundError:
        pass
    for index in range(ERROR_LOG_BACKUP_COUNT - 1, 0, -1):
        source = _rotated_log_path(index)
        if source.exists():
            os.replace(source, _rotated_log_path(index + 1))
    os.replace(ERROR_LOG, _rotated_log_path(1))
    _trim_log_file(_rotated_log_path(1))


def record_error(label, text):
    """Append one failure with fixed-size rotation; never raise."""

    header = f"\n[{datetime.now().isoformat(timespec='seconds')}] {label}\n"
    detail = str(text)
    entry = header + detail
    encoded = entry.encode("utf-8", errors="replace")
    if len(encoded) > ERROR_LOG_MAX_BYTES:
        header_bytes = header.encode("utf-8", errors="replace")
        tail_budget = max(0, ERROR_LOG_MAX_BYTES - len(header_bytes))
        detail_tail = detail.encode("utf-8", errors="replace")[-tail_budget:]
        entry = header + detail_tail.decode("utf-8", errors="ignore")
        encoded = entry.encode("utf-8")
    try:
        with _ERROR_LOG_LOCK:
            # Rotation decisions and the append must observe one process-wide
            # critical section.  Otherwise two simultaneous pythonw failures
            # can both rotate the same active file or append past the bound.
            with InterprocessMutex(ERROR_LOG, timeout=10.0).acquire():
                ERROR_LOG.parent.mkdir(parents=True, exist_ok=True)
                _rotate_error_log_if_needed(len(encoded))
                with ERROR_LOG.open(
                    "a", encoding="utf-8", newline="\n"
                ) as handle:
                    handle.write(entry)
        return True
    except (OSError, QueueError):
        return False


def show_error(title, message):
    # A modal dialog would hang an unattended/CI run, so allow opting out.
    if os.name == "nt" and not os.environ.get("SPEEDTREE_BATCH_TOOLS_NO_DIALOG"):
        try:
            ctypes.windll.user32.MessageBoxW(None, message, title, MB_ICONERROR)
            return
        except (AttributeError, OSError):
            pass
    print(message, file=sys.stderr)


def report(label, headline, detail):
    logged = record_error(label, detail)
    log_note = f"오류 로그: {ERROR_LOG}\n\n" if logged else ""
    show_error(
        "SpeedTree Batch Tools 실행 실패",
        f"{headline}\n\n{log_note}{detail[-1200:]}",
    )


def run_code_compile_gate(target):
    """Run the fast SK Batch source/contract gate for launchers that load it."""
    if target.resolve() not in CODE_COMPILE_GATE_TARGETS:
        return None
    gate_module = runpy.run_path(
        str(CODE_COMPILE_GATE),
        run_name="_speedtree_batch_code_compile_gate",
    )
    run_gate = gate_module.get("run_gate")
    if not callable(run_gate):
        raise RuntimeError(
            f"Code compile gate does not expose run_gate(): {CODE_COMPILE_GATE}"
        )
    return run_gate()


def run_startup_retention():
    """Enforce the shared age/capacity policy before any producer starts."""
    return enforce_retention(phase="startup")


def start_speedtree_session_host(target):
    """Own one blank-anchored Modeler for every export in this GUI run."""

    if (
        target.resolve() not in SPEEDTREE_SESSION_TARGETS
        or os.environ.get("SPEEDTREE_COLLISION_PERSISTENT") != "1"
    ):
        return None
    cli = Path(os.environ.get("SPEEDTREE_COLLISION_CLI_EXE", "")).expanduser()
    anchor = Path(
        os.environ.get("SPEEDTREE_COLLISION_SESSION_ANCHOR", "")
    ).expanduser()
    if not cli.is_file():
        raise RuntimeError(f"SpeedTree collision CLI was not found: {cli}")
    if not anchor.is_file():
        raise RuntimeError(f"SpeedTree persistent anchor was not found: {anchor}")

    log_path = Path(
        os.environ.get(
            "SPEEDTREE_COLLISION_SESSION_HOST_LOG",
            str(Path(os.environ.get("TEMP", REPO_DIR)) / "speedtree_collision_session_host.log"),
        )
    ).expanduser()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_handle = log_path.open("a", encoding="utf-8", newline="\n")
    command = [
        str(cli),
        "--serve-session",
        "--session-anchor",
        str(anchor),
        "--log",
        str(log_path.with_suffix(".hook.log")),
    ]
    try:
        process = owned_popen(
            command,
            source="launch_guard:speedtree_persistent_session_host",
            stdout=log_handle,
            stderr=subprocess.STDOUT,
        )
    except BaseException:
        log_handle.close()
        raise

    try:
        deadline = time.monotonic() + 45.0
        while time.monotonic() < deadline:
            ping = subprocess.run(
                [str(cli), "--ping-session"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=3,
                check=False,
            )
            if ping.returncode == 0:
                return {
                    "process": process,
                    "log_handle": log_handle,
                    "cli": cli,
                }
            if process.poll() is not None:
                log_handle.flush()
                raise RuntimeError(
                    "SpeedTree persistent session host exited before it became ready; "
                    f"see {log_path}"
                )
            time.sleep(0.1)
        raise RuntimeError(
            "SpeedTree persistent session host did not become ready within 45 seconds; "
            f"see {log_path}"
        )
    except BaseException:
        log_handle.close()
        raise


def stop_speedtree_session_host(host):
    if not host:
        return
    process = host["process"]
    log_handle = host["log_handle"]
    cli = host["cli"]
    try:
        subprocess.run(
            [str(cli), "--shutdown-session"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=False,
        )
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            # The process supervisor owns this exact host tree and performs the
            # final fail-closed cleanup immediately after this function.
            pass
    finally:
        log_handle.close()


def _run_main(argv):
    if len(argv) < 2:
        report(
            "launch_guard",
            "실행할 GUI 스크립트를 지정하지 않았습니다.",
            "usage: pythonw launch_guard.pyw <gui script> [args...]\n",
        )
        return 2

    target = Path(argv[1])
    if not target.is_absolute():
        target = (REPO_DIR / target).resolve()
    label = target.name
    if not target.is_file():
        report(label, f"GUI 파일을 찾을 수 없습니다:\n{target}", f"{target}\n")
        return 2


    try:
        target.resolve().relative_to(REPO_DIR.resolve())
        managed_target = True
    except ValueError:
        managed_target = False
    if managed_target:
        try:
            run_startup_retention()
        except (OSError, QueueError, RetentionCapacityError, ValueError) as exc:
            report(
                label,
                f"{label} artifact retention failed during startup.\n\n"
                f"{type(exc).__name__}: {exc}",
                "".join(traceback.format_exception(exc)),
            )
            return 1

    try:
        run_code_compile_gate(target)
    except BaseException as exc:  # noqa: BLE001 - launch must stop on gate failure
        report(
            label,
            f"{label} 코드 컴파일 검사를 통과하지 못했습니다.\n\n"
            f"{type(exc).__name__}: {exc}",
            "".join(traceback.format_exception(exc)),
        )
        return 1

    # The GUI expects the same argv/cwd it would get from a direct launch.
    sys.argv = [str(target), *argv[2:]]
    try:
        runpy.run_path(str(target), run_name="__main__")
    except SystemExit as exc:
        code = exc.code
        if code is None:
            return 0
        if isinstance(code, int):
            # Match CPython, including bool because it is an int subclass.
            return int(code)
        message = str(code)
        report(
            label,
            f"{label} stopped during startup.\n\n{message}",
            f"{message}\n",
        )
        return 1
    except BaseException as exc:  # noqa: BLE001 - last resort reporter
        report(
            label,
            f"{label}을 시작하지 못했습니다.\n\n{type(exc).__name__}: {exc}",
            "".join(traceback.format_exception(exc)),
        )
        return 1
    return 0


def main(argv):
    # Missing/invalid targets never launch children, so retain the existing
    # concise error path without creating an otherwise empty receipt.
    if len(argv) < 2:
        return _run_main(argv)
    target = Path(argv[1])
    if not target.is_absolute():
        target = (REPO_DIR / target).resolve()
    if not target.is_file():
        return _run_main(argv)

    try:
        supervisor = start_process_supervisor()
    except ProcessLifecycleError as exc:
        report(
            target.name,
            f"{target.name} process supervisor failed to start.\n\n{exc}",
            "".join(traceback.format_exception(exc)),
        )
        return 1

    result = 1
    session_host = None
    try:
        session_host = start_speedtree_session_host(target)
        result = _run_main(argv)
    except BaseException as exc:  # noqa: BLE001 - guard its own last boundary
        report(
            target.name,
            f"{target.name} launch supervisor failed.\n\n{exc}",
            "".join(traceback.format_exception(exc)),
        )
        result = 1
    finally:
        try:
            stop_speedtree_session_host(session_host)
        except (OSError, subprocess.SubprocessError) as exc:
            record_error(
                f"{target.name}:speedtree_session_shutdown",
                "".join(traceback.format_exception(exc)),
            )
            result = 1
        try:
            shutdown_process_supervisor(
                "gui_normal_exit" if result == 0 else "gui_error_exit"
            )
        except ProcessLifecycleError as exc:
            report(
                target.name,
                f"{target.name} process cleanup did not complete.\n\n{exc}",
                "".join(traceback.format_exception(exc)),
            )
            result = 1
    # Keep the supervisor reference alive through the final receipt write.
    del supervisor
    return result


if __name__ == "__main__":
    sys.exit(main(sys.argv))
