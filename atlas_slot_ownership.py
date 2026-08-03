"""Live-SPM-authoritative reconciliation for Atlas Generator slot ownership.

The Atlas manifest resolver intentionally fails closed when two producers claim
the same Generator slot differently.  That is the correct default, but an Atlas
producer handoff can leave an older receipt claiming a slot that a later
producer now owns in the saved SPM.  This module repairs only that deterministic
case.

Slot identity is always the opaque pair ``(generator_guid, slot_prefix)``.
There is no Type-number parser, upper bound, contiguous-range assumption, or
provider-count limit here.  The current Material/Mesh pair in the sealed SPM
selects a provider only when that pair belongs to exactly one provider's
material group.  Ambiguity remains a hard block.
"""
from __future__ import annotations

import copy
import gzip
import hashlib
import json
import os
import tempfile
from pathlib import Path

from atlas_manifest_resolver import (
    AtlasManifestResolutionError,
    diagnose_manifest_generator_candidates,
    generator_binding_ownership_fingerprint,
    generator_slot_creation_provenance_fingerprint,
    normalize_generator_target_material_id,
    normalize_generator_target_mesh_id,
    normalized_manifest_path,
    resolve_atlas_manifests,
    resolution_evidence,
)


PLAN_CONTRACT = "atlas_slot_ownership_reconciliation_plan"
PLAN_SCHEMA_VERSION = 1
FLEET_PLAN_CONTRACT = "atlas_slot_ownership_fleet_reconciliation_plan"
FLEET_PLAN_SCHEMA_VERSION = 1
OWNERSHIP_CONTRACT = "atlas_generator_current_binding_ownership"
OWNERSHIP_VERSION = 1
PROVENANCE_CONTRACT = "atlas_generator_slot_creation_provenance"
PROVENANCE_VERSION = 1

_KIND_PRECEDENCE = {
    "exact_per_target": 0,
    "exact_target_scope": 1,
    "exact_global_target": 2,
}

class AtlasSlotOwnershipError(RuntimeError):
    """A reconciliation plan is invalid, stale, ambiguous, or failed to apply."""

    def __init__(self, reason_code, message, *, evidence=None):
        super().__init__(message)
        self.reason_code = str(reason_code)
        self.evidence = copy.deepcopy(evidence or {})


def _canonical_json_bytes(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _pretty_json_bytes(value):
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        )
        + "\n"
    ).encode("utf-8")


def _sha256_bytes(value):
    return hashlib.sha256(value).hexdigest()


def _read_spm_snapshot(path):
    """Read one stable SPM without depending on sync module import shape.

    The sync engine is loaded both as a top-level module by its GUI/tests and
    as a namespace-package child by command-line entry points.  Ownership
    planning must remain usable in either host, so this small reader keeps the
    byte-stability and gzip semantics local.
    """

    path = Path(path)
    raw = None
    for _attempt in range(2):
        before = path.stat()
        candidate = path.read_bytes()
        after = path.stat()
        if (before.st_size, before.st_mtime_ns) == (
            after.st_size,
            after.st_mtime_ns,
        ):
            raw = candidate
            break
    if raw is None:
        raise AtlasSlotOwnershipError(
            "spm_changed_during_read",
            f"Target SPM changed while it was being read: {path}",
        )
    raw_sha256 = _sha256_bytes(raw)
    encoded = gzip.decompress(raw) if raw.startswith(b"\x1f\x8b") else raw
    try:
        return encoded.decode("utf-8"), raw_sha256
    except UnicodeDecodeError as exc:
        raise AtlasSlotOwnershipError(
            "spm_not_utf8",
            f"Target SPM is not UTF-8 XML: {path}",
        ) from exc


def _sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _integer(value):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _slot_key(row):
    return (
        str(row.get("generator_guid") or "").strip(),
        str(row.get("slot_prefix") or "").strip(),
    )


def _ownership_projection(rows):
    projected = []
    seen = set()
    for ordinal, row in enumerate(rows or []):
        if not isinstance(row, dict):
            raise AtlasSlotOwnershipError(
                "ownership_binding_not_object",
                f"Ownership binding #{ordinal + 1} is not an object.",
            )
        guid, slot = _slot_key(row)
        material_id = normalize_generator_target_material_id(
            row.get("target_material_id")
        )
        mesh_id = normalize_generator_target_mesh_id(
            row.get("target_mesh_id")
        )
        if (
            not guid
            or not slot
            or material_id is None
            or material_id <= 0
            or mesh_id is None
            or (mesh_id <= 0 and mesh_id != -10)
        ):
            raise AtlasSlotOwnershipError(
                "ownership_binding_incomplete",
                "Ownership bindings require exact Generator GUID, slot prefix, "
                "positive Material ID, and either a positive Mesh ID or -10.",
                evidence={"binding": copy.deepcopy(row)},
            )
        key = (guid, slot)
        if key in seen:
            raise AtlasSlotOwnershipError(
                "duplicate_ownership_binding",
                f"Duplicate current ownership binding: {guid} / {slot}",
            )
        seen.add(key)
        projected.append({
            "generator_guid": guid,
            "slot_prefix": slot,
            "target_material_id": material_id,
            "target_mesh_id": mesh_id,
        })
    return sorted(
        projected,
        key=lambda row: (row["generator_guid"], row["slot_prefix"]),
    )


def ownership_fingerprint(rows):
    """Return the shared v1 fingerprint for current binding ownership."""
    _ownership_projection(rows)
    return generator_binding_ownership_fingerprint(rows)


def _normalize_provenance_row(row):
    if not isinstance(row, dict):
        raise AtlasSlotOwnershipError(
            "provenance_slot_not_object",
            "Generator slot creation provenance rows must be objects.",
        )
    normalized = copy.deepcopy(row)
    guid, slot = _slot_key(normalized)
    if not guid or not slot:
        raise AtlasSlotOwnershipError(
            "provenance_slot_identity_missing",
            "Generator slot creation provenance requires GUID and slot prefix.",
            evidence={"slot": copy.deepcopy(row)},
        )
    normalized["generator_guid"] = guid
    normalized["slot_prefix"] = slot
    names = normalized.get("created_property_names")
    if names is not None:
        if not isinstance(names, list) or any(
            not isinstance(value, str) for value in names
        ):
            raise AtlasSlotOwnershipError(
                "provenance_created_properties_invalid",
                "created_property_names must be a list of strings.",
                evidence={"slot": copy.deepcopy(row)},
            )
        normalized["created_property_names"] = list(names)
    return normalized


def _normalized_provenance_rows(rows):
    normalized = []
    seen = {}
    for row in rows or []:
        current = _normalize_provenance_row(row)
        key = _slot_key(current)
        previous = seen.get(key)
        if previous is not None and previous != current:
            raise AtlasSlotOwnershipError(
                "conflicting_creation_provenance",
                f"Creation provenance disagrees for {key[0]} / {key[1]}.",
                evidence={"first": previous, "second": current},
            )
        if previous is None:
            seen[key] = current
            normalized.append(current)
    return sorted(
        normalized,
        key=lambda row: (
            row["generator_guid"],
            row["slot_prefix"],
            _canonical_json_bytes(row),
        ),
    )


def provenance_fingerprint(rows):
    """Return the shared v1 fingerprint for immutable creator provenance."""
    normalized = _normalized_provenance_rows(rows)
    return generator_slot_creation_provenance_fingerprint(normalized)


def _ownership_block(rows, *, spm_sha256):
    detailed = sorted(
        [copy.deepcopy(row) for row in rows],
        key=lambda row: _slot_key(row),
    )
    projection = _ownership_projection(detailed)
    return {
        "contract": OWNERSHIP_CONTRACT,
        "version": OWNERSHIP_VERSION,
        "basis": "live_spm_material_mesh_projection",
        "spm_sha256": spm_sha256,
        "binding_count": len(projection),
        "fingerprint": generator_binding_ownership_fingerprint(detailed),
        "bindings": detailed,
    }


def _provenance_block(rows):
    normalized = _normalized_provenance_rows(rows)
    return {
        "contract": PROVENANCE_CONTRACT,
        "version": PROVENANCE_VERSION,
        "slot_count": len(normalized),
        "fingerprint": generator_slot_creation_provenance_fingerprint(
            normalized
        ),
        "slots": normalized,
    }


def _validate_existing_ownership(payload, *, path):
    if "generator_binding_ownership" not in payload:
        return None
    block = payload.get("generator_binding_ownership")
    if not isinstance(block, dict):
        raise AtlasSlotOwnershipError(
            "ownership_contract_not_object",
            f"Current ownership contract is not an object: {path}",
        )
    if (
        block.get("contract") != OWNERSHIP_CONTRACT
        or _integer(block.get("version")) != OWNERSHIP_VERSION
    ):
        raise AtlasSlotOwnershipError(
            "ownership_contract_unsupported",
            f"Unsupported current ownership contract: {path}",
        )
    rows = block.get("bindings")
    if not isinstance(rows, list):
        raise AtlasSlotOwnershipError(
            "ownership_bindings_not_list",
            f"Current ownership bindings are not a list: {path}",
        )
    projection = _ownership_projection(rows)
    expected = generator_binding_ownership_fingerprint(rows)
    if _integer(block.get("binding_count")) != len(projection):
        raise AtlasSlotOwnershipError(
            "ownership_binding_count_mismatch",
            f"Current ownership binding_count is stale: {path}",
        )
    if str(block.get("fingerprint") or "").strip().casefold() != expected:
        raise AtlasSlotOwnershipError(
            "ownership_fingerprint_mismatch",
            f"Current ownership fingerprint is stale: {path}",
        )
    return [copy.deepcopy(row) for row in rows]


def _validate_existing_provenance(payload, *, path):
    if "generator_slot_creation_provenance" not in payload:
        return None
    block = payload.get("generator_slot_creation_provenance")
    if not isinstance(block, dict):
        raise AtlasSlotOwnershipError(
            "provenance_contract_not_object",
            f"Creation provenance contract is not an object: {path}",
        )
    if (
        block.get("contract") != PROVENANCE_CONTRACT
        or _integer(block.get("version")) != PROVENANCE_VERSION
    ):
        raise AtlasSlotOwnershipError(
            "provenance_contract_unsupported",
            f"Unsupported creation provenance contract: {path}",
        )
    rows = block.get("slots")
    if not isinstance(rows, list):
        raise AtlasSlotOwnershipError(
            "provenance_slots_not_list",
            f"Creation provenance slots are not a list: {path}",
        )
    normalized = _normalized_provenance_rows(rows)
    expected = generator_slot_creation_provenance_fingerprint(normalized)
    if _integer(block.get("slot_count")) != len(normalized):
        raise AtlasSlotOwnershipError(
            "provenance_slot_count_mismatch",
            f"Creation provenance slot_count is stale: {path}",
        )
    if str(block.get("fingerprint") or "").strip().casefold() != expected:
        raise AtlasSlotOwnershipError(
            "provenance_fingerprint_mismatch",
            f"Creation provenance fingerprint is stale: {path}",
        )
    return normalized


def _legacy_creator_row(binding):
    if not isinstance(binding, dict) or binding.get("created_slot") is not True:
        return None
    row = copy.deepcopy(binding)
    row.pop("state", None)
    row.pop("created_slot", None)
    for source, target in (
        ("target_material_id", "initial_target_material_id"),
        ("target_material_name", "initial_target_material_name"),
        ("target_mesh_id", "initial_target_mesh_id"),
    ):
        if source in row:
            row[target] = row.pop(source)
    return _normalize_provenance_row(row)


def _candidate_specs(target):
    target = Path(target)
    rows = []
    exact = target.parent / ".atlas_leaf_speedtree_targets" / f"{target.stem}.json"
    if exact.is_file():
        rows.append((exact, "exact_per_target"))
    scope_dir = target.parent / ".atlas_leaf_speedtree_scopes"
    if scope_dir.is_dir():
        rows.extend(
            (path, "exact_target_scope")
            for path in sorted(scope_dir.glob(f"*__{target.stem}.json"))
            if path.is_file()
        )
    global_path = target.parent / "speedtree_import_manifest.json"
    if global_path.is_file():
        rows.append((global_path, "exact_global_target"))
    return rows


def _manifest_schema_version(payload):
    for field in (
        "atlas_manifest_schema_version",
        "manifest_schema_version",
        "schema_version",
    ):
        if field not in payload:
            continue
        return _integer(payload.get(field))
    return 1


def _source_identity(payload, path, target):
    blend = str(payload.get("blend_file") or "").strip()
    collection = str(payload.get("source_collection") or "").strip()
    scope = str(payload.get("export_scope_id") or "").strip()
    return {
        "blend_file": (
            normalized_manifest_path(blend, relative_to=Path(target).parent)
            if blend
            else ""
        ),
        "source_collection": collection.casefold(),
        "export_scope_id": scope.casefold(),
        "declared_blend_file": blend,
        "declared_source_collection": collection,
        "declared_export_scope_id": scope,
        "target_spm": str(Path(target).resolve(strict=False)),
    }


def _provider_key(identity):
    return (
        identity.get("blend_file") or "",
        identity.get("source_collection") or "",
        identity.get("export_scope_id") or "",
    )


def _material_groups(payload):
    groups = payload.get("speedtree_material_groups")
    if not isinstance(groups, list) or not groups:
        groups = payload.get("material_groups")
    return [row for row in groups or [] if isinstance(row, dict)]


def _group_mesh_ids(group):
    values = group.get("mesh_ids") or []
    if not values:
        adoption = group.get("source_material_adoption") or {}
        values = adoption.get("final_material_mesh_ids") or []
    if not values and isinstance(group.get("meshes"), list):
        values = [
            row.get("mesh_id") or row.get("speedtree_mesh_id")
            if isinstance(row, dict)
            else row
            for row in group["meshes"]
        ]
    return sorted({
        value
        for value in (_integer(item) for item in values)
        if value is not None and value > 0
    })


def _material_projection(payload):
    rows = []
    seen = {}
    for group in _material_groups(payload):
        material_id = _integer(group.get("material_id"))
        if material_id is None or material_id <= 0:
            continue
        row = {
            "material": str(group.get("material") or "").strip(),
            "material_id": material_id,
            "mesh_ids": _group_mesh_ids(group),
        }
        previous = seen.get(material_id)
        if previous is not None and previous != row:
            raise AtlasSlotOwnershipError(
                "provider_material_id_duplicated",
                f"Provider declares Material ID {material_id} more than once.",
                evidence={"first": previous, "second": row},
            )
        if previous is None:
            seen[material_id] = row
            rows.append(row)
    return sorted(rows, key=lambda row: (row["material_id"], row["material"]))


def _read_candidate_records(target):
    target_key = normalized_manifest_path(target)
    records = []
    ignored = []
    for path, kind in _candidate_specs(target):
        try:
            raw = path.read_bytes()
            payload = json.loads(raw.decode("utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise AtlasSlotOwnershipError(
                "candidate_manifest_unreadable",
                f"Atlas manifest is unreadable: {path}: {exc}",
            ) from exc
        if not isinstance(payload, dict):
            raise AtlasSlotOwnershipError(
                "candidate_manifest_not_object",
                f"Atlas manifest root is not an object: {path}",
            )
        declared = str(payload.get("spm") or "").strip()
        if not declared or normalized_manifest_path(
            declared, relative_to=target.parent
        ) != target_key:
            ignored.append({
                "path": str(path.resolve(strict=False)),
                "kind": kind,
                "reason": "different_target_spm",
                "declared_spm": declared,
            })
            continue
        lifecycle = payload.get("atlas_scope_lifecycle") or {}
        if lifecycle.get("state") == "retired":
            ignored.append({
                "path": str(path.resolve(strict=False)),
                "kind": kind,
                "reason": "retired_scope_record",
            })
            continue
        if _manifest_schema_version(payload) != 1:
            raise AtlasSlotOwnershipError(
                "candidate_schema_unsupported",
                f"Unsupported Atlas manifest schema: {path}",
            )
        _validate_existing_ownership(payload, path=path)
        _validate_existing_provenance(payload, path=path)
        identity = _source_identity(payload, path, target)
        if not all(_provider_key(identity)):
            raise AtlasSlotOwnershipError(
                "provider_identity_incomplete",
                f"Atlas provider identity is incomplete: {path}",
                evidence={"source_identity": identity},
            )
        records.append({
            "path": str(path.resolve(strict=False)),
            "kind": kind,
            "precedence": _KIND_PRECEDENCE[kind],
            "payload": payload,
            "before_sha256": _sha256_bytes(raw),
            "source_identity": identity,
            "provider_key": _provider_key(identity),
            "material_projection": _material_projection(payload),
        })
    return records, ignored


def _stable_live_snapshot(target, supplied=None):
    from pcg_st9_texture_batch.pcg_texture_audit import (
        generator_delivery_snapshot_from_spm_text,
    )

    text, raw_sha256 = _read_spm_snapshot(Path(target))
    text_sha256 = _sha256_bytes(text.encode("utf-8"))
    if supplied is not None:
        if not isinstance(supplied, dict):
            raise AtlasSlotOwnershipError(
                "live_snapshot_not_object",
                "The supplied live SPM snapshot must be an object.",
            )
        supplied_path = str(supplied.get("spm") or "").strip()
        if supplied_path and normalized_manifest_path(supplied_path) != normalized_manifest_path(target):
            raise AtlasSlotOwnershipError(
                "live_snapshot_foreign_target",
                "The supplied live SPM snapshot belongs to another target.",
            )
        supplied_text_hash = str(
            supplied.get("spm_text_sha256") or ""
        ).strip().casefold()
        if not supplied_text_hash or supplied_text_hash != text_sha256:
            raise AtlasSlotOwnershipError(
                "live_snapshot_stale",
                "The supplied live SPM snapshot does not match current bytes.",
            )
        snapshot = copy.deepcopy(supplied)
    else:
        snapshot = generator_delivery_snapshot_from_spm_text(text, target)
    bindings = snapshot.get("leaf_generator_bindings")
    if not isinstance(bindings, list):
        raise AtlasSlotOwnershipError(
            "live_bindings_missing",
            "The live SPM snapshot has no semantic Generator bindings.",
        )
    return {
        **snapshot,
        "spm": str(Path(target).resolve(strict=False)),
        "spm_sha256": raw_sha256,
        "spm_text_sha256": text_sha256,
        "leaf_generator_bindings": [copy.deepcopy(row) for row in bindings],
    }


def _provider_state(records):
    providers = {}
    for record in records:
        key = record["provider_key"]
        provider = providers.setdefault(key, {
            "provider_key": key,
            "source_identity": record["source_identity"],
            "records": [],
            "material_projection": record["material_projection"],
            "binding_templates": {},
            "creator_rows": {},
        })
        if provider["material_projection"] != record["material_projection"]:
            raise AtlasSlotOwnershipError(
                "provider_material_projection_disagreement",
                "Operational mirrors for one provider disagree on Material/Mesh ownership.",
                evidence={
                    "provider": provider["source_identity"],
                    "first": provider["material_projection"],
                    "second": record["material_projection"],
                    "path": record["path"],
                },
            )
        provider["records"].append(record)

        connection = record["payload"].get("generator_connection") or {}
        template_rows = []
        for field in ("bindings", "authored_bindings"):
            values = connection.get(field) or []
            if not isinstance(values, list):
                raise AtlasSlotOwnershipError(
                    "generator_bindings_not_list",
                    f"generator_connection.{field} is not a list: {record['path']}",
                )
            template_rows.extend(row for row in values if isinstance(row, dict))
        for row in template_rows:
            key_value = _slot_key(row)
            if not all(key_value):
                continue
            provider["binding_templates"].setdefault(key_value, []).append({
                "row": copy.deepcopy(row),
                "precedence": record["precedence"],
                "path": record["path"],
            })

        explicit = _validate_existing_provenance(
            record["payload"], path=record["path"]
        )
        creator_rows = explicit
        if creator_rows is None:
            creator_rows = [
                creator
                for creator in (
                    _legacy_creator_row(row) for row in template_rows
                )
                if creator is not None
            ]
        for row in creator_rows:
            key_value = _slot_key(row)
            previous = provider["creator_rows"].get(key_value)
            if previous is not None and previous != row:
                raise AtlasSlotOwnershipError(
                    "provider_creation_provenance_disagreement",
                    "Operational mirrors disagree on immutable slot creator provenance.",
                    evidence={"first": previous, "second": row},
                )
            provider["creator_rows"][key_value] = copy.deepcopy(row)
    for provider in providers.values():
        provider["records"].sort(
            key=lambda row: (row["precedence"], row["path"].casefold())
        )
    return providers


def _live_owner_assignments(snapshot, providers):
    exact_pair_owners = {}
    material_owners = {}
    managed_material_ids = set()
    managed_mesh_ids = set()
    for key, provider in providers.items():
        for group in provider["material_projection"]:
            material_id = group["material_id"]
            managed_material_ids.add(material_id)
            material_owners.setdefault(material_id, set()).add(key)
            for mesh_id in group["mesh_ids"]:
                managed_mesh_ids.add(mesh_id)
                exact_pair_owners.setdefault(
                    (material_id, mesh_id), set()
                ).add(key)

    assignments = {key: [] for key in providers}
    takeovers = []
    blocking = []
    seen_live = {}
    for live in snapshot["leaf_generator_bindings"]:
        material_id = _integer(live.get("material_id"))
        mesh_id = _integer(live.get("mesh_id"))
        if material_id is None or material_id <= 0 or mesh_id is None:
            continue
        owners = (
            material_owners.get(material_id, set())
            if mesh_id == -10
            else exact_pair_owners.get((material_id, mesh_id), set())
        )
        touches_managed = (
            material_id in managed_material_ids
            or (mesh_id > 0 and mesh_id in managed_mesh_ids)
        )
        if not owners:
            if touches_managed:
                blocking.append({
                    "reason_code": "managed_live_pair_has_no_unique_group",
                    "binding": copy.deepcopy(live),
                })
            continue
        if len(owners) != 1:
            blocking.append({
                "reason_code": "managed_live_pair_provider_ambiguous",
                "binding": copy.deepcopy(live),
                "provider_count": len(owners),
            })
            continue
        guid, slot = _slot_key(live)
        if not guid:
            blocking.append({
                "reason_code": "managed_live_generator_guid_missing",
                "binding": copy.deepcopy(live),
            })
            continue
        if not slot:
            blocking.append({
                "reason_code": "managed_live_slot_prefix_missing",
                "binding": copy.deepcopy(live),
            })
            continue
        slot_identity = (guid, slot)
        previous = seen_live.get(slot_identity)
        if previous is not None:
            blocking.append({
                "reason_code": "managed_live_slot_identity_duplicated",
                "first": previous,
                "second": copy.deepcopy(live),
            })
            continue
        seen_live[slot_identity] = copy.deepcopy(live)
        owner = next(iter(owners))
        assignment = {
            "generator_guid": guid,
            "slot_prefix": slot,
            "generator_index": _integer(live.get("generator_index")),
            "generator_name": str(live.get("generator_name") or "").strip(),
            "generator_type": str(live.get("generator_type") or "").strip(),
            "target_material_id": material_id,
            "target_mesh_id": mesh_id,
        }
        assignments[owner].append(assignment)

    for owner, rows in assignments.items():
        rows.sort(key=lambda row: _slot_key(row))
        for row in rows:
            key_value = _slot_key(row)
            claiming = []
            for provider_key, provider in providers.items():
                for record in provider["records"]:
                    payload = record["payload"]
                    explicit = _validate_existing_ownership(
                        payload, path=record["path"]
                    )
                    values = explicit
                    if values is None:
                        values = (
                            (payload.get("generator_connection") or {}).get("bindings")
                            or []
                        )
                    if any(
                        isinstance(value, dict) and _slot_key(value) == key_value
                        for value in values
                    ):
                        claiming.append(provider_key)
                        break
            relinquished = sorted({
                value for value in claiming if value != owner
            })
            if relinquished:
                takeovers.append({
                    "generator_guid": row["generator_guid"],
                    "slot_prefix": row["slot_prefix"],
                    "live_material_id": row["target_material_id"],
                    "live_mesh_id": row["target_mesh_id"],
                    "current_provider": list(owner),
                    "relinquished_providers": [list(value) for value in relinquished],
                })
    return assignments, takeovers, blocking


def _current_binding_row(assignment, provider):
    key = _slot_key(assignment)
    templates = list(provider["binding_templates"].get(key) or [])
    templates.sort(key=lambda item: (
        0
        if (
            _integer(item["row"].get("target_material_id"))
            == assignment["target_material_id"]
            and _integer(item["row"].get("target_mesh_id"))
            == assignment["target_mesh_id"]
        )
        else 1,
        item["precedence"],
        item["path"].casefold(),
    ))
    if templates:
        row = copy.deepcopy(templates[0]["row"])
    else:
        row = {"state": "reconciled_live_owner"}
    row.update({
        "generator_guid": assignment["generator_guid"],
        "slot_prefix": assignment["slot_prefix"],
        "generator_index": assignment.get("generator_index"),
        "generator_name": assignment.get("generator_name") or "",
        "generator_type": assignment.get("generator_type") or "",
        "target_material_id": assignment["target_material_id"],
        "target_mesh_id": assignment["target_mesh_id"],
        "created_slot": False,
    })
    if (
        not templates
        or _integer(templates[0]["row"].get("target_material_id"))
        != assignment["target_material_id"]
        or _integer(templates[0]["row"].get("target_mesh_id"))
        != assignment["target_mesh_id"]
    ):
        row["state"] = "reconciled_live_owner"
    for field in (
        "variant_parent_property",
        "variant_parent_children_before",
        "variant_parent_children_after",
        "created_material_property",
        "created_mesh_property",
        "created_property_names",
    ):
        row.pop(field, None)
    return row


def _record_current_rows(payload, *, path):
    explicit = _validate_existing_ownership(payload, path=path)
    if explicit is not None:
        return explicit
    return [
        copy.deepcopy(row)
        for row in (
            (payload.get("generator_connection") or {}).get("bindings") or []
        )
        if isinstance(row, dict)
    ]


def _relinquished_rows(record, provider, current_rows, successor_by_slot):
    current_by_slot = {_slot_key(row): row for row in current_rows}
    history = []
    seen = set()
    for row in _record_current_rows(record["payload"], path=record["path"]):
        key = _slot_key(row)
        replacement = current_by_slot.get(key)
        if replacement is not None and (
            _integer(row.get("target_material_id")),
            _integer(row.get("target_mesh_id")),
        ) == (
            _integer(replacement.get("target_material_id")),
            _integer(replacement.get("target_mesh_id")),
        ):
            continue
        historical = copy.deepcopy(row)
        historical["ownership_state"] = "relinquished"
        successor = successor_by_slot.get(key)
        if successor is None:
            historical["reason"] = "live_spm_slot_missing_or_unmanaged"
        else:
            successor_key, live, successor_identity = successor
            historical["reason"] = (
                "live_spm_same_provider_rebound"
                if successor_key == provider["provider_key"]
                else "live_spm_exact_successor_binding"
            )
            historical["live_target_material_id"] = live[
                "target_material_id"
            ]
            historical["live_target_mesh_id"] = live["target_mesh_id"]
            historical["successor_provider"] = copy.deepcopy(
                successor_identity
            )
        fingerprint = _sha256_bytes(_canonical_json_bytes(historical))
        if fingerprint not in seen:
            seen.add(fingerprint)
            history.append(historical)
    return history


def _updated_payload(
    record,
    provider,
    assignments,
    spm_sha256,
    successor_by_slot,
):
    payload = copy.deepcopy(record["payload"])
    connection = copy.deepcopy(payload.get("generator_connection") or {})
    original_bindings = connection.get("bindings") or []
    if original_bindings and "authored_bindings" not in connection:
        connection["authored_bindings"] = copy.deepcopy(original_bindings)
    current_rows = [
        _current_binding_row(assignment, provider)
        for assignment in assignments
    ]
    current_rows.sort(key=lambda row: _slot_key(row))
    connection["bindings"] = current_rows
    relinquished = list(connection.get("relinquished_bindings") or [])
    known_relinquished = {
        _sha256_bytes(_canonical_json_bytes(row))
        for row in relinquished
        if isinstance(row, dict)
    }
    for row in _relinquished_rows(
        record,
        provider,
        current_rows,
        successor_by_slot,
    ):
        fingerprint = _sha256_bytes(_canonical_json_bytes(row))
        if fingerprint not in known_relinquished:
            relinquished.append(row)
            known_relinquished.add(fingerprint)
    if relinquished:
        connection["relinquished_bindings"] = relinquished
    payload["generator_connection"] = connection
    payload["generator_binding_ownership"] = _ownership_block(
        current_rows,
        spm_sha256=spm_sha256,
    )
    payload["generator_slot_creation_provenance"] = _provenance_block(
        provider["creator_rows"].values()
    )
    return payload


def _plan_hash(plan):
    value = copy.deepcopy(plan)
    value.pop("plan_sha256", None)
    return _sha256_bytes(_canonical_json_bytes(value))


def plan_atlas_slot_ownership_reconciliation(target_spm, *, live_snapshot=None):
    """Build a read-only, content-sealed per-slot ownership handoff plan."""
    target = Path(target_spm).expanduser().resolve(strict=False)
    base = {
        "contract": PLAN_CONTRACT,
        "schema_version": PLAN_SCHEMA_VERSION,
        "target_spm": str(target),
        "status": "blocked",
        "reason_code": "unplanned",
        "spm_sha256": "",
        "spm_text_sha256": "",
        "manifest_preconditions": [],
        "provider_updates": [],
        "takeovers": [],
        "blocking": [],
        "ignored_candidates": [],
        "writes": [],
    }
    try:
        if not target.is_file():
            raise AtlasSlotOwnershipError(
                "target_spm_missing",
                f"Target SPM does not exist: {target}",
            )
        snapshot = _stable_live_snapshot(target, live_snapshot)
        base["spm_sha256"] = snapshot["spm_sha256"]
        base["spm_text_sha256"] = snapshot["spm_text_sha256"]
        records, ignored = _read_candidate_records(target)
        base["ignored_candidates"] = ignored
        if not records:
            raise AtlasSlotOwnershipError(
                "operational_manifest_candidates_missing",
                f"No exact operational Atlas receipts exist for {target.name}.",
            )
        base["manifest_preconditions"] = [
            {"path": row["path"], "sha256": row["before_sha256"]}
            for row in sorted(records, key=lambda item: item["path"].casefold())
        ]
        providers = _provider_state(records)
        assignments, takeovers, blocking = _live_owner_assignments(
            snapshot, providers
        )
        base["takeovers"] = takeovers
        base["blocking"] = blocking
        if blocking:
            base["reason_code"] = blocking[0]["reason_code"]
        else:
            writes = []
            provider_updates = []
            successor_by_slot = {
                _slot_key(assignment): (
                    key,
                    assignment,
                    providers[key]["source_identity"],
                )
                for key, values in assignments.items()
                for assignment in values
            }
            for key, provider in sorted(providers.items()):
                current = assignments.get(key) or []
                current_rows = [
                    _current_binding_row(row, provider) for row in current
                ]
                ownership = _ownership_block(
                    current_rows, spm_sha256=snapshot["spm_sha256"]
                )
                provenance = _provenance_block(
                    provider["creator_rows"].values()
                )
                paths = []
                changed_paths = []
                for record in provider["records"]:
                    paths.append(record["path"])
                    updated = _updated_payload(
                        record,
                        provider,
                        current,
                        snapshot["spm_sha256"],
                        successor_by_slot,
                    )
                    if updated != record["payload"]:
                        encoded = _pretty_json_bytes(updated)
                        writes.append({
                            "path": record["path"],
                            "before_sha256": record["before_sha256"],
                            "after_sha256": _sha256_bytes(encoded),
                            "payload": updated,
                        })
                        changed_paths.append(record["path"])
                provider_updates.append({
                    "source_identity": copy.deepcopy(provider["source_identity"]),
                    "manifest_paths": paths,
                    "changed_manifest_paths": changed_paths,
                    "current_binding_count": ownership["binding_count"],
                    "current_binding_fingerprint": ownership["fingerprint"],
                    "creator_slot_count": provenance["slot_count"],
                    "creator_provenance_fingerprint": provenance["fingerprint"],
                    "current_bindings": ownership["bindings"],
                })
            base["provider_updates"] = provider_updates
            base["writes"] = sorted(
                writes, key=lambda row: row["path"].casefold()
            )
            base["status"] = "repairable" if writes else "current"
            base["reason_code"] = (
                "live_spm_ownership_reconciliation_required"
                if writes
                else "live_spm_ownership_already_current"
            )
    except AtlasSlotOwnershipError as exc:
        base["status"] = "blocked"
        base["reason_code"] = exc.reason_code
        base["blocking"].append({
            "reason_code": exc.reason_code,
            "reason": str(exc),
            "evidence": exc.evidence,
        })
    base["plan_sha256"] = _plan_hash(base)
    return base


def _atomic_write_bytes(path, encoded):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _validate_plan(plan):
    if not isinstance(plan, dict):
        raise AtlasSlotOwnershipError(
            "plan_not_object", "Ownership reconciliation plan is not an object."
        )
    if (
        plan.get("contract") != PLAN_CONTRACT
        or _integer(plan.get("schema_version")) != PLAN_SCHEMA_VERSION
    ):
        raise AtlasSlotOwnershipError(
            "plan_contract_unsupported",
            "Unsupported ownership reconciliation plan contract.",
        )
    if str(plan.get("plan_sha256") or "").strip().casefold() != _plan_hash(plan):
        raise AtlasSlotOwnershipError(
            "plan_fingerprint_mismatch",
            "Ownership reconciliation plan fingerprint is invalid.",
        )


def validate_atlas_slot_ownership_plan(
    plan,
    *,
    target_spm=None,
    require_repairable=False,
):
    """Validate and copy one sealed plan without reading or writing live files."""

    _validate_plan(plan)
    validated = copy.deepcopy(plan)
    target = Path(str(validated.get("target_spm") or "")).expanduser()
    if not target.is_absolute() or target.suffix.casefold() != ".spm":
        raise AtlasSlotOwnershipError(
            "plan_target_invalid",
            "Ownership reconciliation plan has no absolute target SPM.",
        )
    if target_spm is not None and normalized_manifest_path(
        target
    ) != normalized_manifest_path(target_spm):
        raise AtlasSlotOwnershipError(
            "plan_foreign_target",
            "Ownership reconciliation plan belongs to another target SPM.",
        )
    status = str(validated.get("status") or "").casefold()
    if status not in {"blocked", "current", "repairable"}:
        raise AtlasSlotOwnershipError(
            "plan_status_invalid",
            "Ownership reconciliation plan status is invalid.",
        )
    if require_repairable and status != "repairable":
        raise AtlasSlotOwnershipError(
            str(validated.get("reason_code") or "plan_not_repairable"),
            "Ownership reconciliation plan is not repairable.",
            evidence={"blocking": validated.get("blocking") or []},
        )

    def valid_sha256(value):
        text = str(value or "")
        return (
            len(text) == 64
            and text == text.casefold()
            and all(character in "0123456789abcdef" for character in text)
        )

    if not valid_sha256(validated.get("spm_sha256")) or not valid_sha256(
        validated.get("spm_text_sha256")
    ):
        raise AtlasSlotOwnershipError(
            "plan_spm_fingerprint_invalid",
            "Ownership reconciliation plan has no exact SPM fingerprints.",
        )
    preconditions = validated.get("manifest_preconditions")
    writes = validated.get("writes")
    if not isinstance(preconditions, list) or not isinstance(writes, list):
        raise AtlasSlotOwnershipError(
            "plan_manifest_scope_invalid",
            "Ownership reconciliation plan has no exact manifest scope.",
        )
    precondition_map = {}
    for row in preconditions:
        if not isinstance(row, dict):
            raise AtlasSlotOwnershipError(
                "plan_manifest_precondition_invalid",
                "Manifest preconditions must be objects.",
            )
        path = Path(str(row.get("path") or "")).expanduser()
        if (
            not path.is_absolute()
            or path.suffix.casefold() != ".json"
            or not valid_sha256(row.get("sha256"))
        ):
            raise AtlasSlotOwnershipError(
                "plan_manifest_precondition_invalid",
                "Manifest precondition identity is invalid.",
            )
        key = normalized_manifest_path(path)
        if key in precondition_map:
            raise AtlasSlotOwnershipError(
                "plan_manifest_precondition_duplicated",
                f"Manifest precondition is duplicated: {path}",
            )
        precondition_map[key] = row["sha256"]
    write_keys = set()
    for row in writes:
        if not isinstance(row, dict) or not isinstance(row.get("payload"), dict):
            raise AtlasSlotOwnershipError(
                "plan_write_invalid",
                "Planned manifest writes must contain exact JSON payloads.",
            )
        path = Path(str(row.get("path") or "")).expanduser()
        key = normalized_manifest_path(path)
        if (
            not path.is_absolute()
            or path.suffix.casefold() != ".json"
            or key in write_keys
            or precondition_map.get(key) != row.get("before_sha256")
            or not valid_sha256(row.get("after_sha256"))
            or _sha256_bytes(_pretty_json_bytes(row["payload"]))
            != row["after_sha256"]
        ):
            raise AtlasSlotOwnershipError(
                "plan_write_invalid",
                f"Planned manifest write is invalid: {path}",
            )
        write_keys.add(key)
    if status == "repairable" and not writes:
        raise AtlasSlotOwnershipError(
            "plan_writes_missing",
            "Repairable ownership plan contains no manifest writes.",
        )
    if status == "current" and writes:
        raise AtlasSlotOwnershipError(
            "current_plan_has_writes",
            "Current ownership plan unexpectedly contains manifest writes.",
        )
    return validated


def apply_atlas_slot_ownership_reconciliation(plan):
    """Apply one sealed plan with CAS checks, rollback, and live verification."""
    plan = validate_atlas_slot_ownership_plan(plan)
    if plan.get("status") == "current":
        return {**copy.deepcopy(plan), "apply_status": "current"}
    if plan.get("status") != "repairable":
        raise AtlasSlotOwnershipError(
            str(plan.get("reason_code") or "plan_not_repairable"),
            "Atlas slot ownership is not deterministically repairable.",
            evidence={"blocking": plan.get("blocking") or []},
        )
    target = Path(plan["target_spm"])
    if _sha256_file(target) != plan.get("spm_sha256"):
        raise AtlasSlotOwnershipError(
            "spm_precondition_changed",
            "Target SPM changed after the ownership plan was sealed.",
        )
    writes = list(plan.get("writes") or [])
    originals = {}
    for row in writes:
        path = Path(row["path"])
        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise AtlasSlotOwnershipError(
                "manifest_precondition_unreadable",
                f"Cannot read planned Atlas manifest: {path}: {exc}",
            ) from exc
        if _sha256_bytes(raw) != row.get("before_sha256"):
            raise AtlasSlotOwnershipError(
                "manifest_precondition_changed",
                f"Atlas manifest changed after planning: {path}",
            )
        encoded = _pretty_json_bytes(row.get("payload"))
        if _sha256_bytes(encoded) != row.get("after_sha256"):
            raise AtlasSlotOwnershipError(
                "planned_payload_fingerprint_mismatch",
                f"Planned Atlas payload fingerprint is invalid: {path}",
            )
        originals[path] = raw

    committed = []
    try:
        for row in writes:
            path = Path(row["path"])
            _atomic_write_bytes(path, _pretty_json_bytes(row["payload"]))
            committed.append(path)
        if _sha256_file(target) != plan.get("spm_sha256"):
            raise AtlasSlotOwnershipError(
                "spm_changed_during_apply",
                "Target SPM changed while Atlas manifests were being replaced.",
            )
        resolution = resolve_atlas_manifests(target)
        from pcg_st9_texture_batch.pcg_texture_audit import (
            live_generator_delivery_snapshot,
        )

        live = live_generator_delivery_snapshot(target)
        diagnostics = diagnose_manifest_generator_candidates(
            resolution,
            live.get("leaf_generator_bindings") or [],
        )
        if diagnostics.get("status") != "coherent":
            raise AtlasSlotOwnershipError(
                "postwrite_live_diagnostics_conflict",
                "Atlas manifests still disagree with live Generator ownership.",
                evidence=diagnostics,
            )
        verification = plan_atlas_slot_ownership_reconciliation(
            target,
            live_snapshot=live,
        )
        if verification.get("status") != "current":
            raise AtlasSlotOwnershipError(
                "postwrite_reconciliation_not_idempotent",
                "Atlas ownership reconciliation did not reach an idempotent state.",
                evidence=verification,
            )
    except Exception as exc:
        rollback_errors = []
        for path in reversed(committed):
            try:
                _atomic_write_bytes(path, originals[path])
            except Exception as rollback_exc:  # pragma: no cover - catastrophic I/O
                rollback_errors.append({
                    "path": str(path),
                    "error": f"{type(rollback_exc).__name__}: {rollback_exc}",
                })
        if rollback_errors:
            raise AtlasSlotOwnershipError(
                "rollback_failed",
                "Atlas ownership apply failed and exact-byte rollback was incomplete.",
                evidence={
                    "original_error": f"{type(exc).__name__}: {exc}",
                    "rollback_errors": rollback_errors,
                },
            ) from exc
        if isinstance(exc, AtlasSlotOwnershipError):
            raise
        if isinstance(exc, AtlasManifestResolutionError):
            raise AtlasSlotOwnershipError(
                "postwrite_manifest_resolution_failed",
                str(exc),
                evidence=exc.resolution,
            ) from exc
        raise AtlasSlotOwnershipError(
            "ownership_apply_failed",
            f"Atlas ownership reconciliation failed: {exc}",
        ) from exc

    return {
        **copy.deepcopy(plan),
        "apply_status": "reconciled",
        "verified_resolution": resolution_evidence(resolution),
        "verified_live_diagnostics": diagnostics,
        "verified_plan_sha256": verification["plan_sha256"],
    }


def reconcile_atlas_slot_ownership(target_spm, *, live_snapshot=None):
    """Plan and, when safe, apply current ownership for one exact target."""
    plan = plan_atlas_slot_ownership_reconciliation(
        target_spm,
        live_snapshot=live_snapshot,
    )
    return apply_atlas_slot_ownership_reconciliation(plan)


def plan_atlas_slot_ownership_fleet(target_spms, *, live_snapshots=None):
    """Seal independent exact-target plans as one all-or-nothing JSON fleet."""
    snapshots = live_snapshots or {}
    plans = []
    seen_targets = set()
    for value in target_spms or []:
        target = Path(value).expanduser().resolve(strict=False)
        key = normalized_manifest_path(target)
        if key in seen_targets:
            continue
        seen_targets.add(key)
        supplied = snapshots.get(str(target)) if isinstance(snapshots, dict) else None
        if supplied is None and isinstance(snapshots, dict):
            supplied = snapshots.get(key)
        plans.append(plan_atlas_slot_ownership_reconciliation(
            target,
            live_snapshot=supplied,
        ))
    blocking = [
        {
            "target_spm": plan["target_spm"],
            "reason_code": plan["reason_code"],
            "blocking": copy.deepcopy(plan.get("blocking") or []),
        }
        for plan in plans
        if plan.get("status") == "blocked"
    ]
    writes = {}
    for plan in plans:
        for row in plan.get("writes") or []:
            key = normalized_manifest_path(row["path"])
            previous = writes.get(key)
            if previous is not None and previous != row:
                blocking.append({
                    "target_spm": plan["target_spm"],
                    "reason_code": "fleet_manifest_write_disagreement",
                    "path": row["path"],
                })
            else:
                writes[key] = copy.deepcopy(row)
    fleet = {
        "contract": FLEET_PLAN_CONTRACT,
        "schema_version": FLEET_PLAN_SCHEMA_VERSION,
        "status": (
            "blocked"
            if blocking
            else "repairable"
            if any(plan.get("status") == "repairable" for plan in plans)
            else "current"
        ),
        "reason_code": (
            blocking[0]["reason_code"]
            if blocking
            else "live_spm_fleet_ownership_reconciliation_required"
            if any(plan.get("status") == "repairable" for plan in plans)
            else "live_spm_fleet_ownership_already_current"
        ),
        "target_count": len(plans),
        "write_count": len(writes),
        "target_plans": plans,
        "blocking": blocking,
    }
    fleet["plan_sha256"] = _plan_hash(fleet)
    return fleet


def _validate_fleet_plan(plan):
    if not isinstance(plan, dict):
        raise AtlasSlotOwnershipError(
            "fleet_plan_not_object",
            "Atlas ownership fleet plan is not an object.",
        )
    if (
        plan.get("contract") != FLEET_PLAN_CONTRACT
        or _integer(plan.get("schema_version")) != FLEET_PLAN_SCHEMA_VERSION
    ):
        raise AtlasSlotOwnershipError(
            "fleet_plan_contract_unsupported",
            "Unsupported Atlas ownership fleet plan contract.",
        )
    if str(plan.get("plan_sha256") or "").strip().casefold() != _plan_hash(plan):
        raise AtlasSlotOwnershipError(
            "fleet_plan_fingerprint_mismatch",
            "Atlas ownership fleet plan fingerprint is invalid.",
        )
    children = plan.get("target_plans")
    if not isinstance(children, list):
        raise AtlasSlotOwnershipError(
            "fleet_target_plans_missing",
            "Atlas ownership fleet plan has no target plans.",
        )
    for child in children:
        _validate_plan(child)


def apply_atlas_slot_ownership_fleet(plan):
    """Apply all exact-target receipt rewrites in one rollback domain."""
    _validate_fleet_plan(plan)
    if plan.get("status") == "current":
        return {**copy.deepcopy(plan), "apply_status": "current"}
    if plan.get("status") != "repairable":
        raise AtlasSlotOwnershipError(
            str(plan.get("reason_code") or "fleet_plan_not_repairable"),
            "Atlas ownership fleet is not deterministically repairable.",
            evidence={"blocking": plan.get("blocking") or []},
        )
    children = list(plan["target_plans"])
    writes = {}
    for child in children:
        if child.get("status") not in {"current", "repairable"}:
            raise AtlasSlotOwnershipError(
                "fleet_child_not_repairable",
                f"Atlas fleet child is not repairable: {child.get('target_spm')}",
            )
        target = Path(child["target_spm"])
        if _sha256_file(target) != child.get("spm_sha256"):
            raise AtlasSlotOwnershipError(
                "spm_precondition_changed",
                f"Target SPM changed after fleet planning: {target}",
            )
        for row in child.get("writes") or []:
            key = normalized_manifest_path(row["path"])
            previous = writes.get(key)
            if previous is not None and previous != row:
                raise AtlasSlotOwnershipError(
                    "fleet_manifest_write_disagreement",
                    f"Fleet targets disagree on a shared manifest: {row['path']}",
                )
            writes[key] = copy.deepcopy(row)

    ordered_writes = sorted(
        writes.values(), key=lambda row: row["path"].casefold()
    )
    originals = {}
    for row in ordered_writes:
        path = Path(row["path"])
        raw = path.read_bytes()
        if _sha256_bytes(raw) != row.get("before_sha256"):
            raise AtlasSlotOwnershipError(
                "manifest_precondition_changed",
                f"Atlas manifest changed after fleet planning: {path}",
            )
        encoded = _pretty_json_bytes(row.get("payload"))
        if _sha256_bytes(encoded) != row.get("after_sha256"):
            raise AtlasSlotOwnershipError(
                "planned_payload_fingerprint_mismatch",
                f"Fleet payload fingerprint is invalid: {path}",
            )
        originals[path] = raw

    committed = []
    verified = []
    try:
        for row in ordered_writes:
            path = Path(row["path"])
            _atomic_write_bytes(path, _pretty_json_bytes(row["payload"]))
            committed.append(path)
        from pcg_st9_texture_batch.pcg_texture_audit import (
            live_generator_delivery_snapshot,
        )

        for child in children:
            target = Path(child["target_spm"])
            if _sha256_file(target) != child.get("spm_sha256"):
                raise AtlasSlotOwnershipError(
                    "spm_changed_during_apply",
                    f"Target SPM changed during fleet apply: {target}",
                )
            resolution = resolve_atlas_manifests(target)
            live = live_generator_delivery_snapshot(target)
            diagnostics = diagnose_manifest_generator_candidates(
                resolution,
                live.get("leaf_generator_bindings") or [],
            )
            if diagnostics.get("status") != "coherent":
                raise AtlasSlotOwnershipError(
                    "postwrite_live_diagnostics_conflict",
                    f"Fleet target remains incoherent: {target}",
                    evidence=diagnostics,
                )
            current = plan_atlas_slot_ownership_reconciliation(
                target,
                live_snapshot=live,
            )
            if current.get("status") != "current":
                raise AtlasSlotOwnershipError(
                    "postwrite_reconciliation_not_idempotent",
                    f"Fleet target did not reach an idempotent state: {target}",
                    evidence=current,
                )
            verified.append({
                "target_spm": str(target),
                "resolution": resolution_evidence(resolution),
                "live_diagnostics": diagnostics,
                "verified_plan_sha256": current["plan_sha256"],
            })
    except Exception as exc:
        rollback_errors = []
        for path in reversed(committed):
            try:
                _atomic_write_bytes(path, originals[path])
            except Exception as rollback_exc:  # pragma: no cover - catastrophic I/O
                rollback_errors.append({
                    "path": str(path),
                    "error": f"{type(rollback_exc).__name__}: {rollback_exc}",
                })
        if rollback_errors:
            raise AtlasSlotOwnershipError(
                "rollback_failed",
                "Atlas fleet apply failed and exact-byte rollback was incomplete.",
                evidence={
                    "original_error": f"{type(exc).__name__}: {exc}",
                    "rollback_errors": rollback_errors,
                },
            ) from exc
        if isinstance(exc, AtlasSlotOwnershipError):
            raise
        if isinstance(exc, AtlasManifestResolutionError):
            raise AtlasSlotOwnershipError(
                "postwrite_manifest_resolution_failed",
                str(exc),
                evidence=exc.resolution,
            ) from exc
        raise AtlasSlotOwnershipError(
            "ownership_fleet_apply_failed",
            f"Atlas ownership fleet reconciliation failed: {exc}",
        ) from exc
    return {
        **copy.deepcopy(plan),
        "apply_status": "reconciled",
        "verified_targets": verified,
    }
