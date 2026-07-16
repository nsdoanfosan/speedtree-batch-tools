"""Audit PCG/ST9 SpeedTree texture preparation state.

This is intentionally conservative. It reads filesystem/SPM/SBS evidence and
only mutates files when --prepare-sk is passed.
"""
import argparse
import csv
import functools
import gzip
import html
import json
import os
import re
import shutil
import subprocess
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
MATERIAL_ID_RE = re.compile(r'<Material_v8\b[^>]*?ID="([^"]+)"', re.IGNORECASE)
MATERIAL_BLOCK_RE = re.compile(
    r"<Material_v8\b[^>]*>.*?</Material_v8>", re.IGNORECASE | re.DOTALL)
TEX_FILENAME_RE = re.compile(
    r"<TexFilename\b[^>]*>([^<]*?)(?:</TexFilename>|<\\TexFilename>)",
    re.IGNORECASE | re.DOTALL)
GENERATOR_BLOCK_RE = re.compile(
    r"<Generator\b[^>]*>.*?</Generator>", re.IGNORECASE | re.DOTALL)
PROPERTY_BLOCK_RE = re.compile(
    r"<Property\b[^>]*>.*?</Property>", re.IGNORECASE | re.DOTALL)
PROPERTY_NAME_RE = re.compile(
    r"<Name\b[^>]*>(.*?)</Name>", re.IGNORECASE | re.DOTALL)
PROPERTY_VALUE_RE = re.compile(
    r"<Value\b[^>]*>(.*?)</Value>", re.IGNORECASE | re.DOTALL)
ACTIVE_MATERIAL_VALUE_RE = re.compile(
    r"<Name\b[^>]*>([^<]*:Material)</Name>\s*"
    r"<Value\b[^>]*>(.*?)</Value>",
    re.IGNORECASE | re.DOTALL,
)
GENERATOR_LINK_RE = re.compile(r"<Link>.*?</Link>", re.IGNORECASE | re.DOTALL)
ELEMENT_GUID_RE = re.compile(r"<GUID>([^<]*)</GUID>", re.IGNORECASE)
ELEMENT_HIDDEN_RE = re.compile(r"<Hidden>([^<]*)</Hidden>", re.IGNORECASE)
LINK_SOURCE_GUID_RE = re.compile(r"<SourceGUID>([^<]*)</SourceGUID>", re.IGNORECASE)
LINK_TARGET_GUID_RE = re.compile(r"<TargetGUID>([^<]*)</TargetGUID>", re.IGNORECASE)
BLEND_IMAGE_EXTENSION_RE = re.compile(
    rb"\.(?:png|tga|tif|tiff|jpg|jpeg|exr|bmp)", re.IGNORECASE)
BLEND_IMAGE_NAME_BYTES = frozenset(
    b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_. ()-"
)
ABS_IMAGE_RE = re.compile(
    r"[A-Za-z]:[\\/][^<>'\"\r\n]+?\.(?:png|tga|tif|tiff|jpg|jpeg|exr|bmp)",
    re.IGNORECASE,
)
TOKEN_SPLIT_RE = re.compile(r"[\s<>'\"=;,\(\)\[\]\{\}]+")
LEAF_SOURCE_WORDS = ("leaf", "leaves", "foliage", "needle")
ALPHA_WORDS = ("alpha", "opacity", "transparency", "mask")
GENERATED_EXPORT_RE = re.compile(
    r"^[mt]_.*_(?:color|normal|extra|height|opacity|subsurface)\.(?:tga|png|tif|tiff|exr)$",
    re.IGNORECASE,
)
SOURCE_MAP_SUFFIX_RE = re.compile(
    r"(?:[_-](?:base[_-]?color|basecolor|albedo|diffuse|colour|color|opacity|alpha|"
    r"transparency|mask|normal|roughness|rough|gloss|height|displacement|depth|"
    r"ambient[_-]?occlusion|ao|occlusion|translucency|subsurface))$",
    re.IGNORECASE,
)
SOURCE_RESOLUTION_SUFFIX_RE = re.compile(
    r"(?:[_-](?:1k|2k|4k|8k|16k|512|1024|2048|4096|8192|16384|\d+x\d+))$",
    re.IGNORECASE)
_SPM_ANALYSIS_CACHE = {}
_PERSISTENT_SPM_ANALYSIS = None
_PERSISTENT_SPM_ANALYSIS_DIRTY = False
# v3 stores both all referenced IDs (provenance) and visible/export IDs.
SPM_ANALYSIS_CACHE_PATH = REPORT_DIR / "_cache" / "spm_analysis_v3.json"
SBS_GRAPH_CACHE_PATH = REPORT_DIR / "_cache" / "sbs_graph_names_v1.json"
BLEND_IMAGE_CACHE_PATH = REPORT_DIR / "_cache" / "blend_image_names_v1.json"
_PERSISTENT_SBS_GRAPHS = None
_PERSISTENT_SBS_GRAPHS_DIRTY = False
_PERSISTENT_BLEND_IMAGES = None
_PERSISTENT_BLEND_IMAGES_DIRTY = False
_DIRECTORY_FILE_INDEX_CACHE = {}
_BLEND_IMAGE_NAMES_CACHE = {}
_IMAGE_EQUAL_CACHE = {}
COMMON_BARK_END_RE = re.compile(
    r"^m_bark_common_(?!end_).+_end_0*(\d+)$", re.IGNORECASE)
GENERIC_MATERIAL_NAME_RE = re.compile(
    r"^(?:m_)?material(?:\s+copy)?(?:\s*\d+)?$", re.IGNORECASE)


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


def _file_cache_key(path):
    path = Path(path)
    try:
        stat = path.stat()
        return str(path).lower(), stat.st_size, stat.st_mtime_ns
    except OSError:
        return str(path).lower(), 0, 0


def _persistent_spm_analysis():
    global _PERSISTENT_SPM_ANALYSIS
    if _PERSISTENT_SPM_ANALYSIS is not None:
        return _PERSISTENT_SPM_ANALYSIS
    try:
        payload = json.loads(SPM_ANALYSIS_CACHE_PATH.read_text(encoding="utf-8"))
        entries = payload.get("entries", {}) if payload.get("version") == 1 else {}
        _PERSISTENT_SPM_ANALYSIS = entries if isinstance(entries, dict) else {}
    except Exception:
        _PERSISTENT_SPM_ANALYSIS = {}
    return _PERSISTENT_SPM_ANALYSIS


def save_spm_analysis_cache():
    """Persist compact parsed SPM metadata; never writes to asset folders."""
    global _PERSISTENT_SPM_ANALYSIS_DIRTY, _PERSISTENT_SBS_GRAPHS_DIRTY
    global _PERSISTENT_BLEND_IMAGES_DIRTY
    written = []
    for dirty, cache_path, entries in (
        (_PERSISTENT_SPM_ANALYSIS_DIRTY, SPM_ANALYSIS_CACHE_PATH,
         _persistent_spm_analysis()),
        (_PERSISTENT_SBS_GRAPHS_DIRTY, SBS_GRAPH_CACHE_PATH,
         _persistent_sbs_graphs()),
        (_PERSISTENT_BLEND_IMAGES_DIRTY, BLEND_IMAGE_CACHE_PATH,
         _persistent_blend_images()),
    ):
        if not dirty:
            continue
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = cache_path.with_name(
            f".{cache_path.name}.{os.getpid()}.tmp")
        payload = {"version": 1, "entries": entries}
        temp_path.write_text(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        temp_path.replace(cache_path)
        written.append(cache_path)
    _PERSISTENT_SPM_ANALYSIS_DIRTY = False
    _PERSISTENT_SBS_GRAPHS_DIRTY = False
    _PERSISTENT_BLEND_IMAGES_DIRTY = False
    return written or None


def _persistent_sbs_graphs():
    global _PERSISTENT_SBS_GRAPHS
    if _PERSISTENT_SBS_GRAPHS is not None:
        return _PERSISTENT_SBS_GRAPHS
    try:
        payload = json.loads(SBS_GRAPH_CACHE_PATH.read_text(encoding="utf-8"))
        entries = payload.get("entries", {}) if payload.get("version") == 1 else {}
        _PERSISTENT_SBS_GRAPHS = entries if isinstance(entries, dict) else {}
    except Exception:
        _PERSISTENT_SBS_GRAPHS = {}
    return _PERSISTENT_SBS_GRAPHS


def _persistent_blend_images():
    global _PERSISTENT_BLEND_IMAGES
    if _PERSISTENT_BLEND_IMAGES is not None:
        return _PERSISTENT_BLEND_IMAGES
    try:
        payload = json.loads(BLEND_IMAGE_CACHE_PATH.read_text(encoding="utf-8"))
        entries = payload.get("entries", {}) if payload.get("version") == 1 else {}
        _PERSISTENT_BLEND_IMAGES = entries if isinstance(entries, dict) else {}
    except Exception:
        _PERSISTENT_BLEND_IMAGES = {}
    return _PERSISTENT_BLEND_IMAGES


def _cached_sbs_graph_names(sbs_path):
    global _PERSISTENT_SBS_GRAPHS_DIRTY
    from sbs_auto import list_m_graphs
    cache_key = _file_cache_key(sbs_path)
    path_key, size, mtime_ns = cache_key
    entry = _persistent_sbs_graphs().get(path_key)
    if entry and entry.get("size") == size and entry.get("mtime_ns") == mtime_ns:
        return entry.get("names", [])
    names = list_m_graphs(sbs_path)
    if size or mtime_ns:
        _persistent_sbs_graphs()[path_key] = {
            "size": size,
            "mtime_ns": mtime_ns,
            "names": names,
        }
        _PERSISTENT_SBS_GRAPHS_DIRTY = True
    return names


def _referenced_material_ids_from_text(text):
    """All material IDs referenced by any SpeedTree Generator property."""
    return {
        html.unescape(match.group(2).strip())
        for match in ACTIVE_MATERIAL_VALUE_RE.finditer(text)
    }


def _visible_material_ids_from_text(text):
    """Material IDs referenced by generators that would actually export.

    SpeedTree keeps ``...:Material`` references on generators whose eye is
    toggled off (``<Hidden>true</Hidden>``); that geometry never exports, so
    counting it made disabled experiments look like active materials. A
    generator is effectively hidden when it or any node-graph ancestor
    (``<Link>`` Source->Target) is hidden. References that appear outside any
    Generator block keep the previous always-active behavior.
    """
    all_ids = _referenced_material_ids_from_text(text)
    generators = []
    hidden_by_guid = {}
    generator_ids = set()
    for block_match in GENERATOR_BLOCK_RE.finditer(text):
        block = block_match.group(0)
        ids = {
            html.unescape(match.group(2).strip())
            for match in ACTIVE_MATERIAL_VALUE_RE.finditer(block)
        }
        generator_ids |= ids
        guid_match = ELEMENT_GUID_RE.search(block)
        hidden_match = ELEMENT_HIDDEN_RE.search(block)
        guid = guid_match.group(1).strip() if guid_match else ""
        if guid:
            hidden_by_guid[guid] = bool(
                hidden_match and hidden_match.group(1).strip().lower() == "true"
            )
        generators.append((guid, ids))
    if not generators:
        return all_ids

    parent = {}
    for link_match in GENERATOR_LINK_RE.finditer(text):
        block = link_match.group(0)
        source = LINK_SOURCE_GUID_RE.search(block)
        target = LINK_TARGET_GUID_RE.search(block)
        if source and target:
            parent[target.group(1).strip()] = source.group(1).strip()

    def effectively_hidden(guid):
        seen = set()
        while guid and guid not in seen:
            seen.add(guid)
            if hidden_by_guid.get(guid):
                return True
            guid = parent.get(guid, "")
        return False

    active = all_ids - generator_ids
    for guid, ids in generators:
        if not guid or not effectively_hidden(guid):
            active |= ids
    return active


def _spm_analysis(path):
    """Decompress and parse one SPM once for all read-only audit queries."""
    global _PERSISTENT_SPM_ANALYSIS_DIRTY
    cache_key = _file_cache_key(path)
    cached = _SPM_ANALYSIS_CACHE.get(cache_key)
    if cached is not None:
        return cached

    path_key, size, mtime_ns = cache_key
    persistent = _persistent_spm_analysis()
    disk_entry = persistent.get(path_key)
    if disk_entry and disk_entry.get("size") == size \
            and disk_entry.get("mtime_ns") == mtime_ns:
        analysis = {
            "material_rows": disk_entry.get("material_rows", []),
            "material_names": disk_entry.get("material_names", []),
            "active_material_ids": set(disk_entry.get("referenced_material_ids", [])),
            "visible_material_ids": set(disk_entry.get("visible_material_ids", [])),
        }
        _SPM_ANALYSIS_CACHE[cache_key] = analysis
        return analysis

    text = read_maybe_gzip_text(path)
    rows = []
    for block_match in MATERIAL_BLOCK_RE.finditer(text):
        block = block_match.group(0)
        name_match = MATERIAL_RE.search(block)
        id_match = MATERIAL_ID_RE.search(block)
        name = html.unescape(name_match.group(2)) if name_match else ""
        refs = []
        for ref_match in TEX_FILENAME_RE.finditer(block):
            value = html.unescape(ref_match.group(1).strip())
            if value and Path(value).suffix.lower() in IMAGE_EXTS:
                refs.append(value.replace("/", "\\"))
        rows.append({
            "material_id": id_match.group(1) if id_match else None,
            "material_name": name,
            "refs": unique(refs),
        })

    referenced = _referenced_material_ids_from_text(text)
    visible = _visible_material_ids_from_text(text)

    analysis = {
        "material_rows": rows,
        "material_names": unique(
            row["material_name"] for row in rows if row["material_name"]),
        # Keep active_material_ids as the legacy all-reference set. Cluster
        # provenance needs hidden source generators too; final jobs use the
        # separate visible_material_ids set below.
        "active_material_ids": referenced,
        "visible_material_ids": visible,
    }
    # A modified file receives a new stat key. Drop only stale entries for
    # this exact path so long-running GUI refreshes do not grow indefinitely.
    path_key = cache_key[0]
    for old_key in [key for key in _SPM_ANALYSIS_CACHE
                    if key[0] == path_key and key != cache_key]:
        del _SPM_ANALYSIS_CACHE[old_key]
    _SPM_ANALYSIS_CACHE[cache_key] = analysis
    if size or mtime_ns:
        persistent[path_key] = {
            "size": size,
            "mtime_ns": mtime_ns,
            "material_rows": rows,
            "material_names": analysis["material_names"],
            "referenced_material_ids": sorted(referenced),
            "visible_material_ids": sorted(visible),
        }
        _PERSISTENT_SPM_ANALYSIS_DIRTY = True
    return analysis


def canonical_material_name(name):
    """Normalize shared bark-end aliases; preserve tint-specific stem names."""
    name = str(name or "").strip()
    prefixed = name if name.lower().startswith("m_") else "M_" + name
    match = COMMON_BARK_END_RE.match(prefixed)
    if match:
        return f"M_bark_common_end_{int(match.group(1)):02d}"
    return prefixed


def material_rename_plan(spm, exclude=None):
    names = visible_material_names(spm)
    exclude = {str(name) for name in (exclude or [])}
    renames = []
    for name in names:
        if not name or name in exclude:
            continue
        target = canonical_material_name(name)
        if target != name:
            renames.append([name, target])
    return renames


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
    return _spm_analysis(spm)["material_names"]


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
    return _spm_analysis(path)["material_rows"]


def active_material_ids(path):
    """All IDs referenced by a Generator, including hidden provenance nodes."""
    return _spm_analysis(path)["active_material_ids"]


def active_material_names(path):
    """All material names referenced by a Generator, including hidden nodes."""
    active_ids = active_material_ids(path)
    rows = extract_material_image_refs(path)
    if not active_ids:
        return unique(row["material_name"] for row in rows if row["material_name"])
    return unique(
        row["material_name"] for row in rows
        if row["material_name"] and row.get("material_id") in active_ids
    )


def visible_material_ids(path):
    """IDs used by visible generators whose ancestor chain is also visible."""
    return _spm_analysis(path)["visible_material_ids"]


def visible_material_names(path):
    """Material names that contribute geometry to the exported SpeedTree."""
    visible_ids = visible_material_ids(path)
    rows = extract_material_image_refs(path)
    if not active_material_ids(path):
        return unique(row["material_name"] for row in rows if row["material_name"])
    return unique(
        row["material_name"] for row in rows
        if row["material_name"] and row.get("material_id") in visible_ids
    )


@functools.lru_cache(maxsize=32768)
def _resolve_spm_image_ref_cached(spm_text, ref_text):
    path = Path(ref_text.replace("/", "\\"))
    if not path.is_absolute():
        path = Path(spm_text).parent / path
    try:
        return path.resolve()
    except (OSError, ValueError):
        return path.absolute()


def resolve_spm_image_ref(spm, ref):
    return _resolve_spm_image_ref_cached(str(spm), str(ref))


def path_exists(path):
    try:
        return Path(path).exists()
    except (OSError, ValueError):
        return False


def source_family_name(path):
    """Stable display/output name shared by all maps in one source atlas set."""
    stem = Path(path).stem
    stem = SOURCE_MAP_SUFFIX_RE.sub("", stem)
    stem = SOURCE_RESOLUTION_SUFFIX_RE.sub("", stem)
    stem = SOURCE_MAP_SUFFIX_RE.sub("", stem)
    return stem.strip("_-") or Path(path).stem


def leaf_sources_from_spm(spm, source_kind, excluded_albedo_stems=None, active_only=True):
    """Find coherent leaf albedo/alpha pairs used by one SPM material.

    One material normally owns one atlas pair. If a material happens to list
    more maps, family-name matching keeps albedo and alpha in the same set.
    """
    results = []
    excluded_albedo_stems = {str(stem).lower() for stem in (excluded_albedo_stems or [])}
    active_ids = active_material_ids(spm) if active_only else None
    for row in extract_material_image_refs(spm):
        if active_only and active_ids and row.get("material_id") not in active_ids:
            continue
        name = row["material_name"]
        refs = row["refs"]
        searchable = " ".join([name] + refs).lower()
        if not any(word in searchable for word in LEAF_SOURCE_WORDS):
            continue
        # Material_v8 keeps the base-color texture in its first texture slot.
        # Do not guess from a missing filename suffix such as leaf_x.tga.
        albedo_refs = [refs[0]] if refs else []
        alpha_refs = []
        for ref in refs[1:]:
            low = Path(ref).name.lower()
            if any(word in low for word in ALPHA_WORDS):
                alpha_refs.append(ref)
        albedo_refs = [
            ref for ref in albedo_refs
            if Path(ref).stem.lower() not in excluded_albedo_stems
            and not GENERATED_EXPORT_RE.match(Path(ref).name)
        ]
        alpha_refs = [
            ref for ref in alpha_refs
            if not GENERATED_EXPORT_RE.match(Path(ref).name)
        ]
        if not albedo_refs or not alpha_refs:
            continue
        albedos = [resolve_spm_image_ref(spm, ref) for ref in albedo_refs]
        alphas = [resolve_spm_image_ref(spm, ref) for ref in alpha_refs]
        albedos = [path for path in albedos if path_exists(path)]
        alphas = [path for path in alphas if path_exists(path)]
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
            family_refs = []
            for ref in refs:
                path = resolve_spm_image_ref(spm, ref)
                if path_exists(path) and source_family_name(path).lower() == family.lower():
                    family_refs.append(str(path))
            results.append({
                "albedo": str(albedo),
                "alpha": str(alpha),
                "source_family": family,
                "atlas_base": f"M_{safe_family}_atlas_01",
                "source_kind": source_kind,
                "target_spm": str(Path(spm)),
                "material_names": [name] if name else [],
                "source_refs": unique(family_refs),
            })
    return results


def _is_under(path, roots):
    try:
        resolved = Path(path).resolve()
    except (OSError, ValueError):
        resolved = Path(path).absolute()
    for root in roots:
        try:
            resolved.relative_to(Path(root).resolve())
            return True
        except (OSError, ValueError):
            continue
    return False


def image_pixels_equal(left, right):
    """Compare decoded pixels so PNG/TIF/JPG copies can be safely deduplicated."""
    left = Path(left)
    right = Path(right)
    key = tuple(sorted((str(left).lower(), str(right).lower())))
    if key in _IMAGE_EQUAL_CACHE:
        return _IMAGE_EQUAL_CACHE[key]
    try:
        from PIL import Image, ImageChops
        with Image.open(left) as left_image, Image.open(right) as right_image:
            if left_image.size != right_image.size:
                equal = False
            else:
                left_rgba = left_image.convert("RGBA")
                right_rgba = right_image.convert("RGBA")
                equal = ImageChops.difference(left_rgba, right_rgba).getbbox() is None
    except Exception:
        equal = False
    _IMAGE_EQUAL_CACHE[key] = equal
    return equal


def canonicalize_leaf_sources(sources, candidates, cfg, folder):
    """Replace pixel-identical local copies with a referenced external pair."""
    source_roots = [Path(path) for path in cfg.get("source_texture_roots", [])]
    by_family = {}
    for candidate in candidates:
        if not _is_under(candidate["albedo"], source_roots):
            continue
        by_family.setdefault(candidate["source_family"].lower(), []).append(candidate)
    for source in sources:
        if not _is_under(source["albedo"], [folder]):
            continue
        originals = [
            candidate
            for candidate in by_family.get(source["source_family"].lower(), [])
            if image_pixels_equal(source["albedo"], candidate["albedo"])
            and image_pixels_equal(source["alpha"], candidate["alpha"])
        ]
        if not originals:
            continue
        original = sorted(
            originals,
            key=lambda row: (str(row["albedo"]).lower(), str(row["alpha"]).lower()),
        )[0]
        old_pair = [source["albedo"], source["alpha"]]
        source["albedo"] = original["albedo"]
        source["alpha"] = original["alpha"]
        source["source_refs"] = list(original.get("source_refs") or [
            original["albedo"], original["alpha"]])
        source["canonicalized_from"] = old_pair
    return sources


def merge_leaf_mesh_sources(sources, cfg, folder):
    """Deduplicate by source atlas pair and merge final-SK application targets."""
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
        trace = None
        if source.get("trace_spm"):
            trace = {
                "spm": source["trace_spm"],
                "material_names": list(source.get("trace_material_names") or []),
            }
        if entry is None:
            entry = dict(source)
            entry["targets"] = [target]
            entry["trace_sources"] = [trace] if trace else []
            entry["source_kinds"] = [source.get("source_kind", "direct")]
            grouped[key] = entry
            continue
        if source.get("source_kind") not in entry["source_kinds"]:
            entry["source_kinds"].append(source.get("source_kind"))
        if trace and not any(
                row["spm"].lower() == trace["spm"].lower()
                for row in entry["trace_sources"]):
            entry["trace_sources"].append(trace)
        existing = next(
            (row for row in entry["targets"]
             if row["spm"].lower() == target["spm"].lower()), None)
        if existing:
            existing["material_names"] = unique(
                existing["material_names"] + target["material_names"])
        else:
            entry["targets"].append(target)
    results = list(grouped.values())
    assign_leaf_atlas_bases(results, folder)
    for entry in results:
        material_names = unique(
            material_name
            for target in entry.get("targets", [])
            for material_name in target.get("material_names", [])
        )
        entry["atlas_blends"] = find_atlas_blends(
            cfg["atlas_root"], folder, entry["atlas_base"],
            source_images=(entry.get("albedo"),), aliases=material_names)
    return results


def _material_leaf_atlas_name(name):
    """Convert a meaningful SpeedTree material name to the atlas convention."""
    canonical = canonical_material_name(name)
    match = re.match(r"^(.*?)(?:_atlas)?_0*(\d+)$", canonical, re.IGNORECASE)
    if match:
        return f"{match.group(1)}_atlas_{int(match.group(2)):02d}"
    return f"{canonical}_atlas_01"


def _folder_leaf_atlas_root(folder):
    """Fallback name for cluster internals that have only generic materials."""
    asset = Path(folder).name
    asset = re.sub(r"^(?:tree|bush|shrub|weed|grass)_", "", asset,
                   flags=re.IGNORECASE)
    safe = re.sub(r"[^A-Za-z0-9_-]+", "_", asset).strip("_-") or "asset"
    return f"M_leaf_{safe}_atlas"


def assign_leaf_atlas_bases(sources, folder):
    """Name generated leaf atlases from asset/material context, never source files.

    A referenced final material is authoritative when one unambiguous name is
    available.  Cluster internals commonly expose only ``Material`` names, so
    those sources receive the asset fallback and a stable per-source index.
    """
    fallback = []
    for source in sources:
        names = unique(
            name
            for target in source.get("targets") or []
            for name in target.get("material_names") or []
            if name and not GENERIC_MATERIAL_NAME_RE.match(str(name))
        )
        canonical = unique(_material_leaf_atlas_name(name) for name in names)
        if len(canonical) == 1:
            source["atlas_base"] = canonical[0]
        else:
            fallback.append(source)

    root = _folder_leaf_atlas_root(folder)
    for index, source in enumerate(sorted(
            fallback,
            key=lambda row: (
                str(row.get("albedo", "")).lower(),
                str(row.get("alpha", "")).lower(),
            )), 1):
        source["atlas_base"] = f"{root}_{index:02d}"
    return sources


def cluster_material_usage(target_spms, clusters):
    """Active final-SPM materials that actually use each Cluster render."""
    by_stem = {cluster.stem.lower(): cluster for cluster in clusters}
    found = {}
    for spm in target_spms:
        active_ids = active_material_ids(spm)
        original_refs = (
            _source_material_ref_map(Path(spm).parent, Path(spm))
            if Path(spm).name.lower().startswith("sk_") else {}
        )
        for material in extract_material_image_refs(spm):
            if active_ids and material.get("material_id") not in active_ids:
                continue
            refs = material["refs"]
            canonical = canonical_material_name(material.get("material_name")).lower()
            # A converted SK often no longer contains the Cluster render path,
            # even though its exact non-SK source does.  Follow the authoritative
            # source slot before deciding whether a Cluster SPM is referenced;
            # otherwise the trace stops at leaf_x.tga and never reaches the
            # single-leaf atlas used to produce that render.
            if _refs_are_only_managed_outputs(refs) and original_refs.get(canonical):
                refs = original_refs[canonical]
            matched = {
                by_stem[Path(ref).stem.lower()]
                for ref in refs
                if Path(ref).stem.lower() in by_stem
            }
            for cluster in matched:
                key = str(cluster).lower()
                usage = found.setdefault(key, {
                    "spms": [], "material_names": [], "source_refs": [],
                    "source_albedo": [], "source_alpha": [],
                })
                if str(spm) not in usage["spms"]:
                    usage["spms"].append(str(spm))
                if material["material_name"] not in usage["material_names"]:
                    usage["material_names"].append(material["material_name"])
                for ref in refs:
                    resolved = resolve_spm_image_ref(spm, ref)
                    if not path_exists(resolved):
                        continue
                    text = str(resolved)
                    if text.lower() not in {value.lower() for value in usage["source_refs"]}:
                        usage["source_refs"].append(text)
                    stem = Path(ref).stem.lower()
                    if stem == cluster.stem.lower():
                        if text.lower() not in {value.lower() for value in usage["source_albedo"]}:
                            usage["source_albedo"].append(text)
                    elif stem.startswith(cluster.stem.lower() + "_") and any(
                            word in stem for word in ALPHA_WORDS):
                        if text.lower() not in {value.lower() for value in usage["source_alpha"]}:
                            usage["source_alpha"].append(text)
    return found


def referenced_cluster_spms(target_spms, clusters):
    """Map actively used Cluster SPMs to the final SPMs using their renders."""
    found = {}
    for key, usage in cluster_material_usage(target_spms, clusters).items():
        found[key] = sorted(usage["spms"])
    return found


def discover_leaf_mesh_sources(folder, cfg, target_spms, clusters):
    """Discover direct leaf atlases plus leaf atlases inside referenced clusters."""
    referenced = referenced_cluster_spms(target_spms, clusters)
    referenced_stems = {Path(path).stem.lower() for path in referenced}
    sources = []
    candidates = []
    candidate_spms = list(target_spms)
    candidate_spms.extend(
        spm for spm in source_spms(folder) if spm not in candidate_spms)
    for candidate_spm in candidate_spms:
        candidates.extend(leaf_sources_from_spm(
            candidate_spm, "direct", excluded_albedo_stems=referenced_stems,
            active_only=False))
    for spm in target_spms:
        authoritative = spm
        if spm.name.lower().startswith("sk_"):
            source = Path(folder) / spm.name[3:]
            if source.is_file():
                authoritative = source
        target_names = {}
        target_active = active_material_ids(spm)
        for row in extract_material_image_refs(spm):
            if target_active and row.get("material_id") not in target_active:
                continue
            key = canonical_material_name(row.get("material_name")).lower()
            target_names.setdefault(key, []).append(row.get("material_name"))
        for source in leaf_sources_from_spm(
                authoritative, "direct", excluded_albedo_stems=referenced_stems,
                active_only=False):
            source["target_spm"] = str(spm)
            mapped_names = []
            for name in source.get("material_names") or []:
                mapped_names.extend(
                    target_names.get(canonical_material_name(name).lower(), []))
            if not mapped_names:
                continue
            source["material_names"] = unique(mapped_names)
            sources.append(source)
    for cluster in clusters:
        if str(cluster).lower() not in referenced:
            continue
        candidates.extend(leaf_sources_from_spm(
            cluster, "cluster", active_only=False))
        for source in leaf_sources_from_spm(cluster, "cluster"):
            final_spms = referenced[str(cluster).lower()]
            for final_spm in final_spms:
                target_source = dict(source)
                target_source["trace_spm"] = str(cluster)
                target_source["trace_material_names"] = list(
                    source.get("material_names") or [])
                target_source["referenced_by_spms"] = final_spms
                target_source["target_spm"] = final_spm
                # The Cluster material explains where the original atlas came
                # from; it is not the material being updated in the final SK.
                target_source["material_names"] = []
                sources.append(target_source)
    canonicalize_leaf_sources(sources, candidates, cfg, folder)
    return merge_leaf_mesh_sources(sources, cfg, folder), referenced


def patch_m_prefix(spm, exclude=None):
    spm = Path(spm)
    text = read_maybe_gzip_text(spm)
    renames = dict(material_rename_plan(spm, exclude=exclude))
    if not renames:
        return {"spm": str(spm), "changed": False, "renames": []}

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    # Keep backups in a subfolder so the working SPM list stays clean.
    backup_dir = spm.parent / "_spm_backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup = backup_dir / f"{spm.stem}.pcgtex_backup_before_material_names_{ts}.spm"
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
    return material_rename_plan(spm, exclude=exclude)


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
    materials = visible_material_names(target) if target else []
    missing_m = [
        m for m in materials
        if m and not m.startswith("M_")
    ]
    renames_needed = material_rename_plan(target) if target else []
    blend = blend_for_spm(sk) if sk else None
    status = "ready"
    actions = []
    if not source and not sk:
        status = "needs_source_review"
        actions.append("원본 SPM 확인 필요")
    elif not sk:
        status = "needs_sk"
        actions.append("SK SPM 생성 필요")
    elif renames_needed or missing_m:
        status = "needs_m_prefix"
        actions.append("머티리얼 이름 M_/공용 이름 정리 필요")
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
        "material_renames_needed": renames_needed,
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
    match = re.search(r"[_-]0*(\d+)$", stem)
    index = int(match.group(1)) if match else 1
    base = stem[:match.start()] if match else stem
    base = base.strip("_-")
    return f"M_{base}_atlas_{index:02d}"


def texture_base_for_material(material_name):
    """Unreal handoff contract: M_x material -> T_x texture set."""
    name = str(material_name).strip()
    if name.lower().startswith("m_"):
        return "T_" + name[2:]
    if name.lower().startswith("t_"):
        return "T_" + name[2:]
    return "T_" + name


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


def _blend_image_names(path):
    global _PERSISTENT_BLEND_IMAGES_DIRTY
    path = Path(path)
    cache_key = _file_cache_key(path)
    cached = _BLEND_IMAGE_NAMES_CACHE.get(cache_key)
    if cached is not None:
        return cached
    path_key, size, mtime_ns = cache_key
    disk_entry = _persistent_blend_images().get(path_key)
    if disk_entry and disk_entry.get("size") == size \
            and disk_entry.get("mtime_ns") == mtime_ns:
        names = set(disk_entry.get("names", []))
        _BLEND_IMAGE_NAMES_CACHE[cache_key] = names
        return names
    try:
        data = path.read_bytes()
        names = set()
        for match in BLEND_IMAGE_EXTENSION_RE.finditer(data):
            start = match.start()
            limit = max(0, start - 180)
            while start > limit and data[start - 1] in BLEND_IMAGE_NAME_BYTES:
                start -= 1
            if start < match.start():
                names.add(data[start:match.end()].decode(
                    "ascii", errors="ignore").lower())
    except OSError:
        names = set()
    _BLEND_IMAGE_NAMES_CACHE[cache_key] = names
    if size or mtime_ns:
        _persistent_blend_images()[path_key] = {
            "size": size,
            "mtime_ns": mtime_ns,
            "names": sorted(names),
        }
        _PERSISTENT_BLEND_IMAGES_DIRTY = True
    return names


def register_blend_source_images(blend_path, source_images, authoritative=False):
    """Record sources for a newly generated compressed blend without reopening it."""
    global _PERSISTENT_BLEND_IMAGES_DIRTY
    cache_key = _file_cache_key(blend_path)
    path_key, size, mtime_ns = cache_key
    names = {
        Path(value).name.lower() for value in (source_images or ()) if value
    }
    _BLEND_IMAGE_NAMES_CACHE[cache_key] = names
    if size or mtime_ns:
        _persistent_blend_images()[path_key] = {
            "size": size,
            "mtime_ns": mtime_ns,
            "names": sorted(names),
            "indexed_by_blender": bool(authoritative),
        }
        _PERSISTENT_BLEND_IMAGES_DIRTY = True
    return names


def ensure_blend_source_index(cfg):
    """One-time Blender index for compressed blends; later audits use the cache."""
    atlas_root = Path(cfg.get("atlas_root", ""))
    blender = Path(cfg.get("blender_exe", ""))
    script = Path(__file__).resolve().parent / "jobs" / "index_leaf_blend_sources.py"
    if not atlas_root.is_dir() or not blender.is_file() or not script.is_file():
        return {"indexed": 0, "reason": "index prerequisites unavailable"}

    pending = []
    persistent = _persistent_blend_images()
    for blend in atlas_root.glob("*.blend"):
        if not blend.is_file() or is_backup_path(blend):
            continue
        path_key, size, mtime_ns = _file_cache_key(blend)
        entry = persistent.get(path_key)
        if not entry or entry.get("size") != size \
                or entry.get("mtime_ns") != mtime_ns \
                or not entry.get("indexed_by_blender"):
            pending.append(blend)
    if not pending:
        return {"indexed": 0, "cached": True}

    cache_dir = BLEND_IMAGE_CACHE_PATH.parent
    cache_dir.mkdir(parents=True, exist_ok=True)
    report_path = cache_dir / f".blend_source_index_{os.getpid()}.json"
    cmd = [
        str(blender), "--factory-startup", "--background",
        "--python", str(script), "--",
        "--root", str(atlas_root), "--out", str(report_path),
    ]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=cfg.get("atlas_job_timeout", 1800),
            creationflags=0x08000000,
        )
        if result.returncode != 0 or not report_path.is_file():
            return {"indexed": 0, "error": (result.stderr or result.stdout)[-500:]}
        rows = json.loads(report_path.read_text(encoding="utf-8"))
        indexed = 0
        for row in rows:
            if row.get("error"):
                continue
            register_blend_source_images(
                row.get("blend"), row.get("images", []), authoritative=True)
            indexed += 1
        return {"indexed": indexed, "pending": len(pending)}
    except Exception as exc:
        return {"indexed": 0, "error": str(exc)}
    finally:
        if report_path.exists():
            report_path.unlink()


def find_atlas_blends(atlas_root, folder, atlas_base, source_images=None,
                      aliases=None):
    """Find a completed leaf blend by output name or embedded source image."""
    roots = [Path(atlas_root), Path(folder)]
    needles = [atlas_base.lower()]
    # Also allow existing files with small case differences such as M_Leaf_*.
    parts = [p for p in atlas_base.lower().split("_") if p not in {"m", "01"}]
    candidates = []
    for root in roots:
        if not root.exists():
            continue
        for path in root.glob("*.blend"):
            if is_backup_path(path):
                continue
            candidates.append(path)
    alias_stems = {
        (name if str(name).lower().startswith("m_") else "M_" + str(name)).lower()
        for name in (aliases or []) if name
    }
    exact = [path for path in candidates if path.stem.lower() in needles]
    if exact:
        return unique([str(path) for path in exact])
    alias_matches = [path for path in candidates if path.stem.lower() in alias_stems]
    if alias_matches:
        return unique([str(path) for path in alias_matches])
    fuzzy = [
        path for path in candidates
        if path.stem.lower().startswith("m_leaf_")
        and all(part in path.stem.lower() for part in parts)
    ]
    if fuzzy:
        return unique([str(path) for path in fuzzy])
    source_names = {
        Path(value).name.lower() for value in (source_images or ()) if value
    }
    source_families = {
        source_family_name(value).lower()
        for value in (source_images or ()) if value
    }
    source_matches = []
    if source_names:
        for path in candidates:
            # Source-image matching is for sharing an already canonical leaf
            # atlas across SPMs.  Do not legitimize legacy blends whose names
            # were copied from TCom/Megascan source filenames.
            if not path.stem.lower().startswith("m_leaf_"):
                continue
            embedded_names = _blend_image_names(path)
            if any(
                embedded.endswith(source_name)
                or source_family_name(embedded).lower() in source_families
                for embedded in embedded_names for source_name in source_names
            ):
                source_matches.append(path)
    if not source_matches:
        return []
    folder_tokens = [
        token for token in re.findall(r"[a-z0-9]+", Path(folder).name.lower())
        if token not in {"tree", "bush", "weed"} and len(token) > 3
    ]
    ranked = sorted(
        source_matches,
        key=lambda path: (
            -sum(len(token) for token in folder_tokens
                 if token in path.stem.lower()),
            path.name.lower(),
        ),
    )
    return [str(ranked[0])]


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
    try:
        signature = texture_dir.stat().st_mtime_ns
    except OSError:
        signature = 0
    cache_key = str(texture_dir).lower()
    cached = _DIRECTORY_FILE_INDEX_CACHE.get(cache_key)
    if cached is None or cached[0] != signature:
        low_files = {
            path.name.lower(): path
            for path in texture_dir.iterdir()
            if path.is_file() and not is_backup_path(path)
        }
        _DIRECTORY_FILE_INDEX_CACHE[cache_key] = (signature, low_files)
    else:
        low_files = cached[1]
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
            matches = [p for p in low_files.values() if p.stem.lower() == prefix]
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
    """Collect managed graphs and index common aliases by their canonical T_ name."""
    graphs = {}
    for sbs in sbs_files:
        for name in _cached_sbs_graph_names(sbs):
            pair = (name, str(sbs))
            is_current = name.lower().startswith("t_")
            if is_current:
                graphs[name.lower()] = pair
            else:
                graphs.setdefault(name.lower(), pair)
            material_name = (
                "M_" + name[2:]
                if name.lower().startswith("t_") else name
            )
            canonical = canonical_material_name(material_name)
            canonical_keys = (
                canonical.lower(),
                texture_base_for_material(canonical).lower(),
            )
            for key in canonical_keys:
                if is_current:
                    graphs[key] = pair
                else:
                    graphs.setdefault(key, pair)
    return graphs


def auto_split_atlas_base(name, blend_stems, graphs):
    """Atlas base material for an Auto Split-suffixed name, else None.

    Unlike leaf-source provenance this works from the name alone, so a
    normalized SK (whose slots now point at managed T_ outputs instead of
    the original leaf sources) still groups M_x_atlas_01_green under
    M_x_atlas_01 instead of spawning a texture set for the suffixed name.
    """
    low = str(name).lower()
    for stem_low, blend in blend_stems.items():
        if low.startswith(stem_low + "_") and low[len(stem_low) + 1:] in AUTO_SPLIT_SUFFIXES:
            return blend.stem
    for graph_low, (graph, _sbs) in graphs.items():
        if low.startswith(graph_low + "_") and low[len(graph_low) + 1:] in AUTO_SPLIT_SUFFIXES:
            return "M_" + graph[2:] if graph.lower().startswith("t_") else graph
    return None


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


def material_color_alpha_refs(refs):
    """Read SpeedTree's first (Color) slot and explicitly named opacity slot."""
    albedo = [refs[0]] if refs else []
    alpha = []
    for ref in refs[1:]:
        low = Path(ref).name.lower()
        if any(word in low for word in ALPHA_WORDS):
            alpha.append(ref)
    return unique(albedo), unique(alpha)


def legacy_export_maps_from_refs(spm, refs, material_base, required_maps):
    """Find old M_* managed outputs referenced by a material for T_ migration."""
    result = {map_name: None for map_name in required_maps}
    base = str(material_base).lower()
    for ref in refs:
        stem = Path(ref).stem.lower()
        for map_name in required_maps:
            if stem != f"{base}_{map_name}":
                continue
            path = resolve_spm_image_ref(spm, ref)
            if path_exists(path):
                result[map_name] = str(path)
    return result


def _refs_are_only_managed_outputs(refs):
    refs = [ref for ref in refs if ref]
    return bool(refs) and all(GENERATED_EXPORT_RE.match(Path(ref).name) for ref in refs)


def _source_material_ref_map(folder, sk_spm):
    """Original material refs for one SK copy, keyed by canonical name.

    An SK may have been copied from an ``SM_`` authoring variant while an
    older unsuffixed source with the same tail also exists. Prefer that SM_
    peer, then the exact non-SK peer, and finally fill missing material names
    from the remaining sources. This recovers original connections after an
    earlier pass replaced the SK slots with managed T_ outputs.
    """
    folder = Path(folder)
    expected = folder / sk_spm.name[3:] if sk_spm.name.lower().startswith("sk_") else None
    tail = sk_spm.stem[3:] if sk_spm.name.lower().startswith("sk_") else sk_spm.stem
    sm_expected = folder / f"SM_{tail}.spm"
    candidates = unique(
        [path for path in (sm_expected, expected) if path and path.is_file()]
        + source_spms(folder)
    )
    result = {}
    for source in candidates:
        for row in extract_material_image_refs(source):
            key = canonical_material_name(row.get("material_name")).lower()
            refs = row.get("refs") or []
            if key and refs and key not in result:
                result[key] = list(refs)
    return result


def is_cluster_render_material(folder, spm, refs):
    """True when the authoritative Color slot is a derived SpeedTree cluster render."""
    if not refs:
        return False
    color = resolve_spm_image_ref(spm, refs[0])
    if _is_under(color, [Path(folder) / "cluster"]):
        return True
    # Cluster cards are frequently shared across neighbouring asset folders
    # (for example River Birch using tree_birch_paper\cluster\branch...).
    # The semantic boundary is the Cluster directory itself, not ownership by
    # the current asset folder.
    return any(part.lower() == "cluster" for part in Path(color).parts[:-1])


def cluster_render_source_definitions(folder):
    """Unique authoritative cluster-slot definition for each material in a folder."""
    folder = Path(folder)
    variants = {}
    for source_spm in source_spms(folder):
        active = active_material_ids(source_spm)
        for row in extract_material_image_refs(source_spm):
            if active and row.get("material_id") not in active:
                continue
            refs = row.get("refs") or []
            if not is_cluster_render_material(folder, source_spm, refs):
                continue
            canonical = canonical_material_name(row.get("material_name"))
            signature = tuple(
                str(resolve_spm_image_ref(source_spm, ref)).lower() for ref in refs)
            variants.setdefault(canonical.lower(), {}).setdefault(signature, {
                "source_spm": str(source_spm),
                "material_name": canonical,
                "source_refs": list(refs),
            })
    return {
        canonical: next(iter(definitions.values()))
        for canonical, definitions in variants.items()
        if len(definitions) == 1
    }


def cluster_render_definitions_from_spm(folder, source_spm):
    """Unambiguous cluster definitions inside one exact non-SK counterpart."""
    variants = {}
    active = active_material_ids(source_spm)
    for row in extract_material_image_refs(source_spm):
        if active and row.get("material_id") not in active:
            continue
        refs = row.get("refs") or []
        if not is_cluster_render_material(folder, source_spm, refs):
            continue
        canonical = canonical_material_name(row.get("material_name"))
        signature = tuple(
            str(resolve_spm_image_ref(source_spm, ref)).lower() for ref in refs)
        variants.setdefault(canonical.lower(), {}).setdefault(signature, {
            "source_spm": str(source_spm),
            "material_name": canonical,
            "source_refs": list(refs),
        })
    return {
        canonical: next(iter(definitions.values()))
        for canonical, definitions in variants.items()
        if len(definitions) == 1
    }


def preserved_cluster_materials(folder, definitions=None):
    """Map every current SK use to the folder's unique cluster-render definition."""
    folder = Path(folder)
    definitions = definitions if definitions is not None \
        else cluster_render_source_definitions(folder)
    preserved = []
    for sk_spm in preferred_sk_spms(folder):
        exact_source = folder / sk_spm.name[3:]
        exact_definitions = (
            cluster_render_definitions_from_spm(folder, exact_source)
            if exact_source.is_file() else {})
        sk_active = active_material_ids(sk_spm)
        for row in extract_material_image_refs(sk_spm):
            if sk_active and row.get("material_id") not in sk_active:
                continue
            canonical = canonical_material_name(row.get("material_name")).lower()
            definition = exact_definitions.get(canonical) or definitions.get(canonical)
            if not definition:
                continue
            preserved.append({
                "spm": str(sk_spm),
                **definition,
                "reason": "folder-wide authoritative Color slot is under the asset cluster folder",
            })
    return preserved


def _leaf_source_for_active_material(spm, refs, leaf_mesh_sources):
    """Resolve an active material to its atlas by texture provenance only."""
    albedo_refs, alpha_refs = material_color_alpha_refs(refs)
    if albedo_refs and alpha_refs:
        signature = (
            str(resolve_spm_image_ref(spm, albedo_refs[0])).lower(),
            str(resolve_spm_image_ref(spm, alpha_refs[0])).lower(),
        )
        for source in leaf_mesh_sources or []:
            source_signature = (
                str(Path(source.get("albedo", "")).resolve()).lower(),
                str(Path(source.get("alpha", "")).resolve()).lower(),
            )
            if signature == source_signature:
                return source

    # Idempotent reruns: once normalized, the SPM points at T_* outputs rather
    # than the original atlas. Recover the same provenance from that T_* base.
    managed_bases = {
        re.sub(
            r"_(?:color|normal|extra|height|opacity|subsurface)$", "",
            Path(ref).stem, flags=re.IGNORECASE).lower()
        for ref in refs
        if GENERATED_EXPORT_RE.match(Path(ref).name)
    }
    for source in leaf_mesh_sources or []:
        if texture_base_for_material(source.get("atlas_base", "")).lower() in managed_bases:
            return source
    return None


def material_texture_items(folder, cfg, tex_dirs, graphs, preserved_definitions=None,
                           leaf_mesh_sources=None):
    """One managed texture-set job for every material used by a Generator."""
    spms = preferred_sk_spms(folder) or source_spms(folder)
    preserved_definitions = preserved_definitions if preserved_definitions is not None \
        else cluster_render_source_definitions(folder)
    preserved_names = set(preserved_definitions)
    name_spm_pairs = []
    for spm in spms:
        active_ids = active_material_ids(spm)
        original_refs = _source_material_ref_map(folder, spm) if spm.name.lower().startswith("sk_") else {}
        for row in extract_material_image_refs(spm):
            if active_ids and row.get("material_id") not in active_ids:
                continue
            refs = row["refs"]
            current_is_managed = _refs_are_only_managed_outputs(refs)
            leaf_source = _leaf_source_for_active_material(
                spm, refs, leaf_mesh_sources or [])
            canonical = canonical_material_name(row["material_name"]).lower()
            # SK files often point only at old M_/T_ Unreal outputs.  Those
            # are not source inputs; recover the same material's references
            # from the original SPM copy instead.
            if leaf_source:
                refs = list(leaf_source.get("source_refs") or [
                    leaf_source.get("albedo"), leaf_source.get("alpha")])
                refs = [str(ref) for ref in refs if ref]
            elif current_is_managed and original_refs.get(canonical):
                refs = original_refs[canonical]
            name_spm_pairs.append((
                row["material_name"], spm, refs, current_is_managed, leaf_source))
    blend_stems = atlas_blend_stems(cfg)
    items = []
    by_base = {}
    for name, spm, refs, current_is_managed, leaf_source in name_spm_pairs:
        if not name:
            continue
        if canonical_material_name(name).lower() in preserved_names:
            continue
        # A cluster-folder image is already a derived SpeedTree cluster render.
        # It is not an atlas/source texture to feed through SBS a second time.
        if is_cluster_render_material(folder, spm, refs):
            continue
        base = (leaf_source["atlas_base"] if leaf_source
                else auto_split_atlas_base(name, blend_stems, graphs)
                or canonical_material_name(name))
        key = base.lower()
        texture_base = texture_base_for_material(base)
        graph = graphs.get(texture_base.lower()) or graphs.get(key)
        # A default SpeedTree name is normally too ambiguous to create a new
        # texture set from.  It is still a real normalization target when the
        # active SK slot already points only at a managed T_ output set and a
        # matching SBS graph exists.  This keeps existing Material 2 / Copy
        # slots covered without inventing name-based exceptions.
        if GENERIC_MATERIAL_NAME_RE.match(name) and not (current_is_managed and graph):
            continue
        source_albedo, source_alpha = material_color_alpha_refs(refs)
        referenced_legacy = legacy_export_maps_from_refs(
            spm, refs, name, cfg["required_export_maps"])
        if key in by_base:
            if name not in by_base[key]["material_names"]:
                by_base[key]["material_names"].append(name)
            spm_text = str(spm)
            if spm_text not in by_base[key]["material_spms"]:
                by_base[key]["material_spms"].append(spm_text)
            by_base[key]["source_refs"] = unique(
                by_base[key]["source_refs"] + refs)
            by_base[key]["source_albedo"] = unique(
                by_base[key]["source_albedo"] + source_albedo)
            by_base[key]["source_alpha"] = unique(
                by_base[key]["source_alpha"] + source_alpha)
            for map_name, path in referenced_legacy.items():
                if path and not by_base[key]["legacy_export_maps"].get(map_name):
                    by_base[key]["legacy_export_maps"][map_name] = path
            continue
        maps_dir, export_maps = find_export_maps_multi(
            tex_dirs, texture_base, cfg["required_export_maps"])
        _legacy_dir, legacy_export_maps = find_export_maps_multi(
            tex_dirs, base, cfg["required_export_maps"])
        for map_name, path in referenced_legacy.items():
            if path and not legacy_export_maps.get(map_name):
                legacy_export_maps[map_name] = path
        entry = {
            "cluster_spm": None,
            "name": base,
            "source": "material",
            "material_names": [name],
            "material_spms": [str(spm)],
            "atlas_base": base,
            "texture_base": texture_base,
            "is_atlas": bool(leaf_source)
                        or bool(material_atlas_base(name, blend_stems, graphs))
                        or any("cluster\\" in str(ref).lower() for ref in refs),
            "needs_leaf_mesh": False,
            "atlas_blends": list(leaf_source.get("atlas_blends") or [])
                             if leaf_source else [],
            "export_maps": export_maps,
            "missing_export_maps": [k for k, v in export_maps.items() if not v],
            "legacy_export_maps": legacy_export_maps,
            "texture_dir": str(maps_dir) if maps_dir else None,
            "m_graph": graph[0] if graph else None,
            "m_graph_sbs": graph[1] if graph else None,
            "legacy_m_graph": bool(graph and graph[0].lower().startswith("m_")),
            "source_refs": list(refs),
            "source_albedo": source_albedo,
            "source_alpha": source_alpha,
            "leaf_source_provenance": bool(leaf_source),
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
    materials = visible_material_names(chosen) if chosen else []
    missing_m = [
        m for m in materials
        if m and not m.startswith("M_")
    ]
    renames_needed = material_rename_plan(chosen) if chosen else []
    sbs_files = active_sbs_files(folder)
    texture_dir = sbs_files[0].parent if sbs_files else (folder / "texture")
    tex_dirs = texture_dir_candidates(folder, sbs_files) or [texture_dir]
    folder_graphs = folder_m_graph_names(sbs_files)
    clusters = cluster_spms(folder)
    target_spms = preferred or loose or sources
    leaf_mesh_sources, referenced_clusters = discover_leaf_mesh_sources(
        folder, cfg, target_spms, clusters)
    # ③은 아틀라스 파일 개수가 아니라 실제 Generator가 사용하는 모든
    # M_ 머티리얼을 기준으로 한 행씩 만든다. Cluster SPM의 미사용 테스트
    # 머티리얼은 이 목록에 들어오지 않는다.
    cluster_definitions = cluster_render_source_definitions(folder)
    cluster_items = material_texture_items(
        folder, cfg, tex_dirs, folder_graphs,
        preserved_definitions=cluster_definitions,
        leaf_mesh_sources=leaf_mesh_sources)
    ignored_cluster_spms = [
        str(cluster) for cluster in clusters
        if str(cluster).lower() not in referenced_clusters
    ]
    all_refs = []
    if include_refs:
        relevant_clusters = [
            Path(path) for path in referenced_clusters
        ]
        for path in ([chosen] if chosen else []) + relevant_clusters + sbs_files:
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
        "material_renames_needed": renames_needed,
        "sbs_files": [str(p) for p in sbs_files],
        "texture_dir": str(texture_dir),
        "texture_dirs": [str(d) for d in tex_dirs],
        "m_graph_names": {low: list(pair) for low, pair in folder_graphs.items()},
        "cluster_items": cluster_items,
        "preserved_cluster_materials": preserved_cluster_materials(
            folder, definitions=cluster_definitions),
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
    if item.get("material_renames_needed") or item["materials_missing_m_prefix"]:
        if status != "needs_sk":
            status = "needs_m_prefix"
        actions.append("머티리얼 이름 M_/공용 이름 정리 필요")
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


def global_m_graph_names(items, cfg):
    """Find material graphs in tree-local and shared source-texture SBS files."""
    paths = []
    for item in items:
        paths.extend(Path(path) for path in item.get("sbs_files") or [])
    roots = [Path(cfg.get("tree_root", ""))]
    roots.extend(Path(path) for path in cfg.get("source_texture_roots", []))
    for root in roots:
        if not root.exists():
            continue
        paths.extend(
            path for path in root.rglob("*.sbs")
            if path.is_file() and not is_backup_path(path)
            and ".autosave" not in str(path).lower()
        )
    return folder_m_graph_names(unique(paths))


def attach_global_m_graphs(items, cfg):
    """Attach canonical shared SBS graphs without treating old M_ outputs as inputs."""
    graphs = global_m_graph_names(items, cfg)
    changed = set()
    for item in items:
        for entry in item.get("cluster_items") or []:
            graph = (
                graphs.get(str(entry.get("texture_base", "")).lower())
                or graphs.get(str(entry.get("atlas_base", "")).lower())
            )
            if not graph:
                continue
            graph_name, graph_sbs = graph
            texture_dir = Path(graph_sbs).parent
            export_maps = find_export_maps(
                texture_dir, entry.get("texture_base") or entry["atlas_base"],
                cfg["required_export_maps"])
            entry.update(
                m_graph=graph_name,
                m_graph_sbs=graph_sbs,
                legacy_m_graph=graph_name.lower().startswith("m_"),
                texture_dir=str(texture_dir),
                export_maps=export_maps,
                missing_export_maps=[name for name, path in export_maps.items() if not path],
            )
            changed.add(item["name"])
    items_by_name = {item["name"]: item for item in items}
    for name in changed:
        derive_status_actions(items_by_name[name])
    return graphs


def resolve_shared_atlas_entries(items, cfg):
    """Choose one owner for each exact material name and suppress duplicate jobs."""
    def priority(entry):
        if entry.get("m_graph"):
            return 3
        if any((entry.get("export_maps") or {}).values()):
            return 2
        return 1

    owners = {}
    items_by_name = {item["name"]: item for item in items}
    candidates = [
        (priority(entry), item["name"], entry)
        for item in items for entry in item.get("cluster_items") or []
    ]
    for _rank, item_name, entry in sorted(
            candidates, key=lambda row: (-row[0], row[1].lower())):
        owners.setdefault(entry["atlas_base"].lower(), (item_name, entry))

    changed = set()
    for item in items:
        for entry in item.get("cluster_items") or []:
            owner_name, owner_entry = owners[entry["atlas_base"].lower()]
            if owner_name == item["name"] and owner_entry is entry:
                continue
            entry["shared_from"] = owner_name
            entry["export_maps"] = owner_entry.get("export_maps", {})
            entry["missing_export_maps"] = owner_entry.get("missing_export_maps", [])
            entry["m_graph"] = owner_entry.get("m_graph")
            entry["m_graph_sbs"] = owner_entry.get("m_graph_sbs")
            entry["legacy_m_graph"] = owner_entry.get("legacy_m_graph", False)
            entry["texture_dir"] = owner_entry.get("texture_dir")
            changed.add(item["name"])
    for name in changed:
        derive_status_actions(items_by_name[name])
    return sorted(changed)


def make_report(cfg, targets=None, include_refs=False, pcg_targets=None):
    ensure_blend_source_index(cfg)
    folders = candidate_folders(cfg, targets, pcg_targets=pcg_targets)
    items = [audit_folder(folder, cfg, include_refs=include_refs) for folder in folders]
    attach_global_m_graphs(items, cfg)
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
    save_spm_analysis_cache()
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
