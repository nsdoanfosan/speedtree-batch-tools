"""Replace one spelling token inside a Blender file without rebuilding data."""

import argparse
import json
import re
import sys

import bpy


def _replace(value, old, new):
    return re.sub(re.escape(old), new, str(value), flags=re.IGNORECASE)


def _iter_ids():
    for collection_name in (
        "actions",
        "armatures",
        "cameras",
        "collections",
        "curves",
        "fonts",
        "grease_pencils",
        "images",
        "lattices",
        "libraries",
        "lights",
        "materials",
        "meshes",
        "metaballs",
        "movieclips",
        "node_groups",
        "objects",
        "particles",
        "scenes",
        "sounds",
        "texts",
        "textures",
        "worlds",
        "workspaces",
    ):
        collection = getattr(bpy.data, collection_name, None)
        if collection is None:
            continue
        for datablock in collection:
            yield collection_name, datablock


def replace_file(old, new, apply_changes):
    changes = []
    for collection_name, datablock in _iter_ids():
        name = str(getattr(datablock, "name", "") or "")
        replaced = _replace(name, old, new)
        if replaced != name:
            changes.append(
                {
                    "kind": "datablock_name",
                    "collection": collection_name,
                    "old": name,
                    "new": replaced,
                }
            )
            if apply_changes:
                datablock.name = replaced

        for attribute in ("filepath", "filepath_raw"):
            if not hasattr(datablock, attribute):
                continue
            value = str(getattr(datablock, attribute, "") or "")
            replaced = _replace(value, old, new)
            if replaced == value:
                continue
            changes.append(
                {
                    "kind": attribute,
                    "collection": collection_name,
                    "datablock": name,
                    "old": value,
                    "new": replaced,
                }
            )
            if apply_changes:
                setattr(datablock, attribute, replaced)

        for key in list(datablock.keys()):
            value = datablock.get(key)
            if not isinstance(value, str):
                continue
            replaced = _replace(value, old, new)
            if replaced == value:
                continue
            changes.append(
                {
                    "kind": "custom_property",
                    "collection": collection_name,
                    "datablock": name,
                    "property": key,
                    "old": value,
                    "new": replaced,
                }
            )
            if apply_changes:
                datablock[key] = replaced

    if apply_changes and changes:
        bpy.ops.wm.save_as_mainfile(filepath=bpy.data.filepath, check_existing=False)
    return changes


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--old", required=True)
    parser.add_argument("--new", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--report", required=True)
    argv = sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else []
    args = parser.parse_args(argv)
    changes = replace_file(args.old, args.new, args.apply)
    payload = {
        "blend": bpy.data.filepath,
        "applied": bool(args.apply),
        "change_count": len(changes),
        "changes": changes,
    }
    with open(args.report, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False)


if __name__ == "__main__":
    main()
