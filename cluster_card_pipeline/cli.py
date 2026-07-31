"""Command-line entry point for the isolated cluster-card pipeline."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from .contract import (
    ContractError,
    _fingerprint,
    build_normalization_contract,
    write_handoff_spm_copies,
    write_json,
)


def _arguments(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--camera-spm", required=True)
    parser.add_argument("--tree-spm", required=True)
    parser.add_argument("--camera-name", default="Dropped XY plane camera 2")
    parser.add_argument("--material", default="M_branch_elm_01")
    parser.add_argument("--mesh-ids", nargs=3, type=int, default=(1, 2, 9))
    parser.add_argument("--output-prefix", default="branch_elm_01")
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--blender-exe")
    parser.add_argument("--skip-blender", action="store_true")
    parser.add_argument("--speedtree-exe")
    parser.add_argument("--speedtree-fbx-options")
    parser.add_argument("--speedtree-xml-options")
    return parser.parse_args(argv)


def _build_assembly_parts(contract, blender_report):
    report_by_name = {item["name"]: item for item in blender_report["planes"]}
    active_mesh_ids = list(contract.get("active_cutout_mesh_ids") or [])
    generator_bindings = list(contract.get("tree_generator_bindings") or [])
    parts = []
    for plane in contract["planes"]:
        artifact = report_by_name[plane["name"]]
        parts.append({
            "prototype_id": f"branch_card_cutout_{plane['source_mesh_id']}",
            "asset_name": plane["sk_alias"],
            "export_stem": plane["name"],
            "role": "branch_card",
            "role_identity": contract["material"]["name"],
            "source_cutout_mesh_id": plane["source_mesh_id"],
            "active_in_tree_generator": plane["source_mesh_id"] in active_mesh_ids,
            "fbx": _fingerprint(artifact["fbx"]["path"]),
            "obj": _fingerprint(artifact["obj"]["path"]),
            "topology_sha256": plane["topology_sha256"],
            "uv_sha256": plane["uv_sha256"],
            "attachment_origin": [0.0, 0.0, 0.0],
            "transform": {
                "translation": [0.0, 0.0, 0.0],
                "rotation_euler": [0.0, 0.0, 0.0],
                "scale": [1.0, 1.0, 1.0],
            },
            "validation": plane["validation"],
        })
    requested_mesh_ids = [plane["source_mesh_id"] for plane in contract["planes"]]
    missing_bindings = [mesh_id for mesh_id in requested_mesh_ids if mesh_id not in active_mesh_ids]
    placement_status = "ready" if not missing_bindings else "requires_tree_instance_binding"
    return {
        "kind": "speedtree_cluster_card_nanite_assembly_parts",
        "version": 1,
        "status": "prototype_ready",
        "material": contract["material"]["name"],
        "capture_camera": contract["camera"],
        "raw_3d_source_contract": contract["source_contracts"]["raw_3d"],
        "tree_generator_bindings": generator_bindings,
        "active_cutout_mesh_ids": active_mesh_ids,
        "parts": parts,
        "placement": {
            "status": placement_status,
            "missing_mesh_ids": missing_bindings,
            "reason": (
                "Derived from exact Generator GUID/slot bindings in the tree SPM; "
                f"active mesh IDs are {active_mesh_ids}, so placements for {missing_bindings} "
                "must not be invented."
                if missing_bindings else
                "Every normalized cutout mesh is represented by an exact tree Generator binding."
            ),
        },
    }


def run(argv=None):
    args = _arguments(argv)
    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    contract = build_normalization_contract(
        args.camera_spm,
        args.tree_spm,
        camera_name=args.camera_name,
        material_name=args.material,
        mesh_ids=args.mesh_ids,
        output_prefix=args.output_prefix,
    )
    manifest_path = write_json(output_dir / "branch_elm_01_normalization_manifest.json", contract)
    if not args.skip_blender:
        if not args.blender_exe:
            raise ContractError("--blender-exe is required unless --skip-blender is used")
        job = Path(__file__).with_name("blender_job.py")
        command = [
            str(Path(args.blender_exe).resolve()),
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
        completed = subprocess.run(command, text=True, capture_output=True)
        (output_dir / "blender_job_stdout.log").write_text(completed.stdout, encoding="utf-8")
        (output_dir / "blender_job_stderr.log").write_text(completed.stderr, encoding="utf-8")
        if completed.returncode:
            raise ContractError(
                f"Blender background job failed ({completed.returncode}); see output logs"
            )
        blender_report_path = output_dir / "branch_elm_01_blender_validation.json"
        blender_report = json.loads(blender_report_path.read_text(encoding="utf-8"))
        if blender_report.get("status") != "ready":
            raise ContractError("Blender validation report is not ready")
        handoff = write_handoff_spm_copies(contract, output_dir)
        write_json(output_dir / "branch_elm_01_speedtree_handoff.json", handoff)
        assembly = _build_assembly_parts(contract, blender_report)
        speedtree_values = (
            args.speedtree_exe,
            args.speedtree_fbx_options,
            args.speedtree_xml_options,
        )
        if any(speedtree_values) and not all(speedtree_values):
            raise ContractError(
                "SpeedTree verification requires --speedtree-exe, "
                "--speedtree-fbx-options, and --speedtree-xml-options together"
            )
        if all(speedtree_values):
            from .speedtree_verify import verify_speedtree_handoff

            speedtree_report = verify_speedtree_handoff(
                handoff["tree_handoff_spm"],
                output_dir / "speedtree_verify",
                speedtree_exe=args.speedtree_exe,
                fbx_options=args.speedtree_fbx_options,
                xml_options=args.speedtree_xml_options,
                contract=contract,
            )
            speedtree_report_path = write_json(
                output_dir / "branch_elm_01_speedtree_modeler_validation.json",
                speedtree_report,
            )
            assembly["speedtree_modeler_validation"] = {
                "status": speedtree_report["status"],
                "report": str(speedtree_report_path),
                "verified_mesh_ids": speedtree_report["verified_mesh_ids"],
                "variant_geometry": [
                    {
                        "source_mesh_id": row["source_mesh_id"],
                        "active_material_geometry": row["active_material_geometry"],
                    }
                    for row in speedtree_report["variants"]
                ],
            }
        write_json(output_dir / "branch_elm_01_assembly_parts.json", assembly)
    # Prove both SPM inputs stayed byte-identical throughout the operation.
    current_camera = _fingerprint(args.camera_spm)
    current_tree = _fingerprint(args.tree_spm)
    if current_camera["sha256"] != contract["camera_spm"]["sha256"]:
        raise ContractError("Camera SPM changed during normalization")
    if current_tree["sha256"] != contract["tree_spm"]["sha256"]:
        raise ContractError("Tree SPM changed during normalization")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(run())
    except ContractError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2)
