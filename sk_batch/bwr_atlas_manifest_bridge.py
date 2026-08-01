"""Bind the external BWR add-on to the shared Atlas manifest resolver."""
from __future__ import annotations

import os
from pathlib import Path

from atlas_manifest_resolver import (
    resolution_evidence,
    resolve_atlas_manifests,
)


def install_bwr_atlas_manifest_resolver(bwr_core, target_spm):
    """Replace BWR's rolling-global lookup for one exact exported target.

    The installed BWR add-on predates the shared resolver.  Its legacy lookup
    reads ``speedtree_import_manifest.json`` even when that rolling file names
    another SPM.  A headless batch process is short-lived, so installing this
    exact-target adapter before import is both isolated and deterministic.
    """
    target = Path(target_spm).expanduser().resolve(strict=False)
    resolution = resolve_atlas_manifests(target)
    resolver_selected_paths = []
    selected_paths = []
    seen = set()
    for row in resolution.get("selected") or []:
        path = Path(row.get("path") or "").resolve(strict=False)
        key = os.path.normcase(str(path)).casefold()
        if key not in seen:
            seen.add(key)
            resolver_selected_paths.append(path)
            # BWR can prove a scoped identity only from the consumer-suffixed
            # record.  Exact per-target/global records remain authoritative to
            # the strict preflight envelope, but must not be reinterpreted by
            # the add-on's older scope-only overlay reader.
            if row.get("kind") == "exact_target_scope":
                selected_paths.append(path)

    original = getattr(bwr_core, "_speedtree_manifest_paths", None)
    if not callable(original):
        raise RuntimeError(
            "Installed speedtree_bone_weight_repair add-on does not expose "
            "the Atlas manifest lookup required by the shared resolver bridge"
        )
    target_stem = target.stem.casefold()

    def exact_target_manifest_paths(source_fbx_path, stmat_material=None):
        if Path(str(source_fbx_path or "")).stem.casefold() == target_stem:
            return list(selected_paths)
        return original(source_fbx_path, stmat_material)

    bwr_core._speedtree_manifest_paths = exact_target_manifest_paths
    evidence = resolution_evidence(resolution)
    evidence["consumer"] = "speedtree_bone_weight_repair"
    evidence["adapter"] = "shared_exact_target_manifest_paths"
    evidence["resolver_selected_manifest_paths"] = [
        str(path) for path in resolver_selected_paths
    ]
    evidence["selected_manifest_paths"] = [
        str(path) for path in selected_paths
    ]
    return evidence
