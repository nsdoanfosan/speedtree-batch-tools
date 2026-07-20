"""Read-only lineage contract for Generators born from legacy Cluster cards.

The visible foreground color is an artist-facing marker.  The permanent
classification is the Generator GUID snapshot written at the same time, so a
later material reconnect, SpeedTree save, or palette refresh cannot erase the
Generator's history.
"""
from __future__ import annotations

import copy
import functools
import gzip
import json
import os
import re
from pathlib import Path


RECEIPT_KIND = "skbatch_legacy_cluster_marker_once"
RECEIPT_VERSION = 1
FOREGROUND_TAGS = (
    "m_bSetForegroundIconColor",
    "m_vecForegroundIconColor_r",
    "m_vecForegroundIconColor_g",
    "m_vecForegroundIconColor_b",
    "m_vecForegroundIconColor_a",
)
LEGACY_CLUSTER_MARKER_VALUES = {
    "m_bSetForegroundIconColor": "true",
    "m_vecForegroundIconColor_r": "1",
    "m_vecForegroundIconColor_g": "0",
    "m_vecForegroundIconColor_b": "1",
    "m_vecForegroundIconColor_a": "1",
}
GENERATOR_BLOCK_RE = re.compile(
    r"<Generator\b[^>]*>.*?</Generator>", re.IGNORECASE | re.DOTALL
)
GUID_RE = re.compile(r"<GUID>([^<]*)</GUID>", re.IGNORECASE)


def marker_receipt_path(spm_path):
    spm = Path(spm_path)
    return spm.parent / "reports" / f"{spm.stem}_legacy_cluster_marker_once.json"


def problem_marker_receipt_path(spm_path):
    spm = Path(spm_path)
    return spm.parent / "reports" / f"{spm.stem}_material_problem_node_markers.json"


def _stat_key(path):
    path = Path(path)
    try:
        stat = path.stat()
        return str(path.resolve()), stat.st_size, stat.st_mtime_ns
    except OSError:
        return str(path.resolve()), -1, -1


def _load_json(path):
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _tag_value(block, tag):
    match = re.search(
        rf"<{re.escape(tag)}>([^<]*)</{re.escape(tag)}>",
        block,
        re.IGNORECASE,
    )
    return match.group(1) if match else None


def _generator_foregrounds(spm):
    raw = Path(spm).read_bytes()
    if raw.startswith(b"\x1f\x8b"):
        raw = gzip.decompress(raw)
    text = raw.decode("utf-8")
    rows = {}
    duplicate_guids = set()
    for match in GENERATOR_BLOCK_RE.finditer(text):
        block = match.group(0)
        guid_match = GUID_RE.search(block)
        guid = guid_match.group(1).strip() if guid_match else ""
        if not guid:
            continue
        if guid in rows:
            duplicate_guids.add(guid)
            continue
        rows[guid] = {
            tag: _tag_value(block, tag) for tag in FOREGROUND_TAGS
        }
    return rows, duplicate_guids


def _problem_marker_guids(spm):
    payload = _load_json(problem_marker_receipt_path(spm)) or {}
    if payload.get("status") != "active":
        return set()
    return {
        str(value).strip()
        for value in (
            payload.get("active_problem_guids")
            or (payload.get("entries") or {}).keys()
        )
        if str(value).strip()
    }


@functools.lru_cache(maxsize=2048)
def _inspect_cached(spm_key, receipt_key, problem_key):
    spm = Path(spm_key[0])
    receipt_path = Path(receipt_key[0])
    result = {
        "spm": str(spm),
        "receipt": str(receipt_path),
        "receipt_valid": False,
        "receipt_path_rebound": False,
        "classified_generator_guids": [],
        "receipt_generator_guids": [],
        "current_marker_guids": [],
        "ambiguous_marker_guids": [],
        "problem_marker_guids": [],
        "marker_drift_guids": [],
        "missing_generator_guids": [],
        "duplicate_generator_guids": [],
        "errors": [],
        "evidence_by_guid": {},
    }
    receipt = _load_json(receipt_path)
    if receipt is None:
        # Color-only evidence is intentionally non-authoritative, so the hot
        # full-folder audit path never decompresses a second copy of every SPM
        # merely to discover an unusable magenta value. Initial migration uses
        # the existing Cluster Color-path heuristic instead.
        return result
    valid = bool(
        receipt.get("kind") == RECEIPT_KIND
        and receipt.get("version") == RECEIPT_VERSION
        and receipt.get("status") in {"applied", "recorded"}
    )
    if not valid:
        result["errors"].append("legacy cluster receipt is invalid")
        return result

    try:
        foregrounds, duplicates = _generator_foregrounds(spm)
    except (OSError, UnicodeError, ValueError) as exc:
        result["errors"].append(str(exc))
        return result
    result["duplicate_generator_guids"] = sorted(duplicates)
    current_markers = {
        guid for guid, values in foregrounds.items()
        if values == LEGACY_CLUSTER_MARKER_VALUES
    }
    result["current_marker_guids"] = sorted(current_markers)
    problem_guids = _problem_marker_guids(spm)
    result["problem_marker_guids"] = sorted(problem_guids)

    result["receipt_valid"] = True
    stored = os.path.normcase(os.path.abspath(str(receipt.get("spm") or "")))
    current = os.path.normcase(os.path.abspath(str(spm)))
    result["receipt_path_rebound"] = stored != current
    receipt_guids = {
        str(value).strip()
        for value in receipt.get("generator_guids") or []
        if str(value).strip()
    }
    result["receipt_generator_guids"] = sorted(receipt_guids)
    missing = receipt_guids - set(foregrounds)
    classified = receipt_guids.intersection(foregrounds) - duplicates
    result["missing_generator_guids"] = sorted(missing)
    result["classified_generator_guids"] = sorted(classified)
    result["marker_drift_guids"] = sorted(
        guid for guid in classified if guid not in current_markers
    )
    result["evidence_by_guid"] = {
        guid: "legacy_marker_receipt" for guid in sorted(classified)
    }

    classified = set(result["classified_generator_guids"])
    # Magenta alone is deliberately not authoritative: the former temporary
    # problem marker used the same color.  Keep it visible as migration/debug
    # evidence without silently turning it into legacy lineage.
    result["ambiguous_marker_guids"] = sorted(
        current_markers - classified - problem_guids
    )
    return result


def inspect_legacy_cluster_state(spm_path):
    """Return the immutable GUID lineage plus current marker drift, read-only."""
    spm = Path(spm_path)
    result = _inspect_cached(
        _stat_key(spm),
        _stat_key(marker_receipt_path(spm)),
        _stat_key(problem_marker_receipt_path(spm)),
    )
    return copy.deepcopy(result)


def legacy_cluster_generator_guids(spm_path):
    return set(
        inspect_legacy_cluster_state(spm_path)["classified_generator_guids"]
    )


__all__ = [
    "FOREGROUND_TAGS",
    "LEGACY_CLUSTER_MARKER_VALUES",
    "RECEIPT_KIND",
    "RECEIPT_VERSION",
    "inspect_legacy_cluster_state",
    "legacy_cluster_generator_guids",
    "marker_receipt_path",
    "problem_marker_receipt_path",
]
