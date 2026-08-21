"""Small transactional SPM text reader/writer for explicit source tools."""

import gzip
import os
from pathlib import Path

from spm_generator_sync.spm_generator_sync import read_spm_snapshot


def read_spm(path):
    text, _compressed, _fingerprint = read_spm_snapshot(Path(path))
    return text


def write_spm(path, text):
    """Replace one SPM only if its bytes did not change after the read."""
    candidate = Path(path)
    _current_text, compressed, fingerprint = read_spm_snapshot(candidate)
    payload = str(text).encode("utf-8")
    if compressed:
        payload = gzip.compress(payload, compresslevel=9, mtime=0)
    _latest_text, _latest_compressed, latest = read_spm_snapshot(candidate)
    if latest != fingerprint:
        raise RuntimeError(f"SPM changed before write: {candidate}")
    temporary = candidate.with_name(
        f".{candidate.name}.{os.getpid()}.spm-write.tmp"
    )
    try:
        temporary.write_bytes(payload)
        os.replace(temporary, candidate)
    finally:
        if temporary.exists():
            temporary.unlink()


__all__ = ["read_spm", "write_spm"]
