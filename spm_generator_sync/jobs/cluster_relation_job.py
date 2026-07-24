"""Blender job for one normalized Cluster blend's ON/OFF relationships."""

import argparse
import json
import shutil
import sys
import traceback
from pathlib import Path

import addon_utils
import bpy


def parse_args():
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("sync", "remove"), required=True)
    parser.add_argument("--blend", required=True)
    parser.add_argument("--target", action="append", default=[], required=True)
    parser.add_argument("--report", required=True)
    return parser.parse_args(argv)


def key(path):
    return str(Path(path).expanduser().absolute()).casefold()


def restore_cleanups(cleanups):
    restored = []
    for cleanup in reversed(cleanups):
        backup = cleanup.get("backup")
        spm = cleanup.get("spm")
        if backup and spm and Path(backup).is_file():
            shutil.copy2(backup, spm)
            restored.append(str(spm))
    return restored


def sync_targets(blend, requested):
    if key(bpy.data.filepath) != key(blend):
        raise RuntimeError(
            f"Loaded blend differs from requested Cluster blend: {bpy.data.filepath}"
        )
    from atlas_leaf_mesh_builder.props import (
        save_spm_target_registry,
        sync_spm_target_registry,
    )
    from atlas_leaf_mesh_builder.speedtree import (
        export_or_update_speedtree_spm_targets,
        extend_source_material_adoptions_for_targets,
    )
    from atlas_leaf_mesh_builder.target_registry import load_target_registry

    props = bpy.context.scene.atlas_leaf_builder
    registry = load_target_registry(blend)
    if registry is None:
        raise RuntimeError(f"Atlas target JSON does not exist for {blend}")
    registered_paths = [
        Path(path).expanduser().absolute()
        for path in registry["target_spms"]
    ]
    registered = {key(path) for path in registered_paths}
    missing = [str(path) for path in requested if key(path) not in registered]
    if missing:
        raise RuntimeError("Requested ON target is absent from Atlas JSON: " + ", ".join(missing))
    sync_spm_target_registry(props, initialize_missing=False)
    scene_targets = {key(item.path) for item in props.speedtree_spm_items}
    if scene_targets != registered:
        raise RuntimeError("Blender Atlas target list did not match the external JSON")
    mapping_update = extend_source_material_adoptions_for_targets(
        props,
        registered_paths,
        blend_path=blend,
    )
    save_spm_target_registry(props)
    results = export_or_update_speedtree_spm_targets(props)
    completed = {key(result.get("spm_path")): result for result in results}
    unresolved = [str(path) for path in requested if key(path) not in completed]
    if unresolved:
        raise RuntimeError("Atlas build returned no result for: " + ", ".join(unresolved))
    return {
        "mode": "sync",
        "blend": str(blend),
        "target_spms": [str(path) for path in requested],
        "source_material_mapping_update": mapping_update,
        "results": results,
    }


def remove_targets(blend, requested):
    from atlas_leaf_mesh_builder.speedtree import remove_blend_target_from_spm
    from atlas_leaf_mesh_builder.target_registry import (
        load_target_registry,
        save_target_registry,
    )

    registry = load_target_registry(blend)
    if registry is None:
        raise RuntimeError(f"Atlas target JSON does not exist for {blend}")
    registered = {key(path): Path(path).expanduser().absolute() for path in registry["target_spms"]}
    for target in requested:
        if key(target) not in registered:
            raise RuntimeError(f"Requested OFF target is not ON for this blend: {target}")
    cleanups = []
    registry_path = Path(registry["registry_path"])
    registry_bytes = registry_path.read_bytes()
    try:
        for target in requested:
            cleanups.append(remove_blend_target_from_spm(blend, target))
        removed = {key(path) for path in requested}
        remaining = [path for path_key, path in registered.items() if path_key not in removed]
        payload = save_target_registry(blend, remaining)
    except Exception:
        restore_cleanups(cleanups)
        registry_path.write_bytes(registry_bytes)
        raise
    return {
        "mode": "remove",
        "blend": str(blend),
        "target_spms": [str(path) for path in requested],
        "remaining_target_spms": payload["target_spms"],
        "results": cleanups,
    }


def main():
    args = parse_args()
    report_path = Path(args.report).expanduser().absolute()
    report = {"status": "error"}
    try:
        enabled = addon_utils.enable(
            "atlas_leaf_mesh_builder", default_set=False, persistent=False
        )
        if enabled is None:
            raise RuntimeError("Could not enable atlas_leaf_mesh_builder")
        blend = Path(args.blend).expanduser().absolute()
        targets = [Path(value).expanduser().absolute() for value in args.target]
        payload = (
            sync_targets(blend, targets)
            if args.mode == "sync"
            else remove_targets(blend, targets)
        )
        report = {"status": "ok", **payload}
    except Exception as exc:
        report = {
            "status": "error",
            "error": str(exc),
            "traceback": traceback.format_exc(),
        }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(
            report,
            indent=2,
            ensure_ascii=False,
            default=str,
        )
        + "\n",
        encoding="utf-8",
    )
    if report["status"] != "ok":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
