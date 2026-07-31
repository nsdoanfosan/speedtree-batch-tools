"""Durable, content-addressed positive receipts for SPM bone calibration.

The GUI state is a convenience cache and may be deleted or malformed.  These
small per-SPM receipts live in one central cache directory so a lost global
state file does not schedule every SpeedTree XML/FBX export again.

Only a previously successful calibration is reusable.  Missing, malformed or
stale receipts are ordinary cache misses and never become data errors.
"""
import hashlib
import json
import os
import sys
from datetime import datetime
from pathlib import Path

try:
    from speedtree_pipeline_contract import (
        SPM_STRUCTURAL_SEMANTIC_PROJECTION_VERSION,
        spm_structural_semantic_fingerprint,
    )
except ImportError:
    REPO_DIR = Path(__file__).resolve().parent.parent
    if str(REPO_DIR) not in sys.path:
        sys.path.insert(0, str(REPO_DIR))
    from speedtree_pipeline_contract import (
        SPM_STRUCTURAL_SEMANTIC_PROJECTION_VERSION,
        spm_structural_semantic_fingerprint,
    )


SPM_CALIBRATION_RECEIPT_VERSION = 1
# v1 inherited the shared structural projection, which ignored scalar
# ``Property`` vertex-color values but accidentally retained the real
# ``SplineProperty`` values used by SpeedTree.  Those values cannot change
# skeleton output, so v2 removes both forms without changing the shared
# structural projection used by Cluster relationship receipts.
SPM_BONE_SEMANTIC_PROJECTION_VERSION = 2
POSITIVE_CALIBRATION_STATUSES = frozenset({"calibrated", "already-ok"})


def bone_semantic_fingerprint(source_text, *, context=None):
    return spm_structural_semantic_fingerprint(
        source_text,
        context=context,
        ignore_vertex_color_spline_properties=True,
        projection_version=SPM_BONE_SEMANTIC_PROJECTION_VERSION,
    )


def legacy_bone_semantic_fingerprint(source_text, *, context=None):
    """Return the v1 fingerprint so current positive receipts migrate once."""
    return spm_structural_semantic_fingerprint(
        source_text,
        context=context,
    )


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
    legacy_bone_semantic_fingerprint_values=(),
):
    """Return a current positive receipt, otherwise ``None``."""
    path = calibration_receipt_path(spm_path, cache_dir)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    stored_fingerprint = payload.get("bone_semantic_fingerprint")
    stored_projection = payload.get("bone_semantic_projection_version")
    current_fingerprint_match = (
        stored_fingerprint == bone_semantic_fingerprint_value
        and stored_projection == SPM_BONE_SEMANTIC_PROJECTION_VERSION
    )
    legacy_fingerprint_match = (
        stored_projection == SPM_STRUCTURAL_SEMANTIC_PROJECTION_VERSION
        and stored_fingerprint
        in frozenset(legacy_bone_semantic_fingerprint_values or ())
    )
    if (
        payload.get("kind") != "spm_bone_calibration_positive_receipt"
        or payload.get("version") != SPM_CALIBRATION_RECEIPT_VERSION
        or payload.get("status") not in POSITIVE_CALIBRATION_STATUSES
        or payload.get("spm_identity") != normalized_spm_identity(spm_path)
        or not (current_fingerprint_match or legacy_fingerprint_match)
        or payload.get("settings_signature") != settings_signature
        or payload.get("bone_contract_version") != bone_contract_version
    ):
        return None
    if legacy_fingerprint_match:
        payload = dict(payload)
        payload["legacy_bone_semantic_receipt_migrated"] = True
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
    "legacy_bone_semantic_fingerprint",
    "load_positive_calibration_receipt",
    "normalized_spm_identity",
    "write_positive_calibration_receipt",
]
