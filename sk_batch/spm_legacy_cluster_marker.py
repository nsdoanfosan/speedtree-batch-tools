"""Permanent one-time foreground marker for legacy cluster Generators.

This is deliberately separate from material preflight.  PCG -> SK conversion
calls it once for a newly copied SK SPM, and existing SKs can be migrated once
with the same function.  Normal SPM invalidation is expected after the write.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import shutil
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path


BATCH_TOOLS_DIR = Path(__file__).resolve().parent.parent
if str(BATCH_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(BATCH_TOOLS_DIR))

try:
    from .spm_problem_node_marker import (
        FOREGROUND_TAGS,
        GENERATOR_BLOCK_RE,
        GUID_RE,
        _patch_generator_fields,
        _read_spm_text,
        _tag_value,
    )
except ImportError:  # Direct execution with sk_batch on sys.path.
    from spm_problem_node_marker import (
        FOREGROUND_TAGS,
        GENERATOR_BLOCK_RE,
        GUID_RE,
        _patch_generator_fields,
        _read_spm_text,
        _tag_value,
    )

from speedtree_legacy_cluster_contract import (
    LEGACY_CLUSTER_MARKER_VALUES,
    RECEIPT_KIND,
    RECEIPT_VERSION,
    inspect_legacy_cluster_state,
    legacy_cluster_generator_guids,
    marker_receipt_path as _contract_marker_receipt_path,
)


MARKER_VALUES = dict(LEGACY_CLUSTER_MARKER_VALUES)


def marker_receipt_path(spm_path):
    return _contract_marker_receipt_path(spm_path)


def _sha256_bytes(payload):
    return hashlib.sha256(payload).hexdigest()


def _atomic_write_bytes(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{uuid.uuid4().hex}")
    try:
        temporary.write_bytes(payload)
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _json_bytes(payload):
    return json.dumps(payload, indent=2, ensure_ascii=False).encode("utf-8")


def _load_receipt(path):
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _canonical_key(path):
    return os.path.normcase(os.path.abspath(str(path))).casefold()


def _existing_receipt(spm):
    path = marker_receipt_path(spm)
    receipt = _load_receipt(path)
    if not receipt:
        if path.exists():
            raise ValueError(f"legacy cluster marker receipt is malformed: {path}")
        return None
    if (
        receipt.get("kind") != RECEIPT_KIND
        or receipt.get("version") != RECEIPT_VERSION
        or _canonical_key(receipt.get("spm")) != _canonical_key(spm)
        or receipt.get("status") not in {"applied", "recorded"}
    ):
        raise ValueError(f"legacy cluster marker receipt is invalid: {path}")
    return receipt


def _candidate_map(candidates):
    result = {}
    for candidate in candidates or []:
        guid = str(candidate.get("generator_guid") or "").strip()
        if not guid:
            continue
        row = result.setdefault(guid, dict(candidate))
        row["generator_guid"] = guid
    return result


def _patch_plan(spm, candidates):
    text = _read_spm_text(spm)
    candidates = _candidate_map(candidates)
    entries = {}
    for match in GENERATOR_BLOCK_RE.finditer(text):
        block = match.group(0)
        guid_match = GUID_RE.search(block)
        guid = guid_match.group(1).strip() if guid_match else ""
        if guid not in candidates:
            continue
        original = {tag: _tag_value(block, tag) for tag in FOREGROUND_TAGS}
        missing = [tag for tag, value in original.items() if value is None]
        if missing:
            raise ValueError(
                f"Generator {guid} has no foreground fields: {', '.join(missing)}"
            )
        entries[guid] = {
            "generator": candidates[guid],
            "original": original,
            "already_marker_color": original == MARKER_VALUES,
        }
    missing_guids = sorted(set(candidates) - set(entries))
    if missing_guids:
        raise ValueError(
            "legacy cluster Generator GUIDs were not found: "
            + ", ".join(missing_guids)
        )
    patched, found, unavailable, missing_fields = _patch_generator_fields(
        text,
        {guid: dict(MARKER_VALUES) for guid in candidates},
    )
    if found != set(candidates) or unavailable or missing_fields:
        raise ValueError("legacy cluster marker patch was incomplete")
    return text, patched, entries


def mark_generator_guids_once(spm_path, candidates, *, dry_run=False):
    """Color exact Generator GUIDs magenta once and keep a permanent receipt."""
    spm = Path(spm_path).resolve()
    existing = _existing_receipt(spm)
    if existing:
        return {
            "spm": str(spm),
            "status": "already_applied",
            "changed": False,
            "generator_count": len(existing.get("generator_guids") or []),
            "receipt": str(marker_receipt_path(spm)),
        }

    candidates = _candidate_map(candidates)
    if not candidates:
        return {
            "spm": str(spm),
            "status": "not_applicable",
            "changed": False,
            "generator_count": 0,
        }

    before_raw = spm.read_bytes()
    before_stat = spm.stat()
    before_text, patched_text, entries = _patch_plan(spm, candidates.values())
    changed = patched_text != before_text
    if changed:
        patched_bytes = patched_text.encode("utf-8")
        after_raw = (
            gzip.compress(patched_bytes, mtime=0)
            if before_raw.startswith(b"\x1f\x8b")
            else patched_bytes
        )
    else:
        # A different gzip timestamp is not a content change and must never
        # invalidate an already-current SK pipeline.
        after_raw = before_raw
    plan = {
        "spm": str(spm),
        "status": "planned" if dry_run else "pending",
        "changed": changed,
        "generator_count": len(entries),
        "generator_guids": sorted(entries),
        "before_sha256": _sha256_bytes(before_raw),
        "after_sha256": _sha256_bytes(after_raw),
    }
    if dry_run:
        return plan

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    backup_dir = (
        spm.parent / "_spm_backups" / f"legacy_cluster_marker_once_{stamp}"
    )
    backup_dir.mkdir(parents=True, exist_ok=False)
    backup = backup_dir / spm.name
    shutil.copy2(spm, backup)
    receipt_path = marker_receipt_path(spm)
    receipt = {
        "kind": RECEIPT_KIND,
        "version": RECEIPT_VERSION,
        "status": "applied" if changed else "recorded",
        "spm": str(spm),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "marker": dict(MARKER_VALUES),
        "owned_fields": list(FOREGROUND_TAGS),
        "generator_guids": sorted(entries),
        "entries": entries,
        "before": {
            "sha256": _sha256_bytes(before_raw),
            "size": len(before_raw),
            "mtime_ns": before_stat.st_mtime_ns,
            "backup": str(backup),
        },
        "after": {
            "sha256": _sha256_bytes(after_raw),
            "size": len(after_raw),
        },
        "material_preflight_integration": False,
        "invalidation_policy": "normal_one_time_refresh",
    }
    wrote_spm = False
    try:
        if (
            spm.read_bytes() != before_raw
            or spm.stat().st_mtime_ns != before_stat.st_mtime_ns
        ):
            raise RuntimeError(f"SPM changed while marker was prepared: {spm}")
        if changed:
            _atomic_write_bytes(spm, after_raw)
            os.chmod(spm, before_stat.st_mode)
            wrote_spm = True
        _atomic_write_bytes(receipt_path, _json_bytes(receipt))
    except Exception:
        if wrote_spm:
            _atomic_write_bytes(spm, before_raw)
            os.chmod(spm, before_stat.st_mode)
            os.utime(spm, ns=(before_stat.st_atime_ns, before_stat.st_mtime_ns))
        try:
            receipt_path.unlink()
        except FileNotFoundError:
            pass
        try:
            backup.unlink()
        except FileNotFoundError:
            pass
        try:
            backup_dir.rmdir()
        except OSError:
            pass
        raise

    plan.update(
        {
            "status": receipt["status"],
            "receipt": str(receipt_path),
            "backup": str(backup),
        }
    )
    return plan


__all__ = [
    "MARKER_VALUES",
    "inspect_legacy_cluster_state",
    "legacy_cluster_generator_guids",
    "marker_receipt_path",
    "mark_generator_guids_once",
]
