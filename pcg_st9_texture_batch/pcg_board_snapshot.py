"""Display-only snapshot cache for the PCG ST9 texture board.

The snapshot makes the previous table available while a new live audit runs.
It is deliberately not execution evidence: this module only reports whether
the stored display context matches the current inputs.  Callers must keep
mutation controls gated by their live audit state.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import date, datetime
from pathlib import Path

try:
    from pcg_texture_common import SHARED_CACHE_DIR, TARGETS_PATH
except ImportError:
    from .pcg_texture_common import SHARED_CACHE_DIR, TARGETS_PATH


BOARD_SNAPSHOT_SCHEMA_VERSION = 2
BOARD_SNAPSHOT_KIND = "pcg_board_display_snapshot"
BOARD_SNAPSHOT_PATH = SHARED_CACHE_DIR / "board_snapshot_v2.json"
BOARD_SNAPSHOT_MAX_BYTES = 16 * 1024 * 1024
BOARD_SNAPSHOT_RETENTION_COUNT = 1

# These arrays are useful in detailed geometry diagnostics but can dominate a
# board snapshot.  Their accompanying counts and contract summaries remain.
DISPLAY_SNAPSHOT_OMITTED_KEYS = frozenset({
    "_gui_blender_connection_pending",
    "_gui_blender_connection_rows",
    "_gui_live_evidence",
    "_gui_exact_mutation_evidence",
    "component_polygon_indices",
    # These live-audit provenance graphs dominate real 55-folder reports but
    # are not read by the cached board renderer.  The complete live report is
    # left untouched and is still required before controls can be unlocked.
    "assembly_handoff",
    "expected_generator_bindings",
    "generator_bindings",
    "leaf_atlas_lineage",
    "polygon_indices",
    "source_generator_bindings",
    "source_material_statuses",
    "triangle_indices",
    "vertex_indices",
})


def _json_safe(
        value, *, omit_diagnostics=False, omission_counts=None):
    if isinstance(value, dict):
        result = {}
        for key, nested in value.items():
            key_text = str(key)
            if (
                omit_diagnostics
                and key_text.casefold() in DISPLAY_SNAPSHOT_OMITTED_KEYS
            ):
                if omission_counts is not None:
                    omission_counts[key_text.casefold()] = (
                        omission_counts.get(key_text.casefold(), 0) + 1
                    )
                continue
            result[key_text] = _json_safe(
                nested,
                omit_diagnostics=omit_diagnostics,
                omission_counts=omission_counts,
            )
        return result
    if isinstance(value, (list, tuple)):
        return [
            _json_safe(
                item,
                omit_diagnostics=omit_diagnostics,
                omission_counts=omission_counts,
            )
            for item in value
        ]
    if isinstance(value, (set, frozenset)):
        rows = [
            _json_safe(
                item,
                omit_diagnostics=omit_diagnostics,
                omission_counts=omission_counts,
            )
            for item in value
        ]
        return sorted(
            rows,
            key=lambda item: json.dumps(
                item,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        )
    if isinstance(value, os.PathLike):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def compact_display_report(report, *, metrics=None):
    """Return a JSON-safe report with large geometry diagnostics removed."""
    omission_counts = {}
    result = _json_safe(
        report or {},
        omit_diagnostics=True,
        omission_counts=omission_counts,
    )
    if metrics is not None:
        metrics["projection_schema_version"] = (
            BOARD_SNAPSHOT_SCHEMA_VERSION
        )
        metrics["omission_counts"] = dict(sorted(omission_counts.items()))
        metrics["omitted_field_count"] = sum(omission_counts.values())
    return result


def _canonical_json(value):
    return json.dumps(
        _json_safe(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _sha256_json(value):
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _normalized_path(value):
    if not value:
        return ""
    candidate = Path(str(value)).expanduser()
    try:
        candidate = candidate.resolve(strict=False)
    except (OSError, RuntimeError):
        candidate = candidate.absolute()
    return os.path.normcase(str(candidate))


def _file_fingerprint(path):
    if not path:
        return {
            "path": "",
            "exists": False,
            "size": None,
            "mtime_ns": None,
            "sha256": None,
        }
    candidate = Path(str(path)).expanduser()
    try:
        candidate = candidate.resolve(strict=False)
    except (OSError, RuntimeError):
        candidate = candidate.absolute()
    try:
        stat = candidate.stat()
        digest = hashlib.sha256()
        with candidate.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        return {
            "path": os.path.normcase(str(candidate)),
            "exists": False,
            "size": None,
            "mtime_ns": None,
            "sha256": None,
        }
    return {
        "path": os.path.normcase(str(candidate)),
        "exists": True,
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "sha256": digest.hexdigest(),
    }


def _declared_source_metadata(pcg_targets):
    payload = pcg_targets or {}
    source_file = payload.get("source_file")
    source_file_fingerprint = (
        _file_fingerprint(source_file)
        if source_file and Path(str(source_file)).expanduser().is_file()
        else None
    )
    details = {
        "source": payload.get("source"),
        "source_file": str(source_file) if source_file else None,
        "source_file_fingerprint": source_file_fingerprint,
        "generated_at": payload.get("generated_at"),
        "graph": payload.get("graph"),
    }
    return {
        "details": details,
        "sha256": _sha256_json(details),
    }


def board_display_context(
        cfg, pcg_targets=None, pcg_targets_path=TARGETS_PATH):
    """Describe inputs that determine whether a cached table is current."""
    safe_cfg = _json_safe(dict(cfg or {}))
    tree_root = _normalized_path(safe_cfg.get("tree_root"))
    if "tree_root" in safe_cfg:
        safe_cfg["tree_root"] = tree_root

    pcg_applied = pcg_targets is not None
    if pcg_applied:
        target_payload_sha256 = _sha256_json(pcg_targets)
        target_file = _file_fingerprint(pcg_targets_path)
        source = _declared_source_metadata(pcg_targets)
    else:
        target_payload_sha256 = None
        target_file = None
        source = None

    return {
        "tree_root": tree_root,
        "config_fingerprint": {
            "algorithm": "sha256",
            "sha256": _sha256_json(safe_cfg),
        },
        "pcg_input_fingerprint": {
            "applied": pcg_applied,
            "target_payload_sha256": target_payload_sha256,
            "target_file": target_file,
            "source": source,
        },
    }


def write_board_display_snapshot(
        report,
        cfg,
        pcg_targets=None,
        path=BOARD_SNAPSHOT_PATH,
        pcg_targets_path=TARGETS_PATH,
        max_bytes=BOARD_SNAPSHOT_MAX_BYTES,
        metrics=None,
        publish_check=None,
):
    """Atomically retain one compact snapshot within the byte budget.

    An over-budget candidate is discarded before it reaches disk, preserving
    the previous bounded snapshot when one exists.
    """
    destination = Path(path)
    projection_metrics = {}
    payload = {
        "schema_version": BOARD_SNAPSHOT_SCHEMA_VERSION,
        "kind": BOARD_SNAPSHOT_KIND,
        "display_only": True,
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "context": board_display_context(
            cfg,
            pcg_targets=pcg_targets,
            pcg_targets_path=pcg_targets_path,
        ),
        "projection": {
            "schema_version": BOARD_SNAPSHOT_SCHEMA_VERSION,
            "omitted_keys": sorted(DISPLAY_SNAPSHOT_OMITTED_KEYS),
        },
        "display_report": compact_display_report(
            report,
            metrics=projection_metrics,
        ),
    }
    payload["projection"].update(projection_metrics)
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    candidate_bytes = len(encoded)
    if metrics is not None:
        metrics.update(projection_metrics)
        metrics.update({
            "candidate_bytes": candidate_bytes,
            "max_bytes": int(max_bytes),
            "item_count": len((report or {}).get("items") or ()),
            "written": False,
        })
    if candidate_bytes > int(max_bytes):
        if metrics is not None:
            metrics["reason"] = "over_budget"
        return None
    if publish_check is not None and not publish_check():
        if metrics is not None:
            metrics["reason"] = "publication_canceled"
        return None
    destination.parent.mkdir(parents=True, exist_ok=True)

    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        if publish_check is not None and not publish_check():
            if metrics is not None:
                metrics["reason"] = "publication_canceled"
            return None
        os.replace(temporary_path, destination)
        temporary_path = None
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except OSError:
                pass
    if metrics is not None:
        metrics["written"] = True
        metrics["reason"] = "written"
    return destination


def _context_mismatches(stored, expected):
    stored = stored if isinstance(stored, dict) else {}
    mismatches = []
    if stored.get("tree_root") != expected["tree_root"]:
        mismatches.append("tree_root")
    if (
        (stored.get("config_fingerprint") or {}).get("sha256")
        != expected["config_fingerprint"]["sha256"]
    ):
        mismatches.append("config_fingerprint")

    stored_pcg = stored.get("pcg_input_fingerprint") or {}
    expected_pcg = expected["pcg_input_fingerprint"]
    if stored_pcg.get("applied") != expected_pcg["applied"]:
        mismatches.append("pcg_input_applied")
    elif expected_pcg["applied"]:
        if (
            stored_pcg.get("target_payload_sha256")
            != expected_pcg["target_payload_sha256"]
        ):
            mismatches.append("pcg_target_payload")
        stored_target_file = stored_pcg.get("target_file") or {}
        expected_target_file = expected_pcg.get("target_file") or {}
        if (
            stored_target_file.get("path") != expected_target_file.get("path")
            or stored_target_file.get("exists")
            != expected_target_file.get("exists")
            or stored_target_file.get("sha256")
            != expected_target_file.get("sha256")
        ):
            mismatches.append("pcg_target_file")
        if (
            (stored_pcg.get("source") or {}).get("sha256")
            != (expected_pcg.get("source") or {}).get("sha256")
        ):
            mismatches.append("pcg_declared_source")
    return mismatches


def read_board_display_snapshot(
        cfg,
        pcg_targets=None,
        path=BOARD_SNAPSHOT_PATH,
        pcg_targets_path=TARGETS_PATH,
        max_bytes=BOARD_SNAPSHOT_MAX_BYTES,
):
    """Read a display cache and describe how it relates to current inputs.

    ``cache_state`` is intentionally limited to display-cache freshness.  It
    must not be used as an execution or pipeline-success decision.
    """
    source_path = Path(path)
    expected_context = board_display_context(
        cfg,
        pcg_targets=pcg_targets,
        pcg_targets_path=pcg_targets_path,
    )
    base = {
        "display_only": True,
        "path": str(source_path),
        "display_report": None,
        "stored_context": None,
        "expected_context": expected_context,
        "mismatch_reasons": [],
    }
    if not source_path.is_file():
        return {**base, "cache_state": "missing", "can_display": False}
    try:
        if source_path.stat().st_size > int(max_bytes):
            return {**base, "cache_state": "oversized", "can_display": False}
    except OSError:
        return {**base, "cache_state": "unreadable", "can_display": False}
    try:
        payload = json.loads(source_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return {**base, "cache_state": "unreadable", "can_display": False}
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != BOARD_SNAPSHOT_SCHEMA_VERSION
        or payload.get("kind") != BOARD_SNAPSHOT_KIND
        or payload.get("display_only") is not True
        or not isinstance(payload.get("display_report"), dict)
    ):
        return {
            **base,
            "cache_state": "incompatible",
            "can_display": False,
        }

    stored_context = payload.get("context")
    mismatches = _context_mismatches(stored_context, expected_context)
    return {
        **base,
        "cache_state": (
            "matching_inputs" if not mismatches else "stale_inputs"
        ),
        "can_display": True,
        "created_at": payload.get("created_at"),
        "display_report": payload["display_report"],
        "stored_context": stored_context,
        "mismatch_reasons": mismatches,
    }


__all__ = [
    "BOARD_SNAPSHOT_KIND",
    "BOARD_SNAPSHOT_MAX_BYTES",
    "BOARD_SNAPSHOT_PATH",
    "BOARD_SNAPSHOT_RETENTION_COUNT",
    "BOARD_SNAPSHOT_SCHEMA_VERSION",
    "DISPLAY_SNAPSHOT_OMITTED_KEYS",
    "board_display_context",
    "compact_display_report",
    "read_board_display_snapshot",
    "write_board_display_snapshot",
]
