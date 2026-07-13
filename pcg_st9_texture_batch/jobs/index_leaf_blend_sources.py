"""Read source-image names from existing .blend files in one Blender process."""
import argparse
import json
import os
import sys

import bpy


def main():
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)

    rows = []
    for name in sorted(os.listdir(args.root)):
        if not name.lower().endswith(".blend") or "backup" in name.lower():
            continue
        path = os.path.join(args.root, name)
        try:
            bpy.ops.wm.open_mainfile(filepath=path, load_ui=False)
            images = sorted({
                os.path.basename(bpy.path.abspath(image.filepath)).lower()
                for image in bpy.data.images if image.filepath
            })
            rows.append({"blend": path, "images": images})
        except Exception as exc:
            rows.append({"blend": path, "images": [], "error": str(exc)})

    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(rows, handle, indent=2, ensure_ascii=False)


main()
