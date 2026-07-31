"""Index an exact requested set of .blend files in one Blender process."""
import argparse
import json
import os
import sys
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


def main():
    argv = sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else []
    parser = argparse.ArgumentParser()
    parser.add_argument("--request", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)

    report = {"schema_version": SCHEMA_VERSION, "status": "error", "rows": []}
    try:
        enabled = addon_utils.enable(
            "atlas_leaf_mesh_builder", default_set=False, persistent=False
        )
        if enabled is None:
            raise RuntimeError("atlas_leaf_mesh_builder add-on could not be enabled")
        from atlas_leaf_mesh_builder.source_index import (
            current_blend_source_index,
        )

        rows = []
        for request in _load_requests(args.request):
            blend = request["blend"]
            expected_sha256 = request["blend_sha256"]
            bpy.ops.wm.open_mainfile(filepath=str(blend), load_ui=False)
            rows.append(
                current_blend_source_index(
                    expected_blend_path=blend,
                    expected_sha256=expected_sha256,
                )
            )
        report = {
            "schema_version": SCHEMA_VERSION,
            "status": "ok",
            "rows": rows,
        }
    except Exception as exc:
        report = {
            "schema_version": SCHEMA_VERSION,
            "status": "error",
            "error": f"{type(exc).__name__}: {exc}",
            "rows": [],
        }
    _write_report(args.out, report)
    if report["status"] != "ok":
        sys.exit(1)


main()
