"""Shared config/helpers for the SK batch pipeline tool.

Pure Python (no bpy). Used by the GUI and by spm_audit; the Blender-side job
scripts under jobs/ are self-contained on purpose (they run inside Blender).
"""
import configparser
import copy
import ctypes
import hashlib
import ipaddress
import json
import os
import re
import subprocess
import sys
import threading
import uuid
import warnings
from datetime import datetime, timezone
from pathlib import Path
from stat import S_ISREG

TOOL_DIR = Path(__file__).resolve().parent
REPO_ROOT = TOOL_DIR.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from speedtree_pipeline_contract import (
    BACKUP_DIRECTORY_NAMES,
    is_live_spm,
    production_spm_folders,
)
from cluster_spm_pair_contract import (
    ClusterSpmPairPathError,
    bootstrap_cluster_authoring,
    inspect_cluster_spm_pair,
    resolve_cluster_spm_pair,
)
from shared_job_queue import InterprocessMutex
from process_lifecycle import (
    ProcessLifecycleError,
    complete_owned_process,
    owned_popen,
    terminate_owned_process,
)
from blender_addon_contract import discover_installed_addon_source


def _default_addon_dir():
    """Use the same BWR source that the newest Blender install will load."""
    override = os.environ.get("SPEEDTREE_BWR_ADDON_DIR")
    if override:
        return Path(override).expanduser()
    installed = discover_installed_addon_source(
        "speedtree_bone_weight_repair"
    )
    if installed is not None:
        return installed
    return (
        REPO_ROOT.parent
        / "speedtree-bone-weight-repair-addon"
        / "addons"
        / "speedtree_bone_weight_repair"
    )


ADDON_DIR = _default_addon_dir()
PRESET_DIR = ADDON_DIR / "presets" / "speedtree_10_1"

CONFIG_PATH = TOOL_DIR / "sk_batch_config.json"
STATE_PATH = TOOL_DIR / "sk_batch_state.json"
UNREAL_WAIT_REFERENCE_FILENAME = "unreal_wait_references.json"
LOG_DIR = TOOL_DIR / "logs"
STATE_RECOVERY_LOG_PATH = LOG_DIR / "state_recovery.log"
STATE_RECOVERY_LOG_MAX_BYTES = 64 * 1024
PUSH_MANIFEST_SCHEMA_VERSION = 1
PUSH_SOURCE_FINGERPRINT_CACHE_VERSION = 1
PUSH_SOURCE_FINGERPRINT_CONTRACT = "content_only_v2"
DEFAULT_SEND2UE_DIR = Path(
    r"C:\Users\PARK\Documents\GitHub\BlenderTools\src\addons\send2ue"
)
DEFAULT_SEND2UE_EXPORT_CACHE_ROOT = Path(
    r"D:\SpeedTreeBatchTools\send2ue_fbx"
)


def send2ue_export_cache_root():
    """Return the non-OneDrive D: workspace for large transient FBX exports."""
    override = os.environ.get("SPEEDTREE_SEND2UE_EXPORT_ROOT")
    if override:
        return Path(override).expanduser().resolve()
    if os.name == "nt":
        return DEFAULT_SEND2UE_EXPORT_CACHE_ROOT
    # Keep tests and non-Windows tooling usable without inventing a literal
    # drive-letter directory. Production Windows runs always use D: above.
    return LOG_DIR / "send2ue_fbx"



DEFAULT_CONFIG = {
    "root": r"D:\OneDrive\Forestportfolio\02_nature\Tree",
    "blender_exe": r"C:\Program Files\Blender Foundation\Blender 5.1\blender.exe",
    "speedtree_exe": r"C:\Program Files\SpeedTree\SpeedTree Modeler v10.1.0\win64\SpeedTree_Modeler.exe",
    "fbx_ini": str(PRESET_DIR / "Options_MA_Fbx.ini"),
    "xml_ini": str(PRESET_DIR / "Options_HI_Xml.ini"),
    # SPM bone calibration (size-aware, total-budget):
    # probe(Absolute/1) counts total branches, then ONE Relative value is solved
    # so total bones ~= min(branches x per_branch, max_total). Small plants land
    # near per_branch; big trees hit the cap, leaving tiny twigs at 0-1 bone.
    "target_bones_per_branch": 2.0,   # small-plant target (bones per branch)
    "max_total_bones": 1500,          # hard cap on a tree's total bones
    "total_window_low": 0.6,          # accept total in [low, high] x target
    "total_window_high": 1.5,
    "seed_relative_value": 0.5,       # first Relative value tried
    "value_cap": 64.0,
    "value_floor": 0.02,
    "max_calibration_rounds": 4,
    "probe_cache_enabled": True,
    # Positive bone receipts are independent of the large GUI state JSON.
    # Keeping them in one ignored tool cache avoids sidecars in asset folders.
    "spm_calibration_receipt_dir": str(TOOL_DIR / "cache" / "spm_calibration"),
    # Cluster prototype SPMs use exactly one Absolute bone on the first
    # renderable structural Branch below each Tree root. Meshless placement
    # Trunks and terminal needle/leaf spines stay at Absolute/0 and never reach
    # Blender as long pivot bones or hundreds of Start/End pairs.
    "cluster_root_only_bones": True,
    "rename_materials": True,    # checklist item 2: M_ prefix
    # Direct leaf-parent Branches receive R=0 at the root and R=1 at the tip.
    # This is tree-only and preserves the established G channel contract.
    "tree_leaf_parent_red_gradient": True,
    "backup_spm": True,
    # SpeedTree startup is expensive, so independent SPMs run concurrently.
    # A single slow export is bounded separately by spm_verify_timeout.
    "spm_parallel_jobs": 4,
    "check_parallel_jobs": 8,
    "blender_parallel_jobs": 2,
    # resource limits (checklist "background + cpu limit")
    "priority": "belownormal",   # idle | belownormal | normal
    "cpu_cores": max(1, (os.cpu_count() or 8) // 2),
    "spm_verify_timeout": 120,
    # Default to the shared owned-streaming Modeler contract. Set false only
    # as an operational fallback to the former regular-temp-file path while a
    # full production calibration batch is being observed end to end.
    "spm_stream_modeler_output": True,
    # Whole-worker lifetime includes waiting for the machine-wide serialized
    # SpeedTree exporter.  The per-export timeout above starts only after the
    # worker owns that gate.
    "spm_job_timeout": 7200,
    "blender_job_timeout": 3600,
    "speedtree_material_preflight_timeout": 900,
    # These are inactivity budgets, not one queue+runtime wall-clock budget.
    # A child progress marker resets the applicable phase budget.
    "speedtree_material_preflight_queue_timeout": 3600,
    "child_stage_inactivity_timeout": 180,
    "child_timeout_grace": 60,
    "cluster_receipt_refresh_timeout": 600,
    "cluster_unit_probe": (
        r"C:\UnrealProjects\MyProject2\work\branch_cluster_uv_audit"
        r"\speedtree_unit_probe_10cm_user_scale_0_1_verified.json"
    ),
    "cluster_capture_resolution": 1024,
    "push_job_timeout": 1800,
    # Avoid wedging Unreal's synchronous RPC queue with assets that are faster
    # to handle manually than to import through the unattended handoff.
    "push_max_polygons": 2_000_000,
    "push_max_bones": 1_500,
    # Successful RPC calls return immediately. These bounds only control how
    # long an unattended Push tolerates a slow import before declaring failure.
    "push_rpc_timeout_min": 180,
    "push_rpc_timeout_max": 900,
    # Headless is the safe default.  Interactive RPC remains available, while
    # ``unreal_wait`` can materialize exports for a later manual headless run.
    "push_transport": "headless",
    "night_headless": True,
    "unreal_editor_cmd": r"C:\Program Files\Epic Games\UE_5.8\Engine\Binaries\Win64\UnrealEditor-Cmd.exe",
    "unreal_project": r"C:\UnrealProjects\MyProject2\MyProject2.uproject",
    "send2ue_dir": str(DEFAULT_SEND2UE_DIR),
    "headless_item_crash_retries": 2,
    "headless_batch_max_restarts": 10,
    "headless_job_timeout": 14_400,
    # Observational warning only: the existing exact-owned process lifecycle
    # remains the sole termination path for a retry worker tree.
    "retry_stall_warning_seconds": 120,
    "retry_owner_lost_seconds": 45,
    "process_poll_interval": 0.2,
}

PRIORITY_FLAGS = {
    "idle": 0x00000040,        # IDLE_PRIORITY_CLASS
    "belownormal": 0x00004000, # BELOW_NORMAL_PRIORITY_CLASS
    "normal": 0x00000020,      # NORMAL_PRIORITY_CLASS
}
CREATE_NO_WINDOW = 0x08000000
CALIBRATION_CACHE_VERSION = 2
# Bump only when the semantic skeleton selection/calibration result changes.
# Ordinary diagnostics, exporter presets and timeout changes are not bone
# contracts and must not schedule every SPM again.
SPM_BONE_CONTRACT_VERSION = 1
_JSON_WRITE_LOCK = threading.RLock()


def unreal_remote_execution_settings(unreal_project):
    """Read Send2UE-compatible RPC discovery settings from an Unreal project.

    Unreal's Python Remote Execution plugin binds multicast discovery to the
    adapter configured in DefaultEngine.ini.  Send2UE also uses its command
    endpoint host as the multicast bind address, so background Blender must
    mirror the project setting instead of assuming loopback.
    """
    if not unreal_project:
        return {}
    project_path = Path(unreal_project).expanduser()
    project_dir = (
        project_path.parent
        if project_path.suffix.casefold() == ".uproject"
        else project_path
    )
    ini_path = project_dir / "Config" / "DefaultEngine.ini"
    if not ini_path.is_file():
        return {}

    parser = configparser.RawConfigParser(
        strict=False,
        interpolation=None,
        allow_no_value=True,
    )
    try:
        parser.read_string(ini_path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, configparser.Error):
        return {}

    section_name = "/Script/PythonScriptPlugin.PythonScriptPluginSettings"
    if not parser.has_section(section_name):
        return {}
    values = {
        str(key).casefold(): str(value or "").strip()
        for key, value in parser.items(section_name)
    }
    enabled = values.get("bremoteexecution", "true").casefold()
    if enabled in {"0", "false", "no", "off"}:
        return {}

    settings = {"config_path": str(ini_path.resolve())}
    bind_address = values.get("remoteexecutionmulticastbindaddress", "")
    try:
        bind_ip = ipaddress.ip_address(bind_address)
        if bind_ip.version == 4 and not bind_ip.is_unspecified:
            settings["multicast_bind_address"] = bind_address
    except ValueError:
        pass

    group_endpoint = values.get("remoteexecutionmulticastgroupendpoint", "")
    group_match = re.fullmatch(r"([^:\s]+):(\d{1,5})", group_endpoint)
    if group_match and 0 < int(group_match.group(2)) <= 65535:
        settings["multicast_group_endpoint"] = group_endpoint

    ttl_text = values.get("remoteexecutionmulticastttl", "")
    try:
        ttl = int(ttl_text)
    except ValueError:
        ttl = None
    if ttl is not None and 0 <= ttl <= 255:
        settings["multicast_ttl"] = ttl
    return settings


def _atomic_write_json(path, data):
    """Serialize JSON without exposing a partially-written state/config file."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(f".{target.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    with _JSON_WRITE_LOCK:
        try:
            payload = json.dumps(data, indent=2, ensure_ascii=False)
            temp.write_text(payload, encoding="utf-8")
            os.replace(temp, target)
        finally:
            if temp.exists():
                temp.unlink()


def atomic_write_bytes(path, payload):
    """Atomically replace a small receipt/report without exposing partial data."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(
        f".{target.name}.{os.getpid()}.{threading.get_ident()}.tmp"
    )
    with _JSON_WRITE_LOCK:
        try:
            try:
                if target.read_bytes() == payload:
                    return
            except OSError:
                pass
            temp.write_bytes(payload)
            os.replace(temp, target)
        finally:
            if temp.exists():
                temp.unlink()


def atomic_write_json(path, data):
    """Public atomic JSON writer for queue manifests and checkpoints."""
    _atomic_write_json(path, data)


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        try:
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            # Only accept keys we still use, so options removed in a redesign
            # (e.g. the old per-branch-average knobs) don't linger in the file.
            cfg.update({k: v for k, v in data.items() if k in DEFAULT_CONFIG})
        except Exception:
            pass
    return cfg


def save_config(cfg):
    _atomic_write_json(CONFIG_PATH, cfg)


def _append_bounded_state_recovery_log(message):
    """Persist a small path-safe recovery notice without unbounded growth."""

    try:
        existing = STATE_RECOVERY_LOG_PATH.read_bytes()
    except OSError:
        existing = b""
    encoded = str(message).encode("utf-8", errors="replace")
    payload = existing + encoded
    if len(payload) > STATE_RECOVERY_LOG_MAX_BYTES:
        payload = payload[-STATE_RECOVERY_LOG_MAX_BYTES:]
        newline = payload.find(b"\n")
        if newline >= 0:
            payload = payload[newline + 1:]
    try:
        atomic_write_bytes(STATE_RECOVERY_LOG_PATH, payload)
    except OSError:
        pass


def _state_mutex():
    """Build the path-scoped process mutex after test/runtime path overrides."""

    return InterprocessMutex(Path(STATE_PATH), timeout=10.0)


def _state_bytes_unchanged(expected):
    try:
        return STATE_PATH.read_bytes() == expected
    except FileNotFoundError:
        return False
    except OSError:
        return False


def _quarantine_unreadable_state(exc, expected_bytes):
    """Move the exact unreadable snapshot aside, or request a re-read."""

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    quarantine = STATE_PATH.with_name(
        f"{STATE_PATH.stem}.unreadable-{stamp}-{uuid.uuid4().hex[:8]}"
        f"{STATE_PATH.suffix}"
    )
    if not _state_bytes_unchanged(expected_bytes):
        return None
    try:
        os.replace(STATE_PATH, quarantine)
    except FileNotFoundError:
        return None
    except OSError as move_exc:
        raise RuntimeError(
            "state_quarantine_failed: unreadable SK Batch state could not "
            "be quarantined; refusing to start with empty state"
        ) from move_exc

    notice = (
        f"[{datetime.now(timezone.utc).isoformat(timespec='seconds')}] "
        f"state_unreadable_quarantined name={quarantine.name} "
        f"({type(exc).__name__})\n"
    )
    _append_bounded_state_recovery_log(notice)
    try:
        warnings.warn(notice.strip(), RuntimeWarning, stacklevel=2)
    except RuntimeWarning:
        pass
    return quarantine


def _state_entry_is_persistable(key):
    """Drop only confirmed dead/backup SPM rows; preserve unknown metadata."""

    if not isinstance(key, str):
        return True
    candidate = Path(key).expanduser()
    if candidate.suffix.casefold() != ".spm":
        return True
    if not is_live_spm(candidate, require_file=False):
        return False
    try:
        return S_ISREG(candidate.stat().st_mode)
    except FileNotFoundError:
        return False
    except OSError:
        # A temporarily unavailable drive or permission boundary is not proof
        # that a production asset is dead.
        return True


def _prune_state_entries(state):
    if not isinstance(state, dict):
        raise ValueError("SK Batch state root must be a JSON object")
    return {
        key: value
        for key, value in state.items()
        if _state_entry_is_persistable(key)
    }


def _unreal_wait_reference_path():
    return Path(STATE_PATH).with_name(UNREAL_WAIT_REFERENCE_FILENAME)


def _write_unreal_wait_references(state, *, previous_state=None):
    """Publish a small retention receipt for only durable Unreal-wait rows.

    ``previous_state`` keeps both sides of a state transition protected until
    the authoritative state replace commits.
    """
    items = []
    seen = set()
    snapshots = (previous_state, state) if previous_state is not None else (state,)
    for snapshot in snapshots:
        for queue_id, entry in sorted(
            snapshot.items(), key=lambda row: str(row[0])
        ):
            if not isinstance(entry, dict):
                continue
            if entry.get("push_status_kind") != "exported_pending_unreal":
                continue
            item = {
                "queue_id": str(queue_id),
                "push_status_kind": "exported_pending_unreal",
                "push_paths": copy.deepcopy(entry.get("push_paths") or {}),
                "push_export_cache": copy.deepcopy(
                    entry.get("push_export_cache") or {}
                ),
            }
            signature = json.dumps(item, ensure_ascii=False, sort_keys=True)
            if signature in seen:
                continue
            seen.add(signature)
            items.append(item)
    _atomic_write_json(
        _unreal_wait_reference_path(),
        {
            "schema_version": 1,
            "kind": "sk_batch_unreal_wait_references",
            "items": items,
        },
    )


def load_state():
    with _JSON_WRITE_LOCK:
        with _state_mutex().acquire():
            for _attempt in range(8):
                try:
                    raw = STATE_PATH.read_bytes()
                except FileNotFoundError:
                    _write_unreal_wait_references({})
                    return {}
                except OSError as exc:
                    raise RuntimeError(
                        "state_read_failed: SK Batch state could not be read"
                    ) from exc
                try:
                    loaded = json.loads(raw)
                    pruned = _prune_state_entries(loaded)
                except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
                    quarantine = _quarantine_unreadable_state(exc, raw)
                    if quarantine is None:
                        continue
                    return {}
                if pruned != loaded:
                    if not _state_bytes_unchanged(raw):
                        continue
                    _atomic_write_json(STATE_PATH, pruned)
                _write_unreal_wait_references(pruned)
                return pruned
    raise RuntimeError(
        "state_changed_during_load: SK Batch state changed repeatedly"
    )


def save_state(state):
    incoming = _prune_state_entries(state)
    with _JSON_WRITE_LOCK:
        with _state_mutex().acquire():
            current = {}
            try:
                raw = STATE_PATH.read_bytes()
            except FileNotFoundError:
                raw = None
            except OSError as exc:
                raise RuntimeError(
                    "state_read_failed: SK Batch state could not be read"
                ) from exc
            if raw is not None:
                try:
                    current = _prune_state_entries(json.loads(raw))
                except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
                    quarantine = _quarantine_unreadable_state(exc, raw)
                    if quarantine is None:
                        raise RuntimeError(
                            "state_changed_during_save: SK Batch state changed "
                            "before quarantine"
                        )
            # State rows are asset-keyed independent updates.  Merging the
            # latest locked snapshot prevents a stale producer from deleting
            # a valid row created by another process; confirmed dead/backup
            # rows were already removed from both sides.
            merged = dict(current)
            merged.update(incoming)
            if raw is not None and STATE_PATH.exists():
                if not _state_bytes_unchanged(raw):
                    raise RuntimeError(
                        "state_changed_during_save: SK Batch state changed "
                        "during the locked transaction"
                    )
            _write_unreal_wait_references(merged, previous_state=current)
            _atomic_write_json(STATE_PATH, merged)
            _write_unreal_wait_references(merged)


def file_content_fingerprint(path, digest_size=16):
    """Fast stable content key; 74 current SPMs hash in roughly 0.1s serial."""
    hasher = hashlib.blake2b(digest_size=digest_size)
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def file_content_snapshot(path):
    """Content fingerprint paired with the exact stat observed while hashing."""
    candidate = Path(path)
    for _attempt in range(2):
        before = candidate.stat()
        fingerprint = file_content_fingerprint(candidate)
        after = candidate.stat()
        before_key = (before.st_size, before.st_mtime_ns)
        after_key = (after.st_size, after.st_mtime_ns)
        if before_key == after_key:
            return {
                "fingerprint": fingerprint,
                "size": after.st_size,
                "mtime_ns": after.st_mtime_ns,
            }
    raise RuntimeError(f"File changed while hashing: {candidate}")


def cached_file_content_snapshot(path, cache=None):
    """Reuse a verified content snapshot while the file stat is unchanged."""
    candidate = Path(path)
    stat = candidate.stat()
    if (
        isinstance(cache, dict)
        and cache.get("fingerprint")
        and cache.get("size") == stat.st_size
        and cache.get("mtime_ns") == stat.st_mtime_ns
    ):
        return {
            "fingerprint": cache["fingerprint"],
            "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
        }, True
    return file_content_snapshot(candidate), False


def _push_source_stat_identity(path, required=False):
    candidate = Path(path)
    if not candidate.is_file():
        if required:
            raise FileNotFoundError(f"Push source file missing: {candidate}")
        return {"path": str(candidate), "missing": True}
    stat = candidate.stat()
    return {
        "path": str(candidate.resolve()),
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
    }


def push_source_snapshot(blend_path, dependency_paths=()):
    """Cheap source identity used to decide whether a full content hash is needed."""
    return {
        "blend": _push_source_stat_identity(blend_path, required=True),
        "dependencies": [
            _push_source_stat_identity(path) for path in dependency_paths
        ],
    }


def _push_source_fingerprint_record(blend_path, dependency_paths=()):
    """Hash one stable cross-file snapshot and return its persistent cache record."""
    dependency_paths = tuple(dependency_paths)
    for _attempt in range(2):
        blend_candidate = Path(blend_path)
        blend_content = file_content_snapshot(blend_candidate)
        blend_path_resolved = str(blend_candidate.resolve())
        blend_identity = {
            "path": blend_path_resolved,
            "fingerprint": blend_content["fingerprint"],
        }
        snapshot = {
            "blend": {
                "path": blend_path_resolved,
                "size": blend_content["size"],
                "mtime_ns": blend_content["mtime_ns"],
            },
            "dependencies": [],
        }
        dependencies = []
        for path in dependency_paths:
            candidate = Path(path)
            if not candidate.is_file():
                missing = {"path": str(candidate), "missing": True}
                dependencies.append(missing)
                snapshot["dependencies"].append(dict(missing))
                continue
            content = file_content_snapshot(candidate)
            resolved = str(candidate.resolve())
            dependencies.append(
                {"path": resolved, "fingerprint": content["fingerprint"]}
            )
            snapshot["dependencies"].append(
                {
                    "path": resolved,
                    "size": content["size"],
                    "mtime_ns": content["mtime_ns"],
                }
            )

        # A source changing after its own hash but before the set completed must
        # not be cached as one coherent export input.
        if snapshot != push_source_snapshot(blend_path, dependency_paths):
            continue
        payload = {
            "schema_version": PUSH_MANIFEST_SCHEMA_VERSION,
            "blend": blend_identity,
            "dependencies": dependencies,
        }
        encoded = json.dumps(
            payload,
            sort_keys=True,
            ensure_ascii=False,
        ).encode("utf-8")
        return {
            "version": PUSH_SOURCE_FINGERPRINT_CACHE_VERSION,
            "fingerprint_contract": PUSH_SOURCE_FINGERPRINT_CONTRACT,
            "fingerprint": hashlib.blake2b(encoded, digest_size=16).hexdigest(),
            "snapshot": snapshot,
        }
    raise RuntimeError("Push source set changed while hashing")


def push_source_fingerprint(blend_path, dependency_paths=()):
    """Fingerprint the Blender export input and per-asset content contracts."""
    return _push_source_fingerprint_record(
        blend_path,
        dependency_paths,
    )["fingerprint"]


def _legacy_push_source_snapshot_covers(legacy_snapshot, current_snapshot):
    """Return whether a pre-v2 snapshot proves the current content-only set."""
    if not isinstance(legacy_snapshot, dict):
        return False
    if legacy_snapshot.get("blend") != current_snapshot.get("blend"):
        return False
    legacy_dependencies = legacy_snapshot.get("dependencies")
    current_dependencies = current_snapshot.get("dependencies")
    if not isinstance(legacy_dependencies, list) or not isinstance(
        current_dependencies, list
    ):
        return False
    return all(
        identity in legacy_dependencies for identity in current_dependencies
    )


def push_source_cache_matches_snapshot(cache, snapshot):
    """Validate a current cache or a legacy code-inclusive cache safely."""
    if not isinstance(cache, dict) or not isinstance(snapshot, dict):
        return False
    if cache.get("version") != PUSH_SOURCE_FINGERPRINT_CACHE_VERSION:
        return False
    fingerprint = cache.get("fingerprint")
    if not isinstance(fingerprint, str) or not fingerprint:
        return False
    cached_snapshot = cache.get("snapshot")
    if cached_snapshot == snapshot:
        return True
    return (
        not cache.get("fingerprint_contract")
        and _legacy_push_source_snapshot_covers(cached_snapshot, snapshot)
    )


def cached_push_source_fingerprint(blend_path, dependency_paths=(), cache=None):
    """Reuse a stored content fingerprint when every source stat is unchanged."""
    dependency_paths = tuple(dependency_paths)
    snapshot = push_source_snapshot(blend_path, dependency_paths)
    cache = cache if isinstance(cache, dict) else {}
    fingerprint = cache.get("fingerprint")
    cache_valid = (
        push_source_cache_matches_snapshot(cache, snapshot)
        and re.fullmatch(r"[0-9a-f]{32}", fingerprint) is not None
    )
    if cache_valid:
        return fingerprint, {
            "version": PUSH_SOURCE_FINGERPRINT_CACHE_VERSION,
            "fingerprint_contract": PUSH_SOURCE_FINGERPRINT_CONTRACT,
            "fingerprint": fingerprint,
            "snapshot": snapshot,
        }, True
    record = _push_source_fingerprint_record(blend_path, dependency_paths)
    return record["fingerprint"], record, False


def manifest_item_files_match(item):
    """Validate immutable cached export artifacts referenced by one item.

    ``code_files`` remain in manifests for diagnostics and Unreal recovery, but
    code edited after export cannot change an already-written FBX or handoff.
    """
    if not isinstance(item, dict) or not item.get("fingerprint"):
        return False
    groups = (
        item.get("exported_files") or [],
        item.get("handoff_files") or [],
        [item["wind_file"]] if item.get("wind_file") else [],
    )
    for group in groups:
        for identity in group:
            try:
                path = Path(identity["path"])
                if not path.is_file():
                    return False
                stat = path.stat()
                if stat.st_size != identity.get("size"):
                    return False
                if not identity.get("fingerprint"):
                    return False
                # New manifests retain the exact stat observed after hashing.
                # Reusing a multi-GB cached FBX should be an O(1) metadata check,
                # not another full disk read.  Older manifests have no mtime and
                # deliberately fall back to the content hash once.
                if identity.get("mtime_ns") == stat.st_mtime_ns:
                    continue
                if file_content_fingerprint(path) != identity.get("fingerprint"):
                    return False
            except (KeyError, OSError, TypeError):
                return False
    return True


def _dependency_identity(path, hash_content=False, include_hashed_stat=False):
    candidate = Path(path) if path else None
    if not candidate or not candidate.exists():
        return {"path": str(candidate or ""), "missing": True}
    try:
        stat = candidate.stat()
        identity = {"path": str(candidate.resolve())}
        if hash_content:
            identity["fingerprint"] = file_content_fingerprint(candidate)
            if include_hashed_stat:
                identity["size"] = stat.st_size
                identity["mtime_ns"] = stat.st_mtime_ns
        else:
            identity["size"] = stat.st_size
            identity["mtime_ns"] = stat.st_mtime_ns
        return identity
    except OSError as exc:
        return {"path": str(candidate), "error": str(exc)}


def _legacy_calibration_settings_signature(cfg):
    """Return the pre-semantic signature for one-time GUI-state migration."""
    setting_keys = (
        "target_bones_per_branch",
        "max_total_bones",
        "total_window_low",
        "total_window_high",
        "seed_relative_value",
        "value_cap",
        "value_floor",
        "max_calibration_rounds",
        "probe_cache_enabled",
        "cluster_root_only_bones",
        "spm_verify_timeout",
        "rename_materials",
        "tree_leaf_parent_red_gradient",
    )
    payload = {
        "version": CALIBRATION_CACHE_VERSION,
        "settings": {key: cfg.get(key) for key in setting_keys},
        # Last broad-signature producer before the semantic contract split.
        # Keeping its exact content identity lets the current in-progress
        # estate migrate once even though this module now imports the durable
        # receipt fast-path.
        "spm_audit": {
            "path": str((TOOL_DIR / "spm_audit.py").resolve()),
            "fingerprint": "d45ea6e72c0b1e506b4f07d97c6d7610",
        },
        "xml_ini": _dependency_identity(
            cfg.get("xml_ini"),
            hash_content=True,
            include_hashed_stat=False,
        ),
        "fbx_ini": _dependency_identity(
            cfg.get("fbx_ini"),
            hash_content=True,
            include_hashed_stat=False,
        ),
        # Hashing the large executable would erase the speed win; size+mtime
        # changes whenever the installed SpeedTree build is replaced.
        "speedtree_exe": _dependency_identity(cfg.get("speedtree_exe")),
    }
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.blake2b(encoded, digest_size=16).hexdigest()


def calibration_settings_signature(cfg):
    """Hash only settings that can change the semantic skeleton result."""
    setting_keys = (
        "target_bones_per_branch",
        "max_total_bones",
        "total_window_low",
        "total_window_high",
        "seed_relative_value",
        "value_cap",
        "value_floor",
        "max_calibration_rounds",
        "cluster_root_only_bones",
    )
    payload = {
        "bone_contract_version": SPM_BONE_CONTRACT_VERSION,
        "settings": {key: cfg.get(key) for key in setting_keys},
    }
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.blake2b(encoded, digest_size=16).hexdigest()


def legacy_calibration_settings_signature(cfg):
    """Return the former broad signature so current state migrates once."""
    return _legacy_calibration_settings_signature(cfg)


def calibration_cache_matches(
    cache,
    spm_fingerprint,
    settings_signature,
    legacy_settings_signature=None,
):
    accepted_signatures = {settings_signature}
    if legacy_settings_signature:
        accepted_signatures.add(legacy_settings_signature)
    return bool(
        isinstance(cache, dict)
        and cache.get("version") == CALIBRATION_CACHE_VERSION
        and cache.get("spm_fingerprint") == spm_fingerprint
        and cache.get("settings_signature") in accepted_signatures
    )


def load_job_report(path):
    """Best-effort job JSON loader; malformed/missing reports stay diagnosable."""
    report_path = Path(path)
    if not report_path.exists():
        return {"_report_error": "job report was not created"}
    try:
        data = json.loads(report_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"_report_error": f"job report could not be read: {exc}"}
    if isinstance(data, list):
        data = data[0] if data and isinstance(data[0], dict) else {}
    return data if isinstance(data, dict) else {"_report_error": "job report is not an object"}


def compact_error_message(message, max_chars=100):
    """One-line status text, without a long log path or traceback whitespace."""
    text = re.sub(r"\s+", " ", str(message or "")).strip()
    text = re.sub(r"\s+[—-]\s+로그:\s+.*$", "", text, flags=re.IGNORECASE)
    if not text:
        return "원인 확인 불가"
    if len(text) > max_chars:
        return text[: max(1, max_chars - 1)].rstrip() + "…"
    return text


def _read_log_tail(path, max_bytes=65536):
    try:
        log_path = Path(path)
        with log_path.open("rb") as handle:
            size = handle.seek(0, 2)
            handle.seek(max(0, size - max_bytes))
            return handle.read().decode("utf-8", errors="replace")
    except Exception:
        return ""


PUSH_ABORT_KINDS = frozenset(
    {"unreal_crash", "unreal_unavailable", "rpc_timeout", "push_timeout"}
)


def classify_push_failure(report=None, log_path=None):
    """Classify whether a Push failure is item-local or poisons the UE queue."""
    report = report if isinstance(report, dict) else {}
    explicit = report.get("failure_kind")
    if explicit:
        return str(explicit)
    if report.get("status") == "manual_required" or report.get("manual_required"):
        return "manual_required"

    text = "\n".join(
        str(value)
        for value in (
            report.get("error"),
            report.get("reason"),
            report.get("message"),
            report.get("traceback"),
            report.get("trace"),
            report.get("_report_error"),
            _read_log_tail(log_path) if log_path else "",
        )
        if value
    )
    lowered = text.lower()
    if "unreal editor crashed or exited during push" in lowered:
        return "unreal_crash"
    if any(
        token in lowered
        for token in (
            'the call "import_asset" timed out',
            "rpc timeout",
            "rpc_time_out",
            "no result file (timed out",
        )
    ):
        return "rpc_timeout"
    if any(
        token in lowered
        for token in (
            "could not find an open unreal editor instance",
            "rpc not reachable",
            "unreal editor is not running",
            "connectionreseterror",
            "winerror 10054",
        )
    ):
        if report.get("unreal_editor_running_after_failure") is False:
            return "unreal_crash"
        return "unreal_unavailable"
    return "data_error"


def summarize_job_failure(report=None, log_path=None, max_chars=100):
    """Extract a short actionable cause from Blender/Unreal reports and logs."""
    report = report if isinstance(report, dict) else {}
    report_sources = []
    for key in ("error", "reason", "message", "traceback", "trace", "_report_error"):
        value = report.get(key)
        if value:
            report_sources.append(str(value))
    wind = report.get("wind")
    if isinstance(wind, dict):
        for key in ("error", "trace"):
            if wind.get(key):
                report_sources.append(str(wind[key]))
    report_text = "\n".join(report_sources)
    log_text = _read_log_tail(log_path) if log_path else ""
    text = "\n".join(value for value in (report_text, log_text) if value)
    lowered = text.lower()

    empty_slot = re.search(
        r"(?:mesh\s+['\"]([^'\"]+)['\"]\s+)?slot\s+(\d+)\s+has no material",
        text,
        re.IGNORECASE,
    )
    if empty_slot:
        mesh_name = empty_slot.group(1)
        slot_index = empty_slot.group(2)
        location = f"{mesh_name} slot {slot_index}" if mesh_name else f"slot {slot_index}"
        return compact_error_message(
            f"머티리얼 빈 슬롯: {location} — ② Blender Repair에서 확인 필요",
            max_chars,
        )

    if "unreal editor crashed or exited during push" in lowered:
        return "Unreal Editor 크래시 — Push 중 에디터 종료"

    failure_kind = classify_push_failure(report, log_path)
    if failure_kind == "manual_required":
        primary = report.get("error") or report.get("reason") or report.get("message")
        return compact_error_message(primary or "Unreal Push 수동 처리 필요", max_chars)
    if failure_kind == "rpc_timeout":
        return "Unreal RPC 시간 초과 — 큐 응답 정지"
    if failure_kind == "unreal_crash":
        return "Unreal Editor 크래시 — Push 중 에디터 종료"
    if failure_kind == "unreal_unavailable":
        return "Unreal 연결 실패 — 에디터/RPC 응답 없음"

    if any(
        token in lowered
        for token in (
            "could not find an open unreal editor instance",
            "rpc not reachable",
            "unreal editor is not running",
            "connectionreseterror",
            "winerror 10054",
        )
    ):
        return "Unreal 연결 실패 — 에디터/RPC 응답 없음"

    mesh_match = re.search(r"mesh not found:\s*([^\r\n(]+)", text, re.IGNORECASE)
    if mesh_match:
        return compact_error_message(f"Unreal 메시를 찾지 못함: {mesh_match.group(1).strip()}", max_chars)

    if "codexdynamicwindimportlibrary missing" in lowered:
        return "Unreal Codex 플러그인 미로드"
    if "unreal wind import: no result file" in lowered or "wind import" in lowered and "timed out" in lowered:
        return "Unreal wind 처리 시간 초과"
    if "unreal wind import failed" in lowered:
        match = re.search(r"unreal wind import failed:\s*([^\r\n]+)", text, re.IGNORECASE)
        detail = match.group(1).strip() if match else "상세 결과는 로그 확인"
        return compact_error_message(f"Unreal wind 적용 실패: {detail}", max_chars)
    if "armature-only" in lowered or "contains no mesh geometry" in lowered:
        return "FBX 메시 지오메트리 없음"
    if ".crash.txt" in lowered or "writing:" in lowered and "blender" in lowered:
        return "Blender 백그라운드 크래시"
    if "speedtree" in lowered and "export" in lowered and "fail" in lowered:
        return "SpeedTree export 실패"
    if "not sk-ready" in lowered or "bones disabled" in lowered:
        return "SK 본 설정 미완료"
    if "no module named" in lowered or "addon_enable" in lowered and "error" in lowered:
        return "Blender add-on 로드 실패"
    if "send2ue returned" in lowered:
        match = re.search(r"send2ue returned\s*([^\r\n]+)", text, re.IGNORECASE)
        return compact_error_message(f"Send to Unreal 실행 실패: {(match.group(1) if match else '').strip()}", max_chars)
    if "export_from_speedtree returned" in lowered:
        match = re.search(r"export_from_speedtree returned\s*([^\r\n]+)", text, re.IGNORECASE)
        return compact_error_message(f"Blender repair 실행 실패: {(match.group(1) if match else '').strip()}", max_chars)

    # A structured report is authoritative. Blender shutdown can append noisy
    # unregister_class exceptions after the real item error; those must never
    # replace the cause shown in GUI/state.
    exception_text = report_text or log_text
    exception_lines = []
    for line in exception_text.splitlines():
        stripped = line.strip()
        if re.match(r"^[A-Za-z_][\w.]*?(?:Error|Exception):\s*.+", stripped):
            exception_lines.append(stripped)
    if exception_lines:
        return compact_error_message(exception_lines[-1], max_chars)

    primary = report.get("error") or report.get("reason") or report.get("message")
    if primary:
        first_line = next((line.strip() for line in str(primary).splitlines() if line.strip()), primary)
        return compact_error_message(first_line, max_chars)
    return compact_error_message(report.get("_report_error") or "원인 확인 불가", max_chars)


BACKUP_SUBDIR = "_spm_backups"
MANUAL_BONES_SUFFIX = ".skbatch_manual_bones.json"
# Older runs used a per-tool folder name; still skip it so stragglers never
# reappear in the working list.
LEGACY_BACKUP_SUBDIRS = ("_skbatch_backup", "_pcgtex_backups")


def manual_bones_marker_path(spm_path):
    """Persistent marker stored beside the SPM backups, outside the scan list."""
    spm = Path(spm_path)
    return spm.parent / BACKUP_SUBDIR / f"{spm.stem}{MANUAL_BONES_SUFFIX}"


def is_manual_bones_locked(spm_path, state_entry=None):
    """State is the fast local cache; the marker survives GUI/repo moves."""
    if state_entry and state_entry.get("manual_bones_locked", False):
        return True
    return manual_bones_marker_path(spm_path).exists()


def set_manual_bones_marker(spm_path, locked):
    marker = manual_bones_marker_path(spm_path)
    if locked:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(
            json.dumps(
                {
                    "version": 1,
                    "spm": str(Path(spm_path)),
                    "manual_bones_locked": True,
                    "note": "Preserve user-authored SpeedTree bone settings; skip automatic calibration.",
                },
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    elif marker.exists():
        marker.unlink()
    return marker


def scan_sk_spms(root):
    """Non-Cluster SK inputs; Cluster canonical rows come from pair inventory.

    Only ``<owner>/SK_x.spm`` is a shipping identity.  Timestamped safety
    copies, capture staging and verify candidates all reuse the same SK_ name
    deeper in the tree, so a recursive walk drowned the real rows; the location
    contract keeps the list to what can actually be pushed.
    """
    out = []
    for folder in production_spm_folders(root):
        if folder.name.casefold() == "cluster":
            continue
        try:
            names = sorted(path.name for path in folder.iterdir() if path.is_file())
        except OSError:
            continue
        for name in names:
            if not name.lower().startswith("sk_") or not name.lower().endswith(".spm"):
                continue
            candidate = folder / name
            if not is_live_spm(candidate):
                continue
            out.append(candidate)
    return sorted(out)


def _connected_cluster_rows_by_owner(owner_clusters, metrics=None):
    """Return PCG-equivalent connections from one shared bounded inventory."""
    from pcg_st9_texture_batch.cluster_connection_index import (
        cluster_connection_rows_by_owner,
    )

    return cluster_connection_rows_by_owner(
        owner_clusters,
        metrics=metrics,
    )


def _path_key(value):
    return os.path.normcase(os.path.abspath(str(value))).casefold()


def scan_cluster_spm_sources(root, metrics=None):
    """Connected Cluster outputs, normalized to one canonical SK row.

    A legacy unprefixed file remains a discoverable normalization input only
    while its canonical ``SK_`` output is missing.  Once canonical exists, all
    downstream identities use it and never publish back to the legacy name.
    """
    inventory = {}
    root = Path(root)
    if not root.exists():
        return inventory
    for cluster_folder in production_spm_folders(root):
        if cluster_folder.name.casefold() != "cluster":
            continue
        try:
            names = sorted(
                path.name for path in cluster_folder.iterdir() if path.is_file()
            )
        except OSError:
            continue
        for name in names:
            lowered = name.casefold()
            if (
                not lowered.endswith(".spm")
                or lowered.startswith("~")
            ):
                continue
            source = cluster_folder / name
            if not is_live_spm(source):
                continue
            try:
                pair = resolve_cluster_spm_pair(source)
            except ClusterSpmPairPathError:
                continue
            inventory.setdefault(pair["pair_id"], pair)

    rows = []
    by_owner = {}
    for pair in inventory.values():
        preview = inspect_cluster_spm_pair(pair["canonical_spm"])
        row = {
            "kind": "cluster_spm_output",
            "source_spm": pair["canonical_spm"],
            "authoring_spm": pair["canonical_spm"],
            "output_spm": pair["canonical_spm"],
            "legacy_output_spm": pair["mirror_spm"],
            "blend_path": pair["canonical_spm"].with_suffix(".blend"),
            "cluster_folder": pair["canonical_spm"].parent,
            "owner_folder": pair["canonical_spm"].parent.parent,
            "display_name": pair["canonical_spm"].name,
            "pair_status": preview["status"],
            "pair_action": preview["action"],
            "pair_generation": preview["generation"],
            "pair_conflicts": tuple(preview["conflicts"]),
            "can_bootstrap": bool(preview["can_bootstrap"]),
            "can_publish": bool(preview["can_publish"]),
            "pair_receipt": pair["receipt_path"],
            # Compatibility alias for state written by the former raw-row UI.
            "legacy_sk_spm": pair["canonical_spm"],
        }
        by_owner.setdefault(row["owner_folder"], []).append(row)
    sources_by_owner = {
        owner_folder: [
            row["output_spm"]
            if row["output_spm"].is_file()
            else row["legacy_output_spm"]
            for row in owner_rows
        ]
        for owner_folder, owner_rows in by_owner.items()
    }
    connections_by_owner = _connected_cluster_rows_by_owner(
        sources_by_owner,
        metrics=metrics,
    )
    for owner_folder, owner_rows in by_owner.items():
        connection_by_source = {
            _path_key(row.get("source_spm") or row.get("authoring_spm")): row
            for row in connections_by_owner.get(
                Path(owner_folder).absolute(), ()
            )
        }
        for row in owner_rows:
            connection = connection_by_source.get(
                _path_key(row["output_spm"])
            ) or connection_by_source.get(
                _path_key(row["legacy_output_spm"])
            ) or {}
            connected = tuple(connection.get("cluster_output_textures") or ())
            if not connected and not row["authoring_spm"].is_file():
                continue
            row["connected_output_textures"] = connected
            row["missing_output_textures"] = tuple(
                connection.get("missing_cluster_output_textures") or ()
            )
            row["referenced_by_spms"] = tuple(
                connection.get("referenced_by_spms") or ()
            )
            rows.append(row)
    return sorted(rows, key=lambda row: str(row["authoring_spm"]).casefold())


def speedtree_output_spm_for(spm_path):
    """Return the canonical SK output identity for one SK Batch row."""
    candidate = Path(spm_path)
    try:
        return resolve_cluster_spm_pair(candidate)["canonical_spm"]
    except ClusterSpmPairPathError:
        return candidate


def prepare_cluster_spm_pair_for_job(spm_path):
    """Normalize a legacy Cluster output name before an SK job starts."""
    candidate = Path(spm_path)
    try:
        preview = inspect_cluster_spm_pair(candidate)
    except ClusterSpmPairPathError:
        return {"status": "not_applicable", "canonical_spm": candidate,
                "mirror_spm": candidate, "operation": "none"}
    if preview["can_bootstrap"]:
        return bootstrap_cluster_authoring(preview["mirror_spm"])
    if Path(preview["canonical_spm"]).is_file():
        return {
            "status": "up_to_date",
            "operation": "none",
            "generation": preview["generation"],
            "canonical_spm": preview["canonical_spm"],
            "mirror_spm": preview["mirror_spm"],
            "output_spm": preview["canonical_spm"],
            "receipt_path": preview["receipt_path"],
        }
    raise RuntimeError(
        f"Cluster SPM output name cannot be normalized ({preview['status']}): "
        + "; ".join(preview["conflicts"])
    )


# Wind preset from the file name (checklist item 4). Dead vegetation maps to
# the shared NONE response slot, whose default values are zero.
def normalize_wind_override(value):
    normalized = str(value or "auto").strip().upper()
    if normalized == "AUTO":
        return "auto"
    if normalized == "GRASS":
        return "WEED"
    return normalized if normalized in {"TREE", "BUSH", "WEED", "NONE"} else "auto"


def wind_preset_for(stem):
    s = stem.lower()
    if "deadleave" in s or "deadbranch" in s:
        return "NONE"
    if "tree" in s:
        return "TREE"
    if "bush" in s:
        return "BUSH"
    if "weed" in s or "grass" in s:
        return "WEED"
    return "WEED"


def wind_preset_for_spm(spm_path):
    """Resolve Cluster child wind from its owning vegetation folder."""
    spm = Path(spm_path)
    owner = spm.parent.parent.name if spm.parent.name.casefold() == "cluster" else ""
    return wind_preset_for(f"{spm.stem} {owner}")


def blend_path_for(spm_path):
    """Return the canonical Blend identity for ordinary or Cluster inputs."""
    spm = Path(spm_path)
    try:
        spm = resolve_cluster_spm_pair(spm)["canonical_spm"]
    except ClusterSpmPairPathError:
        pass
    return spm.with_suffix(".blend")


def blender_open_file_window_titles(blend_path):
    """Return interactive Blender windows that currently hold this blend."""
    if os.name != "nt":
        return []
    try:
        expected = str(Path(blend_path).resolve()).casefold()
    except (OSError, ValueError):
        return []
    titles = []
    try:
        user32 = ctypes.windll.user32
        callback_type = ctypes.WINFUNCTYPE(
            ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p
        )

        @callback_type
        def collect(window, _extra):
            length = user32.GetWindowTextLengthW(window)
            if length <= 0:
                return True
            buffer = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(window, buffer, length + 1)
            title = buffer.value
            lowered = title.casefold()
            if "blender" in lowered and expected in lowered:
                titles.append(title)
            return True

        user32.EnumWindows(collect, 0)
    except (AttributeError, OSError):
        return []
    return titles


def set_process_affinity(pid, cores):
    """Limit a process to the first `cores` logical CPUs (inherited by children)."""
    import ctypes

    total = os.cpu_count() or 1
    cores = max(1, min(int(cores), total))
    if cores >= total:
        return False
    mask = (1 << cores) - 1
    PROCESS_SET_INFORMATION = 0x0200
    PROCESS_QUERY_INFORMATION = 0x0400
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(PROCESS_SET_INFORMATION | PROCESS_QUERY_INFORMATION, False, pid)
    if not handle:
        return False
    try:
        return bool(kernel32.SetProcessAffinityMask(handle, mask))
    finally:
        kernel32.CloseHandle(handle)


def attach_process_kill_job(proc):
    """Compatibility shim for callers migrated to the shared supervisor."""

    job = getattr(proc, "speedtree_lifecycle_tree_job", None)
    proc.sk_job_handle = job
    return job is not None


def close_process_kill_job(proc):
    """Finalize a completed shared-supervisor launch and its receipt."""

    if getattr(proc, "speedtree_lifecycle_launch_id", None) is None:
        return False
    try:
        complete_owned_process(proc, reason="sk_worker_complete")
        proc.sk_job_handle = None
        return True
    except ProcessLifecycleError:
        return False


def launch_limited(
    cmd,
    cfg,
    log_file=None,
    cwd=None,
    affinity=True,
    env=None,
    cooperative_cancel=None,
):
    """Start a background child at reduced priority + optional CPU affinity.

    Priority class and affinity are inherited by grandchildren (Blender ->
    SpeedTree CLI), so one launch covers the whole job tree. Returns Popen.

    affinity=False leaves the child free to use every core: use this when the
    caller runs several children at once, where the whole point is to spread
    the (cold-start-bound) SpeedTree exports across all cores. Priority alone
    keeps the machine responsive.
    """
    flags = PRIORITY_FLAGS.get(cfg.get("priority", "belownormal"), PRIORITY_FLAGS["belownormal"])
    flags |= CREATE_NO_WINDOW
    handle = open(log_file, "w", encoding="utf-8", errors="replace") if log_file else None
    try:
        proc = owned_popen(
            cmd,
            source="sk_batch.sk_common.launch_limited",
            popen_factory=subprocess.Popen,
            cooperative_cancel=cooperative_cancel,
            stdout=handle if handle else subprocess.DEVNULL,
            stderr=subprocess.STDOUT,
            cwd=cwd,
            env=env,
            creationflags=flags,
        )
    except Exception:
        if handle:
            handle.close()
        raise
    proc.sk_log_handle = handle  # caller closes after wait (see GUI _run_limited)
    attach_process_kill_job(proc)
    try:
        if affinity:
            set_process_affinity(proc.pid, cfg.get("cpu_cores", os.cpu_count()))
    except Exception:
        pass
    return proc


def terminate_process_tree(proc, wait_seconds=5.0):
    """Terminate a managed process and all of its descendants.

    Killing only the Python wrapper leaves SpeedTree_Modeler.exe orphaned on
    Windows. Repeated stop/retry cycles then accumulate multi-GB workers and
    make later calibration batches appear hung.

    Returns True when Windows confirms the tree kill (or the process was already
    gone). A direct-process kill remains as a last resort, but returns False
    because descendants could not be confirmed terminated.
    """
    if getattr(proc, "speedtree_lifecycle_launch_id", None) is not None:
        try:
            terminate_owned_process(
                proc,
                reason="sk_stop",
                terminate_grace=min(1.0, max(0.0, float(wait_seconds))),
                kill_grace=max(0.1, float(wait_seconds)),
            )
            return proc.poll() is not None
        except ProcessLifecycleError:
            return False

    # Fail closed for legacy/injected process objects: signal only the exact
    # retained handle and never discover or kill descendants by PID/name.
    if proc.poll() is not None:
        return True
    try:
        proc.terminate()
        proc.wait(timeout=max(0.1, float(wait_seconds)))
    except (OSError, subprocess.TimeoutExpired):
        if proc.poll() is None:
            try:
                proc.kill()
                proc.wait(timeout=max(0.1, float(wait_seconds)))
            except (OSError, subprocess.SubprocessError):
                pass
    return False
