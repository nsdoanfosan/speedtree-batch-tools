"""Proof-gated migration of legacy Atlas producer targets to canonical SPMs.

This module deliberately does not launch Blender, SpeedTree, or any batch UI.
It proves that an unprefixed Cluster SPM was normalized to an authoritative
``SK_`` output, plans the exact registry-only substitution, and publishes that
substitution with the target registry's compare-and-swap contract.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Iterable, Mapping

from artifact_content_key import SHA256_ALGORITHM, file_content_key_snapshot
from atlas_target_registry import (
    TargetRegistryError,
    load_target_registry,
    registry_path_for_blend,
    save_target_registry,
    target_registry_path_state,
)
from cluster_spm_pair_contract import (
    RECEIPT_KIND,
    SCHEMA_VERSION as PAIR_RECEIPT_SCHEMA_VERSION,
    inspect_cluster_spm_pair,
    resolve_cluster_spm_pair,
)


PROOF_KIND = "atlas_producer_canonical_rebind"
PROOF_SCHEMA_VERSION = 1
PROOF_POLICY = "cluster_pair_normalization_registry_rebind_v1"
PLAN_KIND = "atlas_producer_registry_rebind_plan"
PLAN_SCHEMA_VERSION = 1


class AtlasProducerRebindError(RuntimeError):
    """The requested producer relation cannot be proven safely."""


class AtlasProducerRebindProofError(AtlasProducerRebindError):
    """A producer proof or plan is malformed, unsealed, or inapplicable."""


def _canonical_json(value: Mapping[str, Any]) -> bytes:
    return (json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ) + "\n").encode("utf-8")


def _sealed(value: Mapping[str, Any], field: str) -> dict[str, Any]:
    result = copy.deepcopy(dict(value))
    result.pop(field, None)
    result[field] = hashlib.sha256(_canonical_json(result)).hexdigest()
    return result


def _valid_sha256(value: Any) -> bool:
    text = str(value or "").casefold()
    return len(text) == 64 and all(character in "0123456789abcdef" for character in text)


def _path_key(value: str | Path) -> str:
    return os.path.normcase(os.path.abspath(os.fspath(value))).casefold()


def _same_path(left: str | Path, right: str | Path) -> bool:
    if _path_key(left) == _path_key(right):
        return True
    left_path = Path(left)
    right_path = Path(right)
    if left_path.exists() and right_path.exists():
        try:
            return os.path.samefile(left_path, right_path)
        except OSError:
            pass
    return False


def _absolute_path(value: Any, label: str, suffix: str = "") -> Path:
    path = Path(str(value or "").strip()).expanduser()
    if not path.is_absolute():
        raise AtlasProducerRebindProofError(f"{label} must be an absolute path")
    if suffix and path.suffix.casefold() != suffix.casefold():
        raise AtlasProducerRebindProofError(f"{label} must be a {suffix} file")
    return path.absolute()


def _file_record(path: str | Path, label: str) -> dict[str, Any]:
    candidate = Path(path)
    if not candidate.is_file():
        raise AtlasProducerRebindProofError(f"{label} does not exist: {candidate}")
    snapshot = file_content_key_snapshot(candidate, SHA256_ALGORITHM)
    return {
        "path": str(candidate.absolute()),
        "sha256": snapshot["digest"],
        "size": snapshot["size"],
        "algorithm": snapshot["algorithm"],
    }


def _optional_file_record(path: str | Path) -> dict[str, Any]:
    candidate = Path(path)
    if not candidate.is_file():
        return {"path": str(candidate.absolute()), "state": "missing"}
    return {"state": "file", **_file_record(candidate, "legacy Cluster SPM")}


def _read_json_object(path: Path, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise AtlasProducerRebindProofError(f"cannot read {label} {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise AtlasProducerRebindProofError(f"{label} must contain an object: {path}")
    return payload


def _stable_json_object(
    path: Path,
    label: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    before = _file_record(path, label)
    payload = _read_json_object(path, label)
    after = _file_record(path, label)
    if before != after:
        raise AtlasProducerRebindProofError(
            f"{label} changed while its authority was being sealed"
        )
    return payload, after


def _inventory_spelling(
    canonical_spm: str | Path,
    inventory_paths: Iterable[str | Path],
) -> Path:
    requested = _absolute_path(canonical_spm, "canonical SPM", ".spm")
    if not requested.is_file():
        raise AtlasProducerRebindProofError(f"canonical SPM does not exist: {requested}")
    for value in inventory_paths:
        candidate = Path(str(value or "").strip()).expanduser()
        if (
            candidate.is_absolute()
            and candidate.suffix.casefold() == ".spm"
            and candidate.is_file()
            and _same_path(candidate, requested)
        ):
            return candidate.absolute()
    raise AtlasProducerRebindProofError(
        "canonical SPM does not match an exact current inventory identity"
    )


def _material_groups(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw_groups = payload.get("speedtree_material_groups") or payload.get("material_groups") or []
    if not raw_groups and payload.get("material_id"):
        raw_groups = [{
            "material_id": payload.get("material_id"),
            "material": payload.get("material_name") or payload.get("material"),
            "mesh_ids": payload.get("mesh_ids") or [],
        }]
    groups = []
    for raw in raw_groups:
        if not isinstance(raw, Mapping):
            continue
        material_id = raw.get("material_id")
        mesh_values = raw.get("mesh_ids") or []
        if (
            isinstance(material_id, bool)
            or not isinstance(material_id, int)
            or material_id <= 0
            or not isinstance(mesh_values, (list, tuple))
        ):
            continue
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value <= 0
            for value in mesh_values
        ):
            continue
        mesh_ids = sorted(set(mesh_values))
        if not mesh_ids:
            continue
        groups.append({
            "material_id": material_id,
            "material_name": str(raw.get("material") or ""),
            "material_collection": str(raw.get("collection") or ""),
            "mesh_ids": mesh_ids,
        })
    if not groups:
        raise AtlasProducerRebindProofError(
            "legacy Atlas manifest has no exact positive Material/Mesh ownership groups"
        )
    return groups


def _validate_pair_receipt(receipt: Mapping[str, Any], pair: Mapping[str, Any]) -> None:
    policy = receipt.get("policy") or {}
    invariants = receipt.get("invariants") or {}
    expected_policy = {
        "canonical_role": "production_output",
        "legacy_role": "normalization_input_only",
        "normalization_direction": "legacy_unprefixed_to_sk",
        "publish_to_legacy_allowed": False,
    }
    expected_invariants = {
        "source_unchanged_during_copy": True,
        "after_content_equal": True,
        "canonical_output_authoritative": True,
    }
    if (
        receipt.get("receipt_kind") != RECEIPT_KIND
        or receipt.get("schema_version") != PAIR_RECEIPT_SCHEMA_VERSION
        or receipt.get("status") != "complete"
        or receipt.get("operation") != "normalize_legacy_output_to_canonical"
        or str(receipt.get("pair_id") or "") != str(pair["pair_id"])
        or any(policy.get(key) != value for key, value in expected_policy.items())
        or any(invariants.get(key) != value for key, value in expected_invariants.items())
    ):
        raise AtlasProducerRebindProofError(
            "Cluster pair receipt does not seal canonical normalization authority"
        )


def _manifest_declared_spm(payload: Mapping[str, Any], manifest_path: Path) -> Path:
    declared = Path(str(payload.get("spm") or "").strip()).expanduser()
    if not declared.is_absolute():
        declared = manifest_path.parent.parent / declared
    return declared.absolute()


def build_atlas_producer_rebind_proof(
    canonical_spm: str | Path,
    legacy_manifest_path: str | Path,
    *,
    inventory_paths: Iterable[str | Path],
) -> dict[str, Any]:
    """Seal exact historical and current evidence for one canonical rebind."""

    canonical = _inventory_spelling(canonical_spm, inventory_paths)
    manifest_path = _absolute_path(legacy_manifest_path, "legacy Atlas manifest", ".json")
    manifest, manifest_record = _stable_json_object(
        manifest_path, "legacy Atlas manifest"
    )
    legacy = _manifest_declared_spm(manifest, manifest_path)
    pair = resolve_cluster_spm_pair(legacy)
    if pair["input_role"] != "mirror":
        raise AtlasProducerRebindProofError(
            "legacy Atlas manifest must declare the unprefixed Cluster pair member"
        )
    if not _same_path(pair["canonical_spm"], canonical):
        raise AtlasProducerRebindProofError(
            "legacy Atlas manifest does not resolve to the requested canonical SPM"
        )
    expected_manifest = (
        legacy.parent / ".atlas_leaf_speedtree_targets" / f"{legacy.stem}.json"
    ).absolute()
    if not _same_path(manifest_path, expected_manifest):
        raise AtlasProducerRebindProofError(
            "legacy Atlas manifest is not the exact target-local producer receipt"
        )

    receipt_path = Path(pair["receipt_path"])
    if not receipt_path.is_file():
        raise AtlasProducerRebindProofError(
            "canonical Cluster SPM has no valid historical pair normalization receipt"
        )
    receipt_record_before = _file_record(
        receipt_path, "Cluster pair receipt"
    )
    inspection = inspect_cluster_spm_pair(canonical)
    receipt_record = _file_record(receipt_path, "Cluster pair receipt")
    if receipt_record_before != receipt_record:
        raise AtlasProducerRebindProofError(
            "Cluster pair receipt changed while its authority was being sealed"
        )
    receipt = inspection.get("receipt")
    if inspection.get("status") != "current" or not isinstance(receipt, Mapping):
        raise AtlasProducerRebindProofError(
            "canonical Cluster SPM has no valid historical pair normalization receipt"
        )
    _validate_pair_receipt(receipt, pair)

    blend = _absolute_path(manifest.get("blend_file"), "producer blend", ".blend")
    if not blend.is_file():
        raise AtlasProducerRebindProofError(f"producer blend does not exist: {blend}")
    source_collection = str(manifest.get("source_collection") or "").strip()
    export_scope_id = str(manifest.get("export_scope_id") or "").strip()
    if not source_collection or not export_scope_id:
        raise AtlasProducerRebindProofError(
            "legacy Atlas manifest has no complete producer source/export identity"
        )

    registry_state_before = target_registry_path_state(blend)
    try:
        registry = load_target_registry(blend)
    except TargetRegistryError as exc:
        raise AtlasProducerRebindProofError(str(exc)) from exc
    if not registry:
        raise AtlasProducerRebindProofError("producer blend has no target registry")
    registry_state = target_registry_path_state(blend)
    if registry_state_before != registry_state:
        raise AtlasProducerRebindProofError(
            "producer registry changed while its authority was being sealed"
        )
    if not _same_path(registry.get("atlas_blend") or "", blend):
        raise AtlasProducerRebindProofError("target registry belongs to a different blend")
    targets = list(registry["target_spms"])
    legacy_indexes = [index for index, value in enumerate(targets) if _same_path(value, legacy)]
    if len(legacy_indexes) != 1:
        raise AtlasProducerRebindProofError(
            "producer registry must contain the exact legacy target exactly once"
        )

    connection = manifest.get("generator_connection") or {}
    requested = connection.get("requested") is True
    complete = connection.get("complete") is True
    if requested and not complete:
        raise AtlasProducerRebindProofError(
            "requested Generator bindings are not complete in the legacy receipt"
        )

    proof = {
        "kind": PROOF_KIND,
        "schema_version": PROOF_SCHEMA_VERSION,
        "status": "validated",
        "authoritative": True,
        "proof_policy": PROOF_POLICY,
        "canonical_spm": _file_record(canonical, "canonical Cluster SPM"),
        "legacy_spm": _optional_file_record(legacy),
        "pair": {
            "pair_id": pair["pair_id"],
            "generation": int(receipt["generation"]),
            "receipt": receipt_record,
        },
        "legacy_manifest": manifest_record,
        "producer": {
            "blend": _file_record(blend, "producer blend"),
            "source_collection": source_collection,
            "export_scope_id": export_scope_id,
            "material_groups": _material_groups(manifest),
            "connection_mode": "generator_bindings" if requested else "assets_only",
        },
        "registry": {
            "path": str(registry_path_for_blend(blend)),
            "state": registry_state,
            "target_spms": targets,
            "legacy_target_index": legacy_indexes[0],
        },
    }
    return _sealed(proof, "proof_sha256")


def _validate_file_record(record: Any, label: str, suffix: str) -> Path:
    if not isinstance(record, Mapping):
        raise AtlasProducerRebindProofError(f"{label} record is missing")
    path = _absolute_path(record.get("path"), label, suffix)
    if not _valid_sha256(record.get("sha256")) or record.get("algorithm") != SHA256_ALGORITHM:
        raise AtlasProducerRebindProofError(f"{label} has no full SHA-256 identity")
    size = record.get("size")
    if isinstance(size, bool) or not isinstance(size, int) or size < 0:
        raise AtlasProducerRebindProofError(f"{label} size is invalid")
    return path


def validate_atlas_producer_rebind_proof(
    proof: Mapping[str, Any],
    *,
    canonical_spm: str | Path | None = None,
) -> dict[str, Any]:
    """Validate one sealed proof without reinterpreting unsealed audit data."""

    if not isinstance(proof, Mapping):
        raise AtlasProducerRebindProofError("Atlas producer relation proof must be an object")
    candidate = copy.deepcopy(dict(proof))
    supplied_sha256 = candidate.pop("proof_sha256", None)
    expected_sha256 = hashlib.sha256(_canonical_json(candidate)).hexdigest()
    if not _valid_sha256(supplied_sha256) or supplied_sha256 != expected_sha256:
        raise AtlasProducerRebindProofError("Atlas producer relation proof seal is invalid")
    if (
        candidate.get("kind") != PROOF_KIND
        or candidate.get("schema_version") != PROOF_SCHEMA_VERSION
        or candidate.get("status") != "validated"
        or candidate.get("authoritative") is not True
        or candidate.get("proof_policy") != PROOF_POLICY
    ):
        raise AtlasProducerRebindProofError("unsupported Atlas producer relation proof")

    canonical = _validate_file_record(candidate.get("canonical_spm"), "canonical SPM", ".spm")
    legacy_record = candidate.get("legacy_spm")
    if not isinstance(legacy_record, Mapping):
        raise AtlasProducerRebindProofError("legacy SPM record is missing")
    legacy = _absolute_path(legacy_record.get("path"), "legacy SPM", ".spm")
    if legacy_record.get("state") == "file":
        _validate_file_record(legacy_record, "legacy SPM", ".spm")
    elif legacy_record.get("state") != "missing":
        raise AtlasProducerRebindProofError("legacy SPM state is invalid")
    pair_contract = resolve_cluster_spm_pair(legacy)
    if (
        pair_contract["input_role"] != "mirror"
        or not _same_path(pair_contract["canonical_spm"], canonical)
    ):
        raise AtlasProducerRebindProofError(
            "proof paths do not form one legacy-to-canonical Cluster pair"
        )
    if canonical_spm is not None:
        requested_canonical = _absolute_path(
            canonical_spm, "requested canonical SPM", ".spm"
        )
        if not _same_path(canonical, requested_canonical):
            raise AtlasProducerRebindProofError(
                "producer proof is for a different canonical SPM"
            )

    pair = candidate.get("pair")
    if not isinstance(pair, Mapping) or not _valid_sha256(pair.get("pair_id")):
        raise AtlasProducerRebindProofError("pair identity is invalid")
    generation = pair.get("generation")
    if isinstance(generation, bool) or not isinstance(generation, int) or generation < 1:
        raise AtlasProducerRebindProofError("pair generation is invalid")
    receipt_path = _validate_file_record(
        pair.get("receipt"), "pair receipt", ".json"
    )
    if (
        str(pair.get("pair_id")) != pair_contract["pair_id"]
        or not _same_path(receipt_path, pair_contract["receipt_path"])
    ):
        raise AtlasProducerRebindProofError("pair receipt identity is inconsistent")
    manifest_path = _validate_file_record(
        candidate.get("legacy_manifest"), "legacy manifest", ".json"
    )
    expected_manifest = (
        legacy.parent / ".atlas_leaf_speedtree_targets" / f"{legacy.stem}.json"
    )
    if not _same_path(manifest_path, expected_manifest):
        raise AtlasProducerRebindProofError("legacy manifest path is inconsistent")

    producer = candidate.get("producer")
    if not isinstance(producer, Mapping):
        raise AtlasProducerRebindProofError("producer identity is missing")
    blend = _validate_file_record(producer.get("blend"), "producer blend", ".blend")
    if not str(producer.get("source_collection") or "").strip() or not str(
        producer.get("export_scope_id") or ""
    ).strip():
        raise AtlasProducerRebindProofError("producer source/export identity is incomplete")
    if producer.get("connection_mode") not in {"assets_only", "generator_bindings"}:
        raise AtlasProducerRebindProofError("producer connection mode is invalid")
    groups = producer.get("material_groups")
    if not isinstance(groups, list) or not groups:
        raise AtlasProducerRebindProofError("producer Material/Mesh groups are missing")
    for group in groups:
        if not isinstance(group, Mapping):
            raise AtlasProducerRebindProofError("producer Material/Mesh group is invalid")
        material_id = group.get("material_id")
        mesh_ids = group.get("mesh_ids")
        if (
            isinstance(material_id, bool)
            or not isinstance(material_id, int)
            or material_id <= 0
            or not isinstance(mesh_ids, list)
            or not mesh_ids
            or any(
                isinstance(value, bool)
                or not isinstance(value, int)
                or value <= 0
                for value in mesh_ids
            )
            or mesh_ids != sorted(set(mesh_ids))
        ):
            raise AtlasProducerRebindProofError("producer Material/Mesh group is invalid")

    registry = candidate.get("registry")
    if not isinstance(registry, Mapping):
        raise AtlasProducerRebindProofError("producer registry identity is missing")
    registry_path = _absolute_path(registry.get("path"), "producer registry", ".json")
    if not _same_path(registry_path, registry_path_for_blend(blend)):
        raise AtlasProducerRebindProofError("producer registry path does not match its blend")
    state = registry.get("state")
    targets = registry.get("target_spms")
    if not isinstance(state, Mapping) or state.get("state") != "file":
        raise AtlasProducerRebindProofError("producer registry CAS state is invalid")
    if (
        not _same_path(state.get("path") or "", registry_path)
        or state.get("algorithm") != SHA256_ALGORITHM
        or not _valid_sha256(state.get("sha256"))
        or isinstance(state.get("size"), bool)
        or not isinstance(state.get("size"), int)
        or state.get("size") < 0
    ):
        raise AtlasProducerRebindProofError("producer registry CAS identity is invalid")
    if not isinstance(targets, list) or not targets:
        raise AtlasProducerRebindProofError("producer registry targets are missing")
    normalized_targets = [
        str(_absolute_path(value, "producer registry target", ".spm"))
        for value in targets
    ]
    if len({_path_key(value) for value in normalized_targets}) != len(normalized_targets):
        raise AtlasProducerRebindProofError("producer registry targets are duplicated")
    legacy_indexes = [index for index, value in enumerate(normalized_targets) if _same_path(value, legacy)]
    if legacy_indexes != [registry.get("legacy_target_index")]:
        raise AtlasProducerRebindProofError("producer registry legacy target identity is invalid")
    candidate["registry"]["target_spms"] = normalized_targets
    candidate["proof_sha256"] = supplied_sha256
    return candidate


def plan_atlas_producer_registry_rebind(proof: Mapping[str, Any]) -> dict[str, Any]:
    """Plan the one exact legacy-to-canonical registry substitution."""

    validated = validate_atlas_producer_rebind_proof(proof)
    canonical = validated["canonical_spm"]["path"]
    legacy = validated["legacy_spm"]["path"]
    before = list(validated["registry"]["target_spms"])
    after = []
    seen = set()
    for value in before:
        output = canonical if _same_path(value, legacy) else value
        key = _path_key(output)
        if key not in seen:
            seen.add(key)
            after.append(output)
    if not any(_same_path(value, canonical) for value in after):
        raise AtlasProducerRebindProofError("planned registry has no canonical producer target")
    plan = {
        "kind": PLAN_KIND,
        "schema_version": PLAN_SCHEMA_VERSION,
        "status": "no_change" if before == after else "ready",
        "proof_sha256": validated["proof_sha256"],
        "producer_blend": validated["producer"]["blend"]["path"],
        "legacy_spm": legacy,
        "canonical_spm": canonical,
        "registry_path": validated["registry"]["path"],
        "expected_registry_state": copy.deepcopy(validated["registry"]["state"]),
        "before_target_spms": before,
        "after_target_spms": after,
    }
    return _sealed(plan, "plan_sha256")


def validate_atlas_producer_rebind_plan(plan: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(plan, Mapping):
        raise AtlasProducerRebindProofError("Atlas producer rebind plan must be an object")
    candidate = copy.deepcopy(dict(plan))
    supplied_sha256 = candidate.pop("plan_sha256", None)
    expected_sha256 = hashlib.sha256(_canonical_json(candidate)).hexdigest()
    if not _valid_sha256(supplied_sha256) or supplied_sha256 != expected_sha256:
        raise AtlasProducerRebindProofError("Atlas producer rebind plan seal is invalid")
    if (
        candidate.get("kind") != PLAN_KIND
        or candidate.get("schema_version") != PLAN_SCHEMA_VERSION
        or candidate.get("status") not in {"ready", "no_change"}
        or not _valid_sha256(candidate.get("proof_sha256"))
    ):
        raise AtlasProducerRebindProofError("unsupported Atlas producer rebind plan")
    blend = _absolute_path(candidate.get("producer_blend"), "producer blend", ".blend")
    legacy = _absolute_path(candidate.get("legacy_spm"), "legacy SPM", ".spm")
    canonical = _absolute_path(candidate.get("canonical_spm"), "canonical SPM", ".spm")
    registry = _absolute_path(candidate.get("registry_path"), "producer registry", ".json")
    if not _same_path(registry, registry_path_for_blend(blend)):
        raise AtlasProducerRebindProofError("plan registry does not match its producer blend")
    before = candidate.get("before_target_spms")
    after = candidate.get("after_target_spms")
    if not isinstance(before, list) or not isinstance(after, list):
        raise AtlasProducerRebindProofError("plan target lists are missing")
    if not any(_same_path(value, legacy) for value in before):
        raise AtlasProducerRebindProofError("plan preimage has no exact legacy target")
    if any(_same_path(value, legacy) for value in after) or not any(
        _same_path(value, canonical) for value in after
    ):
        raise AtlasProducerRebindProofError("plan postimage is not canonical")
    expected_after = []
    seen = set()
    for value in before:
        output = str(canonical) if _same_path(value, legacy) else str(value)
        key = _path_key(output)
        if key not in seen:
            seen.add(key)
            expected_after.append(output)
    if [_path_key(value) for value in after] != [
        _path_key(value) for value in expected_after
    ]:
        raise AtlasProducerRebindProofError(
            "plan postimage changes more than the exact legacy target"
        )
    if not isinstance(candidate.get("expected_registry_state"), Mapping):
        raise AtlasProducerRebindProofError("plan registry CAS state is missing")
    candidate["plan_sha256"] = supplied_sha256
    return candidate


def apply_atlas_producer_registry_rebind(plan: Mapping[str, Any]) -> dict[str, Any]:
    """CAS-publish a validated registry rebind, preserving concurrent work."""

    validated = validate_atlas_producer_rebind_plan(plan)
    blend = validated["producer_blend"]
    current = load_target_registry(blend)
    if not current:
        raise AtlasProducerRebindProofError("producer registry disappeared before apply")
    targets = list(current["target_spms"])
    legacy = validated["legacy_spm"]
    canonical = validated["canonical_spm"]
    if (
        any(_same_path(value, canonical) for value in targets)
        and not any(_same_path(value, legacy) for value in targets)
    ):
        return {
            "status": "up_to_date",
            "committed": False,
            "producer_blend": blend,
            "target_spms": targets,
            "registry_state": target_registry_path_state(blend),
            "plan_sha256": validated["plan_sha256"],
        }
    published = save_target_registry(
        blend,
        validated["after_target_spms"],
        expected_registry_state=validated["expected_registry_state"],
    )
    return {
        "status": "applied",
        "committed": True,
        "producer_blend": blend,
        "target_spms": published["target_spms"],
        "registry_path": published["registry_path"],
        "registry_state": published["registry_state"],
        "plan_sha256": validated["plan_sha256"],
    }


def validate_atlas_producer_refresh_manifest(
    proof: Mapping[str, Any],
    payload: Mapping[str, Any],
    *,
    manifest_path: str | Path | None = None,
) -> dict[str, Any]:
    """Validate one prospective canonical producer manifest.

    This payload-only form is intentionally usable by the add-on's private
    staging transaction.  It lets the exact producer prove the canonical
    receipt before any staged SPM, mesh, or receipt is committed to the live
    target folder.
    """

    validated = validate_atlas_producer_rebind_proof(proof)
    if validated["producer"]["connection_mode"] != "assets_only":
        raise AtlasProducerRebindProofError(
            "canonical producer refresh currently supports assets-only receipts"
        )
    canonical = Path(validated["canonical_spm"]["path"])
    legacy = validated["legacy_spm"]["path"]
    blend = validated["producer"]["blend"]["path"]
    expected = (
        canonical.parent
        / ".atlas_leaf_speedtree_targets"
        / f"{canonical.stem}.json"
    ).absolute()
    receipt_path = (
        _absolute_path(manifest_path, "canonical Atlas receipt", ".json")
        if manifest_path is not None
        else expected
    )
    if not _same_path(receipt_path, expected):
        raise AtlasProducerRebindProofError(
            "canonical Atlas receipt is not the exact target-local path"
        )
    if not isinstance(payload, Mapping):
        raise AtlasProducerRebindProofError(
            "canonical Atlas receipt must contain an object"
        )
    declared_spm = _manifest_declared_spm(payload, receipt_path)
    if not _same_path(declared_spm, canonical):
        raise AtlasProducerRebindProofError(
            "canonical Atlas receipt declares a different SPM"
        )
    declared_blend = _absolute_path(
        payload.get("blend_file"), "canonical receipt producer blend", ".blend"
    )
    if not _same_path(declared_blend, blend):
        raise AtlasProducerRebindProofError(
            "canonical Atlas receipt declares a different producer blend"
        )
    if (
        str(payload.get("source_collection") or "").strip()
        != validated["producer"]["source_collection"]
        or str(payload.get("export_scope_id") or "").strip()
        != validated["producer"]["export_scope_id"]
    ):
        raise AtlasProducerRebindProofError(
            "canonical Atlas receipt producer source/export identity changed"
        )
    expected_groups = [
        (row["material_id"], tuple(row["mesh_ids"]))
        for row in validated["producer"]["material_groups"]
    ]
    current_groups = [
        (row["material_id"], tuple(row["mesh_ids"]))
        for row in _material_groups(payload)
    ]
    if current_groups != expected_groups:
        raise AtlasProducerRebindProofError(
            "canonical Atlas receipt Material/Mesh ownership changed"
        )
    connection = payload.get("generator_connection") or {}
    if connection.get("requested") is not False:
        raise AtlasProducerRebindProofError(
            "assets-only canonical receipt unexpectedly requests Generator bindings"
        )
    declared_target_manifest = str(payload.get("target_manifest") or "").strip()
    if declared_target_manifest and not _same_path(
        declared_target_manifest, receipt_path
    ):
        raise AtlasProducerRebindProofError(
            "canonical Atlas receipt self-path is inconsistent"
        )
    return {
        "status": "validated",
        "canonical_spm": str(canonical),
        "producer_blend": str(declared_blend),
        "material_groups": copy.deepcopy(
            validated["producer"]["material_groups"]
        ),
        "connection_mode": "assets_only",
        "proof_sha256": validated["proof_sha256"],
    }


def validate_atlas_producer_refresh_receipt(
    proof: Mapping[str, Any],
    *,
    manifest_path: str | Path | None = None,
) -> dict[str, Any]:
    """Validate the committed canonical target-local receipt after rebind."""

    validated = validate_atlas_producer_rebind_proof(proof)
    canonical = Path(validated["canonical_spm"]["path"])
    legacy = validated["legacy_spm"]["path"]
    blend = validated["producer"]["blend"]["path"]
    expected = (
        canonical.parent
        / ".atlas_leaf_speedtree_targets"
        / f"{canonical.stem}.json"
    ).absolute()
    receipt_path = (
        _absolute_path(manifest_path, "canonical Atlas receipt", ".json")
        if manifest_path is not None
        else expected
    )
    payload, receipt_record = _stable_json_object(
        receipt_path, "canonical Atlas receipt"
    )
    manifest_validation = validate_atlas_producer_refresh_manifest(
        validated,
        payload,
        manifest_path=receipt_path,
    )
    registry = load_target_registry(blend)
    if not registry:
        raise AtlasProducerRebindProofError(
            "canonical producer registry disappeared after refresh"
        )
    targets = registry["target_spms"]
    if (
        not any(_same_path(value, canonical) for value in targets)
        or any(_same_path(value, legacy) for value in targets)
    ):
        raise AtlasProducerRebindProofError(
            "canonical producer registry postcondition is not current"
        )
    return {
        **manifest_validation,
        "receipt": receipt_record,
        "registry_target_spms": list(targets),
    }


__all__ = [
    "AtlasProducerRebindError",
    "AtlasProducerRebindProofError",
    "PROOF_KIND",
    "PROOF_POLICY",
    "PROOF_SCHEMA_VERSION",
    "PLAN_KIND",
    "PLAN_SCHEMA_VERSION",
    "apply_atlas_producer_registry_rebind",
    "build_atlas_producer_rebind_proof",
    "plan_atlas_producer_registry_rebind",
    "validate_atlas_producer_rebind_plan",
    "validate_atlas_producer_rebind_proof",
    "validate_atlas_producer_refresh_manifest",
    "validate_atlas_producer_refresh_receipt",
]
