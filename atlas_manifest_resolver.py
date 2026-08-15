"""Fail-closed selection for Atlas -> SpeedTree relationship manifests.

Atlas writes three operational copies of a target relationship:

* ``.atlas_leaf_speedtree_targets/*.json`` (exact per-target receipt),
* ``.atlas_leaf_speedtree_scopes/*__<target-stem>.json`` (exact scope), and
* ``speedtree_import_manifest.json`` (rolling global target record).

They are considered in that order.  Coherent lower-precedence mirrors remain
selected so a writer can update every current operational record.  Disjoint
scope records may coexist because one target can consume multiple Atlas
providers.  Any two operational records that overlap and disagree on source
identity, material ownership, material-group content, or a Generator binding
fail closed.  Precedence never turns a disagreement into a last-writer win.

A Cluster output carries two names for one file: the canonical ``SK_<name>``
production output and the legacy unprefixed ``<name>`` normalization input.
An Atlas export aimed at the legacy name therefore writes records that name
the target's own mirror.  Those records are accepted for the canonical target
only when ``cluster_spm_pair_contract`` has a **complete** normalization
receipt proving both names hold identical content -- name shape alone proves
nothing, and a record naming an unrelated SPM still fails closed.

Historical ``speedtree_import_manifest_M_*.json`` files and non-target-
suffixed scope identity files are evidence only.  They are never returned as
operational payloads.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path

from cluster_spm_pair_contract import (
    ClusterSpmPairError,
    resolve_cluster_spm_pair,
)


RESOLUTION_SCHEMA_VERSION = 1
SUPPORTED_ATLAS_SCHEMA_VERSIONS = {1}
GENERATOR_BINDING_OWNERSHIP_CONTRACT = (
    "atlas_generator_current_binding_ownership"
)
GENERATOR_BINDING_OWNERSHIP_VERSION = 1
GENERATOR_SLOT_CREATION_PROVENANCE_CONTRACT = (
    "atlas_generator_slot_creation_provenance"
)
GENERATOR_SLOT_CREATION_PROVENANCE_VERSION = 1

KIND_PRECEDENCE = {
    "exact_per_target": 0,
    "exact_target_scope": 1,
    "exact_global_target": 2,
}

# Within one Cluster pair the canonical ``SK_`` output is the production
# identity and the unprefixed name is normalization input only, so a record
# written against the legacy name is ordered after every canonical-named one.
CANONICAL_IDENTITY_RANK = 0
LEGACY_IDENTITY_RANK = 1

_DERIVED_TEXTURE_FIELDS = {
    "blender_cluster_bake_texture",
    "canonical_texture_output",
    "source_texture_fallback",
    "texture_origin_receipt",
    "texture_provisional_receipt",
    "texture_remediation",
    "texture_source_origin",
    "texture_warning",
}


class AtlasManifestResolutionError(RuntimeError):
    """The exact Atlas manifest set is unreadable or contradictory."""

    def __init__(self, message, resolution):
        super().__init__(message)
        self.resolution = resolution


class _GeneratorContractError(ValueError):
    """One explicit Generator ownership/provenance block is untrustworthy."""

    def __init__(self, reason, **details):
        super().__init__(reason)
        self.reason = str(reason)
        self.details = dict(details)


class _GeneratorBindingOwnershipContractError(_GeneratorContractError):
    """One explicit current-binding ownership block is not trustworthy."""


class _GeneratorSlotCreationProvenanceContractError(_GeneratorContractError):
    """One explicit slot-creation provenance block is not trustworthy."""


def normalized_manifest_path(path, *, relative_to=None):
    """Return a stable, case-insensitive filesystem identity."""
    value = Path(str(path or "")).expanduser()
    if relative_to is not None and not value.is_absolute():
        value = Path(relative_to) / value
    try:
        value = value.resolve(strict=False)
    except (OSError, RuntimeError):
        value = value.absolute()
    return os.path.normcase(str(value)).casefold()


def _read_payload(path):
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return None, f"unreadable_json: {exc}"
    if not isinstance(payload, dict):
        return None, "manifest_root_not_object"
    return payload, None


def _schema_contract(payload):
    for field in (
        "atlas_manifest_schema_version",
        "manifest_schema_version",
        "schema_version",
    ):
        if field not in payload:
            continue
        raw = payload.get(field)
        try:
            version = int(raw)
        except (TypeError, ValueError):
            return {
                "field": field,
                "version": raw,
                "status": "invalid",
                "reason": "schema_version_not_integer",
            }
        if version not in SUPPORTED_ATLAS_SCHEMA_VERSIONS:
            return {
                "field": field,
                "version": version,
                "status": "unsupported",
                "reason": "unsupported_schema_version",
            }
        return {
            "field": field,
            "version": version,
            "status": "supported",
            "reason": "explicit_supported_schema",
        }
    # The shipping Atlas add-on predates an explicit manifest schema field.
    # Keeping this named compatibility contract preserves existing receipts
    # without silently accepting an explicitly unknown future schema.
    return {
        "field": None,
        "version": 1,
        "status": "supported",
        "reason": "compatibility_unversioned_v1",
    }


def _source_identity(payload, path, *, target_parent=None):
    blend = str(payload.get("blend_file") or "").strip()
    return {
        "blend_file": (
            normalized_manifest_path(
                blend,
                relative_to=target_parent or Path(path).parent,
            )
            if blend
            else ""
        ),
        "source_collection": str(
            payload.get("source_collection") or ""
        ).strip().casefold(),
        "export_scope_id": str(
            payload.get("export_scope_id") or ""
        ).strip().casefold(),
    }


def _without_derived_texture_fields(value):
    if isinstance(value, dict):
        return {
            str(key): _without_derived_texture_fields(item)
            for key, item in sorted(value.items(), key=lambda row: str(row[0]))
            if str(key) not in _DERIVED_TEXTURE_FIELDS
        }
    if isinstance(value, list):
        return [_without_derived_texture_fields(item) for item in value]
    return value


def _material_groups(payload):
    # The post-SPM list carries final SpeedTree ownership.  Pre-apply and older
    # manifests only have material_groups, so retain that compatibility path.
    groups = payload.get("speedtree_material_groups")
    if not isinstance(groups, list) or not groups:
        groups = payload.get("material_groups")
    return [row for row in groups or [] if isinstance(row, dict)]


def _claim_projection(group):
    return _without_derived_texture_fields(copy.deepcopy(group))


def _integer_claim_value(value):
    if isinstance(value, bool):
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def normalize_generator_target_material_id(value):
    """Return one semantic Material ID, or ``None`` when invalid."""
    normalized = _integer_claim_value(value)
    return normalized if normalized is not None and normalized > 0 else None


def normalize_generator_target_mesh_id(value):
    """Return a positive Mesh ID or SpeedTree's default-cutout sentinel."""
    normalized = _integer_claim_value(value)
    if normalized == -10 or (normalized is not None and normalized > 0):
        return normalized
    return None


def _canonical_json_sha256(value):
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def generator_binding_ownership_projection(bindings):
    """Return the canonical current-owner rows shared with reconcilers."""
    rows = []
    for binding in bindings or []:
        if not isinstance(binding, dict):
            continue
        rows.append({
            "generator_guid": str(
                binding.get("generator_guid") or ""
            ).strip(),
            "slot_prefix": str(binding.get("slot_prefix") or "").strip(),
            "target_material_id": normalize_generator_target_material_id(
                binding.get("target_material_id")
            ),
            "target_mesh_id": normalize_generator_target_mesh_id(
                binding.get("target_mesh_id")
            ),
        })
    return sorted(
        rows,
        key=lambda row: (
            row["generator_guid"],
            row["slot_prefix"],
        ),
    )


def generator_binding_ownership_fingerprint(bindings):
    return _canonical_json_sha256(
        generator_binding_ownership_projection(bindings)
    )


def generator_slot_creation_provenance_fingerprint(slots):
    """Fingerprint full creator rows without turning them into ownership."""
    normalized = []
    for slot in slots or []:
        if not isinstance(slot, dict):
            continue
        row = copy.deepcopy(slot)
        row["generator_guid"] = str(
            row.get("generator_guid") or ""
        ).strip()
        row["slot_prefix"] = str(row.get("slot_prefix") or "").strip()
        normalized.append(row)
    normalized.sort(key=lambda row: (
        row["generator_guid"],
        row["slot_prefix"],
        json.dumps(
            row,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
    ))
    return _canonical_json_sha256(normalized)


def _generator_binding_slot_identity(binding, *, require_guid=False):
    """Return an opaque Generator/slot identity without folding GUID case."""
    guid = str(binding.get("generator_guid") or "").strip()
    slot = str(binding.get("slot_prefix") or "").strip()
    if require_guid and not guid:
        return None, slot
    if guid:
        return ("guid", guid), slot
    index = str(binding.get("generator_index") or "").strip()
    name = str(binding.get("generator_name") or "").strip().casefold()
    owner = (
        ("index", index)
        if index
        else ("name", name)
        if name
        else None
    )
    return owner, slot


def _generator_binding_claim_projection(binding, *, require_guid=False):
    owner, slot = _generator_binding_slot_identity(
        binding,
        require_guid=require_guid,
    )
    if owner is None or not slot:
        return None
    owner_kind, owner_value = owner
    projection = {
        "generator_identity": {
            "kind": owner_kind,
            "value": owner_value,
        },
        "slot_prefix": slot,
        "target_material_id": normalize_generator_target_material_id(
            binding.get("target_material_id")
        ),
        "target_mesh_id": normalize_generator_target_mesh_id(
            binding.get("target_mesh_id")
        ),
    }
    return projection


def _explicit_generator_binding_ownership(payload):
    """Validate and normalize an additive current-ownership contract.

    ``generator_connection.bindings`` remains immutable authored/run evidence.
    Once this block exists, including with an empty list, only this block
    carries current binding ownership.
    """
    if "generator_binding_ownership" not in payload:
        return None
    contract = payload.get("generator_binding_ownership")
    if not isinstance(contract, dict):
        raise _GeneratorBindingOwnershipContractError(
            "generator_binding_ownership_not_object"
        )
    if contract.get("contract") != GENERATOR_BINDING_OWNERSHIP_CONTRACT:
        raise _GeneratorBindingOwnershipContractError(
            "generator_binding_ownership_contract_invalid",
            contract=contract.get("contract"),
        )
    version = contract.get("version")
    if isinstance(version, bool) or not isinstance(version, int):
        raise _GeneratorBindingOwnershipContractError(
            "generator_binding_ownership_version_invalid",
            version=version,
        )
    if version != GENERATOR_BINDING_OWNERSHIP_VERSION:
        raise _GeneratorBindingOwnershipContractError(
            "generator_binding_ownership_version_unsupported",
            version=version,
        )
    bindings = contract.get("bindings")
    if not isinstance(bindings, list):
        raise _GeneratorBindingOwnershipContractError(
            "generator_binding_ownership_bindings_not_list"
        )
    count = contract.get("binding_count")
    if (
        isinstance(count, bool)
        or not isinstance(count, int)
        or count != len(bindings)
    ):
        raise _GeneratorBindingOwnershipContractError(
            "generator_binding_ownership_binding_count_invalid",
            binding_count=count,
            actual_binding_count=len(bindings),
        )

    normalized = []
    seen = set()
    for ordinal, raw_binding in enumerate(bindings):
        if not isinstance(raw_binding, dict):
            raise _GeneratorBindingOwnershipContractError(
                "generator_binding_ownership_binding_not_object",
                binding_index=ordinal,
            )
        binding = copy.deepcopy(raw_binding)
        guid = str(binding.get("generator_guid") or "").strip()
        slot = str(binding.get("slot_prefix") or "").strip()
        material_id = normalize_generator_target_material_id(
            binding.get("target_material_id")
        )
        mesh_id = normalize_generator_target_mesh_id(
            binding.get("target_mesh_id")
        )
        if not guid:
            raise _GeneratorBindingOwnershipContractError(
                "generator_binding_ownership_generator_guid_missing",
                binding_index=ordinal,
            )
        if not slot:
            raise _GeneratorBindingOwnershipContractError(
                "generator_binding_ownership_slot_prefix_missing",
                binding_index=ordinal,
            )
        if material_id is None:
            raise _GeneratorBindingOwnershipContractError(
                "generator_binding_ownership_target_material_id_invalid",
                binding_index=ordinal,
            )
        if mesh_id is None:
            raise _GeneratorBindingOwnershipContractError(
                "generator_binding_ownership_target_mesh_id_invalid",
                binding_index=ordinal,
            )
        key = (guid, slot)
        if key in seen:
            raise _GeneratorBindingOwnershipContractError(
                "generator_binding_ownership_duplicate_slot",
                binding_index=ordinal,
                generator_guid=guid,
                slot_prefix=slot,
            )
        seen.add(key)
        binding["generator_guid"] = guid
        binding["slot_prefix"] = slot
        binding["target_material_id"] = material_id
        binding["target_mesh_id"] = mesh_id
        normalized.append(binding)
    fingerprint = str(contract.get("fingerprint") or "").strip()
    expected_fingerprint = generator_binding_ownership_fingerprint(normalized)
    if fingerprint != expected_fingerprint:
        raise _GeneratorBindingOwnershipContractError(
            "generator_binding_ownership_fingerprint_invalid",
            fingerprint=fingerprint,
            expected_fingerprint=expected_fingerprint,
        )
    return normalized


def _current_generator_bindings(payload):
    explicit = _explicit_generator_binding_ownership(payload)
    if explicit is not None:
        return explicit, True
    connection = payload.get("generator_connection") or {}
    bindings = connection.get("bindings") or []
    return [row for row in bindings if isinstance(row, dict)], False


def manifest_current_generator_bindings(payload):
    """Return current bindings, preferring explicit authority even when empty."""
    bindings, _explicit = _current_generator_bindings(payload)
    return copy.deepcopy(bindings)


def _explicit_generator_slot_creation_provenance(payload):
    if "generator_slot_creation_provenance" not in payload:
        return None
    provenance = payload.get("generator_slot_creation_provenance")
    error = _GeneratorSlotCreationProvenanceContractError
    if not isinstance(provenance, dict):
        raise error("generator_slot_creation_provenance_not_object")
    if (
        provenance.get("contract")
        != GENERATOR_SLOT_CREATION_PROVENANCE_CONTRACT
    ):
        raise error(
            "generator_slot_creation_provenance_contract_invalid",
            contract=provenance.get("contract"),
        )
    version = provenance.get("version")
    if isinstance(version, bool) or not isinstance(version, int):
        raise error(
            "generator_slot_creation_provenance_version_invalid",
            version=version,
        )
    if version != GENERATOR_SLOT_CREATION_PROVENANCE_VERSION:
        raise error(
            "generator_slot_creation_provenance_version_unsupported",
            version=version,
        )
    slots = provenance.get("slots")
    if not isinstance(slots, list):
        raise error("generator_slot_creation_provenance_slots_not_list")
    count = provenance.get("slot_count")
    if (
        isinstance(count, bool)
        or not isinstance(count, int)
        or count != len(slots)
    ):
        raise error(
            "generator_slot_creation_provenance_slot_count_invalid",
            slot_count=count,
            actual_slot_count=len(slots),
        )

    normalized = []
    seen = set()
    for ordinal, raw_slot in enumerate(slots):
        if not isinstance(raw_slot, dict):
            raise error(
                "generator_slot_creation_provenance_slot_not_object",
                slot_index=ordinal,
            )
        slot = copy.deepcopy(raw_slot)
        guid = str(slot.get("generator_guid") or "").strip()
        prefix = str(slot.get("slot_prefix") or "").strip()
        if not guid:
            raise error(
                "generator_slot_creation_provenance_generator_guid_missing",
                slot_index=ordinal,
            )
        if not prefix:
            raise error(
                "generator_slot_creation_provenance_slot_prefix_missing",
                slot_index=ordinal,
            )
        key = (guid, prefix)
        if key in seen:
            raise error(
                "generator_slot_creation_provenance_duplicate_slot",
                slot_index=ordinal,
                generator_guid=guid,
                slot_prefix=prefix,
            )
        seen.add(key)
        slot["generator_guid"] = guid
        slot["slot_prefix"] = prefix
        normalized.append(slot)

    fingerprint = str(provenance.get("fingerprint") or "").strip()
    expected_fingerprint = generator_slot_creation_provenance_fingerprint(
        normalized
    )
    if fingerprint != expected_fingerprint:
        raise error(
            "generator_slot_creation_provenance_fingerprint_invalid",
            fingerprint=fingerprint,
            expected_fingerprint=expected_fingerprint,
        )
    return normalized


def manifest_generator_slot_creation_provenance(payload):
    """Return immutable creator rows without treating them as current owners."""
    explicit = _explicit_generator_slot_creation_provenance(payload)
    if explicit is not None:
        return copy.deepcopy(explicit)
    connection = payload.get("generator_connection") or {}
    return [
        copy.deepcopy(row)
        for row in connection.get("bindings") or []
        if isinstance(row, dict) and row.get("created_slot")
    ]


def _candidate_claims(payload, source_identity, kind):
    # Creator history is deliberately not projected as ownership, but an
    # explicit block must still validate fail-closed before this receipt can
    # participate in resolution.
    _explicit_generator_slot_creation_provenance(payload)
    claims = {}
    for group in _material_groups(payload):
        name = str(group.get("material") or "").strip().casefold()
        material_id = str(group.get("material_id") or "").strip()
        projection = {
            "source_identity": source_identity,
            "material_group": _claim_projection(group),
        }
        if name:
            claims[f"material_name:{name}"] = projection
        if material_id:
            claims[f"material_id:{material_id}"] = projection

    bindings, explicit = _current_generator_bindings(payload)
    binding_keys = set()
    for binding in bindings:
        if (
            not explicit
            and "target_material_id" not in binding
            and "target_mesh_id" not in binding
        ):
            # Pre-ownership receipts used the whole legacy binding row and did
            # not publish semantic target IDs.  Preserve that read-only
            # compatibility path; once either target field is present the
            # stricter semantic ID contract below applies fail-closed.
            slot = str(binding.get("slot_prefix") or "").strip().casefold()
            guid = str(binding.get("generator_guid") or "").strip().casefold()
            index = str(binding.get("generator_index") or "").strip()
            name = str(binding.get("generator_name") or "").strip().casefold()
            owner = (
                f"guid:{guid}"
                if guid
                else f"index:{index}"
                if index
                else f"name:{name}"
                if name
                else ""
            )
            if not owner or not slot:
                continue
            claim_key = f"generator:{owner}:slot:{slot}"
            claims[claim_key] = {
                "source_identity": source_identity,
                "binding": _without_derived_texture_fields(
                    copy.deepcopy(binding)
                ),
            }
            continue
        projection = _generator_binding_claim_projection(
            binding,
            require_guid=explicit,
        )
        if projection is None:
            continue
        identity = projection["generator_identity"]
        slot = projection["slot_prefix"]
        if projection["target_material_id"] is None:
            raise _GeneratorBindingOwnershipContractError(
                "generator_binding_ownership_target_material_id_invalid"
                if explicit
                else "legacy_generator_binding_target_material_id_invalid",
                generator_identity=copy.deepcopy(identity),
                slot_prefix=slot,
            )
        if projection["target_mesh_id"] is None:
            raise _GeneratorBindingOwnershipContractError(
                "generator_binding_ownership_target_mesh_id_invalid"
                if explicit
                else "legacy_generator_binding_target_mesh_id_invalid",
                generator_identity=copy.deepcopy(identity),
                slot_prefix=slot,
            )
        claim_key = (
            f"generator:{identity['kind']}:{identity['value']}:slot:{slot}"
        )
        if claim_key in binding_keys:
            raise _GeneratorBindingOwnershipContractError(
                "generator_binding_ownership_duplicate_slot"
                if explicit
                else "legacy_generator_binding_duplicate_slot",
                generator_identity=copy.deepcopy(identity),
                slot_prefix=slot,
            )
        binding_keys.add(claim_key)
        claims[claim_key] = {
            "source_identity": source_identity,
            "binding": projection,
        }

    if not claims:
        # Empty/tombstone records still need deterministic duplicate handling.
        record_key = (
            f"scope:{source_identity['export_scope_id']}"
            if kind == "exact_target_scope"
            and source_identity["export_scope_id"]
            else "target"
        )
        claims[f"record:{record_key}"] = {
            "source_identity": source_identity,
            "current_generator_bindings": sorted(
                (
                    _generator_binding_claim_projection(
                        binding,
                        require_guid=explicit,
                    )
                    for binding in bindings
                ),
                key=lambda row: json.dumps(
                    row or {}, sort_keys=True, separators=(",", ":")
                ),
            ),
            "collection_tombstone": _without_derived_texture_fields(
                copy.deepcopy(payload.get("collection_tombstone"))
            ),
        }
    return claims


def _diagnostic_disjoint_candidate(candidate, winners, disagreement_keys):
    """Keep only claim groups unique to a later Provider for read-only audit.

    A single operational record may disagree with an earlier Provider for role
    A while uniquely supplying role B. Strict mutation selection still rejects
    that record as a whole. Live audit must not erase role B, so this projection
    carries only groups/bindings whose complete claim-key set is disjoint from
    every existing winner and from the disagreement set.
    """

    claims = candidate["_claims"]
    allowed_keys = set(claims) - set(winners)
    if not allowed_keys:
        return None
    disagreement_keys = set(disagreement_keys)
    payload = copy.deepcopy(candidate["payload"])

    def group_keys(group):
        keys = set()
        name = str(group.get("material") or "").strip().casefold()
        material_id = str(group.get("material_id") or "").strip()
        if name:
            keys.add(f"material_name:{name}")
        if material_id:
            keys.add(f"material_id:{material_id}")
        return keys

    kept_group_count = 0
    for field in ("speedtree_material_groups", "material_groups"):
        raw_groups = payload.get(field)
        if not isinstance(raw_groups, list):
            continue
        kept = []
        for group in raw_groups:
            if not isinstance(group, dict):
                continue
            keys = group_keys(group)
            if keys and keys.issubset(allowed_keys) and not keys & disagreement_keys:
                kept.append(group)
        payload[field] = kept
        kept_group_count += len(kept)

    # The Atlas writer also publishes single-group convenience fields at the
    # manifest root.  They are derived authority, not independent claims.  A
    # diagnostic projection must rebuild them from the groups that survived;
    # otherwise a removed Provider role can re-enter through material_id,
    # mesh_ids, an adoption snapshot, or a physical-capture receipt.
    projected_groups = _material_groups(payload)
    projected_names = [
        str(group.get("material") or "").strip()
        for group in projected_groups
        if str(group.get("material") or "").strip()
    ]
    projected_ids = [
        group.get("material_id")
        for group in projected_groups
        if str(group.get("material_id") or "").strip()
    ]
    projected_mesh_ids = sorted({
        mesh_id
        for group in projected_groups
        for mesh_id in (group.get("mesh_ids") or [])
    }, key=lambda value: str(value))
    projected_name = projected_names[0] if len(projected_names) == 1 else None
    projected_id = projected_ids[0] if len(projected_ids) == 1 else None
    for field in ("material", "material_name"):
        if field in payload:
            payload[field] = projected_name
    if "material_id" in payload:
        payload["material_id"] = projected_id
    if "mesh_ids" in payload:
        payload["mesh_ids"] = projected_mesh_ids

    adoption = payload.get("source_material_adoption")
    if isinstance(adoption, dict):
        adoption_name = str(adoption.get("material_name") or "").casefold()
        adoption_id = str(adoption.get("material_id") or "").strip()
        if (
            projected_name is None
            or adoption_name != projected_name.casefold()
            or (
                adoption_id
                and projected_id is not None
                and adoption_id != str(projected_id)
            )
        ):
            payload["source_material_adoption"] = None
    # These receipts seal the original full Provider output.  Once claims are
    # projected they cannot be treated as a seal for the reduced payload.
    for field in (
        "normalized_prototype_receipt",
        "physical_capture_contract",
        "physical_capture_receipt",
        "unit_probe_contract",
    ):
        payload.pop(field, None)

    current_bindings, explicit_ownership = _current_generator_bindings(payload)
    connection = payload.get("generator_connection")
    kept_binding_count = 0
    if isinstance(connection, dict) or explicit_ownership:
        kept_bindings = []
        for binding in current_bindings:
            if not isinstance(binding, dict):
                continue
            if (
                not explicit_ownership
                and "target_material_id" not in binding
                and "target_mesh_id" not in binding
            ):
                slot = str(
                    binding.get("slot_prefix") or ""
                ).strip().casefold()
                guid = str(
                    binding.get("generator_guid") or ""
                ).strip().casefold()
                index = str(binding.get("generator_index") or "").strip()
                name = str(
                    binding.get("generator_name") or ""
                ).strip().casefold()
                owner = (
                    f"guid:{guid}"
                    if guid
                    else f"index:{index}"
                    if index
                    else f"name:{name}"
                    if name
                    else ""
                )
                key = f"generator:{owner}:slot:{slot}" if owner and slot else ""
            else:
                projection = _generator_binding_claim_projection(
                    binding,
                    require_guid=explicit_ownership,
                )
                identity = (projection or {}).get("generator_identity") or {}
                slot = str((projection or {}).get("slot_prefix") or "")
                key = (
                    f"generator:{identity.get('kind')}:{identity.get('value')}"
                    f":slot:{slot}"
                    if identity and slot
                    else ""
                )
            if key and key in allowed_keys and key not in disagreement_keys:
                kept_bindings.append(binding)
        kept_binding_count = len(kept_bindings)
        if explicit_ownership:
            ownership = payload["generator_binding_ownership"]
            ownership["bindings"] = kept_bindings
            ownership["binding_count"] = kept_binding_count
            ownership["fingerprint"] = (
                generator_binding_ownership_fingerprint(kept_bindings)
            )
        else:
            connection["bindings"] = kept_bindings

    allowed_claims = {
        key: value for key, value in claims.items() if key in allowed_keys
    }
    if not allowed_claims or not (
        kept_group_count
        or kept_binding_count
        or any(key.startswith("record:") for key in allowed_claims)
    ):
        return None
    projected = copy.deepcopy(candidate)
    projected.update({
        "reason": "diagnostic_disjoint_provider_claims",
        "payload": payload,
        "ownership_claims": sorted(allowed_claims),
        "diagnostic_excluded_claims": sorted(set(claims) - set(allowed_claims)),
        "_claims": allowed_claims,
    })
    return projected


def _public_record(record, *, include_payload=True):
    public = {
        key: value
        for key, value in record.items()
        if not key.startswith("_")
    }
    if not include_payload:
        public.pop("payload", None)
    return public


def resolution_evidence(resolution):
    """Return JSON-safe path/reason evidence without embedding payload copies."""
    evidence = {
        "schema_version": resolution["schema_version"],
        "contract": resolution["contract"],
        "target_spm": resolution["target_spm"],
        "selected": [
            _public_record(row, include_payload=False)
            for row in resolution["selected"]
        ],
        "rejected": [
            _public_record(row, include_payload=False)
            for row in resolution["rejected"]
        ],
        "shadowed": [
            _public_record(row, include_payload=False)
            for row in resolution["shadowed"]
        ],
        "conflicting": [
            _public_record(row, include_payload=False)
            for row in resolution["conflicting"]
        ],
        "missing": list(resolution["missing"]),
    }
    if resolution.get("cluster_pair_identity"):
        evidence["cluster_pair_identity"] = copy.deepcopy(
            resolution["cluster_pair_identity"]
        )
    for field in (
        "diagnostic_only",
        "mutation_authorized",
        "diagnostic_status",
    ):
        if field in resolution:
            evidence[field] = copy.deepcopy(resolution[field])
    return evidence


CLUSTER_PAIR_RECEIPT_KIND = "cluster_spm_output_name_normalization"


def proven_cluster_pair_identity(target_spm):
    """Return the receipt-proven other name of one Cluster SPM, or ``None``.

    ``SK_<name>.spm`` and ``<name>.spm`` are two names for one Cluster output,
    but the naming convention alone is not evidence.  The normalization
    receipt is: it records both paths, one ``pair_id``, and the invariants
    proving the canonical copy is byte-identical and authoritative.  Only a
    complete receipt makes an Atlas record written against one name readable
    as a record for the other.
    """

    target = Path(target_spm).expanduser().resolve(strict=False)
    try:
        pair = resolve_cluster_spm_pair(target)
    except ClusterSpmPairError:
        return None
    canonical = Path(pair["canonical_spm"])
    mirror = Path(pair["mirror_spm"])
    counterpart = mirror if pair["input_role"] == "canonical" else canonical
    if normalized_manifest_path(counterpart) == normalized_manifest_path(target):
        return None
    payload, read_error = _read_payload(pair["receipt_path"])
    if read_error or not isinstance(payload, dict):
        return None
    invariants = payload.get("invariants") or {}
    paths = payload.get("paths") or {}
    proven = (
        payload.get("receipt_kind") == CLUSTER_PAIR_RECEIPT_KIND
        and payload.get("status") == "complete"
        and payload.get("pair_id") == pair["pair_id"]
        and invariants.get("after_content_equal") is True
        and invariants.get("canonical_output_authoritative") is True
        and normalized_manifest_path(paths.get("canonical_output") or "")
        == normalized_manifest_path(canonical)
        and normalized_manifest_path(paths.get("legacy_unprefixed_input") or "")
        == normalized_manifest_path(mirror)
    )
    if not proven:
        return None
    return {
        "pair_id": pair["pair_id"],
        "input_role": pair["input_role"],
        "counterpart_spm": counterpart,
        "receipt_path": Path(pair["receipt_path"]),
    }


def _target_stems(target, pair_identity=None):
    """Return every SPM stem that names this exact target."""
    stems = [target.stem]
    if pair_identity:
        counterpart_stem = Path(pair_identity["counterpart_spm"]).stem
        if counterpart_stem.casefold() != target.stem.casefold():
            stems.append(counterpart_stem)
    return stems


def _target_scope_paths(scope_dir, target, pair_identity=None):
    paths = {}
    for stem in _target_stems(target, pair_identity):
        for path in scope_dir.glob(f"*__{stem}.json"):
            if path.is_file():
                paths[normalized_manifest_path(path)] = path
    return paths


_ISOLATION_DIR_NAME = ".sk_batch_isolated_bark"


def _manifest_owner_target(target):
    """Production target that owns Atlas manifests for this queried SPM.

    Bark isolation copies the SPM into ``<production dir>/.sk_batch_isolated_bark/
    <hash>/<mirrored suffix>/``.  The mirrored copy never carries the
    ``.atlas_leaf_speedtree_targets`` / ``.atlas_leaf_speedtree_scopes``
    receipts or pair-normalization receipt.  The isolation root's parent owns
    the production file with the same basename, so manifest identity and
    relative paths must be resolved in that coordinate system.  Non-isolated
    targets are unaffected.
    """
    parts = [part.casefold() for part in target.parts]
    try:
        index = parts.index(_ISOLATION_DIR_NAME)
    except ValueError:
        return target
    production_dir = Path(*target.parts[:index])
    production_target = production_dir / target.name
    return production_target if production_dir.is_dir() else target


def _manifest_search_dir(target):
    return _manifest_owner_target(target).parent


def _candidate_specs(target, pair_identity=None):
    search_dir = _manifest_search_dir(target)
    target_dir = search_dir / ".atlas_leaf_speedtree_targets"
    scope_dir = search_dir / ".atlas_leaf_speedtree_scopes"
    specs = []
    if target_dir.is_dir():
        specs.extend(
            (path, "exact_per_target")
            for path in sorted(target_dir.glob("*.json"))
            if path.is_file()
        )
    if scope_dir.is_dir():
        exact = _target_scope_paths(scope_dir, target, pair_identity)
        specs.extend(
            (path, "exact_target_scope")
            for _key, path in sorted(exact.items())
        )
    global_path = search_dir / "speedtree_import_manifest.json"
    if global_path.is_file():
        specs.append((global_path, "exact_global_target"))
    return specs


def _diagnostic_specs(target, pair_identity=None):
    search_dir = _manifest_search_dir(target)
    scope_dir = search_dir / ".atlas_leaf_speedtree_scopes"
    exact_keys = set()
    if scope_dir.is_dir():
        exact_keys = set(
            _target_scope_paths(scope_dir, target, pair_identity)
        )
    rows = []
    if scope_dir.is_dir():
        rows.extend(
            (path, "scope_identity_shadow")
            for path in sorted(scope_dir.glob("*.json"))
            if path.is_file()
            and normalized_manifest_path(path) not in exact_keys
        )
    rows.extend(
        (path, "legacy_material_manifest")
        for path in sorted(search_dir.glob("speedtree_import_manifest_M_*.json"))
        if path.is_file()
    )
    return rows


def _identity_mismatch(source_identity, *, blend, source_collection, export_scope_id):
    if blend:
        if not source_identity["blend_file"]:
            return "missing_blend_identity"
        if source_identity["blend_file"] != normalized_manifest_path(blend):
            return "foreign_blend_identity"
    if source_collection:
        actual = source_identity["source_collection"]
        if not actual:
            return "missing_source_collection_identity"
        if actual != str(source_collection).strip().casefold():
            return "foreign_source_collection_identity"
    if export_scope_id:
        actual = source_identity["export_scope_id"]
        if not actual:
            return "missing_export_scope_identity"
        if actual != str(export_scope_id).strip().casefold():
            return "foreign_export_scope_identity"
    return None


def resolve_atlas_manifests(
    target_spm,
    *,
    expected_blend=None,
    expected_source_collection=None,
    expected_export_scope_id=None,
    require_generator_complete=False,
    diagnostic_only=False,
    include_shadow_diagnostics=True,
):
    """Resolve exact Atlas records for one SPM and return auditable evidence.

    The function is read-only.  Its default remains strict for mutation writers:
    invalid JSON, unsupported schemas, and contradictory operational mirrors
    fail closed.  A read-only live audit may opt into ``diagnostic_only``.  In
    that mode the same records remain visible as diagnostics, but deterministic
    valid winners are returned so metadata cannot erase the live Assembly graph.
    Foreign-target and caller-filtered records are rejected with reasons. The
    default also records every diagnostic legacy/scope shadow. Selected-only
    read-only consumers may disable that unrelated inventory without changing
    operational candidate resolution or conflict handling.
    """
    target = Path(target_spm).expanduser().resolve(strict=False)
    owner_target = _manifest_owner_target(target)
    owner_parent = owner_target.parent
    target_key = normalized_manifest_path(owner_target)
    pair_identity = proven_cluster_pair_identity(owner_target)
    identity_keys = {target_key}
    legacy_identity_key = None
    if pair_identity:
        counterpart_key = normalized_manifest_path(
            pair_identity["counterpart_spm"]
        )
        identity_keys.add(counterpart_key)
        legacy_identity_key = (
            counterpart_key if pair_identity["input_role"] == "canonical"
            else target_key
        )
    resolution = {
        "schema_version": RESOLUTION_SCHEMA_VERSION,
        "contract": "atlas_speedtree_manifest_resolution_v1",
        "target_spm": str(target),
        "selected": [],
        "rejected": [],
        "shadowed": [],
        "conflicting": [],
        "missing": [],
    }
    if pair_identity:
        resolution["cluster_pair_identity"] = {
            "pair_id": pair_identity["pair_id"],
            "counterpart_spm": str(pair_identity["counterpart_spm"]),
            "receipt_path": str(pair_identity["receipt_path"]),
        }

    target_dir = owner_parent / ".atlas_leaf_speedtree_targets"
    target_stems = _target_stems(owner_target, pair_identity)
    if not any((target_dir / f"{stem}.json").is_file() for stem in target_stems):
        resolution["missing"].append({
            "path": str(target_dir / f"{owner_target.stem}.json"),
            "reason": "candidate_file_missing",
        })
    global_path = owner_parent / "speedtree_import_manifest.json"
    if not global_path.is_file():
        resolution["missing"].append({
            "path": str(global_path),
            "reason": "candidate_file_missing",
        })
    scope_dir = owner_parent / ".atlas_leaf_speedtree_scopes"
    if not scope_dir.is_dir() or not _target_scope_paths(
        scope_dir, owner_target, pair_identity
    ):
        resolution["missing"].append({
            "path": str(scope_dir / f"*__{owner_target.stem}.json"),
            "reason": "target_scope_candidate_missing",
        })

    valid = []
    fatal = []
    for path, kind in _candidate_specs(owner_target, pair_identity):
        payload, read_error = _read_payload(path)
        base = {
            "path": str(path.resolve(strict=False)),
            "kind": kind,
            "precedence": KIND_PRECEDENCE[kind],
        }
        if read_error:
            row = {**base, "reason": read_error}
            resolution["conflicting"].append(row)
            fatal.append(row)
            continue

        declared_spm = str(payload.get("spm") or "").strip()
        if not declared_spm:
            resolution["rejected"].append({
                **base,
                "reason": "missing_spm_identity",
            })
            continue
        declared_key = normalized_manifest_path(
            declared_spm,
            relative_to=owner_parent,
        )
        if declared_key not in identity_keys:
            resolution["rejected"].append({
                **base,
                "reason": "different_target_spm",
                "declared_spm": declared_spm,
            })
            continue
        identity_rank = CANONICAL_IDENTITY_RANK
        if declared_key == legacy_identity_key:
            # The record names this exact output under its legacy unprefixed
            # name.  The normalization receipt already proved both names hold
            # the same bytes, so this is the target's own record -- but the
            # canonical name is the production identity, so a legacy-named
            # record never outranks one written against the canonical name.
            identity_rank = LEGACY_IDENTITY_RANK
            base = {
                **base,
                "declared_spm": declared_spm,
                "identity_match": "cluster_spm_pair_legacy_name",
                "cluster_pair_receipt": str(
                    (pair_identity or {}).get("receipt_path", "")
                ),
            }
        lifecycle = payload.get("atlas_scope_lifecycle") or {}
        if lifecycle.get("state") == "retired":
            resolution["shadowed"].append({
                **base,
                "reason": "retired_scope_record",
            })
            continue
        schema = _schema_contract(payload)
        if schema["status"] != "supported":
            row = {
                **base,
                "reason": schema["reason"],
                "schema": schema,
            }
            resolution["conflicting"].append(row)
            fatal.append(row)
            continue
        source_identity = _source_identity(
            payload,
            path,
            target_parent=owner_parent,
        )
        mismatch = _identity_mismatch(
            source_identity,
            blend=expected_blend,
            source_collection=expected_source_collection,
            export_scope_id=expected_export_scope_id,
        )
        if mismatch:
            resolution["rejected"].append({
                **base,
                "reason": mismatch,
                "source_identity": source_identity,
            })
            continue
        if (
            require_generator_complete
            and (payload.get("generator_connection") or {}).get("complete")
            is not True
        ):
            resolution["rejected"].append({
                **base,
                "reason": "generator_connection_incomplete",
                "source_identity": source_identity,
            })
            continue
        try:
            claims = _candidate_claims(payload, source_identity, kind)
        except _GeneratorContractError as exc:
            row = {
                **base,
                "reason": exc.reason,
                "source_identity": source_identity,
                "contract_error": copy.deepcopy(exc.details),
            }
            resolution["conflicting"].append(row)
            fatal.append(row)
            continue
        valid.append({
            **base,
            "reason": "operational_candidate",
            "schema": schema,
            "source_identity": source_identity,
            "ownership_claims": sorted(claims),
            "payload": payload,
            "_claims": claims,
            "_identity_rank": identity_rank,
        })

    if include_shadow_diagnostics:
        for path, kind in _diagnostic_specs(owner_target, pair_identity):
            payload, read_error = _read_payload(path)
            row = {
                "path": str(path.resolve(strict=False)),
                "kind": kind,
                "reason": (
                    read_error
                    or "diagnostic_only_legacy_shadow"
                    if kind == "legacy_material_manifest"
                    else read_error or "diagnostic_only_scope_identity_shadow"
                ),
            }
            if payload is not None:
                row["declared_spm"] = str(payload.get("spm") or "")
                row["source_identity"] = _source_identity(
                    payload,
                    path,
                    target_parent=owner_parent,
                )
                try:
                    row["ownership_claims"] = sorted(
                        _candidate_claims(
                            payload,
                            row["source_identity"],
                            kind,
                        )
                    )
                except _GeneratorContractError as exc:
                    row["ownership_claims"] = []
                    row["ownership_contract_error"] = {
                        "reason": exc.reason,
                        **copy.deepcopy(exc.details),
                    }
            resolution["shadowed"].append(row)

    if fatal and not diagnostic_only:
        evidence = resolution_evidence(resolution)
        raise AtlasManifestResolutionError(
            "Atlas manifest resolution failed for "
            f"{target.name}: {fatal[0]['reason']} at {fatal[0]['path']}",
            evidence,
        )

    winners = {}
    selected = []
    for candidate in sorted(
        valid,
        key=lambda row: (
            row["_identity_rank"],
            row["precedence"],
            row["path"].casefold(),
        ),
    ):
        claims = candidate["_claims"]
        overlaps = [
            (key, winners[key]) for key in sorted(claims) if key in winners
        ]
        disagreements = [
            (key, winner)
            for key, winner in overlaps
            if claims[key] != winner["_claims"][key]
        ]
        if disagreements:
            # The per-target record is the publication authority for this exact
            # provider/scope.  Once a new target record exists, an older scope
            # or rolling-global mirror from the same provider must not remain a
            # second operational candidate and resurrect superseded Material /
            # Mesh bindings.  Other providers remain independent candidates;
            # this only shadows a lower-precedence mirror of the same source.
            superseded_same_source = next(
                (
                    winner
                    for _key, winner in disagreements
                    if winner.get("kind") == "exact_per_target"
                    and winner.get("precedence", 99)
                    < candidate.get("precedence", 99)
                    and winner.get("source_identity")
                    == candidate.get("source_identity")
                    and normalized_manifest_path(
                        (candidate.get("payload") or {}).get(
                            "target_manifest"
                        ) or ""
                    )
                    == normalized_manifest_path(winner.get("path") or "")
                ),
                None,
            )
            if superseded_same_source is not None:
                resolution["shadowed"].append({
                    "path": candidate["path"],
                    "kind": candidate["kind"],
                    "precedence": candidate["precedence"],
                    "reason": "superseded_same_source_mirror",
                    "superseded_by": superseded_same_source["path"],
                    "source_identity": candidate.get("source_identity"),
                })
                continue
            # A record written against the legacy unprefixed name loses to one
            # written against the canonical output name: the pair contract
            # makes the canonical name the production identity, so the legacy
            # record is a superseded rename artifact, not a live disagreement.
            superseded_by = next(
                (
                    winner for _key, winner in disagreements
                    if winner["_identity_rank"] < candidate["_identity_rank"]
                ),
                None,
            )
            if superseded_by is not None:
                resolution["shadowed"].append({
                    "path": candidate["path"],
                    "kind": candidate["kind"],
                    "precedence": candidate["precedence"],
                    "reason": "superseded_legacy_name_record",
                    "declared_spm": candidate.get("declared_spm", ""),
                    "superseded_by": superseded_by["path"],
                })
                continue
            for key, winner in disagreements:
                resolution["conflicting"].append({
                    "path": candidate["path"],
                    "kind": candidate["kind"],
                    "precedence": candidate["precedence"],
                    "reason": "operational_candidate_disagreement",
                    "ownership_claim": key,
                    "conflicts_with": winner["path"],
                    "conflicts_with_kind": winner["kind"],
                    "conflicts_with_precedence": winner["precedence"],
                })
            if diagnostic_only:
                projected = _diagnostic_disjoint_candidate(
                    candidate,
                    winners,
                    {key for key, _winner in disagreements},
                )
                if projected is not None:
                    selected.append(projected)
                    for key in projected["_claims"]:
                        winners.setdefault(key, projected)
            continue

        candidate["reason"] = (
            "coherent_operational_mirror" if overlaps else "selected_authority"
        )
        selected.append(candidate)
        for key in claims:
            winners.setdefault(key, candidate)

    if resolution["conflicting"]:
        resolution["selected"] = [
            _public_record(row) for row in selected
        ]
        if diagnostic_only:
            resolution.update({
                "diagnostic_only": True,
                "mutation_authorized": False,
                "diagnostic_status": "atlas_manifest_metadata_disagreement",
            })
            return resolution
        evidence = resolution_evidence(resolution)
        first = resolution["conflicting"][0]
        raise AtlasManifestResolutionError(
            "Atlas manifest ownership conflict for "
            f"{target.name}: {first['reason']} at {first['path']}",
            evidence,
        )

    resolution["selected"] = [_public_record(row) for row in selected]
    return resolution


_MIRROR_OWNERSHIP_FIELDS = (
    "blend_file",
    "source_collection",
    "export_scope_id",
    "material_groups",
    "speedtree_material_groups",
    "generator_connection",
    "generator_binding_ownership",
    "generator_slot_creation_provenance",
    "collection_tombstone",
)


def _scope_file_stem(scope_id):
    """Return the scope filename component every reader derives the same way."""
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(scope_id or "")).strip("._")
    return safe or "AtlasLeaf"


def canonical_name_publication_plan(target_spm, resolution=None):
    """Return the canonical-name records a legacy-named export never wrote.

    The resolver reads a Cluster output's legacy-named records, but every
    downstream reader that is not the resolver -- the Blender push gate above
    all -- looks the manifest up by the canonical file's own stem.  With only
    legacy-named records on disk that lookup misses and falls back to the
    unsuffixed identity shadow, which is diagnostic-only and last-writer-wins.

    So the canonical name has to exist on disk as well.  This is a rename
    completion, not an authoring decision: the payload is copied verbatim from
    the record the resolver already selected, with only ``spm`` rebound.
    """

    target = Path(target_spm).expanduser().resolve(strict=False)
    if resolution is None:
        try:
            resolution = resolve_atlas_manifests(target)
        except AtlasManifestResolutionError:
            return []
    pair = resolution.get("cluster_pair_identity")
    if not pair:
        return []
    canonical = Path(resolution["target_spm"])
    # The pair contract forbids publishing to the legacy name, so only a
    # canonical target may gain records; a legacy-named query never writes.
    if not canonical.name.casefold().startswith("sk_"):
        return []
    publications = []
    for row in resolution.get("selected") or ():
        if row.get("identity_match") != "cluster_spm_pair_legacy_name":
            continue
        kind = row.get("kind")
        payload = row.get("payload")
        if not isinstance(payload, dict):
            payload, read_error = _read_payload(row.get("path"))
            if read_error or payload is None:
                continue
        if kind == "exact_per_target":
            destination = (
                canonical.parent
                / ".atlas_leaf_speedtree_targets"
                / f"{canonical.stem}.json"
            )
        elif kind == "exact_target_scope":
            scope_id = str(payload.get("export_scope_id") or "").strip()
            if not scope_id:
                continue
            destination = (
                canonical.parent
                / ".atlas_leaf_speedtree_scopes"
                / f"{_scope_file_stem(scope_id)}__{canonical.stem}.json"
            )
        else:
            # The rolling global record is shared by every target in the
            # folder; completing one rename must not claim it.
            continue
        if destination.is_file():
            continue
        published = copy.deepcopy(payload)
        published["spm"] = str(canonical)
        publications.append({
            "kind": kind,
            "source": str(Path(row["path"]).resolve(strict=False)),
            "destination": str(destination),
            "payload": published,
        })
    return publications


def atlas_manifest_mirror_repair_plan(target_spm, resolution=None):
    """Classify whether one conflict is a stale mirror, without mutating it.

    Automatic repair is intentionally narrow.  One exact-per-target authority
    must exist, every conflicting record must be lower precedence, and both
    its source identity and complete ownership-claim key set must match the
    authority.  A different source/scope or extra claim is authoring ambiguity,
    not a stale mirror.

    A resolvable target can still be incomplete: when its records exist only
    under the legacy unprefixed name, the canonical-name copies every other
    reader looks for are missing, and that is repairable too.
    """

    target = Path(target_spm).expanduser().resolve(strict=False)
    if resolution is None:
        try:
            current = resolve_atlas_manifests(target)
        except AtlasManifestResolutionError as exc:
            resolution = exc.resolution
        else:
            publications = canonical_name_publication_plan(target, current)
            if publications:
                return {
                    "schema_version": 1,
                    "status": "repairable",
                    "reason_code": "atlas_manifest_canonical_name_missing",
                    "target_spm": str(target),
                    "authority": None,
                    "mirrors": [],
                    "publications": [
                        {
                            key: value for key, value in row.items()
                            if key != "payload"
                        }
                        for row in publications
                    ],
                    "resolution": resolution_evidence(current),
                }
            return {
                "schema_version": 1,
                "status": "not_needed",
                "reason_code": "atlas_manifest_current",
                "target_spm": str(target),
                "authority": None,
                "mirrors": [],
            }
    resolution = copy.deepcopy(dict(resolution or {}))
    conflicts = list(resolution.get("conflicting") or ())
    authorities = []
    for raw_row in resolution.get("selected") or ():
        if raw_row.get("kind") != "exact_per_target":
            continue
        row = copy.deepcopy(raw_row)
        payload = row.get("payload")
        if not isinstance(payload, dict):
            payload, read_error = _read_payload(row.get("path"))
            if read_error or payload is None:
                continue
            row["payload"] = payload
        authorities.append(row)

    def unrepairable(reason):
        return {
            "schema_version": 1,
            "status": "unrepairable",
            "reason_code": "atlas_manifest_ownership_conflict",
            "reason": str(reason),
            "target_spm": str(target),
            "authority": None,
            "mirrors": [],
            "resolution": resolution_evidence(resolution),
        }

    if len(authorities) != 1:
        return unrepairable("exactly one exact-per-target authority is required")
    if not conflicts or any(
        row.get("reason") != "operational_candidate_disagreement"
        for row in conflicts
    ):
        return unrepairable("conflict is not a coherent operational mirror drift")
    authority = authorities[0]
    authority_path = normalized_manifest_path(authority["path"])
    authority_payload = authority["payload"]
    authority_identity = _source_identity(
        authority_payload,
        authority["path"],
        target_parent=target.parent,
    )
    authority_claims = _candidate_claims(
        authority_payload,
        authority_identity,
        authority["kind"],
    )
    mirror_paths = []
    for row in conflicts:
        if normalized_manifest_path(row.get("conflicts_with")) != authority_path:
            return unrepairable("conflict winner is not the exact-per-target authority")
        if int(row.get("precedence", -1)) <= int(authority.get("precedence", 0)):
            return unrepairable("conflicting record is not a lower-precedence mirror")
        path = Path(str(row.get("path") or ""))
        key = normalized_manifest_path(path)
        if key in {normalized_manifest_path(value) for value in mirror_paths}:
            continue
        payload, read_error = _read_payload(path)
        if read_error or payload is None:
            return unrepairable(read_error or "mirror payload is unavailable")
        identity = _source_identity(payload, path, target_parent=target.parent)
        if identity != authority_identity:
            return unrepairable("conflicting mirror has a different source identity")
        claims = _candidate_claims(payload, identity, str(row.get("kind") or ""))
        if set(claims) != set(authority_claims):
            return unrepairable("conflicting mirror has a different ownership claim set")
        mirror_paths.append(path)
    if not mirror_paths:
        return unrepairable("no lower-precedence mirror is repairable")
    return {
        "schema_version": 1,
        "status": "repairable",
        "reason_code": "atlas_manifest_mirror_conflict_repairable",
        "target_spm": str(target),
        "authority": str(Path(authority["path"]).resolve(strict=False)),
        "mirrors": [str(path.resolve(strict=False)) for path in mirror_paths],
        "source_identity": authority_identity,
        "ownership_claims": sorted(authority_claims),
        "resolution": resolution_evidence(resolution),
    }


def _atomic_manifest_write_bytes(path, encoded):
    destination = Path(path)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=str(destination.parent)
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _atomic_manifest_write(path, payload):
    encoded = (
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    _atomic_manifest_write_bytes(path, encoded)


def _publish_canonical_name_records(target, plan):
    """Write the canonical-name copies, or leave the folder untouched."""

    publications = canonical_name_publication_plan(target)
    if not publications:
        # Another process completed the rename between plan and repair.
        return {**plan, "status": "not_needed", "published": []}
    committed = []
    try:
        for row in publications:
            destination = Path(row["destination"])
            if destination.exists():
                raise OSError(
                    f"canonical Atlas record already exists: {destination}"
                )
            destination.parent.mkdir(parents=True, exist_ok=True)
            _atomic_manifest_write(destination, row["payload"])
            committed.append(destination)
        verified = resolve_atlas_manifests(target)
    except Exception:
        for path in reversed(committed):
            try:
                path.unlink()
            except OSError:
                pass
        raise
    return {
        **plan,
        "status": "repaired",
        "published": [str(path) for path in committed],
        "verified_resolution": resolution_evidence(verified),
    }


def repair_atlas_manifest_mirrors(target_spm):
    """Repair only a freshly proven exact-authority/lower-mirror conflict."""

    target = Path(target_spm).expanduser().resolve(strict=False)
    plan = atlas_manifest_mirror_repair_plan(target)
    if plan.get("status") == "not_needed":
        return plan
    if plan.get("reason_code") == "atlas_manifest_canonical_name_missing":
        return _publish_canonical_name_records(target, plan)
    if plan.get("status") != "repairable":
        raise AtlasManifestResolutionError(
            str(plan.get("reason") or "Atlas manifest conflict is not repairable"),
            plan.get("resolution") or {},
        )
    authority_payload, read_error = _read_payload(plan["authority"])
    if read_error or authority_payload is None:
        raise AtlasManifestResolutionError(
            read_error or "Atlas manifest authority is unavailable",
            plan.get("resolution") or {},
        )
    originals = {}
    committed = []
    try:
        for value in plan["mirrors"]:
            path = Path(value)
            payload, read_error = _read_payload(path)
            if read_error or payload is None:
                raise OSError(read_error or "mirror payload is unavailable")
            originals[path] = path.read_bytes()
            repaired = copy.deepcopy(payload)
            for field in _MIRROR_OWNERSHIP_FIELDS:
                if field in authority_payload:
                    repaired[field] = copy.deepcopy(authority_payload[field])
                else:
                    repaired.pop(field, None)
            _atomic_manifest_write(path, repaired)
            committed.append(path)
        verified = resolve_atlas_manifests(target)
    except Exception:
        for path in reversed(committed):
            try:
                _atomic_manifest_write_bytes(path, originals[path])
            except OSError:
                pass
        raise
    return {
        **plan,
        "status": "repaired",
        "verified_resolution": resolution_evidence(verified),
    }


def selected_manifest_payload(resolution):
    """Return the highest-precedence selected payload, or an empty mapping."""
    selected = resolution.get("selected") or []
    return dict(selected[0].get("payload") or {}) if selected else {}


def _integer_text(value):
    try:
        return str(int(str(value).strip()))
    except (TypeError, ValueError):
        return ""


def _group_mesh_ids(group):
    values = group.get("mesh_ids") or []
    if not values:
        adoption = group.get("source_material_adoption") or {}
        values = adoption.get("final_material_mesh_ids") or []
    return sorted({
        value for value in (_integer_text(item) for item in values) if value
    })


def _source_path_values(payload, group):
    """Return the authoritative source paths carried by one material group."""
    mappings = []
    for field, child in (
        ("files", group.get("blender_cluster_bake_texture")),
        ("files", group.get("canonical_texture_output")),
        ("source_paths", group.get("source_texture_fallback")),
    ):
        if isinstance(child, dict) and isinstance(child.get(field), dict):
            mappings.append(child[field])
    groups = _material_groups(payload)
    if len(groups) == 1 and isinstance(payload.get("source_textures"), dict):
        mappings.append(payload["source_textures"])
    values = []
    for mapping in mappings:
        values.extend(
            str(value).strip()
            for value in mapping.values()
            if str(value or "").strip()
        )
    return values


def resolve_manifest_material_ownership(
    resolution,
    live_materials,
    *,
    target_spm=None,
):
    """Prove exact material/mesh/source ownership from the selected set.

    The resolver remains the sole candidate-selection authority.  This helper
    adds read-only live evidence after selection, so a rolling per-target copy
    cannot erase other legitimate providers and a marker alone cannot bless a
    material whose ID, cutout meshes, or connected source files disagree.
    """
    target = Path(
        target_spm or resolution.get("target_spm") or ""
    ).expanduser().resolve(strict=False)
    selected_groups = []
    for selected in resolution.get("selected") or []:
        payload = selected.get("payload") or {}
        for group in _material_groups(payload):
            selected_groups.append((selected, payload, group))

    proven = []
    unproven = []
    for material in live_materials or []:
        name = str(
            material.get("material_name") or material.get("name") or ""
        ).strip()
        material_id = _integer_text(
            material.get("material_id")
            if material.get("material_id") is not None
            else material.get("id")
        )
        mesh_ids = sorted({
            value
            for value in (
                _integer_text(item)
                for item in (
                    material.get("cutout_mesh_ids")
                    or material.get("mesh_ids")
                    or []
                )
            )
            if value
        })
        refs = material.get("refs") or material.get("source_refs") or []
        if isinstance(refs, dict):
            refs = refs.values()
        live_signature = sorted({
            normalized_manifest_path(value, relative_to=target.parent)
            for value in refs
            if str(value or "").strip()
        })

        candidates = []
        mismatch_reasons = set()
        for selected, payload, group in selected_groups:
            if str(group.get("material") or "").strip().casefold() != name.casefold():
                continue
            if _integer_text(group.get("material_id")) != material_id:
                mismatch_reasons.add("material_id_mismatch")
                continue
            if _group_mesh_ids(group) != mesh_ids:
                mismatch_reasons.add("material_mesh_set_mismatch")
                continue
            declared_signature = sorted({
                normalized_manifest_path(value, relative_to=target.parent)
                for value in _source_path_values(payload, group)
            })
            if not live_signature or not declared_signature:
                mismatch_reasons.add("source_signature_missing")
                continue
            if declared_signature != live_signature:
                mismatch_reasons.add("source_signature_mismatch")
                continue
            candidates.append({
                "material_name": name,
                "material_id": material_id,
                "mesh_ids": mesh_ids,
                "source_signature": live_signature,
                "manifest_path": selected.get("path", ""),
                "manifest_kind": selected.get("kind", ""),
                "manifest_precedence": selected.get("precedence"),
                "source_identity": copy.deepcopy(
                    selected.get("source_identity") or {}
                ),
                "material_group": copy.deepcopy(group),
            })
        if candidates:
            candidates.sort(key=lambda row: (
                int(row.get("manifest_precedence") or 0),
                str(row.get("manifest_path") or "").casefold(),
            ))
            proven.append(candidates[0])
        else:
            unproven.append({
                "material_name": name,
                "material_id": material_id,
                "mesh_ids": mesh_ids,
                "source_signature": live_signature,
                "reasons": sorted(mismatch_reasons) or [
                    "matching_manifest_material_group_missing"
                ],
            })

    if not live_materials:
        status = "not_applicable"
    elif unproven:
        status = "unproven"
    else:
        status = "proven"
    return {
        "status": status,
        "materials": proven,
        "unproven": unproven,
    }


def _binding_identity_matches(declared, live):
    slot = str(declared.get("slot_prefix") or "").strip()
    if slot != str(live.get("slot_prefix") or "").strip():
        return False
    declared_guid = str(declared.get("generator_guid") or "").strip()
    if declared_guid:
        return declared_guid == str(
            live.get("generator_guid") or ""
        ).strip()
    for field in ("generator_index", "generator_name"):
        left = declared.get(field)
        right = live.get(field)
        if left not in {None, ""} and right not in {None, ""}:
            return str(left).strip().casefold() == str(right).strip().casefold()
    return False


def diagnose_manifest_generator_candidates(resolution, live_bindings):
    """Separate stale manifest declarations from live SPM asset defects."""
    live = [dict(row) for row in live_bindings or []]
    candidates = []
    conflicting = []
    for selected in resolution.get("selected") or []:
        payload = selected.get("payload") or {}
        connection = payload.get("generator_connection") or {}
        declared_bindings, explicit_ownership = _current_generator_bindings(
            payload
        )
        requested = connection.get("requested") is True
        row = {
            "path": selected.get("path", ""),
            "kind": selected.get("kind", ""),
            "precedence": selected.get("precedence"),
            "requested": requested,
            "declared_complete": connection.get("complete") is True,
            "status": "no_generator_claim",
            "reasons": [],
            "binding_results": [],
        }
        # The explicit block is authoritative independently of the legacy
        # request bit.  That bit remains relevant only to compatibility
        # receipts that do not yet carry current ownership.
        if not requested and not explicit_ownership:
            candidates.append(row)
            continue

        if not declared_bindings and requested and not explicit_ownership:
            row["reasons"].append("declared_generator_bindings_missing")
        for declared in declared_bindings:
            matches = [
                current for current in live
                if _binding_identity_matches(declared, current)
            ]
            errors = []
            current = matches[0] if len(matches) == 1 else None
            if current is None:
                errors.append(
                    "generator_slot_ambiguous" if len(matches) > 1
                    else "generator_slot_missing"
                )
            else:
                target_material_id = _integer_text(
                    declared.get("target_material_id")
                )
                target_mesh_id = _integer_text(declared.get("target_mesh_id"))
                if target_material_id and target_material_id != _integer_text(
                    current.get("material_id")
                ):
                    errors.append("target_material_mismatch")
                if target_mesh_id and target_mesh_id != _integer_text(
                    current.get("mesh_id")
                ):
                    errors.append("target_mesh_mismatch")
            row["binding_results"].append({
                "declared": copy.deepcopy(declared),
                "current": copy.deepcopy(current),
                "errors": errors,
            })
            row["reasons"].extend(errors)

        # Legacy receipts conflate the producer's complete asset inventory
        # with current Generator ownership. Preserve their old diagnostic
        # until they are migrated. An explicit ownership block deliberately
        # permits generated Material/Mesh variants to remain unbound.
        if not explicit_ownership:
            exporting_meshes = {}
            for current in live:
                if not current.get(
                    "export_participates", current.get("visible", True)
                ):
                    continue
                material_id = _integer_text(current.get("material_id"))
                mesh_id = _integer_text(current.get("mesh_id"))
                if material_id and mesh_id:
                    exporting_meshes.setdefault(material_id, set()).add(mesh_id)
            for group in _material_groups(payload):
                material_id = _integer_text(group.get("material_id"))
                declared_meshes = set(_group_mesh_ids(group))
                missing = sorted(
                    declared_meshes - exporting_meshes.get(material_id, set())
                )
                if missing:
                    row["reasons"].append(
                        "declared_mesh_not_export_participating"
                    )
                    row.setdefault(
                        "non_export_participating_mesh_ids", []
                    ).extend(missing)

        row["reasons"] = sorted(set(row["reasons"]))
        row["status"] = (
            "manifest_candidate_live_conflict"
            if row["reasons"] else "live_coherent"
        )
        candidates.append(row)
        if row["status"] == "manifest_candidate_live_conflict":
            conflicting.append(copy.deepcopy(row))
    return {
        "status": "conflicting" if conflicting else "coherent",
        "candidates": candidates,
        "conflicting": conflicting,
    }
