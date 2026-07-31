"""Regenerate existing Atlas manifest FBXs from their corrected Blender source.

This intentionally updates only the mesh FBXs already declared by one active
Atlas scope manifest. It does not write SPMs or alter Cluster relationships.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import traceback
from pathlib import Path

import bpy


def _parse_args():
    argv = sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else []
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--report", required=True)
    return parser.parse_args(argv)


def _sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _write_report(path, payload):
    target = Path(path).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _same_path(first, second):
    return str(Path(first).resolve()).casefold() == str(
        Path(second).resolve()
    ).casefold()


def main():
    args = _parse_args()
    manifest_path = Path(args.manifest).expanduser().resolve()
    report_path = Path(args.report).expanduser().resolve()
    payload = {
        "status": "failed",
        "blend": str(Path(bpy.data.filepath).resolve()),
        "manifest": str(manifest_path),
        "exports": [],
        "errors": [],
    }
    exit_code = 1
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not _same_path(manifest.get("blend_file", ""), bpy.data.filepath):
            raise RuntimeError(
                "Atlas manifest belongs to a different Blender source: "
                f"{manifest.get('blend_file')} != {bpy.data.filepath}"
            )
        geometry_scale = float(manifest.get("mesh_geometry_scale") or 1.0)
        if abs(geometry_scale - 1.0) > 1.0e-12:
            raise RuntimeError(
                "Focused Atlas FBX regeneration requires mesh_geometry_scale=1"
            )
        rows = list(manifest.get("meshes") or [])
        if not rows:
            raise RuntimeError("Atlas manifest declares no mesh FBXs")

        backup_root = (
            report_path.parent
            / "atlas_fbx_typo_backup_20260729"
            / manifest_path.stem
        )
        backup_root.mkdir(parents=True, exist_ok=True)
        depsgraph = bpy.context.evaluated_depsgraph_get()
        temp_collection = bpy.data.collections.new(
            "Codex_Atlas_FBX_Typo_Regeneration"
        )
        bpy.context.scene.collection.children.link(temp_collection)
        try:
            for row in rows:
                source_name = str(row.get("source_object") or "")
                export_name = str(row.get("name") or "")
                source = bpy.data.objects.get(source_name)
                if source is None or source.type != "MESH":
                    raise RuntimeError(
                        f"Atlas manifest source mesh is missing: {source_name}"
                    )
                material_name = str(row.get("material") or "")
                material = bpy.data.materials.get(material_name)
                if material is None:
                    raise RuntimeError(
                        f"Atlas manifest material is missing: {material_name}"
                    )
                fbx_path = Path(str(row.get("fbx") or "")).expanduser().resolve()
                if not fbx_path.name.casefold().endswith(".fbx"):
                    raise RuntimeError(
                        f"Atlas manifest FBX path is invalid: {fbx_path}"
                    )
                if "wilow" in str(fbx_path).casefold():
                    raise RuntimeError(
                        f"Atlas manifest still has misspelled FBX path: {fbx_path}"
                    )
                if fbx_path.is_file():
                    shutil.copy2(fbx_path, backup_root / fbx_path.name)

                evaluated = source.evaluated_get(depsgraph)
                mesh = bpy.data.meshes.new_from_object(
                    evaluated,
                    depsgraph=depsgraph,
                )
                mesh.materials.clear()
                mesh.materials.append(material)
                for polygon in mesh.polygons:
                    polygon.material_index = 0
                mesh.name = export_name
                temp_object = bpy.data.objects.new(export_name, mesh)
                temp_object.matrix_world = source.matrix_world.copy()
                temp_collection.objects.link(temp_object)
                try:
                    bpy.ops.object.select_all(action="DESELECT")
                    temp_object.select_set(True)
                    bpy.context.view_layer.objects.active = temp_object
                    result = bpy.ops.export_scene.fbx(
                        filepath=str(fbx_path),
                        use_selection=True,
                        object_types={"MESH"},
                        apply_unit_scale=True,
                        bake_space_transform=False,
                        add_leaf_bones=False,
                        path_mode="RELATIVE",
                        embed_textures=False,
                    )
                    if set(result) != {"FINISHED"}:
                        raise RuntimeError(
                            f"Blender FBX export did not finish: {result}"
                        )
                    data = fbx_path.read_bytes()
                    if b"wilow" in data.lower():
                        raise RuntimeError(
                            f"Regenerated FBX still contains typo: {fbx_path}"
                        )
                    payload["exports"].append(
                        {
                            "source_object": source_name,
                            "material": material_name,
                            "fbx": str(fbx_path),
                            "size": len(data),
                            "sha256": _sha256(fbx_path),
                        }
                    )
                finally:
                    bpy.data.objects.remove(temp_object, do_unlink=True)
                    if mesh.users == 0:
                        bpy.data.meshes.remove(mesh)
        finally:
            bpy.data.collections.remove(temp_collection)

        payload["backup_root"] = str(backup_root)
        payload["status"] = "ok"
        exit_code = 0
    except Exception as exc:
        payload["errors"].append(str(exc))
        payload["traceback"] = traceback.format_exc()
    _write_report(report_path, payload)
    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
