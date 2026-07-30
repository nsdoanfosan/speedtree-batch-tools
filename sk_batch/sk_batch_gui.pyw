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

from batch_ui_common import CheckedRowController, copy_selected_row_paths
from shared_queue_runtime import SharedQueueRuntime, WaitCancelled

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
    leaf_contract_user_message,
    save_leaf_contract_cache,
    speedtree_stmat_path,
)
from speedtree_pipeline_contract import (
    build_preflight_envelope,
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
from send2ue_manifest_contract import (
    is_actionable_cluster_assembly_manifest,
)
from pcg_st9_texture_batch.pcg_cluster_assembly_contract import (
    ClusterAssemblyReceiptStaleError,
    cluster_assembly_receipt_resolution,
    load_cluster_assembly_receipt,
)
from pcg_st9_texture_batch.pcg_canonical_outputs import (
    CanonicalOutputManifestError,
    refresh_atlas_manifests_for_spm,
)
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
CHECK_ON = "☑"
CHECK_OFF = "☐"
STATUS_COLUMNS = ("spm_status", "blend_status", "push_status")
# Temporary production drain mode: select only owner rows that already have
# cluster/*.spm providers but do not yet have a local Assembly output.
TEMP_SELECT_CLUSTER_WITHOUT_ASSEMBLY_PUSH_ROWS = True
CLUSTER_RELATION_LOCKS_GUARD = threading.Lock()
_REPAIR_REPORT_READ_LOCAL = threading.local()
CLUSTER_RELATION_LOCKS = {}
SPEEDTREE_SLOT_WAIT_MARKER = "SK_BATCH_SPEEDTREE_SLOT_WAIT"
SPEEDTREE_SLOT_ACQUIRED_MARKER = "SK_BATCH_SPEEDTREE_SLOT_ACQUIRED"


def is_cluster_source_spm(spm):
    path = Path(spm)
    return (
        path.suffix.casefold() == ".spm"
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
                candidate.is_file()
                and candidate.suffix.casefold() == ".spm"
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
    unresolved_materials = [
        str(intent.get("material_name") or "<unnamed>")
        for intent in migrated_envelope.get("material_intents") or []
        if str(intent.get("texture_source_mode") or "") == "unresolved"
    ]
    if unresolved_materials:
        raise ValueError(
            "legacy Repair report cannot prove material texture bindings; "
            "run Blender Repair again: "
            + ", ".join(unresolved_materials)
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
        self.cell_editor = None
        self.stop_flag = threading.Event()
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
        self.root.after(100, self._drain_ui_queue)
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
        chk = ttk.Checkbutton(opts, text="완료된 항목도 다시 실행", variable=self.force_var)
        chk.pack(side="left", padx=12)
        Tooltip(chk, ("② Blender Repair에서, 이미 SPM보다 최신인 .blend가 있는 항목은 기본적으로 "
                      "건너뜁니다. ① SPM 본 세팅도 동일 SPM/옵션 캐시를 기본 사용합니다. "
                      "이 옵션을 켜면 ①② 모두 강제로 다시 실행합니다."))

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
        spms = scan_sk_spms(root)
        cluster_sources = scan_cluster_spm_sources(root)
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
            "btn_check", "btn_spm", "btn_blender", "btn_push", "btn_all",
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
        cluster_sources = prepared.get("cluster_sources") or []
        cluster_by_source = {
            _normalized_path(row["authoring_spm"]): row
            for row in cluster_sources
        }
        spms = list(prepared["spms"]) + [
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
        if TEMP_SELECT_CLUSTER_WITHOUT_ASSEMBLY_PUSH_ROWS:
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

    def _snapshot_batch_request(self, target_iids):
        inventory = {
            iid: self._snapshot_batch_item(item)
            for iid, item in self.items.items()
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
            "btn_check", "btn_spm", "btn_blender", "btn_push", "btn_all",
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
        self._active_batch_inventory = job["inventory"]
        self._active_batch_items = job["inventory"]
        self._reset_cluster_receipt_refresh_memo()
        self.stop_flag.clear()
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

    def _run_queued_batch_job(self, job):
        error = None
        status = "completed"
        failed_count = 0
        lease = None
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

                lease = shared_runtime.wait_for_turn(
                    shared_job_id,
                    on_wait=report_wait,
                    cancel_event=self.stop_flag,
                )
                self.ui_queue.put((
                    "progress",
                    "공용 대기열 진입 · 단독 실행",
                ))
            if job["mode"] == "pipeline":
                completed = self._run_full_pipeline(
                    job["targets"],
                    terminal_phase=job["terminal_phase"],
                    selected_scope=job["selected_scope"],
                    emit_done=False,
                )
            else:
                completed = self._run_batch(
                    job["phase"], job["targets"], emit_done=False
                )
            failed_count = len(
                getattr(self, "_phase_failed_items", set()) or ()
            )
            if self.stop_flag.is_set():
                status = "stopped"
            elif completed is False and failed_count:
                status = "partial"
            elif completed is False:
                status = "failed"
            elif failed_count:
                status = "partial"
            if status == "partial":
                error = f"항목 실패/준비 제외 {failed_count}개"
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
                    lease.finish(
                        success=(status == "completed"),
                        result={
                            "tool": "sk_batch",
                            "local_job_id": job["id"],
                            "outcome": status,
                            "failed_count": failed_count,
                            "error": error,
                        },
                    )
                except Exception as queue_exc:
                    error = compact_error_message(queue_exc)
                    status = "failed"
                    self.log(
                        f"[대기열 #{job['id']}] 공용 대기열 종료 기록 실패 · "
                        f"{job['label']}: {error}"
                    )
            self.ui_queue.put((
                "batch_job_done",
                {
                    "id": job["id"],
                    "error": error,
                    "status": status,
                    "failed_count": failed_count,
                },
            ))

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
                    "failed_count": int(
                        payload.get("failed_count", 0) or 0
                    ),
                }
            )
        outcome_text = {
            "completed": "완료",
            "partial": "실패/준비 제외 기록 후 다음 작업 계속",
            "failed": "실패 기록 후 다음 작업 계속",
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
            "_active_blender_dependency_map",
            "_active_pipeline_terminal_phase",
            "_active_repair_stage_contracts",
            "_pipeline_upstream_failed_items",
            "_active_push_dependency_map",
            "_active_push_auto_added_ids",
            "_phase_failed_items",
            "_cluster_receipt_refresh_memo_lock",
            "_cluster_receipt_refresh_memo",
            "_cluster_receipt_refresh_flights",
            "_cluster_receipt_owner_locks",
        ):
            self.__dict__.pop(key, None)
        if self.pending_batch_jobs:
            self._start_next_batch_job()
            return
        self._set_batch_queue_controls(False)
        failure_count = len(self.batch_job_failures)
        if status == "stopped":
            self.progress_var.set("대기열 중지됨")
        elif failure_count:
            self.progress_var.set(
                f"대기열 완료 · 작업 실패/부분 실패 {failure_count}건 기록"
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

    def _run_full_pipeline(
        self,
        targets,
        terminal_phase="push",
        selected_scope=False,
        emit_done=True,
    ):
        self._active_pipeline_terminal_phase = terminal_phase
        self._active_repair_stage_contracts = {}
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
            schedule.append(
                ("spm", downstream_targets, "Tree ① SPM 본 세팅")
            )
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
        failed_items = set()
        for phase, scheduled_targets, label in schedule:
            if self.stop_flag.is_set():
                break
            eligible_stage = [
                item for item in scheduled_targets
                if str(item["spm"]) not in failed_items
            ]
            if not eligible_stage:
                continue
            self._pipeline_upstream_failed_items = set(failed_items)
            self.log(f"🌙 {label} 시작")
            phase_ok = self._run_batch(
                phase, eligible_stage, emit_done=False
            )
            phase_failed = set(getattr(self, "_phase_failed_items", set()))
            if phase_failed:
                failed_items.update(phase_failed)
                self.log(
                    f"🌙 {label}: 실패/준비 제외 {len(phase_failed)}개는 "
                    "다음 단계로 넘기지 않습니다."
                )
            self.log(f"🌙 {label} 종료")
            if not phase_ok:
                pipeline_abort = getattr(self, "_phase_abort_reason", None)
                break
        eligible = [
            item for item in targets
            if str(item["spm"]) not in failed_items
        ]
        if self.stop_flag.is_set():
            final_text = "중지됨"
        elif pipeline_abort:
            final_text = f"전체 자동 중단 — {pipeline_abort}"
        elif failed_items:
            final_text = (
                f"전체 자동 종료 — 성공 {len(eligible)}개 · "
                f"실패/준비 제외 {len(failed_items)}개"
            )
        else:
            final_text = "전체 자동 완료"
        if selected_scope:
            terminal_label = phase_labels[terminal_phase]
            if self.stop_flag.is_set():
                final_text = f"{terminal_label} 연계 실행 중지됨"
            elif pipeline_abort:
                final_text = f"{terminal_label} 연계 실행 중단 · {pipeline_abort}"
            elif failed_items:
                final_text = (
                    f"{terminal_label} 연계 실행 종료 · 성공 {len(eligible)}개 · "
                    f"실패/준비 제외 {len(failed_items)}개"
                )
            else:
                final_text = f"{terminal_label} 연계 실행 완료"
        self._phase_failed_items = set(failed_items)
        self.ui_queue.put(("progress", final_text))
        if emit_done:
            self.ui_queue.put(("done", None))
        self.__dict__.pop("_active_blender_dependency_map", None)
        self.__dict__.pop("_pipeline_upstream_failed_items", None)
        self.log(f"🌙 {final_text}")
        return not (
            self.stop_flag.is_set()
            or pipeline_abort
            or failed_items
        )

    def stop_batch(self):
        self._ensure_batch_queue_state()
        pending_jobs = list(self.pending_batch_jobs)
        pending = len(pending_jobs)
        shared_runtime = getattr(self, "shared_queue_runtime", None)
        if shared_runtime is not None:
            for job in pending_jobs:
                shared_job_id = job.get("shared_queue_job_id")
                if not shared_job_id:
                    continue
                try:
                    shared_runtime.cancel(
                        shared_job_id,
                        reason="sk_batch_local_queue_cancelled",
                    )
                except Exception:
                    # A job that acquired its lease between the snapshot and
                    # this cancellation is owned by the worker and is released
                    # only after its child processes have stopped.
                    pass
        self.pending_batch_jobs.clear()
        self.stop_flag.set()
        # Worker polling performs the tree kill. Keeping it in one place avoids
        # racing a direct parent-only kill that would orphan SpeedTree children.
        suffix = f" · 대기 작업 {pending}개 취소" if pending else ""
        self.log(
            "중지 요청됨 — 실행 중인 작업과 SpeedTree 자식을 종료합니다."
            + suffix
        )

    def shutdown_shared_queue(self):
        runtime = getattr(self, "shared_queue_runtime", None)
        if runtime is not None:
            runtime.shutdown()

    def _record_phase_status(
        self, iid, column, status_text, kind, reason, details=None, persist=True
    ):
        """Write the same structured item outcome to GUI and persistent state."""
        self.ui_queue.put(("cell", (iid, column, status_text)))
        with self.state_lock:
            state_entry = self.state.setdefault(iid, {})
            state_entry[column] = status_text
            state_entry[f"{column}_kind"] = kind
            error_entry = {
                "time": datetime.now().isoformat(timespec="seconds"),
                "kind": kind,
                "message": reason,
            }
            if details:
                error_entry.update(details)
            state_entry[f"{column}_error"] = error_entry
            if persist:
                save_state(self.state)

    @staticmethod
    def _failure_status_text(reason, kind):
        if kind == "manual_required":
            return reason
        if kind in PUSH_ABORT_KINDS:
            return f"중단: {reason}"
        return f"실패: {reason}"

    def _run_batch(self, phase, targets, emit_done=True):
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        self._phase_abort_reason = None
        self._phase_failed_items = set()
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
            except (
                PushDependencyError,
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
            try:
                if phase == "push":
                    dependencies = self._active_push_dependency_map.get(
                        iid, ()
                    )
                    blocked = [
                        dependency
                        for dependency in dependencies
                        if dependency in failed_items
                    ]
                    if blocked:
                        raise BatchItemError(
                            "required Cluster Push did not complete: "
                            + ", ".join(
                                sorted(Path(value).name for value in blocked)
                            ),
                            kind="dependency_blocked",
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
            except Exception as exc:
                reason = compact_error_message(exc)
                kind = getattr(exc, "kind", "data_error")
                if phase == "blender":
                    self._publish_repair_stage_contract(
                        spm,
                        ready=False,
                        reason=reason,
                        kind=kind,
                    )
                if phase == "push" and kind == "data_error" and "시간 초과" in reason:
                    kind = "push_timeout"
                    reason = "Push 작업 시간 초과 — Unreal/RPC 상태 확인 필요"
                status_text = self._failure_status_text(reason, kind)
                tag = {
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
                self._record_phase_status(
                    iid,
                    column,
                    status_text,
                    kind,
                    reason,
                    details=details,
                    persist=phase != "check",
                )
                with self.state_lock:
                    failed_items.add(iid)
                if phase == "push" and kind in PUSH_ABORT_KINDS:
                    self._phase_abort_reason = reason
                    phase_abort.set()
                    self.log(
                        "[Push 단계 중단] Unreal/RPC 상태가 안전하지 않아 남은 항목을 실행하지 않습니다."
                    )
            finally:
                with self.state_lock:
                    self._batch_active -= 1
                    self._batch_done += 1
                    done = self._batch_done
                    active = self._batch_active
                self.ui_queue.put(("batch_progress", (done, total)))
                self.ui_queue.put(
                    ("progress", f"{title} {done}/{total} · 실행 중 {active}개")
                )

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

    def _run_limited(
        self,
        cmd,
        log_name,
        timeout,
        affinity=True,
        progress_callback=None,
        env=None,
    ):
        log_file = LOG_DIR / log_name
        proc = launch_limited(
            cmd,
            self.cfg,
            log_file=str(log_file),
            affinity=affinity,
            env=env,
        )
        with self.procs_lock:
            self.active_procs.add(proc)
        try:
            started = time.monotonic()
            deadline = (
                None if timeout is None else started + timeout
            )
            next_progress = 0.0
            while proc.poll() is None:
                if self.stop_flag.is_set():
                    tree_stopped = terminate_process_tree(proc)
                    detail = "" if tree_stopped else " (자식 프로세스 종료 확인 실패)"
                    raise RuntimeError("사용자 중지" + detail)
                now = time.monotonic()
                latest_line = ""
                if progress_callback is not None:
                    latest_line = self._latest_log_line(log_file)
                if deadline is not None and now > deadline:
                    tree_stopped = terminate_process_tree(proc)
                    detail = "" if tree_stopped else " — 자식 프로세스 종료 확인 실패"
                    timeout_error = RuntimeError(
                        f"시간 초과({timeout}s){detail} — 로그: {log_file}"
                    )
                    timeout_error.log_file = log_file
                    raise timeout_error
                if progress_callback is not None and now >= next_progress:
                    progress_callback(
                        now - started,
                        latest_line,
                    )
                    next_progress = now + 1.0
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
                    if (
                        not rendered_unused
                        or not receipt_fingerprints_match(
                            embedded_receipt,
                            current_receipt_record,
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
                "② Blender Repair 다시 실행: " + compact_error_message(exc)
            )

    def _repair_runtime_addon_dir(self):
        """Installed BWR addon folder, derived identically for read and write."""
        return addon_dir_from_config(getattr(self, "cfg", {}) or {})

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
        leaf_ok, leaf_reason = self._leaf_reference_ready(
            speedtree_output_spm_for(spm)
        )
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
            spm, content_receipt_current=receipt_current
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

    def _publish_repair_stage_contract(
        self,
        spm,
        *,
        ready,
        reason,
        kind=None,
        push_dependency_contract=None,
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
    def _leaf_reference_ready(spm):
        contract = inspect_spm_leaf_contract(spm)
        return leaf_contract_user_message(contract)

    @staticmethod
    def _material_export_ready(spm, content_receipt_current=False):
        speedtree_spm = speedtree_output_spm_for(spm)
        contract = inspect_spm_leaf_contract(speedtree_spm)
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
        missing = list(normalization.get("missing", []))
        def recorded_file_missing(value):
            try:
                candidate = Path(value) if value else None
                return not candidate or not candidate.is_file() or candidate.stat().st_size <= 0
            except OSError:
                return True

        # A report can outlive one of its local T_ files. Re-check recorded
        # paths cheaply so deletion/OneDrive placeholders cannot pass on a
        # stale "ok" label. Preserved Cluster rows carry their own source-file
        # map and are validated the same way.
        for material in normalization.get("materials", []):
            material_status = str(material.get("status") or "")
            if material_status not in {
                "ok",
                "preserved_cluster",
                "needs_pcg_generation",
            }:
                continue
            files = (
                material.get("files")
                or material.get("preserved_files")
                or material.get("source_maps")
                or material.get("source_paths")
                or {}
            )
            missing_roles = [
                role for role, value in files.items()
                if recorded_file_missing(value)
            ]
            if material_status == "needs_pcg_generation" and not files:
                missing_roles = list(material.get("source_roles") or ["source"])
            if missing_roles:
                missing.append(
                    {
                        "material": material.get("material", "?"),
                        "expected_texture_base": (
                            material.get("texture_base")
                            or material.get("expected_texture_base")
                            or "보존 Cluster"
                        ),
                        "missing_roles": missing_roles,
                    }
                )
        if missing:
            details = []
            for item in missing:
                roles = ",".join(item.get("missing_roles", [])) or "대응 세트"
                details.append(
                    f"{item.get('material', '?')}→"
                    f"{item.get('expected_texture_base', 'T_?')}[{roles}]"
                )
            return False, (
                "텍스처 준비 안 됨: " + " | ".join(details)
                + " → PCG ③ 또는 ② Repair 확인"
            )
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
        if (
            normalization_status not in {"ok", "preserved_cluster"}
            and not source_fallback_ready
        ):
            return False, "텍스처 정규화 미완료 → ② 필요"
        handoff = report.get("handoff_preflight")
        if not isinstance(handoff, dict):
            return False, "② 사전검사 정보 없음 → ② Blender Repair 다시 실행"
        if handoff.get("status") != "ok":
            slots = handoff.get("empty_material_slots") or []
            outputs = handoff.get("missing_outputs") or []
            materials = (
                handoff.get("missing_materials")
                or (handoff.get("material_export") or {}).get("missing_materials")
                or []
            )
            vertex_contract = handoff.get("vertex_color_contract") or {}
            payload_contract = handoff.get("vertex_payload_contract") or {}
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
            if vertex_contract.get("status") == "blocked":
                reasons.append("버텍스 컬러 검사 실패")
            if payload_contract.get("status") == "blocked":
                reasons.append("AO/Nanite UV payload 검사 실패")
            return False, "② 사전검사 차단: " + (" | ".join(reasons) or "보고서 확인")
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
        return True, "텍스처 정규화 완료"

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
            raise BatchItemError(
                "Canonical PCG → Atlas manifest preflight failed: "
                + str(exc),
                kind="data_error",
                report={
                    "status": "failed",
                    "stage": "canonical_atlas_manifest_preflight",
                    "spm": str(spm),
                    "error": str(exc),
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
        material_cmd = [
            sys.executable,
            str(TOOL_DIR / "jobs" / "speedtree_material_preflight.py"),
            "--spm", str(speedtree_spm),
            "--canonical-spm", str(spm),
            "--speedtree-exe", str(self.cfg["speedtree_exe"]),
            "--fbx-ini", str(fbx_ini),
            "--speedtree-cli", str(speedtree_cli),
            "--report", str(material_report),
            "--timeout", str(
                self.cfg.get("speedtree_material_preflight_timeout", 900)
            ),
        ]
        last_progress = {"bucket": -1, "phase": ""}

        def report_material_progress(elapsed, latest_line):
            if SPEEDTREE_SLOT_ACQUIRED_MARKER in latest_line:
                phase = "SpeedTree 실행 중"
            elif SPEEDTREE_SLOT_WAIT_MARKER in latest_line:
                phase = "SpeedTree 단일 슬롯 대기 중"
            else:
                phase = "계약 검사/캐시 확인 중"
            bucket = int(elapsed // 30)
            if (
                bucket == last_progress["bucket"]
                and phase == last_progress["phase"]
            ):
                return
            last_progress.update(bucket=bucket, phase=phase)
            self.log(
                f"재질 사전검사 진행: {spm.name} · {phase} "
                f"· 총 {int(elapsed)}초"
            )

        material_code, material_log = self._run_limited(
            material_cmd,
            material_log_name,
            # speedtree_cli starts its own export timeout only after it owns
            # the machine-wide Modeler gate. A second absolute parent timeout
            # would incorrectly subtract queue time from that execution
            # budget. This supervisor still owns Stop/Job cleanup.
            None,
            affinity=False,
            progress_callback=report_material_progress,
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

        A queued GUI job owns exactly one generation. Nothing is written to
        disk, and starting the next queued job discards every successful
        result and in-flight handle from the previous one.
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
                continue

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
                            "exists",
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
                expected_sha256 = record.get("sha256")
                if expected_sha256:
                    current_sha256 = _sha256_snapshot(candidate)["sha256"]
                    if (
                        str(expected_sha256).casefold()
                        != current_sha256.casefold()
                    ):
                        errors.append(f"sha256 changed: {candidate}")
                        continue
                expected_fingerprint = record.get("fingerprint")
                if isinstance(expected_fingerprint, str) and expected_fingerprint:
                    current_fingerprint = file_content_snapshot(
                        candidate
                    )["fingerprint"]
                    if (
                        expected_fingerprint.casefold()
                        != current_fingerprint.casefold()
                    ):
                        errors.append(f"fingerprint changed: {candidate}")
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
    ):
        """Hash the owner inputs while keeping stable large files O(1).

        SPM and JSON manifests use content hashes. Large Blender/FBX/texture
        artifacts already covered by the live report use path/size/mtime
        identities, matching the repository's other content-addressed cache
        validators. Missing paths remain part of the key, so an artifact
        appearing later invalidates the memo automatically.
        """
        spm = Path(spm).resolve()
        content_paths = self._cluster_receipt_discovery_input_paths(spm)

        all_paths = {
            Path(path).resolve()
            for path in live_artifact_paths
        }
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
            if (
                candidate in content_paths
                or candidate.suffix.casefold() in {".spm", ".json"}
            ):
                snapshot = file_content_snapshot(candidate)
                records.append({
                    "path": key,
                    "exists": True,
                    **snapshot,
                })
            else:
                records.append({
                    "path": key,
                    "exists": True,
                    "size": stat.st_size,
                    "mtime_ns": stat.st_mtime_ns,
                })

        envelope = {
            "version": 1,
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

    def _refresh_stale_cluster_receipt(
        self,
        spm,
        stamp,
    ):
        """Reuse one hash-current live audit inside the active GUI batch.

        Successful raw audits only are memoized. Concurrent callers for the
        same owner/input share one Future; an execution exception reaches all
        existing waiters but is removed immediately so a later caller may
        retry.
        """
        spm = Path(spm).resolve()
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
            try:
                current_cache_fingerprint = (
                    self._cluster_receipt_refresh_input_fingerprint(
                        spm,
                        live_artifact_paths=cached_live_artifact_paths,
                    )
                )
                pre_discovery_fingerprint = (
                    self._cluster_receipt_refresh_input_fingerprint(spm)
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
            for attempt in range(2):
                raw_audit = self._refresh_stale_cluster_receipt_uncached(
                    spm,
                    (
                        stamp
                        if attempt == 0
                        else f"{stamp}_retry{attempt}"
                    ),
                    _raw_only=True,
                )
                post_discovery_fingerprint = (
                    self._cluster_receipt_refresh_input_fingerprint(spm)
                )
                artifacts_match, artifact_errors = (
                    self._cluster_receipt_live_artifacts_match(
                        raw_audit.get("payload") or {}
                    )
                )
                stable = bool(
                    attempt_pre_fingerprint
                    == post_discovery_fingerprint
                    and artifacts_match
                )
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
                    final_discovery_fingerprint = (
                        self._cluster_receipt_refresh_input_fingerprint(spm)
                    )
                    final_artifacts_match, final_artifact_errors = (
                        self._cluster_receipt_live_artifacts_match(
                            raw_audit.get("payload") or {}
                        )
                    )
                    stable = bool(
                        final_discovery_fingerprint
                        == attempt_pre_fingerprint
                        and final_artifacts_match
                    )
                    if stable:
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
                if attempt == 0:
                    retry_reason = (
                        "; ".join(artifact_errors[:3])
                        if artifact_errors
                        else "asset SPM/manifest content changed"
                    )
                    self.log(
                        "Cluster Assembly live audit asset input changed "
                        f"during audit; retrying once: {spm.name} · "
                        f"{retry_reason}"
                    )
                    attempt_pre_fingerprint = (
                        self._cluster_receipt_refresh_input_fingerprint(spm)
                    )
                    continue
                detail = "; ".join(artifact_errors[:3])
                raise BatchItemError(
                    "Cluster Assembly live audit inputs kept changing; "
                    f"result was not cached: {spm.name}"
                    + (f": {detail}" if detail else ""),
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
            except ClusterAssemblyReceiptStaleError:
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
            timeout = int(self.cfg.get("cluster_receipt_refresh_timeout", 600))
            audit_command = [
                sys.executable,
                str(audit_script),
                "--target", str(spm.parent),
                "--target-mesh", (
                    spm.stem[3:]
                    if spm.stem.casefold().startswith("sk_")
                    else spm.stem
                ),
                "--json", str(audit_report),
            ]
            if not _persist_receipt:
                audit_command.append("--no-receipt")
            code, log_file = self._run_limited(
                audit_command,
                f"{run_identity}.log",
                timeout,
                affinity=False,
            )

            payload_error = None
            try:
                payload = json.loads(
                    audit_report.read_text(encoding="utf-8")
                )
            except (OSError, TypeError, ValueError) as exc:
                payload = None
                payload_error = exc
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
                raise BatchItemError(
                    "Cluster Assembly live audit process failed: "
                    f"{spm.name} (exit {code})",
                    kind="internal_error",
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

        failures = []
        for issue in live_issues:
            code_value = str(
                issue.get("code") or "CLUSTER_DATA_INVALID"
            )
            role = str(issue.get("role") or "")
            details = issue.get("details") or {}
            status = str(details.get("status") or "")
            missing = [
                str(value)
                for value in details.get("missing") or []
            ]
            fields = [code_value]
            if role:
                fields.append(f"role={role}")
            if status:
                fields.append(f"status={status}")
            if missing:
                fields.append("missing=" + ", ".join(missing[:3]))
            failures.append(" ".join(fields))
        actual_failure = " | ".join(failures[:5])

        if actual_failure:
            raise BatchItemError(
                "Cluster Assembly actual data audit failed: "
                + actual_failure,
                kind="data_error",
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
            summary = " | ".join(
                " ".join(
                    value
                    for value in (
                        str(issue.get("code") or "CLUSTER_DATA_INVALID"),
                        (
                            f"role={issue.get('role')}"
                            if issue.get("role")
                            else ""
                        ),
                    )
                    if value
                )
                for issue in blocking[:5]
            )
            stage = "output" if require_normalized else "input"
            raise BatchItemError(
                f"Cluster normalization {stage} validation failed: {summary}",
                kind="data_error",
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
                    live_target_contracts = []
                    for target in relation_targets:
                        live_resolution = (
                            self._cluster_normalization_stage_observation(
                                target,
                                f"{stamp}_normalization_input",
                                producer_spm,
                                require_normalized=False,
                            )
                        )
                        live_report = live_resolution.get(
                            "live_audit_report"
                        )
                        contract = copy.deepcopy(
                            live_resolution.get(
                                "selected_contract"
                            )
                        )
                        live_target_contracts.append({
                            "target_spm": str(target),
                            "report": str(live_report or ""),
                            "policy": "normalization_stage_input",
                            "contract": contract,
                        })
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
        cluster_receipt_resolution = None
        if not cluster_source:
            # A current blend is not enough for an owner Tree.  Refresh the
            # live Cluster relationship first so a missing Assembly manifest
            # invalidates the early Repair skip instead of being silently
            # treated as a completed non-Assembly asset.
            cluster_receipt_resolution = (
                self._refresh_stale_cluster_receipt(speedtree_spm, stamp)
            )
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
                            self._cluster_normalization_stage_observation(
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
        if not cluster_receipt_resolution_uses_live_audit(
            cluster_receipt_resolution
        ):
            cluster_receipt_resolution = self._refresh_stale_cluster_receipt(
                speedtree_spm,
                stamp,
            )
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
                        self._cluster_normalization_stage_observation(
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
            if repair_contract is None:
                ok, why = self._handoff_ready(spm)
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
                        "source_review"
                        if (
                            repair_contract is not None
                            and repair_contract.get("kind")
                            == "source_review"
                        )
                        else "preflight_skip"
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
        result = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq UnrealEditor.exe", "/NH"],
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
                    kind in {"exported_pending_unreal", "importing", "imported_ok"}
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
            if kind in {"exported_pending_unreal", "importing", "imported_ok"}:
                entry.pop("push_status_error", None)
            else:
                error = {
                    "time": datetime.now().isoformat(timespec="seconds"),
                    "kind": kind,
                    "message": error_message,
                }
                if details:
                    error.update(details)
                entry["push_status_error"] = error
            if details:
                entry["push_paths"] = details
            save_state(self.state)

    def _push_dependency_paths(self):
        send2ue_dir = Path(self.cfg["send2ue_dir"])
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
        state_entry = self.state.setdefault(iid, {}) if iid else {}
        fingerprint, record, cache_hit = cached_push_source_fingerprint(
            blend,
            self._push_source_dependency_paths(iid),
            cache=state_entry.get("push_source_fingerprint_cache"),
        )
        if iid:
            with self.state_lock:
                self.state.setdefault(iid, {})[
                    "push_source_fingerprint_cache"
                ] = record
        if cache_hit:
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
            item = (manifest.get("items") or [])[0]
        except (OSError, ValueError, IndexError, TypeError):
            return None
        if str(item.get("queue_id")) != iid or not manifest_item_files_match(item):
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
        code, log_file = self._run_limited(
            cmd,
            export_log_name,
            self.cfg.get("push_job_timeout", 1800),
            affinity=self.cfg.get("blender_parallel_jobs", 2) <= 1,
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

    def _sync_headless_checkpoint(self, checkpoint_path, item_by_id, log_file=None):
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
            if log_file:
                details["log"] = str(log_file)
            if status == "imported_ok":
                self.state.setdefault(queue_id, {})["push_import_fingerprint"] = item[
                    "fingerprint"
                ]
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
                    f"Unreal Push {completed}/{total} · "
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

    def _run_headless_push_batch(self, targets, emit_done=True):
        batch_stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        total = len(targets)
        self._headless_progress_snapshot = None
        self.ui_queue.put(("progress", f"Unreal Push export 준비 0/{total}"))
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
            self.ui_queue.put(("cell", (iid, "push_status", "Send2UE export 중...")))
            try:
                return index, self._export_manifest_item(iid, spm, batch_stamp)
            except Exception as exc:
                reason = compact_error_message(exc)
                kind = getattr(exc, "kind", "data_error")
                details = {}
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
                        f"Unreal Push export {completed}/{total} · "
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
        for item in exported:
            iid = str(item.get("queue_id") or "")
            unavailable = [
                dependency
                for dependency in dependency_map.get(iid, ())
                if dependency not in exported_ids
            ]
            if not unavailable:
                continue
            details = []
            for dependency in unavailable:
                provider_state = self.state.get(dependency, {})
                provider_reason = (
                    (provider_state.get("push_status_error") or {}).get(
                        "message"
                    )
                    or provider_state.get("push_status")
                    or "export 산출물 없음"
                )
                details.append(
                    f"{Path(dependency).name} ({compact_error_message(provider_reason, 80)})"
                )
            reason = (
                "required Cluster export did not complete: "
                + ", ".join(details)
            )
            self._set_push_state(
                iid,
                "dependency_blocked",
                self._failure_status_text(reason, "dependency_blocked"),
                message=reason,
            )
            failed_items.add(iid)
            dependency_blocked_ids.add(iid)
        if dependency_blocked_ids:
            exported = [
                item
                for item in exported
                if str(item.get("queue_id") or "")
                not in dependency_blocked_ids
            ]

        if self.stop_flag.is_set():
            for item in targets:
                iid = str(item["spm"])
                if self.state.get(iid, {}).get("push_status_kind") not in {
                    "exported_pending_unreal", "imported_ok", "data_error", "manual_required"
                }:
                    self._set_push_state(iid, "not_run", "미실행: 사용자 중지")
            if emit_done:
                self.ui_queue.put(("progress", "중지됨"))
                self.ui_queue.put(("done", None))
            self._phase_failed_items = failed_items
            return False

        pending = []
        for item in exported:
            iid = str(item["queue_id"])
            item["depends_on_queue_ids"] = list(
                dependency_map.get(iid, ())
            )
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
                    "Unreal Push 완료 (cache)"
                    if not failure_count
                    else (
                        f"Unreal Push 종료 — 성공 {success_count}개 · "
                        f"실패/준비 제외 {failure_count}개"
                    )
                )
                self.ui_queue.put(("progress", text))
                self.ui_queue.put(("done", None))
            return not failure_count

        manifest_path = LOG_DIR / f"headless_queue_{batch_stamp}.json"
        checkpoint_path = LOG_DIR / f"headless_queue_{batch_stamp}_checkpoint.json"
        report_path = LOG_DIR / f"headless_queue_{batch_stamp}_report.json"
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
        max_restarts = max(0, int(self.cfg.get("headless_batch_max_restarts", 10)))
        complete = False
        last_log = None
        checkpoint = {}
        for launch_index in range(max_restarts + 1):
            if self.stop_flag.is_set():
                break
            self.ui_queue.put(
                (
                    "progress",
                    "Unreal Push headless 시작 · "
                    f"시도 {launch_index + 1}/{max_restarts + 1}",
                )
            )
            self.log(
                f"UnrealEditor-Cmd headless 시작 ({launch_index + 1}/{max_restarts + 1})"
            )
            attempt_log = LOG_DIR / (
                f"headless_unreal_{batch_stamp}_{launch_index + 1}.log"
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
                    ),
                    env=env,
                )
            except Exception as exc:
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
                1 for item in targets
                if str(item["spm"]) not in failed_items
            )
            failure_count = len(failed_items)
            if not complete:
                progress_text = self._phase_abort_reason
            elif failure_count:
                progress_text = (
                    f"Unreal Push 종료 — 성공 {success_count}개 · "
                    f"실패/준비 제외 {failure_count}개"
                )
            else:
                progress_text = "Unreal Push 완료"
            self.ui_queue.put(
                ("progress", progress_text)
            )
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
        code, log_file = self._run_limited(cmd, f"{spm.stem}_push_{stamp}.log",
                                           self.cfg.get("push_job_timeout", 1800))
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
        self.log(f"push 완료: {result.get('unreal_folder', '?')}{result.get('unit_name', '')}")


def main():
    root = tk.Tk()
    try:
        ttk.Style().theme_use("vista")
    except tk.TclError:
        pass
    app = App(root)

    def close():
        app.stop_batch()
        app.shutdown_shared_queue()
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", close)
    root.mainloop()


if __name__ == "__main__":
    main()
