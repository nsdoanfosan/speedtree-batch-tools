"""SK Vegetation Batch — SPM 본 캘리브레이션 + Blender Repair + Unreal Push GUI.

단계 (왼쪽부터, 빠른 것 → 느린 것):
  🔍 검사        : 아무것도 수정하지 않고 상태만 표에 채움 (SPM 본 세팅 상태,
                   M_ 머티리얼, blend 최신 여부, 핸드오프 JSON 준비 여부)
  ① SPM 본 세팅 : 가지 수를 실측(프로브 익스포트)해서 "가지당 목표 본 수"에
                   맞게 Relative 값을 자동 계산. 파일당 수십초~수분. 실패해도
                   백업에서 자동 복원되므로 여기서 전부 끝내고 ②로 넘어가면 됨.
  ② Blender Repair : 헤드리스 Blender로 import/repair 후 SPM 옆에 .blend 저장.
                   파일당 수분~수십분(느림). 이미 최신인 blend는 건너뜀.
  ③ Unreal Push : 보내기 전에 준비 검사(blend/JSON 존재, 언리얼 실행 여부)를
                   먼저 전부 통과시킨 뒤에만 실제 push 시작.

모든 무거운 작업은 낮은 우선순위 + CPU 코어 제한이 걸린 백그라운드 프로세스로
실행된다 (자식 SpeedTree CLI에 상속. 헤드리스 Blender는 GPU를 쓰지 않음).
"""
import copy
import ctypes
import hashlib
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
import traceback
import uuid
import xml.etree.ElementTree as ET
from collections import deque
from concurrent.futures import (
    Future,
    ThreadPoolExecutor,
    TimeoutError as FutureTimeoutError,
    as_completed,
)
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

TOOL_DIR = Path(__file__).resolve().parent
REPO_DIR = TOOL_DIR.parent
sys.path.insert(0, str(REPO_DIR))
sys.path.insert(0, str(TOOL_DIR))

from process_lifecycle import owned_run

from code_compile_gate import (
    CompileGateError,
    production_source_manifest,
    run_gate as run_code_compile_gate,
    validate_production_source_manifest,
    validate_production_source_revision_report,
)
_PROCESS_PRODUCTION_SOURCE_MANIFEST = production_source_manifest(REPO_DIR)
from batch_ui_common import CheckedRowController, copy_selected_row_paths
from shared_queue_runtime import SharedQueueRuntime, WaitCancelled
from exact_target_command import (
    build_exact_target_request,
    run_exact_target_request,
)
from repair_orchestration import (
    ATLAS_MANIFEST_MIRROR_REPAIR,
    ALL_REPAIR_CONTRACT_CODES,
    DURABLE_FAILURE_REASON_CODES,
    GENERATOR_SYNC_TOOL,
    MODELER_NODE_TABLE_RECOVERY,
    MODELER_RECOVERY_TOOL,
    PCG_TEXTURE_TOOL,
    REPAIR_PLAN_SCHEMA_VERSION,
    REPAIR_UI_AUTOMATIC,
    REPAIR_UI_BLOCKED,
    RepairPlan,
    STATUS_CANCELLED,
    STATUS_CLUSTER,
    STATUS_COMPLETED,
    STATUS_FINAL_FAILED,
    STATUS_GENERATOR,
    STATUS_LABELS as AUTOMATIC_REPAIR_STATUS_LABELS,
    STATUS_PENDING,
    STATUS_PIPELINE,
    STATUS_REAUDIT,
    build_exact_target_repair_plan,
    compact_success_message,
    evidence_reason_codes,
    fresh_repair_receipt_authoritative,
    has_repair_contract_evidence,
    repair_progress_payload,
    repair_ui_decision,
    stage_running_status,
)
from repair_failure_provenance import (
    REPAIR_FAILURE_KEY,
    build_repair_failure,
    mark_fresh_reaudit_attempted,
    needs_fresh_reaudit,
    repair_failure_record,
    root_reason_codes as repair_root_reason_codes,
)
from child_progress_contract import (
    material_preflight_inactivity_rules,
    send2ue_inactivity_rules,
)
from retry_progress import (
    BLENDER as RETRY_STAGE_BLENDER,
    BLOCKED as RETRY_STAGE_BLOCKED,
    CANCELLED as RETRY_STAGE_CANCELLED,
    CLAIMED as RETRY_STAGE_CLAIMED,
    COMPLETE as RETRY_STAGE_COMPLETE,
    FAILED as RETRY_STAGE_FAILED,
    OWNER_LOST as RETRY_STAGE_OWNER_LOST,
    PENDING_UNREAL as RETRY_STAGE_PENDING_UNREAL,
    PLANNING as RETRY_STAGE_PLANNING,
    POST_CHECK as RETRY_STAGE_POST_CHECK,
    SEND2UE as RETRY_STAGE_SEND2UE,
    SHARED_QUEUE_WAIT as RETRY_STAGE_SHARED_QUEUE_WAIT,
    UNREAL as RETRY_STAGE_UNREAL,
    RetryProgressReceipt,
    stage_for_send2ue_marker,
)
from retry_planning import (
    MAX_REPORT_BYTES as RETRY_PLANNING_MAX_REPORT_BYTES,
    RetryPlanningContext,
    RetryPlanningSnapshotError,
    build_plan_cache_artifact,
    cheap_durable_candidate,
    hydrate_plan_cache_artifact,
    planning_input_signature,
)
from artifact_content_key import (
    artifact_record_content_key,
    file_content_key_snapshot,
    sampled_file_content_snapshot,
)

from sk_common import (
    CALIBRATION_CACHE_VERSION,
    LOG_DIR,
    PUSH_ABORT_KINDS,
    PUSH_MANIFEST_SCHEMA_VERSION,
    PUSH_SOURCE_FINGERPRINT_CACHE_VERSION,
    SPM_BONE_CONTRACT_VERSION,
    atomic_write_bytes,
    atomic_write_json,
    blend_path_for,
    cached_file_content_snapshot,
    cached_push_source_fingerprint,
    calibration_cache_matches,
    calibration_settings_signature,
    classify_push_failure,
    close_process_kill_job,
    compact_error_message,
    file_content_snapshot,
    is_manual_bones_locked,
    legacy_calibration_settings_signature,
    launch_limited,
    load_config,
    load_job_report,
    load_state,
    manifest_item_files_match,
    manual_bones_marker_path,
    save_config,
    save_state,
    scan_cluster_spm_sources,
    scan_sk_spms,
    set_manual_bones_marker,
    push_source_snapshot,
    prepare_cluster_spm_pair_for_job,
    summarize_job_failure,
    speedtree_output_spm_for,
    terminate_process_tree,
    unreal_remote_execution_settings,
    wind_preset_for_spm,
)
from spm_leaf_handoff_contract import (
    inspect_all_speedtree_material_export,
    inspect_speedtree_material_export,
    inspect_spm_leaf_contract,
    inspect_spm_mesh_file_references,
    leaf_contract_user_message,
    save_leaf_contract_cache,
    speedtree_stmat_path,
)
from speedtree_pipeline_contract import (
    build_preflight_envelope,
    is_live_spm,
    source_identity,
    validate_preflight_envelope,
)
from speedtree_texture_contract import (
    TEXTURE_ORIGIN_NEEDS_PCG_GENERATION,
)
from push_dependency_schedule import (
    PushDependencyError,
    exact_dependency_contract_from_validated_manifest,
    expand_push_targets,
)
from failed_retry_eligibility import (
    BLENDER_EXPORT_RETRY_FAILURE_KINDS,
    BLENDER_REBUILD,
    CURRENT_BLENDER_EXCLUDED,
    PENDING_UNREAL_VALIDATION,
    RETRY_ELIGIBILITY_SCHEMA_VERSION,
    UNREAL_ONLY,
    UNREAL_PARENT_ABSENT,
    UNREAL_PARENT_CANDIDATE,
    UNREAL_PARENT_CURRENT,
    UNREAL_PARENT_DEPENDENCY_REBUILD,
    UNREAL_PARENT_INCOMPLETE,
    UNREAL_PARENT_INVALID,
    UNREAL_RECOVERY_FAILURE_KINDS,
    classify_failed_retry,
)
from push_unreal_recovery import (
    PushUnrealRecoveryError,
    dependency_closure as unreal_recovery_dependency_closure,
    load_parent_manifest as load_unreal_recovery_parent_manifest,
    recover_manifest_item,
    validate_unreal_only_recovery_evidence,
)
from send2ue_manifest_contract import (
    is_actionable_cluster_assembly_manifest,
)
from pcg_st9_texture_batch.pcg_cluster_assembly_contract import (
    ClusterAssemblyReceiptAmbiguityError,
    ClusterAssemblyReceiptStaleError,
    cluster_assembly_receipt_resolution,
    load_cluster_assembly_receipt,
)
from pcg_st9_texture_batch.pcg_canonical_outputs import (
    CanonicalOutputManifestError,
    refresh_atlas_manifests_for_spm,
)
from atlas_manifest_resolver import atlas_manifest_mirror_repair_plan
from repair_runtime_contract import (
    REPAIR_OUTPUT_CONTRACT_VERSION,
    REPAIR_RUNTIME_RECEIPT_VERSION,
    addon_dir_from_config,
    migrate_repair_runtime_receipt,
    repair_runtime_code_paths,
    repair_runtime_code_state,
    repair_runtime_output_contract,
    repair_runtime_receipt_needs_migration,
    repair_runtime_receipt_path,
    write_repair_runtime_receipt,
)
from repair_push_evidence import (
    RepairPushEvidenceError,
    build_repair_push_evidence_bundle,
    stale_execution_freeze_message,
    validate_repair_push_evidence_bundle,
)
from spm_audit import (
    cluster_root_logical_postcondition,
    current_bone_semantic_fingerprint,
    read_spm,
)
from spm_calibration_receipt import write_positive_calibration_receipt

WIND_OPTIONS = (
    ("자동 (식생 종류 기준)", "auto"),
    ("TREE", "TREE"),
    ("BUSH", "BUSH"),
    ("GRASS", "GRASS"),
    ("NONE", "NONE"),
)
BONE_MODE_OPTIONS = (("자동 계산", "auto"), ("수동 본 유지", "manual"))
PLANNED_EXCLUSION_KINDS = frozenset({
    "dependency_blocked",
    "manual_required",
    "planned_excluded",
    "preflight_skip",
    "source_review",
    "stale_execution_freeze",
})
_INLINE_ATLAS_REPAIR_LOCK = threading.RLock()
_REGISTERED_RELATION_REPAIR_LOCK = threading.RLock()
CHECK_ON = "☑"
CHECK_OFF = "☐"
STATUS_COLUMNS = ("spm_status", "blend_status", "push_status")
# Temporary production drain mode: select only owner rows that already have
# cluster/*.spm providers but do not yet have a local Assembly output.
TEMP_SELECT_CLUSTER_WITHOUT_ASSEMBLY_PUSH_ROWS = True
# Temporary retry drain mode: select only rows whose saved push evidence the
# ordinary failed-retry route cannot admit, so one forced rerun drains exactly
# those instead of rebuilding everything that already imported (#175). Takes
# precedence over the cluster drain mode above while it is on.
TEMP_SELECT_RETRY_BLOCKED_ROWS = True
# push_status_kind values that are in neither BLENDER_EXPORT_RETRY_FAILURE_KINDS
# nor UNREAL_RECOVERY_FAILURE_KINDS and carry no evidence of what to repair, so
# classify_failed_retry keeps failing closed on them. Only an explicit force
# rerun can drain these.
#
# `ready` / `exported_pending_unreal` are deliberately NOT here: those are now
# routed by PUSH_INCOMPLETE_KINDS on the ordinary retry path and need no force.
RETRY_BLOCKED_PUSH_STATUS_KINDS = frozenset({
    "automatic_repair_failed",
    "automatic_repair_reaudit_failed",
    "planned_excluded",
    "preflight_skip",
})
CLUSTER_RELATION_LOCKS_GUARD = threading.Lock()
_REPAIR_REPORT_READ_LOCAL = threading.local()
CLUSTER_RELATION_LOCKS = {}
MATERIAL_PREFLIGHT_START_MARKER = "SK_BATCH_MATERIAL_PREFLIGHT_START"
MATERIAL_PREFLIGHT_STATIC_DONE_MARKER = (
    "SK_BATCH_MATERIAL_PREFLIGHT_STATIC_DONE"
)
SPEEDTREE_SLOT_WAIT_MARKER = "SK_BATCH_SPEEDTREE_SLOT_WAIT"
SPEEDTREE_SLOT_ACQUIRED_MARKER = "SK_BATCH_SPEEDTREE_SLOT_ACQUIRED"
MATERIAL_PREFLIGHT_EXPORT_DONE_MARKER = (
    "SK_BATCH_MATERIAL_PREFLIGHT_EXPORT_DONE"
)
MATERIAL_PREFLIGHT_INSPECTION_DONE_MARKER = (
    "SK_BATCH_MATERIAL_PREFLIGHT_INSPECTION_DONE"
)
MATERIAL_PREFLIGHT_CONTRACT_DONE_MARKER = (
    "SK_BATCH_MATERIAL_PREFLIGHT_CONTRACT_DONE"
)
MATERIAL_PREFLIGHT_FAILED_MARKER = "SK_BATCH_MATERIAL_PREFLIGHT_FAILED"
MATERIAL_PREFLIGHT_DONE_MARKER = "SK_BATCH_MATERIAL_PREFLIGHT_DONE"
CLUSTER_LIVE_AUDIT_START_MARKER = "SK_BATCH_CLUSTER_LIVE_AUDIT_START"
CLUSTER_LIVE_AUDIT_REVISION_OK_MARKER = (
    "SK_BATCH_CLUSTER_LIVE_AUDIT_REVISION_OK"
)
CLUSTER_LIVE_AUDIT_REPORT_START_MARKER = (
    "SK_BATCH_CLUSTER_LIVE_AUDIT_REPORT_START"
)
CLUSTER_LIVE_AUDIT_FOLDER_DONE_MARKER = (
    "SK_BATCH_CLUSTER_LIVE_AUDIT_FOLDER_DONE"
)
CLUSTER_LIVE_AUDIT_REPORT_DONE_MARKER = (
    "SK_BATCH_CLUSTER_LIVE_AUDIT_REPORT_DONE"
)
CLUSTER_LIVE_AUDIT_RECEIPT_START_MARKER = (
    "SK_BATCH_CLUSTER_LIVE_AUDIT_RECEIPT_START"
)
CLUSTER_LIVE_AUDIT_RECEIPT_DONE_MARKER = (
    "SK_BATCH_CLUSTER_LIVE_AUDIT_RECEIPT_DONE"
)
CLUSTER_LIVE_AUDIT_FAILED_MARKER = "SK_BATCH_CLUSTER_LIVE_AUDIT_FAILED"
CLUSTER_LIVE_AUDIT_DONE_MARKER = "SK_BATCH_CLUSTER_LIVE_AUDIT_DONE"


def material_preflight_mesh_reference_block(spm):
    """Return a read-only early block for a referenced missing external mesh."""
    contract = inspect_spm_mesh_file_references(spm)
    missing = list(contract.get("missing") or [])
    if not missing:
        return None
    names = ", ".join(
        str(row.get("filename") or "?")
        for row in missing[:8]
    )
    if len(missing) > 8:
        names += f" 외 {len(missing) - 8}개"
    return {
        "status": "blocked",
        "stage": "speedtree_material_static_preflight",
        "classification": "asset_external_mesh_path_missing",
        "error": (
            "SPM이 참조하는 외부 메시 FBX "
            f"{len(missing)}개가 디스크에 없음 — {names}. "
            "이 상태의 SpeedTree 익스포트는 타임아웃까지 멈추므로 "
            "Cluster live audit와 재질 익스포트 전에 차단했습니다."
        ),
        "remediation": (
            "SK Batch performs no source mutation. The item remains blocked "
            "until every authored external Mesh Asset reference resolves."
        ),
        "mesh_file_reference_contract": contract,
        "missing_external_meshes": [
            {
                "filename": str(row.get("filename") or ""),
                "resolved_path": str(row.get("resolved_path") or ""),
                "mesh_name": str(row.get("mesh_name") or ""),
            }
            for row in missing
        ],
    }


def is_cluster_source_spm(spm):
    path = Path(spm)
    return (
        is_live_spm(path, require_file=False)
        and path.parent.name.casefold() == "cluster"
    )


def should_refresh_canonical_atlas_manifests(spm):
    """Return whether this SPM is an Atlas-producing Cluster source."""
    return is_cluster_source_spm(spm)


def current_cluster_root_postcondition(spm):
    if not is_cluster_source_spm(spm):
        return {"ok": True, "mode": "not_cluster_source"}
    try:
        return cluster_root_logical_postcondition(read_spm(spm))
    except (OSError, ValueError, RuntimeError) as exc:
        return {"ok": False, "error": str(exc)}


def validate_spm_audit_result(spm, report, final_snapshot):
    if not isinstance(final_snapshot, dict):
        raise RuntimeError("본 세팅 후 최종 SPM 지문을 계산하지 못함")
    reported_fingerprint = str(report.get("final_spm_fingerprint") or "")
    if reported_fingerprint != final_snapshot.get("fingerprint"):
        raise RuntimeError(
            "본 세팅 결과 보고서의 최종 SPM 지문이 실제 파일과 "
            f"일치하지 않음: {Path(spm).name}"
        )
    if is_cluster_source_spm(spm):
        reported_postcondition = (
            report.get("cluster_root_logical_postcondition") or {}
        )
        actual_postcondition = current_cluster_root_postcondition(spm)
        if (
            reported_postcondition.get("ok") is not True
            or actual_postcondition.get("ok") is not True
        ):
            raise RuntimeError(
                "Cluster SPM 최종 root-bone 정규화 조건이 충족되지 않음: "
                f"{reported_postcondition or actual_postcondition}"
            )
    return True


def manifest_item_requires_unreal_asset_verification(item):
    """Return True when a cache hit still has a live Unreal asset contract."""
    assembly = (item or {}).get("cluster_assembly") or {}
    plan = assembly.get("ingest_plan") or {}
    return (
        plan.get("status") == "ready"
        and isinstance(plan.get("asset_contract"), dict)
        and bool(plan.get("asset_contract"))
    )


def repair_pipeline_report_path(spm):
    spm = Path(spm)
    return (
        spm.parent / "reports" /
        f"{spm.stem}_speedtree_repair_pipeline_report_codex.json"
    )


def owner_cluster_spm_paths(spm):
    """Return direct cluster/*.spm providers belonging to one owner row."""
    spm = Path(spm)
    if is_cluster_source_spm(spm):
        return ()
    try:
        cluster_dirs = (
            child
            for child in spm.parent.iterdir()
            if child.is_dir() and child.name.casefold() == "cluster"
        )
        return tuple(sorted(
            candidate
            for cluster_dir in cluster_dirs
            for candidate in cluster_dir.iterdir()
            if (
                is_live_spm(candidate)
                and not candidate.name.startswith("~")
            )
        ))
    except OSError:
        return ()


def is_cluster_without_assembly_push_row(spm):
    """Return whether an owner has cluster sources but no local Assembly."""
    spm = Path(spm)
    if is_cluster_source_spm(spm) or not owner_cluster_spm_paths(spm):
        return False
    try:
        report = _read_repair_pipeline_json(
            repair_pipeline_report_path(spm)
        )
    except (OSError, ValueError):
        return True
    return not is_actionable_cluster_assembly_manifest(
        report.get("cluster_assembly_manifest")
    )


@contextmanager
def repair_report_read_scope():
    """Reuse one large Repair JSON only within one semantic status decision."""
    previous = getattr(_REPAIR_REPORT_READ_LOCAL, "cache", None)
    _REPAIR_REPORT_READ_LOCAL.cache = {}
    try:
        yield
    finally:
        if previous is None:
            try:
                del _REPAIR_REPORT_READ_LOCAL.cache
            except AttributeError:
                pass
        else:
            _REPAIR_REPORT_READ_LOCAL.cache = previous


def _repair_report_stat_key(path):
    path = Path(path)
    stat = path.stat()
    return (
        os.path.normcase(os.path.abspath(str(path))),
        stat.st_size,
        stat.st_mtime_ns,
    )


def _read_repair_pipeline_json(path):
    """Read a report once per scope without retaining multi-MB data globally."""
    path = Path(path)
    cache = getattr(_REPAIR_REPORT_READ_LOCAL, "cache", None)
    key = _repair_report_stat_key(path)
    if cache is not None and key in cache:
        return cache[key]
    payload = json.loads(path.read_text(encoding="utf-8"))
    if cache is not None:
        # A status decision concerns one SPM. Keep this cache explicitly
        # bounded in case a future nested helper consults a second report.
        cache.clear()
        cache[key] = payload
    return payload


def _cache_written_repair_pipeline_json(path, payload):
    cache = getattr(_REPAIR_REPORT_READ_LOCAL, "cache", None)
    if cache is None:
        return
    cache.clear()
    cache[_repair_report_stat_key(path)] = payload


def _artifact_fingerprints_match(expected, actual):
    """Compare content-addressed artifacts without trusting timestamps alone."""
    if not isinstance(expected, dict) or not isinstance(actual, dict):
        return False
    try:
        expected_path = os.path.normcase(
            os.path.abspath(str(expected.get("path") or ""))
        )
        actual_path = os.path.normcase(
            os.path.abspath(str(actual.get("path") or ""))
        )
        expected_size = int(expected.get("size"))
        actual_size = int(actual.get("size"))
    except (TypeError, ValueError):
        return False
    return bool(
        expected_path
        and expected_path == actual_path
        and expected.get("exists") is True
        and actual.get("exists") is True
        and expected_size == actual_size
        and expected.get("sha256")
        and str(expected.get("sha256")).casefold()
        == str(actual.get("sha256") or "").casefold()
    )


def _material_handoff_envelope_for_push(pipeline, canonical_spm):
    """Select and bind the exact material input that produced the blend."""
    canonical_spm = Path(canonical_spm).resolve()
    handoff = pipeline.get("speedtree_material_handoff_contract")
    if not isinstance(handoff, dict):
        return (
            pipeline.get("speedtree_pipeline_contract"),
            canonical_spm,
        )

    source_spm = (
        ((handoff.get("source") or {}).get("spm") or {}).get(
            "canonical_path"
        )
        or ""
    )
    if not source_spm:
        raise RuntimeError(
            "SpeedTree material handoff contract has no source SPM identity"
        )
    source_spm = Path(source_spm).resolve()
    resolution = pipeline.get("cluster_bark_source_resolution") or {}
    isolated = resolution.get("speedtree_spm") or {}
    production = resolution.get("source_spm") or {}
    isolated_mode = bool(
        resolution.get("status") == "ready"
        and _normalized_path(production.get("path") or "")
        != _normalized_path(isolated.get("path") or "")
    )
    if (
        _normalized_path(source_spm) == _normalized_path(canonical_spm)
        and not isolated_mode
    ):
        return handoff, source_spm
    if not isolated_mode:
        raise RuntimeError(
            "SpeedTree material handoff source differs from the canonical "
            "SPM without an isolated bark-normalization contract"
        )

    handoff_source = (handoff.get("source") or {}).get("spm") or {}
    live_production = source_identity(canonical_spm)

    def same_identity(left, right, *, left_path_key, right_path_key):
        try:
            return bool(
                _normalized_path(left.get(left_path_key) or "")
                == _normalized_path(right.get(right_path_key) or "")
                and int(left.get("size")) == int(right.get("size"))
                and str(left.get("sha256") or "").casefold()
                == str(right.get("sha256") or "").casefold()
                and left.get("sha256")
            )
        except (TypeError, ValueError):
            return False

    if not (
        resolution.get("status") == "ready"
        and same_identity(
            handoff_source,
            isolated,
            left_path_key="canonical_path",
            right_path_key="path",
        )
        and same_identity(
            live_production,
            production,
            left_path_key="canonical_path",
            right_path_key="path",
        )
    ):
        raise RuntimeError(
            "SpeedTree isolated material handoff is not bound to the "
            "current canonical SPM and bark-normalization receipt"
        )
    return handoff, source_spm


def cluster_bark_pipeline_matches_resolution(
        spm, resolution, pipeline, fingerprint):
    """Prove that a cached isolated-bark source was actually consumed by BWR."""
    if not isinstance(resolution, dict) or resolution.get("status") not in {
        "prepared",
        "cached",
    }:
        return True
    if not isinstance(pipeline, dict):
        return False
    captured = pipeline.get("cluster_bark_source_resolution") or {}
    validation = pipeline.get("cluster_bark_export_validation") or {}
    if (
        captured.get("status") != "ready"
        or validation.get("status")
        != "ready_for_downstream_blender_mapping"
        or validation.get("production_sources_mutated") is not False
    ):
        return False
    expected_material = str(
        (resolution.get("normalization") or {}).get(
            "canonical_material"
        )
        or ""
    ).casefold()
    captured_material = str(
        captured.get("canonical_material") or ""
    ).casefold()
    if expected_material and captured_material != expected_material:
        return False
    artifacts = (
        ("manifest", resolution.get("manifest")),
        ("source_spm", resolution.get("source_spm") or spm),
        ("speedtree_spm", resolution.get("speedtree_spm")),
    )
    for key, path in artifacts:
        if not path:
            return False
        if not _artifact_fingerprints_match(
            captured.get(key),
            fingerprint(Path(path)),
        ):
            return False
    return True


def cluster_bark_resolution_requires_repair(spm, resolution):
    """Return True until BWR has captured this exact isolated bark bundle."""
    if not isinstance(resolution, dict) or resolution.get("status") not in {
        "prepared",
        "cached",
    }:
        return False
    try:
        from cluster_assembly_builder import file_fingerprint

        pipeline = load_current_repair_pipeline_report(spm)
        return not cluster_bark_pipeline_matches_resolution(
            spm,
            resolution,
            pipeline,
            file_fingerprint,
        )
    except (OSError, TypeError, ValueError, RuntimeError):
        return True


def cluster_receipt_resolution_uses_live_audit(resolution):
    return bool(
        isinstance(resolution, dict)
        and str(resolution.get("policy") or "").startswith("live_audit")
        and resolution.get("live_audit_report")
    )


def _cluster_variant_artifact_identity(record):
    if not isinstance(record, dict):
        return None
    sha256 = str(record.get("sha256") or "").casefold()
    path = str(record.get("path") or "")
    try:
        size = int(record.get("size"))
    except (TypeError, ValueError):
        return None
    if not sha256 or not path or size < 0:
        return None
    return {
        "path": os.path.normcase(os.path.abspath(path)).casefold(),
        "size": size,
        "sha256": sha256,
    }


def _cluster_prepared_role_identity(role):
    if not isinstance(role, dict):
        return None
    normalized = role.get("normalized_variants") or {}
    if normalized.get("status") != "ready":
        return None
    manifest = _cluster_variant_artifact_identity(
        normalized.get("manifest")
    )
    source_blend = _cluster_variant_artifact_identity(
        normalized.get("source_blend")
    )
    if manifest is None or source_blend is None:
        return None
    variants = []
    for variant in normalized.get("variants") or []:
        plan_fbx = _cluster_variant_artifact_identity(
            variant.get("plan_fbx") if isinstance(variant, dict) else None
        )
        if plan_fbx is None:
            return None
        variants.append({
            "ordinal": variant.get("ordinal"),
            "plan_name": str(variant.get("plan_name") or ""),
            "skeletal_asset_name": str(
                variant.get("skeletal_asset_name") or ""
            ),
            "source_prototype_index": variant.get(
                "source_prototype_index"
            ),
            "source_partition_mode": str(
                variant.get("source_partition_mode") or ""
            ),
            "target_mesh_id": variant.get("target_mesh_id"),
            "physical_capture_contract_sha256": str(
                variant.get("physical_capture_contract_sha256") or ""
            ).casefold(),
            "plan_fbx": plan_fbx,
        })
    return {
        "role": str(role.get("role") or "").casefold(),
        "material": str(normalized.get("material") or "").casefold(),
        "material_id": str(normalized.get("material_id") or ""),
        "contract": str(normalized.get("contract") or ""),
        "manifest": manifest,
        "source_blend": source_blend,
        "variants": sorted(
            variants,
            key=lambda row: (
                str(row["ordinal"]),
                row["plan_name"].casefold(),
            ),
        ),
    }


def rendered_unused_pass_through_matches_live(
    saved_manifest,
    live_contract,
):
    """Keep a content-proven unused role from recurring on every live audit."""
    if not isinstance(saved_manifest, dict) or not isinstance(
        live_contract, dict
    ):
        return False
    try:
        rendered_role_count = int(
            saved_manifest.get("rendered_role_count", -1)
        )
    except (TypeError, ValueError):
        return False
    if not (
        saved_manifest.get("status") == "pass_through"
        and saved_manifest.get("content_decision") == "pass_through"
        and saved_manifest.get("reason")
        == "normalized_roles_are_prepared_but_unused_by_rendered_mesh"
        and rendered_role_count == 0
    ):
        return False
    saved_roles = (
        saved_manifest.get("prepared_unused_roles")
        or (saved_manifest.get("handoff_evidence") or {}).get(
            "prepared_unused_roles"
        )
        or {}
    )
    live_handoff = live_contract.get("handoff") or {}
    if live_handoff.get("status") != "ready":
        return False
    saved_identities = [
        _cluster_prepared_role_identity(role)
        for role in (
            saved_roles.values()
            if isinstance(saved_roles, dict)
            else saved_roles
        )
    ]
    live_identities = [
        _cluster_prepared_role_identity(role)
        for role in live_handoff.get("roles") or []
        if isinstance(role, dict) and role.get("normalized_variants")
    ]
    if (
        not saved_identities
        or any(identity is None for identity in saved_identities)
        or any(identity is None for identity in live_identities)
    ):
        return False
    order = lambda row: (
        row["role"],
        row["material"],
        row["material_id"],
    )
    return sorted(saved_identities, key=order) == sorted(
        live_identities, key=order
    )


def dynamic_wind_skeleton_contract_ready(path):
    """Validate the derived wind file against its own final-skeleton identity."""
    path = Path(path)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return False, f"DynamicWind JSON 읽기 실패: {exc}"
    contract = payload.get("SkeletonContract")
    if not isinstance(contract, dict):
        return False, "DynamicWind JSON에 최종 SkeletonContract가 없음"
    try:
        schema_version = int(contract.get("SchemaVersion", -1))
        bone_count = int(contract.get("BoneCount", -1))
    except (TypeError, ValueError):
        return False, "DynamicWind SkeletonContract 수치가 잘못됨"
    bones = contract.get("Bones")
    if (
        schema_version != 2
        or bone_count <= 0
        or not isinstance(bones, list)
        or len(bones) != bone_count
    ):
        return False, "DynamicWind SkeletonContract 본 목록이 불완전함"
    digest = hashlib.sha1()
    for expected_index, row in enumerate(bones):
        if not isinstance(row, dict):
            return False, "DynamicWind SkeletonContract 본 항목이 잘못됨"
        name = str(row.get("BoneName") or "")
        try:
            index = int(row.get("BoneIndex", -1))
            parent = int(row.get("ParentIndex", -2))
        except (TypeError, ValueError):
            return False, "DynamicWind SkeletonContract 본 인덱스가 잘못됨"
        if (
            not name
            or index != expected_index
            or parent < -1
            or parent >= index
        ):
            return False, "DynamicWind SkeletonContract 본 계층이 잘못됨"
        digest.update(f"{index}\0{name}\0{parent}\n".encode("utf-8"))
    if str(contract.get("BoneNameIndexParentSha1") or "") != digest.hexdigest():
        return False, "DynamicWind SkeletonContract 본 해시가 일치하지 않음"
    import_root = contract.get("ImportRoot")
    first = bones[0]
    try:
        import_root_matches = (
            isinstance(import_root, dict)
            and str(import_root.get("BoneName") or "")
            == str(first.get("BoneName") or "")
            and int(import_root.get("BoneIndex", -1)) == 0
            and int(import_root.get("ParentIndex", -2)) == -1
        )
    except (TypeError, ValueError):
        import_root_matches = False
    if not import_root_matches:
        return False, "DynamicWind SkeletonContract ImportRoot가 잘못됨"
    return True, ""


def _same_content_identity(recorded, current):
    if not isinstance(recorded, dict) or not isinstance(current, dict):
        return False
    try:
        recorded_path = os.path.normcase(
            str(Path(recorded.get("canonical_path", "")).resolve())
        ).casefold()
        current_path = os.path.normcase(
            str(Path(current.get("canonical_path", "")).resolve())
        ).casefold()
    except (OSError, ValueError):
        return False
    return (
        recorded_path == current_path
        and int(recorded.get("size", -1)) == int(current.get("size", -2))
        and str(recorded.get("sha256") or "").casefold()
        == str(current.get("sha256") or "").casefold()
    )


def load_current_repair_pipeline_report(spm, *, migrate_legacy=True):
    """Load a content-current Repair report, upgrading proven legacy data.

    Legacy reports can be upgraded without Blender only when they already
    contain the exact live SPM content identity and were committed after the
    saved blend.  A missing identity, changed SPM, or blend newer than the
    report remains stale; rebuilding a contract from current inputs must never
    bless an unrelated old blend.
    """
    canonical_spm = speedtree_output_spm_for(spm)
    report_path = repair_pipeline_report_path(spm)
    try:
        report = _read_repair_pipeline_json(report_path)
    except (OSError, ValueError) as exc:
        raise ValueError(f"Repair report could not be read: {exc}") from exc
    if not isinstance(report, dict):
        raise ValueError("Repair report is not an object")

    bark_resolution = report.get("cluster_bark_source_resolution") or {}
    isolated_bark_input = bool(
        bark_resolution.get("status") == "ready"
        and _normalized_path(
            (bark_resolution.get("source_spm") or {}).get("path") or ""
        )
        != _normalized_path(
            (bark_resolution.get("speedtree_spm") or {}).get("path") or ""
        )
    )
    exact_handoff = report.get("speedtree_material_handoff_contract")
    if isinstance(exact_handoff, dict):
        envelope, material_source_spm = (
            _material_handoff_envelope_for_push(
                report,
                canonical_spm,
            )
        )
        validate_preflight_envelope(
            envelope,
            material_source_spm,
            require_ok=True,
        )
        return report
    if isolated_bark_input:
        raise ValueError(
            "Repair report used an isolated bark-normalized source but has "
            "no exact material handoff contract; run Blender Repair again"
        )

    envelope = report.get("speedtree_pipeline_contract")
    try:
        validate_preflight_envelope(envelope, canonical_spm, require_ok=True)
        return report
    except (OSError, ValueError, RuntimeError):
        if not migrate_legacy:
            raise

    handoff_status = str(
        (report.get("handoff_preflight") or {}).get("status") or ""
    )
    if handoff_status not in {"ok", "source_review"}:
        raise ValueError(
            "legacy Repair report has no completed handoff state"
        )
    recorded_identity = (
        (report.get("speedtree_live_source_identity") or {}).get("spm")
    )
    current_identity = source_identity(canonical_spm)
    if not _same_content_identity(recorded_identity, current_identity):
        raise ValueError(
            "legacy Repair report source identity is missing or stale"
        )
    blend = blend_path_for(spm)
    try:
        if (
            not blend.is_file()
            or report_path.stat().st_mtime_ns < blend.stat().st_mtime_ns
        ):
            raise ValueError(
                "legacy Repair report predates the saved blend"
            )
    except OSError as exc:
        raise ValueError(
            f"legacy Repair artifact timestamp could not be read: {exc}"
        ) from exc

    normalization = report.get("texture_normalization") or {}
    texture_readiness = {
        "status": (
            normalization.get("texture_contract_status")
            or normalization.get("status")
            or "legacy"
        )
    }
    migrated = dict(report)
    migrated_envelope = build_preflight_envelope(
        canonical_spm,
        outcome="ok",
        texture_readiness=texture_readiness,
    )
    migrated["speedtree_pipeline_contract"] = migrated_envelope
    migrated["speedtree_pipeline_contract_required"] = True
    migrated["report_contract_migration"] = {
        "kind": "legacy_content_identity_upgrade",
        "source_identity": current_identity,
    }
    atomic_write_json(report_path, migrated)
    _cache_written_repair_pipeline_json(report_path, migrated)
    return migrated


def normalized_folder_key(path):
    try:
        return os.path.normcase(str(Path(path).resolve())).casefold()
    except (OSError, ValueError):
        return os.path.normcase(str(Path(path).absolute())).casefold()


def cluster_relation_output_targets(cluster_spm, referenced_by_spms):
    """Return canonical targets where registry intent and live use agree.

    The Atlas registry is the explicit ON/OFF authority, while
    ``referenced_by_spms`` is the current content dependency discovered from
    export-participating SPM materials.  Either source alone is too broad:
    registry-only scheduling rebuilds unused providers, and reference-only
    scheduling revives relations that were explicitly switched OFF.
    """
    from atlas_target_registry import (
        TargetRegistryError,
        load_target_registry,
    )

    cluster_spm = Path(cluster_spm).expanduser().absolute()
    owner = cluster_spm.parent.parent
    owner_key = normalized_folder_key(owner)
    registry = load_target_registry(blend_path_for(cluster_spm))
    if registry is None:
        return []

    referenced_keys = set()
    for value in referenced_by_spms or ():
        reference = Path(value).expanduser().absolute()
        if (
            normalized_folder_key(reference.parent) != owner_key
            or reference.suffix.casefold() != ".spm"
        ):
            continue
        if not reference.name.casefold().startswith("sk_"):
            canonical = reference.with_name(f"SK_{reference.name}")
            if canonical.is_file():
                reference = canonical
        if reference.name.casefold().startswith("sk_"):
            referenced_keys.add(normalized_folder_key(reference))

    targets = []
    seen = set()
    for value in registry.get("target_spms") or ():
        target = Path(value).expanduser().absolute()
        if (
            normalized_folder_key(target.parent) == owner_key
            and target.suffix.casefold() == ".spm"
            and not target.name.casefold().startswith("sk_")
        ):
            canonical = target.with_name(f"SK_{target.name}")
            if canonical.is_file():
                target = canonical
        if (
            normalized_folder_key(target.parent) != owner_key
            or target.suffix.casefold() != ".spm"
            or not target.name.casefold().startswith("sk_")
        ):
            raise TargetRegistryError(
                "Atlas target registry contains a non-owner or non-canonical "
                f"SPM target for {cluster_spm.name}: {target}"
            )
        key = normalized_folder_key(target)
        if key in seen or key not in referenced_keys:
            continue
        seen.add(key)
        targets.append(target)
    return targets


def cluster_contract_dependency_for_spm(contract, spm):
    """Return the exact canonical provider dependency from one owner contract."""
    wanted = normalized_folder_key(spm)
    for dependency in (contract or {}).get("dependencies") or ():
        if not isinstance(dependency, dict):
            continue
        for field in ("spm", "source_spm", "authoring_spm", "output_spm"):
            value = dependency.get(field)
            if value and normalized_folder_key(value) == wanted:
                return dependency
    return None


def cluster_live_audit_target_block(contract, target_spm):
    """Find the one provider whose live delivery explicitly blocks a target.

    The normalization gate is handed its provider; the live-audit gate audits
    an owner SPM and has to discover it.  Target-local isolation is unchanged:
    only a provider that names this target in `delivery_blocked_targets`
    qualifies, so a shared provider issue still fails closed (#16).
    """
    for dependency in (contract or {}).get("dependencies") or ():
        if not isinstance(dependency, dict):
            continue
        for field in ("spm", "source_spm", "authoring_spm", "output_spm"):
            producer = dependency.get(field)
            if not producer:
                continue
            block = cluster_target_delivery_block(
                contract,
                target_spm,
                producer,
            )
            if block:
                return Path(str(producer)), block
    return None, None


def cluster_contract_issues(contract):
    """Return one stable issue list from the selected owner handoff."""
    handoff = (contract or {}).get("handoff") or {}
    return [
        issue
        for issue in (
            handoff.get("errors")
            or handoff.get("issues")
            or ()
        )
        if isinstance(issue, dict)
    ]


def cluster_target_delivery_block(contract, target_spm, producer_spm):
    """Return a target-local live-delivery exclusion, if one is explicit.

    A normalized provider is shared, but its live Generator connection is
    evaluated for each owner target.  Only ``delivery_blocked_targets`` binds a
    provider issue to one target; a provider-level issue without that binding
    remains shared and must fail closed.
    """
    dependency = cluster_contract_dependency_for_spm(contract, producer_spm)
    normalized = (
        dependency.get("normalized_variants")
        if isinstance(dependency, dict)
        else None
    )
    if not isinstance(normalized, dict):
        return None
    wanted = normalized_folder_key(target_spm)
    for row in normalized.get("delivery_blocked_targets") or ():
        if not isinstance(row, dict) or not row.get("spm"):
            continue
        if normalized_folder_key(row["spm"]) != wanted:
            continue
        reason_token = str(
            row.get("delivery_reason")
            or "normalized_generator_delivery_incomplete"
        ).strip() or "normalized_generator_delivery_incomplete"
        live_node_table = row.get("live_node_table")
        live_node_table = (
            live_node_table if isinstance(live_node_table, dict) else {}
        )
        # Persist counts and the affected Mesh IDs, but not raw Generator
        # GUIDs.  The operator needs enough evidence to identify and repair
        # the authored SPM without copying identity-bearing GUID values into
        # the shared queue/state files.
        live_node_table_summary = {
            key: copy.deepcopy(live_node_table.get(key))
            for key in (
                "stale",
                "generator_count",
                "node_table_generator_count",
                "orphan_node_count",
                "total_node_count",
            )
            if key in live_node_table
        }
        live_node_table_summary["orphan_generator_guid_count"] = len(
            live_node_table.get("orphan_generator_guids") or ()
        )
        return {
            "reason_token": reason_token,
            "target_spm": str(row["spm"]),
            "target_name": Path(str(row["spm"])).name,
            "delivery_mode": str(
                normalized.get("delivery_mode")
                or dependency.get("normalized_delivery_mode")
                or ""
            ),
            "delivery_errors": sorted({
                str(value)
                for value in (
                    row.get("errors")
                    or normalized.get("delivery_errors")
                    or ()
                )
                if value
            }),
            "delivery_remedy": str(
                row.get("delivery_remedy") or ""
            ).strip(),
            "stale_node_table_target_mesh_ids": [
                copy.deepcopy(value)
                for value in row.get("stale_node_table_target_mesh_ids") or ()
            ],
            "live_node_table": live_node_table_summary,
        }
    return None


def cluster_stale_node_table_recovery_scope(
    contract,
    target_spm,
    audit_report,
):
    """Seal authoritative authored and required-live scopes or fail closed.

    Only a producer-validated explicit delivery intent may authorize recovery.
    Live survivors, visibility, node counts, and diagnostic stale subsets are
    observations, never recovery intent.  Every required provider-role slice
    for the exact target must be present in the same content-bound live audit.
    """
    target = Path(target_spm).expanduser().resolve(strict=False)

    def unavailable(reason_token, **details):
        return {
            "schema_version": 2,
            "available": False,
            "mode": "owned_semantic_uia_modeler_save_watch",
            "scope_policy": "explicit_sealed_delivery_scopes_v1",
            "reason_token": str(reason_token),
            "target_spm": str(target),
            "audit_report": str(audit_report or ""),
            **copy.deepcopy(details),
        }

    def canonical_mesh_scope(values, label, *, allow_empty=False):
        if not isinstance(values, (list, tuple)):
            return None, f"{label}_missing"
        if any(
            isinstance(value, bool)
            or not isinstance(value, int)
            or value <= 0
            for value in values
        ):
            return None, f"{label}_invalid"
        canonical = sorted(set(values))
        if list(values) != canonical:
            return None, f"{label}_not_canonical"
        if not canonical and not allow_empty:
            return None, f"{label}_missing"
        return canonical, None

    if not isinstance(contract, dict):
        return unavailable("recovery_contract_missing")

    identity_matches = []
    for row in contract.get("tree_source_identities") or ():
        if not isinstance(row, dict):
            continue
        identity = row.get("target_spm")
        if not isinstance(identity, dict) or not identity.get("path"):
            continue
        if normalized_folder_key(identity["path"]) == normalized_folder_key(
            target
        ):
            identity_matches.append(identity)
    if len(identity_matches) != 1:
        return unavailable(
            "target_audit_identity_missing_or_ambiguous",
            identity_match_count=len(identity_matches),
        )
    target_sha256 = str(
        identity_matches[0].get("sha256") or ""
    ).strip().casefold()
    if len(target_sha256) != 64 or any(
        value not in "0123456789abcdef" for value in target_sha256
    ):
        return unavailable("target_audit_sha256_missing_or_invalid")

    dependencies = contract.get("dependencies") or ()
    if not isinstance(dependencies, (list, tuple)) or not dependencies:
        return unavailable("target_recovery_dependencies_missing")

    strict_policy = "ensure_all_material_cutouts"
    stale_reason = "live_export_evidence_unavailable_stale_node_table"
    stale_consequences = {
        "generator_export_evidence_stale_node_table",
        "normalized_and_live_target_mesh_sets_differ",
    }
    authoring_mesh_ids = set()
    required_live_mesh_ids = set()
    provider_slices = []
    required_dependency_count = 0
    stale_slice_count = 0

    for dependency in dependencies:
        if not isinstance(dependency, dict):
            return unavailable("target_recovery_dependency_invalid")
        if dependency.get("normalized_variants_required") is not True:
            continue
        required_dependency_count += 1
        variants = dependency.get("normalized_variants")
        if not isinstance(variants, dict):
            return unavailable(
                "normalized_delivery_evidence_missing",
                provider_role=str(dependency.get("role") or ""),
            )
        if str(variants.get("status") or "") not in {"ready", "current"}:
            return unavailable(
                "normalized_delivery_evidence_not_current",
                provider_role=str(dependency.get("role") or ""),
            )
        target_rows = [
            row
            for row in variants.get("target_deliveries") or ()
            if isinstance(row, dict)
            and row.get("spm")
            and normalized_folder_key(row["spm"])
            == normalized_folder_key(target)
        ]
        if len(target_rows) != 1:
            return unavailable(
                "target_delivery_missing_or_ambiguous",
                provider_role=str(dependency.get("role") or ""),
                target_delivery_match_count=len(target_rows),
            )
        delivery = target_rows[0]
        if delivery.get("generator_variant_policy") != strict_policy:
            return unavailable(
                "target_delivery_variant_policy_not_supported",
                provider_role=str(dependency.get("role") or ""),
            )
        if delivery.get("delivery_scope_mode") != "explicit_sealed_v1":
            return unavailable(
                "target_delivery_scope_not_explicit",
                provider_role=str(dependency.get("role") or ""),
            )
        recovery_target_scope = delivery.get("recovery_target_scope")
        if not (
            isinstance(recovery_target_scope, dict)
            and recovery_target_scope.get("contract")
            == "speedtree_stale_node_recovery_target_scope"
            and recovery_target_scope.get("schema_version") == 1
            and recovery_target_scope.get("policy")
            == "explicit_sealed_scopes_v1"
        ):
            return unavailable(
                "authoritative_recovery_target_scope_missing",
                provider_role=str(dependency.get("role") or ""),
            )
        supplied_scope_sha256 = str(
            recovery_target_scope.get("scope_sha256") or ""
        ).strip().casefold()
        scope_projection = {
            key: value
            for key, value in recovery_target_scope.items()
            if key != "scope_sha256"
        }
        expected_scope_sha256 = hashlib.sha256(
            json.dumps(
                scope_projection,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        if supplied_scope_sha256 != expected_scope_sha256:
            return unavailable(
                "authoritative_recovery_target_scope_hash_mismatch",
                provider_role=str(dependency.get("role") or ""),
            )
        intent_sha256 = str(
            recovery_target_scope.get("delivery_scope_intent_sha256") or ""
        ).strip().casefold()
        if len(intent_sha256) != 64 or any(
            value not in "0123456789abcdef" for value in intent_sha256
        ):
            return unavailable(
                "target_delivery_scope_intent_sha256_invalid",
                provider_role=str(dependency.get("role") or ""),
            )
        if intent_sha256 != str(
            delivery.get("delivery_scope_intent_sha256") or ""
        ).strip().casefold():
            return unavailable(
                "target_delivery_scope_intent_echo_mismatch",
                provider_role=str(dependency.get("role") or ""),
            )
        authoring_ids, scope_error = canonical_mesh_scope(
            recovery_target_scope.get("authoring_mesh_ids"),
            "authoring_mesh_ids",
        )
        if scope_error:
            return unavailable(
                scope_error,
                provider_role=str(dependency.get("role") or ""),
            )
        normalized_ids, scope_error = canonical_mesh_scope(
            delivery.get("normalized_target_mesh_ids"),
            "normalized_authoring_mesh_ids",
        )
        if scope_error:
            return unavailable(
                scope_error,
                provider_role=str(dependency.get("role") or ""),
            )
        declared_ids, scope_error = canonical_mesh_scope(
            delivery.get("declared_target_mesh_ids"),
            "declared_authoring_mesh_ids",
        )
        if scope_error:
            return unavailable(
                scope_error,
                provider_role=str(dependency.get("role") or ""),
            )
        if not (normalized_ids == declared_ids == authoring_ids):
            return unavailable(
                "authoring_scope_not_exact_declared_scope",
                provider_role=str(dependency.get("role") or ""),
            )
        required_live_ids, scope_error = canonical_mesh_scope(
            recovery_target_scope.get("required_live_mesh_ids"),
            "required_live_mesh_ids",
            allow_empty=True,
        )
        if scope_error:
            return unavailable(
                scope_error,
                provider_role=str(dependency.get("role") or ""),
            )
        if not set(required_live_ids).issubset(authoring_ids):
            return unavailable(
                "required_live_scope_not_authoring_subset",
                provider_role=str(dependency.get("role") or ""),
            )
        current_required_ids, scope_error = canonical_mesh_scope(
            delivery.get("current_required_target_mesh_ids"),
            "current_required_live_mesh_ids",
            allow_empty=True,
        )
        if scope_error:
            return unavailable(
                scope_error,
                provider_role=str(dependency.get("role") or ""),
            )
        if current_required_ids != required_live_ids:
            return unavailable(
                "required_live_scope_not_exact_delivery_scope",
                provider_role=str(dependency.get("role") or ""),
            )

        count_fields = {
            name: delivery.get(name)
            for name in (
                "declared_binding_count",
                "active_required_binding_count",
                "planned_inactive_binding_count",
                "delivery_scope_required_live_slot_count",
                "delivery_scope_continuity_only_slot_count",
            )
        }
        if any(
            type(value) is not int or value < 0
            for value in count_fields.values()
        ):
            return unavailable(
                "target_delivery_scope_counts_invalid",
                provider_role=str(dependency.get("role") or ""),
            )
        if not (
            count_fields["declared_binding_count"] > 0
            and count_fields["planned_inactive_binding_count"] == 0
            and count_fields["active_required_binding_count"]
            == count_fields["delivery_scope_required_live_slot_count"]
            and count_fields["declared_binding_count"]
            == count_fields["delivery_scope_required_live_slot_count"]
            + count_fields["delivery_scope_continuity_only_slot_count"]
            and bool(required_live_ids)
            == bool(count_fields["delivery_scope_required_live_slot_count"])
        ):
            return unavailable(
                "target_delivery_scope_counts_inconsistent",
                provider_role=str(dependency.get("role") or ""),
            )
        if authoring_mesh_ids.intersection(authoring_ids):
            return unavailable(
                "authoring_mesh_scope_overlaps_provider_roles",
                provider_role=str(dependency.get("role") or ""),
            )

        errors = {
            str(value)
            for value in delivery.get("errors") or ()
            if str(value)
        }
        decision = str(delivery.get("delivery_decision") or "")
        if decision == "blocked":
            stale_ids = delivery.get("stale_node_table_target_mesh_ids")
            if not isinstance(stale_ids, (list, tuple)) or not stale_ids:
                return unavailable(
                    "stale_target_mesh_scope_missing",
                    provider_role=str(dependency.get("role") or ""),
                )
            canonical_stale_ids = sorted(set(stale_ids))
            live_node_table = delivery.get("live_node_table")
            orphan_owner_count = len(
                (live_node_table or {}).get("orphan_generator_guids") or ()
            ) if isinstance(live_node_table, dict) else 0
            orphan_node_count = int(
                (live_node_table or {}).get("orphan_node_count") or 0
            ) if isinstance(live_node_table, dict) else 0
            if (
                list(stale_ids) != canonical_stale_ids
                or any(
                    isinstance(value, bool)
                    or not isinstance(value, int)
                    or value <= 0
                    for value in stale_ids
                )
                or not set(canonical_stale_ids).issubset(required_live_ids)
                or delivery.get("delivery_reason") != stale_reason
                or not errors
                or not errors.issubset(stale_consequences)
                or not isinstance(delivery.get("live_node_table"), dict)
                or delivery["live_node_table"].get("stale") is not True
                or orphan_owner_count <= 0
                or orphan_node_count <= 0
            ):
                return unavailable(
                    "target_delivery_not_stale_only",
                    provider_role=str(dependency.get("role") or ""),
                )
            stale_slice_count += 1
        elif not (
            (
                decision == "normalize_part"
                and not errors
                and delivery.get("live_generator_delivery_complete") is True
            )
            or (
                decision == "pass_through"
                and not required_live_ids
                and not errors
                and delivery.get("delivery_reason")
                == "relationship_continuity_only"
            )
        ):
            return unavailable(
                "target_delivery_has_independent_blocker",
                provider_role=str(dependency.get("role") or ""),
            )

        authoring_mesh_ids.update(authoring_ids)
        required_live_mesh_ids.update(required_live_ids)
        provider_slices.append({
            "role": str(dependency.get("role") or ""),
            "provider_spm": str(dependency.get("spm") or ""),
            "delivery_scope_intent_sha256": intent_sha256,
            "authoring_mesh_ids": authoring_ids,
            "required_live_mesh_ids": required_live_ids,
            "delivery_decision": decision,
            "orphan_generator_guid_count": (
                orphan_owner_count if decision == "blocked" else 0
            ),
            "orphan_node_count": (
                orphan_node_count if decision == "blocked" else 0
            ),
        })

    if required_dependency_count == 0 or not authoring_mesh_ids:
        return unavailable("target_recovery_scope_empty")
    if stale_slice_count == 0:
        return unavailable("target_has_no_stale_blocking_delivery")

    sealed = {
        "schema_version": 2,
        "available": True,
        "mode": "owned_semantic_uia_modeler_save_watch",
        "scope_policy": "explicit_sealed_delivery_scopes_v1",
        "target_spm": str(target),
        "target_preimage_raw_sha256": target_sha256,
        "authoring_mesh_ids": sorted(authoring_mesh_ids),
        "required_live_mesh_ids": sorted(required_live_mesh_ids),
        "provider_slices": sorted(
            provider_slices,
            key=lambda row: (row["role"], row["provider_spm"]),
        ),
        "audit_report": str(audit_report or ""),
    }
    sealed["scope_sha256"] = hashlib.sha256(
        (
            json.dumps(
                sealed,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            )
            + "\n"
        ).encode("utf-8")
    ).hexdigest()
    return sealed


def target_planned_exclusion_summary(target_spm, reason_token, evidence):
    """Render one Korean cause/action summary for a narrow GUI row."""
    target = Path(target_spm)
    evidence = evidence if isinstance(evidence, dict) else {}
    decision = repair_ui_decision({
        "reason_token": reason_token,
        "evidence": evidence,
    })
    parts = [target.name, f"원인: {decision['reason']}"]
    live_node_table = evidence.get("live_node_table")
    live_node_table = (
        live_node_table if isinstance(live_node_table, dict) else {}
    )
    if live_node_table.get("stale") is True:
        parts.append("Node table 오래됨")
        parts.append(
            "고아 Generator GUID "
            + str(live_node_table.get("orphan_generator_guid_count") or 0)
            + "개"
        )
        orphan_nodes = live_node_table.get("orphan_node_count")
        total_nodes = live_node_table.get("total_node_count")
        if orphan_nodes is not None or total_nodes is not None:
            parts.append(f"고아 Node {orphan_nodes or 0}/{total_nodes or 0}")
        mesh_ids = evidence.get("stale_node_table_target_mesh_ids") or ()
        if mesh_ids:
            parts.append(
                "대상 Mesh ID " + ",".join(str(value) for value in mesh_ids)
            )
    parts.append(f"조치: {decision['action']}")
    return " | ".join(parts)


def cluster_relation_refresh_state(cluster_spm, target_spms):
    """Return whether the provider's committed Atlas delivery is current."""
    from atlas_target_registry import (
        TargetRegistryError,
        load_target_registry,
    )
    from cluster_blend_sync import (
        inspect_cluster_target,
        normalized_path_key,
    )

    canonical = Path(cluster_spm).expanduser().absolute()
    blend = blend_path_for(canonical)
    try:
        registry = load_target_registry(blend)
    except TargetRegistryError as exc:
        return {
            "current": False,
            "reason": f"target_registry_invalid: {exc}",
            "targets": [],
        }
    registered = {
        normalized_path_key(value)
        for value in (registry or {}).get("target_spms") or ()
    }
    rows = []
    for target_spm in target_spms:
        target = Path(target_spm).expanduser().absolute()
        relation_on = normalized_path_key(target) in registered
        state = inspect_cluster_target(
            blend,
            target,
            relation_on,
            canonical_spm=canonical,
        )
        rows.append({"target_spm": str(target), **state})
    stale = [
        row for row in rows
        if row.get("status") != "synced"
    ]
    return {
        "current": not stale,
        "reason": "; ".join(
            f"{Path(row['target_spm']).name}:{row.get('status')}"
            + (
                f"({','.join(row.get('refresh_reasons') or ())})"
                if row.get("refresh_reasons")
                else ""
            )
            for row in stale
        ),
        "targets": rows,
    }


def cluster_relation_owner_lock(spm):
    owner_key = normalized_folder_key(Path(spm).parent.parent)
    with CLUSTER_RELATION_LOCKS_GUARD:
        return CLUSTER_RELATION_LOCKS.setdefault(
            owner_key, threading.Lock()
        )


def send2ue_rpc_cli_args(unreal_project):
    """Build background-Blender RPC overrides from the target UE project."""
    settings = unreal_remote_execution_settings(unreal_project)
    args = []
    bind_address = settings.get("multicast_bind_address")
    if bind_address:
        args.extend(["--rpc-multicast-bind-address", str(bind_address)])
    group_endpoint = settings.get("multicast_group_endpoint")
    if group_endpoint:
        args.extend(["--rpc-multicast-group-endpoint", str(group_endpoint)])
    if "multicast_ttl" in settings:
        args.extend(["--rpc-multicast-ttl", str(settings["multicast_ttl"])])
    return args


def blender_open_file_window_titles(blend_path):
    """Return interactive Blender windows that currently hold this blend."""
    if os.name != "nt":
        return []
    try:
        expected = str(Path(blend_path).resolve()).casefold()
    except (OSError, ValueError):
        return []
    titles = []
    try:
        user32 = ctypes.windll.user32
        current_pid = os.getpid()
        callback_type = ctypes.WINFUNCTYPE(
            ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p
        )

        @callback_type
        def collect(window, _extra):
            # GetWindowTextLengthW sends WM_GETTEXTLENGTH when the window
            # belongs to this process.  The batch main thread waits for these
            # workers, so querying its hidden Tk window here deadlocks both
            # sides.  Only external Blender windows can satisfy this guard;
            # reject our own windows before asking Windows for their titles.
            window_pid = ctypes.c_ulong()
            user32.GetWindowThreadProcessId(
                window, ctypes.byref(window_pid)
            )
            if window_pid.value == current_pid:
                return True
            length = user32.GetWindowTextLengthW(window)
            if length <= 0:
                return True
            buffer = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(window, buffer, len(buffer))
            title = buffer.value
            folded = title.casefold()
            if "blender" in folded and expected in folded:
                titles.append(title)
            return True

        user32.EnumWindows(collect, 0)
    except (AttributeError, OSError):
        return []
    return titles


def blender_repair_schedule_waves(targets):
    """Run authored Cluster sources before root assets that fingerprint them."""
    cluster_sources = []
    downstream = []
    for item in targets:
        target = (
            item.get("spm")
            if isinstance(item, dict)
            else getattr(item, "spm", None)
        )
        if is_cluster_source_spm(target):
            cluster_sources.append(item)
        else:
            downstream.append(item)
    return [
        wave for wave in (cluster_sources, downstream) if wave
    ]


def expand_blender_repair_targets(selected_targets, all_items):
    """Add only Cluster rows selected by both registry intent and live use.

    The Atlas target registry supplies the explicit ON/OFF intent, and current
    SPM material references supply the actual dependency. A provider must
    satisfy both sides of that contract; registry-only, reference-only, and
    relation-OFF providers cannot block a standalone owner Repair or export.
    """
    selected_targets = list(selected_targets)
    values = all_items.values() if isinstance(all_items, dict) else all_items
    inventory = [
        item for item in values
        if isinstance(item, dict) and item.get("spm")
    ]
    selected_downstream_by_path = {}
    dependencies_by_root = {}
    for item in selected_targets:
        spm = item.get("spm")
        if not spm or is_cluster_source_spm(spm):
            continue
        iid = str(spm)
        dependencies_by_root.setdefault(iid, [])
        for field in ("spm", "authoring_spm", "output_spm"):
            value = item.get(field)
            if value:
                selected_downstream_by_path[
                    normalized_folder_key(value)
                ] = iid

    dependency_items = []
    auto_added_ids = set()
    selected_ids = {str(item["spm"]) for item in selected_targets}
    for item in inventory:
        cluster_spm = item["spm"]
        if not is_cluster_source_spm(cluster_spm):
            continue
        matched_roots = set()
        for reference in cluster_relation_output_targets(
            cluster_spm,
            item.get("referenced_by_spms") or (),
        ):
            if not reference:
                continue
            matched = selected_downstream_by_path.get(
                normalized_folder_key(reference)
            )
            reference_path = Path(reference).expanduser().absolute()
            if (
                matched is None
                and reference_path.suffix.casefold() == ".spm"
                and not reference_path.name.casefold().startswith("sk_")
            ):
                canonical = reference_path.with_name(
                    f"SK_{reference_path.name}"
                )
                matched = selected_downstream_by_path.get(
                    normalized_folder_key(canonical)
                )
            if matched is not None:
                matched_roots.add(matched)
        matched_roots.discard(None)
        if not matched_roots:
            continue
        dependency_iid = str(cluster_spm)
        dependency_items.append(item)
        if dependency_iid not in selected_ids:
            auto_added_ids.add(dependency_iid)
        for root_iid in matched_roots:
            dependencies_by_root.setdefault(root_iid, []).append(
                dependency_iid
            )

    ordered = []
    seen = set()
    for item in dependency_items + selected_targets:
        iid = str(item["spm"])
        if iid in seen:
            continue
        seen.add(iid)
        ordered.append(item)
    return (
        ordered,
        {
            root: tuple(dict.fromkeys(dependencies))
            for root, dependencies in dependencies_by_root.items()
        },
        auto_added_ids,
    )


def should_calibrate_spm(item):
    """Return whether stage ① may normalize this row's SPM bone contract.

    Only Cluster providers require the dedicated first-renderable-root
    Absolute/1 contract. Owner Tree rows pass directly to later stages instead
    of spending batch time in the whole-tree density solver.
    """
    spm = item.get("spm")
    return (
        not item.get("source_read_only")
        and spm is not None
        and is_cluster_source_spm(spm)
    )


def sk_batch_folder_chain(root, spm):
    """Return the owner/Cluster hierarchy used by the SK Batch table."""
    root = Path(root)
    spm = Path(spm)
    if is_cluster_source_spm(spm):
        return [spm.parent.parent, spm.parent]
    return [spm.parent] if spm.parent != root else [root]


def compact_table_status(value, max_chars=28):
    """Keep the overview table compact; the full value stays in row details."""
    text = " ".join(str(value or "-").split())
    if len(text) <= max_chars:
        return text
    return f"{text[:max_chars - 1].rstrip()}…"


def _normalized_path(path):
    return os.path.normcase(os.path.abspath(str(path)))


def _sha256_snapshot(path):
    """Read a stable SHA-256 snapshot without changing the source file."""
    candidate = Path(path)
    for _attempt in range(2):
        before = candidate.stat()
        digest = hashlib.sha256()
        with candidate.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        after = candidate.stat()
        if (before.st_size, before.st_mtime_ns) == (
            after.st_size,
            after.st_mtime_ns,
        ):
            return {
                "sha256": digest.hexdigest(),
                "size": after.st_size,
            }
    raise OSError(f"File changed while reading: {candidate}")


def current_speedtree_bone_measurement(spm):
    """Return a verified exported bone count for the current SPM, or None.

    The XML is accepted only when its artifact hash matches the export receipt.
    The receipt's SPM input hash marks the count as current or last-measured.
    This is deliberately read-only and content-based: a timestamp-only change
    cannot schedule or invalidate work.
    """
    spm = Path(spm)
    xml_path = spm.parent / "xml" / f"{spm.stem}.xml"
    receipt_path = (
        xml_path.parent / ".speedtree_export_cache" / f"{xml_path.name}.json"
    )
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        source = receipt["inputs"]["spm"]
        if _normalized_path(source["path"]) != _normalized_path(spm):
            return None
        source_snapshot = _sha256_snapshot(spm)
        source_current = not (
            source_snapshot["size"] != int(source["size"])
            or source_snapshot["sha256"].lower()
            != str(source["sha256"]).lower()
        )

        artifact = next(
            item
            for item in receipt["artifacts"]
            if item.get("relative_path") == xml_path.name
        )
        xml_snapshot = _sha256_snapshot(xml_path)
        if (
            xml_snapshot["size"] != int(artifact["size"])
            or xml_snapshot["sha256"].lower()
            != str(artifact["sha256"]).lower()
        ):
            return None

        root = ET.parse(xml_path).getroot()
        xml_source = root.get("Source")
        if xml_source and _normalized_path(xml_source) != _normalized_path(spm):
            return None
        bones = root.find(".//Bones")
        if bones is None:
            return None
        actual_count = len(bones.findall("Bone"))
        declared_count = int(bones.get("Count", actual_count))
        if declared_count != actual_count:
            return None
        return {
            "count": actual_count,
            "current": source_current,
            "xml_path": xml_path,
            "receipt_path": receipt_path,
        }
    except (ET.ParseError, OSError, TypeError, ValueError, KeyError, StopIteration):
        return None


def manual_bone_status_text(spm):
    measurement = current_speedtree_bone_measurement(spm)
    if measurement is None:
        return "수동 본 유지 🔒 · 본 수 미측정 (검증된 SpeedTree XML 없음)"
    if not measurement["current"]:
        return (
            f"수동 본 유지 🔒 · 최근 실측 본 {measurement['count']}개 "
            "(SPM 변경 후 미재검증)"
        )
    return (
        f"수동 본 유지 🔒 · SpeedTree 본 {measurement['count']}개 "
        "(현재 SPM과 일치하는 XML)"
    )


def selected_row_detail_text(spm, statuses):
    return "\n".join(
        (
            f"경로  {spm}",
            f"① SPM  {statuses.get('spm_status', '-')}",
            f"② Blender  {statuses.get('blend_status', '-')}",
            f"③ Unreal  {statuses.get('push_status', '-')}",
        )
    )


def spm_check_status_parts(audit):
    """Return human-readable SPM settings without implying a failure."""
    generators = audit.get("generators") or []
    fixed = sum(
        1
        for generator in generators
        if generator.get("style") == 0.0
        and (generator.get("bones") or 0) > 0
    )
    relative = sum(
        1 for generator in generators if generator.get("style") == 1.0
    )
    disabled = sum(
        1
        for generator in generators
        if generator.get("style") == 0.0
        and (generator.get("bones") or 0) == 0
    )
    material_renames = sum(
        1 for material in (audit.get("materials") or [])
        if material.get("needs_prefix")
    )
    parts = []
    if fixed:
        parts.append(f"고정 본(Absolute) {fixed}개")
    if relative:
        parts.append(f"자동 본(Relative) {relative}개")
    if disabled:
        parts.append(f"본 꺼짐 {disabled}개")
    if material_renames:
        parts.append(f"M_ 필요 {material_renames}개")
    graph = audit.get("bone_graph") or {}
    if graph.get("root_target_generator_count"):
        parts.append(
            f"자동 대상 {graph['root_target_generator_count']} / "
            f"Base 제외 {graph['base_excluded_generator_count']}"
        )
    if graph.get("unknown_base_generators"):
        parts.append(f"Base 미분류 {len(graph['unknown_base_generators'])}")
    return parts


def cluster_issue_summary(issues, limit=5):
    """Render Cluster audit issues so the cause and the fix are both visible.

    A bare ``CODE role=cluster`` line told an operator that something was wrong
    but not what to do about it, and for a stale node table it named the wrong
    subject entirely.  Any ``remedy`` the contract published is carried through.
    """
    lines = []
    for issue in list(issues or ())[:limit]:
        if not isinstance(issue, dict):
            lines.append(str(issue))
            continue
        fields = [str(issue.get("code") or "CLUSTER_DATA_INVALID")]
        # Put the authored target before provider-role/error metadata so the
        # target remains visible even when a narrow board cell truncates the
        # rest of the diagnostic.
        blocked_target_rows = [
            row
            for row in issue.get("blocked_targets") or ()
            if isinstance(row, dict) and row.get("spm")
        ]
        targets = [
            Path(str(row.get("spm"))).name
            for row in blocked_target_rows
        ]
        if targets:
            fields.append("targets=" + ", ".join(targets[:3]))
        role = str(issue.get("role") or "")
        if role:
            fields.append(f"role={role}")
        details = issue.get("details") or {}
        status = str(details.get("status") or "")
        if status:
            fields.append(f"status={status}")
        missing = [str(value) for value in details.get("missing") or []]
        if missing:
            fields.append("missing=" + ", ".join(missing[:3]))
        errors = [
            str(value).split(":", 1)[0]
            for value in issue.get("errors") or []
            if str(value).strip()
        ]
        if errors:
            fields.append("errors=" + ", ".join(errors[:3]))
        stale_target_evidence = []
        for row in blocked_target_rows[:3]:
            live_node_table = row.get("live_node_table")
            if not isinstance(live_node_table, dict):
                continue
            if live_node_table.get("stale") is not True:
                continue
            evidence = {
                "live_node_table": {
                    "stale": True,
                    "orphan_generator_guid_count": len(
                        live_node_table.get("orphan_generator_guids") or ()
                    ),
                    "orphan_node_count": live_node_table.get(
                        "orphan_node_count"
                    ),
                    "total_node_count": live_node_table.get(
                        "total_node_count"
                    ),
                },
                "stale_node_table_target_mesh_ids": row.get(
                    "stale_node_table_target_mesh_ids"
                ) or (),
            }
            stale_target_evidence.append(
                target_planned_exclusion_summary(
                    row["spm"],
                    row.get("delivery_reason")
                    or "live_export_evidence_unavailable_stale_node_table",
                    evidence,
                )
            )
        if stale_target_evidence:
            fields.append("target_evidence=" + "; ".join(stale_target_evidence))
        # A block can be partly explained by a stale node table even when
        # an independent fault keeps the overall code generic. Surface that
        # subset and its fix instead of only the file that failed.
        stale_targets = [
            Path(str(row.get("spm"))).name
            for row in issue.get("stale_node_table_targets") or ()
            if isinstance(row, dict) and row.get("spm")
        ]
        if stale_targets:
            fields.append(
                "stale_node_table=" + ", ".join(stale_targets[:3])
            )
        remedy = str(issue.get("remedy") or "").strip()
        if remedy:
            fields.append("→ " + remedy)
        stale_remedy = str(issue.get("stale_node_table_remedy") or "").strip()
        if stale_remedy:
            fields.append("→ " + stale_remedy)
        lines.append(" ".join(fields))
    return " | ".join(lines)


class BatchItemError(RuntimeError):
    """One item failed, with a machine-readable queue-impact classification."""

    def __init__(
        self, reason, kind="data_error", report=None, log_file=None, report_file=None
    ):
        super().__init__(reason)
        self.kind = kind
        self.report = report or {}
        self.log_file = log_file
        self.report_file = report_file


class InlineAtlasRepairRequested(BatchItemError):
    """Internal control flow for a read-only child audit's exact repair."""

    def __init__(self, target_spm, report, *, log_file=None, report_file=None):
        self.target_spm = Path(target_spm)
        super().__init__(
            "Atlas manifest exact repair requested",
            kind="automatic_repair_requested",
            report=report,
            log_file=log_file,
            report_file=report_file,
        )


class TargetPlannedExclusionError(BatchItemError):
    """One target is blocked without making its shared provider fail."""

    def __init__(
        self,
        reason,
        *,
        reason_token,
        target_spm,
        producer_spm,
        evidence=None,
        log_file=None,
        report_file=None,
    ):
        report = {
            "status": "planned_excluded",
            "scope": "target",
            "reason_token": str(reason_token),
            "target_spm": str(target_spm),
            "producer_spm": str(producer_spm),
            "evidence": copy.deepcopy(evidence or {}),
        }
        super().__init__(
            reason,
            kind="planned_excluded",
            report=report,
            log_file=log_file,
            report_file=report_file,
        )
        self.reason_token = str(reason_token)
        self.target_spm = Path(target_spm)
        self.producer_spm = Path(producer_spm)
        self.evidence = copy.deepcopy(evidence or {})


class Tooltip:
    """말풍선 도움말: 위젯에 마우스를 올리면 설명이 뜬다."""

    def __init__(self, widget, text, wrap=380):
        self.widget = widget
        self.text = text
        self.wrap = wrap
        self.tip = None
        widget.bind("<Enter>", self._show, add="+")
        widget.bind("<Leave>", self._hide, add="+")

    def _show(self, _event=None):
        if self.tip:
            return
        x = self.widget.winfo_rootx() + 12
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6
        self.tip = tk.Toplevel(self.widget)
        self.tip.wm_overrideredirect(True)
        self.tip.wm_geometry(f"+{x}+{y}")
        tk.Label(
            self.tip, text=self.text, justify="left", wraplength=self.wrap,
            background="#ffffe0", relief="solid", borderwidth=1, padx=6, pady=4,
        ).pack()

    def _hide(self, _event=None):
        if self.tip:
            self.tip.destroy()
            self.tip = None


class App:
    def __init__(self, root):
        self.root = root
        self.cfg = load_config()
        self.state = load_state()
        self.items = {}  # iid -> {"spm": Path, "checked": bool, "wind_override": str}
        self.folder_rows = {}
        self.row_copy_paths = {}
        self._table_full_values = {}
        self.checked_rows = CheckedRowController(self.items, self._redraw_checked_row)
        self.ui_queue = queue.Queue()
        self.worker = None
        self.pending_batch_jobs = deque()
        self.active_batch_job = None
        self.batch_job_sequence = 0
        self.batch_job_failures = []
        self.shared_queue_runtime = SharedQueueRuntime("sk_batch")
        self._active_retry_progress = None
        self._retry_progress_by_run_id = {}
        self._async_retry_planning_enabled = True
        self._retry_planning_workers = set()
        self._ui_thread_ident = threading.get_ident()
        self._retry_thread_context = threading.local()
        self.cell_editor = None
        self.stop_flag = threading.Event()
        self._app_open = True
        self._recovery_commit_lock = threading.RLock()
        self._recovery_resume_commit = None
        self._stale_node_table_modeler_session = None
        self.active_procs = set()          # all running child procs (serial or parallel)
        self.procs_lock = threading.Lock()
        self.state_lock = threading.RLock()  # guards self.state writes across worker threads
        self._reset_cluster_receipt_refresh_memo()
        self._scan_generation = 0
        self.scan_worker = None
        self._live_poll_active = False
        self._live_poll_after_id = None
        self.spm_calibration_signature = calibration_settings_signature(self.cfg)
        self.legacy_spm_calibration_signature = (
            legacy_calibration_settings_signature(self.cfg)
        )

        root.title("SK Vegetation Batch — 검사 → 본 세팅 → Blender → Unreal")
        root.geometry("1460x840")
        self._build_ui()
        self._restore_latest_retry_progress()
        self.root.after(100, self._drain_ui_queue)
        self.root.after(1000, self._refresh_retry_liveness)
        self.scan()

    # ------------------------------------------------------------------ UI
    def _build_ui(self):
        top = ttk.Frame(self.root, padding=6)
        top.pack(fill="x")
        ttk.Label(top, text="루트:").pack(side="left")
        self.root_var = tk.StringVar(value=self.cfg["root"])
        self.root_entry = ttk.Entry(
            top, textvariable=self.root_var, width=66
        )
        self.root_entry.pack(side="left", padx=4)
        self.btn_pick_root = ttk.Button(top, text="...", width=3, command=self._pick_root)
        self.btn_pick_root.pack(side="left")
        self.btn_scan = ttk.Button(top, text="스캔", command=self.scan)
        self.btn_scan.pack(side="left", padx=6)
        self.btn_select_all = ttk.Button(top, text="전체 선택", command=lambda: self._set_all(True))
        self.btn_select_all.pack(side="left")
        self.btn_clear_all = ttk.Button(top, text="전체 해제", command=lambda: self._set_all(False))
        self.btn_clear_all.pack(side="left", padx=4)
        self.btn_recent_24h = ttk.Button(
            top,
            text="최근 24시간",
            command=self._check_recent_24h,
        )
        self.btn_recent_24h.pack(side="left")
        Tooltip(
            self.btn_recent_24h,
            (
                "현재 시각 기준으로 권위 SPM 파일이 최근 24시간 안에 수정된 "
                "행만 체크합니다.\n"
                "체크 상태만 바꾸며 작업은 실행하지 않습니다."
            ),
        )
        ttk.Button(
            top, text="선택 SPM 경로 복사", command=self.copy_selected_paths
        ).pack(side="left", padx=(4, 0))

        opts = ttk.LabelFrame(self.root, text="옵션 (각 항목에 마우스를 올리면 설명이 뜹니다)", padding=6)
        opts.pack(fill="x", padx=6)

        transport_opts = ttk.LabelFrame(
            self.root,
            text="Unreal Push transport",
            padding=6,
        )
        transport_opts.pack(fill="x", padx=6, pady=(4, 0))
        ttk.Label(transport_opts, text="③ Unreal Push:").pack(side="left")
        self.transport_var = tk.StringVar(
            value=self.cfg.get("push_transport", "rpc")
        )
        transport_combo = ttk.Combobox(
            transport_opts,
            textvariable=self.transport_var,
            values=("rpc", "headless"),
            width=12,
            state="readonly",
        )
        transport_combo.pack(side="left", padx=6)
        Tooltip(
            transport_combo,
            "rpc = 기존 열린 Unreal Editor 경로\n"
            "headless = Blender 전체 export 후 UnrealEditor-Cmd 1회 배치 import",
        )
        self.night_headless_var = tk.BooleanVar(
            value=bool(self.cfg.get("night_headless", True))
        )
        night_check = ttk.Checkbutton(
            transport_opts,
            text="목록 전체 자동(야간)은 headless",
            variable=self.night_headless_var,
        )
        night_check.pack(side="left", padx=(12, 0))
        Tooltip(
            night_check,
            "켜면 목록 전체 자동의 마지막 Push는 열린 Unreal 없이 headless로 실행합니다. "
            "단독 ③ Push는 왼쪽 transport 선택을 따릅니다.",
        )
        ttk.Label(transport_opts, text="② Repair·③ export 동시:").pack(
            side="left", padx=(18, 0)
        )
        self.blender_parallel_var = tk.IntVar(
            value=int(self.cfg.get("blender_parallel_jobs", 2))
        )
        blender_parallel_spin = ttk.Spinbox(
            transport_opts,
            from_=1,
            to=4,
            textvariable=self.blender_parallel_var,
            width=4,
        )
        blender_parallel_spin.pack(side="left", padx=4)
        Tooltip(
            blender_parallel_spin,
            "Blender Repair와 headless Send2UE export를 동시에 처리할 개수입니다. "
            "기본값 2는 시작 비용을 겹치되 메모리 사용량을 제한합니다.",
        )

        lbl = ttk.Label(opts, text="가지당 목표 본 수:")
        lbl.pack(side="left")
        self.target_var = tk.DoubleVar(value=float(self.cfg.get("target_bones_per_branch", 2.0)))
        spin = ttk.Spinbox(opts, from_=1, to=6, increment=0.5, textvariable=self.target_var, width=5)
        spin.pack(side="left", padx=4)
        tip = ("① SPM 본 세팅에서 '작은 식물'의 목표입니다.\n"
               "가지(spline) 수를 실제 익스포트로 세고, 총 본 수가 대략 '가지 수 × 이 값'이 "
               "되도록 SpeedTree의 Relative 값을 자동 계산합니다.\n"
               "· 작은 풀: 가지 하나당 이 개수 근처의 본이 들어감\n"
               "· 큰 나무: '가지 수 × 이 값'이 아래 '최대 총 본 수'를 넘으면 "
               "Base 소속 본을 먼저 끄고, 그래도 넘을 때만 Tree 밀도를 낮춤\n"
               "Relative 스타일이라 가지 길이(=Size scalar 포함)에 비례해 본이 배분됩니다.")
        Tooltip(lbl, tip); Tooltip(spin, tip)

        lbl2 = ttk.Label(opts, text="최대 총 본 수:")
        lbl2.pack(side="left", padx=(10, 0))
        self.maxtotal_var = tk.IntVar(value=int(self.cfg.get("max_total_bones", 1500)))
        spin2 = ttk.Spinbox(opts, from_=200, to=8000, increment=100, textvariable=self.maxtotal_var, width=6)
        spin2.pack(side="left", padx=4)
        tip2 = ("한 나무의 총 본 수 상한입니다 (본 폭증 방지의 핵심).\n"
                "큰 나무는 가지가 수천~수만 개라 '가지당 목표'를 그대로 적용하면 본이 폭발합니다"
                "(예: elm_03은 가지 15,234개 → 예전 방식 80,000본).\n"
                "이 상한이 걸리면 Base 소속 자동 대상부터 Absolute/0으로 끕니다. "
                "그래도 초과할 때만 Tree/트렁크의 Relative 밀도를 낮춥니다.")
        Tooltip(lbl2, tip2); Tooltip(spin2, tip2)

        lbl4 = ttk.Label(opts, text="우선순위:")
        lbl4.pack(side="left", padx=(14, 0))
        self.priority_var = tk.StringVar(value=self.cfg.get("priority", "belownormal"))
        combo = ttk.Combobox(opts, textvariable=self.priority_var, values=["idle", "belownormal", "normal"],
                             width=11, state="readonly")
        combo.pack(side="left", padx=4)
        tip4 = ("백그라운드 작업의 CPU 우선순위입니다.\n"
                "idle = 다른 작업에 거의 영향 없음(가장 느림)\n"
                "belownormal = 권장 (다른 작업 우선, 놀 때만 전력)\n"
                "normal = 최고 속도 (컴퓨터가 무거워질 수 있음)")
        Tooltip(lbl4, tip4); Tooltip(combo, tip4)

        lbl5 = ttk.Label(opts, text="CPU 코어:")
        lbl5.pack(side="left", padx=(10, 0))
        self.cores_var = tk.IntVar(value=int(self.cfg.get("cpu_cores", 4)))
        spin5 = ttk.Spinbox(opts, from_=1, to=64, textvariable=self.cores_var, width=5)
        spin5.pack(side="left", padx=4)
        tip5 = ("백그라운드 작업이 사용할 수 있는 CPU 코어 수 제한입니다 (순차 실행 시). "
                "동시 실행이 2 이상이면 이 제한은 무시하고 모든 코어에 분산합니다.")
        Tooltip(lbl5, tip5); Tooltip(spin5, tip5)

        lbl6 = ttk.Label(opts, text="동시 실행:")
        lbl6.pack(side="left", padx=(10, 0))
        self.parallel_var = tk.IntVar(value=int(self.cfg.get("spm_parallel_jobs", 4)))
        spin6 = ttk.Spinbox(opts, from_=1, to=16, textvariable=self.parallel_var, width=4)
        spin6.pack(side="left", padx=4)
        tip6 = ("① SPM 본 세팅을 몇 개 파일 동시에 처리할지.\n"
                "기본값 4로 독립 SPM을 병렬 처리합니다. 한 번의 SpeedTree 익스포트가 "
                "2분을 넘는 파일은 원본을 복원하고 수동 처리로 넘깁니다.")
        Tooltip(lbl6, tip6); Tooltip(spin6, tip6)

        self.force_var = tk.BooleanVar(value=False)
        chk = ttk.Checkbutton(
            opts,
            text="최신 .blend/캐시가 있어도 강제로 다시 실행",
            variable=self.force_var,
        )
        chk.pack(side="left", padx=12)
        Tooltip(chk, ("② Blender Repair에서, 이미 SPM보다 최신인 .blend가 있는 항목은 기본적으로 "
                      "건너뜁니다. ① SPM 본 세팅도 동일 SPM/옵션 캐시를 기본 사용합니다. "
                      "이 옵션을 켜면 ①② 모두 강제로 다시 실행합니다.\n"
                      "판정 기준은 '작업이 성공했는가'가 아니라 '.blend가 SPM보다 최신인가' "
                      "입니다. 그래서 export가 게이트에 막혀 산출물이 안 나온 항목도 "
                      "이전 .blend가 남아 있으면 기본적으로 건너뜁니다.\n"
                      "↻ 재시도에서도 같은 옵션이 적용되어, 증거가 불완전해 fail-closed로 "
                      "막히던 항목까지 전체 Blender→Send2UE→Unreal 재빌드로 보냅니다."))

        actions = ttk.Frame(self.root, padding=6)
        actions.pack(fill="x")
        self.btn_check = ttk.Button(actions, text="🔍 검사 (수정 없음)",
                                    command=lambda: self.start_batch("check"))
        self.btn_check.pack(side="left")
        Tooltip(self.btn_check, ("아무것도 수정하지 않습니다.\n"
                                 "SPM 본 세팅 상태 / M_ 머티리얼 / blend 최신 여부 / 핸드오프 JSON "
                                 "준비 여부를 빠르게 확인해서 표에 채웁니다.\n"
                                 "'고정 본(Absolute)'은 자동 보정 실패가 아니라 가지마다 고정된 "
                                 "본 수를 쓰는 Generator이고, '자동 본(Relative)'은 가지 길이에 따라 "
                                 "본 밀도가 계산되는 Generator입니다.\n"
                                 "수동 본 유지 항목은 검증된 최근 XML의 실제 SpeedTree 본 수를 "
                                 "표시하고, 이후 SPM이 바뀌었으면 미재검증으로 구분합니다. "
                                 "검사 결과는 파일이나 실행 캐시에 저장하지 않습니다.\n"
                                 "오래 걸리는 ①②③을 돌리기 전에 먼저 눌러보세요."))
        self.btn_spm = ttk.Button(actions, text="① SPM 본 세팅 (빠름)",
                                  command=lambda: self.start_batch("spm"))
        self.btn_spm.pack(side="left", padx=6)
        Tooltip(self.btn_spm, ("SPM만 수정합니다 (blend/언리얼은 건드리지 않음).\n"
                               "가지 수를 실측해서 '가지당 목표 본 수'에 맞게 본 값을 자동 계산하고, "
                               "머티리얼에 M_ 프리픽스를 붙입니다.\n"
                               "수정 전 _spm_backups\\ 에 백업이 남고, 실패하면 자동 복원됩니다.\n"
                               "느린 ②로 넘어가기 전에 여기서 전부 끝내고 결과를 확인하세요."))
        self.btn_blender = ttk.Button(actions, text="② Blender Repair (느림)",
                                      command=lambda: self.start_batch("blender"))
        self.btn_blender.pack(side="left", padx=6)
        Tooltip(self.btn_blender, ("① SPM 본 세팅을 먼저 실행한 뒤 ② Blender Repair를 실행합니다.\n"
                                   "헤드리스 Blender로 SpeedTree 익스포트→임포트→본/웨이트 수리를 돌리고 "
                                   "SPM 옆에 같은 이름의 .blend와 wind JSON을 저장합니다.\n"
                                   "완료 전에 T_ 6종 또는 보존 Cluster 텍스처, 빈 머티리얼 슬롯까지 검사합니다.\n"
                                   "파일당 수분~수십분. 이미 최신인 blend는 건너뜁니다("
                                   "'완료된 항목도 다시 실행'으로 강제 가능)."))
        self.btn_push = ttk.Button(actions, text="③ Unreal Push",
                                   command=lambda: self.start_batch("push"))
        self.btn_push.pack(side="left", padx=6)
        Tooltip(self.btn_push, ("① SPM 본 세팅→② Blender Repair를 먼저 실행한 뒤 "
                                "③ Unreal Push를 실행합니다.\n"
                                "push 전에 준비 검사를 먼저 전부 통과시킵니다:\n"
                                "· .blend 존재 + SPM보다 최신인지\n"
                                "· 텍스처 세트와 Repair 사전검사(빈 머티리얼 슬롯 포함)\n"
                                "· wind JSON(핸드오프 산출물) 존재\n"
                                "· 언리얼 에디터 실행 여부\n"
                                "준비 안 된 항목은 이유를 표시하고 건너뛴 뒤, 준비된 것만 push합니다."))
        self.btn_retry_failed = ttk.Button(
            actions,
            text="↻ 전체 실패 이력 재시도",
            command=self.start_failed_results_retry,
        )
        self.btn_retry_failed.pack(side="left", padx=(2, 6))
        Tooltip(
            self.btn_retry_failed,
            "체크 상태와 무관하게 현재 목록 전체의 실패/stale 이력을 "
            "원인별로 나눠 재시도합니다.\n"
            "· Repair reason code: exact PCG 텍스처 또는 Generator/Cluster를 "
            "자동 복구하고 fresh 재검증 후 Blender/Unreal 재시도\n"
            "· Blender/Send2UE export 실패: ② Blender부터 export를 다시 만들고 "
            "③ Unreal Push까지 실행\n"
            "· Unreal ingest 실패: Blender를 다시 돌리지 않고 기존 FBX·JSON·Assembly "
            "산출물과 입력 identity를 검증한 뒤 현재 Unreal 코드로만 재시도\n"
            "BAT로 해결할 수 없거나 fresh 재검증에 실패한 항목만 최종 실패로 "
            "남기고 사용자 조치와 원래 reason code를 기록합니다.",
        )
        self.btn_retry_checked = ttk.Button(
            actions,
            text="↻ 체크 항목 실패 재시도",
            command=self.start_checked_failed_results_retry,
        )
        self.btn_retry_checked.pack(side="left", padx=(2, 6))
        Tooltip(
            self.btn_retry_checked,
            "현재 체크한 항목만 실패/stale 이력을 판정하고 재시도합니다.\n"
            "체크하지 않은 항목은 계획·검증·실행 대상에 포함하지 않습니다.",
        )
        self.btn_all = ttk.Button(
            actions,
            text="🌙 목록 전체 자동 ①→②→③",
            command=self.start_full_pipeline,
        )
        self.btn_all.pack(side="left", padx=(10, 4))
        Tooltip(
            self.btn_all,
            "체크 상태와 무관하게 현재 목록의 모든 항목을 밤새 순서대로 처리합니다.\n"
            "① SPM 본 세팅 전체 완료 → ② Blender Repair 전체 완료 → "
            "③ Unreal Push 순서입니다.\n"
            "개별 ①/②/③ 버튼만 체크된 항목을 대상으로 합니다.\n"
            "개별 실패·수동 처리 항목은 기록하고 나머지 파일은 계속 진행합니다.",
        )
        self.btn_stop = ttk.Button(actions, text="중지", command=self.stop_batch, state="disabled")
        self.btn_stop.pack(side="left", padx=10)
        self.progress_var = tk.StringVar(value="대기")
        ttk.Label(actions, textvariable=self.progress_var).pack(side="left", padx=14)

        meters = ttk.Frame(self.root, padding=(8, 0, 8, 4))
        meters.pack(fill="x")
        ttk.Label(meters, text="전체:").pack(side="left")
        self.batch_progress = ttk.Progressbar(
            meters, mode="determinate", maximum=100, length=220
        )
        self.batch_progress.pack(side="left", padx=(4, 6))
        self.batch_progress_var = tk.StringVar(value="0/0 (0%)")
        ttk.Label(meters, textvariable=self.batch_progress_var, width=15).pack(side="left")
        ttk.Label(
            meters,
            text="단계·경과 시간·수동 전환까지 남은 시간은 각 파일 행에 표시됩니다.",
        ).pack(side="left", padx=(14, 0))

        retry_live = ttk.LabelFrame(
            self.root,
            text="실패 재시도 진행·liveness (durable receipt)",
            padding=(8, 4),
        )
        retry_live.pack(fill="x", padx=6, pady=(0, 4))
        self.retry_target_var = tk.StringVar(
            value="current target: - · 0/0 · partition=-"
        )
        self.retry_liveness_var = tk.StringVar(
            value=(
                "stage=idle · elapsed 0s · progress age - · "
                "output age - · heartbeat age -"
            )
        )
        self.retry_outcome_var = tk.StringVar(
            value=(
                "retry scope: historical failed/stale selection · "
                "current state: idle · terminal outcome: pending"
            )
        )
        self.retry_diagnostic_var = tk.StringVar(value="latest: -")
        ttk.Label(
            retry_live,
            textvariable=self.retry_target_var,
            anchor="w",
        ).pack(fill="x")
        ttk.Label(
            retry_live,
            textvariable=self.retry_liveness_var,
            anchor="w",
        ).pack(fill="x")
        ttk.Label(
            retry_live,
            textvariable=self.retry_outcome_var,
            anchor="w",
        ).pack(fill="x")
        ttk.Label(
            retry_live,
            textvariable=self.retry_diagnostic_var,
            anchor="w",
        ).pack(fill="x")

        cols = ("bone_mode", "wind", "spm_status", "blend_status", "push_status", "folder")
        visible_cols = ("bone_mode", "wind", "spm_status", "blend_status", "push_status")
        tablef = ttk.LabelFrame(
            self.root,
            text="파일 목록 (표는 요약 · 행을 선택하면 아래에 전체 내용 표시)",
            padding=4,
        )
        tablef.pack(fill="both", expand=True, padx=6)
        tablef.columnconfigure(0, weight=1)
        tablef.rowconfigure(0, weight=1)
        self.tree = ttk.Treeview(
            tablef,
            columns=cols,
            displaycolumns=visible_cols,
            show="tree headings",
            height=12,
        )
        self.tree.heading("#0", text="파일 (첫 클릭=이 행만 활성 · Ctrl+C=SPM 경로)")
        self.tree.column("#0", width=300, minwidth=220, anchor="w")
        headers = {
            "bone_mode": ("본 모드 (▼)", 125),
            "wind": ("Wind (▼)", 110),
            "spm_status": ("① SPM", 205),
            "blend_status": ("② Blender", 245),
            "push_status": ("③ Unreal", 190),
            "folder": ("폴더", 0),
        }
        for key, (label, width) in headers.items():
            self.tree.heading(key, text=label)
            self.tree.column(key, width=width, anchor="w")
        tree_y = ttk.Scrollbar(tablef, orient="vertical", command=self.tree.yview)
        tree_x = ttk.Scrollbar(tablef, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=tree_y.set, xscrollcommand=tree_x.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        tree_y.grid(row=0, column=1, sticky="ns")
        tree_x.grid(row=1, column=0, sticky="ew")
        self.tree.bind("<Button-1>", self._on_click)
        self.tree.bind("<Control-c>", self.copy_selected_paths, add="+")
        self.tree.bind("<<TreeviewSelect>>", self._refresh_selected_detail, add="+")

        detailf = ttk.LabelFrame(
            self.root,
            text="선택 항목 상세 (읽기 전용)",
            padding=4,
        )
        detailf.pack(fill="x", padx=6, pady=(4, 0))
        detailf.columnconfigure(0, weight=1)
        detailf.rowconfigure(0, weight=1)
        self.detail_text = tk.Text(
            detailf,
            height=5,
            wrap="word",
            state="disabled",
            borderwidth=0,
        )
        detail_y = ttk.Scrollbar(
            detailf, orient="vertical", command=self.detail_text.yview
        )
        self.detail_text.configure(yscrollcommand=detail_y.set)
        self.detail_text.grid(row=0, column=0, sticky="nsew")
        detail_y.grid(row=0, column=1, sticky="ns")

        logf = ttk.LabelFrame(self.root, text="로그", padding=4)
        logf.pack(fill="both", padx=6, pady=4)
        logf.columnconfigure(0, weight=1)
        logf.rowconfigure(0, weight=1)
        self.log_text = tk.Text(logf, height=7, wrap="none", state="disabled")
        log_y = ttk.Scrollbar(logf, orient="vertical", command=self.log_text.yview)
        log_x = ttk.Scrollbar(logf, orient="horizontal", command=self.log_text.xview)
        self.log_text.configure(yscrollcommand=log_y.set, xscrollcommand=log_x.set)
        self.log_text.grid(row=0, column=0, sticky="nsew")
        log_y.grid(row=0, column=1, sticky="ns")
        log_x.grid(row=1, column=0, sticky="ew")

    def _pick_root(self):
        path = filedialog.askdirectory(initialdir=self.root_var.get())
        if path:
            self.root_var.set(path)

    def log(self, msg):
        self.ui_queue.put(("log", msg))

    def _retry_progress_notify(self, snapshot):
        self.ui_queue.put(("retry_progress", snapshot))

    def _retry_progress_thresholds(self, cfg=None):
        cfg = cfg or getattr(self, "cfg", {}) or {}
        return {
            "stall_warning_seconds": float(
                cfg.get("retry_stall_warning_seconds", 120)
            ),
            "owner_lost_seconds": float(
                cfg.get("retry_owner_lost_seconds", 45)
            ),
        }

    def _new_retry_progress(self, target_ids, cfg=None):
        tracker = RetryProgressReceipt.create(
            target_ids,
            notify=self._retry_progress_notify,
            **self._retry_progress_thresholds(cfg),
        )
        self._active_retry_progress = tracker
        self._retry_progress_by_run_id[tracker.run_id] = tracker
        return tracker

    def _reusable_retry_plan_cache(self):
        """Return only a completed prior run's durable plan candidate."""

        if self.__dict__.pop("_skip_retry_plan_cache_once", False):
            return None
        tracker = getattr(self, "_active_retry_progress", None)
        if tracker is None:
            return None
        snapshot = tracker.snapshot(evaluate=False)
        if snapshot.get("run_state") != "terminal":
            return None
        return tracker.planning_cache()

    def _restore_latest_retry_progress(self):
        tracker = RetryProgressReceipt.load_latest(
            notify=None,
            **self._retry_progress_thresholds(),
        )
        if tracker is None:
            return None
        runtime = getattr(self, "shared_queue_runtime", None)
        if runtime is not None:
            tracker.reconcile_queue(runtime.queue)
            planning = tracker.snapshot(evaluate=False).get("planning") or {}
            tracker.reconcile_planning_owner(
                runtime.owner_process_alive(planning.get("owner"))
            )
        else:
            tracker.reconcile_planning_owner(None)
        tracker.set_notify(self._retry_progress_notify)
        self._active_retry_progress = tracker
        self._retry_progress_by_run_id[tracker.run_id] = tracker
        self._render_retry_progress(tracker.snapshot())
        return tracker

    @staticmethod
    def _retry_age_text(value):
        if value is None:
            return "-"
        seconds = max(0, int(float(value)))
        if seconds < 60:
            return f"{seconds}s"
        minutes, seconds = divmod(seconds, 60)
        if minutes < 60:
            return f"{minutes}m {seconds:02d}s"
        hours, minutes = divmod(minutes, 60)
        return f"{hours}h {minutes:02d}m"

    def _render_retry_progress(self, snapshot):
        if not isinstance(snapshot, dict):
            return
        rows = snapshot.get("targets") or []
        current_id = snapshot.get("current_target_id")
        current = next(
            (row for row in rows if row.get("target_id") == current_id),
            rows[-1] if rows else None,
        )
        if current is None:
            self.retry_target_var.set("current target: - · 0/0 · partition=-")
            self.retry_liveness_var.set(
                "stage=idle · elapsed 0s · progress age - · "
                "output age - · heartbeat age -"
            )
            self.retry_outcome_var.set(
                "retry scope: historical failed/stale selection · "
                "current state: idle · terminal outcome: pending"
            )
            self.retry_diagnostic_var.set("latest: -")
            return
        finished = sum(
            row.get("terminal_at") is not None for row in rows
        )
        succeeded = sum(
            row.get("stage") == RETRY_STAGE_COMPLETE for row in rows
        )
        waiting = sum(
            row.get("stage") == RETRY_STAGE_PENDING_UNREAL for row in rows
        )
        cancelled = sum(
            row.get("stage") == RETRY_STAGE_CANCELLED for row in rows
        )
        blocked = sum(
            row.get("stage") == RETRY_STAGE_BLOCKED for row in rows
        )
        owner_lost = sum(
            row.get("stage") == RETRY_STAGE_OWNER_LOST for row in rows
        )
        failed = sum(
            row.get("stage") == RETRY_STAGE_FAILED
            for row in rows
        )
        remaining = max(0, len(rows) - finished)
        terminal = (
            snapshot.get("run_state") == "terminal"
            or snapshot.get("terminal_at") is not None
        )
        if terminal:
            outcome_text = (
                "terminal outcome: "
                f"{snapshot.get('terminal_outcome') or snapshot.get('stage') or '-'}"
                + (
                    f" ({snapshot.get('terminal_reason')})"
                    if snapshot.get("terminal_reason")
                    else ""
                )
            )
            state_text = "terminal"
        else:
            outcome_text = "terminal outcome: pending"
            evidence_state = str(
                snapshot.get("evidence_state") or "evidence_unknown"
            )
            if snapshot.get("run_state") == "waiting":
                state_text = "waiting"
            elif evidence_state in {
                "stalled",
                "owner_lost",
                "failed",
                "owner_unknown",
                "heartbeat_unknown",
            }:
                state_text = evidence_state
            else:
                state_text = "running"
        continuation = (
            " · current run continues after individual failures"
            if (failed or owner_lost or blocked) and remaining
            else ""
        )
        self.retry_outcome_var.set(
            "retry scope: historical failed/stale selection · "
            f"current state: {state_text} · success {succeeded} · "
            f"waiting {waiting} · cancelled {cancelled} · "
            f"blocked {blocked} · owner_lost {owner_lost} · "
            f"failed {failed} · remaining {remaining} · {outcome_text}"
            + continuation
        )
        partition_ordinal = current.get("partition_ordinal") or "?"
        partition_total = current.get("partition_total") or "?"
        planning = snapshot.get("planning") or {}
        planning_progress = planning.get("progress") or {}
        planning_visible = (
            current.get("stage") == RETRY_STAGE_PLANNING
            and planning.get("status") in {"active", "ready", "committing"}
        )
        if planning_visible and planning_progress:
            completed = int(planning_progress.get("completed_count") or 0)
            total = int(planning_progress.get("total_count") or len(rows))
            percent = (completed / total * 100.0) if total else 0.0
            substage = str(planning_progress.get("substage") or "planning")
            cache_status = str(
                planning_progress.get("cache_status") or "unchecked"
            )
            self.retry_target_var.set(
                f"planning: {substage} · {completed}/{total} · "
                f"current {current.get('target_name') or '-'} · "
                f"cache={cache_status}"
            )
            self.progress_var.set(
                f"retry planning · {substage} · {completed}/{total} · "
                f"classified {int(planning_progress.get('classified_count') or 0)} · "
                f"validated {int(planning_progress.get('validated_count') or 0)} · "
                f"cache={cache_status}"
            )
            self.batch_progress.configure(value=percent)
            self.batch_progress_var.set(
                f"{completed}/{total} ({percent:.0f}%)"
            )
        else:
            self.retry_target_var.set(
                f"current target: {current.get('target_name') or '-'} · "
                f"{finished}/{len(rows)} finished · partition="
                f"{current.get('partition') or '-'} "
                f"{partition_ordinal}/{partition_total}"
            )
        # Prefix this as an individual target observation: an item-level
        # failed stage is not a current batch terminal outcome.
        self.retry_liveness_var.set(
            "evidence state="
            f"{snapshot.get('evidence_state') or 'unknown'} · "
            "current target stage="
            f"{current.get('stage') or '-'} · wall elapsed "
            f"{self._retry_age_text(current.get('wall_elapsed_seconds', current.get('elapsed_seconds')))} · "
            "progress age "
            f"{self._retry_age_text(current.get('last_progress_age_seconds'))} · "
            "output age "
            f"{self._retry_age_text(current.get('last_output_age_seconds'))} · "
            "heartbeat age "
            f"{self._retry_age_text(current.get('last_heartbeat_age_seconds'))}"
        )
        self.retry_diagnostic_var.set(
            "latest: " + str(current.get("latest_diagnostic") or "-")
        )

    def _retry_tracker_for_job(self, job=None):
        job = job or getattr(self, "active_batch_job", None)
        if not isinstance(job, dict):
            return None
        tracker = job.get("_retry_progress_tracker")
        if tracker is not None:
            return tracker
        metadata = job.get("retry_metadata") or {}
        run_id = str(metadata.get("progress_run_id") or "")
        tracker = getattr(self, "_retry_progress_by_run_id", {}).get(run_id)
        if tracker is not None:
            job["_retry_progress_tracker"] = tracker
        return tracker

    def _retry_transition(
        self,
        target_id,
        stage,
        diagnostic,
        *,
        progress=False,
        output=False,
        heartbeat=False,
        terminal_reason=None,
        outcome=None,
    ):
        tracker = self._retry_tracker_for_job()
        if tracker is None:
            return False
        return tracker.transition(
            str(target_id),
            stage,
            diagnostic=diagnostic,
            progress=progress,
            output=output,
            heartbeat=heartbeat,
            terminal_reason=terminal_reason,
            outcome=outcome,
        )

    def _refresh_retry_liveness(self):
        if not getattr(self, "_app_open", True):
            return
        tracker = getattr(self, "_active_retry_progress", None)
        active_job = getattr(self, "active_batch_job", None)
        job_tracker = self._retry_tracker_for_job(active_job)
        if job_tracker is not None:
            tracker = job_tracker
            self._active_retry_progress = tracker
            lease = getattr(self, "_active_shared_queue_lease", None)
            partition = str(
                ((active_job or {}).get("retry_metadata") or {}).get(
                    "partition"
                )
                or ""
            )
            if lease is not None and partition:
                heartbeat_error = lease.heartbeat_error
                if heartbeat_error is not None:
                    tracker.mark_partition_terminal(
                        partition,
                        RETRY_STAGE_OWNER_LOST,
                        "shared queue lease heartbeat lost: "
                        + compact_error_message(heartbeat_error, 160),
                    )
                    self.stop_flag.set()
                else:
                    snapshot = tracker.snapshot(evaluate=False)
                    current_id = snapshot.get("current_target_id")
                    if current_id:
                        tracker.observe_process(current_id)
        if tracker is not None:
            runtime = getattr(self, "shared_queue_runtime", None)
            if runtime is not None:
                tracker.reconcile_queue(runtime.queue)
                planning = tracker.snapshot(evaluate=False).get(
                    "planning"
                ) or {}
                owner_alive = runtime.owner_process_alive(
                    planning.get("owner")
                )
                worker = next(
                    (
                        candidate
                        for candidate in getattr(
                            self, "_retry_planning_workers", ()
                        )
                        if getattr(
                            candidate, "retry_progress_run_id", None
                        ) == tracker.run_id
                    ),
                    None,
                )
                if (
                    worker is not None
                    and planning.get("status") == "active"
                ):
                    owner_alive = worker.is_alive()
                tracker.reconcile_planning_owner(owner_alive)
            else:
                tracker.reconcile_planning_owner(None)
            self._render_retry_progress(tracker.snapshot())
        self.root.after(1000, self._refresh_retry_liveness)

    def _table_display_value(self, iid, column, value):
        if column not in STATUS_COLUMNS:
            return value
        if not hasattr(self, "_table_full_values"):
            self._table_full_values = {}
        full_value = str(value or "-")
        self._table_full_values[(iid, column)] = full_value
        return compact_table_status(full_value)

    def _set_table_cell(self, iid, column, value):
        self.tree.set(iid, column, self._table_display_value(iid, column, value))
        self._refresh_selected_detail()

    def _refresh_selected_detail(self, _event=None):
        detail = getattr(self, "detail_text", None)
        if detail is None:
            return
        selected = self.tree.selection()
        folder_rows = getattr(self, "folder_rows", {})
        if selected and selected[0] in folder_rows:
            text = f"경로 복사 전용 폴더 행\n{folder_rows[selected[0]]}"
        elif not selected or selected[0] not in self.items:
            text = "행을 선택하면 전체 경로와 ①②③ 상태가 여기에 표시됩니다."
        else:
            iid = selected[0]
            statuses = {
                column: getattr(self, "_table_full_values", {}).get(
                    (iid, column), self.tree.set(iid, column) or "-"
                )
                for column in STATUS_COLUMNS
            }
            text = selected_row_detail_text(self.items[iid]["spm"], statuses)
        detail.configure(state="normal")
        detail.delete("1.0", "end")
        detail.insert("1.0", text)
        detail.configure(state="disabled")

    def _drain_ui_queue(self):
        try:
            while True:
                kind, payload = self.ui_queue.get_nowait()
                if kind == "log":
                    stamp = datetime.now().strftime("%H:%M:%S")
                    self.log_text.configure(state="normal")
                    self.log_text.insert("end", f"[{stamp}] {payload}\n")
                    self.log_text.see("end")
                    self.log_text.configure(state="disabled")
                elif kind == "cell":
                    iid, column, value = payload
                    if iid in self.items:
                        self._set_table_cell(iid, column, value)
                elif kind == "progress":
                    pending = len(getattr(self, "pending_batch_jobs", ()))
                    suffix = f" · 대기 {pending}개" if pending else ""
                    self.progress_var.set(f"{payload}{suffix}")
                elif kind == "batch_progress":
                    done, total = payload
                    percent = (done / total * 100.0) if total else 0.0
                    self.batch_progress.configure(value=percent)
                    pending = len(getattr(self, "pending_batch_jobs", ()))
                    suffix = f" · 대기 {pending}개" if pending else ""
                    self.batch_progress_var.set(
                        f"{done}/{total} ({percent:.0f}%){suffix}"
                    )
                elif kind == "scan_done":
                    generation, result, error = payload
                    if error is not None:
                        self.scan_worker = None
                        self.log(f"스캔 실패: {error}")
                        self.progress_var.set("스캔 실패")
                        self._set_scan_controls(False)
                    else:
                        self.scan(prepared=result, generation=generation)
                elif kind == "live_status_done":
                    if isinstance(payload, tuple):
                        generation, audit_complete, error_count = payload
                    else:
                        generation = payload
                        audit_complete = True
                        error_count = 0
                    if generation == getattr(
                        self, "_initial_live_status_generation", None
                    ):
                        self._initial_live_status_generation = None
                        self._set_scan_controls(False)
                        if audit_complete:
                            self.progress_var.set(
                                "스캔 완료 · Blender/파이프라인 상태 확인 완료"
                            )
                        else:
                            self.progress_var.set(
                                "스캔 완료 · Blender 상태 확인 실패 "
                                f"{error_count}개 · 다음 주기 자동 재시도"
                            )
                elif kind == "done":
                    # Compatibility for direct worker/test calls. Queued jobs
                    # finish through batch_job_done so the next request starts
                    # only after the previous worker has returned.
                    if getattr(self, "active_batch_job", None) is None:
                        self._set_batch_queue_controls(False)
                elif kind == "batch_job_done":
                    self._finish_batch_job(payload)
                elif kind == "retry_progress":
                    self._render_retry_progress(payload)
                elif kind == "retry_plan_ready":
                    self._commit_failed_retry_plan(payload)
                elif kind == "modeler_recovery":
                    target = Path(payload["target_spm"])
                    self.progress_var.set(
                        "SpeedTree Modeler semantic Save in progress — "
                        + target.name
                    )
        except queue.Empty:
            pass
        self.root.after(150, self._drain_ui_queue)

    # ------------------------------------------------------------------ scan
    @staticmethod
    def _snapshot_spm(task):
        spm, cached_snapshot = task
        try:
            snapshot, cache_hit = cached_file_content_snapshot(
                spm, cached_snapshot
            )
            return str(spm), snapshot, None, cache_hit
        except OSError as exc:
            return str(spm), None, str(exc), False

    @classmethod
    def _collect_scan_result(cls, root, snapshot_caches):
        spms = [
            spm
            for spm in scan_sk_spms(root)
            if is_live_spm(spm, require_file=False)
        ]
        cluster_sources = [
            row
            for row in scan_cluster_spm_sources(root)
            if is_live_spm(row.get("authoring_spm"), require_file=False)
        ]
        # The population scan above can cold-parse hundreds of SPMs. Persist
        # that shared analysis immediately instead of waiting for a later
        # live-status change that may never occur before process exit.
        save_leaf_contract_cache()
        all_spms = list(spms)
        snapshot_aliases = {}
        for row in cluster_sources:
            canonical = row["authoring_spm"]
            snapshot_source = (
                canonical if canonical.is_file() else row["output_spm"]
            )
            all_spms.append(snapshot_source)
            snapshot_aliases[str(snapshot_source)] = str(canonical)
        snapshots = {}
        errors = []
        cache_hits = 0
        if all_spms:
            tasks = [
                (spm, snapshot_caches.get(str(spm))) for spm in all_spms
            ]
            with ThreadPoolExecutor(max_workers=min(8, len(all_spms))) as pool:
                for iid, snapshot, error, cache_hit in pool.map(
                    cls._snapshot_spm, tasks
                ):
                    result_iid = snapshot_aliases.get(iid, iid)
                    if snapshot:
                        snapshots[result_iid] = snapshot
                        cache_hits += int(cache_hit)
                    elif error:
                        errors.append((result_iid, error))
        return {
            "spms": spms,
            "cluster_sources": cluster_sources,
            "snapshots": snapshots,
            "errors": errors,
            "snapshot_cache_hits": cache_hits,
        }

    @staticmethod
    def _migrate_restored_marker_calibration_cache(
        spm, calibration_cache, current_snapshot
    ):
        """Retarget a cache produced from this tool's former cosmetic marker.

        The cleanup receipt must prove that the current SPM is the exact
        pre-marker backup and the cached SPM fingerprint is the exact marked
        recovery backup. Ordinary user edits cannot pass both checks.
        """
        if (
            not isinstance(calibration_cache, dict)
            or calibration_cache.get("version") != CALIBRATION_CACHE_VERSION
            or calibration_cache.get("status") not in {"calibrated", "already-ok"}
            or not isinstance(current_snapshot, dict)
        ):
            return False
        spm = Path(spm)
        receipt_path = (
            spm.parent
            / "reports"
            / f"{spm.stem}_material_problem_node_markers.json"
        )
        try:
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            receipt_spm = os.path.normcase(
                os.path.abspath(str(receipt.get("spm") or ""))
            )
            if (
                receipt.get("status") != "restored"
                or receipt.get("restore_preserved_original_timestamp") is not True
                or receipt_spm
                != os.path.normcase(os.path.abspath(str(spm)))
            ):
                return False
            restore_source = Path(receipt.get("restore_source") or "")
            recovery_rows = [
                row
                for row in (receipt.get("backups") or [])
                if isinstance(row, dict)
                and row.get("reason") == "before_exact_marker_cleanup_restore"
            ]
            if not restore_source.is_file() or not recovery_rows:
                return False
            original_snapshot = file_content_snapshot(restore_source)
            marked_snapshot = file_content_snapshot(recovery_rows[-1]["path"])
        except (OSError, ValueError, TypeError, KeyError):
            return False
        if (
            original_snapshot.get("fingerprint")
            != current_snapshot.get("fingerprint")
            or marked_snapshot.get("fingerprint")
            != calibration_cache.get("spm_fingerprint")
        ):
            return False
        calibration_cache["spm_fingerprint"] = current_snapshot["fingerprint"]
        calibration_cache["marker_restore_cache_migrated_at"] = (
            datetime.now().isoformat(timespec="seconds")
        )
        return True

    @staticmethod
    def _quick_blend_status_text(spm):
        """Paint a non-running placeholder; live validation follows."""
        blend = blend_path_for(spm)
        if not blend.exists():
            return "생성 필요 — blend 없음 → ② Blender Repair"
        return "상태 확인 대기…"

    @staticmethod
    def _normalized_live_status_signature(value):
        """Normalize JSON lists to the tuple form produced at runtime."""
        if isinstance(value, (list, tuple)):
            return tuple(
                App._normalized_live_status_signature(child)
                for child in value
            )
        return value

    @staticmethod
    def _transient_blend_status(value):
        text = str(value or "").strip().casefold()
        return any(marker in text for marker in (
            "검증 중",
            "상태 확인 대기",
            "재질 사전검사 중",
            "blender repair 중",
        ))

    def _set_scan_controls(self, scanning):
        state = "disabled" if scanning else "normal"
        for name in (
            "btn_check", "btn_spm", "btn_blender", "btn_push",
            "btn_retry_failed", "btn_all",
            "btn_pick_root", "btn_scan", "btn_select_all", "btn_clear_all",
            "btn_recent_24h",
        ):
            button = getattr(self, name, None)
            if button is not None:
                button.configure(state=state)
        root_entry = getattr(self, "root_entry", None)
        if root_entry is not None:
            root_entry.configure(state=state)

    def scan(self, prepared=None, generation=None):
        if prepared is None:
            self._scan_generation = getattr(self, "_scan_generation", 0) + 1
            generation = self._scan_generation
            root = self.root_var.get()
            self.cfg = self._collect_cfg()
            self.cfg["root"] = root
            self.spm_calibration_signature = calibration_settings_signature(self.cfg)
            self.legacy_spm_calibration_signature = (
                legacy_calibration_settings_signature(self.cfg)
            )
            save_config(self.cfg)
            with self.state_lock:
                snapshot_caches = {
                    iid: entry.get("spm_snapshot_cache")
                    for iid, entry in self.state.items()
                    if isinstance(entry, dict)
                }

            if hasattr(self, "root") and hasattr(self.root, "after"):
                self._set_scan_controls(True)
                if hasattr(self, "progress_var"):
                    self.progress_var.set("SK SPM 스캔 중…")

                def worker():
                    try:
                        result = self._collect_scan_result(root, snapshot_caches)
                        error = None
                    except Exception as exc:
                        result, error = None, exc
                    self.ui_queue.put((
                        "scan_done", (generation, result, error)
                    ))

                self.scan_worker = threading.Thread(target=worker, daemon=True)
                self.scan_worker.start()
                return
            prepared = self._collect_scan_result(root, snapshot_caches)
        elif generation != getattr(self, "_scan_generation", 0):
            return
        else:
            root = self.root_var.get()
            self.scan_worker = None

        for iid in self.tree.get_children():
            self.tree.delete(iid)
        self.items.clear()
        if not hasattr(self, "folder_rows"):
            self.folder_rows = {}
        else:
            self.folder_rows.clear()
        if not hasattr(self, "row_copy_paths"):
            self.row_copy_paths = {}
        else:
            self.row_copy_paths.clear()
        if not hasattr(self, "_table_full_values"):
            self._table_full_values = {}
        else:
            self._table_full_values.clear()
        cluster_sources = [
            row
            for row in (prepared.get("cluster_sources") or [])
            if is_live_spm(row.get("authoring_spm"), require_file=False)
        ]
        cluster_by_source = {
            _normalized_path(row["authoring_spm"]): row
            for row in cluster_sources
        }
        spms = [
            spm
            for spm in prepared["spms"]
            if is_live_spm(spm, require_file=False)
        ] + [
            row["authoring_spm"] for row in cluster_sources
        ]
        snapshots = prepared["snapshots"]

        def ensure_folder_row(folder, parent=""):
            folder = Path(folder)
            folder_iid = f"folder::{_normalized_path(folder)}"
            if folder_iid in self.folder_rows:
                return folder_iid
            values = ("", "", "", "", "", str(folder))
            try:
                self.tree.insert(
                    parent,
                    "end",
                    iid=folder_iid,
                    text=folder.name or str(folder),
                    values=values,
                    open=True,
                )
            except TypeError:
                # Lightweight test Treeviews do not expose Tk's `open` option.
                self.tree.insert(
                    parent,
                    "end",
                    iid=folder_iid,
                    text=folder.name or str(folder),
                    values=values,
                )
            self.folder_rows[folder_iid] = folder
            self.row_copy_paths[folder_iid] = [folder]
            return folder_iid

        for iid, snapshot_error in prepared.get("errors", []):
            self.log(
                f"[경고] SPM 지문 계산 실패: {Path(iid).name}: "
                f"{snapshot_error}"
            )
        for spm in spms:
            iid = str(spm)
            cluster_source = cluster_by_source.get(_normalized_path(spm))
            source_read_only = False
            entry = self.state.setdefault(iid, {})
            cached_blend_status = entry.get("blend_status")
            cached_live_signature = self._normalized_live_status_signature(
                entry.get("live_status_signature")
            )
            cached_texture_paths = tuple(
                entry.get("live_texture_paths") or ()
            )
            # Never restore a former in-progress label as active work. A
            # cached result is displayed only with its saved filesystem
            # signature; otherwise the row waits for the read-only audit.
            if (
                cached_live_signature is not None
                and cached_blend_status
                and not self._transient_blend_status(cached_blend_status)
            ):
                blend_status = cached_blend_status
            else:
                blend_status = self._quick_blend_status_text(spm)
                # A signature without a reusable result is not a valid cache
                # entry.  Keeping it would make the first live audit see an
                # unchanged filesystem and skip the row forever, leaving the
                # placeholder/in-progress label on screen.
                cached_live_signature = None
                cached_texture_paths = ()
                entry.pop("live_status_signature", None)
                entry.pop("live_texture_paths", None)
                if self._transient_blend_status(cached_blend_status):
                    entry.pop("blend_status", None)
            if snapshots.get(iid):
                entry["spm_snapshot_cache"] = snapshots[iid]
                calibration_cache = entry.get("calibration_cache")
                self._migrate_restored_marker_calibration_cache(
                    spm, calibration_cache, snapshots[iid]
                )
                if (
                    isinstance(calibration_cache, dict)
                    and calibration_cache.get("settings_signature")
                    == getattr(self, "legacy_spm_calibration_signature", None)
                    and calibration_cache_matches(
                        calibration_cache,
                        snapshots[iid]["fingerprint"],
                        self.spm_calibration_signature,
                        getattr(
                            self, "legacy_spm_calibration_signature", None
                        ),
                    )
                ):
                    # Migrate a still-current legacy stat-bearing signature at
                    # scan time.  From now on a touch-only INI/audit timestamp
                    # change cannot schedule this SPM again.
                    calibration_cache["settings_signature"] = (
                        self.spm_calibration_signature
                    )
            wind_override = entry.get("wind_override", "auto")
            if wind_override not in {value for _label, value in WIND_OPTIONS}:
                wind_override = "auto"
                entry["wind_override"] = "auto"
            manual_bones_locked = is_manual_bones_locked(spm, entry)
            if manual_bones_locked:
                entry["manual_bones_locked"] = True
                self.state[iid] = entry
                marker = manual_bones_marker_path(spm)
                if not marker.exists():
                    try:
                        set_manual_bones_marker(spm, True)
                    except OSError as exc:
                        self.log(f"[경고] 수동 본 marker 저장 실패: {spm.name}: {exc}")
            self.items[iid] = {
                "spm": spm,
                "display_name": (
                    cluster_source["display_name"]
                    if cluster_source else spm.name
                ),
                "cluster_source_spm": (
                    cluster_source["output_spm"] if cluster_source else None
                ),
                "authoring_spm": (
                    cluster_source["authoring_spm"] if cluster_source else spm
                ),
                "output_spm": (
                    cluster_source["output_spm"] if cluster_source else spm
                ),
                "cluster_pair_status": (
                    cluster_source["pair_status"] if cluster_source else None
                ),
                "referenced_by_spms": (
                    cluster_source.get("referenced_by_spms", ())
                    if cluster_source else ()
                ),
                "source_read_only": source_read_only,
                "blend_path": blend_path_for(spm),
                "checked": True,
                "wind_override": wind_override,
                "bone_mode": "manual" if manual_bones_locked else "auto",
                "manual_bones_locked": manual_bones_locked,
                "spm_snapshot": snapshots.get(iid),
                "live_texture_paths": cached_texture_paths,
                "live_status_signature": cached_live_signature,
            }
            spm_status = (
                f"Output 규격 · {cluster_source['pair_status']}"
                if cluster_source
                else manual_bone_status_text(spm)
                if manual_bones_locked
                else entry.get("spm_status", "-")
            )
            push_status = self._current_push_status_text(iid, spm)
            owner = spm.parent.parent if cluster_source else spm.parent
            owner_iid = ensure_folder_row(owner)
            parent_iid = owner_iid
            if cluster_source:
                parent_iid = ensure_folder_row(spm.parent, owner_iid)
            self.tree.insert(
                parent_iid, "end", iid=iid,
                text=self._item_label(iid),
                values=(
                    self._bone_mode_label(iid),
                    self._wind_label(iid),
                    self._table_display_value(iid, "spm_status", spm_status),
                    self._table_display_value(iid, "blend_status", blend_status),
                    self._table_display_value(iid, "push_status", push_status),
                    str(spm.parent),
                ),
            )
            self.row_copy_paths[iid] = [spm]
        if TEMP_SELECT_RETRY_BLOCKED_ROWS:
            self._set_temporary_retry_blocked_rows()
        elif TEMP_SELECT_CLUSTER_WITHOUT_ASSEMBLY_PUSH_ROWS:
            self._set_temporary_cluster_without_assembly_push_rows()
        self.checked_rows.sync_after_reload()
        save_state(self.state)
        cache_count = sum(
            1
            for iid, item in self.items.items()
            if should_calibrate_spm(item)
            and item.get("spm_snapshot")
            and calibration_cache_matches(
                self.state.get(iid, {}).get("calibration_cache"),
                item["spm_snapshot"]["fingerprint"],
                self.spm_calibration_signature,
                getattr(self, "legacy_spm_calibration_signature", None),
            )
        )
        self.log(
            f"스캔 완료: SK SPM {len(spms)}개 · 본 세팅 캐시 {cache_count}개. "
            "먼저 [🔍 검사]로 상태를 확인해보세요."
        )

        if hasattr(self, "progress_var"):
            self.progress_var.set(
                f"스캔 완료 · SPM {len(spms)}개 · "
                f"지문 재사용 {prepared.get('snapshot_cache_hits', 0)}개"
            )
        if hasattr(self, "root") and hasattr(self.root, "after"):
            # Do not let ①→②→③ start while the first live status audit is
            # unresolved. This used to leave every existing blend painted as
            # "검증 중" even though no Blender process existed.
            self._initial_live_status_generation = generation
            if hasattr(self, "progress_var"):
                self.progress_var.set(
                    f"Blender/파이프라인 상태 확인 중 · {len(self.items)}개"
                )
            self._schedule_live_status_poll(50)
        else:
            self._set_scan_controls(False)

    @staticmethod
    def _reported_texture_paths(spm):
        report = (
            Path(spm).parent / "reports" /
            f"{Path(spm).stem}_speedtree_repair_pipeline_report_codex.json"
        )
        try:
            data = json.loads(report.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return ()
        paths = []
        for material in (data.get("texture_normalization") or {}).get(
            "materials", []
        ):
            files = (
                material.get("files")
                or material.get("preserved_files")
                or material.get("source_maps")
                or {}
            )
            for value in files.values():
                if value:
                    paths.append(str(Path(value)))
        return tuple(sorted(set(paths), key=os.path.normcase))

    @staticmethod
    def _live_status_signature(spm, texture_paths=()):
        """Cheap cross-tool change detector for PCG -> SK handoff files."""
        spm = Path(spm)
        speedtree_spm = speedtree_output_spm_for(spm)
        blend = blend_path_for(spm)
        report = (
            spm.parent / "reports" /
            f"{spm.stem}_speedtree_repair_pipeline_report_codex.json"
        )
        stmat = speedtree_stmat_path(speedtree_spm)

        def stat_key(path):
            try:
                stat = Path(path).stat()
                return stat.st_size, stat.st_mtime_ns
            except OSError:
                return None

        texture_stats = tuple(
            (os.path.normcase(str(path)), stat_key(path))
            for path in texture_paths
        )
        return (
            stat_key(spm), stat_key(speedtree_spm), stat_key(blend),
            stat_key(report), stat_key(stmat),
            texture_stats,
        )

    def _schedule_live_status_poll(self, delay_ms=10000):
        try:
            pending = getattr(self, "_live_poll_after_id", None)
            if pending is not None:
                self.root.after_cancel(pending)
            self._live_poll_after_id = self.root.after(
                delay_ms, self._poll_live_file_statuses
            )
        except (AttributeError, tk.TclError):
            self._live_poll_after_id = None

    def _poll_live_file_statuses(self):
        """Start a non-blocking cross-tool status refresh."""
        self._live_poll_after_id = None
        try:
            if self._live_poll_active:
                return
            generation = self._scan_generation
            snapshot = [
                (
                    iid,
                    item["spm"],
                    tuple(item.get("live_texture_paths") or ()),
                    item.get("live_status_signature"),
                )
                for iid, item in list(self.items.items())
            ]
            self._live_poll_active = True
            threading.Thread(
                target=self._poll_live_file_status_worker,
                args=(generation, snapshot),
                daemon=True,
            ).start()
        finally:
            self._schedule_live_status_poll(10000)

    def _poll_live_file_status_worker(self, generation, snapshot):
        changed = False
        audit_complete = True
        error_count = 0
        try:
            def inspect_row(row):
                iid, spm, texture_paths, previous_signature = row
                try:
                    signature = self._live_status_signature(spm, texture_paths)
                    if signature == previous_signature:
                        return None
                    # A changed report can point at a new set of
                    # T_/Cluster files.
                    texture_paths = self._reported_texture_paths(spm)
                    signature = self._live_status_signature(
                        spm, texture_paths
                    )
                    status = self._blend_status_text(spm)
                    push_status = self._current_push_status_text(iid, spm)
                    return (
                        iid, texture_paths, signature, status, push_status, ""
                    )
                except Exception as exc:
                    # One damaged row must not terminate the whole audit.  A
                    # None signature intentionally schedules this row again
                    # on the next poll instead of caching the failure.
                    message = f"{type(exc).__name__}: {exc}"
                    return (
                        iid,
                        texture_paths,
                        None,
                        f"확인 실패 · {message}",
                        "-",
                        message,
                    )

            rows = []
            if snapshot:
                # Four workers keep the large embedded-geometry SPMs bounded
                # in memory while gzip/regex parsing proceeds in parallel.
                with ThreadPoolExecutor(max_workers=min(4, len(snapshot))) as pool:
                    rows = [row for row in pool.map(inspect_row, snapshot) if row]
            for (
                iid, texture_paths, signature, status, push_status, row_error
            ) in rows:
                with self.state_lock:
                    if generation != self._scan_generation or iid not in self.items:
                        continue
                    item = self.items[iid]
                    item["live_texture_paths"] = texture_paths
                    item["live_status_signature"] = signature
                    state_entry = self.state.setdefault(iid, {})
                    state_entry["blend_status"] = status
                    state_entry["live_texture_paths"] = list(texture_paths)
                    state_entry["live_status_signature"] = signature
                    if row_error:
                        state_entry["live_status_error"] = row_error
                    else:
                        state_entry.pop("live_status_error", None)
                self.ui_queue.put(("cell", (iid, "blend_status", status)))
                self.ui_queue.put(("cell", (iid, "push_status", push_status)))
                if row_error:
                    audit_complete = False
                    error_count += 1
                    self.log(
                        f"[상태 확인 실패] {Path(self.items[iid]['spm']).name}: "
                        f"{row_error}"
                    )
                changed = True
            if changed:
                with self.state_lock:
                    if generation == self._scan_generation:
                        save_state(self.state)
                try:
                    save_leaf_contract_cache()
                except OSError as exc:
                    self.log(f"[캐시 경고] leaf 상태 캐시 저장 실패: {exc}")
        except Exception as exc:
            audit_complete = False
            error_count += 1
            self.log(f"[상태 확인 실패] Blender 상태 감사 중단: {exc}")
        finally:
            self._live_poll_active = False
            self.ui_queue.put(
                (
                    "live_status_done",
                    (generation, audit_complete, error_count),
                )
            )

    def _item_label(self, iid):
        item = self.items[iid]
        mark = CHECK_ON if item["checked"] else CHECK_OFF
        lock = "🔒 " if item.get("manual_bones_locked", False) else ""
        name = item.get("display_name") or item["spm"].name
        return f"{mark} {lock}{name}"

    def _redraw_checked_row(self, iid, _item):
        self.tree.item(iid, text=self._item_label(iid))

    def _bone_mode_label(self, iid):
        if self.items[iid].get("manual_bones_locked", False):
            return "수동 본 유지 🔒  ▼"
        return "자동 계산  ▼"

    def _wind_label(self, iid):
        item = self.items[iid]
        auto = wind_preset_for_spm(item["spm"])
        if item["wind_override"] == "auto":
            return f"{auto} (자동)  ▼"
        return f"{item['wind_override']} (수동)  ▼"

    def _close_cell_editor(self):
        if self.cell_editor is not None:
            try:
                self.cell_editor.destroy()
            except tk.TclError:
                pass
            self.cell_editor = None

    def _open_cell_dropdown(self, iid, column, options, current_value, callback):
        self._close_cell_editor()
        bbox = self.tree.bbox(iid, column)
        if not bbox:
            return
        x, y, width, height = bbox
        labels = [label for label, _value in options]
        value_by_label = {label: value for label, value in options}
        label_by_value = {value: label for label, value in options}
        editor = ttk.Combobox(self.tree, state="readonly", values=labels)
        editor.set(label_by_value.get(current_value, labels[0]))
        editor.place(x=x, y=y, width=width, height=height)
        self.cell_editor = editor

        def commit(_event=None):
            value = value_by_label.get(editor.get())
            self._close_cell_editor()
            if value is not None:
                callback(value)

        editor.bind("<<ComboboxSelected>>", commit)
        editor.bind("<Escape>", lambda _event: self._close_cell_editor())
        editor.focus_set()

        def post_dropdown():
            if self.cell_editor is editor and editor.winfo_exists():
                editor.tk.call("ttk::combobox::Post", editor._w)

        self.root.after_idle(post_dropdown)

    def _on_click(self, event):
        self._close_cell_editor()
        region = self.tree.identify_region(event.x, event.y)
        iid = self.tree.identify_row(event.y)
        if iid in getattr(self, "folder_rows", {}):
            self.checked_rows.set_all(False)
            self.tree.selection_set(iid)
            self.tree.focus(iid)
            self.tree.focus_set()
            return "break"
        if iid not in self.items:
            return
        if region == "tree":
            # Returning "break" suppresses Treeview's native selection, so
            # select explicitly.  Ctrl+C must copy the row the user just hit,
            # not every checked execution target or a stale prior selection.
            self.tree.selection_set(iid)
            self.tree.focus(iid)
            self.tree.focus_set()
            self.checked_rows.click(iid)
            return "break"
        if region != "cell":
            return
        if getattr(self, "active_batch_job", None) is not None:
            # Checked targets may change for a future queued request. Per-row
            # modes can write marker/state data, so freeze only those edits
            # until the current worker has completely returned.
            self.tree.selection_set(iid)
            self.tree.focus(iid)
            self.tree.focus_set()
            return "break"
        column = self.tree.identify_column(event.x)
        if column == "#1":
            current = self.items[iid].get("bone_mode", "auto")
            self.root.after_idle(
                lambda: self._open_cell_dropdown(
                    iid,
                    "bone_mode",
                    BONE_MODE_OPTIONS,
                    current,
                    lambda value: self._set_bone_mode(iid, value),
                )
            )
            return "break"
        if column == "#2":
            current = self.items[iid].get("wind_override", "auto")
            self.root.after_idle(
                lambda: self._open_cell_dropdown(
                    iid,
                    "wind",
                    WIND_OPTIONS,
                    current,
                    lambda value: self._set_wind_override(iid, value),
                )
            )
            return "break"

    def _set_all(self, checked):
        self.checked_rows.set_all(checked)

    def _set_temporary_cluster_without_assembly_push_rows(self):
        """Check only owners that have cluster sources but no Assembly output."""
        checked_count = 0
        for iid, item in self.items.items():
            checked = is_cluster_without_assembly_push_row(item["spm"])
            item["checked"] = checked
            checked_count += int(checked)
            if callable(getattr(self.tree, "item", None)):
                self._redraw_checked_row(iid, item)
        self.checked_rows.armed = False
        self.log(
            "[임시 선택] cluster는 있고 Assembly가 없는 Tree 행 "
            f"{checked_count}개만 Unreal Push 대상으로 체크"
        )
        return checked_count

    def _set_temporary_retry_blocked_rows(self):
        """Check only rows the ordinary failed-retry route cannot admit.

        ``automatic_repair_failed`` and friends are in neither retry-eligible
        kind set, so the planner classifies them fail-closed and skips them no
        matter how often the button is pressed. Selecting exactly those rows
        lets one forced rerun drain them.
        """
        blocked = set()
        with self.state_lock:
            for iid in self.items:
                entry = self.state.get(str(iid))
                if not isinstance(entry, dict):
                    continue
                kind = str(entry.get("push_status_kind") or "")
                if kind in RETRY_BLOCKED_PUSH_STATUS_KINDS:
                    blocked.add(iid)
        for iid, item in self.items.items():
            item["checked"] = iid in blocked
            if callable(getattr(self.tree, "item", None)):
                self._redraw_checked_row(iid, item)
        self.checked_rows.armed = False
        self.log(
            "[임시 선택] 일반 재시도가 받아주지 못하는 행 "
            f"{len(blocked)}개만 체크 (총 {len(self.items)}개) · "
            "'최신 .blend/캐시가 있어도 강제로 다시 실행'을 켜고 "
            "↻ 체크 항목 실패 재시도를 누르세요"
        )
        return len(blocked)

    def _set_recent_modified(self, hours=24, now_ns=None):
        """Check only rows whose authoritative SPM changed within ``hours``."""
        now_ns = time.time_ns() if now_ns is None else int(now_ns)
        cutoff_ns = now_ns - int(float(hours) * 60 * 60 * 1_000_000_000)
        checked_count = 0
        for iid, item in self.items.items():
            # A Cluster row is intentionally one-way: only its normalized
            # authoring SPM is authoritative.  Mirror/report/blend timestamps
            # must not select the row or feed changes back into its owner.
            spm = Path(item.get("authoring_spm") or item["spm"])
            try:
                checked = spm.is_file() and spm.stat().st_mtime_ns >= cutoff_ns
            except OSError:
                checked = False
            item["checked"] = bool(checked)
            checked_count += int(checked)
            self._redraw_checked_row(iid, item)
        self.checked_rows.armed = False
        message = (
            f"최근 {hours:g}시간 수정 SPM {checked_count}개 체크 "
            f"(총 {len(self.items)}개)"
        )
        self.progress_var.set(message)
        self.log(f"[선택] {message} · 작업은 실행하지 않음")

    def _check_recent_24h(self):
        self._set_recent_modified(hours=24)

    def copy_selected_paths(self, _event=None):
        if not hasattr(self, "row_copy_paths"):
            self.row_copy_paths = {
                iid: [item.get("spm")]
                for iid, item in self.items.items()
                if item.get("spm")
            }
        count = copy_selected_row_paths(
            self.root,
            self.tree,
            self.row_copy_paths,
            lambda paths: paths,
        )
        if count:
            self.progress_var.set(f"경로 복사 완료 · {count}개")
        else:
            self.progress_var.set("복사할 폴더 또는 SPM 행을 먼저 클릭하세요")
        return "break"

    def _start_shared_setting_job(
            self, label, payload, work, on_success):
        """Queue a short marker/state mutation with the long batch jobs."""

        runtime = getattr(self, "shared_queue_runtime", None)
        if runtime is None:
            on_success(work())
            return
        try:
            record = runtime.enqueue(
                str(label),
                {"tool": "sk_batch", **dict(payload or {})},
            )
        except Exception as exc:
            messagebox.showerror(
                "공용 대기열 등록 실패",
                (
                    "다른 창과 동시에 변경되지 않도록 설정을 적용하지 "
                    f"않았습니다.\n\n{exc}"
                ),
                parent=self.root,
            )
            return
        self.progress_var.set(
            f"공용 대기열 등록 · {label} · #{record['sequence']}"
        )

        def run():
            lease = None
            try:
                lease = runtime.wait_for_turn(
                    record["id"],
                    on_wait=lambda state: self.ui_queue.put((
                        "progress",
                        (
                            f"공용 대기열 대기 · 전체 "
                            f"{state.get('position') or '?'}번째"
                        ),
                    )),
                )
                result = work()
                lease.finish(
                    success=True,
                    result={
                        "tool": "sk_batch",
                        "label": str(label),
                        "outcome": "completed",
                    },
                )
                self.root.after(
                    0,
                    lambda value=result: on_success(value),
                )
            except WaitCancelled:
                return
            except Exception as exc:
                if lease is not None and not lease.finished:
                    try:
                        lease.finish(
                            success=False,
                            result={
                                "tool": "sk_batch",
                                "label": str(label),
                                "outcome": "failed",
                                "error": str(exc),
                            },
                        )
                    except Exception:
                        pass
                self.root.after(
                    0,
                    lambda error=exc: messagebox.showerror(
                        "설정 적용 실패",
                        str(error),
                        parent=self.root,
                    ),
                )

        threading.Thread(target=run, daemon=True).start()

    def _set_bone_mode(self, iid, mode):
        if iid not in self.items or mode not in {"auto", "manual"}:
            return
        item = self.items[iid]
        locked = mode == "manual"
        spm = Path(item["spm"])

        def apply():
            marker = set_manual_bones_marker(spm, locked)
            state_lock = getattr(
                self, "state_lock", threading.RLock()
            )
            with state_lock:
                entry = self.state.setdefault(iid, {})
                if locked:
                    entry["manual_bones_locked"] = True
                    entry["spm_status"] = manual_bone_status_text(spm)
                else:
                    entry.pop("manual_bones_locked", None)
                    entry["spm_status"] = "자동 계산 대상"
                    entry.pop("spm_summary", None)
                save_state(self.state)
                status = entry["spm_status"]
            return marker, status

        def done(result):
            marker, status = result
            current = self.items.get(iid)
            if current is None:
                return
            current["bone_mode"] = mode
            current["manual_bones_locked"] = locked
            self.tree.item(iid, text=self._item_label(iid))
            self.tree.set(iid, "bone_mode", self._bone_mode_label(iid))
            self._set_table_cell(iid, "spm_status", status)
            self.log(
                f"{'수동 본 유지 설정' if locked else '자동 계산 복귀'}: "
                f"{spm.name} ({marker})"
            )

        self._start_shared_setting_job(
            f"본 모드 · {spm.name}",
            {
                "operation": "bone_mode",
                "spm": str(spm),
                "mode": mode,
            },
            apply,
            done,
        )

    def _set_wind_override(self, iid, value):
        valid_values = {option_value for _label, option_value in WIND_OPTIONS}
        if iid not in self.items or value not in valid_values:
            return
        item = self.items[iid]
        spm = Path(item["spm"])

        def apply():
            state_lock = getattr(
                self, "state_lock", threading.RLock()
            )
            with state_lock:
                self.state.setdefault(iid, {})["wind_override"] = value
                save_state(self.state)
            return value

        def done(applied):
            current = self.items.get(iid)
            if current is None:
                return
            current["wind_override"] = applied
            self.tree.set(iid, "wind", self._wind_label(iid))
            self.log(
                f"Wind 설정: {spm.name} → "
                f"{self._wind_label(iid).replace('  ▼', '')}"
            )

        self._start_shared_setting_job(
            f"Wind 설정 · {spm.name}",
            {
                "operation": "wind_override",
                "spm": str(spm),
                "value": value,
            },
            apply,
            done,
        )

    # ------------------------------------------------------------------ batch
    def _collect_cfg(self):
        cfg = dict(self.cfg)
        try:
            cfg["priority"] = self.priority_var.get()
            cfg["cpu_cores"] = int(self.cores_var.get())
            cfg["target_bones_per_branch"] = float(self.target_var.get())
            cfg["max_total_bones"] = int(self.maxtotal_var.get())
            cfg["spm_parallel_jobs"] = max(1, int(self.parallel_var.get()))
            cfg["blender_parallel_jobs"] = max(
                1, min(4, int(self.blender_parallel_var.get()))
            )
            transport = self.transport_var.get()
            cfg["push_transport"] = transport if transport in {"rpc", "headless"} else "rpc"
            cfg["night_headless"] = bool(self.night_headless_var.get())
        except (AttributeError, tk.TclError):
            pass
        return cfg

    @staticmethod
    def _snapshot_still_current(spm, snapshot):
        if not snapshot:
            return False
        try:
            stat = Path(spm).stat()
        except OSError:
            return False
        return (
            snapshot.get("size") == stat.st_size
            and snapshot.get("mtime_ns") == stat.st_mtime_ns
        )

    def _current_spm_snapshot(self, item):
        snapshot = item.get("spm_snapshot")
        if not self._snapshot_still_current(item["spm"], snapshot):
            snapshot = file_content_snapshot(item["spm"])
            item["spm_snapshot"] = snapshot
            with self.state_lock:
                self.state.setdefault(str(item["spm"]), {})[
                    "spm_snapshot_cache"
                ] = snapshot
        return snapshot

    def _spm_cache_matches(self, item):
        snapshot = self._current_spm_snapshot(item)
        entry = self.state.get(str(item["spm"]), {})
        return calibration_cache_matches(
            entry.get("calibration_cache"),
            snapshot["fingerprint"],
            self.spm_calibration_signature,
            getattr(self, "legacy_spm_calibration_signature", None),
        )

    def _spm_schedule_key(self, item):
        if not should_calibrate_spm(item):
            return (0, 0.0, item["spm"].name.lower())
        if item.get("manual_bones_locked", False):
            return (0, 0.0, item["spm"].name.lower())
        try:
            cache_matches = (
                not self.force_rerun
                and self._spm_cache_matches(item)
            )
        except OSError:
            # A row can become stale after the table scan (deleted, renamed,
            # disconnected drive, and so on).  Scheduling must remain total:
            # let the normal per-item worker record that row's data error
            # instead of aborting the entire queued batch during sorting.
            cache_matches = False
        if cache_matches:
            return (1, 0.0, item["spm"].name.lower())
        entry = self.state.get(str(item["spm"]), {})
        duration = float(entry.get("spm_last_duration_seconds", 0.0) or 0.0)
        # Longest predicted jobs start first after all zero-cost work has been
        # resolved, which avoids a single slow tree becoming the final tail.
        return (2, -duration, item["spm"].name.lower())

    @staticmethod
    def _snapshot_batch_item(item):
        """Freeze nested scan/report containers at click time."""

        def freeze(value):
            if isinstance(value, dict):
                return {
                    key: freeze(child)
                    for key, child in value.items()
                }
            if isinstance(value, list):
                return [freeze(child) for child in value]
            if isinstance(value, set):
                return {freeze(child) for child in value}
            if isinstance(value, tuple):
                return tuple(freeze(child) for child in value)
            return value

        return freeze(item)

    def _ensure_batch_queue_state(self):
        if not hasattr(self, "pending_batch_jobs"):
            self.pending_batch_jobs = deque()
        if not hasattr(self, "active_batch_job"):
            self.active_batch_job = None
        if not hasattr(self, "batch_job_sequence"):
            self.batch_job_sequence = 0
        if not hasattr(self, "batch_job_failures"):
            self.batch_job_failures = []
        if not hasattr(self, "_recovery_commit_lock"):
            self._recovery_commit_lock = threading.RLock()
        if not hasattr(self, "_recovery_resume_commit"):
            self._recovery_resume_commit = None
        if not hasattr(self, "_stale_node_table_modeler_session"):
            self._stale_node_table_modeler_session = None

    def _snapshot_batch_request(self, target_iids):
        inventory = {
            iid: self._snapshot_batch_item(item)
            for iid, item in self.items.items()
            if is_live_spm(item.get("spm"), require_file=False)
        }
        targets = [
            inventory[iid] for iid in target_iids if iid in inventory
        ]
        return inventory, targets

    def _batch_job_inventory(self):
        active = getattr(self, "_active_batch_inventory", None)
        return (
            active
            if active is not None
            else getattr(self, "items", {})
        )

    def _batch_job_item(self, iid):
        active = getattr(self, "_active_batch_items", None)
        if active is not None and iid in active:
            return active[iid]
        return getattr(self, "items", {})[iid]

    def _set_batch_queue_controls(self, active):
        # Action and selection controls stay enabled so another request can be
        # captured and queued. Root replacement/rescan would invalidate the
        # visible inventory, so only those controls are frozen.
        for name in (
            "btn_check", "btn_spm", "btn_blender", "btn_push",
            "btn_retry_failed", "btn_retry_checked", "btn_all",
            "btn_select_all", "btn_clear_all", "btn_recent_24h",
        ):
            button = getattr(self, name, None)
            if button is not None:
                button.configure(state="normal")
        for name in ("btn_pick_root", "btn_scan"):
            button = getattr(self, name, None)
            if button is not None:
                button.configure(state="disabled" if active else "normal")
        root_entry = getattr(self, "root_entry", None)
        if root_entry is not None:
            root_entry.configure(state="disabled" if active else "normal")
        stop = getattr(self, "btn_stop", None)
        if stop is not None:
            stop.configure(state="normal" if active else "disabled")

    def _enqueue_batch_job(self, job):
        self._ensure_batch_queue_state()
        if (
            self.active_batch_job is None
            and not self.pending_batch_jobs
        ):
            self.batch_job_failures = []
            # One queue drain owns one validated live-audit memo generation.
            # Every lookup still re-fingerprints the production inputs.
            self._reset_cluster_receipt_refresh_memo()
        self.batch_job_sequence += 1
        job = dict(job)
        job["id"] = self.batch_job_sequence
        shared_runtime = getattr(self, "shared_queue_runtime", None)
        if shared_runtime is not None:
            try:
                shared = shared_runtime.enqueue(
                    str(job["label"]),
                    {
                        "tool": "sk_batch",
                        "local_job_id": job["id"],
                        "label": str(job["label"]),
                        "mode": str(job.get("mode") or ""),
                        "phase": str(job.get("phase") or ""),
                        "retry": copy.deepcopy(
                            job.get("retry_metadata") or {}
                        ),
                        "target_spms": [
                            str(item.get("spm") or "")
                            for item in job.get("targets") or []
                        ],
                    },
                )
            except Exception as exc:
                messagebox.showerror(
                    "공용 대기열 등록 실패",
                    (
                        "다른 창과 동시에 실행되지 않도록 작업을 시작하지 "
                        f"않았습니다.\n\n{exc}"
                    ),
                    parent=self.root,
                )
                self.progress_var.set(
                    "공용 대기열 등록 실패 · 실행하지 않음"
                )
                return None
            job["shared_queue_job_id"] = shared["id"]
            job["shared_queue_sequence"] = shared["sequence"]
            tracker = job.get("_retry_progress_tracker")
            partition = str(
                (job.get("retry_metadata") or {}).get("partition") or ""
            )
            if tracker is not None and partition:
                tracker.register_queue_job(
                    partition,
                    shared["id"],
                    shared["sequence"],
                    local_job_id=job["id"],
                )
        self.pending_batch_jobs.append(job)
        if self.active_batch_job is not None:
            pending = len(self.pending_batch_jobs)
            self.log(
                f"[대기열 #{job['id']}] {job['label']} 등록 · "
                f"앞 대기 {pending - 1}개"
            )
        self._set_batch_queue_controls(True)
        self._start_next_batch_job()
        return job["id"]

    def _start_next_batch_job(self):
        self._ensure_batch_queue_state()
        if self.active_batch_job is not None or not self.pending_batch_jobs:
            return
        job = self.pending_batch_jobs.popleft()
        self.active_batch_job = job
        self.cfg = dict(job["cfg"])
        self.spm_calibration_signature = calibration_settings_signature(
            self.cfg
        )
        self.legacy_spm_calibration_signature = (
            legacy_calibration_settings_signature(self.cfg)
        )
        self.force_rerun = job["force_rerun"]
        self.active_push_transport = job["push_transport"]
        self._active_retry_metadata = copy.deepcopy(
            job.get("retry_metadata") or {}
        )
        self._inline_atlas_repair_results = {}
        self._registered_relation_repair_results = {}
        self._active_batch_inventory = job["inventory"]
        self._active_batch_items = job["inventory"]
        self._ensure_cluster_receipt_refresh_memo()
        self.stop_flag.clear()
        with self._recovery_commit_lock:
            self._recovery_resume_commit = None
        self.batch_progress.configure(value=0)
        pending = len(self.pending_batch_jobs)
        suffix = f" · 대기 {pending}개" if pending else ""
        self.batch_progress_var.set(
            f"0/{len(job['targets'])} (0%){suffix}"
        )
        self._set_batch_queue_controls(True)
        self.log(f"[대기열 #{job['id']}] 시작 · {job['label']}")
        self.worker = threading.Thread(
            target=self._run_queued_batch_job,
            args=(job,),
            daemon=True,
        )
        self.worker.start()

    def _freeze_batch_production_source_manifest(self):
        gate_result = run_code_compile_gate(
            REPO_DIR,
            TOOL_DIR / "sk_batch_gui.pyw",
        )
        manifest = gate_result.production_source_manifest
        try:
            validate_production_source_manifest(
                _PROCESS_PRODUCTION_SOURCE_MANIFEST,
                manifest,
                label="Batch-start production source",
            )
        except CompileGateError as exc:
            raise BatchItemError(
                "Production sources changed after the GUI process loaded; "
                "restart SK Batch before running another batch: " + str(exc),
                kind="internal_error",
            ) from exc
        self._active_production_source_manifest = manifest
        self.log(
            "Production source revision 고정: "
            f"{manifest.content_hash} · {manifest.source_count} files"
        )
        return manifest

    def _assert_active_production_source_manifest(self):
        expected = getattr(
            self,
            "_active_production_source_manifest",
            None,
        )
        if expected is None:
            if getattr(self, "active_batch_job", None) is not None:
                raise BatchItemError(
                    "Active batch has no pinned production source manifest",
                    kind="internal_error",
                )
            expected = _PROCESS_PRODUCTION_SOURCE_MANIFEST
        try:
            current = production_source_manifest(REPO_DIR)
            validate_production_source_manifest(
                expected,
                current,
                label="Parent production source",
            )
        except CompileGateError as exc:
            raise BatchItemError(
                "Production source revision changed during the active batch: "
                + str(exc),
                kind="internal_error",
            ) from exc
        return expected

    @staticmethod
    def _require_child_production_source_manifest(
        payload,
        expected_manifest,
        *,
        report_file,
        log_file,
    ):
        try:
            return validate_production_source_revision_report(
                payload,
                expected_manifest,
            )
        except CompileGateError as exc:
            raise BatchItemError(
                "Cluster Assembly live audit worker revision mismatch: "
                + str(exc),
                kind="internal_error",
                report=payload if isinstance(payload, dict) else None,
                log_file=log_file,
                report_file=report_file,
            ) from exc

    def _run_queued_batch_job(self, job):
        error = None
        status = "completed"
        failed_count = 0
        summary = {
            "selected_count": len(job.get("targets") or ()),
            "completed_count": 0,
            "pending_count": 0,
            "cancelled_count": 0,
            "blocked_count": 0,
            "owner_lost_count": 0,
            "planned_excluded_count": 0,
            "dependency_blocked_count": 0,
            "failed_count": 0,
            "target_outcomes": [],
            "shared_failures": [],
        }
        lease = None
        tracker = self._retry_tracker_for_job(job)
        retry_partition = str(
            (job.get("retry_metadata") or {}).get("partition") or ""
        )
        try:
            shared_job_id = job.get("shared_queue_job_id")
            shared_runtime = getattr(
                self, "shared_queue_runtime", None
            )
            if shared_job_id and shared_runtime is not None:
                def report_wait(wait_state):
                    position = wait_state.get("position")
                    queued = wait_state.get("queued_count", 0)
                    text = (
                        "공용 대기열 대기"
                        + (
                            f" · 전체 {position}번째"
                            if position else ""
                        )
                        + f" · 대기 {queued}개"
                    )
                    self.ui_queue.put(("progress", text))
                    if tracker is not None and retry_partition:
                        tracker.queue_wait(
                            retry_partition,
                            position=position,
                            queued_count=queued,
                            running_head=wait_state.get("running_head"),
                        )

                lease = shared_runtime.wait_for_turn(
                    shared_job_id,
                    on_wait=report_wait,
                    cancel_event=self.stop_flag,
                )
                self._active_shared_queue_lease = lease
                if tracker is not None and retry_partition:
                    tracker.claimed(retry_partition, lease.record)
                self.ui_queue.put((
                    "progress",
                    "공용 대기열 진입 · 단독 실행",
                ))
            elif tracker is not None and retry_partition:
                tracker.claimed(retry_partition)
            self.__dict__.pop("_phase_result_summary", None)
            self._freeze_batch_production_source_manifest()
            if job["mode"] == "pipeline":
                completed = self._run_full_pipeline(
                    job["targets"],
                    terminal_phase=job["terminal_phase"],
                    selected_scope=job["selected_scope"],
                    emit_done=False,
                )
            elif job["mode"] == "unreal_recovery":
                completed = self._run_failed_unreal_recovery(
                    job["targets"],
                    job.get("recovery_requests") or [],
                    emit_done=False,
                )
            elif job["mode"] == "failed_retry_repair":
                if lease is None:
                    raise RuntimeError(
                        "automatic exact repair requires a current shared queue lease"
                    )
                completed = self._run_failed_retry_repair_job(job, lease)
            else:
                completed = self._run_batch(
                    job["phase"], job["targets"], emit_done=False
                )
            authoritative_summary = getattr(
                self,
                "_phase_result_summary",
                None,
            )
            summary = copy.deepcopy(
                authoritative_summary
                or self._summarize_phase_targets(
                    job["targets"],
                    phase=(
                        job.get("terminal_phase")
                        if job.get("mode") == "pipeline"
                        else "push"
                        if job.get("mode") == "unreal_recovery"
                        else job.get("phase")
                    ),
                )
            )
            failed_count = int(summary["failed_count"])
            blocked_count = int(summary["blocked_count"])
            owner_lost_count = int(summary.get("owner_lost_count", 0) or 0)
            pending_count = int(summary.get("pending_count", 0) or 0)
            cancelled_count = int(summary.get("cancelled_count", 0) or 0)
            completed_count = int(summary.get("completed_count", 0) or 0)
            selected_count = int(summary.get("selected_count", 0) or 0)
            actual_problem_count = (
                failed_count + blocked_count + owner_lost_count
            )
            if (
                selected_count
                and completed_count == selected_count
                and (
                    not self.stop_flag.is_set()
                    or authoritative_summary is not None
                )
            ):
                status = "completed"
            elif actual_problem_count:
                status = "partial"
            elif cancelled_count or self.stop_flag.is_set():
                status = "stopped"
            elif pending_count:
                status = "waiting"
            elif completed is False:
                status = "failed"
            if status == "partial":
                tokens = sorted({
                    str(row.get("reason_token"))
                    for row in summary["target_outcomes"]
                    if row.get("reason_token")
                    and row.get("outcome") in {
                        "failed",
                        "blocked",
                        "planned_excluded",
                        "owner_lost",
                    }
                })
                error = (
                    f"completed={summary['completed_count']} "
                    f"blocked={blocked_count} failed={failed_count} "
                    f"owner_lost={owner_lost_count}"
                    + (f" | reasons={','.join(tokens)}" if tokens else "")
                )
            elif status == "failed":
                error = compact_error_message(
                    getattr(self, "_phase_abort_reason", None)
                    or "작업이 완료되지 않음"
                )
        except WaitCancelled:
            error = "공용 대기열 대기 중 취소됨"
            status = "stopped"
        except Exception as exc:
            error = compact_error_message(exc)
            status = "failed"
            self.log(
                f"[대기열 #{job['id']}] 예외 종료 · "
                f"{job['label']}: {error}"
            )
        finally:
            if lease is not None and not lease.finished:
                try:
                    finish_options = {
                        "success": status in {"completed", "waiting"},
                        "result": {
                            "tool": "sk_batch",
                            "local_job_id": job["id"],
                            "outcome": status,
                            "error": error,
                            "retry": copy.deepcopy(
                                job.get("retry_metadata") or {}
                            ),
                            **summary,
                        },
                    }
                    if status == "stopped":
                        finish_options["terminal_status"] = "cancelled"
                    lease.finish(**finish_options)
                except Exception as queue_exc:
                    error = compact_error_message(queue_exc)
                    status = "failed"
                    if tracker is not None and retry_partition:
                        reconciled = tracker.reconcile_queue(
                            getattr(self, "shared_queue_runtime", None).queue
                        )
                        if not reconciled and lease.heartbeat_error is not None:
                            tracker.mark_partition_terminal(
                                retry_partition,
                                RETRY_STAGE_OWNER_LOST,
                                "shared queue lease lost before receipt finalization",
                            )
                    self.log(
                        f"[대기열 #{job['id']}] 공용 대기열 종료 기록 실패 · "
                        f"{job['label']}: {error}"
                    )
            if tracker is not None:
                self._finalize_retry_progress_for_job(
                    job,
                    tracker,
                    status,
                    summary,
                    error,
                )
            self.__dict__.pop("_active_shared_queue_lease", None)
            self.ui_queue.put((
                "batch_job_done",
                {
                    "id": job["id"],
                    "error": error,
                    "status": status,
                    "retry": copy.deepcopy(
                        job.get("retry_metadata") or {}
                    ),
                    **summary,
                },
            ))

    def _finalize_retry_progress_for_job(
        self,
        job,
        tracker,
        status,
        summary,
        error,
    ):
        """Seal only this partition while preserving prior target terminals."""
        metadata = job.get("retry_metadata") or {}
        partition = str(metadata.get("partition") or "")
        selected_ids = [
            str(value) for value in metadata.get("selected_queue_ids") or []
        ]
        outcomes = {
            str(row.get("target")): row
            for row in (summary or {}).get("target_outcomes") or []
            if isinstance(row, dict) and row.get("target")
        }
        for target_id in selected_ids:
            row = outcomes.get(target_id)
            outcome = str((row or {}).get("outcome") or "")
            reason = str(
                (row or {}).get("reason_token")
                or error
                or outcome
                or status
            )
            if outcome == "completed" or (
                not outcome and status == "completed"
            ):
                tracker.transition(
                    target_id,
                    RETRY_STAGE_POST_CHECK,
                    diagnostic="post-check complete",
                    progress=True,
                    heartbeat=True,
                )
                tracker.transition(
                    target_id,
                    RETRY_STAGE_COMPLETE,
                    diagnostic="retry target complete",
                    terminal_reason="completed",
                    outcome=RETRY_STAGE_COMPLETE,
                )
            elif outcome in {
                "pending_unreal",
                "exported_pending_unreal",
            } or (not outcome and status == "waiting"):
                tracker.transition(
                    target_id,
                    RETRY_STAGE_PENDING_UNREAL,
                    diagnostic=reason or "exported; Unreal pending",
                    outcome=RETRY_STAGE_PENDING_UNREAL,
                )
            elif outcome in {"cancelled", "stopped"} or (
                not outcome and status == "stopped"
            ):
                tracker.transition(
                    target_id,
                    RETRY_STAGE_CANCELLED,
                    diagnostic=reason,
                    terminal_reason="operator_cancelled",
                    outcome=RETRY_STAGE_CANCELLED,
                )
            elif outcome == "owner_lost":
                tracker.transition(
                    target_id,
                    RETRY_STAGE_OWNER_LOST,
                    diagnostic=reason,
                    terminal_reason="owner_lost",
                    outcome=RETRY_STAGE_OWNER_LOST,
                )
            elif outcome in {"blocked", "planned_excluded"}:
                tracker.transition(
                    target_id,
                    RETRY_STAGE_BLOCKED,
                    diagnostic=reason,
                    terminal_reason=reason,
                    outcome=RETRY_STAGE_BLOCKED,
                )
            else:
                tracker.transition(
                    target_id,
                    RETRY_STAGE_FAILED,
                    diagnostic=reason,
                    terminal_reason=reason,
                    outcome=RETRY_STAGE_FAILED,
                )
        tracker.finalize()

    def _finish_batch_job(self, payload):
        self._ensure_batch_queue_state()
        job = self.active_batch_job
        if job is None or payload.get("id") != job.get("id"):
            return
        error = payload.get("error")
        status = payload.get("status")
        if status is None:
            status = "failed" if error else "completed"
        if status in {"failed", "partial"}:
            self.batch_job_failures.append(
                {
                    "id": job["id"],
                    "label": job["label"],
                    "error": error or status,
                    "status": status,
                    "completed_count": int(
                        payload.get("completed_count", 0) or 0
                    ),
                    "pending_count": int(
                        payload.get("pending_count", 0) or 0
                    ),
                    "cancelled_count": int(
                        payload.get("cancelled_count", 0) or 0
                    ),
                    "blocked_count": int(
                        payload.get("blocked_count", 0) or 0
                    ),
                    "owner_lost_count": int(
                        payload.get("owner_lost_count", 0) or 0
                    ),
                    "planned_excluded_count": int(
                        payload.get("planned_excluded_count", 0) or 0
                    ),
                    "dependency_blocked_count": int(
                        payload.get("dependency_blocked_count", 0) or 0
                    ),
                    "failed_count": int(
                        payload.get("failed_count", 0) or 0
                    ),
                    "target_outcomes": copy.deepcopy(
                        payload.get("target_outcomes") or []
                    ),
                    "shared_failures": copy.deepcopy(
                        payload.get("shared_failures") or []
                    ),
                }
            )
        outcome_text = {
            "completed": "완료",
            "partial": "실패/준비 제외 기록 후 다음 작업 계속",
            "failed": "실패 기록 후 다음 작업 계속",
            "waiting": "Unreal 대기 상태 기록",
            "stopped": "중지",
        }.get(status, str(status))
        self.log(
            f"[대기열 #{job['id']}] {outcome_text} · {job['label']}"
        )
        self.active_batch_job = None
        self.worker = None
        for key in (
            "_active_batch_inventory",
            "_active_batch_items",
            "_active_production_source_manifest",
            "_active_retry_metadata",
            "_active_blender_dependency_map",
            "_active_pipeline_terminal_phase",
            "_active_repair_stage_contracts",
            "_pipeline_upstream_failed_items",
            "_active_push_dependency_map",
            "_active_push_auto_added_ids",
            "_headless_progress_label",
            "_retry_checkpoint_versions",
            "_retry_checkpoint_output_lines",
            "_phase_failed_items",
            "_phase_result_summary",
            "_inline_atlas_repair_results",
        ):
            self.__dict__.pop(key, None)
        if self.pending_batch_jobs:
            self._start_next_batch_job()
            return
        self._reset_cluster_receipt_refresh_memo()
        self._set_batch_queue_controls(False)
        failure_count = len(self.batch_job_failures)
        if status == "stopped":
            self.progress_var.set(
                "대기열 중지됨 · cancelled "
                f"{int(payload.get('cancelled_count', 0) or 0)}"
            )
        elif status == "waiting":
            self.progress_var.set(
                "대기열 완료 · Unreal 대기 "
                f"{int(payload.get('pending_count', 0) or 0)}"
            )
        elif failure_count:
            tokens = sorted({
                str(row.get("reason_token"))
                for failure in self.batch_job_failures
                for row in failure.get("target_outcomes") or ()
                if row.get("reason_token")
                and row.get("outcome") in {
                    "failed",
                    "blocked",
                    "planned_excluded",
                    "owner_lost",
                }
            })
            completed_total = sum(
                row.get("completed_count", 0)
                for row in self.batch_job_failures
            )
            blocked_total = sum(
                row.get("blocked_count", 0)
                for row in self.batch_job_failures
            )
            failed_total = sum(
                row.get("failed_count", 0)
                for row in self.batch_job_failures
            )
            owner_lost_total = sum(
                row.get("owner_lost_count", 0)
                for row in self.batch_job_failures
            )
            self.progress_var.set(
                "대기열 완료 · "
                f"completed {completed_total} · "
                f"blocked {blocked_total} · owner_lost {owner_lost_total} · "
                f"failed {failed_total}"
                + (f" · {', '.join(tokens)}" if tokens else "")
            )
        else:
            self.progress_var.set("대기열 완료")

    def start_batch(self, phase):
        self._close_cell_editor()
        target_iids = [
            iid for iid, item in self.items.items() if item["checked"]
        ]
        if not target_iids:
            messagebox.showinfo("SK Batch", "선택된 항목이 없습니다.")
            return
        cfg = dict(self._collect_cfg())
        if phase != "check":
            save_config(cfg)
        inventory, targets = self._snapshot_batch_request(target_iids)
        phase_labels = {
            "check": "검사",
            "spm": "① SPM 본 세팅",
            "blender": "② Blender Repair 연계 ①→②",
            "push": "③ Unreal Push 연계 ①→②→③",
        }
        job = {
            "label": f"{phase_labels[phase]} · 선택 {len(targets)}개",
            "mode": (
                "pipeline" if phase in {"blender", "push"} else "phase"
            ),
            "phase": phase,
            "terminal_phase": phase,
            "selected_scope": phase in {"blender", "push"},
            "targets": targets,
            "inventory": inventory,
            "cfg": cfg,
            "force_rerun": bool(self.force_var.get()),
            "push_transport": cfg.get("push_transport", "rpc"),
        }
        self._enqueue_batch_job(job)

    def _failed_retry_planning_context(self):
        local = getattr(self, "_retry_thread_context", None)
        context = getattr(local, "planning_context", None)
        return context if isinstance(context, RetryPlanningContext) else None

    def _failed_retry_state_entry(self, iid):
        context = self._failed_retry_planning_context()
        if context is not None:
            return context.entry(iid)
        with self.state_lock:
            value = self.state.get(str(iid), {})
            return copy.deepcopy(value if isinstance(value, dict) else {})

    @staticmethod
    def _canonical_receipt_sha256(value):
        return hashlib.sha256(json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")).hexdigest()

    def _failure_record_provenance(self, iid):
        """Bind one durable failure to target bytes and running code."""

        target = Path(iid).expanduser().absolute()
        source_identity = self._artifact_path_identity(target)
        manifest = getattr(
            self,
            "_active_production_source_manifest",
            None,
        ) or _PROCESS_PRODUCTION_SOURCE_MANIFEST
        return {
            "schema_version": 1,
            "target_source": source_identity,
            "production_source_revision": str(manifest.content_hash),
        }

    def _bind_failure_record(self, iid, kind, reason, details=None):
        provenance = self._failure_record_provenance(iid)
        dependency_artifacts = (
            (details or {}).get("dependency_artifacts")
            if isinstance(details, dict)
            else None
        )
        if isinstance(dependency_artifacts, dict):
            provenance["dependency_artifacts"] = copy.deepcopy(
                dependency_artifacts
            )
        evidence = {
            "kind": str(kind),
            "message": str(reason),
            "details": copy.deepcopy(details or {}),
        }
        return {
            "failure_provenance": provenance,
            "production_source_revision": provenance[
                "production_source_revision"
            ],
            "provenance_sha256": self._canonical_receipt_sha256(provenance),
            "evidence_sha256": self._canonical_receipt_sha256(evidence),
        }

    @staticmethod
    def _content_identity_matches(recorded, current):
        if not isinstance(recorded, dict) or not isinstance(current, dict):
            return False
        if not isinstance(recorded.get("exists"), bool) or not isinstance(
            current.get("exists"), bool
        ):
            return False
        if bool(recorded.get("exists")) != bool(current.get("exists")):
            return False
        try:
            same_path = _normalized_path(recorded.get("path") or "") == (
                _normalized_path(current.get("path") or "")
            )
        except (OSError, ValueError):
            return False
        if not same_path:
            return False
        if recorded.get("exists") is not True:
            return True
        return bool(
            str(recorded.get("fingerprint") or "").casefold()
            == str(current.get("fingerprint") or "").casefold()
            and recorded.get("fingerprint")
            and str(recorded.get("fingerprint_algorithm") or "")
            == str(current.get("fingerprint_algorithm") or "")
            and int(recorded.get("size", -1))
            == int(current.get("size", -2))
        )

    def _failure_record_freshness(self, iid, error):
        """Cheaply decide whether a saved failure still describes reality."""

        if not isinstance(error, dict):
            return {
                "status": "invalid",
                "reason": "저장된 실패 행에 구조화된 오류가 없습니다.",
            }
        provenance = error.get("failure_provenance")
        if not isinstance(provenance, dict):
            return {
                "status": "invalid",
                "reason": "과거 실패 행에 target content key가 없어 폐기했습니다.",
            }
        if error.get("provenance_sha256") != self._canonical_receipt_sha256(
            provenance
        ):
            return {
                "status": "invalid",
                "reason": "과거 실패 행의 provenance checksum이 일치하지 않습니다.",
            }
        recorded_revision = str(
            provenance.get("production_source_revision") or ""
        )
        if str(error.get("production_source_revision") or "") != (
            recorded_revision
        ):
            return {
                "status": "invalid",
                "reason": "과거 실패 행의 direct source revision이 일치하지 않습니다.",
            }
        current_revision = str(
            (
                getattr(self, "_active_production_source_manifest", None)
                or _PROCESS_PRODUCTION_SOURCE_MANIFEST
            ).content_hash
        )
        if not recorded_revision or recorded_revision != current_revision:
            return {
                "status": "invalid",
                "reason": (
                    "과거 실패 행을 만든 production source revision이 "
                    "현재 코드와 달라 폐기했습니다."
                ),
                "recorded_revision": recorded_revision,
                "current_revision": current_revision,
            }
        recorded_source = provenance.get("target_source")
        try:
            current = self._failure_record_provenance(iid)["target_source"]
        except (OSError, ValueError) as exc:
            return {
                "status": "invalid",
                "reason": f"target content key를 다시 확인할 수 없습니다: {exc}",
            }
        if not self._content_identity_matches(recorded_source, current):
            return {
                "status": "invalid",
                "reason": "target content key가 과거 실패 행 이후 변경되었습니다.",
                "recorded_source": copy.deepcopy(recorded_source),
                "current_source": current,
            }
        dependency_artifacts = provenance.get("dependency_artifacts")
        if isinstance(dependency_artifacts, dict):
            for dependency, artifact in dependency_artifacts.items():
                phase = str(
                    (artifact or {}).get("phase")
                    if isinstance(artifact, dict)
                    else ""
                )
                identities = (
                    (artifact or {}).get("artifact_identity")
                    if isinstance(artifact, dict)
                    else None
                )
                if not isinstance(identities, dict) or not identities:
                    return {
                        "status": "invalid",
                        "reason": (
                            "과거 dependency 행에 producer artifact "
                            f"content key가 없습니다: {Path(dependency).name}"
                        ),
                    }
                if phase not in {"blender", "push"}:
                    return {
                        "status": "invalid",
                        "reason": (
                            "과거 dependency 행에 producer artifact phase가 "
                            f"없습니다: {Path(dependency).name}"
                        ),
                    }
                current_verdict = self._dependency_artifact_verdict(
                    dependency,
                    phase=phase,
                )
                if str(current_verdict.get("status") or "") != str(
                    artifact.get("status") or ""
                ):
                    return {
                        "status": "invalid",
                        "reason": (
                            "producer artifact 상태가 과거 dependency 행 "
                            f"이후 변경되었습니다: {Path(dependency).name}"
                        ),
                        "recorded_status": str(
                            artifact.get("status") or ""
                        ),
                        "current_status": str(
                            current_verdict.get("status") or ""
                        ),
                    }
                for recorded_identity in identities.values():
                    if not isinstance(recorded_identity, dict):
                        return {
                            "status": "invalid",
                            "reason": "과거 producer artifact identity가 손상되었습니다.",
                        }
                    current_identity = self._artifact_path_identity(
                        recorded_identity.get("path") or ""
                    )
                    if not self._content_identity_matches(
                        recorded_identity,
                        current_identity,
                    ):
                        return {
                            "status": "invalid",
                            "reason": (
                                "producer artifact content key가 과거 "
                                "dependency 행 이후 변경되었습니다: "
                                f"{Path(dependency).name}"
                            ),
                            "recorded_artifact": copy.deepcopy(
                                recorded_identity
                            ),
                            "current_artifact": current_identity,
                        }
        evidence = {
            "kind": str(error.get("kind") or ""),
            "message": str(error.get("message") or ""),
            "details": {
                key: copy.deepcopy(value)
                for key, value in error.items()
                if key not in {
                    "time",
                    "kind",
                    "message",
                    "failure_provenance",
                    "production_source_revision",
                    "provenance_sha256",
                    "evidence_sha256",
                }
            },
        }
        if error.get("evidence_sha256") != self._canonical_receipt_sha256(
            evidence
        ):
            return {
                "status": "invalid",
                "reason": "과거 실패 행의 evidence checksum이 일치하지 않습니다.",
            }
        return {
            "status": "current",
            "reason": "target content key와 production source revision이 일치합니다.",
            "production_source_revision": current_revision,
        }

    def _effective_failure_entry(self, iid, entry):
        """Drop only invalid saved errors before retry routing."""

        effective = copy.deepcopy(entry if isinstance(entry, dict) else {})
        verdicts = {}
        for column in ("push_status", "blend_status", "spm_status"):
            error_key = f"{column}_error"
            error = effective.get(error_key)
            if not isinstance(error, dict):
                continue
            verdict = self._failure_record_freshness(iid, error)
            verdicts[column] = verdict
            if verdict.get("status") == "current":
                continue
            effective.pop(error_key, None)
            # Keep the phase marker only as a route to current validation.
            # The bound error/cause is void, so dependency and exact-repair
            # logic cannot obey it.  Parent artifact validation may still use
            # the phase marker to choose Unreal rebind vs Blender rebuild.
        return effective, verdicts

    def _failed_retry_repair_state(self, iid):
        """Return one live provenance decision, never a saved table label."""
        try:
            state = self._repair_output_state(Path(iid))
            if not isinstance(state, dict) or "current" not in state:
                raise ValueError("Repair eligibility state is incomplete")
            return state
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            return {
                "current": False,
                "push_ready": False,
                "kind": "inspection_incomplete",
                "reason": (
                    "Blender Repair freshness could not be proven: "
                    + compact_error_message(exc, 160)
                ),
            }

    def _failed_retry_durable_evidence(
        self,
        iid,
        repair_state=None,
        *,
        entry_override=None,
        failure_record_verdicts=None,
    ):
        """Return the saved structured failure plus current live provenance."""

        context = self._failed_retry_planning_context()
        entry = (
            copy.deepcopy(entry_override)
            if isinstance(entry_override, dict)
            else self._failed_retry_state_entry(iid)
        )
        automation = entry.get("failed_retry_automation") or {}
        automation_status = str(
            automation.get("status")
            if isinstance(automation, dict)
            else ""
        )
        disposition = "candidate"
        if automation_status == STATUS_COMPLETED:
            disposition = "current_success"
        elif automation_status == STATUS_CANCELLED:
            disposition = "resumable_cancelled"
        authoritative_result = None
        authoritative_reader = getattr(
            self,
            "_target_authoritative_result",
            None,
        )
        if callable(authoritative_reader):
            authoritative_result = authoritative_reader(str(iid), "push")
        normalized_outcome = str(
            (authoritative_result or {}).get("outcome") or ""
        )
        if normalized_outcome == "completed":
            disposition = "current_success"
        elif normalized_outcome == "pending_unreal":
            disposition = "current_wait"
        elif (
            normalized_outcome == "cancelled"
            and disposition != "resumable_cancelled"
        ):
            disposition = "current_cancelled"
        reason_token, evidence = self._failure_result_from_entry(entry)
        current_errors = {
            key: copy.deepcopy(value)
            for key, value in entry.items()
            if key in {
                "push_status_error",
                "blend_status_error",
                "spm_status_error",
            }
        }
        if disposition in {
            "current_success",
            "current_wait",
            "current_cancelled",
        }:
            reason_token = disposition
            evidence = {}
            current_errors = {}
        report_payloads = []
        if disposition not in {
            "current_success",
            "current_wait",
            "current_cancelled",
        }:
            pending = [evidence, current_errors]
            report_paths = []
            while pending:
                value = pending.pop()
                if isinstance(value, dict):
                    for key, child in value.items():
                        if key in {"report", "audit_report", "report_file"}:
                            if isinstance(child, (str, os.PathLike)):
                                report_paths.append(Path(child))
                        elif isinstance(child, (dict, list, tuple)):
                            pending.append(child)
                elif isinstance(value, (list, tuple)):
                    pending.extend(value)
            seen_reports = set()
            for report_path in report_paths:
                absolute = report_path.expanduser().absolute()
                key = os.path.normcase(str(absolute)).casefold()
                if key in seen_reports:
                    continue
                seen_reports.add(key)
                try:
                    if (
                        not absolute.is_file()
                        or absolute.stat().st_size
                        > RETRY_PLANNING_MAX_REPORT_BYTES
                    ):
                        continue
                    payload = (
                        context.load_json(
                            absolute,
                            namespace="durable_report",
                            max_bytes=RETRY_PLANNING_MAX_REPORT_BYTES,
                        )
                        if context is not None
                        else json.loads(
                            absolute.read_text(encoding="utf-8")
                        )
                    )
                except (
                    OSError,
                    UnicodeError,
                    json.JSONDecodeError,
                    RetryPlanningSnapshotError,
                ):
                    continue
                report_payloads.append({
                    "path": str(absolute),
                    "payload": payload,
                })
        resumable = {}
        if disposition == "resumable_cancelled" and isinstance(
            automation, dict
        ):
            resumable = {
                "original_failure": copy.deepcopy(
                    automation.get("original_failure") or {}
                ),
                "plan": copy.deepcopy(automation.get("plan") or {}),
                "cancelled_receipt": {
                    key: copy.deepcopy(automation.get(key))
                    for key in (
                        "request_id",
                        "parent_retry_id",
                        "exact_spm",
                        "status",
                        "attempted_stages",
                    )
                },
            }
        atlas_manifest_repair = {}
        if disposition == "candidate":
            try:
                current_atlas_plan = atlas_manifest_mirror_repair_plan(iid)
            except (OSError, RuntimeError, TypeError, ValueError):
                current_atlas_plan = {}
            if current_atlas_plan.get("status") in {
                "repairable",
                "unrepairable",
            }:
                atlas_manifest_repair = current_atlas_plan
        return {
            "schema_version": 1,
            "queue_id": str(iid),
            "terminal_disposition": disposition,
            "current_phase_errors": current_errors,
            "current_report_payloads": report_payloads,
            "selected_failure": {
                "reason_token": reason_token,
                "evidence": copy.deepcopy(evidence),
            },
            "resumable_cancelled": resumable,
            "current_atlas_manifest_repair": atlas_manifest_repair,
            "current_repair_state": copy.deepcopy(
                repair_state
                if isinstance(repair_state, dict)
                    else self._failed_retry_repair_state(iid)
            ),
            "failure_record_freshness": copy.deepcopy(
                failure_record_verdicts or {}
            ),
        }

    def _recover_missing_root_reason(self, iid, evidence):
        """Regenerate a lost root reason with exactly one fresh audit.

        A row that carries only repair wrappers -- `automatic_repair_failed`
        and friends -- records that a repair failed without recording what it
        was for.  The operator action printed on such a row already says to
        run a fresh audit and retry the real reason, so run it rather than
        print it (#167).

        The budget is one, durable, and spent *before* the audit runs: the
        state this recovers from is exactly the state a crash leaves behind,
        so an in-memory counter would reset into a loop.
        """
        if not needs_fresh_reaudit(evidence):
            return evidence

        # The evidence above decides whether a recovery is warranted; the
        # state row holds the budget.  Keeping them separate matters because
        # the wrapper can land on any status column, while the budget must be
        # one per target no matter which column recorded the failure.
        with self.state_lock:
            entry = self.state.setdefault(str(iid), {})
            record = repair_failure_record(entry)
            if record.get("fresh_reaudit_attempted"):
                return evidence
            entry[REPAIR_FAILURE_KEY] = mark_fresh_reaudit_attempted(
                {REPAIR_FAILURE_KEY: record} if record else {},
                request_id=str(iid),
            )[REPAIR_FAILURE_KEY]
            save_state(self.state)

        self.log(
            "[복구 provenance] 원인 코드가 남지 않은 실패 행을 fresh audit으로 "
            f"1회 재생성합니다 · {Path(iid).name}"
        )
        try:
            fresh_state = self._failed_retry_repair_state(iid)
        except Exception as exc:  # noqa: BLE001 - audit must not raise here
            fresh_state = {
                "current": False,
                "push_ready": False,
                "kind": "inspection_incomplete",
                "reason": compact_error_message(exc, 240),
            }

        recovered = copy.deepcopy(evidence)
        recovered["fresh_reaudit"] = {
            "policy": "single_shot_root_reason_recovery_v1",
            "repair_state": copy.deepcopy(fresh_state),
        }
        recovered[REPAIR_FAILURE_KEY] = mark_fresh_reaudit_attempted(
            {REPAIR_FAILURE_KEY: evidence.get(REPAIR_FAILURE_KEY)}
            if isinstance(evidence, dict)
            else {},
            request_id=str(iid),
        )[REPAIR_FAILURE_KEY]
        recovered["current_repair_state"] = copy.deepcopy(fresh_state)
        return recovered

    def _set_failed_retry_automatic_status(
        self,
        iid,
        status,
        *,
        plan=None,
        attempted_stages=(),
        completed_stages=0,
        error="",
        friendly_reason="",
        remaining_action="",
    ):
        """Persist one non-failure transition or one terminal final failure."""

        iid = str(iid)
        label = AUTOMATIC_REPAIR_STATUS_LABELS.get(status, str(status))
        plan_payload = (
            plan.metadata() if hasattr(plan, "metadata")
            else copy.deepcopy(dict(plan or {}))
        )
        progress = repair_progress_payload(
            plan_payload,
            status=status,
            completed_stages=completed_stages,
            attempted_stages=attempted_stages,
            error=error,
        )
        progress.update({
            "friendly_reason": str(friendly_reason or ""),
            "remaining_action": str(remaining_action or ""),
        })
        display = label
        if status == STATUS_FINAL_FAILED and friendly_reason:
            display = f"최종 차단 · 원인: {friendly_reason}"
            if remaining_action:
                display += f" · 조치: {remaining_action}"
        elif status == STATUS_COMPLETED:
            display = compact_success_message(attempted_stages)
        self.ui_queue.put(("cell", (iid, "push_status", display)))
        with self.state_lock:
            entry = self.state.setdefault(iid, {})
            automation = entry.setdefault("failed_retry_automation", {})
            if "original_failure" not in automation:
                automation["original_failure"] = {
                    key: copy.deepcopy(value)
                    for key, value in entry.items()
                    if key.endswith("_status_error")
                    or key.endswith("_status_kind")
                }
            automation.update(progress)
            automation["plan"] = plan_payload
            entry["push_status"] = display
            entry["push_status_kind"] = (
                "automatic_repair_failed"
                if status == STATUS_FINAL_FAILED
                else "automatic_repair"
            )
            if status == STATUS_FINAL_FAILED:
                entry["push_status_error"] = {
                    "time": datetime.now().isoformat(timespec="seconds"),
                    "kind": "automatic_repair_failed",
                    "message": friendly_reason or "자동 복구 후 재검증 실패",
                    "attempted_stages": copy.deepcopy(list(attempted_stages)),
                    "remaining_action": str(remaining_action or ""),
                    "reason_codes": list(plan_payload.get("reason_codes") or ()),
                    "raw_error": str(error or ""),
                }
            else:
                entry.pop("push_status_error", None)
            save_state(self.state)
        return progress

    def _fresh_reaudit_after_exact_repair(self, item, stage):
        """Run the existing read-only exact SPM audit after every BAT stage."""

        iid = str(item["spm"])
        stage_name = str(stage.get("stage") or "")
        exact_spm = Path(item["spm"])
        evidence = {}
        if stage_name == "atlas_manifest_repair":
            refreshed = self._refresh_canonical_atlas_manifests(exact_spm)
            current_plan = atlas_manifest_mirror_repair_plan(exact_spm)
            if current_plan.get("status") != "not_needed":
                raise RuntimeError(
                    "Atlas manifest conflict remained after exact repair"
                )
            evidence["atlas_manifest_refresh"] = copy.deepcopy(refreshed)
        if stage_name == "pcg_texture":
            artifact = self._execute_material_preflight(
                exact_spm,
                speedtree_output_spm_for(exact_spm),
                datetime.now().strftime("%Y%m%d_%H%M%S_reaudit"),
            )
            material_result = artifact.get("result") or {}
            if (
                artifact.get("code") != 0
                or material_result.get("status") != "ok"
            ):
                raise RuntimeError(
                    material_result.get("failure_reason")
                    or material_result.get("error")
                    or "PCG texture fresh material re-audit failed"
                )
            evidence["material_preflight_report"] = str(
                artifact.get("report") or ""
            )
        if stage_name in {"generator_sync_and_cluster", "cluster_refresh"}:
            providers = [
                Path(path)
                for path in stage.get("target_spms") or ()
                if os.path.normcase(os.path.abspath(str(path))).casefold()
                != os.path.normcase(os.path.abspath(str(exact_spm))).casefold()
            ]
            observations = []
            for provider in providers:
                observations.append(
                    self._cluster_normalization_stage_observation(
                        exact_spm,
                        datetime.now().strftime("%Y%m%d_%H%M%S_reaudit"),
                        provider,
                        require_normalized=True,
                    )
                )
            if stage_name == "generator_sync_and_cluster" and not observations:
                raise RuntimeError(
                    "Generator/Cluster fresh re-audit has no exact provider target"
                )
            evidence["cluster_observations"] = observations
        self._job_check(iid, Path(item["spm"]))
        state = self._failed_retry_repair_state(iid)
        if state.get("kind") == "inspection_incomplete":
            raise RuntimeError(state.get("reason") or "fresh re-audit incomplete")
        return {
            "stage": str(stage.get("stage") or ""),
            "status": "audited",
            "repair_state": copy.deepcopy(state),
            "evidence": evidence,
        }

    def _execute_exact_repair_stage(
        self,
        plan,
        stage,
        lease,
        *,
        stage_index,
        receipt,
        provenance_source,
        on_progress=None,
    ):
        """Execute one registry-selected stage through the shared BAT path."""

        from pcg_st9_texture_batch.exact_target_repair import (
            execute_step3_standard,
        )
        from spm_generator_sync.exact_target_repair import (
            execute_exact_generator_request,
        )

        def execute_modeler_node_table(
            request,
            *,
            progress,
            cancel_event,
            lease,
        ):
            del lease
            if cancel_event.is_set():
                raise WaitCancelled("Modeler Node table repair cancelled")
            scope = copy.deepcopy(stage.get("recovery_scope") or {})
            producer_spm = stage.get("producer_spm")
            target_spms = list(request.get("target_spms") or ())
            if (
                scope.get("available") is not True
                or not producer_spm
                or len(target_spms) != 1
            ):
                return {
                    "outcome": "failed",
                    "shared_queue_success": False,
                    "reason": "sealed Modeler Node table scope is incomplete",
                }
            target_spm = Path(target_spms[0])
            synthetic = TargetPlannedExclusionError(
                "sealed Modeler Node table repair",
                reason_token="normalized_generator_node_table_stale",
                target_spm=target_spm,
                producer_spm=producer_spm,
                evidence={
                    "issue_codes": [
                        "NORMALIZED_GENERATOR_NODE_TABLE_STALE"
                    ],
                    "stale_node_table_recovery": scope,
                },
            )
            progress(
                "modeler_node_table_recovery",
                completed=0,
                remaining=1,
                unit_stage="modeler_node_table_recovery",
            )
            try:
                resolution = self._attempt_stale_node_table_recovery(
                    synthetic,
                    f"exact_{request['request_id']}",
                    producer_spm,
                    require_normalized=False,
                )
            except BatchItemError as exc:
                if exc.kind == "cancelled":
                    raise WaitCancelled(str(exc)) from exc
                raise
            if resolution is None:
                attempt = synthetic.evidence.get("recovery_attempt") or {}
                return {
                    "outcome": "failed",
                    "shared_queue_success": False,
                    "reason": str(
                        attempt.get("reason_token")
                        or "sealed Modeler Node table repair failed"
                    ),
                    "recovery_attempt": copy.deepcopy(attempt),
                }
            progress(
                "modeler_node_table_recovery",
                completed=1,
                remaining=0,
                unit_stage="modeler_node_table_recovery",
            )
            return {
                "outcome": "completed",
                "shared_queue_success": True,
                "live_resolution": copy.deepcopy(resolution),
            }

        executors = {
            PCG_TEXTURE_TOOL: execute_step3_standard,
            GENERATOR_SYNC_TOOL: execute_exact_generator_request,
            MODELER_RECOVERY_TOOL: execute_modeler_node_table,
        }
        tool = str(stage.get("tool") or "")
        if (
            tool == MODELER_RECOVERY_TOOL
            and stage.get("repair_action") != MODELER_NODE_TABLE_RECOVERY
        ):
            raise RuntimeError(
                "unsupported SpeedTree Modeler exact repair action: "
                + str(stage.get("repair_action") or "")
            )
        executor = executors.get(tool)
        if executor is None:
            raise RuntimeError(f"unsupported exact repair tool: {tool}")
        request = build_exact_target_request(
            tool=tool,
            repair_action=stage["repair_action"],
            target_spms=stage["target_spms"],
            repair_stage=stage["stage"],
            provenance={
                "reason_codes": list(plan.get("reason_codes") or ()),
                "evidence_sha256": plan.get("evidence_sha256"),
                "source": str(provenance_source),
            },
            parent_retry_id=plan["parent_retry_id"],
            request_id=f"{plan['request_id']}-{stage_index}",
            receipt=receipt,
        )
        return run_exact_target_request(
            request,
            executor,
            inherited_lease=lease,
            cancel_event=self.stop_flag,
            on_progress=on_progress,
        )

    def _run_failed_retry_repair_job(self, job, lease):
        """Run exact BAT stages, fresh-audit, then re-enter the pipeline."""
        targets_by_id = {
            str(item["spm"]): item for item in job.get("targets") or ()
        }
        plans = list(job.get("repair_plans") or ())
        plans_by_id = {
            str(plan.get("exact_spm")): plan for plan in plans
        }
        total_stage_count = sum(len(plan.get("stages") or ()) for plan in plans)
        global_completed = 0
        outcomes = []
        pipeline_targets = []
        successful_repair_ids = set()
        cancelled_repair_ids = set()

        for plan in plans:
            iid = str(plan["exact_spm"])
            item = targets_by_id.get(iid)
            attempted = []
            if item is None:
                missing_decision = repair_ui_decision({
                    "reason_token": "repair_inventory_target_missing",
                    "evidence": {"plan": copy.deepcopy(plan)},
                })
                outcomes.append({
                    "target": iid,
                    "target_name": Path(iid).name,
                    "outcome": "failed",
                    "reason_token": "repair_inventory_target_missing",
                    "evidence": {"plan": copy.deepcopy(plan)},
                })
                self._set_failed_retry_automatic_status(
                    iid,
                    STATUS_FINAL_FAILED,
                    plan=plan,
                    friendly_reason=missing_decision["reason"],
                    remaining_action=missing_decision["action"],
                )
                continue

            cancelled = False
            failed = None
            for stage_index, stage in enumerate(plan.get("stages") or (), 1):
                if self.stop_flag.is_set():
                    cancelled = True
                    break
                status = stage_running_status(stage)
                self._set_failed_retry_automatic_status(
                    iid,
                    status,
                    plan=plan,
                    attempted_stages=attempted,
                    completed_stages=stage_index - 1,
                )
                label = AUTOMATIC_REPAIR_STATUS_LABELS[status]
                self.ui_queue.put((
                    "progress",
                    f"{Path(iid).name} · {label} · "
                    f"완료 {global_completed}/{total_stage_count} · "
                    f"남음 {max(0, total_stage_count - global_completed)}",
                ))
                receipt = LOG_DIR / (
                    f"exact_repair_{plan['request_id']}_{stage_index}.json"
                )
                current_stage_status = [status]

                def on_exact_progress(payload, asset=Path(iid).name):
                    unit_stage = str(payload.get("unit_stage") or "")
                    desired_status = (
                        STATUS_CLUSTER
                        if unit_stage == "cluster_refresh"
                        else (
                            STATUS_GENERATOR
                            if unit_stage == "generator_sync"
                            else current_stage_status[0]
                        )
                    )
                    if desired_status != current_stage_status[0]:
                        current_stage_status[0] = desired_status
                        self._set_failed_retry_automatic_status(
                            iid,
                            desired_status,
                            plan=plan,
                            attempted_stages=attempted,
                            completed_stages=stage_index - 1,
                        )
                    self.ui_queue.put((
                        "progress",
                        f"{asset} · "
                        f"{payload.get('current_stage') or label} · "
                        f"완료 {global_completed}/{total_stage_count} · "
                        f"남음 {max(0, total_stage_count - global_completed)}",
                    ))

                terminal = self._execute_exact_repair_stage(
                    plan,
                    stage,
                    lease,
                    stage_index=stage_index,
                    receipt=receipt,
                    provenance_source="sk_batch.failed_retry",
                    on_progress=on_exact_progress,
                )
                attempted_row = {
                    "stage": stage["stage"],
                    "tool": stage["tool"],
                    "repair_action": stage["repair_action"],
                    "targets": list(stage["target_spms"]),
                    "receipt": str(receipt),
                    "status": terminal.get("status"),
                }
                attempted.append(attempted_row)
                terminal_status = str(
                    terminal.get("terminal_status")
                    or terminal.get("status")
                    or ""
                )
                if terminal_status == "cancelled":
                    cancelled = True
                    break
                if terminal_status != "completed":
                    failed = (
                        "BAT exact repair failed",
                        terminal.get("error")
                        or str((terminal.get("result") or {}).get("reason") or ""),
                    )
                    break
                global_completed += 1
                self._set_failed_retry_automatic_status(
                    iid,
                    STATUS_REAUDIT,
                    plan=plan,
                    attempted_stages=attempted,
                    completed_stages=stage_index,
                )
                self.ui_queue.put((
                    "progress",
                    f"{Path(iid).name} · 재검증 중 · "
                    f"완료 {global_completed}/{total_stage_count} · "
                    f"남음 {max(0, total_stage_count - global_completed)}",
                ))
                try:
                    attempted_row["fresh_reaudit"] = (
                        self._fresh_reaudit_after_exact_repair(item, stage)
                    )
                except Exception as exc:
                    failed = (
                        "fresh re-audit failed",
                        compact_error_message(exc, 400),
                    )
                    break

            if cancelled:
                cancelled_repair_ids.add(iid)
                self._set_failed_retry_automatic_status(
                    iid,
                    STATUS_CANCELLED,
                    plan=plan,
                    attempted_stages=attempted,
                    completed_stages=sum(
                        row.get("status") == "completed" for row in attempted
                    ),
                )
                outcomes.append({
                    "target": iid,
                    "target_name": Path(iid).name,
                    "outcome": "cancelled",
                    "reason_token": "automatic_repair_cancelled",
                    "evidence": {"attempted_stages": copy.deepcopy(attempted)},
                })
                continue
            if failed is not None:
                headline, raw_error = failed
                reaudit_failed = headline == "fresh re-audit failed"
                failure_token = (
                    "automatic_repair_reaudit_failed"
                    if reaudit_failed
                    else "automatic_repair_failed"
                )
                failure_evidence = {
                    "attempted_stages": copy.deepcopy(attempted),
                    "raw_error": raw_error,
                    "reason_codes": list(plan.get("reason_codes") or ()),
                }
                failure_decision = repair_ui_decision({
                    "reason_token": failure_token,
                    "evidence": failure_evidence,
                })
                self._set_failed_retry_automatic_status(
                    iid,
                    STATUS_FINAL_FAILED,
                    plan=plan,
                    attempted_stages=attempted,
                    completed_stages=sum(
                        row.get("status") == "completed" for row in attempted
                    ),
                    error=raw_error,
                    friendly_reason=failure_decision["reason"],
                    remaining_action=failure_decision["action"],
                )
                outcomes.append({
                    "target": iid,
                    "target_name": Path(iid).name,
                    "outcome": "failed",
                    "reason_token": failure_token,
                    "evidence": failure_evidence,
                })
                continue
            self._set_failed_retry_automatic_status(
                iid,
                STATUS_PIPELINE,
                plan=plan,
                attempted_stages=attempted,
                completed_stages=len(plan.get("stages") or ()),
            )
            item["_failed_retry_attempted_stages"] = attempted
            pipeline_targets.append(item)
            successful_repair_ids.add(iid)

        pipeline_target_ids = {
            str(item["spm"]) for item in pipeline_targets
        }
        resume_after_repairs = copy.deepcopy(
            job.get("resume_after_repairs") or {}
        )
        for resume_iid, required_roots in resume_after_repairs.items():
            resume_iid = str(resume_iid)
            required_roots = [str(value) for value in required_roots or ()]
            item = targets_by_id.get(resume_iid)
            missing_roots = [
                root
                for root in required_roots
                if root not in successful_repair_ids
            ]
            if item is not None and required_roots and not missing_roots:
                if resume_iid not in pipeline_target_ids:
                    item["_failed_retry_resumed_by"] = required_roots
                    pipeline_targets.append(item)
                    pipeline_target_ids.add(resume_iid)
                self.log(
                    "[자동 복구 완료] 필수 Cluster 복구 후 Blender 재개: "
                    f"{Path(resume_iid).name}"
                )
                continue
            cancelled_roots = [
                root for root in required_roots
                if root in cancelled_repair_ids
            ]
            if cancelled_roots or self.stop_flag.is_set():
                names = ", ".join(
                    Path(value).name
                    for value in cancelled_roots or required_roots
                )
                reason = (
                    "필수 Cluster 자동 복구가 취소되어 Blender 재개도 "
                    f"취소했습니다: {names or '대상 없음'}"
                )
                self._record_phase_status(
                    resume_iid,
                    "push_status",
                    f"재시도 취소: {reason}",
                    "cancelled",
                    reason,
                    details={
                        "blocked_by": required_roots,
                        "repair_disposition": "cancelled",
                    },
                    persist=True,
                )
                outcomes.append({
                    "target": resume_iid,
                    "target_name": Path(resume_iid).name,
                    "outcome": "cancelled",
                    "reason_token": "required_cluster_repair_cancelled",
                    "evidence": {"blocked_by": required_roots},
                })
                continue
            names = ", ".join(
                Path(value).name for value in missing_roots or required_roots
            )
            reason = (
                "필수 Cluster 자동 복구가 완료되지 않아 Blender를 재개하지 "
                f"못했습니다: {names or '대상 없음'}"
            )
            if item is None:
                reason = "재개할 소비자가 current inventory에서 사라졌습니다."
            self._record_phase_status(
                resume_iid,
                "push_status",
                f"최종 차단: {reason}",
                "dependency_blocked",
                reason,
                details={
                    "blocked_by": required_roots,
                    "repair_disposition": REPAIR_UI_BLOCKED,
                },
                persist=True,
            )
            outcomes.append({
                "target": resume_iid,
                "target_name": Path(resume_iid).name,
                "outcome": "blocked",
                "reason_token": "required_cluster_repair_failed",
                "evidence": {"blocked_by": required_roots},
            })

        if pipeline_targets and not self.stop_flag.is_set():
            self.ui_queue.put((
                "progress",
                f"Blender-Unreal 재시도 중 · 대상 {len(pipeline_targets)} · "
                f"완료 0 · 남음 {len(pipeline_targets)}",
            ))
            self._run_full_pipeline(
                pipeline_targets,
                terminal_phase="push",
                selected_scope=True,
                emit_done=False,
            )
            pipeline_summary = copy.deepcopy(
                getattr(self, "_phase_result_summary", None)
                or self._summarize_phase_targets(pipeline_targets)
            )
            pipeline_by_id = {
                str(row.get("target")): row
                for row in pipeline_summary.get("target_outcomes") or ()
            }
            for item in pipeline_targets:
                iid = str(item["spm"])
                attempted = item.pop("_failed_retry_attempted_stages", [])
                resumed_by = item.pop("_failed_retry_resumed_by", [])
                pipeline_row = pipeline_by_id.get(iid, {})
                plan = plans_by_id.get(iid)
                pipeline_outcome = str(pipeline_row.get("outcome") or "")
                if plan is None:
                    outcomes.append(pipeline_row or {
                        "target": iid,
                        "target_name": Path(iid).name,
                        "outcome": "failed",
                        "reason_token": "resumed_pipeline_result_missing",
                        "evidence": {"resumed_by": resumed_by},
                    })
                    continue
                if pipeline_outcome == "completed":
                    final_receipt = self._set_failed_retry_automatic_status(
                        iid,
                        STATUS_COMPLETED,
                        plan=plan,
                        attempted_stages=attempted,
                        completed_stages=len(attempted),
                    )
                    if not fresh_repair_receipt_authoritative(
                        final_receipt, plan
                    ):
                        raise RuntimeError(
                            "fresh automatic repair receipt identity mismatch"
                        )
                elif pipeline_outcome == "pending_unreal":
                    self._set_failed_retry_automatic_status(
                        iid,
                        STATUS_PIPELINE,
                        plan=plan,
                        attempted_stages=attempted,
                        completed_stages=len(attempted),
                    )
                elif pipeline_outcome == "cancelled" or (
                    not pipeline_outcome and self.stop_flag.is_set()
                ):
                    self._set_failed_retry_automatic_status(
                        iid,
                        STATUS_CANCELLED,
                        plan=plan,
                        attempted_stages=attempted,
                        completed_stages=len(attempted),
                    )
                    if not pipeline_row:
                        pipeline_row = {
                            "target": iid,
                            "target_name": Path(iid).name,
                            "outcome": "cancelled",
                            "reason_token": "automatic_retry_cancelled",
                            "evidence": {},
                        }
                else:
                    evidence = copy.deepcopy(pipeline_row.get("evidence") or {})
                    retry_token = str(
                        pipeline_row.get("reason_token")
                        or "pipeline_retry_result_missing"
                    )
                    retry_decision = repair_ui_decision({
                        "reason_token": retry_token,
                        "evidence": evidence,
                    })
                    raw_error = str(
                        evidence.get("message")
                        or evidence.get("raw_error")
                        or retry_token
                        or "pipeline retry failed"
                    )
                    self._set_failed_retry_automatic_status(
                        iid,
                        STATUS_FINAL_FAILED,
                        plan=plan,
                        attempted_stages=attempted,
                        completed_stages=len(attempted),
                        error=raw_error,
                        friendly_reason=retry_decision["reason"],
                        remaining_action=retry_decision["action"],
                    )
                outcomes.append(pipeline_row or {
                    "target": iid,
                    "target_name": Path(iid).name,
                    "outcome": "failed",
                    "reason_token": "pipeline_retry_result_missing",
                    "evidence": {},
                })
        elif pipeline_targets:
            for item in pipeline_targets:
                iid = str(item["spm"])
                attempted = item.pop("_failed_retry_attempted_stages", [])
                item.pop("_failed_retry_resumed_by", None)
                plan = plans_by_id.get(iid)
                if plan is not None:
                    self._set_failed_retry_automatic_status(
                        iid,
                        STATUS_CANCELLED,
                        plan=plan,
                        attempted_stages=attempted,
                        completed_stages=len(attempted),
                    )
                outcomes.append({
                    "target": iid,
                    "target_name": Path(iid).name,
                    "outcome": "cancelled",
                    "reason_token": "automatic_retry_cancelled",
                    "evidence": {"attempted_stages": copy.deepcopy(attempted)},
                })

        completed_count = sum(
            row.get("outcome") == "completed" for row in outcomes
        )
        pending_count = sum(
            row.get("outcome") == "pending_unreal" for row in outcomes
        )
        failed_count = sum(row.get("outcome") == "failed" for row in outcomes)
        cancelled_count = sum(
            row.get("outcome") == "cancelled" for row in outcomes
        )
        blocked_count = sum(
            row.get("outcome") in {"blocked", "planned_excluded"}
            for row in outcomes
        )
        owner_lost_count = sum(
            row.get("outcome") == "owner_lost" for row in outcomes
        )
        summary = {
            "selected_count": len(outcomes),
            "completed_count": completed_count,
            "pending_count": pending_count,
            "blocked_count": blocked_count,
            "owner_lost_count": owner_lost_count,
            "planned_excluded_count": sum(
                row.get("outcome") == "planned_excluded" for row in outcomes
            ),
            "dependency_blocked_count": sum(
                row.get("outcome") == "blocked" for row in outcomes
            ),
            "failed_count": failed_count,
            "cancelled_count": cancelled_count,
            "target_outcomes": outcomes,
            "shared_failures": [],
        }
        self._phase_result_summary = summary
        return not (
            failed_count
            or blocked_count
            or owner_lost_count
            or cancelled_count
        )

    def _failed_retry_parent_source_record(self, queue_id, parent_item):
        state_entry = self._failed_retry_state_entry(queue_id)
        expected = str(parent_item.get("source_fingerprint") or "")
        source_record = copy.deepcopy(
            state_entry.get("push_source_fingerprint_cache") or {}
        )
        if source_record.get("fingerprint") != expected:
            source_record = copy.deepcopy(
                (
                    state_entry.get("push_recovery_source_proofs") or {}
                ).get(expected)
                or {}
            )
        if source_record.get("fingerprint") != expected:
            recovery = parent_item.get("recovery") or {}
            snapshot = recovery.get("current_source_snapshot")
            if (
                snapshot
                and recovery.get("current_source_fingerprint") == expected
            ):
                source_record = {
                    "version": PUSH_SOURCE_FINGERPRINT_CACHE_VERSION,
                    "fingerprint": expected,
                    "snapshot": copy.deepcopy(snapshot),
                }
        if source_record.get("fingerprint") != expected:
            raise PushUnrealRecoveryError(
                "parent source proof is unavailable for "
                + Path(queue_id).name
            )
        return source_record

    def _validate_failed_retry_unreal_item_current(
        self,
        queue_id,
        parent_item,
        parent_source_record,
    ):
        """Use the execution validator before classifying as Unreal-only."""
        blend_value = str(parent_item.get("blend") or "")
        if not blend_value:
            raise PushUnrealRecoveryError(
                "parent manifest item has no Blender source path"
            )
        current_fingerprint = self._source_push_fingerprint(
            Path(blend_value), queue_id
        )
        current_record = copy.deepcopy(
            self._failed_retry_state_entry(queue_id).get(
                "push_source_fingerprint_cache"
            )
            or {}
        )
        validate_unreal_only_recovery_evidence(
            parent_item,
            parent_source_record=parent_source_record,
            current_source_record=current_record,
            current_source_fingerprint=current_fingerprint,
            rebindable_code_paths=self._push_rebindable_unreal_code_paths(),
        )
        return current_record

    def start_failed_results_retry(self):
        """Plan complete-inventory retry work without blocking the Tk thread."""
        return self._start_failed_results_retry(
            list(self.items),
            scope="all",
            dialog_title="전체 실패 이력 재시도",
            empty_message="현재 목록 전체에 재시도할 대상이 없습니다.",
        )

    def start_checked_failed_results_retry(self):
        """Plan failed/stale retry work for the exact checked rows only."""
        return self._start_failed_results_retry(
            [
                iid
                for iid, item in self.items.items()
                if bool(item.get("checked"))
            ],
            scope="checked",
            dialog_title="체크 항목 실패 재시도",
            empty_message="체크한 재시도 대상이 없습니다.",
        )

    def _start_failed_results_retry(
        self,
        candidate_iids,
        *,
        scope,
        dialog_title,
        empty_message,
    ):
        """Snapshot one explicit retry scope and plan it off the Tk thread."""
        self._close_cell_editor()
        candidate_iids = list(candidate_iids)
        if not candidate_iids:
            messagebox.showinfo(
                dialog_title,
                empty_message,
            )
            return
        retry_request = {
            "scope": str(scope),
            "dialog_title": str(dialog_title),
            "empty_message": str(empty_message),
        }
        cfg = dict(self._collect_cfg())
        inventory_snapshot, _snapshot_targets = self._snapshot_batch_request(
            candidate_iids
        )
        cached_plan = self._reusable_retry_plan_cache()
        tracker = None
        if getattr(self, "_async_retry_planning_enabled", False):
            if (
                getattr(self, "active_batch_job", None) is None
                and not getattr(self, "pending_batch_jobs", ())
            ):
                self.stop_flag.clear()
            self._set_batch_queue_controls(True)
            self.progress_var.set(
                f"retry stage={RETRY_STAGE_PLANNING} · "
                f"대상 {len(candidate_iids)}개"
            )
            # Flush the planning label before any inventory/parent validation
            # begins in the worker. This callback is the Tk owner thread.
            self.root.update_idletasks()
            tracker = self._new_retry_progress(candidate_iids, cfg)
            planning_session_id = uuid.uuid4().hex
            runtime = getattr(self, "shared_queue_runtime", None)
            if runtime is not None:
                planning_owner = runtime.owner_identity
            else:
                planning_owner = {
                    "owner_id": f"sk_batch-planning:{os.getpid()}",
                    "hostname": os.environ.get("COMPUTERNAME") or "local",
                    "pid": os.getpid(),
                    "process_marker": None,
                }
            planning_owner.update({
                "planning_session_id": planning_session_id,
                "thread_name": f"retry-planner-{tracker.run_id[:8]}",
            })
            tracker.start_planning(planning_owner)
            planning_finished = threading.Event()

            def plan_in_worker():
                plan = None
                try:
                    plan = self._build_failed_retry_plan(
                        candidate_iids,
                        cfg,
                        tracker=tracker,
                        inventory_snapshot=inventory_snapshot,
                        planning_session_id=planning_session_id,
                        cached_plan_cache=cached_plan,
                    )
                except Exception as exc:
                    plan = {
                        "error": compact_error_message(exc),
                        "tracker": tracker,
                        "selected_iids": list(candidate_iids),
                        "cfg": cfg,
                    }
                if plan is not None:
                    plan["_retry_request"] = copy.deepcopy(retry_request)
                    tracker.planning_ready(planning_session_id)
                    planning_finished.set()
                    self.ui_queue.put(("retry_plan_ready", plan))

            worker = threading.Thread(
                target=plan_in_worker,
                name=planning_owner["thread_name"],
                daemon=True,
            )
            worker.retry_progress_run_id = tracker.run_id
            self._retry_planning_workers.add(worker)
            worker.start()

            def heartbeat_planner():
                while not planning_finished.wait(1.0):
                    if not worker.is_alive():
                        tracker.finish_planning(
                            RETRY_STAGE_FAILED,
                            "planner thread terminated before plan commit",
                        )
                        planning_finished.set()
                        return
                    if not tracker.planning_heartbeat(
                        planning_session_id,
                        thread_ident=worker.ident,
                    ):
                        return

            threading.Thread(
                target=heartbeat_planner,
                name=f"retry-planner-heartbeat-{tracker.run_id[:8]}",
                daemon=True,
            ).start()
            return tracker.run_id

        plan = self._build_failed_retry_plan(
            candidate_iids,
            cfg,
            inventory_snapshot=inventory_snapshot,
        )
        plan["_retry_request"] = copy.deepcopy(retry_request)
        return self._commit_failed_retry_plan(plan)

    def _build_failed_retry_plan(
        self,
        candidate_iids,
        cfg,
        tracker=None,
        inventory_snapshot=None,
        planning_session_id=None,
        cached_plan_cache=None,
    ):
        """Capture one bounded generation, then build without Tk or live state."""
        candidate_iids = list(candidate_iids)
        cfg = dict(cfg)
        cfg["_planning_production_source_revision"] = str(
            _PROCESS_PRODUCTION_SOURCE_MANIFEST.content_hash
        )
        if inventory_snapshot is None:
            inventory_snapshot, _targets = self._snapshot_batch_request(
                candidate_iids
            )
        local = getattr(self, "_retry_thread_context", None)
        if local is None:
            local = threading.local()
            self._retry_thread_context = local
        previous = getattr(local, "planning_context", None)
        context = RetryPlanningContext.capture(
            target_ids=candidate_iids,
            state=getattr(self, "state", {}),
            state_lock=getattr(self, "state_lock", threading.RLock()),
            cfg_snapshot=cfg,
            inventory_snapshot=inventory_snapshot,
            cancel_event=getattr(self, "stop_flag", None),
            tracker=tracker,
            planning_session_id=planning_session_id,
        )
        local.planning_context = context
        try:
            context.publish(
                "cache_lookup",
                scanned=0,
                total=len(candidate_iids),
                cache_status="checking",
                progress=False,
                force=True,
            )
            cache_hit = bool(
                isinstance(cached_plan_cache, dict)
                and cached_plan_cache.get("schema_version") == 1
                and cached_plan_cache.get("input_signature")
                == context.input_signature
                and isinstance(cached_plan_cache.get("artifact"), dict)
                and tracker is not None
            )
            if cache_hit:
                try:
                    plan = hydrate_plan_cache_artifact(
                        cached_plan_cache["artifact"],
                        inventory_snapshot=inventory_snapshot,
                        cfg_snapshot=cfg,
                        tracker=tracker,
                        side_effects_committed=bool(
                            cached_plan_cache.get("side_effects_committed")
                        ),
                    )
                except (KeyError, TypeError, ValueError):
                    context.counters["plan_cache_invalid"] += 1
                    cache_hit = False
                else:
                    context.counters["plan_cache_hits"] += 1
                    context.publish(
                        "cache_reuse",
                        scanned=len(candidate_iids),
                        total=len(candidate_iids),
                        last_completed="saved retry plan",
                        cache_status="hit",
                        force=True,
                    )
                    artifact = copy.deepcopy(cached_plan_cache["artifact"])
            if not cache_hit:
                context.counters["plan_cache_misses"] += 1
                context.publish(
                    "cache_lookup",
                    scanned=0,
                    total=len(candidate_iids),
                    cache_status="miss",
                    progress=False,
                    force=True,
                )
                plan = self._build_failed_retry_plan_scoped(
                    candidate_iids,
                    cfg,
                    tracker=tracker,
                    inventory_snapshot=inventory_snapshot,
                    planning_context=context,
                )
                artifact = None
                if tracker is not None:
                    try:
                        artifact = build_plan_cache_artifact(
                            plan,
                            tracker.snapshot(evaluate=False),
                        )
                    except (KeyError, TypeError, ValueError):
                        context.counters["plan_cache_write_rejections"] += 1
            plan["_planning_input_signature"] = copy.deepcopy(
                context.input_signature
            )
            plan["_planning_inventory_snapshot"] = copy.deepcopy(
                inventory_snapshot
            )
            plan["_planning_cache_artifact"] = artifact
            plan["_planning_session_id"] = planning_session_id
            plan["_planning_cache_reused"] = cache_hit
            if (
                tracker is not None
                and planning_session_id is not None
                and artifact is not None
            ):
                tracker.store_planning_cache(
                    planning_session_id,
                    input_signature=context.input_signature,
                    artifact=artifact,
                    side_effects_committed=(
                        bool(cached_plan_cache.get("side_effects_committed"))
                        if cache_hit
                        else False
                    ),
                )
            plan["planning_diagnostics"] = context.diagnostics()
            plan["planning_diagnostics"]["plan_cache"] = (
                "hit" if cache_hit else "miss"
            )
            return plan
        finally:
            if previous is None:
                try:
                    del local.planning_context
                except AttributeError:
                    pass
            else:
                local.planning_context = previous

    def _build_failed_retry_plan_scoped(
        self,
        candidate_iids,
        cfg,
        *,
        tracker=None,
        inventory_snapshot=None,
        planning_context,
    ):
        """Return immutable queue jobs from one RetryPlanningContext."""
        candidate_iids = list(candidate_iids)
        cfg = dict(cfg)
        self._pipeline_dependency_artifact_cache = {}
        deferred_status_updates = []
        deferred_logs = []

        def defer_status(iid, status, **kwargs):
            deferred_status_updates.append({
                "iid": str(iid),
                "status": str(status),
                "kwargs": kwargs,
            })

        repair_states = {}
        effective_entries = {}
        failure_record_verdicts = {}
        fresh_candidate_iids = []
        with planning_context.span("cheap_candidate_discovery"):
            for index, iid in enumerate(candidate_iids, start=1):
                planning_context.check_cancel()
                entry, record_verdicts = self._effective_failure_entry(
                    iid,
                    planning_context.entry(iid),
                )
                effective_entries[iid] = entry
                failure_record_verdicts[iid] = record_verdicts
                saved_signature = self._normalized_live_status_signature(
                    entry.get("live_status_signature")
                )
                live_identity_current = False
                if saved_signature is not None:
                    try:
                        current_signature = self._live_status_signature(
                            Path(iid),
                            tuple(entry.get("live_texture_paths") or ()),
                        )
                        live_identity_current = (
                            current_signature == saved_signature
                        )
                        planning_context.counters[
                            "durable_identity_matches"
                            if live_identity_current
                            else "durable_identity_misses"
                        ] += 1
                    except OSError:
                        planning_context.counters[
                            "durable_identity_incomplete"
                        ] += 1
                candidate, reason = cheap_durable_candidate(
                    entry,
                    live_identity_current=live_identity_current,
                )
                planning_context.counters["candidate_rows"] += int(candidate)
                planning_context.counters["durable_current_excluded"] += int(
                    not candidate
                )
                if candidate:
                    fresh_candidate_iids.append(iid)
                else:
                    repair_states[iid] = {
                        "current": True,
                        "push_ready": True,
                        "kind": "ready",
                        "reason": reason,
                    }
                planning_context.publish(
                    "cheap_candidate",
                    current_target=iid,
                    scanned=index,
                    last_completed=iid,
                )

        with planning_context.span("repair_state_validation"):
            total_fresh = len(fresh_candidate_iids)
            for index, iid in enumerate(fresh_candidate_iids, start=1):
                planning_context.check_cancel()
                planning_context.publish(
                    "repair_state_validation",
                    current_target=iid,
                    scanned=(
                        len(candidate_iids) - total_fresh + index - 1
                    ),
                    last_completed=(
                        fresh_candidate_iids[index - 2]
                        if index > 1
                        else "cheap candidate pass"
                    ),
                    progress=False,
                )
                current_failure_record = any(
                    verdict.get("status") == "current"
                    for verdict in failure_record_verdicts.get(iid, {}).values()
                )
                if current_failure_record:
                    repair_states[iid] = {
                        "current": False,
                        "push_ready": False,
                        "kind": "recorded_failure_current",
                        "reason": (
                            "저장된 실패 행의 target content key와 "
                            "production source revision이 current입니다."
                        ),
                    }
                    planning_context.counters[
                        "current_failure_records_reused"
                    ] += 1
                else:
                    repair_states[iid] = self._failed_retry_repair_state(iid)
                    planning_context.counters[
                        "invalid_failure_records_reaudited"
                    ] += 1
                    planning_context.counters[
                        "repair_state_validations"
                    ] += 1
                planning_context.publish(
                    "repair_state_validation",
                    current_target=iid,
                    scanned=len(candidate_iids) - total_fresh + index,
                    last_completed=iid,
                )
        retry_run_id = (
            "failed-retry-"
            + datetime.now().strftime("%Y%m%dT%H%M%S")
            + "-"
            + uuid.uuid4().hex[:10]
        )
        automatic_plans = {}
        unsupported_plans = {}
        for evidence_index, iid in enumerate(
            fresh_candidate_iids,
            start=1,
        ):
            planning_context.check_cancel()
            with planning_context.span("durable_evidence_load"):
                evidence = self._failed_retry_durable_evidence(
                    iid,
                    repair_states[iid],
                    entry_override=effective_entries[iid],
                    failure_record_verdicts=failure_record_verdicts[iid],
                )
            with planning_context.span("root_reason_recovery"):
                evidence = self._recover_missing_root_reason(iid, evidence)
            planning_context.counters["durable_evidence_rows"] += 1
            planning_context.publish(
                "durable_evidence",
                current_target=iid,
                scanned=(
                    len(candidate_iids)
                    - len(fresh_candidate_iids)
                    + evidence_index
                ),
                last_completed=iid,
            )
            if not has_repair_contract_evidence(evidence):
                continue
            reason_codes = (
                set(evidence_reason_codes(evidence))
                & ALL_REPAIR_CONTRACT_CODES
            )
            if reason_codes and reason_codes.issubset(
                DURABLE_FAILURE_REASON_CODES
            ):
                # These are real terminal failures, not informational facts.
                # Their recovery owner is the phase-aware retry classifier
                # below (Unreal-only vs Blender rebuild), not an exact BAT
                # mutation plan.  The classifier always emits a runnable job
                # or an explicit Korean blocked row.
                planning_context.counters[
                    "durable_failures_routed_by_phase"
                ] += 1
                continue
            request_id = (
                "repair-"
                + hashlib.sha256(
                    (iid + retry_run_id).encode("utf-8")
                ).hexdigest()[:16]
            )
            try:
                plan = build_exact_target_repair_plan(
                    iid,
                    evidence,
                    inventory_paths=candidate_iids,
                    parent_retry_id=retry_run_id,
                    request_id=request_id,
                )
            except (FileNotFoundError, TypeError, ValueError) as exc:
                raw_error = compact_error_message(exc, 240)
                plan = RepairPlan(
                    REPAIR_PLAN_SCHEMA_VERSION,
                    request_id,
                    retry_run_id,
                    str(Path(iid).expanduser().absolute()),
                    hashlib.sha256(json.dumps(
                        evidence,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                        default=str,
                    ).encode("utf-8")).hexdigest(),
                    evidence_reason_codes(evidence),
                    (),
                    False,
                    STATUS_FINAL_FAILED,
                    "exact target/provenance 검증에 실패해 자동 복구를 시작하지 않았습니다.",
                    "목록을 새로 검사해 canonical SPM identity를 갱신한 뒤 다시 실행하세요.",
                )
                unsupported_plans[iid] = plan
                defer_status(
                    iid,
                    STATUS_FINAL_FAILED,
                    plan=plan,
                    error=raw_error,
                    friendly_reason=plan.friendly_reason,
                    remaining_action=plan.remaining_action,
                )
                deferred_logs.append(
                    f"[automatic repair plan excluded] {Path(iid).name}: "
                    f"{raw_error}"
                )
                continue
            if plan.supported:
                automatic_plans[iid] = plan
                defer_status(
                    iid,
                    STATUS_PENDING,
                    plan=plan,
                )
            else:
                unsupported_plans[iid] = plan
                defer_status(
                    iid,
                    STATUS_FINAL_FAILED,
                    plan=plan,
                    friendly_reason=plan.friendly_reason,
                    remaining_action=plan.remaining_action,
                )
                deferred_logs.append(
                    f"[automatic repair unsupported] {Path(iid).name}: "
                    f"{plan.friendly_reason} | remaining={plan.remaining_action} | "
                    f"details reason_codes={','.join(plan.reason_codes)}"
                )
        bat_handled_ids = set(automatic_plans) | set(unsupported_plans)
        candidate_by_path = {
            os.path.normcase(os.path.abspath(str(iid))).casefold(): iid
            for iid in candidate_iids
        }

        def blocked_dependencies(iid):
            entry = effective_entries.get(iid, {})
            dependencies = []
            for status_column, error_column in (
                ("push_status_kind", "push_status_error"),
                ("blend_status_kind", "blend_status_error"),
                ("spm_status_kind", "spm_status_error"),
            ):
                error = entry.get(error_column)
                if not isinstance(error, dict):
                    continue
                status_kind = str(entry.get(status_column) or "")
                error_kind = str(error.get("kind") or "")
                if status_kind != "dependency_blocked" and not (
                    not status_kind and error_kind == "dependency_blocked"
                ):
                    continue
                blocked_by = error.get("blocked_by")
                if not isinstance(blocked_by, (list, tuple, set)):
                    continue
                for value in blocked_by:
                    key = os.path.normcase(
                        os.path.abspath(str(value))
                    ).casefold()
                    dependency = candidate_by_path.get(key)
                    if dependency and dependency not in dependencies:
                        dependencies.append(dependency)
            return dependencies

        def automatic_dependency_roots(iid):
            memo = {}

            def visit(target, visiting):
                if target in memo:
                    return memo[target]
                if target in visiting:
                    return None
                dependencies = blocked_dependencies(target)
                if not dependencies:
                    return None
                roots = set()
                next_visiting = set(visiting)
                next_visiting.add(target)
                for dependency in dependencies:
                    if dependency in automatic_plans:
                        roots.add(dependency)
                        continue
                    nested = visit(dependency, next_visiting)
                    if not nested:
                        return None
                    roots.update(nested)
                memo[target] = roots
                return roots

            roots = visit(iid, set())
            if not roots:
                return ()
            return tuple(
                candidate
                for candidate in candidate_iids
                if candidate in roots
            )

        resume_after_repairs = {}
        for iid in candidate_iids:
            if iid in bat_handled_ids:
                continue
            roots = automatic_dependency_roots(iid)
            if roots:
                resume_after_repairs[iid] = roots
        bat_handled_ids.update(resume_after_repairs)
        planning_context.counters["dependency_resume_targets"] += len(
            resume_after_repairs
        )
        parent_statuses = {
            iid: UNREAL_PARENT_ABSENT for iid in candidate_iids
        }
        parent_diagnostics = {iid: "" for iid in candidate_iids}
        grouped = {}
        validated_parent_manifests = {}

        for parent_index, iid in enumerate(candidate_iids, start=1):
            planning_context.check_cancel()
            planning_context.publish(
                "parent_grouping",
                current_target=iid,
                scanned=parent_index - 1,
                last_completed=(
                    candidate_iids[parent_index - 2]
                    if parent_index > 1
                    else "durable evidence pass"
                ),
                progress=False,
            )
            entry = effective_entries.get(iid, {})
            push_status_kind = str(entry.get("push_status_kind") or "")
            if (
                push_status_kind == "imported_ok"
                and repair_states[iid].get("current") is True
            ):
                # A content-proven successful ingest is not a failed Unreal
                # parent. Unknown Repair state still takes the existing
                # fail-closed parent path; known stale state rebuilds below.
                continue
            paths = entry.get("push_paths") or {}
            manifest_value = paths.get("manifest")
            checkpoint_value = paths.get("checkpoint")
            if not manifest_value and not checkpoint_value:
                continue
            if not manifest_value or not checkpoint_value:
                parent_statuses[iid] = UNREAL_PARENT_INCOMPLETE
                parent_diagnostics[iid] = (
                    "Unreal parent manifest/checkpoint 불완전 · "
                    "전체 Blender→Push를 명시적으로 실행하세요"
                )
                continue
            if push_status_kind not in UNREAL_RECOVERY_FAILURE_KINDS:
                parent_statuses[iid] = UNREAL_PARENT_INVALID
                parent_diagnostics[iid] = (
                    "Unreal parent는 있으나 retryable ingest 실패 상태가 아님"
                )
                continue
            try:
                with planning_context.span("parent_grouping_validation"):
                    manifest_payload = planning_context.load_json(
                        manifest_value,
                        namespace="parent_manifest",
                    )
                    manifest_identity = (
                        planning_context.parent_cache.identity(
                            manifest_value,
                            namespace="parent_manifest",
                        )
                    )
                    parent_snapshot = validated_parent_manifests.get(
                        manifest_identity
                    )
                    if parent_snapshot is None:
                        parent_snapshot = (
                            load_unreal_recovery_parent_manifest(
                                manifest_value,
                                manifest_payload=manifest_payload,
                            )
                        )
                        validated_parent_manifests[manifest_identity] = (
                            parent_snapshot
                        )
                        planning_context.counters[
                            "parent_manifest_validations"
                        ] += 1
                    else:
                        planning_context.counters[
                            "parent_manifest_validation_cache_hits"
                        ] += 1
                    manifest_path, manifest, items_by_id = parent_snapshot
                    checkpoint = planning_context.load_json(
                        checkpoint_value,
                        namespace="parent_checkpoint",
                    )
                checkpoint_status = str(
                    ((checkpoint.get("items") or {}).get(iid) or {}).get(
                        "status"
                    )
                    or ""
                )
                if checkpoint_status not in UNREAL_RECOVERY_FAILURE_KINDS:
                    raise PushUnrealRecoveryError(
                        "parent checkpoint does not record a retryable "
                        "Unreal failure"
                    )
                if iid not in items_by_id:
                    raise PushUnrealRecoveryError(
                        "selected item is absent from its parent manifest"
                    )
            except (
                OSError,
                TypeError,
                ValueError,
                PushUnrealRecoveryError,
                RetryPlanningSnapshotError,
            ) as exc:
                parent_statuses[iid] = UNREAL_PARENT_INVALID
                parent_diagnostics[iid] = compact_error_message(exc, 160)
                continue

            key = str(manifest_path)
            group = grouped.setdefault(
                key,
                {
                    "parent_manifest": key,
                    "parent_report": str(
                        manifest.get("report_path")
                        or paths.get("batch_report")
                        or ""
                    ),
                    "selected_queue_ids": [],
                    "items_by_id": items_by_id,
                },
            )
            group["selected_queue_ids"].append(iid)
            parent_statuses[iid] = UNREAL_PARENT_CANDIDATE
            planning_context.publish(
                "parent_grouping",
                current_target=iid,
                scanned=parent_index,
                last_completed=iid,
            )

        # The retry planner also runs in contexts that never built the options
        # widgets, so read the force request defensively and default to the
        # ordinary exclusion when it is unavailable.
        force_var = getattr(self, "force_var", None)
        try:
            retry_force_rerun = bool(force_var.get()) if force_var else False
        except Exception:
            retry_force_rerun = False

        def classify(iid):
            return classify_failed_retry(
                effective_entries.get(iid, {}),
                repair_states[iid],
                unreal_parent_status=parent_statuses[iid],
                unreal_parent_diagnostic=parent_diagnostics[iid],
                force_rerun=retry_force_rerun,
            )

        decisions = {}
        with planning_context.span("classification"):
            for classification_index, iid in enumerate(
                candidate_iids,
                start=1,
            ):
                planning_context.check_cancel()
                decisions[iid] = classify(iid)
                planning_context.counters["classifications"] += 1
                planning_context.publish(
                    "classification",
                    current_target=iid,
                    scanned=classification_index,
                    last_completed=iid,
                )
        rebuild_ids = {
            iid
            for iid, decision in decisions.items()
            if decision.classification == BLENDER_REBUILD
            and iid not in bat_handled_ids
        }
        recovery_requests = []

        for group in grouped.values():
            planning_context.check_cancel()
            pending_selected = [
                iid
                for iid in group["selected_queue_ids"]
                if decisions[iid].classification
                == PENDING_UNREAL_VALIDATION
                and iid not in bat_handled_ids
            ]
            if not pending_selected:
                continue
            try:
                with planning_context.span("dependency_closure"):
                    required_ids = unreal_recovery_dependency_closure(
                        group["items_by_id"], pending_selected
                    )
                planning_context.counters["dependency_closures"] += 1
            except PushUnrealRecoveryError as exc:
                for iid in pending_selected:
                    parent_statuses[iid] = UNREAL_PARENT_INVALID
                    parent_diagnostics[iid] = compact_error_message(exc, 160)
                continue

            overlap = set(required_ids) & rebuild_ids
            if overlap:
                names = ", ".join(
                    sorted(Path(value).name for value in overlap)
                )
                for iid in pending_selected:
                    parent_statuses[iid] = (
                        UNREAL_PARENT_DEPENDENCY_REBUILD
                    )
                    parent_diagnostics[iid] = (
                        "Unreal recovery dependency가 Blender rebuild 대상: "
                        + names
                    )
                rebuild_ids.update(pending_selected)
                continue

            source_records = {}
            try:
                for queue_id in sorted(required_ids):
                    planning_context.check_cancel()
                    parent_item = group["items_by_id"][queue_id]
                    with planning_context.span(
                        "immutable_unreal_validation"
                    ):
                        source_record = (
                            self._failed_retry_parent_source_record(
                                queue_id, parent_item
                            )
                        )
                        self._validate_failed_retry_unreal_item_current(
                            queue_id,
                            parent_item,
                            source_record,
                        )
                    planning_context.counters[
                        "immutable_unreal_validations"
                    ] += 1
                    source_records[queue_id] = source_record
            except (
                OSError,
                RuntimeError,
                TypeError,
                ValueError,
                PushUnrealRecoveryError,
            ) as exc:
                for iid in pending_selected:
                    parent_statuses[iid] = UNREAL_PARENT_INVALID
                    parent_diagnostics[iid] = (
                        "immutable Unreal evidence가 current가 아님 · "
                        + compact_error_message(exc, 160)
                        + " · 전체 Blender→Push를 실행하세요"
                    )
                # The manifest dependency graph was structurally valid even
                # though its immutable source proof was not. Preserve that
                # exact closure for the safe full-pipeline fallback instead
                # of excluding the current provider and blocking its tree.
                rebuild_ids.update(required_ids)
                continue

            for iid in pending_selected:
                parent_statuses[iid] = UNREAL_PARENT_CURRENT
            recovery_requests.append({
                "parent_manifest": group["parent_manifest"],
                "parent_report": group["parent_report"],
                "selected_queue_ids": list(pending_selected),
                "source_records": source_records,
            })

        # Reconcile across parent manifests as a fixed point. A queue id can
        # appear as a dependency in an older parent batch while its selected
        # row belongs to a newer batch. Never leave that id in both partitions.
        while True:
            conflicting = next(
                (
                    request
                    for request in recovery_requests
                    if set(request["source_records"]) & rebuild_ids
                ),
                None,
            )
            if conflicting is None:
                break
            overlap = set(conflicting["source_records"]) & rebuild_ids
            names = ", ".join(
                sorted(Path(value).name for value in overlap)
            )
            for iid in conflicting["selected_queue_ids"]:
                parent_statuses[iid] = UNREAL_PARENT_DEPENDENCY_REBUILD
                parent_diagnostics[iid] = (
                    "Unreal recovery dependency가 Blender rebuild 대상: "
                    + names
                )
            rebuild_ids.update(conflicting["selected_queue_ids"])
            recovery_requests.remove(conflicting)

        with planning_context.span("classification"):
            decisions = {
                iid: classify(iid)
                for iid in candidate_iids
            }
        planning_context.counters["classifications"] += len(candidate_iids)
        export_iids = [
            iid
            for iid in candidate_iids
            if (
                decisions[iid].classification == BLENDER_REBUILD
                or iid in rebuild_ids
            )
            and iid not in bat_handled_ids
        ]
        unreal_iids = [
            iid
            for iid in candidate_iids
            if decisions[iid].classification == UNREAL_ONLY
            and iid not in bat_handled_ids
        ]
        skipped = []
        for iid in candidate_iids:
            if iid in bat_handled_ids:
                continue
            decision = decisions[iid]
            if (
                decision.classification in {BLENDER_REBUILD, UNREAL_ONLY}
                or iid in rebuild_ids
            ):
                continue
            prefix = (
                ".blend가 SPM보다 최신 · 제외 (강제 재실행 옵션으로 재빌드 가능)"
                if decision.classification == CURRENT_BLENDER_EXCLUDED
                else "재시도 증거 불완전 · fail closed "
                "(강제 재실행 옵션으로 재빌드 가능)"
            )
            skipped.append(
                f"{Path(iid).name}: {prefix} · "
                f"{decision.reason_code} · {decision.diagnostic}"
            )

        if skipped:
            deferred_logs.append(
                "전체 대상 실패/stale 재시도 제외:\n  - "
                + "\n  - ".join(skipped)
            )
        terminal_details = [
            f"{Path(iid).name}: {plan.friendly_reason}\n"
            "  attempted automatic repair: none\n"
            f"  remaining action: {plan.remaining_action}\n"
            f"  details reason_codes={','.join(plan.reason_codes)}"
            for iid, plan in unsupported_plans.items()
        ]
        eligible_set = set(export_iids) | set(unreal_iids)
        eligible_iids = [
            iid for iid in candidate_iids if iid in eligible_set
        ]
        runnable_set = (
            eligible_set
            | set(automatic_plans)
            | set(resume_after_repairs)
        )
        runnable_ids = [
            iid for iid in candidate_iids if iid in runnable_set
        ]
        if not runnable_ids:
            if tracker is not None:
                for iid in candidate_iids:
                    if iid in unsupported_plans:
                        repair_plan = unsupported_plans[iid]
                        tracker.transition(
                            iid,
                            RETRY_STAGE_FAILED,
                            diagnostic=repair_plan.friendly_reason,
                            terminal_reason=(
                                next(iter(repair_plan.reason_codes), None)
                                or "automatic_repair_unsupported"
                            ),
                            outcome=RETRY_STAGE_FAILED,
                        )
                    else:
                        decision = decisions[iid]
                        tracker.transition(
                            iid,
                            RETRY_STAGE_BLOCKED,
                            diagnostic=decision.diagnostic,
                            terminal_reason=decision.reason_code,
                            outcome=RETRY_STAGE_BLOCKED,
                        )
                tracker.finalize("no retryable targets")
            return {
                "jobs": [],
                "skipped": terminal_details + skipped,
                "selected_iids": candidate_iids,
                "cfg": cfg,
                "tracker": tracker,
                "deferred_status_updates": deferred_status_updates,
                "deferred_logs": deferred_logs,
            }

        with planning_context.span("snapshot_commit"):
            inventory = copy.deepcopy(dict(inventory_snapshot or {}))
            targets = [
                inventory[iid]
                for iid in runnable_ids
                if iid in inventory
            ]
        planning_context.publish(
            "snapshot_commit",
            scanned=len(candidate_iids),
            last_completed="immutable queue plan",
        )
        targets_by_id = {str(item["spm"]): item for item in targets}
        action_kind = "failed_blender_export_and_unreal_retry"
        jobs = []

        def eligibility_receipt(ids):
            return {
                "schema_version": RETRY_ELIGIBILITY_SCHEMA_VERSION,
                "items": [
                    {
                        "queue_id": iid,
                        **(
                            {
                                **decisions[iid].metadata(),
                                "classification": BLENDER_REBUILD,
                                "reason_code": (
                                    "unreal_dependency_full_rebuild_fallback"
                                ),
                                "diagnostic": (
                                    "Exact immutable parent dependency is "
                                    "included in the full rebuild fallback"
                                ),
                                "scheduled_as_dependency": True,
                            }
                            if iid in rebuild_ids
                            and decisions[iid].classification
                            != BLENDER_REBUILD
                            else decisions[iid].metadata()
                        ),
                    }
                    for iid in ids
                ],
            }

        # Run immutable Unreal recovery first.  A later Blender/export retry
        # may legitimately rewrite other artifacts; it must not do so before
        # the recovery partition proves the parent identities it selected.
        unreal_targets = [
            targets_by_id[iid] for iid in unreal_iids if iid in targets_by_id
        ]
        if unreal_targets:
            unreal_ids = [str(item["spm"]) for item in unreal_targets]
            metadata = {
                "schema_version": 1,
                "kind": action_kind,
                "partition": "unreal_ingest",
                "execution_path": "immutable_unreal_only",
                "selected_queue_ids": unreal_ids,
                "eligibility": eligibility_receipt(unreal_ids),
            }
            if tracker is not None:
                tracker.assign_partition(
                    "unreal_ingest", unreal_ids, "immutable_unreal_only"
                )
                metadata.update({
                    "progress_run_id": tracker.run_id,
                    "progress_receipt_path": str(tracker.path),
                })
            jobs.append({
                "label": (
                    "실패 재시도 · Unreal-only current-code · "
                    f"{len(unreal_targets)}개"
                ),
                "mode": "unreal_recovery",
                "phase": "push",
                "terminal_phase": "push",
                "selected_scope": True,
                "targets": unreal_targets,
                "inventory": inventory,
                "cfg": cfg,
                "force_rerun": False,
                "push_transport": "headless",
                "recovery_requests": recovery_requests,
                "retry_metadata": metadata,
                "_retry_progress_tracker": tracker,
            })

        # The queue label describes the current execution. Historical
        # failed/stale classification is shown only in the retry receipt UI.
        if unreal_targets:
            jobs[-1]["label"] = (
                "Retry run · Unreal-only current-code · "
                f"{len(unreal_targets)} targets"
            )

        repair_root_targets = [
            targets_by_id[iid]
            for iid in candidate_iids
            if iid in automatic_plans and iid in targets_by_id
        ]
        if repair_root_targets:
            repair_ids = [str(item["spm"]) for item in repair_root_targets]
            resume_ids = [
                iid
                for iid in candidate_iids
                if iid in resume_after_repairs and iid in targets_by_id
            ]
            repair_job_ids = [
                iid
                for iid in candidate_iids
                if iid in set(repair_ids) | set(resume_ids)
            ]
            repair_targets = [targets_by_id[iid] for iid in repair_job_ids]
            metadata = {
                "schema_version": 1,
                "kind": action_kind,
                "partition": "exact_bat_repair",
                "execution_path": (
                    "exact_bat_then_fresh_reaudit_then_blender_unreal"
                ),
                "selected_queue_ids": repair_job_ids,
                "eligibility": {
                    "schema_version": 1,
                    "items": [
                        {
                            "queue_id": iid,
                            **(
                                {
                                    "repair_plan": automatic_plans[
                                        iid
                                    ].metadata(),
                                }
                                if iid in automatic_plans
                                else {
                                    "reason_code": (
                                        "required_cluster_repaired_resume"
                                    ),
                                    "resume_after_repairs": list(
                                        resume_after_repairs[iid]
                                    ),
                                }
                            ),
                        }
                        for iid in repair_job_ids
                    ],
                },
            }
            if tracker is not None:
                tracker.assign_partition(
                    "exact_bat_repair", repair_job_ids,
                    metadata["execution_path"],
                )
                metadata.update({
                    "progress_run_id": tracker.run_id,
                    "progress_receipt_path": str(tracker.path),
                })
            jobs.append({
                "label": (
                    "자동 복구 · exact BAT → 재검증 → "
                    f"차단 소비자 재개 → Blender/Unreal · "
                    f"{len(repair_targets)}"
                ),
                "mode": "failed_retry_repair",
                "phase": "push",
                "terminal_phase": "push",
                "selected_scope": True,
                "targets": repair_targets,
                "inventory": inventory,
                "cfg": cfg,
                "force_rerun": True,
                "push_transport": "headless",
                "repair_plans": [
                    automatic_plans[iid].metadata()
                    for iid in repair_ids
                ],
                "resume_after_repairs": {
                    iid: list(resume_after_repairs[iid])
                    for iid in resume_ids
                },
                "retry_metadata": metadata,
                "_retry_progress_tracker": tracker,
            })

        export_targets = [
            targets_by_id[iid] for iid in export_iids if iid in targets_by_id
        ]
        if export_targets:
            export_ids = [str(item["spm"]) for item in export_targets]
            metadata = {
                "schema_version": 1,
                "kind": action_kind,
                "partition": "blender_export",
                "execution_path": "blender_send2ue_then_unreal",
                "selected_queue_ids": export_ids,
                "eligibility": eligibility_receipt(export_ids),
            }
            if tracker is not None:
                tracker.assign_partition(
                    "blender_export",
                    export_ids,
                    "blender_send2ue_then_unreal",
                )
                metadata.update({
                    "progress_run_id": tracker.run_id,
                    "progress_receipt_path": str(tracker.path),
                })
            jobs.append({
                "label": (
                    "실패/stale 재시도 · Blender/Send2UE→Unreal · "
                    f"{len(export_targets)}개"
                ),
                "mode": "pipeline",
                "phase": "push",
                "terminal_phase": "push",
                "selected_scope": True,
                "targets": export_targets,
                "inventory": inventory,
                "cfg": cfg,
                "force_rerun": True,
                "push_transport": "headless",
                "retry_metadata": metadata,
                "_retry_progress_tracker": tracker,
            })

        if export_targets:
            jobs[-1]["label"] = (
                "Retry run · Blender/Send2UE→Unreal · "
                f"{len(export_targets)} targets"
            )

        missing_ids = [
            iid for iid in runnable_ids if iid not in targets_by_id
        ]
        if tracker is not None:
            for iid in candidate_iids:
                if iid in runnable_set and iid not in missing_ids:
                    continue
                if iid in unsupported_plans:
                    repair_plan = unsupported_plans[iid]
                    tracker.transition(
                        iid,
                        RETRY_STAGE_FAILED,
                        diagnostic=repair_plan.friendly_reason,
                        terminal_reason=(
                            next(iter(repair_plan.reason_codes), None)
                            or "automatic_repair_unsupported"
                        ),
                        outcome=RETRY_STAGE_FAILED,
                    )
                else:
                    decision = decisions[iid]
                    tracker.transition(
                        iid,
                        RETRY_STAGE_BLOCKED,
                        diagnostic=(
                            "selected target disappeared from the planning snapshot"
                            if iid in missing_ids
                            else decision.diagnostic
                        ),
                        terminal_reason=(
                            "planning_target_missing"
                            if iid in missing_ids
                            else decision.reason_code
                        ),
                        outcome=RETRY_STAGE_BLOCKED,
                    )
        return {
            "jobs": jobs,
            "skipped": terminal_details + skipped,
            "selected_iids": candidate_iids,
            "cfg": cfg,
            "tracker": tracker,
            "deferred_status_updates": deferred_status_updates,
            "deferred_logs": deferred_logs,
        }

    def _commit_failed_retry_plan(self, plan):
        """Main-thread half of planning: message boxes, Tk, and enqueue."""
        if not isinstance(plan, dict):
            return None
        retry_request = copy.deepcopy(plan.get("_retry_request") or {})
        retry_scope = str(retry_request.get("scope") or "all")
        dialog_title = str(
            retry_request.get("dialog_title") or "전체 실패 이력 재시도"
        )
        ui_thread_ident = getattr(
            self,
            "_ui_thread_ident",
            threading.main_thread().ident,
        )
        if threading.get_ident() != ui_thread_ident:
            raise RuntimeError(
                "retry planning commit must run on the Tk owner thread"
            )
        current = threading.current_thread()
        workers = getattr(self, "_retry_planning_workers", None)
        if isinstance(workers, set):
            finished_workers = {
                worker for worker in workers if not worker.is_alive()
            }
            workers.difference_update(finished_workers)
            workers.discard(current)
        tracker = plan.get("tracker")
        error = plan.get("error")
        if self.stop_flag.is_set() and tracker is not None:
            tracker.finish_planning(
                RETRY_STAGE_CANCELLED,
                "operator cancelled during retry planning",
            )
            if not getattr(self, "active_batch_job", None) and not getattr(
                self, "pending_batch_jobs", ()
            ):
                self._set_batch_queue_controls(False)
            return None
        if tracker is not None and not tracker.claim_planning_commit():
            # A duplicate ready event, cooperative cancellation, or restored
            # terminal receipt must never enqueue the same immutable plan.
            return None
        if plan.get("_planning_cache_reused"):
            with self.state_lock:
                current_state = copy.deepcopy(dict(self.state or {}))
            current_signature = planning_input_signature(
                plan.get("selected_iids") or (),
                current_state,
                plan.get("cfg") or {},
                plan.get("_planning_inventory_snapshot") or {},
            )
            if current_signature != plan.get("_planning_input_signature"):
                if tracker is not None:
                    tracker.finish_planning(
                        RETRY_STAGE_CANCELLED,
                        "cached retry plan input changed before commit; replanning",
                    )
                self._skip_retry_plan_cache_once = True
                restart = (
                    self.start_checked_failed_results_retry
                    if retry_scope == "checked"
                    else self.start_failed_results_retry
                )
                self.root.after(0, restart)
                return None
        if error:
            if tracker is not None:
                tracker.finish_planning(
                    RETRY_STAGE_FAILED,
                    error,
                )
            messagebox.showerror(
                "실패 재시도 planning 실패",
                str(error),
                parent=self.root,
            )
            if not getattr(self, "active_batch_job", None) and not getattr(
                self, "pending_batch_jobs", ()
            ):
                self._set_batch_queue_controls(False)
            return None
        diagnostics = plan.get("planning_diagnostics") or {}
        if diagnostics:
            self.log(
                "[retry planning diagnostics] "
                + json.dumps(
                    diagnostics,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
        for update in plan.get("deferred_status_updates") or ():
            self._set_failed_retry_automatic_status(
                update["iid"],
                update["status"],
                **dict(update.get("kwargs") or {}),
            )
        for message in plan.get("deferred_logs") or ():
            self.log(message)
        cfg = dict(plan.get("cfg") or {})
        artifact = plan.get("_planning_cache_artifact")
        planning_session_id = plan.get("_planning_session_id")
        if (
            tracker is not None
            and planning_session_id is not None
            and isinstance(artifact, dict)
        ):
            with self.state_lock:
                committed_state = copy.deepcopy(dict(self.state or {}))
            committed_signature = planning_input_signature(
                plan.get("selected_iids") or (),
                committed_state,
                cfg,
                plan.get("_planning_inventory_snapshot") or {},
            )
            original_signature = plan.get("_planning_input_signature") or {}
            if committed_signature.get("file_identities_sha256") == (
                original_signature.get("file_identities_sha256")
            ):
                tracker.store_planning_cache(
                    planning_session_id,
                    input_signature=committed_signature,
                    artifact=artifact,
                    side_effects_committed=True,
                )
        try:
            save_config(cfg)
        except Exception as exc:
            error = compact_error_message(exc)
            if tracker is not None:
                tracker.finish_planning(
                    RETRY_STAGE_FAILED,
                    "retry plan config commit failed: " + error,
                )
            messagebox.showerror(
                "실패 재시도 planning commit 실패",
                error,
                parent=getattr(self, "root", None),
            )
            if not getattr(self, "active_batch_job", None) and not getattr(
                self, "pending_batch_jobs", ()
            ):
                self._set_batch_queue_controls(False)
            return None
        jobs = list(plan.get("jobs") or [])
        if not jobs:
            if tracker is not None:
                tracker.complete_planning_commit()
            messagebox.showinfo(
                dialog_title,
                (
                    "체크 항목에 재시도 가능한 실패/stale 이력이 없습니다."
                    if retry_scope == "checked"
                    else "현재 목록 전체에 재시도 가능한 실패/stale 이력이 없습니다."
                )
                + "\n\n"
                + "\n".join((plan.get("skipped") or [])[:8]),
                parent=getattr(self, "root", None),
            )
            if not getattr(self, "active_batch_job", None) and not getattr(
                self, "pending_batch_jobs", ()
            ):
                self._set_batch_queue_controls(False)
            return None
        enqueued = []
        for job in jobs:
            if self.stop_flag.is_set():
                if tracker is not None:
                    partition = (job.get("retry_metadata") or {}).get(
                        "partition", "unclassified"
                    )
                    tracker.mark_partition_terminal(
                        partition,
                        RETRY_STAGE_CANCELLED,
                        "operator cancelled before plan partition enqueue",
                    )
                continue
            local_id = self._enqueue_batch_job(job)
            if local_id is not None:
                enqueued.append(local_id)
            elif tracker is not None:
                partition = (job.get("retry_metadata") or {}).get(
                    "partition", "unclassified"
                )
                tracker.mark_partition_terminal(
                    partition,
                    RETRY_STAGE_FAILED,
                    "shared queue registration failed; retry not executed",
                )
        if tracker is not None:
            tracker.complete_planning_commit()
        return enqueued

    def start_failed_unreal_retry(self):
        """Backward-compatible entry point for existing UI integrations."""
        return self.start_failed_results_retry()

    def start_full_pipeline(self):
        self._close_cell_editor()
        target_iids = list(self.items)
        if not target_iids:
            messagebox.showinfo("SK Batch", "현재 목록에 항목이 없습니다.")
            return
        cfg = dict(self._collect_cfg())
        save_config(cfg)
        inventory, targets = self._snapshot_batch_request(target_iids)
        job = {
            "label": f"목록 전체 자동 ①→②→③ · {len(targets)}개",
            "mode": "pipeline",
            "phase": "push",
            "terminal_phase": "push",
            "selected_scope": False,
            "targets": targets,
            "inventory": inventory,
            "cfg": cfg,
            "force_rerun": bool(self.force_var.get()),
            "push_transport": (
                "headless"
                if cfg.get("night_headless", True)
                else cfg.get("push_transport", "rpc")
            ),
        }
        self._enqueue_batch_job(job)

    def _pipeline_dependency_blocks(
        self,
        targets,
        dependency_map,
        root_failed_ids,
    ):
        """Block only failed dependencies whose saved output is not current."""
        failed = {str(value) for value in root_failed_ids}
        blocked = {}
        verdicts_by_target = {}
        reused_by_target = self.__dict__.setdefault(
            "_pipeline_dependency_reuse_evidence", {}
        )
        for item in targets:
            iid = str(item["spm"])
            causes = []
            verdicts = {}
            for dependency in dependency_map.get(iid, ()):
                if dependency not in failed:
                    continue
                verdict = self._dependency_artifact_verdict(
                    dependency,
                    phase="blender",
                )
                verdicts[dependency] = verdict
                if verdict["status"] == "current":
                    reused_by_target.setdefault(iid, {})[
                        dependency
                    ] = copy.deepcopy(verdict)
                    self.log(
                        "[의존 산출물 재사용] "
                        f"{Path(iid).name}: {Path(dependency).name}의 "
                        "기존 Blender 산출물이 current이므로 이번 실패 "
                        "행으로 consumer를 차단하지 않습니다."
                    )
                    continue
                causes.append(dependency)
            if causes:
                blocked[iid] = tuple(causes)
                verdicts_by_target[iid] = verdicts
        self._pipeline_dependency_artifact_verdicts = verdicts_by_target
        return blocked

    @staticmethod
    def _dependency_artifact_state_label(status):
        return {
            "missing": "산출물 없음",
            "stale": "산출물 낡음",
            "waiting": "산출물 생성/반영 대기",
            "current": "산출물 current",
        }.get(str(status or ""), "산출물 검증 실패")

    @staticmethod
    def _artifact_path_identity(path):
        candidate = Path(path).expanduser().absolute()
        try:
            return {
                "path": str(candidate),
                "exists": True,
                **sampled_file_content_snapshot(candidate),
            }
        except FileNotFoundError:
            return {"path": str(candidate), "exists": False}
        except OSError as exc:
            return {
                "path": str(candidate),
                "exists": None,
                "error": compact_error_message(exc, 160),
            }

    def _dependency_artifact_identity(self, dependency, phase, verdict):
        paths = {
            "producer_spm": Path(dependency),
            "producer_blend": blend_path_for(dependency),
        }
        if phase == "blender":
            paths["producer_repair_report"] = repair_pipeline_report_path(
                Path(dependency)
            )
        manifest = str((verdict or {}).get("manifest") or "")
        if manifest:
            paths["producer_push_manifest"] = Path(manifest)
        return {
            name: self._artifact_path_identity(path)
            for name, path in paths.items()
        }

    @staticmethod
    def _saved_dependency_blender_receipt_current(dependency):
        """Read-only content-key check for an existing producer output."""

        try:
            report = load_current_repair_pipeline_report(
                Path(dependency),
                migrate_legacy=False,
            )
        except (OSError, RuntimeError, TypeError, ValueError):
            return False
        return str(
            (report.get("handoff_preflight") or {}).get("status") or ""
        ) in {"ok", "source_review"}

    def _current_push_output_artifact_state(self, dependency):
        """Validate persisted export/import evidence despite a new failed row."""
        iid = str(dependency)
        entry = self._failed_retry_state_entry(iid)
        source_cache = entry.get("push_source_fingerprint_cache") or {}
        export_cache = entry.get("push_export_cache") or {}
        if (
            source_cache.get("version")
            != PUSH_SOURCE_FINGERPRINT_CACHE_VERSION
            or not source_cache.get("fingerprint")
        ):
            return {
                "status": "missing",
                "phase": "push",
                "reason": "current Push 입력 영수증이 없습니다.",
            }
        try:
            current_snapshot = push_source_snapshot(
                blend_path_for(dependency),
                self._push_source_dependency_paths(dependency),
            )
        except (OSError, ValueError, KeyError) as exc:
            return {
                "status": "missing",
                "phase": "push",
                "reason": f"Push 입력 파일을 확인할 수 없습니다: {exc}",
            }
        if source_cache.get("snapshot") != current_snapshot:
            return {
                "status": "stale",
                "phase": "push",
                "reason": "Blender 또는 Push 입력이 영수증 이후 변경되었습니다.",
            }
        if export_cache.get("source_fingerprint") != source_cache.get(
            "fingerprint"
        ):
            return {
                "status": "stale",
                "phase": "push",
                "reason": "Push export 영수증이 current 입력과 일치하지 않습니다.",
            }
        manifest_path = Path(export_cache.get("manifest") or "")
        if not manifest_path.is_file():
            return {
                "status": "missing",
                "phase": "push",
                "reason": f"Push export manifest가 없습니다: {manifest_path}",
            }
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest_item = next(
                row for row in (manifest.get("items") or ())
                if str((row or {}).get("queue_id")) == iid
            )
        except (OSError, ValueError, StopIteration, TypeError) as exc:
            return {
                "status": "stale",
                "phase": "push",
                "reason": f"Push export manifest의 exact 항목이 유효하지 않습니다: {exc}",
            }
        if not manifest_item_files_match(manifest_item):
            return {
                "status": "stale",
                "phase": "push",
                "reason": "Push export 파일 fingerprint가 current manifest와 다릅니다.",
            }
        export_fingerprint = export_cache.get("fingerprint")
        if not export_fingerprint or manifest_item.get(
            "fingerprint"
        ) != export_fingerprint:
            return {
                "status": "stale",
                "phase": "push",
                "reason": "Push export fingerprint가 manifest와 일치하지 않습니다.",
            }
        if entry.get("push_import_fingerprint") == export_fingerprint:
            return {
                "status": "current",
                "phase": "push",
                "reason": "current export가 Unreal import 영수증과 일치합니다.",
                "manifest": str(manifest_path),
                "fingerprint": export_fingerprint,
            }
        return {
            "status": "waiting",
            "phase": "push",
            "reason": "current export는 있으나 Unreal import 완료 영수증이 없습니다.",
            "manifest": str(manifest_path),
            "fingerprint": export_fingerprint,
        }

    def _dependency_artifact_verdict(self, dependency, *, phase):
        """Return artifact truth independently of this run's failure row."""
        dependency = str(dependency)
        cache = self.__dict__.setdefault(
            "_pipeline_dependency_artifact_cache", {}
        )
        key = (str(phase), _normalized_path(dependency))
        if key in cache:
            return copy.deepcopy(cache[key])
        if phase == "push":
            verdict = self._current_push_output_artifact_state(dependency)
        else:
            blend = blend_path_for(Path(dependency))
            if not blend.is_file():
                verdict = {
                    "status": "missing",
                    "phase": "blender",
                    "output_kind": "missing_blend",
                    "reason": f"Blender 산출물이 없습니다: {blend}",
                }
            elif self._saved_dependency_blender_receipt_current(
                Path(dependency)
            ):
                verdict = {
                    "status": "current",
                    "phase": "blender",
                    "reason": (
                        "saved Blender/Repair 영수증이 current producer "
                        "SPM content key와 일치합니다."
                    ),
                }
            else:
                verdict = {
                    "status": "stale",
                    "phase": "blender",
                    "output_kind": "repair_receipt_not_current",
                    "reason": (
                        "Blender/Repair 영수증이 current producer SPM "
                        "content key와 일치하지 않습니다."
                    ),
                }
        verdict["artifact_identity"] = self._dependency_artifact_identity(
            dependency,
            phase,
            verdict,
        )
        cache[key] = copy.deepcopy(verdict)
        return verdict

    @staticmethod
    def _filter_pipeline_excluded_targets(targets, excluded_ids):
        excluded = {str(value) for value in excluded_ids}
        return [
            item for item in targets
            if str(item["spm"]) not in excluded
        ]

    def _recorded_failure_reason(self, spm, max_chars=180, _seen=None):
        """Return the recorded failure text for one already-failed row.

        A ``dependency_blocked`` column has no cause of its own -- it only
        records which upstream rows it was blocked by. Stopping there just
        repeats a generic required-Cluster block one hop away from the
        actual error, so walk ``blocked_by`` to the real failure instead.
        """
        seen = _seen if _seen is not None else set()
        columns = ("blend", "push", "spm")

        def new_frame(value):
            key = str(value)
            if key in seen:
                return None
            seen.add(key)
            with self.state_lock:
                entry = self.state.get(key)
                entry = dict(entry) if isinstance(entry, dict) else {}
            return {
                "entry": entry,
                "column_index": 0,
                "waiting": None,
            }

        first = new_frame(spm)
        if first is None:
            return ""
        stack = [first]

        def finish_frame(result):
            stack.pop()
            if not stack:
                return True, result
            waiting = stack[-1]["waiting"]
            if result:
                waiting["nested"].append(result)
            waiting["index"] += 1
            return False, ""

        while stack:
            frame = stack[-1]
            waiting = frame["waiting"]
            if waiting is not None:
                if waiting["index"] < len(waiting["blocked_by"]):
                    child = new_frame(
                        waiting["blocked_by"][waiting["index"]]
                    )
                    if child is None:
                        waiting["index"] += 1
                    else:
                        stack.append(child)
                    continue
                nested = sorted(set(waiting["nested"]))
                frame["waiting"] = None
                if nested:
                    result = compact_error_message(
                        " | ".join(nested), max_chars
                    )
                    done, result = finish_frame(result)
                    if done:
                        return result
                continue

            if frame["column_index"] >= len(columns):
                done, result = finish_frame("")
                if done:
                    return result
                continue

            column = columns[frame["column_index"]]
            frame["column_index"] += 1
            entry = frame["entry"]
            kind = entry.get(f"{column}_status_kind")
            if kind in {None, "ok", "skipped"}:
                continue
            error_entry = entry.get(f"{column}_status_error")
            if kind == "dependency_blocked":
                blocked_by = (
                    error_entry.get("blocked_by")
                    if isinstance(error_entry, dict)
                    else None
                )
                if not isinstance(blocked_by, (list, tuple, set)):
                    blocked_by = ()
                frame["waiting"] = {
                    "blocked_by": list(blocked_by),
                    "index": 0,
                    "nested": [],
                }
                continue
            recorded = error_entry
            if isinstance(recorded, dict):
                recorded = recorded.get("message")
            if not isinstance(recorded, str) or not recorded.strip():
                recorded = entry.get(f"{column}_status")
            if not isinstance(recorded, str) or not recorded.strip():
                continue
            result = compact_error_message(recorded.strip(), max_chars)
            done, result = finish_frame(result)
            if done:
                return result
        return ""

    def _record_pipeline_dependency_block(
        self,
        item,
        column,
        blocked_sources,
        *,
        persist,
        publish_repair_contract=False,
        dependency_verdicts=None,
    ):
        iid = str(item["spm"])
        root_decisions = []
        for value in sorted(blocked_sources, key=lambda row: Path(row).name):
            entry = self._failed_retry_state_entry(value)
            decision = repair_ui_decision(entry)
            root_decisions.append((Path(value).name, decision))
        automatic = bool(root_decisions) and all(
            decision["status"] == REPAIR_UI_AUTOMATIC
            for _, decision in root_decisions
        )
        dependency_verdicts = (
            dependency_verdicts
            if isinstance(dependency_verdicts, dict)
            else {}
        )
        status_prefix = "자동 복구 대상" if automatic else "최종 차단"
        names = ", ".join(name for name, _ in root_decisions)
        artifact_causes = []
        artifact_actions = []
        for value in blocked_sources:
            verdict = dependency_verdicts.get(value) or {}
            status = str(verdict.get("status") or "stale")
            label = self._dependency_artifact_state_label(status)
            artifact_causes.append(
                f"{Path(value).name}: {label} · "
                f"{verdict.get('reason') or 'current 증거 없음'}"
            )
            artifact_actions.append(
                f"{Path(value).name}의 exact 산출물을 다시 생성한 뒤 "
                "current 영수증으로 재검증"
            )
        root_causes = " | ".join(artifact_causes) or " | ".join(
            f"{name}: {decision['reason']}"
            for name, decision in root_decisions
        ) or "상위 Cluster의 current 산출물 증거를 확인하지 못했습니다."
        root_actions = " | ".join(dict.fromkeys(artifact_actions)) or (
            " | ".join(dict.fromkeys(
                decision["action"] for _, decision in root_decisions
            ))
            or "상위 Cluster의 current 산출물 증거를 다시 생성해야 합니다."
        )
        reason = (
            f"필수 producer 산출물 · {names} · 원인: {root_causes} · "
            f"조치: {root_actions}"
        )
        self._record_phase_status(
            iid,
            column,
            f"{status_prefix}: {reason}",
            "dependency_blocked",
            reason,
            details={
                "blocked_by": list(blocked_sources),
                "repair_disposition": (
                    REPAIR_UI_AUTOMATIC if automatic else REPAIR_UI_BLOCKED
                ),
                "root_repair_decisions": {
                    name: copy.deepcopy(decision)
                    for name, decision in root_decisions
                },
                "dependency_artifacts": copy.deepcopy(
                    dependency_verdicts
                ),
            },
            persist=persist,
        )
        if publish_repair_contract:
            self._publish_repair_stage_contract(
                item["spm"],
                ready=False,
                reason=reason,
                kind="dependency_blocked",
            )
        self.log(f"[{status_prefix}] {Path(iid).name}: {reason}")

    def _record_pipeline_planned_exclusion(self, target_spm, error):
        """Persist one target-local block without failing a shared producer."""
        target = Path(target_spm)
        iid = str(target)
        reason_token = str(
            getattr(error, "reason_token", None)
            or (getattr(error, "report", {}) or {}).get("reason_token")
            or "target_live_delivery_blocked"
        )
        evidence = copy.deepcopy(
            getattr(error, "evidence", None)
            or (getattr(error, "report", {}) or {}).get("evidence")
            or {}
        )
        if getattr(error, "report_file", None):
            evidence.setdefault("report", str(error.report_file))
        if getattr(error, "log_file", None):
            evidence.setdefault("log", str(error.log_file))
        record = {
            "target": iid,
            "target_name": target.name,
            "outcome": "planned_excluded",
            "reason_token": reason_token,
            "evidence": evidence,
        }
        with self.state_lock:
            planned = self.__dict__.setdefault(
                "_pipeline_planned_exclusions", {}
            )
            planned[iid] = copy.deepcopy(record)
        details = {
            "reason_token": reason_token,
            "evidence": copy.deepcopy(evidence),
        }
        decision = repair_ui_decision({
            "reason_token": reason_token,
            "evidence": evidence,
        })
        details["repair_disposition"] = decision["status"]
        details["reason_ko"] = decision["reason"]
        details["action_ko"] = decision["action"]
        status_summary = target_planned_exclusion_summary(
            target,
            reason_token,
            evidence,
        )
        # 자동 복구 대상 is a promise, so it may only appear before a repair
        # runs.  By the time an exclusion is recorded the attempt has already
        # been made, declined, or ruled out, and the row must say which (#160).
        attempt = evidence.get("repair_attempt")
        attempt_status = str(
            (attempt or {}).get("status") or ""
        ) if isinstance(attempt, dict) else ""
        if attempt_status in {"failed", "repaired_but_reaudit_blocked"}:
            status_prefix = "자동 복구 실패"
        elif attempt_status:
            status_prefix = "최종 차단"
        elif decision["status"] == REPAIR_UI_AUTOMATIC:
            status_prefix = "복구 계획됨"
        else:
            status_prefix = "최종 차단"
        self._record_phase_status(
            iid,
            "blend_status",
            f"{status_prefix}: Generator/Cluster Sync · {status_summary}",
            "planned_excluded",
            f"원인: {decision['reason']} · 조치: {decision['action']}",
            details=details,
            persist=False,
        )
        self._record_phase_status(
            iid,
            "push_status",
            f"{status_prefix}: Push 대기 · {status_summary}",
            "planned_excluded",
            f"원인: {decision['reason']} · 조치: {decision['action']}",
            details=details,
            persist=True,
        )
        self.log(
            f"[{status_prefix}] {status_summary}"
        )

    @staticmethod
    def _failure_result_from_entry(entry, default_token="item_failed"):
        entry = dict(entry) if isinstance(entry, dict) else {}
        for column in ("push_status", "blend_status", "spm_status"):
            kind = str(entry.get(f"{column}_kind") or "")
            error = entry.get(f"{column}_error")
            error = dict(error) if isinstance(error, dict) else {}
            if not kind and not error:
                continue
            reason_token = str(
                error.get("reason_token") or kind or default_token
            )
            evidence = {
                key: copy.deepcopy(value)
                for key, value in error.items()
                if key not in {"time", "kind", "reason_token"}
            }
            return reason_token, evidence
        return default_token, {}

    def _target_failure_result(self, iid, default_token="item_failed"):
        context = self._failed_retry_planning_context()
        state = getattr(self, "state", {}) or {}
        lock = getattr(self, "state_lock", None)
        if context is not None:
            entry = context.entry(iid)
        elif lock is None:
            entry = state.get(str(iid))
        else:
            with lock:
                entry = state.get(str(iid))
        return self._failure_result_from_entry(entry, default_token)

    def _target_failure_kind(self, iid):
        state = getattr(self, "state", {}) or {}
        entry = state.get(str(iid))
        entry = dict(entry) if isinstance(entry, dict) else {}
        for column in ("push_status", "blend_status", "spm_status"):
            kind = str(entry.get(f"{column}_kind") or "")
            if kind:
                return kind
        return ""

    @staticmethod
    def _target_outcome_for_kind(kind, message=""):
        """Map one durable status kind to its authoritative result class."""
        normalized = str(kind or "").strip().casefold()
        diagnostic = str(message or "").strip().casefold()
        if "사용자 중지" in diagnostic or "operator cancel" in diagnostic:
            return "cancelled"
        if normalized in {"completed", "imported_ok", "ready"}:
            return "completed"
        if normalized in {
            "exported_pending_unreal",
            "importing",
            "dependency_waiting",
        }:
            return "pending_unreal"
        if normalized in {"cancelled", "stopped"}:
            return "cancelled"
        if normalized == "owner_lost":
            return "owner_lost"
        if normalized in PLANNED_EXCLUSION_KINDS:
            return "planned_excluded"
        if normalized in {
            "dependency_blocked",
            "manual_required",
            "not_run",
            "not_run_unreal",
            "recovery_blocked",
        }:
            return "blocked"
        if normalized:
            return "failed"
        return None

    def _target_authoritative_result(self, iid, phase=None):
        """Project the latest durable phase state without relabeling it failed."""
        phase_columns = {
            "check": ("spm_status",),
            "spm": ("spm_status",),
            "blender": ("blend_status", "spm_status"),
            "push": ("push_status", "blend_status", "spm_status"),
        }
        columns = phase_columns.get(
            str(phase or ""),
            ("push_status", "blend_status", "spm_status"),
        )
        context = self._failed_retry_planning_context()
        state = getattr(self, "state", {}) or {}
        lock = getattr(self, "state_lock", None)
        if context is not None:
            entry = copy.deepcopy(context.entry(iid))
        elif lock is None:
            entry = copy.deepcopy(state.get(str(iid), {}))
        else:
            with lock:
                entry = copy.deepcopy(state.get(str(iid), {}))
        for column in columns:
            error = entry.get(f"{column}_error")
            error = error if isinstance(error, dict) else {}
            result = entry.get(f"{column}_result")
            result = result if isinstance(result, dict) else {}
            kind = str(
                entry.get(f"{column}_kind")
                or result.get("kind")
                or error.get("kind")
                or ""
            )
            message = str(
                result.get("message")
                or error.get("message")
                or entry.get(column)
                or ""
            )
            outcome = self._target_outcome_for_kind(kind, message)
            if outcome is None:
                continue
            reason_token = None
            if outcome == "pending_unreal":
                reason_token = "exported_pending_unreal"
            elif outcome == "cancelled":
                reason_token = "operator_cancelled"
            elif outcome != "completed":
                reason_token = str(
                    error.get("reason_token")
                    or result.get("reason_token")
                    or kind
                    or outcome
                )
            evidence = {
                "durable_kind": kind,
                "status_column": column,
            }
            if message:
                evidence["message"] = message
            for source in (error, result):
                for key, value in source.items():
                    if key not in {"time", "kind", "message", "reason_token"}:
                        evidence[key] = copy.deepcopy(value)
            return {
                "target": str(iid),
                "target_name": Path(str(iid)).name,
                "outcome": outcome,
                "reason_token": reason_token,
                "evidence": evidence,
            }
        return None

    @staticmethod
    def _count_target_outcomes(outcomes):
        return {
            "completed_count": sum(
                row.get("outcome") == "completed" for row in outcomes
            ),
            "pending_count": sum(
                row.get("outcome") == "pending_unreal" for row in outcomes
            ),
            "cancelled_count": sum(
                row.get("outcome") == "cancelled" for row in outcomes
            ),
            "blocked_count": sum(
                row.get("outcome") in {"blocked", "planned_excluded"}
                for row in outcomes
            ),
            "owner_lost_count": sum(
                row.get("outcome") == "owner_lost" for row in outcomes
            ),
            "failed_count": sum(
                row.get("outcome") == "failed" for row in outcomes
            ),
        }

    def _summarize_phase_targets(self, targets, phase=None):
        """Build the persisted queue result for a non-pipeline phase."""
        failed_ids = set(
            getattr(self, "_phase_failed_items", set()) or ()
        )
        outcomes = []
        for item in targets:
            iid = str(item["spm"])
            name = Path(iid).name
            if iid not in failed_ids:
                authoritative = self._target_authoritative_result(iid, phase)
                if authoritative and authoritative["outcome"] in {
                    "pending_unreal",
                    "cancelled",
                    "owner_lost",
                }:
                    outcomes.append(authoritative)
                    continue
                outcomes.append({
                    "target": iid,
                    "target_name": name,
                    "outcome": "completed",
                    "reason_token": None,
                    "evidence": {},
                })
                continue
            authoritative = self._target_authoritative_result(iid, phase)
            if authoritative is not None:
                outcomes.append(authoritative)
                continue
            reason_token, evidence = self._target_failure_result(iid)
            failure_kind = self._target_failure_kind(iid)
            outcomes.append({
                "target": iid,
                "target_name": name,
                "outcome": (
                    "planned_excluded"
                    if failure_kind in PLANNED_EXCLUSION_KINDS
                    else "failed"
                ),
                "reason_token": reason_token,
                "evidence": evidence,
            })
        counts = self._count_target_outcomes(outcomes)
        return {
            "selected_count": len(outcomes),
            **counts,
            "planned_excluded_count": sum(
                row["outcome"] == "planned_excluded" for row in outcomes
            ),
            "dependency_blocked_count": 0,
            "target_outcomes": outcomes,
            "shared_failures": [],
        }

    def _build_pipeline_result_summary(
        self,
        selected_targets,
        root_failed_ids,
        dependency_blocked_ids,
        pipeline_abort,
    ):
        selected = []
        seen = set()
        for item in selected_targets:
            iid = str(item["spm"])
            if iid in seen:
                continue
            seen.add(iid)
            selected.append(iid)
        planned = copy.deepcopy(
            getattr(self, "_pipeline_planned_exclusions", {}) or {}
        )
        failed = set(root_failed_ids) & set(selected)
        dependency_blocked = set(dependency_blocked_ids) & set(selected)
        planned_ids = set(planned) & set(selected)
        if pipeline_abort:
            failed.update(
                set(selected)
                - dependency_blocked
                - planned_ids
            )

        outcomes = []
        dependency_map = {
            key: tuple(value)
            for key, value in (
                getattr(self, "_active_blender_dependency_map", {}) or {}
            ).items()
        }
        for key, value in (
            getattr(self, "_active_push_dependency_map", {}) or {}
        ).items():
            dependency_map[key] = tuple(dict.fromkeys(
                (*dependency_map.get(key, ()), *value)
            ))
        for iid in selected:
            authoritative = self._target_authoritative_result(
                iid,
                getattr(self, "_active_pipeline_terminal_phase", None),
            )
            if authoritative and authoritative["outcome"] in {
                "completed",
                "pending_unreal",
                "cancelled",
                "owner_lost",
            }:
                outcomes.append(authoritative)
                continue
            if iid in planned_ids:
                outcomes.append(copy.deepcopy(planned[iid]))
                continue
            if iid in dependency_blocked:
                blocked_by = [
                    value
                    for value in dependency_map.get(iid, ())
                    if value in root_failed_ids
                ]
                recorded_token, recorded_evidence = (
                    self._target_failure_result(
                        iid,
                        default_token=(
                            "shared_dependency_failed"
                            if blocked_by
                            else "dependency_root_reason_missing"
                        ),
                    )
                )
                artifact_rows = recorded_evidence.get(
                    "dependency_artifacts"
                ) or {}
                artifact_statuses = {
                    str((row or {}).get("status") or "")
                    for row in artifact_rows.values()
                    if isinstance(row, dict)
                }
                if "missing" in artifact_statuses:
                    reason_token = "dependency_output_missing"
                elif "stale" in artifact_statuses:
                    reason_token = "dependency_output_stale"
                else:
                    reason_token = "dependency_root_reason_missing"
                evidence = {
                    "blocked_by": blocked_by,
                    "declared_dependencies": list(
                        dependency_map.get(iid, ())
                    ),
                    "recorded_wrapper": recorded_token,
                }
                evidence.update(recorded_evidence)
                outcomes.append({
                    "target": iid,
                    "target_name": Path(iid).name,
                    "outcome": "blocked",
                    "reason_token": reason_token,
                    "evidence": evidence,
                })
                continue
            if iid in failed:
                if authoritative is not None:
                    outcomes.append(authoritative)
                    continue
                reason_token, evidence = self._target_failure_result(
                    iid,
                    default_token=(
                        "pipeline_aborted"
                        if pipeline_abort
                        else "item_failed"
                    ),
                )
                outcomes.append({
                    "target": iid,
                    "target_name": Path(iid).name,
                    "outcome": "failed",
                    "reason_token": reason_token,
                    "evidence": evidence,
                })
                continue
            outcomes.append({
                "target": iid,
                "target_name": Path(iid).name,
                "outcome": "completed",
                "reason_token": None,
                "evidence": {},
            })

        reuse_evidence = getattr(
            self, "_pipeline_dependency_reuse_evidence", {}
        ) or {}
        for row in outcomes:
            reused = reuse_evidence.get(row.get("target"))
            if not reused:
                continue
            row.setdefault("evidence", {})[
                "dependency_resolution"
            ] = "current_output_reused"
            row["evidence"]["dependency_artifacts"] = copy.deepcopy(
                reused
            )

        shared_failures = []
        for dependency in sorted(set(root_failed_ids) - set(selected)):
            affected = [
                iid
                for iid in selected
                if (
                    iid in dependency_blocked
                    and dependency in dependency_map.get(iid, ())
                )
            ]
            if not affected:
                continue
            reason_token, evidence = self._target_failure_result(
                dependency,
                default_token="dependency_root_reason_missing",
            )
            shared_failures.append({
                "dependency": dependency,
                "dependency_name": Path(dependency).name,
                "reason_token": reason_token,
                "affected_targets": affected,
                "evidence": evidence,
            })

        counts = self._count_target_outcomes(outcomes)
        planned_count = sum(
            row["outcome"] == "planned_excluded" for row in outcomes
        )
        dependency_blocked_count = sum(
            row["outcome"] == "blocked" for row in outcomes
        )
        return {
            "selected_count": len(outcomes),
            **counts,
            "planned_excluded_count": planned_count,
            "dependency_blocked_count": dependency_blocked_count,
            "target_outcomes": outcomes,
            "shared_failures": shared_failures,
        }

    def _run_full_pipeline(
        self,
        targets,
        terminal_phase="push",
        selected_scope=False,
        emit_done=True,
    ):
        self._active_pipeline_terminal_phase = terminal_phase
        self._active_repair_stage_contracts = {}
        self._pipeline_root_failed_items = set()
        self._pipeline_blocked_items = set()
        self._pipeline_planned_exclusions = {}
        self._pipeline_dependency_artifact_cache = {}
        self._pipeline_dependency_artifact_verdicts = {}
        self._pipeline_dependency_reuse_evidence = {}
        self._active_pipeline_selected_targets = list(targets)
        self.__dict__.pop("_phase_result_summary", None)
        try:
            return self._run_full_pipeline_stages(
                targets,
                terminal_phase=terminal_phase,
                selected_scope=selected_scope,
                emit_done=emit_done,
            )
        finally:
            for key in (
                "_active_blender_dependency_map",
                "_active_pipeline_terminal_phase",
                "_active_repair_stage_contracts",
                "_pipeline_upstream_failed_items",
                "_pipeline_root_failed_items",
                "_pipeline_blocked_items",
                "_pipeline_planned_exclusions",
                "_active_pipeline_selected_targets",
                "_pipeline_dependency_artifact_cache",
                "_pipeline_dependency_artifact_verdicts",
                "_pipeline_dependency_reuse_evidence",
            ):
                self.__dict__.pop(key, None)

    def _run_full_pipeline_stages(
        self,
        targets,
        terminal_phase="push",
        selected_scope=False,
        emit_done=True,
    ):
        (
            targets,
            self._active_blender_dependency_map,
            auto_added_cluster_ids,
        ) = expand_blender_repair_targets(
            targets,
            self._batch_job_inventory()
            or {str(item["spm"]): item for item in targets},
        )
        if auto_added_cluster_ids:
            self.log(
                "Tree Repair dependency: Cluster "
                f"{len(auto_added_cluster_ids)}개 자동 포함 — "
                + ", ".join(
                    sorted(
                        Path(iid).name for iid in auto_added_cluster_ids
                    )
                )
            )
        phase_labels = {
            "spm": "① SPM 본 세팅",
            "blender": "② Blender Repair",
            "push": "③ Unreal Push",
        }
        cluster_targets = [
            item for item in targets
            if is_cluster_source_spm(item["spm"])
        ]
        downstream_targets = [
            item for item in targets
            if not is_cluster_source_spm(item["spm"])
        ]
        schedule = []
        if cluster_targets:
            schedule.append(
                ("spm", cluster_targets, "Cluster ① SPM 본 세팅")
            )
            if terminal_phase in {"blender", "push"}:
                schedule.append(
                    (
                        "blender",
                        cluster_targets,
                        "Cluster ② Blender/Normalizer",
                    )
                )
        if downstream_targets:
            if terminal_phase in {"blender", "push"}:
                schedule.append(
                    (
                        "blender",
                        downstream_targets,
                        "Tree ② Blender Repair",
                    )
                )
        if terminal_phase == "push":
            schedule.append(("push", targets, phase_labels["push"]))
        pipeline_abort = None
        root_failed_ids = set()
        blocked_consumer_ids = set()
        planned_excluded_ids = set()
        for phase, scheduled_targets, label in schedule:
            if self.stop_flag.is_set():
                break
            stage_blocked = {}
            if phase == "blender":
                tree_targets = [
                    item for item in scheduled_targets
                    if not is_cluster_source_spm(item["spm"])
                ]
                stage_blocked = self._pipeline_dependency_blocks(
                    tree_targets,
                    self._active_blender_dependency_map,
                    root_failed_ids,
                )
                for item in tree_targets:
                    iid = str(item["spm"])
                    blocked_sources = stage_blocked.get(iid)
                    if blocked_sources:
                        self._record_pipeline_dependency_block(
                            item,
                            "blend_status",
                            blocked_sources,
                            persist=True,
                            publish_repair_contract=True,
                            dependency_verdicts=(
                                self._pipeline_dependency_artifact_verdicts.get(
                                    iid, {}
                                )
                            ),
                        )
                blocked_consumer_ids.update(stage_blocked)
                if stage_blocked:
                    self.log(
                        f"🌙 {label}: dependency 차단 "
                        f"{len(stage_blocked)}개 · root 원인 "
                        f"{len(root_failed_ids)}개"
                    )
            planned_excluded_ids = set(
                getattr(self, "_pipeline_planned_exclusions", {}) or {}
            )
            excluded_ids = (
                root_failed_ids
                | blocked_consumer_ids
                | planned_excluded_ids
            )
            eligible_stage = self._filter_pipeline_excluded_targets(
                scheduled_targets,
                excluded_ids,
            )
            if not eligible_stage:
                continue
            self._pipeline_root_failed_items = set(root_failed_ids)
            self._pipeline_blocked_items = set(blocked_consumer_ids)
            self._pipeline_upstream_failed_items = set(excluded_ids)
            self.log(f"🌙 {label} 시작")
            phase_ok = self._run_batch(
                phase, eligible_stage, emit_done=False
            )
            phase_failed = set(getattr(self, "_phase_failed_items", set()))
            for failed_iid in sorted(phase_failed):
                failure_kind = self._target_failure_kind(failed_iid)
                if failure_kind == "dependency_blocked":
                    blocked_consumer_ids.add(failed_iid)
                    continue
                if failure_kind not in PLANNED_EXCLUSION_KINDS:
                    continue
                reason_token, evidence = self._target_failure_result(
                    failed_iid,
                    default_token=failure_kind,
                )
                self._pipeline_planned_exclusions.setdefault(
                    failed_iid,
                    {
                        "target": failed_iid,
                        "target_name": Path(failed_iid).name,
                        "outcome": "planned_excluded",
                        "reason_token": reason_token,
                        "evidence": evidence,
                    },
                )
            planned_excluded_ids = set(
                getattr(self, "_pipeline_planned_exclusions", {}) or {}
            )
            new_root_failures = (
                phase_failed
                - blocked_consumer_ids
                - planned_excluded_ids
            )
            if new_root_failures:
                root_failed_ids.update(new_root_failures)
                self.log(
                    f"🌙 {label}: root 실패 "
                    f"{len(new_root_failures)}개는 다음 단계로 넘기지 않습니다."
                )
            self.log(f"🌙 {label} 종료")
            if not phase_ok:
                pipeline_abort = getattr(self, "_phase_abort_reason", None)
                if pipeline_abort or self.stop_flag.is_set():
                    break
                # Item-local failures are inputs to the next dependency gate,
                # not a fleet-wide abort.  The next stage either reuses a
                # current producer output or records its exact missing/stale
                # artifact for only the mapped consumers.
        planned_excluded_ids = set(
            getattr(self, "_pipeline_planned_exclusions", {}) or {}
        )
        excluded_ids = (
            root_failed_ids
            | blocked_consumer_ids
            | planned_excluded_ids
        )
        summary = self._build_pipeline_result_summary(
            getattr(self, "_active_pipeline_selected_targets", targets),
            root_failed_ids,
            blocked_consumer_ids,
            pipeline_abort,
        )
        self._phase_result_summary = copy.deepcopy(summary)
        all_completed = bool(summary["selected_count"]) and (
            summary["completed_count"] == summary["selected_count"]
        )
        actual_issue_count = (
            summary["failed_count"]
            + summary["blocked_count"]
            + summary.get("owner_lost_count", 0)
        )
        if all_completed:
            final_text = "전체 자동 완료"
        elif self.stop_flag.is_set() or summary.get("cancelled_count", 0):
            final_text = "중지됨"
        elif pipeline_abort:
            final_text = f"전체 자동 중단 — {pipeline_abort}"
        elif actual_issue_count:
            final_text = (
                "전체 자동 종료 — "
                f"completed {summary['completed_count']} · "
                f"blocked {summary['blocked_count']} · "
                f"owner_lost {summary.get('owner_lost_count', 0)} · "
                f"failed {summary['failed_count']}"
            )
        elif summary.get("pending_count", 0):
            final_text = (
                "전체 자동 Unreal 대기 — "
                f"pending {summary['pending_count']}"
            )
        else:
            final_text = "전체 자동 완료"
        if selected_scope:
            terminal_label = phase_labels[terminal_phase]
            if all_completed:
                final_text = f"{terminal_label} 연계 실행 완료"
            elif self.stop_flag.is_set() or summary.get("cancelled_count", 0):
                final_text = f"{terminal_label} 연계 실행 중지됨"
            elif pipeline_abort:
                final_text = f"{terminal_label} 연계 실행 중단 · {pipeline_abort}"
            elif actual_issue_count:
                final_text = (
                    f"{terminal_label} 연계 실행 종료 · "
                    f"completed {summary['completed_count']} · "
                    f"blocked {summary['blocked_count']} · "
                    f"owner_lost {summary.get('owner_lost_count', 0)} · "
                    f"failed {summary['failed_count']}"
                )
            elif summary.get("pending_count", 0):
                final_text = (
                    f"{terminal_label} 연계 실행 Unreal 대기 · "
                    f"pending {summary['pending_count']}"
                )
            else:
                final_text = f"{terminal_label} 연계 실행 완료"
        self._pipeline_root_failed_items = set(root_failed_ids)
        self._pipeline_blocked_items = set(blocked_consumer_ids)
        self._phase_failed_items = set(excluded_ids)
        self.ui_queue.put(("progress", final_text))
        if emit_done:
            self.ui_queue.put(("done", None))
        self.__dict__.pop("_active_blender_dependency_map", None)
        self.__dict__.pop("_pipeline_upstream_failed_items", None)
        self.log(f"🌙 {final_text}")
        return all_completed or not (
            self.stop_flag.is_set()
            or pipeline_abort
            or excluded_ids
        )

    def stop_batch(self):
        self._ensure_batch_queue_state()
        pending_jobs = list(self.pending_batch_jobs)
        pending = len(pending_jobs)
        shared_runtime = getattr(self, "shared_queue_runtime", None)
        if shared_runtime is not None:
            for job in pending_jobs:
                tracker = self._retry_tracker_for_job(job)
                partition = str(
                    (job.get("retry_metadata") or {}).get("partition") or ""
                )
                shared_job_id = job.get("shared_queue_job_id")
                if not shared_job_id:
                    continue
                try:
                    shared_runtime.cancel(
                        shared_job_id,
                        reason="sk_batch_local_queue_cancelled",
                    )
                    if tracker is not None and partition:
                        tracker.mark_partition_terminal(
                            partition,
                            RETRY_STAGE_CANCELLED,
                            "operator cancelled before shared queue claim",
                        )
                except Exception:
                    # A job that acquired its lease between the snapshot and
                    # this cancellation is owned by the worker and is released
                    # only after its child processes have stopped.
                    pass
        self.pending_batch_jobs.clear()
        with self._recovery_commit_lock:
            resume_commit = copy.deepcopy(self._recovery_resume_commit)
            self.stop_flag.set()
        planning_tracker = getattr(self, "_active_retry_progress", None)
        if planning_tracker is not None:
            planning = planning_tracker.snapshot(evaluate=False).get(
                "planning"
            ) or {}
            if planning.get("status") in {"active", "ready", "committing"}:
                planning_tracker.finish_planning(
                    RETRY_STAGE_CANCELLED,
                    "operator cancelled retry planning",
                )
        active_tracker = self._retry_tracker_for_job(
            getattr(self, "active_batch_job", None)
        )
        if active_tracker is not None:
            snapshot = active_tracker.snapshot(evaluate=False)
            current_id = snapshot.get("current_target_id")
            if current_id:
                active_tracker.observe_process(
                    current_id,
                    diagnostic=(
                        "operator cancellation requested; stopping exact "
                        "owned process tree"
                    ),
                )
        # Worker polling performs the tree kill. Keeping it in one place avoids
        # racing a direct parent-only kill that would orphan SpeedTree children.
        suffix = f" · 대기 작업 {pending}개 취소" if pending else ""
        if (
            isinstance(resume_commit, dict)
            and isinstance(self.active_batch_job, dict)
            and resume_commit.get("job_id") == self.active_batch_job.get("id")
        ):
            self.log(
                "중지 요청이 Modeler 복구 재개 commit 뒤에 도착했습니다. "
                "봉인된 callback은 한 번 완료될 수 있으며 이후 작업은 "
                "중지합니다. 사용자가 연 Modeler는 종료하지 않습니다."
                + suffix
            )
        else:
            self.log(
                "중지 요청됨 — 실행 중인 자동화 작업과 관리 대상 SpeedTree "
                "자식을 종료합니다. 수동 복구 Modeler는 종료하지 않습니다."
                + suffix
            )

    def shutdown_shared_queue(self):
        runtime = getattr(self, "shared_queue_runtime", None)
        if runtime is not None:
            # Persist the operator-close event before the GUI process can
            # disappear. A later lease recovery remains owner_lost, but its
            # receipt proves that it followed this close request.
            runtime.shutdown(operator_close=True)
        with self._recovery_commit_lock:
            self._app_open = False
            tracker = self._retry_tracker_for_job(
                getattr(self, "active_batch_job", None)
            )
            planning = any(
                worker.is_alive()
                for worker in getattr(self, "_retry_planning_workers", ())
            )
            if tracker is None and planning:
                tracker = getattr(self, "_active_retry_progress", None)
            if tracker is not None:
                tracker.record_operator_close(
                    "operator closed the SK Batch window; shutdown requested"
                )
            self.stop_batch()

    def _record_phase_status(
        self, iid, column, status_text, kind, reason, details=None, persist=True
    ):
        """Write the same structured item outcome to GUI and persistent state."""
        if kind in {"cancelled", "stopped"}:
            terminal_stage = RETRY_STAGE_CANCELLED
        elif kind == "owner_lost":
            terminal_stage = RETRY_STAGE_OWNER_LOST
        elif kind == "automatic_repair_pending":
            terminal_stage = RETRY_STAGE_BLOCKED
        elif kind in PLANNED_EXCLUSION_KINDS or kind in {
                "dependency_blocked",
                "manual_required",
                "not_run",
                "not_run_unreal",
                "recovery_blocked",
        }:
            terminal_stage = RETRY_STAGE_BLOCKED
        else:
            terminal_stage = RETRY_STAGE_FAILED
        self._retry_transition(
            iid,
            terminal_stage,
            reason,
            terminal_reason=str(kind),
            outcome=terminal_stage,
        )
        self.ui_queue.put(("cell", (iid, column, status_text)))
        with self.state_lock:
            state_entry = self.state.setdefault(iid, {})
            state_entry[column] = status_text
            state_entry[f"{column}_kind"] = kind
            durable_entry = {
                "time": datetime.now().isoformat(timespec="seconds"),
                "kind": kind,
                "message": reason,
            }
            if details:
                durable_entry.update(details)
            durable_entry.update(
                self._bind_failure_record(iid, kind, reason, details)
            )
            if kind in {"cancelled", "stopped"}:
                durable_entry["outcome"] = "cancelled"
                state_entry[f"{column}_result"] = durable_entry
                state_entry.pop(f"{column}_error", None)
            else:
                state_entry[f"{column}_error"] = durable_entry
                state_entry.pop(f"{column}_result", None)
            if persist:
                save_state(self.state)

    @staticmethod
    def _failure_status_text(reason, kind):
        if kind in {"cancelled", "stopped"}:
            return f"중지: {reason}"
        if kind == "automatic_repair_pending":
            return f"자동 복구 대기: {reason}"
        if kind == "automatic_repair_failed":
            return f"자동 복구 실패: {reason}"
        if kind == "manual_required":
            return reason
        if kind in PUSH_ABORT_KINDS:
            return f"중단: {reason}"
        return f"실패: {reason}"

    def _run_batch(self, phase, targets, emit_done=True):
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        self._phase_abort_reason = None
        self._phase_failed_items = set()
        if phase in {"blender", "push"}:
            self._pipeline_dependency_artifact_cache = {}
        requested_targets = list(targets)
        self._active_push_dependency_map = {}
        self._active_push_auto_added_ids = set()
        preflight_skipped = set()
        if phase == "spm":
            targets = sorted(targets, key=self._spm_schedule_key)
        if phase == "push":
            try:
                stage_dependency_contracts = {}
                repair_contracts = getattr(
                    self,
                    "_active_repair_stage_contracts",
                    None,
                )
                if isinstance(repair_contracts, dict):
                    with self.state_lock:
                        for root, repair_contract in repair_contracts.items():
                            if not isinstance(repair_contract, dict):
                                continue
                            if repair_contract.get("ready") is True:
                                self._validate_repair_stage_contract(
                                    None,
                                    repair_contract,
                                )
                            dependency_contract = repair_contract.get(
                                "push_dependency_contract"
                            )
                            if isinstance(dependency_contract, dict):
                                stage_dependency_contracts[root] = (
                                    copy.deepcopy(dependency_contract)
                                )
                (
                    targets,
                    self._active_push_dependency_map,
                    self._active_push_auto_added_ids,
                ) = expand_push_targets(
                    targets,
                    self._batch_job_inventory()
                    or {str(item["spm"]): item for item in targets},
                    stage_dependency_contracts=(
                        stage_dependency_contracts or None
                    ),
                )
                upstream_root_failed = set(
                    getattr(self, "_pipeline_root_failed_items", set())
                )
                upstream_blocked = set(
                    getattr(self, "_pipeline_blocked_items", set())
                )
                upstream_excluded = upstream_root_failed | upstream_blocked
                if upstream_excluded:
                    expanded_by_id = {
                        str(item["spm"]): item for item in targets
                    }
                    for iid in sorted(upstream_blocked):
                        item = expanded_by_id.get(iid)
                        if item is not None:
                            blocked_sources = tuple(
                                dependency
                                for dependency in (
                                    self._active_blender_dependency_map.get(
                                        iid, ()
                                    )
                                )
                                if dependency in upstream_root_failed
                            )
                            if blocked_sources:
                                self._record_pipeline_dependency_block(
                                    item,
                                    "push_status",
                                    blocked_sources,
                                    persist=True,
                                    dependency_verdicts=(
                                        getattr(
                                            self,
                                            "_pipeline_dependency_artifact_verdicts",
                                            {},
                                        ).get(iid, {})
                                    ),
                                )
                    removed_roots = {
                        iid for iid in upstream_root_failed
                        if iid in expanded_by_id
                    }
                    targets = self._filter_pipeline_excluded_targets(
                        targets,
                        upstream_excluded,
                    )
                    self._active_push_auto_added_ids.difference_update(
                        upstream_excluded
                    )
                    if removed_roots:
                        self.log(
                            "Tree Push dependency: upstream root 실패 "
                            f"{len(removed_roots)}개 재도입 차단"
                        )
                    if upstream_blocked:
                        self.log(
                            "Tree Push dependency: blocked consumer "
                            f"{len(upstream_blocked)}개 Push 제외"
                        )
            except (
                PushDependencyError,
                RepairPushEvidenceError,
                OSError,
                TypeError,
                ValueError,
            ) as exc:
                reason = compact_error_message(exc)
                requested_ids = {
                    str(item["spm"]) for item in requested_targets
                }
                self._phase_failed_items.update(requested_ids)
                self._phase_abort_reason = reason
                for item in requested_targets:
                    self._record_phase_status(
                        str(item["spm"]),
                        "push_status",
                        self._failure_status_text(reason, "data_error"),
                        "data_error",
                        reason,
                        persist=False,
                    )
                self.log(f"Unreal Push dependency scheduling failed — {reason}")
                if emit_done:
                    self.ui_queue.put(
                        ("progress", f"Unreal Push 중단 — {reason}")
                    )
                    self.ui_queue.put(("done", None))
                return False
            requested_targets = list(targets)
            if self._active_push_auto_added_ids:
                names = sorted(
                    Path(iid).name
                    for iid in self._active_push_auto_added_ids
                )
                self.log(
                    "Tree Push dependency: Cluster "
                    f"{len(names)}개 자동 포함 — {', '.join(names)}"
                )
            requested_ids = {str(item["spm"]) for item in requested_targets}
            targets, preflight_abort = self._push_preflight(targets)
            ready_ids = {str(item["spm"]) for item in targets}
            preflight_skipped = requested_ids - ready_ids
            self._phase_failed_items.update(preflight_skipped)
            if preflight_abort:
                self._phase_failed_items.update(requested_ids)
                self._phase_abort_reason = preflight_abort
                if emit_done:
                    self.ui_queue.put(("progress", f"Unreal Push 중단 — {preflight_abort}"))
                    self.ui_queue.put(("done", None))
                return False
            if not targets:
                excluded = len(preflight_skipped)
                reason = (
                    f"준비 검사 통과 항목 없음 · {excluded}개 제외"
                    if excluded else "준비 검사 통과 항목 없음"
                )
                self._phase_abort_reason = reason
                self.log(f"Unreal Push 중단 — {reason}. 표의 준비 검사 결과를 확인하세요.")
                if emit_done:
                    self.ui_queue.put(("progress", f"Unreal Push 중단 — {reason}"))
                    self.ui_queue.put(("done", None))
                return False
        titles = {"check": "검사", "spm": "SPM 본 세팅", "blender": "Blender Repair", "push": "Unreal Push"}
        column_by_phase = {"check": "spm_status", "spm": "spm_status",
                           "blender": "blend_status", "push": "push_status"}
        if (
            phase == "push"
            and getattr(self, "active_push_transport", "rpc") == "headless"
        ):
            return self._run_headless_push_batch(targets, emit_done=emit_done)
        title = titles[phase]
        column = column_by_phase[phase]
        total = len(targets)
        self.ui_queue.put(("batch_progress", (0, total)))
        self._batch_done = 0
        self._batch_active = 0
        phase_abort = threading.Event()
        attempted = set()
        failed_items = set(preflight_skipped)
        blender_dependency_map = (
            getattr(self, "_active_blender_dependency_map", None)
            if phase == "blender"
            else None
        )
        if phase == "blender" and blender_dependency_map is None:
            (
                _expanded,
                blender_dependency_map,
                _auto_added,
            ) = expand_blender_repair_targets(
                targets,
                self._batch_job_inventory()
                or {str(item["spm"]): item for item in targets},
            )
        blender_dependency_map = blender_dependency_map or {}
        upstream_failed_items = set(
            getattr(self, "_pipeline_upstream_failed_items", set())
        )

        def run_one(item):
            if self.stop_flag.is_set():
                return
            spm = item["spm"]
            iid = str(spm)
            with self.state_lock:
                attempted.add(iid)
                self._batch_active += 1
                active = self._batch_active
                done = self._batch_done
            self.ui_queue.put(
                ("progress", f"{title} {done}/{total} · 실행 중 {active}개")
            )
            retry_context = getattr(self, "_retry_thread_context", None)
            retry_stage = (
                RETRY_STAGE_UNREAL
                if phase == "push"
                else RETRY_STAGE_BLENDER
            )
            if retry_context is not None:
                retry_context.target_id = iid
                retry_context.stage = retry_stage
            self._retry_transition(
                iid,
                retry_stage,
                f"{title} started",
                progress=True,
                heartbeat=True,
            )
            try:
                if phase == "push":
                    dependencies = self._active_push_dependency_map.get(
                        iid, ()
                    )
                    blocked = []
                    waiting = []
                    verdicts = {}
                    for dependency in dependencies:
                        if dependency not in failed_items:
                            continue
                        verdict = self._dependency_artifact_verdict(
                            dependency,
                            phase="push",
                        )
                        verdicts[dependency] = verdict
                        if verdict["status"] == "current":
                            self.__dict__.setdefault(
                                "_pipeline_dependency_reuse_evidence", {}
                            ).setdefault(iid, {})[
                                dependency
                            ] = copy.deepcopy(verdict)
                            self.log(
                                "[의존 산출물 재사용] "
                                f"{spm.name}: {Path(dependency).name}의 "
                                "기존 Unreal import가 current이므로 이번 "
                                "실패 행으로 consumer를 차단하지 않습니다."
                            )
                        elif verdict["status"] == "waiting":
                            waiting.append(dependency)
                        else:
                            blocked.append(dependency)
                    if waiting and not blocked:
                        reason = " | ".join(
                            f"{Path(value).name}: "
                            f"{verdicts[value].get('reason')}"
                            for value in waiting
                        )
                        message = (
                            "필수 producer Unreal 산출물 대기: " + reason
                        )
                        self._set_push_state(
                            iid,
                            "dependency_waiting",
                            "대기: " + message,
                            details={
                                "reason_token": "dependency_waiting",
                                "blocked_by": list(waiting),
                                "dependency_artifacts": copy.deepcopy(
                                    verdicts
                                ),
                            },
                            message=message,
                        )
                        self.log(f"[의존 산출물 대기] {spm.name}: {reason}")
                        return
                    if blocked:
                        reason = " | ".join(
                            f"{Path(value).name}: "
                            f"{self._dependency_artifact_state_label(verdicts[value].get('status'))}"
                            f" · {verdicts[value].get('reason')}"
                            for value in blocked
                        )
                        raise BatchItemError(
                            "필수 producer Unreal 산출물이 current가 아닙니다: "
                            + reason,
                            kind="dependency_blocked",
                            report={
                                "reason_token": "shared_dependency_failed",
                                "evidence": {
                                    "blocked_by": list(blocked),
                                    "dependency_artifacts": copy.deepcopy(
                                        verdicts
                                    ),
                                },
                            },
                        )
                if phase == "blender" and not is_cluster_source_spm(spm):
                    blocked_sources = [
                        dependency
                        for dependency in blender_dependency_map.get(iid, ())
                        if (
                            dependency in failed_items
                            or dependency in upstream_failed_items
                        )
                    ]
                    if blocked_sources:
                        raise BatchItemError(
                            "Cluster source Repair failed, so downstream "
                            "Repair was not run: "
                            + ", ".join(
                                sorted(
                                    Path(value).name
                                    for value in blocked_sources
                                )
                            ),
                            kind="dependency_blocked",
                        )
                if phase == "check":
                    self._job_check(iid, spm)
                elif phase == "spm":
                    self._job_spm(iid, spm)
                elif phase == "blender":
                    self._job_blender(iid, spm, item)
                else:
                    self._job_push(iid, spm)
                self._retry_transition(
                    iid,
                    RETRY_STAGE_POST_CHECK,
                    f"{title} post-check",
                    progress=True,
                    heartbeat=True,
                )
            except Exception as exc:
                full_reason = str(exc)
                reason = compact_error_message(full_reason)
                kind = getattr(exc, "kind", "data_error")
                if self.stop_flag.is_set() or kind in {"cancelled", "stopped"}:
                    kind = "cancelled"
                    reason = reason or "사용자 중지"
                    full_reason = full_reason or reason
                if phase == "blender":
                    self._publish_repair_stage_contract(
                        spm,
                        ready=False,
                        reason=full_reason,
                        kind=kind,
                    )
                if phase == "push" and kind == "data_error" and "시간 초과" in reason:
                    kind = "push_timeout"
                    reason = "Push 작업 시간 초과 — Unreal/RPC 상태 확인 필요"
                status_text = self._failure_status_text(reason, kind)
                tag = {
                    "cancelled": "중지",
                    "automatic_repair_pending": "자동 복구 대기",
                    "automatic_repair_failed": "자동 복구 실패",
                    "manual_required": "수동",
                    "unreal_crash": "Unreal 중단",
                    "unreal_unavailable": "Unreal 중단",
                    "rpc_timeout": "RPC 중단",
                    "push_timeout": "Push 중단",
                }.get(kind, "실패")
                self.log(f"[{tag}] {spm.name}: {reason}")
                details = {}
                if getattr(exc, "log_file", None):
                    details["log"] = str(exc.log_file)
                if getattr(exc, "report_file", None):
                    details["report"] = str(exc.report_file)
                exception_report = copy.deepcopy(
                    getattr(exc, "report", {}) or {}
                )
                if exception_report:
                    details["failure_report"] = exception_report
                    for report_key in (
                        "reason_token",
                        "evidence",
                        "issues",
                        "repair_disposition",
                        "reason_ko",
                        "action_ko",
                        "stage",
                    ):
                        if report_key in exception_report:
                            details[report_key] = copy.deepcopy(
                                exception_report[report_key]
                            )
                self._record_phase_status(
                    iid,
                    column,
                    status_text,
                    kind,
                    full_reason,
                    details=details,
                    persist=phase != "check",
                )
                if kind != "cancelled":
                    with self.state_lock:
                        failed_items.add(iid)
                if phase == "push" and kind in PUSH_ABORT_KINDS:
                    self._phase_abort_reason = reason
                    phase_abort.set()
                    self.log(
                        "[Push 단계 중단] Unreal/RPC 상태가 안전하지 않아 남은 항목을 실행하지 않습니다."
                    )
            finally:
                if retry_context is not None:
                    retry_context.target_id = None
                    retry_context.stage = None
                with self.state_lock:
                    self._batch_active -= 1
                    self._batch_done += 1
                    done = self._batch_done
                    active = self._batch_active
                    failed = len(failed_items)
                self.ui_queue.put(("batch_progress", (done, total)))
                self.ui_queue.put((
                    "progress",
                    f"{title} {done}/{total} · 실행 중 {active}개 "
                    f"· 실패/차단 {failed}개",
                ))

        # Independent jobs overlap inside a wave. Blender keeps a barrier
        # between Cluster sources and root assemblies so saved input hashes
        # cannot change after a downstream Repair receipt is written.
        # RPC Push stays serial; headless Push parallelizes only its Blender
        # export stage below.
        if phase == "spm":
            workers = self.cfg.get("spm_parallel_jobs", 1)
        elif phase == "check":
            workers = self.cfg.get("check_parallel_jobs", 8)
        elif phase == "blender":
            workers = self.cfg.get("blender_parallel_jobs", 2)
        else:
            workers = 1
        workers = max(1, min(int(workers), total))
        waves = (
            blender_repair_schedule_waves(targets)
            if phase == "blender"
            else [targets]
        )
        if phase == "blender" and len(waves) > 1:
            self.log(
                "Blender Repair 의존성: Cluster 소스를 먼저 완료한 뒤 "
                "루트/Assembly Repair를 시작합니다."
            )
        for wave_index, wave in enumerate(waves):
            if self.stop_flag.is_set() or phase_abort.is_set():
                break
            cluster_owner_groups = []
            if (
                phase == "blender"
                and wave
                and all(
                    is_cluster_source_spm(item.get("spm"))
                    for item in wave
                )
            ):
                grouped = {}
                for item in wave:
                    owner_key = normalized_folder_key(
                        Path(item["spm"]).parent.parent
                    )
                    grouped.setdefault(owner_key, []).append(item)
                cluster_owner_groups = list(grouped.values())

            execution_units = cluster_owner_groups or [
                [item] for item in wave
            ]
            wave_workers = max(
                1,
                min(workers, len(execution_units)),
            )

            def run_unit(unit):
                for unit_item in unit:
                    if self.stop_flag.is_set() or phase_abort.is_set():
                        break
                    run_one(unit_item)

            if wave_workers > 1:
                self.log(
                    f"{title}: {wave_workers}개 동시 실행 "
                    f"(wave {wave_index + 1}/{len(waves)})"
                )
                with ThreadPoolExecutor(max_workers=wave_workers) as pool:
                    list(pool.map(run_unit, execution_units))
            else:
                for unit in execution_units:
                    if self.stop_flag.is_set() or phase_abort.is_set():
                        break
                    run_unit(unit)
        if phase_abort.is_set():
            deferred_reason = f"Unreal/RPC 중단으로 미실행 — {self._phase_abort_reason}"
            for item in targets:
                iid = str(item["spm"])
                if iid in attempted:
                    continue
                self._record_phase_status(
                    iid,
                    column,
                    deferred_reason,
                    "not_run_unreal",
                    deferred_reason,
                    persist=False,
                )
                self.log(f"[미실행] {item['spm'].name}: {deferred_reason}")
        if self.stop_flag.is_set():
            for item in targets:
                iid = str(item["spm"])
                if iid in attempted:
                    continue
                self._record_phase_status(
                    iid,
                    column,
                    "중지: 사용자 중지로 미실행",
                    "cancelled",
                    "사용자 중지로 미실행",
                    persist=False,
                )

        with self.state_lock:
            self._phase_failed_items = set(failed_items)
            if phase != "check":
                save_state(self.state)
        if emit_done:
            progress = (
                f"{title} 중단 — {self._phase_abort_reason}"
                if phase_abort.is_set()
                else "대기"
            )
            self.ui_queue.put(("progress", progress))
            self.ui_queue.put(("done", None))
        self.log(f"{title} 배치 종료.")
        return not phase_abort.is_set()

    # ------------------------------------------------------------------ jobs
    @staticmethod
    def _latest_log_line(log_file, max_bytes=8192):
        try:
            with Path(log_file).open("rb") as handle:
                size = handle.seek(0, 2)
                handle.seek(max(0, size - max_bytes))
                text = handle.read().decode("utf-8", errors="replace")
            return next(
                (line.strip() for line in reversed(text.splitlines()) if line.strip()),
                "",
            )
        except OSError:
            return ""

    @staticmethod
    def _read_log_lines_since(log_file, offset=0, remainder=b""):
        """Read each flushed child line exactly once without PIPE semantics."""
        try:
            with Path(log_file).open("rb") as handle:
                size = handle.seek(0, 2)
                if size < offset:
                    offset = 0
                    remainder = b""
                handle.seek(offset)
                chunk = handle.read()
                offset = handle.tell()
        except OSError:
            return offset, remainder, []
        payload = remainder + chunk
        if not payload:
            return offset, b"", []
        pieces = payload.splitlines(keepends=True)
        if pieces and not pieces[-1].endswith((b"\n", b"\r")):
            remainder = pieces.pop()
        else:
            remainder = b""
        lines = [
            piece.decode("utf-8", errors="replace").strip()
            for piece in pieces
            if piece.strip()
        ]
        return offset, remainder, lines

    @staticmethod
    def _retry_output_is_progress(line):
        value = str(line or "").lstrip()
        return value.startswith((
            "SK_BATCH_",
            "PROGRESS",
            "progress",
            "[progress]",
        ))

    def _run_limited(
        self,
        cmd,
        log_name,
        timeout,
        affinity=True,
        progress_callback=None,
        env=None,
        inactivity_timeout=None,
        inactivity_timeout_by_marker=None,
    ):
        log_file = LOG_DIR / log_name
        proc = launch_limited(
            cmd,
            self.cfg,
            log_file=str(log_file),
            affinity=affinity,
            env=env,
            cooperative_cancel=self.stop_flag.set,
        )
        with self.procs_lock:
            self.active_procs.add(proc)
        try:
            retry_tracker = self._retry_tracker_for_job()
            retry_context = getattr(self, "_retry_thread_context", None)
            retry_target_id = getattr(retry_context, "target_id", None)
            retry_stage = getattr(retry_context, "stage", None)
            started = time.monotonic()
            deadline = (
                None if timeout is None else started + timeout
            )
            current_inactivity_timeout = (
                None
                if inactivity_timeout is None
                else float(inactivity_timeout)
            )
            inactivity_deadline = (
                None
                if current_inactivity_timeout is None
                else started + current_inactivity_timeout
            )
            last_progress = started
            current_progress_marker = "process_start"
            progress_rules = tuple(
                (
                    str(marker),
                    None if limit is None else float(limit),
                )
                for marker, limit in (
                    inactivity_timeout_by_marker or {}
                ).items()
            )
            log_offset = 0
            log_remainder = b""
            latest_line = ""
            latest_progress_line = ""
            next_progress = 0.0
            next_retry_heartbeat = 0.0
            while proc.poll() is None:
                active_lease = getattr(
                    self, "_active_shared_queue_lease", None
                )
                if (
                    retry_tracker is not None
                    and active_lease is not None
                    and active_lease.heartbeat_error is not None
                ):
                    partition = str(
                        (
                            getattr(self, "_active_retry_metadata", {}) or {}
                        ).get("partition")
                        or ""
                    )
                    if partition:
                        retry_tracker.mark_partition_terminal(
                            partition,
                            RETRY_STAGE_OWNER_LOST,
                            "shared queue lease heartbeat lost while owned "
                            "process was active",
                        )
                    tree_stopped = terminate_process_tree(proc)
                    detail = (
                        ""
                        if tree_stopped
                        else " (exact owned tree termination unconfirmed)"
                    )
                    owner_error = RuntimeError(
                        "shared queue owner_lost" + detail
                    )
                    owner_error.kind = "owner_lost"
                    raise owner_error
                if self.stop_flag.is_set():
                    tree_stopped = terminate_process_tree(proc)
                    detail = "" if tree_stopped else " (자식 프로세스 종료 확인 실패)"
                    raise BatchItemError(
                        "사용자 중지" + detail,
                        kind="cancelled",
                    )
                now = time.monotonic()
                new_lines = []
                if (
                    progress_callback is not None
                    or progress_rules
                    or (retry_tracker is not None and retry_target_id)
                ):
                    (
                        log_offset,
                        log_remainder,
                        new_lines,
                    ) = self._read_log_lines_since(
                        log_file,
                        log_offset,
                        log_remainder,
                    )
                    for line in new_lines:
                        latest_line = line
                        for marker, marker_timeout in progress_rules:
                            if not line.startswith(marker):
                                continue
                            current_progress_marker = marker
                            latest_progress_line = line
                            current_inactivity_timeout = marker_timeout
                            last_progress = now
                            inactivity_deadline = (
                                None
                                if marker_timeout is None
                                else now + marker_timeout
                            )
                            break
                    if retry_tracker is not None and retry_target_id and new_lines:
                        marker_stage = retry_stage
                        marker_progress = False
                        for line in new_lines:
                            mapped = stage_for_send2ue_marker(
                                line, marker_stage
                            )
                            marker_progress = marker_progress or (
                                mapped != marker_stage
                                or self._retry_output_is_progress(line)
                            )
                            marker_stage = mapped
                        retry_stage = marker_stage
                        retry_tracker.observe_process(
                            retry_target_id,
                            stage=retry_stage,
                            diagnostic=new_lines[-1],
                            output=True,
                            progress=marker_progress,
                        )
                if deadline is not None and now > deadline:
                    tree_stopped = terminate_process_tree(proc)
                    detail = "" if tree_stopped else " — 자식 프로세스 종료 확인 실패"
                    timeout_error = RuntimeError(
                        f"시간 초과({timeout}s){detail} — 로그: {log_file}"
                    )
                    timeout_error.log_file = log_file
                    raise timeout_error
                if (
                    inactivity_deadline is not None
                    and now > inactivity_deadline
                ):
                    tree_stopped = terminate_process_tree(proc)
                    detail = "" if tree_stopped else " — 자식 프로세스 종료 확인 실패"
                    idle_seconds = int(max(0.0, now - last_progress))
                    timeout_error = RuntimeError(
                        "진행 없음 시간 초과("
                        f"{current_inactivity_timeout:g}s) — 단계: "
                        f"{current_progress_marker} · 마지막 진행 "
                        f"{idle_seconds}s 전{detail} — 로그: {log_file}"
                    )
                    timeout_error.kind = "internal_error"
                    timeout_error.timeout_kind = "child_progress_inactivity"
                    timeout_error.progress_marker = current_progress_marker
                    timeout_error.log_file = log_file
                    raise timeout_error
                if progress_callback is not None and now >= next_progress:
                    progress_callback(
                        now - started,
                        (
                            latest_progress_line
                            if progress_rules
                            else latest_line
                        ),
                    )
                    next_progress = now + 1.0
                if (
                    retry_tracker is not None
                    and retry_target_id
                    and now >= next_retry_heartbeat
                ):
                    retry_tracker.observe_process(
                        retry_target_id,
                        stage=retry_stage,
                    )
                    next_retry_heartbeat = now + 1.0
                interval = float(self.cfg.get("process_poll_interval", 0.2))
                time.sleep(max(0.05, min(interval, 1.0)))
        finally:
            # A progress callback, log reader, or UI shutdown can raise while
            # the child is still alive.  Leaving that Blender/SpeedTree tree
            # behind makes every later batch slower and can retain gigabytes
            # of working memory, so every exit path owns its child cleanup.
            if proc.poll() is None:
                terminate_process_tree(proc)
            # Always close the Job, even when the direct parent has already
            # exited.  That is the path where a crashed Blender/Python wrapper
            # can otherwise leave a multi-GB SpeedTree descendant behind.
            close_process_kill_job(proc)
            with self.procs_lock:
                self.active_procs.discard(proc)
            handle = getattr(proc, "sk_log_handle", None)
            if handle:
                try:
                    handle.close()
                except Exception:
                    pass
        return proc.returncode, log_file

    @staticmethod
    def _repair_contract_current(spm):
        """Prove the saved blend/report came from the current SPM content."""
        try:
            report = load_current_repair_pipeline_report(spm)
            if (report.get("handoff_preflight") or {}).get("status") not in {
                "ok", "source_review",
            }:
                return False
            return True
        except (OSError, ValueError, RuntimeError):
            return False

    @staticmethod
    def _cluster_assembly_inputs_current(
        spm,
        dependency_contract_out=None,
    ):
        """Validate every external Cluster artifact captured by BWR."""
        from cluster_assembly_builder import (
            MANIFEST_KIND,
            file_fingerprint,
            validate_file_fingerprint,
            validate_manifest_artifacts,
            validate_receipt_pass_through_manifest,
        )

        def receipt_fingerprints_match(expected, actual):
            if not isinstance(expected, dict) or not expected.get("path"):
                return False
            try:
                expected_path = os.path.normcase(
                    os.path.abspath(str(expected["path"]))
                )
                actual_path = os.path.normcase(
                    os.path.abspath(str(actual.get("path") or ""))
                )
                expected_size = int(expected.get("size"))
                actual_size = int(actual.get("size"))
            except (TypeError, ValueError):
                return False
            return bool(
                expected_path == actual_path
                and expected.get("exists") is True
                and actual.get("exists") is True
                and expected_size == actual_size
                and str(expected.get("sha256") or "").casefold()
                == str(actual.get("sha256") or "").casefold()
                and expected.get("sha256")
            )

        report_path = (
            Path(spm).parent / "reports" /
            f"{Path(spm).stem}_speedtree_repair_pipeline_report_codex.json"
        )
        try:
            pipeline = _read_repair_pipeline_json(report_path)
            embedded = pipeline.get("cluster_assembly_manifest")
            pipeline_resolution = (
                pipeline.get("cluster_assembly_receipt_resolution") or {}
            )
            pipeline_policy = str(
                pipeline_resolution.get("policy") or ""
            )
            pipeline_live_receipt = bool(
                pipeline_resolution.get("selected_receipt")
                and (
                    pipeline_policy.startswith("live_audit")
                    or pipeline_policy == "embedded_live_audit_authoritative"
                )
            )
            if pipeline_live_receipt:
                resolution = pipeline_resolution
            else:
                try:
                    resolution = cluster_assembly_receipt_resolution(spm)
                except (
                    FileNotFoundError,
                    ClusterAssemblyReceiptStaleError,
                ):
                    # The persisted receipt is only a cache snapshot.  The BWR
                    # manifest below contains and validates the real artifact
                    # fingerprints that determine whether Repair is current.
                    resolution = None
            if not isinstance(embedded, dict):
                if resolution is None:
                    # Vegetation with no Cluster relationship is a legitimate
                    # non-Assembly asset.
                    return True, ""
                if resolution.get("selected_receipt"):
                    raise RuntimeError(
                        "current Cluster relationship receipt exists, but the "
                        "Repair report has no Assembly manifest"
                    )
                return True, ""

            current_receipt_record = None
            current_receipt_path = None
            current_contract = {}
            current_handoff = {}
            if resolution and resolution.get("selected_receipt"):
                current_receipt_path = Path(resolution["selected_receipt"])
                if pipeline_live_receipt:
                    from cluster_assembly_handoff_contract import (
                        select_cluster_contract,
                    )

                    current_payload = json.loads(
                        current_receipt_path.read_text(encoding="utf-8")
                    )
                    current_contract = select_cluster_contract(
                        current_payload,
                        spm,
                    )
                else:
                    current_payload = load_cluster_assembly_receipt(
                        current_receipt_path,
                        requested_spm=spm,
                    )
                    current_contract = (
                        current_payload.get("cluster_assembly") or {}
                    )
                current_handoff = current_contract.get("handoff") or {}
                current_receipt_record = file_fingerprint(
                    current_receipt_path
                )

            if embedded.get("status") == "pass_through":
                if isinstance(
                    embedded.get("pass_through_provenance"), dict
                ):
                    if current_receipt_path is None:
                        raise RuntimeError(
                            "current Cluster pass-through receipt is "
                            "unavailable for provenance validation"
                        )
                    validate_receipt_pass_through_manifest(
                        embedded,
                        receipt_path=current_receipt_path,
                        target_contract=current_contract,
                        target_spm=spm,
                    )
                if current_receipt_record is not None and any((
                    current_handoff.get("roles"),
                    current_handoff.get("cluster_dependencies"),
                    current_handoff.get(
                        "separate_nanite_assembly_requested"
                    ),
                )):
                    rendered_unused = (
                        embedded.get("content_decision") == "pass_through"
                        and embedded.get("reason")
                        == (
                            "normalized_roles_are_prepared_but_unused_by_"
                            "rendered_mesh"
                        )
                        and int(embedded.get("rendered_role_count", -1)) == 0
                    )
                    embedded_receipt = (
                        (embedded.get("handoff_evidence") or {}).get(
                            "pcg_receipt"
                        )
                    )
                    receipt_bound_pass_through = isinstance(
                        embedded.get("pass_through_provenance"), dict
                    )
                    if (
                        not receipt_bound_pass_through
                        and (
                            not rendered_unused
                            or not receipt_fingerprints_match(
                                embedded_receipt,
                                current_receipt_record,
                            )
                        )
                    ):
                        raise RuntimeError(
                            "current Cluster relationship receipt is actionable, "
                            "but the Repair report recorded Assembly pass_through"
                        )
                if isinstance(dependency_contract_out, dict):
                    dependency_contract_out.update(
                        exact_dependency_contract_from_validated_manifest(
                            spm,
                            embedded,
                        )
                    )
                return True, ""
            manifest_record = embedded.get("manifest") or {}
            manifest_path = Path(str(manifest_record.get("path") or ""))
            if not manifest_path.is_file():
                raise RuntimeError(
                    "BWR Cluster Assembly manifest file is missing: "
                    + str(manifest_path)
                )
            validate_file_fingerprint(
                manifest_record, "BWR Cluster Assembly manifest"
            )
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["manifest"] = manifest_record
            if manifest.get("kind") != MANIFEST_KIND:
                raise RuntimeError(
                    "unsupported BWR Cluster Assembly manifest kind"
                )
            if current_receipt_record is not None:
                embedded_receipt = (
                    (pipeline.get("cluster_assembly_handoff") or {}).get(
                        "pcg_receipt"
                    )
                    or (manifest.get("handoff_evidence") or {}).get(
                        "pcg_receipt"
                    )
                )
                if not receipt_fingerprints_match(
                    embedded_receipt, current_receipt_record
                ):
                    raise RuntimeError(
                        "current Cluster relationship receipt differs from "
                        "the receipt captured by Blender Repair"
                    )
            validate_manifest_artifacts(manifest)
            if isinstance(dependency_contract_out, dict):
                dependency_contract_out.update(
                    exact_dependency_contract_from_validated_manifest(
                        spm,
                        manifest,
                    )
                )
            return True, ""
        except (OSError, ValueError, RuntimeError) as exc:
            return False, (
                "Cluster Assembly input changed after Repair → "
                "② Blender Repair 다시 실행: " + str(exc)
            )

    def _repair_runtime_addon_dir(self):
        """Installed BWR addon folder, derived identically for read and write."""
        planning = self._failed_retry_planning_context()
        cfg = (
            planning.cfg_snapshot
            if planning is not None
            else getattr(self, "cfg", {}) or {}
        )
        return addon_dir_from_config(cfg)

    @staticmethod
    def _repair_runtime_code_paths(addon_dir):
        """Every producer module that can change a completed Repair result."""
        return repair_runtime_code_paths(addon_dir)

    def _repair_runtime_code_state(self, addon_dir):
        """Content hash per producer module, independent of timestamps."""
        return repair_runtime_code_state(
            addon_dir,
            modules=self._repair_runtime_code_paths(addon_dir),
        )

    @staticmethod
    def _repair_runtime_receipt_path(spm):
        return repair_runtime_receipt_path(spm)

    def _write_repair_runtime_receipt(self, spm):
        """Record which BWR code produced this completed Repair result.

        A run that finds nothing to change legitimately saves no .blend, so the
        blend timestamp cannot stand in for code freshness: it pins the check
        forever and keeps demanding a rerun that can never satisfy it.  The
        receipt states what the run actually verified instead.
        """
        addon_dir = self._repair_runtime_addon_dir()
        if addon_dir is None:
            return
        try:
            state = self._repair_runtime_code_state(addon_dir)
        except OSError as exc:
            self.log(f"  [② 경고] Repair 런타임 지문 계산 실패: {spm.name}: {exc}")
            return
        if not state:
            return
        try:
            write_repair_runtime_receipt(
                spm,
                getattr(self, "cfg", {}) or {},
                addon_dir=addon_dir,
                code_state=state,
            )
        except OSError as exc:
            self.log(f"  [② 경고] Repair 런타임 기록 실패: {spm.name}: {exc}")

    def _repair_runtime_fresh(self, spm):
        """Gate only on an explicit saved-output contract revision.

        Producer source hashes are retained in the receipt for diagnostics,
        but ordinary code edits must not invalidate every completed Repair.
        The live SPM/report/artifact contracts below remain authoritative for
        content freshness.
        """
        blend = blend_path_for(spm)
        report_path = (
            Path(spm).parent / "reports" /
            f"{Path(spm).stem}_speedtree_repair_pipeline_report_codex.json"
        )
        if not blend.is_file() or not report_path.is_file():
            # Missing/stale source outputs are explained by the ordinary
            # handoff checks; runtime freshness applies only to a completed
            # Repair result that would otherwise be skipped as current.
            return True, ""

        receipt_path = self._repair_runtime_receipt_path(spm)
        candidate = None
        if receipt_path.is_file():
            try:
                candidate = json.loads(receipt_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                candidate = None
        saved_contract = repair_runtime_output_contract(candidate)
        if (
            saved_contract is not None
            and saved_contract != REPAIR_OUTPUT_CONTRACT_VERSION
        ):
            return False, (
                "Blender Repair 산출물 계약이 변경됨 → "
                "② Blender Repair 다시 실행 "
                f"({saved_contract} → "
                f"{REPAIR_OUTPUT_CONTRACT_VERSION})"
            )

        # This file is diagnostic metadata, not proof that the artifacts are
        # current.  Upgrade missing/legacy/corrupt receipts only after the
        # content-addressed SPM/report contract independently proves that the
        # saved result is current.  A failed rewrite never makes a good result
        # stale, and an old receipt can never bless stale artifacts.
        if (
            repair_runtime_receipt_needs_migration(candidate)
            and self._repair_contract_current(spm)
        ):
            planning = self._failed_retry_planning_context()
            if planning is not None:
                planning.counters["runtime_receipt_migrations_deferred"] += 1
                return True, ""
            try:
                migrate_repair_runtime_receipt(
                    spm,
                    candidate,
                    addon_dir=self._repair_runtime_addon_dir(),
                )
            except OSError as exc:
                self.log(
                    f"  [② 경고] Repair 런타임 기록 승격 실패: "
                    f"{Path(spm).name}: {exc}"
                )
        return True, ""

    def _repair_output_state(self, spm, pipeline_projection_out=None):
        with repair_report_read_scope():
            state = self._repair_output_state_scoped(spm)
            if isinstance(pipeline_projection_out, dict):
                try:
                    pipeline = _read_repair_pipeline_json(
                        repair_pipeline_report_path(spm)
                    )
                except (OSError, TypeError, ValueError):
                    pipeline = {}
                manifest = pipeline.get("cluster_assembly_manifest")
                if isinstance(manifest, dict):
                    pipeline_projection_out["cluster_assembly_manifest"] = (
                        copy.deepcopy(manifest)
                    )
            return state

    def _repair_output_state_scoped(self, spm):
        """One semantic decision shared by row status and the ② queue gate."""
        speedtree_spm = speedtree_output_spm_for(spm)
        leaf_projection = {}
        leaf_ok, leaf_reason = self._leaf_reference_ready(
            speedtree_spm,
            contract_out=leaf_projection,
        )
        leaf_contract = leaf_projection.get("contract")
        if not leaf_ok:
            return {
                "current": False,
                "push_ready": False,
                "kind": "source_data",
                "reason": leaf_reason,
            }
        blend = blend_path_for(spm)
        if not blend.exists():
            return {
                "current": False,
                "push_ready": False,
                "kind": "missing_blend",
                "reason": "생성 필요 — blend 없음 → ② Blender Repair",
            }
        runtime_fresh, runtime_reason = self._repair_runtime_fresh(spm)
        if not runtime_fresh:
            return {
                "current": False,
                "push_ready": False,
                "kind": "output_contract",
                "reason": runtime_reason,
            }
        receipt_current = self._repair_contract_current(spm)
        assembly_state = None
        assembly_dependency_contract = {}

        def assembly_inputs():
            nonlocal assembly_state
            if assembly_state is None:
                assembly_state = self._cluster_assembly_inputs_current(
                    spm,
                    dependency_contract_out=assembly_dependency_contract,
                )
            return assembly_state

        if receipt_current:
            assembly_current, assembly_reason = assembly_inputs()
            if not assembly_current:
                return {
                    "current": False,
                    "push_ready": False,
                    "kind": "assembly_stale",
                    "reason": assembly_reason,
                }
        if blend.stat().st_mtime < spm.stat().st_mtime and not receipt_current:
            return {
                "current": False,
                "push_ready": False,
                "kind": "stale_content",
                "reason": (
                    "Blender 갱신 필요 — SPM이 더 최근에 수정됨 "
                    "→ ② Blender Repair"
                ),
            }

        handoff_status = ""
        if receipt_current:
            try:
                report = load_current_repair_pipeline_report(spm)
                handoff_status = str(
                    (report.get("handoff_preflight") or {}).get("status")
                    or ""
                )
            except (OSError, ValueError, RuntimeError):
                receipt_current = False
            if handoff_status == "source_review":
                state = {
                    "current": True,
                    "push_ready": False,
                    "kind": "source_review",
                    "reason": "원본/재질 검토 필요 — Unreal Push 차단",
                    "texture_reason": "",
                }
                if assembly_dependency_contract:
                    state["push_dependency_contract"] = copy.deepcopy(
                        assembly_dependency_contract
                    )
                return state

        material_ok, material_reason = self._material_export_ready(
            spm,
            content_receipt_current=receipt_current,
            leaf_contract=leaf_contract,
        )
        if not material_ok:
            return {
                "current": False,
                "push_ready": False,
                "kind": "material",
                "reason": material_reason,
            }
        wind_json = blend.parent / "JSON" / f"{spm.stem}_dynamic_wind_import_from_megaplant_groups.json"
        if not wind_json.exists():
            return {
                "current": False,
                "push_ready": False,
                "kind": "missing_wind",
                "reason": "wind JSON 없음 → ② 필요",
            }
        wind_ready, wind_reason = dynamic_wind_skeleton_contract_ready(
            wind_json
        )
        if not wind_ready:
            return {
                "current": False,
                "push_ready": False,
                "kind": "wind_contract",
                "reason": (
                    f"{wind_reason} — "
                    "② Blender Repair에서 자동 재생성"
                ),
            }
        texture_ok, texture_reason = self._texture_normalization_ready(
            spm, content_receipt_current=receipt_current
        )
        if not texture_ok:
            return {
                "current": False,
                "push_ready": False,
                "kind": "texture",
                "reason": texture_reason,
            }
        assembly_current, assembly_reason = assembly_inputs()
        if not assembly_current:
            return {
                "current": False,
                "push_ready": False,
                "kind": "assembly_stale",
                "reason": assembly_reason,
            }
        state = {
            "current": True,
            "push_ready": True,
            "kind": "ready",
            "reason": "준비됨 ✓",
            "texture_reason": texture_reason,
        }
        if assembly_dependency_contract:
            state["push_dependency_contract"] = copy.deepcopy(
                assembly_dependency_contract
            )
        return state

    def _handoff_ready(self, spm, state_out=None):
        """Return Unreal handoff readiness from the shared Repair decision."""
        state = self._repair_output_state(spm)
        if isinstance(state_out, dict):
            state_out.update(copy.deepcopy(state))
        return bool(state["current"] and state["push_ready"]), state["reason"]

    def _build_repair_stage_evidence(
        self,
        spm,
        push_dependency_contract=None,
    ):
        """Capture immutable Repair inputs for this queue generation."""
        report_path = repair_pipeline_report_path(spm)
        pipeline = _read_repair_pipeline_json(report_path)
        return build_repair_push_evidence_bundle(
            queue_spm=spm,
            speedtree_spm=speedtree_output_spm_for(spm),
            blend=blend_path_for(spm),
            repair_report=report_path,
            pipeline=pipeline,
            push_dependency_contract=push_dependency_contract,
        )

    def _repair_stage_evidence_if_active(
        self,
        spm,
        push_dependency_contract=None,
    ):
        """Capture evidence only for a Repair -> Push pipeline generation."""
        contracts = getattr(self, "_active_repair_stage_contracts", None)
        if not isinstance(contracts, dict):
            return None
        return self._build_repair_stage_evidence(
            spm,
            push_dependency_contract,
        )

    def _publish_repair_stage_contract(
        self,
        spm,
        *,
        ready,
        reason,
        kind=None,
        push_dependency_contract=None,
        evidence_bundle=None,
    ):
        """Publish ②'s final result for ③ in the same pipeline only."""
        contracts = getattr(self, "_active_repair_stage_contracts", None)
        if not isinstance(contracts, dict):
            return
        value = {
            "ready": bool(ready),
            "reason": str(reason or ("준비됨 ✓" if ready else "준비 안 됨")),
            "kind": str(kind or ("ready" if ready else "not_ready")),
        }
        if isinstance(push_dependency_contract, dict):
            value["push_dependency_contract"] = copy.deepcopy(
                push_dependency_contract
            )
        if isinstance(evidence_bundle, dict):
            value["evidence_bundle"] = copy.deepcopy(evidence_bundle)
        key = _normalized_path(speedtree_output_spm_for(spm))
        with self.state_lock:
            contracts[key] = value

    def _repair_stage_contract(self, spm):
        """Read a job-scoped ② result without accepting persisted state."""
        contracts = getattr(self, "_active_repair_stage_contracts", None)
        if not isinstance(contracts, dict):
            return None
        key = _normalized_path(speedtree_output_spm_for(spm))
        with self.state_lock:
            value = contracts.get(key)
            return copy.deepcopy(value) if isinstance(value, dict) else None

    @staticmethod
    def _validate_repair_stage_contract(spm, repair_contract):
        """Fail closed when same-generation evidence no longer matches."""
        if not isinstance(repair_contract, dict):
            raise RepairPushEvidenceError(
                "same-generation Repair stage contract is malformed"
            )
        if repair_contract.get("ready") is not True:
            return repair_contract
        try:
            validate_repair_push_evidence_bundle(
                repair_contract.get("evidence_bundle"),
                expected_queue_spm=(
                    spm if spm is not None else None
                ),
            )
        except RepairPushEvidenceError as exc:
            raise RepairPushEvidenceError(
                stale_execution_freeze_message(exc)
            ) from exc
        return repair_contract

    @staticmethod
    def _leaf_reference_ready(spm, contract_out=None):
        contract = inspect_spm_leaf_contract(spm)
        if isinstance(contract_out, dict):
            contract_out["contract"] = contract
        return leaf_contract_user_message(contract)

    @staticmethod
    def _material_export_ready(
        spm,
        content_receipt_current=False,
        leaf_contract=None,
    ):
        speedtree_spm = speedtree_output_spm_for(spm)
        contract = (
            leaf_contract
            if isinstance(leaf_contract, dict)
            else inspect_spm_leaf_contract(speedtree_spm)
        )
        exported = inspect_speedtree_material_export(speedtree_spm, contract)
        all_exported = inspect_all_speedtree_material_export(speedtree_spm)
        if all_exported.get("status") not in {"ok", "not_applicable"}:
            exported = all_exported
        status = exported.get("status")
        if status in {"ok", "not_applicable"} or (
            status == "stale" and content_receipt_current
        ):
            return True, "SpeedTree 재질 export 정상"
        missing = list(exported.get("missing_materials") or [])
        if status == "missing_stmat":
            return False, "SpeedTree .stmat 없음 → ② Blender Repair"
        if status == "invalid_stmat":
            return False, (
                "SpeedTree .stmat 파싱 실패 — "
                + str(exported.get("error") or "파일 확인")
            )
        if status == "stale":
            return False, "SPM 변경 후 SpeedTree .stmat이 오래됨 → ② 다시 실행"
        if missing:
            return False, (
                "현재 내보내는 노드의 재질이 SpeedTree FBX에서 빠짐 — "
                + ", ".join(missing)
                + " → 표시된 SpeedTree 노드의 재질/텍스처 확인"
            )
        return False, "SpeedTree 재질 export 사전검사 실패 → ② 확인"

    @staticmethod
    def _texture_normalization_ready(spm, content_receipt_current=None):
        report_path = repair_pipeline_report_path(spm)
        if not report_path.is_file():
            return False, "텍스처 정규화 정보 없음 → ② 필요"
        if content_receipt_current is None:
            content_receipt_current = App._repair_contract_current(spm)
        try:
            if (
                report_path.stat().st_mtime_ns < Path(spm).stat().st_mtime_ns
                and not content_receipt_current
            ):
                return False, "SPM 변경 후 Repair 보고서가 오래됨 → ② 다시 실행"
        except OSError as exc:
            return False, f"텍스처 보고서 시간 확인 실패: {exc}"
        try:
            report = load_current_repair_pipeline_report(spm)
        except (OSError, ValueError, RuntimeError) as exc:
            if "legacy Repair report" in str(exc):
                return False, (
                    "공통 SpeedTree 계약 정보 없음 → "
                    "② Blender Repair 다시 실행"
                )
            return False, f"텍스처 정규화 보고서 오류: {exc}"
        normalization = report.get("texture_normalization") or {}
        normalization_status = str(normalization.get("status") or "")
        fallback_rows = [
            material
            for material in normalization.get("materials", [])
            if str(material.get("status") or "") == "needs_pcg_generation"
        ]
        source_fallback_ready = (
            normalization_status == "needs_pcg_generation"
            and str(normalization.get("texture_contract_status") or "")
            == TEXTURE_ORIGIN_NEEDS_PCG_GENERATION
            and bool(fallback_rows)
            and all(
                str(material.get("texture_contract_status") or "")
                == TEXTURE_ORIGIN_NEEDS_PCG_GENERATION
                for material in fallback_rows
            )
        )
        handoff = report.get("handoff_preflight")
        if not isinstance(handoff, dict):
            return False, "② 사전검사 정보 없음 → ② Blender Repair 다시 실행"
        slots = handoff.get("empty_material_slots") or []
        outputs = handoff.get("missing_outputs") or []
        materials = (
            handoff.get("missing_materials")
            or (handoff.get("material_export") or {}).get("missing_materials")
            or []
        )
        collections = handoff.get("export_collection_issues") or []
        vertex_contract = handoff.get("vertex_color_contract") or {}
        payload_contract = handoff.get("vertex_payload_contract") or {}
        leaf_contract = handoff.get("leaf_reference_contract") or {}
        reasons = []
        if slots:
            reasons.append(
                "머티리얼 빈 슬롯 "
                + ", ".join(
                    f"{item.get('object', '?')}[{item.get('slot', '?')}]"
                    for item in slots
                )
            )
        if outputs:
            reasons.append("핸드오프 파일 누락 " + ", ".join(map(str, outputs)))
        if materials:
            reasons.append("SpeedTree export 재질 누락 " + ", ".join(materials))
        if collections:
            reasons.append("Export collection 오류 " + ", ".join(map(str, collections)))
        if vertex_contract.get("status") == "blocked":
            reasons.append("버텍스 컬러 검사 실패")
        if payload_contract.get("status") == "blocked":
            reasons.append("AO/Nanite UV payload 검사 실패")
        if leaf_contract.get("status") in {"blocked", "replacement_needed"}:
            reasons.append("SPM leaf 참조 실패")
        if reasons:
            return False, "② 사전검사 차단: " + " | ".join(reasons)
        if handoff.get("status") not in {"ok", "source_review", "blocked"}:
            return False, "② 사전검사 미완료 → Blender Repair 다시 실행"
        preserved_count = sum(
            1 for item in normalization.get("materials", [])
            if item.get("status") == "preserved_cluster"
        )
        if preserved_count:
            return True, f"텍스처 준비 완료 · 보존 Cluster {preserved_count}세트"
        if source_fallback_ready:
            return True, (
                "텍스처 준비 완료 · 원본 텍스처 사용 중 · "
                "canonical T_* 미생성 "
                f"{len(fallback_rows)}세트"
            )
        return True, "머티리얼 준비 완료 · 텍스처 선택 연결"

    @staticmethod
    def _blend_status_from_repair_state(state):
        """Format one already-computed Repair state for the overview table."""
        if state["kind"] == "missing_blend":
            return "생성 필요 — blend 없음 · ② Blender Repair 실행"
        if state["kind"] == "stale_content":
            return "Blender 갱신 필요 — SPM이 더 최근에 수정됨 · ② 다시 실행"
        if not state["current"]:
            return f"Repair 필요 — {state['reason']}"
        if state["kind"] == "source_review":
            return "Blend 완료 · 원본 검토 필요 · Unreal Push 차단"
        texture_reason = state.get("texture_reason", "")
        if "보존 Cluster" in texture_reason:
            return "최신 ✓ · 보존 Cluster ✓"
        if "canonical T_* 미생성" in texture_reason:
            return (
                "최신 ✓ · 원본 텍스처 사용 중 · "
                "T_* 미생성 (비차단)"
            )
        return "최신 ✓"

    def _blend_status_text(self, spm):
        """Return the live SK handoff status; never trust a saved UI label."""
        try:
            state = self._repair_output_state(spm)
        except OSError as exc:
            return f"확인 실패 — {exc}"
        return self._blend_status_from_repair_state(state)

    def _record_live_blend_status(
        self,
        iid,
        spm,
        persist=True,
        repair_state=None,
    ):
        text = (
            self._blend_status_from_repair_state(repair_state)
            if isinstance(repair_state, dict)
            else self._blend_status_text(spm)
        )
        self.ui_queue.put(("cell", (iid, "blend_status", text)))
        if persist:
            with self.state_lock:
                self.state.setdefault(iid, {})["blend_status"] = text
                save_state(self.state)
        return text

    def _job_check(self, iid, spm):
        from spm_audit import audit_spm, inspect_interrupted_calibration, sk_readiness

        interrupted = inspect_interrupted_calibration(spm)
        if interrupted["status"] not in {"clean", "interrupted_but_intact"}:
            # ① restores automatically on its next run; say so instead of
            # reporting bone numbers read out of a half-rewritten source.
            recoverable = (
                "① 실행 시 자동 복원"
                if interrupted.get("backup_available")
                else "백업 없음 · 수동 확인 필요"
            )
            text = f"⚠ 중단된 캘리브레이션 흔적 · {recoverable}"
            self.ui_queue.put(("cell", (iid, "spm_status", text)))
            self.log(
                f"  [① 중단 흔적] {Path(spm).name}: {interrupted['marker']} "
                f"({recoverable})"
            )
            self._record_live_blend_status(iid, spm, persist=False)
            return

        entry = self.state.get(iid, {})
        item = self._batch_job_item(iid)
        pair_status = item.get("cluster_pair_status")
        if pair_status == "bootstrap_ready":
            text = "Cluster canonical 생성 예정 · 첫 작업에서 안전 bootstrap"
            self.ui_queue.put(("cell", (iid, "spm_status", text)))
            self._record_live_blend_status(iid, spm, persist=False)
            return
        if pair_status and pair_status not in {
            "current", "publish_ready", "mirror_missing"
        }:
            text = f"오류: Cluster pair {pair_status}"
            self.ui_queue.put(("cell", (iid, "spm_status", text)))
            return
        manual_bones_locked = item.get("manual_bones_locked", False)
        summary = entry.get("spm_summary", "")
        if manual_bones_locked:
            text = manual_bone_status_text(spm)
        else:
            audit = audit_spm(spm, analyze_bone_graph=True)
            readiness = sk_readiness(audit)
            parts = spm_check_status_parts(audit)
            if not readiness["ready"]:
                disabled = ", ".join(
                    f"{item['generator']}({item['style']:g}/{item['bones']:g})"
                    for item in readiness["disabled_generators"]
                )
                text = f"오류: SK 미제작 · {disabled}"
            else:
                text = " · ".join(parts) if parts else "본 설정 없음"
        if not manual_bones_locked and summary and summary not in text:
            text += f" | {summary}"
        self.ui_queue.put(("cell", (iid, "spm_status", text)))

        self._record_live_blend_status(iid, spm, persist=False)

        ok, why = self._handoff_ready(spm)
        pushed = self._current_push_status_text(iid, spm)
        push_text = why if not pushed or not ok else f"{why} | {pushed}"
        self.ui_queue.put(("cell", (iid, "push_status", push_text)))

    @staticmethod
    def _spm_report_summary(rep):
        if rep.get("cached_display_summary"):
            return str(rep["cached_display_summary"])
        total = rep.get("total_bones")
        meta = rep.get("calibration") or {}
        branches = meta.get("total_branches")
        if rep.get("status") == "not-sk-ready":
            return "SK 미제작"
        if rep.get("status") == "manual-required":
            return "수동 처리 필요"
        if total is not None and branches:
            if meta.get("base_priority_applied"):
                disabled = meta.get("disabled_base_generator_count", 0)
                tag = f" [상한·Base {disabled}개 OFF]"
            else:
                tag = " [상한]" if meta.get("capped") else ""
            graph_tag = ""
            if meta.get("root_target_generator_count") is not None:
                graph_tag = (
                    f" · 대상 {meta['root_target_generator_count']}"
                    f"/Base제외 {meta.get('base_excluded_generator_count', 0)}"
                )
            return f"본 {total} / 가지 {branches}{tag}{graph_tag}"
        if meta.get("mode") == "no_branch_generators":
            return "SpeedTree 본 없음 → rigid 1본 폴백"
        return rep.get("status", "?")

    def _prepare_pair_for_job(self, spm):
        try:
            result = prepare_cluster_spm_pair_for_job(spm)
        except Exception as exc:
            raise BatchItemError(str(exc), kind="data_error") from exc
        if result.get("status") == "applied":
            self.log(
                f"Cluster output 이름 정규화: "
                f"{Path(result['mirror_spm']).name} → "
                f"{Path(result['canonical_spm']).name}"
            )
        return Path(result.get("canonical_spm") or spm)

    def _run_inline_atlas_manifest_repair(self, spm, failure_report):
        """Run one exact mirror repair under the already-owned batch lease."""

        from pcg_st9_texture_batch.exact_target_repair import (
            execute_step3_standard,
        )

        spm = Path(spm).expanduser().absolute()
        lease = getattr(self, "_active_shared_queue_lease", None)
        if lease is None or getattr(lease, "finished", False):
            raise BatchItemError(
                "Atlas manifest 자동 복구에 필요한 현재 공용 대기열 소유권이 없습니다.",
                kind="automatic_repair_pending",
                report=copy.deepcopy(failure_report),
            )
        key = os.path.normcase(os.path.abspath(str(spm))).casefold()
        # Known before any stage runs, and the only thing on this path that
        # says what the repair was for.  The failure branch below has to carry
        # it onto the durable row; the cached path never reaches the block
        # that used to compute it.
        root_reason_codes = sorted(set(
            evidence_reason_codes(failure_report)
            or ["atlas_manifest_mirror_conflict_repairable"]
        ))
        with _INLINE_ATLAS_REPAIR_LOCK:
            memo = self.__dict__.setdefault("_inline_atlas_repair_results", {})
            cached = memo.get(key)
            if cached is not None:
                cached_status = str(
                    cached.get("terminal_status")
                    or cached.get("status")
                    or ""
                )
                if cached_status != "completed":
                    terminal = copy.deepcopy(cached)
                else:
                    current_plan = atlas_manifest_mirror_repair_plan(spm)
                    if current_plan.get("status") == "not_needed":
                        terminal = copy.deepcopy(cached)
                    else:
                        # A later producer can make the same mirror stale again
                        # during one long batch. Current evidence, not the earlier
                        # successful receipt, decides whether another exact repair
                        # is required.
                        cached = None
            if cached is None:
                active_job = getattr(self, "active_batch_job", None) or {}
                retry_metadata = copy.deepcopy(
                    getattr(self, "_active_retry_metadata", {}) or {}
                )
                queue_identity = str(
                    active_job.get("shared_queue_job_id")
                    or active_job.get("id")
                    or "direct-batch"
                )
                request_hash = hashlib.sha256(
                    (queue_identity + "\0" + key).encode("utf-8")
                ).hexdigest()[:16]
                request_id = f"inline-atlas-{request_hash}"
                receipt = LOG_DIR / f"exact_repair_{request_id}.json"
                parent_retry_id = str(
                    retry_metadata.get("progress_run_id")
                    or active_job.get("shared_queue_job_id")
                    or f"sk-batch-{active_job.get('id') or 'direct'}"
                )
                reason_codes = list(root_reason_codes)
                request = build_exact_target_request(
                    tool=PCG_TEXTURE_TOOL,
                    repair_action=ATLAS_MANIFEST_MIRROR_REPAIR,
                    target_spms=[spm],
                    repair_stage="atlas_manifest_repair",
                    provenance={
                        "reason_codes": reason_codes,
                        "source": "sk_batch.inline_atlas_preflight",
                    },
                    parent_retry_id=parent_retry_id,
                    request_id=request_id,
                    receipt=receipt,
                )

                def on_progress(payload):
                    stage = str(
                        payload.get("current_stage")
                        or "Atlas manifest 자동 복구"
                    )
                    self.ui_queue.put((
                        "progress",
                        f"{spm.name} · {stage}",
                    ))
                    self._retry_transition(
                        str(spm),
                        RETRY_STAGE_BLENDER,
                        stage,
                        progress=bool(payload.get("completed")),
                        output=True,
                        heartbeat=True,
                    )

                terminal = run_exact_target_request(
                    request,
                    execute_step3_standard,
                    inherited_lease=lease,
                    cancel_event=self.stop_flag,
                    on_progress=on_progress,
                )
                memo[key] = copy.deepcopy(terminal)
            terminal_status = str(
                terminal.get("terminal_status")
                or terminal.get("status")
                or ""
            )
            if terminal_status == "cancelled":
                raise BatchItemError(
                    "Atlas manifest 자동 복구가 취소되었습니다.",
                    kind="cancelled",
                    report=terminal,
                )
            if terminal_status != "completed":
                raw_error = str(
                    terminal.get("error")
                    or (terminal.get("result") or {}).get("reason")
                    or "exact BAT Atlas manifest 복구 실패"
                )
                raise BatchItemError(
                    "Atlas manifest 자동 복구 실패: "
                    + compact_error_message(raw_error, 320),
                    kind="automatic_repair_failed",
                    report={
                        "repair_disposition": REPAIR_UI_BLOCKED,
                        "reason_codes": list(root_reason_codes),
                        REPAIR_FAILURE_KEY: build_repair_failure(
                            request_id=str(terminal.get("request_id") or ""),
                            root_reason_codes=root_reason_codes,
                            planned_actions=[ATLAS_MANIFEST_MIRROR_REPAIR],
                            failed_stage="atlas_manifest_repair",
                            failure_code="automatic_repair_failed",
                            failure_report=str(terminal.get("receipt") or ""),
                            error=compact_error_message(raw_error, 320),
                        ),
                        "original_failure": copy.deepcopy(failure_report),
                        "exact_repair_receipt": copy.deepcopy(terminal),
                    },
                )
            result = copy.deepcopy(terminal.get("result") or {})
            self.log(
                "[자동 복구 완료] Canonical PCG → Atlas manifest · "
                f"{spm.name}"
            )
            return copy.deepcopy(result.get("canonical_refresh") or {})

    def _refresh_canonical_atlas_manifests(self, spm):
        """Synchronize canonical PCG output into Atlas before Blender starts."""
        spm = Path(spm)
        if not should_refresh_canonical_atlas_manifests(spm):
            return {
                "status": "not_applicable",
                "reason": "owner_spm_is_not_an_atlas_producer",
                "target_spm": str(spm),
                "canonical_manifest": None,
                "updated": [],
                "current": [],
                "pending": [],
            }
        try:
            result = refresh_atlas_manifests_for_spm(
                spm,
                require_complete=True,
            )
        except CanonicalOutputManifestError as exc:
            failure_report = copy.deepcopy(getattr(exc, "report", {}) or {})
            decision = repair_ui_decision(failure_report)
            automatic = decision["status"] == REPAIR_UI_AUTOMATIC
            if automatic:
                return self._run_inline_atlas_manifest_repair(
                    spm,
                    {
                        "status": "automatic_repair_pending",
                        "stage": "canonical_atlas_manifest_preflight",
                        "spm": str(spm),
                        "error": str(exc),
                        "repair_disposition": decision["status"],
                        "reason_ko": decision["reason"],
                        "action_ko": decision["action"],
                        **failure_report,
                    },
                )
            raise BatchItemError(
                "최종 차단: Canonical PCG → Atlas 사전 검사 · "
                f"원인: {decision['reason']} · 조치: {decision['action']}",
                kind="data_error",
                report={
                    "status": "failed",
                    "stage": "canonical_atlas_manifest_preflight",
                    "spm": str(spm),
                    "error": str(exc),
                    "repair_disposition": decision["status"],
                    "reason_ko": decision["reason"],
                    "action_ko": decision["action"],
                    **failure_report,
                },
            ) from exc
        if result["updated"]:
            self.log(
                "Canonical PCG → Atlas manifest regenerated before Blender: "
                f"{Path(spm).name} · {len(result['updated'])} file(s)"
            )
        return result

    def _execute_material_preflight(
        self,
        spm,
        speedtree_spm,
        stamp,
    ):
        """Run the existing fast pre-Blender material contract check."""
        spm = Path(spm)
        speedtree_spm = Path(speedtree_spm)
        fbx_ini = Path(self.cfg["fbx_ini"]).resolve()
        try:
            speedtree_cli = fbx_ini.parents[2] / "speedtree_cli.py"
        except IndexError as exc:
            raise BatchItemError(
                f"SpeedTree export helper 경로를 찾을 수 없음: {fbx_ini}",
                kind="data_error",
            ) from exc
        if not speedtree_cli.is_file():
            raise BatchItemError(
                f"SpeedTree export helper 없음: {speedtree_cli}",
                kind="data_error",
            )
        material_report = LOG_DIR / (
            f"{spm.stem}_material_preflight_{stamp}.json"
        )
        material_log_name = f"{spm.stem}_material_preflight_{stamp}.log"
        export_timeout = max(1, int(
            self.cfg.get("speedtree_material_preflight_timeout", 900)
        ))
        stage_timeout = max(1, int(
            self.cfg.get("child_stage_inactivity_timeout", 180)
        ))
        queue_timeout = max(1, int(
            self.cfg.get("speedtree_material_preflight_queue_timeout", 3600)
        ))
        material_cmd = [
            sys.executable,
            str(TOOL_DIR / "jobs" / "speedtree_material_preflight.py"),
            "--spm", str(speedtree_spm),
            "--canonical-spm", str(spm),
            "--speedtree-exe", str(self.cfg["speedtree_exe"]),
            "--fbx-ini", str(fbx_ini),
            "--speedtree-cli", str(speedtree_cli),
            "--report", str(material_report),
            "--timeout", str(export_timeout),
        ]
        last_progress = {
            "bucket": -1,
            "phase": "프로세스 시작",
            "failure_logged": False,
        }
        phase_markers = (
            (MATERIAL_PREFLIGHT_FAILED_MARKER, "실패 보고서 정리 중"),
            (MATERIAL_PREFLIGHT_DONE_MARKER, "완료 처리 중"),
            (MATERIAL_PREFLIGHT_CONTRACT_DONE_MARKER, "보고서 저장 중"),
            (MATERIAL_PREFLIGHT_INSPECTION_DONE_MARKER, "계약 봉투 생성 중"),
            (MATERIAL_PREFLIGHT_EXPORT_DONE_MARKER, "재질/텍스처 검사 중"),
            (SPEEDTREE_SLOT_ACQUIRED_MARKER, "SpeedTree 실행 중"),
            (SPEEDTREE_SLOT_WAIT_MARKER, "SpeedTree 단일 슬롯 대기 중"),
            (MATERIAL_PREFLIGHT_STATIC_DONE_MARKER, "정적 계약 완료"),
            (MATERIAL_PREFLIGHT_START_MARKER, "정적 계약 검사 중"),
        )

        def report_material_progress(elapsed, latest_line):
            for marker, label in phase_markers:
                if latest_line.startswith(marker):
                    last_progress["phase"] = label
                    if (
                        marker == MATERIAL_PREFLIGHT_FAILED_MARKER
                        and not last_progress["failure_logged"]
                    ):
                        self.log(
                            "재질 사전검사 child 실패 단계 보고: "
                            f"{spm.name} · {latest_line}"
                        )
                        last_progress["failure_logged"] = True
                    break
            phase = last_progress["phase"]
            bucket = int(elapsed // 30)
            if (
                bucket == last_progress["bucket"]
                and phase == last_progress.get("reported_phase")
            ):
                return
            last_progress.update(
                bucket=bucket,
                reported_phase=phase,
            )
            self.log(
                f"재질 사전검사 heartbeat: {spm.name} · {phase} "
                f"· 총 {int(elapsed)}초"
            )

        material_code, material_log = self._run_limited(
            material_cmd,
            material_log_name,
            # The child export timeout starts only after the machine-wide gate.
            # Parent safety is therefore phase inactivity, never one combined
            # queue+execution wall-clock deadline.
            None,
            affinity=False,
            progress_callback=report_material_progress,
            inactivity_timeout=stage_timeout,
            inactivity_timeout_by_marker=material_preflight_inactivity_rules(
                stage_timeout, queue_timeout
            ),
        )
        material_result = load_job_report(material_report)
        return {
            "code": material_code,
            "report": material_report,
            "log": material_log,
            "result": material_result,
        }

    def _refresh_cluster_source_relations(self, spm, item):
        """Regenerate stale provider outputs without rebuilding current BWR."""
        from cluster_blend_sync import (
            run_cluster_relation_transaction,
        )

        targets = cluster_relation_output_targets(
            spm,
            item.get("referenced_by_spms") or (),
        )
        if not targets:
            return {
                "status": "pass_through",
                "reason": "no_explicit_owner_relation",
                "targets": [],
            }
        state = cluster_relation_refresh_state(spm, targets)
        if state["current"]:
            return {
                "status": "ok",
                "no_change": True,
                "targets": state["targets"],
            }
        self.log(
            f"Cluster Normalizer/Atlas 자동 재생성: {Path(spm).name}"
            f" · {state['reason']}"
        )
        try:
            with cluster_relation_owner_lock(spm):
                result = run_cluster_relation_transaction(
                    blend_path_for(spm),
                    targets,
                    enabled=True,
                    blender_exe=Path(self.cfg["blender_exe"]),
                    unit_probe_path=Path(self.cfg["cluster_unit_probe"]),
                    capture_resolution=int(
                        self.cfg.get("cluster_capture_resolution", 1024)
                    ),
                    repair_runtime_config=self.cfg,
                    timeout=int(
                        self.cfg.get("blender_job_timeout", 3600)
                    ),
                )
        except Exception as exc:
            raise BatchItemError(
                "Cluster Normalizer/Atlas 자동 재생성 실패: " + str(exc),
                kind="data_error",
            ) from exc
        verified = cluster_relation_refresh_state(spm, targets)
        if not verified["current"]:
            raise BatchItemError(
                "Cluster Normalizer/Atlas 재생성 후 실제 출력 검증 실패: "
                + verified["reason"],
                kind="data_error",
                report=result,
            )
        self.log(f"Cluster Normalizer/Atlas 갱신 완료: {Path(spm).name}")
        return result

    def _job_spm(self, iid, spm):
        spm = Path(spm)
        entry = self.state.setdefault(iid, {})
        item = self._batch_job_item(iid)
        if not should_calibrate_spm(item):
            read_only = item.get("source_read_only")
            summary = (
                "건너뜀 · 읽기 전용 SPM"
                if read_only
                else "건너뜀 · 일반 SPM 본 세팅 제외"
            )
            self.ui_queue.put(("cell", (iid, "spm_status", summary)))
            with self.state_lock:
                entry["spm_status"] = summary
                save_state(self.state)
            reason = "source read-only" if read_only else "Cluster 전용 정책"
            self.log(f"SPM 본 보정 건너뜀 ({reason}): {spm.name}")
            return
        spm = self._prepare_pair_for_job(spm)
        if item.get("manual_bones_locked", False):
            summary = f"{manual_bone_status_text(spm)} · ① 전체 건너뜀"
            self.ui_queue.put(("cell", (iid, "spm_status", summary)))
            with self.state_lock:
                entry["spm_status"] = summary
                save_state(self.state)
            self.log(f"본 세팅 건너뜀 (수동 본 유지): {spm.name}")
            return

        snapshot = self._current_spm_snapshot(item)
        cache = entry.get("calibration_cache")
        cluster_postcondition = current_cluster_root_postcondition(spm)
        if (
            not self.force_rerun
            and calibration_cache_matches(
                cache,
                snapshot["fingerprint"],
                self.spm_calibration_signature,
                getattr(self, "legacy_spm_calibration_signature", None),
            )
            and (
                not is_cluster_source_spm(spm)
                or bool((cluster_postcondition or {}).get("ok"))
            )
        ):
            summary = cache.get("summary", cache.get("status", "캐시"))
            cached_text = f"{summary} ✓ (변경 없음)"
            if cache.get("status") == "not-sk-ready":
                raise RuntimeError(f"SK 미제작: {cache.get('error', '본 설정 필요')} (캐시)")
            self.ui_queue.put(("cell", (iid, "spm_status", cached_text)))
            with self.state_lock:
                cache["settings_signature"] = self.spm_calibration_signature
                entry["spm_status"] = cached_text
                entry["spm_summary"] = summary
                save_state(self.state)
            try:
                write_positive_calibration_receipt(
                    spm,
                    getattr(self, "cfg", {}).get("spm_calibration_receipt_dir")
                    or (TOOL_DIR / "cache" / "spm_calibration"),
                    bone_semantic_fingerprint_value=(
                        current_bone_semantic_fingerprint(spm)
                    ),
                    settings_signature=self.spm_calibration_signature,
                    bone_contract_version=SPM_BONE_CONTRACT_VERSION,
                    report={
                        "status": cache.get("status"),
                        "cached_display_summary": summary,
                        "calibration": {
                            "mode": "migrated_positive_gui_cache",
                        },
                    },
                )
            except Exception as exc:
                self.log(
                    "  [캐시 경고] SPM bone receipt migration failed: "
                    f"{spm.name}: {exc}"
                )
            self.log(f"본 세팅 건너뜀 (SPM/옵션 변경 없음): {spm.name}")
            return

        started = time.perf_counter()
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.log(f"SPM 본 세팅 시작: {spm.name}")
        self.ui_queue.put(("cell", (iid, "spm_status", "캘리브레이션 중...")))
        report_path = LOG_DIR / f"{spm.stem}_spm_{stamp}.json"
        cmd = [sys.executable.replace("pythonw.exe", "python.exe"), "-X", "utf8", "-u",
               str(TOOL_DIR / "spm_audit.py"), str(spm), "--report", str(report_path)]
        if self.force_rerun:
            cmd.append("--force-rerun")
        # In parallel mode, don't pin every worker to the same core subset.
        parallel = self.cfg.get("spm_parallel_jobs", 1) > 1
        progress_state = {"stage": "준비 중", "stage_started": 0.0}

        def report_progress(total_elapsed, latest_line):
            if "[SpeedTree]" in latest_line:
                stage = latest_line.split("[SpeedTree]", 1)[1].strip()
                if stage != progress_state["stage"]:
                    progress_state["stage"] = stage
                    progress_state["stage_started"] = total_elapsed
            stage_elapsed = max(0.0, total_elapsed - progress_state["stage_started"])
            limit = int(self.cfg.get("spm_verify_timeout", 120))
            remaining = max(0, limit - int(stage_elapsed))
            elapsed_text = time.strftime("%M:%S", time.gmtime(stage_elapsed))
            text = (
                f"{progress_state['stage']} · {elapsed_text} "
                f"(수동 전환까지 {remaining}s)"
            )
            self.ui_queue.put(("cell", (iid, "spm_status", text)))

        try:
            code, log_file = self._run_limited(
                cmd,
                f"{spm.stem}_spm_{stamp}.log",
                max(
                    self.cfg.get("spm_verify_timeout", 120) * 5,
                    self.cfg.get("spm_job_timeout", 7200),
                ),
                affinity=not parallel,
                progress_callback=report_progress,
            )
        except BatchItemError:
            raise
        except Exception as exc:
            raise BatchItemError(
                f"본 세팅 실행 실패: {exc}",
                kind="internal_error",
                report_file=report_path,
            ) from exc
        if not report_path.exists():
            raise BatchItemError(
                f"본 세팅 실패 — 실행 보고서 없음 — 로그: {log_file}",
                kind="internal_error",
                log_file=log_file,
                report_file=report_path,
            )
        try:
            report_rows = json.loads(
                report_path.read_text(encoding="utf-8")
            )
            rep = report_rows[0]
            if not isinstance(rep, dict):
                raise ValueError("first SPM report row is not an object")
        except (OSError, ValueError, TypeError, IndexError) as exc:
            raise BatchItemError(
                f"본 세팅 실패 — 실행 보고서 손상: {exc} — 로그: {log_file}",
                kind="internal_error",
                log_file=log_file,
                report_file=report_path,
            ) from exc
        status = rep.get("status")
        if status == "failed" or (code != 0 and status != "not-sk-ready"):
            raise BatchItemError(
                f"본 세팅 실패: {rep.get('error', '?')} — 로그: {log_file}",
                kind=rep.get("failure_kind") or "data_error",
                report=rep,
                log_file=log_file,
                report_file=report_path,
            )
        summary = self._spm_report_summary(rep)
        warn = " ⚠" if rep.get("warnings") else ""
        duration = time.perf_counter() - started
        try:
            final_snapshot = file_content_snapshot(spm)
            item["spm_snapshot"] = final_snapshot
        except OSError as exc:
            final_snapshot = None
            self.log(f"  [캐시 경고] 최종 SPM 지문 계산 실패: {spm.name}: {exc}")
        validate_spm_audit_result(spm, rep, final_snapshot)
        cacheable = status in {"calibrated", "already-ok", "manual-required", "not-sk-ready"}
        chained_repair = (
            getattr(self, "_active_pipeline_terminal_phase", None)
            in {"blender", "push"}
        )
        blend_status = (
            "② 대기 · ① 완료"
            if chained_repair
            else self._blend_status_text(spm)
        )
        with self.state_lock:
            if cacheable and final_snapshot:
                entry["calibration_cache"] = {
                    "version": CALIBRATION_CACHE_VERSION,
                    "spm_fingerprint": final_snapshot["fingerprint"],
                    "settings_signature": self.spm_calibration_signature,
                    "status": status,
                    "summary": summary,
                    "error": rep.get("error", ""),
                    "probe_cache_hit": bool((rep.get("calibration") or {}).get("probe_cache_hit")),
                    "completed_at": datetime.now().isoformat(timespec="seconds"),
                }
            entry["spm_last_duration_seconds"] = round(duration, 3)
            entry["spm_status"] = f"{summary}{warn}"
            entry["spm_summary"] = summary
            entry["blend_status"] = blend_status
            save_state(self.state)
        self.ui_queue.put(("cell", (iid, "blend_status", entry["blend_status"])))
        if status == "not-sk-ready":
            raise RuntimeError(f"SK 미제작: {rep.get('error', '보이는 Branch의 본 설정이 모두 꺼져 있음')}")
        self.ui_queue.put(("cell", (iid, "spm_status", f"{summary}{warn}")))
        for warning in rep.get("warnings", []):
            self.log(f"  [경고] {spm.name}: {warning}")
        if status == "manual-required":
            self.log(f"자동 본 세팅 건너뜀: {spm.name} — 수동 처리 필요 (원본 복원됨)")
        else:
            self.log(f"본 세팅 완료: {spm.name} — {summary}")

    def _reset_cluster_receipt_refresh_memo(self):
        """Start one process-local Cluster audit memo generation.

        One local queue drain owns one generation.  A hit is never trusted by
        age alone: every caller re-fingerprints the production inputs and live
        artifacts before reuse, and the memo is discarded when the queue drains.
        """
        self._cluster_receipt_refresh_memo_lock = threading.Lock()
        self._cluster_receipt_refresh_memo = {}
        self._cluster_receipt_refresh_flights = {}
        self._cluster_receipt_owner_locks = {}

    def _ensure_cluster_receipt_refresh_memo(self):
        if not hasattr(self, "_cluster_receipt_refresh_memo_lock"):
            self._reset_cluster_receipt_refresh_memo()

    def _cluster_receipt_owner_lock(self, spm):
        self._ensure_cluster_receipt_refresh_memo()
        scope = self._cluster_receipt_refresh_scope(spm)
        with self._cluster_receipt_refresh_memo_lock:
            return self._cluster_receipt_owner_locks.setdefault(
                scope,
                threading.Lock(),
            )

    def _wait_cluster_receipt_refresh_flight(self, flight, spm):
        started = time.monotonic()
        next_heartbeat = started + 30.0
        while True:
            stop_flag = getattr(self, "stop_flag", None)
            if stop_flag is not None and stop_flag.is_set():
                raise BatchItemError(
                    "Cluster Assembly live audit wait stopped: "
                    f"{Path(spm).name}",
                    kind="internal_error",
                )
            try:
                return flight.result(timeout=0.2)
            except FutureTimeoutError:
                now = time.monotonic()
                if now >= next_heartbeat:
                    self.log(
                        "Cluster Assembly live audit shared-flight heartbeat: "
                        f"{Path(spm).name} · {int(now - started)}초"
                    )
                    next_heartbeat = now + 30.0

    @staticmethod
    def _cluster_receipt_refresh_scope(spm):
        return os.path.normcase(
            os.path.abspath(str(Path(spm).expanduser()))
        )

    @staticmethod
    def _cluster_contract_identifies_requested_spm(contract, spm):
        """Require an explicit Tree identity for the requested owner SPM."""
        if not isinstance(contract, dict):
            return False
        requested_key = normalized_folder_key(spm)
        for row in contract.get("tree_source_identities") or ():
            if not isinstance(row, dict):
                continue
            for field in ("target_spm", "authoritative_tree_source"):
                identity = row.get(field) or {}
                path = identity.get("path")
                if (
                    path
                    and normalized_folder_key(path) == requested_key
                ):
                    return True
        return False

    @staticmethod
    def _cluster_receipt_live_artifact_records(payload):
        """Return file identities recorded by the immutable audit payload."""
        records = []

        def visit(value):
            if isinstance(value, dict):
                path = value.get("path")
                if (
                    path
                    and any(
                        key in value
                        for key in (
                            "sha256",
                            "fingerprint",
                            "size",
                            "mtime_ns",
                        )
                    )
                ):
                    records.append({
                        key: value.get(key)
                        for key in (
                            "path",
                            "exists",
                            "size",
                            "mtime_ns",
                            "sha256",
                            "fingerprint",
                            "fingerprint_algorithm",
                        )
                        if key in value
                    })
                for child in value.values():
                    visit(child)
            elif isinstance(value, list):
                for child in value:
                    visit(child)

        visit(payload)
        return tuple(records)

    @classmethod
    def _cluster_receipt_live_artifact_paths(
        cls,
        payload,
        *,
        report_path=None,
    ):
        paths = {
            os.path.normcase(
                os.path.abspath(
                    str(Path(record["path"]).expanduser())
                )
            )
            for record in cls._cluster_receipt_live_artifact_records(payload)
        }
        if report_path:
            paths.add(
                os.path.normcase(
                    os.path.abspath(
                        str(Path(report_path).expanduser())
                    )
                )
            )
        return tuple(sorted(paths))

    @classmethod
    def _cluster_receipt_live_artifacts_match(cls, payload):
        """Verify that report evidence still describes the current files."""
        errors = []
        for record in cls._cluster_receipt_live_artifact_records(payload):
            candidate = Path(record["path"]).expanduser()
            exists = candidate.exists()
            if (
                "exists" in record
                and bool(record.get("exists")) != exists
            ):
                errors.append(f"exists changed: {candidate}")
                continue
            if not exists:
                continue
            try:
                stat = candidate.stat()
                if (
                    record.get("size") is not None
                    and int(record["size"]) != stat.st_size
                ):
                    errors.append(f"size changed: {candidate}")
                    continue
                if (
                    record.get("mtime_ns") is not None
                    and int(record["mtime_ns"]) != stat.st_mtime_ns
                ):
                    errors.append(f"mtime changed: {candidate}")
                    continue
                content_key = artifact_record_content_key(record)
                if content_key is None:
                    errors.append(
                        f"content digest missing: {candidate}"
                    )
                    continue
                current_key = file_content_key_snapshot(
                    candidate,
                    content_key["algorithm"],
                )
                if (
                    content_key["digest"].casefold()
                    != current_key["digest"].casefold()
                ):
                    errors.append(
                        f"{content_key['field']} changed: {candidate}"
                    )
            except (OSError, RuntimeError, TypeError, ValueError) as exc:
                errors.append(f"identity unavailable: {candidate}: {exc}")
        return not errors, tuple(errors)

    @staticmethod
    def _cluster_receipt_discovery_input_paths(spm):
        """Discover only contract inputs, never BWR/runtime report JSON."""
        spm = Path(spm).resolve()
        owner = spm.parent
        # Runtime source files are launch-time code, not asset inputs. The
        # compile gate validates them before the GUI starts and every queued
        # job owns one in-memory generation. Including source mtimes here made
        # a developer edit during a long wave look like asset churn and forced
        # an unrelated live-audit retry.
        paths = {spm}

        def is_temporary_contract_path(candidate):
            try:
                relative = candidate.relative_to(owner)
            except ValueError:
                return False
            return any(
                part.casefold()
                in {"_spm_backups", ".sk_batch_isolated_bark"}
                for part in relative.parts
            )

        for candidate in owner.rglob("*.spm"):
            if (
                candidate.is_file()
                and not is_temporary_contract_path(candidate)
            ):
                paths.add(candidate.resolve())

        input_json_patterns = (
            "*.atlas_leaf_targets.json",
            "*_auto_capture_manifest.json",
            "*_normalization_manifest.json",
            "bark_normalization_manifest.json",
            "*_bark_source_manifest.json",
        )
        for pattern in input_json_patterns:
            for candidate in owner.rglob(pattern):
                if (
                    candidate.is_file()
                    and not is_temporary_contract_path(candidate)
                ):
                    paths.add(candidate.resolve())

        import_manifest = owner / "speedtree_import_manifest.json"
        if import_manifest.is_file():
            paths.add(import_manifest.resolve())
        for folder_name in (
            ".atlas_leaf_speedtree_scopes",
            ".atlas_leaf_speedtree_targets",
        ):
            folder = owner / folder_name
            if not folder.is_dir():
                continue
            for candidate in folder.glob("*.json"):
                if candidate.is_file():
                    paths.add(candidate.resolve())
        return paths

    def _cluster_receipt_refresh_input_fingerprint(
        self,
        spm,
        *,
        live_artifact_paths=(),
        _records_out=None,
    ):
        """Fingerprint every owner input and live artifact by current bytes.

        Discovery inputs retain full content fingerprints so a genuine input
        write remains fail-closed.  Additional live artifacts use the shared
        bounded sampled key, avoiding full reads of large textures on each memo
        check.  Size and mtime remain diagnostics and negative-change hints;
        they never authorize a positive hit without a content-derived key.
        Missing paths remain in the key, so a later appearance invalidates it.
        """
        spm = Path(spm).resolve()
        content_paths = self._cluster_receipt_discovery_input_paths(spm)

        live_paths = {
            Path(path).resolve()
            for path in live_artifact_paths
        }
        all_paths = set(live_paths)
        all_paths.update(content_paths)
        records = []
        for candidate in sorted(
            all_paths,
            key=lambda path: os.path.normcase(str(path)),
        ):
            key = os.path.normcase(os.path.abspath(str(candidate)))
            try:
                stat = candidate.stat()
            except FileNotFoundError:
                records.append({"path": key, "exists": False})
                continue
            snapshot = (
                file_content_snapshot(candidate)
                if candidate in content_paths
                else sampled_file_content_snapshot(candidate)
            )
            records.append({
                "path": key,
                "exists": True,
                **snapshot,
            })

        if _records_out is not None:
            _records_out[:] = copy.deepcopy(records)

        envelope = {
            "version": 3,
            "scope": self._cluster_receipt_refresh_scope(spm),
            "files": records,
        }
        encoded = json.dumps(
            envelope,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.blake2b(encoded, digest_size=16).hexdigest()

    @staticmethod
    def _cluster_receipt_changed_input_paths(before_records, after_records):
        """Return paths whose fingerprint-envelope records differ."""
        before_by_path = {
            str(record.get("path")): record
            for record in before_records or ()
            if isinstance(record, dict) and record.get("path")
        }
        after_by_path = {
            str(record.get("path")): record
            for record in after_records or ()
            if isinstance(record, dict) and record.get("path")
        }
        return tuple(sorted(
            path
            for path in set(before_by_path) | set(after_by_path)
            if before_by_path.get(path) != after_by_path.get(path)
        ))

    @staticmethod
    def _cluster_receipt_stability_failure_message(
        *,
        fingerprint_changed,
        changed_input_paths=(),
        artifact_errors=(),
    ):
        """Describe each failed half of the live-audit stability gate."""
        artifact_errors = tuple(artifact_errors or ())
        if fingerprint_changed and artifact_errors:
            heading = (
                "Input fingerprint changed + live artifact mismatch "
                "during audit"
            )
        elif fingerprint_changed:
            heading = "Input fingerprint changed during audit"
        else:
            heading = "Live artifact mismatch"

        details = []
        if fingerprint_changed:
            changed_detail = ", ".join(changed_input_paths[:3])
            details.append(
                "changed paths: "
                + (changed_detail or "unavailable from fingerprint records")
            )
        if artifact_errors:
            details.append("; ".join(artifact_errors[:3]))
        return heading + (f": {'; '.join(details)}" if details else "")

    def _refresh_stale_cluster_receipt(
        self,
        spm,
        stamp,
    ):
        """Reuse one hash-current live audit inside the active queue drain.

        Successful raw audits only are memoized. Concurrent callers for the
        same owner/input share one Future; an execution exception reaches all
        existing waiters but is removed immediately so a later caller may
        retry.
        """
        spm = Path(spm).resolve()
        self._assert_active_production_source_manifest()
        if not (spm.parent / "Cluster").is_dir():
            return self._refresh_stale_cluster_receipt_uncached(
                spm,
                stamp,
            )

        self._ensure_cluster_receipt_refresh_memo()
        scope = self._cluster_receipt_refresh_scope(spm)
        cached_hit = False
        cached_raw_audit = None
        while True:
            with self._cluster_receipt_refresh_memo_lock:
                cached = self._cluster_receipt_refresh_memo.get(scope)
                cached_live_artifact_paths = (
                    (cached or {}).get("live_artifact_paths") or ()
                )
            pre_discovery_records = []
            try:
                current_cache_fingerprint = (
                    self._cluster_receipt_refresh_input_fingerprint(
                        spm,
                        live_artifact_paths=cached_live_artifact_paths,
                    )
                )
                pre_discovery_fingerprint = (
                    self._cluster_receipt_refresh_input_fingerprint(
                        spm,
                        _records_out=pre_discovery_records,
                    )
                )
            except (OSError, RuntimeError) as exc:
                raise BatchItemError(
                    "Cluster Assembly live audit input fingerprint failed: "
                    f"{spm.name}: {exc}",
                    kind="internal_error",
                ) from exc
            cached_artifacts_match = False
            if cached is not None:
                cached_artifacts_match, _cached_artifact_errors = (
                    self._cluster_receipt_live_artifacts_match(
                        (cached.get("raw_audit") or {}).get("payload") or {}
                    )
                )
            with self._cluster_receipt_refresh_memo_lock:
                # Another owner-equivalent caller may have published a newer
                # immutable cache entry while hashes were calculated. Validate
                # that entry outside the lock instead of accepting or
                # overwriting it based on the older snapshot.
                if self._cluster_receipt_refresh_memo.get(scope) is not cached:
                    continue
                if (
                    cached is not None
                    and cached.get("input_fingerprint")
                    == current_cache_fingerprint
                    and cached.get("discovery_fingerprint")
                    == pre_discovery_fingerprint
                    and cached_artifacts_match
                ):
                    cached_raw_audit = cached.get("raw_audit")
                    cached_hit = True
                    flight_key = None
                    flight = None
                    owns_flight = False
                else:
                    flight_key = (scope, pre_discovery_fingerprint)
                    flight = self._cluster_receipt_refresh_flights.get(
                        flight_key
                    )
                    if flight is None:
                        flight = Future()
                        self._cluster_receipt_refresh_flights[
                            flight_key
                        ] = flight
                        owns_flight = True
                    else:
                        owns_flight = False
                break

        if cached_hit:
            if hasattr(self, "log"):
                self.log(
                    "Cluster Assembly live audit memo hit: "
                    f"{spm.name}"
                )
            return self._evaluate_cluster_receipt_live_audit(
                spm,
                copy.deepcopy(cached_raw_audit),
            )
        if not owns_flight:
            raw_audit = copy.deepcopy(
                self._wait_cluster_receipt_refresh_flight(flight, spm)
            )
            return self._evaluate_cluster_receipt_live_audit(
                spm,
                raw_audit,
            )

        completion_error = None
        cache_entry = None
        try:
            attempt_pre_fingerprint = pre_discovery_fingerprint
            attempt_pre_records = pre_discovery_records
            for attempt in range(2):
                audit_stamp = (
                    stamp
                    if attempt == 0
                    else f"{stamp}_retry{attempt}"
                )
                try:
                    raw_audit = self._refresh_stale_cluster_receipt_uncached(
                        spm,
                        audit_stamp,
                        _raw_only=True,
                    )
                except InlineAtlasRepairRequested as repair_request:
                    self._run_inline_atlas_manifest_repair(
                        repair_request.target_spm,
                        repair_request.report,
                    )
                    try:
                        raw_audit = self._refresh_stale_cluster_receipt_uncached(
                            spm,
                            f"{audit_stamp}_atlas_repaired",
                            _raw_only=True,
                        )
                    except InlineAtlasRepairRequested as repeated:
                        raise BatchItemError(
                            "Atlas manifest 자동 복구 후 재검사에도 같은 충돌이 남았습니다.",
                            kind="automatic_repair_failed",
                            report={
                                "stage": "cluster_assembly_live_audit",
                                "repair_disposition": REPAIR_UI_BLOCKED,
                                "reason_codes": list(
                                    repair_root_reason_codes(
                                        repair_request.report
                                    )
                                ),
                                REPAIR_FAILURE_KEY: build_repair_failure(
                                    root_reason_codes=(
                                        repair_root_reason_codes(
                                            repair_request.report
                                        )
                                    ),
                                    planned_actions=[
                                        ATLAS_MANIFEST_MIRROR_REPAIR
                                    ],
                                    failed_stage="atlas_manifest_repair",
                                    failure_code=(
                                        "automatic_repair_reaudit_failed"
                                    ),
                                    failure_report=str(
                                        repeated.report_file or ""
                                    ),
                                    error=(
                                        "same Atlas manifest conflict after "
                                        "the exact mirror repair"
                                    ),
                                ),
                                "first_failure": copy.deepcopy(
                                    repair_request.report
                                ),
                                "repeated_failure": copy.deepcopy(
                                    repeated.report
                                ),
                            },
                            log_file=repeated.log_file,
                            report_file=repeated.report_file,
                        ) from repeated
                post_discovery_records = []
                post_discovery_fingerprint = (
                    self._cluster_receipt_refresh_input_fingerprint(
                        spm,
                        _records_out=post_discovery_records,
                    )
                )
                artifacts_match, artifact_errors = (
                    self._cluster_receipt_live_artifacts_match(
                        raw_audit.get("payload") or {}
                    )
                )
                fingerprint_changed = (
                    attempt_pre_fingerprint
                    != post_discovery_fingerprint
                )
                stable = bool(
                    not fingerprint_changed
                    and artifacts_match
                )
                observed_discovery_records = post_discovery_records
                if stable:
                    live_artifact_paths = (
                        self._cluster_receipt_live_artifact_paths(
                            raw_audit.get("payload") or {},
                            report_path=raw_audit.get("audit_report"),
                        )
                    )
                    post_cache_fingerprint = (
                        self._cluster_receipt_refresh_input_fingerprint(
                            spm,
                            live_artifact_paths=live_artifact_paths,
                        )
                    )
                    final_discovery_records = []
                    final_discovery_fingerprint = (
                        self._cluster_receipt_refresh_input_fingerprint(
                            spm,
                            _records_out=final_discovery_records,
                        )
                    )
                    final_artifacts_match, final_artifact_errors = (
                        self._cluster_receipt_live_artifacts_match(
                            raw_audit.get("payload") or {}
                        )
                    )
                    fingerprint_changed = (
                        final_discovery_fingerprint
                        != attempt_pre_fingerprint
                    )
                    stable = bool(
                        not fingerprint_changed
                        and final_artifacts_match
                    )
                    if stable:
                        self._assert_active_production_source_manifest()
                        cache_entry = {
                            "input_fingerprint": post_cache_fingerprint,
                            "discovery_fingerprint": (
                                final_discovery_fingerprint
                            ),
                            "live_artifact_paths": live_artifact_paths,
                            "raw_audit": copy.deepcopy(raw_audit),
                        }
                        break
                    artifact_errors = final_artifact_errors
                    observed_discovery_records = final_discovery_records
                changed_input_paths = (
                    self._cluster_receipt_changed_input_paths(
                        attempt_pre_records,
                        observed_discovery_records,
                    )
                    if fingerprint_changed
                    else ()
                )
                failure_message = (
                    self._cluster_receipt_stability_failure_message(
                        fingerprint_changed=fingerprint_changed,
                        changed_input_paths=changed_input_paths,
                        artifact_errors=artifact_errors,
                    )
                )
                if attempt == 0:
                    self.log(
                        f"{failure_message}; retrying once: {spm.name}"
                    )
                    attempt_pre_records = []
                    attempt_pre_fingerprint = (
                        self._cluster_receipt_refresh_input_fingerprint(
                            spm,
                            _records_out=attempt_pre_records,
                        )
                    )
                    continue
                raise BatchItemError(
                    f"{failure_message}; result was not cached: {spm.name}",
                    kind="internal_error",
                    log_file=raw_audit.get("log_file"),
                    report_file=raw_audit.get("audit_report"),
                )
        except Exception as exc:
            completion_error = exc

        publish_error = completion_error
        with self._cluster_receipt_refresh_memo_lock:
            try:
                if publish_error is None and cache_entry is not None:
                    self._cluster_receipt_refresh_memo[scope] = cache_entry
                if publish_error is None:
                    flight.set_result(copy.deepcopy(raw_audit))
                else:
                    flight.set_exception(publish_error)
            except Exception as exc:
                publish_error = publish_error or exc
                if (
                    cache_entry is not None
                    and self._cluster_receipt_refresh_memo.get(scope)
                    is cache_entry
                ):
                    self._cluster_receipt_refresh_memo.pop(scope, None)
                if not flight.done():
                    try:
                        flight.set_exception(publish_error)
                    except Exception:
                        pass
            finally:
                if (
                    self._cluster_receipt_refresh_flights.get(flight_key)
                    is flight
                ):
                    self._cluster_receipt_refresh_flights.pop(
                        flight_key,
                        None,
                    )

        if publish_error is not None:
            raise publish_error
        return self._evaluate_cluster_receipt_live_audit(
            spm,
            raw_audit,
        )

    def _refresh_stale_cluster_receipt_uncached(
        self,
        spm,
        stamp,
        *,
        _raw_only=False,
        _persist_receipt=True,
    ):
        """Run the live PCG audit before launching Blender for owner Trees.

        The live PCG audit is authoritative.  A receipt only snapshots the
        artifacts that existed when that audit ran; a hash-current receipt can
        still encode an older contract decision after producer code changes.
        Therefore a Tree with a real Cluster folder is always re-audited.  A
        missing dependency is reported as the audit's real data issue instead
        of making the receipt invalidate itself.  A clean live contract remains
        usable when the optional persisted receipt cannot be written or
        reselected.
        """
        spm = Path(spm).resolve()
        with self._cluster_receipt_owner_lock(spm):
            owner_has_cluster = (spm.parent / "Cluster").is_dir()
            try:
                cached_resolution = cluster_assembly_receipt_resolution(spm)
                if not owner_has_cluster:
                    return cached_resolution
                self.log(f"Cluster Assembly live contract 확인: {spm.name}")
            except FileNotFoundError:
                # Only an owner Tree with a real Cluster child can be missing
                # an actionable receipt. Ordinary vegetation and the Cluster
                # source rows themselves remain pass-through.
                if not owner_has_cluster:
                    return None
                self.log(
                    f"Cluster Assembly 영수증 탐색: {spm.name} "
                    "(저장된 영수증 없음; 현재 폴더 감사)"
                )
            except (
                ClusterAssemblyReceiptStaleError,
                ClusterAssemblyReceiptAmbiguityError,
            ):
                self.log(
                    f"Cluster Assembly 영수증 갱신: {spm.name} "
                    f"(현재 산출물 해시 재감사)"
                )

            scope_hash = hashlib.blake2b(
                self._cluster_receipt_refresh_scope(spm).encode("utf-8"),
                digest_size=8,
            ).hexdigest()
            active_job = getattr(self, "active_batch_job", None)
            job_id = (
                str(active_job.get("id"))
                if isinstance(active_job, dict) and active_job.get("id")
                else "adhoc"
            )
            run_identity = (
                f"{spm.stem}_{scope_hash}_job{job_id}_{stamp}_"
                f"{uuid.uuid4().hex[:12]}"
            )
            audit_report = LOG_DIR / f"{run_identity}.json"
            audit_script = (
                REPO_DIR
                / "pcg_st9_texture_batch"
                / "pcg_texture_audit.py"
            )
            expected_manifest = self._assert_active_production_source_manifest()
            expected_production_source_revision = str(
                expected_manifest.content_hash
            ).casefold()
            audit_timeout = max(1, int(
                self.cfg.get("cluster_receipt_refresh_timeout", 600)
            ))
            stage_timeout = max(1, int(
                self.cfg.get("child_stage_inactivity_timeout", 180)
            ))
            audit_command = [
                sys.executable,
                str(audit_script),
                "--target", str(spm.parent),
                "--target-mesh", (
                    spm.stem[3:]
                    if spm.stem.casefold().startswith("sk_")
                    else spm.stem
                ),
                "--expected-production-source-revision",
                expected_production_source_revision,
                "--json", str(audit_report),
            ]
            if not _persist_receipt:
                audit_command.append("--no-receipt")
            audit_progress = {
                "bucket": -1,
                "phase": "프로세스 시작",
                "completed": "",
            }
            phase_markers = (
                (CLUSTER_LIVE_AUDIT_FAILED_MARKER, "실패 보고서 정리 중"),
                (CLUSTER_LIVE_AUDIT_DONE_MARKER, "완료 처리 중"),
                (CLUSTER_LIVE_AUDIT_RECEIPT_DONE_MARKER, "보고서 저장 중"),
                (CLUSTER_LIVE_AUDIT_RECEIPT_START_MARKER, "영수증 검증/기록 중"),
                (CLUSTER_LIVE_AUDIT_REPORT_DONE_MARKER, "production revision 재검증 중"),
                (CLUSTER_LIVE_AUDIT_FOLDER_DONE_MARKER, "에셋 폴더 감사 중"),
                (CLUSTER_LIVE_AUDIT_REPORT_START_MARKER, "live evidence 감사 중"),
                (CLUSTER_LIVE_AUDIT_REVISION_OK_MARKER, "설정/대상 로드 중"),
                (CLUSTER_LIVE_AUDIT_START_MARKER, "production revision 검사 중"),
            )

            def report_live_audit_progress(elapsed, latest_line):
                for marker, label in phase_markers:
                    if latest_line.startswith(marker):
                        audit_progress["phase"] = label
                        if marker == CLUSTER_LIVE_AUDIT_FOLDER_DONE_MARKER:
                            fields = dict(
                                token.split("=", 1)
                                for token in latest_line.split()
                                if "=" in token
                            )
                            audit_progress["completed"] = (
                                f" · {fields.get('completed', '?')}/"
                                f"{fields.get('total', '?')}"
                            )
                        break
                bucket = int(elapsed // 30)
                if (
                    bucket == audit_progress["bucket"]
                    and audit_progress["phase"]
                    == audit_progress.get("reported_phase")
                ):
                    return
                audit_progress.update(
                    bucket=bucket,
                    reported_phase=audit_progress["phase"],
                )
                self.log(
                    "Cluster Assembly live audit heartbeat: "
                    f"{spm.name} · {audit_progress['phase']}"
                    f"{audit_progress['completed']} · 총 {int(elapsed)}초"
                )

            code, log_file = self._run_limited(
                audit_command,
                f"{run_identity}.log",
                # A multi-folder audit may run longer than one folder budget as
                # long as the child keeps publishing real progress.
                None,
                affinity=False,
                progress_callback=report_live_audit_progress,
                inactivity_timeout=stage_timeout,
                inactivity_timeout_by_marker={
                    CLUSTER_LIVE_AUDIT_START_MARKER: stage_timeout,
                    CLUSTER_LIVE_AUDIT_REVISION_OK_MARKER: stage_timeout,
                    CLUSTER_LIVE_AUDIT_REPORT_START_MARKER: audit_timeout,
                    CLUSTER_LIVE_AUDIT_FOLDER_DONE_MARKER: audit_timeout,
                    CLUSTER_LIVE_AUDIT_REPORT_DONE_MARKER: stage_timeout,
                    CLUSTER_LIVE_AUDIT_RECEIPT_START_MARKER: audit_timeout,
                    CLUSTER_LIVE_AUDIT_RECEIPT_DONE_MARKER: stage_timeout,
                    CLUSTER_LIVE_AUDIT_FAILED_MARKER: stage_timeout,
                    CLUSTER_LIVE_AUDIT_DONE_MARKER: stage_timeout,
                },
            )

            payload_error = None
            try:
                payload = json.loads(
                    audit_report.read_text(encoding="utf-8")
                )
            except (OSError, TypeError, ValueError) as exc:
                payload = None
                payload_error = exc
            self._require_child_production_source_manifest(
                payload,
                expected_manifest,
                report_file=audit_report,
                log_file=log_file,
            )
            self._assert_active_production_source_manifest()
            persistence = (
                payload.get("cluster_assembly_receipt_persistence") or {}
                if isinstance(payload, dict)
                else {}
            )
            persistence_only_failure = bool(
                persistence.get("status") == "warning"
                and persistence.get("stage") == "receipt_persistence"
                and persistence.get("code")
                == "RECEIPT_PERSISTENCE_FAILED"
                and persistence.get("live_audit_complete") is True
            )
            if code != 0 and not persistence_only_failure:
                failure_report = copy.deepcopy(
                    (payload or {}).get("failure") or {}
                )
                failure_token = str(
                    failure_report.get("reason_token") or ""
                )
                if failure_token.startswith("atlas_manifest_"):
                    decision = repair_ui_decision(failure_report)
                    repair_evidence = failure_report.get("evidence") or {}
                    repair_target = str(
                        repair_evidence.get("target_spm") or ""
                    )
                    if (
                        decision["status"] == REPAIR_UI_AUTOMATIC
                        and repair_target
                    ):
                        raise InlineAtlasRepairRequested(
                            repair_target,
                            {
                                "status": "automatic_repair_pending",
                                "stage": "cluster_assembly_live_audit",
                                "owner_spm": str(spm),
                                "repair_disposition": decision["status"],
                                "reason_ko": decision["reason"],
                                "action_ko": decision["action"],
                                **failure_report,
                            },
                            log_file=log_file,
                            report_file=audit_report,
                        )
                    status_prefix = (
                        "자동 복구 실패"
                        if decision["status"] == REPAIR_UI_AUTOMATIC
                        else "최종 차단"
                    )
                    audit_root_codes = repair_root_reason_codes(
                        failure_report
                    )
                    raise BatchItemError(
                        f"{status_prefix}: Cluster Assembly live audit · "
                        f"원인: {decision['reason']} · 조치: {decision['action']}",
                        kind=(
                            "automatic_repair_failed"
                            if decision["status"] == REPAIR_UI_AUTOMATIC
                            else "data_error"
                        ),
                        report={
                            "stage": "cluster_assembly_live_audit",
                            "repair_disposition": decision["status"],
                            "reason_ko": decision["reason"],
                            "action_ko": decision["action"],
                            "reason_codes": list(audit_root_codes),
                            REPAIR_FAILURE_KEY: build_repair_failure(
                                root_reason_codes=audit_root_codes,
                                failed_stage="cluster_assembly_live_audit",
                                failure_code=failure_token,
                                failure_report=str(audit_report or ""),
                            ),
                            **failure_report,
                        },
                        log_file=log_file,
                        report_file=audit_report,
                    )
                raise BatchItemError(
                    "Cluster Assembly live audit process failed: "
                    f"{spm.name} (exit {code})",
                    kind="internal_error",
                    report=payload if isinstance(payload, dict) else None,
                    log_file=log_file,
                    report_file=audit_report,
                )
            if (
                not isinstance(payload, dict)
                or not isinstance(payload.get("items"), list)
                or not payload.get("items")
            ):
                raise BatchItemError(
                    "Cluster Assembly live audit report is missing, corrupt, "
                    f"or empty: {spm.name}"
                    + (f": {payload_error}" if payload_error else ""),
                    kind="internal_error",
                    log_file=log_file,
                    report_file=audit_report,
                )
            if code != 0:
                self.log(
                    "Cluster Assembly 영수증 저장 경고 무시 "
                    f"(live audit 완료): {spm.name}: "
                    f"{persistence.get('error') or f'exit {code}'}"
                )
            try:
                from cluster_assembly_handoff_contract import (
                    select_cluster_contract,
                )
                selected_contract = select_cluster_contract(payload, spm)
            except (ImportError, ValueError) as exc:
                raise BatchItemError(
                    "Cluster Assembly live audit could not select an "
                    f"identity-bound owner contract: {spm.name}: {exc}",
                    kind="internal_error",
                    log_file=log_file,
                    report_file=audit_report,
                ) from exc
            if (
                owner_has_cluster
                and (
                    not isinstance(selected_contract, dict)
                    or not selected_contract.get("tree_source_identities")
                    or not self._cluster_contract_identifies_requested_spm(
                        selected_contract,
                        spm,
                    )
                )
            ):
                raise BatchItemError(
                    "Cluster Assembly live audit did not return a strict "
                    f"identity-bound owner contract: {spm.name}",
                    kind="internal_error",
                    log_file=log_file,
                    report_file=audit_report,
                )
            raw_audit = {
                "requested_spm": str(spm),
                "payload": payload,
                "selected_contract": selected_contract,
                "audit_report": str(audit_report),
                "log_file": str(log_file) if log_file else None,
                "persistence": persistence,
            }
            if _raw_only:
                return raw_audit
            return self._evaluate_cluster_receipt_live_audit(
                spm,
                raw_audit,
            )

    def _evaluate_cluster_receipt_live_audit(
        self,
        spm,
        raw_audit,
    ):
        """Require one reusable live observation to be fully ready."""
        spm = Path(spm).resolve()
        raw_audit = raw_audit if isinstance(raw_audit, dict) else {}
        payload = raw_audit.get("payload") or {}
        audit_report = Path(
            raw_audit.get("audit_report")
            or (
                LOG_DIR
                / f"{spm.stem}_cluster_receipt_refresh_unknown.json"
            )
        )
        log_value = raw_audit.get("log_file")
        log_file = Path(log_value) if log_value else None
        persistence = (
            raw_audit.get("persistence")
            or payload.get("cluster_assembly_receipt_persistence")
            or {}
        )

        live_issues = []
        for audit_item in payload.get("items") or []:
            handoff = (
                (audit_item.get("cluster_assembly") or {}).get("handoff")
                or {}
            )
            live_issues.extend(
                handoff.get("errors")
                or handoff.get("issues")
                or []
            )

        actual_failure = cluster_issue_summary(live_issues)

        if actual_failure:
            decision = repair_ui_decision({"issues": live_issues})
            # This gate used to print 자동 복구 대상 and then terminate the
            # target without ever consulting the planner (#160).  A block the
            # registry can act on now leaves through the same typed exclusion
            # the repair path admits; everything else stays terminal.
            selected = raw_audit.get("selected_contract")
            if not isinstance(selected, dict):
                try:
                    from cluster_assembly_handoff_contract import (
                        select_cluster_contract,
                    )
                    selected = select_cluster_contract(payload, spm)
                except (ImportError, ValueError):
                    selected = None
            audit_producer, audit_block = cluster_live_audit_target_block(
                selected if isinstance(selected, dict) else {},
                spm,
            )
            if (
                audit_block
                and audit_producer is not None
                and has_repair_contract_evidence({"issues": live_issues})
            ):
                evidence = {
                    "audit_report": str(audit_report),
                    "target_spm": audit_block["target_spm"],
                    "target_name": audit_block["target_name"],
                    "delivery_mode": audit_block["delivery_mode"],
                    "delivery_errors": audit_block["delivery_errors"],
                    "delivery_remedy": audit_block["delivery_remedy"],
                    "stale_node_table_target_mesh_ids": audit_block[
                        "stale_node_table_target_mesh_ids"
                    ],
                    "live_node_table": audit_block["live_node_table"],
                    "issue_codes": sorted({
                        str(issue.get("code") or "")
                        for issue in live_issues
                        if isinstance(issue, dict) and issue.get("code")
                    }),
                    "issues": copy.deepcopy(live_issues),
                    "gate": "cluster_assembly_live_audit",
                }
                raise TargetPlannedExclusionError(
                    "현재 Cluster Assembly 전달이 차단됨: "
                    + target_planned_exclusion_summary(
                        spm,
                        audit_block["reason_token"],
                        evidence,
                    ),
                    reason_token=audit_block["reason_token"],
                    target_spm=spm,
                    producer_spm=audit_producer,
                    evidence=evidence,
                    log_file=log_file,
                    report_file=audit_report,
                )
            raise BatchItemError(
                f"최종 차단: {spm.name} Cluster Assembly 데이터 검사 · "
                f"원인: {decision['reason']} · 조치: {decision['action']}",
                kind="data_error",
                report={
                    "stage": "cluster_assembly_live_audit",
                    "spm": str(spm),
                    "repair_disposition": decision["status"],
                    "reason_ko": decision["reason"],
                    "action_ko": decision["action"],
                    "issues": copy.deepcopy(live_issues),
                    "internal_summary": actual_failure,
                },
                log_file=log_file,
                report_file=audit_report,
            )

        live_contract = copy.deepcopy(raw_audit.get("selected_contract"))
        if not isinstance(live_contract, dict):
            try:
                from cluster_assembly_handoff_contract import (
                    select_cluster_contract,
                )
                live_contract = select_cluster_contract(payload, spm)
            except (ImportError, ValueError):
                live_contract = None
        if not isinstance(live_contract, dict):
            live_contract = None
        if (
            live_contract is not None
            and not self._cluster_contract_identifies_requested_spm(
                live_contract,
                spm,
            )
        ):
            raise BatchItemError(
                "Cluster Assembly live audit selected a contract for a "
                f"different owner SPM: {spm.name}",
                kind="internal_error",
                log_file=log_file,
                report_file=audit_report,
            )

        live_contract_actionable = bool(
            live_contract
            and live_contract.get("dependencies")
            and live_contract.get("tree_source_identities")
        )
        if not live_contract_actionable:
            # A clean identity-bound pass-through still has to reach BWR so an
            # older cache cannot reintroduce a removed Cluster relationship.
            self.log(f"Cluster Assembly 영수증 비대상: {spm.name}")
            if (
                live_contract
                and live_contract.get("tree_source_identities")
                and str(
                    (live_contract.get("handoff") or {}).get("status")
                    or ""
                ) == "pass_through"
            ):
                return {
                    "policy": "live_audit_authoritative_pass_through",
                    "requested_spm": str(spm),
                    "selected_receipt": str(audit_report),
                    "live_audit_report": str(audit_report),
                    "live_audit_payload": copy.deepcopy(payload),
                    "selected_contract": copy.deepcopy(live_contract),
                    "persisted_receipt": None,
                    "current_candidates": [],
                    "superseded_current_receipts": [],
                    "ignored_stale_candidates": [],
                    "receipt_persistence_warning": str(
                        persistence.get("error") or ""
                    ),
                }
            return None

        persistence_error = str(persistence.get("error") or "")
        persisted_resolution = None
        ignored_stale = []
        cache_resolution_error = ""
        try:
            persisted_resolution = cluster_assembly_receipt_resolution(spm)
        except (
            FileNotFoundError,
            ClusterAssemblyReceiptStaleError,
            ClusterAssemblyReceiptAmbiguityError,
        ) as exc:
            cache_resolution_error = str(exc)
            ignored_stale.append({
                "path": "",
                "error": cache_resolution_error,
            })
            self.log(
                "Cluster Assembly live audit 사용 "
                f"(영수증은 캐시 경고): {spm.name}: "
                + (persistence_error or cache_resolution_error)
            )
        else:
            self.log(
                f"Cluster Assembly live contract 검증 완료: {spm.name}"
            )

        return {
            "policy": "live_audit_authoritative",
            "requested_spm": str(spm),
            "selected_receipt": str(audit_report),
            "live_audit_report": str(audit_report),
            "live_audit_payload": copy.deepcopy(payload),
            "selected_contract": copy.deepcopy(live_contract),
            "persisted_receipt": (
                (persisted_resolution or {}).get("selected_receipt")
            ),
            "current_candidates": (
                (persisted_resolution or {}).get("current_candidates", [])
            ),
            "superseded_current_receipts": (
                (persisted_resolution or {}).get(
                    "superseded_current_receipts", []
                )
            ),
            "ignored_stale_candidates": (
                (persisted_resolution or {}).get(
                    "ignored_stale_candidates", []
                )
                + ignored_stale
            ),
            "receipt_persistence_warning": (
                persistence_error or cache_resolution_error
            ),
        }

    def _cluster_normalization_stage_observation(
        self,
        target_spm,
        stamp,
        producer_spm,
        *,
        require_normalized,
    ):
        """Inspect one producer-owned slice without declaring the Tree ready.

        A Tree contract may contain several independent Cluster providers.
        Producer work owns only the issue rows bound to its exact canonical
        SPM.  Other providers remain separate scheduled jobs; global issues
        remain blockers.  This is a stage input/output contract, not a
        successful Assembly audit and never substitutes for the strict Tree
        audit that runs after all dependencies complete.
        """
        target = Path(target_spm).resolve()
        producer = Path(producer_spm).resolve()
        raw_audit = self._refresh_stale_cluster_receipt_uncached(
            target,
            stamp,
            _raw_only=True,
            _persist_receipt=False,
        )
        contract = copy.deepcopy(raw_audit.get("selected_contract"))
        dependency = cluster_contract_dependency_for_spm(contract, producer)
        audit_report = Path(raw_audit.get("audit_report") or "")
        log_value = raw_audit.get("log_file")
        log_file = Path(log_value) if log_value else None
        if dependency is None:
            raise BatchItemError(
                "Cluster relation/content mismatch: "
                f"{producer.name} is registered for {target.name} but is not "
                "a live content dependency",
                kind="data_error",
                log_file=log_file,
                report_file=audit_report,
            )

        owned_issues = []
        global_issues = []
        dependency_keys = {
            normalized_folder_key(value)
            for row in (contract or {}).get("dependencies") or ()
            if isinstance(row, dict)
            for field in ("spm", "source_spm", "authoring_spm", "output_spm")
            for value in (row.get(field),)
            if value
        }
        for issue in cluster_contract_issues(contract):
            issue_spm = issue.get("spm")
            if not issue_spm:
                global_issues.append(issue)
            elif (
                normalized_folder_key(issue_spm)
                == normalized_folder_key(producer)
            ):
                owned_issues.append(issue)
            elif normalized_folder_key(issue_spm) not in dependency_keys:
                global_issues.append(issue)

        normalizable_codes = {
            "NORMALIZED_VARIANTS_REQUIRED",
            "NORMALIZED_VARIANTS_STALE",
        }
        unexpected_owned = [
            issue
            for issue in owned_issues
            if str(issue.get("code") or "") not in normalizable_codes
        ]
        blocking = (
            global_issues + owned_issues
            if require_normalized
            else global_issues + unexpected_owned
        )
        normalized_variants = dependency.get("normalized_variants")
        variants_required = bool(
            dependency.get("normalized_variants_required")
        )
        if (
            require_normalized
            and variants_required
            and not normalized_variants
            and not owned_issues
        ):
            blocking.append({
                "code": "NORMALIZED_VARIANTS_REQUIRED",
                "spm": str(producer),
            })
        if blocking:
            target_block = cluster_target_delivery_block(
                contract,
                target,
                producer,
            )
            blocking_codes = {
                str(issue.get("code") or "")
                for issue in blocking
                if isinstance(issue, dict)
            }
            # Admission is the registry's call, not a per-gate allowlist.
            # The old `blocking_codes <= {two codes}` guard meant every new
            # repairable reason -- canonical_bark_normalization_required,
            # normalized_variants_required, normalized_variants_stale --
            # printed 자동 복구 대상 and then terminated the target, because
            # nobody remembered to widen this set (#160).
            # `not global_issues` stays: target-local isolation (#16).
            # Disposition authority stays with build_exact_target_repair_plan(),
            # which returns unsupported for anything it cannot act on, and the
            # exclusion then propagates as a visible planned exclusion.
            if (
                target_block
                and not global_issues
                and has_repair_contract_evidence({"issues": blocking})
            ):
                reason_token = target_block["reason_token"]
                evidence = {
                    "audit_report": str(audit_report),
                    "target_spm": target_block["target_spm"],
                    "target_name": target_block["target_name"],
                    "delivery_mode": target_block["delivery_mode"],
                    "delivery_errors": target_block["delivery_errors"],
                    "delivery_remedy": target_block["delivery_remedy"],
                    "stale_node_table_target_mesh_ids": target_block[
                        "stale_node_table_target_mesh_ids"
                    ],
                    "live_node_table": target_block["live_node_table"],
                    "issue_codes": sorted(blocking_codes),
                    "push_readiness": "blocked_by_current_live_delivery",
                    "sync_outcome_authoritative": False,
                    "normalization_postcondition": "not_run",
                }
                evidence["stale_node_table_recovery"] = (
                    cluster_stale_node_table_recovery_scope(
                        contract,
                        target,
                        audit_report,
                    )
                )
                summary = target_planned_exclusion_summary(
                    target,
                    reason_token,
                    evidence,
                )
                raise TargetPlannedExclusionError(
                    "현재 Generator 전달이 차단됨: " + summary,
                    reason_token=reason_token,
                    target_spm=target,
                    producer_spm=producer,
                    evidence=evidence,
                    log_file=log_file,
                    report_file=audit_report,
                )
            summary = cluster_issue_summary(blocking)
            stage = "출력" if require_normalized else "입력"
            decision = repair_ui_decision({"issues": blocking})
            # Reaching here means no repair will run for this row, so it may
            # not claim one is coming.  A row cannot be labelled 자동 복구 대상
            # and be terminal at the same time (#160).
            raise BatchItemError(
                f"최종 차단: {target.name} Cluster 정규화 {stage} 검사 · "
                f"원인: {decision['reason']} · 조치: {decision['action']}",
                kind="data_error",
                report={
                    "stage": "cluster_normalization_validation",
                    "validation_side": stage,
                    "target_spm": str(target),
                    "producer_spm": str(producer),
                    "repair_disposition": decision["status"],
                    "reason_ko": decision["reason"],
                    "action_ko": decision["action"],
                    "issues": copy.deepcopy(blocking),
                    "internal_summary": summary,
                },
                log_file=log_file,
                report_file=audit_report,
            )

        return {
            "status": (
                "normalized"
                if require_normalized
                else (
                    "normalization_required"
                    if owned_issues
                    else "current"
                )
            ),
            "target_spm": str(target),
            "producer_spm": str(producer),
            "live_audit_report": str(audit_report),
            "live_audit_payload": copy.deepcopy(
                raw_audit.get("payload") or {}
            ),
            "selected_contract": contract,
            "owned_work_items": copy.deepcopy(owned_issues),
        }

    def _cluster_relation_input_plan(
        self,
        relation_targets,
        stamp,
        producer_spm,
    ):
        """Partition one shared provider into runnable and excluded targets."""
        live_target_contracts = []
        runnable_relation_targets = []
        for target in relation_targets:
            try:
                live_resolution = (
                    self._cluster_normalization_stage_with_recovery(
                        target,
                        f"{stamp}_normalization_input",
                        producer_spm,
                        require_normalized=False,
                    )
                )
            except TargetPlannedExclusionError as exc:
                self._record_pipeline_planned_exclusion(target, exc)
                continue
            live_report = live_resolution.get("live_audit_report")
            contract = copy.deepcopy(
                live_resolution.get("selected_contract")
            )
            live_target_contracts.append({
                "target_spm": str(target),
                "report": str(live_report or ""),
                "policy": "normalization_stage_input",
                "contract": contract,
            })
            runnable_relation_targets.append(target)
        return runnable_relation_targets, live_target_contracts

    def _attempt_registered_relation_repair(
        self,
        exclusion,
        stamp,
        producer_spm,
        *,
        require_normalized,
        reaudit=None,
    ):
        """Run a relation gate's registered repair under its current lease.

        The relation audit and failed-retry button share the same plan builder
        and exact executor.  An unsupported reason remains a visible target
        exclusion; cancellation or lost queue ownership remains a lifecycle
        terminal and is never relabelled as damaged target data.
        """

        target = Path(exclusion.target_spm).resolve(strict=False)
        producer = Path(producer_spm).resolve(strict=False)
        durable_evidence = copy.deepcopy(exclusion.evidence)
        durable_evidence.pop("repair_attempt", None)
        repair_evidence = {
            "reason_token": str(exclusion.reason_token),
            "target_spm": str(target),
            "producer_spm": str(producer),
            "evidence": durable_evidence,
        }
        reason_codes = set(evidence_reason_codes(repair_evidence))
        if not reason_codes or not has_repair_contract_evidence(
            repair_evidence
        ):
            return None

        def attach_attempt(payload):
            attempt = copy.deepcopy(payload)
            exclusion.evidence["repair_attempt"] = attempt
            report = getattr(exclusion, "report", None)
            if isinstance(report, dict):
                report.setdefault("evidence", {})["repair_attempt"] = (
                    copy.deepcopy(attempt)
                )

        if self.stop_flag.is_set():
            raise BatchItemError(
                "Generator/Cluster 자동 복구가 사용자에 의해 취소되었습니다.",
                kind="cancelled",
                report={
                    "reason_token": "operator_cancelled",
                    "target_spm": str(target),
                    "producer_spm": str(producer),
                },
            )

        active_job = getattr(self, "active_batch_job", None)
        if not isinstance(active_job, dict) or not active_job.get("id"):
            attach_attempt({
                "status": "not_started",
                "reason_token": "initiating_job_context_missing",
                "reason_codes": sorted(reason_codes),
            })
            return None

        inventory_paths = []
        seen_inventory = set()

        def add_inventory(value):
            if not value:
                return
            path = Path(value).resolve(strict=False)
            key = os.path.normcase(os.path.abspath(str(path))).casefold()
            if key not in seen_inventory:
                seen_inventory.add(key)
                inventory_paths.append(path)

        add_inventory(target)
        add_inventory(producer)
        for source_name in ("_active_batch_inventory", "items"):
            source = getattr(self, source_name, None)
            if not isinstance(source, dict):
                continue
            for iid, item in source.items():
                add_inventory(
                    item.get("spm") if isinstance(item, dict) else iid
                )

        retry_metadata = copy.deepcopy(
            getattr(self, "_active_retry_metadata", {}) or {}
        )
        queue_identity = str(
            active_job.get("shared_queue_job_id")
            or active_job.get("id")
        )
        parent_retry_id = str(
            retry_metadata.get("progress_run_id")
            or active_job.get("shared_queue_job_id")
            or f"sk-batch-{active_job['id']}"
        )
        request_digest = hashlib.sha256(json.dumps(
            {
                "queue_identity": queue_identity,
                "target_spm": str(target),
                "producer_spm": str(producer),
                "reason_codes": sorted(reason_codes),
                "evidence": repair_evidence,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")).hexdigest()[:16]
        request_id = f"cluster-relation-{request_digest}"

        try:
            repair_plan = build_exact_target_repair_plan(
                target,
                repair_evidence,
                inventory_paths=inventory_paths,
                parent_retry_id=parent_retry_id,
                request_id=request_id,
            )
        except (FileNotFoundError, TypeError, ValueError) as exc:
            attach_attempt({
                "status": "not_started",
                "reason_token": "exact_target_plan_invalid",
                "reason_codes": sorted(reason_codes),
                "error": compact_error_message(exc, 320),
            })
            return None

        plan = repair_plan.metadata()
        if not repair_plan.supported:
            attach_attempt({
                "status": "unsupported",
                "reason_token": "registered_reason_has_no_exact_action",
                "reason_codes": list(plan.get("reason_codes") or ()),
                "reason_ko": repair_plan.friendly_reason,
                "action_ko": repair_plan.remaining_action,
                REPAIR_FAILURE_KEY: build_repair_failure(
                    request_id=request_id,
                    plan_metadata=plan,
                    failure_code="registered_reason_has_no_exact_action",
                ),
            })
            return None

        lease = getattr(self, "_active_shared_queue_lease", None)
        captured_shared_job_id = active_job.get("shared_queue_job_id")
        lease_job_id = getattr(lease, "job_id", None)
        if (
            lease is None
            or getattr(lease, "finished", False)
            or (
                captured_shared_job_id
                and lease_job_id != captured_shared_job_id
            )
        ):
            raise BatchItemError(
                "Generator/Cluster 자동 복구의 공용 대기열 소유권이 없습니다.",
                kind="owner_lost",
                report={
                    "reason_token": "shared_queue_lease_owner_lost",
                    "target_spm": str(target),
                    "producer_spm": str(producer),
                    "request_id": request_id,
                },
            )
        renew = getattr(lease, "renew_and_check_current", None)
        if renew is not None and not renew():
            raise BatchItemError(
                "Generator/Cluster 자동 복구의 공용 대기열 lease가 만료되었습니다.",
                kind="owner_lost",
                report={
                    "reason_token": "shared_queue_lease_owner_lost",
                    "target_spm": str(target),
                    "producer_spm": str(producer),
                    "request_id": request_id,
                },
            )

        def observe_fresh():
            # The live-audit gate re-resolves a receipt, not a normalization
            # observation.  Both admit repair through this one executor, so
            # the caller supplies the shape its own gate returns (#160).
            if reaudit is not None:
                return reaudit()
            return self._cluster_normalization_stage_observation(
                target,
                f"{stamp}_registered_repair_reaudit",
                producer,
                require_normalized=require_normalized,
            )

        def fresh_reaudit(attempted_stages):
            try:
                return observe_fresh()
            except TargetPlannedExclusionError as fresh_exclusion:
                fresh_attempt = {
                    "status": "repaired_but_reaudit_blocked",
                    "request_id": request_id,
                    "reason_codes": list(plan.get("reason_codes") or ()),
                    "attempted_stages": copy.deepcopy(attempted_stages),
                }
                fresh_exclusion.evidence["repair_attempt"] = fresh_attempt
                if isinstance(fresh_exclusion.report, dict):
                    fresh_exclusion.report.setdefault("evidence", {})[
                        "repair_attempt"
                    ] = copy.deepcopy(fresh_attempt)
                raise

        with _REGISTERED_RELATION_REPAIR_LOCK:
            memo = self.__dict__.setdefault(
                "_registered_relation_repair_results", {}
            )
            cached = copy.deepcopy(memo.get(request_id))
            if cached is not None:
                cached_status = str(cached.get("status") or "")
                if cached_status == "completed":
                    return fresh_reaudit(cached.get("attempted_stages") or ())
                if cached_status == "cancelled":
                    raise BatchItemError(
                        "Generator/Cluster 자동 복구가 사용자에 의해 취소되었습니다.",
                        kind="cancelled",
                        report=cached,
                    )
                if cached_status == "owner_lost":
                    raise BatchItemError(
                        "Generator/Cluster 자동 복구의 실행 소유권을 잃었습니다.",
                        kind="owner_lost",
                        report=cached,
                    )
                attach_attempt(cached)
                return None

            attempted = []
            planned_stages = list(plan.get("stages") or ())
            modeler_live_resolution = None
            self.ui_queue.put((
                "progress",
                f"{target.name} · Generator/Cluster exact 자동 복구 시작",
            ))
            self.log(
                "[자동 복구 시작] Generator/Cluster exact target · "
                f"{target.name} · provider={producer.name} · "
                f"reasons={','.join(sorted(reason_codes))}"
            )
            for stage_index, stage in enumerate(planned_stages, 1):
                if self.stop_flag.is_set():
                    cancelled = {
                        "status": "cancelled",
                        "reason_token": "operator_cancelled",
                        "request_id": request_id,
                        "attempted_stages": copy.deepcopy(attempted),
                    }
                    memo[request_id] = copy.deepcopy(cancelled)
                    raise BatchItemError(
                        "Generator/Cluster 자동 복구가 사용자에 의해 취소되었습니다.",
                        kind="cancelled",
                        report=cancelled,
                    )
                receipt = LOG_DIR / (
                    f"exact_repair_{request_id}_{stage_index}.json"
                )

                def on_progress(payload, asset=target.name):
                    self.ui_queue.put((
                        "progress",
                        f"{asset} · "
                        f"{payload.get('current_stage') or stage['stage']}",
                    ))

                terminal = self._execute_exact_repair_stage(
                    plan,
                    stage,
                    lease,
                    stage_index=stage_index,
                    receipt=receipt,
                    provenance_source="sk_batch.cluster_relation",
                    on_progress=on_progress,
                )
                terminal_status = str(
                    terminal.get("terminal_status")
                    or terminal.get("status")
                    or ""
                )
                terminal_result = terminal.get("result") or {}
                if (
                    len(planned_stages) == 1
                    and stage.get("repair_action")
                    == MODELER_NODE_TABLE_RECOVERY
                    and isinstance(
                        terminal_result.get("live_resolution"), dict
                    )
                ):
                    modeler_live_resolution = copy.deepcopy(
                        terminal_result["live_resolution"]
                    )
                attempted.append({
                    "stage": stage["stage"],
                    "tool": stage["tool"],
                    "repair_action": stage["repair_action"],
                    "targets": list(stage["target_spms"]),
                    "receipt": str(receipt),
                    "status": terminal_status,
                })
                if terminal_status == "cancelled":
                    cancelled = {
                        "status": "cancelled",
                        "reason_token": "operator_cancelled",
                        "request_id": request_id,
                        "attempted_stages": copy.deepcopy(attempted),
                    }
                    memo[request_id] = copy.deepcopy(cancelled)
                    raise BatchItemError(
                        "Generator/Cluster 자동 복구가 사용자에 의해 취소되었습니다.",
                        kind="cancelled",
                        report=cancelled,
                    )
                if terminal_status != "completed":
                    if terminal.get("failure_kind") == "owner_lost":
                        owner_lost = {
                            "reason_token": "shared_queue_lease_owner_lost",
                            "request_id": request_id,
                            "attempted_stages": copy.deepcopy(attempted),
                            "error": compact_error_message(
                                terminal.get("error")
                                or "exact repair owner lost",
                                320,
                            ),
                        }
                        memo[request_id] = {
                            "status": "owner_lost",
                            **copy.deepcopy(owner_lost),
                        }
                        raise BatchItemError(
                            "Generator/Cluster 자동 복구의 실행 소유권을 잃었습니다.",
                            kind="owner_lost",
                            report=owner_lost,
                        )
                    raw_error = compact_error_message(
                        terminal.get("error")
                        or (terminal.get("result") or {}).get("reason")
                        or "exact BAT repair failed",
                        320,
                    )
                    failed = {
                        "status": "failed",
                        "reason_token": "exact_relation_repair_failed",
                        "request_id": request_id,
                        "reason_codes": list(plan.get("reason_codes") or ()),
                        "attempted_stages": copy.deepcopy(attempted),
                        "error": raw_error,
                        # Survives the process; the memo above does not (#167).
                        REPAIR_FAILURE_KEY: build_repair_failure(
                            request_id=request_id,
                            plan_metadata=plan,
                            attempted_stages=attempted,
                            failed_stage=stage["stage"],
                            failure_code="exact_relation_repair_failed",
                            failure_report=str(receipt),
                            error=raw_error,
                        ),
                    }
                    memo[request_id] = copy.deepcopy(failed)
                    attach_attempt(failed)
                    return None

            completed = {
                "status": "completed",
                "request_id": request_id,
                "reason_codes": list(plan.get("reason_codes") or ()),
                "attempted_stages": copy.deepcopy(attempted),
            }
            memo[request_id] = copy.deepcopy(completed)
            self.ui_queue.put((
                "progress",
                f"{target.name} · exact 자동 복구 완료 · live 재검증 중",
            ))
            if modeler_live_resolution is not None:
                return modeler_live_resolution
            return fresh_reaudit(attempted)

    def _cluster_receipt_with_recovery(self, spm, stamp):
        """Resolve one owner receipt, repairing a registered target block.

        The direct Cluster Assembly run reached the live-audit gate with no
        repair frame at all: the row said 자동 복구 대상 and the target died
        (#160).  It now uses the same admission, executor and lease the
        normalization gate uses, and re-resolves the receipt afterwards.
        """
        try:
            return self._refresh_stale_cluster_receipt(spm, stamp)
        except TargetPlannedExclusionError as exc:
            if self.stop_flag.is_set():
                raise BatchItemError(
                    "Cluster Assembly 검사가 사용자에 의해 취소되었습니다.",
                    kind="cancelled",
                    report={
                        "reason_token": "operator_cancelled",
                        "target_spm": str(spm),
                    },
                )
            recovered = self._attempt_registered_relation_repair(
                exc,
                stamp,
                exc.producer_spm,
                require_normalized=True,
                reaudit=lambda: self._refresh_stale_cluster_receipt(
                    spm,
                    f"{stamp}_after_registered_repair",
                ),
            )
            if recovered is not None:
                return recovered
            if self.stop_flag.is_set():
                raise BatchItemError(
                    "Cluster Assembly 자동 복구가 사용자에 의해 취소되었습니다.",
                    kind="cancelled",
                    report={
                        "reason_token": "operator_cancelled",
                        "target_spm": str(spm),
                    },
                )
            raise

    def _cluster_normalization_stage_with_recovery(
        self,
        target_spm,
        stamp,
        producer_spm,
        *,
        require_normalized,
    ):
        """Observe one stage and recover a safely sealed stale exclusion."""
        try:
            return self._cluster_normalization_stage_observation(
                target_spm,
                stamp,
                producer_spm,
                require_normalized=require_normalized,
            )
        except TargetPlannedExclusionError as exc:
            if self.stop_flag.is_set():
                raise BatchItemError(
                    "Generator/Cluster 검사가 사용자에 의해 취소되었습니다.",
                    kind="cancelled",
                    report={
                        "reason_token": "operator_cancelled",
                        "target_spm": str(target_spm),
                        "producer_spm": str(producer_spm),
                    },
                )
            recovered = self._attempt_registered_relation_repair(
                exc,
                stamp,
                producer_spm,
                require_normalized=require_normalized,
            )
            if recovered is not None:
                return recovered
            if self.stop_flag.is_set():
                raise BatchItemError(
                    "Generator/Cluster 자동 복구가 사용자에 의해 취소되었습니다.",
                    kind="cancelled",
                    report={
                        "reason_token": "operator_cancelled",
                        "target_spm": str(target_spm),
                        "producer_spm": str(producer_spm),
                    },
                )
            raise

    def _attempt_stale_node_table_recovery(
        self,
        exclusion,
        stamp,
        producer_spm,
        *,
        require_normalized,
    ):
        """Run one sealed semantic-Save recovery inside the active job lease."""
        scope = (
            exclusion.evidence.get("stale_node_table_recovery")
            if isinstance(exclusion.evidence, dict)
            else None
        )
        if not isinstance(scope, dict) or scope.get("available") is not True:
            return None
        self._ensure_batch_queue_state()
        job = getattr(self, "active_batch_job", None)
        if not isinstance(job, dict) or not job.get("id"):
            exclusion.evidence["recovery_attempt"] = {
                "status": "not_started",
                "reason_token": "initiating_job_context_missing",
            }
            return None
        if self.stop_flag.is_set():
            exclusion.evidence["recovery_attempt"] = {
                "status": "not_started",
                "reason_token": "initiating_job_cancelled",
            }
            raise BatchItemError(
                "SpeedTree Node table 자동 복구가 사용자에 의해 취소되었습니다.",
                kind="cancelled",
                report=copy.deepcopy(exclusion.evidence["recovery_attempt"]),
            )

        from pcg_st9_texture_batch.stale_node_table_recovery import (
            StaleNodeTableRecoveryError,
            recover_stale_node_table,
        )
        from pcg_st9_texture_batch.speedtree_modeler_uia import (
            SpeedTreeModelerRecoverySession,
        )

        target = Path(scope["target_spm"]).resolve(strict=False)
        captured_job_id = job["id"]
        captured_generation = (
            job.get("shared_queue_sequence") or captured_job_id
        )
        captured_shared_job_id = job.get("shared_queue_job_id")

        def is_job_current():
            current = getattr(self, "active_batch_job", None)
            return (
                isinstance(current, dict)
                and current.get("id") == captured_job_id
                and (
                    current.get("shared_queue_sequence") or current.get("id")
                )
                == captured_generation
            )

        def is_queue_current():
            if not captured_shared_job_id:
                return True
            lease = getattr(self, "_active_shared_queue_lease", None)
            return bool(
                lease is not None
                and lease.job_id == captured_shared_job_id
                and not lease.finished
                and lease.renew_and_check_current()
            )

        guards = {
            "is_cancelled": self.stop_flag.is_set,
            "is_app_open": lambda: bool(
                getattr(self, "_app_open", True)
            ),
            "is_job_current": is_job_current,
            "is_queue_current": is_queue_current,
        }

        def retry_stage(continuation):
            self.ui_queue.put((
                "progress",
                "Modeler Save verified; identity-bound live re-audit running — "
                + target.name,
            ))
            return self._cluster_normalization_stage_observation(
                target,
                f"{stamp}_modeler_recovery_reaudit",
                producer_spm,
                require_normalized=require_normalized,
            )

        def mark_resume_committed(claim_payload):
            self._recovery_resume_commit = {
                "job_id": captured_job_id,
                "job_generation": captured_generation,
                "verified_after_raw_sha256": claim_payload[
                    "verified_after_raw_sha256"
                ],
            }
            self.ui_queue.put((
                "progress",
                "Modeler Save verified; original-stage resume committed — "
                + target.name,
            ))

        self.ui_queue.put((
            "progress",
            "Sealing Modeler recovery preimage — " + target.name,
        ))
        self.log(
            "Stale Node-table recovery scope sealed from live audit: "
            f"{target} | Authoring Mesh IDs "
            + ",".join(str(value) for value in scope["authoring_mesh_ids"])
            + " | required-live Mesh IDs "
            + ",".join(
                str(value) for value in scope["required_live_mesh_ids"]
            )
            + f" | scope={scope['scope_sha256']}"
        )
        executable = self.cfg.get("speedtree_exe") or ""
        modeler_session = self._stale_node_table_modeler_session
        if (
            modeler_session is None
            or not modeler_session.is_compatible(executable)
        ):
            modeler_session = SpeedTreeModelerRecoverySession(executable)
            self._stale_node_table_modeler_session = modeler_session
        self.ui_queue.put((
            "modeler_recovery",
            {
                "target_spm": str(target),
                "scope_sha256": scope["scope_sha256"],
                "authoring_mesh_ids": list(scope["authoring_mesh_ids"]),
                "required_live_mesh_ids": list(
                    scope["required_live_mesh_ids"]
                ),
            },
        ))
        try:
            result = recover_stale_node_table(
                target,
                executable,
                authoring_mesh_ids=scope["authoring_mesh_ids"],
                required_live_mesh_ids=scope["required_live_mesh_ids"],
                timeout=7200,
                poll_interval=2.0,
                stable_reads=3,
                retry=retry_stage,
                job_id=str(captured_job_id),
                job_generation=str(captured_generation),
                guards=guards,
                expected_preimage_raw_sha256=scope[
                    "target_preimage_raw_sha256"
                ],
                modeler_session=modeler_session,
                continuation_commit_lock=self._recovery_commit_lock,
                on_continuation_claimed=mark_resume_committed,
            )
        except StaleNodeTableRecoveryError as exc:
            if exc.reason_token in {
                "initiating_job_cancelled",
                "initiating_app_closed",
            }:
                raise BatchItemError(
                    "SpeedTree Node table 자동 복구가 취소되었습니다.",
                    kind="cancelled",
                    report={
                        "reason_token": exc.reason_token,
                        "evidence": copy.deepcopy(exc.evidence),
                    },
                ) from exc
            if exc.reason_token in {
                "initiating_job_generation_stale",
                "initiating_queue_lease_lost",
            }:
                raise BatchItemError(
                    "SpeedTree Node table 자동 복구의 실행 소유권을 잃었습니다.",
                    kind="owner_lost",
                    report={
                        "reason_token": exc.reason_token,
                        "evidence": copy.deepcopy(exc.evidence),
                    },
                ) from exc
            continuation_failed = exc.reason_token in {
                "continuation_callback_failed",
                "continuation_claim_publish_failed",
            }
            exclusion.evidence["recovery_attempt"] = {
                "status": (
                    "repaired_but_continuation_failed_replan_required"
                    if continuation_failed
                    else "blocked"
                ),
                "reason_token": exc.reason_token,
                "blocked_event": exc.evidence.get("blocked_event"),
                "blocked_event_sha256": exc.evidence.get(
                    "blocked_event_sha256"
                ),
            }
            if continuation_failed:
                self.log(
                    "The SPM passed the sealed recovery gate, but the original "
                    "stage continuation failed after its once-only claim: "
                    f"{target.name} | reason={exc.reason_token}. The claim "
                    "will not be replayed; start a fresh live-audit job."
                )
            else:
                self.log(
                    "Stale Node-table recovery stopped fail-closed: "
                    f"{target.name} | reason={exc.reason_token}. "
                    "No unrelated Modeler process was touched. Start a fresh "
                    "live audit before retry."
                )
            return None
        except OSError as exc:
            exclusion.evidence["recovery_attempt"] = {
                "status": "blocked",
                "reason_token": "modeler_launch_or_recovery_io_failed",
            }
            self.log(
                "Stale Node-table recovery I/O failed closed: "
                f"{target.name} | {compact_error_message(exc)}. "
                "No unrelated Modeler process was touched."
            )
            return None

        if result.get("status") == "already_repaired":
            exclusion.evidence["recovery_attempt"] = {
                "status": "replan_required",
                "reason_token": "source_already_repaired_after_blocking_audit",
                "after_raw_sha256": result.get("after_raw_sha256"),
            }
            self.log(
                "The SPM changed after the blocking audit and is already "
                f"non-stale: {target.name}. Run a fresh job plan; the old "
                "checkpoint was not resumed."
            )
            return None
        live_resolution = result.get("retry_result")
        if not isinstance(live_resolution, dict):
            exclusion.evidence["recovery_attempt"] = {
                "status": "blocked",
                "reason_token": "recovery_continuation_result_missing",
            }
            return None
        self.log(
            "Stale Node-table recovery verified and original stage resumed "
            f"once: {target.name} | after={result.get('after_raw_sha256')} | "
            "semantic exact-document Close verified; owned session retained"
        )
        return live_resolution

    def _job_blender(self, iid, spm, item):
        from spm_audit import audit_spm, sk_readiness

        spm = self._prepare_pair_for_job(spm)
        cluster_source = is_cluster_source_spm(spm)
        self._refresh_canonical_atlas_manifests(spm)
        producer_spm = speedtree_output_spm_for(spm)
        speedtree_spm = producer_spm
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        bark_source_resolution = None
        relation_targets = ()
        if cluster_source:
            relation_targets = cluster_relation_output_targets(
                spm,
                item.get("referenced_by_spms") or (),
            )
            if relation_targets:
                from cluster_bark_source_resolution import (
                    ClusterBarkSourceResolutionError,
                    resolve_cluster_bark_source_spm,
                )

                try:
                    (
                        relation_targets,
                        live_target_contracts,
                    ) = self._cluster_relation_input_plan(
                        relation_targets,
                        stamp,
                        producer_spm,
                    )
                    if not relation_targets:
                        reason = (
                            "all registered consumers are target-local "
                            "planned exclusions"
                        )
                        self._record_phase_status(
                            iid,
                            "blend_status",
                            "Sync skipped: no runnable consumers",
                            "skipped",
                            reason,
                            details={
                                "reason_token": (
                                    "all_consumers_planned_excluded"
                                ),
                                "push_readiness": False,
                            },
                            persist=True,
                        )
                        self.log(
                            f"Cluster relation Sync skipped: {spm.name}: "
                            + reason
                        )
                        return
                    bark_source_resolution = (
                        resolve_cluster_bark_source_spm(
                            speedtree_spm,
                            relation_targets,
                            live_target_contracts=live_target_contracts,
                        )
                    )
                except (
                    ClusterBarkSourceResolutionError,
                    OSError,
                    TypeError,
                    ValueError,
                ) as exc:
                    report_file = (
                        LOG_DIR
                        / (
                            f"{spm.stem}_cluster_bark_resolution_"
                            f"{stamp}.json"
                        )
                    )
                    if isinstance(exc, ClusterBarkSourceResolutionError):
                        classification = "asset_dependency_error"
                        failure_kind = "data_error"
                    elif (
                        isinstance(exc, PermissionError)
                        or getattr(exc, "winerror", None) == 5
                    ):
                        classification = "process_cache_io_error"
                        failure_kind = "internal_error"
                    else:
                        classification = "process_resolution_error"
                        failure_kind = "internal_error"
                    failure_report = {
                        "status": "failed",
                        "stage": "cluster_canonical_bark_isolation",
                        "classification": classification,
                        "failure_kind": failure_kind,
                        "error": str(exc),
                        "exception_type": type(exc).__name__,
                        "errno": getattr(exc, "errno", None),
                        "winerror": getattr(exc, "winerror", None),
                        "source_spm": str(spm),
                        "speedtree_spm": str(speedtree_spm),
                        "relation_targets": [
                            str(target) for target in relation_targets
                        ],
                        "traceback": traceback.format_exc(),
                        "generated_at": datetime.now().isoformat(
                            timespec="seconds"
                        ),
                    }
                    try:
                        atomic_write_json(report_file, failure_report)
                    except (OSError, TypeError, ValueError):
                        report_file = None
                    raise BatchItemError(
                        "Cluster canonical bark isolated source failed: "
                        + str(exc),
                        kind=failure_kind,
                        report=failure_report,
                        report_file=report_file,
                    ) from exc
                speedtree_spm = Path(
                    bark_source_resolution["speedtree_spm"]
                )
                if bark_source_resolution["status"] in {
                    "prepared",
                    "cached",
                }:
                    self.log(
                        "Cluster canonical bark 격리 소스 "
                        f"{bark_source_resolution['status']}: "
                        f"{spm.name} → {speedtree_spm}"
                    )
        blend = blend_path_for(spm)
        leaf_ok, leaf_reason = self._leaf_reference_ready(speedtree_spm)
        if not leaf_ok:
            status = self._record_live_blend_status(iid, spm)
            contract = inspect_spm_leaf_contract(speedtree_spm)
            self.log(f"  [② 조기 차단] {spm.name}: {leaf_reason}")
            raise BatchItemError(
                leaf_reason,
                kind="data_error",
                report={
                    "status": "blocked",
                    "error": leaf_reason,
                    "blend_status": status,
                    "leaf_reference_contract": contract,
                },
            )
        mesh_block = material_preflight_mesh_reference_block(speedtree_spm)
        if mesh_block is not None:
            self.log(
                f"  [② 조기 차단] {spm.name}: {mesh_block['error']}"
            )
            raise BatchItemError(
                mesh_block["error"],
                kind="data_error",
                report=mesh_block,
            )
        cluster_receipt_resolution = None
        cluster_receipt_resolved = False

        def resolve_cluster_receipt_once():
            nonlocal cluster_receipt_resolution, cluster_receipt_resolved
            if not cluster_receipt_resolved:
                cluster_receipt_resolution = (
                    self._cluster_receipt_with_recovery(speedtree_spm, stamp)
                )
                cluster_receipt_resolved = True
            return cluster_receipt_resolution

        if not cluster_source:
            # A current blend is not enough for an owner Tree.  Resolve the
            # live Cluster relationship before authorizing an early Repair skip.
            resolve_cluster_receipt_once()
        if not self.force_rerun:
            live_contract = cluster_receipt_resolution_uses_live_audit(
                cluster_receipt_resolution
            )
            saved_pipeline = {}
            repair_state = self._repair_output_state(
                spm,
                pipeline_projection_out=(
                    saved_pipeline if live_contract else None
                ),
            )
            if (
                cluster_source
                and cluster_bark_resolution_requires_repair(
                    spm,
                    bark_source_resolution,
                )
            ):
                repair_state = {
                    "current": False,
                    "push_ready": False,
                    "kind": "cluster_bark_capture",
                    "reason": (
                        "prepared isolated canonical bark source has not "
                        "been captured by Blender Repair"
                    ),
                }
                self.log(
                    "Cluster canonical bark capture 필요: "
                    f"{spm.name} · 기존 blend 최신 판정 무효화"
                )
            if repair_state["current"]:
                if live_contract:
                    # A clean live contract with no persisted receipt may skip
                    # only when a prior Repair actually captured an Assembly
                    # manifest.  Otherwise run BWR once and embed the live
                    # contract instead of treating the receipt warning as an
                    # error or silently degrading to non-Assembly.
                    saved_manifest = saved_pipeline.get(
                        "cluster_assembly_manifest"
                    )
                    if not isinstance(saved_manifest, dict):
                        repair_state["current"] = False
                    else:
                        try:
                            live_contract = copy.deepcopy(
                                cluster_receipt_resolution.get(
                                    "selected_contract"
                                )
                            )
                            if not isinstance(live_contract, dict):
                                raise ValueError(
                                    "selected live contract is unavailable"
                                )
                            live_status = str(
                                (
                                    live_contract.get("handoff") or {}
                                ).get("status")
                                or ""
                            )
                            saved_status = str(
                                saved_manifest.get("status") or ""
                            )
                            if isinstance(
                                saved_manifest.get(
                                    "pass_through_provenance"
                                ),
                                dict,
                            ):
                                from cluster_assembly_builder import (
                                    validate_receipt_pass_through_manifest,
                                )

                                validate_receipt_pass_through_manifest(
                                    saved_manifest,
                                    receipt_path=cluster_receipt_resolution[
                                        "live_audit_report"
                                    ],
                                    target_contract=live_contract,
                                    target_spm=spm,
                                )
                            status_mismatch = (
                                live_status == "pass_through"
                                and saved_status != "pass_through"
                            ) or (
                                live_status != "pass_through"
                                and saved_status == "pass_through"
                            )
                            if (
                                status_mismatch
                                and not rendered_unused_pass_through_matches_live(
                                    saved_manifest,
                                    live_contract,
                                )
                            ):
                                repair_state["current"] = False
                        except (
                            OSError,
                            RuntimeError,
                            TypeError,
                            ValueError,
                        ):
                            # A live-audit result that cannot be reconciled
                            # with the saved manifest must never authorize an
                            # early Repair skip.
                            repair_state["current"] = False
                if repair_state["current"]:
                    if cluster_source:
                        relation_refresh = (
                            self._refresh_cluster_source_relations(spm, item)
                        )
                        for target in relation_targets:
                            self._cluster_normalization_stage_with_recovery(
                                target,
                                f"{stamp}_normalization_output",
                                producer_spm,
                                require_normalized=True,
                            )
                        relation_outputs_changed = bool(
                            isinstance(relation_refresh, dict)
                            and not relation_refresh.get("no_change")
                            and relation_refresh.get("status")
                            != "pass_through"
                        )
                        if relation_outputs_changed:
                            repair_state = self._repair_output_state(spm)
                    stage_evidence = None
                    if (
                        repair_state["current"]
                        and repair_state["push_ready"]
                    ):
                        try:
                            stage_evidence = (
                                self._repair_stage_evidence_if_active(
                                    spm,
                                    repair_state.get(
                                        "push_dependency_contract"
                                    ),
                                )
                            )
                        except (
                            OSError,
                            TypeError,
                            ValueError,
                            RepairPushEvidenceError,
                        ) as exc:
                            repair_state["current"] = False
                            self.log(
                                "Current Repair output has no v2 stage "
                                "evidence; rebuilding instead of falling "
                                f"through to standalone Push: {spm.name} · "
                                f"{compact_error_message(exc)}"
                            )
                    if repair_state["current"]:
                        self._record_live_blend_status(
                            iid,
                            spm,
                            repair_state=repair_state,
                        )
                        suffix = (
                            " · Unreal Push 차단 상태 유지"
                            if not repair_state["push_ready"]
                            else ""
                        )
                        self._publish_repair_stage_contract(
                            spm,
                            ready=repair_state["push_ready"],
                            reason=repair_state["reason"],
                            kind=repair_state.get("kind"),
                            push_dependency_contract=repair_state.get(
                                "push_dependency_contract"
                            ),
                            evidence_bundle=stage_evidence,
                        )
                        self.log(f"건너뜀 (blend 최신{suffix}): {spm.name}")
                        return
                    self.log(
                        "Cluster 관계 산출물 갱신 후 Repair 상태가 변경되어 "
                        f"②를 계속 실행: {spm.name}"
                    )
        open_windows = blender_open_file_window_titles(blend)
        if open_windows:
            raise BatchItemError(
                "Repair 대상 .blend가 대화형 Blender에 열려 있습니다. "
                "저장하거나 닫은 뒤 다시 실행하세요: " + blend.name,
                kind="manual_required",
                report={
                    "status": "blocked",
                    "stage": "interactive_blender_guard",
                    "blend": str(blend),
                    "open_windows": open_windows,
                },
            )
        if should_calibrate_spm(item):
            readiness = sk_readiness(
                audit_spm(
                    spm,
                    analyze_bone_graph=not item.get(
                        "manual_bones_locked", False
                    ),
                )
            )
            if not readiness["ready"]:
                raise RuntimeError(f"SK 미제작: {readiness['error']}")
        entry = self.state.setdefault(iid, {})
        self.log(f"재질 사전검사 시작: {spm.name} (Blender 실행 전)")
        self.ui_queue.put((
            "cell",
            (iid, "blend_status", "재질 사전검사 중..."),
        ))
        artifact = self._execute_material_preflight(
            spm,
            speedtree_spm,
            stamp,
        )
        material_report = artifact["report"]
        material_log = artifact["log"]
        material_result = artifact["result"]
        if (
            artifact["code"] != 0
            or material_result.get("status") != "ok"
        ):
            reason = summarize_job_failure(
                material_result, material_log
            )
            self.log(
                f"  [Blender 실행 전 차단] {spm.name}: {reason} — "
                f"상세 보고서: {material_report}"
            )
            raise BatchItemError(
                reason,
                kind="data_error",
                report=material_result,
                log_file=material_log,
                report_file=material_report,
            )
        # Cluster sources resolve here; owner Trees reuse the result that
        # was already required for the Repair-current decision.  The local
        # resolver guarantees one runtime live-audit resolution per item.
        cluster_receipt_resolution = resolve_cluster_receipt_once()
        if cluster_receipt_resolution_uses_live_audit(
            cluster_receipt_resolution
        ):
            live_report = Path(
                cluster_receipt_resolution["live_audit_report"]
            )
            try:
                live_payload = copy.deepcopy(
                    cluster_receipt_resolution.get("live_audit_payload")
                )
                selected_contract = copy.deepcopy(
                    cluster_receipt_resolution.get("selected_contract")
                )
                if (
                    not isinstance(live_payload, dict)
                    or not isinstance(selected_contract, dict)
                ):
                    raise ValueError(
                        "immutable live audit payload/contract is unavailable"
                    )
                material_result["cluster_assembly"] = selected_contract
                material_result[
                    "cluster_assembly_receipt_persistence"
                ] = live_payload.get(
                    "cluster_assembly_receipt_persistence"
                ) or {
                    "status": "warning",
                    "error": cluster_receipt_resolution.get(
                        "receipt_persistence_warning",
                        "",
                    ),
                }
                atomic_write_json(material_report, material_result)
            except (OSError, TypeError, ValueError) as exc:
                raise BatchItemError(
                    "Cluster Assembly live audit contract could not be "
                    f"embedded for Blender Repair: {spm.name}: {exc}",
                    kind="internal_error",
                    report_file=live_report,
                ) from exc
        self.log(f"재질 사전검사 통과: {spm.name}")
        self.log(f"Blender repair 시작: {spm.name} (수분 소요될 수 있음)")
        self.ui_queue.put(("cell", (iid, "blend_status", "Blender repair 중...")))
        wind = item["wind_override"]
        if wind == "auto":
            wind = wind_preset_for_spm(spm)
        job_report = LOG_DIR / f"{spm.stem}_bwr_{stamp}.json"
        pipeline_report = (
            spm.parent / "reports" /
            f"{spm.stem}_speedtree_repair_pipeline_report_codex.json"
        )
        try:
            previous_pipeline_report = pipeline_report.read_bytes()
        except OSError:
            previous_pipeline_report = None
        runtime_receipt = self._repair_runtime_receipt_path(spm)
        try:
            previous_runtime_receipt = runtime_receipt.read_bytes()
        except OSError:
            previous_runtime_receipt = None
        cluster_blend_backup = None
        cluster_blend_existed = cluster_source and blend.is_file()
        cluster_source_build_committed = False
        if cluster_blend_existed:
            cluster_blend_backup = (
                LOG_DIR / f"{spm.stem}_pre_repair_{stamp}.blend"
            )
            shutil.copy2(blend, cluster_blend_backup)

        def restore_cluster_repair_outputs():
            # This backup belongs only to the raw BWR producer transaction.
            # Once BWR has committed a ready Cluster source, the downstream
            # Normalizer/Atlas transaction owns its own snapshots.  Restoring
            # this older backup after that boundary would discard the valid
            # source blend/report and make the next run rebuild BWR again.
            if cluster_source_build_committed:
                return []
            restored = []
            if cluster_blend_backup and cluster_blend_backup.is_file():
                shutil.copy2(cluster_blend_backup, blend)
                restored.append(str(blend))
            elif cluster_source and not cluster_blend_existed and blend.exists():
                blend.unlink()
                restored.append(str(blend))
            if previous_pipeline_report is not None:
                atomic_write_bytes(
                    pipeline_report, previous_pipeline_report
                )
                restored.append(str(pipeline_report))
            elif cluster_source and pipeline_report.exists():
                pipeline_report.unlink()
                restored.append(str(pipeline_report))
            if previous_runtime_receipt is not None:
                atomic_write_bytes(
                    runtime_receipt,
                    previous_runtime_receipt,
                )
                restored.append(str(runtime_receipt))
            elif cluster_source and runtime_receipt.exists():
                runtime_receipt.unlink()
                restored.append(str(runtime_receipt))
            return restored

        cmd = [
            self.cfg["blender_exe"], "--factory-startup", "-b",
            "--python", str(TOOL_DIR / "jobs" / "bwr_headless_job.py"), "--",
            "--spm", str(spm),
            "--speedtree-spm", str(speedtree_spm),
            "--blend", str(blend),
            "--wind", wind,
            "--material-contract", str(material_report),
            "--report", str(job_report),
        ]
        if item.get("manual_bones_locked", False):
            cmd.insert(
                cmd.index("--material-contract"),
                "--manual-bones-locked",
            )
        if (
            bark_source_resolution
            and bark_source_resolution.get("manifest")
        ):
            cmd.extend([
                "--bark-normalization-manifest",
                str(bark_source_resolution["manifest"]),
            ])
        if cluster_source and relation_targets:
            cmd.insert(
                cmd.index("--material-contract"),
                "--cluster-source-build-only",
            )
        parallel = self.cfg.get("blender_parallel_jobs", 2) > 1
        code, log_file = self._run_limited(
            cmd,
            f"{spm.stem}_bwr_{stamp}.log",
            self.cfg.get("blender_job_timeout", 3600),
            affinity=not parallel,
        )
        result = load_job_report(job_report)
        if code != 0 or result.get("status") != "ok":
            if cluster_source:
                try:
                    restore_cluster_repair_outputs()
                except OSError as exc:
                    self.log(
                        f"  [Repair rollback warning] {spm.name}: {exc}"
                    )
            if previous_pipeline_report is not None:
                try:
                    atomic_write_bytes(pipeline_report, previous_pipeline_report)
                    self.log(
                        f"  [② 복구] 실패 전 최신성 보고서 보존: {spm.name}"
                    )
                except OSError as exc:
                    self.log(
                        f"  [② 복구 경고] 이전 보고서 복원 실패: "
                        f"{spm.name}: {exc}"
                    )
            reason = summarize_job_failure(result, log_file)
            self.log(f"  [Blender 실패 원인] {reason} — 상세 로그: {log_file}")
            raise BatchItemError(
                reason,
                kind="data_error",
                report=result,
                log_file=log_file,
                report_file=job_report,
            )
        if cluster_source and relation_targets:
            source_build = (
                result.get("cluster_source_build_contract") or {}
            )
            if source_build.get("status") != "ready":
                try:
                    restore_cluster_repair_outputs()
                except OSError:
                    pass
                raise BatchItemError(
                    "Cluster raw source build did not produce a ready "
                    "Normalizer contract",
                    kind="data_error",
                    report=result,
                    log_file=log_file,
                    report_file=job_report,
                )
            # BWR's source blend and producer report are now durable inputs to
            # the separate Normalizer/Atlas transaction.  That transaction
            # still rolls back SPM/Atlas partial outputs (and restores this
            # exact committed source snapshot), but must never cross back into
            # the pre-BWR backup.
            cluster_source_build_committed = True
            if cluster_blend_backup is not None:
                try:
                    cluster_blend_backup.unlink(missing_ok=True)
                    cluster_blend_backup = None
                except OSError as exc:
                    self.log(
                        f"  [backup cleanup warning] {spm.name}: {exc}"
                    )
            from cluster_blend_sync import (
                run_cluster_relation_transaction,
            )

            if not relation_targets:
                result["cluster_relation_sync"] = {
                    "status": "pass_through",
                    "reason": "no_explicit_owner_relation",
                    "targets": [],
                }
            else:
                try:
                    with cluster_relation_owner_lock(spm):
                        relation_result = run_cluster_relation_transaction(
                            blend,
                            relation_targets,
                            enabled=True,
                            blender_exe=Path(self.cfg["blender_exe"]),
                            unit_probe_path=Path(
                                self.cfg["cluster_unit_probe"]
                            ),
                            capture_resolution=int(
                                self.cfg.get(
                                    "cluster_capture_resolution", 1024
                                )
                            ),
                            repair_runtime_config=self.cfg,
                            timeout=int(
                                self.cfg.get("blender_job_timeout", 3600)
                            ),
                        )
                    result["cluster_relation_sync"] = relation_result
                    final_pipeline = load_job_report(pipeline_report)
                    result["handoff_preflight"] = (
                        final_pipeline.get("handoff_preflight") or {}
                    )
                    result["source_review_required"] = bool(
                        final_pipeline.get("source_review_required")
                    )
                    result["cluster_normalization_verification"] = [
                        self._cluster_normalization_stage_with_recovery(
                            target,
                            f"{stamp}_normalization_output",
                            producer_spm,
                            require_normalized=True,
                        )
                        for target in relation_targets
                    ]
                except Exception as exc:
                    try:
                        restored = restore_cluster_repair_outputs()
                    except OSError as restore_exc:
                        restored = [f"rollback_failed:{restore_exc}"]
                    raise BatchItemError(
                        "Cluster Normalizer/Atlas sync failed: "
                        + str(exc)
                        + (
                            " | restored: " + ", ".join(restored)
                            if restored
                            else ""
                        ),
                        kind="data_error",
                        report=result,
                        log_file=log_file,
                        report_file=job_report,
                    ) from exc
        elif cluster_source:
            result["cluster_relation_sync"] = {
                "status": "pass_through",
                "reason": "no_explicit_owner_relation",
                "targets": [],
                "repair_mode": "standalone_final_handoff",
            }
        if cluster_source:
            try:
                atomic_write_json(job_report, result)
            except (OSError, TypeError, ValueError) as exc:
                self.log(
                    "  [report warning] Cluster relation result could not "
                    f"be persisted for {spm.name}: {exc}"
                )
        # Written before the handoff check reads it: this run verified the saved
        # outputs against the code now installed, whether or not it had to
        # rewrite the .blend.
        self._write_repair_runtime_receipt(spm)
        handoff_state = {}
        handoff_ok, handoff_reason = self._handoff_ready(
            spm,
            state_out=handoff_state,
        )
        blend_status = (
            self._blend_status_from_repair_state(handoff_state)
            if handoff_state
            else self._blend_status_text(spm)
        )
        self.ui_queue.put(("cell", (iid, "blend_status", blend_status)))
        with self.state_lock:
            entry["blend_status"] = blend_status
            save_state(self.state)
        source_review = bool(
            result.get("source_review_required")
            or (result.get("handoff_preflight") or {}).get("status")
            == "source_review"
        )
        repair_contract_reason = (
            "원본/재질 검토 필요 — Unreal Push 차단"
            if source_review
            else handoff_reason
        )
        self._publish_repair_stage_contract(
            spm,
            ready=handoff_ok and not source_review,
            reason=repair_contract_reason,
            kind=(
                "source_review"
                if source_review
                else handoff_state.get("kind")
            ),
            push_dependency_contract=handoff_state.get(
                "push_dependency_contract"
            ),
            evidence_bundle=(
                self._repair_stage_evidence_if_active(
                    spm,
                    handoff_state.get("push_dependency_contract"),
                )
                if handoff_ok and not source_review
                else None
            ),
        )
        if not handoff_ok and not source_review:
            if cluster_source:
                try:
                    restore_cluster_repair_outputs()
                except OSError as exc:
                    self.log(
                        f"  [Repair rollback warning] {spm.name}: {exc}"
                    )
            raise BatchItemError(
                f"② 완료 후 사전검사 실패: {handoff_reason}",
                kind="data_error",
                report=result,
                log_file=log_file,
                report_file=job_report,
            )
        if source_review:
            push_status = "차단 · 원본/재질 검토 필요"
            self.ui_queue.put(("cell", (iid, "push_status", push_status)))
            with self.state_lock:
                entry["push_status"] = push_status
                entry["push_status_kind"] = "source_review"
                save_state(self.state)
            self.log(
                f"② 완료 · source review 필요 · Unreal Push 차단: "
                f"{spm.name} ({handoff_reason})"
            )
        else:
            push_status = "준비됨 ✓"
            self.ui_queue.put(("cell", (iid, "push_status", push_status)))
            with self.state_lock:
                entry["push_status"] = push_status
                entry["push_status_kind"] = "ready"
                entry.pop("push_status_error", None)
                save_state(self.state)
        if cluster_blend_backup is not None:
            try:
                cluster_blend_backup.unlink(missing_ok=True)
            except OSError as exc:
                self.log(
                    f"  [backup cleanup warning] {spm.name}: {exc}"
                )
        for warning in result.get("warnings", []):
            self.log(f"  [② 경고] {spm.name}: {warning}")
        self.log(f"repair 완료: {blend.name}")

    def _push_preflight(self, targets):
        """Return (ready items, fatal Unreal reason) after recording all skips."""
        self.log("push 준비 검사 중...")
        transport = getattr(
            self,
            "active_push_transport",
            self.cfg.get("push_transport", "rpc"),
        )
        if transport == "rpc" and not self._unreal_running():
            reason = "Unreal Editor 종료 — MyProject2가 실행 중이 아님"
            self.log(f"[중단] {reason}")
            for item in targets:
                iid = str(item["spm"])
                self._record_phase_status(
                    iid,
                    "push_status",
                    f"중단: {reason}",
                    "unreal_unavailable",
                    reason,
                    persist=False,
                )
            with self.state_lock:
                save_state(self.state)
            return [], reason
        if transport == "headless" and self._unreal_running():
            reason = "headless Push는 자산 잠금 충돌 방지를 위해 Unreal Editor를 닫아야 함"
            self.log(f"[중단] {reason}")
            for item in targets:
                self._record_phase_status(
                    str(item["spm"]),
                    "push_status",
                    f"중단: {reason}",
                    "unreal_unavailable",
                    reason,
                    persist=False,
                )
            with self.state_lock:
                save_state(self.state)
            return [], reason
        ready = []
        reused_repair_contracts = 0
        for item in targets:
            spm = item["spm"]
            dirty_windows = [
                title
                for title in blender_open_file_window_titles(
                    blend_path_for(spm)
                )
                if title.lstrip().startswith("*")
            ]
            if dirty_windows:
                why = (
                    "Blender에 저장되지 않은 변경이 있어 저장본 Export를 "
                    "차단함 — 저장하거나 닫은 뒤 다시 실행"
                )
                self._record_phase_status(
                    str(spm),
                    "push_status",
                    f"건너뜀: {why}",
                    "manual_required",
                    why,
                    persist=False,
                )
                self.log(f"[준비 제외] {spm.name}: {why}")
                continue
            repair_contract = self._repair_stage_contract(spm)
            contract_failure_kind = None
            if repair_contract is None:
                ok, why = self._handoff_ready(spm)
            else:
                try:
                    self._validate_repair_stage_contract(
                        spm,
                        repair_contract,
                    )
                except RepairPushEvidenceError as exc:
                    ok = False
                    why = str(exc)
                    contract_failure_kind = "stale_execution_freeze"
                else:
                    ok = bool(repair_contract["ready"])
                    why = str(repair_contract["reason"])
                    reused_repair_contracts += 1
            if ok:
                ready.append(item)
            else:
                status_text = f"건너뜀: {why}"
                self._record_phase_status(
                    str(spm),
                    "push_status",
                    status_text,
                    (
                        contract_failure_kind
                        or (
                            "source_review"
                            if (
                                repair_contract is not None
                                and repair_contract.get("kind")
                                == "source_review"
                            )
                            else "preflight_skip"
                        )
                    ),
                    why,
                    persist=False,
                )
                self.log(f"[준비 안 됨] {spm.name}: {why}")
        with self.state_lock:
            save_state(self.state)
        reuse_suffix = (
            f" · ② 결과 재사용 {reused_repair_contracts}개"
            if reused_repair_contracts
            else ""
        )
        self.log(
            f"준비 검사: {len(ready)}/{len(targets)}개 push 가능."
            + reuse_suffix
        )
        return ready, None

    @staticmethod
    def _unreal_running():
        result = owned_run(
            ["tasklist", "/FI", "IMAGENAME eq UnrealEditor.exe", "/NH"],
            source="sk_batch.sk_batch_gui.tasklist_observation",
            run_factory=subprocess.run,
            capture_output=True,
            creationflags=0x08000000,
        )
        # ``tasklist`` writes in the active Windows OEM/ANSI code page.  The
        # GUI can run under ``python -X utf8``, where text=True makes the
        # subprocess reader thread decode that output as UTF-8 and crash on
        # localized column text.  The executable token itself is ASCII, so
        # inspect raw bytes and avoid a locale contract entirely.
        return b"UnrealEditor.exe" in (result.stdout or b"")

    def _set_push_state(self, iid, kind, status_text, details=None, message=None):
        progress_message = message or status_text
        non_error_kinds = {
            "completed",
            "ready",
            "exported_pending_unreal",
            "importing",
            "imported_ok",
            "cancelled",
            "stopped",
            "dependency_waiting",
        }
        self.ui_queue.put(("cell", (iid, "push_status", status_text)))
        with self.state_lock:
            entry = self.state.setdefault(iid, {})
            error_message = message or status_text
            existing_error = entry.get("push_status_error") or {}
            unchanged = (
                entry.get("push_status") == status_text
                and entry.get("push_status_kind") == kind
                and (not details or entry.get("push_paths") == details)
                and (
                    kind in non_error_kinds
                    or (
                        existing_error.get("kind") == kind
                        and existing_error.get("message") == error_message
                    )
                )
            )
            if unchanged:
                return
            entry["push_status"] = status_text
            entry["push_status_kind"] = kind
            if kind in non_error_kinds:
                entry.pop("push_status_error", None)
                if kind in {"cancelled", "stopped"}:
                    result = {
                        "time": datetime.now().isoformat(timespec="seconds"),
                        "kind": "cancelled",
                        "outcome": "cancelled",
                        "message": error_message,
                    }
                    if details:
                        result.update(details)
                    entry["push_status_result"] = result
                else:
                    entry.pop("push_status_result", None)
            else:
                error = {
                    "time": datetime.now().isoformat(timespec="seconds"),
                    "kind": kind,
                    "message": error_message,
                }
                if details:
                    error.update(details)
                error.update(
                    self._bind_failure_record(
                        iid,
                        kind,
                        error_message,
                        details,
                    )
                )
                entry["push_status_error"] = error
                entry.pop("push_status_result", None)
            if details:
                entry["push_paths"] = details
            save_state(self.state)
        # Receipt completion follows the durable target state. If the process
        # exits in this narrow gap, restart either reconciles the exact queue
        # result or re-runs the normal #79/#89 provenance checks; it never
        # treats a receipt alone as asset verification.
        if kind == "exported_pending_unreal":
            self._retry_transition(
                iid,
                RETRY_STAGE_POST_CHECK,
                progress_message,
                progress=True,
                heartbeat=True,
            )
        elif kind == "importing":
            self._retry_transition(
                iid,
                RETRY_STAGE_UNREAL,
                progress_message,
                heartbeat=True,
            )
        elif kind == "imported_ok":
            self._retry_transition(
                iid,
                RETRY_STAGE_POST_CHECK,
                progress_message,
                progress=True,
                heartbeat=True,
            )
            self._retry_transition(
                iid,
                RETRY_STAGE_COMPLETE,
                "Unreal post-check complete",
                terminal_reason="completed",
                outcome=RETRY_STAGE_COMPLETE,
            )
        elif kind in {"cancelled", "stopped"}:
            self._retry_transition(
                iid,
                RETRY_STAGE_CANCELLED,
                progress_message,
                terminal_reason="operator_cancelled",
                outcome=RETRY_STAGE_CANCELLED,
            )
        elif kind == "dependency_waiting":
            self._retry_transition(
                iid,
                RETRY_STAGE_POST_CHECK,
                progress_message,
                progress=True,
                heartbeat=True,
            )
        else:
            terminal_stage = (
                RETRY_STAGE_BLOCKED
                if kind in PLANNED_EXCLUSION_KINDS
                or kind in {
                    "dependency_blocked",
                    "manual_required",
                    "not_run",
                    "not_run_unreal",
                    "recovery_blocked",
                }
                else RETRY_STAGE_FAILED
            )
            self._retry_transition(
                iid,
                terminal_stage,
                progress_message,
                terminal_reason=str(kind),
                outcome=terminal_stage,
            )

    def _push_dependency_paths(self):
        planning = self._failed_retry_planning_context()
        cfg = planning.cfg_snapshot if planning is not None else self.cfg
        send2ue_dir = Path(cfg["send2ue_dir"])
        return [
            TOOL_DIR / "jobs" / "send2ue_push_job.py",
            TOOL_DIR / "jobs" / "vertex_color_contract.py",
            TOOL_DIR / "dynamic_wind_handoff_policy.py",
            TOOL_DIR / "cluster_assembly_builder.py",
            TOOL_DIR / "cluster_assembly_handoff_contract.py",
            TOOL_DIR / "push_dependency_schedule.py",
            TOOL_DIR / "unreal_ingest.py",
            TOOL_DIR / "nanite_assembly_materials.py",
            REPO_DIR / "cluster_spm_pair_contract.py",
            REPO_DIR / "speedtree_pipeline_contract.py",
            send2ue_dir / "core" / "export.py",
            send2ue_dir / "core" / "ingest.py",
            send2ue_dir / "dependencies" / "unreal.py",
            send2ue_dir / "resources" / "extensions" / "send2ue_material_pipeline.py",
            send2ue_dir / "resources" / "pipeline" / "ue_material_setup.py",
            Path(
                r"C:\Users\PARK\Documents\GitHub\ue-unique-export-names-addon"
                r"\ue_unique_export_names_addon\unreal_material_json.py"
            ),
        ]

    def _push_unreal_code_paths(self):
        """Code executed or consumed by the current Unreal ingest contract."""
        planning = self._failed_retry_planning_context()
        cfg = planning.cfg_snapshot if planning is not None else self.cfg
        send2ue_dir = Path(cfg["send2ue_dir"])
        return [
            TOOL_DIR / "unreal_ingest.py",
            send2ue_dir / "dependencies" / "unreal.py",
            TOOL_DIR / "dynamic_wind_handoff_policy.py",
            TOOL_DIR / "cluster_assembly_builder.py",
            TOOL_DIR / "nanite_assembly_materials.py",
            send2ue_dir / "resources" / "pipeline" / "ue_material_setup.py",
        ]

    def _push_rebindable_unreal_code_paths(self):
        """Runtime code whose derived bindings can be rebuilt without Blender."""
        paths = self._push_unreal_code_paths()
        # Dynamic-wind policy resolves Blender object facts while exporting;
        # changing it requires a full Push.  The remaining modules consume the
        # frozen artifact/sidecar contract or are regenerated below.
        return [
            path
            for path in paths
            if Path(path).name != "dynamic_wind_handoff_policy.py"
        ]

    def _push_source_dependency_paths(self, spm=None):
        """Return code plus the per-asset Repair/Assembly contract."""
        paths = list(self._push_dependency_paths())
        if spm is not None:
            paths.append(repair_pipeline_report_path(Path(spm)))
        return paths

    @staticmethod
    def _push_material_contract(spm):
        """Write a strict, live-validated contract wrapper for Blender Push."""
        spm = Path(spm)
        repair_report = (
            spm.parent / "reports" /
            f"{spm.stem}_speedtree_repair_pipeline_report_codex.json"
        )
        try:
            payload = json.loads(repair_report.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise RuntimeError(
                f"SpeedTree Repair contract report could not be read: {exc}"
            ) from exc
        envelope, material_source_spm = (
            _material_handoff_envelope_for_push(
                payload,
                speedtree_output_spm_for(spm),
            )
        )
        if not isinstance(envelope, dict):
            raise RuntimeError(
                "SpeedTree Repair report has no current pipeline contract; "
                "run Blender Repair again"
            )
        if (payload.get("handoff_preflight") or {}).get("status") != "ok":
            raise RuntimeError(
                "SpeedTree Repair handoff preflight is not ready; "
                "run Blender Repair again"
            )
        validate_preflight_envelope(
            envelope, material_source_spm, require_ok=True
        )
        source_fingerprint = str(envelope.get("source_fingerprint") or "")
        suffix = source_fingerprint[:16] or hashlib.sha256(
            _normalized_path(spm).encode("utf-8")
        ).hexdigest()[:16]
        contract_path = LOG_DIR / (
            f"{spm.stem}_push_material_contract_{suffix}.json"
        )
        atomic_write_json(
            contract_path,
            {
                "status": "ok",
                "speedtree_pipeline_contract": envelope,
                "canonical_spm": str(
                    speedtree_output_spm_for(spm).resolve()
                ),
                "material_source_spm": str(material_source_spm),
                "source_repair_report": str(repair_report.resolve()),
            },
        )
        return contract_path

    def _repair_evidence_path_for_push(self, spm):
        """Materialize validated same-generation evidence for Blender."""
        repair_contract = self._repair_stage_contract(spm)
        if repair_contract is None or repair_contract.get("ready") is not True:
            return None
        try:
            self._validate_repair_stage_contract(spm, repair_contract)
        except RepairPushEvidenceError as exc:
            raise BatchItemError(
                str(exc),
                kind="stale_execution_freeze",
            ) from exc
        bundle = repair_contract["evidence_bundle"]
        digest = str(bundle["bundle_sha256"])
        destination = LOG_DIR / (
            f"{Path(spm).stem}_repair_push_evidence_{digest[:16]}.json"
        )
        atomic_write_json(destination, bundle)
        return destination

    def _current_push_status_text(self, iid, spm):
        """Show current receipt validity without hashing multi-GB blend files."""
        entry = self.state.get(iid, {})
        saved = str(entry.get("push_status") or "")
        kind = str(entry.get("push_status_kind") or "")
        if kind == "preflight_skip":
            ready, reason = self._handoff_ready(spm)
            return "준비됨 ✓" if ready else f"건너뜀: {reason}"
        success_like = kind == "imported_ok" or saved.startswith("완료")
        export_like = kind == "exported_pending_unreal" or saved.startswith(
            "export 완료"
        )
        if not (success_like or export_like):
            return saved or "-"

        source_cache = entry.get("push_source_fingerprint_cache") or {}
        export_cache = entry.get("push_export_cache") or {}
        if (
            source_cache.get("version")
            != PUSH_SOURCE_FINGERPRINT_CACHE_VERSION
            or not source_cache.get("fingerprint")
        ):
            return "Push 재확인 필요 — 최신성 영수증 없음"
        try:
            current_snapshot = push_source_snapshot(
                blend_path_for(spm),
                self._push_source_dependency_paths(spm),
            )
        except (OSError, ValueError, KeyError):
            return "Push 재확인 필요 — 입력 파일 확인"
        if source_cache.get("snapshot") != current_snapshot:
            return "Push 재확인 필요 — Blender/파이프라인 변경"
        if export_cache.get("source_fingerprint") != source_cache.get(
            "fingerprint"
        ):
            return "Push 재확인 필요 — export 영수증 불일치"
        manifest_path = Path(export_cache.get("manifest") or "")
        if not manifest_path.is_file():
            return "Push 재확인 필요 — export manifest 없음"
        export_fingerprint = export_cache.get("fingerprint")
        if success_like:
            if entry.get("push_import_fingerprint") != export_fingerprint:
                return "Push 재확인 필요 — Unreal 결과 불일치"
            return "완료 (현재 최신)"
        return "export 완료 · Unreal 대기"

    def _source_push_fingerprint(self, blend, iid=None):
        """Hash a large source blend once, then reuse its stable stat cache."""
        planning = self._failed_retry_planning_context()
        if iid:
            if planning is not None:
                cache = copy.deepcopy(
                    planning.entry(iid).get("push_source_fingerprint_cache")
                )
            else:
                with self.state_lock:
                    cache = copy.deepcopy(
                        self.state.get(iid, {}).get(
                            "push_source_fingerprint_cache"
                        )
                    )
        else:
            cache = None
        fingerprint, record, cache_hit = cached_push_source_fingerprint(
            blend,
            self._push_source_dependency_paths(iid),
            cache=cache,
        )
        if iid and planning is None:
            with self.state_lock:
                self.state.setdefault(iid, {})[
                    "push_source_fingerprint_cache"
                ] = record
        if planning is not None:
            planning.counters[
                "source_fingerprint_cache_hits"
                if cache_hit
                else "source_fingerprint_cache_misses"
            ] += 1
        if cache_hit and planning is None:
            self.log(f"[source hash cache] {Path(blend).name}: 재사용")
        return fingerprint

    def _cached_manifest_item(self, iid, source_fingerprint):
        if getattr(self, "force_rerun", False):
            return None
        cache = self.state.get(iid, {}).get("push_export_cache") or {}
        if cache.get("source_fingerprint") != source_fingerprint:
            return None
        manifest_path = Path(cache.get("manifest", ""))
        if not manifest_path.is_file():
            return None
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            item = next(
                candidate
                for candidate in (manifest.get("items") or [])
                if str((candidate or {}).get("queue_id")) == iid
            )
        except (OSError, ValueError, StopIteration, TypeError):
            return None
        if str(item.get("queue_id")) != iid or not manifest_item_files_match(item):
            return None
        return item

    def _export_manifest_item(self, iid, spm, batch_stamp):
        spm = self._prepare_pair_for_job(spm)
        blend = blend_path_for(spm)
        repair_evidence = self._repair_evidence_path_for_push(spm)
        source_fingerprint = self._source_push_fingerprint(blend, iid)
        cached = (
            None
            if repair_evidence is not None
            else self._cached_manifest_item(iid, source_fingerprint)
        )
        import_report = LOG_DIR / f"{spm.stem}_unreal_{batch_stamp}.json"
        if cached is not None:
            cached = dict(cached)
            cached["report_path"] = str(import_report.resolve())
            details = {
                "manifest": self.state[iid]["push_export_cache"]["manifest"],
                "export_report": cached.get("export_report_path", ""),
                "import_report": str(import_report),
                "cache": "hit",
            }
            self._set_push_state(
                iid,
                "exported_pending_unreal",
                "export 완료 · Unreal 대기 (cache)",
                details=details,
            )
            self.log(f"[export cache] {spm.name}: {cached['fingerprint']}")
            return cached

        cache_root = LOG_DIR / "send2ue_export_cache" / source_fingerprint / spm.stem
        manifest_path = LOG_DIR / f"{spm.stem}_manifest_{source_fingerprint}.json"
        export_report = LOG_DIR / f"{spm.stem}_export_{source_fingerprint}.json"
        export_log_name = f"{spm.stem}_export_{batch_stamp}.log"
        checkpoint_path = LOG_DIR / f"{spm.stem}_rpc_checkpoint_{batch_stamp}.json"
        batch_report = LOG_DIR / f"{spm.stem}_rpc_batch_{batch_stamp}.json"
        send2ue_unreal_py = (
            Path(self.cfg["send2ue_dir"]) / "dependencies" / "unreal.py"
        )
        material_contract = self._push_material_contract(spm)
        cmd = [
            self.cfg["blender_exe"],
            "--factory-startup",
            "-b",
            str(blend),
            "--python",
            str(TOOL_DIR / "jobs" / "send2ue_push_job.py"),
            "--",
            "--transport",
            "headless_export",
            "--report",
            str(export_report),
            "--spm",
            str(spm),
            "--material-contract",
            str(material_contract),
            "--manifest",
            str(manifest_path),
            "--checkpoint",
            str(checkpoint_path),
            "--batch-report",
            str(batch_report),
            "--item-import-report",
            str(import_report),
            "--export-root",
            str(cache_root),
            "--queue-id",
            iid,
            "--source-fingerprint",
            source_fingerprint,
            "--unreal-ingest",
            str(TOOL_DIR / "unreal_ingest.py"),
            "--send2ue-unreal-py",
            str(send2ue_unreal_py),
            "--max-push-polygons",
            str(self.cfg.get("push_max_polygons", 2_000_000)),
            "--max-push-bones",
            str(self.cfg.get("push_max_bones", 1_500)),
        ]
        if (
            getattr(self, "_active_push_dependency_map", {}) or {}
        ).get(iid):
            cmd.append("--dependency-orchestrated")
        if repair_evidence is not None:
            cmd.extend([
                "--repair-evidence",
                str(repair_evidence),
            ])
        wind_override = self._batch_job_item(iid).get(
            "wind_override", "auto"
        )
        resolved_wind = (
            wind_preset_for_spm(spm)
            if wind_override == "auto"
            else wind_override
        )
        if resolved_wind == "TREE":
            cmd.append("--require-green-signal")
        stage_timeout = max(1, int(
            self.cfg.get("child_stage_inactivity_timeout", 180)
        ))
        disk_export_timeout = max(1, int(
            self.cfg.get("push_job_timeout", 1800)
        ))
        code, log_file = self._run_limited(
            cmd,
            export_log_name,
            None,
            affinity=self.cfg.get("blender_parallel_jobs", 2) <= 1,
            inactivity_timeout=disk_export_timeout,
            inactivity_timeout_by_marker=send2ue_inactivity_rules(
                stage_timeout, disk_export_timeout
            ),
        )
        result = load_job_report(export_report)
        if code != 0 or result.get("status") != "exported_pending_unreal":
            reason = summarize_job_failure(result, log_file)
            raise BatchItemError(
                reason,
                kind=classify_push_failure(result, log_file),
                report=result,
                log_file=log_file,
                report_file=export_report,
            )
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            item = (manifest.get("items") or [])[0]
        except (OSError, ValueError, IndexError, TypeError) as exc:
            raise BatchItemError(
                f"manifest read failed: {exc}",
                kind="data_error",
                log_file=log_file,
                report_file=export_report,
            ) from exc
        if not manifest_item_files_match(item):
            raise BatchItemError(
                "manifest exported-file fingerprint verification failed",
                kind="data_error",
                log_file=log_file,
                report_file=export_report,
            )

        post_export_source_fingerprint = self._source_push_fingerprint(blend, iid)
        with self.state_lock:
            self.state.setdefault(iid, {})["push_export_cache"] = {
                "source_fingerprint": post_export_source_fingerprint,
                "manifest": str(manifest_path),
                "fingerprint": item["fingerprint"],
            }
        details = {
            "manifest": str(manifest_path),
            "export_report": str(export_report),
            "export_log": str(log_file),
            "import_report": str(import_report),
            "cache": "miss",
        }
        self._set_push_state(
            iid,
            "exported_pending_unreal",
            "export 완료 · Unreal 대기",
            details=details,
        )
        return item

    def _sync_headless_checkpoint(
        self,
        checkpoint_path,
        item_by_id,
        log_file=None,
        observed_line=None,
    ):
        try:
            checkpoint = json.loads(Path(checkpoint_path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        labels = {
            "importing": "Unreal import 중...",
            "imported_ok": "완료 (headless)",
            "data_error": "실패: data error",
            "manual_required": "수동 처리 필요",
            "unreal_crash": "Unreal commandlet crash",
            "not_run": "미실행",
        }
        for queue_id, result in (checkpoint.get("items") or {}).items():
            if queue_id not in item_by_id:
                continue
            status = result.get("status", "not_run")
            message = result.get("message") or labels.get(status, status)
            if status == "importing":
                versions = self.__dict__.setdefault(
                    "_retry_checkpoint_versions", {}
                )
                version = str(result.get("updated_at") or "")
                progressed = bool(version and versions.get(queue_id) != version)
                if version:
                    versions[queue_id] = version
                last_lines = self.__dict__.setdefault(
                    "_retry_checkpoint_output_lines", {}
                )
                output_changed = bool(
                    observed_line
                    and last_lines.get(queue_id) != str(observed_line)
                )
                if output_changed:
                    last_lines[queue_id] = str(observed_line)
                tracker = self._retry_tracker_for_job()
                if tracker is not None:
                    tracker.observe_process(
                        queue_id,
                        stage=RETRY_STAGE_UNREAL,
                        diagnostic=observed_line or message,
                        output=output_changed,
                        progress=progressed,
                    )
            text = labels.get(status, status)
            if status in {"data_error", "manual_required", "unreal_crash", "not_run"}:
                text = f"{text}: {compact_error_message(message, 80)}"
            item = item_by_id[queue_id]
            details = {
                "manifest": str(item.get("batch_manifest", "")),
                "checkpoint": str(checkpoint_path),
                "report": str(item.get("report_path", "")),
                "batch_report": str(item.get("batch_report", "")),
            }
            recovery = item.get("recovery") or {}
            if recovery:
                details.update({
                    "parent_manifest": str(
                        recovery.get("parent_manifest") or ""
                    ),
                    "parent_report": str(
                        recovery.get("parent_report") or ""
                    ),
                    "old_code_revision": str(
                        recovery.get("old_code_revision") or ""
                    ),
                    "new_code_revision": str(
                        recovery.get("new_code_revision") or ""
                    ),
                })
            if log_file:
                details["log"] = str(log_file)
            if status == "imported_ok":
                entry = self.state.setdefault(queue_id, {})
                entry["push_import_fingerprint"] = item["fingerprint"]
                if recovery:
                    entry["push_export_cache"] = {
                        "source_fingerprint": item["source_fingerprint"],
                        "manifest": str(item.get("batch_manifest", "")),
                        "fingerprint": item["fingerprint"],
                    }
            self._set_push_state(
                queue_id,
                status,
                text,
                details=details,
                message=message,
            )
        status_counts = {}
        for result in (checkpoint.get("items") or {}).values():
            status = str(result.get("status") or "not_run")
            status_counts[status] = status_counts.get(status, 0) + 1
        total = len(item_by_id)
        active = status_counts.get("importing", 0)
        completed = sum(
            count
            for status, count in status_counts.items()
            if status != "importing"
        )
        progress_snapshot = (completed, total, active)
        if progress_snapshot != getattr(
            self, "_headless_progress_snapshot", None
        ):
            self._headless_progress_snapshot = progress_snapshot
            self.ui_queue.put(
                (
                    "progress",
                    f"{getattr(self, '_headless_progress_label', 'Unreal Push')} "
                    f"{completed}/{total} · "
                    f"headless 처리 중 {active}개",
                )
            )
        return checkpoint

    @staticmethod
    def _record_headless_process_exit(checkpoint_path, max_item_crash_retries):
        checkpoint_path = Path(checkpoint_path)
        try:
            checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        current_id = checkpoint.get("current_item")
        state = (checkpoint.get("items") or {}).get(current_id)
        if not current_id or not state or state.get("status") != "importing":
            return checkpoint
        crash_count = int(state.get("crash_count", 0)) + 1
        status = (
            "manual_required"
            if crash_count > int(max_item_crash_retries)
            else "unreal_crash"
        )
        message = (
            f"Unreal commandlet crash retry limit exceeded ({max_item_crash_retries})"
            if status == "manual_required"
            else "UnrealEditor-Cmd exited while this item was importing"
        )
        state.update(
            {
                "status": status,
                "crash_count": crash_count,
                "message": message,
                "updated_at": datetime.now().isoformat(timespec="seconds"),
            }
        )
        checkpoint["current_item"] = None
        checkpoint["updated_at"] = datetime.now().isoformat(timespec="seconds")
        atomic_write_json(checkpoint_path, checkpoint)
        return checkpoint

    def _run_failed_unreal_recovery(
        self,
        targets,
        recovery_requests,
        emit_done=True,
    ):
        """Rebind verified cached exports to current code and run Unreal only."""
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        batch_stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        self._headless_progress_snapshot = None
        self._phase_abort_reason = None
        failed_items = set()
        recovered_by_id = {}
        selected_ids = {str(item["spm"]) for item in targets}
        retry_metadata = copy.deepcopy(
            getattr(self, "_active_retry_metadata", {}) or {}
        )
        runtime_code_paths = self._push_unreal_code_paths()
        rebindable_code_paths = self._push_rebindable_unreal_code_paths()
        parent_lineage = []
        pending_order = []
        total = len(selected_ids)
        self.ui_queue.put(("batch_progress", (0, total)))
        self.ui_queue.put((
            "progress",
            f"실패 재시도 · Unreal-only 산출물 검증 0/{total}",
        ))
        prepared_selected = 0

        for request in recovery_requests:
            request_selected = {
                str(value) for value in request.get("selected_queue_ids") or []
            }
            try:
                parent_path, parent_manifest, parent_items = (
                    load_unreal_recovery_parent_manifest(
                        request.get("parent_manifest")
                    )
                )
                required_ids = unreal_recovery_dependency_closure(
                    parent_items, request_selected
                )
                source_records = request.get("source_records") or {}
                group_items = {}
                for queue_id, parent_item in parent_items.items():
                    if queue_id not in required_ids:
                        continue
                    self.ui_queue.put(
                        (
                            "cell",
                            (
                                queue_id,
                                "push_status",
                                "기존 산출물 검증 · 현재 코드 재결합 중...",
                            ),
                        )
                    )
                    parent_source_record = copy.deepcopy(
                        source_records.get(queue_id) or {}
                    )
                    with self.state_lock:
                        self.state.setdefault(queue_id, {}).setdefault(
                            "push_recovery_source_proofs", {}
                        )[str(parent_item.get("source_fingerprint") or "")] = (
                            copy.deepcopy(parent_source_record)
                        )
                    blend = Path(parent_item.get("blend") or "")
                    current_fingerprint = self._source_push_fingerprint(
                        blend, queue_id
                    )
                    current_record = copy.deepcopy(
                        self.state.get(queue_id, {}).get(
                            "push_source_fingerprint_cache"
                        )
                        or {}
                    )
                    item_report = LOG_DIR / (
                        f"{Path(queue_id).stem}_failed_retry_unreal_{batch_stamp}.json"
                    )
                    recovered = recover_manifest_item(
                        parent_item,
                        parent_manifest_path=parent_path,
                        parent_report_path=(
                            request.get("parent_report")
                            or parent_manifest.get("report_path")
                            or ""
                        ),
                        parent_source_record=parent_source_record,
                        current_source_record=current_record,
                        current_source_fingerprint=current_fingerprint,
                        runtime_code_paths=runtime_code_paths,
                        rebindable_code_paths=rebindable_code_paths,
                        report_path=item_report,
                        selected=queue_id in request_selected,
                    )
                    existing = recovered_by_id.get(queue_id)
                    if existing is not None and existing.get(
                        "fingerprint"
                    ) != recovered.get("fingerprint"):
                        raise PushUnrealRecoveryError(
                            "conflicting parent recovery contracts for "
                            + Path(queue_id).name
                        )
                    group_items[queue_id] = recovered
                recovered_by_id.update(group_items)
                pending_order.extend(
                    queue_id
                    for queue_id in parent_items
                    if queue_id in group_items
                )
                parent_lineage.append({
                    "manifest": str(parent_path),
                    "report": str(
                        request.get("parent_report")
                        or parent_manifest.get("report_path")
                        or ""
                    ),
                    "selected_queue_ids": sorted(request_selected),
                })
                prepared_selected += len(request_selected)
                self.ui_queue.put(
                    ("batch_progress", (prepared_selected, total))
                )
                self.ui_queue.put(
                    (
                        "progress",
                        "실패 재시도 · Unreal-only 산출물 검증 "
                        f"{prepared_selected}/{total}",
                    )
                )
            except Exception as exc:
                reason = compact_error_message(exc)
                for queue_id in request_selected:
                    self._set_push_state(
                        queue_id,
                        "recovery_blocked",
                        self._failure_status_text(
                            "재시도 차단 · " + reason,
                            "recovery_blocked",
                        ),
                        details={
                            "parent_manifest": str(
                                request.get("parent_manifest") or ""
                            ),
                        },
                        message=reason,
                    )
                    failed_items.add(queue_id)
                self.log(
                    "[Failed retry Unreal-only blocked] "
                    + ", ".join(Path(value).name for value in request_selected)
                    + f": {reason}"
                )

        pending = [
            recovered_by_id[queue_id]
            for queue_id in pending_order
            if queue_id in recovered_by_id
        ]
        deduplicated = []
        seen = set()
        for item in pending:
            queue_id = str(item["queue_id"])
            if queue_id in seen:
                continue
            seen.add(queue_id)
            deduplicated.append(item)
            details = {
                "parent_manifest": str(
                    (item.get("recovery") or {}).get("parent_manifest") or ""
                ),
                "old_code_revision": str(
                    (item.get("recovery") or {}).get("old_code_revision") or ""
                ),
                "new_code_revision": str(
                    (item.get("recovery") or {}).get("new_code_revision") or ""
                ),
            }
            self._set_push_state(
                queue_id,
                "exported_pending_unreal",
                "기존 export 검증 완료 · 현재 코드 Unreal 대기",
                details=details,
            )
        pending = deduplicated

        if not pending:
            self._phase_failed_items = failed_items or selected_ids
            if emit_done:
                self.ui_queue.put((
                    "progress",
                    "실패 재시도 · Unreal-only 차단 · Blender→Push 필요",
                ))
                self.ui_queue.put(("done", None))
            return False

        active_source = getattr(self, "_active_production_source_manifest", None)
        metadata = {
            "retry": retry_metadata or {
                "schema_version": 1,
                "kind": "failed_blender_export_and_unreal_retry",
                "partition": "unreal_ingest",
                "execution_path": "immutable_unreal_only",
                "selected_queue_ids": sorted(selected_ids),
            },
            "recovery": {
                "schema_version": 1,
                "kind": "failed_results_retry_unreal_only",
                "parents": parent_lineage,
                "selected_queue_ids": sorted(selected_ids),
                "production_source_revision": (
                    active_source.content_hash if active_source is not None else ""
                ),
            }
        }
        return self._run_headless_import_items(
            pending,
            targets,
            failed_items,
            emit_done=emit_done,
            batch_stamp=batch_stamp,
            manifest_metadata=metadata,
            progress_label="실패 재시도 · Unreal-only",
            file_prefix="failed_retry_unreal",
        )

    def _run_headless_push_batch(self, targets, emit_done=True):
        batch_stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        total = len(targets)
        retry_metadata = copy.deepcopy(
            getattr(self, "_active_retry_metadata", {}) or {}
        )
        export_retry = retry_metadata.get("partition") == "blender_export"
        progress_label = (
            "실패 재시도 · Blender/Send2UE→Unreal"
            if export_retry
            else "Unreal Push"
        )
        self._headless_progress_snapshot = None
        self.ui_queue.put(("progress", f"{progress_label} export 준비 0/{total}"))
        dependency_map = dict(
            getattr(self, "_active_push_dependency_map", {}) or {}
        )
        required_dependency_ids = {
            dependency
            for dependencies in dependency_map.values()
            for dependency in dependencies
        }
        self.ui_queue.put(("batch_progress", (0, total)))
        exported_by_index = {}
        failed_items = set(getattr(self, "_phase_failed_items", set()))
        workers = max(
            1,
            min(int(self.cfg.get("blender_parallel_jobs", 2)), total),
        )
        if workers > 1:
            self.log(f"Send2UE export: {workers}개 동시 실행")

        def export_one(index, item):
            if self.stop_flag.is_set():
                return index, None
            spm = item["spm"]
            iid = str(spm)
            retry_context = getattr(self, "_retry_thread_context", None)
            if retry_context is not None:
                retry_context.target_id = iid
                retry_context.stage = RETRY_STAGE_SEND2UE
            self._retry_transition(
                iid,
                RETRY_STAGE_SEND2UE,
                "Send2UE export started",
                progress=True,
                heartbeat=True,
            )
            self.ui_queue.put(("cell", (iid, "push_status", "Send2UE export 중...")))
            try:
                return index, self._export_manifest_item(iid, spm, batch_stamp)
            except Exception as exc:
                reason = compact_error_message(exc)
                kind = getattr(exc, "kind", "data_error")
                details = {}
                failure_report = getattr(exc, "report", None)
                if isinstance(failure_report, dict) and failure_report:
                    details["failure_report"] = copy.deepcopy(failure_report)
                    if failure_report.get("reason_token"):
                        details["reason_token"] = str(
                            failure_report["reason_token"]
                        )
                    if isinstance(failure_report.get("evidence"), dict):
                        details["evidence"] = copy.deepcopy(
                            failure_report["evidence"]
                        )
                if getattr(exc, "log_file", None):
                    details["log"] = str(exc.log_file)
                if getattr(exc, "report_file", None):
                    details["report"] = str(exc.report_file)
                self._set_push_state(
                    iid,
                    kind,
                    self._failure_status_text(reason, kind),
                    details=details,
                    message=reason,
                )
                self.log(f"[headless export {kind}] {spm.name}: {reason}")
                with self.state_lock:
                    failed_items.add(iid)
                return index, None
            finally:
                if retry_context is not None:
                    retry_context.target_id = None
                    retry_context.stage = None

        completed = 0
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(export_one, index, item): item
                for index, item in enumerate(targets)
            }
            for future in as_completed(futures):
                index, exported_item = future.result()
                if exported_item is not None:
                    exported_by_index[index] = exported_item
                completed += 1
                self.ui_queue.put(("batch_progress", (completed, total)))
                self.ui_queue.put(
                    (
                        "progress",
                        f"{progress_label} export {completed}/{total} · "
                        "headless 준비",
                    )
                )

        exported = [exported_by_index[index] for index in sorted(exported_by_index)]
        exported_ids = {
            str(item.get("queue_id"))
            for item in exported
            if item.get("queue_id")
        }
        dependency_blocked_ids = set()
        dependency_waiting_ids = set()
        external_current_dependencies = {}
        for item in exported:
            iid = str(item.get("queue_id") or "")
            unavailable = [
                dependency
                for dependency in dependency_map.get(iid, ())
                if dependency not in exported_ids
            ]
            if not unavailable:
                continue
            blocked = []
            waiting = []
            verdicts = {}
            for dependency in unavailable:
                verdict = self._dependency_artifact_verdict(
                    dependency,
                    phase="push",
                )
                verdicts[dependency] = verdict
                status = str(verdict.get("status") or "stale")
                if status == "current":
                    external_current_dependencies.setdefault(iid, set()).add(
                        dependency
                    )
                    self.__dict__.setdefault(
                        "_pipeline_dependency_reuse_evidence", {}
                    ).setdefault(iid, {})[dependency] = copy.deepcopy(verdict)
                    self.log(
                        "[의존 산출물 재사용] "
                        f"{Path(iid).name}: {Path(dependency).name}의 "
                        "기존 Unreal import 영수증이 current이므로 진행합니다."
                    )
                elif status == "waiting":
                    waiting.append(dependency)
                else:
                    blocked.append(dependency)
            if blocked:
                reason = "필수 producer 산출물이 current가 아닙니다: " + " | ".join(
                    f"{Path(dependency).name}: "
                    f"{self._dependency_artifact_state_label(verdicts[dependency].get('status'))} · "
                    f"{verdicts[dependency].get('reason') or 'current 증거 없음'}"
                    for dependency in blocked
                )
                self._set_push_state(
                    iid,
                    "dependency_blocked",
                    self._failure_status_text(reason, "dependency_blocked"),
                    details={
                        "reason_token": "shared_dependency_failed",
                        "blocked_by": blocked,
                        "dependency_artifacts": copy.deepcopy(verdicts),
                    },
                    message=reason,
                )
                failed_items.add(iid)
                dependency_blocked_ids.add(iid)
                continue
            if waiting:
                reason = "필수 producer의 current export는 있으나 Unreal 반영 완료를 기다립니다: " + ", ".join(
                    Path(dependency).name for dependency in waiting
                )
                self._set_push_state(
                    iid,
                    "dependency_waiting",
                    f"대기: {reason}",
                    details={
                        "blocked_by": waiting,
                        "dependency_artifacts": copy.deepcopy(verdicts),
                    },
                    message=reason,
                )
                dependency_waiting_ids.add(iid)
                self.log(f"[의존 산출물 대기] {Path(iid).name}: {reason}")
        unavailable_consumer_ids = (
            dependency_blocked_ids | dependency_waiting_ids
        )
        if unavailable_consumer_ids:
            exported = [
                item
                for item in exported
                if str(item.get("queue_id") or "")
                not in unavailable_consumer_ids
            ]

        if self.stop_flag.is_set():
            for item in targets:
                iid = str(item["spm"])
                if self.state.get(iid, {}).get("push_status_kind") not in {
                    "exported_pending_unreal", "imported_ok", "data_error", "manual_required"
                }:
                    self._set_push_state(iid, "cancelled", "중지: 사용자 중지")
            if emit_done:
                self.ui_queue.put(("progress", "중지됨"))
                self.ui_queue.put(("done", None))
            self._phase_failed_items = failed_items
            return False

        pending = []
        for item in exported:
            iid = str(item["queue_id"])
            item["depends_on_queue_ids"] = [
                dependency
                for dependency in dependency_map.get(iid, ())
                if dependency not in external_current_dependencies.get(
                    iid, set()
                )
            ]
            entry = self.state.setdefault(iid, {})
            import_cache_matches = (
                not self.force_rerun
                and entry.get("push_import_fingerprint") == item["fingerprint"]
            )
            must_verify_unreal_assets = (
                iid in required_dependency_ids
                or manifest_item_requires_unreal_asset_verification(item)
            )
            if import_cache_matches and must_verify_unreal_assets:
                # The export cache is still valid, but a Tree in this batch
                # needs every Assembly/provider asset to exist in this Unreal
                # project. Unreal imports only when any contracted path is
                # absent.
                item["verify_existing_assets"] = True
                pending.append(item)
                continue
            if import_cache_matches:
                self._set_push_state(iid, "imported_ok", "완료 (import cache)")
                continue
            pending.append(item)

        if not pending:
            self._phase_failed_items = failed_items
            success_count = sum(
                1 for item in targets
                if str(item["spm"]) not in failed_items
            )
            failure_count = len(failed_items)
            if emit_done:
                text = (
                    f"{progress_label} 완료 (cache)"
                    if not failure_count
                    else (
                        f"{progress_label} 종료 — 성공 {success_count}개 · "
                        f"실패/준비 제외 {failure_count}개"
                    )
                )
                self.ui_queue.put(("progress", text))
                self.ui_queue.put(("done", None))
            return not failure_count

        return self._run_headless_import_items(
            pending,
            targets,
            failed_items,
            emit_done=emit_done,
            batch_stamp=batch_stamp,
            manifest_metadata=(
                {"retry": retry_metadata} if retry_metadata else None
            ),
            progress_label=progress_label,
            file_prefix=(
                "failed_retry_blender_export"
                if export_retry
                else "headless_queue"
            ),
        )

    def _run_headless_import_items(
        self,
        pending,
        targets,
        failed_items,
        *,
        emit_done,
        batch_stamp,
        manifest_metadata=None,
        progress_label="Unreal Push",
        file_prefix="headless_queue",
    ):
        """Run already-materialized immutable items through Unreal only."""
        self._headless_progress_label = progress_label
        manifest_path = LOG_DIR / f"{file_prefix}_{batch_stamp}.json"
        checkpoint_path = LOG_DIR / f"{file_prefix}_{batch_stamp}_checkpoint.json"
        report_path = LOG_DIR / f"{file_prefix}_{batch_stamp}_report.json"
        for item in pending:
            item["batch_manifest"] = str(manifest_path)
            item["batch_report"] = str(report_path)
        manifest = {
            "schema_version": PUSH_MANIFEST_SCHEMA_VERSION,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "checkpoint_path": str(checkpoint_path),
            "report_path": str(report_path),
            "max_item_crash_retries": int(
                self.cfg.get("headless_item_crash_retries", 2)
            ),
            "items": pending,
        }
        reserved = set(manifest)
        for key, value in (manifest_metadata or {}).items():
            if key not in reserved:
                manifest[key] = copy.deepcopy(value)
        atomic_write_json(manifest_path, manifest)
        item_by_id = {str(item["queue_id"]): item for item in pending}

        commandlet = self.cfg["unreal_editor_cmd"]
        project = self.cfg["unreal_project"]
        runner = TOOL_DIR / "unreal_ingest.py"
        cmd = [
            commandlet,
            project,
            "-run=pythonscript",
            f"-script={runner}",
            "-unattended",
            "-NoSplash",
            "-NoSound",
            "-UTF8Output",
        ]
        env = os.environ.copy()
        env.update(
            {
                "SK_BATCH_MANIFEST_PATH": str(manifest_path.resolve()),
                "SK_BATCH_CHECKPOINT_PATH": str(checkpoint_path.resolve()),
                "SK_BATCH_REPORT_PATH": str(report_path.resolve()),
            }
        )
        max_restarts = max(
            0, int(self.cfg.get("headless_batch_max_restarts", 10))
        )
        complete = False
        last_log = None
        checkpoint = {}
        for launch_index in range(max_restarts + 1):
            if self.stop_flag.is_set():
                break
            self.ui_queue.put(
                (
                    "progress",
                    f"{progress_label} headless 시작 · "
                    f"시도 {launch_index + 1}/{max_restarts + 1}",
                )
            )
            self.log(
                f"UnrealEditor-Cmd headless 시작 ({launch_index + 1}/{max_restarts + 1})"
            )
            attempt_log = LOG_DIR / (
                f"{file_prefix}_unreal_{batch_stamp}_{launch_index + 1}.log"
            )
            last_log = attempt_log
            try:
                code, last_log = self._run_limited(
                    cmd,
                    attempt_log.name,
                    self.cfg.get("headless_job_timeout", 14_400),
                    affinity=False,
                    progress_callback=lambda _elapsed, _line: self._sync_headless_checkpoint(
                        checkpoint_path,
                        item_by_id,
                        attempt_log,
                        observed_line=_line,
                    ),
                    env=env,
                )
            except Exception as exc:
                if (
                    self.stop_flag.is_set()
                    or getattr(exc, "kind", "") in {"cancelled", "stopped"}
                ):
                    self.log(f"[headless 중지] {exc}")
                    break
                code = -1
                self.log(f"[headless watchdog] {exc}")
            checkpoint = self._sync_headless_checkpoint(
                checkpoint_path,
                item_by_id,
                last_log,
            )
            complete = bool(checkpoint.get("complete")) and report_path.is_file()
            if complete:
                break
            checkpoint = self._record_headless_process_exit(
                checkpoint_path,
                manifest["max_item_crash_retries"],
            )
            self._sync_headless_checkpoint(
                checkpoint_path,
                item_by_id,
                last_log,
            )
            self.log(
                f"[headless watchdog] commandlet 종료 code={code}; checkpoint 재개"
            )

        if self.stop_flag.is_set():
            checkpoint = self._sync_headless_checkpoint(
                checkpoint_path,
                item_by_id,
                last_log,
            )
            actual_failure_kinds = {
                "data_error",
                "manual_required",
                "unreal_crash",
                "owner_lost",
            }
            for iid in item_by_id:
                item_status = str(
                    ((checkpoint.get("items") or {}).get(iid) or {}).get(
                        "status"
                    )
                    or ""
                )
                if item_status == "imported_ok":
                    continue
                if item_status in actual_failure_kinds:
                    failed_items.add(str(iid))
                    continue
                self._set_push_state(
                    iid,
                    "cancelled",
                    "중지: 사용자 중지",
                    details={
                        "manifest": str(manifest_path),
                        "checkpoint": str(checkpoint_path),
                        "log": str(last_log or ""),
                    },
                )
            with self.state_lock:
                self._phase_failed_items = set(failed_items)
                save_state(self.state)
            if emit_done:
                self.ui_queue.put(("progress", f"{progress_label} 중지됨"))
                self.ui_queue.put(("done", None))
            return False

        if not complete:
            checkpoint = self._sync_headless_checkpoint(
                checkpoint_path,
                item_by_id,
                last_log,
            )
            terminal = {
                "imported_ok",
                "data_error",
                "manual_required",
                "unreal_crash",
            }
            for iid in item_by_id:
                status = (checkpoint.get("items") or {}).get(iid, {}).get("status")
                if status not in terminal:
                    self._set_push_state(
                        iid,
                        "not_run",
                        "미실행: headless watchdog 재시작 상한 초과",
                        details={
                            "manifest": str(manifest_path),
                            "checkpoint": str(checkpoint_path),
                            "log": str(last_log or ""),
                        },
                    )
            self._phase_abort_reason = "headless watchdog 재시작 상한 초과"

        for iid, result in (checkpoint.get("items") or {}).items():
            if result.get("status") != "imported_ok":
                failed_items.add(str(iid))

        with self.state_lock:
            self._phase_failed_items = set(failed_items)
            save_state(self.state)
        if emit_done:
            success_count = sum(
                1
                for item in targets
                if str(item["spm"]) not in failed_items
            )
            failure_count = len(failed_items)
            if not complete:
                progress_text = self._phase_abort_reason
            elif failure_count:
                progress_text = (
                    f"{progress_label} 종료 — 성공 {success_count}개 · "
                    f"실패/준비 제외 {failure_count}개"
                )
            else:
                progress_text = f"{progress_label} 완료"
            self.ui_queue.put(("progress", progress_text))
            self.ui_queue.put(("done", None))
        return complete and not failed_items

    def _job_push(self, iid, spm):
        spm = self._prepare_pair_for_job(spm)
        blend = blend_path_for(spm)
        repair_evidence = self._repair_evidence_path_for_push(spm)
        source_fingerprint = self._source_push_fingerprint(blend, iid)
        if (
            repair_evidence is None
            and not getattr(self, "force_rerun", False)
        ):
            cached = self._cached_manifest_item(iid, source_fingerprint)
            if (
                cached is not None
                and self.state.get(iid, {}).get("push_import_fingerprint")
                == cached.get("fingerprint")
                and not manifest_item_requires_unreal_asset_verification(
                    cached
                )
            ):
                self._set_push_state(iid, "imported_ok", "완료 (import cache)")
                self.log(f"Unreal push 건너뜀 (입력/가져오기 변경 없음): {spm.name}")
                return
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.log(f"Unreal push 시작: {blend.name}")
        self.ui_queue.put(("cell", (iid, "push_status", "push 중...")))
        job_report = LOG_DIR / f"{spm.stem}_push_{stamp}.json"
        manifest_path = LOG_DIR / f"{spm.stem}_rpc_manifest_{stamp}.json"
        checkpoint_path = LOG_DIR / f"{spm.stem}_rpc_checkpoint_{stamp}.json"
        batch_report = LOG_DIR / f"{spm.stem}_rpc_batch_{stamp}.json"
        import_report = LOG_DIR / f"{spm.stem}_rpc_unreal_{stamp}.json"
        export_root = LOG_DIR / "send2ue_rpc_export" / stamp / spm.stem
        send2ue_unreal_py = (
            Path(self.cfg["send2ue_dir"]) / "dependencies" / "unreal.py"
        )
        material_contract = self._push_material_contract(spm)
        cmd = [
            self.cfg["blender_exe"], "--factory-startup", "-b", str(blend),
            "--python", str(TOOL_DIR / "jobs" / "send2ue_push_job.py"), "--",
            "--report", str(job_report),
            "--spm", str(spm),
            "--material-contract", str(material_contract),
            "--transport", "rpc",
            "--manifest", str(manifest_path),
            "--checkpoint", str(checkpoint_path),
            "--batch-report", str(batch_report),
            "--item-import-report", str(import_report),
            "--export-root", str(export_root),
            "--queue-id", iid,
            "--source-fingerprint", source_fingerprint,
            "--unreal-ingest", str(TOOL_DIR / "unreal_ingest.py"),
            "--send2ue-unreal-py", str(send2ue_unreal_py),
            "--max-push-polygons", str(self.cfg.get("push_max_polygons", 2_000_000)),
            "--max-push-bones", str(self.cfg.get("push_max_bones", 1_500)),
            "--rpc-timeout-min", str(self.cfg.get("push_rpc_timeout_min", 180)),
            "--rpc-timeout-max", str(self.cfg.get("push_rpc_timeout_max", 900)),
        ]
        if (
            getattr(self, "_active_push_dependency_map", {}) or {}
        ).get(iid):
            cmd.append("--dependency-orchestrated")
        if repair_evidence is not None:
            cmd.extend([
                "--repair-evidence",
                str(repair_evidence),
            ])
        cmd.extend(send2ue_rpc_cli_args(self.cfg.get("unreal_project")))
        wind_override = self._batch_job_item(iid).get(
            "wind_override", "auto"
        )
        resolved_wind = (
            wind_preset_for_spm(spm)
            if wind_override == "auto"
            else wind_override
        )
        if resolved_wind == "TREE":
            cmd.append("--require-green-signal")
        stage_timeout = max(1, int(
            self.cfg.get("child_stage_inactivity_timeout", 180)
        ))
        disk_export_timeout = max(1, int(
            self.cfg.get("push_job_timeout", 1800)
        ))
        code, log_file = self._run_limited(
            cmd,
            f"{spm.stem}_push_{stamp}.log",
            None,
            inactivity_timeout=disk_export_timeout,
            inactivity_timeout_by_marker=send2ue_inactivity_rules(
                stage_timeout, disk_export_timeout
            ),
        )
        result = load_job_report(job_report)
        if code != 0 or result.get("status") != "ok":
            reason = summarize_job_failure(result, log_file)
            kind = classify_push_failure(result, log_file)
            self.log(f"  [Unreal 실패 원인] {reason} — 상세 로그: {log_file}")
            raise BatchItemError(
                reason,
                kind=kind,
                report=result,
                log_file=log_file,
                report_file=job_report,
            )
        wind_info = result.get("wind")
        wind_ok = "wind ✓" if isinstance(wind_info, dict) and wind_info.get("ok") else "wind -"
        self.ui_queue.put(("cell", (iid, "push_status", f"완료 ({wind_ok})")))
        entry = self.state.setdefault(iid, {})
        entry["push_status"] = f"완료 {datetime.now():%m-%d %H:%M}"
        entry["push_status_kind"] = "imported_ok"
        manifest_fingerprint = result.get("manifest_fingerprint")
        entry["push_import_fingerprint"] = manifest_fingerprint
        if manifest_path.is_file() and manifest_fingerprint:
            entry["push_export_cache"] = {
                "source_fingerprint": source_fingerprint,
                "manifest": str(manifest_path),
                "fingerprint": manifest_fingerprint,
            }
        entry["push_paths"] = {
            "manifest": str(manifest_path),
            "checkpoint": str(checkpoint_path),
            "report": str(job_report),
            "import_report": str(import_report),
            "log": str(log_file),
        }
        entry.pop("push_status_error", None)
        save_state(self.state)
        self._retry_transition(
            iid,
            RETRY_STAGE_POST_CHECK,
            "Unreal RPC post-check complete",
            progress=True,
            heartbeat=True,
        )
        self._retry_transition(
            iid,
            RETRY_STAGE_COMPLETE,
            "Unreal RPC retry complete",
            terminal_reason="completed",
            outcome=RETRY_STAGE_COMPLETE,
        )
        self.log(f"push 완료: {result.get('unreal_folder', '?')}{result.get('unit_name', '')}")


def main():
    root = tk.Tk()
    try:
        ttk.Style().theme_use("vista")
    except tk.TclError:
        pass
    app = App(root)

    def close():
        app.shutdown_shared_queue()
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", close)
    root.mainloop()


if __name__ == "__main__":
    main()
