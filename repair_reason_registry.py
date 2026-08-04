"""One disposition per reason code the pipeline can block on.

Before this registry existed the repair planner owned 31 codes in five private
frozensets, the runtime-normalized scan found 226 production codes, and nothing
compared the two lists.  The original lowercase-only scan undercounted that
surface by 27 codes -- so `has_repair_contract_evidence()` skipped
almost every blocked target before a plan was ever built, and the block reached
the operator as a terminal failure with no recovery path and no record that one
could have existed.

The registry exists so that set can never drift unobserved again:

* `repairable`   -- the exact-target repair planner owns a recovery for it.
* `unsupported`  -- no automatic path; the operator gets a friendly action.
* `fatal`        -- real data damage; automatic recovery would hide it.
* `informational`-- a fact/wrapper that cannot itself terminate a target.

Every blocking row also owns its operator-facing cause/action.  Repairable
rows additionally name the existing exact-target BAT action and the canonical
evidence that must be present before that action can be scheduled.  There is
no runtime ``unclassified`` disposition: an unknown token resolves to one
friendly, fail-closed unsupported row while the source scanner makes the same
omission a CI failure.

The scan now follows conditional values, reason-parameter helper calls, named
failure-kind sets, and scope-local names assigned a literal -- the
`reason = "..."` then `{"reason": reason}` shape that hid a whole family of
gates.  That expanded the statically visible surface to 366.  It is paired
with a sanitized snapshot of 20 tokens observed in durable queue state,
because source possibility and observed runtime state catch different
omissions.

`tests/test_repair_reason_registry.py` fails when a module emits a code that is
absent here, when a `repairable` code is never emitted (dead vocabulary -- the
defect this registry was written to expose), or when a blocking row lacks a
complete domain contract.  A new block therefore forces a decision at review
time instead of at 3am in front of a stuck batch.
"""
from __future__ import annotations

import re
from typing import Any, Mapping, NamedTuple


REPAIRABLE = "repairable"
UNSUPPORTED = "unsupported"
FATAL = "fatal"
UNCLASSIFIED = "unclassified"
INFORMATIONAL = "informational"

DISPOSITIONS = frozenset({
    REPAIRABLE,
    UNSUPPORTED,
    FATAL,
    INFORMATIONAL,
})

REASON_CODE_TOKEN = re.compile(r"^[a-z][a-z0-9_]{2,60}$")


def normalize_reason_code(value) -> str:
    """Normalize one structured token, excluding prose, paths and error text."""

    normalized = str(value or "").strip().casefold()
    return normalized if REASON_CODE_TOKEN.fullmatch(normalized) else ""


class ReasonRow(NamedTuple):
    """How the pipeline must treat one reason code, and who emits it."""

    disposition: str
    owner: str
    policy: str = ""
    repair_action: str = ""
    evidence_requirements: tuple[str, ...] = ()
    friendly_cause: str = ""
    operator_action: str = ""
    evidence_failure_cause: str = ""
    evidence_failure_action: str = ""
    fallback_only: bool = False
    phase_routed: bool = False


class ReasonPolicy(NamedTuple):
    """Reusable domain semantics copied into every exported registry row."""

    repair_action: str = ""
    evidence_requirements: tuple[str, ...] = ()
    friendly_cause: str = ""
    operator_action: str = ""
    evidence_failure_cause: str = ""
    evidence_failure_action: str = ""
    fallback_only: bool = False
    phase_routed: bool = False


STEP3_STANDARD_ACTION = "step3-standard"
ATLAS_MANIFEST_MIRROR_REPAIR_ACTION = "atlas-manifest-mirror-repair"
GENERATOR_SYNC_ACTION = "generator-sync"
CLUSTER_REFRESH_ACTION = "cluster-refresh"
GENERATOR_SYNC_AND_CLUSTER_ACTION = "generator-sync-and-cluster"
MODELER_NODE_TABLE_RECOVERY_ACTION = "modeler-node-table-recovery"

EXACT_INVENTORY_SPM = "exact_inventory_spm"
EXACT_CLUSTER_RELATION = "exact_cluster_relation"
CURRENT_MATERIAL_BINDING_RECIPE = "current_material_binding_recipe"
ATLAS_MIRROR_REPAIR_PLAN = "atlas_mirror_repair_plan"
SEALED_MODELER_RECOVERY_SCOPE = "sealed_modeler_recovery_scope"
EXACT_CLUSTER_PROVIDER = "exact_cluster_provider"


def _automatic(action, requirements, cause, operator_action, *,
               evidence_failure_cause="", evidence_failure_action=""):
    return ReasonPolicy(
        action,
        tuple(requirements),
        cause,
        operator_action,
        evidence_failure_cause,
        evidence_failure_action,
    )


def _terminal(
    cause,
    operator_action,
    *,
    fallback_only=False,
    phase_routed=False,
):
    return ReasonPolicy(
        friendly_cause=cause,
        operator_action=operator_action,
        fallback_only=fallback_only,
        phase_routed=phase_routed,
    )


def _informational():
    return ReasonPolicy()


POLICY_CONTRACTS = {
    "pcg_texture": _automatic(
        STEP3_STANDARD_ACTION,
        (EXACT_INVENTORY_SPM,),
        "canonical PCG 텍스처 출력이 없거나 현재 입력보다 오래되었습니다.",
        "exact PCG BAT로 해당 SPM의 텍스처만 복구한 뒤 material preflight를 다시 실행합니다.",
    ),
    "atlas_manifest": _automatic(
        ATLAS_MANIFEST_MIRROR_REPAIR_ACTION,
        (EXACT_INVENTORY_SPM, ATLAS_MIRROR_REPAIR_PLAN),
        "동일 원본의 낡은 Atlas manifest 미러가 exact authority와 충돌합니다.",
        "exact BAT가 증명된 낡은 미러만 갱신하고 Canonical PCG → Atlas 계약을 다시 검사합니다.",
        evidence_failure_cause="Atlas mirror 복구 가능성을 증명하는 exact authority 계획이 없습니다.",
        evidence_failure_action="fresh Atlas manifest resolution으로 authority와 stale mirror 범위를 다시 확정하세요.",
    ),
    "atlas_manifest_canonical_name": _automatic(
        ATLAS_MANIFEST_MIRROR_REPAIR_ACTION,
        (EXACT_INVENTORY_SPM, ATLAS_MIRROR_REPAIR_PLAN),
        "Atlas 영수증이 Cluster 출력의 legacy 이름으로만 존재해, "
        "canonical 이름으로 찾는 Push 검사가 이를 보지 못합니다.",
        "exact BAT가 선택된 영수증을 canonical 이름으로 그대로 복제한 뒤 "
        "Atlas 계약을 다시 검사합니다.",
        evidence_failure_cause=(
            "canonical 이름 복제의 근거가 되는 Cluster pair 정규화 영수증을 "
            "증명하지 못했습니다."
        ),
        evidence_failure_action=(
            "cluster_spm_pair 영수증을 다시 생성해 canonical/legacy 동일성을 "
            "증명한 뒤 다시 검사하세요."
        ),
    ),
    "generator": _automatic(
        GENERATOR_SYNC_ACTION,
        (EXACT_INVENTORY_SPM,),
        "Generator 전달 계약이 현재 SPM과 일치하지 않습니다.",
        "exact Generator Sync를 실행하고 현재 연결을 다시 검사합니다.",
    ),
    "generator_cluster": _automatic(
        GENERATOR_SYNC_AND_CLUSTER_ACTION,
        (EXACT_INVENTORY_SPM, EXACT_CLUSTER_RELATION),
        "Generator 전달과 Cluster 관계 갱신이 필요합니다.",
        "exact Generator Sync와 Cluster 갱신을 순서대로 실행한 뒤 live delivery를 다시 검사합니다.",
        evidence_failure_cause="Generator 복구에 필요한 canonical Cluster exact relation target을 증명하지 못했습니다.",
        evidence_failure_action="fresh audit으로 canonical Cluster identity와 target relation을 다시 생성하세요.",
    ),
    "cluster_refresh": _automatic(
        CLUSTER_REFRESH_ACTION,
        (EXACT_INVENTORY_SPM, EXACT_CLUSTER_RELATION),
        "Cluster 결과 또는 그 source/receipt 증거가 현재 입력과 일치하지 않습니다.",
        "exact Cluster 관계를 갱신한 뒤 현재 결과를 다시 검사합니다.",
        evidence_failure_cause="Cluster 갱신의 exact relation target을 증명하지 못했습니다.",
        evidence_failure_action="fresh audit으로 canonical Cluster identity와 target relation을 다시 생성하세요.",
    ),
    "cluster_refresh_atlas_authority": _automatic(
        CLUSTER_REFRESH_ACTION,
        (EXACT_INVENTORY_SPM, EXACT_CLUSTER_RELATION),
        "현재 대상의 Atlas producer 영수증이 없습니다.",
        "exact Cluster 관계를 갱신해 current producer 영수증을 다시 만든 뒤 검사합니다.",
    ),
    "cluster_refresh_atlas_conflict": _automatic(
        CLUSTER_REFRESH_ACTION,
        (EXACT_INVENTORY_SPM, EXACT_CLUSTER_RELATION),
        "현재 Atlas producer 영수증들이 서로 충돌합니다.",
        "exact Cluster 관계를 갱신해 target-local authority를 다시 확정한 뒤 검사합니다.",
    ),
    "cluster_refresh_lineage": _automatic(
        CLUSTER_REFRESH_ACTION,
        (EXACT_INVENTORY_SPM, EXACT_CLUSTER_RELATION),
        "일부 Atlas asset의 current producer 계보가 증명되지 않았습니다.",
        "exact Cluster 관계를 갱신해 Material/Mesh lineage를 다시 증명한 뒤 검사합니다.",
    ),
    "cluster_refresh_variants_required": _automatic(
        CLUSTER_REFRESH_ACTION,
        (EXACT_INVENTORY_SPM, EXACT_CLUSTER_RELATION),
        "필수 정규화 Cluster variant가 아직 생성되지 않았습니다.",
        "exact Cluster 관계를 갱신해 필요한 normalized variant만 생성한 뒤 검사합니다.",
    ),
    "cluster_refresh_recipe": _automatic(
        CLUSTER_REFRESH_ACTION,
        (EXACT_INVENTORY_SPM, EXACT_CLUSTER_RELATION,
         CURRENT_MATERIAL_BINDING_RECIPE),
        "Cluster bake texture binding이 current material recipe와 다릅니다.",
        "증명된 current recipe로 exact Cluster 관계를 갱신한 뒤 다시 검사합니다.",
        evidence_failure_cause="권위 있는 current material binding recipe가 없습니다.",
        evidence_failure_action="material ID와 map role을 포함한 canonical binding recipe를 fresh audit에서 다시 생성하세요.",
    ),
    "modeler_node_table": _automatic(
        MODELER_NODE_TABLE_RECOVERY_ACTION,
        (EXACT_INVENTORY_SPM, SEALED_MODELER_RECOVERY_SCOPE,
         EXACT_CLUSTER_PROVIDER),
        "SpeedTree Generator Node table이 오래되었습니다.",
        "BAT가 소유한 Modeler 세션에서 해당 SPM만 다시 저장하고 live audit을 재실행합니다.",
        evidence_failure_cause="SpeedTree Node table이 오래되었지만 자동 저장할 exact target 범위가 증명되지 않았습니다.",
        evidence_failure_action="fresh live audit으로 대상 SPM, provider, Mesh ID 복구 범위를 다시 확정하세요.",
    ),
    "atlas_ownership": _terminal(
        "서로 다른 원본·export 범위 또는 ownership claim이 같은 Atlas 대상을 소유한다고 기록되어 있습니다.",
        "충돌한 manifest의 원본과 소유 범위를 확인해야 하며 BAT가 한쪽을 임의로 덮어쓰지 않습니다. 자동으로 덮어쓸 수 없습니다.",
    ),
    "export_material": _terminal(
        "SpeedTree 내보내기에 재질이 없습니다.",
        "SpeedTree Modeler에서 표시된 Generator에 재질을 지정하고 FBX/STMAT를 다시 내보낸 뒤 재검사하세요.",
    ),
    "exporter_crash": _terminal(
        "SpeedTree exporter가 반복해서 access violation으로 종료되었습니다.",
        "해당 exact SPM의 재질과 Generator 상태를 Modeler에서 확인한 뒤 수동 export를 시험하세요.",
    ),
    "cluster_data": _terminal(
        "Cluster TGA 참조가 canonical SPM 파일 규칙과 다릅니다.",
        "표시된 TGA를 복원하거나 canonical basename으로 다시 생성한 뒤 live audit을 재실행하세요.",
    ),
    "cluster_handoff": _terminal(
        "PCG Cluster handoff가 현재 실행 가능한 상태가 아닙니다.",
        "handoff 영수증의 상세 원인을 확인하고 누락된 Cluster 입력 또는 bark 정규화를 완료한 뒤 재검사하세요.",
    ),
    "preflight_error": _terminal(
        "SpeedTree 재질 사전 검사 자체가 완료되지 않았습니다.",
        "사전 검사 보고서의 원본 오류를 확인한 뒤 검사를 다시 실행하세요.",
    ),
    "modeler_recovery_scope": _terminal(
        "SpeedTree Node table 복구에 필요한 exact 대상 범위 증거가 없거나 손상되었습니다.",
        "fresh live audit으로 대상 SPM, provider, Mesh ID 범위를 확인해 복구 범위를 다시 확정한 뒤 재시도하세요.",
    ),
    "failure_wrapper": _terminal(
        "이전 자동 복구가 실패했지만 재시도할 구체 원인이 영수증에 남지 않았습니다.",
        "fresh audit으로 실제 원인 코드를 다시 생성한 뒤 해당 exact 복구를 재시도하세요.",
    ),
    "visible_generator_pair": _terminal(
        "표시되는 Generator가 해당 Material이 소유하지 않는 Mesh를 참조합니다.",
        "오류 행의 Generator, Material ID, Mesh ID를 확인해 visible 연결을 수정한 뒤 재검사하세요.",
    ),
    "manual_asset_reference": _terminal(
        "필수 외부 asset 참조가 없거나 현재 authoring 범위와 일치하지 않습니다.",
        "registry owner가 표시한 source path/asset ID를 복원하거나 Modeler에서 exact 참조를 다시 연결한 뒤 재검사하세요.",
    ),
    "atlas_integrity_wrapper": _terminal(
        "Atlas-managed Material/Mesh 무결성 검사에서 하나 이상의 blocker가 발견되었습니다.",
        "integrity_issues의 code와 asset ID를 확인하고, repairable 하위 원인은 exact Cluster refresh로, 나머지는 owner marker/Generator 연결을 수정하세요.",
    ),
    "cluster_bake_origin": _terminal(
        "Blender Cluster bake output의 origin receipt가 현재 SPM/slot 범위를 증명하지 못합니다.",
        "정확한 material ID와 map index로 bake receipt를 다시 생성한 뒤 material preflight를 재실행하세요.",
    ),
    "provisional_texture_source": _terminal(
        "관리 대상 material이 허용되지 않은 provisional texture source를 참조합니다.",
        "해당 source를 canonical input으로 선언하거나 material reference를 안전한 source로 수정한 뒤 PCG Texture를 다시 실행하세요.",
    ),
    "cluster_source_missing": _terminal(
        "Cluster exact action에 필요한 canonical source 또는 target 파일이 없습니다.",
        "owner module이 표시한 canonical SPM/target/source 파일을 복원하고 relation inventory를 다시 검사하세요.",
    ),
    "cluster_source_integrity": _terminal(
        "Cluster source 또는 receipt의 identity/semantic 증거가 손상되거나 서로 충돌합니다.",
        "source SPM과 receipt를 fresh audit으로 다시 읽고, 충돌한 authority를 수동으로 정리한 뒤 재시도하세요.",
    ),
    "pcg_audit_runtime": _terminal(
        "PCG texture/Blend source audit가 취소 이외의 오류로 완료되지 않았습니다.",
        "표시된 audit stage와 process receipt를 확인하고 exact 대상 audit를 다시 실행하세요.",
    ),
    "atlas_asset_authoring": _terminal(
        "Atlas-managed asset의 marker, owner scope 또는 Generator pair가 모호합니다.",
        "표시된 Material/Mesh/Generator ID의 owner marker와 연결을 수정한 뒤 integrity audit를 재실행하세요.",
    ),
    "node_scope_evidence": _terminal(
        "Node-table recovery의 required-live material scope 증거가 불완전합니다.",
        "fresh live audit으로 required target/material scope를 다시 봉인한 뒤 복구를 재시도하세요.",
    ),
    "texture_authoring": _terminal(
        "SpeedTree texture slot 또는 isolated rebase의 canonical 역할을 결정할 수 없습니다.",
        "표시된 Material ID, map slot, authored reference를 수정한 뒤 PCG Texture audit를 재실행하세요.",
    ),
    "manifest_corruption": _terminal(
        "canonical texture manifest 또는 SPM material scope가 읽을 수 없거나 schema 계약과 다릅니다.",
        "손상된 manifest/SPM을 권위 있는 source에서 복원하고 schema와 material ID를 확인한 뒤 재생성하세요.",
    ),
    "texture_scope_integrity": _terminal(
        "canonical texture manifest의 asset/root/origin 범위가 production SPM과 일치하지 않습니다.",
        "manifest를 임의로 덮어쓰지 말고 production asset root와 canonical output ownership을 수동으로 바로잡으세요.",
    ),
    "output_identity": _terminal(
        "PCG Step 3 exact output identity가 없어 안전한 대상 경로를 만들 수 없습니다.",
        "texture base와 output directory를 다시 선택한 뒤 exact Step 3를 재실행하세요.",
    ),
    "canonical_material_mapping": _terminal(
        "canonical output manifest가 일부 production Material을 고유하게 매핑하지 못했습니다.",
        "누락된 Material ID/name target을 manifest producer에서 확정한 뒤 다시 promote하세요.",
    ),
    "generator_authority": _terminal(
        "Generator reference repair에 필요한 authoritative property pair가 없습니다.",
        "fresh SPM audit으로 정확한 Generator GUID/property pair를 확보한 뒤 repair plan을 다시 만드세요.",
    ),
    "worker_runtime": _terminal(
        "exact worker가 완료 영수증을 반환하기 전에 종료되거나 대기 단계가 실패했습니다.",
        "worker log와 queue ownership을 확인하고 fresh exact job으로 재시도하세요.",
    ),
    "report_missing": _terminal(
        "child pipeline이 필수 structured report를 남기지 않았습니다.",
        "child log와 report destination 권한을 확인한 뒤 exact job을 다시 실행하세요.",
    ),
    "export_output_missing": _terminal(
        "SpeedTree exporter가 성공 코드를 반환했지만 필수 output 파일을 만들지 않았습니다.",
        "Modeler에서 exact asset의 export 경로와 재질/Generator 상태를 확인한 뒤 수동 export를 시험하세요.",
    ),
    "schema_contract": _terminal(
        "Atlas manifest의 명시적 schema version이 손상되었거나 지원 범위를 벗어났습니다.",
        "지원되는 producer로 manifest를 다시 생성하고 기존 unknown-schema 파일은 보존하여 검토하세요.",
    ),
    "generic_terminal": _terminal(
        "등록된 자동 BAT가 이 blocker를 안전하게 복구할 수 없습니다.",
        "registry owner가 표시한 audit evidence를 확인하고 원본 authoring 또는 실행 환경을 수정한 뒤 재검사하세요.",
    ),
    "dependency_output_missing": _terminal(
        "필수 producer 산출물이 없습니다.",
        "오류 행에 표시된 producer의 exact 산출물과 content key를 다시 생성한 뒤 해당 consumer만 재검사하세요.",
    ),
    "dependency_output_stale": _terminal(
        "필수 producer 산출물이 현재 입력보다 오래되었습니다.",
        "오류 행에 표시된 producer의 exact 산출물과 content key를 다시 생성한 뒤 해당 consumer만 재검사하세요.",
    ),
    "data_failure": _terminal(
        "작업 데이터 처리에 실패했습니다.",
        "같은 행의 structured evidence와 report 경로에서 실제 하위 원인을 확인한 뒤 해당 대상만 다시 실행하세요.",
        fallback_only=True,
        phase_routed=True,
    ),
    "internal_failure": _terminal(
        "BAT 내부 실행 오류가 발생했습니다.",
        "같은 행의 structured evidence와 report 경로에서 실제 하위 원인을 확인한 뒤 해당 대상만 다시 실행하세요.",
        fallback_only=True,
        phase_routed=True,
    ),
    "unreal_unavailable_failure": _terminal(
        "Unreal 실행 상태가 현재 Push 방식과 맞지 않습니다.",
        "현재 Push transport와 Unreal 실행 상태를 맞춘 뒤 해당 대상만 다시 실행하세요.",
        fallback_only=True,
        phase_routed=True,
    ),
    "dependency_failure_wrapper": _terminal(
        "필수 Cluster 복구 단계가 완료되지 않았습니다.",
        "같은 evidence의 실제 Cluster 원인 코드를 먼저 해결한 뒤 해당 consumer만 다시 실행하세요.",
        fallback_only=True,
    ),
    "phase_failure_fallback": _terminal(
        "파이프라인이 구체적인 하위 원인 없이 실패로 종료되었습니다.",
        "phase-aware retry 분류로 해당 대상을 다시 검사하고 남은 Blender/Unreal 단계만 실행하세요.",
        fallback_only=True,
        phase_routed=True,
    ),
    "owner_lost_failure": _terminal(
        "작업의 queue/process owner가 종료되어 완료 영수증을 확정하지 못했습니다.",
        "현재 owner와 queue 상태를 다시 검사한 뒤 phase-aware retry로 해당 대상만 재실행하세요.",
        fallback_only=True,
        phase_routed=True,
    ),
    "planning_snapshot_missing": _terminal(
        "선택한 대상이 retry planning snapshot에서 사라졌습니다.",
        "목록을 새로 검사해 canonical SPM identity를 갱신한 뒤 다시 실행하세요.",
    ),
    "spm_concurrent_edit": _terminal(
        "SPM을 갱신하는 동안 다른 프로세스가 같은 파일을 변경했습니다.",
        "외부 편집을 보존한 채 다른 writer를 종료하고 SPM을 다시 검사한 뒤 재시도하세요.",
    ),
    "interrupted_calibration": _terminal(
        "중단된 SPM calibration을 기록된 backup으로 안전하게 복구하지 못했습니다.",
        "calibration marker와 backup/source hash를 확인해 원본을 복원한 뒤 다시 검사하세요.",
    ),
}

# Existing decided families whose operator wording does not need a distinct
# repair action still get an explicit policy contract.  Keeping these aliases
# here makes the exported row self-contained while avoiding GUI-local text
# tables that can drift from the disposition.
for _policy_name in (
    "asset_missing", "atlas_integrity", "cluster_identity",
    "cluster_integrity", "dependency_provenance", "material_contract",
    "pipeline_contract", "blender_cluster_bake", "cluster_authoring",
    "export_hierarchy", "export_inspection", "exporter_process",
    "lifecycle_owner_lost", "process_lifecycle", "receipt_persistence",
    "repair_execution", "repair_plan", "retry_evidence",
    "unreal_retry_evidence",
):
    POLICY_CONTRACTS.setdefault(_policy_name, POLICY_CONTRACTS["generic_terminal"])

for _policy_name in (
    "blender_rebuild_route", "current_atlas_authority",
    "current_authority_variant", "current_texture_output",
    "dependency_wrapper", "diagnostic_shadow", "durable_status",
    "lifecycle_cancelled", "nonparticipating_material",
    "protected_manual_asset", "receipt_status", "recovery_verified",
    "request_provenance", "retry_route_excluded", "retry_route_status",
    "unmanaged_texture", "unreal_only_route", "unreal_validation_route",
    "valid_marker", "atlas_candidate_diagnostic",
    "diagnostic_integrity_field", "material_export_scope_diagnostic",
    "relation_decision_diagnostic", "lifecycle_event",
    "cluster_handoff_diagnostic", "prepared_unused", "audit_detail",
    "dependency_lifecycle", "generator_reference_mutation",
):
    POLICY_CONTRACTS.setdefault(_policy_name, _informational())


_REASON_SEEDS: dict[str, ReasonRow] = {
    "access_violation_exhausted": ReasonRow(
        UNSUPPORTED, "sk_batch/spm_audit.py", "exporter_crash",
    ),
    "all_export_inspection_error": ReasonRow(
        UNSUPPORTED, "sk_batch/jobs/speedtree_material_preflight.py",
        "export_inspection",
    ),
    "actionable_role_has_no_current_atlas_normalized_variants": ReasonRow(
        UNCLASSIFIED, "pcg_st9_texture_batch/pcg_cluster_assembly_contract.py", "",
    ),
    "all_consumers_planned_excluded": ReasonRow(
        UNCLASSIFIED, "sk_batch/sk_batch_gui.pyw", "",
    ),
    "ambiguous_complete_sets": ReasonRow(
        UNSUPPORTED, "speedtree_texture_contract.py",
        "texture_authoring",
    ),
    "assembly_source_fbx_pending_export": ReasonRow(
        UNCLASSIFIED, "sk_batch/cluster_assembly_handoff_contract.py", "",
    ),
    "asset_audit_failed": ReasonRow(
        UNCLASSIFIED, "pcg_st9_texture_batch/pcg_texture_audit.py", "",
    ),
    "asset_cluster_bake_texture_contract_invalid": ReasonRow(
        UNSUPPORTED, "sk_batch/jobs/speedtree_material_preflight.py", "recipe_gated",
    ),
    "asset_dependency_error": ReasonRow(
        UNSUPPORTED, "sk_batch/sk_batch_gui.pyw",
        "cluster_source_missing",
    ),
    "asset_export_material_missing": ReasonRow(
        UNSUPPORTED, "sk_batch/jobs/speedtree_material_preflight.py", "export_material",
    ),
    "asset_external_mesh_path_missing": ReasonRow(
        UNCLASSIFIED, "sk_batch/jobs/speedtree_material_preflight.py", "",
    ),
    "asset_registration_only": ReasonRow(
        INFORMATIONAL, "sk_batch/cluster_assembly_handoff_contract.py",
        "cluster_handoff_diagnostic",
    ),
    "asset_texture_source_path_missing": ReasonRow(
        UNCLASSIFIED, "sk_batch/spm_leaf_handoff_contract.py", "",
    ),
    "asset_texture_source_undeclared": ReasonRow(
        UNCLASSIFIED, "sk_batch/spm_leaf_handoff_contract.py", "",
    ),
    "atlas_blend_missing": ReasonRow(
        UNCLASSIFIED, "pcg_st9_texture_batch/pcg_texture_audit.py", "",
    ),
    "atlas_blender_source_index_invalid": ReasonRow(
        UNCLASSIFIED, "cluster_atlas_source_index.py", "",
    ),
    "atlas_managed_asset_integrity_stale": ReasonRow(
        UNCLASSIFIED, "sk_batch/jobs/speedtree_material_preflight.py", "",
    ),
    "atlas_manifest_current": ReasonRow(
        UNCLASSIFIED, "atlas_manifest_resolver.py", "",
    ),
    "atlas_manifest_candidate_conflict": ReasonRow(
        UNSUPPORTED, "sk_batch/jobs/speedtree_material_preflight.py",
        "atlas_ownership",
    ),
    "atlas_manifest_mirror_conflict_repairable": ReasonRow(
        REPAIRABLE, "atlas_manifest_resolver.py", "atlas_manifest",
    ),
    # The records resolve, but only under the Cluster output's legacy name.
    # Every reader that looks a manifest up by the canonical file's own stem
    # -- the Blender push gate first -- still misses, so completing the rename
    # is exactly the same bounded mirror write.
    "atlas_manifest_canonical_name_missing": ReasonRow(
        REPAIRABLE, "atlas_manifest_resolver.py", "atlas_manifest_canonical_name",
    ),
    "atlas_manifest_ownership_conflict": ReasonRow(
        UNSUPPORTED, "atlas_manifest_resolver.py", "atlas_ownership",
    ),
    "atlas_ownership_marker_invalid": ReasonRow(
        UNCLASSIFIED, "sk_batch/atlas_consumer_integrity.py", "",
    ),
    "atlas_ownership_provenance_mismatch": ReasonRow(
        FATAL, "sk_batch/jobs/speedtree_material_preflight.py",
        "atlas_integrity",
    ),
    "authoritative_managed_mesh_sentinel": ReasonRow(
        INFORMATIONAL, "pcg_st9_texture_batch/spm_generator_reference_repair.py",
        "generator_reference_mutation",
    ),
    "authoritative_managed_ordinal_mismatch": ReasonRow(
        INFORMATIONAL, "pcg_st9_texture_batch/spm_generator_reference_repair.py",
        "generator_reference_mutation",
    ),
    "authoritative_managed_to_source_restore": ReasonRow(
        INFORMATIONAL, "pcg_st9_texture_batch/spm_generator_reference_repair.py",
        "generator_reference_mutation",
    ),
    "authoritative_material_mesh_mismatch": ReasonRow(
        INFORMATIONAL, "pcg_st9_texture_batch/spm_generator_reference_repair.py",
        "generator_reference_mutation",
    ),
    "authoritative_missing_mesh_reference": ReasonRow(
        INFORMATIONAL, "pcg_st9_texture_batch/spm_generator_reference_repair.py",
        "generator_reference_mutation",
    ),
    "authoritative_property_pair_missing": ReasonRow(
        UNCLASSIFIED, "pcg_st9_texture_batch/spm_generator_reference_repair.py", "",
    ),
    "authoritative_source_mesh_sentinel_restore": ReasonRow(
        INFORMATIONAL, "pcg_st9_texture_batch/spm_generator_reference_repair.py",
        "generator_reference_mutation",
    ),
    "authoritative_source_missing_mesh_restore": ReasonRow(
        INFORMATIONAL, "pcg_st9_texture_batch/spm_generator_reference_repair.py",
        "generator_reference_mutation",
    ),
    "authoritative_source_ordinal_restore": ReasonRow(
        INFORMATIONAL, "pcg_st9_texture_batch/spm_generator_reference_repair.py",
        "generator_reference_mutation",
    ),
    "authoritative_source_to_managed": ReasonRow(
        INFORMATIONAL, "pcg_st9_texture_batch/spm_generator_reference_repair.py",
        "generator_reference_mutation",
    ),
    "automatic_repair_cancelled": ReasonRow(
        UNCLASSIFIED, "sk_batch/sk_batch_gui.pyw", "",
    ),
    "automatic_retry_cancelled": ReasonRow(
        UNCLASSIFIED, "sk_batch/sk_batch_gui.pyw", "",
    ),
    "basename_or_suffix_mismatch": ReasonRow(
        UNSUPPORTED, "pcg_st9_texture_batch/pcg_cluster_assembly_contract.py",
        "cluster_data",
    ),
    "before_marker_restore": ReasonRow(
        UNCLASSIFIED, "sk_batch/spm_problem_node_marker.py", "",
    ),
    "blend_missing": ReasonRow(
        UNCLASSIFIED, "cluster_normalization_sync.py", "",
    ),
    "blend_source_index_cancelled": ReasonRow(
        UNCLASSIFIED, "pcg_st9_texture_batch/pcg_texture_audit.py", "",
    ),
    "blend_source_index_error": ReasonRow(
        UNCLASSIFIED, "pcg_st9_texture_batch/pcg_texture_audit.py", "",
    ),
    "blend_source_index_error_root_exit": ReasonRow(
        UNCLASSIFIED, "pcg_st9_texture_batch/pcg_texture_audit.py", "",
    ),
    "blend_source_index_root_exit": ReasonRow(
        UNCLASSIFIED, "pcg_st9_texture_batch/pcg_texture_audit.py", "",
    ),
    "blend_source_index_timeout": ReasonRow(
        UNCLASSIFIED, "pcg_st9_texture_batch/pcg_texture_audit.py", "",
    ),
    "blender_cluster_bake_origin_invalid": ReasonRow(
        UNCLASSIFIED, "sk_batch/jobs/speedtree_material_preflight.py", "",
    ),
    "blender_source_collection_content_key_invalid": ReasonRow(
        UNCLASSIFIED, "cluster_atlas_source_index.py", "",
    ),
    "blender_source_collection_identity_missing": ReasonRow(
        UNCLASSIFIED, "cluster_atlas_source_index.py", "",
    ),
    "blender_source_content_changed": ReasonRow(
        UNCLASSIFIED, "cluster_atlas_source_index.py", "",
    ),
    "blender_source_content_key_missing": ReasonRow(
        UNCLASSIFIED, "cluster_atlas_source_index.py", "",
    ),
    "blender_source_empty_tombstone_invalid": ReasonRow(
        UNCLASSIFIED, "cluster_atlas_source_index.py", "",
    ),
    "blender_source_identity_ambiguous": ReasonRow(
        UNCLASSIFIED, "cluster_atlas_source_index.py", "",
    ),
    "blender_source_identity_invalid": ReasonRow(
        UNCLASSIFIED, "cluster_atlas_source_index.py", "",
    ),
    "blender_source_identity_missing": ReasonRow(
        UNCLASSIFIED, "cluster_atlas_source_index.py", "",
    ),
    "blender_source_mesh_content_key_missing": ReasonRow(
        UNCLASSIFIED, "cluster_atlas_source_index.py", "",
    ),
    "blender_source_missing": ReasonRow(
        UNCLASSIFIED, "cluster_atlas_source_index.py", "",
    ),
    "blender_source_object_identity_missing": ReasonRow(
        UNCLASSIFIED, "cluster_atlas_source_index.py", "",
    ),
    "blender_source_object_inventory_invalid": ReasonRow(
        UNCLASSIFIED, "cluster_atlas_source_index.py", "",
    ),
    "blender_source_path_changed": ReasonRow(
        UNCLASSIFIED, "cluster_atlas_source_index.py", "",
    ),
    "blender_source_path_identity_missing": ReasonRow(
        UNCLASSIFIED, "cluster_atlas_source_index.py", "",
    ),
    "blender_source_publication_binding_invalid": ReasonRow(
        UNCLASSIFIED, "cluster_atlas_source_index.py", "",
    ),
    "blender_source_publication_binding_missing": ReasonRow(
        UNCLASSIFIED, "cluster_atlas_source_index.py", "",
    ),
    "blender_source_scope_identity_missing": ReasonRow(
        UNCLASSIFIED, "cluster_atlas_source_index.py", "",
    ),
    "callback_error": ReasonRow(
        UNCLASSIFIED, "spm_generator_sync/process_stream.py", "",
    ),
    "cancelled": ReasonRow(
        UNCLASSIFIED, "spm_generator_sync/process_stream.py", "",
    ),
    "candidate": ReasonRow(
        INFORMATIONAL, "sk_batch/sk_batch_gui.pyw",
        "retry_route_status",
    ),
    "candidate_file_missing": ReasonRow(
        UNCLASSIFIED, "atlas_manifest_resolver.py", "",
    ),
    "canonical_bark_manifest_target_missing": ReasonRow(
        UNCLASSIFIED, "cluster_bark_source_resolution.py", "",
    ),
    "canonical_bark_ambiguous": ReasonRow(
        UNSUPPORTED, "sk_batch/cluster_assembly_handoff_contract.py",
        "cluster_authoring",
    ),
    "canonical_bark_missing": ReasonRow(
        UNSUPPORTED,
        "pcg_st9_texture_batch/pcg_cluster_assembly_contract.py",
        "cluster_authoring",
    ),
    "canonical_bark_material_ambiguous": ReasonRow(
        UNCLASSIFIED, "cluster_bark_source_resolution.py", "",
    ),
    "canonical_bark_material_id_missing": ReasonRow(
        UNCLASSIFIED, "cluster_bark_source_resolution.py", "",
    ),
    "canonical_bark_production_ref_not_manifest_output": ReasonRow(
        UNCLASSIFIED, "cluster_bark_source_resolution.py", "",
    ),
    "canonical_bark_normalization_required": ReasonRow(
        REPAIRABLE, "sk_batch/cluster_assembly_handoff_contract.py",
        "cluster_refresh",
    ),
    "canonical_material_mapping_incomplete": ReasonRow(
        UNCLASSIFIED, "pcg_st9_texture_batch/pcg_canonical_outputs.py", "",
    ),
    "canonical_output_has_no_material_targets": ReasonRow(
        UNCLASSIFIED, "speedtree_texture_contract.py", "",
    ),
    "canonical_output_manifest_empty": ReasonRow(
        UNCLASSIFIED, "speedtree_texture_contract.py", "",
    ),
    "canonical_output_manifest_invalid_json": ReasonRow(
        UNCLASSIFIED, "speedtree_texture_contract.py", "",
    ),
    "canonical_output_manifest_missing": ReasonRow(
        UNCLASSIFIED, "speedtree_texture_contract.py", "",
    ),
    "canonical_output_manifest_schema_mismatch": ReasonRow(
        UNCLASSIFIED, "speedtree_texture_contract.py", "",
    ),
    "canonical_source_changed": ReasonRow(
        UNCLASSIFIED, "cluster_blend_sync.py", "",
    ),
    "canonical_source_missing": ReasonRow(
        UNCLASSIFIED, "cluster_blend_sync.py", "",
    ),
    "canonical_source_semantic_unavailable": ReasonRow(
        UNCLASSIFIED, "cluster_blend_sync.py", "",
    ),
    "canonical_source_structural_changed": ReasonRow(
        UNCLASSIFIED, "cluster_blend_sync.py", "",
    ),
    "cleanup": ReasonRow(
        UNCLASSIFIED, "spm_generator_sync/process_stream.py", "",
    ),
    "cluster_missing_normalized_export_pivot": ReasonRow(
        UNCLASSIFIED, "cluster_export_handoff_contract.py", "",
    ),
    "cluster_canonical_spm_missing": ReasonRow(
        FATAL, "pcg_st9_texture_batch/pcg_cluster_assembly_contract.py",
        "cluster_identity",
    ),
    "cluster_export_artifact_mismatch": ReasonRow(
        FATAL, "sk_batch/cluster_assembly_handoff_contract.py",
        "cluster_integrity",
    ),
    "cluster_role_conflict": ReasonRow(
        FATAL, "pcg_st9_texture_batch/pcg_cluster_assembly_contract.py",
        "cluster_integrity",
    ),
    "cluster_role_handoff_blocked": ReasonRow(
        INFORMATIONAL, "sk_batch/cluster_assembly_handoff_contract.py",
        "cluster_handoff_diagnostic",
    ),
    "cluster_source_build_contract_invalid": ReasonRow(
        UNSUPPORTED, "cluster_normalization_sync.py",
        "cluster_source_integrity",
    ),
    "cluster_tga_basename_invalid": ReasonRow(
        UNSUPPORTED,
        "pcg_st9_texture_batch/pcg_cluster_assembly_contract.py",
        "cluster_data",
    ),
    "communication_error": ReasonRow(
        UNCLASSIFIED, "process_lifecycle.py", "",
    ),
    "compatibility_unversioned_v1": ReasonRow(
        UNCLASSIFIED, "atlas_manifest_resolver.py", "",
    ),
    "copied_artifacts": ReasonRow(
        UNCLASSIFIED, "cluster_bark_source_resolution.py", "",
    ),
    "current_atlas_material_mesh_connected": ReasonRow(
        UNCLASSIFIED, "pcg_st9_texture_batch/pcg_texture_audit.py", "",
    ),
    "current_material_mesh_connection_incomplete": ReasonRow(
        UNCLASSIFIED, "pcg_st9_texture_batch/pcg_texture_audit.py", "",
    ),
    "current_unused_group": ReasonRow(
        UNCLASSIFIED, "sk_batch/atlas_consumer_integrity.py", "",
    ),
    "current_preserved_unreferenced": ReasonRow(
        INFORMATIONAL, "sk_batch/atlas_consumer_integrity.py",
        "current_authority_variant",
    ),
    "dependency_root_reason_missing": ReasonRow(
        FATAL, "sk_batch/sk_batch_gui.pyw", "dependency_provenance",
    ),
    "dependency_output_missing": ReasonRow(
        UNSUPPORTED, "sk_batch/sk_batch_gui.pyw", "dependency_artifact",
    ),
    "dependency_output_stale": ReasonRow(
        UNSUPPORTED, "sk_batch/sk_batch_gui.pyw", "dependency_artifact",
    ),
    "dependency_waiting": ReasonRow(
        INFORMATIONAL, "sk_batch/sk_batch_gui.pyw", "dependency_lifecycle",
    ),
    "declared_generator_slot_not_declared_exactly_once": ReasonRow(
        UNCLASSIFIED, "pcg_st9_texture_batch/pcg_cluster_assembly_contract.py", "",
    ),
    "different_target_spm": ReasonRow(
        UNCLASSIFIED, "atlas_manifest_resolver.py", "",
    ),
    "duplicate_material_id": ReasonRow(
        UNCLASSIFIED, "sk_batch/atlas_consumer_integrity.py", "",
    ),
    "duplicate_mesh_id": ReasonRow(
        UNCLASSIFIED, "sk_batch/atlas_consumer_integrity.py", "",
    ),
    "explicit_supported_schema": ReasonRow(
        UNCLASSIFIED, "atlas_manifest_resolver.py", "",
    ),
    "explicit_target_relation_off": ReasonRow(
        UNCLASSIFIED, "pcg_st9_texture_batch/pcg_cluster_assembly_contract.py", "",
    ),
    "exact_relation_repair_failed": ReasonRow(
        UNSUPPORTED, "sk_batch/sk_batch_gui.pyw", "repair_execution",
    ),
    "exact_target_plan_invalid": ReasonRow(
        UNSUPPORTED, "sk_batch/sk_batch_gui.pyw", "repair_plan",
    ),
    "fresh_live_export_nonparticipation": ReasonRow(
        UNCLASSIFIED, "sk_batch/jobs/speedtree_material_preflight.py", "",
    ),
    "fbx_role_material_mesh_partial": ReasonRow(
        FATAL, "pcg_st9_texture_batch/pcg_cluster_assembly_contract.py",
        "cluster_integrity",
    ),
    "generator_connection_all_bindings_planned_inactive": ReasonRow(
        UNCLASSIFIED, "pcg_st9_texture_batch/pcg_cluster_assembly_contract.py", "",
    ),
    "generator_connection_contract_incomplete": ReasonRow(
        REPAIRABLE, "pcg_st9_texture_batch/pcg_cluster_assembly_contract.py", "generator_cluster",
    ),
    "generator_connection_incomplete": ReasonRow(
        UNCLASSIFIED, "atlas_manifest_resolver.py", "",
    ),
    "generator_connection_matches_live_export": ReasonRow(
        UNCLASSIFIED, "pcg_st9_texture_batch/pcg_cluster_assembly_contract.py", "",
    ),
    "generator_connection_not_requested": ReasonRow(
        UNCLASSIFIED, "pcg_st9_texture_batch/pcg_cluster_assembly_contract.py", "",
    ),
    "generator_cross_group_pair": ReasonRow(
        UNSUPPORTED, "sk_batch/atlas_consumer_integrity.py", "visible_generator_pair",
    ),
    "generator_guid_missing": ReasonRow(
        UNCLASSIFIED, "sk_batch/atlas_consumer_integrity.py", "",
    ),
    "generator_material_scope_ambiguous": ReasonRow(
        UNSUPPORTED, "speedtree_texture_contract.py",
        "texture_authoring",
    ),
    "generator_material_scope_empty": ReasonRow(
        UNSUPPORTED, "speedtree_texture_contract.py",
        "texture_authoring",
    ),
    "generator_material_scope_unreadable": ReasonRow(
        UNCLASSIFIED, "speedtree_texture_contract.py", "",
    ),
    "generator_slot_pair_incomplete": ReasonRow(
        UNCLASSIFIED, "sk_batch/atlas_consumer_integrity.py", "",
    ),
    "hash_validated_cluster_assembly_source": ReasonRow(
        UNCLASSIFIED, "sk_batch/cluster_assembly_handoff_contract.py", "",
    ),
    "incomplete_texture_set": ReasonRow(
        UNSUPPORTED, "speedtree_texture_contract.py",
        "texture_authoring",
    ),
    "initiating_job_cancelled": ReasonRow(
        UNCLASSIFIED, "sk_batch/sk_batch_gui.pyw", "",
    ),
    "initiating_job_context_missing": ReasonRow(
        UNCLASSIFIED, "sk_batch/sk_batch_gui.pyw", "",
    ),
    "inactive_material_provisional_source": ReasonRow(
        INFORMATIONAL, "sk_batch/jobs/speedtree_material_preflight.py",
        "nonparticipating_material",
    ),
    "instance_profile_invalid": ReasonRow(
        FATAL, "speedtree_pipeline_contract.py", "pipeline_contract",
    ),
    "invalid_required_roles": ReasonRow(
        UNCLASSIFIED, "speedtree_texture_contract.py", "",
    ),
    "invalid_texture_base": ReasonRow(
        UNCLASSIFIED, "speedtree_texture_contract.py", "",
    ),
    "isolated_spm_sha256": ReasonRow(
        UNCLASSIFIED, "cluster_bark_source_resolution.py", "",
    ),
    "isolated_texture_rebase_verification_failed": ReasonRow(
        UNCLASSIFIED, "speedtree_texture_contract.py", "",
    ),
    "isolated_texture_role_unmapped": ReasonRow(
        UNCLASSIFIED, "speedtree_texture_contract.py", "",
    ),
    "kind": ReasonRow(
        UNCLASSIFIED, "cluster_bark_source_resolution.py", "",
    ),
    "live_export_evidence_contract_ambiguous": ReasonRow(
        UNCLASSIFIED, "sk_batch/jobs/speedtree_material_preflight.py", "",
    ),
    "live_export_evidence_node_table_empty": ReasonRow(
        UNCLASSIFIED, "sk_batch/jobs/speedtree_material_preflight.py", "",
    ),
    "live_export_evidence_source_mismatch": ReasonRow(
        UNCLASSIFIED, "sk_batch/jobs/speedtree_material_preflight.py", "",
    ),
    "live_export_evidence_stale_node_table": ReasonRow(
        UNCLASSIFIED, "sk_batch/jobs/speedtree_material_preflight.py", "",
    ),
    "live_export_evidence_unavailable": ReasonRow(
        UNCLASSIFIED, "sk_batch/jobs/speedtree_material_preflight.py", "",
    ),
    "live_generator_slot_not_declared_exactly_once": ReasonRow(
        UNCLASSIFIED, "pcg_st9_texture_batch/pcg_cluster_assembly_contract.py", "",
    ),
    "managed_mesh_owner_ambiguous": ReasonRow(
        UNCLASSIFIED, "sk_batch/atlas_consumer_integrity.py", "",
    ),
    "managed_mesh_scope_mismatch": ReasonRow(
        UNCLASSIFIED, "sk_batch/atlas_consumer_integrity.py", "",
    ),
    "manifest_outside_texture_root": ReasonRow(
        UNCLASSIFIED, "speedtree_texture_contract.py", "",
    ),
    "material_absent_from_rendered_mesh": ReasonRow(
        UNCLASSIFIED, "sk_batch/cluster_assembly_builder.py", "",
    ),
    "material_canonical_output_unmapped": ReasonRow(
        UNCLASSIFIED, "speedtree_texture_contract.py", "",
    ),
    "material_canonical_role_unmapped": ReasonRow(
        UNCLASSIFIED, "speedtree_texture_contract.py", "",
    ),
    "material_export_scope_evidence_ambiguous": ReasonRow(
        UNCLASSIFIED, "sk_batch/jobs/speedtree_material_preflight.py", "",
    ),
    "material_has_no_rendered_polygons": ReasonRow(
        UNCLASSIFIED, "sk_batch/cluster_assembly_builder.py", "",
    ),
    "material_id_unavailable": ReasonRow(
        UNCLASSIFIED, "sk_batch/jobs/speedtree_material_preflight.py", "",
    ),
    "material_canonical_name_divergence": ReasonRow(
        FATAL, "speedtree_pipeline_contract.py", "material_contract",
    ),
    "material_identity_not_unique": ReasonRow(
        UNCLASSIFIED, "sk_batch/jobs/speedtree_material_preflight.py", "",
    ),
    "material_is_expected_to_export": ReasonRow(
        UNCLASSIFIED, "sk_batch/jobs/speedtree_material_preflight.py", "",
    ),
    "material_intent_parse_error": ReasonRow(
        FATAL, "speedtree_pipeline_contract.py", "material_contract",
    ),
    "material_live_binding_evidence_missing": ReasonRow(
        UNCLASSIFIED, "sk_batch/jobs/speedtree_material_preflight.py", "",
    ),
    "material_live_binding_evidence_stale_or_ambiguous": ReasonRow(
        UNCLASSIFIED, "sk_batch/jobs/speedtree_material_preflight.py", "",
    ),
    "material_live_binding_state_ambiguous": ReasonRow(
        UNCLASSIFIED, "sk_batch/jobs/speedtree_material_preflight.py", "",
    ),
    "material_live_binding_visible_or_exporting": ReasonRow(
        UNCLASSIFIED, "sk_batch/jobs/speedtree_material_preflight.py", "",
    ),
    "material_texture_origin_invalid": ReasonRow(
        UNCLASSIFIED, "speedtree_texture_contract.py", "",
    ),
    "merged_source_missing": ReasonRow(
        UNSUPPORTED, "cluster_normalization_sync.py",
        "cluster_source_integrity",
    ),
    "missing_export_collection": ReasonRow(
        UNCLASSIFIED, "cluster_export_handoff_contract.py", "",
    ),
    "missing_or_incomplete_report": ReasonRow(
        UNSUPPORTED, "cluster_normalization_sync.py",
        "cluster_source_integrity",
    ),
    "missing_pipeline_report": ReasonRow(
        UNCLASSIFIED, "sk_batch/jobs/bwr_headless_job.py", "",
    ),
    "missing_spm_identity": ReasonRow(
        UNCLASSIFIED, "atlas_manifest_resolver.py", "",
    ),
    "modeler_launch_or_recovery_io_failed": ReasonRow(
        UNCLASSIFIED, "sk_batch/sk_batch_gui.pyw", "",
    ),
    "multiple_unsuffixed_export_roots": ReasonRow(
        UNCLASSIFIED, "cluster_export_handoff_contract.py", "",
    ),
    "new_exact_bwr_source_hierarchy": ReasonRow(
        UNCLASSIFIED, "cluster_export_handoff_contract.py", "",
    ),
    "no_cluster_assembly_roles": ReasonRow(
        UNCLASSIFIED, "sk_batch/cluster_assembly_handoff_contract.py", "",
    ),
    "no_explicit_owner_relation": ReasonRow(
        UNCLASSIFIED, "sk_batch/sk_batch_gui.pyw", "",
    ),
    "no_unsuffixed_export_root": ReasonRow(
        UNCLASSIFIED, "cluster_export_handoff_contract.py", "",
    ),
    "not_referenced_by_generator_material_property": ReasonRow(
        UNCLASSIFIED, "speedtree_texture_contract.py", "",
    ),
    "normalized_generator_delivery_incomplete": ReasonRow(
        REPAIRABLE,
        "pcg_st9_texture_batch/pcg_cluster_assembly_contract.py",
        "generator_cluster",
    ),
    "normalized_generator_node_table_stale": ReasonRow(
        REPAIRABLE,
        "pcg_st9_texture_batch/pcg_cluster_assembly_contract.py",
        "modeler_node_table",
    ),
    "normalized_variants_required": ReasonRow(
        REPAIRABLE,
        "pcg_st9_texture_batch/pcg_cluster_assembly_contract.py",
        "cluster_refresh",
    ),
    "normalized_variants_stale": ReasonRow(
        REPAIRABLE,
        "pcg_st9_texture_batch/pcg_cluster_assembly_contract.py",
        "cluster_refresh",
    ),
    "normal_exit": ReasonRow(
        INFORMATIONAL, "process_lifecycle.py", "lifecycle_event",
    ),
    "operational_candidate": ReasonRow(
        UNCLASSIFIED, "atlas_manifest_resolver.py", "",
    ),
    "operational_candidate_disagreement": ReasonRow(
        UNCLASSIFIED, "atlas_manifest_resolver.py", "",
    ),
    "operator_cancelled": ReasonRow(
        INFORMATIONAL, "exact_target_command.py", "lifecycle_cancelled",
    ),
    "operator_release_requested": ReasonRow(
        INFORMATIONAL, "shared_job_queue.py", "lifecycle_cancelled",
    ),
    "output_filename": ReasonRow(
        UNCLASSIFIED, "cluster_bark_source_resolution.py", "",
    ),
    "output_identity_missing": ReasonRow(
        UNCLASSIFIED, "pcg_st9_texture_batch/pcg_texture_gui.pyw", "",
    ),
    "output_missing": ReasonRow(
        UNCLASSIFIED, "sk_batch/spm_audit.py", "",
    ),
    "output_set_incomplete": ReasonRow(
        UNCLASSIFIED, "pcg_st9_texture_batch/pcg_texture_gui.pyw", "",
    ),
    "over_budget": ReasonRow(
        UNCLASSIFIED, "pcg_st9_texture_batch/pcg_board_snapshot.py", "",
    ),
    "owner_release_acknowledged": ReasonRow(
        UNCLASSIFIED, "shared_job_queue.py", "",
    ),
    "owner_spm_is_not_an_atlas_producer": ReasonRow(
        UNCLASSIFIED, "sk_batch/sk_batch_gui.pyw", "",
    ),
    "persisted_unsuffixed_export_root": ReasonRow(
        UNCLASSIFIED, "cluster_export_handoff_contract.py", "",
    ),
    "physical_capture_changed": ReasonRow(
        UNCLASSIFIED, "cluster_blend_sync.py", "",
    ),
    "physical_capture_manifest_missing": ReasonRow(
        UNCLASSIFIED, "cluster_blend_sync.py", "",
    ),
    "path_alias_missing": ReasonRow(
        UNSUPPORTED, "pcg_st9_texture_batch/pcg_cluster_assembly_contract.py",
        "cluster_data",
    ),
    "pcg_cluster_handoff_not_ready": ReasonRow(
        UNSUPPORTED, "sk_batch/cluster_assembly_handoff_contract.py",
        "cluster_handoff",
    ),
    "pipeline_retry_result_missing": ReasonRow(
        UNCLASSIFIED, "sk_batch/sk_batch_gui.pyw", "",
    ),
    "pipeline_aborted": ReasonRow(
        UNSUPPORTED, "sk_batch/sk_batch_gui.pyw", "phase_failure_fallback",
    ),
    "planning_target_missing": ReasonRow(
        UNSUPPORTED, "sk_batch/sk_batch_gui.pyw", "planning_snapshot_missing",
    ),
    "production_source_mutated": ReasonRow(
        UNCLASSIFIED, "cluster_bark_source_resolution.py", "",
    ),
    "production_spm_is_derived_cache": ReasonRow(
        UNCLASSIFIED, "speedtree_texture_contract.py", "",
    ),
    "production_spm_outside_manifest_asset": ReasonRow(
        UNCLASSIFIED, "speedtree_texture_contract.py", "",
    ),
    "preflight_error": ReasonRow(
        UNSUPPORTED, "sk_batch/jobs/speedtree_material_preflight.py",
        "preflight_error",
    ),
    "production_texture_not_canonical_output": ReasonRow(
        UNSUPPORTED, "speedtree_texture_contract.py",
        "texture_scope_integrity",
    ),
    "production_texture_uses_derived_cache": ReasonRow(
        UNSUPPORTED, "speedtree_texture_contract.py",
        "texture_scope_integrity",
    ),
    "protected_manual": ReasonRow(
        UNCLASSIFIED, "sk_batch/atlas_consumer_integrity.py", "",
    ),
    "provider_identity": ReasonRow(
        UNCLASSIFIED, "cluster_bark_source_resolution.py", "",
    ),
    "provisional_source_blocked": ReasonRow(
        UNCLASSIFIED, "sk_batch/jobs/speedtree_material_preflight.py", "",
    ),
    "publication_canceled": ReasonRow(
        UNCLASSIFIED, "pcg_st9_texture_batch/pcg_board_snapshot.py", "",
    ),
    "receipt_not_applicable": ReasonRow(
        INFORMATIONAL, "pcg_st9_texture_batch/pcg_texture_audit.py",
        "receipt_status",
    ),
    "receipt_not_requested": ReasonRow(
        INFORMATIONAL, "pcg_st9_texture_batch/pcg_texture_audit.py",
        "receipt_status",
    ),
    "receipt_persisted": ReasonRow(
        INFORMATIONAL, "pcg_st9_texture_batch/pcg_texture_audit.py",
        "receipt_status",
    ),
    "receipt_persistence_failed": ReasonRow(
        UNSUPPORTED, "pcg_st9_texture_batch/pcg_texture_audit.py",
        "receipt_persistence",
    ),
    "registered_reason_has_no_exact_action": ReasonRow(
        UNSUPPORTED, "sk_batch/sk_batch_gui.pyw", "repair_plan",
    ),
    "receipt_unchanged": ReasonRow(
        INFORMATIONAL, "pcg_st9_texture_batch/pcg_texture_audit.py",
        "receipt_status",
    ),
    "recorded_source_conflict": ReasonRow(
        UNCLASSIFIED, "cluster_blend_sync.py", "",
    ),
    "recorded_source_missing": ReasonRow(
        UNCLASSIFIED, "cluster_blend_sync.py", "",
    ),
    "recovery_continuation_result_missing": ReasonRow(
        UNCLASSIFIED, "sk_batch/sk_batch_gui.pyw", "",
    ),
    "recovery_live_export_target_scope_empty": ReasonRow(
        UNCLASSIFIED, "pcg_st9_texture_batch/stale_node_table_recovery.py", "",
    ),
    "recovery_target_material_scope_missing": ReasonRow(
        UNCLASSIFIED, "pcg_st9_texture_batch/stale_node_table_recovery.py", "",
    ),
    "relation_physical_proof_incomplete": ReasonRow(
        UNCLASSIFIED, "cluster_blend_sync.py", "",
    ),
    "relationship_continuity_only": ReasonRow(
        UNCLASSIFIED, "pcg_st9_texture_batch/pcg_cluster_assembly_contract.py", "",
    ),
    "repair_inventory_target_missing": ReasonRow(
        UNCLASSIFIED, "sk_batch/sk_batch_gui.pyw", "",
    ),
    "required_cluster_repair_cancelled": ReasonRow(
        UNCLASSIFIED, "sk_batch/sk_batch_gui.pyw", "",
    ),
    "required_cluster_repair_failed": ReasonRow(
        UNCLASSIFIED, "sk_batch/sk_batch_gui.pyw", "",
    ),
    "required_cluster_repaired_resume": ReasonRow(
        UNCLASSIFIED, "sk_batch/sk_batch_gui.pyw", "",
    ),
    "resumable_cancelled": ReasonRow(
        INFORMATIONAL, "sk_batch/sk_batch_gui.pyw",
        "lifecycle_cancelled",
    ),
    "resumed_pipeline_result_missing": ReasonRow(
        UNCLASSIFIED, "sk_batch/sk_batch_gui.pyw", "",
    ),
    "retired_scope_record": ReasonRow(
        UNCLASSIFIED, "atlas_manifest_resolver.py", "",
    ),
    "root_exit": ReasonRow(
        UNCLASSIFIED, "process_lifecycle.py", "",
    ),
    "runtime_shutdown": ReasonRow(
        UNCLASSIFIED, "shared_queue_runtime.py", "",
    ),
    "same_role_reference_provider_is_not_an_assembly_part": ReasonRow(
        UNCLASSIFIED, "pcg_st9_texture_batch/pcg_cluster_assembly_contract.py", "",
    ),
    "schema_version_not_integer": ReasonRow(
        UNCLASSIFIED, "atlas_manifest_resolver.py", "",
    ),
    "sealed_policy_has_no_required_live_targets": ReasonRow(
        UNCLASSIFIED, "pcg_st9_texture_batch/stale_node_table_recovery.py", "",
    ),
    "shared_dependency_failed": ReasonRow(
        INFORMATIONAL, "sk_batch/sk_batch_gui.pyw", "dependency_wrapper",
    ),
    "shared_queue_lease_owner_lost": ReasonRow(
        UNSUPPORTED, "sk_batch/sk_batch_gui.pyw", "lifecycle_owner_lost",
    ),
    "signature": ReasonRow(
        UNCLASSIFIED, "cluster_bark_source_resolution.py", "",
    ),
    "sk_batch_local_queue_cancelled": ReasonRow(
        UNCLASSIFIED, "sk_batch/sk_batch_gui.pyw", "",
    ),
    "sk_stop": ReasonRow(
        UNCLASSIFIED, "sk_batch/sk_common.py", "",
    ),
    "sk_worker_complete": ReasonRow(
        UNCLASSIFIED, "sk_batch/sk_common.py", "",
    ),
    "source_already_repaired_after_blocking_audit": ReasonRow(
        UNCLASSIFIED, "sk_batch/sk_batch_gui.pyw", "",
    ),
    "source_fbx_changed": ReasonRow(
        UNCLASSIFIED, "cluster_blend_sync.py", "",
    ),
    "source_fbx_missing": ReasonRow(
        UNCLASSIFIED, "cluster_blend_sync.py", "",
    ),
    "source_handoff_blocked": ReasonRow(
        UNSUPPORTED, "cluster_normalization_sync.py",
        "cluster_source_integrity",
    ),
    "source_identity_stale": ReasonRow(
        UNSUPPORTED, "cluster_normalization_sync.py",
        "cluster_source_integrity",
    ),
    "source_semantic_unavailable": ReasonRow(
        UNSUPPORTED, "cluster_normalization_sync.py",
        "cluster_source_integrity",
    ),
    "source_spm_missing": ReasonRow(
        UNCLASSIFIED, "cluster_bark_source_resolution.py", "",
    ),
    "source_spm_path": ReasonRow(
        UNCLASSIFIED, "cluster_bark_source_resolution.py", "",
    ),
    "source_spm_sha256": ReasonRow(
        UNCLASSIFIED, "cluster_bark_source_resolution.py", "",
    ),
    "source_xml_missing": ReasonRow(
        UNCLASSIFIED, "cluster_normalization_sync.py", "",
    ),
    "speedtree_root_exit": ReasonRow(
        UNCLASSIFIED, "sk_batch/spm_audit.py", "",
    ),
    "speedtree_spm_filename": ReasonRow(
        UNCLASSIFIED, "cluster_bark_source_resolution.py", "",
    ),
    "speedtree_spm_missing": ReasonRow(
        UNCLASSIFIED, "cluster_bark_source_resolution.py", "",
    ),
    "speedtree_spm_path": ReasonRow(
        UNCLASSIFIED, "cluster_bark_source_resolution.py", "",
    ),
    "speedtree_texture_role_unknown": ReasonRow(
        UNCLASSIFIED, "speedtree_texture_contract.py", "",
    ),
    "speedtree_timeout": ReasonRow(
        UNCLASSIFIED, "sk_batch/spm_audit.py", "",
    ),
    "spm_mesh_file_missing": ReasonRow(
        FATAL, "sk_batch/jobs/speedtree_material_preflight.py",
        "asset_missing",
    ),
    "spm_visible_default_material_ambiguous": ReasonRow(
        FATAL, "sk_batch/job_report_contract.py", "material_contract",
    ),
    "status": ReasonRow(
        UNCLASSIFIED, "cluster_bark_source_resolution.py", "",
    ),
    "target_material_lineage_missing": ReasonRow(
        UNCLASSIFIED, "pcg_st9_texture_batch/pcg_texture_audit.py", "",
    ),
    "target_mesh_asset_missing": ReasonRow(
        UNCLASSIFIED, "pcg_st9_texture_batch/pcg_cluster_assembly_contract.py", "",
    ),
    "target_missing": ReasonRow(
        UNCLASSIFIED, "cluster_blend_sync.py", "",
    ),
    "target_outside_owner": ReasonRow(
        UNCLASSIFIED, "cluster_blend_sync.py", "",
    ),
    "target_scope_candidate_missing": ReasonRow(
        UNCLASSIFIED, "atlas_manifest_resolver.py", "",
    ),
    "target_scope_changed": ReasonRow(
        UNCLASSIFIED, "cluster_blend_sync.py", "",
    ),
    "timed_out": ReasonRow(
        UNCLASSIFIED, "spm_generator_sync/process_stream.py", "",
    ),
    "texture_ref_not_found": ReasonRow(
        UNSUPPORTED, "pcg_st9_texture_batch/pcg_cluster_assembly_contract.py",
        "cluster_data",
    ),
    "texture_set_incomplete": ReasonRow(
        REPAIRABLE, "sk_batch/jobs/speedtree_material_preflight.py",
        "pcg_texture",
    ),
    "timeout": ReasonRow(
        UNCLASSIFIED, "process_lifecycle.py", "",
    ),
    "unreal_dependency_full_rebuild_fallback": ReasonRow(
        UNCLASSIFIED, "sk_batch/sk_batch_gui.pyw", "",
    ),
    "unsupported_schema_version": ReasonRow(
        UNCLASSIFIED, "atlas_manifest_resolver.py", "",
    ),
    "userdata_not_object": ReasonRow(
        UNCLASSIFIED, "sk_batch/atlas_consumer_integrity.py", "",
    ),
    "worker_wait_failed": ReasonRow(
        UNCLASSIFIED, "spm_generator_sync/spm_generator_sync_gui.pyw", "",
    ),
    "written": ReasonRow(
        UNCLASSIFIED, "pcg_st9_texture_batch/pcg_board_snapshot.py", "",
    ),
    # Expanded static-scan shapes and the sanitized observed-token snapshot
    # from PR #148.  Durable statuses and retry-routing decisions are inputs,
    # never blocking verdicts by themselves; exact evidence failures remain
    # loud unsupported/fatal rows.
    "all_recovery_target_material_scopes_match_live_export": ReasonRow(
        INFORMATIONAL, "pcg_st9_texture_batch/stale_node_table_recovery.py",
        "recovery_verified",
    ),
    "ambiguous_unsuffixed_export_hierarchy": ReasonRow(
        UNSUPPORTED, "cluster_export_handoff_contract.py", "export_hierarchy",
    ),
    "atlas_manifest_authority_missing": ReasonRow(
        REPAIRABLE, "sk_batch/atlas_consumer_integrity.py", "cluster_refresh",
    ),
    "atlas_manifest_resolution_conflict": ReasonRow(
        REPAIRABLE, "sk_batch/atlas_consumer_integrity.py", "cluster_refresh",
    ),
    "atlas_marker_kind_mismatch": ReasonRow(
        FATAL, "sk_batch/atlas_consumer_integrity.py", "atlas_integrity",
    ),
    "authoring_mesh_scope_overlaps_provider_roles": ReasonRow(
        UNSUPPORTED, "sk_batch/sk_batch_gui.pyw", "modeler_recovery_scope",
    ),
    "authoring_scope_not_exact_declared_scope": ReasonRow(
        UNSUPPORTED, "sk_batch/sk_batch_gui.pyw", "modeler_recovery_scope",
    ),
    "authoritative_recovery_target_scope_hash_mismatch": ReasonRow(
        FATAL, "sk_batch/sk_batch_gui.pyw", "modeler_recovery_scope",
    ),
    "authoritative_recovery_target_scope_missing": ReasonRow(
        UNSUPPORTED, "sk_batch/sk_batch_gui.pyw", "modeler_recovery_scope",
    ),
    "automatic_repair_failed": ReasonRow(
        UNSUPPORTED, "sk_batch/sk_batch_gui.pyw", "failure_wrapper",
    ),
    "automatic_repair_reaudit_failed": ReasonRow(
        UNSUPPORTED, "sk_batch/sk_batch_gui.pyw", "failure_wrapper",
    ),
    "automatic_repair_unsupported": ReasonRow(
        UNSUPPORTED, "sk_batch/sk_batch_gui.pyw", "failure_wrapper",
    ),
    "blender_cluster_bake_output_missing": ReasonRow(
        UNSUPPORTED, "speedtree_texture_contract.py", "blender_cluster_bake",
    ),
    "blender_cluster_bake_uses_derived_cache": ReasonRow(
        UNSUPPORTED, "speedtree_texture_contract.py", "blender_cluster_bake",
    ),
    "blender_output_not_current": ReasonRow(
        INFORMATIONAL, "sk_batch/failed_retry_eligibility.py", "blender_rebuild_route",
    ),
    "canonical_output": ReasonRow(
        INFORMATIONAL, "speedtree_texture_contract.py", "current_texture_output",
    ),
    "canonical_output_missing": ReasonRow(
        REPAIRABLE, "speedtree_texture_contract.py", "pcg_texture",
    ),
    "canonical_output_must_be_manifest_relative": ReasonRow(
        UNSUPPORTED, "speedtree_texture_contract.py",
        "texture_authoring",
    ),
    "canonical_output_outside_texture_root": ReasonRow(
        UNSUPPORTED, "speedtree_texture_contract.py",
        "texture_scope_integrity",
    ),
    "canonical_output_role_mismatch": ReasonRow(
        UNSUPPORTED, "speedtree_texture_contract.py",
        "texture_authoring",
    ),
    "canonical_output_role_undeclared": ReasonRow(
        UNSUPPORTED, "speedtree_texture_contract.py",
        "texture_authoring",
    ),
    "canonical_output_uses_derived_cache": ReasonRow(
        REPAIRABLE, "speedtree_texture_contract.py", "pcg_texture",
    ),
    "coherent_operational_mirror": ReasonRow(
        INFORMATIONAL, "atlas_manifest_resolver.py", "current_atlas_authority",
    ),
    "cancelled_by_runtime": ReasonRow(
        INFORMATIONAL, "shared_queue_runtime.py", "lifecycle_cancelled",
    ),
    "completed": ReasonRow(
        INFORMATIONAL, "sk_batch/retry_progress.py", "durable_status",
    ),
    "current_blender_success": ReasonRow(
        INFORMATIONAL, "sk_batch/failed_retry_eligibility.py", "retry_route_excluded",
    ),
    "current_immutable_unreal_failure": ReasonRow(
        INFORMATIONAL, "sk_batch/failed_retry_eligibility.py", "unreal_only_route",
    ),
    "current_source_newer_than_outputs": ReasonRow(
        REPAIRABLE, "pcg_st9_texture_batch/pcg_texture_gui.pyw", "pcg_texture",
    ),
    "current_success": ReasonRow(
        INFORMATIONAL, "sk_batch/sk_batch_gui.pyw", "durable_status",
    ),
    "current_wait": ReasonRow(
        INFORMATIONAL, "sk_batch/sk_batch_gui.pyw", "retry_route_status",
    ),
    "current_cancelled": ReasonRow(
        INFORMATIONAL, "sk_batch/sk_batch_gui.pyw", "lifecycle_cancelled",
    ),
    "concurrent_spm_modification": ReasonRow(
        UNSUPPORTED, "sk_batch/spm_audit.py", "spm_concurrent_edit",
    ),
    "data_error": ReasonRow(
        UNSUPPORTED, "sk_batch/failed_retry_eligibility.py", "durable_failure",
    ),
    "diagnostic_only_legacy_shadow": ReasonRow(
        INFORMATIONAL, "atlas_manifest_resolver.py", "diagnostic_shadow",
    ),
    "diagnostic_only_scope_identity_shadow": ReasonRow(
        INFORMATIONAL, "atlas_manifest_resolver.py", "diagnostic_shadow",
    ),
    # A Cluster output's legacy unprefixed name is normalization input only.
    # A record written against it that disagrees with a canonical-named one is
    # a rename artifact, so it is shadowed rather than allowed to fail the
    # target closed -- a fact about one record, never a target's verdict.
    "superseded_legacy_name_record": ReasonRow(
        INFORMATIONAL, "atlas_manifest_resolver.py", "diagnostic_shadow",
    ),
    # send2ue_push_job.py writes this as a report stage/status, which is not a
    # reason field.  The token only becomes a reason code where the GUI turns
    # it into `reason_token`, so that module owns the registry row.
    "exported_pending_unreal": ReasonRow(
        INFORMATIONAL, "sk_batch/sk_batch_gui.pyw", "durable_status",
    ),
    "foreign_or_manual_userdata": ReasonRow(
        INFORMATIONAL, "sk_batch/atlas_consumer_integrity.py", "protected_manual_asset",
    ),
    "imported_ok": ReasonRow(
        INFORMATIONAL, "sk_batch/unreal_ingest.py", "durable_status",
    ),
    "internal_error": ReasonRow(
        UNSUPPORTED, "sk_batch/failed_retry_eligibility.py", "durable_failure",
    ),
    "item_failed": ReasonRow(
        UNSUPPORTED, "sk_batch/sk_batch_gui.pyw", "phase_failure_fallback",
    ),
    "interpreter_exit": ReasonRow(
        UNSUPPORTED, "process_lifecycle.py", "process_lifecycle",
    ),
    "interrupted_calibration": ReasonRow(
        UNSUPPORTED, "sk_batch/spm_audit.py", "interrupted_calibration",
    ),
    "owner_lost": ReasonRow(
        UNSUPPORTED, "sk_batch/retry_progress.py", "owner_lost_failure",
    ),
    "runtime_cancel_all": ReasonRow(
        INFORMATIONAL, "shared_queue_runtime.py", "lifecycle_cancelled",
    ),
    "live_export_evidence_unavailable_stale_node_table": ReasonRow(
        REPAIRABLE, "sk_batch/sk_batch_gui.pyw", "modeler_node_table",
    ),
    "lineage_unproven": ReasonRow(
        REPAIRABLE, "sk_batch/atlas_consumer_integrity.py", "cluster_refresh",
    ),
    "manual_required": ReasonRow(
        INFORMATIONAL, "sk_batch/failed_retry_eligibility.py", "unreal_only_route",
    ),
    "missing_source_collection": ReasonRow(
        UNSUPPORTED, "cluster_export_handoff_contract.py", "export_hierarchy",
    ),
    "non_retryable_returncode": ReasonRow(
        UNSUPPORTED, "sk_batch/spm_audit.py", "exporter_process",
    ),
    "normalized_delivery_evidence_missing": ReasonRow(
        UNSUPPORTED, "sk_batch/sk_batch_gui.pyw", "modeler_recovery_scope",
    ),
    "normalized_delivery_evidence_not_current": ReasonRow(
        UNSUPPORTED, "sk_batch/sk_batch_gui.pyw", "modeler_recovery_scope",
    ),
    "not_managed": ReasonRow(
        INFORMATIONAL, "speedtree_texture_contract.py", "unmanaged_texture",
    ),
    "not_run": ReasonRow(
        INFORMATIONAL, "sk_batch/failed_retry_eligibility.py", "retry_route_status",
    ),
    "not_run_unreal": ReasonRow(
        INFORMATIONAL, "sk_batch/failed_retry_eligibility.py", "blender_rebuild_route",
    ),
    "outputs_current": ReasonRow(
        INFORMATIONAL, "pcg_st9_texture_batch/pcg_texture_gui.pyw", "current_texture_output",
    ),
    "parent_not_retryable_unreal_failure": ReasonRow(
        UNSUPPORTED, "sk_batch/failed_retry_eligibility.py", "unreal_retry_evidence",
    ),
    "preflight_skip": ReasonRow(
        INFORMATIONAL, "sk_batch/sk_batch_gui.pyw", "durable_status",
    ),
    "process": ReasonRow(
        INFORMATIONAL, "sk_batch/failed_retry_eligibility.py", "blender_rebuild_route",
    ),
    "process_cache_io_error": ReasonRow(
        UNSUPPORTED, "sk_batch/sk_batch_gui.pyw",
        "worker_runtime",
    ),
    "process_resolution_error": ReasonRow(
        UNSUPPORTED, "sk_batch/sk_batch_gui.pyw",
        "worker_runtime",
    ),
    "process_timeout": ReasonRow(
        INFORMATIONAL, "sk_batch/failed_retry_eligibility.py", "blender_rebuild_route",
    ),
    "public_exact_target_request": ReasonRow(
        INFORMATIONAL, "pcg_st9_texture_batch/exact_target_repair.py", "request_provenance",
    ),
    "push_timeout": ReasonRow(
        INFORMATIONAL, "sk_batch/failed_retry_eligibility.py", "blender_rebuild_route",
    ),
    "push_phase_evidence_missing_full_rebuild": ReasonRow(
        INFORMATIONAL, "sk_batch/failed_retry_eligibility.py", "blender_rebuild_route",
    ),
    "ready": ReasonRow(
        INFORMATIONAL, "sk_batch/retry_planning.py", "durable_status",
    ),
    "recovery_blocked": ReasonRow(
        UNSUPPORTED, "sk_batch/sk_batch_gui.pyw", "failure_wrapper",
    ),
    "recovery_contract_missing": ReasonRow(
        UNSUPPORTED, "sk_batch/sk_batch_gui.pyw", "modeler_recovery_scope",
    ),
    "recovery_target_material_scope_incomplete": ReasonRow(
        UNSUPPORTED, "pcg_st9_texture_batch/stale_node_table_recovery.py",
        "modeler_recovery_scope",
    ),
    "required_live_scope_not_authoring_subset": ReasonRow(
        FATAL, "sk_batch/sk_batch_gui.pyw", "modeler_recovery_scope",
    ),
    "required_live_scope_not_exact_delivery_scope": ReasonRow(
        FATAL, "sk_batch/sk_batch_gui.pyw", "modeler_recovery_scope",
    ),
    "retry_evidence_ambiguous": ReasonRow(
        UNSUPPORTED, "sk_batch/failed_retry_eligibility.py", "retry_evidence",
    ),
    "rpc_timeout": ReasonRow(
        INFORMATIONAL, "sk_batch/failed_retry_eligibility.py", "blender_rebuild_route",
    ),
    "selected_authority": ReasonRow(
        INFORMATIONAL, "atlas_manifest_resolver.py", "current_atlas_authority",
    ),
    "shutdown": ReasonRow(
        INFORMATIONAL, "process_lifecycle.py", "lifecycle_cancelled",
    ),
    "stale_target_mesh_scope_missing": ReasonRow(
        UNSUPPORTED, "sk_batch/sk_batch_gui.pyw", "modeler_recovery_scope",
    ),
    "structured_blender_failure": ReasonRow(
        INFORMATIONAL, "sk_batch/failed_retry_eligibility.py", "blender_rebuild_route",
    ),
    "structured_send2ue_export_failure": ReasonRow(
        INFORMATIONAL, "sk_batch/failed_retry_eligibility.py", "blender_rebuild_route",
    ),
    "target_audit_identity_missing_or_ambiguous": ReasonRow(
        UNSUPPORTED, "sk_batch/sk_batch_gui.pyw", "modeler_recovery_scope",
    ),
    "target_audit_sha256_missing_or_invalid": ReasonRow(
        FATAL, "sk_batch/sk_batch_gui.pyw", "modeler_recovery_scope",
    ),
    "target_delivery_has_independent_blocker": ReasonRow(
        UNSUPPORTED, "sk_batch/sk_batch_gui.pyw", "modeler_recovery_scope",
    ),
    "target_delivery_missing_or_ambiguous": ReasonRow(
        UNSUPPORTED, "sk_batch/sk_batch_gui.pyw", "modeler_recovery_scope",
    ),
    "target_delivery_not_stale_only": ReasonRow(
        UNSUPPORTED, "sk_batch/sk_batch_gui.pyw", "modeler_recovery_scope",
    ),
    "target_delivery_scope_counts_inconsistent": ReasonRow(
        FATAL, "sk_batch/sk_batch_gui.pyw", "modeler_recovery_scope",
    ),
    "target_delivery_scope_counts_invalid": ReasonRow(
        FATAL, "sk_batch/sk_batch_gui.pyw", "modeler_recovery_scope",
    ),
    "target_delivery_scope_intent_echo_mismatch": ReasonRow(
        FATAL, "sk_batch/sk_batch_gui.pyw", "modeler_recovery_scope",
    ),
    "target_delivery_scope_intent_sha256_invalid": ReasonRow(
        FATAL, "sk_batch/sk_batch_gui.pyw", "modeler_recovery_scope",
    ),
    "target_delivery_scope_not_explicit": ReasonRow(
        UNSUPPORTED, "sk_batch/sk_batch_gui.pyw", "modeler_recovery_scope",
    ),
    "target_delivery_variant_policy_not_supported": ReasonRow(
        UNSUPPORTED, "sk_batch/sk_batch_gui.pyw", "modeler_recovery_scope",
    ),
    "target_has_no_stale_blocking_delivery": ReasonRow(
        UNSUPPORTED, "sk_batch/sk_batch_gui.pyw", "modeler_recovery_scope",
    ),
    "target_recovery_dependencies_missing": ReasonRow(
        UNSUPPORTED, "sk_batch/sk_batch_gui.pyw", "modeler_recovery_scope",
    ),
    "target_recovery_dependency_invalid": ReasonRow(
        FATAL, "sk_batch/sk_batch_gui.pyw", "modeler_recovery_scope",
    ),
    "target_recovery_scope_empty": ReasonRow(
        UNSUPPORTED, "sk_batch/sk_batch_gui.pyw", "modeler_recovery_scope",
    ),
    "texture_source_fallback_needs_pcg_generation": ReasonRow(
        REPAIRABLE, "sk_batch/jobs/speedtree_material_preflight.py", "pcg_texture",
    ),
    "unreal_dependency_requires_rebuild": ReasonRow(
        INFORMATIONAL, "sk_batch/failed_retry_eligibility.py", "blender_rebuild_route",
    ),
    "unreal_parent_evidence_incomplete_full_rebuild": ReasonRow(
        INFORMATIONAL, "sk_batch/failed_retry_eligibility.py", "blender_rebuild_route",
    ),
    "unreal_parent_evidence_invalid_full_rebuild": ReasonRow(
        INFORMATIONAL, "sk_batch/failed_retry_eligibility.py", "blender_rebuild_route",
    ),
    "unreal_parent_validation_pending": ReasonRow(
        INFORMATIONAL, "sk_batch/failed_retry_eligibility.py", "unreal_validation_route",
    ),
    "unreal_unavailable": ReasonRow(
        UNSUPPORTED, "sk_batch/failed_retry_eligibility.py", "durable_failure",
    ),
    "unreal_crash": ReasonRow(
        INFORMATIONAL, "sk_batch/failed_retry_eligibility.py", "unreal_only_route",
    ),
    "userdata_missing": ReasonRow(
        INFORMATIONAL, "sk_batch/atlas_consumer_integrity.py", "protected_manual_asset",
    ),
    "userdata_not_json": ReasonRow(
        INFORMATIONAL, "sk_batch/atlas_consumer_integrity.py", "protected_manual_asset",
    ),
    "valid": ReasonRow(
        INFORMATIONAL, "sk_batch/atlas_consumer_integrity.py", "valid_marker",
    ),
    "wait_cancelled": ReasonRow(
        INFORMATIONAL, "shared_queue_runtime.py", "lifecycle_cancelled",
    ),
}


_DOMAIN_CLASSIFICATIONS: dict[str, tuple[str, str]] = {}


def _classify(disposition, policy, *codes):
    for code in codes:
        normalized = str(code).strip().casefold()
        if normalized in _DOMAIN_CLASSIFICATIONS:
            raise RuntimeError(f"duplicate reason classification: {normalized}")
        _DOMAIN_CLASSIFICATIONS[normalized] = (disposition, policy)


# Atlas resolution records both authoritative outcomes and rejected-candidate
# diagnostics.  Only malformed/unknown explicit schemas terminate resolution;
# the higher-level ownership row owns a disagreement's blocking disposition.
_classify(
    INFORMATIONAL, "atlas_candidate_diagnostic",
    "atlas_manifest_current", "candidate_file_missing",
    "compatibility_unversioned_v1", "different_target_spm",
    "explicit_supported_schema", "generator_connection_incomplete",
    "missing_spm_identity", "operational_candidate",
    "operational_candidate_disagreement", "retired_scope_record",
    "target_scope_candidate_missing",
)
_classify(
    FATAL, "schema_contract",
    "schema_version_not_integer", "unsupported_schema_version",
)

# These are the exact refresh reasons consumed by the Cluster transaction.
# A missing .blend has no executable exact relation; every other persisted
# index drift is rebuilt by that transaction from the current Blender source.
_classify(
    REPAIRABLE, "cluster_refresh",
    "atlas_blender_source_index_invalid",
    "blender_source_collection_content_key_invalid",
    "blender_source_collection_identity_missing",
    "blender_source_content_changed", "blender_source_content_key_missing",
    "blender_source_empty_tombstone_invalid",
    "blender_source_identity_ambiguous", "blender_source_identity_invalid",
    "blender_source_identity_missing", "blender_source_mesh_content_key_missing",
    "blender_source_object_identity_missing",
    "blender_source_object_inventory_invalid", "blender_source_path_changed",
    "blender_source_path_identity_missing",
    "blender_source_publication_binding_invalid",
    "blender_source_publication_binding_missing",
    "blender_source_scope_identity_missing",
)
_classify(UNSUPPORTED, "cluster_source_missing", "blender_source_missing")

# Canonical-bark cache validation reports field names and nested manifest issue
# names inside one exception.  They are not durable target verdicts; the
# owning Cluster gate emits the canonical repair/terminal wrapper.
_classify(
    INFORMATIONAL, "diagnostic_integrity_field",
    "canonical_bark_manifest_target_missing",
    "canonical_bark_material_ambiguous", "canonical_bark_material_id_missing",
    "canonical_bark_production_ref_not_manifest_output", "copied_artifacts",
    "isolated_spm_sha256", "kind", "output_filename",
    "production_source_mutated", "provider_identity", "signature",
    "source_spm_missing", "source_spm_path", "source_spm_sha256",
    "speedtree_spm_filename", "speedtree_spm_missing",
    "speedtree_spm_path", "status",
)

# The live-export-scope reasons explain why a provisional material may or may
# not be excluded; the outer provisional/asset row is the actual gate.
_classify(
    INFORMATIONAL, "material_export_scope_diagnostic",
    "fresh_live_export_nonparticipation",
    "live_export_evidence_contract_ambiguous",
    "live_export_evidence_node_table_empty",
    "live_export_evidence_source_mismatch",
    "live_export_evidence_stale_node_table", "live_export_evidence_unavailable",
    "material_export_scope_evidence_ambiguous", "material_id_unavailable",
    "material_identity_not_unique", "material_is_expected_to_export",
    "material_live_binding_evidence_missing",
    "material_live_binding_evidence_stale_or_ambiguous",
    "material_live_binding_state_ambiguous",
    "material_live_binding_visible_or_exporting",
)
_classify(
    UNSUPPORTED, "manual_asset_reference", "asset_external_mesh_path_missing",
)
_classify(
    UNSUPPORTED, "atlas_integrity_wrapper", "atlas_managed_asset_integrity_stale",
)
_classify(
    UNSUPPORTED, "cluster_bake_origin", "blender_cluster_bake_origin_invalid",
)
_classify(
    UNSUPPORTED, "provisional_texture_source", "provisional_source_blocked",
)

# Retry/orchestration lifecycle outcomes remain visible in their own status
# columns.  Missing context/results are terminal execution blockers; successful
# pass-through/resume and user cancellation are not target repair verdicts.
_classify(
    INFORMATIONAL, "lifecycle_event",
    "automatic_repair_cancelled", "automatic_retry_cancelled",
    "initiating_job_cancelled", "no_explicit_owner_relation",
    "owner_spm_is_not_an_atlas_producer", "required_cluster_repair_cancelled",
    "required_cluster_repaired_resume", "sk_batch_local_queue_cancelled",
    "initiating_job_context_missing",
    "source_already_repaired_after_blocking_audit",
    "unreal_dependency_full_rebuild_fallback",
)
_classify(
    UNSUPPORTED, "worker_runtime",
    "all_consumers_planned_excluded",
    "modeler_launch_or_recovery_io_failed", "pipeline_retry_result_missing",
    "recovery_continuation_result_missing", "repair_inventory_target_missing",
    "resumed_pipeline_result_missing",
)

# Cluster refresh is the owning transaction for current/missing generated
# receipts and changed source artifacts.  Missing canonical targets and
# ambiguous/unreadable source authority cannot safely enter that transaction.
_classify(
    REPAIRABLE, "cluster_refresh",
    "canonical_source_changed", "canonical_source_structural_changed",
    "physical_capture_changed", "physical_capture_manifest_missing",
    "recorded_source_missing", "relation_physical_proof_incomplete",
    "source_fbx_changed", "source_fbx_missing", "target_scope_changed",
)
_classify(
    UNSUPPORTED, "cluster_source_missing",
    "canonical_source_missing", "target_missing", "target_outside_owner",
)
_classify(
    FATAL, "cluster_source_integrity",
    "canonical_source_semantic_unavailable", "recorded_source_conflict",
)

# Normalized Generator delivery exposes detailed slot/result facts under one
# canonical delivery blocker.  Pass-through, matching, and nested mismatches
# therefore remain diagnostic-only at repair admission.
_classify(
    INFORMATIONAL, "relation_decision_diagnostic",
    "actionable_role_has_no_current_atlas_normalized_variants",
    "declared_generator_slot_not_declared_exactly_once",
    "explicit_target_relation_off",
    "generator_connection_all_bindings_planned_inactive",
    "generator_connection_matches_live_export",
    "generator_connection_not_requested",
    "live_generator_slot_not_declared_exactly_once",
    "relationship_continuity_only",
    "same_role_reference_provider_is_not_an_assembly_part",
    "target_mesh_asset_missing",
)

_classify(
    INFORMATIONAL, "audit_detail",
    "blend_source_index_cancelled", "current_atlas_material_mesh_connected",
    "current_material_mesh_connection_incomplete", "target_material_lineage_missing",
)
_classify(
    UNSUPPORTED, "pcg_audit_runtime",
    "asset_audit_failed", "atlas_blend_missing", "blend_source_index_error",
    "blend_source_index_error_root_exit", "blend_source_index_root_exit",
    "blend_source_index_timeout",
)

# Atlas integrity owns these decisions directly.  Duplicate IDs and invalid
# ownership marker kinds make identity data unsafe; ambiguous scopes/pairs are
# concrete manual authoring blockers.  Protected/manual/current-unused rows
# are deliberately nonblocking.
_classify(
    FATAL, "atlas_integrity",
    "atlas_ownership_marker_invalid", "duplicate_material_id", "duplicate_mesh_id",
)
_classify(
    UNSUPPORTED, "atlas_asset_authoring",
    "generator_guid_missing", "generator_slot_pair_incomplete",
    "managed_mesh_owner_ambiguous", "managed_mesh_scope_mismatch",
)
_classify(
    INFORMATIONAL, "protected_manual_asset",
    "current_unused_group", "protected_manual", "userdata_not_object",
)

# These handoff/root-selection values are nested resolution or successful
# reconciliation facts.  The outer PCG_CLUSTER_HANDOFF_NOT_READY row is the
# sole blocker when the handoff cannot proceed.
_classify(
    INFORMATIONAL, "cluster_handoff_diagnostic",
    "cluster_missing_normalized_export_pivot", "missing_export_collection",
    "multiple_unsuffixed_export_roots", "new_exact_bwr_source_hierarchy",
    "no_unsuffixed_export_root", "persisted_unsuffixed_export_root",
    "assembly_source_fbx_pending_export", "hash_validated_cluster_assembly_source",
    "no_cluster_assembly_roles",
)

_classify(
    REPAIRABLE, "cluster_refresh", "blend_missing", "source_xml_missing",
)
_classify(
    INFORMATIONAL, "lifecycle_event",
    "callback_error", "cancelled", "cleanup", "timed_out",
    "communication_error", "root_exit", "timeout",
    "owner_release_acknowledged", "runtime_shutdown", "before_marker_restore",
    "sk_stop", "sk_worker_complete", "speedtree_root_exit", "speedtree_timeout",
)
_classify(
    INFORMATIONAL, "receipt_status", "over_budget", "publication_canceled", "written",
)
_classify(
    INFORMATIONAL, "prepared_unused",
    "material_absent_from_rendered_mesh", "material_has_no_rendered_polygons",
)

_classify(
    UNSUPPORTED, "node_scope_evidence",
    "recovery_live_export_target_scope_empty",
    "recovery_target_material_scope_missing",
    "sealed_policy_has_no_required_live_targets",
)
_classify(
    REPAIRABLE, "pcg_texture", "output_set_incomplete",
)
_classify(UNSUPPORTED, "output_identity", "output_identity_missing")
_classify(
    UNSUPPORTED, "manual_asset_reference",
    "asset_texture_source_path_missing", "asset_texture_source_undeclared",
)
_classify(
    UNSUPPORTED, "canonical_material_mapping", "canonical_material_mapping_incomplete",
)
_classify(
    UNSUPPORTED, "generator_authority", "authoritative_property_pair_missing",
)
_classify(UNSUPPORTED, "report_missing", "missing_pipeline_report")
_classify(UNSUPPORTED, "worker_runtime", "worker_wait_failed")
_classify(UNSUPPORTED, "export_output_missing", "output_missing")
_classify(
    UNSUPPORTED, "dependency_output_missing", "dependency_output_missing",
)
_classify(
    UNSUPPORTED, "dependency_output_stale", "dependency_output_stale",
)
_classify(UNSUPPORTED, "data_failure", "data_error")
_classify(UNSUPPORTED, "internal_failure", "internal_error")
_classify(
    UNSUPPORTED, "unreal_unavailable_failure", "unreal_unavailable",
)
_classify(
    UNSUPPORTED, "dependency_failure_wrapper",
    "required_cluster_repair_failed",
)

# Canonical texture issues name the exact PCG remediation only when Step 3 can
# safely regenerate outputs from the inventory target.  Unknown roles remain
# manual authoring; malformed/scope-invalid manifests fail as data integrity.
_classify(
    REPAIRABLE, "pcg_texture",
    "canonical_output_has_no_material_targets", "canonical_output_manifest_empty",
    "canonical_output_manifest_missing", "material_canonical_output_unmapped",
    "material_canonical_role_unmapped",
)
_classify(
    UNSUPPORTED, "texture_authoring",
    "isolated_texture_role_unmapped", "speedtree_texture_role_unknown",
)
_classify(
    FATAL, "manifest_corruption",
    "canonical_output_manifest_invalid_json",
    "canonical_output_manifest_schema_mismatch", "generator_material_scope_unreadable",
    "invalid_required_roles", "invalid_texture_base",
    "isolated_texture_rebase_verification_failed",
)
_classify(
    FATAL, "texture_scope_integrity",
    "manifest_outside_texture_root", "material_texture_origin_invalid",
    "production_spm_is_derived_cache", "production_spm_outside_manifest_asset",
)
_classify(
    INFORMATIONAL, "nonparticipating_material",
    "not_referenced_by_generator_material_property",
)

# A recipe-gated Cluster mismatch was historically registered unsupported and
# then silently promoted by orchestration.  Its single registry row now owns
# that conditional exact action and its canonical recipe requirement.
_classify(
    REPAIRABLE, "cluster_refresh_recipe",
    "asset_cluster_bake_texture_contract_invalid",
)
_classify(
    REPAIRABLE, "cluster_refresh_atlas_authority",
    "atlas_manifest_authority_missing",
)
_classify(
    REPAIRABLE, "cluster_refresh_atlas_conflict",
    "atlas_manifest_resolution_conflict",
)
_classify(REPAIRABLE, "cluster_refresh_lineage", "lineage_unproven")
_classify(
    REPAIRABLE, "cluster_refresh_variants_required",
    "normalized_variants_required",
)


_seeded_unclassified = {
    code for code, row in _REASON_SEEDS.items()
    if row.disposition == UNCLASSIFIED
}
_missing_domain_calls = sorted(
    _seeded_unclassified - set(_DOMAIN_CLASSIFICATIONS)
)
_unknown_domain_calls = sorted(
    set(_DOMAIN_CLASSIFICATIONS) - set(_REASON_SEEDS)
)
if _missing_domain_calls or _unknown_domain_calls:
    raise RuntimeError(
        "reason domain classification table does not match the seeded ledger; "
        f"missing={_missing_domain_calls}, unknown={_unknown_domain_calls}"
    )


REASON_REGISTRY: dict[str, ReasonRow] = {}
for _code, _seed in _REASON_SEEDS.items():
    _disposition, _policy = _DOMAIN_CLASSIFICATIONS.get(
        _code,
        (_seed.disposition, _seed.policy),
    )
    _contract = POLICY_CONTRACTS.get(_policy)
    if _contract is None:
        raise RuntimeError(
            f"reason code {_code!r} names unknown policy {_policy!r}"
        )
    REASON_REGISTRY[_code] = ReasonRow(
        _disposition,
        _seed.owner,
        _policy,
        _contract.repair_action,
        _contract.evidence_requirements,
        _contract.friendly_cause,
        _contract.operator_action,
        _contract.evidence_failure_cause,
        _contract.evidence_failure_action,
        _contract.fallback_only,
        _contract.phase_routed,
    )


UNCLASSIFIED_CEILING = 0

# The planner now derives its vocabulary exclusively from emitted registry
# rows.  Historical aliases with no production emitter were deleted rather
# than preserved as unreachable repair promises.
UNEMITTED_PLANNER_CODES = frozenset()


BLOCKING_DISPOSITIONS = frozenset({REPAIRABLE, UNSUPPORTED, FATAL})
BLOCKING_REASON_CODES = frozenset(
    code for code, row in REASON_REGISTRY.items()
    if row.disposition in BLOCKING_DISPOSITIONS
)
DURABLE_FAILURE_REASON_CODES = frozenset(
    code for code, row in REASON_REGISTRY.items()
    if row.phase_routed
)


def reason_row(code: str) -> ReasonRow:
    """Return one registered row; unknown runtime tokens fail closed."""

    normalized = normalize_reason_code(code) or str(code).strip().casefold()
    row = REASON_REGISTRY.get(normalized)
    if row is not None:
        return row
    return ReasonRow(
        UNSUPPORTED,
        "unregistered_runtime_reason",
        "generic_terminal",
        friendly_cause=(
            f"등록되지 않은 차단 코드 '{normalized or '<empty>'}'가 감지되었습니다."
        ),
        operator_action=(
            "자동 복구를 실행하지 말고 owning gate의 structured evidence를 "
            "보존한 채 registry와 scanner coverage를 먼저 갱신하세요."
        ),
    )


def _walk_evidence(value: Any, trail: tuple[str, ...] = ()):
    if isinstance(value, Mapping):
        for key, child in value.items():
            child_trail = (*trail, str(key))
            yield child_trail, child
            yield from _walk_evidence(child, child_trail)
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            child_trail = (*trail, str(index))
            yield child_trail, child
            yield from _walk_evidence(child, child_trail)


def _detail_values(evidence: Mapping[str, Any], keys: set[str]) -> list[str]:
    values = []
    seen = set()
    for trail, value in _walk_evidence(evidence):
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


def present_reason(
    code: str,
    evidence: Mapping[str, Any],
) -> tuple[str, str]:
    """Render the registry-owned operator presentation with evidence details."""

    row = reason_row(code)
    cause = row.friendly_cause
    action = row.operator_action
    if row.policy == "export_material":
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
        action += suffix
    elif row.policy == "exporter_crash":
        attempts = _detail_values(evidence, {"attempt_count", "attempts"})
        if attempts:
            cause = cause.rstrip(".") + f" ({attempts[0]}회)."
    elif row.policy == "cluster_data":
        missing = _detail_values(evidence, {"missing"})
        invalid = _detail_values(evidence, {"invalid"})
        expected = _detail_values(evidence, {"expected_base"})
        if missing:
            cause = "Cluster가 참조하는 TGA 파일이 없습니다: " + ", ".join(
                missing[:8]
            )
        elif invalid:
            cause = "Cluster TGA basename이 올바르지 않습니다: " + ", ".join(
                invalid[:8]
            )
        if expected:
            action += " 기준 basename: " + ", ".join(expected[:4])
    return str(cause), str(action)


def present_evidence_failure(
    row: ReasonRow,
    evidence: Mapping[str, Any],
) -> tuple[str, str]:
    """Render missing-canonical-evidence text owned by a repair policy."""

    cause = row.evidence_failure_cause or row.friendly_cause
    action = row.evidence_failure_action or row.operator_action
    if CURRENT_MATERIAL_BINDING_RECIPE in row.evidence_requirements:
        material_ids = _detail_values(evidence, {"material_id", "material_ids"})
        roles = _detail_values(
            evidence,
            {"role", "roles", "map_role", "map_roles"},
        )
        details = []
        if material_ids:
            details.append("material IDs " + ", ".join(material_ids[:8]))
        if roles:
            details.append("roles " + ", ".join(roles[:8]))
        if details:
            cause = cause.rstrip(".") + " (" + "; ".join(details) + ")."
    return str(cause), str(action)


def disposition_of(code: str) -> str:
    """Return the registered disposition; unknown runtime codes are unsupported."""

    return reason_row(code).disposition


def codes_with(disposition: str) -> tuple[str, ...]:
    return tuple(sorted(
        code for code, row in REASON_REGISTRY.items()
        if row.disposition == disposition
    ))
