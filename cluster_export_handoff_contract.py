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


def _object_identity(obj):
    as_pointer = getattr(obj, "as_pointer", None)
    if callable(as_pointer):
        return ("blender_pointer", int(as_pointer()))
    return ("python_id", id(obj))


def _normalized_source_path(value):
    if not value:
        return ""
    return os.path.normcase(
        os.path.normpath(os.path.abspath(os.path.expanduser(str(value))))
    )


def capture_cluster_export_snapshot(blender_data, cluster_source_stem):
    """Capture persisted Export ownership before BWR mutates the scene.

    Object pointers distinguish objects authored in the loaded blend from
    objects created by the current FBX import.  The unsuffixed-name snapshot is
    retained separately so an owned object removed and recreated by BWR cannot
    make a persisted ambiguous root look transient.
    """

    export_collection = blender_data.collections.get("Export")
    objects = list(export_collection.objects) if export_collection else []
    expected_name = str(cluster_source_stem or "").casefold()
    return {
        "object_identities": frozenset(
            _object_identity(obj) for obj in objects
        ),
        "unsuffixed_empty_names": tuple(
            sorted(
                obj.name
                for obj in objects
                if obj.type == "EMPTY"
                and obj.name.casefold() == expected_name
            )
        ),
    }


def _matches_exact_source_ownership(
    obj,
    *,
    source_fbx_path,
    source_identity_path,
):
    expected_fbx = _normalized_source_path(source_fbx_path)
    expected_identity = _normalized_source_path(source_identity_path)
    return bool(expected_fbx and expected_identity) and (
        _normalized_source_path(obj.get("codex_source_fbx", ""))
        == expected_fbx
        and _normalized_source_path(obj.get("codex_source_identity", ""))
        == expected_identity
    )


def reconcile_transient_cluster_export_root(
    blender_data,
    source_collection,
    *,
    cluster_source_stem,
    source_fbx_path,
    source_identity_path,
    before_snapshot,
):
    """Move one proven current-run BWR Export hierarchy out of Send2UE.

    A standalone Cluster blend may already contain normalized ordinal pivots.
    BWR additionally builds a Full-SK reference hierarchy for the current FBX;
    that hierarchy belongs in ``SpeedTree_Source``, not alongside the persisted
    pivots in ``Export``.  Reconciliation is fail-closed unless the unsuffixed
    root and every connected Export member are new and carry both the exact
    current FBX ownership tag and stable source-SPM identity.
    """

    export_collection = blender_data.collections.get("Export")
    base_report = {
        "status": "not_needed",
        "cluster_source_stem": str(cluster_source_stem or ""),
        "source_fbx": str(source_fbx_path or ""),
        "source_identity": str(source_identity_path or ""),
        "moved_export_objects": [],
        "issues": [],
    }
    if export_collection is None:
        base_report["reason"] = "missing_export_collection"
        return base_report

    expected_name = str(cluster_source_stem or "").casefold()
    objects = list(export_collection.objects)
    roots = [
        obj
        for obj in objects
        if obj.type == "EMPTY"
        and obj.children
        and obj.name.casefold() == expected_name
    ]
    persisted_names = tuple(
        (before_snapshot or {}).get("unsuffixed_empty_names") or ()
    )
    if persisted_names:
        base_report.update(
            {
                "status": "blocked",
                "reason": "persisted_unsuffixed_export_root",
                "issues": [
                    f"cluster_unsuffixed_export_unit:{name}"
                    for name in persisted_names
                ],
            }
        )
        return base_report
    if not roots:
        base_report["reason"] = "no_unsuffixed_export_root"
        return base_report
    if len(roots) != 1:
        base_report.update(
            {
                "status": "blocked",
                "reason": "multiple_unsuffixed_export_roots",
                "issues": [
                    f"cluster_unsuffixed_export_unit:{obj.name}"
                    for obj in roots
                ],
            }
        )
        return base_report

    root = roots[0]
    before_identities = frozenset(
        (before_snapshot or {}).get("object_identities") or ()
    )
    if _object_identity(root) in before_identities:
        base_report.update(
            {
                "status": "blocked",
                "reason": "persisted_unsuffixed_export_root",
                "issues": [
                    f"cluster_unsuffixed_export_unit:{root.name}"
                ],
            }
        )
        return base_report

    object_set = set(objects)
    connected = set()
    pending = [root]
    while pending:
        obj = pending.pop()
        if obj in connected or obj not in object_set:
            continue
        connected.add(obj)
        parent = getattr(obj, "parent", None)
        if parent in object_set:
            pending.append(parent)
        pending.extend(
            child
            for child in getattr(obj, "children", ())
            if child in object_set
        )

    ambiguous = [
        obj
        for obj in connected
        if _object_identity(obj) in before_identities
        or not _matches_exact_source_ownership(
            obj,
            source_fbx_path=source_fbx_path,
            source_identity_path=source_identity_path,
        )
    ]
    if ambiguous or source_collection is None:
        base_report.update(
            {
                "status": "blocked",
                "reason": (
                    "missing_source_collection"
                    if source_collection is None
                    else "ambiguous_unsuffixed_export_hierarchy"
                ),
                "ambiguous_objects": sorted(obj.name for obj in ambiguous),
                "issues": [
                    f"cluster_unsuffixed_export_unit:{root.name}"
                ],
            }
        )
        return base_report

    moved = sorted(connected, key=lambda obj: obj.name.casefold())
    for obj in moved:
        if obj not in source_collection.objects:
            source_collection.objects.link(obj)
        export_collection.objects.unlink(obj)
    base_report.update(
        {
            "status": "reconciled",
            "reason": "new_exact_bwr_source_hierarchy",
            "moved_export_objects": [obj.name for obj in moved],
        }
    )
    return base_report


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
    export_postcondition=None,
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
        if (
            export_postcondition is not None
            and result.get("repair_push_export_postcondition")
            != export_postcondition
        ):
            result["repair_push_export_postcondition"] = copy.deepcopy(
                export_postcondition
            )
            return result, True
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
    if export_postcondition is not None:
        result["repair_push_export_postcondition"] = copy.deepcopy(
            export_postcondition
        )
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
    "capture_cluster_export_snapshot",
    "cluster_export_contract_issues",
    "finalize_cluster_pipeline_payload",
    "reconcile_transient_cluster_export_root",
    "validate_pending_source_contract",
]
