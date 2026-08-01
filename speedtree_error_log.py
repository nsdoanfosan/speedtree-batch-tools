"""Shared exception logging for GUI failures that do not escape mainloop.

The launch guard owns the bounded, interprocess-safe log implementation.  GUI
callbacks use this adapter so asynchronous failures receive the same complete
traceback and rotation policy as process-start failures.
"""

from __future__ import annotations

import os
import runpy
import threading
import traceback
from pathlib import Path


REPO_DIR = Path(__file__).resolve().parent
LAUNCH_GUARD = REPO_DIR / "launch_guard.pyw"
ERROR_LOG = Path(
    os.environ.get(
        "SPEEDTREE_BATCH_TOOLS_ERROR_LOG",
        str(REPO_DIR / "speedtree_batch_tools_error.log"),
    )
).expanduser()
_RECORDER = None
_RECORDER_LOCK = threading.Lock()


def format_exception_traceback(exc) -> str:
    """Return the complete traceback retained by an exception object."""

    return "".join(traceback.format_exception(exc))


def _bounded_error_recorder():
    """Load the launch guard's bounded recorder once without running main()."""

    global _RECORDER
    if _RECORDER is not None:
        return _RECORDER
    with _RECORDER_LOCK:
        if _RECORDER is None:
            module = runpy.run_path(
                str(LAUNCH_GUARD),
                run_name="_speedtree_shared_error_log",
            )
            recorder = module.get("record_error")
            if not callable(recorder):
                raise RuntimeError(
                    f"Bounded error recorder is unavailable: {LAUNCH_GUARD}"
                )
            _RECORDER = recorder
    return _RECORDER


def record_exception(label, exc) -> bool:
    """Record one exception with its full traceback; never raise from here."""

    detail = format_exception_traceback(exc)
    try:
        return bool(_bounded_error_recorder()(label, detail))
    except Exception:
        return False
