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
import json
import os
import tempfile
from pathlib import Path

from cluster_spm_pair_contract import (
    ClusterSpmPairError,
    resolve_cluster_spm_pair,
)


RESOLUTION_SCHEMA_VERSION = 1
SUPPORTED_ATLAS_SCHEMA_VERSIONS = {1}

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


def _candidate_claims(payload, source_identity, kind):
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

    connection = payload.get("generator_connection") or {}
    for binding in connection.get("bindings") or []:
        if not isinstance(binding, dict):
            continue
        slot = str(binding.get("slot_prefix") or "").strip().casefold()
        guid = str(binding.get("generator_guid") or "").strip().casefold()
        index = str(binding.get("generator_index") or "").strip()
        name = str(binding.get("generator_name") or "").strip().casefold()
        owner = (
            guid
            or (f"index:{index}" if index else "")
            or (f"name:{name}" if name else "")
        )
        if not owner or not slot:
            continue
        claims[f"generator:{owner}:{slot}"] = {
            "source_identity": source_identity,
            "binding": _without_derived_texture_fields(copy.deepcopy(binding)),
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
            "generator_connection": _without_derived_texture_fields(
                copy.deepcopy(connection)
            ),
            "collection_tombstone": _without_derived_texture_fields(
                copy.deepcopy(payload.get("collection_tombstone"))
            ),
        }
    return claims


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


def _candidate_specs(target, pair_identity=None):
    target_dir = target.parent / ".atlas_leaf_speedtree_targets"
    scope_dir = target.parent / ".atlas_leaf_speedtree_scopes"
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
    global_path = target.parent / "speedtree_import_manifest.json"
    if global_path.is_file():
        specs.append((global_path, "exact_global_target"))
    return specs


def _diagnostic_specs(target, pair_identity=None):
    scope_dir = target.parent / ".atlas_leaf_speedtree_scopes"
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
        for path in sorted(target.parent.glob("speedtree_import_manifest_M_*.json"))
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
):
    """Resolve exact Atlas records for one SPM and return auditable evidence.

    The function is read-only.  Invalid JSON and explicit unsupported schemas
    are fail-closed.  Foreign-target and caller-filtered records are rejected
    with reasons, while diagnostic legacy records are always shadowed.
    """
    target = Path(target_spm).expanduser().resolve(strict=False)
    target_key = normalized_manifest_path(target)
    pair_identity = proven_cluster_pair_identity(target)
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

    target_dir = target.parent / ".atlas_leaf_speedtree_targets"
    target_stems = _target_stems(target, pair_identity)
    if not any((target_dir / f"{stem}.json").is_file() for stem in target_stems):
        resolution["missing"].append({
            "path": str(target_dir / f"{target.stem}.json"),
            "reason": "candidate_file_missing",
        })
    global_path = target.parent / "speedtree_import_manifest.json"
    if not global_path.is_file():
        resolution["missing"].append({
            "path": str(global_path),
            "reason": "candidate_file_missing",
        })
    scope_dir = target.parent / ".atlas_leaf_speedtree_scopes"
    if not scope_dir.is_dir() or not _target_scope_paths(
        scope_dir, target, pair_identity
    ):
        resolution["missing"].append({
            "path": str(scope_dir / f"*__{target.stem}.json"),
            "reason": "target_scope_candidate_missing",
        })

    valid = []
    fatal = []
    for path, kind in _candidate_specs(target, pair_identity):
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
            relative_to=target.parent,
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
            target_parent=target.parent,
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
        claims = _candidate_claims(payload, source_identity, kind)
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

    for path, kind in _diagnostic_specs(target, pair_identity):
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
                target_parent=target.parent,
            )
            row["ownership_claims"] = sorted(
                _candidate_claims(payload, row["source_identity"], kind)
            )
        resolution["shadowed"].append(row)

    if fatal:
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
    "collection_tombstone",
)


def atlas_manifest_mirror_repair_plan(target_spm, resolution=None):
    """Classify whether one conflict is a stale mirror, without mutating it.

    Automatic repair is intentionally narrow.  One exact-per-target authority
    must exist, every conflicting record must be lower precedence, and both
    its source identity and complete ownership-claim key set must match the
    authority.  A different source/scope or extra claim is authoring ambiguity,
    not a stale mirror.
    """

    target = Path(target_spm).expanduser().resolve(strict=False)
    if resolution is None:
        try:
            resolve_atlas_manifests(target)
        except AtlasManifestResolutionError as exc:
            resolution = exc.resolution
        else:
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


def repair_atlas_manifest_mirrors(target_spm):
    """Repair only a freshly proven exact-authority/lower-mirror conflict."""

    target = Path(target_spm).expanduser().resolve(strict=False)
    plan = atlas_manifest_mirror_repair_plan(target)
    if plan.get("status") == "not_needed":
        return plan
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
    slot = str(declared.get("slot_prefix") or "").strip().casefold()
    if slot != str(live.get("slot_prefix") or "").strip().casefold():
        return False
    for field in ("generator_guid", "generator_index", "generator_name"):
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
        if not requested:
            candidates.append(row)
            continue

        declared_bindings = [
            binding
            for binding in connection.get("bindings") or []
            if isinstance(binding, dict)
        ]
        if not declared_bindings:
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
                row.setdefault("non_export_participating_mesh_ids", []).extend(
                    missing
                )

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
