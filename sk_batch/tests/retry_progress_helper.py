"""Sanitized child used only by retry progress/liveness acceptance tests."""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "mode", choices=("slow", "silent", "fail", "hung", "hung_leaf")
    )
    parser.add_argument("--duration", type=float, default=0.3)
    parser.add_argument("--interval", type=float, default=0.05)
    args = parser.parse_args()

    if args.mode == "slow":
        deadline = time.monotonic() + max(0.01, args.duration)
        step = 0
        while time.monotonic() < deadline:
            step += 1
            print(f"PROGRESS step={step}", flush=True)
            time.sleep(max(0.01, args.interval))
        return
    if args.mode == "silent":
        time.sleep(max(0.01, args.duration))
        return
    if args.mode == "fail":
        print("PROGRESS before_nonzero_exit", flush=True)
        raise SystemExit(7)
    if args.mode == "hung":
        child = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "hung_leaf"]
        )
        print(f"CHILD_PID={child.pid}", flush=True)
    while True:
        time.sleep(1.0)


if __name__ == "__main__":
    main()
