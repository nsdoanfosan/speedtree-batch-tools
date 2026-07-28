"""SpeedTree 10.1 CLI verification for isolated card handoff copies."""

from __future__ import annotations

import argparse
import hashlib
import math
import struct
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

from speedtree_export_options_contract import require_texture_skip_writing
from .contract import (
    ContractError,
    _fingerprint,
    _read_spm_root,
    _write_spm_root,
    write_json,
)


def _run_export(executable, spm, options, output, timeout):
    output = Path(output)
    require_texture_skip_writing(
        options,
        purpose=f"{Path(spm).name} Cluster card verification export",
    )
    if output.exists():
        output.unlink()
    command = [str(executable), str(spm), "-export_options", str(options), "-export", str(output)]
    try:
        completed = subprocess.run(
            command,
            cwd=str(Path(spm).parent),
            capture_output=True,
            text=True,
            timeout=timeout,
            creationflags=0x08000000,
        )
    except subprocess.TimeoutExpired as exc:
        raise ContractError(f"SpeedTree export timed out: {output.name}") from exc
    if completed.returncode or not output.is_file():
        detail = (completed.stderr or completed.stdout or "").strip()[-1000:]
        raise ContractError(
            f"SpeedTree export failed ({completed.returncode}): {output.name}: {detail}"
        )
    return {
        "command": command,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
        "artifact": _fingerprint(output),
    }


def _number_list(text, cast=float):
    values = str(text or "").split()
    if cast is float:
        values = [value.replace(",", ".") for value in values]
    return [cast(value) for value in values]


def _triangle_uv_topology_hash(triangles, digits=6):
    canonical = []
    for triangle in triangles:
        canonical.append(tuple(sorted(
            tuple(round(float(value), digits) for value in uv)
            for uv in triangle
        )))
    digest = hashlib.sha256()
    for triangle in sorted(canonical):
        for uv in triangle:
            digest.update(struct.pack("<2f", *uv))
    return digest.hexdigest()


def _components(triangles):
    parent = {}

    def find(value):
        parent.setdefault(value, value)
        while parent[value] != value:
            parent[value] = parent[parent[value]]
            value = parent[value]
        return value

    def union(left, right):
        left = find(left)
        right = find(right)
        if left != right:
            parent[right] = left

    for triangle in triangles:
        points = triangle["points"]
        union(points[0], points[1])
        union(points[1], points[2])
    grouped = {}
    for triangle in triangles:
        grouped.setdefault(find(triangle["points"][0]), []).append(triangle)
    return list(grouped.values())


def _component_validation(component, active_prototype, point_positions):
    actual_point_ids = sorted({value for triangle in component for value in triangle["points"]})
    expected_uvs = [tuple(value) for value in active_prototype["uvs"]]
    unused_expected = set(range(len(expected_uvs)))
    mapping = {}
    uv_errors = []
    actual_uv_by_point = {}
    for triangle in component:
        for point_id, uv in zip(triangle["points"], triangle["uvs"]):
            previous = actual_uv_by_point.setdefault(point_id, tuple(uv))
            if math.hypot(previous[0] - uv[0], previous[1] - uv[1]) > 1.0e-9:
                raise ContractError("SpeedTree XML point has inconsistent loop UV values")
    for point_id in actual_point_ids:
        uv = actual_uv_by_point[point_id]
        candidates = [
            (math.hypot(uv[0] - expected_uvs[index][0], uv[1] - expected_uvs[index][1]), index)
            for index in unused_expected
        ]
        if not candidates:
            break
        error, expected_index = min(candidates)
        if error > 1.0e-6:
            break
        mapping[point_id] = expected_index
        unused_expected.remove(expected_index)
        uv_errors.append(error)
    uv_mapping_complete = (
        len(mapping) == len(actual_point_ids) == len(expected_uvs)
        and not unused_expected
    )
    mapped_topology = sorted(
        tuple(sorted(mapping.get(value, -1) for value in triangle["points"]))
        for triangle in component
    )
    expected_topology = sorted(tuple(sorted(face)) for face in active_prototype["faces"])
    topology_preserved = uv_mapping_complete and mapped_topology == expected_topology

    component_point_ids = actual_point_ids
    if not component_point_ids or min(component_point_ids) < 0 or max(component_point_ids) >= len(point_positions):
        raise ContractError("SpeedTree XML material point index is out of range")
    points = [point_positions[index] for index in component_point_ids]
    plane_normal = None
    origin = points[0]
    for first_index in range(1, len(points)):
        first = tuple(points[first_index][axis] - origin[axis] for axis in range(3))
        for second_index in range(first_index + 1, len(points)):
            second = tuple(points[second_index][axis] - origin[axis] for axis in range(3))
            cross = (
                first[1] * second[2] - first[2] * second[1],
                first[2] * second[0] - first[0] * second[2],
                first[0] * second[1] - first[1] * second[0],
            )
            length = math.sqrt(sum(value * value for value in cross))
            if length > 1.0e-10:
                plane_normal = tuple(value / length for value in cross)
                break
        if plane_normal is not None:
            break
    if plane_normal is None:
        raise ContractError("SpeedTree XML component is degenerate")
    distances = [
        abs(sum((point[axis] - origin[axis]) * plane_normal[axis] for axis in range(3)))
        for point in points
    ]
    extents = [max(point[axis] for point in points) - min(point[axis] for point in points) for axis in range(3)]
    scale = max(extents)
    relative_planarity_error = max(distances) / scale if scale > 1.0e-12 else float("inf")
    return {
        "uv_mapping_complete": uv_mapping_complete,
        "max_uv_mapping_error": max(uv_errors, default=float("inf")),
        "mapped_topology_preserved": topology_preserved,
        "actual_point_count": len(actual_point_ids),
        "relative_planarity_error": relative_planarity_error,
    }


def _material_geometry(xml_path, material_name, active_prototype):
    root = ET.parse(xml_path).getroot()
    expected_export_name = material_name + "_Mat"
    materials = [
        node for node in root.findall("./Materials/Material")
        if node.get("Name") == expected_export_name
    ]
    if len(materials) != 1:
        raise ContractError(
            f"SpeedTree XML material '{expected_export_name}' resolved {len(materials)} times"
        )
    material_id = materials[0].get("ID")
    prototype_triangle_count = int(active_prototype["triangle_count"])
    prototype_vertex_count = int(active_prototype["vertex_count"])
    expected_uv_topology = _triangle_uv_topology_hash([
        [active_prototype["uvs"][vertex_index] for vertex_index in face]
        for face in active_prototype["faces"]
    ])
    triangle_count = 0
    object_names = []
    component_rows = []
    material_points = []
    for obj in root.findall(".//Object"):
        u_values = _number_list(obj.findtext("Vertices/TexcoordU"))
        v_values = _number_list(obj.findtext("Vertices/TexcoordV"))
        x_values = _number_list(obj.findtext("Points/X"))
        y_values = _number_list(obj.findtext("Points/Y"))
        z_values = _number_list(obj.findtext("Points/Z"))
        point_positions = list(zip(x_values, y_values, z_values))
        object_triangles = []
        for node in obj.findall("Triangles"):
            if node.get("Material") != material_id:
                continue
            point_indices = _number_list(node.findtext("PointIndices"), int)
            vertex_indices = _number_list(node.findtext("VertexIndices"), int)
            expected_count = int(node.get("Count") or "0")
            if len(point_indices) != expected_count * 3 or len(vertex_indices) != expected_count * 3:
                raise ContractError("SpeedTree XML triangle index payload is incomplete")
            for index in range(0, len(point_indices), 3):
                points = point_indices[index:index + 3]
                vertices = vertex_indices[index:index + 3]
                if max(vertices) >= len(u_values) or max(vertices) >= len(v_values):
                    raise ContractError("SpeedTree XML material vertex index is out of range")
                object_triangles.append({
                    "points": points,
                    "vertices": vertices,
                    "uvs": [(u_values[value], v_values[value]) for value in vertices],
                })
        if not object_triangles:
            continue
        referenced_point_ids = {
            point_index
            for triangle in object_triangles
            for point_index in triangle["points"]
        }
        material_points.extend(
            point_positions[point_index]
            for point_index in sorted(referenced_point_ids)
            if 0 <= point_index < len(point_positions)
        )
        triangle_count += len(object_triangles)
        object_names.append(str(obj.get("Name") or ""))
        for component in _components(object_triangles):
            unique_points = {value for triangle in component for value in triangle["points"]}
            uv_topology = _triangle_uv_topology_hash([triangle["uvs"] for triangle in component])
            validation = _component_validation(component, active_prototype, point_positions)
            component_points = [
                point_positions[index]
                for index in sorted(unique_points)
            ]
            component_min = [
                min(point[axis] for point in component_points)
                for axis in range(3)
            ]
            component_max = [
                max(point[axis] for point in component_points)
                for axis in range(3)
            ]
            component_size = [
                component_max[axis] - component_min[axis]
                for axis in range(3)
            ]
            component_rows.append({
                "triangle_count": len(component),
                "point_count": len(unique_points),
                "uv_topology_sha256": uv_topology,
                "bounds": {
                    "minimum": component_min,
                    "maximum": component_max,
                    "size": component_size,
                    "max_extent": max(component_size),
                },
                **validation,
                "matches_active_prototype": (
                    len(component) == prototype_triangle_count
                    and len(unique_points) == prototype_vertex_count
                    and validation["uv_mapping_complete"]
                    and validation["mapped_topology_preserved"]
                ),
            })
    if triangle_count <= 0:
        raise ContractError(f"SpeedTree XML contains no geometry for material {material_name}")
    if prototype_triangle_count <= 0 or triangle_count % prototype_triangle_count:
        raise ContractError(
            f"Material triangle count {triangle_count} is not a multiple of prototype {prototype_triangle_count}"
        )
    if not component_rows or not all(row["matches_active_prototype"] for row in component_rows):
        failed = [row for row in component_rows if not row["matches_active_prototype"]]
        failure_summary = {
            "component_count": len(component_rows),
            "failed_count": len(failed),
            "triangle_counts": sorted({row["triangle_count"] for row in failed}),
            "point_counts": sorted({row["point_count"] for row in failed}),
            "uv_mapping_failures": sum(not row["uv_mapping_complete"] for row in failed),
            "topology_failures": sum(not row["mapped_topology_preserved"] for row in failed),
            "max_uv_mapping_error": max(
                (row["max_uv_mapping_error"] for row in failed), default=float("inf")
            ),
            "max_relative_planarity_error": max(
                (row["relative_planarity_error"] for row in failed), default=float("inf")
            ),
        }
        raise ContractError(
            "SpeedTree XML material components do not preserve the active prototype "
            f"UV/topology: {failure_summary}"
        )
    if not material_points:
        raise ContractError(
            f"SpeedTree XML contains no referenced points for material {material_name}"
        )
    bounds_min = [
        min(point[axis] for point in material_points)
        for axis in range(3)
    ]
    bounds_max = [
        max(point[axis] for point in material_points)
        for axis in range(3)
    ]
    bounds_size = [
        bounds_max[axis] - bounds_min[axis]
        for axis in range(3)
    ]
    return {
        "material_id": int(material_id),
        "material_name": expected_export_name,
        "triangle_count": triangle_count,
        "active_prototype_triangle_count": prototype_triangle_count,
        "inferred_instance_count": triangle_count // prototype_triangle_count,
        "component_count": len(component_rows),
        "bounds": {
            "minimum": bounds_min,
            "maximum": bounds_max,
            "size": bounds_size,
            "max_extent": max(bounds_size),
            "point_count": len(material_points),
        },
        "expected_uv_topology_sha256": expected_uv_topology,
        "all_component_uv_topology_matches": True,
        "component_summary": {
            "triangle_counts": sorted({row["triangle_count"] for row in component_rows}),
            "point_counts": sorted({row["point_count"] for row in component_rows}),
            "uv_topology_hashes": sorted({row["uv_topology_sha256"] for row in component_rows}),
            "component_max_extents": sorted(
                row["bounds"]["max_extent"]
                for row in component_rows
            ),
            "max_uv_mapping_error": max(row["max_uv_mapping_error"] for row in component_rows),
            "max_relative_planarity_error": max(
                row["relative_planarity_error"] for row in component_rows
            ),
            "planarity_note": (
                "Diagnostic only: the tree Frond generator may deform placed instances; "
                "normalized prototype planarity is gated by the Blender/FBX report."
            ),
            "all_mapped_topology_preserved": all(
                row["mapped_topology_preserved"] for row in component_rows
            ),
        },
        "object_names": object_names,
    }


def _write_mesh_variant(source_spm, target_spm, contract, mesh_id):
    root = _read_spm_root(source_spm)
    generators = {
        str(node.findtext("GUID") or ""): node
        for node in root.findall(".//Generator")
    }
    changed = []
    for binding in contract.get("tree_generator_bindings") or []:
        generator = generators.get(str(binding.get("generator_guid") or ""))
        if generator is None:
            raise ContractError(
                f"SpeedTree variant lost Generator GUID {binding.get('generator_guid')}"
            )
        properties = {
            str(item.findtext("Name") or ""): item
            for item in generator.findall("./Properties/Property")
        }
        material_property = properties.get(str(binding.get("material_property") or ""))
        mesh_property = properties.get(str(binding.get("mesh_property") or ""))
        if material_property is None or mesh_property is None:
            raise ContractError("SpeedTree variant lost exact Generator Material/Mesh properties")
        if str(material_property.findtext("Value") or "") != str(binding.get("material") or ""):
            raise ContractError("SpeedTree variant Generator material binding drifted")
        value_node = mesh_property.find("Value")
        if value_node is None:
            value_node = ET.SubElement(mesh_property, "Value")
        value_node.text = str(int(mesh_id))
        changed.append({
            "generator_guid": binding["generator_guid"],
            "slot": binding["slot"],
            "material": binding["material"],
            "source_mesh": binding["mesh"],
            "verification_mesh": int(mesh_id),
        })
    if not changed:
        raise ContractError("SpeedTree variant has no explicit Generator binding to patch")
    _write_spm_root(target_spm, root)
    return changed


def verify_speedtree_handoff(
    candidate_spm,
    output_dir,
    *,
    speedtree_exe,
    fbx_options,
    xml_options,
    contract,
    timeout=180,
):
    candidate_spm = Path(candidate_spm).resolve()
    output_dir = Path(output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    material_name = contract["material"]["name"]
    variants = []
    for plane in contract["planes"]:
        mesh_id = int(plane["source_mesh_id"])
        stem = f"{candidate_spm.stem}_mesh_{mesh_id}_candidate"
        variant_spm = output_dir / f"{stem}.spm"
        bindings = _write_mesh_variant(candidate_spm, variant_spm, contract, mesh_id)
        fbx_path = output_dir / f"{stem}.fbx"
        xml_path = output_dir / f"{stem}.xml"
        fbx = _run_export(speedtree_exe, variant_spm, fbx_options, fbx_path, timeout)
        xml = _run_export(speedtree_exe, variant_spm, xml_options, xml_path, timeout)
        if b"Vertices" not in fbx_path.read_bytes():
            raise ContractError(
                f"SpeedTree mesh {mesh_id} candidate FBX contains no geometry vertex payload"
            )
        geometry = _material_geometry(xml_path, material_name, plane)
        variants.append({
            "source_mesh_id": mesh_id,
            "normalized_name": plane["name"],
            "variant_spm": _fingerprint(variant_spm),
            "generator_verification_bindings": bindings,
            "fbx_export": fbx,
            "xml_export": xml,
            "active_material_geometry": geometry,
        })
    return {
        "kind": "speedtree_cluster_card_modeler_validation",
        "version": 1,
        "status": "ready",
        "candidate_spm": _fingerprint(candidate_spm),
        "material": material_name,
        "verified_mesh_ids": [row["source_mesh_id"] for row in variants],
        "variants": variants,
    }


def _arguments(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidate-spm", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--speedtree-exe", required=True)
    parser.add_argument("--fbx-options", required=True)
    parser.add_argument("--xml-options", required=True)
    parser.add_argument("--contract-json", required=True)
    parser.add_argument("--report", required=True)
    return parser.parse_args(argv)


def main(argv=None):
    args = _arguments(argv)
    report = verify_speedtree_handoff(
        args.candidate_spm,
        args.output_dir,
        speedtree_exe=args.speedtree_exe,
        fbx_options=args.fbx_options,
        xml_options=args.xml_options,
        contract=__import__("json").loads(Path(args.contract_json).read_text(encoding="utf-8")),
    )
    write_json(args.report, report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
