"""Pure shared contract for one narrow SpeedTree preview-role fallback."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
from pathlib import Path


PREVIEW_ROLE_FALLBACKS_FIELD = "preview_role_fallbacks"
PREVIEW_FALLBACK_SCHEMA_FIELD = (
    "preview_role_fallbacks_schema_version"
)
PREVIEW_FALLBACK_SCHEMA_VERSION = 1
RECEIPT_CAPABILITIES_FIELD = "receipt_capabilities"
PREVIEW_FALLBACK_CAPABILITY = "speedtree_preview_role_fallback_v1"
PREVIEW_RECEIPT_VERSION = 2
RECEIPT_CLAIM_FIELD = "receipt_claim"
PREVIEW_ONLY_USAGE = "speedtree_preview_only"
RECEIPT_CORE_SHA256_FIELD = "receipt_core_sha256"
RECEIPT_CACHE_KEY_FIELD = "receipt_cache_key"
PREVIEW_RECEIPT_KIND = "blender_cluster_bake_texture_origin_receipt"
PREVIEW_RECEIPT_SOURCE_ORIGIN = "blender_cluster_bake"
PHYSICAL_DIRECT_CAPTURE_WORKFLOW = "PHYSICAL_DIRECT_CAPTURE"
PHYSICAL_DIRECT_CAPTURE_UV_SOURCE = (
    "same_blender_physical_capture_projection"
)
SUBSURFACE_AMOUNT_ROLE = "subsurfaceamount"
SUBSURFACE_COLOR_ROLE = "subsurfacecolor"

FALLBACK_CANONICAL_FIELDS = (
    "slot_role",
    "manifest_role",
    "usage",
    "material_id",
    "material_name",
    "contract_hash",
    "map_index",
    "map",
    "path",
    "sha256",
)
SLOT_FILE_CANONICAL_FIELDS = (
    "map_index",
    "spm_map_index",
    "stmat_map_index",
    "map",
    "capture_role",
    "path",
    "sha256",
)

_SHA256_RE = re.compile(r"[0-9a-f]{64}")


def _path_identity(value):
    return os.path.normcase(
        str(Path(str(value or "")).expanduser().resolve())
    ).casefold()


def _map_role(value):
    return re.sub(r"[^a-z0-9]+", "", str(value or "").casefold())


def build_preview_role_fallback(
    *,
    slot_role,
    slot_path,
    selected_rows,
    declared_rows,
    material_id,
    material_name,
    contract_hash,
    map_index,
    map_name,
    workflow_mode,
    direct_uv_source,
):
    """Reinterpret only the exact manifest entry selected by ``slot_path``.

    The caller proves live bytes for every manifest row before calling.  This
    helper does not search by role or basename: ``selected_rows`` is the exact
    path match already chosen by the authored STMAT slot.
    """
    if (
        workflow_mode != PHYSICAL_DIRECT_CAPTURE_WORKFLOW
        or direct_uv_source != PHYSICAL_DIRECT_CAPTURE_UV_SOURCE
        or slot_role != SUBSURFACE_AMOUNT_ROLE
        or _map_role(map_name) != SUBSURFACE_AMOUNT_ROLE
        or not isinstance(selected_rows, list)
        or len(selected_rows) != 1
        or not isinstance(declared_rows, dict)
    ):
        return None
    selected = selected_rows[0]
    declared_slot = declared_rows.get(SUBSURFACE_AMOUNT_ROLE)
    if not isinstance(selected, dict) or not isinstance(declared_slot, dict):
        return None
    if (
        selected.get("role") != SUBSURFACE_COLOR_ROLE
        or selected.get("raw_role") != SUBSURFACE_COLOR_ROLE
        or declared_slot.get("role") != SUBSURFACE_AMOUNT_ROLE
        or declared_slot.get("raw_role") != SUBSURFACE_AMOUNT_ROLE
    ):
        return None
    selected_path = str(selected.get("path") or "").strip()
    declared_slot_path = str(declared_slot.get("path") or "").strip()
    selected_sha256 = str(selected.get("sha256") or "").strip().casefold()
    declared_slot_sha256 = str(
        declared_slot.get("sha256") or ""
    ).strip().casefold()
    material_id = str(material_id or "").strip()
    material_name = str(material_name or "").strip()
    contract_hash = str(contract_hash or "").strip().casefold()
    try:
        normalized_index = int(map_index)
    except (TypeError, ValueError):
        return None
    if (
        normalized_index < 0
        or not material_id
        or not material_name
        or not selected_path
        or not declared_slot_path
        or _path_identity(slot_path) != _path_identity(selected_path)
        or _path_identity(declared_slot_path) == _path_identity(selected_path)
        or not _SHA256_RE.fullmatch(selected_sha256)
        or not _SHA256_RE.fullmatch(declared_slot_sha256)
        or not _SHA256_RE.fullmatch(contract_hash)
    ):
        return None
    return {
        "slot_role": SUBSURFACE_AMOUNT_ROLE,
        "manifest_role": SUBSURFACE_COLOR_ROLE,
        "usage": PREVIEW_ONLY_USAGE,
        "material_id": material_id,
        "material_name": material_name,
        "contract_hash": contract_hash,
        "map_index": normalized_index,
        "map": str(map_name),
        "path": str(Path(selected_path).expanduser().resolve()),
        "sha256": selected_sha256,
    }


def preview_role_fallback_signature(row):
    """Return the cross-reader comparison signature for one v1 row."""
    if (
        not isinstance(row, dict)
        or set(row) != set(FALLBACK_CANONICAL_FIELDS)
        or len(row) != len(FALLBACK_CANONICAL_FIELDS)
    ):
        return None
    try:
        map_index = int(row.get("map_index"))
    except (TypeError, ValueError):
        return None
    path = str(row.get("path") or "").strip()
    sha256 = str(row.get("sha256") or "").strip().casefold()
    contract_hash = str(row.get("contract_hash") or "").strip().casefold()
    map_name = str(row.get("map") or "")
    material_id = str(row.get("material_id") or "").strip()
    material_name = str(row.get("material_name") or "").strip()
    if (
        row.get("slot_role") != SUBSURFACE_AMOUNT_ROLE
        or row.get("manifest_role") != SUBSURFACE_COLOR_ROLE
        or row.get("usage") != PREVIEW_ONLY_USAGE
        or not material_id
        or not material_name
        or not _SHA256_RE.fullmatch(contract_hash)
        or map_index < 0
        or _map_role(map_name) != SUBSURFACE_AMOUNT_ROLE
        or not path
        or not _SHA256_RE.fullmatch(sha256)
    ):
        return None
    return (
        SUBSURFACE_AMOUNT_ROLE,
        SUBSURFACE_COLOR_ROLE,
        PREVIEW_ONLY_USAGE,
        material_id,
        material_name,
        contract_hash,
        map_index,
        map_name,
        _path_identity(path),
        sha256,
    )


def canonical_preview_role_fallbacks(rows):
    """Return v1 rows in the shared field and array ordering."""
    if not isinstance(rows, list):
        raise ValueError("preview_role_fallbacks must be an array")
    normalized = []
    signatures = set()
    for row in rows:
        signature = preview_role_fallback_signature(row)
        if signature is None or signature in signatures:
            raise ValueError("invalid or duplicate preview fallback row")
        signatures.add(signature)
        item = {
            field: row[field]
            for field in FALLBACK_CANONICAL_FIELDS
        }
        item["contract_hash"] = str(item["contract_hash"]).casefold()
        item["map_index"] = int(item["map_index"])
        item["path"] = str(Path(item["path"]).expanduser().resolve())
        item["sha256"] = str(item["sha256"]).casefold()
        normalized.append(item)
    normalized.sort(
        key=lambda row: (
            str(row["material_id"]),
            str(row["material_name"]).casefold(),
            str(row["contract_hash"]),
            int(row["map_index"]),
            str(row["map"]),
            _path_identity(row["path"]),
        )
    )
    return normalized


def preview_role_fallbacks_signature(rows):
    """Normalize a receipt list, rejecting malformed or duplicate rows."""
    try:
        normalized = canonical_preview_role_fallbacks(rows)
    except ValueError:
        return None
    return tuple(
        preview_role_fallback_signature(row)
        for row in normalized
    )


def _canonical_slot_files(rows):
    if not isinstance(rows, list):
        raise ValueError("slot_files must be an array")
    normalized = []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("slot_files contains a non-object row")
        item = {
            field: row[field]
            for field in SLOT_FILE_CANONICAL_FIELDS
            if field in row
        }
        if "map_index" not in item or "map" not in item or "path" not in item:
            raise ValueError("slot_files row is incomplete")
        for field in ("map_index", "spm_map_index", "stmat_map_index"):
            if field in item:
                item[field] = int(item[field])
        item["path"] = str(Path(item["path"]).expanduser().resolve())
        if "sha256" in item:
            item["sha256"] = str(item["sha256"]).casefold()
        normalized.append(item)
    normalized.sort(
        key=lambda row: (
            int(row["map_index"]),
            str(row["map"]),
            _path_identity(row["path"]),
        )
    )
    return normalized


def preview_receipt_core(receipt):
    """Return the canonical digest/cache/claim payload for one v2 receipt."""
    if not isinstance(receipt, dict):
        raise ValueError("preview receipt must be an object")
    fallbacks = canonical_preview_role_fallbacks(
        receipt.get(PREVIEW_ROLE_FALLBACKS_FIELD)
    )
    if not fallbacks:
        raise ValueError("preview receipt has no fallback rows")
    capabilities = receipt.get(RECEIPT_CAPABILITIES_FIELD)
    if not isinstance(capabilities, list):
        raise ValueError("preview receipt capabilities must be an array")
    return {
        "kind": str(receipt.get("kind") or ""),
        "version": int(receipt.get("version") or 0),
        "source_origin": str(receipt.get("source_origin") or ""),
        "material_id": str(receipt.get("material_id") or ""),
        "material_name": str(receipt.get("material_name") or ""),
        "slot_index_space": str(receipt.get("slot_index_space") or ""),
        "physical_capture_manifest": str(
            Path(
                str(receipt.get("physical_capture_manifest") or "")
            ).expanduser().resolve()
        ),
        "physical_capture_contract_sha256": str(
            receipt.get("physical_capture_contract_sha256") or ""
        ).casefold(),
        PREVIEW_FALLBACK_SCHEMA_FIELD: int(
            receipt.get(PREVIEW_FALLBACK_SCHEMA_FIELD) or 0
        ),
        RECEIPT_CAPABILITIES_FIELD: sorted(
            str(value) for value in capabilities
        ),
        RECEIPT_CLAIM_FIELD: str(
            receipt.get(RECEIPT_CLAIM_FIELD) or ""
        ),
        "slot_files": _canonical_slot_files(
            receipt.get("slot_files") or []
        ),
        PREVIEW_ROLE_FALLBACKS_FIELD: fallbacks,
    }


def preview_receipt_core_sha256(receipt):
    encoded = json.dumps(
        preview_receipt_core(receipt),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def finalize_preview_receipt(receipt):
    """Add the v2 capability, claim, core digest, and cache key."""
    normalized = copy.deepcopy(receipt)
    normalized["version"] = PREVIEW_RECEIPT_VERSION
    normalized[PREVIEW_FALLBACK_SCHEMA_FIELD] = (
        PREVIEW_FALLBACK_SCHEMA_VERSION
    )
    normalized[RECEIPT_CAPABILITIES_FIELD] = [
        PREVIEW_FALLBACK_CAPABILITY
    ]
    normalized[RECEIPT_CLAIM_FIELD] = PREVIEW_ONLY_USAGE
    normalized[PREVIEW_ROLE_FALLBACKS_FIELD] = (
        canonical_preview_role_fallbacks(
            normalized.get(PREVIEW_ROLE_FALLBACKS_FIELD)
        )
    )
    digest = preview_receipt_core_sha256(normalized)
    normalized[RECEIPT_CORE_SHA256_FIELD] = digest
    normalized[RECEIPT_CACHE_KEY_FIELD] = (
        f"{PREVIEW_ONLY_USAGE}:{digest}"
    )
    return normalized


def receipt_declares_preview_fallback(receipt):
    if not isinstance(receipt, dict):
        return False
    return bool(
        receipt.get(PREVIEW_ROLE_FALLBACKS_FIELD)
        or PREVIEW_FALLBACK_SCHEMA_FIELD in receipt
        or PREVIEW_FALLBACK_CAPABILITY
        in (receipt.get(RECEIPT_CAPABILITIES_FIELD) or [])
    )


def validate_preview_receipt(receipt, *, requested_usage):
    """Fail closed on schema/capability/claim/digest/cache mismatches."""
    if not isinstance(receipt, dict):
        raise ValueError("preview receipt must be an object")
    if int(receipt.get("version") or 0) != PREVIEW_RECEIPT_VERSION:
        raise ValueError("unsupported preview receipt version")
    if (
        int(receipt.get(PREVIEW_FALLBACK_SCHEMA_FIELD) or 0)
        != PREVIEW_FALLBACK_SCHEMA_VERSION
    ):
        raise ValueError("unsupported preview fallback schema")
    if receipt.get(RECEIPT_CAPABILITIES_FIELD) != [
        PREVIEW_FALLBACK_CAPABILITY
    ]:
        raise ValueError("unsupported preview fallback capability")
    if str(requested_usage or "") != PREVIEW_ONLY_USAGE:
        raise ValueError("preview fallback is forbidden for this consumer")
    if receipt.get(RECEIPT_CLAIM_FIELD) != PREVIEW_ONLY_USAGE:
        raise ValueError("preview receipt claim mismatch")
    if (
        receipt.get("kind") != PREVIEW_RECEIPT_KIND
        or receipt.get("source_origin")
        != PREVIEW_RECEIPT_SOURCE_ORIGIN
    ):
        raise ValueError("unsupported preview receipt identity")
    parent_material_id = str(receipt.get("material_id") or "").strip()
    parent_material_name = str(
        receipt.get("material_name") or ""
    ).strip()
    parent_contract_hash = str(
        receipt.get("physical_capture_contract_sha256") or ""
    ).strip().casefold()
    manifest_path = str(
        receipt.get("physical_capture_manifest") or ""
    ).strip()
    if (
        not parent_material_id
        or not parent_material_name
        or not _SHA256_RE.fullmatch(parent_contract_hash)
        or not manifest_path
    ):
        raise ValueError("preview receipt parent identity is incomplete")
    canonical_rows = canonical_preview_role_fallbacks(
        receipt.get(PREVIEW_ROLE_FALLBACKS_FIELD)
    )
    if canonical_rows != receipt.get(PREVIEW_ROLE_FALLBACKS_FIELD):
        raise ValueError("preview fallback rows are not canonically ordered")
    if any(
        tuple(row) != FALLBACK_CANONICAL_FIELDS
        for row in receipt.get(PREVIEW_ROLE_FALLBACKS_FIELD)
    ):
        raise ValueError("preview fallback row fields are not canonical")
    for row in canonical_rows:
        if (
            str(row["material_id"]) != parent_material_id
            or str(row["material_name"]) != parent_material_name
            or str(row["contract_hash"]).casefold()
            != parent_contract_hash
        ):
            raise ValueError("preview fallback parent identity mismatch")
    digest = preview_receipt_core_sha256(receipt)
    if receipt.get(RECEIPT_CORE_SHA256_FIELD) != digest:
        raise ValueError("preview receipt core digest mismatch")
    if receipt.get(RECEIPT_CACHE_KEY_FIELD) != (
        f"{PREVIEW_ONLY_USAGE}:{digest}"
    ):
        raise ValueError("preview receipt cache key mismatch")
    return copy.deepcopy(receipt)
