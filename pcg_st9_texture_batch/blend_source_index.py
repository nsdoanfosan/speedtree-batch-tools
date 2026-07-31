"""Content-addressed Blender source-image index session.

Only rows produced by Blender for the exact current ``.blend`` SHA-256 can
answer source-image queries.  A persisted row is merely a memoized Blender
result; it becomes usable only after the current file bytes hash to the same
identity.  Size, mtime, regex scans, and caller declarations are never source
authority.
"""
from __future__ import annotations

import contextvars
import hashlib
import os
import re
import threading
from contextlib import contextmanager
from pathlib import Path


SOURCE_INDEX_SCHEMA_VERSION = 1
SOURCE_INDEX_CACHE_VERSION = 2
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_ACTIVE_SESSION = contextvars.ContextVar(
    "pcg_blend_source_index_session", default=None
)


class BlendSourceIndexError(RuntimeError):
    """Raised when exact Blender source evidence cannot be established."""


def path_key(value) -> str:
    return os.path.normcase(os.path.abspath(os.fspath(value))).casefold()


def file_sha256(path) -> str:
    """Return exact bytes SHA-256, rejecting a file changed during hashing."""
    source = Path(path)
    try:
        before = source.stat()
    except OSError as exc:
        raise BlendSourceIndexError(f"blend file is unavailable: {source}: {exc}") from exc
    hasher = hashlib.sha256()
    try:
        with source.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                hasher.update(chunk)
        after = source.stat()
    except OSError as exc:
        raise BlendSourceIndexError(f"blend file could not be hashed: {source}: {exc}") from exc
    # Stat fields are used only to detect an in-flight mutation.  Reuse is
    # authorized exclusively by the digest below.
    if (
        before.st_size != after.st_size
        or before.st_mtime_ns != after.st_mtime_ns
        or getattr(before, "st_ino", None) != getattr(after, "st_ino", None)
    ):
        raise BlendSourceIndexError(f"blend changed while it was being hashed: {source}")
    return hasher.hexdigest()


def _image_tokens(images) -> frozenset[str]:
    tokens = set()
    for image in images or ():
        if not isinstance(image, dict):
            raise BlendSourceIndexError("Blender source-index image row is not an object")
        for field in ("name", "filepath", "filepath_raw"):
            value = str(image.get(field) or "").strip()
            if value:
                tokens.add(Path(value).name.casefold())
    return frozenset(tokens)


def _validate_row(row, *, expected_path, expected_sha256) -> frozenset[str]:
    if not isinstance(row, dict):
        raise BlendSourceIndexError("Blender source-index row is missing")
    if row.get("schema_version") != SOURCE_INDEX_SCHEMA_VERSION:
        raise BlendSourceIndexError("unsupported Blender source-index row schema")
    if row.get("status") != "ok" or row.get("indexed_by_blender") is not True:
        raise BlendSourceIndexError("source-index row is not authoritative Blender output")
    row_path = row.get("blend")
    if not row_path or path_key(row_path) != path_key(expected_path):
        raise BlendSourceIndexError("source-index blend path does not match the request")
    row_sha = str(row.get("blend_sha256") or "").strip().casefold()
    if not _SHA256_RE.fullmatch(row_sha):
        raise BlendSourceIndexError("source-index blend SHA-256 is malformed")
    if row_sha != str(expected_sha256).casefold():
        raise BlendSourceIndexError("source-index blend SHA-256 does not match current content")
    images = row.get("images")
    if not isinstance(images, list):
        raise BlendSourceIndexError("source-index images must be a list")
    return _image_tokens(images)


class BlendSourceIndexSession:
    """One audit's immutable-content source-index lookup session."""

    def __init__(self, persisted_entries=None):
        self._entries = persisted_entries if isinstance(persisted_entries, dict) else {}
        self._sha_by_path = {}
        self._pending = {}
        self._lock = threading.RLock()
        self._changed = False

    @property
    def changed(self) -> bool:
        return self._changed

    def begin_pass(self) -> None:
        """Start a new filesystem pass without discarding validated Blender rows."""
        with self._lock:
            self._sha_by_path.clear()
            self._pending.clear()

    def _current_sha_locked(self, blend_path) -> str:
        key = path_key(blend_path)
        digest = self._sha_by_path.get(key)
        if digest is None:
            digest = file_sha256(blend_path)
            self._sha_by_path[key] = digest
        return digest

    def lookup(self, blend_path) -> frozenset[str]:
        """Return Blender-proven image tokens or mark this exact SHA pending."""
        blend_path = Path(blend_path)
        key = path_key(blend_path)
        with self._lock:
            digest = self._current_sha_locked(blend_path)
            row = self._entries.get(key)
            try:
                return _validate_row(
                    row,
                    expected_path=blend_path,
                    expected_sha256=digest,
                )
            except BlendSourceIndexError:
                self._pending[key] = {
                    "blend": str(blend_path.resolve()),
                    "blend_sha256": digest,
                }
                return frozenset()

    def pending_requests(self) -> list[dict]:
        with self._lock:
            return [
                dict(self._pending[key])
                for key in sorted(self._pending)
            ]

    def install_report(self, report, requests) -> int:
        """Install one all-or-nothing Blender report for an exact request set."""
        if not isinstance(report, dict):
            raise BlendSourceIndexError("Blender source-index report is missing")
        if report.get("schema_version") != SOURCE_INDEX_SCHEMA_VERSION:
            raise BlendSourceIndexError("unsupported Blender source-index report schema")
        if report.get("status") != "ok":
            raise BlendSourceIndexError(
                "Blender source indexing failed: " + str(report.get("error") or "unknown error")
            )
        rows = report.get("rows")
        if not isinstance(rows, list):
            raise BlendSourceIndexError("Blender source-index report rows are missing")

        expected = {}
        for request in requests or ():
            if not isinstance(request, dict):
                raise BlendSourceIndexError("source-index request is not an object")
            blend = request.get("blend")
            digest = str(request.get("blend_sha256") or "").casefold()
            if not blend or not _SHA256_RE.fullmatch(digest):
                raise BlendSourceIndexError("source-index request identity is incomplete")
            key = path_key(blend)
            if key in expected:
                raise BlendSourceIndexError("duplicate source-index request")
            expected[key] = {"blend": blend, "blend_sha256": digest}

        by_key = {}
        for row in rows:
            if not isinstance(row, dict) or not row.get("blend"):
                raise BlendSourceIndexError("source-index report contains an invalid row")
            key = path_key(row["blend"])
            if key in by_key:
                raise BlendSourceIndexError("source-index report contains a duplicate blend")
            by_key[key] = row
        if set(by_key) != set(expected):
            missing = sorted(set(expected) - set(by_key))
            extra = sorted(set(by_key) - set(expected))
            raise BlendSourceIndexError(
                f"source-index report request mismatch: missing={missing}, extra={extra}"
            )

        validated = {}
        for key, request in expected.items():
            # Re-hash after Blender exits.  A file changed after request creation
            # cannot inherit the just-produced row even when size/mtime match.
            current_sha = file_sha256(request["blend"])
            if current_sha != request["blend_sha256"]:
                raise BlendSourceIndexError(
                    f"blend changed during source indexing: {request['blend']}"
                )
            _validate_row(
                by_key[key],
                expected_path=request["blend"],
                expected_sha256=current_sha,
            )
            validated[key] = dict(by_key[key])

        with self._lock:
            self._entries.update(validated)
            for key, request in expected.items():
                self._sha_by_path[key] = request["blend_sha256"]
                self._pending.pop(key, None)
            self._changed = bool(validated) or self._changed
        return len(validated)

    def register_row(self, row, expected_blend_path) -> frozenset[str]:
        """Register the Blender-authored row returned by a just-finished job."""
        current_sha = file_sha256(expected_blend_path)
        tokens = _validate_row(
            row,
            expected_path=expected_blend_path,
            expected_sha256=current_sha,
        )
        key = path_key(expected_blend_path)
        with self._lock:
            self._entries[key] = dict(row)
            self._sha_by_path[key] = current_sha
            self._pending.pop(key, None)
            self._changed = True
        return tokens


@contextmanager
def use_blend_source_index(session):
    token = _ACTIVE_SESSION.set(session)
    try:
        yield session
    finally:
        _ACTIVE_SESSION.reset(token)


def active_blend_source_index():
    return _ACTIVE_SESSION.get()


def lookup_blend_source_images(blend_path, persisted_entries) -> frozenset[str]:
    """Lookup through the active audit; standalone callers fail if indexing is due."""
    session = active_blend_source_index()
    if session is not None:
        return session.lookup(blend_path)
    session = BlendSourceIndexSession(persisted_entries)
    result = session.lookup(blend_path)
    if session.pending_requests():
        raise BlendSourceIndexError(
            "source-image matching requires an authoritative Blender index: "
            + str(blend_path)
        )
    return result
