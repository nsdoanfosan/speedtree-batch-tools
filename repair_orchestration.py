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

from atlas_producer_rebind import (
    PROOF_KIND as ATLAS_PRODUCER_RELATION_KIND,
    AtlasProducerRebindProofError,
    validate_atlas_producer_rebind_proof,
)
from atlas_slot_ownership import (
    PLAN_CONTRACT as ATLAS_SLOT_OWNERSHIP_PLAN_CONTRACT,
    AtlasSlotOwnershipError,
    validate_atlas_slot_ownership_plan,
)
from repair_reason_registry import (
    ATLAS_MIRROR_REPAIR_PLAN,
    BLOCKING_DISPOSITIONS,
    BLOCKING_REASON_CODES,
    CURRENT_MATERIAL_BINDING_RECIPE,
    DURABLE_FAILURE_REASON_CODES,
    EXACT_ATLAS_PRODUCER_RELATION,
    EXACT_CLUSTER_PROVIDER,
    EXACT_LIVE_SPM_OWNERSHIP_PLAN,
    FATAL,
    REPAIRABLE,
    SEALED_MODELER_RECOVERY_SCOPE,
    UNSUPPORTED,
    normalize_reason_code,
    present_evidence_failure,
    present_reason,
    reason_row,
)


REPAIR_PLAN_SCHEMA_VERSION = 1
REPAIR_RECEIPT_SCHEMA_VERSION = 1

PCG_TEXTURE_TOOL = "pcg_st9_texture_batch"
GENERATOR_SYNC_TOOL = "spm_generator_sync"
MODELER_RECOVERY_TOOL = "speedtree_modeler"

STEP3_STANDARD = "step3-standard"
ATLAS_MANIFEST_MIRROR_REPAIR = "atlas-manifest-mirror-repair"
ATLAS_PRODUCER_REFRESH = "atlas-producer-refresh"
ATLAS_SLOT_OWNERSHIP_RECONCILE = "atlas-slot-ownership-reconcile"
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

# Compatibility export for callers/tests that inspect the admitted vocabulary.
# The value itself is owned and constructed by the registry module.
ALL_REPAIR_CONTRACT_CODES = BLOCKING_REASON_CODES

REPAIR_UI_AUTOMATIC = "automatic_repair"
REPAIR_UI_BLOCKED = "final_blocked"

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
            if normalize_reason_code(normalized):
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


def _cluster_provider_relations_for_exact(
    evidence: Mapping[str, Any],
    exact_spm: str | Path,
) -> tuple[dict, ...]:
    """Return deduplicated sealed provider relations for this exact target."""

    exact_key = _path_key(exact_spm)
    relations: dict[str, dict] = {}
    for trail, value in _walk(evidence):
        if (
            not trail
            or trail[-1].casefold() != "cluster_provider_relations"
            or not isinstance(value, (list, tuple))
        ):
            continue
        for row in value:
            if not isinstance(row, Mapping):
                continue
            target = row.get("target_spm")
            provider = row.get("provider_spm")
            blend = row.get("provider_blend")
            if not all(isinstance(item, (str, os.PathLike)) for item in (
                target,
                provider,
                blend,
            )):
                continue
            if _path_key(target) != exact_key:
                continue
            provider_path = Path(provider)
            blend_path = Path(blend)
            if (
                not provider_path.is_absolute()
                or provider_path.suffix.casefold() != ".spm"
                or not blend_path.is_absolute()
                or blend_path.suffix.casefold() != ".blend"
                or _path_key(blend_path)
                != _path_key(provider_path.with_suffix(".blend"))
            ):
                continue
            key = _path_key(provider_path)
            candidate = copy.deepcopy(dict(row))
            existing = relations.get(key)
            if existing is not None and existing != candidate:
                return ()
            relations[key] = candidate
    return tuple(relations[key] for key in sorted(relations))


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


_DISPOSITION_PRIORITY = {FATAL: 0, UNSUPPORTED: 1, REPAIRABLE: 2}
_ACTION_PRIORITY = {
    ATLAS_MANIFEST_MIRROR_REPAIR: 0,
    ATLAS_PRODUCER_REFRESH: 1,
    ATLAS_SLOT_OWNERSHIP_RECONCILE: 2,
    STEP3_STANDARD: 3,
    MODELER_NODE_TABLE_RECOVERY: 4,
    GENERATOR_SYNC: 5,
    GENERATOR_SYNC_AND_CLUSTER: 6,
    CLUSTER_REFRESH: 7,
}


def _evidence_rows(evidence: Mapping[str, Any]):
    return tuple(
        (code, reason_row(code))
        for code in evidence_reason_codes(evidence)
    )


def _active_policy_rows(rows):
    active = [
        (code, row) for code, row in rows
        if row.disposition in BLOCKING_DISPOSITIONS
    ]
    if any(not row.fallback_only for _code, row in active):
        active = [
            (code, row) for code, row in active
            if not row.fallback_only
        ]
    return active


def _primary_policy_row(rows):
    active = _active_policy_rows(rows)
    if not active:
        return None
    return min(
        active,
        key=lambda item: (
            _DISPOSITION_PRIORITY[item[1].disposition],
            _ACTION_PRIORITY.get(item[1].repair_action, 99),
            item[0],
        ),
    )


def _validated_atlas_producer_relation(
    evidence: Mapping[str, Any],
    canonical: Path | None = None,
) -> dict[str, Any] | None:
    for trail, value in _walk(evidence):
        if not isinstance(value, Mapping):
            continue
        if (
            value.get("kind") != ATLAS_PRODUCER_RELATION_KIND
            and (not trail or trail[-1].casefold() != "atlas_producer_relation")
        ):
            continue
        try:
            return validate_atlas_producer_rebind_proof(
                value,
                canonical_spm=canonical,
            )
        except (AtlasProducerRebindProofError, OSError, ValueError):
            continue
    return None


def _validated_live_spm_ownership_plan(
    evidence: Mapping[str, Any],
    canonical: Path | None = None,
) -> dict[str, Any] | None:
    for trail, value in _walk(evidence):
        if not isinstance(value, Mapping):
            continue
        if (
            value.get("contract") != ATLAS_SLOT_OWNERSHIP_PLAN_CONTRACT
            and (
                not trail
                or trail[-1].casefold() not in {
                    "atlas_slot_ownership_plan",
                    "live_spm_ownership_plan",
                }
            )
        ):
            continue
        try:
            return validate_atlas_slot_ownership_plan(
                value,
                target_spm=canonical,
                require_repairable=True,
            )
        except (AtlasSlotOwnershipError, OSError, ValueError):
            continue
    return None


def _requirement_failure(rows, evidence, canonical=None, *, check_atlas=False):
    repairable = [row for _code, row in rows if row.disposition == REPAIRABLE]
    requirements = {
        requirement
        for row in repairable
        for requirement in row.evidence_requirements
    }
    failing = None
    if EXACT_ATLAS_PRODUCER_RELATION in requirements:
        if _validated_atlas_producer_relation(evidence, canonical) is None:
            failing = next(
                row for row in repairable
                if EXACT_ATLAS_PRODUCER_RELATION in row.evidence_requirements
            )
    if failing is None and EXACT_LIVE_SPM_OWNERSHIP_PLAN in requirements:
        if _validated_live_spm_ownership_plan(evidence, canonical) is None:
            failing = next(
                row for row in repairable
                if EXACT_LIVE_SPM_OWNERSHIP_PLAN in row.evidence_requirements
            )
    if failing is None and CURRENT_MATERIAL_BINDING_RECIPE in requirements:
        if _validated_recipe(evidence) is None:
            failing = next(
                row for row in repairable
                if CURRENT_MATERIAL_BINDING_RECIPE in row.evidence_requirements
            )
    if failing is None and SEALED_MODELER_RECOVERY_SCOPE in requirements:
        recovery = _first_nested_mapping(evidence, "stale_node_table_recovery")
        recovery_target = (
            recovery.get("target_spm") if isinstance(recovery, Mapping) else None
        )
        expected = canonical or (Path(recovery_target) if recovery_target else None)
        if expected is None or _validated_modeler_recovery_scope(
            evidence, Path(expected)
        ) is None:
            failing = next(
                row for row in repairable
                if SEALED_MODELER_RECOVERY_SCOPE in row.evidence_requirements
            )
    if failing is None and check_atlas and ATLAS_MIRROR_REPAIR_PLAN in requirements:
        valid_plan = False
        for _trail, value in _walk(evidence):
            if not isinstance(value, Mapping):
                continue
            if (
                str(value.get("reason_code") or "").casefold()
                != "atlas_manifest_mirror_conflict_repairable"
                or value.get("status") != "repairable"
            ):
                continue
            target = value.get("target_spm")
            if canonical is None or (
                target and _path_key(target) == _path_key(canonical)
            ):
                valid_plan = True
                break
        if not valid_plan:
            failing = next(
                row for row in repairable
                if ATLAS_MIRROR_REPAIR_PLAN in row.evidence_requirements
            )
    if failing is None:
        return None
    return present_evidence_failure(failing, evidence)


def repair_ui_decision(evidence: Mapping[str, Any]) -> dict[str, Any]:
    """Render the disposition, cause and action owned by the registry row."""

    codes = tuple(evidence_reason_codes(evidence))
    rows = _evidence_rows(evidence)
    primary = _primary_policy_row(rows)

    def decision(status, cause, action):
        return {
            "status": status,
            "reason": str(cause),
            "action": str(action),
            "reason_codes": tuple(sorted(codes)),
        }

    if primary is None:
        row = reason_row("registered_reason_has_no_exact_action")
        cause, action = present_reason(
            "registered_reason_has_no_exact_action", evidence
        )
        return decision(REPAIR_UI_BLOCKED, cause, action)
    code, row = primary
    cause, action = present_reason(code, evidence)
    if row.disposition != REPAIRABLE:
        return decision(REPAIR_UI_BLOCKED, cause, action)
    failed = _requirement_failure(rows, evidence)
    if failed is not None:
        return decision(REPAIR_UI_BLOCKED, *failed)
    return decision(REPAIR_UI_AUTOMATIC, cause, action)


def _unsupported_message(codes: set[str], evidence: Mapping[str, Any]) -> tuple[str, str]:
    primary = _primary_policy_row(
        tuple((code, reason_row(code)) for code in sorted(codes))
    )
    if primary is None:
        row = reason_row("registered_reason_has_no_exact_action")
        return present_reason("registered_reason_has_no_exact_action", evidence)
    return present_reason(primary[0], evidence)


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
    rows = tuple((code, reason_row(code)) for code in sorted(codes))
    active_rows = tuple(_active_policy_rows(rows))
    repairable_rows = tuple(
        row for _code, row in active_rows if row.disposition == REPAIRABLE
    )
    repair_actions = {row.repair_action for row in repairable_rows}
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

    texture = STEP3_STANDARD in repair_actions
    atlas_manifest = ATLAS_MANIFEST_MIRROR_REPAIR in repair_actions
    atlas_producer = ATLAS_PRODUCER_REFRESH in repair_actions
    atlas_slot_ownership = (
        ATLAS_SLOT_OWNERSHIP_RECONCILE in repair_actions
    )
    generator = GENERATOR_SYNC in repair_actions
    generator_cluster = GENERATOR_SYNC_AND_CLUSTER in repair_actions
    cluster_stale = CLUSTER_REFRESH in repair_actions
    modeler_node_table = MODELER_NODE_TABLE_RECOVERY in repair_actions
    requirements = {
        requirement
        for row in repairable_rows
        for requirement in row.evidence_requirements
    }
    recipe = (
        _validated_recipe(evidence)
        if CURRENT_MATERIAL_BINDING_RECIPE in requirements
        else None
    )

    # The registry row is the only disposition authority.  Unknown runtime
    # codes resolve to a synthetic unsupported row, so they cannot disappear
    # even before the source-coverage CI catches the missing registration.
    explicit_blockers = [
        (code, row) for code, row in active_rows
        if row.disposition in {UNSUPPORTED, FATAL}
    ]
    if explicit_blockers:
        reason, action = _unsupported_message(codes, evidence)
        return RepairPlan(
            REPAIR_PLAN_SCHEMA_VERSION, str(request_id), str(parent_retry_id),
            canonical, evidence_sha256, tuple(sorted(codes)), (), False,
            STATUS_FINAL_FAILED, reason, action,
        )

    failed_requirement = _requirement_failure(
        rows,
        evidence,
        canonical,
        check_atlas=True,
    )
    if failed_requirement is not None:
        reason, action = failed_requirement
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
    if atlas_producer:
        add(
            "atlas_producer_refresh",
            PCG_TEXTURE_TOOL,
            ATLAS_PRODUCER_REFRESH,
            [canonical],
            producer_relation=_validated_atlas_producer_relation(
                evidence, Path(canonical)
            ),
        )
    if atlas_slot_ownership:
        add(
            "atlas_slot_ownership_reconcile",
            GENERATOR_SYNC_TOOL,
            ATLAS_SLOT_OWNERSHIP_RECONCILE,
            [canonical],
            ownership_plan=_validated_live_spm_ownership_plan(
                evidence, Path(canonical)
            ),
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
    sealed_cluster_relations = _cluster_provider_relations_for_exact(
        evidence,
        canonical,
    )
    normalized_provider_refresh = bool(
        sealed_cluster_relations
        and cluster_stale
        and codes.intersection({
            "normalized_variants_required",
            "normalized_variants_stale",
        })
    )
    if CURRENT_MATERIAL_BINDING_RECIPE in requirements and recipe is not None:
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
            failing = next(
                row for row in repairable_rows
                if EXACT_CLUSTER_PROVIDER in row.evidence_requirements
            )
            reason, action = present_evidence_failure(failing, evidence)
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
                reason,
                action,
            )
        add(
            "modeler_node_table_recovery",
            MODELER_RECOVERY_TOOL,
            MODELER_NODE_TABLE_RECOVERY,
            [canonical],
            producer_spm=node_provider,
            recovery_scope=copy.deepcopy(recovery_scope),
        )

    needs_cluster = generator_cluster or cluster_stale
    cluster_targets = []
    if needs_cluster:
        cluster_targets = (
            [canonical]
            if normalized_provider_refresh
            else exact_clusters or ([canonical] if cluster_stale else [])
        )
        if not cluster_targets:
            primary = _primary_policy_row(rows)
            row = primary[1] if primary is not None else reason_row(
                "registered_reason_has_no_exact_action"
            )
            reason, action = present_evidence_failure(row, evidence)
            return RepairPlan(
                REPAIR_PLAN_SCHEMA_VERSION, str(request_id), str(parent_retry_id),
                canonical, evidence_sha256, tuple(sorted(codes)), (), False,
                STATUS_FINAL_FAILED, reason,
                action,
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
        extra = {}
        if normalized_provider_refresh:
            extra["cluster_provider_relations"] = list(
                sealed_cluster_relations
            )
        add(
            "cluster_refresh",
            GENERATOR_SYNC_TOOL,
            CLUSTER_REFRESH,
            cluster_targets,
            **extra,
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

    Registered unsupported/fatal codes and unknown runtime tokens deliberately
    enter planning so they produce an explicit row.  Informational tokens do
    not.  This replaces the old allow-list whose miss path silently
    ``continue``-d and made most blocked targets disappear.
    """

    return any(
        reason_row(code).disposition in BLOCKING_DISPOSITIONS
        for code in evidence_reason_codes(evidence)
    )


def stage_running_status(stage: Mapping[str, Any]) -> str:
    return {
        "atlas_manifest_repair": STATUS_ATLAS,
        "atlas_producer_refresh": STATUS_ATLAS,
        "atlas_slot_ownership_reconcile": STATUS_ATLAS,
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
