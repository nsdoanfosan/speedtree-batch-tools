"""Export XML-only candidates after Relative=1 and compare exact bone counts."""

from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
import uuid
import xml.etree.ElementTree as ET
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
        "kind": "speedtree_relative_one_xml_bone_compare",
        "status": "failed",
        "request": str(request_path),
        "results": [],
    }
    try:
        request = json.loads(request_path.read_text(encoding="utf-8"))
        targets = list(request.get("targets") or [])
        if int(request.get("schema_version", 0)) != 1 or not targets:
            raise RuntimeError("Relative-one XML compare request is invalid")
        executable = Path(str(request.get("speedtree_exe") or "")).resolve()
        options = Path(str(request.get("xml_export_options") or "")).resolve()
        fbx_options = Path(str(request.get("fbx_export_options") or "")).resolve()
        output_root = Path(str(request.get("output_root") or "")).resolve()
        if not executable.is_file() or not options.is_file() or not fbx_options.is_file():
            raise RuntimeError("Relative-one XML compare executable/options are missing")
        runtime = prepare_runtime(
            "sk_batch.jobs.relative_one_xml_compare_headless_job",
            {"speedtree_bone_weight_repair": ("speedtree_export_v1",)},
        )
        operation = runtime.operation(
            "speedtree_bone_weight_repair",
            "run_speedtree_cli_export",
        )
        report["blender_addon_runtime"] = runtime.receipt
        for raw in targets:
            spm = Path(str(raw.get("spm") or "")).resolve()
            if not spm.is_file():
                raise RuntimeError("Relative-one compare SPM is missing: " + str(spm))
            target_output = output_root / spm.stem
            export = operation(
                str(spm),
                speedtree_exe_path=str(executable),
                fbx_export_options_path=str(fbx_options),
                xml_export_options_path=str(options),
                output_root=str(target_output),
                name_stem=spm.stem,
                export_fbx=True,
                export_xml=True,
                timeout_seconds=int(request.get("timeout_seconds", 900)),
                force_reexport=True,
                allow_verification_fallback=bool(
                    request.get("allow_verification_fallback", True)
                ),
            )
            xml_path = Path(export["exports"]["xml"]["path"]).resolve()
            after_count = len(ET.parse(xml_path).getroot().findall(".//Bones/Bone"))
            before_count = int(raw.get("before_bone_count", 0))
            report["results"].append(
                {
                    "spm": str(spm),
                    "asset": spm.stem,
                    "before_bone_count": before_count,
                    "after_bone_count": after_count,
                    "bone_delta": after_count - before_count,
                    "xml": str(xml_path),
                    "export": export,
                }
            )
        report["target_count"] = len(targets)
        report["before_bone_count"] = sum(
            row["before_bone_count"] for row in report["results"]
        )
        report["after_bone_count"] = sum(
            row["after_bone_count"] for row in report["results"]
        )
        report["bone_delta"] = report["after_bone_count"] - report["before_bone_count"]
        report["status"] = "ok"
    except Exception as exc:
        report["error"] = str(exc)
        report["traceback"] = traceback.format_exc()
    write_json_atomic(report_path, report)
    if report["status"] != "ok":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
