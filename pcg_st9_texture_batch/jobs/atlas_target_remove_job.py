"""Detach one Atlas blend's managed assets from selected SPMs, then edit JSON."""

import argparse
import json
import shutil
import sys
import traceback
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parents[1]
REPO_DIR = TOOL_DIR.parent
sys.path.insert(0, str(REPO_DIR))
sys.path.insert(0, str(TOOL_DIR))

from mutation_plan_authority import validate_child_authority
from blender_addon_gateway import prepare_runtime


def parse_args():
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--blend", required=True)
    parser.add_argument("--spm", action="append", default=[], required=True)
    parser.add_argument("--report", required=True)
    parser.add_argument("--authority-json", required=True)
    parser.add_argument("--authority-sha256", required=True)
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
    addon_runtime = None
    try:
        authority = validate_child_authority(
            args.authority_json,
            args.authority_sha256,
        )
        addon_runtime = prepare_runtime(
            "pcg_st9_texture_batch.jobs.atlas_target_remove_job",
            {"atlas_leaf_mesh_builder": ("target_registry_v1",)},
        )
        remove_blend_target_from_spm = addon_runtime.operation(
            "atlas_leaf_mesh_builder", "remove_blend_target_from_spm"
        )
        load_target_registry = addon_runtime.operation(
            "atlas_leaf_mesh_builder", "load_target_registry"
        )
        save_target_registry = addon_runtime.operation(
            "atlas_leaf_mesh_builder", "save_target_registry"
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
        if registry_path.read_bytes() != registry_bytes:
            raise RuntimeError(
                "Atlas target registry changed during removal; rolling back"
            )
        payload = save_target_registry(blend, remaining)
        report = {
            "status": "ok",
            "blend": str(blend),
            "removed_target_spms": [str(path) for path in requested],
            "remaining_target_spms": payload["target_spms"],
            "registry_path": payload["registry_path"],
            "results": cleanups,
            "authority_sha256": authority.get(
                "parent_authority_sha256"
            ),
            "authority_unit": authority.get("unit_id"),
            "blender_addon_runtime": addon_runtime.receipt,
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
            "authority_document_sha256": args.authority_sha256,
        }
        if addon_runtime is not None:
            report["blender_addon_runtime"] = addon_runtime.receipt
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    if report["status"] != "ok":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
