"""PCG ST9 → SK 전환 준비 보드.

목적: 언리얼 PCG에서 쓰는 ST9 나무(WPO + 마스크 머티리얼)를
SK_ 데이터(나나이트 + 논마스크 지오메트리 + 버추얼 텍스처)로 바꾸기 위해,
나무 폴더마다 "뭐가 되어 있고 뭐가 남았는지"를 한 화면에서 보여준다.

단계 (표의 컬럼 순서 = 작업 순서):
  ① SK SPM + M_      : 일반 식생과 Cluster 모두 SK 생성과 M_ 정리를 함께 한다.
  ② 잎 메시 (Blender) : 헤드리스 아틀라스 리프 제너레이터로 오파시티 없는 잎
                       지오메트리와 blend를 만들고, 선택 시 SK SPM에도 반영한다.
  ③ 텍스처 (Substance) : SBS에 원본을 연결해 6장
                       (color/normal/extra/height/opacity/subsurface)
                       익스포트하고, 필요하면 HBAO와 T_ 그래프도 만든다.
  ④ Blend ↔ SPM 확인   : 실제 blend 파일과 현재/예정 SPM Generator 연결을
                       별도 관리 정보로 감사한다.

①~③은 실행 전 확인창을 띄우며, SPM/SBS/기존 출력은 수정 전에 백업한다.
"""
import copy
import hashlib
import json
import os
import subprocess
import sys
import threading
import time
import tkinter as tk
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

TOOL_DIR = Path(__file__).resolve().parent
REPO_DIR = TOOL_DIR.parent
GUI_SOURCE_PATH = Path(__file__).resolve()
GUI_SOURCE_SHA256 = hashlib.sha256(GUI_SOURCE_PATH.read_bytes()).hexdigest()
GUI_MODULE_LOADED_AT = datetime.now().astimezone().isoformat(
    timespec="milliseconds"
)
sys.path.insert(0, str(REPO_DIR))
sys.path.insert(0, str(TOOL_DIR))

from process_lifecycle import external_handoff_popen, owned_run

from batch_ui_common import CheckedRowController, copy_selected_row_paths
from speedtree_error_log import ERROR_LOG, record_exception
from shared_queue_runtime import SharedQueueRuntime
from atlas_target_registry import (
    TargetRegistryError,
    load_target_registry,
    registry_path_for_blend,
    save_target_registry,
)
from cluster_blend_sync import (
    RelationValidationCache,
    discover_cluster_blend_relations,
    normalized_blend_for_source,
)

from pcg_texture_common import (
    REPORT_DIR, SHARED_CACHE_DIR, TARGETS_PATH, is_backup_path, load_config,
    load_pcg_targets, save_config,
)
from pcg_texture_audit import (
    _unsafe_provisional_source,
    atlas_provisional_source_declarations,
    candidate_folders,
    make_report,
    persist_cluster_assembly_receipts_safely,
    prepare_sk,
    register_blend_source_index,
    save_spm_analysis_cache,
)
from pcg_board_snapshot import (
    read_board_display_snapshot,
    write_board_display_snapshot,
)
from pcg_startup_cache import (
    BoundedDiscoveryError,
    ContentAddressedJsonCache,
    canonical_json_sha256,
    content_identity,
    json_safe as startup_json_safe,
    path_key as startup_path_key,
)
from pcg_startup_latency import StartupLatencyTracker
from mutation_plan_authority import (
    MutationAuthorityError,
    capture_manifest as capture_mutation_authority_manifest,
    complete_unit as complete_mutation_authority_unit,
    expected_item_state as expected_mutation_item_state,
    expected_path_state as mutation_authority_expected_path_state,
    require_unit as require_mutation_authority_unit,
    write_child_authority as write_child_mutation_authority,
)
from export_review_queue import GENERIC_MATERIAL_RE
from export_texture_plan import bucket_refs, build_texture_plan_from_report
from pcg_canonical_outputs import (
    canonical_texture_root,
    record_canonical_output,
)
import sbs_auto
from migrate_current_sk_textures import (
    build_job as build_texture_job,
    complete_output_set,
    expected_job_size,
    job_needs_source_repair,
    run_job as run_texture_job,
)
from unreal_texture_sync import (
    UnrealTextureSyncDeferred,
    is_texture_synced,
    load_sync_state,
    sync_texture_files,
    validate_unreal_texture_name,
)
from spm_texture_normalize import (
    cleanup_preserved_cluster_outputs,
    jobs_from_texture_plan,
    normalize_spms_transactionally,
    output_paths,
)

CHECK_ON = "☑"
CHECK_OFF = "☐"
DEFAULT_USE_PCG_TARGETS = False
TARGET_ROW_COLORS = {
    "target_pcg": "#E8F3FF",
    "target_level": "#FFF0D0",
    "target_both": "#E4F4E4",
}
BLENDER_RELATION_CACHE_PATH = (
    SHARED_CACHE_DIR / "pcg_blender_relation_rows_v2.json"
)
BLENDER_RELATION_CACHE_KIND = "pcg_blender_relation_rows_v2"
RELATION_INPUT_SUFFIXES = frozenset({
    ".blend", ".exr", ".fbx", ".jpeg", ".jpg", ".json", ".png",
    ".sbs", ".spm", ".tga", ".tif", ".tiff",
})
RELATION_BULK_IMAGE_SUFFIXES = frozenset({
    ".exr", ".jpeg", ".jpg", ".png", ".tga", ".tif", ".tiff",
})


def cluster_pair_step1_text(child):
    """Render one exact Cluster pair state in user-facing Korean."""
    target_status = dict(child.get("target_status") or {})
    pair_status = child.get("pair_status") or "unknown"
    pair_action = child.get("pair_action") or ""
    pair_conflicts = list(child.get("pair_conflicts") or [])
    if pair_conflicts:
        text = f"⚠ Cluster pair 충돌 · {pair_status}"
    elif target_status.get("status") == "material_name_conflict":
        text = "⚠ Cluster M_ 이름 충돌 · 자동 처리 차단"
    elif target_status.get("status") == "needs_sk":
        text = "Output 이름 SK_ 정규화 필요"
    elif target_status.get("status") == "needs_m_prefix":
        text = "canonical SK의 M_ 이름 정리 필요"
    elif pair_status == "current" and pair_action in {"", "none"}:
        return "완료 ✓"
    else:
        text = f"Cluster pair {pair_status}"
    if pair_action and pair_action != "none":
        text += f" · {pair_action}"
    return text


def cluster_texture_status_text(child, output_paths, missing_output_paths):
    """Prefer physical-capture completion over legacy raw map counts."""
    if child.get("normalization_workflow_mode") == "PHYSICAL_DIRECT_CAPTURE":
        missing_core = list(child.get("physical_capture_core_missing") or [])
        missing_count = max(len(missing_output_paths), len(missing_core))
        if missing_count:
            return f"Blender 촬영 TGA 누락 {missing_count}장"
        resolution = list(child.get("physical_capture_resolution") or [])
        if len(resolution) == 2:
            try:
                width, height = (int(resolution[0]), int(resolution[1]))
            except (TypeError, ValueError):
                width = height = 0
            if width > 0 and height > 0:
                size = f"{width}²" if width == height else f"{width}×{height}"
                return f"Blender 촬영 {size} · 완료 ✓"
        return "Blender 촬영 완료 ✓"
    contract_state = child.get("texture_contract_state")
    if contract_state == "source_fallback_needs_pcg_generation":
        return "원본 provisional · PCG T_* 생성 필요"
    if contract_state == "blocked_source_missing":
        return "원본 입력 누락 · PCG 생성 차단"
    if contract_state == "blocked_cache_source":
        return "cache/isolated 입력 차단"
    if contract_state == "blocked_generated_source":
        return "생성 출력의 source 재사용 차단"
    if contract_state == "canonical_outputs_need_manifest":
        return "T_* 6장 있음 · canonical manifest 생성 필요"
    if contract_state == "canonical_manifest_invalid":
        return "canonical manifest 오류"
    if contract_state == "canonical":
        return "PCG canonical T_* 6장 ✓"
    if output_paths:
        return (
            f"Cluster 출력 TGA 연결 {len(output_paths)}장"
            + (
                f" · 누락 {len(missing_output_paths)}장"
                if missing_output_paths else ""
            )
        )
    return "Cluster 출력 TGA 연결 없음"


def focus_data_asset_label(paths):
    names = []
    for path in paths or []:
        name = str(path).rsplit("/", 1)[-1]
        if name.startswith("DA_Base_"):
            name = "DB " + name[len("DA_Base_"):]
        names.append(name)
    return "/".join(names) or "PCG"


def step3_item_state(item):
    """Summarize unique texture outputs separately from SPM connection work."""
    all_entries = list(item.get("cluster_items") or [])
    local_entries = [row for row in all_entries if not row.get("shared_from")]
    missing_rows = [row for row in local_entries if row.get("missing_export_maps")]
    missing_maps = sum(len(row.get("missing_export_maps") or []) for row in missing_rows)
    connection_rows = [
        row for row in all_entries if row.get("connection_update_needed")
    ]
    total_maps = len(local_entries) * len(sbs_auto.RENDER_MAPS)
    complete_rows = [
        row for row in all_entries if not row.get("missing_export_maps")
    ]
    unreal_synced_sets = sum(
        1 for row in complete_rows if row.get("unreal_synced")
    )
    return {
        "sets": len(local_entries),
        "shared_sets": len(all_entries) - len(local_entries),
        "missing_sets": len(missing_rows),
        "missing_maps": missing_maps,
        "complete_maps": total_maps - missing_maps,
        "total_maps": total_maps,
        "connection_sets": len(connection_rows),
        "unreal_total_sets": len(complete_rows),
        "unreal_synced_sets": unreal_synced_sets,
        "unreal_pending_sets": len(complete_rows) - unreal_synced_sets,
        "unreal_state_known": bool(complete_rows)
        and all("unreal_synced" in row for row in complete_rows),
        "unreal_all_synced": bool(complete_rows)
        and unreal_synced_sets == len(complete_rows),
    }


def step3_selection_state(entries):
    """Return the dynamic label/state for the selected step-3 action."""
    selected = [entry for entry in entries.values() if entry.get("checked")]
    if not selected:
        return {"text": "③ 실행 — 선택 항목 없음", "state": "disabled"}
    states = [step3_item_state(entry.get("item") or {}) for entry in selected]
    missing_sets = sum(row["missing_sets"] for row in states)
    missing_maps = sum(row["missing_maps"] for row in states)
    connection_sets = sum(row["connection_sets"] for row in states)
    texture_sets = sum(row["sets"] + row["shared_sets"] for row in states)
    if missing_sets:
        return {
            "text": (
                f"③ 실행 — 연결 텍스처 {missing_sets}세트 완성 "
                f"(누락 {missing_maps}장)"
            ),
            "state": "normal",
        }
    if connection_sets:
        return {
            "text": f"③ 실행 — 완료 텍스처 연결 정리 ({connection_sets}세트)",
            "state": "normal",
        }
    unreal_total = sum(row["unreal_total_sets"] for row in states)
    unreal_pending = sum(row["unreal_pending_sets"] for row in states)
    unreal_state_known = bool(states) and all(
        row["unreal_state_known"] for row in states if row["unreal_total_sets"])
    if texture_sets and unreal_total and unreal_state_known and not unreal_pending:
        return {
            "text": f"③ Unreal 전체 재확인 ({unreal_total}세트 · 현재 최신 ✓)",
            "state": "normal",
            "force_unreal_verify": True,
        }
    if texture_sets:
        if not unreal_state_known:
            return {
                "text": f"③ Unreal 동기화 — 완료 텍스처 확인 ({texture_sets}세트)",
                "state": "normal",
            }
        return {
            "text": f"③ Unreal 동기화 — {unreal_pending or texture_sets}세트 확인",
            "state": "normal",
        }
    return {"text": "③ 실행 — 선택 항목에 텍스처 없음", "state": "disabled"}


def step3_force_selection_state(entries):
    """Return the explicit manual full-rerender action for the whole board."""
    texture_sets = sum(
        step3_item_state(entry.get("item") or {})["sets"]
        for entry in entries.values()
    )
    if not texture_sets:
        return {"text": "③ 전체 다시 뽑기 — 대상 없음", "state": "disabled"}
    return {
        "text": f"③ 전체 다시 뽑기 ({texture_sets}세트)",
        "state": "normal",
    }


def spm_paths_for_item(item):
    """Return the concrete SPM paths represented by a PCG folder row."""

    paths = []
    for status in item.get("target_spm_statuses") or []:
        path = status.get("sk_spm") or status.get("source_spm")
        if path:
            paths.append(path)
    if not paths:
        paths.extend(item.get("sk_spms") or [])
    if not paths and item.get("chosen_spm"):
        paths.append(item["chosen_spm"])
    return paths


def spm_display_rows(item):
    """Return the individual current/expected SPM rows shown under a folder."""

    statuses = list(item.get("target_spm_statuses") or [])
    if not statuses:
        statuses = [
            {
                "mesh_name": Path(value).stem.removeprefix("SK_"),
                "sk_spm": value if Path(value).name.lower().startswith("sk_") else None,
                "source_spm": value if not Path(value).name.lower().startswith("sk_") else None,
                "status": "ready" if Path(value).name.lower().startswith("sk_") else "needs_sk",
            }
            for value in spm_paths_for_item(item)
        ]

    labels = {
        "ready": "완료 ✓",
        "needs_sk": "Output 이름 SK_ 정규화 필요 → [① 실행]",
        "needs_m_prefix": "M_ 이름 정리 필요 → [① 실행]",
        "pair_conflict": "⚠ Output 이름 정규화 충돌 · 자동 처리 차단",
        "material_name_conflict": "⚠ M_ 이름 충돌 · 자동 처리 차단",
        "needs_source_review": "⚠ 원본 SPM 확인 필요",
    }
    rows = []
    seen = set()
    for status in statuses:
        source = Path(status["source_spm"]) if status.get("source_spm") else None
        current = Path(status["sk_spm"]) if status.get("sk_spm") else source
        mesh_name = status.get("mesh_name") or (
            current.stem.removeprefix("SK_") if current else "SPM"
        )
        if status.get("sk_spm"):
            display = Path(status["sk_spm"])
        elif source:
            display = source.with_name(f"SK_{source.name}")
        else:
            display = Path(item.get("folder", "")) / f"SK_{mesh_name}.spm"
        key = (str(item.get("folder", "")).casefold(), str(mesh_name).casefold())
        if key in seen:
            continue
        seen.add(key)
        rows.append({
            "mesh_name": mesh_name,
            "display_spm": display,
            "current_spm": current,
            "source_spm": source,
            "status": status,
            "step1": labels.get(status.get("status"), status.get("status") or "-"),
        })
    return rows


def step3_texture_row_key(item, atlas_base):
    """Return the stable folder/material key used by Step 3 exclusions."""
    folder = os.path.normcase(
        os.path.abspath(str((item or {}).get("folder") or ""))
    )
    return folder, str(atlas_base or "").strip().casefold()


def step3_row_key_json(key):
    """Return a JSON-friendly stable Step 3 row identity."""
    return {
        "folder": str(key[0]),
        "atlas_base": str(key[1]),
    }


def write_step3_run_report(path, payload):
    """Atomically persist a run-specific Step 3 diagnostic snapshot."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(
        f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
    )
    try:
        temporary.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()
    return path


def step3_job_dependency_paths(job):
    """Return current SBS/bitmap inputs whose changes invalidate old outputs."""
    paths = []
    sbs_path = Path(job["sbs"]) if job.get("sbs") else None
    if sbs_path and sbs_path.is_file():
        paths.append(sbs_path)
    for value in (job.get("inputs") or {}).values():
        path = Path(value)
        if path.is_file():
            paths.append(path)
    graph = job.get("graph")
    if sbs_path and graph:
        try:
            parsed = sbs_auto.parse_m_graph(sbs_path, graph)
        except Exception:
            parsed = {}
        for value in (parsed.get("inputs") or {}).values():
            path = Path(value)
            if path.is_file():
                paths.append(path)
        try:
            inspected = sbs_auto.inspect_graph_sources(sbs_path, graph)
        except Exception:
            inspected = {}
        for bitmap in inspected.get("bitmaps") or []:
            path = Path(bitmap.get("path") or "")
            if path.is_file():
                paths.append(path)
    result = []
    seen = set()
    for path in paths:
        try:
            absolute = path.resolve()
        except OSError:
            absolute = path.absolute()
        key = os.path.normcase(str(absolute))
        if key in seen:
            continue
        seen.add(key)
        result.append(absolute)
    return result


def step3_existing_output_freshness(job):
    """Prove that a complete output set is not older than its live sources."""
    texture_base = job.get("texture_base") or job.get("base")
    out_dir = job.get("out_dir")
    if not texture_base or not out_dir:
        return {
            "fresh": False,
            "reason": "output_identity_missing",
            "dependencies": [],
            "outputs": [],
        }
    outputs = [
        Path(path)
        for path in output_paths(out_dir, texture_base).values()
    ]
    if any(not path.is_file() or path.stat().st_size <= 0 for path in outputs):
        return {
            "fresh": False,
            "reason": "output_set_incomplete",
            "dependencies": [],
            "outputs": [str(path) for path in outputs],
        }
    dependencies = step3_job_dependency_paths(job)
    oldest_output_ns = min(path.stat().st_mtime_ns for path in outputs)
    newer = [
        path for path in dependencies
        if path.stat().st_mtime_ns > oldest_output_ns
    ]
    return {
        "fresh": not newer,
        "reason": (
            "current_source_newer_than_outputs"
            if newer else "outputs_current"
        ),
        "dependencies": [str(path) for path in dependencies],
        "newer_dependencies": [str(path) for path in newer],
        "outputs": [str(path) for path in outputs],
        "oldest_output_mtime_ns": oldest_output_ns,
    }


def checked_step3_spms(
        entries, allowed_folders=None, selected_texture_rows=None,
        include_unchecked=False):
    """Return exact selected final and canonical provider SK paths.

    Final PCG targets come from the checked tree entries.  A selected texture
    row can additionally own an exact canonical ``cluster/SK_*.spm`` material
    target; include that target so the T_* set rendered for the row is wired
    back into its authoring SPM.  Raw non-SK counterparts remain excluded.
    """
    allowed = (
        {
            os.path.normcase(os.path.abspath(str(folder)))
            for folder in allowed_folders
        }
        if allowed_folders is not None
        else None
    )
    result = []
    seen = set()
    for entry in entries.values():
        if not include_unchecked and not entry.get("checked"):
            continue
        item = entry.get("item") or {}
        folder_key = os.path.normcase(
            os.path.abspath(str(item.get("folder") or ""))
        )
        if allowed is not None and folder_key not in allowed:
            continue
        for value in spm_paths_for_item(item):
            path = Path(value)
            if (
                not path.name.lower().startswith("sk_")
                or is_backup_path(path)
            ):
                continue
            key = os.path.normcase(os.path.abspath(str(path)))
            if key in seen:
                continue
            seen.add(key)
            result.append(str(path))
    for item, row in selected_texture_rows or []:
        folder_key = os.path.normcase(
            os.path.abspath(str(item.get("folder") or ""))
        )
        if allowed is not None and folder_key not in allowed:
            continue
        for target in row.get("material_targets") or []:
            if not isinstance(target, dict) or not target.get("spm"):
                continue
            path = Path(target["spm"])
            if (
                not path.name.lower().startswith("sk_")
                or is_backup_path(path)
            ):
                continue
            key = os.path.normcase(os.path.abspath(str(path)))
            if key in seen:
                continue
            seen.add(key)
            result.append(str(path))
    return sorted(result, key=lambda value: os.path.normcase(value))


def step3_audit_folder_for_spm(spm):
    """Return the asset owner folder for a final or Cluster canonical SK."""
    parent = Path(spm).expanduser().absolute().parent
    if parent.name.casefold() == "cluster":
        return parent.parent
    return parent


def cluster_hierarchy_rows(item):
    """Return exact Cluster source rows independently of Assembly roles."""
    contract = item.get("cluster_assembly") or {}
    hierarchy = contract.get("hierarchy") or {}
    dependencies = contract.get("dependencies") or []
    bark_status = (contract.get("canonical_bark") or {}).get("status", "")
    source_rows = list(item.get("cluster_source_rows") or [])
    if not source_rows:
        # Compatibility with reports written before exact Cluster source rows
        # were added.  Keep every primary/reference SPM individually visible.
        for child in hierarchy.get("children") or []:
            source_rows.append({
                "name": child.get("name", ""),
                "source_spm": child.get("source_spm", ""),
                "referenced": True,
                "assembly_role": child.get("role"),
                "assembly_decision": child.get("decision"),
            })
            for reference in child.get("references") or []:
                source_rows.append({
                    "name": reference.get("name", ""),
                    "source_spm": reference.get("source_spm", ""),
                    "referenced": True,
                    "assembly_role": child.get("role"),
                    "assembly_decision": reference.get("decision"),
                })
    if not source_rows:
        return []
    dependency_by_path = {
        os.path.normcase(os.path.abspath(str(row.get("spm")))): row
        for row in dependencies if row.get("spm")
    }
    cluster_path = hierarchy.get("path") or ""
    if source_rows[0].get("source_spm"):
        cluster_path = str(Path(source_rows[0]["source_spm"]).parent)
    rows = [{
        "kind": "cluster",
        "name": hierarchy.get("name") or "Cluster",
        "source": cluster_path,
        "materials": "",
        "textures": "",
        "handoff": (contract.get("handoff") or {}).get("status", ""),
    }]
    decision_labels = {
        "normalize_part": "FBX 완전쌍 · Assembly 파츠",
        "pass_through": "FBX 역할쌍 없음 · 기존 통과",
        "pending_export": "FBX export 후 검증",
        "blocked": "⚠ FBX material–mesh 불완전",
        "reference_only": "Assembly 참고 전용",
    }
    for source_row in source_rows:
        source = source_row.get("source_spm") or source_row.get("source") or ""
        dependency = dependency_by_path.get(
            os.path.normcase(os.path.abspath(str(source)))) if source else None
        if not dependency:
            dependency = next((
                row for row in dependencies
                if row.get("name", "").casefold()
                == str(source_row.get("name") or Path(source).stem).casefold()
                and (
                    not source_row.get("assembly_role")
                    or row.get("role") == source_row.get("assembly_role")
                )
            ), None)
        dependency = dependency or {}
        owned = [dependency] if dependency else []
        material_names = sorted({
            material.get("material_name", "")
            for dependency in owned
            for material in dependency.get("source_materials") or []
            if material.get("material_name")
        })
        mesh_ids = {
            str(mesh_id)
            for dependency in owned
            for mesh_id in dependency.get("source_mesh_ids") or []
        }
        output_textures = list(
            source_row.get("cluster_output_textures")
            or dependency.get("texture_dependencies")
            or []
        )
        output_paths = [
            row.get("path") if isinstance(row, dict) else str(row)
            for row in output_textures
        ]
        output_paths = [path for path in output_paths if path]
        missing_output_paths = list(
            source_row.get("missing_cluster_output_textures")
            or (dependency.get("tga_basename_validation") or {}).get("missing")
            or []
        )
        decision = (
            source_row.get("assembly_decision")
            or dependency.get("decision")
            or ""
        )
        referenced = bool(source_row.get("referenced"))
        handoff = decision_labels.get(decision, decision)
        if not handoff:
            handoff = "현재 모델에서 사용" if referenced else "현재 모델에서 사용 안 함"
        if bark_status == "replacement_required" and decision:
            handoff += " · bark 이름/연결 확인 필요"
        rows.append({
            "kind": "cluster_spm",
            "role": source_row.get("assembly_role") or dependency.get("role") or "",
            "name": source_row.get("name") or Path(source).stem,
            "source": source,
            "canonical_spm": source_row.get("authoring_spm") or source,
            "mirror_spm": source_row.get("mirror_spm") or "",
            "pair_status": source_row.get("pair_status") or "",
            "pair_action": source_row.get("pair_action") or "",
            "pair_conflicts": list(source_row.get("pair_conflicts") or []),
            "target_status": source_row.get("target_status") or {},
            "materials": (
                f"material {len(material_names)} · mesh {len(mesh_ids)}"
            ),
            "textures": cluster_texture_status_text(
                source_row,
                output_paths,
                missing_output_paths,
            ),
            "output_textures": output_paths,
            "missing_output_textures": missing_output_paths,
            "normalization_workflow_mode": source_row.get(
                "normalization_workflow_mode"
            ) or "",
            "direct_uv_source": source_row.get("direct_uv_source") or "",
            "physical_capture_manifest": source_row.get(
                "physical_capture_manifest"
            ) or "",
            "physical_capture_contract_sha256": source_row.get(
                "physical_capture_contract_sha256"
            ) or "",
            "physical_capture_resolution": list(
                source_row.get("physical_capture_resolution") or []
            ),
            "physical_capture_core_complete": bool(
                source_row.get("physical_capture_core_complete")
            ),
            "physical_capture_core_missing": list(
                source_row.get("physical_capture_core_missing") or []
            ),
            "handoff": handoff,
            "referenced": referenced,
            "references": [],
        })
    return rows


def apply_target_registry_to_connection_row(row):
    """Replace inferred targets with the exact JSON list when it exists."""
    try:
        registry = load_target_registry(row["blend"])
    except TargetRegistryError as exc:
        row["registry_error"] = str(exc)
        return row
    if registry is None:
        return row

    audited = row["spms_by_key"]
    listed = {}
    for raw_spm in registry["target_spms"]:
        spm = Path(raw_spm).expanduser().absolute()
        spm_key = os.path.normcase(str(spm)).casefold()
        target = dict(audited.get(spm_key) or {
            "spm": spm,
            "connected": None,
            "export_participating": row.get("export_participating", True),
            "priority": -1,
        })
        target["spm"] = spm
        target["listed_in_registry"] = True
        target["exists"] = spm.is_file()
        listed[spm_key] = target
    row["spms_by_key"] = listed
    row["registry_managed"] = True
    row["registry_path"] = registry["registry_path"]
    return row


def blender_connection_rows(
    item, validation_cache=None, *, verify_physical=True,
):
    """Return concrete blend files and their audited final-SPM connections.

    ``leaf_mesh_sources`` describes work/provenance while
    ``leaf_atlas_inventory`` describes the current Generator state. Both can
    mention the same blend, so current inventory has the final say for an
    explicit per-SPM connection result.
    """
    if item.get("_gui_blender_connection_pending"):
        return []
    cached = item.get("_gui_blender_connection_rows")
    if isinstance(cached, list):
        return cached
    rows_by_blend = {}
    collections = (
        (0, item.get("leaf_mesh_sources") or ()),
        (1, item.get("leaf_atlas_inventory") or ()),
    )
    for priority, sources in collections:
        for source in sources:
            targets = list(source.get("targets") or ())
            if not targets:
                targets = [
                    {"spm": spm}
                    for spm in source.get("target_spms") or ()
                    if spm
                ]
            source_active = bool(source.get("export_participating", True))
            source_connected = source.get("generator_connection_complete")
            for raw_blend in source.get("atlas_blends") or ():
                if not raw_blend:
                    continue
                blend = Path(raw_blend).expanduser().absolute()
                key = os.path.normcase(str(blend)).casefold()
                row = rows_by_blend.setdefault(key, {
                    "blend": blend,
                    "spms_by_key": {},
                    "export_participating": False,
                    "active_priority": -1,
                })
                if priority > row["active_priority"]:
                    row["export_participating"] = source_active
                    row["active_priority"] = priority
                elif priority == row["active_priority"]:
                    row["export_participating"] = bool(
                        row["export_participating"] or source_active
                    )
                for target in targets:
                    raw_spm = target.get("spm")
                    if not raw_spm:
                        continue
                    spm = Path(raw_spm).expanduser().absolute()
                    spm_key = os.path.normcase(str(spm)).casefold()
                    connected = target.get("generator_connection_complete")
                    if connected is None:
                        connected = source_connected
                    if connected is not None:
                        connected = bool(connected)
                    existing = row["spms_by_key"].get(spm_key)
                    if existing is None:
                        row["spms_by_key"][spm_key] = {
                            "spm": spm,
                            "connected": connected,
                            "export_participating": bool(
                                target.get(
                                    "export_participating", source_active
                                )
                            ),
                            "priority": priority,
                        }
                    elif priority >= existing["priority"]:
                        if connected is not None or existing["connected"] is None:
                            existing["connected"] = connected
                        existing["export_participating"] = bool(
                            target.get("export_participating", source_active)
                        )
                        existing["priority"] = priority

    # Cluster Normalizer outputs live beside their authoritative non-SK camera
    # SPM in the owner's Cluster folder.  Unlike ordinary atlas provenance,
    # every owner SK is a concrete ON/OFF candidate and must stay visible when
    # it is OFF.  Generator Sync is the mutation owner; PCG reads the same JSON.
    try:
        cluster_blends = discover_cluster_blend_relations(
            item.get("folder") or "",
            verify_physical=verify_physical,
            validation_cache=validation_cache,
        )
    except Exception as exc:
        cluster_blends = []
        item["cluster_blend_scan_error"] = str(exc)
    for cluster in cluster_blends:
        blend = Path(cluster["blend"]).expanduser().absolute()
        key = os.path.normcase(str(blend)).casefold()
        row = rows_by_blend.setdefault(key, {
            "blend": blend,
            "spms_by_key": {},
            "export_participating": True,
            "active_priority": 2,
        })
        row.update({
            "cluster_normalized": True,
            "managed_by": "spm_generator_sync",
            "source_spm": Path(cluster["source_spm"]),
            "registry_managed": cluster.get("registry_managed", False),
            "registry_path": str(cluster.get("registry_path") or ""),
            "registry_error": cluster.get("registry_error"),
            "folder_relation": cluster.get("folder_relation"),
            "owner_target_count": cluster.get("owner_target_count", 0),
            "owner_on_count": cluster.get("owner_on_count", 0),
            "owner_off_count": cluster.get("owner_off_count", 0),
            "export_participating": True,
            "active_priority": 2,
        })
        row["spms_by_key"] = {}
        for target in cluster.get("targets") or ():
            spm = Path(target["target_spm"]).expanduser().absolute()
            spm_key = os.path.normcase(str(spm)).casefold()
            row["spms_by_key"][spm_key] = {
                "spm": spm,
                "connected": target.get("connected"),
                "export_participating": bool(target.get("relation_on")),
                "priority": 2,
                "relation_on": bool(target.get("relation_on")),
                "relation_status": target.get("status") or "unknown",
                "material": target.get("material"),
                "manifest": target.get("manifest"),
                "listed_in_registry": bool(target.get("relation_on")),
                "exists": bool(target.get("exists")),
                "managed_by": "spm_generator_sync",
            }

    rows = []
    for row in rows_by_blend.values():
        if not row.get("cluster_normalized"):
            apply_target_registry_to_connection_row(row)
        spms = list(row.pop("spms_by_key").values())
        row.pop("active_priority", None)
        for target in spms:
            target.pop("priority", None)
        row["spms"] = sorted(
            spms,
            key=lambda target: (
                target["spm"].name.casefold(), str(target["spm"]).casefold()
            ),
        )
        rows.append(row)
    return sorted(
        rows,
        key=lambda row: (
            row["blend"].name.casefold(), str(row["blend"]).casefold()
        ),
    )


def _relation_item_payload(item):
    def source_payload(source):
        return {
            "atlas_blends": list(source.get("atlas_blends") or ()),
            "targets": [
                {
                    key: target.get(key)
                    for key in (
                        "spm",
                        "generator_connection_complete",
                        "export_participating",
                    )
                }
                for target in source.get("targets") or ()
            ],
            "target_spms": list(source.get("target_spms") or ()),
            "export_participating": source.get("export_participating"),
            "generator_connection_complete": source.get(
                "generator_connection_complete"
            ),
        }

    return {
        "folder": item.get("folder"),
        "name": item.get("name"),
        "chosen_spm": item.get("chosen_spm"),
        "leaf_mesh_sources": [
            source_payload(source)
            for source in item.get("leaf_mesh_sources") or ()
        ],
        "leaf_atlas_inventory": [
            source_payload(source)
            for source in item.get("leaf_atlas_inventory") or ()
        ],
    }


def _mutation_item_payload(item):
    """Keep every existing mutation-plan field outside the slim relation key."""
    return {
        key: item.get(key)
        for key in (
            "folder",
            "name",
            "chosen_spm",
            "leaf_mesh_sources",
            "leaf_atlas_inventory",
            "cluster_items",
            "target_spm_statuses",
            "sbs_files",
            "preserved_cluster_materials",
            "texture_dir",
            "normal_convention",
            "ao_policy",
            "sdf_policy",
            "pcg_target_names",
            "duplicate_target_names",
            # Atlas add/remove plans are derived from these rows. Reproduce
            # them from current bytes before granting mutation authority.
            "_gui_blender_connection_rows",
        )
    }


def _relation_capture_manifest_path(blend):
    blend = Path(blend).expanduser().absolute()
    stem = blend.stem
    if stem.casefold().startswith("sk_"):
        stem = stem[3:]
    return blend.with_name(f"{stem}_auto_capture_manifest.json")


def _relation_semantic_inventory(
        item, payload, *, directory_memo=None, json_memo=None):
    """Return the bounded exact dependency closure read by relation rows.

    Blender bytes and bulk texture pixels do not affect relation-row
    calculation.  Their paths and presence remain membership evidence, while
    current SPM/FBX and registry/scope/capture JSON bytes are exact.  JSON
    sidecars can name external SPM/FBX dependencies, so those paths are folded
    into the same bounded closure instead of depending on the broad audit
    payload by accident.
    """
    exact_suffixes = frozenset({
        ".fbx", ".json", ".spm", ".stmat", ".xml",
    })
    paths = {}
    expected = {}
    membership = set()
    directories = {}
    scope_target_stems = {}
    json_queue = []
    parsed_json = set()

    def normalize_candidate(value, *, base=None):
        text = os.fspath(value)
        candidate = Path(text).expanduser()
        if not candidate.is_absolute() and base is not None:
            candidate = Path(base) / candidate
        return candidate.absolute()

    def add_directory(value, *, role="owner", target_stem=None):
        directory = normalize_candidate(value)
        key = startup_path_key(directory)
        existing = directories.get(key)
        if existing is None or existing[1] != "scope":
            directories[key] = (directory, str(role))
        if role == "scope" and target_stem:
            scope_target_stems.setdefault(key, set()).add(
                str(target_stem).casefold()
            )

    def add_expected(value, *, exact=False, base=None):
        if not value:
            return
        candidate = normalize_candidate(value, base=base)
        key = startup_path_key(candidate)
        try:
            is_file = candidate.is_file()
        except OSError:
            is_file = False
        expected[key] = candidate
        membership.add(
            "expected:" + key + (":file" if is_file else ":missing")
        )
        if is_file and exact:
            paths[key] = candidate
            if candidate.suffix.casefold() == ".json":
                json_queue.append(candidate)

    def collect_projection(value, key=None):
        if isinstance(value, dict):
            for nested_key, nested in value.items():
                collect_projection(nested, nested_key)
            return
        if isinstance(value, (list, tuple, set, frozenset)):
            for nested in value:
                collect_projection(nested, key)
            return
        if not isinstance(value, (str, os.PathLike)):
            return
        text = os.fspath(value)
        suffix = Path(text).suffix.casefold()
        if not (
            isinstance(value, os.PathLike)
            or "\\" in text
            or "/" in text
            or suffix in RELATION_INPUT_SUFFIXES
        ):
            return
        if key == "folder":
            add_directory(text, role="owner")
            return
        is_blend = suffix == ".blend" or key == "atlas_blends"
        add_expected(text, exact=(suffix in exact_suffixes and not is_blend))
        if is_blend:
            blend = normalize_candidate(text)
            add_expected(registry_path_for_blend(blend), exact=True)
            add_expected(_relation_capture_manifest_path(blend), exact=True)

    collect_projection(payload)
    folder = normalize_candidate(item.get("folder") or "")
    add_directory(folder, role="owner")
    add_directory(folder / "Cluster", role="cluster")

    # Direct owner/Cluster entries drive cluster source/blend discovery.
    enumerated = set()
    scanned_entry_count = 0
    semantic_entry_count = 0
    while True:
        pending_directories = [
            (key, directory, role)
            for key, (directory, role) in directories.items()
            if key not in enumerated
        ]
        if not pending_directories:
            break
        for directory_key, directory, role in pending_directories:
            enumerated.add(directory_key)
            if not directory.is_dir():
                membership.add("directory:" + directory_key + ":missing")
                continue
            entries = (
                directory_memo.get(directory_key)
                if directory_memo is not None else None
            )
            if entries is None:
                try:
                    entries = tuple(sorted(
                        directory.iterdir(),
                        key=lambda path: path.name.casefold(),
                    ))
                except OSError as exc:
                    raise RuntimeError(
                        "Blender relation inputs could not enumerate "
                        f"{directory}: {exc}"
                    ) from exc
                if directory_memo is not None:
                    directory_memo[directory_key] = entries
            scanned_entry_count += len(entries)
            if scanned_entry_count > 32_768:
                raise BoundedDiscoveryError(
                    "Blender relation discovery exceeded 32768 directory "
                    "entries for "
                    + str(folder)
                )
            membership.add("directory:" + directory_key + ":present")
            for candidate in entries:
                suffix = candidate.suffix.casefold()
                if is_backup_path(candidate):
                    continue
                try:
                    is_file = candidate.is_file()
                    is_directory = candidate.is_dir()
                except OSError:
                    is_file = is_directory = False
                if is_directory:
                    continue
                if role == "scope":
                    name = candidate.name.casefold()
                    relevant = suffix == ".json" and any(
                        name.endswith(f"__{stem}.json")
                        for stem in scope_target_stems.get(
                            directory_key, ()
                        )
                    )
                else:
                    relevant = suffix == ".spm"
                if not is_file or not relevant:
                    continue
                semantic_entry_count += 1
                if semantic_entry_count > 4_096:
                    raise BoundedDiscoveryError(
                        "Blender relation semantic membership exceeded 4096 "
                        "entries for " + str(folder)
                    )
                candidate_key = startup_path_key(candidate)
                membership.add("entry:" + candidate_key + ":file")
                if role == "scope":
                    try:
                        selection_mtime_ns = candidate.stat().st_mtime_ns
                    except OSError as exc:
                        raise RuntimeError(
                            "Blender relation scope ordering could not stat "
                            f"{candidate}: {exc}"
                        ) from exc
                    membership.add(
                        "scope-selection-mtime:"
                        + candidate_key
                        + ":"
                        + str(selection_mtime_ns)
                    )
                # Owner/Cluster discovery reads SPM membership. Scope
                # discovery reads JSON membership. Registry and capture JSON
                # are added explicitly from each projected blend above, so
                # unrelated report/image files never amplify this identity.
                add_expected(candidate, exact=True)
                if role == "cluster" and suffix == ".spm":
                    # Cluster relation discovery derives normalized blends
                    # from every live canonical/legacy SPM pair.  Bind that
                    # derived path and both sidecars even when the primary
                    # report did not project the blend into a leaf row.
                    blend = normalized_blend_for_source(candidate)
                    add_expected(blend, exact=False)
                    add_expected(registry_path_for_blend(blend), exact=True)
                    add_expected(
                        _relation_capture_manifest_path(blend), exact=True
                    )

        # Scope selection is a glob over each exact target SPM's sibling dir.
        for candidate in list(paths.values()):
            if candidate.suffix.casefold() == ".spm":
                add_directory(
                    candidate.parent / ".atlas_leaf_speedtree_scopes",
                    role="scope",
                    target_stem=candidate.stem,
                )

        # Sidecars can declare exact external source artifacts.  Parsing is
        # discovery only; their bytes are still full-SHA bound below.
        while json_queue:
            json_path = json_queue.pop()
            json_key = startup_path_key(json_path)
            if json_key in parsed_json:
                continue
            parsed_json.add(json_key)
            try:
                document = (
                    json_memo.get(json_key)
                    if json_memo is not None and json_key in json_memo
                    else json.loads(json_path.read_text(encoding="utf-8"))
                )
                if json_memo is not None and json_key not in json_memo:
                    json_memo[json_key] = document
            except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
                continue

            def collect_declared(value, declared_key=None):
                if isinstance(value, dict):
                    for key, nested in value.items():
                        collect_declared(nested, str(key).casefold())
                    return
                if isinstance(value, list):
                    for nested in value:
                        collect_declared(nested, declared_key)
                    return
                if not isinstance(value, str):
                    return
                if declared_key == "source_fbx":
                    add_expected(
                        value,
                        exact=True,
                        base=json_path.parent,
                    )
                elif declared_key in {
                    "spm",
                    "source_spm",
                    "target_spm",
                    "target_spms",
                    "canonical_spm",
                    "mirror_spm",
                }:
                    add_expected(
                        value,
                        exact=True,
                        base=json_path.parent,
                    )

            collect_declared(document)

    return (
        paths,
        membership,
        len(expected),
        len(enumerated),
        scanned_entry_count,
        semantic_entry_count,
    )


def _relation_item_content_identity(
        item, *, content_memo=None, directory_memo=None, json_memo=None,
        exact_all=False, prepared_inventory=None, workers=8):
    """Bind relation/live-mutation evidence to payload content and membership.

    Every file that the report payload actually names is full-SHA bound.  The
    bounded directory inventory separately binds the complete relevant name
    set, so additions/removals remain invalidating without full-hashing every
    unrelated texture merely because it shares the folder.  A file whose
    bytes can affect the report must be present in the payload and therefore
    remains exact mutation evidence.
    """
    payload = (
        _mutation_item_payload(item)
        if exact_all else _relation_item_payload(item)
    )
    paths = {}

    def collect(value):
        if isinstance(value, dict):
            for nested in value.values():
                collect(nested)
            return
        if isinstance(value, (list, tuple, set, frozenset)):
            for nested in value:
                collect(nested)
            return
        if not isinstance(value, (str, os.PathLike)):
            return
        text = os.fspath(value)
        if not (
            isinstance(value, os.PathLike)
            or "\\" in text
            or "/" in text
            or Path(text).suffix.casefold() in RELATION_INPUT_SUFFIXES
        ):
            return
        candidate = Path(text).expanduser().absolute()
        if candidate.is_file():
            paths[startup_path_key(candidate)] = candidate

    collect(payload)
    folder = Path(item.get("folder") or "").expanduser().absolute()
    if not exact_all:
        inventory = prepared_inventory or _relation_semantic_inventory(
            item,
            payload,
            directory_memo=directory_memo,
            json_memo=json_memo,
        )
        (
            paths,
            membership,
            expected_count,
            enumerated_count,
            scanned_entry_count,
            semantic_entry_count,
        ) = inventory
        membership.add(
            "payload:" + canonical_json_sha256(startup_json_safe(payload))
        )
        identity = content_identity(
            paths.values(),
            membership=membership,
            memo=content_memo,
            max_files=2_048,
            exact=True,
            workers=workers,
        )
        identity.update({
            "exact_content_file_count": len(paths),
            "sampled_content_file_count": 0,
            "evidence_scope": (
                "exact-relation-semantic-dependencies-plus-bounded-"
                "membership-v2"
            ),
            "payload_content_file_count": len(paths),
            "membership_file_count": expected_count,
            "directory_count": enumerated_count,
            "scanned_directory_entry_count": scanned_entry_count,
            "semantic_membership_entry_count": semantic_entry_count,
        })
        return identity

    semantic_inventory = _relation_semantic_inventory(
        item,
        _relation_item_payload(item),
        directory_memo=directory_memo,
        json_memo=json_memo,
    )
    semantic_paths, semantic_membership = semantic_inventory[:2]
    paths.update(semantic_paths)

    directories = [folder, folder / "Cluster"]
    for path in list(paths.values()):
        directories.append(path.parent)
        directories.append(path.parent / ".atlas_leaf_speedtree_scopes")
    membership = set(semantic_membership)
    membership.add(
        "payload:" + canonical_json_sha256(startup_json_safe(payload))
    )
    membership_file_keys = set()
    seen_directories = set()
    entry_count = 0
    for directory in directories:
        directory_key = startup_path_key(directory)
        if directory_key in seen_directories or not directory.is_dir():
            continue
        seen_directories.add(directory_key)
        entries = (
            directory_memo.get(directory_key)
            if directory_memo is not None else None
        )
        if entries is None:
            try:
                entries = tuple(sorted(
                    directory.iterdir(), key=lambda path: path.name.casefold()
                ))
            except OSError as exc:
                raise RuntimeError(
                    "Blender relation inputs could not enumerate "
                    f"{directory}: {exc}"
                ) from exc
            if directory_memo is not None:
                directory_memo[directory_key] = entries
        entry_count += len(entries)
        if entry_count > 4_096:
            raise BoundedDiscoveryError(
                "Blender relation membership exceeded 4096 entries for "
                + str(folder)
            )
        for candidate in entries:
            if (
                candidate.is_file()
                and candidate.suffix.casefold() in RELATION_INPUT_SUFFIXES
                and not is_backup_path(candidate)
            ):
                key = startup_path_key(candidate)
                membership.add(key)
                membership_file_keys.add(key)
    if exact_all:
        identity = content_identity(
            paths.values(),
            membership=membership,
            memo=content_memo,
            max_files=2_048,
            exact=True,
            workers=8,
        )
        identity["exact_content_file_count"] = len(paths)
        identity["sampled_content_file_count"] = 0
        identity["evidence_scope"] = (
            "full-mutation-payload-plus-exact-relation-dependencies-plus-"
            "bounded-membership-v2"
        )
    else:
        exact_paths = [
            path for path in paths.values()
            if path.suffix.casefold() not in RELATION_BULK_IMAGE_SUFFIXES
        ]
        sampled_paths = [
            path for path in paths.values()
            if path.suffix.casefold() in RELATION_BULK_IMAGE_SUFFIXES
        ]
        exact_identity = content_identity(
            exact_paths,
            membership=membership,
            memo=content_memo,
            max_files=2_048,
            exact=True,
            workers=8,
        )
        sampled_identity = content_identity(
            sampled_paths,
            memo=content_memo,
            max_files=2_048,
            exact=False,
            workers=8,
        )
        identity = {
            "algorithm": "sha256-of-hybrid-content-keys-v1",
            "sha256": canonical_json_sha256({
                "exact": exact_identity["sha256"],
                "sampled": sampled_identity["sha256"],
                "membership": sorted(membership),
            }),
            "file_count": (
                exact_identity["file_count"]
                + sampled_identity["file_count"]
            ),
            "files": (
                exact_identity["files"] + sampled_identity["files"]
            ),
            "membership": sorted(membership),
            "exact_content_file_count": len(exact_paths),
            "sampled_content_file_count": len(sampled_paths),
            "evidence_scope": (
                "full-semantic-content-plus-sampled-bulk-images-plus-"
                "bounded-directory-membership-v1"
            ),
        }
    identity["payload_content_file_count"] = len(paths)
    identity["membership_file_count"] = len(membership_file_keys)
    return identity


def item_has_current_live_evidence(
        item, *, content_memo=None, directory_memo=None):
    """True only when a row's current bytes reproduce its live audit token."""
    expected = item.get("_gui_live_evidence")
    if not isinstance(expected, dict) or not expected.get("sha256"):
        return False
    try:
        current = _relation_item_content_identity(
            item,
            content_memo=content_memo,
            directory_memo=directory_memo,
        )
    except Exception:
        return False
    return current.get("sha256") == expected.get("sha256")


def require_current_live_evidence(
        item, *, content_memo=None, exact_content_memo=None,
        directory_memo=None):
    expected = item.get("_gui_live_evidence")
    if (
        not isinstance(expected, dict)
        or expected.get("relation_validation_mode") != "physical"
    ):
        raise RuntimeError(
            "deferred/display-only relation evidence cannot authorize mutation"
        )
    if not item_has_current_live_evidence(
        item,
        content_memo=content_memo,
        directory_memo=directory_memo,
    ):
        raise RuntimeError(
            "선택한 행의 현재 입력이 live 검사 증거와 일치하지 않습니다. "
            "다시 검사한 뒤 실행하세요."
        )
    # Bulk image bytes do not change relation semantics, but they are mutation
    # inputs.  Capture their full current identity inside the execution worker
    # immediately before any write/external job; this exact evidence is never
    # replaced by the sampled startup/cache identity.
    item["_gui_exact_mutation_evidence"] = _relation_item_content_identity(
        item,
        content_memo=exact_content_memo,
        directory_memo=directory_memo,
        exact_all=True,
    )
    return True


def mutation_semantic_digest(item):
    """Digest every report field from which mutation plans are derived."""
    return canonical_json_sha256(
        startup_json_safe(_mutation_item_payload(item))
    )


def _exact_mutation_item_key(item):
    return (
        startup_path_key((item or {}).get("folder") or "")
        + "|"
        + str((item or {}).get("name") or "").casefold()
    )


def seal_exact_mutation_baseline(
        items, *, action, plan_payload=None, authority_units=None,
        config_projection=None, tool_paths=None, receipt_path=None):
    """Seal current exact inputs after a selected-item semantic re-audit."""
    content_memo = {}
    exact_content_memo = {}
    directory_memo = {}
    rows = []
    seen = set()
    for item in items:
        if not isinstance(item, dict) or id(item) in seen:
            continue
        seen.add(id(item))
        require_current_live_evidence(
            item,
            content_memo=content_memo,
            exact_content_memo=exact_content_memo,
            directory_memo=directory_memo,
        )
        identity = item["_gui_exact_mutation_evidence"]
        rows.append({
            "item_key": _exact_mutation_item_key(item),
            "semantic_sha256": mutation_semantic_digest(item),
            "exact_sha256": identity["sha256"],
            "file_count": identity.get("file_count", 0),
            "membership": list(identity.get("membership") or ()),
            "item": item,
        })
    public_rows = [
        {key: value for key, value in row.items() if key != "item"}
        for row in rows
    ]
    if authority_units is None:
        authority_units = [{
            "unit_id": "all",
            "payload": plan_payload,
            "item_keys": [row["item_key"] for row in rows],
            "paths": (),
            "memberships": (),
        }]
    item_states_by_key = {
        row["item_key"]: {
            "exact_sha256": row["exact_sha256"],
            "file_count": row.get("file_count", 0),
            "membership": list(row.get("membership") or ()),
        }
        for row in rows
    }
    authority_units = [
        {
            **unit,
            "item_states": {
                key: item_states_by_key[key]
                for key in unit.get("item_keys") or ()
                if key in item_states_by_key
            },
        }
        for unit in authority_units
    ]
    unit_authority = capture_mutation_authority_manifest(
        action=action,
        logical_plan=plan_payload,
        config_projection=config_projection,
        tool_paths=(GUI_SOURCE_PATH, *(tool_paths or ())),
        units=authority_units,
        receipt_path=receipt_path,
    )
    plan_digest = canonical_json_sha256(startup_json_safe({
        "action": str(action),
        "items": public_rows,
        "plan": plan_payload,
        "authority_sha256": unit_authority["authority_sha256"],
    }))
    return {
        "schema_version": 1,
        "authority": "selected_current_reaudit_exact_baseline_v1",
        "action": str(action),
        "plan_digest": plan_digest,
        "items": rows,
        "plan_payload": startup_json_safe(plan_payload),
        "unit_authority": unit_authority,
        "writes_before_match": 0,
    }


def require_exact_mutation_baseline(
        baseline, *, unit_id=None, current_unit_payload=None,
        current_config=None):
    """Re-hash the selected unit immediately before its first write."""
    if not isinstance(baseline, dict) or baseline.get("schema_version") != 1:
        raise RuntimeError("exact mutation baseline is unavailable")
    authority = baseline.get("unit_authority")
    units = list((authority or {}).get("units") or ())
    if unit_id is None:
        if len(units) != 1:
            raise RuntimeError("exact mutation baseline unit is required")
        unit_id = units[0].get("unit_id")
    unit = next((
        row for row in units if row.get("unit_id") == str(unit_id)
    ), None)
    if unit is None:
        raise RuntimeError("exact mutation baseline unit is unavailable")
    unit_item_keys = set(unit.get("item_keys") or ())
    expected_public_rows = []
    content_memo = {}
    directory_memo = {}
    for row in baseline.get("items") or ():
        if row.get("item_key") not in unit_item_keys:
            continue
        item = row.get("item")
        if not isinstance(item, dict):
            raise RuntimeError("exact mutation baseline item is unavailable")
        current = _relation_item_content_identity(
            item,
            content_memo=content_memo,
            directory_memo=directory_memo,
            exact_all=True,
        )
        advanced_item = expected_mutation_item_state(
            authority, str(unit_id), row.get("item_key")
        )
        expected_exact_sha256 = (
            advanced_item.get("exact_sha256")
            if isinstance(advanced_item, dict)
            else row.get("exact_sha256")
        )
        if current.get("sha256") != expected_exact_sha256:
            raise RuntimeError(
                "선택 작업 입력이 exact 계획 생성 후 변경되었습니다. "
                "production 파일을 쓰지 않고 중단합니다."
            )
        expected_public_rows.append({
            key: value for key, value in row.items() if key != "item"
        })
    current_plan_digest = canonical_json_sha256(startup_json_safe({
        "action": baseline.get("action"),
        "items": [
            {key: value for key, value in row.items() if key != "item"}
            for row in baseline.get("items") or ()
        ],
        "plan": baseline.get("plan_payload"),
        "authority_sha256": (authority or {}).get("authority_sha256"),
    }))
    if current_plan_digest != baseline.get("plan_digest"):
        raise RuntimeError("exact mutation plan seal is invalid")
    require_mutation_authority_unit(
        authority,
        str(unit_id),
        current_payload=(
            unit.get("payload")
            if current_unit_payload is None else current_unit_payload
        ),
        current_config=current_config,
    )
    baseline["writes_before_match"] = len(
        authority.get("receipt", {}).get("authorized_units") or ()
    )
    return True


def complete_exact_mutation_unit(baseline, unit_id, *, post_paths=None):
    authority = baseline["unit_authority"]
    unit = next((
        row for row in authority.get("units") or ()
        if row.get("unit_id") == str(unit_id)
    ), None)
    if unit is None:
        raise RuntimeError("exact mutation baseline unit is unavailable")
    unit_item_keys = set(unit.get("item_keys") or ())
    content_memo = {}
    directory_memo = {}
    item_post_states = {}
    for row in baseline.get("items") or ():
        item_key = row.get("item_key")
        if item_key not in unit_item_keys:
            continue
        current = _relation_item_content_identity(
            row["item"],
            content_memo=content_memo,
            directory_memo=directory_memo,
            exact_all=True,
        )
        item_post_states[str(item_key)] = {
            "exact_sha256": current.get("sha256"),
            "file_count": current.get("file_count", 0),
            "membership": list(current.get("membership") or ()),
        }
    receipt = complete_mutation_authority_unit(
        authority,
        str(unit_id),
        post_paths=post_paths,
        item_post_states=item_post_states,
    )
    baseline["writes_before_match"] = len(
        receipt.get("authorized_units") or ()
    )
    return receipt


def exact_mutation_unit_id(baseline, preferred):
    units = list(
        (baseline or {}).get("unit_authority", {}).get("units") or ()
    )
    if any(unit.get("unit_id") == str(preferred) for unit in units):
        return str(preferred)
    if len(units) == 1:
        return str(units[0].get("unit_id"))
    raise RuntimeError(f"exact mutation unit is unavailable: {preferred}")


def capture_runtime_mutation_authority(
        *, action, unit_id, payload, paths, memberships=(),
        write_paths=(), write_memberships=(),
        config_projection=None, tool_paths=()):
    """Seal a plan that is intentionally derived after an earlier unit."""
    receipt_path = REPORT_DIR / (
        "mutation_authority_"
        + str(action).replace("/", "_").replace("\\", "_")
        + "_"
        + datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        + ".json"
    )
    return capture_mutation_authority_manifest(
        action=action,
        logical_plan=payload,
        config_projection=config_projection,
        tool_paths=(GUI_SOURCE_PATH, *(tool_paths or ())),
        units=[{
            "unit_id": unit_id,
            "payload": payload,
            "item_keys": [],
            "paths": paths,
            "write_paths": write_paths,
            "memberships": memberships,
            "write_memberships": write_memberships,
        }],
        receipt_path=receipt_path,
    )


def _restore_relation_paths(value, key=None):
    path_fields = {
        "blend", "spm", "source_spm", "target_spm",
        "canonical_spm", "mirror_spm",
    }
    if isinstance(value, dict):
        return {
            nested_key: _restore_relation_paths(nested, nested_key)
            for nested_key, nested in value.items()
        }
    if isinstance(value, list):
        return [_restore_relation_paths(nested, key) for nested in value]
    if key in path_fields and isinstance(value, str) and value:
        return Path(value)
    return value


def cache_blender_connection_rows(
    report,
    progress_callback=None,
    cancel_check=None,
    metrics=None,
    session_evidence=None,
    *,
    verify_physical=True,
    read_cache=True,
):
    """Resolve or content-validate relation rows off the Tk main thread."""
    items = list((report or {}).get("items") or [])
    if not items:
        if metrics is not None:
            metrics.update({"item_count": 0, "cache_hits": 0, "cache_misses": 0})
        return report
    for item in items:
        item.pop("_gui_blender_connection_pending", None)
        item.pop("_gui_live_evidence", None)

    cache = ContentAddressedJsonCache(
        BLENDER_RELATION_CACHE_PATH,
        BLENDER_RELATION_CACHE_KIND,
        max_entries=512,
    )
    identities = {}
    misses = []
    completed = 0
    cache_hits = 0
    cache_updates = []
    changed_during_scan = 0
    exact_rows = (
        session_evidence.get("exact_content_rows")
        if isinstance(session_evidence, dict)
        and session_evidence.get("schema_version") == 1
        and session_evidence.get("kind")
        == "pcg_refresh_exact_content_evidence"
        else {}
    )
    session_exact_candidates = {
        str(key): dict(row)
        for key, row in (exact_rows or {}).items()
        if isinstance(row, dict)
        and row.get("fingerprint_algorithm") == "sha256-full-v1"
        and row.get("fingerprint")
    }
    # Session evidence is a same-refresh acceleration candidate, never
    # current-content authority.  Cache hits must hash the current bytes too,
    # including same-size/restored-mtime tampering after the primary audit.
    first_pass_content_memo = {}
    reused_exact_content_file_count = 0
    first_pass_directory_memo = {}
    first_pass_json_memo = {}
    final_pass_content_memo = {}
    final_pass_directory_memo = {}
    final_pass_json_memo = {}

    def prepare_inventories(
            target_items, content_memo, directory_memo, json_memo):
        prepared = {}
        union_paths = {}
        for target_item in target_items:
            if cancel_check is not None and cancel_check():
                raise RuntimeError(
                    "PCG Blender relation calculation cancelled"
                )
            payload = _relation_item_payload(target_item)
            inventory = _relation_semantic_inventory(
                target_item,
                payload,
                directory_memo=directory_memo,
                json_memo=json_memo,
            )
            prepared[id(target_item)] = inventory
            for path in inventory[0].values():
                union_paths[startup_path_key(path)] = path
        # Hash the generation-wide dependency union once. Per-item identity
        # assembly below is then a deterministic in-memory projection instead
        # of creating 55 executor pools and reopening shared files.
        content_identity(
            union_paths.values(),
            memo=content_memo,
            max_files=10_000,
            exact=True,
            workers=8,
        )
        return prepared

    first_pass_prepared = prepare_inventories(
        items,
        first_pass_content_memo,
        first_pass_directory_memo,
        first_pass_json_memo,
    )

    def update_metrics(status):
        if metrics is None:
            return
        unique_file_rows = {
            str(row.get("path") or "").casefold(): row
            for _namespace, identity in identities.values()
            for row in identity.get("files") or ()
            if row.get("path")
        }
        membership_paths = {
            member
            for _namespace, identity in identities.values()
            for member in identity.get("membership") or ()
            if not str(member).startswith("payload:")
        }
        metrics.update({
            "status": str(status),
            "item_count": len(items),
            "completed_item_count": completed,
            "cache_hits": cache_hits,
            "cache_misses": len(misses),
            "content_file_count": sum(
                identity["file_count"]
                for _namespace, identity in identities.values()
            ),
            "unique_content_file_count": len(unique_file_rows),
            "content_bytes": sum(
                row.get("size", 0)
                for _namespace, identity in identities.values()
                for row in identity.get("files") or ()
            ),
            "unique_content_bytes": sum(
                row.get("size", 0) for row in unique_file_rows.values()
            ),
            "membership_file_count": sum(
                identity.get("membership_file_count", 0)
                for _namespace, identity in identities.values()
            ),
            "unique_membership_file_count": len(membership_paths),
            "changed_during_scan": changed_during_scan,
            "first_pass_physical_content_reads": len(
                first_pass_content_memo
            ) - reused_exact_content_file_count,
            "reused_exact_content_file_count": (
                reused_exact_content_file_count
            ),
            "session_exact_candidate_count": len(
                session_exact_candidates
            ),
            "first_pass_directory_enumerations": len(
                first_pass_directory_memo
            ),
            "first_pass_json_documents": len(first_pass_json_memo),
            "final_pass_physical_content_reads": len(
                final_pass_content_memo
            ),
            "final_pass_directory_enumerations": len(
                final_pass_directory_memo
            ),
            "final_pass_json_documents": len(final_pass_json_memo),
            "content_identity_algorithm": (
                "sha256-of-full-content-keys-v1"
            ),
            "content_identity_scope": (
                "exact-relation-semantic-dependencies-plus-bounded-"
                "membership-v2"
            ),
            "exact_content_file_count": sum(
                identity.get("exact_content_file_count", 0)
                for _namespace, identity in identities.values()
            ),
            "sampled_content_file_count": sum(
                identity.get("sampled_content_file_count", 0)
                for _namespace, identity in identities.values()
            ),
            "mutation_authority_algorithm": (
                "sha256-of-full-content-keys-v1"
            ),
            "physical_validation": (
                "full" if verify_physical else "deferred"
            ),
            "persisted_cache_policy": (
                "read_write" if read_cache else "fresh_projection"
            ),
        })

    for item in items:
        if cancel_check is not None and cancel_check():
            update_metrics("canceled")
            raise RuntimeError("PCG Blender relation calculation cancelled")
        identity = _relation_item_content_identity(
            item,
            content_memo=first_pass_content_memo,
            directory_memo=first_pass_directory_memo,
            json_memo=first_pass_json_memo,
            prepared_inventory=first_pass_prepared[id(item)],
            workers=1,
        )
        validation_mode = "physical" if verify_physical else "deferred"
        identity["relation_validation_mode"] = validation_mode
        namespace = (
            startup_path_key(item.get("folder") or item.get("name") or "")
            + "|" + validation_mode
        )
        identities[id(item)] = (namespace, identity)
        cached = (
            cache.get(namespace, identity["sha256"])
            if read_cache else None
        )
        if isinstance(cached, list):
            item["_gui_blender_connection_rows"] = _restore_relation_paths(
                cached
            )
            item["_gui_live_evidence"] = identity
            cache_hits += 1
            completed += 1
            if progress_callback is not None:
                progress_callback(completed, len(items), item)
        else:
            misses.append(item)

    with ThreadPoolExecutor(
        max_workers=min(4, len(misses) or 1),
        thread_name_prefix="pcg-gui-relations",
    ) as executor:
        relation_validation_cache = RelationValidationCache()
        for path_key, row in first_pass_content_memo.items():
            relation_validation_cache.seed(
                ("sha256", str(path_key)),
                row["fingerprint"],
            )
        for path_key, row in (
            (session_evidence or {}).get(
                "spm_structural_semantic_rows"
            ) or {}
        ).items():
            current_row = first_pass_content_memo.get(str(path_key))
            if (
                not isinstance(current_row, dict)
                or current_row.get("fingerprint")
                != row.get("raw_sha256")
            ):
                continue
            relation_validation_cache.seed(
                (
                    "spm_structural_semantic",
                    str(path_key),
                    str(row.get("raw_sha256") or ""),
                ),
                row.get("semantic_fingerprint"),
            )
        futures = {
            executor.submit(
                blender_connection_rows,
                item,
                relation_validation_cache,
                verify_physical=verify_physical,
            ): item
            for item in misses
        }
        calculated_rows = {}
        for future in as_completed(futures):
            if cancel_check is not None and cancel_check():
                for pending in futures:
                    pending.cancel()
                update_metrics("canceled")
                raise RuntimeError("PCG Blender relation calculation cancelled")
            item = futures[future]
            try:
                calculated_rows[id(item)] = future.result()
            except Exception:
                update_metrics("error")
                raise
        final_pass_prepared = prepare_inventories(
            misses,
            final_pass_content_memo,
            final_pass_directory_memo,
            final_pass_json_memo,
        )
        # Re-observe every miss only after all relation calculations finish.
        # Shared memos avoid re-hashing/re-enumerating common inputs once per
        # row while preserving a distinct post-compute snapshot.
        for item in misses:
            if cancel_check is not None and cancel_check():
                update_metrics("canceled")
                raise RuntimeError("PCG Blender relation calculation cancelled")
            namespace, before = identities[id(item)]
            after = _relation_item_content_identity(
                item,
                content_memo=final_pass_content_memo,
                directory_memo=final_pass_directory_memo,
                json_memo=final_pass_json_memo,
                prepared_inventory=final_pass_prepared[id(item)],
                workers=1,
            )
            after["relation_validation_mode"] = (
                "physical" if verify_physical else "deferred"
            )
            if before["sha256"] != after["sha256"]:
                changed_during_scan += 1
                update_metrics("changed_during_scan")
                raise RuntimeError(
                    "Blender relation inputs changed during calculation: "
                    + str(item.get("folder") or item.get("name") or "")
                )
            rows = calculated_rows[id(item)]
            item["_gui_blender_connection_rows"] = rows
            item["_gui_live_evidence"] = after
            cache_updates.append((namespace, after["sha256"], rows))
            completed += 1
            if progress_callback is not None:
                progress_callback(completed, len(items), item)
    if cache_updates:
        cache.put_many(cache_updates)
    update_metrics("ok")
    return report


def mark_blender_connection_rows_pending(report):
    """Keep relation discovery off the Tk thread until its worker finishes."""
    for item in (report or {}).get("items") or []:
        item.pop("_gui_blender_connection_rows", None)
        item.pop("_gui_live_evidence", None)
        item["_gui_blender_connection_pending"] = True
    return report


def blender_connection_summary(row):
    """Return a compact connection summary for one concrete blend file."""
    targets = row.get("spms") or ()
    prefix = "비활성 기록 · " if not row.get("export_participating", True) else ""
    if row.get("registry_error"):
        return prefix + "대상 JSON 오류"
    if not targets:
        return prefix + "연결 SPM 없음"
    if row.get("cluster_normalized"):
        on_count = sum(bool(target.get("relation_on")) for target in targets)
        off_count = len(targets) - on_count
        folder_relation = row.get("folder_relation")
        if folder_relation == "partial":
            return (
                f"관계 PARTIAL {on_count}/{len(targets)} · "
                "Generator Sync에서 ON/OFF 정규화 필요"
            )
        if folder_relation == "off":
            return f"관계 OFF · 폴더 SK {len(targets)}개"
        pending = sum(
            target.get("relation_on")
            and target.get("relation_status") not in {"synced"}
            for target in targets
        )
        summary = f"관계 ON {on_count} · OFF {off_count}"
        if pending:
            summary += f" · 동기화 필요 {pending}개"
        elif on_count:
            summary += " · 메시 교체 완료 ✓"
        return summary
    pending = sum(target.get("connected") is False for target in targets)
    unknown = sum(target.get("connected") is None for target in targets)
    missing = sum(target.get("exists") is False for target in targets)
    summary = f"연결 SPM {len(targets)}개"
    if pending:
        summary += f" · 점검 필요 {pending}개"
    if unknown:
        summary += f" · 상태 미확인 {unknown}개"
    if missing:
        summary += f" · 파일 없음 {missing}개"
    if not pending and not unknown:
        summary += " · 연결 완료 ✓"
    return prefix + summary


def blender_connection_overview(item):
    """Summarize active blend/SPM connections for the fourth board column."""
    if item.get("_gui_blender_connection_pending"):
        return "연결 계산 중…"
    rows = blender_connection_rows(item)
    if not rows:
        return "blend 없음"
    active = [row for row in rows if row.get("export_participating", True)]
    inactive_count = len(rows) - len(active)
    pending = sum(
        target.get("connected") is False
        and target.get("relation_on", True)
        for row in active for target in row.get("spms") or ()
    )
    unknown = sum(
        target.get("connected") is None
        and target.get("relation_on", True)
        for row in active for target in row.get("spms") or ()
    )
    missing = sum(
        target.get("exists") is False
        for row in active for target in row.get("spms") or ()
    )
    registry_errors = sum(bool(row.get("registry_error")) for row in active)
    partial_relations = sum(
        bool(
            row.get("cluster_normalized")
            and row.get("folder_relation") == "partial"
        )
        for row in active
    )
    unlinked = sum(not row.get("spms") for row in active)
    parts = [f"blend {len(rows)}개"]
    relation_off = sum(
        bool(
            row.get("cluster_normalized")
            and row.get("folder_relation") == "off"
        )
        for row in active
    ) + sum(
        target.get("relation_on") is False
        for row in active if not row.get("cluster_normalized")
        for target in row.get("spms") or ()
    )
    if not active:
        parts.append("활성 연결 없음")
    elif pending or unknown or unlinked or missing or registry_errors or partial_relations:
        if pending:
            parts.append(f"점검 {pending}개")
        if unknown:
            parts.append(f"미확인 {unknown}개")
        if unlinked:
            parts.append(f"SPM 없음 {unlinked}개")
        if missing:
            parts.append(f"파일 없음 {missing}개")
        if registry_errors:
            parts.append(f"JSON 오류 {registry_errors}개")
        if partial_relations:
            parts.append(f"부분 연결 {partial_relations}개")
    else:
        parts.append("연결 완료 ✓")
    if inactive_count:
        parts.append(f"비활성 {inactive_count}개")
    if relation_off:
        parts.append(f"관계 OFF {relation_off}개")
    return " · ".join(parts)


def leaf_target_material_names(target):
    """Return the source material names used to locate Generator slots."""
    return list(
        target.get("source_material_names")
        or target.get("material_names")
        or []
    )


def leaf_target_connection_complete(target):
    """Read the explicit audit result for one final-SK target.

    A missing value is deliberately *not* treated as complete.  Older reports
    only proved that a blend existed, which is the false-positive this status
    is intended to prevent.
    """
    if "generator_connection_complete" in target:
        return bool(target.get("generator_connection_complete"))
    if "generator_connection_update_needed" in target:
        return not bool(target.get("generator_connection_update_needed"))
    return False


def leaf_source_step2_state(source):
    """Summarize blend generation and final-SK Generator connection."""
    has_blend = bool(source.get("atlas_blends"))
    source_contract_state = str(
        source.get("texture_contract_state") or ""
    )
    source_blocked = (
        not has_blend
        and source_contract_state.startswith("blocked_")
    )
    targets = [
        target for target in source.get("targets", []) if target.get("spm")
    ]
    connection_required = bool(targets)
    explicit_target_results = connection_required and all(
        "generator_connection_complete" in target for target in targets
    )
    if not connection_required:
        connection_complete = True
    elif explicit_target_results:
        connection_complete = all(
            leaf_target_connection_complete(target) for target in targets
        )
    elif "generator_connection_complete" in source:
        connection_complete = bool(source.get("generator_connection_complete"))
    elif "generator_connection_update_needed" in source:
        connection_complete = not bool(
            source.get("generator_connection_update_needed"))
    else:
        connection_complete = all(
            leaf_target_connection_complete(target) for target in targets
        )
    return {
        "has_blend": has_blend,
        "connection_required": connection_required,
        "connection_complete": connection_complete,
        "complete": has_blend and connection_complete,
        "needs_build": not has_blend and not source_blocked,
        "needs_connection": has_blend and connection_required
        and not connection_complete,
        "source_blocked": source_blocked,
        "source_contract_state": source_contract_state,
        "targets": targets,
    }


def step2_target_payload(job):
    """Build the versioned JSON contract consumed by the Blender job."""
    targets = []
    for detail in job.get("target_details") or []:
        targets.append({
            "spm": detail["spm"],
            "source_material_names": list(
                detail.get("source_material_names") or []),
            "source_material_ids": list(
                detail.get("source_material_ids") or []),
            "generator_bindings": list(
                detail.get("generator_bindings") or []),
        })
    return {"version": 1, "targets": targets}


def mutation_config_projection(cfg, keys):
    """Return only config values that can alter one mutation action."""
    return {
        str(key): startup_json_safe((cfg or {}).get(key))
        for key in keys
    }


def mutation_source_path(value):
    """Return the loaded Python source that implements a mutation helper."""
    module = sys.modules.get(getattr(value, "__module__", ""))
    source = getattr(module, "__file__", None)
    return Path(source).absolute() if source else None


def step1_unit_payload(row):
    item = row.get("item") or {}
    return {
        "folder": str(item.get("folder") or ""),
        "mesh": str(row.get("mesh") or ""),
        "exclude": list(row.get("exclude") or ()),
    }


def step1_authority_units(rows):
    units = []
    for index, row in enumerate(rows or (), 1):
        item = row.get("item") or {}
        folder = Path(item.get("folder") or "").absolute()
        item_key = _exact_mutation_item_key(item)
        units.append({
            "unit_id": f"step1:{index}:{startup_path_key(folder)}",
            "payload": step1_unit_payload(row),
            "item_keys": [item_key],
            "write_item_keys": [item_key],
            "paths": [folder],
            "write_paths": [folder],
            "memberships": [folder, folder / "Cluster"],
            "write_memberships": [folder, folder / "Cluster"],
        })
    return units


def step2_exact_plan_payload(jobs, push_spm):
    """Return the current command/producer scope sealed before Step 2 writes."""
    return {
        "push_spm": bool(push_spm),
        "jobs": [
            {
                "base": str(job.get("base") or ""),
                "albedo": str(job.get("albedo") or ""),
                "alpha": str(job.get("alpha") or ""),
                "blend_out": str(job.get("blend_out") or ""),
                "reuse_existing_blend": bool(
                    job.get("reuse_existing_blend")
                ),
                "target_payload": step2_target_payload(job),
            }
            for job in jobs or ()
        ],
    }


def step2_unit_payload(job, push_spm):
    return {
        "push_spm": bool(push_spm),
        "job": step2_exact_plan_payload([job], push_spm)["jobs"][0],
    }


def step2_authority_units(jobs, push_spm):
    units = []
    for index, job in enumerate(jobs or (), 1):
        items = list(job.get("items") or [job.get("item")])
        paths = [
            job.get("albedo"),
            job.get("alpha"),
            job.get("blend_out"),
            *(job.get("target_spms") or ()),
        ]
        write_paths = [
            job.get("blend_out"),
            *(job.get("target_spms") or ()),
        ]
        memberships = [
            Path(path).parent / ".atlas_leaf_speedtree_scopes"
            for path in job.get("target_spms") or ()
        ]
        item_keys = [
            _exact_mutation_item_key(item)
            for item in items if isinstance(item, dict)
        ]
        units.append({
            "unit_id": f"step2:{index}:{job.get('base') or ''}",
            "payload": step2_unit_payload(job, push_spm),
            "item_keys": item_keys,
            "write_item_keys": item_keys,
            "paths": [path for path in paths if path],
            "write_paths": [path for path in write_paths if path],
            "memberships": memberships,
            "write_memberships": memberships,
        })
    return units


def step3_exact_plan_payload(plan):
    """Return Step 3's producer/consumer scope without mutable UI objects."""
    return {
        "jobs": [
            {
                key: value
                for key, value in job.items()
                if key not in {"item", "items"}
            }
            for job in (plan or {}).get("jobs") or ()
        ],
        "sync_files": [
            str(path) for path in (plan or {}).get("sync_files") or ()
        ],
        "exact_step3_spms": [
            str(path) for path in (plan or {}).get("exact_step3_spms") or ()
        ],
        "normalization_plan": startup_json_safe(
            (plan or {}).get("normalization_plan") or {}
        ),
        "force_unreal_verify": bool(
            (plan or {}).get("force_unreal_verify")
        ),
        "eligible_row_keys": sorted(
            list((plan or {}).get("eligible_row_keys") or ())
        ),
        "pending_manifest_rows": [
            {
                "row": {
                    key: value
                    for key, value in (row or {}).items()
                    if key != "item"
                },
                "files": [str(path) for path in files or ()],
            }
            for row, files in (
                (plan or {}).get("pending_manifest_rows") or ()
            )
        ],
        "require_all_renders_for_sync": bool(
            (plan or {}).get("require_all_renders_for_sync")
        ),
        "operation_label": str(
            (plan or {}).get("operation_label") or ""
        ),
    }


def step3_job_payload(job):
    return {
        key: value
        for key, value in job.items()
        if key not in {"item", "items"}
    }


def step3_authority_units(plan):
    units = []
    for index, (row, files) in enumerate(
            (plan or {}).get("pending_manifest_rows") or (), 1):
        item = None
        if isinstance(row, dict):
            item = row.get("item") or row
        texture_root = (
            canonical_texture_root(item.get("folder"))
            if isinstance(item, dict) and item.get("folder")
            else None
        )
        manifest_path = (
            texture_root / "pcg_st9_canonical_outputs.json"
            if texture_root is not None else None
        )
        item_keys = (
            [_exact_mutation_item_key(item)]
            if isinstance(item, dict) else []
        )
        units.append({
            "unit_id": f"step3-manifest:{index}",
            "payload": {
                "row": row,
                "files": [str(path) for path in files or ()],
            },
            "item_keys": item_keys,
            "write_item_keys": item_keys,
            "paths": [
                *(files or ()),
                *([manifest_path] if manifest_path is not None else ()),
            ],
            "write_paths": (
                [manifest_path] if manifest_path is not None else []
            ),
            "memberships": (
                [texture_root / "_pcgtex_generated"]
                if texture_root is not None else []
            ),
        })
    for index, job in enumerate((plan or {}).get("jobs") or (), 1):
        dependency_paths = step3_job_dependency_paths(job)
        planned_outputs = output_paths(
            job["out_dir"], job["texture_base"]
        ).values()
        items = list(job.get("items") or [job.get("item")])
        item_keys = [
            _exact_mutation_item_key(item)
            for item in items if isinstance(item, dict)
        ]
        write_paths = [
            *([job.get("sbs")] if job.get("sbs") else ()),
            *planned_outputs,
        ]
        units.append({
            "unit_id": f"step3-render:{index}:{job.get('base') or ''}",
            "payload": step3_job_payload(job),
            "item_keys": item_keys,
            "write_item_keys": item_keys,
            "paths": [*dependency_paths, *write_paths],
            "write_paths": write_paths,
            "memberships": [
                Path(path).parent / ".atlas_leaf_speedtree_scopes"
                for path in dependency_paths
                if Path(path).suffix.casefold() == ".spm"
            ],
        })
    if not units:
        units.append({
            "unit_id": "step3-preflight-only",
            "payload": step3_exact_plan_payload(plan),
            "item_keys": [],
            "paths": list((plan or {}).get("sync_files") or ()),
            "memberships": [],
        })
    return units


def existing_leaf_blend(source):
    """Return the first audited blend that still exists on disk."""
    for value in source.get("atlas_blends") or []:
        path = Path(value)
        if path.is_file():
            return path
    return None


def pending_leaf_targets(source):
    """Return final-SK targets that still require Generator connection."""
    targets = [
        target for target in source.get("targets", []) if target.get("spm")
    ]
    pending = [
        target for target in targets
        if not leaf_target_connection_complete(target)
    ]
    if targets and all(
            "generator_connection_complete" in target for target in targets):
        return pending
    # If only the source aggregate is stale, conservatively revalidate every
    # target instead of scheduling an empty connection job.
    if not pending and targets and not leaf_source_step2_state(source)["connection_complete"]:
        return targets
    return pending


def merge_step2_target_detail(job, target):
    """Merge one exact target mapping into a shared atlas job."""
    spm = str(target.get("spm") or "")
    key = spm.lower()
    detail = next(
        (row for row in job["target_details"]
         if str(row.get("spm") or "").lower() == key),
        None,
    )
    if detail is None:
        detail = {
            "spm": spm,
            "source_material_names": [],
            "source_material_ids": [],
            "generator_bindings": [],
        }
        job["target_details"].append(detail)
        job["target_spms"].append(spm)
    for field, values in (
        ("source_material_names", leaf_target_material_names(target)),
        ("source_material_ids", target.get("source_material_ids") or []),
        ("generator_bindings", target.get("generator_bindings") or []),
    ):
        for value in values:
            if value not in detail[field]:
                detail[field].append(value)


def validate_step2_job_report(data, require_generator_connections=False):
    """Reject partial Blender reports before the GUI counts a job successful."""
    if data.get("status") != "ok":
        raise RuntimeError(data.get("error") or "Blender atlas job failed")
    if require_generator_connections:
        if not data.get("spm_built"):
            raise RuntimeError("최종 SK 반영 결과가 보고되지 않았습니다")
        if data.get("generator_connections_complete") is not True:
            raise RuntimeError("Generator Material/Mesh 연결 검증이 완료되지 않았습니다")


def blender_user_startup_path(blender_exe):
    version = Path(blender_exe).parent.name.rsplit(" ", 1)[-1]
    appdata = os.environ.get("APPDATA")
    if not appdata or not version or not version[0].isdigit():
        return None
    startup = (Path(appdata) / "Blender Foundation" / "Blender" /
               version / "config" / "startup.blend")
    return startup if startup.is_file() else None


def atlas_blender_command(blender_exe):
    """Save a clean atlas file with PARK's startup UI/workspaces embedded."""
    startup = blender_user_startup_path(blender_exe)
    # Always isolate background work from interactive/GPU user add-ons. The
    # startup file is optional and only supplies PARK's screens/workspaces.
    command = [blender_exe, "--factory-startup", "--background"]
    if startup:
        command.append(str(startup))
    return command + [
        "--python", str(TOOL_DIR / "jobs" / "atlas_blend_job.py"), "--",
    ]

# 자동 적용을 막는 문제들의 한국어 설명
BLOCKER_TEXT = {
    "duplicate": "같은 이름이 다른 폴더에도 매칭됨 — 어느 폴더가 진짜인지 먼저 확인",
    "source_review": "이 폴더에서 원본 SPM을 못 찾음 — 파일 이름을 직접 확인 필요",
    "generic_only": "남은 작업이 'Material 2' 같은 기본 이름뿐 — SpeedTree에서 이름 지은 뒤 다시 ① 실행",
    "pair_conflict": "Cluster output 이름을 canonical SK 규격으로 정규화할 수 없어 중단",
    "material_name_conflict": "Cluster M_ 정리 결과가 기존 재료 이름과 겹침 — 중복 이름을 만들지 않고 중단",
}


def split_generic(names):
    """머티리얼 이름을 (정상 이름, 'Material 2' 같은 기본 이름)으로 나눈다."""
    unprefixed = [
        n for n in names
        if not str(n).strip().lower().startswith("m_")
    ]
    generic = [n for n in unprefixed if GENERIC_MATERIAL_RE.match(str(n).strip())]
    normal = [n for n in unprefixed if n not in generic]
    return normal, generic


def generic_material_issues(item):
    """Return exact SPM/material locations for default SpeedTree names."""
    issues = []
    statuses = item.get("target_spm_statuses") or []
    if statuses:
        for status in statuses:
            _normal, generic = split_generic(
                status.get("materials_missing_m_prefix", []))
            spm = status.get("sk_spm") or status.get("source_spm") or ""
            for material in generic:
                issues.append({
                    "mesh_name": status.get("mesh_name", ""),
                    "spm": spm,
                    "material": material,
                })
        return issues
    _normal, generic = split_generic(item.get("materials_missing_m_prefix", []))
    spm = item.get("chosen_spm") or next(iter(item.get("sk_spms") or []), "")
    for material in generic:
        issues.append({"mesh_name": "", "spm": spm, "material": material})
    return issues


def generic_material_summary(item):
    issues = generic_material_issues(item)
    if not issues:
        return ""
    grouped = {}
    for issue in issues:
        label = Path(issue["spm"]).name if issue["spm"] else "SPM 미상"
        grouped.setdefault(label, []).append(issue["material"])
    if len(issues) == 1:
        issue = issues[0]
        return f"⚠ {Path(issue['spm']).name or 'SPM 미상'} → {issue['material']}"
    locations = ", ".join(
        f"{spm}({len(materials)})" for spm, materials in grouped.items())
    return f"⚠ 기본명 {len(issues)}개 · {locations}"


class Tooltip:
    """말풍선 도움말: 위젯에 마우스를 올리면 설명이 뜬다."""

    def __init__(self, widget, text, wrap=420):
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
        self.startup_latency = StartupLatencyTracker(
            selected_perf=getattr(
                root, "_speedtree_tab_selected_perf", time.perf_counter()
            ),
            source_provenance={
                "path": str(GUI_SOURCE_PATH),
                "sha256": GUI_SOURCE_SHA256,
                "module_loaded_at": GUI_MODULE_LOADED_AT,
                "process_id": os.getpid(),
            },
        )
        self.cfg = load_config()
        self.report = None
        self.items = {}  # iid(folder) -> {"item": dict, "checked": bool}
        self.target_items = {}  # iid(SPM) -> exact per-mesh ① target
        self.row_copy_paths = {}  # every visible iid -> exact copied paths
        self.blend_connection_meta = {}
        self.checked_rows = CheckedRowController(self.items, self._redraw_checked_row)
        self.target_checked_rows = CheckedRowController(
            self.target_items, self._redraw_target_checked_row)
        self.texplan_cache = {}  # folder -> texture plan rows (선택 시 지연 계산)
        self.texplan_errors = {}  # folder -> explicit execution blocker
        self.sync_state = {"entries": {}}
        self.sync_migration_worker = None
        self._sync_state_migrating = False
        self._sync_state_migration_failed = False
        self._initial_relation_finished = False
        self._initial_relation_ready = False
        self._pending_initial_sync_result = None
        self.worker = None
        self._busy = False
        self.shared_queue_runtime = SharedQueueRuntime(
            "pcg_st9_texture_batch"
        )
        self._initial_refreshing = False
        self._manual_refreshing = False
        self._target_refresh_active = False
        self._pending_refresh = False
        self._display_only_snapshot = False
        self._refresh_generation = 0
        self._refresh_cancel_event = threading.Event()
        self.focus_label = focus_data_asset_label(self.cfg.get("pcg_focus_data_assets"))
        root.title("PCG ST9 → SK 전환 준비 보드")
        root.geometry("1320x820")
        self._build_ui()
        self._set_busy(True)
        snapshot_view_started = time.perf_counter()
        snapshot_painted = self._load_initial_display_snapshot()
        snapshot_view_seconds = time.perf_counter() - snapshot_view_started
        self._had_initial_snapshot = bool(snapshot_painted)
        snapshot_state = getattr(self, "_initial_snapshot_state", "missing")
        self.startup_latency.mark(
            "cached_board_paint",
            status="painted" if snapshot_painted else snapshot_state,
            counts={
                "row_count": len((self.report or {}).get("items") or ()),
                "painted": bool(snapshot_painted),
            },
            details={
                "cache_state": snapshot_state,
                "view_apply_seconds": round(snapshot_view_seconds, 6),
            },
        )
        self.startup_latency.milestone(
            "cached_board_view_applied",
            status="painted" if snapshot_painted else snapshot_state,
            counts={
                "row_count": len((self.report or {}).get("items") or ()),
            },
            details={
                "view_apply_seconds": round(snapshot_view_seconds, 6),
            },
        )
        if not snapshot_painted:
            self._load_initial_folder_inventory()
        try:
            self.sync_state = load_sync_state(migrate=False)
        except Exception as exc:
            self.log(f"[Unreal 동기화 기록 경고] {exc}")
            self.sync_state = {"entries": {}}
        self._set_busy(False)
        self._update_step3_button()

    # ------------------------------------------------------------------ UI
    def _build_ui(self):
        intro = ttk.Label(
            self.root, padding=(8, 6, 8, 0), foreground="#444",
            text="PCG에서 쓰는 ST9 식생을 SK_(나나이트+논마스크+VT)로 바꾸는 준비 보드입니다. "
                 "행을 클릭하면 아래에 '다음에 뭘 하면 되는지'가 나옵니다. "
                 "①~③은 실행 전 대상·변경·백업 범위를 확인합니다.",
        )
        intro.pack(fill="x")

        top = ttk.Frame(self.root, padding=6)
        top.pack(fill="x")
        ttk.Label(top, text="식생 루트:").pack(side="left")
        self.root_var = tk.StringVar(value=self.cfg["tree_root"])
        self.root_entry = ttk.Entry(top, textvariable=self.root_var, width=62)
        self.root_entry.pack(side="left", padx=4)
        self.btn_pick_root = ttk.Button(
            top, text="...", width=3, command=self.pick_root
        )
        self.btn_pick_root.pack(side="left")
        self.btn_refresh = ttk.Button(
            top, text="🔍 다시 검사 (자산 수정 없음)", command=self.refresh
        )
        self.btn_refresh.pack(side="left", padx=6)
        Tooltip(self.btn_refresh, "아무것도 수정하지 않습니다.\n"
                                  "식생 폴더마다 SK SPM / M_ 이름 / 잎 메시 blend / 텍스처 6장 "
                                  "상태를 다시 읽어서 표를 갱신합니다.")
        self.btn_select_all = ttk.Button(
            top, text="전체 선택", command=lambda: self._set_all(True)
        )
        self.btn_select_all.pack(side="left")
        self.btn_clear_all = ttk.Button(
            top, text="전체 해제", command=lambda: self._set_all(False)
        )
        self.btn_clear_all.pack(side="left", padx=4)

        src = ttk.Frame(self.root, padding=(6, 0, 6, 3))
        src.pack(fill="x")
        ttk.Label(src, text=f"{self.focus_label} PCG + 배치 레벨 대상:").pack(side="left")
        self.btn_live_targets = ttk.Button(
            src, text="Unreal에서 읽기", command=self.refresh_pcg_targets
        )
        self.btn_live_targets.pack(side="left", padx=(6, 0))
        Tooltip(self.btn_live_targets, "언리얼 에디터가 켜져 있을 때 사용.\n"
                                       f"{self.focus_label} 활성 항목과 설정된 작업 레벨에 직접 배치된 ST9 메시를 읽습니다.\n"
                                       "현재 설정 레벨: /Game/Level/Cliff_final_01")
        self.btn_saved_targets = ttk.Button(
            src, text="저장된 리포트에서 읽기",
            command=self.import_saved_pcg_report,
        )
        self.btn_saved_targets.pack(side="left", padx=(6, 0))
        Tooltip(self.btn_saved_targets, "언리얼 에디터가 꺼져 있을 때 사용.\n"
                                        "이전에 저장해 둔 PCG 덤프 파일에서 메시 목록을 읽어 옵니다.")
        # The default inventory must include every Tree/Bush/Weed Cluster.
        # A saved PCG report is an optional focus filter, not authorization to
        # hide real source folders merely because the report exists.
        self.use_pcg_targets_var = tk.BooleanVar(
            value=DEFAULT_USE_PCG_TARGETS)
        self.chk_pcg_targets = ttk.Checkbutton(
            src, text=f"{self.focus_label}/Cliff 대상만 보기",
            variable=self.use_pcg_targets_var, command=self.refresh,
        )
        self.chk_pcg_targets.pack(side="left", padx=10)
        Tooltip(self.chk_pcg_targets, f"켜면: {self.focus_label}의 Weight>0 항목 또는 Cliff_final_01 직접 배치와\n"
                                      "매칭되는 식생 폴더만 표에 나옵니다.\n"
                                      "끄면(기본): 루트 아래 모든 Tree/Bush/Weed 폴더가 나옵니다.")
        self.targets_info_var = tk.StringVar(value="")
        ttk.Label(src, textvariable=self.targets_info_var, foreground="#666").pack(side="left", padx=8)

        legend = ttk.Frame(self.root, padding=(6, 0, 6, 3))
        legend.pack(fill="x")
        ttk.Label(legend, text="행 색:").pack(side="left")
        for text, tag in (
            (f"{self.focus_label} PCG", "target_pcg"),
            ("Cliff 직접 배치", "target_level"),
            ("PCG + 직접 배치", "target_both"),
        ):
            tk.Label(
                legend, text=f"  {text}  ",
                background=TARGET_ROW_COLORS[tag], relief="solid", borderwidth=1,
            ).pack(side="left", padx=(5, 0))

        actions = ttk.Frame(self.root, padding=6)
        actions.pack(fill="x")
        self.btn_prepare = ttk.Button(actions, text="① 식생 — SK 만들기 + M_ 이름 붙이기 (체크 행)",
                                      command=self.start_prepare)
        self.btn_prepare.pack(side="left")
        Tooltip(self.btn_prepare, "체크된 행 중 ①이 필요한 항목에만:\n"
                                  "· SK SPM이 없으면 → 원본 SPM을 복사해서 SK_이름.spm 생성\n"
                                  "· 머티리얼 이름 앞에 M_ 을 붙임 (send2ue 임포트 규칙 때문)\n"
                                  "수정 전 원본은 각 폴더의 _spm_backups\\ 에 백업됩니다.\n"
                                  "문제가 있는 항목(중복 매칭·원본 불명·기본 이름 머티리얼)은\n"
                                  "자동으로 건너뛰고 표와 로그에 이유를 표시합니다.\n"
                                  "Cluster는 최초 raw→SK 생성 후 canonical SK만 수정하고,\n"
                                  "완료된 SK를 원래 이름의 raw 출력 SPM으로 단방향 게시합니다.")
        self.force_var = tk.BooleanVar(value=False)
        self.chk_force = ttk.Checkbutton(
            actions, text="⚠ 문제 표시된 항목도 적용", variable=self.force_var
        )
        self.chk_force.pack(side="left", padx=10)
        Tooltip(self.chk_force, "기본은 끔(안전). 켜면:\n"
                                "· '중복 매칭' 경고가 있어도 그대로 적용\n"
                                "· 'Material 2' 같은 기본 이름도 M_Material 2 로 강제 변경\n"
                                "백업은 똑같이 남습니다.\n"
                                "원본 SPM을 아예 못 찾은 항목은 켜도 처리할 수 없습니다.")
        btn_open = ttk.Button(actions, text="선택 폴더 열기", command=self.open_selected_folder)
        btn_open.pack(side="left", padx=10)
        Tooltip(btn_open, "표에서 클릭한 행의 나무 폴더를 탐색기로 엽니다.")
        btn_copy = ttk.Button(
            actions, text="선택 경로 복사", command=self.copy_selected_paths
        )
        btn_copy.pack(side="left")
        Tooltip(
            btn_copy,
            "나무/식생 행은 SPM, Cluster는 폴더, Cluster 자식은 해당 SPM, "
            "blend 파일 행은 실제 .blend, 그 자식은 연결 대상 SPM을 복사합니다.",
        )
        self.status_var = tk.StringVar(value="대기")
        ttk.Label(actions, textvariable=self.status_var).pack(side="right")

        actions2 = ttk.Frame(self.root, padding=(6, 0, 6, 6))
        actions2.pack(fill="x")
        self.btn_step2 = ttk.Button(actions2, text="② 실행 — 잎 메시 blend 만들기 (선택 항목)",
                                    command=self.start_step2)
        self.btn_step2.pack(side="left")
        Tooltip(self.btn_step2, "체크된 행의 최종/Cluster SPM을 따라 원본 잎 아틀라스를 찾고\n"
                                "아틀라스 리프 제너레이터를 돌립니다 (묶음당 수십초~수분):\n"
                                "· Cluster 결과 TGA가 아니라 그 Cluster SPM 내부 원본을 사용\n"
                                "· 같은 원본 아틀라스를 여러 SPM이 쓰면 한 번만 생성\n"
                                "· 알베도+알파의 모든 알파 아일랜드를 잎 메시로 생성\n"
                                "· Quality=Low, Plate=One Plate 고정\n"
                                "· atlas 폴더에 M_이름.blend 저장 (기존 파일은 재생성하지 않음)\n"
                                "· 기존 blend는 수동 편집을 보존한 채 최종 SK 연결에 재사용\n"
                                "알베도/알파 원본을 못 찾은 묶음은 이유를 표시하고 건너뜁니다.\n"
                                "메시를 눈으로 확인/정리하려면 저장된 blend를 열면 됩니다.")
        self.spm_push_var = tk.BooleanVar(value=True)
        self.chk_spm_push = ttk.Checkbutton(
            actions2, text="최종 SK에 잎 메시 생성 + Generator 연결",
            variable=self.spm_push_var)
        self.chk_spm_push.pack(side="left", padx=8)
        Tooltip(self.chk_spm_push, "켜면 ② 직후 Material_v8과 FBX/XML Mesh 자산을 최종 SK SPM에 등록하고\n"
                                  "원본을 사용하던 Leaf Mesh/Frond Generator의 Material/Mesh 슬롯도 연결합니다.\n"
                                  "Cluster SPM은 원본 아틀라스 추적에만 쓰며 수정/개수 계산에서 제외합니다.\n"
                                  "기존 blend가 있으면 다시 만들지 않고 현재 메시 편집을 그대로 사용합니다.\n"
                                  "SPM은 먼저 백업하며 연결 검증에 실패하면 원본으로 복원합니다.")
        self.btn_step3 = ttk.Button(actions2, text="③ 실행 — 연결 텍스처 만들기 (선택 항목)",
                                    command=self.start_step3)
        self.btn_step3.pack(side="left", padx=10)
        Tooltip(self.btn_step3, "체크된 행의 표시된 SpeedTree Generator에 연결된 모든 텍스처 세트를 처리합니다.\n"
                                "각 세트는 color/normal/extra/height/opacity/subsurface 6장을 4K 이내로 완성합니다 (세트당 ~1분):\n"
                                "· 숨김 Generator와 숨김 부모 아래 항목은 생성 대상에서 제외\n"
                                "· *_yellow / *_green처럼 같은 원본을 공유하는 파생 슬롯은 한 세트로 통합\n"
                                "· 아틀라스뿐 아니라 bark/stem/surface 등 표시 Generator의 연결 텍스처도 모두 대상\n"
                                 "· 생성 후 /Game/Textures의 같은 T_ 이름으로 Unreal에 자동 동기화\n"
                                 "· 내용 MD5가 같으면 checkout/import/save하지 않고, 신규만 Perforce add\n"
                                 "· 모두 최신이면 같은 버튼이 'Unreal 전체 재확인'으로 바뀌어 receipt 캐시를 무시한 검증 경로를 제공\n"
                                 "· 머티리얼 M_ 이름은 유지하고 출력 텍스처만 T_ 이름으로 저장\n"
                                "· SBS의 T_ 그래프를 렌더하고, 기존 M_ 그래프는 T_로 안전하게 이름 변경\n"
                                "· 없으면: 원본 텍스처를 자동 매핑해 렌더하고, 나중에 Designer에서\n"
                                "  관리할 수 있게 T_ 그래프를 SBS에 새로 넣습니다 (수정 전 백업 저장)\n"
                                "AO 없으면 height에서 Designer HBAO 생성, SDF=0, 노멀 방식은 원본 출처로 자동 판정.\n"
                                "기존 T_ 6장은 렌더 전에 _pcgtex_backups\\ 에 백업하고,\n"
                                "새 T_ 렌더가 성공하면 대응하는 기존 M_ 출력은 삭제합니다.")
        self.btn_step3_force = ttk.Button(
            actions2,
            text="③ 전체 다시 뽑기 (선택 항목)",
            command=self.start_step3_force,
        )
        self.btn_step3_force.pack(side="left", padx=4)
        Tooltip(
            self.btn_step3_force,
            "Cluster_System_01.sbsar를 수정한 뒤 수동으로 사용하는 전체 재추출입니다.\n"
            "체크 여부와 무관하게 현재 표의 모든 세트를 세트당 T_ 6장씩 다시 렌더합니다.\n"
            "절차형 SBS 그래프도 기존 쿡 캐시를 재사용하지 않고 현재 Cluster_System을 다시 쿡합니다.\n"
            "모든 렌더가 성공하면 결과 전체를 모아 Unreal 동기화를 한 번만 실행합니다.\n"
            "성공한 세트는 루트 SK와 canonical Cluster SK까지 SPM 연결을 정규화합니다.\n"
            "기존 출력은 일반 ③ 실행과 같이 안전하게 백업합니다.",
        )
        registry_actions = ttk.Frame(self.root, padding=(6, 0, 6, 6))
        registry_actions.pack(fill="x")
        ttk.Label(registry_actions, text="④ Atlas 대상 JSON:").pack(side="left")
        self.btn_add_target = ttk.Button(
            registry_actions,
            text="선택 blend에 SPM 추가",
            command=self.add_blend_target_spm,
        )
        self.btn_add_target.pack(side="left", padx=(6, 4))
        Tooltip(
            self.btn_add_target,
            "표의 ◆ blend 행이나 그 자식 SPM을 선택한 뒤 대상 .spm을 추가합니다.\n"
            "목록은 blend 옆 .atlas_leaf_targets.json에 저장되어 Blender 애드온과 공유됩니다.",
        )
        self.btn_remove_target = ttk.Button(
            registry_actions,
            text="선택 SPM 제거",
            command=self.remove_blend_target_spms,
        )
        self.btn_remove_target.pack(side="left")
        Tooltip(
            self.btn_remove_target,
            "◆ blend 아래에서 선택한 SPM만 대상 JSON 목록에서 제거합니다.\n"
            "원래 Generator 슬롯을 복원하고 해당 Atlas scope 자산을 SPM에서 정리한 뒤 목록에서 제거합니다.\n"
            "SPM은 먼저 _spm_backups에 백업하며 실제 .spm 파일 자체는 삭제하지 않습니다.",
        )
        cols = ("pcg", "step1", "step2", "step3", "step4", "next")
        self.tree = ttk.Treeview(self.root, columns=cols, show="tree headings", height=16)
        self.tree.heading("#0", text="식생 폴더 / SPM (첫 클릭=이 행만 활성 · Ctrl+C=선택 경로)")
        self.tree.column("#0", width=285, anchor="w")
        headers = {
            "pcg": ("PCG/레벨 사용", 120),
            "step1": ("① SK_ + M_ 정규화", 250),
            "step2": ("② 잎 메시 (Blender)", 140),
            "step3": ("③ 텍스처 (Substance)", 160),
            "step4": ("④ Blend ↔ SPM 확인", 190),
            "next": ("다음 할 일", 220),
        }
        for key, (label, width) in headers.items():
            self.tree.heading(key, text=label)
            self.tree.column(key, width=width, anchor="w")
        for tag, color in TARGET_ROW_COLORS.items():
            self.tree.tag_configure(tag, background=color)
        self.tree.tag_configure("target_spm_ready", background="#f4faf4")
        self.tree.tag_configure(
            "target_spm_attention", background="#fff7d6", foreground="#6f4b00"
        )
        self.tree.tag_configure(
            "blender_file", background="#e9eff5", font=("Segoe UI", 9, "bold")
        )
        self.tree.tag_configure("blender_spm", background="#f7f9fb")
        self.tree.tag_configure(
            "blender_attention", background="#fff0c2", foreground="#7a4700"
        )
        self.tree.tag_configure("blender_inactive", foreground="#777777")
        self.tree.pack(fill="both", expand=True, padx=6, pady=(0, 2))
        self.tree.bind("<Button-1>", self._on_click)
        self.tree.bind("<Control-c>", self.copy_selected_paths, add="+")
        self.tree.bind("<<TreeviewSelect>>", lambda _e: self.show_details())
        header_tip = ("컬럼 = 작업 순서입니다.\n"
                      "PCG/레벨 사용: 각 위치에서 사용하는 메시 이름 개수\n"
                      "①: SK SPM 존재 + 머티리얼 M_ 이름 (이 도구가 자동 처리)\n"
                      "②: 잎 지오메트리 blend — 헤드리스 Blender 자동 생성\n"
                      "③: 사용 머티리얼별 T_ 텍스처 6장 — sbsrender 자동 생성\n"
                      "④: 실제 blend 파일과 연결 대상 SPM 감사 — 펼쳐서 상세 확인")
        Tooltip(self.tree, header_tip, wrap=460)

        details = ttk.LabelFrame(self.root, text="선택한 폴더의 할 일 (위 표에서 행을 클릭)", padding=4)
        details.pack(fill="both", padx=6, pady=(0, 4))
        det_wrap = ttk.Frame(details)
        det_wrap.pack(fill="both", expand=True)
        self.text = tk.Text(det_wrap, height=14, wrap="none", state="disabled",
                            font=("Malgun Gothic", 9))
        det_scroll = ttk.Scrollbar(det_wrap, orient="vertical", command=self.text.yview)
        self.text.configure(yscrollcommand=det_scroll.set)
        det_scroll.pack(side="right", fill="y")
        self.text.pack(side="left", fill="both", expand=True)

        logf = ttk.LabelFrame(self.root, text="로그", padding=4)
        logf.pack(fill="x", padx=6, pady=(0, 6))
        self.log_text = tk.Text(logf, height=6, wrap="none", state="disabled")
        self.log_text.pack(fill="both", expand=True)

    def log(self, msg):
        stamp = datetime.now().strftime("%H:%M:%S")
        self.log_text.configure(state="normal")
        self.log_text.insert("end", f"[{stamp}] {msg}\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def pick_root(self):
        path = filedialog.askdirectory(initialdir=self.root_var.get())
        if path:
            self.root_var.set(path)

    def _set_all(self, checked):
        if getattr(self, "_busy", False):
            return
        self.checked_rows.set_all(checked)
        target_rows = getattr(self, "target_checked_rows", None)
        if target_rows is not None:
            target_rows.set_all(checked)
        self._sync_folder_checks_from_targets()
        self._update_step3_button()

    def _redraw_checked_row(self, iid, entry):
        mark = CHECK_ON if entry["checked"] else CHECK_OFF
        targets = [
            row for row in getattr(self, "target_items", {}).values()
            if row.get("folder_iid") == iid
        ]
        suffix = ""
        if targets:
            selected = sum(bool(row.get("checked")) for row in targets)
            suffix = f" · SPM {selected}/{len(targets)}"
        self.tree.item(iid, text=f"{mark} {entry['item']['name']}{suffix}")

    def _redraw_target_checked_row(self, iid, entry):
        mark = CHECK_ON if entry["checked"] else CHECK_OFF
        self.tree.item(iid, text=f"{mark} {entry['display_name']}")

    def _sync_target_checks_from_folders(self):
        for iid, entry in getattr(self, "target_items", {}).items():
            parent = self.items.get(entry["folder_iid"])
            entry["checked"] = bool(parent and parent.get("checked"))
            self._redraw_target_checked_row(iid, entry)
        controller = getattr(self, "target_checked_rows", None)
        if controller is not None:
            controller.sync_after_reload()
        for iid, entry in self.items.items():
            self._redraw_checked_row(iid, entry)

    def _sync_folder_checks_from_targets(self):
        target_items = getattr(self, "target_items", {})
        if not target_items:
            return
        for iid, entry in self.items.items():
            children = [
                row for row in target_items.values()
                if row.get("folder_iid") == iid
            ]
            if children:
                entry["checked"] = any(row.get("checked") for row in children)
            self._redraw_checked_row(iid, entry)
        self.checked_rows.armed = False

    def _on_click(self, event):
        if getattr(self, "_busy", False):
            # Freeze checked execution targets, but keep read-only row
            # selection available for details and path copying.
            iid = self.tree.identify_row(event.y)
            if iid in self.row_copy_paths:
                self.tree.selection_set(iid)
                self.tree.focus(iid)
                self.tree.focus_set()
            return "break"
        if self.tree.identify_region(event.x, event.y) != "tree":
            return
        iid = self.tree.identify_row(event.y)
        if iid in getattr(self, "target_items", {}):
            self.tree.selection_set(iid)
            self.tree.focus(iid)
            self.tree.focus_set()
            self.target_checked_rows.click(iid)
            self._sync_folder_checks_from_targets()
            self._update_step3_button()
            return "break"
        if iid not in self.items and iid in self.row_copy_paths:
            identify_element = getattr(self.tree, "identify_element", None)
            if identify_element and identify_element(event.x, event.y) == "Treeitem.indicator":
                return
            # Cluster/Blender helper rows are selectable and copyable but do
            # not inherit stale checked parent work.  A Cluster SPM child is
            # still consumed explicitly by start_prepare() from selection.
            self.tree.selection_set(iid)
            self.tree.focus(iid)
            self.tree.focus_set()
            self.checked_rows.set_all(False)
            target_rows = getattr(self, "target_checked_rows", None)
            if target_rows is not None:
                target_rows.set_all(False)
                self._sync_folder_checks_from_targets()
            self._update_step3_button()
            return "break"
        self.checked_rows.click(iid)
        self._sync_target_checks_from_folders()
        self._update_step3_button()

    def copy_selected_paths(self, _event=None):
        count = copy_selected_row_paths(
            self.root,
            self.tree,
            self.row_copy_paths,
            lambda paths: paths,
        )
        if count:
            self.status_var.set(f"경로 복사 완료 · {count}개")
        else:
            self.status_var.set("복사할 경로가 있는 행을 먼저 클릭하세요")
        return "break"

    def _selected_blend_connection_rows(self):
        return [
            self.blend_connection_meta[iid]
            for iid in self.tree.selection()
            if iid in self.blend_connection_meta
        ]

    def _current_targets_for_blend(self, blend):
        registry = load_target_registry(blend)
        if registry is not None:
            return [Path(value) for value in registry["target_spms"]]
        targets = []
        seen = set()
        blend_key = os.path.normcase(str(Path(blend).absolute())).casefold()
        for meta in self.blend_connection_meta.values():
            if meta.get("spm") is None:
                continue
            if os.path.normcase(str(Path(meta["blend"]).absolute())).casefold() != blend_key:
                continue
            spm = Path(meta["spm"]).absolute()
            key = os.path.normcase(str(spm)).casefold()
            if key not in seen:
                seen.add(key)
                targets.append(spm)
        return targets

    def add_blend_target_spm(self):
        rows = self._selected_blend_connection_rows()
        evidence_items = [row.get("item") for row in rows if row.get("item")]
        if rows and not self._require_live_mutation_items(evidence_items):
            return
        if any(row.get("managed_by") == "spm_generator_sync" for row in rows):
            messagebox.showinfo(
                "Cluster 관계",
                "SpeedTree Cluster Normalizer blend의 ON/OFF와 실제 메시 동기화는 "
                "SPM Generator Sync에서 관리합니다.",
                parent=self.root,
            )
            return
        blends = {
            os.path.normcase(str(Path(row["blend"]).absolute())).casefold():
                Path(row["blend"]).absolute()
            for row in rows
        }
        if len(blends) != 1:
            messagebox.showinfo(
                "대상 SPM 추가",
                "④ 아래의 ◆ blend 행 또는 그 자식 SPM 한 묶음을 선택하세요.",
                parent=self.root,
            )
            return
        blend = next(iter(blends.values()))
        selected = filedialog.askopenfilename(
            title=f"{blend.name} 대상 SPM 추가",
            initialdir=str(blend.parent),
            filetypes=(("SpeedTree SPM", "*.spm"), ("모든 파일", "*.*")),
            parent=self.root,
        )
        if not selected:
            return
        selected_path = Path(selected).absolute()
        try:
            shared = self._enqueue_shared_execution(
                f"Atlas 대상 추가 · {blend.name}",
                {
                    "operation": "atlas_target_add",
                    "blend": str(blend),
                    "target_spm": str(selected_path),
                },
            )
        except Exception as exc:
            self._shared_execution_enqueue_failed(exc)
            return
        self._set_busy(True)
        self.status_var.set(f"Atlas 대상 추가 대기 · {selected_path.name}")
        self.worker = threading.Thread(
            target=self._run_shared_execution,
            args=(
                shared,
                f"Atlas 대상 추가 · {blend.name}",
                self._run_add_blend_target_spm,
                (blend, selected_path, evidence_items),
            ),
            daemon=True,
        )
        self.worker.start()

    def _run_add_blend_target_spm(
        self, blend, selected_path, evidence_items=None
    ):
        if not Path(selected_path).is_file():
            raise RuntimeError(
                "selected Atlas target SPM is not a current file; "
                "production registry was not written"
            )
        plan_payload = {
            "blend": str(blend),
            "target_spm": str(selected_path),
            "target_registry": str(registry_path_for_blend(blend)),
            "capture_manifest": str(_relation_capture_manifest_path(blend)),
        }
        unit_id = "atlas-target-add"
        evidence_item_keys = [
            _exact_mutation_item_key(item)
            for item in evidence_items or ()
            if isinstance(item, dict)
        ]
        baseline = self._reaudit_and_seal_mutation_items(
            evidence_items or (),
            action="atlas_target_add",
            plan_payload=plan_payload,
            authority_units=[{
                "unit_id": unit_id,
                "payload": plan_payload,
                "item_keys": evidence_item_keys,
                "write_item_keys": evidence_item_keys,
                "paths": [
                    blend,
                    selected_path,
                    registry_path_for_blend(blend),
                    _relation_capture_manifest_path(blend),
                ],
                "write_paths": [registry_path_for_blend(blend)],
                "memberships": [
                    Path(selected_path).parent
                    / ".atlas_leaf_speedtree_scopes",
                ],
            }],
            tool_paths=[mutation_source_path(save_target_registry)],
        )
        require_exact_mutation_baseline(
            baseline,
            unit_id=unit_id,
            current_unit_payload=plan_payload,
        )
        targets = self._current_targets_for_blend(blend)
        keys = {
            os.path.normcase(str(path.absolute())).casefold()
            for path in targets
        }
        selected_key = os.path.normcase(
            str(selected_path.absolute())
        ).casefold()
        if selected_key not in keys:
            targets.append(selected_path)
        require_exact_mutation_baseline(
            baseline,
            unit_id=unit_id,
            current_unit_payload=plan_payload,
        )
        payload = save_target_registry(
            blend,
            targets,
            expected_registry_state=mutation_authority_expected_path_state(
                baseline["unit_authority"],
                unit_id,
                registry_path_for_blend(blend),
            ),
        )
        complete_exact_mutation_unit(
            baseline,
            unit_id,
            post_paths=[registry_path_for_blend(blend)],
        )
        self._ui(
            lambda source=blend, target=selected_path, result=payload:
            self._add_blend_target_done(source, target, result)
        )
        return {
            "shared_queue_success": True,
            "shared_queue_result": {
                "operation": "atlas_target_add",
                "blend": str(blend),
                "target_spm": str(selected_path),
            },
        }

    def _add_blend_target_done(self, blend, selected_path, payload):
        self.worker = None
        self._set_busy(False)
        self.populate()
        self.status_var.set(
            f"Atlas 대상 추가 · {selected_path.name} · "
            f"총 {len(payload['target_spms'])}개"
        )
        self.log(
            f"Atlas 대상 JSON 추가: {blend.name} → {selected_path}"
        )

    def remove_blend_target_spms(self):
        rows = [row for row in self._selected_blend_connection_rows() if row.get("spm")]
        evidence_items = [row.get("item") for row in rows if row.get("item")]
        if rows and not self._require_live_mutation_items(evidence_items):
            return
        if any(row.get("managed_by") == "spm_generator_sync" for row in rows):
            messagebox.showinfo(
                "Cluster 관계",
                "SpeedTree Cluster Normalizer blend의 ON/OFF와 원본 메시 복원은 "
                "SPM Generator Sync에서 관리합니다.",
                parent=self.root,
            )
            return
        blends = {
            os.path.normcase(str(Path(row["blend"]).absolute())).casefold():
                Path(row["blend"]).absolute()
            for row in rows
        }
        if not rows or len(blends) != 1:
            messagebox.showinfo(
                "대상 SPM 제거",
                "④에서 ◆ blend 아래의 제거할 SPM 행을 선택하세요.",
                parent=self.root,
            )
            return
        blend = next(iter(blends.values()))
        remove_paths = [Path(row["spm"]).absolute() for row in rows]
        preview = "\n".join(f"• {path}" for path in remove_paths)
        if not messagebox.askyesno(
            "대상 SPM 제거",
            f"{blend.name}의 대상 JSON 목록에서 다음 {len(remove_paths)}개를 제거합니다.\n"
            "실제 SPM 파일은 삭제하지 않습니다.\n\n"
            f"{preview}",
            parent=self.root,
        ):
            return
        if getattr(self, "_busy", False):
            return
        try:
            shared = self._enqueue_shared_execution(
                f"Atlas 대상 해제 · {blend.name}",
                {
                    "operation": "atlas_target_remove",
                    "blend": str(blend),
                    "target_spms": [str(path) for path in remove_paths],
                },
            )
        except Exception as exc:
            self._shared_execution_enqueue_failed(exc)
            return
        self._set_busy(True)
        self.status_var.set(f"Atlas 대상 해제 중 · {len(remove_paths)}개")
        self.worker = threading.Thread(
            target=self._run_shared_execution,
            args=(
                shared,
                f"Atlas 대상 해제 · {blend.name}",
                self._run_remove_blend_target_spms,
                (blend, remove_paths, evidence_items),
            ),
            daemon=True,
        )
        self.worker.start()

    def handle_delete_key(self):
        """Use Delete as target unlink for Atlas SPM child rows."""
        rows = [
            row for row in self._selected_blend_connection_rows()
            if row.get("spm")
        ]
        if not rows:
            return False
        self.remove_blend_target_spms()
        return True

    def _run_remove_blend_target_spms(
        self, blend, remove_paths, evidence_items=None
    ):
        plan_payload = {
            "blend": str(blend),
            "target_spms": [str(path) for path in remove_paths],
            "target_registry": str(registry_path_for_blend(blend)),
            "capture_manifest": str(_relation_capture_manifest_path(blend)),
        }
        unit_id = "atlas-target-remove"
        blender_exe = self.cfg.get("blender_exe", "")
        startup = blender_user_startup_path(blender_exe)
        config_projection = mutation_config_projection(
            self.cfg, ("blender_exe", "atlas_job_timeout")
        )
        evidence_item_keys = [
            _exact_mutation_item_key(item)
            for item in evidence_items or ()
            if isinstance(item, dict)
        ]
        scope_memberships = [
            Path(path).parent / ".atlas_leaf_speedtree_scopes"
            for path in remove_paths
        ]
        baseline = self._reaudit_and_seal_mutation_items(
            evidence_items or (),
            action="atlas_target_remove",
            plan_payload=plan_payload,
            authority_units=[{
                "unit_id": unit_id,
                "payload": plan_payload,
                "item_keys": evidence_item_keys,
                "write_item_keys": evidence_item_keys,
                "paths": [
                    blend,
                    *remove_paths,
                    registry_path_for_blend(blend),
                    _relation_capture_manifest_path(blend),
                ],
                "write_paths": [
                    registry_path_for_blend(blend),
                    *remove_paths,
                ],
                "memberships": scope_memberships,
                "write_memberships": scope_memberships,
            }],
            config_projection=config_projection,
            tool_paths=[
                blender_exe,
                startup,
                TOOL_DIR / "jobs" / "atlas_target_remove_job.py",
                mutation_source_path(save_target_registry),
            ],
        )
        require_exact_mutation_baseline(
            baseline,
            unit_id=unit_id,
            current_unit_payload=plan_payload,
            current_config=mutation_config_projection(
                self.cfg, ("blender_exe", "atlas_job_timeout")
            ),
        )
        REPORT_DIR.mkdir(parents=True, exist_ok=True)
        receipt_stem = (
            f"atlas_target_remove_{os.getpid()}_{threading.get_ident()}"
        )
        report_path = REPORT_DIR / f"{receipt_stem}.json"
        child_authority_path = REPORT_DIR / f"{receipt_stem}.authority.json"
        child_authority_sha256 = write_child_mutation_authority(
            baseline["unit_authority"],
            unit_id,
            child_authority_path,
        )
        command = atlas_blender_command(self.cfg.get("blender_exe", "")) + [
            "--python", str(TOOL_DIR / "jobs" / "atlas_target_remove_job.py"), "--",
            "--blend", str(blend),
            "--report", str(report_path),
            "--authority-json", str(child_authority_path),
            "--authority-sha256", child_authority_sha256,
        ]
        for path in remove_paths:
            command.extend(["--spm", str(path)])
        data = None
        error = None
        try:
            result = owned_run(
                command,
                source="pcg_st9_texture_batch.pcg_texture_gui.remove_atlas_targets",
                run_factory=subprocess.run,
                capture_output=True,
                text=True,
                timeout=self.cfg.get("atlas_job_timeout", 1800),
                creationflags=0x08000000,
            )
            if report_path.is_file():
                data = json.loads(report_path.read_text(encoding="utf-8"))
            if result.returncode != 0 or not data or data.get("status") != "ok":
                detail = (data or {}).get("error") or (result.stderr or result.stdout)[-1200:]
                raise RuntimeError(detail or "Atlas 대상 해제 작업이 실패했습니다")
            complete_exact_mutation_unit(
                baseline,
                unit_id,
                post_paths=[
                    registry_path_for_blend(blend),
                    *remove_paths,
                ],
            )
        except Exception as exc:
            error = exc
        self._ui(
            lambda result=data, failure=error, source=blend, paths=remove_paths:
                self._remove_blend_targets_done(source, paths, result, failure)
        )
        return {
            "shared_queue_success": error is None,
            "shared_queue_result": {
                "operation": "atlas_target_remove",
                "blend": str(blend),
                "target_count": len(remove_paths),
                "report_path": str(report_path),
                "authority_path": str(child_authority_path),
                "error": str(error) if error is not None else None,
            },
        }

    def _remove_blend_targets_done(self, blend, remove_paths, data, error):
        self.worker = None
        self._set_busy(False)
        if error is not None:
            messagebox.showerror("대상 SPM 해제 실패", str(error), parent=self.root)
            self.status_var.set("Atlas 대상 해제 실패 · JSON/SPM 복원됨")
            self.log(f"Atlas 대상 해제 실패: {blend.name} · {error}")
            return
        self.populate()
        remaining = len((data or {}).get("remaining_target_spms") or [])
        cleaned = sum(
            row.get("status") in {"cleaned", "already_clean"}
            for row in (data or {}).get("results") or []
        )
        self.status_var.set(
            f"Atlas 대상 해제 완료 · {len(remove_paths)}개 · SPM 정리 {cleaned}개 · 남음 {remaining}개"
        )
        self.log(
            f"Atlas 대상 해제: {blend.name} → "
            + ", ".join(path.name for path in remove_paths)
        )

    # ------------------------------------------------------------------ scan
    def _begin_refresh_generation(self):
        previous = getattr(self, "_refresh_cancel_event", None)
        if previous is not None:
            previous.set()
        self._refresh_generation = getattr(self, "_refresh_generation", 0) + 1
        self._refresh_cancel_event = threading.Event()
        return self._refresh_generation, self._refresh_cancel_event

    def _refresh_generation_is_current(self, generation, cancel_event):
        return (
            generation == getattr(self, "_refresh_generation", None)
            and cancel_event is getattr(self, "_refresh_cancel_event", None)
            and not cancel_event.is_set()
        )

    def cancel_refresh(self):
        event = getattr(self, "_refresh_cancel_event", None)
        if event is not None:
            event.set()

    def _initial_pcg_targets(self):
        if not bool(self.use_pcg_targets_var.get()):
            return None
        return load_pcg_targets()

    def _load_initial_display_snapshot(self):
        """Paint the previous board immediately, never as execution evidence."""
        self._initial_snapshot_state = "missing"
        try:
            pcg_targets = self._initial_pcg_targets()
            snapshot = read_board_display_snapshot(
                self.cfg,
                pcg_targets=pcg_targets,
            )
        except Exception as exc:
            self._initial_snapshot_state = "error"
            self.log(f"[표시 캐시 경고] 이전 표를 읽지 못함: {exc}")
            return False
        self._initial_snapshot_state = str(
            snapshot.get("cache_state") or "unknown"
        )
        if not snapshot.get("can_display"):
            return False
        report = snapshot.get("display_report")
        if not isinstance(report, dict):
            return False
        mark_blender_connection_rows_pending(report)
        self.report = report
        self._display_only_snapshot = True
        self.populate()
        self._update_summary()
        stale = snapshot.get("cache_state") != "matching_inputs"
        suffix = " · 입력 변경 감지" if stale else ""
        self.status_var.set(
            "저장된 표 즉시 표시"
            f"{suffix} · ①② 변경 작업 잠김 · "
            "③ 실행 시 선택 범위만 최신화"
        )
        return True

    def _load_initial_folder_inventory(self):
        """Open a cold board from cheap folder discovery only.

        Detailed SPM/SBS/Blender inspection is intentionally user-triggered.
        """
        try:
            folders = candidate_folders(
                self.cfg,
                pcg_targets=self._initial_pcg_targets(),
            )
        except Exception as exc:
            self.log(f"[초기 폴더 목록 경고] {exc}")
            folders = []
        items = [self._initial_inventory_item(folder) for folder in folders]
        self.report = {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "pcg_targets": {"mesh_count": 0},
            "summary": {
                "total": len(items),
                "by_status": {"startup_pending": len(items)},
            },
            "items": items,
            "inventory_pending": bool(items),
            "partial_live_audit": True,
        }
        self._display_only_snapshot = True
        self.populate()
        self._update_summary()
        self.status_var.set(
            f"생산 폴더 {len(items)}개 즉시 표시 · "
            "상세 검사는 검사 버튼 또는 선택 실행 시 수행"
        )

    @staticmethod
    def _initial_inventory_item(folder):
        """Return a display-only row for a discovered production folder."""
        folder = Path(folder)
        return {
            "folder": str(folder),
            "name": folder.name,
            "status": "startup_pending",
            "actions": ["선택 실행 시 상세 검사"],
            "source_spms": [],
            "sk_spms": [],
            "cluster_items": [],
            "target_spm_statuses": [],
            "target_mesh_names": [],
            "pcg_target_mesh_names": [],
            "pcg_target_meshes": [],
            "pcg_mesh_names": [],
            "pcg_data_assets": [],
            "level_mesh_names": [],
            "level_placements": [],
            "leaf_mesh_sources": [],
            "leaf_atlas_inventory": [],
            "cluster_source_rows": [],
            "cluster_assembly": {},
            "_gui_blender_connection_pending": True,
            "audit_complete": False,
        }

    def _initial_partial_live_item(
        self,
        item,
        *,
        generation,
        cancel_event,
    ):
        """Paint the first cold live row while the remaining fleet continues."""
        if not self._refresh_generation_is_current(generation, cancel_event):
            return
        if getattr(self, "_had_initial_snapshot", False):
            return
        item.setdefault("target_spm_statuses", [])
        item.setdefault("target_mesh_names", [])
        item.setdefault("pcg_target_mesh_names", [])
        item.setdefault("pcg_mesh_names", [])
        item.setdefault("pcg_data_assets", [])
        item.setdefault("level_mesh_names", [])
        item.setdefault("level_placements", [])
        item.setdefault("pcg_target_meshes", [])
        mark_blender_connection_rows_pending({"items": [item]})
        self.report = {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "pcg_targets": {"mesh_count": 0},
            "summary": {
                "total": 1,
                "by_status": {item.get("status", "unknown"): 1},
            },
            "items": [item],
            "partial_live_audit": True,
        }
        self._display_only_snapshot = True
        self.populate()
        self._update_summary()
        self._set_busy(False)
        self._lock_mutation_controls()
        self.status_var.set(
            "첫 live 행 표시 · 나머지 폴더 검사 중… · 변경 작업 잠김"
        )

    def _start_initial_refresh(self):
        """Run only the first read-only audit off the Tk main thread."""
        generation, cancel_event = self._begin_refresh_generation()
        self._initial_refreshing = True
        self._initial_relation_finished = False
        self._initial_relation_ready = False
        self._pending_initial_sync_result = None
        self._set_busy(True)
        self.status_var.set("초기 검사 중...")
        self.cfg["tree_root"] = self.root_var.get()
        save_config(self.cfg)
        cfg = dict(self.cfg)
        use_pcg_targets = bool(self.use_pcg_targets_var.get())
        partial_lock = threading.Lock()
        partial_sent = False
        session_evidence = {}

        def worker():
            nonlocal partial_sent
            report = None
            error = None
            pcg_targets = None
            try:
                pcg_targets = load_pcg_targets() if use_pcg_targets else None

                def progress(completed, total, folder):
                    if not self._refresh_generation_is_current(
                        generation, cancel_event
                    ):
                        return
                    message = (
                        f"초기 검사 중... {completed}/{total} "
                        f"({Path(folder).name})"
                    )
                    self.root.after(
                        0,
                        lambda value=message: self.status_var.set(value),
                    )

                def first_live_item(item, _folder):
                    nonlocal partial_sent
                    if not self._refresh_generation_is_current(
                        generation, cancel_event
                    ):
                        return
                    if getattr(self, "_had_initial_snapshot", False):
                        return
                    with partial_lock:
                        if partial_sent:
                            return
                        partial_sent = True
                    display_item = copy.deepcopy(item)
                    self.root.after(
                        0,
                        lambda value=display_item:
                            self._initial_partial_live_item(
                                value,
                                generation=generation,
                                cancel_event=cancel_event,
                            ),
                    )

                report = make_report(
                    cfg,
                    pcg_targets=pcg_targets,
                    progress_callback=progress,
                    item_callback=first_live_item,
                    cancel_check=cancel_event.is_set,
                    session_evidence=session_evidence,
                )
            except Exception as exc:
                error = exc
            if not self._refresh_generation_is_current(
                generation, cancel_event
            ):
                return
            self.root.after(
                0,
                lambda result=report, targets=pcg_targets, failure=error,
                refresh_generation=generation,
                refresh_cancel=cancel_event:
                    self._initial_primary_refresh_done(
                        result,
                        cfg,
                        targets,
                        failure,
                        generation=refresh_generation,
                        cancel_event=refresh_cancel,
                        session_evidence=session_evidence,
                    ),
            )

        self.worker = threading.Thread(target=worker, daemon=True)
        self.worker.start()

    def _initial_primary_refresh_done(
        self,
        report,
        cfg,
        pcg_targets,
        error=None,
        *,
        generation=None,
        cancel_event=None,
        session_evidence=None,
    ):
        """Paint live primary columns before resolving Blender relations."""
        if generation is not None and not self._refresh_generation_is_current(
            generation, cancel_event
        ):
            return
        if error is not None:
            self._initial_refresh_done(
                report,
                error,
                stage="primary",
                generation=generation,
                cancel_event=cancel_event,
            )
            return
        mark_blender_connection_rows_pending(report)
        tracker = getattr(self, "startup_latency", None)
        if tracker is not None:
            audit_timing = report.get("startup_timing") or {}
            tracker.milestone(
                "primary_compute_complete",
                counts={
                    "folder_count": len(report.get("items") or ()),
                },
                details={
                    "audit_wall_seconds": audit_timing.get("wall_seconds"),
                    "cache_state": audit_timing.get("cache_state"),
                    "session_cache_metrics": audit_timing.get(
                        "session_cache_metrics"
                    ) or {},
                },
            )
        primary_view_started = time.perf_counter()
        self.report = report
        self._display_only_snapshot = False
        self.texplan_cache.clear()
        if not hasattr(self, "texplan_errors"):
            self.texplan_errors = {}
        self.texplan_errors.clear()
        self.populate()
        self._update_summary()
        tracker = getattr(self, "startup_latency", None)
        if tracker is not None:
            audit_timing = report.get("startup_timing") or {}
            tracker.milestone(
                "primary_view_applied",
                counts={
                    "folder_count": len(report.get("items") or ()),
                },
                details={
                    "view_apply_seconds": round(
                        time.perf_counter() - primary_view_started, 6
                    ),
                },
            )
            tracker.mark(
                "primary_live_audit",
                counts={
                    "folder_count": len(report.get("items") or ()),
                    "blend_index_request_count": audit_timing.get(
                        "blend_index_request_count", 0
                    ),
                    "revalidated_folder_count": audit_timing.get(
                        "revalidated_folder_count", 0
                    ),
                },
                details={
                    "audit_wall_seconds": audit_timing.get("wall_seconds"),
                    "cache_state": audit_timing.get("cache_state"),
                    "phases": audit_timing.get("phases") or [],
                },
            )
        # The primary board is safe for search/review now.  Mutation controls
        # remain locked until per-row relation/live identities are complete.
        self._set_busy(False)
        self._lock_mutation_controls()
        self.status_var.set(
            "기본 live 검사 완료 · Blender 연결 계산 중… · 변경 작업 잠김"
        )

        def relation_worker():
            failure = None
            relation_metrics = {}
            persistence_receipt = {
                "cache": {"status": "not_attempted"},
                "projection": {"status": "not_attempted"},
            }
            publication_allowed = lambda: self._refresh_generation_is_current(
                generation, cancel_event
            )

            # These are non-authoritative memoization/display artifacts. Save
            # them before relation work so a safe changed-during-scan failure
            # does not throw away minutes of completed primary computation.
            persist_started = time.perf_counter()
            try:
                written = save_spm_analysis_cache(
                    publish_check=publication_allowed,
                )
                persistence_receipt["cache"] = {
                    "status": (
                        "written" if written else
                        "canceled" if not publication_allowed() else
                        "unchanged"
                    ),
                    "file_count": len(written or ()),
                    "wall_seconds": round(
                        time.perf_counter() - persist_started, 6
                    ),
                }
            except Exception as exc:
                persistence_receipt["cache"] = {
                    "status": "warning",
                    "error": f"{type(exc).__name__}: {exc}",
                    "wall_seconds": round(
                        time.perf_counter() - persist_started, 6
                    ),
                }

            projection_metrics = {}
            projection_started = time.perf_counter()
            try:
                snapshot_path = write_board_display_snapshot(
                    report,
                    cfg,
                    pcg_targets=pcg_targets,
                    metrics=projection_metrics,
                    publish_check=publication_allowed,
                )
                persistence_receipt["projection"] = {
                    **projection_metrics,
                    "status": (
                        "written" if snapshot_path else
                        projection_metrics.get("reason", "not_written")
                    ),
                    "wall_seconds": round(
                        time.perf_counter() - projection_started, 6
                    ),
                }
            except Exception as exc:
                persistence_receipt["projection"] = {
                    **projection_metrics,
                    "status": "warning",
                    "error": f"{type(exc).__name__}: {exc}",
                    "wall_seconds": round(
                        time.perf_counter() - projection_started, 6
                    ),
                }

            try:

                def relation_progress(completed, total, item):
                    message = (
                        f"연결 표시 준비 중... {completed}/{total} "
                        f"({item.get('name') or Path(item['folder']).name})"
                    )
                    self.root.after(
                        0,
                        lambda value=message: self.status_var.set(value),
                    )

                cache_blender_connection_rows(
                    report,
                    progress_callback=relation_progress,
                    cancel_check=(
                        cancel_event.is_set if cancel_event is not None else None
                    ),
                    metrics=relation_metrics,
                    session_evidence=session_evidence,
                    verify_physical=False,
                )
                tracker = getattr(self, "startup_latency", None)
                if tracker is not None:
                    tracker.milestone(
                        "relation_compute_complete",
                        counts=relation_metrics,
                    )
            except Exception as exc:
                failure = exc
            if generation is not None and not self._refresh_generation_is_current(
                generation, cancel_event
            ):
                return
            self.root.after(
                0,
                lambda result=report, relation_error=failure,
                metrics=dict(relation_metrics),
                persistence=dict(persistence_receipt):
                    self._initial_refresh_done(
                        result,
                        relation_error,
                        stage="relation",
                        relation_metrics=metrics,
                        persistence_receipt=persistence,
                        generation=generation,
                        cancel_event=cancel_event,
                    ),
            )

        self.worker = threading.Thread(target=relation_worker, daemon=True)
        self.worker.start()
        # Receipt migration is an independent read/verification tail.  Run it
        # beside relation calculation; its Tk projection is coalesced with the
        # relation projection when migration wins the race.
        self._start_sync_state_migration()

    def _initial_refresh_done(
        self,
        report,
        error=None,
        *,
        stage="relation",
        relation_metrics=None,
        persistence_receipt=None,
        generation=None,
        cancel_event=None,
    ):
        """Apply the initial worker result on the Tk main thread."""
        if generation is not None and not self._refresh_generation_is_current(
            generation, cancel_event
        ):
            return
        pending_sync = None
        if stage == "relation":
            self._initial_relation_finished = True
            self._initial_relation_ready = error is None
            pending_sync = getattr(
                self, "_pending_initial_sync_result", None
            )
            if (
                error is None
                and pending_sync is not None
                and pending_sync[1] is None
            ):
                # Include an already-computed sync projection in the relation
                # Treeview rebuild instead of deleting/inserting every row a
                # third time.
                self.sync_state = pending_sync[0]
        self.worker = None
        self._initial_refreshing = False
        tracker = getattr(self, "startup_latency", None)
        if tracker is not None:
            if stage == "relation":
                tracker.milestone(
                    "relation_compute_complete",
                    status="ok" if error is None else "error",
                    counts=relation_metrics or {},
                    details=(
                        None if error is None else {
                            "error": f"{type(error).__name__}: {error}"
                        }
                    ),
                )
                tracker.mark(
                    "blender_relations",
                    status="ok" if error is None else "error",
                    counts=relation_metrics or {},
                    details={
                        **(
                            {} if error is None else {
                                "error": f"{type(error).__name__}: {error}"
                            }
                        ),
                        "persistence": dict(persistence_receipt or {}),
                    },
                )
                if error is not None:
                    tracker.finish_early("relation_error", error=error)
            elif error is not None:
                tracker.finish_early("primary_error", error=error)
        if error is not None:
            logged = record_exception("PCG ST9 Texture initial refresh", error)
            log_note = (
                f"\n\n전체 traceback: {ERROR_LOG}"
                if logged
                else ""
            )
            messagebox.showerror(
                "검사 실패",
                f"{error}{log_note}",
                parent=self.root,
            )
            self.status_var.set(
                "live 검사 실패 · 저장/기본 표는 표시 전용 · 다시 검사 필요"
            )
            self._set_busy(False)
            self._display_only_snapshot = True
            self._lock_mutation_controls()
        else:
            relation_view_started = time.perf_counter()
            self.report = report
            self._display_only_snapshot = False
            persistence = (
                report.get("cluster_assembly_receipt_persistence") or {}
            )
            if persistence.get("status") == "warning":
                self.log(
                    "[검사 경고] live audit는 완료됐지만 Cluster receipt "
                    f"저장에 실패했습니다: {persistence.get('error', '')}"
                )
            self.texplan_cache.clear()
            if not hasattr(self, "texplan_errors"):
                self.texplan_errors = {}
            self.texplan_errors.clear()
            self.populate()
            self._update_summary()
            self._set_busy(False)
            if tracker is not None:
                tracker.milestone(
                    "relation_view_applied",
                    counts={
                        "folder_count": len(report.get("items") or ()),
                    },
                    details={
                        "view_apply_seconds": round(
                            time.perf_counter() - relation_view_started, 6
                        ),
                    },
                )
                tracker.milestone(
                    "mutation_usable_ready",
                    counts={
                        "folder_count": len(report.get("items") or ()),
                    },
                    details={"live_relation_evidence": True},
                )
        if stage == "relation" and pending_sync is not None:
            self._pending_initial_sync_result = None
            self._apply_sync_state_migration(
                *pending_sync,
                view_already_applied=(
                    error is None and pending_sync[1] is None
                ),
            )
        for name, receipt in (persistence_receipt or {}).items():
            if receipt.get("status") == "warning":
                self.log(
                    f"[초기검사 캐시 경고] {name}: "
                    f"{receipt.get('error', '저장 실패')}"
                )
        # A refresh requested mid-initial-scan must not be dropped. Start the
        # already-running receipt migration first; refresh() queues behind it
        # so two filesystem audits never race each other.
        if getattr(self, "_pending_refresh", False):
            self._pending_refresh = False
            self.refresh()

    def _lock_mutation_controls(self):
        """Leave read-only refresh available while cached display is visible."""
        for name in (
            "btn_prepare",
            "btn_step2",
            "btn_step3_force",
            "btn_add_target",
            "btn_remove_target",
        ):
            control = getattr(self, name, None)
            if control is not None:
                control.configure(state="disabled")

    def _start_sync_state_migration(self):
        """Verify legacy success reports without delaying the first table paint."""
        if (
            getattr(self, "_initial_relation_finished", False)
            and not getattr(self, "_initial_relation_ready", False)
        ):
            return
        if (self.sync_state or {}).get("migration_complete"):
            tracker = getattr(self, "startup_latency", None)
            if tracker is not None:
                tracker.milestone(
                    "sync_compute_complete",
                    status="cached",
                    counts={
                        "entry_count": len(
                            (self.sync_state or {}).get("entries") or {}
                        )
                    },
                )
            self._sync_state_migration_done(
                self.sync_state, status="cached"
            )
            return
        worker = getattr(self, "sync_migration_worker", None)
        if worker is not None and worker.is_alive():
            return
        self._sync_state_migration_failed = False
        self._sync_state_migrating = True
        self.status_var.set("기존 Unreal 동기화 기록 확인 중…")

        def migrate():
            try:
                state = load_sync_state(migrate=True)
                error = None
            except Exception as exc:
                state, error = None, exc
            tracker = getattr(self, "startup_latency", None)
            if tracker is not None:
                tracker.milestone(
                    "sync_compute_complete",
                    status="ok" if error is None else "error",
                    counts={
                        "entry_count": len((state or {}).get("entries") or {})
                    },
                    details=(
                        None if error is None else {
                            "error": f"{type(error).__name__}: {error}"
                        }
                    ),
                )
            self.root.after(
                0,
                lambda result=state, failure=error:
                    self._sync_state_migration_done(result, failure),
            )

        self.sync_migration_worker = threading.Thread(
            target=migrate, daemon=True
        )
        self.sync_migration_worker.start()

    def _sync_state_migration_done(self, state, error=None, status=None):
        self.sync_migration_worker = None
        self._sync_state_migrating = False
        if (
            getattr(self, "_initial_refreshing", False)
            and not getattr(self, "_initial_relation_finished", False)
        ):
            self._pending_initial_sync_result = (state, error, status)
            return
        self._apply_sync_state_migration(state, error, status)

    def _apply_sync_state_migration(
        self,
        state,
        error=None,
        status=None,
        *,
        view_already_applied=False,
    ):
        """Apply a verified sync state after relation authority is ready."""
        if not getattr(self, "_initial_relation_ready", False):
            if error is not None:
                self.log(f"기존 Unreal 동기화 기록 확인 실패: {error}")
            if getattr(
                self, "_pending_refresh_after_sync_migration", False
            ):
                self._pending_refresh_after_sync_migration = False
                self.refresh()
            return
        tracker = getattr(self, "startup_latency", None)
        phase_status = status or ("ok" if error is None else "error")
        if tracker is not None:
            tracker.mark(
                "sync_migration",
                status=phase_status,
                counts={
                    "entry_count": len((state or {}).get("entries") or {})
                },
                details=(
                    None if error is None
                    else {"error": f"{type(error).__name__}: {error}"}
                ),
            )
        if error is not None:
            self._sync_state_migration_failed = True
            self.log(f"기존 Unreal 동기화 기록 확인 실패: {error}")
            self.status_var.set("기존 Unreal 기록 확인 실패 · ③에서 다시 확인")
            self._update_step3_button()
            if getattr(
                self, "_pending_refresh_after_sync_migration", False
            ):
                self._pending_refresh_after_sync_migration = False
                self.refresh()
            return
        self._sync_state_migration_failed = False
        self.sync_state = state
        sync_view_started = time.perf_counter()
        if not view_already_applied:
            self.populate()
            self._update_summary()
        sync_view_seconds = (
            0.0 if view_already_applied
            else time.perf_counter() - sync_view_started
        )
        if tracker is not None:
            tracker.milestone(
                "sync_view_applied",
                status=phase_status,
                counts={
                    "entry_count": len((state or {}).get("entries") or {})
                },
                details={
                    "view_apply_seconds": round(
                        sync_view_seconds, 6
                    ),
                    "coalesced_with_relation_view": bool(
                        view_already_applied
                    ),
                },
            )
            tracker.milestone(
                "usable_ready_all",
                status=phase_status,
                counts={
                    "folder_count": len((self.report or {}).get("items") or ()),
                    "sync_entry_count": len(
                        (state or {}).get("entries") or {}
                    ),
                },
                details={
                    "live_relation_evidence": True,
                    "sync_state_applied": True,
                },
            )
        count = len((state or {}).get("entries") or {})
        self.status_var.set(f"기존 Unreal 동기화 기록 확인 완료 · {count}장")
        if getattr(self, "_pending_refresh_after_sync_migration", False):
            self._pending_refresh_after_sync_migration = False
            self.refresh()

    def refresh(self):
        if getattr(self, "_sync_state_migrating", False):
            self._pending_refresh_after_sync_migration = True
            self.status_var.set(
                "기존 Unreal 동기화 기록 확인 중… (끝나면 자동으로 다시 검사)"
            )
            return
        if getattr(self, "_initial_refreshing", False):
            self._pending_refresh = True
            self.status_var.set("초기 검사 중... (끝나면 자동으로 다시 검사)")
            return False
        if getattr(self, "_manual_refreshing", False):
            self._pending_refresh = True
            self.status_var.set("검사 중... (끝나면 한 번 더 갱신)")
            return False
        if getattr(self, "_busy", False):
            self._pending_refresh = True
            self.status_var.set("다른 작업 중... (끝나면 다시 검사)")
            return False
        self.cfg["tree_root"] = self.root_var.get()
        save_config(self.cfg)
        self.status_var.set("검사 중...")
        self._manual_refreshing = True
        self._set_busy(True)
        generation, cancel_event = self._begin_refresh_generation()
        cfg = dict(self.cfg)
        use_pcg_targets = bool(self.use_pcg_targets_var.get())

        def worker():
            report = None
            sync_state = None
            error = None
            session_evidence = {}
            try:
                pcg_targets = (
                    load_pcg_targets() if use_pcg_targets else None
                )

                def progress(completed, total, folder):
                    if not self._refresh_generation_is_current(
                        generation, cancel_event
                    ):
                        return
                    message = (
                        f"다시 검사 중... {completed}/{total} "
                        f"({Path(folder).name})"
                    )
                    self.root.after(
                        0,
                        lambda value=message: self.status_var.set(value),
                    )

                report = make_report(
                    cfg,
                    pcg_targets=pcg_targets,
                    progress_callback=progress,
                    cancel_check=cancel_event.is_set,
                    session_evidence=session_evidence,
                )
                publication_allowed = (
                    lambda: self._refresh_generation_is_current(
                        generation, cancel_event
                    )
                )
                # Preserve current content-bound memoization even when the
                # following relation verification safely fails closed.
                save_spm_analysis_cache(
                    publish_check=publication_allowed,
                )
                write_board_display_snapshot(
                    report,
                    cfg,
                    pcg_targets=pcg_targets,
                    publish_check=publication_allowed,
                )

                def relation_progress(completed, total, item):
                    message = (
                        f"연결 표시 준비 중... {completed}/{total} "
                        f"({item.get('name') or Path(item['folder']).name})"
                    )
                    self.root.after(
                        0,
                        lambda value=message: self.status_var.set(value),
                    )

                cache_blender_connection_rows(
                    report,
                    progress_callback=relation_progress,
                    cancel_check=cancel_event.is_set,
                    session_evidence=session_evidence,
                )
                sync_state = load_sync_state(migrate=False)
            except Exception as exc:
                error = exc
            if not self._refresh_generation_is_current(
                generation, cancel_event
            ):
                return
            self.root.after(
                0,
                lambda result=report, state=sync_state, failure=error,
                refresh_generation=generation,
                refresh_cancel=cancel_event:
                    self._manual_refresh_done(
                        result,
                        state,
                        failure,
                        generation=refresh_generation,
                        cancel_event=refresh_cancel,
                    ),
            )

        self.worker = threading.Thread(target=worker, daemon=True)
        self.worker.start()
        return True

    def _manual_refresh_done(
        self,
        report,
        sync_state,
        error=None,
        *,
        generation=None,
        cancel_event=None,
    ):
        """Apply a user-requested audit without blocking the Tk thread."""
        if generation is not None and not self._refresh_generation_is_current(
            generation, cancel_event
        ):
            return
        self.worker = None
        self._manual_refreshing = False
        if error is not None:
            messagebox.showerror("검사 실패", str(error))
            self.status_var.set("검사 실패")
            self._set_busy(False)
        else:
            self.report = report
            self.sync_state = sync_state
            persistence = (
                report.get("cluster_assembly_receipt_persistence") or {}
            )
            if persistence.get("status") == "warning":
                self.log(
                    "[검사 경고] live audit는 완료됐지만 Cluster receipt "
                    f"저장에 실패했습니다: {persistence.get('error', '')}"
                )
            self.texplan_cache.clear()
            if not hasattr(self, "texplan_errors"):
                self.texplan_errors = {}
            self.texplan_errors.clear()
            self.populate()
            self._update_summary()
            self._set_busy(False)

        if getattr(self, "_pending_refresh", False):
            self._pending_refresh = False
            self.refresh()

    def _start_completion_refresh(self, final_status, targets=None):
        """Refresh the table off the Tk thread after a batch completes.

        The completed operation's summary remains the authoritative status;
        the follow-up audit only updates the rows that summary refers to.
        """
        if not getattr(self, "_busy", False):
            self._set_busy(True)
        scoped_targets = sorted(
            {str(path) for path in targets or ()},
            key=os.path.normcase,
        )
        refresh_label = "선택 범위 갱신" if scoped_targets else "표 재검사"
        self.status_var.set(f"{final_status} · {refresh_label} 중...")
        self.cfg["tree_root"] = self.root_var.get()
        save_config(self.cfg)
        cfg = dict(self.cfg)
        use_pcg_targets = bool(self.use_pcg_targets_var.get())

        def worker():
            report = None
            sync_state = None
            error = None
            session_evidence = {}
            try:
                pcg_targets = load_pcg_targets() if use_pcg_targets else None
                report_kwargs = {
                    "pcg_targets": pcg_targets,
                    "session_evidence": session_evidence,
                }
                if scoped_targets:
                    report_kwargs["targets"] = scoped_targets
                report = make_report(cfg, **report_kwargs)
                cache_blender_connection_rows(
                    report,
                    session_evidence=session_evidence,
                )
                persist_cluster_assembly_receipts_safely(report)
                save_spm_analysis_cache()
                sync_state = load_sync_state(migrate=False)
            except Exception as exc:
                error = exc
            self.root.after(
                0,
                lambda result=report, state=sync_state, failure=error,
                scoped=bool(scoped_targets):
                    self._completion_refresh_done(
                        result, state, final_status, failure,
                        scoped=scoped),
            )

        self.worker = threading.Thread(target=worker, daemon=True)
        self.worker.start()

    def _completion_refresh_done(
            self, report, sync_state, final_status, error=None, *,
            scoped=False):
        """Apply a completed background audit and restore its final summary."""
        self.worker = None
        if error is not None:
            self.log(f"표 재검사 실패: {error}")
            self._set_busy(False)
            self.status_var.set(f"{final_status} · 표 재검사 실패")
            return
        if scoped:
            self._apply_step3_scope_report(report)
        else:
            self.report = report
            self._display_only_snapshot = False
        self.sync_state = sync_state
        self.texplan_cache.clear()
        if not hasattr(self, "texplan_errors"):
            self.texplan_errors = {}
        self.texplan_errors.clear()
        self.populate()
        self._update_summary()
        self._set_busy(False)
        self.status_var.set(final_status)

    def _update_summary(self):
        items = self.report["items"]
        n = len(items)
        if self.report.get("inventory_pending"):
            pending = sum(
                item.get("status") == "startup_pending" for item in items
            )
            complete = n - pending
            self.targets_info_var.set("")
            self.status_var.set(
                f"생산 폴더 {n}개 표시 · 상세 검사 {complete}/{n} · "
                "변경 작업 잠김"
            )
            return
        done = sum(1 for i in items if i["status"] == "ready")
        need1 = sum(1 for i in items if i["status"] in ("needs_sk", "needs_m_prefix"))
        need23 = sum(1 for i in items if i["status"] == "needs_texture_work")
        review = sum(1 for i in items if i["status"] in ("needs_source_review", "needs_duplicate_review"))
        pcg = self.report.get("pcg_targets", {})
        if pcg.get("mesh_count"):
            head = f"{self.focus_label}/Cliff에서 쓰는 나무 폴더 {n}개"
            source_time = f" · {pcg['generated_at']}" if pcg.get("generated_at") else ""
            level_reports = pcg.get("levels") or []
            level_names = [Path(row.get("level", "")).name for row in level_reports if row.get("level")]
            level_label = ", ".join(level_names) or "작업 레벨"
            overlap = pcg.get("pcg_level_overlap_mesh_count", 0)
            overlap_text = f" · 양쪽 중복 {overlap}개" if overlap else ""
            self.targets_info_var.set(
                f"고유 대상 {pcg['mesh_count']}개 ({self.focus_label} PCG {pcg.get('pcg_mesh_count', 0)} · "
                f"{level_label} 직접 배치 {pcg.get('level_mesh_count', 0)}{overlap_text}) 중 "
                f"{pcg['matched_mesh_count']}개가 식생 폴더에 매칭됨{source_time}"
            )
            unmatched = pcg.get("unmatched_mesh_names", [])
            if unmatched:
                self.log(f"⚠ 사용 메시 {len(unmatched)}개는 매칭되는 식생 폴더를 못 찾았습니다: "
                         + ", ".join(unmatched[:10])
                         + (" ..." if len(unmatched) > 10 else ""))
            dup = pcg.get("duplicate_mesh_matches", {})
            if dup:
                self.log(f"⚠ 사용 메시 {len(dup)}개는 폴더가 2개 이상 매칭됩니다 (표에 '중복' 표시): "
                         + ", ".join(dup))
        else:
            head = f"식생 폴더 {n}개 (대상 필터 없음)"
            self.targets_info_var.set("")
        self.status_var.set(
            f"{head} — ✅ 다 됨 {done} · ① 필요 {need1} · ②③ 남음 {need23} · ⚠ 확인 필요 {review}"
        )
        self.log(f"검사 완료: {head}. ✅ {done} / ① {need1} / ②③ {need23} / ⚠ {review}")

    @staticmethod
    def _target_source_text(item):
        pcg_count = len(item.get("pcg_mesh_names") or [])
        level_count = len(item.get("level_mesh_names") or [])
        parts = []
        if pcg_count:
            parts.append(f"PCG {pcg_count}")
        if level_count:
            parts.append(f"레벨 {level_count}")
        return " / ".join(parts) or "-"

    @staticmethod
    def _target_row_tag(item):
        in_pcg = bool(item.get("pcg_mesh_names"))
        in_level = bool(item.get("level_mesh_names"))
        if in_pcg and in_level:
            return "target_both"
        if in_pcg:
            return "target_pcg"
        if in_level:
            return "target_level"
        return ""

    def populate(self):
        old_checked = {iid: e["checked"] for iid, e in self.items.items()}
        old_target_checked = {
            entry.get("selection_key"): entry.get("checked", False)
            for entry in getattr(self, "target_items", {}).values()
            if entry.get("selection_key")
        }
        for iid in self.tree.get_children():
            self.tree.delete(iid)
        self.items.clear()
        if not hasattr(self, "target_items"):
            self.target_items = {}
        else:
            self.target_items.clear()
        if not hasattr(self, "target_checked_rows"):
            self.target_checked_rows = CheckedRowController(
                self.target_items, self._redraw_target_checked_row)
        self.row_copy_paths.clear()
        if not hasattr(self, "blend_connection_meta"):
            self.blend_connection_meta = {}
        else:
            self.blend_connection_meta.clear()
        for item in self.report["items"]:
            self._annotate_unreal_sync(item)
            iid = item["folder"]
            # The board is an inventory, not an implicit batch selection.
            # Selecting every new row turned one-folder Sync into fleet work.
            folder_default = old_checked.get(
                iid,
                not getattr(self, "_display_only_snapshot", False),
            )
            target_rows = []
            for target_index, target in enumerate(spm_display_rows(item)):
                selection_key = (
                    str(item["folder"]).casefold(),
                    str(target["mesh_name"]).casefold(),
                )
                target_rows.append({
                    **target,
                    "iid": f"{iid}::target::{target_index}",
                    "selection_key": selection_key,
                    "checked": old_target_checked.get(
                        selection_key, folder_default),
                })
            checked = (
                any(row["checked"] for row in target_rows)
                if target_rows else folder_default
            )
            self.items[iid] = {"item": item, "checked": checked}
            self.row_copy_paths[iid] = spm_paths_for_item(item)
            mark = CHECK_ON if checked else CHECK_OFF
            row_tag = self._target_row_tag(item)
            target_suffix = ""
            if target_rows:
                selected_count = sum(row["checked"] for row in target_rows)
                target_suffix = f" · SPM {selected_count}/{len(target_rows)}"
            self.tree.insert(
                "", "end", iid=iid,
                text=f"{mark} {item['name']}{target_suffix}",
                values=(
                    self._target_source_text(item),
                    self.step1_text(item),
                    self.step2_text(item),
                    self.step3_text(item),
                    blender_connection_overview(item),
                    item["actions"][0] if item["actions"] else "없음 — 준비 끝 ✓",
                ),
                tags=(row_tag,) if row_tag else (),
                open=True,
            )
            for target in target_rows:
                target_iid = target["iid"]
                target_mark = CHECK_ON if target["checked"] else CHECK_OFF
                display_name = target["display_spm"].name
                source_name = (
                    target["source_spm"].name
                    if target.get("source_spm") else ""
                )
                current_path = target.get("current_spm")
                self.target_items[target_iid] = {
                    "item": item,
                    "folder_iid": iid,
                    "mesh": target["mesh_name"],
                    "target_status": target["status"],
                    "display_name": display_name,
                    "selection_key": target["selection_key"],
                    "checked": target["checked"],
                }
                self.tree.insert(
                    iid, "end", iid=target_iid,
                    text=f"{target_mark} {display_name}",
                    values=(
                        target["mesh_name"],
                        target["step1"],
                        "폴더 공유",
                        "폴더 공유",
                        "",
                        f"원본 {source_name}" if source_name else "",
                    ),
                    tags=(
                        "target_spm_ready"
                        if target["status"].get("status") == "ready"
                        else "target_spm_attention",
                    ),
                )
                self.row_copy_paths[target_iid] = (
                    [current_path] if current_path else []
                )
            hierarchy_rows = cluster_hierarchy_rows(item)
            if hierarchy_rows:
                cluster = hierarchy_rows[0]
                cluster_iid = f"{iid}::cluster"
                self.tree.insert(
                    iid, "end", iid=cluster_iid,
                    text=cluster["name"],
                    values=(
                        "", "실제 Cluster 파일", "", "", "", cluster["handoff"]
                    ),
                    open=True,
                )
                self.row_copy_paths[cluster_iid] = [cluster["source"]]
                for child_index, child in enumerate(hierarchy_rows[1:]):
                    role_iid = f"{cluster_iid}::spm::{child_index}"
                    canonical = Path(
                        child.get("canonical_spm") or child["source"]
                    )
                    mirror = Path(
                        child.get("mirror_spm")
                        or canonical.with_name(canonical.name[3:])
                    )
                    target_status = dict(child.get("target_status") or {})
                    step1_note = cluster_pair_step1_text(child)
                    selection_key = (
                        "cluster",
                        str(canonical).casefold(),
                    )
                    checked = old_target_checked.get(
                        selection_key, folder_default)
                    self.target_items[role_iid] = {
                        "item": {
                            "folder": str(canonical.parent),
                            "name": mirror.stem,
                            "duplicate_target_mesh_names": [],
                            "duplicate_pcg_target_mesh_names": [],
                            "target_spm_statuses": [target_status],
                        },
                        "folder_iid": iid,
                        "mesh": mirror.stem,
                        "target_status": target_status,
                        "display_name": canonical.name,
                        "selection_key": selection_key,
                        "checked": checked,
                    }
                    self.row_copy_paths[role_iid] = [
                        canonical if canonical.is_file() else mirror
                    ]
                    assembly_evidence = (
                        f" · {child['materials']}"
                        if child.get("role") and child.get("materials") else ""
                    )
                    target_mark = CHECK_ON if checked else CHECK_OFF
                    self.tree.insert(
                        cluster_iid, "end", iid=role_iid,
                        text=f"{target_mark} {canonical.name}",
                        values=(
                            mirror.stem,
                            step1_note,
                            "—",
                            child["textures"],
                            "",
                            child["handoff"] + assembly_evidence,
                        ),
                    )
            for blend_index, blend_row in enumerate(
                    blender_connection_rows(item)):
                blend_iid = f"{iid}::blend::{blend_index}"
                summary = blender_connection_summary(blend_row)
                needs_attention = any(
                    target.get("connected") is not True
                    and target.get("relation_on", True)
                    for target in blend_row["spms"]
                ) or not blend_row["spms"]
                if not blend_row["export_participating"]:
                    blend_tag = "blender_inactive"
                elif needs_attention:
                    blend_tag = "blender_attention"
                else:
                    blend_tag = "blender_file"
                self.tree.insert(
                    iid, "end", iid=blend_iid,
                    text=f"◆ {blend_row['blend'].name}",
                    values=("", "", "", "", summary, ""),
                    tags=(blend_tag,),
                    open=False,
                )
                self.row_copy_paths[blend_iid] = [blend_row["blend"]]
                self.blend_connection_meta[blend_iid] = {
                    "blend": blend_row["blend"], "spm": None,
                    "item": item,
                    "managed_by": blend_row.get("managed_by"),
                    "cluster_normalized": bool(blend_row.get("cluster_normalized")),
                }
                if not blend_row["spms"]:
                    empty_iid = f"{blend_iid}::spm::none"
                    self.tree.insert(
                        blend_iid, "end", iid=empty_iid,
                        text="↳ 연결 SPM 없음",
                        values=("", "", "", "", "관리 확인 필요", ""),
                        tags=("blender_attention",),
                    )
                    self.row_copy_paths[empty_iid] = []
                    self.blend_connection_meta[empty_iid] = {
                        "blend": blend_row["blend"], "spm": None,
                        "item": item,
                        "managed_by": blend_row.get("managed_by"),
                        "cluster_normalized": bool(blend_row.get("cluster_normalized")),
                    }
                for spm_index, target in enumerate(blend_row["spms"]):
                    spm_iid = f"{blend_iid}::spm::{spm_index}"
                    if target.get("relation_on") is False:
                        connection = "관계 OFF · Generator Sync에서 ON 가능"
                        target_tag = "blender_inactive"
                    elif target["connected"] is True:
                        connection = "Generator 연결 완료 ✓"
                        target_tag = "blender_spm"
                    elif target["connected"] is False:
                        connection = "Generator 연결 점검 필요"
                        target_tag = "blender_attention"
                    else:
                        connection = (
                            "목록 등록 · 연결 상태 미확인"
                            if target.get("listed_in_registry")
                            else "연결 상태 미확인"
                        )
                        target_tag = "blender_attention"
                    if target.get("exists") is False:
                        connection = "목록 등록 · SPM 파일 없음"
                        target_tag = "blender_attention"
                    if (
                        not target["export_participating"]
                        and not blend_row.get("cluster_normalized")
                    ):
                        connection = f"비활성 기록 · {connection}"
                        target_tag = "blender_inactive"
                    self.tree.insert(
                        blend_iid, "end", iid=spm_iid,
                        text=f"↳ {target['spm'].name}",
                        values=("", "", "", "", connection, ""),
                        tags=(target_tag,),
                    )
                    self.row_copy_paths[spm_iid] = [target["spm"]]
                    self.blend_connection_meta[spm_iid] = {
                        "blend": blend_row["blend"], "spm": target["spm"],
                        "item": item,
                        "managed_by": target.get("managed_by") or blend_row.get("managed_by"),
                        "cluster_normalized": bool(blend_row.get("cluster_normalized")),
                        "relation_on": target.get("relation_on"),
                    }
        self.checked_rows.sync_after_reload()
        self.target_checked_rows.sync_after_reload()
        self._update_step3_button()

    def _annotate_unreal_sync(self, item):
        destination = self.cfg.get(
            "unreal_texture_destination", "/Game/Textures")
        for row in item.get("cluster_items") or []:
            row["unreal_synced"] = False
            texture_dir = row.get("texture_dir")
            texture_base = row.get("texture_base") or row.get("atlas_base")
            if not texture_dir or not texture_base \
                    or row.get("missing_export_maps"):
                continue
            paths = output_paths(texture_dir, texture_base)
            row["unreal_synced"] = all(
                is_texture_synced(
                    paths[role], state=getattr(
                        self, "sync_state", {"entries": {}}),
                    destination=destination,
                )
                for role in sbs_auto.RENDER_MAPS
            )

    # ---------------------------------------------------------- column texts
    def step1_text(self, item):
        if item.get("status") == "startup_pending":
            return "상세 검사 대기"
        if item.get("duplicate_target_mesh_names") or item.get("duplicate_pcg_target_mesh_names"):
            return "⚠ 중복 매칭 확인"
        statuses = item.get("target_spm_statuses") or []
        if statuses:
            n_src = sum(1 for s in statuses if s["status"] == "needs_source_review")
            n_sk = sum(1 for s in statuses if s["status"] == "needs_sk")
            missing = [m for s in statuses for m in s.get("materials_missing_m_prefix", [])]
            renames = [pair for s in statuses for pair in s.get("material_renames_needed", [])]
            if n_src:
                return f"⚠ 원본 못 찾음 {n_src}개"
            if n_sk:
                return f"SK 없음 {n_sk}개 → [① 실행]"
        else:
            if not item.get("sk_spms"):
                return "SK 없음 → [① 실행]" if item.get("source_spms") else "⚠ SPM 없음"
            missing = item.get("materials_missing_m_prefix", [])
            renames = item.get("material_renames_needed", [])
        normal, generic = split_generic(missing)
        common = [pair for pair in renames if str(pair[0]).lower().startswith("m_")]
        if common and not normal:
            return f"공용 머티리얼 {len(common)}개 이름 통일 → [① 실행]"
        if normal:
            return f"머티리얼 이름 {len(renames)}개 정리 → [① 실행]"
        if generic:
            return generic_material_summary(item)
        return "완료 ✓"

    def step2_text(self, item):
        if item.get("status") == "startup_pending":
            return "상세 검사 대기"
        sources = item.get("leaf_mesh_sources") or []
        if not sources:
            inventory = item.get("leaf_atlas_inventory") or []
            if inventory:
                active_inventory = [
                    atlas for atlas in inventory
                    if atlas.get("export_participating", True)
                ]
                inactive_count = len(inventory) - len(active_inventory)
                if not active_inventory:
                    return (
                        "현재 잎 매쉬 없음"
                        f" · 비활성 Blender 기록 {inactive_count}세트"
                    )
                complete = sum(
                    1 for atlas in active_inventory if atlas.get("complete"))
                missing_blends = sum(
                    1 for atlas in active_inventory
                    if not atlas.get("atlas_blends"))
                connection_issues = sum(
                    1 for atlas in active_inventory
                    if atlas.get("atlas_blends")
                    and not atlas.get("generator_connection_complete"))
                if complete == len(active_inventory):
                    result = (
                        f"현재 잎 매쉬 {len(active_inventory)}세트 · 연결 완료 ✓"
                    )
                    if inactive_count:
                        result += f" · 비활성 기록 {inactive_count}"
                    return result
                parts = [f"현재 잎 매쉬 {len(active_inventory)}세트"]
                if complete:
                    parts.append(f"연결 완료 {complete}")
                if missing_blends:
                    parts.append(f"제작 파일 없음 {missing_blends}")
                if connection_issues:
                    parts.append(f"연결 점검 {connection_issues}")
                if inactive_count:
                    parts.append(f"비활성 기록 {inactive_count}")
                return " · ".join(parts)
            managed = item.get("managed_leaf_outputs") or []
            if managed:
                return f"현재 연결된 잎 매쉬 {len(managed)}개 ✓"
            provenance = item.get("leaf_source_provenance") or []
            if provenance:
                return (
                    f"과거 잎 원본 {len(provenance)}세트 보존 · "
                    "현재 사용 안 함"
                )
            return "현재 잎 매쉬 없음"
        states = [leaf_source_step2_state(source) for source in sources]
        complete = sum(1 for state in states if state["complete"])
        builds = sum(1 for state in states if state["needs_build"])
        connects = sum(1 for state in states if state["needs_connection"])
        blocked = sum(1 for state in states if state["source_blocked"])
        targets = {
            target.get("spm", "").lower()
            for source in sources for target in source.get("targets", [])
            if target.get("spm")
        }
        if complete == len(sources):
            return f"현재 잎 매쉬 {len(sources)}세트 · 연결 완료 ✓"
        parts = ["작업 필요"]
        if complete:
            parts.append(f"현재 연결 완료 {complete}세트")
        if builds:
            parts.append(f"새로 만들기 {builds}세트")
        if blocked:
            parts.append(f"원본 차단 {blocked}세트")
        if connects:
            parts.append(f"기존 매쉬 연결 {connects}세트")
        if targets:
            parts.append(f"적용 모델 {len(targets)}개")
        return " · ".join(parts)

    def step3_text(self, item):
        if item.get("status") == "startup_pending":
            return "상세 검사 대기"
        state = step3_item_state(item)
        if not state["sets"]:
            if state["connection_sets"]:
                return f"공유 텍스처 · 연결 {state['connection_sets']}세트 정리"
            if state["shared_sets"]:
                suffix = " · Unreal 최신 ✓" if state["unreal_all_synced"] else ""
                return f"공유 텍스처 {state['shared_sets']}세트 사용 ✓{suffix}"
            return "-"
        if state["missing_sets"]:
            return (
                f"연결 텍스처 {state['sets']}세트 · "
                f"{state['complete_maps']}/{state['total_maps']}장 · "
                f"{state['missing_maps']}장 생성"
            )
        if state["connection_sets"]:
            return (
                f"연결 텍스처 {state['sets']}세트 · "
                f"{state['total_maps']}장 완료 · 연결 정리"
            )
        if state["unreal_all_synced"]:
            return (
                f"연결 텍스처 {state['sets']}세트 · "
                f"{state['total_maps']}장 완료 · Unreal 최신 ✓"
            )
        if state["unreal_synced_sets"]:
            return (
                f"연결 텍스처 {state['sets']}세트 · "
                f"Unreal {state['unreal_synced_sets']}/"
                f"{state['unreal_total_sets']}세트 최신"
            )
        return (
            f"연결 텍스처 {state['sets']}세트 · "
            f"{state['total_maps']}장 완료 ✓"
        )

    # ------------------------------------------------------------- details
    def selected_item(self):
        sel = self.tree.selection()
        if not sel:
            return None
        iid = sel[0]
        while iid and iid not in self.items:
            iid = self.tree.parent(iid)
        entry = self.items.get(iid)
        return entry["item"] if entry else None

    def _checked_report_items(self):
        selected = []
        seen = set()
        for iid, entry in self.items.items():
            if not entry.get("checked"):
                continue
            item = entry.get("item")
            if isinstance(item, dict) and id(item) not in seen:
                seen.add(id(item))
                selected.append(item)
        for entry in getattr(self, "target_items", {}).values():
            if not entry.get("checked"):
                continue
            folder_entry = self.items.get(entry.get("folder_iid"))
            item = (folder_entry or {}).get("item")
            if isinstance(item, dict) and id(item) not in seen:
                seen.add(id(item))
                selected.append(item)
        return selected

    def _require_live_mutation_items(self, items=None):
        selected = list(items if items is not None else self._checked_report_items())
        if getattr(self, "_display_only_snapshot", False):
            stale = selected or [self.selected_item()]
        else:
            stale = [
                item for item in selected
                if isinstance(item, dict)
                and not (
                    isinstance(item.get("_gui_live_evidence"), dict)
                    and item["_gui_live_evidence"].get("sha256")
                )
            ]
        if not stale:
            return True
        names = [
            str((item or {}).get("name") or Path((item or {}).get("folder") or "").name)
            for item in stale[:5]
        ]
        messagebox.showerror(
            "live 증거 필요",
            "저장 snapshot 또는 변경된 입력은 작업 권한이 아닙니다. "
            "현재 content identity와 일치하는 live 증거가 없는 행이 있습니다:\n"
            + "\n".join(f"• {name}" for name in names)
            + "\n\n다시 검사한 뒤 실행하세요.",
            parent=self.root,
        )
        self.status_var.set("변경 작업 차단 · current live 증거가 필요합니다")
        return False

    @staticmethod
    def _validate_live_mutation_items(items):
        seen = set()
        content_memo = {}
        exact_content_memo = {}
        directory_memo = {}
        for item in items:
            if not isinstance(item, dict) or id(item) in seen:
                continue
            seen.add(id(item))
            require_current_live_evidence(
                item,
                content_memo=content_memo,
                exact_content_memo=exact_content_memo,
                directory_memo=directory_memo,
            )
        return True

    @staticmethod
    def _mutation_item_key(item):
        return startup_path_key((item or {}).get("folder") or "")

    def _reaudit_and_seal_mutation_items(
            self, items, *, action, plan_payload=None,
            authority_units=None, config_projection=None,
            tool_paths=None):
        """Re-audit selected folders and seal exact current plan inputs.

        The startup hybrid evidence is display/readiness authority only.  A
        mutating worker reaches this gate after obtaining its shared-queue
        turn.  The current semantic report must reproduce the exact mutation
        projection used by the queued plan; only then are all selected payload
        bytes full-hashed and sealed for a final pre-write comparison.
        """
        selected = []
        seen = set()
        for item in items:
            if not isinstance(item, dict):
                continue
            key = self._mutation_item_key(item)
            if not key or key in seen:
                continue
            seen.add(key)
            selected.append(item)
        if not selected:
            raise RuntimeError(
                "mutation authority scope is empty; production files were "
                "not written"
            )

        target_mesh_names = sorted({
            str(status.get("mesh_name") or "")
            for item in selected
            for status in item.get("target_spm_statuses") or ()
            if str(status.get("mesh_name") or "").strip()
        })
        current_report = make_report(
            self.cfg,
            targets=[item["folder"] for item in selected],
            target_mesh_names=target_mesh_names or None,
            mutation_authority=True,
        )
        cache_blender_connection_rows(
            current_report,
            verify_physical=True,
            read_cache=False,
        )
        current_by_key = {
            self._mutation_item_key(item): item
            for item in current_report.get("items") or ()
        }
        current_items = []
        for stale_item in selected:
            key = self._mutation_item_key(stale_item)
            current_item = current_by_key.get(key)
            if current_item is None:
                raise RuntimeError(
                    "선택 작업의 current affected-folder audit 결과가 없습니다. "
                    "production 파일을 쓰지 않고 중단합니다."
                )
            if mutation_semantic_digest(current_item) != mutation_semantic_digest(
                stale_item
            ):
                raise RuntimeError(
                    "선택 작업 계획이 current affected-folder audit와 다릅니다. "
                    "다시 검사한 뒤 실행하세요. production 파일은 쓰지 않았습니다."
                )
            current_items.append(current_item)
        return seal_exact_mutation_baseline(
            current_items,
            action=action,
            plan_payload=plan_payload,
            authority_units=authority_units,
            config_projection=config_projection,
            tool_paths=tool_paths,
            receipt_path=(
                REPORT_DIR
                / (
                    "mutation_authority_"
                    + str(action).replace("/", "_").replace("\\", "_")
                    + "_"
                    + datetime.now().strftime("%Y%m%d_%H%M%S_%f")
                    + ".json"
                )
            ),
        )

    def _texplan_rows(self, item):
        if not hasattr(self, "texplan_errors"):
            self.texplan_errors = {}
        folder = item["folder"]
        if folder not in self.texplan_cache:
            try:
                mini = {"items": [item], "pcg_targets": self.report.get("pcg_targets", {})}
                plan = build_texture_plan_from_report(mini, "<board>")
                self.texplan_cache[folder] = plan.get("items", [])
                self.texplan_errors.pop(folder, None)
            except Exception as exc:
                self.texplan_cache[folder] = None
                self.texplan_errors[folder] = str(exc)
        return self.texplan_cache[folder] or []

    @staticmethod
    def _detail_texture_rows(item):
        """Build lightweight display rows without reopening SPM/SBS files.

        The full texture plan resolves provenance and is intentionally kept for
        the execution buttons.  A row selection must only display information
        already collected by the audit, otherwise every first click performs a
        fresh material-reference scan and makes the Treeview feel unresponsive.
        """
        rows = []
        for cluster in item.get("cluster_items") or []:
            row = dict(cluster)
            buckets = bucket_refs(cluster.get("source_refs") or [])
            row.update({
                "folder": item.get("folder", ""),
                "sbs_files": item.get("sbs_files") or [],
                "normal_convention": item.get("normal_convention", "unknown"),
            })
            for kind, refs in buckets.items():
                row.setdefault(f"source_{kind}", refs)
            # Color and opacity come from the exact SpeedTree material slots;
            # keep those authoritative instead of guessing from file names.
            if cluster.get("source_albedo"):
                row["source_albedo"] = cluster["source_albedo"]
            if cluster.get("source_alpha"):
                row["source_alpha"] = cluster["source_alpha"]
            rows.append(row)
        return rows

    def show_details(self):
        item = self.selected_item()
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        if item:
            self.text.insert("end", self._detail_text(item))
        self.text.configure(state="disabled")

    def _detail_text(self, item):
        L = []
        L.append(f"📁 {item['name']}   {item['folder']}")
        target_names = item.get("target_mesh_names") or item.get("pcg_target_mesh_names") or []
        pcg_names = item.get("pcg_mesh_names") or []
        level_names = item.get("level_mesh_names") or []
        placements = item.get("level_placements") or []
        if pcg_names:
            data_assets = [Path(path).name for path in item.get("pcg_data_assets") or []]
            data_asset_text = f" ({', '.join(data_assets)})" if data_assets else ""
            L.append(
                f"{self.focus_label} PCG에서 사용하는 메시 {len(pcg_names)}개{data_asset_text}: "
                f"{', '.join(pcg_names)}"
            )
        if level_names:
            level_paths = sorted({row.get("level", "") for row in placements if row.get("level")})
            level_label = ", ".join(Path(path).name for path in level_paths) or "작업 레벨"
            component_count = len(placements)
            instance_count = sum(int(row.get("instance_count", 1)) for row in placements)
            actor_count = len({row.get("actor_name") or row.get("actor") for row in placements})
            L.append(
                f"{level_label} 직접 배치 메시 {len(level_names)}개: {', '.join(level_names)} "
                f"(액터 {actor_count} · 컴포넌트 {component_count} · 인스턴스 {instance_count})"
            )
        if not target_names:
            L.append("대상 매칭 정보 없음 (전체 보기 모드이거나 PCG/작업 레벨에서 안 쓰는 폴더)")
        for dup in item.get("duplicate_target_mesh_names") or item.get("duplicate_pcg_target_mesh_names") or []:
            L.append(f"⚠ '{dup}' 은(는) 다른 폴더에도 매칭됩니다 — 어느 폴더가 진짜인지 먼저 정한 뒤 ①을 실행하세요.")
        L.append("")

        hierarchy_rows = cluster_hierarchy_rows(item)
        if hierarchy_rows:
            contract = item.get("cluster_assembly") or {}
            L.append("── Cluster 파일 → SK/Assembly 전달 ──   (실제 폴더 기준)")
            L.append(
                "  · 'Cluster 출력 TGA 연결'은 최종 SPM 슬롯에 기록된 렌더 결과 경로입니다. "
                "실제 파일이 없으면 누락 수를 따로 표시합니다. "
                "Cluster 내부 원본 텍스처와는 다른 항목입니다."
            )
            for row in hierarchy_rows[1:]:
                role_note = (
                    f" / Assembly 역할: {row['role']} / {row['materials']}"
                    if row.get("role") else " / Assembly 역할 판정 없음"
                )
                L.append(f"  · {row['name']}: {row['textures']} / {row['handoff']}{role_note}")
                L.append(f"      source SPM: {row['source']}")
                output_names = [
                    Path(path).name for path in row.get("output_textures") or []
                ]
                if output_names:
                    L.append("      연결된 Cluster 출력: " + ", ".join(output_names))
            bark = contract.get("canonical_bark") or {}
            L.append(
                f"  · canonical bark: {bark.get('canonical_material', '')} / "
                f"{bark.get('status', '')} "
                "(① 접두사 정리와 별도 판정 · 나머지 작업은 계속 가능)"
            )
            wind = (contract.get("handoff") or {}).get(
                "skeleton_wind_contract") or {}
            if wind:
                L.append(
                    "  · Skeleton/Wind: 최종 생성 Skeleton 기준 재생성 · "
                    "Full SK/Assembly 동일 계약 · binding 계층 검증"
                )
            receipt_path = item.get("cluster_assembly_receipt")
            if receipt_path:
                L.append(f"  · persisted receipt: {receipt_path}")
            L.append(f"  · Blender 행 복사 경로: {item['folder']}")
            L.append("")

        # ① SK + M_
        L.append("── ① SK SPM + M_ 머티리얼 이름 ──   (이 도구의 [① 실행] 버튼이 자동 처리)")
        statuses = item.get("target_spm_statuses") or []
        if statuses:
            for s in statuses:
                mesh = s["mesh_name"]
                if s["status"] == "needs_source_review":
                    L.append(f"  · {mesh}: ⚠ 이 폴더에서 원본 SPM을 못 찾음 — 파일 이름을 확인해 주세요.")
                elif not s.get("sk_spm"):
                    src = Path(s["source_spm"]).name if s.get("source_spm") else "?"
                    L.append(f"  · {mesh}: SK 없음 → [① 실행]이 {src} 을(를) 복사해 SK_{src} 생성")
                else:
                    miss = s.get("materials_missing_m_prefix", [])
                    common = [pair for pair in s.get("material_renames_needed", [])
                              if str(pair[0]).lower().startswith("m_")]
                    if miss or common:
                        normal, generic = split_generic(miss)
                        parts = []
                        if normal:
                            parts.append(f"M_ 필요 {len(normal)}개: " + ", ".join(normal))
                        if generic:
                            parts.append(f"⚠ 기본 이름 {len(generic)}개(SpeedTree에서 이름 지은 뒤 ①): "
                                         + ", ".join(generic))
                        if common:
                            parts.append("공용 이름 통일: "
                                         + ", ".join(f"{old}→{new}" for old, new in common))
                        L.append(f"  · {mesh}: {Path(s['sk_spm']).name} ✓ / " + " / ".join(parts))
                    else:
                        L.append(f"  · {mesh}: {Path(s['sk_spm']).name} ✓  머티리얼 이름 완료 ✓")
        else:
            if item.get("sk_spms"):
                L.append("  · SK SPM: " + ", ".join(Path(p).name for p in item["sk_spms"]) + " ✓")
            elif item.get("source_spms"):
                src = Path(item["source_spms"][0]).name
                L.append(f"  · SK 없음 → [① 실행]이 {src} 을(를) 복사해 SK_{src} 생성")
            else:
                L.append("  · ⚠ 이 폴더에 SPM이 없습니다.")
            miss = item.get("materials_missing_m_prefix", [])
            if miss:
                normal, generic = split_generic(miss)
                if normal:
                    L.append(f"  · M_ 필요한 머티리얼 {len(normal)}개: " + ", ".join(normal))
                if generic:
                    L.append(f"  · ⚠ 기본 이름 머티리얼 {len(generic)}개 (SpeedTree에서 이름 지은 뒤 ①): "
                             + ", ".join(generic))
            common = [pair for pair in item.get("material_renames_needed", [])
                      if str(pair[0]).lower().startswith("m_")]
            if common:
                L.append("  · 공용 머티리얼 이름 통일: "
                         + ", ".join(f"{old} → {new}" for old, new in common))
        generic_issues = generic_material_issues(item)
        if generic_issues:
            L.append("  · ⚠ 기본 이름 문제 위치 (이 SPM을 SpeedTree에서 열어 이름 수정):")
            grouped = {}
            for issue in generic_issues:
                key = (issue["spm"], issue["mesh_name"])
                grouped.setdefault(key, []).append(issue["material"])
            for (spm, mesh), names in grouped.items():
                mesh_note = f" / 사용 메시: {mesh}" if mesh else ""
                L.append(f"      SPM: {spm or '미상'}{mesh_note}")
                L.append(f"      머티리얼: {', '.join(names)}")
        L.append("  왜? SK SPM은 나나이트 전환용 복사본, M_ 이름은 send2ue 임포트 규칙입니다.")
        L.append("")

        # ② 원본 잎 아틀라스 / ③ 텍스처 계열
        clusters = item.get("cluster_items", [])
        rows = self._detail_texture_rows(item) if clusters else []
        L.append("── ② 잎 메시 만들기 ──   ([② 실행] 버튼이 자동 처리 · Blender 아틀라스 리프 제너레이터)")
        leaf_sources = item.get("leaf_mesh_sources") or []
        leaf_inventory = item.get("leaf_atlas_inventory") or []
        active_leaf_inventory = [
            atlas for atlas in leaf_inventory
            if atlas.get("export_participating", True)
        ]
        inactive_leaf_inventory = [
            atlas for atlas in leaf_inventory
            if not atlas.get("export_participating", True)
        ]
        leaf_provenance = item.get("leaf_source_provenance") or []
        leaf_targets = item.get("leaf_mesh_target_spms") or []
        legacy_states = item.get("legacy_cluster_states") or []
        legacy_count = sum(
            len(state.get("classified_generator_guids") or [])
            for state in legacy_states
        )
        L.append(
            f"  · 검사 범위: 현재 적용 모델 {len(leaf_targets)}개"
            + (
                " — " + ", ".join(Path(path).name for path in leaf_targets)
                if leaf_targets else ""
            )
        )
        if legacy_count:
            L.append(
                f"  · 과거 Cluster 기록: Generator {legacy_count}개 "
                "(숨김 기록은 자동 작업 수에서 제외)"
            )
        if not leaf_sources and active_leaf_inventory:
            L.append("  · 현재 상태 — 사용 중인 잎 매쉬:")
            for atlas in active_leaf_inventory:
                blends = atlas.get("atlas_blends") or []
                if atlas.get("complete"):
                    state = f"{Path(blends[0]).name} + 현재 Generator 연결 ✓"
                elif blends:
                    state = f"{Path(blends[0]).name} 있음 · 현재 연결 점검 필요"
                else:
                    state = "현재 Generator에서 사용 중 · 대응 blend 없음"
                materials = ", ".join(atlas.get("material_names") or [])
                L.append(f"  · {atlas.get('atlas_base', '')}: {state}")
                if materials:
                    L.append(f"      현재 머티리얼: {materials}")
                L.append("      판정: 읽기 전용 현재 상태 · 자동 재연결 대상 아님")
        if inactive_leaf_inventory:
            L.append("  · 비활성/과거 Blender 결과 — 자동 작업 제외:")
            for atlas in inactive_leaf_inventory:
                blends = atlas.get("atlas_blends") or []
                blend_note = Path(blends[0]).name if blends else "대응 blend 없음"
                L.append(
                    f"      {atlas.get('atlas_base', '')}: {blend_note} · "
                    "현재 export Generator에 참여하지 않음"
                )
        if leaf_provenance:
            L.append("  · 과거 기록 — 현재 사용하지 않는 원본(자동 작업 제외):")
            for source in leaf_provenance:
                L.append(
                    f"      {source.get('source_family', '')}: "
                    f"{source.get('albedo', '')} / {source.get('alpha', '')}")
        if not leaf_sources and not leaf_inventory and not leaf_provenance:
            L.append("  · 최종/Cluster SPM에서 메시화할 원본 잎 아틀라스를 찾지 못했습니다.")
        for source in leaf_sources:
            kinds = source.get("source_kinds") or [source.get("source_kind", "direct")]
            route = "Cluster SPM 내부 원본" if "cluster" in kinds else "최종 SPM 직접 원본"
            blends = source.get("atlas_blends") or []
            step2_state = leaf_source_step2_state(source)
            if blends and step2_state["connection_complete"]:
                state = f"{Path(blends[0]).name} + Generator 연결 ✓"
            elif blends:
                state = f"{Path(blends[0]).name} 있음 · Generator 연결 필요 → [② 실행]"
            else:
                state = "blend 없음 → [② 실행]"
            L.append(
                f"  · 실행할 작업 — {source['source_family']}: "
                f"{state}  ({route})"
            )
            L.append(f"      Albedo: {source['albedo']}")
            L.append(f"      Alpha:  {source['alpha']}")
            L.append(f"      생성 이름: {source['atlas_base']} / Quality: Low / Plate Mode: One Plate")
            for trace in source.get("trace_sources", []):
                materials = ", ".join(trace.get("material_names") or []) or "머티리얼 미상"
                L.append(f"      추적 근거(수정 안 함): {trace.get('spm', '')}  /  원본 머티리얼: {materials}")
            for target in source.get("targets", []):
                materials = ", ".join(leaf_target_material_names(target))
                material_note = f"  /  직접 원본 머티리얼: {materials}" if materials else ""
                connected = leaf_target_connection_complete(target)
                connection_note = "연결 완료 ✓" if connected else "Generator 연결 필요"
                reason = target.get("generator_connection_reason")
                reason_note = f" ({reason})" if reason and not connected else ""
                L.append(
                    f"      최종 SK 적용 대상: {target.get('spm', '')}{material_note}"
                    f"  /  {connection_note}{reason_note}")
            if len(source.get("targets", [])) > 1:
                L.append("      ↳ 같은 원본 아틀라스이므로 메시 생성은 한 번, 모든 최종 SK에 함께 반영")
        if leaf_sources:
            L.append("  왜? Cluster 결과 TGA가 아니라, 그것을 만드는 원본 잎 아틀라스의 알파를 실제 지오메트리로 바꾸기 위해서입니다.")
        L.append("")

        L.append("── ③ 사용 머티리얼별 텍스처 6장 ──   ([③ 실행] 버튼이 자동 처리 · Substance Designer)")
        if not clusters:
            L.append("  · 해당 없음.")
        seen_bases = set()
        for row in rows:
            base = row["atlas_base"]
            if base in seen_bases:
                continue
            seen_bases.add(base)
            if row.get("shared_from"):
                L.append(f"  · {base}: 다른 폴더({row['shared_from']})에서 관리")
                continue
            texture_base = row.get("texture_base") or base
            missing = row.get("missing_export_maps") or []
            if not missing:
                if row.get("connection_update_needed"):
                    aliases = ", ".join(row.get("connection_materials") or [])
                    L.append(
                        f"  · {base} → {texture_base}: 6장 모두 있음 · "
                        f"SpeedTree 연결 정리 필요 ({aliases})")
                else:
                    L.append(f"  · {base} → {texture_base}: 6장 모두 있음 ✓")
                continue
            L.append(f"  · {base} → {texture_base}: 누락 → {', '.join(missing)} → [③ 실행]이 자동 렌더")
            if not row.get("m_graph"):
                try:
                    selected = sbs_auto.select_source_set(row, require_alpha=False)
                    L.append(f"      SpeedTree 원본 참조: {selected['label']}")
                    if row.get("material_spms"):
                        L.append("      연결 근거 SPM: "
                                 + ", ".join(Path(path).name for path in row["material_spms"]))
                except Exception as exc:
                    legacy = row.get("legacy_export_maps") or {}
                    if all(legacy.get(name) for name in sbs_auto.RENDER_MAPS):
                        L.append("      기존 M_ 출력은 SBS의 Unreal용 출력이므로 원본 입력에서 제외")
                    L.append(f"      ⚠ {exc}")
            sbs = ([row.get("m_graph_sbs")] if row.get("m_graph_sbs")
                   else row.get("sbs_files") or [])
            L.append(f"      SBS: {Path(sbs[0]).name if sbs else '⚠ 없음 — [③ 실행]이 텍스처만 생성'}"
                     f"   저장 위치: {row.get('texture_dir', '')}")
            conv = row.get("normal_convention", "unknown")
            if conv == "OpenGL":
                L.append("      노멀: OpenGL=true (TCom/Megascan 원본)")
            elif conv == "DirectX":
                L.append("      노멀: OpenGL=false (Substance 머티리얼 원본)")
            else:
                L.append("      노멀: ⚠ 원본 출처 불명 — TCom/Megascan이면 OpenGL, sbsar이면 DirectX")
            L.append("      AO: 원본 AO 없으면 height로 HBAO 만들어 연결 / SDF: 연결만 하고 값 0")
        return "\n".join(L)

    # ------------------------------------------------------------- ① 실행
    def _build_prepare_rows(self, entries=None):
        """선택 범위에서 ① 작업 목록을 만들고 문제(blocker)를 분류한다."""
        rows = []
        if entries is None:
            source_entries = getattr(self, "target_items", {}) or self.items
        else:
            source_entries = entries
        for iid, entry in source_entries.items():
            if not entry["checked"]:
                continue
            item = entry["item"]
            duplicates = set(item.get("duplicate_target_mesh_names") or item.get("duplicate_pcg_target_mesh_names", []))
            exact_status = entry.get("target_status")
            statuses = (
                [exact_status]
                if exact_status is not None
                else item.get("target_spm_statuses") or []
            )
            jobs = []  # (mesh_name or None)
            if statuses:
                for s in statuses:
                    if s["status"] in (
                        "needs_sk", "needs_m_prefix"
                    ):
                        jobs.append(s["mesh_name"])
                    elif s["status"] == "pair_conflict":
                        rows.append({
                            "item": item,
                            "mesh": s["mesh_name"],
                            "preview": None,
                            "blockers": ["pair_conflict"],
                            "generic": [],
                            "tree_iid": iid,
                        })
                    elif s["status"] == "material_name_conflict":
                        rows.append({
                            "item": item,
                            "mesh": s["mesh_name"],
                            "preview": None,
                            "blockers": ["material_name_conflict"],
                            "generic": [],
                            "tree_iid": iid,
                        })
                    elif s["status"] == "needs_source_review":
                        rows.append({
                            "item": item, "mesh": s["mesh_name"], "preview": None,
                            "blockers": ["source_review"], "generic": [],
                            "tree_iid": iid,
                        })
            else:
                if (not item.get("sk_spms") or item.get("materials_missing_m_prefix")
                        or item.get("material_renames_needed")):
                    if item.get("source_spms") or item.get("sk_spms"):
                        jobs.append(None)
                    else:
                        continue
            for mesh in jobs:
                blockers = []
                if mesh and mesh in duplicates:
                    blockers.append("duplicate")
                try:
                    preview = prepare_sk(item["folder"], [mesh] if mesh else None, dry_run=True)
                except Exception as exc:
                    rows.append({"item": item, "mesh": mesh, "preview": None,
                                 "blockers": [f"오류: {exc}"], "generic": [],
                                 "tree_iid": iid})
                    continue
                targets = preview.get("targets") or [preview]
                target = targets[0] if targets else {}
                if target.get("status") == "skipped":
                    blockers.append(f"자동 처리 불가: {target.get('reason', '?')}")
                elif target.get("status") == "blocked":
                    blockers.append(
                        "material_name_conflict"
                        if target.get("material_name_conflicts")
                        else "pair_conflict"
                    )
                elif target.get("status") == "up_to_date":
                    continue
                patch = target.get("patch") or {}
                renames = patch.get("renames", [])
                normal, generic = split_generic([old for old, _new in renames])
                # 기본 이름 머티리얼은 이름을 바꾸지 않고 남겨둔다(부분 적용).
                # SK 생성도, 정상 이름 변경도 없다면 이 항목은 ①이 할 일이 없다.
                if not target.get("would_create") and not normal and generic:
                    blockers.append("generic_only")
                rows.append({"item": item, "mesh": mesh, "preview": target,
                             "blockers": blockers, "generic": generic,
                             "normal_renames": normal, "tree_iid": iid})
        return rows

    @staticmethod
    def _blocker_text(code):
        return BLOCKER_TEXT.get(code, code)

    def start_prepare(self):
        if not self.report:
            self.refresh()
            self.status_var.set("검사가 끝난 뒤 ①을 다시 실행하세요.")
            return
        prepare_entries = getattr(self, "target_items", {}) or self.items
        checked = [e for e in prepare_entries.values() if e["checked"]]
        if not checked:
            messagebox.showinfo(
                "① 실행",
                "체크된 SPM 행이 없습니다.\n"
                "일반 식생 또는 Cluster의 canonical SK 자식 행을 체크하세요.",
            )
            return
        if not self._require_live_mutation_items():
            return
        self.status_var.set("① 준비 상태 확인 중...")
        self.root.update_idletasks()
        rows = self._build_prepare_rows()
        if not rows:
            messagebox.showinfo(
                "① 실행",
                "선택 범위에 ①이 필요한 것이 없습니다.\n"
                "(변경 없음: 모두 SK와 M_ 이름이 이미 완료된 상태)",
            )
            self.status_var.set("대기")
            return
        force = bool(self.force_var.get())
        doable = []
        skipped = []
        for row in rows:
            hard_block = row["preview"] is None or any(
                b.startswith("자동 처리 불가") or b.startswith("오류")
                or b in (
                    "source_review", "generic_only", "pair_conflict",
                    "material_name_conflict",
                )
                for b in row["blockers"]
            )
            if hard_block or (row["blockers"] and not force):
                skipped.append(row)
            else:
                # force가 아니면 기본 이름 머티리얼은 이름을 바꾸지 않고 남겨둔다.
                row["exclude"] = [] if force else list(row["generic"])
                doable.append(row)
        n_create = sum(1 for r in doable if r["preview"].get("would_create"))
        n_rename = sum(
            len((r["preview"].get("patch") or {}).get("renames", [])) - len(r["exclude"])
            for r in doable
        )
        n_generic_kept = sum(len(r["exclude"]) for r in doable)
        msg = ["① SK 만들기 + 머티리얼 이름 정리\n", "지금 실행하면:"]
        msg.append(f" · SK SPM 새로 만들기: {n_create}개 (원본 SPM을 SK_이름.spm 으로 복사)")
        msg.append(f" · 머티리얼 M_ / 공용 이름 정리: {n_rename}개")
        if n_generic_kept:
            msg.append(f" · 'Material 2' 같은 기본 이름 {n_generic_kept}개는 그대로 둡니다 "
                       "(SpeedTree에서 이름 지은 뒤 다시 ① 실행)")
        msg.append(" · 수정 전 원본은 각 폴더의 _spm_backups\\ 에 백업됩니다.")
        if skipped:
            msg.append(f"\n건너뛰는 항목 {len(skipped)}개 (로그에 이유 표시):")
            for row in skipped[:8]:
                label = row["mesh"] or row["item"]["name"]
                msg.append(f" · {label}: {self._blocker_text(row['blockers'][0])}")
            if len(skipped) > 8:
                msg.append(f" · ... 외 {len(skipped) - 8}개")
        if not doable:
            msg.append("\n적용할 수 있는 항목이 없습니다.")
            messagebox.showinfo("① 실행", "\n".join(msg))
            self.status_var.set("대기")
            for row in skipped:
                label = row["mesh"] or row["item"]["name"]
                self.log(f"[① 건너뜀] {label}: " + "; ".join(self._blocker_text(b) for b in row["blockers"]))
            return
        msg.append("\n계속할까요?")
        if not messagebox.askyesno("① 실행", "\n".join(msg)):
            self.status_var.set("대기")
            return
        for row in skipped:
            label = row["mesh"] or row["item"]["name"]
            self.log(f"[① 건너뜀] {label}: " + "; ".join(self._blocker_text(b) for b in row["blockers"]))
            self.tree.set(
                row.get("tree_iid")
                or row["item"].get("_tree_iid", row["item"]["folder"]),
                "step1",
                "⚠ 건너뜀 (로그 참조)",
            )
        try:
            shared = self._enqueue_shared_execution(
                f"PCG ① SK/재질 정규화 · {len(doable)}개",
                {
                    "operation": "step1_prepare",
                    "targets": [
                        {
                            "folder": str(row["item"].get("folder") or ""),
                            "mesh": str(row.get("mesh") or ""),
                        }
                        for row in doable
                    ],
                },
            )
        except Exception as exc:
            self._shared_execution_enqueue_failed(exc)
            return
        self._set_busy(True)
        self.status_var.set(f"① 적용 중... ({len(doable)}개)")
        self.worker = threading.Thread(
            target=self._run_shared_execution,
            args=(
                shared,
                f"PCG ① SK/재질 정규화 · {len(doable)}개",
                self._run_prepare,
                (doable,),
            ),
            daemon=True,
        )
        self.worker.start()

    def _run_prepare(self, rows):
        plan_payload = {
            "rows": [step1_unit_payload(row) for row in rows]
        }
        authority_units = step1_authority_units(rows)
        baseline = self._reaudit_and_seal_mutation_items(
            (row.get("item") for row in rows),
            action="step1_prepare",
            plan_payload=plan_payload,
            authority_units=authority_units,
            tool_paths=[mutation_source_path(prepare_sk)],
        )
        done = 0
        failed = 0
        for index, row in enumerate(rows):
            item = row["item"]
            mesh = row["mesh"]
            label = mesh or item["name"]
            preferred_unit_id = authority_units[index]["unit_id"]
            unit_id = exact_mutation_unit_id(
                baseline, preferred_unit_id
            )
            require_exact_mutation_baseline(
                baseline,
                unit_id=unit_id,
                current_unit_payload=(
                    step1_unit_payload(row)
                    if unit_id == preferred_unit_id else None
                ),
            )
            try:
                result = prepare_sk(item["folder"], [mesh] if mesh else None,
                                    dry_run=False, exclude_materials=row.get("exclude"))
                complete_exact_mutation_unit(
                    baseline,
                    unit_id,
                    post_paths=[item["folder"]],
                )
                targets = result.get("targets") or [result]
                for target in targets:
                    if target.get("created"):
                        self._ui(lambda t=target: self.log(f"[① SK 생성] {Path(t['created']).name}"))
                    patch = target.get("patch") or {}
                    for old, new in patch.get("renames", []):
                        self._ui(lambda o=old, n=new, lb=label: self.log(
                            f"[① 머티리얼 이름] {lb}: {o} → {n}"))
                    if patch.get("backup"):
                        self._ui(lambda p=patch: self.log(f"    백업: {p['backup']}"))
                    normalized = target.get("bootstrap") or {}
                    if normalized.get("status") == "applied":
                        self._ui(lambda t=target: self.log(
                            "[① Output 이름 정규화] "
                            f"{Path(t['mirror_spm']).name} → "
                            f"{Path(t['canonical_spm']).name}"
                        ))
                done += 1
                up_to_date = bool(targets) and all(
                    target.get("status") == "up_to_date" for target in targets
                )
                if up_to_date:
                    self._ui(lambda lb=label: self.log(
                        f"[① 변경 없음] {lb}: 이미 최신입니다."))
                    self._ui(lambda i=item, r=row: self.tree.set(
                        r.get("tree_iid")
                        or i.get("_tree_iid", i["folder"]),
                        "step1", "완료 ✓ (변경 없음)"))
                else:
                    self._ui(lambda i=item, r=row: self.tree.set(
                        r.get("tree_iid")
                        or i.get("_tree_iid", i["folder"]),
                        "step1", "완료 ✓ (방금 적용)"))
            except Exception as exc:
                failed += 1
                self._ui(lambda lb=label, e=exc: self.log(f"[① 실패] {lb}: {e}"))
        self._ui(lambda: self._prepare_finished(done, failed))
        return {
            "shared_queue_success": failed == 0,
            "shared_queue_result": {
                "operation": "step1_prepare",
                "completed": done,
                "failed": failed,
            },
        }

    def _ui(self, fn):
        self.root.after(0, fn)

    def _enqueue_shared_execution(self, label, payload):
        """Register one mutating request before any asset worker starts."""

        runtime = getattr(self, "shared_queue_runtime", None)
        if runtime is None:
            # Lightweight unit-test App instances deliberately omit the
            # production cross-process runtime.
            return None
        record = runtime.enqueue(
            str(label),
            {
                "tool": "pcg_st9_texture_batch",
                **dict(payload or {}),
            },
        )
        self.log(
            f"[공용 대기열 등록 #{record['sequence']}] {label}"
        )
        return record

    def _wait_for_shared_execution(self, record):
        if record is None:
            return None
        runtime = getattr(self, "shared_queue_runtime", None)
        if runtime is None:
            return None

        def on_wait(wait_state):
            position = wait_state.get("position")
            queued = wait_state.get("queued_count", 0)
            text = (
                "공용 대기열 대기"
                + (f" · 전체 {position}번째" if position else "")
                + f" · 대기 {queued}개"
            )
            self._ui(lambda value=text: self.status_var.set(value))

        lease = runtime.wait_for_turn(
            record["id"],
            on_wait=on_wait,
        )
        self._ui(
            lambda: self.status_var.set(
                "공용 대기열 진입 · 단독 실행"
            )
        )
        return lease

    def _shared_execution_enqueue_failed(self, exc):
        self.status_var.set("공용 대기열 등록 실패 · 실행하지 않음")
        messagebox.showerror(
            "공용 대기열 등록 실패",
            (
                "다른 창과 동시에 실행되지 않도록 작업을 시작하지 "
                f"않았습니다.\n\n{exc}"
            ),
        )

    def _run_shared_execution(
            self, record, label, target, args=(), kwargs=None):
        """Wait for the global turn, run one callback, and release safely."""

        lease = None
        try:
            lease = self._wait_for_shared_execution(record)
            result = target(*(args or ()), **dict(kwargs or {}))
            queue_success = not (
                isinstance(result, dict)
                and result.get("shared_queue_success") is False
            )
            queue_result = (
                dict(result.get("shared_queue_result") or {})
                if isinstance(result, dict) else {}
            )
            if lease is not None:
                lease.finish(
                    success=queue_success,
                    result={
                        "tool": "pcg_st9_texture_batch",
                        "label": str(label),
                        "outcome": (
                            "completed" if queue_success else "failed"
                        ),
                        **queue_result,
                    },
                )
            return result
        except Exception as exc:
            if lease is not None and not lease.finished:
                try:
                    lease.finish(
                        success=False,
                        result={
                            "tool": "pcg_st9_texture_batch",
                            "label": str(label),
                            "outcome": "failed",
                            "error": str(exc),
                        },
                    )
                except Exception as queue_exc:
                    exc = RuntimeError(
                        f"{exc}; 공용 대기열 종료 기록 실패: {queue_exc}"
                    )
            self._ui(
                lambda error=exc, action=str(label):
                self._shared_execution_failed(action, error)
            )
            return None

    def _run_claimed_shared_execution(
            self, lease, label, target, args=(), kwargs=None):
        """Continue a job whose lease was acquired during live planning."""

        try:
            result = target(*(args or ()), **dict(kwargs or {}))
            queue_success = not (
                isinstance(result, dict)
                and result.get("shared_queue_success") is False
            )
            queue_result = (
                dict(result.get("shared_queue_result") or {})
                if isinstance(result, dict) else {}
            )
            lease.finish(
                success=queue_success,
                result={
                    "tool": "pcg_st9_texture_batch",
                    "label": str(label),
                    "outcome": (
                        "completed" if queue_success else "failed"
                    ),
                    **queue_result,
                },
            )
            return result
        except Exception as exc:
            if not lease.finished:
                try:
                    lease.finish(
                        success=False,
                        result={
                            "tool": "pcg_st9_texture_batch",
                            "label": str(label),
                            "outcome": "failed",
                            "error": str(exc),
                        },
                    )
                except Exception as queue_exc:
                    exc = RuntimeError(
                        f"{exc}; 공용 대기열 종료 기록 실패: {queue_exc}"
                    )
            self._ui(
                lambda error=exc, action=str(label):
                self._shared_execution_failed(action, error)
            )
            return None

    def _shared_execution_failed(self, label, exc):
        self.log(f"[공용 대기열 작업 실패] {label}: {exc}")
        self.status_var.set(f"{label} 실패")
        self._set_busy(False)
        messagebox.showerror("작업 실패", str(exc))

    def _set_busy(self, busy):
        self._busy = bool(busy)
        state = "disabled" if busy else "normal"
        control_names = (
            "btn_prepare", "btn_step2", "btn_refresh", "btn_pick_root",
            "btn_select_all", "btn_clear_all", "btn_live_targets",
            "btn_saved_targets", "root_entry", "chk_pcg_targets",
            "chk_force", "chk_spm_push",
            "btn_add_target", "btn_remove_target",
        )
        for name in control_names:
            control = getattr(self, name, None)
            if control is not None:
                control.configure(state=state)
        self._update_step3_button()

    def _update_step3_button(self):
        """Keep the action truthful after scans and checkbox changes."""
        if not hasattr(self, "btn_step3"):
            return
        if getattr(self, "_display_only_snapshot", False):
            checked = any(
                entry.get("checked")
                for entry in getattr(self, "items", {}).values()
            )
            self.btn_step3.configure(
                text="③ 선택 항목만 검사 후 실행",
                state=(
                    "normal"
                    if checked and not getattr(self, "_busy", False)
                    else "disabled"
                ),
            )
            if hasattr(self, "btn_step3_force"):
                self.btn_step3_force.configure(state="disabled")
            return
        if getattr(self, "_sync_state_migrating", False):
            self.btn_step3.configure(
                text="③ 기존 Unreal 동기화 기록 확인 중…",
                state="disabled",
            )
            if hasattr(self, "btn_step3_force"):
                self.btn_step3_force.configure(state="disabled")
            return
        if getattr(self, "_sync_state_migration_failed", False):
            self.btn_step3.configure(
                text="③ 기존 Unreal 기록 확인 실패 · 다시 검사",
                state="disabled",
            )
            if hasattr(self, "btn_step3_force"):
                self.btn_step3_force.configure(state="disabled")
            return
        state = step3_selection_state(getattr(self, "items", {}))
        self.btn_step3.configure(
            text=state["text"],
            state="disabled" if getattr(self, "_busy", False) else state["state"],
        )
        if hasattr(self, "btn_step3_force"):
            force_state = step3_force_selection_state(
                getattr(self, "items", {})
            )
            self.btn_step3_force.configure(
                text=force_state["text"],
                state=(
                    "disabled"
                    if getattr(self, "_busy", False)
                    else force_state["state"]
                ),
            )

    def _prepare_finished(self, done, failed):
        summary = f"① 완료: 처리 {done}개, 실패 {failed}개"
        self.log(f"{summary}. 표를 다시 검사합니다.")
        self._start_completion_refresh(summary)

    # ------------------------------------------------------------- ②③ 공용
    def _scoped_texplan_rows(self, checked_only=True):
        """Return unique (item, texture row) pairs in the requested board scope."""
        result = []
        for entry in self.items.values():
            if checked_only and not entry["checked"]:
                continue
            item = entry["item"]
            if not item.get("cluster_items"):
                continue
            seen = set()
            for row in self._texplan_rows(item):
                base = row.get("atlas_base", "")
                if not base or base.lower() in seen:
                    continue
                seen.add(base.lower())
                result.append((item, row))
        return result

    def _checked_texplan_rows(self):
        """체크된 행의 (item, texplan row) 목록. 같은 atlas_base는 폴더당 1번."""
        return self._scoped_texplan_rows(checked_only=True)

    def _all_texplan_rows(self):
        """현재 표 전체의 고유 로컬 텍스처 행을 반환한다."""
        return self._scoped_texplan_rows(checked_only=False)

    def _graph_albedo_alpha(self, row):
        """SBS의 T_ 그래프(또는 레거시 M_) 원본 연결을 그대로 쓴다."""
        if not row.get("m_graph") or not row.get("m_graph_sbs"):
            return None, None
        try:
            info = sbs_auto.parse_m_graph(row["m_graph_sbs"], row["m_graph"])
        except Exception:
            return None, None

        def usable(slot):
            path = info["inputs"].get(slot)
            if not path:
                return None
            path = Path(path)
            if not path.exists() or "neutral" in path.name.lower():
                return None
            return path

        return usable("Base_Color"), usable("Opacity")

    # ------------------------------------------------------------- ② 실행
    def _step2_jobs(self, connect_spm=True):
        grouped, skipped = {}, []
        atlas_root = Path(self.cfg["atlas_root"])
        for entry in self.items.values():
            if not entry["checked"]:
                continue
            item = entry["item"]
            for source in item.get("leaf_mesh_sources") or []:
                base = source.get("atlas_base", "")
                state = leaf_source_step2_state(source)
                audited_blend = existing_leaf_blend(source)
                if state["complete"] and audited_blend:
                    continue
                expected_blend = atlas_root / f"{base}.blend"
                if not audited_blend and expected_blend.is_file():
                    skipped.append((
                        item, base,
                        "동일 이름 blend가 있지만 이 원본과의 일치가 확인되지 않음",
                    ))
                    continue
                if audited_blend and state["needs_connection"] and not connect_spm:
                    skipped.append((
                        item, base,
                        "blend는 있으나 최종 SK Generator 연결 필요 — 연결 옵션을 켜세요",
                    ))
                    continue
                albedo = Path(source.get("albedo", ""))
                alpha = Path(source.get("alpha", ""))
                if not audited_blend and (not albedo.is_file() or not alpha.is_file()):
                    missing = []
                    if not albedo.is_file():
                        missing.append("알베도")
                    if not alpha.is_file():
                        missing.append("알파")
                    skipped.append((item, base, f"{'/'.join(missing)} 원본 파일 없음"))
                    continue
                if not audited_blend:
                    declarations = atlas_provisional_source_declarations(
                        item.get("folder") or ""
                    )
                    blocked_sources = []
                    for role, path in (
                        ("Albedo", albedo),
                        ("Alpha", alpha),
                    ):
                        blocked_state = _unsafe_provisional_source(
                            path,
                            asset_root=item.get("folder") or "",
                            source_texture_roots=(
                                self.cfg.get("source_texture_roots") or []
                            ),
                            declared_source_paths=declarations,
                        )
                        if blocked_state:
                            blocked_sources.append(
                                f"{role}={path} ({blocked_state})"
                            )
                    if blocked_sources:
                        skipped.append((
                            item,
                            base,
                            "원본 차단: " + "; ".join(blocked_sources),
                        ))
                        continue

                pending_targets = pending_leaf_targets(source) if connect_spm else []
                target_errors = []
                for target in pending_targets:
                    spm = Path(target.get("spm", ""))
                    if not spm.is_file():
                        target_errors.append(f"{spm.name or spm}: 최종 SK SPM 없음")
                    elif not leaf_target_material_names(target):
                        reason = target.get("generator_connection_reason")
                        suffix = f" ({reason})" if reason else ""
                        target_errors.append(
                            f"{spm.name}: 원본 머티리얼 식별 실패{suffix}")
                if target_errors:
                    skipped.append((item, base, "; ".join(target_errors)))
                    continue

                if audited_blend:
                    key = ("reuse", str(audited_blend.resolve()).lower())
                else:
                    key = (
                        "build",
                        str(albedo.resolve()).lower(),
                        str(alpha.resolve()).lower(),
                    )
                job = grouped.get(key)
                if job is None:
                    job = {
                        "item": item, "items": [item], "base": base,
                        "albedo": albedo, "alpha": alpha,
                        "blend_out": audited_blend or expected_blend,
                        "reuse_existing_blend": bool(audited_blend),
                        "target_spms": [], "target_details": [],
                    }
                    grouped[key] = job
                elif item not in job["items"]:
                    job["items"].append(item)
                for target in pending_targets:
                    merge_step2_target_detail(job, target)
        return list(grouped.values()), skipped

    def start_step2(self):
        if not self.report:
            self.refresh()
            self.status_var.set("검사가 끝난 뒤 ②를 다시 실행하세요.")
            return
        if not self._require_live_mutation_items():
            return
        self.status_var.set("② 대상 확인 중...")
        self.root.update_idletasks()
        push_spm = bool(self.spm_push_var.get())
        jobs, skipped = self._step2_jobs(connect_spm=push_spm)
        for item, base, reason in skipped:
            self.log(f"[② 건너뜀] {item['name']} / {base}: {reason}")
        if not jobs:
            messagebox.showinfo("② 실행", "체크된 항목 중 잎 메시를 만들거나 연결할 작업이 없습니다."
                                + (f"\n(건너뜀 {len(skipped)}개 — 로그 참조)" if skipped else ""))
            self.status_var.set("대기")
            return
        no_spm = [j for j in jobs if push_spm and not j["target_spms"]]
        build_count = sum(not job["reuse_existing_blend"] for job in jobs)
        reuse_count = len(jobs) - build_count
        msg = ["② 잎 메시 만들기 / 최종 SK 연결 (헤드리스 Blender)\n"]
        msg.append(
            f"새 blend {build_count}개 · 기존 blend 재사용 {reuse_count}개 "
            "(Quality=Low, One Plate):")
        for j in jobs[:10]:
            mode = "기존 blend 재사용" if j["reuse_existing_blend"] else "새로 생성"
            source_note = (
                f"알베도: {Path(j['albedo']).name} / "
                if not j["reuse_existing_blend"] else "")
            msg.append(
                f" · {Path(j['blend_out']).name}  ({mode} / {source_note}"
                f"최종 SK: {len(j['target_spms'])}개)")
        if len(jobs) > 10:
            msg.append(f" · ... 외 {len(jobs) - 10}개")
        msg.append(f"저장 위치: {self.cfg['atlas_root']}")
        if push_spm:
            msg.append("\nMaterial_v8 + Mesh 자산을 최종 SK에 등록하고 Leaf Mesh/Frond Generator를 연결합니다.")
            msg.append("Cluster SPM은 수정하지 않으며, 실패한 최종 SK는 백업에서 복원합니다.")
            if no_spm:
                msg.append(f"⚠ 최종 SK가 없는 {len(no_spm)}개는 blend만 만듭니다.")
        if skipped:
            msg.append(f"\n건너뜀 {len(skipped)}개 (원본 못 찾음 — 로그 참조)")
        msg.append("\n묶음당 수십초~수분 걸립니다. 계속할까요?")
        if not messagebox.askyesno("② 실행", "\n".join(msg)):
            self.status_var.set("대기")
            return
        try:
            shared = self._enqueue_shared_execution(
                f"PCG ② Atlas/Cluster · {len(jobs)}개",
                {
                    "operation": "step2_atlas",
                    "push_spm": bool(push_spm),
                    "blend_outputs": [
                        str(job.get("blend_out") or "")
                        for job in jobs
                    ],
                    "target_spms": [
                        str(path)
                        for job in jobs
                        for path in job.get("target_spms") or []
                    ],
                },
            )
        except Exception as exc:
            self._shared_execution_enqueue_failed(exc)
            return
        self._set_busy(True)
        self.worker = threading.Thread(
            target=self._run_shared_execution,
            args=(
                shared,
                f"PCG ② Atlas/Cluster · {len(jobs)}개",
                self._run_step2,
                (jobs, push_spm),
            ),
            daemon=True,
        )
        self.worker.start()

    def _run_step2(self, jobs, push_spm):
        plan_payload = step2_exact_plan_payload(jobs, push_spm)
        authority_units = step2_authority_units(jobs, push_spm)
        config_keys = ("blender_exe", "atlas_job_timeout")
        blender_exe = self.cfg.get("blender_exe", "")
        baseline = self._reaudit_and_seal_mutation_items(
            (
                item
                for job in jobs
                for item in job.get("items") or [job.get("item")]
            ),
            action="step2_atlas",
            plan_payload=plan_payload,
            authority_units=authority_units,
            config_projection=mutation_config_projection(
                self.cfg, config_keys
            ),
            tool_paths=[
                blender_exe,
                blender_user_startup_path(blender_exe),
                TOOL_DIR / "jobs" / "atlas_blend_job.py",
            ],
        )
        from pcg_texture_common import REPORT_DIR
        REPORT_DIR.mkdir(parents=True, exist_ok=True)
        done = failed = 0
        total = len(jobs)
        for index, job in enumerate(jobs, 1):
            preferred_unit_id = authority_units[index - 1]["unit_id"]
            unit_id = exact_mutation_unit_id(
                baseline, preferred_unit_id
            )
            require_exact_mutation_baseline(
                baseline,
                unit_id=unit_id,
                current_unit_payload=(
                    step2_unit_payload(job, push_spm)
                    if unit_id == preferred_unit_id else None
                ),
                current_config=(
                    mutation_config_projection(self.cfg, config_keys)
                    if unit_id == preferred_unit_id else None
                ),
            )
            base = job["base"]
            action = "기존 blend 연결" if job["reuse_existing_blend"] else "생성"
            self._ui(lambda i=index, b=base, a=action: self.status_var.set(
                f"② {i}/{total}: {b} {a} 중..."))
            self._ui(lambda it=job["item"], a=action: self.tree.set(
                it["folder"], "step2", f"{a} 중..."))
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            report_path = REPORT_DIR / f"atlas_job_{base}_{stamp}.json"
            target_map_path = REPORT_DIR / f"atlas_job_{base}_{stamp}.targets.json"
            cmd = atlas_blender_command(self.cfg.get("blender_exe", "")) + [
                "--albedo", str(job["albedo"]),
                "--alpha", str(job["alpha"]),
                "--material-name", base,
                "--blend-out", str(job["blend_out"]),
                "--report", str(report_path),
                "--quality", "SPEEDTREE_LOW",
                "--plate-mode", "SINGLE",
            ]
            if job["reuse_existing_blend"]:
                cmd.append("--reuse-existing-blend")
            if push_spm and job["target_spms"]:
                target_map_path.write_text(
                    json.dumps(step2_target_payload(job), indent=2, ensure_ascii=False),
                    encoding="utf-8",
                )
                for spm in job["target_spms"]:
                    cmd += ["--spm", str(spm)]
                cmd += ["--target-map-json", str(target_map_path)]
                cmd.append("--build-spm")
            child_payload = {
                "albedo": str(job["albedo"]),
                "alpha": str(job["alpha"]),
                "material_name": str(base),
                "blend_out": str(job["blend_out"]),
                "spms": [str(path) for path in job.get("target_spms") or ()],
                "target_map_json": (
                    str(target_map_path)
                    if push_spm and job.get("target_spms") else ""
                ),
                "build_spm": bool(push_spm and job.get("target_spms")),
                "reuse_existing_blend": bool(
                    job.get("reuse_existing_blend")
                ),
                "quality": "SPEEDTREE_LOW",
                "plate_mode": "SINGLE",
            }
            child_paths = [
                job["albedo"],
                job["alpha"],
                job["blend_out"],
                *(job.get("target_spms") or ()),
            ]
            if child_payload["target_map_json"]:
                child_paths.append(target_map_path)
            child_authority = capture_runtime_mutation_authority(
                action=f"step2_atlas_child_{index}",
                unit_id="step2-child",
                payload=child_payload,
                paths=child_paths,
                write_paths=[
                    job["blend_out"],
                    *(job.get("target_spms") or ()),
                ],
                memberships=[
                    Path(path).parent / ".atlas_leaf_speedtree_scopes"
                    for path in job.get("target_spms") or ()
                ],
                write_memberships=[
                    Path(path).parent / ".atlas_leaf_speedtree_scopes"
                    for path in job.get("target_spms") or ()
                ],
                config_projection=mutation_config_projection(
                    self.cfg, config_keys
                ),
                tool_paths=[
                    blender_exe,
                    blender_user_startup_path(blender_exe),
                    TOOL_DIR / "jobs" / "atlas_blend_job.py",
                ],
            )
            require_mutation_authority_unit(
                child_authority,
                "step2-child",
                current_payload=child_payload,
                current_config=mutation_config_projection(
                    self.cfg, config_keys
                ),
            )
            child_authority_path = REPORT_DIR / (
                f"atlas_job_{base}_{stamp}.authority.json"
            )
            child_authority_sha256 = write_child_mutation_authority(
                child_authority,
                "step2-child",
                child_authority_path,
            )
            cmd += [
                "--authority-json", str(child_authority_path),
                "--authority-sha256", child_authority_sha256,
            ]
            try:
                result = owned_run(cmd,
                                        source="pcg_st9_texture_batch.pcg_texture_gui.atlas_job",
                                        run_factory=subprocess.run,
                                        capture_output=True, text=True,
                                        encoding="utf-8", errors="replace",
                                        timeout=self.cfg.get("atlas_job_timeout", 1800),
                                        creationflags=0x08000000)
                data = json.loads(report_path.read_text(encoding="utf-8")) if report_path.exists() else {}
                if result.returncode != 0:
                    raise RuntimeError(data.get("error") or (result.stderr or result.stdout)[-400:])
                validate_step2_job_report(
                    data,
                    require_generator_connections=bool(
                        push_spm and job["target_spms"]),
                )
                register_blend_source_index(
                    data.get("blend_source_index"),
                    job["blend_out"],
                )
                save_spm_analysis_cache()
                complete_mutation_authority_unit(
                    child_authority,
                    "step2-child",
                    post_paths=[
                        job["blend_out"],
                        *(job.get("target_spms") or ()),
                    ],
                )
                complete_exact_mutation_unit(
                    baseline,
                    unit_id,
                    post_paths=[
                        job["blend_out"],
                        *(job.get("target_spms") or ()),
                    ],
                )
                done += 1
                spm_note = (
                    " + 최종 SK Generator 연결 완료"
                    if data.get("generator_connections_complete") else "")
                reuse_note = " (기존 blend 보존·재사용)" if job["reuse_existing_blend"] else ""
                self._ui(lambda b=base, d=data, s=spm_note, r=reuse_note: self.log(
                    f"[② 완료] {b}.blend — 잎 메시 {d.get('meshes', '?')}개{s}{r}"))
                if data.get("spm_backups"):
                    self._ui(lambda b=base, paths=data["spm_backups"]: self.log(
                        f"[② SPM 백업] {b}: {len(paths)}개 → {Path(paths[0]).parent}"))
            except Exception as exc:
                failed += 1
                self._ui(lambda b=base, e=exc: self.log(f"[② 실패] {b}: {e}"))
        self._ui(lambda: self._batch_finished("②", done, failed))
        return {
            "shared_queue_success": failed == 0,
            "shared_queue_result": {
                "operation": "step2_atlas",
                "completed": done,
                "failed": failed,
            },
        }

    # ------------------------------------------------------------- ③ 실행
    @staticmethod
    def _step3_rows_share_owner(selected_row, owner_row):
        """Return whether an explicit shared row belongs to this owner row."""
        selected_base = str(
            selected_row.get("atlas_base") or ""
        ).strip().casefold()
        owner_base = str(owner_row.get("atlas_base") or "").strip().casefold()
        if not selected_base or selected_base != owner_base:
            return False
        selected_signature = tuple(
            str(value).casefold()
            for value in selected_row.get("source_signature") or ()
        )
        owner_signature = tuple(
            str(value).casefold()
            for value in owner_row.get("source_signature") or ()
        )
        return (
            not selected_signature
            or not owner_signature
            or selected_signature == owner_signature
        )

    def _step3_shared_owner_candidates(self, selected_row):
        """Resolve a shared row's real render owner even when it is unchecked."""
        owner_name = str(selected_row.get("shared_from") or "").strip()
        if not owner_name:
            return []
        candidates = []
        for entry in self.items.values():
            owner_item = entry.get("item") or {}
            if str(owner_item.get("name") or "").casefold() != owner_name.casefold():
                continue
            for owner_row in self._texplan_rows(owner_item):
                if owner_row.get("shared_from"):
                    continue
                if self._step3_rows_share_owner(selected_row, owner_row):
                    candidates.append((owner_item, owner_row))
        return candidates

    def _step3_jobs(self, force_rerender=False, all_rows=False):
        jobs, skipped = [], []
        self._step3_render_required_row_keys = set()
        self._step3_existing_output_row_keys = set()
        self._step3_effective_output_rows = {}
        scoped_rows = list(
            self._all_texplan_rows()
            if all_rows else self._checked_texplan_rows()
        )
        execution_groups = {}
        for item, row in scoped_rows:
            base = row["atlas_base"]
            if row.get("shared_from"):
                owners = self._step3_shared_owner_candidates(row)
                if len(owners) != 1:
                    owner_name = row.get("shared_from") or "unknown"
                    reason = (
                        f"shared texture render owner not found: {owner_name}"
                        if not owners else
                        f"shared texture render owner is ambiguous: {owner_name}"
                    )
                    skipped.append((item, base, reason))
                    continue
                owner_item, owner_row = owners[0]
            else:
                owner_item, owner_row = item, row
            owner_key = step3_texture_row_key(
                owner_item, owner_row.get("atlas_base")
            )
            group = execution_groups.setdefault(owner_key, {
                "owner_item": owner_item,
                "owner_row": owner_row,
                "consumers": [],
            })
            consumer_key = step3_texture_row_key(item, base)
            if all(
                current_key != consumer_key
                for _current_item, _current_row, current_key
                in group["consumers"]
            ):
                group["consumers"].append((item, row, consumer_key))

        for group in execution_groups.values():
            owner_item = group["owner_item"]
            owner_row = group["owner_row"]
            base = owner_row["atlas_base"]
            consumer_keys = {
                consumer_key
                for _item, _row, consumer_key in group["consumers"]
            }
            for consumer_key in consumer_keys:
                self._step3_effective_output_rows[consumer_key] = (
                    owner_item,
                    owner_row,
                )
            try:
                job = build_texture_job(owner_row)
                job["item"] = owner_item
                job["step3_consumer_row_keys"] = tuple(
                    sorted(consumer_keys)
                )
                job["size_log2"] = expected_job_size(job)
                expected_pixels = sbs_auto.size_log2_pixels(job["size_log2"])
                graph_needs_update = False
                if job.get("mode") == "render":
                    graph_needs_update = sbs_auto.managed_graph_resolution_state(
                        job["sbs"], job["graph"])["needs_update"]
                source_needs_repair = job_needs_source_repair(job)
                structural_needs_update = bool(
                    job.get("mode") == "insert"
                    or job.get("promote_authoring")
                    or job.get("normalize_cluster")
                    or job.get("rename_graph_to")
                    or job.get("repair_resources")
                )
                canonical_row = dict(owner_row)
                canonical_row["texture_dir"] = job["out_dir"]
                complete = complete_output_set(
                    canonical_row, expected_pixels=expected_pixels)
                freshness = (
                    step3_existing_output_freshness(job)
                    if complete else {
                        "fresh": False,
                        "reason": "output_set_incomplete",
                    }
                )
                job["existing_output_freshness"] = freshness
                render_required = bool(
                    force_rerender
                    or not complete
                    or graph_needs_update
                    or source_needs_repair
                    or structural_needs_update
                    or not freshness["fresh"]
                )
                if force_rerender:
                    job["force_cluster_recook"] = True
                if not render_required:
                    self._step3_existing_output_row_keys.update(consumer_keys)
                    continue
                self._step3_render_required_row_keys.update(consumer_keys)
                jobs.append(job)
            except Exception as exc:
                for consumer_item, consumer_row, _key in group["consumers"]:
                    skipped.append((
                        consumer_item,
                        consumer_row["atlas_base"],
                        str(exc),
                    ))
        already_reported = {item["folder"] for item, _base, _reason in skipped}
        for entry in self.items.values():
            item = entry["item"]
            folder = item["folder"]
            in_scope = all_rows or entry["checked"]
            if in_scope and folder in self.texplan_errors \
                    and folder not in already_reported:
                skipped.append((
                    item, "텍스처 계획",
                    self.texplan_errors[folder],
                ))
        return jobs, skipped

    def _step3_sync_files(self, excluded_row_keys=None):
        """Collect only complete sets proven current by the preceding preflight."""
        current_existing = getattr(
            self, "_step3_existing_output_row_keys", None
        )
        files = []
        seen = set()
        seen_manifests = set()
        manifest_rows = []
        for item, row in self._checked_texplan_rows():
            row_key = step3_texture_row_key(
                item, row.get("atlas_base")
            )
            if self._step3_row_is_excluded(
                    item, row.get("atlas_base"), excluded_row_keys):
                continue
            # start_step3 always calls _step3_jobs first.  Only rows that
            # passed that live SBS/input freshness check may turn pre-existing
            # files into a canonical manifest.  ``None`` keeps direct helper
            # calls backward compatible while the real GUI remains fail-safe.
            if current_existing is not None and row_key not in current_existing:
                continue
            _output_item, output_row = getattr(
                self, "_step3_effective_output_rows", {}
            ).get(row_key, (item, row))
            texture_dir = (
                canonical_texture_root(output_row["folder"])
                if output_row.get("folder")
                else output_row.get("texture_dir")
            )
            texture_base = (
                output_row.get("texture_base")
                or output_row.get("atlas_base")
            )
            if not texture_dir or not texture_base:
                continue
            paths = output_paths(texture_dir, texture_base)
            selected = [paths[role] for role in sbs_auto.RENDER_MAPS]
            canonical_row = dict(output_row)
            canonical_row["texture_dir"] = str(texture_dir)
            if not complete_output_set(canonical_row):
                continue
            manifest_key = step3_texture_row_key(
                output_row,
                output_row.get("atlas_base"),
            )
            if manifest_key not in seen_manifests:
                seen_manifests.add(manifest_key)
                manifest_rows.append((
                    output_row,
                    [str(path) for path in selected],
                ))
            for path in selected:
                key = os.path.normcase(os.path.abspath(str(path)))
                if key not in seen:
                    seen.add(key)
                    files.append(str(path))
        self._pending_step3_manifest_rows = manifest_rows
        return files

    @staticmethod
    def _step3_exclusion_keys(skipped):
        return {
            step3_texture_row_key(
                item,
                None if base == "텍스처 계획" else base,
            )
            for item, base, _reason in skipped
        }

    @staticmethod
    def _step3_key_is_excluded(key, excluded_row_keys):
        folder_wildcard = (key[0], "")
        excluded = set(excluded_row_keys or ())
        return key in excluded or folder_wildcard in excluded

    @classmethod
    def _step3_row_is_excluded(
            cls, item, atlas_base, excluded_row_keys):
        return cls._step3_key_is_excluded(
            step3_texture_row_key(item, atlas_base),
            excluded_row_keys,
        )

    @classmethod
    def _step3_jobs_excluding(cls, jobs, excluded_row_keys):
        """Drop excluded consumers without discarding a shared valid owner."""
        result = []
        for job in jobs:
            consumer_keys = tuple(
                job.get("step3_consumer_row_keys") or (
                    step3_texture_row_key(
                        job.get("item") or {},
                        job.get("base"),
                    ),
                )
            )
            allowed_keys = tuple(
                key for key in consumer_keys
                if not cls._step3_key_is_excluded(
                    key, excluded_row_keys
                )
            )
            if not allowed_keys:
                continue
            job["step3_consumer_row_keys"] = allowed_keys
            result.append(job)
        return result

    def _step3_eligible_row_keys(self, excluded_row_keys):
        return {
            step3_texture_row_key(item, row.get("atlas_base"))
            for item, row in self._checked_texplan_rows()
            if not self._step3_row_is_excluded(
                item,
                row.get("atlas_base"),
                excluded_row_keys,
            )
        }

    @staticmethod
    def _step3_exclusion_message(
            skipped, render_count, sync_file_count, *, force=False):
        operation = "③ 전체 재추출" if force else "③ 실행"
        lines = [
            f"오류 항목 {len(skipped)}개를 제외하고 "
            f"나머지 {operation} 대상을 처리할 수 있습니다.",
            "",
            f"정상 렌더 대상: {render_count}세트",
        ]
        if not force:
            lines.append(
                f"완성 출력·Unreal 동기화 후보: {sync_file_count}장"
            )
        lines.extend([
            "",
            "제외 항목은 렌더·manifest·Unreal 동기화·SPM 정리에서 "
            "빠지며 로그와 최종 결과에 제외 건수로 남습니다.",
            "",
        ])
        for item, base, reason in skipped[:8]:
            lines.append(f"· {item['name']} / {base}: {reason}")
        if len(skipped) > 8:
            lines.append(f"· ... 외 {len(skipped) - 8}개")
        lines.extend(["", "오류 항목을 제외하고 계속 실행하시겠습니까?"])
        return "\n".join(lines)

    def _new_step3_run_report(
            self, jobs, skipped, affected_spms, sync_files,
            *, force_unreal_verify=False, selected_rows=None):
        """Create and persist the immutable Step 3 preflight boundary."""
        stamp = datetime.now().strftime("%Y%m%dT%H%M%S_%f")
        report_path = (
            Path(REPORT_DIR)
            / f"step3_run_{stamp}_{os.getpid()}.json"
        )
        selected = []
        for item, row in (
                self._checked_texplan_rows()
                if selected_rows is None else selected_rows):
            selected.append({
                "item": item.get("name") or Path(item["folder"]).name,
                "row_key": step3_row_key_json(step3_texture_row_key(
                    item, row.get("atlas_base")
                )),
                "texture_base": (
                    row.get("texture_base") or row.get("atlas_base")
                ),
                "shared_from": row.get("shared_from"),
            })
        excluded = [{
            "item": item.get("name") or Path(item["folder"]).name,
            "row_key": step3_row_key_json(step3_texture_row_key(
                item,
                None if base == "텍스처 계획" else base,
            )),
            "display_base": base,
            "reason": reason,
            "scopes": [
                "render",
                "existing_output",
                "canonical_manifest",
                "spm_normalization",
                "unreal_sync",
            ],
        } for item, base, reason in skipped]
        render_rows = []
        for job in jobs:
            render_rows.append({
                "owner": step3_row_key_json(step3_texture_row_key(
                    job.get("item") or {}, job.get("base")
                )),
                "consumer_row_keys": [
                    step3_row_key_json(key)
                    for key in job.get("step3_consumer_row_keys") or ()
                ],
                "texture_base": job.get("texture_base"),
                "mode": job.get("mode"),
                "status": "planned",
                "files": [],
            })
        existing_reuse = [{
            "owner": step3_row_key_json(step3_texture_row_key(
                row, row.get("atlas_base")
            )),
            "texture_base": (
                row.get("texture_base") or row.get("atlas_base")
            ),
            "files": [str(path) for path in files],
            "manifest_status": "planned",
        } for row, files in (
            getattr(self, "_pending_step3_manifest_rows", []) or []
        )]
        payload = {
            "kind": "pcg_step3_run_report",
            "schema_version": 1,
            "run_id": report_path.stem,
            "started_at": datetime.now().astimezone().isoformat(),
            "finished_at": None,
            "status": "queued",
            "preflight": {
                "selected": selected,
                "excluded": excluded,
                "render_jobs": [
                    {
                        **row,
                        "consumer_row_keys": list(
                            row["consumer_row_keys"]
                        ),
                        "files": list(row["files"]),
                    }
                    for row in render_rows
                ],
                "existing_reuse": existing_reuse,
                "spms": [str(path) for path in affected_spms],
                "sync_candidates": [str(path) for path in sync_files],
                "force_unreal_verify": bool(force_unreal_verify),
            },
            "render": render_rows,
            "normalization": {"status": "not_run"},
            "unreal_sync": {"status": "not_run"},
            "failures": [],
            "counts": {
                "selected": len(selected),
                "excluded": len(excluded),
                "render_planned": len(render_rows),
                "render_succeeded": 0,
                "render_failed": 0,
                "existing_reused": len(existing_reuse),
                "manifests_recorded": 0,
                "spms_normalized": 0,
                "spms_skipped": 0,
                "materials_skipped": 0,
                "sync_candidates": len(sync_files),
                "sync_latest": 0,
                "sync_changed": 0,
                "sync_failed": 0,
            },
        }
        write_step3_run_report(report_path, payload)
        self.log(f"[③ 실행 리포트] {report_path}")
        return str(report_path), payload

    def _save_step3_run_report(
            self, report_path, payload, *, stage):
        if not report_path or payload is None:
            return False
        try:
            write_step3_run_report(report_path, payload)
            return True
        except Exception as exc:
            self._ui(lambda e=exc, s=stage: self.log(
                f"[③ 실행 리포트 기록 실패] {s}: {e}"
            ))
            return False

    def _begin_step3_run_report(self, *args, **kwargs):
        try:
            return self._new_step3_run_report(*args, **kwargs)
        except Exception as exc:
            self.log(f"[③ 실행 리포트 생성 실패] {exc}")
            return None, None

    def _sync_pending_texture_files(
            self, files, progress=None, force_verify=False):
        """Skip receipt-current files before expensive hashing/Unreal RPC."""
        state = load_sync_state(migrate=False)
        destination = self.cfg.get(
            "unreal_texture_destination", "/Game/Textures"
        )
        pending = []
        receipt_current = 0
        if force_verify:
            pending.extend(files)
        else:
            for path in files:
                if is_texture_synced(
                    path, state=state, destination=destination
                ):
                    receipt_current += 1
                else:
                    pending.append(path)
        if pending:
            report = sync_texture_files(
                pending, cfg=self.cfg, progress=progress
            )
        else:
            report = {
                "mode": "receipt_cache",
                "entries": [],
                "counts": {},
                "errors": [],
            }
        report["receipt_current"] = receipt_current
        report["pending_count"] = len(pending)
        report["forced_verify_count"] = len(files) if force_verify else 0
        return report

    @staticmethod
    def _step3_unreal_name_errors(jobs, sync_files):
        """Validate existing and planned output names before local mutation."""
        candidates = [Path(path) for path in sync_files]
        for job in jobs:
            candidates.extend(
                output_paths(job["out_dir"], job["texture_base"]).values()
            )
        errors = []
        seen = set()
        for path in candidates:
            asset_name = Path(path).stem
            if asset_name in seen:
                continue
            seen.add(asset_name)
            try:
                validate_unreal_texture_name(asset_name)
            except ValueError as exc:
                errors.append((asset_name, str(exc)))
        return errors

    def _step3_unreal_name_skips(
            self, jobs, sync_files, *, selected_rows=None):
        """Turn invalid Unreal output names into row-scoped exclusions."""
        if not self._step3_unreal_name_errors(jobs, sync_files):
            return []
        scoped_rows = list(
            self._checked_texplan_rows()
            if selected_rows is None else selected_rows
        )
        rows_by_key = {
            step3_texture_row_key(item, row.get("atlas_base")): (item, row)
            for item, row in scoped_rows
        }
        skips = []
        seen = set()

        def add_skips(row_keys, fallback_item, fallback_base, errors):
            reason = "Unreal 출력 이름 오류: " + "; ".join(
                f"{name}: {message}" for name, message in errors
            )
            keys = tuple(row_keys) or (
                step3_texture_row_key(fallback_item, fallback_base),
            )
            for key in keys:
                item, row = rows_by_key.get(
                    key,
                    (
                        fallback_item,
                        {"atlas_base": fallback_base},
                    ),
                )
                identity = (key, reason)
                if identity in seen:
                    continue
                seen.add(identity)
                skips.append((
                    item,
                    row.get("atlas_base") or fallback_base,
                    reason,
                ))

        for job in jobs:
            errors = self._step3_unreal_name_errors([job], [])
            if not errors:
                continue
            add_skips(
                job.get("step3_consumer_row_keys") or (),
                job.get("item") or {},
                job.get("base"),
                errors,
            )

        sync_keys = {
            os.path.normcase(os.path.abspath(str(path)))
            for path in sync_files or []
        }
        effective_rows = getattr(
            self, "_step3_effective_output_rows", {}
        )
        for output_row, files in (
                getattr(self, "_pending_step3_manifest_rows", []) or []):
            selected_files = [
                path for path in files
                if os.path.normcase(os.path.abspath(str(path))) in sync_keys
            ]
            errors = self._step3_unreal_name_errors([], selected_files)
            if not errors:
                continue
            output_key = step3_texture_row_key(
                output_row,
                output_row.get("atlas_base"),
            )
            consumer_keys = [
                consumer_key
                for consumer_key, (_item, effective_row)
                in effective_rows.items()
                if step3_texture_row_key(
                    effective_row,
                    effective_row.get("atlas_base"),
                ) == output_key
            ]
            add_skips(
                consumer_keys or (output_key,),
                output_row,
                output_row.get("atlas_base"),
                errors,
            )
        return skips

    def _build_step3_force_execution_plan(self):
        """Build the force-rerender plan after the shared FIFO grants it."""

        self._pending_step3_manifest_rows = []
        selected_rows = self._all_texplan_rows()
        jobs, skipped = self._step3_jobs(force_rerender=True, all_rows=True)
        excluded_row_keys = self._step3_exclusion_keys(skipped)
        if excluded_row_keys:
            jobs = self._step3_jobs_excluding(
                jobs,
                excluded_row_keys,
            )
        unreal_name_skips = self._step3_unreal_name_skips(
            jobs,
            [],
            selected_rows=selected_rows,
        )
        if unreal_name_skips:
            skipped.extend(unreal_name_skips)
            excluded_row_keys = self._step3_exclusion_keys(skipped)
            jobs = self._step3_jobs_excluding(
                jobs,
                excluded_row_keys,
            )
        eligible_rows = [
            (item, row)
            for item, row in selected_rows
            if not self._step3_row_is_excluded(
                item,
                row.get("atlas_base"),
                excluded_row_keys,
            )
        ]
        eligible_row_keys = {
            step3_texture_row_key(
                item,
                row.get("atlas_base"),
            )
            for item, row in eligible_rows
        }
        exact_step3_spms = checked_step3_spms(
            self.items,
            allowed_folders={
                key[0] for key in eligible_row_keys
            },
            selected_texture_rows=eligible_rows,
            include_unchecked=True,
        )
        return {
            "jobs": jobs,
            "skipped": skipped,
            "sync_files": [],
            "force_unreal_verify": False,
            "eligible_row_keys": eligible_row_keys,
            "exact_step3_spms": exact_step3_spms,
            "selected_rows": selected_rows,
            "normalization_plan": build_texture_plan_from_report(
                getattr(self, "report", None) or {"items": []},
                "<step3-force-current-board>",
            ),
            "scope_folders": sorted({
                str(item.get("folder"))
                for item, _row in selected_rows
                if str(item.get("folder") or "").strip()
            }, key=os.path.normcase),
            "require_all_renders_for_sync": True,
            "operation_label": "PCG ③ 전체 재추출",
        }

    def start_step3_force(self):
        if not self.report:
            self.refresh()
            self.status_var.set("검사가 끝난 뒤 전체 재추출을 다시 실행하세요.")
            return
        if getattr(self, "_busy", False):
            self.status_var.set(
                "현재 작업이 끝난 뒤 전체 재추출을 다시 누르세요."
            )
            return
        if not messagebox.askyesno(
            "③ 전체 다시 뽑기",
            (
                "현재 표의 연결 텍스처를 실행 차례에 다시 검사한 뒤 "
                "T_ 6장을 전부 재추출합니다.\n"
                "오류 항목은 제외하고 정상 항목만 계속하며, 모든 렌더가 "
                "성공하면 Unreal 동기화를 한 번 실행합니다.\n"
                "성공한 세트는 루트 SK와 canonical Cluster SK까지 SPM 연결을 정규화합니다.\n\n"
                "공용 대기열에 등록할까요?"
            ),
        ):
            self.status_var.set("대기")
            return
        try:
            shared = self._enqueue_shared_execution(
                "PCG ③ 전체 재추출",
                {
                    "operation": "step3_force",
                    "force_rerender": True,
                },
            )
        except Exception as exc:
            self._shared_execution_enqueue_failed(exc)
            return
        self._set_busy(True)
        self.status_var.set(
            "③ 전체 재추출 공용 대기열 등록 · 실행 차례에 재검사"
        )
        self.root.update_idletasks()
        self.worker = threading.Thread(
            target=self._run_step3_force_planning,
            args=(shared,),
            daemon=True,
        )
        self.worker.start()

    def _run_step3_force_planning(self, shared_record):
        plan = None
        error = None
        lease = None
        try:
            lease = self._wait_for_shared_execution(shared_record)
            self._ui(
                lambda: self.status_var.set(
                    "③ 전체 재추출 차례 도착 · 최신 대상 계획 계산 중..."
                )
            )
            plan = self._build_step3_force_execution_plan()
            self._seal_step3_force_execution_plan(plan)
        except Exception as exc:
            error = exc
        self._ui(
            lambda result=plan, failure=error, claimed=lease:
            self._step3_planning_done(result, failure, claimed)
        )

    def _seal_step3_force_execution_plan(self, plan):
        """Seal only the concrete render inputs for a manual full rerender.

        The normal Step 3 path re-audits every affected folder and requires
        the newly derived semantic report to be byte-for-byte equivalent to
        the report currently displayed by the GUI.  That is appropriate for
        SPM normalization, but it made the explicit "전체 다시 뽑기" action
        fail before sbsrender was launched whenever unrelated audit evidence
        changed.  A manual full rerender is already rebuilt from the current
        board after its shared-queue turn, so seal its concrete SBS/bitmap,
        output, configuration, and tool inputs directly without the redundant
        affected-folder semantic gate.
        """
        plan["pending_manifest_rows"] = list(
            (plan or {}).get("pending_manifest_rows")
            or getattr(self, "_pending_step3_manifest_rows", [])
            or []
        )
        baseline = seal_exact_mutation_baseline(
            [],
            action="step3_texture_force",
            plan_payload=step3_exact_plan_payload(plan),
            authority_units=step3_authority_units(plan),
            config_projection=mutation_config_projection(
                self.cfg,
                (
                    "designer_dir",
                    "cluster_sbsar",
                    "cluster_sbsar_normal_behavior",
                    "sbsrender_timeout",
                    "tree_root",
                    "unreal_texture_sync_enabled",
                    "unreal_project",
                    "unreal_editor_cmd",
                    "unreal_texture_destination",
                    "unreal_texture_sync_timeout",
                ),
            ),
            tool_paths=[
                Path(self.cfg.get("designer_dir") or "") / "sbsrender.exe",
                Path(self.cfg.get("designer_dir") or "") / "sbscooker.exe",
                self.cfg.get("cluster_sbsar"),
                self.cfg.get("unreal_editor_cmd"),
                mutation_source_path(run_texture_job),
                mutation_source_path(normalize_spms_transactionally),
                mutation_source_path(sync_texture_files),
            ],
            receipt_path=(
                REPORT_DIR
                / (
                    "mutation_authority_step3_texture_force_"
                    + datetime.now().strftime("%Y%m%d_%H%M%S_%f")
                    + ".json"
                )
            ),
        )
        plan["_exact_mutation_baseline"] = baseline
        return baseline

    def _step3_scope_folders(self):
        """Return checked folders plus only their explicitly shared owners."""
        selected = [
            entry.get("item")
            for entry in getattr(self, "items", {}).values()
            if entry.get("checked") and isinstance(entry.get("item"), dict)
        ]
        if not selected:
            raise RuntimeError("③ 실행할 폴더를 하나 이상 체크하세요")

        by_name = {
            str((entry.get("item") or {}).get("name") or "").casefold():
            entry.get("item")
            for entry in getattr(self, "items", {}).values()
            if isinstance(entry.get("item"), dict)
        }
        scoped = list(selected)
        seen_ids = {id(item) for item in scoped}
        for item in list(selected):
            for row in item.get("cluster_items") or ():
                owner_name = str(row.get("shared_from") or "").casefold()
                owner = by_name.get(owner_name)
                if owner is not None and id(owner) not in seen_ids:
                    seen_ids.add(id(owner))
                    scoped.append(owner)
        return sorted(
            {
                str(item.get("folder"))
                for item in scoped
                if str(item.get("folder") or "").strip()
            },
            key=os.path.normcase,
        )

    def _apply_step3_scope_report(self, report):
        """Replace only audited rows while preserving the rest of the board."""
        if not hasattr(self, "texplan_cache"):
            self.texplan_cache = {}
        if not hasattr(self, "texplan_errors"):
            self.texplan_errors = {}
        live_by_key = {
            startup_path_key(item.get("folder") or ""): item
            for item in (report or {}).get("items") or ()
        }
        for iid, entry in getattr(self, "items", {}).items():
            live = live_by_key.get(startup_path_key(iid))
            if live is not None:
                entry["item"] = live
                self.texplan_cache.pop(iid, None)
                self.texplan_errors.pop(iid, None)

        current = dict(getattr(self, "report", {}) or {})
        current_items = list(current.get("items") or ())
        replaced = set()
        for index, item in enumerate(current_items):
            key = startup_path_key(item.get("folder") or "")
            live = live_by_key.get(key)
            if live is not None:
                current_items[index] = live
                replaced.add(key)
        current_items.extend(
            item for key, item in live_by_key.items() if key not in replaced
        )
        current["items"] = current_items
        current["generated_at"] = (report or {}).get(
            "generated_at", current.get("generated_at")
        )
        current["inventory_pending"] = any(
            item.get("status") == "startup_pending" for item in current_items
        )
        self.report = current
        self._step3_live_scope_report = report
        return report

    def _refresh_step3_execution_scope(self):
        """Audit only the checked Step 3 scope after its queue turn arrives."""
        scope_folders = self._step3_scope_folders()
        pcg_targets = (
            load_pcg_targets()
            if bool(getattr(
                self, "_active_step3_use_pcg_targets", False
            )) else None
        )
        session_evidence = {}
        report = make_report(
            self.cfg,
            targets=scope_folders,
            pcg_targets=pcg_targets,
            mutation_authority=True,
            session_evidence=session_evidence,
        )
        cache_blender_connection_rows(
            report,
            verify_physical=True,
            read_cache=False,
            session_evidence=session_evidence,
        )
        self._apply_step3_scope_report(report)
        return scope_folders

    def _validate_step3_plan_live_evidence(self, plan):
        plan["pending_manifest_rows"] = list(
            (plan or {}).get("pending_manifest_rows")
            or getattr(self, "_pending_step3_manifest_rows", [])
            or []
        )
        selected_rows = list((plan or {}).get("selected_rows") or ())
        items = [
            row[0]
            for row in selected_rows
            if isinstance(row, (list, tuple)) and row
            and isinstance(row[0], dict)
        ]
        if not items:
            items = [
                item
                for job in (plan or {}).get("jobs") or ()
                for item in job.get("items") or [job.get("item")]
                if isinstance(item, dict)
            ]
        # The exact selected scope was audited immediately before this plan was
        # built.  Seal those rows directly; calling make_report again here was
        # a redundant second audit of the same folders.
        self._validate_live_mutation_items(items)
        baseline = seal_exact_mutation_baseline(
            items,
            action="step3_texture",
            plan_payload=step3_exact_plan_payload(plan),
            authority_units=step3_authority_units(plan),
            config_projection=mutation_config_projection(
                self.cfg,
                (
                    "designer_dir",
                    "cluster_sbsar",
                    "cluster_sbsar_normal_behavior",
                    "sbsrender_timeout",
                    "tree_root",
                    "unreal_texture_sync_enabled",
                    "unreal_project",
                    "unreal_editor_cmd",
                    "unreal_texture_destination",
                    "unreal_texture_sync_timeout",
                ),
            ),
            tool_paths=[
                Path(self.cfg.get("designer_dir") or "") / "sbsrender.exe",
                Path(self.cfg.get("designer_dir") or "") / "sbscooker.exe",
                self.cfg.get("cluster_sbsar"),
                self.cfg.get("unreal_editor_cmd"),
                mutation_source_path(run_texture_job),
                mutation_source_path(normalize_spms_transactionally),
                mutation_source_path(sync_texture_files),
            ],
        )
        plan["_exact_mutation_baseline"] = baseline
        return baseline

    def _build_step3_execution_plan(self):
        """Perform the expensive Step 3 preflight without touching Tk."""

        self._pending_step3_manifest_rows = []
        jobs, skipped = self._step3_jobs()
        excluded_row_keys = self._step3_exclusion_keys(skipped)
        if excluded_row_keys:
            jobs = self._step3_jobs_excluding(
                jobs,
                excluded_row_keys,
            )
        sync_files = (
            self._step3_sync_files(excluded_row_keys)
            if excluded_row_keys
            else self._step3_sync_files()
        )
        unreal_name_skips = self._step3_unreal_name_skips(
            jobs,
            sync_files,
        )
        if unreal_name_skips:
            skipped.extend(unreal_name_skips)
            excluded_row_keys = self._step3_exclusion_keys(skipped)
            jobs = self._step3_jobs_excluding(
                jobs,
                excluded_row_keys,
            )
            sync_files = self._step3_sync_files(excluded_row_keys)
        selection_state = step3_selection_state(getattr(self, "items", {}))
        force_unreal_verify = bool(
            selection_state.get("force_unreal_verify")
        ) and not skipped
        if skipped and not jobs and not sync_files:
            return {
                "jobs": [],
                "skipped": skipped,
                "sync_files": [],
                "force_unreal_verify": False,
                "eligible_row_keys": set(),
                "exact_step3_spms": [],
                "selected_rows": [],
            }
        selected_rows = list(self._checked_texplan_rows())
        eligible_row_keys = (
            self._step3_eligible_row_keys(excluded_row_keys)
            if excluded_row_keys
            else None
        )
        normalization_rows = selected_rows
        if eligible_row_keys is not None:
            normalization_rows = [
                (item, row)
                for item, row in selected_rows
                if step3_texture_row_key(
                    item,
                    row.get("atlas_base"),
                ) in eligible_row_keys
            ]
        exact_step3_spms = checked_step3_spms(
            self.items,
            allowed_folders=(
                {key[0] for key in eligible_row_keys}
                if eligible_row_keys is not None
                else None
            ),
            selected_texture_rows=normalization_rows,
        )
        live_scope_report = getattr(self, "_step3_live_scope_report", None)
        normalization_plan = (
            build_texture_plan_from_report(
                live_scope_report,
                "<step3-selected-scope>",
            )
            if live_scope_report is not None else {}
        )
        return {
            "jobs": jobs,
            "skipped": skipped,
            "sync_files": sync_files,
            "force_unreal_verify": force_unreal_verify,
            "eligible_row_keys": eligible_row_keys,
            "exact_step3_spms": exact_step3_spms,
            "selected_rows": selected_rows,
            "normalization_plan": normalization_plan,
            "scope_folders": list(
                getattr(self, "_active_step3_scope_folders", ())
            ),
        }

    def _run_step3_planning(self, shared_record=None):
        plan = None
        error = None
        lease = None
        try:
            lease = self._wait_for_shared_execution(shared_record)
            if lease is not None:
                self._ui(
                    lambda: self.status_var.set(
                        "③ 실행 차례 도착 · 최신 대상 계획 계산 중..."
                    )
                )
            self._active_step3_scope_folders = (
                self._refresh_step3_execution_scope()
            )
            plan = self._build_step3_execution_plan()
            self._validate_step3_plan_live_evidence(plan)
        except Exception as exc:
            error = exc
        self._ui(
            lambda result=plan, failure=error, claimed=lease:
            self._step3_planning_done(result, failure, claimed)
        )

    def start_step3(self):
        if not self.report:
            self.refresh()
            self.status_var.set("검사가 끝난 뒤 ③을 다시 실행하세요.")
            return
        if getattr(self, "_busy", False):
            self.status_var.set("현재 작업이 끝난 뒤 ③ 실행을 다시 누르세요.")
            return
        if not any(
            entry.get("checked")
            for entry in getattr(self, "items", {}).values()
        ):
            self.status_var.set("③ 실행할 폴더를 하나 이상 체크하세요")
            return
        if (
            not getattr(self, "_display_only_snapshot", False)
            and not self._require_live_mutation_items()
        ):
            return
        # Tk variables must only be read on the UI thread.  The queue worker
        # consumes this immutable snapshot when its execution turn arrives.
        use_pcg_targets_var = getattr(self, "use_pcg_targets_var", None)
        self._active_step3_use_pcg_targets = bool(
            use_pcg_targets_var.get()
        ) if use_pcg_targets_var is not None else False
        try:
            shared = self._enqueue_shared_execution(
                "PCG ③ 텍스처 생성/정규화/Unreal 동기화",
                {
                    "operation": "step3",
                    "selected_folders": [
                        str(folder)
                        for folder, entry in self.items.items()
                        if entry.get("checked")
                    ],
                },
            )
        except Exception as exc:
            self._shared_execution_enqueue_failed(exc)
            return
        self._set_busy(True)
        self.status_var.set(
            "③ 공용 대기열 등록 · 실행 차례에 최신 계획을 계산합니다."
        )
        self.root.update_idletasks()
        self.worker = threading.Thread(
            target=self._run_step3_planning,
            args=(shared,),
            daemon=True,
        )
        self.worker.start()

    def _step3_planning_done(self, plan, error=None, shared_lease=None):
        """Launch the prepared Step 3 worker on the Tk thread."""

        self.worker = None
        if error is not None:
            if shared_lease is not None and not shared_lease.finished:
                try:
                    shared_lease.finish(
                        success=False,
                        result={
                            "tool": "pcg_st9_texture_batch",
                            "operation": "step3_planning",
                            "outcome": "failed",
                            "error": str(error),
                        },
                    )
                except Exception as queue_exc:
                    error = RuntimeError(
                        f"{error}; 공용 대기열 종료 기록 실패: {queue_exc}"
                    )
            self._pending_step3_manifest_rows = []
            self._set_busy(False)
            self.status_var.set("③ 대상 계획 실패")
            messagebox.showerror("③ 실행 계획 실패", str(error))
            self.log(f"[③ 실행 계획 실패] {error}")
            return

        exact_mutation_baseline = plan.pop(
            "_exact_mutation_baseline",
            None,
        )
        jobs = plan["jobs"]
        skipped = plan["skipped"]
        sync_files = plan["sync_files"]
        force_unreal_verify = plan["force_unreal_verify"]
        eligible_row_keys = plan["eligible_row_keys"]
        exact_step3_spms = plan["exact_step3_spms"]
        selected_rows = plan["selected_rows"]
        normalization_plan = copy.deepcopy(
            plan.get("normalization_plan") or {}
        )
        self._active_step3_refresh_folders = list(
            plan.get("scope_folders") or ()
        )
        require_all_renders_for_sync = bool(
            plan.get("require_all_renders_for_sync", False)
        )
        operation_label = str(
            plan.get("operation_label")
            or "PCG ③ 텍스처 생성/정규화/Unreal 동기화"
        )

        for item, base, reason in skipped:
            self.log(f"[③ 건너뜀] {item['name']} / {base}: {reason}")
        if skipped and not jobs and not sync_files:
            lines = [
                "오류 항목을 제외하면 ③에서 실행할 정상 대상이 없습니다.",
                "",
            ]
            for item, base, reason in skipped[:8]:
                lines.append(f"· {item['name']} / {base}: {reason}")
            if len(skipped) > 8:
                lines.append(f"· ... 외 {len(skipped) - 8}개")
            messagebox.showerror("③ 실행 불가", "\n".join(lines))
            self.status_var.set(f"③ 실행 차단 · 제외 대상 {len(skipped)}개")
            self._pending_step3_manifest_rows = []
            self._set_busy(False)
            if shared_lease is not None and not shared_lease.finished:
                shared_lease.finish(
                    success=True,
                    result={
                        "tool": "pcg_st9_texture_batch",
                        "operation": "step3",
                        "outcome": "no_eligible_rows",
                        "excluded": len(skipped),
                    },
                )
            return
        if not jobs:
            if not sync_files:
                messagebox.showinfo(
                    "③ 실행",
                    "체크된 항목에서 완성된 TGA 6장 세트나 생성 작업을 찾지 못했습니다.\n"
                    "③ 컬럼의 누락 파일 또는 제외 사유를 확인하세요.",
                )
                self.status_var.set("대기")
                self._pending_step3_manifest_rows = []
                self._set_busy(False)
                if shared_lease is not None and not shared_lease.finished:
                    shared_lease.finish(
                        success=True,
                        result={
                            "tool": "pcg_st9_texture_batch",
                            "operation": "step3",
                            "outcome": "nothing_to_do",
                        },
                    )
                return
            run_report_path, run_report = self._begin_step3_run_report(
                [],
                skipped,
                [] if force_unreal_verify else exact_step3_spms,
                sync_files,
                force_unreal_verify=force_unreal_verify,
                selected_rows=selected_rows,
            )
            exclusion_status = (
                f" · 오류 항목 제외 {len(skipped)}개"
                if skipped else ""
            )
            self.status_var.set(
                f"③ Unreal 동기화 시작 · 대상 {len(sync_files)}장"
                f"{exclusion_status}"
            )
            self.root.update_idletasks()
            worker_kwargs = {}
            if normalization_plan:
                worker_kwargs["normalization_plan"] = normalization_plan
            if exact_mutation_baseline is not None:
                worker_kwargs["exact_mutation_baseline"] = (
                    exact_mutation_baseline
                )
            if plan.get("pending_manifest_rows"):
                worker_kwargs["pending_manifest_rows"] = list(
                    plan["pending_manifest_rows"]
                )
            if skipped and eligible_row_keys is not None:
                worker_kwargs.update({
                    "planned_skipped": len(skipped),
                    "allowed_step3_row_keys": tuple(sorted(
                        eligible_row_keys
                    )),
                })
            if run_report_path:
                worker_kwargs.update({
                    "step3_run_report_path": run_report_path,
                    "step3_run_report": run_report,
                })
            step3_args = (
                [],
                [] if force_unreal_verify else exact_step3_spms,
                sync_files,
                force_unreal_verify,
            )
            if require_all_renders_for_sync:
                step3_args += (True,)
            if shared_lease is None:
                self.worker = threading.Thread(
                    target=self._run_step3,
                    args=step3_args,
                    daemon=True,
                    **({"kwargs": worker_kwargs} if worker_kwargs else {}),
                )
            else:
                self.worker = threading.Thread(
                    target=self._run_claimed_shared_execution,
                    args=(
                        shared_lease,
                        operation_label,
                        self._run_step3,
                        step3_args,
                        worker_kwargs,
                    ),
                    daemon=True,
                )
            self.worker.start()
            return
        run_report_path, run_report = self._begin_step3_run_report(
            jobs,
            skipped,
            exact_step3_spms,
            sync_files,
            force_unreal_verify=force_unreal_verify,
            selected_rows=selected_rows,
        )
        exclusion_status = (
            f" · 오류 항목 제외 {len(skipped)}개" if skipped else ""
        )
        self.status_var.set(
            f"③ 작업 시작 · 렌더 {len(jobs)}세트 · "
            f"Unreal 후보 {len(sync_files)}장{exclusion_status}"
        )
        self.root.update_idletasks()
        # Shared texture owners can render on behalf of another checked row,
        # but only the exact checked/PCG-target SK paths may be normalized.
        # A folder can contain sibling variants that were not selected.
        worker_kwargs = {}
        if normalization_plan:
            worker_kwargs["normalization_plan"] = normalization_plan
        if exact_mutation_baseline is not None:
            worker_kwargs["exact_mutation_baseline"] = (
                exact_mutation_baseline
            )
        if plan.get("pending_manifest_rows"):
            worker_kwargs["pending_manifest_rows"] = list(
                plan["pending_manifest_rows"]
            )
        if skipped:
            worker_kwargs["planned_skipped"] = len(skipped)
        if skipped and eligible_row_keys is not None:
            worker_kwargs.update({
                "allowed_step3_row_keys": tuple(sorted(
                    eligible_row_keys
                )),
            })
        if run_report_path:
            worker_kwargs.update({
                "step3_run_report_path": run_report_path,
                "step3_run_report": run_report,
            })
        step3_args = (
            jobs,
            exact_step3_spms,
            sync_files,
            force_unreal_verify,
        )
        if require_all_renders_for_sync:
            step3_args += (True,)
        if shared_lease is None:
            self.worker = threading.Thread(
                target=self._run_step3,
                args=step3_args,
                daemon=True,
                **({"kwargs": worker_kwargs} if worker_kwargs else {}),
            )
        else:
            self.worker = threading.Thread(
                target=self._run_claimed_shared_execution,
                args=(
                    shared_lease,
                    operation_label,
                    self._run_step3,
                    step3_args,
                    worker_kwargs,
                ),
                daemon=True,
            )
        self.worker.start()

    def _run_step3(
            self, jobs, affected_spms, sync_files=None,
            force_unreal_verify=False, require_all_renders_for_sync=False,
            planned_skipped=0, allowed_step3_row_keys=None,
            step3_run_report_path=None, step3_run_report=None,
            exact_mutation_baseline=None, pending_manifest_rows=None,
            normalization_plan=None):
        done = failed = 0
        sync_summary = {"latest": 0, "changed": 0, "failed": 0}
        sync_report_path = None
        sync_deferred = None
        sync_candidates = []
        normalization_failed_row_keys = set()
        run_report = step3_run_report
        if run_report is not None:
            run_report["status"] = "running"
            self._save_step3_run_report(
                step3_run_report_path,
                run_report,
                stage="worker_start",
            )
        pending_manifest_rows = list(pending_manifest_rows or ())
        self._pending_step3_manifest_rows = []
        if pending_manifest_rows:
            for manifest_index, (row, files) in enumerate(
                    pending_manifest_rows):
                preferred_unit_id = f"step3-manifest:{manifest_index + 1}"
                unit_id = exact_mutation_unit_id(
                    exact_mutation_baseline, preferred_unit_id
                )
                unit_payload = {
                    "row": row,
                    "files": [str(path) for path in files or ()],
                }
                require_exact_mutation_baseline(
                    exact_mutation_baseline,
                    unit_id=unit_id,
                    current_unit_payload=(
                        unit_payload
                        if unit_id == preferred_unit_id else None
                    ),
                )
                try:
                    manifest = record_canonical_output(
                        row,
                        files,
                        producer_source=(
                            f"{row.get('m_graph_sbs') or ''}"
                            f"#{row.get('m_graph') or row.get('texture_base') or ''}"
                        ),
                    )
                    sync_candidates.extend(files)
                    complete_exact_mutation_unit(
                        exact_mutation_baseline,
                        unit_id,
                        post_paths=[manifest],
                    )
                    self._ui(lambda path=manifest: self.log(
                        f"[③ canonical manifest] {path}"
                    ))
                    if run_report is not None:
                        reuse_rows = (
                            run_report.get("preflight", {})
                            .get("existing_reuse", [])
                        )
                        if manifest_index < len(reuse_rows):
                            reuse_rows[manifest_index].update({
                                "manifest_status": "recorded",
                                "manifest": str(manifest),
                            })
                        run_report["counts"]["manifests_recorded"] += 1
                        self._save_step3_run_report(
                            step3_run_report_path,
                            run_report,
                            stage="existing_manifest",
                        )
                except Exception as exc:
                    failed += 1
                    failed_output_key = step3_texture_row_key(
                        row,
                        row.get("atlas_base"),
                    )
                    consumer_keys = [
                        consumer_key
                        for consumer_key, (_item, effective_row)
                        in getattr(
                            self,
                            "_step3_effective_output_rows",
                            {},
                        ).items()
                        if step3_texture_row_key(
                            effective_row,
                            effective_row.get("atlas_base"),
                        ) == failed_output_key
                    ]
                    normalization_failed_row_keys.update(
                        consumer_keys or (failed_output_key,)
                    )
                    if run_report is not None:
                        reuse_rows = (
                            run_report.get("preflight", {})
                            .get("existing_reuse", [])
                        )
                        if manifest_index < len(reuse_rows):
                            reuse_rows[manifest_index].update({
                                "manifest_status": "failed",
                                "error": str(exc),
                            })
                        run_report["failures"].append({
                            "stage": "canonical_manifest",
                            "row": step3_row_key_json(
                                step3_texture_row_key(
                                    row, row.get("atlas_base")
                                )
                            ),
                            "reason": str(exc),
                        })
                        self._save_step3_run_report(
                            step3_run_report_path,
                            run_report,
                            stage="existing_manifest_failed",
                        )
                    self._ui(lambda e=exc: self.log(
                        f"[③ canonical manifest 실패] {e}"
                    ))
        else:
            sync_candidates.extend(sync_files or [])
        total = len(jobs)
        timeout = self.cfg.get("sbsrender_timeout", 1800)
        for index, job in enumerate(jobs, 1):
            base = job["base"]
            texture_base = job["texture_base"]
            render_row = None
            if run_report is not None:
                render_rows = run_report.get("render") or []
                if index - 1 < len(render_rows):
                    render_row = render_rows[index - 1]
                    render_row["status"] = "running"
                    self._save_step3_run_report(
                        step3_run_report_path,
                        run_report,
                        stage="render_start",
                    )
            self._ui(lambda i=index, b=base, t=texture_base: self.status_var.set(
                f"③ {i}/{total}: {b} → {t} 렌더 중..."))
            self._ui(lambda it=job["item"]: self.tree.set(it["folder"], "step3", "렌더 중..."))
            preferred_unit_id = f"step3-render:{index}:{base}"
            unit_id = exact_mutation_unit_id(
                exact_mutation_baseline, preferred_unit_id
            )
            config_keys = (
                "designer_dir",
                "cluster_sbsar",
                "cluster_sbsar_normal_behavior",
                "sbsrender_timeout",
                "tree_root",
                "unreal_texture_sync_enabled",
                "unreal_project",
                "unreal_editor_cmd",
                "unreal_texture_destination",
                "unreal_texture_sync_timeout",
            )
            require_exact_mutation_baseline(
                exact_mutation_baseline,
                unit_id=unit_id,
                current_unit_payload=(
                    step3_job_payload(job)
                    if unit_id == preferred_unit_id else None
                ),
                current_config=(
                    mutation_config_projection(self.cfg, config_keys)
                    if unit_id == preferred_unit_id else None
                ),
            )
            try:
                result = run_texture_job(job, self.cfg, timeout)
                sync_candidates.extend(result.get("files") or [])
                complete_exact_mutation_unit(
                    exact_mutation_baseline,
                    unit_id,
                    post_paths=[
                        job.get("sbs"),
                        *(result.get("files") or ()),
                    ],
                )
                done += 1
                if render_row is not None:
                    render_row.update({
                        "status": "succeeded",
                        "files": [
                            str(path)
                            for path in result.get("files") or []
                        ],
                        "canonical_manifest": (
                            str(result["canonical_manifest"])
                            if result.get("canonical_manifest") else None
                        ),
                    })
                    run_report["counts"]["render_succeeded"] += 1
                    self._save_step3_run_report(
                        step3_run_report_path,
                        run_report,
                        stage="render_succeeded",
                    )
                self._ui(lambda b=base, r=result: self.log(
                    f"[③ 완료] {b} → {r['texture_base']} — "
                    f"{len(r['files'])}장 / {Path(r['files'][0]).parent}"))
                continue
            except Exception as exc:
                failed += 1
                normalization_failed_row_keys.update(
                    tuple(key)
                    for key in job.get("step3_consumer_row_keys") or (
                        step3_texture_row_key(
                            job.get("item") or {},
                            job.get("base"),
                        ),
                    )
                )
                if render_row is not None:
                    render_row.update({
                        "status": "failed",
                        "error": str(exc),
                    })
                    run_report["counts"]["render_failed"] += 1
                    run_report["failures"].append({
                        "stage": "render",
                        "owner": render_row.get("owner"),
                        "reason": str(exc),
                    })
                    self._save_step3_run_report(
                        step3_run_report_path,
                        run_report,
                        stage="render_failed",
                    )
                self._ui(lambda b=base, e=exc: self.log(f"[③ 실패] {b}: {e}"))
        if run_report is not None:
            if affected_spms:
                run_report["normalization"] = {
                    "status": (
                        "running_partial"
                        if normalization_failed_row_keys
                        else "running"
                    ),
                    "targets": [str(path) for path in affected_spms],
                    "excluded_failed_rows": [
                        step3_row_key_json(key)
                        for key in sorted(
                            normalization_failed_row_keys
                        )
                    ],
                }
                self._save_step3_run_report(
                    step3_run_report_path,
                    run_report,
                    stage="normalization_start",
                )
            else:
                run_report["normalization"] = {
                    "status": "no_targets",
                    "targets": [],
                }
        if affected_spms:
            try:
                self._ui(lambda: self.status_var.set("③ SK SPM 머티리얼 슬롯 정리 중..."))
                affected_keys = {
                    os.path.normcase(os.path.abspath(str(path)))
                    for path in affected_spms
                }
                plan = copy.deepcopy(normalization_plan or {})
                if not plan:
                    raise RuntimeError(
                        "선택 범위 normalization plan이 없습니다; "
                        "전체 폴더를 다시 검사하지 않고 중단합니다"
                    )
                exact_plan = dict(plan)
                allowed_rows = (
                    {
                        tuple(key) for key in allowed_step3_row_keys
                    }
                    if allowed_step3_row_keys is not None
                    else None
                )
                if (
                    allowed_rows is not None
                    or normalization_failed_row_keys
                ):
                    exact_plan["items"] = [
                        row for row in plan.get("items") or []
                        if (
                            (
                                allowed_rows is None
                                or step3_texture_row_key(
                                    {"folder": row.get("folder")},
                                    row.get("atlas_base"),
                                ) in allowed_rows
                            )
                            and step3_texture_row_key(
                                {"folder": row.get("folder")},
                                row.get("atlas_base"),
                            ) not in normalization_failed_row_keys
                        )
                    ]
                exact_plan["preserved_cluster_materials"] = [
                    row for row in plan.get("preserved_cluster_materials") or []
                    if os.path.normcase(os.path.abspath(str(row.get("spm", ""))))
                    in affected_keys
                ]
                spm_jobs = jobs_from_texture_plan(
                    exact_plan, allowed_spms=affected_spms
                )
                normalization_payload = {
                    "affected_spms": [
                        str(path) for path in affected_spms
                    ],
                    "allowed_step3_row_keys": sorted(
                        list(allowed_rows or ())
                    ),
                    "spm_jobs": startup_json_safe(spm_jobs),
                    "exact_plan": startup_json_safe(exact_plan),
                }
                normalization_config = mutation_config_projection(
                    self.cfg, ("tree_root",)
                )
                normalization_backup_root = (
                    Path(self.cfg["tree_root"]) / "_spm_backups"
                )
                normalization_scope_memberships = [
                    Path(path).parent
                    / ".atlas_leaf_speedtree_scopes"
                    for path in affected_spms
                ]
                normalization_authority = (
                    capture_runtime_mutation_authority(
                        action="step3_normalize",
                        unit_id="step3-normalize",
                        payload=normalization_payload,
                        paths=[
                            *affected_spms,
                            normalization_backup_root,
                        ],
                        write_paths=[
                            *affected_spms,
                            normalization_backup_root,
                        ],
                        memberships=[
                            *normalization_scope_memberships,
                            normalization_backup_root,
                        ],
                        write_memberships=[normalization_backup_root],
                        config_projection=normalization_config,
                        tool_paths=[
                            mutation_source_path(
                                normalize_spms_transactionally
                            ),
                        ],
                    )
                )
                require_mutation_authority_unit(
                    normalization_authority,
                    "step3-normalize",
                    current_payload=normalization_payload,
                    current_config=mutation_config_projection(
                        self.cfg, ("tree_root",)
                    ),
                )
                normalized = normalize_spms_transactionally(
                    spm_jobs,
                    backup_root=Path(self.cfg["tree_root"]) / "_spm_backups",
                    skip_unbuildable=True,
                    allow_partial_materials=True,
                )
                cleanup = cleanup_preserved_cluster_outputs(exact_plan)
                complete_mutation_authority_unit(
                    normalization_authority,
                    "step3-normalize",
                    post_paths=affected_spms,
                )
                if run_report is not None:
                    normalized_spms = [
                        str(path) for path in normalized.get("spms") or []
                    ]
                    skipped_spms = [{
                        "spm": str(entry.get("spm") or ""),
                        "reason": str(entry.get("reason") or ""),
                    } for entry in normalized.get("skipped") or []]
                    skipped_materials = [{
                        "spm": str(entry.get("spm") or ""),
                        "material_id": str(
                            entry.get("material_id") or ""
                        ),
                        "material_name": str(
                            entry.get("material_name") or ""
                        ),
                        "reason": str(entry.get("reason") or ""),
                    } for entry in normalized.get("skipped_materials") or []]
                    run_report["normalization"] = {
                        "status": (
                            "partial_success"
                            if (
                                normalization_failed_row_keys
                                or skipped_spms
                                or skipped_materials
                            )
                            else "succeeded"
                        ),
                        "normalized_spms": normalized_spms,
                        "materials": int(normalized.get("materials", 0)),
                        "backup_dir": normalized.get("backup_dir"),
                        "skipped": skipped_spms,
                        "skipped_materials": skipped_materials,
                        "excluded_failed_rows": [
                            step3_row_key_json(key)
                            for key in sorted(
                                normalization_failed_row_keys
                            )
                        ],
                    }
                    run_report["counts"]["spms_normalized"] = len(
                        normalized_spms
                    )
                    run_report["counts"]["spms_skipped"] = len(
                        skipped_spms
                    )
                    run_report["counts"]["materials_skipped"] = len(
                        skipped_materials
                    )
                    self._save_step3_run_report(
                        step3_run_report_path,
                        run_report,
                        stage="normalization_succeeded",
                    )
                self._ui(lambda r=normalized: self.log(
                    f"[③ SK SPM 정리] {len(r['spms'])}개 SPM / "
                    f"{r['materials']}개 머티리얼 — 백업: {r['backup_dir']}"))
                for entry in normalized.get("skipped", []):
                    self._ui(lambda e=entry: self.log(
                        f"[③ SK SPM 정리 건너뜀] {Path(e['spm']).name}: {e['reason']}"))
                for entry in normalized.get("skipped_materials", []):
                    self._ui(lambda e=entry: self.log(
                        "[③ SK SPM 머티리얼 건너뜀] "
                        f"{Path(e['spm']).name} / "
                        f"{e.get('material_name') or e.get('material_id')}: "
                        f"{e['reason']}"
                    ))
                if cleanup["cleaned"]:
                    self._ui(lambda r=cleanup: self.log(
                        f"[③ Cluster 원본 보존] {len(r['cleaned'])}개 머티리얼 복원"))
            except MutationAuthorityError:
                raise
            except MutationAuthorityError:
                raise
            except Exception as exc:
                failed += 1
                if run_report is not None:
                    run_report["normalization"] = {
                        "status": "failed",
                        "targets": [str(path) for path in affected_spms],
                        "error": str(exc),
                    }
                    run_report["failures"].append({
                        "stage": "spm_normalization",
                        "reason": str(exc),
                    })
                    self._save_step3_run_report(
                        step3_run_report_path,
                        run_report,
                        stage="normalization_failed",
                    )
                self._ui(lambda e=exc: self.log(
                    f"[③ SK SPM 정리 실패] 출력 파일은 보존됨, SPM은 변경하지 않음: {e}"))
        sync_allowed = not require_all_renders_for_sync or failed == 0
        if require_all_renders_for_sync and failed and sync_candidates:
            self._ui(lambda count=failed: self.log(
                f"[③ Unreal 동기화 건너뜀] 렌더 실패 {count}개 — "
                "전체 재추출 성공 후 한 번에 동기화합니다."))
        if run_report is not None:
            if not self.cfg.get("unreal_texture_sync_enabled", True):
                run_report["unreal_sync"] = {"status": "disabled"}
            elif not sync_candidates:
                run_report["unreal_sync"] = {"status": "no_candidates"}
            elif not sync_allowed:
                run_report["unreal_sync"] = {
                    "status": "skipped_due_render_failure",
                    "candidate_count": len(sync_candidates),
                }
        if (sync_allowed and sync_candidates
                and self.cfg.get("unreal_texture_sync_enabled", True)):
            try:
                unique_candidates = []
                seen_candidates = set()
                for path in sync_candidates:
                    absolute = os.path.abspath(str(path))
                    key = os.path.normcase(absolute)
                    if key not in seen_candidates:
                        seen_candidates.add(key)
                        unique_candidates.append(absolute)
                if run_report is not None:
                    run_report["unreal_sync"] = {
                        "status": "running",
                        "candidates": list(unique_candidates),
                        "force_verify": bool(force_unreal_verify),
                    }
                    run_report["counts"]["sync_candidates"] = len(
                        unique_candidates
                    )
                    self._save_step3_run_report(
                        step3_run_report_path,
                        run_report,
                        stage="unreal_sync_start",
                    )
                sync_payload = {
                    "files": list(unique_candidates),
                    "force_verify": bool(force_unreal_verify),
                }
                sync_config_keys = (
                    "unreal_texture_sync_enabled",
                    "unreal_project",
                    "unreal_editor_cmd",
                    "unreal_texture_destination",
                    "unreal_texture_commandlet_fallback",
                    "unreal_texture_sync_timeout",
                )
                sync_config = mutation_config_projection(
                    self.cfg, sync_config_keys
                )
                sync_authority = capture_runtime_mutation_authority(
                    action="step3_unreal_sync",
                    unit_id="step3-unreal-sync",
                    payload=sync_payload,
                    paths=unique_candidates,
                    config_projection=sync_config,
                    tool_paths=[
                        self.cfg.get("unreal_editor_cmd"),
                        mutation_source_path(sync_texture_files),
                    ],
                )
                status_text = (
                    "③ Unreal 전체 재확인 중..."
                    if force_unreal_verify else
                    "③ Unreal 텍스처 내용 해시 확인·동기화 중..."
                )
                self._ui(lambda text=status_text: self.status_var.set(text))
                self._ui(lambda count=len(unique_candidates): self.log(
                    f"[③ Unreal 동기화 시작] 후보 {count}장 · "
                    "응답 대기 중에는 5초마다 경과 시간을 표시합니다."))

                def sync_progress(event):
                    message = event.get("message") or "Unreal 텍스처 확인 중..."
                    self._ui(lambda text=message: self.status_var.set(
                        f"③ {text}"))

                heartbeat_stop = threading.Event()
                heartbeat_started = time.monotonic()

                def sync_heartbeat():
                    while not heartbeat_stop.wait(5.0):
                        elapsed = int(time.monotonic() - heartbeat_started)
                        self._ui(
                            lambda seconds=elapsed, count=len(unique_candidates):
                            None if heartbeat_stop.is_set() else
                            self.status_var.set(
                                f"③ Unreal 응답 대기 {seconds}초 · 대상 {count}장"
                            )
                        )

                heartbeat = threading.Thread(
                    target=sync_heartbeat, daemon=True
                )
                heartbeat.start()
                try:
                    require_mutation_authority_unit(
                        sync_authority,
                        "step3-unreal-sync",
                        current_payload={
                            "files": list(unique_candidates),
                            "force_verify": bool(force_unreal_verify),
                        },
                        current_config=mutation_config_projection(
                            self.cfg, sync_config_keys
                        ),
                    )
                    sync_report = self._sync_pending_texture_files(
                        unique_candidates,
                        progress=sync_progress,
                        force_verify=force_unreal_verify,
                    )
                    complete_mutation_authority_unit(
                        sync_authority,
                        "step3-unreal-sync",
                        post_paths=unique_candidates,
                    )
                finally:
                    heartbeat_stop.set()
                counts = sync_report.get("counts") or {}
                mode = sync_report.get("mode", "unknown")
                sync_summary["latest"] = (
                    int(sync_report.get("receipt_current", 0))
                    + int(counts.get("unchanged", 0))
                )
                sync_summary["changed"] = sum(
                    int(counts.get(name, 0))
                    for name in ("configured", "reimported", "created")
                )
                errors = list(sync_report.get("errors") or [])
                sync_summary["failed"] = max(
                    len(errors), int(counts.get("error", 0)))
                failed += sync_summary["failed"]
                sync_report_path = sync_report.get("report_path")
                if run_report is not None:
                    run_report["counts"].update({
                        "sync_latest": sync_summary["latest"],
                        "sync_changed": sync_summary["changed"],
                        "sync_failed": sync_summary["failed"],
                    })
                    run_report["unreal_sync"].update({
                        "status": (
                            "failed"
                            if sync_summary["failed"] else "succeeded"
                        ),
                        "mode": mode,
                        "counts": dict(counts),
                        "errors": errors,
                        "report_path": sync_report_path,
                    })
                    for error in errors:
                        run_report["failures"].append({
                            "stage": "unreal_sync",
                            "reason": str(error),
                        })
                    self._save_step3_run_report(
                        step3_run_report_path,
                        run_report,
                        stage="unreal_sync_finished",
                    )
                self._ui(lambda c=counts, m=mode: self.log(
                    "[③ Unreal 동기화] "
                    f"방식={m} · 동일 {c.get('unchanged', 0)} · "
                    f"재임포트 {c.get('reimported', 0)} · 설정 {c.get('configured', 0)} · "
                    f"신규 add {c.get('created', 0)}"))
                receipt_current = int(sync_report.get("receipt_current", 0))
                if receipt_current:
                    self._ui(lambda count=receipt_current: self.log(
                        f"[③ Unreal 영수증 캐시] 현재 파일 {count}장 재해시/RPC 생략"))
                forced_verify_count = int(
                    sync_report.get("forced_verify_count", 0)
                )
                if forced_verify_count:
                    self._ui(lambda count=forced_verify_count: self.log(
                        f"[③ Unreal 전체 재확인] receipt 캐시 무시 · "
                        f"{count}장 RPC 검증"))
                if sync_report_path:
                    self._ui(lambda path=sync_report_path: self.log(
                        f"[③ Unreal 동기화 리포트] {path}"))
                for error in errors:
                    self._ui(lambda e=error: self.log(f"[③ Unreal 동기화 실패] {e}"))
            except MutationAuthorityError:
                raise
            except UnrealTextureSyncDeferred as exc:
                failed += 1
                sync_summary["failed"] += 1
                sync_deferred = str(exc)
                if run_report is not None:
                    run_report["counts"]["sync_failed"] += 1
                    run_report["unreal_sync"].update({
                        "status": "deferred",
                        "error": str(exc),
                    })
                    run_report["failures"].append({
                        "stage": "unreal_sync_deferred",
                        "reason": str(exc),
                    })
                    self._save_step3_run_report(
                        step3_run_report_path,
                        run_report,
                        stage="unreal_sync_deferred",
                    )
                self._ui(lambda e=exc: self.log(
                    f"[③ Unreal 동기화 보류] 로컬 TGA/SPM은 완료됨: {e}"))
            except Exception as exc:
                failed += 1
                sync_summary["failed"] += 1
                if run_report is not None:
                    run_report["counts"]["sync_failed"] += 1
                    run_report["unreal_sync"].update({
                        "status": "failed",
                        "error": str(exc),
                    })
                    run_report["failures"].append({
                        "stage": "unreal_sync",
                        "reason": str(exc),
                    })
                    self._save_step3_run_report(
                        step3_run_report_path,
                        run_report,
                        stage="unreal_sync_failed",
                    )
                self._ui(lambda e=exc: self.log(
                    f"[③ Unreal 동기화 실패] 로컬 TGA/SPM은 보존됨: {e}"))
        if run_report is not None:
            run_report["status"] = (
                "failed"
                if failed else
                "partial_success"
                if planned_skipped else
                "success"
            )
            run_report["finished_at"] = datetime.now().astimezone().isoformat()
            run_report["final"] = {
                "render_succeeded": done,
                "failed": failed,
                "planned_skipped": planned_skipped,
                "sync": dict(sync_summary),
            }
            self._save_step3_run_report(
                step3_run_report_path,
                run_report,
                stage="final",
            )
            self._ui(lambda path=step3_run_report_path: self.log(
                f"[③ 실행 리포트 완료] {path}"
            ))
        self._ui(lambda: self._step3_finished(
            done,
            failed,
            sync_summary,
            sync_report_path,
            sync_deferred,
            planned_skipped,
        ))
        return {
            "shared_queue_success": failed == 0,
            "shared_queue_result": {
                "operation": "step3",
                "render_completed": done,
                "failed": failed,
                "planned_skipped": planned_skipped,
                "sync": dict(sync_summary),
            },
        }

    def _step3_finished(self, render_done, failed, sync_summary,
                        report_path=None, deferred=None,
                        planned_skipped=0):
        sync_total = sum(sync_summary.values())
        render_failed = max(0, failed - sync_summary["failed"])
        if sync_total or deferred:
            summary = (
                f"③ 완료: 렌더 {render_done}세트"
                f"(실패 {render_failed}) · "
                f"Unreal 최신 {sync_summary['latest']}장 · "
                f"변경 {sync_summary['changed']}장 · "
                f"실패 {sync_summary['failed']}장"
            )
        else:
            summary = f"③ 완료: 렌더 {render_done}세트 · 실패 {failed}개"
        if planned_skipped:
            summary = summary.replace(
                "③ 완료:",
                "③ 부분 완료:",
                1,
            )
            summary += f" · 오류 항목 제외 {planned_skipped}개"
            if failed == 0:
                summary += " · 나머지 완료"
        self.log(summary)
        if report_path:
            self.log(f"③ 결과 리포트: {report_path}")
        if deferred:
            self.log(f"③ Unreal 보류 사유: {deferred}")
        final_status = (
            summary + (f" · 리포트 {report_path}" if report_path else ""))
        refresh_folders = list(
            getattr(self, "_active_step3_refresh_folders", ())
        )
        self._active_step3_refresh_folders = []
        if refresh_folders:
            self._start_completion_refresh(
                final_status,
                targets=refresh_folders,
            )
        else:
            self._start_completion_refresh(final_status)

    def _batch_finished(self, label, done, failed):
        summary = f"{label} 완료: 성공 {done}개, 실패 {failed}개"
        self.log(f"{summary}. 표를 다시 검사합니다.")
        self._start_completion_refresh(summary)

    # ---------------------------------------------------------- PCG + placed-level targets
    def refresh_pcg_targets(self):
        return self._start_target_refresh(
            "refresh_pcg_targets.py",
            "Unreal에서 PCG + 작업 레벨 대상 읽는 중...",
        )

    def import_saved_pcg_report(self):
        return self._start_target_refresh(
            "import_pcg_display_report.py",
            "저장된 PCG 리포트 읽는 중...",
        )

    def _start_target_refresh(self, script_name, progress_text):
        if getattr(self, "_target_refresh_active", False):
            self.status_var.set("PCG + 작업 레벨 대상을 이미 읽는 중입니다.")
            return False
        if getattr(self, "_busy", False):
            self.status_var.set("다른 작업이 끝난 뒤 대상을 다시 읽으세요.")
            return False

        timeout = max(
            1.0, float(self.cfg.get("pcg_target_refresh_timeout", 120))
        )
        self._target_refresh_active = True
        self._set_busy(True)
        self.status_var.set(progress_text)

        def worker():
            cmd = [
                sys.executable.replace("pythonw.exe", "python.exe"),
                str(TOOL_DIR / script_name),
                "--out", str(TARGETS_PATH),
            ]
            result = None
            error = None
            try:
                result = owned_run(
                    cmd,
                    source="pcg_st9_texture_batch.pcg_texture_gui.refresh_targets",
                    run_factory=subprocess.run,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    creationflags=0x08000000 if os.name == "nt" else 0,
                )
            except subprocess.TimeoutExpired:
                error = (
                    f"대상 읽기가 {timeout:g}초 동안 끝나지 않았습니다. "
                    "Unreal Editor 또는 저장 리포트 상태를 확인한 뒤 다시 시도하세요."
                )
            except Exception as exc:
                error = str(exc)
            self.root.after(
                0,
                lambda completed=result, failure=error:
                    self._pcg_targets_done(completed, failure),
            )

        self.worker = threading.Thread(target=worker, daemon=True)
        self.worker.start()
        return True

    def _pcg_targets_done(self, result, error=None):
        self.worker = None
        self._target_refresh_active = False
        self._set_busy(False)
        if error is not None:
            messagebox.showerror("대상 읽기 실패", error)
            self.status_var.set("PCG + 작업 레벨 대상 읽기 실패")
            return
        if result.returncode != 0:
            messagebox.showerror("대상 읽기 실패", (result.stderr or result.stdout)[-2000:])
            self.status_var.set("PCG + 작업 레벨 대상 읽기 실패")
            return
        self.use_pcg_targets_var.set(True)
        self.log((result.stdout or "PCG + 작업 레벨 대상 목록 갱신됨").strip())
        self._pending_refresh = False
        self.refresh()

    def open_selected_folder(self):
        item = self.selected_item()
        if not item:
            messagebox.showinfo("폴더 열기", "표에서 행을 먼저 클릭하세요.")
            return
        external_handoff_popen(
            ["explorer", item["folder"]],
            source="pcg_st9_texture_batch.pcg_texture_gui.open_folder",
            ownership="shell_handoff",
        )

    def shutdown_shared_queue(self):
        self.cancel_refresh()
        tracker = getattr(self, "startup_latency", None)
        if tracker is not None and tracker.snapshot().get("status") == "running":
            tracker.finish_early("cancelled")
        runtime = getattr(self, "shared_queue_runtime", None)
        if runtime is not None:
            runtime.shutdown()


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
