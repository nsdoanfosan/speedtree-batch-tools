"""Run one exact SK Batch Unreal Push through the production Blender job.

This is the non-GUI counterpart to the SK Batch Push button.  It deliberately
reuses the same green-signal evidence, Send2UE export, Unreal ingest, Nanite
Assembly builder, Perforce checkout, DynamicWind import, and runtime probe.
"""

from __future__ import annotations

import argparse
import hashlib
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
from sk_common import (  # noqa: E402
    DEFAULT_SEND2UE_DIR,
    UNREAL_COMMANDLET_BASE_ARGS,
    send2ue_export_cache_root,
    unreal_remote_execution_settings,
    wind_preset_for_spm,
)
from unreal_ingest_policy import bounded_heavy_process_item_limit  # noqa: E402

LOG_DIR = SK_BATCH_DIR / "logs"
PUSH_JOB = SK_BATCH_DIR / "jobs" / "send2ue_push_job.py"
ASSEMBLY_JOB = SK_BATCH_DIR / "jobs" / "assembly_headless_job.py"
UNREAL_INGEST = SK_BATCH_DIR / "unreal_ingest.py"
GUI_ENTRY = SK_BATCH_DIR / "sk_batch_gui.pyw"
DEFAULT_BLENDER = Path(
    r"C:\Program Files\Blender Foundation\Blender 5.2\blender.exe"
)
DEFAULT_UNREAL_PROJECT = Path(
    r"C:\UnrealProjects\MyProject2\MyProject2.uproject"
)
DEFAULT_UNREAL_EDITOR_CMD = Path(
    r"C:\Program Files\Epic Games\UE_5.8\Engine\Binaries\Win64\UnrealEditor-Cmd.exe"
)
DEFAULT_MAX_PUSH_POLYGONS = 2_000_000
DEFAULT_MAX_PUSH_BONES = 1_500
DEFAULT_RPC_TIMEOUT_MIN = 180
DEFAULT_RPC_TIMEOUT_MAX = 900


class ExactPushError(RuntimeError):
    pass


def _promote_live_material_contract(command, outputs, assembly_report):
    """Use the contract produced by the assembly that immediately precedes Push.

    Assembly can regenerate the exact-target STMAT/FBX contract.  Continuing with
    the wrapper captured before Assembly makes the subsequent Push compare the
    new source artifacts against stale fingerprints.
    """
    evidence = assembly_report.get("live_material_contract") or {}
    raw_path = evidence.get("canonical_path") or evidence.get("path")
    if not raw_path:
        raise ExactPushError(
            "assembly report did not publish its live material contract"
        )
    contract_path = Path(raw_path).expanduser().resolve()
    if not contract_path.is_file():
        raise ExactPushError(
            "assembly live material contract is missing: " + str(contract_path)
        )
    expected_size = evidence.get("size")
    if expected_size is not None and contract_path.stat().st_size != int(expected_size):
        raise ExactPushError(
            "assembly live material contract size changed: " + str(contract_path)
        )
    expected_sha256 = str(evidence.get("sha256") or "").strip().casefold()
    if expected_sha256:
        digest = hashlib.sha256(contract_path.read_bytes()).hexdigest()
        if digest != expected_sha256:
            raise ExactPushError(
                "assembly live material contract fingerprint changed: "
                + str(contract_path)
            )
    try:
        argument_index = command.index("--material-contract") + 1
    except ValueError as exc:
        raise ExactPushError(
            "exact Push command is missing --material-contract"
        ) from exc
    command[argument_index] = str(contract_path)
    outputs["material_contract"] = contract_path
    return contract_path


def _rpc_cli_args(unreal_project: Path | None) -> list[str]:
    """Build the same project-scoped Send2UE RPC overrides as the GUI."""
    settings = unreal_remote_execution_settings(unreal_project)
    arguments = []
    bind_address = settings.get("multicast_bind_address")
    if bind_address:
        arguments.extend(["--rpc-multicast-bind-address", str(bind_address)])
    group_endpoint = settings.get("multicast_group_endpoint")
    if group_endpoint:
        arguments.extend(
            ["--rpc-multicast-group-endpoint", str(group_endpoint)]
        )
    if "multicast_ttl" in settings:
        arguments.extend(
            ["--rpc-multicast-ttl", str(settings["multicast_ttl"])]
        )
    return arguments


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
    """Regenerate the single current wrapper from the asset-local Assembly report."""
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


def build_assembly_refresh_command(
    spm: Path,
    *,
    blender: Path,
    material_contract: Path,
    report_path: Path,
) -> list[str]:
    """Refresh the assembled Blend from the current post-collision SPM export."""
    return [
        str(blender),
        "--factory-startup",
        "--background",
        "--python",
        str(ASSEMBLY_JOB),
        "--",
        "--spm",
        str(spm),
        "--speedtree-spm",
        str(spm),
        "--blend",
        str(spm.with_suffix(".blend")),
        "--wind",
        wind_preset_for_spm(spm),
        "--material-contract",
        str(material_contract),
        "--force-native-export",
        "--report",
        str(report_path),
    ]


def build_exact_push_command(
    spm: Path,
    *,
    blender: Path = DEFAULT_BLENDER,
    log_dir: Path = LOG_DIR,
    run_id: str | None = None,
    material_contract: Path | None = None,
    unreal_project: Path | None = DEFAULT_UNREAL_PROJECT,
    transport: str = "headless",
    send2ue_dir: Path = DEFAULT_SEND2UE_DIR,
    max_push_polygons: int = DEFAULT_MAX_PUSH_POLYGONS,
    max_push_bones: int = DEFAULT_MAX_PUSH_BONES,
    rpc_timeout_min: int = DEFAULT_RPC_TIMEOUT_MIN,
    rpc_timeout_max: int = DEFAULT_RPC_TIMEOUT_MAX,
) -> tuple[list[str], dict]:
    spm = spm.expanduser().resolve()
    blender = blender.expanduser().resolve()
    log_dir = log_dir.expanduser().resolve()
    blend = spm.with_suffix(".blend")
    if spm.suffix.casefold() != ".spm" or not spm.is_file():
        raise ExactPushError(f"exact production SPM is missing: {spm}")
    if not blend.is_file():
        raise ExactPushError(f"assembled Blender source is missing: {blend}")
    if not blender.is_file():
        raise ExactPushError(f"Blender executable is missing: {blender}")
    if not PUSH_JOB.is_file():
        raise ExactPushError(f"production Push job is missing: {PUSH_JOB}")
    if not ASSEMBLY_JOB.is_file():
        raise ExactPushError(f"production Assembly job is missing: {ASSEMBLY_JOB}")
    transport = str(transport).strip().casefold()
    if transport not in {"headless", "rpc"}:
        raise ExactPushError(f"unsupported exact Push transport: {transport}")

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
        / ("rpc" if transport == "rpc" else "exact")
        / stem
        / str(run_id)
    )
    outputs = {
        "report": prefix.with_suffix(".json"),
        "assembly_report": prefix.with_name(prefix.name + "_assembly.json"),
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
        "rpc" if transport == "rpc" else "headless_export",
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
    outputs["transport"] = transport
    if transport == "rpc":
        send2ue_unreal_py = (
            Path(send2ue_dir).expanduser().resolve()
            / "dependencies"
            / "unreal.py"
        )
        command.extend([
            "--send2ue-unreal-py",
            str(send2ue_unreal_py),
            "--max-push-polygons",
            str(max(1, int(max_push_polygons))),
            "--max-push-bones",
            str(max(1, int(max_push_bones))),
            "--rpc-timeout-min",
            str(max(1, int(rpc_timeout_min))),
            "--rpc-timeout-max",
            str(max(1, int(rpc_timeout_max))),
        ])
        command.extend(_rpc_cli_args(outputs["unreal_project"]))
    return command, outputs


# Checkpoint states that only exist because UnrealEditor-Cmd stopped while the
# item was importing.  A deliberate operator stop is indistinguishable from a
# real commandlet crash at the process level, so both land here and both are
# safe to requeue on explicit request.
OPERATOR_RETRYABLE_ITEM_STATES = ("unreal_crash", "manual_required")


def reset_checkpoint_item_retries(
    checkpoint_path: Path,
    *,
    retry_data_errors: bool = False,
) -> dict:
    """Requeue items that only failed because the commandlet was stopped.

    ``data_error`` is left alone by default because an unchanged retry cannot
    fix content.  A caller resuming after an implementation/data repair may
    explicitly requeue it with ``retry_data_errors=True``. ``not_run`` resolves
    on its own once its dependency imports. Successful items keep
    ``imported_ok`` so the resumed run still skips them.
    """
    checkpoint_path = Path(checkpoint_path).expanduser().resolve()
    try:
        checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ExactPushError(
            f"Unreal checkpoint is unreadable: {checkpoint_path}: {exc}"
        ) from exc

    reset = []
    for queue_id, state in (checkpoint.get("items") or {}).items():
        if not isinstance(state, dict):
            continue
        old_status = state.get("status")
        retryable = set(OPERATOR_RETRYABLE_ITEM_STATES)
        if retry_data_errors:
            retryable.add("data_error")
        if old_status not in retryable:
            continue
        state.update({
            "status": "operator_retry_pending",
            "crash_count": 0,
            "message": (
                "requeued by operator after "
                f"{old_status}; crash budget cleared"
            ),
            "updated_at": datetime.now().isoformat(timespec="seconds"),
        })
        reset.append(queue_id)

    if reset:
        checkpoint["current_item"] = None
        checkpoint["complete"] = False
        checkpoint["updated_at"] = datetime.now().isoformat(timespec="seconds")
        checkpoint_path.write_text(
            json.dumps(checkpoint, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    return {
        "checkpoint": str(checkpoint_path),
        "reset": sorted(reset),
        "retry_data_errors": bool(retry_data_errors),
    }


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
        *UNREAL_COMMANDLET_BASE_ARGS,
    ]
    environment = os.environ.copy()
    environment.update({
        "SK_BATCH_MANIFEST_PATH": str(manifest_path),
        "SK_BATCH_CHECKPOINT_PATH": str(checkpoint_path),
        "SK_BATCH_REPORT_PATH": str(batch_report_path),
        # A successful process yield is an intentional package/render-resource
        # lifetime boundary, not a crash retry.  Six large vegetation items per
        # commandlet keeps the working set bounded without per-asset startup.
        "SK_BATCH_MAX_ITEMS_PER_PROCESS": str(
            bounded_heavy_process_item_limit(
                environment.get("SK_BATCH_MAX_ITEMS_PER_PROCESS", "6")
            )
        ),
    })
    last_returncode = None
    crash_restarts = 0
    launch_count = 0
    planned_yields = 0
    restart_budget = max(0, int(max_restarts))
    while crash_restarts <= restart_budget:
        launch_count += 1
        try:
            prelaunch_checkpoint = json.loads(
                checkpoint_path.read_text(encoding="utf-8")
            )
        except (OSError, ValueError):
            prelaunch_checkpoint = {}
        prelaunch_yield = prelaunch_checkpoint.get("process_yield")
        print(
            f"UnrealEditor-Cmd headless ingest "
            f"(launch {launch_count}, crash restarts "
            f"{crash_restarts}/{restart_budget})",
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
        current_yield = checkpoint.get("process_yield")
        if (
            completed.returncode == 0
            and current_yield
            and current_yield != prelaunch_yield
        ):
            planned_yields += 1
            if planned_yields > 1000:
                raise ExactPushError(
                    "Unreal headless ingest exceeded 1000 planned process "
                    f"lifetime yields: {checkpoint_path}"
                )
            continue
        crash_restarts += 1
    raise ExactPushError(
        "UnrealEditor-Cmd did not complete the manifest after "
        f"{launch_count} launches and {crash_restarts} failed/incomplete "
        f"launches; crash restart budget={restart_budget}, "
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
        description=(
            "Push one exact assembled SK target through headless Unreal or the "
            "currently open Unreal Editor RPC endpoint"
        ),
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
    parser.add_argument(
        "--transport",
        choices=("headless", "rpc"),
        default="headless",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--defer-unreal", action="store_true")
    parser.add_argument("--prepared-report", type=Path)
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
            transport=args.transport,
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
    assembly_command = build_assembly_refresh_command(
        args.spm.expanduser().resolve(),
        blender=args.blender.expanduser().resolve(),
        material_contract=outputs["material_contract"],
        report_path=outputs["assembly_report"],
    )
    print("Refreshing assembled Blend from post-collision SpeedTree export", flush=True)
    assembly_completed = owned_run(
        assembly_command,
        source="sk_batch.exact_push.blender_assembly_refresh",
        run_factory=subprocess.run,
        check=False,
    )
    if assembly_completed.returncode != 0:
        print(
            "SK Exact Push assembly refresh failed; production report: "
            + str(outputs["assembly_report"]),
            file=sys.stderr,
        )
        return assembly_completed.returncode or 1
    try:
        assembly_report = json.loads(
            outputs["assembly_report"].read_text(encoding="utf-8")
        )
    except (OSError, ValueError) as exc:
        print(f"SK Exact Push assembly report is unreadable: {exc}", file=sys.stderr)
        return 1
    collision_cli = assembly_report.get("speedtree_collision_cli") or {}
    if (
        assembly_report.get("status") != "ok"
        or collision_cli.get("status") != "required"
    ):
        print(
            "SK Exact Push assembly did not prove the collision CLI contract: "
            + str(outputs["assembly_report"]),
            file=sys.stderr,
        )
        return 1
    try:
        live_material_contract = _promote_live_material_contract(
            command,
            outputs,
            assembly_report,
        )
    except ExactPushError as exc:
        print(
            "SK Exact Push assembly material contract is invalid: " + str(exc),
            file=sys.stderr,
        )
        return 1
    print(
        "Using assembly live material contract: " + str(live_material_contract),
        flush=True,
    )
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
        if args.transport == "rpc":
            if export_report.get("status") != "ok":
                raise ExactPushError(
                    "Blender RPC Push did not reach status=ok: "
                    + str(export_report)
                )
            report = export_report
        else:
            if export_report.get("status") != "exported_pending_unreal":
                raise ExactPushError(
                    "Blender export did not reach exported_pending_unreal: "
                    + str(export_report)
                )
            if args.defer_unreal:
                if args.prepared_report is None:
                    raise ExactPushError(
                        "--defer-unreal requires --prepared-report"
                    )
                prepared_report = args.prepared_report.expanduser().resolve()
                prepared_report.parent.mkdir(parents=True, exist_ok=True)
                prepared_report.write_text(
                    json.dumps({
                        "schema_version": 1,
                        "status": "prepared_pending_unreal",
                        "outputs": {
                            key: str(value) if value is not None else None
                            for key, value in outputs.items()
                        },
                    }, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                print("SK_EXACT_PUSH_PREPARED=" + str(prepared_report))
                return 0
            batch_result = run_headless_manifest(
                outputs["manifest"],
                outputs["checkpoint"],
                outputs["batch_report"],
                unreal_project=args.unreal_project,
                unreal_editor_cmd=args.unreal_editor_cmd,
            )
            report = merge_unreal_result(outputs, batch_result)
    except (OSError, ValueError, ExactPushError) as exc:
        print(f"SK Exact Push finalization failed: {exc}", file=sys.stderr)
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
