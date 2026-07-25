"""Read-only Cluster role and FBX material/mesh handoff contract.

This module is part of the existing PCG ST9 Texture Batch audit.  It does not
create a second tool or an Assembly toggle.  A role becomes an Assembly part
candidate only when the exported FBX itself contains both the role material
and a mesh model to which that material is connected.
"""
from __future__ import annotations

import functools
import hashlib
import copy
import json
import math
import os
import re
import struct
import sys
import tempfile
from pathlib import Path


REPO_DIR = Path(__file__).resolve().parent.parent
if str(REPO_DIR) not in sys.path:
    sys.path.insert(0, str(REPO_DIR))

from cluster_spm_pair_contract import resolve_cluster_spm_pair


SCHEMA_VERSION = 1
PERSISTED_RECEIPT_KIND = "pcg_cluster_assembly_receipt"
DEFAULT_RECEIPT_DIR = Path(__file__).resolve().parent / "reports" / "cluster_assembly"
FBX_BINARY_HEADER = b"Kaydara FBX Binary  \x00\x1a\x00"
ROLE_ORDER = ("branch", "leaf", "leaf_side")
ROLE_PREFIX_RE = re.compile(
    r"^(leaf_side|leaf(?:_[^_]+)*_side|branch|leaf)(?:_|$)",
    re.IGNORECASE,
)
FBX_OBJECT_RE = re.compile(
    r'^\s*(Geometry|Model|Material):\s*(-?\d+)\s*,\s*"([^"]*)"'
    r'(?:\s*,\s*"([^"]*)")?',
    re.MULTILINE,
)
FBX_CONNECTION_RE = re.compile(
    r'^\s*C:\s*"(OO|OP)"\s*,\s*(-?\d+)\s*,\s*(-?\d+)',
    re.MULTILINE,
)


class ClusterAssemblyReceiptError(ValueError):
    """Base error for fail-closed persisted receipt selection."""


class ClusterAssemblyReceiptStaleError(ClusterAssemblyReceiptError):
    """A receipt identity or artifact hash no longer matches disk."""


def normalize_export_name(value):
    """Normalize only exporter wrappers, not authored role identity."""
    name = display_export_name(value)
    name = name.strip().replace("\\", "/").rsplit("/", 1)[-1]
    if name.casefold().endswith("_mat"):
        name = name[:-4]
    if name.casefold().startswith("m_"):
        name = name[2:]
    if name.casefold().startswith("sk_"):
        name = name[3:]
    return name.strip().casefold()


def display_export_name(value):
    """Remove FBX namespace/class wrappers while preserving authored case."""
    name = str(value or "").split("\x00", 1)[0]
    name = name.strip().replace("\\", "/").rsplit("/", 1)[-1]
    if "::" in name:
        name = name.rsplit("::", 1)[-1]
    return name.strip()


def dependency_role(value):
    match = ROLE_PREFIX_RE.match(normalize_export_name(value))
    if not match:
        return None
    token = match.group(1).casefold()
    return "leaf_side" if token.startswith("leaf") and token != "leaf" else token


@functools.lru_cache(maxsize=4096)
def _sha256_cached(path_text, size, mtime_ns):
    digest = hashlib.sha256()
    with Path(path_text).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def file_fingerprint(path, hash_content=True):
    candidate = Path(path)
    try:
        stat = candidate.stat()
    except OSError:
        return {
            "path": str(candidate),
            "exists": False,
            "size": None,
            "mtime_ns": None,
            "sha256": None,
        }
    absolute = os.path.abspath(str(candidate))
    return {
        "path": str(candidate),
        "exists": True,
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "sha256": (
            _sha256_cached(absolute, stat.st_size, stat.st_mtime_ns)
            if hash_content else None
        ),
    }


class _BinaryFbxReader:
    """Small streaming reader for the Objects/Connections FBX contract."""

    def __init__(self, path):
        self.path = Path(path)
        self.handle = self.path.open("rb")
        header = self.handle.read(len(FBX_BINARY_HEADER))
        if header != FBX_BINARY_HEADER:
            self.handle.close()
            raise ValueError("FBX binary header missing")
        raw_version = self.handle.read(4)
        if len(raw_version) != 4:
            self.handle.close()
            raise ValueError("FBX version missing")
        self.version = struct.unpack("<I", raw_version)[0]
        self.wide = self.version >= 7500
        self.null_record_size = 25 if self.wide else 13
        self.objects = {}
        self.connections = []

    def close(self):
        self.handle.close()

    def _read_exact(self, size):
        data = self.handle.read(size)
        if len(data) != size:
            raise ValueError("unexpected end of FBX")
        return data

    def _header(self):
        if self.wide:
            raw = self._read_exact(25)
            end_offset, count, prop_size, name_size = struct.unpack(
                "<QQQB", raw)
        else:
            raw = self._read_exact(13)
            end_offset, count, prop_size, name_size = struct.unpack(
                "<IIIB", raw)
        return end_offset, count, prop_size, name_size

    def _property(self):
        kind = self._read_exact(1)
        scalars = {
            b"Y": (2, "<h"), b"C": (1, "<?"), b"I": (4, "<i"),
            b"F": (4, "<f"), b"D": (8, "<d"), b"L": (8, "<q"),
        }
        if kind in scalars:
            size, fmt = scalars[kind]
            return struct.unpack(fmt, self._read_exact(size))[0]
        if kind in {b"S", b"R"}:
            size = struct.unpack("<I", self._read_exact(4))[0]
            value = self._read_exact(size)
            if kind == b"S":
                return value.decode("utf-8", errors="replace")
            return None
        if kind in {b"f", b"d", b"l", b"i", b"b", b"c"}:
            _length, _encoding, byte_count = struct.unpack(
                "<III", self._read_exact(12))
            self.handle.seek(byte_count, os.SEEK_CUR)
            return None
        raise ValueError(f"unsupported FBX property type: {kind!r}")

    def _node(self, parent=""):
        start = self.handle.tell()
        end_offset, prop_count, _prop_size, name_size = self._header()
        if end_offset == 0:
            return False
        if end_offset <= start:
            raise ValueError("invalid FBX node end offset")
        name = self._read_exact(name_size).decode("utf-8", errors="replace")
        props = [self._property() for _ in range(prop_count)]

        if parent == "Objects" and name in {"Geometry", "Model", "Material"}:
            object_id = props[0] if props and isinstance(props[0], int) else None
            if object_id is not None:
                self.objects[object_id] = {
                    "kind": name,
                    "name": str(props[1] if len(props) > 1 else ""),
                    "subtype": str(props[2] if len(props) > 2 else ""),
                }
        elif parent == "Connections" and name == "C" and len(props) >= 3:
            relation = str(props[0])
            child = props[1]
            parent_id = props[2]
            if relation in {"OO", "OP"} \
                    and isinstance(child, int) and isinstance(parent_id, int):
                self.connections.append((relation, child, parent_id))

        while self.handle.tell() < end_offset:
            remaining = end_offset - self.handle.tell()
            if remaining <= self.null_record_size:
                self.handle.seek(end_offset)
                break
            if not self._node(parent=name):
                break
        if self.handle.tell() < end_offset:
            self.handle.seek(end_offset)
        return True

    def parse(self):
        file_size = self.path.stat().st_size
        while self.handle.tell() + self.null_record_size <= file_size:
            before = self.handle.tell()
            if not self._node():
                break
            if self.handle.tell() <= before:
                raise ValueError("FBX parser made no progress")
        return self.objects, self.connections, self.version


def _ascii_fbx(path):
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    objects = {}
    for match in FBX_OBJECT_RE.finditer(text):
        kind, object_id, name, subtype = match.groups()
        objects[int(object_id)] = {
            "kind": kind,
            "name": name,
            "subtype": subtype or "",
        }
    connections = [
        (match.group(1), int(match.group(2)), int(match.group(3)))
        for match in FBX_CONNECTION_RE.finditer(text)
    ]
    return objects, connections, None


def _material_mesh_report(path, objects, connections, file_format, version):
    children_by_parent = {}
    for relation, child, parent in connections:
        if relation != "OO":
            continue
        children_by_parent.setdefault(parent, []).append(child)

    materials = sorted({
        display_export_name(object_row["name"])
        for object_row in objects.values()
        if object_row["kind"] == "Material"
    })
    mesh_names = sorted({
        display_export_name(object_row["name"])
        for object_row in objects.values()
        if object_row["kind"] in {"Geometry", "Model"}
        and (
            object_row["kind"] == "Geometry"
            or normalize_export_name(object_row.get("subtype")) == "mesh"
        )
    })
    pairs = []
    meshes = []
    for object_id, object_row in objects.items():
        if object_row["kind"] != "Model" \
                or normalize_export_name(object_row.get("subtype")) != "mesh":
            continue
        child_ids = children_by_parent.get(object_id, [])
        geometry_names = [
            display_export_name(objects[child]["name"])
            for child in child_ids
            if child in objects and objects[child]["kind"] == "Geometry"
        ]
        material_names = [
            display_export_name(objects[child]["name"])
            for child in child_ids
            if child in objects and objects[child]["kind"] == "Material"
        ]
        meshes.append({
            "model": display_export_name(object_row["name"]),
            "geometries": geometry_names,
            "materials": material_names,
        })
        for material in material_names:
            pairs.append({
                "material": material,
                "model": display_export_name(object_row["name"]),
                "geometries": geometry_names,
            })
    return {
        "status": "ok",
        "fbx": str(path),
        "format": file_format,
        "version": version,
        "materials": materials,
        "mesh_names": mesh_names,
        "meshes": meshes,
        "material_mesh_pairs": pairs,
    }


@functools.lru_cache(maxsize=256)
def _inspect_fbx_cached(path_text, _size, _mtime_ns):
    path = Path(path_text)
    try:
        with path.open("rb") as handle:
            binary = handle.read(len(FBX_BINARY_HEADER)) == FBX_BINARY_HEADER
        if binary:
            reader = _BinaryFbxReader(path)
            try:
                objects, connections, version = reader.parse()
            finally:
                reader.close()
            return _material_mesh_report(
                path, objects, connections, "binary", version)
        objects, connections, version = _ascii_fbx(path)
        return _material_mesh_report(
            path, objects, connections, "ascii", version)
    except (OSError, ValueError, struct.error) as exc:
        return {
            "status": "inspection_error",
            "fbx": str(path),
            "error": str(exc),
            "materials": [],
            "mesh_names": [],
            "meshes": [],
            "material_mesh_pairs": [],
        }


def inspect_fbx_material_mesh_pairs(path):
    candidate = Path(path)
    try:
        stat = candidate.stat()
    except OSError as exc:
        return {
            "status": "missing",
            "fbx": str(candidate),
            "error": str(exc),
            "materials": [],
            "mesh_names": [],
            "meshes": [],
            "material_mesh_pairs": [],
        }
    return dict(_inspect_fbx_cached(
        os.path.abspath(str(candidate)), stat.st_size, stat.st_mtime_ns))


def classify_fbx_role(fbx_report, role_identity):
    """Apply the independent content-driven complete/absent/partial gate."""
    expected = normalize_export_name(role_identity)
    if fbx_report.get("status") == "missing":
        return {
            "status": "pending_export",
            "decision": "pending_export",
            "role_identity": role_identity,
            "material_matches": [],
            "mesh_name_matches": [],
            "complete_pairs": [],
            "complete_pair_count": 0,
            "complete_pair_evidence_truncated": False,
            "error": fbx_report.get("error"),
        }
    if fbx_report.get("status") != "ok":
        return {
            "status": "inspection_error",
            "decision": "blocked",
            "role_identity": role_identity,
            "material_matches": [],
            "mesh_name_matches": [],
            "complete_pairs": [],
            "complete_pair_count": 0,
            "complete_pair_evidence_truncated": False,
            "error": fbx_report.get("error"),
        }
    material_matches = [
        value for value in fbx_report.get("materials") or []
        if normalize_export_name(value) == expected
    ]
    mesh_name_matches = [
        value for value in fbx_report.get("mesh_names") or []
        if normalize_export_name(value) == expected
    ]
    matched_pairs = [
        row for row in fbx_report.get("material_mesh_pairs") or []
        if normalize_export_name(row.get("material")) == expected
    ]
    pair_counts = {}
    for row in matched_pairs:
        key = (
            row.get("material", ""),
            row.get("model", ""),
            tuple(row.get("geometries") or []),
        )
        pair_counts[key] = pair_counts.get(key, 0) + 1
    pair_items = list(pair_counts.items())
    evidence_items = (
        pair_items[:6] + pair_items[-6:]
        if len(pair_items) > 12 else pair_items
    )
    complete_pairs = [
        {
            "material": key[0],
            "model": key[1],
            "geometries": list(key[2]),
            "connection_count": count,
        }
        for key, count in evidence_items
    ]
    if matched_pairs:
        status = "complete_pair"
        decision = "normalize_part"
    elif material_matches or mesh_name_matches:
        status = (
            "material_without_mesh" if material_matches
            else "mesh_without_material"
        )
        decision = "blocked"
    else:
        status = "absent"
        decision = "pass_through"
    return {
        "status": status,
        "decision": decision,
        "role_identity": role_identity,
        "material_matches": material_matches,
        "mesh_name_matches": mesh_name_matches,
        "complete_pairs": complete_pairs,
        "complete_pair_count": len(matched_pairs),
        "complete_pair_evidence_truncated": len(pair_counts) > 12,
        "error": None,
    }


def _asset_species(folder):
    value = Path(folder).name
    value = re.sub(
        r"^(?:tree|bush|shrub|weed|grass)_", "", value,
        flags=re.IGNORECASE,
    )
    return value.strip("_-").casefold()


def _expected_role_identity(folder, role):
    species = _asset_species(folder) or "asset"
    if str(role).casefold() == "leaf_side":
        return f"leaf_{species}_side_01"
    return f"{role}_{species}_01"


def _resolve_ref(spm, value):
    path = Path(str(value).replace("/", "\\"))
    if not path.is_absolute():
        path = Path(spm).parent / path
    return Path(os.path.abspath(os.path.normpath(str(path))))


def _export_bundle(spm):
    spm = Path(spm)
    base = spm.parent / "fbx" / spm.stem
    paths = {
        "fbx": base.with_suffix(".fbx"),
        "xml": base.with_suffix(".xml"),
        "stmat": base.with_suffix(".stmat"),
    }
    fbx_report = inspect_fbx_material_mesh_pairs(paths["fbx"])
    return {
        "paths": {
            name: file_fingerprint(path, hash_content=False)
            for name, path in paths.items()
        },
        "fbx_contract": fbx_report,
    }


def _cluster_pair_row(cluster):
    """Resolve one Cluster input to the canonical SK production output."""
    pair = resolve_cluster_spm_pair(cluster)
    canonical = Path(pair["canonical_spm"])
    legacy = Path(pair["mirror_spm"])
    source = canonical if canonical.is_file() else legacy
    return {
        "input": Path(cluster),
        "authoring_spm": canonical,
        "output_spm": canonical,
        "legacy_output_spm": legacy,
        "source_spm": source,
        "canonical_exists": canonical.is_file(),
    }


def _usage_for_cluster(usage, pair_row):
    for path in (
        pair_row["input"],
        pair_row["authoring_spm"],
        pair_row["output_spm"],
        pair_row["legacy_output_spm"],
    ):
        value = usage.get(str(path).casefold())
        if value is not None:
            return value
    return None


def _spm_material_mesh_pair(audit, spm, material_names):
    material_keys = {normalize_export_name(name) for name in material_names}
    actual_mesh_ids = {str(value) for value in audit.mesh_asset_ids(spm)}
    rows = [
        row for row in audit.extract_material_image_refs(spm)
        if normalize_export_name(row.get("material_name")) in material_keys
    ]
    records = []
    for row in rows:
        owned = {
            str(value) for value in row.get("cutout_mesh_ids") or []
            if str(value) not in {"", "-1"}
        }
        connected = sorted(owned.intersection(actual_mesh_ids))
        records.append({
            "material_id": row.get("material_id"),
            "material_name": row.get("material_name"),
            "owned_mesh_ids": sorted(owned),
            "existing_mesh_ids": connected,
            "complete": bool(connected),
        })
    return {
        "status": (
            "complete_pair" if any(row["complete"] for row in records)
            else "material_without_mesh" if records else "absent"
        ),
        "records": records,
    }


def _material_rows(audit, spm):
    rows = []
    for row in audit.extract_material_image_refs(spm):
        refs = []
        for value in row.get("refs") or []:
            resolved = _resolve_ref(spm, value)
            refs.append({
                "authored": value,
                **file_fingerprint(resolved, hash_content=False),
            })
        rows.append({
            "material_id": row.get("material_id"),
            "material_name": row.get("material_name"),
            "cutout_mesh_ids": list(row.get("cutout_mesh_ids") or []),
            "textures": refs,
        })
    return rows


def _canonical_bark_contract(audit, folder, target_spms, dependencies):
    expected = f"bark_{_asset_species(folder)}_01"
    species = _asset_species(folder)
    canonical = []
    for spm in target_spms:
        for row in audit.extract_material_image_refs(spm):
            if normalize_export_name(row.get("material_name")) == expected:
                canonical.append({"spm": str(spm), **row})
    canonical_basenames = {
        Path(value).name.casefold()
        for row in canonical for value in row.get("refs") or []
    }
    sources = []
    for dependency in dependencies:
        spm = dependency.get("source_spm") or dependency["spm"]
        for row in audit.extract_material_image_refs(spm):
            if "bark" not in normalize_export_name(row.get("material_name")):
                continue
            source_basenames = {
                Path(value).name.casefold() for value in row.get("refs") or []
            }
            matches = bool(
                canonical_basenames
                and source_basenames
                and canonical_basenames.intersection(source_basenames)
            )
            # Texture provenance, rather than a convenient material label, is
            # the fallback evidence.  This catches Elm-named materials that
            # still point into a Nothofagus source set.
            if not matches and species and row.get("refs"):
                matches = all(
                    species in str(value).casefold()
                    for value in row.get("refs") or []
                )
            sources.append({
                "cluster_spm": str(spm),
                "material_id": row.get("material_id"),
                "material_name": row.get("material_name"),
                "texture_refs": list(row.get("refs") or []),
                "matches_canonical_textures": matches,
                "replacement": "not_required" if matches else "required",
            })
    if not canonical:
        status = "blocked_canonical_missing"
    elif any(row["replacement"] == "required" for row in sources):
        status = "replacement_required"
    else:
        status = "canonical"
    return {
        "status": status,
        "canonical_material": f"M_{expected}",
        "canonical_sources": canonical,
        "cluster_bark_sources": sources,
        "mutation_applied": False,
    }


def _tga_basename_validation(
        output_spm, dependency_usage, legacy_output_spm=None):
    expected = Path(output_spm).stem.casefold()
    accepted = {expected}
    if legacy_output_spm:
        accepted.add(Path(legacy_output_spm).stem.casefold())
    connected_refs = dependency_usage.get("connected_refs")
    if connected_refs is None:
        connected_refs = dependency_usage.get("source_refs") or []
    refs = [Path(value) for value in connected_refs]
    missing = [str(path) for path in refs if not path.is_file()]
    invalid = [
        str(path) for path in refs
        if path.suffix.casefold() != ".tga"
        or not any(
            path.stem.casefold() == base
            or path.stem.casefold().startswith(base + "_")
            for base in accepted
        )
    ]
    if missing:
        status = "missing"
    elif invalid:
        status = "basename_mismatch"
    elif refs:
        status = "ok"
    else:
        status = "not_found"
    return {
        "status": status,
        "expected_base": Path(output_spm).stem,
        "accepted_legacy_base": (
            Path(legacy_output_spm).stem if legacy_output_spm else None
        ),
        "refs": [str(path) for path in refs],
        "missing": missing,
        "invalid": invalid,
    }


def _atlas_manifest_candidates(folder, target_spms):
    """Return stable per-scope/per-target receipts before the rolling global file."""
    paths = []
    for target in target_spms or []:
        target = Path(target)
        scope_dir = target.parent / ".atlas_leaf_speedtree_scopes"
        if scope_dir.is_dir():
            paths.extend(sorted(scope_dir.glob(f"*__{target.stem}.json")))
        target_receipt = (
            target.parent
            / ".atlas_leaf_speedtree_targets"
            / f"{target.stem}.json"
        )
        if target_receipt.is_file():
            paths.append(target_receipt)
    # A stable receipt set is authoritative.  The legacy global file is a
    # rolling last-export snapshot, so it may only be consulted when no
    # target/scope receipt exists at all.
    if not paths:
        global_manifest = Path(folder) / "speedtree_import_manifest.json"
        if global_manifest.is_file():
            paths.append(global_manifest)
    unique = []
    seen = set()
    for path in paths:
        key = os.path.normcase(os.path.abspath(str(path)))
        if key not in seen:
            seen.add(key)
            unique.append(path)
    return unique


def _normalized_bounds_contract(value, label, required=False):
    if value is None and not required:
        return None
    if not isinstance(value, dict):
        raise ClusterAssemblyReceiptError(
            f"{label} normalized bounds are missing"
        )
    size = value.get("size")
    if not isinstance(size, (list, tuple)) or len(size) != 3:
        raise ClusterAssemblyReceiptError(
            f"{label} normalized bounds size is invalid"
        )
    try:
        checked_size = [float(component) for component in size]
    except (TypeError, ValueError) as exc:
        raise ClusterAssemblyReceiptError(
            f"{label} normalized bounds size is not numeric"
        ) from exc
    if any(
        not math.isfinite(component) or component < 0.0
        for component in checked_size
    ) or max(checked_size) <= 0.0:
        raise ClusterAssemblyReceiptError(
            f"{label} normalized bounds size is not finite and non-negative"
        )
    result = copy.deepcopy(value)
    result["size"] = checked_size
    return result


def _physical_normalization_receipt(payload):
    receipt = (payload or {}).get("normalized_prototype_receipt")
    if receipt is None:
        return None
    if (
        not isinstance(receipt, dict)
        or receipt.get("workflow_mode") != "PHYSICAL_DIRECT_CAPTURE"
    ):
        raise ClusterAssemblyReceiptError(
            "Atlas normalized prototype receipt has an invalid workflow"
        )
    capture = receipt.get("physical_capture_contract")
    capture_hash = str(
        receipt.get("physical_capture_contract_sha256") or ""
    )
    if (
        not isinstance(capture, dict)
        or not capture_hash
        or str(capture.get("contract_sha256") or "") != capture_hash
    ):
        raise ClusterAssemblyReceiptError(
            "Atlas physical normalization receipt has no matching capture contract"
        )
    prototypes = {}
    for row in receipt.get("prototypes") or []:
        asset_name = str(
            row.get("skeletal_asset")
            or row.get("prototype_asset")
            or ""
        ).strip()
        key = asset_name.casefold()
        if not asset_name or key in prototypes:
            raise ClusterAssemblyReceiptError(
                "Atlas physical prototype assets are missing or duplicated"
            )
        prototypes[key] = _normalized_bounds_contract(
            row.get("normalized_bounds"),
            f"Atlas physical prototype {asset_name}",
            required=True,
        )
    if not prototypes:
        raise ClusterAssemblyReceiptError(
            "Atlas physical normalization receipt has no prototypes"
        )
    unit_probe = (payload or {}).get("unit_probe_contract")
    if (
        not isinstance(unit_probe, dict)
        or unit_probe.get("kind") != "speedtree_fbx_spm_unit_probe"
        or str(unit_probe.get("status") or "").casefold() != "verified"
    ):
        raise ClusterAssemblyReceiptError(
            "Atlas physical normalization receipt has no verified common unit probe"
        )
    return {
        "receipt": copy.deepcopy(receipt),
        "prototype_bounds": prototypes,
        "capture_hash": capture_hash,
        "unit_probe_contract": copy.deepcopy(unit_probe),
    }


def _normalized_composite_parts(row, source_partition_mode):
    raw_parts = row.get("composite_parts") or []
    is_composite = source_partition_mode == "COMPOSITE_PER_DEFORM_ROOT"
    if not is_composite:
        if raw_parts:
            raise ClusterAssemblyReceiptError(
                "Non-composite normalized variant contains composite parts"
            )
        return []
    if not isinstance(raw_parts, list) or not raw_parts:
        raise ClusterAssemblyReceiptError(
            "COMPOSITE_PER_DEFORM_ROOT variant has no composite parts"
        )
    checked = []
    for expected_index, raw in enumerate(raw_parts, 1):
        if not isinstance(raw, dict):
            raise ClusterAssemblyReceiptError(
                "Normalized composite part is not an object"
            )
        index = int(raw.get("subpart_index") or 0)
        asset_name = str(raw.get("skeletal_asset_name") or "").strip()
        matrix = raw.get("subpart_to_card_matrix")
        if index != expected_index or not asset_name.casefold().startswith("sk_"):
            raise ClusterAssemblyReceiptError(
                "Normalized composite part indices/names must be consecutive SK assets"
            )
        if (
            not isinstance(matrix, list)
            or len(matrix) != 4
            or any(not isinstance(matrix_row, list) or len(matrix_row) != 4 for matrix_row in matrix)
        ):
            raise ClusterAssemblyReceiptError(
                f"Normalized composite part matrix is not 4x4: {asset_name}"
            )
        normalized_matrix = []
        for matrix_row in matrix:
            values = [float(value) for value in matrix_row]
            if any(not math.isfinite(value) for value in values):
                raise ClusterAssemblyReceiptError(
                    f"Normalized composite part matrix is not finite: {asset_name}"
                )
            normalized_matrix.append(values)
        checked.append({
            "subpart_index": index,
            "skeletal_asset_name": asset_name,
            "source_bone": str(raw.get("source_bone") or ""),
            "endpoint_bone": str(raw.get("endpoint_bone") or ""),
            "subpart_to_card_matrix": normalized_matrix,
            "pivot_contract": str(
                raw.get("pivot_contract")
                or "normalized_attachment_origin_0_0_0"
            ),
            **(
                {
                    "normalized_bounds": _normalized_bounds_contract(
                        raw.get("normalized_bounds"),
                        f"normalized composite part {asset_name}",
                        required=True,
                    )
                }
                if raw.get("normalized_bounds") is not None
                else {}
            ),
        })
    if len({part["skeletal_asset_name"] for part in checked}) != len(checked):
        raise ClusterAssemblyReceiptError(
            "Normalized composite part asset names are duplicated"
        )
    return checked


def _normalized_variant_contract(manifest_path, payload, group):
    physical = _physical_normalization_receipt(payload)
    mesh_ids = [int(value) for value in group.get("mesh_ids") or []]
    mesh_rows = list(group.get("meshes") or [])
    if len(mesh_ids) != len(mesh_rows) or not mesh_rows:
        raise ClusterAssemblyReceiptError(
            "Atlas normalized variant mesh IDs do not match exported plan rows"
        )
    variants = []
    for mesh_id, row in zip(mesh_ids, mesh_rows):
        plan_name = str(row.get("source_object") or "").strip()
        ordinal = int(row.get("source_ordinal") or 0)
        raw_prototype_index = row.get("source_prototype_index")
        source_prototype_index = (
            int(raw_prototype_index)
            if raw_prototype_index not in (None, "")
            else None
        )
        source_partition_mode = str(
            row.get("source_partition_mode") or ""
        ).strip() or None
        composite_parts = _normalized_composite_parts(
            row,
            source_partition_mode,
        )
        skeletal_asset_name = str(
            row.get("skeletal_asset_name")
            or group.get("skeletal_asset_name")
            or ("SK_" + plan_name if plan_name else "")
        ).strip()
        if (
            not plan_name
            or ordinal <= 0
            or not skeletal_asset_name.casefold().startswith("sk_")
            or (
                source_prototype_index is not None
                and source_prototype_index <= 0
            )
        ):
            raise ClusterAssemblyReceiptError(
                "Atlas normalized variant has no explicit "
                "plan/ordinal/SK/prototype asset contract"
            )
        normalized_bounds = _normalized_bounds_contract(
            row.get("normalized_bounds"),
            f"Atlas normalized variant {plan_name}",
            required=physical is not None,
        )
        if physical is not None:
            row_hash = str(
                row.get("physical_capture_contract_sha256") or ""
            )
            if (
                row.get("normalization_workflow_mode")
                != "PHYSICAL_DIRECT_CAPTURE"
                or row_hash != physical["capture_hash"]
            ):
                raise ClusterAssemblyReceiptError(
                    "Atlas physical variant row is detached from its capture receipt: "
                    + plan_name
                )
            expected = physical["prototype_bounds"].get(
                skeletal_asset_name.casefold()
            )
            if expected is None or json.dumps(
                normalized_bounds,
                sort_keys=True,
                separators=(",", ":"),
            ) != json.dumps(expected, sort_keys=True, separators=(",", ":")):
                raise ClusterAssemblyReceiptError(
                    "Atlas physical variant bounds disagree with its prototype receipt: "
                    + skeletal_asset_name
                )
            for composite_part in composite_parts:
                part_name = composite_part["skeletal_asset_name"]
                expected_part = physical["prototype_bounds"].get(
                    part_name.casefold()
                )
                if expected_part is None or json.dumps(
                    composite_part.get("normalized_bounds"),
                    sort_keys=True,
                    separators=(",", ":"),
                ) != json.dumps(
                    expected_part,
                    sort_keys=True,
                    separators=(",", ":"),
                ):
                    raise ClusterAssemblyReceiptError(
                        "Atlas physical composite bounds disagree with its "
                        "prototype receipt: "
                        + part_name
                    )
        plan_fbx_path = row.get("assembly_plan_fbx") or row.get("fbx")
        plan_fbx = file_fingerprint(plan_fbx_path)
        if not plan_fbx.get("exists") or not plan_fbx.get("sha256"):
            raise ClusterAssemblyReceiptError(
                f"Atlas normalized plan FBX is missing: {plan_fbx_path}"
            )
        variant = {
            "ordinal": ordinal,
            "plan_name": plan_name,
            "skeletal_asset_name": skeletal_asset_name,
            "source_prototype_index": source_prototype_index,
            "source_partition_mode": source_partition_mode,
            "composite_parts": composite_parts,
            "target_mesh_id": mesh_id,
            "plan_fbx": plan_fbx,
            "unreal_relative_folder": str(
                row.get("unreal_relative_folder")
                or group.get("unreal_relative_folder")
                or "Cluster"
            ),
            "pivot_contract": str(
                row.get("pivot_contract")
                or group.get("pivot_contract")
                or "normalized_attachment_origin_0_0_0"
            ),
        }
        if normalized_bounds is not None:
            variant["normalized_bounds"] = normalized_bounds
        if physical is not None:
            variant["normalization_workflow_mode"] = (
                "PHYSICAL_DIRECT_CAPTURE"
            )
            variant["physical_capture_contract_sha256"] = physical[
                "capture_hash"
            ]
        variants.append(variant)
    variants.sort(key=lambda row: row["ordinal"])
    actual_ordinals = [row["ordinal"] for row in variants]
    if actual_ordinals != list(range(1, len(variants) + 1)):
        raise ClusterAssemblyReceiptError(
            "Atlas normalized variant ordinals must be consecutive 1..N: "
            + str(actual_ordinals)
        )
    if physical is not None:
        receipt_variants = {
            (
                str(row.get("plan") or ""),
                str(
                    row.get("skeletal_asset")
                    or row.get("prototype_asset")
                    or ""
                ),
            )
            for row in physical["receipt"].get("variants") or []
        }
        exported_variants = {
            (row["plan_name"], row["skeletal_asset_name"])
            for row in variants
        }
        if receipt_variants != exported_variants:
            raise ClusterAssemblyReceiptError(
                "Atlas physical receipt variants do not match exported plan/SK pairs"
            )
    source_blend = file_fingerprint(payload.get("blend_file"))
    if not source_blend.get("exists") or not source_blend.get("sha256"):
        raise ClusterAssemblyReceiptError(
            "Atlas normalized variant Blender source is missing"
        )
    result = {
        "status": "ready",
        "contract": (
            "atlas_normalized_plan_composite_skeletal_pair_v2"
            if any(row.get("composite_parts") for row in variants)
            else "atlas_normalized_plan_skeletal_pair_v1"
        ),
        "manifest": file_fingerprint(manifest_path),
        "source_blend": source_blend,
        "material": str(group.get("material") or ""),
        "material_id": int(group.get("material_id") or 0),
        "variants": variants,
        "generator_bindings": list(
            (payload.get("generator_connection") or {}).get("bindings") or []
        ),
    }
    if physical is not None:
        result["production_normalization"] = physical["receipt"]
        result["unit_probe_contract"] = physical["unit_probe_contract"]
    return result


def _atlas_normalized_variants(
    folder,
    role_identity,
    target_spms,
    audit=None,
):
    """Read one current role contract from stable Atlas target/scope receipts."""
    allowed_spms = {
        Path(path).resolve(strict=False): Path(path) for path in target_spms or []
    }
    if not allowed_spms:
        return None
    candidates = []
    stale = []
    for manifest_path in _atlas_manifest_candidates(folder, target_spms):
        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ClusterAssemblyReceiptError(
                f"Atlas normalized variant manifest is unreadable: {manifest_path}: {exc}"
            ) from exc
        manifest_spm = Path(
            str(payload.get("spm") or "")
        ).resolve(strict=False)
        if manifest_spm not in allowed_spms:
            continue
        groups = [
            row for row in payload.get("material_groups") or []
            if normalize_export_name(row.get("material"))
            == normalize_export_name(role_identity)
        ]
        if not groups:
            continue
        if len(groups) != 1:
            raise ClusterAssemblyReceiptError(
                "Atlas normalized variant material identity is ambiguous: "
                + str(role_identity)
            )
        group = groups[0]
        mesh_ids = [int(value) for value in group.get("mesh_ids") or []]
        current_matches = []
        if audit is not None:
            current_matches = [
                row
                for row in audit.extract_material_image_refs(
                    allowed_spms[manifest_spm]
                )
                if normalize_export_name(row.get("material_name"))
                == normalize_export_name(role_identity)
                and int(row.get("material_id") or 0)
                == int(group.get("material_id") or 0)
                and [int(value) for value in row.get("cutout_mesh_ids") or []]
                == mesh_ids
            ]
            if len(current_matches) != 1:
                stale.append(str(manifest_path))
                continue
        contract = _normalized_variant_contract(
            manifest_path,
            payload,
            group,
        )
        # One Cluster blend legitimately delivers the same plan set to several
        # tree SPMs, and each target assigns its own local Material/Mesh IDs.
        # Those per-target values must stay out of the identity: including them
        # made every additional ON relationship look like a conflicting
        # receipt, so a fully consistent folder failed while a half-applied one
        # passed.  Identity is what the Cluster produced, not where it landed.
        identity = json.dumps(
            {
                "material": normalize_export_name(contract["material"]),
                "source_blend": contract["source_blend"].get("sha256"),
                "variants": [
                    {
                        "ordinal": row["ordinal"],
                        "plan_name": row["plan_name"],
                        "skeletal_asset_name": row["skeletal_asset_name"],
                        "source_prototype_index": row[
                            "source_prototype_index"
                        ],
                        "source_partition_mode": row[
                            "source_partition_mode"
                        ],
                        "plan_fbx": row["plan_fbx"].get("sha256"),
                    }
                    for row in contract["variants"]
                ],
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        candidates.append((str(manifest_path), identity, contract))
    distinct = {}
    for _manifest, identity, contract in sorted(candidates):
        distinct.setdefault(identity, contract)
    if len(distinct) > 1:
        raise ClusterAssemblyReceiptError(
            "Atlas normalized role has multiple current receipts: "
            + str(role_identity)
        )
    if distinct:
        return next(iter(distinct.values()))
    if stale:
        raise ClusterAssemblyReceiptStaleError(
            "Atlas normalized role receipts are stale: " + "; ".join(stale)
        )
    return None


def build_cluster_assembly_contract(
        folder, target_spms, clusters, cluster_usage=None,
        assembly_source_spms=None):
    """Build hierarchy and downstream handoff from actual SPM dependencies."""
    try:
        from . import pcg_texture_audit as audit
    except ImportError:
        import pcg_texture_audit as audit

    folder = Path(folder)
    full_target_spms = [Path(path) for path in target_spms or []]
    if assembly_source_spms is None:
        assembly_source_spms = []
        for spm in full_target_spms:
            source = spm
            if spm.name.casefold().startswith("sk_"):
                candidate = spm.with_name(spm.name[3:])
                if candidate.is_file():
                    source = candidate
            if source not in assembly_source_spms:
                assembly_source_spms.append(source)
    else:
        assembly_source_spms = list(dict.fromkeys(
            Path(path) for path in assembly_source_spms or []))
    cluster_pairs = [_cluster_pair_row(path) for path in clusters or []]
    clusters = [row["output_spm"] for row in cluster_pairs]
    usage = cluster_usage
    if usage is None:
        usage = audit.cluster_material_usage(
            assembly_source_spms, clusters)

    relevant_clusters = [
        row for row in cluster_pairs
        if _usage_for_cluster(usage, row) is not None
        and dependency_role(row["output_spm"].stem) in ROLE_ORDER
    ]
    export_bundles = {
        str(spm).casefold(): _export_bundle(spm)
        for spm in assembly_source_spms
    } if relevant_clusters else {}
    actual_dependencies = []
    for pair_row in relevant_clusters:
        cluster = pair_row["source_spm"]
        authoring_spm = pair_row["authoring_spm"]
        output_spm = pair_row["output_spm"]
        role = dependency_role(output_spm.stem)
        dependency_usage = _usage_for_cluster(usage, pair_row)
        expected_identity = _expected_role_identity(folder, role)
        normalized_variants = _atlas_normalized_variants(
            folder,
            expected_identity,
            full_target_spms,
            audit=audit,
        )
        primary_role_source = (
            normalize_export_name(output_spm.stem)
            == normalize_export_name(expected_identity)
        )
        usage_roles = {
            dependency_role(name)
            for name in dependency_usage.get("material_names") or []
            if dependency_role(name)
        }
        role_conflict = bool(usage_roles and usage_roles != {role})
        material_names_by_spm = dependency_usage.get(
            "material_names_by_spm") or {}
        targets = []
        for spm in assembly_source_spms:
            material_names = list(
                material_names_by_spm.get(str(spm), [])
                or material_names_by_spm.get(str(spm).casefold(), [])
            )
            if not material_names:
                material_names = list(dependency_usage.get("material_names") or [])
            role_identity = expected_identity
            if primary_role_source:
                fbx_gate = classify_fbx_role(
                    export_bundles[str(spm).casefold()]["fbx_contract"],
                    role_identity,
                )
            else:
                fbx_gate = {
                    "status": "reference_only",
                    "decision": "reference_only",
                    "role_identity": role_identity,
                    "material_matches": [],
                    "mesh_name_matches": [],
                    "complete_pairs": [],
                    "complete_pair_count": 0,
                    "complete_pair_evidence_truncated": False,
                    "error": None,
                }
            targets.append({
                "spm": str(spm),
                "material_names": material_names,
                "spm_material_mesh_pair": _spm_material_mesh_pair(
                    audit, spm, material_names or [role_identity]),
                "export_bundle": export_bundles[str(spm).casefold()]["paths"],
                "fbx_material_mesh_pair": fbx_gate,
            })
        decisions = {
            target["fbx_material_mesh_pair"]["decision"] for target in targets
        }
        if role_conflict:
            decision = "blocked"
        elif not primary_role_source:
            decision = "reference_only"
        elif "blocked" in decisions:
            decision = "blocked"
        elif "pending_export" in decisions:
            decision = "pending_export"
        elif decisions == {"normalize_part"}:
            decision = "normalize_part"
        elif decisions == {"pass_through"}:
            decision = "pass_through"
        else:
            decision = "blocked"
        normalized_variants_required = decision == "normalize_part"
        normalized_variants_missing = bool(
            normalized_variants_required and not normalized_variants
        )
        if normalized_variants_missing:
            decision = "blocked"
        actual_dependencies.append({
            "role": role,
            "name": output_spm.stem,
            "spm": str(authoring_spm),
            "authoring_spm": str(authoring_spm),
            "output_spm": str(output_spm),
            "source_spm": str(cluster),
            "pair_status": (
                "canonical_pair" if pair_row["canonical_exists"]
                else "legacy_raw_only"
            ),
            "primary_role_source": primary_role_source,
            "role_conflict": role_conflict,
            "usage_roles": sorted(usage_roles),
            "spm_fingerprint": file_fingerprint(cluster),
            "authoring_spm_fingerprint": file_fingerprint(authoring_spm),
            "output_spm_fingerprint": file_fingerprint(output_spm),
            "source_materials": _material_rows(audit, cluster),
            "source_mesh_ids": sorted(audit.mesh_asset_ids(cluster)),
            "texture_dependencies": [
                file_fingerprint(value, hash_content=False)
                for value in (
                    dependency_usage.get("connected_refs")
                    if dependency_usage.get("connected_refs") is not None
                    else dependency_usage.get("source_refs") or []
                )
            ],
            "tga_basename_validation": _tga_basename_validation(
                output_spm,
                dependency_usage,
                legacy_output_spm=pair_row["legacy_output_spm"],
            ),
            "referenced_by_spms": list(dependency_usage.get("spms") or []),
            "target_material_names": list(
                dependency_usage.get("material_names") or []),
            "targets": targets,
            "decision": decision,
            "normalized_variants": normalized_variants,
            "normalized_variants_required": normalized_variants_required,
            "normalized_variants_missing": normalized_variants_missing,
        })

    children = []
    for role in ROLE_ORDER:
        role_dependencies = [
            row for row in actual_dependencies if row["role"] == role
        ]
        if not role_dependencies:
            continue
        expected_identity = _expected_role_identity(folder, role)
        primary = next(
            (
                row for row in role_dependencies
                if normalize_export_name(row["name"])
                == normalize_export_name(expected_identity)
            ),
            role_dependencies[0],
        )
        children.append({
            "role": role,
            "name": primary["name"],
            "source_spm": str(primary["spm"]),
            "decision": primary["decision"],
            "references": [
                {
                    "name": row["name"],
                    "source_spm": str(row["spm"]),
                    "decision": row["decision"],
                }
                for row in role_dependencies if row is not primary
            ],
        })

    source_identities = []
    for spm in full_target_spms:
        authoritative = spm
        if spm.name.casefold().startswith("sk_"):
            source = spm.with_name(spm.name[3:])
            if source.is_file():
                authoritative = source
        source_identities.append({
            "target_spm": file_fingerprint(spm),
            "authoritative_tree_source": file_fingerprint(authoritative),
        })

    bark = _canonical_bark_contract(
        audit,
        folder,
        full_target_spms or assembly_source_spms,
        actual_dependencies,
    )
    decisions = {
        row["decision"] for row in actual_dependencies
        if row["decision"] != "reference_only"
    }
    canonical_pair_missing = any(
        row.get("pair_status") != "canonical_pair"
        for row in actual_dependencies
    )
    if (
        canonical_pair_missing
        or "blocked" in decisions
        or bark["status"].startswith("blocked")
    ):
        handoff_status = "blocked"
    elif "pending_export" in decisions:
        handoff_status = "pending_export"
    elif bark["status"] == "replacement_required":
        handoff_status = "needs_bark_normalization"
    elif "normalize_part" in decisions:
        handoff_status = "ready"
    else:
        handoff_status = "pass_through"
    issues = []
    for dependency in actual_dependencies:
        if dependency.get("pair_status") != "canonical_pair":
            issues.append({
                "code": "CLUSTER_CANONICAL_SPM_MISSING",
                "role": dependency["role"],
                "spm": dependency["authoring_spm"],
                "output_spm": dependency["output_spm"],
            })
        if dependency.get("role_conflict"):
            issues.append({
                "code": "CLUSTER_ROLE_CONFLICT",
                "role": dependency["role"],
                "spm": str(dependency["spm"]),
                "usage_roles": dependency.get("usage_roles") or [],
            })
        if dependency.get("normalized_variants_missing"):
            issues.append({
                "code": "NORMALIZED_VARIANTS_REQUIRED",
                "role": dependency["role"],
                "spm": str(dependency["spm"]),
                "reason": "actionable_role_has_no_current_atlas_normalized_variants",
            })
        tga_validation = dependency.get("tga_basename_validation") or {}
        if tga_validation.get("status") not in {"ok"}:
            issues.append({
                "code": "CLUSTER_TGA_BASENAME_INVALID",
                "role": dependency["role"],
                "spm": str(dependency["spm"]),
                "details": tga_validation,
            })
        for target in dependency["targets"]:
            gate = target["fbx_material_mesh_pair"]
            if gate["decision"] == "blocked":
                issues.append({
                    "code": "FBX_ROLE_MATERIAL_MESH_PARTIAL",
                    "role": dependency["role"],
                    "spm": target["spm"],
                    "status": gate["status"],
                    "error": gate.get("error"),
                })
    if bark["status"] == "blocked_canonical_missing":
        issues.append({
            "code": "CANONICAL_BARK_MISSING",
            "material": bark["canonical_material"],
        })

    role_receipts = []
    for row in actual_dependencies:
        if not row.get("primary_role_source"):
            continue
        child = next(
            (value for value in children if value["role"] == row["role"]),
            {},
        )
        role_receipts.append({
            "role": row["role"],
            "name": row["name"],
            "decision": row["decision"],
            "spm": str(row["spm"]),
            "authoring_spm": row["authoring_spm"],
            "output_spm": row["output_spm"],
            "source_materials": row["source_materials"],
            "source_mesh_ids": row["source_mesh_ids"],
            "canonical_part_reference_evidence": {
                "primary_spm": str(row["spm"]),
                "mesh_ids": row["source_mesh_ids"],
                "reference_dependencies": list(child.get("references") or []),
            },
            "normalized_variants": row.get("normalized_variants"),
            "targets": row["targets"],
        })
    dependency_receipts = [
        {
            "role": row["role"],
            "name": row["name"],
            "spm": str(row["spm"]),
            "authoring_spm": row["authoring_spm"],
            "output_spm": row["output_spm"],
            "pair_status": row["pair_status"],
            "primary_role_source": row["primary_role_source"],
            "role_conflict": row["role_conflict"],
            "spm_fingerprint": row["spm_fingerprint"],
            "authoring_spm_fingerprint": row["authoring_spm_fingerprint"],
            "output_spm_fingerprint": row["output_spm_fingerprint"],
            "source_materials": row["source_materials"],
            "source_mesh_ids": row["source_mesh_ids"],
            "texture_dependencies": row["texture_dependencies"],
            "tga_basename_validation": row["tga_basename_validation"],
            "referenced_by_spms": row["referenced_by_spms"],
            "normalized_variants": row.get("normalized_variants"),
            "normalized_variants_required": row.get(
                "normalized_variants_required", False
            ),
            "normalized_variants_missing": row.get(
                "normalized_variants_missing", False
            ),
        }
        for row in actual_dependencies
    ]
    handoff = {
        "receipt_kind": "pcg_cluster_assembly_handoff",
        "schema_version": SCHEMA_VERSION,
        "status": handoff_status,
        "tree_source_identities": source_identities,
        "cluster_dependencies": dependency_receipts,
        "canonical_bark": bark,
        "full_skeletal_mesh_preserved": True,
        "separate_nanite_assembly_requested": bool(role_receipts),
        "requires_actual_fbx_revalidation": True,
        "skeleton_wind_contract": {
            "mode": "regenerate_from_final_skeleton",
            "shared_by": ["full_skeletal_mesh", "nanite_assembly"],
            "requires_wind_json_bone_index_validation": True,
            "requires_binding_hierarchy_validation": True,
        },
        "roles": role_receipts,
        "issues": issues,
        "errors": issues,
    }

    return {
        "schema_version": SCHEMA_VERSION,
        "folder": str(folder),
        "tree_source_identities": source_identities,
        "hierarchy": {
            "name": "Cluster",
            "path": str(folder / "Cluster"),
            "children": children,
        },
        "dependencies": actual_dependencies,
        "canonical_bark": bark,
        "handoff": handoff,
    }


def _normalized_identity_path(value):
    return os.path.normcase(os.path.abspath(os.path.normpath(str(value))))


def _tree_identity_paths(contract):
    paths = []
    for row in contract.get("tree_source_identities") or []:
        for key in ("target_spm", "authoritative_tree_source"):
            path = (row.get(key) or {}).get("path")
            if path:
                paths.append(_normalized_identity_path(path))
    return sorted(set(paths))


def source_path_identity(contract):
    """Return the stable folder receipt identity derived only from source paths."""
    paths = sorted({
        _normalized_identity_path(path)
        for row in contract.get("tree_source_identities") or []
        for path in [(row.get("authoritative_tree_source") or {}).get("path")]
        if path
    })
    if not paths:
        paths = _tree_identity_paths(contract)
    if not paths:
        raise ClusterAssemblyReceiptError(
            "Cluster Assembly contract has no Tree SPM path identity")
    digest = hashlib.sha256("\n".join(paths).encode("utf-8")).hexdigest()
    return {
        "algorithm": "sha256",
        "normalized_paths": paths,
        "sha256": digest,
    }


def cluster_assembly_receipt_path(contract, receipt_dir=None):
    """Return one stable tool-owned path for a folder's self-contained receipt."""
    identity = source_path_identity(contract)
    folder_name = Path(str(contract.get("folder") or "asset")).name or "asset"
    safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", folder_name).strip("._")
    if not safe_name:
        safe_name = "asset"
    directory = Path(receipt_dir) if receipt_dir else DEFAULT_RECEIPT_DIR
    return directory / (
        f"cluster_assembly_{safe_name}_{identity['sha256'][:20]}.json")


def _upgrade_persisted_hashes(contract):
    """Hash receipt-owned Cluster TGAs without changing source or audit state."""
    result = copy.deepcopy(contract)
    dependency_groups = [result.get("dependencies") or []]
    handoff = result.get("handoff") or {}
    dependency_groups.append(handoff.get("cluster_dependencies") or [])
    for dependencies in dependency_groups:
        for dependency in dependencies:
            textures = dependency.get("texture_dependencies") or []
            dependency["texture_dependencies"] = [
                file_fingerprint(row.get("path"), hash_content=True)
                if isinstance(row, dict) and row.get("path") else row
                for row in textures
            ]
    return result


def _atomic_write_json(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (
        json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True)
        + "\n"
    ).encode("utf-8")
    try:
        if path.read_bytes() == encoded:
            return False
    except OSError:
        pass
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(str(temp_path), str(path))
    except Exception:
        try:
            temp_path.unlink()
        except OSError:
            pass
        raise
    return True


def persist_cluster_assembly_receipt(contract, receipt_dir=None):
    """Atomically persist one self-contained receipt under the tool reports."""
    if not isinstance(contract, dict):
        raise ClusterAssemblyReceiptError("Cluster Assembly contract must be a dict")
    persisted_contract = _upgrade_persisted_hashes(contract)
    identity = source_path_identity(persisted_contract)
    path = cluster_assembly_receipt_path(persisted_contract, receipt_dir)
    payload = {
        "receipt_kind": PERSISTED_RECEIPT_KIND,
        "schema_version": SCHEMA_VERSION,
        "source_path_identity": identity,
        "cluster_assembly": persisted_contract,
    }
    _atomic_write_json(path, payload)
    return path


def persist_cluster_assembly_receipts(report, receipt_dir=None):
    """Persist every actionable Cluster contract in one existing audit report."""
    written = []
    for item in (report or {}).get("items") or []:
        contract = item.get("cluster_assembly") or {}
        if not (contract.get("dependencies") and contract.get("tree_source_identities")):
            continue
        path = persist_cluster_assembly_receipt(contract, receipt_dir)
        item["cluster_assembly_receipt"] = str(path)
        written.append(str(path))
    return written


def _receipt_contract(payload):
    if not isinstance(payload, dict):
        raise ClusterAssemblyReceiptError("Cluster Assembly receipt root must be a dict")
    if payload.get("receipt_kind") != PERSISTED_RECEIPT_KIND:
        raise ClusterAssemblyReceiptError("unsupported Cluster Assembly receipt kind")
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise ClusterAssemblyReceiptError("unsupported Cluster Assembly receipt schema")
    contract = payload.get("cluster_assembly")
    if not isinstance(contract, dict):
        raise ClusterAssemblyReceiptError("Cluster Assembly receipt has no contract")
    if payload.get("source_path_identity") != source_path_identity(contract):
        raise ClusterAssemblyReceiptStaleError(
            "Cluster Assembly receipt source path identity is stale")
    return contract


def _current_fingerprint_matches(expected):
    path = expected.get("path") if isinstance(expected, dict) else None
    expected_hash = expected.get("sha256") if isinstance(expected, dict) else None
    if not path or not expected_hash:
        return False
    actual = file_fingerprint(path, hash_content=True)
    return bool(
        actual.get("exists")
        and actual.get("sha256") == expected_hash
    )


def validate_cluster_assembly_receipt(payload, requested_spm=None):
    """Reject path mismatch and every stale Tree/Cluster SPM or TGA hash."""
    contract = _receipt_contract(payload)
    identity_paths = set(_tree_identity_paths(contract))
    if requested_spm is not None:
        requested_path = _normalized_identity_path(requested_spm)
        if requested_path not in identity_paths:
            raise ClusterAssemblyReceiptError(
                "Cluster Assembly receipt does not identify the requested SPM")

    expected_artifacts = []
    for row in contract.get("tree_source_identities") or []:
        expected_artifacts.extend(
            row.get(key) or {}
            for key in ("target_spm", "authoritative_tree_source")
        )
    dependencies = (
        (contract.get("handoff") or {}).get("cluster_dependencies")
        or contract.get("dependencies")
        or []
    )
    for dependency in dependencies:
        pair_fingerprints = [
            dependency.get("authoring_spm_fingerprint"),
            dependency.get("output_spm_fingerprint"),
        ]
        if any(pair_fingerprints):
            expected_artifacts.extend(
                row for row in pair_fingerprints
                if isinstance(row, dict) and row.get("exists")
            )
        else:
            expected_artifacts.append(dependency.get("spm_fingerprint") or {})
        expected_artifacts.extend(dependency.get("texture_dependencies") or [])
        variants = dependency.get("normalized_variants") or {}
        if variants:
            expected_artifacts.extend([
                variants.get("manifest") or {},
                variants.get("source_blend") or {},
            ])
            expected_artifacts.extend(
                row.get("plan_fbx") or {}
                for row in variants.get("variants") or []
            )

    stale = []
    seen = set()
    for expected in expected_artifacts:
        path = expected.get("path") if isinstance(expected, dict) else None
        if not path:
            stale.append("<missing path>")
            continue
        key = _normalized_identity_path(path)
        if key in seen:
            continue
        seen.add(key)
        if not _current_fingerprint_matches(expected):
            stale.append(str(path))
    if stale:
        raise ClusterAssemblyReceiptStaleError(
            "Cluster Assembly receipt artifact hash is stale: "
            + "; ".join(stale))
    return contract


def load_cluster_assembly_receipt(path, requested_spm=None):
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    validate_cluster_assembly_receipt(payload, requested_spm=requested_spm)
    return payload


def cluster_assembly_receipt_resolution(spm_path, receipt_dir=None):
    """Resolve the newest hash-current receipt and retain the candidate audit.

    Repeated PCG audits can legitimately persist overlapping folder snapshots
    (for example, four selected Tree SPMs followed by six).  Those snapshots
    have different stable receipt names but can all identify the same SK SPM.
    Treating that history as an ambiguity permanently blocked Blender Repair.

    Stale snapshots are never selected.  When several hash-current snapshots
    remain, the newest tool-owned receipt is authoritative and the older paths
    are returned as explicit, non-destructive audit history.
    """
    requested = _normalized_identity_path(spm_path)
    directory = Path(receipt_dir) if receipt_dir else DEFAULT_RECEIPT_DIR
    matches = []
    for path in sorted(directory.glob("cluster_assembly_*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            contract = _receipt_contract(payload)
        except (OSError, json.JSONDecodeError, ClusterAssemblyReceiptError):
            continue
        if requested in set(_tree_identity_paths(contract)):
            matches.append((path, payload))
    if not matches:
        raise FileNotFoundError(
            f"no Cluster Assembly receipt identifies SPM: {spm_path}")

    current = []
    stale = []
    for path, payload in matches:
        try:
            validate_cluster_assembly_receipt(payload, requested_spm=spm_path)
        except ClusterAssemblyReceiptError as exc:
            stale.append({"path": str(path), "error": str(exc)})
            continue
        try:
            mtime_ns = path.stat().st_mtime_ns
        except OSError:
            mtime_ns = -1
        current.append({"path": path, "mtime_ns": mtime_ns})

    if not current:
        details = "; ".join(
            f"{Path(row['path']).name}: {row['error']}" for row in stale
        )
        raise ClusterAssemblyReceiptStaleError(
            f"no hash-current Cluster Assembly receipt identifies SPM: "
            f"{spm_path}" + (f" ({details})" if details else "")
        )

    current.sort(
        key=lambda row: (row["mtime_ns"], str(row["path"]).casefold())
    )
    selected = current[-1]
    current_rows = [
        {"path": str(row["path"]), "mtime_ns": row["mtime_ns"]}
        for row in current
    ]
    return {
        "policy": "newest_hash_current_receipt",
        "requested_spm": str(spm_path),
        "selected_receipt": str(selected["path"]),
        "current_candidates": current_rows,
        "superseded_current_receipts": [
            row["path"] for row in current_rows[:-1]
        ],
        "ignored_stale_candidates": stale,
    }


def locate_cluster_assembly_receipt(spm_path, receipt_dir=None):
    """Locate the authoritative current receipt for an SPM."""
    resolution = cluster_assembly_receipt_resolution(spm_path, receipt_dir)
    return Path(resolution["selected_receipt"])
