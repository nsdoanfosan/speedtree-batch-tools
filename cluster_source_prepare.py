"""Conditionally rebuild a Cluster source blend before Generator Sync.

Generator Sync consumes the normalized render mesh saved by the SK Batch/BWR
job.  This module does not weaken that hash contract.  It only runs the same
SPM bone-normalization, material preflight, and headless Blender source-build
stages when the canonical Cluster SPM proves that saved result is stale.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from cluster_normalization_sync import (
    ClusterSourceBuildRequiredError,
    resolve_normalization_recipe,
)
from sk_batch.repair_runtime_contract import write_repair_runtime_receipt
from sk_batch.sk_common import (
    LOG_DIR,
    atomic_write_bytes,
    blender_open_file_window_titles,
    load_config,
    load_job_report,
    prepare_cluster_spm_pair_for_job,
    summarize_job_failure,
    wind_preset_for_spm,
)


REPO_DIR = Path(__file__).resolve().parent
SK_BATCH_DIR = REPO_DIR / "sk_batch"


class ClusterSourcePreparationError(RuntimeError):
    """A named source-preparation stage failed before Cluster Sync."""

    def __init__(
        self,
        stage,
        message,
        *,
        log_file=None,
        report_file=None,
        report=None,
    ):
        detail = f"Cluster source preparation failed at {stage}: {message}"
        if log_file:
            detail += f" — log: {log_file}"
        if report_file:
            detail += f" — report: {report_file}"
        super().__init__(detail)
        self.stage = str(stage)
        self.log_file = Path(log_file) if log_file else None
        self.report_file = Path(report_file) if report_file else None
        self.report = report if isinstance(report, dict) else {}


def _notify(callback, stage, message):
    if callback is not None:
        callback(str(stage), str(message))


def _write_log(path, command, result=None, error=None):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["COMMAND", subprocess.list2cmdline([str(value) for value in command])]
    if result is not None:
        lines.extend([
            "",
            "STDOUT",
            str(result.stdout or ""),
            "",
            "STDERR",
            str(result.stderr or ""),
            "",
            f"EXIT_CODE={result.returncode}",
        ])
    if error is not None:
        lines.extend(["", "ERROR", str(error)])
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _run_stage(command, log_file, *, timeout, stage):
    try:
        result = subprocess.run(
            [str(value) for value in command],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=int(timeout),
            creationflags=(0x08000000 if os.name == "nt" else 0),
        )
    except (subprocess.SubprocessError, OSError) as exc:
        _write_log(log_file, command, error=exc)
        raise ClusterSourcePreparationError(
            stage,
            str(exc),
            log_file=log_file,
        ) from exc
    _write_log(log_file, command, result=result)
    return result


def _first_report(path):
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return {}
    if isinstance(payload, list) and payload and isinstance(payload[0], dict):
        return payload[0]
    return payload if isinstance(payload, dict) else {}


def _python_executable():
    executable = str(sys.executable)
    if executable.casefold().endswith("pythonw.exe"):
        return executable[:-11] + "python.exe"
    return executable


def _build_cluster_source(
    canonical_spm,
    blend,
    *,
    cfg,
    progress_callback=None,
):
    canonical_spm = Path(canonical_spm).resolve()
    blend = Path(blend).resolve()
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")

    open_windows = blender_open_file_window_titles(blend)
    if open_windows:
        raise ClusterSourcePreparationError(
            "interactive_blender_guard",
            "대상 Cluster .blend가 대화형 Blender에 열려 있습니다. "
            "저장하거나 닫은 뒤 다시 실행하세요: "
            + blend.name,
            report={
                "blend": str(blend),
                "open_windows": open_windows,
            },
        )

    _notify(
        progress_callback,
        "spm_bone_setup",
        f"Cluster 본 규격 정규화 · {canonical_spm.name}",
    )
    spm_report_path = LOG_DIR / f"{canonical_spm.stem}_spm_{stamp}.json"
    spm_log = LOG_DIR / f"{canonical_spm.stem}_spm_{stamp}.log"
    spm_command = [
        _python_executable(),
        "-X",
        "utf8",
        "-u",
        SK_BATCH_DIR / "spm_audit.py",
        canonical_spm,
        "--report",
        spm_report_path,
    ]
    spm_result = _run_stage(
        spm_command,
        spm_log,
        timeout=int(cfg.get("spm_verify_timeout", 120)) * 5,
        stage="spm_bone_setup",
    )
    spm_report = _first_report(spm_report_path)
    spm_status = str(spm_report.get("status") or "")
    if (
        spm_result.returncode != 0
        or spm_status not in {"calibrated", "already-ok"}
    ):
        reason = str(spm_report.get("error") or f"status={spm_status or 'missing'}")
        raise ClusterSourcePreparationError(
            "spm_bone_setup",
            reason,
            log_file=spm_log,
            report_file=spm_report_path,
            report=spm_report,
        )

    fbx_ini = Path(str(cfg.get("fbx_ini") or "")).resolve()
    try:
        speedtree_cli = fbx_ini.parents[2] / "speedtree_cli.py"
    except IndexError as exc:
        raise ClusterSourcePreparationError(
            "material_preflight",
            f"SpeedTree export helper path cannot be resolved from {fbx_ini}",
        ) from exc
    if not speedtree_cli.is_file():
        raise ClusterSourcePreparationError(
            "material_preflight",
            f"SpeedTree export helper does not exist: {speedtree_cli}",
        )

    _notify(
        progress_callback,
        "material_preflight",
        f"Cluster 렌더 소스 검사 · {canonical_spm.name}",
    )
    material_report_path = (
        LOG_DIR / f"{canonical_spm.stem}_material_preflight_{stamp}.json"
    )
    material_log = LOG_DIR / f"{canonical_spm.stem}_material_preflight_{stamp}.log"
    material_command = [
        _python_executable(),
        SK_BATCH_DIR / "jobs" / "speedtree_material_preflight.py",
        "--spm",
        canonical_spm,
        "--canonical-spm",
        canonical_spm,
        "--speedtree-exe",
        cfg["speedtree_exe"],
        "--fbx-ini",
        fbx_ini,
        "--speedtree-cli",
        speedtree_cli,
        "--report",
        material_report_path,
        "--timeout",
        int(cfg.get("speedtree_material_preflight_timeout", 900)),
    ]
    material_result_process = _run_stage(
        material_command,
        material_log,
        timeout=int(cfg.get("speedtree_material_preflight_timeout", 900)) + 30,
        stage="material_preflight",
    )
    material_report = load_job_report(material_report_path)
    if (
        material_result_process.returncode != 0
        or material_report.get("status") != "ok"
    ):
        raise ClusterSourcePreparationError(
            "material_preflight",
            summarize_job_failure(material_report, material_log),
            log_file=material_log,
            report_file=material_report_path,
            report=material_report,
        )

    _notify(
        progress_callback,
        "cluster_source_build",
        f"Cluster 기준 Blend 재생성 · {blend.name}",
    )
    job_report_path = LOG_DIR / f"{canonical_spm.stem}_bwr_{stamp}.json"
    job_log = LOG_DIR / f"{canonical_spm.stem}_bwr_{stamp}.log"
    pipeline_report = (
        canonical_spm.parent
        / "reports"
        / f"{canonical_spm.stem}_speedtree_repair_pipeline_report_codex.json"
    )
    try:
        previous_pipeline_report = pipeline_report.read_bytes()
    except OSError:
        previous_pipeline_report = None
    job_command = [
        cfg["blender_exe"],
        "--factory-startup",
        "-b",
        "--python",
        SK_BATCH_DIR / "jobs" / "bwr_headless_job.py",
        "--",
        "--spm",
        canonical_spm,
        "--speedtree-spm",
        canonical_spm,
        "--blend",
        blend,
        "--wind",
        wind_preset_for_spm(canonical_spm),
        "--cluster-source-build-only",
        "--material-contract",
        material_report_path,
        "--report",
        job_report_path,
    ]
    job_process = _run_stage(
        job_command,
        job_log,
        timeout=int(cfg.get("blender_job_timeout", 3600)),
        stage="cluster_source_build",
    )
    job_report = load_job_report(job_report_path)
    source_build_contract = (
        job_report.get("cluster_source_build_contract") or {}
    )
    if (
        job_process.returncode != 0
        or job_report.get("status") != "ok"
        or source_build_contract.get("status") != "ready"
    ):
        if previous_pipeline_report is not None:
            try:
                atomic_write_bytes(pipeline_report, previous_pipeline_report)
            except OSError:
                pass
        raise ClusterSourcePreparationError(
            "cluster_source_build",
            summarize_job_failure(job_report, job_log),
            log_file=job_log,
            report_file=job_report_path,
            report=job_report,
        )

    runtime_receipt = write_repair_runtime_receipt(canonical_spm, cfg)
    return {
        "status": "rebuilt",
        "spm": str(canonical_spm),
        "blend": str(blend),
        "spm_bone_setup": {
            "status": spm_status,
            "report": str(spm_report_path),
            "log": str(spm_log),
        },
        "material_preflight": {
            "status": "ok",
            "report": str(material_report_path),
            "log": str(material_log),
        },
        "cluster_source_build": {
            "status": "ok",
            "contract": source_build_contract,
            "report": str(job_report_path),
            "log": str(job_log),
            "pipeline_report": str(pipeline_report),
        },
        "runtime_receipt": str(runtime_receipt) if runtime_receipt else "",
    }


def prepare_cluster_source_if_required(
    blend,
    target_spms,
    *,
    blender_exe,
    unit_probe_path,
    capture_resolution=1024,
    progress_callback=None,
):
    """Return immediately when current; otherwise rebuild and revalidate once."""
    blend = Path(blend).expanduser().absolute()
    targets = [Path(path).expanduser().absolute() for path in target_spms]
    canonical_spm = blend.with_suffix(".spm")
    try:
        resolve_normalization_recipe(
            blend,
            targets,
            canonical_spm=canonical_spm,
            unit_probe_path=unit_probe_path,
            capture_resolution=capture_resolution,
        )
        return {
            "status": "current",
            "spm": str(canonical_spm),
            "blend": str(blend),
        }
    except ClusterSourceBuildRequiredError as required:
        rebuild_reason = required.reason

    pair = prepare_cluster_spm_pair_for_job(canonical_spm)
    canonical_spm = Path(pair.get("canonical_spm") or canonical_spm).resolve()
    canonical_blend = canonical_spm.with_suffix(".blend")
    if canonical_blend != blend.resolve():
        raise ClusterSourcePreparationError(
            "cluster_pair_contract",
            "Cluster relation points at a non-canonical blend: "
            f"{blend} (expected {canonical_blend})",
            report=pair,
        )

    cfg = load_config()
    cfg["blender_exe"] = str(Path(blender_exe).expanduser().absolute())
    result = _build_cluster_source(
        canonical_spm,
        canonical_blend,
        cfg=cfg,
        progress_callback=progress_callback,
    )
    result["reason"] = rebuild_reason

    _notify(
        progress_callback,
        "source_contract_validation",
        f"Cluster 기준 Blend 계약 재검사 · {canonical_blend.name}",
    )
    try:
        resolve_normalization_recipe(
            canonical_blend,
            targets,
            canonical_spm=canonical_spm,
            unit_probe_path=unit_probe_path,
            capture_resolution=capture_resolution,
        )
    except Exception as exc:
        raise ClusterSourcePreparationError(
            "source_contract_validation",
            str(exc),
            report=result,
        ) from exc
    return result


__all__ = [
    "ClusterSourcePreparationError",
    "prepare_cluster_source_if_required",
]
