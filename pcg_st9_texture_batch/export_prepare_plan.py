"""Export a dry-run SK/M_ preparation plan from an audit report."""
import argparse
import csv
import json
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from pcg_texture_audit import prepare_sk


def build_plan_from_report(report, source_report="<memory>"):
    planned = []
    for item in report.get("items", []):
        target_names = [
            entry["mesh_name"]
            for entry in item.get("target_spm_statuses", [])
            if entry.get("status") in {"needs_sk", "needs_m_prefix"}
        ]
        if not target_names:
            continue
        plan = prepare_sk(item["folder"], target_names, dry_run=True)
        plan["name"] = item.get("name")
        plan["status"] = item.get("status")
        plan["duplicate_pcg_target_mesh_names"] = item.get("duplicate_pcg_target_mesh_names", [])
        planned.append(plan)
    return {
        "source_report": str(source_report),
        "pcg_targets": report.get("pcg_targets", {}),
        "summary": {
            "folders": len(planned),
            "targets": sum(len(item.get("targets", [])) for item in planned),
        },
        "items": planned,
    }


def build_plan(report_path):
    report = json.loads(Path(report_path).read_text(encoding="utf-8"))
    return build_plan_from_report(report, report_path)


def write_csv(plan, csv_path):
    rows = []
    for item in plan.get("items", []):
        for target in item.get("targets", []):
            patch = target.get("patch") or {}
            rows.append({
                "folder_name": item.get("name", ""),
                "folder": item.get("folder", ""),
                "mesh_name": target.get("mesh_name", ""),
                "duplicate_match": target.get("mesh_name", "") in item.get("duplicate_pcg_target_mesh_names", []),
                "status": target.get("status", ""),
                "sk_spm": target.get("sk_spm", ""),
                "would_create": target.get("would_create", ""),
                "rename_count": len(patch.get("renames", [])),
                "renames": "; ".join(f"{old}->{new}" for old, new in patch.get("renames", [])),
                "reason": target.get("reason", ""),
            })
    fields = [
        "folder_name", "folder", "mesh_name", "duplicate_match", "status", "sk_spm",
        "would_create", "rename_count", "renames", "reason",
    ]
    with Path(csv_path).open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report")
    parser.add_argument("--json", dest="json_path")
    parser.add_argument("--csv", dest="csv_path")
    args = parser.parse_args()
    plan = build_plan(args.report)
    if args.json_path:
        Path(args.json_path).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json_path).write_text(json.dumps(plan, indent=2, ensure_ascii=False), encoding="utf-8")
    if args.csv_path:
        Path(args.csv_path).parent.mkdir(parents=True, exist_ok=True)
        write_csv(plan, args.csv_path)
    if not args.json_path and not args.csv_path:
        print(json.dumps(plan, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
