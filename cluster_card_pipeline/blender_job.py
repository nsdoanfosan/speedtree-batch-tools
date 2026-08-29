"""Blender 5.2 background job for normalized cluster-card outputs."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import struct
import sys
from pathlib import Path

import bpy


def _parse_args():
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--output-dir", required=True)
    return parser.parse_args(argv)


def _sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _uv_hash(values):
    digest = hashlib.sha256()
    for left, right in values:
        digest.update(struct.pack("<2f", float(left), float(right)))
    return digest.hexdigest()


def _multiset_hash(values, digits=7):
    digest = hashlib.sha256()
    for row in sorted(tuple(round(float(value), digits) for value in item) for item in values):
        digest.update(struct.pack("<" + "f" * len(row), *row))
    return digest.hexdigest()


def _triangle_position_uv_hash(triangles, digits=6):
    canonical = []
    for triangle in triangles:
        corners = []
        for position, uv in triangle:
            corners.append(tuple(
                round(float(value), digits)
                for value in (*position, *uv)
            ))
        canonical.append(tuple(sorted(corners)))
    digest = hashlib.sha256()
    for triangle in sorted(canonical):
        for corner in triangle:
            digest.update(struct.pack("<5f", *corner))
    return digest.hexdigest()


def _identity_error(obj):
    expected = ((1.0, 0.0, 0.0, 0.0), (0.0, 1.0, 0.0, 0.0),
                (0.0, 0.0, 1.0, 0.0), (0.0, 0.0, 0.0, 1.0))
    return max(abs(obj.matrix_world[row][column] - expected[row][column])
               for row in range(4) for column in range(4))


def _dot(left, right):
    return sum(a * b for a, b in zip(left, right))


def _cross(left, right):
    return (
        left[1] * right[2] - left[2] * right[1],
        left[2] * right[0] - left[0] * right[2],
        left[0] * right[1] - left[1] * right[0],
    )


def _sub(left, right):
    return tuple(a - b for a, b in zip(left, right))


def _normalized(value):
    length = math.sqrt(_dot(value, value))
    return tuple(item / length for item in value) if length else (0.0, 0.0, 0.0)


def _faces_with_camera_winding(vertices, faces, normal):
    dots = []
    for face in faces:
        a, b, c = (vertices[index] for index in face)
        dots.append(_dot(_normalized(_cross(_sub(b, a), _sub(c, a))), normal))
    if not dots or max(abs(value) for value in dots) < 0.999:
        raise RuntimeError("Plane triangles are degenerate or not camera-normal aligned")
    if not all(value > 0.999 for value in dots):
        raise RuntimeError(
            "Source winding does not face the capture-camera normal; topology mutation is forbidden"
        )
    return faces, False


def _material(manifest):
    name = manifest["material"]["name"]
    material = bpy.data.materials.new(name=name)
    material.use_nodes = True
    color = manifest["material"]["maps"].get("Color") or {}
    path = Path(color.get("path") or "")
    if path.is_file():
        image = bpy.data.images.load(str(path), check_existing=True)
        nodes = material.node_tree.nodes
        texture = nodes.new("ShaderNodeTexImage")
        texture.image = image
        principled = next((node for node in nodes if node.type == "BSDF_PRINCIPLED"), None)
        if principled:
            material.node_tree.links.new(texture.outputs["Color"], principled.inputs["Base Color"])
    return material


def _write_mtl(path, manifest):
    material_name = manifest["material"]["name"]
    lines = [f"newmtl {material_name}", "Kd 1 1 1", "d 1"]
    color = manifest["material"]["maps"].get("Color") or {}
    opacity = manifest["material"]["maps"].get("Opacity") or {}
    if color.get("path"):
        lines.append("map_Kd " + str(color["path"]).replace("\\", "/"))
    if opacity.get("path"):
        lines.append("map_d " + str(opacity["path"]).replace("\\", "/"))
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_obj(path, plane, faces, normal, material_name, mtl_name):
    lines = [f"mtllib {mtl_name}", f"o {plane['name']}", f"usemtl {material_name}"]
    for vertex in plane["vertices"]:
        lines.append("v {:.9g} {:.9g} {:.9g}".format(*vertex))
    for uv in plane["uvs"]:
        lines.append("vt {:.9g} {:.9g}".format(*uv))
    lines.append("vn {:.9g} {:.9g} {:.9g}".format(*normal))
    for face in faces:
        lines.append("f " + " ".join(f"{index + 1}/{index + 1}/1" for index in face))
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")


def _build_object(plane, material, camera_normal):
    vertices = [tuple(value) for value in plane["vertices"]]
    faces, winding_flipped = _faces_with_camera_winding(vertices, plane["faces"], camera_normal)
    mesh = bpy.data.meshes.new(plane["name"])
    mesh.from_pydata(vertices, [], faces)
    mesh.update(calc_edges=True)
    uv_layer = mesh.uv_layers.new(name="UVMap")
    source_uvs = plane["uvs"]
    for polygon in mesh.polygons:
        for loop_index in polygon.loop_indices:
            vertex_index = mesh.loops[loop_index].vertex_index
            uv_layer.data[loop_index].uv = source_uvs[vertex_index]
    mesh.materials.append(material)
    obj = bpy.data.objects.new(plane["name"], mesh)
    bpy.context.scene.collection.objects.link(obj)
    obj.location = (0.0, 0.0, 0.0)
    obj.rotation_mode = "XYZ"
    obj.rotation_euler = (0.0, 0.0, 0.0)
    obj.scale = (1.0, 1.0, 1.0)
    obj["speedtree_cutout_mesh_id"] = int(plane["source_mesh_id"])
    obj["speedtree_cutout_source_name"] = plane["source_mesh_name"]
    obj["speedtree_capture_camera"] = "Dropped XY plane camera 2"
    obj["speedtree_attachment_pivot_uv"] = plane["attachment"]["pivot_uv"]
    obj["speedtree_uv_sha256"] = plane["uv_sha256"]
    obj["speedtree_topology_sha256"] = plane["topology_sha256"]
    obj["speedtree_straighten_used"] = False
    return obj, faces, winding_flipped


def _select_only(obj):
    bpy.ops.object.select_all(action="DESELECT")
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj


def _fbx_roundtrip(path, plane, camera_normal):
    before = set(bpy.data.objects)
    bpy.ops.import_scene.fbx(filepath=str(path), use_anim=False)
    imported = [obj for obj in bpy.data.objects if obj not in before and obj.type == "MESH"]
    if len(imported) != 1:
        raise RuntimeError(f"FBX round-trip expected one mesh, found {len(imported)}: {path}")
    obj = imported[0]
    bpy.context.view_layer.update()
    actual_positions = [tuple(obj.matrix_world @ vertex.co) for vertex in obj.data.vertices]
    expected_positions = [tuple(value) for value in plane["vertices"]]
    unused_expected = set(range(len(expected_positions)))
    vertex_mapping = {}
    position_errors = []
    for actual_index, actual in enumerate(actual_positions):
        candidates = [
            (
                math.sqrt(sum(
                    (actual[axis] - expected_positions[expected_index][axis]) ** 2
                    for axis in range(3)
                )),
                expected_index,
            )
            for expected_index in unused_expected
        ]
        if not candidates:
            break
        error, expected_index = min(candidates)
        if error > 1.0e-6:
            break
        vertex_mapping[actual_index] = expected_index
        unused_expected.remove(expected_index)
        position_errors.append(error)
    position_mapping_complete = (
        len(vertex_mapping) == len(actual_positions) == len(expected_positions)
        and not unused_expected
    )
    max_position_error = max(position_errors, default=float("inf"))
    actual_uvs = []
    uv_layer = obj.data.uv_layers.active
    if uv_layer:
        actual_uvs = [tuple(item.uv) for item in uv_layer.data]
    obj.data.calc_loop_triangles()
    expected_uvs = [
        tuple(plane["uvs"][vertex_index])
        for face in plane["faces"] for vertex_index in face
    ]
    actual_triangle_payload = []
    mapped_topology = []
    mapped_uv_errors = []
    for triangle in obj.data.loop_triangles:
        corners = []
        mapped_face = []
        for loop_index in triangle.loops:
            vertex_index = obj.data.loops[loop_index].vertex_index
            position = tuple(obj.matrix_world @ obj.data.vertices[vertex_index].co)
            uv = tuple(uv_layer.data[loop_index].uv) if uv_layer else (0.0, 0.0)
            corners.append((position, uv))
            expected_index = vertex_mapping.get(vertex_index)
            if expected_index is not None:
                mapped_face.append(expected_index)
                expected_uv = plane["uvs"][expected_index]
                mapped_uv_errors.append(math.hypot(
                    uv[0] - expected_uv[0], uv[1] - expected_uv[1]
                ))
        actual_triangle_payload.append(corners)
        if len(mapped_face) == 3:
            mapped_topology.append(tuple(sorted(mapped_face)))
    expected_triangle_payload = [
        [
            (tuple(plane["vertices"][vertex_index]), tuple(plane["uvs"][vertex_index]))
            for vertex_index in face
        ]
        for face in plane["faces"]
    ]
    actual_triangle_hash = _triangle_position_uv_hash(actual_triangle_payload)
    expected_triangle_hash = _triangle_position_uv_hash(expected_triangle_payload)
    expected_topology = sorted(tuple(sorted(face)) for face in plane["faces"])
    topology_preserved = position_mapping_complete and sorted(mapped_topology) == expected_topology
    max_mapped_uv_error = max(mapped_uv_errors, default=float("inf"))
    vertex_uv_face_mapping_preserved = (
        topology_preserved and max_mapped_uv_error <= 1.0e-6
    )
    normal_dots = []
    for triangle in obj.data.loop_triangles:
        world_normal = obj.matrix_world.to_3x3() @ triangle.normal
        normal_dots.append(_dot(_normalized(world_normal), camera_normal))
    result = {
        "mesh_object_count": 1,
        "vertex_count": len(actual_positions),
        "triangle_count": len(obj.data.loop_triangles),
        "world_position_multiset_sha256": _multiset_hash(actual_positions),
        "expected_world_position_multiset_sha256": _multiset_hash(expected_positions),
        "max_world_position_error": max_position_error,
        "position_mapping_complete": position_mapping_complete,
        "world_positions_preserved": position_mapping_complete and max_position_error <= 1.0e-6,
        "uv_corner_multiset_sha256": _multiset_hash(actual_uvs),
        "expected_uv_corner_multiset_sha256": _multiset_hash(expected_uvs),
        "uv_corners_preserved": _multiset_hash(actual_uvs) == _multiset_hash(expected_uvs),
        "triangle_position_uv_sha256": actual_triangle_hash,
        "expected_triangle_position_uv_sha256": expected_triangle_hash,
        "mapped_topology_preserved": topology_preserved,
        "max_mapped_uv_error": max_mapped_uv_error,
        "vertex_uv_face_mapping_preserved": vertex_uv_face_mapping_preserved,
        "triangle_position_uv_topology_preserved": vertex_uv_face_mapping_preserved,
        "normal_min_dot_camera_normal": min(normal_dots) if normal_dots else -1.0,
        "object_transform_identity_error": _identity_error(obj),
    }
    for imported_obj in [obj for obj in bpy.data.objects if obj not in before]:
        bpy.data.objects.remove(imported_obj, do_unlink=True)
    return result


def main():
    args = _parse_args()
    manifest_path = Path(args.manifest).resolve()
    output_dir = Path(args.output_dir).resolve()
    mesh_dir = output_dir / "meshes"
    mesh_dir.mkdir(parents=True, exist_ok=True)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("kind") != "speedtree_cluster_card_camera_projection":
        raise RuntimeError("Unexpected cluster-card manifest kind")
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete(use_global=False)
    material = _material(manifest)
    normal = tuple(manifest["camera"]["plane_normal"])
    mtl_path = mesh_dir / f"{manifest['material']['name']}.mtl"
    _write_mtl(mtl_path, manifest)
    objects = []
    output_planes = []
    for plane in manifest["planes"]:
        obj, faces, winding_flipped = _build_object(plane, material, normal)
        objects.append(obj)
        obj_path = mesh_dir / f"{plane['name']}.obj"
        fbx_path = mesh_dir / f"{plane['name']}.fbx"
        _write_obj(obj_path, plane, faces, normal, material.name, mtl_path.name)
        _select_only(obj)
        bpy.ops.export_scene.fbx(
            filepath=str(fbx_path),
            use_selection=True,
            object_types={"MESH"},
            apply_unit_scale=False,
            bake_space_transform=False,
            axis_forward="-Z",
            axis_up="Y",
            add_leaf_bones=False,
            bake_anim=False,
            path_mode="AUTO",
        )
        roundtrip = _fbx_roundtrip(fbx_path, plane, normal)
        actual_vertex_uvs = [tuple(uv_layer.uv) for uv_layer in obj.data.uv_layers["UVMap"].data]
        expected_corner_uvs = [
            tuple(plane["uvs"][obj.data.loops[loop_index].vertex_index])
            for polygon in obj.data.polygons for loop_index in polygon.loop_indices
        ]
        max_plane_distance = max(abs(_dot(vertex.co, normal)) for vertex in obj.data.vertices)
        output_planes.append({
            "name": obj.name,
            "source_mesh_id": plane["source_mesh_id"],
            "vertex_count": len(obj.data.vertices),
            "triangle_count": len(obj.data.polygons),
            "transform_identity_error": _identity_error(obj),
            "max_plane_distance": max_plane_distance,
            "uv_corner_sha256": _uv_hash(actual_vertex_uvs),
            "expected_uv_corner_sha256": _uv_hash(expected_corner_uvs),
            "uv_corner_preserved": _uv_hash(actual_vertex_uvs) == _uv_hash(expected_corner_uvs),
            "winding_flipped_to_camera_normal": winding_flipped,
            "fbx_roundtrip": roundtrip,
            "fbx": {"path": str(fbx_path), "sha256": _sha256(fbx_path), "size": fbx_path.stat().st_size},
            "obj": {"path": str(obj_path), "sha256": _sha256(obj_path), "size": obj_path.stat().st_size},
        })
    bpy.ops.object.select_all(action="DESELECT")
    blend_path = output_dir / manifest["output"]["blend_name"]
    bpy.ops.wm.save_as_mainfile(filepath=str(blend_path), check_existing=False)
    report = {
        "kind": "speedtree_cluster_card_blender_validation",
        "version": 1,
        "status": "ready",
        "manifest": str(manifest_path),
        "blend": {"path": str(blend_path), "sha256": _sha256(blend_path), "size": blend_path.stat().st_size},
        "camera_basis": {
            "right": manifest["camera"]["right"],
            "up": manifest["camera"]["up"],
            "normal": manifest["camera"]["plane_normal"],
        },
        "planes": output_planes,
        "all_transform_identity": all(item["transform_identity_error"] <= 1.0e-7 for item in output_planes),
        "all_planar": all(item["max_plane_distance"] <= 1.0e-6 for item in output_planes),
        "all_uv_preserved": all(item["uv_corner_preserved"] for item in output_planes),
        "all_fbx_roundtrip_preserved": all(
            item["fbx_roundtrip"]["world_positions_preserved"]
            and item["fbx_roundtrip"]["uv_corners_preserved"]
            and item["fbx_roundtrip"]["triangle_position_uv_topology_preserved"]
            and item["fbx_roundtrip"]["normal_min_dot_camera_normal"] >= 0.999
            and item["fbx_roundtrip"]["object_transform_identity_error"] <= 1.0e-6
            for item in output_planes
        ),
    }
    if not (
        report["all_transform_identity"]
        and report["all_planar"]
        and report["all_uv_preserved"]
        and report["all_fbx_roundtrip_preserved"]
    ):
        report["status"] = "blocked"
    output_prefix = str(
        manifest.get("output", {}).get("object_prefix")
        or Path(manifest["output"]["blend_name"]).stem
    )
    report_path = output_dir / f"{output_prefix}_blender_validation.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    if report["status"] != "ready":
        raise RuntimeError(f"Blender validation failed: {report_path}")


if __name__ == "__main__":
    main()
