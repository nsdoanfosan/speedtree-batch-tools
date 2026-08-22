"""Strict reader for evidence emitted by the native SpeedTree exporter.

This module never opens or interprets an SPM.  The receipt is produced while
SpeedTree Modeler is operating on its already-parsed runtime model and while
its FBX serializer is assigning geometry vertices and deform clusters.
"""

import base64
import json
import math
from pathlib import Path


RECEIPT_KIND = "speedtree_native_export_receipt"
RECEIPT_SCHEMA_VERSION = 2


class NativeReceiptError(RuntimeError):
    """Raised when native evidence is stale, incomplete, or inconsistent."""


def _windows_write_time_100ns(path):
    stat = Path(path).stat()
    return stat.st_mtime_ns // 100 + 116444736000000000


def _guid(value, context):
    text = str(value or "")
    try:
        decoded = base64.b64decode(text, validate=True)
    except (ValueError, TypeError) as exc:
        raise NativeReceiptError(f"{context} is not a valid runtime GUID") from exc
    if len(decoded) != 16:
        raise NativeReceiptError(f"{context} is not a 16-byte runtime GUID")
    return text


def _float3(value, context):
    try:
        row = tuple(float(item) for item in value)
    except (TypeError, ValueError) as exc:
        raise NativeReceiptError(f"{context} is not a float3") from exc
    if len(row) != 3 or not all(math.isfinite(item) for item in row):
        raise NativeReceiptError(f"{context} is not a finite float3")
    return row


def load_native_export_receipt(path, *, source_spm=None):
    """Load one fresh native receipt without any SPM-side reconstruction."""
    receipt_path = Path(path).resolve()
    try:
        payload = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise NativeReceiptError(
            f"native SpeedTree receipt cannot be read: {receipt_path}"
        ) from exc
    if (
        payload.get("kind") != RECEIPT_KIND
        or payload.get("status") != "ready"
        or int(payload.get("schema_version") or 0) != RECEIPT_SCHEMA_VERSION
    ):
        raise NativeReceiptError("native SpeedTree receipt contract is unsupported")
    id_zero_cluster_write = str(
        payload.get("id_zero_cluster_write") or "legacy_unreported"
    )
    if id_zero_cluster_write not in {
        "legacy_unreported",
        "native_exact_bone_record",
        "omitted_no_exact_bone_record",
        "not_applicable_boneless_export",
    }:
        raise NativeReceiptError(
            "native SpeedTree ID-0 cluster-write contract is unsupported"
        )
    payload["id_zero_cluster_write"] = id_zero_cluster_write

    source = payload.get("source") or {}
    source_path = Path(str(source.get("path") or "")).resolve()
    if source_spm is not None:
        expected = Path(source_spm).resolve()
        if str(source_path).casefold() != str(expected).casefold():
            raise NativeReceiptError("native SpeedTree receipt source path is stale")
        stat = expected.stat()
        if (
            int(source.get("size") or -1) != stat.st_size
            or int(source.get("last_write_time_100ns") or -1)
            != _windows_write_time_100ns(expected)
        ):
            raise NativeReceiptError("native SpeedTree receipt source identity is stale")

    geometries = list(payload.get("geometries") or [])
    geometry_count = payload.get("geometry_count")
    if int(geometry_count if geometry_count is not None else -1) != len(
        geometries
    ):
        raise NativeReceiptError("native SpeedTree geometry count is inconsistent")
    checked_geometries = []
    for expected_ordinal, row in enumerate(geometries):
        try:
            ordinal = int(row.get("ordinal"))
            vertex_count = int(row.get("vertex_count"))
        except (AttributeError, TypeError, ValueError) as exc:
            raise NativeReceiptError("native SpeedTree geometry row is invalid") from exc
        if ordinal != expected_ordinal or vertex_count <= 0:
            raise NativeReceiptError("native SpeedTree geometry identity is invalid")
        checked_geometries.append({
            "ordinal": ordinal,
            "vertex_count": vertex_count,
        })

    checked_bones = []
    bone_ids = set()
    for row in payload.get("bones") or []:
        try:
            bone_id = int(row.get("id"))
            parent_id = int(row.get("parent_id"))
        except (AttributeError, TypeError, ValueError) as exc:
            raise NativeReceiptError("native SpeedTree bone row is invalid") from exc
        if bone_id in bone_ids:
            raise NativeReceiptError("native SpeedTree bone IDs are not unique")
        bone_ids.add(bone_id)
        checked_bones.append({
            **row,
            "id": bone_id,
            "parent_id": parent_id,
            "start_native": _float3(
                row.get("start_native"), f"Bone {bone_id} start"
            ),
            "end_native": _float3(
                row.get("end_native"), f"Bone {bone_id} end"
            ),
        })

    checked_instances = []
    for row in payload.get("generated_instances") or []:
        try:
            geometry_ordinal = int(row.get("geometry_ordinal"))
            source_bone_id = int(row.get("source_bone_id"))
        except (AttributeError, TypeError, ValueError) as exc:
            raise NativeReceiptError("native generated-instance row is invalid") from exc
        if not 0 <= geometry_ordinal < len(checked_geometries):
            raise NativeReceiptError("native generated-instance geometry is invalid")
        maximum = checked_geometries[geometry_ordinal]["vertex_count"] - 1
        checked_ranges = []
        previous_last = -1
        for value in row.get("vertex_ranges") or []:
            try:
                first, last = (int(value[0]), int(value[1]))
            except (IndexError, TypeError, ValueError) as exc:
                raise NativeReceiptError("native vertex range is invalid") from exc
            if first <= previous_last or first < 0 or last < first or last > maximum:
                raise NativeReceiptError("native vertex ranges are not exact and ordered")
            checked_ranges.append((first, last))
            previous_last = last
        if not checked_ranges:
            raise NativeReceiptError("native generated instance has no vertex range")

        checked = {
            **row,
            "geometry_ordinal": geometry_ordinal,
            "source_bone_id": source_bone_id,
            "vertex_ranges": checked_ranges,
        }
        if row.get("node_guid"):
            checked["node_guid"] = _guid(row.get("node_guid"), "node GUID")
            if row.get("parent_guid"):
                checked["parent_guid"] = _guid(
                    row.get("parent_guid"), "parent GUID"
                )
            if row.get("generator_guid"):
                checked["generator_guid"] = _guid(
                    row.get("generator_guid"), "generator GUID"
                )
            checked["authored_position_native"] = _float3(
                row.get("authored_position_native"), "authored node position"
            )
            influences = []
            for influence in row.get("authored_position_influences") or []:
                try:
                    weight = float(influence.get("weight"))
                    mapping_bone_id = int(influence.get("bone_id"))
                except (AttributeError, TypeError, ValueError) as exc:
                    raise NativeReceiptError("native influence is invalid") from exc
                cluster_name = str(
                    influence.get("exported_cluster_name") or ""
                )
                if (
                    not math.isfinite(weight)
                    or weight <= 0.0
                    or influence.get("mapping_node") != "start"
                    or (
                        not cluster_name
                        and not (
                            mapping_bone_id == 0
                            and influence.get("native_root") is True
                        )
                    )
                ):
                    raise NativeReceiptError("native influence identity is incomplete")
                influences.append({
                    **influence,
                    "bone_id": mapping_bone_id,
                    "weight": weight,
                    "exported_cluster_name": cluster_name,
                })
            if not influences or abs(sum(row["weight"] for row in influences) - 1.0) > 2.0e-6:
                raise NativeReceiptError("native authored-position weights are invalid")
            checked["authored_position_influences"] = influences
        checked_instances.append(checked)

    payload["receipt_path"] = str(receipt_path)
    payload["source"]["path"] = str(source_path)
    payload["geometries"] = checked_geometries
    payload["bones"] = checked_bones
    payload["generated_instances"] = checked_instances
    return payload


def exact_geometry_ordinal(receipt, vertex_count):
    """Resolve a Blender FBX mesh by exact native vertex count only."""
    count = int(vertex_count)
    matches = [
        row["ordinal"]
        for row in receipt.get("geometries") or []
        if int(row["vertex_count"]) == count
    ]
    if len(matches) != 1:
        raise NativeReceiptError(
            "FBX mesh has no unique exact native geometry identity: "
            f"vertex_count={count}, matches={matches}"
        )
    return matches[0]


def exact_generated_instance(receipt, geometry_ordinal, vertex_indices):
    """Return the sole native runtime node intersecting supplied vertices.

    SpeedTree can clip the authored attachment/origin vertex out of the FBX
    subset and can leave serializer-only vertices without a Node record.  The
    surviving Node-owned vertices remain exact local indices.  Accept them
    only when their native ranges identify one and only one runtime Node;
    there is deliberately no coverage ranking or fallback selection.
    """
    vertices = sorted({int(value) for value in vertex_indices})
    if not vertices:
        raise NativeReceiptError("target component has no vertices")
    matches = []
    for row in receipt.get("generated_instances") or []:
        if int(row["geometry_ordinal"]) != int(geometry_ordinal):
            continue
        ranges = row["vertex_ranges"]
        matched = [
            vertex
            for vertex in vertices
            if any(first <= vertex <= last for first, last in ranges)
        ]
        if matched:
            matches.append((row, matched))
    if len(matches) != 1:
        raise NativeReceiptError(
            "target component has no sole intersecting native runtime owner: "
            f"geometry={geometry_ordinal}, matches={len(matches)}"
        )
    row, matched = matches[0]
    matched_set = set(matched)
    return {
        **row,
        "matched_native_vertex_indices": matched,
        "queried_native_vertex_count": len(vertices),
        "unowned_native_vertex_count": sum(
            vertex not in matched_set for vertex in vertices
        ),
        "owner_selection_policy": "sole_exact_native_range_intersection_v1",
    }
