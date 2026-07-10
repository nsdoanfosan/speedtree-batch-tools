"""Seed pcg_targets.json from an existing PCG display/audit JSON report.

This is an offline fallback for when Unreal Editor is closed. It is weaker than
refresh_pcg_targets.py because it depends on a saved report, but it lets the
status board focus on the known PCG_01 candidates immediately.
"""
import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from pcg_texture_common import TARGETS_PATH


def graph_matches(graph, needle):
    needle = needle.lower()
    label = str(graph.get("label", "")).lower()
    path = str(graph.get("graph", "")).lower()
    return needle in label or needle in path


def import_report(source_path, out_path, graph_filter):
    data = json.loads(Path(source_path).read_text(encoding="utf-8"))
    meshes = {}
    graph_labels = []
    for graph in data.get("graphs", []):
        if graph_filter and not graph_matches(graph, graph_filter):
            continue
        graph_labels.append({"label": graph.get("label"), "graph": graph.get("graph")})
        for asset in graph.get("assets", []):
            path = asset.get("path")
            if not path or "/Game/Meshes/Tree/st9/" not in path:
                continue
            meshes.setdefault(path, {
                "static_mesh": path,
                "sections": [],
                "data_assets": [],
                "source_graphs": [],
            })
            meshes[path]["source_graphs"].append(graph.get("label") or graph.get("graph"))
    result = {
        "graph": graph_filter,
        "source": "imported_pcg_display_report",
        "source_file": str(source_path),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "graph_matches": graph_labels,
        "data_assets": [],
        "meshes": sorted(meshes.values(), key=lambda item: item["static_mesh"].lower()),
        "errors": [],
    }
    Path(out_path).write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source",
        default=r"C:\UnrealProjects\MyProject2\work\create_pcg_data_display_level_result.json",
    )
    parser.add_argument("--out", default=str(TARGETS_PATH))
    parser.add_argument("--graph-filter", default="PCG_01")
    args = parser.parse_args()
    result = import_report(args.source, args.out, args.graph_filter)
    print(json.dumps({
        "out": args.out,
        "source": args.source,
        "graph_filter": args.graph_filter,
        "graph_matches": len(result["graph_matches"]),
        "meshes": len(result["meshes"]),
    }, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
