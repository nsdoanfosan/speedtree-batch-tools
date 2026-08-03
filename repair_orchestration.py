"""Fail-closed exact-target repair planning shared by the batch tools.

The planner is deliberately free of GUI and process-launching code.  It turns
durable SK audit evidence into a bounded sequence of existing BAT operations.
Only contract reason codes are routing authority; asset names are never used.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from repair_reason_registry import (
    FATAL,
    INFORMATIONAL,
    REASON_REGISTRY,
    REPAIRABLE,
    UNSUPPORTED,
)


REPAIR_PLAN_SCHEMA_VERSION = 1
REPAIR_RECEIPT_SCHEMA_VERSION = 1

PCG_TEXTURE_TOOL = "pcg_st9_texture_batch"
GENERATOR_SYNC_TOOL = "spm_generator_sync"
MODELER_RECOVERY_TOOL = "speedtree_modeler"

STEP3_STANDARD = "step3-standard"
ATLAS_MANIFEST_MIRROR_REPAIR = "atlas-manifest-mirror-repair"
GENERATOR_SYNC = "generator-sync"
CLUSTER_REFRESH = "cluster-refresh"
GENERATOR_SYNC_AND_CLUSTER = "generator-sync-and-cluster"
MODELER_NODE_TABLE_RECOVERY = "modeler-node-table-recovery"

STATUS_PENDING = "automatic_repair_pending"
STATUS_TEXTURE = "pcg_texture_repair_running"
STATUS_ATLAS = "atlas_manifest_repair_running"
STATUS_GENERATOR = "generator_sync_running"
STATUS_CLUSTER = "cluster_refresh_running"
STATUS_MODELER = "modeler_node_table_recovery_running"
STATUS_REAUDIT = "fresh_reaudit_running"
STATUS_PIPELINE = "blender_unreal_retry_running"
STATUS_COMPLETED = "automatic_repair_completed"
STATUS_FINAL_FAILED = "final_failed"
STATUS_CANCELLED = "cancelled"

STATUS_LABELS = {
    STATUS_PENDING: "자동 복구 대기",
    STATUS_TEXTURE: "PCG 텍스처 복구 중",
    STATUS_ATLAS: "Atlas manifest 복구 중",
    STATUS_GENERATOR: "Generator Sync 중",
    STATUS_CLUSTER: "Cluster 갱신 중",
    STATUS_MODELER: "SpeedTree Node table 복구 중",
    STATUS_REAUDIT: "재검증 중",
    STATUS_PIPELINE: "Blender-Unreal 재시도 중",
    STATUS_COMPLETED: "자동 복구 완료",
    STATUS_FINAL_FAILED: "최종 차단",
    STATUS_CANCELLED: "자동 복구 취소",
}

# The registry is the only disposition source.  Action-specific sets are
# derived from its rows so the planner cannot drift onto vocabulary no gate
# emits (the original implementation had 25 such dead codes).
def _registered_codes(*, disposition=None, note=None):
    return frozenset(
        code for code, row in REASON_REGISTRY.items()
        if (disposition is None or row.disposition == disposition)
        and (note is None or row.note == note)
    )


TEXTURE_REASON_CODES = _registered_codes(
    disposition=REPAIRABLE, note="pcg_texture",
)
ATLAS_MANIFEST_REPAIR_CODES = _registered_codes(
    disposition=REPAIRABLE, note="atlas_manifest",
)
GENERATOR_REASON_CODES = _registered_codes(
    disposition=REPAIRABLE, note="generator",
)
GENERATOR_AND_CLUSTER_REASON_CODES = _registered_codes(
    disposition=REPAIRABLE, note="generator_cluster",
)
CLUSTER_STALE_REASON_CODES = _registered_codes(
    disposition=REPAIRABLE, note="cluster_refresh",
)
MODELER_NODE_TABLE_REASON_CODES = _registered_codes(
    disposition=REPAIRABLE, note="modeler_node_table",
)

UNSUPPORTED_ATLAS_MANIFEST_CODES = _registered_codes(
    disposition=UNSUPPORTED, note="atlas_ownership",
)
UNSUPPORTED_CLUSTER_DATA_CODES = _registered_codes(
    disposition=UNSUPPORTED, note="cluster_data",
)
RECIPE_GATED_REASON_CODES = _registered_codes(note="recipe_gated")
UNSUPPORTED_EXPORT_MATERIAL_CODES = _registered_codes(
    disposition=UNSUPPORTED, note="export_material",
)
UNSUPPORTED_EXPORTER_CRASH_CODES = _registered_codes(
    disposition=UNSUPPORTED, note="exporter_crash",
)
UNSUPPORTED_CLUSTER_HANDOFF_CODES = _registered_codes(
    disposition=UNSUPPORTED, note="cluster_handoff",
)
UNSUPPORTED_PREFLIGHT_ERROR_CODES = _registered_codes(
    disposition=UNSUPPORTED, note="preflight_error",
)

FATAL_REASON_CODES = _registered_codes(disposition=FATAL)
UNSUPPORTED_REASON_CODES = _registered_codes(disposition=UNSUPPORTED)
REPAIRABLE_REASON_CODES = _registered_codes(disposition=REPAIRABLE)
ALL_REPAIR_CONTRACT_CODES = frozenset(
    code for code, row in REASON_REGISTRY.items()
    if row.disposition != INFORMATIONAL
)

REPAIR_UI_AUTOMATIC = "automatic_repair"
REPAIR_UI_BLOCKED = "final_blocked"

REASON_LABELS_KO = {
    "canonical_texture_output_unmapped": "canonical PCG 텍스처 연결이 누락되었습니다",
    "managed_texture_set_incomplete": "관리 대상 PCG 텍스처 세트가 불완전합니다",
    "canonical_texture_set_incomplete": "canonical PCG 텍스처 세트가 불완전합니다",
    "canonical_texture_output_stale": "canonical PCG 텍스처 출력이 오래되었습니다",
    "managed_texture_set_stale": "관리 대상 PCG 텍스처 세트가 오래되었습니다",
    "source_fallback_needs_pcg_generation": "원본 fallback에 PCG 텍스처 생성이 필요합니다",
    "texture_set_incomplete": "텍스처 세트가 불완전합니다",
    "texture_source_fallback_needs_pcg_generation": "텍스처 원본 fallback에 PCG 생성이 필요합니다",
    "atlas_manifest_mirror_conflict_repairable": "동일 원본의 낡은 Atlas manifest 미러가 충돌합니다",
    "atlas_manifest_ownership_conflict": "서로 다른 Atlas 소유권 주장이 충돌합니다",
    "generator_slot_pair_drift": "Generator 슬롯 쌍이 현재 계약과 다릅니다",
    "atlas_generator_connection_missing": "Atlas Generator 연결이 누락되었습니다",
    "normalized_prototype_zero_match": "정규화된 prototype 연결 대상을 찾지 못했습니다",
    "generator_connection_contract_incomplete": "Generator 연결 계약이 불완전합니다",
    "normalized_generator_delivery_incomplete": "정규화된 Generator 전달이 불완전합니다",
    "cluster_stale": "Cluster 결과가 오래되었습니다",
    "cluster_relation_stale": "Cluster 관계가 오래되었습니다",
    "cluster_refresh_required": "Cluster 갱신이 필요합니다",
    "normalized_generator_node_table_stale": "SpeedTree Generator Node table이 오래되었습니다",
    "normalized_variants_required": "필수 정규화 Cluster variant가 아직 생성되지 않았습니다",
    "normalized_variants_stale": "정규화 Cluster variant가 현재 원본보다 오래되었습니다",
    "cluster_tga_basename_invalid": "Cluster TGA 참조가 canonical 파일 규칙과 다릅니다",
    "asset_cluster_bake_texture_contract_invalid": "Cluster bake 텍스처 계약이 올바르지 않습니다",
    "blender_cluster_bake_map_role_mismatch": "Blender Cluster bake map 역할이 서로 다릅니다",
    "atlas_strict_material_binding_conflict": "Atlas material binding이 다른 canonical 텍스처를 가리킵니다",
    "asset_export_material_missing": "SpeedTree 내보내기에 필요한 재질이 없습니다",
    "material_export_missing": "SpeedTree 내보내기 재질이 누락되었습니다",
    "all_export_material_missing": "SpeedTree 내보내기 재질이 모두 누락되었습니다",
    "process_exporter_crash": "SpeedTree exporter가 비정상 종료되었습니다",
    "access_violation_exhausted": "SpeedTree exporter access violation 재시도가 모두 실패했습니다",
    "0xc0000005": "SpeedTree exporter에서 access violation이 발생했습니다",
    "live_export_evidence_unavailable_stale_node_table": "오래된 Node table 때문에 현재 export 연결을 증명할 수 없습니다",
    "pcg_cluster_handoff_not_ready": "PCG Cluster handoff가 현재 실행 가능한 상태가 아닙니다",
    "preflight_error": "SpeedTree 재질 사전 검사 자체가 완료되지 않았습니다",
    "dependency_root_reason_missing": "의존 작업의 실제 실패 원인이 영수증에서 유실되었습니다",
}

REASON_KEYS = frozenset({
    "reason",
    "reason_code",
    "reason_codes",
    "reason_token",
    "classification",
    "classifications",
    "code",
    "codes",
    "issue",
    "issues",
    "issue_code",
    "issue_codes",
    "delivery_reason",
    "blocked_reason_token",
    "result",
})

PATH_KEYS = frozenset({
    "spm",
    "target_spm",
    "canonical_spm",
    "canonical_target_spm",
    "cluster_target_spm",
    "authoring_spm",
    "output_spm",
})

CLUSTER_PATH_KEYS = frozenset({
    "cluster_target_spm",
    "canonical_cluster_spm",
    "producer_spm",
    "on_target_spm",
    "on_target_spms",
})

TARGET_IDENTITY_KEYS = frozenset({
    "queue_id",
    "exact_spm",
    "spm",
    "target_spm",
    "canonical_spm",
    "canonical_target_spm",
    "authoring_spm",
    "output_spm",
})


def _normal_token(value: Any) -> str:
    return str(value or "").strip().casefold()


def _path_key(value: str | Path) -> str:
    return os.path.normcase(os.path.abspath(str(value))).casefold()


def _json_digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")).hexdigest()


def _walk(value: Any, trail: tuple[str, ...] = ()):
    if isinstance(value, Mapping):
        for key, child in value.items():
            child_trail = (*trail, str(key))
            yield child_trail, child
            yield from _walk(child, child_trail)
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            child_trail = (*trail, str(index))
            yield child_trail, child
            yield from _walk(child, child_trail)


def evidence_reason_codes(evidence: Mapping[str, Any]) -> tuple[str, ...]:
    """Extract contract tokens only from structured reason-bearing fields."""

    result: set[str] = set()
    for trail, value in _walk(evidence):
        if not trail or trail[-1].casefold() not in REASON_KEYS:
            continue
        values = value if isinstance(value, (list, tuple, set)) else (value,)
        for token in values:
            if isinstance(token, Mapping):
                continue
            normalized = _normal_token(token)
            if normalized:
                result.add(normalized)
    return tuple(sorted(result))


def fresh_repair_receipt_authoritative(
    receipt: Mapping[str, Any],
    plan: Mapping[str, Any],
) -> bool:
    """Require an exact current request/parent/target match for final authority."""

    if not isinstance(receipt, Mapping) or not isinstance(plan, Mapping):
        return False
    if _normal_token(receipt.get("status")) not in {
        STATUS_COMPLETED,
        STATUS_FINAL_FAILED,
        STATUS_CANCELLED,
    }:
        return False
    return all(
        str(receipt.get(key) or "") == str(plan.get(key) or "")
        for key in ("request_id", "parent_retry_id", "exact_spm")
    )


def _candidate_paths(
    evidence: Mapping[str, Any],
    *,
    cluster_only: bool = False,
) -> tuple[str, ...]:
    keys = CLUSTER_PATH_KEYS if cluster_only else PATH_KEYS
    result: dict[str, str] = {}
    for trail, value in _walk(evidence):
        if not trail or trail[-1].casefold() not in keys:
            continue
        values = value if isinstance(value, (list, tuple, set)) else (value,)
        for candidate in values:
            if not isinstance(candidate, (str, os.PathLike)):
                continue
            text = str(candidate).strip()
            if text and Path(text).suffix.casefold() == ".spm":
                result.setdefault(_path_key(text), os.path.abspath(text))
    return tuple(result[key] for key in sorted(result))


def _cluster_paths_for_exact(
    evidence: Mapping[str, Any],
    exact_spm: str | Path,
) -> tuple[str, ...]:
    """Extract only providers in the current target's provenance slice.

    A durable audit file may contain relations for the whole inventory.  A
    nested mapping that declares a target identity therefore resets inherited
    scope: only a mapping whose identity is the exact requested SPM may expose
    provider paths.  Report payload collections also start unscoped so a root
    SK queue identity cannot accidentally authorize sibling report rows.
    """

    exact_key = _path_key(exact_spm)
    result: dict[str, str] = {}

    def path_values(value):
        values = value if isinstance(value, (list, tuple, set)) else (value,)
        return [
            str(candidate).strip()
            for candidate in values
            if isinstance(candidate, (str, os.PathLike))
            and str(candidate).strip()
            and Path(str(candidate).strip()).suffix.casefold() == ".spm"
        ]

    def visit(value: Any, scoped: bool):
        if isinstance(value, Mapping):
            identities = []
            for key, child in value.items():
                if str(key).casefold() in TARGET_IDENTITY_KEYS:
                    identities.extend(path_values(child))
            local_scope = (
                any(_path_key(path) == exact_key for path in identities)
                if identities
                else scoped
            )
            if local_scope:
                provider_keys = set(CLUSTER_PATH_KEYS)
                if _normal_token(value.get("repair_action")) in {
                    CLUSTER_REFRESH,
                    GENERATOR_SYNC_AND_CLUSTER,
                }:
                    provider_keys.add("target_spms")
                for key, child in value.items():
                    if str(key).casefold() not in provider_keys:
                        continue
                    for path in path_values(child):
                        if _path_key(path) != exact_key:
                            result.setdefault(_path_key(path), os.path.abspath(path))
            for key, child in value.items():
                child_scope = local_scope
                if str(key).casefold() == "current_report_payloads":
                    child_scope = False
                visit(child, child_scope)
        elif isinstance(value, (list, tuple)):
            for child in value:
                visit(child, scoped)

    visit(evidence, True)
    return tuple(result[key] for key in sorted(result))


def canonical_exact_spm(
    value: str | Path,
    inventory_paths: Iterable[str | Path],
    *,
    require_exists: bool = True,
) -> str:
    """Return the inventory-owned spelling of one exact real SPM.

    Case normalization and ``samefile`` aliases are accepted.  Similar names,
    parent-folder guesses, and typo repair are intentionally absent.
    """

    path = Path(value).expanduser()
    if not path.is_absolute() or path.suffix.casefold() != ".spm":
        raise ValueError("exact target must be an absolute .spm path")
    if require_exists and not path.is_file():
        raise FileNotFoundError(f"exact target SPM does not exist: {path}")
    candidates = [
        Path(candidate).expanduser()
        for candidate in inventory_paths
        if str(candidate).strip()
    ]
    if not candidates:
        raise ValueError("inventory identity is required for exact-target repair")
    for candidate in candidates:
        if not candidate.is_absolute() or candidate.suffix.casefold() != ".spm":
            continue
        same = _path_key(candidate) == _path_key(path)
        if not same and path.exists() and candidate.exists():
            try:
                same = os.path.samefile(path, candidate)
            except OSError:
                same = False
        if same:
            if require_exists and not candidate.is_file():
                continue
            return str(candidate.absolute())
    raise ValueError(
        "exact target does not match a canonical inventory SPM identity"
    )


def _validated_recipe(evidence: Mapping[str, Any]) -> Mapping[str, Any] | None:
    for trail, value in _walk(evidence):
        if not trail or trail[-1].casefold() not in {
            "material_binding_recipe",
            "canonical_binding_recipe",
            "cluster_material_binding_recipe",
        }:
            continue
        if not isinstance(value, Mapping):
            continue
        if _normal_token(value.get("status")) not in {
            "current", "validated", "ready",
        }:
            continue
        if value.get("authoritative") is not True:
            continue
        if not (value.get("target_spm") or value.get("target_spms")):
            continue
        return value
    return None


def _first_nested_mapping(
    evidence: Mapping[str, Any],
    key: str,
) -> Mapping[str, Any] | None:
    wanted = str(key).casefold()
    for trail, value in _walk(evidence):
        if (
            trail
            and trail[-1].casefold() == wanted
            and isinstance(value, Mapping)
        ):
            return value
    return None


def _validated_modeler_recovery_scope(
    evidence: Mapping[str, Any],
    canonical: Path,
) -> dict[str, Any] | None:
    scope = _first_nested_mapping(evidence, "stale_node_table_recovery")
    if not isinstance(scope, Mapping):
        return None
    if (
        scope.get("available") is not True
        or scope.get("schema_version") != 2
        or scope.get("mode") != "owned_semantic_uia_modeler_save_watch"
        or scope.get("scope_policy")
        != "explicit_sealed_delivery_scopes_v1"
        or not scope.get("target_spm")
        or _path_key(scope["target_spm"]) != _path_key(canonical)
    ):
        return None

    def canonical_mesh_ids(key: str) -> list[int] | None:
        values = scope.get(key)
        if not isinstance(values, (list, tuple)):
            return None
        if any(
            isinstance(value, bool)
            or not isinstance(value, int)
            or value <= 0
            for value in values
        ):
            return None
        normalized = sorted(set(values))
        return normalized if list(values) == normalized else None

    authoring_mesh_ids = canonical_mesh_ids("authoring_mesh_ids")
    required_live_mesh_ids = canonical_mesh_ids("required_live_mesh_ids")
    if (
        not authoring_mesh_ids
        or not required_live_mesh_ids
        or not set(required_live_mesh_ids).issubset(authoring_mesh_ids)
    ):
        return None
    preimage = str(scope.get("target_preimage_raw_sha256") or "").casefold()
    if len(preimage) != 64 or any(
        character not in "0123456789abcdef" for character in preimage
    ):
        return None
    scope_sha256 = str(scope.get("scope_sha256") or "").casefold()
    sealed = {
        name: value for name, value in scope.items()
        if name != "scope_sha256"
    }
    expected_scope_sha256 = hashlib.sha256((json.dumps(
        sealed,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ) + "\n").encode("utf-8")).hexdigest()
    if (
        len(scope_sha256) != 64
        or scope_sha256 != expected_scope_sha256
    ):
        return None
    return copy.deepcopy(dict(scope))


def _reason_labels_ko(codes: set[str]) -> list[str]:
    return [
        REASON_LABELS_KO[code]
        for code in sorted(codes)
        if code in REASON_LABELS_KO
    ]


def repair_ui_decision(evidence: Mapping[str, Any]) -> dict[str, Any]:
    """Return one Korean, single-meaning operator disposition.

    Stable English reason tokens remain in ``reason_codes`` for receipts and
    diagnostics.  The visible title/reason/action never asks an operator to
    interpret one umbrella error as both repairable and terminal.
    """

    codes = set(evidence_reason_codes(evidence))
    labels = _reason_labels_ko(codes)

    def decision(status: str, reason: str, action: str) -> dict[str, Any]:
        return {
            "status": status,
            "reason": str(reason),
            "action": str(action),
            "reason_codes": tuple(sorted(codes)),
        }

    if codes & UNSUPPORTED_ATLAS_MANIFEST_CODES:
        return decision(
            REPAIR_UI_BLOCKED,
            "서로 다른 원본·export 범위 또는 ownership claim이 같은 Atlas 대상을 소유한다고 기록되어 있습니다.",
            "충돌한 manifest의 원본과 소유 범위를 확인해야 하며 BAT가 한쪽을 임의로 덮어쓰지 않습니다.",
        )
    if codes & UNSUPPORTED_EXPORT_MATERIAL_CODES:
        return decision(
            REPAIR_UI_BLOCKED,
            labels[0] if labels else "SpeedTree 내보내기 재질이 누락되었습니다.",
            "SpeedTree Modeler에서 해당 Generator에 재질을 지정하고 FBX/STMAT를 다시 내보내야 합니다.",
        )
    if codes & UNSUPPORTED_EXPORTER_CRASH_CODES:
        return decision(
            REPAIR_UI_BLOCKED,
            labels[0] if labels else "SpeedTree exporter가 반복해서 비정상 종료되었습니다.",
            "해당 SPM의 재질과 Generator 상태를 Modeler에서 확인한 뒤 수동 export를 시험해야 합니다.",
        )
    if codes & UNSUPPORTED_CLUSTER_DATA_CODES:
        missing = _detail_values(evidence, {"missing"})
        invalid = _detail_values(evidence, {"invalid"})
        expected = _detail_values(evidence, {"expected_base"})
        if missing:
            reason = "Cluster가 참조하는 TGA 파일이 없습니다: " + ", ".join(
                missing[:8]
            )
        elif invalid:
            reason = (
                "Cluster TGA 파일명이 canonical SPM basename과 일치하지 "
                "않습니다: " + ", ".join(invalid[:8])
            )
        else:
            reason = "Cluster TGA 참조가 canonical 파일 규칙과 다릅니다."
        action = (
            "표시된 TGA를 복원하거나 Cluster SPM basename에 맞게 다시 "
            "생성한 뒤 live audit을 다시 실행해야 합니다."
        )
        if expected:
            action += " 기준 basename: " + ", ".join(expected[:4])
        return decision(REPAIR_UI_BLOCKED, reason, action)
    if codes & UNSUPPORTED_CLUSTER_HANDOFF_CODES:
        return decision(
            REPAIR_UI_BLOCKED,
            "PCG Cluster handoff가 현재 실행 가능한 상태가 아닙니다.",
            "handoff 영수증의 상세 원인을 확인하고 누락된 Cluster 입력 또는 bark 정규화를 완료한 뒤 다시 검사하세요.",
        )
    if codes & UNSUPPORTED_PREFLIGHT_ERROR_CODES:
        return decision(
            REPAIR_UI_BLOCKED,
            "SpeedTree 재질 사전 검사 자체가 완료되지 않았습니다.",
            "사전 검사 보고서의 원본 오류를 확인한 뒤 검사를 다시 실행하세요. BAT가 원인을 추측해 성공 처리하지 않습니다.",
        )

    recipe_codes = codes & RECIPE_GATED_REASON_CODES
    if recipe_codes and _validated_recipe(evidence) is None:
        return decision(
            REPAIR_UI_BLOCKED,
            labels[0] if labels else "Cluster material binding 계약이 올바르지 않습니다.",
            "권위 있는 current material binding recipe가 없어 BAT가 연결을 추측하지 않습니다.",
        )

    if "normalized_generator_node_table_stale" in codes:
        recovery = _first_nested_mapping(
            evidence,
            "stale_node_table_recovery",
        )
        recovery_target = (
            recovery.get("target_spm")
            if isinstance(recovery, Mapping)
            else None
        )
        if (
            not recovery_target
            or _validated_modeler_recovery_scope(
                evidence, Path(recovery_target)
            )
            is None
        ):
            return decision(
                REPAIR_UI_BLOCKED,
                "SpeedTree Generator Node table이 오래되었지만 자동 저장할 exact target 범위가 증명되지 않았습니다.",
                "현재 live audit에서 대상 전체의 복구 범위를 다시 확정해야 합니다.",
            )
        return decision(
            REPAIR_UI_AUTOMATIC,
            "SpeedTree Generator Node table이 오래되었고 exact target 자동 복구 범위가 확인되었습니다.",
            "BAT가 소유한 Modeler 세션에서 해당 SPM만 다시 저장하고 live audit을 재실행합니다.",
        )

    if codes & ATLAS_MANIFEST_REPAIR_CODES:
        return decision(
            REPAIR_UI_AUTOMATIC,
            "동일 원본의 낡은 Atlas manifest 미러가 exact authority와 충돌합니다.",
            "exact BAT가 낡은 미러만 갱신하고 Canonical PCG → Atlas 계약을 다시 검사합니다.",
        )
    if codes & TEXTURE_REASON_CODES:
        return decision(
            REPAIR_UI_AUTOMATIC,
            labels[0] if labels else "canonical PCG 텍스처 복구가 필요합니다.",
            "exact PCG BAT로 해당 대상의 텍스처만 복구한 뒤 material preflight를 다시 실행합니다.",
        )
    if codes & GENERATOR_AND_CLUSTER_REASON_CODES:
        return decision(
            REPAIR_UI_AUTOMATIC,
            labels[0] if labels else "Generator 전달과 Cluster 관계 갱신이 필요합니다.",
            "exact Generator Sync와 Cluster 갱신을 순서대로 실행한 뒤 live delivery를 다시 검사합니다.",
        )
    if codes & GENERATOR_REASON_CODES:
        return decision(
            REPAIR_UI_AUTOMATIC,
            labels[0] if labels else "Generator Sync가 필요합니다.",
            "exact Generator Sync를 실행하고 현재 연결을 다시 검사합니다.",
        )
    if codes & CLUSTER_STALE_REASON_CODES or recipe_codes:
        return decision(
            REPAIR_UI_AUTOMATIC,
            labels[0] if labels else "Cluster 갱신이 필요합니다.",
            "exact Cluster 관계를 갱신한 뒤 현재 결과를 다시 검사합니다.",
        )

    return decision(
        REPAIR_UI_BLOCKED,
        "이 차단 사유에는 등록된 자동 복구 동작이 없습니다.",
        "표시된 원인 코드와 감사 증거를 확인해 원본 문제를 수정한 뒤 다시 검사하세요.",
    )


def _detail_values(evidence: Mapping[str, Any], keys: set[str]) -> list[str]:
    values = []
    seen = set()
    for trail, value in _walk(evidence):
        if not trail or trail[-1].casefold() not in keys:
            continue
        children = value if isinstance(value, (list, tuple, set)) else (value,)
        for child in children:
            if isinstance(child, (str, int, float)):
                text = str(child).strip()
                if text and text.casefold() not in seen:
                    seen.add(text.casefold())
                    values.append(text)
    return values


def _unsupported_message(codes: set[str], evidence: Mapping[str, Any]) -> tuple[str, str]:
    if codes & UNSUPPORTED_EXPORT_MATERIAL_CODES:
        materials = _detail_values(
            evidence,
            {
                "material",
                "material_name",
                "material_names",
                "active_material_names",
                "export_visible_materials",
                "missing_export_materials",
            },
        )
        suffix = f" 대상 재질: {', '.join(materials[:8])}." if materials else ""
        return (
            "SpeedTree 내보내기에 재질이 없습니다.",
            "Modeler에서 표시된 재질을 Generator에 다시 지정/확인하고 "
            "재질을 포함해 FBX/STMAT를 내보낸 뒤 다시 실행하세요." + suffix,
        )
    if codes & UNSUPPORTED_EXPORTER_CRASH_CODES:
        attempts = _detail_values(evidence, {"attempt_count", "attempts"})
        attempt_text = attempts[0] if attempts else "3"
        return (
            f"SpeedTree exporter가 access violation으로 {attempt_text}회 종료되었습니다.",
            "Modeler에서 이 exact asset이 정상적으로 열리고 재질/Generator가 "
            "유효한지 확인한 뒤 수동 export를 시험하세요.",
        )
    if codes & UNSUPPORTED_ATLAS_MANIFEST_CODES:
        return (
            "Atlas operational manifest ownership이 서로 다릅니다.",
            "서로 다른 source/export scope 또는 ownership claim을 자동으로 "
            "덮어쓸 수 없습니다. 충돌 영수증의 두 manifest를 확인하세요.",
        )
    if codes & UNSUPPORTED_CLUSTER_DATA_CODES:
        decision = repair_ui_decision(evidence)
        return decision["reason"], decision["action"]
    if codes & (
        UNSUPPORTED_CLUSTER_HANDOFF_CODES
        | UNSUPPORTED_PREFLIGHT_ERROR_CODES
    ):
        decision = repair_ui_decision(evidence)
        return decision["reason"], decision["action"]
    if "atlas_strict_material_binding_conflict" in codes:
        return (
            "실제 소비 texture가 다른 에셋 폴더의 canonical texture와 연결되어 있습니다.",
            "공식 canonical binding provenance/recipe를 확정한 뒤 exact repair를 다시 실행하세요.",
        )
    if codes & {
        "asset_cluster_bake_texture_contract_invalid",
        "blender_cluster_bake_map_role_mismatch",
    }:
        material_ids = _detail_values(evidence, {"material_id", "material_ids"})
        roles = _detail_values(evidence, {"role", "roles", "map_role", "map_roles"})
        details = []
        if material_ids:
            details.append("material IDs " + ", ".join(material_ids[:8]))
        if roles:
            details.append("roles " + ", ".join(roles[:8]))
        suffix = f" ({'; '.join(details)})" if details else ""
        return (
            "실제 소비 texture map과 finalized cluster bake map 역할이 다릅니다" + suffix + ".",
            "공식 material binding recipe를 확정한 뒤 Cluster exact refresh를 다시 실행하세요.",
        )
    return (
        "자동 BAT 복구 경로를 증명할 수 없습니다.",
        "상세 reason code와 audit evidence를 확인한 뒤 원본 authoring 문제를 수정하세요.",
    )


@dataclass(frozen=True)
class RepairPlan:
    schema_version: int
    request_id: str
    parent_retry_id: str
    exact_spm: str
    evidence_sha256: str
    reason_codes: tuple[str, ...]
    stages: tuple[dict, ...]
    supported: bool
    initial_status: str
    friendly_reason: str = ""
    remaining_action: str = ""

    def metadata(self) -> dict:
        return asdict(self)


def build_exact_target_repair_plan(
    exact_spm: str | Path,
    evidence: Mapping[str, Any],
    *,
    inventory_paths: Sequence[str | Path],
    parent_retry_id: str,
    request_id: str,
    require_exists: bool = True,
) -> RepairPlan:
    """Build the exact ordered BAT plan for one failed SK inventory item."""

    if not isinstance(evidence, Mapping) or not evidence:
        raise ValueError("durable audit evidence is required")
    canonical = canonical_exact_spm(
        exact_spm,
        inventory_paths,
        require_exists=require_exists,
    )
    codes = set(evidence_reason_codes(evidence))
    evidence_sha256 = _json_digest(evidence)
    stages: list[dict] = []

    def add(stage: str, tool: str, action: str, targets: Sequence[str], **extra):
        normalized = []
        seen = set()
        for value in targets:
            key = _path_key(value)
            if key in seen:
                continue
            seen.add(key)
            normalized.append(str(Path(value).absolute()))
        stages.append({
            "stage": stage,
            "tool": tool,
            "repair_action": action,
            "target_spms": normalized,
            "reason_codes": sorted(codes),
            "provenance_sha256": evidence_sha256,
            "parent_retry_id": str(parent_retry_id),
            "request_id": str(request_id),
            **extra,
        })

    texture = bool(codes & TEXTURE_REASON_CODES)
    atlas_manifest = bool(codes & ATLAS_MANIFEST_REPAIR_CODES)
    generator = bool(codes & GENERATOR_REASON_CODES)
    generator_cluster = bool(codes & GENERATOR_AND_CLUSTER_REASON_CODES)
    cluster_stale = bool(codes & CLUSTER_STALE_REASON_CODES)
    modeler_node_table = bool(codes & MODELER_NODE_TABLE_REASON_CODES)
    recipe_codes = codes & RECIPE_GATED_REASON_CODES
    recipe = _validated_recipe(evidence) if recipe_codes else None

    if "normalized_generator_node_table_stale" in codes:
        node_table_decision = repair_ui_decision(evidence)
        if node_table_decision["status"] == REPAIR_UI_BLOCKED:
            return RepairPlan(
                REPAIR_PLAN_SCHEMA_VERSION,
                str(request_id),
                str(parent_retry_id),
                canonical,
                evidence_sha256,
                tuple(sorted(codes)),
                (),
                False,
                STATUS_FINAL_FAILED,
                node_table_decision["reason"],
                node_table_decision["action"],
            )

    # An explicit unsupported/fatal fact always wins over a repairable token
    # found elsewhere in the same receipt.  Unclassified tokens do not mask a
    # proven repair action, but if no action is proven they fall through to the
    # visible unsupported plan below instead of disappearing at admission.
    explicit_blockers = codes & (
        UNSUPPORTED_REASON_CODES | FATAL_REASON_CODES
    )
    if recipe is not None:
        explicit_blockers.difference_update(recipe_codes)
    if explicit_blockers:
        reason, action = _unsupported_message(codes, evidence)
        return RepairPlan(
            REPAIR_PLAN_SCHEMA_VERSION, str(request_id), str(parent_retry_id),
            canonical, evidence_sha256, tuple(sorted(codes)), (), False,
            STATUS_FINAL_FAILED, reason, action,
        )
    if recipe_codes and recipe is None:
        reason, action = _unsupported_message(codes, evidence)
        return RepairPlan(
            REPAIR_PLAN_SCHEMA_VERSION, str(request_id), str(parent_retry_id),
            canonical, evidence_sha256, tuple(sorted(codes)), (), False,
            STATUS_FINAL_FAILED, reason, action,
        )

    if atlas_manifest:
        add(
            "atlas_manifest_repair",
            PCG_TEXTURE_TOOL,
            ATLAS_MANIFEST_MIRROR_REPAIR,
            [canonical],
        )
    if texture:
        add(
            "pcg_texture",
            PCG_TEXTURE_TOOL,
            STEP3_STANDARD,
            [canonical],
            force_rerender=False,
        )

    cluster_candidates = list(_cluster_paths_for_exact(evidence, canonical))
    if recipe is not None:
        cluster_candidates.extend(_candidate_paths(recipe, cluster_only=False))
    inventory = list(inventory_paths)
    exact_clusters = []
    for candidate in cluster_candidates:
        try:
            resolved = canonical_exact_spm(
                candidate,
                inventory,
                require_exists=require_exists,
            )
        except (FileNotFoundError, ValueError):
            continue
        if _path_key(resolved) != _path_key(canonical):
            exact_clusters.append(resolved)

    if modeler_node_table:
        recovery_scope = _validated_modeler_recovery_scope(
            evidence, canonical
        )
        node_provider = None
        provider_value = evidence.get("producer_spm")
        try:
            if provider_value:
                node_provider = canonical_exact_spm(
                    provider_value,
                    inventory,
                    require_exists=require_exists,
                )
        except (FileNotFoundError, ValueError):
            node_provider = None
        if node_provider is None:
            unique_clusters = {
                _path_key(candidate): candidate
                for candidate in exact_clusters
            }
            if len(unique_clusters) == 1:
                node_provider = next(iter(unique_clusters.values()))
        if (
            recovery_scope is None
            or not node_provider
            or _path_key(node_provider) == _path_key(canonical)
        ):
            return RepairPlan(
                REPAIR_PLAN_SCHEMA_VERSION,
                str(request_id),
                str(parent_retry_id),
                canonical,
                evidence_sha256,
                tuple(sorted(codes)),
                (),
                False,
                STATUS_FINAL_FAILED,
                "Node table 자동 복구의 exact target/provider 범위를 하나로 증명하지 못했습니다.",
                "fresh live audit로 대상 SPM과 현재 Cluster provider를 다시 확정하세요.",
            )
        add(
            "modeler_node_table_recovery",
            MODELER_RECOVERY_TOOL,
            MODELER_NODE_TABLE_RECOVERY,
            [canonical],
            producer_spm=node_provider,
            recovery_scope=copy.deepcopy(recovery_scope),
        )

    needs_cluster = generator_cluster or cluster_stale or recipe is not None
    cluster_targets = []
    if needs_cluster:
        cluster_targets = exact_clusters or (
            [canonical] if cluster_stale or recipe is not None else []
        )
        if not cluster_targets:
            reason = (
                "Generator repair에는 canonical Cluster 관계가 필요하지만 "
                "durable audit evidence에서 exact target을 증명하지 못했습니다."
            )
            return RepairPlan(
                REPAIR_PLAN_SCHEMA_VERSION, str(request_id), str(parent_retry_id),
                canonical, evidence_sha256, tuple(sorted(codes)), (), False,
                STATUS_FINAL_FAILED, reason,
                "fresh audit로 canonical Cluster identity/provenance를 다시 생성하세요.",
            )
    if (generator or generator_cluster) and needs_cluster:
        add(
            "generator_sync_and_cluster",
            GENERATOR_SYNC_TOOL,
            GENERATOR_SYNC_AND_CLUSTER,
            [canonical, *cluster_targets],
        )
    elif generator or generator_cluster:
        add("generator_sync", GENERATOR_SYNC_TOOL, GENERATOR_SYNC, [canonical])
    elif needs_cluster:
        add(
            "cluster_refresh",
            GENERATOR_SYNC_TOOL,
            CLUSTER_REFRESH,
            cluster_targets,
        )

    if not stages:
        reason, action = _unsupported_message(codes, evidence)
        return RepairPlan(
            REPAIR_PLAN_SCHEMA_VERSION, str(request_id), str(parent_retry_id),
            canonical, evidence_sha256, tuple(sorted(codes)), (), False,
            STATUS_FINAL_FAILED, reason, action,
        )

    # Keep the dependency order stable and collapse exact duplicate tool/action
    # units.  The same reason appearing in several nested receipts must not
    # enqueue repeated work.
    unique = []
    seen_stages = set()
    for stage in stages:
        identity = (
            stage["tool"],
            stage["repair_action"],
            tuple(_path_key(path) for path in stage["target_spms"]),
        )
        if identity not in seen_stages:
            seen_stages.add(identity)
            unique.append(stage)
    return RepairPlan(
        REPAIR_PLAN_SCHEMA_VERSION, str(request_id), str(parent_retry_id),
        canonical, evidence_sha256, tuple(sorted(codes)), tuple(unique), True,
        STATUS_PENDING,
    )


def has_repair_contract_evidence(evidence: Mapping[str, Any]) -> bool:
    """Return whether evidence contains a visible repair-policy reason.

    Registered unsupported, fatal and still-unclassified codes deliberately
    enter planning so they produce an explicit row.  Informational tokens do
    not.  This replaces the old 31-code allow-list whose miss path silently
    ``continue``-d and made most blocked targets disappear.
    """

    return bool(set(evidence_reason_codes(evidence)) & ALL_REPAIR_CONTRACT_CODES)


def stage_running_status(stage: Mapping[str, Any]) -> str:
    return {
        "atlas_manifest_repair": STATUS_ATLAS,
        "pcg_texture": STATUS_TEXTURE,
        "generator_sync": STATUS_GENERATOR,
        "generator_sync_and_cluster": STATUS_GENERATOR,
        "cluster_refresh": STATUS_CLUSTER,
        "modeler_node_table_recovery": STATUS_MODELER,
    }.get(str(stage.get("stage") or ""), STATUS_PENDING)


def repair_progress_payload(
    plan: RepairPlan | Mapping[str, Any],
    *,
    status: str,
    completed_stages: int,
    attempted_stages: Sequence[Mapping[str, Any]] = (),
    error: str = "",
) -> dict:
    metadata = plan.metadata() if isinstance(plan, RepairPlan) else copy.deepcopy(dict(plan))
    stages = list(metadata.get("stages") or ())
    return {
        "schema_version": REPAIR_RECEIPT_SCHEMA_VERSION,
        "request_id": metadata.get("request_id"),
        "parent_retry_id": metadata.get("parent_retry_id"),
        "exact_spm": metadata.get("exact_spm"),
        "status": str(status),
        "status_label": STATUS_LABELS.get(str(status), str(status)),
        "completed_stages": int(completed_stages),
        "remaining_stages": max(0, len(stages) - int(completed_stages)),
        "total_stages": len(stages),
        "attempted_stages": copy.deepcopy(list(attempted_stages)),
        "reason_codes": list(metadata.get("reason_codes") or ()),
        "error": str(error or ""),
    }


def final_failure_filter(rows: Iterable[Mapping[str, Any]]) -> list[dict]:
    """Return only unsupported or terminally re-audited failures."""

    return [
        copy.deepcopy(dict(row))
        for row in rows
        if str(row.get("status") or "") == STATUS_FINAL_FAILED
    ]


def compact_success_message(attempted_stages: Sequence[Mapping[str, Any]]) -> str:
    names = []
    stage_names = {str(row.get("stage") or "") for row in attempted_stages}
    if "pcg_texture" in stage_names:
        names.append("PCG 텍스처")
    if "atlas_manifest_repair" in stage_names:
        names.append("Atlas manifest")
    if stage_names & {"generator_sync", "generator_sync_and_cluster"}:
        names.append("Generator")
    if "modeler_node_table_recovery" in stage_names:
        names.append("SpeedTree Node table")
    if stage_names & {"cluster_refresh", "generator_sync_and_cluster"}:
        names.append("Cluster")
    names.extend(("Blender", "Unreal"))
    return "자동 복구: " + " → ".join(names) + ", 통과"


__all__ = tuple(name for name in globals() if name.isupper()) + (
    "RepairPlan",
    "build_exact_target_repair_plan",
    "canonical_exact_spm",
    "compact_success_message",
    "evidence_reason_codes",
    "final_failure_filter",
    "fresh_repair_receipt_authoritative",
    "has_repair_contract_evidence",
    "repair_ui_decision",
    "repair_progress_payload",
    "stage_running_status",
)
