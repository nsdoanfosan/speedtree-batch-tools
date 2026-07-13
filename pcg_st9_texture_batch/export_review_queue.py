"""Export a combined manual-review queue for PCG ST9 texture prep."""
import argparse
import csv
import json
import re
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from export_prepare_plan import build_plan_from_report
from export_texture_plan import build_texture_plan_from_report

GENERIC_MATERIAL_RE = re.compile(r"^(?:m_)?material(?:\s+copy)?(?:\s*\d+)?$", re.IGNORECASE)


def add_row(rows, severity, issue_type, folder="", target="", cluster="", detail="", action="", evidence=""):
    rows.append({
        "severity": severity,
        "issue_type": issue_type,
        "folder": folder,
        "target": target,
        "cluster": cluster,
        "detail": detail,
        "recommended_action": action,
        "evidence": evidence,
    })


def generic_renames(renames):
    result = []
    for old, new in renames:
        if GENERIC_MATERIAL_RE.match(str(old).strip()):
            result.append(f"{old}->{new}")
    return result


def build_review_queue_from_report(report, source_report="<memory>"):
    rows = []
    pcg = report.get("pcg_targets", {})
    for mesh_name in pcg.get("unmatched_mesh_names", []):
        meshes = [
            path for path in pcg.get("unmatched_meshes", [])
            if mesh_name in path.lower()
        ]
        add_row(
            rows,
            "P1",
            "unmatched_pcg_mesh",
            target=mesh_name,
            detail="PCG target did not map to a local SpeedTree folder.",
            action="Verify the mesh in live Unreal DataAssets before treating it as missing source data.",
            evidence="; ".join(meshes),
        )
    for mesh_name, folders in pcg.get("duplicate_mesh_matches", {}).items():
        add_row(
            rows,
            "P1",
            "duplicate_folder_match",
            target=mesh_name,
            detail=f"PCG target matched multiple local folders: {', '.join(folders)}",
            action="Choose the owning folder before running SK/M_ or texture automation for this target.",
            evidence=", ".join(folders),
        )

    for item in report.get("items", []):
        for status in item.get("target_spm_statuses", []):
            if status.get("status") == "needs_source_review":
                add_row(
                    rows,
                    "P1",
                    "source_spm_missing",
                    folder=item.get("name", ""),
                    target=status.get("mesh_name", ""),
                    detail="No exact source or SK SPM was found for this PCG mesh target.",
                    action="Locate the source SPM or confirm this target should be ignored/substituted.",
                    evidence=item.get("folder", ""),
                )
            elif status.get("status") in {"needs_blend", "needs_blend_update"}:
                detail = "SK SPM exists but matching blend was not found."
                if status.get("status") == "needs_blend_update":
                    detail = "SK SPM is newer than the matching blend."
                add_row(
                    rows,
                    "P2",
                    "blend_missing",
                    folder=item.get("name", ""),
                    target=status.get("mesh_name", ""),
                    detail=detail,
                    action="Record this as a separate SK/BWR pipeline dependency before atlas handoff.",
                    evidence=status.get("sk_spm", ""),
                )

    prepare_plan = build_plan_from_report(report, source_report)
    for item in prepare_plan.get("items", []):
        duplicate_targets = set(item.get("duplicate_pcg_target_mesh_names", []))
        for target in item.get("targets", []):
            mesh_name = target.get("mesh_name", "")
            patch = target.get("patch") or {}
            renames = patch.get("renames", [])
            generic = generic_renames(renames)
            if target.get("status") == "skipped":
                add_row(
                    rows,
                    "P1",
                    "prepare_sk_skipped",
                    folder=item.get("name", ""),
                    target=mesh_name,
                    detail=target.get("reason", "prepare target skipped"),
                    action="Resolve source mapping before applying SK/M_ prep.",
                    evidence=item.get("folder", ""),
                )
            if mesh_name in duplicate_targets:
                add_row(
                    rows,
                    "P1",
                    "prepare_duplicate_target",
                    folder=item.get("name", ""),
                    target=mesh_name,
                    detail="This target is in the SK/M_ plan but also matched another folder.",
                    action="Resolve folder ownership before applying this row.",
                    evidence=target.get("sk_spm", "") or target.get("would_create", ""),
                )
            if generic:
                add_row(
                    rows,
                    "P2",
                    "generic_material_rename",
                    folder=item.get("name", ""),
                    target=mesh_name,
                    detail=f"Generic material names would be renamed: {'; '.join(generic[:8])}",
                    action="Open the SPM/material list and confirm these generic names are real target materials.",
                    evidence=target.get("sk_spm", "") or target.get("would_create", ""),
                )

    texture_plan = build_texture_plan_from_report(report, source_report)
    for item in texture_plan.get("items", []):
        if item.get("atlas_blend_status") != "ok":
            add_row(
                rows,
                "P2",
                "atlas_blend_missing",
                folder=item.get("folder_name", ""),
                target=", ".join(item.get("pcg_target_meshes", [])),
                cluster=item.get("cluster_name", ""),
                detail=f"Atlas blend missing for {item.get('atlas_base', '')}.",
                action="Create or locate the atlas blend before Blender leaf-generator handoff.",
                evidence=item.get("cluster_spm", ""),
            )
        if item.get("atlas_generator_input_status") != "ready":
            add_row(
                rows,
                "P2",
                "atlas_input_missing",
                folder=item.get("folder_name", ""),
                target=", ".join(item.get("pcg_target_meshes", [])),
                cluster=item.get("cluster_name", ""),
                detail=item.get("atlas_generator_input_status", ""),
                action="Confirm albedo and alpha source texture candidates for the Blender atlas generator.",
                evidence=item.get("cluster_spm", ""),
            )
        if item.get("missing_export_maps"):
            add_row(
                rows,
                "P2",
                "sbs_export_maps_missing",
                folder=item.get("folder_name", ""),
                target=", ".join(item.get("pcg_target_meshes", [])),
                cluster=item.get("cluster_name", ""),
                detail=f"Missing exported maps: {', '.join(item.get('missing_export_maps', []))}",
                action="Connect sources in SBS and export color, normal, extra, height, opacity to the SBS folder.",
                evidence="; ".join(item.get("sbs_files", [])),
            )
        if item.get("sbs_status") != "ready":
            add_row(
                rows,
                "P2",
                "sbs_input_missing",
                folder=item.get("folder_name", ""),
                target=", ".join(item.get("pcg_target_meshes", [])),
                cluster=item.get("cluster_name", ""),
                detail=item.get("sbs_status", ""),
                action="Locate or create the SBS/source input set for this cluster.",
                evidence=item.get("texture_dir", ""),
            )
        if item.get("normal_convention") == "unknown":
            add_row(
                rows,
                "P3",
                "normal_convention_unknown",
                folder=item.get("folder_name", ""),
                target=", ".join(item.get("pcg_target_meshes", [])),
                cluster=item.get("cluster_name", ""),
                detail="Normal convention could not be inferred from source refs.",
                action="Confirm OpenGL for TCom/Megascan or DirectX for Substance/SBSAR before exporting.",
                evidence=item.get("cluster_spm", ""),
            )

    order = {"P1": 0, "P2": 1, "P3": 2}
    rows.sort(key=lambda row: (order.get(row["severity"], 9), row["issue_type"], row["folder"], row["target"], row["cluster"]))
    counts = {}
    for row in rows:
        counts[row["severity"]] = counts.get(row["severity"], 0) + 1
    return {
        "source_report": str(source_report),
        "pcg_targets": pcg,
        "summary": {
            "total": len(rows),
            "by_severity": counts,
        },
        "items": rows,
    }


def build_review_queue(report_path):
    report = json.loads(Path(report_path).read_text(encoding="utf-8"))
    return build_review_queue_from_report(report, report_path)


def write_csv(queue, csv_path):
    fields = [
        "severity",
        "issue_type",
        "folder",
        "target",
        "cluster",
        "detail",
        "recommended_action",
        "evidence",
    ]
    with Path(csv_path).open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(queue.get("items", []))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report")
    parser.add_argument("--json", dest="json_path")
    parser.add_argument("--csv", dest="csv_path")
    args = parser.parse_args()
    queue = build_review_queue(args.report)
    if args.json_path:
        Path(args.json_path).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json_path).write_text(json.dumps(queue, indent=2, ensure_ascii=False), encoding="utf-8")
    if args.csv_path:
        Path(args.csv_path).parent.mkdir(parents=True, exist_ok=True)
        write_csv(queue, args.csv_path)
    if not args.json_path and not args.csv_path:
        print(json.dumps(queue, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
