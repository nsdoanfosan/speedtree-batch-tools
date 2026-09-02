"""Set every Relative SpeedTree Branch/Spline Branch Physics:Bones to one."""

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
        "kind": "speedtree_relative_branch_bones_one_batch",
        "status": "failed",
        "request": str(request_path),
        "results": [],
    }
    try:
        request = json.loads(request_path.read_text(encoding="utf-8"))
        if not isinstance(request, dict) or int(request.get("schema_version", 0)) != 1:
            raise RuntimeError("Relative-one request schema is invalid")
        raw_targets = list(request.get("targets") or [])
        if not raw_targets:
            raise RuntimeError("Relative-one request has no targets")
        targets = [Path(value).expanduser().resolve() for value in raw_targets]
        if len({str(path).casefold() for path in targets}) != len(targets):
            raise RuntimeError("Relative-one request contains duplicate targets")
        for target in targets:
            if target.suffix.casefold() != ".spm" or not target.is_file():
                raise RuntimeError("Relative-one SPM is missing: " + str(target))

        apply_changes = bool(request.get("apply", False))
        runtime = prepare_runtime(
            "sk_batch.jobs.relative_bone_one_headless_job",
            {"speedtree_bone_weight_repair": ("relative_bone_one_v1",)},
        )
        operation_name = (
            "apply_relative_branch_bones_one"
            if apply_changes
            else "plan_relative_branch_bones_one"
        )
        operation = runtime.operation(
            "speedtree_bone_weight_repair",
            operation_name,
        )
        report["blender_addon_runtime"] = runtime.receipt
        report["apply"] = apply_changes
        for target in targets:
            result = operation(str(target))
            valid_statuses = (
                {"updated", "already_compliant"}
                if apply_changes
                else {"planned", "already_compliant"}
            )
            if not isinstance(result, dict) or result.get("status") not in valid_statuses:
                raise RuntimeError(
                    "Relative-one operation returned an invalid receipt: "
                    + str(target)
                )
            report["results"].append(result)
        report["target_count"] = len(targets)
        report["updated_count"] = sum(
            row.get("status") == "updated" for row in report["results"]
        )
        report["planned_count"] = sum(
            row.get("status") == "planned" for row in report["results"]
        )
        report["already_compliant_count"] = sum(
            row.get("status") == "already_compliant" for row in report["results"]
        )
        report["relative_generator_count"] = sum(
            int(row.get("relative_generator_count", 0))
            for row in report["results"]
        )
        report["changed_generator_count"] = sum(
            int(row.get("changed_generator_count", 0))
            for row in report["results"]
        )
        report["backup_count"] = sum(
            bool(row.get("backup")) for row in report["results"]
        )
        report["status"] = "ok"
    except Exception as exc:
        report["error"] = str(exc)
        report["traceback"] = traceback.format_exc()
    write_json_atomic(report_path, report)
    if report["status"] != "ok":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
