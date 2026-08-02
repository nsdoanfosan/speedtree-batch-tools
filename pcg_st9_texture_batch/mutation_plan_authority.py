"""Exact, per-unit mutation authority manifests and partial receipts.

Startup caches are intentionally outside this contract.  A manifest captures
the current full bytes/presence of every declared plan path and the bounded
membership of directories whose contents influence the plan.  Each mutable
unit is revalidated immediately before its first write or external call.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

from artifact_content_key import SHA256_ALGORITHM, file_content_key_snapshot


SCHEMA_VERSION = 2
MAX_MEMBERSHIP_ENTRIES = 32_768


def _json_safe(value):
    if isinstance(value, dict):
        return {
            str(key): _json_safe(nested)
            for key, nested in value.items()
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json_safe(nested) for nested in value]
    if isinstance(value, os.PathLike):
        return str(value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return repr(value)


def _digest(value):
    return hashlib.sha256(json.dumps(
        _json_safe(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")).hexdigest()


def _path_key(path):
    return os.path.normcase(str(Path(path).expanduser().absolute()))


def path_state(path):
    """Return an exact present/missing precondition for one planned path."""
    candidate = Path(path).expanduser().absolute()
    key = _path_key(candidate)
    try:
        if candidate.is_file():
            snapshot = file_content_key_snapshot(
                candidate, SHA256_ALGORITHM
            )
            return {
                "path": key,
                "state": "file",
                "size": snapshot["size"],
                "sha256": snapshot["digest"],
                "algorithm": snapshot["algorithm"],
            }
        if candidate.is_dir():
            return {"path": key, "state": "directory"}
        if candidate.exists():
            return {"path": key, "state": "other"}
        return {"path": key, "state": "missing"}
    except OSError as exc:
        raise RuntimeError(
            f"mutation authority could not inspect {candidate}: {exc}"
        ) from exc


def directory_state(path, *, max_entries=MAX_MEMBERSHIP_ENTRIES):
    """Capture direct membership, type, and selection-relevant mtime."""
    directory = Path(path).expanduser().absolute()
    key = _path_key(directory)
    try:
        if not directory.exists():
            return {"path": key, "state": "missing", "entries": []}
        if not directory.is_dir():
            return {"path": key, "state": "not_directory", "entries": []}
        entries = []
        for index, entry in enumerate(sorted(
                directory.iterdir(), key=lambda row: row.name.casefold())):
            if index >= int(max_entries):
                raise RuntimeError(
                    "mutation authority directory bound exceeded for "
                    + str(directory)
                )
            stat = entry.stat()
            entries.append({
                "name": entry.name,
                "kind": (
                    "file" if entry.is_file()
                    else "directory" if entry.is_dir()
                    else "other"
                ),
                "mtime_ns": stat.st_mtime_ns,
            })
        return {"path": key, "state": "directory", "entries": entries}
    except OSError as exc:
        raise RuntimeError(
            f"mutation authority could not enumerate {directory}: {exc}"
        ) from exc


def _unique_paths(values):
    result = {}
    for value in values or ():
        if value is None or str(value).strip() == "":
            continue
        result[_path_key(value)] = str(Path(value).expanduser().absolute())
    return [result[key] for key in sorted(result)]


def _capture_unit(spec):
    unit_id = str(spec.get("unit_id") or "").strip()
    if not unit_id:
        raise ValueError("mutation authority unit_id is required")
    payload = _json_safe(spec.get("payload"))
    paths = _unique_paths(spec.get("paths"))
    memberships = _unique_paths(spec.get("memberships"))
    write_paths = _unique_paths(spec.get("write_paths"))
    write_memberships = _unique_paths(spec.get("write_memberships"))
    item_keys = sorted(str(key) for key in spec.get("item_keys") or ())
    write_item_keys = sorted(
        str(key) for key in spec.get("write_item_keys") or ()
    )
    item_states = {
        str(key): _json_safe(value)
        for key, value in (spec.get("item_states") or {}).items()
    }
    if not {_path_key(path) for path in write_paths}.issubset(
            {_path_key(path) for path in paths}):
        raise ValueError("mutation authority write_paths must be sealed paths")
    if not {_path_key(path) for path in write_memberships}.issubset(
            {_path_key(path) for path in memberships}):
        raise ValueError(
            "mutation authority write_memberships must be sealed memberships"
        )
    if not set(write_item_keys).issubset(item_keys):
        raise ValueError(
            "mutation authority write_item_keys must be sealed item keys"
        )
    if not set(item_states).issubset(item_keys):
        raise ValueError(
            "mutation authority item_states must be sealed item keys"
        )
    return {
        "unit_id": unit_id,
        "payload": payload,
        "payload_sha256": _digest(payload),
        "item_keys": item_keys,
        "item_states": item_states,
        "write_item_keys": write_item_keys,
        "path_names": paths,
        "write_path_names": write_paths,
        "path_states": [path_state(path) for path in paths],
        "membership_names": memberships,
        "write_membership_names": write_memberships,
        "membership_states": [
            directory_state(path) for path in memberships
        ],
    }


def capture_manifest(
        *, action, logical_plan, config_projection=None, tool_paths=None,
        units, receipt_path=None):
    """Seal an immutable logical plan and exact per-unit preconditions."""
    captured_units = [_capture_unit(spec) for spec in units or ()]
    if not captured_units:
        raise RuntimeError(
            "mutation authority needs at least one execution unit"
        )
    unit_ids = [unit["unit_id"] for unit in captured_units]
    if len(set(unit_ids)) != len(unit_ids):
        raise ValueError("mutation authority unit_id values must be unique")
    logical_plan = _json_safe(logical_plan)
    config_projection = _json_safe(config_projection or {})
    tools = _unique_paths(tool_paths)
    public = {
        "schema_version": SCHEMA_VERSION,
        "kind": "speedtree_exact_mutation_authority_v2",
        "action": str(action),
        "logical_plan": logical_plan,
        "logical_plan_sha256": _digest(logical_plan),
        "config_projection": config_projection,
        "config_sha256": _digest(config_projection),
        "tool_path_names": tools,
        "tool_path_states": [path_state(path) for path in tools],
        "units": captured_units,
    }
    public["authority_sha256"] = _digest(public)
    manifest = dict(public)
    manifest["receipt_path"] = (
        str(Path(receipt_path).absolute()) if receipt_path else None
    )
    manifest["receipt"] = {
        "schema_version": 1,
        "kind": "speedtree_mutation_authority_receipt_v1",
        "authority_sha256": public["authority_sha256"],
        "action": str(action),
        "status": "sealed",
        "authorized_units": [],
        "completed_units": [],
        "unit_precondition_advances": {},
        "blocked_unit": None,
        "writes_before_block": 0,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    _persist_receipt(manifest)
    return manifest


def _public_manifest(manifest):
    return {
        key: value
        for key, value in manifest.items()
        if key not in {"authority_sha256", "receipt_path", "receipt"}
    }


def _persist_receipt(manifest):
    raw_path = manifest.get("receipt_path")
    if not raw_path:
        return
    path = Path(raw_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(
        f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
    )
    payload = {
        "authority": {
            "schema_version": manifest.get("schema_version"),
            "kind": manifest.get("kind"),
            "action": manifest.get("action"),
            "authority_sha256": manifest.get("authority_sha256"),
            "logical_plan_sha256": manifest.get("logical_plan_sha256"),
            "config_sha256": manifest.get("config_sha256"),
        },
        "receipt": manifest.get("receipt"),
    }
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


class MutationAuthorityError(RuntimeError):
    """Fail-closed authority mismatch carrying the partial run receipt."""

    def __init__(self, message, receipt):
        super().__init__(message)
        self.receipt = _json_safe(receipt)


def _block(manifest, unit_id, reason):
    receipt = manifest["receipt"]
    receipt.update({
        "status": "blocked",
        "blocked_unit": str(unit_id),
        "reason": str(reason),
        "writes_before_block": len(receipt.get("authorized_units") or ()),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    })
    _persist_receipt(manifest)
    raise MutationAuthorityError(
        f"mutation authority blocked {unit_id}: {reason}", receipt
    )


def require_unit(
        manifest, unit_id, *, current_payload, current_config=None):
    """Revalidate one unit immediately before its first mutation boundary."""
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 2:
        raise RuntimeError("mutation authority manifest is unavailable")
    expected_digest = _digest({
        **_public_manifest(manifest),
    })
    if expected_digest != manifest.get("authority_sha256"):
        _block(manifest, unit_id, "authority manifest digest changed")
    unit = next((
        row for row in manifest.get("units") or ()
        if row.get("unit_id") == str(unit_id)
    ), None)
    if unit is None:
        _block(manifest, unit_id, "execution unit is not sealed")
    if _digest(current_payload) != unit.get("payload_sha256"):
        _block(manifest, unit_id, "live execution payload changed")
    if current_config is not None and _digest(current_config) != manifest.get(
            "config_sha256"):
        _block(manifest, unit_id, "mutation config changed")
    try:
        current_tools = [
            path_state(path) for path in manifest.get("tool_path_names") or ()
        ]
        current_paths = [
            path_state(path) for path in unit.get("path_names") or ()
        ]
        current_memberships = [
            directory_state(path)
            for path in unit.get("membership_names") or ()
        ]
    except Exception as exc:
        _block(manifest, unit_id, str(exc))
    if current_tools != manifest.get("tool_path_states"):
        _block(manifest, unit_id, "tool/package input changed")
    advances = (
        manifest.get("receipt", {}).get("unit_precondition_advances", {})
        .get(str(unit_id), {})
    )
    expected_paths = {
        state.get("path"): state for state in unit.get("path_states") or ()
    }
    expected_paths.update(advances.get("path_states") or {})
    expected_memberships = {
        state.get("path"): state
        for state in unit.get("membership_states") or ()
    }
    expected_memberships.update(advances.get("membership_states") or {})
    if current_paths != [
            expected_paths.get(state.get("path"), state)
            for state in unit.get("path_states") or ()]:
        _block(manifest, unit_id, "planned path state changed")
    if current_memberships != [
            expected_memberships.get(state.get("path"), state)
            for state in unit.get("membership_states") or ()]:
        _block(manifest, unit_id, "planned directory membership changed")
    receipt = manifest["receipt"]
    authorized = receipt.setdefault("authorized_units", [])
    if str(unit_id) not in authorized:
        authorized.append(str(unit_id))
    receipt.update({
        "status": "running",
        "blocked_unit": None,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    })
    _persist_receipt(manifest)
    return True


def expected_path_state(manifest, unit_id, path):
    """Return the sealed pre-state for one path in an execution unit."""
    wanted = _path_key(path)
    unit = next((
        row for row in manifest.get("units") or ()
        if row.get("unit_id") == str(unit_id)
    ), None)
    if unit is None:
        raise RuntimeError("mutation authority unit is unavailable")
    advanced = (
        manifest.get("receipt", {}).get("unit_precondition_advances", {})
        .get(str(unit_id), {}).get("path_states", {})
    )
    for state in unit.get("path_states") or ():
        if state.get("path") == wanted:
            return dict(advanced.get(wanted, state))
    raise RuntimeError(
        f"mutation authority path was not sealed for {unit_id}: {path}"
    )


def write_child_authority(manifest, unit_id, path):
    """Write the exact subset a child process must verify before mutation."""
    unit = next((
        row for row in manifest.get("units") or ()
        if row.get("unit_id") == str(unit_id)
    ), None)
    if unit is None:
        raise RuntimeError("mutation authority child unit is unavailable")
    payload = {
        "schema_version": 1,
        "kind": "speedtree_child_mutation_authority_v1",
        "parent_authority_sha256": manifest.get("authority_sha256"),
        "unit_id": str(unit_id),
        "payload": unit.get("payload"),
        "payload_sha256": unit.get("payload_sha256"),
        "write_item_keys": unit.get("write_item_keys") or [],
        "path_names": unit.get("path_names") or [],
        "path_states": unit.get("path_states") or [],
        "write_path_names": unit.get("write_path_names") or [],
        "membership_names": unit.get("membership_names") or [],
        "membership_states": unit.get("membership_states") or [],
        "write_membership_names": (
            unit.get("write_membership_names") or []
        ),
        "tool_path_names": manifest.get("tool_path_names") or [],
        "tool_path_states": manifest.get("tool_path_states") or [],
    }
    payload["document_sha256"] = _digest(payload)
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(
        f".{output.name}.{os.getpid()}.{threading.get_ident()}.tmp"
    )
    try:
        temporary.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        os.replace(temporary, output)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return payload["document_sha256"]


def validate_child_authority(path, expected_document_sha256):
    """Recompute a parent-sealed child subset before production mutation."""
    document = json.loads(Path(path).read_text(encoding="utf-8"))
    document_sha256 = document.pop("document_sha256", None)
    actual_document_sha256 = _digest(document)
    if (
        document_sha256 != actual_document_sha256
        or actual_document_sha256 != str(expected_document_sha256)
    ):
        raise RuntimeError("child mutation authority document changed")
    current_paths = [
        path_state(value) for value in document.get("path_names") or ()
    ]
    current_memberships = [
        directory_state(value)
        for value in document.get("membership_names") or ()
    ]
    current_tools = [
        path_state(value) for value in document.get("tool_path_names") or ()
    ]
    if current_paths != document.get("path_states"):
        raise RuntimeError("child mutation planned path state changed")
    if current_memberships != document.get("membership_states"):
        raise RuntimeError("child mutation directory membership changed")
    if current_tools != document.get("tool_path_states"):
        raise RuntimeError("child mutation tool input changed")
    return document


def require_child_payload(document, current_payload):
    """Bind child CLI/current intent to the parent-sealed unit payload."""
    if _digest(current_payload) != document.get("payload_sha256"):
        raise RuntimeError("child mutation execution payload changed")
    return True


def expected_item_state(manifest, unit_id, item_key):
    """Return an item post-state advanced by a completed prior unit."""
    advanced = (
        manifest.get("receipt", {}).get("unit_precondition_advances", {})
        .get(str(unit_id), {}).get("item_states", {}).get(str(item_key))
    )
    if advanced is not None:
        return advanced
    unit = next((
        row for row in manifest.get("units") or ()
        if row.get("unit_id") == str(unit_id)
    ), None)
    if unit is None:
        return None
    return (unit.get("item_states") or {}).get(str(item_key))


def complete_unit(
        manifest, unit_id, *, post_paths=None, item_post_states=None):
    """Append a durable per-unit completion/post-state receipt."""
    receipt = manifest["receipt"]
    completed = receipt.setdefault("completed_units", [])
    if any(row.get("unit_id") == str(unit_id) for row in completed):
        return receipt
    authorized = receipt.setdefault("authorized_units", [])
    if str(unit_id) not in authorized:
        _block(manifest, unit_id, "execution unit was not authorized")
    units = list(manifest.get("units") or ())
    current_index = next((
        index for index, row in enumerate(units)
        if row.get("unit_id") == str(unit_id)
    ), None)
    if current_index is None:
        _block(manifest, unit_id, "execution unit is not sealed")
    unit = units[current_index]
    try:
        current_paths = [
            path_state(path) for path in unit.get("path_names") or ()
        ]
        current_memberships = [
            directory_state(path)
            for path in unit.get("membership_names") or ()
        ]
        declared_post_states = [
            path_state(path) for path in _unique_paths(post_paths)
        ]
    except Exception as exc:
        _block(manifest, unit_id, str(exc))
    normalized_item_states = {
        str(key): _json_safe(value)
        for key, value in (item_post_states or {}).items()
    }
    current_path_map = {
        state.get("path"): state for state in current_paths
    }
    current_membership_map = {
        state.get("path"): state for state in current_memberships
    }
    write_path_keys = {
        _path_key(path) for path in unit.get("write_path_names") or ()
    }
    write_membership_keys = {
        _path_key(path)
        for path in unit.get("write_membership_names") or ()
    }
    write_item_keys = set(unit.get("write_item_keys") or ())
    unit_advances = (
        receipt.get("unit_precondition_advances", {}).get(str(unit_id), {})
    )
    expected_path_map = {
        state.get("path"): state for state in unit.get("path_states") or ()
    }
    expected_path_map.update(unit_advances.get("path_states") or {})
    expected_membership_map = {
        state.get("path"): state
        for state in unit.get("membership_states") or ()
    }
    expected_membership_map.update(
        unit_advances.get("membership_states") or {}
    )
    expected_item_map = dict(unit.get("item_states") or {})
    expected_item_map.update(unit_advances.get("item_states") or {})
    if any(
        state != expected_path_map.get(key)
        for key, state in current_path_map.items()
        if key not in write_path_keys
    ):
        _block(manifest, unit_id, "unplanned path changed during execution")
    if any(
        state != expected_membership_map.get(key)
        for key, state in current_membership_map.items()
        if key not in write_membership_keys
    ):
        _block(
            manifest,
            unit_id,
            "unplanned directory membership changed during execution",
        )
    if any(
        state != expected_item_map.get(key)
        for key, state in normalized_item_states.items()
        if key not in write_item_keys
    ):
        _block(manifest, unit_id, "unplanned item changed during execution")
    completed.append({
        "unit_id": str(unit_id),
        "post_states": declared_post_states,
        "exact_path_post_states": current_paths,
        "directory_post_states": current_memberships,
        "item_post_states": normalized_item_states,
        "completed_at": datetime.now(timezone.utc).isoformat(),
    })
    advances = receipt.setdefault("unit_precondition_advances", {})
    for future in units[current_index + 1:]:
        future_id = str(future.get("unit_id"))
        if future_id in authorized:
            continue
        future_path_keys = {
            state.get("path") for state in future.get("path_states") or ()
        }
        future_membership_keys = {
            state.get("path")
            for state in future.get("membership_states") or ()
        }
        future_item_keys = set(future.get("item_keys") or ())
        path_updates = {
            key: value for key, value in current_path_map.items()
            if key in future_path_keys and key in write_path_keys
        }
        membership_updates = {
            key: value for key, value in current_membership_map.items()
            if key in future_membership_keys
            and key in write_membership_keys
        }
        item_updates = {
            key: value for key, value in normalized_item_states.items()
            if key in future_item_keys and key in write_item_keys
        }
        if not (path_updates or membership_updates or item_updates):
            continue
        advance = advances.setdefault(future_id, {
            "advanced_by": [],
            "path_states": {},
            "membership_states": {},
            "item_states": {},
        })
        advance.setdefault("advanced_by", []).append(str(unit_id))
        advance.setdefault("path_states", {}).update(path_updates)
        advance.setdefault("membership_states", {}).update(
            membership_updates
        )
        advance.setdefault("item_states", {}).update(item_updates)
    receipt.update({
        "status": (
            "completed"
            if len(completed) == len(manifest.get("units") or ())
            else "running"
        ),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    })
    _persist_receipt(manifest)
    return receipt
