"""Fail-closed contract for SpeedTree capture-camera cluster cards.

This module is deliberately independent from the raw 3D/BWR pipeline.  It
reads the capture camera from the authoring SPM and the authoritative embedded
cutout geometry from the tree SPM.  Straighten data is never consulted.
"""

from __future__ import annotations

import base64
import copy
import gzip
import hashlib
import json
import math
import os
import re
import struct
import xml.etree.ElementTree as ET
from pathlib import Path


CONTRACT_KIND = "speedtree_cluster_card_camera_projection"
CONTRACT_VERSION = 1
UV_TEMPLATE_CONTRACT_KIND = "speedtree_cluster_card_uv_template"
EMBEDDED_V7_FLOATS = 37
EMBEDDED_V7_STRIDE = EMBEDDED_V7_FLOATS * 4


class ContractError(RuntimeError):
    pass


def _fingerprint(path):
    candidate = Path(path).resolve()
    digest = hashlib.sha256()
    with candidate.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    stat = candidate.stat()
    return {
        "path": str(candidate),
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "sha256": digest.hexdigest(),
    }


def _read_spm_bytes(path):
    raw = Path(path).read_bytes()
    return gzip.decompress(raw) if raw[:2] == b"\x1f\x8b" else raw


def _read_spm_root(path):
    try:
        return ET.fromstring(_read_spm_bytes(path))
    except (OSError, gzip.BadGzipFile, ET.ParseError) as exc:
        raise ContractError(f"Unable to read SPM XML: {path}: {exc}") from exc


def _write_spm_root(path, root):
    payload = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_bytes(gzip.compress(payload, mtime=0))


def _properties(node):
    return {
        str(item.findtext("Name") or "").strip():
        str(item.findtext("Value") or "").strip()
        for item in node.findall("./Properties/Property")
        if str(item.findtext("Name") or "").strip()
    }


def _required_float(values, key):
    try:
        return float(values[key])
    except (KeyError, TypeError, ValueError) as exc:
        raise ContractError(f"Capture camera is missing numeric property '{key}'") from exc


def _normalize(vector):
    length = math.sqrt(sum(value * value for value in vector))
    if length <= 1.0e-12:
        raise ContractError("Zero-length camera rotation axis")
    return tuple(value / length for value in vector)


def _dot(left, right):
    return sum(a * b for a, b in zip(left, right))


def _cross(left, right):
    return (
        left[1] * right[2] - left[2] * right[1],
        left[2] * right[0] - left[0] * right[2],
        left[0] * right[1] - left[1] * right[0],
    )


def _rotate_axis_angle(vector, axis, angle):
    axis = _normalize(axis)
    cosine = math.cos(angle)
    sine = math.sin(angle)
    cross = _cross(axis, vector)
    projection = _dot(axis, vector) * (1.0 - cosine)
    return tuple(
        vector[index] * cosine + cross[index] * sine + axis[index] * projection
        for index in range(3)
    )


def _camera_basis_from_name(name):
    """Return the canonical SpeedTree camera frame plus the name assertion.

    SceneCamera transforms are authored relative to the canonical XY frame.  A
    ``Dropped <plane>`` name is therefore a validation assertion, not a second
    source of orientation.  Generic ``Ortho camera`` names carry no plane
    assertion and are resolved entirely from the transform.
    """
    match = re.search(r"Dropped\s+([XYZ]{2})\s+plane\s+camera", name, re.IGNORECASE)
    if match:
        declared_plane = match.group(1).upper()
        if declared_plane not in {"XY", "XZ", "YZ"}:
            raise ContractError(
                f"Unsupported dropped camera plane '{declared_plane}'"
            )
        name_kind = "explicit_dropped_plane"
    elif re.fullmatch(r"\s*Ortho\s+camera(?:\s+\d+)?\s*", name, re.IGNORECASE):
        declared_plane = None
        name_kind = "generic_ortho"
    else:
        raise ContractError(
            "Capture camera name must be 'Dropped XY/XZ/YZ plane camera' or "
            "generic 'Ortho camera'"
        )
    return {
        "canonical_plane": "XY",
        "declared_plane": declared_plane,
        "name_kind": name_kind,
        "right": (1.0, 0.0, 0.0),
        "up": (0.0, 1.0, 0.0),
        "view_direction": (0.0, 0.0, -1.0),
        "plane_normal": (0.0, 0.0, 1.0),
    }


def _resolved_axis_plane(normal, tolerance=1.0e-6):
    absolute = [abs(value) for value in normal]
    dominant_index = max(range(3), key=absolute.__getitem__)
    axis_error = max(
        abs(absolute[dominant_index] - 1.0),
        *(absolute[index] for index in range(3) if index != dominant_index),
    )
    if axis_error > tolerance:
        raise ContractError(
            "Capture camera transform does not resolve to an axis-aligned XY/XZ/YZ "
            f"plane (axis error {axis_error})"
        )
    return ("YZ", "XZ", "XY")[dominant_index], axis_error


def _capture_camera(root, requested_name):
    matches = [
        node for node in root.findall(".//SceneCamera")
        if str(node.findtext("Name") or "") == requested_name
    ]
    if len(matches) != 1:
        raise ContractError(
            f"Capture camera '{requested_name}' must resolve exactly once; found {len(matches)}"
        )
    node = matches[0]
    values = _properties(node)
    camera_type = int(_required_float(values, "Settings:Type"))
    if camera_type != 1:
        raise ContractError(f"Capture camera is not orthographic (Settings:Type={camera_type})")
    basis = _camera_basis_from_name(requested_name)
    axis = (
        _required_float(values, "Transform:Rotation:Axis X"),
        _required_float(values, "Transform:Rotation:Axis Y"),
        _required_float(values, "Transform:Rotation:Axis Z"),
    )
    angle_degrees = _required_float(values, "Transform:Rotation:Angle")
    angle_radians = math.radians(angle_degrees)
    if abs(angle_radians) > 1.0e-12:
        basis = {
            **basis,
            "right": _rotate_axis_angle(basis["right"], axis, angle_radians),
            "up": _rotate_axis_angle(basis["up"], axis, angle_radians),
            "view_direction": _rotate_axis_angle(
                basis["view_direction"], axis, angle_radians
            ),
            "plane_normal": _rotate_axis_angle(
                basis["plane_normal"], axis, angle_radians
            ),
        }
    right = _normalize(basis["right"])
    up = _normalize(basis["up"])
    view = _normalize(basis["view_direction"])
    normal = _normalize(basis["plane_normal"])
    orthogonality = max(abs(_dot(right, up)), abs(_dot(right, view)), abs(_dot(up, view)))
    if orthogonality > 1.0e-6 or _dot(_cross(right, up), normal) < 0.999999:
        raise ContractError("Capture camera basis is not a right-handed orthonormal frame")
    resolved_plane, axis_alignment_error = _resolved_axis_plane(normal)
    declared_plane = basis["declared_plane"]
    if declared_plane is not None and declared_plane != resolved_plane:
        raise ContractError(
            f"Capture camera name declares {declared_plane} plane but transform "
            f"resolves to {resolved_plane} plane"
        )
    return {
        "name": requested_name,
        "guid": str(node.findtext("GUID") or ""),
        "type_raw": camera_type,
        "projection": "orthographic",
        "canonical_plane": basis["canonical_plane"],
        "declared_plane": declared_plane,
        "resolved_plane": resolved_plane,
        "plane": resolved_plane,
        "name_kind": basis["name_kind"],
        "width": _required_float(values, "Settings:Width"),
        "height": _required_float(values, "Settings:Height"),
        "depth": _required_float(values, "Settings:Depth"),
        "near": _required_float(values, "Settings:Near"),
        "far": _required_float(values, "Settings:Far"),
        "export_resolution_raw": str(values.get("Export:Resolution", "")),
        "export_custom_resolution_raw": str(values.get("Export:Custom resolution", "")),
        "translation": [
            _required_float(values, "Transform:Translation:X"),
            _required_float(values, "Transform:Translation:Y"),
            _required_float(values, "Transform:Translation:Z"),
        ],
        "rotation_axis": list(axis),
        "rotation_angle_degrees": angle_degrees,
        "rotation_angle_radians": angle_radians,
        "right": list(right),
        "up": list(up),
        "view_direction": list(view),
        "plane_normal": list(normal),
        "orthogonality_error": orthogonality,
        "axis_alignment_error": axis_alignment_error,
        "declared_plane_matches_resolved": (
            None if declared_plane is None else declared_plane == resolved_plane
        ),
    }


def _ordered_mesh_ids(material):
    values = []
    primary = str(material.findtext("CutoutMeshID") or "").strip()
    if primary and primary != "-1":
        values.append(int(primary))
    for node in material.findall("./SupplementalCutoutMeshIDs/CutoutMesh"):
        value = str(node.get("ID") or "").strip()
        if value and value != "-1":
            values.append(int(value))
    return values


def _resolve_material_name(root, *, material_name=None, material_id=None, camera_spm=None):
    """Resolve one Material_v8 without relying on current Generator bindings."""
    requested_name = str(material_name).strip() if material_name is not None else None
    try:
        requested_id = int(material_id) if material_id is not None else None
    except (TypeError, ValueError) as exc:
        raise ContractError(f"Invalid Material_v8 ID: {material_id!r}") from exc
    matches = []
    for node in root.findall(".//Material_v8"):
        node_name = str(node.get("Name") or "")
        try:
            node_id = int(node.get("ID") or "-1")
        except ValueError as exc:
            raise ContractError(
                f"Material_v8 '{node_name}' has invalid ID {node.get('ID')!r}"
            ) from exc
        if requested_name is not None and node_name != requested_name:
            continue
        if requested_id is not None and node_id != requested_id:
            continue
        if requested_name is None and requested_id is None and camera_spm is not None:
            color = next(
                (item for item in node.findall("Map") if str(item.get("Name") or "") == "Color"),
                None,
            )
            stored = str(color.findtext("TexFilename") or "").strip() if color is not None else ""
            if not stored or Path(stored).stem.casefold() != Path(camera_spm).stem.casefold():
                continue
        matches.append((node_name, node_id))
    requested = []
    if requested_name is not None:
        requested.append(f"name={requested_name!r}")
    if requested_id is not None:
        requested.append(f"ID={requested_id}")
    label = (
        ", ".join(requested)
        if requested
        else f"Material_v8 with Color atlas stem {Path(camera_spm).stem!r}"
    )
    if len(matches) != 1:
        raise ContractError(f"{label} must resolve exactly once; found {len(matches)}")
    return matches[0][0]


def _material_contract(root, material_name, tree_spm):
    matches = [
        node for node in root.findall(".//Material_v8")
        if str(node.get("Name") or "") == material_name
    ]
    if len(matches) != 1:
        raise ContractError(
            f"Material '{material_name}' must resolve exactly once; found {len(matches)}"
        )
    material = matches[0]
    try:
        width = int(material.findtext("Width") or "0")
        height = int(material.findtext("Height") or "0")
    except ValueError as exc:
        raise ContractError(f"Material '{material_name}' has invalid atlas dimensions") from exc
    if width <= 0 or height <= 0:
        raise ContractError(f"Material '{material_name}' has no atlas dimensions")
    maps = {}
    for item in material.findall("Map"):
        filename = str(item.findtext("TexFilename") or "").strip()
        if not filename:
            continue
        source = (Path(tree_spm).resolve().parent / filename).resolve()
        try:
            texture_size = [
                int(item.findtext("TexSizeX") or "0"),
                int(item.findtext("TexSizeY") or "0"),
            ]
        except ValueError as exc:
            raise ContractError(
                f"Material '{material_name}' map '{item.get('Name')}' has invalid texture dimensions"
            ) from exc
        maps[str(item.get("Name") or "")] = {
            "stored": filename,
            "path": str(source),
            "exists": source.is_file(),
            "size": texture_size,
        }
    color = maps.get("Color")
    if not color or not color["exists"]:
        raise ContractError(f"Material '{material_name}' color atlas is missing")
    color_path = Path(color["path"])
    if color_path.suffix.casefold() != ".tga":
        raise ContractError(
            "Current cluster-card contract requires the authoritative Color atlas to be TGA"
        )
    for map_name, atlas_map in maps.items():
        if not atlas_map["exists"]:
            raise ContractError(f"Material '{material_name}' map '{map_name}' is missing")
        atlas_path = Path(atlas_map["path"])
        fingerprint = _fingerprint(atlas_path)
        atlas_map["file_size"] = fingerprint["size"]
        atlas_map["sha256"] = fingerprint["sha256"]
        if atlas_path.suffix.casefold() == ".tga":
            header = atlas_path.read_bytes()[:18]
            if len(header) != 18:
                raise ContractError(f"Material {map_name} atlas has no valid TGA header")
            actual_size = [
                int.from_bytes(header[12:14], "little"),
                int.from_bytes(header[14:16], "little"),
            ]
            atlas_map["actual_size"] = actual_size
            if actual_size != [width, height]:
                raise ContractError(
                    f"{map_name} atlas header size {actual_size} does not match material "
                    f"{[width, height]}"
                )
        if atlas_map["size"] != [width, height]:
            actual_detail = (
                f"; file header is {atlas_map['actual_size']}"
                if "actual_size" in atlas_map
                else ""
            )
            raise ContractError(
                f"Material {map_name} map size {atlas_map['size']} does not match the "
                f"material atlas dimensions {[width, height]}{actual_detail}. "
                "Metadata repair is required; implicit repair is forbidden"
            )
    try:
        material_id = int(material.get("ID") or "-1")
    except ValueError as exc:
        raise ContractError(f"Material '{material_name}' has invalid ID") from exc
    return {
        "id": material_id,
        "name": material_name,
        "width": width,
        "height": height,
        "ordered_cutout_mesh_ids": _ordered_mesh_ids(material),
        "maps": maps,
    }, material


def _decode_expected_base64(text, expected_bytes, label):
    encoded = "".join(str(text or "").split())
    if not encoded:
        raise ContractError(f"{label} is empty")

    def decode(value):
        return base64.b64decode(value + "=" * (-len(value) % 4), validate=True)

    attempts = [encoded]
    # SpeedTree 10.1 EmbeddedData_v7 omits the final zero sextet before its
    # padding.  Restore only that zero value, and only when the exact expected
    # byte size proves the repair.  No arbitrary data recovery is attempted.
    attempts.append(encoded.rstrip("=") + "A")
    for attempt in attempts:
        try:
            decoded = decode(attempt)
        except Exception:
            continue
        if len(decoded) == expected_bytes:
            return decoded
    raise ContractError(
        f"{label} cannot be decoded to the required {expected_bytes} bytes"
    )


def _float_rows(payload, count):
    rows = []
    for index in range(count):
        start = index * EMBEDDED_V7_STRIDE
        rows.append(struct.unpack("<37f", payload[start:start + EMBEDDED_V7_STRIDE]))
    return rows


def _hash_float_pairs(values):
    digest = hashlib.sha256()
    for left, right in values:
        digest.update(struct.pack("<2f", float(left), float(right)))
    return digest.hexdigest()


def _hash_indices(values):
    return hashlib.sha256(struct.pack("<" + "I" * len(values), *values)).hexdigest()


def _plane_from_mesh(mesh, mesh_id, output_name, camera, atlas_size):
    if str(mesh.findtext("Embedded") or "").strip().lower() != "true":
        raise ContractError(f"Cutout mesh ID {mesh_id} is not embedded")
    if str(mesh.findtext("Scale") or "").strip() != "1":
        raise ContractError(f"Cutout mesh ID {mesh_id} does not have Scale=1")
    data = mesh.find("EmbeddedData_v7")
    lod = mesh.find("./Cutout/LOD0")
    if data is None or lod is None:
        raise ContractError(f"Cutout mesh ID {mesh_id} lacks EmbeddedData_v7 or Cutout/LOD0")
    try:
        vertex_count = int(data.get("NumVertices") or "0")
        index_count = int(data.get("NumIndices") or "0")
    except ValueError as exc:
        raise ContractError(f"Cutout mesh ID {mesh_id} has invalid topology counts") from exc
    if vertex_count <= 2 or index_count <= 2 or index_count % 3:
        raise ContractError(f"Cutout mesh ID {mesh_id} has invalid topology counts")
    vertex_payload = _decode_expected_base64(
        data.findtext("Vertices"), vertex_count * EMBEDDED_V7_STRIDE,
        f"Mesh {mesh_id} Vertices",
    )
    index_payload = _decode_expected_base64(
        data.findtext("Indices"), index_count * 4,
        f"Mesh {mesh_id} Indices",
    )
    rows = _float_rows(vertex_payload, vertex_count)
    positions = [tuple(float(value) for value in row[0:3]) for row in rows]
    source_normals = [tuple(float(value) for value in row[3:6]) for row in rows]
    source_uvs = [tuple(float(value) for value in row[12:14]) for row in rows]
    indices = list(struct.unpack("<" + "I" * index_count, index_payload))
    if max(indices) >= vertex_count:
        raise ContractError(f"Cutout mesh ID {mesh_id} has an out-of-range index")
    faces = [indices[index:index + 3] for index in range(0, len(indices), 3)]
    if any(len(set(face)) != 3 for face in faces):
        raise ContractError(f"Cutout mesh ID {mesh_id} has a degenerate topology index triplet")
    if set(indices) != set(range(vertex_count)):
        raise ContractError(f"Cutout mesh ID {mesh_id} has unreferenced vertices")
    if not all(math.isfinite(value) for row in positions + source_normals + source_uvs for value in row):
        raise ContractError(f"Cutout mesh ID {mesh_id} contains non-finite vertex data")
    u_values = [uv[0] for uv in source_uvs]
    v_values = [uv[1] for uv in source_uvs]
    uvs_within_unit_domain = all(
        -1.0e-6 <= value <= 1.0 + 1.0e-6
        for uv in source_uvs
        for value in uv
    )
    for face in faces:
        a, b, c = (positions[index] for index in face)
        ab = tuple(b[index] - a[index] for index in range(3))
        ac = tuple(c[index] - a[index] for index in range(3))
        if math.sqrt(_dot(_cross(ab, ac), _cross(ab, ac))) <= 1.0e-12:
            raise ContractError(f"Cutout mesh ID {mesh_id} has a zero-area position triangle")
        uv_a, uv_b, uv_c = (source_uvs[index] for index in face)
        uv_area_twice = abs(
            (uv_b[0] - uv_a[0]) * (uv_c[1] - uv_a[1])
            - (uv_b[1] - uv_a[1]) * (uv_c[0] - uv_a[0])
        )
        if uv_area_twice <= 1.0e-12:
            raise ContractError(f"Cutout mesh ID {mesh_id} has a zero-area UV triangle")
    z_values = [point[2] for point in positions]
    max_plane_distance = max(z_values) - min(z_values)
    if max_plane_distance > 1.0e-6:
        raise ContractError(f"Cutout mesh ID {mesh_id} is not planar")
    try:
        pivot_uv = (float(lod.get("PivotX")), float(lod.get("PivotY")))
        cutout_angle = float(lod.get("Angle"))
    except (TypeError, ValueError) as exc:
        raise ContractError(f"Cutout mesh ID {mesh_id} has invalid pivot/angle metadata") from exc
    right = camera["right"]
    up = camera["up"]
    normal = camera["plane_normal"]
    normalized = []
    for point in positions:
        normalized.append([
            right[index] * point[0] + up[index] * point[1] + normal[index] * point[2]
            for index in range(3)
        ])
    normalized_normals = []
    for source in source_normals:
        normalized_normals.append([
            right[index] * source[0] + up[index] * source[1] + normal[index] * source[2]
            for index in range(3)
        ])
    normalized_uvs = [tuple(value) for value in source_uvs]
    output_indices = list(indices)
    # Embedded cutout coordinates already encode Cutout PivotX/PivotY and
    # Angle around mesh-local (0,0,0).  Preserve that origin exactly; bounds or
    # UV fitting must not create a replacement pivot.  Round-trip the baked
    # vertices through the capture-camera basis to prove the same footprint.
    reprojection_errors = []
    projected_relative = []
    for point, source in zip(normalized, positions):
        recovered = (_dot(point, right), _dot(point, up), _dot(point, normal))
        reprojection_errors.append(math.sqrt(sum((a - b) ** 2 for a, b in zip(recovered, source))))
        projected_relative.append([
            recovered[0] / camera["width"],
            recovered[1] / camera["height"],
        ])
    max_reprojection = max(reprojection_errors)
    uv_reprojection_errors = [
        math.hypot(output[0] - source[0], output[1] - source[1])
        for source, output in zip(source_uvs, normalized_uvs)
    ]
    max_uv_error = max(uv_reprojection_errors, default=0.0)
    max_pixel_error = max_uv_error * max(atlas_size)
    embedded_origin = (0.0, 0.0, 0.0)
    normalized_origin = tuple(
        right[index] * embedded_origin[0]
        + up[index] * embedded_origin[1]
        + normal[index] * embedded_origin[2]
        for index in range(3)
    )
    attachment_origin_error = math.sqrt(_dot(normalized_origin, normalized_origin))
    source_uv_hash = _hash_float_pairs(source_uvs)
    normalized_uv_hash = _hash_float_pairs(normalized_uvs)
    source_topology_hash = _hash_indices(indices)
    normalized_topology_hash = _hash_indices(output_indices)
    normal_dots = [_dot(_normalize(value), normal) for value in normalized_normals]
    if min(normal_dots) < 0.999:
        raise ContractError(f"Cutout mesh ID {mesh_id} normals do not share the capture-camera normal")
    return {
        "source_mesh_id": mesh_id,
        "source_mesh_name": str(mesh.get("Name") or ""),
        "name": output_name,
        "sk_alias": "SK_" + output_name,
        "vertex_count": vertex_count,
        "triangle_count": len(faces),
        "vertices": normalized,
        "faces": faces,
        "uvs": [list(value) for value in normalized_uvs],
        "normals": normalized_normals,
        "attachment": {
            "pivot_uv": list(pivot_uv),
            "source_plane_xy": [0.0, 0.0],
            "normalized_local": [0.0, 0.0, 0.0],
            "source": "EmbeddedData_v7 mesh origin with Cutout/LOD0 PivotX/PivotY metadata preserved",
        },
        "cutout_angle_radians": cutout_angle,
        "straighten_used": False,
        "source_uv_sha256": source_uv_hash,
        "uv_sha256": normalized_uv_hash,
        "source_topology_sha256": source_topology_hash,
        "topology_sha256": normalized_topology_hash,
        "validation": {
            "source_planarity_max_distance": max_plane_distance,
            "camera_basis_roundtrip_max_position_error": max_reprojection,
            "reprojection_max_uv_error": max_uv_error,
            "reprojection_max_pixel_error": max_pixel_error,
            "normal_min_dot_camera_normal": min(normal_dots),
            "attachment_origin_error": attachment_origin_error,
            "uv_preserved": source_uv_hash == normalized_uv_hash,
            "topology_preserved": source_topology_hash == normalized_topology_hash,
            "all_vertices_referenced": set(indices) == set(range(vertex_count)),
            "uvs_finite": True,
            "uvs_within_unit_domain": uvs_within_unit_domain,
            "strict_vertex_uv_topology": True,
        },
        "projection_model": {
            "camera_right": list(right),
            "camera_up": list(up),
            "camera_normal": list(normal),
            "camera_relative_projected_footprint": projected_relative,
            "atlas_footprint_uv": [list(value) for value in normalized_uvs],
            "atlas_footprint_uv_bounds": {
                "min": [min(u_values), min(v_values)],
                "max": [max(u_values), max(v_values)],
            },
        },
    }


def _uv_template_contract_from_roots(
    camera_spm,
    tree_spm,
    camera_root,
    tree_root,
    *,
    camera_name,
    material_name,
    material_id,
    output_prefix,
    expected_mesh_ids=None,
):
    """Build the shared camera/material/embedded-plane contract from parsed SPM roots."""
    camera = _capture_camera(camera_root, camera_name)
    resolved_material_name = _resolve_material_name(
        tree_root,
        material_name=material_name,
        material_id=material_id,
        camera_spm=camera_spm,
    )
    material, _material_node = _material_contract(
        tree_root, resolved_material_name, tree_spm
    )
    if material_id is not None and material["id"] != int(material_id):
        raise ContractError(
            f"Material ID drift: expected {int(material_id)}, found {material['id']}"
        )
    camera["resolved_export_resolution_pixels"] = [material["width"], material["height"]]
    camera["resolved_export_resolution_source"] = (
        "target Material_v8 Width/Height and Color TexSizeX/TexSizeY"
    )
    camera["translation_policy"] = (
        "recorded for capture provenance; removed from normalized local coordinates "
        "because the embedded attachment origin is local (0,0,0)"
    )
    requested = list(material["ordered_cutout_mesh_ids"])
    if not requested:
        raise ContractError(
            f"Material '{resolved_material_name}' owns no embedded cutout meshes"
        )
    if expected_mesh_ids is not None:
        expected = [int(value) for value in expected_mesh_ids]
        if requested != expected:
            raise ContractError(
                f"Material cutout drift: expected {expected}, found {requested}"
            )
    color_path = Path(material["maps"]["Color"]["path"])
    if color_path.stem.casefold() != camera_spm.stem.casefold():
        raise ContractError(
            "Explicit camera SPM stem and target Color atlas stem do not match; "
            "camera/material pairing is unproven"
        )
    meshes = {
        int(node.get("ID")): node
        for node in tree_root.findall(".//Mesh")
        if str(node.get("ID") or "").lstrip("-").isdigit()
    }
    planes = []
    for ordinal, mesh_id in enumerate(requested, 1):
        mesh = meshes.get(mesh_id)
        if mesh is None:
            raise ContractError(f"Cutout mesh ID {mesh_id} is missing")
        planes.append(_plane_from_mesh(
            mesh,
            mesh_id,
            f"{output_prefix}_{ordinal:02d}",
            camera,
            (material["width"], material["height"]),
        ))
    common_normal_error = max(
        1.0 - plane["validation"]["normal_min_dot_camera_normal"]
        for plane in planes
    )
    return {
        "kind": UV_TEMPLATE_CONTRACT_KIND,
        "version": CONTRACT_VERSION,
        "camera_spm": _fingerprint(camera_spm),
        "tree_spm": _fingerprint(tree_spm),
        "camera": camera,
        "material": material,
        "planes": planes,
        "validation": {
            "camera_color_atlas_stem_match": True,
            "material_color_dimensions_match": True,
            "material_all_map_dimensions_match": all(
                atlas_map["size"] == [material["width"], material["height"]]
                and atlas_map["exists"]
                and (
                    "actual_size" not in atlas_map
                    or atlas_map["actual_size"] == [material["width"], material["height"]]
                )
                for atlas_map in material["maps"].values()
            ),
            "strict_vertex_uv_topology": all(
                plane["validation"]["strict_vertex_uv_topology"] for plane in planes
            ),
            "straighten_used": False,
            "common_camera_axis_error": camera["orthogonality_error"],
            "camera_axis_alignment_error": camera["axis_alignment_error"],
            "camera_resolved_plane": camera["resolved_plane"],
            "camera_declared_plane_matches_resolved": camera[
                "declared_plane_matches_resolved"
            ],
            "common_normal_error": common_normal_error,
            "max_reprojection_pixel_error": max(
                plane["validation"]["reprojection_max_pixel_error"] for plane in planes
            ),
            "uv_hashes_unique_per_source": len({plane["uv_sha256"] for plane in planes}) == len(planes),
            "status": "ready",
        },
    }


def read_uv_template_contract(
    camera_spm,
    tree_spm,
    material_name=None,
    material_id=None,
    *,
    camera_name="Dropped XY plane camera 2",
    output_prefix=None,
):
    """Read the authoritative camera-atlas cutout template without changing either SPM.

    Material ownership comes from Material_v8 itself.  Generator slots are deliberately
    outside this read-only contract, so the API remains valid after an adoption workflow
    temporarily or permanently points the Generator at another material.
    """
    camera_spm = Path(camera_spm).resolve()
    tree_spm = Path(tree_spm).resolve()
    if camera_spm == tree_spm:
        raise ContractError("Camera SPM and tree cutout SPM must be distinct explicit inputs")
    camera_root = _read_spm_root(camera_spm)
    tree_root = _read_spm_root(tree_spm)
    return _uv_template_contract_from_roots(
        camera_spm,
        tree_spm,
        camera_root,
        tree_root,
        camera_name=camera_name,
        material_name=material_name,
        material_id=material_id,
        output_prefix=output_prefix or camera_spm.stem,
    )


def build_normalization_contract(
    camera_spm,
    tree_spm,
    *,
    camera_name="Dropped XY plane camera 2",
    material_name="M_branch_elm_01",
    mesh_ids=(1, 2, 9),
    output_prefix="branch_elm_01",
):
    camera_spm = Path(camera_spm).resolve()
    tree_spm = Path(tree_spm).resolve()
    if camera_spm == tree_spm:
        raise ContractError("Camera SPM and tree cutout SPM must be distinct explicit inputs")
    camera_root = _read_spm_root(camera_spm)
    tree_root = _read_spm_root(tree_spm)
    template = _uv_template_contract_from_roots(
        camera_spm,
        tree_spm,
        camera_root,
        tree_root,
        camera_name=camera_name,
        material_name=material_name,
        material_id=None,
        output_prefix=output_prefix,
        expected_mesh_ids=mesh_ids,
    )
    material = template["material"]
    planes = template["planes"]
    requested = list(material["ordered_cutout_mesh_ids"])
    generator_bindings = [
        row for row in _generator_pairs(tree_root)
        if row["material"] == str(material["id"])
    ]
    if not generator_bindings:
        raise ContractError(
            f"No Generator slot references material ID {material['id']} ({material_name})"
        )
    active_mesh_ids = sorted({
        int(row["mesh"])
        for row in generator_bindings
        if str(row["mesh"]).lstrip("-").isdigit() and int(row["mesh"]) >= 0
    })
    if any(mesh_id not in requested for mesh_id in active_mesh_ids):
        raise ContractError(
            f"Generator references cutout meshes outside the material contract: {active_mesh_ids}"
        )
    contract = {
        "kind": CONTRACT_KIND,
        "version": CONTRACT_VERSION,
        "source_contracts": {
            "raw_3d": "SK SPM -> same-stem SK blend (separate BWR pipeline)",
            "cluster_cards": "non-SK camera SPM -> same-stem non-SK blend",
        },
        "camera_spm": template["camera_spm"],
        "tree_spm": template["tree_spm"],
        "camera": template["camera"],
        "material": material,
        "output": {
            "blend_name": f"{output_prefix}.blend",
            "object_prefix": output_prefix,
            "raw_sk_blend_name": f"SK_{output_prefix}.blend",
        },
        "planes": planes,
        "tree_generator_bindings": generator_bindings,
        "active_cutout_mesh_ids": active_mesh_ids,
        "validation": {
            **template["validation"],
        },
    }
    return contract


def _relative_path(source, owner):
    return os.path.relpath(str(Path(source).resolve()), str(Path(owner).resolve().parent)).replace("\\", "/")


def _set_child(node, tag, value):
    child = node.find(tag)
    if child is None:
        child = ET.SubElement(node, tag)
    child.text = str(value)
    return child


def _make_external_mesh(mesh, mesh_id, name, fbx_path, owner_spm):
    output = copy.deepcopy(mesh)
    output.set("ID", str(mesh_id))
    output.set("Name", name)
    _set_child(output, "Filename", _relative_path(fbx_path, owner_spm))
    _set_child(output, "Embedded", "false")
    _set_child(output, "FixWinding", "false")
    _set_child(output, "FlipNormals", "false")
    _set_child(output, "Scale", "1")
    for child in list(output):
        if child.tag.startswith("Lod_") or child.tag in {"EmbeddedData_v7", "Cutout"}:
            output.remove(child)
    return output


def _generator_pairs(root):
    output = []
    for index, generator in enumerate(root.findall(".//Generator")):
        by_name = {
            str(item.findtext("Name") or ""): str(item.findtext("Value") or "")
            for item in generator.findall("./Properties/Property")
        }
        for name, value in by_name.items():
            if name.endswith(":Material"):
                prefix = name[:-len(":Material")]
                output.append({
                    "generator_index": index,
                    "generator_name": str(generator.findtext("Name") or ""),
                    "generator_guid": str(generator.findtext("GUID") or ""),
                    "generator_type": str(generator.get("Type") or generator.findtext("Type") or ""),
                    "slot": prefix,
                    "material_property": name,
                    "mesh_property": prefix + ":Mesh",
                    "material": value,
                    "mesh": by_name.get(prefix + ":Mesh", ""),
                })
    return output


def _material_snapshot(root, material_name):
    matches = [
        node for node in root.findall(".//Material_v8")
        if node.get("Name") == material_name
    ]
    if len(matches) != 1:
        raise ContractError(
            f"Material snapshot '{material_name}' resolved {len(matches)} times"
        )
    material = matches[0]
    return {
        "id": int(material.get("ID") or "-1"),
        "name": str(material.get("Name") or ""),
        "cutout_mesh_ids": _ordered_mesh_ids(material),
        "user_data": str(material.findtext("UserData") or ""),
    }


def _external_mesh_snapshot(root, mesh_ids):
    by_id = {
        int(node.get("ID")): node
        for node in root.findall(".//Mesh")
        if str(node.get("ID") or "").isdigit()
    }
    output = []
    for mesh_id in mesh_ids:
        mesh = by_id.get(int(mesh_id))
        if mesh is None:
            raise ContractError(f"External mesh snapshot lost ID {mesh_id}")
        filename = str(mesh.findtext("Filename") or "")
        output.append({
            "id": int(mesh_id),
            "name": str(mesh.get("Name") or ""),
            "embedded": str(mesh.findtext("Embedded") or ""),
            "scale": str(mesh.findtext("Scale") or ""),
            "filename": filename,
            "has_embedded_data": mesh.find("EmbeddedData_v7") is not None,
            "has_cutout": mesh.find("Cutout") is not None,
        })
    return output


def write_handoff_spm_copies(contract, output_dir):
    """Write isolated, non-production SPM copies linked to normalized FBXs."""
    output_dir = Path(output_dir).resolve()
    speedtree_dir = output_dir / "speedtree"
    mesh_dir = output_dir / "meshes"
    speedtree_dir.mkdir(parents=True, exist_ok=True)
    camera_source = Path(contract["camera_spm"]["path"])
    tree_source = Path(contract["tree_spm"]["path"])
    camera_target = speedtree_dir / camera_source.name
    tree_target = speedtree_dir / tree_source.name
    camera_root = _read_spm_root(camera_source)
    tree_root = _read_spm_root(tree_source)
    tree_assets = tree_root.find("Assets")
    if tree_assets is None:
        raise ContractError("Tree SPM Assets section is missing")
    tree_before = _generator_pairs(tree_root)
    tree_material_before = _material_snapshot(tree_root, contract["material"]["name"])
    tree_meshes = {int(node.get("ID")): node for node in tree_root.findall(".//Mesh") if node.get("ID")}
    for plane in contract["planes"]:
        mesh_id = plane["source_mesh_id"]
        original = tree_meshes.get(mesh_id)
        if original is None:
            raise ContractError(f"Tree copy lost mesh ID {mesh_id}")
        parent = next(node for node in tree_root.iter() if original in list(node))
        index = list(parent).index(original)
        fbx = mesh_dir / f"{plane['name']}.fbx"
        parent.remove(original)
        parent.insert(index, _make_external_mesh(original, mesh_id, plane["name"], fbx, tree_target))
    tree_material_node = next(
        node for node in tree_assets.findall("Material_v8")
        if node.get("Name") == contract["material"]["name"]
    )
    for map_node in tree_material_node.findall("Map"):
        name = str(map_node.get("Name") or "")
        source_map = contract["material"]["maps"].get(name)
        if source_map:
            _set_child(map_node, "TexFilename", _relative_path(source_map["path"], tree_target))
    tree_after = _generator_pairs(tree_root)
    if tree_before != tree_after:
        raise ContractError("Tree Generator Material/Mesh bindings changed during external handoff")
    tree_material_after = _material_snapshot(tree_root, contract["material"]["name"])
    if tree_material_before != tree_material_after:
        raise ContractError("Tree material ID/name/cutout ownership changed during external handoff")
    _write_spm_root(tree_target, tree_root)

    verified_tree = _read_spm_root(tree_target)
    verified_tree_bindings = _generator_pairs(verified_tree)
    verified_tree_material = _material_snapshot(verified_tree, contract["material"]["name"])
    verified_tree_meshes = _external_mesh_snapshot(
        verified_tree, [plane["source_mesh_id"] for plane in contract["planes"]]
    )
    if verified_tree_bindings != tree_before:
        raise ContractError("Saved tree candidate changed Generator GUID/slot bindings")
    if verified_tree_material != tree_material_before:
        raise ContractError("Saved tree candidate changed material ID/name/cutout IDs")
    for plane, mesh in zip(contract["planes"], verified_tree_meshes):
        expected_filename = _relative_path(
            mesh_dir / f"{plane['name']}.fbx", tree_target
        )
        if (
            mesh["id"] != plane["source_mesh_id"]
            or mesh["name"] != plane["name"]
            or mesh["embedded"].casefold() != "false"
            or mesh["scale"] != "1"
            or mesh["filename"] != expected_filename
            or mesh["has_embedded_data"]
            or mesh["has_cutout"]
        ):
            raise ContractError(f"Saved tree external mesh contract mismatch: {mesh}")

    camera_assets = camera_root.find("Assets")
    if camera_assets is None or tree_assets is None:
        raise ContractError("SPM Assets section is missing")
    if any(node.get("Name") == contract["material"]["name"] for node in camera_assets.findall("Material_v8")):
        raise ContractError("Camera SPM already contains the target branch material; refusing ambiguous merge")
    material_source = next(
        node for node in tree_assets.findall("Material_v8")
        if node.get("Name") == contract["material"]["name"]
    )
    used_material_ids = [int(node.get("ID")) for node in camera_assets.findall("Material_v8") if str(node.get("ID", "")).isdigit()]
    used_mesh_ids = [int(node.get("ID")) for node in camera_assets.findall("Mesh") if str(node.get("ID", "")).isdigit()]
    material_id = max(used_material_ids, default=0) + 1
    next_mesh_id = max(used_mesh_ids, default=0) + 1
    material_copy = copy.deepcopy(material_source)
    material_copy.set("ID", str(material_id))
    remapped_mesh_ids = []
    external_meshes = []
    for plane in contract["planes"]:
        source_mesh = tree_meshes[plane["source_mesh_id"]]
        new_id = next_mesh_id
        next_mesh_id += 1
        remapped_mesh_ids.append(new_id)
        external_meshes.append(_make_external_mesh(
            source_mesh,
            new_id,
            plane["name"],
            mesh_dir / f"{plane['name']}.fbx",
            camera_target,
        ))
    _set_child(material_copy, "CutoutMeshID", remapped_mesh_ids[0])
    supplemental = material_copy.find("SupplementalCutoutMeshIDs")
    if supplemental is None:
        supplemental = ET.SubElement(material_copy, "SupplementalCutoutMeshIDs")
    for child in list(supplemental):
        supplemental.remove(child)
    supplemental.set("Count", str(len(remapped_mesh_ids) - 1))
    for mesh_id in remapped_mesh_ids[1:]:
        ET.SubElement(supplemental, "CutoutMesh", {"ID": str(mesh_id)})
    # Rebase every original tree material texture path into the isolated camera copy.
    for map_node in material_copy.findall("Map"):
        name = str(map_node.get("Name") or "")
        source_map = contract["material"]["maps"].get(name)
        if source_map:
            _set_child(map_node, "TexFilename", _relative_path(source_map["path"], camera_target))
    camera_assets.append(material_copy)
    for mesh in external_meshes:
        camera_assets.append(mesh)
    _write_spm_root(camera_target, camera_root)
    verified_camera = _read_spm_root(camera_target)
    verified_camera_material = _material_snapshot(
        verified_camera, contract["material"]["name"]
    )
    verified_camera_meshes = _external_mesh_snapshot(verified_camera, remapped_mesh_ids)
    if verified_camera_material["id"] != material_id:
        raise ContractError("Saved camera-card candidate changed the allocated material ID")
    if verified_camera_material["cutout_mesh_ids"] != remapped_mesh_ids:
        raise ContractError("Saved camera-card candidate changed remapped cutout IDs")
    if [row["name"] for row in verified_camera_meshes] != [
        plane["name"] for plane in contract["planes"]
    ]:
        raise ContractError("Saved camera-card candidate changed normalized mesh names")
    return {
        "camera_card_spm": str(camera_target),
        "tree_handoff_spm": str(tree_target),
        "tree_generator_bindings_preserved": tree_before == tree_after,
        "tree_generator_binding_snapshot": tree_before,
        "tree_material_snapshot": tree_material_before,
        "tree_external_mesh_snapshot": verified_tree_meshes,
        "tree_mesh_ids_preserved": [plane["source_mesh_id"] for plane in contract["planes"]],
        "tree_id_policy": "preserve production material ID and cutout mesh IDs exactly",
        "camera_copy_material_id": material_id,
        "camera_copy_mesh_ids": remapped_mesh_ids,
        "camera_copy_id_policy": (
            "allocate non-colliding IDs because camera SPM IDs 1/2/3 belong to unrelated leaf/bark assets"
        ),
        "production_sources_modified": False,
    }


def write_json(path, payload):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return Path(path)
