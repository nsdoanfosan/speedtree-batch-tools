"""Durable result and failed-unit retry contracts for connected-board runs.

This module deliberately contains no Tk code and performs no production
mutation.  It gives the GUI stable unit identities, content fingerprints,
failure classifications, bounded publish retries, and compact shared-queue
receipts.
"""

from __future__ import annotations

import copy
import fnmatch
import hashlib
import json
import os
import stat
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence


CONNECTED_REPORT_SCHEMA_VERSION = 2
DEPENDENCY_IDENTITY_SCHEMA_VERSION = 3
CONNECTED_PLAN_SCHEMA_VERSION = 2
PUBLISH_RETRY_DELAYS_SECONDS = (0.2, 0.5)
ISSUE_ROOT = "https://github.com/nsdoanfosan/speedtree-batch-tools/issues"


class RetryPlanInvalid(RuntimeError):
    """Immutable retry evidence no longer matches the current board."""

    def __init__(self, reasons: Sequence[str]):
        self.reasons = tuple(str(reason) for reason in reasons)
        super().__init__("; ".join(self.reasons))


def _json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")


def _digest(value: Any) -> str:
    return hashlib.sha256(_json_bytes(value)).hexdigest()


def _absolute_path(value: str | Path) -> str:
    return str(Path(value).expanduser().absolute())


def _path_key(value: str | Path) -> str:
    return os.path.normcase(_absolute_path(value)).casefold()


def _unique_paths(values: Iterable[str | Path]) -> list[str]:
    paths: dict[str, str] = {}
    for value in values:
        if value is None or not str(value).strip():
            continue
        absolute = _absolute_path(value)
        paths.setdefault(_path_key(absolute), absolute)
    return [paths[key] for key in sorted(paths)]


def generator_unit_record(group: Mapping[str, Any]) -> dict:
    folder = _absolute_path(group["folder"])
    master = str(group["master"]).strip()
    followers = sorted(
        {str(value).strip() for value in group.get("names") or () if str(value).strip()},
        key=str.casefold,
    )
    selector = {
        "folder": folder,
        "master": master,
        "followers": followers,
    }
    return {
        "unit_id": f"generator_sync:{_digest(selector)}",
        "stage": "generator_sync",
        "selector": selector,
    }


def cluster_unit_record(row: Mapping[str, Any]) -> dict:
    blend = _absolute_path(row["blend"])
    targets = sorted(
        _unique_paths(row.get("on_target_spms") or ()),
        key=str.casefold,
    )
    selector = {"blend": blend, "targets": targets}
    dependencies = [blend, *targets]
    for key in (
        "source_spm",
        "canonical_spm",
        "mirror_spm",
        "registry_path",
    ):
        value = row.get(key)
        if value:
            dependencies.append(value)
    registry = Path(blend).with_suffix(".atlas_leaf_targets.json")
    dependencies.append(registry)
    return {
        "unit_id": f"cluster_refresh:{_digest(selector)}",
        "stage": "cluster_refresh",
        "selector": selector,
        "dependency_paths": _unique_paths(dependencies),
    }


def connected_unit_records(groups, cluster_rows) -> list[dict]:
    records = [generator_unit_record(group) for group in groups]
    records.extend(cluster_unit_record(row) for row in cluster_rows)
    unit_ids = [record["unit_id"] for record in records]
    if len(unit_ids) != len(set(unit_ids)):
        raise ValueError("connected run contains duplicate unit identities")
    for plan_index, record in enumerate(records):
        record["plan_index"] = plan_index
        record["plan_schema_version"] = CONNECTED_PLAN_SCHEMA_VERSION
        record["resource_sets"] = _unit_resource_sets(record)
    return records


def _dependency_paths(unit: Mapping[str, Any]) -> list[str]:
    selector = unit.get("selector") or {}
    if unit.get("stage") == "generator_sync":
        folder = Path(selector["folder"])
        values: list[str | Path] = [
            folder / selector["master"],
            folder / "spm_generator_sync.json",
        ]
        values.extend(folder / name for name in selector.get("followers") or ())
        return _unique_paths(values)
    values = list(unit.get("dependency_paths") or ())
    selector = unit.get("selector") or {}
    blend = Path(selector["blend"])
    values.extend(blend.parent.glob("*_auto_capture_manifest.json"))
    values.extend(blend.parent.glob("*.tga"))
    receipt_dir = blend.parent / "reports"
    if receipt_dir.is_dir():
        values.extend(
            receipt_dir.glob("*_cluster_normalization_sync_receipt.json")
        )
    isolated_cache = blend.parent / ".sk_batch_isolated_bark"
    if isolated_cache.is_dir():
        values.extend(isolated_cache.rglob("*.json"))
        values.extend(isolated_cache.rglob("*.spm"))
    for owner in {
        Path(target).parent for target in selector.get("targets") or ()
    }:
        values.extend((
            owner / "speedtree_import_manifest.json",
            owner / "README_SPEEDTREE_IMPORT.md",
        ))
        target_receipts = owner / ".atlas_leaf_speedtree_targets"
        scope_receipts = owner / ".atlas_leaf_speedtree_scopes"
        if target_receipts.is_dir():
            values.extend(target_receipts.glob("*.json"))
        if scope_receipts.is_dir():
            values.extend(scope_receipts.glob("*.json"))
        meshes = owner / "meshes"
        if meshes.is_dir():
            values.extend(meshes.glob("*.fbx"))
    return _unique_paths(values)


def _inventory_specs(unit: Mapping[str, Any]) -> list[dict]:
    if unit.get("stage") != "cluster_refresh":
        return []
    selector = unit.get("selector") or {}
    blend = Path(selector["blend"])
    specs = [
        {
            "path": str(blend.parent),
            "recursive": False,
            "patterns": ["*.tga", "*_auto_capture_manifest.json"],
        },
        {
            "path": str(blend.parent / "reports"),
            "recursive": False,
            "patterns": ["*_cluster_normalization_sync_receipt.json"],
        },
        {
            "path": str(blend.parent / ".sk_batch_isolated_bark"),
            "recursive": True,
            "patterns": ["*.json", "*.spm"],
        },
    ]
    for owner in sorted(
        {Path(target).parent for target in selector.get("targets") or ()},
        key=lambda path: str(path).casefold(),
    ):
        specs.extend((
            {
                "path": str(owner / ".atlas_leaf_speedtree_targets"),
                "recursive": False,
                "patterns": ["*.json"],
            },
            {
                "path": str(owner / ".atlas_leaf_speedtree_scopes"),
                "recursive": False,
                "patterns": ["*.json"],
            },
            {
                "path": str(owner / "meshes"),
                "recursive": False,
                "patterns": ["*.fbx"],
            },
        ))
    return specs


def _unit_resource_sets(unit: Mapping[str, Any]) -> dict:
    """Describe conservative read/write ownership for overlap fail-closed."""

    files = [
        {"kind": "file", "path": path}
        for path in _dependency_paths(unit)
    ]
    trees = [
        {"kind": "tree", "path": _absolute_path(spec["path"])}
        for spec in _inventory_specs(unit)
    ]
    read_set = [*files, *trees]
    if unit.get("stage") == "generator_sync":
        selector = unit.get("selector") or {}
        folder = Path(selector["folder"])
        write_paths = [
            folder / "spm_generator_sync.json",
            *(folder / name for name in selector.get("followers") or ()),
        ]
        write_set = [
            {"kind": "file", "path": path}
            for path in _unique_paths(write_paths)
        ]
    else:
        # A Cluster refresh can rewrite target SPMs, registries, receipts,
        # generated manifests, textures, caches, or mesh artifacts. Treat all
        # discovered dependencies and inventory roots as mutable rather than
        # assuming semantic isolation the transaction does not prove.
        write_set = copy.deepcopy(read_set)

    def unique(resources):
        values = {}
        for resource in resources:
            normalized = {
                "kind": str(resource["kind"]),
                "path": _absolute_path(resource["path"]),
            }
            key = (normalized["kind"], _path_key(normalized["path"]))
            values.setdefault(key, normalized)
        return [values[key] for key in sorted(values)]

    return {"read": unique(read_set), "write": unique(write_set)}


def _resources_intersect(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    left_path = _path_key(left["path"])
    right_path = _path_key(right["path"])
    if left_path == right_path:
        return True
    separator = os.sep.casefold()
    if left.get("kind") == "tree" and right_path.startswith(
        left_path.rstrip(separator) + separator
    ):
        return True
    if right.get("kind") == "tree" and left_path.startswith(
        right_path.rstrip(separator) + separator
    ):
        return True
    return False


def unit_overlap_graph(units: Iterable[Mapping[str, Any]]) -> list[dict]:
    """Return sealed conservative mutation-overlap edges for an ordered plan."""

    units = [copy.deepcopy(dict(unit)) for unit in units]
    edges = []
    for left_index, left in enumerate(units):
        left_sets = left.get("resource_sets") or _unit_resource_sets(left)
        for right in units[left_index + 1:]:
            right_sets = right.get("resource_sets") or _unit_resource_sets(right)
            intersections = []
            comparisons = (
                (left_sets.get("write") or (), right_sets.get("read") or ()),
                (left_sets.get("write") or (), right_sets.get("write") or ()),
                (right_sets.get("write") or (), left_sets.get("read") or ()),
            )
            for left_resources, right_resources in comparisons:
                for left_resource in left_resources:
                    for right_resource in right_resources:
                        if _resources_intersect(left_resource, right_resource):
                            intersections.extend((
                                _absolute_path(left_resource["path"]),
                                _absolute_path(right_resource["path"]),
                            ))
            if intersections:
                edges.append({
                    "left_unit_id": left["unit_id"],
                    "right_unit_id": right["unit_id"],
                    "resources": _unique_paths(intersections),
                })
    return edges


def _settings_dependency_paths(
    unit: Mapping[str, Any],
    settings: Mapping[str, Any],
) -> list[str]:
    repo = Path(__file__).resolve().parent.parent
    paths: list[str | Path] = [
        Path(__file__).resolve(),
        Path(__file__).with_name("spm_generator_sync_gui.pyw"),
    ]
    if unit.get("stage") == "generator_sync":
        paths.extend((
            repo / "spm_generator_sync" / "spm_generator_sync.py",
            repo / "spm_generator_sync" / "process_stream.py",
            settings.get("speedtree_exe"),
            settings.get("xml_ini"),
        ))
    else:
        paths.extend((
            repo / "cluster_blend_sync.py",
            repo / "cluster_source_prepare.py",
            repo / "atlas_target_registry.py",
            repo / "spm_generator_sync" / "jobs" / "cluster_relation_job.py",
            settings.get("blender_exe"),
            settings.get("cluster_unit_probe"),
        ))
    return _unique_paths(path for path in paths if path)


def directory_inventory_identity(spec: Mapping[str, Any]) -> dict:
    absolute = _absolute_path(spec["path"])
    root = Path(absolute)
    patterns = sorted(
        {str(pattern).casefold() for pattern in spec.get("patterns") or ("*",)}
    )
    recursive = bool(spec.get("recursive"))
    record = {
        "path": absolute,
        "kind": "directory_inventory",
        "patterns": patterns,
        "recursive": recursive,
    }
    if not root.exists():
        return {**record, "exists": False, "entries": []}
    if not root.is_dir():
        return {
            **record,
            "exists": True,
            "error_type": "NotADirectory",
            "error": "inventory root is not a directory",
        }
    entries = []
    try:
        if recursive:
            candidates = (
                Path(directory) / name
                for directory, subdirs, files in os.walk(
                    root,
                    followlinks=False,
                )
                for name in sorted((*subdirs, *files), key=str.casefold)
            )
        else:
            candidates = iter(root.iterdir())
        for candidate in candidates:
            relative = candidate.relative_to(root).as_posix()
            if candidate.is_dir() and not candidate.is_symlink():
                if recursive:
                    entries.append({
                        "path": relative,
                        "type": "directory",
                        "symlink": False,
                        "reparse_point": bool(
                            getattr(candidate.stat(), "st_file_attributes", 0)
                            & 0x400
                        ),
                    })
                continue
            if not any(
                fnmatch.fnmatch(candidate.name.casefold(), pattern)
                for pattern in patterns
            ):
                continue
            candidate_stat = candidate.lstat()
            entries.append({
                "path": relative,
                "type": (
                    "symlink" if candidate.is_symlink()
                    else "file" if candidate.is_file()
                    else "other"
                ),
                "symlink": candidate.is_symlink(),
                "reparse_point": bool(
                    getattr(candidate_stat, "st_file_attributes", 0) & 0x400
                ),
            })
    except OSError as exc:
        return {
            **record,
            "exists": True,
            "error_type": type(exc).__name__,
            "error": str(exc),
        }
    return {
        **record,
        "exists": True,
        "entries": sorted(entries, key=lambda entry: entry["path"].casefold()),
    }


def file_content_identity(path: str | Path) -> dict:
    absolute = _absolute_path(path)
    candidate = Path(absolute)
    record = {"path": absolute}
    try:
        link_stat = candidate.lstat()
        record.update({
            "symlink": stat.S_ISLNK(link_stat.st_mode),
            "reparse_point": bool(
                getattr(link_stat, "st_file_attributes", 0) & 0x400
            ),
        })
        if record["symlink"]:
            record["symlink_target"] = os.readlink(candidate)
        path_stat = candidate.stat()
    except FileNotFoundError:
        if record.get("symlink"):
            return {
                **record,
                "exists": True,
                "kind": "broken_symlink",
            }
        return {**record, "exists": False}
    except OSError as exc:
        return {
            **record,
            "exists": None,
            "error_type": type(exc).__name__,
            "error": str(exc),
        }
    if not stat.S_ISREG(path_stat.st_mode):
        return {
            **record,
            "exists": True,
            "kind": (
                "directory" if stat.S_ISDIR(path_stat.st_mode) else "other"
            ),
        }
    hasher = hashlib.sha256()
    verification_hasher = hashlib.sha256()
    bytes_read = 0
    verification_bytes_read = 0
    try:
        with candidate.open("rb") as stream:
            before = os.fstat(stream.fileno())
            while True:
                block = stream.read(1024 * 1024)
                if not block:
                    break
                hasher.update(block)
                bytes_read += len(block)
            middle = os.fstat(stream.fileno())
            stream.seek(0)
            while True:
                block = stream.read(1024 * 1024)
                if not block:
                    break
                verification_hasher.update(block)
                verification_bytes_read += len(block)
            after = os.fstat(stream.fileno())
    except OSError as exc:
        return {
            **record,
            "exists": True,
            "kind": "file",
            "size": path_stat.st_size,
            "error_type": type(exc).__name__,
            "error": str(exc),
        }
    before_state = (before.st_size, before.st_mtime_ns)
    middle_state = (middle.st_size, middle.st_mtime_ns)
    after_state = (after.st_size, after.st_mtime_ns)
    first_digest = hasher.hexdigest()
    second_digest = verification_hasher.hexdigest()
    if (
        before_state != middle_state
        or middle_state != after_state
        or bytes_read != after.st_size
        or verification_bytes_read != after.st_size
        or first_digest != second_digest
    ):
        return {
            **record,
            "exists": True,
            "kind": "file",
            "error_type": "ContentChangedDuringHash",
            "error": "dependency changed while its content identity was read",
            "before_size": before.st_size,
            "before_mtime_ns": before.st_mtime_ns,
            "after_size": after.st_size,
            "after_mtime_ns": after.st_mtime_ns,
            "bytes_read": bytes_read,
            "verification_bytes_read": verification_bytes_read,
        }
    return {
        **record,
        "exists": True,
        "kind": "file",
        "size": after.st_size,
        "sha256": first_digest,
    }


def production_code_identity() -> dict:
    """Seal the repository-owned Python code that can affect a mutation."""

    repo = Path(__file__).resolve().parent.parent
    excluded = {".git", "__pycache__", "reports", "tests"}

    def source_snapshot():
        sources = sorted(
            (
                path for path in repo.rglob("*")
                if path.suffix.casefold() in {".py", ".pyw"}
                and not any(
                    part.casefold() in excluded for part in path.parts
                )
            ),
            key=lambda path: path.relative_to(repo).as_posix().casefold(),
        )
        inventory = []
        for path in sources:
            path_stat = path.lstat()
            inventory.append({
                "path": path.relative_to(repo).as_posix(),
                "size": int(path_stat.st_size),
                "mtime_ns": int(path_stat.st_mtime_ns),
                "symlink": path.is_symlink(),
                "reparse_point": bool(
                    getattr(path_stat, "st_file_attributes", 0) & 0x400
                ),
                "symlink_target": (
                    os.readlink(path) if path.is_symlink() else None
                ),
            })
        return sources, inventory

    try:
        sources, inventory_before = source_snapshot()
    except OSError as exc:
        return {
            "algorithm": "sha256",
            "root": str(repo),
            "source_count": 0,
            "manifest_sha256": None,
            "stable": False,
            "errors": [{
                "error_type": type(exc).__name__,
                "error": str(exc),
            }],
        }
    manifest = []
    errors = []
    for path in sources:
        identity = file_content_identity(path)
        entry = {
            "path": path.relative_to(repo).as_posix(),
            "exists": identity.get("exists"),
            "size": identity.get("size"),
            "sha256": identity.get("sha256"),
            "symlink": identity.get("symlink", False),
            "reparse_point": identity.get("reparse_point", False),
            "symlink_target": identity.get("symlink_target"),
        }
        if identity.get("error_type"):
            entry["error_type"] = identity["error_type"]
            errors.append(copy.deepcopy(entry))
        manifest.append(entry)
    try:
        _sources_after, inventory_after = source_snapshot()
    except OSError as exc:
        inventory_after = None
        errors.append({
            "error_type": type(exc).__name__,
            "error": str(exc),
        })
    inventory_stable = inventory_after == inventory_before
    if not inventory_stable:
        errors.append({
            "error_type": "ProductionCodeInventoryChangedDuringCapture",
            "error": "production source inventory changed while code was sealed",
        })
    return {
        "algorithm": "sha256",
        "root": str(repo),
        "source_count": len(manifest),
        "manifest_sha256": _digest(manifest),
        "inventory_before_sha256": _digest(inventory_before),
        "inventory_after_sha256": (
            _digest(inventory_after) if inventory_after is not None else None
        ),
        "stable": not errors,
        "errors": errors,
    }


def _semantic_production_code_identity(identity: Any) -> Any:
    """Exclude capture-only inventory timestamps from dependency equality."""

    if not isinstance(identity, Mapping):
        return copy.deepcopy(identity)
    payload = copy.deepcopy(dict(identity))
    payload.pop("inventory_before_sha256", None)
    payload.pop("inventory_after_sha256", None)
    return payload


def _semantic_cluster_producer_identity(identity: Any) -> Any:
    """Keep producer code identity while dropping per-probe diagnostics."""

    if not isinstance(identity, Mapping):
        return copy.deepcopy(identity)
    payload = copy.deepcopy(dict(identity))
    payload.pop("report_sha256", None)
    runtime = payload.get("blender_addon_runtime")
    if isinstance(runtime, Mapping):
        runtime = copy.deepcopy(dict(runtime))
        runtime.pop("process_id", None)
        payload["blender_addon_runtime"] = runtime
    addons = []
    for addon in payload.get("addons") or ():
        if not isinstance(addon, Mapping):
            addons.append(copy.deepcopy(addon))
            continue
        semantic_addon = copy.deepcopy(dict(addon))
        semantic_addon.pop("inventory_before_sha256", None)
        semantic_addon.pop("inventory_after_sha256", None)
        addons.append(semantic_addon)
    if "addons" in payload:
        payload["addons"] = addons
    return payload


def probe_cluster_producer_identity(
    blender_exe: str | Path,
    *,
    timeout_seconds: float = 120.0,
    run_process: Callable[..., Any] = subprocess.run,
) -> dict:
    """Ask Blender to seal the exact installed add-on producer code."""

    executable = Path(blender_exe).expanduser().absolute()
    job = Path(__file__).with_name("jobs") / "connected_producer_identity_job.py"
    if not executable.is_file():
        return {
            "schema_version": 1,
            "kind": "connected_cluster_producer_identity",
            "stable": True,
            "provider_available": False,
            "blender_exe": str(executable),
            "reason": "configured Blender executable is absent",
        }
    if not job.is_file():
        return {
            "schema_version": 1,
            "kind": "connected_cluster_producer_identity",
            "stable": False,
            "provider_available": True,
            "blender_exe": str(executable),
            "error": f"producer identity job is missing: {job}",
        }
    with tempfile.TemporaryDirectory(
        prefix="speedtree_connected_producer_identity_"
    ) as temporary:
        report = Path(temporary) / "producer_identity.json"
        command = [
            str(executable),
            "--background",
            "--factory-startup",
            "--python",
            str(job),
            "--",
            "--report",
            str(report),
        ]
        try:
            completed = run_process(
                command,
                capture_output=True,
                text=True,
                timeout=float(timeout_seconds),
                creationflags=(
                    subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
                ),
                check=False,
            )
        except Exception as exc:
            return {
                "schema_version": 1,
                "kind": "connected_cluster_producer_identity",
                "stable": False,
                "provider_available": True,
                "blender_exe": str(executable),
                "error": f"{type(exc).__name__}: {exc}",
            }
        if completed.returncode != 0 or not report.is_file():
            return {
                "schema_version": 1,
                "kind": "connected_cluster_producer_identity",
                "stable": False,
                "provider_available": True,
                "blender_exe": str(executable),
                "returncode": int(completed.returncode),
                "stdout_tail": str(completed.stdout or "")[-2000:],
                "stderr_tail": str(completed.stderr or "")[-2000:],
                "error": "Blender producer identity probe failed",
            }
        try:
            raw = report.read_bytes()
            payload = json.loads(raw.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            return {
                "schema_version": 1,
                "kind": "connected_cluster_producer_identity",
                "stable": False,
                "provider_available": True,
                "blender_exe": str(executable),
                "error": f"producer identity report is invalid: {exc}",
            }
    if not isinstance(payload, dict) or payload.get("kind") != (
        "connected_cluster_producer_identity"
    ):
        return {
            "schema_version": 1,
            "kind": "connected_cluster_producer_identity",
            "stable": False,
            "provider_available": True,
            "blender_exe": str(executable),
            "error": "producer identity report kind is invalid",
        }
    payload = copy.deepcopy(payload)
    modules = {
        str(entry.get("module") or "")
        for entry in payload.get("addons") or ()
        if isinstance(entry, dict)
    }
    expected_modules = {
        "atlas_leaf_mesh_builder",
        "speedtree_cluster_normalizer",
    }
    manifest_digest = str(payload.get("producer_manifest_sha256") or "")
    if modules != expected_modules or len(manifest_digest) != 64:
        payload["stable"] = False
        payload["error"] = "producer identity report is incomplete"
    payload["report_sha256"] = hashlib.sha256(raw).hexdigest()
    payload["blender_exe"] = str(executable)
    return payload


def dependency_identity(
    unit: Mapping[str, Any],
    settings: Optional[Mapping[str, Any]] = None,
    file_identity_cache: Optional[dict[str, dict]] = None,
    *,
    refresh_execution_identity: bool = False,
) -> dict:
    relevant_settings = dict(settings or {})
    production_before = None
    if refresh_execution_identity:
        production_before = production_code_identity()
        relevant_settings["production_code_identity"] = production_before
    if (
        refresh_execution_identity
        and unit.get("stage") == "cluster_refresh"
        and "cluster_producer_identity" in relevant_settings
    ):
        relevant_settings["cluster_producer_identity"] = (
            probe_cluster_producer_identity(
                relevant_settings.get("blender_exe") or "."
            )
        )
    if "production_code_identity" in relevant_settings:
        relevant_settings["production_code_identity"] = (
            _semantic_production_code_identity(
                relevant_settings["production_code_identity"]
            )
        )
    if "cluster_producer_identity" in relevant_settings:
        relevant_settings["cluster_producer_identity"] = (
            _semantic_cluster_producer_identity(
                relevant_settings["cluster_producer_identity"]
            )
        )
    cache = file_identity_cache if file_identity_cache is not None else {}
    inventory_specs = _inventory_specs(unit)
    inventory_before = [
        directory_inventory_identity(spec) for spec in inventory_specs
    ]
    inputs = []
    paths = _unique_paths((
        *_dependency_paths(unit),
        *_settings_dependency_paths(unit, relevant_settings),
    ))
    for path in paths:
        key = f"file:{_path_key(path)}"
        if key not in cache:
            cache[key] = file_content_identity(path)
        inputs.append(copy.deepcopy(cache[key]))
    inventory_after = [
        directory_inventory_identity(spec) for spec in inventory_specs
    ]
    inventories = []
    for before, after in zip(inventory_before, inventory_after):
        if before == after:
            inventories.append({
                **copy.deepcopy(before),
                "capture_verified": True,
                "snapshot_sha256": _digest(before),
            })
        else:
            inventories.append({
                "path": before.get("path") or after.get("path"),
                "kind": "directory_inventory",
                "capture_verified": False,
                "error_type": "InventoryChangedDuringCapture",
                "error": "directory inventory changed while inputs were sealed",
                "before": copy.deepcopy(before),
                "after": copy.deepcopy(after),
            })
    execution_capture_stable = True
    if production_before is not None:
        production_after = production_code_identity()
        execution_capture_stable = (
            production_before.get("stable") is True
            and production_after.get("stable") is True
            and _digest(production_before) == _digest(production_after)
        )
        if not execution_capture_stable:
            relevant_settings["production_code_capture_error"] = {
                "stable": False,
                "error_type": "ProductionCodeChangedDuringExecutionCapture",
                "before": production_before,
                "after": production_after,
            }
    payload = {
        "identity_schema_version": DEPENDENCY_IDENTITY_SCHEMA_VERSION,
        "unit_id": unit["unit_id"],
        "plan_index": unit.get("plan_index"),
        "plan_schema_version": unit.get("plan_schema_version"),
        "stage": unit["stage"],
        "selector": copy.deepcopy(unit.get("selector") or {}),
        "resource_sets": copy.deepcopy(unit.get("resource_sets") or {}),
        "settings": relevant_settings,
        "inputs": inputs,
        "inventories": inventories,
    }
    stable = all(
        not entry.get("error_type")
        for entry in (*inputs, *inventories)
    ) and execution_capture_stable and (
        relevant_settings.get("production_code_identity", {}).get(
            "stable",
            True,
        )
        is True
    ) and (
        unit.get("stage") != "cluster_refresh"
        or
        relevant_settings.get("cluster_producer_identity", {}).get(
            "stable",
            True,
        )
        is True
    )
    return {
        "algorithm": "sha256",
        "stable": stable,
        "digest": _digest(payload),
        **payload,
    }


def scope_dependency_identities(
    units: Iterable[Mapping[str, Any]],
    settings: Optional[Mapping[str, Any]] = None,
) -> dict[str, dict]:
    cache: dict[str, dict] = {}
    return {
        unit["unit_id"]: dependency_identity(
            unit,
            settings,
            file_identity_cache=cache,
        )
        for unit in units
    }


def _identity_rebase_changes(
    expected: Mapping[str, Any],
    current: Mapping[str, Any],
    authorized_writes: Sequence[Mapping[str, Any]],
) -> Optional[list[dict]]:
    """Return changed resources when every change is an authorized write.

    ``None`` means the identity also changed outside the predecessor's sealed
    write ownership and therefore must retain its old fail-closed baseline.
    """

    if expected.get("stable") is not True or current.get("stable") is not True:
        return None
    static_exclusions = {"digest", "inputs", "inventories"}
    expected_static = {
        key: copy.deepcopy(value)
        for key, value in expected.items()
        if key not in static_exclusions
    }
    current_static = {
        key: copy.deepcopy(value)
        for key, value in current.items()
        if key not in static_exclusions
    }
    if expected_static != current_static:
        return None

    def authorized(resource: Mapping[str, Any]) -> bool:
        return any(
            _resources_intersect(resource, write)
            for write in authorized_writes
        )

    def input_rows(identity: Mapping[str, Any]) -> dict[str, dict]:
        return {
            _path_key(entry["path"]): copy.deepcopy(dict(entry))
            for entry in identity.get("inputs") or ()
            if isinstance(entry, Mapping) and entry.get("path")
        }

    def inventory_rows(identity: Mapping[str, Any]) -> dict[tuple, dict]:
        rows = {}
        for entry in identity.get("inventories") or ():
            if not isinstance(entry, Mapping) or not entry.get("path"):
                continue
            key = (
                _path_key(entry["path"]),
                bool(entry.get("recursive")),
                tuple(sorted(str(value) for value in entry.get("patterns") or ())),
            )
            rows[key] = copy.deepcopy(dict(entry))
        return rows

    changed: dict[tuple[str, str], dict] = {}
    expected_inputs = input_rows(expected)
    current_inputs = input_rows(current)
    for key in sorted(set(expected_inputs) | set(current_inputs)):
        before = expected_inputs.get(key)
        after = current_inputs.get(key)
        if before == after:
            continue
        path = (after or before)["path"]
        resource = {"kind": "file", "path": _absolute_path(path)}
        if not authorized(resource):
            return None
        changed[("file", _path_key(path))] = resource

    expected_inventories = inventory_rows(expected)
    current_inventories = inventory_rows(current)
    for key in sorted(set(expected_inventories) | set(current_inventories)):
        before = expected_inventories.get(key)
        after = current_inventories.get(key)
        if before == after:
            continue
        path = (after or before)["path"]
        resource = {"kind": "tree", "path": _absolute_path(path)}
        if not authorized(resource):
            return None
        changed[("tree", _path_key(path))] = resource
    return [changed[key] for key in sorted(changed)]


def rebase_authorized_dependency_identities(
    completed_unit: Mapping[str, Any],
    pending_units: Iterable[Mapping[str, Any]],
    expected_identities: Mapping[str, Mapping[str, Any]],
    settings: Optional[Mapping[str, Any]] = None,
) -> dict[str, dict]:
    """Refresh pending baselines changed only by a successful predecessor."""

    writes = tuple(
        (completed_unit.get("resource_sets") or {}).get("write") or ()
    )
    if not writes:
        return {}
    affected = []
    for unit in pending_units:
        resource_sets = unit.get("resource_sets") or _unit_resource_sets(unit)
        owned = (
            *(resource_sets.get("read") or ()),
            *(resource_sets.get("write") or ()),
        )
        if any(
            _resources_intersect(write, resource)
            for write in writes
            for resource in owned
        ):
            affected.append(unit)
    if not affected:
        return {}

    current_identities = scope_dependency_identities(affected, settings)
    updates = {}
    for unit in affected:
        unit_id = unit["unit_id"]
        expected = expected_identities.get(unit_id)
        if not isinstance(expected, Mapping):
            raise ValueError(f"missing expected dependency identity for {unit_id}")
        current = current_identities[unit_id]
        changes = _identity_rebase_changes(expected, current, writes)
        if not changes:
            continue
        updates[unit_id] = {
            "authorized_by_unit_id": completed_unit["unit_id"],
            "previous_digest": expected.get("digest"),
            "digest": current.get("digest"),
            "changed_resources": changes,
            "identity": current,
        }
    return updates


def connected_settings(
    config: Mapping[str, Any],
    verify_speedtree: bool,
    board_root: Optional[str | Path] = None,
    *,
    include_cluster_producer: bool = False,
) -> dict:
    """Keep mutation-affecting settings in every dependency identity."""

    settings = {
        "dependency_identity_schema_version": (
            DEPENDENCY_IDENTITY_SCHEMA_VERSION
        ),
        "production_code_identity": production_code_identity(),
        "board_root": _absolute_path(board_root or "."),
        "verify_speedtree": bool(verify_speedtree),
        "sk_only": bool(config.get("sk_only", True)),
        "speedtree_exe": _absolute_path(config.get("speedtree_exe") or "."),
        "xml_ini": _absolute_path(config.get("xml_ini") or "."),
        "blender_exe": _absolute_path(config.get("blender_exe") or "."),
        "cluster_unit_probe": _absolute_path(
            config.get("cluster_unit_probe") or "."
        ),
        "cluster_capture_resolution": int(
            config.get("cluster_capture_resolution") or 1024
        ),
    }
    if include_cluster_producer:
        settings["cluster_producer_identity"] = (
            probe_cluster_producer_identity(settings["blender_exe"])
        )
    return settings


def _issue(number: int) -> dict:
    return {"number": int(number), "url": f"{ISSUE_ROOT}/{int(number)}"}


def classify_failure(reason: Any) -> dict:
    text = str(reason or "")
    folded = text.casefold()
    retry_contract = getattr(reason, "connected_retry_contract", None)
    if not isinstance(retry_contract, dict):
        retry_contract = {}
    if any(
        token in folded
        for token in (
            "transaction_rollback_failed",
            "rollback failed",
            "restore failed",
        )
    ):
        return {
            "category": "non_retryable_internal_contract_failure",
            "automatic_retry": False,
            "retryable": False,
            "mapping": {"kind": "rollback_integrity_failure"},
        }
    permission = any(
        token in folded
        for token in (
            "permission denied",
            "access is denied",
            "[winerror 5]",
            "sharing violation",
        )
    )
    json_publish = ".json" in folded
    publish_context = any(
        token in folded
        for token in (
            ".json.tmp",
            " -> ",
            "publish",
            "cluster relationship on apply failed",
        )
    )
    if permission and json_publish and publish_context:
        structured_retry = (
            retry_contract.get("operation_phase")
            in {"atomic_json_publish", "registry_publish"}
            and retry_contract.get("committed") is False
            and retry_contract.get("rollback_succeeded") is True
            and retry_contract.get("temporary_output_isolated") is True
            and int(retry_contract.get("error_code") or 0) in {5, 13, 32}
        )
        return {
            "category": "transient_retryable_publish_lock",
            "automatic_retry": structured_retry,
            "retryable": True,
            "mapping": {
                "kind": "owned_by_current_issue",
                "issue": _issue(101),
            },
        }
    if "xml source spm does not exist" in folded or (
        ".sk_batch_isolated_bark" in folded and "does not exist" in folded
    ):
        return {
            "category": "existing_software_issue_recurrence",
            "automatic_retry": False,
            "retryable": False,
            "mapping": {"kind": "existing_issue", "issue": _issue(69)},
        }
    if any(
        token in folded
        for token in (
            "drifted from recorded target",
            "cannot repair created generator variants",
            "manifest_candidate_live_conflict",
        )
    ):
        return {
            "category": "existing_software_issue_recurrence",
            "automatic_retry": False,
            "retryable": False,
            "mapping": {"kind": "existing_issue", "issue": _issue(58)},
        }
    if "generation pass" in folded or "blocked isolated texture" in folded:
        return {
            "category": "asset_manifest_repair_required",
            "automatic_retry": False,
            "retryable": False,
            "mapping": {
                "kind": "asset_followup",
                "duplicate_issue_required": False,
            },
        }
    if "stale" in folded or "superseded" in folded:
        return {
            "category": "stale_or_superseded_evidence",
            "automatic_retry": False,
            "retryable": False,
            "mapping": {"kind": "fresh_plan_required"},
        }
    return {
        "category": "non_retryable_internal_contract_failure",
        "automatic_retry": False,
        "retryable": False,
        "mapping": {"kind": "unmapped_internal_failure"},
    }


def execute_with_bounded_publish_retry(
    action: Callable[[], Any],
    *,
    capture_identity: Callable[[], Mapping[str, Any]],
    ownership_is_current: Callable[[], bool],
    cancel_exception_type: type[BaseException] | tuple[type[BaseException], ...] = (),
    delays: Sequence[float] = PUBLISH_RETRY_DELAYS_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
    on_attempt_event: Optional[Callable[[Mapping[str, Any]], None]] = None,
    expected_identity: Optional[Mapping[str, Any]] = None,
) -> dict:
    """Run once, retrying only exact-identity, exactly-owned JSON publishes."""

    baseline = (
        copy.deepcopy(dict(expected_identity))
        if expected_identity is not None
        else None
    )
    attempts = []
    maximum_attempts = 1 + len(tuple(delays))
    emit = on_attempt_event or (lambda _event: None)
    if baseline is not None and baseline.get("stable") is False:
        reason = (
            "[connected_retry_dependency_unstable] dependency content "
            "identity could not be read atomically"
        )
        classification = {
            "category": "stale_or_superseded_evidence",
            "automatic_retry": False,
            "retryable": False,
            "mapping": {"kind": "fresh_plan_required"},
        }
        return {
            "ok": False,
            "reason": reason,
            "classification": classification,
            "attempts": [],
            "retry_exhausted": False,
        }
    for attempt_number in range(1, maximum_attempts + 1):
        if not ownership_is_current():
            reason = (
                "[connected_retry_ownership_lost] exact shared-queue lease "
                "is no longer current"
            )
            classification = classify_failure(reason)
            attempts.append({
                "attempt": attempt_number,
                "reason": reason,
                "classification": classification,
            })
            return {
                "ok": False,
                "reason": reason,
                "classification": classification,
                "attempts": attempts,
                "retry_exhausted": False,
            }
        current_before_attempt = copy.deepcopy(dict(capture_identity()))
        if baseline is None:
            baseline = current_before_attempt
        if current_before_attempt.get("stable") is False:
            reason = (
                "[connected_retry_dependency_unstable] dependency content "
                "identity could not be read atomically before attempt"
            )
            classification = {
                "category": "stale_or_superseded_evidence",
                "automatic_retry": False,
                "retryable": False,
                "mapping": {"kind": "fresh_plan_required"},
            }
            return {
                "ok": False,
                "reason": reason,
                "classification": classification,
                "attempts": attempts,
                "retry_exhausted": False,
            }
        if current_before_attempt.get("digest") != baseline.get("digest"):
            reason = (
                "[connected_retry_content_drift] dependency identity changed "
                "before the attempt; fresh plan required"
            )
            classification = {
                "category": "stale_or_superseded_evidence",
                "automatic_retry": False,
                "retryable": False,
                "mapping": {"kind": "fresh_plan_required"},
            }
            return {
                "ok": False,
                "reason": reason,
                "classification": classification,
                "attempts": attempts,
                "retry_exhausted": False,
            }
        if not ownership_is_current():
            reason = (
                "[connected_retry_ownership_lost] exact shared-queue lease "
                "was lost during dependency capture"
            )
            classification = classify_failure(reason)
            attempts.append({
                "attempt": attempt_number,
                "reason": reason,
                "classification": classification,
            })
            return {
                "ok": False,
                "reason": reason,
                "classification": classification,
                "attempts": attempts,
                "retry_exhausted": False,
            }
        emit({
            "event": "attempt_started",
            "attempt": attempt_number,
            "pre_attempt_dependency_identity": current_before_attempt,
        })
        if not ownership_is_current():
            reason = (
                "[connected_retry_ownership_lost] exact shared-queue lease "
                "was lost after the durable attempt boundary"
            )
            classification = classify_failure(reason)
            attempts.append({
                "attempt": attempt_number,
                "reason": reason,
                "classification": classification,
            })
            return {
                "ok": False,
                "reason": reason,
                "classification": classification,
                "attempts": attempts,
                "retry_exhausted": False,
            }
        try:
            result = action()
        except cancel_exception_type:
            raise
        except Exception as exc:  # unit isolation intentionally captures all
            reason = str(exc)
            classification = classify_failure(exc)
            attempt = {
                "attempt": attempt_number,
                "reason": reason,
                "classification": classification,
            }
            attempts.append(attempt)
            emit({
                "event": "attempt_failed",
                **copy.deepcopy(attempt),
            })
            can_retry = (
                classification.get("automatic_retry") is True
                and attempt_number < maximum_attempts
            )
            if not can_retry:
                return {
                    "ok": False,
                    "exception": exc,
                    "reason": reason,
                    "classification": classification,
                    "attempts": attempts,
                    "retry_exhausted": bool(
                        classification.get("automatic_retry")
                        and attempt_number >= maximum_attempts
                    ),
                }
            current = dict(capture_identity())
            if (
                current.get("stable") is False
                or current.get("digest") != baseline.get("digest")
            ):
                drift_reason = (
                    "[connected_retry_content_drift] dependency identity "
                    "changed after the publish failure; fresh plan required"
                )
                drift_classification = {
                    "category": "stale_or_superseded_evidence",
                    "automatic_retry": False,
                    "retryable": False,
                    "mapping": {"kind": "fresh_plan_required"},
                }
                attempts.append({
                    "attempt": attempt_number,
                    "reason": drift_reason,
                    "classification": drift_classification,
                })
                return {
                    "ok": False,
                    "reason": drift_reason,
                    "classification": drift_classification,
                    "attempts": attempts,
                    "retry_exhausted": False,
                }
            delay = float(delays[attempt_number - 1])
            attempt["next_delay_seconds"] = delay
            sleep(delay)
            continue
        return {
            "ok": True,
            "result": result,
            "attempts": attempts,
            "attempt_count": attempt_number,
        }
    raise AssertionError("bounded retry loop did not return")


def new_unit_results(units: Iterable[Mapping[str, Any]], identities) -> list[dict]:
    return [
        {
            **copy.deepcopy(dict(unit)),
            "outcome": "pending",
            "planned_dependency_identity": copy.deepcopy(
                identities[unit["unit_id"]]
            ),
        }
        for unit in units
    ]


def summarize_unit_results(unit_results: Iterable[Mapping[str, Any]]) -> dict:
    summary = {
        "generator": {"succeeded": 0, "failed": 0, "pending": 0, "total": 0},
        "cluster": {"succeeded": 0, "failed": 0, "pending": 0, "total": 0},
        "failures": 0,
    }
    for entry in unit_results:
        bucket = "generator" if entry.get("stage") == "generator_sync" else "cluster"
        summary[bucket]["total"] += 1
        outcome = str(entry.get("outcome") or "pending")
        if outcome == "succeeded":
            summary[bucket]["succeeded"] += 1
        elif outcome == "failed":
            summary[bucket]["failed"] += 1
            summary["failures"] += 1
        else:
            summary[bucket]["pending"] += 1
    return summary


def status_from_unit_results(unit_results, *, cancelled: bool = False) -> str:
    summary = summarize_unit_results(unit_results)
    failed = summary["failures"]
    pending = summary["generator"]["pending"] + summary["cluster"]["pending"]
    if cancelled:
        return "cancelled"
    # A connected run is one pipeline, not a bag of independent jobs.  Once
    # one unit fails, reporting the earlier mutations as a successful
    # "partial" terminal outcome hides a broken handoff.  Historical partial
    # reports remain readable, but newly sealed runs fail as a whole.
    if failed:
        return "failed"
    if pending:
        return "incomplete"
    return "ok"


def update_unit_result(
    unit_results: list[dict],
    unit_id: str,
    *,
    outcome: str,
    result: Optional[Mapping[str, Any]] = None,
    failure: Optional[Mapping[str, Any]] = None,
) -> dict:
    matches = [entry for entry in unit_results if entry.get("unit_id") == unit_id]
    if len(matches) != 1:
        raise ValueError(f"expected one unit result for {unit_id}, found {len(matches)}")
    entry = matches[0]
    entry["outcome"] = str(outcome)
    entry.pop("result", None)
    entry.pop("failure", None)
    if result is not None:
        entry["result"] = copy.deepcopy(dict(result))
    if failure is not None:
        entry["failure"] = copy.deepcopy(dict(failure))
    return entry


def apply_final_identities(unit_results: list[dict], identities) -> None:
    for entry in unit_results:
        identity = identities.get(entry.get("unit_id"))
        if identity is not None:
            entry["dependency_identity"] = copy.deepcopy(identity)


def selected_failed_units(report_payload: Mapping[str, Any]) -> list[dict]:
    units = [
        copy.deepcopy(dict(entry))
        for entry in report_payload.get("unit_results") or ()
        if entry.get("outcome") == "failed"
    ]
    seen = set()
    for unit in units:
        unit_id = str(unit.get("unit_id") or "")
        if not unit_id or unit_id in seen:
            raise RetryPlanInvalid(["retry report has missing or duplicate unit IDs"])
        seen.add(unit_id)
    return units


def validate_failed_retry_plan(
    report_payload: Mapping[str, Any],
    current_units: Iterable[Mapping[str, Any]],
    settings: Optional[Mapping[str, Any]] = None,
) -> dict:
    if int(report_payload.get("schema_version") or 0) < CONNECTED_REPORT_SCHEMA_VERSION:
        raise RetryPlanInvalid(["legacy report has no immutable unit identities"])
    report_units = list(report_payload.get("unit_results") or ())
    if not report_units:
        raise RetryPlanInvalid(["report has no unit-level retry evidence"])
    current_units = [copy.deepcopy(dict(unit)) for unit in current_units]
    current_by_id = {unit["unit_id"]: unit for unit in current_units}
    report_by_id = {str(unit.get("unit_id") or ""): unit for unit in report_units}
    reasons = []
    report_order = [str(unit.get("unit_id") or "") for unit in report_units]
    current_order = [unit["unit_id"] for unit in current_units]
    if report_order != current_order:
        reasons.append("current ordered unit plan differs from the report")
    if len(current_by_id) != len(current_units):
        reasons.append("current board contains duplicate unit identities")
    if len(report_by_id) != len(report_units) or "" in report_by_id:
        reasons.append("report contains missing or duplicate unit identities")
    missing = sorted(set(report_by_id) - set(current_by_id))
    added = sorted(set(current_by_id) - set(report_by_id))
    if missing:
        reasons.append(f"{len(missing)} report unit(s) are no longer in the board")
    if added:
        reasons.append(f"{len(added)} current board unit(s) were not in the report")
    current_identities = scope_dependency_identities(current_units, settings)
    for unit_id in sorted(set(report_by_id) & set(current_by_id)):
        report_plan = {
            key: copy.deepcopy(report_by_id[unit_id].get(key))
            for key in (
                "plan_index",
                "plan_schema_version",
                "stage",
                "selector",
                "resource_sets",
            )
        }
        current_plan = {
            key: copy.deepcopy(current_by_id[unit_id].get(key))
            for key in report_plan
        }
        if report_plan != current_plan:
            reasons.append(f"{unit_id} sealed plan/resource ownership changed")
        expected = report_by_id[unit_id].get("dependency_identity")
        if not isinstance(expected, dict) or not expected.get("digest"):
            reasons.append(f"{unit_id} has no final dependency identity")
            continue
        if int(expected.get("identity_schema_version") or 0) != (
            DEPENDENCY_IDENTITY_SCHEMA_VERSION
        ):
            reasons.append(f"{unit_id} dependency identity schema changed")
            continue
        current = current_identities[unit_id]
        if expected.get("stable") is not True:
            reasons.append(f"{unit_id} report dependency identity is unstable")
            continue
        if current.get("stable") is not True:
            reasons.append(f"{unit_id} current dependency identity is unstable")
            continue
        if current.get("digest") != expected.get("digest"):
            reasons.append(f"{unit_id} dependency identity changed")
    failed = selected_failed_units(report_payload)
    if not failed:
        reasons.append("report has no failed units to retry")
    if reasons:
        raise RetryPlanInvalid(reasons)
    overlap_edges = unit_overlap_graph(current_units)
    succeeded_ids = {
        str(entry.get("unit_id") or "")
        for entry in report_units
        if entry.get("outcome") == "succeeded"
    }
    failed_ids = {entry["unit_id"] for entry in failed}
    ineligible = {}
    for edge in overlap_edges:
        left = edge["left_unit_id"]
        right = edge["right_unit_id"]
        if left in failed_ids and right in succeeded_ids:
            ineligible.setdefault(left, []).append(copy.deepcopy(edge))
        if right in failed_ids and left in succeeded_ids:
            ineligible.setdefault(right, []).append(copy.deepcopy(edge))
    return {
        "units": [
            current_by_id[entry["unit_id"]]
            for entry in failed
            if entry["unit_id"] not in ineligible
        ],
        "ineligible_overlaps": ineligible,
        "overlap_graph": overlap_edges,
        "source_unit_results": copy.deepcopy(report_units),
        "current_identities": current_identities,
    }


def validate_preserved_unit_identities(
    current_units: Iterable[Mapping[str, Any]],
    expected_order: Sequence[str],
    protected_identities: Mapping[str, Mapping[str, Any]],
    settings: Optional[Mapping[str, Any]] = None,
    capture_unit_ids: Iterable[str] = (),
) -> dict[str, dict]:
    """Fail closed when one retry mutates an already-successful unit.

    Cluster relations can share registries, manifests, meshes, or generated
    artifacts. A successful retry is therefore not an isolation proof by
    itself. Rescan the ordered board plan and compare every protected success
    against its last sealed dependency identity before another failed unit may
    run.
    """

    units = [copy.deepcopy(dict(unit)) for unit in current_units]
    current_order = [str(unit.get("unit_id") or "") for unit in units]
    reasons = []
    if current_order != [str(unit_id) for unit_id in expected_order]:
        reasons.append("ordered unit plan changed during failed-unit retry")
    current_by_id = {unit["unit_id"]: unit for unit in units}
    if len(current_by_id) != len(units) or "" in current_order:
        reasons.append("current retry plan contains missing or duplicate unit IDs")

    identity_unit_ids = set(protected_identities)
    identity_unit_ids.update(str(unit_id) for unit_id in capture_unit_ids)
    identities = scope_dependency_identities(
        (
            unit for unit in units
            if unit["unit_id"] in identity_unit_ids
        ),
        settings,
    )
    for unit_id, expected in protected_identities.items():
        if unit_id not in current_by_id:
            reasons.append(f"protected successful unit disappeared: {unit_id}")
            continue
        current = identities[unit_id]
        if not isinstance(expected, Mapping) or expected.get("stable") is not True:
            reasons.append(f"protected successful unit has unstable evidence: {unit_id}")
        elif current.get("stable") is not True:
            reasons.append(f"protected successful unit became unstable: {unit_id}")
        elif current.get("digest") != expected.get("digest"):
            reasons.append(f"protected successful unit changed: {unit_id}")
    if reasons:
        raise RetryPlanInvalid(reasons)
    return identities


def report_file_identity(path: str | Path) -> dict:
    identity = file_content_identity(path)
    if identity.get("exists") is not True or not identity.get("sha256"):
        raise OSError(f"connected report is not a readable file: {path}")
    return {
        "path": identity["path"],
        "sha256": identity["sha256"],
        "size": identity["size"],
    }


def load_exact_report(path: str | Path, expected_identity=None) -> tuple[dict, dict]:
    absolute = _absolute_path(path)
    try:
        raw = Path(absolute).read_bytes()
    except OSError as exc:
        raise RetryPlanInvalid([f"source report is unreadable: {exc}"]) from exc
    identity = {
        "path": absolute,
        "sha256": hashlib.sha256(raw).hexdigest(),
        "size": len(raw),
    }
    if expected_identity is not None:
        for key in ("path", "sha256", "size"):
            if identity.get(key) != expected_identity.get(key):
                raise RetryPlanInvalid([f"source report {key} changed"])
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RetryPlanInvalid([f"source report is unreadable: {exc}"]) from exc
    if not isinstance(payload, dict):
        raise RetryPlanInvalid(["source report root is not an object"])
    return payload, identity


def legacy_or_current_summary(payload: Mapping[str, Any]) -> dict:
    unit_results = payload.get("unit_results")
    if isinstance(unit_results, list):
        return summarize_unit_results(unit_results)
    existing = payload.get("summary")
    if isinstance(existing, dict):
        return copy.deepcopy(existing)
    scope = payload.get("scope") or {}
    generator_ok = len(payload.get("generator_sync") or ())
    cluster_ok = len(payload.get("cluster_refresh") or ())
    generator_failed = sum(
        failure.get("stage") == "generator_sync"
        for failure in payload.get("failures") or ()
    )
    cluster_failed = sum(
        failure.get("stage") == "cluster_refresh"
        for failure in payload.get("failures") or ()
    )
    return {
        "generator": {
            "succeeded": generator_ok,
            "failed": generator_failed,
            "pending": 0,
            "total": int(scope.get("generator_group_count") or generator_ok + generator_failed),
        },
        "cluster": {
            "succeeded": cluster_ok,
            "failed": cluster_failed,
            "pending": 0,
            "total": int(scope.get("cluster_relation_count") or cluster_ok + cluster_failed),
        },
        "failures": len(payload.get("failures") or ()),
    }


def shared_queue_result(
    payload: Optional[Mapping[str, Any]],
    local_job_id: Any,
    *,
    error: Optional[str] = None,
    termination_state: Optional[str] = None,
) -> dict:
    payload = dict(payload or {})
    status = str(payload.get("status") or ("failed" if error else "ok")).casefold()
    outcome = "completed" if status in {"ok", "completed"} else status
    result = {
        "schema_version": 2,
        "tool": "spm_generator_sync",
        "local_job_id": local_job_id,
        "outcome": outcome,
        "run_status": status,
        "run_id": payload.get("run_id"),
        "board_root": payload.get("root"),
        "report_schema_version": payload.get("schema_version"),
        "counts": legacy_or_current_summary(payload),
    }
    if payload.get("unit_results") is not None:
        result["unit_results_sha256"] = _digest(payload["unit_results"])
    report_identity = payload.get("report_identity")
    if isinstance(report_identity, dict):
        result["report"] = copy.deepcopy(report_identity)
    elif payload.get("report_path"):
        try:
            result["report"] = report_file_identity(payload["report_path"])
        except OSError as exc:
            result["report_error"] = str(exc)
    if payload.get("retry_of"):
        result["retry_of"] = copy.deepcopy(payload["retry_of"])
    if termination_state:
        result["termination_state"] = str(termination_state)
    if error:
        result["error"] = str(error)
    return result


def validate_queue_anchored_report(job, payload, report_identity) -> dict:
    """Prove a retry report is sealed by its exact terminal queue record."""

    reasons = []
    result = job.get("result") if isinstance(job, dict) else None
    if not isinstance(result, dict):
        raise RetryPlanInvalid(["queue job has no structured terminal result"])
    if job.get("status") != "failed":
        reasons.append("queue job is not a failed terminal record")
    if result.get("tool") != "spm_generator_sync":
        reasons.append("queue result belongs to another tool")
    if result.get("outcome") not in {"partial", "failed"}:
        reasons.append("queue result is not failed-unit retry eligible")
    if result.get("run_status") not in {"partial", "failed"}:
        reasons.append("queue run status is not retry eligible")
    if result.get("report") != report_identity:
        reasons.append("queue report identity does not match exact bytes")
    if result.get("run_id") != payload.get("run_id"):
        reasons.append("queue/report run ID mismatch")
    if int(result.get("report_schema_version") or 0) != int(
        payload.get("schema_version") or 0
    ):
        reasons.append("queue/report schema mismatch")
    try:
        if _path_key(result.get("board_root") or ".") != _path_key(
            payload.get("root") or "."
        ):
            reasons.append("queue/report board root mismatch")
    except (OSError, ValueError):
        reasons.append("queue/report board root is invalid")
    if result.get("counts") != legacy_or_current_summary(payload):
        reasons.append("queue/report counts mismatch")
    unit_results = payload.get("unit_results")
    if unit_results is None or result.get("unit_results_sha256") != _digest(
        unit_results
    ):
        reasons.append("queue/report unit result digest mismatch")
    queue_identity = payload.get("queue_identity") or {}
    if queue_identity.get("mode") != "shared":
        reasons.append("report was not produced under a shared queue lease")
    if str(queue_identity.get("job_id") or "") != str(job.get("id") or ""):
        reasons.append("report queue job identity mismatch")
    if queue_identity.get("sequence") != job.get("sequence"):
        reasons.append("report queue sequence mismatch")
    last_lease = job.get("last_lease") or {}
    if not queue_identity.get("owner_id") or (
        str(queue_identity.get("owner_id"))
        != str(last_lease.get("owner_id") or "")
    ):
        reasons.append("report queue lease owner mismatch")
    if reasons:
        raise RetryPlanInvalid(reasons)
    return {
        "queue_job_id": str(job["id"]),
        "queue_sequence": job.get("sequence"),
        "run_id": payload.get("run_id"),
        "board_root": payload.get("root"),
        "report": copy.deepcopy(report_identity),
    }


__all__ = [
    "CONNECTED_REPORT_SCHEMA_VERSION",
    "CONNECTED_PLAN_SCHEMA_VERSION",
    "PUBLISH_RETRY_DELAYS_SECONDS",
    "RetryPlanInvalid",
    "apply_final_identities",
    "classify_failure",
    "cluster_unit_record",
    "connected_settings",
    "connected_unit_records",
    "dependency_identity",
    "execute_with_bounded_publish_retry",
    "generator_unit_record",
    "legacy_or_current_summary",
    "load_exact_report",
    "new_unit_results",
    "probe_cluster_producer_identity",
    "report_file_identity",
    "rebase_authorized_dependency_identities",
    "scope_dependency_identities",
    "selected_failed_units",
    "shared_queue_result",
    "status_from_unit_results",
    "summarize_unit_results",
    "update_unit_result",
    "unit_overlap_graph",
    "validate_failed_retry_plan",
    "validate_preserved_unit_identities",
    "validate_queue_anchored_report",
]
