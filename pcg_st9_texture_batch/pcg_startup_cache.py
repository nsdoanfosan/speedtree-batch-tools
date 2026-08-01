"""Bounded content-identity caches for PCG board startup.

Every persisted row is memoization only.  Callers must first reproduce the
current input identity; metadata alone never authorizes a cache hit or a
mutation.  Recursive discovery is breadth-first and capped so a bad root
cannot turn tab selection into an unbounded filesystem walk.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
from collections import deque
from pathlib import Path

from artifact_content_key import sampled_file_content_snapshot


CONTENT_CACHE_SCHEMA_VERSION = 1
DEFAULT_MAX_DISCOVERY_DIRECTORIES = 20_000
DEFAULT_MAX_DISCOVERY_FILES = 5_000
DEFAULT_MAX_EVIDENCE_FILES = 2_048
_CACHE_LOCK = threading.RLock()


class BoundedDiscoveryError(RuntimeError):
    """A configured filesystem/evidence bound was exceeded."""


class ContentIdentityError(RuntimeError):
    """Current inputs could not be captured as one stable identity."""


def path_key(value) -> str:
    return os.path.normcase(os.path.abspath(os.fspath(value))).casefold()


def json_safe(value):
    if isinstance(value, dict):
        return {str(key): json_safe(nested) for key, nested in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(nested) for nested in value]
    if isinstance(value, (set, frozenset)):
        return sorted(json_safe(nested) for nested in value)
    if isinstance(value, os.PathLike):
        return str(value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def canonical_json_sha256(value) -> str:
    encoded = json.dumps(
        json_safe(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _stable_content_row(path, memo=None):
    candidate = Path(path).expanduser().absolute()
    key = path_key(candidate)
    if memo is not None and key in memo:
        return dict(memo[key])
    if not candidate.is_file():
        raise ContentIdentityError(
            f"Content-identity input is unavailable: {candidate}"
        )
    try:
        snapshot = sampled_file_content_snapshot(candidate)
    except OSError as exc:
        raise ContentIdentityError(
            f"Content-identity input could not be read: {candidate}: {exc}"
        ) from exc
    row = {
        "path": key,
        "size": snapshot["size"],
        "mtime_ns": snapshot["mtime_ns"],
        "fingerprint": snapshot["fingerprint"],
        "fingerprint_algorithm": snapshot["fingerprint_algorithm"],
    }
    if memo is not None:
        memo[key] = dict(row)
    return row


def content_identity(paths, *, membership=None, memo=None, max_files=None):
    """Capture stable bounded content keys plus caller-owned membership data."""
    unique = {
        path_key(path): Path(path).expanduser().absolute()
        for path in paths or ()
    }
    limit = DEFAULT_MAX_EVIDENCE_FILES if max_files is None else int(max_files)
    if len(unique) > limit:
        raise BoundedDiscoveryError(
            f"Content identity needs {len(unique)} files; bound is {limit}"
        )
    rows = [_stable_content_row(unique[key], memo=memo) for key in sorted(unique)]
    stable_rows = [
        {
            "path": row["path"],
            "size": row["size"],
            "fingerprint": row["fingerprint"],
            "fingerprint_algorithm": row["fingerprint_algorithm"],
        }
        for row in rows
    ]
    stable_membership = sorted(str(value) for value in membership or ())
    return {
        "algorithm": "sha256-of-bounded-content-keys-v1",
        "sha256": canonical_json_sha256({
            "files": stable_rows,
            "membership": stable_membership,
        }),
        "file_count": len(rows),
        "files": rows,
        "membership": stable_membership,
    }


def bounded_recursive_files(
    roots,
    *,
    suffix,
    exclude_path=None,
    max_directories=DEFAULT_MAX_DISCOVERY_DIRECTORIES,
    max_files=DEFAULT_MAX_DISCOVERY_FILES,
):
    """Return matching files from a bounded, non-symlink BFS traversal."""
    wanted = str(suffix).casefold()
    queue = deque()
    seen_roots = set()
    for raw_root in roots or ():
        root = Path(raw_root).expanduser().absolute()
        key = path_key(root)
        if key in seen_roots or not root.is_dir():
            continue
        seen_roots.add(key)
        queue.append(root)

    files = {}
    directory_count = 0
    while queue:
        directory = queue.popleft()
        directory_count += 1
        if directory_count > int(max_directories):
            raise BoundedDiscoveryError(
                "Recursive discovery exceeded the directory bound "
                f"({max_directories}) under {directory}"
            )
        try:
            entries = sorted(
                os.scandir(directory),
                key=lambda entry: entry.name.casefold(),
            )
        except OSError as exc:
            raise ContentIdentityError(
                f"Recursive discovery could not enumerate {directory}: {exc}"
            ) from exc
        for entry in entries:
            candidate = Path(entry.path)
            try:
                if entry.is_dir(follow_symlinks=False):
                    queue.append(candidate)
                    continue
                if not entry.is_file(follow_symlinks=False):
                    continue
            except OSError as exc:
                raise ContentIdentityError(
                    f"Recursive discovery could not inspect {candidate}: {exc}"
                ) from exc
            if candidate.suffix.casefold() != wanted:
                continue
            if exclude_path is not None and exclude_path(candidate):
                continue
            files[path_key(candidate)] = candidate
            if len(files) > int(max_files):
                raise BoundedDiscoveryError(
                    "Recursive discovery exceeded the file bound "
                    f"({max_files}) under {directory}"
                )
    return [files[key] for key in sorted(files)], {
        "directory_count": directory_count,
        "file_count": len(files),
        "max_directories": int(max_directories),
        "max_files": int(max_files),
    }


def atomic_write_json(path, payload):
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
        temporary = None
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except OSError:
                pass


class ContentAddressedJsonCache:
    """Small multi-namespace cache validated by a caller-produced identity."""

    def __init__(self, path, kind, *, max_entries=16):
        self.path = Path(path)
        self.kind = str(kind)
        self.max_entries = int(max_entries)

    def _load(self):
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            return {"entries": {}}
        if (
            not isinstance(payload, dict)
            or payload.get("schema_version") != CONTENT_CACHE_SCHEMA_VERSION
            or payload.get("kind") != self.kind
            or not isinstance(payload.get("entries"), dict)
        ):
            return {"entries": {}}
        return payload

    def get(self, namespace, identity_sha256):
        with _CACHE_LOCK:
            payload = self._load()
            row = payload["entries"].get(str(namespace))
            if (
                not isinstance(row, dict)
                or row.get("identity_sha256") != str(identity_sha256)
                or "value" not in row
                or row.get("value_sha256")
                != canonical_json_sha256(row.get("value"))
            ):
                return None
            return row["value"]

    def put(self, namespace, identity_sha256, value):
        self.put_many([(namespace, identity_sha256, value)])

    def put_many(self, rows):
        with _CACHE_LOCK:
            payload = self._load()
            entries = payload.setdefault("entries", {})
            protected = set()
            for namespace, identity_sha256, value in rows:
                namespace = str(namespace)
                protected.add(namespace)
                entries[namespace] = {
                    "identity_sha256": str(identity_sha256),
                    "value": json_safe(value),
                    "value_sha256": canonical_json_sha256(value),
                }
            if len(entries) > self.max_entries:
                for key in sorted(entries)[:len(entries) - self.max_entries]:
                    if key not in protected:
                        entries.pop(key, None)
            atomic_write_json(self.path, {
                "schema_version": CONTENT_CACHE_SCHEMA_VERSION,
                "kind": self.kind,
                "entries": entries,
            })


__all__ = [
    "BoundedDiscoveryError",
    "ContentAddressedJsonCache",
    "ContentIdentityError",
    "atomic_write_json",
    "bounded_recursive_files",
    "canonical_json_sha256",
    "content_identity",
    "json_safe",
    "path_key",
]
