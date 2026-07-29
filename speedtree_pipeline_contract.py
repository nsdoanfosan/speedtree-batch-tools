"""Shared, dependency-free SpeedTree pipeline facts for batch workflows.

The policy API lives beside ``substance-tools/pipeline_contract.json``.  This
module only discovers that API and adds file-format/provenance helpers that are
specific to SpeedTree Batch.  It deliberately contains no Blender, Unreal, or
GUI dependency.
"""
from __future__ import annotations

import contextlib
import copy
import gzip
import hashlib
import importlib.util
import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from functools import lru_cache
from pathlib import Path


PREFLIGHT_CONTRACT_KIND = "speedtree_material_preflight"
PREFLIGHT_SCHEMA_VERSION = 1
SPM_STRUCTURAL_SEMANTIC_PROJECTION_VERSION = 1
TREE_USER_DATA_PROPERTY = "SpeedTree SDK:User data"
BACKUP_DIRECTORY_NAMES = frozenset(
    {
        "_spm_backups",
        "_skbatch_backup",
        "_pcgtex_backups",
        "_atlas_cluster_normalization_backups",
    }
)
BACKUP_FILENAME_RE = re.compile(
    r"\.(?:codex_backup|skbatch_backup|pcgtex_backup)", re.IGNORECASE
)
_SPM_SEMANTIC_IGNORED_SUBTREE_TAGS = frozenset({"Thumbnail", "Preview"})
_SPM_SEMANTIC_MATERIAL_GEOMETRY_TAGS = frozenset(
    {
        "CutoutMeshID",
        "SupplementalCutoutMeshIDs",
        "UVAreas",
        "Width",
        "Height",
        "UnwrapScale",
        "AtlasMaker",
    }
)


def _canonical_path(value):
    path = Path(str(value or "")).expanduser()
    try:
        return path.resolve(strict=False)
    except (OSError, RuntimeError):
        return path.absolute()


def canonical_path(value):
    """Return the normalized absolute spelling used in report provenance."""
    return str(_canonical_path(value))


def canonical_path_key(value):
    """Return a case-insensitive comparison key without changing display case."""
    return os.path.normcase(canonical_path(value)).casefold()


def branch_generator_has_render_geometry(properties):
    """Return whether one Branch/Trunk generator can contribute a render mesh.

    ``Skin:Type=3`` is an authoring scaffold unless a segment mesh is enabled.
    Generated spline nodes alone do not make that scaffold an FBX material
    consumer.
    """
    properties = properties or {}

    def number(value):
        try:
            return float(str(value).strip())
        except (TypeError, ValueError):
            return None

    skin_type = number(properties.get("Skin:Type"))
    skin_visibility = number(properties.get("Skin:Visibility"))
    segment_mesh = str(
        properties.get("Segments:Features:Mesh:Enabled") or ""
    ).strip().casefold() in {"1", "true", "yes"}
    has_visible_skin = (
        skin_type is not None
        and skin_type != 3.0
        and (skin_visibility is None or skin_visibility > 0.0)
    )
    return bool(has_visible_skin or segment_mesh)


def is_live_spm(path, require_file=True):
    """Classify an active SPM while excluding every pipeline backup namespace."""
    candidate = _canonical_path(path)
    if candidate.suffix.casefold() != ".spm":
        return False
    if BACKUP_FILENAME_RE.search(candidate.name):
        return False
    if any(part.casefold() in BACKUP_DIRECTORY_NAMES for part in candidate.parts):
        return False
    return candidate.is_file() if require_file else True


def production_spm_folders(root):
    """Yield the only folders that can hold a production SPM identity.

    A vegetation root holds ``<owner>/x.spm`` and ``<owner>/Cluster/x.spm`` and
    nothing else.  Every other SPM under the root is a work artifact: capture
    staging, verify candidates, timestamped safety copies, per-experiment
    output trees.  A recursive walk surfaces all of those in the batch list and
    buries the ~20 files that actually ship, so scanning is location-driven
    instead of blacklist-driven.  PCG ST9 Texture and SPM Generator Sync
    already scan this way, which is why their lists stay readable.
    """
    root = Path(root)
    if not root.is_dir():
        return
    # A root pointed straight at one vegetation folder is still valid input.
    yield root
    cluster = root / "Cluster"
    if cluster.is_dir():
        yield cluster
    try:
        owners = sorted(
            (path for path in root.iterdir() if path.is_dir()),
            key=lambda path: path.name.casefold(),
        )
    except OSError:
        return
    for owner in owners:
        if owner.name.casefold() in BACKUP_DIRECTORY_NAMES:
            continue
        if owner.name.casefold() == "cluster":
            continue  # already yielded above
        yield owner
        owner_cluster = owner / "Cluster"
        if owner_cluster.is_dir():
            yield owner_cluster


def is_production_spm_location(path, root):
    """True when *path* sits in a folder that can hold a shipping SPM."""
    parent_key = canonical_path_key(Path(path).parent)
    return any(
        canonical_path_key(folder) == parent_key
        for folder in production_spm_folders(root)
    )


def _candidate_contract_paths():
    override = os.environ.get("SUBSTANCE_TOOLS_PIPELINE_CONTRACT")
    if override:
        yield Path(override)

    here = Path(__file__).resolve()
    for parent in here.parents:
        yield parent / "pipeline_contract.json"
        yield parent / "substance-tools" / "pipeline_contract.json"
        yield parent / "substance_tools" / "pipeline_contract.json"

    appdata = os.environ.get("APPDATA")
    if appdata:
        blender_root = Path(appdata) / "Blender Foundation" / "Blender"
        for path in sorted(
            blender_root.glob(
                "*/scripts/addons/substance_tools/pipeline_contract.json"
            ),
            reverse=True,
        ):
            yield path


@lru_cache(maxsize=1)
def pipeline_contract_path():
    seen = set()
    for candidate in _candidate_contract_paths():
        key = canonical_path_key(candidate)
        if key in seen:
            continue
        seen.add(key)
        if candidate.is_file():
            return candidate.resolve()
    return None


@lru_cache(maxsize=1)
def shared_contract_api():
    """Dynamically load the central stdlib-only SpeedTree policy module."""
    contract_path = pipeline_contract_path()
    if contract_path is None:
        raise RuntimeError(
            "substance-tools/pipeline_contract.json could not be located"
        )
    module_path = contract_path.with_name("speedtree_handoff_contract.py")
    if not module_path.is_file():
        raise RuntimeError(
            f"central SpeedTree handoff API is missing: {module_path}"
        )

    module_name = "_speedtree_batch_shared_handoff_contract"
    loaded = sys.modules.get(module_name)
    if (
        loaded is not None
        and getattr(loaded, "__file__", None)
        and Path(loaded.__file__).resolve() == module_path.resolve()
    ):
        return loaded
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(
            f"central SpeedTree handoff API could not be loaded: {module_path}"
        )
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(module_name, None)
        raise

    required = (
        "build_material_intent",
        "build_sidecar_descriptor",
        "contract_descriptor",
        "dynamic_wind_rules",
        "normalize_instance_profile",
        "validate_material_intent",
        "validate_material_intent_for_name",
        "validate_preflight_envelope",
        "validate_sidecar_descriptor",
    )
    missing = [name for name in required if not callable(getattr(module, name, None))]
    if missing:
        sys.modules.pop(module_name, None)
        raise RuntimeError(
            "central SpeedTree handoff API is incomplete: " + ", ".join(missing)
        )
    return module


def clear_contract_caches():
    """Test/install helper for switching an explicit central contract path."""
    pipeline_contract_path.cache_clear()
    shared_contract_api.cache_clear()
    sys.modules.pop("_speedtree_batch_shared_handoff_contract", None)


@contextlib.contextmanager
def open_spm_binary(path):
    """Open a plain-XML or gzip-compressed SPM as a binary XML stream."""
    candidate = _canonical_path(path)
    raw = candidate.open("rb")
    try:
        magic = raw.read(2)
        raw.seek(0)
        if magic == b"\x1f\x8b":
            with gzip.GzipFile(fileobj=raw, mode="rb") as decoded:
                yield decoded
        else:
            yield raw
    finally:
        raw.close()


def read_spm_text(path):
    """Read either supported SPM container through one shared decoder."""
    with open_spm_binary(path) as handle:
        return handle.read().decode("utf-8", errors="replace")


def _local_xml_tag(tag):
    return str(tag).rsplit("}", 1)[-1]


def _is_material_slot_property(name):
    folded = str(name or "").strip().casefold()
    return (
        folded.endswith(":material")
        or folded.startswith("materials:")
        or folded
        in {
            "material:frond",
            "mesh:material",
            "mesh:render material",
        }
    )


def _material_slot_assignment_state(raw_value):
    value = str(raw_value or "").strip()
    if not value or value.casefold() in {"none", "null", "unassigned"}:
        return "UNASSIGNED"
    try:
        if float(value) < 0:
            return "UNASSIGNED"
    except ValueError:
        pass
    return "ASSIGNED"


def _remove_non_structural_spm_content(
    parent,
    *,
    ignore_vertex_color_spline_properties=False,
):
    """Remove mutable shading metadata while retaining physical structure."""
    for child in list(parent):
        tag = _local_xml_tag(child.tag)
        remove = tag in _SPM_SEMANTIC_IGNORED_SUBTREE_TAGS
        if tag == "Material_v8":
            cutout = child.findtext("CutoutMeshID")
            supplemental = child.find("SupplementalCutoutMeshIDs")
            uv_areas = child.find("UVAreas")
            has_geometry = (
                str(cutout or "").strip() not in {"", "-1"}
                or (
                    supplemental is not None
                    and str(supplemental.get("Count") or "0") != "0"
                )
                or (
                    uv_areas is not None
                    and str(uv_areas.get("Count") or "0") != "0"
                )
            )
            if not has_geometry:
                parent.remove(child)
                continue
            child.attrib.pop("Name", None)
            for material_child in list(child):
                if (
                    _local_xml_tag(material_child.tag)
                    not in _SPM_SEMANTIC_MATERIAL_GEOMETRY_TAGS
                ):
                    child.remove(material_child)
            continue
        if tag == "Property" or (
            ignore_vertex_color_spline_properties
            and tag == "SplineProperty"
        ):
            name = str(child.findtext("Name") or "").strip().casefold()
            remove = name.startswith("vertex color:")
            if not remove and _is_material_slot_property(name):
                value = child.find("Value")
                if value is not None:
                    value.text = _material_slot_assignment_state(value.text)
        if remove:
            parent.remove(child)
            continue
        _remove_non_structural_spm_content(
            child,
            ignore_vertex_color_spline_properties=(
                ignore_vertex_color_spline_properties
            ),
        )
        if (
            _local_xml_tag(child.tag) == "Assets"
            and not list(child)
            and not str(child.text or "").strip()
        ):
            parent.remove(child)


def spm_structural_semantic_fingerprint(
    source_text,
    *,
    context=None,
    ignore_vertex_color_spline_properties=False,
    projection_version=None,
):
    """Hash bone/geometry/cutout/generator semantics, not shading metadata."""
    # ``root`` is a fresh parse owned by this function. Mutating it directly
    # produces the same projection while avoiding a second full in-memory XML
    # tree; production SPMs can otherwise spend several seconds just copying
    # data before every compatibility check.
    projected = ET.fromstring(source_text)
    _remove_non_structural_spm_content(
        projected,
        ignore_vertex_color_spline_properties=(
            ignore_vertex_color_spline_properties
        ),
    )
    payload = ET.tostring(
        projected,
        encoding="unicode",
        short_empty_elements=True,
    )
    payload = re.sub(r">\s+<", "><", payload).strip()
    envelope = {
        "projection_version": (
            SPM_STRUCTURAL_SEMANTIC_PROJECTION_VERSION
            if projection_version is None
            else projection_version
        ),
        "source": payload,
        "context": context or {},
    }
    encoded = json.dumps(
        envelope,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.blake2b(encoded, digest_size=16).hexdigest()


@lru_cache(maxsize=512)
def _spm_file_structural_semantic_fingerprint_cached(
    path_text,
    raw_sha256,
    context_json,
):
    del raw_sha256
    return spm_structural_semantic_fingerprint(
        read_spm_text(path_text),
        context=json.loads(context_json),
    )


def spm_file_structural_semantic_fingerprint(
    path,
    *,
    context=None,
    raw_sha256=None,
):
    """Hash one SPM projection once per exact raw byte identity."""
    candidate = _canonical_path(path)
    raw_identity = str(raw_sha256 or sha256_file(candidate)).casefold()
    context_json = json.dumps(
        context or {},
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return _spm_file_structural_semantic_fingerprint_cached(
        str(candidate),
        raw_identity,
        context_json,
    )


def spm_container_format(path):
    with _canonical_path(path).open("rb") as handle:
        return "gzip" if handle.read(2) == b"\x1f\x8b" else "plain_xml"


def _local_name(tag):
    return str(tag or "").rsplit("}", 1)[-1]


def _attribute(node, name):
    expected = str(name).casefold()
    for key, value in node.attrib.items():
        if _local_name(key).casefold() == expected:
            return str(value or "").strip()
    return ""


def _child_text(node, name):
    expected = str(name).casefold()
    for child in node:
        if _local_name(child.tag).casefold() == expected:
            return "".join(child.itertext()).strip()
    return ""


def read_tree_user_data(path):
    """Read Tree Generator > SpeedTree SDK > User data from SpeedTree 10.1."""
    values = []
    with open_spm_binary(path) as handle:
        for _event, element in ET.iterparse(handle, events=("end",)):
            if _local_name(element.tag).casefold() != "generator":
                continue
            if _attribute(element, "Type").casefold() != "tree":
                element.clear()
                continue
            for property_node in element.iter():
                if _local_name(property_node.tag).casefold() != "property":
                    continue
                if _child_text(property_node, "Name").casefold() != (
                    TREE_USER_DATA_PROPERTY.casefold()
                ):
                    continue
                values.append(_child_text(property_node, "Value"))
            element.clear()
            break
    distinct = []
    for value in values:
        if value not in distinct:
            distinct.append(value)
    if len(distinct) > 1:
        raise ValueError(
            "Tree Generator contains conflicting SpeedTree SDK User data values"
        )
    return distinct[0] if distinct else ""


def read_tree_instance_profile(path):
    """Return the validated literal profile; never infer it from material names."""
    return shared_contract_api().normalize_instance_profile(read_tree_user_data(path))


def inspect_tree_instance_profile(path):
    try:
        raw = read_tree_user_data(path)
    except Exception as exc:
        return {
            "property": TREE_USER_DATA_PROPERTY,
            "raw": "",
            "normalized": "",
            "status": "invalid",
            "error": str(exc),
        }
    try:
        normalized = shared_contract_api().normalize_instance_profile(raw)
    except Exception as exc:
        return {
            "property": TREE_USER_DATA_PROPERTY,
            "raw": raw,
            "normalized": "",
            "status": "invalid",
            "error": str(exc),
        }
    return {
        "property": TREE_USER_DATA_PROPERTY,
        "raw": raw,
        "normalized": normalized,
        "status": "ok",
    }


def sha256_file(path):
    hasher = hashlib.sha256()
    with _canonical_path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _path_is_within(path, root):
    candidate = _canonical_path(path)
    parent = _canonical_path(root)
    try:
        candidate.relative_to(parent)
        return True
    except ValueError:
        return False


def prove_legacy_texture_normalize_semantic_migration(
    source_spm,
    recorded_raw_sha256,
):
    """Prove one legacy raw-hash drift came from declared texture normalization.

    A legacy physical receipt has no recorded semantic fingerprint.  Raw drift
    is accepted only when an ``ok`` texture-normalization receipt names the
    current SPM, its declared backup contains the exact recorded bytes, and the
    old/current structural projections are identical.  Unknown drift remains
    fail-closed.
    """
    source = _canonical_path(source_spm)
    recorded = str(recorded_raw_sha256 or "").strip().casefold()
    if not source.is_file() or not recorded:
        return None
    current_raw = sha256_file(source).casefold()
    current_semantic = spm_file_structural_semantic_fingerprint(
        source,
        raw_sha256=current_raw,
    )
    base = {
        "source_spm": str(source),
        "recorded_source_spm_sha256": recorded,
        "current_source_spm_sha256": current_raw,
        "source_spm_semantic_projection_version":
            SPM_STRUCTURAL_SEMANTIC_PROJECTION_VERSION,
        "source_spm_semantic_fingerprint": current_semantic,
    }
    if current_raw == recorded:
        return {
            **base,
            "status": "legacy_raw_exact",
            "raw_sha256_drift": False,
        }

    reports_dir = source.parent / "reports"
    backup_root = reports_dir / "texture_normalize_backups"
    if not reports_dir.is_dir() or not backup_root.is_dir():
        return None
    source_key = canonical_path_key(source)
    for receipt_path in sorted(reports_dir.glob("*.json")):
        try:
            payload = json.loads(receipt_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        normalization = payload.get("normalization")
        if (
            payload.get("status") != "ok"
            or not isinstance(normalization, dict)
            or source_key
            not in {
                canonical_path_key(value)
                for value in normalization.get("spms") or []
            }
        ):
            continue
        declared_backup_dir = normalization.get("backup_dir")
        if not declared_backup_dir:
            continue
        backup_dir = _canonical_path(declared_backup_dir)
        if (
            not backup_dir.is_dir()
            or not _path_is_within(backup_dir, backup_root)
            or not backup_dir.name.casefold().startswith(
                "texture_normalize_"
            )
        ):
            continue
        suffix = "_" + source.name.casefold()
        for backup in sorted(backup_dir.iterdir()):
            if (
                not backup.is_file()
                or backup.suffix.casefold() != ".spm"
                or (
                    backup.name.casefold() != source.name.casefold()
                    and not backup.name.casefold().endswith(suffix)
                )
            ):
                continue
            try:
                backup_raw = sha256_file(backup).casefold()
                backup_semantic = (
                    spm_file_structural_semantic_fingerprint(
                        backup,
                        raw_sha256=backup_raw,
                    )
                )
            except (OSError, ET.ParseError, ValueError):
                continue
            if backup_raw != recorded or backup_semantic != current_semantic:
                continue
            return {
                **base,
                "status": "legacy_texture_normalize_migrated",
                "raw_sha256_drift": True,
                "provenance_receipt": source_identity(receipt_path),
                "recorded_source_backup": source_identity(backup),
            }
    return None


def source_identity(path, required=True):
    """Hash one stable source snapshot using SHA-256."""
    candidate = _canonical_path(path)
    if not candidate.is_file():
        if required:
            raise FileNotFoundError(f"SpeedTree source is missing: {candidate}")
        return {"canonical_path": str(candidate), "missing": True}
    for _attempt in range(2):
        before = candidate.stat()
        digest = sha256_file(candidate)
        after = candidate.stat()
        if (before.st_size, before.st_mtime_ns) == (
            after.st_size,
            after.st_mtime_ns,
        ):
            return {
                "canonical_path": str(candidate),
                "sha256": digest,
                "size": after.st_size,
                "mtime_ns": after.st_mtime_ns,
            }
    raise RuntimeError(f"SpeedTree source changed while hashing: {candidate}")


SOURCE_FINGERPRINT_POLICY = "canonical_path_sha256_size_v1"


def _content_identity(value):
    if isinstance(value, dict):
        return {
            key: _content_identity(item)
            for key, item in sorted(value.items())
            if key != "mtime_ns"
        }
    if isinstance(value, list):
        return [_content_identity(item) for item in value]
    return value


def source_snapshot_fingerprint(source):
    encoded = json.dumps(
        source, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def source_set_fingerprint(source):
    """Hash stable source content identity; mtime remains diagnostic only."""
    encoded = json.dumps(
        _content_identity(source),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def speedtree_stmat_path(spm_path):
    spm = _canonical_path(spm_path)
    return spm.parent / "fbx" / f"{spm.stem}.stmat"


def dynamic_wind_path(spm_path):
    spm = _canonical_path(spm_path)
    rules = shared_contract_api().dynamic_wind_rules()
    suffix = str(
        rules.get("filename_suffix")
        or "_dynamic_wind_import_from_megaplant_groups.json"
    )
    return spm.parent / "JSON" / f"{spm.stem}{suffix}"


def _stmat_material_rows(stmat_path):
    candidate = _canonical_path(stmat_path)
    if not candidate.is_file():
        return []
    root = ET.parse(candidate).getroot()
    rows = []
    for node in root.iter():
        if _local_name(node.tag).casefold() != "material":
            continue
        name = _attribute(node, "Name")
        if not name:
            continue
        rows.append(
            {
                "stmat_material_index": len(rows),
                "stmat_material_id": _attribute(node, "ID"),
                "material_name": name,
            }
        )
    return rows


def _binding_summary(binding):
    # Preserve the established resolve_texture_bindings record losslessly.
    # BWR still consumes stmat_roles/material identity fields, while the outer
    # intent index remains the authoritative duplicate-safe join key.
    return copy.deepcopy(binding)


def _texture_source_mode(binding):
    status = str((binding or {}).get("status") or "")
    if status == "ok":
        return "managed_texture_set"
    if status == "not_managed":
        return "preserve_declared_sources"
    return "unresolved"


def build_stmat_material_intents(
    stmat_path, instance_profile="", texture_readiness=None
):
    """Build duplicate-safe, index-addressed material intents for one STMAT."""
    api = shared_contract_api()
    normalized_profile = api.normalize_instance_profile(instance_profile)
    by_index = {
        int(item.get("material_index")): item
        for item in ((texture_readiness or {}).get("bindings") or [])
        if item.get("material_index") is not None
    }
    result = []
    for row in _stmat_material_rows(stmat_path):
        intent = api.build_material_intent(
            row["material_name"], instance_profile=normalized_profile
        )
        api.validate_material_intent(intent)
        binding = by_index.get(row["stmat_material_index"], {})
        result.append(
            {
                **row,
                **intent,
                "texture_source_mode": _texture_source_mode(binding),
                "texture_binding": _binding_summary(binding),
            }
        )
    return result


def naming_shadow_issue(
    material_name, *, production_canonical_name, workflow_canonical_name, workflow
):
    """Expose a legacy naming disagreement without changing either mutator."""
    production = str(production_canonical_name or "").strip()
    workflow_name = str(workflow_canonical_name or "").strip()
    if production.casefold() == workflow_name.casefold():
        return None
    return {
        "code": "MATERIAL_CANONICAL_NAME_DIVERGENCE",
        "severity": "warning",
        "scope": "material_name",
        "entity": str(material_name or ""),
        "message": (
            f"PCG production canonical name {production!r} differs from "
            f"{workflow} canonical name {workflow_name!r}"
        ),
        "details": {
            "production_canonical_name": production,
            "workflow": str(workflow or ""),
            "workflow_canonical_name": workflow_name,
        },
    }


def build_preflight_envelope(
    spm_path,
    stmat_path=None,
    *,
    outcome,
    texture_readiness=None,
    issues=None,
):
    """Build the versioned handoff envelope while preserving legacy reports."""
    spm = _canonical_path(spm_path)
    stmat = _canonical_path(stmat_path or speedtree_stmat_path(spm))
    profile = inspect_tree_instance_profile(spm)
    normalized_profile = profile.get("normalized", "")
    source = {
        "spm": source_identity(spm),
        "stmat": [source_identity(stmat)] if stmat.is_file() else [],
    }
    api = shared_contract_api()
    descriptor = api.build_sidecar_descriptor(spm.stem, source=source)
    api.validate_sidecar_descriptor(descriptor, spm.stem)
    envelope_issues = [copy.deepcopy(item) for item in (issues or [])]
    material_intents = []
    if profile.get("status") == "ok" and stmat.is_file():
        try:
            material_intents = build_stmat_material_intents(
                stmat,
                normalized_profile,
                texture_readiness=texture_readiness,
            )
        except (OSError, ET.ParseError, ValueError) as exc:
            envelope_issues.append(
                {
                    "code": "MATERIAL_INTENT_PARSE_ERROR",
                    "severity": "error",
                    "scope": "stmat",
                    "entity": stmat.name,
                    "message": str(exc),
                }
            )
    if profile.get("status") != "ok":
        envelope_issues.append(
            {
                "code": "INSTANCE_PROFILE_INVALID",
                "severity": "error",
                "scope": "spm",
                "entity": spm.name,
                "message": str(profile.get("error") or "invalid Tree User data"),
            }
        )
    envelope = {
        "kind": PREFLIGHT_CONTRACT_KIND,
        "schema_version": PREFLIGHT_SCHEMA_VERSION,
        "speedtree_handoff_contract": descriptor,
        "outcome": str(outcome),
        "source": source,
        "source_fingerprint": source_set_fingerprint(source),
        "source_fingerprint_policy": SOURCE_FINGERPRINT_POLICY,
        "source_snapshot_fingerprint": source_snapshot_fingerprint(source),
        "instance_profile": normalized_profile,
        "tree_user_data": profile,
        "material_intents": material_intents,
        "dynamic_wind": {
            "path": canonical_path(dynamic_wind_path(spm)),
            "rules": api.dynamic_wind_rules(),
        },
        "issues": envelope_issues,
    }
    api.validate_preflight_envelope(envelope, expected_mesh_name=spm.stem)
    return envelope


def _same_source_identity(recorded, current):
    if bool(recorded.get("missing")) != bool(current.get("missing")):
        return False
    if canonical_path_key(recorded.get("canonical_path")) != canonical_path_key(
        current.get("canonical_path")
    ):
        return False
    if current.get("missing"):
        return True
    return (
        str(recorded.get("sha256") or "").casefold()
        == str(current.get("sha256") or "").casefold()
        and recorded.get("size") == current.get("size")
    )


def validate_preflight_envelope(envelope, spm_path, require_ok=True):
    """Reject wrong-model, stale, malformed, or central-contract-mismatched data."""
    if not isinstance(envelope, dict):
        raise ValueError("SpeedTree pipeline contract is not an object")
    if envelope.get("kind") != PREFLIGHT_CONTRACT_KIND:
        raise ValueError(f"unexpected SpeedTree contract kind: {envelope.get('kind')!r}")
    if int(envelope.get("schema_version", 0)) != PREFLIGHT_SCHEMA_VERSION:
        raise ValueError(
            "SpeedTree preflight schema mismatch: "
            f"{envelope.get('schema_version')} != {PREFLIGHT_SCHEMA_VERSION}"
        )
    if require_ok and envelope.get("outcome") != "ok":
        raise ValueError(
            f"SpeedTree preflight outcome is not ok: {envelope.get('outcome')!r}"
        )

    spm = _canonical_path(spm_path)
    source = envelope.get("source") or {}
    recorded_spm = source.get("spm") or {}
    current_spm = source_identity(spm)
    if not _same_source_identity(recorded_spm, current_spm):
        raise ValueError("SpeedTree preflight SPM source is stale or belongs to another model")

    recorded_stmats = source.get("stmat") or []
    current_stmat_path = speedtree_stmat_path(spm)
    current_stmats = (
        [source_identity(current_stmat_path)] if current_stmat_path.is_file() else []
    )
    if len(recorded_stmats) != len(current_stmats) or any(
        not _same_source_identity(recorded, current)
        for recorded, current in zip(recorded_stmats, current_stmats)
    ):
        raise ValueError(
            "SpeedTree preflight STMAT source is stale or belongs to another model"
        )
    recorded_fingerprint = envelope.get("source_fingerprint")
    fingerprint_policy = envelope.get("source_fingerprint_policy")
    if fingerprint_policy not in (None, "", SOURCE_FINGERPRINT_POLICY):
        raise ValueError(
            "unsupported SpeedTree preflight source fingerprint policy: "
            + str(fingerprint_policy)
        )
    current_content_fingerprint = source_set_fingerprint(source)
    legacy_snapshot_fingerprint = source_snapshot_fingerprint(source)
    if (
        recorded_fingerprint != current_content_fingerprint
        and not (
            not fingerprint_policy
            and recorded_fingerprint == legacy_snapshot_fingerprint
        )
    ):
        raise ValueError("SpeedTree preflight source fingerprint mismatch")
    recorded_snapshot = envelope.get("source_snapshot_fingerprint")
    if (
        recorded_snapshot is not None
        and recorded_snapshot != legacy_snapshot_fingerprint
    ):
        raise ValueError("SpeedTree preflight source snapshot fingerprint mismatch")

    api = shared_contract_api()
    api.validate_preflight_envelope(envelope, expected_mesh_name=spm.stem)
    descriptor = envelope.get("speedtree_handoff_contract")
    api.validate_sidecar_descriptor(descriptor, spm.stem)
    current_profile = read_tree_instance_profile(spm)
    recorded_profile = api.normalize_instance_profile(
        envelope.get("instance_profile", "")
    )
    if current_profile != recorded_profile:
        raise ValueError("SpeedTree Tree User data instance profile is stale")
    for row in envelope.get("material_intents") or []:
        api.validate_material_intent(row)
    expected_wind_path = canonical_path_key(dynamic_wind_path(spm))
    recorded_wind_path = canonical_path_key(
        (envelope.get("dynamic_wind") or {}).get("path", "")
    )
    if expected_wind_path != recorded_wind_path:
        raise ValueError("SpeedTree dynamic-wind path contract mismatch")
    return copy.deepcopy(envelope)


def validate_preflight_report(report_path, spm_path, require_ok=True):
    path = _canonical_path(report_path)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"SpeedTree preflight report could not be read: {path} ({exc})") from exc
    if not isinstance(payload, dict):
        raise ValueError("SpeedTree preflight report is not an object")
    if require_ok and payload.get("status") != "ok":
        raise ValueError(
            f"SpeedTree material preflight status is not ok: {payload.get('status')!r}"
        )
    validate_preflight_envelope(
        payload.get("speedtree_pipeline_contract"),
        spm_path,
        require_ok=require_ok,
    )
    return payload


__all__ = [
    "BACKUP_DIRECTORY_NAMES",
    "PREFLIGHT_CONTRACT_KIND",
    "PREFLIGHT_SCHEMA_VERSION",
    "SPM_STRUCTURAL_SEMANTIC_PROJECTION_VERSION",
    "TREE_USER_DATA_PROPERTY",
    "build_preflight_envelope",
    "build_stmat_material_intents",
    "canonical_path",
    "canonical_path_key",
    "clear_contract_caches",
    "dynamic_wind_path",
    "inspect_tree_instance_profile",
    "is_live_spm",
    "is_production_spm_location",
    "naming_shadow_issue",
    "production_spm_folders",
    "prove_legacy_texture_normalize_semantic_migration",
    "open_spm_binary",
    "pipeline_contract_path",
    "read_spm_text",
    "read_tree_instance_profile",
    "read_tree_user_data",
    "sha256_file",
    "shared_contract_api",
    "source_identity",
    "source_set_fingerprint",
    "speedtree_stmat_path",
    "spm_container_format",
    "spm_file_structural_semantic_fingerprint",
    "spm_structural_semantic_fingerprint",
    "validate_preflight_envelope",
    "validate_preflight_report",
]
