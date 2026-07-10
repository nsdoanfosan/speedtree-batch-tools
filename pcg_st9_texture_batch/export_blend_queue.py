"""Export missing/stale blend status rows for PCG target SK SPMs."""
import argparse
import csv
import json
import sys
from datetime import datetime
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from pcg_texture_audit import make_report
from pcg_texture_common import TARGETS_PATH, load_config, load_pcg_targets

BLEND_STATUSES = {"needs_blend", "needs_blend_update"}


def wind_preset_for(stem):
    value = str(stem).lower()
    if "deadleave" in value or "deadbranch" in value:
        return "NONE"
    if "tree" in value:
        return "TREE"
    if "bush" in value:
        return "BUSH"
    if "weed" in value or "grass" in value:
        return "GRASS"
    return "GRASS"


def iso_mtime(path):
    if not path:
        return ""
    p = Path(path)
    if not p.exists():
        return ""
    return datetime.fromtimestamp(p.stat().st_mtime).isoformat(timespec="seconds")


def expected_blend_for_spm(spm):
    return str(Path(spm).with_suffix(".blend")) if spm else ""


def build_blend_queue_from_report(report, source_report="<memory>"):
    rows = []
    for item in report.get("items", []):
        duplicate_targets = set(item.get("duplicate_pcg_target_mesh_names", []))
        for status in item.get("target_spm_statuses", []):
            if status.get("status") not in BLEND_STATUSES:
                continue
            sk_spm = status.get("sk_spm", "")
            blend = status.get("blend") or expected_blend_for_spm(sk_spm)
            blockers = []
            mesh_name = status.get("mesh_name", "")
            if mesh_name in duplicate_targets:
                blockers.append("duplicate_target_review")
            if status.get("materials_missing_m_prefix"):
                blockers.append("m_prefix_first")
            blend_state = "stale" if status.get("blend_stale") else "missing"
            rows.append({
                "folder_name": item.get("name", ""),
                "folder": item.get("folder", ""),
                "mesh_name": mesh_name,
                "queue_status": "review" if blockers else "ready",
                "target_status": status.get("status", ""),
                "blend_state": blend_state,
                "sk_spm": sk_spm,
                "blend": blend,
                "spm_mtime": iso_mtime(sk_spm),
                "blend_mtime": iso_mtime(blend),
                "wind_preset": wind_preset_for(Path(sk_spm).stem),
                "blockers": blockers,
                "actions": [
                    "Blend is missing or stale; handle it in the separate SK/BWR repair workflow if needed.",
                    "If queue_status=review, resolve blockers before acting on this row.",
                ],
            })
    summary = {
        "rows": len(rows),
        "ready": sum(1 for row in rows if row["queue_status"] == "ready"),
        "review": sum(1 for row in rows if row["queue_status"] != "ready"),
        "missing_blend": sum(1 for row in rows if row["blend_state"] == "missing"),
        "stale_blend": sum(1 for row in rows if row["blend_state"] == "stale"),
    }
    return {
        "source_report": str(source_report),
        "pcg_targets": report.get("pcg_targets", {}),
        "summary": summary,
        "items": rows,
    }


def build_blend_queue(report_path):
    report = json.loads(Path(report_path).read_text(encoding="utf-8"))
    return build_blend_queue_from_report(report, report_path)


def join_list(values):
    return "; ".join(str(value) for value in (values or []))


def write_csv(queue, csv_path):
    fields = [
        "queue_status",
        "target_status",
        "blend_state",
        "folder_name",
        "mesh_name",
        "wind_preset",
        "sk_spm",
        "blend",
        "spm_mtime",
        "blend_mtime",
        "blockers",
        "folder",
    ]
    rows = []
    for item in queue.get("items", []):
        rows.append({
            "queue_status": item.get("queue_status", ""),
            "target_status": item.get("target_status", ""),
            "blend_state": item.get("blend_state", ""),
            "folder_name": item.get("folder_name", ""),
            "mesh_name": item.get("mesh_name", ""),
            "wind_preset": item.get("wind_preset", ""),
            "sk_spm": item.get("sk_spm", ""),
            "blend": item.get("blend", ""),
            "spm_mtime": item.get("spm_mtime", ""),
            "blend_mtime": item.get("blend_mtime", ""),
            "blockers": join_list(item.get("blockers", [])),
            "folder": item.get("folder", ""),
        })
    with Path(csv_path).open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", nargs="?")
    parser.add_argument("--pcg-targets", default=str(TARGETS_PATH))
    parser.add_argument("--json", dest="json_path")
    parser.add_argument("--csv", dest="csv_path")
    args = parser.parse_args()

    if args.report:
        queue = build_blend_queue(args.report)
    else:
        cfg = load_config()
        pcg_targets = load_pcg_targets(args.pcg_targets) if args.pcg_targets else None
        report = make_report(cfg, pcg_targets=pcg_targets)
        queue = build_blend_queue_from_report(report, "<live-audit>")
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
