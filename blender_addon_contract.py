"""Pure-Python contract for SpeedTree Batch Tools <-> Blender add-ons.

The batch process owns orchestration.  Blender add-ons own application-local
mutation.  :mod:`blender_addon_gateway` is the only supported place where a
batch worker may resolve add-on implementation symbols.

This module deliberately has no ``bpy`` dependency so launchers and tests can
validate a request before Blender is started.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path


CONTRACT_SCHEMA_VERSION = 1
GATEWAY_API_NAME = "speedtree_batch_tools.blender_addon_gateway"
GATEWAY_API_VERSION = 1


OWNERSHIP = {
    "batch": {
        "owns": [
            "target_selection",
            "job_queue_and_process_lifecycle",
            "retry_and_timeout_policy",
            "filesystem_transactions_outside_blender",
            "request_and_receipt_persistence",
        ],
        "must_not": [
            "import_addon_implementation_modules",
            "infer_addon_capabilities_from_version_strings",
            "mutate_blender_scene_without_gateway_contract",
        ],
    },
    "gateway": {
        "owns": [
            "addon_enablement",
            "capability_negotiation",
            "implementation_symbol_resolution",
            "loaded_source_identity",
            "runtime_receipts",
        ],
        "must_not": [
            "select_production_targets",
            "schedule_or_retry_jobs",
            "silently_fallback_on_missing_capabilities",
        ],
    },
    "addon": {
        "owns": [
            "blender_scene_and_datablock_mutation",
            "addon_specific_speedtree_semantics",
            "addon_specific_unreal_handoff_semantics",
            "operation_postconditions",
        ],
        "must_not": [
            "mutate_the_batch_queue",
            "choose_unrequested_targets",
            "own_cross_process_retry_policy",
        ],
    },
}


ADDONS = {
    "speedtree_bone_weight_repair": {
        "module": "speedtree_bone_weight_repair",
        "native_api_module": None,
        "minimum_native_api_version": None,
        "source_environment": "SPEEDTREE_BWR_ADDON_DIR",
        "capabilities": {
            "spm_sk_preflight_v1": {
                "operations": ["require_spm_sk_ready"],
            },
            "speedtree_export_v1": {
                "operations": ["run_speedtree_cli_export"],
            },
            "repair_pipeline_v1": {
                "operations": ["run_import_and_repair"],
            },
            "material_handoff_v1": {
                "operations": [
                    "consolidate_speedtree_group_materials",
                    "load_speedtree_texture_readiness_contract",
                    "normalize_speedtree_material_textures",
                ],
            },
            "atlas_manifest_consumer_v1": {
                "operations": ["speedtree_manifest_paths"],
            },
        },
        "operations": {
            "require_spm_sk_ready": (
                "speedtree_bone_weight_repair.core:require_spm_sk_ready"
            ),
            "run_speedtree_cli_export": (
                "speedtree_bone_weight_repair.core:run_speedtree_cli_export"
            ),
            "run_import_and_repair": (
                "speedtree_bone_weight_repair.core:run_import_and_repair"
            ),
            "consolidate_speedtree_group_materials": (
                "speedtree_bone_weight_repair.core:"
                "consolidate_speedtree_group_materials"
            ),
            "load_speedtree_texture_readiness_contract": (
                "speedtree_bone_weight_repair.core:"
                "load_speedtree_texture_readiness_contract"
            ),
            "normalize_speedtree_material_textures": (
                "speedtree_bone_weight_repair.core:"
                "normalize_speedtree_material_textures"
            ),
            "speedtree_manifest_paths": (
                "speedtree_bone_weight_repair.core:_speedtree_manifest_paths"
            ),
        },
    },
    "atlas_leaf_mesh_builder": {
        "module": "atlas_leaf_mesh_builder",
        "native_api_module": "atlas_leaf_mesh_builder.integration_api",
        "minimum_native_api_version": 1,
        "source_environment": "SPEEDTREE_ATLAS_ADDON_DIR",
        "capabilities": {
            "scene_generation_v1": {
                "operations": [
                    "add_spm_target_item",
                    "save_spm_target_registry_from_props",
                    "sync_spm_target_registry",
                ],
            },
            "target_registry_v1": {
                "operations": [
                    "load_target_registry",
                    "save_target_registry",
                    "remove_blend_target_from_spm",
                ],
            },
            "source_index_v1": {
                "operations": [
                    "current_blend_source_index",
                    "grouped_source_objects",
                ],
            },
            "speedtree_publish_v1": {
                "operations": ["export_or_update_speedtree_spm_path"],
            },
            "atomic_target_transaction_v1": {
                "operations": [
                    "configure_external_plan_target",
                    "execute_external_target_transaction",
                ],
                "native_capabilities": [
                    "atomic_exact_target_slice_v1",
                    "generator_adoption_reconciliation_v1",
                    "structured_transaction_conflict_v1",
                ],
            },
        },
        "operations": {
            "add_spm_target_item": (
                "atlas_leaf_mesh_builder.props:add_spm_target_item"
            ),
            "save_spm_target_registry_from_props": (
                "atlas_leaf_mesh_builder.props:save_spm_target_registry"
            ),
            "sync_spm_target_registry": (
                "atlas_leaf_mesh_builder.props:sync_spm_target_registry"
            ),
            "load_target_registry": (
                "atlas_leaf_mesh_builder.target_registry:load_target_registry"
            ),
            "save_target_registry": (
                "atlas_leaf_mesh_builder.target_registry:save_target_registry"
            ),
            "remove_blend_target_from_spm": (
                "atlas_leaf_mesh_builder.speedtree:remove_blend_target_from_spm"
            ),
            "current_blend_source_index": (
                "atlas_leaf_mesh_builder.source_index:current_blend_source_index"
            ),
            "grouped_source_objects": (
                "atlas_leaf_mesh_builder.speedtree:grouped_source_objects"
            ),
            "export_or_update_speedtree_spm_path": (
                "atlas_leaf_mesh_builder.speedtree:"
                "export_or_update_speedtree_spm_path"
            ),
            "configure_external_plan_target": (
                "atlas_leaf_mesh_builder.integration_api:"
                "configure_external_plan_target"
            ),
            "execute_external_target_transaction": (
                "atlas_leaf_mesh_builder.integration_api:"
                "execute_external_target_transaction"
            ),
        },
    },
    "send2ue": {
        "module": "send2ue",
        "native_api_module": None,
        "minimum_native_api_version": None,
        "source_environment": "SPEEDTREE_SEND2UE_ADDON_DIR",
        "capabilities": {
            "headless_export_v1": {
                "operations": [
                    "send_to_disk_path_mode",
                    "sync_unreal_mesh_folder_path",
                    "build_manifest_items",
                ],
            },
            "unreal_rpc_v1": {
                "operations": [
                    "is_unreal_connected",
                    "unreal_dependency_module",
                    "run_commands",
                    "set_rpc_env",
                ],
            },
            "fbx_export_v1": {
                "operations": ["fbx_export"],
            },
        },
        "operations": {
            "send_to_disk_path_mode": (
                "send2ue.constants:PathModes.SEND_TO_DISK.value"
            ),
            "sync_unreal_mesh_folder_path": (
                "send2ue.core.utilities:sync_unreal_mesh_folder_path"
            ),
            "build_manifest_items": "send2ue.core.ingest:build_manifest_items",
            "is_unreal_connected": (
                "send2ue.core.utilities:is_unreal_connected"
            ),
            "unreal_dependency_module": "send2ue.dependencies:unreal",
            "run_commands": "send2ue.dependencies.unreal:run_commands",
            "set_rpc_env": "send2ue.dependencies.unreal:set_rpc_env",
            "fbx_export": "send2ue.core.io.fbx_b4:export",
        },
    },
    "speedtree_cluster_normalizer": {
        "module": "speedtree_cluster_normalizer",
        "native_api_module": None,
        "minimum_native_api_version": None,
        "source_environment": "SPEEDTREE_CLUSTER_NORMALIZER_ADDON_DIR",
        "capabilities": {
            "cluster_normalization_v1": {"operations": []},
        },
        "operations": {},
    },
    "ue_unique_export_names_addon": {
        "module": "ue_unique_export_names_addon",
        "native_api_module": None,
        "minimum_native_api_version": None,
        "source_environment": "SPEEDTREE_UNIQUE_EXPORT_NAMES_ADDON_DIR",
        "capabilities": {
            "unreal_handoff_json_v1": {
                "operations": ["refresh_handoff_json"],
            },
        },
        "operations": {
            "refresh_handoff_json": (
                "ue_unique_export_names_addon.api:refresh_handoff_json"
            ),
        },
    },
}


class AddonContractError(ValueError):
    """A request or receipt violates the pure integration contract."""


def _canonical_json(payload):
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _sha256_payload(payload):
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def normalize_requirements(requirements):
    if not isinstance(requirements, dict) or not requirements:
        raise AddonContractError("add-on requirements must be a non-empty mapping")
    normalized = {}
    for addon_id, raw_capabilities in requirements.items():
        if addon_id not in ADDONS:
            raise AddonContractError(f"unknown Blender add-on: {addon_id}")
        if isinstance(raw_capabilities, str):
            raw_capabilities = [raw_capabilities]
        try:
            capabilities = sorted({str(value) for value in raw_capabilities})
        except TypeError as exc:
            raise AddonContractError(
                f"capabilities for {addon_id} must be iterable"
            ) from exc
        if not capabilities:
            raise AddonContractError(
                f"at least one capability is required for {addon_id}"
            )
        unknown = sorted(
            set(capabilities) - set(ADDONS[addon_id]["capabilities"])
        )
        if unknown:
            raise AddonContractError(
                f"unknown capabilities for {addon_id}: {', '.join(unknown)}"
            )
        normalized[addon_id] = capabilities
    return dict(sorted(normalized.items()))


def build_runtime_request(job, requirements, *, expected_sources=None):
    job = str(job or "").strip()
    if not job:
        raise AddonContractError("runtime request job cannot be empty")
    expected_sources = expected_sources or {}
    if not isinstance(expected_sources, dict):
        raise AddonContractError("expected_sources must be a mapping")
    normalized_sources = {
        addon_id: str(Path(path).expanduser().absolute())
        for addon_id, path in sorted(expected_sources.items())
        if str(path or "").strip()
    }
    unknown_sources = sorted(set(normalized_sources) - set(ADDONS))
    if unknown_sources:
        raise AddonContractError(
            "source expectations reference unknown add-ons: "
            + ", ".join(unknown_sources)
        )
    request = {
        "schema_version": CONTRACT_SCHEMA_VERSION,
        "gateway": {
            "name": GATEWAY_API_NAME,
            "minimum_version": GATEWAY_API_VERSION,
        },
        "job": job,
        "requirements": normalize_requirements(requirements),
        "expected_sources": normalized_sources,
    }
    request["request_sha256"] = _sha256_payload(request)
    return request


def source_expectations_from_environment(environ=None):
    environ = os.environ if environ is None else environ
    result = {}
    for addon_id, specification in ADDONS.items():
        variable = specification.get("source_environment")
        value = environ.get(variable, "") if variable else ""
        if str(value).strip():
            result[addon_id] = str(Path(value).expanduser().absolute())
    return result


def discover_installed_addon_source(addon_id, *, appdata=None):
    """Resolve the newest Blender junction/package for one known add-on."""
    if addon_id not in ADDONS:
        raise AddonContractError(f"unknown Blender add-on: {addon_id}")
    appdata = appdata or os.environ.get("APPDATA")
    if not appdata:
        return None
    blender_root = Path(appdata) / "Blender Foundation" / "Blender"

    def version_key(path):
        parts = []
        for value in path.name.split("."):
            try:
                parts.append((1, int(value)))
            except ValueError:
                parts.append((0, value.casefold()))
        return tuple(parts)

    versions = sorted(
        (path for path in blender_root.iterdir() if path.is_dir()),
        key=version_key,
        reverse=True,
    ) if blender_root.is_dir() else []
    module_name = ADDONS[addon_id]["module"]
    for version in versions:
        candidate = version / "scripts" / "addons" / module_name
        if candidate.is_dir():
            try:
                return candidate.resolve()
            except OSError:
                return candidate.absolute()
    return None


def operations_for_requirements(requirements, addon_id):
    normalized = normalize_requirements(requirements)
    if addon_id not in normalized:
        raise AddonContractError(f"runtime did not request add-on: {addon_id}")
    specification = ADDONS[addon_id]
    operations = set()
    for capability in normalized[addon_id]:
        operations.update(
            specification["capabilities"][capability].get("operations", [])
        )
    return frozenset(operations)


def native_capabilities_for(requirements, addon_id):
    normalized = normalize_requirements(requirements)
    capabilities = set()
    for capability in normalized.get(addon_id, []):
        capabilities.update(
            ADDONS[addon_id]["capabilities"][capability].get(
                "native_capabilities", []
            )
        )
    return tuple(sorted(capabilities))


def validate_runtime_receipt(request, receipt):
    if not isinstance(receipt, dict):
        raise AddonContractError("runtime receipt must be a mapping")
    if receipt.get("schema_version") != CONTRACT_SCHEMA_VERSION:
        raise AddonContractError("runtime receipt schema mismatch")
    if receipt.get("status") != "ready":
        raise AddonContractError(
            f"Blender add-on runtime is not ready: {receipt.get('status')}"
        )
    if receipt.get("job") != request.get("job"):
        raise AddonContractError("runtime receipt job mismatch")
    if receipt.get("request_sha256") != request.get("request_sha256"):
        raise AddonContractError("runtime receipt request identity mismatch")
    expected = set(request.get("requirements") or {})
    actual_rows = receipt.get("addons")
    if not isinstance(actual_rows, list):
        raise AddonContractError("runtime receipt add-ons must be a list")
    actual = {row.get("id") for row in actual_rows if isinstance(row, dict)}
    if actual != expected:
        raise AddonContractError(
            f"runtime receipt add-on set mismatch: expected={sorted(expected)}, "
            f"actual={sorted(str(value) for value in actual)}"
        )
    for row in actual_rows:
        if row.get("status") != "ready":
            raise AddonContractError(
                f"runtime add-on is not ready: {row.get('id')}"
            )
        if not row.get("module_file") or not row.get("capabilities"):
            raise AddonContractError(
                f"runtime add-on identity is incomplete: {row.get('id')}"
            )
    return copy.deepcopy(receipt)


def integration_manifest():
    return {
        "schema_version": CONTRACT_SCHEMA_VERSION,
        "gateway": {
            "name": GATEWAY_API_NAME,
            "version": GATEWAY_API_VERSION,
        },
        "ownership": copy.deepcopy(OWNERSHIP),
        "addons": copy.deepcopy(ADDONS),
    }


__all__ = [
    "ADDONS",
    "AddonContractError",
    "CONTRACT_SCHEMA_VERSION",
    "GATEWAY_API_NAME",
    "GATEWAY_API_VERSION",
    "OWNERSHIP",
    "build_runtime_request",
    "discover_installed_addon_source",
    "integration_manifest",
    "native_capabilities_for",
    "normalize_requirements",
    "operations_for_requirements",
    "source_expectations_from_environment",
    "validate_runtime_receipt",
]
