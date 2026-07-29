"""Fast pre-Blender SpeedTree FBX/STMAT material preflight.

This process performs only the cached SpeedTree FBX export and read-only SPM /
STMAT checks.  Blender is launched only after this succeeds.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import traceback
from pathlib import Path


JOBS_DIR = Path(__file__).resolve().parent
SK_BATCH_DIR = JOBS_DIR.parent
if str(SK_BATCH_DIR) not in sys.path:
    sys.path.insert(0, str(SK_BATCH_DIR))
BATCH_TOOLS_DIR = SK_BATCH_DIR.parent
if str(BATCH_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(BATCH_TOOLS_DIR))

from spm_leaf_handoff_contract import (  # noqa: E402
    inspect_all_speedtree_material_export,
    inspect_speedtree_material_export,
    inspect_speedtree_texture_sources,
    inspect_spm_leaf_contract,
    inspect_spm_mesh_file_references,
    leaf_contract_user_message,
)
from speedtree_texture_contract import (  # noqa: E402
    REQUIRED_TEXTURE_ROLES,
    TEXTURE_ORIGIN_BLENDER_CLUSTER_BAKE,
    TEXTURE_ORIGIN_NEEDS_PCG_GENERATION,
    inspect_spm_texture_slots,
    read_stmat_material_sources,
    resolve_blender_cluster_bake_origin,
    resolve_texture_bindings,
)
from speedtree_export_options_contract import (  # noqa: E402
    require_texture_skip_writing,
)
from pcg_st9_texture_batch.pcg_texture_audit import (  # noqa: E402
    _unsafe_provisional_source,
    atlas_provisional_source_declarations,
    canonical_material_name as pcg_canonical_material_name,
    texture_base_for_material,
)
from pcg_st9_texture_batch.pcg_texture_common import (  # noqa: E402
    DEFAULT_CONFIG as PCG_DEFAULT_CONFIG,
    load_config as load_pcg_texture_config,
)
from speedtree_pipeline_contract import (  # noqa: E402
    PREFLIGHT_CONTRACT_KIND,
    PREFLIGHT_SCHEMA_VERSION,
    build_preflight_envelope,
    naming_shadow_issue,
    read_tree_instance_profile,
    shared_contract_api,
    speedtree_stmat_path as contract_stmat_path,
)


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--spm", required=True)
    parser.add_argument("--canonical-spm", default="")
    parser.add_argument("--speedtree-exe", required=True)
    parser.add_argument("--fbx-ini", required=True)
    parser.add_argument("--speedtree-cli", required=True)
    parser.add_argument("--report", required=True)
    parser.add_argument("--timeout", type=int, default=900)
    return parser.parse_args()


def write_report(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def load_speedtree_cli(path):
    path = Path(path)
    spec = importlib.util.spec_from_file_location("sk_batch_speedtree_cli", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"SpeedTree export helper를 불러올 수 없음: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def problem_generators(contract, missing_materials):
    missing = {str(name).casefold() for name in missing_materials}
    rows = []
    seen = set()
    for slot in contract.get("slots") or []:
        if not slot.get("export_participates", slot.get("visible")):
            continue
        if not slot.get("valid"):
            continue
        if str(slot.get("material_name") or "").casefold() not in missing:
            continue
        key = (
            str(slot.get("generator_guid") or ""),
            str(slot.get("slot_prefix") or ""),
            str(slot.get("material_name") or ""),
        )
        if key in seen:
            continue
        seen.add(key)
        rows.append({
            "generator_guid": slot.get("generator_guid", ""),
            "generator_name": slot.get("generator_name", ""),
            "generator_type": slot.get("generator_type", ""),
            "slot_prefix": slot.get("slot_prefix", ""),
            "material_id": slot.get("material_id"),
            "material_name": slot.get("material_name", ""),
            "generated_node_count": slot.get("generated_node_count", 0),
        })
    return rows


def _material_contract_key(value):
    value = str(value or "").strip()
    if value.casefold().endswith("_mat"):
        value = value[:-4]
    if value.casefold().startswith("m_"):
        value = value[2:]
    return "".join(character for character in value.casefold()
                   if character.isalnum())


def _path_key(value):
    try:
        return str(Path(str(value or "")).resolve()).casefold()
    except (OSError, RuntimeError):
        return str(value or "").casefold()


def _production_asset_root(spm_path):
    spm = Path(spm_path).resolve()
    cluster = next(
        (
            parent for parent in spm.parents
            if parent.name.casefold() == "cluster"
        ),
        None,
    )
    return cluster.parent if cluster is not None else spm.parent


def _is_cluster_capture_source_set(paths):
    """True for maps stored directly in an asset Cluster output folder."""
    try:
        resolved = [
            Path(path).expanduser().resolve()
            for path in paths
            if str(path or "").strip()
        ]
    except (OSError, TypeError, ValueError):
        return False
    return bool(resolved) and all(
        path.parent.name.casefold() == "cluster"
        for path in resolved
    )


def augment_texture_readiness_contract(
    readiness,
    stmat_path,
    production_spm,
    *,
    source_texture_roots=None,
):
    """Add provisional/raw and Blender-bake provenance to STMAT readiness."""
    readiness = dict(readiness or {})
    bindings = [
        dict(binding)
        for binding in readiness.get("bindings") or []
    ]
    readiness["bindings"] = bindings
    parsed = read_stmat_material_sources(stmat_path)
    parsed_by_index = {
        int(material["material_index"]): material
        for material in parsed.get("materials") or []
    }
    spm_inspection = inspect_spm_texture_slots(production_spm)
    spm_by_key = {}
    for material in spm_inspection.get("materials") or []:
        spm_by_key.setdefault(
            _material_contract_key(material.get("material_name")),
            [],
        ).append(material)
    asset_root = _production_asset_root(production_spm)
    provisional_declarations = atlas_provisional_source_declarations(
        asset_root
    )
    source_roots = list(
        source_texture_roots
        if source_texture_roots is not None
        else PCG_DEFAULT_CONFIG.get("source_texture_roots") or []
    )
    warnings = list(readiness.get("warnings") or [])
    missing = list(readiness.get("missing") or [])

    for binding in bindings:
        if binding.get("status") != "not_managed":
            continue
        material = parsed_by_index.get(
            int(binding.get("material_index", -1))
        )
        sources = list((material or {}).get("sources") or [])
        if not sources:
            continue
        source_paths = {
            str(source.get("map") or "").strip().casefold(): str(
                source.get("resolved_source") or ""
            )
            for source in sources
            if source.get("resolved_source")
        }
        spm_matches = spm_by_key.get(
            _material_contract_key(binding.get("material")),
            [],
        )
        bake_receipt = {}
        bake_issue = ""
        spm_source_paths = {}
        if len(spm_matches) == 1:
            spm_material = spm_matches[0]
            spm_slots = list(spm_material.get("slots") or [])
            spm_source_paths = {
                str(slot.get("map") or "").strip().casefold(): str(
                    slot.get("resolved_ref") or ""
                )
                for slot in spm_slots
                if slot.get("resolved_ref")
            }
            if {
                _path_key(path)
                for path in source_paths.values()
            } == {
                _path_key(path)
                for path in spm_source_paths.values()
            }:
                bake_receipt, bake_issue = (
                    resolve_blender_cluster_bake_origin(
                        production_spm,
                        spm_material,
                        {
                            "slot_files": [
                                {
                                    "map_index": int(
                                        slot.get("map_index", -1)
                                    ),
                                    "map": str(slot.get("map") or ""),
                                    "role": str(slot.get("role") or ""),
                                    "path": str(
                                        slot.get("resolved_ref") or ""
                                    ),
                                }
                                for slot in spm_slots
                            ],
                        },
                        asset_root,
                    )
                )
                if bake_receipt:
                    bake_receipt = {
                        **bake_receipt,
                        "material_id": str(
                            spm_material.get("material_id") or ""
                        ),
                        "material_name": str(
                            spm_material.get("material_name") or ""
                        ),
                        "slot_files": [
                            {
                                "map_index": int(
                                    slot.get("map_index", -1)
                                ),
                                "map": str(slot.get("map") or ""),
                                "role": str(slot.get("role") or ""),
                                "path": str(
                                    slot.get("resolved_ref") or ""
                                ),
                            }
                            for slot in spm_slots
                        ],
                    }
        if bake_receipt:
            binding.update({
                "texture_contract_status": "blender_cluster_bake",
                "origin_kind": "blender_cluster_bake",
                "origin_state": TEXTURE_ORIGIN_BLENDER_CLUSTER_BAKE,
                "origin_receipt": bake_receipt,
                "source_paths": spm_source_paths,
            })
            continue
        if (
            bake_issue
            and _is_cluster_capture_source_set(
                spm_source_paths.values(),
            )
        ):
            validation = {
                "classification":
                    "asset_cluster_bake_texture_contract_invalid",
                "issue": bake_issue,
                "material": str(
                    (spm_matches[0] if spm_matches else {}).get(
                        "material_name"
                    )
                    or binding.get("material")
                    or ""
                ),
                "material_id": str(
                    (spm_matches[0] if spm_matches else {}).get(
                        "material_id"
                    )
                    or ""
                ),
                "source_paths": dict(spm_source_paths),
            }
            binding.update({
                "status": "blocked_source",
                "texture_contract_status": "blocked_source",
                "source_paths": dict(spm_source_paths),
                "origin_validation": validation,
            })
            missing.append({
                "material": binding.get("material"),
                "material_index": binding.get("material_index"),
                "reason": "blender_cluster_bake_origin_invalid",
                "origin_validation_issue": bake_issue,
                "origin_validation": validation,
            })
            continue

        expected_base = texture_base_for_material(
            pcg_canonical_material_name(
                str(binding.get("material") or "").removesuffix("_Mat")
            )
        )
        expected_t_paths = {
            role: str(
                asset_root / "texture"
                / f"{expected_base}_{role}.tga"
            )
            for role in REQUIRED_TEXTURE_ROLES
        }
        rejected = []
        for source in sources:
            path = str(source.get("resolved_source") or "")
            state = _unsafe_provisional_source(
                path,
                asset_root=asset_root,
                source_texture_roots=source_roots,
                declared_source_paths=provisional_declarations,
            )
            if state:
                rejected.append({
                    "material": binding.get("material"),
                    "role": str(source.get("map") or "").casefold(),
                    "path": path,
                    "state": state,
                })
        if rejected:
            binding["status"] = "blocked_source"
            binding["texture_contract_status"] = "blocked_source"
            binding["source_rejections"] = rejected
            missing_row = {
                "material": binding.get("material"),
                "material_index": binding.get("material_index"),
                "reason": "provisional_source_blocked",
                "missing_roles": sorted({
                    row["role"] for row in rejected if row["role"]
                }),
                "expected_texture_base": expected_base,
                "expected_t_paths": expected_t_paths,
                "source_rejections": rejected,
            }
            missing.append(missing_row)
            continue

        source_roles = sorted(source_paths)
        warning = (
            f"{binding.get('material')}: 원본 texture 역할 "
            f"{', '.join(source_roles)}을 provisional로 사용 중입니다. "
            f"{expected_base} 6역할 T_* output 생성이 필요합니다."
        )
        binding.update({
            "texture_contract_status":
                "source_fallback_needs_pcg_generation",
            "origin_state": TEXTURE_ORIGIN_NEEDS_PCG_GENERATION,
            "source_paths": source_paths,
            "source_roles": source_roles,
            "expected_texture_base": expected_base,
            "expected_t_paths": expected_t_paths,
            "warning": warning,
            "remediation": (
                "PCG ST9 Texture에서 Substance 그래프 생성 및 export 실행"
            ),
        })
        warnings.append({
            "material": binding.get("material"),
            "material_index": binding.get("material_index"),
            "status": "source_fallback_needs_pcg_generation",
            "origin_state": TEXTURE_ORIGIN_NEEDS_PCG_GENERATION,
            "source_paths": source_paths,
            "source_roles": source_roles,
            "expected_texture_base": expected_base,
            "expected_t_paths": expected_t_paths,
            "warning": warning,
            "remediation": (
                "PCG ST9 Texture에서 Substance 그래프 생성 및 export 실행"
            ),
        })

    readiness["missing"] = missing
    readiness["warnings"] = warnings
    if missing:
        readiness["status"] = "incomplete"
    elif warnings:
        readiness["status"] = (
            "source_fallback_needs_pcg_generation"
        )
    return readiness


def run_export(args, speedtree_cli):
    spm = Path(args.spm)
    target = spm.parent / "fbx" / f"{spm.stem}.fbx"
    require_texture_skip_writing(
        args.fbx_ini,
        purpose=f"{spm.name} material-preflight FBX export",
    )
    return speedtree_cli.export_target(
        exe=Path(args.speedtree_exe),
        spm=spm,
        options=Path(args.fbx_ini),
        kind="fbx",
        target=target,
        timeout_seconds=max(1, int(args.timeout)),
    )


def _issue(code, scope, entity, message, severity="error", details=None):
    row = {
        "code": code,
        "severity": severity,
        "scope": scope,
        "entity": str(entity or ""),
        "message": str(message or code),
    }
    if details:
        row["details"] = details
    return row


def preflight_contract_issues(report):
    """Map legacy workflow statuses to stable, additive issue codes."""
    issues = []
    leaf = report.get("leaf_reference_contract") or {}
    leaf_status = str(leaf.get("status") or "")
    leaf_codes = {
        "inspection_error": "SPM_LEAF_INSPECTION_ERROR",
        "invalid_references": "ATLAS_REFERENCE_INVALID",
        "replacement_needed": "ATLAS_REPLACEMENT_REQUIRED",
    }
    if leaf_status in leaf_codes:
        issues.append(
            _issue(
                leaf_codes[leaf_status],
                "spm",
                report.get("spm"),
                leaf.get("error")
                or "; ".join(leaf.get("blocking_issues") or leaf.get("issues") or [])
                or leaf_status,
            )
        )
    ownership = leaf.get("managed_ownership_provenance") or {}
    if ownership.get("status") == "marker_only":
        issues.append(
            _issue(
                "ATLAS_OWNERSHIP_PROVENANCE_MISMATCH",
                "atlas_builder",
                report.get("spm"),
                ownership.get("reason")
                or "Atlas builder ownership provenance is marker-only",
                severity="warning",
                details={
                    "material_names": ownership.get("material_names") or [],
                    "validation": "shadow_only_no_mutation_change",
                },
            )
        )

    mesh_files = report.get("mesh_file_reference_contract") or {}
    if mesh_files.get("missing"):
        issues.append(
            _issue(
                "SPM_MESH_FILE_MISSING",
                "spm",
                report.get("spm"),
                report.get("error")
                or "SPM이 참조하는 외부 메시 FBX가 디스크에 없음",
                details={
                    "missing_mesh_files": [
                        str(row.get("filename") or "")
                        for row in mesh_files.get("missing") or []
                    ],
                },
            )
        )

    material = report.get("material_export_contract") or {}
    material_status = str(material.get("status") or "")
    material_codes = {
        "missing_stmat": "STMAT_MISSING",
        "invalid_stmat": "STMAT_INVALID",
        "stale": "STMAT_STALE",
        "missing_materials": "MATERIAL_EXPORT_MISSING",
    }
    if material_status in material_codes:
        issues.append(
            _issue(
                material_codes[material_status],
                "stmat",
                material.get("stmat"),
                material.get("error") or material_status,
                details={"missing_materials": material.get("missing_materials") or []},
            )
        )

    all_material = report.get("all_export_material_contract") or {}
    all_material_status = str(all_material.get("status") or "")
    if all_material.get("warning_code"):
        issues.append(
            _issue(
                all_material.get("warning_code"),
                "all_export_materials",
                all_material.get("spm") or report.get("spm"),
                all_material.get("warning")
                or "Export-participating material references could not be proven",
                severity="warning",
                details={
                    "coverage_confidence": all_material.get(
                        "coverage_confidence"
                    )
                    or "unknown",
                },
            )
        )
    if all_material_status == "inspection_error":
        issues.append(
            _issue(
                "ALL_EXPORT_INSPECTION_ERROR",
                "spm",
                report.get("spm"),
                all_material.get("error") or all_material_status,
            )
        )
    elif all_material_status in material_codes:
        code = (
            "ALL_EXPORT_MATERIAL_MISSING"
            if all_material_status == "missing_materials"
            else material_codes[all_material_status]
        )
        issues.append(
            _issue(
                code,
                "all_export_materials",
                all_material.get("stmat"),
                all_material.get("error") or all_material_status,
                details={
                    "missing_materials": all_material.get("missing_materials") or [],
                    "missing_material_records": all_material.get(
                        "missing_material_records"
                    )
                    or [],
                },
            )
        )

    textures = report.get("texture_source_contract") or {}
    if textures.get("status") not in {None, "", "ok"}:
        code = {
            "missing_stmat": "STMAT_MISSING",
            "invalid_stmat": "STMAT_INVALID",
            "stale": "STMAT_STALE",
            "missing_sources": "TEXTURE_SOURCE_MISSING",
        }.get(str(textures.get("status")), "TEXTURE_SOURCE_INVALID")
        issues.append(
            _issue(
                code,
                "texture_source",
                textures.get("stmat"),
                textures.get("error") or textures.get("status"),
                details={"missing_sources": textures.get("missing_sources") or []},
            )
        )

    readiness = report.get("texture_readiness_contract") or {}
    for warning in readiness.get("warnings") or []:
        issues.append(
            _issue(
                "TEXTURE_SOURCE_FALLBACK_NEEDS_PCG_GENERATION",
                "material",
                warning.get("material"),
                warning.get("warning")
                or "Original source texture is provisional",
                severity="warning",
                details={
                    "material_index": warning.get("material_index"),
                    "source_roles": warning.get("source_roles") or [],
                    "source_paths": warning.get("source_paths") or {},
                    "expected_texture_base": warning.get(
                        "expected_texture_base"
                    )
                    or "",
                    "expected_t_paths": warning.get("expected_t_paths")
                    or {},
                    "remediation": warning.get("remediation") or "",
                },
            )
        )
    for missing in readiness.get("missing") or []:
        issues.append(
            _issue(
                "TEXTURE_SET_INCOMPLETE",
                "material",
                missing.get("material"),
                missing.get("reason") or "managed texture set is incomplete",
                details={
                    "material_index": missing.get("material_index"),
                    "missing_roles": missing.get("missing_roles") or [],
                    "expected_texture_base": missing.get("expected_texture_base")
                    or missing.get("texture_base")
                    or "",
                },
            )
        )
    if report.get("status") == "failed" and not issues:
        issues.append(
            _issue(
                "PREFLIGHT_ERROR",
                "preflight",
                report.get("spm"),
                report.get("error") or "SpeedTree material preflight failed",
            )
        )
    return issues


def attach_pipeline_contract(report, spm_path):
    status = str(report.get("status") or "failed")
    outcome = "ok" if status == "ok" else "blocked" if status == "blocked" else "error"
    texture_readiness = report.get("texture_readiness_contract") or {}
    stmat = (
        (report.get("texture_source_contract") or {}).get("stmat")
        or texture_readiness.get("stmat")
        or contract_stmat_path(spm_path)
    )
    envelope = build_preflight_envelope(
        spm_path,
        stmat,
        outcome=outcome,
        texture_readiness=texture_readiness,
        issues=preflight_contract_issues(report),
    )
    api = shared_contract_api()
    for intent in envelope.get("material_intents") or []:
        material_name = api.normalize_material_name(intent.get("material_name"))
        sk_name = (
            material_name
            if material_name.casefold().startswith("m_")
            else "M_" + material_name
        )
        shadow = naming_shadow_issue(
            material_name,
            production_canonical_name=pcg_canonical_material_name(material_name),
            workflow_canonical_name=sk_name,
            workflow="sk_batch",
        )
        if shadow is not None:
            envelope["issues"].append(shadow)
    report["speedtree_pipeline_contract"] = envelope
    report["instance_profile"] = envelope.get("instance_profile", "")
    return envelope


def main():
    args = parse_args()
    speedtree_spm = Path(args.spm).resolve()
    canonical_spm = Path(getattr(args, "canonical_spm", "") or args.spm).resolve()
    report = {
        "status": "failed",
        "stage": "speedtree_material_preflight",
        "spm": str(canonical_spm),
        "speedtree_spm": str(speedtree_spm),
        "contract_kind": PREFLIGHT_CONTRACT_KIND,
        "schema_version": PREFLIGHT_SCHEMA_VERSION,
    }
    try:
        # Invalid Tree User data is a real authoring error.  Material labels
        # such as Green/Yellow/Dead never substitute for this model-wide key.
        report["instance_profile"] = read_tree_instance_profile(speedtree_spm)
        speedtree_cli = load_speedtree_cli(args.speedtree_cli)
        leaf_contract = inspect_spm_leaf_contract(speedtree_spm)
        leaf_ok, leaf_message = leaf_contract_user_message(leaf_contract)
        report["leaf_reference_contract"] = leaf_contract
        mesh_files = inspect_spm_mesh_file_references(speedtree_spm)
        report["mesh_file_reference_contract"] = mesh_files
        missing_mesh_files = list(mesh_files.get("missing") or [])
        if not leaf_ok:
            report["status"] = "blocked"
            report["error"] = leaf_message
        elif missing_mesh_files:
            names = ", ".join(
                str(row.get("filename") or "?")
                for row in missing_mesh_files[:8]
            )
            if len(missing_mesh_files) > 8:
                names += f" 외 {len(missing_mesh_files) - 8}개"
            report["status"] = "blocked"
            report["error"] = (
                "SPM이 참조하는 외부 메시 FBX "
                f"{len(missing_mesh_files)}개가 디스크에 없음 — " + names
                + ". 이 상태의 SpeedTree 익스포트는 타임아웃까지 멈추므로 "
                "실행 전에 차단했습니다. Modeler에서 Mesh Asset 참조를 "
                "정리하거나 파일을 복구하세요."
            )
            report["classification"] = (
                "asset_external_mesh_path_missing"
            )
            report["remediation"] = (
                "Restore the listed external FBX files at their declared "
                "paths, or relink/remove those Mesh Asset references in "
                "SpeedTree Modeler and save the SPM"
            )
            report["missing_external_meshes"] = [
                {
                    "filename": str(row.get("filename") or ""),
                    "resolved_path": str(
                        row.get("resolved_path") or ""
                    ),
                    "mesh_name": str(row.get("mesh_name") or ""),
                }
                for row in missing_mesh_files
            ]
        else:
            report["speedtree_export"] = run_export(args, speedtree_cli)
            material = inspect_speedtree_material_export(
                speedtree_spm, leaf_contract
            )
            all_material = inspect_all_speedtree_material_export(speedtree_spm)
            textures = inspect_speedtree_texture_sources(speedtree_spm)
            texture_readiness = augment_texture_readiness_contract(
                resolve_texture_bindings(textures.get("stmat")),
                textures.get("stmat"),
                canonical_spm,
                source_texture_roots=(
                    load_pcg_texture_config().get(
                        "source_texture_roots"
                    )
                    or []
                ),
            )
            report["material_export_contract"] = material
            report["all_export_material_contract"] = all_material
            report["texture_source_contract"] = textures
            report["texture_readiness_contract"] = texture_readiness
            leaf_missing = list(material.get("missing_materials") or [])
            missing = list(dict.fromkeys(
                leaf_missing + list(all_material.get("missing_materials") or [])
            ))
            missing_sources = list(textures.get("missing_sources") or [])
            missing_sets = list(texture_readiness.get("missing") or [])
            if textures.get("classification"):
                report["classification"] = textures["classification"]
                report["failure_reason"] = textures.get(
                    "failure_reason", ""
                )
                report["remediation"] = textures.get("remediation", "")
            if (
                material.get("status") in {"ok", "not_applicable"}
                and all_material.get("status") in {"ok", "not_applicable"}
                and textures.get("status") == "ok"
                and texture_readiness.get("status") in {
                    "ok",
                    "not_applicable",
                    "source_fallback_needs_pcg_generation",
                }
            ):
                report["status"] = "ok"
            elif missing:
                problems = problem_generators(leaf_contract, leaf_missing)
                report["problem_generators"] = problems
                report["classification"] = "asset_export_material_missing"
                report["failure_reason"] = (
                    "The live SpeedTree FBX/STMAT export does not contain "
                    "materials referenced by visible SPM generators"
                )
                report["remediation"] = (
                    "In SpeedTree Modeler, assign/export the listed materials "
                    "on the reported visible generators, or remove obsolete "
                    "material references, then save the SPM and rerun"
                )
                report["missing_export_materials"] = missing
                # Material preflight is a diagnostic boundary.  Never write a
                # foreground marker into the source SPM here: that cosmetic
                # edit invalidates the already-current Blender repair, SK
                # calibration and Unreal Push receipts.  The report contains
                # the exact Generator GUIDs/names for UI-side highlighting.
                report["problem_node_marker"] = {
                    "status": "reported_only",
                    "changed": False,
                    "marked": [],
                }
                report["status"] = "blocked"
                if missing:
                    names = ", ".join(missing)
                    node_names = ", ".join(
                        sorted({
                            str(item.get("generator_name") or "<이름 없음>")
                            for item in problems
                        })
                    )
                    marker_note = (
                        f" 문제 Generator: {node_names}."
                        if problems
                        else ""
                    )
                    report["error"] = (
                        "현재 실제 내보내는 노드의 재질이 SpeedTree FBX에서 빠짐 — "
                        + names + "." + marker_note + " SPM은 수정하지 않았습니다."
                    )
                else:
                    report["error"] = (
                        "SpeedTree FBX 재질 사전검사 실패 — "
                        + str(material.get("status") or "unknown")
                    )
            else:
                report["status"] = "blocked"
                if missing_sets:
                    details = ", ".join(
                        f"{item.get('material', '?')}:"
                        f"{','.join(item.get('missing_roles') or []) or item.get('reason', '?')}"
                        for item in missing_sets[:8]
                    )
                    report["error"] = (
                        "SpeedTree managed texture set is incomplete: "
                        + details
                    )
                elif missing_sources:
                    details = ", ".join(
                        f"{item.get('material', '?')}:{item.get('map', '?')}"
                        " -> "
                        + (
                            str(item.get("resolved") or "<Source 미지정>")
                        )
                        for item in missing_sources[:8]
                    )
                    report["error"] = (
                        "에셋 텍스처 Source 계약 오류 — "
                        + details
                    )
                else:
                    report["error"] = (
                        "SpeedTree 텍스처 사전검사 실패 — "
                        + str(textures.get("status") or "unknown")
                    )
    except Exception as exc:
        report["status"] = "failed"
        report["error"] = str(exc)
        report["traceback"] = traceback.format_exc()
    try:
        attach_pipeline_contract(report, speedtree_spm)
    except Exception as exc:
        report["status"] = "failed"
        report["pipeline_contract_error"] = str(exc)
        report["error"] = report.get("error") or str(exc)
        report.setdefault("traceback", traceback.format_exc())
    write_report(args.report, report)
    if report["status"] != "ok":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
