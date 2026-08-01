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
from speedtree_pipeline_contract import is_live_spm


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
    """Prove source/export inputs stayed fixed while allowing runtime code drift.

    Legacy Push receipts store exact size/mtime observations for the blend and
    dependency set.  Runtime-only code is the sole allowed difference.  New
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
    changed_runtime = []
    for key in sorted(parent_records):
        before = parent_records[key]
        after = current_records[key]
        if before == after:
            continue
        if key in rebindable:
            changed_runtime.append(str(after.get("path") or before.get("path")))
            continue
        raise PushUnrealRecoveryError(
            "Push source/export-time dependency changed; full Push required: "
            + str(after.get("path") or before.get("path") or key)
        )
    return changed_runtime


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


def load_parent_manifest(path):
    manifest_path = Path(path)
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PushUnrealRecoveryError(
            f"parent Push manifest could not be read: {manifest_path}: {exc}"
        ) from exc
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


def validate_unreal_only_recovery_evidence(
    parent_item,
    *,
    parent_source_record,
    current_source_record,
    current_source_fingerprint,
    rebindable_code_paths,
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
    validate_source_snapshots(
        parent_source_record.get("snapshot"),
        current_source_record.get("snapshot"),
        rebindable_code_paths=rebindable_code_paths,
    )
    validate_item_artifacts(parent_item)
    return True


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
):
    """Create one current-code item after validating immutable parent evidence."""
    parent_source_record = (
        parent_source_record if isinstance(parent_source_record, dict) else {}
    )
    current_source_record = (
        current_source_record if isinstance(current_source_record, dict) else {}
    )
    parent_fingerprint = str(parent_item.get("source_fingerprint") or "")
    validate_unreal_only_recovery_evidence(
        parent_item,
        parent_source_record=parent_source_record,
        current_source_record=current_source_record,
        current_source_fingerprint=current_source_fingerprint,
        rebindable_code_paths=(
            runtime_code_paths
            if rebindable_code_paths is None
            else rebindable_code_paths
        ),
    )

    current_code_files = [code_file_identity(path) for path in runtime_code_paths]
    old_code_revision = code_revision(parent_item.get("code_files") or [])
    new_code_revision = code_revision(current_code_files)
    recovered = copy.deepcopy(parent_item)
    recovered["source_fingerprint"] = current_source_fingerprint
    recovered["code_files"] = current_code_files

    assembly = recovered.get("cluster_assembly")
    regenerated_sidecars = []
    if assembly is not None:
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
        "depends_on_queue_ids": list(
            recovered.get("depends_on_queue_ids") or []
        ),
        "checkout_asset_paths": list(
            recovered.get("checkout_asset_paths") or []
        ),
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
    }
    contract = {
        key: copy.deepcopy(recovered.get(key))
        for key in PUSH_CONTRACT_KEYS
    }
    recovered["fingerprint"] = stable_fingerprint(
        {"contract": contract, "recovery": recovery_identity}
    )
    return recovered
