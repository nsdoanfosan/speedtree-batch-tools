"""Sanitized child/grandchild fixtures for process lifecycle tests only."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path


REPO_DIR = Path(__file__).resolve().parents[1]
if str(REPO_DIR) not in sys.path:
    sys.path.insert(0, str(REPO_DIR))

from process_lifecycle import (
    _current_process_start_identity,
    _process_start_identity,
    owned_popen,
    start_process_supervisor,
)


def _write_json(path, payload):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, target)


def _sleep_until(path=None, seconds=60.0):
    deadline = time.monotonic() + float(seconds)
    while time.monotonic() < deadline:
        if path is not None and Path(path).exists():
            return
        time.sleep(0.02)


def _grandchild(args):
    if args.ready:
        _write_json(
            args.ready,
            {
                "grandchild_pid": os.getpid(),
                "grandchild_start_identity": _current_process_start_identity(),
            },
        )
    _sleep_until(args.stop, args.seconds)
    return 0


def _tree(args):
    child_ready = Path(args.ready).with_suffix(".grandchild.json")
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "grandchild",
        "--ready",
        str(child_ready),
        "--seconds",
        str(args.seconds),
    ]
    if args.stop:
        command.extend(["--stop", str(args.stop)])
    child = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=(
            getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        ),
    )
    deadline = time.monotonic() + 10.0
    while not child_ready.is_file() and time.monotonic() < deadline:
        time.sleep(0.01)
    child_payload = json.loads(child_ready.read_text(encoding="utf-8"))
    _write_json(
        args.ready,
        {
            "parent_pid": os.getpid(),
            "parent_start_identity": _current_process_start_identity(),
            "grandchild_pid": child.pid,
            "grandchild_start_identity": _process_start_identity(child),
            "grandchild_reported_identity": child_payload[
                "grandchild_start_identity"
            ],
            "mode": args.mode,
        },
    )
    if args.mode == "root-exit":
        return 23
    if args.mode == "normal":
        Path(args.stop).touch()
        return child.wait(timeout=10)
    _sleep_until(args.stop, args.seconds)
    if child.poll() is None:
        child.wait(timeout=10)
    return child.returncode or 0


def _supervisor(args):
    start_process_supervisor(
        "sanitized:test-supervisor",
        receipt_dir=args.receipt_dir,
    )
    process = owned_popen(
        [
            sys.executable,
            str(Path(__file__).resolve()),
            "tree",
            "--mode",
            "sleep",
            "--ready",
            str(args.ready),
            "--seconds",
            str(args.seconds),
        ],
        source="tests.process_lifecycle_helper.supervisor",
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=(
            getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        ),
    )
    process.wait(timeout=float(args.seconds) + 10.0)
    return process.returncode or 0


def _nested_supervisor(args):
    start_process_supervisor(
        "sanitized:nested-supervisor",
        receipt_dir=args.receipt_dir,
    )
    child_ready = Path(args.ready).with_suffix(".nested-grandchild.json")
    child = owned_popen(
        [
            sys.executable,
            str(Path(__file__).resolve()),
            "grandchild",
            "--ready",
            str(child_ready),
            "--seconds",
            str(args.seconds),
        ],
        source="tests.process_lifecycle_helper.nested_grandchild",
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=(
            getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        ),
    )
    child_payload = _wait_json(child_ready)
    _write_json(
        args.ready,
        {
            "parent_pid": os.getpid(),
            "parent_start_identity": _current_process_start_identity(),
            "grandchild_pid": child.pid,
            "grandchild_start_identity": _process_start_identity(child),
            "grandchild_reported_identity": child_payload[
                "grandchild_start_identity"
            ],
            "mode": "nested-supervisor",
        },
    )
    child.wait(timeout=float(args.seconds) + 10.0)
    return child.returncode or 0


def _wait_json(path, timeout=10.0):
    deadline = time.monotonic() + float(timeout)
    while time.monotonic() < deadline:
        try:
            return json.loads(Path(path).read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            time.sleep(0.01)
    raise RuntimeError(f"timed out waiting for {path}")


def _arguments(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "role",
        choices=("grandchild", "tree", "supervisor", "nested-supervisor"),
    )
    parser.add_argument("--mode", choices=("sleep", "normal", "root-exit"), default="sleep")
    parser.add_argument("--ready")
    parser.add_argument("--stop")
    parser.add_argument("--receipt-dir")
    parser.add_argument("--seconds", type=float, default=60.0)
    return parser.parse_args(argv)


def main(argv=None):
    args = _arguments(argv)
    if args.role == "grandchild":
        return _grandchild(args)
    if args.role == "tree":
        return _tree(args)
    if args.role == "nested-supervisor":
        return _nested_supervisor(args)
    return _supervisor(args)


if __name__ == "__main__":
    raise SystemExit(main())
