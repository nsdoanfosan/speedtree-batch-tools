"""Export a Substance Designer SBS handoff queue for PCG ST9 texture work."""
import argparse
import csv
import json
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from export_texture_plan import build_texture_plan_from_report, join_list

REQUIRED_EXPORTS = ["color", "normal", "extra", "height", "opacity"]


def first(values):
    return values[0] if values else ""


def normal_opengl_value(convention):
    if convention == "OpenGL":
        return True
    if convention == "DirectX":
        return False
    return None


def sbs_status_for_row(row):
    missing = []
    if not row.get("sbs_files"):
        missing.append("sbs")
    if not row.get("source_albedo"):
        missing.append("albedo")
    if not row.get("source_normal"):
        missing.append("normal")
    if not row.get("source_height"):
        missing.append("height")
    if row.get("normal_convention") == "unknown":
        missing.append("normal_convention")
    if row.get("duplicate_pcg_target_meshes"):
        missing.append("duplicate_target_review")
    status = "ready" if not missing else "review"
    return status, missing


def build_sbs_queue_from_report(report, source_report="<memory>"):
    texture_plan = build_texture_plan_from_report(report, source_report)
    rows = []
    for row in texture_plan.get("items", []):
        handoff_status, missing = sbs_status_for_row(row)
        export_maps = row.get("export_maps", {})
        rows.append({
            "folder_name": row.get("folder_name", ""),
            "folder": row.get("folder", ""),
            "cluster_name": row.get("cluster_name", ""),
            "cluster_spm": row.get("cluster_spm", ""),
            "graph_or_material_name": row.get("atlas_base", ""),
            "sbs_file": first(row.get("sbs_files", [])),
            "sbs_files": row.get("sbs_files", []),
            "texture_dir": row.get("texture_dir", ""),
            "pcg_target_meshes": row.get("pcg_target_meshes", []),
            "duplicate_pcg_target_meshes": row.get("duplicate_pcg_target_meshes", []),
            "source_albedo": first(row.get("source_albedo", [])),
            "source_normal": first(row.get("source_normal", [])),
            "source_height": first(row.get("source_height", [])),
            "source_ao": first(row.get("source_ao", [])),
            "source_opacity": first(row.get("source_alpha", [])),
            "source_roughness": first(row.get("source_roughness", [])),
            "albedo_candidates": row.get("source_albedo", []),
            "normal_candidates": row.get("source_normal", []),
            "height_candidates": row.get("source_height", []),
            "ao_candidates": row.get("source_ao", []),
            "opacity_candidates": row.get("source_alpha", []),
            "base_color_policy": "leave default 0",
            "ao_policy": "use source AO if present; otherwise derive HBAO from height",
            "normal_convention": row.get("normal_convention", "unknown"),
            "normal_opengl": normal_opengl_value(row.get("normal_convention", "unknown")),
            "sdf_policy": "connect opacity if needed, set SDF to 0",
            "sdf_value": 0,
            "required_exports": REQUIRED_EXPORTS,
            "export_maps": export_maps,
            "missing_export_maps": row.get("missing_export_maps", []),
            "export_status": "ok" if not row.get("missing_export_maps") else "missing " + ",".join(row.get("missing_export_maps", [])),
            "handoff_status": handoff_status,
            "missing": missing,
            "notes": [
                "Open the SBS and connect sources to the cluster_system_01-style graph/output.",
                "Export color, normal, extra, height, opacity into the SBS texture folder.",
            ],
        })
    counts = {
        "rows": len(rows),
        "ready": sum(1 for row in rows if row["handoff_status"] == "ready"),
        "review": sum(1 for row in rows if row["handoff_status"] != "ready"),
        "missing_sbs": sum(1 for row in rows if "sbs" in row["missing"]),
        "missing_albedo": sum(1 for row in rows if "albedo" in row["missing"]),
        "missing_normal": sum(1 for row in rows if "normal" in row["missing"]),
        "missing_height": sum(1 for row in rows if "height" in row["missing"]),
        "unknown_normal_convention": sum(1 for row in rows if "normal_convention" in row["missing"]),
        "missing_exports": sum(1 for row in rows if row["missing_export_maps"]),
    }
    return {
        "source_report": str(source_report),
        "pcg_targets": report.get("pcg_targets", {}),
        "summary": counts,
        "items": rows,
    }


def build_sbs_queue(report_path):
    report = json.loads(Path(report_path).read_text(encoding="utf-8"))
    return build_sbs_queue_from_report(report, report_path)


def write_csv(queue, csv_path):
    fields = [
        "handoff_status",
        "folder_name",
        "cluster_name",
        "graph_or_material_name",
        "sbs_file",
        "source_albedo",
        "source_normal",
        "source_height",
        "source_ao",
        "source_opacity",
        "normal_convention",
        "normal_opengl",
        "base_color_policy",
        "ao_policy",
        "sdf_policy",
        "sdf_value",
        "required_exports",
        "export_status",
        "missing_export_maps",
        "missing",
        "pcg_target_meshes",
        "duplicate_pcg_target_meshes",
        "texture_dir",
        "cluster_spm",
        "folder",
    ]
    rows = []
    for item in queue.get("items", []):
        rows.append({
            "handoff_status": item.get("handoff_status", ""),
            "folder_name": item.get("folder_name", ""),
            "cluster_name": item.get("cluster_name", ""),
            "graph_or_material_name": item.get("graph_or_material_name", ""),
            "sbs_file": item.get("sbs_file", ""),
            "source_albedo": item.get("source_albedo", ""),
            "source_normal": item.get("source_normal", ""),
            "source_height": item.get("source_height", ""),
            "source_ao": item.get("source_ao", ""),
            "source_opacity": item.get("source_opacity", ""),
            "normal_convention": item.get("normal_convention", ""),
            "normal_opengl": item.get("normal_opengl", ""),
            "base_color_policy": item.get("base_color_policy", ""),
            "ao_policy": item.get("ao_policy", ""),
            "sdf_policy": item.get("sdf_policy", ""),
            "sdf_value": item.get("sdf_value", ""),
            "required_exports": join_list(item.get("required_exports", []), 10),
            "export_status": item.get("export_status", ""),
            "missing_export_maps": join_list(item.get("missing_export_maps", []), 10),
            "missing": join_list(item.get("missing", []), 20),
            "pcg_target_meshes": join_list(item.get("pcg_target_meshes", []), 20),
            "duplicate_pcg_target_meshes": join_list(item.get("duplicate_pcg_target_meshes", []), 20),
            "texture_dir": item.get("texture_dir", ""),
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
    queue = build_sbs_queue(args.report)
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
