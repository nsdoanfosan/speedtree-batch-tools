"""Run one exact SK Batch Unreal Push through the production Blender job.

This is the non-GUI counterpart to the SK Batch Push button.  It deliberately
reuses the same green-signal evidence, Send2UE export, Unreal ingest, Nanite
Assembly builder, Perforce checkout, DynamicWind import, and runtime probe.
"""

from __future__ import annotations

import argparse
import json
import os
import runpy
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

SK_BATCH_DIR = Path(__file__).resolve().parent
REPO_DIR = SK_BATCH_DIR.parent
if str(REPO_DIR) not in sys.path:
    sys.path.insert(0, str(REPO_DIR))

from process_lifecycle import owned_run  # noqa: E402
from artifact_retention import estimate_output_reservation_bytes  # noqa: E402
from sk_common import send2ue_export_cache_root  # noqa: E402

LOG_DIR = SK_BATCH_DIR / "logs"
PUSH_JOB = SK_BATCH_DIR / "jobs" / "send2ue_push_job.py"
UNREAL_INGEST = SK_BATCH_DIR / "unreal_ingest.py"
GUI_ENTRY = SK_BATCH_DIR / "sk_batch_gui.pyw"
DEFAULT_BLENDER = Path(
    r"C:\Program Files\Blender Foundation\Blender 5.1\blender.exe"
)
DEFAULT_UNREAL_PROJECT = Path(
    r"C:\UnrealProjects\MyProject2\MyProject2.uproject"
)
DEFAULT_UNREAL_EDITOR_CMD = Path(
    r"C:\Program Files\Epic Games\UE_5.8\Engine\Binaries\Win64\UnrealEditor-Cmd.exe"
)


class ExactPushError(RuntimeError):
    pass


def _wait_for_export_disk_space(export_root: Path, expected_bytes: int) -> None:
    volume = Path(export_root.anchor) if export_root.anchor else export_root
    required_free = int(expected_bytes) + 5 * 1024**3
    last_notice = 0.0
    while True:
        free_bytes = shutil.disk_usage(volume).free
        if free_bytes >= required_free:
            return
        now = time.monotonic()
        if now - last_notice >= 15.0:
            last_notice = now
            print(
                "Waiting for D: FBX export space: "
                f"free={free_bytes / 1024**3:.1f} GiB, "
                f"required={required_free / 1024**3:.1f} GiB",
                file=sys.stderr,
            )
        time.sleep(1.0)


def _write_current_material_contract(spm: Path) -> Path:
    """Regenerate the single current wrapper from the asset-local Repair report."""
    try:
        namespace = runpy.run_path(
            str(GUI_ENTRY),
            run_name="sk_batch_current_contract_writer",
        )
        path = namespace["App"]._push_material_contract(spm)
    except Exception as exc:
        raise ExactPushError(
            f"current Push material contract could not be regenerated: {exc}"
        ) from exc
    path = Path(path).resolve()
    if not path.is_file():
        raise ExactPushError(f"current Push material contract was not written: {path}")
    return path


def build_exact_push_command(
    spm: Path,
    *,
    blender: Path = DEFAULT_BLENDER,
    log_dir: Path = LOG_DIR,
    run_id: str | None = None,
    material_contract: Path | None = None,
    unreal_project: Path | None = DEFAULT_UNREAL_PROJECT,
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
    if material_contract is None:
        material_contract = _write_current_material_contract(spm)
    else:
        material_contract = material_contract.expanduser().resolve()
        if not material_contract.is_file():
            raise ExactPushError(
                f"explicit Push material contract is missing: {material_contract}"
            )
    run_id = run_id or datetime.now().strftime("%Y%m%d_%H%M%S")
    prefix = log_dir / f"{stem}_exact_push_{run_id}"
    export_root = (
        send2ue_export_cache_root()
        / "exact"
        / stem
        / str(run_id)
    )
    outputs = {
        "report": prefix.with_suffix(".json"),
        "manifest": prefix.with_name(prefix.name + "_manifest.json"),
        "checkpoint": prefix.with_name(prefix.name + "_checkpoint.json"),
        "batch_report": prefix.with_name(prefix.name + "_batch.json"),
        "item_import_report": prefix.with_name(prefix.name + "_unreal.json"),
        "export_root": export_root,
        "material_contract": material_contract,
        "queue_id": str(spm),
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
        "--transport",
        "headless_export",
        "--dependency-orchestrated",
        "--manifest",
        str(outputs["manifest"]),
        "--checkpoint",
        str(outputs["checkpoint"]),
        "--batch-report",
        str(outputs["batch_report"]),
        "--item-import-report",
        str(outputs["item_import_report"]),
        "--export-root",
        str(outputs["export_root"]),
        "--queue-id",
        outputs["queue_id"],
        "--unreal-ingest",
        str(UNREAL_INGEST),
    ]
    outputs["unreal_project"] = (
        Path(unreal_project).expanduser().resolve()
        if unreal_project
        else None
    )
    return command, outputs


def run_headless_manifest(
    manifest_path: Path,
    checkpoint_path: Path,
    batch_report_path: Path,
    *,
    unreal_project: Path = DEFAULT_UNREAL_PROJECT,
    unreal_editor_cmd: Path = DEFAULT_UNREAL_EDITOR_CMD,
    max_restarts: int = 10,
) -> dict:
    """Ingest one immutable manifest in UnrealEditor-Cmd, with crash resume."""
    unreal_project = unreal_project.expanduser().resolve()
    unreal_editor_cmd = unreal_editor_cmd.expanduser().resolve()
    if not unreal_project.is_file():
        raise ExactPushError(f"Unreal project is missing: {unreal_project}")
    if not unreal_editor_cmd.is_file():
        raise ExactPushError(
            f"UnrealEditor-Cmd executable is missing: {unreal_editor_cmd}"
        )
    if not UNREAL_INGEST.is_file():
        raise ExactPushError(f"Unreal ingest script is missing: {UNREAL_INGEST}")

    manifest_path = manifest_path.expanduser().resolve()
    checkpoint_path = checkpoint_path.expanduser().resolve()
    batch_report_path = batch_report_path.expanduser().resolve()
    command = [
        str(unreal_editor_cmd),
        str(unreal_project),
        "-run=pythonscript",
        f"-script={UNREAL_INGEST}",
        "-unattended",
        "-NoSplash",
        "-NoSound",
        "-UTF8Output",
    ]
    environment = os.environ.copy()
    environment.update({
        "SK_BATCH_MANIFEST_PATH": str(manifest_path),
        "SK_BATCH_CHECKPOINT_PATH": str(checkpoint_path),
        "SK_BATCH_REPORT_PATH": str(batch_report_path),
    })
    last_returncode = None
    for attempt in range(max(0, int(max_restarts)) + 1):
        print(
            f"UnrealEditor-Cmd headless ingest "
            f"({attempt + 1}/{max(0, int(max_restarts)) + 1})",
            flush=True,
        )
        completed = owned_run(
            command,
            source="sk_batch.exact_push.unreal_ingest",
            run_factory=subprocess.run,
            check=False,
            env=environment,
        )
        last_returncode = completed.returncode
        try:
            checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            checkpoint = {}
        if checkpoint.get("complete") and batch_report_path.is_file():
            try:
                return json.loads(batch_report_path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise ExactPushError(
                    f"Unreal batch report is unreadable: {exc}"
                ) from exc
    raise ExactPushError(
        "UnrealEditor-Cmd did not complete the manifest after "
        f"{max(0, int(max_restarts)) + 1} launches; "
        f"last return code={last_returncode}, checkpoint={checkpoint_path}"
    )


def merge_unreal_result(outputs: dict, batch_result: dict) -> dict:
    """Promote commandlet item evidence into the exact Push report."""
    report_path = Path(outputs["report"])
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ExactPushError(f"exact Push export report is unreadable: {exc}") from exc
    item = (batch_result.get("items") or {}).get(outputs["queue_id"], {})
    report["unreal_result"] = item
    report["wind"] = item.get("wind")
    report["materials"] = item.get("materials")
    report["checkout"] = item.get("checkout")
    if item.get("status") == "imported_ok":
        report["stage"] = "completed"
        report["status"] = "ok"
        report["failure_kind"] = None
    else:
        report["stage"] = "unreal_ingest_failed"
        report["status"] = item.get("status") or "failed"
        report["failure_kind"] = item.get("status") or "data_error"
        report["error"] = item.get("message") or "Unreal manifest ingest failed"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return report


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Push one exact repaired SK target through UnrealEditor-Cmd",
    )
    parser.add_argument("--spm", required=True, type=Path)
    parser.add_argument("--blender", type=Path, default=DEFAULT_BLENDER)
    parser.add_argument("--log-dir", type=Path, default=LOG_DIR)
    parser.add_argument("--material-contract", type=Path)
    parser.add_argument("--unreal-project", type=Path, default=DEFAULT_UNREAL_PROJECT)
    parser.add_argument(
        "--unreal-editor-cmd",
        type=Path,
        default=DEFAULT_UNREAL_EDITOR_CMD,
    )
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    try:
        command, outputs = build_exact_push_command(
            args.spm,
            blender=args.blender,
            log_dir=args.log_dir,
            material_contract=args.material_contract,
            unreal_project=args.unreal_project,
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

    blend = args.spm.expanduser().resolve().with_suffix(".blend")
    expected_export_bytes = estimate_output_reservation_bytes(
        blend, minimum_bytes=1024**3, multiplier=4
    )
    _wait_for_export_disk_space(outputs["export_root"], expected_export_bytes)
    completed = owned_run(
        command,
        source="sk_batch.exact_push.blender_export",
        run_factory=subprocess.run,
        check=False,
    )
    if completed.returncode != 0:
        print(
            "SK Exact Push failed; production report: "
            + str(outputs["report"]),
            file=sys.stderr,
        )
        return completed.returncode or 1
    try:
        export_report = json.loads(outputs["report"].read_text(encoding="utf-8"))
        if export_report.get("status") != "exported_pending_unreal":
            raise ExactPushError(
                "Blender export did not reach exported_pending_unreal: "
                + str(export_report)
            )
        batch_result = run_headless_manifest(
            outputs["manifest"],
            outputs["checkpoint"],
            outputs["batch_report"],
            unreal_project=args.unreal_project,
            unreal_editor_cmd=args.unreal_editor_cmd,
        )
        report = merge_unreal_result(outputs, batch_result)
    except (OSError, ValueError, ExactPushError) as exc:
        print(f"SK Exact Push headless ingest failed: {exc}", file=sys.stderr)
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
