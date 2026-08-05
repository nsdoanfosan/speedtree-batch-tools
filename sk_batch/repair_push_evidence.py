"""Content-addressed Repair -> Push execution evidence.

The bundle is intentionally job-scoped.  It proves that Push is consuming the
same saved Repair outputs and dependency bytes that were inspected by Repair,
while the Export postcondition proves exact coverage of the scene loaded by
the Blender Push worker.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
from collections import Counter
from pathlib import Path


EVIDENCE_BUNDLE_KIND = "sk_batch_repair_push_evidence"
EVIDENCE_BUNDLE_VERSION = 2
EXPORT_POSTCONDITION_KIND = "sk_batch_export_object_postcondition"
EXPORT_POSTCONDITION_VERSION = 2
STALE_EXECUTION_FREEZE = "STALE_EXECUTION_FREEZE"


class RepairPushEvidenceError(RuntimeError):
    """Malformed, incomplete, or stale Repair -> Push evidence."""


def _canonical_json_sha256(payload):
    encoded = json.dumps(
        payload,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _stable_file_sha256(path):
    candidate = Path(path)
    for _attempt in range(2):
        before = candidate.stat()
        digest = hashlib.sha256()
        with candidate.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        after = candidate.stat()
        if (
            before.st_size == after.st_size
            and before.st_mtime_ns == after.st_mtime_ns
        ):
            return digest.hexdigest(), after.st_size
    raise RepairPushEvidenceError(
        f"file changed while hashing: {candidate}"
    )


def content_file_descriptor(path, *, role):
    candidate = Path(path).expanduser()
    if not candidate.is_file():
        raise RepairPushEvidenceError(
            f"required {role} file is missing: {candidate}"
        )
    digest, size = _stable_file_sha256(candidate)
    return {
        "role": str(role),
        "path": str(candidate.resolve()),
        "size": size,
        "sha256": digest,
    }


def _descriptor_path(value):
    if not isinstance(value, dict):
        return None
    return value.get("canonical_path") or value.get("path")


def _recorded_digest(value):
    if not isinstance(value, dict):
        return ""
    return str(value.get("sha256") or "").strip().casefold()


def _descriptors_from_recorded_values(payload, *, role_prefix):
    """Re-hash local files referenced by content-addressed report rows."""

    descriptors = {}

    def visit(value, trail):
        if isinstance(value, dict):
            path = _descriptor_path(value)
            recorded = _recorded_digest(value)
            if path and recorded:
                candidate = Path(path).expanduser()
                if candidate.is_file():
                    role = role_prefix + (
                        "." + ".".join(trail) if trail else ""
                    )
                    descriptor = content_file_descriptor(
                        candidate,
                        role=role,
                    )
                    if descriptor["sha256"].casefold() != recorded:
                        raise RepairPushEvidenceError(
                            f"recorded {role} digest is stale: {candidate}"
                        )
                    descriptors[
                        os.path.normcase(descriptor["path"]).casefold()
                    ] = descriptor
            for key, child in value.items():
                visit(child, trail + (str(key),))
        elif isinstance(value, (list, tuple)):
            for index, child in enumerate(value):
                visit(child, trail + (str(index),))

    visit(payload, ())
    return [
        descriptors[key]
        for key in sorted(descriptors)
    ]


def _value_tuple(value):
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return float(value)
    try:
        return [float(item) for item in value]
    except (TypeError, ValueError):
        return str(value)


def _sequence_digest(values):
    digest = hashlib.sha256()
    for value in values:
        encoded = json.dumps(
            value,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return digest.hexdigest()


def _mesh_channel_rows(channels, value_names):
    rows = []
    for channel in channels or ():
        values = []
        for item in getattr(channel, "data", ()) or ():
            selected = None
            for name in value_names:
                if hasattr(item, name):
                    selected = getattr(item, name)
                    break
            values.append(_value_tuple(selected))
        rows.append({
            "name": str(getattr(channel, "name", "")),
            "domain": str(getattr(channel, "domain", "")),
            "data_type": str(getattr(channel, "data_type", "")),
            "value_count": len(values),
            "values_sha256": _sequence_digest(values),
        })
    return sorted(rows, key=lambda row: row["name"].casefold())


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
        raise RepairPushEvidenceError(
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
            polygons = list(getattr(data, "polygons", ()) or ())
            material_index_counts = Counter(
                int(getattr(polygon, "material_index", 0))
                for polygon in polygons
            )
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
                "polygon_layout_sha256": _sequence_digest(
                    {
                        "vertices": [
                            int(value)
                            for value in getattr(polygon, "vertices", ())
                        ],
                        "material_index": int(
                            getattr(polygon, "material_index", 0)
                        ),
                    }
                    for polygon in polygons
                ),
                "materials": materials,
                "uv_layers": _mesh_channel_rows(
                    getattr(data, "uv_layers", ()),
                    ("uv",),
                ),
                "color_attributes": _mesh_channel_rows(
                    getattr(data, "color_attributes", ()),
                    ("color", "color_srgb", "value"),
                ),
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


def validate_export_object_postcondition(expected, blender_data):
    if not isinstance(expected, dict):
        raise RepairPushEvidenceError(
            "Repair evidence has no Export object postcondition"
        )
    recorded_hash = str(expected.get("content_sha256") or "").casefold()
    unsigned = dict(expected)
    unsigned.pop("content_sha256", None)
    if (
        expected.get("kind") != EXPORT_POSTCONDITION_KIND
        or expected.get("schema_version") != EXPORT_POSTCONDITION_VERSION
        or expected.get("coverage")
        != "exact_export_collection_all_objects"
        or not recorded_hash
        or _canonical_json_sha256(unsigned) != recorded_hash
        or expected.get("empty_material_slots") != []
    ):
        raise RepairPushEvidenceError(
            "Repair Export postcondition is incomplete or invalid"
        )
    for row in expected.get("objects") or []:
        mesh = row.get("mesh") if isinstance(row, dict) else None
        if not isinstance(mesh, dict):
            continue
        polygon_count = mesh.get("polygon_count")
        materials = mesh.get("materials")
        index_counts = mesh.get("material_index_counts")
        if (
            isinstance(polygon_count, bool)
            or not isinstance(polygon_count, int)
            or polygon_count < 0
            or not isinstance(materials, list)
            or not isinstance(index_counts, list)
            or any(not isinstance(material, dict) for material in materials)
        ):
            raise RepairPushEvidenceError(
                "Repair Export mesh material evidence is invalid"
            )
        counted_polygons = 0
        seen_indices = set()
        for count_row in index_counts:
            if not isinstance(count_row, dict):
                raise RepairPushEvidenceError(
                    "Repair Export mesh material assignment is invalid"
                )
            material_index = count_row.get("material_index")
            assigned_count = count_row.get("polygon_count")
            if (
                isinstance(material_index, bool)
                or not isinstance(material_index, int)
                or material_index in seen_indices
                or material_index < 0
                or material_index >= len(materials)
                or isinstance(assigned_count, bool)
                or not isinstance(assigned_count, int)
                or assigned_count < 1
            ):
                raise RepairPushEvidenceError(
                    "Repair Export mesh material assignment is invalid"
                )
            seen_indices.add(material_index)
            counted_polygons += assigned_count
        if counted_polygons != polygon_count:
            raise RepairPushEvidenceError(
                "Repair Export mesh material assignments do not cover faces"
            )
    actual = export_object_postcondition(blender_data)
    if actual != expected:
        raise RepairPushEvidenceError(
            "actual Export scene does not exactly match Repair postcondition"
        )
    return actual


def _material_envelope(pipeline):
    envelope = pipeline.get("speedtree_material_handoff_contract")
    if not isinstance(envelope, dict):
        envelope = pipeline.get("speedtree_pipeline_contract")
    if not isinstance(envelope, dict):
        raise RepairPushEvidenceError(
            "Repair report has no strict material descriptor"
        )
    return envelope


def build_repair_push_evidence_bundle(
    *,
    queue_spm,
    speedtree_spm,
    blend,
    repair_report,
    pipeline,
    push_dependency_contract=None,
):
    """Build and seal the same-generation evidence bundle."""

    if not isinstance(pipeline, dict):
        raise RepairPushEvidenceError("Repair report payload is invalid")
    export_objects = pipeline.get("repair_push_export_postcondition")
    validate_export_object_postcondition_shape(export_objects)
    material = _material_envelope(pipeline)
    assembly = pipeline.get("cluster_assembly_manifest")
    if not isinstance(assembly, dict):
        assembly = {"status": "not_applicable"}
    assembly_descriptor = {"embedded": assembly}
    assembly_manifest_path = _descriptor_path(
        assembly.get("manifest") or {}
    )
    if assembly_manifest_path:
        try:
            assembly_descriptor["manifest"] = json.loads(
                Path(assembly_manifest_path).read_text(encoding="utf-8")
            )
        except (OSError, ValueError) as exc:
            raise RepairPushEvidenceError(
                "Cluster Assembly manifest could not be captured: "
                + str(exc)
            ) from exc

    dependency_spms = sorted({
        str(Path(path).expanduser().resolve())
        for path in (
            (push_dependency_contract or {}).get("dependency_spms") or ()
        )
    }, key=lambda value: os.path.normcase(value).casefold())
    payload = {
        "kind": EVIDENCE_BUNDLE_KIND,
        "schema_version": EVIDENCE_BUNDLE_VERSION,
        "root": {
            "queue_spm": content_file_descriptor(
                queue_spm,
                role="root.queue_spm",
            ),
            "speedtree_spm": content_file_descriptor(
                speedtree_spm,
                role="root.speedtree_spm",
            ),
            "blend": content_file_descriptor(
                blend,
                role="root.blend",
            ),
            "repair_report": content_file_descriptor(
                repair_report,
                role="root.repair_report",
            ),
        },
        "dependency_spms": [
            content_file_descriptor(
                path,
                role="dependency_spm",
            )
            for path in dependency_spms
        ],
        "material": {
            "descriptor_sha256": _canonical_json_sha256(material),
            "files": _descriptors_from_recorded_values(
                material,
                role_prefix="material",
            ),
        },
        "assembly": {
            "descriptor_sha256": _canonical_json_sha256(
                assembly_descriptor
            ),
            "files": _descriptors_from_recorded_values(
                assembly_descriptor,
                role_prefix="assembly",
            ),
        },
        "export_objects": copy.deepcopy(export_objects),
    }
    payload["bundle_sha256"] = _canonical_json_sha256(payload)
    return payload


def validate_export_object_postcondition_shape(expected):
    if not isinstance(expected, dict):
        raise RepairPushEvidenceError(
            "Repair report has no final Export object postcondition"
        )
    recorded_hash = str(expected.get("content_sha256") or "").casefold()
    unsigned = dict(expected)
    unsigned.pop("content_sha256", None)
    if (
        expected.get("kind") != EXPORT_POSTCONDITION_KIND
        or expected.get("schema_version") != EXPORT_POSTCONDITION_VERSION
        or expected.get("coverage")
        != "exact_export_collection_all_objects"
        or not isinstance(expected.get("objects"), list)
        or expected.get("empty_material_slots") != []
        or len(recorded_hash) != 64
        or _canonical_json_sha256(unsigned) != recorded_hash
    ):
        raise RepairPushEvidenceError(
            "Repair Export object postcondition is incomplete"
        )
    return expected


def _bundle_file_descriptors(bundle):
    root = bundle.get("root") or {}
    yield from root.values()
    yield from bundle.get("dependency_spms") or ()
    yield from (bundle.get("material") or {}).get("files") or ()
    yield from (bundle.get("assembly") or {}).get("files") or ()


def _same_path(left, right):
    return (
        os.path.normcase(os.path.abspath(str(left))).casefold()
        == os.path.normcase(os.path.abspath(str(right))).casefold()
    )


def validate_repair_push_evidence_bundle(bundle, *, expected_queue_spm=None):
    """Re-hash every described file; stat metadata is never positive proof."""

    if not isinstance(bundle, dict):
        raise RepairPushEvidenceError(
            "same-generation Repair evidence is missing"
        )
    recorded_hash = str(bundle.get("bundle_sha256") or "").casefold()
    unsigned = copy.deepcopy(bundle)
    unsigned.pop("bundle_sha256", None)
    if (
        bundle.get("kind") != EVIDENCE_BUNDLE_KIND
        or bundle.get("schema_version") != EVIDENCE_BUNDLE_VERSION
        or len(recorded_hash) != 64
        or _canonical_json_sha256(unsigned) != recorded_hash
    ):
        raise RepairPushEvidenceError(
            "same-generation Repair evidence envelope is invalid"
        )
    root = bundle.get("root") or {}
    if set(root) != {
        "queue_spm",
        "speedtree_spm",
        "blend",
        "repair_report",
    }:
        raise RepairPushEvidenceError(
            "Repair evidence root descriptors are incomplete"
        )
    if expected_queue_spm is not None and not _same_path(
        (root.get("queue_spm") or {}).get("path"),
        expected_queue_spm,
    ):
        raise RepairPushEvidenceError(
            "Repair evidence belongs to a different queue SPM"
        )
    validate_export_object_postcondition_shape(
        bundle.get("export_objects")
    )
    for group in ("material", "assembly"):
        descriptor = bundle.get(group) or {}
        digest = str(descriptor.get("descriptor_sha256") or "")
        if len(digest) != 64 or not isinstance(
            descriptor.get("files"), list
        ):
            raise RepairPushEvidenceError(
                f"Repair evidence {group} descriptor is incomplete"
            )

    for recorded in _bundle_file_descriptors(bundle):
        if not isinstance(recorded, dict):
            raise RepairPushEvidenceError(
                "Repair evidence contains a malformed file descriptor"
            )
        path = recorded.get("path")
        expected_sha256 = str(
            recorded.get("sha256") or ""
        ).casefold()
        if not path or len(expected_sha256) != 64:
            raise RepairPushEvidenceError(
                "Repair evidence contains a non-content-addressed file"
            )
        current = content_file_descriptor(
            path,
            role=recorded.get("role") or "evidence",
        )
        if current["sha256"].casefold() != expected_sha256:
            raise RepairPushEvidenceError(
                f"content changed after Repair: {path}"
            )
    return bundle


def stale_execution_freeze_message(error):
    return f"{STALE_EXECUTION_FREEZE}: {error}"


__all__ = [
    "EVIDENCE_BUNDLE_KIND",
    "EVIDENCE_BUNDLE_VERSION",
    "EXPORT_POSTCONDITION_KIND",
    "EXPORT_POSTCONDITION_VERSION",
    "RepairPushEvidenceError",
    "STALE_EXECUTION_FREEZE",
    "build_repair_push_evidence_bundle",
    "content_file_descriptor",
    "export_object_postcondition",
    "stale_execution_freeze_message",
    "validate_export_object_postcondition",
    "validate_export_object_postcondition_shape",
    "validate_repair_push_evidence_bundle",
]
