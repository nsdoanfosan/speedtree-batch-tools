"""Index an exact requested set of .blend files in one Blender process."""
import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path

import addon_utils
import bpy


SCHEMA_VERSION = 1


def _write_report(path, payload):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    os.replace(temporary, target)


def _load_requests(path):
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise RuntimeError("unsupported blend source-index request schema")
    rows = payload.get("requests")
    if not isinstance(rows, list) or not rows:
        raise RuntimeError("blend source-index request is empty")
    requests = []
    seen = set()
    for row in rows:
        if not isinstance(row, dict) or not row.get("blend") or not row.get("blend_sha256"):
            raise RuntimeError("blend source-index request identity is incomplete")
        blend = Path(row["blend"]).resolve()
        key = os.path.normcase(str(blend)).casefold()
        if key in seen:
            raise RuntimeError(f"duplicate blend source-index request: {blend}")
        seen.add(key)
        requests.append(
            {"blend": blend, "blend_sha256": str(row["blend_sha256"]).casefold()}
        )
    return requests


def _load_source_index_function():
    """Import the pure index function, then remove add-on side effects."""
    enabled = addon_utils.enable(
        "atlas_leaf_mesh_builder", default_set=False, persistent=False
    )
    if enabled is None:
        raise RuntimeError("atlas_leaf_mesh_builder add-on could not be enabled")
    try:
        from atlas_leaf_mesh_builder.source_index import (
            current_blend_source_index,
        )

        # Enabling the add-on makes its package importable, but this worker
        # must not retain the UI add-on's load handlers or delayed scene
        # initialization while it opens unrelated source files.
        addon_module = sys.modules.get("atlas_leaf_mesh_builder")
        initialize_scene_items = getattr(
            addon_module, "initialize_scene_items", None
        )
        if (
            initialize_scene_items is not None
            and bpy.app.timers.is_registered(initialize_scene_items)
        ):
            bpy.app.timers.unregister(initialize_scene_items)
        return current_blend_source_index
    finally:
        addon_utils.disable("atlas_leaf_mesh_builder", default_set=False)


def main():
    argv = sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else []
    parser = argparse.ArgumentParser()
    parser.add_argument("--request", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)

    process_started = time.perf_counter()
    report = {"schema_version": SCHEMA_VERSION, "status": "error", "rows": []}
    try:
        addon_started = time.perf_counter()
        current_blend_source_index = _load_source_index_function()
        addon_seconds = time.perf_counter() - addon_started

        rows = []
        request_timings = []
        active_blend = None
        for request in _load_requests(args.request):
            blend = request["blend"]
            active_blend = blend
            expected_sha256 = request["blend_sha256"]
            open_started = time.perf_counter()
            bpy.ops.wm.open_mainfile(filepath=str(blend), load_ui=False)
            open_seconds = time.perf_counter() - open_started
            index_started = time.perf_counter()
            row = current_blend_source_index(
                expected_blend_path=blend,
                expected_sha256=expected_sha256,
            )
            index_seconds = time.perf_counter() - index_started
            rows.append(row)
            request_timings.append({
                "blend_path_sha256": hashlib.sha256(
                    os.path.normcase(str(blend)).casefold().encode("utf-8")
                ).hexdigest(),
                "blend_bytes": blend.stat().st_size,
                "open_seconds": round(open_seconds, 6),
                "index_seconds": round(index_seconds, 6),
            })
            active_blend = None
        report = {
            "schema_version": SCHEMA_VERSION,
            "status": "ok",
            "rows": rows,
            "timing": {
                "addon_enable_seconds": round(addon_seconds, 6),
                "requests": request_timings,
                "total_seconds": round(
                    time.perf_counter() - process_started, 6
                ),
            },
        }
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        if locals().get("active_blend") is not None:
            error = f"{active_blend}: {error}"
        report = {
            "schema_version": SCHEMA_VERSION,
            "status": "error",
            "error": error,
            "rows": [],
        }
    _write_report(args.out, report)
    if report["status"] != "ok":
        sys.exit(1)


main()
