"""Diagnostic snapshot of the Blender Export collection after Assembly.

This module deliberately contains no Assembly-to-Push validation. Upstream
authoring state is mutable and must never freeze a queued Unreal import.
"""

from __future__ import annotations

import hashlib
import json
from array import array
from collections import Counter


EXPORT_POSTCONDITION_KIND = "sk_batch_export_object_postcondition"
EXPORT_POSTCONDITION_VERSION = 2


def _canonical_json_sha256(payload):
    encoded = json.dumps(
        payload,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _value_tuple(value):
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return float(value)
    try:
        return [float(item) for item in value]
    except (TypeError, ValueError):
        return str(value)


def _material_index_counts(polygons):
    """Read one integer per face without materializing Blender polygons."""

    count = len(polygons)
    foreach_get = getattr(polygons, "foreach_get", None)
    if callable(foreach_get):
        values = array("i", [0]) * count
        foreach_get("material_index", values)
        return Counter(values)
    return Counter(
        int(getattr(polygon, "material_index", 0))
        for polygon in polygons
    )


def _material_descriptor(material):
    if material is None:
        return None
    payload = {
        "name": str(getattr(material, "name", "")),
        "use_nodes": bool(getattr(material, "use_nodes", False)),
    }
    node_tree = getattr(material, "node_tree", None)
    if node_tree is None:
        payload["node_tree_sha256"] = None
        return payload
    nodes = []
    for node in getattr(node_tree, "nodes", ()) or ():
        image = getattr(node, "image", None)
        inputs = []
        for socket in getattr(node, "inputs", ()) or ():
            inputs.append({
                "name": str(getattr(socket, "name", "")),
                "identifier": str(getattr(socket, "identifier", "")),
                "linked": bool(getattr(socket, "is_linked", False)),
                "default": _value_tuple(
                    getattr(socket, "default_value", None)
                ),
            })
        nodes.append({
            "name": str(getattr(node, "name", "")),
            "type": str(
                getattr(node, "bl_idname", "")
                or getattr(node, "type", "")
            ),
            "label": str(getattr(node, "label", "")),
            "mute": bool(getattr(node, "mute", False)),
            "image": (
                {
                    "filepath": str(getattr(image, "filepath", "")),
                    "source": str(getattr(image, "source", "")),
                    "colorspace": str(
                        getattr(
                            getattr(image, "colorspace_settings", None),
                            "name",
                            "",
                        )
                    ),
                }
                if image is not None
                else None
            ),
            "inputs": inputs,
        })
    links = [
        {
            "from_node": str(getattr(link.from_node, "name", "")),
            "from_socket": str(getattr(link.from_socket, "identifier", "")),
            "to_node": str(getattr(link.to_node, "name", "")),
            "to_socket": str(getattr(link.to_socket, "identifier", "")),
        }
        for link in getattr(node_tree, "links", ()) or ()
    ]
    payload["node_tree_sha256"] = _canonical_json_sha256({
        "nodes": sorted(nodes, key=lambda row: row["name"].casefold()),
        "links": sorted(
            links,
            key=lambda row: (
                row["from_node"].casefold(),
                row["from_socket"].casefold(),
                row["to_node"].casefold(),
                row["to_socket"].casefold(),
            ),
        ),
    })
    return payload


def export_object_postcondition(blender_data):
    """Describe every actual object in the Send2UE Export collection."""

    collection = blender_data.collections.get("Export")
    if collection is None:
        raise RuntimeError(
            "Export postcondition cannot cover a missing Export collection"
        )
    objects = list(collection.all_objects)
    rows = []
    empty_material_slots = []
    for obj in objects:
        row = {
            "name": str(obj.name),
            "type": str(obj.type),
            "parent": (
                str(obj.parent.name)
                if getattr(obj, "parent", None) is not None
                else None
            ),
            "children": sorted(
                str(child.name)
                for child in (getattr(obj, "children", ()) or ())
            ),
            "cluster_generated": bool(
                obj.get("speedtree_cluster_generated", False)
            ),
            "asset_role": obj.get("speedtree_cluster_asset_role"),
        }
        data = getattr(obj, "data", None)
        if obj.type == "MESH" and data is not None:
            materials = [
                _material_descriptor(material)
                for material in (getattr(data, "materials", ()) or ())
            ]
            for index, material in enumerate(materials):
                if material is None:
                    empty_material_slots.append({
                        "object": str(obj.name),
                        "slot": index,
                    })
            polygons = getattr(data, "polygons", ()) or ()
            material_index_counts = _material_index_counts(polygons)
            row["mesh"] = {
                "vertex_count": len(getattr(data, "vertices", ()) or ()),
                "edge_count": len(getattr(data, "edges", ()) or ()),
                "polygon_count": len(polygons),
                "material_index_counts": [
                    {
                        "material_index": material_index,
                        "polygon_count": polygon_count,
                    }
                    for material_index, polygon_count in sorted(
                        material_index_counts.items()
                    )
                ],
                "materials": materials,
            }
        elif obj.type == "ARMATURE" and data is not None:
            row["armature"] = {
                "bones": sorted(
                    str(bone.name)
                    for bone in (getattr(data, "bones", ()) or ())
                ),
            }
        rows.append(row)
    payload = {
        "kind": EXPORT_POSTCONDITION_KIND,
        "schema_version": EXPORT_POSTCONDITION_VERSION,
        "coverage": "exact_export_collection_all_objects",
        "objects": sorted(rows, key=lambda row: row["name"].casefold()),
        "empty_material_slots": sorted(
            empty_material_slots,
            key=lambda row: (row["object"].casefold(), row["slot"]),
        ),
    }
    payload["content_sha256"] = _canonical_json_sha256(payload)
    return payload


__all__ = [
    "EXPORT_POSTCONDITION_KIND",
    "EXPORT_POSTCONDITION_VERSION",
    "export_object_postcondition",
]
