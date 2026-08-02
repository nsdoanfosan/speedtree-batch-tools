"""Shared content-key contract for live Cluster audit artifact records.

Artifact records prove current content with one of three keys, in priority
order: a full SHA-256, an explicitly identified fingerprint algorithm, or the
legacy full-file BLAKE2b-128 fingerprint when no algorithm identifier exists.
Records with neither ``sha256`` nor ``fingerprint`` have no content key and
must be rejected by stability consumers.

Large live-audit artifacts use ``SAMPLED_FINGERPRINT_ALGORITHM``.  It hashes
eight evenly spaced 64 KiB windows (or the whole file below that 512 KiB
budget), including file size and window offsets in the digest.  Stat metadata
is checked before and after every attempt so a file changing while sampled is
never published as a stable snapshot.  This is a bounded content-derived key,
not a full-file digest; persisted receipts can still use full SHA-256 where
that stronger archival identity is required.
"""

from __future__ import annotations

import hashlib
import struct
import threading
from concurrent.futures import Future
from pathlib import Path


SHA256_ALGORITHM = "sha256-full-v1"
LEGACY_FINGERPRINT_ALGORITHM = "blake2b-128-full-v1"
SAMPLED_FINGERPRINT_ALGORITHM = "blake2b-128-sampled-8x64k-v1"
SAMPLED_WINDOW_BYTES = 64 * 1024
SAMPLED_WINDOW_COUNT = 8
SAMPLED_MAX_READ_BYTES = SAMPLED_WINDOW_BYTES * SAMPLED_WINDOW_COUNT
_STABLE_READ_ATTEMPTS = 2
_SAMPLED_DOMAIN = b"speedtree-live-artifact-sample-v1\0"


class ArtifactContentKeyChangedError(RuntimeError):
    """The artifact did not remain one stable snapshot while it was read."""


class ConcurrentContentDigestMemo:
    """Refresh-local single-flight memo for exact artifact digests.

    Callers own the cache lifetime and must key entries with an already
    observed path/size/mtime identity.  The memo never turns metadata into
    authority: one caller still computes the full digest for every unseeded
    key, while concurrent consumers wait for that exact result instead of
    opening the same large file again.
    """

    def __init__(self):
        self._lock = threading.RLock()
        self._values = {}
        self._pending = {}
        self._counts = {
            "hits": 0,
            "misses": 0,
            "waits": 0,
            "seeds": 0,
        }

    def get_or_compute(self, key, compute):
        owner = False
        with self._lock:
            future = self._pending.get(key)
            if future is not None:
                self._counts["waits"] += 1
            elif key in self._values:
                self._counts["hits"] += 1
                return self._values[key]
            else:
                future = Future()
                self._pending[key] = future
                self._counts["misses"] += 1
                owner = True
        if not owner:
            return future.result()
        try:
            result = compute()
        except BaseException as exc:
            with self._lock:
                self._pending.pop(key, None)
                self._values.pop(key, None)
                future.set_exception(exc)
            raise
        with self._lock:
            existing = self._values.get(key)
            if existing is not None and existing != result:
                self._pending.pop(key, None)
                self._values.pop(key, None)
                error = ValueError(
                    "Conflicting exact result for one artifact identity"
                )
                future.set_exception(error)
                raise error
            self._values[key] = result
            self._pending.pop(key, None)
            future.set_result(result)
        return result

    def get_or_compute_verified(self, key, compute):
        """Single-flight a fresh authority read and reject identity conflicts.

        Unlike ``get_or_compute``, an already stored value is not authority for
        a later read.  The caller recomputes current exact evidence while
        concurrent callers still share that one read.  Reusing metadata after
        a same-size/restored-mtime replacement therefore fails closed instead
        of silently returning the earlier digest or validation result.
        """
        owner = False
        with self._lock:
            future = self._pending.get(key)
            if future is None:
                future = Future()
                self._pending[key] = future
                self._counts["misses"] += 1
                owner = True
            else:
                self._counts["waits"] += 1
        if not owner:
            return future.result()
        try:
            result = compute()
        except BaseException as exc:
            with self._lock:
                self._pending.pop(key, None)
                self._values.pop(key, None)
                future.set_exception(exc)
            raise
        with self._lock:
            existing = self._values.get(key)
            if existing is not None and existing != result:
                self._pending.pop(key, None)
                self._values.pop(key, None)
                error = ValueError(
                    "Artifact content changed without an identity change"
                )
                future.set_exception(error)
                raise error
            self._values[key] = result
            self._pending.pop(key, None)
            future.set_result(result)
        return result

    def seed(self, key, value):
        """Publish a digest already computed from the same exact bytes."""
        with self._lock:
            existing = self._values.get(key)
            if existing is not None and existing != value:
                raise ValueError(
                    "Conflicting exact digest for one artifact identity"
                )
            if existing is None:
                self._values[key] = value
                self._counts["seeds"] += 1
        return value

    def get(self, key, default=None):
        with self._lock:
            return self._values.get(key, default)

    def items(self):
        with self._lock:
            return tuple(self._values.items())

    def metrics(self):
        with self._lock:
            logical_bytes = sum(
                int(key[1])
                for key in self._values
                if (
                    isinstance(key, tuple)
                    and len(key) == 3
                    and isinstance(key[1], int)
                )
            )
            return {
                **self._counts,
                "unique_files": len(self._values),
                "logical_bytes": logical_bytes,
                "pending": len(self._pending),
            }

    def __contains__(self, key):
        with self._lock:
            return key in self._values

    def __getitem__(self, key):
        with self._lock:
            return self._values[key]

    def __setitem__(self, key, value):
        self.seed(key, value)

    def __len__(self):
        with self._lock:
            return len(self._values)


def _stat_identity(stat):
    return stat.st_size, stat.st_mtime_ns


def _sample_windows(size):
    if size <= SAMPLED_MAX_READ_BYTES:
        return ((0, size),)
    last_start = size - SAMPLED_WINDOW_BYTES
    return tuple(
        (
            (last_start * index) // (SAMPLED_WINDOW_COUNT - 1),
            SAMPLED_WINDOW_BYTES,
        )
        for index in range(SAMPLED_WINDOW_COUNT)
    )


def _digest_open_file(handle, algorithm, size):
    if algorithm == SHA256_ALGORITHM:
        digest = hashlib.sha256()
        bytes_read = 0
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
            bytes_read += len(chunk)
        return digest.hexdigest(), bytes_read == size

    if algorithm == LEGACY_FINGERPRINT_ALGORITHM:
        digest = hashlib.blake2b(digest_size=16)
        bytes_read = 0
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
            bytes_read += len(chunk)
        return digest.hexdigest(), bytes_read == size

    if algorithm == SAMPLED_FINGERPRINT_ALGORITHM:
        digest = hashlib.blake2b(digest_size=16)
        digest.update(_SAMPLED_DOMAIN)
        digest.update(struct.pack("<Q", size))
        complete = True
        for offset, length in _sample_windows(size):
            handle.seek(offset)
            chunk = handle.read(length)
            if len(chunk) != length:
                complete = False
            digest.update(struct.pack("<QQ", offset, len(chunk)))
            digest.update(chunk)
        return digest.hexdigest(), complete

    raise ValueError(f"Unsupported artifact content-key algorithm: {algorithm}")


def file_content_key_snapshot(path, algorithm):
    """Return one stat-bracketed content key for ``algorithm``."""
    candidate = Path(path)
    for _attempt in range(_STABLE_READ_ATTEMPTS):
        before = candidate.stat()
        with candidate.open("rb") as handle:
            digest, complete = _digest_open_file(
                handle,
                algorithm,
                before.st_size,
            )
        after = candidate.stat()
        if complete and _stat_identity(before) == _stat_identity(after):
            return {
                "algorithm": algorithm,
                "digest": digest,
                "size": after.st_size,
                "mtime_ns": after.st_mtime_ns,
            }
    raise ArtifactContentKeyChangedError(
        f"Artifact changed while calculating its content key: {candidate}"
    )


def sampled_file_content_snapshot(path):
    """Return the bounded fingerprint record fields for one stable file."""
    snapshot = file_content_key_snapshot(path, SAMPLED_FINGERPRINT_ALGORITHM)
    return {
        "fingerprint": snapshot["digest"],
        "fingerprint_algorithm": snapshot["algorithm"],
        "size": snapshot["size"],
        "mtime_ns": snapshot["mtime_ns"],
    }


def artifact_record_content_key(record):
    """Interpret an artifact record according to the shared key contract."""
    if not isinstance(record, dict):
        raise TypeError("Artifact record must be an object")

    sha256 = record.get("sha256")
    if isinstance(sha256, str) and sha256:
        return {
            "field": "sha256",
            "algorithm": SHA256_ALGORITHM,
            "digest": sha256,
        }

    fingerprint = record.get("fingerprint")
    if isinstance(fingerprint, str) and fingerprint:
        algorithm = record.get("fingerprint_algorithm")
        if algorithm is None:
            algorithm = LEGACY_FINGERPRINT_ALGORITHM
        if algorithm not in {
            LEGACY_FINGERPRINT_ALGORITHM,
            SAMPLED_FINGERPRINT_ALGORITHM,
        }:
            raise ValueError(
                "Unsupported artifact fingerprint algorithm: "
                + str(algorithm)
            )
        return {
            "field": "fingerprint",
            "algorithm": algorithm,
            "digest": fingerprint,
        }

    return None
