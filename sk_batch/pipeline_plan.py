"""Read-only planning for the headless SK Batch command line.

This module deliberately does not import the Tk GUI, shared queue runtime, or
any job launcher.  A plan is compiled only from current production files and
already-persisted exact Repair/Assembly contracts.  Missing or stale contracts
remain blocked evidence; planning never refreshes or repairs them.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


TOOL_DIR = Path(__file__).resolve().parent
if str(TOOL_DIR) not in sys.path:
    # Existing SK Batch modules are also loaded as flat modules by Blender and
    # the GUI.  Keep that established import contract without importing either
    # runtime surface here.
    sys.path.insert(0, str(TOOL_DIR))

from push_dependency_schedule import (  # noqa: E402
    PushDependencyError,
    exact_dependency_contract_from_validated_manifest,
    expand_push_targets,
    is_cluster_source_spm,
    load_current_cluster_assembly_manifest,
)
from sk_common import scan_cluster_spm_sources, scan_sk_spms  # noqa: E402


PLAN_KIND = "sk_batch_pipeline_plan"
PLAN_SCHEMA_VERSION = 1
PLAN_PHASES = ("spm", "blender", "push")


class PipelinePlanInputError(ValueError):
    """The requested root, target, phase, or index is invalid."""


def _path_key(value):
    try:
        path = Path(value).expanduser().resolve()
    except (OSError, RuntimeError, ValueError):
        path = Path(value).expanduser().absolute()
    return os.path.normcase(str(path)).casefold()


def _inventory_item(spm, **values):
    spm = Path(spm).expanduser().absolute()
    return {
        "spm": spm,
        "authoring_spm": Path(
            values.pop("authoring_spm", spm)
        ).expanduser().absolute(),
        "output_spm": Path(
            values.pop("output_spm", spm)
        ).expanduser().absolute(),
        "kind": (
            "cluster"
            if is_cluster_source_spm(spm)
            else "tree"
        ),
        **values,
    }


def scan_pipeline_inventory(root):
    """Return deterministic production rows without writing state or caches."""
    root = Path(root).expanduser().resolve()
    if not root.is_dir():
        raise PipelinePlanInputError(
            f"production root is not a directory: {root}"
        )

    by_path = {}
    for spm in scan_sk_spms(root):
        item = _inventory_item(spm)
        by_path[_path_key(spm)] = item
    for row in scan_cluster_spm_sources(root):
        spm = Path(
            row.get("authoring_spm")
            or row.get("output_spm")
            or row.get("source_spm")
            or ""
        )
        if not str(spm):
            continue
        item = _inventory_item(
            spm,
            authoring_spm=row.get("authoring_spm") or spm,
            output_spm=row.get("output_spm") or spm,
            cluster_pair_status=str(row.get("pair_status") or ""),
            referenced_by_spms=tuple(
                str(value)
                for value in row.get("referenced_by_spms") or ()
            ),
        )
        by_path[_path_key(spm)] = item

    inventory = sorted(
        by_path.values(),
        key=lambda item: (
            0 if item["kind"] == "cluster" else 1,
            _path_key(item["spm"]),
        ),
    )
    for index, item in enumerate(inventory):
        item["index"] = index
    return inventory


def _resolve_requested_targets(root, inventory, targets=(), indexes=()):
    lookup = {
        _path_key(item["spm"]): item
        for item in inventory
    }
    selected = []
    seen = set()

    for value in targets or ():
        candidate = Path(value).expanduser()
        if not candidate.is_absolute():
            candidate = Path(root) / candidate
        key = _path_key(candidate)
        item = lookup.get(key)
        if item is None:
            raise PipelinePlanInputError(
                f"target is not in the production scan: {candidate}"
            )
        if key not in seen:
            seen.add(key)
            selected.append(item)

    for value in indexes or ():
        try:
            index = int(value)
        except (TypeError, ValueError) as exc:
            raise PipelinePlanInputError(
                f"target index is not an integer: {value}"
            ) from exc
        if index < 0 or index >= len(inventory):
            raise PipelinePlanInputError(
                f"target index is outside the production scan: {index}"
            )
        item = inventory[index]
        key = _path_key(item["spm"])
        if key not in seen:
            seen.add(key)
            selected.append(item)

    if not targets and not indexes:
        selected = list(inventory)
    if not selected:
        raise PipelinePlanInputError(
            f"production scan contains no requested SPM targets: {root}"
        )
    return selected


def _blocked_row(item, code, message):
    return {
        "spm": str(Path(item["spm"]).resolve()),
        "phase": "dependency_resolution",
        "code": code,
        "message": str(message),
    }


def _cluster_pair_blocker(item):
    """Return why a Cluster pair cannot be consumed without normalization."""
    if not is_cluster_source_spm(item["spm"]):
        return None
    status = str(item.get("cluster_pair_status") or "").strip()
    if status != "current" or not Path(item["spm"]).is_file():
        return (
            "canonical Cluster SPM pair is not current "
            f"(status={status or 'unknown'}): {item['spm']}"
        )
    return None


def _exact_dependency_plan(selected, inventory):
    """Expand only current persisted Assembly contracts, one root at a time."""
    inventory_by_path = {
        str(item["spm"]): item
        for item in inventory
    }
    ordered_candidates = []
    dependencies_by_root = {}
    auto_added = set()
    blocked = []
    ready_tree_keys = set()

    for item in selected:
        spm = Path(item["spm"])
        if is_cluster_source_spm(spm):
            pair_blocker = _cluster_pair_blocker(item)
            if pair_blocker:
                blocked.append(_blocked_row(
                    item,
                    "exact_cluster_pair_not_current",
                    pair_blocker,
                ))
                continue
            ordered_candidates.append(item)
            continue
        try:
            manifest = load_current_cluster_assembly_manifest(spm)
            if manifest is None:
                blocked.append(_blocked_row(
                    item,
                    "exact_cluster_dependency_contract_missing",
                    (
                        "current Blender Repair report has no exact Cluster "
                        "Assembly dependency contract"
                    ),
                ))
                continue
            stage_contract = (
                exact_dependency_contract_from_validated_manifest(
                    spm,
                    manifest,
                )
            )
            ordered, dependencies, added = expand_push_targets(
                [item],
                inventory_by_path,
                stage_dependency_contracts={
                    str(spm): stage_contract,
                },
            )
            pair_blockers = [
                _cluster_pair_blocker(candidate)
                for candidate in ordered
                if is_cluster_source_spm(candidate["spm"])
            ]
            pair_blockers = [
                message for message in pair_blockers if message
            ]
            if pair_blockers:
                blocked.append(_blocked_row(
                    item,
                    "exact_cluster_pair_not_current",
                    "; ".join(pair_blockers),
                ))
                continue
        except PushDependencyError as exc:
            blocked.append(_blocked_row(
                item,
                "exact_cluster_dependency_contract_stale",
                exc,
            ))
            continue
        ordered_candidates.extend(ordered)
        dependencies_by_root.update(dependencies)
        auto_added.update(added)
        ready_tree_keys.add(_path_key(spm))

    seen = set()
    ordered = []
    for item in sorted(
        ordered_candidates,
        key=lambda row: (
            0 if is_cluster_source_spm(row["spm"]) else 1,
            _path_key(row["spm"]),
        ),
    ):
        key = _path_key(item["spm"])
        if key in seen:
            continue
        if not is_cluster_source_spm(item["spm"]) and (
            key not in ready_tree_keys
        ):
            continue
        seen.add(key)
        ordered.append(item)
    return ordered, dependencies_by_root, auto_added, blocked


def _serialized_item(item, requested_keys, auto_added):
    spm = Path(item["spm"]).resolve()
    cluster = is_cluster_source_spm(spm)
    return {
        "index": int(item["index"]),
        "spm": str(spm),
        "kind": "cluster" if cluster else "tree",
        "requested": _path_key(spm) in requested_keys,
        "auto_added_dependency": str(spm) in auto_added,
        "bone_setting": (
            "required_cluster_contract"
            if cluster
            else "skipped_non_cluster"
        ),
    }


def _stage_rows(phase, ordered_items):
    rows = []
    cluster_targets = [
        str(Path(item["spm"]).resolve())
        for item in ordered_items
        if is_cluster_source_spm(item["spm"])
    ]
    tree_targets = [
        str(Path(item["spm"]).resolve())
        for item in ordered_items
        if not is_cluster_source_spm(item["spm"])
    ]
    if cluster_targets:
        rows.append({
            "phase": phase,
            "wave": "cluster",
            "targets": cluster_targets,
            "bone_setting_targets": (
                cluster_targets if phase == "spm" else []
            ),
        })
    if tree_targets and phase != "spm":
        rows.append({
            "phase": phase,
            "wave": "tree",
            "targets": tree_targets,
            "bone_setting_targets": [],
        })
    return rows


def build_pipeline_plan(
    root,
    *,
    phase,
    targets=(),
    indexes=(),
):
    """Compile one deterministic plan without executing or repairing anything."""
    if phase not in PLAN_PHASES:
        raise PipelinePlanInputError(f"unknown pipeline phase: {phase}")
    root = Path(root).expanduser().resolve()
    inventory = scan_pipeline_inventory(root)
    selected = _resolve_requested_targets(
        root,
        inventory,
        targets=targets,
        indexes=indexes,
    )
    requested_keys = {
        _path_key(item["spm"])
        for item in selected
    }

    if phase == "spm":
        ordered = sorted(
            selected,
            key=lambda item: (
                0 if is_cluster_source_spm(item["spm"]) else 1,
                _path_key(item["spm"]),
            ),
        )
        dependencies = {}
        auto_added = set()
        blocked = []
    else:
        (
            ordered,
            dependencies,
            auto_added,
            blocked,
        ) = _exact_dependency_plan(selected, inventory)

    phases = PLAN_PHASES[: PLAN_PHASES.index(phase) + 1]
    stages = []
    for stage_phase in phases:
        stages.extend(_stage_rows(stage_phase, ordered))

    return {
        "kind": PLAN_KIND,
        "schema_version": PLAN_SCHEMA_VERSION,
        "mode": "plan_only",
        "status": "blocked" if blocked else "ready",
        "root": str(root),
        "terminal_phase": phase,
        "inventory": [
            _serialized_item(item, requested_keys, auto_added)
            for item in inventory
        ],
        "ordered_targets": [
            str(Path(item["spm"]).resolve())
            for item in ordered
        ],
        "dependencies_by_root": {
            str(Path(root_spm).resolve()): [
                str(Path(dependency).resolve())
                for dependency in dependencies_for_root
            ]
            for root_spm, dependencies_for_root
            in sorted(
                dependencies.items(),
                key=lambda row: _path_key(row[0]),
            )
        },
        "auto_added_dependencies": sorted(
            (str(Path(value).resolve()) for value in auto_added),
            key=str.casefold,
        ),
        "stages": stages,
        "blocked": blocked,
    }


__all__ = [
    "PLAN_KIND",
    "PLAN_PHASES",
    "PLAN_SCHEMA_VERSION",
    "PipelinePlanInputError",
    "build_pipeline_plan",
    "scan_pipeline_inventory",
]
