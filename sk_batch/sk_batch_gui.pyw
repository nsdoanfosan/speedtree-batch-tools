"""SK Vegetation Batch — native SpeedTree Assembly + Unreal Push GUI.

단계 (왼쪽부터, 빠른 것 → 느린 것):
  🔍 검사        : 아무것도 수정하지 않고 Assembly/Push 산출물 상태만 확인.
  ① Blender Assembly : 정확한 native FBX/XML을 import/assembly 후 .blend 저장.
                   파일당 수분~수십분(느림). 이미 최신인 blend는 건너뜀.
  ② Unreal Push : 보내기 전에 준비 검사(blend/JSON 존재, 언리얼 실행 여부)를
                   먼저 전부 통과시킨 뒤에만 실제 push 시작.

모든 무거운 작업은 낮은 우선순위 + CPU 코어 제한이 걸린 백그라운드 프로세스로
실행된다 (자식 SpeedTree CLI에 상속. 헤드리스 Blender는 GPU를 쓰지 않음).
"""
import copy
import ctypes
from ctypes import wintypes
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

from process_lifecycle import owned_run, shutdown_process_supervisor

from code_compile_gate import (
    CODE_REVISION_RESTART_ROUTE,
    CompileGateError,
    production_source_manifest,
    run_gate as run_code_compile_gate,
    validate_production_source_manifest,
    validate_production_source_revision_report,
)
_PROCESS_PRODUCTION_SOURCE_MANIFEST = production_source_manifest(REPO_DIR)
CLUSTER_LIVE_AUDIT_CACHE_KIND = "sk_batch_cluster_live_audit_cache"
CLUSTER_LIVE_AUDIT_CACHE_VERSION = 2


class _WindowsFileBasicInfo(ctypes.Structure):
    _fields_ = (
        ("creation_time", ctypes.c_longlong),
        ("last_access_time", ctypes.c_longlong),
        ("last_write_time", ctypes.c_longlong),
        ("change_time", ctypes.c_longlong),
        ("file_attributes", wintypes.DWORD),
    )


if os.name == "nt":
    _KERNEL32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _CREATE_FILE_W = _KERNEL32.CreateFileW
    _CREATE_FILE_W.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    _CREATE_FILE_W.restype = wintypes.HANDLE
    _GET_FILE_INFORMATION_BY_HANDLE_EX = (
        _KERNEL32.GetFileInformationByHandleEx
    )
    _GET_FILE_INFORMATION_BY_HANDLE_EX.argtypes = (
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    )
    _GET_FILE_INFORMATION_BY_HANDLE_EX.restype = wintypes.BOOL
    _CLOSE_HANDLE = _KERNEL32.CloseHandle
    _CLOSE_HANDLE.argtypes = (wintypes.HANDLE,)
    _CLOSE_HANDLE.restype = wintypes.BOOL


def _windows_file_change_token(path):
    """Return NTFS' non-user-timestamp change counter for a file or folder."""
    if os.name != "nt":
        return None
    handle = _CREATE_FILE_W(
        str(Path(path)),
        0x0080,  # FILE_READ_ATTRIBUTES
        0x00000001 | 0x00000002 | 0x00000004,
        None,
        3,  # OPEN_EXISTING
        0x02000000,  # FILE_FLAG_BACKUP_SEMANTICS (also accepts directories)
        None,
    )
    invalid_handle = ctypes.c_void_p(-1).value
    if handle == invalid_handle:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        info = _WindowsFileBasicInfo()
        if not _GET_FILE_INFORMATION_BY_HANDLE_EX(
            handle,
            0,
            ctypes.byref(info),
            ctypes.sizeof(info),
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        return int(info.change_time)
    finally:
        _CLOSE_HANDLE(handle)


def _fast_file_change_identity(path, stat_result=None):
    """Cheap exact-change identity used only to reuse an existing digest."""
    candidate = Path(path)
    stat_result = stat_result or candidate.stat()
    try:
        change_token = (
            _windows_file_change_token(candidate)
            if os.name == "nt"
            else int(stat_result.st_ctime_ns)
        )
    except OSError:
        change_token = None
    return {
        "size": int(stat_result.st_size),
        "mtime_ns": int(stat_result.st_mtime_ns),
        "device": int(stat_result.st_dev),
        "file_id": int(stat_result.st_ino),
        "change_token": change_token,
    }
from batch_ui_common import CheckedRowController, copy_selected_row_paths
from shared_queue_runtime import SharedQueueRuntime, WaitCancelled
from exact_target_command import (
    build_exact_target_request,
    run_exact_target_request,
)
from repair_orchestration import (
    ATLAS_MANIFEST_MIRROR_REPAIR,
    ATLAS_PRODUCER_REFRESH,
    ATLAS_SLOT_OWNERSHIP_RECONCILE,
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
    STEP3_STANDARD,
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
from material_preflight_cache import (
    load_material_preflight_cache,
    material_preflight_runtime_signature,
    store_material_preflight_cache,
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
    LEGACY_FINGERPRINT_ALGORITHM,
    SAMPLED_FINGERPRINT_ALGORITHM,
    SHA256_ALGORITHM,
    artifact_record_content_key,
    file_content_key_snapshot,
    sampled_file_content_snapshot,
)
from artifact_retention import (
    estimate_output_reservation_bytes,
)

from sk_common import (
    ADDON_ENTRY_DIR,
    LOG_DIR,
    PUSH_ABORT_KINDS,
    PUSH_MANIFEST_SCHEMA_VERSION,
    PUSH_SOURCE_FINGERPRINT_CACHE_VERSION,
    atomic_write_bytes,
    atomic_write_json,
    blend_path_for,
    cached_push_source_fingerprint,
    classify_push_failure,
    close_process_kill_job,
    compact_error_message,
    file_content_snapshot,
    launch_limited,
    load_config,
    load_job_report,
    load_state,
    manifest_item_files_match,
    push_source_cache_matches_snapshot,
    save_config,
    save_state,
    scan_cluster_spm_sources,
    scan_sk_spms,
    send2ue_export_cache_root,
    push_source_snapshot,
    prepare_cluster_spm_pair_for_job,
    summarize_job_failure,
    speedtree_output_spm_for,
    terminate_process_tree,
    unreal_remote_execution_settings,
    normalize_wind_override,
    wind_preset_for_spm,
)
from spm_leaf_handoff_contract import (
    classify_material_export_admission,
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
    partition_push_targets,
)
from failed_retry_eligibility import (
    BLENDER_EXPORT_RETRY_FAILURE_KINDS,
    BLENDER_REBUILD,
    CURRENT_BLENDER_EXCLUDED,
    PENDING_UNREAL_VALIDATION,
    RETRY_ELIGIBILITY_SCHEMA_VERSION,
    SEND2UE_REEXPORT,
    UNREAL_ONLY,
    UNREAL_PARENT_ABSENT,
    UNREAL_PARENT_CANDIDATE,
    UNREAL_PARENT_CURRENT,
    UNREAL_PARENT_DEPENDENCY_REBUILD,
    UNREAL_PARENT_EXPORT_STALE,
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
    manifest_item_has_current_skeleton_root_export,
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
from assembly_runtime_contract import (
    ASSEMBLY_RUNTIME_RECEIPT_VERSION,
    addon_dir_from_config,
    assembly_runtime_code_paths,
    assembly_runtime_code_state,
    assembly_runtime_receipt_path,
    write_assembly_runtime_receipt,
)
from blender_resume_receipt import (
    BlenderResumeReceiptError,
    build_blender_resume_receipt,
    validate_blender_resume_receipt,
)
WIND_OPTIONS = (
    ("자동 (식생 종류 기준)", "auto"),
    ("TREE", "TREE"),
    ("BUSH", "BUSH"),
    ("WEED", "WEED"),
    ("NONE", "NONE"),
)
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
BLENDER_RESUME_CONFIG_KEYS = (
    "fbx_ini",
    "rename_materials",
    "cluster_unit_probe",
    "cluster_capture_resolution",
)
CLUSTER_RELATION_LOCKS_GUARD = threading.Lock()
_ASSEMBLY_REPORT_READ_LOCAL = threading.local()
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

_DURABLE_FAILURE_REPORT_KEYS = (
    "status",
    "error",
    "error_type",
    "traceback",
    "reason_token",
    "evidence",
    "issues",
    "repair_disposition",
    "reason_ko",
    "action_ko",
    "stage",
    "stage_timings_seconds",
    "pipeline_report",
    "unreal_push_ready",
    "final_handoff_status",
)


def compact_durable_failure_report(report):
    """Persist the failure decision, never a second copy of artifact payloads."""

    if not isinstance(report, dict):
        return {}
    return {
        key: copy.deepcopy(report[key])
        for key in _DURABLE_FAILURE_REPORT_KEYS
        if key in report
    }


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


def manifest_item_requires_unreal_asset_verification(item):
    """Return True when a cache hit still has a live Unreal asset contract."""
    assembly = (item or {}).get("cluster_assembly") or {}
    plan = assembly.get("ingest_plan") or {}
    return (
        plan.get("status") == "ready"
        and isinstance(plan.get("asset_contract"), dict)
        and bool(plan.get("asset_contract"))
    )


def assembly_pipeline_report_path(spm):
    spm = Path(spm)
    return (
        spm.parent / "reports" /
        f"{spm.stem}_speedtree_assembly_pipeline_report_codex.json"
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
        report = _read_assembly_pipeline_json(
            assembly_pipeline_report_path(spm)
        )
    except (OSError, ValueError):
        return True
    return not is_actionable_cluster_assembly_manifest(
        report.get("cluster_assembly_manifest")
    )


@contextmanager
def assembly_report_read_scope():
    """Reuse one large Assembly JSON only within one semantic status decision."""
    previous = getattr(_ASSEMBLY_REPORT_READ_LOCAL, "cache", None)
    _ASSEMBLY_REPORT_READ_LOCAL.cache = {}
    try:
        yield
    finally:
        if previous is None:
            try:
                del _ASSEMBLY_REPORT_READ_LOCAL.cache
            except AttributeError:
                pass
        else:
            _ASSEMBLY_REPORT_READ_LOCAL.cache = previous


def _assembly_report_stat_key(path):
    path = Path(path)
    stat = path.stat()
    return (
        os.path.normcase(os.path.abspath(str(path))),
        stat.st_size,
        stat.st_mtime_ns,
    )


def _read_assembly_pipeline_json(path):
    """Read a report once per scope without retaining multi-MB data globally."""
    path = Path(path)
    cache = getattr(_ASSEMBLY_REPORT_READ_LOCAL, "cache", None)
    key = _assembly_report_stat_key(path)
    if cache is not None and key in cache:
        return cache[key]
    payload = json.loads(path.read_text(encoding="utf-8"))
    if cache is not None:
        # A status decision concerns one SPM. Keep this cache explicitly
        # bounded in case a future nested helper consults a second report.
        cache.clear()
        cache[key] = payload
    return payload


def _cache_written_assembly_pipeline_json(path, payload):
    cache = getattr(_ASSEMBLY_REPORT_READ_LOCAL, "cache", None)
    if cache is None:
        return
    cache.clear()
    cache[_assembly_report_stat_key(path)] = payload


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
    """Prove that a cached isolated-bark source was actually consumed by Assembly."""
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
    """Return True until Assembly has captured this exact isolated bark bundle."""
    if not isinstance(resolution, dict) or resolution.get("status") not in {
        "prepared",
        "cached",
    }:
        return False
    try:
        from cluster_assembly_builder import file_fingerprint

        pipeline = load_current_assembly_pipeline_report(spm)
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


def load_current_assembly_pipeline_report(spm, *, migrate_legacy=True):
    """Load a content-current Assembly report, upgrading proven legacy data.

    Legacy reports can be upgraded without Blender only when they already
    contain the exact live SPM content identity and were committed after the
    saved blend.  A missing identity, changed SPM, or blend newer than the
    report remains stale; rebuilding a contract from current inputs must never
    bless an unrelated old blend.
    """
    canonical_spm = speedtree_output_spm_for(spm)
    report_path = assembly_pipeline_report_path(spm)
    try:
        report = _read_assembly_pipeline_json(report_path)
    except (OSError, ValueError) as exc:
        raise ValueError(f"Assembly report could not be read: {exc}") from exc
    if not isinstance(report, dict):
        raise ValueError("Assembly report is not an object")

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
            "Assembly report used an isolated bark-normalized source but has "
            "no exact material handoff contract; run Blender Assembly again"
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
            "legacy Assembly report has no completed handoff state"
        )
    recorded_identity = (
        (report.get("speedtree_live_source_identity") or {}).get("spm")
    )
    current_identity = source_identity(canonical_spm)
    if not _same_content_identity(recorded_identity, current_identity):
        raise ValueError(
            "legacy Assembly report source identity is missing or stale"
        )
    blend = blend_path_for(spm)
    try:
        if (
            not blend.is_file()
            or report_path.stat().st_mtime_ns < blend.stat().st_mtime_ns
        ):
            raise ValueError(
                "legacy Assembly report predates the saved blend"
            )
    except OSError as exc:
        raise ValueError(
            f"legacy Assembly artifact timestamp could not be read: {exc}"
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
    _cache_written_assembly_pipeline_json(report_path, migrated)
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


_LEGACY_NONBLOCKING_NORMALIZATION_CODES = frozenset({
    "NORMALIZED_VARIANTS_REQUIRED",
    "NORMALIZED_VARIANTS_STALE",
})

# POLICY: these tokens exist only in old saved receipts.  A missing or stale
# optional normalized variant is not a delivery failure, must never schedule a
# provider refresh, and must never participate in target admission.  Current
# producers no longer emit these rows; this quarantine remains solely so an
# old receipt cannot resurrect the deleted gate after an application restart.


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
        # This legacy-named metadata may only narrow an already-authorized
        # stale-node-table recovery scope.  It never initiates normalization,
        # repair, admission, or failure by itself.
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
        diagnostic_targets = [
            str(Path(value).expanduser().absolute())
            for value in target_spms
        ]
        return {
            # An unreadable registry cannot select a writer.  It removes
            # mutation authority, but it is not evidence that the current
            # live target is unsafe to export.
            "current": True,
            "reason": f"target_registry_invalid: {exc}",
            "targets": [],
            "actionable_targets": [],
            "metadata_diagnostic_targets": diagnostic_targets,
            "mutation_authorized": False,
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
    diagnostic_stale = []
    actionable_stale = []
    for row in stale:
        resolution = row.get("atlas_manifest_resolution") or {}
        # Every refresh reason on this row was derived from the selected
        # manifest payload.  When resolver ownership is diagnostic-only, no
        # such reason may pick that Provider for a mutation -- including
        # physical-capture/source-FBX drift.  Independent live content audit,
        # not disputed metadata, decides whether export can continue.
        if resolution.get("mutation_authorized") is False:
            diagnostic_stale.append(row)
        else:
            actionable_stale.append(row)
    return {
        "current": not actionable_stale,
        "reason": "; ".join(
            f"{Path(row['target_spm']).name}:{row.get('status')}"
            + (
                f"({','.join(row.get('refresh_reasons') or ())})"
                if row.get("refresh_reasons")
                else ""
            )
            for row in actionable_stale
        ),
        "targets": rows,
        "actionable_targets": [
            row["target_spm"] for row in actionable_stale
        ],
        "metadata_diagnostic_targets": [
            row["target_spm"] for row in diagnostic_stale
        ],
        "mutation_authorized": not diagnostic_stale,
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


def blender_assembly_schedule_waves(targets):
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


def expand_blender_assembly_targets(selected_targets, all_items):
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


def selected_row_detail_text(spm, statuses):
    return "\n".join(
        (
            f"경로  {spm}",
            f"Source  {statuses.get('spm_status', '-')}",
            f"① Blender  {statuses.get('blend_status', '-')}",
            f"② Unreal  {statuses.get('push_status', '-')}",
        )
    )


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


class CodeRevisionRestartRequired(RuntimeError):
    """Structured evidence for a non-blocking code-revision warning."""

    route = CODE_REVISION_RESTART_ROUTE

    def __init__(
        self,
        compile_error,
        *,
        context,
        report=None,
        log_file=None,
        report_file=None,
    ):
        details = copy.deepcopy(
            getattr(compile_error, "details", {}) or {}
        )
        details.update({
            "route": CODE_REVISION_RESTART_ROUTE,
            "status": CODE_REVISION_RESTART_ROUTE,
            "context": str(context),
        })
        message = (
            f"{context}: {compile_error}\n\n"
            "정확한 변경 경로는 위에 기록되며 작업은 현재 production "
            "source 기준으로 계속됩니다. 이 상태는 자산 실패가 아닙니다."
        )
        details["message"] = message
        super().__init__(message)
        self.details = details
        self.report = copy.deepcopy(report or {})
        self.log_file = log_file
        self.report_file = report_file

    def as_dict(self):
        return copy.deepcopy(self.details)


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
        self._active_process_cleanup_lock = threading.Lock()
        self._active_process_cleanup_worker = None
        self._shutdown_callbacks = []
        self._shutdown_poll_scheduled = False
        self._shutdown_complete = False
        self.state_lock = threading.RLock()  # guards self.state writes across worker threads
        self._reset_cluster_receipt_refresh_memo()
        self._scan_generation = 0
        self.scan_worker = None
        self._live_poll_active = False
        self._live_poll_after_id = None
        root.title("SK Vegetation Batch — 검사 → Assembly → Unreal")
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
        ttk.Label(transport_opts, text="② Unreal Push:").pack(side="left")
        self.transport_var = tk.StringVar(
            value=self.cfg.get("push_transport", "headless")
        )
        transport_combo = ttk.Combobox(
            transport_opts,
            textvariable=self.transport_var,
            values=("rpc", "headless", "unreal_wait"),
            width=14,
            state="readonly",
        )
        transport_combo.pack(side="left", padx=6)
        Tooltip(
            transport_combo,
            "rpc = 기존 열린 Unreal Editor 경로\n"
            "headless = Blender 전체 export 후 UnrealEditor-Cmd 1회 배치 import\n"
            "unreal_wait = export만 완료하고 영구 Unreal 대기 큐에 등록",
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
            "단, Unreal Wait를 선택하면 전체 자동에서도 이를 덮어쓰지 않고 "
            "영구 대기 상태로 남깁니다. 단독 ② Push도 왼쪽 transport 선택을 따릅니다.",
        )
        ttk.Label(transport_opts, text="① Assembly·② export 동시:").pack(
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
            "Blender Assembly와 headless Send2UE export를 동시에 처리할 개수입니다. "
            "기본값 2는 시작 비용을 겹치되 메모리 사용량을 제한합니다.",
        )

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
        tip5 = ("백그라운드 작업이 사용할 수 있는 CPU 코어 수 제한입니다. "
                "동시 실행 중에도 각 자식 프로세스가 이 제한을 지킵니다.")
        Tooltip(lbl5, tip5); Tooltip(spin5, tip5)

        self.force_var = tk.BooleanVar(value=False)
        chk = ttk.Checkbutton(
            opts,
            text="최신 .blend/캐시가 있어도 강제로 다시 실행",
            variable=self.force_var,
        )
        chk.pack(side="left", padx=12)
        Tooltip(chk, ("① Blender Assembly에서, 이미 정확한 현재 산출물이 있는 항목은 기본적으로 "
                      "건너뜁니다. 이 옵션을 켜면 Assembly를 강제로 다시 실행합니다.\n"
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
                                 "native FBX/XML / 머티리얼 / blend 최신 여부 / 핸드오프 JSON "
                                 "준비 여부를 빠르게 확인해서 표에 채웁니다.\n"
                                 "본 계층은 고쳐진 native exporter가 FBX/XML에 함께 기록하므로 "
                                 "검사 단계에서 SPM을 다시 파싱하지 않습니다. "
                                 "검사 결과는 파일이나 실행 캐시에 저장하지 않습니다.\n"
                                 "오래 걸리는 ①②를 돌리기 전에 먼저 눌러보세요."))
        self.btn_blender = ttk.Button(actions, text="① Blender Assembly",
                                      command=lambda: self.start_batch("blender"))
        self.btn_blender.pack(side="left", padx=6)
        Tooltip(self.btn_blender, ("헤드리스 Blender로 정확한 SpeedTree FBX/XML을 export/import한 뒤 "
                                   "재질·메시·Export 구조를 Assembly하고 "
                                   "SPM 옆에 같은 이름의 .blend와 wind JSON을 저장합니다.\n"
                                   "완료 전에 T_ 6종 또는 보존 Cluster 텍스처, 빈 머티리얼 슬롯까지 검사합니다.\n"
                                   "파일당 수분~수십분. 이미 최신인 blend는 건너뜁니다("
                                   "'완료된 항목도 다시 실행'으로 강제 가능)."))
        self.btn_push = ttk.Button(actions, text="② Unreal Push",
                                   command=lambda: self.start_batch("push"))
        self.btn_push.pack(side="left", padx=6)
        Tooltip(self.btn_push, ("① Blender Assembly를 먼저 실행한 뒤 "
                                "② Unreal Push를 실행합니다.\n"
                                "push 전에 준비 검사를 먼저 전부 통과시킵니다:\n"
                                "· .blend 존재 + SPM보다 최신인지\n"
                                "· 텍스처 세트와 Assembly 사전검사(빈 머티리얼 슬롯 포함)\n"
                                "· wind JSON(핸드오프 산출물) 존재\n"
                                "· 언리얼 에디터 실행 여부\n"
                                "준비 안 된 항목은 이유를 표시하고 건너뛴 뒤, 준비된 것만 push합니다."))
        self.btn_waiting_import = ttk.Button(
            actions,
            text="대기 에셋 임포트",
            command=self.start_waiting_asset_import,
        )
        self.btn_waiting_import.pack(side="left", padx=(2, 6))
        Tooltip(
            self.btn_waiting_import,
            "unreal_wait로 export가 끝난 에셋을 영구 상태에서 다시 모아 "
            "UnrealEditor-Cmd headless 한 세션으로 임포트합니다.\n"
            "MyProject2 Unreal Editor가 완전히 꺼진 상태에서만 시작합니다.",
        )
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
            "· 자동 복구 reason code: exact PCG 텍스처 또는 Generator/Cluster를 "
            "자동 복구하고 fresh 재검증 후 Blender/Unreal 재시도\n"
            "· Blender/Send2UE export 실패: ① Blender부터 export를 다시 만들고 "
            "② Unreal Push까지 실행\n"
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
            text="🌙 목록 전체 자동 ①→②",
            command=self.start_full_pipeline,
        )
        self.btn_all.pack(side="left", padx=(10, 4))
        Tooltip(
            self.btn_all,
            "체크 상태와 무관하게 현재 목록의 모든 항목을 밤새 순서대로 처리합니다.\n"
            "① Blender Assembly 전체 완료 → ② Unreal Push 순서입니다.\n"
            "개별 ①/② 버튼은 체크된 항목만 대상으로 합니다.\n"
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

        cols = ("wind", "spm_status", "blend_status", "push_status", "folder")
        visible_cols = ("wind", "spm_status", "blend_status", "push_status")
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
            "wind": ("Wind (▼)", 110),
            "spm_status": ("Source", 205),
            "blend_status": ("① Blender", 245),
            "push_status": ("② Unreal", 190),
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
            text = "행을 선택하면 전체 경로와 Source/①/② 상태가 여기에 표시됩니다."
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
    @classmethod
    def _collect_scan_result(cls, root, _snapshot_caches=None):
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
        # Persist the bounded relationship inventory once. Native FBX/XML owns
        # the skeleton contract, so list discovery never hashes every SPM.
        save_leaf_contract_cache()
        return {
            "spms": spms,
            "cluster_sources": cluster_sources,
        }

    @staticmethod
    def _quick_blend_status_text(spm):
        """Paint a non-running placeholder; live validation follows."""
        blend = blend_path_for(spm)
        if not blend.exists():
            return "생성 필요 — blend 없음 → ① Blender Assembly"
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
            "SpeedTree FBX/XML 준비 중",
            "blender assembly 중",
        ))

    def _set_scan_controls(self, scanning):
        state = "disabled" if scanning else "normal"
        for name in (
            "btn_check", "btn_blender", "btn_push",
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
            # An explicit scan starts a new asset verification session.
            # Queue boundaries do not: the memo itself re-fingerprints all
            # inputs and live artifacts before any reuse.
            self._reset_cluster_receipt_refresh_memo()
            root = self.root_var.get()
            self.cfg = self._collect_cfg()
            self.cfg["root"] = root
            save_config(self.cfg)

            if hasattr(self, "root") and hasattr(self.root, "after"):
                self._set_scan_controls(True)
                if hasattr(self, "progress_var"):
                    self.progress_var.set("SK SPM 스캔 중…")

                def worker():
                    try:
                        result = self._collect_scan_result(root)
                        error = None
                    except Exception as exc:
                        result, error = None, exc
                    self.ui_queue.put((
                        "scan_done", (generation, result, error)
                    ))

                self.scan_worker = threading.Thread(target=worker, daemon=True)
                self.scan_worker.start()
                return
            prepared = self._collect_scan_result(root)
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
            saved_wind_override = entry.get("wind_override", "auto")
            wind_override = normalize_wind_override(saved_wind_override)
            if wind_override != saved_wind_override:
                entry["wind_override"] = wind_override
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
                "live_texture_paths": cached_texture_paths,
                "live_status_signature": cached_live_signature,
                "blend_resume_receipt": copy.deepcopy(
                    entry.get("blend_resume_receipt")
                ),
            }
            spm_status = (
                f"Output 규격 · {cluster_source['pair_status']}"
                if cluster_source
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
                    self._wind_label(iid),
                    self._table_display_value(iid, "spm_status", spm_status),
                    self._table_display_value(iid, "blend_status", blend_status),
                    self._table_display_value(iid, "push_status", push_status),
                    str(spm.parent),
                ),
            )
            self.row_copy_paths[iid] = [spm]
        self.checked_rows.sync_after_reload()
        save_state(self.state)
        self.log(
            f"스캔 완료: SK SPM {len(spms)}개. "
            "먼저 [🔍 검사]로 상태를 확인해보세요."
        )

        if hasattr(self, "progress_var"):
            self.progress_var.set(
                f"스캔 완료 · SPM {len(spms)}개"
            )
        if hasattr(self, "root") and hasattr(self.root, "after"):
            # Do not let ①→② start while the first live status audit is
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
            f"{Path(spm).stem}_speedtree_assembly_pipeline_report_codex.json"
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
            f"{spm.stem}_speedtree_assembly_pipeline_report_codex.json"
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

    def _blender_resume_settings(self, iid, item=None):
        """Return only settings that can change durable Assembly output."""

        if item is None:
            item = self._batch_job_item(iid)
        return {
            "wind_override": normalize_wind_override(
                item.get("wind_override", "auto")
            ),
            "config": {
                key: self.cfg.get(key)
                for key in BLENDER_RESUME_CONFIG_KEYS
            },
        }

    def _blender_resume_tracked_paths(
        self,
        spm,
        repair_state,
        texture_paths=None,
    ):
        """Bind the files consulted by the successful live Assembly audit."""

        spm = Path(spm)
        speedtree_spm = speedtree_output_spm_for(spm)
        report_path = assembly_pipeline_report_path(spm)
        wind_path = (
            blend_path_for(spm).parent
            / "JSON"
            / f"{spm.stem}_dynamic_wind_import_from_megaplant_groups.json"
        )
        required = [
            spm,
            speedtree_spm,
            blend_path_for(spm),
            report_path,
            wind_path,
        ]
        for path in required:
            if not Path(path).is_file():
                raise BlenderResumeReceiptError(
                    f"current Assembly result is missing a required file: {path}"
                )

        optional = []
        optional.append(speedtree_stmat_path(speedtree_spm))
        if texture_paths is None:
            texture_paths = self._reported_texture_paths(spm)
        optional.extend(
            Path(path) for path in texture_paths
        )
        return required + optional

    def _blender_resume_relation_inventory(self, spm, item=None):
        """Describe registered relations from the already-scanned table.

        This path deliberately reads no SPM/Blend contents and resolves no
        global receipt cache.  The scan has already discovered provider use;
        only small target registries are consulted here.
        """

        spm = Path(spm).expanduser().absolute()
        cluster_source = is_cluster_source_spm(spm)
        owner = spm.parent.parent if cluster_source else spm.parent
        owner_has_cluster = (owner / "Cluster").is_dir()
        if cluster_source:
            candidates = [dict(item or {}, spm=spm)]
            requested_target = None
        elif owner_has_cluster:
            table = getattr(self, "items", None)
            if not isinstance(table, dict):
                return None
            candidates = list(table.values())
            requested_target = normalized_folder_key(spm)
        else:
            return []

        rows = []
        for candidate in candidates:
            if not isinstance(candidate, dict) or not candidate.get("spm"):
                continue
            provider = Path(candidate["spm"]).expanduser().absolute()
            if (
                not is_cluster_source_spm(provider)
                or normalized_folder_key(provider.parent.parent)
                != normalized_folder_key(owner)
            ):
                continue
            try:
                targets = cluster_relation_output_targets(
                    provider,
                    candidate.get("referenced_by_spms") or (),
                )
            except (OSError, RuntimeError, TypeError, ValueError):
                return None
            target_keys = sorted(
                normalized_folder_key(target) for target in targets
            )
            if requested_target and requested_target not in target_keys:
                continue
            authoring = candidate.get("authoring_spm")
            rows.append({
                "provider_spm": str(provider),
                "provider_authoring_spm": (
                    str(Path(authoring).expanduser().absolute())
                    if authoring
                    else ""
                ),
                "provider_blend": str(blend_path_for(provider)),
                "target_registry": str(
                    blend_path_for(provider).with_suffix(
                        ".atlas_leaf_targets.json"
                    )
                ),
                "registered_targets": target_keys,
            })
        rows.sort(key=lambda row: normalized_folder_key(row["provider_spm"]))
        return rows

    def _blender_resume_relation_signature(
        self,
        spm,
        item=None,
        *,
        relation_inventory=None,
    ):
        """Return the stat-backed relation index for fast skip eligibility."""

        spm = Path(spm).expanduser().absolute()
        if relation_inventory is None:
            relation_inventory = self._blender_resume_relation_inventory(
                spm,
                item=item,
            )
        if relation_inventory is None:
            return {"status": "unresolved"}
        return {
            "status": (
                "tracked"
                if (
                    is_cluster_source_spm(spm)
                    or (spm.parent / "Cluster").is_dir()
                )
                else "not_applicable"
            ),
            "relations": copy.deepcopy(relation_inventory),
        }

    @staticmethod
    def _blender_resume_relation_paths(
        spm,
        repair_state,
        *,
        relation_inventory=(),
    ):
        """Files whose drift requires relation maintenance, not a failure."""

        paths = []
        for row in relation_inventory or ():
            if not isinstance(row, dict):
                continue
            paths.extend(
                Path(row[key])
                for key in (
                    "provider_spm",
                    "provider_authoring_spm",
                    "provider_blend",
                    "target_registry",
                )
                if row.get(key)
            )
        dependency_contract = repair_state.get("push_dependency_contract")
        if isinstance(dependency_contract, dict):
            paths.extend(
                Path(path)
                for path in dependency_contract.get("dependency_spms") or ()
            )
        return paths

    def _build_blender_resume_receipt(
        self,
        iid,
        spm,
        repair_state,
        *,
        texture_paths=None,
        item=None,
        relation_validated=False,
    ):
        spm = Path(spm)
        if item is None:
            try:
                item = self._batch_job_item(iid)
            except (KeyError, TypeError):
                # Detached status probes may not have a current table row.
                # They may omit the optimization; they must not turn receipt
                # creation into a status failure.
                item = {}
        speedtree_spm = speedtree_output_spm_for(spm)
        relation_inventory = self._blender_resume_relation_inventory(
            spm,
            item=item,
        )
        relation_signature = self._blender_resume_relation_signature(
            spm,
            item=item,
            relation_inventory=relation_inventory,
        )
        relation_sensitive = bool(
            is_cluster_source_spm(spm)
            or (speedtree_spm.parent / "Cluster").is_dir()
        )
        if (
            relation_signature.get("status") == "unresolved"
            or (relation_sensitive and not relation_validated)
        ):
            raise BlenderResumeReceiptError(
                "Cluster relationship has not been live-validated",
                resume_action="relation_changed",
            )
        return build_blender_resume_receipt(
            spm,
            tracked_paths=self._blender_resume_tracked_paths(
                spm,
                repair_state,
                texture_paths=texture_paths,
            ),
            relation_paths=self._blender_resume_relation_paths(
                spm,
                repair_state,
                relation_inventory=relation_inventory,
            ),
            relation_signature=relation_signature,
            settings=self._blender_resume_settings(iid, item=item),
            assembly_state=repair_state,
        )

    def _validated_blender_resume_state(self, iid, spm, item, receipt):
        relation_inventory = self._blender_resume_relation_inventory(
            spm,
            item=item,
        )
        return validate_blender_resume_receipt(
            receipt,
            spm,
            settings=self._blender_resume_settings(iid, item=item),
            relation_signature=self._blender_resume_relation_signature(
                spm,
                item=item,
                relation_inventory=relation_inventory,
            ),
        )

    def _migrate_blender_resume_from_stat_state(self, iid, spm, item):
        """Seal an existing current row without opening SPM/Blend contents."""

        with self.state_lock:
            entry = copy.deepcopy(self.state.get(iid) or {})
        status_text = str(entry.get("blend_status") or "")
        status_kind = str(entry.get("blend_status_kind") or "")
        if (
            status_kind not in {"", "ok"}
            or not (
                status_text.startswith("최신")
                or status_text.startswith("Blend 완료")
            )
        ):
            return {"policy": "live_validation"}

        spm = Path(spm).expanduser().absolute()
        speedtree_spm = speedtree_output_spm_for(spm)
        blend = blend_path_for(spm)
        report = assembly_pipeline_report_path(spm)
        wind = (
            blend.parent
            / "JSON"
            / f"{spm.stem}_dynamic_wind_import_from_megaplant_groups.json"
        )
        required = (spm, speedtree_spm, blend, report, wind)
        try:
            stats = {path: Path(path).stat() for path in required}
        except OSError:
            return {"policy": "rebuild"}
        newest_source_mtime = max(
            stats[spm].st_mtime_ns,
            stats[speedtree_spm].st_mtime_ns,
        )
        if newest_source_mtime > stats[blend].st_mtime_ns:
            return {"policy": "rebuild"}

        relation_inventory = self._blender_resume_relation_inventory(
            spm,
            item=item,
        )
        if relation_inventory is None:
            return {"policy": "live_validation"}
        relation_paths = self._blender_resume_relation_paths(
            spm,
            {},
            relation_inventory=relation_inventory,
        )
        for path in relation_paths:
            try:
                if Path(path).stat().st_mtime_ns > stats[blend].st_mtime_ns:
                    return {"policy": "live_validation"}
            except FileNotFoundError:
                # Missing registry sidecars are an explicit OFF/pass-through
                # state and are sealed as missing in the receipt.
                if str(path).casefold().endswith(
                    ".atlas_leaf_targets.json"
                ):
                    continue
                return {"policy": "live_validation"}
            except OSError:
                return {"policy": "live_validation"}

        push_ready = "Push 차단" not in status_text
        repair_state = {
            "current": True,
            "push_ready": push_ready,
            "kind": "ready" if push_ready else "source_review",
            "reason": (
                "준비됨 ✓"
                if push_ready
                else "원본/재질 검토 필요 — Unreal Push 차단"
            ),
        }
        try:
            receipt = self._build_blender_resume_receipt(
                iid,
                spm,
                repair_state,
                texture_paths=tuple(entry.get("live_texture_paths") or ()),
                item=item,
                relation_validated=True,
            )
        except (
            BlenderResumeReceiptError,
            OSError,
            TypeError,
            ValueError,
        ):
            return {"policy": "live_validation"}
        return {
            "policy": "skip",
            "assembly_state": repair_state,
            "receipt": receipt,
        }

    def _prefilter_blender_resume_targets(self, targets):
        """Exclude unchanged completed rows before workers and waves exist."""

        targets = list(targets)
        if getattr(self, "force_rerun", False):
            return targets, []
        runnable = []
        skipped = []
        for item in targets:
            item.pop("_blender_resume_policy", None)
            spm = Path(item["spm"])
            iid = str(spm)
            with self.state_lock:
                receipt = copy.deepcopy(
                    (self.state.get(iid) or {}).get(
                        "blend_resume_receipt"
                    )
                )
            try:
                repair_state = self._validated_blender_resume_state(
                    iid,
                    spm,
                    item,
                    receipt,
                )
            except BlenderResumeReceiptError as exc:
                if exc.resume_action == "missing":
                    migration = self._migrate_blender_resume_from_stat_state(
                        iid,
                        spm,
                        item,
                    )
                    if migration.get("policy") == "skip":
                        if self._publish_current_assembly_skip(
                            iid,
                            spm,
                            migration["assembly_state"],
                            validated_resume_receipt=migration["receipt"],
                        ):
                            skipped.append(item)
                            continue
                    elif migration.get("policy") == "rebuild":
                        item["_blender_resume_policy"] = "rebuild"
                        runnable.append(item)
                        continue
                item["_blender_resume_policy"] = (
                    "rebuild"
                    if exc.resume_action in {
                        "rebuild_required",
                        "wrong_target",
                    }
                    else "live_validation"
                )
                runnable.append(item)
                continue
            except (OSError, TypeError, ValueError):
                item["_blender_resume_policy"] = "live_validation"
                runnable.append(item)
                continue
            assembly_inputs_current, assembly_reason = (
                self._cluster_assembly_inputs_current(spm)
            )
            if not assembly_inputs_current:
                item["_blender_resume_policy"] = "rebuild"
                item["_blender_resume_reason"] = assembly_reason
                runnable.append(item)
                continue
            if not self._publish_current_assembly_skip(
                iid,
                spm,
                repair_state,
                validated_resume_receipt=receipt,
            ):
                runnable.append(item)
                continue
            skipped.append(item)
        if skipped:
            self.log(
                "Blender Assembly 재개 영수증: 완료 항목 "
                f"{len(skipped)}개를 실행 대기열 전에 건너뜀"
            )
        return runnable, skipped

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
                    copy.deepcopy(item.get("blend_resume_receipt")),
                    {
                        "wind_override": item.get("wind_override", "auto"),
                    },
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
                legacy_snapshot = len(row) == 4
                if legacy_snapshot:
                    iid, spm, texture_paths, previous_signature = row
                    resume_receipt = None
                    resume_item = {}
                else:
                    (
                        iid,
                        spm,
                        texture_paths,
                        previous_signature,
                        resume_receipt,
                        resume_item,
                ) = row
                try:
                    signature = self._live_status_signature(spm, texture_paths)
                    if signature == previous_signature:
                        # Unchanged rows need no SPM/material audit. A missing
                        # fast receipt is migrated from this saved status and
                        # current stat identity only when Blender is run.
                        return None
                    # A changed report can point at a new set of
                    # T_/Cluster files.
                    texture_paths = self._reported_texture_paths(spm)
                    signature = self._live_status_signature(
                        spm, texture_paths
                    )
                    repair_state = None
                    if legacy_snapshot:
                        status = self._blend_status_text(spm)
                    else:
                        repair_state = self._assembly_output_state(spm)
                        status = self._blend_status_from_assembly_state(
                            repair_state
                        )
                    push_status = self._current_push_status_text(iid, spm)
                    resume_receipt = None
                    if (
                        isinstance(repair_state, dict)
                        and repair_state.get("current") is True
                    ):
                        try:
                            resume_receipt = (
                                self._build_blender_resume_receipt(
                                    iid,
                                    spm,
                                    repair_state,
                                    texture_paths=texture_paths,
                                    item=resume_item,
                                )
                            )
                        except (
                            BlenderResumeReceiptError,
                            OSError,
                            TypeError,
                            ValueError,
                        ):
                            # A receipt is an optimization. Keep the live
                            # status authoritative and let the queue fall
                            # back to its ordinary validation path.
                            resume_receipt = None
                    return (
                        iid,
                        texture_paths,
                        signature,
                        status,
                        push_status,
                        resume_receipt,
                        "",
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
                        None,
                        message,
                    )

            rows = []
            if snapshot:
                # Four workers keep the large embedded-geometry SPMs bounded
                # in memory while gzip/regex parsing proceeds in parallel.
                with ThreadPoolExecutor(max_workers=min(4, len(snapshot))) as pool:
                    rows = [row for row in pool.map(inspect_row, snapshot) if row]
            for (
                iid,
                texture_paths,
                signature,
                status,
                push_status,
                resume_receipt,
                row_error,
            ) in rows:
                with self.state_lock:
                    if generation != self._scan_generation or iid not in self.items:
                        continue
                    item = self.items[iid]
                    item["live_texture_paths"] = texture_paths
                    item["live_status_signature"] = signature
                    item["blend_resume_receipt"] = copy.deepcopy(
                        resume_receipt
                    )
                    state_entry = self.state.setdefault(iid, {})
                    state_entry["blend_status"] = status
                    state_entry["live_texture_paths"] = list(texture_paths)
                    state_entry["live_status_signature"] = signature
                    if resume_receipt is not None:
                        state_entry["blend_resume_receipt"] = copy.deepcopy(
                            resume_receipt
                        )
                        state_entry["blend_status_kind"] = "ok"
                        state_entry.pop("blend_status_error", None)
                        state_entry.pop("blend_status_result", None)
                    else:
                        state_entry.pop("blend_resume_receipt", None)
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
        name = item.get("display_name") or item["spm"].name
        return f"{mark} {name}"

    def _redraw_checked_row(self, iid, _item):
        self.tree.item(iid, text=self._item_label(iid))

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
            cfg["blender_parallel_jobs"] = max(
                1, min(4, int(self.blender_parallel_var.get()))
            )
            transport = self.transport_var.get()
            cfg["push_transport"] = (
                transport
                if transport in {"rpc", "headless", "unreal_wait"}
                else "headless"
            )
            cfg["night_headless"] = bool(self.night_headless_var.get())
        except (AttributeError, tk.TclError):
            pass
        return cfg

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
            "btn_check", "btn_blender", "btn_push",
            "btn_waiting_import",
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
        self.force_rerun = job["force_rerun"]
        # Retrying a failed stage does not grant full-rebuild semantics.
        # Only the operator's explicit force checkbox bypasses reusable
        # upstream artifacts such as the material preflight receipt.
        self.force_full_rebuild = bool(
            job.get("force_full_rebuild", self.force_rerun)
        )
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
        self._mark_previous_cancellations_pending(job)
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

    def _mark_previous_cancellations_pending(self, job):
        """Do not present a previous stop as the state of a new active job."""

        state = getattr(self, "state", None)
        state_lock = getattr(self, "state_lock", None)
        ui_queue = getattr(self, "ui_queue", None)
        if not isinstance(state, dict) or state_lock is None:
            return

        if str(job.get("mode") or "") == "pipeline":
            columns = {
                "blender": ("spm_status", "blend_status"),
                "push": STATUS_COLUMNS,
            }.get(str(job.get("terminal_phase") or "push"), STATUS_COLUMNS)
        else:
            columns = {
                "check": ("spm_status",),
                "blender": ("blend_status",),
                "push": ("push_status",),
            }.get(str(job.get("phase") or ""), STATUS_COLUMNS)

        changed = []
        with state_lock:
            for item in job.get("targets") or ():
                iid = str(item.get("spm") or "")
                entry = state.get(iid)
                if not iid or not isinstance(entry, dict):
                    continue
                for column in columns:
                    kind = str(entry.get(f"{column}_kind") or "").casefold()
                    if kind not in {"cancelled", "stopped"}:
                        continue
                    entry[column] = "재실행 대기"
                    entry[f"{column}_kind"] = "rerun_pending"
                    changed.append((iid, column))
            if changed:
                save_state(state)

        if ui_queue is not None:
            for iid, column in changed:
                ui_queue.put(("cell", (iid, column, "재실행 대기")))

    def _production_source_revision_precheck(self):
        try:
            current = production_source_manifest(REPO_DIR)
            validate_production_source_manifest(
                _PROCESS_PRODUCTION_SOURCE_MANIFEST,
                current,
                label="Preflight production source",
            )
        except CompileGateError as exc:
            self._present_code_revision_restart_required(
                CodeRevisionRestartRequired(
                    exc,
                    context=(
                        "Code revision changed before job start; continuing "
                        "with the current production sources"
                    ),
                )
            )
        return None

    def _present_code_revision_restart_required(self, requirement):
        details = (
            requirement.as_dict()
            if isinstance(requirement, CodeRevisionRestartRequired)
            else copy.deepcopy(requirement or {})
        )
        details.update({
            "route": "code_revision_warning",
            "status": "warning",
            "asset_failure": False,
            "batch_continues": True,
        })
        message = str(
            details.get("message")
            or "Code revision changed; the batch continues with current code."
        )
        details["message"] = message
        signature = (
            str(details.get("expected_revision") or ""),
            str(details.get("actual_revision") or ""),
        )
        if not any(signature):
            signature = (
                hashlib.sha256(message.encode("utf-8")).hexdigest(),
                "",
            )
        self.__dict__.pop("_code_revision_restart_required", None)
        progress_var = getattr(self, "progress_var", None)
        if callable(getattr(progress_var, "set", None)):
            progress_var.set(
                "code_revision_warning · 현재 코드로 작업 계속"
            )
        if signature != getattr(
            self,
            "_last_code_revision_restart_notice_signature",
            None,
        ):
            self._last_code_revision_restart_notice_signature = signature
            if callable(getattr(self, "log", None)):
                self.log(
                    "[code_revision_warning · non-blocking]\n" + message
                )
        return details

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
            self._present_code_revision_restart_required(
                CodeRevisionRestartRequired(
                    exc,
                    context=(
                        "Production sources changed after the GUI loaded; "
                        "the batch uses the current compiled source set"
                    ),
                )
            )
        self._active_production_source_manifest = manifest
        self._active_production_source_fast_identity = (
            self._production_source_fast_identity(manifest)
        )
        self.log(
            "Production source revision 고정: "
            f"{manifest.content_hash} · {manifest.source_count} files"
        )
        return manifest

    @staticmethod
    def _production_source_fast_identity(manifest):
        """Stat known production files without Git discovery or content reads."""
        records = []
        for source in manifest.files:
            path = REPO_DIR / source.path
            try:
                identity = _fast_file_change_identity(path)
            except OSError:
                identity = {"missing": True}
            records.append((source.path, identity))
        return tuple(records)

    def _assert_active_production_source_manifest(self):
        lock = self.__dict__.setdefault(
            "_production_source_identity_lock",
            threading.Lock(),
        )
        with lock:
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
            current_fast = self._production_source_fast_identity(expected)
            expected_fast = getattr(
                self,
                "_active_production_source_fast_identity",
                None,
            )
            if expected_fast is None:
                self._active_production_source_fast_identity = current_fast
                return expected
            if current_fast == expected_fast:
                return expected
            try:
                current = production_source_manifest(REPO_DIR)
                validate_production_source_manifest(
                    expected,
                    current,
                    label="Parent production source",
                )
            except CompileGateError as exc:
                self._present_code_revision_restart_required(
                    CodeRevisionRestartRequired(
                        exc,
                        context=(
                            "Production source revision changed during the active "
                            "batch; continuing with the current source set"
                        ),
                    )
                )
                self._active_production_source_manifest = current
                self._active_production_source_fast_identity = (
                    self._production_source_fast_identity(current)
                )
                return current
            self._active_production_source_fast_identity = current_fast
            return expected

    def _require_child_production_source_manifest(
        self,
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
            warning = CodeRevisionRestartRequired(
                exc,
                context=(
                    "Cluster Assembly live audit worker revision differs; "
                    "keeping the live audit result"
                ),
                report=payload if isinstance(payload, dict) else None,
                log_file=log_file,
                report_file=report_file,
            )
            self._present_code_revision_restart_required(warning)
            return copy.deepcopy(
                (payload or {}).get("production_source_revision") or {}
            )

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
            elif job["mode"] == "waiting_import":
                completed = self._run_waiting_asset_import_batch(
                    job["targets"],
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
        except CodeRevisionRestartRequired as exc:
            revision_restart = exc.as_dict()
            error = str(exc)
            status = "completed"
            summary.setdefault("job_diagnostics", []).append({
                **copy.deepcopy(revision_restart),
                "stage": "production_source_revision",
                "route": "code_revision_warning",
                "asset_failure": False,
                "batch_continues": True,
            })
            self._present_code_revision_restart_required(revision_restart)
            self.log(
                f"[대기열 #{job['id']}] "
                f"code_revision_warning · continuing\n{error}"
            )
            error = None
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
                    queue_error = compact_error_message(queue_exc)
                    summary.setdefault("job_diagnostics", []).append({
                        "stage": "shared_queue_finalization",
                        "error": queue_error,
                        "asset_failure": False,
                        "owner_lost": bool(
                            getattr(lease, "heartbeat_error", None)
                        ),
                    })
                    error = queue_error
                    status = "failed"
                    if tracker is not None and retry_partition:
                        reconciled = tracker.reconcile_queue(
                            getattr(
                                self,
                                "shared_queue_runtime",
                                None,
                            ).queue
                        )
                        if (
                            not reconciled
                            and lease.heartbeat_error is not None
                        ):
                            tracker.mark_partition_terminal(
                                retry_partition,
                                RETRY_STAGE_OWNER_LOST,
                                "shared queue lease lost before receipt finalization",
                            )
                    self.log(
                        f"[대기열 #{job['id']}] 공용 대기열 종료 기록 실패 · "
                        f"{job['label']}: {queue_error}"
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
        if status == CODE_REVISION_RESTART_ROUTE:
            requirement = copy.deepcopy(
                payload.get(CODE_REVISION_RESTART_ROUTE) or {}
            )
            requirement.setdefault("message", str(error or ""))
            self._present_code_revision_restart_required(requirement)
            status = "completed"
            error = None
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
            "_active_assembly_stage_contracts",
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
            "blender": "① Blender Assembly",
            "push": "② Unreal Push 연계 ①→②",
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
            "push_transport": cfg.get("push_transport", "headless"),
        }
        self._enqueue_batch_job(job)

    def start_waiting_asset_import(self):
        """Queue every durable Unreal-wait row for one headless import run."""
        self._close_cell_editor()
        if self._unreal_running():
            self.progress_var.set("대기 에셋 임포트 · Unreal Editor 종료 필요")
            messagebox.showinfo(
                "대기 에셋 임포트",
                "MyProject2 Unreal Editor를 완전히 종료한 뒤 다시 실행하세요.",
                parent=self.root,
            )
            return None

        with self.state_lock:
            target_iids = [
                iid
                for iid, entry in self.state.items()
                if isinstance(iid, str)
                and isinstance(entry, dict)
                and Path(iid).suffix.casefold() == ".spm"
                and entry.get("push_status_kind")
                == "exported_pending_unreal"
            ]
        if not target_iids:
            messagebox.showinfo(
                "대기 에셋 임포트",
                "영구 상태에 Unreal import를 기다리는 에셋이 없습니다.",
                parent=self.root,
            )
            return None

        cfg = dict(self._collect_cfg())
        cfg["push_transport"] = "headless"
        save_config(cfg)
        inventory, targets = self._snapshot_batch_request(target_iids)
        for iid in target_iids:
            if iid not in inventory:
                inventory[iid] = {"spm": Path(iid)}
        targets = [inventory[iid] for iid in target_iids]
        return self._enqueue_batch_job({
            "label": f"대기 에셋 임포트 · {len(targets)}개",
            "mode": "waiting_import",
            "phase": "push",
            "terminal_phase": "push",
            "selected_scope": False,
            "targets": targets,
            "inventory": inventory,
            "cfg": cfg,
            "force_rerun": False,
            "push_transport": "headless",
        })

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
            state = self._assembly_output_state(Path(iid))
            if not isinstance(state, dict) or "current" not in state:
                raise ValueError("Repair eligibility state is incomplete")
            return state
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            return {
                "current": False,
                "push_ready": False,
                "kind": "inspection_incomplete",
                "reason": (
                    "Blender Assembly freshness could not be proven: "
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
        preserve_phase_status=False,
    ):
        """Persist automation progress without erasing a newer phase verdict."""

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
        if not preserve_phase_status:
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
            if not preserve_phase_status:
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
                        "reason_codes": list(
                            plan_payload.get("reason_codes") or ()
                        ),
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
            sealed_relations = stage.get("cluster_provider_relations") or ()
            if sealed_relations:
                providers = [
                    Path(row["provider_spm"])
                    for row in sealed_relations
                    if isinstance(row, dict) and row.get("provider_spm")
                ]
            else:
                providers = [
                    Path(path)
                    for path in stage.get("target_spms") or ()
                    if os.path.normcase(os.path.abspath(str(path))).casefold()
                    != os.path.normcase(
                        os.path.abspath(str(exact_spm))
                    ).casefold()
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
        provenance = {
            "reason_codes": list(plan.get("reason_codes") or ()),
            "evidence_sha256": plan.get("evidence_sha256"),
            "source": str(provenance_source),
        }
        if stage.get("repair_action") == ATLAS_SLOT_OWNERSHIP_RECONCILE:
            provenance["ownership_plan"] = copy.deepcopy(
                stage.get("ownership_plan")
            )
        if stage.get("repair_action") == ATLAS_PRODUCER_REFRESH:
            provenance["producer_relation"] = copy.deepcopy(
                stage.get("producer_relation")
            )
        if stage.get("cluster_provider_relations"):
            provenance["cluster_provider_relations"] = copy.deepcopy(
                stage["cluster_provider_relations"]
            )
        request = build_exact_target_request(
            tool=tool,
            repair_action=stage["repair_action"],
            target_spms=stage["target_spms"],
            repair_stage=stage["stage"],
            provenance=provenance,
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
        pipeline_reaudit_ids = set()
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
            exact_stages = list(plan.get("stages") or ())
            for stage_index, stage in enumerate(exact_stages, 1):
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

                try:
                    terminal = self._execute_exact_repair_stage(
                        plan,
                        stage,
                        lease,
                        stage_index=stage_index,
                        receipt=receipt,
                        provenance_source="sk_batch.failed_retry",
                        on_progress=on_exact_progress,
                    )
                except CodeRevisionRestartRequired:
                    raise
                except WaitCancelled as exc:
                    attempted.append({
                        "stage": stage["stage"],
                        "tool": stage["tool"],
                        "repair_action": stage["repair_action"],
                        "targets": list(stage["target_spms"]),
                        "receipt": str(receipt),
                        "status": "cancelled",
                        "error": compact_error_message(exc, 400),
                    })
                    cancelled = True
                    break
                except Exception as exc:
                    raw_error = compact_error_message(exc, 400)
                    attempted.append({
                        "stage": stage["stage"],
                        "tool": stage["tool"],
                        "repair_action": stage["repair_action"],
                        "targets": list(stage["target_spms"]),
                        "receipt": str(receipt),
                        "status": "orchestration_diagnostic",
                        "error": raw_error,
                    })
                    failed = (
                        "BAT exact repair orchestration failed",
                        raw_error,
                    )
                    break
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
                if stage_index < len(exact_stages):
                    # One exact repair plan is one bounded transaction.  An
                    # early stage may intentionally prepare inputs that only a
                    # later registered Generator/Cluster stage makes current.
                    # Auditing the whole asset at that intermediate boundary
                    # would create a new checkpoint and strand the remaining
                    # known repair action.
                    attempted_row["fresh_reaudit"] = {
                        "status": "deferred_until_remaining_exact_stages",
                        "remaining_stage_count": (
                            len(exact_stages) - stage_index
                        ),
                    }
                    self.ui_queue.put((
                        "progress",
                        f"{Path(iid).name}: exact repair stage "
                        f"{stage_index}/{len(exact_stages)} complete",
                    ))
                    continue
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
                except CodeRevisionRestartRequired:
                    raise
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
                # A BAT process/result failure is orchestration evidence, not
                # an asset verdict. Preserve it on the immutable retry item
                # and return to the ordinary full pipeline. Its fresh content
                # preflight is the only authority that may block this target.
                self._set_failed_retry_automatic_status(
                    iid,
                    STATUS_PIPELINE,
                    plan=plan,
                    attempted_stages=attempted,
                    completed_stages=sum(
                        row.get("status") == "completed" for row in attempted
                    ),
                )
                item["_failed_retry_attempted_stages"] = attempted
                item["_failed_retry_stage_diagnostic"] = {
                    "reason_token": failure_token,
                    "headline": headline,
                    "evidence": failure_evidence,
                }
                pipeline_targets.append(item)
                pipeline_reaudit_ids.add(iid)
                self.log(
                    "[automatic repair diagnostic] BAT result does not gate "
                    f"export; fresh full pipeline retained: {Path(iid).name} "
                    f"({failure_token}: {raw_error})"
                )
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
            pipeline_reaudit_ids.add(iid)

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
                if root not in pipeline_reaudit_ids
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
                stage_diagnostic = item.pop(
                    "_failed_retry_stage_diagnostic",
                    None,
                )
                resumed_by = item.pop("_failed_retry_resumed_by", [])
                pipeline_row = copy.deepcopy(pipeline_by_id.get(iid, {}))
                if stage_diagnostic and pipeline_row:
                    pipeline_row.setdefault("evidence", {})[
                        "automatic_repair_attempt"
                    ] = copy.deepcopy(stage_diagnostic)
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
                        preserve_phase_status=True,
                    )
                    if not fresh_repair_receipt_authoritative(
                        final_receipt, plan
                    ):
                        pipeline_row.setdefault("evidence", {})[
                            "automatic_repair_receipt_diagnostic"
                        ] = {
                            "status": "identity_mismatch",
                            "asset_failure": False,
                            "fresh_pipeline_outcome": "completed",
                            "receipt": copy.deepcopy(final_receipt),
                        }
                        self.log(
                            "[automatic repair route] completed fresh pipeline "
                            "owns the target verdict despite receipt bookkeeping "
                            f"mismatch: {Path(iid).name}"
                        )
                elif pipeline_outcome == "pending_unreal":
                    self._set_failed_retry_automatic_status(
                        iid,
                        STATUS_PIPELINE,
                        plan=plan,
                        attempted_stages=attempted,
                        completed_stages=len(attempted),
                        preserve_phase_status=True,
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
                        preserve_phase_status=True,
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
                    retry_token = str(
                        pipeline_row.get("reason_token")
                        or "pipeline_retry_result_missing"
                    )
                    # `_run_full_pipeline` already persisted the exact current
                    # content result. Do not overwrite it with the historical
                    # automation wrapper that preceded this audit.
                    self.log(
                        "[automatic repair route] fresh pipeline result owns "
                        f"the target verdict: {Path(iid).name} "
                        f"({retry_token})"
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
                item.pop("_failed_retry_stage_diagnostic", None)
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
        if not manifest_item_has_current_skeleton_root_export(parent_item):
            raise PushUnrealRecoveryError(
                "parent Send2UE FBX predates the authored Skeleton Root "
                "export contract; regenerate it through Send2UE"
            )
        blend_value = str(parent_item.get("blend") or "")
        if not blend_value:
            raise PushUnrealRecoveryError(
                "parent manifest item has no Blender source path"
            )
        current_fingerprint, current_record = self._source_push_fingerprint(
            Path(blend_value),
            queue_id,
            return_record=True,
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
        self._production_source_revision_precheck()
        # Capture the operator's rerun authority on the Tk owner thread.  The
        # planner runs in a worker and must never read a live Tk variable.  An
        # exact checked retry is itself an explicit rerun request; it does not
        # require a second force checkbox and a historical success receipt
        # cannot veto it (#175/#178).
        force_var = getattr(self, "force_var", None)
        try:
            force_checked = bool(force_var.get()) if force_var else False
        except Exception:
            force_checked = False
        retry_force_rerun = bool(scope == "checked" or force_checked)
        retry_force_full_rebuild = bool(force_checked)
        retry_request = {
            "scope": str(scope),
            "dialog_title": str(dialog_title),
            "empty_message": str(empty_message),
            "force_rerun": retry_force_rerun,
            "force_full_rebuild": retry_force_full_rebuild,
        }
        cfg = dict(self._collect_cfg())
        cfg["_retry_force_rerun"] = retry_force_rerun
        cfg["_retry_force_full_rebuild"] = retry_force_full_rebuild
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
                if Path(iid).is_file():
                    # A repair-plan/provenance wrapper is not export
                    # authority.  Keep the exact diagnostic, then let the
                    # phase classifier run a fresh full pipeline; concrete
                    # current content failures will be reported by that
                    # pipeline instead of by a zero-stage automation record.
                    planning_context.counters[
                        "repair_plan_diagnostics_routed_to_pipeline"
                    ] += 1
                    deferred_logs.append(
                        "[retry routing] repair plan unavailable; fresh full "
                        f"pipeline retained for {Path(iid).name}: {raw_error}"
                    )
                    continue
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
                if Path(iid).is_file():
                    planning_context.counters[
                        "unsupported_repair_routed_to_pipeline"
                    ] += 1
                    deferred_logs.append(
                        "[retry routing] metadata/repair plan is not an "
                        "export gate; fresh full pipeline retained for "
                        f"{Path(iid).name}: "
                        f"reason_codes={','.join(plan.reason_codes)}"
                    )
                    continue
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
                # parent. Unknown Assembly state still takes the existing
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

            # Waiting-import batches can be hundreds of MB because they
            # aggregate every immutable item.  Retry planning intentionally
            # caps one JSON document at 64 MiB, so inspect the exact per-item
            # export manifest first.  A proven legacy root contract needs a
            # Send2UE re-export and does not require loading the aggregate or
            # rebuilding the current Blender Assembly.
            export_manifest_value = str(
                ((entry.get("push_export_cache") or {}).get("manifest") or "")
            ).strip()
            if export_manifest_value:
                try:
                    export_manifest = planning_context.load_json(
                        export_manifest_value,
                        namespace="exact_push_export_manifest",
                    )
                    export_item = next(
                        row for row in (export_manifest.get("items") or ())
                        if str((row or {}).get("queue_id")) == iid
                    )
                except (
                    OSError,
                    StopIteration,
                    TypeError,
                    ValueError,
                    RetryPlanningSnapshotError,
                ):
                    export_item = None
                if (
                    export_item is not None
                    and not manifest_item_has_current_skeleton_root_export(
                        export_item
                    )
                ):
                    parent_statuses[iid] = UNREAL_PARENT_EXPORT_STALE
                    parent_diagnostics[iid] = (
                        "exact Push export FBX가 authored Skeleton Root export "
                        "계약보다 오래됨 · Blender Assembly는 유지하고 "
                        "Send2UE부터 재실행"
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

            parent_item = items_by_id[iid]
            if not manifest_item_has_current_skeleton_root_export(parent_item):
                parent_statuses[iid] = UNREAL_PARENT_EXPORT_STALE
                parent_diagnostics[iid] = (
                    "Send2UE FBX가 authored Skeleton Root export 계약보다 "
                    "오래됨 · Blender Assembly는 유지하고 Send2UE부터 재실행"
                )
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

        # This immutable value was captured on the Tk owner thread before the
        # async planner started and is part of the plan-cache signature.
        retry_force_rerun = bool(cfg.get("_retry_force_rerun"))
        retry_force_full_rebuild = bool(
            cfg.get("_retry_force_full_rebuild")
        )

        def classify(iid):
            return classify_failed_retry(
                effective_entries.get(iid, {}),
                repair_states[iid],
                unreal_parent_status=parent_statuses[iid],
                unreal_parent_diagnostic=parent_diagnostics[iid],
                force_rerun=retry_force_rerun,
                force_full_rebuild=retry_force_full_rebuild,
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
        send2ue_iids = [
            iid
            for iid in candidate_iids
            if decisions[iid].classification == SEND2UE_REEXPORT
            and iid not in bat_handled_ids
        ]
        skipped = []
        for iid in candidate_iids:
            if iid in bat_handled_ids:
                continue
            decision = decisions[iid]
            if (
                decision.classification in {
                    BLENDER_REBUILD,
                    SEND2UE_REEXPORT,
                    UNREAL_ONLY,
                }
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
        eligible_set = (
            set(export_iids) | set(send2ue_iids) | set(unreal_iids)
        )
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
                        if (
                            decision.classification
                            == CURRENT_BLENDER_EXCLUDED
                        ):
                            tracker.transition(
                                iid,
                                RETRY_STAGE_COMPLETE,
                                diagnostic=decision.diagnostic,
                                terminal_reason=decision.reason_code,
                                outcome=RETRY_STAGE_COMPLETE,
                            )
                            continue
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

        send2ue_targets = [
            targets_by_id[iid]
            for iid in send2ue_iids
            if iid in targets_by_id
        ]
        if send2ue_targets:
            send2ue_ids = [str(item["spm"]) for item in send2ue_targets]
            metadata = {
                "schema_version": 1,
                "kind": action_kind,
                "partition": "send2ue_reexport",
                "execution_path": "send2ue_then_unreal",
                "selected_queue_ids": send2ue_ids,
                "eligibility": eligibility_receipt(send2ue_ids),
            }
            if tracker is not None:
                tracker.assign_partition(
                    "send2ue_reexport",
                    send2ue_ids,
                    "send2ue_then_unreal",
                )
                metadata.update({
                    "progress_run_id": tracker.run_id,
                    "progress_receipt_path": str(tracker.path),
                })
            jobs.append({
                "label": (
                    "Retry run · Send2UE→Unreal · "
                    f"{len(send2ue_targets)} targets"
                ),
                "mode": "push",
                "phase": "push",
                "terminal_phase": "push",
                "selected_scope": True,
                "targets": send2ue_targets,
                "inventory": inventory,
                "cfg": cfg,
                "force_rerun": True,
                "push_transport": "headless",
                "retry_metadata": metadata,
                "_retry_progress_tracker": tracker,
            })

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

        for retry_job in jobs:
            retry_job["force_full_rebuild"] = retry_force_full_rebuild

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
                    if (
                        decision.classification
                        == CURRENT_BLENDER_EXCLUDED
                        and iid not in missing_ids
                    ):
                        tracker.transition(
                            iid,
                            RETRY_STAGE_COMPLETE,
                            diagnostic=decision.diagnostic,
                            terminal_reason=decision.reason_code,
                            outcome=RETRY_STAGE_COMPLETE,
                        )
                        continue
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
        self._production_source_revision_precheck()
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
        persisted_cfg = dict(cfg)
        persisted_cfg.pop("_retry_force_rerun", None)
        persisted_cfg.pop("_retry_force_full_rebuild", None)
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
            save_config(persisted_cfg)
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
            job_cfg = dict(job.get("cfg") or {})
            job_cfg.pop("_retry_force_rerun", None)
            job_cfg.pop("_retry_force_full_rebuild", None)
            job["cfg"] = job_cfg
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
        self._production_source_revision_precheck()
        cfg = dict(self._collect_cfg())
        save_config(cfg)
        inventory, targets = self._snapshot_batch_request(target_iids)
        selected_transport = cfg.get("push_transport", "headless")
        push_transport = (
            "unreal_wait"
            if selected_transport == "unreal_wait"
            else (
                "headless"
                if cfg.get("night_headless", True)
                else selected_transport
            )
        )
        job = {
            "label": f"목록 전체 자동 ①→② · {len(targets)}개",
            "mode": "pipeline",
            "phase": "push",
            "terminal_phase": "push",
            "selected_scope": False,
            "targets": targets,
            "inventory": inventory,
            "cfg": cfg,
            "force_rerun": bool(self.force_var.get()),
            "push_transport": push_transport,
        }
        self._enqueue_batch_job(job)

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
            paths["producer_assembly_report"] = assembly_pipeline_report_path(
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
            report = load_current_assembly_pipeline_report(
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
        if not push_source_cache_matches_snapshot(
            source_cache, current_snapshot
        ):
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
        if not manifest_item_has_current_skeleton_root_export(manifest_item):
            return {
                "status": "stale",
                "phase": "push",
                "reason": (
                    "Push export가 현재 Send2UE authored Skeleton Root "
                    "계약보다 오래되었습니다."
                ),
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
                        "saved Blender/Assembly 영수증이 current producer "
                        "SPM content key와 일치합니다."
                    ),
                }
            else:
                verdict = {
                    "status": "stale",
                    "phase": "blender",
                    "output_kind": "repair_receipt_not_current",
                    "reason": (
                        "Blender/Assembly 영수증이 current producer SPM "
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
        if normalized == "rerun_pending":
            return None
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
        self._active_assembly_stage_contracts = {}
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
                "_active_assembly_stage_contracts",
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
        ) = expand_blender_assembly_targets(
            targets,
            self._batch_job_inventory()
            or {str(item["spm"]): item for item in targets},
        )
        if auto_added_cluster_ids:
            self.log(
                "Tree Assembly dependency: Cluster "
                f"{len(auto_added_cluster_ids)}개 자동 포함 — "
                + ", ".join(
                    sorted(
                        Path(iid).name for iid in auto_added_cluster_ids
                    )
                )
            )
        phase_labels = {
            "blender": "① Blender Assembly",
            "push": "② Unreal Push",
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
        if cluster_targets and terminal_phase in {"blender", "push"}:
            schedule.append(
                (
                    "blender",
                    cluster_targets,
                    "Cluster ① Blender/Normalizer",
                )
            )
        if downstream_targets:
            if terminal_phase in {"blender", "push"}:
                schedule.append(
                    (
                        "blender",
                        downstream_targets,
                        "Tree ① Blender Assembly",
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
            planned_excluded_ids = set(
                getattr(self, "_pipeline_planned_exclusions", {}) or {}
            )
            excluded_ids = set(planned_excluded_ids)
            eligible_stage = self._filter_pipeline_excluded_targets(
                scheduled_targets,
                excluded_ids,
            )
            if not eligible_stage:
                continue
            self._pipeline_root_failed_items = set(root_failed_ids)
            self._pipeline_blocked_items = set(blocked_consumer_ids)
            self._pipeline_upstream_failed_items = set()
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
            self.log(f"🌙 {label} 종료")
            if not phase_ok:
                pipeline_abort = getattr(self, "_phase_abort_reason", None)
                if pipeline_abort or self.stop_flag.is_set():
                    break
                # Item-local failures do not suppress later target stages.
        planned_excluded_ids = set(
            getattr(self, "_pipeline_planned_exclusions", {}) or {}
        )
        summary = self._build_pipeline_result_summary(
            getattr(self, "_active_pipeline_selected_targets", targets),
            root_failed_ids,
            blocked_consumer_ids,
            pipeline_abort,
        )
        excluded_ids = {
            str(row.get("target") or "")
            for row in summary["target_outcomes"]
            if row.get("outcome") in {
                "failed", "blocked", "planned_excluded", "owner_lost",
            }
        }
        excluded_ids.discard("")
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

    def _start_active_process_cleanup(self):
        """Immediately reap every exact process tree owned by the active batch.

        Worker polling remains the normal completion path, but the Tk Stop
        handler must not wait for a long Python stage to return to that poll.
        ``terminate_process_tree`` is serialized per retained process handle,
        so this cleanup can safely race the worker's own finally block without
        ever broadening termination by executable name or PID discovery.
        """

        lock = getattr(self, "_active_process_cleanup_lock", None)
        if lock is None:
            lock = threading.Lock()
            self._active_process_cleanup_lock = lock
        with lock:
            current = getattr(self, "_active_process_cleanup_worker", None)
            if current is not None and current.is_alive():
                return current

            procs_lock = getattr(self, "procs_lock", None)
            if procs_lock is None:
                procs_lock = threading.Lock()
                self.procs_lock = procs_lock
            if not hasattr(self, "active_procs"):
                self.active_procs = set()

            def cleanup():
                failures = 0
                with procs_lock:
                    processes = list(self.active_procs)
                for process in processes:
                    try:
                        if process.poll() is None and not terminate_process_tree(
                            process
                        ):
                            failures += 1
                    except Exception:
                        failures += 1
                if processes:
                    if failures:
                        self.log(
                            "중지 정리: 관리 프로세스 트리 "
                            f"{len(processes)}개 중 {failures}개 종료 확인 실패"
                        )
                    else:
                        self.log(
                            "중지 정리 완료: 관리 프로세스 트리 "
                            f"{len(processes)}개 회수"
                        )

            worker = threading.Thread(
                target=cleanup,
                name="sk-batch-process-cleanup",
                daemon=True,
            )
            self._active_process_cleanup_worker = worker
            worker.start()
            return worker

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
        self._start_active_process_cleanup()
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

    def shutdown_shared_queue(self, *, on_complete=None):
        if callable(on_complete):
            if getattr(self, "_shutdown_complete", False):
                self.root.after_idle(on_complete)
                return
            callbacks = getattr(self, "_shutdown_callbacks", None)
            if callbacks is None:
                callbacks = []
                self._shutdown_callbacks = callbacks
            callbacks.append(on_complete)
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
        if callable(on_complete):
            self._schedule_shutdown_completion_poll()

    def _schedule_shutdown_completion_poll(self):
        if (
            getattr(self, "_shutdown_complete", False)
            or getattr(self, "_shutdown_poll_scheduled", False)
        ):
            return
        self._shutdown_poll_scheduled = True
        self.root.after(50, self._poll_shutdown_completion)

    def _poll_shutdown_completion(self):
        self._shutdown_poll_scheduled = False
        cleanup = getattr(self, "_active_process_cleanup_worker", None)
        if cleanup is not None and cleanup.is_alive():
            self._schedule_shutdown_completion_poll()
            return
        with self.procs_lock:
            live_processes = [
                process
                for process in self.active_procs
                if process.poll() is None
            ]
        if live_processes:
            self._start_active_process_cleanup()
            self._schedule_shutdown_completion_poll()
            return
        self._shutdown_complete = True
        callbacks = list(getattr(self, "_shutdown_callbacks", ()))
        self._shutdown_callbacks.clear()
        for callback in callbacks:
            callback()

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
            if column == "blend_status":
                state_entry.pop("blend_resume_receipt", None)
                item = getattr(self, "items", {}).get(iid)
                if isinstance(item, dict):
                    item["blend_resume_receipt"] = None
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

    def _begin_phase_state_save_batch(self):
        """Defer routine state snapshots until the active phase completes."""
        with self.state_lock:
            self._phase_state_save_batch_depth = (
                int(getattr(self, "_phase_state_save_batch_depth", 0)) + 1
            )

    def _save_state_after_phase_update(self):
        """Persist now unless an active phase can safely coalesce this update.

        Callers hold ``state_lock`` while updating ``self.state``.  Terminal
        failures (and in-flight cancellations) continue to call ``save_state``
        directly; deferred cancellation rows are captured by the forced phase
        flush.
        """
        if getattr(self, "_phase_state_save_batch_depth", 0) > 0:
            self._phase_state_save_batch_dirty = True
            return
        save_state(self.state)

    def _end_phase_state_save_batch(self, *, force=False):
        """Flush a coalesced phase state snapshot, including exceptional exits."""
        with self.state_lock:
            depth = int(getattr(self, "_phase_state_save_batch_depth", 0))
            if depth > 1:
                self._phase_state_save_batch_depth = depth - 1
                return
            if force or getattr(self, "_phase_state_save_batch_dirty", False):
                save_state(self.state)
            self._phase_state_save_batch_depth = 0
            self._phase_state_save_batch_dirty = False

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
        # Blender state writes are UI/progress snapshots committed once at the
        # phase boundary. Item failures stay immediate.
        if phase != "blender":
            return self._run_batch_impl(phase, targets, emit_done=emit_done)
        self._begin_phase_state_save_batch()
        try:
            return self._run_batch_impl(phase, targets, emit_done=emit_done)
        finally:
            # Force one final snapshot even when a revision fence or another
            # exceptional path exits before the normal phase tail.
            self._end_phase_state_save_batch(force=True)

    def _run_batch_impl(self, phase, targets, emit_done=True):
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        self._phase_abort_reason = None
        self._phase_failed_items = set()
        if phase in {"blender", "push"}:
            self._pipeline_dependency_artifact_cache = {}
        requested_targets = list(targets)
        self._active_push_dependency_map = {}
        self._active_push_auto_added_ids = set()
        preflight_skipped = set()
        if phase == "push":
            requested_ids = {
                str(item["spm"]) for item in requested_targets
            }
            targets, preflight_abort = self._push_preflight(
                requested_targets
            )
            ready_requested_ids = {
                str(item["spm"]) for item in targets
            }
            preflight_skipped = requested_ids - ready_requested_ids
            self._phase_failed_items.update(preflight_skipped)
            if preflight_abort:
                self._phase_failed_items.update(requested_ids)
                self._phase_abort_reason = preflight_abort
                if emit_done:
                    self.ui_queue.put(
                        (
                            "progress",
                            f"Unreal Push 중단 — {preflight_abort}",
                        )
                    )
                    self.ui_queue.put(("done", None))
                return False
            if not targets:
                excluded = len(preflight_skipped)
                reason = (
                    f"준비 검사 통과 항목 없음 · {excluded}개 제외"
                    if excluded
                    else "준비 검사 통과 항목 없음"
                )
                self.log(
                    f"Unreal Push 실행 항목 없음 — {reason}. "
                    "각 대상의 현재 준비 결과만 유지합니다."
                )
                if emit_done:
                    self.ui_queue.put(("progress", reason))
                    self.ui_queue.put(("done", None))
                return True

            stage_dependency_contracts = {}
            ready_requested_contract_keys = {
                _normalized_path(
                    speedtree_output_spm_for(item["spm"])
                )
                for item in targets
            }
            repair_contracts = getattr(
                self,
                "_active_assembly_stage_contracts",
                None,
            )
            if isinstance(repair_contracts, dict):
                with self.state_lock:
                    for root, repair_contract in repair_contracts.items():
                        if (
                            _normalized_path(root)
                            not in ready_requested_contract_keys
                            or not isinstance(repair_contract, dict)
                        ):
                            continue
                        dependency_contract = repair_contract.get(
                            "push_dependency_contract"
                        )
                        if isinstance(dependency_contract, dict):
                            stage_dependency_contracts[root] = (
                                copy.deepcopy(dependency_contract)
                            )

            inventory = self._batch_job_inventory() or {
                str(item["spm"]): item for item in targets
            }
            try:
                (
                    targets,
                    self._active_push_dependency_map,
                    self._active_push_auto_added_ids,
                    dependency_issues,
                ) = partition_push_targets(
                    targets,
                    inventory,
                    stage_dependency_contracts=(
                        stage_dependency_contracts or None
                    ),
                )
            except (
                PushDependencyError,
                OSError,
                TypeError,
                ValueError,
            ) as exc:
                # Dependency discovery is orchestration metadata.  A current,
                # preflight-ready export remains runnable when that metadata
                # cannot be read; the Push job itself still validates its
                # exact live files item by item.
                dependency_issues = {}
                self._active_push_dependency_map = {
                    str(item["spm"]): () for item in targets
                }
                self._active_push_auto_added_ids = set()
                self.log(
                    "Push dependency metadata unavailable; continuing exact "
                    "preflight-ready targets without inferred dependencies: "
                    + compact_error_message(exc)
                )

            dependency_blocked_roots = set()
            metadata_issue_count = 0
            metadata_issue_details = []
            for root, issue in dependency_issues.items():
                if not issue.concrete_missing:
                    metadata_issue_count += 1
                    metadata_issue_details.append(
                        f"{Path(root).name}: {issue}"
                    )
                    continue
                dependency_blocked_roots.add(str(root))
                preflight_skipped.add(str(root))
                dependency_path = str(issue.dependency_path or "")
                evidence = {
                    "scope": "exact_root",
                    "scheduling_error": str(issue),
                    "dependency_path": dependency_path,
                }
                self._record_phase_status(
                    str(root),
                    "push_status",
                    "건너뜀: 필요한 Cluster SPM 파일 없음",
                    "dependency_blocked",
                    str(issue),
                    details={
                        "reason_token": "dependency_output_missing",
                        "blocked_by": (
                            [dependency_path] if dependency_path else []
                        ),
                        "evidence": evidence,
                    },
                    persist=False,
                )
            if metadata_issue_count:
                self.log(
                    "Push dependency metadata nonblocking: "
                    f"{metadata_issue_count}개 current-ready target 계속"
                    + (
                        " — " + " | ".join(metadata_issue_details[:3])
                        if metadata_issue_details
                        else ""
                    )
                    + (
                        f" | 외 {metadata_issue_count - 3}개"
                        if metadata_issue_count > 3
                        else ""
                    )
                )

            actual_auto_added_ids = (
                self._active_push_auto_added_ids - requested_ids
            )
            auto_items = [
                item
                for item in targets
                if (
                    str(item["spm"])
                    in actual_auto_added_ids
                )
            ]
            ready_auto_ids = set()
            if auto_items:
                ready_auto, auto_abort = self._push_preflight(auto_items)
                if auto_abort:
                    self._phase_failed_items.update(requested_ids)
                    self._phase_abort_reason = auto_abort
                    if emit_done:
                        self.ui_queue.put(
                            (
                                "progress",
                                f"Unreal Push 중단 — {auto_abort}",
                            )
                        )
                        self.ui_queue.put(("done", None))
                    return False
                ready_auto_ids = {
                    str(item["spm"]) for item in ready_auto
                }

            reusable_current_dependencies = set()
            reusable_waiting_dependencies = set()
            unavailable_dependencies = {}
            for dependency in (
                actual_auto_added_ids - ready_auto_ids
            ):
                verdict = self._dependency_artifact_verdict(
                    dependency,
                    phase="push",
                )
                status = str(verdict.get("status") or "")
                if status == "current":
                    if getattr(self, "force_rerun", False):
                        ready_auto_ids.add(dependency)
                    else:
                        reusable_current_dependencies.add(dependency)
                    continue
                if status == "waiting":
                    reusable_waiting_dependencies.add(dependency)
                    continue
                unavailable_dependencies[dependency] = verdict

            dependency_reuse = self.__dict__.setdefault(
                "_pipeline_dependency_reuse_evidence",
                {},
            )
            for root, dependencies in list(
                self._active_push_dependency_map.items()
            ):
                if root in dependency_blocked_roots:
                    continue
                unavailable = [
                    dependency
                    for dependency in dependencies
                    if dependency in unavailable_dependencies
                ]
                if unavailable:
                    dependency_blocked_roots.add(root)
                    preflight_skipped.add(root)
                    artifact_rows = {
                        dependency: copy.deepcopy(
                            unavailable_dependencies[dependency]
                        )
                        for dependency in unavailable
                    }
                    reason_token = (
                        "dependency_output_missing"
                        if any(
                            row.get("status") == "missing"
                            for row in artifact_rows.values()
                        )
                        else "dependency_output_stale"
                    )
                    reason = (
                        "필요한 Cluster Push 산출물이 현재 없거나 낡음: "
                        + ", ".join(
                            Path(value).name for value in unavailable
                        )
                    )
                    self._record_phase_status(
                        root,
                        "push_status",
                        f"건너뜀: {reason}",
                        "dependency_blocked",
                        reason,
                        details={
                            "reason_token": reason_token,
                            "blocked_by": unavailable,
                            "dependency_artifacts": artifact_rows,
                            "evidence": {
                                "scope": "exact_root",
                                "dependency_artifacts": artifact_rows,
                            },
                        },
                        persist=False,
                    )
                    continue
                reused = {
                    dependency: copy.deepcopy(
                        self._dependency_artifact_verdict(
                            dependency,
                            phase="push",
                        )
                    )
                    for dependency in dependencies
                    if dependency in (
                        reusable_current_dependencies
                        | reusable_waiting_dependencies
                    )
                }
                if reused:
                    dependency_reuse[root] = reused

            filtered_dependency_map = {}
            for root, dependencies in self._active_push_dependency_map.items():
                if root in dependency_blocked_roots:
                    continue
                filtered_dependency_map[root] = tuple(
                    dependency
                    for dependency in dependencies
                    if dependency not in reusable_current_dependencies
                )
            self._active_push_dependency_map = filtered_dependency_map
            required_dependency_ids = {
                dependency
                for dependencies in filtered_dependency_map.values()
                for dependency in dependencies
            }
            targets = [
                item
                for item in targets
                if (
                    str(item["spm"]) not in dependency_blocked_roots
                    and (
                        str(item["spm"]) in ready_requested_ids
                        or str(item["spm"]) in required_dependency_ids
                    )
                )
            ]
            self._active_push_auto_added_ids = (
                required_dependency_ids - requested_ids
            )
            self._phase_failed_items.update(preflight_skipped)

            if self._active_push_auto_added_ids:
                names = sorted(
                    Path(iid).name
                    for iid in self._active_push_auto_added_ids
                )
                self.log(
                    "Tree Push dependency: Cluster "
                    f"{len(names)}개 자동 포함 — {', '.join(names)}"
                )
            if not targets:
                excluded = len(preflight_skipped)
                reason = (
                    f"준비 검사 통과 항목 없음 · {excluded}개 제외"
                    if excluded else "준비 검사 통과 항목 없음"
                )
                self.log(
                    f"Unreal Push 실행 항목 없음 — {reason}. "
                    "각 대상의 현재 결과만 유지합니다."
                )
                if emit_done:
                    self.ui_queue.put(("progress", reason))
                    self.ui_queue.put(("done", None))
                return True
        if phase == "blender":
            targets, resume_skipped = self._prefilter_blender_resume_targets(
                targets
            )
            if not targets:
                self.ui_queue.put(("batch_progress", (0, 0)))
                self.log(
                    "Blender Assembly 실행 항목 없음 — 재개 영수증으로 "
                    f"{len(resume_skipped)}개 완료 상태 확인"
                )
                if emit_done:
                    self.ui_queue.put(("progress", "대기"))
                    self.ui_queue.put(("done", None))
                return True
        titles = {"check": "검사", "blender": "Blender Assembly", "push": "Unreal Push"}
        column_by_phase = {"check": "spm_status",
                           "blender": "blend_status", "push": "push_status"}
        if (
            phase == "push"
            and getattr(self, "active_push_transport", "rpc") == "headless"
        ):
            return self._run_headless_push_batch(targets, emit_done=emit_done)
        if (
            phase == "push"
            and getattr(self, "active_push_transport", "rpc") == "unreal_wait"
        ):
            return self._run_headless_push_batch(
                targets,
                emit_done=emit_done,
                defer_import=True,
            )
        title = titles[phase]
        column = column_by_phase[phase]
        total = len(targets)
        self.ui_queue.put(("batch_progress", (0, total)))
        self._batch_done = 0
        self._batch_active = 0
        phase_abort = threading.Event()
        attempted = set()
        failed_items = set(preflight_skipped)

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
                if phase == "check":
                    self._job_check(iid, spm)
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
            except CodeRevisionRestartRequired:
                raise
            except Exception as exc:
                full_reason = str(exc)
                reason = compact_error_message(full_reason)
                kind = getattr(exc, "kind", "data_error")
                if self.stop_flag.is_set() or kind in {"cancelled", "stopped"}:
                    kind = "cancelled"
                    reason = reason or "사용자 중지"
                    full_reason = full_reason or reason
                if phase == "blender":
                    self._publish_assembly_stage_contract(
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
                exception_report = compact_durable_failure_report(
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
        # cannot change after a downstream Assembly receipt is written.
        # RPC Push stays serial; headless Push parallelizes only its Blender
        # export stage below.
        if phase == "check":
            workers = self.cfg.get("check_parallel_jobs", 8)
        elif phase == "blender":
            workers = self.cfg.get("blender_parallel_jobs", 2)
        else:
            workers = 1
        workers = max(1, min(int(workers), total))
        waves = (
            blender_assembly_schedule_waves(targets)
            if phase == "blender"
            else [targets]
        )
        if phase == "blender" and len(waves) > 1:
            self.log(
                "Blender Assembly 의존성: Cluster 소스를 먼저 완료한 뒤 "
                "루트/Assembly 재생성을 시작합니다."
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
                self._save_state_after_phase_update()
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
    def _assembly_contract_current(spm, pipeline_out=None):
        """Prove the saved blend/report came from the current SPM content."""
        try:
            report = load_current_assembly_pipeline_report(spm)
            if isinstance(pipeline_out, dict):
                pipeline_out["payload"] = report
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
        """Validate the saved materialized Assembly output, not its provenance."""
        from cluster_assembly_builder import (
            MANIFEST_KIND,
            validate_file_fingerprint,
            validate_manifest_artifacts,
        )

        report_path = (
            Path(spm).parent / "reports" /
            f"{Path(spm).stem}_speedtree_assembly_pipeline_report_codex.json"
        )
        if not report_path.is_file():
            # No report means this asset has never published an Assembly
            # manifest.  It is an ordinary asset; there is no stale manifest
            # for Push to reject.
            return True, ""
        try:
            pipeline = _read_assembly_pipeline_json(report_path)
            embedded = pipeline.get("cluster_assembly_manifest")
            if not isinstance(embedded, dict):
                # Vegetation with no saved Assembly is a legitimate ordinary
                # asset.  A newer PCG relation is work for a future Assembly, not
                # a reason to invalidate an already materialized Push payload.
                return True, ""

            if embedded.get("status") == "pass_through":
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
                    "Assembly Cluster Assembly manifest file is missing: "
                    + str(manifest_path)
                )
            validate_file_fingerprint(
                manifest_record, "Assembly Cluster Assembly manifest"
            )
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["manifest"] = manifest_record
            if manifest.get("kind") != MANIFEST_KIND:
                raise RuntimeError(
                    "unsupported Assembly Cluster Assembly manifest kind"
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
                "Cluster Assembly export payload is unavailable: "
                + str(exc)
            )

    def _assembly_runtime_addon_dir(self):
        """Installed Assembly addon folder, derived identically for read and write."""
        planning = self._failed_retry_planning_context()
        cfg = (
            planning.cfg_snapshot
            if planning is not None
            else getattr(self, "cfg", {}) or {}
        )
        return addon_dir_from_config(cfg)

    @staticmethod
    def _assembly_runtime_code_paths(addon_dir):
        """Every producer module that can change a completed Assembly result."""
        return assembly_runtime_code_paths(addon_dir)

    def _assembly_runtime_code_state(self, addon_dir):
        """Content hash per producer module, independent of timestamps."""
        return assembly_runtime_code_state(
            addon_dir,
            modules=self._assembly_runtime_code_paths(addon_dir),
        )

    @staticmethod
    def _assembly_runtime_receipt_path(spm):
        return assembly_runtime_receipt_path(spm)

    def _write_assembly_runtime_receipt(self, spm):
        """Record which Assembly code produced this completed Assembly result.

        A run that finds nothing to change legitimately saves no .blend, so the
        blend timestamp cannot stand in for code freshness: it pins the check
        forever and keeps demanding a rerun that can never satisfy it.  The
        receipt states what the run actually verified instead.
        """
        addon_dir = self._assembly_runtime_addon_dir()
        if addon_dir is None:
            return
        try:
            state = self._assembly_runtime_code_state(addon_dir)
        except OSError as exc:
            self.log(f"  [① 경고] Assembly 런타임 지문 계산 실패: {spm.name}: {exc}")
            return
        if not state:
            return
        try:
            write_assembly_runtime_receipt(
                spm,
                getattr(self, "cfg", {}) or {},
                addon_dir=addon_dir,
                code_state=state,
                blend=blend_path_for(spm),
            )
        except OSError as exc:
            self.log(f"  [① 경고] Assembly 런타임 기록 실패: {spm.name}: {exc}")

    def _assembly_runtime_fresh(self, spm, content_contract_out=None):
        """Gate only on an explicit saved-output contract revision.

        Producer source hashes are retained in the receipt for diagnostics,
        but ordinary code edits must not invalidate every completed Assembly.
        The live SPM/report/artifact contracts below remain authoritative for
        content freshness.
        """
        blend = blend_path_for(spm)
        report_path = (
            Path(spm).parent / "reports" /
            f"{Path(spm).stem}_speedtree_assembly_pipeline_report_codex.json"
        )
        if not blend.is_file() or not report_path.is_file():
            # Missing/stale source outputs are explained by the ordinary
            # handoff checks; runtime freshness applies only to a completed
            # Assembly result that would otherwise be skipped as current.
            return True, ""

        pipeline_probe = {}
        content_contract_current = self._assembly_contract_current(
            spm,
            pipeline_out=pipeline_probe,
        )
        if isinstance(content_contract_out, dict):
            content_contract_out["current"] = content_contract_current
        # Cleanup/output-contract versions and runtime receipts are diagnostic.
        # Push performs the current in-memory material/slot normalization and
        # must not demand another Assembly solely to refresh this metadata.
        return True, ""

    def _assembly_output_state(self, spm, pipeline_projection_out=None):
        with assembly_report_read_scope():
            state = self._assembly_output_state_scoped(spm)
            if isinstance(pipeline_projection_out, dict):
                try:
                    pipeline = _read_assembly_pipeline_json(
                        assembly_pipeline_report_path(spm)
                    )
                except (OSError, TypeError, ValueError):
                    pipeline = {}
                manifest = pipeline.get("cluster_assembly_manifest")
                if isinstance(manifest, dict):
                    pipeline_projection_out["cluster_assembly_manifest"] = (
                        copy.deepcopy(manifest)
                    )
            return state

    def _assembly_output_state_scoped(self, spm):
        """One semantic decision shared by row status and the ① queue gate."""
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
                "reason": "생성 필요 — blend 없음 → ① Blender Assembly",
            }
        content_contract_probe = {}
        runtime_fresh, runtime_reason = self._assembly_runtime_fresh(
            spm,
            content_contract_out=content_contract_probe,
        )
        if not runtime_fresh:
            return {
                "current": False,
                "push_ready": False,
                "kind": "output_contract",
                "reason": runtime_reason,
            }
        receipt_current = content_contract_probe.get("current")
        if not isinstance(receipt_current, bool):
            receipt_current = self._assembly_contract_current(spm)
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

        # Push validates materialized Assembly manifests unconditionally.  The
        # scheduler must use the same rule even when an older content receipt
        # is merely diagnostic; otherwise it skips ① and hands Push a manifest
        # that Push correctly rejects under the current placement contract.
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
                    "→ ① Blender Assembly"
                ),
            }

        handoff_status = ""
        if receipt_current:
            try:
                report = load_current_assembly_pipeline_report(spm)
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
                "reason": "wind JSON 없음 → ① 필요",
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
                    "① Blender Assembly에서 자동 재생성"
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
        """Return Unreal handoff readiness from the shared Assembly decision."""
        state = self._assembly_output_state(spm)
        if isinstance(state_out, dict):
            state_out.update(copy.deepcopy(state))
        return bool(state["current"] and state["push_ready"]), state["reason"]

    def _publish_assembly_stage_contract(
        self,
        spm,
        *,
        ready,
        reason,
        kind=None,
        push_dependency_contract=None,
        evidence_bundle=None,
    ):
        """Publish ①'s final result for ② in the same pipeline only."""
        contracts = getattr(self, "_active_assembly_stage_contracts", None)
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

    def _assembly_stage_contract(self, spm):
        """Read a job-scoped ① result without accepting persisted state."""
        contracts = getattr(self, "_active_assembly_stage_contracts", None)
        if not isinstance(contracts, dict):
            return None
        key = _normalized_path(speedtree_output_spm_for(spm))
        with self.state_lock:
            value = contracts.get(key)
            return copy.deepcopy(value) if isinstance(value, dict) else None

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
        if content_receipt_current:
            exported = dict(exported)
            all_exported = dict(all_exported)
            if exported.get("status") == "stale":
                exported["status"] = "ok"
            if all_exported.get("status") == "stale":
                all_exported["status"] = "ok"
        admission = classify_material_export_admission(
            exported,
            all_exported,
        )
        if admission.get("status") in {"ok", "diagnostic_only"}:
            return True, "SpeedTree 재질 export 정상"
        failed_contract = (
            all_exported
            if all_exported.get("status") not in {"ok", "not_applicable"}
            else exported
        )
        status = failed_contract.get("status")
        missing = list(admission.get("missing_materials") or [])
        if status == "missing_stmat":
            return False, "SpeedTree .stmat 없음 → ① Blender Assembly"
        if status == "invalid_stmat":
            return False, (
                "SpeedTree .stmat 파싱 실패 — "
                + str(failed_contract.get("error") or "파일 확인")
            )
        if status == "stale":
            return False, "SPM 변경 후 SpeedTree .stmat이 오래됨 → ① 다시 실행"
        if missing:
            return False, (
                "현재 내보내는 노드의 재질이 SpeedTree FBX에서 빠짐 — "
                + ", ".join(missing)
                + " → 표시된 SpeedTree 노드의 재질/텍스처 확인"
            )
        return False, "SpeedTree 재질 export 사전검사 실패 → ① 확인"

    @staticmethod
    def _texture_normalization_ready(spm, content_receipt_current=None):
        report_path = assembly_pipeline_report_path(spm)
        if not report_path.is_file():
            return False, "텍스처 정규화 정보 없음 → ① 필요"
        if content_receipt_current is None:
            content_receipt_current = App._assembly_contract_current(spm)
        try:
            if (
                report_path.stat().st_mtime_ns < Path(spm).stat().st_mtime_ns
                and not content_receipt_current
            ):
                return False, "SPM 변경 후 Assembly 보고서가 오래됨 → ① 다시 실행"
        except OSError as exc:
            return False, f"텍스처 보고서 시간 확인 실패: {exc}"
        try:
            report = load_current_assembly_pipeline_report(spm)
        except (OSError, ValueError, RuntimeError) as exc:
            if "legacy Assembly report" in str(exc):
                return False, (
                    "공통 SpeedTree 계약 정보 없음 → "
                    "① Blender Assembly 다시 실행"
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
            return False, "① 사전검사 정보 없음 → ① Blender Assembly 다시 실행"
        slots = handoff.get("empty_material_slots") or []
        outputs = handoff.get("missing_outputs") or []
        material_export = handoff.get("material_export") or {}
        material_admission = classify_material_export_admission(
            material_export,
            {},
            report.get("speedtree_native_receipt") or {},
        )
        materials = (
            list(material_admission.get("missing_materials") or [])
            if material_admission.get("status") == "blocked"
            else []
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
        if leaf_contract.get("status") in {"blocked", "inspection_error", "invalid_references"}:
            reasons.append("SPM leaf 참조 실패")
        if reasons:
            return False, "① 사전검사 차단: " + " | ".join(reasons)
        if handoff.get("status") not in {"ok", "source_review", "blocked"}:
            return False, "① 사전검사 미완료 → Blender Assembly 다시 실행"
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
    def _blend_status_from_assembly_state(state):
        """Format one already-computed Assembly state for the overview table."""
        if state["kind"] == "missing_blend":
            return "생성 필요 — blend 없음 · ① Blender Assembly 실행"
        if state["kind"] == "stale_content":
            return "Blender 갱신 필요 — SPM이 더 최근에 수정됨 · ① 다시 실행"
        if not state["current"]:
            return f"Assembly 필요 — {state['reason']}"
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
            state = self._assembly_output_state(spm)
        except OSError as exc:
            return f"확인 실패 — {exc}"
        return self._blend_status_from_assembly_state(state)

    def _record_live_blend_status(
        self,
        iid,
        spm,
        persist=True,
        repair_state=None,
        validated_resume_receipt=None,
        relation_validated=False,
    ):
        text = (
            self._blend_status_from_assembly_state(repair_state)
            if isinstance(repair_state, dict)
            else self._blend_status_text(spm)
        )
        self.ui_queue.put(("cell", (iid, "blend_status", text)))
        # A read-only Check used to build and hash a durable resume receipt and
        # then throw it away.  That repeated the most expensive status work
        # for every row despite persist=False.
        if not persist:
            return text
        resume_receipt = None
        texture_paths = ()
        live_signature = None
        if (
            isinstance(repair_state, dict)
            and repair_state.get("current") is True
        ):
            if isinstance(validated_resume_receipt, dict):
                resume_receipt = copy.deepcopy(validated_resume_receipt)
                texture_paths = tuple(
                    (self.state.get(iid) or {}).get(
                        "live_texture_paths"
                    )
                    or ()
                )
                live_signature = self._live_status_signature(
                    spm,
                    texture_paths,
                )
            else:
                try:
                    texture_paths = self._reported_texture_paths(spm)
                    live_signature = self._live_status_signature(
                        spm,
                        texture_paths,
                    )
                    resume_receipt = self._build_blender_resume_receipt(
                        iid,
                        spm,
                        repair_state,
                        texture_paths=texture_paths,
                        relation_validated=relation_validated,
                    )
                except (
                    BlenderResumeReceiptError,
                    OSError,
                    TypeError,
                    ValueError,
                ) as exc:
                    self.log(
                        "  [① 재개 영수증 경고] "
                        f"{Path(spm).name}: {compact_error_message(exc)}"
                    )
        with self.state_lock:
                entry = self.state.setdefault(iid, {})
                entry["blend_status"] = text
                if (
                    isinstance(repair_state, dict)
                    and repair_state.get("current") is True
                ):
                    # Fresh live Repair evidence supersedes an older persisted
                    # failure. Keeping data_error beside a current status made
                    # retry planning strand already-repaired providers.
                    entry["blend_status_kind"] = "ok"
                    entry.pop("blend_status_error", None)
                    entry.pop("blend_status_result", None)
                    if resume_receipt is not None:
                        entry["blend_resume_receipt"] = resume_receipt
                        entry["live_texture_paths"] = list(texture_paths)
                        entry["live_status_signature"] = live_signature
                    else:
                        entry.pop("blend_resume_receipt", None)
                    item = getattr(self, "items", {}).get(iid)
                    if isinstance(item, dict):
                        item["blend_resume_receipt"] = copy.deepcopy(
                            resume_receipt
                        )
                        if resume_receipt is not None:
                            item["live_texture_paths"] = texture_paths
                            item["live_status_signature"] = live_signature
                elif isinstance(repair_state, dict):
                    entry.pop("blend_resume_receipt", None)
                self._save_state_after_phase_update()
        return text

    def _job_check(self, iid, spm):
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
        # The version-locked native exporter now emits one exact bone graph to
        # both FBX and XML. A read-only status pass must not parse the SPM again
        # or attempt to classify/repair authored bone-generator settings.
        text = "Native FBX/XML 본 계약 · SPM 재파싱 없음"
        self.ui_queue.put(("cell", (iid, "spm_status", text)))

        try:
            assembly_state = self._assembly_output_state(spm)
        except OSError as exc:
            why = f"확인 실패 — {exc}"
            ok = False
            self.ui_queue.put(("cell", (iid, "blend_status", why)))
        else:
            self._record_live_blend_status(
                iid,
                spm,
                persist=False,
                repair_state=assembly_state,
            )
            ok = bool(
                assembly_state.get("current")
                and assembly_state.get("push_ready")
            )
            why = str(assembly_state.get("reason") or "준비 안 됨")
        pushed = self._current_push_status_text(iid, spm)
        push_text = why if not pushed or not ok else f"{why} | {pushed}"
        self.ui_queue.put(("cell", (iid, "push_status", push_text)))

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

    def _run_inline_atlas_manifest_repair(
        self,
        spm,
        failure_report,
        repair_action=ATLAS_MANIFEST_MIRROR_REPAIR,
    ):
        """Run one exact PCG/Atlas repair under the owned batch lease."""

        from pcg_st9_texture_batch.exact_target_repair import (
            execute_step3_standard,
        )

        spm = Path(spm).expanduser().absolute()
        reason_codes = sorted(set(evidence_reason_codes(failure_report)))
        ownership_route = bool({
            "atlas_manifest_ownership_conflict",
            "atlas_manifest_resolution_conflict",
        }.intersection(reason_codes))
        ownership_plan = None
        if ownership_route:
            from spm_generator_sync.exact_target_repair import (
                execute_exact_generator_request,
            )
            from atlas_slot_ownership import (
                AtlasSlotOwnershipError,
                plan_atlas_slot_ownership_reconciliation,
                validate_atlas_slot_ownership_plan,
            )

            evidence = failure_report.get("evidence") or {}
            supplied_plan = (
                evidence.get("ownership_plan")
                if isinstance(evidence, dict)
                else None
            )
            try:
                ownership_plan = validate_atlas_slot_ownership_plan(
                    supplied_plan,
                    target_spm=spm,
                    require_repairable=True,
                )
            except (AtlasSlotOwnershipError, OSError, ValueError) as exc:
                raise BatchItemError(
                    "최종 차단: live SPM 기반 Atlas slot ownership 계획이 "
                    "없거나 더 이상 유효하지 않습니다.",
                    kind="data_error",
                    report={
                        "repair_disposition": REPAIR_UI_BLOCKED,
                        "original_failure": copy.deepcopy(failure_report),
                        "ownership_plan_error": str(exc),
                    },
                ) from exc
            repair_action = ATLAS_SLOT_OWNERSHIP_RECONCILE
            repair_tool = GENERATOR_SYNC_TOOL
            repair_stage = "atlas_slot_ownership_reconcile"
            executor = execute_exact_generator_request
        else:
            repair_tool = PCG_TEXTURE_TOOL
            repair_stage = (
                "atlas_manifest_repair"
                if repair_action == ATLAS_MANIFEST_MIRROR_REPAIR
                else "pcg_texture"
            )
            executor = execute_step3_standard
        lease = getattr(self, "_active_shared_queue_lease", None)
        if lease is None or getattr(lease, "finished", False):
            raise BatchItemError(
                "Atlas manifest 자동 복구에 필요한 현재 공용 대기열 소유권이 없습니다.",
                kind="automatic_repair_pending",
                report=copy.deepcopy(failure_report),
            )
        key = os.path.normcase(os.path.abspath(str(spm))).casefold()
        memo_key = f"{repair_action}\0{key}"
        # Known before any stage runs, and the only thing on this path that
        # says what the repair was for.  The failure branch below has to carry
        # it onto the durable row; the cached path never reaches the block
        # that used to compute it.
        root_reason_codes = sorted(set(
            evidence_reason_codes(failure_report)
            or [
                "atlas_manifest_ownership_conflict"
                if ownership_route
                else "atlas_manifest_mirror_conflict_repairable"
            ]
        ))
        with _INLINE_ATLAS_REPAIR_LOCK:
            memo = self.__dict__.setdefault("_inline_atlas_repair_results", {})
            cached = memo.get(memo_key)
            if cached is not None:
                cached_status = str(
                    cached.get("terminal_status")
                    or cached.get("status")
                    or ""
                )
                if cached_status != "completed":
                    terminal = copy.deepcopy(cached)
                else:
                    if ownership_route:
                        current_plan = (
                            plan_atlas_slot_ownership_reconciliation(spm)
                        )
                        repair_is_current = (
                            current_plan.get("status") == "current"
                        )
                    elif repair_action == ATLAS_MANIFEST_MIRROR_REPAIR:
                        current_plan = atlas_manifest_mirror_repair_plan(spm)
                        repair_is_current = (
                            current_plan.get("status") == "not_needed"
                        )
                    else:
                        repair_is_current = True
                    if repair_is_current:
                        terminal = copy.deepcopy(cached)
                    else:
                        # A later producer can change mirror or slot ownership
                        # during one long batch. Current evidence, not the earlier
                        # receipt, decides whether another exact repair is needed.
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
                    (
                        queue_identity
                        + "\0"
                        + repair_action
                        + "\0"
                        + key
                    ).encode("utf-8")
                ).hexdigest()[:16]
                request_id = f"inline-atlas-{request_hash}"
                receipt = LOG_DIR / f"exact_repair_{request_id}.json"
                parent_retry_id = str(
                    retry_metadata.get("progress_run_id")
                    or active_job.get("shared_queue_job_id")
                    or f"sk-batch-{active_job.get('id') or 'direct'}"
                )
                provenance = {
                    "reason_codes": list(root_reason_codes),
                    "source": "sk_batch.inline_atlas_preflight",
                }
                if ownership_route:
                    provenance["ownership_plan"] = copy.deepcopy(
                        ownership_plan
                    )
                request = build_exact_target_request(
                    tool=repair_tool,
                    repair_action=repair_action,
                    target_spms=[spm],
                    repair_stage=repair_stage,
                    provenance=provenance,
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
                    executor,
                    inherited_lease=lease,
                    cancel_event=self.stop_flag,
                    on_progress=on_progress,
                )
                memo[memo_key] = copy.deepcopy(terminal)
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
                diagnostic = {
                    "status": "diagnostic_only",
                    "mutation_authorized": False,
                    "reason_codes": list(root_reason_codes),
                    "repair_attempt": {
                        "status": "failed",
                        "error": compact_error_message(raw_error, 320),
                        "receipt": copy.deepcopy(terminal),
                    },
                    "original_failure": copy.deepcopy(failure_report),
                }
                # This refresh writes metadata. Its process result is not
                # authority over a valid live SpeedTree graph. The caller
                # still performs a fresh audit and only concrete content from
                # that audit may stop export.
                self.log(
                    "[Atlas metadata diagnostic] exact refresh did not "
                    "complete; export remains admitted to fresh content "
                    f"audit: {spm.name} "
                    f"({diagnostic['repair_attempt']['error']})"
                )
                return diagnostic
            result = copy.deepcopy(terminal.get("result") or {})
            if ownership_route or repair_action == STEP3_STANDARD:
                try:
                    canonical_refresh = refresh_atlas_manifests_for_spm(
                        spm,
                        require_complete=True,
                    )
                except CanonicalOutputManifestError as exc:
                    diagnostic = {
                        "status": "diagnostic_only",
                        "mutation_authorized": False,
                        "reason_codes": list(root_reason_codes),
                        "repair_attempt": {
                            "status": "completed_but_reaudit_unresolved",
                            "receipt": copy.deepcopy(terminal),
                        },
                        "fresh_reaudit": copy.deepcopy(
                            getattr(exc, "report", {}) or {}
                        ),
                        "original_failure": copy.deepcopy(failure_report),
                    }
                    self.log(
                        "[Canonical mapping diagnostic] exact PCG refresh "
                        "completed but metadata mapping remains unresolved; "
                        f"fresh live audit continues: {spm.name}"
                    )
                    if not ownership_route:
                        return diagnostic
                    raise BatchItemError(
                        "Atlas slot ownership 복구 후 canonical manifest "
                        "재검증에 실패했습니다: "
                        + compact_error_message(str(exc), 320),
                        kind="automatic_repair_failed",
                        report={
                            "repair_disposition": REPAIR_UI_BLOCKED,
                            "original_failure": copy.deepcopy(failure_report),
                            "exact_repair_receipt": copy.deepcopy(terminal),
                            "post_repair_failure": copy.deepcopy(
                                getattr(exc, "report", {}) or {}
                            ),
                        },
                    ) from exc
            else:
                canonical_refresh = copy.deepcopy(
                    result.get("canonical_refresh") or {}
                )
            self.log(
                "[자동 복구 완료] Canonical PCG → Atlas manifest · "
                f"{spm.name}"
            )
            return canonical_refresh

    def _refresh_canonical_atlas_manifests(self, spm):
        """Synchronize canonical PCG output for an explicit PCG repair action."""
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
            failure_stage = str(failure_report.get("stage") or "")
            failure_reason_codes = set(evidence_reason_codes(failure_report))
            ownership_evidence = failure_report.get("evidence") or {}
            ownership_repair = bool(
                {
                    "atlas_manifest_ownership_conflict",
                    "atlas_manifest_resolution_conflict",
                }.intersection(failure_reason_codes)
                and isinstance(ownership_evidence, dict)
                and ownership_evidence.get("ownership_plan")
            )
            atlas_metadata_only = bool(
                failure_stage == "atlas_manifest_resolution"
                or (ownership_repair and automatic)
            )
            canonical_mapping_repair = (
                failure_stage == "canonical_material_mapping"
            )
            if automatic and (
                atlas_metadata_only or canonical_mapping_repair
            ):
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
                    repair_action=(
                        STEP3_STANDARD
                        if canonical_mapping_repair
                        else ATLAS_MANIFEST_MIRROR_REPAIR
                    ),
                )
            if atlas_metadata_only:
                # Canonical publication is a metadata mutation. Multiple or
                # disagreeing Providers do not invalidate current SpeedTree
                # content, so skip only this write and continue into the fresh
                # material/Cluster audit. Concrete canonical texture failures
                # use another stage and remain actionable errors below.
                diagnostic = {
                    "status": "diagnostic_only",
                    "mutation_authorized": False,
                    "target_spm": str(spm),
                    "canonical_manifest": None,
                    "updated": [],
                    "current": [],
                    "pending": [],
                    "atlas_manifest_diagnostic": failure_report,
                }
                self.log(
                    "[Atlas metadata diagnostic] canonical publication "
                    f"skipped; live audit continues: {spm.name}"
                )
                return diagnostic
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

    def _material_preflight_cache_context(
        self,
        fbx_ini,
        speedtree_cli,
        xml_ini=None,
        speedtree_exe=None,
    ):
        cached = self.__dict__.get("_material_preflight_cache_context_value")
        if isinstance(cached, dict):
            return cached
        # Re-export is an asset operation, not a source-code migration.  The
        # cache module's explicit contract version is the opt-in migration
        # boundary for intentional semantic changes; hashing implementation
        # .py files here made every ordinary code edit re-run the expensive
        # SpeedTree FBX/STMAT export for unchanged data.  Only the actual
        # export preset and installed SpeedTree identity belong in this
        # automatic runtime signature.  A user force-run still bypasses the
        # cache through the existing path.
        semantic_files = tuple(
            Path(path)
            for path in (fbx_ini, xml_ini)
            if path is not None
        )
        cache_dir = Path(
            self.cfg.get("material_preflight_cache_dir")
            or (TOOL_DIR / "cache" / "material_preflight")
        ).expanduser()
        try:
            runtime_signature = material_preflight_runtime_signature(
                semantic_files=semantic_files,
                speedtree_exe=(
                    speedtree_exe or self.cfg["speedtree_exe"]
                ),
            )
        except OSError:
            # Missing test/development inputs disable this optimization.  The
            # existing child process remains the authority for its own error.
            runtime_signature = None
        context = {
            "cache_dir": cache_dir,
            "runtime_signature": runtime_signature,
        }
        self._material_preflight_cache_context_value = context
        return context

    def _material_preflight_seed_candidates(self, spm):
        index = self.__dict__.get("_material_preflight_seed_index")
        if not isinstance(index, dict):
            index = {}
            try:
                paths = LOG_DIR.glob("*_material_preflight_*.json")
                for path in paths:
                    stem = path.name.split("_material_preflight_", 1)[0]
                    index.setdefault(stem.casefold(), []).append(path)
            except OSError:
                index = {}
            def candidate_mtime(path):
                try:
                    return path.stat().st_mtime_ns
                except OSError:
                    return -1
            for candidates in index.values():
                candidates.sort(
                    key=candidate_mtime,
                    reverse=True,
                )
            self._material_preflight_seed_index = index
        return index.get(Path(spm).stem.casefold(), ())

    def _load_or_seed_material_preflight_cache(
        self,
        cache_context,
        spm,
        speedtree_spm,
    ):
        cached = load_material_preflight_cache(
            cache_context["cache_dir"],
            spm,
            speedtree_spm,
            runtime_signature=cache_context["runtime_signature"],
        )
        if cached is not None:
            return cached
        # Adopt an existing successful report once.  Current source/STMAT/FBX
        # content validation is identical to an ordinary durable cache load;
        # foreign, stale, failed or corrupt historical reports are silent
        # misses and never become asset failures.
        for candidate in self._material_preflight_seed_candidates(spm):
            report = load_job_report(candidate)
            try:
                store_material_preflight_cache(
                    cache_context["cache_dir"],
                    spm,
                    speedtree_spm,
                    report,
                    runtime_signature=cache_context["runtime_signature"],
                )
            except (OSError, TypeError, ValueError, RuntimeError):
                continue
            cached = load_material_preflight_cache(
                cache_context["cache_dir"],
                spm,
                speedtree_spm,
                runtime_signature=cache_context["runtime_signature"],
            )
            if cached is not None:
                self.log(
                    "기존 SpeedTree FBX/XML 결과를 재사용 캐시로 등록: "
                    f"{Path(spm).name}"
                )
                return cached
        return None

    def _execute_material_preflight(
        self,
        spm,
        speedtree_spm,
        stamp,
    ):
        """Run the existing fast pre-Blender material contract check."""
        spm = Path(spm)
        speedtree_spm = Path(speedtree_spm)
        configured_fbx_ini = Path(self.cfg["fbx_ini"]).resolve()
        configured_xml_ini = Path(
            self.cfg.get("xml_ini")
            or configured_fbx_ini.with_name("Options_HI_Xml.ini")
        ).resolve()
        # Blender Assembly loads the junction-installed add-on. Material
        # preflight must use that exact helper and its exact preset paths;
        # export cache fingerprints are intentionally path-sensitive. Using
        # the configured source checkout here caused one official-Modeler FBX
        # export, followed by a second collision-CLI FBX/XML export in Assembly.
        installed_addon_dir = Path(ADDON_ENTRY_DIR).absolute()
        try:
            configured_addon_dir = configured_fbx_ini.parents[2]
        except IndexError:
            configured_addon_dir = None
        # Production always prefers the exact Blender junction entry. CI and
        # portable development have no Blender install; only there, use the
        # explicitly configured add-on checkout that owns the supplied preset.
        addon_dir = next(
            (
                candidate
                for candidate in (
                    installed_addon_dir,
                    configured_addon_dir,
                )
                if candidate is not None
                and (candidate / "speedtree_cli.py").is_file()
            ),
            installed_addon_dir,
        )
        speedtree_cli = addon_dir / "speedtree_cli.py"
        preset_dir = addon_dir / "presets" / "speedtree_10_1"
        fbx_ini = preset_dir / configured_fbx_ini.name
        xml_ini = preset_dir / configured_xml_ini.name
        if not fbx_ini.is_file():
            fbx_ini = configured_fbx_ini
        if not xml_ini.is_file():
            xml_ini = configured_xml_ini
        collision_cli = Path(
            os.environ.get("SPEEDTREE_COLLISION_CLI_EXE")
            or (
                REPO_DIR / "speedtree_collision_cli" / "bin"
                / "speedtree_collision_cli.exe"
            )
        ).resolve()
        if not speedtree_cli.is_file():
            raise BatchItemError(
                f"SpeedTree export helper 없음: {speedtree_cli}",
                kind="data_error",
            )
        cache_context = self._material_preflight_cache_context(
            fbx_ini,
            speedtree_cli,
            xml_ini,
            collision_cli,
        )
        material_report = LOG_DIR / (
            f"{spm.stem}_material_preflight_{stamp}.json"
        )
        material_log_name = f"{spm.stem}_material_preflight_{stamp}.log"
        if (
            not getattr(
                self,
                "force_full_rebuild",
                getattr(self, "force_rerun", False),
            )
            and cache_context["runtime_signature"]
        ):
            cached = self._load_or_seed_material_preflight_cache(
                cache_context,
                spm,
                speedtree_spm,
            )
            if cached is not None:
                self.log(
                    "SpeedTree FBX/XML 재사용 (동일 입력 캐시 적중): "
                    f"{spm.name}"
                )
                return {
                    "code": 0,
                    "report": cached["report_path"],
                    "run_report": material_report,
                    "log": cached["receipt_path"],
                    "result": cached["report"],
                    "cache_hit": True,
                }
        export_timeout = max(1, int(
            self.cfg.get("speedtree_material_preflight_timeout", 900)
        ))
        native_process_timeout = max(1, min(export_timeout, int(
            self.cfg.get("speedtree_native_process_timeout", 180)
        )))
        stage_timeout = max(1, int(
            self.cfg.get("child_stage_inactivity_timeout", 180)
        ))
        queue_timeout = max(1, int(
            self.cfg.get("speedtree_material_preflight_queue_timeout", 3600)
        ))
        execution_timeout = export_timeout + max(5, int(
            self.cfg.get("speedtree_material_preflight_cleanup_grace", 30)
        ))
        material_cmd = [
            sys.executable,
            str(TOOL_DIR / "jobs" / "speedtree_material_preflight.py"),
            "--spm", str(speedtree_spm),
            "--canonical-spm", str(spm),
            "--speedtree-exe", str(collision_cli),
            "--fbx-ini", str(fbx_ini),
            "--xml-ini", str(xml_ini),
            "--speedtree-cli", str(speedtree_cli),
            "--report", str(material_report),
            "--timeout", str(export_timeout),
            "--native-process-timeout", str(native_process_timeout),
        ]
        last_progress = {
            "bucket": -1,
            "phase": "선행 작업 시작",
            "phase_started": 0.0,
            "failure_logged": False,
        }
        phase_markers = (
            (MATERIAL_PREFLIGHT_FAILED_MARKER, "실패 보고서 정리 중"),
            (MATERIAL_PREFLIGHT_DONE_MARKER, "완료 처리 중"),
            (MATERIAL_PREFLIGHT_CONTRACT_DONE_MARKER, "보고서 저장 중"),
            (MATERIAL_PREFLIGHT_INSPECTION_DONE_MARKER, "재질 데이터 정리 중"),
            (MATERIAL_PREFLIGHT_EXPORT_DONE_MARKER, "SpeedTree FBX/XML 생성 완료"),
            (SPEEDTREE_SLOT_ACQUIRED_MARKER, "SpeedTree 실행 중"),
            (SPEEDTREE_SLOT_WAIT_MARKER, "SpeedTree 단일 슬롯 대기 중"),
            (MATERIAL_PREFLIGHT_STATIC_DONE_MARKER, "정적 계약 완료"),
            (MATERIAL_PREFLIGHT_START_MARKER, "정적 계약 검사 중"),
        )

        def report_material_progress(elapsed, latest_line):
            for marker, label in phase_markers:
                if latest_line.startswith(marker):
                    if last_progress["phase"] != label:
                        last_progress["phase"] = label
                        last_progress["phase_started"] = elapsed
                    if (
                        marker == MATERIAL_PREFLIGHT_FAILED_MARKER
                        and not last_progress["failure_logged"]
                    ):
                        self.log(
                            "SpeedTree FBX/XML child 실패 단계 보고: "
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
                f"SpeedTree FBX/XML 상태: {spm.name} · {phase} "
                f"· 단계 {int(max(0.0, elapsed - last_progress['phase_started']))}초 "
                f"· 전체 {int(elapsed)}초"
            )

        material_code, material_log = self._run_limited(
            material_cmd,
            material_log_name,
            # Queue wait and acquired execution have independent marker-based
            # deadlines; there is intentionally no combined whole-job limit.
            None,
            # Honor the configured CPU budget even when several item workers
            # are active. Duplicate audits are eliminated separately; letting
            # every child escape affinity only creates machine-wide spikes.
            affinity=True,
            progress_callback=report_material_progress,
            inactivity_timeout=stage_timeout,
            inactivity_timeout_by_marker=material_preflight_inactivity_rules(
                stage_timeout, queue_timeout, execution_timeout
            ),
        )
        material_result = load_job_report(material_report)
        if (
            material_code == 0
            and material_result.get("status") == "ok"
            and cache_context["runtime_signature"]
        ):
            try:
                store_material_preflight_cache(
                    cache_context["cache_dir"],
                    spm,
                    speedtree_spm,
                    material_result,
                    runtime_signature=cache_context["runtime_signature"],
                )
            except (OSError, TypeError, ValueError, RuntimeError) as exc:
                # Cache publication is an optimization.  The authoritative
                # just-completed report still proceeds to Blender Assembly.
                self.log(
                    "  [캐시 기록 경고] SpeedTree FBX/XML 결과는 유효하지만 "
                    f"재사용 캐시를 기록하지 못함: {spm.name} · "
                    f"{compact_error_message(exc)}"
                )
        return {
            "code": material_code,
            "report": material_report,
            "run_report": material_report,
            "log": material_log,
            "result": material_result,
            "cache_hit": False,
        }

    def _refresh_cluster_source_relations(self, spm, item):
        """Regenerate stale provider outputs without rebuilding current Assembly."""
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
                "status": (
                    "pass_through"
                    if state.get("metadata_diagnostic_targets")
                    else "ok"
                ),
                "no_change": True,
                "targets": state["targets"],
                "reason": (
                    "provider_metadata_diagnostic_only"
                    if state.get("metadata_diagnostic_targets")
                    else "already_current"
                ),
                "metadata_diagnostic_targets": list(
                    state.get("metadata_diagnostic_targets") or ()
                ),
            }
        actionable_targets = state.get("actionable_targets")
        if actionable_targets is None:
            # Compatibility for older cached/test projections.  Current
            # production states always publish the exact mutation subset.
            actionable_targets = targets
        actionable_targets = [
            Path(value).expanduser().absolute()
            for value in actionable_targets
        ]
        if not actionable_targets:
            return {
                "status": "pass_through",
                "no_change": True,
                "reason": "provider_metadata_diagnostic_only",
                "targets": state.get("targets") or [],
                "metadata_diagnostic_targets": list(
                    state.get("metadata_diagnostic_targets") or ()
                ),
            }
        self.log(
            f"Cluster Normalizer/Atlas 자동 재생성: {Path(spm).name}"
            f" · {state['reason']}"
        )
        try:
            with cluster_relation_owner_lock(spm):
                result = run_cluster_relation_transaction(
                    blend_path_for(spm),
                    actionable_targets,
                    enabled=True,
                    blender_exe=Path(self.cfg["blender_exe"]),
                    unit_probe_path=Path(self.cfg["cluster_unit_probe"]),
                    capture_resolution=int(
                        self.cfg.get("cluster_capture_resolution", 1024)
                    ),
                    assembly_runtime_config=self.cfg,
                    timeout=int(
                        self.cfg.get("blender_job_timeout", 3600)
                    ),
                )
        except Exception as exc:
            diagnostic = {
                "status": "pass_through",
                "no_change": True,
                "reason": "cluster_refresh_orchestration_diagnostic",
                "targets": state["targets"],
                "attempted": True,
                "error": compact_error_message(exc),
                "asset_failure": False,
            }
            self.log(
                "Cluster Normalizer/Atlas 자동 재생성은 진단으로만 남기고 "
                f"fresh pipeline을 계속합니다: {Path(spm).name} · "
                f"{diagnostic['error']}"
            )
            return diagnostic
        verified = cluster_relation_refresh_state(spm, targets)
        if not verified["current"]:
            diagnostic = {
                "status": "pass_through",
                "no_change": True,
                "reason": "cluster_refresh_reaudit_diagnostic",
                "targets": verified["targets"],
                "attempted": True,
                "result": result,
                "fresh_audit": verified,
                "asset_failure": False,
            }
            self.log(
                "Cluster Normalizer/Atlas fresh audit는 현재 live content "
                f"pipeline으로 이관합니다: {Path(spm).name} · "
                f"{verified['reason']}"
            )
            return diagnostic
        self.log(f"Cluster Normalizer/Atlas 갱신 완료: {Path(spm).name}")
        return result

    def _reset_cluster_receipt_refresh_memo(self):
        """Discard the process-local Cluster live-audit memo.

        The memo lives for the GUI verification session and is reset only for
        an explicit scan (or when the app exits).  A hit is never trusted by
        age alone: every caller re-fingerprints production inputs and live
        artifacts before reuse.
        """
        self._cluster_receipt_refresh_memo_lock = threading.Lock()
        self._cluster_receipt_refresh_memo = {}
        self._cluster_receipt_refresh_flights = {}
        self._cluster_receipt_owner_locks = {}

    def _ensure_cluster_receipt_refresh_memo(self):
        if not hasattr(self, "_cluster_receipt_refresh_memo_lock"):
            self._reset_cluster_receipt_refresh_memo()

    @staticmethod
    def _cluster_live_audit_cache_paths(scope):
        digest = hashlib.sha256(scope.encode("utf-8")).hexdigest()
        directory = LOG_DIR.parent / "cache" / "cluster_live_audit"
        base = directory / f"cluster_live_audit_{digest}"
        return base.with_suffix(".json"), base.with_name(
            base.name + "_report.json"
        )

    @staticmethod
    def _cluster_live_audit_cache_digest(payload):
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def _load_cluster_live_audit_cache(
        self,
        scope,
        production_source_revision,
    ):
        """Load an exact prior observation; current bytes are checked later."""
        cache_path, report_path = self._cluster_live_audit_cache_paths(scope)
        try:
            payload = json.loads(cache_path.read_text(encoding="utf-8"))
            recorded_digest = str(payload.pop("cache_sha256"))
        except (OSError, TypeError, ValueError, KeyError):
            return None
        if (
            payload.get("kind") != CLUSTER_LIVE_AUDIT_CACHE_KIND
            or payload.get("version") != CLUSTER_LIVE_AUDIT_CACHE_VERSION
            or payload.get("scope") != scope
            or payload.get("production_source_revision")
            != production_source_revision
            or recorded_digest != self._cluster_live_audit_cache_digest(payload)
        ):
            return None
        raw_audit = payload.get("raw_audit")
        live_artifact_paths = payload.get("live_artifact_paths")
        input_records = payload.get("input_records")
        discovery_records = payload.get("discovery_records")
        if (
            not isinstance(raw_audit, dict)
            or not isinstance(raw_audit.get("payload"), dict)
            or not isinstance(live_artifact_paths, list)
            or not all(isinstance(path, str) for path in live_artifact_paths)
            or not isinstance(input_records, list)
            or not all(isinstance(record, dict) for record in input_records)
            or not isinstance(discovery_records, list)
            or not all(
                isinstance(record, dict) for record in discovery_records
            )
            or not str(payload.get("input_fingerprint") or "")
            or not str(payload.get("discovery_fingerprint") or "")
        ):
            return None
        raw_audit = copy.deepcopy(raw_audit)
        # The stable report copy is diagnostic only; the cache envelope owns
        # the immutable payload and all positive reuse is revalidated below.
        raw_audit["audit_report"] = str(
            report_path if report_path.is_file() else cache_path
        )
        return {
            "production_source_revision": production_source_revision,
            "input_fingerprint": payload["input_fingerprint"],
            "discovery_fingerprint": payload["discovery_fingerprint"],
            "live_artifact_paths": tuple(live_artifact_paths),
            "input_records": copy.deepcopy(input_records),
            "discovery_records": copy.deepcopy(discovery_records),
            "raw_audit": raw_audit,
            "_cache_origin": "durable",
        }

    def _store_cluster_live_audit_cache(
        self,
        scope,
        production_manifest,
        cache_entry,
    ):
        """Persist only a child report proven to match this exact code tree."""
        raw_audit = copy.deepcopy(cache_entry.get("raw_audit"))
        if not isinstance(raw_audit, dict):
            return False
        child_payload = raw_audit.get("payload")
        try:
            validate_production_source_revision_report(
                child_payload,
                production_manifest,
            )
        except (CompileGateError, OSError, TypeError, ValueError):
            return False
        cache_path, report_path = self._cluster_live_audit_cache_paths(scope)
        raw_audit["audit_report"] = str(report_path)
        payload = {
            "kind": CLUSTER_LIVE_AUDIT_CACHE_KIND,
            "version": CLUSTER_LIVE_AUDIT_CACHE_VERSION,
            "scope": scope,
            "production_source_revision": str(
                production_manifest.content_hash
            ),
            "input_fingerprint": str(cache_entry["input_fingerprint"]),
            "discovery_fingerprint": str(
                cache_entry["discovery_fingerprint"]
            ),
            "live_artifact_paths": list(
                cache_entry.get("live_artifact_paths") or ()
            ),
            "input_records": copy.deepcopy(
                cache_entry.get("input_records") or []
            ),
            "discovery_records": copy.deepcopy(
                cache_entry.get("discovery_records") or []
            ),
            "raw_audit": raw_audit,
        }
        payload["cache_sha256"] = self._cluster_live_audit_cache_digest(
            payload
        )
        try:
            atomic_write_json(report_path, child_payload)
            atomic_write_json(cache_path, payload)
        except (OSError, TypeError, ValueError):
            return False
        return True

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
    def _cluster_receipt_live_artifact_seed_records(cls, payload):
        """Normalize child-produced digests for immediate parent adoption."""
        grouped = {}
        for record in cls._cluster_receipt_live_artifact_records(payload):
            key = os.path.normcase(
                os.path.abspath(str(Path(record["path"]).expanduser()))
            )
            grouped.setdefault(key, []).append(record)
        seeds = []
        algorithm_priority = (
            SAMPLED_FINGERPRINT_ALGORITHM,
            SHA256_ALGORITHM,
            LEGACY_FINGERPRINT_ALGORITHM,
        )
        for path_key, records in grouped.items():
            sizes = {
                int(record["size"])
                for record in records
                if record.get("size") is not None
            }
            mtimes = {
                int(record["mtime_ns"])
                for record in records
                if record.get("mtime_ns") is not None
            }
            if len(sizes) != 1 or len(mtimes) != 1:
                continue
            by_algorithm = {}
            for record in records:
                try:
                    content_key = artifact_record_content_key(record)
                except (TypeError, ValueError):
                    continue
                if content_key is None:
                    continue
                by_algorithm.setdefault(content_key["algorithm"], set()).add(
                    content_key["digest"].casefold()
                )
            selected = next(
                (
                    (algorithm, next(iter(by_algorithm[algorithm])))
                    for algorithm in algorithm_priority
                    if len(by_algorithm.get(algorithm) or ()) == 1
                ),
                None,
            )
            if selected is None:
                continue
            algorithm, digest = selected
            seeds.append({
                "path": path_key,
                "exists": True,
                "fingerprint": digest,
                "fingerprint_algorithm": algorithm,
                "size": next(iter(sizes)),
                "mtime_ns": next(iter(mtimes)),
            })
        return tuple(seeds)

    @classmethod
    def _cluster_receipt_live_artifacts_match(
        cls,
        payload,
        *,
        verified_records=(),
    ):
        """Verify each physical artifact once, even if the report repeats it."""
        errors = []
        verified_by_path = {
            str(record.get("path")): record
            for record in verified_records or ()
            if isinstance(record, dict) and record.get("path")
        }
        grouped = {}
        for record in cls._cluster_receipt_live_artifact_records(payload):
            candidate = Path(record["path"]).expanduser()
            key = os.path.normcase(os.path.abspath(str(candidate)))
            grouped.setdefault(key, []).append(record)
        for path_key, records in grouped.items():
            candidate = Path(path_key)
            exists = candidate.exists()
            expected_exists = {
                bool(record.get("exists"))
                for record in records
                if "exists" in record
            }
            if len(expected_exists) > 1:
                errors.append(f"conflicting exists evidence: {candidate}")
                continue
            if expected_exists and next(iter(expected_exists)) != exists:
                errors.append(f"exists changed: {candidate}")
                continue
            if not exists:
                continue
            try:
                stat = candidate.stat()
                expected_sizes = {
                    int(record["size"])
                    for record in records
                    if record.get("size") is not None
                }
                if len(expected_sizes) > 1:
                    errors.append(f"conflicting size evidence: {candidate}")
                    continue
                if expected_sizes and next(iter(expected_sizes)) != stat.st_size:
                    errors.append(f"size changed: {candidate}")
                    continue
                expected_mtimes = {
                    int(record["mtime_ns"])
                    for record in records
                    if record.get("mtime_ns") is not None
                }
                if len(expected_mtimes) > 1:
                    errors.append(f"conflicting mtime evidence: {candidate}")
                    continue
                if (
                    expected_mtimes
                    and next(iter(expected_mtimes)) != stat.st_mtime_ns
                ):
                    errors.append(f"mtime changed: {candidate}")
                    continue
                expected_by_algorithm = {}
                fields_by_algorithm = {}
                for record in records:
                    content_key = artifact_record_content_key(record)
                    if content_key is None:
                        errors.append(f"content digest missing: {candidate}")
                        continue
                    algorithm = content_key["algorithm"]
                    expected_by_algorithm.setdefault(algorithm, set()).add(
                        content_key["digest"].casefold()
                    )
                    fields_by_algorithm.setdefault(
                        algorithm,
                        content_key["field"],
                    )
                if any(
                    len(expected_digests) != 1
                    for expected_digests in expected_by_algorithm.values()
                ):
                    errors.append(f"conflicting content evidence: {candidate}")
                    continue
                verified = verified_by_path.get(path_key)
                if isinstance(verified, dict):
                    verified_identity = _fast_file_change_identity(
                        candidate,
                        stat,
                    )
                    if (
                        verified.get("exists") is True
                        and verified.get("change_token") is not None
                        and all(
                            verified.get(field)
                            == verified_identity.get(field)
                            for field in (
                                "size",
                                "mtime_ns",
                                "device",
                                "file_id",
                                "change_token",
                            )
                        )
                    ):
                        continue
                for algorithm, expected_digests in expected_by_algorithm.items():
                    current_key = file_content_key_snapshot(
                        candidate,
                        algorithm,
                    )
                    if (
                        next(iter(expected_digests))
                        != current_key["digest"].casefold()
                    ):
                        errors.append(
                            f"{fields_by_algorithm[algorithm]} changed: "
                            f"{candidate}"
                        )
            except (OSError, RuntimeError, TypeError, ValueError) as exc:
                errors.append(f"identity unavailable: {candidate}: {exc}")
        return not errors, tuple(errors)

    @staticmethod
    def _cluster_receipt_discovery_input_paths(spm):
        """Discover only contract inputs, never Assembly/runtime report JSON."""
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

        for current_root, directories, filenames in os.walk(owner):
            current = Path(current_root)
            relative_parts = {
                part.casefold()
                for part in current.relative_to(owner).parts
            }
            if relative_parts & {
                "_spm_backups",
                ".sk_batch_isolated_bark",
            }:
                directories[:] = []
                continue
            directories[:] = [
                name
                for name in directories
                if name.casefold()
                not in {"_spm_backups", ".sk_batch_isolated_bark"}
            ]
            in_atlas_identity_folder = current.name.casefold() in {
                ".atlas_leaf_speedtree_scopes",
                ".atlas_leaf_speedtree_targets",
            }
            for filename in filenames:
                candidate = current / filename
                folded = filename.casefold()
                if folded.endswith(".spm"):
                    if is_live_spm(candidate):
                        paths.add(candidate.resolve())
                    continue
                if not folded.endswith(".json"):
                    continue
                if (
                    folded == "speedtree_import_manifest.json"
                    or folded.endswith(".atlas_leaf_targets.json")
                    or folded.endswith("_auto_capture_manifest.json")
                    or folded.endswith("_normalization_manifest.json")
                    or folded == "bark_normalization_manifest.json"
                    or folded.endswith("_bark_source_manifest.json")
                    or in_atlas_identity_folder
                ):
                    paths.add(candidate.resolve())
        return paths

    @classmethod
    def _cluster_receipt_records_fingerprint(cls, spm, records):
        envelope = {
            "version": 4,
            "scope": cls._cluster_receipt_refresh_scope(spm),
            "files": records,
        }
        encoded = json.dumps(
            envelope,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.blake2b(encoded, digest_size=16).hexdigest()

    def _cluster_receipt_refresh_input_fingerprint(
        self,
        spm,
        *,
        live_artifact_paths=(),
        _records_out=None,
        _discovery_records_out=None,
        _cached_records=(),
        _trusted_records=(),
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
        cached_by_path = {
            str(record.get("path")): record
            for record in _cached_records or ()
            if isinstance(record, dict) and record.get("path")
        }
        trusted_by_path = {
            str(record.get("path")): record
            for record in _trusted_records or ()
            if isinstance(record, dict) and record.get("path")
        }

        live_paths = {
            Path(path).resolve()
            for path in live_artifact_paths
        }
        all_paths = set(live_paths)
        all_paths.update(content_paths)
        all_paths.update(
            Path(record["path"])
            for record in cached_by_path.values()
        )
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
            identity = _fast_file_change_identity(candidate, stat)
            cached_record = cached_by_path.get(key)
            trusted_record = trusted_by_path.get(key)
            cache_identity_matches = bool(
                isinstance(cached_record, dict)
                and cached_record.get("exists") is True
                and identity.get("change_token") is not None
                and all(
                    cached_record.get(field) == identity.get(field)
                    for field in (
                        "size",
                        "mtime_ns",
                        "device",
                        "file_id",
                        "change_token",
                    )
                )
            )
            trusted_identity_matches = bool(
                isinstance(trusted_record, dict)
                and trusted_record.get("exists") is True
                and trusted_record.get("fingerprint")
                and trusted_record.get("size") == identity.get("size")
                and trusted_record.get("mtime_ns") == identity.get("mtime_ns")
            )
            reusable_record = (
                cached_record
                if cache_identity_matches
                else trusted_record if trusted_identity_matches else None
            )
            reused_cached_snapshot = False
            if candidate in content_paths:
                content_reusable_record = (
                    cached_record
                    if cache_identity_matches
                    else (
                        trusted_record
                        if (
                            trusted_identity_matches
                            and trusted_record.get("fingerprint_algorithm")
                            == LEGACY_FINGERPRINT_ALGORITHM
                        )
                        else None
                    )
                )
                if content_reusable_record is not None:
                    snapshot = {
                        "fingerprint": content_reusable_record["fingerprint"],
                        "size": identity["size"],
                        "mtime_ns": identity["mtime_ns"],
                    }
                    if (
                        content_reusable_record.get("fingerprint_algorithm")
                        and content_reusable_record.get(
                            "fingerprint_algorithm"
                        )
                        != LEGACY_FINGERPRINT_ALGORITHM
                    ):
                        snapshot["fingerprint_algorithm"] = reusable_record[
                            "fingerprint_algorithm"
                        ]
                    reused_cached_snapshot = True
                else:
                    snapshot = file_content_snapshot(candidate)
            elif (
                reusable_record is not None
                and reusable_record.get("fingerprint_algorithm")
            ):
                snapshot = {
                    "fingerprint": reusable_record["fingerprint"],
                    "fingerprint_algorithm": reusable_record[
                        "fingerprint_algorithm"
                    ],
                    "size": identity["size"],
                    "mtime_ns": identity["mtime_ns"],
                }
                reused_cached_snapshot = True
            else:
                snapshot = sampled_file_content_snapshot(candidate)
            if reused_cached_snapshot:
                final_identity = identity
            else:
                final_stat = candidate.stat()
                final_identity = _fast_file_change_identity(
                    candidate,
                    final_stat,
                )
            if (
                snapshot.get("size") != final_identity["size"]
                or snapshot.get("mtime_ns") != final_identity["mtime_ns"]
            ):
                raise RuntimeError(f"File changed while fingerprinting: {candidate}")
            records.append({
                "path": key,
                "exists": True,
                **snapshot,
                "device": final_identity["device"],
                "file_id": final_identity["file_id"],
                "change_token": final_identity["change_token"],
            })

        if _records_out is not None:
            _records_out[:] = copy.deepcopy(records)
        if _discovery_records_out is not None:
            content_keys = {
                os.path.normcase(os.path.abspath(str(path)))
                for path in content_paths
            }
            _discovery_records_out[:] = copy.deepcopy([
                record
                for record in records
                if record.get("path") in content_keys
            ])
        return self._cluster_receipt_records_fingerprint(spm, records)

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
        *,
        _raw_only=False,
        _persist_receipt=True,
    ):
        """Reuse one hash-current live audit within this GUI session.

        Successful raw audits only are memoized. Concurrent callers for the
        same owner/input share one Future; an execution exception reaches all
        existing waiters but is removed immediately so a later caller may
        retry.
        """
        spm = Path(spm).resolve()
        production_manifest = self._assert_active_production_source_manifest()
        production_source_revision = str(production_manifest.content_hash)
        force_rerun = bool(getattr(self, "force_rerun", False))
        if not (spm.parent / "Cluster").is_dir():
            return self._refresh_stale_cluster_receipt_uncached(
                spm,
                stamp,
            )

        self._ensure_cluster_receipt_refresh_memo()
        asset_scope = self._cluster_receipt_refresh_scope(spm)
        # A normalization observation deliberately disables receipt writes.
        # Do not let it suppress a later owner audit that is expected to
        # publish one, while still allowing input/output observations to share
        # the same exact live result when no asset byte changed between them.
        scope = (
            asset_scope
            if _persist_receipt
            else asset_scope + "\nreceipt_persistence=disabled"
        )
        cached_hit = False
        cached_raw_audit = None
        while True:
            with self._cluster_receipt_refresh_memo_lock:
                cached = (
                    None
                    if force_rerun
                    else self._cluster_receipt_refresh_memo.get(scope)
                )
                if cached is None and not force_rerun:
                    cached = self._load_cluster_live_audit_cache(
                        scope,
                        production_source_revision,
                    )
                    if cached is not None:
                        self._cluster_receipt_refresh_memo[scope] = cached
                cached_live_artifact_paths = (
                    (cached or {}).get("live_artifact_paths") or ()
                )
            pre_input_records = []
            pre_discovery_records = []
            try:
                current_cache_fingerprint = (
                    self._cluster_receipt_refresh_input_fingerprint(
                        spm,
                        live_artifact_paths=cached_live_artifact_paths,
                        _records_out=pre_input_records,
                        _discovery_records_out=pre_discovery_records,
                        _cached_records=(cached or {}).get(
                            "input_records"
                        ) or (),
                    )
                )
                pre_discovery_fingerprint = (
                    self._cluster_receipt_records_fingerprint(
                        spm,
                        pre_discovery_records,
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
                if (
                    cached.get("input_fingerprint")
                    == current_cache_fingerprint
                    and cached.get("discovery_fingerprint")
                    == pre_discovery_fingerprint
                ):
                    # Every recorded path retained the exact file/change token
                    # that authorized reuse of its prior content digest.  The
                    # fresh-audit publish gate already checked every payload
                    # artifact, so repeating 758 report rows here is redundant.
                    cached_artifacts_match = True
                else:
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
                if (
                    not force_rerun
                    and self._cluster_receipt_refresh_memo.get(scope)
                    is not cached
                ):
                    continue
                if (
                    cached is not None
                    and cached.get("production_source_revision")
                    == production_source_revision
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
                elif force_rerun:
                    # A force run must execute an independent live audit.
                    # Keep the existing retry/stability wrapper, but neither
                    # consume nor publish a session memo or shared flight.
                    flight_key = None
                    flight = Future()
                    owns_flight = True
                else:
                    flight_key = (
                        scope,
                        production_source_revision,
                        pre_discovery_fingerprint,
                    )
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
                if cached.get("_cache_origin") == "durable":
                    self.log(
                        "Cluster Assembly live audit exact cache hit: "
                        f"{spm.name}"
                    )
                else:
                    self.log(
                        "Cluster Assembly live audit memo hit: "
                        f"{spm.name}"
                    )
            cached_raw_audit = copy.deepcopy(cached_raw_audit)
            if _raw_only:
                return cached_raw_audit
            return self._evaluate_cluster_receipt_live_audit(
                spm,
                cached_raw_audit,
            )
        if not owns_flight:
            raw_audit = copy.deepcopy(
                self._wait_cluster_receipt_refresh_flight(flight, spm)
            )
            if _raw_only:
                return raw_audit
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
                        _persist_receipt=_persist_receipt,
                    )
                except InlineAtlasRepairRequested as repair_request:
                    self._run_inline_atlas_manifest_repair(
                        repair_request.target_spm,
                        repair_request.report,
                    )
                    # The read-only audit consumes Atlas disagreement as
                    # diagnostics while retaining the identity-bound Assembly
                    # graph.  Even if the optional exact metadata refresh did
                    # not change anything, this second run returns real live
                    # contract evidence rather than a synthetic pass-through.
                    raw_audit = self._refresh_stale_cluster_receipt_uncached(
                        spm,
                        f"{audit_stamp}_atlas_repaired",
                        _raw_only=True,
                        _persist_receipt=_persist_receipt,
                    )
                live_artifact_paths = (
                    self._cluster_receipt_live_artifact_paths(
                        raw_audit.get("payload") or {},
                    )
                )
                child_artifact_seeds = (
                    self._cluster_receipt_live_artifact_seed_records(
                        raw_audit.get("payload") or {},
                    )
                )
                post_input_records = []
                post_discovery_records = []
                post_cache_fingerprint = (
                    self._cluster_receipt_refresh_input_fingerprint(
                        spm,
                        live_artifact_paths=live_artifact_paths,
                        _records_out=post_input_records,
                        _discovery_records_out=post_discovery_records,
                        _trusted_records=child_artifact_seeds,
                    )
                )
                post_discovery_fingerprint = (
                    self._cluster_receipt_records_fingerprint(
                        spm,
                        post_discovery_records,
                    )
                )
                artifacts_match, artifact_errors = (
                    self._cluster_receipt_live_artifacts_match(
                        raw_audit.get("payload") or {},
                        verified_records=post_input_records,
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
                    final_input_records = []
                    final_discovery_records = []
                    final_cache_fingerprint = (
                        self._cluster_receipt_refresh_input_fingerprint(
                            spm,
                            live_artifact_paths=live_artifact_paths,
                            _records_out=final_input_records,
                            _discovery_records_out=final_discovery_records,
                            _cached_records=post_input_records,
                        )
                    )
                    final_discovery_fingerprint = (
                        self._cluster_receipt_records_fingerprint(
                            spm,
                            final_discovery_records,
                        )
                    )
                    final_artifacts_match, final_artifact_errors = (
                        (
                            True,
                            (),
                        )
                        if final_cache_fingerprint == post_cache_fingerprint
                        else self._cluster_receipt_live_artifacts_match(
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
                        final_manifest = (
                            self._assert_active_production_source_manifest()
                        )
                        final_revision = str(final_manifest.content_hash)
                        if (
                            not force_rerun
                            and final_revision == production_source_revision
                        ):
                            cache_entry = {
                                "production_source_revision": (
                                    production_source_revision
                                ),
                                "input_fingerprint": final_cache_fingerprint,
                                "discovery_fingerprint": (
                                    final_discovery_fingerprint
                                ),
                                "live_artifact_paths": live_artifact_paths,
                                "input_records": final_input_records,
                                "discovery_records": final_discovery_records,
                                "raw_audit": copy.deepcopy(raw_audit),
                            }
                        elif final_revision != production_source_revision:
                            self.log(
                                "Cluster Assembly live audit memo not "
                                "published after production source revision "
                                f"changed: {spm.name}"
                            )
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
                self.log(
                    f"{failure_message}; accepting fresh audit without "
                    f"memoization: {spm.name}"
                )
                break
        except Exception as exc:
            completion_error = exc

        publish_error = completion_error
        with self._cluster_receipt_refresh_memo_lock:
            try:
                if (
                    publish_error is None
                    and not force_rerun
                    and cache_entry is not None
                ):
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
                    flight_key is not None
                    and self._cluster_receipt_refresh_flights.get(flight_key)
                    is flight
                ):
                    self._cluster_receipt_refresh_flights.pop(
                        flight_key,
                        None,
                    )

        if publish_error is not None:
            raise publish_error
        if cache_entry is not None and not force_rerun:
            self._store_cluster_live_audit_cache(
                scope,
                production_manifest,
                cache_entry,
            )
        if _raw_only:
            return raw_audit
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
            if not owner_has_cluster:
                try:
                    return cluster_assembly_receipt_resolution(spm)
                except FileNotFoundError:
                    return None
                except (
                    ClusterAssemblyReceiptStaleError,
                    ClusterAssemblyReceiptAmbiguityError,
                ):
                    # A removed Cluster folder can still have an old actionable
                    # receipt. Run one live pass-through audit to retire that
                    # relationship instead of accepting the stale snapshot.
                    self.log(
                        "Cluster Assembly removed-relation live 감사: "
                        f"{spm.name}"
                    )
            else:
                # A live audit is authoritative for owners. Resolving an old
                # persisted receipt here used to hash ~675 MB before launching
                # the audit that was going to replace that evidence anyway.
                self.log(f"Cluster Assembly live contract 감사: {spm.name}")

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
                "--cluster-assembly-only",
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
                # Healthy audits finish in roughly 1-2 seconds. Do not turn
                # those normal completions into noisy per-item heartbeats.
                if elapsed < 5.0:
                    return
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
                affinity=True,
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
                    raise BatchItemError(
                        "Cluster Assembly child returned a strict Atlas "
                        "metadata failure instead of a diagnostic live "
                        f"contract: {spm.name}",
                        kind="internal_error",
                        report={
                            "stage": "cluster_assembly_live_audit",
                            "asset_failure": False,
                            "unexpected_strict_atlas_failure": copy.deepcopy(
                                failure_report
                            ),
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
                    "provider_spms": copy.deepcopy(
                        audit_block.get("provider_spms") or []
                    ),
                    "cluster_provider_relations": copy.deepcopy(
                        audit_block.get("cluster_provider_relations") or []
                    ),
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
            terminal_issues = [
                issue
                for issue in live_issues
                if str(issue.get("code") or "").strip().upper()
                not in _LEGACY_NONBLOCKING_NORMALIZATION_CODES
            ]
            if terminal_issues:
                terminal_summary = cluster_issue_summary(terminal_issues)
                decision = repair_ui_decision({"issues": terminal_issues})
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
                        "issues": copy.deepcopy(terminal_issues),
                        "internal_summary": terminal_summary,
                    },
                    log_file=log_file,
                    report_file=audit_report,
                )
            # Preserve old rows for diagnostics only.  They are intentionally
            # excluded before admission and cannot trigger repair or failure.
            raw_audit["nonblocking_maintenance_issues"] = copy.deepcopy(
                live_issues
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
            # A clean identity-bound pass-through still has to reach Assembly so an
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
                    "nonblocking_maintenance_issues": copy.deepcopy(
                        raw_audit.get("nonblocking_maintenance_issues") or []
                    ),
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
        persisted_paths = list(persistence.get("written") or ()) + list(
            persistence.get("unchanged") or ()
        )
        if persistence_error:
            self.log(
                "Cluster Assembly live audit 사용 "
                f"(영수증은 캐시 경고): {spm.name}: "
                + persistence_error
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
            "nonblocking_maintenance_issues": copy.deepcopy(
                raw_audit.get("nonblocking_maintenance_issues") or []
            ),
            "persisted_receipt": (
                persisted_paths[0] if persisted_paths else None
            ),
            "current_candidates": [],
            "superseded_current_receipts": [],
            "ignored_stale_candidates": [],
            "receipt_persistence_warning": (
                persistence_error
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
        raw_audit = self._refresh_stale_cluster_receipt(
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
            issue_code = str(issue.get("code") or "").strip().upper()
            if issue_code in _LEGACY_NONBLOCKING_NORMALIZATION_CODES:
                # Deleted policy: old receipts may still contain these rows,
                # but they are not producer work and must not become
                # `normalization_required` after restart or cache recovery.
                continue
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

        blocking = global_issues + owned_issues
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
            # The old per-gate allowlist meant new repairable reasons printed
            # 자동 복구 대상 and then terminated the target (#160).
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
        for relation in (
            durable_evidence.get("cluster_provider_relations") or ()
        ):
            if isinstance(relation, dict):
                add_inventory(relation.get("provider_spm"))
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

    def _publish_current_assembly_skip(
        self,
        iid,
        spm,
        repair_state,
        *,
        validated_resume_receipt=None,
        relation_validated=False,
    ):
        """Publish one exact current Assembly result without rediscovering work."""
        if not isinstance(repair_state, dict) or not repair_state.get("current"):
            return False
        self._record_live_blend_status(
            iid,
            spm,
            repair_state=repair_state,
            validated_resume_receipt=validated_resume_receipt,
            relation_validated=relation_validated,
        )
        suffix = (
            " · Unreal Push 차단 상태 유지"
            if not repair_state.get("push_ready")
            else ""
        )
        self._publish_assembly_stage_contract(
            spm,
            ready=repair_state.get("push_ready") is True,
            reason=repair_state.get("reason"),
            kind=repair_state.get("kind"),
            push_dependency_contract=repair_state.get(
                "push_dependency_contract"
            ),
        )
        self.log(f"건너뜀 (blend 최신{suffix}): {Path(spm).name}")
        return True

    def _job_blender(self, iid, spm, item):
        spm = self._prepare_pair_for_job(spm)
        cluster_source = is_cluster_source_spm(spm)
        relation_sensitive = bool(
            cluster_source
            or (speedtree_output_spm_for(spm).parent / "Cluster").is_dir()
        )
        resume_policy = str(
            item.get("_blender_resume_policy") or ""
        )
        if (
            not self.force_rerun
            and not resume_policy
            and not relation_sensitive
        ):
            # The shared Assembly decision already validates the exact SPM,
            # blend, Assembly report, material/wind output and exact dependency
            # artifacts.  Consult it before relation audits or material
            # preflight for ordinary non-Cluster rows. Relation rows
            # reach this worker only when their fast receipt was unavailable
            # or changed, so they must refresh the relation once here.
            # Explicit force rebuild deliberately bypasses this fast path.
            repair_state = self._assembly_output_state(spm)
            if self._publish_current_assembly_skip(
                iid,
                spm,
                repair_state,
            ):
                return
        # Canonical PCG publication is owned by the PCG pipeline (or its
        # explicit repair action).  A Blender assembly job consumes the live
        # SPM/FBX/material inputs and must not be blocked by unrelated Atlas
        # publication metadata before those inputs are even inspected.
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
        # The material-preflight child performs both leaf and external-mesh
        # checks before it enters the shared SpeedTree export gate. Repeating
        # those exact SPM parses here doubled the read cost without providing
        # an earlier safety boundary.
        cluster_receipt_resolution = None
        cluster_receipt_resolved = False

        def resolve_cluster_receipt_once():
            nonlocal cluster_receipt_resolution, cluster_receipt_resolved
            if not cluster_receipt_resolved:
                if (speedtree_spm.parent / "Cluster").is_dir():
                    cluster_receipt_resolution = (
                        self._cluster_receipt_with_recovery(
                            speedtree_spm,
                            stamp,
                        )
                    )
                else:
                    # Ordinary vegetation and provider rows have no owner
                    # Assembly audit. Do not scan/hash the global receipt
                    # directory merely to prove that absence.
                    cluster_receipt_resolution = None
                cluster_receipt_resolved = True
            return cluster_receipt_resolution

        if not self.force_rerun:
            if resume_policy == "live_validation" or relation_sensitive:
                # Receipt drift schedules work; it is never an item failure by
                # itself.  Run the existing registered relationship recovery
                # before deciding whether the materialized Blend still needs
                # to be rebuilt.
                resolve_cluster_receipt_once()
            live_contract = cluster_receipt_resolution_uses_live_audit(
                cluster_receipt_resolution
            )
            saved_pipeline = {}
            repair_state = self._assembly_output_state(
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
                        "been captured by Blender Assembly"
                    ),
                }
                self.log(
                    "Cluster canonical bark capture 필요: "
                    f"{spm.name} · 기존 blend 최신 판정 무효화"
                )
            if repair_state["current"] and resume_policy != "rebuild":
                if live_contract:
                    # An actionable live Assembly contract needs a matching
                    # materialized manifest before the old Blend may be
                    # reused.  Pass-through has no Assembly output to embed,
                    # so missing metadata alone must not manufacture a Assembly
                    # rerun.
                    saved_manifest = saved_pipeline.get(
                        "cluster_assembly_manifest"
                    )
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
                        if not isinstance(saved_manifest, dict):
                            # A first resume audit may establish that an
                            # ordinary asset still has no Assembly work.  That
                            # is enough to create the skip receipt; it must not
                            # force a ceremonial Assembly rebuild just to persist a
                            # pass-through manifest.
                            if live_status != "pass_through":
                                repair_state["current"] = False
                        else:
                            saved_status = str(
                                saved_manifest.get("status") or ""
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
                        # This is a skip decision only.  Continue through Assembly
                        # instead of turning relationship metadata into a
                        # separate terminal failure.
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
                            repair_state = self._assembly_output_state(spm)
                    if self._publish_current_assembly_skip(
                        iid,
                        spm,
                        repair_state,
                        relation_validated=cluster_receipt_resolved,
                    ):
                        return
                    self.log(
                        "Cluster 관계 산출물 갱신 후 Assembly 상태가 변경되어 "
                        f"①을 계속 실행: {spm.name}"
                    )
        open_windows = blender_open_file_window_titles(blend)
        if open_windows:
            raise BatchItemError(
                "Assembly 대상 .blend가 대화형 Blender에 열려 있습니다. "
                "저장하거나 닫은 뒤 다시 실행하세요: " + blend.name,
                kind="manual_required",
                report={
                    "status": "blocked",
                    "stage": "interactive_blender_guard",
                    "blend": str(blend),
                    "open_windows": open_windows,
                },
            )
        entry = self.state.setdefault(iid, {})
        self.log(f"SpeedTree FBX/XML 준비 시작: {spm.name} (Blender 실행 전)")
        self.ui_queue.put((
            "cell",
            (iid, "blend_status", "SpeedTree FBX/XML 준비 중..."),
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
        # Resolve the hash-current persisted Cluster receipt only after the
        # material contract is available.  A durable preflight cache hit keeps
        # its old run-specific live marker disabled, so this is a cheap receipt
        # lookup unless exact recovery is genuinely required.
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
                if artifact.get("cache_hit"):
                    # Never mutate the durable cache report with run-specific
                    # live evidence.  Materialize a report only for the rare
                    # exact-recovery run that actually needs new embedding.
                    material_report = artifact["run_report"]
                atomic_write_json(material_report, material_result)
            except (OSError, TypeError, ValueError) as exc:
                raise BatchItemError(
                    "Cluster Assembly live audit contract could not be "
                    f"embedded for Blender Assembly: {spm.name}: {exc}",
                    kind="internal_error",
                    report_file=live_report,
                ) from exc
        self.log(f"SpeedTree FBX/XML 준비 완료: {spm.name}")
        self.log(f"Blender Assembly 시작: {spm.name} (수분 소요될 수 있음)")
        self.ui_queue.put(("cell", (iid, "blend_status", "Blender Assembly 중...")))
        wind = item["wind_override"]
        if wind == "auto":
            wind = wind_preset_for_spm(spm)
        job_report = LOG_DIR / f"{spm.stem}_assembly_{stamp}.json"
        pipeline_report = (
            spm.parent / "reports" /
            f"{spm.stem}_speedtree_assembly_pipeline_report_codex.json"
        )
        try:
            previous_pipeline_report = pipeline_report.read_bytes()
        except OSError:
            previous_pipeline_report = None
        runtime_receipt = self._assembly_runtime_receipt_path(spm)
        try:
            previous_runtime_receipt = runtime_receipt.read_bytes()
        except OSError:
            previous_runtime_receipt = None
        cluster_blend_backup = None
        cluster_blend_existed = cluster_source and blend.is_file()
        cluster_source_build_committed = False
        if cluster_blend_existed:
            cluster_blend_backup = (
                LOG_DIR / f"{spm.stem}_pre_assembly_{stamp}.blend"
            )
            # Startup retention already cleaned the managed roots.  A repair
            # item must not re-scan every managed artifact while holding the
            # global retention mutex just to make its transaction backup.
            # That old hot-path reservation serialized otherwise independent
            # Blender workers and could fail an item after a 120-second wait.
            shutil.copy2(blend, cluster_blend_backup)

        def restore_cluster_assembly_outputs():
            # This backup belongs only to the raw Assembly producer transaction.
            # Once Assembly has committed a ready Cluster source, the downstream
            # Normalizer/Atlas transaction owns its own snapshots.  Restoring
            # this older backup after that boundary would discard the valid
            # source blend/report and make the next run rebuild Assembly again.
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
            "--python", str(TOOL_DIR / "jobs" / "assembly_headless_job.py"), "--",
            "--spm", str(spm),
            "--speedtree-spm", str(speedtree_spm),
            "--blend", str(blend),
            "--wind", wind,
            "--material-contract", str(material_report),
            "--report", str(job_report),
        ]
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
        self._assert_active_production_source_manifest()
        try:
            code, log_file = self._run_limited(
                cmd,
                f"{spm.stem}_assembly_{stamp}.log",
                self.cfg.get("blender_job_timeout", 3600),
                affinity=True,
            )
        finally:
            # A code change while Blender is running is a restart route, never
            # evidence that this asset failed its repair contract.
            self._assert_active_production_source_manifest()
        result = load_job_report(job_report)
        if code != 0 or result.get("status") != "ok":
            if cluster_source:
                try:
                    restore_cluster_assembly_outputs()
                except OSError as exc:
                    self.log(
                        f"  [Assembly rollback warning] {spm.name}: {exc}"
                    )
            if previous_pipeline_report is not None:
                try:
                    atomic_write_bytes(pipeline_report, previous_pipeline_report)
                    self.log(
                        f"  [① 복구] 실패 전 최신성 보고서 보존: {spm.name}"
                    )
                except OSError as exc:
                    self.log(
                        f"  [① 복구 경고] 이전 보고서 복원 실패: "
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
                    restore_cluster_assembly_outputs()
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
            # Assembly's source blend and producer report are now durable inputs to
            # the separate Normalizer/Atlas transaction.  That transaction
            # still rolls back SPM/Atlas partial outputs (and restores this
            # exact committed source snapshot), but must never cross back into
            # the pre-Assembly backup.
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
                            assembly_runtime_config=self.cfg,
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
                        restored = restore_cluster_assembly_outputs()
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
                "assembly_mode": "standalone_final_handoff",
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
        self._write_assembly_runtime_receipt(spm)
        handoff_state = {}
        handoff_ok, handoff_reason = self._handoff_ready(
            spm,
            state_out=handoff_state,
        )
        blend_status = self._record_live_blend_status(
            iid,
            spm,
            repair_state=handoff_state or None,
            relation_validated=cluster_receipt_resolved,
        )
        source_review = bool(
            result.get("source_review_required")
            or (result.get("handoff_preflight") or {}).get("status")
            == "source_review"
        )
        assembly_contract_reason = (
            "원본/재질 검토 필요 — Unreal Push 차단"
            if source_review
            else handoff_reason
        )
        self._publish_assembly_stage_contract(
            spm,
            ready=handoff_ok and not source_review,
            reason=assembly_contract_reason,
            kind=(
                "source_review"
                if source_review
                else handoff_state.get("kind")
            ),
            push_dependency_contract=handoff_state.get(
                "push_dependency_contract"
            ),
            evidence_bundle=None,
        )
        if not handoff_ok and not source_review:
            if cluster_source:
                try:
                    restore_cluster_assembly_outputs()
                except OSError as exc:
                    self.log(
                        f"  [Assembly rollback warning] {spm.name}: {exc}"
                    )
            raise BatchItemError(
                f"① 완료 후 사전검사 실패: {handoff_reason}",
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
                f"① 완료 · source review 필요 · Unreal Push 차단: "
                f"{spm.name} ({handoff_reason})"
            )
        else:
            push_status = "준비됨 ✓"
            self.ui_queue.put(("cell", (iid, "push_status", push_status)))
            with self.state_lock:
                entry["push_status"] = push_status
                entry["push_status_kind"] = "ready"
                entry.pop("push_status_error", None)
                self._save_state_after_phase_update()
        if cluster_blend_backup is not None:
            try:
                cluster_blend_backup.unlink(missing_ok=True)
            except OSError as exc:
                self.log(
                    f"  [backup cleanup warning] {spm.name}: {exc}"
                )
        for warning in result.get("warnings", []):
            self.log(f"  [① 경고] {spm.name}: {warning}")
        self.log(f"Assembly 완료: {blend.name}")

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
        reused_assembly_contracts = 0
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
            repair_contract = self._assembly_stage_contract(spm)
            contract_failure_kind = None
            if repair_contract is None:
                ok, why = self._handoff_ready(spm)
            else:
                ok = bool(repair_contract["ready"])
                why = str(repair_contract["reason"])
                reused_assembly_contracts += 1
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
            f" · ① 결과 재사용 {reused_assembly_contracts}개"
            if reused_assembly_contracts
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
            "exporting",
            "artifact_waiting",
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
        elif kind in {"exporting", "artifact_waiting"}:
            self._retry_transition(
                iid,
                RETRY_STAGE_SEND2UE,
                progress_message,
                progress=True,
                heartbeat=True,
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

    def _wait_for_send2ue_disk_space(self, iid, export_root, expected_bytes):
        """Wait for D: FBX workspace capacity without entering retention."""
        export_root = Path(export_root)
        volume = Path(export_root.anchor) if export_root.anchor else export_root
        workers = max(1, int(self.cfg.get("blender_parallel_jobs", 2)))
        safety_bytes = 5 * 1024**3
        required_free = int(expected_bytes) * workers + safety_bytes
        started = time.monotonic()
        last_notice_bucket = None
        while True:
            if self.stop_flag.is_set():
                raise BatchItemError(
                    "D: FBX export 공간 대기 취소",
                    kind="cancelled",
                )
            try:
                free_bytes = shutil.disk_usage(volume).free
            except OSError as exc:
                raise BatchItemError(
                    f"FBX export 드라이브를 사용할 수 없음: {volume}: {exc}",
                    kind="data_error",
                ) from exc
            if free_bytes >= required_free:
                return
            elapsed = max(0.0, time.monotonic() - started)
            notice_bucket = int(elapsed // 15)
            if notice_bucket != last_notice_bucket:
                last_notice_bucket = notice_bucket
                status = (
                    "D: FBX export 공간 대기 중... "
                    f"(여유 {free_bytes / 1024**3:.1f} GiB, "
                    f"필요 {required_free / 1024**3:.1f} GiB)"
                )
                self._set_push_state(
                    iid,
                    "artifact_waiting",
                    status,
                    message=status,
                )
                self.ui_queue.put(
                    ("progress", f"{Path(iid).name}: {status}")
                )
                self.log(
                    f"[D: FBX capacity wait] {Path(iid).name}: "
                    f"free={free_bytes}, required={required_free}"
                )
            if self.stop_flag.wait(1.0):
                raise BatchItemError(
                    "D: FBX export 공간 대기 취소",
                    kind="cancelled",
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
        """Code drift allowed after the immutable export is proven current.

        Every path returned by ``_push_dependency_paths`` is executable code,
        not per-asset source data.  Once the parent FBX/JSON/handoff artifacts
        pass their exact content-identity checks, later exporter-code changes
        cannot retroactively change them.  Recovery records that drift and
        rebinds the existing artifacts to the current Unreal runtime.  The
        blend and per-asset Assembly report remain outside this set, so an actual
        source-data change still requires a full Push.
        """
        return list(dict.fromkeys(self._push_dependency_paths()))

    def _push_source_dependency_paths(self, spm=None):
        """Return only content that can change an asset's exported payload.

        Executable code is tracked separately for diagnostics and Unreal
        rebinding. Editing code must not invalidate every immutable FBX/JSON
        export in the batch.
        """
        if spm is None:
            return []
        return [assembly_pipeline_report_path(Path(spm))]

    @staticmethod
    def _push_material_contract(spm):
        """Write a strict, live-validated contract wrapper for Blender Push."""
        spm = Path(spm)
        repair_report = (
            spm.parent / "reports" /
            f"{spm.stem}_speedtree_assembly_pipeline_report_codex.json"
        )
        try:
            payload = json.loads(repair_report.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise RuntimeError(
                f"SpeedTree Assembly contract report could not be read: {exc}"
            ) from exc
        # Push consumes the current repaired blend and current source paths.
        # A recorded source hash/profile belongs to an earlier diagnostic pass
        # and must not veto regeneration of generated Unreal assets.
        envelope = payload.get("speedtree_material_handoff_contract")
        if not isinstance(envelope, dict):
            envelope = payload.get("speedtree_pipeline_contract")
        if not isinstance(envelope, dict):
            raise RuntimeError(
                "SpeedTree Assembly report has no readable material contract"
            )
        material_source_spm = Path(
            str((((envelope.get("source") or {}).get("spm") or {}).get(
                "canonical_path"
            )) or speedtree_output_spm_for(spm))
        ).resolve()
        # This is an execution input, not a historical artifact. Keep exactly one
        # current wrapper per asset so old contracts can never be selected later.
        contract_path = LOG_DIR / (
            f"{spm.stem}_push_material_contract_current.json"
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
                "source_assembly_report": str(repair_report.resolve()),
                "historical_identity_fields_are_diagnostic": True,
            },
        )
        return contract_path

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
        if not push_source_cache_matches_snapshot(
            source_cache, current_snapshot
        ):
            return "Push 재확인 필요 — Blender/콘텐츠 계약 변경"
        if export_cache.get("source_fingerprint") != source_cache.get(
            "fingerprint"
        ):
            return "Push 재확인 필요 — export 영수증 불일치"
        manifest_path = Path(export_cache.get("manifest") or "")
        if not manifest_path.is_file():
            return "Push 재확인 필요 — export manifest 없음"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest_item = next(
                row for row in (manifest.get("items") or ())
                if str((row or {}).get("queue_id")) == iid
            )
        except (OSError, ValueError, StopIteration, TypeError):
            return "Push 재확인 필요 — export manifest 항목 오류"
        if not manifest_item_has_current_skeleton_root_export(manifest_item):
            return "Push 재확인 필요 — Skeleton Root export 계약 갱신"
        export_fingerprint = export_cache.get("fingerprint")
        if success_like:
            if entry.get("push_import_fingerprint") != export_fingerprint:
                return "Push 재확인 필요 — Unreal 결과 불일치"
            return "완료 (현재 최신)"
        return "export 완료 · Unreal 대기"

    def _source_push_fingerprint(
        self,
        blend,
        iid=None,
        *,
        return_record=False,
    ):
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
        if return_record:
            # Retry planning owns an immutable state snapshot, so a cache
            # miss cannot publish the newly computed record back through
            # ``planning.entry``.  Return the fingerprint and its exact
            # record as one value pair; reading the old snapshot record here
            # falsely routes a current Unreal-only retry through Blender.
            return fingerprint, copy.deepcopy(record)
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
        if (
            str(item.get("queue_id")) != iid
            or not manifest_item_has_current_skeleton_root_export(item)
            or not manifest_item_files_match(item)
        ):
            return None
        exported_files = item.get("exported_files") or []
        if exported_files:
            d_export_root = send2ue_export_cache_root().resolve()
            uses_d_export_root = False
            for identity in exported_files:
                path = str((identity or {}).get("path") or "").strip()
                if not path:
                    continue
                try:
                    Path(path).resolve().relative_to(d_export_root)
                    uses_d_export_root = True
                    break
                except (OSError, ValueError):
                    continue
            if not uses_d_export_root:
                # Do not copy or migrate old C: payloads. A current source
                # simply exports once into the new D: FBX workspace.
                return None
        return item

    def _export_manifest_item(self, iid, spm, batch_stamp):
        spm = self._prepare_pair_for_job(spm)
        blend = blend_path_for(spm)
        source_fingerprint = self._source_push_fingerprint(blend, iid)
        cached = self._cached_manifest_item(iid, source_fingerprint)
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

        cache_root = (
            send2ue_export_cache_root()
            / "headless"
            / source_fingerprint
            / spm.stem
        )
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
        self._assert_active_production_source_manifest()
        expected_export_bytes = estimate_output_reservation_bytes(
            blend, minimum_bytes=1024**3, multiplier=4
        )
        try:
            self._wait_for_send2ue_disk_space(
                iid, cache_root, expected_export_bytes
            )
            self._set_push_state(
                iid,
                "exporting",
                "Send2UE export 중...",
            )
            code, log_file = self._run_limited(
                cmd,
                export_log_name,
                None,
                affinity=True,
                inactivity_timeout=disk_export_timeout,
                inactivity_timeout_by_marker=send2ue_inactivity_rules(
                    stage_timeout, disk_export_timeout
                ),
            )
        finally:
            # Send2UE output produced across two code revisions is discarded by
            # the parent restart fence and must not create an asset failure.
            self._assert_active_production_source_manifest()
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
        if not manifest_item_has_current_skeleton_root_export(item):
            raise BatchItemError(
                "Send2UE export omitted the authored Skeleton Root contract",
                kind="data_error",
                log_file=log_file,
                report_file=export_report,
            )
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
            except CodeRevisionRestartRequired:
                raise
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

    def _run_headless_push_batch(
        self,
        targets,
        emit_done=True,
        defer_import=False,
    ):
        batch_stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        total = len(targets)
        retry_metadata = copy.deepcopy(
            getattr(self, "_active_retry_metadata", {}) or {}
        )
        export_retry = retry_metadata.get("partition") == "blender_export"
        progress_label = (
            "Unreal 대기 등록"
            if defer_import
            else (
                "실패 재시도 · Blender/Send2UE→Unreal"
                if export_retry
                else "Unreal Push"
            )
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
            except CodeRevisionRestartRequired:
                raise
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
                if dependency in exported_ids
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

        if defer_import:
            return self._persist_waiting_import_items(
                pending,
                targets,
                failed_items,
                emit_done=emit_done,
                batch_stamp=batch_stamp,
            )

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

    def _persist_waiting_import_items(
        self,
        pending,
        targets,
        failed_items,
        *,
        emit_done,
        batch_stamp,
    ):
        """Persist immutable exports without starting UnrealEditor-Cmd."""
        created_at = datetime.now().isoformat(timespec="seconds")
        manifest_path = LOG_DIR / f"unreal_wait_{batch_stamp}.json"
        checkpoint_path = LOG_DIR / (
            f"unreal_wait_{batch_stamp}_checkpoint.json"
        )
        report_path = LOG_DIR / f"unreal_wait_{batch_stamp}_report.json"
        waiting_items = copy.deepcopy(list(pending))
        for item in waiting_items:
            item["batch_manifest"] = str(manifest_path)
            item["batch_report"] = str(report_path)
        atomic_write_json(
            manifest_path,
            {
                "schema_version": PUSH_MANIFEST_SCHEMA_VERSION,
                "kind": "sk_batch_unreal_wait_queue",
                "created_at": created_at,
                "checkpoint_path": str(checkpoint_path),
                "report_path": str(report_path),
                "max_item_crash_retries": int(
                    self.cfg.get("headless_item_crash_retries", 2)
                ),
                "items": waiting_items,
            },
        )

        for position, item in enumerate(waiting_items):
            iid = str(item["queue_id"])
            with self.state_lock:
                details = copy.deepcopy(
                    self.state.get(iid, {}).get("push_paths") or {}
                )
            details.update({
                "waiting_manifest": str(manifest_path),
                "waiting_checkpoint": str(checkpoint_path),
                "waiting_report": str(report_path),
                "waiting_queued_at": created_at,
                "waiting_position": position,
            })
            self._set_push_state(
                iid,
                "exported_pending_unreal",
                "export 완료 · Unreal 영구 대기",
                details=details,
            )

        self._phase_failed_items = set(failed_items)
        if emit_done:
            failure_count = len(failed_items)
            text = f"Unreal 대기 등록 완료 · {len(waiting_items)}개"
            if failure_count:
                text += f" · 실패/준비 제외 {failure_count}개"
            self.ui_queue.put(("progress", text))
            self.ui_queue.put(("done", None))
        self.log(
            f"Unreal 영구 대기 큐 등록: {len(waiting_items)}개 · "
            f"{manifest_path}"
        )
        return not failed_items

    def _load_waiting_import_item(self, target, batch_stamp):
        """Load one finalized durable manifest item without re-auditing it."""
        spm = Path(target["spm"])
        iid = str(spm)
        with self.state_lock:
            entry = copy.deepcopy(self.state.get(iid, {}))
        if entry.get("push_status_kind") != "exported_pending_unreal":
            raise BatchItemError(
                "Unreal 대기 상태가 아님",
                kind="data_error",
            )

        export_cache = entry.get("push_export_cache") or {}
        paths = entry.get("push_paths") or {}
        manifest_value = (
            paths.get("waiting_manifest")
            or export_cache.get("manifest")
        )
        manifest_path = Path(str(manifest_value or ""))
        if not manifest_path.is_file():
            raise BatchItemError(
                "대기 manifest 파일 없음: " + str(manifest_path),
                kind="data_error",
            )
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            item = next(
                copy.deepcopy(candidate)
                for candidate in (manifest.get("items") or [])
                if str((candidate or {}).get("queue_id")) == iid
            )
        except (OSError, ValueError, StopIteration, TypeError) as exc:
            raise BatchItemError(
                f"대기 manifest 항목을 읽을 수 없음: {exc}",
                kind="data_error",
            ) from exc
        item["report_path"] = str(
            LOG_DIR / f"{spm.stem}_waiting_unreal_{batch_stamp}.json"
        )
        return item

    @staticmethod
    def _order_waiting_import_items(items):
        """Return a stable dependency-first order for persisted queue items."""
        records = [copy.deepcopy(item) for item in items]
        by_id = {str(item["queue_id"]): item for item in records}
        queue_ids = set(by_id)
        for item in records:
            item["depends_on_queue_ids"] = [
                str(dependency)
                for dependency in item.get("depends_on_queue_ids") or []
                if str(dependency) in queue_ids
            ]

        emitted = set()
        ordered = []
        remaining = list(records)
        while remaining:
            ready = [
                item
                for item in remaining
                if set(item.get("depends_on_queue_ids") or []).issubset(
                    emitted
                )
            ]
            if not ready:
                cycle = ", ".join(
                    Path(str(item.get("queue_id") or "?")).name
                    for item in remaining
                )
                raise BatchItemError(
                    "Unreal 대기 큐 dependency cycle: " + cycle,
                    kind="data_error",
                )
            for item in ready:
                ordered.append(item)
                emitted.add(str(item["queue_id"]))
                remaining.remove(item)
        return ordered

    def _run_waiting_asset_import_batch(self, targets, emit_done=True):
        """Import durable Unreal-wait rows in one headless commandlet session."""
        if self._unreal_running():
            self._phase_failed_items = set()
            self._phase_abort_reason = (
                "대기 에셋 임포트는 Unreal Editor가 꺼진 상태에서만 실행됩니다"
            )
            self.ui_queue.put(("progress", self._phase_abort_reason))
            if emit_done:
                self.ui_queue.put(("done", None))
            return False

        batch_stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        total = len(targets)
        self.ui_queue.put(("batch_progress", (0, total)))
        pending = []
        invalid_ids = set()
        target_by_id = {str(item["spm"]): item for item in targets}
        for index, target in enumerate(targets, 1):
            iid = str(target["spm"])
            try:
                pending.append(
                    self._load_waiting_import_item(target, batch_stamp)
                )
            except Exception as exc:
                reason = compact_error_message(exc)
                with self.state_lock:
                    details = copy.deepcopy(
                        self.state.get(iid, {}).get("push_paths") or {}
                    )
                details["waiting_import_error"] = reason
                self._set_push_state(
                    iid,
                    "exported_pending_unreal",
                    "Unreal 대기 유지 · import manifest 확인 필요",
                    details=details,
                )
                self.log(f"[대기 import 준비 실패] {Path(iid).name}: {reason}")
                invalid_ids.add(iid)
            self.ui_queue.put(("batch_progress", (index, total)))
            self.ui_queue.put((
                "progress",
                f"대기 에셋 준비 {index}/{total}",
            ))

        if not pending:
            self._phase_failed_items = set(invalid_ids)
            self._phase_abort_reason = "가져올 수 있는 Unreal 대기 에셋 없음"
            self.ui_queue.put(("progress", self._phase_abort_reason))
            if emit_done:
                self.ui_queue.put(("done", None))
            return False

        try:
            pending = self._order_waiting_import_items(pending)
        except BatchItemError as exc:
            reason = compact_error_message(exc)
            for item in pending:
                iid = str(item["queue_id"])
                with self.state_lock:
                    details = copy.deepcopy(
                        self.state.get(iid, {}).get("push_paths") or {}
                    )
                details["waiting_import_error"] = reason
                self._set_push_state(
                    iid,
                    "exported_pending_unreal",
                    "Unreal 대기 유지 · dependency 확인 필요",
                    details=details,
                )
                invalid_ids.add(iid)
            self._phase_failed_items = set(invalid_ids)
            self._phase_abort_reason = reason
            self.ui_queue.put(("progress", reason))
            if emit_done:
                self.ui_queue.put(("done", None))
            return False

        ordered_targets = [
            target_by_id[str(item["queue_id"])] for item in pending
        ]
        if self._unreal_running():
            self._phase_failed_items = set(invalid_ids)
            self._phase_abort_reason = (
                "검증 중 Unreal Editor가 실행되어 headless import를 시작하지 않음"
            )
            self.ui_queue.put(("progress", self._phase_abort_reason))
            if emit_done:
                self.ui_queue.put(("done", None))
            return False
        return self._run_headless_import_items(
            pending,
            ordered_targets,
            invalid_ids,
            emit_done=emit_done,
            batch_stamp=batch_stamp,
            manifest_metadata={
                "queue_kind": "durable_unreal_wait_import",
                "source_waiting_manifests": sorted({
                    str(
                        (self.state.get(str(item["queue_id"]), {}).get(
                            "push_paths"
                        ) or {}).get("waiting_manifest")
                        or ""
                    )
                    for item in pending
                }),
            },
            progress_label="대기 에셋 임포트",
            file_prefix="waiting_assets",
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
        source_fingerprint = self._source_push_fingerprint(blend, iid)
        if not getattr(self, "force_rerun", False):
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
        export_root = (
            send2ue_export_cache_root()
            / "rpc"
            / stamp
            / spm.stem
        )
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
        expected_export_bytes = estimate_output_reservation_bytes(
            blend, minimum_bytes=1024**3, multiplier=4
        )
        self._wait_for_send2ue_disk_space(
            iid, export_root, expected_export_bytes
        )
        self._set_push_state(
            iid,
            "exporting",
            "Send2UE export 중...",
        )
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
        if getattr(app, "_standalone_close_requested", False):
            return
        app._standalone_close_requested = True
        root.withdraw()

        def finalize():
            try:
                receipt = shutdown_process_supervisor(
                    "sk_batch_gui_close",
                    terminate_grace=1.0,
                    kill_grace=5.0,
                ) or {}
                survivors = list(receipt.get("survivors") or ())
                if survivors:
                    raise RuntimeError(
                        "관리 프로세스 트리 종료 후 survivor가 남았습니다: "
                        + ", ".join(str(value) for value in survivors)
                    )
            except Exception as exc:
                app._standalone_close_requested = False
                messagebox.showerror("종료 정리 실패", str(exc), parent=root)
                root.deiconify()
                return
            root.destroy()

        app.shutdown_shared_queue(on_complete=finalize)

    root.protocol("WM_DELETE_WINDOW", close)
    root.mainloop()


if __name__ == "__main__":
    main()
