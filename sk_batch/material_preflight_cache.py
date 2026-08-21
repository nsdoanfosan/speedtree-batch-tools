"""Durable reuse for successful SpeedTree material preflight reports.

The material preflight is intentionally expensive: it may export FBX/STMAT,
inspect the live SPM and resolve the current Cluster handoff.  A later Blender
Assembly failure must not make that already successful inspection disappear.
This module keeps one content-bound report per exact canonical/source SPM pair.

A cache miss is only permission to run the existing preflight again.  It is
never an asset failure and never relaxes the validation performed by Blender
Assembly when it consumes the report.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import uuid
from pathlib import Path

from speedtree_pipeline_contract import validate_preflight_envelope


MATERIAL_PREFLIGHT_CACHE_SCHEMA_VERSION = 1
MATERIAL_PREFLIGHT_CACHE_CONTRACT_VERSION = 1
_HASH_CHUNK_SIZE = 4 * 1024 * 1024


def _canonical_path(path):
    return str(Path(path).expanduser().resolve())


def _path_key(path):
    return os.path.normcase(_canonical_path(path)).casefold()


def _sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while True:
            chunk = handle.read(_HASH_CHUNK_SIZE)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_write_json(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{uuid.uuid4().hex}")
    try:
        temporary.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def semantic_file_identity(path, *, path_sensitive=False, hash_content=True):
    """Describe a runtime input by behavior, not by checkout location.

    FBX option files and Python helpers can exist in multiple worktrees while
    containing identical bytes.  Their checkout path and mtime do not change
    export behavior, so only content participates unless the caller explicitly
    marks a binary installation as path-sensitive.
    """
    candidate = Path(path).expanduser().resolve()
    stat = candidate.stat()
    identity = {"size": int(stat.st_size)}
    if path_sensitive:
        identity.update(
            path=str(candidate),
            mtime_ns=int(stat.st_mtime_ns),
        )
    if hash_content:
        identity["sha256"] = _sha256_file(candidate)
    return identity


def material_preflight_runtime_signature(
    *,
    semantic_files,
    speedtree_exe,
):
    """Fingerprint only inputs that can change preflight semantics."""
    payload = {
        "contract_version": MATERIAL_PREFLIGHT_CACHE_CONTRACT_VERSION,
        "semantic_files": [
            semantic_file_identity(path) for path in semantic_files
        ],
        # Hashing the 35+ MB executable on every GUI start adds no useful
        # precision.  Its installed path/stat identity changes on replacement.
        "speedtree_exe": semantic_file_identity(
            speedtree_exe,
            path_sensitive=True,
            hash_content=False,
        ),
    }
    encoded = json.dumps(
        payload,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _cache_key(canonical_spm, speedtree_spm):
    payload = {
        "canonical_spm": _path_key(canonical_spm),
        "speedtree_spm": _path_key(speedtree_spm),
    }
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:32]


def material_preflight_cache_paths(cache_dir, canonical_spm, speedtree_spm):
    root = Path(cache_dir).expanduser().resolve()
    key = _cache_key(canonical_spm, speedtree_spm)
    return (
        root / f"{key}.material.json",
        root / f"{key}.receipt.json",
    )


def _report_paths_match(report, canonical_spm, speedtree_spm):
    try:
        return bool(
            _path_key(report.get("spm") or "") == _path_key(canonical_spm)
            and _path_key(report.get("speedtree_spm") or "")
            == _path_key(speedtree_spm)
        )
    except (OSError, ValueError):
        return False


def _export_artifacts_current(report):
    export = report.get("speedtree_export") or {}
    target = Path(str(export.get("path") or ""))
    artifacts = export.get("artifacts")
    if not target.is_absolute() or not isinstance(artifacts, list) or not artifacts:
        return False
    root = target.parent
    for record in artifacts:
        try:
            relative = Path(str(record["relative_path"]))
            if relative.is_absolute() or ".." in relative.parts:
                return False
            path = root / relative
            stat = path.stat()
            if not path.is_file() or stat.st_size != int(record["size"]):
                return False
            if stat.st_mtime_ns == int(record.get("mtime_ns", -1)):
                continue
            if _sha256_file(path) != str(record.get("sha256") or ""):
                return False
        except (KeyError, OSError, TypeError, ValueError):
            return False
    return True


def _reusable_report(report, canonical_spm, speedtree_spm):
    if not isinstance(report, dict) or report.get("status") != "ok":
        raise ValueError("cached material preflight is not successful")
    if not _report_paths_match(report, canonical_spm, speedtree_spm):
        raise ValueError("cached material preflight belongs to another target")
    validate_preflight_envelope(
        report.get("speedtree_pipeline_contract"),
        speedtree_spm,
        require_ok=True,
    )
    if not _export_artifacts_current(report):
        raise ValueError("cached material preflight export artifacts changed")
    reusable = copy.deepcopy(report)
    persistence = reusable.get("cluster_assembly_receipt_persistence")
    if isinstance(persistence, dict):
        # A run-specific live-audit marker must not remain authoritative across
        # jobs.  Blender Assembly will resolve the hash-current persisted receipt
        # (or the existing exact recovery route) for this new run.
        persistence = copy.deepcopy(persistence)
        persistence["live_audit_complete"] = False
        persistence["cache_reused"] = True
        reusable["cluster_assembly_receipt_persistence"] = persistence
    reusable["material_preflight_cache"] = {
        "status": "reused",
        "schema_version": MATERIAL_PREFLIGHT_CACHE_SCHEMA_VERSION,
        "contract_version": MATERIAL_PREFLIGHT_CACHE_CONTRACT_VERSION,
    }
    return reusable


def load_material_preflight_cache(
    cache_dir,
    canonical_spm,
    speedtree_spm,
    *,
    runtime_signature,
):
    """Return a validated reusable report, or ``None`` on an ordinary miss."""
    report_path, receipt_path = material_preflight_cache_paths(
        cache_dir, canonical_spm, speedtree_spm
    )
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        if not isinstance(receipt, dict):
            return None
        if (
            receipt.get("schema_version")
            != MATERIAL_PREFLIGHT_CACHE_SCHEMA_VERSION
            or receipt.get("contract_version")
            != MATERIAL_PREFLIGHT_CACHE_CONTRACT_VERSION
            or receipt.get("runtime_signature") != runtime_signature
            or _path_key(receipt.get("canonical_spm") or "")
            != _path_key(canonical_spm)
            or _path_key(receipt.get("speedtree_spm") or "")
            != _path_key(speedtree_spm)
        ):
            return None
        if _sha256_file(report_path) != str(receipt.get("report_sha256") or ""):
            return None
        report = json.loads(report_path.read_text(encoding="utf-8"))
        reusable = _reusable_report(
            report, canonical_spm, speedtree_spm
        )
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return None
    return {
        "report": reusable,
        "report_path": report_path,
        "receipt_path": receipt_path,
    }


def store_material_preflight_cache(
    cache_dir,
    canonical_spm,
    speedtree_spm,
    report,
    *,
    runtime_signature,
):
    """Persist one successful exact report after validating its live source."""
    reusable = _reusable_report(report, canonical_spm, speedtree_spm)
    report_path, receipt_path = material_preflight_cache_paths(
        cache_dir, canonical_spm, speedtree_spm
    )
    _atomic_write_json(report_path, reusable)
    receipt = {
        "schema_version": MATERIAL_PREFLIGHT_CACHE_SCHEMA_VERSION,
        "contract_version": MATERIAL_PREFLIGHT_CACHE_CONTRACT_VERSION,
        "canonical_spm": _canonical_path(canonical_spm),
        "speedtree_spm": _canonical_path(speedtree_spm),
        "runtime_signature": runtime_signature,
        "report_path": str(report_path),
        "report_sha256": _sha256_file(report_path),
    }
    _atomic_write_json(receipt_path, receipt)
    return {
        "report_path": report_path,
        "receipt_path": receipt_path,
    }
