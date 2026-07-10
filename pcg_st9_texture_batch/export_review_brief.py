"""Export a human-readable review brief for the PCG ST9 texture pass."""
import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from export_atlas_handoff_queue import build_atlas_queue_from_report
from export_blend_queue import build_blend_queue_from_report
from export_prepare_apply_queue import build_prepare_apply_queue_from_report
from export_review_queue import build_review_queue_from_report
from export_sbs_handoff_queue import build_sbs_queue_from_report
from export_texture_plan import build_texture_plan_from_report


def _count_by(rows, key):
    counts = {}
    for row in rows:
        value = row.get(key) or "unknown"
        counts[value] = counts.get(value, 0) + 1
    return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))


def _target_label(row):
    parts = []
    if row.get("folder"):
        parts.append(row["folder"])
    if row.get("target"):
        parts.append(row["target"])
    if row.get("cluster"):
        parts.append(row["cluster"])
    return " / ".join(parts) if parts else "(global)"


def _append_rows(lines, rows, limit=20):
    for row in rows[:limit]:
        lines.append(
            f"- `{row.get('issue_type', '')}` | `{_target_label(row)}`: "
            f"{row.get('detail', '')} Action: {row.get('recommended_action', '')}"
        )
    if len(rows) > limit:
        lines.append(f"- ... {len(rows) - limit} more rows in the JSON/CSV review queue.")


def build_review_brief_from_report(report, source_report="<memory>", max_rows=20):
    review_queue = build_review_queue_from_report(report, source_report)
    prepare_queue = build_prepare_apply_queue_from_report(report, source_report)
    blend_queue = build_blend_queue_from_report(report, source_report)
    texture_plan = build_texture_plan_from_report(report, source_report)
    atlas_queue = build_atlas_queue_from_report(report, source_report)
    sbs_queue = build_sbs_queue_from_report(report, source_report)

    review_items = review_queue.get("items", [])
    p1_rows = [row for row in review_items if row.get("severity") == "P1"]
    p2_rows = [row for row in review_items if row.get("severity") == "P2"]
    p3_rows = [row for row in review_items if row.get("severity") == "P3"]
    blend_rows = blend_queue.get("items", [])
    blend_review = [row for row in blend_rows if row.get("queue_status") == "review"]
    stale_blends = [row for row in blend_rows if row.get("blend_state") == "stale"]
    missing_blends = [row for row in blend_rows if row.get("blend_state") == "missing"]

    pcg = report.get("pcg_targets", {})
    prepare_summary = prepare_queue.get("summary", {})
    blend_summary = blend_queue.get("summary", {})
    texture_summary = texture_plan.get("summary", {})
    atlas_summary = atlas_queue.get("summary", {})
    sbs_summary = sbs_queue.get("summary", {})

    lines = [
        "# PCG ST9 Texture Review Brief",
        "",
        f"Generated: {datetime.now().isoformat(timespec='seconds')}",
        f"Source report: `{source_report}`",
        "",
        "## Current State",
        "",
        f"- PCG graph: `{pcg.get('graph', '')}`",
        f"- PCG mesh targets: {pcg.get('mesh_count', 0)} total, "
        f"{pcg.get('matched_mesh_count', 0)} matched, "
        f"{pcg.get('unmatched_mesh_count', 0)} unmatched, "
        f"{pcg.get('duplicate_mesh_match_count', 0)} duplicate-name matches",
        f"- SK/M_ safe apply rows: {prepare_summary.get('ready', 0)} ready, "
        f"{prepare_summary.get('review', 0)} review",
        f"- Blend status rows: {blend_summary.get('rows', 0)} total, "
        f"{blend_summary.get('missing_blend', 0)} missing, "
        f"{blend_summary.get('stale_blend', 0)} stale",
        f"- Texture clusters: {texture_summary.get('clusters', 0)} total, "
        f"{texture_summary.get('missing_atlas_inputs', 0)} missing atlas inputs, "
        f"{texture_summary.get('missing_export_maps', 0)} missing exports",
        f"- Atlas handoff: {atlas_summary.get('ready', 0)} ready, "
        f"{atlas_summary.get('review', 0)} review",
        f"- SBS handoff: {sbs_summary.get('ready', 0)} ready, "
        f"{sbs_summary.get('review', 0)} review",
        "",
        "## P1 Decisions First",
        "",
    ]
    if p1_rows:
        _append_rows(lines, p1_rows, max_rows)
    else:
        lines.append("- No P1 review rows.")

    lines.extend([
        "",
        "## SK/M_ Remaining Blockers",
        "",
    ])
    blockers = prepare_summary.get("blockers", {})
    if blockers:
        for name, count in sorted(blockers.items(), key=lambda item: (-item[1], item[0])):
            lines.append(f"- `{name}`: {count}")
    else:
        lines.append("- No SK/M_ blockers reported.")

    lines.extend([
        "",
        "## Blend Status Only",
        "",
        "This section is only a dependency/status record. This tool does not launch the SPM repair or Blender repair workflow.",
        "",
        f"- Missing blend rows: {len(missing_blends)}",
        f"- Stale blend rows: {len(stale_blends)}",
        f"- Review blend rows: {len(blend_review)}",
    ])
    if stale_blends:
        lines.append("")
        lines.append("Stale blend rows:")
        for row in stale_blends[:max_rows]:
            lines.append(f"- `{row.get('folder_name', '')}/{row.get('mesh_name', '')}`: `{row.get('sk_spm', '')}`")
    if blend_review:
        lines.append("")
        lines.append("Blend rows needing ownership review:")
        for row in blend_review[:max_rows]:
            lines.append(f"- `{row.get('folder_name', '')}/{row.get('mesh_name', '')}`: {', '.join(row.get('blockers', []))}")

    lines.extend([
        "",
        "## Review Row Counts",
        "",
    ])
    lines.append(f"- By severity: {review_queue.get('summary', {}).get('by_severity', {})}")
    lines.append(f"- By issue type: {_count_by(review_items, 'issue_type')}")

    lines.extend([
        "",
        "## P2 Texture Work Sample",
        "",
    ])
    if p2_rows:
        _append_rows(lines, p2_rows, max_rows)
    else:
        lines.append("- No P2 review rows.")

    lines.extend([
        "",
        "## P3 Checks",
        "",
    ])
    if p3_rows:
        _append_rows(lines, p3_rows, max_rows)
    else:
        lines.append("- No P3 review rows.")

    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report")
    parser.add_argument("--md", dest="md_path")
    parser.add_argument("--max-rows", type=int, default=20)
    args = parser.parse_args()

    report_path = Path(args.report)
    report = json.loads(report_path.read_text(encoding="utf-8"))
    brief = build_review_brief_from_report(report, report_path, max_rows=args.max_rows)
    if args.md_path:
        path = Path(args.md_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(brief, encoding="utf-8")
    else:
        print(brief)


if __name__ == "__main__":
    main()
