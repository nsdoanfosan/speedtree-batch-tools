"""Read-only lineage contract for Generators born from legacy Cluster cards.

The visible foreground color is an artist-facing marker.  The permanent
classification is the Generator GUID snapshot written at the same time, so a
later material reconnect, SpeedTree save, or palette refresh cannot erase the
Generator's history.
"""
from __future__ import annotations

import copy
import contextvars
import functools
import gzip
import itertools
import json
import os
import re
from dataclasses import dataclass
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
HANDOFF_STAT_MISMATCH_REASON = "legacy_snapshot_handoff_stat_mismatch"
SNAPSHOT_SCAN_FAILED_REASON = "legacy_snapshot_scan_failed"
GENERATOR_BLOCK_RE = re.compile(
    r"<Generator\b[^>]*>.*?</Generator>", re.IGNORECASE | re.DOTALL
)
GUID_RE = re.compile(r"<GUID>([^<]*)</GUID>", re.IGNORECASE)
# Block boundaries only.  ``GENERATOR_BLOCK_RE`` has to walk ``.*?`` one
# character at a time, which dominated the cost of a full-folder scan; matching
# just the open/close tags and then searching inside the recorded offsets is the
# same walk without materializing a substring per Generator.
GENERATOR_BOUNDARY_RE = re.compile(r"<(/?)Generator\b[^>]*>", re.IGNORECASE)
# A Generator block never carries a foreground tag unless one of these names
# appears in it, so one scan replaces five misses on the common path.
FOREGROUND_ANY_RE = re.compile(
    "|".join(re.escape(tag) for tag in FOREGROUND_TAGS), re.IGNORECASE
)
FOREGROUND_TAG_RE = {
    tag: re.compile(
        rf"<{re.escape(tag)}>([^<]*)</{re.escape(tag)}>", re.IGNORECASE
    )
    for tag in FOREGROUND_TAGS
}
# ``<Nodes>`` is roughly 90% of a large SPM (248 MB of 280 MB on
# SK_tree_scotspine_02), and it can hold no Generator block.  Narrowing the
# document to ``<Generators>`` before any regex work or UTF-8 decode is what
# keeps a cold GUI launch from spending seconds here.
GENERATORS_OPEN_BYTES_RE = re.compile(rb"<Generators\b[^>]*>", re.IGNORECASE)
GENERATORS_CLOSE_BYTES_RE = re.compile(rb"</Generators\s*>", re.IGNORECASE)
GENERATOR_CLOSE_BYTES = b"</Generator>"
# str-level equivalents of the byte patterns above, for callers that already
# hold the fully decoded document (e.g. pcg_texture_audit's per-SPM analysis)
# and only need the cheap narrowing, not another read/decompress/decode.
GENERATORS_OPEN_RE = re.compile(r"<Generators\b[^>]*>", re.IGNORECASE)
GENERATORS_CLOSE_RE = re.compile(r"</Generators\s*>", re.IGNORECASE)
GENERATOR_CLOSE_TAG = "</Generator>"

# In-process snapshots of {guid: {tag: value}} + duplicate GUIDs, keyed by the
# same (resolved path, size, mtime_ns) tuple as ``_stat_key``.  A caller that
# already decoded an SPM (pcg_texture_audit's ``_spm_analysis``) shares the
# parsed result here so ``_generator_foregrounds`` never re-reads/decompresses
# the same file.  Keying on the exact stat means a changed file is always a
# miss, never a stale hit.
_SHARED_GENERATOR_SNAPSHOTS = {}

# A decoded SPM can be hundreds of MB. Keep it owned by the exact synchronous
# inspection that received it rather than in a module-global path slot/map.
# ContextVar isolates both worker threads and re-entrant async contexts; reset
# in ``inspect_legacy_cluster_state`` bounds the lifetime to that one call.
_ACTIVE_DECODED_HANDOFF = contextvars.ContextVar(
    "speedtree_legacy_decoded_handoff", default=None
)
_HANDOFF_GENERATIONS = itertools.count(1)


@dataclass(frozen=True)
class DecodedSpmHandoff:
    """One caller-owned decoded document bound to path/stat/generation."""

    spm_key: tuple
    generation: int
    text: str


def make_decoded_spm_handoff(spm_path, text, *, size, mtime_ns):
    """Bind decoded text to the stat observed before its owning cold parse."""
    key = (str(Path(spm_path).resolve()), int(size), int(mtime_ns))
    return DecodedSpmHandoff(key, next(_HANDOFF_GENERATIONS), text)


def _store_generator_snapshot(key, foregrounds, duplicate_guids):
    path_key = key[0]
    for old_key in [
        existing for existing in _SHARED_GENERATOR_SNAPSHOTS
        if existing[0] == path_key and existing != key
    ]:
        del _SHARED_GENERATOR_SNAPSHOTS[old_key]
    result = (dict(foregrounds), set(duplicate_guids))
    _SHARED_GENERATOR_SNAPSHOTS[key] = result
    return result


def _prime_generator_snapshot(spm, snapshot):
    """Register a (foregrounds, duplicate_guids) pair computed elsewhere."""
    if snapshot is None:
        return
    foregrounds, duplicate_guids = snapshot
    _store_generator_snapshot(_stat_key(spm), foregrounds, duplicate_guids)


def peek_generator_foregrounds(spm_path):
    """Return a previously shared/computed (foregrounds, duplicates) pair.

    Returns ``None`` if nothing has been computed for the SPM's current stat
    in this process yet -- used by callers that want to opportunistically
    persist a snapshot they did not compute themselves.
    """
    snapshot = _SHARED_GENERATOR_SNAPSHOTS.get(_stat_key(spm_path))
    if snapshot is None:
        return None
    foregrounds, duplicate_guids = snapshot
    return dict(foregrounds), set(duplicate_guids)


def generator_foregrounds_from_decoded_text(text):
    """Same result as ``_generator_foregrounds``, from text decoded elsewhere.

    Sharing the already-decoded document removes the redundant read and
    gzip/UTF-8 decode; the boundary-only regex walk still runs once, over the
    same narrowed ``<Generators>`` section a fresh read would have used.
    """
    return _generator_foregrounds_from_text(_generator_section_text(text))


def _generator_section_text(text):
    """String-level narrowing mirror of ``_generator_section_bytes``."""
    open_match = GENERATORS_OPEN_RE.search(text)
    if open_match is None:
        return text
    close_match = GENERATORS_CLOSE_RE.search(text, open_match.end())
    section = text[
        open_match.end(): close_match.start() if close_match else len(text)
    ]
    if section.count(GENERATOR_CLOSE_TAG) != text.count(GENERATOR_CLOSE_TAG):
        return text
    return section


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


def _record_generator_block(text, start, end, rows, duplicate_guids):
    """Record one ``<Generator>`` body, given its half-open text offsets."""

    guid_match = GUID_RE.search(text, start, end)
    guid = guid_match.group(1).strip() if guid_match else ""
    if not guid:
        return
    if guid in rows:
        duplicate_guids.add(guid)
        return
    if FOREGROUND_ANY_RE.search(text, start, end) is None:
        rows[guid] = {tag: None for tag in FOREGROUND_TAGS}
        return
    values = {}
    for tag in FOREGROUND_TAGS:
        match = FOREGROUND_TAG_RE[tag].search(text, start, end)
        values[tag] = match.group(1) if match else None
    rows[guid] = values


def _generator_foregrounds_from_text(text):
    """Return ``{guid: {foreground tag: value}}`` plus duplicate GUIDs."""

    rows = {}
    duplicate_guids = set()
    body_start = None
    for match in GENERATOR_BOUNDARY_RE.finditer(text):
        if match.group(1):  # </Generator> closes the open block, if any.
            if body_start is not None:
                _record_generator_block(
                    text, body_start, match.start(), rows, duplicate_guids
                )
                body_start = None
        elif body_start is None:
            # A nested <Generator ...> stays part of the enclosing block, which
            # is what the former non-greedy block match also did.
            body_start = match.end()
    return rows, duplicate_guids


def _generator_section_bytes(raw):
    """Narrow a decompressed SPM to the region that can hold Generator blocks.

    Falls back to the whole document whenever the narrowed region would not
    contain every ``</Generator>``, so an unexpected layout can only cost time,
    never a missed Generator.  That fallback is not theoretical: production
    ``tree_Weeping_Willow_01_back.spm`` keeps 258 of its 259 Generators outside
    the first ``<Generators>`` container.
    """
    open_match = GENERATORS_OPEN_BYTES_RE.search(raw)
    if open_match is None:
        return raw
    close_match = GENERATORS_CLOSE_BYTES_RE.search(raw, open_match.end())
    section = raw[
        open_match.end(): close_match.start() if close_match else len(raw)
    ]
    if section.count(GENERATOR_CLOSE_BYTES) != raw.count(GENERATOR_CLOSE_BYTES):
        return raw
    return section


def _generator_foregrounds(spm):
    key = _stat_key(spm)
    shared = _SHARED_GENERATOR_SNAPSHOTS.get(key)
    if shared is not None:
        return shared
    # A caller that just decoded this exact SPM may own a handoff in this
    # execution context. The identity is generation-bound and the path/stat
    # was validated before the context was activated, so another worker can
    # neither overwrite nor consume it.
    handoff = _ACTIVE_DECODED_HANDOFF.get()
    if handoff is not None and handoff.spm_key == key:
        foregrounds, duplicate_guids = generator_foregrounds_from_decoded_text(
            handoff.text)
        return _store_generator_snapshot(key, foregrounds, duplicate_guids)
    raw = Path(spm).read_bytes()
    if raw.startswith(b"\x1f\x8b"):
        raw = gzip.decompress(raw)
    foregrounds, duplicate_guids = _generator_foregrounds_from_text(
        _generator_section_bytes(raw).decode("utf-8")
    )
    return _store_generator_snapshot(key, foregrounds, duplicate_guids)


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


def _empty_inspection_result(spm, receipt_path):
    return {
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
        "reason_tokens": [],
        "handoff_evidence": {},
        "failure_evidence": {},
        "evidence_by_guid": {},
    }


@functools.lru_cache(maxsize=2048)
def _inspect_cached(spm_key, receipt_key, problem_key):
    spm = Path(spm_key[0])
    receipt_path = Path(receipt_key[0])
    result = _empty_inspection_result(spm, receipt_path)
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
        result["reason_tokens"].append(SNAPSHOT_SCAN_FAILED_REASON)
        result["failure_evidence"] = {
            "exception_type": type(exc).__name__,
            "message": str(exc),
            "spm_key": list(spm_key),
        }
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


def inspect_legacy_cluster_state(
        spm_path, foregrounds_snapshot=None, decoded_handoff=None):
    """Return the immutable GUID lineage plus current marker drift, read-only.

    Two independent, optional hand-offs let a caller who already touched this
    exact SPM (pcg_texture_audit's per-SPM analysis) avoid a second
    read/decompress here, without ever running the Generator-foreground scan
    for an SPM that turns out not to need it:

    ``foregrounds_snapshot``
        An already-*derived* ``(foregrounds, duplicate_guids)`` pair (see
        ``generator_foregrounds_from_decoded_text``) -- typically a snapshot
        persisted on a previous run. No scan runs at all; it is used as-is.
    ``decoded_handoff``
        A caller-owned :class:`DecodedSpmHandoff`. Its path/stat must still
        match the inspected SPM. The generation stays local to this execution
        context, and the decoded text is released in ``finally`` after this
        call, whether it succeeds, is interrupted, or hits a cache entry.

    Both are ignored whenever the ``(spm, receipt, problem_receipt)`` stat
    triple is already cached -- lru_cache reuse alone then makes either
    unnecessary.
    """
    spm = Path(spm_path)
    key = _stat_key(spm)
    if decoded_handoff is not None:
        if not isinstance(decoded_handoff, DecodedSpmHandoff) \
                or decoded_handoff.spm_key != key:
            result = _empty_inspection_result(
                spm, marker_receipt_path(spm))
            result["errors"].append(
                "decoded SPM handoff no longer matches the inspected path/stat"
            )
            result["reason_tokens"].append(HANDOFF_STAT_MISMATCH_REASON)
            expected = (
                decoded_handoff.spm_key
                if isinstance(decoded_handoff, DecodedSpmHandoff)
                else None
            )
            result["handoff_evidence"] = {
                "generation": getattr(decoded_handoff, "generation", None),
                "expected_spm_key": list(expected) if expected else None,
                "actual_spm_key": list(key),
            }
            return result
    _prime_generator_snapshot(spm, foregrounds_snapshot)
    token = None
    if foregrounds_snapshot is None and decoded_handoff is not None:
        token = _ACTIVE_DECODED_HANDOFF.set(decoded_handoff)
    try:
        result = _inspect_cached(
            key,
            _stat_key(marker_receipt_path(spm)),
            _stat_key(problem_marker_receipt_path(spm)),
        )
    finally:
        if token is not None:
            _ACTIVE_DECODED_HANDOFF.reset(token)
    return copy.deepcopy(result)


def legacy_cluster_generator_guids(spm_path):
    return set(
        inspect_legacy_cluster_state(spm_path)["classified_generator_guids"]
    )


__all__ = [
    "FOREGROUND_TAGS",
    "HANDOFF_STAT_MISMATCH_REASON",
    "LEGACY_CLUSTER_MARKER_VALUES",
    "RECEIPT_KIND",
    "RECEIPT_VERSION",
    "SNAPSHOT_SCAN_FAILED_REASON",
    "DecodedSpmHandoff",
    "generator_foregrounds_from_decoded_text",
    "inspect_legacy_cluster_state",
    "legacy_cluster_generator_guids",
    "make_decoded_spm_handoff",
    "marker_receipt_path",
    "peek_generator_foregrounds",
    "problem_marker_receipt_path",
]
