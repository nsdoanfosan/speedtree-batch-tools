"""Export a texture/atlas/SBS work plan from a PCG texture audit report."""
import argparse
import csv
import gzip
import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from pcg_texture_audit import infer_normal_convention, unique


MAP_KEYWORDS = {
    "albedo": ("albedo", "basecolor", "base_color", "diffuse", "_color", "-color", "colour"),
    "alpha": ("alpha", "opacity", "transparency", "mask"),
    "normal": ("normal", "nor_gl", "nrm"),
    "height": ("height", "displacement", "depth"),
    "ao": ("ambientocclusion", "ambient_occlusion", "_ao", "-ao", "occlusion"),
    "roughness": ("roughness", "rough", "gloss"),
}

MAX_SCAN_BYTES = 8 * 1024 * 1024
MAX_REFS_PER_FILE = 80
IMAGE_SUFFIXES = (".png", ".tga", ".tif", ".tiff", ".jpg", ".jpeg", ".exr", ".bmp")
TOKEN_SEPARATORS = b"\x00\r\n\t <>'\"=;,()[]{}"


def fast_extract_image_refs(path):
    path = Path(path)
    try:
        with path.open("rb") as handle:
            magic = handle.read(2)
            handle.seek(0)
            if magic == b"\x1f\x8b":
                with gzip.open(handle, "rb") as gzip_handle:
                    data = gzip_handle.read(MAX_SCAN_BYTES)
            else:
                data = handle.read(MAX_SCAN_BYTES)
    except Exception:
        return []
    for sep in TOKEN_SEPARATORS:
        data = data.replace(bytes([sep]), b" ")
    refs = []
    for token in data.split():
        if len(token) > 300:
            continue
        try:
            value = token.decode("utf-8", errors="ignore").strip()
        except Exception:
            continue
        low = value.lower()
        if not low.endswith(IMAGE_SUFFIXES):
            continue
        looks_like_path = "\\" in value or "/" in value or ":" in value
        looks_like_named_map = any(keyword in low for keyword in (
            "albedo", "basecolor", "base_color", "opacity", "alpha",
            "normal", "height", "depth", "ambientocclusion", "_ao",
        ))
        if not looks_like_path and not looks_like_named_map:
            continue
        refs.append(value.replace("/", "\\"))
        if len(refs) >= MAX_REFS_PER_FILE:
            return unique(refs)
    return unique(refs)


def classify_ref(ref):
    low = str(ref).replace("\\", "/").lower()
    stem = Path(low).stem
    result = []
    for kind, keywords in MAP_KEYWORDS.items():
        if any(keyword in low or keyword in stem for keyword in keywords):
            result.append(kind)
    return result or ["unknown"]


def bucket_refs(refs):
    buckets = {kind: [] for kind in list(MAP_KEYWORDS) + ["unknown"]}
    for ref in unique(refs):
        for kind in classify_ref(ref):
            buckets.setdefault(kind, []).append(ref)
    return buckets


def cached_image_refs(path, ref_cache):
    key = str(path)
    if key not in ref_cache:
        ref_cache[key] = fast_extract_image_refs(path)
    return ref_cache[key]


def extract_material_image_refs(path, material_names):
    """Stream an SPM XML and return TexFilename refs for the requested materials only."""
    path = Path(path)
    wanted = {str(name).lower() for name in material_names if name}
    if not wanted or not path.exists():
        return {}
    found = {}
    handle = None
    try:
        with path.open("rb") as probe:
            is_gzip = probe.read(2) == b"\x1f\x8b"
        handle = gzip.open(path, "rb") if is_gzip else path.open("rb")
        current_name = None
        current_refs = []
        for event, element in ET.iterparse(handle, events=("start", "end")):
            if event == "start" and element.tag == "Material_v8":
                current_name = element.attrib.get("Name", "")
                current_refs = []
                continue
            if event != "end":
                continue
            if current_name is not None and element.tag == "TexFilename":
                value = (element.text or "").strip()
                if value and value.lower().endswith(IMAGE_SUFFIXES):
                    current_refs.append(value.replace("/", "\\"))
            if element.tag == "Material_v8":
                low = current_name.lower() if current_name else ""
                if low in wanted:
                    found[low] = unique(current_refs)
                current_name = None
                current_refs = []
                element.clear()
                if wanted.issubset(found):
                    break
            elif current_name is None:
                element.clear()
    except Exception:
        return {}
    finally:
        if handle is not None:
            handle.close()
    return found


def collect_item_fallback_refs(item, ref_cache):
    fallback_refs = []
    fallback_paths = []
    if item.get("chosen_spm"):
        fallback_paths.append(item["chosen_spm"])
    fallback_paths.extend(item.get("sbs_files", []))
    # Keep this tight. Source/SK variant folders can contain many large SPMs;
    # chosen_spm and SBS usually carry the useful fallback references.
    for path in unique(fallback_paths):
        fallback_refs.extend(cached_image_refs(path, ref_cache))
    return unique(fallback_refs)


def collect_cluster_refs(item, cluster, item_fallback_refs, ref_cache, material_ref_map=None):
    if cluster.get("source") == "material" and material_ref_map:
        material_refs = []
        for name in cluster.get("material_names") or []:
            material_refs.extend(material_ref_map.get(str(name).lower(), []))
        if material_refs:
            return unique(material_refs), [], "material_spm"
    cluster_refs = cluster.get("source_refs") or []
    if not cluster_refs and cluster.get("cluster_spm"):
        cluster_refs = cached_image_refs(cluster["cluster_spm"], ref_cache)
    if cluster_refs:
        return unique(cluster_refs), item_fallback_refs, "cluster_spm"
    # cluster_spm이 없는 항목(머티리얼 기반 아틀라스)은 폴더 폴백 참조를 쓴다.
    return [], item_fallback_refs, "folder_fallback"


def atlas_quality_for_folder(folder_name):
    return "low" if str(folder_name).lower().startswith("tree") else "review"


def input_status(buckets):
    missing = []
    if not buckets.get("albedo"):
        missing.append("albedo")
    if not buckets.get("alpha"):
        missing.append("alpha")
    if missing:
        return "missing " + ",".join(missing)
    return "ready"


def sbs_status(item, buckets):
    missing = []
    if not item.get("sbs_files"):
        missing.append("sbs")
    if not any(buckets.get(kind) for kind in ("albedo", "normal", "height", "ao")):
        missing.append("source_refs")
    if missing:
        return "missing " + ",".join(missing)
    return "ready"


def build_texture_plan_from_report(report, source_report="<memory>"):
    rows = []
    folder_notes = []
    ref_cache = {}
    for item in report.get("items", []):
        clusters = item.get("cluster_items", [])
        if not clusters:
            folder_notes.append({
                "folder_name": item.get("name", ""),
                "folder": item.get("folder", ""),
                "status": item.get("status", ""),
                "reason": "no relevant cluster SPMs detected",
                "actions": item.get("actions", []),
            })
            continue
        item_fallback_refs = None
        requested_by_spm = {}
        for cluster in clusters:
            if cluster.get("source") != "material":
                continue
            material_spms = cluster.get("material_spms") or (
                [item["chosen_spm"]] if item.get("chosen_spm") else [])
            for spm in material_spms:
                requested_by_spm.setdefault(spm, []).extend(cluster.get("material_names") or [])
        material_ref_map = {}
        for spm, names in requested_by_spm.items():
            for name, refs in extract_material_image_refs(spm, names).items():
                material_ref_map[name] = unique(material_ref_map.get(name, []) + refs)
        for cluster in clusters:
            cluster_refs, _fallback_refs, source_scope = collect_cluster_refs(
                item, cluster, [], ref_cache, material_ref_map=material_ref_map)
            if cluster_refs:
                fallback_refs = []
            else:
                if item_fallback_refs is None:
                    item_fallback_refs = collect_item_fallback_refs(item, ref_cache)
                fallback_refs = item_fallback_refs
                source_scope = "folder_fallback"
            refs = cluster_refs or fallback_refs
            buckets = bucket_refs(refs)
            normal_convention = infer_normal_convention(refs) if refs else item.get("normal_convention", "unknown")
            missing_maps = cluster.get("missing_export_maps", [])
            duplicate_targets = item.get("duplicate_pcg_target_mesh_names", [])
            rows.append({
                "folder_name": item.get("name", ""),
                "folder": item.get("folder", ""),
                "folder_status": item.get("status", ""),
                "pcg_target_meshes": item.get("pcg_target_mesh_names", []),
                "duplicate_pcg_target_meshes": duplicate_targets,
                "cluster_name": cluster.get("name", ""),
                "cluster_spm": cluster.get("cluster_spm", ""),
                "source": cluster.get("source", "cluster"),
                "material_names": cluster.get("material_names", []),
                "material_spms": cluster.get("material_spms", []),
                "needs_leaf_mesh": cluster.get("needs_leaf_mesh", True),
                "shared_from": cluster.get("shared_from"),
                "m_graph": cluster.get("m_graph"),
                "m_graph_sbs": cluster.get("m_graph_sbs"),
                "atlas_base": cluster.get("atlas_base", ""),
                "atlas_blends": cluster.get("atlas_blends", []),
                "atlas_blend_status": "ok" if cluster.get("atlas_blends") else "missing",
                "atlas_generator_quality": atlas_quality_for_folder(item.get("name", "")),
                "atlas_generator_plate_mode": "one plate",
                "atlas_generator_input_status": input_status(buckets),
                "sbs_files": item.get("sbs_files", []),
                "sbs_status": sbs_status(item, buckets),
                "texture_dir": cluster.get("texture_dir") or item.get("texture_dir", ""),
                "export_maps": cluster.get("export_maps", {}),
                "missing_export_maps": missing_maps,
                "export_status": "ok" if not missing_maps else "missing " + ",".join(missing_maps),
                "normal_convention": normal_convention,
                "normal_opengl": True if normal_convention == "OpenGL" else (False if normal_convention == "DirectX" else None),
                "ao_policy": item.get("ao_policy", ""),
                "sdf_policy": item.get("sdf_policy", ""),
                "source_scope": source_scope,
                "source_refs": refs,
                "source_albedo": buckets.get("albedo", []),
                "source_alpha": buckets.get("alpha", []),
                "source_normal": buckets.get("normal", []),
                "source_height": buckets.get("height", []),
                "source_ao": buckets.get("ao", []),
                "source_roughness": buckets.get("roughness", []),
                "unknown_source_refs": buckets.get("unknown", []),
                "actions": item.get("actions", []),
            })
    counts = {
        "clusters": len(rows),
        "missing_atlas_blend": sum(1 for row in rows if row["atlas_blend_status"] != "ok"),
        "missing_export_maps": sum(1 for row in rows if row["missing_export_maps"]),
        "missing_atlas_inputs": sum(1 for row in rows if row["atlas_generator_input_status"] != "ready"),
        "missing_sbs_inputs": sum(1 for row in rows if row["sbs_status"] != "ready"),
        "folder_notes": len(folder_notes),
    }
    return {
        "source_report": str(source_report),
        "pcg_targets": report.get("pcg_targets", {}),
        "summary": counts,
        "items": rows,
        "folder_notes": folder_notes,
    }


def build_texture_plan(report_path):
    report = json.loads(Path(report_path).read_text(encoding="utf-8"))
    return build_texture_plan_from_report(report, report_path)


def join_list(values, limit=8):
    values = values or []
    shown = [str(value) for value in values[:limit]]
    if len(values) > limit:
        shown.append(f"...(+{len(values) - limit})")
    return "; ".join(shown)


def write_csv(plan, csv_path):
    fields = [
        "folder_name",
        "folder_status",
        "pcg_target_meshes",
        "duplicate_pcg_target_meshes",
        "cluster_name",
        "atlas_base",
        "atlas_blend_status",
        "atlas_blends",
        "atlas_generator_quality",
        "atlas_generator_plate_mode",
        "atlas_generator_input_status",
        "sbs_status",
        "sbs_files",
        "export_status",
        "missing_export_maps",
        "normal_convention",
        "normal_opengl",
        "source_scope",
        "source_albedo",
        "source_alpha",
        "source_normal",
        "source_height",
        "source_ao",
        "texture_dir",
        "cluster_spm",
        "folder",
        "actions",
    ]
    rows = []
    for item in plan.get("items", []):
        rows.append({
            "folder_name": item.get("folder_name", ""),
            "folder_status": item.get("folder_status", ""),
            "pcg_target_meshes": join_list(item.get("pcg_target_meshes", []), 20),
            "duplicate_pcg_target_meshes": join_list(item.get("duplicate_pcg_target_meshes", []), 20),
            "cluster_name": item.get("cluster_name", ""),
            "atlas_base": item.get("atlas_base", ""),
            "atlas_blend_status": item.get("atlas_blend_status", ""),
            "atlas_blends": join_list(item.get("atlas_blends", []), 4),
            "atlas_generator_quality": item.get("atlas_generator_quality", ""),
            "atlas_generator_plate_mode": item.get("atlas_generator_plate_mode", ""),
            "atlas_generator_input_status": item.get("atlas_generator_input_status", ""),
            "sbs_status": item.get("sbs_status", ""),
            "sbs_files": join_list(item.get("sbs_files", []), 4),
            "export_status": item.get("export_status", ""),
            "missing_export_maps": join_list(item.get("missing_export_maps", []), 10),
            "normal_convention": item.get("normal_convention", ""),
            "normal_opengl": item.get("normal_opengl", ""),
            "source_scope": item.get("source_scope", ""),
            "source_albedo": join_list(item.get("source_albedo", []), 6),
            "source_alpha": join_list(item.get("source_alpha", []), 6),
            "source_normal": join_list(item.get("source_normal", []), 6),
            "source_height": join_list(item.get("source_height", []), 6),
            "source_ao": join_list(item.get("source_ao", []), 6),
            "texture_dir": item.get("texture_dir", ""),
            "cluster_spm": item.get("cluster_spm", ""),
            "folder": item.get("folder", ""),
            "actions": join_list(item.get("actions", []), 8),
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
    plan = build_texture_plan(args.report)
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
