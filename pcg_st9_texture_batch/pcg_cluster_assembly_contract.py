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
from atlas_target_registry import (
    TargetRegistryError,
    load_target_registry,
)


SCHEMA_VERSION = 1
PERSISTED_RECEIPT_KIND = "pcg_cluster_assembly_receipt"
DEFAULT_RECEIPT_DIR = Path(__file__).resolve().parent / "reports" / "cluster_assembly"
FBX_BINARY_HEADER = b"Kaydara FBX Binary  \x00\x1a\x00"
ROLE_ORDER = ("branch", "cluster", "leaf", "leaf_side")
ROLE_PREFIX_RE = re.compile(
    r"^(leaf_side|leaf(?:_[^_]+)*_side|branch|cluster|leaf)(?:_|$)",
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


def _fresh_file_fingerprint(path):
    """Hash one stable snapshot without trusting the process-wide stat cache."""
    candidate = Path(path)
    for _attempt in range(2):
        try:
            before = candidate.stat()
            digest = hashlib.sha256()
            size = 0
            with candidate.open("rb") as handle:
                for chunk in iter(
                    lambda: handle.read(1024 * 1024),
                    b"",
                ):
                    digest.update(chunk)
                    size += len(chunk)
            after = candidate.stat()
        except OSError:
            return {
                "path": str(candidate),
                "exists": False,
                "size": None,
                "mtime_ns": None,
                "sha256": None,
            }
        if (
            size == after.st_size
            and (before.st_size, before.st_mtime_ns)
            == (after.st_size, after.st_mtime_ns)
        ):
            return {
                "path": str(candidate),
                "exists": True,
                "size": after.st_size,
                "mtime_ns": after.st_mtime_ns,
                "sha256": digest.hexdigest(),
            }
    raise ClusterAssemblyReceiptStaleError(
        "Artifact changed while its content fingerprint was calculated: "
        + str(candidate)
    )


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


def classify_fbx_role(
    fbx_report,
    role_identity,
    role_identity_aliases=(),
):
    """Apply the independent content-driven complete/absent/partial gate."""
    identities = [role_identity, *(role_identity_aliases or ())]
    identity_alias_values = [
        identity
        for identity in identities
        if identity and identity != role_identity
    ]
    expected = {
        normalize_export_name(identity)
        for identity in identities
        if identity
    }
    if fbx_report.get("status") == "missing":
        return {
            "status": "pending_export",
            "decision": "pending_export",
            "role_identity": role_identity,
            "role_identity_aliases": identity_alias_values,
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
            "role_identity_aliases": identity_alias_values,
            "material_matches": [],
            "mesh_name_matches": [],
            "complete_pairs": [],
            "complete_pair_count": 0,
            "complete_pair_evidence_truncated": False,
            "error": fbx_report.get("error"),
        }
    material_matches = [
        value for value in fbx_report.get("materials") or []
        if normalize_export_name(value) in expected
    ]
    mesh_name_matches = [
        value for value in fbx_report.get("mesh_names") or []
        if normalize_export_name(value) in expected
    ]
    matched_pairs = [
        row for row in fbx_report.get("material_mesh_pairs") or []
        if normalize_export_name(row.get("material")) in expected
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
        "role_identity_aliases": identity_alias_values,
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


def _cluster_target_relation(pair_row, target_spms):
    """Resolve whether one physical Cluster provider is ON for this target.

    Material/mesh names prove that a rendered role exists, but they do not
    prove that a same-folder Cluster blend owns that role.  When an Atlas
    target registry exists it is the authoritative ON/OFF relation.  Providers
    without a registry retain the legacy content-driven behavior so older
    projects are not silently disconnected.
    """
    source_blend = Path(pair_row["output_spm"]).with_suffix(".blend").absolute()
    try:
        registry = load_target_registry(source_blend)
    except TargetRegistryError as exc:
        raise ClusterAssemblyReceiptError(
            f"Atlas target relation registry is invalid for {source_blend}: {exc}"
        ) from exc
    if registry is None:
        return {
            "status": "legacy_unregistered",
            "allowed": True,
            "source_blend": str(source_blend),
            "registry": None,
            "registered_target_spms": [],
            "matched_target_spms": [],
        }

    registered = {
        _normalized_identity_path(value): str(value)
        for value in registry.get("target_spms") or []
    }
    selected = {
        _normalized_identity_path(value): str(Path(value).absolute())
        for value in target_spms or []
    }
    matched_keys = sorted(set(registered).intersection(selected))
    return {
        "status": "explicit_on" if matched_keys else "explicit_off",
        "allowed": bool(matched_keys),
        "source_blend": str(source_blend),
        "registry": file_fingerprint(registry["registry_path"]),
        "registered_target_spms": [
            registered[key] for key in sorted(registered)
        ],
        "matched_target_spms": [selected[key] for key in matched_keys],
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
    # Older authored trees can retain the owner-folder token in the rendered
    # bark material (for example ``Bark_tree_<species>_01``).  That is still
    # one content-driven canonical bark slot, not an absent material.  Prefer
    # the compact species identity, then the exact owner-folder identity;
    # never guess from a branch number or a convenient first material.
    accepted_identities = [expected]
    folder_identity = f"bark_{Path(folder).name}_01".casefold()
    if folder_identity not in accepted_identities:
        accepted_identities.append(folder_identity)
    candidates = []
    for spm in target_spms:
        active_ids = {
            str(value) for value in audit.active_material_ids(spm)
        }
        for row in audit.extract_material_image_refs(spm):
            if (
                active_ids
                and str(row.get("material_id")) not in active_ids
            ):
                continue
            identity = normalize_export_name(row.get("material_name"))
            if identity in accepted_identities:
                candidates.append({
                    "spm": str(spm),
                    "identity": identity,
                    **row,
                })
    canonical = []
    for identity in accepted_identities:
        canonical = [
            row for row in candidates if row["identity"] == identity
        ]
        if canonical:
            break
    canonical_basenames = {
        Path(value).name.casefold()
        for row in canonical for value in row.get("refs") or []
    }
    sources = []
    for dependency in dependencies:
        spm = dependency.get("source_spm") or dependency["spm"]
        isolated_capture = (
            (dependency.get("normalized_variants") or {}).get(
                "isolated_bark_capture"
            )
            or {}
        )
        isolated_output_materials = {
            normalize_export_name(value)
            for value in isolated_capture.get("output_materials") or []
        }
        isolated_canonical_matches = bool(
            isolated_capture
            and canonical
            and normalize_export_name(
                isolated_capture.get("canonical_material")
            )
            == canonical[0]["identity"]
        )
        active_ids = {
            str(value) for value in audit.active_material_ids(spm)
        }
        for row in audit.extract_material_image_refs(spm):
            # Cluster source files often retain unused material-library rows.
            # The bark handoff contract is about generator-bound/rendered
            # material data, not every dormant material whose label happens to
            # contain "bark".
            if str(row.get("material_id")) not in active_ids:
                continue
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
            if (
                not matches
                and not canonical
                and species
                and row.get("refs")
            ):
                matches = all(
                    species in str(value).casefold()
                    for value in row.get("refs") or []
                )
            isolated_capture_matches = bool(
                isolated_canonical_matches
                and normalize_export_name(row.get("material_name"))
                in isolated_output_materials
            )
            sources.append({
                "cluster_spm": str(spm),
                "material_id": row.get("material_id"),
                "material_name": row.get("material_name"),
                "texture_refs": list(row.get("refs") or []),
                "matches_canonical_textures": matches,
                "replacement": (
                    "not_required"
                    if matches
                    else "isolated_capture_validated"
                    if isolated_capture_matches
                    else "required"
                ),
                "normalization_evidence": (
                    isolated_capture if isolated_capture_matches else None
                ),
            })
    canonical_conflicts = []
    if not canonical and sources:
        provider_groups = {}
        for row in sources:
            if not row.get("matches_canonical_textures"):
                continue
            authority = normalize_export_name(row.get("material_name"))
            texture_signature = tuple(sorted({
                Path(value).name.casefold()
                for value in row.get("texture_refs") or []
            }))
            provider_groups.setdefault(
                (authority, texture_signature),
                [],
            ).append(row)
        if len(provider_groups) == 1:
            (authority, _texture_signature), rows = next(
                iter(provider_groups.items())
            )
            canonical = [
                {
                    "spm": row["cluster_spm"],
                    "identity": authority,
                    "material_id": row.get("material_id"),
                    "material_name": row.get("material_name"),
                    "refs": list(row.get("texture_refs") or []),
                    "authority": "active_provider_texture_provenance",
                }
                for row in rows
            ]
        elif provider_groups:
            canonical_conflicts = [
                {
                    "material_identity": identity,
                    "texture_basenames": list(texture_signature),
                    "providers": sorted({
                        row["cluster_spm"] for row in rows
                    }),
                }
                for (identity, texture_signature), rows
                in sorted(provider_groups.items())
            ]
    if not canonical and not sources:
        status = "not_applicable"
    elif canonical_conflicts:
        status = "blocked_canonical_ambiguous"
    elif not canonical:
        status = "blocked_canonical_missing"
    elif any(row["replacement"] == "required" for row in sources):
        status = "replacement_required"
    else:
        status = "canonical"
    return {
        "status": status,
        "canonical_material": (
            display_export_name(canonical[0]["material_name"])
            if canonical
            else f"M_{expected}" if sources else None
        ),
        "canonical_sources": canonical,
        "canonical_conflicts": canonical_conflicts,
        "cluster_bark_sources": sources,
        "mutation_applied": False,
    }


def _canonical_cluster_texture_refs(values, preferred_folder):
    """Collapse path aliases for the same Cluster texture to one authority.

    Older SPMs can retain an absolute path from a previous OneDrive layout
    alongside the current path.  Those rows describe the same texture when
    their filenames match; recording both makes a newly written hash receipt
    stale immediately because the legacy alias no longer exists.  Prefer an
    existing file in the Cluster output folder, then any existing alias.  A
    genuinely missing unique texture remains in the result and still blocks.
    """
    preferred_key = os.path.normcase(os.path.abspath(str(preferred_folder)))
    groups = {}
    group_order = []
    for value in values or []:
        path = Path(value)
        name_key = path.name.casefold()
        key = ("name", name_key) if name_key else (
            "path", _normalized_identity_path(path)
        )
        if key not in groups:
            groups[key] = []
            group_order.append(key)
        normalized = _normalized_identity_path(path)
        if all(
            _normalized_identity_path(existing) != normalized
            for existing in groups[key]
        ):
            groups[key].append(path)

    selected = []
    ignored_aliases = []
    for key in group_order:
        candidates = groups[key]

        def rank(path):
            parent_key = os.path.normcase(os.path.abspath(str(path.parent)))
            return (
                0 if path.is_file() else 1,
                0 if parent_key == preferred_key else 1,
                len(str(path)),
                str(path).casefold(),
            )

        authority = min(candidates, key=rank)
        selected.append(authority)
        ignored_aliases.extend(
            path for path in candidates
            if _normalized_identity_path(path)
            != _normalized_identity_path(authority)
        )
    return selected, ignored_aliases


def _tga_basename_validation(
        output_spm, dependency_usage, legacy_output_spm=None,
        resolved_refs=None, ignored_aliases=None):
    expected = Path(output_spm).stem.casefold()
    accepted = {expected}
    if legacy_output_spm:
        accepted.add(Path(legacy_output_spm).stem.casefold())
    if resolved_refs is None:
        connected_refs = dependency_usage.get("connected_refs")
        if connected_refs is None:
            connected_refs = dependency_usage.get("source_refs") or []
        refs, ignored_aliases = _canonical_cluster_texture_refs(
            connected_refs, Path(output_spm).parent
        )
    else:
        refs = [Path(value) for value in resolved_refs]
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
        "ignored_legacy_aliases": [
            str(path) for path in (ignored_aliases or [])
        ],
        "missing": missing,
        "invalid": invalid,
    }


def _normalized_capture_texture_refs(normalized_variants):
    """Return the final physical-capture maps used by normalized Assembly parts.

    The provider SPM intentionally retains its authored source images so the
    Normalizer can regenerate the capture.  Once a hash-validated physical
    Atlas receipt exists, those source refs are inputs, not the Assembly
    output texture contract.  The capture maps are the actual part textures.
    """
    normalization = (
        (normalized_variants or {}).get("production_normalization") or {}
    )
    if normalization.get("workflow_mode") != "PHYSICAL_DIRECT_CAPTURE":
        return []
    capture = normalization.get("physical_capture_contract") or {}
    capture_maps = (
        normalization.get("capture_maps")
        or capture.get("capture_maps")
        or []
    )
    refs = []
    seen = set()
    for row in capture_maps:
        value = str((row or {}).get("path") or "").strip()
        if not value:
            continue
        path = Path(value)
        key = _normalized_identity_path(path)
        if key in seen:
            continue
        seen.add(key)
        refs.append(path)
    return refs


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


def _physical_source_3d_artifacts(receipt):
    """Validate the exact SPM/FBX inputs shared by every physical variant."""
    artifacts = None
    identity = None
    variants = list((receipt or {}).get("variants") or [])
    if not variants:
        raise ClusterAssemblyReceiptError(
            "Atlas physical normalization receipt has no variants"
        )
    for index, row in enumerate(variants, 1):
        transfer = row.get("plan_uv_transfer")
        source = (
            transfer.get("source_3d_contract")
            if isinstance(transfer, dict)
            else None
        )
        if not isinstance(source, dict):
            raise ClusterAssemblyReceiptError(
                "Atlas physical variant has no source 3D contract: "
                + str(row.get("plan") or index)
            )
        current = {}
        identity_rows = []
        for artifact, label in (
            ("source_spm", "SPM"),
            ("source_fbx", "FBX"),
        ):
            path = str(source.get(artifact) or "").strip()
            recorded_hash = str(
                source.get(f"{artifact}_sha256") or ""
            ).strip().casefold()
            if not path or not recorded_hash:
                raise ClusterAssemblyReceiptError(
                    f"Atlas physical source 3D contract has no {label} "
                    f"path/hash: {row.get('plan') or index}"
                )
            fingerprint = _fresh_file_fingerprint(path)
            if (
                not fingerprint.get("exists")
                or not fingerprint.get("sha256")
                or fingerprint["sha256"].casefold() != recorded_hash
            ):
                raise ClusterAssemblyReceiptStaleError(
                    f"Atlas physical source {label} artifact hash is stale: "
                    + path
                )
            current[artifact] = fingerprint
            identity_rows.append(
                (
                    artifact,
                    _normalized_identity_path(path),
                    recorded_hash,
                )
            )
        row_identity = tuple(identity_rows)
        if identity is None:
            identity = row_identity
            artifacts = current
        elif row_identity != identity:
            raise ClusterAssemblyReceiptError(
                "Atlas physical variants have conflicting source 3D contracts"
            )
    return artifacts


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
    source_3d_artifacts = _physical_source_3d_artifacts(receipt)
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
        "source_3d_artifacts": source_3d_artifacts,
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


def _physical_variant_attachment_contract(receipt_variant, label):
    """Keep the authored plan pivot needed by downstream rigid placement."""
    transfer = (receipt_variant or {}).get("plan_uv_transfer") or {}
    try:
        vertex_index = int(transfer.get("attachment_vertex_index"))
        vertex_uv = [
            float(value)
            for value in transfer.get("attachment_vertex_uv")
        ]
    except (TypeError, ValueError) as exc:
        raise ClusterAssemblyReceiptError(
            f"{label} physical variant attachment metadata is invalid"
        ) from exc
    if (
        vertex_index < 0
        or len(vertex_uv) != 2
        or any(not math.isfinite(value) for value in vertex_uv)
    ):
        raise ClusterAssemblyReceiptError(
            f"{label} physical variant attachment metadata is invalid"
        )
    normalized_local = (
        (transfer.get("capture_attachment") or {}).get("normalized_local")
    )
    try:
        normalized_local_is_origin = (
            isinstance(normalized_local, (list, tuple))
            and len(normalized_local) == 3
            and max(abs(float(value)) for value in normalized_local)
            <= 1.0e-8
        )
    except (TypeError, ValueError):
        normalized_local_is_origin = False
    if not normalized_local_is_origin:
        raise ClusterAssemblyReceiptError(
            f"{label} physical variant attachment is not normalized to the origin"
        )
    return {
        # The index proves which authored plan vertex supplied this record.
        # FBX may reorder vertices, so downstream correspondence uses the UV.
        "attachment_vertex_index": vertex_index,
        "attachment_vertex_uv": vertex_uv,
    }


def _normalized_variant_contract(manifest_path, payload, group):
    physical = _physical_normalization_receipt(payload)
    receipt_variants = {}
    if physical is not None:
        for row in physical["receipt"].get("variants") or []:
            key = (
                str(row.get("plan") or ""),
                str(
                    row.get("skeletal_asset")
                    or row.get("prototype_asset")
                    or ""
                ),
            )
            if not all(key) or key in receipt_variants:
                raise ClusterAssemblyReceiptError(
                    "Atlas physical receipt variants are missing or duplicated"
                )
            receipt_variants[key] = row
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
        if physical is not None:
            receipt_variant = receipt_variants.get(
                (plan_name, skeletal_asset_name)
            )
            if receipt_variant is None:
                raise ClusterAssemblyReceiptError(
                    "Atlas physical receipt has no matching exported plan/SK pair: "
                    + plan_name
                )
            variant.update(
                _physical_variant_attachment_contract(
                    receipt_variant,
                    f"Atlas physical variant {plan_name}",
                )
            )
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
        receipt_variant_pairs = set(receipt_variants)
        exported_variants = {
            (row["plan_name"], row["skeletal_asset_name"])
            for row in variants
        }
        if receipt_variant_pairs != exported_variants:
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
        result["source_3d_artifacts"] = physical["source_3d_artifacts"]
    return result


def _physical_target_registry_contract(source_blend):
    """Return the explicit ON-target contract for one physical source blend."""
    blend = Path(source_blend).expanduser().absolute()
    try:
        registry = load_target_registry(blend)
    except TargetRegistryError as exc:
        raise ClusterAssemblyReceiptError(
            f"Atlas physical target registry is invalid for {blend}: {exc}"
        ) from exc
    if registry is None:
        raise ClusterAssemblyReceiptError(
            "Atlas physical normalization has no explicit target registry: "
            + str(blend)
        )
    try:
        registered_blend = Path(
            str(registry.get("atlas_blend") or "")
        ).expanduser().absolute()
    except (OSError, TypeError, ValueError) as exc:
        raise ClusterAssemblyReceiptError(
            "Atlas physical target registry has no valid blend identity: "
            + str(blend)
        ) from exc
    if _normalized_identity_path(registered_blend) != _normalized_identity_path(
        blend
    ):
        raise ClusterAssemblyReceiptError(
            "Atlas physical target registry identifies another blend: "
            + str(registered_blend)
        )
    targets = [
        Path(value).expanduser().absolute()
        for value in registry.get("target_spms") or []
    ]
    registry_fingerprint = file_fingerprint(registry["registry_path"])
    if (
        not registry_fingerprint.get("exists")
        or not registry_fingerprint.get("sha256")
    ):
        raise ClusterAssemblyReceiptError(
            "Atlas physical target registry fingerprint is unavailable: "
            + str(registry.get("registry_path") or "")
        )
    return {
        "fingerprint": registry_fingerprint,
        "target_spms": targets,
        "target_keys": {
            _normalized_identity_path(path): path for path in targets
        },
    }


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
            adoption = payload.get("source_material_adoption") or {}
            declared_final_mesh_ids = [
                int(value)
                for value in adoption.get("final_material_mesh_ids") or []
            ]
            expected_live_mesh_ids = (
                declared_final_mesh_ids
                if declared_final_mesh_ids
                else mesh_ids
            )
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
                 == expected_live_mesh_ids
            ]
            if len(current_matches) != 1:
                stale.append(str(manifest_path))
                continue
        try:
            contract = _normalized_variant_contract(
                manifest_path,
                payload,
                group,
            )
        except ClusterAssemblyReceiptStaleError:
            # A normalized receipt is a cache of a previously validated
            # delivery.  Staleness cannot decide the current content
            # contract; ignore it here and let the live SPM/FBX role audit
            # determine pass-through vs normalized_variants_required.
            stale.append(str(manifest_path))
            continue
        target_registry = None
        if contract.get("production_normalization") is not None:
            target_registry = _physical_target_registry_contract(
                contract["source_blend"]["path"]
            )
            manifest_key = _normalized_identity_path(manifest_spm)
            if manifest_key not in target_registry["target_keys"]:
                stale.append(str(manifest_path))
                continue
            contract["target_registry"] = target_registry["fingerprint"]
            contract["registered_target_spms"] = [
                str(path) for path in target_registry["target_spms"]
            ]
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
                "source_3d_artifacts": {
                    key: {
                        "path": _normalized_identity_path(row.get("path")),
                        "sha256": row.get("sha256"),
                    }
                    for key, row in (
                        contract.get("source_3d_artifacts") or {}
                    ).items()
                },
                "target_registry": (
                    contract.get("target_registry") or {}
                ).get("sha256"),
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
                        "attachment_vertex_index": row.get(
                            "attachment_vertex_index"
                        ),
                        "attachment_vertex_uv": row.get(
                            "attachment_vertex_uv"
                        ),
                    }
                    for row in contract["variants"]
                ],
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        candidates.append(
            (
                str(manifest_path),
                identity,
                contract,
                _normalized_identity_path(manifest_spm),
            )
        )
    distinct = {}
    for _manifest, identity, contract, target_key in sorted(candidates):
        selected = distinct.setdefault(
            identity,
            {"contract": contract, "delivered_target_keys": set()},
        )
        selected["delivered_target_keys"].add(target_key)
    if len(distinct) > 1:
        raise ClusterAssemblyReceiptError(
            "Atlas normalized role has multiple current receipts: "
            + str(role_identity)
        )
    if distinct:
        selected = next(iter(distinct.values()))
        contract = selected["contract"]
        if contract.get("production_normalization") is not None:
            registered = {
                _normalized_identity_path(path): Path(path)
                for path in contract.get("registered_target_spms") or []
            }
            allowed_keys = {
                _normalized_identity_path(path) for path in allowed_spms
            }
            required_keys = set(registered).intersection(allowed_keys)
            missing = sorted(
                required_keys.difference(
                    selected["delivered_target_keys"]
                )
            )
            if missing:
                return None
        return contract
    if stale:
        return None
    return None


def _artifact_identity_matches(recorded, current):
    if not isinstance(recorded, dict) or not isinstance(current, dict):
        return False
    recorded_path = recorded.get("path") or recorded.get("canonical_path")
    current_path = current.get("path") or current.get("canonical_path")
    return bool(
        recorded_path
        and current_path
        and recorded.get("sha256")
        and current.get("sha256")
        and _normalized_identity_path(recorded_path)
        == _normalized_identity_path(current_path)
        and str(recorded["sha256"]).casefold()
        == str(current["sha256"]).casefold()
    )


def _validated_isolated_bark_capture(normalized_variants, output_spm):
    """Prove that a physical Atlas capture used a current isolated bark export.

    The production provider SPM intentionally keeps its authored material
    library.  A content-addressed isolated copy may replace only the live bark
    texture set for BWR/Atlas.  Accept that different SPM path only through the
    immutable normalization manifest, a fresh export-bundle validation, and
    the exact FBX/SPM artifacts recorded by the physical capture.
    """
    source_blend = (normalized_variants or {}).get("source_blend") or {}
    blend_path = Path(str(source_blend.get("path") or ""))
    if not blend_path.is_file():
        raise ClusterAssemblyReceiptStaleError(
            "Atlas isolated bark capture has no current source blend"
        )
    current_blend = _fresh_file_fingerprint(blend_path)
    if not _artifact_identity_matches(source_blend, current_blend):
        raise ClusterAssemblyReceiptStaleError(
            "Atlas isolated bark capture source blend is stale: "
            + str(blend_path)
        )
    current_output = _fresh_file_fingerprint(output_spm)
    recorded = (
        (normalized_variants.get("source_3d_artifacts") or {}).get(
            "source_spm"
        )
        or {}
    )
    isolated_spm_path = Path(str(recorded.get("path") or ""))
    isolated_spm = _fresh_file_fingerprint(isolated_spm_path)
    if not _artifact_identity_matches(recorded, isolated_spm):
        raise ClusterAssemblyReceiptStaleError(
            "Atlas physical normalization source does not match the current "
            "isolated bark SPM"
        )

    manifest_path = next(
        (
            parent / "bark_normalization_manifest.json"
            for parent in isolated_spm_path.parents
            if (parent / "bark_normalization_manifest.json").is_file()
        ),
        None,
    )
    if manifest_path is None:
        raise ClusterAssemblyReceiptStaleError(
            "Atlas isolated bark capture has no normalization manifest"
        )
    current_manifest = _fresh_file_fingerprint(manifest_path)
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ClusterAssemblyReceiptStaleError(
            "Atlas isolated bark normalization manifest is unreadable"
        ) from exc
    if (
        manifest.get("kind")
        != "cluster_isolated_canonical_bark_source"
        or manifest.get("status") != "ready"
        or manifest.get("production_source_mutated") is not False
        or _normalized_identity_path(manifest.get("source_spm"))
        != _normalized_identity_path(output_spm)
        or str(manifest.get("source_spm_sha256") or "").casefold()
        != str(current_output.get("sha256") or "").casefold()
        or not _artifact_identity_matches(
            {
                "path": manifest.get("speedtree_spm"),
                "sha256": manifest.get("isolated_spm_sha256"),
            },
            recorded,
        )
    ):
        raise ClusterAssemblyReceiptStaleError(
            "Atlas isolated bark normalization manifest does not match the "
            "captured source pair"
        )

    recorded_fbx = (
        (normalized_variants.get("source_3d_artifacts") or {}).get(
            "source_fbx"
        )
        or {}
    )
    isolated_fbx_path = Path(str(recorded_fbx.get("path") or ""))
    if not _artifact_identity_matches(
        recorded_fbx,
        _fresh_file_fingerprint(isolated_fbx_path),
    ):
        raise ClusterAssemblyReceiptStaleError(
            "Atlas isolated bark capture FBX is stale"
        )
    try:
        from pcg_cluster_bark_normalization import (
            BarkNormalizationError,
            validate_canonical_bark_export_bundle,
        )

        validation = validate_canonical_bark_export_bundle(
            isolated_fbx_path,
            isolated_fbx_path.with_suffix(".stmat"),
            isolated_spm_path.parent
            / "xml"
            / f"{isolated_spm_path.stem}.xml",
            manifest.get("normalization") or {},
        )
    except (BarkNormalizationError, OSError, ValueError) as exc:
        raise ClusterAssemblyReceiptStaleError(
            "Atlas isolated bark capture export bundle is no longer valid: "
            + str(exc)
        ) from exc
    if (
        validation.get("status")
        != "ready_for_downstream_blender_mapping"
        or validation.get("production_sources_mutated") is not False
        or validation.get("material_slot_propagated") is not True
        or validation.get("texture_set_propagated") is not True
        or validation.get("uv_preserved") is not True
        or _normalized_identity_path(
            (validation.get("fbx") or {}).get("path")
        )
        != _normalized_identity_path(recorded_fbx.get("path"))
    ):
        raise ClusterAssemblyReceiptStaleError(
            "Atlas isolated bark capture has no current validated FBX bark "
            "mapping"
        )

    canonical_material = str(
        (manifest.get("normalization") or {}).get(
            "canonical_material"
        )
        or ""
    ).strip()
    output_materials = [
        str(value)
        for value in validation.get("output_materials") or []
        if str(value).strip()
    ]
    if not canonical_material or not output_materials:
        raise ClusterAssemblyReceiptError(
            "Atlas isolated bark capture has no canonical/output material "
            "identity"
        )
    return {
        "status": "validated",
        "policy": "current_bwr_isolated_bark_physical_capture_v1",
        "canonical_material": canonical_material,
        "output_materials": output_materials,
        "production_spm": current_output,
        "isolated_spm": isolated_spm,
        "isolated_fbx": recorded_fbx,
        "source_blend": current_blend,
        "normalization_manifest": current_manifest,
        "export_validation": validation,
        "production_source_mutated": False,
    }


def _validate_normalized_source_dependency(normalized_variants, output_spm):
    """Bind a physical normalization receipt to this exact Cluster input."""
    if not normalized_variants or not normalized_variants.get(
        "production_normalization"
    ):
        return
    recorded = (
        normalized_variants.get("source_3d_artifacts") or {}
    ).get("source_spm")
    if not isinstance(recorded, dict):
        raise ClusterAssemblyReceiptError(
            "Atlas physical normalized variants have no source SPM artifact"
        )
    expected = file_fingerprint(output_spm)
    if (
        not expected.get("exists")
        or not expected.get("sha256")
    ):
        raise ClusterAssemblyReceiptStaleError(
            "Atlas physical normalization source does not match the current "
            "Cluster dependency: "
            + str(output_spm)
        )
    if _artifact_identity_matches(recorded, expected):
        return
    normalized_variants["isolated_bark_capture"] = (
        _validated_isolated_bark_capture(
            normalized_variants,
            output_spm,
        )
    )


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
    selected_relation_targets = list(dict.fromkeys(
        full_target_spms + assembly_source_spms
    ))
    for pair_row in cluster_pairs:
        pair_row["target_relation"] = _cluster_target_relation(
            pair_row,
            selected_relation_targets,
        )
    clusters = [row["output_spm"] for row in cluster_pairs]
    usage = cluster_usage
    if usage is None:
        usage = audit.cluster_material_usage(
            assembly_source_spms, clusters)

    for pair_row in cluster_pairs:
        dependency_usage = _usage_for_cluster(usage, pair_row)
        usage_roles = {
            dependency_role(name)
            for name in (
                (dependency_usage or {}).get("material_names") or []
            )
            if dependency_role(name)
        }
        filename_role = dependency_role(pair_row["output_spm"].stem)
        pair_row["filename_role"] = filename_role
        pair_row["usage_roles"] = sorted(usage_roles)
        pair_row["content_role"] = (
            next(iter(usage_roles))
            if len(usage_roles) == 1
            else filename_role
        )
    content_candidates = [
        row for row in cluster_pairs
        if _usage_for_cluster(usage, row) is not None
        and row.get("content_role") in ROLE_ORDER
    ]
    relevant_clusters = [
        row for row in content_candidates
        if row["target_relation"]["allowed"]
    ]
    excluded_unregistered_clusters = [
        {
            "role": row.get("content_role"),
            "name": row["output_spm"].stem,
            "spm": str(row["output_spm"]),
            "reason": "explicit_target_relation_off",
            "target_relation": copy.deepcopy(row["target_relation"]),
        }
        for row in content_candidates
        if not row["target_relation"]["allowed"]
    ]
    export_bundles = {
        str(spm).casefold(): _export_bundle(spm)
        for spm in assembly_source_spms
    } if relevant_clusters else {}
    primary_provider_by_role = {}
    for role in ROLE_ORDER:
        providers = [
            row for row in relevant_clusters
            if row.get("content_role") == role
        ]
        if not providers:
            continue
        preferred_identity = _expected_role_identity(folder, role)
        primary_provider_by_role[role] = next(
            (
                row for row in providers
                if normalize_export_name(row["output_spm"].stem)
                == normalize_export_name(preferred_identity)
            ),
            providers[0],
        )
    actual_dependencies = []
    for pair_row in relevant_clusters:
        cluster = pair_row["source_spm"]
        authoring_spm = pair_row["authoring_spm"]
        output_spm = pair_row["output_spm"]
        role = pair_row["content_role"]
        dependency_usage = _usage_for_cluster(usage, pair_row)
        primary_provider = primary_provider_by_role[role]
        expected_identity = primary_provider["output_spm"].stem
        primary_role_source = (
            _normalized_identity_path(output_spm)
            == _normalized_identity_path(primary_provider["output_spm"])
        )
        # One normalized plan set belongs to the exact canonical provider for a
        # role.  Same-role siblings are provenance/reference-only inputs and
        # must never be validated against the canonical provider's receipt.
        normalized_variants = None
        if primary_role_source:
            normalized_variants = _atlas_normalized_variants(
                folder,
                expected_identity,
                full_target_spms,
                audit=audit,
            )
            _validate_normalized_source_dependency(
                normalized_variants,
                output_spm,
            )
        usage_roles = set(pair_row.get("usage_roles") or [])
        role_conflict = len(usage_roles) > 1
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
                    material_names,
                )
            else:
                fbx_gate = {
                    "status": "reference_only",
                    "decision": "reference_only",
                    "role_identity": role_identity,
                    "role_identity_aliases": material_names,
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
        capture_texture_refs = _normalized_capture_texture_refs(
            normalized_variants
        )
        if capture_texture_refs:
            texture_refs, ignored_texture_aliases = (
                _canonical_cluster_texture_refs(
                    capture_texture_refs, output_spm.parent
                )
            )
            texture_contract_source = "atlas_physical_capture"
        else:
            raw_texture_refs = (
                dependency_usage.get("connected_refs")
                if dependency_usage.get("connected_refs") is not None
                else dependency_usage.get("source_refs") or []
            )
            texture_refs, ignored_texture_aliases = (
                _canonical_cluster_texture_refs(
                    raw_texture_refs, output_spm.parent
                )
            )
            texture_contract_source = "connected_spm_material"
        if decision == "reference_only":
            tga_validation = {
                "status": "not_applicable",
                "reason": "same_role_reference_provider_is_not_an_assembly_part",
                "expected_base": output_spm.stem,
                "accepted_legacy_base": (
                    pair_row["legacy_output_spm"].stem
                    if pair_row["legacy_output_spm"]
                    else None
                ),
                "refs": [str(path) for path in texture_refs],
                "ignored_legacy_aliases": [
                    str(path) for path in ignored_texture_aliases
                ],
                "missing": [],
                "invalid": [],
            }
        else:
            tga_validation = _tga_basename_validation(
                output_spm,
                dependency_usage,
                legacy_output_spm=pair_row["legacy_output_spm"],
                resolved_refs=texture_refs,
                ignored_aliases=ignored_texture_aliases,
            )
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
            "filename_role": pair_row.get("filename_role"),
            "usage_roles": sorted(usage_roles),
            "spm_fingerprint": file_fingerprint(cluster),
            "authoring_spm_fingerprint": file_fingerprint(authoring_spm),
            "output_spm_fingerprint": file_fingerprint(output_spm),
            "source_materials": _material_rows(audit, cluster),
            "source_mesh_ids": sorted(audit.mesh_asset_ids(cluster)),
            "texture_dependencies": [
                file_fingerprint(value, hash_content=False)
                for value in texture_refs
            ],
            "texture_contract_source": texture_contract_source,
            "tga_basename_validation": tga_validation,
            "referenced_by_spms": list(dependency_usage.get("spms") or []),
            "target_material_names": list(
                dependency_usage.get("material_names") or []),
            "targets": targets,
            "decision": decision,
            "normalized_variants": normalized_variants,
            "normalized_variants_required": normalized_variants_required,
            "normalized_variants_missing": normalized_variants_missing,
            "target_relation": copy.deepcopy(pair_row["target_relation"]),
        })

    children = []
    for role in ROLE_ORDER:
        role_dependencies = [
            row for row in actual_dependencies if row["role"] == role
        ]
        if not role_dependencies:
            continue
        primary = next(
            (
                row for row in role_dependencies
                if row.get("primary_role_source")
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

    if actual_dependencies:
        bark = _canonical_bark_contract(
            audit,
            folder,
            full_target_spms or assembly_source_spms,
            actual_dependencies,
        )
    else:
        # Canonical bark is an Assembly part-sharing contract.  Ordinary
        # vegetation with no content-driven Cluster dependency has nothing to
        # normalize and must remain a real pass-through instead of being
        # blocked merely because a species-shaped bark name is absent.
        bark = {
            "status": "not_applicable",
            "canonical_material": (
                f"M_bark_{_asset_species(folder)}_01"
            ),
            "canonical_sources": [],
            "cluster_bark_sources": [],
            "mutation_applied": False,
        }
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
        if tga_validation.get("status") not in {"ok", "not_applicable"}:
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
            "target_relation": row.get("target_relation"),
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
        "relationship_policy": {
            "mode": "explicit_registry_when_present_else_legacy_content",
            "excluded_cluster_sources": excluded_unregistered_clusters,
        },
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
        "relationship_policy": {
            "mode": "explicit_registry_when_present_else_legacy_content",
            "excluded_cluster_sources": excluded_unregistered_clusters,
        },
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
    """Hash receipt-owned artifacts without changing source or audit state."""
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
    for role in handoff.get("roles") or []:
        for target in role.get("targets") or []:
            bundle = target.get("export_bundle") or {}
            for artifact in ("fbx", "xml", "stmat"):
                row = bundle.get(artifact)
                if isinstance(row, dict) and row.get("path"):
                    bundle[artifact] = file_fingerprint(
                        row["path"], hash_content=True
                    )
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
    """Atomically persist one self-consistent snapshot under tool reports.

    The live audit contract remains authoritative for data errors.  A receipt
    only proves that the existing artifacts observed by that audit have not
    changed, so a blocked contract with a genuinely missing dependency can
    still have a hash-current receipt whose handoff issues explain the block.
    """
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
    # Never publish a receipt that invalidates itself immediately.  Missing
    # source data is represented by the authoritative handoff issues, while
    # this validation covers only artifacts that existed in the snapshot.
    validate_cluster_assembly_receipt(payload)
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


def _upgrade_legacy_texture_alias_receipt(contract):
    """Normalize legacy duplicate texture paths without hiding real absence.

    Receipts written before path-alias normalization can contain both a live
    Cluster output and a dead absolute path from an older OneDrive layout.
    Loading those receipts must not revive the old false data error.  This is
    an in-memory compatibility upgrade only; genuinely unique missing paths
    remain in both the dependency and the authoritative handoff issues.
    """
    handoff = contract.get("handoff") or {}
    dependency_groups = [
        contract.get("dependencies") or [],
        handoff.get("cluster_dependencies") or [],
    ]
    healed_dependencies = set()
    for dependencies in dependency_groups:
        for dependency in dependencies:
            texture_rows = [
                row
                for row in dependency.get("texture_dependencies") or []
                if isinstance(row, dict) and row.get("path")
            ]
            if len(texture_rows) < 2:
                continue
            output_spm = (
                dependency.get("output_spm")
                or dependency.get("authoring_spm")
                or dependency.get("spm")
                or ""
            )
            selected, ignored = _canonical_cluster_texture_refs(
                [row["path"] for row in texture_rows],
                Path(output_spm).parent if output_spm else Path("."),
            )
            if not ignored:
                continue
            rows_by_path = {
                _normalized_identity_path(row["path"]): row
                for row in texture_rows
            }
            dependency["texture_dependencies"] = [
                rows_by_path[_normalized_identity_path(path)]
                for path in selected
            ]

            validation = dependency.get("tga_basename_validation")
            if not isinstance(validation, dict):
                continue
            accepted = {
                str(value).casefold()
                for value in (
                    validation.get("expected"),
                    validation.get("legacy_expected"),
                )
                if value
            }
            missing = [str(path) for path in selected if not path.is_file()]
            invalid = [
                str(path)
                for path in selected
                if path.is_file()
                and accepted
                and path.stem.casefold() not in accepted
            ]
            ignored_values = list(
                validation.get("ignored_legacy_aliases") or []
            )
            for path in ignored:
                if _normalized_identity_path(path) not in {
                    _normalized_identity_path(value)
                    for value in ignored_values
                }:
                    ignored_values.append(str(path))
            validation.update({
                "status": (
                    "missing" if missing else "invalid" if invalid else "ok"
                ),
                "refs": [str(path) for path in selected],
                "ignored_legacy_aliases": ignored_values,
                "missing": missing,
                "invalid": invalid,
            })
            if validation["status"] == "ok":
                healed_dependencies.add((
                    str(dependency.get("role") or "").casefold(),
                    _normalized_identity_path(
                        dependency.get("spm")
                        or dependency.get("output_spm")
                        or ""
                    ),
                ))

    if healed_dependencies:
        for issue_field in ("issues", "errors"):
            issues = handoff.get(issue_field)
            if not isinstance(issues, list):
                continue
            handoff[issue_field] = [
                issue
                for issue in issues
                if not (
                    isinstance(issue, dict)
                    and issue.get("code") == "CLUSTER_TGA_BASENAME_INVALID"
                    and (
                        str(issue.get("role") or "").casefold(),
                        _normalized_identity_path(issue.get("spm") or ""),
                    ) in healed_dependencies
                )
            ]
    return contract


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
    _upgrade_legacy_texture_alias_receipt(contract)
    if payload.get("source_path_identity") != source_path_identity(contract):
        raise ClusterAssemblyReceiptStaleError(
            "Cluster Assembly receipt source path identity is stale")
    return contract


def _current_fingerprint_matches(expected):
    path = expected.get("path") if isinstance(expected, dict) else None
    expected_hash = expected.get("sha256") if isinstance(expected, dict) else None
    if not path or not expected_hash:
        return False
    actual = _fresh_file_fingerprint(path)
    return bool(
        actual.get("exists")
        and actual.get("sha256") == expected_hash
    )


def validate_cluster_assembly_receipt(payload, requested_spm=None):
    """Reject path mismatch and changed artifacts from a persisted snapshot."""
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
        expected_artifacts.extend(
            row
            for row in dependency.get("texture_dependencies") or []
            # A missing texture is an authoritative live-audit data issue, not
            # proof that the receipt snapshot itself is stale.  If the texture
            # later appears, the next audit can produce a new ready snapshot.
            if isinstance(row, dict) and row.get("exists")
        )
        variants = dependency.get("normalized_variants") or {}
        if variants:
            production = variants.get("production_normalization") or {}
            if production.get("workflow_mode") == "PHYSICAL_DIRECT_CAPTURE":
                try:
                    nested_artifacts = _physical_source_3d_artifacts(
                        production
                    )
                except ClusterAssemblyReceiptStaleError:
                    raise
                except ClusterAssemblyReceiptError as exc:
                    raise ClusterAssemblyReceiptStaleError(
                        "Persisted physical normalization source contract is "
                        f"stale: {exc}"
                    ) from exc
                extracted_artifacts = variants.get(
                    "source_3d_artifacts"
                )
                if (
                    not isinstance(extracted_artifacts, dict)
                    or not variants.get("target_registry")
                ):
                    raise ClusterAssemblyReceiptStaleError(
                        "Persisted physical normalization receipt predates "
                        "source/target freshness coverage and must be rebuilt"
                    )
                for artifact in ("source_spm", "source_fbx"):
                    nested = nested_artifacts.get(artifact) or {}
                    extracted = extracted_artifacts.get(artifact) or {}
                    if (
                        _normalized_identity_path(nested.get("path"))
                        != _normalized_identity_path(extracted.get("path"))
                        or str(nested.get("sha256") or "").casefold()
                        != str(extracted.get("sha256") or "").casefold()
                    ):
                        raise ClusterAssemblyReceiptStaleError(
                            "Persisted physical normalization source artifact "
                            f"disagrees with its nested receipt: {artifact}"
                        )
                dependency_spm = (
                    dependency.get("output_spm_fingerprint")
                    or dependency.get("authoring_spm_fingerprint")
                    or dependency.get("spm_fingerprint")
                    or {}
                )
                source_spm = extracted_artifacts.get("source_spm") or {}
                if (
                    _normalized_identity_path(dependency_spm.get("path"))
                    != _normalized_identity_path(source_spm.get("path"))
                    or str(dependency_spm.get("sha256") or "").casefold()
                    != str(source_spm.get("sha256") or "").casefold()
                ):
                    raise ClusterAssemblyReceiptStaleError(
                        "Persisted physical normalization source does not "
                        "match its Cluster dependency"
                    )
            expected_artifacts.extend([
                variants.get("manifest") or {},
                variants.get("source_blend") or {},
            ])
            expected_artifacts.extend(
                row
                for row in (
                    variants.get("source_3d_artifacts") or {}
                ).values()
                if isinstance(row, dict)
            )
            if variants.get("target_registry"):
                expected_artifacts.append(variants["target_registry"])
            expected_artifacts.extend(
                row.get("plan_fbx") or {}
                for row in variants.get("variants") or []
            )
    for role in (contract.get("handoff") or {}).get("roles") or []:
        for target in role.get("targets") or []:
            bundle = target.get("export_bundle") or {}
            expected_artifacts.extend(
                row
                for row in (
                    bundle.get("fbx"),
                    bundle.get("xml"),
                    bundle.get("stmat"),
                )
                # A missing export remains a valid pending-export state. Once
                # present in a persisted receipt, its SHA is authoritative.
                if isinstance(row, dict) and row.get("exists")
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
