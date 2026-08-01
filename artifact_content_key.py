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
