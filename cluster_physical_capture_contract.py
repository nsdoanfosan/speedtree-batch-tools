"""Fail-closed validation for finalized Blender physical-capture delivery.

The Blender add-on writes a self-hashed capture contract, eight fingerprinted
maps, and a normalization receipt that binds the manifest bytes.  This module
validates that evidence outside Blender so no host-side no-op or Atlas export
can route around plane, extent, coverage, or fingerprint checks.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path


CAPTURE_KIND = "speedtree_cluster_blender_auto_capture"
CAPTURE_CONTRACT_KIND = "speedtree_cluster_physical_capture_fit"
CAPTURE_WORKFLOW = "PHYSICAL_DIRECT_CAPTURE"
DIRECT_UV_SOURCE = "same_blender_physical_capture_projection"
FRAME_POLICY = "physical_target_uniform_whole_source_fit"
NORMALIZATION_RECEIPT_KIND = "speedtree_cluster_sync_normalization"
REQUIRED_MAP_ROLES = (
    "Color",
    "Opacity",
    "Normal",
    "Gloss",
    "SubsurfaceColor",
    "SubsurfaceAmount",
    "AO",
    "Height",
)
PLANE_BASES = {
    "XY": {
        "right": (1.0, 0.0, 0.0),
        "up": (0.0, 1.0, 0.0),
        "normal": (0.0, 0.0, 1.0),
        "view_direction": (0.0, 0.0, -1.0),
        "rotation_degrees": 0.0,
        "axis_indices": (0, 1, 2),
    },
    "XZ": {
        "right": (1.0, 0.0, 0.0),
        "up": (0.0, 0.0, 1.0),
        "normal": (0.0, -1.0, 0.0),
        "view_direction": (0.0, 1.0, 0.0),
        "rotation_degrees": 90.0,
        "axis_indices": (0, 2, 1),
    },
    "YZ": {
        "right": (0.0, 1.0, 0.0),
        "up": (0.0, 0.0, 1.0),
        "normal": (1.0, 0.0, 0.0),
        "view_direction": (-1.0, 0.0, 0.0),
        "rotation_degrees": 90.0,
        "axis_indices": (1, 2, 0),
    },
}


class PhysicalCaptureValidationError(ValueError):
    """A finalized physical-capture delivery is not safe to consume."""

    def __init__(self, code, message):
        self.code = str(code)
        super().__init__(message)


def canonical_sha256(value):
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def file_fingerprint(path):
    """Return one stable full-file fingerprint or fail if the file changes."""
    candidate = Path(path).expanduser().absolute()
    try:
        if not candidate.is_file():
            raise PhysicalCaptureValidationError(
                "coverage_invalid",
                f"Physical-capture file is missing: {candidate}",
            )
        for _attempt in range(2):
            before = candidate.stat()
            digest = hashlib.sha256()
            with candidate.open("rb") as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(block)
            after = candidate.stat()
            if (before.st_size, before.st_mtime_ns) == (
                after.st_size,
                after.st_mtime_ns,
            ):
                return {
                    "path": str(candidate),
                    "size": after.st_size,
                    "sha256": digest.hexdigest(),
                }
    except PhysicalCaptureValidationError:
        raise
    except OSError as exc:
        raise PhysicalCaptureValidationError(
            "fingerprint_mismatch",
            f"Physical-capture file could not be fingerprinted: {candidate}: {exc}",
        ) from exc
    raise PhysicalCaptureValidationError(
        "fingerprint_mismatch",
        f"Physical-capture file changed while hashing: {candidate}",
    )


def _normalized_path(path):
    return os.path.normcase(
        os.path.abspath(str(Path(path).expanduser()))
    ).casefold()


def _same_path(first, second):
    try:
        return _normalized_path(first) == _normalized_path(second)
    except (OSError, TypeError, ValueError):
        return False


def _load_json(path, label):
    candidate = Path(path).expanduser().absolute()
    try:
        payload = json.loads(candidate.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PhysicalCaptureValidationError(
            "schema_invalid", f"{label} is missing or invalid JSON: {candidate}: {exc}"
        ) from exc
    if not isinstance(payload, dict):
        raise PhysicalCaptureValidationError(
            "schema_invalid", f"{label} must contain one JSON object: {candidate}"
        )
    return candidate, payload


def _finite_number(value, label, *, positive=False):
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise PhysicalCaptureValidationError(
            "extent_invalid", f"{label} is not numeric"
        ) from exc
    if not math.isfinite(result) or (positive and result <= 0.0):
        raise PhysicalCaptureValidationError(
            "extent_invalid", f"{label} must be finite"
            + (" and positive" if positive else "")
        )
    return result


def _integer(value, label, *, validation_error="schema_invalid", minimum=None):
    try:
        result = int(value)
        numeric = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise PhysicalCaptureValidationError(
            validation_error, f"{label} is not an integer"
        ) from exc
    if not math.isfinite(numeric) or numeric != result:
        raise PhysicalCaptureValidationError(
            validation_error, f"{label} is not an integer"
        )
    if minimum is not None and result < minimum:
        raise PhysicalCaptureValidationError(
            validation_error, f"{label} must be at least {minimum}"
        )
    return result


def _sha256_text(value, label, *, validation_error="fingerprint_mismatch"):
    result = str(value or "").strip().casefold()
    if len(result) != 64 or any(character not in "0123456789abcdef" for character in result):
        raise PhysicalCaptureValidationError(
            validation_error, f"{label} is not a SHA-256 fingerprint"
        )
    return result


def _vector(value, label):
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise PhysicalCaptureValidationError(
            "orientation_mismatch", f"{label} must contain three values"
        )
    return tuple(_finite_number(component, label) for component in value)


def _dot(first, second):
    return sum(a * b for a, b in zip(first, second))


def _cross(first, second):
    return (
        first[1] * second[2] - first[2] * second[1],
        first[2] * second[0] - first[0] * second[2],
        first[0] * second[1] - first[1] * second[0],
    )


def _vector_close(first, second, tolerance=1.0e-7):
    return max(abs(a - b) for a, b in zip(first, second)) <= tolerance


def _validate_orientation(frame, expected_plane):
    plane = str(frame.get("plane") or "").upper()
    if plane not in PLANE_BASES:
        raise PhysicalCaptureValidationError(
            "orientation_mismatch", f"Unsupported physical-capture plane: {plane!r}"
        )
    if expected_plane is not None and plane != str(expected_plane).upper():
        raise PhysicalCaptureValidationError(
            "orientation_mismatch",
            f"Physical-capture plane {plane} does not match required "
            f"{str(expected_plane).upper()}",
        )
    expected = PLANE_BASES[plane]
    vectors = {
        name: _vector(frame.get(name), f"capture frame {name}")
        for name in ("right", "up", "normal", "view_direction")
    }
    for name, value in vectors.items():
        if not _vector_close(value, expected[name]):
            raise PhysicalCaptureValidationError(
                "orientation_mismatch",
                f"{plane} capture {name} is {value}, expected {expected[name]}",
            )
    right, up, normal = (
        vectors["right"],
        vectors["up"],
        vectors["normal"],
    )
    orthogonality = max(
        abs(_dot(right, up)),
        abs(_dot(right, normal)),
        abs(_dot(up, normal)),
    )
    handedness = _dot(_cross(right, up), normal)
    recorded_orthogonality = _finite_number(
        frame.get("orthogonality_error"), "capture orthogonality error"
    )
    recorded_handedness = _finite_number(
        frame.get("handedness"), "capture handedness"
    )
    rotation = _finite_number(
        frame.get("rotation_degrees"), "capture rotation"
    )
    if (
        orthogonality > 1.0e-7
        or abs(handedness - 1.0) > 1.0e-7
        or abs(recorded_orthogonality - orthogonality) > 1.0e-7
        or abs(recorded_handedness - handedness) > 1.0e-7
        or abs(rotation - expected["rotation_degrees"]) > 1.0e-7
    ):
        raise PhysicalCaptureValidationError(
            "orientation_mismatch",
            f"{plane} capture basis is not its deterministic right-handed frame",
        )
    center = _vector(frame.get("center"), "capture center")
    camera = _vector(frame.get("camera_location"), "capture camera location")
    displacement = tuple(camera[index] - center[index] for index in range(3))
    normal_distance = _dot(displacement, normal)
    perpendicular = tuple(
        displacement[index] - normal[index] * normal_distance
        for index in range(3)
    )
    if normal_distance <= 0.0 or math.sqrt(_dot(perpendicular, perpendicular)) > 1.0e-7:
        raise PhysicalCaptureValidationError(
            "orientation_mismatch",
            "Physical-capture camera is not placed on the declared positive normal",
        )
    return {
        "plane": plane,
        "right": list(right),
        "up": list(up),
        "normal": list(normal),
        "view_direction": list(vectors["view_direction"]),
        "rotation_degrees": rotation,
    }


def _bounds(frame, prefix):
    minimum = _vector(frame.get(f"{prefix}_world_bounds_min"), f"{prefix} bounds min")
    maximum = _vector(frame.get(f"{prefix}_world_bounds_max"), f"{prefix} bounds max")
    if any(maximum[index] < minimum[index] for index in range(3)):
        raise PhysicalCaptureValidationError(
            "extent_invalid", f"{prefix} physical-capture bounds are inverted"
        )
    return minimum, maximum


def _validate_extent(frame, plane, *, expected_target_meters, expected_padding_ratio):
    if (
        frame.get("policy") != FRAME_POLICY
        or frame.get("workflow_mode") != CAPTURE_WORKFLOW
        or frame.get("direct_uv_source") != DIRECT_UV_SOURCE
        or str(frame.get("unit_system") or "") != "METRIC"
    ):
        raise PhysicalCaptureValidationError(
            "extent_invalid", "Physical-capture frame policy or unit system is invalid"
        )
    width = _finite_number(frame.get("width"), "capture width", positive=True)
    height = _finite_number(frame.get("height"), "capture height", positive=True)
    content_width = _finite_number(
        frame.get("content_width"), "capture content width", positive=True
    )
    content_height = _finite_number(
        frame.get("content_height"), "capture content height", positive=True
    )
    raw_width = _finite_number(
        frame.get("raw_content_width"), "raw capture content width", positive=True
    )
    raw_height = _finite_number(
        frame.get("raw_content_height"), "raw capture content height", positive=True
    )
    fitted_depth = _finite_number(
        frame.get("fitted_depth"), "fitted capture depth", positive=True
    )
    fit_scale = _finite_number(frame.get("fit_scale"), "capture fit scale", positive=True)
    scale_length = _finite_number(
        frame.get("scale_length"), "capture scale length", positive=True
    )
    padding = _finite_number(frame.get("padding_ratio"), "capture padding ratio")
    if padding < 0.0:
        raise PhysicalCaptureValidationError(
            "extent_invalid", "Physical-capture padding ratio cannot be negative"
        )
    if expected_padding_ratio is not None and abs(
        padding - float(expected_padding_ratio)
    ) > 1.0e-7:
        raise PhysicalCaptureValidationError(
            "extent_invalid", "Physical-capture padding does not match its recipe"
        )
    targets_m = frame.get("target_meters") or []
    targets_bu = frame.get("target_blender_units") or []
    if len(targets_m) != 2 or len(targets_bu) != 2:
        raise PhysicalCaptureValidationError(
            "extent_invalid", "Physical-capture target extent evidence is incomplete"
        )
    targets_m = [
        _finite_number(value, "capture target meters", positive=True)
        for value in targets_m
    ]
    targets_bu = [
        _finite_number(value, "capture target Blender units", positive=True)
        for value in targets_bu
    ]
    target_meters = (
        float(expected_target_meters)
        if expected_target_meters is not None
        else targets_m[0]
    )
    tolerance = max(width * 1.0e-6, 1.0e-9)
    if (
        abs(width - height) > tolerance
        or max(abs(value - width) for value in targets_bu) > tolerance
        or max(abs(value - target_meters) for value in targets_m) > 1.0e-6
        or max(
            abs(targets_bu[index] * scale_length - targets_m[index])
            for index in range(2)
        )
        > 1.0e-6
        or content_width > width + tolerance
        or content_height > height + tolerance
        or abs(raw_width * fit_scale - content_width) > tolerance
        or abs(raw_height * fit_scale - content_height) > tolerance
    ):
        raise PhysicalCaptureValidationError(
            "extent_invalid", "Physical-capture content does not fit its declared extent"
        )
    expected_coverage = 1.0 / (1.0 + 2.0 * padding)
    coverage = max(content_width, content_height) / width
    if abs(coverage - expected_coverage) > 1.0e-6:
        raise PhysicalCaptureValidationError(
            "extent_invalid", "Physical-capture padding/coverage ratio is inconsistent"
        )
    raw_min, raw_max = _bounds(frame, "raw")
    fitted_min, fitted_max = _bounds(frame, "fitted")
    right_axis, up_axis, normal_axis = PLANE_BASES[plane]["axis_indices"]
    raw_axis_extents = [
        raw_max[index] - raw_min[index] for index in range(3)
    ]
    fitted_axis_extents = [
        fitted_max[index] - fitted_min[index] for index in range(3)
    ]
    # Blender's mathutils matrices store floats at single precision.  The
    # analytically calculated fitted dimensions and the transformed AABB can
    # therefore differ by a few float32 ULPs even though they describe the
    # same 0.1 m frame.  Keep the broader tolerance isolated to that comparison;
    # raw axis coverage and all declared physical extents remain strict.
    fitted_bounds_tolerance = max(width * 1.0e-5, 1.0e-8)
    if (
        abs(raw_axis_extents[right_axis] - raw_width) > 1.0e-6
        or abs(raw_axis_extents[up_axis] - raw_height) > 1.0e-6
        or abs(fitted_axis_extents[right_axis] - content_width)
        > fitted_bounds_tolerance
        or abs(fitted_axis_extents[up_axis] - content_height)
        > fitted_bounds_tolerance
        or abs(fitted_axis_extents[normal_axis] - fitted_depth)
        > fitted_bounds_tolerance
    ):
        raise PhysicalCaptureValidationError(
            "extent_invalid",
            "Physical-capture bounds do not cover the declared frame axes: "
            f"raw={raw_axis_extents}, declared_raw={[raw_width, raw_height]}, "
            f"fitted={fitted_axis_extents}, "
            f"declared_fitted={[content_width, content_height, fitted_depth]}, "
            f"axes={[right_axis, up_axis, normal_axis]}, "
            f"tolerance={fitted_bounds_tolerance}",
        )
    return {
        "target_meters": targets_m,
        "target_blender_units": targets_bu,
        "content_width": content_width,
        "content_height": content_height,
        "coverage_ratio": coverage,
        "padding_ratio": padding,
        "fit_scale": fit_scale,
    }


def _map_rows(rows, label):
    if not isinstance(rows, list):
        raise PhysicalCaptureValidationError(
            "coverage_invalid", f"{label} has no physical-capture map rows"
        )
    by_role = {}
    for row in rows:
        if not isinstance(row, dict):
            raise PhysicalCaptureValidationError(
                "coverage_invalid", f"{label} contains an invalid map row"
            )
        role = str(row.get("role") or "")
        if role not in REQUIRED_MAP_ROLES or role in by_role:
            raise PhysicalCaptureValidationError(
                "coverage_invalid", f"{label} contains a duplicate or unsupported role: {role!r}"
            )
        by_role[role] = row
    if set(by_role) != set(REQUIRED_MAP_ROLES):
        missing = sorted(set(REQUIRED_MAP_ROLES) - set(by_role))
        extra = sorted(set(by_role) - set(REQUIRED_MAP_ROLES))
        raise PhysicalCaptureValidationError(
            "coverage_invalid",
            f"{label} map coverage is incomplete; missing={missing}, extra={extra}",
        )
    return by_role


def validate_physical_capture_manifest(
    manifest_path,
    *,
    expected_blend=None,
    expected_plane=None,
    expected_resolution=None,
    expected_target_meters=0.1,
    expected_padding_ratio=None,
    verify_map_files=True,
):
    """Validate one finalized manifest and return immutable evidence."""
    manifest_path, manifest = _load_json(manifest_path, "Physical-capture manifest")
    manifest_version = _integer(
        manifest.get("version"),
        "Physical-capture manifest version",
        minimum=2,
    )
    if (
        manifest.get("kind") != CAPTURE_KIND
        or manifest_version < 2
        or manifest.get("workflow_mode") != CAPTURE_WORKFLOW
        or manifest.get("direct_uv_source") != DIRECT_UV_SOURCE
        or manifest.get("normalization_status") != "finalized"
    ):
        raise PhysicalCaptureValidationError(
            "schema_invalid", "Physical-capture manifest is not a finalized production receipt"
        )
    if expected_blend is not None and not _same_path(
        manifest.get("blend"), expected_blend
    ):
        raise PhysicalCaptureValidationError(
            "fingerprint_mismatch", "Physical-capture manifest belongs to another blend"
        )
    contract = manifest.get("physical_capture_contract")
    if not isinstance(contract, dict):
        raise PhysicalCaptureValidationError(
            "schema_invalid", "Physical-capture manifest has no embedded contract"
        )
    contract_version = _integer(
        contract.get("version"),
        "Physical-capture contract version",
    )
    if (
        contract.get("kind") != CAPTURE_CONTRACT_KIND
        or contract_version != 1
        or contract.get("workflow_mode") != CAPTURE_WORKFLOW
        or contract.get("direct_uv_source") != DIRECT_UV_SOURCE
    ):
        raise PhysicalCaptureValidationError(
            "schema_invalid", "Embedded physical-capture contract kind/workflow is invalid"
        )
    if expected_blend is not None and not _same_path(
        contract.get("source_blend"), expected_blend
    ):
        raise PhysicalCaptureValidationError(
            "fingerprint_mismatch", "Physical-capture contract belongs to another blend"
        )
    recorded_contract_hash = _sha256_text(
        contract.get("contract_sha256"),
        "Physical-capture contract hash",
    )
    hash_payload = {
        key: value for key, value in contract.items() if key != "contract_sha256"
    }
    actual_contract_hash = canonical_sha256(hash_payload)
    if recorded_contract_hash != actual_contract_hash:
        raise PhysicalCaptureValidationError(
            "fingerprint_mismatch", "Physical-capture contract hash is missing or stale"
        )
    if str(manifest.get("physical_capture_contract_sha256") or "").casefold() != recorded_contract_hash:
        raise PhysicalCaptureValidationError(
            "fingerprint_mismatch", "Manifest and physical-capture contract hashes disagree"
        )
    if not _same_path(contract.get("capture_manifest"), manifest_path):
        raise PhysicalCaptureValidationError(
            "fingerprint_mismatch", "Physical-capture contract points to another manifest"
        )
    contract_sources = contract.get("source_objects")
    if not isinstance(contract_sources, list) or not contract_sources:
        raise PhysicalCaptureValidationError(
            "coverage_invalid", "Physical-capture contract has no source-object coverage"
        )
    source_projection = []
    for source in contract_sources:
        if not isinstance(source, dict) or not str(source.get("name") or ""):
            raise PhysicalCaptureValidationError(
                "coverage_invalid", "Physical-capture source-object evidence is incomplete"
            )
        row = {
            "name": str(source["name"]),
            "vertices": _integer(
                source.get("vertices"),
                "Physical-capture source vertex count",
                validation_error="coverage_invalid",
                minimum=1,
            ),
            "polygons": _integer(
                source.get("polygons"),
                "Physical-capture source polygon count",
                validation_error="coverage_invalid",
                minimum=1,
            ),
        }
        _sha256_text(
            source.get("evaluated_sha256"),
            "Physical-capture evaluated source hash",
            validation_error="coverage_invalid",
        )
        source_projection.append(row)
    if manifest.get("source_objects") != source_projection:
        raise PhysicalCaptureValidationError(
            "coverage_invalid",
            "Manifest source-object coverage differs from its capture contract",
        )
    attachment_pivots = contract.get("attachment_pivots")
    if not isinstance(attachment_pivots, list) or not attachment_pivots:
        raise PhysicalCaptureValidationError(
            "coverage_invalid", "Physical-capture contract has no attachment coverage"
        )
    prototype_indices = set()
    for pivot in attachment_pivots:
        if not isinstance(pivot, dict) or not str(pivot.get("prototype_asset") or ""):
            raise PhysicalCaptureValidationError(
                "coverage_invalid", "Physical-capture attachment evidence is incomplete"
            )
        prototype_index = _integer(
            pivot.get("prototype_index"),
            "Physical-capture prototype index",
            validation_error="coverage_invalid",
            minimum=1,
        )
        _integer(
            pivot.get("xml_bone_id"),
            "Physical-capture XML bone id",
            validation_error="coverage_invalid",
            minimum=0,
        )
        for field in ("source_world", "fitted_capture_world", "normalized_local"):
            _vector(pivot.get(field), f"Physical-capture attachment {field}")
        if prototype_index in prototype_indices:
            raise PhysicalCaptureValidationError(
                "coverage_invalid", "Physical-capture prototype indices are duplicated"
            )
        prototype_indices.add(prototype_index)
    frame = contract.get("frame") or {}
    if manifest.get("frame") != frame:
        raise PhysicalCaptureValidationError(
            "fingerprint_mismatch", "Manifest frame disagrees with its embedded contract"
        )
    orientation = _validate_orientation(frame, expected_plane)
    extent = _validate_extent(
        frame,
        orientation["plane"],
        expected_target_meters=expected_target_meters,
        expected_padding_ratio=expected_padding_ratio,
    )
    resolution = manifest.get("resolution") or []
    contract_resolution = contract.get("capture_resolution") or []
    resolution_values = (
        [
            _integer(
                value,
                "Physical-capture resolution",
                validation_error="coverage_invalid",
                minimum=1,
            )
            for value in resolution
        ]
        if isinstance(resolution, list)
        else []
    )
    contract_resolution_values = (
        [
            _integer(
                value,
                "Physical-capture contract resolution",
                validation_error="coverage_invalid",
                minimum=1,
            )
            for value in contract_resolution
        ]
        if isinstance(contract_resolution, list)
        else []
    )
    if (
        len(resolution_values) != 2
        or len(contract_resolution_values) != 2
        or resolution_values != contract_resolution_values
        or resolution_values[0] != resolution_values[1]
        or (
            expected_resolution is not None
            and resolution_values
            != [
                _integer(
                    expected_resolution,
                    "Expected physical-capture resolution",
                    validation_error="coverage_invalid",
                    minimum=1,
                )
            ]
            * 2
        )
    ):
        raise PhysicalCaptureValidationError(
            "coverage_invalid", "Physical-capture resolution evidence is inconsistent"
        )
    manifest_maps = _map_rows(manifest.get("maps"), "Manifest")
    contract_maps = _map_rows(contract.get("capture_maps"), "Contract")
    validated_maps = []
    for role in REQUIRED_MAP_ROLES:
        row = manifest_maps[role]
        contract_row = contract_maps[role]
        row_size = _integer(
            row.get("size"),
            f"{role} manifest map size",
            validation_error="coverage_invalid",
            minimum=1,
        )
        contract_size = _integer(
            contract_row.get("size"),
            f"{role} contract map size",
            validation_error="coverage_invalid",
            minimum=1,
        )
        row_hash = _sha256_text(row.get("sha256"), f"{role} manifest map hash")
        contract_hash = _sha256_text(
            contract_row.get("sha256"), f"{role} contract map hash"
        )
        if (
            not _same_path(row.get("path"), contract_row.get("path"))
            or row_size != contract_size
            or row_hash != contract_hash
            or str(row.get("physical_capture_contract_sha256") or "").casefold()
            != recorded_contract_hash
        ):
            raise PhysicalCaptureValidationError(
                "fingerprint_mismatch", f"{role} map evidence disagrees with the contract"
            )
        evidence = {
            "role": role,
            "path": str(Path(row.get("path") or "").expanduser().absolute()),
            "size": row_size,
            "sha256": row_hash,
        }
        if verify_map_files:
            actual = file_fingerprint(evidence["path"])
            if (
                actual["size"] != evidence["size"]
                or actual["sha256"].casefold() != evidence["sha256"]
            ):
                raise PhysicalCaptureValidationError(
                    "fingerprint_mismatch", f"{role} map bytes changed after capture"
                )
        validated_maps.append(evidence)
    manifest_fingerprint = file_fingerprint(manifest_path)
    return {
        "status": "ready",
        "manifest": manifest_fingerprint,
        "contract_sha256": recorded_contract_hash,
        "contract": contract,
        "frame": frame,
        "orientation": orientation,
        "extent": extent,
        "resolution": resolution_values,
        "map_roles": list(REQUIRED_MAP_ROLES),
        "maps": validated_maps,
    }


def validate_normalization_receipt(
    receipt_path,
    *,
    manifest_evidence,
    expected_blend=None,
    expected_normalization_contract_sha256=None,
):
    """Bind a normalization receipt to the exact validated manifest bytes."""
    receipt_path, receipt = _load_json(
        receipt_path, "Cluster normalization receipt"
    )
    receipt_version = _integer(
        receipt.get("version"),
        "Cluster normalization receipt version",
        minimum=3,
    )
    if (
        receipt.get("kind") != NORMALIZATION_RECEIPT_KIND
        or receipt_version < 3
        or receipt.get("status") != "ready"
    ):
        raise PhysicalCaptureValidationError(
            "schema_invalid", "Cluster normalization receipt is not ready/version 3+"
        )
    if expected_blend is not None and not _same_path(
        receipt.get("blend"), expected_blend
    ):
        raise PhysicalCaptureValidationError(
            "fingerprint_mismatch", "Normalization receipt belongs to another blend"
        )
    manifest = manifest_evidence["manifest"]
    if (
        not _same_path(receipt.get("capture_manifest"), manifest["path"])
        or str(receipt.get("capture_manifest_sha256") or "").casefold()
        != manifest["sha256"].casefold()
    ):
        raise PhysicalCaptureValidationError(
            "fingerprint_mismatch", "Normalization receipt does not bind current manifest bytes"
        )
    if expected_normalization_contract_sha256 is not None and str(
        receipt.get("normalization_contract_sha256")
        or receipt.get("recipe_sha256")
        or ""
    ).casefold() != str(expected_normalization_contract_sha256).casefold():
        raise PhysicalCaptureValidationError(
            "fingerprint_mismatch", "Normalization receipt uses another recipe fingerprint"
        )
    build = receipt.get("build") or {}
    capture_contract = build.get("physical_capture_contract") or {}
    if (
        build.get("workflow_mode") != CAPTURE_WORKFLOW
        or capture_contract != manifest_evidence["contract"]
    ):
        raise PhysicalCaptureValidationError(
            "fingerprint_mismatch", "Normalization receipt capture contract is stale"
        )
    current_manifest = file_fingerprint(manifest["path"])
    if current_manifest != manifest:
        raise PhysicalCaptureValidationError(
            "fingerprint_mismatch",
            "Physical-capture manifest changed during normalization validation",
        )
    blend_path = expected_blend or receipt.get("blend")
    blend_fingerprint = file_fingerprint(blend_path)
    output_blend_sha256 = _sha256_text(
        receipt.get("output_blend_sha256"),
        "Normalization output blend hash",
    )
    if output_blend_sha256 != blend_fingerprint["sha256"]:
        raise PhysicalCaptureValidationError(
            "fingerprint_mismatch",
            "Normalization receipt does not bind current blend bytes",
        )
    return {
        "status": "ready",
        "receipt": file_fingerprint(receipt_path),
        "blend": blend_fingerprint,
        "normalization_contract_sha256": str(
            receipt.get("normalization_contract_sha256")
            or receipt.get("recipe_sha256")
            or ""
        ),
        "output_blend_sha256": output_blend_sha256,
    }


__all__ = [
    "CAPTURE_CONTRACT_KIND",
    "CAPTURE_KIND",
    "CAPTURE_WORKFLOW",
    "DIRECT_UV_SOURCE",
    "FRAME_POLICY",
    "NORMALIZATION_RECEIPT_KIND",
    "PLANE_BASES",
    "PhysicalCaptureValidationError",
    "REQUIRED_MAP_ROLES",
    "canonical_sha256",
    "file_fingerprint",
    "validate_normalization_receipt",
    "validate_physical_capture_manifest",
]
