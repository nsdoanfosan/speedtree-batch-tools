"""Shared Cluster source-build and final Export handoff contract.

The BWR source-build stage intentionally runs before the Cluster Normalizer.
At that point the Send2UE ``Export`` collection is an output that may not exist
yet.  This module keeps that temporary state explicit and provides the same
structural inspection to both stages, so a pending raw source can never be
mistaken for an Unreal-ready blend.
"""

from __future__ import annotations

import copy
import json
import os
import re
import tempfile
from pathlib import Path


PENDING_HANDOFF_STATUS = "cluster_export_pending"
SOURCE_BUILD_MODE = "raw_source_for_cluster_normalizer"
FINAL_HANDOFF_STATUSES = frozenset({"ok", "source_review"})


def cluster_export_contract_issues(blender_data, cluster_source_stem):
    """Return structural Send2UE Export issues for one Cluster source.

    ``blender_data`` is passed explicitly so the contract can be tested without
    importing Blender and so every caller evaluates the exact same rules.
    """

    export_collection = blender_data.collections.get("Export")
    if export_collection is None:
        return ["missing_export_collection"]
    issues = [
        f"orphan_owned_export_empty:{obj.name}"
        for obj in export_collection.objects
        if obj.type == "EMPTY"
        and not obj.children
        and bool(obj.get("codex_source_fbx", ""))
    ]
    pivots = [
        obj
        for obj in export_collection.objects
        if obj.type == "EMPTY"
        and obj.children
        and bool(obj.get("speedtree_cluster_generated"))
        and obj.get("speedtree_cluster_asset_role") == "send2ue_pivot"
    ]
    issues.extend(
        f"cluster_unsuffixed_export_unit:{obj.name}"
        for obj in export_collection.objects
        if obj.type == "EMPTY" and obj.children and obj not in pivots
    )
    pattern = re.compile(
        rf"^{re.escape(str(cluster_source_stem))}_(\d{{2}})$",
        re.IGNORECASE,
    )
    ordinals = []
    for pivot in pivots:
        match = pattern.fullmatch(pivot.name)
        if match is None:
            issues.append(f"cluster_invalid_export_pivot:{pivot.name}")
        else:
            ordinals.append(int(match.group(1)))
    if not pivots:
        issues.append("cluster_missing_normalized_export_pivot")
    elif sorted(ordinals) != list(range(1, len(pivots) + 1)):
        issues.append(
            "cluster_nonconsecutive_export_pivots:"
            + ",".join(str(value) for value in sorted(ordinals))
        )
    return issues


def validate_pending_source_contract(payload, *, expected_source_object=None):
    """Validate the only report state that a Normalizer may promote."""

    handoff = payload.get("handoff_preflight") or {}
    if handoff.get("status") != PENDING_HANDOFF_STATUS:
        raise ValueError("Cluster source report is not Export-pending")
    if handoff.get("unreal_push_ready") is not False:
        raise ValueError("Export-pending Cluster source is marked Unreal-ready")

    source_build = payload.get("cluster_source_build_contract") or {}
    deferred_issues = list(source_build.get("deferred_export_issues") or ())
    merged_name = str((payload.get("paths") or {}).get("merged_name") or "")
    expected = str(expected_source_object or merged_name)
    if (
        source_build.get("status") != "ready"
        or source_build.get("mode") != SOURCE_BUILD_MODE
        or source_build.get("final_export_required") is not True
        or not deferred_issues
        or source_build.get("source_blend_committed") is not True
        or not expected
        or str(source_build.get("source_object") or "") != expected
        or (merged_name and merged_name != expected)
    ):
        raise ValueError("Cluster source-build pending contract is incomplete")
    return source_build


def finalize_cluster_pipeline_payload(
    payload,
    *,
    export_issues,
    expected_source_object=None,
):
    """Promote a valid pending report only after final Export verification.

    Existing final reports are returned unchanged for backward compatibility.
    Explicitly blocked or unknown states remain fail-closed.
    """

    issues = list(export_issues or ())
    if issues:
        raise ValueError(
            "Cluster Normalizer final Export contract failed: "
            + ", ".join(issues)
        )

    result = copy.deepcopy(payload)
    handoff = result.get("handoff_preflight") or {}
    status = str(handoff.get("status") or "")
    if status in FINAL_HANDOFF_STATUSES or not status:
        return result, False
    if status != PENDING_HANDOFF_STATUS:
        raise ValueError(
            f"Cluster source handoff cannot be finalized from status={status}"
        )

    source_build = validate_pending_source_contract(
        result,
        expected_source_object=expected_source_object,
    )
    final_status = str(
        source_build.get("post_normalization_handoff_status") or "ok"
    )
    if final_status not in FINAL_HANDOFF_STATUSES:
        raise ValueError(
            "Cluster source-build post-normalization handoff status is "
            f"invalid: {final_status}"
        )

    handoff["status"] = final_status
    handoff["export_collection_issues"] = []
    handoff["source_review_required"] = final_status == "source_review"
    handoff["unreal_push_ready"] = final_status == "ok"
    result["handoff_preflight"] = handoff
    result["export_collection_issues"] = []
    result["source_review_required"] = final_status == "source_review"
    result["unreal_push_ready"] = final_status == "ok"
    source_build["status"] = "normalized"
    source_build["deferred_export_issues"] = []
    source_build["final_export_required"] = False
    source_build["final_export_verified"] = True
    result["cluster_source_build_contract"] = source_build
    return result, True


def atomic_write_json(path, payload):
    """Replace a JSON report atomically in its existing directory."""

    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            delete=False,
            dir=str(destination.parent),
            prefix=f".{destination.name}.",
            suffix=".tmp",
        ) as handle:
            temporary = Path(handle.name)
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(temporary, destination)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


__all__ = [
    "FINAL_HANDOFF_STATUSES",
    "PENDING_HANDOFF_STATUS",
    "SOURCE_BUILD_MODE",
    "atomic_write_json",
    "cluster_export_contract_issues",
    "finalize_cluster_pipeline_payload",
    "validate_pending_source_contract",
]
