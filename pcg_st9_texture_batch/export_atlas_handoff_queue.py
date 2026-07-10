"""Export a Blender Atlas Leaf Mesh Builder handoff queue."""
import argparse
import csv
import json
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from export_texture_plan import build_texture_plan_from_report, join_list


def normalize_key(value):
    return str(value or "").lower()


def planned_sk_path(source_spm):
    if not source_spm:
        return ""
    source = Path(source_spm)
    return str(source.with_name(f"SK_{source.name}"))


def target_spm_paths_for_item(item):
    paths = []
    for status in item.get("target_spm_statuses", []):
        if status.get("sk_spm"):
            paths.append(status["sk_spm"])
        elif status.get("source_spm"):
            paths.append(planned_sk_path(status["source_spm"]))
    seen = set()
    unique_paths = []
    for path in paths:
        key = normalize_key(path)
        if path and key not in seen:
            seen.add(key)
            unique_paths.append(path)
    return unique_paths


def target_source_status(item):
    statuses = {entry.get("status") for entry in item.get("target_spm_statuses", [])}
    if "needs_source_review" in statuses:
        return "needs_source_review"
    if "needs_sk" in statuses:
        return "needs_sk_first"
    if "needs_m_prefix" in statuses:
        return "needs_m_prefix_first"
    return "ready"


def proposed_blend_path(texture_row, cfg):
    atlas_base = texture_row.get("atlas_base", "")
    if not atlas_base:
        return ""
    atlas_root = Path(cfg.get("atlas_root", ""))
    if atlas_root:
        return str(atlas_root / f"{atlas_base}.blend")
    folder = texture_row.get("folder", "")
    return str(Path(folder) / f"{atlas_base}.blend") if folder else f"{atlas_base}.blend"


def first(values):
    return values[0] if values else ""


def build_atlas_queue_from_report(report, source_report="<memory>"):
    texture_plan = build_texture_plan_from_report(report, source_report)
    items_by_folder = {item.get("name"): item for item in report.get("items", [])}
    rows = []
    for texture_row in texture_plan.get("items", []):
        folder_name = texture_row.get("folder_name", "")
        audit_item = items_by_folder.get(folder_name, {})
        target_spms = target_spm_paths_for_item(audit_item)
        missing = []
        if not texture_row.get("source_albedo"):
            missing.append("albedo")
        if not texture_row.get("source_alpha"):
            missing.append("alpha")
        if not target_spms:
            missing.append("target_spm")
        if audit_item.get("duplicate_pcg_target_mesh_names"):
            missing.append("duplicate_target_review")
        source_status = target_source_status(audit_item)
        if source_status == "needs_source_review":
            missing.append("source_review")
        elif source_status in {"needs_sk_first", "needs_m_prefix_first"}:
            missing.append(source_status)
        atlas_blends = texture_row.get("atlas_blends", [])
        status = "ready" if not missing and atlas_blends else "review"
        rows.append({
            "folder_name": folder_name,
            "folder": texture_row.get("folder", ""),
            "cluster_name": texture_row.get("cluster_name", ""),
            "cluster_spm": texture_row.get("cluster_spm", ""),
            "pcg_target_meshes": texture_row.get("pcg_target_meshes", []),
            "duplicate_pcg_target_meshes": texture_row.get("duplicate_pcg_target_meshes", []),
            "material_name": texture_row.get("atlas_base", ""),
            "atlas_blend": first(atlas_blends),
            "proposed_atlas_blend": first(atlas_blends) or proposed_blend_path(texture_row, report.get("config", {})),
            "albedo_path": first(texture_row.get("source_albedo", [])),
            "alpha_path": first(texture_row.get("source_alpha", [])),
            "albedo_candidates": texture_row.get("source_albedo", []),
            "alpha_candidates": texture_row.get("source_alpha", []),
            "quality": "SPEEDTREE_LOW" if folder_name.lower().startswith("tree") else "REVIEW",
            "plate_mode": "SINGLE",
            "plate_mode_label": "One Plate",
            "spm_to_add": first(target_spms),
            "target_spms": target_spms,
            "target_source_status": source_status,
            "handoff_status": status,
            "missing": missing,
            "notes": [
                "Use Atlas Leaf Mesh Builder > Generate Leaf Meshes, then Build/Update Target SPMs.",
                "Set Material Name to material_name.",
            ],
        })
    counts = {
        "rows": len(rows),
        "ready": sum(1 for row in rows if row["handoff_status"] == "ready"),
        "review": sum(1 for row in rows if row["handoff_status"] != "ready"),
        "missing_albedo": sum(1 for row in rows if "albedo" in row["missing"]),
        "missing_alpha": sum(1 for row in rows if "alpha" in row["missing"]),
        "missing_target_spm": sum(1 for row in rows if "target_spm" in row["missing"]),
        "duplicate_target_review": sum(1 for row in rows if "duplicate_target_review" in row["missing"]),
    }
    return {
        "source_report": str(source_report),
        "pcg_targets": report.get("pcg_targets", {}),
        "summary": counts,
        "items": rows,
    }


def build_atlas_queue(report_path):
    report = json.loads(Path(report_path).read_text(encoding="utf-8"))
    return build_atlas_queue_from_report(report, report_path)


def write_csv(queue, csv_path):
    fields = [
        "handoff_status",
        "folder_name",
        "cluster_name",
        "material_name",
        "atlas_blend",
        "proposed_atlas_blend",
        "albedo_path",
        "alpha_path",
        "quality",
        "plate_mode",
        "plate_mode_label",
        "spm_to_add",
        "target_spms",
        "target_source_status",
        "missing",
        "pcg_target_meshes",
        "duplicate_pcg_target_meshes",
        "cluster_spm",
        "folder",
    ]
    rows = []
    for item in queue.get("items", []):
        rows.append({
            "handoff_status": item.get("handoff_status", ""),
            "folder_name": item.get("folder_name", ""),
            "cluster_name": item.get("cluster_name", ""),
            "material_name": item.get("material_name", ""),
            "atlas_blend": item.get("atlas_blend", ""),
            "proposed_atlas_blend": item.get("proposed_atlas_blend", ""),
            "albedo_path": item.get("albedo_path", ""),
            "alpha_path": item.get("alpha_path", ""),
            "quality": item.get("quality", ""),
            "plate_mode": item.get("plate_mode", ""),
            "plate_mode_label": item.get("plate_mode_label", ""),
            "spm_to_add": item.get("spm_to_add", ""),
            "target_spms": join_list(item.get("target_spms", []), 20),
            "target_source_status": item.get("target_source_status", ""),
            "missing": join_list(item.get("missing", []), 20),
            "pcg_target_meshes": join_list(item.get("pcg_target_meshes", []), 20),
            "duplicate_pcg_target_meshes": join_list(item.get("duplicate_pcg_target_meshes", []), 20),
            "cluster_spm": item.get("cluster_spm", ""),
            "folder": item.get("folder", ""),
        })
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
    queue = build_atlas_queue(args.report)
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
