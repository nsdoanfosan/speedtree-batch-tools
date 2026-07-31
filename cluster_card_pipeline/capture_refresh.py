"""Request/finalize contract for SpeedTree camera-atlas captures.

SpeedTree Modeler 10.1 exposes mesh export on its command line, but not the
camera ``Export image`` operation.  The capture therefore remains an explicit
SpeedTree authoring action.  This module makes that action deterministic:

1. ``begin_camera_capture_request`` freezes the exact camera SPM, material,
   expected map set, resolution, and each map's pre-capture fingerprint.
2. The artist exports every camera map from SpeedTree.
3. ``finalize_camera_capture_request`` proves that the SPM stayed unchanged
   and every expected map was rewritten after the request, then emits a
   content-addressed receipt.

``ensure_camera_capture_refresh`` is the integration entry point used by the
Blender normalizer.  It accepts an already-valid receipt, finalizes an existing
request, or creates a new request and fails closed with an actionable message.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from .contract import ContractError, _fingerprint, _read_spm_root


CAPTURE_REQUEST_KIND = "speedtree_cluster_camera_capture_request"
CAPTURE_REQUEST_VERSION = 1
CAPTURE_RECEIPT_KIND = "speedtree_cluster_camera_capture_receipt"
CAPTURE_RECEIPT_VERSION = 1
REQUIRED_CAPTURE_MAPS = (
    "Color",
    "Opacity",
)
OPTIONAL_CAPTURE_MAPS = (
    "Normal",
    "Gloss",
    "SubsurfaceColor",
    "SubsurfaceAmount",
    "AO",
    "Height",
)


def _canonical_sha256(value):
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _write_json_atomic(path, payload):
    path = Path(path).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(temporary, path)
    return path


def _read_json(path, label):
    path = Path(path).expanduser().resolve()
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractError(f"{label} is unreadable: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ContractError(f"{label} must contain a JSON object: {path}")
    return value


def _same_path(first, second):
    return os.path.normcase(os.path.realpath(str(first))) == os.path.normcase(
        os.path.realpath(str(second))
    )


def _require_file(path, label):
    candidate = Path(path).expanduser().resolve()
    if not candidate.is_file():
        raise ContractError(f"{label} is missing: {candidate}")
    return candidate


def _capture_paths(manifest_path, request_path=None, receipt_path=None):
    manifest_path = Path(manifest_path).expanduser().resolve()
    base = manifest_path.stem.replace("_normalization_manifest", "")
    return {
        "request": (
            Path(request_path).expanduser().resolve()
            if request_path is not None
            else manifest_path.with_name(f"{base}_capture_request.json")
        ),
        "receipt": (
            Path(receipt_path).expanduser().resolve()
            if receipt_path is not None
            else manifest_path.with_name(f"{base}_capture_receipt.json")
        ),
    }


def camera_external_mesh_dependencies(camera_spm):
    """Return external Mesh asset files referenced by the camera SPM."""
    camera_spm = _require_file(camera_spm, "Camera SPM")
    root = _read_spm_root(camera_spm)
    output = []
    seen = set()
    for mesh in root.findall(".//Mesh"):
        if str(mesh.findtext("Embedded") or "").strip().casefold() != "false":
            continue
        filename = str(mesh.findtext("Filename") or "").strip()
        if not filename:
            continue
        dependency = (camera_spm.parent / filename).resolve()
        if not dependency.is_file():
            raise ContractError(
                "SpeedTree camera capture depends on a missing external mesh: "
                f"{dependency}"
            )
        key = str(dependency).casefold()
        if key not in seen:
            seen.add(key)
            output.append(dependency)
    return output


def _validate_tga_dimensions(texture, expected_size):
    texture = _require_file(texture, "Camera atlas")
    if texture.suffix.casefold() != ".tga":
        raise ContractError(f"Camera atlas must be TGA: {texture}")
    header = texture.read_bytes()[:18]
    if len(header) != 18:
        raise ContractError(f"Camera atlas has no valid TGA header: {texture}")
    actual = [
        int.from_bytes(header[12:14], "little"),
        int.from_bytes(header[14:16], "little"),
    ]
    if actual != list(expected_size):
        raise ContractError(
            f"Camera atlas dimensions changed: {texture.name}: "
            f"{actual} != {list(expected_size)}"
        )
    return actual


def _material_capture_rows(contract, roles=None):
    material = contract.get("material") or {}
    expected_size = [int(material.get("width") or 0), int(material.get("height") or 0)]
    if min(expected_size) <= 0:
        raise ContractError("Camera contract material has invalid atlas dimensions.")
    maps = material.get("maps") or {}
    missing = [name for name in REQUIRED_CAPTURE_MAPS if name not in maps]
    if missing:
        raise ContractError(
            "Camera contract is missing required SpeedTree export maps: "
            + ", ".join(missing)
        )
    rows = []
    ordered_roles = (
        list(roles)
        if roles is not None
        else [
            *REQUIRED_CAPTURE_MAPS,
            *(name for name in OPTIONAL_CAPTURE_MAPS if name in maps),
        ]
    )
    for role in ordered_roles:
        map_row = maps.get(role) or {}
        texture = _require_file(map_row.get("path"), f"Camera atlas {role}")
        actual_size = _validate_tga_dimensions(texture, expected_size)
        row = _fingerprint(texture)
        row.update(
            {
                "role": role,
                "expected_size": list(expected_size),
                "actual_size": actual_size,
            }
        )
        rows.append(row)
    return rows


def _dependency_rows(camera_spm, extra_dependencies=()):
    paths = [
        _require_file(camera_spm, "Camera SPM"),
        *camera_external_mesh_dependencies(camera_spm),
    ]
    paths.extend(
        _require_file(value, "Camera capture dependency")
        for value in extra_dependencies
    )
    output = []
    seen = set()
    for path in paths:
        key = str(path).casefold()
        if key in seen:
            continue
        seen.add(key)
        output.append(_fingerprint(path))
    return output


def _request_payload(contract, camera_spm, extra_dependencies=()):
    camera_spm = _require_file(camera_spm, "Camera SPM")
    contract_camera = contract.get("camera_spm") or {}
    if not _same_path(contract_camera.get("path", ""), camera_spm):
        raise ContractError("Camera contract targets another SPM.")
    current_camera = _fingerprint(camera_spm)
    if current_camera["sha256"] != contract_camera.get("sha256"):
        raise ContractError(
            "Camera SPM content changed after the authoritative UV reference was built. "
            "Rebuild the camera reference before requesting an atlas capture."
        )
    created_at_ns = time.time_ns()
    payload = {
        "kind": CAPTURE_REQUEST_KIND,
        "version": CAPTURE_REQUEST_VERSION,
        "status": "capture_required",
        "created_at_ns": created_at_ns,
        "created_at_utc": datetime.fromtimestamp(
            created_at_ns / 1_000_000_000,
            tz=timezone.utc,
        ).isoformat(),
        "camera_spm": current_camera,
        "camera": copy.deepcopy(contract.get("camera") or {}),
        "material": {
            "id": int((contract.get("material") or {}).get("id")),
            "name": str((contract.get("material") or {}).get("name") or ""),
            "width": int((contract.get("material") or {}).get("width") or 0),
            "height": int((contract.get("material") or {}).get("height") or 0),
        },
        "dependencies": _dependency_rows(camera_spm, extra_dependencies),
        "maps_before_capture": _material_capture_rows(contract),
        "required_roles": list(REQUIRED_CAPTURE_MAPS),
        "optional_roles": [
            name
            for name in OPTIONAL_CAPTURE_MAPS
            if name in (contract.get("material") or {}).get("maps", {})
        ],
    }
    payload["request_sha256"] = _canonical_sha256(payload)
    return payload


def begin_camera_capture_request(
    contract,
    camera_spm,
    manifest_path,
    *,
    request_path=None,
    receipt_path=None,
    extra_dependencies=(),
):
    """Freeze the exact pre-capture state and write a capture request."""
    paths = _capture_paths(manifest_path, request_path, receipt_path)
    request = _request_payload(contract, camera_spm, extra_dependencies)
    request["receipt_path"] = str(paths["receipt"])
    request["request_sha256"] = _canonical_sha256(
        {key: value for key, value in request.items() if key != "request_sha256"}
    )
    _write_json_atomic(paths["request"], request)
    return {
        "status": "capture_required",
        "request": request,
        "request_path": str(paths["request"]),
        "receipt_path": str(paths["receipt"]),
    }


def _validate_request_identity(request):
    if (
        request.get("kind") != CAPTURE_REQUEST_KIND
        or int(request.get("version") or 0) != CAPTURE_REQUEST_VERSION
    ):
        raise ContractError("SpeedTree camera capture request kind/version is unsupported.")
    expected = str(request.get("request_sha256") or "")
    actual = _canonical_sha256(
        {key: value for key, value in request.items() if key != "request_sha256"}
    )
    if not expected or expected != actual:
        raise ContractError("SpeedTree camera capture request hash is invalid.")


def _current_map_rows(request):
    output = []
    before_rows = request.get("maps_before_capture") or []
    before_by_role = {str(row.get("role") or ""): row for row in before_rows}
    if not set(REQUIRED_CAPTURE_MAPS).issubset(before_by_role):
        raise ContractError("Capture request does not contain Color and Opacity.")
    created_at_ns = int(request.get("created_at_ns") or 0)
    for role in [
        *REQUIRED_CAPTURE_MAPS,
        *(
            name
            for name in OPTIONAL_CAPTURE_MAPS
            if name in before_by_role
        ),
    ]:
        before = before_by_role[role]
        texture = _require_file(before.get("path"), f"Camera atlas {role}")
        actual_size = _validate_tga_dimensions(texture, before["expected_size"])
        current = _fingerprint(texture)
        current.update(
            {
                "role": role,
                "expected_size": list(before["expected_size"]),
                "actual_size": actual_size,
                "sha256_changed": current["sha256"] != before.get("sha256"),
                "rewritten_after_request": int(current["mtime_ns"]) > created_at_ns,
                "required": role in REQUIRED_CAPTURE_MAPS,
            }
        )
        output.append(current)
    return output


def finalize_camera_capture_request(
    request_path,
    *,
    receipt_path=None,
):
    """Validate a completed SpeedTree Camera Export and write its receipt."""
    request_path = Path(request_path).expanduser().resolve()
    request = _read_json(request_path, "SpeedTree camera capture request")
    _validate_request_identity(request)
    camera_spm = _require_file(
        (request.get("camera_spm") or {}).get("path"),
        "Camera SPM",
    )
    current_camera = _fingerprint(camera_spm)
    if current_camera["sha256"] != (request.get("camera_spm") or {}).get("sha256"):
        raise ContractError(
            "Camera SPM changed after capture was requested. Discard this request, "
            "rebuild the authoritative camera reference, and start again."
        )
    current_dependencies = _dependency_rows(
        camera_spm,
        [
            row["path"]
            for row in request.get("dependencies") or []
            if not _same_path(row.get("path", ""), camera_spm)
            and Path(row.get("path", "")).is_file()
        ],
    )
    requested_dependencies = {
        str(Path(row["path"]).resolve()).casefold(): row
        for row in request.get("dependencies") or []
    }
    for row in current_dependencies:
        before = requested_dependencies.get(str(Path(row["path"]).resolve()).casefold())
        if before is None or row["sha256"] != before.get("sha256"):
            raise ContractError(
                "A camera capture dependency changed after the request: "
                f"{row['path']}"
            )

    maps = _current_map_rows(request)
    not_rewritten = [
        row["role"]
        for row in maps
        if row["required"] and not row["rewritten_after_request"]
    ]
    if not_rewritten:
        raise ContractError(
            "SpeedTree Camera Export is not complete. Export Color and Opacity "
            f"after the request was created. Waiting for: {', '.join(not_rewritten)}. "
            f"Request: {request_path}"
        )

    finalized_at_ns = time.time_ns()
    receipt = {
        "kind": CAPTURE_RECEIPT_KIND,
        "version": CAPTURE_RECEIPT_VERSION,
        "status": "ready",
        "request_path": str(request_path),
        "request_sha256": request["request_sha256"],
        "finalized_at_ns": finalized_at_ns,
        "finalized_at_utc": datetime.fromtimestamp(
            finalized_at_ns / 1_000_000_000,
            tz=timezone.utc,
        ).isoformat(),
        "camera_spm": current_camera,
        "camera": copy.deepcopy(request.get("camera") or {}),
        "material": copy.deepcopy(request.get("material") or {}),
        "dependencies": current_dependencies,
        "textures": maps,
        "all_required_maps_rewritten_after_request": True,
        "all_maps_rewritten_after_request": all(
            row["rewritten_after_request"] for row in maps
        ),
        "changed_content_roles": sorted(
            row["role"] for row in maps if row["sha256_changed"]
        ),
    }
    receipt["receipt_sha256"] = _canonical_sha256(receipt)
    resolved_receipt = (
        Path(receipt_path).expanduser().resolve()
        if receipt_path is not None
        else Path(request.get("receipt_path") or request_path.with_name(
            request_path.name.replace("_request.json", "_receipt.json")
        )).expanduser().resolve()
    )
    _write_json_atomic(resolved_receipt, receipt)
    return {
        "status": "ready",
        "receipt": receipt,
        "request_path": str(request_path),
        "receipt_path": str(resolved_receipt),
    }


def validate_camera_capture_receipt(
    receipt_path,
    contract,
    camera_spm,
):
    """Prove that a receipt still matches the current SPM and all eight maps."""
    receipt_path = Path(receipt_path).expanduser().resolve()
    receipt = _read_json(receipt_path, "SpeedTree camera capture receipt")
    if (
        receipt.get("kind") != CAPTURE_RECEIPT_KIND
        or int(receipt.get("version") or 0) != CAPTURE_RECEIPT_VERSION
        or receipt.get("status") != "ready"
    ):
        raise ContractError("SpeedTree camera capture receipt is unsupported or incomplete.")
    expected = str(receipt.get("receipt_sha256") or "")
    actual = _canonical_sha256(
        {key: value for key, value in receipt.items() if key != "receipt_sha256"}
    )
    if not expected or expected != actual:
        raise ContractError("SpeedTree camera capture receipt hash is invalid.")
    current_camera = _fingerprint(_require_file(camera_spm, "Camera SPM"))
    if current_camera["sha256"] != (receipt.get("camera_spm") or {}).get("sha256"):
        raise ContractError("Camera SPM changed after the atlas capture was finalized.")
    material = contract.get("material") or {}
    if (
        int(material.get("id")) != int((receipt.get("material") or {}).get("id"))
        or str(material.get("name") or "")
        != str((receipt.get("material") or {}).get("name") or "")
        or [int(material.get("width")), int(material.get("height"))]
        != [
            int((receipt.get("material") or {}).get("width")),
            int((receipt.get("material") or {}).get("height")),
        ]
    ):
        raise ContractError("Camera capture receipt targets another material contract.")
    current_by_role = {
        row["role"]: row
        for row in _material_capture_rows(contract, roles=REQUIRED_CAPTURE_MAPS)
    }
    receipt_by_role = {
        str(row.get("role") or ""): row for row in receipt.get("textures") or []
    }
    if not set(REQUIRED_CAPTURE_MAPS).issubset(receipt_by_role):
        raise ContractError("Camera capture receipt does not contain Color and Opacity.")
    for role in REQUIRED_CAPTURE_MAPS:
        current = current_by_role[role]
        recorded = receipt_by_role[role]
        if (
            not _same_path(current["path"], recorded.get("path", ""))
            or current["sha256"] != recorded.get("sha256")
            or current["size"] != recorded.get("size")
        ):
            raise ContractError(
                f"Camera atlas {role} changed after the capture receipt was finalized."
            )
    return receipt


def _apply_receipt_to_contract(contract, receipt, receipt_path):
    updated = copy.deepcopy(contract)
    maps = (updated.get("material") or {}).get("maps") or {}
    by_role = {
        str(row.get("role") or ""): row for row in receipt.get("textures") or []
    }
    for role, map_row in maps.items():
        if role not in by_role:
            continue
        texture = by_role[role]
        map_row["file_size"] = int(texture["size"])
        map_row["sha256"] = texture["sha256"]
        map_row["actual_size"] = list(texture["actual_size"])
    updated["camera_capture_receipt"] = {
        "kind": receipt["kind"],
        "version": receipt["version"],
        "path": str(Path(receipt_path).resolve()),
        "sha256": receipt["receipt_sha256"],
        "request_sha256": receipt["request_sha256"],
    }
    return updated


def _refresh_manifest(manifest_path, contract):
    manifest_path = Path(manifest_path).expanduser().resolve()
    manifest = _read_json(manifest_path, "Camera normalization manifest")
    manifest["camera_spm"] = copy.deepcopy(contract.get("camera_spm") or {})
    manifest["camera"] = copy.deepcopy(contract.get("camera") or {})
    manifest["material"] = copy.deepcopy(contract.get("material") or {})
    manifest["camera_capture_receipt"] = copy.deepcopy(
        contract.get("camera_capture_receipt") or {}
    )
    _write_json_atomic(manifest_path, manifest)
    return manifest


def ensure_camera_capture_refresh(
    contract,
    camera_spm,
    manifest_path,
    *,
    request_path=None,
    receipt_path=None,
    extra_dependencies=(),
):
    """Return a receipt-backed contract or create/await a capture request."""
    paths = _capture_paths(manifest_path, request_path, receipt_path)
    if paths["receipt"].is_file():
        try:
            receipt = validate_camera_capture_receipt(
                paths["receipt"],
                contract,
                camera_spm,
            )
        except ContractError:
            receipt = None
        if receipt is not None:
            updated = _apply_receipt_to_contract(contract, receipt, paths["receipt"])
            _refresh_manifest(manifest_path, updated)
            return {
                "status": "ready",
                "contract": updated,
                "receipt": receipt,
                "request_path": str(paths["request"]),
                "receipt_path": str(paths["receipt"]),
                "manifest_path": str(Path(manifest_path).resolve()),
            }

    if paths["request"].is_file():
        finalized = finalize_camera_capture_request(
            paths["request"],
            receipt_path=paths["receipt"],
        )
        updated = _apply_receipt_to_contract(
            contract,
            finalized["receipt"],
            paths["receipt"],
        )
        _refresh_manifest(manifest_path, updated)
        return {
            **finalized,
            "contract": updated,
            "manifest_path": str(Path(manifest_path).resolve()),
        }

    started = begin_camera_capture_request(
        contract,
        camera_spm,
        manifest_path,
        request_path=paths["request"],
        receipt_path=paths["receipt"],
        extra_dependencies=extra_dependencies,
    )
    raise ContractError(
        "SpeedTree camera atlas refresh is required. A capture request was created. "
        "Open the named camera in SpeedTree, use Camera Export to rewrite Color and "
        f"Opacity, then run normalization again. Request: {started['request_path']}"
    )


__all__ = [
    "CAPTURE_RECEIPT_KIND",
    "CAPTURE_RECEIPT_VERSION",
    "CAPTURE_REQUEST_KIND",
    "CAPTURE_REQUEST_VERSION",
    "OPTIONAL_CAPTURE_MAPS",
    "REQUIRED_CAPTURE_MAPS",
    "begin_camera_capture_request",
    "camera_external_mesh_dependencies",
    "ensure_camera_capture_refresh",
    "finalize_camera_capture_request",
    "validate_camera_capture_receipt",
]
