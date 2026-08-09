"""Run one exact SK Batch Unreal Push through the production Blender job.

This is the non-GUI counterpart to the SK Batch Push button.  It deliberately
reuses the same green-signal evidence, Send2UE export, Unreal ingest, Nanite
Assembly builder, Perforce checkout, DynamicWind import, and runtime probe.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path


SK_BATCH_DIR = Path(__file__).resolve().parent
LOG_DIR = SK_BATCH_DIR / "logs"
PUSH_JOB = SK_BATCH_DIR / "jobs" / "send2ue_push_job.py"
DEFAULT_BLENDER = Path(
    r"C:\Program Files\Blender Foundation\Blender 5.1\blender.exe"
)


class ExactPushError(RuntimeError):
    pass


def _latest_exact(log_dir: Path, pattern: str, label: str) -> Path:
    matches = sorted(
        (path for path in log_dir.glob(pattern) if path.is_file()),
        key=lambda path: path.stat().st_mtime_ns,
        reverse=True,
    )
    if not matches:
        raise ExactPushError(f"current {label} was not found: {pattern}")
    return matches[0].resolve()


def build_exact_push_command(
    spm: Path,
    *,
    blender: Path = DEFAULT_BLENDER,
    log_dir: Path = LOG_DIR,
    run_id: str | None = None,
) -> tuple[list[str], dict]:
    spm = spm.expanduser().resolve()
    blender = blender.expanduser().resolve()
    log_dir = log_dir.expanduser().resolve()
    blend = spm.with_suffix(".blend")
    if spm.suffix.casefold() != ".spm" or not spm.is_file():
        raise ExactPushError(f"exact production SPM is missing: {spm}")
    if not blend.is_file():
        raise ExactPushError(f"repaired Blender source is missing: {blend}")
    if not blender.is_file():
        raise ExactPushError(f"Blender executable is missing: {blender}")
    if not PUSH_JOB.is_file():
        raise ExactPushError(f"production Push job is missing: {PUSH_JOB}")

    stem = spm.stem
    material_contract = _latest_exact(
        log_dir,
        f"{stem}_push_material_contract_*.json",
        "Push material contract",
    )
    repair_evidence = _latest_exact(
        log_dir,
        f"{stem}_repair_push_evidence_*.json",
        "Repair/Push evidence",
    )
    run_id = run_id or datetime.now().strftime("%Y%m%d_%H%M%S")
    prefix = log_dir / f"{stem}_exact_push_{run_id}"
    outputs = {
        "report": prefix.with_suffix(".json"),
        "manifest": prefix.with_name(prefix.name + "_manifest.json"),
        "checkpoint": prefix.with_name(prefix.name + "_checkpoint.json"),
        "batch_report": prefix.with_name(prefix.name + "_batch.json"),
        "material_contract": material_contract,
        "repair_evidence": repair_evidence,
    }
    command = [
        str(blender),
        "--factory-startup",
        "--background",
        str(blend),
        "--python",
        str(PUSH_JOB),
        "--",
        "--report",
        str(outputs["report"]),
        "--spm",
        str(spm),
        "--material-contract",
        str(material_contract),
        "--repair-evidence",
        str(repair_evidence),
        "--transport",
        "rpc",
        "--require-green-signal",
        "--dependency-orchestrated",
        "--manifest",
        str(outputs["manifest"]),
        "--checkpoint",
        str(outputs["checkpoint"]),
        "--batch-report",
        str(outputs["batch_report"]),
        "--queue-id",
        str(spm),
    ]
    return command, outputs


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Push one exact repaired SK target to the open Unreal Editor",
    )
    parser.add_argument("--spm", required=True, type=Path)
    parser.add_argument("--blender", type=Path, default=DEFAULT_BLENDER)
    parser.add_argument("--log-dir", type=Path, default=LOG_DIR)
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    try:
        command, outputs = build_exact_push_command(
            args.spm,
            blender=args.blender,
            log_dir=args.log_dir,
        )
    except ExactPushError as exc:
        print(f"SK Exact Push failed: {exc}", file=sys.stderr)
        return 2

    print(json.dumps(
        {
            "spm": str(args.spm.expanduser().resolve()),
            "command": command,
            "outputs": {key: str(value) for key, value in outputs.items()},
            "dry_run": bool(args.dry_run),
        },
        ensure_ascii=False,
        indent=2,
    ))
    if args.dry_run:
        return 0

    completed = subprocess.run(command, check=False)
    if completed.returncode != 0:
        print(
            "SK Exact Push failed; production report: "
            + str(outputs["report"]),
            file=sys.stderr,
        )
        return completed.returncode or 1
    try:
        report = json.loads(outputs["report"].read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"SK Exact Push wrote no readable report: {exc}", file=sys.stderr)
        return 1
    if report.get("status") != "ok":
        print(
            "SK Exact Push did not reach status=ok: " + str(report),
            file=sys.stderr,
        )
        return 1
    print("SK_EXACT_PUSH_OK=" + str(outputs["report"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
