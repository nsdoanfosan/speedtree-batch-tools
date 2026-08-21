"""Non-blocking resume hints for completed Blender Assembly rows.

A receipt may remove an unchanged row from a restarted queue.  A missing or
changed receipt never fails an item: orchestration either runs relationship
maintenance before deciding whether Assembly is still reusable, or rebuilds the
row when an output-affecting input changed.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path

BLENDER_RESUME_RECEIPT_KIND = "sk_batch_blender_resume_receipt"
BLENDER_RESUME_RECEIPT_VERSION = 2


class BlenderResumeReceiptError(ValueError):
    """A resume hint cannot authorize a queue-level skip."""

    def __init__(self, message, *, resume_action="invalid"):
        super().__init__(message)
        self.resume_action = str(resume_action)


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


def _file_identity(path):
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
            f"bound Assembly artifact is not a file: {candidate}"
        )
    identity = {
        "path": str(candidate),
        "size": int(stat.st_size),
        "mtime_ns": int(stat.st_mtime_ns),
        "ctime_ns": int(stat.st_ctime_ns),
        "device": int(stat.st_dev),
        "file_id": int(stat.st_ino),
    }
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
    relation_paths=(),
    relation_signature=None,
    settings,
    assembly_state,
):
    """Seal one already-validated current Assembly decision."""

    if not isinstance(assembly_state, dict) or assembly_state.get("current") is not True:
        raise BlenderResumeReceiptError(
            "only a current live Assembly decision can create a resume receipt"
        )
    queue_spm = _canonical_path(queue_spm)
    if not Path(queue_spm).is_file():
        raise BlenderResumeReceiptError(
            f"queue SPM is missing: {queue_spm}"
        )
    paths = {_path_key(queue_spm): (queue_spm, "core")}
    for path in tracked_paths or ():
        if path:
            canonical = _canonical_path(path)
            paths[_path_key(canonical)] = (canonical, "core")
    for path in relation_paths or ():
        if path:
            canonical = _canonical_path(path)
            # A core output/input must stay core even if a dependency report
            # redundantly lists the same path as relationship metadata.
            paths.setdefault(
                _path_key(canonical),
                (canonical, "relation"),
            )
    files = [
        {
            **_file_identity(paths[key][0]),
            "role": paths[key][1],
        }
        for key in sorted(paths)
    ]
    result = {
        key: copy.deepcopy(assembly_state[key])
        for key in (
            "current",
            "push_ready",
            "kind",
            "reason",
            "texture_reason",
            "push_dependency_contract",
        )
        if key in assembly_state
    }
    payload = {
        "kind": BLENDER_RESUME_RECEIPT_KIND,
        "schema_version": BLENDER_RESUME_RECEIPT_VERSION,
        "queue_spm": queue_spm,
        "settings_sha256": settings_signature(settings),
        "relation_signature": copy.deepcopy(relation_signature),
        "files": files,
        "assembly_state": result,
    }
    payload["receipt_sha256"] = _canonical_json_sha256(payload)
    return payload


def validate_blender_resume_receipt(
    receipt,
    queue_spm,
    *,
    settings,
    relation_signature=None,
):
    """Return the saved decision only when a queue-level skip is safe."""

    if not isinstance(receipt, dict):
        raise BlenderResumeReceiptError(
            "Blender resume receipt is missing",
            resume_action="missing",
        )
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
            "Blender resume receipt envelope is invalid",
            resume_action="invalid",
        )
    if _path_key(receipt.get("queue_spm") or "") != _path_key(queue_spm):
        raise BlenderResumeReceiptError(
            "Blender resume receipt belongs to another SPM",
            resume_action="wrong_target",
        )
    if receipt.get("settings_sha256") != settings_signature(settings):
        raise BlenderResumeReceiptError(
            "Blender Assembly output settings changed",
            resume_action="rebuild_required",
        )
    if receipt.get("relation_signature") != relation_signature:
        raise BlenderResumeReceiptError(
            "Blender Assembly relationship signal changed",
            resume_action="relation_changed",
        )
    files = receipt.get("files")
    if not isinstance(files, list) or not files:
        raise BlenderResumeReceiptError(
            "Blender resume receipt has no bound files",
            resume_action="invalid",
        )
    queue_key = _path_key(queue_spm)
    seen = set()
    for recorded in files:
        if not isinstance(recorded, dict) or not recorded.get("path"):
            raise BlenderResumeReceiptError(
                "Blender resume receipt contains a malformed file identity",
                resume_action="invalid",
            )
        key = _path_key(recorded["path"])
        if key in seen:
            raise BlenderResumeReceiptError(
                "Blender resume receipt contains duplicate file identities",
                resume_action="invalid",
            )
        seen.add(key)
        current = _file_identity(recorded["path"])
        expected = dict(recorded)
        role = str(expected.pop("role", "core") or "core")
        # Receipts from the pre-merge prototype may contain a sampled content
        # key. Drop it instead of reading SPM/Blend bytes again; current stat
        # identity is the resume contract.
        expected.pop("content_key", None)
        if current != expected:
            raise BlenderResumeReceiptError(
                f"bound Assembly artifact changed: {recorded['path']}",
                resume_action=(
                    "relation_changed"
                    if role == "relation"
                    else "rebuild_required"
                ),
            )
    if queue_key not in seen:
        raise BlenderResumeReceiptError(
            "Blender resume receipt does not bind its queue SPM",
            resume_action="invalid",
        )
    result = copy.deepcopy(receipt.get("assembly_state"))
    if not isinstance(result, dict) or result.get("current") is not True:
        raise BlenderResumeReceiptError(
            "Blender resume receipt has no current Assembly result",
            resume_action="invalid",
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
