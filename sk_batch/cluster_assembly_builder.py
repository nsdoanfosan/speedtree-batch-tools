"""Content-driven Blender -> UE 5.8 Skeletal Nanite Assembly builder.

This module is a downstream stage of the existing SK Batch/BWR pipeline.  It is
not an Assembly switch or a standalone tool: a reconciled
``cluster_assembly_handoff`` decides whether the stage builds, passes through,
or fails closed.

The Blender half derives a base mesh and rigid part prototypes from the final
BWR armature/merged mesh.  The Unreal half consumes those generated assets with
``NaniteAssemblySkeletalMeshBuilder`` while preserving the existing Full
Skeletal Mesh.  Both outputs are checked against the same final-skeleton and
newly-generated wind JSON contract; production DynamicWind data is never
copied.

``bpy`` and ``unreal`` are intentionally lazy dependencies so all contract and
hierarchy checks remain unit-testable with ordinary Python.
"""
from __future__ import annotations

import hashlib
import itertools
import json
import math
import os
import statistics
from contextlib import contextmanager
from copy import deepcopy
from collections import Counter, defaultdict
from pathlib import Path


SCHEMA_VERSION = 1
MANIFEST_KIND = "sk_batch_cluster_nanite_assembly_inputs"
ROLE_ORDER = ("branch", "leaf")


class ClusterAssemblyBuildError(RuntimeError):
    """Raised when a content or hierarchy invariant is not proven."""


def _canonical_json(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _sha256_bytes(value):
    return hashlib.sha256(value).hexdigest()


def file_fingerprint(path):
    candidate = Path(path)
    if not candidate.is_file():
        return {
            "path": str(candidate),
            "exists": False,
            "size": None,
            "sha256": None,
        }
    digest = hashlib.sha256()
    with candidate.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return {
        "path": str(candidate.resolve()),
        "exists": True,
        "size": candidate.stat().st_size,
        "sha256": digest.hexdigest(),
    }


def validate_file_fingerprint(record, label):
    """Fail closed when a persisted handoff artifact changed after receipt."""
    if not isinstance(record, dict) or not record.get("path"):
        raise ClusterAssemblyBuildError(f"{label} fingerprint is missing")
    expected_exists = record.get("exists")
    expected_size = record.get("size")
    expected_sha256 = str(record.get("sha256") or "").casefold()
    if expected_exists is not True or expected_size is None or not expected_sha256:
        raise ClusterAssemblyBuildError(f"{label} fingerprint is incomplete")
    actual = file_fingerprint(record["path"])
    if (
        actual.get("exists") is not True
        or int(actual.get("size") or -1) != int(expected_size)
        or str(actual.get("sha256") or "").casefold() != expected_sha256
    ):
        raise ClusterAssemblyBuildError(
            f"{label} changed after the BWR receipt: "
            f"expected={_canonical_json(record)} actual={_canonical_json(actual)}"
        )
    return actual


def validate_manifest_artifacts(manifest):
    """Revalidate every Blender/FBX/wind artifact at each consumer boundary."""
    if (manifest or {}).get("status") != "ready":
        raise ClusterAssemblyBuildError("Assembly manifest is not ready")
    checked = {
        "full_fbx": validate_file_fingerprint(
            manifest.get("full_fbx"), "Full SK FBX"
        ),
        "base_fbx": validate_file_fingerprint(
            (manifest.get("base") or {}).get("fbx"), "Assembly base FBX"
        ),
        "wind_json": validate_file_fingerprint(
            (manifest.get("wind_contract") or {}).get("wind_json"),
            "final Skeleton wind JSON",
        ),
        "parts": {},
    }
    for part in manifest.get("parts") or []:
        prototype_id = str(part.get("prototype_id") or "")
        if not prototype_id or prototype_id in checked["parts"]:
            raise ClusterAssemblyBuildError(
                f"invalid or duplicate Assembly prototype id: {prototype_id}"
            )
        checked["parts"][prototype_id] = validate_file_fingerprint(
            part.get("fbx"), f"Assembly part FBX {prototype_id}"
        )
    return checked


def normalize_role_identity(value):
    name = str(value or "").split("\x00", 1)[0]
    name = name.strip().replace("\\", "/").rsplit("/", 1)[-1]
    if "::" in name:
        name = name.rsplit("::", 1)[-1]
    if name.casefold().endswith("_mat"):
        name = name[:-4]
    if name.casefold().startswith("m_"):
        name = name[2:]
    return name.strip().casefold()


def content_build_decision(handoff):
    """Return ``build`` or ``pass_through`` solely from receipt content."""
    if not isinstance(handoff, dict):
        raise ClusterAssemblyBuildError("cluster Assembly handoff is missing")
    status = str(handoff.get("status") or "")
    if status == "blocked":
        issues = handoff.get("issues") or []
        raise ClusterAssemblyBuildError(
            "cluster Assembly handoff is blocked: " + _canonical_json(issues)
        )
    if status == "pass_through":
        if (handoff.get("assembly") or {}).get("requested"):
            raise ClusterAssemblyBuildError(
                "pass-through handoff cannot request an Assembly"
            )
        return "pass_through"
    if status != "ready":
        raise ClusterAssemblyBuildError(
            f"unsupported cluster Assembly handoff status: {status or '<empty>'}"
        )
    assembly = handoff.get("assembly") or {}
    if not assembly.get("requested"):
        raise ClusterAssemblyBuildError("ready handoff did not request an Assembly")
    if not (handoff.get("full_skeletal_mesh") or {}).get("preserved"):
        raise ClusterAssemblyBuildError("Full Skeletal Mesh preservation is not proven")
    inputs = assembly.get("part_builder_inputs") or []
    if not inputs:
        raise ClusterAssemblyBuildError("ready handoff contains no part inputs")
    seen = set()
    for row in inputs:
        role = str(row.get("role") or "").casefold()
        if role not in ROLE_ORDER or role in seen:
            raise ClusterAssemblyBuildError(f"invalid or duplicate Assembly role: {role}")
        seen.add(role)
        if not row.get("assignments"):
            raise ClusterAssemblyBuildError(
                f"{role} has no actual FBX polygon assignment evidence"
            )
    return "build"


def _matrix_rows(matrix):
    return [
        [round(float(value), 9) for value in row]
        for row in matrix
    ]


def snapshot_blender_armature(armature):
    """Capture the final FBX-import Skeleton, including its object root.

    The existing BWR/Send2UE FBX contract imports the Blender armature object as
    reference-skeleton index 0, then the authored Blender bones starting at
    index 1.  DynamicWind generation uses that exact exported identity.
    """
    if getattr(armature, "type", "") != "ARMATURE":
        raise ClusterAssemblyBuildError("final armature object is invalid")
    bones = list(getattr(getattr(armature, "data", None), "bones", ()) or ())
    if not bones:
        raise ClusterAssemblyBuildError("final armature has no bones")
    indices = {bone.name: index + 1 for index, bone in enumerate(bones)}
    rows = [
        {
            "index": 0,
            "name": str(armature.name),
            "parent_index": -1,
            "parent_name": None,
            "bind_matrix": _matrix_rows(armature.matrix_world),
        }
    ]
    for source_index, bone in enumerate(bones):
        index = source_index + 1
        parent = getattr(bone, "parent", None)
        parent_name = str(parent.name) if parent else None
        rows.append(
            {
                "index": index,
                "name": str(bone.name),
                "parent_index": indices[parent_name] if parent_name else 0,
                "parent_name": parent_name or str(armature.name),
                "bind_matrix": _matrix_rows(bone.matrix_local),
            }
        )
    return make_skeleton_snapshot(rows)


def make_skeleton_snapshot(bones):
    """Validate and hash ordered final-skeleton records."""
    normalized = []
    names = set()
    for expected_index, raw in enumerate(bones or []):
        index = int(raw.get("index", -1))
        name = str(raw.get("name") or "")
        parent_index = int(raw.get("parent_index", -1))
        parent_name = raw.get("parent_name")
        if index != expected_index:
            raise ClusterAssemblyBuildError(
                f"skeleton index discontinuity at {expected_index}: {index}"
            )
        if not name or name in names:
            raise ClusterAssemblyBuildError(f"invalid or duplicate bone name: {name}")
        if parent_index >= index or parent_index < -1:
            raise ClusterAssemblyBuildError(
                f"invalid parent index for {name}: {parent_index}"
            )
        if parent_index == -1:
            if parent_name not in (None, ""):
                raise ClusterAssemblyBuildError(
                    f"root bone {name} has a parent name without an index"
                )
            parent_name = None
        else:
            expected_parent = normalized[parent_index]["name"]
            if str(parent_name or "") != expected_parent:
                raise ClusterAssemblyBuildError(
                    f"parent name/index mismatch for {name}: "
                    f"{parent_name!r} != {expected_parent!r}"
                )
            parent_name = expected_parent
        bind_matrix = raw.get("bind_matrix")
        if bind_matrix is not None:
            if len(bind_matrix) != 4 or any(len(row) != 4 for row in bind_matrix):
                raise ClusterAssemblyBuildError(
                    f"bind matrix for {name} is not 4x4"
                )
            bind_matrix = _matrix_rows(bind_matrix)
        normalized.append(
            {
                "index": index,
                "name": name,
                "parent_index": parent_index,
                "parent_name": parent_name,
                "bind_matrix": bind_matrix,
            }
        )
        names.add(name)
    if not normalized:
        raise ClusterAssemblyBuildError("final skeleton is empty")
    roots = [row for row in normalized if row["parent_index"] == -1]
    if len(roots) != 1 or roots[0]["index"] != 0:
        raise ClusterAssemblyBuildError(
            "final skeleton must have exactly one root at index 0"
        )
    digest = _sha256_bytes(_canonical_json(normalized).encode("utf-8"))
    identity_sha1 = hashlib.sha1()
    for row in normalized:
        identity_sha1.update(
            (
                f"{row['index']}\0{row['name']}\0"
                f"{row['parent_index']}\n"
            ).encode("utf-8")
        )
    return {
        "contract": "final_skeleton_v2",
        "bone_count": len(normalized),
        "bones": normalized,
        "sha256": digest,
        "bone_name_index_parent_sha1": identity_sha1.hexdigest(),
    }


def _skeleton_maps(snapshot):
    bones = list((snapshot or {}).get("bones") or [])
    checked = make_skeleton_snapshot(bones)
    if snapshot.get("sha256") and snapshot.get("sha256") != checked["sha256"]:
        raise ClusterAssemblyBuildError("final skeleton snapshot hash mismatch")
    by_name = {row["name"]: row for row in checked["bones"]}
    return checked, by_name


def ancestor_chain(bone_name, skeleton_snapshot, skeleton_by_name=None):
    if skeleton_by_name is None:
        checked, by_name = _skeleton_maps(skeleton_snapshot)
        del checked
    else:
        by_name = skeleton_by_name
    if bone_name not in by_name:
        raise ClusterAssemblyBuildError(
            f"binding bone is missing from final skeleton: {bone_name}"
        )
    chain = []
    row = by_name[bone_name]
    while row is not None:
        chain.append(row["name"])
        parent = row["parent_name"]
        row = by_name.get(parent) if parent else None
    return chain


def lowest_common_ancestor(
    bone_names,
    skeleton_snapshot,
    skeleton_by_name=None,
):
    names = list(dict.fromkeys(str(value) for value in bone_names if value))
    if not names:
        raise ClusterAssemblyBuildError("binding contains no bones")
    chains = [
        ancestor_chain(name, skeleton_snapshot, skeleton_by_name)
        for name in names
    ]
    shared = set(chains[0])
    for chain in chains[1:]:
        shared.intersection_update(chain)
    if not shared:
        raise ClusterAssemblyBuildError(
            "binding bones do not share a final-skeleton ancestor: "
            + ", ".join(names)
        )
    return next(name for name in chains[0] if name in shared)


def validate_binding_hierarchy(
    binding,
    skeleton_snapshot,
    wind_bones=None,
    skeleton_by_name=None,
):
    if skeleton_by_name is None:
        checked, skeleton_by_name = _skeleton_maps(skeleton_snapshot)
        del checked
    influences = list((binding or {}).get("bone_influences") or [])
    if not influences:
        raise ClusterAssemblyBuildError("Assembly binding has no bone influences")
    names = []
    total = 0.0
    for item in influences:
        name = str(item.get("bone") or "")
        weight = float(item.get("weight", 0.0))
        if not name or not math.isfinite(weight) or weight <= 0.0:
            raise ClusterAssemblyBuildError(
                f"invalid Assembly bone influence: {item!r}"
            )
        ancestor_chain(name, skeleton_snapshot, skeleton_by_name)
        if wind_bones is not None and name not in wind_bones:
            raise ClusterAssemblyBuildError(
                f"Assembly binding bone is absent from final wind JSON: {name}"
            )
        names.append(name)
        total += weight
    if not math.isfinite(total) or abs(total - 1.0) > 1.0e-4:
        raise ClusterAssemblyBuildError(
            f"Assembly influence weights do not sum to one: {total}"
        )
    anchor = lowest_common_ancestor(
        names,
        skeleton_snapshot,
        skeleton_by_name,
    )
    if wind_bones is not None and anchor not in wind_bones:
        raise ClusterAssemblyBuildError(
            f"Assembly binding ancestor is absent from final wind hierarchy: {anchor}"
        )
    declared = binding.get("anchor_bone")
    if declared and declared != anchor:
        raise ClusterAssemblyBuildError(
            f"Assembly binding anchor mismatch: {declared} != {anchor}"
        )
    for name in names:
        if anchor not in ancestor_chain(
            name,
            skeleton_snapshot,
            skeleton_by_name,
        ):
            raise ClusterAssemblyBuildError(
                f"{anchor} is not an ancestor of binding bone {name}"
            )
    return {
        "anchor_bone": anchor,
        "influence_bones": names,
        "weight_sum": total,
    }


def _joint_name(row):
    return str(
        row.get("JointName")
        or row.get("joint_name")
        or row.get("name")
        or ""
    )


def validate_wind_json_against_skeleton(
    wind_json_or_payload,
    skeleton_snapshot,
    bindings=(),
):
    """Resolve every generated wind joint against the final Skeleton.

    DynamicWind's public JSON stores names, while its imported asset data stores
    final reference-skeleton indices.  The returned ``resolved_joints`` is the
    explicit name/index proof required at this boundary.
    """
    if isinstance(wind_json_or_payload, (str, os.PathLike, Path)):
        wind_path = Path(wind_json_or_payload)
        if not wind_path.is_file():
            raise ClusterAssemblyBuildError(
                f"generated DynamicWind JSON is missing: {wind_path}"
            )
        payload = json.loads(wind_path.read_text(encoding="utf-8"))
        fingerprint = file_fingerprint(wind_path)
    else:
        payload = wind_json_or_payload
        fingerprint = None
    if not isinstance(payload, dict):
        raise ClusterAssemblyBuildError("DynamicWind JSON root is not an object")
    checked, by_name = _skeleton_maps(skeleton_snapshot)
    declared_skeleton = payload.get("SkeletonContract")
    if not isinstance(declared_skeleton, dict):
        raise ClusterAssemblyBuildError(
            "DynamicWind JSON has no final SkeletonContract"
        )
    if int(declared_skeleton.get("SchemaVersion", -1)) != 2:
        raise ClusterAssemblyBuildError(
            "DynamicWind SkeletonContract is not schema version 2"
        )
    if int(declared_skeleton.get("BoneCount", -1)) != checked["bone_count"]:
        raise ClusterAssemblyBuildError(
            "DynamicWind SkeletonContract bone count does not match final Skeleton"
        )
    declared_identity = list(declared_skeleton.get("Bones") or [])
    if len(declared_identity) != checked["bone_count"]:
        raise ClusterAssemblyBuildError(
            "DynamicWind SkeletonContract lacks complete bone identity records"
        )
    for expected, actual in zip(checked["bones"], declared_identity):
        if (
            str(actual.get("BoneName") or "") != expected["name"]
            or int(actual.get("BoneIndex", -1)) != expected["index"]
            or int(actual.get("ParentIndex", -2)) != expected["parent_index"]
        ):
            raise ClusterAssemblyBuildError(
                "DynamicWind SkeletonContract name/index/parent mismatch at "
                f"bone {expected['index']} ({expected['name']})"
            )
    declared_hash = str(
        declared_skeleton.get("BoneNameIndexParentSha1") or ""
    ).casefold()
    if declared_hash != checked["bone_name_index_parent_sha1"]:
        raise ClusterAssemblyBuildError(
            "DynamicWind SkeletonContract identity hash mismatch"
        )
    import_root = declared_skeleton.get("ImportRoot")
    root = checked["bones"][0]
    if not isinstance(import_root, dict) or (
        str(import_root.get("BoneName") or "") != root["name"]
        or int(import_root.get("BoneIndex", -1)) != root["index"]
        or int(import_root.get("ParentIndex", -2)) != root["parent_index"]
    ):
        raise ClusterAssemblyBuildError(
            "DynamicWind SkeletonContract ImportRoot does not match the final Skeleton"
        )
    joints = list(payload.get("Joints") or payload.get("joints") or [])
    if not joints:
        raise ClusterAssemblyBuildError("DynamicWind JSON contains no joints")
    groups = list(
        payload.get("SimulationGroups")
        or payload.get("simulation_groups")
        or []
    )
    resolved = []
    seen = set()
    for row in joints:
        name = _joint_name(row)
        if not name or name in seen:
            raise ClusterAssemblyBuildError(
                f"DynamicWind joint name is missing or duplicated: {name!r}"
            )
        bone = by_name.get(name)
        if bone is None:
            raise ClusterAssemblyBuildError(
                f"DynamicWind joint is missing from final skeleton: {name}"
            )
        group_index = row.get(
            "SimulationGroupIndex",
            row.get("simulation_group_index"),
        )
        if group_index is None:
            raise ClusterAssemblyBuildError(
                f"DynamicWind joint has no simulation group: {name}"
            )
        group_index = int(group_index)
        if group_index < 0 or group_index >= len(groups):
            raise ClusterAssemblyBuildError(
                f"DynamicWind group index is out of range for {name}: {group_index}"
            )
        declared_index = row.get("BoneIndex", row.get("bone_index"))
        declared_parent = row.get("ParentIndex", row.get("parent_index"))
        if declared_index is None or declared_parent is None:
            raise ClusterAssemblyBuildError(
                f"DynamicWind joint has no final bone index/parent: {name}"
            )
        if int(declared_index) != bone["index"]:
            raise ClusterAssemblyBuildError(
                f"DynamicWind bone index mismatch for {name}: "
                f"{declared_index} != {bone['index']}"
            )
        if int(declared_parent) != bone["parent_index"]:
            raise ClusterAssemblyBuildError(
                f"DynamicWind parent index mismatch for {name}: "
                f"{declared_parent} != {bone['parent_index']}"
            )
        resolved.append(
            {
                "joint_name": name,
                "bone_index": bone["index"],
                "simulation_group_index": group_index,
            }
        )
        seen.add(name)
    binding_reports = [
        validate_binding_hierarchy(
            binding,
            checked,
            wind_bones=seen,
            skeleton_by_name=by_name,
        )
        for binding in bindings
    ]
    return {
        "status": "ok",
        "contract": "final_skeleton_v2",
        "skeleton_sha256": checked["sha256"],
        "skeleton_identity_sha1": checked["bone_name_index_parent_sha1"],
        "wind_json": fingerprint,
        "joint_count": len(resolved),
        "simulation_group_count": len(groups),
        "resolved_joints": resolved,
        "binding_hierarchy": binding_reports,
    }


def fit_trs_transform(source_points, target_points):
    """Fit the best Blender-space TRS while reporting affine residuals."""
    try:
        import numpy as np
    except ImportError as exc:  # pragma: no cover - Blender bundles NumPy.
        raise ClusterAssemblyBuildError("NumPy is required for Assembly fitting") from exc
    source = np.asarray(source_points, dtype=np.float64)
    target = np.asarray(target_points, dtype=np.float64)
    if source.shape != target.shape or source.ndim != 2 or source.shape[1] != 3:
        raise ClusterAssemblyBuildError("TRS fit point arrays must both be Nx3")
    if source.shape[0] < 3:
        raise ClusterAssemblyBuildError("TRS fit needs at least three vertices")
    augmented = np.concatenate(
        [source, np.ones((source.shape[0], 1), dtype=np.float64)], axis=1
    )
    coefficients, _, _, _ = np.linalg.lstsq(augmented, target, rcond=None)
    affine = coefficients[:3, :]
    affine_translation = coefficients[3, :]
    affine_prediction = source @ affine + affine_translation

    unsigned_scale = np.linalg.norm(affine, axis=1)
    unsigned_scale[unsigned_scale < 1.0e-12] = 1.0
    normalized_rows = affine / unsigned_scale[:, None]
    best = None
    for signs in itertools.product((-1.0, 1.0), repeat=3):
        signed_rows = np.diag(signs) @ normalized_rows
        left, _, right = np.linalg.svd(signed_rows)
        rotation_rows = left @ right
        if np.linalg.det(rotation_rows) < 0.0:
            left[:, -1] *= -1.0
            rotation_rows = left @ right
        scale = unsigned_scale * np.asarray(signs)
        trs_affine = np.diag(scale) @ rotation_rows
        error = float(np.linalg.norm(affine - trs_affine))
        negative_scales = sum(value < 0.0 for value in scale)
        score = error + 1.0e-8 * max(float(np.linalg.norm(affine)), 1.0) * negative_scales
        if best is None or score < best[0]:
            best = (score, scale, rotation_rows, trs_affine)
    _, scale, rotation_rows, trs_affine = best
    translation = np.mean(target - source @ trs_affine, axis=0)
    prediction = source @ trs_affine + translation
    diagonal = max(
        float(np.linalg.norm(np.max(target, axis=0) - np.min(target, axis=0))),
        1.0e-9,
    )
    affine_rms = float(
        np.sqrt(np.mean(np.sum((target - affine_prediction) ** 2, axis=1)))
    )
    trs_rms = float(np.sqrt(np.mean(np.sum((target - prediction) ** 2, axis=1))))
    quaternion = _rotation_matrix_to_quaternion(rotation_rows.T.tolist())
    return {
        "translation": [float(value) for value in translation],
        "rotation_xyzw": quaternion,
        "scale": [float(value) for value in scale],
        "affine_relative_rms": affine_rms / diagonal,
        "trs_relative_rms": trs_rms / diagonal,
        "shear_relative_norm": float(
            np.linalg.norm(affine - trs_affine)
            / max(float(np.linalg.norm(affine)), 1.0e-9)
        ),
    }


def _rotation_matrix_to_quaternion(matrix):
    m = matrix
    trace = m[0][0] + m[1][1] + m[2][2]
    if trace > 0.0:
        root = math.sqrt(trace + 1.0) * 2.0
        w = 0.25 * root
        x = (m[2][1] - m[1][2]) / root
        y = (m[0][2] - m[2][0]) / root
        z = (m[1][0] - m[0][1]) / root
    elif m[0][0] > m[1][1] and m[0][0] > m[2][2]:
        root = math.sqrt(1.0 + m[0][0] - m[1][1] - m[2][2]) * 2.0
        w = (m[2][1] - m[1][2]) / root
        x = 0.25 * root
        y = (m[0][1] + m[1][0]) / root
        z = (m[0][2] + m[2][0]) / root
    elif m[1][1] > m[2][2]:
        root = math.sqrt(1.0 + m[1][1] - m[0][0] - m[2][2]) * 2.0
        w = (m[0][2] - m[2][0]) / root
        x = (m[0][1] + m[1][0]) / root
        y = 0.25 * root
        z = (m[1][2] + m[2][1]) / root
    else:
        root = math.sqrt(1.0 + m[2][2] - m[0][0] - m[1][1]) * 2.0
        w = (m[1][0] - m[0][1]) / root
        x = (m[0][2] + m[2][0]) / root
        y = (m[1][2] + m[2][1]) / root
        z = 0.25 * root
    length = math.sqrt(x * x + y * y + z * z + w * w)
    return [x / length, y / length, z / length, w / length]


def _component_groups(mesh, polygon_indices):
    polygon_indices = sorted(set(int(value) for value in polygon_indices))
    parent = {index: index for index in polygon_indices}

    def find(value):
        while parent[value] != value:
            parent[value] = parent[parent[value]]
            value = parent[value]
        return value

    def union(left, right):
        left = find(left)
        right = find(right)
        if left != right:
            parent[right] = left

    owners = {}
    for polygon_index in polygon_indices:
        polygon = mesh.polygons[polygon_index]
        for vertex in polygon.vertices:
            previous = owners.setdefault(int(vertex), polygon_index)
            union(polygon_index, previous)
    groups = defaultdict(lambda: {"vertices": set(), "polygons": []})
    for polygon_index in polygon_indices:
        root = find(polygon_index)
        row = groups[root]
        row["polygons"].append(polygon_index)
        row["vertices"].update(int(value) for value in mesh.polygons[polygon_index].vertices)
    result = []
    for row in groups.values():
        result.append(
            {
                "vertices": sorted(row["vertices"]),
                "polygons": sorted(row["polygons"]),
            }
        )
    return sorted(result, key=lambda row: row["polygons"][0])


def _component_signature(mesh, component):
    uv_layer = mesh.uv_layers.active
    uv_faces = []
    polygon_sizes = []
    for polygon_index in component["polygons"]:
        polygon = mesh.polygons[polygon_index]
        polygon_sizes.append(len(polygon.vertices))
        if uv_layer is not None:
            uv_faces.append(
                tuple(
                    sorted(
                        (
                            round(float(uv_layer.data[loop].uv.x), 6),
                            round(float(uv_layer.data[loop].uv.y), 6),
                        )
                        for loop in polygon.loop_indices
                    )
                )
            )
    value = {
        "vertices": len(component["vertices"]),
        "polygons": len(component["polygons"]),
        "polygon_sizes": sorted(polygon_sizes),
        "uv_faces": sorted(uv_faces),
    }
    return hashlib.sha1(_canonical_json(value).encode("utf-8")).hexdigest()[:16]


def _vertex_descriptors(obj, component):
    mesh = obj.data
    uv_layer = mesh.uv_layers.active
    degree = Counter()
    component_edges = set()
    for polygon_index in component["polygons"]:
        vertices = [int(value) for value in mesh.polygons[polygon_index].vertices]
        for index, left in enumerate(vertices):
            right = vertices[(index + 1) % len(vertices)]
            component_edges.add(tuple(sorted((left, right))))
    for left, right in component_edges:
        degree[left] += 1
        degree[right] += 1
    records = defaultdict(list)
    for polygon_index in component["polygons"]:
        polygon = mesh.polygons[polygon_index]
        face_uv = ()
        if uv_layer is not None:
            face_uv = tuple(
                sorted(
                    (
                        round(float(uv_layer.data[loop].uv.x), 6),
                        round(float(uv_layer.data[loop].uv.y), 6),
                    )
                    for loop in polygon.loop_indices
                )
            )
        for loop_index in polygon.loop_indices:
            loop = mesh.loops[loop_index]
            uv = (0.0, 0.0)
            if uv_layer is not None:
                value = uv_layer.data[loop_index].uv
                uv = (round(float(value.x), 6), round(float(value.y), 6))
            records[int(loop.vertex_index)].append((uv, face_uv))
    return sorted(
        (
            (degree[index], tuple(sorted(records[index]))),
            index,
        )
        for index in component["vertices"]
    )


def _ordered_correspondence(obj, template, target):
    source = _vertex_descriptors(obj, template)
    destination = _vertex_descriptors(obj, target)
    if [row[0] for row in source] != [row[0] for row in destination]:
        raise ClusterAssemblyBuildError(
            "part prototype UV/topology descriptors do not match an instance"
        )
    return [row[1] for row in source], [row[1] for row in destination]


def _world_points(obj, vertex_indices):
    return [
        tuple(float(value) for value in (obj.matrix_world @ obj.data.vertices[index].co))
        for index in vertex_indices
    ]


def _component_influences(obj, component):
    totals = Counter()
    for vertex_index in component["vertices"]:
        for item in obj.data.vertices[vertex_index].groups:
            group = obj.vertex_groups[item.group]
            totals[str(group.name)] += float(item.weight)
    total = sum(totals.values())
    if total <= 0.0:
        raise ClusterAssemblyBuildError("part component has no final-skeleton weights")
    return [
        {"bone": name, "weight": value / total}
        for name, value in totals.most_common()
        if value > 0.0
    ]


def _copy_component_as_rigid_part(bpy, source_obj, component, name):
    mesh = source_obj.data
    ordered_vertices = component["vertices"]
    source_world = _world_points(source_obj, ordered_vertices)
    center = [statistics.fmean(values) for values in zip(*source_world)]
    remap = {old: new for new, old in enumerate(ordered_vertices)}
    faces = [
        [remap[int(index)] for index in mesh.polygons[polygon].vertices]
        for polygon in component["polygons"]
    ]
    vertices = [
        tuple(point[axis] - center[axis] for axis in range(3))
        for point in source_world
    ]
    new_mesh = bpy.data.meshes.new(name + "_Mesh")
    new_mesh.from_pydata(vertices, [], faces)
    new_mesh.update()
    new_obj = bpy.data.objects.new(name, new_mesh)
    bpy.context.scene.collection.objects.link(new_obj)
    used_material_indices = sorted(
        {
            int(mesh.polygons[index].material_index)
            for index in component["polygons"]
        }
    )
    material_remap = {
        old_index: new_index
        for new_index, old_index in enumerate(used_material_indices)
    }
    for old_index in used_material_indices:
        new_mesh.materials.append(mesh.materials[old_index])
    for new_polygon, old_index in zip(new_mesh.polygons, component["polygons"]):
        source_polygon = mesh.polygons[old_index]
        new_polygon.material_index = material_remap[source_polygon.material_index]
        new_polygon.use_smooth = source_polygon.use_smooth
    if mesh.uv_layers.active is not None:
        source_uv = mesh.uv_layers.active
        target_uv = new_mesh.uv_layers.new(name=source_uv.name)
        for new_polygon, old_index in zip(new_mesh.polygons, component["polygons"]):
            old_polygon = mesh.polygons[old_index]
            for new_loop, old_loop in zip(
                new_polygon.loop_indices, old_polygon.loop_indices
            ):
                target_uv.data[new_loop].uv = source_uv.data[old_loop].uv
    for attribute in mesh.color_attributes:
        copied = new_mesh.color_attributes.new(
            name=attribute.name,
            type=attribute.data_type,
            domain=attribute.domain,
        )
        if attribute.domain == "POINT":
            for old_index, new_index in remap.items():
                copied.data[new_index].color = attribute.data[old_index].color
        elif attribute.domain == "CORNER":
            for new_polygon, old_index in zip(
                new_mesh.polygons,
                component["polygons"],
            ):
                old_polygon = mesh.polygons[old_index]
                for new_loop, old_loop in zip(
                    new_polygon.loop_indices,
                    old_polygon.loop_indices,
                ):
                    copied.data[new_loop].color = attribute.data[old_loop].color

    armature_data = bpy.data.armatures.new(name + "_PartSkeleton")
    part_armature = bpy.data.objects.new(name + "_PartArmature", armature_data)
    bpy.context.scene.collection.objects.link(part_armature)
    bpy.context.view_layer.objects.active = part_armature
    part_armature.select_set(True)
    bpy.ops.object.mode_set(mode="EDIT")
    bone = armature_data.edit_bones.new("part_root")
    bone.head = (0.0, 0.0, 0.0)
    bone.tail = (0.0, 0.0, 1.0)
    bpy.ops.object.mode_set(mode="OBJECT")
    new_obj.parent = part_armature
    modifier = new_obj.modifiers.new(name="Armature", type="ARMATURE")
    modifier.object = part_armature
    group = new_obj.vertex_groups.new(name="part_root")
    group.add(list(range(len(new_mesh.vertices))), 1.0, "REPLACE")
    return new_obj, part_armature, center


def _copy_base_without_role_polygons(bpy, source_obj, polygon_indices, name):
    import bmesh

    duplicate = source_obj.copy()
    duplicate.data = source_obj.data.copy()
    duplicate.name = name
    duplicate.data.name = name + "_Mesh"
    bpy.context.scene.collection.objects.link(duplicate)
    mesh = duplicate.data
    bm = bmesh.new()
    bm.from_mesh(mesh)
    bm.faces.ensure_lookup_table()
    targets = [bm.faces[index] for index in sorted(set(polygon_indices))]
    bmesh.ops.delete(bm, geom=targets, context="FACES")
    loose = [vertex for vertex in bm.verts if not vertex.link_faces]
    if loose:
        bmesh.ops.delete(bm, geom=loose, context="VERTS")
    bm.to_mesh(mesh)
    bm.free()
    mesh.update()
    if not mesh.polygons:
        raise ClusterAssemblyBuildError("Assembly base would contain no polygons")
    source_materials = list(mesh.materials)
    old_polygon_slots = [int(polygon.material_index) for polygon in mesh.polygons]
    used_slots = sorted(set(old_polygon_slots))
    slot_remap = {
        old_index: new_index
        for new_index, old_index in enumerate(used_slots)
    }
    mesh.materials.clear()
    for old_index in used_slots:
        mesh.materials.append(source_materials[old_index])
    for polygon, old_index in zip(mesh.polygons, old_polygon_slots):
        polygon.material_index = slot_remap[old_index]
    mesh.update()
    return duplicate


def _strip_fbx_scene_textures(scene_data, get_fbx_uuid_from_key):
    """Keep material slots but remove FBX Texture/Video records and links.

    Assembly imports use the existing material-pipeline JSON sidecar as the
    only texture source of truth.  Exporting Blender image records as well
    makes Unreal derive names such as ``M_leaf_elm_01`` from file basenames;
    those collide with the material slots of the same name and can be
    reassigned incorrectly on reimport.
    """
    templates = dict(scene_data.templates)
    templates_users = scene_data.templates_users
    for template_key in (b"TextureFile", b"Video"):
        template = templates.pop(template_key, None)
        if template is not None:
            templates_users -= template.nbr_users

    texture_ids = {
        get_fbx_uuid_from_key(texture_key)
        for texture_key, _fbx_property in scene_data.data_textures.values()
    }
    video_ids = {
        get_fbx_uuid_from_key(video_key)
        for video_key, _texture_keys in scene_data.data_videos.values()
    }
    removed_ids = texture_ids | video_ids
    connections = [
        connection
        for connection in scene_data.connections
        if connection[1] not in removed_ids and connection[2] not in removed_ids
    ]
    return scene_data._replace(
        templates=templates,
        templates_users=templates_users,
        connections=connections,
        data_textures={},
        data_videos={},
    )


@contextmanager
def _textureless_fbx_scene_data(bpy):
    """Patch Blender's stock FBX writer only for one Assembly export."""
    # Ordinary unit tests use a small bpy mock.  Real Blender must provide and
    # successfully patch the stock exporter; there is no textured fallback.
    if not hasattr(bpy, "app"):
        yield
        return
    try:
        import io_scene_fbx.export_fbx_bin as export_fbx_bin
        from io_scene_fbx.fbx_utils import get_fbx_uuid_from_key
    except (ImportError, AttributeError) as exc:
        raise ClusterAssemblyBuildError(
            "Assembly FBX textureless exporter is unavailable"
        ) from exc

    original = export_fbx_bin.fbx_data_from_scene

    def without_textures(scene, depsgraph, settings):
        return _strip_fbx_scene_textures(
            original(scene, depsgraph, settings),
            get_fbx_uuid_from_key,
        )

    export_fbx_bin.fbx_data_from_scene = without_textures
    try:
        yield
    finally:
        export_fbx_bin.fbx_data_from_scene = original


def _validate_textureless_fbx(bpy, path):
    """Fail closed if an Assembly FBX contains any Texture or Video object."""
    if not hasattr(bpy, "app"):
        return {
            "status": "not_available_in_mock",
            "texture_records": 0,
            "video_records": 0,
        }
    try:
        from io_scene_fbx import parse_fbx

        root, version = parse_fbx.parse(str(path))
    except (ImportError, OSError, ValueError) as exc:
        raise ClusterAssemblyBuildError(
            f"Assembly FBX texture-record validation failed: {path}"
        ) from exc
    objects = next((item for item in root.elems if item.id == b"Objects"), None)
    texture_records = 0
    video_records = 0
    if objects is not None:
        texture_records = sum(item.id == b"Texture" for item in objects.elems)
        video_records = sum(item.id == b"Video" for item in objects.elems)
    if texture_records or video_records:
        raise ClusterAssemblyBuildError(
            "Assembly FBX contains texture records that can collide on Unreal "
            f"reimport: textures={texture_records}, videos={video_records}, path={path}"
        )
    return {
        "status": "textureless",
        "fbx_version": version,
        "texture_records": texture_records,
        "video_records": video_records,
        "material_source": "material_pipeline_json_sidecar",
    }


def _weighted_bones_for_base(obj, skeleton_snapshot, skeleton_by_name=None):
    if skeleton_by_name is None:
        checked, skeleton_by_name = _skeleton_maps(skeleton_snapshot)
        del checked
    weighted = set()
    unweighted_vertices = []
    for vertex in obj.data.vertices:
        vertex_weight = 0.0
        for item in vertex.groups:
            weight = float(item.weight)
            if weight <= 0.0:
                continue
            group_name = str(obj.vertex_groups[item.group].name)
            if group_name not in skeleton_by_name:
                raise ClusterAssemblyBuildError(
                    "Assembly base uses a weighted group outside the final Skeleton: "
                    + group_name
                )
            weighted.add(group_name)
            vertex_weight += weight
        if vertex_weight <= 0.0:
            unweighted_vertices.append(int(vertex.index))
    if unweighted_vertices:
        raise ClusterAssemblyBuildError(
            "Assembly base has unweighted vertices: "
            + ", ".join(str(index) for index in unweighted_vertices[:20])
        )
    if not weighted:
        raise ClusterAssemblyBuildError("Assembly base has no final-Skeleton weights")
    return sorted(
        weighted,
        key=lambda name: int(skeleton_by_name[name]["index"]),
    )


def _export_selected_fbx(bpy, path, objects):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    bpy.ops.object.mode_set(mode="OBJECT") if bpy.context.object and bpy.context.object.mode != "OBJECT" else None
    bpy.ops.object.select_all(action="DESELECT")
    previous = []
    for obj in objects:
        previous.append((obj, bool(obj.hide_get()), bool(obj.hide_viewport)))
        obj.hide_viewport = False
        obj.hide_set(False)
        obj.select_set(True)
    bpy.context.view_layer.objects.active = objects[0]
    try:
        with _textureless_fbx_scene_data(bpy):
            result = bpy.ops.export_scene.fbx(
                filepath=str(path),
                use_selection=True,
                object_types={"ARMATURE", "MESH"},
                # Keep BASE/PART reference poses on the exact stock-FBX contract
                # used by speedtree_bone_weight_repair.core for the Full SK.  A
                # separate unit-scale or modifier policy changes the imported
                # armature-object root (bone 0), even when every authored Blender
                # bone has the same name/order/parent/bind matrix.
                use_mesh_modifiers=False,
                mesh_smooth_type="FACE",
                use_custom_props=False,
                add_leaf_bones=False,
                primary_bone_axis="Y",
                secondary_bone_axis="X",
                armature_nodetype="NULL",
                bake_anim=False,
                path_mode="AUTO",
            )
        if "FINISHED" not in result:
            raise ClusterAssemblyBuildError(f"FBX export failed: {result}")
    finally:
        for obj, hidden, hide_viewport in previous:
            obj.hide_viewport = hide_viewport
            obj.hide_set(hidden)
    if not path.is_file():
        raise ClusterAssemblyBuildError(f"FBX export produced no file: {path}")
    return _validate_textureless_fbx(bpy, path)


def _role_material_polygons(merged_mesh, role_inputs):
    materials = list(merged_mesh.data.materials)
    slots_by_identity = defaultdict(set)
    for index, material in enumerate(materials):
        identity = normalize_role_identity(getattr(material, "name", ""))
        slots_by_identity[identity].add(index)
    result = {}
    for row in role_inputs:
        role = str(row["role"]).casefold()
        identity = normalize_role_identity(row.get("role_identity"))
        slots = slots_by_identity.get(identity) or set()
        polygons = [
            int(polygon.index)
            for polygon in merged_mesh.data.polygons
            if int(polygon.material_index) in slots
        ]
        if not slots or not polygons:
            raise ClusterAssemblyBuildError(
                f"final BWR mesh lost the actual {role} material/mesh pair: {identity}"
            )
        result[role] = {
            "role": role,
            "role_identity": row.get("role_identity"),
            "material_slots": sorted(slots),
            "polygon_indices": polygons,
        }
    return result


def build_blender_assembly_inputs(
    handoff,
    final_armature,
    final_merged_mesh,
    output_dir,
    full_fbx_path,
    wind_json_path,
):
    """Generate base/part FBXs and a strict builder manifest inside BWR.

    No ``.blend`` is created and the supplied Full FBX is never rewritten.
    """
    decision = content_build_decision(handoff)
    if decision == "pass_through":
        return {
            "schema_version": SCHEMA_VERSION,
            "kind": MANIFEST_KIND,
            "status": "pass_through",
            "full_skeletal_mesh_preserved": True,
        }
    try:
        import bpy
    except ImportError as exc:  # pragma: no cover - only executes in Blender.
        raise ClusterAssemblyBuildError("Blender bpy is required") from exc
    if getattr(final_merged_mesh, "type", "") != "MESH":
        raise ClusterAssemblyBuildError("final BWR merged mesh is invalid")
    full_fingerprint_before = file_fingerprint(full_fbx_path)
    if not full_fingerprint_before["exists"]:
        raise ClusterAssemblyBuildError(f"existing Full SK FBX is missing: {full_fbx_path}")
    snapshot = snapshot_blender_armature(final_armature)
    checked_snapshot, skeleton_by_name = _skeleton_maps(snapshot)
    snapshot = checked_snapshot
    role_inputs = list((handoff.get("assembly") or {}).get("part_builder_inputs") or [])
    roles = _role_material_polygons(final_merged_mesh, role_inputs)
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    stem = Path(str((handoff.get("spm") or {}).get("path") or full_fbx_path)).stem
    base_fbx = output / f"BASE_{stem}.fbx"
    manifest_path = output / f"{stem}_cluster_assembly_bindings.json"
    excluded_polygons = sorted(
        {index for row in roles.values() for index in row["polygon_indices"]}
    )
    created_objects = []
    parts = []
    all_bindings = []
    base_obj = None
    try:
        base_obj = _copy_base_without_role_polygons(
            bpy,
            final_merged_mesh,
            excluded_polygons,
            f"BASE_{stem}",
        )
        created_objects.append(base_obj)
        base_weighted_bones = _weighted_bones_for_base(
            base_obj,
            snapshot,
            skeleton_by_name,
        )
        base_fbx_texture_contract = _export_selected_fbx(
            bpy,
            base_fbx,
            [final_armature, base_obj],
        )
        for role in ROLE_ORDER:
            role_row = roles.get(role)
            if role_row is None:
                continue
            components = _component_groups(
                final_merged_mesh.data,
                role_row["polygon_indices"],
            )
            by_signature = defaultdict(list)
            for component in components:
                by_signature[_component_signature(final_merged_mesh.data, component)].append(component)
            for signature, instances in sorted(by_signature.items()):
                template = instances[0]
                prototype_id = f"{role}_{signature}"
                part_obj, part_armature, center = _copy_component_as_rigid_part(
                    bpy,
                    final_merged_mesh,
                    template,
                    "PART_" + prototype_id,
                )
                created_objects.extend([part_obj, part_armature])
                part_fbx = output / f"PART_{prototype_id}.fbx"
                part_fbx_texture_contract = _export_selected_fbx(
                    bpy,
                    part_fbx,
                    [part_armature, part_obj],
                )
                bindings = []
                for instance_index, component in enumerate(instances):
                    source_indices, target_indices = _ordered_correspondence(
                        final_merged_mesh,
                        template,
                        component,
                    )
                    source_world = _world_points(final_merged_mesh, source_indices)
                    source_centered = [
                        [point[axis] - center[axis] for axis in range(3)]
                        for point in source_world
                    ]
                    target_world = _world_points(final_merged_mesh, target_indices)
                    transform = fit_trs_transform(source_centered, target_world)
                    influences = _component_influences(final_merged_mesh, component)
                    binding = {
                        "instance": instance_index,
                        "component_polygon_indices": component["polygons"],
                        "transform": transform,
                        "bone_influences": influences,
                    }
                    hierarchy = validate_binding_hierarchy(
                        binding,
                        snapshot,
                        skeleton_by_name=skeleton_by_name,
                    )
                    binding["anchor_bone"] = hierarchy["anchor_bone"]
                    bindings.append(binding)
                    all_bindings.append(binding)
                parts.append(
                    {
                        "prototype_id": prototype_id,
                        "role": role,
                        "role_identity": role_row["role_identity"],
                        "topology_signature": signature,
                        "fbx": file_fingerprint(part_fbx),
                        "fbx_texture_contract": part_fbx_texture_contract,
                        "template": {
                            "vertex_count": len(template["vertices"]),
                            "polygon_count": len(template["polygons"]),
                            "center": center,
                        },
                        "bindings": bindings,
                        "fit_summary": {
                            "trs_relative_rms_median": statistics.median(
                                row["transform"]["trs_relative_rms"] for row in bindings
                            ),
                            "trs_relative_rms_max": max(
                                row["transform"]["trs_relative_rms"] for row in bindings
                            ),
                            "affine_relative_rms_median": statistics.median(
                                row["transform"]["affine_relative_rms"] for row in bindings
                            ),
                            "affine_relative_rms_max": max(
                                row["transform"]["affine_relative_rms"] for row in bindings
                            ),
                        },
                    }
                )
        wind_validation = validate_wind_json_against_skeleton(
            wind_json_path,
            snapshot,
            all_bindings,
        )
        wind_bones = {
            joint["joint_name"]
            for joint in wind_validation["resolved_joints"]
        }
        missing_base_wind_bones = [
            name for name in base_weighted_bones if name not in wind_bones
        ]
        if missing_base_wind_bones:
            raise ClusterAssemblyBuildError(
                "Assembly base weights are absent from the final wind hierarchy: "
                + ", ".join(missing_base_wind_bones[:20])
            )
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "kind": MANIFEST_KIND,
            "status": "ready",
            "content_decision": "build",
            "full_skeletal_mesh_preserved": True,
            "full_fbx": full_fingerprint_before,
            "base": {
                "fbx": file_fingerprint(base_fbx),
                "fbx_texture_contract": base_fbx_texture_contract,
                "excluded_role_polygon_count": len(excluded_polygons),
                "final_armature": final_armature.name,
                "weighted_bones": base_weighted_bones,
                "weighted_bone_count": len(base_weighted_bones),
                "all_weighted_bones_in_final_wind": True,
            },
            "parts": parts,
            "final_skeleton": snapshot,
            "wind_contract": wind_validation,
            "coordinate_contract": {
                "source": "Blender world, Z-up, right-handed",
                "target": "Unreal local, Z-up, left-handed, centimeters",
                "centimeters_per_blender_unit": (
                    100.0 * float(bpy.context.scene.unit_settings.scale_length)
                ),
                "translation_axis_map": ["x", "-y", "z"],
                "rotation_quaternion_axis_map": ["-x", "y", "-z", "w"],
                "transform_space": "Local",
            },
            "handoff_evidence": {
                "actual_fbx": handoff.get("actual_fbx"),
                "pcg_receipt": handoff.get("pcg_receipt"),
                "spm": handoff.get("spm"),
                "roles": roles,
            },
        }
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        manifest["manifest"] = file_fingerprint(manifest_path)
    finally:
        for obj in reversed(created_objects):
            data = getattr(obj, "data", None)
            object_type = str(getattr(obj, "type", ""))
            if obj.name in bpy.data.objects:
                bpy.data.objects.remove(obj, do_unlink=True)
            if data is not None and getattr(data, "users", 1) == 0:
                collection = (
                    bpy.data.armatures
                    if object_type == "ARMATURE"
                    else bpy.data.meshes
                )
                if data.name in collection:
                    collection.remove(data)
    full_fingerprint_after = file_fingerprint(full_fbx_path)
    if full_fingerprint_after != full_fingerprint_before:
        raise ClusterAssemblyBuildError("existing Full SK FBX changed during Assembly build")
    return manifest


def validate_unreal_asset_contract(manifest, asset_contract):
    if (manifest or {}).get("status") != "ready":
        raise ClusterAssemblyBuildError("Assembly manifest is not ready")
    required = ("full_skeletal_mesh", "base_skeletal_mesh", "assembly")
    for key in required:
        value = str((asset_contract or {}).get(key) or "")
        if not value.startswith("/Game/"):
            raise ClusterAssemblyBuildError(f"invalid Unreal asset path for {key}: {value}")
    if asset_contract["full_skeletal_mesh"] in {
        asset_contract["base_skeletal_mesh"],
        asset_contract["assembly"],
    }:
        raise ClusterAssemblyBuildError(
            "Full Skeletal Mesh must remain separate from base and Assembly assets"
        )
    part_paths = asset_contract.get("parts") or {}
    expected = {row["prototype_id"] for row in manifest.get("parts") or []}
    if set(part_paths) != expected:
        raise ClusterAssemblyBuildError(
            "Unreal part asset paths do not exactly match Blender prototypes"
        )
    if any(not str(path).startswith("/Game/") for path in part_paths.values()):
        raise ClusterAssemblyBuildError("invalid Unreal part asset path")
    return {
        "full_skeletal_mesh": asset_contract["full_skeletal_mesh"],
        "base_skeletal_mesh": asset_contract["base_skeletal_mesh"],
        "parts": dict(part_paths),
        "assembly": asset_contract["assembly"],
    }


def _rewrite_command_asset_path(command_groups, source_path, target_path):
    rewritten = []
    for commands in command_groups or []:
        if not isinstance(commands, list):
            raise ClusterAssemblyBuildError(
                "generated Assembly import commands are not grouped lists"
            )
        rewritten.append([
            str(command).replace(str(source_path), str(target_path))
            for command in commands
        ])
    return rewritten


def _generated_material_sidecar(template_data, generated_mesh_name, output_dir):
    source_value = str(template_data.get("_material_pipeline_json_path") or "")
    source_path = Path(source_value)
    if not source_value or not source_path.is_file():
        raise ClusterAssemblyBuildError(
            "Assembly generated imports require the existing Full SK material sidecar"
        )
    try:
        payload = json.loads(source_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ClusterAssemblyBuildError(
            f"Full SK material sidecar is unreadable: {source_path}"
        ) from exc
    descriptor = payload.get("speedtree_handoff_contract")
    if not isinstance(descriptor, dict):
        raise ClusterAssemblyBuildError(
            "Full SK material sidecar has no SpeedTree handoff descriptor"
        )
    payload["mesh_name"] = generated_mesh_name
    descriptor["mesh_name"] = generated_mesh_name
    validation = payload.get("validation_children")
    if isinstance(validation, dict):
        validation["asset_unit"] = generated_mesh_name
        validation["json_name"] = generated_mesh_name
    target_dir = Path(output_dir) / "material_sidecars"
    target_dir.mkdir(parents=True, exist_ok=True)
    target_path = target_dir / f"{generated_mesh_name}.json"
    encoded = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    temporary = target_path.with_name(
        f".{target_path.name}.{os.getpid()}.tmp"
    )
    temporary.write_text(encoded, encoding="utf-8")
    os.replace(temporary, target_path)
    return {
        "source_path": str(source_path.resolve()),
        "generated": file_fingerprint(target_path),
    }


def scope_material_pipeline_to_codex_tests(
    manifest_assets,
    unreal_folder,
    output_dir,
):
    """Keep test material, layer-instance, and texture writes under Codex/Tests.

    The scope is inferred from the existing Unreal destination path.  It is not
    a feature flag and production destinations are rejected rather than
    silently redirected.
    """
    folder = str(unreal_folder or "").rstrip("/")
    if not folder.startswith("/Game/Codex/Tests/"):
        raise ClusterAssemblyBuildError(
            "isolated material scoping requires a /Game/Codex/Tests destination"
        )
    material_root = folder + "/_MaterialPipeline"
    target_dir = Path(output_dir) / "material_sidecars"
    target_dir.mkdir(parents=True, exist_ok=True)
    scoped = []
    for manifest_asset in manifest_assets or []:
        data = manifest_asset.get("asset_data") or {}
        asset_path = str(data.get("asset_path") or "").split(".", 1)[0].rstrip("/")
        if not asset_path or not asset_path.startswith(folder + "/"):
            raise ClusterAssemblyBuildError(
                "Codex test material scope requires every manifest asset under "
                f"{folder}: {asset_path or '<missing asset_path>'}"
            )
        source_value = str(data.get("_material_pipeline_json_path") or "")
        source_path = Path(source_value)
        if not source_value or not source_path.is_file():
            raise ClusterAssemblyBuildError(
                "Codex test import requires an existing material sidecar: "
                + source_value
            )
        payload = json.loads(source_path.read_text(encoding="utf-8"))
        materials = list(payload.get("materials") or [])
        if not materials:
            raise ClusterAssemblyBuildError(
                "Codex test material sidecar contains no materials"
            )
        presets = set()
        for entry in materials:
            if str(entry.get("instance_profile") or "").strip():
                raise ClusterAssemblyBuildError(
                    "Codex test material scope cannot mutate a user-managed instance profile"
                )
            preset = str(
                entry.get("master_preset")
                or payload.get("material_master")
                or ""
            )
            if not preset:
                raise ClusterAssemblyBuildError(
                    "Codex test material has no master preset"
                )
            presets.add(preset)
            old_target = str(
                entry.get("target_material_path")
                or entry.get("material_instance_path")
                or entry.get("unreal_material_path")
                or ""
            ).split(".", 1)[0]
            target_name = old_target.rsplit("/", 1)[-1]
            if not target_name:
                base = str(
                    (entry.get("speedtree_intent") or {}).get(
                        "material_instance_base"
                    )
                    or entry.get("name")
                    or "Material"
                )
                target_name = "MI_" + base.removeprefix("M_").removeprefix("MI_")
            if not target_name.startswith("MI_"):
                target_name = "MI_" + target_name
            entry["target_material_path"] = (
                material_root + "/MI/" + target_name
            )
            for alias in ("material_instance_path", "unreal_material_path"):
                if alias in entry:
                    entry[alias] = entry["target_material_path"]
            layer = entry.get("material_layer")
            if not isinstance(layer, dict):
                layer = {}
                entry["material_layer"] = layer
            old_path = str(layer.get("instance_path") or "")
            instance_name = old_path.rsplit("/", 1)[-1]
            if not instance_name:
                base = str(
                    (entry.get("speedtree_intent") or {}).get(
                        "material_instance_base"
                    )
                    or entry.get("name")
                    or "Material"
                )
                instance_name = "MYI_" + base.removeprefix("M_")
            if not instance_name.startswith("MYI_"):
                instance_name = "MYI_" + instance_name
            layer["instance_path"] = (
                material_root + "/MYI/" + instance_name
            )
            for alias in (
                "material_layer_instance_path",
                "layer_instance_path",
                "target_layer_instance_path",
            ):
                if alias in entry:
                    entry[alias] = layer["instance_path"]
        asset_name = str(data.get("asset_path") or "Asset").rsplit("/", 1)[-1]
        payload["codex_test_asset_scope"] = {
            "root": material_root,
            "mesh": str(data.get("asset_path") or ""),
            "production_materials_preserved": True,
        }
        target_path = target_dir / f"{asset_name}.codex_test.json"
        encoded = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        temporary = target_path.with_name(
            f".{target_path.name}.{os.getpid()}.tmp"
        )
        temporary.write_text(encoded, encoding="utf-8")
        os.replace(temporary, target_path)
        target_text = str(target_path.resolve()).replace("\\", "/")
        source_variants = {
            str(source_path),
            str(source_path).replace("\\", "/"),
        }
        config_lines = [
            f"_p.TEXTURES_FOLDER = r'{material_root}/Textures'",
            "_p.TEXTURE_IMPORT_CACHE = r'"
            + str((Path(output_dir) / "_texture_import_cache.json").resolve()).replace("\\", "/")
            + "'",
        ]
        for preset in sorted(presets):
            config_lines.extend(
                [
                    f"_p.MASTER_PRESETS[{preset!r}]['mi_folder'] = r'{material_root}/MI'",
                    f"_p.MASTER_PRESETS[{preset!r}]['layer_instance_folder'] = r'{material_root}/MYI'",
                ]
            )
        for key in ("pre_import_commands", "post_import_commands"):
            groups = manifest_asset.get(key) or []
            for commands in groups:
                for index, command in enumerate(commands):
                    rewritten = str(command)
                    for source in source_variants:
                        rewritten = rewritten.replace(source, target_text)
                    commands[index] = rewritten
                marker = next(
                    (
                        index
                        for index, command in enumerate(commands)
                        if "_spec.loader.exec_module(_p)" in str(command)
                    ),
                    None,
                )
                if marker is None:
                    raise ClusterAssemblyBuildError(
                        "material pipeline command has no module-load marker"
                    )
                commands[marker + 1:marker + 1] = config_lines
        data["_material_pipeline_json_path"] = target_text
        data["_material_pipeline_json_fingerprint"] = file_fingerprint(
            target_path
        )
        scoped.append(
            {
                "asset_path": data.get("asset_path"),
                "material_root": material_root,
                "sidecar": data["_material_pipeline_json_fingerprint"],
            }
        )
    return {
        "status": "scoped_to_codex_tests",
        "material_root": material_root,
        "assets": scoped,
    }


def build_unreal_ingest_plan(manifest, full_manifest_asset, full_asset_path, unreal_folder):
    """Derive generated imports from the existing Send2UE Full-mesh contract."""
    if (manifest or {}).get("status") == "pass_through":
        return {"status": "pass_through", "assets": [], "asset_contract": None}
    if (manifest or {}).get("status") != "ready":
        raise ClusterAssemblyBuildError("Assembly input manifest is not ready")
    validate_manifest_artifacts(manifest)
    if not isinstance(full_manifest_asset, dict):
        raise ClusterAssemblyBuildError("Send2UE Full-mesh manifest asset is missing")
    template_data = full_manifest_asset.get("asset_data") or {}
    if template_data.get("_asset_type") != "SkeletalMesh":
        raise ClusterAssemblyBuildError("Assembly imports require a SkeletalMesh template")
    source_asset_path = str(full_asset_path or template_data.get("asset_path") or "")
    if not source_asset_path.startswith("/Game/"):
        raise ClusterAssemblyBuildError("invalid Full Skeletal Mesh asset path")
    folder = str(unreal_folder or "").rstrip("/") + "/Assembly"
    generated_output_dir = Path(
        ((manifest.get("base") or {}).get("fbx") or {}).get("path")
    ).parent

    def generated_asset(file_path, asset_path, asset_id, use_full_skeleton):
        source = Path(file_path)
        if not source.is_file():
            raise ClusterAssemblyBuildError(f"generated Assembly FBX is missing: {source}")
        row = deepcopy(full_manifest_asset)
        data = row.setdefault("asset_data", {})
        data["file_path"] = str(source.resolve())
        data["asset_folder"] = folder + "/"
        data["asset_path"] = asset_path
        data["_mesh_object_name"] = source.stem
        data["empty_object_name"] = source.stem
        data["skeleton_asset_path"] = "__FULL_FINAL_SKELETON__" if use_full_skeleton else ""
        sidecar = _generated_material_sidecar(
            template_data,
            source.stem,
            generated_output_dir,
        )
        data["_material_pipeline_json_path"] = sidecar["generated"]["path"]
        data["_material_pipeline_json_fingerprint"] = sidecar["generated"]
        row["asset_id"] = asset_id
        for key in ("pre_import_commands", "post_import_commands"):
            row[key] = _rewrite_command_asset_path(
                row.get(key), source_asset_path, asset_path
            )
            for commands in row[key]:
                for index, command in enumerate(commands):
                    rewritten = str(command)
                    for old_path in {
                        sidecar["source_path"],
                        sidecar["source_path"].replace("\\", "/"),
                    }:
                        rewritten = rewritten.replace(
                            old_path,
                            sidecar["generated"]["path"].replace("\\", "/"),
                        )
                    commands[index] = rewritten
        return row

    base_fbx = ((manifest.get("base") or {}).get("fbx") or {}).get("path")
    base_path = folder + "/" + Path(str(base_fbx)).stem
    assets = [generated_asset(base_fbx, base_path, "cluster_assembly_base", True)]
    part_paths = {}
    for part in manifest.get("parts") or []:
        prototype_id = str(part.get("prototype_id") or "")
        part_fbx = (part.get("fbx") or {}).get("path")
        if not prototype_id or not part_fbx:
            raise ClusterAssemblyBuildError("Assembly part manifest is incomplete")
        part_path = folder + "/" + Path(str(part_fbx)).stem
        if prototype_id in part_paths:
            raise ClusterAssemblyBuildError(
                f"duplicate Assembly prototype id: {prototype_id}"
            )
        part_paths[prototype_id] = part_path
        assets.append(
            generated_asset(
                part_fbx,
                part_path,
                "cluster_assembly_part_" + prototype_id,
                False,
            )
        )
    asset_contract = {
        "full_skeletal_mesh": source_asset_path,
        "base_skeletal_mesh": base_path,
        "parts": part_paths,
        "assembly": folder + "/" + Path(source_asset_path).name + "_NaniteAssembly",
    }
    validate_unreal_asset_contract(manifest, asset_contract)
    return {
        "status": "ready",
        "assets": assets,
        "asset_contract": asset_contract,
    }


def _unwrap_struct_result(result, expected_class):
    if isinstance(result, tuple):
        success = next((item for item in result if isinstance(item, bool)), False)
        value = next(
            (item for item in result if isinstance(item, expected_class)),
            None,
        )
        return success, value
    if isinstance(result, expected_class):
        return True, result
    return bool(result), None


def _unreal_bone_names(unreal, mesh):
    component = unreal.new_object(unreal.SkeletalMeshComponent)
    if hasattr(component, "set_skinned_asset_and_update"):
        component.set_skinned_asset_and_update(mesh, False)
    else:  # pragma: no cover - compatibility for older Python exposure.
        component.set_editor_property("skeletal_mesh_asset", mesh)
    return [
        str(component.get_bone_name(index))
        for index in range(component.get_num_bones())
    ]


def _unreal_transform(unreal, transform, coordinate_contract):
    factor = float(coordinate_contract["centimeters_per_blender_unit"])
    translation = transform["translation"]
    rotation = transform["rotation_xyzw"]
    scale = transform["scale"]
    return unreal.Transform(
        location=unreal.Vector(
            float(translation[0]) * factor,
            -float(translation[1]) * factor,
            float(translation[2]) * factor,
        ),
        rotation=unreal.Quat(
            -float(rotation[0]),
            float(rotation[1]),
            -float(rotation[2]),
            float(rotation[3]),
        ).rotator(),
        scale=unreal.Vector(float(scale[0]), float(scale[1]), float(scale[2])),
    )


def _dynamic_wind_user_data(unreal, mesh):
    del unreal
    return [
        item
        for item in list(mesh.get_editor_property("asset_user_data") or [])
        if item.get_class().get_path_name()
        == "/Script/DynamicWind.DynamicWindSkeletalData"
    ]


def build_unreal_nanite_assembly(unreal, manifest, asset_contract):
    """Build and save the separate UE 5.8 Assembly from imported inputs.

    The existing Full SK must already have its newly generated DynamicWind data.
    The same wind JSON is imported anew for the Assembly after the native build.
    """
    validate_manifest_artifacts(manifest)
    paths = validate_unreal_asset_contract(manifest, asset_contract)

    def load_skeletal(path):
        asset = unreal.EditorAssetLibrary.load_asset(path)
        if not isinstance(asset, unreal.SkeletalMesh):
            raise ClusterAssemblyBuildError(f"not a SkeletalMesh: {path}")
        return asset

    full = load_skeletal(paths["full_skeletal_mesh"])
    base = load_skeletal(paths["base_skeletal_mesh"])
    part_assets = {
        key: load_skeletal(value) for key, value in paths["parts"].items()
    }
    expected_bones = [
        row["name"] for row in manifest["final_skeleton"]["bones"]
    ]
    actual_bones = _unreal_bone_names(unreal, full)
    if actual_bones != expected_bones:
        raise ClusterAssemblyBuildError(
            "Full SK final Skeleton does not match the BWR Assembly manifest"
        )
    full_skeleton = full.get_editor_property("skeleton")
    base_skeleton = base.get_editor_property("skeleton")
    if full_skeleton is None or base_skeleton is None:
        raise ClusterAssemblyBuildError("Full/base Skeletal Mesh has no Skeleton")
    if full_skeleton.get_path_name() != base_skeleton.get_path_name():
        raise ClusterAssemblyBuildError(
            "Assembly base does not use the Full SK final Skeleton"
        )
    if not _dynamic_wind_user_data(unreal, full):
        raise ClusterAssemblyBuildError(
            "Full SK has no newly generated DynamicWindSkeletalData"
        )
    all_bindings = [
        binding
        for part in manifest.get("parts") or []
        for binding in part.get("bindings") or []
    ]
    wind_path = ((manifest.get("wind_contract") or {}).get("wind_json") or {}).get("path")
    wind_validation = validate_wind_json_against_skeleton(
        wind_path,
        manifest["final_skeleton"],
        all_bindings,
    )
    expected_wind = manifest.get("wind_contract") or {}
    if wind_validation["skeleton_sha256"] != expected_wind.get("skeleton_sha256"):
        raise ClusterAssemblyBuildError("wind/final Skeleton contract hash changed")
    if (wind_validation.get("wind_json") or {}).get("sha256") != (
        expected_wind.get("wind_json") or {}
    ).get("sha256"):
        raise ClusterAssemblyBuildError("generated wind JSON changed after Blender handoff")
    checked_skeleton, skeleton_by_name = _skeleton_maps(
        manifest["final_skeleton"]
    )
    del checked_skeleton
    wind_bones = {
        joint["joint_name"]
        for joint in wind_validation["resolved_joints"]
    }
    base_contract = manifest.get("base") or {}
    base_weighted_bones = list(base_contract.get("weighted_bones") or [])
    if (
        not base_weighted_bones
        or int(base_contract.get("weighted_bone_count", -1))
        != len(base_weighted_bones)
        or not base_contract.get("all_weighted_bones_in_final_wind")
    ):
        raise ClusterAssemblyBuildError(
            "Assembly base has no verified final wind-bone weight contract"
        )
    for bone_name in base_weighted_bones:
        if bone_name not in skeleton_by_name or bone_name not in wind_bones:
            raise ClusterAssemblyBuildError(
                "Assembly base weighted bone is outside the final wind hierarchy: "
                + str(bone_name)
            )

    assembly_path = paths["assembly"]
    directory, name = assembly_path.rsplit("/", 1)
    parameters = unreal.NaniteAssemblyCreateNewParameters()
    parameters.set_editor_property(
        "target_directory",
        unreal.DirectoryPath(path=directory),
    )
    parameters.set_editor_property("asset_name", name)
    parameters.set_editor_property("overwrite_existing", True)
    builder = unreal.NaniteAssemblySkeletalMeshBuilder.begin_new_skeletal_mesh_assembly_build(
        parameters,
        base,
    )
    if builder is None:
        raise ClusterAssemblyBuildError(
            "BeginNewSkeletalMeshAssemblyBuild failed"
        )
    built_parts = []
    for part in manifest.get("parts") or []:
        bindings = []
        for row in part.get("bindings") or []:
            validate_binding_hierarchy(
                row,
                manifest["final_skeleton"],
                wind_bones=wind_bones,
                skeleton_by_name=skeleton_by_name,
            )
            influences = list(row["bone_influences"])
            primary = influences[0]
            result = builder.create_binding_by_bone_name(
                primary["bone"],
                float(primary["weight"]),
                _unreal_transform(
                    unreal,
                    row["transform"],
                    manifest["coordinate_contract"],
                ),
                unreal.NaniteAssemblyNodeTransformSpace.LOCAL,
            )
            success, binding = _unwrap_struct_result(
                result,
                unreal.NaniteAssemblySkeletalMeshPartBinding,
            )
            if not success or binding is None:
                raise ClusterAssemblyBuildError(
                    f"primary Assembly binding failed: {primary['bone']}"
                )
            for influence in influences[1:]:
                add_result = builder.add_bone_influence_by_name(
                    binding,
                    influence["bone"],
                    float(influence["weight"]),
                )
                if isinstance(add_result, tuple):
                    add_success = next(
                        (value for value in add_result if isinstance(value, bool)),
                        False,
                    )
                    updated = next(
                        (
                            value
                            for value in add_result
                            if isinstance(
                                value,
                                unreal.NaniteAssemblySkeletalMeshPartBinding,
                            )
                        ),
                        None,
                    )
                    if updated is not None:
                        binding = updated
                else:
                    add_success = bool(add_result)
                if not add_success:
                    raise ClusterAssemblyBuildError(
                        f"additional Assembly binding failed: {influence['bone']}"
                    )
            bindings.append(binding)
        if not builder.add_assembly_parts(
            part_assets[part["prototype_id"]],
            bindings,
        ):
            raise ClusterAssemblyBuildError(
                f"AddAssemblyParts failed: {part['prototype_id']}"
            )
        built_parts.append(
            {
                "prototype_id": part["prototype_id"],
                "bindings": len(bindings),
            }
        )
    finish = builder.finish_assembly_build()
    success, assembly = _unwrap_struct_result(finish, unreal.SkeletalMesh)
    if not success or assembly is None:
        raise ClusterAssemblyBuildError(f"FinishAssemblyBuild failed: {finish!r}")
    assembly_skeleton = assembly.get_editor_property("skeleton")
    if (
        assembly_skeleton is None
        or assembly_skeleton.get_path_name() != full_skeleton.get_path_name()
    ):
        raise ClusterAssemblyBuildError(
            "finished Assembly does not use the Full SK final Skeleton"
        )
    if not hasattr(unreal, "CodexDynamicWindImportLibrary"):
        raise ClusterAssemblyBuildError("CodexDynamicWindImportLibrary is unavailable")
    pose_sync_result = (
        unreal.CodexDynamicWindImportLibrary
        .synchronize_mesh_reference_pose_to_skeleton(assembly)
    )
    try:
        reference_pose_sync = json.loads(str(pose_sync_result))
    except (TypeError, ValueError) as exc:
        raise ClusterAssemblyBuildError(
            "Assembly reference-pose synchronization returned invalid JSON: "
            f"{pose_sync_result!r}"
        ) from exc
    if not reference_pose_sync.get("success"):
        raise ClusterAssemblyBuildError(
            "Assembly reference pose could not be synchronized to the Full SK "
            "final Skeleton: "
            + str(reference_pose_sync.get("error") or reference_pose_sync)
        )
    result = unreal.CodexDynamicWindImportLibrary.import_dynamic_wind_json_to_skeletal_mesh(
        assembly,
        str(wind_path),
    )
    try:
        wind_import = json.loads(str(result))
    except (TypeError, ValueError) as exc:
        raise ClusterAssemblyBuildError(
            f"Assembly DynamicWind import returned invalid JSON: {result!r}"
        ) from exc
    if not wind_import.get("success"):
        raise ClusterAssemblyBuildError(
            "Assembly DynamicWind regeneration failed: "
            + str(wind_import.get("error") or wind_import)
        )
    if wind_import.get("skeleton_contract") != "final_skeleton_v2":
        raise ClusterAssemblyBuildError(
            "Assembly DynamicWind importer did not confirm final_skeleton_v2"
        )
    if not wind_import.get("skeleton_hash"):
        raise ClusterAssemblyBuildError(
            "Assembly DynamicWind importer returned no final Skeleton hash"
        )
    if not wind_import.get("skeleton_bind_pose_matches"):
        raise ClusterAssemblyBuildError(
            "Assembly mesh-local reference pose does not match the Full SK final Skeleton"
        )
    expected_ue_skeleton_hash = manifest["final_skeleton"][
        "bone_name_index_parent_sha1"
    ]
    if str(wind_import["skeleton_hash"]).casefold() != expected_ue_skeleton_hash:
        raise ClusterAssemblyBuildError(
            "Assembly UE Skeleton name/index/parent hash does not match BWR output"
        )
    if not _dynamic_wind_user_data(unreal, assembly):
        raise ClusterAssemblyBuildError(
            "finished Assembly has no regenerated DynamicWindSkeletalData"
        )
    unreal.EditorAssetLibrary.save_loaded_asset(assembly, only_if_is_dirty=False)
    return {
        "status": "ok",
        "full_skeletal_mesh": paths["full_skeletal_mesh"],
        "full_skeletal_mesh_preserved": True,
        "assembly": assembly.get_path_name(),
        "final_skeleton": full_skeleton.get_path_name(),
        "final_skeleton_bones": len(actual_bones),
        "production_skeleton_required": False,
        "parts": built_parts,
        "binding_count": sum(row["bindings"] for row in built_parts),
        "base_weighted_bone_count": len(base_weighted_bones),
        "base_weights_in_final_wind": True,
        "reference_pose_sync": reference_pose_sync,
        "wind_json_sha256": (wind_validation.get("wind_json") or {}).get("sha256"),
        "dynamic_wind": wind_import,
    }


__all__ = [
    "ClusterAssemblyBuildError",
    "MANIFEST_KIND",
    "SCHEMA_VERSION",
    "ancestor_chain",
    "build_blender_assembly_inputs",
    "build_unreal_ingest_plan",
    "build_unreal_nanite_assembly",
    "content_build_decision",
    "file_fingerprint",
    "fit_trs_transform",
    "lowest_common_ancestor",
    "make_skeleton_snapshot",
    "normalize_role_identity",
    "scope_material_pipeline_to_codex_tests",
    "snapshot_blender_armature",
    "validate_binding_hierarchy",
    "validate_file_fingerprint",
    "validate_manifest_artifacts",
    "validate_unreal_asset_contract",
    "validate_wind_json_against_skeleton",
]
