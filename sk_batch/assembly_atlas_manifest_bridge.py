"""Bind the installed Assembly add-on to the Atlas manifest resolver."""
from __future__ import annotations

import os
from pathlib import Path

from atlas_manifest_resolver import (
    resolution_evidence,
    resolve_atlas_manifests,
)


def install_assembly_atlas_manifest_resolver(addon_runtime, target_spm):
    """Replace the add-on's rolling-global lookup for one exported target.

    The installed add-on predates the shared resolver. Its legacy lookup
    reads ``speedtree_import_manifest.json`` even when that rolling file names
    another SPM.  A headless batch process is short-lived, so installing this
    exact-target adapter before import is both isolated and deterministic.
    """
    target = Path(target_spm).expanduser().resolve(strict=False)
    # The add-on consumes paths read-only. Provider overlap or stale metadata
    # veto the normal SpeedTree export; the diagnostic resolver preserves only
    # deterministic/disjoint selected claims and keeps mutation unauthorized.
    resolution = resolve_atlas_manifests(target, diagnostic_only=True)
    resolver_selected_paths = []
    selected_paths = []
    seen = set()
    for row in resolution.get("selected") or []:
        path = Path(row.get("path") or "").resolve(strict=False)
        key = os.path.normcase(str(path)).casefold()
        if key not in seen:
            seen.add(key)
            resolver_selected_paths.append(path)
            # The add-on proves a scoped identity only from consumer-suffixed
            # record.  Exact per-target/global records remain authoritative to
            # the strict preflight envelope, but must not be reinterpreted by
            # the add-on's older scope-only overlay reader.
            if (
                resolution.get("mutation_authorized") is not False
                and row.get("kind") == "exact_target_scope"
                and row.get("reason")
                != "diagnostic_disjoint_provider_claims"
            ):
                selected_paths.append(path)

    original = addon_runtime.operation(
        "speedtree_bone_weight_repair",
        "speedtree_manifest_paths",
    )
    if not callable(original):
        raise RuntimeError(
            "Installed SpeedTree Assembly add-on does not expose "
            "the Atlas manifest lookup required by the shared resolver bridge"
        )
    target_stem = target.stem.casefold()

    def exact_target_manifest_paths(source_fbx_path, stmat_material=None):
        if Path(str(source_fbx_path or "")).stem.casefold() == target_stem:
            return list(selected_paths)
        return original(source_fbx_path, stmat_material)

    addon_runtime.replace_operation(
        "speedtree_bone_weight_repair",
        "speedtree_manifest_paths",
        exact_target_manifest_paths,
    )
    evidence = resolution_evidence(resolution)
    evidence["consumer"] = "speedtree_assembly"
    evidence["adapter"] = "shared_exact_target_manifest_paths"
    evidence["resolver_selected_manifest_paths"] = [
        str(path) for path in resolver_selected_paths
    ]
    evidence["selected_manifest_paths"] = [
        str(path) for path in selected_paths
    ]
    evidence["projected_manifest_paths_withheld"] = [
        str(Path(row.get("path") or "").resolve(strict=False))
        for row in resolution.get("selected") or []
        if row.get("reason") == "diagnostic_disjoint_provider_claims"
    ]
    return evidence
