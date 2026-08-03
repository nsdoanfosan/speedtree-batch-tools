"""Fail-closed authored-versus-required-live Generator delivery scope.

The scope is authored upstream of the SpeedTree write.  Neither this module
nor its consumers may infer it from visibility, node counts, export survivors,
or any other observation of the current target document.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

INTENT_KIND = "speedtree_generator_delivery_scope_intent"
RESOLVED_KIND = "speedtree_generator_delivery_scope_resolved"
SCOPE_KIND = "speedtree_generator_delivery_scope"
SCHEMA_VERSION = 1
RUNTIME_INACTIVE_POLICY = "sealed_required_live"
CONTINUITY_ONLY_POLICY = "relationship_continuity_only"
MATERIAL_DEFAULT_MESH_ID = -10
POSTWRITE_MODE_EXACT_CURRENT = "exact_current"
POSTWRITE_MODE_HISTORICAL_PROOF = "historical_production_proof"
_SHA256_RE = re.compile(r"[0-9a-f]{64}")


class GeneratorDeliveryScopeError(ValueError):
    """An explicit scope is incomplete, foreign, or internally inconsistent."""


def canonical_sha256(value):
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def generator_guid_key(value):
    """Return an exact opaque SpeedTree GUID with outer whitespace removed."""

    return str(value or "").strip()


def _positive_id(value, label):
    if isinstance(value, bool):
        raise GeneratorDeliveryScopeError(f"{label} must be a positive integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise GeneratorDeliveryScopeError(
            f"{label} must be a positive integer"
        ) from exc
    if parsed <= 0:
        raise GeneratorDeliveryScopeError(f"{label} must be a positive integer")
    return parsed


def _target_mesh_id(value, label):
    if isinstance(value, bool):
        raise GeneratorDeliveryScopeError(
            f"{label} must be a positive integer or "
            f"{MATERIAL_DEFAULT_MESH_ID} (material default)"
        )
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise GeneratorDeliveryScopeError(
            f"{label} must be a positive integer or "
            f"{MATERIAL_DEFAULT_MESH_ID} (material default)"
        ) from exc
    if parsed != MATERIAL_DEFAULT_MESH_ID and parsed <= 0:
        raise GeneratorDeliveryScopeError(
            f"{label} must be a positive integer or "
            f"{MATERIAL_DEFAULT_MESH_ID} (material default)"
        )
    return parsed


def _nonempty_text(value, label):
    text = str(value or "").strip()
    if not text:
        raise GeneratorDeliveryScopeError(f"{label} must be non-empty")
    return text


def _path_key(value):
    return str(Path(value).expanduser().absolute()).casefold()


def canonical_slot_identity(value, *, strict=True):
    """Return the semantic Generator slot identity used by producer and audit."""
    if isinstance(value, dict):
        exact_prefix = str(
            value.get("slot_prefix")
            or str(value.get("material_property") or "").rsplit(":", 1)[0]
        ).strip()
        guid = generator_guid_key(value.get("generator_guid"))
        if guid and exact_prefix:
            return ("guid", guid, exact_prefix)
        identity = (
            "named",
            str(value.get("generator_type") or "").strip().casefold(),
            str(value.get("generator_name") or "").strip().casefold(),
            exact_prefix,
        )
    elif isinstance(value, (list, tuple)):
        exact = tuple(str(part or "").strip() for part in value)
        kind = exact[0].casefold() if exact else ""
        if kind == "guid" and len(exact) == 3:
            identity = (
                "guid",
                generator_guid_key(exact[1]),
                exact[2],
            )
        elif kind == "named" and len(exact) == 4:
            identity = (
                "named",
                exact[1].casefold(),
                exact[2].casefold(),
                exact[3],
            )
        else:
            identity = (kind, *exact[1:])
    else:
        raise GeneratorDeliveryScopeError("slot identity must be an object or list")
    if len(identity) not in {3, 4} or identity[0] not in {"guid", "named"}:
        raise GeneratorDeliveryScopeError("slot identity is incomplete")
    if identity[0] == "guid" and len(identity) != 3:
        raise GeneratorDeliveryScopeError("GUID slot identity has invalid arity")
    if identity[0] == "named" and len(identity) != 4:
        raise GeneratorDeliveryScopeError("named slot identity has invalid arity")
    if strict and any(not part for part in identity):
        raise GeneratorDeliveryScopeError("slot identity is incomplete")
    if not strict and not identity[-1]:
        raise GeneratorDeliveryScopeError("slot identity has no semantic prefix")
    return identity


def canonical_authored_slot(row):
    if not isinstance(row, dict):
        raise GeneratorDeliveryScopeError("authored slot must be an object")
    identity_source = row.get("slot_identity", row)
    return {
        "slot_identity": list(canonical_slot_identity(identity_source)),
        "target_material_id": _positive_id(
            row.get("target_material_id"),
            "authored slot target_material_id",
        ),
        "target_mesh_id": _target_mesh_id(
            row.get("target_mesh_id"),
            "authored slot target_mesh_id",
        ),
    }


def canonical_authored_slots(rows):
    if not isinstance(rows, list) or not rows:
        raise GeneratorDeliveryScopeError("authored_slots must be a non-empty list")
    normalized = [canonical_authored_slot(row) for row in rows]
    identities = [tuple(row["slot_identity"]) for row in normalized]
    if len(identities) != len(set(identities)):
        raise GeneratorDeliveryScopeError(
            "authored slot identities must be declared exactly once"
        )
    return sorted(
        normalized,
        key=lambda row: json.dumps(
            row["slot_identity"], ensure_ascii=False, separators=(",", ":")
        ),
    )


def _canonical_identity_list(rows, label):
    if not isinstance(rows, list):
        raise GeneratorDeliveryScopeError(f"{label} must be a list")
    normalized = [list(canonical_slot_identity(row)) for row in rows]
    keys = [tuple(row) for row in normalized]
    if len(keys) != len(set(keys)):
        raise GeneratorDeliveryScopeError(f"{label} contains duplicate identities")
    return sorted(normalized, key=lambda row: json.dumps(row, separators=(",", ":")))


def _canonical_continuity_rows(rows):
    if not isinstance(rows, list):
        raise GeneratorDeliveryScopeError("continuity_only_slots must be a list")
    normalized = []
    for row in rows:
        if not isinstance(row, dict):
            raise GeneratorDeliveryScopeError(
                "continuity-only declaration must be an object"
            )
        provenance = row.get("provenance")
        if not isinstance(provenance, dict) or not provenance:
            raise GeneratorDeliveryScopeError(
                "continuity-only provenance must be a non-empty object"
            )
        policy = _nonempty_text(row.get("policy"), "continuity-only policy")
        if policy != CONTINUITY_ONLY_POLICY:
            raise GeneratorDeliveryScopeError(
                "continuity-only policy is unsupported"
            )
        normalized.append({
            "slot_identity": list(canonical_slot_identity(row.get("slot_identity"))),
            "reason": _nonempty_text(row.get("reason"), "continuity-only reason"),
            "policy": policy,
            "provenance": provenance,
        })
    identities = [tuple(row["slot_identity"]) for row in normalized]
    if len(identities) != len(set(identities)):
        raise GeneratorDeliveryScopeError(
            "continuity_only_slots contains duplicate identities"
        )
    return sorted(
        normalized,
        key=lambda row: json.dumps(row["slot_identity"], separators=(",", ":")),
    )


def validate_delivery_scope_intent(
    intent,
    *,
    target_spm=None,
    material_id=None,
    provider_blend=None,
):
    if not isinstance(intent, dict):
        raise GeneratorDeliveryScopeError("delivery scope intent must be an object")
    if intent.get("kind") != INTENT_KIND or intent.get("schema_version") != 1:
        raise GeneratorDeliveryScopeError("delivery scope intent schema is unsupported")
    authority = intent.get("authority")
    if not isinstance(authority, dict):
        raise GeneratorDeliveryScopeError("delivery scope authority must be an object")
    _nonempty_text(authority.get("kind"), "delivery scope authority kind")
    _nonempty_text(authority.get("id"), "delivery scope authority id")
    provenance = authority.get("provenance")
    if not isinstance(provenance, dict) or not provenance:
        raise GeneratorDeliveryScopeError(
            "delivery scope authority provenance must be a non-empty object"
        )
    target = intent.get("target")
    if not isinstance(target, dict):
        raise GeneratorDeliveryScopeError("delivery scope target must be an object")
    target_path = _nonempty_text(target.get("spm"), "delivery scope target SPM")
    provider_path = _nonempty_text(
        target.get("provider_blend"), "delivery scope provider blend"
    )
    _nonempty_text(target.get("provider_scope_id"), "delivery provider scope ID")
    target_material_id = _positive_id(
        target.get("material_id"), "delivery scope target material_id"
    )
    if target_spm is not None and _path_key(target_path) != _path_key(target_spm):
        raise GeneratorDeliveryScopeError("delivery scope belongs to another target SPM")
    if material_id is not None and target_material_id != _positive_id(
        material_id, "expected material_id"
    ):
        raise GeneratorDeliveryScopeError("delivery scope belongs to another material")
    if provider_blend is not None and _path_key(provider_path) != _path_key(
        provider_blend
    ):
        raise GeneratorDeliveryScopeError(
            "delivery scope belongs to another provider blend"
        )
    if intent.get("runtime_inactive_policy") != RUNTIME_INACTIVE_POLICY:
        raise GeneratorDeliveryScopeError(
            "delivery scope runtime inactive policy is unsupported"
        )

    authored = canonical_authored_slots(intent.get("authored_slots"))
    required = _canonical_identity_list(
        intent.get("required_live_slot_identities"),
        "required_live_slot_identities",
    )
    continuity = _canonical_continuity_rows(intent.get("continuity_only_slots"))
    authored_ids = {tuple(row["slot_identity"]) for row in authored}
    required_ids = {tuple(row) for row in required}
    continuity_ids = {tuple(row["slot_identity"]) for row in continuity}
    if not required_ids.issubset(authored_ids):
        raise GeneratorDeliveryScopeError(
            "required-live identities must be a subset of authored identities"
        )
    if continuity_ids != authored_ids.difference(required_ids):
        raise GeneratorDeliveryScopeError(
            "continuity-only identities must be the exact authored-minus-required complement"
        )

    projection = {key: value for key, value in intent.items() if key != "intent_sha256"}
    expected_hash = canonical_sha256(projection)
    supplied_hash = str(intent.get("intent_sha256") or "").strip().casefold()
    if supplied_hash != expected_hash:
        raise GeneratorDeliveryScopeError("delivery scope intent hash mismatch")
    return {
        "authored_slots": authored,
        "authored_slot_identities": authored_ids,
        "required_live_slot_identities": required_ids,
        "continuity_only_slot_identities": continuity_ids,
        "intent_sha256": supplied_hash,
    }


def validate_resolved_delivery_scope(
    connection,
    *,
    target_spm,
    material_id,
    provider_blend,
    target_spm_postwrite_sha256,
    postwrite_validation_mode=POSTWRITE_MODE_EXACT_CURRENT,
):
    if not isinstance(connection, dict):
        raise GeneratorDeliveryScopeError("generator connection must be an object")
    scope = connection.get("delivery_scope")
    if not isinstance(scope, dict):
        raise GeneratorDeliveryScopeError("resolved delivery scope must be an object")
    if scope.get("kind") != SCOPE_KIND or scope.get("schema_version") != 1:
        raise GeneratorDeliveryScopeError("resolved delivery scope schema is unsupported")
    intent = scope.get("intent")
    validated = validate_delivery_scope_intent(
        intent,
        target_spm=target_spm,
        material_id=material_id,
        provider_blend=provider_blend,
    )
    bindings = (
        connection.get("authored_bindings")
        if "authored_bindings" in connection
        else connection.get("bindings")
    )
    actual_authored = canonical_authored_slots(bindings)
    if actual_authored != validated["authored_slots"]:
        raise GeneratorDeliveryScopeError(
            "resolved Generator bindings differ from sealed authored slots"
        )

    resolved = scope.get("resolved")
    if not isinstance(resolved, dict):
        raise GeneratorDeliveryScopeError("resolved delivery receipt is missing")
    if resolved.get("kind") != RESOLVED_KIND or resolved.get("schema_version") != 1:
        raise GeneratorDeliveryScopeError("resolved delivery receipt schema is unsupported")
    if str(resolved.get("intent_sha256") or "").strip().casefold() != validated[
        "intent_sha256"
    ]:
        raise GeneratorDeliveryScopeError("resolved delivery intent echo mismatch")
    bindings_sha256 = canonical_sha256(actual_authored)
    if str(resolved.get("bindings_sha256") or "").strip().casefold() != bindings_sha256:
        raise GeneratorDeliveryScopeError("resolved delivery bindings hash mismatch")
    postwrite = str(resolved.get("target_spm_postwrite_sha256") or "").strip().casefold()
    expected_postwrite = str(target_spm_postwrite_sha256 or "").strip().casefold()
    if postwrite_validation_mode not in {
        POSTWRITE_MODE_EXACT_CURRENT,
        POSTWRITE_MODE_HISTORICAL_PROOF,
    }:
        raise GeneratorDeliveryScopeError(
            "resolved delivery postwrite validation mode is unsupported"
        )
    if _SHA256_RE.fullmatch(postwrite) is None:
        raise GeneratorDeliveryScopeError(
            "resolved delivery target SPM hash is invalid"
        )
    if _SHA256_RE.fullmatch(expected_postwrite) is None:
        raise GeneratorDeliveryScopeError(
            "current target SPM hash is invalid"
        )
    postwrite_matches_current = postwrite == expected_postwrite
    if (
        postwrite_validation_mode == POSTWRITE_MODE_EXACT_CURRENT
        and not postwrite_matches_current
    ):
        raise GeneratorDeliveryScopeError("resolved delivery target SPM hash mismatch")
    projection = {key: value for key, value in resolved.items() if key != "resolved_sha256"}
    expected_resolved_hash = canonical_sha256(projection)
    supplied_resolved_hash = str(resolved.get("resolved_sha256") or "").strip().casefold()
    if supplied_resolved_hash != expected_resolved_hash:
        raise GeneratorDeliveryScopeError("resolved delivery receipt hash mismatch")
    return {
        **validated,
        "postwrite_validation_mode": postwrite_validation_mode,
        "target_spm_postwrite_sha256": postwrite,
        "current_target_spm_sha256": expected_postwrite,
        "target_spm_postwrite_matches_current": postwrite_matches_current,
    }


__all__ = [
    "CONTINUITY_ONLY_POLICY",
    "GeneratorDeliveryScopeError",
    "INTENT_KIND",
    "MATERIAL_DEFAULT_MESH_ID",
    "POSTWRITE_MODE_EXACT_CURRENT",
    "POSTWRITE_MODE_HISTORICAL_PROOF",
    "RESOLVED_KIND",
    "RUNTIME_INACTIVE_POLICY",
    "SCHEMA_VERSION",
    "SCOPE_KIND",
    "canonical_authored_slots",
    "canonical_sha256",
    "canonical_slot_identity",
    "generator_guid_key",
    "validate_delivery_scope_intent",
    "validate_resolved_delivery_scope",
]
