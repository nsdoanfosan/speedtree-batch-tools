"""Print a compact object/material inventory for a headless FBX audit."""

import argparse
import json
import sys
from pathlib import Path

import bpy


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--fbx", required=True, type=Path)
    args = parser.parse_args(sys.argv[sys.argv.index("--") + 1 :])

    bpy.ops.wm.read_factory_settings(use_empty=True)
    result = bpy.ops.import_scene.fbx(filepath=str(args.fbx.resolve()))
    if "FINISHED" not in result:
        raise RuntimeError(f"FBX import failed: {sorted(result)}")
    rows = []
    for obj in sorted(bpy.data.objects, key=lambda item: item.name.casefold()):
        if obj.type != "MESH":
            continue
        rows.append(
            {
                "object": obj.name,
                "vertices": len(obj.data.vertices),
                "polygons": len(obj.data.polygons),
                "materials": [
                    slot.material.name if slot.material else None
                    for slot in obj.material_slots
                ],
            }
        )
    print("SK_FBX_MATERIAL_INVENTORY=" + json.dumps(rows, ensure_ascii=False))


if __name__ == "__main__":
    main()
