"""Dependency discovery and stable ordering for SK Batch Unreal Push.

Tree assembly manifests already record the exact normalized Cluster ``.blend``
files used by Blender Repair.  This module turns those records into batch
dependencies without guessing asset names, branch counts, or export suffixes.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from cluster_assembly_builder import (
    MANIFEST_KIND,
    validate_file_fingerprint,
    validate_manifest_artifacts,
)
from cluster_blend_sync import discover_cluster_blend_relations


class PushDependencyError(RuntimeError):
    """The saved Blender Repair dependency contract cannot be scheduled."""


def normalized_path_key(path):
    try:
        value = Path(path).resolve()
    except (OSError, ValueError):
        value = Path(path).absolute()
    return os.path.normcase(str(value)).casefold()


def is_cluster_source_spm(spm):
    path = Path(spm)
    return (
        path.suffix.casefold() == ".spm"
        and path.parent.name.casefold() == "cluster"
    )


def repair_pipeline_report_path(spm):
    spm = Path(spm)
    return (
        spm.parent
        / "reports"
        / f"{spm.stem}_speedtree_repair_pipeline_report_codex.json"
    )


def load_current_cluster_assembly_manifest(spm):
    """Return the current content-driven contract, including pass-through."""
    spm = Path(spm)
    if is_cluster_source_spm(spm):
        return None
    report_path = repair_pipeline_report_path(spm)
    if not report_path.is_file():
        # The ordinary Push preflight owns the missing-Repair-report message.
        return None
    try:
        pipeline = json.loads(report_path.read_text(encoding="utf-8"))
        embedded = pipeline.get("cluster_assembly_manifest")
        if not isinstance(embedded, dict):
            return None
        if embedded.get("status") == "pass_through":
            return {
                "kind": MANIFEST_KIND,
                "status": "pass_through",
                "parts": [],
            }
        manifest_record = embedded.get("manifest") or {}
        manifest_path = Path(str(manifest_record.get("path") or ""))
        if not manifest_path.is_file():
            raise PushDependencyError(
                "BWR Cluster Assembly manifest file is missing: "
                + str(manifest_path)
            )
        validate_file_fingerprint(
            manifest_record, "BWR Cluster Assembly manifest"
        )
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["manifest"] = manifest_record
        if manifest.get("kind") != MANIFEST_KIND:
            raise PushDependencyError(
                "unsupported BWR Cluster Assembly manifest kind"
            )
        validate_manifest_artifacts(manifest)
        return manifest
    except PushDependencyError:
        raise
    except (OSError, ValueError, RuntimeError, TypeError) as exc:
        raise PushDependencyError(
            f"Cluster Assembly dependency contract is not current: {exc}"
        ) from exc


def _manifest_dependency_spms(root_spm):
    """Resolve exact Cluster SPM inputs from rendered-part source blends."""
    manifest = load_current_cluster_assembly_manifest(root_spm)
    if manifest is None:
        return None
    if manifest.get("status") == "pass_through":
        return []

    dependencies = []
    seen = set()
    for part in manifest.get("parts") or []:
        if not isinstance(part, dict):
            continue
        external = part.get("external_source")
        if not isinstance(external, dict):
            continue
        source_blend = external.get("source_blend")
        if not isinstance(source_blend, dict) or not source_blend.get("path"):
            raise PushDependencyError(
                "external Cluster part has no source_blend contract: "
                + str(part.get("prototype_id") or part.get("asset_name") or "?")
            )
        dependency = Path(str(source_blend["path"])).with_suffix(".spm")
        if not is_cluster_source_spm(dependency):
            raise PushDependencyError(
                "Cluster dependency source is outside a Cluster folder: "
                + str(dependency)
            )
        if not dependency.is_file():
            raise PushDependencyError(
                "Cluster dependency SPM is missing: " + str(dependency)
            )
        key = normalized_path_key(dependency)
        if key not in seen:
            seen.add(key)
            dependencies.append(dependency)
    return dependencies


def _relation_dependency_spms(root_spm, relation_cache=None):
    """Resolve explicit ON relations before an Assembly receipt exists."""
    root = Path(root_spm).expanduser().absolute()
    root_key = normalized_path_key(root)
    owner_key = normalized_path_key(root.parent)
    if relation_cache is not None and owner_key in relation_cache:
        relations = relation_cache[owner_key]
    else:
        try:
            relations = discover_cluster_blend_relations(root.parent)
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            raise PushDependencyError(
                f"Cluster relation dependency contract is unreadable: {exc}"
            ) from exc
        if relation_cache is not None:
            relation_cache[owner_key] = relations

    dependencies = []
    seen = set()
    for relation in relations:
        registry_error = str(relation.get("registry_error") or "").strip()
        if registry_error:
            raise PushDependencyError(
                "Cluster relation target registry is invalid: "
                + registry_error
            )
        matches_root = any(
            target.get("relation_on") is True
            and target.get("owner_target") is True
            and normalized_path_key(target.get("target_spm")) == root_key
            for target in relation.get("targets") or []
            if target.get("target_spm")
        )
        if not matches_root:
            continue
        dependency = Path(relation.get("source_spm") or "")
        if not is_cluster_source_spm(dependency):
            raise PushDependencyError(
                "Cluster relation source is outside a Cluster folder: "
                + str(dependency)
            )
        if not dependency.is_file():
            raise PushDependencyError(
                "Cluster relation source SPM is missing: " + str(dependency)
            )
        key = normalized_path_key(dependency)
        if key not in seen:
            seen.add(key)
            dependencies.append(dependency)
    return dependencies


def cluster_dependency_spms(root_spm, relation_cache=None):
    """Prefer current content receipts; use relations only before first receipt."""
    manifest_error = None
    try:
        manifest_dependencies = _manifest_dependency_spms(root_spm)
    except PushDependencyError as exc:
        manifest_error = exc
        manifest_dependencies = None
    if manifest_dependencies is not None:
        return manifest_dependencies
    relation_dependencies = _relation_dependency_spms(
        root_spm,
        relation_cache=relation_cache,
    )
    if manifest_error is not None and not relation_dependencies:
        raise manifest_error

    dependencies = []
    seen = set()
    for dependency in relation_dependencies:
        key = normalized_path_key(dependency)
        if key in seen:
            continue
        seen.add(key)
        dependencies.append(dependency)
    return dependencies


def _item_path_lookup(all_items):
    lookup = {}
    values = all_items.values() if isinstance(all_items, dict) else all_items
    for item in values:
        if not isinstance(item, dict):
            continue
        for field in ("spm", "authoring_spm", "output_spm"):
            value = item.get(field)
            if value:
                lookup.setdefault(normalized_path_key(value), item)
    return lookup


def expand_push_targets(selected_targets, all_items):
    """Add exact Cluster dependencies and return them before downstream roots.

    Returns ``(ordered_targets, dependencies_by_root, auto_added_ids)``.
    Explicitly selected Cluster rows are preserved and duplicate dependencies
    shared by multiple Trees are scheduled only once.
    """
    selected_targets = list(selected_targets)
    lookup = _item_path_lookup(all_items)
    dependency_items = []
    explicit_cluster_items = []
    downstream_items = []
    dependencies_by_root = {}
    auto_added_ids = set()
    relation_cache = {}
    selected_ids = {
        normalized_path_key(item["spm"])
        for item in selected_targets
    }

    for item in selected_targets:
        spm = Path(item["spm"])
        if is_cluster_source_spm(spm):
            explicit_cluster_items.append(item)
            continue
        downstream_items.append(item)
        dependency_ids = []
        for dependency_spm in cluster_dependency_spms(
            spm,
            relation_cache=relation_cache,
        ):
            dependency = lookup.get(normalized_path_key(dependency_spm))
            if dependency is None:
                raise PushDependencyError(
                    "Cluster dependency exists but is not present in the current "
                    "SK Batch scan; rescan the asset root: "
                    + str(dependency_spm)
                )
            dependency_iid = str(dependency["spm"])
            dependency_ids.append(dependency_iid)
            dependency_items.append(dependency)
            if normalized_path_key(dependency["spm"]) not in selected_ids:
                auto_added_ids.add(dependency_iid)
        dependencies_by_root[str(spm)] = tuple(dict.fromkeys(dependency_ids))

    ordered = []
    seen = set()
    for item in dependency_items + explicit_cluster_items + downstream_items:
        key = normalized_path_key(item["spm"])
        if key in seen:
            continue
        seen.add(key)
        ordered.append(item)
    return ordered, dependencies_by_root, auto_added_ids
