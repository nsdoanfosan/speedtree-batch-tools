"""Durable, content-addressed positive receipts for SPM bone calibration.

The GUI state is a convenience cache and may be deleted or malformed.  These
small per-SPM receipts live in one central cache directory so a lost global
state file does not schedule every SpeedTree XML/FBX export again.

Only a previously successful calibration is reusable.  Missing, malformed or
stale receipts are ordinary cache misses and never become data errors.
"""
import copy
import hashlib
import json
import os
import re
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path


SPM_CALIBRATION_RECEIPT_VERSION = 1
SPM_BONE_SEMANTIC_PROJECTION_VERSION = 1
POSITIVE_CALIBRATION_STATUSES = frozenset({"calibrated", "already-ok"})

_IGNORED_SUBTREE_TAGS = frozenset({"Thumbnail", "Preview"})
_MATERIAL_GEOMETRY_TAGS = frozenset({
    "CutoutMeshID",
    "SupplementalCutoutMeshIDs",
    "UVAreas",
    "Width",
    "Height",
    "UnwrapScale",
    "AtlasMaker",
})


def _local_tag(tag):
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


def _remove_non_bone_content(parent):
    """Remove material/preview data and vertex-colour authoring in place."""
    for child in list(parent):
        tag = _local_tag(child.tag)
        remove = tag in _IGNORED_SUBTREE_TAGS
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
                if _local_tag(material_child.tag) not in _MATERIAL_GEOMETRY_TAGS:
                    child.remove(material_child)
            continue
        if tag == "Property":
            name = str(child.findtext("Name") or "").strip().casefold()
            remove = name.startswith("vertex color:")
            if not remove and _is_material_slot_property(name):
                value = child.find("Value")
                if value is not None:
                    value.text = _material_slot_assignment_state(value.text)
        if remove:
            parent.remove(child)
            continue
        _remove_non_bone_content(child)
        if (
            _local_tag(child.tag) == "Assets"
            and not list(child)
            and not str(child.text or "").strip()
        ):
            parent.remove(child)


def bone_semantic_fingerprint(source_text, *, context=None):
    """Hash only structure/geometry/bone semantics that require re-export.

    Texture paths and map contents, material names, thumbnails/previews and
    vertex-colour authoring are deliberately excluded.  Those are handled by
    their own cheap contracts and cannot change the calibrated skeleton.
    """
    root = ET.fromstring(source_text)
    projected = copy.deepcopy(root)
    _remove_non_bone_content(projected)
    payload = ET.tostring(projected, encoding="unicode", short_empty_elements=True)
    # ElementTree preserves indentation in text/tails.  Formatting-only edits
    # are not SpeedTree skeleton changes.
    payload = re.sub(r">\s+<", "><", payload).strip()
    envelope = {
        "projection_version": SPM_BONE_SEMANTIC_PROJECTION_VERSION,
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


def normalized_spm_identity(spm_path):
    return os.path.normcase(os.path.abspath(str(Path(spm_path).expanduser())))


def calibration_receipt_path(spm_path, cache_dir):
    identity = normalized_spm_identity(spm_path)
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    return Path(cache_dir) / f"{digest}.json"


def load_positive_calibration_receipt(
    spm_path,
    cache_dir,
    *,
    bone_semantic_fingerprint_value,
    settings_signature,
    bone_contract_version,
):
    """Return a current positive receipt, otherwise ``None``."""
    path = calibration_receipt_path(spm_path, cache_dir)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None
    if (
        not isinstance(payload, dict)
        or payload.get("kind") != "spm_bone_calibration_positive_receipt"
        or payload.get("version") != SPM_CALIBRATION_RECEIPT_VERSION
        or payload.get("status") not in POSITIVE_CALIBRATION_STATUSES
        or payload.get("spm_identity") != normalized_spm_identity(spm_path)
        or payload.get("bone_semantic_fingerprint")
        != bone_semantic_fingerprint_value
        or payload.get("settings_signature") != settings_signature
        or payload.get("bone_contract_version") != bone_contract_version
    ):
        return None
    return payload


def write_positive_calibration_receipt(
    spm_path,
    cache_dir,
    *,
    bone_semantic_fingerprint_value,
    settings_signature,
    bone_contract_version,
    report,
):
    """Atomically persist the reusable subset of one successful report."""
    status = str((report or {}).get("status") or "")
    if status not in POSITIVE_CALIBRATION_STATUSES:
        return None
    path = calibration_receipt_path(spm_path, cache_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "kind": "spm_bone_calibration_positive_receipt",
        "version": SPM_CALIBRATION_RECEIPT_VERSION,
        "spm_identity": normalized_spm_identity(spm_path),
        "bone_semantic_projection_version":
            SPM_BONE_SEMANTIC_PROJECTION_VERSION,
        "bone_semantic_fingerprint": bone_semantic_fingerprint_value,
        "settings_signature": settings_signature,
        "bone_contract_version": bone_contract_version,
        "status": status,
        "summary": {
            "display_summary": (report or {}).get("cached_display_summary"),
            "generators": (report or {}).get("generators") or {},
            "rounds": (report or {}).get("rounds") or [],
            "total_bones": (report or {}).get("total_bones"),
            "calibration": (report or {}).get("calibration") or {},
            "skipped": (report or {}).get("skipped") or [],
        },
        "completed_at": datetime.now().isoformat(timespec="seconds"),
    }
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()
    return path


__all__ = [
    "POSITIVE_CALIBRATION_STATUSES",
    "SPM_BONE_SEMANTIC_PROJECTION_VERSION",
    "SPM_CALIBRATION_RECEIPT_VERSION",
    "bone_semantic_fingerprint",
    "calibration_receipt_path",
    "load_positive_calibration_receipt",
    "normalized_spm_identity",
    "write_positive_calibration_receipt",
]
