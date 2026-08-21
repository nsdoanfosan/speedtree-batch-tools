"""Bounded retention for regenerable SpeedTree artifacts.

Every apply is re-audited against the live filesystem immediately before an
unlink.  Production backups are eligible only when an unambiguous live original
still exists; multi-file recovery sets without adequate ownership evidence stay
visible but are never deleted speculatively.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import stat
import time
import uuid
import zipfile
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

from process_lifecycle import (
    _current_process_start_identity,
    process_identity_is_alive,
)
from shared_job_queue import (
    InterprocessMutex,
    QueueError,
    SharedJobQueue,
    default_queue_state_path,
)

from speedtree_pipeline_contract import (
    BACKUP_DIRECTORY_NAMES,
    BACKUP_FILENAME_RE,
    MANUAL_COPY_FILENAME_RE,
)


LOG_SCOPE = "logs"
RETRY_SCOPE = "retry_progress"
PRODUCTION_BACKUP_SCOPE = "production_backups"
PCG_REPORT_SCOPE = "pcg_reports"
SPM_REPORT_SCOPE = "spm_reports"
SK_CACHE_SCOPE = "sk_cache"
SHARED_CACHE_SCOPE = "shared_cache"
PROCESS_RECEIPT_SCOPE = "process_receipts"
ROOT_DIAGNOSTIC_SCOPE = "root_diagnostics"
QUEUE_STATE_SCOPE = "queue_state"
SUPPORTED_SCOPES = frozenset(
    {
        LOG_SCOPE,
        RETRY_SCOPE,
        PRODUCTION_BACKUP_SCOPE,
        PCG_REPORT_SCOPE,
        SPM_REPORT_SCOPE,
        SK_CACHE_SCOPE,
        SHARED_CACHE_SCOPE,
        PROCESS_RECEIPT_SCOPE,
        ROOT_DIAGNOSTIC_SCOPE,
        QUEUE_STATE_SCOPE,
    }
)
DEFAULT_SCOPES = (
    LOG_SCOPE,
    PCG_REPORT_SCOPE,
    SPM_REPORT_SCOPE,
    SK_CACHE_SCOPE,
    SHARED_CACHE_SCOPE,
    RETRY_SCOPE,
    PROCESS_RECEIPT_SCOPE,
    ROOT_DIAGNOSTIC_SCOPE,
    QUEUE_STATE_SCOPE,
    PRODUCTION_BACKUP_SCOPE,
)

# Scope roots are identities, not caller-selected scan locations.  Keeping the
# production root explicit prevents a typo or a forged plan from widening a
# retention walk to D:\, OneDrive, or the repository root.
REPOSITORY_LOG_ROOT = Path(__file__).resolve().parent / "sk_batch" / "logs"
REPOSITORY_ROOT = Path(__file__).resolve().parent
REPOSITORY_UNREAL_WAIT_REFERENCES = (
    REPOSITORY_ROOT / "sk_batch" / "unreal_wait_references.json"
)
PCG_REPORT_ROOT = REPOSITORY_ROOT / "pcg_st9_texture_batch" / "reports"
SPM_REPORT_ROOT = REPOSITORY_ROOT / "spm_generator_sync" / "reports"
SK_CACHE_ROOT = REPOSITORY_ROOT / "sk_batch" / "cache"
PRODUCTION_TREE_ROOT = Path(r"D:\OneDrive\Forestportfolio")
PRODUCTION_SCAN_RELATIVE_ROOTS = (
    Path("02_nature") / "Tree",
    Path("Texture"),
    Path("00_common") / "MaterialLibrary" / "Tiling" / "textures",
    Path("substanceDesigner"),
)

HARD_MAX_BYTES = 10 * 1024**3
DEFAULT_SAFETY_HEADROOM_BYTES = 256 * 1024**2
DEFAULT_TARGET_BYTES = HARD_MAX_BYTES - DEFAULT_SAFETY_HEADROOM_BYTES
DEFAULT_MAX_AGE_SECONDS = 3 * 24 * 60 * 60
# Hash small metadata/log files so same-size rewrites cannot evade the apply
# recheck.  Large/OneDrive assets use immutable stat identity to avoid costly
# cache hydration and multi-gigabyte startup reads.
MAX_HASH_BYTES = 1024**2
RETENTION_LOCK_TIMEOUT_SECONDS = 120.0
RESERVATION_MAX_AGE_SECONDS = 24 * 60 * 60

RETRY_RECEIPT_FILENAME_RE = re.compile(
    r"^retry_\d{8}T\d{6}_[0-9a-f]{32}\.json$", re.IGNORECASE
)
PRE_REPAIR_BLEND_RE = re.compile(
    r"_pre_repair_\d{8}_\d{6}(?:_\d{6})?\.blend$", re.IGNORECASE
)
ATOMIC_TEMP_RE = re.compile(
    r"^\..+\.\d+\.\d+\.[0-9a-f]{32}\.tmp$", re.IGNORECASE
)
GENERATED_LOG_SUFFIXES = frozenset({".json", ".log", ".fbx"})
GENERATED_DIRECTORY_SCOPES = frozenset(
    {
        PCG_REPORT_SCOPE,
        SPM_REPORT_SCOPE,
        SK_CACHE_SCOPE,
        SHARED_CACHE_SCOPE,
        PROCESS_RECEIPT_SCOPE,
    }
)
GENERATED_DIRECTORY_SUFFIXES = frozenset(
    {
        ".blend",
        ".blend1",
        ".csv",
        ".fbx",
        ".json",
        ".log",
        ".png",
        ".sbsar",
        ".stmat",
        ".tga",
        ".tmp",
        ".tsv",
        ".txt",
        ".zip",
    }
)
RETAINED_RECOVERY_ARCHIVE_RE = re.compile(
    r"^\.(?P<transaction>.+)\.retention_delete_[0-9a-f]{32}\.zip$",
    re.IGNORECASE,
)
_BACKUP_SERIES_SUFFIX_RE = re.compile(
    r"(?:"
    r"\.(?:codex_backup|skbatch_backup|pcgtex_backup|skbatch-rescue)"
    r"(?:[._-].*)?"
    r"|\.texture_slot_backup_\d{8}_\d{6}(?:_\d{6})?"
    r"|\.apply_rollback_backup"
    r"|\.preimage"
    r"|\.pre_xml_root_fix_\d{8}(?:_\d{6}(?:_\d{6})?)?"
    r")\.spm$",
    re.IGNORECASE,
)
_SYNC_BACKUP_SERIES_RE = re.compile(
    r"^(?P<operation>__spm_sync_(?:preflight|verify)|\.__spm_pass_repair)_.*\.spm$",
    re.IGNORECASE,
)
_WINDOWS_REPARSE_ATTRIBUTE = 0x400
_MAX_RECEIPT_BYTES = 32 * 1024**2
BACKUP_BUNDLE_MANIFEST_FILENAME = ".speedtree-backup-bundle.json"
BACKUP_BUNDLE_MANIFEST_KIND = "speedtree_backup_transaction_manifest"
BACKUP_BUNDLE_MANIFEST_SCHEMA_VERSION = 1
BACKUP_BUNDLE_PRODUCER_SCHEMA_VERSION = 1
REGISTERED_BACKUP_MANIFEST_PRODUCERS = frozenset(
    {
        "atlas_cluster_normalization",
        "cluster_card_metadata_repair",
        "pcg_texture_audit",
        "spm_generator_sync",
        "spm_legacy_cluster_marker",
        "spm_problem_node_marker",
        "stale_node_recovery",
        "texture_normalize",
    }
)
MAINTENANCE_RESULT_KIND = "speedtree_artifact_maintenance_result"

_GENERATED_TIMESTAMP_RE = re.compile(
    r"(?<!\d)(?P<date>20\d{6})[_T-](?P<time>\d{6})(?:_(?P<micro>\d{1,6}))?(?!\d)"
)
_GENERATED_BACKUP_SIBLING_RE = re.compile(
    r"(?i)(?:[._-](?:skbatch|pcgtex|codex)_backup(?:_before)?[._-].*)"
    r"\.(?:blend|sbs|spm)$"
)
_BACKUP_OPERATION_SUFFIX_RE = re.compile(
    r"(?i)(?:\.(?:atlas_target_remove|before_pcg_generator_connect)_[^.]*)$"
)
_NUMBERED_BACKUP_PREFIX_RE = re.compile(r"^\d+[_-](?=.+\.[^.]+$)")
_REGENERABLE_BACKUP_AUX_RE = re.compile(
    r"(?i)(?:cache|checkpoint|manifest|queue|receipt|report|rollback|transaction)"
)
_CLOUD_PLACEHOLDER_ATTRIBUTES = 0x1000 | 0x40000 | 0x400000


@dataclass(frozen=True)
class RetentionPolicy:
    """Maximum age plus a strict byte ceiling.

    ``max_bytes`` is exclusive: a total exactly equal to it is over budget.
    ``keep_count`` remains only for API compatibility and never protects a
    file beyond ``max_age_seconds`` or from the strict capacity pass.
    """

    keep_count: int
    max_age_seconds: float
    max_bytes: int
    dry_run: bool = False
    include_manual_copies: bool = False
    include_retained_recovery_archives: bool = False

    def __post_init__(self):
        if int(self.keep_count) < 0:
            raise ValueError("keep_count must be non-negative")
        if float(self.max_age_seconds) < 0:
            raise ValueError("max_age_seconds must be non-negative")
        if int(self.max_bytes) < 0:
            raise ValueError("max_bytes must be non-negative")
        if int(self.max_bytes) > HARD_MAX_BYTES:
            raise ValueError("max_bytes cannot exceed the 10 GiB hard limit")


DEFAULT_RETENTION_POLICIES = {
    LOG_SCOPE: RetentionPolicy(
        keep_count=0,
        max_age_seconds=DEFAULT_MAX_AGE_SECONDS,
        max_bytes=DEFAULT_TARGET_BYTES,
    ),
    RETRY_SCOPE: RetentionPolicy(
        keep_count=0,
        max_age_seconds=DEFAULT_MAX_AGE_SECONDS,
        max_bytes=DEFAULT_TARGET_BYTES,
    ),
    PRODUCTION_BACKUP_SCOPE: RetentionPolicy(
        keep_count=0,
        max_age_seconds=DEFAULT_MAX_AGE_SECONDS,
        max_bytes=DEFAULT_TARGET_BYTES,
        include_manual_copies=False,
        include_retained_recovery_archives=True,
    ),
}
for _scope in (
    PCG_REPORT_SCOPE,
    SPM_REPORT_SCOPE,
    SK_CACHE_SCOPE,
    SHARED_CACHE_SCOPE,
    PROCESS_RECEIPT_SCOPE,
    ROOT_DIAGNOSTIC_SCOPE,
    QUEUE_STATE_SCOPE,
):
    DEFAULT_RETENTION_POLICIES[_scope] = RetentionPolicy(
        keep_count=0,
        max_age_seconds=DEFAULT_MAX_AGE_SECONDS,
        max_bytes=DEFAULT_TARGET_BYTES,
    )


def _lexical_path(value):
    """Normalize spelling without resolving links/junctions."""
    return Path(
        os.path.abspath(os.path.normpath(os.fspath(Path(value).expanduser())))
    )


def _canonical_path(value, *, strict=False):
    return _lexical_path(value).resolve(strict=strict)


def _path_key(value):
    return os.path.normcase(str(_lexical_path(value))).casefold()


def _canonical_key(value):
    return os.path.normcase(str(_canonical_path(value))).casefold()


def _is_within_lexical(path, root):
    try:
        _lexical_path(path).relative_to(_lexical_path(root))
        return True
    except ValueError:
        return False


def _is_within_canonical(path, root):
    try:
        _canonical_path(path).relative_to(_canonical_path(root))
        return True
    except (ValueError, OSError, RuntimeError):
        return False


def _is_reparse_stat(value):
    attributes = int(getattr(value, "st_file_attributes", 0))
    return bool(attributes & _WINDOWS_REPARSE_ATTRIBUTE) and not bool(
        attributes & _CLOUD_PLACEHOLDER_ATTRIBUTES
    )


def _is_cloud_placeholder_stat(value):
    return bool(
        int(getattr(value, "st_file_attributes", 0))
        & _CLOUD_PLACEHOLDER_ATTRIBUTES
    )


def _is_reparse_path(path):
    try:
        info = _lexical_path(path).lstat()
    except (FileNotFoundError, OSError):
        return False
    return stat.S_ISLNK(info.st_mode) or _is_reparse_stat(info)


def _has_reparse_component(path, root):
    """Reject a lexical child whose route crosses a symlink/junction."""
    candidate = _lexical_path(path)
    boundary = _lexical_path(root)
    try:
        relative = candidate.relative_to(boundary)
    except ValueError:
        return True
    current = boundary
    if _is_reparse_path(current):
        return True
    for part in relative.parts:
        current = current / part
        if _is_reparse_path(current):
            return True
    return False


def _retry_root():
    local_app_data = os.environ.get("LOCALAPPDATA")
    if not local_app_data:
        raise ValueError(
            "LOCALAPPDATA is required for the retry_progress retention scope"
        )
    return _lexical_path(local_app_data) / "SpeedTreeBatchTools" / "retry_progress"


def _local_runtime_root():
    local_app_data = os.environ.get("LOCALAPPDATA")
    if not local_app_data:
        raise ValueError("LOCALAPPDATA is required for local artifact retention")
    return _lexical_path(local_app_data) / "SpeedTreeBatchTools"


def expected_root_for_scope(scope):
    if scope == LOG_SCOPE:
        return _lexical_path(REPOSITORY_LOG_ROOT)
    if scope == PCG_REPORT_SCOPE:
        return _lexical_path(PCG_REPORT_ROOT)
    if scope == SPM_REPORT_SCOPE:
        return _lexical_path(SPM_REPORT_ROOT)
    if scope == SK_CACHE_SCOPE:
        return _lexical_path(SK_CACHE_ROOT)
    if scope == SHARED_CACHE_SCOPE:
        return _local_runtime_root() / "cache"
    if scope == RETRY_SCOPE:
        return _retry_root()
    if scope == PROCESS_RECEIPT_SCOPE:
        return _local_runtime_root() / "process_receipts"
    if scope == ROOT_DIAGNOSTIC_SCOPE:
        return _lexical_path(REPOSITORY_ROOT)
    if scope == QUEUE_STATE_SCOPE:
        return _local_runtime_root()
    if scope == PRODUCTION_BACKUP_SCOPE:
        return _lexical_path(PRODUCTION_TREE_ROOT)
    raise ValueError(f"unsupported retention scope: {scope}")


def _validate_exact_scope_root(scope, root):
    if scope not in SUPPORTED_SCOPES:
        raise ValueError(f"unsupported retention scope: {scope}")
    supplied = _lexical_path(root)
    expected = expected_root_for_scope(scope)
    if _path_key(supplied) != _path_key(expected):
        raise ValueError(
            f"retention root does not match exact {scope} root: "
            f"expected={expected} actual={supplied}"
        )
    if _canonical_key(supplied) != _canonical_key(expected):
        raise ValueError(
            f"retention root canonical identity changed: "
            f"expected={_canonical_path(expected)} "
            f"actual={_canonical_path(supplied)}"
        )
    if supplied.exists() and _is_reparse_path(supplied):
        raise ValueError(f"retention root cannot be a reparse point: {supplied}")
    return supplied


def is_manual_copy_inventory_artifact(path):
    """Return true only for the explicit Explorer duplicate suffix."""
    return bool(MANUAL_COPY_FILENAME_RE.search(Path(path).name))


def generated_production_backup_kind(path):
    """Classify explicit backup inventory, including Explorer copies."""
    candidate = _lexical_path(path)
    backup_parts = [
        part.casefold()
        for part in candidate.parts
        if part.casefold() in BACKUP_DIRECTORY_NAMES
    ]
    if backup_parts and RETAINED_RECOVERY_ARCHIVE_RE.fullmatch(candidate.name):
        return "retained_recovery_archive"
    if backup_parts:
        return f"backup_directory:{backup_parts[0]}"
    folded_parts = [part.casefold() for part in candidate.parts]
    if (
        candidate.suffix.casefold() == ".sbs"
        and any(
            folded_parts[index : index + 2] == ["backups", "sbs"]
            for index in range(max(0, len(folded_parts) - 1))
        )
    ):
        return "backup_directory:backups/sbs"
    if is_manual_copy_inventory_artifact(candidate):
        return "manual_copy_backup"
    if BACKUP_FILENAME_RE.search(candidate.name) or (
        _GENERATED_BACKUP_SIBLING_RE.search(candidate.name)
    ):
        return "backup_sibling"
    return None


def generated_log_kind(path, root):
    candidate = _lexical_path(path)
    if not _is_within_lexical(candidate, root):
        return None
    if candidate.name.casefold() == "state_recovery.log":
        return "state_recovery_log"
    if candidate.suffix.casefold() in GENERATED_LOG_SUFFIXES:
        return f"log_artifact:{candidate.suffix.casefold()[1:]}"
    if PRE_REPAIR_BLEND_RE.search(candidate.name):
        return "pre_repair_blend"
    if ATOMIC_TEMP_RE.search(candidate.name):
        return "abandoned_atomic_temp"
    return None


def generated_retry_kind(path, root):
    candidate = _lexical_path(path)
    if not _is_within_lexical(candidate, root):
        return None
    if RETRY_RECEIPT_FILENAME_RE.fullmatch(candidate.name):
        return "retry_receipt"
    if ATOMIC_TEMP_RE.search(candidate.name):
        return "abandoned_atomic_temp"
    return None


def generated_directory_kind(path, root, scope):
    candidate = _lexical_path(path)
    if scope not in GENERATED_DIRECTORY_SCOPES:
        return None
    if not _is_within_lexical(candidate, root):
        return None
    if candidate.name == BACKUP_BUNDLE_MANIFEST_FILENAME:
        return "generated_manifest"
    suffix = candidate.suffix.casefold()
    if suffix in GENERATED_DIRECTORY_SUFFIXES or ATOMIC_TEMP_RE.search(
        candidate.name
    ):
        return f"generated_{scope}:{suffix.lstrip('.') or 'file'}"
    return None


def generated_root_diagnostic_kind(path, root):
    candidate = _lexical_path(path)
    if candidate.parent != _lexical_path(root):
        return None
    if re.fullmatch(
        r"speedtree_batch_tools_error\.log(?:\.\d+)?",
        candidate.name,
        re.IGNORECASE,
    ):
        return "root_error_log"
    return None


def generated_queue_state_kind(path, root):
    """Classify the live queue and abandoned atomic queue writes only."""
    candidate = _lexical_path(path)
    if candidate.parent != _lexical_path(root):
        return None
    if candidate.name.casefold() == "shared_job_queue.json":
        return "live_queue_state"
    if (
        candidate.name.casefold().startswith(".shared_job_queue.json.")
        and candidate.name.casefold().endswith(".tmp")
    ):
        return "abandoned_queue_temp"
    return None


def _classify(scope, path, root):
    if scope == LOG_SCOPE:
        return generated_log_kind(path, root)
    if scope == RETRY_SCOPE:
        return generated_retry_kind(path, root)
    if scope in GENERATED_DIRECTORY_SCOPES:
        return generated_directory_kind(path, root, scope)
    if scope == ROOT_DIAGNOSTIC_SCOPE:
        return generated_root_diagnostic_kind(path, root)
    if scope == QUEUE_STATE_SCOPE:
        return generated_queue_state_kind(path, root)
    if scope == PRODUCTION_BACKUP_SCOPE:
        if not _is_within_lexical(path, root):
            return None
        return generated_production_backup_kind(path)
    raise ValueError(f"unsupported retention scope: {scope}")


def _walk_strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for nested in value.values():
            yield from _walk_strings(nested)
    elif isinstance(value, (list, tuple)):
        for nested in value:
            yield from _walk_strings(nested)


def referenced_paths_from_receipts(receipt_paths, *, allowed_roots):
    """Return current in-scope references, resolving receipt-relative paths."""
    roots = tuple(_lexical_path(root) for root in allowed_roots)
    protected = set()
    for value in receipt_paths:
        receipt = _lexical_path(value)
        if any(_is_within_lexical(receipt, root) for root in roots):
            protected.add(_path_key(receipt))
        try:
            if receipt.stat().st_size > _MAX_RECEIPT_BYTES:
                continue
            payload = json.loads(receipt.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError):
            continue
        for text in _walk_strings(payload):
            if not text or len(text) > 32_768:
                continue
            candidate = Path(text).expanduser()
            if not candidate.is_absolute():
                candidate = receipt.parent / candidate
            referenced = _lexical_path(candidate)
            if any(_is_within_lexical(referenced, root) for root in roots):
                protected.add(_path_key(referenced))
    return protected


def _pending_unreal_paths_from_reference_receipt(*, allowed_roots):
    """Protect only artifacts still waiting for the Unreal headless consumer."""
    roots = tuple(_lexical_path(root) for root in allowed_roots)
    try:
        if REPOSITORY_UNREAL_WAIT_REFERENCES.stat().st_size > _MAX_RECEIPT_BYTES:
            return set()
        payload = json.loads(
            REPOSITORY_UNREAL_WAIT_REFERENCES.read_text(encoding="utf-8")
        )
    except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError):
        return set()
    if not isinstance(payload, dict):
        return set()

    rows = payload.get("items")
    if not isinstance(rows, list):
        return set()
    protected = set()
    manifests = []
    for state in rows:
        if not isinstance(state, dict):
            continue
        if state.get("push_status_kind") != "exported_pending_unreal":
            continue
        for field in (state.get("push_paths"), state.get("push_export_cache")):
            for text in _walk_strings(field):
                if not text or len(text) > 32_768:
                    continue
                candidate = Path(text).expanduser()
                if not candidate.is_absolute():
                    candidate = REPOSITORY_UNREAL_WAIT_REFERENCES.parent / candidate
                referenced = _lexical_path(candidate)
                if not any(_is_within_lexical(referenced, root) for root in roots):
                    continue
                protected.add(_path_key(referenced))
                if referenced.suffix.casefold() == ".json":
                    manifests.append(referenced)

    protected.update(
        referenced_paths_from_receipts(manifests, allowed_roots=roots)
    )
    return protected


def _iter_regular_inventory(root):
    """Walk without following or descending through reparse directories."""
    boundary = _lexical_path(root)
    if not boundary.is_dir():
        return
    for current, directory_names, file_names in os.walk(
        boundary, topdown=True, followlinks=False
    ):
        owner = Path(current)
        kept_directories = []
        for name in directory_names:
            child = owner / name
            if name.casefold() in {".git", ".pytest_cache", "__pycache__"}:
                continue
            if not _is_reparse_path(child):
                kept_directories.append(name)
        directory_names[:] = sorted(kept_directories, key=str.casefold)
        for name in sorted(file_names, key=str.casefold):
            candidate = owner / name
            if _has_reparse_component(candidate, boundary):
                continue
            try:
                file_stat = candidate.lstat()
            except (FileNotFoundError, OSError):
                continue
            if stat.S_ISREG(file_stat.st_mode) and not _is_reparse_stat(file_stat):
                yield candidate, file_stat


def _is_backup_namespace_path(path, boundary):
    try:
        relative = _lexical_path(path).relative_to(_lexical_path(boundary))
    except ValueError:
        return False
    folded = [part.casefold() for part in relative.parts]
    if any(part in BACKUP_DIRECTORY_NAMES for part in folded):
        return True
    return any(
        folded[index : index + 2] == ["backups", "sbs"]
        for index in range(max(0, len(folded) - 1))
    )


def _iter_production_backup_inventory(root):
    """Discover backup namespaces without statting every OneDrive original."""
    boundary = _lexical_path(root)
    if not boundary.is_dir():
        return
    scan_roots = [
        boundary / relative
        for relative in PRODUCTION_SCAN_RELATIVE_ROOTS
        if (boundary / relative).is_dir()
    ]
    if not scan_roots:
        scan_roots = [boundary]
    for scan_root in scan_roots:
        for current, directory_names, file_names in os.walk(
            scan_root, topdown=True, followlinks=False
        ):
            owner = Path(current)
            try:
                discovery_depth = len(owner.relative_to(scan_root).parts)
            except ValueError:
                discovery_depth = 99
            inside_namespace = _is_backup_namespace_path(owner, boundary)
            if not inside_namespace and discovery_depth >= 4:
                directory_names[:] = [
                    name
                    for name in directory_names
                    if name.casefold() in BACKUP_DIRECTORY_NAMES
                    or (
                        owner.name.casefold() == "backups"
                        and name.casefold() == "sbs"
                    )
                ]
            directory_names[:] = sorted(
                (
                    name
                    for name in directory_names
                    if name.casefold()
                    not in {".git", ".pytest_cache", "__pycache__"}
                    and not _is_reparse_path(owner / name)
                ),
                key=str.casefold,
            )
            for name in sorted(file_names, key=str.casefold):
                candidate = owner / name
                if not inside_namespace:
                    folded_name = name.casefold()
                    if not (
                        "backup" in folded_name
                        or folded_name.startswith("__spm_sync_")
                        or folded_name.startswith(".__spm_pass_repair_")
                        or " - copy" in folded_name
                        or " - \ubcf5\uc0ac\ubcf8" in folded_name
                    ):
                        continue
                    if generated_production_backup_kind(candidate) is None:
                        continue
                try:
                    file_stat = candidate.lstat()
                except (FileNotFoundError, OSError):
                    continue
                if stat.S_ISREG(file_stat.st_mode) and not _is_reparse_stat(file_stat):
                    yield candidate, file_stat


def _iter_scope_inventory(scope, root):
    if scope == PRODUCTION_BACKUP_SCOPE:
        yield from _iter_production_backup_inventory(root) or ()
        return
    if scope in {ROOT_DIAGNOSTIC_SCOPE, QUEUE_STATE_SCOPE}:
        boundary = _lexical_path(root)
        if not boundary.is_dir():
            return
        try:
            candidates = sorted(boundary.iterdir(), key=lambda path: path.name.casefold())
        except OSError:
            return
        for candidate in candidates:
            if _classify(scope, candidate, boundary) is None:
                continue
            try:
                file_stat = candidate.lstat()
            except (FileNotFoundError, OSError):
                continue
            if stat.S_ISREG(file_stat.st_mode) and not _is_reparse_stat(file_stat):
                yield candidate, file_stat
        return
    yield from _iter_regular_inventory(root) or ()


def _backup_namespace(path):
    candidate = _lexical_path(path)
    for index, part in enumerate(candidate.parts):
        if part.casefold() in BACKUP_DIRECTORY_NAMES:
            namespace = Path(*candidate.parts[: index + 1])
            return namespace, Path(*candidate.parts[index + 1 :])
    folded = [part.casefold() for part in candidate.parts]
    for index in range(max(0, len(folded) - 1)):
        if folded[index : index + 2] == ["backups", "sbs"]:
            namespace = Path(*candidate.parts[: index + 2])
            return namespace, Path(*candidate.parts[index + 2 :])
    return None, None


def _backup_original_filename(path):
    candidate = Path(path)
    name = _NUMBERED_BACKUP_PREFIX_RE.sub("", candidate.name)
    if is_manual_copy_inventory_artifact(name):
        return MANUAL_COPY_FILENAME_RE.sub(candidate.suffix, name)
    stem = Path(name).stem
    suffix = Path(name).suffix
    marker = re.search(
        r"(?i)(?:[._-](?:skbatch|pcgtex|codex)_backup(?:_before)?[._-])",
        stem,
    )
    if marker:
        stem = stem[: marker.start()]
    else:
        stem = _BACKUP_OPERATION_SUFFIX_RE.sub("", stem)
    return stem + suffix


def _production_original_index(inventory, boundary):
    index = {}
    for candidate, file_stat in inventory:
        if candidate.suffix.casefold() not in {
            ".blend",
            ".fbx",
            ".png",
            ".sbs",
            ".spm",
            ".tga",
            ".tif",
            ".tiff",
        }:
            continue
        if generated_production_backup_kind(candidate) is not None:
            continue
        if (
            not stat.S_ISREG(file_stat.st_mode)
            or _is_reparse_stat(file_stat)
            or not _is_within_lexical(candidate, boundary)
        ):
            continue
        index.setdefault(candidate.name.casefold(), []).append(candidate)
    return index


def _legacy_backup_manifest_originals(bundle_root, boundary):
    """Return strictly validated backup-to-live mappings from legacy manifests.

    The atlas normalization producer predates the common sealed-manifest format,
    but records absolute ``source`` and ``backup`` paths plus the copied byte
    count.  Each row is accepted independently only when both paths remain in
    their exact roots, the backup identity matches the row, and the source is a
    regular non-backup file that still exists.  File contents are never opened,
    which avoids hydrating OneDrive placeholders.
    """
    root = _lexical_path(bundle_root)
    allowed = _lexical_path(boundary)
    manifest = root / "backup_manifest.json"
    try:
        manifest_stat = manifest.lstat()
        if (
            not stat.S_ISREG(manifest_stat.st_mode)
            or _is_reparse_stat(manifest_stat)
            or manifest_stat.st_size > _MAX_RECEIPT_BYTES
            or _has_reparse_component(manifest, allowed)
        ):
            return {}, ""
        payload = json.loads(manifest.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError):
        return {}, ""
    if not isinstance(payload, dict) or payload.get("status") != "ok":
        return {}, ""
    if _path_key(payload.get("backup_root", "")) != _path_key(root):
        return {}, ""
    rows = payload.get("files")
    if not isinstance(rows, list) or len(rows) > 100_000:
        return {}, ""

    mappings = {}
    seen_backups = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            backup = _lexical_path(row["backup"])
            source = _lexical_path(row["source"])
            declared_size = int(row["size"])
        except (KeyError, OSError, TypeError, ValueError):
            continue
        backup_key = _path_key(backup)
        if backup_key in seen_backups:
            mappings.pop(backup_key, None)
            continue
        seen_backups.add(backup_key)
        if (
            declared_size < 0
            or not _is_within_lexical(backup, root)
            or not _is_within_canonical(backup, root)
            or not _is_within_lexical(source, allowed)
            or not _is_within_canonical(source, allowed)
            or _is_backup_namespace_path(source, allowed)
            or generated_production_backup_kind(source) is not None
            or _has_reparse_component(backup, root)
            or _has_reparse_component(source, allowed)
        ):
            continue
        try:
            backup_stat = backup.lstat()
            source_stat = source.lstat()
        except (FileNotFoundError, OSError):
            continue
        if (
            not stat.S_ISREG(backup_stat.st_mode)
            or _is_reparse_stat(backup_stat)
            or int(backup_stat.st_size) != declared_size
            or not stat.S_ISREG(source_stat.st_mode)
            or _is_reparse_stat(source_stat)
        ):
            continue
        mappings[backup_key] = source
    return mappings, str(manifest)


def _verified_original_for_backup(path, kind, boundary, original_index):
    """Return one unambiguous live original without opening OneDrive data."""
    candidate = _lexical_path(path)
    if kind == "retained_recovery_archive":
        return None
    original_name = _backup_original_filename(candidate)
    direct = []
    namespace, _relative = _backup_namespace(candidate)
    if kind in {"backup_sibling", "manual_copy_backup"}:
        direct.append(candidate.with_name(original_name))
    if namespace is not None:
        direct.append(namespace.parent / original_name)
        if namespace.name.casefold() == "sbs" and namespace.parent.name.casefold() == "backups":
            direct.append(namespace.parent.parent / original_name)
            direct.append(namespace.parent.parent / "sbs" / original_name)
    candidates = []
    for value in (*direct, *(original_index.get(original_name.casefold()) or ())):
        value = _lexical_path(value)
        if _path_key(value) == _path_key(candidate):
            continue
        if any(_path_key(value) == _path_key(existing) for existing in candidates):
            continue
        candidates.append(value)
    valid = []
    for value in candidates:
        if not _is_within_lexical(value, boundary):
            continue
        if generated_production_backup_kind(value) is not None:
            continue
        try:
            file_stat = value.lstat()
        except (FileNotFoundError, OSError):
            continue
        if not stat.S_ISREG(file_stat.st_mode) or _is_reparse_stat(file_stat):
            continue
        valid.append(value)
    direct_keys = {_path_key(value) for value in direct}
    direct_valid = [value for value in valid if _path_key(value) in direct_keys]
    if len(direct_valid) == 1:
        return direct_valid[0]
    if not direct_valid and len(valid) == 1:
        return valid[0]
    return None


def _strip_manual_copy_suffix(name):
    return MANUAL_COPY_FILENAME_RE.sub(".spm", name).casefold()


def _backup_series_name(name):
    """Collapse every registered backup revision to its live/operation series."""
    folded = str(name).casefold()
    sync = _SYNC_BACKUP_SERIES_RE.fullmatch(folded)
    if sync:
        return sync.group("operation").casefold()
    stripped = _BACKUP_SERIES_SUFFIX_RE.sub("", folded)
    if stripped != folded:
        return stripped
    return Path(folded).stem


def _bundle_facts(scope, path, kind):
    """Return bundle/series identity, atomic mode, uncertainty and root."""
    candidate = _lexical_path(path)
    if scope != PRODUCTION_BACKUP_SCOPE:
        key = _path_key(candidate)
        return key, _path_key(candidate.parent), "single_file", "", candidate
    if kind == "manual_copy_backup":
        return (
            _path_key(candidate),
            f"{_path_key(candidate.parent)}::{_strip_manual_copy_suffix(candidate.name)}",
            "single_file",
            "",
            candidate,
        )
    if kind == "retained_recovery_archive":
        return (
            _path_key(candidate),
            f"{_path_key(candidate.parent)}::retained_recovery_archives",
            "single_file",
            "",
            candidate,
        )
    if kind == "backup_sibling":
        return (
            _path_key(candidate),
            f"{_path_key(candidate.parent)}::{_backup_series_name(candidate.name)}",
            "single_file",
            "",
            candidate,
        )
    namespace, relative = _backup_namespace(candidate)
    if namespace is None or relative is None:
        return (
            _path_key(candidate),
            _path_key(candidate.parent),
            "none",
            "unknown_backup_format",
            candidate,
        )
    if len(relative.parts) == 1 and candidate.suffix.casefold() in {
        ".blend",
        ".fbx",
        ".png",
        ".sbs",
        ".spm",
        ".tga",
        ".tif",
        ".tiff",
    }:
        return (
            _path_key(candidate),
            f"{_path_key(namespace)}::{_backup_series_name(candidate.name)}",
            "single_file",
            "",
            candidate,
        )
    # A nested transaction folder can contain an SPM, receipts and auxiliary
    # recovery files.  Without a transaction manifest, its complete membership
    # and atomic deletion semantics are not provable.
    top = relative.parts[0] if relative.parts else candidate.name
    return (
        f"uncertain::{_path_key(namespace / top)}",
        _path_key(namespace),
        "none",
        "uncertain_multi_file_recovery_bundle",
        namespace / top,
    )


def _creation_evidence_ns(file_stat):
    """Use conservative creation/change evidence so copy2 mtime cannot age a copy."""
    birth = getattr(file_stat, "st_birthtime_ns", None)
    if birth is None:
        # Windows st_ctime is creation time.  On POSIX it is metadata-change
        # time; treating that as a younger floor is conservative.
        birth = getattr(file_stat, "st_ctime_ns", 0)
    return max(int(file_stat.st_mtime_ns), int(birth or 0))


def _generated_time_ns(path, file_stat):
    """Prefer an explicit tool timestamp over copy-preserved/source metadata."""
    matches = list(_GENERATED_TIMESTAMP_RE.finditer(str(_lexical_path(path))))
    if matches:
        match = matches[-1]
        micro = (match.group("micro") or "").ljust(6, "0")[:6]
        text = f"{match.group('date')}{match.group('time')}{micro}"
        try:
            parsed = datetime.strptime(text, "%Y%m%d%H%M%S%f")
            return int(parsed.timestamp() * 1_000_000_000)
        except (OSError, OverflowError, ValueError):
            pass
    return _creation_evidence_ns(file_stat)


def _entry(
    path,
    kind,
    bundle_id,
    series_id,
    atomic_mode,
    uncertainty,
    bundle_root,
    file_stat,
):
    candidate = _lexical_path(path)
    return {
        "path": str(candidate),
        "kind": kind,
        "bundle_id": bundle_id,
        "series_id": series_id,
        "atomic_mode": atomic_mode,
        "bundle_root": str(_lexical_path(bundle_root)),
        "bytes": int(file_stat.st_size),
        "mtime_ns": int(file_stat.st_mtime_ns),
        "ctime_ns": int(file_stat.st_ctime_ns),
        "effective_time_ns": _generated_time_ns(candidate, file_stat),
        "device": int(file_stat.st_dev),
        "inode": int(file_stat.st_ino),
        "sha256": "",
        "identity_mode": "pending",
        "original_path": "",
        "original_verified": False,
        "action": "keep",
        "retention_basis": uncertainty or "within_budget",
    }


def _same_stat_identity(left, right):
    return (
        stat.S_ISREG(right.st_mode)
        and not _is_reparse_stat(right)
        and int(left.st_size) == int(right.st_size)
        and int(left.st_mtime_ns) == int(right.st_mtime_ns)
        and int(left.st_ctime_ns) == int(right.st_ctime_ns)
        and int(left.st_dev) == int(right.st_dev)
        and int(left.st_ino) == int(right.st_ino)
    )


def _sha256_with_identity(path, expected_stat):
    digest = hashlib.sha256()
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        opened_stat = os.fstat(descriptor)
        if not _same_stat_identity(expected_stat, opened_stat):
            raise OSError("identity changed before hashing")
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
        final_stat = os.fstat(descriptor)
        if not _same_stat_identity(expected_stat, final_stat):
            raise OSError("identity changed while hashing")
    finally:
        os.close(descriptor)
    return digest.hexdigest()


def _bundle_tree_inventory(bundle_root, *, exclude_manifest=False):
    """Return exact regular-file/directory membership or a safety error."""
    root = _lexical_path(bundle_root)
    if not root.is_dir() or _is_reparse_path(root):
        raise ValueError(f"backup bundle root is unavailable or reparsed: {root}")
    files = {}
    directories = set()
    for current, directory_names, file_names in os.walk(
        root, topdown=True, followlinks=False
    ):
        owner = Path(current)
        safe_directories = []
        for name in sorted(directory_names, key=str.casefold):
            child = owner / name
            if _is_reparse_path(child):
                raise ValueError(f"backup bundle contains a reparse directory: {child}")
            relative = child.relative_to(root).as_posix()
            directories.add(relative)
            safe_directories.append(name)
        directory_names[:] = safe_directories
        for name in sorted(file_names, key=str.casefold):
            candidate = owner / name
            if _is_reparse_path(candidate):
                raise ValueError(f"backup bundle contains a reparse file: {candidate}")
            try:
                file_stat = candidate.lstat()
            except OSError as exc:
                raise ValueError(f"backup bundle member is unreadable: {candidate}") from exc
            if not stat.S_ISREG(file_stat.st_mode):
                raise ValueError(f"backup bundle member is not regular: {candidate}")
            relative = candidate.relative_to(root).as_posix()
            if exclude_manifest and relative == BACKUP_BUNDLE_MANIFEST_FILENAME:
                continue
            files[relative] = (candidate, file_stat)
    return files, directories


def _validate_manifest_relative_path(value):
    text = str(value or "").replace("\\", "/")
    relative = Path(text)
    if (
        not text
        or text.startswith("/")
        or relative.is_absolute()
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        raise ValueError(f"unsafe backup bundle relative path: {value!r}")
    return Path(*relative.parts).as_posix()


def _atomic_write_json(path, payload):
    target = _lexical_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.parent / (
        f".{target.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    )
    encoded = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode(
        "utf-8"
    )
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "wb", closefd=True) as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
        except BaseException:
            # fdopen owns descriptor after successful construction.
            raise
        os.replace(temporary, target)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return target


def seal_backup_transaction(
    bundle_root,
    member_paths,
    *,
    producer,
    transaction_id=None,
):
    """Seal one producer-owned nested backup transaction.

    The caller must enumerate every file currently in the transaction.  The
    helper refuses missing, extra, linked, or out-of-root members rather than
    inferring transaction membership from filenames.
    """
    root = _lexical_path(bundle_root)
    namespace, relative = _backup_namespace(root)
    if namespace is None or relative is None or len(relative.parts) != 1:
        raise ValueError(
            "backup transaction root must be one direct child of a registered "
            "backup namespace"
        )
    manifest = root / BACKUP_BUNDLE_MANIFEST_FILENAME
    if manifest.exists():
        raise FileExistsError(f"backup transaction is already sealed: {manifest}")
    producer = str(producer or "").strip()
    if producer not in REGISTERED_BACKUP_MANIFEST_PRODUCERS:
        raise ValueError(
            f"producer is not registered to seal backup transactions: {producer!r}"
        )
    actual, directories = _bundle_tree_inventory(root, exclude_manifest=True)
    declared = set()
    for value in member_paths:
        candidate = Path(value)
        if not candidate.is_absolute():
            candidate = root / candidate
        candidate = _lexical_path(candidate)
        if not _is_within_lexical(candidate, root) or not _is_within_canonical(
            candidate, root
        ):
            raise ValueError(f"backup transaction member is outside root: {candidate}")
        declared.add(_validate_manifest_relative_path(candidate.relative_to(root).as_posix()))
    if declared != set(actual):
        missing = sorted(set(actual) - declared)
        extra = sorted(declared - set(actual))
        raise ValueError(
            "backup transaction membership is incomplete: "
            f"undeclared={missing} absent={extra}"
        )
    members = []
    for relative_path in sorted(actual, key=str.casefold):
        candidate, file_stat = actual[relative_path]
        members.append(
            {
                "path": relative_path,
                "bytes": int(file_stat.st_size),
                "sha256": _sha256_with_identity(candidate, file_stat),
            }
        )
    payload = {
        "schema_version": BACKUP_BUNDLE_MANIFEST_SCHEMA_VERSION,
        "kind": BACKUP_BUNDLE_MANIFEST_KIND,
        "complete_membership": True,
        "producer": producer,
        "producer_schema_version": BACKUP_BUNDLE_PRODUCER_SCHEMA_VERSION,
        "transaction_id": str(transaction_id or uuid.uuid4()),
        "bundle_basename": root.name,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "directories": sorted(directories, key=str.casefold),
        "members": members,
    }
    _atomic_write_json(manifest, payload)
    verified = verify_backup_transaction_manifest(root)
    if not verified["valid"]:
        raise ValueError(
            "new backup transaction manifest did not verify: "
            f"{verified['retention_basis']}"
        )
    return payload


def verify_backup_transaction_manifest(bundle_root):
    """Verify a manifest-declared exact directory bundle without inference."""
    root = _lexical_path(bundle_root)
    manifest = root / BACKUP_BUNDLE_MANIFEST_FILENAME
    try:
        if manifest.stat().st_size > _MAX_RECEIPT_BYTES:
            raise ValueError("manifest_too_large")
        payload = json.loads(manifest.read_text(encoding="utf-8"))
        if payload.get("kind") != BACKUP_BUNDLE_MANIFEST_KIND:
            raise ValueError("manifest_kind_mismatch")
        if int(payload.get("schema_version", -1)) != BACKUP_BUNDLE_MANIFEST_SCHEMA_VERSION:
            raise ValueError("manifest_schema_mismatch")
        if payload.get("complete_membership") is not True:
            raise ValueError("membership_not_sealed")
        if payload.get("producer") not in REGISTERED_BACKUP_MANIFEST_PRODUCERS:
            raise ValueError("producer_unregistered")
        if int(payload.get("producer_schema_version", -1)) != (
            BACKUP_BUNDLE_PRODUCER_SCHEMA_VERSION
        ):
            raise ValueError("producer_schema_mismatch")
        if not str(payload.get("transaction_id") or "").strip():
            raise ValueError("transaction_id_missing")
        if str(payload.get("bundle_basename") or "") != root.name:
            raise ValueError("bundle_basename_mismatch")
        actual, actual_directories = _bundle_tree_inventory(root, exclude_manifest=True)
        declared_directories = {
            _validate_manifest_relative_path(value)
            for value in payload.get("directories") or []
        }
        if declared_directories != actual_directories:
            raise ValueError("directory_membership_changed")
        declared = {}
        for row in payload.get("members") or []:
            if not isinstance(row, dict):
                raise ValueError("member_row_invalid")
            relative = _validate_manifest_relative_path(row.get("path"))
            if relative == BACKUP_BUNDLE_MANIFEST_FILENAME or relative in declared:
                raise ValueError("member_path_duplicate_or_reserved")
            declared[relative] = row
        if set(declared) != set(actual):
            raise ValueError("file_membership_changed")
        for relative_path, row in declared.items():
            candidate, file_stat = actual[relative_path]
            if int(row.get("bytes", -1)) != int(file_stat.st_size):
                raise ValueError(f"member_size_changed:{relative_path}")
            digest = _sha256_with_identity(candidate, file_stat)
            if digest.casefold() != str(row.get("sha256") or "").casefold():
                raise ValueError(f"member_hash_changed:{relative_path}")
    except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        return {
            "valid": False,
            "bundle_root": str(root),
            "manifest": str(manifest),
            "retention_basis": str(exc) or type(exc).__name__,
        }
    return {
        "valid": True,
        "bundle_root": str(root),
        "manifest": str(manifest),
        "producer": payload["producer"],
        "transaction_id": payload["transaction_id"],
        "members": payload["members"],
        "directories": payload.get("directories") or [],
    }


def verify_retained_recovery_archive(path):
    """Verify a crash-bridge ZIP as a complete sealed recovery set."""
    candidate = _lexical_path(path)
    try:
        with zipfile.ZipFile(candidate, "r") as archive:
            infos = {info.filename: info for info in archive.infolist()}
            manifest_info = infos.get(BACKUP_BUNDLE_MANIFEST_FILENAME)
            if manifest_info is None or manifest_info.file_size > _MAX_RECEIPT_BYTES:
                raise ValueError("archive_manifest_missing_or_large")
            payload = json.loads(
                archive.read(BACKUP_BUNDLE_MANIFEST_FILENAME).decode("utf-8")
            )
            if payload.get("kind") != BACKUP_BUNDLE_MANIFEST_KIND:
                raise ValueError("archive_manifest_kind_mismatch")
            if int(payload.get("schema_version", -1)) != BACKUP_BUNDLE_MANIFEST_SCHEMA_VERSION:
                raise ValueError("archive_manifest_schema_mismatch")
            if payload.get("complete_membership") is not True:
                raise ValueError("archive_membership_not_sealed")
            if payload.get("producer") not in REGISTERED_BACKUP_MANIFEST_PRODUCERS:
                raise ValueError("archive_producer_unregistered")
            if int(payload.get("producer_schema_version", -1)) != (
                BACKUP_BUNDLE_PRODUCER_SCHEMA_VERSION
            ):
                raise ValueError("archive_producer_schema_mismatch")
            archive_name = RETAINED_RECOVERY_ARCHIVE_RE.fullmatch(candidate.name)
            if archive_name is None:
                raise ValueError("archive_filename_invalid")
            transaction_name = archive_name.group("transaction")
            _retained_archive_source_root(candidate)
            if str(payload.get("bundle_basename") or "") != transaction_name:
                raise ValueError("archive_bundle_basename_mismatch")
            declared = {}
            for row in payload.get("members") or []:
                if not isinstance(row, dict):
                    raise ValueError("archive_member_row_invalid")
                relative = _validate_manifest_relative_path(row.get("path"))
                if relative == BACKUP_BUNDLE_MANIFEST_FILENAME or relative in declared:
                    raise ValueError("archive_member_duplicate_or_reserved")
                declared[relative] = row
            directories = {
                _validate_manifest_relative_path(value).rstrip("/") + "/"
                for value in payload.get("directories") or []
            }
            expected_names = (
                set(declared) | directories | {BACKUP_BUNDLE_MANIFEST_FILENAME}
            )
            if set(infos) != expected_names:
                raise ValueError("archive_exact_membership_changed")
            for relative, row in declared.items():
                info = infos[relative]
                if int(row.get("bytes", -1)) != int(info.file_size):
                    raise ValueError(f"archive_member_size_changed:{relative}")
                if _zip_member_sha256(archive, relative).casefold() != str(
                    row.get("sha256") or ""
                ).casefold():
                    raise ValueError(f"archive_member_hash_changed:{relative}")
    except (
        FileNotFoundError,
        OSError,
        UnicodeError,
        json.JSONDecodeError,
        KeyError,
        TypeError,
        ValueError,
        zipfile.BadZipFile,
    ) as exc:
        return {
            "valid": False,
            "path": str(candidate),
            "retention_basis": str(exc) or type(exc).__name__,
        }
    return {
        "valid": True,
        "path": str(candidate),
        "producer": str(payload.get("producer") or ""),
        "transaction_id": str(payload.get("transaction_id") or ""),
        "members": payload.get("members") or [],
        "directories": payload.get("directories") or [],
    }


def _receipt_inputs(scope, boundary, receipt_paths):
    values = [_lexical_path(value) for value in receipt_paths]
    if scope == RETRY_SCOPE:
        latest = boundary / "latest.json"
        if _path_key(latest) not in {_path_key(value) for value in values}:
            values.append(latest)
    return values


def _operational_receipt_paths(
    scope, boundary, *, inventory=(), legacy_manifest_cache=None
):
    """Return narrow current authorities that may reference cleanup targets."""
    values = []
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        application = Path(local_app_data) / "SpeedTreeBatchTools"
        values.extend(
            (
                application / "shared_job_queue.json",
                application / "retry_progress" / "latest.json",
            )
        )
    # JSON stored *inside* a backup namespace is historical bundle data, not a
    # live authority.  Treating arbitrary manifests there as current receipts
    # permanently protects every backup path they describe and makes one apply
    # re-read thousands of old files.  Live authorities are the local queue,
    # retry latest pointer, explicit receipt_paths, and output reservations.
    deduplicated = []
    seen = set()
    for value in values:
        key = _path_key(value)
        if key not in seen:
            seen.add(key)
            deduplicated.append(_lexical_path(value))
    return deduplicated


def _protection_keys(boundary, protected_paths, active_paths, receipt_paths):
    direct = {_path_key(value) for value in (*protected_paths, *active_paths)}
    direct.update(
        referenced_paths_from_receipts(
            receipt_paths,
            allowed_roots=(boundary,),
        )
    )
    direct.update(
        _path_key(value)
        for value in receipt_paths
        if _is_within_lexical(value, boundary)
    )
    if _path_key(boundary) == _path_key(REPOSITORY_LOG_ROOT):
        direct.update(
            _pending_unreal_paths_from_reference_receipt(
                allowed_roots=(boundary,)
            )
        )
    return direct


def _path_key_overlap(left, right):
    left = str(left).rstrip("\\/")
    right = str(right).rstrip("\\/")
    if left == right:
        return True
    separator = os.sep.casefold()
    return left.startswith(right + separator) or right.startswith(left + separator)


def _sealed_bundle_is_protected(members, bundle_root, protected_keys):
    root_key = _path_key(bundle_root)
    if any(_path_key_overlap(root_key, key) for key in protected_keys):
        return True
    return any(
        any(_path_key_overlap(_path_key(member["path"]), key) for key in protected_keys)
        for member in members
    )


def _recovery_archive_source_is_protected(archive_path, protected_keys):
    source_key = _path_key(_retained_archive_source_root(archive_path))
    return any(_path_key_overlap(source_key, key) for key in protected_keys)


def _prepare_bundle_for_deletion(bundle, basis):
    """Bind a candidate to stable metadata without hydrating large/cloud files."""
    try:
        for member in bundle["members"]:
            candidate = Path(member["path"])
            current = candidate.lstat()
            if not _same_stat_identity(current, current):
                raise OSError("not a stable regular file")
            if (
                int(current.st_size) <= MAX_HASH_BYTES
                and not _is_cloud_placeholder_stat(current)
                and member.get("scope") != PRODUCTION_BACKUP_SCOPE
            ):
                member["sha256"] = _sha256_with_identity(candidate, current)
                member["identity_mode"] = "sha256_and_stat"
            else:
                member["sha256"] = ""
                member["identity_mode"] = "stat_only"
    except (FileNotFoundError, OSError):
        for member in bundle["members"]:
            member["sha256"] = ""
            member["identity_mode"] = "unstable"
        bundle["retention_basis"] = "identity_unstable_during_plan"
        return False
    bundle["action"] = "delete"
    bundle["retention_basis"] = basis
    for member in bundle["members"]:
        member["action"] = "delete"
        member["retention_basis"] = basis
    return True


def plan_retention(
    root,
    *,
    scope,
    policy=None,
    protected_paths=(),
    active_paths=(),
    receipt_paths=(),
    now=None,
    enforce_capacity=True,
):
    """Build a deterministic, read-only plan for one exact allowed root."""
    boundary = _validate_exact_scope_root(scope, root)
    policy = policy or DEFAULT_RETENTION_POLICIES[scope]
    now = float(time.time() if now is None else now)
    inventory = tuple(_iter_scope_inventory(scope, boundary) or ())
    original_index = {}
    legacy_manifest_cache = {}
    receipts = _receipt_inputs(
        scope,
        boundary,
        (
            *receipt_paths,
            *_operational_receipt_paths(
                scope,
                boundary,
                inventory=inventory,
                legacy_manifest_cache=legacy_manifest_cache,
            ),
        ),
    )
    protected = _protection_keys(
        boundary, protected_paths, active_paths, receipts
    )

    entries = []
    for candidate, file_stat in inventory:
        kind = _classify(scope, candidate, boundary)
        if kind is None:
            continue
        bundle_id, series_id, atomic_mode, uncertainty, bundle_root = _bundle_facts(
            scope, candidate, kind
        )
        legacy_original = None
        legacy_manifest = ""
        if (
            scope == PRODUCTION_BACKUP_SCOPE
            and atomic_mode == "none"
            and bundle_root
        ):
            cache_key = _path_key(bundle_root)
            if cache_key not in legacy_manifest_cache:
                legacy_manifest_cache[cache_key] = (
                    _legacy_backup_manifest_originals(bundle_root, boundary)
                )
            legacy_map, legacy_manifest = legacy_manifest_cache[cache_key]
            legacy_original = legacy_map.get(_path_key(candidate))
            if legacy_original is not None:
                bundle_id = _path_key(candidate)
                series_id = f"{_path_key(Path(legacy_manifest).parent)}::legacy_manifest"
                atomic_mode = "single_file"
                uncertainty = ""
                bundle_root = candidate
        entry = _entry(
            candidate,
            kind,
            bundle_id,
            series_id,
            atomic_mode,
            uncertainty,
            bundle_root,
            file_stat,
        )
        entry["scope"] = scope
        if scope == PRODUCTION_BACKUP_SCOPE:
            original = legacy_original or _verified_original_for_backup(
                candidate, kind, boundary, original_index
            )
            if original is not None:
                entry["original_path"] = str(original)
                entry["original_verified"] = True
            if legacy_original is not None:
                entry["original_evidence_manifest"] = legacy_manifest
        if kind == "manual_copy_backup" and not policy.include_manual_copies:
            entry["retention_basis"] = "manual_copy_requires_explicit_backup_policy"
        elif kind == "retained_recovery_archive" and not (
            policy.include_retained_recovery_archives
        ):
            entry["retention_basis"] = (
                "retained_recovery_archive_requires_explicit_policy"
            )
        elif kind == "retained_recovery_archive" and not (
            verify_retained_recovery_archive(candidate)["valid"]
        ):
            entry["retention_basis"] = "retained_recovery_archive_invalid"
        elif kind == "retained_recovery_archive" and (
            _recovery_archive_source_is_protected(candidate, protected)
        ):
            entry["retention_basis"] = "protected_current_active_or_referenced"
        elif any(
            _path_key_overlap(_path_key(candidate), key) for key in protected
        ):
            entry["retention_basis"] = "protected_current_active_or_referenced"
        elif (
            scope == ROOT_DIAGNOSTIC_SCOPE
            and candidate.name.casefold() == "speedtree_batch_tools_error.log"
        ):
            entry["retention_basis"] = "protected_current_active_or_referenced"
        elif (
            scope == QUEUE_STATE_SCOPE
            and candidate.name.casefold() == "shared_job_queue.json"
        ):
            # The queue prunes terminal history itself; the current atomic state
            # file contributes to the global byte budget but is never unlinked.
            entry["retention_basis"] = "protected_current_active_or_referenced"
        elif (
            scope == PRODUCTION_BACKUP_SCOPE
            and kind not in {"retained_recovery_archive"}
            and candidate.suffix.casefold()
            in {".blend", ".fbx", ".png", ".sbs", ".spm", ".tga", ".tif", ".tiff"}
            and not entry["original_verified"]
        ):
            entry["retention_basis"] = "backup_original_unverified"
        entries.append(entry)

    bundles = {}
    for entry in entries:
        bundles.setdefault(entry["bundle_id"], []).append(entry)
    bundle_rows = []
    for bundle_id, members in bundles.items():
        bundle_roots = {member["bundle_root"] for member in members}
        seal = {"valid": False, "retention_basis": "not_a_manifest_bundle"}
        if (
            scope == PRODUCTION_BACKUP_SCOPE
            and len(bundle_roots) == 1
            and any(
                member["atomic_mode"] == "none"
                for member in members
            )
        ):
            seal = verify_backup_transaction_manifest(next(iter(bundle_roots)))
            if seal["valid"]:
                for member in members:
                    member["atomic_mode"] = "sealed_directory_archive_bridge"
                    if member["retention_basis"] == "uncertain_multi_file_recovery_bundle":
                        member["retention_basis"] = "within_budget"
                if (
                    not policy.include_manual_copies
                    and any(
                        is_manual_copy_inventory_artifact(member["path"])
                        for member in members
                    )
                ):
                    for member in members:
                        member["retention_basis"] = (
                            "manual_copy_requires_explicit_backup_policy"
                        )
                if _sealed_bundle_is_protected(
                    members, next(iter(bundle_roots)), protected
                ):
                    for member in members:
                        member["retention_basis"] = (
                            "protected_current_active_or_referenced"
                        )
        retention_bases = {member["retention_basis"] for member in members}
        atomic_mode = "none"
        if len(members) == 1 and members[0]["atomic_mode"] == "single_file":
            atomic_mode = "single_file"
        elif seal["valid"] and all(
            member["atomic_mode"] == "sealed_directory_archive_bridge"
            for member in members
        ):
            atomic_mode = "sealed_directory_archive_bridge"
        elif scope == PRODUCTION_BACKUP_SCOPE:
            asset_members = [
                member
                for member in members
                if Path(member["path"]).suffix.casefold()
                in {
                    ".blend",
                    ".fbx",
                    ".png",
                    ".sbs",
                    ".spm",
                    ".tga",
                    ".tif",
                    ".tiff",
                }
            ]
            auxiliary_members = [
                member for member in members if member not in asset_members
            ]
            if (
                asset_members
                and all(member["original_verified"] for member in asset_members)
                and all(
                    _REGENERABLE_BACKUP_AUX_RE.search(Path(member["path"]).name)
                    for member in auxiliary_members
                )
            ):
                atomic_mode = "verified_original_bundle"
                for member in members:
                    member["atomic_mode"] = "verified_original_bundle"
                    if member["retention_basis"] == "uncertain_multi_file_recovery_bundle":
                        member["retention_basis"] = "within_budget"
        atomic = atomic_mode != "none"
        retention_bases = {member["retention_basis"] for member in members}
        retention_basis = "within_budget"
        if not atomic:
            retention_basis = "uncertain_multi_file_recovery_bundle"
        elif retention_bases != {"within_budget"}:
            retention_basis = sorted(
                value for value in retention_bases if value != "within_budget"
            )[0]
        bundle_rows.append(
            {
                "bundle_id": bundle_id,
                "bundle_root": next(iter(bundle_roots)) if len(bundle_roots) == 1 else "",
                "series_id": members[0]["series_id"],
                "member_count": len(members),
                "bytes": sum(member["bytes"] for member in members),
                "effective_time_ns": max(member["effective_time_ns"] for member in members),
                "atomic_mode": atomic_mode,
                "seal_valid": bool(seal["valid"]),
                "seal_basis": (
                    "" if seal["valid"] else seal.get("retention_basis", "")
                ),
                "action": "keep",
                "retention_basis": retention_basis,
                "members": members,
            }
        )

    total_bytes = sum(bundle["bytes"] for bundle in bundle_rows)
    projected_bytes = total_bytes
    eligible = sorted(
        (
            bundle
            for bundle in bundle_rows
            if bundle["retention_basis"] == "within_budget"
        ),
        key=lambda item: (item["effective_time_ns"], item["bundle_id"]),
    )
    for bundle in eligible:
        age_seconds = now - (bundle["effective_time_ns"] / 1_000_000_000)
        if age_seconds <= policy.max_age_seconds:
            continue
        if _prepare_bundle_for_deletion(bundle, "older_than_max_age"):
            projected_bytes -= bundle["bytes"]

    for bundle in eligible:
        if not enforce_capacity:
            break
        if projected_bytes < policy.max_bytes:
            break
        if bundle["action"] == "delete":
            continue
        if _prepare_bundle_for_deletion(
            bundle, "over_max_bytes_oldest_eligible"
        ):
            projected_bytes -= bundle["bytes"]

    for bundle in bundle_rows:
        for member in bundle.pop("members"):
            member["action"] = bundle["action"]
            member["retention_basis"] = bundle["retention_basis"]

    delete_entries = [entry for entry in entries if entry["action"] == "delete"]
    return {
        "schema_version": 2,
        "kind": "speedtree_artifact_retention_plan",
        "scope": scope,
        "root": str(boundary),
        "root_canonical": str(_canonical_path(boundary)),
        "dry_run": bool(policy.dry_run),
        "policy": asdict(policy),
        "guard_inputs": {
            "protected_paths": [str(_lexical_path(value)) for value in protected_paths],
            "active_paths": [str(_lexical_path(value)) for value in active_paths],
            "receipt_paths": [str(value) for value in receipts],
        },
        "generated_file_count": len(entries),
        "generated_bytes": total_bytes,
        "planned_delete_count": len(delete_entries),
        "planned_delete_bytes": sum(entry["bytes"] for entry in delete_entries),
        "projected_bytes": projected_bytes,
        "budget_unmet_bytes": max(
            0, projected_bytes - (policy.max_bytes - 1)
        ) if enforce_capacity else 0,
        "manifest_sealed_bundle_count": sum(
            1 for bundle in bundle_rows if bundle["seal_valid"]
        ),
        "uncertain_backup_bundle_count": sum(
            1
            for bundle in bundle_rows
            if bundle["retention_basis"] == "uncertain_multi_file_recovery_bundle"
        ),
        "producer_manifest_adoption_required": any(
            bundle["retention_basis"] == "uncertain_multi_file_recovery_bundle"
            for bundle in bundle_rows
        ),
        "manual_copy_inventory": [
            {
                "path": entry["path"],
                "bytes": entry["bytes"],
                "retention_eligible": entry["retention_basis"]
                != "manual_copy_requires_explicit_backup_policy",
                "retention_basis": entry["retention_basis"],
            }
            for entry in entries
            if entry["kind"] == "manual_copy_backup"
        ],
        "bundles": [
            {key: value for key, value in bundle.items() if key != "members"}
            for bundle in bundle_rows
        ],
        "entries": entries,
    }


def _apply_protection_keys(
    plan, boundary, *, protected_paths, active_paths, receipt_paths
):
    guards = plan.get("guard_inputs") or {}
    stored_protected = tuple(guards.get("protected_paths") or ())
    stored_active = tuple(guards.get("active_paths") or ())
    stored_receipts = tuple(guards.get("receipt_paths") or ())
    current_receipts = _receipt_inputs(
        plan["scope"], boundary, (*stored_receipts, *receipt_paths)
    )
    return _protection_keys(
        boundary,
        (*stored_protected, *protected_paths),
        (*stored_active, *active_paths),
        current_receipts,
    )


def _validate_entry_stat_identity(entry, candidate):
    try:
        file_stat = candidate.lstat()
    except FileNotFoundError:
        return None, "already_absent"
    except OSError:
        return None, "identity_unavailable"
    expected = {
        "bytes": int(entry.get("bytes", -1)),
        "mtime_ns": int(entry.get("mtime_ns", -1)),
        "ctime_ns": int(entry.get("ctime_ns", -1)),
        "device": int(entry.get("device", -1)),
        "inode": int(entry.get("inode", -1)),
    }
    actual = {
        "bytes": int(file_stat.st_size),
        "mtime_ns": int(file_stat.st_mtime_ns),
        "ctime_ns": int(file_stat.st_ctime_ns),
        "device": int(file_stat.st_dev),
        "inode": int(file_stat.st_ino),
    }
    if (
        expected != actual
        or not stat.S_ISREG(file_stat.st_mode)
        or _is_reparse_stat(file_stat)
    ):
        return None, "identity_changed"
    if entry.get("identity_mode") not in {"sha256_and_stat", "stat_only"}:
        return None, "identity_changed"
    return file_stat, ""


def _validate_entry_identity(entry, candidate):
    file_stat, reason = _validate_entry_stat_identity(entry, candidate)
    if reason:
        return None, reason
    if entry.get("identity_mode") == "stat_only":
        return file_stat, ""
    try:
        digest = _sha256_with_identity(candidate, file_stat)
    except OSError:
        return None, "identity_changed"
    if digest.casefold() != str(entry["sha256"]).casefold():
        return None, "content_hash_changed"
    return file_stat, ""


def _entry_original_still_valid(entry, boundary):
    original_text = str(entry.get("original_path") or "")
    if not entry.get("original_verified") or not original_text:
        return False
    original = _lexical_path(original_text)
    if not _is_within_lexical(original, boundary):
        return False
    if generated_production_backup_kind(original) is not None:
        return False
    if _has_reparse_component(original, boundary):
        return False
    try:
        file_stat = original.lstat()
    except (FileNotFoundError, OSError):
        return False
    return stat.S_ISREG(file_stat.st_mode) and not _is_reparse_stat(file_stat)


def _zip_member_sha256(archive, member_name):
    digest = hashlib.sha256()
    with archive.open(member_name, "r") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _unlink_bundle_member(path):
    _lexical_path(path).unlink()


def _retained_archive_source_root(archive_path, *, allowed_root=None):
    candidate = _lexical_path(archive_path)
    match = RETAINED_RECOVERY_ARCHIVE_RE.fullmatch(candidate.name)
    if not match:
        raise ValueError(f"not a retained recovery archive: {candidate}")
    transaction = match.group("transaction")
    if (
        transaction in {"", ".", ".."}
        or Path(transaction).name != transaction
        or "/" in transaction
        or "\\" in transaction
    ):
        raise ValueError(f"unsafe recovery archive transaction name: {transaction!r}")
    namespace, relative = _backup_namespace(candidate)
    if (
        namespace is None
        or relative is None
        or len(relative.parts) != 1
        or _path_key(namespace) != _path_key(candidate.parent)
    ):
        raise ValueError(
            f"recovery archive must be directly inside a backup namespace: {candidate}"
        )
    source_root = _lexical_path(candidate.parent / transaction)
    if source_root.parent != candidate.parent:
        raise ValueError(f"recovery source is not a direct child: {source_root}")
    if not _is_within_lexical(source_root, namespace) or not _is_within_canonical(
        source_root, namespace
    ):
        raise ValueError(f"recovery source escaped backup namespace: {source_root}")
    if allowed_root is not None and (
        not _is_within_lexical(source_root, allowed_root)
        or not _is_within_canonical(source_root, allowed_root)
    ):
        raise ValueError(f"recovery source escaped allowed Tree root: {source_root}")
    return source_root


def _reconcile_retained_recovery_archive(
    archive_path, verification, *, allowed_root
):
    """Finish deletion of an exact partial source while the full ZIP exists."""
    archive_path = _lexical_path(archive_path)
    source_root = _retained_archive_source_root(
        archive_path, allowed_root=allowed_root
    )
    if not source_root.exists():
        return {"complete": True, "deleted": [], "retention_basis": ""}
    deleted = []
    try:
        actual, actual_directories = _bundle_tree_inventory(
            source_root, exclude_manifest=False
        )
        expected_files = {
            _validate_manifest_relative_path(row["path"])
            for row in verification.get("members") or []
        }
        expected_files.add(BACKUP_BUNDLE_MANIFEST_FILENAME)
        expected_directories = {
            _validate_manifest_relative_path(value)
            for value in verification.get("directories") or []
        }
        if not set(actual).issubset(expected_files):
            raise OSError("partial source contains undeclared files")
        if not actual_directories.issubset(expected_directories):
            raise OSError("partial source contains undeclared directories")
        with zipfile.ZipFile(archive_path, "r") as archive:
            for relative, (candidate, file_stat) in actual.items():
                if int(file_stat.st_size) != int(
                    archive.getinfo(relative).file_size
                ):
                    raise OSError(f"partial source size changed: {relative}")
                source_sha = _sha256_with_identity(candidate, file_stat)
                archive_sha = _zip_member_sha256(archive, relative)
                if source_sha.casefold() != archive_sha.casefold():
                    raise OSError(f"partial source content changed: {relative}")
        for relative, (candidate, _file_stat) in sorted(
            actual.items(),
            key=lambda item: len(item[1][0].parts),
            reverse=True,
        ):
            current = candidate.lstat()
            with zipfile.ZipFile(archive_path, "r") as archive:
                if _sha256_with_identity(candidate, current).casefold() != (
                    _zip_member_sha256(archive, relative).casefold()
                ):
                    raise OSError(
                        f"partial source changed during reconciliation: {relative}"
                    )
            _unlink_bundle_member(candidate)
            deleted.append(str(candidate))
        for current, directory_names, _file_names in os.walk(
            source_root, topdown=False, followlinks=False
        ):
            owner = Path(current)
            for name in directory_names:
                (owner / name).rmdir()
        source_root.rmdir()
        return {
            "complete": True,
            "deleted": deleted,
            "retention_basis": "",
        }
    except (OSError, RuntimeError, ValueError, zipfile.BadZipFile) as exc:
        return {
            "complete": False,
            "deleted": deleted,
            "retention_basis": (
                f"recovery_archive_reconciliation_failed:{type(exc).__name__}:"
                f"{str(exc)[:500]}"
            ),
        }


def _delete_sealed_bundle_with_archive(bundle_root, members, verification):
    """Remove a sealed directory while retaining a complete crash bridge.

    A verified ZIP is installed beside the transaction before the first source
    member is unlinked.  If any later step fails, that archive is retained as a
    complete recovery set.  The final archive removal is one single-file
    unlink, so there is never a state where only a partial recovery set exists.
    """
    root = _lexical_path(bundle_root)
    archive_path = root.parent / (
        f".{root.name}.retention_delete_{uuid.uuid4().hex}.zip"
    )
    temporary = archive_path.with_name(f".{archive_path.name}.tmp")
    by_relative = {
        _lexical_path(entry["path"]).relative_to(root).as_posix(): entry
        for entry in members
    }
    deleted = []
    archive_installed = False
    try:
        with zipfile.ZipFile(
            temporary,
            mode="x",
            compression=zipfile.ZIP_DEFLATED,
            allowZip64=True,
        ) as archive:
            for directory in sorted(
                verification.get("directories") or [], key=str.casefold
            ):
                archive.writestr(directory.rstrip("/") + "/", b"")
            for relative, entry in sorted(
                by_relative.items(), key=lambda item: item[0].casefold()
            ):
                archive.write(entry["path"], arcname=relative)
        with temporary.open("r+b") as handle:
            os.fsync(handle.fileno())
        with zipfile.ZipFile(temporary, mode="r") as archive:
            expected_names = set(by_relative)
            expected_names.update(
                directory.rstrip("/") + "/"
                for directory in verification.get("directories") or []
            )
            if set(archive.namelist()) != expected_names:
                raise OSError("recovery archive membership verification failed")
            for relative, entry in by_relative.items():
                if _zip_member_sha256(archive, relative).casefold() != str(
                    entry["sha256"]
                ).casefold():
                    raise OSError(
                        f"recovery archive hash verification failed: {relative}"
                    )
        os.replace(temporary, archive_path)
        archive_installed = True

        # Validate every member again before the first unlink.  Once deletion
        # starts, the verified archive is the complete recovery authority.
        for entry in members:
            _file_stat, reason = _validate_entry_stat_identity(
                entry, _lexical_path(entry["path"])
            )
            if reason:
                raise OSError(f"bundle member changed before unlink: {reason}")
        for entry in sorted(
            members,
            key=lambda row: len(_lexical_path(row["path"]).parts),
            reverse=True,
        ):
            candidate = _lexical_path(entry["path"])
            _file_stat, reason = _validate_entry_stat_identity(entry, candidate)
            if reason:
                raise OSError(f"bundle member changed during unlink: {reason}")
            _unlink_bundle_member(candidate)
            deleted.append(str(candidate))
        for current, directory_names, _file_names in os.walk(
            root, topdown=False, followlinks=False
        ):
            owner = Path(current)
            for name in directory_names:
                (owner / name).rmdir()
        root.rmdir()
        archive_path.unlink()
        archive_installed = False
        return {
            "complete": True,
            "deleted": deleted,
            "archive": "",
            "retention_basis": "",
        }
    except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
        return {
            "complete": False,
            "deleted": deleted,
            "archive": str(archive_path) if archive_installed else "",
            "retention_basis": (
                f"sealed_bundle_delete_failed:{type(exc).__name__}:"
                f"{str(exc)[:500]}"
            ),
        }
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def apply_retention_plan(
    plan,
    *,
    apply=False,
    root_acknowledgement=None,
    protected_paths=(),
    active_paths=(),
    receipt_paths=(),
):
    """Apply unchanged single-file bundles after explicit authorization.

    Production backup deletion additionally requires an exact spelling of the
    configured Tree root in ``root_acknowledgement``.  No caller can authorize
    a broader parent directory.
    """
    if not apply:
        return {"status": "dry_run", "deleted": [], "skipped": []}
    if (
        not isinstance(plan, dict)
        or plan.get("kind") != "speedtree_artifact_retention_plan"
        or int(plan.get("schema_version", -1)) != 2
    ):
        raise ValueError("invalid retention plan")
    scope = plan.get("scope")
    boundary = _validate_exact_scope_root(scope, plan.get("root"))
    if _canonical_key(boundary) != os.path.normcase(
        str(plan.get("root_canonical") or "")
    ).casefold():
        raise ValueError("retention root canonical identity changed after planning")
    if scope == PRODUCTION_BACKUP_SCOPE:
        if root_acknowledgement is None or _path_key(root_acknowledgement) != _path_key(boundary):
            raise ValueError(
                "production backup apply requires exact Tree root acknowledgement"
            )
    receipt_paths = (
        *receipt_paths,
        *_operational_receipt_paths(scope, boundary),
    )
    legacy_apply_cache = {}

    deleted = []
    skipped = []
    retained_recovery_archives = []
    bundles = {}
    for entry in plan.get("entries") or []:
        if isinstance(entry, dict) and entry.get("action") == "delete":
            bundles.setdefault(entry.get("bundle_id"), []).append(entry)

    for bundle_id, members in sorted(bundles.items(), key=lambda item: str(item[0])):
        paths = [str(_lexical_path(member.get("path"))) for member in members]
        modes = {member.get("atomic_mode") for member in members}
        single_file = len(members) == 1 and modes == {"single_file"}
        sealed_directory = (
            scope == PRODUCTION_BACKUP_SCOPE
            and modes == {"sealed_directory_archive_bridge"}
            and len({member.get("bundle_root") for member in members}) == 1
        )
        verified_original_bundle = (
            scope == PRODUCTION_BACKUP_SCOPE
            and modes == {"verified_original_bundle"}
            and len({member.get("bundle_root") for member in members}) == 1
        )
        if not single_file and not sealed_directory and not verified_original_bundle:
            skipped.extend(
                {"path": path, "retention_basis": "non_atomic_bundle_refused"}
                for path in paths
            )
            continue
        bundle_invalid = ""
        for entry in members:
            candidate = _lexical_path(entry.get("path"))
            if not _is_within_lexical(candidate, boundary):
                bundle_invalid = "outside_root"
                break
            if _has_reparse_component(candidate, boundary) or not _is_within_canonical(
                candidate, boundary
            ):
                bundle_invalid = "unsafe_reparse_or_canonical_path"
                break
            if _classify(scope, candidate, boundary) != entry.get("kind"):
                bundle_invalid = "classification_changed"
                break
            if (
                scope == PRODUCTION_BACKUP_SCOPE
                and candidate.suffix.casefold()
                in {".blend", ".fbx", ".png", ".sbs", ".spm", ".tga", ".tif", ".tiff"}
                and not _entry_original_still_valid(entry, boundary)
            ):
                bundle_invalid = "backup_original_missing_or_changed"
                break
            if (
                entry.get("kind") == "manual_copy_backup"
                and not bool((plan.get("policy") or {}).get("include_manual_copies"))
            ):
                bundle_invalid = "manual_copy_policy_not_authorized"
                break
            if (
                entry.get("kind") == "retained_recovery_archive"
                and not bool(
                    (plan.get("policy") or {}).get(
                        "include_retained_recovery_archives"
                    )
                )
            ):
                bundle_invalid = "retained_recovery_archive_policy_not_authorized"
                break
            if (
                entry.get("kind") == "retained_recovery_archive"
                and not verify_retained_recovery_archive(candidate)["valid"]
            ):
                bundle_invalid = "retained_recovery_archive_invalid"
                break
            evidence_manifest = str(entry.get("original_evidence_manifest") or "")
            if evidence_manifest:
                evidence_path = _lexical_path(evidence_manifest)
                evidence_key = _path_key(evidence_path.parent)
                if evidence_key not in legacy_apply_cache:
                    legacy_apply_cache[evidence_key] = (
                        _legacy_backup_manifest_originals(
                            evidence_path.parent, boundary
                        )
                    )
                evidence_map, current_manifest = legacy_apply_cache[evidence_key]
                current_original = evidence_map.get(_path_key(candidate))
                if (
                    _path_key(current_manifest or boundary) != _path_key(evidence_path)
                    or current_original is None
                    or _path_key(current_original)
                    != _path_key(entry.get("original_path"))
                ):
                    bundle_invalid = "backup_manifest_or_original_changed"
                    break
                current_bundle = (
                    _path_key(candidate),
                    f"{_path_key(evidence_path.parent)}::legacy_manifest",
                    "single_file",
                    "",
                    candidate,
                )
            else:
                current_bundle = _bundle_facts(scope, candidate, entry.get("kind"))
            if current_bundle[0] != bundle_id:
                bundle_invalid = "bundle_identity_changed"
                break
            if single_file and current_bundle[2] != "single_file":
                bundle_invalid = "bundle_identity_changed"
                break
            if sealed_directory and _path_key(current_bundle[4]) != _path_key(
                entry.get("bundle_root")
            ):
                bundle_invalid = "bundle_identity_changed"
                break
        if bundle_invalid:
            skipped.extend(
                {"path": path, "retention_basis": bundle_invalid}
                for path in paths
            )
            continue

        verification = None
        if sealed_directory:
            verification = verify_backup_transaction_manifest(
                members[0]["bundle_root"]
            )
            if not verification["valid"]:
                skipped.extend(
                    {"path": path, "retention_basis": "bundle_manifest_changed"}
                    for path in paths
                )
                continue
            bundle_root = _lexical_path(members[0]["bundle_root"])
            expected_relative_paths = {
                _validate_manifest_relative_path(row["path"])
                for row in verification.get("members") or []
            }
            expected_relative_paths.add(BACKUP_BUNDLE_MANIFEST_FILENAME)
            planned_relative_paths = {
                _lexical_path(entry["path"]).relative_to(bundle_root).as_posix()
                for entry in members
            }
            if planned_relative_paths != expected_relative_paths:
                skipped.extend(
                    {
                        "path": path,
                        "retention_basis": "bundle_plan_membership_mismatch",
                    }
                    for path in paths
                )
                continue
            if (
                not bool((plan.get("policy") or {}).get("include_manual_copies"))
                and any(
                    is_manual_copy_inventory_artifact(entry["path"])
                    for entry in members
                )
            ):
                skipped.extend(
                    {
                        "path": path,
                        "retention_basis": "manual_copy_policy_not_authorized",
                    }
                    for path in paths
                )
                continue

        # Active paths and receipt contents are live inputs.  Re-read them at
        # apply time, after classification, and once more immediately before
        # unlink so a changed latest.json cannot be ignored.
        protected = _apply_protection_keys(
            plan,
            boundary,
            protected_paths=protected_paths,
            active_paths=active_paths,
            receipt_paths=receipt_paths,
        )
        if (
            sealed_directory
            and _sealed_bundle_is_protected(
                members, members[0]["bundle_root"], protected
            )
        ) or (
            single_file
            and members[0].get("kind") == "retained_recovery_archive"
            and _recovery_archive_source_is_protected(
                members[0]["path"], protected
            )
        ) or any(
            any(
                _path_key_overlap(_path_key(entry["path"]), key)
                for key in protected
            )
            for entry in members
        ):
            skipped.extend(
                {"path": path, "retention_basis": "newly_active_or_referenced"}
                for path in paths
            )
            continue
        identity_reason = ""
        for entry in members:
            _file_stat, identity_reason = _validate_entry_stat_identity(
                entry, _lexical_path(entry["path"])
            )
            if identity_reason:
                break
        if identity_reason:
            skipped.extend(
                {"path": path, "retention_basis": identity_reason}
                for path in paths
            )
            continue
        protected = _apply_protection_keys(
            plan,
            boundary,
            protected_paths=protected_paths,
            active_paths=active_paths,
            receipt_paths=receipt_paths,
        )
        if (
            sealed_directory
            and _sealed_bundle_is_protected(
                members, members[0]["bundle_root"], protected
            )
        ) or (
            single_file
            and members[0].get("kind") == "retained_recovery_archive"
            and _recovery_archive_source_is_protected(
                members[0]["path"], protected
            )
        ) or any(
            any(
                _path_key_overlap(_path_key(entry["path"]), key)
                for key in protected
            )
            for entry in members
        ):
            skipped.extend(
                {"path": path, "retention_basis": "newly_active_or_referenced"}
                for path in paths
            )
            continue
        _validate_exact_scope_root(scope, boundary)
        if any(
            _has_reparse_component(entry["path"], boundary)
            or not _is_within_canonical(entry["path"], boundary)
            for entry in members
        ):
            skipped.extend(
                {
                    "path": path,
                    "retention_basis": "unsafe_reparse_or_canonical_path",
                }
                for path in paths
            )
            continue
        if sealed_directory:
            outcome = _delete_sealed_bundle_with_archive(
                members[0]["bundle_root"], members, verification
            )
            deleted.extend(outcome["deleted"])
            if outcome["archive"]:
                retained_recovery_archives.append(outcome["archive"])
            if not outcome["complete"]:
                skipped.extend(
                    {"path": path, "retention_basis": outcome["retention_basis"]}
                    for path in paths
                    if path not in outcome["deleted"]
                )
            continue

        if verified_original_bundle:
            for entry in sorted(members, key=lambda item: item["path"]):
                candidate = _lexical_path(entry["path"])
                _final_stat, reason = _validate_entry_identity(entry, candidate)
                if reason:
                    skipped.append(
                        {"path": str(candidate), "retention_basis": reason}
                    )
                    continue
                try:
                    candidate.unlink()
                except OSError as exc:
                    skipped.append(
                        {
                            "path": str(candidate),
                            "retention_basis": (
                                f"unlink_failed:{type(exc).__name__}"
                            ),
                        }
                    )
                    continue
                deleted.append(str(candidate))
            continue

        entry = members[0]
        candidate = _lexical_path(entry["path"])
        if entry.get("kind") == "retained_recovery_archive":
            archive_verification = verify_retained_recovery_archive(candidate)
            if not archive_verification["valid"]:
                skipped.append(
                    {
                        "path": str(candidate),
                        "retention_basis": "retained_recovery_archive_invalid",
                    }
                )
                continue
            reconciliation = _reconcile_retained_recovery_archive(
                candidate, archive_verification, allowed_root=boundary
            )
            deleted.extend(reconciliation["deleted"])
            if not reconciliation["complete"]:
                retained_recovery_archives.append(str(candidate))
                skipped.append(
                    {
                        "path": str(candidate),
                        "retention_basis": reconciliation["retention_basis"],
                    }
                )
                continue
        _final_stat, reason = _validate_entry_identity(entry, candidate)
        if reason:
            skipped.append(
                {"path": str(candidate), "retention_basis": reason}
            )
            continue
        try:
            candidate.unlink()
        except OSError as exc:
            skipped.append(
                {
                    "path": str(candidate),
                    "retention_basis": f"unlink_failed:{type(exc).__name__}",
                }
            )
            continue
        deleted.append(str(candidate))
    return {
        "status": "applied",
        "deleted": deleted,
        "skipped": skipped,
        "retained_recovery_archives": retained_recovery_archives,
    }


class RetentionCapacityError(RuntimeError):
    """The managed inventory could not be brought below the hard ceiling."""


def _refresh_plan_summary(plan):
    delete_entries = [
        entry for entry in plan["entries"] if entry["action"] == "delete"
    ]
    plan["planned_delete_count"] = len(delete_entries)
    plan["planned_delete_bytes"] = sum(
        entry["bytes"] for entry in delete_entries
    )
    plan["projected_bytes"] = (
        plan["generated_bytes"] - plan["planned_delete_bytes"]
    )
    bundle_state = {}
    for entry in plan["entries"]:
        bundle_state.setdefault(entry["bundle_id"], []).append(entry)
    for bundle in plan.get("bundles") or ():
        members = bundle_state.get(bundle["bundle_id"]) or ()
        deleting = [member for member in members if member["action"] == "delete"]
        if deleting:
            bundle["action"] = "delete"
            bundle["retention_basis"] = deleting[0]["retention_basis"]
    return plan


def plan_global_retention(
    scopes=DEFAULT_SCOPES,
    *,
    max_age_seconds=DEFAULT_MAX_AGE_SECONDS,
    max_bytes=DEFAULT_TARGET_BYTES,
    reserved_bytes=0,
    active_paths=(),
    include_manual_copies=False,
    now=None,
):
    """Plan age-first cleanup and then one oldest-first aggregate byte pass."""
    max_age_seconds = float(max_age_seconds)
    max_bytes = int(max_bytes)
    reserved_bytes = max(0, int(reserved_bytes))
    if max_age_seconds < 0 or max_age_seconds > DEFAULT_MAX_AGE_SECONDS:
        raise ValueError("max_age_seconds must be between 0 and 3 days")
    if max_bytes < 0 or max_bytes > HARD_MAX_BYTES:
        raise ValueError("max_bytes cannot exceed the 10 GiB hard limit")
    ordered = []
    for scope in scopes:
        if scope not in SUPPORTED_SCOPES:
            raise ValueError(f"unsupported retention scope: {scope}")
        if scope not in ordered:
            ordered.append(scope)
    if not ordered:
        raise ValueError("at least one retention scope is required")

    timestamp = float(time.time() if now is None else now)
    plans = []
    for scope in ordered:
        default = DEFAULT_RETENTION_POLICIES[scope]
        policy = replace(
            default,
            keep_count=0,
            max_age_seconds=max_age_seconds,
            max_bytes=min(max_bytes, HARD_MAX_BYTES),
            dry_run=True,
            include_manual_copies=(
                bool(include_manual_copies)
                if scope == PRODUCTION_BACKUP_SCOPE
                else False
            ),
        )
        plans.append(
            plan_retention(
                expected_root_for_scope(scope),
                scope=scope,
                policy=policy,
                active_paths=active_paths,
                now=timestamp,
                enforce_capacity=False,
            )
        )

    generated_bytes = sum(plan["generated_bytes"] for plan in plans)
    age_delete_bytes = sum(
        entry["bytes"]
        for plan in plans
        for entry in plan["entries"]
        if entry["action"] == "delete"
    )
    projected_bytes = generated_bytes - age_delete_bytes
    candidates = []
    for plan in plans:
        grouped = {}
        for entry in plan["entries"]:
            if entry["action"] != "delete" and entry["retention_basis"] == "within_budget":
                grouped.setdefault(entry["bundle_id"], []).append(entry)
        for bundle_id, members in grouped.items():
            candidates.append(
                {
                    "scope": plan["scope"],
                    "bundle_id": bundle_id,
                    "members": members,
                    "bytes": sum(member["bytes"] for member in members),
                    "effective_time_ns": max(
                        member["effective_time_ns"] for member in members
                    ),
                    "action": "keep",
                    "retention_basis": "within_budget",
                }
            )
    candidates.sort(
        key=lambda item: (
            item["effective_time_ns"],
            item["scope"],
            item["bundle_id"],
        )
    )
    for bundle in candidates:
        if projected_bytes + reserved_bytes < max_bytes:
            break
        if _prepare_bundle_for_deletion(
            bundle, "global_over_max_bytes_oldest_first"
        ):
            projected_bytes -= bundle["bytes"]

    for plan in plans:
        _refresh_plan_summary(plan)
        plan["budget_unmet_bytes"] = 0

    total_with_reservations = projected_bytes + reserved_bytes
    return {
        "schema_version": 1,
        "kind": "speedtree_global_artifact_retention_plan",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "policy": {
            "max_age_seconds": max_age_seconds,
            "max_bytes_exclusive": max_bytes,
            "hard_max_bytes_exclusive": HARD_MAX_BYTES,
            "default_safety_headroom_bytes": DEFAULT_SAFETY_HEADROOM_BYTES,
        },
        "reserved_bytes": reserved_bytes,
        "generated_bytes": generated_bytes,
        "planned_delete_bytes": sum(
            plan["planned_delete_bytes"] for plan in plans
        ),
        "projected_bytes": projected_bytes,
        "projected_with_reservations_bytes": total_with_reservations,
        "capacity_unmet_bytes": max(
            0, total_with_reservations - (max_bytes - 1)
        ),
        "target_satisfied": total_with_reservations < max_bytes,
        "hard_limit_satisfied": total_with_reservations < HARD_MAX_BYTES,
        "plans": plans,
    }


def _retention_state_root():
    return _local_runtime_root() / "artifact_retention"


def _retention_lock_path():
    return _retention_state_root() / "global_retention.lock"


def _reservation_directory():
    return _retention_state_root() / "reservations"


def _active_reservations_unlocked(now=None):
    now = float(time.time() if now is None else now)
    directory = _reservation_directory()
    if not directory.is_dir():
        return []
    active = []
    for path in sorted(directory.glob("*.json")):
        try:
            if path.stat().st_size > 64 * 1024:
                raise ValueError("reservation receipt is too large")
            payload = json.loads(path.read_text(encoding="utf-8"))
            if payload.get("kind") != "speedtree_artifact_output_reservation":
                raise ValueError("unexpected reservation kind")
            pid = int(payload["pid"])
            marker = payload.get("process_start_identity")
            created = float(payload["created_at_epoch"])
            expected = int(payload["expected_bytes"])
            paths = [str(_lexical_path(value)) for value in payload["paths"]]
            alive = process_identity_is_alive(pid, marker)
            if not alive or now - created > RESERVATION_MAX_AGE_SECONDS:
                path.unlink(missing_ok=True)
                continue
            if expected < 0 or not paths:
                raise ValueError("invalid reservation payload")
            payload["expected_bytes"] = expected
            payload["paths"] = paths
            payload["receipt"] = str(path)
            active.append(payload)
        except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
            try:
                path.unlink()
            except OSError:
                pass
    return active


def _apply_global_plan_unlocked(global_plan):
    results = []
    for plan in global_plan["plans"]:
        results.append(
            {
                "scope": plan["scope"],
                **apply_retention_plan(
                    plan,
                    apply=True,
                    root_acknowledgement=(
                        expected_root_for_scope(PRODUCTION_BACKUP_SCOPE)
                        if plan["scope"] == PRODUCTION_BACKUP_SCOPE
                        else None
                    ),
                ),
            }
        )
    return results


def _enforce_global_unlocked(
    scopes=DEFAULT_SCOPES,
    *,
    max_age_seconds=DEFAULT_MAX_AGE_SECONDS,
    max_bytes=DEFAULT_TARGET_BYTES,
    reserved_bytes=0,
    active_paths=(),
    include_manual_copies=False,
):
    plan = plan_global_retention(
        scopes,
        max_age_seconds=max_age_seconds,
        max_bytes=max_bytes,
        reserved_bytes=reserved_bytes,
        active_paths=active_paths,
        include_manual_copies=include_manual_copies,
    )
    results = _apply_global_plan_unlocked(plan)
    verification = plan_global_retention(
        scopes,
        max_age_seconds=max_age_seconds,
        max_bytes=max_bytes,
        reserved_bytes=reserved_bytes,
        active_paths=active_paths,
        include_manual_copies=include_manual_copies,
    )
    observed_with_reservations = verification["generated_bytes"] + reserved_bytes
    return {
        "plan": plan,
        "results": results,
        "verification": verification,
        "observed_bytes": verification["generated_bytes"],
        "observed_with_reservations_bytes": observed_with_reservations,
        "target_satisfied": observed_with_reservations < max_bytes,
        "hard_limit_satisfied": observed_with_reservations < HARD_MAX_BYTES,
    }


def enforce_retention(
    *,
    phase="runtime",
    scopes=DEFAULT_SCOPES,
    max_age_seconds=DEFAULT_MAX_AGE_SECONDS,
    max_bytes=DEFAULT_TARGET_BYTES,
    active_paths=(),
):
    """Apply retention under the process-wide mutex and verify live bytes."""
    SharedJobQueue(default_queue_state_path()).snapshot()
    with InterprocessMutex(
        _retention_lock_path(), timeout=RETENTION_LOCK_TIMEOUT_SECONDS
    ).acquire():
        reservations = _active_reservations_unlocked()
        reserved_bytes = sum(row["expected_bytes"] for row in reservations)
        reserved_paths = tuple(
            path for row in reservations for path in row["paths"]
        )
        outcome = _enforce_global_unlocked(
            scopes,
            max_age_seconds=max_age_seconds,
            max_bytes=max_bytes,
            reserved_bytes=reserved_bytes,
            active_paths=(*active_paths, *reserved_paths),
        )
    outcome["phase"] = str(phase)
    if not outcome["hard_limit_satisfied"]:
        raise RetentionCapacityError(
            "managed artifacts remain at or above 10 GiB after safe cleanup: "
            f"{outcome['observed_with_reservations_bytes']} bytes"
        )
    return outcome


def _begin_output_reservation(paths, expected_bytes):
    normalized = tuple(str(_lexical_path(path)) for path in paths)
    if not normalized:
        raise ValueError("at least one managed output path is required")
    expected_bytes = int(expected_bytes)
    if expected_bytes < 0 or expected_bytes >= HARD_MAX_BYTES:
        raise RetentionCapacityError(
            "one output reservation must be smaller than the 10 GiB hard limit"
        )
    token = uuid.uuid4().hex
    receipt = _reservation_directory() / f"{token}.json"
    with InterprocessMutex(
        _retention_lock_path(), timeout=RETENTION_LOCK_TIMEOUT_SECONDS
    ).acquire():
        reservations = _active_reservations_unlocked()
        other_reserved = sum(row["expected_bytes"] for row in reservations)
        other_paths = tuple(
            path for row in reservations for path in row["paths"]
        )
        outcome = _enforce_global_unlocked(
            reserved_bytes=other_reserved + expected_bytes,
            active_paths=(*other_paths, *normalized),
        )
        if not outcome["target_satisfied"]:
            raise RetentionCapacityError(
                "cannot reserve output without crossing the managed artifact "
                f"target: requested={expected_bytes} bytes"
            )
        payload = {
            "schema_version": 1,
            "kind": "speedtree_artifact_output_reservation",
            "token": token,
            "pid": os.getpid(),
            "process_start_identity": _current_process_start_identity(),
            "created_at_epoch": time.time(),
            "expected_bytes": expected_bytes,
            "paths": list(normalized),
        }
        _atomic_write_json(receipt, payload)
    return receipt


def _finish_output_reservation(receipt):
    with InterprocessMutex(
        _retention_lock_path(), timeout=RETENTION_LOCK_TIMEOUT_SECONDS
    ).acquire():
        reservation_root = _reservation_directory()
        candidate = _lexical_path(receipt)
        if not _is_within_lexical(candidate, reservation_root):
            raise ValueError("reservation receipt escaped its exact directory")
        candidate.unlink(missing_ok=True)
        reservations = _active_reservations_unlocked()
        reserved_bytes = sum(row["expected_bytes"] for row in reservations)
        reserved_paths = tuple(
            path for row in reservations for path in row["paths"]
        )
        outcome = _enforce_global_unlocked(
            reserved_bytes=reserved_bytes,
            active_paths=reserved_paths,
        )
    if not outcome["hard_limit_satisfied"]:
        raise RetentionCapacityError(
            "managed artifacts crossed the 10 GiB hard limit after output"
        )
    return outcome


@contextlib.contextmanager
def managed_output_reservation(paths, expected_bytes):
    """Reserve aggregate capacity before a large write and clean afterward."""
    if isinstance(paths, (str, os.PathLike)):
        paths = (paths,)
    paths = tuple(paths)
    managed_roots = tuple(
        expected_root_for_scope(scope) for scope in DEFAULT_SCOPES
    )
    if not any(
        _is_within_lexical(path, root)
        for path in paths
        for root in managed_roots
    ):
        yield None
        return
    receipt = _begin_output_reservation(paths, expected_bytes)
    try:
        yield receipt
    finally:
        _finish_output_reservation(receipt)


def estimate_output_reservation_bytes(
    source_paths,
    *,
    minimum_bytes=512 * 1024**2,
    multiplier=4,
):
    """Return a conservative reservation for an external/cache producer."""
    if isinstance(source_paths, (str, os.PathLike)):
        source_paths = (source_paths,)
    source_bytes = 0
    for path in source_paths:
        try:
            source_bytes += max(0, int(Path(path).stat().st_size))
        except OSError:
            continue
    estimate = max(int(minimum_bytes), source_bytes * max(1, int(multiplier)))
    return min(estimate, HARD_MAX_BYTES - 1)


def _maintenance_output_path():
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        root = Path(local_app_data) / "SpeedTreeBatchTools" / "artifact_retention"
    else:
        root = Path.cwd() / ".speedtree_artifact_retention"
    return root / "latest_plan.json"


def _policy_with_overrides(
    scope,
    *,
    dry_run=True,
    keep_count=None,
    max_age_days=None,
    min_age_days=None,
    max_gib=None,
    include_manual_copies=False,
    include_retained_recovery_archives=False,
):
    default = DEFAULT_RETENTION_POLICIES[scope]
    return RetentionPolicy(
        keep_count=default.keep_count if keep_count is None else int(keep_count),
        max_age_seconds=(
            default.max_age_seconds
            if max_age_days is None and min_age_days is None
            else float(
                max_age_days if max_age_days is not None else min_age_days
            )
            * 24
            * 60
            * 60
        ),
        max_bytes=(
            default.max_bytes
            if max_gib is None
            else int(float(max_gib) * 1024**3)
        ),
        dry_run=bool(dry_run),
        include_manual_copies=(
            bool(include_manual_copies)
            if scope == PRODUCTION_BACKUP_SCOPE
            else False
        ),
        include_retained_recovery_archives=(
            bool(include_retained_recovery_archives) or default.include_retained_recovery_archives
            if scope == PRODUCTION_BACKUP_SCOPE
            else False
        ),
    )


def run_maintenance(
    scopes,
    *,
    apply=False,
    tree_root_acknowledgement=None,
    include_manual_copies=False,
    include_retained_recovery_archives=False,
    keep_count=None,
    max_age_days=None,
    min_age_days=None,
    max_gib=None,
):
    """Plan/apply the age-first, aggregate policy under one process mutex."""
    ordered_scopes = []
    for scope in scopes:
        if scope not in SUPPORTED_SCOPES:
            raise ValueError(f"unsupported retention scope: {scope}")
        if scope not in ordered_scopes:
            ordered_scopes.append(scope)
    if not ordered_scopes:
        raise ValueError("at least one retention scope is required")
    if include_manual_copies and PRODUCTION_BACKUP_SCOPE not in ordered_scopes:
        raise ValueError(
            "include_manual_copies requires the production_backups scope"
        )
    if tree_root_acknowledgement is not None and (
        _path_key(tree_root_acknowledgement)
        != _path_key(expected_root_for_scope(PRODUCTION_BACKUP_SCOPE))
    ):
        raise ValueError("tree_root_acknowledgement does not match Forestportfolio")
    age_days = (
        3.0
        if max_age_days is None and min_age_days is None
        else float(max_age_days if max_age_days is not None else min_age_days)
    )
    max_age_seconds = age_days * 24 * 60 * 60
    max_bytes = (
        DEFAULT_TARGET_BYTES
        if max_gib is None
        else int(float(max_gib) * 1024**3)
    )
    if apply:
        SharedJobQueue(default_queue_state_path()).snapshot()
    with InterprocessMutex(
        _retention_lock_path(), timeout=RETENTION_LOCK_TIMEOUT_SECONDS
    ).acquire():
        reservations = _active_reservations_unlocked()
        reserved_bytes = sum(row["expected_bytes"] for row in reservations)
        active_paths = tuple(
            path for row in reservations for path in row["paths"]
        )
        global_plan = plan_global_retention(
            ordered_scopes,
            max_age_seconds=max_age_seconds,
            max_bytes=max_bytes,
            reserved_bytes=reserved_bytes,
            active_paths=active_paths,
            include_manual_copies=include_manual_copies,
        )
        if apply:
            results = _apply_global_plan_unlocked(global_plan)
            verification = plan_global_retention(
                ordered_scopes,
                max_age_seconds=max_age_seconds,
                max_bytes=max_bytes,
                reserved_bytes=reserved_bytes,
                active_paths=active_paths,
                include_manual_copies=include_manual_copies,
            )
        else:
            results = [
                {"scope": plan["scope"], "status": "dry_run", "deleted": [], "skipped": []}
                for plan in global_plan["plans"]
            ]
            verification = global_plan
    plans = global_plan["plans"]
    observed_bytes = verification["generated_bytes"]

    return {
        "schema_version": 1,
        "kind": MAINTENANCE_RESULT_KIND,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "status": "applied" if apply else "dry_run",
        "push_independent": True,
        "pipeline_gate": True,
        "automatic_schedule_installed": False,
        "operator_apply_required": not apply,
        "global_plan": global_plan,
        "verification": verification,
        "summary": {
            "scope_count": len(plans),
            "generated_file_count": sum(
                plan["generated_file_count"] for plan in plans
            ),
            "generated_bytes": sum(plan["generated_bytes"] for plan in plans),
            "planned_delete_count": sum(
                plan["planned_delete_count"] for plan in plans
            ),
            "planned_delete_bytes": sum(
                plan["planned_delete_bytes"] for plan in plans
            ),
            "budget_unmet_bytes": verification["capacity_unmet_bytes"],
            "observed_bytes": observed_bytes,
            "observed_with_reservations_bytes": (
                observed_bytes + verification["reserved_bytes"]
            ),
            "strictly_below_10_gib": (
                observed_bytes + verification["reserved_bytes"] < HARD_MAX_BYTES
            ),
            "manifest_sealed_bundle_count": sum(
                plan["manifest_sealed_bundle_count"] for plan in plans
            ),
            "uncertain_backup_bundle_count": sum(
                plan["uncertain_backup_bundle_count"] for plan in plans
            ),
            "producer_manifest_adoption_required": any(
                plan["producer_manifest_adoption_required"] for plan in plans
            ),
            "deleted_count": sum(len(result["deleted"]) for result in results),
            "skipped_count": sum(len(result["skipped"]) for result in results),
            "retained_recovery_archive_count": sum(
                len(result.get("retained_recovery_archives") or ())
                for result in results
            ),
        },
        "plans": plans,
        "results": results,
    }


def _build_cli_parser():
    parser = argparse.ArgumentParser(
        description=(
            "Enforce 3-day retention and one aggregate total strictly below "
            "10 GiB. The default applies cleanup; use --dry-run to inspect."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  Apply every managed scope:\n"
            "    SpeedTree_Artifact_Maintenance.bat\n"
            "  Read-only preview:\n"
            "    SpeedTree_Artifact_Maintenance.bat --dry-run\n"
            "  Apply logs and retry receipts only:\n"
            "    SpeedTree_Artifact_Maintenance.bat --scope logs "
            "--scope retry_progress\n"
            "  Production backups:\n"
            "    SpeedTree_Artifact_Maintenance.bat --scope "
            "production_backups\n\n"
            "Normal GUI launchers run the same enforcement automatically."
        ),
    )
    parser.add_argument(
        "--scope",
        action="append",
        choices=(*DEFAULT_SCOPES, "all"),
        help="Repeat to select scopes; default is every managed scope.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Compatibility flag; cleanup is applied by default.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Write a read-only plan without deleting files.",
    )
    parser.add_argument(
        "--include-manual-copies",
        action="store_true",
        help=(
            "Make explicit ' - Copy' and localized Explorer-copy SPM backup inventory "
            "eligible under the production backup policy."
        ),
    )
    parser.add_argument(
        "--tree-root-ack",
        help=(
            "Optional safety assertion; when supplied it must exactly equal "
            f"{PRODUCTION_TREE_ROOT}"
        ),
    )
    parser.add_argument(
        "--include-recovery-archives",
        action="store_true",
        help=(
            "Include verified complete .retention_delete_*.zip crash bridges "
            "in the production backup policy."
        ),
    )
    parser.add_argument("--keep-count", type=int)
    parser.add_argument("--max-age-days", type=float)
    parser.add_argument("--min-age-days", type=float, help=argparse.SUPPRESS)
    parser.add_argument("--max-gib", type=float)
    parser.add_argument(
        "--output",
        help=(
            "Full JSON result path, '-' for stdout, or omit for the bounded "
            "LOCALAPPDATA latest_plan.json receipt."
        ),
    )
    return parser


def main(argv=None):
    parser = _build_cli_parser()
    args = parser.parse_args(argv)
    if args.keep_count is not None and args.keep_count < 0:
        parser.error("--keep-count must be non-negative")
    if args.max_age_days is not None and not 0 <= args.max_age_days <= 3:
        parser.error("--max-age-days must be between 0 and 3")
    if args.min_age_days is not None and not 0 <= args.min_age_days <= 3:
        parser.error("--min-age-days compatibility value must be between 0 and 3")
    if args.max_gib is not None and args.max_gib < 0:
        parser.error("--max-gib must be non-negative")
    if args.max_gib is not None and args.max_gib > 10:
        parser.error("--max-gib cannot exceed 10")
    requested = args.scope or ["all"]
    scopes = []
    for value in requested:
        values = (
            DEFAULT_SCOPES
            if value == "all"
            else (value,)
        )
        for scope in values:
            if scope not in scopes:
                scopes.append(scope)
    try:
        configured_age = args.max_age_days
        if configured_age is None:
            configured_age = args.min_age_days
        if configured_age is None and os.environ.get("SPEEDTREE_RETENTION_MAX_AGE_DAYS"):
            configured_age = float(os.environ["SPEEDTREE_RETENTION_MAX_AGE_DAYS"])
        configured_gib = args.max_gib
        if configured_gib is None and os.environ.get("SPEEDTREE_RETENTION_MAX_GIB"):
            configured_gib = float(os.environ["SPEEDTREE_RETENTION_MAX_GIB"])
        payload = run_maintenance(
            scopes,
            apply=not args.dry_run,
            tree_root_acknowledgement=args.tree_root_ack,
            include_manual_copies=args.include_manual_copies,
            include_retained_recovery_archives=(
                args.include_recovery_archives
            ),
            keep_count=args.keep_count,
            max_age_days=configured_age,
            min_age_days=args.min_age_days,
            max_gib=configured_gib,
        )
    except ValueError as exc:
        parser.error(str(exc))

    output_text = args.output
    if output_text == "-":
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        output_path = "stdout"
    else:
        output = _lexical_path(output_text or _maintenance_output_path())
        _atomic_write_json(output, payload)
        output_path = str(output)
        print(
            json.dumps(
                {
                    "kind": MAINTENANCE_RESULT_KIND,
                    "status": payload["status"],
                    "output": output_path,
                    "summary": payload["summary"],
                },
                ensure_ascii=False,
            )
        )
    return 0 if payload["summary"]["strictly_below_10_gib"] else 3


__all__ = [
    "DEFAULT_RETENTION_POLICIES",
    "DEFAULT_MAX_AGE_SECONDS",
    "DEFAULT_TARGET_BYTES",
    "DEFAULT_SCOPES",
    "HARD_MAX_BYTES",
    "BACKUP_BUNDLE_MANIFEST_FILENAME",
    "BACKUP_BUNDLE_MANIFEST_KIND",
    "LOG_SCOPE",
    "PCG_REPORT_SCOPE",
    "PROCESS_RECEIPT_SCOPE",
    "PRODUCTION_BACKUP_SCOPE",
    "PRODUCTION_TREE_ROOT",
    "QUEUE_STATE_SCOPE",
    "REGISTERED_BACKUP_MANIFEST_PRODUCERS",
    "REPOSITORY_LOG_ROOT",
    "RETRY_SCOPE",
    "ROOT_DIAGNOSTIC_SCOPE",
    "SHARED_CACHE_SCOPE",
    "SK_CACHE_SCOPE",
    "SPM_REPORT_SCOPE",
    "RetentionCapacityError",
    "RetentionPolicy",
    "apply_retention_plan",
    "expected_root_for_scope",
    "generated_log_kind",
    "generated_production_backup_kind",
    "generated_retry_kind",
    "enforce_retention",
    "estimate_output_reservation_bytes",
    "is_manual_copy_inventory_artifact",
    "main",
    "managed_output_reservation",
    "plan_global_retention",
    "plan_retention",
    "referenced_paths_from_receipts",
    "run_maintenance",
    "seal_backup_transaction",
    "verify_backup_transaction_manifest",
    "verify_retained_recovery_archive",
]


if __name__ == "__main__":
    raise SystemExit(main())
