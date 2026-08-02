"""Generate exact Blender camera-reference artifacts for any cluster-card role."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from process_lifecycle import owned_run

from .contract import CONTRACT_KIND, ContractError, read_uv_template_contract, write_json


def _arguments(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--camera-spm", required=True)
    parser.add_argument("--tree-spm", required=True)
    parser.add_argument("--camera-name", required=True)
    parser.add_argument("--material", required=True)
    parser.add_argument("--material-id", type=int)
    parser.add_argument("--output-prefix", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--blender-exe", required=True)
    return parser.parse_args(argv)


def run(argv=None):
    args = _arguments(argv)
    output_dir = Path(args.output_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    contract = read_uv_template_contract(
        args.camera_spm,
        args.tree_spm,
        camera_name=args.camera_name,
        material_name=args.material,
        material_id=args.material_id,
        output_prefix=args.output_prefix,
    )
    contract["kind"] = CONTRACT_KIND
    contract["output"] = {
        "blend_name": f"{args.output_prefix}.blend",
        "object_prefix": args.output_prefix,
        "raw_sk_blend_name": f"SK_{args.output_prefix}.blend",
    }
    contract["reference_generation"] = {
        "mode": "authoritative_uv_template_only",
        "generator_placement_invented": False,
        "ordered_cutout_count": len(contract["planes"]),
    }
    manifest_path = write_json(
        output_dir / f"{args.output_prefix}_normalization_manifest.json",
        contract,
    )
    job = Path(__file__).with_name("blender_job.py")
    command = [
        str(Path(args.blender_exe).expanduser().resolve()),
        "--factory-startup",
        "--background",
        "--python",
        str(job),
        "--",
        "--manifest",
        str(manifest_path),
        "--output-dir",
        str(output_dir),
    ]
    completed = owned_run(
        command,
        source="cluster_card_pipeline.reference_cli.blender_job",
        run_factory=subprocess.run,
        text=True,
        capture_output=True,
    )
    (output_dir / "blender_job_stdout.log").write_text(
        completed.stdout, encoding="utf-8"
    )
    (output_dir / "blender_job_stderr.log").write_text(
        completed.stderr, encoding="utf-8"
    )
    if completed.returncode:
        raise ContractError(
            f"Blender reference job failed ({completed.returncode}); see output logs"
        )
    validation_path = output_dir / f"{args.output_prefix}_blender_validation.json"
    if not validation_path.is_file():
        raise ContractError(f"Blender reference validation is missing: {validation_path}")
    validation = json.loads(validation_path.read_text(encoding="utf-8"))
    if validation.get("status") != "ready":
        raise ContractError("Blender reference validation is not ready")
    print(str(validation_path))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(run())
    except ContractError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2)
