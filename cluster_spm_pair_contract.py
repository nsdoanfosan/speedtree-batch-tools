"""Normalize legacy unprefixed Cluster SPM outputs to the canonical SK name.

The production output is always ``SK_<name>.spm``.  An unprefixed
``<name>.spm`` is accepted only as a legacy input that may be copied once to
the canonical name.  It is never a publication target and is never used as the
SpeedTree/Blender/Unreal identity after the canonical file exists.

Inspection is always read-only.  Mutating operations use stable SHA-256
snapshots, sibling temporary files, ``fsync`` and ``os.replace``.  They never
choose a direction from timestamps.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


RECEIPT_KIND = "cluster_spm_output_name_normalization"
SCHEMA_VERSION = 2
BACKUP_SUBDIR = "_spm_backups"


class ClusterSpmPairError(RuntimeError):
    """Base error for Cluster SPM pair operations."""


class ClusterSpmPairPathError(ClusterSpmPairError):
    """Raised when a path cannot identify a canonical/mirror Cluster pair."""


class ClusterSpmPairConflictError(ClusterSpmPairError):
    """Raised when independent edits make an automatic publish unsafe."""


class ClusterSpmPairReceiptError(ClusterSpmPairError):
    """Raised when persisted lineage cannot be trusted."""


class ClusterSpmPairSourceChangedError(ClusterSpmPairError):
    """Raised when OneDrive, SpeedTree, or another process changes a source."""


class ClusterSpmPairBusyError(ClusterSpmPairError):
    """Raised when another process is already mutating the same pair."""


def _path_key(path):
    return os.path.normcase(os.path.abspath(str(path))).casefold()


def _utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _sha256_stream(handle):
    digest = hashlib.sha256()
    size = 0
    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
        digest.update(chunk)
        size += len(chunk)
    return digest.hexdigest(), size


def _snapshot(path, *, required=False):
    """Return one content-stable SHA-256/stat snapshot."""
    candidate = Path(path)
    for _attempt in range(2):
        try:
            before = candidate.stat()
        except OSError as exc:
            if required:
                raise ClusterSpmPairSourceChangedError(
                    f"required Cluster SPM is unavailable: {candidate}: {exc}"
                ) from exc
            return {
                "path": str(candidate),
                "exists": False,
                "sha256": None,
                "size": None,
                "mtime_ns": None,
            }
        try:
            with candidate.open("rb") as handle:
                digest, size = _sha256_stream(handle)
            after = candidate.stat()
        except OSError as exc:
            if required:
                raise ClusterSpmPairSourceChangedError(
                    f"Cluster SPM became unavailable while hashing: {candidate}: {exc}"
                ) from exc
            continue
        before_key = (before.st_size, before.st_mtime_ns)
        after_key = (after.st_size, after.st_mtime_ns)
        if before_key == after_key and size == after.st_size:
            return {
                "path": str(candidate.resolve()),
                "exists": True,
                "sha256": digest,
                "size": after.st_size,
                "mtime_ns": after.st_mtime_ns,
            }
    raise ClusterSpmPairSourceChangedError(
        f"Cluster SPM changed while hashing: {candidate}"
    )


def _same_content(left, right):
    return bool(
        isinstance(left, dict)
        and isinstance(right, dict)
        and left.get("exists") is True
        and right.get("exists") is True
        and left.get("sha256")
        and left.get("sha256") == right.get("sha256")
        and int(left.get("size")) == int(right.get("size"))
    )


def _same_snapshot(left, right):
    keys = ("exists", "sha256", "size", "mtime_ns")
    return all((left or {}).get(key) == (right or {}).get(key) for key in keys)


def _predicted_copy(source, target):
    return {
        "path": str(Path(target)),
        "exists": True,
        "sha256": source.get("sha256"),
        "size": source.get("size"),
        "mtime_ns": None,
        "predicted": True,
    }


def resolve_cluster_spm_pair(path):
    """Resolve either member of ``Cluster/SK_x.spm`` + ``Cluster/x.spm``."""
    candidate = Path(path).expanduser().resolve(strict=False)
    if candidate.suffix.casefold() != ".spm":
        raise ClusterSpmPairPathError(f"Cluster pair member is not an SPM: {candidate}")
    if candidate.parent.name.casefold() != "cluster":
        raise ClusterSpmPairPathError(
            f"Cluster pair member must be directly inside a Cluster folder: {candidate}"
        )
    if candidate.name.startswith("~"):
        raise ClusterSpmPairPathError(
            f"SpeedTree temporary SPM cannot be a Cluster pair member: {candidate}"
        )

    if candidate.name.casefold().startswith("sk_"):
        input_role = "canonical"
        canonical = candidate
        mirror_name = candidate.name[3:]
        if not mirror_name or mirror_name.casefold() == ".spm":
            raise ClusterSpmPairPathError(f"canonical Cluster SPM has no base name: {candidate}")
        mirror = candidate.with_name(mirror_name)
    else:
        input_role = "mirror"
        mirror = candidate
        canonical = candidate.with_name("SK_" + candidate.name)

    identity_text = "\n".join(sorted((_path_key(canonical), _path_key(mirror))))
    pair_id = hashlib.sha256(identity_text.encode("utf-8")).hexdigest()
    receipt_path = (
        candidate.parent
        / "reports"
        / f"{canonical.stem}_cluster_spm_pair.json"
    )
    return {
        "kind": "cluster_spm_pair",
        "pair_id": pair_id,
        "input_role": input_role,
        "canonical_spm": canonical,
        "mirror_spm": mirror,
        "receipt_path": receipt_path,
    }


def cluster_spm_pair_receipt_path(path):
    return resolve_cluster_spm_pair(path)["receipt_path"]


def _validate_fingerprint(value, label):
    if not isinstance(value, dict):
        raise ClusterSpmPairReceiptError(f"receipt {label} fingerprint is missing")
    if value.get("exists") is not True:
        raise ClusterSpmPairReceiptError(f"receipt {label} fingerprint is not a file")
    if not value.get("sha256") or value.get("size") is None:
        raise ClusterSpmPairReceiptError(f"receipt {label} fingerprint is incomplete")


def _validate_receipt(payload, pair):
    if not isinstance(payload, dict):
        raise ClusterSpmPairReceiptError("Cluster SPM pair receipt must contain an object")
    if payload.get("receipt_kind") != RECEIPT_KIND:
        raise ClusterSpmPairReceiptError("unexpected Cluster SPM pair receipt kind")
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise ClusterSpmPairReceiptError("unsupported Cluster SPM pair receipt version")
    if payload.get("status") != "complete":
        raise ClusterSpmPairReceiptError("Cluster SPM pair receipt is not complete")
    try:
        generation = int(payload.get("generation"))
    except (TypeError, ValueError) as exc:
        raise ClusterSpmPairReceiptError("Cluster SPM pair generation is invalid") from exc
    if generation < 1:
        raise ClusterSpmPairReceiptError("Cluster SPM pair generation must be positive")
    if str(payload.get("pair_id") or "") != pair["pair_id"]:
        raise ClusterSpmPairReceiptError("Cluster SPM pair receipt identity does not match")
    paths = payload.get("paths") or {}
    if _path_key(paths.get("canonical_output")) != _path_key(pair["canonical_spm"]):
        raise ClusterSpmPairReceiptError("receipt canonical path does not match this pair")
    if _path_key(paths.get("legacy_unprefixed_input")) != _path_key(pair["mirror_spm"]):
        raise ClusterSpmPairReceiptError("receipt legacy input path does not match this pair")
    after = payload.get("after") or {}
    _validate_fingerprint(after.get("canonical"), "after canonical")
    _validate_fingerprint(after.get("mirror"), "after mirror")
    if not _same_content(after["canonical"], after["mirror"]):
        raise ClusterSpmPairReceiptError("receipt completed with divergent pair content")
    return payload


def _load_receipt(pair):
    path = pair["receipt_path"]
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise ClusterSpmPairReceiptError(
            f"cannot read Cluster SPM pair receipt {path}: {exc}"
        ) from exc
    return _validate_receipt(payload, pair)


def inspect_cluster_spm_pair(path):
    """Read legacy-name normalization state without making the raw name live.

    Once the canonical ``SK_`` file exists it is authoritative.  A remaining
    unprefixed file is merely legacy evidence, even if it later diverges.
    """
    pair = resolve_cluster_spm_pair(path)
    canonical = _snapshot(pair["canonical_spm"])
    mirror = _snapshot(pair["mirror_spm"])
    conflicts = []
    warnings = []
    try:
        receipt = _load_receipt(pair)
        receipt_error = None
    except ClusterSpmPairReceiptError as exc:
        receipt = None
        receipt_error = str(exc)

    generation = int((receipt or {}).get("generation") or 0)
    status = "missing"
    action = "blocked"
    can_bootstrap = False
    can_publish = False

    if canonical["exists"]:
        status = "current"
        action = "none"
        if receipt_error:
            warnings.append(receipt_error)
        if mirror["exists"]:
            warnings.append(
                "legacy unprefixed output remains but is not a publication target"
            )
            if not _same_content(canonical, mirror):
                warnings.append(
                    "legacy unprefixed output differs and is intentionally ignored"
                )
    elif mirror["exists"]:
        status = "normalization_ready"
        action = "normalize_output_name"
        can_bootstrap = True
        if receipt_error:
            warnings.append(receipt_error)
    else:
        conflicts.append("neither canonical nor legacy Cluster SPM exists")

    return {
        **pair,
        "status": status,
        "action": action,
        "generation": generation,
        "canonical": canonical,
        "mirror": mirror,
        "receipt": receipt,
        "conflicts": conflicts,
        "warnings": warnings,
        "can_bootstrap": can_bootstrap,
        "can_publish": can_publish,
    }


def _atomic_write_json(path, payload):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    encoded = (
        json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    ).encode("utf-8")
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.",
        suffix=".tmp",
        dir=str(target.parent),
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _atomic_copy(source, target, expected_source):
    """Copy a verified stable source and atomically replace the target."""
    source = Path(source)
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    if not _same_snapshot(_snapshot(source, required=True), expected_source):
        raise ClusterSpmPairSourceChangedError(
            f"source fingerprint changed before copy: {source}"
        )
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent)
    )
    temporary = Path(temporary_name)
    try:
        digest = hashlib.sha256()
        size = 0
        with source.open("rb") as reader, os.fdopen(fd, "wb") as writer:
            for chunk in iter(lambda: reader.read(1024 * 1024), b""):
                writer.write(chunk)
                digest.update(chunk)
                size += len(chunk)
            writer.flush()
            os.fsync(writer.fileno())
        if digest.hexdigest() != expected_source.get("sha256") or size != int(
            expected_source.get("size")
        ):
            raise ClusterSpmPairSourceChangedError(
                f"source content changed during copy: {source}"
            )
        shutil.copystat(source, temporary)
        if not _same_snapshot(_snapshot(source, required=True), expected_source):
            raise ClusterSpmPairSourceChangedError(
                f"source fingerprint changed before atomic replace: {source}"
            )
        os.replace(temporary, target)
        source_after = _snapshot(source, required=True)
        target_after = _snapshot(target, required=True)
        if not _same_snapshot(source_after, expected_source):
            raise ClusterSpmPairSourceChangedError(
                f"source fingerprint changed after atomic replace: {source}"
            )
        if not _same_content(source_after, target_after):
            raise ClusterSpmPairError(
                f"atomic Cluster SPM copy verification failed: {source} -> {target}"
            )
        return target_after
    except OSError as exc:
        raise ClusterSpmPairError(
            f"atomic Cluster SPM copy failed: {source} -> {target}: {exc}"
        ) from exc
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


@contextmanager
def _pair_lock(pair):
    lock_path = pair["receipt_path"].with_suffix(
        pair["receipt_path"].suffix + ".lock"
    )
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as exc:
        raise ClusterSpmPairBusyError(
            f"Cluster SPM pair is already being updated: {lock_path}"
        ) from exc
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({"pid": os.getpid(), "created_at_utc": _utc_now()}, handle)
            handle.flush()
            os.fsync(handle.fileno())
        yield
    finally:
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass


def _verified_backup(source, target):
    source_snapshot = _snapshot(source, required=True)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    target_snapshot = _snapshot(target, required=True)
    if not _same_content(source_snapshot, target_snapshot):
        raise ClusterSpmPairError(f"Cluster SPM pair backup verification failed: {target}")
    return target_snapshot


def _create_backups(pair, generation, *, target, include_target):
    candidates = []
    target = Path(target)
    if include_target and target.is_file():
        candidates.append(("target_before", target))
    receipt = pair["receipt_path"]
    if receipt.is_file():
        candidates.append(("previous_receipt", receipt))
    if not candidates:
        return {"directory": None, "target_before": None, "previous_receipt": None}

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    directory = (
        pair["canonical_spm"].parent
        / BACKUP_SUBDIR
        / (
            f"cluster_spm_pair_{pair['pair_id'][:12]}_g{generation:04d}_"
            f"{stamp}_{uuid.uuid4().hex[:8]}"
        )
    )
    directory.mkdir(parents=True, exist_ok=False)
    result = {"directory": str(directory), "target_before": None, "previous_receipt": None}
    for label, source in candidates:
        backup = directory / f"{label}__{source.name}"
        fingerprint = _verified_backup(source, backup)
        result[label] = {"path": str(backup), "fingerprint": fingerprint}
    return result


def _restore_path(entry, target):
    if not entry:
        return
    backup = Path(entry["path"])
    _atomic_copy(backup, target, entry["fingerprint"])


def _rollback(pair, backup, *, target, target_existed, target_written):
    errors = []
    if target_written:
        try:
            if target_existed:
                _restore_path(backup.get("target_before"), target)
            else:
                Path(target).unlink(missing_ok=True)
        except Exception as exc:  # Preserve the original failure and backup evidence.
            errors.append(f"target rollback failed: {exc}")
    try:
        if backup.get("previous_receipt"):
            _restore_path(backup["previous_receipt"], pair["receipt_path"])
        else:
            pair["receipt_path"].unlink(missing_ok=True)
    except Exception as exc:
        errors.append(f"receipt rollback failed: {exc}")
    if errors:
        raise ClusterSpmPairError("; ".join(errors))


def _receipt_payload(pair, generation, operation, before, after, backup, previous):
    previous_receipt_sha256 = None
    if backup.get("previous_receipt"):
        previous_receipt_sha256 = backup["previous_receipt"]["fingerprint"]["sha256"]
    return {
        "receipt_kind": RECEIPT_KIND,
        "schema_version": SCHEMA_VERSION,
        "status": "complete",
        "pair_id": pair["pair_id"],
        "generation": generation,
        "operation": operation,
        "transaction_id": uuid.uuid4().hex,
        "created_at_utc": _utc_now(),
        "paths": {
            "canonical_output": str(pair["canonical_spm"]),
            "legacy_unprefixed_input": str(pair["mirror_spm"]),
        },
        "policy": {
            "canonical_role": "production_output",
            "legacy_role": "normalization_input_only",
            "normalization_direction": "legacy_unprefixed_to_sk",
            "publish_to_legacy_allowed": False,
        },
        "before": before,
        "after": after,
        "backup": backup,
        "lineage": {
            "previous_generation": int((previous or {}).get("generation") or 0),
            "previous_receipt_sha256": previous_receipt_sha256,
        },
        "invariants": {
            "source_unchanged_during_copy": True,
            "after_content_equal": _same_content(after["canonical"], after["mirror"]),
            "canonical_output_authoritative": True,
        },
    }


def _operation_result(status, operation, pair, generation, backup, before, after):
    return {
        "status": status,
        "operation": operation,
        "generation": generation,
        "canonical_spm": pair["canonical_spm"],
        "mirror_spm": pair["mirror_spm"],
        "receipt_path": pair["receipt_path"],
        "backup": backup,
        "before": before,
        "after": after,
    }


def bootstrap_cluster_authoring(raw_spm, *, dry_run=False):
    """Normalize one legacy unprefixed output into the canonical SK name."""
    pair = resolve_cluster_spm_pair(raw_spm)
    if pair["input_role"] != "mirror":
        raise ClusterSpmPairPathError(
            "bootstrap requires the unprefixed raw Cluster mirror path"
        )
    preview = inspect_cluster_spm_pair(raw_spm)
    if preview["canonical"]["exists"]:
        return _operation_result(
            "up_to_date", "normalize_legacy_output_to_canonical", pair,
            preview["generation"], None,
            {"canonical": preview["canonical"], "mirror": preview["mirror"]},
            {"canonical": preview["canonical"], "mirror": preview["mirror"]},
        )
    if not preview["can_bootstrap"]:
        raise ClusterSpmPairConflictError(
            f"Cluster authoring bootstrap is blocked ({preview['status']}): "
            + "; ".join(preview["conflicts"])
        )
    before = {"canonical": preview["canonical"], "mirror": preview["mirror"]}
    predicted = {
        "canonical": _predicted_copy(preview["mirror"], pair["canonical_spm"]),
        "mirror": preview["mirror"],
    }
    if dry_run:
        return _operation_result(
            "would_apply", "normalize_legacy_output_to_canonical", pair, 1, None, before, predicted
        )

    with _pair_lock(pair):
        current = inspect_cluster_spm_pair(raw_spm)
        if current["canonical"]["exists"]:
            return _operation_result(
                "up_to_date", "normalize_legacy_output_to_canonical", pair,
                current["generation"], None,
                {"canonical": current["canonical"], "mirror": current["mirror"]},
                {"canonical": current["canonical"], "mirror": current["mirror"]},
            )
        if not current["can_bootstrap"]:
            raise ClusterSpmPairConflictError(
                f"Cluster authoring bootstrap changed before apply ({current['status']})"
            )
        before = {"canonical": current["canonical"], "mirror": current["mirror"]}
        backup = _create_backups(
            pair, 1, target=pair["canonical_spm"], include_target=False
        )
        target_written = False
        try:
            # Treat the target as potentially changed before entering the
            # atomic helper: it can detect a source change after os.replace.
            # Rollback must still remove the newly-created canonical file.
            target_written = True
            _atomic_copy(
                pair["mirror_spm"], pair["canonical_spm"], current["mirror"]
            )
            after = {
                "canonical": _snapshot(pair["canonical_spm"], required=True),
                "mirror": _snapshot(pair["mirror_spm"], required=True),
            }
            if not _same_content(after["canonical"], after["mirror"]):
                raise ClusterSpmPairSourceChangedError(
                    "legacy output changed while SK name normalization was completing"
                )
            receipt = _receipt_payload(
                pair, 1, "normalize_legacy_output_to_canonical", before, after, backup, None
            )
            _atomic_write_json(pair["receipt_path"], receipt)
        except Exception as exc:
            try:
                _rollback(
                    pair, backup, target=pair["canonical_spm"],
                    target_existed=False, target_written=target_written,
                )
            except Exception as rollback_exc:
                raise ClusterSpmPairError(
                    f"bootstrap failed ({exc}); rollback also failed ({rollback_exc})"
                ) from exc
            raise
    return _operation_result(
        "applied", "normalize_legacy_output_to_canonical", pair, 1, backup, before, after
    )


def publish_cluster_atlas_mirror(
    canonical_spm,
    *,
    expected_generation=None,
    allow_mirror_overwrite=False,
    dry_run=False,
):
    """Reject the retired canonical-to-unprefixed output publication path."""
    raise ClusterSpmPairConflictError(
        "unprefixed Cluster outputs are legacy inputs, not publication targets"
    )


__all__ = [
    "BACKUP_SUBDIR",
    "RECEIPT_KIND",
    "SCHEMA_VERSION",
    "ClusterSpmPairBusyError",
    "ClusterSpmPairConflictError",
    "ClusterSpmPairError",
    "ClusterSpmPairPathError",
    "ClusterSpmPairReceiptError",
    "ClusterSpmPairSourceChangedError",
    "bootstrap_cluster_authoring",
    "cluster_spm_pair_receipt_path",
    "inspect_cluster_spm_pair",
    "publish_cluster_atlas_mirror",
    "resolve_cluster_spm_pair",
]
