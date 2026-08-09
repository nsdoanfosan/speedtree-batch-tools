"""Immutable recovery contracts for retrying only failed Unreal ingest work.

The ordinary Push cache intentionally includes export-time and Unreal runtime
code.  This module provides the narrower, explicit recovery path: prove that
the Blender source contract and every exported artifact are unchanged, rebuild
the code-derived Unreal bindings with the currently loaded production source,
and issue a new manifest without mutating the parent evidence.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
from datetime import datetime
from pathlib import Path

from cluster_assembly_builder import (
    ClusterAssemblyBuildError,
    build_unreal_ingest_plan,
    validate_manifest_artifacts,
)
from sk_common import (
    PUSH_MANIFEST_SCHEMA_VERSION,
    file_content_fingerprint,
    file_content_snapshot,
)
from send2ue_manifest_contract import (
    normalize_manifest_handoff_sidecars,
    validate_material_handoff_wrapper,
)
from speedtree_pipeline_contract import (
    is_live_spm,
    shared_contract_api,
    validate_preflight_envelope as validate_speedtree_preflight_envelope,
)


RECOVERY_SCHEMA_VERSION = 1
PUSH_CONTRACT_KEYS = (
    "schema_version",
    "source_fingerprint",
    "blend",
    "unit_name",
    "unreal_folder",
    "mesh_path",
    "assets",
    "exported_files",
    "handoff_files",
    "wind_file",
    "wind_policy",
    "code_files",
    "cluster_assembly",
    "dependency_orchestrated",
    "material_asset_scope",
)


class PushUnrealRecoveryError(RuntimeError):
    """The cached export cannot safely be rebound to current Unreal code."""


def _canonical_json(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def stable_fingerprint(value):
    encoded = _canonical_json(value).encode("utf-8")
    return hashlib.blake2b(encoded, digest_size=16).hexdigest()


def _normalized_path(value):
    return os.path.normcase(os.path.abspath(os.path.normpath(str(value))))


def _identity_path(record, label):
    if not isinstance(record, dict) or not record.get("path"):
        raise PushUnrealRecoveryError(f"{label} fingerprint is missing")
    return Path(record["path"])


def validate_content_identity(record, label):
    """Hash an old BLAKE2b receipt and fail closed on any artifact drift."""
    path = _identity_path(record, label)
    try:
        stat = path.stat()
    except OSError as exc:
        raise PushUnrealRecoveryError(f"{label} is missing: {path}") from exc
    expected_size = record.get("size")
    expected_fingerprint = str(record.get("fingerprint") or "").casefold()
    if expected_size is None or not expected_fingerprint:
        raise PushUnrealRecoveryError(f"{label} fingerprint is incomplete")
    if stat.st_size != int(expected_size):
        raise PushUnrealRecoveryError(
            f"{label} size changed: {path} "
            f"(expected {expected_size}, actual {stat.st_size})"
        )
    actual_fingerprint = file_content_fingerprint(path)
    if actual_fingerprint.casefold() != expected_fingerprint:
        raise PushUnrealRecoveryError(f"{label} content changed: {path}")
    return {
        "path": str(path.resolve()),
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "fingerprint": actual_fingerprint,
    }


def validate_item_artifacts(item):
    """Verify every exported FBX/JSON and nested Assembly receipt exactly."""
    if not isinstance(item, dict) or not item.get("fingerprint"):
        raise PushUnrealRecoveryError("parent manifest item is incomplete")
    groups = (
        ("exported file", item.get("exported_files") or []),
        ("handoff file", item.get("handoff_files") or []),
        (
            "wind file",
            [item["wind_file"]] if item.get("wind_file") else [],
        ),
    )
    validated = []
    for group_label, records in groups:
        for index, record in enumerate(records):
            validated.append(
                validate_content_identity(record, f"{group_label} #{index + 1}")
            )
    assembly = item.get("cluster_assembly")
    if assembly is not None:
        manifest = assembly.get("manifest") if isinstance(assembly, dict) else None
        if not isinstance(manifest, dict):
            raise PushUnrealRecoveryError(
                "Cluster Assembly manifest is missing from the parent item"
            )
        try:
            validate_manifest_artifacts(manifest)
        except ClusterAssemblyBuildError as exc:
            raise PushUnrealRecoveryError(str(exc)) from exc
    return validated


def _validate_immutable_source_files(records):
    for index, record in enumerate(records or []):
        label = f"immutable source file #{index + 1}"
        path = _identity_path(record, label)
        if record.get("missing") is True:
            if path.exists():
                raise PushUnrealRecoveryError(
                    f"{label} appeared after the parent recovery: {path}"
                )
            continue
        validate_content_identity(record, label)


def _snapshot_records(snapshot):
    if not isinstance(snapshot, dict):
        raise PushUnrealRecoveryError("Push source snapshot is missing")
    blend = snapshot.get("blend")
    if not isinstance(blend, dict) or not blend.get("path"):
        raise PushUnrealRecoveryError("Push source blend snapshot is missing")
    records = {_normalized_path(blend["path"]): blend}
    for record in snapshot.get("dependencies") or []:
        if not isinstance(record, dict) or not record.get("path"):
            raise PushUnrealRecoveryError(
                "Push source dependency snapshot is incomplete"
            )
        key = _normalized_path(record["path"])
        if key in records:
            raise PushUnrealRecoveryError(
                f"duplicate Push source snapshot path: {record['path']}"
            )
        records[key] = record
    return records


def validate_source_snapshots(
    parent_snapshot,
    current_snapshot,
    *,
    rebindable_code_paths,
):
    """Prove source data stayed fixed while allowing explicit code rebinding.

    Legacy Push receipts store exact size/mtime observations for the blend and
    dependency set.  Only paths explicitly identified as executable code may
    differ after the immutable export artifacts have been validated.  New
    recovery manifests retain both snapshots and fresh artifact content hashes.
    """
    parent_records = _snapshot_records(parent_snapshot)
    current_records = _snapshot_records(current_snapshot)
    if set(parent_records) != set(current_records):
        removed = sorted(set(parent_records) - set(current_records))
        added = sorted(set(current_records) - set(parent_records))
        raise PushUnrealRecoveryError(
            "Push source dependency set changed; full Push required: "
            f"removed={removed} added={added}"
        )
    rebindable = {_normalized_path(path) for path in rebindable_code_paths}
    changed_code = []
    for key in sorted(parent_records):
        before = parent_records[key]
        after = current_records[key]
        if before == after:
            continue
        if key in rebindable:
            changed_code.append(str(after.get("path") or before.get("path")))
            continue
        raise PushUnrealRecoveryError(
            "Push source/export-time dependency changed; full Push required: "
            + str(after.get("path") or before.get("path") or key)
        )
    return changed_code


def code_file_identity(path):
    candidate = Path(path)
    if not candidate.is_file():
        raise PushUnrealRecoveryError(f"current Unreal code file is missing: {candidate}")
    snapshot = file_content_snapshot(candidate)
    return {
        "path": str(candidate.resolve()),
        "size": snapshot["size"],
        "mtime_ns": snapshot["mtime_ns"],
        "fingerprint": snapshot["fingerprint"],
    }


def immutable_source_identities(snapshot, *, rebindable_code_paths):
    """Persist content hashes for all source inputs that recovery may not rebind."""
    rebindable = {_normalized_path(path) for path in rebindable_code_paths}
    records = _snapshot_records(snapshot)
    identities = []
    for key in sorted(records):
        if key in rebindable:
            continue
        record = records[key]
        path = Path(record["path"])
        if record.get("missing") is True:
            identities.append({"path": str(path), "missing": True})
        else:
            identities.append(code_file_identity(path))
    return identities


def code_revision(records):
    identities = []
    for record in records or []:
        path = _identity_path(record, "code file")
        fingerprint = str(record.get("fingerprint") or "")
        if not fingerprint:
            raise PushUnrealRecoveryError(
                f"code file fingerprint is missing: {path}"
            )
        identities.append(
            {
                "path": _normalized_path(path),
                "size": record.get("size"),
                "fingerprint": fingerprint.casefold(),
            }
        )
    return stable_fingerprint(identities)


def load_parent_manifest(path, *, manifest_payload=None):
    manifest_path = Path(path)
    if manifest_payload is None:
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise PushUnrealRecoveryError(
                f"parent Push manifest could not be read: {manifest_path}: {exc}"
            ) from exc
    else:
        manifest = manifest_payload
    if not isinstance(manifest, dict):
        raise PushUnrealRecoveryError("parent Push manifest is not an object")
    if manifest.get("schema_version") != PUSH_MANIFEST_SCHEMA_VERSION:
        raise PushUnrealRecoveryError(
            "parent Push manifest schema is incompatible: "
            f"{manifest.get('schema_version')}"
        )
    items = manifest.get("items")
    if not isinstance(items, list) or not items:
        raise PushUnrealRecoveryError("parent Push manifest has no items")
    by_id = {}
    for item in items:
        queue_id = str((item or {}).get("queue_id") or "")
        if not queue_id or queue_id in by_id:
            raise PushUnrealRecoveryError(
                "parent Push manifest queue ids are missing or duplicated"
            )
        if item.get("schema_version") != PUSH_MANIFEST_SCHEMA_VERSION:
            raise PushUnrealRecoveryError(
                f"parent item schema is incompatible: {queue_id}"
            )
        if not is_live_spm(queue_id, require_file=False):
            raise PushUnrealRecoveryError(
                "parent Push manifest contains an ineligible SPM artifact: "
                + queue_id
            )
        by_id[queue_id] = item
    return manifest_path.resolve(), manifest, by_id


def dependency_closure(items_by_id, selected_queue_ids):
    selected = {str(value) for value in selected_queue_ids}
    missing = sorted(selected - set(items_by_id))
    if missing:
        raise PushUnrealRecoveryError(
            "selected item is absent from the parent manifest: " + ", ".join(missing)
        )
    required = set()

    def add(queue_id):
        if queue_id in required:
            return
        item = items_by_id.get(queue_id)
        if item is None:
            raise PushUnrealRecoveryError(
                f"required dependency is absent from the parent manifest: {queue_id}"
            )
        required.add(queue_id)
        for dependency in item.get("depends_on_queue_ids") or []:
            add(str(dependency))

    for queue_id in selected:
        add(queue_id)
    return required


def _full_mesh_template(item):
    mesh_path = str(item.get("mesh_path") or "")
    matches = [
        asset
        for asset in item.get("assets") or []
        if (asset.get("asset_data") or {}).get("_asset_type") == "SkeletalMesh"
        and (asset.get("asset_data") or {}).get("asset_path") == mesh_path
    ]
    if len(matches) != 1:
        raise PushUnrealRecoveryError(
            "Cluster Assembly recovery requires exactly one Full SK template"
        )
    return matches[0]


def _generated_sidecar_paths(ingest_plan):
    paths = []
    for asset in (ingest_plan or {}).get("assets") or []:
        value = (asset.get("asset_data") or {}).get(
            "_material_pipeline_json_path"
        )
        if value and _normalized_path(value) not in {
            _normalized_path(path) for path in paths
        }:
            paths.append(Path(value))
    return paths


def _refresh_handoff_files(records, regenerated_paths):
    regenerated = {_normalized_path(path) for path in regenerated_paths}
    result = []
    seen = set()
    for record in records or []:
        path = _identity_path(record, "handoff file")
        key = _normalized_path(path)
        result.append(code_file_identity(path) if key in regenerated else copy.deepcopy(record))
        seen.add(key)
    for path in regenerated_paths:
        key = _normalized_path(path)
        if key not in seen:
            result.append(code_file_identity(path))
            seen.add(key)
    return result


def _full_blender_rebuild_required(message):
    return PushUnrealRecoveryError(
        "non-Assembly material handoff cannot be safely rebound; "
        "full Blender rebuild required: "
        + str(message)
    )


def _read_json_identity(path, label):
    candidate = Path(path)
    try:
        identity = code_file_identity(candidate)
        payload = json.loads(candidate.read_bytes().decode("utf-8"))
        validate_content_identity(identity, label)
    except (OSError, UnicodeError, ValueError, PushUnrealRecoveryError) as exc:
        raise _full_blender_rebuild_required(
            f"{label} could not be validated: {candidate} ({exc})"
        ) from exc
    if not isinstance(payload, dict):
        raise _full_blender_rebuild_required(
            f"{label} is not an object: {candidate}"
        )
    return payload, identity


def _validate_authoritative_pipeline_contract(pipeline_contract, api):
    pipeline_contract = (
        pipeline_contract if isinstance(pipeline_contract, dict) else {}
    )
    source = pipeline_contract.get("source")
    descriptor = pipeline_contract.get("speedtree_handoff_contract")
    try:
        if not isinstance(source, dict) or not source:
            raise ValueError("no source identity")
        if not isinstance(descriptor, dict) or descriptor.get("source") != source:
            raise ValueError("descriptor is not source-bound")
        api.validate_sidecar_descriptor(
            descriptor,
            str(descriptor.get("mesh_name") or "").strip(),
        )
    except Exception as exc:
        raise _full_blender_rebuild_required(
            "authoritative strict material wrapper is not current: "
            + str(exc)
        ) from exc
    return pipeline_contract


def _load_authoritative_pipeline_contract(item, api):
    export_report_value = str(item.get("export_report_path") or "").strip()
    if not export_report_value:
        raise _full_blender_rebuild_required(
            "legacy sidecar has no authoritative export-report lineage"
        )
    export_report_path = Path(export_report_value)
    export_report, export_report_identity = _read_json_identity(
        export_report_path, "authoritative export report"
    )
    texture_contract = export_report.get("texture_contract") or {}
    if texture_contract.get("strict_speedtree_pipeline_contract") is not True:
        raise _full_blender_rebuild_required(
            "export report has no authoritative strict material wrapper"
        )
    wrapper_value = str(texture_contract.get("path") or "").strip()
    if not wrapper_value:
        raise _full_blender_rebuild_required(
            "export report strict material wrapper path is missing"
        )
    wrapper_path = Path(wrapper_value)
    wrapper, wrapper_identity = _read_json_identity(
        wrapper_path, "authoritative strict material wrapper"
    )
    queue_id = str(item.get("queue_id") or "").strip()
    if not queue_id:
        raise _full_blender_rebuild_required(
            "parent item has no canonical SPM identity"
        )
    try:
        validate_material_handoff_wrapper(wrapper, queue_id)
        pipeline_contract = validate_speedtree_preflight_envelope(
            wrapper.get("speedtree_pipeline_contract"),
            queue_id,
            require_ok=True,
        )
    except Exception as exc:
        raise _full_blender_rebuild_required(
            "authoritative strict material wrapper is stale or incompatible: "
            + str(exc)
        ) from exc
    _validate_authoritative_pipeline_contract(pipeline_contract, api)
    return {
        "pipeline_contract": pipeline_contract,
        "provenance": {
            "export_report": str(export_report_path.resolve()),
            "wrapper": str(wrapper_path.resolve()),
            "source_fingerprint": str(
                pipeline_contract.get("source_fingerprint") or ""
            ),
            "files": [
                export_report_identity,
                wrapper_identity,
            ],
        },
    }


def _validate_material_intent_capability(payload, api, asset_index):
    """Mirror the pure material-intent half of Unreal's current preflight."""
    materials = payload.get("materials", [])
    if not isinstance(materials, list):
        raise _full_blender_rebuild_required(
            f"asset #{asset_index + 1} materials is not a list"
        )
    for material_index, entry in enumerate(materials):
        if not isinstance(entry, dict):
            raise _full_blender_rebuild_required(
                f"asset #{asset_index + 1} materials[{material_index}] "
                "is not an object"
            )
        name = str(entry.get("name") or "")
        is_tree = str(
            entry.get("master_preset") or ""
        ).strip().casefold() == "tree"
        intent = entry.get("speedtree_intent")
        if is_tree and intent is None:
            raise _full_blender_rebuild_required(
                f"asset #{asset_index + 1} tree material {name!r} has no "
                "speedtree_intent"
            )
        if intent is None:
            continue
        if not is_tree:
            raise _full_blender_rebuild_required(
                f"asset #{asset_index + 1} material {name!r} has a "
                "speedtree_intent without master_preset 'tree'"
            )
        validate_intent = getattr(
            api, "validate_material_intent_for_name", None
        )
        build_intent = getattr(api, "build_material_intent", None)
        if not callable(validate_intent) or not callable(build_intent):
            raise _full_blender_rebuild_required(
                "current SpeedTree material-intent API is unavailable"
            )
        try:
            validated = validate_intent(intent, name)
            expected = build_intent(
                name,
                explicit_tree_part=str(entry.get("tree_part") or ""),
                explicit_tree_shading=str(
                    entry.get("tree_shading") or ""
                ),
                instance_profile=str(entry.get("instance_profile") or ""),
            )
            for key, expected_value in expected.items():
                if validated.get(key) != expected_value:
                    raise ValueError(
                        f"speedtree_intent {key} mismatch: "
                        f"{validated.get(key)!r} != {expected_value!r}"
                    )
            if expected.get("instance_profile"):
                entry_mode = str(
                    entry.get("material_instance_mode") or ""
                ).strip().casefold()
                if entry_mode != expected.get("material_instance_mode"):
                    raise ValueError(
                        "material_instance_mode mismatch: "
                        f"{entry_mode!r} != "
                        f"{expected.get('material_instance_mode')!r}"
                    )
        except Exception as exc:
            raise _full_blender_rebuild_required(
                f"asset #{asset_index + 1} material {name!r} has an "
                f"incompatible speedtree_intent: {exc}"
            ) from exc


def _sidecar_path_variants(path):
    spellings = {str(path), str(Path(path).resolve())}
    result = set()
    for spelling in spellings:
        result.update(
            {
                spelling,
                spelling.replace("\\", "/"),
                spelling.replace("/", "\\"),
                spelling.replace("\\", "\\\\"),
                spelling.replace("/", "\\\\"),
            }
        )
    return {value for value in result if value}


def _validate_sidecar_command_bindings(
    manifest_asset,
    sidecar_path,
    sidecar_sha256,
    expected_mesh_name,
    asset_index,
):
    binding_count = 0
    variants = _sidecar_path_variants(sidecar_path)
    for group_key in ("pre_import_commands", "post_import_commands"):
        groups = manifest_asset.get(group_key)
        if not isinstance(groups, list):
            continue
        for commands in groups:
            if not isinstance(commands, list):
                continue
            for line in commands:
                value = str(line)
                if "json_path" not in value or not any(
                    variant in value for variant in variants
                ):
                    continue
                binding_count += 1
                if (
                    "sidecar_sha256=" not in value
                    or sidecar_sha256 not in value.casefold()
                ):
                    raise _full_blender_rebuild_required(
                        f"asset #{asset_index + 1} material command has no "
                        "exact sidecar_sha256 binding"
                    )
                if (
                    "expected_mesh_name=" not in value
                    or expected_mesh_name.casefold() not in value.casefold()
                ):
                    raise _full_blender_rebuild_required(
                        f"asset #{asset_index + 1} material command has no "
                        "exact expected_mesh_name binding"
                    )
    if binding_count == 0:
        raise _full_blender_rebuild_required(
            f"asset #{asset_index + 1} material commands do not reference "
            "the recorded sidecar"
        )


def _classify_sidecar_descriptor(
    descriptor,
    expected_descriptor,
    expected_mesh_name,
    api,
    asset_index,
):
    if descriptor is None:
        return "missing", "descriptor is absent"
    if not isinstance(descriptor, dict):
        raise _full_blender_rebuild_required(
            f"asset #{asset_index + 1} sidecar descriptor is incompatible: "
            "not an object"
        )
    descriptor_name = str(descriptor.get("mesh_name") or "").strip()
    if descriptor_name.casefold() != expected_mesh_name.casefold():
        raise _full_blender_rebuild_required(
            f"asset #{asset_index + 1} sidecar descriptor mesh is "
            f"incompatible: {descriptor_name!r} != {expected_mesh_name!r}"
        )
    for field in ("kind", "asset_kind"):
        if descriptor.get(field) != expected_descriptor.get(field):
            raise _full_blender_rebuild_required(
                f"asset #{asset_index + 1} sidecar descriptor {field} is "
                "incompatible"
            )
    if "source" in descriptor and not isinstance(descriptor.get("source"), dict):
        raise _full_blender_rebuild_required(
            f"asset #{asset_index + 1} sidecar descriptor source is "
            "incompatible"
        )
    try:
        api.validate_sidecar_descriptor(descriptor, expected_mesh_name)
    except Exception as exc:
        validation_error = str(exc)
    else:
        validation_error = ""
    if descriptor == expected_descriptor:
        if validation_error:
            raise _full_blender_rebuild_required(
                f"asset #{asset_index + 1} sidecar descriptor is "
                f"incompatible: {validation_error}"
            )
        return "current", "authoritative current contract"
    if validation_error:
        return "legacy", "legacy contract: " + validation_error
    return "legacy", "current contract is not bound to authoritative source"


def _validate_non_assembly_handoff_sidecars(
    item,
    *,
    contract_api=None,
    authoritative_pipeline_contract=None,
):
    """Prove current capability or a source-bound, cache-local rebind path."""
    if item.get("cluster_assembly") is not None:
        return {
            "plans": [],
            "pipeline_contract": None,
            "authority_provenance": {},
            "contract_api": None,
        }
    sidecar_assets = []
    for index, asset in enumerate(item.get("assets") or []):
        data = asset.get("asset_data") if isinstance(asset, dict) else None
        data = data if isinstance(data, dict) else {}
        if data.get("_asset_type") not in {"SkeletalMesh", "StaticMesh"}:
            continue
        sidecar_value = data.get("_material_pipeline_json_path")
        if sidecar_value:
            sidecar_assets.append((index, asset, data, Path(sidecar_value)))
    if not sidecar_assets:
        return {
            "plans": [],
            "pipeline_contract": None,
            "authority_provenance": {},
            "contract_api": None,
        }

    api = contract_api or shared_contract_api()
    if authoritative_pipeline_contract is None:
        authority = _load_authoritative_pipeline_contract(item, api)
        pipeline_contract = authority["pipeline_contract"]
        authority_provenance = authority["provenance"]
    else:
        pipeline_contract = _validate_authoritative_pipeline_contract(
            authoritative_pipeline_contract,
            api,
        )
        authority_provenance = {}

    receipt_by_path = {}
    for record in item.get("handoff_files") or []:
        path = _identity_path(record, "handoff file")
        receipt_by_path[_normalized_path(path)] = record

    source = pipeline_contract.get("source")
    plans = []
    for index, asset, data, sidecar_path in sidecar_assets:
        receipt = receipt_by_path.get(_normalized_path(sidecar_path))
        if receipt is None:
            raise _full_blender_rebuild_required(
                f"asset #{index + 1} sidecar has no immutable receipt: "
                f"{sidecar_path}"
            )
        validate_content_identity(
            receipt,
            f"material sidecar for asset #{index + 1}",
        )
        try:
            sidecar_bytes = sidecar_path.read_bytes()
            actual_sha256 = hashlib.sha256(sidecar_bytes).hexdigest()
            payload = json.loads(sidecar_bytes.decode("utf-8"))
        except (OSError, UnicodeError, ValueError) as exc:
            raise _full_blender_rebuild_required(
                f"asset #{index + 1} sidecar is unreadable: "
                f"{sidecar_path} ({exc})"
            ) from exc
        if not isinstance(payload, dict):
            raise _full_blender_rebuild_required(
                f"asset #{index + 1} sidecar is not an object: {sidecar_path}"
            )
        expected_sha256 = str(
            data.get("_material_pipeline_json_sha256") or ""
        ).casefold()
        if not expected_sha256:
            raise _full_blender_rebuild_required(
                f"asset #{index + 1} sidecar SHA receipt is missing"
            )
        if expected_sha256 != actual_sha256.casefold():
            raise _full_blender_rebuild_required(
                f"asset #{index + 1} sidecar SHA does not match its manifest"
            )
        saved_name = str(payload.get("mesh_name") or "").strip()
        expected_name = str(
            data.get("_material_pipeline_expected_mesh_name")
            or str(data.get("asset_path") or "").split(".", 1)[0].rsplit("/", 1)[-1]
        ).strip()
        if (
            not saved_name
            or not expected_name
            or saved_name.casefold() != expected_name.casefold()
        ):
            raise _full_blender_rebuild_required(
                f"asset #{index + 1} sidecar mesh identity is ambiguous: "
                f"saved={saved_name!r} expected={expected_name!r}"
            )
        _validate_material_intent_capability(payload, api, index)
        _validate_sidecar_command_bindings(
            asset,
            sidecar_path,
            actual_sha256,
            expected_name,
            index,
        )
        try:
            expected_descriptor = api.build_sidecar_descriptor(
                expected_name,
                source=source,
            )
        except Exception as exc:
            raise _full_blender_rebuild_required(
                f"asset #{index + 1} current descriptor could not be built: "
                + str(exc)
            ) from exc
        descriptor_status, descriptor_reason = _classify_sidecar_descriptor(
            payload.get("speedtree_handoff_contract"),
            expected_descriptor,
            expected_name,
            api,
            index,
        )
        plans.append(
            {
                "asset_index": index,
                "sidecar": str(sidecar_path.resolve()),
                "sidecar_sha256": actual_sha256,
                "mesh_name": expected_name,
                "descriptor_status": descriptor_status,
                "descriptor_reason": descriptor_reason,
                "requires_derivation": descriptor_status != "current",
            }
        )
    return {
        "plans": plans,
        "pipeline_contract": pipeline_contract,
        "authority_provenance": authority_provenance,
        "contract_api": api,
    }


def _checkout_asset_paths(item):
    result = []
    for asset in item.get("assets") or []:
        path = str((asset.get("asset_data") or {}).get("asset_path") or "")
        if path and path not in result:
            result.append(path)
    plan = ((item.get("cluster_assembly") or {}).get("ingest_plan") or {})
    contract = plan.get("asset_contract") or {}
    if plan.get("status") == "ready":
        paths = [
            contract.get("base_skeletal_mesh"),
            contract.get("assembly"),
            *(contract.get("parts") or {}).values(),
            *[
                str(path) + "_Skeleton"
                for path in (contract.get("parts") or {}).values()
            ],
        ]
        for path in paths:
            if path and path not in result:
                result.append(path)
    return result


def _validate_unreal_only_recovery_evidence(
    parent_item,
    *,
    parent_source_record,
    current_source_record,
    current_source_fingerprint,
    rebindable_code_paths,
    sidecar_contract_api=None,
    authoritative_pipeline_contract=None,
):
    """Prove that one parent export is eligible for Unreal-only recovery.

    Selection and execution deliberately call the same validator.  A parent
    manifest label alone is never eligibility: the source snapshot, immutable
    source hashes, and every exported artifact must still match.
    """
    if not isinstance(parent_item, dict):
        raise PushUnrealRecoveryError("parent manifest item is incomplete")
    if parent_item.get("schema_version") != PUSH_MANIFEST_SCHEMA_VERSION:
        raise PushUnrealRecoveryError("parent item schema is incompatible")
    parent_source_record = (
        parent_source_record
        if isinstance(parent_source_record, dict)
        else {}
    )
    current_source_record = (
        current_source_record
        if isinstance(current_source_record, dict)
        else {}
    )
    parent_fingerprint = str(parent_item.get("source_fingerprint") or "")
    if parent_source_record.get("fingerprint") != parent_fingerprint:
        raise PushUnrealRecoveryError(
            "parent source fingerprint proof does not match the parent item"
        )
    if current_source_record.get("fingerprint") != current_source_fingerprint:
        raise PushUnrealRecoveryError(
            "current source fingerprint proof is inconsistent"
        )
    _validate_immutable_source_files(
        (parent_item.get("recovery") or {}).get("immutable_source_files")
    )
    validate_item_artifacts(parent_item)
    rebound_source_code_paths = validate_source_snapshots(
        parent_source_record.get("snapshot"),
        current_source_record.get("snapshot"),
        rebindable_code_paths=rebindable_code_paths,
    )
    sidecar_validation = _validate_non_assembly_handoff_sidecars(
        parent_item,
        contract_api=sidecar_contract_api,
        authoritative_pipeline_contract=authoritative_pipeline_contract,
    )
    return rebound_source_code_paths, sidecar_validation


def validate_unreal_only_recovery_evidence(
    parent_item,
    *,
    parent_source_record,
    current_source_record,
    current_source_fingerprint,
    rebindable_code_paths,
    sidecar_contract_api=None,
    authoritative_pipeline_contract=None,
):
    rebound_source_code_paths, _sidecar_validation = (
        _validate_unreal_only_recovery_evidence(
            parent_item,
            parent_source_record=parent_source_record,
            current_source_record=current_source_record,
            current_source_fingerprint=current_source_fingerprint,
            rebindable_code_paths=rebindable_code_paths,
            sidecar_contract_api=sidecar_contract_api,
            authoritative_pipeline_contract=authoritative_pipeline_contract,
        )
    )
    return rebound_source_code_paths


def recover_manifest_item(
    parent_item,
    *,
    parent_manifest_path,
    parent_report_path,
    parent_source_record,
    current_source_record,
    current_source_fingerprint,
    runtime_code_paths,
    rebindable_code_paths=None,
    report_path,
    selected,
    recovered_at=None,
    sidecar_contract_api=None,
    authoritative_pipeline_contract=None,
):
    """Create one current-code item after validating immutable parent evidence."""
    parent_source_record = (
        parent_source_record if isinstance(parent_source_record, dict) else {}
    )
    current_source_record = (
        current_source_record if isinstance(current_source_record, dict) else {}
    )
    parent_fingerprint = str(parent_item.get("source_fingerprint") or "")
    rebound_source_code_paths, sidecar_validation = (
        _validate_unreal_only_recovery_evidence(
            parent_item,
            parent_source_record=parent_source_record,
            current_source_record=current_source_record,
            current_source_fingerprint=current_source_fingerprint,
            rebindable_code_paths=(
                runtime_code_paths
                if rebindable_code_paths is None
                else rebindable_code_paths
            ),
            sidecar_contract_api=sidecar_contract_api,
            authoritative_pipeline_contract=authoritative_pipeline_contract,
        )
    )

    derivation_code_paths = [Path(__file__).resolve()]
    if sidecar_validation["plans"]:
        shared_contract_source = getattr(
            shared_contract_api,
            "__wrapped__",
            shared_contract_api,
        )
        derivation_code_paths.extend(
            [
                Path(
                    normalize_manifest_handoff_sidecars.__code__.co_filename
                ).resolve(),
                Path(shared_contract_source.__code__.co_filename).resolve(),
            ]
        )
        api = sidecar_validation["contract_api"]
        api_path = getattr(api, "__file__", None)
        if api_path:
            derivation_code_paths.append(Path(api_path).resolve())
        contract_path = getattr(api, "CONTRACT_PATH", None)
        if contract_path and Path(contract_path).is_file():
            derivation_code_paths.append(Path(contract_path).resolve())

    current_code_files = []
    current_code_keys = set()

    def add_current_code_file(path):
        key = _normalized_path(path)
        if key in current_code_keys:
            return
        current_code_keys.add(key)
        current_code_files.append(code_file_identity(path))

    for code_path in [*runtime_code_paths, *derivation_code_paths]:
        add_current_code_file(code_path)

    old_code_revision = code_revision(parent_item.get("code_files") or [])
    new_code_revision = code_revision(current_code_files)
    recovered = copy.deepcopy(parent_item)
    recovered["source_fingerprint"] = current_source_fingerprint
    recovered["code_files"] = current_code_files

    assembly = recovered.get("cluster_assembly")
    regenerated_sidecars = []
    sidecar_derivations = []
    if assembly is None:
        sidecar_plans = sidecar_validation["plans"]
        if any(plan["requires_derivation"] for plan in sidecar_plans):
            recovery_root = (
                Path(report_path).resolve().parent
                / (Path(report_path).stem + "_handoff")
            )
            try:
                normalization = normalize_manifest_handoff_sidecars(
                    recovered.get("assets") or [],
                    recovery_root,
                    sidecar_descriptor_builder=(
                        sidecar_validation[
                            "contract_api"
                        ].build_sidecar_descriptor
                    ),
                    authoritative_pipeline_contract=sidecar_validation[
                        "pipeline_contract"
                    ],
                    normalize_mesh_file=False,
                )
            except RuntimeError as exc:
                raise _full_blender_rebuild_required(exc) from exc
            regenerated_sidecars = [
                Path(row["normalized_sidecar"])
                for row in normalization
                if row.get("identity_changed")
            ]
            recovered["handoff_files"] = _refresh_handoff_files(
                recovered.get("handoff_files"),
                regenerated_sidecars,
            )
            plan_by_source = {
                _normalized_path(plan["sidecar"]): plan
                for plan in sidecar_plans
            }
            for row in normalization:
                if not row.get("identity_changed"):
                    continue
                plan = plan_by_source.get(
                    _normalized_path(row["source_sidecar"]),
                    {},
                )
                normalized_path = Path(row["normalized_sidecar"])
                sidecar_derivations.append(
                    {
                        "asset_index": plan.get("asset_index"),
                        "descriptor_status_before": plan.get(
                            "descriptor_status"
                        ),
                        "source_sidecar": row["source_sidecar"],
                        "source_sidecar_sha256": plan.get(
                            "sidecar_sha256"
                        ),
                        "normalized_sidecar": str(
                            normalized_path.resolve()
                        ),
                        "normalized_sidecar_sha256": hashlib.sha256(
                            normalized_path.read_bytes()
                        ).hexdigest(),
                        "expected_mesh_name": plan.get("mesh_name"),
                    }
                )
            post_validation = _validate_non_assembly_handoff_sidecars(
                recovered,
                contract_api=sidecar_validation["contract_api"],
                authoritative_pipeline_contract=sidecar_validation[
                    "pipeline_contract"
                ],
            )
            if any(
                plan["descriptor_status"] != "current"
                for plan in post_validation["plans"]
            ):
                raise _full_blender_rebuild_required(
                    "cache-local sidecar postcondition is not current"
                )
    else:
        try:
            ingest_plan = build_unreal_ingest_plan(
                assembly.get("manifest"),
                _full_mesh_template(recovered),
                recovered.get("mesh_path"),
                recovered.get("unreal_folder"),
            )
        except ClusterAssemblyBuildError as exc:
            raise PushUnrealRecoveryError(str(exc)) from exc
        assembly["ingest_plan"] = ingest_plan
        regenerated_sidecars = _generated_sidecar_paths(ingest_plan)
        recovered["handoff_files"] = _refresh_handoff_files(
            recovered.get("handoff_files"), regenerated_sidecars
        )

    recovered["checkout_asset_paths"] = _checkout_asset_paths(recovered)
    recovered["report_path"] = str(Path(report_path).resolve())
    recovered["verify_existing_assets"] = not bool(selected)
    recovery_identity = {
        "schema_version": RECOVERY_SCHEMA_VERSION,
        "parent_manifest": str(Path(parent_manifest_path).resolve()),
        "parent_report": (
            str(Path(parent_report_path).resolve()) if parent_report_path else ""
        ),
        "parent_item_fingerprint": str(parent_item.get("fingerprint") or ""),
        "parent_source_fingerprint": parent_fingerprint,
        "current_source_fingerprint": current_source_fingerprint,
        "old_code_revision": old_code_revision,
        "new_code_revision": new_code_revision,
        "old_production_source_revision": old_code_revision,
        "new_production_source_revision": new_code_revision,
        "selected_for_retry": bool(selected),
        "rebound_source_code_paths": list(rebound_source_code_paths or []),
        "depends_on_queue_ids": list(
            recovered.get("depends_on_queue_ids") or []
        ),
        "checkout_asset_paths": list(
            recovered.get("checkout_asset_paths") or []
        ),
        "sidecar_contracts": copy.deepcopy(sidecar_validation["plans"]),
        "sidecar_authority": copy.deepcopy(
            sidecar_validation["authority_provenance"]
        ),
        "sidecar_derivations": copy.deepcopy(sidecar_derivations),
    }
    recovered["recovery"] = {
        **recovery_identity,
        "recovered_at": recovered_at
        or datetime.now().isoformat(timespec="seconds"),
        "current_source_snapshot": copy.deepcopy(
            current_source_record.get("snapshot")
        ),
        "immutable_source_files": immutable_source_identities(
            current_source_record.get("snapshot"),
            rebindable_code_paths=(
                runtime_code_paths
                if rebindable_code_paths is None
                else rebindable_code_paths
            ),
        ),
        "regenerated_sidecars": [
            str(path.resolve()) for path in regenerated_sidecars
        ],
        "derivation_code_files": [
            copy.deepcopy(record)
            for record in current_code_files
            if _normalized_path(record["path"]) in {
                _normalized_path(path) for path in derivation_code_paths
            }
        ],
    }
    contract = {
        key: copy.deepcopy(recovered.get(key))
        for key in PUSH_CONTRACT_KEYS
    }
    recovered["fingerprint"] = stable_fingerprint(
        {"contract": contract, "recovery": recovery_identity}
    )
    return recovered
