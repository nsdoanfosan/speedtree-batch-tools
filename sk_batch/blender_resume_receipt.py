"""Durable, cheap identity receipts for completed Blender Repair rows.

The full live Repair audit remains the authority that creates a receipt.  A
later queue run may reuse that decision only while every file identity and
output-affecting setting bound by the audit is unchanged.  This keeps restart
planning cheap without turning a saved UI label into completion authority.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path

from artifact_content_key import sampled_file_content_snapshot


BLENDER_RESUME_RECEIPT_KIND = "sk_batch_blender_resume_receipt"
BLENDER_RESUME_RECEIPT_VERSION = 1


class BlenderResumeReceiptError(ValueError):
    """The saved receipt is malformed, stale, or belongs to another row."""


def _canonical_json_sha256(payload):
    encoded = json.dumps(
        payload,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _canonical_path(path):
    return str(Path(path).expanduser().absolute())


def _path_key(path):
    return os.path.normcase(_canonical_path(path)).casefold()


def _file_identity(path, *, include_content_key=False):
    candidate = Path(path).expanduser().absolute()
    try:
        stat = candidate.stat()
    except FileNotFoundError:
        return {
            "path": str(candidate),
            "missing": True,
        }
    if not candidate.is_file():
        raise BlenderResumeReceiptError(
            f"bound Repair artifact is not a file: {candidate}"
        )
    identity = {
        "path": str(candidate),
        "size": int(stat.st_size),
        "mtime_ns": int(stat.st_mtime_ns),
        "ctime_ns": int(stat.st_ctime_ns),
        "device": int(stat.st_dev),
        "file_id": int(stat.st_ino),
    }
    if include_content_key:
        # Core source/output files also carry a bounded content key so a
        # same-size replacement with restored timestamps still fails closed.
        identity["content_key"] = sampled_file_content_snapshot(candidate)
    return identity


def settings_signature(settings):
    if not isinstance(settings, dict):
        raise BlenderResumeReceiptError(
            "Blender resume settings must be an object"
        )
    return _canonical_json_sha256(settings)


def build_blender_resume_receipt(
    queue_spm,
    *,
    tracked_paths,
    content_key_paths=(),
    settings,
    repair_state,
):
    """Seal one already-validated current Repair decision."""

    if not isinstance(repair_state, dict) or repair_state.get("current") is not True:
        raise BlenderResumeReceiptError(
            "only a current live Repair decision can create a resume receipt"
        )
    queue_spm = _canonical_path(queue_spm)
    if not Path(queue_spm).is_file():
        raise BlenderResumeReceiptError(
            f"queue SPM is missing: {queue_spm}"
        )
    paths = {_path_key(queue_spm): queue_spm}
    for path in tracked_paths or ():
        if path:
            canonical = _canonical_path(path)
            paths[_path_key(canonical)] = canonical
    content_keys = {
        _path_key(path)
        for path in content_key_paths or ()
        if path
    }
    content_keys.add(_path_key(queue_spm))
    files = [
        _file_identity(
            paths[key],
            include_content_key=key in content_keys,
        )
        for key in sorted(paths)
    ]
    result = {
        key: copy.deepcopy(repair_state[key])
        for key in (
            "current",
            "push_ready",
            "kind",
            "reason",
            "texture_reason",
            "push_dependency_contract",
        )
        if key in repair_state
    }
    payload = {
        "kind": BLENDER_RESUME_RECEIPT_KIND,
        "schema_version": BLENDER_RESUME_RECEIPT_VERSION,
        "queue_spm": queue_spm,
        "settings_sha256": settings_signature(settings),
        "files": files,
        "repair_state": result,
    }
    payload["receipt_sha256"] = _canonical_json_sha256(payload)
    return payload


def validate_blender_resume_receipt(receipt, queue_spm, *, settings):
    """Return the saved current decision after cheap fail-closed validation."""

    if not isinstance(receipt, dict):
        raise BlenderResumeReceiptError("Blender resume receipt is missing")
    recorded_hash = str(receipt.get("receipt_sha256") or "").casefold()
    unsigned = copy.deepcopy(receipt)
    unsigned.pop("receipt_sha256", None)
    if (
        receipt.get("kind") != BLENDER_RESUME_RECEIPT_KIND
        or receipt.get("schema_version") != BLENDER_RESUME_RECEIPT_VERSION
        or len(recorded_hash) != 64
        or _canonical_json_sha256(unsigned) != recorded_hash
    ):
        raise BlenderResumeReceiptError(
            "Blender resume receipt envelope is invalid"
        )
    if _path_key(receipt.get("queue_spm") or "") != _path_key(queue_spm):
        raise BlenderResumeReceiptError(
            "Blender resume receipt belongs to another SPM"
        )
    if receipt.get("settings_sha256") != settings_signature(settings):
        raise BlenderResumeReceiptError(
            "Blender Repair output settings changed"
        )
    files = receipt.get("files")
    if not isinstance(files, list) or not files:
        raise BlenderResumeReceiptError(
            "Blender resume receipt has no bound files"
        )
    queue_key = _path_key(queue_spm)
    seen = set()
    for recorded in files:
        if not isinstance(recorded, dict) or not recorded.get("path"):
            raise BlenderResumeReceiptError(
                "Blender resume receipt contains a malformed file identity"
            )
        key = _path_key(recorded["path"])
        if key in seen:
            raise BlenderResumeReceiptError(
                "Blender resume receipt contains duplicate file identities"
            )
        seen.add(key)
        current = _file_identity(
            recorded["path"],
            include_content_key="content_key" in recorded,
        )
        if current != recorded:
            raise BlenderResumeReceiptError(
                f"bound Repair artifact changed: {recorded['path']}"
            )
    if queue_key not in seen:
        raise BlenderResumeReceiptError(
            "Blender resume receipt does not bind its queue SPM"
        )
    result = copy.deepcopy(receipt.get("repair_state"))
    if not isinstance(result, dict) or result.get("current") is not True:
        raise BlenderResumeReceiptError(
            "Blender resume receipt has no current Repair result"
        )
    return result


__all__ = [
    "BLENDER_RESUME_RECEIPT_KIND",
    "BLENDER_RESUME_RECEIPT_VERSION",
    "BlenderResumeReceiptError",
    "build_blender_resume_receipt",
    "settings_signature",
    "validate_blender_resume_receipt",
]
