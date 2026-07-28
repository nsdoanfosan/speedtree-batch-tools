"""SPM bone calibration + material prefix for the SK skeletal-vegetation pipeline.

Bone rule (checklist item 1, size-aware):
  SpeedTree's Relative bone style already scales bone count with spline length,
  but an uncapped "bones per branch" target explodes on trees with thousands of
  twigs. We calibrate one shared Relative value against a capped total budget:

  1. probe   — temporarily set every target generator to Absolute/1 and export
               XML once. Absolute/1 gives exactly one bone per branch, exposing
               both the branch count and approximate branch lengths.
  2. priority — when the uncapped target exceeds max_total_bones, disable every
               Base-sourced target before reducing the Tree skeleton density.
  3. target  — min(remaining Tree branches x target_bones_per_branch,
               max_total_bones).
  4. solve   — estimate one Relative value for the remaining targets from the
               probe lengths, then verify once in SpeedTree. Only outliers need
               proportional correction rounds.

  Generator/Node GUID ancestry decides which Branch generators belong to the
  live Tree skeleton. Absolute/0 on that root chain is activated automatically;
  Base-reference internals remain excluded except for the first Branch stage of
  an explicitly classified branch reference.

Materials (checklist item 2): every Assets/Material_v8 Name gets the M_ prefix
(attribute-only rename, verified safe — FBX picks up the new name, texture
paths untouched).

The patch is string-level and format-preserving. A timestamped backup goes to
<spm dir>/_spm_backups/ before the first write, and is restored automatically
if calibration dies midway.

Standalone:  python spm_audit.py <file.spm> [--dry-run] [--report out.json]
"""
import argparse
import ctypes
import errno
import gzip
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

BATCH_TOOLS_DIR = Path(__file__).resolve().parent.parent
if str(BATCH_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(BATCH_TOOLS_DIR))

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from sk_common import (
        file_content_fingerprint,
        load_config,
        speedtree_output_spm_for,
    )
else:
    from .sk_common import (
        file_content_fingerprint,
        load_config,
        speedtree_output_spm_for,
    )
from speedtree_pipeline_contract import (
    read_spm_text as read_pipeline_spm_text,
    spm_container_format,
)
from speedtree_export_options_contract import require_texture_skip_writing

GEN_RE = re.compile(r"<Generator\b[^>]*>.*?</Generator>", re.DOTALL)
GEN_TYPE_RE = re.compile(r'<Generator\b[^>]*Type="([^"]+)"')
FIRST_NAME_RE = re.compile(r"<Name>([^<]*)</Name>")
FIRST_GUID_RE = re.compile(r"<GUID>([^<]*)</GUID>")
MATERIAL_RE = re.compile(r'(<Material_v8\b[^>]*?Name=")([^"]*)(")')

BACKUP_SUBDIR = "_spm_backups"
CALIBRATION_MARKER_SUFFIX = ".skbatch_calibration_in_progress.json"
CALIBRATION_MARKER_VERSION = 1
PROBE_CACHE_VERSION = 1
PROBE_CACHE_SUFFIX = ".skbatch_probe_cache.json"
SPM_PROCESS_LOCK_SUFFIX = ".skbatch_process.lock"
SPEEDTREE_EXPORT_MUTEX_ENV = "SPEEDTREE_EXPORT_MUTEX_NAME"
SPEEDTREE_EXPORT_MUTEX_DEFAULT = (
    r"Local\PARK.SpeedTree.Modeler.Export.v1.slot0"
)
BONE_VALUE_RE = re.compile(
    r"(<Name>Physics:(?:Bone style|Bones)</Name>\s*<Value>)[^<]*(</Value>)",
    re.DOTALL,
)


class ManualCalibrationRequired(RuntimeError):
    """A known outlier that should stop quickly without changing the SPM."""

    def __init__(
        self,
        reason,
        *,
        rounds,
        total_bones,
        calibration,
        warnings=None,
        skipped=None,
    ):
        super().__init__(reason)
        self.rounds = rounds
        self.total_bones = total_bones
        self.calibration = calibration
        self.warnings = list(warnings or [])
        self.skipped = list(skipped or [])


class SpeedTreeExportTimeout(RuntimeError):
    """One SpeedTree export exceeded the automatic-calibration time budget."""

    def __init__(self, stage, timeout_seconds):
        self.stage = stage
        self.timeout_seconds = float(timeout_seconds)
        super().__init__(
            f"SpeedTree {stage} export exceeded {self.timeout_seconds:g}s; "
            "skipped automatic calibration for manual bone setup"
        )


def canonical_spm_process_lock_path(spm_path):
    """Return the one persistent lock identity shared by a Cluster SPM pair."""
    canonical = Path(speedtree_output_spm_for(spm_path)).expanduser().resolve()
    return (
        canonical.parent
        / BACKUP_SUBDIR
        / f"{canonical.stem}{SPM_PROCESS_LOCK_SUFFIX}"
    )


def _retryable_windows_lock_error(exc):
    return (
        getattr(exc, "errno", None)
        in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}
        or getattr(exc, "winerror", None) in {33, 36, 158}
    )


@contextmanager
def spm_exclusive_lock(spm_path, *, log=None, retry_seconds=0.1):
    """Serialize every read/recovery/rewrite of one canonical SPM.

    The file remains as a harmless lock identity under ``_spm_backups``.
    The byte-range lock itself is owned by the OS and is released if a worker
    is killed, so a stale file never means a stale lock.
    """
    lock_path = canonical_spm_process_lock_path(spm_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    waited = False
    with lock_path.open("a+b") as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
            os.fsync(handle.fileno())
        if os.name == "nt":
            import msvcrt

            while True:
                handle.seek(0)
                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError as exc:
                    if not _retryable_windows_lock_error(exc):
                        raise
                    if not waited and log is not None:
                        log(
                            "  [SPM lock] another worker is using the same "
                            f"canonical SPM; waiting: {Path(spm_path).name}"
                        )
                    waited = True
                    time.sleep(max(0.01, float(retry_seconds)))
            try:
                yield lock_path
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield lock_path
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def speedtree_export_gate():
    """Allow one SpeedTree Modeler export machine-wide.

    Waiting happens before the Modeler process is launched, so queue time does
    not consume the per-export timeout.  The same stable mutex name is used by
    the Blender repair add-on's ``speedtree_cli`` module.
    """
    name = os.environ.get(
        SPEEDTREE_EXPORT_MUTEX_ENV, SPEEDTREE_EXPORT_MUTEX_DEFAULT
    )
    if os.name == "nt":
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.argtypes = (
            ctypes.c_void_p,
            ctypes.c_bool,
            ctypes.c_wchar_p,
        )
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        kernel32.WaitForSingleObject.argtypes = (
            ctypes.c_void_p,
            ctypes.c_uint32,
        )
        kernel32.WaitForSingleObject.restype = ctypes.c_uint32
        kernel32.ReleaseMutex.argtypes = (ctypes.c_void_p,)
        kernel32.ReleaseMutex.restype = ctypes.c_bool
        kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
        kernel32.CloseHandle.restype = ctypes.c_bool
        handle = kernel32.CreateMutexW(None, False, name)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        acquired = False
        try:
            result = kernel32.WaitForSingleObject(handle, 0xFFFFFFFF)
            if result not in {0x00000000, 0x00000080}:
                if result == 0xFFFFFFFF:
                    raise ctypes.WinError(ctypes.get_last_error())
                raise RuntimeError(
                    f"SpeedTree export mutex wait returned {result:#x}"
                )
            acquired = True
            yield
        finally:
            if acquired:
                kernel32.ReleaseMutex(handle)
            kernel32.CloseHandle(handle)
        return

    import fcntl

    safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", name)
    lock_path = Path(tempfile.gettempdir()) / f"{safe_name}.lock"
    with lock_path.open("a+b") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


SYNC_MANIFEST_NAME = "spm_generator_sync.json"
ASSET_BONE_PROFILES = {
    "tree": {"root_parent_types": ("Tree",), "base_depth": 1},
    "bush": {"root_parent_types": ("Tree", "Zone"), "base_depth": 2},
    "weed": {"root_parent_types": ("Tree", "Zone"), "base_depth": 2},
    "other": {"root_parent_types": ("Tree",), "base_depth": 1},
}


def _direct_properties(element):
    values = {}
    properties = element.find("Properties")
    for prop in list(properties) if properties is not None else ():
        name = prop.findtext("Name")
        if name:
            values[name] = prop.findtext("Value")
    return values


def _number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _icon_category(generator):
    """Read the explicit role colour written by SPM Generator Sync.

    Brightness varies by Base, so category is inferred from hue rather than an
    exact RGBA tuple. This is metadata, not a generator-name heuristic.
    """
    extra = generator.find("Extra")
    if extra is None or (extra.findtext("m_bSetBackgroundIconColor") or "").lower() != "true":
        return None
    rgb = tuple(
        _number(extra.findtext(f"m_vecBackgroundIconColor_{axis}"))
        for axis in "rgb"
    )
    if any(value is None for value in rgb):
        return None
    red, green, blue = rgb
    if red > max(green, blue) * 1.5:
        return "end"
    if blue > max(red, green) * 1.5:
        return "branch"
    if green > max(red, blue) * 1.5:
        return "leaf"
    return None


def load_sync_base_categories(spm_path):
    """Resolve explicit Base roles for a master or follower SPM."""
    if not spm_path:
        return {}, None
    spm = Path(spm_path)
    manifest = spm.parent / SYNC_MANIFEST_NAME
    if not manifest.exists():
        return {}, None
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}, str(manifest)
    filename = spm.name.casefold()
    for group in data.get("groups", []):
        source_categories = group.get("base_categories") or {}
        if str(group.get("master", "")).casefold() == filename:
            return {
                str(name): category
                for name, category in source_categories.items()
                if category
            }, str(manifest)
        for follower in group.get("followers", []):
            if str(follower.get("file", "")).casefold() != filename:
                continue
            resolved = {}
            for source_name, target_name in (follower.get("base_map") or {}).items():
                category = source_categories.get(source_name)
                if target_name and category:
                    resolved[str(target_name)] = category
            return resolved, str(manifest)
    return {}, str(manifest)


def _descendants(seeds, edges):
    result = set(seeds)
    pending = list(seeds)
    while pending:
        parent = pending.pop()
        for child in edges.get(parent, ()):
            if child not in result:
                result.add(child)
                pending.append(child)
    return result


def _descendants_to_depth(seeds, edges, max_depth):
    """Return seed stage 1 through max_depth, measured in Branch stages."""
    result = set()
    pending = [(seed, 1) for seed in seeds]
    while pending:
        guid, depth = pending.pop()
        if guid in result or depth > max_depth:
            continue
        result.add(guid)
        pending.extend((child, depth + 1) for child in edges.get(guid, ()))
    return result


def classify_asset_kind(spm_path):
    """Classify by filename, then the exact owner of a Cluster child.

    Generic parts such as ``SK_branch_elm_01`` omit the vegetation kind.  For
    an immediate ``Cluster`` parent only, the owner folder one level above is
    the established source of that kind.  Arbitrary ancestor walking is not
    allowed because Bush/Weed assets share a physical ``Tree`` library root.
    """
    path = Path(spm_path) if spm_path else None
    stem = path.stem.casefold() if path else ""
    if stem.startswith("sk_"):
        stem = stem[3:]
    for kind in ("tree", "bush", "weed"):
        if stem == kind or stem.startswith(kind + "_"):
            return kind
    if path and path.parent.name.casefold() == "cluster":
        owner = path.parent.parent.name.casefold()
        for kind in ("tree", "bush", "weed"):
            if owner == kind or owner.startswith(kind + "_"):
                return kind
    return "other"


def analyze_branch_bone_graph(text, spm_path=None, base_categories=None):
    """Select automatic bone generators from Generator/Node GUID ancestry.

    Tree-started Branch chains are targets even when authored as Absolute/0.
    Base-reference descendants are excluded. The only Base exception is its
    first Branch stage when the Base is explicitly a branch role, or a
    non-Classic/Any leaf role. Any Tree/Base GUID overlap is an error.
    """
    root = ET.fromstring(text)
    generators = []
    generator_by_guid = {}
    branch_index_by_guid = {}
    branch_index = -1
    duplicate_generator_guids = set()
    for generator in root.findall(".//Generator"):
        guid = generator.findtext("GUID")
        info = {
            "guid": guid,
            "name": generator.findtext("Name") or "?",
            "type": generator.attrib.get("Type", "?"),
            "hidden": (generator.findtext("Hidden") or "").strip().lower()
            in {"1", "true", "yes"},
            "properties": _direct_properties(generator),
            "icon_category": _icon_category(generator),
        }
        generators.append(info)
        if guid:
            if guid in generator_by_guid:
                duplicate_generator_guids.add(guid)
            generator_by_guid[guid] = info
        if info["type"] == "Branch":
            branch_index += 1
            if guid:
                branch_index_by_guid[guid] = branch_index

    nodes = []
    node_by_guid = {}
    duplicate_node_guids = set()
    for node in root.findall(".//Node"):
        guid = node.findtext("GUID")
        record = {
            "guid": guid,
            "type": node.attrib.get("Type", "?"),
            "generator_guid": node.findtext("GeneratorGUID"),
            "parent_guid": node.findtext("ParentGUID"),
        }
        nodes.append(record)
        if guid:
            if guid in node_by_guid:
                duplicate_node_guids.add(guid)
            node_by_guid[guid] = record

    explicit_categories, manifest_path = load_sync_base_categories(spm_path)
    asset_kind = classify_asset_kind(spm_path)
    profile = ASSET_BONE_PROFILES[asset_kind]
    if base_categories:
        explicit_categories.update(base_categories)
    explicit_categories_folded = {
        str(name).casefold(): category for name, category in explicit_categories.items()
    }

    edges = {}
    tree_seeds = set()
    base_entries = {}
    unresolved_parent_nodes = 0
    for node in nodes:
        child_guid = node["generator_guid"]
        if node["type"] != "Branch" or child_guid not in branch_index_by_guid:
            continue
        parent = node_by_guid.get(node["parent_guid"])
        if parent is None:
            unresolved_parent_nodes += 1
            continue
        parent_generator = generator_by_guid.get(parent["generator_guid"], {})
        if parent["type"] in profile["root_parent_types"]:
            tree_seeds.add(child_guid)
        elif parent["type"] == "Branch" and parent["generator_guid"] in branch_index_by_guid:
            edges.setdefault(parent["generator_guid"], set()).add(child_guid)
        elif parent["type"] == "Base":
            base_guid = parent["generator_guid"]
            base_info = generator_by_guid.get(base_guid, {})
            base_name = base_info.get("name", "?")
            category = explicit_categories.get(base_name)
            category_source = "manifest" if category else None
            if not category:
                category = explicit_categories_folded.get(base_name.casefold())
                category_source = "manifest" if category else None
            if not category:
                category = base_info.get("icon_category")
                category_source = "icon" if category else None
            props = base_info.get("properties", {})
            entry = base_entries.setdefault(
                (base_guid, child_guid),
                {
                    "base_guid": base_guid,
                    "base_name": base_name,
                    "branch_guid": child_guid,
                    "branch_name": generator_by_guid[child_guid]["name"],
                    "category": category,
                    "category_source": category_source,
                    "generation_mode": _number(props.get("Generation:Mode")),
                    "generation_style": _number(props.get("Generation:Style")),
                    "node_count": 0,
                },
            )
            entry["node_count"] += 1

    tree_chain = _descendants(tree_seeds, edges)
    base_seeds = {entry["branch_guid"] for entry in base_entries.values()}
    base_chain = _descendants(base_seeds, edges)
    ambiguous = tree_chain & base_chain

    included_base_entries = set()
    included_base_chain = set()
    unknown_bases = []
    leaf_classic_excluded = []
    base_entry_report = []
    for entry in base_entries.values():
        category = entry["category"]
        include = False
        reason = ""
        if category == "branch":
            include = True
            reason = "branch Base first stage"
        elif category == "leaf":
            classic_any = (
                entry["generation_mode"] == 0.0
                and entry["generation_style"] == 0.0
            )
            if classic_any:
                reason = "Classic+Any leaf Base"
                leaf_classic_excluded.append(entry["base_guid"])
            else:
                include = True
                reason = "non-Classic/Any leaf Base first stage"
        elif category == "end":
            reason = "end Base"
        else:
            reason = "Base role is unknown"
            unknown_bases.append(
                {"guid": entry["base_guid"], "name": entry["base_name"]}
            )
        if include:
            included_base_entries.add(entry["branch_guid"])
            included_base_chain.update(
                _descendants_to_depth(
                    {entry["branch_guid"]}, edges, profile["base_depth"]
                )
            )
        base_entry_report.append({**entry, "included": include, "reason": reason})

    target_guids = tree_chain | included_base_chain
    base_excluded = base_chain - included_base_chain
    errors = []
    if duplicate_generator_guids:
        errors.append(
            "duplicate Generator GUIDs prevent deterministic bone selection: "
            + ", ".join(sorted(duplicate_generator_guids))
        )
    if duplicate_node_guids:
        errors.append(
            "duplicate Node GUIDs prevent deterministic bone selection: "
            + ", ".join(sorted(duplicate_node_guids))
        )
    if ambiguous:
        errors.append(
            "the same Branch Generator GUID is reachable from both Tree and Base: "
            + ", ".join(
                f"{generator_by_guid.get(guid, {}).get('name', '?')}({guid})"
                for guid in sorted(ambiguous)
            )
        )

    missing_properties = []
    hidden_targets = []
    target_indices = []
    target_report = []
    activated_zero = []
    for guid in sorted(target_guids, key=lambda item: branch_index_by_guid[item]):
        info = generator_by_guid[guid]
        index = branch_index_by_guid[guid]
        style = _number(info["properties"].get("Physics:Bone style"))
        bones = _number(info["properties"].get("Physics:Bones"))
        if guid in tree_chain:
            source = "tree"
        elif guid in included_base_entries:
            source = "base_entry"
        else:
            source = "base_internal"
        if info["hidden"]:
            hidden_targets.append({"guid": guid, "name": info["name"]})
            continue
        if style is None or bones is None:
            missing_properties.append({"guid": guid, "name": info["name"]})
            continue
        target_indices.append(index)
        target_report.append(
            {"index": index, "guid": guid, "name": info["name"], "source": source}
        )
        if style == 0.0 and bones == 0.0:
            activated_zero.append({"guid": guid, "name": info["name"]})
    if missing_properties:
        errors.append(
            "automatic Branch targets have no bone properties: "
            + ", ".join(
                f"{item['name']}({item['guid']})" for item in missing_properties
            )
        )
    if not target_indices and any(info["type"] == "Branch" for info in generators):
        detail = ""
        if unknown_bases:
            detail = "; unclassified Base references were excluded: " + ", ".join(
                f"{item['name']}({item['guid']})" for item in unknown_bases
            )
        errors.append("no automatic Branch bone targets were found" + detail)

    expected_target_nodes = sum(
        1
        for node in nodes
        if node["type"] == "Branch" and node["generator_guid"] in target_guids
    )
    ambiguous_report = [
        {"guid": guid, "name": generator_by_guid.get(guid, {}).get("name", "?")}
        for guid in sorted(ambiguous)
    ]
    return {
        "mode": "guid_graph",
        "asset_kind": asset_kind,
        "root_parent_types": list(profile["root_parent_types"]),
        "base_branch_depth_limit": profile["base_depth"],
        "target_indices": target_indices,
        "target_generators": target_report,
        "root_target_generator_count": len(target_indices),
        "tree_target_generator_count": sum(item["source"] == "tree" for item in target_report),
        "base_entry_target_generator_count": sum(
            item["source"] == "base_entry" for item in target_report
        ),
        "base_internal_target_generator_count": sum(
            item["source"] == "base_internal" for item in target_report
        ),
        "base_excluded_generator_count": len(base_excluded),
        "base_excluded_guids": sorted(base_excluded),
        "base_generator_indices": sorted(
            branch_index_by_guid[guid]
            for guid in base_chain
            if guid in branch_index_by_guid
        ),
        "base_generator_count": len(base_chain),
        "ambiguous_shared_guids": ambiguous_report,
        "expected_target_branch_nodes": expected_target_nodes,
        "activated_zero_bone_generators": activated_zero,
        "activated_zero_bone_generator_count": len(activated_zero),
        "base_entries": base_entry_report,
        "unknown_base_generators": unknown_bases,
        "leaf_classic_any_excluded_count": len(set(leaf_classic_excluded)),
        "hidden_targets": hidden_targets,
        "missing_bone_property_targets": missing_properties,
        "unresolved_branch_parent_nodes": unresolved_parent_nodes,
        "sync_manifest": manifest_path,
        "errors": errors,
        "ready": not errors,
    }


def read_spm(path):
    return read_pipeline_spm_text(path)


def write_spm(path, text):
    """Replace one SPM atomically with byte-stable output.

    Two properties matter downstream:
      * ``mtime=0`` keeps the gzip header out of the payload, so identical
        logical content always hashes identically.  Otherwise a no-op ① rewrite
        changes the file's content fingerprint and needlessly invalidates the
        calibration cache, the .blend freshness receipt and the Push manifest.
      * ``os.replace`` means a crash or kill during a write can never leave a
        truncated SPM; the previous file survives untouched.
    """
    candidate = Path(path)
    compressed = (
        spm_container_format(candidate) == "gzip" if candidate.is_file() else True
    )
    payload = text.encode("utf-8")
    temporary = candidate.with_name(f".{candidate.name}.{os.getpid()}.skbatch.tmp")
    try:
        if compressed:
            with temporary.open("wb") as raw:
                with gzip.GzipFile(
                    filename=candidate.name, mode="wb", fileobj=raw, mtime=0
                ) as handle:
                    handle.write(payload)
                raw.flush()
                os.fsync(raw.fileno())
        else:
            with temporary.open("wb") as raw:
                raw.write(payload)
                raw.flush()
                os.fsync(raw.fileno())
        os.replace(temporary, candidate)
    finally:
        if temporary.exists():
            temporary.unlink()


def probe_cache_path(spm_path):
    spm = Path(spm_path)
    return spm.parent / BACKUP_SUBDIR / f"{spm.stem}{PROBE_CACHE_SUFFIX}"


def _probe_dependency_identity(path, hash_content=False):
    candidate = Path(path) if path else None
    if not candidate or not candidate.exists():
        return {"path": str(candidate or ""), "missing": True}
    try:
        stat = candidate.stat()
        identity = {
            "path": str(candidate.resolve()),
            "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
        }
        if hash_content:
            identity["fingerprint"] = file_content_fingerprint(candidate)
        return identity
    except OSError as exc:
        return {"path": str(candidate), "error": str(exc)}


def probe_cache_key(source_text, target_indices, cfg):
    # Bone values are the output of calibration, not probe inputs. Material
    # name prefixing also cannot alter branch count/length, so both are masked.
    normalized = BONE_VALUE_RE.sub(r"\1<CACHED_BONE_VALUE>\2", source_text)
    normalized = MATERIAL_RE.sub(r"\1<CACHED_MATERIAL_NAME>\3", normalized)
    payload = {
        "version": PROBE_CACHE_VERSION,
        "source": hashlib.blake2b(normalized.encode("utf-8"), digest_size=16).hexdigest(),
        "target_indices": list(target_indices),
        "xml_ini": _probe_dependency_identity(cfg.get("xml_ini"), hash_content=True),
        "speedtree_exe": _probe_dependency_identity(cfg.get("speedtree_exe")),
    }
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.blake2b(encoded, digest_size=16).hexdigest()


def load_probe_cache(spm_path, key):
    path = probe_cache_path(spm_path)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        counts = data.get("probe_counts")
        lengths = data.get("probe_lengths")
        if (
            data.get("version") != PROBE_CACHE_VERSION
            or data.get("key") != key
            or not isinstance(counts, dict)
            or not isinstance(lengths, list)
            or sum(int(value) for value in counts.values()) <= 0
        ):
            return None
        return {
            "probe_counts": {str(name): int(value) for name, value in counts.items()},
            "probe_lengths": [float(value) for value in lengths],
        }
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return None


def save_probe_cache(spm_path, key, probe_counts, probe_lengths):
    path = probe_cache_path(spm_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    payload = {
        "version": PROBE_CACHE_VERSION,
        "key": key,
        "probe_counts": probe_counts,
        "probe_lengths": probe_lengths,
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }
    try:
        temp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink()
    return path


def prop_value(block, prop_name):
    m = re.search(
        r"<Name>" + re.escape(prop_name) + r"</Name>\s*<Value>([^<]*)</Value>",
        block,
        re.DOTALL,
    )
    return m.group(1) if m else None


def set_prop_value(block, prop_name, new_value):
    pat = re.compile(
        r"(<Name>" + re.escape(prop_name) + r"</Name>\s*<Value>)[^<]*(</Value>)",
        re.DOTALL,
    )
    return pat.sub(lambda m: m.group(1) + format_value(new_value) + m.group(2), block, count=1)


def _is_leaf_generator_type(generator_type):
    """Return whether a SpeedTree generator creates leaf geometry.

    SpeedTree versions and authoring modes use labels such as ``Leaf Mesh``
    and ``Batched Leaf``. Matching the generator *type* keeps this independent
    of artist-controlled generator names.
    """
    return "leaf" in str(generator_type or "").strip().casefold()


def _direct_vertex_color_properties(generator):
    properties = generator.find("Properties")
    if properties is None:
        return {}
    return {
        prop.findtext("Name"): prop
        for prop in list(properties)
        if prop.findtext("Name")
    }


def _vertex_color_channel_signature(text, channel):
    """Semantic snapshot used to prove another channel was not changed."""
    root = ET.fromstring(text)
    prefix = f"Vertex Color:{channel}:"
    signature = []
    for generator in root.findall(".//Generator"):
        properties = _direct_vertex_color_properties(generator)
        payload = [
            ET.tostring(properties[name], encoding="unicode")
            for name in sorted(properties)
            if name.startswith(prefix)
        ]
        if payload:
            signature.append(
                (
                    generator.findtext("GUID") or "",
                    generator.findtext("Name") or "",
                    tuple(payload),
                )
            )
    encoded = json.dumps(signature, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _set_simple_xml_text(block, tag, text):
    pattern = re.compile(
        r"(<" + re.escape(tag) + r">)[^<]*(</" + re.escape(tag) + r">)",
        re.DOTALL,
    )
    return pattern.sub(
        lambda match: match.group(1) + str(text) + match.group(2),
        block,
        count=1,
    )


def _set_simple_xml_value(block, tag, value):
    return _set_simple_xml_text(block, tag, format_value(value))


def _linearize_spline_profile(block, prop_name):
    """Set one SplineProperty profile to Y=X without reserializing the SPM."""
    property_pattern = re.compile(
        r"<SplineProperty>\s*<Name>"
        + re.escape(prop_name)
        + r"</Name>.*?</SplineProperty>",
        re.DOTALL,
    )
    property_match = property_pattern.search(block)
    if not property_match:
        return block, f"missing SplineProperty: {prop_name}"

    property_block = property_match.group(0)
    profile_pattern = re.compile(
        r"(<ProfileSpline\b[^>]*>)(.*?)(</ProfileSpline>)", re.DOTALL
    )
    profile_matches = list(profile_pattern.finditer(property_block))
    if not profile_matches:
        return block, f"missing ProfileSpline: {prop_name}"
    if len(profile_matches) != 1:
        return block, f"expected exactly one ProfileSpline: {prop_name}"
    profile_match = profile_matches[0]

    control_point_pattern = re.compile(
        r"<ControlPoint>.*?</ControlPoint>", re.DOTALL
    )
    control_points = list(control_point_pattern.finditer(profile_match.group(2)))
    if len(control_points) < 2:
        return block, f"ProfileSpline needs at least two points: {prop_name}"

    required_tags = ("X", "Y", "TangentX", "TangentY")

    def parse_control_point(point_text, point_index):
        values = {}
        for tag in required_tags:
            matches = re.findall(
                r"<" + re.escape(tag) + r">([^<]*)</" + re.escape(tag) + r">",
                point_text,
            )
            if len(matches) != 1:
                return None, (
                    f"ProfileSpline point {point_index} needs exactly one {tag}: "
                    f"{prop_name}"
                )
            try:
                value = float(matches[0])
            except (TypeError, ValueError):
                return None, (
                    f"ProfileSpline point {point_index} has an invalid {tag}: "
                    f"{prop_name}"
                )
            if not math.isfinite(value):
                return None, (
                    f"ProfileSpline point {point_index} has a non-finite {tag}: "
                    f"{prop_name}"
                )
            values[tag] = value
        return values, None

    parsed_points = []
    for point_index, point in enumerate(control_points):
        values, point_error = parse_control_point(point.group(0), point_index)
        if point_error:
            return block, point_error
        parsed_points.append(values)

    x_values = [values["X"] for values in parsed_points]
    if any(value < 0.0 or value > 1.0 for value in x_values):
        return block, f"ProfileSpline X coordinates must stay in [0, 1]: {prop_name}"
    if any(
        current < previous
        for previous, current in zip(x_values, x_values[1:])
    ):
        return block, f"ProfileSpline X coordinates are out of order: {prop_name}"
    if not math.isclose(x_values[0], 0.0, abs_tol=1e-6) or not math.isclose(
        x_values[-1], 1.0, abs_tol=1e-6
    ):
        return block, f"ProfileSpline endpoints are not 0 and 1: {prop_name}"

    def patch_control_point(match):
        point = match.group(0)
        x_match = re.search(r"<X>([^<]*)</X>", point)
        point = _set_simple_xml_text(point, "Y", x_match.group(1))
        point = _set_simple_xml_value(point, "TangentX", 0.0)
        point = _set_simple_xml_value(point, "TangentY", 0.0)
        return point

    profile_inner = control_point_pattern.sub(
        patch_control_point, profile_match.group(2)
    )
    patched_control_points = list(control_point_pattern.finditer(profile_inner))
    if len(patched_control_points) != len(parsed_points):
        return block, f"ProfileSpline control-point count changed: {prop_name}"
    for point_index, (original, patched_point) in enumerate(
        zip(parsed_points, patched_control_points)
    ):
        patched_values, point_error = parse_control_point(
            patched_point.group(0), point_index
        )
        if point_error:
            return block, point_error
        if not math.isclose(
            patched_values["X"], original["X"], abs_tol=1e-12
        ):
            return block, f"ProfileSpline point {point_index} changed X: {prop_name}"
        if not math.isclose(
            patched_values["Y"], patched_values["X"], abs_tol=1e-6
        ):
            return block, (
                f"ProfileSpline point {point_index} failed Y=X postcondition: "
                f"{prop_name}"
            )
        if not math.isclose(
            patched_values["TangentX"], 0.0, abs_tol=1e-6
        ) or not math.isclose(patched_values["TangentY"], 0.0, abs_tol=1e-6):
            return block, (
                f"ProfileSpline point {point_index} failed zero-tangent postcondition: "
                f"{prop_name}"
            )
    patched_property = (
        property_block[: profile_match.start(2)]
        + profile_inner
        + property_block[profile_match.end(2) :]
    )
    patched_block = (
        block[: property_match.start()]
        + patched_property
        + block[property_match.end() :]
    )
    return patched_block, None


def _vertex_color_channel_summary(generator, channel):
    properties = _direct_vertex_color_properties(generator)
    style = properties.get(f"Vertex Color:{channel}:Style")
    value = properties.get(f"Vertex Color:{channel}:Value")
    profile = []
    if value is not None:
        for point in value.findall("./ProfileSpline/ControlPoint"):
            profile.append(
                {
                    "x": _number(point.findtext("X")),
                    "y": _number(point.findtext("Y")),
                    "tangent_x": _number(point.findtext("TangentX")),
                    "tangent_y": _number(point.findtext("TangentY")),
                }
            )
    return {
        "style": _number(style.findtext("Value")) if style is not None else None,
        "value": _number(value.findtext("Value")) if value is not None else None,
        "profile": profile,
    }


def apply_leaf_parent_red_gradient(text):
    """Author R on direct leaf-parent Branch generators, preserving G/B/A."""
    root = ET.fromstring(text)
    generators = root.findall(".//Generator")
    generator_by_guid = {
        generator.findtext("GUID"): generator
        for generator in generators
        if generator.findtext("GUID")
    }
    nodes = root.findall(".//Node")
    node_by_guid = {
        node.findtext("GUID"): node
        for node in nodes
        if node.findtext("GUID")
    }
    target_info = {}
    warnings = []
    for leaf_node in nodes:
        if not _is_leaf_generator_type(leaf_node.attrib.get("Type")):
            continue
        leaf_node_guid = leaf_node.findtext("GUID") or ""
        parent_guid = leaf_node.findtext("ParentGUID") or ""
        parent_node = node_by_guid.get(parent_guid)
        if parent_node is None:
            warnings.append(
                f"leaf node {leaf_node_guid or '?'} has no resolvable ParentGUID"
            )
            continue
        if parent_node.attrib.get("Type") != "Branch":
            continue
        branch_generator_guid = parent_node.findtext("GeneratorGUID") or ""
        branch_generator = generator_by_guid.get(branch_generator_guid)
        if branch_generator is None or branch_generator.attrib.get("Type") != "Branch":
            warnings.append(
                f"leaf node {leaf_node_guid or '?'} has a Branch parent whose "
                f"GeneratorGUID is not a Branch generator: {branch_generator_guid or '?'}"
            )
            continue
        leaf_generator_guid = leaf_node.findtext("GeneratorGUID") or ""
        leaf_generator = generator_by_guid.get(leaf_generator_guid)
        info = target_info.setdefault(
            branch_generator_guid,
            {
                "guid": branch_generator_guid,
                "name": branch_generator.findtext("Name") or "?",
                "leaf_nodes": [],
                "before": _vertex_color_channel_summary(branch_generator, "Red"),
            },
        )
        info["leaf_nodes"].append(
            {
                "node_guid": leaf_node_guid,
                "generator_guid": leaf_generator_guid,
                "generator_name": leaf_generator.findtext("Name")
                if leaf_generator is not None
                else "?",
                "generator_type": leaf_generator.attrib.get("Type", "?")
                if leaf_generator is not None
                else leaf_node.attrib.get("Type", "?"),
            }
        )

    green_before = _vertex_color_channel_signature(text, "Green")
    errors = []
    changed_generators = []
    out = []
    position = 0
    for match in GEN_RE.finditer(text):
        block = match.group(0)
        guid_match = FIRST_GUID_RE.search(block)
        guid = guid_match.group(1) if guid_match else None
        if guid in target_info:
            style_name = "Vertex Color:Red:Style"
            value_name = "Vertex Color:Red:Value"
            if prop_value(block, style_name) is None:
                errors.append(
                    f"{target_info[guid]['name']}({guid}) has no {style_name}"
                )
            elif prop_value(block, value_name) is None:
                errors.append(
                    f"{target_info[guid]['name']}({guid}) has no {value_name}"
                )
            else:
                patched = set_prop_value(block, style_name, 0.0)
                patched = set_prop_value(patched, value_name, 1.0)
                patched, profile_error = _linearize_spline_profile(
                    patched, value_name
                )
                if profile_error:
                    errors.append(
                        f"{target_info[guid]['name']}({guid}): {profile_error}"
                    )
                elif patched != block:
                    block = patched
                    changed_generators.append(guid)
        out.append(text[position : match.start()])
        out.append(block)
        position = match.end()
    out.append(text[position:])
    patched_text = "".join(out)

    # Atomic patch: an unsupported target must not leave a half-authored tree.
    if errors:
        patched_text = text
        changed_generators = []

    patched_root = ET.fromstring(patched_text)
    patched_by_guid = {
        generator.findtext("GUID"): generator
        for generator in patched_root.findall(".//Generator")
        if generator.findtext("GUID")
    }
    targets = []
    for guid, info in target_info.items():
        after_generator = patched_by_guid.get(guid)
        targets.append(
            {
                **info,
                "after": _vertex_color_channel_summary(after_generator, "Red")
                if after_generator is not None
                else {},
                "changed": guid in changed_generators,
            }
        )

    green_after = _vertex_color_channel_signature(patched_text, "Green")
    report = {
        "channel": "VertexColor.R",
        "selection": (
            "GeneratorGUID of the immediate Branch parent Node for each "
            "leaf-type Node, deduplicated by Branch generator GUID"
        ),
        "authoring": "Set (Style 0), Value 1, Profile Y=X (root 0 -> tip 1)",
        "target_count": len(targets),
        "leaf_node_count": sum(len(item["leaf_nodes"]) for item in targets),
        "changed_generator_count": len(changed_generators),
        "targets": targets,
        "errors": errors,
        "warnings": warnings,
        "green_signature_before": green_before,
        "green_signature_after": green_after,
        "green_unchanged": green_before == green_after,
    }
    return patched_text, report


def format_value(value):
    value = float(value)
    if value == int(value):
        return str(int(value))
    return f"{value:.4f}".rstrip("0").rstrip(".")


def audit_spm(path, text=None, analyze_bone_graph=False):
    text = read_spm(path) if text is None else text
    generators = []
    for m in GEN_RE.finditer(text):
        block = m.group(0)
        tm = GEN_TYPE_RE.match(block)
        gtype = tm.group(1) if tm else "?"
        nm = FIRST_NAME_RE.search(block)
        gname = nm.group(1) if nm else "?"
        gm = FIRST_GUID_RE.search(block)
        guid = gm.group(1) if gm else None
        if gtype != "Branch":
            continue
        style = prop_value(block, "Physics:Bone style")
        bones = prop_value(block, "Physics:Bones")
        hidden_match = re.search(r"<Hidden>([^<]*)</Hidden>", block)
        hidden = bool(hidden_match and hidden_match.group(1).strip().lower() in {"1", "true", "yes"})
        generators.append(
            {
                "name": gname,
                "guid": guid,
                "type": gtype,
                "style": float(style) if style is not None else None,
                "bones": float(bones) if bones is not None else None,
                "hidden": hidden,
            }
        )
    materials = [
        {"name": m.group(2), "needs_prefix": not m.group(2).startswith("M_")}
        for m in MATERIAL_RE.finditer(text)
    ]
    result = {"path": str(path), "generators": generators, "materials": materials}
    if analyze_bone_graph:
        result["bone_graph"] = analyze_branch_bone_graph(text, spm_path=path)
    return result


def sk_readiness(audit):
    """Classify whether an SPM may enter the skeletal batch pipeline.

    BranchMesh-only assets legitimately have no SpeedTree bone generator and
    are handled later by Blender's rigid one-bone fallback.  A visible Branch
    generator whose bone properties are all Absolute/0 is different: the asset
    has not been authored for the SK path yet, so silently manufacturing a
    skeleton would hide bad source data.
    """
    visible = [
        gen
        for gen in audit.get("generators", [])
        if not gen.get("hidden", False)
        and gen.get("style") is not None
        and gen.get("bones") is not None
    ]
    enabled = [
        gen
        for gen in visible
        if not (gen["style"] == 0.0 and gen["bones"] == 0.0)
    ]
    disabled = [gen for gen in visible if gen not in enabled]
    details = [
        {
            "generator": gen.get("name", "?"),
            "style": gen.get("style"),
            "bones": gen.get("bones"),
        }
        for gen in disabled
    ]
    graph = audit.get("bone_graph")
    if graph is not None:
        if graph.get("errors"):
            error = "SPM bone GUID graph is not SK-ready: " + "; ".join(graph["errors"])
            return {
                "ready": False,
                "mode": "bone_graph_error",
                "visible_branch_generators": len(visible),
                "enabled_branch_generators": len(enabled),
                "disabled_generators": details,
                "bone_graph": graph,
                "error": error,
            }
        if graph.get("target_indices"):
            return {
                "ready": True,
                "mode": "speedtree_bones",
                "visible_branch_generators": len(visible),
                "enabled_branch_generators": len(enabled),
                "disabled_generators": details,
                "bone_graph": graph,
            }
    if visible and not enabled:
        settings = ", ".join(
            f"{item['generator']}(style={item['style']:g}, bones={item['bones']:g})"
            for item in details
        )
        error = (
            "SPM is not SK-ready: every visible Branch bone generator is Absolute/0 "
            f"(bones disabled): {settings}. Configure bones on at least one visible "
            "Branch generator before running the SK batch."
        )
        return {
            "ready": False,
            "mode": "all_bones_disabled",
            "visible_branch_generators": len(visible),
            "enabled_branch_generators": 0,
            "disabled_generators": details,
            "error": error,
        }
    return {
        "ready": True,
        "mode": "speedtree_bones" if enabled else "no_branch_generators",
        "visible_branch_generators": len(visible),
        "enabled_branch_generators": len(enabled),
        "disabled_generators": details,
    }


def plan_material_renames(audit):
    renames = {}
    skipped = []
    existing = {m["name"] for m in audit["materials"]}
    for mat in audit["materials"]:
        if not mat["needs_prefix"]:
            continue
        target = "M_" + mat["name"]
        if target in existing:
            skipped.append({"material": mat["name"], "reason": f"rename target exists: {target}"})
            continue
        renames[mat["name"]] = target
    return renames, skipped


def apply_branch_values(text, indices, style, bones):
    """Set style/bones on the Branch generators at the given positional indices.

    Indices are counted over Branch-type generators in document order (the same
    order audit_spm walks them). This sidesteps duplicate generator NAMES —
    elm_03 has three 'Bifurcating' and three 'Branch 4' — which a name-keyed
    patch would collide on.
    """
    index_set = set(indices)
    out = []
    pos = 0
    branch_i = -1
    for m in GEN_RE.finditer(text):
        block = m.group(0)
        tm = GEN_TYPE_RE.match(block)
        if tm and tm.group(1) == "Branch":
            branch_i += 1
            if branch_i in index_set:
                block = set_prop_value(block, "Physics:Bone style", style)
                block = set_prop_value(block, "Physics:Bones", bones)
        out.append(text[pos:m.start()])
        out.append(block)
        pos = m.end()
    out.append(text[pos:])
    return "".join(out)


def apply_branch_hidden_values(text, indices, hidden):
    """Set the direct Hidden flag on Branch generators by document index."""
    index_set = set(indices)
    value = "true" if hidden else "false"
    out = []
    pos = 0
    branch_i = -1
    for match in GEN_RE.finditer(text):
        block = match.group(0)
        type_match = GEN_TYPE_RE.match(block)
        if type_match and type_match.group(1) == "Branch":
            branch_i += 1
            if branch_i in index_set:
                hidden_match = re.search(
                    r"(<Hidden>)[^<]*(</Hidden>)",
                    block,
                )
                if hidden_match:
                    block = (
                        block[: hidden_match.start()]
                        + hidden_match.group(1)
                        + value
                        + hidden_match.group(2)
                        + block[hidden_match.end() :]
                    )
                elif "<Properties>" in block:
                    block = block.replace(
                        "<Properties>",
                        f"<Hidden>{value}</Hidden><Properties>",
                        1,
                    )
                else:
                    raise RuntimeError(
                        "Branch generator has no Hidden or Properties field "
                        f"at index {branch_i}"
                    )
        out.append(text[pos : match.start()])
        out.append(block)
        pos = match.end()
    out.append(text[pos:])
    return "".join(out)


def is_cluster_normalization_spm(spm_path):
    """Return whether an SPM is a Cluster prototype source.

    Cluster prototype SPMs have a different skeleton contract from whole-tree
    assets.  Their authored hierarchy may contain hundreds of visible terminal
    spines, but Blender normalization needs one SpeedTree root bone per
    top-level Cluster piece, not one bone on every terminal spine.
    """
    path = Path(spm_path)
    return (
        path.parent.name.casefold() == "cluster"
        or path.stem.casefold().startswith("sk_cluster_")
    )


def _cluster_branch_has_render_geometry(properties):
    """Return whether a Branch generator contributes renderable geometry."""
    skin_type = _number(properties.get("Skin:Type"))
    skin_visibility = _number(properties.get("Skin:Visibility"))
    segment_mesh = str(
        properties.get("Segments:Features:Mesh:Enabled") or ""
    ).strip().casefold() in {"1", "true", "yes"}
    has_visible_skin = (
        skin_type is not None
        and skin_type != 3.0
        and (skin_visibility is None or skin_visibility > 0.0)
    )
    return bool(has_visible_skin or segment_mesh)


def plan_cluster_root_bones(text):
    """Select the first renderable Branch node below each Tree root.

    Hidden Branch nodes and visible ``Skin:Type=3`` Branches are authoring
    scaffolds.  The latter are commonly long Trunk spines used only as Cluster
    placement pivots; they have no mesh and must never become exported bones.
    Traversing through both kinds and stopping at the first Branch that
    contributes real skin/segment-mesh geometry reproduces the structural roots
    seen in the known-good elm and elm-side Cluster exports:

    * Tree -> hidden Trunk -> visible Trunk 3
    * Tree -> hidden Trunk -> hidden Trunk 4 -> visible Big 4
    * Tree -> meshless pivot Trunk -> visible Branch 5

    Every selected generator is authored as Absolute/1.  Every other Branch
    generator is authored as Absolute/0, so terminal leaf/needle spines cannot
    inflate the exported skeleton.
    """
    root = ET.fromstring(text)
    branch_generators = []
    generator_by_guid = {}
    duplicate_generator_guids = set()
    for generator in root.findall(".//Generator"):
        if generator.attrib.get("Type") != "Branch":
            continue
        guid = generator.findtext("GUID")
        info = {
            "index": len(branch_generators),
            "guid": guid,
            "name": generator.findtext("Name") or "?",
            "hidden": (generator.findtext("Hidden") or "").strip().lower()
            in {"1", "true", "yes"},
            "properties": _direct_properties(generator),
        }
        info["has_render_geometry"] = _cluster_branch_has_render_geometry(
            info["properties"]
        )
        branch_generators.append(info)
        if guid:
            if guid in generator_by_guid:
                duplicate_generator_guids.add(guid)
            generator_by_guid[guid] = info

    nodes = []
    node_by_guid = {}
    children_by_parent = {}
    duplicate_node_guids = set()
    for node in root.findall(".//Node"):
        record = {
            "guid": node.findtext("GUID"),
            "type": node.attrib.get("Type", "?"),
            "generator_guid": node.findtext("GeneratorGUID"),
            "parent_guid": node.findtext("ParentGUID"),
        }
        nodes.append(record)
        if record["guid"]:
            if record["guid"] in node_by_guid:
                duplicate_node_guids.add(record["guid"])
            node_by_guid[record["guid"]] = record
        children_by_parent.setdefault(record["parent_guid"], []).append(record)

    errors = []
    if duplicate_generator_guids:
        errors.append(
            "duplicate Branch Generator GUIDs prevent Cluster root selection: "
            + ", ".join(sorted(duplicate_generator_guids))
        )
    if duplicate_node_guids:
        errors.append(
            "duplicate Node GUIDs prevent Cluster root selection: "
            + ", ".join(sorted(duplicate_node_guids))
        )

    selected_nodes = []
    meshless_pivot_nodes = []
    visited = set()

    def walk_children(parent_guid):
        if parent_guid in visited:
            errors.append(
                f"Node hierarchy cycle encountered below {parent_guid or '<root>'}"
            )
            return
        visited.add(parent_guid)
        try:
            for child in children_by_parent.get(parent_guid, ()):
                if child["type"] != "Branch":
                    # Zone/Base-style containers may sit between the Tree and
                    # the first authored Branch.  They are transparent here.
                    walk_children(child["guid"])
                    continue
                generator = generator_by_guid.get(child["generator_guid"])
                if generator is None:
                    errors.append(
                        "Cluster root Branch node references an unknown generator: "
                        f"{child['generator_guid'] or '<missing>'}"
                    )
                    continue
                if not generator["has_render_geometry"]:
                    meshless_pivot_nodes.append(
                        {
                            "node_guid": child["guid"],
                            "generator_guid": generator["guid"],
                            "generator_index": generator["index"],
                            "generator_name": generator["name"],
                            "was_hidden": generator["hidden"],
                        }
                    )
                    walk_children(child["guid"])
                elif generator["hidden"]:
                    walk_children(child["guid"])
                else:
                    selected_nodes.append(
                        {
                            "node_guid": child["guid"],
                            "generator_guid": generator["guid"],
                            "generator_index": generator["index"],
                            "generator_name": generator["name"],
                        }
                    )
        finally:
            visited.remove(parent_guid)

    tree_nodes = [node for node in nodes if node["type"] == "Tree"]
    for tree_node in tree_nodes:
        walk_children(tree_node["guid"])

    selected_indices = sorted(
        {item["generator_index"] for item in selected_nodes}
    )
    meshless_pivot_indices = sorted(
        {item["generator_index"] for item in meshless_pivot_nodes}
    )
    for index in selected_indices:
        properties = branch_generators[index]["properties"]
        if (
            "Physics:Bone style" not in properties
            or "Physics:Bones" not in properties
        ):
            errors.append(
                "Cluster structural root generator has no bone properties: "
                + branch_generators[index]["name"]
            )
    if not tree_nodes:
        errors.append("Cluster SPM has no Tree node")
    if not selected_nodes:
        errors.append(
            "Cluster SPM has no renderable structural Branch below its Tree root"
        )

    root_name_counts = {}
    for item in selected_nodes:
        name = item["generator_name"]
        root_name_counts[name] = root_name_counts.get(name, 0) + 1

    return {
        "mode": "cluster_first_renderable_root_absolute_1",
        "branch_generator_count": len(branch_generators),
        "selected_generator_indices": selected_indices,
        "selected_generators": [
            {
                "index": branch_generators[index]["index"],
                "guid": branch_generators[index]["guid"],
                "name": branch_generators[index]["name"],
            }
            for index in selected_indices
        ],
        "selected_nodes": selected_nodes,
        "meshless_pivot_generator_indices": meshless_pivot_indices,
        "meshless_pivot_generators": [
            {
                "index": branch_generators[index]["index"],
                "guid": branch_generators[index]["guid"],
                "name": branch_generators[index]["name"],
                "was_hidden": branch_generators[index]["hidden"],
            }
            for index in meshless_pivot_indices
        ],
        "meshless_pivot_nodes": meshless_pivot_nodes,
        "expected_root_bone_count": len(selected_nodes),
        "expected_root_generator_counts": root_name_counts,
        "disabled_generator_count": (
            len(branch_generators) - len(selected_indices)
        ),
        "errors": errors,
        "ready": not errors,
    }


def apply_cluster_root_bone_plan(text, plan):
    """Author one Absolute bone on Cluster roots and zero everywhere else."""
    if not plan.get("ready"):
        raise RuntimeError(
            "Cluster root bone plan is invalid: "
            + "; ".join(plan.get("errors") or ["unknown error"])
        )
    branch_count = int(plan["branch_generator_count"])
    patched = apply_branch_values(
        text, range(branch_count), 0.0, 0.0
    )
    patched = apply_branch_values(
        patched,
        plan["selected_generator_indices"],
        0.0,
        1.0,
    )
    return apply_branch_hidden_values(
        patched,
        plan.get("meshless_pivot_generator_indices") or (),
        True,
    )


def cluster_root_logical_postcondition(text):
    """Prove that the final Cluster XML is the normalizer's fixed point."""
    try:
        plan = plan_cluster_root_bones(text)
        if not plan.get("ready"):
            return {
                "ok": False,
                "mode": plan.get("mode"),
                "errors": list(plan.get("errors") or ()),
                "expected_root_bone_count": plan.get(
                    "expected_root_bone_count"
                ),
            }
        expected = apply_cluster_root_bone_plan(text, plan)
    except (ET.ParseError, RuntimeError, TypeError, ValueError) as exc:
        return {
            "ok": False,
            "mode": "cluster_root_logical_postcondition_error",
            "errors": [str(exc)],
        }
    return {
        "ok": expected == text,
        "mode": plan.get("mode"),
        "expected_root_bone_count": plan.get("expected_root_bone_count"),
        "selected_generator_indices": list(
            plan.get("selected_generator_indices") or ()
        ),
        "disabled_generator_count": plan.get("disabled_generator_count"),
        "errors": (
            []
            if expected == text
            else [
                "final Cluster SPM is not the fixed point of its current "
                "render-geometry root bone plan"
            ]
        ),
    }


def apply_prioritized_branch_values(
    text, relative_indices, relative_value, disabled_base_indices=()
):
    """Disable capped Base targets, then set Relative density on what remains."""
    patched = apply_branch_values(text, disabled_base_indices, 0.0, 0.0)
    return apply_branch_values(patched, relative_indices, 1.0, relative_value)


def apply_material_renames(text, renames):
    applied = []

    def sub(m):
        old = m.group(2)
        new = renames.get(old)
        if not new:
            return m.group(0)
        applied.append((old, new))
        return m.group(1) + new + m.group(3)

    return MATERIAL_RE.sub(sub, text), applied


def target_generators_have_base_links(text, target_indices):
    """Whether selected Branch generators actually produce Branch<-Base nodes.

    Base/BaseRef nodes elsewhere in the SPM are irrelevant when their Branch
    generator has bones disabled (the birch failures had hundreds of those).
    """
    root = ET.fromstring(text)
    branch_guids = []
    for gen in root.findall(".//Generator"):
        if gen.attrib.get("Type") != "Branch":
            continue
        guid = gen.findtext("GUID")
        branch_guids.append(guid)
    target_guids = {
        branch_guids[index]
        for index in target_indices
        if 0 <= index < len(branch_guids) and branch_guids[index]
    }
    if not target_guids:
        return False
    node_types = {}
    nodes = []
    for node in root.findall(".//Node"):
        guid = node.findtext("GUID")
        if guid:
            node_types[guid] = node.attrib.get("Type")
        nodes.append(node)
    return any(
        node.attrib.get("Type") == "Branch"
        and node.findtext("GeneratorGUID") in target_guids
        and node_types.get(node.findtext("ParentGUID")) == "Base"
        for node in nodes
    )


def repair_external_frond_material_references(text):
    """Fallback Frond generators from external atlas cutouts to embedded ones.

    Some atlas-edited SPMs point procedural Frond generators at materials whose
    cutout meshes are external single-plate FBXs. SpeedTree accepts the SPM but
    exports an armature-only FBX. Keep the added material asset intact and only
    restore the generator reference to the closest embedded-cutout material.
    """
    root = ET.fromstring(text)
    meshes = {}
    for mesh in root.findall(".//Mesh"):
        mesh_id = mesh.get("ID")
        if not mesh_id:
            continue
        meshes[mesh_id] = (mesh.findtext("Embedded") or "false").strip().lower() in {"1", "true", "yes"}

    materials = {}
    for material in root.findall(".//Material_v8"):
        material_id = material.get("ID")
        if material_id:
            materials[material_id] = {
                "id": material_id,
                "name": material.get("Name", ""),
                "mesh_id": material.findtext("CutoutMeshID") or "",
            }

    safe_materials = [
        material
        for material in materials.values()
        if material["mesh_id"] in {"", "-1"} or meshes.get(material["mesh_id"], False)
    ]
    if not safe_materials:
        return text, []

    def name_tokens(name):
        return {
            token
            for token in re.split(r"[^a-z0-9]+", (name or "").lower())
            if token and token not in {"m", "atlas"}
        }

    replacements = {}
    for match in re.finditer(
        r"<Name>(Material:Frond:[^<]+:Material)</Name>\s*<Value>([^<]+)</Value>",
        text,
    ):
        property_name, material_id = match.groups()
        material = materials.get(material_id)
        if (
            not material
            or material["mesh_id"] in {"", "-1"}
            or meshes.get(material["mesh_id"], False)
        ):
            continue
        bad_tokens = name_tokens(material["name"])
        fallback = max(
            safe_materials,
            key=lambda candidate: (
                len(bad_tokens & name_tokens(candidate["name"])),
                -len(bad_tokens ^ name_tokens(candidate["name"])),
                candidate["name"],
            ),
        )
        replacements[material_id] = fallback["id"]

    if not replacements:
        return text, []

    changes = []

    def replace(match):
        old_id = match.group(2)
        new_id = replacements.get(old_id)
        if not new_id:
            return match.group(0)
        changes.append(
            {
                "property": match.group(1),
                "old_material_id": old_id,
                "old_material": materials[old_id]["name"],
                "new_material_id": new_id,
                "new_material": materials[new_id]["name"],
                "reason": "external Frond cutout produced geometry-less FBX",
            }
        )
        return f"<Name>{match.group(1)}</Name>\n<Value>{new_id}</Value>"

    patched = re.sub(
        r"<Name>(Material:Frond:[^<]+:Material)</Name>\s*<Value>([^<]+)</Value>",
        replace,
        text,
    )
    return patched, changes


def backup_spm(path):
    # Keep backups out of the working folder so the SPM list stays clean:
    # <spm dir>/_spm_backups/<stem>.skbatch_backup_<ts>.spm
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_dir = Path(path).parent / BACKUP_SUBDIR
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup = backup_dir / f"{Path(path).stem}.skbatch_backup_{ts}.spm"
    shutil.copy2(path, backup)
    return str(backup)


def _terminate_speedtree_tree(process):
    """Kill a timed-out Modeler and every descendant it launched."""
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill.exe", "/PID", str(process.pid), "/T", "/F"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=30,
                check=False,
                creationflags=0x08000000,  # CREATE_NO_WINDOW
            )
        except (OSError, subprocess.SubprocessError):
            pass
    else:
        try:
            process.terminate()
        except OSError:
            pass
    try:
        process.wait(timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        try:
            process.kill()
            process.wait(timeout=5)
        except (OSError, subprocess.SubprocessError):
            pass


def run_speedtree_export(cmd, cwd, timeout):
    """Run one Modeler export, waiting on the process handle, not on pipe EOF.

    SpeedTree is a GUI executable even under ``-export``: descendants can keep
    an inherited stdout/stderr pipe open after the Modeler process itself has
    exited.  ``subprocess.run(capture_output=True)`` then blocks on EOF until
    the timeout fires and a finished 16s export is reported as a 120s failure.
    Regular temporary files remove that failure mode; this mirrors the add-on's
    ``speedtree_cli._run_process`` contract.
    """
    popen_kwargs = {"cwd": str(cwd), "stdin": subprocess.DEVNULL}
    if os.name == "nt":
        popen_kwargs["creationflags"] = 0x08000000 | getattr(  # CREATE_NO_WINDOW
            subprocess, "CREATE_NEW_PROCESS_GROUP", 0
        )
    else:
        popen_kwargs["start_new_session"] = True

    def _read(handle):
        handle.flush()
        handle.seek(0)
        return handle.read().decode("utf-8", errors="replace")

    with speedtree_export_gate():
        with tempfile.TemporaryFile(
            mode="w+b"
        ) as out_file, tempfile.TemporaryFile(mode="w+b") as err_file:
            process = subprocess.Popen(
                cmd, stdout=out_file, stderr=err_file, **popen_kwargs
            )
            try:
                returncode = process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                _terminate_speedtree_tree(process)
                raise
            return returncode, _read(out_file), _read(err_file)


def calibration_marker_path(spm_path):
    """Crash marker beside the backups, outside the scanned working list."""
    spm = Path(spm_path)
    return spm.parent / BACKUP_SUBDIR / f"{spm.stem}{CALIBRATION_MARKER_SUFFIX}"


def write_calibration_marker(spm_path, backup, source_sha256):
    """Record that the SPM is mid-rewrite before the first destructive write.

    Calibration edits the source SPM in place (Absolute/1 probe, then Relative
    rounds) and restores it afterwards.  Every *exception* path restores, but a
    hard kill has none: Stop and the watchdog timeout both use
    ``taskkill /T /F``, so a killed run can leave the SPM in its probe state.
    This marker makes that state detectable and repairable on the next scan
    instead of silently shipping probe bones into ② and ③.
    """
    marker = calibration_marker_path(spm_path)
    marker.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": CALIBRATION_MARKER_VERSION,
        "spm": str(Path(spm_path)),
        "backup": str(backup) if backup else "",
        "source_sha256": source_sha256,
        "pid": os.getpid(),
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "note": (
            "Calibration was interrupted before it could restore this SPM. "
            "Restore the recorded backup before trusting its bone settings."
        ),
    }
    marker.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return marker


def clear_calibration_marker(spm_path):
    marker = calibration_marker_path(spm_path)
    try:
        marker.unlink()
        return True
    except FileNotFoundError:
        return False
    except OSError:
        return False


def _sha256_bytes(payload):
    return hashlib.sha256(payload).hexdigest()


def inspect_interrupted_calibration(spm_path):
    """Report whether a previous ① run was killed while rewriting this SPM."""
    marker = calibration_marker_path(spm_path)
    if not marker.is_file():
        return {"status": "clean", "marker": str(marker)}
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return {
            "status": "unreadable_marker",
            "marker": str(marker),
            "error": str(exc),
        }
    backup = Path(payload.get("backup") or "")
    result = {
        "status": "interrupted",
        "marker": str(marker),
        "spm": payload.get("spm", str(spm_path)),
        "backup": str(backup) if payload.get("backup") else "",
        "started_at": payload.get("started_at", ""),
        "source_sha256": payload.get("source_sha256", ""),
        "backup_available": bool(payload.get("backup")) and backup.is_file(),
    }
    try:
        result["spm_matches_source"] = (
            _sha256_bytes(Path(spm_path).read_bytes())
            == str(payload.get("source_sha256") or "")
        )
    except OSError:
        result["spm_matches_source"] = False
    if result["spm_matches_source"]:
        # The kill landed between two writes that happen to reproduce the
        # source bytes, so nothing was actually lost.
        result["status"] = "interrupted_but_intact"
    return result


def recover_interrupted_calibration(spm_path):
    """Restore a killed calibration's source SPM from its recorded backup."""
    state = inspect_interrupted_calibration(spm_path)
    if state["status"] == "clean":
        return {**state, "recovered": False}
    if state["status"] == "interrupted_but_intact":
        clear_calibration_marker(spm_path)
        return {**state, "recovered": False, "cleared": True}
    if state["status"] == "unreadable_marker" or not state.get("backup_available"):
        return {**state, "recovered": False}
    backup = Path(state["backup"])
    if state.get("source_sha256") and _sha256_bytes(
        backup.read_bytes()
    ) != state["source_sha256"]:
        return {
            **state,
            "recovered": False,
            "error": "recorded backup does not match the interrupted source hash",
        }
    shutil.copy2(backup, spm_path)
    clear_calibration_marker(spm_path)
    return {**state, "recovered": True, "cleared": True}


def export_verify_xml(spm_path, cfg, out_path):
    require_texture_skip_writing(
        cfg["xml_ini"],
        purpose=f"{Path(spm_path).name} verification XML export",
    )
    cmd = [
        cfg["speedtree_exe"],
        str(spm_path),
        "-export_options",
        cfg["xml_ini"],
        "-export",
        str(out_path),
    ]
    timeout = float(cfg.get("spm_verify_timeout", 120))
    try:
        returncode, stdout, stderr = run_speedtree_export(
            cmd, Path(spm_path).parent, timeout
        )
    except subprocess.TimeoutExpired as exc:
        raise SpeedTreeExportTimeout("XML", timeout) from exc
    if returncode != 0 or not Path(out_path).exists():
        detail = (stderr or stdout or "").strip()[-500:]
        raise RuntimeError(f"SpeedTree XML verify export failed ({returncode}): {detail}")
    return out_path


def export_verify_fbx_geometry(spm_path, cfg, out_path):
    require_texture_skip_writing(
        cfg["fbx_ini"],
        purpose=f"{Path(spm_path).name} verification FBX export",
    )
    cmd = [
        cfg["speedtree_exe"],
        str(spm_path),
        "-export_options",
        cfg["fbx_ini"],
        "-export",
        str(out_path),
    ]
    timeout = float(cfg.get("spm_verify_timeout", 120))
    try:
        returncode, stdout, stderr = run_speedtree_export(
            cmd, Path(spm_path).parent, timeout
        )
    except subprocess.TimeoutExpired as exc:
        raise SpeedTreeExportTimeout("FBX", timeout) from exc
    path = Path(out_path)
    if returncode != 0 or not path.exists():
        detail = (stderr or stdout or "").strip()[-500:]
        raise RuntimeError(f"SpeedTree FBX verify export failed ({returncode}): {detail}")
    # SpeedTree writes binary FBX. A real mesh contains the FBX property name
    # "Vertices" in clear text; armature-only/container-only exports do not.
    return b"Vertices" in path.read_bytes()


def bone_counts_from_xml(xml_path):
    root = ET.parse(xml_path).getroot()
    counts = {}
    for bone in root.findall(".//Bone"):
        gen = bone.get("Generator") or "?"
        counts[gen] = counts.get(gen, 0) + 1
    return counts


def bone_lengths_from_xml(xml_path):
    """Chord lengths from an Absolute/1 probe, one exported bone per branch."""
    root = ET.parse(xml_path).getroot()
    lengths = []
    for bone in root.findall(".//Bone"):
        try:
            start = tuple(float(bone.get(f"Start{axis}")) for axis in "XYZ")
            end = tuple(float(bone.get(f"End{axis}")) for axis in "XYZ")
        except (TypeError, ValueError):
            continue
        length = math.dist(start, end)
        if math.isfinite(length) and length > 0:
            lengths.append(length)
    return lengths


def cluster_root_bones_from_xml(xml_path):
    """Return the exact structural-root bone inventory from a Raw XML export."""
    root = ET.parse(xml_path).getroot()
    bones = root.findall(".//Bone")
    root_bones = []
    non_root_bones = []
    for bone in bones:
        record = {
            "id": bone.get("ID"),
            "parent_id": bone.get("ParentID", "-1"),
            "generator": bone.get("Generator") or "?",
        }
        if record["parent_id"] in {"", "-1"}:
            root_bones.append(record)
        else:
            non_root_bones.append(record)
    counts = {}
    for bone in root_bones:
        name = bone["generator"]
        counts[name] = counts.get(name, 0) + 1
    return {
        "bone_count": len(bones),
        "root_bone_count": len(root_bones),
        "non_root_bone_count": len(non_root_bones),
        "root_generator_counts": counts,
        "root_bones": root_bones,
        "non_root_bones": non_root_bones,
    }


def calibrate_cluster_root_bones(
    spm_path,
    cfg,
    *,
    original_text,
    log=print,
):
    """Write and verify the Cluster one-root-per-piece SpeedTree contract."""
    plan = plan_cluster_root_bones(original_text)
    if not plan["ready"]:
        raise RuntimeError(
            "Cluster root bone planning failed: "
            + "; ".join(plan["errors"])
        )

    patched_text = apply_cluster_root_bone_plan(original_text, plan)
    write_spm(spm_path, patched_text)
    with tempfile.TemporaryDirectory(prefix="skbatch_cluster_root_") as tmp:
        xml_out = Path(tmp) / f"{Path(spm_path).stem}_cluster_root.xml"
        fbx_out = Path(tmp) / f"{Path(spm_path).stem}_cluster_root.fbx"
        log(
            "  [Cluster bones] SpeedTree structural-root XML verification: "
            f"{plan['expected_root_bone_count']} roots"
        )
        export_verify_xml(spm_path, cfg, xml_out)
        inventory = cluster_root_bones_from_xml(xml_out)
        if inventory["bone_count"] != plan["expected_root_bone_count"]:
            raise RuntimeError(
                "Cluster structural-root bone count mismatch: "
                f"expected {plan['expected_root_bone_count']}, "
                f"exported {inventory['bone_count']}"
            )
        if inventory["non_root_bone_count"]:
            raise RuntimeError(
                "Cluster structural-root export still contains descendant bones: "
                f"{inventory['non_root_bone_count']}"
            )
        if (
            inventory["root_generator_counts"]
            != plan["expected_root_generator_counts"]
        ):
            raise RuntimeError(
                "Cluster structural-root generator mismatch: "
                f"expected {plan['expected_root_generator_counts']}, "
                f"exported {inventory['root_generator_counts']}"
            )
        log("  [Cluster bones] SpeedTree FBX geometry verification")
        if not export_verify_fbx_geometry(spm_path, cfg, fbx_out):
            raise RuntimeError(
                "Cluster structural-root bone compaction produced an FBX "
                "without mesh geometry"
            )

    generator_counts = dict(
        sorted(
            inventory["root_generator_counts"].items(),
            key=lambda item: item[0].casefold(),
        )
    )
    meta = {
        **plan,
        "actual_root_bone_count": inventory["root_bone_count"],
        "actual_root_generator_counts": inventory["root_generator_counts"],
        "verified_xml": True,
        "verified_fbx_geometry": True,
    }
    rounds = [
        {
            "phase": "cluster structural roots absolute/1",
            "total_bones": inventory["bone_count"],
            "root_bones": inventory["root_bone_count"],
            "disabled_generator_count": plan["disabled_generator_count"],
            "hidden_meshless_pivot_generator_count": len(
                plan["meshless_pivot_generator_indices"]
            ),
        }
    ]
    changed = patched_text != original_text
    return (
        generator_counts,
        rounds,
        inventory["bone_count"],
        meta,
        [],
        [],
        changed,
    )


def estimate_relative_value_from_probe(
    lengths,
    target_total,
    value_floor,
    value_cap,
    exported_units_per_speedtree_unit=30.48,
):
    """Estimate the Relative value directly from an Absolute/1 XML probe.

    SpeedTree is unitless, but its library convention is roughly one foot per
    model unit. Our XML preset exports centimeters, hence 30.48. The exact
    internal rounding varies slightly with curved spines; the normal Relative
    export still verifies the result and the existing proportional correction
    handles an outlier.
    """
    usable = [
        float(length)
        for length in lengths
        if math.isfinite(length) and length > 0
    ]
    if not usable or exported_units_per_speedtree_unit <= 0:
        return None

    floor_value = max(0.0, float(value_floor))
    cap_value = max(floor_value, float(value_cap))
    target = max(0.0, float(target_total))

    def predicted_total(relative_value):
        # C++-style nearest-integer estimate; unlike Python round(), .5 is not
        # banker's rounding. A final SpeedTree export remains authoritative.
        return sum(
            max(
                0,
                math.floor(length * relative_value / exported_units_per_speedtree_unit + 0.5),
            )
            for length in usable
        )

    if predicted_total(floor_value) >= target:
        return floor_value
    if predicted_total(cap_value) <= target:
        return cap_value

    continuous = max(
        floor_value,
        min(cap_value, target * exported_units_per_speedtree_unit / sum(usable)),
    )
    low = floor_value
    high = cap_value
    for _ in range(64):
        mid = (low + high) * 0.5
        if predicted_total(mid) < target:
            low = mid
        else:
            high = mid
    return min(
        (continuous, low, high),
        key=lambda value: (
            abs(predicted_total(value) - target),
            abs(value - continuous),
        ),
    )


def shared_authored_relative_value(audit, target_indices):
    """Return one unambiguous authored Relative value for selected targets."""
    generators = list((audit or {}).get("generators") or ())
    values = []
    for index in target_indices:
        if not 0 <= int(index) < len(generators):
            continue
        generator = generators[int(index)]
        try:
            style = float(generator.get("style"))
            bones = float(generator.get("bones"))
        except (TypeError, ValueError):
            continue
        if style == 1.0 and math.isfinite(bones) and bones > 0.0:
            values.append(bones)
    if not values:
        return None
    reference = values[0]
    tolerance = max(1e-6, abs(reference) * 1e-6)
    if any(abs(value - reference) > tolerance for value in values[1:]):
        return None
    return sum(values) / len(values)


def _next_relative_candidate(
    observations,
    *,
    target_total,
    value_floor,
    value_cap,
):
    """Choose a bounded correction from measured SpeedTree output."""
    current_r, current_total = observations[-1]
    under = sorted(
        (
            (relative, total)
            for relative, total in observations
            if total < target_total
        ),
        key=lambda item: item[0],
    )
    over = sorted(
        (
            (relative, total)
            for relative, total in observations
            if total > target_total
        ),
        key=lambda item: item[0],
    )
    lower = max(under, default=None, key=lambda item: item[0])
    upper = min(over, default=None, key=lambda item: item[0])
    if lower is not None and upper is not None and lower[0] < upper[0]:
        if lower[0] > 0.0:
            candidate = math.sqrt(lower[0] * upper[0])
        else:
            candidate = (lower[0] + upper[0]) * 0.5
    elif current_total <= 0:
        candidate = max(current_r * 3.0, current_r + 0.05)
    else:
        ratio = max(1e-9, float(target_total) / float(current_total))
        candidate = current_r * ratio ** 0.6
        if current_total < target_total:
            candidate = max(candidate, current_r * 1.2)
        else:
            candidate = min(candidate, current_r * 0.8)
    return max(value_floor, min(value_cap, candidate))


def _relative_failure_mode(
    observations,
    *,
    lo,
    hi,
    value_floor,
    value_cap,
):
    """Classify why bounded Relative calibration did not converge."""
    relative, total = observations[-1]
    epsilon = max(1e-6, abs(relative) * 1e-6)
    if total == 0:
        return "manual_required_relative_zero"
    if relative >= value_cap - epsilon and total < lo:
        return "manual_required_relative_cap"
    if relative <= value_floor + epsilon and total > hi:
        return "manual_required_relative_floor"
    for left_index, (left_r, left_total) in enumerate(observations):
        for right_r, right_total in observations[left_index + 1:]:
            if right_r > left_r + 1e-6 and right_total < left_total:
                return "manual_required_relative_nonmonotonic"
            if left_r > right_r + 1e-6 and left_total < right_total:
                return "manual_required_relative_nonmonotonic"
    if len(observations) >= 2:
        previous_r, previous_total = observations[-2]
        if (
            total == previous_total
            and abs(relative - previous_r)
            > max(1e-5, abs(previous_r) * 1e-4)
        ):
            return "manual_required_relative_plateau"
    if total < lo:
        return "manual_required_relative_underflow"
    return "manual_required_relative_overflow"


def calibrate_bones(spm_path, cfg, log=print, source_text=None, source_audit=None):
    """Keep Tree density first, then solve the remaining capped bone budget.

    Why total-budget instead of per-branch average:
      A big tree has thousands of tiny twigs. "N bones per branch" forces bones
      onto every twig and explodes (elm_03: 15,234 branches x 3 = 45k+ target ->
      80k+ bones). But SpeedTree's Relative style already gives short splines
      FEWER bones (0 when the value is low enough), so we instead pick a single
      value that hits a total budget = min(branches x per_branch, max_total).
      Small plants stay near per_branch. When a large asset hits the cap, every
      Base-sourced target is disabled first. Only when the remaining Tree target
      still exceeds the cap do we reduce its shared Relative density.
    This also dodges duplicate generator names (elm_03) and honours Size scalar
    automatically, because Relative bones track real spline length.

    Returns (generators_report, rounds, total_bones, meta, warnings, skipped, changed).
    """
    per_branch = float(cfg.get("target_bones_per_branch", 2.0))
    max_total = float(cfg.get("max_total_bones", 1500))
    lo_frac = float(cfg.get("total_window_low", 0.6))
    hi_frac = float(cfg.get("total_window_high", 1.5))
    value_cap = float(cfg.get("value_cap", 64.0))
    value_floor = float(cfg.get("value_floor", 0.02))
    seed = float(cfg.get("seed_relative_value", 0.5))
    max_rounds = max(1, int(cfg.get("max_calibration_rounds", 4)))

    original_text = source_text if source_text is not None else read_spm(spm_path)
    if (
        cfg.get("cluster_root_only_bones", True)
        and is_cluster_normalization_spm(spm_path)
    ):
        return calibrate_cluster_root_bones(
            spm_path,
            cfg,
            original_text=original_text,
            log=log,
        )
    audit = source_audit if source_audit is not None else audit_spm(
        spm_path, text=original_text, analyze_bone_graph=True
    )
    graph = audit.get("bone_graph") or analyze_branch_bone_graph(
        original_text, spm_path=spm_path
    )
    target_indices = list(graph.get("target_indices") or [])
    tree_target_indices = [
        item["index"]
        for item in graph.get("target_generators", [])
        if item.get("source") == "tree"
    ]
    base_target_indices = list(graph.get("base_generator_indices") or [])
    if not base_target_indices:
        base_target_indices = [
            item["index"]
            for item in graph.get("target_generators", [])
            if item.get("source") in {"base_entry", "base_internal"}
        ]
    skipped = []
    for item in graph.get("hidden_targets", []):
        skipped.append({"generator": item["name"], "reason": "hidden root target"})
    for item in graph.get("base_entries", []):
        if not item.get("included"):
            skipped.append(
                {
                    "generator": item["branch_name"],
                    "reason": f"Base excluded: {item['reason']}",
                }
            )
    rounds = []
    warnings = []
    if graph.get("errors"):
        raise RuntimeError("; ".join(graph["errors"]))
    if not target_indices:
        warnings.append("no automatic Branch bone targets; Blender will use a rigid one-bone fallback")
        meta = {key: value for key, value in graph.items() if key != "target_indices"}
        meta["mode"] = "no_branch_generators"
        return {}, rounds, None, meta, warnings, skipped, False
    if graph.get("unknown_base_generators"):
        names = ", ".join(
            item["name"] for item in graph["unknown_base_generators"]
        )
        warnings.append(
            f"unclassified Base references were excluded from automatic bones: {names}"
        )

    activated_zero_bone_generators = graph.get("activated_zero_bone_generators", [])
    has_base_links = bool(graph.get("base_entry_target_generator_count"))
    log(
        "  [bone graph] "
        f"targets={graph['root_target_generator_count']} "
        f"(Tree={graph['tree_target_generator_count']}, "
        f"Base-entry={graph['base_entry_target_generator_count']}, "
        f"Base-internal={graph['base_internal_target_generator_count']}), "
        f"Base-excluded={graph['base_excluded_generator_count']}, "
        f"ambiguous={len(graph['ambiguous_shared_guids'])}"
    )
    cache_key = probe_cache_key(original_text, target_indices, cfg)
    cached_probe = (
        load_probe_cache(spm_path, cache_key)
        if cfg.get("probe_cache_enabled", True)
        else None
    )
    probe_cache_hit = cached_probe is not None
    with tempfile.TemporaryDirectory(prefix="skbatch_cal_") as tmp:
        xml_out = Path(tmp) / f"{Path(spm_path).stem}_cal.xml"

        # -- probe: Absolute/1 == exactly 1 bone per branch -> total branch count.
        # The probe depends on topology, selected generators, SpeedTree, and the
        # XML preset—not on the Relative value produced by a previous run.
        if cached_probe:
            probe_counts = cached_probe["probe_counts"]
            probe_lengths = cached_probe["probe_lengths"]
            log(f"  [probe cache] reused {sum(probe_counts.values())} branch lengths")
        else:
            write_spm(spm_path, apply_branch_values(original_text, target_indices, 0.0, 1.0))
            log("  [SpeedTree] XML 가지 프로브 시작")
            export_verify_xml(spm_path, cfg, xml_out)
            probe_counts = bone_counts_from_xml(xml_out)
            probe_lengths = bone_lengths_from_xml(xml_out)
            if cfg.get("probe_cache_enabled", True) and probe_counts:
                try:
                    save_probe_cache(spm_path, cache_key, probe_counts, probe_lengths)
                except OSError as exc:
                    log(f"  [probe cache warning] could not save cache: {exc}")
        total_branches = sum(probe_counts.values())
        graph_meta = {
            "asset_kind": graph["asset_kind"],
            "base_branch_depth_limit": graph["base_branch_depth_limit"],
            "root_target_generator_count": graph["root_target_generator_count"],
            "tree_target_generator_count": graph["tree_target_generator_count"],
            "base_entry_target_generator_count": graph[
                "base_entry_target_generator_count"
            ],
            "base_internal_target_generator_count": graph[
                "base_internal_target_generator_count"
            ],
            "base_excluded_generator_count": graph["base_excluded_generator_count"],
            "base_generator_count": graph.get(
                "base_generator_count", len(base_target_indices)
            ),
            "ambiguous_shared_guids": graph["ambiguous_shared_guids"],
            "expected_target_branch_nodes": graph["expected_target_branch_nodes"],
            "actual_probe_branch_count": total_branches,
            "activated_zero_bone_generator_count": graph[
                "activated_zero_bone_generator_count"
            ],
            "activated_zero_bone_generators": activated_zero_bone_generators,
            "base_entries": graph["base_entries"],
            "unknown_base_generators": graph["unknown_base_generators"],
            "sync_manifest": graph.get("sync_manifest"),
        }
        probe_round = {
            "phase": "probe(cache)" if probe_cache_hit else "probe(absolute/1)",
            "cache_hit": probe_cache_hit,
            "total_branches": total_branches,
            "total_branch_chord_cm": round(sum(probe_lengths), 4),
            "estimated_relative_value": None,
        }
        rounds.append(probe_round)
        log(f"  [probe] total branches = {total_branches}")
        uncapped_target_total = total_branches * per_branch
        capped = uncapped_target_total > max_total
        initial_target_total = min(uncapped_target_total, max_total)

        if total_branches == 0 or (
            total_branches <= 3 and graph["expected_target_branch_nodes"] > 3
        ):
            reason = (
                f"GUID graph expected {graph['expected_target_branch_nodes']} root/base-entry "
                f"Branch nodes, but the SpeedTree Absolute/1 probe exported only "
                f"{total_branches}; refusing to accept a false low-bone success"
            )
            write_spm(spm_path, original_text)
            raise ManualCalibrationRequired(
                reason,
                rounds=list(rounds),
                total_bones=total_branches,
                calibration={
                    **graph_meta,
                    "mode": "manual_required_probe_underflow",
                    "total_branches": total_branches,
                    "target_total": round(initial_target_total),
                    "capped": capped,
                    "probe_cache_hit": probe_cache_hit,
                    "manual_required": True,
                },
                warnings=[reason],
                skipped=list(skipped),
            )

        calibration_indices = target_indices
        calibration_lengths = probe_lengths
        calibration_branches = total_branches
        disabled_base_indices = []
        base_priority_applied = capped and bool(base_target_indices)
        if base_priority_applied:
            disabled_base_indices = list(base_target_indices)
            calibration_indices = list(tree_target_indices)
            priority_probe_text = apply_branch_values(
                original_text, disabled_base_indices, 0.0, 0.0
            )
            priority_probe_text = apply_branch_values(
                priority_probe_text,
                calibration_indices,
                0.0,
                1.0,
            )
            write_spm(spm_path, priority_probe_text)
            log("  [SpeedTree] XML Base OFF / Tree 전용 프로브 시작")
            export_verify_xml(spm_path, cfg, xml_out)
            priority_probe_counts = bone_counts_from_xml(xml_out)
            calibration_lengths = bone_lengths_from_xml(xml_out)
            calibration_branches = sum(priority_probe_counts.values())
            rounds.append(
                {
                    "phase": "probe(tree-only-after-base-disable)",
                    "total_branches": calibration_branches,
                    "disabled_base_generator_count": len(disabled_base_indices),
                    "total_branch_chord_cm": round(sum(calibration_lengths), 4),
                }
            )
            log(
                "  [bone priority] cap exceeded; "
                f"disabled {len(disabled_base_indices)} Base generators, "
                f"Tree branches={calibration_branches}"
            )
            if calibration_indices and calibration_branches == 0:
                reason = (
                    "Base targets were disabled first, but the Tree-only "
                    "Absolute/1 probe exported no bones"
                )
                write_spm(spm_path, original_text)
                raise ManualCalibrationRequired(
                    reason,
                    rounds=list(rounds),
                    total_bones=0,
                    calibration={
                        **graph_meta,
                        "mode": "manual_required_tree_probe_underflow",
                        "total_branches": total_branches,
                        "calibration_branch_count": 0,
                        "target_total": 0,
                        "capped": True,
                        "base_priority_applied": True,
                        "disabled_base_generator_count": len(disabled_base_indices),
                        "probe_cache_hit": probe_cache_hit,
                        "manual_required": True,
                    },
                    warnings=[reason],
                    skipped=list(skipped),
                )

        target_total = min(calibration_branches * per_branch, max_total)
        density_reduced_for_cap = calibration_branches * per_branch > max_total
        lo = target_total * lo_frac
        hi = min(target_total * hi_frac, max_total) if capped else target_total * hi_frac

        estimated_r = estimate_relative_value_from_probe(
            calibration_lengths,
            target_total,
            value_floor,
            value_cap,
        )
        authored_r = shared_authored_relative_value(
            audit, calibration_indices
        )
        probe_round["estimated_relative_value"] = (
            round(estimated_r, 4) if estimated_r is not None else None
        )
        probe_round["length_estimate_valid"] = estimated_r is not None
        probe_round["authored_relative_value"] = (
            round(authored_r, 4) if authored_r is not None else None
        )
        if estimated_r is not None:
            initial_r = estimated_r
            initial_r_source = "absolute_probe_lengths"
            log(
                f"  [estimate] Relative r={estimated_r:.3f} "
                f"from {len(calibration_lengths)} branch lengths"
            )
        elif authored_r is not None:
            initial_r = authored_r
            initial_r_source = "shared_authored_relative"
            log(
                f"  [estimate] Relative r={authored_r:.3f} "
                "from the current rendered target generators"
            )
        else:
            initial_r = seed
            initial_r_source = "configured_seed"
            log(
                f"  [estimate] Relative r={seed:.3f} from configured seed "
                "(probe lengths unavailable)"
            )
        probe_round["initial_relative_source"] = initial_r_source

        def stop_for_manual(reason, relative_value, measured_total, mode):
            # Put the logical source back immediately. process_spm restores the
            # byte-identical timestamped backup as well when backups are on.
            write_spm(spm_path, original_text)
            raise ManualCalibrationRequired(
                reason,
                rounds=list(rounds),
                total_bones=measured_total,
                calibration={
                    **graph_meta,
                    "mode": mode,
                    "total_branches": total_branches,
                    "calibration_branch_count": calibration_branches,
                    "target_total": round(target_total),
                    "relative_value": round(relative_value, 4),
                    "capped": capped,
                    "base_priority_applied": base_priority_applied,
                    "disabled_base_generator_count": len(disabled_base_indices),
                    "density_reduced_for_cap": density_reduced_for_cap,
                    "probe_cache_hit": probe_cache_hit,
                    "initial_relative_source": initial_r_source,
                    "manual_required": True,
                },
                warnings=[reason],
                skipped=list(skipped),
            )

        # -- solve one Relative value for the remaining targets. Under a cap,
        # Base targets stay Absolute/0 and only the Tree side can be density-scaled.
        # grow super-linearly with the value. The Absolute/1 probe gives a
        # length-based first estimate; proportional correction remains for
        # unusual models whose curved spines differ from their probe chords.
        r = initial_r
        final_counts = {}
        total = 0
        observations = []
        for round_index in range(max_rounds):
            r = max(value_floor, min(value_cap, r))
            write_spm(
                spm_path,
                apply_prioritized_branch_values(
                    original_text,
                    calibration_indices,
                    r,
                    disabled_base_indices,
                ),
            )
            log(f"  [SpeedTree] XML Relative 검증 시작 (round {round_index + 1})")
            final_counts = bone_counts_from_xml(export_verify_xml(spm_path, cfg, xml_out))
            total = sum(final_counts.values())
            observations.append((r, total))
            rounds.append({"phase": f"relative round {round_index + 1}", "value": round(r, 4), "total_bones": total})
            log(f"  [calibrate] r={r:.3f} -> {total} bones (target {target_total:.0f}, window {lo:.0f}-{hi:.0f})")
            if lo <= total <= hi:
                break
            new_r = _next_relative_candidate(
                observations,
                target_total=target_total,
                value_floor=value_floor,
                value_cap=value_cap,
            )
            if abs(new_r - r) < max(1e-6, abs(r) * 1e-6):
                break
            r = new_r

        final_r = max(value_floor, min(value_cap, r))
        if not (lo <= total <= hi):
            failure_mode = _relative_failure_mode(
                observations,
                lo=lo,
                hi=hi,
                value_floor=value_floor,
                value_cap=value_cap,
            )
            stop_for_manual(
                (
                    "bounded Relative calibration did not reach the accepted "
                    f"window {lo:.0f}-{hi:.0f} after {len(observations)} "
                    f"round(s); final r={final_r:.4f}, bones={total}, "
                    f"classification={failure_mode}"
                ),
                final_r,
                total,
                failure_mode,
            )

        if base_priority_applied:
            calibration_mode = "base_disabled_tree_relative"
        else:
            calibration_mode = "base_ref_relative" if has_base_links else "root_only_relative"
        absolute_fallback = None
        material_reference_fallbacks = []
        if not has_base_links:
            fbx_out = Path(tmp) / f"{Path(spm_path).stem}_geometry_check.fbx"
            log("  [SpeedTree] FBX geometry 검증 시작")
            if not export_verify_fbx_geometry(spm_path, cfg, fbx_out):
                # Certain root/frond assets export an armature but silently drop
                # every mesh after Relative bone calibration. Absolute/1 is the
                # known-good SpeedTree representation for those assets.
                fallback_text = apply_branch_values(original_text, target_indices, 0.0, 1.0)
                write_spm(spm_path, fallback_text)
                log("  [SpeedTree] XML Absolute/1 fallback 검증 시작")
                final_counts = bone_counts_from_xml(export_verify_xml(spm_path, cfg, xml_out))
                total = sum(final_counts.values())
                if fbx_out.exists():
                    fbx_out.unlink()
                log("  [SpeedTree] FBX Absolute/1 fallback 검증 시작")
                if not export_verify_fbx_geometry(spm_path, cfg, fbx_out):
                    fallback_text, material_reference_fallbacks = repair_external_frond_material_references(
                        fallback_text
                    )
                    if material_reference_fallbacks:
                        write_spm(spm_path, fallback_text)
                        if fbx_out.exists():
                            fbx_out.unlink()
                        log("  [SpeedTree] FBX material fallback 검증 시작")
                    if not material_reference_fallbacks or not export_verify_fbx_geometry(spm_path, cfg, fbx_out):
                        raise RuntimeError("SpeedTree FBX contains no mesh geometry after bone/material fallbacks")
                absolute_fallback = 1
                calibration_mode = (
                    "root_only_absolute_material_fallback"
                    if material_reference_fallbacks
                    else "root_only_absolute_fallback"
                )
                rounds.append(
                    {"phase": "geometry fallback", "bones_per_branch": 1, "total_bones": total}
                )
                warnings.append(
                    "Relative calibration produced an armature-only FBX; switched root-only generators to Absolute/1"
                )
                if material_reference_fallbacks:
                    warnings.append(
                        "external atlas cutouts produced no Frond geometry; restored embedded material references"
                    )
                log(f"  [geometry fallback] Absolute/1 -> {total} bones with valid FBX geometry")
        else:
            fbx_out = Path(tmp) / f"{Path(spm_path).stem}_geometry_check.fbx"
            log("  [SpeedTree] FBX final geometry verification")
            if not export_verify_fbx_geometry(spm_path, cfg, fbx_out):
                stop_for_manual(
                    (
                        "final Relative calibration produced an FBX without "
                        "render mesh geometry; no Base-linked fallback is safe"
                    ),
                    final_r,
                    total,
                    "manual_required_geometry",
                )

    # generator report: names may repeat; XML aggregates bones by generator name
    generators_report = dict(sorted(final_counts.items(), key=lambda kv: -kv[1]))
    if not (lo <= total <= hi):
        if absolute_fallback is not None:
            warnings.append(
                f"total bones {total} outside target window {lo:.0f}-{hi:.0f}; "
                "kept Absolute/1 because it is the geometry-safe fallback"
            )
        else:
            warnings.append(
                f"total bones {total} outside target window {lo:.0f}-{hi:.0f} "
                f"(relative value at floor/cap {final_r:.3f})"
            )
    meta = {
        **graph_meta,
        "mode": calibration_mode,
        "total_branches": total_branches,
        "calibration_branch_count": calibration_branches,
        "target_total": round(target_total),
        "relative_value": round(final_r, 4),
        "capped": capped,
        "base_priority_applied": base_priority_applied,
        "disabled_base_generator_count": len(disabled_base_indices),
        "density_reduced_for_cap": density_reduced_for_cap,
        "probe_cache_hit": probe_cache_hit,
        "initial_relative_source": initial_r_source,
        "verified_fbx_geometry": True,
    }
    if absolute_fallback is not None:
        meta["absolute_bones_per_branch"] = absolute_fallback
    if material_reference_fallbacks:
        meta["material_reference_fallbacks"] = material_reference_fallbacks
    return generators_report, rounds, total, meta, warnings, skipped, True


def _record_final_spm_identity(report, spm_path):
    path = Path(spm_path)
    stat = path.stat()
    report["final_spm_fingerprint"] = file_content_fingerprint(path)
    report["final_spm_size"] = stat.st_size
    report["final_spm_mtime_ns"] = stat.st_mtime_ns
    return report


def _restore_source_snapshot(
    spm_path,
    *,
    backup,
    source_bytes,
    source_stat,
):
    if backup:
        shutil.copy2(backup, spm_path)
    else:
        Path(spm_path).write_bytes(source_bytes)
        os.utime(
            spm_path,
            ns=(source_stat.st_atime_ns, source_stat.st_mtime_ns),
        )


def process_spm(spm_path, cfg, log=print, dry_run=False):
    """Run one complete SPM transaction under its canonical OS lock."""
    with spm_exclusive_lock(spm_path, log=log):
        return _process_spm_locked(
            spm_path,
            cfg,
            log=log,
            dry_run=dry_run,
        )


def _process_spm_locked(spm_path, cfg, log=print, dry_run=False):
    """Material prefix + bone calibration with backup/restore. Returns report."""
    spm_path = Path(spm_path)
    # A previous run killed mid-rewrite left probe bones in the source. Repair
    # that before reading it as authoritative input.
    recovery = recover_interrupted_calibration(spm_path)
    if recovery.get("recovered"):
        log(
            "  [복구] 중단된 캘리브레이션 감지 — 백업에서 원본 SPM 복원: "
            f"{recovery.get('backup', '')}"
        )
    elif (
        recovery.get("status") != "clean"
        and not recovery.get("cleared")
    ):
        raise RuntimeError(
            "Interrupted SPM calibration marker cannot be recovered safely: "
            + str(recovery.get("error") or recovery.get("status"))
        )
    source_bytes = spm_path.read_bytes()
    source_stat = spm_path.stat()
    source_text = gzip.decompress(source_bytes).decode("utf-8")
    report = {
        "spm": str(spm_path),
        "status": "unchanged",
        "backup": None,
        "material_renames": [],
        "generators": {},
        "rounds": [],
        "total_bones": None,
        "skipped": [],
        "warnings": [],
    }
    if recovery["status"] != "clean":
        report["interrupted_calibration_recovery"] = recovery
        if not recovery.get("recovered") and not recovery.get("cleared"):
            report["warnings"].append(
                "이전 캘리브레이션이 중단된 흔적이 있으나 백업으로 복원하지 못했습니다: "
                + str(recovery.get("error") or recovery.get("status"))
            )

    audit = audit_spm(spm_path, text=source_text, analyze_bone_graph=True)
    readiness = sk_readiness(audit)
    report["sk_readiness"] = readiness
    cluster_root_mode = bool(
        cfg.get("cluster_root_only_bones", True)
        and is_cluster_normalization_spm(spm_path)
    )
    renames, mat_skipped = plan_material_renames(audit)
    report["skipped"].extend(mat_skipped)
    apply_tree_red = bool(
        cfg.get("tree_leaf_parent_red_gradient", True)
        and classify_asset_kind(spm_path) == "tree"
    )

    # A Cluster source is intentionally allowed to arrive with every authored
    # Branch bone disabled.  That is the input state repaired by the dedicated
    # first-renderable-root normalizer below; applying the generic Tree
    # readiness gate here made the normalizer unreachable.
    if not readiness["ready"] and not cluster_root_mode:
        report["status"] = "not-sk-ready"
        report["error"] = readiness["error"]
        report["calibration"] = {
            "mode": readiness["mode"],
            "disabled_generators": readiness["disabled_generators"],
            "bone_graph": readiness.get("bone_graph"),
        }
        report["warnings"].append(
            "Skipped without modifying the SPM because its GUID bone graph is not SK-ready."
        )
        return _record_final_spm_identity(report, spm_path)

    if dry_run:
        report["status"] = "dry-run"
        report["planned_materials"] = renames
        report["current_generators"] = audit["generators"]
        report["bone_graph"] = audit.get("bone_graph")
        if cluster_root_mode:
            report["cluster_root_bone_plan"] = plan_cluster_root_bones(
                source_text
            )
        if apply_tree_red:
            _planned_text, vertex_report = apply_leaf_parent_red_gradient(source_text)
            report["vertex_colors"] = vertex_report
        return _record_final_spm_identity(report, spm_path)

    backup = None
    if cfg.get("backup_spm", True):
        backup = backup_spm(spm_path)
        report["backup"] = backup
    # Written before the first in-place write so a hard kill anywhere below is
    # recoverable; every return path below clears it in the finally block.
    report["calibration_marker"] = str(
        write_calibration_marker(spm_path, backup, _sha256_bytes(source_bytes))
    )

    try:
        generators, rounds, total, meta, warnings, skipped, changed = calibrate_bones(
            spm_path,
            cfg,
            log=log,
            source_text=source_text,
            source_audit=audit,
        )
        report["generators"] = generators
        report["rounds"] = rounds
        report["total_bones"] = total
        report["calibration"] = meta
        report["warnings"].extend(warnings)
        report["skipped"].extend(skipped)

        if cfg.get("rename_materials", True) and renames:
            text = read_spm(spm_path)
            text, applied = apply_material_renames(text, renames)
            if applied:
                write_spm(spm_path, text)
                report["material_renames"] = applied

        vertex_changed = False
        if apply_tree_red:
            text = read_spm(spm_path)
            text, vertex_report = apply_leaf_parent_red_gradient(text)
            report["vertex_colors"] = vertex_report
            if vertex_report["errors"]:
                raise RuntimeError(
                    "SpeedTree leaf-parent VertexColor.R contract failed: "
                    + "; ".join(vertex_report["errors"])
                )
            if vertex_report["changed_generator_count"]:
                write_spm(spm_path, text)
                vertex_changed = True
                changed = True

        final_text = read_spm(spm_path)
        if cluster_root_mode:
            postcondition = cluster_root_logical_postcondition(final_text)
            report["cluster_root_logical_postcondition"] = postcondition
            if not postcondition.get("ok"):
                raise RuntimeError(
                    "Cluster final logical bone postcondition failed: "
                    + "; ".join(
                        postcondition.get("errors")
                        or ["unknown logical mismatch"]
                    )
                )

        # Calibration temporarily writes Absolute/1 and Relative variants. If
        # the final logical XML is identical to the source, restore the exact
        # gzip bytes and timestamps so a no-op ① run does not invalidate a
        # perfectly good .blend merely by touching the SPM.
        if final_text == source_text:
            spm_path.write_bytes(source_bytes)
            os.utime(
                spm_path,
                ns=(source_stat.st_atime_ns, source_stat.st_mtime_ns),
            )
            changed = False
            report["source_restored_unchanged"] = True

        report["status"] = (
            "calibrated"
            if (changed or report["material_renames"] or vertex_changed)
            else "already-ok"
        )
    except ManualCalibrationRequired as exc:
        _restore_source_snapshot(
            spm_path,
            backup=backup,
            source_bytes=source_bytes,
            source_stat=source_stat,
        )
        report["status"] = "manual-required"
        report["error"] = str(exc)
        report["rounds"] = exc.rounds
        report["total_bones"] = exc.total_bones
        report["calibration"] = exc.calibration
        report["warnings"].extend(exc.warnings)
        report["warnings"].append("automatic calibration stopped; original SPM restored")
        report["skipped"].extend(exc.skipped)
    except SpeedTreeExportTimeout as exc:
        _restore_source_snapshot(
            spm_path,
            backup=backup,
            source_bytes=source_bytes,
            source_stat=source_stat,
        )
        report["status"] = "manual-required"
        report["error"] = str(exc)
        report["calibration"] = {
            "mode": "manual_required_export_timeout",
            "stage": exc.stage,
            "timeout_seconds": exc.timeout_seconds,
            "manual_required": True,
        }
        report["warnings"].append(
            "SpeedTree export was too slow; automatic calibration stopped and original SPM restored"
        )
    except Exception:
        _restore_source_snapshot(
            spm_path,
            backup=backup,
            source_bytes=source_bytes,
            source_stat=source_stat,
        )
        if backup:
            report["warnings"].append("calibration failed; SPM restored from backup")
        report["status"] = "failed"
        raise
    finally:
        # Every path above has already restored the source, so the marker must
        # not outlive this call and trigger a bogus recovery on the next run.
        clear_calibration_marker(spm_path)
    return _record_final_spm_identity(report, spm_path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("spm", nargs="+")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--report")
    args = parser.parse_args()
    cfg = load_config()
    reports = []
    for spm in args.spm:
        print(f"== {spm}")
        try:
            rep = process_spm(spm, cfg, dry_run=args.dry_run)
        except Exception as exc:
            print(f"FAILED: {exc}")
            rep = {"spm": spm, "status": "failed", "error": str(exc)}
        reports.append(rep)
        print(json.dumps(rep, indent=2, ensure_ascii=False))
    if args.report:
        Path(args.report).write_text(json.dumps(reports, indent=2, ensure_ascii=False), encoding="utf-8")
    if any(r.get("status") in {"failed", "not-sk-ready"} for r in reports):
        sys.exit(1)


if __name__ == "__main__":
    main()
