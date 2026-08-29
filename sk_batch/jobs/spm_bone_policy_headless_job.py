"""Apply the persistent SpeedTree branch-bone policy before fleet work."""

from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
import uuid
from pathlib import Path


JOBS_DIR = Path(__file__).resolve().parent
SK_BATCH_DIR = JOBS_DIR.parent
REPO_DIR = SK_BATCH_DIR.parent
for path in (REPO_DIR, SK_BATCH_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from blender_addon_gateway import prepare_runtime  # noqa: E402


def parse_args(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--request", required=True)
    parser.add_argument("--report", required=True)
    return parser.parse_args(argv)


def blender_arguments():
    return sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else []


def write_json_atomic(path, payload):
    path = Path(path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{uuid.uuid4().hex}")
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def main(argv=None):
    args = parse_args(blender_arguments() if argv is None else argv)
    request_path = Path(args.request).resolve()
    report_path = Path(args.report).resolve()
    report = {
        "schema_version": 1,
        "kind": "speedtree_spm_minimum_bone_policy_batch",
        "status": "failed",
        "request": str(request_path),
        "results": [],
    }
    try:
        request = json.loads(request_path.read_text(encoding="utf-8"))
        if not isinstance(request, dict) or int(
            request.get("schema_version", 0)
        ) != 1:
            raise RuntimeError("SPM bone-policy request schema is invalid")
        raw_targets = list(request.get("targets") or [])
        if not raw_targets:
            raise RuntimeError("SPM bone-policy request has no targets")
        targets = [Path(value).expanduser().resolve() for value in raw_targets]
        if len({str(path).casefold() for path in targets}) != len(targets):
            raise RuntimeError("SPM bone-policy request contains duplicates")
        for target in targets:
            if target.suffix.casefold() != ".spm" or not target.is_file():
                raise RuntimeError(
                    "SPM bone-policy target is missing: " + str(target)
                )

        runtime = prepare_runtime(
            "sk_batch.jobs.spm_bone_policy_headless_job",
            {"speedtree_bone_weight_repair": ("speedtree_export_v1",)},
        )
        operation = runtime.operation(
            "speedtree_bone_weight_repair",
            "ensure_minimum_absolute_branch_bones",
        )
        report["blender_addon_runtime"] = runtime.receipt
        for target in targets:
            receipt = operation(str(target))
            if not isinstance(receipt, dict) or receipt.get("status") not in {
                "updated",
                "already_compliant",
                "excluded_cluster_source",
            }:
                raise RuntimeError(
                    "SPM bone-policy operation returned an invalid receipt: "
                    + str(target)
                )
            report["results"].append(receipt)
        report["target_count"] = len(targets)
        report["updated_count"] = len([
            row for row in report["results"] if row.get("status") == "updated"
        ])
        report["excluded_cluster_count"] = len([
            row for row in report["results"]
            if row.get("status") == "excluded_cluster_source"
        ])
        report["status"] = "ok"
    except Exception as exc:
        report["error"] = str(exc)
        report["traceback"] = traceback.format_exc()
    write_json_atomic(report_path, report)
    if report["status"] != "ok":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
