"""Transactional SpeedTree Generator marker for real material-export failures.

SpeedTree already uses Generator background colors as workflow metadata, so a
problem marker must never overwrite the background.  This module temporarily
sets only the foreground icon color to magenta and records the exact original
values in a sidecar receipt plus a full SPM backup.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import shutil
import uuid
from datetime import datetime
from pathlib import Path


GENERATOR_BLOCK_RE = re.compile(
    r"<Generator\b[^>]*>.*?</Generator>", re.IGNORECASE | re.DOTALL
)
GUID_RE = re.compile(r"<GUID>([^<]*)</GUID>", re.IGNORECASE)
FOREGROUND_TAGS = (
    "m_bSetForegroundIconColor",
    "m_vecForegroundIconColor_r",
    "m_vecForegroundIconColor_g",
    "m_vecForegroundIconColor_b",
    "m_vecForegroundIconColor_a",
)
MARKER_VALUES = {
    "m_bSetForegroundIconColor": "true",
    "m_vecForegroundIconColor_r": "1",
    "m_vecForegroundIconColor_g": "0",
    "m_vecForegroundIconColor_b": "1",
    "m_vecForegroundIconColor_a": "1",
}


def marker_receipt_path(spm_path):
    spm = Path(spm_path)
    return spm.parent / "reports" / f"{spm.stem}_material_problem_node_markers.json"


def _read_spm_text(path):
    raw = Path(path).read_bytes()
    if not raw.startswith(b"\x1f\x8b"):
        raise ValueError("SPM이 gzip 형식이 아닙니다")
    return gzip.decompress(raw).decode("utf-8")


def _sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


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


def _atomic_write_json(path, payload):
    encoded = json.dumps(payload, indent=2, ensure_ascii=False).encode("utf-8")
    _atomic_write_bytes(path, encoded)


def _tag_value(block, tag):
    match = re.search(
        rf"<{re.escape(tag)}>([^<]*)</{re.escape(tag)}>",
        block,
        re.IGNORECASE,
    )
    return match.group(1) if match else None


def _set_tag_value(block, tag, value):
    pattern = re.compile(
        rf"(<{re.escape(tag)}>)[^<]*(</{re.escape(tag)}>)",
        re.IGNORECASE,
    )
    return pattern.subn(rf"\g<1>{value}\g<2>", block, count=1)


def _patch_generator_fields(text, updates):
    found = set()
    missing_fields = {}

    def replace(match):
        block = match.group(0)
        guid_match = GUID_RE.search(block)
        guid = guid_match.group(1).strip() if guid_match else ""
        values = updates.get(guid)
        if not values:
            return block
        missing = [tag for tag in values if _tag_value(block, tag) is None]
        if missing:
            missing_fields[guid] = missing
            return block
        for tag, value in values.items():
            block, count = _set_tag_value(block, tag, value)
            if count != 1:
                missing_fields.setdefault(guid, []).append(tag)
                return match.group(0)
        found.add(guid)
        return block

    patched = GENERATOR_BLOCK_RE.sub(replace, text)
    missing_guids = sorted(set(updates) - found - set(missing_fields))
    return patched, found, missing_guids, missing_fields


def _load_receipt(path):
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    return payload if isinstance(payload, dict) else None


def _new_backup(spm):
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    folder = spm.parent / "_spm_backups" / f"material_problem_marker_{stamp}"
    folder.mkdir(parents=True, exist_ok=False)
    backup = folder / spm.name
    shutil.copy2(spm, backup)
    return backup


def mark_problem_generators(spm_path, problem_generators):
    """Mark problem GUIDs and return a detailed, idempotent receipt summary."""
    spm = Path(spm_path)
    problems = {
        str(item.get("generator_guid") or "").strip(): dict(item)
        for item in problem_generators
        if str(item.get("generator_guid") or "").strip()
    }
    if not problems:
        return {"status": "not_applicable", "changed": False, "marked": []}

    receipt_path = marker_receipt_path(spm)
    receipt = _load_receipt(receipt_path)
    if not receipt or receipt.get("status") != "active":
        receipt = {
            "version": 1,
            "status": "active",
            "spm": str(spm),
            "marker": dict(MARKER_VALUES),
            "entries": {},
            "created_at": datetime.now().isoformat(timespec="seconds"),
        }

    text = _read_spm_text(spm)
    entries = receipt.setdefault("entries", {})
    updates = {}
    unavailable = []
    conflicts = []
    for match in GENERATOR_BLOCK_RE.finditer(text):
        block = match.group(0)
        guid_match = GUID_RE.search(block)
        guid = guid_match.group(1).strip() if guid_match else ""
        if guid not in problems:
            continue
        originals = {tag: _tag_value(block, tag) for tag in FOREGROUND_TAGS}
        if any(value is None for value in originals.values()):
            unavailable.append(guid)
            continue
        existing = entries.get(guid)
        if existing:
            recorded_original = existing.get("original") or {}
            if originals != MARKER_VALUES and originals != recorded_original:
                conflicts.append(guid)
                continue
        if not existing:
            entries[guid] = {
                "generator": problems[guid],
                "original": originals,
            }
        else:
            entries[guid]["generator"] = problems[guid]
        updates[guid] = dict(MARKER_VALUES)

    patched, found, missing_guids, missing_fields = _patch_generator_fields(
        text, updates
    )
    changed = patched != text
    if changed:
        backup = _new_backup(spm)
        receipt.setdefault("backups", []).append({
            "path": str(backup),
            "sha256": _sha256(backup),
            "created_at": datetime.now().isoformat(timespec="seconds"),
        })
        _atomic_write_bytes(spm, gzip.compress(patched.encode("utf-8")))

    receipt.update({
        "status": "active",
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "spm_sha256": _sha256(spm),
        "active_problem_guids": sorted(problems),
        "unavailable_guids": sorted(set(unavailable + missing_guids)),
        "conflicts": sorted(set(conflicts)),
        "missing_fields": missing_fields,
    })
    _atomic_write_json(receipt_path, receipt)
    return {
        "status": "marked" if found else "unavailable",
        "changed": changed,
        "marked": sorted(found),
        "unavailable": receipt["unavailable_guids"],
        "conflicts": receipt["conflicts"],
        "missing_fields": missing_fields,
        "receipt": str(receipt_path),
    }


def restore_problem_generator_markers(spm_path):
    """Restore owned marker colors without overwriting later user edits."""
    spm = Path(spm_path)
    receipt_path = marker_receipt_path(spm)
    receipt = _load_receipt(receipt_path)
    if not receipt or receipt.get("status") != "active":
        return {"status": "not_applicable", "changed": False, "restored": []}

    text = _read_spm_text(spm)
    restore_updates = {}
    conflicts = []
    entries = receipt.get("entries") or {}
    for match in GENERATOR_BLOCK_RE.finditer(text):
        block = match.group(0)
        guid_match = GUID_RE.search(block)
        guid = guid_match.group(1).strip() if guid_match else ""
        entry = entries.get(guid)
        if not entry:
            continue
        current = {tag: _tag_value(block, tag) for tag in FOREGROUND_TAGS}
        if current != MARKER_VALUES:
            conflicts.append(guid)
            continue
        original = entry.get("original") or {}
        if all(original.get(tag) is not None for tag in FOREGROUND_TAGS):
            restore_updates[guid] = {
                tag: str(original[tag]) for tag in FOREGROUND_TAGS
            }

    patched, found, missing_guids, missing_fields = _patch_generator_fields(
        text, restore_updates
    )
    changed = patched != text
    if changed:
        backup = _new_backup(spm)
        receipt.setdefault("backups", []).append({
            "path": str(backup),
            "sha256": _sha256(backup),
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "reason": "before_marker_restore",
        })
        _atomic_write_bytes(spm, gzip.compress(patched.encode("utf-8")))

    receipt.update({
        "status": "restored" if not conflicts and not missing_guids else "restore_conflict",
        "restored_at": datetime.now().isoformat(timespec="seconds"),
        "restored_guids": sorted(found),
        "conflicts": sorted(set(conflicts + missing_guids)),
        "missing_fields": missing_fields,
        "spm_sha256": _sha256(spm),
    })
    _atomic_write_json(receipt_path, receipt)
    return {
        "status": receipt["status"],
        "changed": changed,
        "restored": sorted(found),
        "conflicts": receipt["conflicts"],
        "missing_fields": missing_fields,
        "receipt": str(receipt_path),
    }
