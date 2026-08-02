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


REPAIR_PLAN_SCHEMA_VERSION = 1
REPAIR_RECEIPT_SCHEMA_VERSION = 1

PCG_TEXTURE_TOOL = "pcg_st9_texture_batch"
GENERATOR_SYNC_TOOL = "spm_generator_sync"

STEP3_STANDARD = "step3-standard"
GENERATOR_SYNC = "generator-sync"
CLUSTER_REFRESH = "cluster-refresh"
GENERATOR_SYNC_AND_CLUSTER = "generator-sync-and-cluster"

STATUS_PENDING = "automatic_repair_pending"
STATUS_TEXTURE = "pcg_texture_repair_running"
STATUS_GENERATOR = "generator_sync_running"
STATUS_CLUSTER = "cluster_refresh_running"
STATUS_REAUDIT = "fresh_reaudit_running"
STATUS_PIPELINE = "blender_unreal_retry_running"
STATUS_COMPLETED = "automatic_repair_completed"
STATUS_FINAL_FAILED = "final_failed"
STATUS_CANCELLED = "cancelled"

STATUS_LABELS = {
    STATUS_PENDING: "자동 복구 대기",
    STATUS_TEXTURE: "PCG 텍스처 복구 중",
    STATUS_GENERATOR: "Generator Sync 중",
    STATUS_CLUSTER: "Cluster 갱신 중",
    STATUS_REAUDIT: "재검증 중",
    STATUS_PIPELINE: "Blender-Unreal 재시도 중",
    STATUS_COMPLETED: "자동 복구 완료",
    STATUS_FINAL_FAILED: "실패",
    STATUS_CANCELLED: "자동 복구 취소",
}

# Only published reason/issue codes may select a mutation path.  Equivalent
# official spellings can be added here without changing callers or matching
# human error text.
TEXTURE_REASON_CODES = frozenset({
    "canonical_texture_output_unmapped",
    "managed_texture_set_incomplete",
    "canonical_texture_set_incomplete",
    "canonical_texture_output_stale",
    "managed_texture_set_stale",
    "source_fallback_needs_pcg_generation",
    "texture_set_incomplete",
    "texture_source_fallback_needs_pcg_generation",
})

GENERATOR_REASON_CODES = frozenset({
    "generator_slot_pair_drift",
    "atlas_generator_connection_missing",
    "normalized_prototype_zero_match",
})

GENERATOR_AND_CLUSTER_REASON_CODES = frozenset({
    "generator_connection_contract_incomplete",
    "normalized_generator_delivery_incomplete",
})

CLUSTER_STALE_REASON_CODES = frozenset({
    "cluster_stale",
    "cluster_relation_stale",
    "cluster_refresh_required",
    "normalized_generator_node_table_stale",
})

RECIPE_GATED_REASON_CODES = frozenset({
    "asset_cluster_bake_texture_contract_invalid",
    "blender_cluster_bake_map_role_mismatch",
    "atlas_strict_material_binding_conflict",
})

UNSUPPORTED_EXPORT_MATERIAL_CODES = frozenset({
    "asset_export_material_missing",
    "material_export_missing",
    "all_export_material_missing",
})

UNSUPPORTED_EXPORTER_CRASH_CODES = frozenset({
    "process_exporter_crash",
    "access_violation_exhausted",
    "0xc0000005",
})

ALL_REPAIR_CONTRACT_CODES = frozenset().union(
    TEXTURE_REASON_CODES,
    GENERATOR_REASON_CODES,
    GENERATOR_AND_CLUSTER_REASON_CODES,
    CLUSTER_STALE_REASON_CODES,
    RECIPE_GATED_REASON_CODES,
    UNSUPPORTED_EXPORT_MATERIAL_CODES,
    UNSUPPORTED_EXPORTER_CRASH_CODES,
)

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
    generator = bool(codes & GENERATOR_REASON_CODES)
    generator_cluster = bool(codes & GENERATOR_AND_CLUSTER_REASON_CODES)
    cluster_stale = bool(codes & CLUSTER_STALE_REASON_CODES)
    recipe_codes = codes & RECIPE_GATED_REASON_CODES
    recipe = _validated_recipe(evidence) if recipe_codes else None

    if codes & (UNSUPPORTED_EXPORT_MATERIAL_CODES | UNSUPPORTED_EXPORTER_CRASH_CODES):
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
    """Return whether evidence contains a reason owned by BAT orchestration."""

    return bool(set(evidence_reason_codes(evidence)) & ALL_REPAIR_CONTRACT_CODES)


def stage_running_status(stage: Mapping[str, Any]) -> str:
    return {
        "pcg_texture": STATUS_TEXTURE,
        "generator_sync": STATUS_GENERATOR,
        "generator_sync_and_cluster": STATUS_GENERATOR,
        "cluster_refresh": STATUS_CLUSTER,
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
    if stage_names & {"generator_sync", "generator_sync_and_cluster"}:
        names.append("Generator")
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
    "repair_progress_payload",
    "stage_running_status",
)
