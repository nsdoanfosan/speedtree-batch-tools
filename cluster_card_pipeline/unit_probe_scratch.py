"""Create or neutralize a scratch SPM for the real 10 cm unit probe.

This module never targets the production SPM in place.  It changes only the
generator instances that reference the explicitly supplied probe materials,
and records their original values so the production user-authored values stay
outside the probe.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from pathlib import Path

from .contract import _read_spm_root, _write_spm_root


class UnitProbeScratchError(RuntimeError):
    pass


def _sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _nearest_generator(node, parents):
    current = node
    while current is not None and current.tag != "Generator":
        current = parents.get(current)
    return current


def _owned_properties(generator, root, parents):
    for node in root.iter():
        if node.tag not in {"Property", "SplineProperty"}:
            continue
        if _nearest_generator(node, parents) is generator:
            yield node


def neutralize_probe_generators(root, material_ids):
    material_ids = {int(value) for value in material_ids}
    parents = {child: parent for parent in root.iter() for child in parent}
    changed = []
    matched_types = set()
    for generator in root.iter("Generator"):
        properties = list(_owned_properties(generator, root, parents))
        values = {
            str(prop.findtext("Name") or ""): str(prop.findtext("Value") or "")
            for prop in properties
        }
        referenced = {
            int(value)
            for name, value in values.items()
            if (
                name.startswith("Material:Frond:")
                or name.startswith("Leaves:Type:")
            )
            and name.endswith(":Material")
            and str(value).strip().isdigit()
        }
        if not referenced.intersection(material_ids):
            continue
        if any(name.startswith("Material:Frond:") for name in values):
            generator_type = "Frond"
            targets = ("Shape:Scale:Width", "Shape:Scale:Height")
        elif any(name.startswith("Leaves:Type:") for name in values):
            generator_type = "Leaf Mesh"
            targets = ("Leaves:Size",)
        else:
            continue
        matched_types.add(generator_type)
        for prop in properties:
            name = str(prop.findtext("Name") or "")
            if name not in targets:
                continue
            value = prop.find("Value")
            if value is None:
                raise UnitProbeScratchError(
                    f"Scratch generator property has no Value: {name}"
                )
            before = float(value.text)
            value.text = "1"
            changed.append(
                {
                    "generator_name": str(generator.findtext("Name") or ""),
                    "generator_guid": str(generator.findtext("GUID") or ""),
                    "generator_type": generator_type,
                    "property": name,
                    "before": before,
                    "after": 1.0,
                }
            )
    missing = {"Frond", "Leaf Mesh"} - matched_types
    if missing:
        raise UnitProbeScratchError(
            "Scratch SPM does not expose both required probe generator types: "
            + ", ".join(sorted(missing))
        )
    return changed


def strip_probe_material_ownership(root, material_ids):
    """Make copied managed materials adoptable inside the isolated scratch only."""
    material_ids = {int(value) for value in material_ids}
    assets = root.find("Assets")
    if assets is None:
        raise UnitProbeScratchError("Scratch SPM has no Assets node")
    mesh_ids = set()
    stripped = []
    for material in assets.findall("Material_v8"):
        try:
            material_id = int(material.get("ID") or -1)
        except ValueError:
            continue
        if material_id not in material_ids:
            continue
        primary = material.findtext("CutoutMeshID")
        if str(primary or "").lstrip("-").isdigit() and int(primary) > 0:
            mesh_ids.add(int(primary))
        for node in material.findall("./SupplementalCutoutMeshIDs/CutoutMesh"):
            value = node.get("ID")
            if str(value or "").lstrip("-").isdigit() and int(value) > 0:
                mesh_ids.add(int(value))
        user_data = material.find("UserData")
        if user_data is not None and str(user_data.text or "").strip():
            stripped.append(
                {
                    "kind": "material",
                    "id": material_id,
                    "name": str(material.get("Name") or ""),
                    "user_data_sha256": hashlib.sha256(
                        str(user_data.text).encode("utf-8")
                    ).hexdigest(),
                }
            )
            user_data.text = ""
    for mesh in assets.findall("Mesh"):
        try:
            mesh_id = int(mesh.get("ID") or -1)
        except ValueError:
            continue
        if mesh_id not in mesh_ids:
            continue
        user_data = mesh.find("UserData")
        if user_data is not None and str(user_data.text or "").strip():
            stripped.append(
                {
                    "kind": "mesh",
                    "id": mesh_id,
                    "name": str(mesh.get("Name") or ""),
                    "user_data_sha256": hashlib.sha256(
                        str(user_data.text).encode("utf-8")
                    ).hexdigest(),
                }
            )
            user_data.text = ""
    return stripped


def build_scratch(source_spm, target_spm, material_ids):
    source = Path(source_spm).expanduser().resolve()
    target = Path(target_spm).expanduser().resolve()
    if not source.is_file():
        raise UnitProbeScratchError(f"Production SPM is missing: {source}")
    if source == target:
        raise UnitProbeScratchError(
            "Unit probe must never neutralize the production SPM in place"
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    root = _read_spm_root(target)
    stripped = strip_probe_material_ownership(root, material_ids)
    changed = neutralize_probe_generators(root, material_ids)
    _write_spm_root(target, root)
    return {
        "kind": "speedtree_unit_probe_scratch_spm",
        "version": 1,
        "status": "ready",
        "production_spm": {
            "path": str(source),
            "size": source.stat().st_size,
            "sha256": _sha256(source),
        },
        "scratch_spm": {
            "path": str(target),
            "size": target.stat().st_size,
            "sha256": _sha256(target),
        },
        "material_ids": sorted(int(value) for value in material_ids),
        "scratch_only_removed_atlas_ownership": stripped,
        "neutral_generator_changes": changed,
        "production_generator_values_modified": False,
    }


def neutralize_existing(target_spm, material_ids):
    target = Path(target_spm).expanduser().resolve()
    if not target.is_file():
        raise UnitProbeScratchError(f"Scratch SPM is missing: {target}")
    root = _read_spm_root(target)
    changed = neutralize_probe_generators(root, material_ids)
    _write_spm_root(target, root)
    return {
        "kind": "speedtree_unit_probe_scratch_neutralization",
        "version": 1,
        "status": "ready",
        "scratch_spm": {
            "path": str(target),
            "size": target.stat().st_size,
            "sha256": _sha256(target),
        },
        "material_ids": sorted(int(value) for value in material_ids),
        "neutral_generator_changes": changed,
        "production_generator_values_modified": False,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-spm")
    parser.add_argument("--target-spm", required=True)
    parser.add_argument("--material-id", action="append", required=True, type=int)
    parser.add_argument("--neutralize-existing", action="store_true")
    parser.add_argument("--report", required=True)
    args = parser.parse_args()
    if args.neutralize_existing:
        if args.source_spm:
            raise UnitProbeScratchError(
                "--source-spm is incompatible with --neutralize-existing"
            )
        result = neutralize_existing(args.target_spm, args.material_id)
    else:
        if not args.source_spm:
            raise UnitProbeScratchError("--source-spm is required when creating a scratch")
        result = build_scratch(args.source_spm, args.target_spm, args.material_id)
    report = Path(args.report).expanduser().resolve()
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print("SPEEDTREE_UNIT_PROBE_SCRATCH=" + str(report))


if __name__ == "__main__":
    main()
