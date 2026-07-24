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
import sys
import traceback
from datetime import datetime
from pathlib import Path


REPO_DIR = Path(__file__).resolve().parent
ERROR_LOG = REPO_DIR / "speedtree_batch_tools_error.log"
MB_ICONERROR = 0x10


def record_error(label, text):
    """Append one failure to the shared log; never raise from the reporter."""
    try:
        with ERROR_LOG.open("a", encoding="utf-8") as handle:
            handle.write(
                f"\n[{datetime.now().isoformat(timespec='seconds')}] {label}\n"
            )
            handle.write(text)
        return True
    except OSError:
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


def main(argv):
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

    # The GUI expects the same argv/cwd it would get from a direct launch.
    sys.argv = [str(target), *argv[2:]]
    try:
        runpy.run_path(str(target), run_name="__main__")
    except SystemExit as exc:
        return int(exc.code or 0)
    except BaseException as exc:  # noqa: BLE001 - last resort reporter
        report(
            label,
            f"{label}을 시작하지 못했습니다.\n\n{type(exc).__name__}: {exc}",
            "".join(traceback.format_exception(exc)),
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
