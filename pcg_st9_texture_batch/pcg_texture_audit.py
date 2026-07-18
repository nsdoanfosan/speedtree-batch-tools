"""Audit PCG/ST9 SpeedTree texture preparation state.

This is intentionally conservative. It reads filesystem/SPM/SBS evidence and
only mutates files when --prepare-sk is passed.
"""
import argparse
import contextvars
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

BATCH_TOOLS_DIR = Path(__file__).resolve().parent.parent
if str(BATCH_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(BATCH_TOOLS_DIR))
from speedtree_texture_contract import parse_managed_texture_path, resolve_texture_set
from speedtree_pipeline_contract import read_spm_text as read_pipeline_spm_text

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
else:
    from .pcg_texture_common import (
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
GENERATOR_TYPE_RE = re.compile(
    r'<Generator\b[^>]*\bType="([^"]*)"', re.IGNORECASE)
GENERATOR_NAME_RE = re.compile(
    r"<Name\b[^>]*>(.*?)</Name>", re.IGNORECASE | re.DOTALL)
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
NODE_EXPORT_STATE_RE = re.compile(
    r"<Node\b[^>]*>\s*"
    r"<GeneratorGUID>([^<]*)</GeneratorGUID>\s*"
    r"<ParentGUID>[^<]*</ParentGUID>\s*"
    r"<Name>[^<]*</Name>\s*"
    r"<GUID>[^<]*</GUID>\s*"
    r"<Hidden>([^<]*)</Hidden>\s*"
    r"<Extra>(.*?)</Extra>",
    re.IGNORECASE | re.DOTALL,
)
NODE_DELETED_RE = re.compile(r"<m_bDeleted>([^<]*)</m_bDeleted>", re.IGNORECASE)
NODE_CULLED_RE = re.compile(r"<m_bCulled>([^<]*)</m_bCulled>", re.IGNORECASE)
CUTOUT_MESH_ID_RE = re.compile(
    r"<CutoutMeshID\b[^>]*>([^<]*)</CutoutMeshID>", re.IGNORECASE)
SUPPLEMENTAL_CUTOUT_BLOCK_RE = re.compile(
    r"<SupplementalCutoutMeshIDs\b[^>]*>.*?</SupplementalCutoutMeshIDs>",
    re.IGNORECASE | re.DOTALL,
)
SUPPLEMENTAL_CUTOUT_ID_RE = re.compile(
    r'<CutoutMesh\b[^>]*\bID="([^"]+)"', re.IGNORECASE)
MESH_ASSET_ID_RE = re.compile(
    r'<Mesh\b[^>]*\bID="([^"]+)"', re.IGNORECASE)
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
ALPHA_WORDS = ("alpha", "opacity", "transparency", "mask")
ATLAS_LEAF_BUILDER_MARKER = '"generator":"Atlas Leaf Mesh Builder"'
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
# v4 additionally stores semantic Frond/Leaf Mesh bindings and material mesh IDs.
SPM_ANALYSIS_CACHE_PATH = REPORT_DIR / "_cache" / "spm_analysis_v4.json"
SBS_GRAPH_CACHE_PATH = REPORT_DIR / "_cache" / "sbs_graph_names_v1.json"
BLEND_IMAGE_CACHE_PATH = REPORT_DIR / "_cache" / "blend_image_names_v1.json"
_PERSISTENT_SBS_GRAPHS = None
_PERSISTENT_SBS_GRAPHS_DIRTY = False
_PERSISTENT_BLEND_IMAGES = None
_PERSISTENT_BLEND_IMAGES_DIRTY = False
_DIRECTORY_FILE_INDEX_CACHE = {}
_BLEND_IMAGE_NAMES_CACHE = {}
_IMAGE_EQUAL_CACHE = {}
_REPORT_SCAN_CACHE = contextvars.ContextVar("pcg_report_scan_cache", default=None)
COMMON_BARK_END_RE = re.compile(
    r"^m_bark_common_(?!end_).+_end_0*(\d+)$", re.IGNORECASE)
GENERIC_MATERIAL_NAME_RE = re.compile(
    r"^(?:m_)?material(?:\s+copy)?(?:\s*\d+)?$", re.IGNORECASE)


def read_maybe_gzip_text(path):
    try:
        return read_pipeline_spm_text(path)
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


def _report_scan_cached(func):
    """Give one report a stable, thread-safe snapshot of repeated filesystem reads."""
    @functools.wraps(func)
    def wrapped(*args, **kwargs):
        token = _REPORT_SCAN_CACHE.set({
            "file_cache_keys": {},
            "root_spms": {},
            "path_exists": {},
        })
        try:
            return func(*args, **kwargs)
        finally:
            _REPORT_SCAN_CACHE.reset(token)
    return wrapped


def _file_cache_key(path):
    path = Path(path)
    path_key = str(path).lower()
    report_cache = _REPORT_SCAN_CACHE.get()
    if report_cache is not None:
        cached = report_cache["file_cache_keys"].get(path_key)
        if cached is not None:
            return cached
    try:
        stat = path.stat()
        result = path_key, stat.st_size, stat.st_mtime_ns
    except OSError:
        result = path_key, 0, 0
    if report_cache is not None:
        report_cache["file_cache_keys"][path_key] = result
    return result


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


def _export_node_counts_from_text(text):
    """Count generated, non-hidden nodes by Generator GUID.

    A Generator can keep its eye enabled while belonging to a detached graph
    component that currently produces no nodes. SpeedTree omits that
    Generator's materials from FBX/STMAT, so graph visibility alone is not
    export participation evidence.
    """
    counts = {}
    total_nodes = 0
    for match in NODE_EXPORT_STATE_RE.finditer(text):
        total_nodes += 1
        guid = html.unescape(match.group(1).strip())
        hidden = match.group(2).strip().casefold() in {"1", "true", "yes"}
        extra = match.group(3)
        deleted_match = NODE_DELETED_RE.search(extra)
        culled_match = NODE_CULLED_RE.search(extra)
        deleted = bool(
            deleted_match
            and deleted_match.group(1).strip().casefold() in {"1", "true", "yes"}
        )
        culled = bool(
            culled_match
            and culled_match.group(1).strip().casefold() in {"1", "true", "yes"}
        )
        if guid and not hidden and not deleted and not culled:
            counts[guid] = counts.get(guid, 0) + 1
    return counts, total_nodes


def _visible_material_ids_from_text(text, export_node_counts=None, total_nodes=0):
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
    for generator_index, block_match in enumerate(
            GENERATOR_BLOCK_RE.finditer(text)):
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
        graph_visible = not guid or not effectively_hidden(guid)
        has_export_nodes = (
            not total_nodes
            or not guid
            or bool((export_node_counts or {}).get(guid, 0))
        )
        if graph_visible and has_export_nodes:
            active |= ids
    return active


def _normalized_generator_type(value):
    value = re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()
    return value


def _is_leaf_mesh_generator_type(value):
    normalized = _normalized_generator_type(value)
    return normalized == "frond" or normalized.replace(" ", "") == "leafmesh"


def _leaf_generator_bindings_from_text(
    text, export_node_counts=None, total_nodes=0
):
    """Return material/mesh slot pairs owned by semantic leaf generators.

    Texture filenames and material names are intentionally not part of this
    decision. A source is a leaf-card source only when a ``Frond`` or
    ``Leaf Mesh`` Generator points at its Material ID.
    """
    generators = []
    hidden_by_guid = {}
    for generator_index, block_match in enumerate(
            GENERATOR_BLOCK_RE.finditer(text)):
        block = block_match.group(0)
        guid_match = ELEMENT_GUID_RE.search(block)
        hidden_match = ELEMENT_HIDDEN_RE.search(block)
        guid = guid_match.group(1).strip() if guid_match else ""
        own_hidden = bool(
            hidden_match and hidden_match.group(1).strip().lower() == "true")
        if guid:
            hidden_by_guid[guid] = own_hidden
        type_match = GENERATOR_TYPE_RE.search(block)
        generator_type = html.unescape(type_match.group(1).strip()) \
            if type_match else ""
        if not _is_leaf_mesh_generator_type(generator_type):
            continue
        header = block.split("<Properties", 1)[0]
        name_match = GENERATOR_NAME_RE.search(header)
        generator_name = html.unescape(name_match.group(1).strip()) \
            if name_match else ""
        properties = []
        for property_match in PROPERTY_BLOCK_RE.finditer(block):
            property_block = property_match.group(0)
            property_name = PROPERTY_NAME_RE.search(property_block)
            property_value = PROPERTY_VALUE_RE.search(property_block)
            if not property_name or not property_value:
                continue
            properties.append((
                html.unescape(property_name.group(1).strip()),
                html.unescape(property_value.group(1).strip()),
            ))
        generators.append({
            "generator_index": generator_index,
            "guid": guid,
            "own_hidden": own_hidden,
            "generator_type": generator_type,
            "generator_name": generator_name,
            "properties": properties,
        })

    parent = {}
    for link_match in GENERATOR_LINK_RE.finditer(text):
        block = link_match.group(0)
        source = LINK_SOURCE_GUID_RE.search(block)
        target = LINK_TARGET_GUID_RE.search(block)
        if source and target:
            parent[target.group(1).strip()] = source.group(1).strip()

    def effectively_hidden(generator):
        if generator["own_hidden"]:
            return True
        guid = generator["guid"]
        seen = set()
        while guid and guid not in seen:
            seen.add(guid)
            if hidden_by_guid.get(guid):
                return True
            guid = parent.get(guid, "")
        return False

    bindings = []
    for generator in generators:
        graph_visible = not effectively_hidden(generator)
        generated_node_count = int(
            (export_node_counts or {}).get(generator["guid"], 0)
        )
        export_participates = bool(
            graph_visible
            and (
                not total_nodes
                or not generator["guid"]
                or generated_node_count > 0
            )
        )
        by_name = {name.lower(): (name, value)
                   for name, value in generator["properties"]}
        for property_name, material_id in generator["properties"]:
            if not property_name.lower().endswith(":material"):
                continue
            prefix = property_name[:-len(":Material")]
            mesh_pair = by_name.get((prefix + ":Mesh").lower())
            bindings.append({
                "generator_index": generator["generator_index"],
                "generator_guid": generator["guid"],
                "generator_type": generator["generator_type"],
                "generator_name": generator["generator_name"],
                "material_property": property_name,
                "slot_prefix": prefix,
                "material_id": material_id,
                "mesh_property": mesh_pair[0] if mesh_pair else "",
                "mesh_id": mesh_pair[1] if mesh_pair else "",
                # ``visible`` remains the compatibility field consumed by the
                # SK contract. It now means actual export participation, not
                # merely an enabled eye icon.
                "visible": export_participates,
                "graph_visible": graph_visible,
                "generated_node_count": generated_node_count,
                "export_participates": export_participates,
            })
    return bindings


def _material_cutout_mesh_ids(block):
    mesh_ids = []
    cutout = CUTOUT_MESH_ID_RE.search(block)
    if cutout and cutout.group(1).strip() not in {"", "-1"}:
        mesh_ids.append(cutout.group(1).strip())
    supplemental = SUPPLEMENTAL_CUTOUT_BLOCK_RE.search(block)
    if supplemental:
        mesh_ids.extend(
            match.group(1).strip()
            for match in SUPPLEMENTAL_CUTOUT_ID_RE.finditer(supplemental.group(0))
            if match.group(1).strip() not in {"", "-1"}
        )
    return unique(mesh_ids)


def _material_is_managed_leaf_output(block):
    low = block.lower()
    return (ATLAS_LEAF_BUILDER_MARKER.lower() in low
            or ("atlas leaf mesh builder" in low and '"generator"' in low))


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
            and disk_entry.get("mtime_ns") == mtime_ns \
            and disk_entry.get("leaf_binding_schema") == 3:
        analysis = {
            "material_rows": disk_entry.get("material_rows", []),
            "material_names": disk_entry.get("material_names", []),
            "active_material_ids": set(disk_entry.get("referenced_material_ids", [])),
            "visible_material_ids": set(disk_entry.get("visible_material_ids", [])),
            "leaf_generator_bindings": disk_entry.get(
                "leaf_generator_bindings", []),
            "mesh_asset_ids": set(disk_entry.get("mesh_asset_ids", [])),
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
            "cutout_mesh_ids": _material_cutout_mesh_ids(block),
            "managed_leaf_output": _material_is_managed_leaf_output(block),
        })

    referenced = _referenced_material_ids_from_text(text)
    export_node_counts, total_nodes = _export_node_counts_from_text(text)
    visible = _visible_material_ids_from_text(
        text, export_node_counts=export_node_counts, total_nodes=total_nodes
    )
    leaf_bindings = _leaf_generator_bindings_from_text(
        text, export_node_counts=export_node_counts, total_nodes=total_nodes
    )
    mesh_assets = {
        match.group(1).strip()
        for match in MESH_ASSET_ID_RE.finditer(text)
        if match.group(1).strip() not in {"", "-1"}
    }

    analysis = {
        "material_rows": rows,
        "material_names": unique(
            row["material_name"] for row in rows if row["material_name"]),
        # Keep active_material_ids as the legacy all-reference set. Cluster
        # provenance needs hidden source generators too; final jobs use the
        # separate visible_material_ids set below.
        "active_material_ids": referenced,
        "visible_material_ids": visible,
        "leaf_generator_bindings": leaf_bindings,
        "mesh_asset_ids": mesh_assets,
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
            "leaf_generator_bindings": leaf_bindings,
            "mesh_asset_ids": sorted(mesh_assets),
            "leaf_binding_schema": 3,
        }
        _PERSISTENT_SPM_ANALYSIS_DIRTY = True
    return analysis


def canonical_material_name(name):
    """Normalize shared bark-end aliases; preserve tint-specific stem names."""
    name = str(name or "").strip()
    prefixed = "M_" + name[2:] if name.lower().startswith("m_") else "M_" + name
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
    folder = Path(folder)
    folder_key = str(folder).lower()
    report_cache = _REPORT_SCAN_CACHE.get()
    if report_cache is not None:
        cached = report_cache["root_spms"].get(folder_key)
        if cached is not None:
            return list(cached)
    paths = sorted(
        p for p in Path(folder).glob("*.spm")
        if p.is_file() and not is_backup_path(p)
    )
    if report_cache is not None:
        report_cache["root_spms"][folder_key] = tuple(paths)
    return paths


def preferred_sk_spms(folder):
    return [p for p in root_spms(folder) if p.name.lower().startswith("sk_")]


def loose_sk_spms(folder):
    return [p for p in root_spms(folder) if p.name.lower().startswith("sk")]


def source_spms(folder):
    return [p for p in root_spms(folder) if not p.name.lower().startswith("sk")]


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


def leaf_generator_bindings(path, visible_only=False):
    """Semantic Frond/Leaf Mesh Material+Mesh connections in one SPM."""
    bindings = _spm_analysis(path)["leaf_generator_bindings"]
    if visible_only:
        return [dict(row) for row in bindings if row.get("visible")]
    return [dict(row) for row in bindings]


def leaf_generator_material_ids(path, visible_only=False):
    return {
        row.get("material_id") for row in leaf_generator_bindings(
            path, visible_only=visible_only)
        if row.get("material_id") not in {None, "", "-1"}
    }


def mesh_asset_ids(path):
    """IDs of actual SpeedTree Mesh assets, excluding material cutout lists."""
    return set(_spm_analysis(path)["mesh_asset_ids"])


def managed_leaf_outputs(target_spms):
    """Connected Atlas Leaf Mesh Builder outputs in the exact target SPMs.

    A material name is not completion evidence.  The material must carry the
    builder's UserData marker, a semantic ``Leaf Mesh``/``Frond`` generator
    must point at its Material ID, and the matching Mesh slot must point at a
    cutout mesh owned by that same material.
    """
    outputs = []
    seen_spms = set()
    for spm in target_spms or []:
        spm = Path(spm)
        spm_key = os.path.abspath(str(spm)).lower()
        if spm_key in seen_spms:
            continue
        seen_spms.add(spm_key)

        rows = extract_material_image_refs(spm)
        managed_by_id = {
            str(row["material_id"]): row
            for row in rows
            if row.get("material_id") not in {None, "", "-1"}
            and row.get("managed_leaf_output")
        }
        if not managed_by_id:
            continue

        for material_id, material in managed_by_id.items():
            atlas_base = material.get("material_name", "")
            source_ids = []
            source_names = []
            expected_bindings = []
            for payload in _scoped_connection_payloads(spm, atlas_base):
                connection = payload.get("generator_connection") or {}
                source_ids.extend(connection.get("source_material_ids") or [])
                source_names.extend(connection.get("source_material_names") or [])
                for binding in connection.get("bindings") or []:
                    if not isinstance(binding, dict):
                        continue
                    target_id = binding.get("target_material_id")
                    if target_id not in {None, ""} \
                            and str(target_id) != material_id:
                        continue
                    source_id = binding.get("source_material_id")
                    if source_id not in {None, "", -1, "-1"}:
                        source_ids.append(str(source_id))
                    source_name = binding.get("source_material_name")
                    if source_name:
                        source_names.append(source_name)
                    expected_bindings.append(binding)

            # A connected slot alone cannot prove completion. The target-
            # specific builder manifest is the only durable record of every
            # source Generator slot/leaf ordinal that had to be replaced.
            if not expected_bindings:
                continue
            source_ids = unique(
                str(value) for value in source_ids
                if value not in {None, "", -1, "-1"})
            source_names = unique(source_names)
            if source_ids:
                source_names = unique(source_names + [
                    managed_by_id.get(source_id, {}).get("material_name", "")
                    for source_id in source_ids
                ])
            status = inspect_leaf_generator_connection(
                spm,
                source_material_names=source_names,
                source_material_ids=source_ids,
                atlas_base=atlas_base,
                expected_generator_bindings=expected_bindings,
            )
            if not status["generator_connection_complete"]:
                continue
            bindings = [
                binding for binding in status["managed_generator_bindings"]
                if str(binding.get("material_id") or "") == material_id
            ]
            if not bindings:
                continue
            outputs.append({
                "spm": str(spm),
                "material_id": material_id,
                "material_name": material.get("material_name", ""),
                "cutout_mesh_ids": list(material.get("cutout_mesh_ids") or []),
                "generator_bindings": bindings,
                "expected_slot_count": status["expected_slot_count"],
                "connected_slot_count": status["connected_slot_count"],
                "generator_connection_complete": True,
            })
    return outputs


@functools.lru_cache(maxsize=32768)
def _resolve_spm_image_ref_cached(base_text, ref_text):
    path_text = ref_text.replace("/", "\\")
    if not Path(path_text).is_absolute():
        path_text = os.path.join(base_text, path_text)
    # SPM texture references only need a stable absolute lexical path.  Using
    # Path.resolve() here needlessly probes every path (including missing
    # references) and dominates a full PCG audit on OneDrive-backed folders.
    return Path(os.path.abspath(os.path.normpath(path_text)))


def resolve_spm_image_ref(spm, ref):
    ref_text = str(ref).replace("/", "\\")
    ref_path = Path(ref_text)
    base_text = "" if ref_path.is_absolute() else str(Path(spm).parent)
    return _resolve_spm_image_ref_cached(base_text, ref_text)


def path_exists(path):
    path_key = str(Path(path)).lower()
    report_cache = _REPORT_SCAN_CACHE.get()
    if report_cache is not None and path_key in report_cache["path_exists"]:
        return report_cache["path_exists"][path_key]
    try:
        result = Path(path).exists()
    except (OSError, ValueError):
        result = False
    if report_cache is not None:
        report_cache["path_exists"][path_key] = result
    return result


def source_family_name(path):
    """Stable display/output name shared by all maps in one source atlas set."""
    stem = Path(path).stem
    stem = SOURCE_MAP_SUFFIX_RE.sub("", stem)
    stem = SOURCE_RESOLUTION_SUFFIX_RE.sub("", stem)
    stem = SOURCE_MAP_SUFFIX_RE.sub("", stem)
    return stem.strip("_-") or Path(path).stem


def leaf_sources_from_spm(spm, source_kind, excluded_albedo_stems=None, active_only=True):
    """Find coherent leaf albedo/alpha pairs used by one SPM material.

    A material qualifies only when a semantic ``Frond``/``Leaf Mesh``
    Generator references its Material ID. One material normally owns one
    atlas pair; family-name matching keeps extra maps in the same set.
    """
    results = []
    excluded_albedo_stems = {str(stem).lower() for stem in (excluded_albedo_stems or [])}
    # Hidden authoring generators are still valid provenance and may be
    # re-enabled later; the existing audit's ``active`` contract deliberately
    # retains them. Semantic Generator Type/ID, not eye state, is the gate.
    semantic_ids = leaf_generator_material_ids(spm, visible_only=False)
    if not semantic_ids:
        return results
    for row in extract_material_image_refs(spm):
        if row.get("material_id") not in semantic_ids:
            continue
        # Atlas Leaf Mesh Builder outputs can themselves become active leaf
        # materials after connection. They are results, never new inputs.
        if row.get("managed_leaf_output"):
            continue
        name = row["material_name"]
        refs = row["refs"]
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
        ]
        # An untagged source material may already point at normalized T_* maps
        # even though its generator still needs the Blender leaf mesh. Builder
        # outputs are excluded above by UserData ownership, so those managed
        # maps remain valid provenance without re-discovering generated assets.
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
                "source_material_ids": [row.get("material_id")]
                    if row.get("material_id") else [],
                "material_ids": [row.get("material_id")]
                    if row.get("material_id") else [],
                "source_refs": unique(family_refs),
            })
    return results


def _resolve_for_membership(path):
    """Path.resolve() with a per-report cache; each resolve is 1+ syscalls on
    Windows and _is_under re-resolves the same few roots thousands of times."""
    path_key = str(path).lower()
    report_cache = _REPORT_SCAN_CACHE.get()
    cache = None
    if report_cache is not None:
        cache = report_cache.setdefault("resolved_paths", {})
        cached = cache.get(path_key)
        if cached is not None:
            return cached
    try:
        resolved = Path(path).resolve()
    except (OSError, ValueError):
        resolved = Path(path).absolute()
    if cache is not None:
        cache[path_key] = resolved
    return resolved


def _is_under(path, roots):
    resolved = _resolve_for_membership(path)
    for root in roots:
        try:
            resolved.relative_to(_resolve_for_membership(root))
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
            "source_material_names": list(source.get("material_names") or []),
            "source_material_ids": list(
                source.get("source_material_ids")
                or source.get("material_ids") or []),
            "material_ids": list(
                source.get("material_ids")
                or source.get("source_material_ids") or []),
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
            existing["source_material_names"] = unique(
                existing.get("source_material_names", [])
                + target["source_material_names"])
            existing["source_material_ids"] = unique(
                existing.get("source_material_ids", [])
                + target["source_material_ids"])
            existing["material_ids"] = unique(
                existing.get("material_ids", []) + target["material_ids"])
        else:
            entry["targets"].append(target)
    results = list(grouped.values())
    assign_leaf_atlas_bases(results, folder, atlas_root=cfg.get("atlas_root"))
    for entry in results:
        material_names = unique(
            material_name
            for target in entry.get("targets", [])
            for material_name in target.get("material_names", [])
        )
        entry["atlas_blends"] = find_atlas_blends(
            cfg["atlas_root"], folder, entry["atlas_base"],
            source_images=(entry.get("albedo"),), aliases=material_names)
    annotate_leaf_generator_connections(results)
    return results


def _legacy_material_atlas_name(name):
    """Historical material-derived atlas name, kept for existing files."""
    canonical = canonical_material_name(name)
    match = re.match(r"^(.*?)(?:_atlas)?_0*(\d+)$", canonical, re.IGNORECASE)
    if match:
        return f"{match.group(1)}_atlas_{int(match.group(2)):02d}"
    return f"{canonical}_atlas_01"


def _material_leaf_atlas_name(name):
    """Name a newly generated leaf atlas without reusing cluster identity."""
    atlas_name = _legacy_material_atlas_name(name)
    return re.sub(
        r"^M_cluster_", "M_leaf_", atlas_name,
        count=1, flags=re.IGNORECASE)


def _folder_leaf_atlas_root(folder):
    """Fallback name for cluster internals that have only generic materials."""
    asset = Path(folder).name
    asset = re.sub(r"^(?:tree|bush|shrub|weed|grass)_", "", asset,
                   flags=re.IGNORECASE)
    safe = re.sub(r"[^A-Za-z0-9_-]+", "_", asset).strip("_-") or "asset"
    return f"M_leaf_{safe}_atlas"


def _atlas_source_pair_key(albedo, alpha):
    return (
        os.path.abspath(str(albedo or "")).lower(),
        os.path.abspath(str(alpha or "")).lower(),
    )


def _existing_atlas_registry(atlas_root, folder):
    """Index live blends, Blender backups, and scoped source manifests.

    ``.blend1`` is never considered runnable output, but its base remains
    reserved so a new, unrelated source pair cannot silently inherit it.
    """
    registry = {}
    roots = ([Path(atlas_root)] if atlas_root else []) + [Path(folder)]
    for root in unique(roots):
        root = Path(root)
        if not root.exists():
            continue
        report_cache = _REPORT_SCAN_CACHE.get()
        artifact_cache = report_cache.setdefault("atlas_artifacts", {}) \
            if report_cache is not None else None
        root_key = str(root).lower()
        artifacts = artifact_cache.get(root_key) \
            if artifact_cache is not None else None
        if artifacts is None:
            artifacts = []
            for pattern in ("*.blend", "*.blend1"):
                artifacts.extend(
                    path for path in root.glob(pattern) if path.is_file())
            if artifact_cache is not None:
                artifact_cache[root_key] = tuple(artifacts)
        for path in artifacts:
            entry = registry.setdefault(path.stem.lower(), {
                "base": path.stem, "live_blends": [], "backups": [],
                "source_pairs": [],
            })
            key = "live_blends" if path.suffix.lower() == ".blend" \
                else "backups"
            entry[key].append(str(path))

    scopes = Path(folder) / ".atlas_leaf_speedtree_scopes"
    if scopes.is_dir():
        for manifest_path in scopes.glob("*.json"):
            try:
                payload = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            blend_file = payload.get("blend_file")
            textures = payload.get("textures") or {}
            if not blend_file or not textures.get("albedo") or not textures.get("alpha"):
                continue
            base = Path(blend_file).stem
            entry = registry.setdefault(base.lower(), {
                "base": base, "live_blends": [], "backups": [],
                "source_pairs": [],
            })
            pair = _atlas_source_pair_key(
                textures.get("albedo"), textures.get("alpha"))
            if pair not in entry["source_pairs"]:
                entry["source_pairs"].append(pair)
    return registry


def _blend_contains_source_pair(blend_path, source):
    embedded = _blend_image_names(Path(blend_path))
    if not embedded:
        return False
    embedded_names = {Path(value).name.lower() for value in embedded}
    embedded_families = {source_family_name(value).lower() for value in embedded}
    for role in ("albedo", "alpha"):
        value = source.get(role)
        if not value:
            return False
        if (Path(value).name.lower() not in embedded_names
                and source_family_name(value).lower() not in embedded_families):
            return False
    return True


def _managed_source_pair_matches_base(source, atlas_base):
    expected = texture_base_for_material(atlas_base).lower()
    matched_roles = set()
    for role in ("albedo", "alpha"):
        path = source.get(role)
        if not path or not GENERATED_EXPORT_RE.match(Path(path).name):
            return False
        stem = Path(path).stem
        match = re.match(
            r"^(.*)_(color|opacity)$", stem, re.IGNORECASE)
        if not match or match.group(1).lower() != expected:
            return False
        matched_roles.add(match.group(2).lower())
    return matched_roles == {"color", "opacity"}


def _existing_atlas_match(registry, base, source):
    """Return (reusable, canonical existing spelling) for one source/base."""
    entry = registry.get(str(base).lower())
    if not entry or not entry["live_blends"]:
        return False, None
    pair = _atlas_source_pair_key(source.get("albedo"), source.get("alpha"))
    if pair in entry["source_pairs"]:
        return True, entry["base"]
    # A normalized, untagged source material already named after this exact
    # T_* atlas is explicit lineage evidence even though the blend manifest
    # records the pre-normalization TCom/Megascan inputs.
    if _managed_source_pair_matches_base(source, entry["base"]):
        return True, entry["base"]
    if not entry["source_pairs"] and any(
            _blend_contains_source_pair(path, source)
            for path in entry["live_blends"]):
        return True, entry["base"]
    return False, None


def _next_free_atlas_base(preferred, unavailable):
    match = re.match(r"^(.*?_atlas_)0*(\d+)$", preferred, re.IGNORECASE)
    if match:
        root = match.group(1)
        index = int(match.group(2)) + 1
    else:
        root = preferred.rstrip("_") + "_atlas_"
        index = 1
    while f"{root}{index:02d}".lower() in unavailable:
        index += 1
    return f"{root}{index:02d}"


def assign_leaf_atlas_bases(sources, folder, atlas_root=None):
    """Name generated leaf atlases from asset/material context, never source files.

    A referenced final material is authoritative when one unambiguous name is
    available.  Cluster internals commonly expose only ``Material`` names, so
    those sources receive the asset fallback and a stable per-source index.
    """
    registry = _existing_atlas_registry(atlas_root, folder)
    unavailable = set(registry)
    plans = []
    fallback = []
    for source in sources:
        names = unique(
            name
            for target in source.get("targets") or []
            for name in target.get("material_names") or []
            if name and not GENERIC_MATERIAL_NAME_RE.match(str(name))
        )
        canonical = unique(_material_leaf_atlas_name(name) for name in names)
        legacy = unique(_legacy_material_atlas_name(name) for name in names)
        if len(canonical) == 1:
            preferred = canonical[0]
            reusable, existing = _existing_atlas_match(
                registry, preferred, source)
            if (not reusable and len(legacy) == 1
                    and legacy[0].lower() != preferred.lower()):
                reusable, existing = _existing_atlas_match(
                    registry, legacy[0], source)
            if reusable:
                preferred = existing
                source["legacy_atlas_base_preserved"] = True
            plans.append({
                "source": source, "preferred": preferred,
                "reuses_existing": reusable,
            })
        else:
            fallback.append(source)

    root = _folder_leaf_atlas_root(folder)
    for index, source in enumerate(sorted(
            fallback,
            key=lambda row: (
                str(row.get("albedo", "")).lower(),
                str(row.get("alpha", "")).lower(),
            )), 1):
        preferred = f"{root}_{index:02d}"
        reusable, existing = _existing_atlas_match(
            registry, preferred, source)
        plans.append({
            "source": source,
            "preferred": existing if reusable else preferred,
            "reuses_existing": reusable,
        })

    def source_key(plan):
        source = plan["source"]
        preferred = plan["preferred"]
        return (
            str(preferred).lower(),
            not plan["reuses_existing"],
            str(source.get("albedo", "")).lower(),
            str(source.get("alpha", "")).lower(),
        )

    # Every distinct preferred base is reserved for one source before duplicate
    # names are numbered. This prevents a duplicate _01 from stealing another
    # source's explicit _02 preference merely because its path sorts first.
    reserved = unavailable | {plan["preferred"].lower() for plan in plans}
    used = set()
    duplicates = []
    for plan in sorted(plans, key=source_key):
        source = plan["source"]
        preferred = plan["preferred"]
        key = preferred.lower()
        if key not in used and (key not in unavailable
                                or plan["reuses_existing"]):
            source["atlas_base"] = preferred
            used.add(key)
        else:
            duplicates.append((source, preferred))
    for source, preferred in duplicates:
        allocated = _next_free_atlas_base(preferred, reserved | used)
        source["atlas_base"] = allocated
        used.add(allocated.lower())
    return sources


def _material_name_matches_atlas(material_name, atlas_base):
    name = canonical_material_name(material_name).lower()
    base = canonical_material_name(atlas_base).lower()
    return name == base or name.startswith(base + "_")


def _binding_slot_identity(binding):
    prefix = str(
        binding.get("slot_prefix")
        or str(binding.get("material_property") or "").rsplit(":", 1)[0]
    ).lower()
    index = binding.get("generator_index")
    if index is not None and prefix:
        return "index", str(index), prefix
    guid = str(binding.get("generator_guid") or "").lower()
    if guid and prefix:
        return "guid", guid, prefix
    return (
        "named",
        str(binding.get("generator_type") or "").lower(),
        str(binding.get("generator_name") or "").lower(),
        prefix,
    )


def _scoped_connection_payloads(spm, atlas_base=""):
    """Target-specific Atlas Builder manifests for one exact SPM/atlas."""
    spm = Path(spm)
    scopes = spm.parent / ".atlas_leaf_speedtree_scopes"
    if not scopes.is_dir():
        return []
    report_cache = _REPORT_SCAN_CACHE.get()
    cache = report_cache.setdefault("atlas_connection_manifests", {}) \
        if report_cache is not None else None
    cache_key = (str(scopes).lower(), spm.stem.lower())
    payloads = cache.get(cache_key) if cache is not None else None
    if payloads is None:
        payloads = []
        for path in sorted(scopes.glob(f"*__{spm.stem}.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            payload["_manifest_path"] = str(path)
            payloads.append(payload)
        if cache is not None:
            cache[cache_key] = tuple(payloads)
    if not atlas_base:
        return list(payloads)

    matched = []
    for payload in payloads:
        names = unique([
            payload.get("atlas_asset_name"),
            payload.get("requested_atlas_asset_name"),
            payload.get("material"),
        ] + [
            group.get("material")
            for group in payload.get("material_groups") or []
            if isinstance(group, dict)
        ])
        if any(name and _material_name_matches_atlas(name, atlas_base)
               for name in names):
            matched.append(payload)
    return matched


def _normalize_expected_generator_binding(binding, row_by_id,
                                          default_source_id=None):
    source_id = binding.get("source_material_id")
    if source_id in {None, "", -1, "-1"}:
        source_id = binding.get("material_id", default_source_id)
    if source_id in {None, "", -1, "-1"}:
        source_id = default_source_id
    source_id = str(source_id) if source_id not in {None, ""} else ""
    source_mesh_id = binding.get("source_mesh_id")
    if source_mesh_id in {None, ""}:
        source_mesh_id = binding.get("mesh_id")
    source_mesh_id = str(source_mesh_id) \
        if source_mesh_id not in {None, ""} else ""
    leaf_ordinal = binding.get("leaf_ordinal")
    source_row = row_by_id.get(source_id)
    source_cutouts = list(source_row.get("cutout_mesh_ids") or []) \
        if source_row else []
    if leaf_ordinal in {None, ""}:
        if source_mesh_id == "-10":
            leaf_ordinal = 1
        elif source_mesh_id in source_cutouts:
            leaf_ordinal = source_cutouts.index(source_mesh_id) + 1
    normalized = dict(binding)
    normalized.update({
        "source_material_id": source_id,
        "source_mesh_id": source_mesh_id,
        "leaf_ordinal": int(leaf_ordinal) if str(leaf_ordinal).isdigit() else None,
        "slot_identity": list(_binding_slot_identity(binding)),
        "target_material_id": str(binding.get("target_material_id"))
            if binding.get("target_material_id") not in {None, ""} else "",
        "target_mesh_id": str(binding.get("target_mesh_id"))
            if binding.get("target_mesh_id") not in {None, ""} else "",
    })
    return normalized


def _expected_generator_bindings(spm, atlas_base, requested_ids,
                                 requested_names, row_by_id,
                                 supplied=None, source_bindings=None):
    expected = []
    requested_set = set(requested_ids)
    for payload in _scoped_connection_payloads(spm, atlas_base):
        connection = payload.get("generator_connection") or {}
        connection_names = unique(connection.get("source_material_names") or [])
        default_source_id = requested_ids[0] if len(requested_ids) == 1 else None
        if requested_names and connection_names and not {
                str(name).lower() for name in requested_names
        }.intersection(str(name).lower() for name in connection_names):
            continue
        for binding in connection.get("bindings") or []:
            if not isinstance(binding, dict):
                continue
            row = _normalize_expected_generator_binding(
                binding, row_by_id, default_source_id=default_source_id)
            if requested_set and row["source_material_id"] not in requested_set:
                continue
            expected.append(row)
    for binding in supplied or []:
        if not isinstance(binding, dict):
            continue
        row = _normalize_expected_generator_binding(
            binding, row_by_id,
            default_source_id=requested_ids[0] if len(requested_ids) == 1 else None)
        if requested_set and row["source_material_id"] not in requested_set:
            continue
        expected.append(row)
    # Any source slot that still exists is necessarily expected and pending,
    # even if a stale/partial manifest omitted it.
    for binding in source_bindings or []:
        expected.append(_normalize_expected_generator_binding(
            binding, row_by_id,
            default_source_id=str(binding.get("material_id") or "")))

    deduped = {}
    for binding in expected:
        key = (binding["source_material_id"], tuple(binding["slot_identity"]))
        previous = deduped.get(key)
        if previous is None or (
                not previous.get("target_material_id")
                and binding.get("target_material_id")):
            deduped[key] = binding
    return list(deduped.values())


def inspect_leaf_generator_connection(spm, source_material_names=None,
                                      source_material_ids=None, atlas_base="",
                                      expected_generator_bindings=None):
    """Strictly audit every expected source Generator slot and leaf ordinal."""
    rows = extract_material_image_refs(spm)
    row_by_id = {
        str(row.get("material_id")): row
        for row in rows if row.get("material_id") is not None
    }
    requested_names = unique(source_material_names or [])
    requested_ids = unique(str(value) for value in (source_material_ids or [])
                           if value not in {None, "", "-1"})
    if not requested_ids and requested_names:
        wanted = {canonical_material_name(name).lower()
                  for name in requested_names}
        requested_ids = unique(
            str(row["material_id"]) for row in rows
            if row.get("material_id") is not None
            and canonical_material_name(row.get("material_name")).lower() in wanted
        )

    managed_ids = {
        str(row["material_id"])
        for row in rows
        if row.get("material_id") is not None
        and row.get("managed_leaf_output")
        and _material_name_matches_atlas(row.get("material_name"), atlas_base)
    }
    bindings = leaf_generator_bindings(spm, visible_only=False)
    actual_mesh_ids = mesh_asset_ids(spm)

    def decorate(binding):
        binding = dict(binding)
        material = row_by_id.get(str(binding.get("material_id")))
        mesh_ids = list(material.get("cutout_mesh_ids") or []) if material else []
        binding["material_name"] = material.get("material_name") if material else ""
        binding["material_cutout_mesh_ids"] = mesh_ids
        binding["mesh_belongs_to_material"] = bool(
            binding.get("mesh_id") and str(binding["mesh_id"]) in mesh_ids)
        binding["mesh_asset_exists"] = bool(
            binding.get("mesh_id") and str(binding["mesh_id"]) in actual_mesh_ids)
        binding["managed_leaf_output"] = bool(
            material and material.get("managed_leaf_output"))
        binding["slot_identity"] = list(_binding_slot_identity(binding))
        return binding

    source_bindings = [
        decorate(binding) for binding in bindings
        if str(binding.get("material_id")) in set(requested_ids)
    ]
    managed_bindings = [
        decorate(binding) for binding in bindings
        if str(binding.get("material_id")) in managed_ids
    ]
    current_by_slot = {
        tuple(binding["slot_identity"]): binding
        for binding in (source_bindings + managed_bindings)
    }
    expected = _expected_generator_bindings(
        spm, atlas_base, requested_ids, requested_names, row_by_id,
        supplied=expected_generator_bindings,
        source_bindings=source_bindings)
    statuses = []
    for material_id in requested_ids:
        material = row_by_id.get(material_id)
        id_bindings = [
            binding for binding in source_bindings
            if str(binding.get("material_id")) == material_id
        ]
        id_expected = [row for row in expected
                       if row.get("source_material_id") == material_id]
        slot_results = []
        for expected_binding in id_expected:
            current = current_by_slot.get(
                tuple(expected_binding["slot_identity"]))
            errors = []
            if current is None:
                errors.append("generator_slot_missing")
            else:
                if str(current.get("material_id")) in set(requested_ids):
                    errors.append("source_material_still_connected")
                if not current.get("managed_leaf_output"):
                    errors.append("managed_material_missing")
                if not current.get("mesh_asset_exists"):
                    errors.append("mesh_asset_missing")
                if not current.get("mesh_belongs_to_material"):
                    errors.append("material_cutout_ownership_mismatch")
                target_material_id = expected_binding.get("target_material_id")
                target_mesh_id = expected_binding.get("target_mesh_id")
                if (target_material_id
                        and str(current.get("material_id")) != target_material_id):
                    errors.append("target_material_mismatch")
                if target_mesh_id and str(current.get("mesh_id")) != target_mesh_id:
                    errors.append("target_mesh_mismatch")
            slot_results.append({
                "slot_identity": list(expected_binding["slot_identity"]),
                "leaf_ordinal": expected_binding.get("leaf_ordinal"),
                "expected": expected_binding,
                "current": current,
                "errors": unique(errors),
                "complete": not errors,
            })
        id_complete = bool(id_expected) and all(
            result["complete"] for result in slot_results)
        statuses.append({
            "material_id": material_id,
            "material_name": material.get("material_name") if material else "",
            "bindings": id_bindings,
            "expected_slot_count": len(id_expected),
            "connected_slot_count": sum(
                1 for result in slot_results if result["complete"]),
            "slot_results": slot_results,
            "complete": id_complete,
        })

    complete = bool(requested_ids) and bool(statuses) and all(
        status["complete"] for status in statuses)
    all_errors = unique(
        error for status in statuses for result in status["slot_results"]
        for error in result["errors"])
    if source_bindings:
        reason = "source_material_still_connected"
    elif complete:
        reason = "managed_material_and_mesh_connected"
    elif not requested_ids:
        reason = "source_material_not_found"
    elif not expected:
        reason = "expected_generator_slots_missing"
    elif "mesh_asset_missing" in all_errors:
        reason = "managed_mesh_asset_missing"
    elif "material_cutout_ownership_mismatch" in all_errors:
        reason = "managed_material_mesh_mismatch"
    else:
        reason = "managed_generator_connection_partial"
    return {
        "source_material_names": requested_names,
        "source_material_ids": requested_ids,
        "material_ids": list(requested_ids),
        "source_material_statuses": statuses,
        "expected_generator_bindings": expected,
        "expected_slot_count": sum(
            status["expected_slot_count"] for status in statuses),
        "connected_slot_count": sum(
            status["connected_slot_count"] for status in statuses),
        "source_generator_bindings": source_bindings,
        "managed_generator_bindings": managed_bindings,
        "generator_bindings": source_bindings + managed_bindings,
        "generator_connection_complete": complete,
        "generator_connection_update_needed": not complete,
        "generator_connection_reason": reason,
    }


def annotate_leaf_generator_connections(sources):
    """Attach per-target and aggregate Step 2 completion evidence in-place."""
    for source in sources:
        targets = source.get("targets") or []
        for target in targets:
            status = inspect_leaf_generator_connection(
                target["spm"],
                source_material_names=(
                    target.get("source_material_names")
                    or target.get("material_names") or []),
                source_material_ids=(
                    target.get("source_material_ids")
                    or target.get("material_ids") or []),
                atlas_base=source.get("atlas_base", ""),
                expected_generator_bindings=target.get(
                    "expected_generator_bindings"),
            )
            target.update(status)
        source["generator_connection_complete"] = bool(targets) and all(
            target.get("generator_connection_complete") for target in targets)
        source["generator_connection_update_needed"] = any(
            target.get("generator_connection_update_needed") for target in targets)
        source["leaf_mesh_complete"] = bool(source.get("atlas_blends")) and bool(
            source["generator_connection_complete"])
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
            # A connected Atlas Leaf Mesh Builder material is the finished
            # leaf-card output. Its Color/Opacity may deliberately reference a
            # Cluster render, but following that render back into the Cluster
            # SPM would expand the finished output into new source jobs again.
            if material.get("managed_leaf_output"):
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
                    "material_names_by_spm": {},
                    "material_ids_by_spm": {},
                })
                if str(spm) not in usage["spms"]:
                    usage["spms"].append(str(spm))
                if material["material_name"] not in usage["material_names"]:
                    usage["material_names"].append(material["material_name"])
                spm_key = str(spm)
                names_for_spm = usage["material_names_by_spm"].setdefault(
                    spm_key, [])
                if material["material_name"] not in names_for_spm:
                    names_for_spm.append(material["material_name"])
                material_id = material.get("material_id")
                ids_for_spm = usage["material_ids_by_spm"].setdefault(
                    spm_key, [])
                if material_id and material_id not in ids_for_spm:
                    ids_for_spm.append(material_id)
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
    cluster_usage = cluster_material_usage(target_spms, clusters)
    referenced = {
        key: sorted(usage["spms"])
        for key, usage in cluster_usage.items()
    }
    sources = []
    candidates = []
    direct_target_ids = {}
    candidate_spms = list(target_spms)
    candidate_spms.extend(
        spm for spm in source_spms(folder) if spm not in candidate_spms)
    for candidate_spm in candidate_spms:
        candidates.extend(leaf_sources_from_spm(
            candidate_spm, "direct", active_only=False))
    for spm in target_spms:
        authoritative = spm
        if spm.name.lower().startswith("sk_"):
            source = Path(folder) / spm.name[3:]
            if source.is_file():
                authoritative = source
        target_materials = {}
        target_active = active_material_ids(spm)
        for row in extract_material_image_refs(spm):
            if target_active and row.get("material_id") not in target_active:
                continue
            key = canonical_material_name(row.get("material_name")).lower()
            target_materials.setdefault(key, []).append(row)
        direct_sources = leaf_sources_from_spm(
            authoritative, "direct", active_only=False)
        # Some authoring SPMs are binary containers that this lightweight
        # audit cannot parse, while the current SK exposes normalized T_* slots.
        # The untagged SK source material remains authoritative for mesh work.
        if not direct_sources and Path(authoritative) != Path(spm):
            direct_sources = leaf_sources_from_spm(
                spm, "direct", active_only=False)
        for source in direct_sources:
            source["target_spm"] = str(spm)
            mapped_names = []
            mapped_ids = []
            for name in source.get("material_names") or []:
                for row in target_materials.get(
                        canonical_material_name(name).lower(), []):
                    mapped_names.append(row.get("material_name"))
                    if row.get("material_id"):
                        mapped_ids.append(row["material_id"])
            if not mapped_names:
                continue
            source["material_names"] = unique(mapped_names)
            source["source_material_ids"] = unique(mapped_ids)
            source["material_ids"] = unique(mapped_ids)
            direct_target_ids.setdefault(str(spm).lower(), set()).update(
                source["source_material_ids"])
            sources.append(source)
    for cluster in clusters:
        if str(cluster).lower() not in referenced:
            continue
        candidates.extend(leaf_sources_from_spm(
            cluster, "cluster", active_only=False))
        for source in leaf_sources_from_spm(cluster, "cluster"):
            final_spms = referenced[str(cluster).lower()]
            for final_spm in final_spms:
                usage = cluster_usage[str(cluster).lower()]
                target_ids = list(
                    usage.get("material_ids_by_spm", {}).get(final_spm, []))
                covered = direct_target_ids.get(str(final_spm).lower(), set())
                target_ids = [value for value in target_ids if value not in covered]
                # A coherent atlas/card pair on the final semantic material is
                # the deterministic mesh source. Internal Cluster leaves are
                # only fallback provenance for IDs without that direct pair.
                if not target_ids:
                    continue
                rows_by_id = {
                    str(row.get("material_id")): row
                    for row in extract_material_image_refs(final_spm)
                    if row.get("material_id") is not None
                }
                target_source = dict(source)
                target_source["trace_spm"] = str(cluster)
                target_source["trace_material_names"] = list(
                    source.get("material_names") or [])
                target_source["referenced_by_spms"] = final_spms
                target_source["target_spm"] = final_spm
                # The traced Cluster material owns the source pixels, while the
                # final SPM's cluster-card material/mesh slots are the exact
                # connection target. Keep both identities separate.
                target_source["material_names"] = unique(
                    rows_by_id[str(value)].get("material_name")
                    for value in target_ids if str(value) in rows_by_id)
                target_source["source_material_ids"] = target_ids
                target_source["material_ids"] = list(
                    target_source["source_material_ids"])
                target_source["connection_mode"] = "cluster_trace_fallback"
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
    return {
        "mesh_name": mesh_name,
        "source_spm": str(source) if source else None,
        "sk_spm": str(sk) if sk else None,
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
            has_work = bool(would_create if dry_run else created) or bool(
                patch.get("renames"))
            results.append({
                "mesh_name": mesh_name,
                "status": (
                    ("dry-run" if dry_run else "prepared")
                    if has_work else "up_to_date"
                ),
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
    would_create = str(target) if dry_run and not preferred else None
    has_work = bool(would_create if dry_run else created) or bool(
        patch.get("renames"))
    return {
        "folder": str(folder),
        "status": (
            ("dry-run" if dry_run else "prepared")
            if has_work else "up_to_date"
        ),
        "sk_spm": str(target),
        "would_create": would_create,
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
    # Canonical PCG outputs use a literal T_ base. Resolve those through the
    # shared SpeedTree contract so a first/partial directory cannot mask a
    # later complete set. Legacy M_ outputs retain exact-name lookup; material
    # labels such as Green/Yellow are never inferred as managed T_ sets.
    if str(atlas_base or "").strip().casefold().startswith("t_"):
        resolved = resolve_texture_set(dirs, atlas_base, required_maps)
        export_maps = {
            map_name: resolved["files"].get(str(map_name).casefold())
            for map_name in required_maps
        }
        selected_dir = resolved.get("texture_dir")
        return (
            Path(selected_dir) if selected_dir else dirs[0],
            export_maps,
        )
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
    match = ATLAS_NAME_RE.match(str(name))
    if match:
        suffix = str(name)[len(match.group(1)):].lstrip("_").lower()
        if suffix in AUTO_SPLIT_SUFFIXES:
            return match.group(1)
    return None


def derived_material_base(name):
    """Return a possible base for a SpeedTree Auto Split classification.

    This is deliberately only a *candidate*. ``material_texture_items`` uses
    it for a suffix-free output only when two visible aliases share the exact
    same connected source set. A lone ``*_dead``/``*_green`` material keeps
    its authored name unless atlas/leaf provenance proves the base.
    """
    canonical = canonical_material_name(name)
    for suffix in sorted(AUTO_SPLIT_SUFFIXES, key=len, reverse=True):
        marker = "_" + suffix
        if canonical.lower().endswith(marker):
            return canonical[:-len(marker)]
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


def _source_material_ref_entries(folder, sk_spm):
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
                result[key] = {
                    "spm": Path(source),
                    "refs": list(refs),
                }
    return result


def _source_material_ref_map(folder, sk_spm):
    """Compatibility view of source material references without ownership."""
    return {
        key: list(entry["refs"])
        for key, entry in _source_material_ref_entries(folder, sk_spm).items()
    }


def _managed_texture_directories(spm, refs):
    """Directories named by literal managed T_ refs, relative to their SPM."""
    directories = []
    for ref in refs or []:
        resolved = resolve_spm_image_ref(spm, ref)
        if parse_managed_texture_path(resolved) is None:
            continue
        directories.append(resolved.parent)
    return unique(directories)


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
        referenced_ids = active_material_ids(sk_spm)
        visible_ids = visible_material_ids(sk_spm)
        for row in extract_material_image_refs(sk_spm):
            if referenced_ids and row.get("material_id") not in visible_ids:
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


def _leaf_source_for_atlas_base(atlas_base, leaf_mesh_sources):
    """Resolve one suffix-free atlas name to an unambiguous leaf source."""
    wanted = canonical_material_name(atlas_base).lower()
    matches = [
        source for source in (leaf_mesh_sources or [])
        if canonical_material_name(source.get("atlas_base", "")).lower() == wanted
    ]
    return matches[0] if len(matches) == 1 else None


def _material_ref_signature(spm, refs, material_name=""):
    """Stable identity for the files connected to one SpeedTree material."""
    resolved = unique(
        str(resolve_spm_image_ref(spm, ref)).lower()
        for ref in (refs or []) if ref
    )
    if resolved:
        return tuple(sorted(resolved))
    # Empty materials must never collapse merely because their names have the
    # same Auto Split suffix.
    return ("@material", str(Path(spm)).lower(), str(material_name).lower())


def _managed_connection_matches(refs, texture_base, required_maps):
    stems = {Path(ref).stem.lower() for ref in (refs or []) if ref}
    expected = {
        f"{texture_base}_{map_name}".lower() for map_name in required_maps
    }
    return expected.issubset(stems)


def _graph_for_materials(graphs, base, material_names):
    """Prefer the target graph, then a graph owned by one merged alias."""
    keys = [texture_base_for_material(base).lower(), str(base).lower()]
    for name in material_names:
        canonical = canonical_material_name(name)
        keys.extend((
            texture_base_for_material(canonical).lower(),
            canonical.lower(),
            str(name).lower(),
        ))
    for key in unique(keys):
        if key in graphs:
            return graphs[key]
    return None


def material_texture_items(folder, cfg, tex_dirs, graphs, preserved_definitions=None,
                           leaf_mesh_sources=None):
    """One managed texture-set job for every material used by a Generator."""
    spms = preferred_sk_spms(folder) or source_spms(folder)
    preserved_definitions = preserved_definitions if preserved_definitions is not None \
        else cluster_render_source_definitions(folder)
    preserved_names = set(preserved_definitions)
    blend_stems = atlas_blend_stems(cfg)
    records = []
    for spm in spms:
        referenced_ids = active_material_ids(spm)
        visible_ids = visible_material_ids(spm)
        original_ref_entries = (
            _source_material_ref_entries(folder, spm)
            if spm.name.lower().startswith("sk_") else {}
        )
        for row in extract_material_image_refs(spm):
            # No Generator material properties means a mesh/material asset and
            # retains the legacy include-all fallback. Otherwise only visible
            # generators contribute export jobs; hidden nodes remain available
            # to the separate provenance tracing functions above.
            if referenced_ids and row.get("material_id") not in visible_ids:
                continue
            name = row.get("material_name")
            if not name:
                continue
            current_refs = list(row.get("refs") or [])
            refs = list(current_refs)
            source_ref_spm = Path(spm)
            current_is_managed = _refs_are_only_managed_outputs(current_refs)
            leaf_source = _leaf_source_for_active_material(
                spm, current_refs, leaf_mesh_sources or [])
            canonical_name = canonical_material_name(name)
            canonical = canonical_name.lower()
            auto_base = auto_split_atlas_base(name, blend_stems, graphs)
            # A normalized SK can point at T_*_green while its source SPM has
            # only the suffix-free leaf atlas. Recover that authoritative leaf
            # source by atlas name so the job has every connected input map.
            if not leaf_source and auto_base:
                leaf_source = _leaf_source_for_atlas_base(
                    auto_base, leaf_mesh_sources or [])
            # SK files often point only at old M_/T_ Unreal outputs.  Those
            # are not source inputs; recover the same material's references
            # from the original SPM copy instead.
            if leaf_source:
                refs = list(leaf_source.get("source_refs") or [
                    leaf_source.get("albedo"), leaf_source.get("alpha")])
                refs = [str(ref) for ref in refs if ref]
                source_ref_spm = Path(
                    leaf_source.get("target_spm") or source_ref_spm
                )
            elif current_is_managed and original_ref_entries.get(canonical):
                original_entry = original_ref_entries[canonical]
                refs = list(original_entry["refs"])
                source_ref_spm = Path(original_entry["spm"])
                # Recovered source refs may identify the same atlas that the
                # current managed T_* slots could not identify by themselves.
                # Resolve it now so managed and not-yet-managed SK variants
                # share one provenance/signature.
                leaf_source = _leaf_source_for_active_material(
                    source_ref_spm, refs, leaf_mesh_sources or [])
                if leaf_source:
                    refs = list(leaf_source.get("source_refs") or [
                        leaf_source.get("albedo"), leaf_source.get("alpha")])
                    refs = [str(ref) for ref in refs if ref]
                    source_ref_spm = Path(
                        leaf_source.get("target_spm") or source_ref_spm
                    )
            if canonical in preserved_names:
                continue
            # A cluster-folder image is already a derived SpeedTree cluster
            # render, not an input set to process through Substance again.
            if is_cluster_render_material(folder, spm, refs):
                continue
            exact_graph = _graph_for_materials(graphs, canonical_name, [name])
            is_managed_generic = bool(
                GENERIC_MATERIAL_NAME_RE.match(name) and exact_graph)
            # An explicit T_<generic> graph is the authoritative contract for
            # a visible generic material.  Atlas provenance still supplies
            # its source inputs, but must not split the same material into a
            # second, newly named output set.
            strong_base = (
                canonical_name if is_managed_generic
                else leaf_source.get("atlas_base") if leaf_source else auto_base)
            candidate_base = (
                strong_base or derived_material_base(name) or canonical_name)
            # Generic SpeedTree names are normally disposable placeholders,
            # but an explicit managed graph makes the visible material part
            # of our normalization contract even before its slots have been
            # rewritten to T_* outputs.  Hidden generators were filtered
            # above, so this keeps only actually contributing materials.
            if GENERIC_MATERIAL_NAME_RE.match(name) and not exact_graph:
                continue
            source_albedo, source_alpha = material_color_alpha_refs(refs)
            records.append({
                "name": name,
                "canonical": canonical_name,
                "spm": spm,
                "material_id": row.get("material_id"),
                "current_refs": current_refs,
                "source_refs": list(refs),
                "source_ref_spm": source_ref_spm,
                "source_signature": _material_ref_signature(
                    source_ref_spm, refs, name),
                "source_albedo": source_albedo,
                "source_alpha": source_alpha,
                "current_is_managed": current_is_managed,
                "leaf_source": leaf_source,
                "strong_base": strong_base,
                "candidate_base": candidate_base,
                "referenced_legacy": legacy_export_maps_from_refs(
                    spm, current_refs, name, cfg["required_export_maps"]),
            })

    # First group by the possible suffix-free base, then by the exact connected
    # source signature. Same-named variants with different sources stay apart.
    candidate_groups = {}
    for record in records:
        candidate_groups.setdefault(record["candidate_base"].lower(), {
            "base": record["candidate_base"], "records": [],
        })["records"].append(record)

    grouped = []
    for candidate in candidate_groups.values():
        signature_groups = {}
        for record in candidate["records"]:
            signature_groups.setdefault(record["source_signature"], []).append(record)
        collision = len(signature_groups) > 1
        for signature, aliases in signature_groups.items():
            if not collision and (
                    any(row.get("strong_base") for row in aliases)
                    or len(aliases) > 1):
                base = candidate["base"]
            elif collision and any(
                    row["canonical"].lower() == candidate["base"].lower()
                    for row in aliases):
                base = candidate["base"]
            else:
                base = aliases[0]["canonical"]
            grouped.append((base, signature, aliases))

    items = []
    for base, signature, aliases in grouped:
        names = unique(row["name"] for row in aliases)
        texture_base = texture_base_for_material(base)
        graph = _graph_for_materials(graphs, base, names)
        authoritative_texture_dirs = unique(
            directory
            for row in aliases
            for directory in (
                _managed_texture_directories(row["spm"], row["current_refs"])
                + _managed_texture_directories(
                    row["source_ref_spm"], row["source_refs"])
            )
        )
        maps_dir, export_maps = find_export_maps_multi(
            unique(list(tex_dirs) + authoritative_texture_dirs),
            texture_base,
            cfg["required_export_maps"],
        )
        _legacy_dir, legacy_export_maps = find_export_maps_multi(
            tex_dirs, base, cfg["required_export_maps"])
        for record in aliases:
            for map_name, path in record["referenced_legacy"].items():
                if path and not legacy_export_maps.get(map_name):
                    legacy_export_maps[map_name] = path
        source_refs = unique(
            ref for row in aliases for ref in row["source_refs"])
        source_albedo = unique(
            ref for row in aliases for ref in row["source_albedo"])
        source_alpha = unique(
            ref for row in aliases for ref in row["source_alpha"])
        leaf_sources = unique(
            row["leaf_source"] for row in aliases if row.get("leaf_source"))
        connection_materials = unique(
            row["name"] for row in aliases
            if not _managed_connection_matches(
                row["current_refs"], texture_base, cfg["required_export_maps"])
        )
        entry = {
            "cluster_spm": None,
            "name": base,
            "source": "material",
            "material_names": names,
            "material_aliases": names,
            "material_spms": unique(str(row["spm"]) for row in aliases),
            "material_targets": [
                {
                    "spm": str(row["spm"]),
                    "material_id": (
                        str(row["material_id"])
                        if row.get("material_id") not in {None, ""}
                        else None),
                    "material_name": row["name"],
                    "source_signature": list(row["source_signature"]),
                }
                for row in aliases
            ],
            "atlas_base": base,
            "texture_base": texture_base,
            "is_atlas": any(row.get("leaf_source") for row in aliases)
                        or any(material_atlas_base(row["name"], blend_stems, graphs)
                               for row in aliases)
                        or any("cluster\\" in str(ref).lower() for ref in source_refs),
            "needs_leaf_mesh": False,
            "atlas_blends": unique(
                path for source in leaf_sources
                for path in source.get("atlas_blends") or []),
            "export_maps": export_maps,
            "missing_export_maps": [k for k, v in export_maps.items() if not v],
            "legacy_export_maps": legacy_export_maps,
            "texture_dir": str(maps_dir) if maps_dir else None,
            "m_graph": graph[0] if graph else None,
            "m_graph_sbs": graph[1] if graph else None,
            "legacy_m_graph": bool(graph and graph[0].lower().startswith("m_")),
            "source_refs": source_refs,
            "source_albedo": source_albedo,
            "source_alpha": source_alpha,
            "source_signature": list(signature),
            "leaf_source_provenance": bool(leaf_sources),
            "connection_update_needed": bool(connection_materials),
            "connection_materials": connection_materials,
        }
        items.append(entry)
    return items


def infer_normal_convention(refs):
    low = " ".join(refs).lower()
    if "tcom_" in low or "megascan" in low or "megascans" in low:
        return "OpenGL"
    if ".sbsar" in low or "substance" in low:
        return "DirectX"
    return "unknown"


def audit_folder(folder, cfg, include_refs=False, target_mesh_names=None):
    folder = Path(folder)
    preferred = preferred_sk_spms(folder)
    loose = loose_sk_spms(folder)
    sources = source_spms(folder)
    chosen = preferred[0] if preferred else (loose[0] if loose else (sources[0] if sources else None))
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
    target_spms = []
    for mesh_name in target_mesh_names or []:
        target = (find_sk_spm_for_mesh(folder, mesh_name)
                  or find_source_spm_for_mesh(folder, mesh_name))
        if target and target not in target_spms:
            target_spms.append(target)
    # A folder row without an explicit PCG mesh is represented by its chosen
    # SPM, not every sibling variant. Explicit PCG targets can still request
    # multiple exact variants through target_mesh_names.
    if not target_spms and chosen:
        target_spms = [chosen]
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
        "leaf_mesh_target_spms": [str(path) for path in target_spms],
        "managed_leaf_outputs": managed_leaf_outputs(target_spms),
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
    local_entries = [c for c in item["cluster_items"] if not c.get("shared_from")]
    leaf_sources = item.get("leaf_mesh_sources") or []
    if any(not source.get("atlas_blends") for source in leaf_sources):
        actions.append("Blender 아틀라스 파일 확인 필요")
    if any(source.get("generator_connection_update_needed")
           for source in leaf_sources):
        actions.append("Blender 잎 매쉬 Generator 연결 필요")
    if any(c["missing_export_maps"] for c in local_entries):
        actions.append("Substance에서 출력 텍스처 저장 필요")
    if any(c.get("connection_update_needed") for c in item["cluster_items"]):
        actions.append("SpeedTree 연결 텍스처 정리 필요")
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


def focus_pcg_targets(pcg_targets, focus_data_assets=None, positive_weight_only=True):
    """Keep focused PCG DataAssets plus explicitly placed level meshes.

    The Unreal refresh report intentionally contains the full PCG graph
    dependency set.  The production board can therefore narrow PCG provenance
    to the DataAssets currently used for the shot without losing directly
    placed ST9 components from the configured level.
    """
    if not pcg_targets or not focus_data_assets:
        return pcg_targets

    focus = {str(path).lower() for path in focus_data_assets if path}
    selected_data_assets = []
    pcg_meshes = {}

    def entry_is_active(entry):
        if not entry.get("static_mesh"):
            return False
        if not positive_weight_only:
            return True
        weight = entry.get("weight")
        if weight is None:
            return True
        try:
            return float(weight) > 0.0
        except (TypeError, ValueError):
            return True

    for data_asset in pcg_targets.get("data_assets", []):
        asset_path = str(data_asset.get("asset") or "")
        if asset_path.lower() not in focus:
            continue
        filtered_sections = {}
        for section, entries in (data_asset.get("sections") or {}).items():
            active_entries = [dict(entry) for entry in entries if entry_is_active(entry)]
            if not active_entries:
                continue
            filtered_sections[section] = active_entries
            for entry in active_entries:
                mesh_path = entry["static_mesh"]
                info = pcg_meshes.setdefault(mesh_path, {
                    "data_assets": set(),
                    "sections": set(),
                })
                info["data_assets"].add(asset_path)
                info["sections"].add(section)
        selected = dict(data_asset)
        selected["sections"] = filtered_sections
        selected_data_assets.append(selected)

    selected_meshes = []
    for item in pcg_targets.get("meshes", []):
        mesh_path = item.get("static_mesh")
        placements = [dict(row) for row in (item.get("level_instances") or [])]
        pcg_info = pcg_meshes.get(mesh_path)
        if not pcg_info and not placements:
            continue
        selected = dict(item)
        selected["data_assets"] = sorted(pcg_info["data_assets"]) if pcg_info else []
        selected["sections"] = sorted(pcg_info["sections"]) if pcg_info else []
        selected["source_graphs"] = []
        selected["level_instances"] = placements
        selected_meshes.append(selected)

    focused = dict(pcg_targets)
    focused["data_assets"] = selected_data_assets
    focused["meshes"] = selected_meshes
    focused["focus_data_assets"] = [str(path) for path in focus_data_assets if path]
    focused["positive_weight_only"] = bool(positive_weight_only)
    return focused


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
        "name", "status", "folder", "chosen_spm", "sbs", "clusters",
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
            graph = _graph_for_materials(
                graphs,
                entry.get("atlas_base", ""),
                entry.get("material_names") or [],
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
    """Choose one owner for each exact output/source pair.

    Identical output names backed by different connected source sets are not
    shared; that would silently render one material's maps for another.
    """
    def priority(entry):
        if entry.get("m_graph"):
            return 3
        if any((entry.get("export_maps") or {}).values()):
            return 2
        return 1

    def sharing_key(item_name, entry):
        signature = tuple(entry.get("source_signature") or [])
        if not signature:
            signature = ("@folder", item_name.lower())
        return entry["atlas_base"].lower(), signature

    owners = {}
    items_by_name = {item["name"]: item for item in items}
    candidates = [
        (priority(entry), item["name"], entry)
        for item in items for entry in item.get("cluster_items") or []
    ]
    for _rank, item_name, entry in sorted(
            candidates, key=lambda row: (-row[0], row[1].lower())):
        owners.setdefault(sharing_key(item_name, entry), (item_name, entry))

    changed = set()
    for item in items:
        for entry in item.get("cluster_items") or []:
            owner_name, owner_entry = owners[sharing_key(item["name"], entry)]
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


@_report_scan_cached
def make_report(cfg, targets=None, include_refs=False, pcg_targets=None):
    pcg_targets = focus_pcg_targets(
        pcg_targets,
        cfg.get("pcg_focus_data_assets"),
        cfg.get("pcg_positive_weight_only", True),
    )
    ensure_blend_source_index(cfg)
    folders = candidate_folders(cfg, targets, pcg_targets=pcg_targets)
    report_target_mesh_names = target_mesh_names_from_pcg_targets(pcg_targets)
    items = [
        audit_folder(
            folder, cfg, include_refs=include_refs,
            target_mesh_names=folder_target_mesh_names(
                folder, report_target_mesh_names)
                if report_target_mesh_names else None,
        )
        for folder in folders
    ]
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
        item["pcg_data_assets"] = sorted({
            asset
            for name in folder_matches
            for asset in target_source_map.get(name, {}).get("data_assets", [])
        })
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
            "focus_data_assets": pcg_targets.get("focus_data_assets", []) if pcg_targets else [],
            "positive_weight_only": pcg_targets.get("positive_weight_only") if pcg_targets else None,
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
