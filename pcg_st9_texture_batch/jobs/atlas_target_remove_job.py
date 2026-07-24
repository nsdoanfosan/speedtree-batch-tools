"""Detach one Atlas blend's managed assets from selected SPMs, then edit JSON."""

import argparse
import json
import shutil
import sys
import traceback
from pathlib import Path

import addon_utils


def parse_args():
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--blend", required=True)
    parser.add_argument("--spm", action="append", default=[], required=True)
    parser.add_argument("--report", required=True)
    return parser.parse_args(argv)


def normalized_key(path):
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


def main():
    args = parse_args()
    report_path = Path(args.report).expanduser().absolute()
    report = {"status": "error"}
    cleanups = []
    registry_path = None
    registry_bytes = None
    try:
        enabled = addon_utils.enable(
            "atlas_leaf_mesh_builder", default_set=False, persistent=False
        )
        if enabled is None:
            raise RuntimeError("Could not enable atlas_leaf_mesh_builder")
        from atlas_leaf_mesh_builder.speedtree import remove_blend_target_from_spm
        from atlas_leaf_mesh_builder.target_registry import (
            load_target_registry,
            save_target_registry,
        )

        blend = Path(args.blend).expanduser().absolute()
        registry = load_target_registry(blend)
        if registry is None:
            raise RuntimeError(f"Atlas target JSON does not exist for {blend}")
        registry_path = Path(registry["registry_path"])
        registry_bytes = registry_path.read_bytes()
        registered = {
            normalized_key(path): Path(path).expanduser().absolute()
            for path in registry["target_spms"]
        }
        requested = []
        seen = set()
        for raw_path in args.spm:
            path = Path(raw_path).expanduser().absolute()
            key = normalized_key(path)
            if key in seen:
                continue
            if key not in registered:
                raise RuntimeError(f"SPM is not registered for this Atlas blend: {path}")
            seen.add(key)
            requested.append(path)

        for spm in requested:
            cleanups.append(remove_blend_target_from_spm(blend, spm))
        remaining = [
            path for key, path in registered.items() if key not in seen
        ]
        payload = save_target_registry(blend, remaining)
        report = {
            "status": "ok",
            "blend": str(blend),
            "removed_target_spms": [str(path) for path in requested],
            "remaining_target_spms": payload["target_spms"],
            "registry_path": payload["registry_path"],
            "results": cleanups,
        }
    except Exception as exc:
        restored = restore_cleanups(cleanups)
        if registry_path is not None and registry_bytes is not None:
            registry_path.write_bytes(registry_bytes)
        report = {
            "status": "error",
            "error": str(exc),
            "traceback": traceback.format_exc(),
            "restored_spms": restored,
            "results": cleanups,
        }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    if report["status"] != "ok":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
