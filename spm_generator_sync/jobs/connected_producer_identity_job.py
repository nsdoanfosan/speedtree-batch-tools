"""Read-only Blender job that seals connected Cluster producer add-ons."""

import argparse
import hashlib
import json
import os
import sys
import uuid
from pathlib import Path

import bpy


REPO_DIR = Path(__file__).resolve().parents[2]
if str(REPO_DIR) not in sys.path:
    sys.path.insert(0, str(REPO_DIR))

from blender_addon_gateway import prepare_runtime


ADDONS = (
    "atlas_leaf_mesh_builder",
    "speedtree_cluster_normalizer",
)


def parse_args():
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", required=True)
    return parser.parse_args(argv)


def json_digest(value):
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def inventory(root):
    root = Path(root)
    rows = []
    for path in sorted(
        root.rglob("*"),
        key=lambda value: value.relative_to(root).as_posix().casefold(),
    ):
        relative = path.relative_to(root).as_posix()
        if "__pycache__" in path.parts or path.suffix.casefold() == ".pyc":
            continue
        info = path.lstat()
        if path.is_dir() and not path.is_symlink():
            continue
        rows.append({
            "path": relative,
            "kind": (
                "symlink" if path.is_symlink()
                else "file" if path.is_file()
                else "other"
            ),
            "size": int(info.st_size),
            "mtime_ns": int(info.st_mtime_ns),
            "reparse_point": bool(
                getattr(info, "st_file_attributes", 0) & 0x400
            ),
            "symlink_target": os.readlink(path) if path.is_symlink() else None,
        })
    return rows


def stable_file_hash(path):
    digests = []
    states = []
    for _pass in range(2):
        digest = hashlib.sha256()
        count = 0
        with Path(path).open("rb") as stream:
            before = os.fstat(stream.fileno())
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
                count += len(block)
            after = os.fstat(stream.fileno())
        state = (before.st_size, before.st_mtime_ns, after.st_size, after.st_mtime_ns)
        if state[0:2] != state[2:4] or count != after.st_size:
            raise RuntimeError(f"producer file changed while hashing: {path}")
        digests.append(digest.hexdigest())
        states.append(state[2:4])
    if digests[0] != digests[1] or states[0] != states[1]:
        raise RuntimeError(f"producer file was not stable across hashes: {path}")
    return {"size": states[0][0], "sha256": digests[0]}


def addon_identity(name, runtime_row):
    module_path = Path(runtime_row["module_file"]).resolve()
    root = module_path.parent if module_path.name == "__init__.py" else module_path
    before = inventory(root.parent if root.is_file() else root)
    manifest = []
    base = root.parent if root.is_file() else root
    for entry in before:
        path = base / entry["path"]
        sealed = dict(entry)
        if entry["kind"] in {"file", "symlink"} and path.is_file():
            sealed.update(stable_file_hash(path))
        manifest.append(sealed)
    after = inventory(base)
    stable = before == after
    return {
        "module": name,
        "module_file": str(module_path),
        "root": str(base),
        "file_count": len(manifest),
        "inventory_before_sha256": json_digest(before),
        "inventory_after_sha256": json_digest(after),
        "manifest_sha256": json_digest(manifest),
        "stable": stable,
        "error": None if stable else "add-on inventory changed during capture",
    }


def write_report(path, payload):
    path = Path(path).expanduser().absolute()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(
        f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    )
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def main():
    args = parse_args()
    addons = []
    errors = []
    addon_runtime = None
    try:
        addon_runtime = prepare_runtime(
            "spm_generator_sync.jobs.connected_producer_identity_job",
            {
                "atlas_leaf_mesh_builder": ("source_index_v1",),
                "speedtree_cluster_normalizer": (
                    "cluster_normalization_v1",
                ),
            },
        )
        runtime_rows = {
            row["id"]: row for row in addon_runtime.receipt["addons"]
        }
        for name in ADDONS:
            addons.append(addon_identity(name, runtime_rows[name]))
    except Exception as exc:
        errors.append({
            "module": "blender_addon_gateway",
            "error_type": type(exc).__name__,
            "error": str(exc),
        })
    payload = {
        "schema_version": 1,
        "kind": "connected_cluster_producer_identity",
        "provider_available": True,
        "blender_version": list(bpy.app.version),
        "python_version": list(sys.version_info[:3]),
        "addons": addons,
        "stable": not errors and all(addon["stable"] for addon in addons),
        "errors": errors,
    }
    if addon_runtime is not None:
        payload["blender_addon_runtime"] = addon_runtime.receipt
    payload["producer_manifest_sha256"] = json_digest({
        "blender_version": payload["blender_version"],
        "python_version": payload["python_version"],
        "addons": addons,
    })
    write_report(args.report, payload)


if __name__ == "__main__":
    main()
