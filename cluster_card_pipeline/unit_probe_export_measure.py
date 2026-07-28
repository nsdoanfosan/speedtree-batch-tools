"""Run the real SpeedTree CLI and measure neutral Frond/Leaf Mesh components."""

from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent.parent
if str(REPO_DIR) not in sys.path:
    sys.path.insert(0, str(REPO_DIR))

from speedtree_export_options_contract import require_texture_skip_writing


class UnitProbeExportError(RuntimeError):
    pass


def _sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _fingerprint(path):
    candidate = Path(path).expanduser().resolve()
    if not candidate.is_file():
        raise UnitProbeExportError(f"Unit-probe evidence is missing: {candidate}")
    stat = candidate.stat()
    return {
        "path": str(candidate),
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "sha256": _sha256(candidate),
    }


def _export(speedtree_exe, spm, options, output, timeout):
    output = Path(output).expanduser().resolve()
    require_texture_skip_writing(
        options,
        purpose=f"{Path(spm).name} unit-probe XML export",
    )
    if output.exists():
        output.unlink()
    command = [
        str(Path(speedtree_exe).expanduser().resolve()),
        str(Path(spm).expanduser().resolve()),
        "-export_options",
        str(Path(options).expanduser().resolve()),
        "-export",
        str(output),
    ]
    completed = subprocess.run(
        command,
        cwd=str(Path(spm).expanduser().resolve().parent),
        capture_output=True,
        text=True,
        timeout=timeout,
        creationflags=0x08000000,
    )
    if completed.returncode or not output.is_file():
        detail = (completed.stderr or completed.stdout or "").strip()[-1500:]
        raise UnitProbeExportError(
            f"SpeedTree export failed ({completed.returncode}): {output}: {detail}"
        )
    return {
        "command": command,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
        "artifact": _fingerprint(output),
    }


def _numbers(text, cast=float):
    values = str(text or "").split()
    if cast is float:
        values = [value.replace(",", ".") for value in values]
    return [cast(value) for value in values]


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
        union(triangle[0], triangle[1])
        union(triangle[1], triangle[2])
    groups = {}
    for triangle in triangles:
        groups.setdefault(find(triangle[0]), set()).update(triangle)
    return list(groups.values())


def _material_components(xml_path, material_name, centimeters_per_unit=1.0):
    root = ET.parse(xml_path).getroot()
    export_name = material_name + "_Mat"
    matches = [
        node
        for node in root.findall("./Materials/Material")
        if node.get("Name") == export_name
    ]
    if len(matches) != 1:
        raise UnitProbeExportError(
            f"SpeedTree XML material {export_name!r} resolved {len(matches)} times"
        )
    material_id = str(matches[0].get("ID"))
    rows = []
    for obj in root.findall(".//Object"):
        points = list(
            zip(
                _numbers(obj.findtext("Points/X")),
                _numbers(obj.findtext("Points/Y")),
                _numbers(obj.findtext("Points/Z")),
            )
        )
        triangles = []
        for node in obj.findall("Triangles"):
            if str(node.get("Material")) != material_id:
                continue
            indices = _numbers(node.findtext("PointIndices"), int)
            expected = int(node.get("Count") or 0) * 3
            if len(indices) != expected:
                raise UnitProbeExportError(
                    "SpeedTree XML triangle point payload is incomplete"
                )
            triangles.extend(
                tuple(indices[index : index + 3])
                for index in range(0, len(indices), 3)
            )
        for component in _components(triangles):
            component_points = [points[index] for index in sorted(component)]
            minimum = [
                min(point[axis] for point in component_points)
                for axis in range(3)
            ]
            maximum = [
                max(point[axis] for point in component_points)
                for axis in range(3)
            ]
            size_cm = [
                (maximum[axis] - minimum[axis]) * centimeters_per_unit
                for axis in range(3)
            ]
            rows.append(
                {
                    "object": str(obj.get("Name") or ""),
                    "point_count": len(component),
                    "triangle_count": sum(
                        all(index in component for index in triangle)
                        for triangle in triangles
                    ),
                    "size_centimeters": size_cm,
                    "max_extent_centimeters": max(size_cm),
                    "max_extent_meters": max(size_cm) * 0.01,
                }
            )
    if not rows:
        raise UnitProbeExportError(
            f"SpeedTree XML contains no components for {material_name}"
        )
    extents = sorted(row["max_extent_meters"] for row in rows)
    return {
        "material_name": material_name,
        "speedtree_export_material": export_name,
        "component_count": len(rows),
        "components": rows,
        "extent_meters": {
            "minimum": extents[0],
            "median": statistics.median(extents),
            "maximum": extents[-1],
            "values": extents,
        },
    }


def _centimeter_option_evidence(path):
    text = Path(path).read_text(encoding="utf-8")
    values = {}
    for line in text.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    if (
        values.get("TransformConvertUnit") != "Centimeter"
        or float(values.get("TransformScale", "nan")) != 1.0
    ):
        raise UnitProbeExportError(
            "Unit probe requires TransformConvertUnit=Centimeter and TransformScale=1"
        )
    return {
        "TransformConvertUnit": values["TransformConvertUnit"],
        "TransformScale": float(values["TransformScale"]),
        "meters_per_export_unit": 0.01,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidate-name", required=True)
    parser.add_argument("--spm", required=True)
    parser.add_argument("--speedtree-exe", required=True)
    parser.add_argument("--fbx-options", required=True)
    parser.add_argument("--xml-options", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--mesh-geometry-scale", required=True, type=float)
    parser.add_argument("--mesh-asset-scale", required=True, type=float)
    parser.add_argument("--generator-scale", default=1.0, type=float)
    parser.add_argument("--frond-material", default="M_branch_elm_01")
    parser.add_argument("--leaf-material", default="M_leaf_elm_01")
    parser.add_argument("--report", required=True)
    parser.add_argument("--timeout", type=int, default=300)
    args = parser.parse_args()

    spm = Path(args.spm).expanduser().resolve()
    output_dir = Path(args.output_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    xml_path = output_dir / f"{spm.stem}_{args.candidate_name}.xml"
    fbx_path = output_dir / f"{spm.stem}_{args.candidate_name}.fbx"
    unit_evidence = _centimeter_option_evidence(args.xml_options)
    xml_export = _export(
        args.speedtree_exe,
        spm,
        args.xml_options,
        xml_path,
        args.timeout,
    )
    fbx_export = _export(
        args.speedtree_exe,
        spm,
        args.fbx_options,
        fbx_path,
        args.timeout,
    )
    frond = _material_components(xml_path, args.frond_material)
    leaf = _material_components(xml_path, args.leaf_material)
    payload = {
        "kind": "speedtree_fbx_spm_unit_probe_candidate_measurement",
        "version": 1,
        "status": "measured",
        "candidate_name": args.candidate_name,
        "spm": _fingerprint(spm),
        "speedtree_exe": _fingerprint(args.speedtree_exe),
        "fbx_options": _fingerprint(args.fbx_options),
        "xml_options": _fingerprint(args.xml_options),
        "export_unit_contract": unit_evidence,
        "mesh_geometry_scale": args.mesh_geometry_scale,
        "mesh_asset_scale": args.mesh_asset_scale,
        "generator_scale": args.generator_scale,
        "xml_export": xml_export,
        "fbx_export": fbx_export,
        "generator_measurements": [
            {"generator_type": "Frond", **frond},
            {"generator_type": "Leaf Mesh", **leaf},
        ],
    }
    report = Path(args.report).expanduser().resolve()
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print("SPEEDTREE_UNIT_PROBE_MEASUREMENT=" + str(report))


if __name__ == "__main__":
    main()
