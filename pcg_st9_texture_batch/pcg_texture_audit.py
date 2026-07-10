"""Audit PCG/ST9 SpeedTree texture preparation state.

This is intentionally conservative. It reads filesystem/SPM/SBS evidence and
only mutates files when --prepare-sk is passed.
"""
import argparse
import csv
import gzip
import html
import json
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from pcg_texture_common import (
    IMAGE_EXTS,
    REPORT_DIR,
    is_backup_path,
    json_safe_path,
    load_config,
    load_pcg_targets,
)

MATERIAL_RE = re.compile(r'(<Material_v8\b[^>]*?Name=")([^"]*)(")')
MATERIAL_BLOCK_RE = re.compile(
    r"<Material_v8\b[^>]*>.*?</Material_v8>", re.IGNORECASE | re.DOTALL)
TEX_FILENAME_RE = re.compile(
    r"<TexFilename\b[^>]*>(.*?)</TexFilename>", re.IGNORECASE | re.DOTALL)
ABS_IMAGE_RE = re.compile(
    r"[A-Za-z]:[\\/][^<>'\"\r\n]+?\.(?:png|tga|tif|tiff|jpg|jpeg|exr|bmp)",
    re.IGNORECASE,
)
TOKEN_SPLIT_RE = re.compile(r"[\s<>'\"=;,\(\)\[\]\{\}]+")
LEAF_SOURCE_WORDS = ("leaf", "leaves", "foliage", "needle")
ALBEDO_WORDS = ("albedo", "basecolor", "base_color", "diffuse", "colour", "_color", "-color")
ALPHA_WORDS = ("alpha", "opacity", "transparency", "mask")
NON_COLOR_WORDS = (
    "normal", "rough", "gloss", "height", "displacement", "depth",
    "ambientocclusion", "ambient_occlusion", "occlusion", "_ao", "-ao",
    "subsurface", "translucency", "vertex", "metallic",
)
SOURCE_MAP_SUFFIX_RE = re.compile(
    r"(?:[_-](?:base[_-]?color|basecolor|albedo|diffuse|colour|color|opacity|alpha|"
    r"transparency|mask|normal|roughness|rough|gloss|height|displacement|depth|"
    r"ambient[_-]?occlusion|ao|occlusion|translucency|subsurface))$",
    re.IGNORECASE,
)
SOURCE_RESOLUTION_SUFFIX_RE = re.compile(
    r"(?:[_-](?:1k|2k|4k|8k|16k|\d+x\d+))$", re.IGNORECASE)
_MATERIAL_IMAGE_REF_CACHE = {}


def read_maybe_gzip_text(path):
    path = Path(path)
    try:
        with gzip.open(path, "rb") as handle:
            return handle.read().decode("utf-8", errors="replace")
    except Exception:
        try:
            return path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            return ""


def unique(seq):
    out = []
    seen = set()
    for item in seq:
        key = str(item).lower()
        if key not in seen:
            seen.add(key)
            out.append(item)
    return out


def root_spms(folder):
    return sorted(
        p for p in Path(folder).glob("*.spm")
        if p.is_file() and not is_backup_path(p)
    )


def preferred_sk_spms(folder):
    return [p for p in root_spms(folder) if p.name.lower().startswith("sk_")]


def loose_sk_spms(folder):
    return [p for p in root_spms(folder) if p.name.lower().startswith("sk")]


def source_spms(folder):
    return [p for p in root_spms(folder) if not p.name.lower().startswith("sk")]


def blend_for_spm(spm):
    blend = Path(spm).with_suffix(".blend")
    return blend if blend.exists() else None


def extract_material_names(spm):
    text = read_maybe_gzip_text(spm)
    return unique([m.group(2) for m in MATERIAL_RE.finditer(text)])


def extract_image_refs(path):
    text = read_maybe_gzip_text(path)
    refs = []
    refs.extend(match.group(0).replace("/", "\\") for match in ABS_IMAGE_RE.finditer(text))
    for token in TOKEN_SPLIT_RE.split(text):
        if not token or len(token) > 300:
            continue
        token = token.strip()
        if Path(token).suffix.lower() in IMAGE_EXTS:
            refs.append(token.replace("/", "\\"))
    return unique(refs)


def extract_material_image_refs(path):
    """Return material names and only the image refs owned by each material."""
    path = Path(path)
    try:
        stat = path.stat()
        cache_key = (str(path).lower(), stat.st_size, stat.st_mtime_ns)
    except OSError:
        cache_key = (str(path).lower(), 0, 0)
    cached = _MATERIAL_IMAGE_REF_CACHE.get(cache_key)
    if cached is not None:
        return cached
    text = read_maybe_gzip_text(path)
    if not text:
        return []
    rows = []
    for block_match in MATERIAL_BLOCK_RE.finditer(text):
        block = block_match.group(0)
        name_match = MATERIAL_RE.search(block)
        name = html.unescape(name_match.group(2)) if name_match else ""
        refs = []
        for ref_match in TEX_FILENAME_RE.finditer(block):
            value = html.unescape(ref_match.group(1).strip())
            if value and Path(value).suffix.lower() in IMAGE_EXTS:
                refs.append(value.replace("/", "\\"))
        rows.append({"material_name": name, "refs": unique(refs)})
    _MATERIAL_IMAGE_REF_CACHE[cache_key] = rows
    return rows


def resolve_spm_image_ref(spm, ref):
    path = Path(str(ref).replace("/", "\\"))
    if not path.is_absolute():
        path = Path(spm).parent / path
    try:
        return path.resolve()
    except OSError:
        return path.absolute()


def source_family_name(path):
    """Stable display/output name shared by all maps in one source atlas set."""
    stem = Path(path).stem
    stem = SOURCE_MAP_SUFFIX_RE.sub("", stem)
    stem = SOURCE_RESOLUTION_SUFFIX_RE.sub("", stem)
    stem = SOURCE_MAP_SUFFIX_RE.sub("", stem)
    return stem.strip("_-") or Path(path).stem


def leaf_sources_from_spm(spm, source_kind, excluded_albedo_stems=None):
    """Find coherent leaf albedo/alpha pairs used by one SPM material.

    One material normally owns one atlas pair. If a material happens to list
    more maps, family-name matching keeps albedo and alpha in the same set.
    """
    results = []
    excluded_albedo_stems = {str(stem).lower() for stem in (excluded_albedo_stems or [])}
    for row in extract_material_image_refs(spm):
        name = row["material_name"]
        refs = row["refs"]
        searchable = " ".join([name] + refs).lower()
        if not any(word in searchable for word in LEAF_SOURCE_WORDS):
            continue
        albedo_refs = []
        alpha_refs = []
        for ref in refs:
            low = Path(ref).name.lower()
            if any(word in low for word in ALPHA_WORDS):
                alpha_refs.append(ref)
            elif any(word in low for word in ALBEDO_WORDS):
                albedo_refs.append(ref)
            elif not any(word in low for word in NON_COLOR_WORDS):
                # SpeedTree cluster colors commonly have no "albedo" suffix.
                albedo_refs.append(ref)
        albedo_refs = [
            ref for ref in albedo_refs
            if Path(ref).stem.lower() not in excluded_albedo_stems
        ]
        if not albedo_refs or not alpha_refs:
            continue
        albedos = [resolve_spm_image_ref(spm, ref) for ref in albedo_refs]
        alphas = [resolve_spm_image_ref(spm, ref) for ref in alpha_refs]
        albedos = [path for path in albedos if path.exists()]
        alphas = [path for path in alphas if path.exists()]
        if not albedos or not alphas:
            continue
        alpha_by_family = {}
        for path in alphas:
            alpha_by_family.setdefault(source_family_name(path).lower(), []).append(path)
        pairs = []
        for albedo in albedos:
            family = source_family_name(albedo)
            matches = alpha_by_family.get(family.lower(), [])
            if matches:
                pairs.append((albedo, matches[0], family))
        if not pairs and len(albedos) == 1 and len(alphas) == 1:
            pairs.append((albedos[0], alphas[0], source_family_name(albedos[0])))
        for albedo, alpha, family in pairs:
            safe_family = re.sub(r"[^A-Za-z0-9_-]+", "_", family).strip("_-") or "leaf"
            results.append({
                "albedo": str(albedo),
                "alpha": str(alpha),
                "source_family": family,
                "atlas_base": f"M_{safe_family}_atlas_01",
                "source_kind": source_kind,
                "target_spm": str(Path(spm)),
                "material_names": [name] if name else [],
            })
    return results


def merge_leaf_mesh_sources(sources, cfg, folder):
    """Deduplicate by resolved source atlas pair and merge every target SPM."""
    grouped = {}
    for source in sources:
        key = (
            str(Path(source["albedo"]).resolve()).lower(),
            str(Path(source["alpha"]).resolve()).lower(),
        )
        entry = grouped.get(key)
        target = {
            "spm": source["target_spm"],
            "material_names": list(source.get("material_names") or []),
            "source_kind": source.get("source_kind", "direct"),
        }
        if entry is None:
            entry = dict(source)
            entry["targets"] = [target]
            entry["source_kinds"] = [source.get("source_kind", "direct")]
            entry["atlas_blends"] = find_atlas_blends(
                cfg["atlas_root"], folder, source["atlas_base"])
            grouped[key] = entry
            continue
        if source.get("source_kind") not in entry["source_kinds"]:
            entry["source_kinds"].append(source.get("source_kind"))
        existing = next(
            (row for row in entry["targets"]
             if row["spm"].lower() == target["spm"].lower()), None)
        if existing:
            existing["material_names"] = unique(
                existing["material_names"] + target["material_names"])
        else:
            entry["targets"].append(target)
    return list(grouped.values())


def referenced_cluster_spms(target_spms, clusters):
    """Map final-tree cluster texture refs to the Cluster SPM that rendered them."""
    refs_by_stem = {}
    for spm in target_spms:
        for material in extract_material_image_refs(spm):
            for ref in material["refs"]:
                refs_by_stem.setdefault(Path(ref).stem.lower(), set()).add(str(spm))
    found = {}
    for cluster in clusters:
        users = refs_by_stem.get(cluster.stem.lower())
        if users:
            found[str(cluster).lower()] = sorted(users)
    return found


def discover_leaf_mesh_sources(folder, cfg, target_spms, clusters):
    """Discover direct leaf atlases plus leaf atlases inside referenced clusters."""
    referenced = referenced_cluster_spms(target_spms, clusters)
    referenced_stems = {Path(path).stem.lower() for path in referenced}
    sources = []
    for spm in target_spms:
        for source in leaf_sources_from_spm(
                spm, "direct", excluded_albedo_stems=referenced_stems):
            sources.append(source)
    for cluster in clusters:
        if str(cluster).lower() not in referenced:
            continue
        for source in leaf_sources_from_spm(cluster, "cluster"):
            source["referenced_by_spms"] = referenced[str(cluster).lower()]
            sources.append(source)
    return merge_leaf_mesh_sources(sources, cfg, folder), referenced


def patch_m_prefix(spm, exclude=None):
    spm = Path(spm)
    exclude = {str(name) for name in (exclude or [])}
    text = read_maybe_gzip_text(spm)
    existing = set(extract_material_names(spm))
    renames = {}
    for name in existing:
        if name in exclude:
            continue
        if name and not name.startswith("M_") and ("M_" + name) not in existing:
            renames[name] = "M_" + name
    if not renames:
        return {"spm": str(spm), "changed": False, "renames": []}

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    # Keep backups in a subfolder so the working SPM list stays clean.
    backup_dir = spm.parent / "_spm_backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup = backup_dir / f"{spm.stem}.pcgtex_backup_before_m_prefix_{ts}.spm"
    shutil.copy2(spm, backup)
    applied = []

    def sub(match):
        old = match.group(2)
        new = renames.get(old)
        if not new:
            return match.group(0)
        applied.append([old, new])
        return match.group(1) + new + match.group(3)

    patched = MATERIAL_RE.sub(sub, text)
    with gzip.open(spm, "wb") as handle:
        handle.write(patched.encode("utf-8"))
    return {"spm": str(spm), "changed": True, "backup": str(backup), "renames": applied}


def m_prefix_plan(spm, exclude=None):
    names = extract_material_names(spm)
    exclude = {str(name) for name in (exclude or [])}
    existing = set(names)
    renames = []
    for name in names:
        if name in exclude:
            continue
        if name and not name.startswith("M_") and ("M_" + name) not in existing:
            renames.append([name, "M_" + name])
    return renames


def spm_matches_mesh_name(spm_path, mesh_name):
    return normalize_local_asset_stem(Path(spm_path).stem) == str(mesh_name).lower()


def find_source_spm_for_mesh(folder, mesh_name):
    for spm in source_spms(folder):
        if spm_matches_mesh_name(spm, mesh_name):
            return spm
    return None


def find_sk_spm_for_mesh(folder, mesh_name):
    preferred = []
    loose = []
    for spm in preferred_sk_spms(folder):
        if spm_matches_mesh_name(spm, mesh_name):
            preferred.append(spm)
    for spm in loose_sk_spms(folder):
        if spm in preferred:
            continue
        if spm_matches_mesh_name(spm, mesh_name):
            loose.append(spm)
    if preferred:
        return sorted(unique(preferred), key=lambda p: str(p).lower())[0]
    if loose:
        return sorted(unique(loose), key=lambda p: str(p).lower())[0]
    return None


def target_spm_status(folder, mesh_name):
    folder = Path(folder)
    source = find_source_spm_for_mesh(folder, mesh_name)
    sk = find_sk_spm_for_mesh(folder, mesh_name)
    target = sk or source
    materials = extract_material_names(target) if target else []
    missing_m = [m for m in materials if m and not m.startswith("M_")]
    blend = blend_for_spm(sk) if sk else None
    status = "ready"
    actions = []
    if not source and not sk:
        status = "needs_source_review"
        actions.append("원본 SPM 확인 필요")
    elif not sk:
        status = "needs_sk"
        actions.append("SK SPM 생성 필요")
    elif missing_m:
        status = "needs_m_prefix"
        actions.append("머티리얼 이름 M_ 정리 필요")
    elif not blend:
        status = "needs_blend"
        actions.append("별도 리페어에서 SK Blend 생성 필요")
    elif blend.stat().st_mtime < sk.stat().st_mtime:
        status = "needs_blend_update"
        actions.append("별도 리페어에서 오래된 SK Blend 갱신 필요")
    return {
        "mesh_name": mesh_name,
        "source_spm": str(source) if source else None,
        "sk_spm": str(sk) if sk else None,
        "blend": str(blend) if blend else None,
        "blend_stale": bool(blend and sk and blend.stat().st_mtime < sk.stat().st_mtime),
        "materials_missing_m_prefix": missing_m,
        "status": status,
        "actions": actions,
    }


def prepare_sk(folder, target_mesh_names=None, dry_run=False, exclude_materials=None):
    folder = Path(folder)
    if target_mesh_names:
        results = []
        for mesh_name in sorted(set(target_mesh_names)):
            existing = find_sk_spm_for_mesh(folder, mesh_name)
            created = None
            if existing:
                target = existing
                would_create = None
            else:
                src = find_source_spm_for_mesh(folder, mesh_name)
                if not src:
                    results.append({
                        "mesh_name": mesh_name,
                        "status": "skipped",
                        "reason": "no matching source SPM",
                    })
                    continue
                target = folder / f"SK_{src.name}"
                would_create = str(target)
                if target.exists():
                    results.append({
                        "mesh_name": mesh_name,
                        "status": "skipped",
                        "reason": f"target already exists: {target}",
                    })
                    continue
                if not dry_run:
                    shutil.copy2(src, target)
                    created = str(target)
            patch = (
                {"dry_run": True, "renames": m_prefix_plan(target if existing or not dry_run else src, exclude=exclude_materials)}
                if dry_run else patch_m_prefix(target, exclude=exclude_materials)
            )
            results.append({
                "mesh_name": mesh_name,
                "status": "dry-run" if dry_run else "prepared",
                "sk_spm": str(target),
                "would_create": would_create if dry_run else None,
                "created": created,
                "patch": patch,
            })
        return {"folder": str(folder), "targets": results}

    preferred = preferred_sk_spms(folder)
    if preferred:
        target = preferred[0]
        created = None
        src = target
    else:
        sources = source_spms(folder)
        if not sources:
            raise RuntimeError(f"no source SPM in {folder}")
        src = sources[0]
        target = folder / f"SK_{src.name}"
        if target.exists():
            raise RuntimeError(f"target already exists: {target}")
        if not dry_run:
            shutil.copy2(src, target)
            created = str(target)
        else:
            created = None
    patch = (
        {"dry_run": True, "renames": m_prefix_plan(target if preferred else src, exclude=exclude_materials)}
        if dry_run else patch_m_prefix(target, exclude=exclude_materials)
    )
    return {
        "folder": str(folder),
        "sk_spm": str(target),
        "would_create": str(target) if dry_run and not preferred else None,
        "created": created,
        "patch": patch,
    }


def atlas_base_from_cluster_stem(stem):
    s = stem
    s = re.sub(r"[_-]0*\d+$", "", s)
    s = s.strip("_-")
    return f"M_{s}_atlas_01"


def file_exists_case_insensitive(folder, stem, suffixes):
    folder = Path(folder)
    if not folder.exists():
        return []
    suffixes = {s.lower() for s in suffixes}
    stem_lower = stem.lower()
    return sorted(
        p for p in folder.iterdir()
        if p.is_file()
        and p.suffix.lower() in suffixes
        and p.stem.lower() == stem_lower
        and not is_backup_path(p)
    )


def find_atlas_blends(atlas_root, folder, atlas_base):
    roots = [Path(atlas_root), Path(folder)]
    needles = [atlas_base.lower()]
    # Also allow existing files with small case differences such as M_Leaf_*.
    parts = [p for p in atlas_base.lower().split("_") if p not in {"m", "01"}]
    found = []
    for root in roots:
        if not root.exists():
            continue
        for path in root.glob("*.blend"):
            if is_backup_path(path):
                continue
            low = path.stem.lower()
            if low in needles or all(part in low for part in parts):
                found.append(path)
    return unique([str(p) for p in found])


def texture_dir_candidates(folder, sbs_files):
    """출력 텍스처가 있을 수 있는 폴더 후보들 (sbs 위치 + 관례 폴더)."""
    folder = Path(folder)
    dirs = [Path(p).parent for p in sbs_files]
    dirs += [folder / "texture", folder / "texture" / "substance", folder / "substance"]
    out = []
    seen = set()
    for d in dirs:
        key = str(d).lower()
        if key not in seen and Path(d).exists():
            seen.add(key)
            out.append(Path(d))
    return out


def find_export_maps_multi(dirs, atlas_base, required_maps):
    """여러 폴더 중 맵이 하나라도 있는 폴더의 결과를 쓴다. (폴더, 결과) 반환."""
    dirs = list(dirs)
    if not dirs:
        return None, {m: None for m in required_maps}
    for d in dirs:
        result = find_export_maps(d, atlas_base, required_maps)
        if any(result.values()):
            return d, result
    return dirs[0], find_export_maps(dirs[0], atlas_base, required_maps)


def find_export_maps(texture_dir, atlas_base, required_maps):
    texture_dir = Path(texture_dir)
    result = {}
    if not texture_dir.exists():
        return {m: None for m in required_maps}
    files = [p for p in texture_dir.iterdir() if p.is_file() and not is_backup_path(p)]
    low_files = {p.name.lower(): p for p in files}
    for map_name in required_maps:
        candidates = [
            f"{atlas_base}_{map_name}{ext}".lower()
            for ext in (".tga", ".png", ".tif", ".tiff", ".exr")
        ]
        hit = None
        for candidate in candidates:
            if candidate in low_files:
                hit = low_files[candidate]
                break
        if hit is None:
            prefix = f"{atlas_base}_{map_name}".lower()
            matches = [p for p in files if p.stem.lower() == prefix]
            hit = matches[0] if matches else None
        result[map_name] = str(hit) if hit else None
    return result


def active_sbs_files(folder):
    paths = []
    subdirs = (
        Path(folder) / "texture",
        Path(folder) / "texture" / "substance",
        Path(folder) / "substance",
        Path(folder),
    )
    for sub in subdirs:
        if sub.exists():
            paths.extend(p for p in sub.glob("*.sbs") if not is_backup_path(p))
    # 세트 파일(_set_) 우선, 그다음 짧은 이름 순.
    paths = [p for p in paths if ".autosave" not in str(p).lower()]
    return sorted(
        unique(paths),
        key=lambda p: (0 if "set" in p.stem.lower() else 1, len(p.name), p.name.lower()),
    )


def cluster_spms(folder):
    cluster_dir = Path(folder) / "Cluster"
    if not cluster_dir.exists():
        return []
    return sorted(p for p in cluster_dir.glob("*.spm") if p.is_file() and not is_backup_path(p))


# ---- SPM 머티리얼 이름에서 아틀라스 사용을 감지 (클러스터 SPM이 없는 폴더용) ----
# 예: SK_weed_anamone_01.spm 의 머티리얼 M_leaf_anamone_atlas_01 은
#     클러스터 없이 아틀라스를 직접 쓰는 잎 머티리얼이다.
ATLAS_NAME_RE = re.compile(r"^(.*_atlas_\d+)", re.IGNORECASE)
# 아틀라스 리프 제너레이터 Auto Split 그룹 접미사 (M_x_atlas_01_green 등)
AUTO_SPLIT_SUFFIXES = {
    "green", "green_light", "yellow", "dead", "flower", "bud",
    "stem", "twig", "cluster", "flower_leaf",
}
# 이름에 이 단어가 있으면 잎 지오메트리(② blend)가 필요한 아틀라스로 본다.
# bark/decal/stem 계열은 텍스처(③)만 추적한다.
LEAF_MESH_KEYWORDS = ("leaf", "cluster", "branch")


def atlas_blend_stems(cfg):
    root = Path(cfg.get("atlas_root", ""))
    if not root.exists():
        return {}
    return {
        p.stem.lower(): p
        for p in root.glob("*.blend")
        if p.is_file() and not is_backup_path(p)
    }


def folder_m_graph_names(sbs_files):
    """폴더의 sbs들에서 M_ 그래프 이름을 모은다. {lower: (원래이름, sbs경로)}"""
    from sbs_auto import list_m_graphs
    graphs = {}
    for sbs in sbs_files:
        for name in list_m_graphs(sbs):
            graphs.setdefault(name.lower(), (name, str(sbs)))
    return graphs


def material_atlas_base(name, blend_stems, graphs):
    """머티리얼 이름 → 아틀라스 베이스 이름 (아니면 None)."""
    low = str(name).lower()
    if low in blend_stems:
        return blend_stems[low].stem
    if low in graphs:
        return graphs[low][0]
    for stem_low, blend in blend_stems.items():
        if low.startswith(stem_low + "_") and low[len(stem_low) + 1:] in AUTO_SPLIT_SUFFIXES:
            return blend.stem
    for graph_low, (graph, _sbs) in graphs.items():
        if low.startswith(graph_low + "_") and low[len(graph_low) + 1:] in AUTO_SPLIT_SUFFIXES:
            return graph
    match = ATLAS_NAME_RE.match(str(name))
    if match:
        return match.group(1)
    return None


def material_atlas_items(folder, cfg, tex_dirs, existing_bases, graphs):
    spms = preferred_sk_spms(folder) or source_spms(folder)
    name_spm_pairs = []
    for spm in spms:
        name_spm_pairs.extend((name, spm) for name in extract_material_names(spm))
    blend_stems = atlas_blend_stems(cfg)
    items = []
    by_base = {}
    seen = {b.lower() for b in existing_bases}
    for name, spm in name_spm_pairs:
        base = material_atlas_base(name, blend_stems, graphs)
        if not base:
            continue
        key = base.lower()
        if key in by_base:
            if name not in by_base[key]["material_names"]:
                by_base[key]["material_names"].append(name)
            spm_text = str(spm)
            if spm_text not in by_base[key]["material_spms"]:
                by_base[key]["material_spms"].append(spm_text)
            continue
        if key in seen:
            continue
        seen.add(key)
        blend = blend_stems.get(key)
        maps_dir, export_maps = find_export_maps_multi(tex_dirs, base, cfg["required_export_maps"])
        graph = graphs.get(key)
        entry = {
            "cluster_spm": None,
            "name": base,
            "source": "material",
            "material_names": [name],
            "material_spms": [str(spm)],
            "atlas_base": base,
            "needs_leaf_mesh": any(k in key for k in LEAF_MESH_KEYWORDS),
            "atlas_blends": [str(blend)] if blend else [],
            "export_maps": export_maps,
            "missing_export_maps": [k for k, v in export_maps.items() if not v],
            "texture_dir": str(maps_dir) if maps_dir else None,
            "m_graph": graph[0] if graph else None,
            "m_graph_sbs": graph[1] if graph else None,
            "source_refs": [],
        }
        by_base[key] = entry
        items.append(entry)
    return items


def infer_normal_convention(refs):
    low = " ".join(refs).lower()
    if "tcom_" in low or "megascan" in low or "megascans" in low:
        return "OpenGL"
    if ".sbsar" in low or "substance" in low:
        return "DirectX"
    return "unknown"


def audit_folder(folder, cfg, include_refs=False):
    folder = Path(folder)
    preferred = preferred_sk_spms(folder)
    loose = loose_sk_spms(folder)
    sources = source_spms(folder)
    chosen = preferred[0] if preferred else (loose[0] if loose else (sources[0] if sources else None))
    blend = blend_for_spm(chosen) if chosen else None
    materials = extract_material_names(chosen) if chosen else []
    material_keys = {m.lower() for m in materials}
    missing_m = [m for m in materials if m and not m.startswith("M_")]
    sbs_files = active_sbs_files(folder)
    texture_dir = sbs_files[0].parent if sbs_files else (folder / "texture")
    tex_dirs = texture_dir_candidates(folder, sbs_files) or [texture_dir]
    folder_graphs = folder_m_graph_names(sbs_files)
    clusters = cluster_spms(folder)
    target_spms = preferred or loose or sources
    leaf_mesh_sources, referenced_clusters = discover_leaf_mesh_sources(
        folder, cfg, target_spms, clusters)
    cluster_items = []
    ignored_cluster_spms = []
    for cluster in clusters:
        atlas_base = atlas_base_from_cluster_stem(cluster.stem)
        maps_dir, export_maps = find_export_maps_multi(tex_dirs, atlas_base, cfg["required_export_maps"])
        missing_maps = [name for name, value in export_maps.items() if not value]
        atlas_blends = find_atlas_blends(cfg["atlas_root"], folder, atlas_base)
        relevant_keys = {
            cluster.stem.lower(),
            atlas_base.lower(),
            atlas_base.lower().removeprefix("m_"),
        }
        is_relevant = (
            bool(atlas_blends)
            or any(export_maps.values())
            or bool(material_keys & relevant_keys)
            or str(cluster).lower() in referenced_clusters
        )
        if not is_relevant:
            ignored_cluster_spms.append(str(cluster))
            continue
        graph = folder_graphs.get(atlas_base.lower())
        cluster_items.append(
            {
                "cluster_spm": str(cluster),
                "name": cluster.stem,
                "source": "cluster",
                "material_names": [],
                "atlas_base": atlas_base,
                "needs_leaf_mesh": True,
                "atlas_blends": atlas_blends,
                "export_maps": export_maps,
                "missing_export_maps": missing_maps,
                "texture_dir": str(maps_dir or texture_dir),
                "m_graph": graph[0] if graph else None,
                "m_graph_sbs": graph[1] if graph else None,
                "source_refs": extract_image_refs(cluster)[:20] if include_refs else [],
                "referenced_by_spms": referenced_clusters.get(str(cluster).lower(), []),
            }
        )
    # 클러스터 SPM 없이 아틀라스를 직접 쓰는 머티리얼도 항목으로 추가
    cluster_items.extend(
        material_atlas_items(
            folder, cfg, tex_dirs,
            [entry["atlas_base"] for entry in cluster_items],
            folder_graphs,
        )
    )
    all_refs = []
    if include_refs:
        for path in ([chosen] if chosen else []) + [Path(i["cluster_spm"]) for i in cluster_items if i.get("cluster_spm")] + sbs_files:
            all_refs.extend(extract_image_refs(path))
    all_refs = unique(all_refs)
    item = {
        "folder": str(folder),
        "name": folder.name,
        "source_spms": [str(p) for p in sources],
        "sk_spms": [str(p) for p in preferred],
        "loose_sk_spms": [str(p) for p in loose if p not in preferred],
        "chosen_spm": str(chosen) if chosen else None,
        "blend": str(blend) if blend else None,
        "materials": materials,
        "materials_missing_m_prefix": missing_m,
        "sbs_files": [str(p) for p in sbs_files],
        "texture_dir": str(texture_dir),
        "texture_dirs": [str(d) for d in tex_dirs],
        "m_graph_names": {low: list(pair) for low, pair in folder_graphs.items()},
        "cluster_items": cluster_items,
        "leaf_mesh_sources": leaf_mesh_sources,
        "ignored_cluster_spms": ignored_cluster_spms,
        "source_refs": all_refs[:40],
        "normal_convention": infer_normal_convention(all_refs),
        "ao_policy": "use source AO if present; otherwise derive HBAO from height",
        "sdf_policy": "connect opacity if needed, set SDF to 0",
    }
    derive_status_actions(item)
    return item


def derive_status_actions(item):
    """item의 필드에서 status/actions를 계산한다.

    공유(shared_from) 항목은 다른 폴더에서 관리되므로 이 폴더의 할 일에서 뺀다.
    make_report의 공유 처리 후 다시 호출된다.
    """
    status = "ready"
    actions = []
    if not item["sk_spms"]:
        status = "needs_sk"
        actions.append("SK SPM 생성 필요")
    if item["materials_missing_m_prefix"]:
        if status != "needs_sk":
            status = "needs_m_prefix"
        actions.append("머티리얼 이름 M_ 정리 필요")
    if item["chosen_spm"] and not item["blend"]:
        actions.append("별도 리페어에서 SK Blend 생성 필요")
    local_entries = [c for c in item["cluster_items"] if not c.get("shared_from")]
    leaf_sources = item.get("leaf_mesh_sources") or []
    if any(not source.get("atlas_blends") for source in leaf_sources):
        actions.append("Blender 아틀라스 파일 확인 필요")
    if any(c["missing_export_maps"] for c in local_entries):
        actions.append("Substance에서 출력 텍스처 저장 필요")
    if local_entries and not item["sbs_files"]:
        actions.append("Substance SBS 파일 확인 필요")
    if actions and status == "ready":
        status = "needs_texture_work"
    item["status"] = status
    item["actions"] = unique(actions)
    return item


def mesh_asset_name(mesh_path):
    tail = str(mesh_path).rsplit("/", 1)[-1]
    return tail.split(".", 1)[0]


def target_mesh_names_from_pcg_targets(pcg_targets):
    names = set()
    if not pcg_targets:
        return names
    for item in pcg_targets.get("meshes", []):
        mesh = item.get("static_mesh")
        if mesh:
            names.add(mesh_asset_name(mesh).lower())
    for da in pcg_targets.get("data_assets", []):
        for entries in da.get("sections", {}).values():
            for entry in entries:
                mesh = entry.get("static_mesh")
                if mesh:
                    names.add(mesh_asset_name(mesh).lower())
    return names


def target_mesh_map_from_pcg_targets(pcg_targets):
    mesh_map = {}
    if not pcg_targets:
        return mesh_map
    for item in pcg_targets.get("meshes", []):
        mesh = item.get("static_mesh")
        if mesh:
            mesh_map.setdefault(mesh_asset_name(mesh).lower(), set()).add(mesh)
    for da in pcg_targets.get("data_assets", []):
        for entries in da.get("sections", {}).values():
            for entry in entries:
                mesh = entry.get("static_mesh")
                if mesh:
                    mesh_map.setdefault(mesh_asset_name(mesh).lower(), set()).add(mesh)
    return mesh_map


def target_mesh_source_map(pcg_targets):
    """Return per-mesh PCG and explicitly placed-level provenance."""
    source_map = {}
    if not pcg_targets:
        return source_map

    def entry(mesh):
        name = mesh_asset_name(mesh).lower()
        return source_map.setdefault(name, {
            "paths": set(),
            "pcg": False,
            "data_assets": set(),
            "levels": set(),
            "level_instances": [],
        })

    for item in pcg_targets.get("meshes", []):
        mesh = item.get("static_mesh")
        if not mesh:
            continue
        source = entry(mesh)
        source["paths"].add(mesh)
        data_assets = item.get("data_assets") or []
        if data_assets or item.get("sections") or item.get("source_graphs"):
            source["pcg"] = True
            source["data_assets"].update(data_assets)
        for placement in item.get("level_instances") or []:
            level = placement.get("level")
            if level:
                source["levels"].add(level)
            source["level_instances"].append(dict(placement))

    for da in pcg_targets.get("data_assets", []):
        for entries in da.get("sections", {}).values():
            for item in entries:
                mesh = item.get("static_mesh")
                if not mesh:
                    continue
                source = entry(mesh)
                source["paths"].add(mesh)
                source["pcg"] = True
                if da.get("asset"):
                    source["data_assets"].add(da["asset"])
    return source_map


def folder_matches_target_meshes(folder, target_mesh_names):
    return bool(folder_target_mesh_names(folder, target_mesh_names))


def normalize_local_asset_stem(stem):
    low = str(stem).lower()
    for prefix in ("sk_", "sm_", "m_"):
        if low.startswith(prefix):
            low = low[len(prefix):]
    if low.startswith("sk)"):
        low = low[3:]
    return low


def folder_match_tokens(folder):
    folder = Path(folder)
    tokens = {normalize_local_asset_stem(folder.name)}
    for pattern in ("*.spm", "*.st9"):
        for path in folder.glob(pattern):
            if path.is_file() and not is_backup_path(path):
                tokens.add(normalize_local_asset_stem(path.stem))
    return sorted(t for t in tokens if t)


def folder_target_mesh_names(folder, target_mesh_names):
    if not target_mesh_names:
        return True
    tokens = folder_match_tokens(folder)
    matches = []
    for mesh_name in target_mesh_names:
        for token in tokens:
            if mesh_name == token or mesh_name.startswith(token + "_") or mesh_name.startswith(token):
                matches.append(mesh_name)
                break
    return sorted(matches)


def candidate_folders(cfg, targets=None, pcg_targets=None):
    if targets:
        return [Path(t) for t in targets]
    root = Path(cfg["tree_root"])
    folders = []
    target_mesh_names = target_mesh_names_from_pcg_targets(pcg_targets)
    if not root.exists():
        return folders
    skip = {"atlas", "mesh", "st9", "trunk"}
    for folder in sorted([p for p in root.iterdir() if p.is_dir()], key=lambda p: p.name.lower()):
        if folder.name.lower() in skip:
            continue
        has_spm = any(p.suffix.lower() == ".spm" and not is_backup_path(p) for p in folder.glob("*.spm"))
        has_cluster = (folder / "Cluster").exists() and any(
            p.is_file() and not is_backup_path(p)
            for p in (folder / "Cluster").glob("*.spm")
        )
        has_sbs = (folder / "texture").exists() and any(
            p.is_file() and not is_backup_path(p)
            for p in (folder / "texture").glob("*.sbs")
        )
        if (has_spm or has_cluster or has_sbs) and folder_matches_target_meshes(folder, target_mesh_names):
            folders.append(folder)
    return folders


def write_csv(report, csv_path):
    rows = []
    for item in report["items"]:
        cluster_count = len(item["cluster_items"])
        missing_maps = sorted(
            set(m for c in item["cluster_items"] for m in c["missing_export_maps"])
        )
        rows.append(
            {
                "name": item["name"],
                "status": item["status"],
                "folder": item["folder"],
                "chosen_spm": item["chosen_spm"] or "",
                "blend": item["blend"] or "",
                "sbs": "; ".join(item["sbs_files"]),
                "clusters": cluster_count,
                "missing_m_prefix": "; ".join(item["materials_missing_m_prefix"]),
                "missing_export_maps": "; ".join(missing_maps),
                "normal_convention": item["normal_convention"],
                "pcg_target_meshes": "; ".join(item.get("pcg_target_mesh_names", [])),
                "duplicate_pcg_target_meshes": "; ".join(item.get("duplicate_pcg_target_mesh_names", [])),
                "target_spm_statuses": "; ".join(
                    f"{entry['mesh_name']}={entry['status']}"
                    for entry in item.get("target_spm_statuses", [])
                ),
                "actions": " | ".join(item["actions"]),
            }
        )
    fields = list(rows[0].keys()) if rows else [
        "name", "status", "folder", "chosen_spm", "blend", "sbs", "clusters",
        "missing_m_prefix", "missing_export_maps", "normal_convention",
        "pcg_target_meshes", "duplicate_pcg_target_meshes",
        "target_spm_statuses", "actions",
    ]
    with Path(csv_path).open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def resolve_shared_atlas_entries(items, cfg):
    """다른 폴더에서 관리되는 아틀라스(예: densiflora가 scotspine 아틀라스 사용)를 정리.

    자기 폴더에 클러스터 SPM도, M_ 그래프도, 출력 텍스처도 없는 항목은:
    1) 같은 아틀라스를 소유한 다른 폴더 항목이 있으면 → shared_from 표시하고
       출력 상태를 소유 폴더 기준으로 바꾼 뒤 이 폴더의 할 일에서 뺀다.
    2) 항목은 없지만 다른 폴더의 SBS에 그 M_ 그래프가 있으면 → 그 폴더에
       항목을 만들어 주고(작업이 소유 폴더 행에 보이도록) 여기는 shared 표시.
    """
    def owns(entry):
        return bool(entry.get("cluster_spm") or entry.get("m_graph")
                    or any((entry.get("export_maps") or {}).values()))

    owners = {}
    items_by_name = {item["name"]: item for item in items}
    for item in items:
        for entry in item["cluster_items"]:
            if owns(entry):
                owners.setdefault(entry["atlas_base"].lower(), (item["name"], entry))
    graph_owners = {}
    for item in items:
        for low, pair in (item.get("m_graph_names") or {}).items():
            graph_owners.setdefault(low, (item["name"], pair))

    changed = set()
    for item in items:
        for entry in item["cluster_items"]:
            if owns(entry) or entry.get("shared_from"):
                continue
            base = entry["atlas_base"]
            base_low = base.lower()
            owner = owners.get(base_low)
            if (owner is None or owner[0] == item["name"]) and base_low in graph_owners:
                graph_owner_name, (graph_name, graph_sbs) = graph_owners[base_low]
                if graph_owner_name != item["name"]:
                    owner_item = items_by_name[graph_owner_name]
                    owner_dirs = [Path(d) for d in owner_item.get("texture_dirs")
                                  or [owner_item["texture_dir"]]]
                    maps_dir, export_maps = find_export_maps_multi(
                        owner_dirs, base, cfg["required_export_maps"])
                    new_entry = dict(entry)
                    new_entry.update(
                        m_graph=graph_name, m_graph_sbs=graph_sbs,
                        export_maps=export_maps,
                        missing_export_maps=[k for k, v in export_maps.items() if not v],
                        texture_dir=str(maps_dir) if maps_dir else owner_item["texture_dir"],
                    )
                    owner_item["cluster_items"].append(new_entry)
                    owners[base_low] = (graph_owner_name, new_entry)
                    changed.add(graph_owner_name)
                    owner = owners[base_low]
            if not owner or owner[0] == item["name"]:
                continue
            owner_name, owner_entry = owner
            entry["shared_from"] = owner_name
            entry["export_maps"] = owner_entry.get("export_maps", {})
            entry["missing_export_maps"] = owner_entry.get("missing_export_maps", [])
            entry["m_graph"] = owner_entry.get("m_graph")
            entry["m_graph_sbs"] = owner_entry.get("m_graph_sbs")
            changed.add(item["name"])
    for name in changed:
        derive_status_actions(items_by_name[name])
    return sorted(changed)


def make_report(cfg, targets=None, include_refs=False, pcg_targets=None):
    folders = candidate_folders(cfg, targets, pcg_targets=pcg_targets)
    items = [audit_folder(folder, cfg, include_refs=include_refs) for folder in folders]
    resolve_shared_atlas_entries(items, cfg)
    target_mesh_map = target_mesh_map_from_pcg_targets(pcg_targets)
    target_source_map = target_mesh_source_map(pcg_targets)
    target_mesh_names = set(target_mesh_map)
    matched_target_names = set()
    target_folder_matches = {}
    for item in items:
        folder_matches = folder_target_mesh_names(item["folder"], target_mesh_names)
        if folder_matches is True:
            folder_matches = []
        matched_target_names.update(folder_matches)
        for name in folder_matches:
            target_folder_matches.setdefault(name, []).append(item["name"])
        item["pcg_target_mesh_names"] = folder_matches
        item["target_mesh_names"] = folder_matches
        item["pcg_mesh_names"] = [
            name for name in folder_matches
            if target_source_map.get(name, {}).get("pcg")
        ]
        item["level_mesh_names"] = [
            name for name in folder_matches
            if target_source_map.get(name, {}).get("levels")
        ]
        item["level_placements"] = [
            placement
            for name in folder_matches
            for placement in target_source_map.get(name, {}).get("level_instances", [])
        ]
        item["pcg_target_meshes"] = [
            path
            for name in folder_matches
            for path in sorted(target_mesh_map.get(name, []))
        ]
        item["target_spm_statuses"] = [
            target_spm_status(item["folder"], name)
            for name in folder_matches
        ]
        target_statuses = {entry["status"] for entry in item["target_spm_statuses"]}
        target_actions = [
            action
            for entry in item["target_spm_statuses"]
            for action in entry.get("actions", [])
        ]
        if "needs_source_review" in target_statuses:
            item["status"] = "needs_source_review"
        elif "needs_sk" in target_statuses:
            item["status"] = "needs_sk"
        elif "needs_m_prefix" in target_statuses:
            item["status"] = "needs_m_prefix"
        elif ({"needs_blend", "needs_blend_update"} & target_statuses) and item["status"] == "ready":
            item["status"] = "needs_texture_work"
        item["actions"] = unique(target_actions + item["actions"])
    duplicate_mesh_matches = {
        name: sorted(folders)
        for name, folders in target_folder_matches.items()
        if len(folders) > 1
    }
    for item in items:
        duplicates = [
            name
            for name in item.get("pcg_target_mesh_names", [])
            if name in duplicate_mesh_matches
        ]
        item["duplicate_pcg_target_mesh_names"] = duplicates
        item["duplicate_target_mesh_names"] = duplicates
        if duplicates:
            item["status"] = "needs_duplicate_review"
            item["actions"] = unique([
                "같은 PCG 대상이 여러 폴더에 매칭됨",
            ] + item["actions"])
    counts = {}
    for item in items:
        counts[item["status"]] = counts.get(item["status"], 0) + 1
    target_status_counts = {}
    for item in items:
        for entry in item.get("target_spm_statuses", []):
            status = entry.get("status", "unknown")
            target_status_counts[status] = target_status_counts.get(status, 0) + 1
    unmatched_names = sorted(target_mesh_names - matched_target_names)
    pcg_mesh_names = {
        name for name, source in target_source_map.items()
        if source.get("pcg")
    }
    level_mesh_names = {
        name for name, source in target_source_map.items()
        if source.get("levels")
    }
    level_placements = [
        placement
        for source in target_source_map.values()
        for placement in source.get("level_instances", [])
    ]
    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "config": cfg,
        "pcg_targets": {
            "source": pcg_targets.get("source") if pcg_targets else None,
            "graph": pcg_targets.get("graph") if pcg_targets else None,
            "generated_at": pcg_targets.get("generated_at") if pcg_targets else None,
            "source_file": pcg_targets.get("source_file") if pcg_targets else None,
            "mesh_count": len(target_mesh_names) if pcg_targets else 0,
            "matched_mesh_count": len(matched_target_names) if pcg_targets else 0,
            "unmatched_mesh_count": len(unmatched_names) if pcg_targets else 0,
            "unmatched_mesh_names": unmatched_names,
            "unmatched_meshes": [
                path
                for name in unmatched_names
                for path in sorted(target_mesh_map.get(name, []))
            ],
            "duplicate_mesh_match_count": len(duplicate_mesh_matches) if pcg_targets else 0,
            "duplicate_mesh_matches": duplicate_mesh_matches,
            "pcg_mesh_count": len(pcg_mesh_names),
            "level_mesh_count": len(level_mesh_names),
            "pcg_level_overlap_mesh_count": len(pcg_mesh_names & level_mesh_names),
            "level_component_count": len(level_placements),
            "level_instance_count": sum(int(item.get("instance_count", 1)) for item in level_placements),
            "levels": pcg_targets.get("levels", []) if pcg_targets else [],
        },
        "summary": {
            "total": len(items),
            "by_status": counts,
            "pcg_target_status_counts": target_status_counts,
        },
        "items": items,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", dest="json_path")
    parser.add_argument("--csv", dest="csv_path")
    parser.add_argument("--target", action="append", help="Tree folder to audit; repeatable")
    parser.add_argument("--prepare-sk", action="append", help="Tree folder to copy/patch SK SPM")
    parser.add_argument("--prepare-target-mesh", action="append", help="PCG mesh name to prepare with --prepare-sk")
    parser.add_argument("--dry-run", action="store_true", help="Plan prepare actions without writing files")
    parser.add_argument("--include-refs", action="store_true", help="Read embedded source texture references")
    parser.add_argument("--pcg-targets", help="pcg_targets.json from refresh_pcg_targets.py")
    args = parser.parse_args()
    cfg = load_config()
    if args.prepare_sk:
        results = [prepare_sk(path, args.prepare_target_mesh, dry_run=args.dry_run) for path in args.prepare_sk]
        print(json.dumps({"prepare_sk": results}, indent=2, ensure_ascii=False))
        return
    pcg_targets = load_pcg_targets(args.pcg_targets) if args.pcg_targets else None
    report = make_report(cfg, args.target, include_refs=args.include_refs, pcg_targets=pcg_targets)
    if args.json_path:
        Path(args.json_path).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json_path).write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    if args.csv_path:
        Path(args.csv_path).parent.mkdir(parents=True, exist_ok=True)
        write_csv(report, args.csv_path)
    if not args.json_path and not args.csv_path:
        print(json.dumps(report, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
