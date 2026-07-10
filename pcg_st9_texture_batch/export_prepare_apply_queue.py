"""Classify SK/M_ prepare targets and optionally apply only safe rows."""
import argparse
import csv
import json
import sys
from collections import Counter
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from export_review_queue import generic_renames
from pcg_texture_audit import make_report, prepare_sk
from pcg_texture_common import TARGETS_PATH, load_config, load_pcg_targets

PREPARE_STATUSES = {"needs_sk", "needs_m_prefix"}


def _first_target(preview):
    targets = preview.get("targets") or []
    return targets[0] if targets else {}


def _preview_for_target(item, mesh_name):
    try:
        return _first_target(prepare_sk(item["folder"], [mesh_name], dry_run=True))
    except Exception as exc:
        return {"mesh_name": mesh_name, "status": "skipped", "reason": str(exc)}


def _classify_blockers(item, status_entry, preview):
    blockers = []
    mesh_name = status_entry.get("mesh_name", "")
    if mesh_name in set(item.get("duplicate_pcg_target_mesh_names", [])):
        blockers.append("duplicate_target_review")
    if status_entry.get("status") == "needs_source_review":
        blockers.append("source_review")
    if preview.get("status") == "skipped":
        blockers.append("prepare_skipped")
    patch = preview.get("patch") or {}
    if generic_renames(patch.get("renames", [])):
        blockers.append("generic_material_rename_review")
    return blockers


def build_prepare_apply_queue_from_report(report, source_report="<memory>"):
    rows = []
    for item in report.get("items", []):
        for status_entry in item.get("target_spm_statuses", []):
            if status_entry.get("status") not in PREPARE_STATUSES:
                continue
            mesh_name = status_entry.get("mesh_name", "")
            preview = _preview_for_target(item, mesh_name)
            patch = preview.get("patch") or {}
            renames = patch.get("renames", [])
            blockers = _classify_blockers(item, status_entry, preview)
            safe = not blockers
            rows.append({
                "folder_name": item.get("name", ""),
                "folder": item.get("folder", ""),
                "mesh_name": mesh_name,
                "target_status": status_entry.get("status", ""),
                "apply_status": "ready" if safe else "review",
                "safe_to_apply": safe,
                "blockers": blockers,
                "sk_spm": preview.get("sk_spm", ""),
                "would_create": preview.get("would_create", ""),
                "rename_count": len(renames),
                "renames": renames,
                "reason": preview.get("reason", ""),
            })
    blocker_counts = Counter(
        blocker
        for row in rows
        for blocker in row.get("blockers", [])
    )
    summary = {
        "rows": len(rows),
        "ready": sum(1 for row in rows if row["safe_to_apply"]),
        "review": sum(1 for row in rows if not row["safe_to_apply"]),
        "would_create": sum(1 for row in rows if row["safe_to_apply"] and row.get("would_create")),
        "patch_existing": sum(1 for row in rows if row["safe_to_apply"] and not row.get("would_create")),
        "rename_count": sum(row.get("rename_count", 0) for row in rows if row["safe_to_apply"]),
        "blockers": dict(sorted(blocker_counts.items())),
    }
    return {
        "source_report": str(source_report),
        "pcg_targets": report.get("pcg_targets", {}),
        "summary": summary,
        "items": rows,
    }


def build_prepare_apply_queue(report_path):
    report = json.loads(Path(report_path).read_text(encoding="utf-8"))
    return build_prepare_apply_queue_from_report(report, report_path)


def _join(values):
    return "; ".join(str(value) for value in (values or []))


def _join_renames(renames):
    return "; ".join(f"{old}->{new}" for old, new in (renames or []))


def write_csv(queue, csv_path):
    fields = [
        "apply_status",
        "safe_to_apply",
        "folder_name",
        "mesh_name",
        "target_status",
        "blockers",
        "sk_spm",
        "would_create",
        "rename_count",
        "renames",
        "reason",
        "folder",
    ]
    rows = []
    for item in queue.get("items", []):
        rows.append({
            "apply_status": item.get("apply_status", ""),
            "safe_to_apply": item.get("safe_to_apply", ""),
            "folder_name": item.get("folder_name", ""),
            "mesh_name": item.get("mesh_name", ""),
            "target_status": item.get("target_status", ""),
            "blockers": _join(item.get("blockers", [])),
            "sk_spm": item.get("sk_spm", ""),
            "would_create": item.get("would_create", ""),
            "rename_count": item.get("rename_count", 0),
            "renames": _join_renames(item.get("renames", [])),
            "reason": item.get("reason", ""),
            "folder": item.get("folder", ""),
        })
    with Path(csv_path).open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def apply_safe_rows(queue, dry_run=True):
    results = []
    for row in queue.get("items", []):
        if not row.get("safe_to_apply"):
            continue
        result = prepare_sk(row["folder"], [row["mesh_name"]], dry_run=dry_run)
        result["folder_name"] = row.get("folder_name", "")
        result["mesh_name"] = row.get("mesh_name", "")
        results.append(result)
    summary = {
        "dry_run": dry_run,
        "rows": len(results),
        "targets": sum(len(item.get("targets", [])) for item in results),
        "changed_targets": 0,
        "created": 0,
        "renamed_materials": 0,
    }
    for result in results:
        for target in result.get("targets", []):
            patch = target.get("patch") or {}
            if target.get("created"):
                summary["created"] += 1
            if patch.get("changed"):
                summary["changed_targets"] += 1
            summary["renamed_materials"] += len(patch.get("renames", []))
    return {"summary": summary, "items": results}


def _write_json(path, data):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", nargs="?")
    parser.add_argument("--pcg-targets", default=str(TARGETS_PATH))
    parser.add_argument("--json", dest="json_path")
    parser.add_argument("--csv", dest="csv_path")
    parser.add_argument("--apply", action="store_true", help="Apply only safe rows. Without this, nothing is mutated.")
    parser.add_argument("--apply-json", dest="apply_json_path")
    args = parser.parse_args()

    if args.report:
        report = json.loads(Path(args.report).read_text(encoding="utf-8"))
        source_report = args.report
    else:
        cfg = load_config()
        pcg_targets = load_pcg_targets(args.pcg_targets) if args.pcg_targets else None
        report = make_report(cfg, pcg_targets=pcg_targets)
        source_report = "<live-audit>"
    queue = build_prepare_apply_queue_from_report(report, source_report)

    if args.json_path:
        _write_json(args.json_path, queue)
    if args.csv_path:
        Path(args.csv_path).parent.mkdir(parents=True, exist_ok=True)
        write_csv(queue, args.csv_path)
    if args.apply:
        applied = apply_safe_rows(queue, dry_run=False)
        if args.apply_json_path:
            _write_json(args.apply_json_path, applied)
        print(json.dumps(applied, indent=2, ensure_ascii=False))
    elif not args.json_path and not args.csv_path:
        print(json.dumps(queue, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
