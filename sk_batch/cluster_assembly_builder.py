"""Content-driven Blender -> UE 5.8 Skeletal Nanite Assembly builder.

This module is a downstream stage of the existing SK Batch/BWR pipeline.  It is
not an Assembly switch or a standalone tool: a reconciled
``cluster_assembly_handoff`` decides whether the stage builds, passes through,
or fails closed.

The Blender half derives a base mesh and rigid part prototypes from the final
BWR armature/merged mesh.  The Unreal half consumes those generated assets with
``NaniteAssemblySkeletalMeshBuilder`` while preserving the existing Full
Skeletal Mesh.  Both outputs are checked against the same final-skeleton and
newly-generated wind JSON contract; production DynamicWind data is never
copied.

``bpy`` and ``unreal`` are intentionally lazy dependencies so all contract and
hierarchy checks remain unit-testable with ordinary Python.
"""
from __future__ import annotations

import hashlib
import itertools
import json
import math
import os
import re
import statistics
import subprocess
import sys
from contextlib import contextmanager
from copy import deepcopy
from collections import Counter, defaultdict
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent.parent
if str(REPO_DIR) not in sys.path:
    sys.path.insert(0, str(REPO_DIR))

from process_lifecycle import owned_run
from artifact_content_key import (
    artifact_record_content_key,
    file_content_key_snapshot,
)

from nanite_assembly_materials import (
    NaniteAssemblyMaterialError,
    normalize_unreal_nanite_assembly_materials,
)
from spm_authored_placement import (
    AUTHORED_NODE_GLOBAL_ASSIGNMENT_TOLERANCE_METERS,
    SpmAuthoredPlacementError,
    assign_authored_nodes_to_components,
    parse_spm_authored_placement,
)


SCHEMA_VERSION = 1
MANIFEST_KIND = "sk_batch_cluster_nanite_assembly_inputs"
PLACEMENT_CONTRACT_VERSION = 1
MAX_ASSEMBLY_RESIDUAL_RELATIVE_RMS = 1.0e-2
MAX_ASSEMBLY_PIVOT_ERROR_METERS = 1.0e-8
MAX_NORMALIZED_PLAN_FACE_LOSS_RATIO = 0.20
MAX_SPEEDTREE_RENDER_FACE_MULTIPLICITY = 2
MAX_WEIGHT_BINDING_DISTANCE_SPAN_RATIO = 1.0e-2
MIN_WEIGHT_BINDING_DISTANCE_METERS = 1.0e-2
# Blender stores imported FBX coordinates as 32-bit floats.  At the meter-scale
# positions used by this pipeline, one exporter round-trip can separate an
# authored coincident vertex by roughly 1e-7 m.  Every topology/correspondence
# stage must share the same floor; deriving a smaller tolerance from one tiny
# card after the role-level weld makes the same vertex coincident in one stage
# and ambiguous in the next.
MIN_FBX_COORDINATE_TOLERANCE_METERS = 1.0e-6
PASS_THROUGH_PROVENANCE_SCHEMA_VERSION = 1
PASS_THROUGH_PROVENANCE_REASON = (
    "selected_target_contract_handoff_pass_through"
)
ROLE_ORDER = ("branch", "cluster", "leaf", "leaf_side")
MATERIAL_PIPELINE_SIDECAR_SHA_PATTERN = re.compile(
    r"(?P<prefix>\bsidecar_sha256\s*=\s*)"
    r"(?P<quote>['\"])(?P<sha256>[0-9a-fA-F]{64})(?P=quote)"
)
MATERIAL_PIPELINE_EXPECTED_MESH_PATTERN = re.compile(
    r"(?P<prefix>\bexpected_mesh_name\s*=\s*)"
    r"(?P<quote>['\"])(?P<mesh_name>[^'\"]+)(?P=quote)"
)


class ClusterAssemblyBuildError(RuntimeError):
    """Raised when a content or hierarchy invariant is not proven."""


def _coalesce_normalized_external_parts(parts):
    """Collapse topology buckets that resolve to one normalized SK asset.

    SpeedTree may emit the same mesh slot with several clipped topology
    signatures.  Those signatures need separate correspondence fits, but they
    are instances of one authored normalized skeletal asset and therefore must
    become one native Nanite Assembly part.
    """
    grouped = {}
    passthrough = []
    for source_part in parts or []:
        part = deepcopy(source_part)
        external = part.get("external_source") or {}
        if external.get("kind") != "send_to_unreal_normalized_skeletal_part":
            passthrough.append(part)
            continue
        role = str(part.get("role") or "").casefold()
        provider_key = str(part.get("provider_key") or role).casefold()
        asset_name = str(part.get("asset_name") or "")
        relative_folder = str(
            external.get("unreal_relative_folder") or ""
        ).replace("\\", "/").strip("/")
        if role not in ROLE_ORDER or not asset_name or not relative_folder:
            raise ClusterAssemblyBuildError(
                "normalized external part cannot be logically grouped"
            )
        key = (
            role,
            provider_key,
            relative_folder.casefold(),
            asset_name.casefold(),
        )
        incoming_bindings = list(part.get("bindings") or [])
        accumulator = grouped.get(key)
        if accumulator is None:
            accumulator = part
            accumulator["source_topology_signatures"] = []
            accumulator["bindings"] = []
            grouped[key] = accumulator
        elif (
            str(accumulator.get("role_identity") or "")
            != str(part.get("role_identity") or "")
            or str(
                (accumulator.get("external_source") or {}).get(
                    "pivot_contract"
                )
                or ""
            )
            != str(external.get("pivot_contract") or "")
        ):
            raise ClusterAssemblyBuildError(
                "one normalized SK asset has inconsistent Assembly contracts: "
                + asset_name
            )
        signature = str(part.get("topology_signature") or "")
        if (
            signature
            and signature
            not in accumulator["source_topology_signatures"]
        ):
            accumulator["source_topology_signatures"].append(signature)
        accumulator["bindings"].extend(incoming_bindings)

    collapsed = []
    for (
        role,
        _provider_key,
        relative_folder,
        asset_name,
    ), part in grouped.items():
        external = part["external_source"]
        ordinals = sorted({
            int(value)
            for value in (
                list(external.get("card_ordinals") or [])
                + [int(external.get("ordinal") or 0)]
            )
            if int(value) > 0
        })
        logical_subpart = int(part.get("logical_subpart_index") or 0)
        if logical_subpart <= 0 and len(ordinals) == 1:
            logical_subpart = ordinals[0]
        part["logical_group_index"] = ROLE_ORDER.index(role)
        part["logical_subpart_index"] = logical_subpart
        part["topology_signature"] = "normalized_external_asset"
        part["prototype_id"] = (
            f"{role}_normalized_{logical_subpart:02d}_"
            + hashlib.sha1(
                f"{relative_folder}/{asset_name}".encode("utf-8")
            ).hexdigest()[:16]
        )
        external["card_ordinals"] = ordinals
        if len(ordinals) != 1:
            external["ordinal"] = 0
        for instance_index, binding in enumerate(part["bindings"]):
            binding["instance"] = instance_index
        fit_rows = part["bindings"]
        if fit_rows:
            part["fit_summary"] = _assembly_fit_summary(
                fit_rows,
                "uniform_similarity_3d_normalized_asset",
            )
        collapsed.append(part)

    collapsed.extend(passthrough)
    collapsed.sort(
        key=lambda part: (
            int(
                part.get(
                    "logical_group_index",
                    ROLE_ORDER.index(str(part.get("role") or "").casefold()),
                )
            ),
            int(
                part.get("logical_subpart_index")
                or (part.get("external_source") or {}).get("ordinal")
                or 0
            ),
            str(part.get("asset_name") or "").casefold(),
        )
    )
    return collapsed


def _public_base_name(full_stem):
    return f"{full_stem}_NA_Base"


def _public_part_name(full_stem, role, role_index):
    """Return a tree-specific Assembly prototype name.

    ``SK_branch_elm_01`` and ``SK_leaf_elm_01`` are authoritative raw 3D
    cluster/data exports.  Reusing those names for the tiny repeated Assembly
    prototypes lets a later raw-cluster import silently replace a 14/15-vertex
    prototype with the complete cluster mesh.  Keep the Assembly FBX and
    Unreal asset names in a separate, tree-specific namespace instead.
    """
    stem = str(full_stem or "").strip()
    normalized_role = str(role or "").strip().casefold()
    if not stem:
        raise ClusterAssemblyBuildError("Assembly Full asset stem is missing")
    if normalized_role not in ROLE_ORDER:
        raise ClusterAssemblyBuildError(
            f"Assembly part role is invalid: {normalized_role!r}"
        )
    return f"{stem}_NA_{normalized_role.title()}_{int(role_index):02d}"


def _validate_public_export_names(manifest):
    """Keep topology hashes internal and prove every public FBX/asset name."""
    full_stem = str((manifest or {}).get("full_asset_stem") or "")
    if not full_stem:
        raise ClusterAssemblyBuildError("Assembly Full asset stem is missing")
    base = (manifest or {}).get("base") or {}
    expected_base = _public_base_name(full_stem)
    if (
        str(base.get("asset_name") or "") != expected_base
        or str(base.get("export_stem") or "") != expected_base
    ):
        raise ClusterAssemblyBuildError(
            f"Assembly base public name contract changed: expected {expected_base}"
        )
    base_path = str((base.get("fbx") or {}).get("path") or "")
    if Path(base_path).stem != expected_base:
        raise ClusterAssemblyBuildError(
            "Assembly base FBX does not use its public export stem"
        )

    role_counts = Counter()
    seen_public_names = {expected_base.casefold(): False}
    for part in (manifest or {}).get("parts") or []:
        role = str(part.get("role") or "").casefold()
        role_counts[role] += 1
        external = part.get("external_source") or {}
        if external:
            expected_part = str(part.get("asset_name") or "")
            if (
                not expected_part
                or str(part.get("export_stem") or "") != expected_part
                or external.get("kind")
                != "send_to_unreal_normalized_skeletal_part"
            ):
                raise ClusterAssemblyBuildError(
                    "normalized external Assembly part name/source contract is invalid: "
                    f"role={role!r}, asset_name={expected_part!r}, "
                    f"export_stem={str(part.get('export_stem') or '')!r}, "
                    f"external_kind={str(external.get('kind') or '')!r}"
                )
        else:
            expected_part = _public_part_name(
                full_stem,
                role,
                role_counts[role],
            )
            if (
                str(part.get("asset_name") or "") != expected_part
                or str(part.get("export_stem") or "") != expected_part
            ):
                raise ClusterAssemblyBuildError(
                    f"Assembly part public name contract changed: expected {expected_part}"
                )
            part_path = str((part.get("fbx") or {}).get("path") or "")
            if Path(part_path).stem != expected_part:
                raise ClusterAssemblyBuildError(
                    "Assembly part FBX does not use its public export stem"
                )
        folded = expected_part.casefold()
        if folded in seen_public_names:
            if not external or not seen_public_names[folded]:
                raise ClusterAssemblyBuildError(
                    f"duplicate Assembly public asset name: {expected_part}"
                )
        else:
            seen_public_names[folded] = bool(external)
    return {
        "full_asset_stem": full_stem,
        "base": expected_base,
        "parts": role_counts,
    }


def _canonical_json(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _sha256_bytes(value):
    return hashlib.sha256(value).hexdigest()


def file_fingerprint(path):
    candidate = Path(path)
    if not candidate.is_file():
        return {
            "path": str(candidate),
            "exists": False,
            "size": None,
            "sha256": None,
        }
    digest = hashlib.sha256()
    with candidate.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return {
        "path": str(candidate.resolve()),
        "exists": True,
        "size": candidate.stat().st_size,
        "sha256": digest.hexdigest(),
    }


def validate_file_fingerprint(record, label):
    """Require the current artifact; treat a recorded identity as diagnostic."""
    if not isinstance(record, dict) or not record.get("path"):
        raise ClusterAssemblyBuildError(f"{label} path is missing")
    actual = file_fingerprint(record["path"])
    if actual.get("exists") is not True:
        raise ClusterAssemblyBuildError(
            f"{label} current file is missing: {actual.get('path')}"
        )
    expected_size = record.get("size")
    expected_sha256 = str(record.get("sha256") or "").casefold()
    actual["recorded_identity_is_diagnostic"] = True
    actual["recorded_identity_matches_current"] = bool(
        expected_size is not None
        and expected_sha256
        and int(actual.get("size") or -1) == int(expected_size)
        and str(actual.get("sha256") or "").casefold() == expected_sha256
    )
    return actual


def _normalized_contract_path(value):
    return os.path.normcase(os.path.abspath(os.path.normpath(str(value))))


def _short_contract_value(value, max_chars=240):
    try:
        text = _canonical_json(value)
    except (TypeError, ValueError):
        text = repr(value)
    if len(text) > max_chars:
        return text[: max_chars - 1] + "…"
    return text


def _first_contract_difference(expected, actual, path):
    """Return one deterministic semantic mismatch with its exact field path."""
    if isinstance(expected, dict) and isinstance(actual, dict):
        expected_is_artifact = bool(
            expected.get("path")
            and "exists" in expected
            and any(
                key in expected
                for key in (
                    "size",
                    "mtime_ns",
                    "sha256",
                    "fingerprint",
                )
            )
        )
        actual_is_artifact = bool(
            actual.get("path")
            and "exists" in actual
            and any(
                key in actual
                for key in (
                    "size",
                    "mtime_ns",
                    "sha256",
                    "fingerprint",
                )
            )
        )
        if expected_is_artifact and actual_is_artifact:
            expected_path = _normalized_contract_path(expected["path"])
            actual_path = _normalized_contract_path(actual["path"])
            if expected_path != actual_path:
                return (
                    f"{path}.path: expected={expected_path} "
                    f"actual={actual_path}"
                )
            for field in ("exists", "size"):
                if expected.get(field) != actual.get(field):
                    return (
                        f"{path}.{field}: "
                        f"expected={_short_contract_value(expected.get(field))} "
                        f"actual={_short_contract_value(actual.get(field))}"
                    )
            try:
                expected_content_key = artifact_record_content_key(expected)
                actual_content_key = artifact_record_content_key(actual)
            except (TypeError, ValueError):
                expected_content_key = None
                actual_content_key = None
            if expected_content_key and actual_content_key:
                if expected_content_key != actual_content_key:
                    field = str(expected_content_key.get("field") or "content")
                    return (
                        f"{path}.{field}: "
                        f"expected={_short_contract_value(expected_content_key)} "
                        f"actual={_short_contract_value(actual_content_key)}"
                    )
                # mtime is a discovery hint. Once both contracts carry the
                # same supported content key, a timestamp-only rewrite is not
                # an artifact change and must not invalidate Push.
                identity_fields = {
                    "path",
                    "exists",
                    "size",
                    "mtime_ns",
                    "sha256",
                    "fingerprint",
                    "fingerprint_algorithm",
                }
                expected_extra = {
                    key: value
                    for key, value in expected.items()
                    if key not in identity_fields
                }
                actual_extra = {
                    key: value
                    for key, value in actual.items()
                    if key not in identity_fields
                }
                return _first_contract_difference(
                    expected_extra,
                    actual_extra,
                    path,
                )
    if isinstance(expected, dict) and isinstance(actual, dict):
        for key in sorted(set(expected) | set(actual)):
            child_path = f"{path}.{key}"
            if key not in expected:
                return (
                    f"{child_path}: expected=<missing> "
                    f"actual={_short_contract_value(actual[key])}"
                )
            if key not in actual:
                return (
                    f"{child_path}: "
                    f"expected={_short_contract_value(expected[key])} "
                    "actual=<missing>"
                )
            difference = _first_contract_difference(
                expected[key], actual[key], child_path
            )
            if difference:
                return difference
        return ""
    if isinstance(expected, list) and isinstance(actual, list):
        if len(expected) != len(actual):
            return (
                f"{path}.length: expected={len(expected)} "
                f"actual={len(actual)}"
            )
        for index, (expected_item, actual_item) in enumerate(
            zip(expected, actual)
        ):
            difference = _first_contract_difference(
                expected_item,
                actual_item,
                f"{path}[{index}]",
            )
            if difference:
                return difference
        return ""
    if type(expected) is not type(actual) or expected != actual:
        return (
            f"{path}: expected={_short_contract_value(expected)} "
            f"actual={_short_contract_value(actual)}"
        )
    return ""


def _iter_contract_artifact_records(value, path="target_contract"):
    if isinstance(value, dict):
        is_fingerprint = bool(
            value.get("path")
            and "exists" in value
            and any(
                key in value
                for key in ("size", "mtime_ns", "sha256")
            )
        )
        if is_fingerprint:
            yield path, value
        for key, child in value.items():
            yield from _iter_contract_artifact_records(
                child, f"{path}.{key}"
            )
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _iter_contract_artifact_records(
                child, f"{path}[{index}]"
            )


def _validate_contract_artifact_record(path, expected):
    artifact_path = Path(str(expected.get("path") or ""))
    expected_exists = expected.get("exists") is True
    actual_exists = artifact_path.is_file()
    if actual_exists != expected_exists:
        raise ClusterAssemblyBuildError(
            "Cluster pass-through artifact mismatch at "
            f"{path}.exists: expected={expected_exists} "
            f"actual={actual_exists}; artifact={artifact_path}"
        )
    if not expected_exists:
        return
    try:
        stat = artifact_path.stat()
    except OSError as exc:
        raise ClusterAssemblyBuildError(
            "Cluster pass-through artifact could not be read at "
            f"{path}: {artifact_path}: {exc}"
        ) from exc
    if expected.get("size") is not None and int(expected["size"]) != stat.st_size:
        raise ClusterAssemblyBuildError(
            "Cluster pass-through artifact mismatch at "
            f"{path}.size: expected={expected['size']} "
            f"actual={stat.st_size}; artifact={artifact_path}"
        )
    try:
        expected_content_key = artifact_record_content_key(expected)
    except (TypeError, ValueError) as exc:
        raise ClusterAssemblyBuildError(
            "Cluster pass-through artifact fingerprint is invalid at "
            f"{path}: {exc}; artifact={artifact_path}"
        ) from exc
    if expected_content_key:
        try:
            actual_snapshot = file_content_key_snapshot(
                artifact_path,
                expected_content_key["algorithm"],
            )
        except (OSError, ValueError) as exc:
            raise ClusterAssemblyBuildError(
                "Cluster pass-through artifact could not be fingerprinted at "
                f"{path}: {artifact_path}: {exc}"
            ) from exc
        actual_digest = str(actual_snapshot.get("digest") or "").casefold()
        expected_digest = str(
            expected_content_key.get("digest") or ""
        ).casefold()
        if actual_digest != expected_digest:
            field = str(expected_content_key.get("field") or "content")
            raise ClusterAssemblyBuildError(
                "Cluster pass-through artifact mismatch at "
                f"{path}.{field}: expected={expected_digest} "
                f"actual={actual_digest}; artifact={artifact_path}"
            )
    elif expected.get("mtime_ns") is not None:
        if int(expected["mtime_ns"]) != stat.st_mtime_ns:
            raise ClusterAssemblyBuildError(
                "Cluster pass-through artifact mismatch at "
                f"{path}.mtime_ns: expected={expected['mtime_ns']} "
                f"actual={stat.st_mtime_ns}; artifact={artifact_path}"
            )


def build_receipt_pass_through_manifest(
    handoff,
    *,
    receipt_path,
    target_contract,
    target_spm,
):
    """Persist the exact receipt projection that made BWR pass through."""
    if not isinstance(target_contract, dict):
        raise ClusterAssemblyBuildError(
            "Cluster pass-through target contract is missing"
        )
    selected_handoff = target_contract.get("handoff") or {}
    if selected_handoff.get("status") != "pass_through":
        raise ClusterAssemblyBuildError(
            "Cluster pass-through target contract is actionable at "
            "target_contract.handoff.status: expected=pass_through "
            f"actual={selected_handoff.get('status') or '<missing>'}"
        )
    difference = _first_contract_difference(
        selected_handoff,
        handoff,
        "target_contract.handoff",
    )
    if difference:
        raise ClusterAssemblyBuildError(
            "BWR pass-through handoff differs from the selected target "
            f"contract at {difference}"
        )
    receipt = file_fingerprint(receipt_path)
    if (
        receipt.get("exists") is not True
        or receipt.get("size") is None
        or not receipt.get("sha256")
    ):
        raise ClusterAssemblyBuildError(
            "Cluster pass-through receipt fingerprint is incomplete"
        )
    target_contract_snapshot = deepcopy(target_contract)
    target_contract_sha256 = _sha256_bytes(
        _canonical_json(target_contract_snapshot).encode("utf-8")
    )
    provenance = {
        "schema_version": PASS_THROUGH_PROVENANCE_SCHEMA_VERSION,
        "requested_spm": str(Path(target_spm).resolve()),
        "receipt": receipt,
        "target_contract_sha256": target_contract_sha256,
        "target_contract": target_contract_snapshot,
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": MANIFEST_KIND,
        "status": "pass_through",
        "content_decision": "pass_through",
        "reason": PASS_THROUGH_PROVENANCE_REASON,
        "rendered_role_count": 0,
        "full_skeletal_mesh_preserved": True,
        "handoff": deepcopy(handoff),
        "handoff_evidence": {
            "pcg_receipt": deepcopy(receipt),
            "selected_target_contract_sha256": target_contract_sha256,
        },
        "pass_through_provenance": provenance,
    }


def validate_receipt_pass_through_manifest(
    manifest,
    *,
    receipt_path,
    target_contract,
    target_spm,
):
    """Validate the same target contract and every immutable artifact."""
    if not isinstance(manifest, dict):
        raise ClusterAssemblyBuildError(
            "Cluster pass-through manifest is missing"
        )
    if manifest.get("kind") != MANIFEST_KIND:
        raise ClusterAssemblyBuildError(
            "Cluster pass-through provenance mismatch at manifest.kind: "
            f"expected={MANIFEST_KIND} actual={manifest.get('kind')}"
        )
    if manifest.get("schema_version") != SCHEMA_VERSION:
        raise ClusterAssemblyBuildError(
            "Cluster pass-through provenance mismatch at "
            "manifest.schema_version: "
            f"expected={SCHEMA_VERSION} "
            f"actual={manifest.get('schema_version')}"
        )
    if manifest.get("status") != "pass_through":
        raise ClusterAssemblyBuildError(
            "Cluster pass-through provenance mismatch at manifest.status"
        )
    provenance = manifest.get("pass_through_provenance")
    if not isinstance(provenance, dict):
        raise ClusterAssemblyBuildError(
            "Cluster pass-through provenance is missing"
        )
    if (
        provenance.get("schema_version")
        != PASS_THROUGH_PROVENANCE_SCHEMA_VERSION
    ):
        raise ClusterAssemblyBuildError(
            "Cluster pass-through provenance schema_version changed: "
            f"expected={PASS_THROUGH_PROVENANCE_SCHEMA_VERSION} "
            f"actual={provenance.get('schema_version')}"
        )
    if manifest.get("content_decision") != "pass_through":
        raise ClusterAssemblyBuildError(
            "Cluster pass-through provenance mismatch at "
            "manifest.content_decision"
        )
    if manifest.get("reason") != PASS_THROUGH_PROVENANCE_REASON:
        raise ClusterAssemblyBuildError(
            "Cluster pass-through provenance mismatch at manifest.reason"
        )
    try:
        rendered_role_count = int(manifest.get("rendered_role_count", -1))
    except (TypeError, ValueError):
        rendered_role_count = -1
    if rendered_role_count != 0:
        raise ClusterAssemblyBuildError(
            "Cluster pass-through provenance mismatch at "
            "manifest.rendered_role_count: expected=0 "
            f"actual={manifest.get('rendered_role_count')}"
        )
    expected_target = _normalized_contract_path(
        provenance.get("requested_spm") or ""
    )
    actual_target = _normalized_contract_path(target_spm)
    if expected_target != actual_target:
        raise ClusterAssemblyBuildError(
            "Cluster pass-through provenance mismatch at "
            "pass_through_provenance.requested_spm: "
            f"expected={expected_target} actual={actual_target}"
        )

    saved_contract = provenance.get("target_contract")
    if not isinstance(saved_contract, dict):
        raise ClusterAssemblyBuildError(
            "Cluster pass-through provenance target_contract is missing"
        )
    saved_contract_sha256 = _sha256_bytes(
        _canonical_json(saved_contract).encode("utf-8")
    )
    if saved_contract_sha256 != provenance.get("target_contract_sha256"):
        raise ClusterAssemblyBuildError(
            "Cluster pass-through provenance mismatch at "
            "pass_through_provenance.target_contract_sha256"
        )
    handoff_evidence = manifest.get("handoff_evidence") or {}
    evidence_sha256 = handoff_evidence.get(
        "selected_target_contract_sha256"
    )
    if evidence_sha256 != saved_contract_sha256:
        raise ClusterAssemblyBuildError(
            "Cluster pass-through provenance mismatch at "
            "manifest.handoff_evidence.selected_target_contract_sha256"
        )
    current_handoff = (
        target_contract.get("handoff")
        if isinstance(target_contract, dict)
        else None
    ) or {}
    if current_handoff.get("status") != "pass_through":
        raise ClusterAssemblyBuildError(
            "Cluster pass-through target became actionable at "
            "target_contract.handoff.status: expected=pass_through "
            f"actual={current_handoff.get('status') or '<missing>'}"
        )
    difference = _first_contract_difference(
        saved_contract,
        target_contract,
        "target_contract",
    )
    if difference:
        raise ClusterAssemblyBuildError(
            "Cluster pass-through target contract changed at " + difference
        )
    difference = _first_contract_difference(
        saved_contract.get("handoff") or {},
        manifest.get("handoff") or {},
        "manifest.handoff",
    )
    if difference:
        raise ClusterAssemblyBuildError(
            "Cluster pass-through saved handoff changed at " + difference
        )

    # Compare the selected target before the enclosing receipt fingerprint.
    # A multi-target receipt can change because another target changed; when
    # this target changed, name its exact semantic field instead of reporting
    # only the outer JSON digest.
    recorded_receipt = provenance.get("receipt") or {}
    difference = _first_contract_difference(
        recorded_receipt,
        handoff_evidence.get("pcg_receipt") or {},
        "manifest.handoff_evidence.pcg_receipt",
    )
    if difference:
        raise ClusterAssemblyBuildError(
            "Cluster pass-through saved receipt evidence changed at "
            + difference
        )
    current_receipt = file_fingerprint(receipt_path)
    for field in ("path", "exists", "size", "sha256"):
        expected = recorded_receipt.get(field)
        actual = current_receipt.get(field)
        if field == "path":
            expected = _normalized_contract_path(expected or "")
            actual = _normalized_contract_path(actual or "")
        elif field == "sha256":
            expected = str(expected or "").casefold()
            actual = str(actual or "").casefold()
        if expected != actual:
            raise ClusterAssemblyBuildError(
                "Cluster pass-through receipt mismatch at "
                f"pass_through_provenance.receipt.{field}: "
                f"expected={expected} actual={actual}"
            )
    for artifact_path, artifact in _iter_contract_artifact_records(
        saved_contract
    ):
        _validate_contract_artifact_record(artifact_path, artifact)
    return True


_PHYSICAL_CAPTURE_KIND = "speedtree_cluster_physical_capture_fit"
_PHYSICAL_CAPTURE_WORKFLOW = "PHYSICAL_DIRECT_CAPTURE"
_DIRECT_CAPTURE_UV_SOURCE = "same_blender_physical_capture_projection"
_UNIT_PROBE_KIND = "speedtree_fbx_spm_unit_probe"
_NORMALIZED_PIVOT_CONTRACT = "normalized_attachment_origin_0_0_0"
_RECEIPT_SCAN_SKIP_KEYS = {
    "bindings",
    "bone_influences",
    "component_polygon_indices",
    "fit_matrix_world",
    "final_skeleton",
    "maps",
    "plan_hull",
    "result_uvs",
    "source_objects",
}


def _production_receipts(value, role=None):
    """Yield compact production receipts without walking binding-heavy payloads."""
    if isinstance(value, list):
        for row in value:
            yield from _production_receipts(row, role)
        return
    if not isinstance(value, dict):
        return

    local_role = str(value.get("role") or role or "").casefold() or None
    kind = str(value.get("kind") or "")
    if kind == _PHYSICAL_CAPTURE_KIND:
        yield "capture", local_role, value
        return
    if kind == _UNIT_PROBE_KIND:
        yield "unit_probe", local_role, value
        return
    if (
        value.get("workflow_mode") == _PHYSICAL_CAPTURE_WORKFLOW
        and isinstance(value.get("physical_capture_contract"), dict)
        and isinstance(value.get("prototypes"), list)
        and isinstance(value.get("variants"), list)
    ):
        yield "normalized", local_role, value

    for key, child in value.items():
        if key in _RECEIPT_SCAN_SKIP_KEYS:
            continue
        child_role = key.casefold() if key.casefold() in ROLE_ORDER else local_role
        if isinstance(child, (dict, list)):
            yield from _production_receipts(child, child_role)


def _deduplicated_receipts(manifest):
    result = {"capture": [], "unit_probe": [], "normalized": []}
    seen = set()
    for receipt_type, role, payload in _production_receipts(manifest):
        key = (receipt_type, role, _canonical_json(payload))
        if key in seen:
            continue
        seen.add(key)
        result[receipt_type].append((role, payload))
    return result


def _checked_normalized_bounds(value, label):
    if not isinstance(value, dict):
        raise ClusterAssemblyBuildError(f"{label} normalized bounds are missing")
    size = value.get("size")
    if not isinstance(size, (list, tuple)) or len(size) != 3:
        raise ClusterAssemblyBuildError(
            f"{label} normalized bounds size is invalid"
        )
    try:
        checked_size = [float(component) for component in size]
    except (TypeError, ValueError) as exc:
        raise ClusterAssemblyBuildError(
            f"{label} normalized bounds size is not numeric"
        ) from exc
    if any(
        not math.isfinite(component) or component < 0.0
        for component in checked_size
    ) or max(checked_size) <= 0.0:
        raise ClusterAssemblyBuildError(
            f"{label} normalized bounds size is not finite and non-negative"
        )
    result = deepcopy(value)
    result["size"] = checked_size
    return result


def _expected_normalized_bounds_for_variant(
    normalized_contract,
    variant,
    asset_name,
    ordinal,
):
    """Resolve one prototype's authored meter-space bounds from build metadata."""
    asset_key = str(asset_name or "").casefold()
    try:
        ordinal_key = int(ordinal)
    except (TypeError, ValueError):
        ordinal_key = 0
    candidates = []
    direct = (variant or {}).get("expected_normalized_bounds")
    if direct is None:
        direct = (variant or {}).get("normalized_bounds")
    if direct is not None:
        candidates.append(
            (
                2,
                _checked_normalized_bounds(
                    direct,
                    f"normalized variant {asset_name}",
                ),
            )
        )

    identity_keys = (
        "skeletal_asset_name",
        "skeletal_asset",
        "prototype_asset",
        "asset_name",
    )
    ordinal_keys = ("ordinal", "card_index", "index", "prototype_index")

    def walk(value):
        if isinstance(value, list):
            for child in value:
                walk(child)
            return
        if not isinstance(value, dict):
            return
        bounds = value.get("expected_normalized_bounds")
        if bounds is None:
            bounds = value.get("normalized_bounds")
        if bounds is not None:
            identities = {
                str(value.get(key) or "").casefold()
                for key in identity_keys
                if value.get(key)
            }
            ordinals = set()
            for key in ordinal_keys:
                if value.get(key) is None:
                    continue
                try:
                    ordinals.add(int(value[key]))
                except (TypeError, ValueError):
                    pass
            identity_matches = bool(asset_key and asset_key in identities)
            ordinal_matches = bool(ordinal_key and ordinal_key in ordinals)
            if identity_matches or (not identities and ordinal_matches):
                is_card_variant = bool(
                    value.get("card_index")
                    or value.get("plan")
                    or value.get("plan_name")
                )
                candidates.append(
                    (
                        1 if is_card_variant else 2,
                        _checked_normalized_bounds(
                            bounds,
                            f"normalized build metadata {asset_name}",
                        ),
                    )
                )
        for key, child in value.items():
            if key in _RECEIPT_SCAN_SKIP_KEYS or key in {
                "expected_normalized_bounds",
                "normalized_bounds",
            }:
                continue
            if isinstance(child, (dict, list)):
                walk(child)

    walk(normalized_contract or {})
    highest_priority = max(
        (priority for priority, _candidate in candidates),
        default=0,
    )
    unique = {
        _canonical_json(candidate): candidate
        for priority, candidate in candidates
        if priority == highest_priority
    }
    if len(unique) > 1:
        raise ClusterAssemblyBuildError(
            "normalized prototype has conflicting expected bounds metadata: "
            + str(asset_name)
        )
    return next(iter(unique.values())) if unique else None


def _receipt_normalized_bounds_by_asset(manifest):
    result = {}
    for role, receipt in _deduplicated_receipts(manifest or {})["normalized"]:
        for prototype in receipt.get("prototypes") or []:
            if not isinstance(prototype, dict):
                continue
            asset_name = str(
                prototype.get("skeletal_asset")
                or prototype.get("skeletal_asset_name")
                or prototype.get("prototype_asset")
                or ""
            )
            if not asset_name:
                continue
            bounds = _checked_normalized_bounds(
                prototype.get("normalized_bounds"),
                f"{role or 'normalized'} receipt prototype {asset_name}",
            )
            key = asset_name.casefold()
            existing = result.get(key)
            if existing is not None and _canonical_json(existing) != _canonical_json(
                bounds
            ):
                raise ClusterAssemblyBuildError(
                    "normalized receipts disagree on prototype bounds: "
                    + asset_name
                )
            result[key] = bounds
    return result


def _positive_number(value, label):
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ClusterAssemblyBuildError(f"{label} must be numeric") from exc
    if not math.isfinite(result) or result <= 0.0:
        raise ClusterAssemblyBuildError(f"{label} must be finite and positive")
    return result


def _validate_capture_receipt(contract, role):
    if (
        contract.get("kind") != _PHYSICAL_CAPTURE_KIND
        or int(contract.get("version") or 0) != 1
        or contract.get("workflow_mode") != _PHYSICAL_CAPTURE_WORKFLOW
        or contract.get("direct_uv_source") != _DIRECT_CAPTURE_UV_SOURCE
    ):
        raise ClusterAssemblyBuildError(
            f"{role} physical capture contract kind/workflow is invalid"
        )
    recorded_hash = str(contract.get("contract_sha256") or "")
    hash_payload = {
        key: value for key, value in contract.items()
        if key != "contract_sha256"
    }
    actual_hash = _sha256_bytes(_canonical_json(hash_payload).encode("utf-8"))
    if not recorded_hash or recorded_hash != actual_hash:
        raise ClusterAssemblyBuildError(
            f"{role} physical capture contract hash is missing or stale"
        )

    frame = contract.get("frame") or {}
    if (
        frame.get("policy") != "physical_target_uniform_whole_source_fit"
        or frame.get("workflow_mode") != _PHYSICAL_CAPTURE_WORKFLOW
        or frame.get("direct_uv_source") != _DIRECT_CAPTURE_UV_SOURCE
        or str(frame.get("unit_system") or "") != "METRIC"
    ):
        raise ClusterAssemblyBuildError(
            f"{role} physical capture frame policy is invalid"
        )
    scale_length = _positive_number(
        frame.get("scale_length"), f"{role} capture scale_length"
    )
    _positive_number(
        frame.get("fit_scale"), f"{role} capture fit scale"
    )
    width = _positive_number(frame.get("width"), f"{role} capture width")
    height = _positive_number(frame.get("height"), f"{role} capture height")
    content_width = _positive_number(
        frame.get("content_width"), f"{role} capture content width"
    )
    content_height = _positive_number(
        frame.get("content_height"), f"{role} capture content height"
    )
    target_meters = frame.get("target_meters") or []
    target_units = frame.get("target_blender_units") or []
    if len(target_meters) != 2 or len(target_units) != 2:
        raise ClusterAssemblyBuildError(
            f"{role} physical capture target evidence is incomplete"
        )
    targets_m = [
        _positive_number(value, f"{role} physical target meters")
        for value in target_meters
    ]
    targets_bu = [
        _positive_number(value, f"{role} physical target Blender Units")
        for value in target_units
    ]
    target_tolerance = 1.0e-6
    if any(abs(value - 0.1) > target_tolerance for value in targets_m):
        raise ClusterAssemblyBuildError(
            f"{role} physical capture target is not the declared 0.1m contract"
        )
    unit_tolerance = max(max(targets_bu) * 1.0e-6, 1.0e-9)
    if (
        abs(width - height) > unit_tolerance
        or max(abs(value - width) for value in targets_bu) > unit_tolerance
        or max(
            abs(targets_bu[index] * scale_length - targets_m[index])
            for index in range(2)
        )
        > target_tolerance
        or content_width > width + unit_tolerance
        or content_height > height + unit_tolerance
    ):
        raise ClusterAssemblyBuildError(
            f"{role} physical capture exceeds its declared 0.1m frame"
        )
    center = frame.get("center")
    if (
        not isinstance(center, (list, tuple))
        or len(center) != 3
        or any(not math.isfinite(float(value)) for value in center)
    ):
        raise ClusterAssemblyBuildError(
            f"{role} physical capture center is invalid"
        )
    plane = str(frame.get("plane") or "").upper()
    rotation = float(frame.get("rotation_degrees", math.nan))
    expected_rotation = 0.0 if plane == "XY" else 90.0
    if (
        plane not in {"XY", "XZ", "YZ"}
        or not math.isfinite(rotation)
        or abs(rotation - expected_rotation) > 1.0e-6
    ):
        raise ClusterAssemblyBuildError(
            f"{role} physical capture plane/rotation is invalid"
        )
    return {
        "contract_sha256": recorded_hash,
        "target_meters": targets_m,
        "target_blender_units": targets_bu,
        "scale_length": scale_length,
        "plane": plane,
        "rotation_degrees": rotation,
    }


def _validate_unit_probe_receipt(contract):
    if (
        contract.get("kind") != _UNIT_PROBE_KIND
        or int(contract.get("version") or 0) != 1
        or str(contract.get("status") or "").casefold() != "verified"
    ):
        raise ClusterAssemblyBuildError(
            "common FBX/SpeedTree unit probe is not verified"
        )
    target_meters = _positive_number(
        contract.get("physical_target_meters"),
        "unit probe physical target meters",
    )
    if abs(target_meters - 0.1) > 1.0e-6:
        raise ClusterAssemblyBuildError(
            "unit probe does not use the declared 0.1m capture target"
        )
    blender_units = contract.get("blender_units") or {}
    if str(blender_units.get("system") or "").upper() != "METRIC":
        raise ClusterAssemblyBuildError(
            "unit probe does not prove Blender METRIC units"
        )
    scale_length = _positive_number(
        blender_units.get("scale_length"), "unit probe scale_length"
    )
    target_bu = _positive_number(
        blender_units.get("target_blender_units"),
        "unit probe target Blender Units",
    )
    if abs(target_bu * scale_length - target_meters) > 1.0e-6:
        raise ClusterAssemblyBuildError(
            "unit probe target disagrees with Blender scale_length"
        )

    selected = contract.get("selected") or {}
    geometry_scale = _positive_number(
        selected.get("mesh_geometry_scale"), "FBX geometry scale"
    )
    asset_scale = _positive_number(
        selected.get("mesh_asset_scale"), "SpeedTree Mesh asset scale"
    )
    generator_scale = _positive_number(
        selected.get("generator_scale", 1.0), "SpeedTree generator scale"
    )
    identity_tolerance = 1.0e-9
    non_identity = [
        name
        for name, value in (
            ("FBX_GEOMETRY", geometry_scale),
            ("SPM_MESH_ASSET", asset_scale),
        )
        if abs(value - 1.0) > identity_tolerance
    ]
    if abs(generator_scale - 1.0) > identity_tolerance:
        raise ClusterAssemblyBuildError(
            "unit probe contains a forbidden generator scale patch"
        )
    if len(non_identity) > 1:
        raise ClusterAssemblyBuildError(
            "unit conversion is duplicated across FBX geometry and SPM Mesh Scale"
        )
    expected_location = non_identity[0] if non_identity else "IDENTITY"
    if str(selected.get("scale_location") or "").upper() != expected_location:
        raise ClusterAssemblyBuildError(
            "unit probe scale location disagrees with its numeric scales"
        )
    effective_scale = _positive_number(
        selected.get("effective_scale"), "effective downstream unit scale"
    )
    if abs(effective_scale - geometry_scale * asset_scale) > 1.0e-9:
        raise ClusterAssemblyBuildError(
            "unit probe effective scale is internally inconsistent"
        )
    generator_types = {
        str(row.get("generator_type") or "")
        for row in contract.get("generator_results") or []
        if isinstance(row, dict)
        and str(row.get("status") or "").casefold() == "verified"
        and bool(row.get("same_unit_contract"))
    }
    missing_types = {"Frond", "Leaf Mesh"} - generator_types
    if missing_types:
        raise ClusterAssemblyBuildError(
            "unit probe did not verify one common contract for: "
            + ", ".join(sorted(missing_types))
        )
    return {
        "physical_target_meters": target_meters,
        "scale_length": scale_length,
        "target_blender_units": target_bu,
        "mesh_geometry_scale": geometry_scale,
        "mesh_asset_scale": asset_scale,
        "generator_scale": generator_scale,
        "scale_location": expected_location,
        "effective_scale": effective_scale,
    }


def _part_ordinal(part):
    external = part.get("external_source") or {}
    try:
        return int(
            part.get("logical_subpart_index")
            or external.get("ordinal")
            or 0
        )
    except (TypeError, ValueError):
        return 0


def _validate_common_scale_metadata(manifest, parts, unit_probe):
    forbidden_root_keys = (
        "role_scale_overrides",
        "role_specific_scale_patches",
        "generator_scale_overrides",
        "scale_patches",
    )
    for key in forbidden_root_keys:
        if (manifest or {}).get(key):
            raise ClusterAssemblyBuildError(
                f"role-specific scale patch is forbidden: {key}"
            )

    explicit_patch_keys = (
        "role_multiplier",
        "role_scale",
        "role_scale_multiplier",
        "scale_override",
        "scale_patch",
    )
    generator_patch_keys = (
        "frond_shape_width_scale",
        "frond_shape_height_scale",
        "leaf_size_scale",
        "generator_size_scale",
    )
    common_aliases = {
        "mesh_geometry_scale": (
            "mesh_geometry_scale",
            "geometry_scale",
        ),
        "mesh_asset_scale": (
            "mesh_asset_scale",
            "asset_scale",
        ),
        "generator_scale": ("generator_scale",),
    }
    collected = {key: [] for key in common_aliases}
    for part in parts:
        external = part.get("external_source") or {}
        for container in (part, external):
            for key in explicit_patch_keys:
                if key in container and container.get(key) not in (
                    None, False, 1, 1.0, {}, [],
                ):
                    raise ClusterAssemblyBuildError(
                        f"role-specific scale patch is forbidden: {key}"
                    )
            for key in generator_patch_keys:
                if key in container:
                    value = _positive_number(
                        container.get(key), f"generator size patch {key}"
                    )
                    if abs(value - 1.0) > 1.0e-9:
                        raise ClusterAssemblyBuildError(
                            f"automatic generator size patch is forbidden: {key}"
                        )
            for canonical, aliases in common_aliases.items():
                values = [
                    container.get(alias) for alias in aliases
                    if alias in container
                ]
                if values:
                    collected[canonical].append(
                        _positive_number(
                            values[0], f"{canonical} on {part.get('asset_name')}"
                        )
                    )
    for key, values in collected.items():
        if not values:
            continue
        expected = float(unit_probe[key])
        if len(values) != len(parts) or any(
            abs(value - expected) > 1.0e-9 for value in values
        ):
            raise ClusterAssemblyBuildError(
                f"{key} is not one common role-independent unit contract"
            )


def validate_normalized_prototype_unit_contract(manifest):
    """Fail closed for the physical normalized production Assembly contract.

    Legacy camera-UV/component manifests remain supported until a normalized
    physical-capture or unit-probe receipt is present.  Once any such receipt
    is present, the complete production evidence set is mandatory.
    """
    receipts = _deduplicated_receipts(manifest or {})
    if not any(receipts.values()):
        return {"status": "legacy_contract_not_present"}

    parts = list((manifest or {}).get("parts") or [])
    variants = list((manifest or {}).get("registered_variants") or [])
    placement_contract = (manifest or {}).get("placement_contract")
    placement_v1 = placement_contract is not None
    authored_nodes_available = False
    if placement_v1:
        authored_table_summary = (
            placement_contract.get("authored_node_table") or {}
        )
        authored_nodes_available = bool(
            authored_table_summary.get("available")
        )
        try:
            residual_gate_contract = (
                placement_contract.get("residual_ready_gate") or {}
            )
            placement_threshold = float(
                residual_gate_contract.get("threshold")
            )
            placement_pivot_threshold = float(
                residual_gate_contract.get(
                    "authored_pivot_error_threshold_meters"
                )
            )
        except (TypeError, ValueError) as exc:
            raise ClusterAssemblyBuildError(
                "Assembly placement residual ready gate is invalid"
            ) from exc
        if (
            placement_contract.get("version") != PLACEMENT_CONTRACT_VERSION
            or placement_contract.get("status") != "ready"
            or placement_contract.get(
                "node_fields_do_not_serialize_rotation_or_uniform_scale"
            ) is not True
            or abs(
                placement_threshold - MAX_ASSEMBLY_RESIDUAL_RELATIVE_RMS
            ) > 1.0e-15
            or abs(
                placement_pivot_threshold - MAX_ASSEMBLY_PIVOT_ERROR_METERS
            ) > 1.0e-15
            or (
                placement_contract.get("residual_ready_gate") or {}
            ).get("status") not in {
                "pass",
                "pass_with_quality_warnings",
            }
        ):
            raise ClusterAssemblyBuildError(
                "Assembly placement contract is invalid or ungated"
            )
    prepared_unused = list(
        (manifest or {}).get("prepared_unused_roles") or []
    )
    if not parts and variants and prepared_unused:
        if any(bool(row.get("instanced")) for row in variants):
            raise ClusterAssemblyBuildError(
                "prepared-unused Assembly variants cannot be marked instanced"
            )
        return {
            "status": "prepared_unused_only",
            "native_prototype_count": 0,
            "registered_variant_count": len(variants),
            "roles": dict(Counter(
                str(row.get("role") or "").casefold()
                for row in variants
            )),
        }
    if not parts or not variants:
        raise ClusterAssemblyBuildError(
            "physical normalized production manifest has no native prototypes/variants"
        )
    unit_probe_receipts = {
        _canonical_json(payload): payload
        for _role, payload in receipts["unit_probe"]
    }
    if len(unit_probe_receipts) != 1:
        raise ClusterAssemblyBuildError(
            "physical normalized production requires one common unit-probe contract"
        )
    unit_probe = _validate_unit_probe_receipt(
        next(iter(unit_probe_receipts.values()))
    )

    prototype_rows = {}
    prototype_ids = set()
    asset_names = set()
    role_ordinals = defaultdict(list)
    provider_role_ordinals = defaultdict(list)
    for part in parts:
        role = str(part.get("role") or "").casefold()
        provider_key = str(part.get("provider_key") or role).casefold()
        ordinal = _part_ordinal(part)
        prototype_id = str(part.get("prototype_id") or "")
        asset_name = str(part.get("asset_name") or "")
        external = part.get("external_source") or {}
        key = (provider_key, role, ordinal)
        if (
            role not in ROLE_ORDER
            or not provider_key
            or ordinal <= 0
            or not prototype_id
            or not asset_name
            or external.get("kind")
            != "send_to_unreal_normalized_skeletal_part"
            or str(external.get("pivot_contract") or "")
            != _NORMALIZED_PIVOT_CONTRACT
            or key in prototype_rows
            or prototype_id in prototype_ids
            or asset_name.casefold() in asset_names
        ):
            raise ClusterAssemblyBuildError(
                "native normalized prototype specification is invalid or duplicated"
            )
        bindings = list(part.get("bindings") or [])
        if not bindings:
            raise ClusterAssemblyBuildError(
                f"normalized prototype is never instanced: {asset_name}"
            )
        for binding in bindings:
            transform = binding.get("transform") or {}
            fit_mode = str(transform.get("fit_mode") or "")
            scale = transform.get("scale")
            similarity_relative_rms = transform.get(
                "similarity_relative_rms"
            )
            scale_values = None
            if isinstance(scale, (list, tuple)) and len(scale) == 3:
                try:
                    scale_values = [float(value) for value in scale]
                except (TypeError, ValueError):
                    scale_values = None
            if (
                not fit_mode.startswith("uniform_similarity_3d")
                or "attachment_locked" not in fit_mode
                or transform.get("attachment_pivot_error") is None
                or similarity_relative_rms is None
                or scale_values is None
                or any(
                    not math.isfinite(value) or value <= 0.0
                    for value in scale_values
                )
                or max(scale_values) - min(scale_values) > 1.0e-7
            ):
                raise ClusterAssemblyBuildError(
                    "normalized prototype instances require uniform-similarity transforms"
                )
            try:
                pivot_error = float(
                    transform.get("attachment_pivot_error")
                )
            except (TypeError, ValueError) as exc:
                raise ClusterAssemblyBuildError(
                    "normalized prototype instance has no attachment-locked pivot"
                ) from exc
            if not math.isfinite(pivot_error) or pivot_error > 1.0e-8:
                raise ClusterAssemblyBuildError(
                    "normalized prototype instance attachment pivot drifted"
                )
            try:
                similarity_relative_rms = float(
                    similarity_relative_rms
                )
            except (TypeError, ValueError) as exc:
                raise ClusterAssemblyBuildError(
                    "normalized prototype instance similarity residual is invalid"
                ) from exc
            if not math.isfinite(similarity_relative_rms):
                raise ClusterAssemblyBuildError(
                    "normalized prototype instance similarity residual is invalid"
                )
            if placement_v1:
                validate_persisted_residual_gate(
                    transform,
                    expected_threshold=MAX_ASSEMBLY_RESIDUAL_RELATIVE_RMS,
                )
                placement_source = str(
                    transform.get("placement_source") or ""
                )
                authored_node = transform.get("authored_node")
                if authored_nodes_available:
                    authored_binding = (
                        placement_source.startswith(
                            "authored_spm_node_translation_and_state__"
                        )
                        and isinstance(authored_node, dict)
                        and bool(authored_node.get("node_guid"))
                        and authored_node.get("valid_position") is True
                        and authored_node.get("hidden") is False
                        and authored_node.get("deleted") is False
                        and authored_node.get("culled") is False
                    )
                    degraded_binding = (
                        placement_source
                        == "render_attachment_translation__"
                        "authored_node_assignment_degraded"
                        and authored_node is None
                        and isinstance(
                            transform.get("authored_node_match_warning"),
                            dict,
                        )
                    )
                    if not authored_binding and not degraded_binding:
                        raise ClusterAssemblyBuildError(
                            "normalized prototype instance has neither an "
                            "authored Node nor explicit degraded attachment evidence"
                        )
                elif (
                    placement_source
                    != "legacy_post_cull_similarity_fallback__"
                    "authored_node_data_absent"
                    or authored_node is not None
                ):
                    raise ClusterAssemblyBuildError(
                        "legacy Assembly placement fallback was used despite "
                        "authored Node evidence"
                    )
        prototype_rows[key] = part
        prototype_ids.add(prototype_id)
        asset_names.add(asset_name.casefold())
        role_ordinals[role].append(ordinal)
        provider_role_ordinals[(provider_key, role)].append(ordinal)

    variant_rows = {}
    instanced_variant_rows = {}
    for variant in variants:
        role = str(variant.get("role") or "").casefold()
        provider_key = str(
            variant.get("provider_key") or role
        ).casefold()
        try:
            ordinal = int(variant.get("ordinal") or 0)
        except (TypeError, ValueError):
            ordinal = 0
        key = (provider_key, role, ordinal)
        if (
            role not in ROLE_ORDER
            or not provider_key
            or ordinal <= 0
            or key in variant_rows
        ):
            raise ClusterAssemblyBuildError(
                "registered normalized variant is invalid or duplicated"
            )
        instanced = variant.get("instanced")
        if not isinstance(instanced, bool):
            raise ClusterAssemblyBuildError(
                f"registered normalized variant has no usage decision: {key}"
            )
        if str(variant.get("pivot_contract") or "") != _NORMALIZED_PIVOT_CONTRACT:
            raise ClusterAssemblyBuildError(
                f"registered normalized variant pivot contract is invalid: {key}"
            )
        try:
            attachment_index = int(
                variant.get("attachment_vertex_index")
            )
            attachment_uv = [
                float(value)
                for value in variant.get("attachment_vertex_uv")
            ]
        except (TypeError, ValueError) as exc:
            raise ClusterAssemblyBuildError(
                f"registered normalized variant attachment is invalid: {key}"
            ) from exc
        if (
            attachment_index < 0
            or len(attachment_uv) != 2
            or any(not math.isfinite(value) for value in attachment_uv)
        ):
            raise ClusterAssemblyBuildError(
                f"registered normalized variant attachment is invalid: {key}"
            )
        variant_rows[key] = variant
        if instanced:
            instanced_variant_rows[key] = variant
    if set(instanced_variant_rows) != set(prototype_rows):
        raise ClusterAssemblyBuildError(
            "instanced registered variants do not exactly match native prototype specs"
        )
    for key, part in prototype_rows.items():
        if (
            str(instanced_variant_rows[key].get("skeletal_asset_name") or "")
            != str(part.get("asset_name") or "")
        ):
            raise ClusterAssemblyBuildError(
                "registered variant asset does not match its native prototype spec"
            )

    normalized_by_role = defaultdict(list)
    for role, receipt in receipts["normalized"]:
        if role in role_ordinals:
            normalized_by_role[role].append(receipt)
    if set(normalized_by_role) != set(role_ordinals):
        raise ClusterAssemblyBuildError(
            "every normalized prototype role requires a production normalization receipt"
        )

    capture_reports = {}
    receipt_variant_rows = {}
    available_receipts_by_role = {
        role: {
            _canonical_json(value): value
            for value in normalized_by_role[role]
        }
        for role in role_ordinals
    }
    used_receipts_by_role = defaultdict(set)
    for provider_role, ordinals in sorted(provider_role_ordinals.items()):
        provider_key, role = provider_role
        expected_assets = {
            str(variant.get("skeletal_asset_name") or "")
            for (
                variant_provider,
                variant_role,
                _ordinal,
            ), variant in variant_rows.items()
            if (
                variant_provider == provider_key
                and variant_role == role
            )
        }
        normalized_matches = []
        for receipt_hash, candidate in (
            available_receipts_by_role[role].items()
        ):
            candidate_assets = {
                str(row.get("skeletal_asset") or "")
                for row in candidate.get("prototypes") or []
                if isinstance(row, dict)
            }
            if candidate_assets == expected_assets:
                normalized_matches.append((receipt_hash, candidate))
        if len(normalized_matches) != 1:
            raise ClusterAssemblyBuildError(
                f"{provider_key} has ambiguous production normalization receipts"
            )
        receipt_hash, normalized = normalized_matches[0]
        if receipt_hash in used_receipts_by_role[role]:
            raise ClusterAssemblyBuildError(
                f"{provider_key} reuses another provider's normalization receipt"
            )
        used_receipts_by_role[role].add(receipt_hash)
        capture = normalized.get("physical_capture_contract") or {}
        capture_report = _validate_capture_receipt(capture, role)
        capture_reports[provider_role] = capture_report
        if (
            normalized.get("direct_uv_source") != _DIRECT_CAPTURE_UV_SOURCE
            or normalized.get("size_policy")
            != "uniform_whole_source_physical_target_meters"
            or normalized.get("plan_uv_policy")
            != "direct_physical_capture_projection"
            or normalized.get("generator_size_policy")
            != "preserve_user_authored_leaf_and_frond_dimensions"
            or str(normalized.get("physical_capture_contract_sha256") or "")
            != capture_report["contract_sha256"]
        ):
            raise ClusterAssemblyBuildError(
                f"{role} normalization receipt is not the physical direct-capture contract"
            )

        receipt_prototypes = normalized.get("prototypes") or []
        receipt_assets = {
            str(row.get("skeletal_asset") or "")
            for row in receipt_prototypes
            if isinstance(row, dict)
        }
        pivot_rows = capture.get("attachment_pivots") or []
        pivot_assets = {
            str(row.get("prototype_asset") or "")
            for row in pivot_rows
            if isinstance(row, dict)
        }
        if (
            len(receipt_prototypes) != len(expected_assets)
            or receipt_assets != expected_assets
            or len(pivot_rows) != len(expected_assets)
            or pivot_assets != expected_assets
            ):
                raise ClusterAssemblyBuildError(
                    f"{provider_key} normalized prototype receipts do not "
                    "match native prototype specs"
                )
        for pivot in pivot_rows:
            local = pivot.get("normalized_local")
            if (
                not isinstance(local, (list, tuple))
                or len(local) != 3
                or max(abs(float(value)) for value in local) > 1.0e-8
            ):
                raise ClusterAssemblyBuildError(
                    f"{provider_key} attachment pivot is not baked to (0,0,0)"
                )
        target_units = capture_report["target_blender_units"]
        bounds_tolerance = max(max(target_units) * 1.0e-6, 1.0e-9)
        for prototype in receipt_prototypes:
            size = (prototype.get("normalized_bounds") or {}).get("size")
            if (
                not isinstance(size, (list, tuple))
                or len(size) != 3
                or any(
                    not math.isfinite(float(value)) or float(value) < 0.0
                    for value in size
                )
                or float(size[0]) > target_units[0] + bounds_tolerance
                or float(size[1]) > target_units[1] + bounds_tolerance
            ):
                raise ClusterAssemblyBuildError(
                    f"{provider_key} prototype physical bounds exceed the "
                    "declared 0.1m capture contract"
                )

        for receipt_variant in normalized.get("variants") or []:
            try:
                ordinal = int(
                    receipt_variant.get("card_index")
                    or receipt_variant.get("index")
                    or 0
                )
            except (TypeError, ValueError):
                ordinal = 0
            key = (provider_key, role, ordinal)
            transfer = receipt_variant.get("plan_uv_transfer") or {}
            try:
                attachment_index = int(
                    transfer.get("attachment_vertex_index")
                )
                attachment_uv = [
                    float(value)
                    for value in transfer.get("attachment_vertex_uv")
                ]
            except (TypeError, ValueError):
                attachment_index = -1
                attachment_uv = []
            if (
                key in receipt_variant_rows
                or receipt_variant.get("object_transforms_identity") is not True
                or receipt_variant.get("plan_covers_projection") is not True
                or transfer.get("policy")
                != "direct_physical_capture_projection"
                or transfer.get("direct_uv_source")
                != _DIRECT_CAPTURE_UV_SOURCE
                or attachment_index < 0
                or len(attachment_uv) != 2
                or any(not math.isfinite(value) for value in attachment_uv)
            ):
                raise ClusterAssemblyBuildError(
                    f"{provider_key} normalized variant direct-capture "
                    "evidence is invalid"
                )
            receipt_variant_rows[key] = receipt_variant
    unused_receipts_by_role = {
        role: len(set(available) - used_receipts_by_role[role])
        for role, available in available_receipts_by_role.items()
        if set(available) - used_receipts_by_role[role]
    }
    relevant_variant_rows = {
        key: variant
        for key, variant in variant_rows.items()
        if (key[0], key[1]) in provider_role_ordinals
    }
    if set(receipt_variant_rows) != set(relevant_variant_rows):
        raise ClusterAssemblyBuildError(
            "normalization receipt variants do not exactly match registered variants"
        )
    for key, registered in relevant_variant_rows.items():
        receipt_variant = receipt_variant_rows[key]
        if (
            str(receipt_variant.get("skeletal_asset") or "")
            != str(registered.get("skeletal_asset_name") or "")
            or str(receipt_variant.get("plan") or "")
            != str(registered.get("card_name") or "")
            or int(
                (receipt_variant.get("plan_uv_transfer") or {}).get(
                    "attachment_vertex_index"
                )
            )
            != int(registered.get("attachment_vertex_index"))
            or [
                float(value)
                for value in (
                    receipt_variant.get("plan_uv_transfer") or {}
                ).get("attachment_vertex_uv")
            ]
            != [
                float(value)
                for value in registered.get("attachment_vertex_uv")
            ]
        ):
            raise ClusterAssemblyBuildError(
                "registered variant identity differs from normalization receipt"
            )

    if any(
        abs(report["target_meters"][0] - unit_probe["physical_target_meters"])
        > 1.0e-6
        or abs(report["scale_length"] - unit_probe["scale_length"]) > 1.0e-9
        for report in capture_reports.values()
    ):
        raise ClusterAssemblyBuildError(
            "capture receipts and downstream unit probe use different unit contracts"
        )
    _validate_common_scale_metadata(manifest, parts, unit_probe)
    return {
        "status": "verified",
        "native_prototype_count": len(prototype_rows),
        "registered_variant_count": len(variant_rows),
        "instanced_variant_count": len(instanced_variant_rows),
        "roles": {
            role: len(ordinals)
            for role, ordinals in sorted(role_ordinals.items())
        },
        "role_ordinals": {
            role: sorted(ordinals)
            for role, ordinals in sorted(role_ordinals.items())
        },
        "provider_ordinals": {
            provider_key: sorted(ordinals)
            for (provider_key, _role), ordinals in sorted(
                provider_role_ordinals.items()
            )
        },
        "physical_target_meters": unit_probe["physical_target_meters"],
        "downstream_unit_scale": unit_probe["effective_scale"],
        "scale_location": unit_probe["scale_location"],
        "role_specific_scale_patch": False,
        "instance_fit": "uniform_similarity_3d",
        "unused_normalization_receipts_are_diagnostic": True,
        "unused_normalization_receipts_by_role": unused_receipts_by_role,
    }


def validate_manifest_artifacts(manifest):
    """Revalidate every Blender/FBX/wind artifact at each consumer boundary."""
    if (manifest or {}).get("status") != "ready":
        raise ClusterAssemblyBuildError("Assembly manifest is not ready")
    if not list((manifest or {}).get("parts") or []):
        raise ClusterAssemblyBuildError(
            "ready Assembly manifest has no instantiated parts; prepared-only "
            "content must be pass_through and cannot publish an Assembly"
        )
    normalized_contract = validate_normalized_prototype_unit_contract(manifest)
    _validate_public_export_names(manifest)
    checked = {
        "normalized_prototype_unit_contract": normalized_contract,
        "full_fbx": validate_file_fingerprint(
            manifest.get("full_fbx"), "Full SK FBX"
        ),
        "base_fbx": validate_file_fingerprint(
            (manifest.get("base") or {}).get("fbx"), "Assembly base FBX"
        ),
        "wind_json": validate_file_fingerprint(
            (manifest.get("wind_contract") or {}).get("wind_json"),
            "final Skeleton wind JSON",
        ),
        "parts": {},
    }
    placement_contract = manifest.get("placement_contract")
    if placement_contract is not None:
        checked["authored_placement_spm"] = validate_file_fingerprint(
            placement_contract.get("source_spm"),
            "Assembly authored placement SPM",
        )
    source_blend = manifest.get("assembly_source_blend")
    if source_blend is not None:
        checked["assembly_source_blend"] = validate_file_fingerprint(
            source_blend,
            "Assembly Blender source",
        )
    for part in manifest.get("parts") or []:
        prototype_id = str(part.get("prototype_id") or "")
        if not prototype_id or prototype_id in checked["parts"]:
            raise ClusterAssemblyBuildError(
                f"invalid or duplicate Assembly prototype id: {prototype_id}"
            )
        external = part.get("external_source") or {}
        if external:
            checked["parts"][prototype_id] = {
                "source_blend": validate_file_fingerprint(
                    external.get("source_blend"),
                    f"Send to Unreal source blend {prototype_id}",
                ),
                "plan_fbx": validate_file_fingerprint(
                    external.get("plan_fbx"),
                    f"normalized plan FBX {prototype_id}",
                ),
            }
        else:
            checked["parts"][prototype_id] = validate_file_fingerprint(
                part.get("fbx"), f"Assembly part FBX {prototype_id}"
            )
    return checked


def normalize_role_identity(value):
    name = str(value or "").split("\x00", 1)[0]
    name = name.strip().replace("\\", "/").rsplit("/", 1)[-1]
    if "::" in name:
        name = name.rsplit("::", 1)[-1]
    while re.search(r"\.\d{3}$", name):
        name = re.sub(r"\.\d{3}$", "", name)
    if name.casefold().endswith("_mat"):
        name = name[:-4]
    if name.casefold().startswith("m_"):
        name = name[2:]
    return name.strip().casefold()


def content_build_decision(handoff):
    """Return ``build`` or ``pass_through`` solely from receipt content."""
    if not isinstance(handoff, dict):
        raise ClusterAssemblyBuildError("cluster Assembly handoff is missing")
    status = str(handoff.get("status") or "")
    if status == "blocked":
        issues = handoff.get("issues") or []
        raise ClusterAssemblyBuildError(
            "cluster Assembly handoff is blocked: " + _canonical_json(issues)
        )
    if status == "pass_through":
        if (handoff.get("assembly") or {}).get("requested"):
            raise ClusterAssemblyBuildError(
                "pass-through handoff cannot request an Assembly"
            )
        return "pass_through"
    if status != "ready":
        raise ClusterAssemblyBuildError(
            f"unsupported cluster Assembly handoff status: {status or '<empty>'}"
        )
    assembly = handoff.get("assembly") or {}
    if not assembly.get("requested"):
        raise ClusterAssemblyBuildError("ready handoff did not request an Assembly")
    if not (handoff.get("full_skeletal_mesh") or {}).get("preserved"):
        raise ClusterAssemblyBuildError("Full Skeletal Mesh preservation is not proven")
    inputs = assembly.get("part_builder_inputs") or []
    if not inputs:
        raise ClusterAssemblyBuildError("ready handoff contains no part inputs")
    seen = set()
    for row in inputs:
        role = str(row.get("role") or "").casefold()
        provider_key = str(row.get("provider_key") or role).casefold()
        if role not in ROLE_ORDER or not provider_key or provider_key in seen:
            raise ClusterAssemblyBuildError(
                "invalid or duplicate Assembly provider: "
                f"role={role}, provider={provider_key}"
            )
        seen.add(provider_key)
        if not (
            row.get("assignments")
            or row.get("rendered_provider_expansion_covered") is True
        ):
            raise ClusterAssemblyBuildError(
                f"{role} has no actual FBX polygon assignment evidence"
            )
        normalized = row.get("normalized_variants")
        variants = list((normalized or {}).get("variants") or [])
        try:
            ordinals = [int(value.get("ordinal") or 0) for value in variants]
        except (AttributeError, TypeError, ValueError):
            ordinals = []
        composite_ready = True
        for variant in variants:
            mode = str(variant.get("source_partition_mode") or "")
            composite_parts = variant.get("composite_parts") or []
            if mode == "COMPOSITE_PER_DEFORM_ROOT":
                try:
                    subpart_ordinals = [
                        int(part.get("subpart_index") or 0)
                        for part in composite_parts
                    ]
                except (AttributeError, TypeError, ValueError):
                    subpart_ordinals = []
                composite_ready = composite_ready and (
                    bool(composite_parts)
                    and subpart_ordinals
                    == list(range(1, len(composite_parts) + 1))
                )
            elif composite_parts:
                composite_ready = False
        if (
            not isinstance(normalized, dict)
            or normalized.get("status") != "ready"
            or not variants
            or ordinals != list(range(1, len(variants) + 1))
            or not composite_ready
        ):
            raise ClusterAssemblyBuildError(
                f"{role} requires ready external normalized variants; "
                "component-derived Assembly parts are forbidden"
            )
    return "build"


def _matrix_rows(matrix):
    return [
        [round(float(value), 9) for value in row]
        for row in matrix
    ]


def snapshot_blender_armature(armature):
    """Capture the final FBX-import Skeleton, including its object root.

    The existing BWR/Send2UE FBX contract imports the Blender armature object as
    reference-skeleton index 0, then the authored Blender bones starting at
    index 1.  DynamicWind generation uses that exact exported identity.
    """
    if getattr(armature, "type", "") != "ARMATURE":
        raise ClusterAssemblyBuildError("final armature object is invalid")
    bones = list(getattr(getattr(armature, "data", None), "bones", ()) or ())
    if not bones:
        raise ClusterAssemblyBuildError("final armature has no bones")
    indices = {bone.name: index + 1 for index, bone in enumerate(bones)}
    rows = [
        {
            "index": 0,
            "name": str(armature.name),
            "parent_index": -1,
            "parent_name": None,
            "bind_matrix": _matrix_rows(armature.matrix_world),
        }
    ]
    for source_index, bone in enumerate(bones):
        index = source_index + 1
        parent = getattr(bone, "parent", None)
        parent_name = str(parent.name) if parent else None
        rows.append(
            {
                "index": index,
                "name": str(bone.name),
                "parent_index": indices[parent_name] if parent_name else 0,
                "parent_name": parent_name or str(armature.name),
                "bind_matrix": _matrix_rows(bone.matrix_local),
            }
        )
    return make_skeleton_snapshot(rows)


def make_skeleton_snapshot(bones):
    """Validate and hash ordered final-skeleton records."""
    normalized = []
    names = set()
    for expected_index, raw in enumerate(bones or []):
        index = int(raw.get("index", -1))
        name = str(raw.get("name") or "")
        parent_index = int(raw.get("parent_index", -1))
        parent_name = raw.get("parent_name")
        if index != expected_index:
            raise ClusterAssemblyBuildError(
                f"skeleton index discontinuity at {expected_index}: {index}"
            )
        if not name or name in names:
            raise ClusterAssemblyBuildError(f"invalid or duplicate bone name: {name}")
        if parent_index >= index or parent_index < -1:
            raise ClusterAssemblyBuildError(
                f"invalid parent index for {name}: {parent_index}"
            )
        if parent_index == -1:
            if parent_name not in (None, ""):
                raise ClusterAssemblyBuildError(
                    f"root bone {name} has a parent name without an index"
                )
            parent_name = None
        else:
            expected_parent = normalized[parent_index]["name"]
            if str(parent_name or "") != expected_parent:
                raise ClusterAssemblyBuildError(
                    f"parent name/index mismatch for {name}: "
                    f"{parent_name!r} != {expected_parent!r}"
                )
            parent_name = expected_parent
        bind_matrix = raw.get("bind_matrix")
        if bind_matrix is not None:
            if len(bind_matrix) != 4 or any(len(row) != 4 for row in bind_matrix):
                raise ClusterAssemblyBuildError(
                    f"bind matrix for {name} is not 4x4"
                )
            bind_matrix = _matrix_rows(bind_matrix)
        normalized.append(
            {
                "index": index,
                "name": name,
                "parent_index": parent_index,
                "parent_name": parent_name,
                "bind_matrix": bind_matrix,
            }
        )
        names.add(name)
    if not normalized:
        raise ClusterAssemblyBuildError("final skeleton is empty")
    roots = [row for row in normalized if row["parent_index"] == -1]
    if len(roots) != 1 or roots[0]["index"] != 0:
        raise ClusterAssemblyBuildError(
            "final skeleton must have exactly one root at index 0"
        )
    digest = _sha256_bytes(_canonical_json(normalized).encode("utf-8"))
    identity_sha1 = hashlib.sha1()
    for row in normalized:
        identity_sha1.update(
            (
                f"{row['index']}\0{row['name']}\0"
                f"{row['parent_index']}\n"
            ).encode("utf-8")
        )
    return {
        "contract": "final_skeleton_v2",
        "bone_count": len(normalized),
        "bones": normalized,
        "sha256": digest,
        "bone_name_index_parent_sha1": identity_sha1.hexdigest(),
    }


def _skeleton_maps(snapshot):
    bones = list((snapshot or {}).get("bones") or [])
    checked = make_skeleton_snapshot(bones)
    if snapshot.get("sha256") and snapshot.get("sha256") != checked["sha256"]:
        raise ClusterAssemblyBuildError("final skeleton snapshot hash mismatch")
    by_name = {row["name"]: row for row in checked["bones"]}
    return checked, by_name


def ancestor_chain(bone_name, skeleton_snapshot, skeleton_by_name=None):
    if skeleton_by_name is None:
        checked, by_name = _skeleton_maps(skeleton_snapshot)
        del checked
    else:
        by_name = skeleton_by_name
    if bone_name not in by_name:
        raise ClusterAssemblyBuildError(
            f"binding bone is missing from final skeleton: {bone_name}"
        )
    chain = []
    row = by_name[bone_name]
    while row is not None:
        chain.append(row["name"])
        parent = row["parent_name"]
        row = by_name.get(parent) if parent else None
    return chain


def lowest_common_ancestor(
    bone_names,
    skeleton_snapshot,
    skeleton_by_name=None,
):
    names = list(dict.fromkeys(str(value) for value in bone_names if value))
    if not names:
        raise ClusterAssemblyBuildError("binding contains no bones")
    chains = [
        ancestor_chain(name, skeleton_snapshot, skeleton_by_name)
        for name in names
    ]
    shared = set(chains[0])
    for chain in chains[1:]:
        shared.intersection_update(chain)
    if not shared:
        raise ClusterAssemblyBuildError(
            "binding bones do not share a final-skeleton ancestor: "
            + ", ".join(names)
        )
    return next(name for name in chains[0] if name in shared)


def validate_binding_hierarchy(
    binding,
    skeleton_snapshot,
    wind_bones=None,
    skeleton_by_name=None,
):
    if skeleton_by_name is None:
        checked, skeleton_by_name = _skeleton_maps(skeleton_snapshot)
        del checked
    influences = list((binding or {}).get("bone_influences") or [])
    if not influences:
        raise ClusterAssemblyBuildError("Assembly binding has no bone influences")
    names = []
    total = 0.0
    for item in influences:
        name = str(item.get("bone") or "")
        weight = float(item.get("weight", 0.0))
        if not name or not math.isfinite(weight) or weight <= 0.0:
            raise ClusterAssemblyBuildError(
                f"invalid Assembly bone influence: {item!r}"
            )
        ancestor_chain(name, skeleton_snapshot, skeleton_by_name)
        if wind_bones is not None and name not in wind_bones:
            raise ClusterAssemblyBuildError(
                f"Assembly binding bone is absent from final wind JSON: {name}"
            )
        names.append(name)
        total += weight
    if not math.isfinite(total) or abs(total - 1.0) > 1.0e-4:
        raise ClusterAssemblyBuildError(
            f"Assembly influence weights do not sum to one: {total}"
        )
    anchor = lowest_common_ancestor(
        names,
        skeleton_snapshot,
        skeleton_by_name,
    )
    declared = binding.get("anchor_bone")
    if declared and declared != anchor:
        raise ClusterAssemblyBuildError(
            f"Assembly binding anchor mismatch: {declared} != {anchor}"
        )
    for name in names:
        if anchor not in ancestor_chain(
            name,
            skeleton_snapshot,
            skeleton_by_name,
        ):
            raise ClusterAssemblyBuildError(
                f"{anchor} is not an ancestor of binding bone {name}"
            )
    return {
        "anchor_bone": anchor,
        "influence_bones": names,
        "weight_sum": total,
    }


def _joint_name(row):
    return str(
        row.get("JointName")
        or row.get("joint_name")
        or row.get("name")
        or ""
    )


def validate_wind_json_against_skeleton(
    wind_json_or_payload,
    skeleton_snapshot,
    bindings=(),
):
    """Resolve every generated wind joint against the final Skeleton.

    DynamicWind's public JSON stores names, while its imported asset data stores
    final reference-skeleton indices.  The returned ``resolved_joints`` is the
    explicit name/index proof required at this boundary.
    """
    if isinstance(wind_json_or_payload, (str, os.PathLike, Path)):
        wind_path = Path(wind_json_or_payload)
        if not wind_path.is_file():
            raise ClusterAssemblyBuildError(
                f"generated DynamicWind JSON is missing: {wind_path}"
            )
        payload = json.loads(wind_path.read_text(encoding="utf-8"))
        fingerprint = file_fingerprint(wind_path)
    else:
        payload = wind_json_or_payload
        fingerprint = None
    if not isinstance(payload, dict):
        raise ClusterAssemblyBuildError("DynamicWind JSON root is not an object")
    checked, by_name = _skeleton_maps(skeleton_snapshot)
    declared_skeleton = payload.get("SkeletonContract")
    if not isinstance(declared_skeleton, dict):
        raise ClusterAssemblyBuildError(
            "DynamicWind JSON has no final SkeletonContract"
        )
    if int(declared_skeleton.get("SchemaVersion", -1)) != 2:
        raise ClusterAssemblyBuildError(
            "DynamicWind SkeletonContract is not schema version 2"
        )
    if int(declared_skeleton.get("BoneCount", -1)) != checked["bone_count"]:
        raise ClusterAssemblyBuildError(
            "DynamicWind SkeletonContract bone count does not match final Skeleton"
        )
    declared_identity = list(declared_skeleton.get("Bones") or [])
    if len(declared_identity) != checked["bone_count"]:
        raise ClusterAssemblyBuildError(
            "DynamicWind SkeletonContract lacks complete bone identity records"
        )
    for expected, actual in zip(checked["bones"], declared_identity):
        if (
            str(actual.get("BoneName") or "") != expected["name"]
            or int(actual.get("BoneIndex", -1)) != expected["index"]
            or int(actual.get("ParentIndex", -2)) != expected["parent_index"]
        ):
            raise ClusterAssemblyBuildError(
                "DynamicWind SkeletonContract name/index/parent mismatch at "
                f"bone {expected['index']} ({expected['name']})"
            )
    declared_hash = str(
        declared_skeleton.get("BoneNameIndexParentSha1") or ""
    ).casefold()
    if declared_hash != checked["bone_name_index_parent_sha1"]:
        raise ClusterAssemblyBuildError(
            "DynamicWind SkeletonContract identity hash mismatch"
        )
    import_root = declared_skeleton.get("ImportRoot")
    root = checked["bones"][0]
    if not isinstance(import_root, dict) or (
        str(import_root.get("BoneName") or "") != root["name"]
        or int(import_root.get("BoneIndex", -1)) != root["index"]
        or int(import_root.get("ParentIndex", -2)) != root["parent_index"]
    ):
        raise ClusterAssemblyBuildError(
            "DynamicWind SkeletonContract ImportRoot does not match the final Skeleton"
        )
    joints = list(payload.get("Joints") or payload.get("joints") or [])
    if not joints:
        raise ClusterAssemblyBuildError("DynamicWind JSON contains no joints")
    groups = list(
        payload.get("SimulationGroups")
        or payload.get("simulation_groups")
        or []
    )
    resolved = []
    seen = set()
    for row in joints:
        name = _joint_name(row)
        if not name or name in seen:
            raise ClusterAssemblyBuildError(
                f"DynamicWind joint name is missing or duplicated: {name!r}"
            )
        bone = by_name.get(name)
        if bone is None:
            raise ClusterAssemblyBuildError(
                f"DynamicWind joint is missing from final skeleton: {name}"
            )
        group_index = row.get(
            "SimulationGroupIndex",
            row.get("simulation_group_index"),
        )
        if group_index is None:
            raise ClusterAssemblyBuildError(
                f"DynamicWind joint has no simulation group: {name}"
            )
        group_index = int(group_index)
        if group_index < 0 or group_index >= len(groups):
            raise ClusterAssemblyBuildError(
                f"DynamicWind group index is out of range for {name}: {group_index}"
            )
        declared_index = row.get("BoneIndex", row.get("bone_index"))
        declared_parent = row.get("ParentIndex", row.get("parent_index"))
        if declared_index is None or declared_parent is None:
            raise ClusterAssemblyBuildError(
                f"DynamicWind joint has no final bone index/parent: {name}"
            )
        if int(declared_index) != bone["index"]:
            raise ClusterAssemblyBuildError(
                f"DynamicWind bone index mismatch for {name}: "
                f"{declared_index} != {bone['index']}"
            )
        if int(declared_parent) != bone["parent_index"]:
            raise ClusterAssemblyBuildError(
                f"DynamicWind parent index mismatch for {name}: "
                f"{declared_parent} != {bone['parent_index']}"
            )
        resolved.append(
            {
                "joint_name": name,
                "bone_index": bone["index"],
                "simulation_group_index": group_index,
            }
        )
        seen.add(name)
    binding_reports = [
        validate_binding_hierarchy(
            binding,
            checked,
            wind_bones=seen,
            skeleton_by_name=by_name,
        )
        for binding in bindings
    ]
    return {
        "status": "ok",
        "contract": "final_skeleton_v2",
        "skeleton_sha256": checked["sha256"],
        "skeleton_identity_sha1": checked["bone_name_index_parent_sha1"],
        "wind_json": fingerprint,
        "joint_count": len(resolved),
        "simulation_group_count": len(groups),
        "resolved_joints": resolved,
        "binding_hierarchy": binding_reports,
    }


def fit_trs_transform(source_points, target_points):
    """Fit the best Blender-space TRS while reporting affine residuals."""
    try:
        import numpy as np
    except ImportError as exc:  # pragma: no cover - Blender bundles NumPy.
        raise ClusterAssemblyBuildError("NumPy is required for Assembly fitting") from exc
    source = np.asarray(source_points, dtype=np.float64)
    target = np.asarray(target_points, dtype=np.float64)
    if source.shape != target.shape or source.ndim != 2 or source.shape[1] != 3:
        raise ClusterAssemblyBuildError("TRS fit point arrays must both be Nx3")
    if source.shape[0] < 3:
        raise ClusterAssemblyBuildError("TRS fit needs at least three vertices")
    augmented = np.concatenate(
        [source, np.ones((source.shape[0], 1), dtype=np.float64)], axis=1
    )
    coefficients, _, _, _ = np.linalg.lstsq(augmented, target, rcond=None)
    affine = coefficients[:3, :]
    affine_translation = coefficients[3, :]
    affine_prediction = source @ affine + affine_translation

    unsigned_scale = np.linalg.norm(affine, axis=1)
    unsigned_scale[unsigned_scale < 1.0e-12] = 1.0
    normalized_rows = affine / unsigned_scale[:, None]
    best = None
    for signs in itertools.product((-1.0, 1.0), repeat=3):
        signed_rows = np.diag(signs) @ normalized_rows
        left, _, right = np.linalg.svd(signed_rows)
        rotation_rows = left @ right
        if np.linalg.det(rotation_rows) < 0.0:
            left[:, -1] *= -1.0
            rotation_rows = left @ right
        scale = unsigned_scale * np.asarray(signs)
        trs_affine = np.diag(scale) @ rotation_rows
        error = float(np.linalg.norm(affine - trs_affine))
        negative_scales = sum(value < 0.0 for value in scale)
        score = error + 1.0e-8 * max(float(np.linalg.norm(affine)), 1.0) * negative_scales
        if best is None or score < best[0]:
            best = (score, scale, rotation_rows, trs_affine)
    _, scale, rotation_rows, trs_affine = best
    translation = np.mean(target - source @ trs_affine, axis=0)
    prediction = source @ trs_affine + translation
    diagonal = max(
        float(np.linalg.norm(np.max(target, axis=0) - np.min(target, axis=0))),
        1.0e-9,
    )
    affine_rms = float(
        np.sqrt(np.mean(np.sum((target - affine_prediction) ** 2, axis=1)))
    )
    trs_rms = float(np.sqrt(np.mean(np.sum((target - prediction) ** 2, axis=1))))
    quaternion = _rotation_matrix_to_quaternion(rotation_rows.T.tolist())
    return {
        "translation": [float(value) for value in translation],
        "rotation_xyzw": quaternion,
        "scale": [float(value) for value in scale],
        "affine_relative_rms": affine_rms / diagonal,
        "trs_relative_rms": trs_rms / diagonal,
        "shear_relative_norm": float(
            np.linalg.norm(affine - trs_affine)
            / max(float(np.linalg.norm(affine)), 1.0e-9)
        ),
    }


def fit_uniform_similarity_transform(
    source_points,
    target_points,
    source_attachment=None,
    target_attachment=None,
):
    """Fit a stable 3D rigid rotation plus uniform scale for planar cards.

    A full affine/TRS decomposition is underdetermined when every source point
    lies on a plane.  Normalized SpeedTree plans are intentionally planar, so
    use orthogonal Procrustes/Kabsch fitting and one uniform scale.  This keeps
    the normalized attachment origin meaningful instead of inventing enormous
    scale on the plane normal.  When both attachment points are supplied, fit
    root-relative vectors and constrain translation to map that authored pivot
    exactly instead of allowing deformation residuals to move the stem root.
    """
    try:
        import numpy as np
    except ImportError as exc:  # pragma: no cover - Blender bundles NumPy.
        raise ClusterAssemblyBuildError(
            "NumPy is required for Assembly similarity fitting"
        ) from exc
    source = np.asarray(source_points, dtype=np.float64)
    target = np.asarray(target_points, dtype=np.float64)
    if source.shape != target.shape or source.ndim != 2 or source.shape[1] != 3:
        raise ClusterAssemblyBuildError(
            "similarity fit point arrays must both be Nx3"
        )
    if source.shape[0] < 3:
        raise ClusterAssemblyBuildError(
            "similarity fit needs at least three vertices"
        )
    if (source_attachment is None) != (target_attachment is None):
        raise ClusterAssemblyBuildError(
            "similarity fit requires both source and target attachments"
        )
    attachment_locked = source_attachment is not None
    if attachment_locked:
        source_origin = np.asarray(source_attachment, dtype=np.float64)
        target_origin = np.asarray(target_attachment, dtype=np.float64)
        if (
            source_origin.shape != (3,)
            or target_origin.shape != (3,)
            or not np.all(np.isfinite(source_origin))
            or not np.all(np.isfinite(target_origin))
        ):
            raise ClusterAssemblyBuildError(
                "similarity fit attachment points must both be finite 3D vectors"
            )
    else:
        source_origin = np.mean(source, axis=0)
        target_origin = np.mean(target, axis=0)
    source_centered = source - source_origin
    target_centered = target - target_origin
    source_energy = float(np.sum(source_centered * source_centered))
    if source_energy <= 1.0e-18:
        raise ClusterAssemblyBuildError(
            "similarity fit source points have no spatial extent"
        )
    left, singular, right = np.linalg.svd(
        source_centered.T @ target_centered
    )
    correction = np.ones(3, dtype=np.float64)
    if np.linalg.det(left @ right) < 0.0:
        correction[-1] = -1.0
    rotation_rows = left @ np.diag(correction) @ right
    scale_value = float(np.sum(singular * correction) / source_energy)
    if not np.isfinite(scale_value) or abs(scale_value) <= 1.0e-12:
        raise ClusterAssemblyBuildError(
            "similarity fit produced an invalid uniform scale"
        )
    linear = scale_value * rotation_rows
    translation = target_origin - source_origin @ linear
    prediction = source @ linear + translation
    diagonal = max(
        float(np.linalg.norm(np.max(target, axis=0) - np.min(target, axis=0))),
        1.0e-9,
    )
    rms = float(
        np.sqrt(np.mean(np.sum((target - prediction) ** 2, axis=1)))
    )
    augmented = np.concatenate(
        [source, np.ones((source.shape[0], 1), dtype=np.float64)],
        axis=1,
    )
    coefficients, _, _, _ = np.linalg.lstsq(
        augmented,
        target,
        rcond=None,
    )
    affine = coefficients[:3, :]
    affine_translation = coefficients[3, :]
    affine_prediction = source @ affine + affine_translation
    affine_rms = float(
        np.sqrt(
            np.mean(
                np.sum((target - affine_prediction) ** 2, axis=1)
            )
        )
    )
    pivot_error = None
    if attachment_locked:
        pivot_prediction = source_origin @ linear + translation
        pivot_error = float(np.linalg.norm(target_origin - pivot_prediction))
    quaternion = _rotation_matrix_to_quaternion(rotation_rows.T.tolist())
    source_fit_rank = int(np.linalg.matrix_rank(source_centered))
    affine_linear_delta = float(
        np.linalg.norm(affine - linear)
        / max(float(np.linalg.norm(affine)), 1.0e-9)
    )
    result = {
        "translation": [float(value) for value in translation],
        "rotation_xyzw": quaternion,
        "scale": [scale_value, scale_value, scale_value],
        "affine_relative_rms": affine_rms / diagonal,
        "similarity_relative_rms": rms / diagonal,
        "trs_relative_rms": rms / diagonal,
        # A planar source has no observable normal-axis affine component.
        # Keep its predictive residual, but do not present the decomposed
        # linear delta as a trustworthy shear measurement.
        "shear_relative_norm": (
            affine_linear_delta if source_fit_rank == 3 else None
        ),
        "affine_linear_delta_relative_norm": affine_linear_delta,
        "affine_diagnostic_only": source_fit_rank < 3,
        "source_fit_rank": source_fit_rank,
        "fit_mode": (
            "uniform_similarity_3d_attachment_locked"
            if attachment_locked
            else "uniform_similarity_3d"
        ),
    }
    if pivot_error is not None:
        result["attachment_pivot_error"] = pivot_error
        result["attachment_pivot_error_scope"] = (
            "normalized_plan_attachment"
        )
    return result


def derive_endpoint_uniform_similarity_transform(
    source_points,
    target_points,
    source_attachment,
    target_attachment,
):
    """Place a plan from its authored pivot and surviving root-to-tip frame.

    Translation is exactly the authored target attachment.  Rotation is the
    minimum rotation that aligns the plan's pivot-to-tip vector, preserving
    the remaining plan roll/twist, and scale is the positive endpoint-length
    ratio.  All-point residuals are diagnostics/gates only; they do not fit or
    move the placement.
    """
    try:
        import numpy as np
    except ImportError as exc:  # pragma: no cover - Blender bundles NumPy.
        raise ClusterAssemblyBuildError(
            "NumPy is required for Assembly endpoint placement"
        ) from exc
    source = np.asarray(source_points, dtype=np.float64)
    target = np.asarray(target_points, dtype=np.float64)
    source_origin = np.asarray(source_attachment, dtype=np.float64)
    target_origin = np.asarray(target_attachment, dtype=np.float64)
    if (
        source.shape != target.shape
        or source.ndim != 2
        or source.shape[1] != 3
        or source.shape[0] < 3
        or source_origin.shape != (3,)
        or target_origin.shape != (3,)
        or not np.all(np.isfinite(source))
        or not np.all(np.isfinite(target))
        or not np.all(np.isfinite(source_origin))
        or not np.all(np.isfinite(target_origin))
    ):
        raise ClusterAssemblyBuildError(
            "endpoint Assembly placement requires finite matching Nx3 points"
        )
    source_lengths = np.linalg.norm(source - source_origin, axis=1)
    maximum_source_length = float(np.max(source_lengths))
    if maximum_source_length <= 1.0e-12:
        raise ClusterAssemblyBuildError(
            "endpoint Assembly placement source has no root-to-tip extent"
        )
    endpoint_band = max(maximum_source_length * 1.0e-6, 1.0e-9)
    endpoint_indices = np.flatnonzero(
        source_lengths >= maximum_source_length - endpoint_band
    )
    source_endpoint = np.mean(source[endpoint_indices], axis=0)
    target_endpoint = np.mean(target[endpoint_indices], axis=0)
    source_vector = source_endpoint - source_origin
    target_vector = target_endpoint - target_origin
    source_length = float(np.linalg.norm(source_vector))
    target_length = float(np.linalg.norm(target_vector))
    if source_length <= 1.0e-12 or target_length <= 1.0e-12:
        raise ClusterAssemblyBuildError(
            "endpoint Assembly placement has a degenerate root-to-tip vector"
        )
    source_direction = source_vector / source_length
    target_direction = target_vector / target_length
    cross = np.cross(source_direction, target_direction)
    sine = float(np.linalg.norm(cross))
    cosine = float(np.clip(
        np.dot(source_direction, target_direction), -1.0, 1.0
    ))
    if sine > 1.0e-12:
        axis = cross / sine
        x, y, z = [float(value) for value in axis]
        skew = np.asarray([
            [0.0, -z, y],
            [z, 0.0, -x],
            [-y, x, 0.0],
        ], dtype=np.float64)
        rotation_columns = (
            np.eye(3)
            + skew * sine
            + (skew @ skew) * (1.0 - cosine)
        )
    elif cosine >= 0.0:
        rotation_columns = np.eye(3, dtype=np.float64)
    else:
        # A 180-degree alignment has no unique axis.  Pick the canonical axis
        # least aligned with the source direction, then project it onto the
        # perpendicular plane.  This is deterministic and preserves no hidden
        # iteration-order state.
        canonical = np.eye(3)[
            int(np.argmin(np.abs(source_direction)))
        ]
        axis = canonical - source_direction * float(
            np.dot(canonical, source_direction)
        )
        axis /= np.linalg.norm(axis)
        rotation_columns = 2.0 * np.outer(axis, axis) - np.eye(3)
    rotation_rows = rotation_columns.T
    scale_value = target_length / source_length
    linear = scale_value * rotation_rows
    translation = target_origin - source_origin @ linear
    prediction = source @ linear + translation
    diagonal = max(
        float(np.linalg.norm(np.max(target, axis=0) - np.min(target, axis=0))),
        1.0e-9,
    )
    rms = float(np.sqrt(np.mean(np.sum((target - prediction) ** 2, axis=1))))
    augmented = np.concatenate(
        [source, np.ones((source.shape[0], 1), dtype=np.float64)], axis=1
    )
    coefficients, _, _, _ = np.linalg.lstsq(augmented, target, rcond=None)
    affine_prediction = source @ coefficients[:3, :] + coefficients[3, :]
    affine_rms = float(np.sqrt(np.mean(
        np.sum((target - affine_prediction) ** 2, axis=1)
    )))
    pivot_prediction = source_origin @ linear + translation
    endpoint_prediction = source_endpoint @ linear + translation
    source_fit_rank = int(np.linalg.matrix_rank(source - source_origin))
    return {
        "translation": [float(value) for value in translation],
        "rotation_xyzw": _rotation_matrix_to_quaternion(
            rotation_columns.tolist()
        ),
        "scale": [scale_value, scale_value, scale_value],
        "affine_relative_rms": affine_rms / diagonal,
        "similarity_relative_rms": rms / diagonal,
        "trs_relative_rms": rms / diagonal,
        "shear_relative_norm": None,
        "affine_linear_delta_relative_norm": None,
        "affine_diagnostic_only": True,
        "source_fit_rank": source_fit_rank,
        "fit_mode": (
            "uniform_similarity_3d_attachment_locked_endpoint_frame"
        ),
        "construction_mode": (
            "authored_absolute_pivot_plus_surviving_root_tip_frame_v1"
        ),
        "attachment_pivot_error": float(
            np.linalg.norm(target_origin - pivot_prediction)
        ),
        "attachment_pivot_error_scope": (
            "authored_spm_node_absolute_translation"
        ),
        "endpoint_error": float(
            np.linalg.norm(target_endpoint - endpoint_prediction)
        ),
        "endpoint_evidence": {
            "policy": "mean_of_farthest_surviving_source_distance_band_v1",
            "band_meters": endpoint_band,
            "correspondence_indices": [
                int(value) for value in endpoint_indices.tolist()
            ],
            "source_endpoint": [float(value) for value in source_endpoint],
            "target_endpoint": [float(value) for value in target_endpoint],
            "source_length": source_length,
            "target_length": target_length,
            "uniform_scale": scale_value,
            "rotation_policy": "minimum_root_tip_vector_rotation_preserve_plan_roll",
        },
    }


def attachment_locked_safe_transform(target_attachment, diagnostic):
    """Last-resort rigid transform that keeps export attached and finite."""
    try:
        translation = [float(value) for value in target_attachment]
    except (TypeError, ValueError) as exc:
        raise ClusterAssemblyBuildError(
            "safe Assembly fallback has no finite attachment"
        ) from exc
    if len(translation) != 3 or any(
        not math.isfinite(value) for value in translation
    ):
        raise ClusterAssemblyBuildError(
            "safe Assembly fallback has no finite attachment"
        )
    return {
        "translation": translation,
        "rotation_xyzw": [0.0, 0.0, 0.0, 1.0],
        "scale": [1.0, 1.0, 1.0],
        "affine_relative_rms": 1.0,
        "similarity_relative_rms": 1.0,
        "trs_relative_rms": 1.0,
        "shear_relative_norm": None,
        "affine_linear_delta_relative_norm": None,
        "affine_diagnostic_only": True,
        "source_fit_rank": 0,
        "fit_mode": (
            "uniform_similarity_3d_attachment_locked_safe_fallback"
        ),
        "construction_mode": (
            "authored_or_render_attachment_identity_rotation_unit_scale_v1"
        ),
        "attachment_pivot_error": 0.0,
        "attachment_pivot_error_scope": "attachment_translation_hard_lock",
        "placement_quality_warning": str(diagnostic),
    }


def gate_assembly_transform_residuals(
    transform,
    evidence,
    threshold=MAX_ASSEMBLY_RESIDUAL_RELATIVE_RMS,
    block_geometry=True,
):
    """Hard-gate attachment; optionally downgrade rigid-fit error to warning."""
    try:
        limit = float(threshold)
        pivot_error = float(transform.get("attachment_pivot_error"))
        metrics = {
            name: float(transform.get(name))
            for name in (
                "similarity_relative_rms",
                "trs_relative_rms",
                "affine_relative_rms",
            )
        }
    except (TypeError, ValueError) as exc:
        raise ClusterAssemblyBuildError(
            "Assembly residual ready gate has missing or invalid metrics: "
            + _canonical_json(evidence or {})
        ) from exc
    if (
        not math.isfinite(limit)
        or limit <= 0.0
        or not math.isfinite(pivot_error)
        or any(not math.isfinite(value) for value in metrics.values())
    ):
        raise ClusterAssemblyBuildError(
            "Assembly residual ready gate has non-finite evidence: "
            + _canonical_json({
                "threshold": limit,
                "metrics": metrics,
                "attachment_pivot_error_meters": pivot_error,
                "evidence": evidence or {},
            })
        )
    failed = {
        name: value for name, value in metrics.items()
        if value > limit
    }
    pivot_failed = pivot_error > MAX_ASSEMBLY_PIVOT_ERROR_METERS
    geometry_blocked = bool(failed and block_geometry)
    gate = {
        "status": (
            "blocked"
            if pivot_failed or geometry_blocked
            else "warning" if failed else "pass"
        ),
        "policy": "all_recorded_relative_rms_at_or_below_global_threshold_v1",
        "threshold": limit,
        "metrics": metrics,
        "maximum_metric": max(metrics, key=metrics.get),
        "maximum_relative_rms": max(metrics.values()),
        "attachment_pivot_error_meters": pivot_error,
        "attachment_pivot_error_threshold_meters": (
            MAX_ASSEMBLY_PIVOT_ERROR_METERS
        ),
        "geometry_residual_blocking": bool(block_geometry),
        "evidence": deepcopy(evidence or {}),
    }
    if failed:
        gate["failed_metrics"] = failed
    if pivot_failed:
        gate["attachment_pivot_failed"] = True
    if geometry_blocked or pivot_failed:
        raise ClusterAssemblyBuildError(
            "Assembly residual ready gate blocked placement: "
            + _canonical_json(gate)
        )
    transform["residual_gate"] = gate
    return gate


def validate_persisted_residual_gate(
    transform,
    expected_threshold=MAX_ASSEMBLY_RESIDUAL_RELATIVE_RMS,
):
    """Re-evaluate an additive v1 gate without trusting its saved verdict."""
    recorded = deepcopy((transform or {}).get("residual_gate") or {})
    if (
        recorded.get("status") not in {"pass", "warning"}
        or recorded.get("policy")
        != "all_recorded_relative_rms_at_or_below_global_threshold_v1"
    ):
        raise ClusterAssemblyBuildError(
            "normalized prototype instance has no passing residual ready gate"
        )
    try:
        recorded_threshold = float(recorded.get("threshold"))
    except (TypeError, ValueError) as exc:
        raise ClusterAssemblyBuildError(
            "normalized prototype residual ready gate threshold is invalid"
        ) from exc
    if abs(recorded_threshold - float(expected_threshold)) > 1.0e-15:
        raise ClusterAssemblyBuildError(
            "normalized prototype residual ready gate threshold changed"
        )
    checked = deepcopy(transform)
    checked.pop("residual_gate", None)
    recomputed = gate_assembly_transform_residuals(
        checked,
        recorded.get("evidence") or {},
        threshold=expected_threshold,
        block_geometry=bool(recorded.get("geometry_residual_blocking")),
    )
    for key in (
        "status",
        "policy",
        "threshold",
        "metrics",
        "maximum_metric",
        "maximum_relative_rms",
        "attachment_pivot_error_meters",
        "attachment_pivot_error_threshold_meters",
        "geometry_residual_blocking",
        "evidence",
    ):
        if _canonical_json(recorded.get(key)) != _canonical_json(
            recomputed.get(key)
        ):
            raise ClusterAssemblyBuildError(
                "normalized prototype residual ready gate evidence drifted"
            )
    return recomputed


def _assembly_fit_summary(bindings, fit_mode):
    rows = list(bindings or [])
    if not rows:
        return {"fit_mode": fit_mode}
    transforms = [row.get("transform") or {} for row in rows]
    similarity = [
        float(transform.get(
            "similarity_relative_rms",
            transform["trs_relative_rms"],
        ))
        for transform in transforms
    ]
    trs = [float(transform["trs_relative_rms"]) for transform in transforms]
    affine = [
        float(transform["affine_relative_rms"])
        for transform in transforms
    ]
    gates = [transform.get("residual_gate") for transform in transforms]
    gates_present = [gate for gate in gates if gate is not None]
    if gates_present and len(gates_present) != len(gates):
        raise ClusterAssemblyBuildError(
            "Assembly fit summary mixes gated and legacy bindings"
        )
    if any(
        gate.get("status") not in {"pass", "warning"}
        for gate in gates_present
    ):
        raise ClusterAssemblyBuildError(
            "Assembly fit summary cannot include an ungated ready binding"
        )
    summary = {
        "fit_mode": fit_mode,
        "similarity_relative_rms_count": len(similarity),
        "similarity_relative_rms_median": statistics.median(similarity),
        "similarity_relative_rms_max": max(similarity),
        "trs_relative_rms_median": statistics.median(trs),
        "trs_relative_rms_max": max(trs),
        "affine_relative_rms_median": statistics.median(affine),
        "affine_relative_rms_max": max(affine),
        "placement_sources": dict(sorted(Counter(
            str(transform.get("placement_source") or "unspecified")
            for transform in transforms
        ).items())),
    }
    if gates_present:
        summary["residual_ready_gate"] = {
            "status": (
                "warning"
                if any(gate.get("status") == "warning" for gate in gates_present)
                else "pass"
            ),
            "threshold": MAX_ASSEMBLY_RESIDUAL_RELATIVE_RMS,
            "binding_count": len(gates_present),
            "maximum_relative_rms": max(
                float(gate["maximum_relative_rms"])
                for gate in gates_present
            ),
        }
    return summary


def compose_similarity_with_relative_matrix(transform, relative_matrix):
    """Compose a fitted card transform with one normalized composite subpart.

    ``relative_matrix`` is the Blender column-vector transform from the
    subpart's normalized local space into the shared card/attachment space.
    The fitted card transform is a positive uniform similarity, so the result
    remains a positive uniform similarity suitable for a native Assembly
    binding.
    """
    try:
        import numpy as np
    except ImportError as exc:  # pragma: no cover - Blender bundles NumPy.
        raise ClusterAssemblyBuildError(
            "NumPy is required for composite Assembly transforms"
        ) from exc
    translation = np.asarray(transform.get("translation"), dtype=np.float64)
    rotation = np.asarray(transform.get("rotation_xyzw"), dtype=np.float64)
    scale = np.asarray(transform.get("scale"), dtype=np.float64)
    relative = np.asarray(relative_matrix, dtype=np.float64)
    if (
        translation.shape != (3,)
        or rotation.shape != (4,)
        or scale.shape != (3,)
        or relative.shape != (4, 4)
        or not np.all(np.isfinite(relative))
    ):
        raise ClusterAssemblyBuildError(
            "composite Assembly transform contract is malformed"
        )
    if (
        np.min(scale) <= 0.0
        or float(np.max(scale) - np.min(scale)) > max(float(np.max(scale)), 1.0) * 1.0e-6
    ):
        raise ClusterAssemblyBuildError(
            "composite Assembly card fit is not a positive uniform similarity"
        )
    x, y, z, w = [float(value) for value in rotation]
    quaternion_length = math.sqrt(x * x + y * y + z * z + w * w)
    if quaternion_length <= 1.0e-12:
        raise ClusterAssemblyBuildError(
            "composite Assembly card fit has an invalid rotation"
        )
    x, y, z, w = [value / quaternion_length for value in (x, y, z, w)]
    rotation_matrix = np.asarray([
        [1.0 - 2.0 * (y * y + z * z), 2.0 * (x * y - z * w), 2.0 * (x * z + y * w)],
        [2.0 * (x * y + z * w), 1.0 - 2.0 * (x * x + z * z), 2.0 * (y * z - x * w)],
        [2.0 * (x * z - y * w), 2.0 * (y * z + x * w), 1.0 - 2.0 * (x * x + y * y)],
    ], dtype=np.float64)
    card_matrix = np.eye(4, dtype=np.float64)
    card_matrix[:3, :3] = rotation_matrix * float(scale[0])
    card_matrix[:3, 3] = translation
    if (
        np.max(np.abs(relative[3, :] - np.asarray([0.0, 0.0, 0.0, 1.0]))) > 1.0e-6
        or float(np.linalg.det(relative[:3, :3])) <= 0.0
    ):
        raise ClusterAssemblyBuildError(
            "composite subpart relative matrix is mirrored or non-affine"
        )
    relative_scales = np.linalg.norm(relative[:3, :3], axis=0)
    if (
        np.min(relative_scales) <= 1.0e-12
        or float(np.max(relative_scales) - np.min(relative_scales)) > 1.0e-6
    ):
        raise ClusterAssemblyBuildError(
            "composite subpart relative matrix is not rigid/uniform"
        )
    relative_rotation = relative[:3, :3] / relative_scales
    if np.max(np.abs(relative_rotation.T @ relative_rotation - np.eye(3))) > 1.0e-6:
        raise ClusterAssemblyBuildError(
            "composite subpart relative matrix contains shear"
        )
    composed = card_matrix @ relative
    composed_scales = np.linalg.norm(composed[:3, :3], axis=0)
    if (
        np.min(composed_scales) <= 1.0e-12
        or float(np.max(composed_scales) - np.min(composed_scales)) > max(float(np.max(composed_scales)), 1.0) * 1.0e-6
    ):
        raise ClusterAssemblyBuildError(
            "composed composite subpart transform is not uniform"
        )
    composed_rotation = composed[:3, :3] / composed_scales
    if float(np.linalg.det(composed_rotation)) <= 0.0:
        raise ClusterAssemblyBuildError(
            "composed composite subpart transform is mirrored"
        )
    quaternion = _rotation_matrix_to_quaternion(composed_rotation.tolist())
    result = deepcopy(transform)
    result.update({
        "translation": [float(value) for value in composed[:3, 3]],
        "rotation_xyzw": quaternion,
        "scale": [float(value) for value in composed_scales],
        "fit_mode": (
            "uniform_similarity_3d_attachment_locked_composite_subpart"
            if "attachment_locked"
            in str(transform.get("fit_mode") or "")
            else "uniform_similarity_3d_composite_subpart"
        ),
        "composite_relative_matrix": [
            [float(value) for value in row] for row in relative
        ],
    })
    if result.get("attachment_pivot_error") is not None:
        result["attachment_pivot_error_scope"] = (
            "parent_card_attachment_before_composite"
        )
    return result


def _rotation_matrix_to_quaternion(matrix):
    m = matrix
    trace = m[0][0] + m[1][1] + m[2][2]
    if trace > 0.0:
        root = math.sqrt(trace + 1.0) * 2.0
        w = 0.25 * root
        x = (m[2][1] - m[1][2]) / root
        y = (m[0][2] - m[2][0]) / root
        z = (m[1][0] - m[0][1]) / root
    elif m[0][0] > m[1][1] and m[0][0] > m[2][2]:
        root = math.sqrt(1.0 + m[0][0] - m[1][1] - m[2][2]) * 2.0
        w = (m[2][1] - m[1][2]) / root
        x = 0.25 * root
        y = (m[0][1] + m[1][0]) / root
        z = (m[0][2] + m[2][0]) / root
    elif m[1][1] > m[2][2]:
        root = math.sqrt(1.0 + m[1][1] - m[0][0] - m[2][2]) * 2.0
        w = (m[0][2] - m[2][0]) / root
        x = (m[0][1] + m[1][0]) / root
        y = 0.25 * root
        z = (m[1][2] + m[2][1]) / root
    else:
        root = math.sqrt(1.0 + m[2][2] - m[0][0] - m[1][1]) * 2.0
        w = (m[1][0] - m[0][1]) / root
        x = (m[0][2] + m[2][0]) / root
        y = (m[1][2] + m[2][1]) / root
        z = 0.25 * root
    length = math.sqrt(x * x + y * y + z * z + w * w)
    return [x / length, y / length, z / length, w / length]


def _fbx_coordinate_tolerance(coordinates):
    points = [tuple(float(value) for value in point) for point in coordinates]
    if not points:
        return MIN_FBX_COORDINATE_TOLERANCE_METERS
    spans = [
        max(point[axis] for point in points)
        - min(point[axis] for point in points)
        for axis in range(3)
    ]
    return max(
        max(spans) * 2.0e-6,
        MIN_FBX_COORDINATE_TOLERANCE_METERS,
    )


def _component_groups(mesh, polygon_indices):
    polygon_indices = sorted(set(int(value) for value in polygon_indices))
    if not polygon_indices:
        return []
    polygon_parent = {index: index for index in polygon_indices}

    def polygon_find(value):
        while polygon_parent[value] != value:
            polygon_parent[value] = polygon_parent[polygon_parent[value]]
            value = polygon_parent[value]
        return value

    def polygon_union(left, right):
        left = polygon_find(left)
        right = polygon_find(right)
        if left != right:
            polygon_parent[right] = left

    used_vertices = {
        int(vertex)
        for polygon_index in polygon_indices
        for vertex in mesh.polygons[polygon_index].vertices
    }
    coordinates = {
        index: tuple(float(mesh.vertices[index].co[axis]) for axis in range(3))
        for index in used_vertices
    }
    # Relative to the current role extent, not a species-specific world size.
    # This reconnects seam-split FBX/BWR vertices whose skinning differs only
    # by float noise while remaining far below authored card edge lengths.
    weld_tolerance = _fbx_coordinate_tolerance(coordinates.values())
    weld_tolerance_sq = weld_tolerance * weld_tolerance
    vertex_parent = {index: index for index in used_vertices}

    def vertex_find(value):
        while vertex_parent[value] != value:
            vertex_parent[value] = vertex_parent[vertex_parent[value]]
            value = vertex_parent[value]
        return value

    def vertex_union(left, right):
        left = vertex_find(left)
        right = vertex_find(right)
        if left != right:
            vertex_parent[right] = left

    spatial_vertices = defaultdict(list)
    for vertex, coordinate in coordinates.items():
        cell = tuple(
            int(math.floor(value / weld_tolerance))
            for value in coordinate
        )
        for x in range(cell[0] - 1, cell[0] + 2):
            for y in range(cell[1] - 1, cell[1] + 2):
                for z in range(cell[2] - 1, cell[2] + 2):
                    for other in spatial_vertices.get((x, y, z), ()):
                        distance_sq = sum(
                            (coordinate[axis] - coordinates[other][axis]) ** 2
                            for axis in range(3)
                        )
                        if distance_sq <= weld_tolerance_sq:
                            vertex_union(vertex, other)
        spatial_vertices[cell].append(vertex)

    # Reconnect FBX seam-split faces only across a geometric edge.  A single
    # shared attachment point is not connectivity: several separately
    # rendered cards legitimately meet at the same authored pivot.
    coordinate_pair_owners = {}
    for polygon_index in polygon_indices:
        coordinate_vertices = sorted({
            vertex_find(int(vertex))
            for vertex in mesh.polygons[polygon_index].vertices
        })
        for pair in itertools.combinations(coordinate_vertices, 2):
            previous = coordinate_pair_owners.setdefault(pair, polygon_index)
            polygon_union(polygon_index, previous)
    groups = defaultdict(lambda: {"vertices": set(), "polygons": []})
    for polygon_index in polygon_indices:
        root = polygon_find(polygon_index)
        row = groups[root]
        row["polygons"].append(polygon_index)
        row["vertices"].update(int(value) for value in mesh.polygons[polygon_index].vertices)
    result = []
    for row in groups.values():
        result.append(
            {
                "vertices": sorted(row["vertices"]),
                "polygons": sorted(row["polygons"]),
            }
        )
    return sorted(result, key=lambda row: row["polygons"][0])


def _exact_index_component_groups(mesh, polygon_indices):
    """Split polygons only when they share a real mesh edge.

    The normal component pass welds seam-split FBX vertices by position.  Two
    independent cards can nevertheless touch along the same authored pivot
    edge and be merged by that weld.  This narrower view is used only as a
    recovery candidate when the welded component matches no normalized plan;
    every recovered subcomponent must independently match a plan.
    """
    indices = sorted(set(int(value) for value in polygon_indices))
    if not indices:
        return []
    parent = {index: index for index in indices}

    def find(value):
        while parent[value] != value:
            parent[value] = parent[parent[value]]
            value = parent[value]
        return value

    def union(left, right):
        left = find(left)
        right = find(right)
        if left != right:
            parent[right] = left

    edge_owners = {}
    for polygon_index in indices:
        vertices = sorted(
            set(int(value) for value in mesh.polygons[polygon_index].vertices)
        )
        for edge in itertools.combinations(vertices, 2):
            previous = edge_owners.setdefault(edge, polygon_index)
            union(polygon_index, previous)
    groups = defaultdict(lambda: {"vertices": set(), "polygons": []})
    for polygon_index in indices:
        row = groups[find(polygon_index)]
        row["polygons"].append(polygon_index)
        row["vertices"].update(
            int(value) for value in mesh.polygons[polygon_index].vertices
        )
    return sorted(
        (
            {
                "vertices": sorted(row["vertices"]),
                "polygons": sorted(row["polygons"]),
            }
            for row in groups.values()
        ),
        key=lambda row: row["polygons"][0],
    )


def _component_signature(mesh, component):
    uv_layer = mesh.uv_layers.active
    uv_faces = []
    polygon_sizes = []
    for polygon_index in component["polygons"]:
        polygon = mesh.polygons[polygon_index]
        polygon_sizes.append(len(polygon.vertices))
        if uv_layer is not None:
            uv_faces.append(
                tuple(
                    sorted(
                        (
                            round(float(uv_layer.data[loop].uv.x), 6),
                            round(float(uv_layer.data[loop].uv.y), 6),
                        )
                        for loop in polygon.loop_indices
                    )
                )
            )
    uv_vertices = set()
    if uv_layer is not None:
        for polygon_index in component["polygons"]:
            for loop_index in mesh.polygons[polygon_index].loop_indices:
                uv = uv_layer.data[loop_index].uv
                uv_vertices.add((round(float(uv.x), 6), round(float(uv.y), 6)))
    value = {
        "vertices": len(uv_vertices) if uv_layer is not None else len(component["vertices"]),
        "polygons": len(component["polygons"]),
        "polygon_sizes": sorted(polygon_sizes),
        "uv_faces": sorted(uv_faces),
    }
    return hashlib.sha1(_canonical_json(value).encode("utf-8")).hexdigest()[:16]


def _component_uv_face_counter(mesh, component):
    uv_layer = mesh.uv_layers.active
    if uv_layer is None:
        return None
    faces = []
    for polygon_index in component["polygons"]:
        polygon = mesh.polygons[polygon_index]
        faces.append(tuple(sorted(
            (
                round(float(uv_layer.data[loop].uv.x), 6),
                round(float(uv_layer.data[loop].uv.y), 6),
            )
            for loop in polygon.loop_indices
        )))
    return Counter(faces)


def _uv_vertex_candidates(obj, component):
    uv_layer = obj.data.uv_layers.active
    if uv_layer is None:
        return None
    values = defaultdict(lambda: defaultdict(lambda: {
        "faces": Counter(),
        "neighbors": Counter(),
    }))
    for polygon_index in component["polygons"]:
        polygon = obj.data.polygons[polygon_index]
        loops = list(polygon.loop_indices)
        face_uv = tuple(sorted(
            (
                round(float(uv_layer.data[loop].uv.x), 6),
                round(float(uv_layer.data[loop].uv.y), 6),
            )
            for loop in loops
        ))
        for offset, loop_index in enumerate(loops):
            uv = uv_layer.data[loop_index].uv
            key = (
                round(float(uv.x), 6),
                round(float(uv.y), 6),
            )
            vertex_index = int(obj.data.loops[loop_index].vertex_index)
            record = values[key][vertex_index]
            record["faces"][face_uv] += 1
            for neighbor_offset in (-1, 1):
                neighbor_loop = loops[(offset + neighbor_offset) % len(loops)]
                neighbor_uv = uv_layer.data[neighbor_loop].uv
                record["neighbors"][
                    (
                        round(float(neighbor_uv.x), 6),
                        round(float(neighbor_uv.y), 6),
                    )
                ] += 1
    return {
        key: {
            index: {
                "faces": Counter(record["faces"]),
                "neighbors": Counter(record["neighbors"]),
            }
            for index, record in by_index.items()
        }
        for key, by_index in values.items()
    }


def _coincident_candidate_groups(obj, component, candidates):
    coordinates = {
        int(index): tuple(
            float(value) for value in obj.data.vertices[int(index)].co
        )
        for index in candidates
    }
    component_coordinates = [
        tuple(float(value) for value in obj.data.vertices[index].co)
        for index in component["vertices"]
    ]
    tolerance = _fbx_coordinate_tolerance(component_coordinates)
    groups = []
    for index in sorted(candidates):
        group = next((
            row for row in groups
            if math.dist(coordinates[index], row["coordinate"]) <= tolerance
        ), None)
        if group is None:
            group = {
                "indices": [],
                "coordinate": coordinates[index],
                "faces": Counter(),
                "neighbors": Counter(),
            }
            groups.append(group)
        group["indices"].append(index)
        group["faces"].update(candidates[index]["faces"])
        group["neighbors"].update(candidates[index]["neighbors"])
    for group in groups:
        group["representative"] = min(group["indices"])
    return groups


def _counter_subset(left, right):
    """Match rendered topology after bounded SpeedTree face duplication.

    SpeedTree can emit a two-sided rendered card by duplicating every
    surviving source UV face.  UV identity remains authoritative, while raw
    Counter multiplicity does not.  Keep the conversion bounded so accidental
    stacked geometry cannot silently collapse into one Assembly instance.
    """
    return bool(left) and all(
        int(right.get(key, 0)) > 0
        and int(count)
        <= int(right[key]) * MAX_SPEEDTREE_RENDER_FACE_MULTIPLICITY
        for key, count in left.items()
    )


def _counter_missing_identity_count(source, rendered):
    return sum(
        int(count)
        for key, count in source.items()
        if key not in rendered
    )


def _match_uv_candidate_groups(
    source_obj,
    source_component,
    target_obj,
    target_component,
    key,
    source_candidates,
    target_candidates,
):
    source_groups = _coincident_candidate_groups(
        source_obj, source_component, source_candidates
    )
    target_groups = _coincident_candidate_groups(
        target_obj, target_component, target_candidates
    )
    if len(target_groups) > len(source_groups):
        raise ClusterAssemblyBuildError(
            "normalized plan duplicate UV has fewer source positions than "
            f"the target: uv={key} source={len(source_groups)} "
            f"target={len(target_groups)}"
        )
    if len(source_groups) > 8:
        raise ClusterAssemblyBuildError(
            "normalized plan duplicate UV has too many geometric candidates "
            f"for bounded disambiguation: uv={key} count={len(source_groups)}"
        )
    possibilities = []
    for source_order in itertools.permutations(
        range(len(source_groups)), len(target_groups)
    ):
        score = 0
        valid = True
        for target_index, source_index in enumerate(source_order):
            source = source_groups[source_index]
            target = target_groups[target_index]
            if not (
                _counter_subset(target["faces"], source["faces"])
                and _counter_subset(target["neighbors"], source["neighbors"])
            ):
                valid = False
                break
            score += _counter_missing_identity_count(
                source["faces"], target["faces"]
            )
            score += _counter_missing_identity_count(
                source["neighbors"], target["neighbors"]
            )
        if valid:
            possibilities.append((score, source_order))
    if not possibilities:
        raise ClusterAssemblyBuildError(
            "normalized plan duplicate UV has no topology-compatible "
            f"surviving correspondence: uv={key}"
        )
    possibilities.sort(key=lambda row: (row[0], row[1]))
    best_score = possibilities[0][0]
    best = [row for row in possibilities if row[0] == best_score]
    if len(best) != 1:
        raise ClusterAssemblyBuildError(
            "normalized plan duplicate UV correspondence is ambiguous: "
            f"uv={key} equal_best_assignments={len(best)} score={best_score}"
        )
    assignment = best[0][1]
    return [
        (
            source_groups[source_index]["representative"],
            target_groups[target_index]["representative"],
        )
        for target_index, source_index in enumerate(assignment)
    ], {
        "uv": list(key),
        "source_geometric_candidates": len(source_groups),
        "target_geometric_candidates": len(target_groups),
        "discarded_source_candidates": len(source_groups) - len(target_groups),
        "topology_score": best_score,
    }


def _coincident_uv_vertex_index(obj, component, key):
    uv_layer = obj.data.uv_layers.active
    indices = set()
    for polygon_index in component["polygons"]:
        polygon = obj.data.polygons[polygon_index]
        for loop_index in polygon.loop_indices:
            uv = uv_layer.data[loop_index].uv
            loop_key = (
                round(float(uv.x), 6),
                round(float(uv.y), 6),
            )
            if loop_key == key:
                indices.add(
                    int(obj.data.loops[loop_index].vertex_index)
                )
    if not indices:
        return None
    component_coordinates = [
        tuple(float(value) for value in obj.data.vertices[index].co)
        for index in component["vertices"]
    ]
    tolerance = _fbx_coordinate_tolerance(component_coordinates)
    ordered = sorted(indices)
    reference = tuple(
        float(value) for value in obj.data.vertices[ordered[0]].co
    )
    if any(
        math.dist(
            reference,
            tuple(float(value) for value in obj.data.vertices[index].co),
        )
        > tolerance
        for index in ordered[1:]
    ):
        raise ClusterAssemblyBuildError(
            "attachment pivot UV maps to multiple geometric positions"
        )
    return ordered[0]


def _attachment_vertex_correspondence(
    source_obj,
    source_component,
    target_obj,
    target_component,
    attachment_uv,
):
    """Resolve the authored plan root by UV after any FBX vertex reordering."""
    if (
        not isinstance(attachment_uv, (list, tuple))
        or len(attachment_uv) != 2
    ):
        raise ClusterAssemblyBuildError(
            "normalized plan attachment vertex UV is missing or invalid"
        )
    try:
        key = tuple(round(float(value), 6) for value in attachment_uv)
    except (TypeError, ValueError) as exc:
        raise ClusterAssemblyBuildError(
            "normalized plan attachment vertex UV is missing or invalid"
        ) from exc
    if any(not math.isfinite(value) for value in key):
        raise ClusterAssemblyBuildError(
            "normalized plan attachment vertex UV is missing or invalid"
        )
    if (
        source_obj.data.uv_layers.active is None
        or target_obj.data.uv_layers.active is None
    ):
        raise ClusterAssemblyBuildError(
            "normalized plan attachment correspondence requires UV layers"
        )
    source_index = _coincident_uv_vertex_index(
        source_obj,
        source_component,
        key,
    )
    target_index = _coincident_uv_vertex_index(
        target_obj,
        target_component,
        key,
    )
    if source_index is None or target_index is None:
        raise ClusterAssemblyBuildError(
            "normalized plan attachment pivot UV is absent from the "
            "source/target render component"
        )
    return source_index, target_index


def _attachment_uv_edge_point(obj, component, attachment_uv):
    """Resolve one UV point on a unique geometric edge of a component."""
    uv_layer = obj.data.uv_layers.active
    candidates = []
    uv_tolerance = 2.0e-6
    for polygon_index in component["polygons"]:
        polygon = obj.data.polygons[polygon_index]
        loops = list(polygon.loop_indices)
        for offset, first_loop in enumerate(loops):
            second_loop = loops[(offset + 1) % len(loops)]
            first_uv = uv_layer.data[first_loop].uv
            second_uv = uv_layer.data[second_loop].uv
            delta = (
                float(second_uv.x) - float(first_uv.x),
                float(second_uv.y) - float(first_uv.y),
            )
            length_squared = delta[0] * delta[0] + delta[1] * delta[1]
            if length_squared <= 1.0e-20:
                continue
            relative = (
                float(attachment_uv[0]) - float(first_uv.x),
                float(attachment_uv[1]) - float(first_uv.y),
            )
            parameter = (
                relative[0] * delta[0] + relative[1] * delta[1]
            ) / length_squared
            if parameter < -uv_tolerance or parameter > 1.0 + uv_tolerance:
                continue
            parameter = max(0.0, min(1.0, parameter))
            projected = (
                float(first_uv.x) + delta[0] * parameter,
                float(first_uv.y) + delta[1] * parameter,
            )
            residual = math.dist(projected, attachment_uv)
            if residual > uv_tolerance:
                continue
            first_index = int(obj.data.loops[first_loop].vertex_index)
            second_index = int(obj.data.loops[second_loop].vertex_index)
            first_coordinate = obj.data.vertices[first_index].co
            second_coordinate = obj.data.vertices[second_index].co
            coordinate = tuple(
                float(first_coordinate[axis]) * (1.0 - parameter)
                + float(second_coordinate[axis]) * parameter
                for axis in range(3)
            )
            candidates.append({
                "coordinate": coordinate,
                "edge_vertices": [first_index, second_index],
                "parameter": float(parameter),
                "uv_residual": float(residual),
            })
    if not candidates:
        return None

    coordinates = [
        tuple(float(value) for value in obj.data.vertices[index].co)
        for index in component["vertices"]
    ]
    coordinate_tolerance = _fbx_coordinate_tolerance(coordinates)
    groups = []
    for candidate in sorted(
        candidates,
        key=lambda row: (
            row["uv_residual"],
            row["edge_vertices"],
            row["parameter"],
        ),
    ):
        group = next((
            row for row in groups
            if math.dist(row["coordinate"], candidate["coordinate"])
            <= coordinate_tolerance
        ), None)
        if group is None:
            groups.append({
                "coordinate": candidate["coordinate"],
                "candidates": [candidate],
            })
        else:
            group["candidates"].append(candidate)
    if len(groups) != 1:
        raise ClusterAssemblyBuildError(
            "normalized plan attachment UV crosses multiple geometric edges"
        )
    best = groups[0]["candidates"][0]
    return {
        "coordinate": groups[0]["coordinate"],
        "edge_vertices": best["edge_vertices"],
        "parameter": best["parameter"],
        "uv_residual": best["uv_residual"],
        "candidate_edge_count": len(candidates),
    }


def _attachment_point_correspondence(
    source_obj,
    source_component,
    target_obj,
    target_component,
    attachment_uv,
):
    """Resolve an attachment as an exact vertex or a strict UV-edge point.

    Older normalized plans can contain an authored origin vertex that is not
    referenced by a face.  FBX removes that loose vertex, while preserving the
    boundary edge on which the authored UV lies.  The compatibility path below
    is limited to exact source/target topology matches and requires the source
    edge interpolation to reconstruct local origin.
    """
    if (
        not isinstance(attachment_uv, (list, tuple))
        or len(attachment_uv) != 2
    ):
        raise ClusterAssemblyBuildError(
            "normalized plan attachment vertex UV is missing or invalid"
        )
    try:
        resolved_uv = tuple(float(value) for value in attachment_uv)
    except (TypeError, ValueError) as exc:
        raise ClusterAssemblyBuildError(
            "normalized plan attachment vertex UV is missing or invalid"
        ) from exc
    if any(not math.isfinite(value) for value in resolved_uv):
        raise ClusterAssemblyBuildError(
            "normalized plan attachment vertex UV is missing or invalid"
        )
    if (
        source_obj.data.uv_layers.active is None
        or target_obj.data.uv_layers.active is None
    ):
        raise ClusterAssemblyBuildError(
            "normalized plan attachment correspondence requires UV layers"
        )

    key = tuple(round(value, 6) for value in resolved_uv)
    source_index = _coincident_uv_vertex_index(
        source_obj, source_component, key
    )
    target_index = _coincident_uv_vertex_index(
        target_obj, target_component, key
    )
    if source_index is not None and target_index is not None:
        return {
            "source_index": source_index,
            "target_index": target_index,
            "source_coordinate": tuple(
                float(value) for value in source_obj.data.vertices[source_index].co
            ),
            "target_coordinate": tuple(
                float(value) for value in target_obj.data.vertices[target_index].co
            ),
            "evidence": {
                "policy": "exact_attachment_uv_vertex_v1",
                "attachment_uv": list(resolved_uv),
            },
        }

    if _component_signature(
        source_obj.data, source_component
    ) != _component_signature(target_obj.data, target_component):
        raise ClusterAssemblyBuildError(
            "normalized plan attachment pivot UV is absent from the "
            "source/target render component"
        )

    source_edge = _attachment_uv_edge_point(
        source_obj, source_component, resolved_uv
    )
    target_edge = _attachment_uv_edge_point(
        target_obj, target_component, resolved_uv
    )
    if source_edge is None or target_edge is None:
        raise ClusterAssemblyBuildError(
            "normalized plan attachment pivot UV is absent from the "
            "source/target render component"
        )

    source_coordinates = [
        tuple(float(value) for value in source_obj.data.vertices[index].co)
        for index in source_component["vertices"]
    ]
    spans = [
        max(point[axis] for point in source_coordinates)
        - min(point[axis] for point in source_coordinates)
        for axis in range(3)
    ]
    origin_tolerance = max(max(spans) * 2.0e-6, 1.0e-8)
    source_origin_error = math.dist(
        source_edge["coordinate"], (0.0, 0.0, 0.0)
    )
    if source_origin_error > origin_tolerance:
        raise ClusterAssemblyBuildError(
            "normalized plan attachment UV edge does not resolve to the "
            "normalized source origin"
        )
    return {
        "source_index": source_index,
        "target_index": target_index,
        "source_coordinate": source_edge["coordinate"],
        "target_coordinate": target_edge["coordinate"],
        "evidence": {
            "policy": "normalized_origin_uv_edge_interpolation_v1",
            "attachment_uv": list(resolved_uv),
            "source_origin_error": float(source_origin_error),
            "source_origin_tolerance": float(origin_tolerance),
            "source_edge": source_edge,
            "target_edge": target_edge,
        },
    }


def _normalized_prototype_for_component(prototypes, target_mesh, target_component):
    signature = _component_signature(target_mesh, target_component)
    exact = prototypes.get(signature)
    if exact is not None:
        return exact
    target_faces = _component_uv_face_counter(target_mesh, target_component)
    if target_faces is None:
        return None
    candidates = []
    for prototype in prototypes.values():
        source_faces = _component_uv_face_counter(
            prototype["object"].data,
            prototype["component"],
        )
        if source_faces is None or not _counter_subset(
            target_faces, source_faces
        ):
            continue
        missing_faces = _counter_missing_identity_count(
            source_faces, target_faces
        )
        # SpeedTree 10.1 removes fully clipped boundary triangles when it
        # expands an external cutout mesh into the rendered tree FBX. Reed's
        # current cards lose 7.9-15.6% while retaining an exact UV-face
        # subset. Keep subset and unique-candidate checks strict, but accept
        # that measured exporter loss envelope.
        allowed_missing = max(
            1,
            int(
                math.ceil(
                    sum(source_faces.values())
                    * MAX_NORMALIZED_PLAN_FACE_LOSS_RATIO
                )
            ),
        )
        if missing_faces <= allowed_missing:
            candidates.append((missing_faces, prototype))
    if not candidates:
        return None
    candidates.sort(key=lambda row: row[0])
    best_missing = candidates[0][0]
    best = [row[1] for row in candidates if row[0] == best_missing]
    if len(best) != 1:
        raise ClusterAssemblyBuildError(
            "normalized plan UV-subset match is ambiguous for component: "
            + signature
        )
    return best[0]


def _normalized_prototype_match_diagnostics(
    prototypes,
    target_mesh,
    target_component,
):
    target_faces = _component_uv_face_counter(target_mesh, target_component)
    if target_faces is None:
        return [{"reason": "target_has_no_active_uv"}]
    rows = []
    for signature, prototype in prototypes.items():
        source_faces = _component_uv_face_counter(
            prototype["object"].data,
            prototype["component"],
        )
        if source_faces is None:
            rows.append({
                "prototype_signature": signature,
                "reason": "prototype_has_no_active_uv",
            })
            continue
        source_count = sum(source_faces.values())
        target_only = sum(
            int(count)
            for key, count in target_faces.items()
            if key not in source_faces
        )
        source_only = _counter_missing_identity_count(
            source_faces, target_faces
        )
        rendered_face_multiplicity = max(
            (
                int(count) / float(source_faces[key])
                for key, count in target_faces.items()
                if int(source_faces.get(key, 0)) > 0
            ),
            default=0.0,
        )
        rows.append({
            "prototype_signature": signature,
            "plan_name": str(
                (prototype.get("variant") or {}).get("plan_name") or ""
            ),
            "source_face_count": source_count,
            "target_face_count": sum(target_faces.values()),
            "target_only_face_count": target_only,
            "source_only_face_count": source_only,
            "maximum_rendered_face_multiplicity": (
                rendered_face_multiplicity
            ),
            "source_face_loss_ratio": (
                float(source_only) / float(source_count)
                if source_count
                else None
            ),
        })
    rows.sort(key=lambda row: (
        int(row.get("target_only_face_count") or 0),
        int(row.get("source_only_face_count") or 0),
        str(row.get("prototype_signature") or ""),
    ))
    return rows[:8]


def _partition_normalized_render_components(
    prototypes,
    target_mesh,
    components,
):
    """Separate render components that can be replaced by normalized assets.

    The rendered BWR mesh is authoritative.  A topology absent from the
    normalized capture is preserved in the Base mesh instead of inventing a
    new cluster variant or blocking the complete tree.
    """
    by_signature = defaultdict(list)
    for component in components:
        by_signature[_component_signature(target_mesh, component)].append(
            component
        )
    matched = {}
    preserved = []
    for signature, instances in sorted(by_signature.items()):
        prototype = prototypes.get(signature)
        exact_components = []
        if prototype is None and len(instances) == 1:
            exact_components = _exact_index_component_groups(
                target_mesh,
                instances[0]["polygons"],
            )
            strict_matches = []
            if len(exact_components) > 1:
                for component in exact_components:
                    exact_prototype = prototypes.get(
                        _component_signature(target_mesh, component)
                    )
                    if exact_prototype is None:
                        strict_matches = []
                        break
                    strict_matches.append((component, exact_prototype))
            if strict_matches:
                for component, exact_prototype in strict_matches:
                    recovered_signature = _component_signature(
                        target_mesh,
                        component,
                    )
                    recovered = matched.setdefault(
                        recovered_signature,
                        {
                            "prototype": exact_prototype,
                            "instances": [],
                        },
                    )
                    if recovered["prototype"] is not exact_prototype:
                        raise ClusterAssemblyBuildError(
                            "exact-index component recovery resolved one "
                            "render signature to multiple normalized plans"
                        )
                    recovered["instances"].append(component)
                continue
        if prototype is None:
            prototype = _normalized_prototype_for_component(
                prototypes,
                target_mesh,
                instances[0],
            )
        if prototype is None:
            exact_matches = []
            if len(instances) == 1 and len(exact_components) > 1:
                for component in exact_components:
                    exact_prototype = _normalized_prototype_for_component(
                        prototypes,
                        target_mesh,
                        component,
                    )
                    if exact_prototype is None:
                        exact_matches = []
                        break
                    exact_matches.append((component, exact_prototype))
            if exact_matches:
                for component, exact_prototype in exact_matches:
                    recovered_signature = _component_signature(
                        target_mesh,
                        component,
                    )
                    recovered = matched.setdefault(
                        recovered_signature,
                        {
                            "prototype": exact_prototype,
                            "instances": [],
                        },
                    )
                    if recovered["prototype"] is not exact_prototype:
                        raise ClusterAssemblyBuildError(
                            "exact-index component recovery resolved one "
                            "render signature to multiple normalized plans"
                        )
                    recovered["instances"].append(component)
                continue
            preserved.append({
                "topology_signature": signature,
                "instance_count": len(instances),
                "polygon_count": sum(
                    len(component["polygons"])
                    for component in instances
                ),
                "prototype_match_diagnostics": (
                    _normalized_prototype_match_diagnostics(
                        prototypes,
                        target_mesh,
                        instances[0],
                    )
                ),
            })
            continue
        matched[signature] = {
            "prototype": prototype,
            "instances": instances,
        }
    return matched, preserved


def _vertex_descriptors(obj, component):
    mesh = obj.data
    uv_layer = mesh.uv_layers.active
    degree = Counter()
    component_edges = set()
    representative = defaultdict(set)
    for polygon_index in component["polygons"]:
        polygon = mesh.polygons[polygon_index]
        if uv_layer is not None:
            vertices = [
                (
                    round(float(uv_layer.data[loop].uv.x), 6),
                    round(float(uv_layer.data[loop].uv.y), 6),
                )
                for loop in polygon.loop_indices
            ]
        else:
            vertices = [int(value) for value in polygon.vertices]
        for index, left in enumerate(vertices):
            right = vertices[(index + 1) % len(vertices)]
            component_edges.add(tuple(sorted((left, right))))
    for left, right in component_edges:
        degree[left] += 1
        degree[right] += 1
    records = defaultdict(list)
    for polygon_index in component["polygons"]:
        polygon = mesh.polygons[polygon_index]
        face_uv = ()
        if uv_layer is not None:
            face_uv = tuple(
                sorted(
                    (
                        round(float(uv_layer.data[loop].uv.x), 6),
                        round(float(uv_layer.data[loop].uv.y), 6),
                    )
                    for loop in polygon.loop_indices
                )
            )
        for loop_index in polygon.loop_indices:
            loop = mesh.loops[loop_index]
            if uv_layer is not None:
                loop_uv = uv_layer.data[loop_index].uv
                logical_vertex = (
                    round(float(loop_uv.x), 6),
                    round(float(loop_uv.y), 6),
                )
            else:
                logical_vertex = int(loop.vertex_index)
            representative[logical_vertex].add(int(loop.vertex_index))
            uv = (0.0, 0.0)
            if uv_layer is not None:
                value = uv_layer.data[loop_index].uv
                uv = (round(float(value.x), 6), round(float(value.y), 6))
            records[logical_vertex].append((uv, face_uv))
    component_coordinates = [
        tuple(float(value) for value in mesh.vertices[index].co)
        for index in component["vertices"]
    ]
    tolerance = _fbx_coordinate_tolerance(component_coordinates)
    resolved_representatives = {}
    for logical_vertex, indices in representative.items():
        ordered = sorted(indices)
        reference = tuple(
            float(value) for value in mesh.vertices[ordered[0]].co
        )
        if any(
            math.dist(
                reference,
                tuple(float(value) for value in mesh.vertices[index].co),
            ) > tolerance
            for index in ordered[1:]
        ):
            raise ClusterAssemblyBuildError(
                "UV/topology descriptor has ambiguous geometric positions: "
                f"logical_vertex={logical_vertex!r} indices={ordered}"
            )
        resolved_representatives[logical_vertex] = ordered[0]
    return sorted(
        (
            (degree[logical_vertex], tuple(sorted(records[logical_vertex]))),
            resolved_representatives[logical_vertex],
        )
        for logical_vertex in resolved_representatives
    )


def _ordered_correspondence(obj, template, target):
    source = _vertex_descriptors(obj, template)
    destination = _vertex_descriptors(obj, target)
    if [row[0] for row in source] != [row[0] for row in destination]:
        raise ClusterAssemblyBuildError(
            "part prototype UV/topology descriptors do not match an instance"
        )
    return [row[1] for row in source], [row[1] for row in destination]


def _ordered_cross_object_correspondence(
    source_obj,
    source_component,
    target_obj,
    target_component,
    include_evidence=False,
):
    source_uv = source_obj.data.uv_layers.active
    target_uv = target_obj.data.uv_layers.active
    if source_uv is not None and target_uv is not None:
        source_by_uv = _uv_vertex_candidates(
            source_obj,
            source_component,
        )
        target_by_uv = _uv_vertex_candidates(
            target_obj,
            target_component,
        )
        missing = sorted(set(target_by_uv) - set(source_by_uv))
        if missing:
            raise ClusterAssemblyBuildError(
                "normalized plan UV correspondence does not cover the target "
                f"instance: missing_uv_count={len(missing)} sample={missing[:5]}"
            )
        source_indices = []
        target_indices = []
        duplicate_rows = []
        for key in sorted(target_by_uv):
            pairs, evidence = _match_uv_candidate_groups(
                source_obj,
                source_component,
                target_obj,
                target_component,
                key,
                source_by_uv[key],
                target_by_uv[key],
            )
            source_indices.extend(pair[0] for pair in pairs)
            target_indices.extend(pair[1] for pair in pairs)
            if (
                evidence["source_geometric_candidates"] > 1
                or evidence["target_geometric_candidates"] > 1
                or evidence["discarded_source_candidates"] > 0
            ):
                duplicate_rows.append(evidence)
        if len(source_indices) < 3:
            raise ClusterAssemblyBuildError(
                "normalized plan surviving UV correspondence has fewer than "
                f"three geometric points: count={len(source_indices)}"
            )
        evidence = {
            "policy": "all_candidates_topology_disambiguated_fail_closed_v1",
            "matched_point_count": len(source_indices),
            "target_uv_key_count": len(target_by_uv),
            "source_only_uv_key_count": len(
                set(source_by_uv) - set(target_by_uv)
            ),
            "duplicate_uv_rows": duplicate_rows,
        }
        if include_evidence:
            return source_indices, target_indices, evidence
        return source_indices, target_indices
    source = _vertex_descriptors(source_obj, source_component)
    destination = _vertex_descriptors(target_obj, target_component)
    if [row[0] for row in source] != [row[0] for row in destination]:
        raise ClusterAssemblyBuildError(
            "normalized plan UV/topology descriptors do not match an instance"
        )
    source_indices = [row[1] for row in source]
    target_indices = [row[1] for row in destination]
    if include_evidence:
        return source_indices, target_indices, {
            "policy": "topology_descriptor_no_uv_v1",
            "matched_point_count": len(source_indices),
            "target_uv_key_count": 0,
            "source_only_uv_key_count": 0,
            "duplicate_uv_rows": [],
        }
    return source_indices, target_indices


def _import_normalized_plan_prototypes(bpy, contract):
    if (contract or {}).get("status") != "ready":
        raise ClusterAssemblyBuildError(
            "normalized plan variant contract is not ready"
        )
    prototypes = {}
    created = []
    for variant in contract.get("variants") or []:
        plan_name = str(variant.get("plan_name") or "")
        plan_fbx = validate_file_fingerprint(
            variant.get("plan_fbx"),
            f"normalized plan FBX {plan_name or '<unnamed>'}",
        )["path"]
        before = {obj.as_pointer() for obj in bpy.data.objects}
        result = bpy.ops.import_scene.fbx(filepath=str(Path(plan_fbx).resolve()))
        if "FINISHED" not in result:
            raise ClusterAssemblyBuildError(
                f"normalized plan FBX import failed: {plan_fbx}: {result}"
            )
        imported = [
            obj for obj in bpy.data.objects
            if obj.as_pointer() not in before
        ]
        created.extend(imported)
        meshes = [
            obj for obj in imported
            if obj.type == "MESH" and len(obj.data.polygons)
        ]
        if len(meshes) != 1:
            raise ClusterAssemblyBuildError(
                "normalized plan FBX must contain exactly one mesh: "
                + str(plan_fbx)
            )
        source_obj = meshes[0]
        components = _component_groups(
            source_obj.data,
            [polygon.index for polygon in source_obj.data.polygons],
        )
        if len(components) != 1:
            raise ClusterAssemblyBuildError(
                "normalized plan FBX must contain one connected component: "
                + plan_name
            )
        component = components[0]
        signature = _component_signature(source_obj.data, component)
        if signature in prototypes:
            raise ClusterAssemblyBuildError(
                "normalized plan variants have duplicate UV/topology signatures: "
                + signature
            )
        prototypes[signature] = {
            "variant": deepcopy(variant),
            "object": source_obj,
            "component": component,
            "signature": signature,
        }
    if not prototypes:
        raise ClusterAssemblyBuildError(
            "normalized plan variant contract contains no variants"
        )
    return prototypes, created


def _world_points(obj, vertex_indices):
    return [
        tuple(float(value) for value in (obj.matrix_world @ obj.data.vertices[index].co))
        for index in vertex_indices
    ]


def _world_coordinate(obj, coordinate):
    matrix = obj.matrix_world
    return tuple(
        float(
            sum(
                float(matrix[row][axis]) * float(coordinate[axis])
                for axis in range(3)
            )
            + float(matrix[row][3])
        )
        for row in range(3)
    )


def _component_influences(obj, component):
    totals = Counter()
    for vertex_index in component["vertices"]:
        for item in obj.data.vertices[vertex_index].groups:
            group = obj.vertex_groups[item.group]
            totals[str(group.name)] += float(item.weight)
    total = sum(totals.values())
    if total <= 0.0:
        raise ClusterAssemblyBuildError("part component has no final-skeleton weights")
    return [
        {"bone": name, "weight": value / total}
        for name, value in totals.most_common()
        if value > 0.0
    ]


def _weighted_vertex_locator(obj):
    try:
        from mathutils.kdtree import KDTree
    except ImportError as exc:  # pragma: no cover - Blender-only dependency.
        raise ClusterAssemblyBuildError(
            "Blender KDTree is required for source-role skeleton binding"
        ) from exc
    weighted = [
        vertex for vertex in obj.data.vertices
        if list(vertex.groups)
    ]
    if not weighted:
        raise ClusterAssemblyBuildError(
            "final merged mesh has no weighted vertices for Assembly binding"
        )
    tree = KDTree(len(weighted))
    for vertex in weighted:
        tree.insert(obj.matrix_world @ vertex.co, int(vertex.index))
    tree.balance()
    world_points = [
        tuple(float(value) for value in (obj.matrix_world @ vertex.co))
        for vertex in weighted
    ]
    spans = [
        max(point[axis] for point in world_points)
        - min(point[axis] for point in world_points)
        for axis in range(3)
    ]
    return {
        "tree": tree,
        "tolerance_meters": _weighted_vertex_attachment_tolerance(spans),
    }


def _weighted_vertex_attachment_tolerance(spans):
    """Bound nearest-weight inheritance by one percent of the Full-SK span.

    Authored attachment pivots can sit just outside the rendered branch skin.
    A half-percent gate rejected valid large-tree pivots by only a few
    millimeters; one percent still keeps the lookup local to the attachment
    while covering the branch radius and SpeedTree export clipping margin.
    """
    values = [abs(float(value)) for value in spans]
    if not values:
        return MIN_WEIGHT_BINDING_DISTANCE_METERS
    return max(
        max(values) * MAX_WEIGHT_BINDING_DISTANCE_SPAN_RATIO,
        MIN_WEIGHT_BINDING_DISTANCE_METERS,
    )


def _nearest_weighted_vertex_influences(obj, locator, world_point):
    _coordinate, vertex_index, distance = locator["tree"].find(world_point)
    tolerance = float(locator["tolerance_meters"])
    if vertex_index is None or float(distance) > tolerance:
        raise ClusterAssemblyBuildError(
            "Assembly source-role attachment has no nearby weighted Full-SK "
            f"vertex: distance={float(distance):.9g} tolerance={tolerance:.9g}"
        )
    vertex = obj.data.vertices[int(vertex_index)]
    totals = Counter()
    for item in vertex.groups:
        group = obj.vertex_groups[item.group]
        totals[str(group.name)] += float(item.weight)
    total = sum(totals.values())
    if total <= 0.0:
        raise ClusterAssemblyBuildError(
            "nearest Full-SK attachment vertex has no skeleton weights"
        )
    influences = [
        {"bone": name, "weight": value / total}
        for name, value in totals.most_common()
        if value > 0.0
    ]
    return influences, {
        "policy": "nearest_weighted_full_sk_vertex_v1",
        "vertex_index": int(vertex_index),
        "distance_meters": float(distance),
        "tolerance_meters": tolerance,
    }


def _copy_component_as_rigid_part(bpy, source_obj, component, name):
    mesh = source_obj.data
    ordered_vertices = component["vertices"]
    source_world = _world_points(source_obj, ordered_vertices)
    center = [statistics.fmean(values) for values in zip(*source_world)]
    remap = {old: new for new, old in enumerate(ordered_vertices)}
    faces = [
        [remap[int(index)] for index in mesh.polygons[polygon].vertices]
        for polygon in component["polygons"]
    ]
    vertices = [
        tuple(point[axis] - center[axis] for axis in range(3))
        for point in source_world
    ]
    new_mesh = bpy.data.meshes.new(name + "_Mesh")
    new_mesh.from_pydata(vertices, [], faces)
    new_mesh.update()
    new_obj = bpy.data.objects.new(name, new_mesh)
    bpy.context.scene.collection.objects.link(new_obj)
    used_material_indices = sorted(
        {
            int(mesh.polygons[index].material_index)
            for index in component["polygons"]
        }
    )
    material_remap = {
        old_index: new_index
        for new_index, old_index in enumerate(used_material_indices)
    }
    for old_index in used_material_indices:
        new_mesh.materials.append(mesh.materials[old_index])
    for new_polygon, old_index in zip(new_mesh.polygons, component["polygons"]):
        source_polygon = mesh.polygons[old_index]
        new_polygon.material_index = material_remap[source_polygon.material_index]
        new_polygon.use_smooth = source_polygon.use_smooth
    if mesh.uv_layers.active is not None:
        source_uv = mesh.uv_layers.active
        target_uv = new_mesh.uv_layers.new(name=source_uv.name)
        for new_polygon, old_index in zip(new_mesh.polygons, component["polygons"]):
            old_polygon = mesh.polygons[old_index]
            for new_loop, old_loop in zip(
                new_polygon.loop_indices, old_polygon.loop_indices
            ):
                target_uv.data[new_loop].uv = source_uv.data[old_loop].uv
    for attribute in mesh.color_attributes:
        copied = new_mesh.color_attributes.new(
            name=attribute.name,
            type=attribute.data_type,
            domain=attribute.domain,
        )
        if attribute.domain == "POINT":
            for old_index, new_index in remap.items():
                copied.data[new_index].color = attribute.data[old_index].color
        elif attribute.domain == "CORNER":
            for new_polygon, old_index in zip(
                new_mesh.polygons,
                component["polygons"],
            ):
                old_polygon = mesh.polygons[old_index]
                for new_loop, old_loop in zip(
                    new_polygon.loop_indices,
                    old_polygon.loop_indices,
                ):
                    copied.data[new_loop].color = attribute.data[old_loop].color

    armature_data = bpy.data.armatures.new(name + "_PartSkeleton")
    part_armature = bpy.data.objects.new(name + "_PartArmature", armature_data)
    bpy.context.scene.collection.objects.link(part_armature)
    bpy.context.view_layer.objects.active = part_armature
    part_armature.select_set(True)
    bpy.ops.object.mode_set(mode="EDIT")
    bone = armature_data.edit_bones.new("part_root")
    bone.head = (0.0, 0.0, 0.0)
    bone.tail = (0.0, 0.0, 1.0)
    bpy.ops.object.mode_set(mode="OBJECT")
    new_obj.parent = part_armature
    modifier = new_obj.modifiers.new(name="Armature", type="ARMATURE")
    modifier.object = part_armature
    group = new_obj.vertex_groups.new(name="part_root")
    group.add(list(range(len(new_mesh.vertices))), 1.0, "REPLACE")
    return new_obj, part_armature, center


def _base_role_polygon_indices(role_build_plans, roles, final_merged_mesh):
    """Remove every rendered Assembly role polygon from the generated Base.

    A role component that does not match a normalized prototype is useful
    diagnostic evidence, but preserving it in ``NA_Base`` leaves the authored
    plan/card geometry embedded beside the generated Assembly instances.
    """
    return sorted({
        int(polygon_index)
        for provider_key, plan in role_build_plans.items()
        if plan.get("target_object") is final_merged_mesh
        for polygon_index in (
            (roles.get(provider_key) or {}).get("polygon_indices") or []
        )
    })


def _copy_base_without_role_polygons(bpy, source_obj, polygon_indices, name):
    import bmesh

    duplicate = source_obj.copy()
    duplicate.data = source_obj.data.copy()
    duplicate.name = name
    duplicate.data.name = name + "_Mesh"
    bpy.context.scene.collection.objects.link(duplicate)
    mesh = duplicate.data
    source_scale = [float(value) for value in source_obj.matrix_world.to_scale()]
    if any(abs(value - 1.0) > 1.0e-5 for value in source_scale):
        raise ClusterAssemblyBuildError(
            "final BWR mesh must have applied scale before Assembly generation: "
            f"{source_obj.name} scale={source_scale}"
        )
    # Keep the exact meter-space object/bone encoding used by the Full SK FBX.
    # Blender's FBX exporter carries the scene-unit conversion as FBX metadata;
    # baking another 100x into BASE coordinates changes Unreal's implicit
    # armature root scale and therefore the mesh-local reference pose hash.
    duplicate.matrix_world = source_obj.matrix_world.copy()
    bm = bmesh.new()
    bm.from_mesh(mesh)
    bm.faces.ensure_lookup_table()
    targets = [bm.faces[index] for index in sorted(set(polygon_indices))]
    bmesh.ops.delete(bm, geom=targets, context="FACES")
    loose = [vertex for vertex in bm.verts if not vertex.link_faces]
    if loose:
        bmesh.ops.delete(bm, geom=loose, context="VERTS")
    bm.to_mesh(mesh)
    bm.free()
    mesh.update()
    if not mesh.polygons:
        raise ClusterAssemblyBuildError("Assembly base would contain no polygons")
    source_materials = list(mesh.materials)
    old_polygon_slots = [int(polygon.material_index) for polygon in mesh.polygons]
    used_slots = sorted(set(old_polygon_slots))
    slot_remap = {
        old_index: new_index
        for new_index, old_index in enumerate(used_slots)
    }
    mesh.materials.clear()
    for old_index in used_slots:
        mesh.materials.append(source_materials[old_index])
    for polygon, old_index in zip(mesh.polygons, old_polygon_slots):
        polygon.material_index = slot_remap[old_index]
    mesh.update()
    return duplicate


def _copy_normalized_base_armature(bpy, source_armature, name):
    """Copy the final Full-SK armature without changing its bind pose."""

    duplicate = source_armature.copy()
    duplicate.data = source_armature.data.copy()
    duplicate.name = name
    duplicate.data.name = name + "_Skeleton"
    bpy.context.scene.collection.objects.link(duplicate)
    source_scale = [
        float(value) for value in source_armature.matrix_world.to_scale()
    ]
    if any(abs(value - 1.0) > 1.0e-5 for value in source_scale):
        raise ClusterAssemblyBuildError(
            "final BWR armature must have applied scale before Assembly generation: "
            f"{source_armature.name} scale={source_scale}"
        )
    duplicate.matrix_world = source_armature.matrix_world.copy()
    return duplicate


def _replicate_full_export_parent_chain(
    bpy,
    source_obj,
    source_armature,
    target_obj,
    target_armature,
):
    """Copy the Full Send2UE hierarchy between Armature and mesh.

    BWR's normal Export collection can contain one or more transform empties
    between the final Armature and mesh.  Blender's FBX exporter uses that
    hierarchy when encoding the implicit armature-object root.  Parenting a
    generated BASE mesh directly to the copied Armature preserves its visible
    world transform but changes the imported root scale (observed 1 -> 100).
    Reproduce the source chain and local transforms instead of applying a
    tree-specific scale correction.
    """
    source_chain = []
    current = source_obj.parent
    while current is not None and current != source_armature:
        source_chain.append(current)
        current = current.parent
    if current != source_armature:
        raise ClusterAssemblyBuildError(
            "final BWR mesh is not parented through the final armature"
        )

    target_parent = target_armature
    created = []
    for index, source_parent in enumerate(reversed(source_chain), start=1):
        if source_parent.type != "EMPTY":
            raise ClusterAssemblyBuildError(
                "unsupported Full export parent between armature and mesh: "
                f"{source_parent.name} ({source_parent.type})"
            )
        duplicate = source_parent.copy()
        duplicate.name = f"{target_obj.name}_ExportParent_{index:02d}"
        bpy.context.scene.collection.objects.link(duplicate)
        duplicate.parent = target_parent
        duplicate.matrix_parent_inverse = (
            source_parent.matrix_parent_inverse.copy()
        )
        duplicate.matrix_basis = source_parent.matrix_basis.copy()
        target_parent = duplicate
        created.append(duplicate)

    target_obj.parent = target_parent
    target_obj.matrix_parent_inverse = source_obj.matrix_parent_inverse.copy()
    target_obj.matrix_basis = source_obj.matrix_basis.copy()
    return created


def _validate_base_export_parent_chain(
    base_armature,
    export_parents,
    base_obj,
):
    """Prove the copied Base hierarchy matches the Full-SK source chain."""
    expected_parent = base_armature
    ordered_parents = list(export_parents or [])
    for parent in ordered_parents:
        if parent.parent is not expected_parent:
            raise ClusterAssemblyBuildError(
                "Assembly base export parent chain is not contiguous: "
                f"object={parent.name}, expected_parent={expected_parent.name}"
            )
        expected_parent = parent
    if base_obj.parent is not expected_parent:
        raise ClusterAssemblyBuildError(
            "Assembly base mesh is detached from its replicated Full export "
            f"chain: mesh={base_obj.name}, expected_parent={expected_parent.name}"
        )
    return None


def _strip_fbx_scene_textures(scene_data, get_fbx_uuid_from_key):
    """Keep material slots but remove FBX Texture/Video records and links.

    Assembly imports use the existing material-pipeline JSON sidecar as the
    only texture source of truth.  Exporting Blender image records as well
    makes Unreal derive names such as ``M_leaf_elm_01`` from file basenames;
    those collide with the material slots of the same name and can be
    reassigned incorrectly on reimport.
    """
    templates = dict(scene_data.templates)
    templates_users = scene_data.templates_users
    for template_key in (b"TextureFile", b"Video"):
        template = templates.pop(template_key, None)
        if template is not None:
            templates_users -= template.nbr_users

    texture_ids = {
        get_fbx_uuid_from_key(texture_key)
        for texture_key, _fbx_property in scene_data.data_textures.values()
    }
    video_ids = {
        get_fbx_uuid_from_key(video_key)
        for video_key, _texture_keys in scene_data.data_videos.values()
    }
    removed_ids = texture_ids | video_ids
    connections = [
        connection
        for connection in scene_data.connections
        if connection[1] not in removed_ids and connection[2] not in removed_ids
    ]
    return scene_data._replace(
        templates=templates,
        templates_users=templates_users,
        connections=connections,
        data_textures={},
        data_videos={},
    )


@contextmanager
def _textureless_fbx_scene_data(bpy):
    """Patch Blender's stock FBX writer only for one Assembly export."""
    # Ordinary unit tests use a small bpy mock.  Real Blender must provide and
    # successfully patch the stock exporter; there is no textured fallback.
    if not hasattr(bpy, "app"):
        yield
        return
    try:
        import io_scene_fbx.export_fbx_bin as export_fbx_bin
        from io_scene_fbx.fbx_utils import get_fbx_uuid_from_key
    except (ImportError, AttributeError) as exc:
        raise ClusterAssemblyBuildError(
            "Assembly FBX textureless exporter is unavailable"
        ) from exc

    original = export_fbx_bin.fbx_data_from_scene

    def without_textures(scene, depsgraph, settings):
        return _strip_fbx_scene_textures(
            original(scene, depsgraph, settings),
            get_fbx_uuid_from_key,
        )

    export_fbx_bin.fbx_data_from_scene = without_textures
    try:
        yield
    finally:
        export_fbx_bin.fbx_data_from_scene = original


def _validate_textureless_fbx(
    bpy,
    path,
    *,
    full_skeleton_root=False,
    expected_geometry_bounds=None,
):
    """Fail closed on textures, unit drift, axis drift, or geometry baking."""
    if not hasattr(bpy, "app"):
        return {
            "status": "not_available_in_mock",
            "texture_records": 0,
            "video_records": 0,
        }
    try:
        from io_scene_fbx import parse_fbx

        root, version = parse_fbx.parse(str(path))
    except (ImportError, OSError, ValueError) as exc:
        raise ClusterAssemblyBuildError(
            f"Assembly FBX texture-record validation failed: {path}"
        ) from exc
    objects = next((item for item in root.elems if item.id == b"Objects"), None)
    texture_records = 0
    video_records = 0
    model_scales = []
    geometry_bounds = {}
    if objects is not None:
        texture_records = sum(item.id == b"Texture" for item in objects.elems)
        video_records = sum(item.id == b"Video" for item in objects.elems)
        for item in objects.elems:
            if item.id == b"Geometry" and len(item.props) >= 2:
                vertices = next(
                    (child for child in item.elems if child.id == b"Vertices"),
                    None,
                )
                values = (
                    list(vertices.props[0])
                    if vertices is not None and vertices.props
                    else []
                )
                axes = [values[index::3] for index in range(3)]
                raw_name = item.props[1]
                geometry_name = (
                    raw_name.decode("utf-8", errors="replace")
                    if isinstance(raw_name, bytes)
                    else str(raw_name)
                ).split("\x00", 1)[0]
                geometry_bounds[geometry_name] = {
                    "vertex_count": len(values) // 3,
                    "minimum": [min(axis) if axis else 0.0 for axis in axes],
                    "maximum": [max(axis) if axis else 0.0 for axis in axes],
                }
                continue
            if item.id != b"Model" or len(item.props) < 3:
                continue
            if item.props[2] not in {b"Mesh", b"Null"}:
                continue
            properties = next(
                (child for child in item.elems if child.id == b"Properties70"),
                None,
            )
            scale = [1.0, 1.0, 1.0]
            rotation = [0.0, 0.0, 0.0]
            if properties is not None:
                scaling = next(
                    (
                        child
                        for child in properties.elems
                        if child.id == b"P"
                        and child.props
                        and child.props[0] == b"Lcl Scaling"
                    ),
                    None,
                )
                if scaling is not None and len(scaling.props) >= 3:
                    scale = [float(value) for value in scaling.props[-3:]]
                local_rotation = next(
                    (
                        child
                        for child in properties.elems
                        if child.id == b"P"
                        and child.props
                        and child.props[0] == b"Lcl Rotation"
                    ),
                    None,
                )
                if local_rotation is not None and len(local_rotation.props) >= 3:
                    rotation = [
                        float(value) for value in local_rotation.props[-3:]
                    ]
            raw_name = item.props[1]
            name = (
                raw_name.decode("utf-8", errors="replace")
                if isinstance(raw_name, bytes)
                else str(raw_name)
            ).split("\x00", 1)[0]
            model_scales.append({
                "name": name,
                "type": str(item.props[2]),
                "is_null": item.props[2] == b"Null",
                "scale": scale,
                "rotation": rotation,
            })
    if texture_records or video_records:
        raise ClusterAssemblyBuildError(
            "Assembly FBX contains texture records that can collide on Unreal "
            f"reimport: textures={texture_records}, videos={video_records}, path={path}"
        )
    units = bpy.context.scene.unit_settings
    expected_model_scale = (
        100.0 * float(units.scale_length)
        if str(units.system) == "METRIC"
        else 100.0
    )
    unexpected_model_scales = []
    for row in model_scales:
        row_expected_scale = 1.0 if row["is_null"] else expected_model_scale
        row["expected_scale"] = row_expected_scale
        if any(
            abs(value - row_expected_scale)
            > 1.0e-5 * max(row_expected_scale, 1.0)
            for value in row["scale"]
        ):
            unexpected_model_scales.append(row)
    if unexpected_model_scales:
        raise ClusterAssemblyBuildError(
            "Assembly FBX model scale does not match its Full SK/part root "
            f"contract: {unexpected_model_scales}, path={path}"
        )
    expected_model_rotation = [90.0, 0.0, 0.0]
    unexpected_model_rotations = []
    for row in model_scales:
        row["expected_rotation"] = expected_model_rotation
        if any(
            abs((value - expected + 180.0) % 360.0 - 180.0) > 1.0e-3
            for value, expected in zip(
                row["rotation"], expected_model_rotation
            )
        ):
            unexpected_model_rotations.append(row)
    if unexpected_model_rotations:
        raise ClusterAssemblyBuildError(
            "Assembly FBX model rotation does not match the Unreal Z-up axis "
            f"contract: {unexpected_model_rotations}, path={path}"
        )
    geometry_mismatches = []
    for name, expected in (expected_geometry_bounds or {}).items():
        actual = geometry_bounds.get(name)
        if actual is None:
            geometry_mismatches.append({"name": name, "reason": "missing"})
            continue
        mismatched = actual["vertex_count"] != expected["vertex_count"]
        for key in ("minimum", "maximum"):
            mismatched = mismatched or any(
                abs(float(actual_value) - float(expected_value)) > 1.0e-5
                for actual_value, expected_value in zip(
                    actual[key], expected[key]
                )
            )
        if mismatched:
            geometry_mismatches.append(
                {"name": name, "expected": expected, "actual": actual}
            )
    if geometry_mismatches:
        raise ClusterAssemblyBuildError(
            "Assembly FBX changed local geometry bounds during export: "
            f"{geometry_mismatches}, path={path}"
        )
    return {
        "status": "textureless",
        "fbx_version": version,
        "texture_records": texture_records,
        "video_records": video_records,
        "model_scales": model_scales,
        "expected_model_scale": expected_model_scale,
        "expected_model_rotation": expected_model_rotation,
        "full_skeleton_root": bool(full_skeleton_root),
        "all_model_scales_match_contract": True,
        "all_model_rotations_match_contract": True,
        "geometry_bounds": geometry_bounds,
        "all_geometry_bounds_match_contract": True,
        "material_source": "material_pipeline_json_sidecar",
    }


def _weighted_bones_for_base(obj, skeleton_snapshot, skeleton_by_name=None):
    if skeleton_by_name is None:
        checked, skeleton_by_name = _skeleton_maps(skeleton_snapshot)
        del checked
    weighted = set()
    unweighted_vertices = []
    for vertex in obj.data.vertices:
        vertex_weight = 0.0
        for item in vertex.groups:
            weight = float(item.weight)
            if weight <= 0.0:
                continue
            group_name = str(obj.vertex_groups[item.group].name)
            if group_name not in skeleton_by_name:
                raise ClusterAssemblyBuildError(
                    "Assembly base uses a weighted group outside the final Skeleton: "
                    + group_name
                )
            weighted.add(group_name)
            vertex_weight += weight
        if vertex_weight <= 0.0:
            unweighted_vertices.append(int(vertex.index))
    if unweighted_vertices:
        raise ClusterAssemblyBuildError(
            "Assembly base has unweighted vertices: "
            + ", ".join(str(index) for index in unweighted_vertices[:20])
        )
    if not weighted:
        raise ClusterAssemblyBuildError("Assembly base has no final-Skeleton weights")
    return sorted(
        weighted,
        key=lambda name: int(skeleton_by_name[name]["index"]),
    )


def _export_selected_fbx(bpy, path, objects, *, full_skeleton_root=False):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    bpy.ops.object.mode_set(mode="OBJECT") if bpy.context.object and bpy.context.object.mode != "OBJECT" else None
    bpy.ops.object.select_all(action="DESELECT")
    previous = []
    for obj in objects:
        previous.append((obj, bool(obj.hide_get()), bool(obj.hide_viewport)))
        obj.hide_viewport = False
        obj.hide_set(False)
        obj.select_set(True)
    bpy.context.view_layer.objects.active = objects[0]
    armatures = [obj for obj in objects if getattr(obj, "type", "") == "ARMATURE"]
    meshes = [obj for obj in objects if getattr(obj, "type", "") == "MESH"]
    if len(armatures) != 1 or not meshes:
        raise ClusterAssemblyBuildError(
            "generated Assembly FBX requires one armature and at least one mesh"
        )
    expected_geometry_bounds = None
    if hasattr(bpy, "app"):
        expected_geometry_bounds = {}
        for obj in meshes:
            coordinates = [vertex.co for vertex in obj.data.vertices]
            expected_geometry_bounds[obj.data.name] = {
                "vertex_count": len(coordinates),
                "minimum": [
                    min(float(co[index]) for co in coordinates)
                    if coordinates
                    else 0.0
                    for index in range(3)
                ],
                "maximum": [
                    max(float(co[index]) for co in coordinates)
                    if coordinates
                    else 0.0
                    for index in range(3)
                ],
            }
    for obj in armatures + meshes:
        scale = [float(value) for value in obj.matrix_world.to_scale()]
        if any(abs(value - 1.0) > 1.0e-5 for value in scale):
            raise ClusterAssemblyBuildError(
                f"generated Assembly object transform is not applied: "
                f"{obj.name} scale={scale}"
            )
    units = bpy.context.scene.unit_settings
    if (
        str(units.system) != "METRIC"
        or abs(float(units.scale_length) - 1.0) > 1.0e-9
    ):
        raise ClusterAssemblyBuildError(
            "generated Assembly coordinates must preserve the Full SK meter "
            "scene-unit contract"
        )
    addon_runtime = None
    try:
        if not hasattr(bpy, "app"):
            # Lightweight unit-test mocks exercise the operator settings
            # without importing Blender add-ons.
            result = bpy.ops.export_scene.fbx(
                filepath=str(path),
                use_selection=True,
                object_types={"ARMATURE", "MESH"},
                use_mesh_modifiers=False,
                mesh_smooth_type="FACE",
                use_custom_props=False,
                add_leaf_bones=False,
                primary_bone_axis="Y",
                secondary_bone_axis="X",
                armature_nodetype="NULL",
                bake_anim=False,
                path_mode="AUTO",
                global_scale=1.0,
                apply_unit_scale=True,
                apply_scale_options="FBX_SCALE_NONE",
                axis_forward="Y",
                axis_up="Z",
                bake_space_transform=False,
            )
            if "FINISHED" not in result:
                raise ClusterAssemblyBuildError(f"FBX export failed: {result}")
        else:
            # The Full SK is exported through Send2UE's armature-aware FBX
            # patch. Use that same bind/root-scale contract for generated
            # BASE/part assets, then independently prove that it did not bake
            # or shrink their local geometry.
            from blender_addon_gateway import prepare_runtime

            addon_runtime = prepare_runtime(
                "sk_batch.cluster_assembly_builder.fbx_export",
                {"send2ue": ("fbx_export_v1",)},
            )
            send2ue_fbx_export = addon_runtime.operation(
                "send2ue", "fbx_export"
            )

            bpy.context.scene.send2ue.export_object_name_as_root = True
            bpy.context.scene.send2ue.export_custom_root_name = ""
            bpy.context.scene.send2ue.use_object_origin = False
            textureless_flag = (
                "send2ue_material_pipeline_textureless_fbx_export"
            )
            previous_flag = bpy.app.driver_namespace.get(textureless_flag)
            bpy.app.driver_namespace[textureless_flag] = True
            try:
                send2ue_fbx_export(
                    filepath=str(path),
                    use_selection=True,
                    object_types={"ARMATURE", "MESH", "EMPTY"},
                    use_mesh_modifiers=False,
                    use_mesh_modifiers_render=True,
                    mesh_smooth_type="FACE",
                    use_mesh_edges=False,
                    use_subsurf=False,
                    bake_anim_use_nla_strips=True,
                    bake_anim_use_all_actions=False,
                    bake_anim_force_startend_keying=True,
                    bake_anim_step=1.0,
                    bake_anim_simplify_factor=0.0,
                    path_mode="AUTO",
                    use_metadata=True,
                    apply_unit_scale=True,
                    apply_scale_options="FBX_SCALE_NONE",
                    global_scale=1.0,
                    axis_forward="Y",
                    axis_up="Z",
                    bake_space_transform=False,
                )
            finally:
                if previous_flag is None:
                    bpy.app.driver_namespace.pop(textureless_flag, None)
                else:
                    bpy.app.driver_namespace[textureless_flag] = previous_flag
    finally:
        for obj, hidden, hide_viewport in previous:
            obj.hide_viewport = hide_viewport
            obj.hide_set(hidden)
    if not path.is_file():
        raise ClusterAssemblyBuildError(f"FBX export produced no file: {path}")
    validation = _validate_textureless_fbx(
        bpy,
        path,
        full_skeleton_root=full_skeleton_root,
        expected_geometry_bounds=expected_geometry_bounds,
    )
    if addon_runtime is not None:
        validation["blender_addon_runtime"] = addon_runtime.receipt
    return validation


def _write_assembly_source_blend(bpy, path, objects, contract):
    """Write a standalone Full-SK-space Assembly authoring source.

    The raw ``SK_branch``/``SK_leaf`` cluster scenes are intentionally not
    included.  The saved scene contains only the generated base and repeated
    prototypes plus an embedded JSON contract, so opening or re-exporting it
    cannot overwrite the authoritative raw cluster assets by accident.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    library_path = target.with_name(target.stem + "__library.blend")
    collection = bpy.data.collections.new(target.stem + "_Objects")
    text = bpy.data.texts.new(target.stem + "_Contract.json")
    text.write(json.dumps(contract, ensure_ascii=False, indent=2))
    try:
        for obj in objects:
            collection.objects.link(obj)
        bpy.data.libraries.write(
            str(library_path),
            {collection, text},
            path_remap="RELATIVE_ALL",
            fake_user=True,
            compress=True,
        )
        bootstrap = "\n".join(
            [
                "import bpy, sys",
                "library, target, collection_name, text_name = sys.argv[sys.argv.index('--') + 1:]",
                "for obj in list(bpy.data.objects): bpy.data.objects.remove(obj, do_unlink=True)",
                "with bpy.data.libraries.load(library, link=False) as (source, destination):",
                "    destination.collections = [collection_name]",
                "    destination.texts = [text_name]",
                "collection = bpy.data.collections[collection_name]",
                "bpy.context.scene.collection.children.link(collection)",
                "bpy.context.scene.unit_settings.system = 'METRIC'",
                "bpy.context.scene.unit_settings.scale_length = 1.0",
                "bpy.context.scene.name = collection_name.rsplit('_Objects', 1)[0]",
                "bpy.ops.wm.save_as_mainfile(filepath=target, check_existing=False)",
            ]
        )
        completed = owned_run(
            [
                bpy.app.binary_path,
                "--factory-startup",
                "--background",
                "--python-expr",
                "exec(" + repr(bootstrap) + ")",
                "--",
                str(library_path),
                str(target),
                collection.name,
                text.name,
            ],
            source="sk_batch.cluster_assembly_builder.standalone_blender",
            run_factory=subprocess.run,
            check=False,
            capture_output=True,
            text=True,
        )
        if completed.returncode != 0 or not target.is_file():
            raise ClusterAssemblyBuildError(
                "standalone Assembly Blender source creation failed: "
                + (completed.stderr or completed.stdout or "unknown error")[-4000:]
            )
    finally:
        if collection.name in bpy.data.collections:
            bpy.data.collections.remove(collection, do_unlink=True)
        if text.name in bpy.data.texts:
            bpy.data.texts.remove(text, do_unlink=True)
        if library_path.is_file():
            library_path.unlink()
    return file_fingerprint(target)


def _role_builder_key(row):
    return str(row.get("provider_key") or row.get("role") or "").casefold()


def _role_material_polygons(
    merged_mesh,
    role_inputs,
    *,
    allow_topology_fallback=True,
):
    materials = list(merged_mesh.data.materials)
    slots_by_identity = defaultdict(set)
    for index, material in enumerate(materials):
        identity = normalize_role_identity(getattr(material, "name", ""))
        slots_by_identity[identity].add(index)
    result = {}
    prepared_unused = {}
    claimed_slots = {}
    missing_slot_rows = []
    for row in role_inputs:
        role = str(row["role"]).casefold()
        provider_key = _role_builder_key(row)
        identity_values = [
            row.get("role_identity"),
            *(row.get("role_identity_aliases") or []),
            *[
                assignment.get("material")
                for assignment in (row.get("assignments") or [])
                if isinstance(assignment, dict)
            ],
        ]
        identities = []
        for value in identity_values:
            identity = normalize_role_identity(value)
            if identity and identity not in identities:
                identities.append(identity)
        slots = {
            slot
            for identity in identities
            for slot in (slots_by_identity.get(identity) or set())
        }
        for slot in sorted(slots):
            owner = claimed_slots.get(slot)
            if owner is not None and owner != provider_key:
                raise ClusterAssemblyBuildError(
                    "Assembly material slot is claimed by multiple roles: "
                    f"slot={slot}, roles={owner},{provider_key}"
                )
            claimed_slots[slot] = provider_key
        polygons = [
            int(polygon.index)
            for polygon in merged_mesh.data.polygons
            if int(polygon.material_index) in slots
        ]
        if not slots:
            missing_slot_rows.append((provider_key, role, row, identities))
            continue
        if not polygons:
            normalized = deepcopy(row.get("normalized_variants"))
            variants = list((normalized or {}).get("variants") or [])
            prepared_unused[provider_key] = {
                "role": role,
                "provider_key": provider_key,
                "role_identity": row.get("role_identity"),
                "role_identity_aliases": deepcopy(
                    row.get("role_identity_aliases") or []
                ),
                "matched_material_identities": identities,
                "status": "prepared_unused",
                "reason": "material_has_no_rendered_polygons",
                "material_slots": sorted(slots),
                "normalized_variants": normalized,
                "prepared_variant_count": len(variants),
                "prepared_skeletal_assets": [
                    str(variant.get("skeletal_asset_name") or "")
                    for variant in variants
                    if str(variant.get("skeletal_asset_name") or "")
                ],
            }
            continue
        result[provider_key] = {
            "role": role,
            "provider_key": provider_key,
            "role_identity": row.get("role_identity"),
            "role_identity_aliases": deepcopy(
                row.get("role_identity_aliases") or []
            ),
            "matched_material_identities": identities,
            "material_slots": sorted(slots),
            "polygon_indices": polygons,
            "normalized_variants": deepcopy(row.get("normalized_variants")),
            "selection_basis": "material_identity",
        }

    # BWR can legally consolidate the final rendered mesh onto a canonical
    # material while preserving the role geometry and UV topology.  The PCG
    # handoff has already proven the role on its authoritative source FBX, so
    # absence of that *name* on the merged mesh is not evidence that the
    # geometry disappeared.  Let the existing normalized-prototype topology
    # matcher inspect only polygons not already claimed by an exact material
    # role.  It will still fail closed later when no normalized prototype
    # actually matches.
    claimed_polygons = {
        polygon
        for role_row in result.values()
        for polygon in role_row["polygon_indices"]
    }
    topology_candidates = [
        int(polygon.index)
        for polygon in merged_mesh.data.polygons
        if int(polygon.index) not in claimed_polygons
    ]
    for provider_key, role, row, identities in missing_slot_rows:
        normalized = deepcopy(row.get("normalized_variants"))
        variants = list((normalized or {}).get("variants") or [])
        assignments = [
            assignment
            for assignment in (row.get("assignments") or [])
            if (
                isinstance(assignment, dict)
                and int(assignment.get("used_polygon_count") or 0) > 0
            )
        ]
        if (
            allow_topology_fallback
            and topology_candidates
            and variants
            and (
                assignments
                or row.get("rendered_provider_expansion_covered") is True
            )
        ):
            result[provider_key] = {
                "role": role,
                "provider_key": provider_key,
                "role_identity": row.get("role_identity"),
                "role_identity_aliases": deepcopy(
                    row.get("role_identity_aliases") or []
                ),
                "matched_material_identities": identities,
                "material_slots": [],
                "polygon_indices": list(topology_candidates),
                "normalized_variants": normalized,
                "selection_basis": "normalized_topology_fallback",
            }
            continue
        prepared_unused[provider_key] = {
            "role": role,
            "provider_key": provider_key,
            "role_identity": row.get("role_identity"),
            "role_identity_aliases": deepcopy(
                row.get("role_identity_aliases") or []
            ),
            "matched_material_identities": identities,
            "status": "prepared_unused",
            "reason": "material_absent_from_rendered_mesh",
            "material_slots": [],
            "normalized_variants": normalized,
            "prepared_variant_count": len(variants),
            "prepared_skeletal_assets": [
                str(variant.get("skeletal_asset_name") or "")
                for variant in variants
                if str(variant.get("skeletal_asset_name") or "")
            ],
        }
    return result, prepared_unused


def _role_geometry_sources(bpy_module, final_merged_mesh, role_inputs):
    """Resolve rendered role geometry before BWR's intentional base split.

    Cluster/leaf placement meshes can remain in ``SpeedTree_Source`` without
    an armature and therefore do not belong to the repaired Full-SK base. Their
    exact material identity is nevertheless the authoritative Assembly
    placement geometry. Prefer those exact source objects before falling back
    to topology search on the final merged base.
    """
    exact_final, _unused = _role_material_polygons(
        final_merged_mesh,
        role_inputs,
        allow_topology_fallback=False,
    )
    roles = dict(exact_final)
    targets = {
        provider_key: final_merged_mesh
        for provider_key in exact_final
    }
    unresolved = [
        row for row in role_inputs
        if _role_builder_key(row) not in roles
    ]
    final_property_getter = getattr(final_merged_mesh, "get", None)
    source_fbx = _normalized_contract_path(
        final_property_getter("codex_source_fbx", "")
        if callable(final_property_getter)
        else ""
    )
    source_objects = []
    for obj in bpy_module.data.objects:
        if obj is final_merged_mesh or getattr(obj, "type", "") != "MESH":
            continue
        collections = {
            str(getattr(collection, "name", ""))
            for collection in getattr(obj, "users_collection", ()) or ()
        }
        if "SpeedTree_Source" not in collections:
            continue
        property_getter = getattr(obj, "get", None)
        tagged_fbx = _normalized_contract_path(
            property_getter("codex_source_fbx", "")
            if callable(property_getter)
            else ""
        )
        if source_fbx and tagged_fbx != source_fbx:
            continue
        source_objects.append(obj)

    still_unresolved = []
    for row in unresolved:
        provider_key = _role_builder_key(row)
        matches = []
        for obj in source_objects:
            rendered, _prepared = _role_material_polygons(
                obj,
                [row],
                allow_topology_fallback=False,
            )
            if provider_key in rendered:
                matches.append((obj, rendered[provider_key]))
        if len(matches) > 1:
            raise ClusterAssemblyBuildError(
                "Assembly provider material resolves to multiple current "
                f"SpeedTree source meshes: provider={provider_key}, "
                f"objects={sorted(obj.name for obj, _row in matches)}"
            )
        if len(matches) == 1:
            target, role_row = matches[0]
            role_row["selection_basis"] = (
                "speedtree_source_material_identity"
            )
            roles[provider_key] = role_row
            targets[provider_key] = target
        else:
            still_unresolved.append(row)

    prepared_unused = {}
    if still_unresolved:
        fallback, prepared_unused = _role_material_polygons(
            final_merged_mesh,
            still_unresolved,
            allow_topology_fallback=True,
        )
        roles.update(fallback)
        targets.update({
            provider_key: final_merged_mesh
            for provider_key in fallback
        })
    return roles, prepared_unused, targets


def _validate_role_component_claims(role_build_plans):
    claimed_polygons = {}
    unmatched_roles = []
    for provider_key, plan in role_build_plans.items():
        role = str(plan.get("role") or provider_key)
        target_identity = id(plan.get("target_object"))
        matched_component_count = 0
        for matched in (plan.get("matched") or {}).values():
            instances = list(matched.get("instances") or [])
            matched_component_count += len(instances)
            for component in instances:
                for polygon in component.get("polygons") or []:
                    polygon = int(polygon)
                    claim_key = (target_identity, polygon)
                    owner = claimed_polygons.get(claim_key)
                    if owner is not None and owner != provider_key:
                        raise ClusterAssemblyBuildError(
                            "one rendered mesh component matched multiple "
                            "normalized Assembly roles: "
                            f"polygon={polygon}, roles={owner},{provider_key}"
                        )
                    claimed_polygons[claim_key] = provider_key
        # A normalized provider can legitimately cover only a subset of the
        # rendered variants.  `_partition_normalized_render_components`
        # deliberately leaves unknown topologies in the Full-SK Base so no
        # geometry is invented or discarded.  Do not turn that preservation
        # result back into a provider-wide failure here.  A role with neither
        # a matched nor a preserved component is still invalid, and the final
        # builder also rejects a handoff where no provider produced any part.
        if matched_component_count == 0 and not plan.get("preserved"):
            unmatched_roles.append({
                "provider_key": str(provider_key),
                "preserved": deepcopy(plan.get("preserved") or []),
            })
    if unmatched_roles:
        unmatched_roles.sort(key=lambda row: row["provider_key"])
        raise ClusterAssemblyBuildError(
            "requested rendered Assembly roles matched zero normalized "
            "prototype components: "
            + ", ".join(row["provider_key"] for row in unmatched_roles)
            + "; diagnostics="
            + _canonical_json(unmatched_roles)
        )
    return claimed_polygons


def build_blender_assembly_inputs(
    handoff,
    final_armature,
    final_merged_mesh,
    output_dir,
    full_fbx_path,
    wind_json_path,
    *,
    pass_through_receipt_path=None,
    pass_through_target_contract=None,
    pass_through_target_spm=None,
):
    """Generate base/part FBXs and a strict builder manifest inside BWR.

    No ``.blend`` is created and the supplied Full FBX is never rewritten.
    """
    decision = content_build_decision(handoff)
    if decision == "pass_through":
        return build_receipt_pass_through_manifest(
            handoff,
            receipt_path=pass_through_receipt_path,
            target_contract=pass_through_target_contract,
            target_spm=pass_through_target_spm,
        )
    try:
        import bpy
    except ImportError as exc:  # pragma: no cover - only executes in Blender.
        raise ClusterAssemblyBuildError("Blender bpy is required") from exc
    if getattr(final_merged_mesh, "type", "") != "MESH":
        raise ClusterAssemblyBuildError("final BWR merged mesh is invalid")
    full_fingerprint_before = file_fingerprint(full_fbx_path)
    if not full_fingerprint_before["exists"]:
        raise ClusterAssemblyBuildError(f"existing Full SK FBX is missing: {full_fbx_path}")
    snapshot = snapshot_blender_armature(final_armature)
    checked_snapshot, skeleton_by_name = _skeleton_maps(snapshot)
    snapshot = checked_snapshot
    role_inputs = list((handoff.get("assembly") or {}).get("part_builder_inputs") or [])
    roles, prepared_unused_roles, role_targets = _role_geometry_sources(
        bpy,
        final_merged_mesh,
        role_inputs,
    )
    if prepared_unused_roles:
        details = [
            {
                "role": role,
                "role_identity": row.get("role_identity"),
                "role_identity_aliases": row.get("role_identity_aliases") or [],
                "matched_material_identities": (
                    row.get("matched_material_identities") or []
                ),
                "reason": row.get("reason"),
            }
            for _provider_key, row in sorted(prepared_unused_roles.items())
        ]
        raise ClusterAssemblyBuildError(
            "Assembly handoff requested rendered roles that disappeared before "
            "the Blender Assembly build: "
            + _canonical_json(details)
        )
    output = Path(output_dir)
    stem = Path(str((handoff.get("spm") or {}).get("path") or full_fbx_path)).stem
    base_asset_name = _public_base_name(stem)
    base_export_stem = base_asset_name
    base_fbx = output / f"{base_export_stem}.fbx"
    manifest_path = output / f"{stem}_cluster_assembly_bindings.json"
    assembly_source_blend = output / f"{stem}_NaniteAssemblySource.blend"
    created_objects = []
    parts = []
    all_bindings = []
    role_build_plans = {}
    preserved_render_components = []
    authored_node_table = None
    authored_spm_fingerprint = None
    claimed_authored_node_guids = set()
    authored_binding_count = 0
    degraded_authored_binding_count = 0
    legacy_fallback_binding_count = 0
    authored_component_cache = {}
    weighted_vertex_locator = None
    authored_assignment_report = None
    base_obj = None
    base_armature = None
    scene_units = bpy.context.scene.unit_settings
    source_unit_system = str(scene_units.system)
    source_scale_length = float(scene_units.scale_length)
    source_centimeters_per_blender_unit = (
        100.0 * source_scale_length if source_unit_system != "NONE" else 100.0
    )
    if source_unit_system != "METRIC" or abs(source_scale_length - 1.0) > 1.0e-9:
        raise ClusterAssemblyBuildError(
            "Assembly generation requires the final Full SK meter-space scene "
            f"contract, got system={source_unit_system} scale={source_scale_length}"
        )
    try:
        provider_order = sorted(
            roles,
            key=lambda provider_key: (
                ROLE_ORDER.index(roles[provider_key]["role"]),
                provider_key,
            ),
        )
        for provider_key in provider_order:
            role_row = roles[provider_key]
            role = role_row["role"]
            target_object = role_targets[provider_key]
            normalized_contract = role_row.get("normalized_variants")
            if not normalized_contract:
                raise ClusterAssemblyBuildError(
                    f"{role} has no external normalized variants; "
                    "component-derived tiny-part fallback is disabled"
                )
            validate_file_fingerprint(
                normalized_contract.get("manifest"),
                f"Atlas normalized variant manifest for {role}",
            )
            validate_file_fingerprint(
                normalized_contract.get("source_blend"),
                f"Send to Unreal normalized source blend for {role}",
            )
            prototypes, imported_objects = _import_normalized_plan_prototypes(
                bpy,
                normalized_contract,
            )
            created_objects.extend(imported_objects)
            components = _component_groups(
                target_object.data,
                role_row["polygon_indices"],
            )
            matched, preserved = _partition_normalized_render_components(
                prototypes,
                target_object.data,
                components,
            )
            if preserved and target_object is not final_merged_mesh:
                raise ClusterAssemblyBuildError(
                    "current SpeedTree source role contains geometry absent "
                    "from its normalized provider plans: "
                    f"provider={provider_key}, preserved={preserved}"
                )
            role_build_plans[provider_key] = {
                "role": role,
                "provider_key": provider_key,
                "target_object": target_object,
                "matched": matched,
                "preserved": preserved,
            }
            for row in preserved:
                preserved_render_components.append({
                    "role": role,
                    "provider_key": provider_key,
                    "role_identity": role_row["role_identity"],
                    **row,
                })
        _validate_role_component_claims(role_build_plans)
        authored_spm_fingerprint = validate_file_fingerprint(
            handoff.get("spm"),
            "target SPM authored Node placement",
        )
        try:
            authored_node_table = parse_spm_authored_placement(
                authored_spm_fingerprint["path"]
            )
        except (KeyError, SpmAuthoredPlacementError) as exc:
            raise ClusterAssemblyBuildError(
                "target SPM authored Node placement is invalid: " + str(exc)
            ) from exc
        if authored_node_table.get("available"):
            component_rows = []
            for provider_key, role_plan in sorted(
                role_build_plans.items()
            ):
                role = role_plan["role"]
                target_object = role_plan["target_object"]
                for signature, match in sorted(
                    role_plan["matched"].items()
                ):
                    prototype = match["prototype"]
                    variant = prototype["variant"]
                    source_obj = prototype["object"]
                    source_component = prototype["component"]
                    try:
                        target_mesh_id = int(variant.get("target_mesh_id"))
                    except (TypeError, ValueError) as exc:
                        raise ClusterAssemblyBuildError(
                            "normalized variant has no target mesh id for "
                            "authored Node matching"
                        ) from exc
                    for instance_index, component in enumerate(
                        match["instances"]
                    ):
                        component_id = (
                            f"{provider_key}:{signature}:{instance_index}:"
                            f"{component['polygons'][0]}"
                        )
                        attachment = _attachment_point_correspondence(
                            source_obj,
                            source_component,
                            target_object,
                            component,
                            variant.get("attachment_vertex_uv"),
                        )
                        source_attachment_index = attachment["source_index"]
                        target_attachment_index = attachment["target_index"]
                        source_attachment = _world_coordinate(
                            source_obj,
                            attachment["source_coordinate"],
                        )
                        target_attachment = _world_coordinate(
                            target_object,
                            attachment["target_coordinate"],
                        )
                        authored_component_cache[component_id] = {
                            "source_attachment_index": (
                                source_attachment_index
                            ),
                            "target_attachment_index": (
                                target_attachment_index
                            ),
                            "source_attachment": source_attachment,
                            "target_attachment": target_attachment,
                            "attachment_correspondence": attachment["evidence"],
                        }
                        component_rows.append({
                            "component_id": component_id,
                            "target_mesh_id": target_mesh_id,
                            "position_meters": target_attachment,
                        })
            authored_assignment_report = (
                assign_authored_nodes_to_components(
                    authored_node_table,
                    component_rows,
                    AUTHORED_NODE_GLOBAL_ASSIGNMENT_TOLERANCE_METERS,
                )
            )
            for component_id, authored_node in (
                authored_assignment_report["assignments"].items()
            ):
                authored_component_cache[component_id]["authored_node"] = (
                    authored_node
                )
            unmatched_by_id = {
                row["component_id"]: row
                for row in authored_assignment_report["unmatched"]
            }
            for component_id, warning in unmatched_by_id.items():
                authored_component_cache[component_id][
                    "authored_node_match_warning"
                ] = warning
        else:
            authored_assignment_report = {
                "policy": "legacy_authored_node_data_absent",
                "threshold_meters": None,
                "component_count": 0,
                "candidate_count": 0,
                "assigned_count": 0,
                "unmatched_count": 0,
                "unmatched": [],
            }
        # No public Assembly artifact may exist until every requested rendered
        # role has proven at least one normalized prototype/component match.
        output.mkdir(parents=True, exist_ok=True)
        excluded_polygons = _base_role_polygon_indices(
            role_build_plans,
            roles,
            final_merged_mesh,
        )
        base_obj = _copy_base_without_role_polygons(
            bpy,
            final_merged_mesh,
            excluded_polygons,
            base_export_stem,
        )
        base_armature = _copy_normalized_base_armature(
            bpy,
            final_armature,
            base_export_stem + "_Armature",
        )
        created_objects.extend([base_obj, base_armature])
        base_export_parents = _replicate_full_export_parent_chain(
            bpy,
            final_merged_mesh,
            final_armature,
            base_obj,
            base_armature,
        )
        created_objects.extend(base_export_parents)
        for modifier in base_obj.modifiers:
            if modifier.type == "ARMATURE":
                modifier.object = base_armature
        base_weighted_bones = _weighted_bones_for_base(
            base_obj,
            snapshot,
            skeleton_by_name,
        )
        source_armature_name = final_armature.name
        normalized_armature_name = base_armature.name
        final_armature.name = source_armature_name + "__FullSource"
        base_armature.name = source_armature_name
        try:
            _validate_base_export_parent_chain(
                base_armature,
                base_export_parents,
                base_obj,
            )
            base_fbx_texture_contract = _export_selected_fbx(
                bpy,
                base_fbx,
                [base_armature, base_obj],
                full_skeleton_root=True,
            )
        finally:
            base_armature.name = normalized_armature_name
            final_armature.name = source_armature_name
        if any(
            target is not final_merged_mesh
            for target in role_targets.values()
        ):
            weighted_vertex_locator = _weighted_vertex_locator(
                final_merged_mesh
            )
        for provider_key in provider_order:
            role_row = roles[provider_key]
            role = role_row["role"]
            target_object = role_targets[provider_key]
            normalized_contract = role_row.get("normalized_variants")
            if normalized_contract:
                composite_accumulators = {}
                for signature, match in sorted(
                    role_build_plans[provider_key]["matched"].items()
                ):
                    prototype = match["prototype"]
                    instances = match["instances"]
                    variant = prototype["variant"]
                    source_obj = prototype["object"]
                    source_component = prototype["component"]
                    ordinal = int(variant.get("ordinal") or 0)
                    part_asset_name = str(
                        variant.get("skeletal_asset_name") or ""
                    )
                    if ordinal <= 0 or not part_asset_name:
                        raise ClusterAssemblyBuildError(
                            "normalized variant has no ordinal/skeletal asset name"
                        )
                    try:
                        attachment_vertex_index = int(
                            variant.get("attachment_vertex_index")
                        )
                    except (TypeError, ValueError) as exc:
                        raise ClusterAssemblyBuildError(
                            "normalized variant has no authored attachment vertex"
                        ) from exc
                    attachment_vertex_uv = variant.get(
                        "attachment_vertex_uv"
                    )
                    if attachment_vertex_index < 0:
                        raise ClusterAssemblyBuildError(
                            "normalized variant has no authored attachment vertex"
                        )
                    prototype_id = (
                        f"{role}_normalized_{ordinal:02d}_{signature}"
                    )
                    try:
                        target_mesh_id = int(variant.get("target_mesh_id"))
                    except (TypeError, ValueError) as exc:
                        raise ClusterAssemblyBuildError(
                            "normalized variant has no target mesh id for "
                            "authored Node matching"
                        ) from exc
                    bindings = []
                    for instance_index, component in enumerate(instances):
                        component_id = (
                            f"{provider_key}:{signature}:{instance_index}:"
                            f"{component['polygons'][0]}"
                        )
                        cached_placement = authored_component_cache.get(
                            component_id
                        )
                        if cached_placement is not None:
                            source_attachment_index = cached_placement[
                                "source_attachment_index"
                            ]
                            target_attachment_index = cached_placement[
                                "target_attachment_index"
                            ]
                            source_attachment = cached_placement[
                                "source_attachment"
                            ]
                            target_attachment = cached_placement[
                                "target_attachment"
                            ]
                            attachment_correspondence = cached_placement[
                                "attachment_correspondence"
                            ]
                        else:
                            attachment = _attachment_point_correspondence(
                                source_obj,
                                source_component,
                                target_object,
                                component,
                                attachment_vertex_uv,
                            )
                            source_attachment_index = attachment[
                                "source_index"
                            ]
                            target_attachment_index = attachment[
                                "target_index"
                            ]
                            source_attachment = _world_coordinate(
                                source_obj,
                                attachment["source_coordinate"],
                            )
                            target_attachment = _world_coordinate(
                                target_object,
                                attachment["target_coordinate"],
                            )
                            attachment_correspondence = attachment["evidence"]
                        authored_node = (
                            (cached_placement or {}).get("authored_node")
                        )
                        authored_match_warning = (
                            (cached_placement or {}).get(
                                "authored_node_match_warning"
                            )
                        )
                        if authored_node_table.get("available"):
                            if authored_node is not None:
                                claimed_authored_node_guids.add(
                                    authored_node["node_guid"]
                                )
                                fit_target_attachment = list(
                                    authored_node["position_meters"]
                                )
                                placement_source = (
                                    "authored_spm_node_translation_and_state__"
                                    "surviving_root_tip_rotation_uniform_scale"
                                )
                                authored_binding_count += 1
                            else:
                                fit_target_attachment = target_attachment
                                placement_source = (
                                    "render_attachment_translation__"
                                    "authored_node_assignment_degraded"
                                )
                                degraded_authored_binding_count += 1
                            fit_source_attachment = [0.0, 0.0, 0.0]
                        else:
                            fit_target_attachment = target_attachment
                            fit_source_attachment = source_attachment
                            placement_source = (
                                "legacy_post_cull_similarity_fallback__"
                                "authored_node_data_absent"
                            )
                            legacy_fallback_binding_count += 1
                        correspondence_error = None
                        try:
                            (
                                source_indices,
                                target_indices,
                                correspondence_evidence,
                            ) = _ordered_cross_object_correspondence(
                                source_obj,
                                source_component,
                                target_object,
                                component,
                                include_evidence=True,
                            )
                            source_world = _world_points(
                                source_obj,
                                source_indices,
                            )
                            target_world = _world_points(
                                target_object,
                                target_indices,
                            )
                        except ClusterAssemblyBuildError as exc:
                            if not authored_node_table.get("available"):
                                raise
                            correspondence_error = str(exc)
                            correspondence_evidence = {
                                "policy": (
                                    "attachment_locked_export_safe_degraded_v1"
                                ),
                                "error": correspondence_error,
                            }
                            source_world = _world_points(
                                source_obj, source_component["vertices"]
                            )
                            target_world = []
                        source_diagonal = max(
                            math.dist(
                                [
                                    min(point[axis] for point in source_world)
                                    for axis in range(3)
                                ],
                                [
                                    max(point[axis] for point in source_world)
                                    for axis in range(3)
                                ],
                            ),
                            1.0e-9,
                        )
                        if (
                            math.dist(source_attachment, (0.0, 0.0, 0.0))
                            > max(source_diagonal * 1.0e-6, 1.0e-9)
                        ):
                            raise ClusterAssemblyBuildError(
                                "normalized plan attachment pivot is not at "
                                "the authored local origin"
                            )
                        if authored_node_table.get("available"):
                            if correspondence_error is not None:
                                transform = attachment_locked_safe_transform(
                                    fit_target_attachment,
                                    correspondence_error,
                                )
                            else:
                                try:
                                    transform = (
                                        derive_endpoint_uniform_similarity_transform(
                                            source_world,
                                            target_world,
                                            source_attachment=(
                                                fit_source_attachment
                                            ),
                                            target_attachment=(
                                                fit_target_attachment
                                            ),
                                        )
                                    )
                                except ClusterAssemblyBuildError as exc:
                                    try:
                                        transform = (
                                            fit_uniform_similarity_transform(
                                                source_world,
                                                target_world,
                                                source_attachment=(
                                                    fit_source_attachment
                                                ),
                                                target_attachment=(
                                                    fit_target_attachment
                                                ),
                                            )
                                        )
                                        transform["construction_mode"] = (
                                            "bounded_similarity_after_endpoint_"
                                            "frame_failure_v1"
                                        )
                                        transform[
                                            "placement_quality_warning"
                                        ] = str(exc)
                                    except ClusterAssemblyBuildError as fit_exc:
                                        transform = (
                                            attachment_locked_safe_transform(
                                                fit_target_attachment,
                                                f"endpoint={exc}; fit={fit_exc}",
                                            )
                                        )
                        else:
                            transform = fit_uniform_similarity_transform(
                                source_world,
                                target_world,
                                source_attachment=fit_source_attachment,
                                target_attachment=fit_target_attachment,
                            )
                        transform["placement_source"] = placement_source
                        transform["correspondence_evidence"] = (
                            correspondence_evidence
                        )
                        transform["attachment_correspondence"] = deepcopy(
                            attachment_correspondence
                        )
                        if authored_node is not None:
                            transform["attachment_pivot_error_scope"] = (
                                "authored_spm_node_absolute_translation"
                            )
                            transform["authored_node"] = {
                                key: deepcopy(authored_node[key])
                                for key in (
                                    "node_guid",
                                    "generator_guid",
                                    "parent_guid",
                                    "anchor_index",
                                    "position_speedtree_units",
                                    "position_meters",
                                    "hidden",
                                    "valid_position",
                                    "deleted",
                                    "culled",
                                    "match_evidence",
                                )
                            }
                            transform[
                                "authored_to_render_attachment_error_meters"
                            ] = math.dist(
                                fit_target_attachment,
                                target_attachment,
                            )
                        elif authored_match_warning is not None:
                            transform["authored_node_match_warning"] = (
                                deepcopy(authored_match_warning)
                            )
                        transform["attachment_vertex_index"] = (
                            attachment_vertex_index
                        )
                        transform["attachment_vertex_uv"] = [
                            float(value)
                            for value in attachment_vertex_uv
                        ]
                        gate_assembly_transform_residuals(
                            transform,
                            {
                                "full_asset": stem,
                                "part_asset": part_asset_name,
                                "prototype_id": prototype_id,
                                "role": role,
                                "card_ordinal": ordinal,
                                "instance": instance_index,
                                "target_mesh_id": target_mesh_id,
                                "placement_source": placement_source,
                                "authored_node_guid": (
                                    authored_node.get("node_guid")
                                    if authored_node is not None
                                    else None
                                ),
                                "component_polygon_indices": (
                                    component["polygons"]
                                ),
                                "correspondence_policy": (
                                    correspondence_evidence["policy"]
                                ),
                            },
                            block_geometry=(
                                not authored_node_table.get("available")
                            ),
                        )
                        if target_object is final_merged_mesh:
                            influences = _component_influences(
                                target_object,
                                component,
                            )
                            influence_source = {
                                "policy": "render_component_weights_v1",
                            }
                        else:
                            influences, influence_source = (
                                _nearest_weighted_vertex_influences(
                                    final_merged_mesh,
                                    weighted_vertex_locator,
                                    target_attachment,
                                )
                            )
                        binding = {
                            "instance": instance_index,
                            "card_ordinal": ordinal,
                            "component_polygon_indices": component["polygons"],
                            "transform": transform,
                            "bone_influences": influences,
                            "bone_influence_source": influence_source,
                        }
                        hierarchy = validate_binding_hierarchy(
                            binding,
                            snapshot,
                            skeleton_by_name=skeleton_by_name,
                        )
                        binding["anchor_bone"] = hierarchy["anchor_bone"]
                        bindings.append(binding)
                    composite_parts = list(variant.get("composite_parts") or [])
                    if composite_parts:
                        if str(variant.get("source_partition_mode") or "") != "COMPOSITE_PER_DEFORM_ROOT":
                            raise ClusterAssemblyBuildError(
                                "normalized composite parts require COMPOSITE_PER_DEFORM_ROOT"
                            )
                        for expected_subpart, subpart in enumerate(composite_parts, 1):
                            subpart_index = int(subpart.get("subpart_index") or 0)
                            subpart_asset = str(
                                subpart.get("skeletal_asset_name") or ""
                            )
                            if (
                                subpart_index != expected_subpart
                                or not subpart_asset.casefold().startswith("sk_")
                            ):
                                raise ClusterAssemblyBuildError(
                                    "normalized composite subparts are not consecutive SK assets"
                                )
                            expected_normalized_bounds = (
                                _expected_normalized_bounds_for_variant(
                                    normalized_contract,
                                    subpart,
                                    subpart_asset,
                                    subpart_index,
                                )
                            )
                            key = (subpart_index, subpart_asset)
                            accumulator = composite_accumulators.get(key)
                            if accumulator is None:
                                external_source = {
                                    "kind": "send_to_unreal_normalized_skeletal_part",
                                    "unreal_relative_folder": str(
                                        variant.get("unreal_relative_folder") or "Cluster"
                                    ),
                                    "source_blend": deepcopy(
                                        normalized_contract["source_blend"]
                                    ),
                                    "plan_fbx": deepcopy(variant["plan_fbx"]),
                                    "plan_name": str(variant.get("plan_name") or ""),
                                    "ordinal": ordinal,
                                    "card_ordinals": [],
                                    "source_prototype_index": subpart_index,
                                    "source_partition_mode": "COMPOSITE_PER_DEFORM_ROOT",
                                    "source_bone": str(subpart.get("source_bone") or ""),
                                    "endpoint_bone": str(subpart.get("endpoint_bone") or ""),
                                    "pivot_contract": str(
                                        subpart.get("pivot_contract")
                                        or variant.get("pivot_contract")
                                        or ""
                                    ),
                                }
                                if expected_normalized_bounds is not None:
                                    external_source["expected_normalized_bounds"] = (
                                        expected_normalized_bounds
                                    )
                                    external_source[
                                        "expected_normalized_bounds_source"
                                    ] = "normalized_variant_build_metadata"
                                accumulator = {
                                    "prototype_id": (
                                        f"{role}_composite_{subpart_index:02d}_"
                                        + hashlib.sha1(subpart_asset.encode("utf-8")).hexdigest()[:16]
                                    ),
                                    "asset_name": subpart_asset,
                                    "export_stem": subpart_asset,
                                    "role": role,
                                    "provider_key": provider_key,
                                    "role_identity": role_row["role_identity"],
                                    "topology_signature": "composite_subpart",
                                    "logical_group_index": ROLE_ORDER.index(role),
                                    "logical_subpart_index": subpart_index,
                                    "external_source": external_source,
                                    "template": {
                                        "vertex_count": 0,
                                        "polygon_count": 0,
                                        "center": [0.0, 0.0, 0.0],
                                    },
                                    "bindings": [],
                                    "fit_summary": {
                                        "fit_mode": "uniform_similarity_3d_composite_subpart",
                                    },
                                }
                                composite_accumulators[key] = accumulator
                            else:
                                recorded_bounds = (
                                    accumulator["external_source"].get(
                                        "expected_normalized_bounds"
                                    )
                                )
                                if (
                                    expected_normalized_bounds is not None
                                    and recorded_bounds is not None
                                    and _canonical_json(expected_normalized_bounds)
                                    != _canonical_json(recorded_bounds)
                                ):
                                    raise ClusterAssemblyBuildError(
                                        "normalized composite prototype bounds "
                                        "changed across card variants: "
                                        + subpart_asset
                                    )
                            card_ordinals = accumulator["external_source"]["card_ordinals"]
                            if ordinal not in card_ordinals:
                                card_ordinals.append(ordinal)
                            for base_binding in bindings:
                                composite_binding = deepcopy(base_binding)
                                composite_binding["instance"] = len(
                                    accumulator["bindings"]
                                )
                                composite_binding["card_ordinal"] = ordinal
                                composite_binding["composite_subpart_index"] = subpart_index
                                composite_binding["transform"] = (
                                    compose_similarity_with_relative_matrix(
                                        base_binding["transform"],
                                        subpart.get("subpart_to_card_matrix"),
                                    )
                                )
                                base_gate_evidence = deepcopy(
                                    (
                                        base_binding["transform"].get(
                                            "residual_gate"
                                        )
                                        or {}
                                    ).get("evidence")
                                    or {}
                                )
                                base_gate_evidence.update({
                                    "part_asset": subpart_asset,
                                    "composite_subpart_index": subpart_index,
                                    "residual_scope": (
                                        "parent_card_fit_before_composite"
                                    ),
                                })
                                gate_assembly_transform_residuals(
                                    composite_binding["transform"],
                                    base_gate_evidence,
                                    block_geometry=bool(
                                        (
                                            base_binding["transform"].get(
                                                "residual_gate"
                                            )
                                            or {}
                                        ).get("geometry_residual_blocking")
                                    ),
                                )
                                accumulator["bindings"].append(composite_binding)
                                all_bindings.append(composite_binding)
                        continue
                    all_bindings.extend(bindings)
                    external_source = {
                        "kind": "send_to_unreal_normalized_skeletal_part",
                        "unreal_relative_folder": str(
                            variant.get("unreal_relative_folder") or "Cluster"
                        ),
                        "source_blend": deepcopy(
                            normalized_contract["source_blend"]
                        ),
                        "plan_fbx": deepcopy(variant["plan_fbx"]),
                        "plan_name": str(variant.get("plan_name") or ""),
                        "ordinal": ordinal,
                        "source_prototype_index": (
                            int(variant["source_prototype_index"])
                            if variant.get("source_prototype_index")
                            else None
                        ),
                        "source_partition_mode": str(
                            variant.get("source_partition_mode") or ""
                        ),
                        "pivot_contract": str(
                            variant.get("pivot_contract") or ""
                        ),
                    }
                    expected_normalized_bounds = (
                        _expected_normalized_bounds_for_variant(
                            normalized_contract,
                            variant,
                            part_asset_name,
                            ordinal,
                        )
                    )
                    if expected_normalized_bounds is not None:
                        external_source["expected_normalized_bounds"] = (
                            expected_normalized_bounds
                        )
                        external_source[
                            "expected_normalized_bounds_source"
                        ] = "normalized_variant_build_metadata"
                    parts.append({
                        "prototype_id": prototype_id,
                        "asset_name": part_asset_name,
                        "export_stem": part_asset_name,
                        "role": role,
                        "provider_key": provider_key,
                        "role_identity": role_row["role_identity"],
                        "topology_signature": signature,
                        "external_source": external_source,
                        "template": {
                            "vertex_count": len(source_component["vertices"]),
                            "polygon_count": len(source_component["polygons"]),
                            "center": [0.0, 0.0, 0.0],
                        },
                        "bindings": bindings,
                        "fit_summary": _assembly_fit_summary(
                            bindings,
                            "uniform_similarity_3d",
                        ),
                    })
                for key in sorted(composite_accumulators):
                    accumulator = composite_accumulators[key]
                    fit_rows = accumulator["bindings"]
                    accumulator["fit_summary"] = _assembly_fit_summary(
                        fit_rows,
                        "uniform_similarity_3d_composite_subpart",
                    )
                    parts.append(accumulator)
                continue
            raise ClusterAssemblyBuildError(
                f"{role} has no external normalized variants; "
                "component-derived tiny-part fallback is disabled"
            )
        # Wind contract fields are useful evidence, but generated assets must not be
        # rejected because a previous handoff recorded a different hash/preset/pose.
        # The Unreal importer and the final User Data check are the runtime authority.
        wind_validation = {
            "wind_json": file_fingerprint(wind_json_path),
            "contract_fields_are_diagnostic": True,
        }
        parts = _coalesce_normalized_external_parts(parts)
        if not parts:
            raise ClusterAssemblyBuildError(
                "Assembly handoff requested rendered roles, but no normalized "
                "prototype matched any rendered mesh component"
            )
        registered_variants = []
        used_variant_keys = set()
        for part in parts:
            external = part.get("external_source") or {}
            if not external:
                continue
            ordinals = list(external.get("card_ordinals") or [])
            if not ordinals:
                ordinals = [int(external.get("ordinal") or 0)]
            used_variant_keys.update(
                (
                    str(
                        part.get("provider_key")
                        or part.get("role")
                        or ""
                    ).casefold(),
                    int(ordinal),
                )
                for ordinal in ordinals
                if int(ordinal) > 0
            )
        registered_role_rows = dict(prepared_unused_roles)
        registered_role_rows.update(roles)
        for provider_key, role_row in sorted(
            registered_role_rows.items()
        ):
            role = str(role_row.get("role") or "").casefold()
            normalized = role_row.get("normalized_variants") or {}
            for variant in normalized.get("variants") or []:
                ordinal = int(variant.get("ordinal") or 0)
                registered_variants.append({
                    "role": role,
                    "provider_key": provider_key,
                    "ordinal": ordinal,
                    "card_name": str(variant.get("plan_name") or ""),
                    "skeletal_asset_name": str(
                        variant.get("skeletal_asset_name") or ""
                    ),
                    "source_prototype_index": (
                        int(variant["source_prototype_index"])
                        if variant.get("source_prototype_index")
                        else None
                    ),
                    "source_partition_mode": str(
                        variant.get("source_partition_mode") or ""
                    ),
                    "composite_parts": deepcopy(
                        variant.get("composite_parts") or []
                    ),
                    "target_mesh_id": int(variant.get("target_mesh_id") or 0),
                    "pivot_contract": str(
                        variant.get("pivot_contract") or ""
                    ),
                    "attachment_vertex_index": int(
                        variant.get("attachment_vertex_index")
                    ),
                    "attachment_vertex_uv": [
                        float(value)
                        for value in variant.get("attachment_vertex_uv")
                    ],
                    "instanced": (
                        provider_key.casefold(), ordinal
                    ) in used_variant_keys,
                })
        authored_spm_after = validate_file_fingerprint(
            handoff.get("spm"),
            "target SPM authored Node placement",
        )
        if authored_spm_after != authored_spm_fingerprint:
            raise ClusterAssemblyBuildError(
                "target SPM changed while authored placement was built"
            )
        persisted_residual_gates = [
            (binding.get("transform") or {}).get("residual_gate") or {}
            for part in parts
            for binding in part.get("bindings") or []
        ]
        residual_warning_count = sum(
            gate.get("status") == "warning"
            for gate in persisted_residual_gates
        )
        placement_contract = {
            "version": PLACEMENT_CONTRACT_VERSION,
            "status": "ready",
            "source_spm": authored_spm_fingerprint,
            "authored_node_table": {
                key: deepcopy(authored_node_table.get(key))
                for key in (
                    "status",
                    "available",
                    "unit_contract",
                    "meters_per_speedtree_unit",
                    "node_count",
                    "active_node_count",
                    "excluded_node_count",
                    "excluded_reason_counts",
                )
                if key in authored_node_table
            },
            "candidate_policy": (
                "deterministic_state_mesh_then_global_position_recovery_"
                "one_to_one_v3"
                if authored_node_table.get("available")
                else "legacy_only_when_authored_node_data_absent_v1"
            ),
            "translation_source": (
                "authored_spm_node_absolute_position_or_render_attachment_"
                "when_bounded_assignment_is_unavailable"
                if authored_node_table.get("available")
                else "legacy_post_cull_geometry_attachment"
            ),
            "rotation_uniform_scale_source": (
                "surviving_root_tip_frame_then_bounded_similarity_then_"
                "identity_unit_scale_safe_fallback"
            ),
            "node_fields_do_not_serialize_rotation_or_uniform_scale": True,
            "authored_card_binding_count": authored_binding_count,
            "degraded_authored_card_binding_count": (
                degraded_authored_binding_count
            ),
            "legacy_fallback_card_binding_count": (
                legacy_fallback_binding_count
            ),
            "claimed_authored_node_count": len(
                claimed_authored_node_guids
            ),
            "authored_node_match_threshold_meters": (
                AUTHORED_NODE_GLOBAL_ASSIGNMENT_TOLERANCE_METERS
            ),
            "authored_node_assignment": {
                key: deepcopy(authored_assignment_report.get(key))
                for key in (
                    "policy",
                    "threshold_meters",
                    "component_count",
                    "candidate_count",
                    "global_candidate_count",
                    "bounded_assigned_count",
                    "state_mesh_out_of_tolerance_recovery_count",
                    "global_bounded_recovery_count",
                    "global_out_of_tolerance_recovery_count",
                    "recovered_out_of_tolerance_count",
                    "maximum_recovered_distance_meters",
                    "assigned_count",
                    "unmatched_count",
                    "unmatched",
                )
            },
            "residual_ready_gate": {
                "status": (
                    "pass_with_quality_warnings"
                    if residual_warning_count
                    else "pass"
                ),
                "policy": (
                    "hard_authored_pivot_gate_with_recorded_geometry_quality_"
                    "warning_v1"
                ),
                "threshold": MAX_ASSEMBLY_RESIDUAL_RELATIVE_RMS,
                "authored_pivot_error_threshold_meters": (
                    MAX_ASSEMBLY_PIVOT_ERROR_METERS
                ),
                "asset_special_cases": False,
                "tolerance_widening": False,
                "authored_geometry_residual_blocks_export": False,
                "legacy_geometry_residual_blocks_export": True,
                "binding_count": len(persisted_residual_gates),
                "quality_warning_binding_count": residual_warning_count,
                "maximum_relative_rms": max(
                    (
                        float(gate.get("maximum_relative_rms") or 0.0)
                        for gate in persisted_residual_gates
                    ),
                    default=0.0,
                ),
            },
        }
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "kind": MANIFEST_KIND,
            "status": "ready",
            "content_decision": "build",
            "full_skeletal_mesh_preserved": True,
            "full_asset_stem": stem,
            "full_fbx": full_fingerprint_before,
            "base": {
                "asset_name": base_asset_name,
                "export_stem": base_export_stem,
                "fbx": file_fingerprint(base_fbx),
                "fbx_texture_contract": base_fbx_texture_contract,
                "excluded_role_polygon_count": len(excluded_polygons),
                "unmatched_role_components_removed_from_base": sum(
                    int(row.get("polygon_count") or 0)
                    for row in preserved_render_components
                    if role_build_plans[row["provider_key"]][
                        "target_object"
                    ] is final_merged_mesh
                ),
                "final_armature": final_armature.name,
                "weighted_bones": base_weighted_bones,
                "weighted_bone_count": len(base_weighted_bones),
                "all_weighted_bones_in_final_wind": "diagnostic_only",
            },
            "parts": parts,
            "registered_variants": registered_variants,
            "prepared_unused_roles": [
                prepared_unused_roles[role]
                for role in ROLE_ORDER
                if role in prepared_unused_roles
            ],
            "preserved_render_components": preserved_render_components,
            "placement_contract": placement_contract,
            "final_skeleton": snapshot,
            "wind_contract": wind_validation,
            "coordinate_contract": {
                "source": "Blender world, Z-up, right-handed",
                "target": "Unreal local, Z-up, left-handed, centimeters",
                "centimeters_per_blender_unit": source_centimeters_per_blender_unit,
                "source_unit_system": source_unit_system,
                "source_scale_length": source_scale_length,
                "generated_fbx_unit_scale_factor": 100.0,
                "translation_axis_map": ["x", "-y", "z"],
                "rotation_quaternion_axis_map": ["-x", "y", "-z", "w"],
                "transform_space": "Local",
            },
            "role_contract": {
                "role_material_identities": [
                    str(row.get("role_identity") or "")
                    for _role, row in sorted(registered_role_rows.items())
                ],
                "registered_card_variants": [
                    row["card_name"] for row in registered_variants
                ],
                "registered_skeletal_variants": [
                    row["skeletal_asset_name"] for row in registered_variants
                ],
                "assembly_prototypes": [
                    part["asset_name"] for part in parts
                ],
                "raw_cluster_assets_are_not_assembly_prototypes": True,
            },
            "handoff_evidence": {
                "actual_fbx": handoff.get("actual_fbx"),
                "pcg_receipt": handoff.get("pcg_receipt"),
                "spm": handoff.get("spm"),
                "roles": roles,
                "prepared_unused_roles": prepared_unused_roles,
                "preserved_render_components": preserved_render_components,
            },
        }
        _validate_public_export_names(manifest)
        manifest["assembly_source_blend"] = _write_assembly_source_blend(
            bpy,
            assembly_source_blend,
            created_objects,
            manifest,
        )
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        manifest["manifest"] = file_fingerprint(manifest_path)
    finally:
        for obj in reversed(created_objects):
            data = getattr(obj, "data", None)
            object_type = str(getattr(obj, "type", ""))
            if obj.name in bpy.data.objects:
                bpy.data.objects.remove(obj, do_unlink=True)
            if data is not None and getattr(data, "users", 1) == 0:
                collection = (
                    bpy.data.armatures
                    if object_type == "ARMATURE"
                    else bpy.data.meshes
                )
                if data.name in collection:
                    collection.remove(data)
        scene_units.system = source_unit_system
        scene_units.scale_length = source_scale_length
    full_fingerprint_after = file_fingerprint(full_fbx_path)
    if full_fingerprint_after != full_fingerprint_before:
        raise ClusterAssemblyBuildError("existing Full SK FBX changed during Assembly build")
    return manifest


def validate_unreal_asset_contract(manifest, asset_contract):
    if (manifest or {}).get("status") != "ready":
        raise ClusterAssemblyBuildError("Assembly manifest is not ready")
    public_names = _validate_public_export_names(manifest)
    required = ("full_skeletal_mesh", "base_skeletal_mesh", "assembly")
    for key in required:
        value = str((asset_contract or {}).get(key) or "")
        if not value.startswith("/Game/"):
            raise ClusterAssemblyBuildError(f"invalid Unreal asset path for {key}: {value}")
    if asset_contract["full_skeletal_mesh"] in {
        asset_contract["base_skeletal_mesh"],
        asset_contract["assembly"],
    }:
        raise ClusterAssemblyBuildError(
            "Full Skeletal Mesh must remain separate from base and Assembly assets"
        )
    part_paths = asset_contract.get("parts") or {}
    expected = {row["prototype_id"] for row in manifest.get("parts") or []}
    if set(part_paths) != expected:
        raise ClusterAssemblyBuildError(
            "Unreal part asset paths do not exactly match Blender prototypes"
        )
    if any(not str(path).startswith("/Game/") for path in part_paths.values()):
        raise ClusterAssemblyBuildError("invalid Unreal part asset path")
    if Path(asset_contract["full_skeletal_mesh"]).name != public_names[
        "full_asset_stem"
    ]:
        raise ClusterAssemblyBuildError(
            "Full Skeletal Mesh path violates the Assembly public-name contract"
        )
    expected_base = str((manifest.get("base") or {}).get("asset_name") or "")
    if Path(asset_contract["base_skeletal_mesh"]).name != expected_base:
        raise ClusterAssemblyBuildError(
            "base Skeletal Mesh path violates the Assembly public-name contract"
        )
    expected_parts = {
        str(part.get("prototype_id") or ""): str(part.get("asset_name") or "")
        for part in manifest.get("parts") or []
    }
    for prototype_id, path in part_paths.items():
        if Path(path).name != expected_parts.get(prototype_id):
            raise ClusterAssemblyBuildError(
                "part Skeletal Mesh path violates the Assembly public-name contract: "
                + str(prototype_id)
            )
    expected_assembly = public_names["full_asset_stem"] + "_NaniteAssembly"
    if Path(asset_contract["assembly"]).name != expected_assembly:
        raise ClusterAssemblyBuildError(
            "Nanite Assembly path violates the public-name contract"
        )
    return {
        "full_skeletal_mesh": asset_contract["full_skeletal_mesh"],
        "base_skeletal_mesh": asset_contract["base_skeletal_mesh"],
        "parts": dict(part_paths),
        "assembly": asset_contract["assembly"],
    }


def _build_unreal_assembly_provenance_payload(manifest, paths):
    """Describe production context without duplicating native Assembly data."""
    manifest_fingerprint = (manifest or {}).get("manifest") or {}
    native_groups = {}
    for part in (manifest or {}).get("parts") or []:
        prototype_id = str(part.get("prototype_id") or "")
        native_path = str((paths.get("parts") or {}).get(prototype_id) or "")
        if not native_path:
            raise ClusterAssemblyBuildError(
                "provenance part has no resolved native asset path: " + prototype_id
            )
        group = native_groups.setdefault(native_path, [])
        group.append(part)
    native_parts = []
    for part_index, (_native_path, grouped_parts) in enumerate(native_groups.items()):
        roles = {str(part.get("role") or "") for part in grouped_parts}
        asset_names = {str(part.get("asset_name") or "") for part in grouped_parts}
        pivot_contracts = {
            str((part.get("external_source") or {}).get("pivot_contract") or "")
            for part in grouped_parts
        }
        if len(roles) != 1 or len(asset_names) != 1 or len(pivot_contracts) != 1:
            raise ClusterAssemblyBuildError(
                "one native Assembly part has inconsistent production provenance"
            )
        prototype_ids = [str(part.get("prototype_id") or "") for part in grouped_parts]
        card_names = sorted({
            str((part.get("external_source") or {}).get("plan_name") or "")
            for part in grouped_parts
            if (part.get("external_source") or {}).get("plan_name")
        })
        ordinals = {
            int((part.get("external_source") or {}).get("ordinal") or 0)
            for part in grouped_parts
        }
        logical_group_indices = {
            int(part.get("logical_group_index", ROLE_ORDER.index(next(iter(roles)))))
            for part in grouped_parts
        }
        logical_subpart_indices = {
            int(part.get("logical_subpart_index") or 0)
            for part in grouped_parts
        }
        card_ordinals = sorted({
            int(value)
            for part in grouped_parts
            for value in (
                (part.get("external_source") or {}).get("card_ordinals")
                or [int((part.get("external_source") or {}).get("ordinal") or 0)]
            )
            if int(value) > 0
        })
        if len(logical_group_indices) != 1 or len(logical_subpart_indices) != 1:
            raise ClusterAssemblyBuildError(
                "one native Assembly part has inconsistent logical hierarchy"
            )
        logical_subpart_index = next(iter(logical_subpart_indices))
        native_parts.append({
            "part_index": part_index,
            "group_index": next(iter(logical_group_indices)),
            "subpart_ordinal": (
                logical_subpart_index
                if logical_subpart_index > 0
                else (next(iter(ordinals)) if len(ordinals) == 1 else 0)
            ),
            "role": next(iter(roles)),
            "prototype_id": (
                prototype_ids[0]
                if len(prototype_ids) == 1
                else "+".join(prototype_ids)
            ),
            "asset_name": next(iter(asset_names)),
            "card_name": ", ".join(card_names),
            "ordinal": next(iter(ordinals)) if len(ordinals) == 1 else 0,
            "card_ordinals": card_ordinals,
            "expected_instance_count": sum(
                len(part.get("bindings") or []) for part in grouped_parts
            ),
            "pivot_contract": next(iter(pivot_contracts)),
            "external_normalized_part": all(
                bool(part.get("external_source")) for part in grouped_parts
            ),
        })
    return {
        "schema_version": SCHEMA_VERSION,
        "builder_version": f"{MANIFEST_KIND}:{SCHEMA_VERSION}",
        "full_source": paths["full_skeletal_mesh"],
        "base_source": paths["base_skeletal_mesh"],
        "manifest_path": str(
            manifest_fingerprint.get("path")
            or (manifest or {}).get("manifest_path")
            or ""
        ),
        "manifest_sha256": str(manifest_fingerprint.get("sha256") or ""),
        "build_status": "Valid",
        "registered_variants": deepcopy(
            (manifest or {}).get("registered_variants") or []
        ),
        "parts": native_parts,
    }


def _rewrite_command_asset_path(command_groups, source_path, target_path):
    rewritten = []
    for commands in command_groups or []:
        if not isinstance(commands, list):
            raise ClusterAssemblyBuildError(
                "generated Assembly import commands are not grouped lists"
            )
        rewritten.append([
            str(command).replace(str(source_path), str(target_path))
            for command in commands
        ])
    return rewritten


def _generated_material_sidecar(template_data, generated_mesh_name, output_dir):
    source_value = str(template_data.get("_material_pipeline_json_path") or "")
    source_path = Path(source_value)
    if not source_value or not source_path.is_file():
        raise ClusterAssemblyBuildError(
            "Assembly generated imports require the existing Full SK material sidecar"
        )
    try:
        payload = json.loads(source_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ClusterAssemblyBuildError(
            f"Full SK material sidecar is unreadable: {source_path}"
        ) from exc
    descriptor = payload.get("speedtree_handoff_contract")
    if not isinstance(descriptor, dict):
        raise ClusterAssemblyBuildError(
            "Full SK material sidecar has no SpeedTree handoff descriptor"
        )
    source_mesh_name = str(payload.get("mesh_name") or "").strip()
    descriptor_mesh_name = str(descriptor.get("mesh_name") or "").strip()
    generated_mesh_name = str(generated_mesh_name or "").strip()
    if not source_mesh_name or descriptor_mesh_name != source_mesh_name:
        raise ClusterAssemblyBuildError(
            "Full SK material sidecar has contradictory mesh identity"
        )
    if not generated_mesh_name:
        raise ClusterAssemblyBuildError(
            "generated Assembly material sidecar requires a mesh identity"
        )
    payload["mesh_name"] = generated_mesh_name
    descriptor["mesh_name"] = generated_mesh_name
    validation = payload.get("validation_children")
    if isinstance(validation, dict):
        validation["asset_unit"] = generated_mesh_name
        validation["json_name"] = generated_mesh_name
    target_dir = Path(output_dir) / "material_sidecars"
    target_dir.mkdir(parents=True, exist_ok=True)
    target_path = target_dir / f"{generated_mesh_name}.json"
    encoded = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    temporary = target_path.with_name(
        f".{target_path.name}.{os.getpid()}.tmp"
    )
    temporary.write_text(encoded, encoding="utf-8")
    os.replace(temporary, target_path)
    return {
        "source": file_fingerprint(source_path),
        "generated": file_fingerprint(target_path),
        "source_mesh_name": source_mesh_name,
        "generated_mesh_name": generated_mesh_name,
    }


def _rewrite_generated_material_sidecar_commands(
    command_groups,
    source_sidecar,
    generated_sidecar,
    source_mesh_name,
    generated_mesh_name,
):
    source_path = str(source_sidecar.get("path") or "")
    generated_path = str(generated_sidecar.get("path") or "")
    source_sha256 = str(source_sidecar.get("sha256") or "").casefold()
    generated_sha256 = str(generated_sidecar.get("sha256") or "").casefold()
    source_mesh_name = str(source_mesh_name or "").strip()
    generated_mesh_name = str(generated_mesh_name or "").strip()
    if (
        not source_path
        or not generated_path
        or not source_sha256
        or not generated_sha256
        or not source_mesh_name
        or not generated_mesh_name
    ):
        raise ClusterAssemblyBuildError(
            "generated Assembly material sidecar binding is incomplete"
        )
    source_variants = {source_path, source_path.replace("\\", "/")}
    generated_text = generated_path.replace("\\", "/")

    def replace_sha(match):
        current = match.group("sha256").casefold()
        if current != source_sha256:
            raise ClusterAssemblyBuildError(
                "Full material sidecar command SHA does not match its current bytes"
            )
        quote = match.group("quote")
        return match.group("prefix") + quote + generated_sha256 + quote

    def replace_expected_mesh(match):
        current = match.group("mesh_name")
        if current != source_mesh_name:
            raise ClusterAssemblyBuildError(
                "Full material sidecar command expected mesh name does not match "
                "its sidecar"
            )
        quote = match.group("quote")
        return match.group("prefix") + quote + generated_mesh_name + quote

    rewritten_groups = []
    for commands in command_groups or []:
        rewritten = []
        for command in commands:
            command_text = str(command)
            for source in source_variants:
                command_text = command_text.replace(source, generated_text)
            if "expected_mesh_name" in command_text:
                command_text, replacement_count = (
                    MATERIAL_PIPELINE_EXPECTED_MESH_PATTERN.subn(
                        replace_expected_mesh,
                        command_text,
                    )
                )
                if replacement_count != 1:
                    raise ClusterAssemblyBuildError(
                        "material pipeline command must bind one literal expected "
                        "mesh name"
                    )
            if "sidecar_sha256" in command_text:
                command_text, replacement_count = (
                    MATERIAL_PIPELINE_SIDECAR_SHA_PATTERN.subn(
                        replace_sha,
                        command_text,
                    )
                )
                if replacement_count != 1:
                    raise ClusterAssemblyBuildError(
                        "material pipeline command must bind one literal sidecar SHA"
                    )
            rewritten.append(command_text)
        rewritten_groups.append(rewritten)
    return rewritten_groups


def scope_material_pipeline_to_codex_tests(
    manifest_assets,
    unreal_folder,
    output_dir,
):
    """Keep test material, layer-instance, and texture writes under Codex/Tests.

    The scope is inferred from the existing Unreal destination path.  It is not
    a feature flag and production destinations are rejected rather than
    silently redirected.
    """
    folder = str(unreal_folder or "").rstrip("/")
    if not folder.startswith("/Game/Codex/Tests/"):
        raise ClusterAssemblyBuildError(
            "isolated material scoping requires a /Game/Codex/Tests destination"
        )
    material_root = folder + "/_MaterialPipeline"
    target_dir = Path(output_dir) / "material_sidecars"
    target_dir.mkdir(parents=True, exist_ok=True)
    scoped = []
    for manifest_asset in manifest_assets or []:
        data = manifest_asset.get("asset_data") or {}
        asset_path = str(data.get("asset_path") or "").split(".", 1)[0].rstrip("/")
        if not asset_path or not asset_path.startswith(folder + "/"):
            raise ClusterAssemblyBuildError(
                "Codex test material scope requires every manifest asset under "
                f"{folder}: {asset_path or '<missing asset_path>'}"
            )
        source_value = str(data.get("_material_pipeline_json_path") or "")
        source_path = Path(source_value)
        if not source_value or not source_path.is_file():
            raise ClusterAssemblyBuildError(
                "Codex test import requires an existing material sidecar: "
                + source_value
            )
        payload = json.loads(source_path.read_text(encoding="utf-8"))
        materials = list(payload.get("materials") or [])
        if not materials:
            raise ClusterAssemblyBuildError(
                "Codex test material sidecar contains no materials"
            )
        presets = set()
        for entry in materials:
            if str(entry.get("instance_profile") or "").strip():
                raise ClusterAssemblyBuildError(
                    "Codex test material scope cannot mutate a user-managed instance profile"
                )
            preset = str(
                entry.get("master_preset")
                or payload.get("material_master")
                or ""
            )
            if not preset:
                raise ClusterAssemblyBuildError(
                    "Codex test material has no master preset"
                )
            presets.add(preset)
            old_target = str(
                entry.get("target_material_path")
                or entry.get("material_instance_path")
                or entry.get("unreal_material_path")
                or ""
            ).split(".", 1)[0]
            target_name = old_target.rsplit("/", 1)[-1]
            if not target_name:
                base = str(
                    (entry.get("speedtree_intent") or {}).get(
                        "material_instance_base"
                    )
                    or entry.get("name")
                    or "Material"
                )
                target_name = "MI_" + base.removeprefix("M_").removeprefix("MI_")
            if not target_name.startswith("MI_"):
                target_name = "MI_" + target_name
            entry["target_material_path"] = (
                material_root + "/MI/" + target_name
            )
            for alias in ("material_instance_path", "unreal_material_path"):
                if alias in entry:
                    entry[alias] = entry["target_material_path"]
            layer = entry.get("material_layer")
            if not isinstance(layer, dict):
                layer = {}
                entry["material_layer"] = layer
            old_path = str(layer.get("instance_path") or "")
            instance_name = old_path.rsplit("/", 1)[-1]
            if not instance_name:
                base = str(
                    (entry.get("speedtree_intent") or {}).get(
                        "material_instance_base"
                    )
                    or entry.get("name")
                    or "Material"
                )
                instance_name = "MYI_" + base.removeprefix("M_")
            if not instance_name.startswith("MYI_"):
                instance_name = "MYI_" + instance_name
            layer["instance_path"] = (
                material_root + "/MYI/" + instance_name
            )
            for alias in (
                "material_layer_instance_path",
                "layer_instance_path",
                "target_layer_instance_path",
            ):
                if alias in entry:
                    entry[alias] = layer["instance_path"]
        asset_name = str(data.get("asset_path") or "Asset").rsplit("/", 1)[-1]
        payload["codex_test_asset_scope"] = {
            "root": material_root,
            "mesh": str(data.get("asset_path") or ""),
            "production_materials_preserved": True,
        }
        target_path = target_dir / f"{asset_name}.codex_test.json"
        encoded = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        temporary = target_path.with_name(
            f".{target_path.name}.{os.getpid()}.tmp"
        )
        temporary.write_text(encoded, encoding="utf-8")
        os.replace(temporary, target_path)
        target_text = str(target_path.resolve()).replace("\\", "/")
        source_variants = {
            str(source_path),
            str(source_path).replace("\\", "/"),
        }
        config_lines = [
            f"_p.TEXTURES_FOLDER = r'{material_root}/Textures'",
            "_p.TEXTURE_IMPORT_CACHE = r'"
            + str((Path(output_dir) / "_texture_import_cache.json").resolve()).replace("\\", "/")
            + "'",
        ]
        for preset in sorted(presets):
            config_lines.extend(
                [
                    f"_p.MASTER_PRESETS[{preset!r}]['mi_folder'] = r'{material_root}/MI'",
                    f"_p.MASTER_PRESETS[{preset!r}]['layer_instance_folder'] = r'{material_root}/MYI'",
                ]
            )
        for key in ("pre_import_commands", "post_import_commands"):
            groups = manifest_asset.get(key) or []
            for commands in groups:
                for index, command in enumerate(commands):
                    rewritten = str(command)
                    for source in source_variants:
                        rewritten = rewritten.replace(source, target_text)
                    commands[index] = rewritten
                marker = next(
                    (
                        index
                        for index, command in enumerate(commands)
                        if "_spec.loader.exec_module(_p)" in str(command)
                    ),
                    None,
                )
                if marker is None:
                    raise ClusterAssemblyBuildError(
                        "material pipeline command has no module-load marker"
                    )
                commands[marker + 1:marker + 1] = config_lines
        data["_material_pipeline_json_path"] = target_text
        data["_material_pipeline_json_fingerprint"] = file_fingerprint(
            target_path
        )
        scoped.append(
            {
                "asset_path": data.get("asset_path"),
                "material_root": material_root,
                "sidecar": data["_material_pipeline_json_fingerprint"],
            }
        )
    return {
        "status": "scoped_to_codex_tests",
        "material_root": material_root,
        "assets": scoped,
    }


def scope_material_pipeline_for_destination(
    manifest_assets,
    unreal_folder,
    output_dir,
):
    """Isolate Codex tests while leaving production material intent untouched."""
    folder = str(unreal_folder or "").rstrip("/")
    if folder.casefold().startswith("/game/codex/tests/"):
        return scope_material_pipeline_to_codex_tests(
            manifest_assets,
            folder,
            output_dir,
        )
    return {
        "status": "production_preserved",
        "unreal_folder": folder,
        "production_materials_preserved": True,
        "assets": [
            {
                "asset_path": (asset.get("asset_data") or {}).get("asset_path"),
                "material_sidecar": (
                    asset.get("asset_data") or {}
                ).get("_material_pipeline_json_path"),
            }
            for asset in manifest_assets or []
        ],
    }


def _apply_generated_fbx_import_contract(manifest_asset):
    """Override only settings that differ for generated Blender FBXs.

    The Full SK template may deliberately disable scene conversion for its own
    export.  BASE/part files are emitted directly by Blender and retain Blender
    FBX axis metadata, so Unreal must consume that metadata.  Applying a mesh
    rotation would also rotate the Skeleton/bind space and is forbidden here.
    """
    property_data = (manifest_asset or {}).get("property_data")
    if not isinstance(property_data, dict):
        raise ClusterAssemblyBuildError(
            "Assembly generated import requires Full SK property_data"
        )
    try:
        settings = property_data["unreal"]["import_method"]["fbx"][
            "skeletal_mesh_import_data"
        ]
    except (KeyError, TypeError) as exc:
        raise ClusterAssemblyBuildError(
            "Assembly generated import requires skeletal FBX import settings"
        ) from exc
    if not isinstance(settings, dict):
        raise ClusterAssemblyBuildError(
            "Assembly skeletal FBX import settings are invalid"
        )

    overrides = {
        # Generated FBX geometry stays in the Full SK local coordinate space,
        # while its object transforms already carry the same Unreal Z-up axis
        # conversion as Send2UE. Consume it without a second conversion.
        "convert_scene": False,
        "convert_scene_unit": False,
        "force_front_x_axis": False,
        "import_rotation": [0.0, 0.0, 0.0],
    }
    for name, value in overrides.items():
        setting = settings.setdefault(name, {})
        if not isinstance(setting, dict):
            raise ClusterAssemblyBuildError(
                f"Assembly skeletal FBX setting is invalid: {name}"
            )
        setting["value"] = value
    return {
        "source": "generated Blender FBX axis metadata",
        "mesh_rotation_applied": False,
        **overrides,
    }


def build_unreal_ingest_plan(manifest, full_manifest_asset, full_asset_path, unreal_folder):
    """Derive generated imports from the existing Send2UE Full-mesh contract."""
    if (manifest or {}).get("status") == "pass_through":
        return {"status": "pass_through", "assets": [], "asset_contract": None}
    if (manifest or {}).get("status") != "ready":
        raise ClusterAssemblyBuildError("Assembly input manifest is not ready")
    validate_manifest_artifacts(manifest)
    public_names = _validate_public_export_names(manifest)
    if not isinstance(full_manifest_asset, dict):
        raise ClusterAssemblyBuildError("Send2UE Full-mesh manifest asset is missing")
    template_data = full_manifest_asset.get("asset_data") or {}
    if template_data.get("_asset_type") != "SkeletalMesh":
        raise ClusterAssemblyBuildError("Assembly imports require a SkeletalMesh template")
    source_asset_path = str(full_asset_path or template_data.get("asset_path") or "")
    if not source_asset_path.startswith("/Game/"):
        raise ClusterAssemblyBuildError("invalid Full Skeletal Mesh asset path")
    if Path(source_asset_path).name != public_names["full_asset_stem"]:
        raise ClusterAssemblyBuildError(
            "Assembly Full asset path does not match the Blender public-name contract"
        )
    tree_folder = str(unreal_folder or "").rstrip("/")
    assembly_folder = tree_folder + "/Assembly"
    generated_output_dir = Path(
        ((manifest.get("base") or {}).get("fbx") or {}).get("path")
    ).parent

    def generated_asset(
        file_path,
        asset_path,
        asset_id,
        asset_name,
        export_stem,
        use_full_skeleton,
        prototype_id,
    ):
        source = Path(file_path)
        if not source.is_file():
            raise ClusterAssemblyBuildError(f"generated Assembly FBX is missing: {source}")
        if source.stem != export_stem:
            raise ClusterAssemblyBuildError(
                "generated Assembly FBX filename does not match export_stem"
            )
        if Path(asset_path).name != asset_name:
            raise ClusterAssemblyBuildError(
                "generated Assembly Unreal path does not match asset_name"
            )
        if asset_name != export_stem:
            raise ClusterAssemblyBuildError(
                "generated Assembly asset_name does not match export_stem"
            )
        row = deepcopy(full_manifest_asset)
        data = row.setdefault("asset_data", {})
        data["file_path"] = str(source.resolve())
        data["asset_folder"] = asset_path.rsplit("/", 1)[0] + "/"
        data["asset_path"] = asset_path
        data["_mesh_object_name"] = export_stem
        data["empty_object_name"] = export_stem
        data["skeleton_asset_path"] = "__FULL_FINAL_SKELETON__" if use_full_skeleton else ""
        data["_cluster_assembly_asset_name"] = asset_name
        data["_cluster_assembly_export_stem"] = export_stem
        data["_cluster_assembly_prototype_id"] = prototype_id
        data["_generated_fbx_axis_contract"] = _apply_generated_fbx_import_contract(
            row
        )
        sidecar = _generated_material_sidecar(
            template_data,
            asset_name,
            generated_output_dir,
        )
        source_sidecar = sidecar["source"]
        generated_sidecar = sidecar["generated"]
        source_mesh_name = sidecar["source_mesh_name"]
        generated_mesh_name = sidecar["generated_mesh_name"]
        source_sha256 = str(source_sidecar.get("sha256") or "").casefold()
        template_sha256 = str(
            template_data.get("_material_pipeline_json_sha256") or ""
        ).casefold()
        if template_sha256 and template_sha256 != source_sha256:
            raise ClusterAssemblyBuildError(
                "Full material sidecar SHA changed before Assembly ingest-plan build"
            )
        template_expected_mesh_name = str(
            template_data.get("_material_pipeline_expected_mesh_name") or ""
        ).strip()
        if (
            template_expected_mesh_name
            and template_expected_mesh_name != source_mesh_name
        ):
            raise ClusterAssemblyBuildError(
                "Full material sidecar expected mesh name does not match its sidecar"
            )
        data["_material_pipeline_json_path"] = sidecar["generated"]["path"]
        data["_material_pipeline_json_sha256"] = generated_sidecar["sha256"]
        data["_material_pipeline_json_fingerprint"] = generated_sidecar
        data["_material_pipeline_expected_mesh_name"] = generated_mesh_name
        row["asset_id"] = asset_id
        for key in ("pre_import_commands", "post_import_commands"):
            row[key] = _rewrite_command_asset_path(
                row.get(key), source_asset_path, asset_path
            )
            row[key] = _rewrite_generated_material_sidecar_commands(
                row[key],
                source_sidecar,
                generated_sidecar,
                source_mesh_name,
                generated_mesh_name,
            )
        return row

    base_contract = manifest.get("base") or {}
    base_fbx = (base_contract.get("fbx") or {}).get("path")
    base_name = str(base_contract.get("asset_name") or "")
    base_export_stem = str(base_contract.get("export_stem") or "")
    base_path = assembly_folder + "/" + base_name
    assets = [generated_asset(
        base_fbx,
        base_path,
        "cluster_assembly_base",
        base_name,
        base_export_stem,
        True,
        "base",
    )]
    part_paths = {}
    external_assets = []
    for part in manifest.get("parts") or []:
        prototype_id = str(part.get("prototype_id") or "")
        if not prototype_id:
            raise ClusterAssemblyBuildError("Assembly part manifest is incomplete")
        part_name = str(part.get("asset_name") or "")
        part_export_stem = str(part.get("export_stem") or "")
        if prototype_id in part_paths:
            raise ClusterAssemblyBuildError(
                f"duplicate Assembly prototype id: {prototype_id}"
            )
        external = part.get("external_source") or {}
        if external:
            relative_folder = str(
                external.get("unreal_relative_folder") or ""
            ).strip().replace("\\", "/").strip("/")
            segments = relative_folder.split("/") if relative_folder else []
            if (
                external.get("kind")
                != "send_to_unreal_normalized_skeletal_part"
                or not relative_folder
                or any(segment in {"", ".", ".."} for segment in segments)
            ):
                raise ClusterAssemblyBuildError(
                    "normalized external Assembly part has an invalid Unreal folder"
                )
            part_path = tree_folder + "/" + relative_folder + "/" + part_name
            part_paths[prototype_id] = part_path
            external_asset = {
                "prototype_id": prototype_id,
                "asset_path": part_path,
                "asset_name": part_name,
                "source_kind": external["kind"],
                "plan_name": str(external.get("plan_name") or ""),
                "ordinal": int(external.get("ordinal") or 0),
                "pivot_contract": str(external.get("pivot_contract") or ""),
            }
            if external.get("expected_normalized_bounds") is not None:
                external_asset["expected_normalized_bounds"] = deepcopy(
                    external["expected_normalized_bounds"]
                )
            external_assets.append(external_asset)
            continue

        part_fbx = (part.get("fbx") or {}).get("path")
        if not part_fbx:
            raise ClusterAssemblyBuildError("Assembly generated part FBX is missing")
        part_path = assembly_folder + "/" + part_name
        part_paths[prototype_id] = part_path
        assets.append(
            generated_asset(
                part_fbx,
                part_path,
                "cluster_assembly_part_" + prototype_id,
                part_name,
                part_export_stem,
                False,
                prototype_id,
            )
        )
    asset_contract = {
        "full_skeletal_mesh": source_asset_path,
        "base_skeletal_mesh": base_path,
        "parts": part_paths,
        "assembly": (
            assembly_folder
            + "/"
            + Path(source_asset_path).name
            + "_NaniteAssembly"
        ),
    }
    validate_unreal_asset_contract(manifest, asset_contract)
    return {
        "status": "ready",
        "assets": assets,
        "external_assets": external_assets,
        "asset_contract": asset_contract,
    }


def _unwrap_struct_result(result, expected_class):
    if isinstance(result, tuple):
        success = next((item for item in result if isinstance(item, bool)), False)
        value = next(
            (item for item in result if isinstance(item, expected_class)),
            None,
        )
        return success, value
    if isinstance(result, expected_class):
        return True, result
    return bool(result), None


def _unreal_bone_names(unreal, mesh):
    component = unreal.new_object(unreal.SkeletalMeshComponent)
    if hasattr(component, "set_skinned_asset_and_update"):
        component.set_skinned_asset_and_update(mesh, False)
    else:  # pragma: no cover - compatibility for older Python exposure.
        component.set_editor_property("skeletal_mesh_asset", mesh)
    return [
        str(component.get_bone_name(index))
        for index in range(component.get_num_bones())
    ]


def _current_unreal_skeleton_diagnostic(expected_bones, actual_bones):
    """Describe manifest drift without rejecting a valid reimported Skeleton."""
    expected = [str(value) for value in expected_bones]
    actual = [str(value) for value in actual_bones]
    expected_set = set(expected)
    actual_set = set(actual)
    exact_match = actual == expected
    return {
        "status": "match" if exact_match else "diagnostic_mismatch",
        "manifest_snapshot_is_authoritative": False,
        "current_unreal_skeleton_is_authoritative": True,
        "exact_order_match": exact_match,
        "expected_bone_count": len(expected),
        "actual_bone_count": len(actual),
        "common_bone_count": len(expected_set & actual_set),
        "missing_from_current": [name for name in expected if name not in actual_set],
        "added_in_current": [name for name in actual if name not in expected_set],
    }


def _base_weighted_bone_manifest_diagnostic(base_contract, actual_bones):
    """Describe stale Base weight metadata without overriding the live Base mesh."""
    declared = [
        str(value) for value in (base_contract or {}).get("weighted_bones") or []
    ]
    try:
        declared_count = int((base_contract or {}).get("weighted_bone_count", -1))
    except (TypeError, ValueError):
        declared_count = -1
    actual_set = {str(value) for value in actual_bones}
    missing = [name for name in declared if name not in actual_set]
    count_matches = declared_count == len(declared)
    return {
        "status": (
            "match"
            if declared and count_matches and not missing
            else "diagnostic_mismatch"
        ),
        "manifest_weight_list_is_authoritative": False,
        "current_imported_base_mesh_is_authoritative": True,
        "declared_weighted_bone_count": len(declared),
        "declared_count_field": declared_count,
        "declared_count_matches": count_matches,
        "missing_from_current_skeleton": missing,
    }


def _unreal_transform(unreal, transform, coordinate_contract):
    factor = float(coordinate_contract["centimeters_per_blender_unit"])
    translation = transform["translation"]
    rotation = transform["rotation_xyzw"]
    scale = transform["scale"]
    return unreal.Transform(
        location=unreal.Vector(
            float(translation[0]) * factor,
            -float(translation[1]) * factor,
            float(translation[2]) * factor,
        ),
        rotation=unreal.Quat(
            -float(rotation[0]),
            float(rotation[1]),
            -float(rotation[2]),
            float(rotation[3]),
        ).rotator(),
        scale=unreal.Vector(float(scale[0]), float(scale[1]), float(scale[2])),
    )


def _dynamic_wind_user_data(unreal, mesh):
    del unreal
    return [
        item
        for item in list(mesh.get_editor_property("asset_user_data") or [])
        if item.get_class().get_path_name()
        == "/Script/DynamicWind.DynamicWindSkeletalData"
    ]


def _bounds_size(record, label):
    if not isinstance(record, dict):
        raise ClusterAssemblyBuildError(f"{label} bounds are missing")
    size = record.get("size")
    if size is None and record.get("extent") is not None:
        size = [2.0 * float(value) for value in record["extent"]]
    if not isinstance(size, (list, tuple)) or len(size) != 3:
        raise ClusterAssemblyBuildError(f"{label} bounds size is invalid")
    checked = [float(value) for value in size]
    if any(not math.isfinite(value) or value <= 0.0 for value in checked):
        raise ClusterAssemblyBuildError(f"{label} bounds size is not positive")
    return checked


def validate_unreal_bounds_contract(
    full_bounds,
    base_bounds,
    assembly_bounds=None,
    assembly_relative_tolerance=0.15,
    base_absolute_scale_ratio_limit=10.0,
    allow_normalized_prototype_dominance=False,
):
    """Reject unit/axis errors and incomplete final Assemblies.

    A normalized external prototype can legitimately dominate the Full SK
    bounds while the generator's user-authored Size/Frond dimensions are still
    awaiting art-direction adjustment.  In that production contract the base
    contains only the non-replaced tree geometry, so Full/base size is not a
    unit probe.  The completed native Assembly remains required to reconstruct
    the Full SK within tolerance.  Per-axis ratios remain the strict default;
    an all-normalized external-prototype contract may use the Full mesh's
    largest span as the denominator so a small, centered overhang on a thin
    axis is not mistaken for a unit or attachment error.
    """
    full_size = _bounds_size(full_bounds, "Full SK")
    base_size = _bounds_size(base_bounds, "Assembly base")
    relative_tolerance = _positive_number(
        assembly_relative_tolerance,
        "Nanite Assembly relative tolerance",
    )

    axis_scale_ratios = [
        full_size[index] / base_size[index] for index in range(3)
    ]
    absolute_scale_ratio = math.exp(
        sum(math.log(value) for value in axis_scale_ratios) / 3.0
    )
    ratio_limit = _positive_number(
        base_absolute_scale_ratio_limit,
        "Full/base absolute scale ratio limit",
    )
    base_scale_outside_limit = (
        absolute_scale_ratio > ratio_limit
        or absolute_scale_ratio < 1.0 / ratio_limit
    )
    if base_scale_outside_limit and not allow_normalized_prototype_dominance:
        raise ClusterAssemblyBuildError(
            "Assembly generated import has an absolute unit scale mismatch "
            "relative to the Full SK: "
            f"full_size={full_size} base_size={base_size} "
            f"axis_scale_ratios={axis_scale_ratios} "
            f"geometric_scale_ratio={absolute_scale_ratio}"
        )

    full_normalized = [value / sum(full_size) for value in full_size]
    base_normalized = [value / sum(base_size) for value in base_size]
    direct_error = sum(
        abs(full_normalized[index] - base_normalized[index])
        for index in range(3)
    )
    yz_swapped_error = (
        abs(full_normalized[0] - base_normalized[0])
        + abs(full_normalized[1] - base_normalized[2])
        + abs(full_normalized[2] - base_normalized[1])
    )
    base_axis_shape_validation = (
        "deferred_to_final_assembly"
        if allow_normalized_prototype_dominance
        else "full_base_shape"
    )
    if (
        not allow_normalized_prototype_dominance
        and not base_scale_outside_limit
        and yz_swapped_error + 0.05 < direct_error
    ):
        raise ClusterAssemblyBuildError(
            "Assembly generated import is Y/Z-swapped relative to the Full SK: "
            f"full_size={full_size} base_size={base_size}"
        )

    result = {
        "status": "base_axis_ok",
        "full": full_bounds,
        "base": base_bounds,
        "base_direct_shape_error": direct_error,
        "base_yz_swapped_shape_error": yz_swapped_error,
        "base_axis_shape_validation": base_axis_shape_validation,
        "base_axis_scale_ratios": axis_scale_ratios,
        "base_absolute_scale_ratio": absolute_scale_ratio,
        "base_absolute_scale_ratio_limit": ratio_limit,
        "base_scale_outside_limit": base_scale_outside_limit,
        "normalized_prototype_dominance_allowed": bool(
            allow_normalized_prototype_dominance
        ),
        "assembly_relative_tolerance": relative_tolerance,
    }
    if assembly_bounds is None:
        if base_scale_outside_limit:
            result["status"] = (
                "normalized_prototype_dominance_pending_final_validation"
            )
        return result

    assembly_size = _bounds_size(assembly_bounds, "Nanite Assembly")
    absolute_errors = [
        abs(assembly_size[index] - full_size[index]) for index in range(3)
    ]
    relative_errors = [
        absolute_errors[index] / full_size[index] for index in range(3)
    ]
    full_span_reference = max(full_size)
    full_span_relative_errors = [
        value / full_span_reference for value in absolute_errors
    ]
    axis_relative_size_ok = max(relative_errors) <= relative_tolerance
    normalized_full_span_size_ok = (
        bool(allow_normalized_prototype_dominance)
        and max(full_span_relative_errors) <= relative_tolerance
    )
    size_validation_mode = (
        "axis_relative"
        if axis_relative_size_ok
        else (
            "full_span_relative_normalized_prototype"
            if normalized_full_span_size_ok
            else "diagnostic_mismatch"
        )
    )
    full_origin = full_bounds.get("origin")
    assembly_origin = assembly_bounds.get("origin")
    origin_relative_errors = None
    origin_absolute_errors = None
    origin_full_span_relative_errors = None
    origin_validation_mode = None
    if (
        isinstance(full_origin, (list, tuple))
        and len(full_origin) == 3
        and isinstance(assembly_origin, (list, tuple))
        and len(assembly_origin) == 3
    ):
        origin_absolute_errors = [
            abs(float(assembly_origin[index]) - float(full_origin[index]))
            for index in range(3)
        ]
        origin_relative_errors = [
            origin_absolute_errors[index] / full_size[index]
            for index in range(3)
        ]
        origin_full_span_relative_errors = [
            value / full_span_reference for value in origin_absolute_errors
        ]
        axis_relative_origin_ok = (
            max(origin_relative_errors) <= relative_tolerance
        )
        normalized_full_span_origin_ok = (
            bool(allow_normalized_prototype_dominance)
            and max(origin_full_span_relative_errors) <= relative_tolerance
        )
        origin_validation_mode = (
            "axis_relative"
            if axis_relative_origin_ok
            else (
                "full_span_relative_normalized_prototype"
                if normalized_full_span_origin_ok
                else "diagnostic_mismatch"
            )
        )
    result.update(
        {
            "status": "complete",
            "assembly": assembly_bounds,
            "assembly_full_span_reference": full_span_reference,
            "assembly_size_absolute_errors": absolute_errors,
            "assembly_size_relative_errors": relative_errors,
            "assembly_size_full_span_relative_errors": (
                full_span_relative_errors
            ),
            "assembly_size_validation_mode": size_validation_mode,
            "assembly_size_match_is_diagnostic": True,
            "assembly_origin_absolute_errors": origin_absolute_errors,
            "assembly_origin_relative_errors": origin_relative_errors,
            "assembly_origin_full_span_relative_errors": (
                origin_full_span_relative_errors
            ),
            "assembly_origin_validation_mode": origin_validation_mode,
            "assembly_origin_match_is_diagnostic": True,
        }
    )
    return result


def _unreal_mesh_bounds_record(mesh):
    bounds = mesh.get_bounds()
    origin = [
        float(bounds.origin.x),
        float(bounds.origin.y),
        float(bounds.origin.z),
    ]
    extent = [
        float(bounds.box_extent.x),
        float(bounds.box_extent.y),
        float(bounds.box_extent.z),
    ]
    return {
        "origin": origin,
        "extent": extent,
        "size": [2.0 * value for value in extent],
        "minimum": [origin[index] - extent[index] for index in range(3)],
        "maximum": [origin[index] + extent[index] for index in range(3)],
    }


def _expected_unreal_normalized_bounds_record(
    expected,
    centimeters_per_blender_unit,
    label,
    require_frame=False,
):
    """Convert authored Blender bounds to the imported Unreal mesh frame."""
    expected_size_cm = [
        component * centimeters_per_blender_unit
        for component in expected["size"]
    ]
    frame_keys = ("minimum", "maximum", "center")
    has_frame = all(expected.get(key) is not None for key in frame_keys)
    if not has_frame:
        if require_frame:
            raise ClusterAssemblyBuildError(
                f"{label} normalized bounds must include minimum/maximum/center"
            )
        return {
            "size": expected_size_cm,
            "frame_verified": False,
        }

    checked = {}
    for key in frame_keys:
        value = expected.get(key)
        if not isinstance(value, (list, tuple)) or len(value) != 3:
            raise ClusterAssemblyBuildError(
                f"{label} normalized bounds {key} is invalid"
            )
        try:
            checked[key] = [float(component) for component in value]
        except (TypeError, ValueError) as exc:
            raise ClusterAssemblyBuildError(
                f"{label} normalized bounds {key} is not numeric"
            ) from exc
        if any(not math.isfinite(component) for component in checked[key]):
            raise ClusterAssemblyBuildError(
                f"{label} normalized bounds {key} is not finite"
            )

    minimum = checked["minimum"]
    maximum = checked["maximum"]
    center = checked["center"]
    if any(minimum[index] > maximum[index] for index in range(3)):
        raise ClusterAssemblyBuildError(
            f"{label} normalized bounds minimum/maximum are inverted"
        )

    expected_span = [
        maximum[index] - minimum[index] for index in range(3)
    ]
    expected_midpoint = [
        0.5 * (minimum[index] + maximum[index]) for index in range(3)
    ]
    frame_tolerance = max(max(expected["size"]) * 1.0e-6, 1.0e-9)
    if (
        any(
            abs(expected_span[index] - expected["size"][index])
            > frame_tolerance
            for index in range(3)
        )
        or any(
            abs(expected_midpoint[index] - center[index])
            > frame_tolerance
            for index in range(3)
        )
    ):
        raise ClusterAssemblyBuildError(
            f"{label} normalized bounds frame is internally inconsistent"
        )

    factor = centimeters_per_blender_unit
    return {
        "size": expected_size_cm,
        "minimum": [
            minimum[0] * factor,
            -maximum[1] * factor,
            minimum[2] * factor,
        ],
        "maximum": [
            maximum[0] * factor,
            -minimum[1] * factor,
            maximum[2] * factor,
        ],
        "origin": [
            center[0] * factor,
            -center[1] * factor,
            center[2] * factor,
        ],
        "frame_verified": True,
    }


def validate_unreal_normalized_prototype_bounds(
    unreal,
    manifest,
    part_assets,
    relative_tolerance=0.08,
    absolute_tolerance_cm=0.1,
):
    """Reject stale, shifted, or wrong-skeleton prototypes before build."""
    production_contract = validate_normalized_prototype_unit_contract(manifest)
    receipt_bounds = _receipt_normalized_bounds_by_asset(manifest)
    parts = [
        part
        for part in (manifest or {}).get("parts") or []
        if (part.get("external_source") or {}).get("kind")
        == "send_to_unreal_normalized_skeletal_part"
    ]
    if not parts:
        return {"status": "not_applicable", "parts": []}

    expected_rows = []
    missing = []
    for part in parts:
        external = part.get("external_source") or {}
        asset_name = str(part.get("asset_name") or "")
        explicit = external.get("expected_normalized_bounds")
        receipt = receipt_bounds.get(asset_name.casefold())
        if explicit is not None:
            explicit = _checked_normalized_bounds(
                explicit,
                f"Assembly manifest prototype {asset_name}",
            )
        if (
            explicit is not None
            and receipt is not None
            and _canonical_json(explicit) != _canonical_json(receipt)
        ):
            raise ClusterAssemblyBuildError(
                "Assembly manifest and production receipt disagree on normalized "
                f"prototype bounds: {asset_name}"
            )
        expected = explicit or receipt
        if expected is None:
            missing.append(asset_name)
        else:
            expected_rows.append((part, expected))

    physical_production = production_contract.get("status") == "verified"
    if missing and physical_production:
        raise ClusterAssemblyBuildError(
            "physical normalized Assembly manifest is missing expected prototype "
            "bounds for: "
            + ", ".join(missing)
            + "; regenerate the BWR Assembly manifest from the current Blender "
            "physical-direct-capture build metadata"
        )
    if not expected_rows:
        return {
            "status": "legacy_contract_not_present",
            "parts": [],
        }

    coordinate_contract = (manifest or {}).get("coordinate_contract") or {}
    centimeters_per_blender_unit = _positive_number(
        coordinate_contract.get("centimeters_per_blender_unit"),
        "Assembly prototype centimeters_per_blender_unit",
    )
    relative_tolerance = _positive_number(
        relative_tolerance,
        "normalized prototype relative bounds tolerance",
    )
    absolute_tolerance_cm = _positive_number(
        absolute_tolerance_cm,
        "normalized prototype absolute bounds tolerance",
    )
    checked = []
    for part, expected in expected_rows:
        prototype_id = str(part.get("prototype_id") or "")
        asset_name = str(part.get("asset_name") or "")
        mesh = (part_assets or {}).get(prototype_id)
        if mesh is None:
            raise ClusterAssemblyBuildError(
                "loaded Unreal prototype is missing for bounds preflight: "
                + prototype_id
            )
        bone_names = _unreal_bone_names(unreal, mesh)
        has_direct_part_root = bone_names == ["part_root"]
        has_importer_carrier_root = (
            len(bone_names) == 2
            and bool(bone_names[0])
            and bone_names[0] != "part_root"
            and bone_names[1] == "part_root"
        )
        if not (has_direct_part_root or has_importer_carrier_root):
            raise ClusterAssemblyBuildError(
                "loaded normalized prototype must contain exactly one part_root "
                "deform bone, with at most one importer carrier root before it: "
                f"{asset_name} bones={bone_names}"
            )
        actual_bounds = _unreal_mesh_bounds_record(mesh)
        actual_size_cm = [
            float(value) for value in actual_bounds.get("size") or []
        ]
        actual_frame_vectors = {
            key: [float(value) for value in actual_bounds.get(key) or []]
            for key in ("minimum", "maximum", "origin")
        }
        if (
            len(actual_size_cm) != 3
            or any(
                not math.isfinite(value) or value < 0.0
                for value in actual_size_cm
            )
            or max(actual_size_cm) <= 0.0
            or any(
                len(vector) != 3
                or any(not math.isfinite(value) for value in vector)
                for vector in actual_frame_vectors.values()
            )
            or any(
                actual_frame_vectors["minimum"][index]
                > actual_frame_vectors["maximum"][index]
                for index in range(3)
            )
        ):
            raise ClusterAssemblyBuildError(
                "loaded normalized prototype bounds are invalid: "
                + asset_name
            )
        expected_bounds = _expected_unreal_normalized_bounds_record(
            expected,
            centimeters_per_blender_unit,
            f"Assembly manifest prototype {asset_name}",
            require_frame=physical_production,
        )
        expected_size_cm = expected_bounds["size"]
        absolute_errors_cm = [
            abs(actual_size_cm[index] - expected_size_cm[index])
            for index in range(3)
        ]
        allowed_errors_cm = [
            max(
                absolute_tolerance_cm,
                expected_size_cm[index] * relative_tolerance,
            )
            for index in range(3)
        ]
        if any(
            absolute_errors_cm[index] > allowed_errors_cm[index]
            for index in range(3)
        ):
            raise ClusterAssemblyBuildError(
                "stale or wrong-unit normalized Unreal prototype "
                f"{asset_name}: expected Blender physical-capture bounds "
                f"{[round(value, 6) for value in expected_size_cm]} cm, loaded "
                f"{[round(value, 6) for value in actual_size_cm]} cm. Re-send "
                "the current normalized source blend through the regular Blender "
                "Send to Unreal profile before building the Nanite Assembly; do "
                "not compensate with generator Leaf Size or Frond Width/Height."
            )
        frame_errors_cm = {}
        if expected_bounds["frame_verified"]:
            for report_key, bounds_key in (
                ("minimum", "minimum"),
                ("maximum", "maximum"),
                ("center", "origin"),
            ):
                actual_vector = actual_frame_vectors[bounds_key]
                expected_vector = expected_bounds[bounds_key]
                errors = [
                    abs(actual_vector[index] - expected_vector[index])
                    for index in range(3)
                ]
                frame_errors_cm[report_key] = errors
                if any(
                    errors[index] > allowed_errors_cm[index]
                    for index in range(3)
                ):
                    raise ClusterAssemblyBuildError(
                        "normalized Unreal prototype mesh-local bounds frame "
                        f"drifted for {asset_name}: {report_key} expected "
                        f"{[round(value, 6) for value in expected_vector]} cm, "
                        f"loaded {[round(value, 6) for value in actual_vector]} "
                        "cm. Re-send the current normalized source blend; do "
                        "not compensate in the Assembly node transform."
                    )
        checked.append({
            "prototype_id": prototype_id,
            "asset_name": asset_name,
            "bone_names": bone_names,
            "expected_size_cm": expected_size_cm,
            "actual_size_cm": actual_size_cm,
            "absolute_errors_cm": absolute_errors_cm,
            "allowed_errors_cm": allowed_errors_cm,
            "expected_minimum_cm": expected_bounds.get("minimum"),
            "actual_minimum_cm": actual_bounds["minimum"],
            "expected_maximum_cm": expected_bounds.get("maximum"),
            "actual_maximum_cm": actual_bounds["maximum"],
            "expected_origin_cm": expected_bounds.get("origin"),
            "actual_origin_cm": actual_bounds["origin"],
            "expected_center_cm": expected_bounds.get("origin"),
            "actual_center_cm": actual_bounds["origin"],
            "frame_absolute_errors_cm": frame_errors_cm,
            "frame_verified": expected_bounds["frame_verified"],
        })
    return {
        "status": "verified",
        "physical_production_contract": physical_production,
        "centimeters_per_blender_unit": centimeters_per_blender_unit,
        "prototype_count": len(checked),
        "parts": checked,
    }


def validate_generated_assembly_reference_pose_sync(reference_pose_sync):
    """Accept an intentional generated-Assembly pose synchronization.

    The Assembly is created from the current Full SK skeleton in this build;
    it is not an authored asset whose previous reference pose must be kept.
    Earlier contract checks already validate skeleton identity, bone order,
    bounds, units, and wind hashes.  This boundary therefore requires only
    that synchronization succeeded and removed every mesh/skeleton mismatch.
    """
    if not isinstance(reference_pose_sync, dict):
        raise ClusterAssemblyBuildError(
            "Assembly reference-pose synchronization result is not an object"
        )
    if not reference_pose_sync.get("success"):
        raise ClusterAssemblyBuildError(
            "Assembly reference pose could not be synchronized to the Full SK "
            "final Skeleton: "
            + str(reference_pose_sync.get("error") or reference_pose_sync)
        )
    reference_pose_sync["synchronization_contract"] = (
        "generated_assembly_pose_sync_attempted_v1"
    )
    reference_pose_sync["changed_pose_accepted"] = bool(
        reference_pose_sync.get("changed")
    )
    return reference_pose_sync


def build_unreal_nanite_assembly(unreal, manifest, asset_contract):
    """Build and save the separate UE 5.8 Assembly from imported inputs.

    The existing Full SK must already have its newly generated DynamicWind data.
    The same wind JSON is imported anew for the Assembly after the native build.
    """
    validate_manifest_artifacts(manifest)
    paths = validate_unreal_asset_contract(manifest, asset_contract)

    def load_skeletal(path):
        asset = unreal.EditorAssetLibrary.load_asset(path)
        if not isinstance(asset, unreal.SkeletalMesh):
            raise ClusterAssemblyBuildError(f"not a SkeletalMesh: {path}")
        return asset

    full = load_skeletal(paths["full_skeletal_mesh"])
    base = load_skeletal(paths["base_skeletal_mesh"])
    part_assets = {
        key: load_skeletal(value) for key, value in paths["parts"].items()
    }
    prototype_bounds_preflight = validate_unreal_normalized_prototype_bounds(
        unreal,
        manifest,
        part_assets,
    )
    full_bounds = _unreal_mesh_bounds_record(full)
    base_bounds = _unreal_mesh_bounds_record(base)
    parts = list(manifest.get("parts") or [])
    normalized_external_parts = [
        part
        for part in parts
        if part.get("topology_signature") == "normalized_external_asset"
        and (part.get("external_source") or {}).get("kind")
        == "send_to_unreal_normalized_skeletal_part"
        and (part.get("fit_summary") or {}).get("fit_mode")
        == "uniform_similarity_3d_normalized_asset"
    ]
    allow_normalized_prototype_dominance = bool(parts) and (
        len(normalized_external_parts) == len(parts)
    )
    validate_unreal_bounds_contract(
        full_bounds,
        base_bounds,
        allow_normalized_prototype_dominance=(
            allow_normalized_prototype_dominance
        ),
    )
    expected_bones = [
        row["name"] for row in manifest["final_skeleton"]["bones"]
    ]
    actual_bones = _unreal_bone_names(unreal, full)
    skeleton_snapshot_diagnostic = _current_unreal_skeleton_diagnostic(
        expected_bones,
        actual_bones,
    )
    full_skeleton = full.get_editor_property("skeleton")
    base_skeleton = base.get_editor_property("skeleton")
    if full_skeleton is None or base_skeleton is None:
        raise ClusterAssemblyBuildError("Full/base Skeletal Mesh has no Skeleton")
    if full_skeleton.get_path_name() != base_skeleton.get_path_name():
        raise ClusterAssemblyBuildError(
            "Assembly base does not use the Full SK final Skeleton"
        )
    if not _dynamic_wind_user_data(unreal, full):
        raise ClusterAssemblyBuildError(
            "Full SK has no newly generated DynamicWindSkeletalData"
        )
    wind_path = ((manifest.get("wind_contract") or {}).get("wind_json") or {}).get("path")
    if not wind_path or not Path(wind_path).is_file():
        raise ClusterAssemblyBuildError(
            "Assembly requires an existing DynamicWind JSON for the Unreal importer"
        )
    wind_file_record = file_fingerprint(wind_path)
    checked_skeleton, skeleton_by_name = _skeleton_maps(
        manifest["final_skeleton"]
    )
    del checked_skeleton
    # Binding validity is defined by the final imported Skeleton.  Wind JSON
    # generation/import is verified independently and must not be used as a
    # second, narrower authority for which Assembly bones are allowed.
    wind_bones = set(actual_bones)
    base_contract = manifest.get("base") or {}
    base_weighted_bones = list(base_contract.get("weighted_bones") or [])
    base_weight_manifest_diagnostic = _base_weighted_bone_manifest_diagnostic(
        base_contract,
        actual_bones,
    )

    assembly_path = paths["assembly"]
    directory, name = assembly_path.rsplit("/", 1)
    parameters = unreal.NaniteAssemblyCreateNewParameters()
    parameters.set_editor_property(
        "target_directory",
        unreal.DirectoryPath(path=directory),
    )
    parameters.set_editor_property("asset_name", name)
    parameters.set_editor_property("overwrite_existing", True)
    builder = unreal.NaniteAssemblySkeletalMeshBuilder.begin_new_skeletal_mesh_assembly_build(
        parameters,
        base,
    )
    if builder is None:
        raise ClusterAssemblyBuildError(
            "BeginNewSkeletalMeshAssemblyBuild failed"
        )
    built_parts = []
    for part in manifest.get("parts") or []:
        bindings = []
        for row in part.get("bindings") or []:
            validate_binding_hierarchy(
                row,
                manifest["final_skeleton"],
                wind_bones=wind_bones,
                skeleton_by_name=skeleton_by_name,
            )
            influences = list(row["bone_influences"])
            primary = influences[0]
            result = builder.create_binding_by_bone_name(
                primary["bone"],
                float(primary["weight"]),
                _unreal_transform(
                    unreal,
                    row["transform"],
                    manifest["coordinate_contract"],
                ),
                unreal.NaniteAssemblyNodeTransformSpace.LOCAL,
            )
            success, binding = _unwrap_struct_result(
                result,
                unreal.NaniteAssemblySkeletalMeshPartBinding,
            )
            if not success or binding is None:
                raise ClusterAssemblyBuildError(
                    f"primary Assembly binding failed: {primary['bone']}"
                )
            for influence in influences[1:]:
                add_result = builder.add_bone_influence_by_name(
                    binding,
                    influence["bone"],
                    float(influence["weight"]),
                )
                if isinstance(add_result, tuple):
                    add_success = next(
                        (value for value in add_result if isinstance(value, bool)),
                        False,
                    )
                    updated = next(
                        (
                            value
                            for value in add_result
                            if isinstance(
                                value,
                                unreal.NaniteAssemblySkeletalMeshPartBinding,
                            )
                        ),
                        None,
                    )
                    if updated is not None:
                        binding = updated
                else:
                    add_success = bool(add_result)
                if not add_success:
                    raise ClusterAssemblyBuildError(
                        f"additional Assembly binding failed: {influence['bone']}"
                    )
            bindings.append(binding)
        if not builder.add_assembly_parts(
            part_assets[part["prototype_id"]],
            bindings,
        ):
            raise ClusterAssemblyBuildError(
                f"AddAssemblyParts failed: {part['prototype_id']}"
            )
        built_parts.append(
            {
                "prototype_id": part["prototype_id"],
                "bindings": len(bindings),
            }
        )
    finish = builder.finish_assembly_build()
    success, assembly = _unwrap_struct_result(finish, unreal.SkeletalMesh)
    if not success or assembly is None:
        raise ClusterAssemblyBuildError(f"FinishAssemblyBuild failed: {finish!r}")
    assembly_skeleton = assembly.get_editor_property("skeleton")
    if (
        assembly_skeleton is None
        or assembly_skeleton.get_path_name() != full_skeleton.get_path_name()
    ):
        raise ClusterAssemblyBuildError(
            "finished Assembly does not use the Full SK final Skeleton"
        )
    try:
        material_normalization = normalize_unreal_nanite_assembly_materials(
            unreal,
            assembly,
            apply=True,
            allow_dirty=True,
        )
    except NaniteAssemblyMaterialError as exc:
        raise ClusterAssemblyBuildError(
            "finished Assembly material table/remaps are not canonical: " + str(exc)
        ) from exc
    if not hasattr(unreal, "CodexDynamicWindImportLibrary"):
        raise ClusterAssemblyBuildError("CodexDynamicWindImportLibrary is unavailable")
    pose_sync_result = (
        unreal.CodexDynamicWindImportLibrary
        .synchronize_mesh_reference_pose_to_skeleton(assembly)
    )
    try:
        reference_pose_sync = json.loads(str(pose_sync_result))
    except (TypeError, ValueError) as exc:
        raise ClusterAssemblyBuildError(
            "Assembly reference-pose synchronization returned invalid JSON: "
            f"{pose_sync_result!r}"
        ) from exc
    validate_generated_assembly_reference_pose_sync(reference_pose_sync)
    result = unreal.CodexDynamicWindImportLibrary.import_dynamic_wind_json_to_skeletal_mesh(
        assembly,
        str(wind_path),
    )
    try:
        wind_import = json.loads(str(result))
    except (TypeError, ValueError) as exc:
        raise ClusterAssemblyBuildError(
            f"Assembly DynamicWind import returned invalid JSON: {result!r}"
        ) from exc
    if not wind_import.get("success"):
        raise ClusterAssemblyBuildError(
            "Assembly DynamicWind regeneration failed: "
            + str(wind_import.get("error") or wind_import)
        )
    wind_import["contract_fields_are_diagnostic"] = True
    wind_import["skeleton_bind_pose_match_is_diagnostic"] = True
    if not _dynamic_wind_user_data(unreal, assembly):
        raise ClusterAssemblyBuildError(
            "finished Assembly has no regenerated DynamicWindSkeletalData"
        )
    bounds_completion = validate_unreal_bounds_contract(
        full_bounds,
        base_bounds,
        _unreal_mesh_bounds_record(assembly),
        allow_normalized_prototype_dominance=(
            allow_normalized_prototype_dominance
        ),
    )
    if not hasattr(unreal, "NaniteAssemblyInspectorLibrary"):
        raise ClusterAssemblyBuildError(
            "NaniteAssemblyInspectorLibrary is unavailable"
        )
    provenance_payload = _build_unreal_assembly_provenance_payload(
        manifest,
        paths,
    )
    provenance_result = (
        unreal.NaniteAssemblyInspectorLibrary
        .set_skeletal_mesh_assembly_provenance_from_json(
            assembly,
            json.dumps(provenance_payload, ensure_ascii=False),
        )
    )
    try:
        provenance = json.loads(str(provenance_result))
    except (TypeError, ValueError) as exc:
        raise ClusterAssemblyBuildError(
            "Assembly provenance writer returned invalid JSON: "
            f"{provenance_result!r}"
        ) from exc
    if not provenance.get("success"):
        raise ClusterAssemblyBuildError(
            "Assembly provenance could not be attached: "
            + str(provenance.get("error") or provenance)
        )
    unreal.EditorAssetLibrary.save_loaded_asset(assembly, only_if_is_dirty=False)
    return {
        "status": "ok",
        "full_skeletal_mesh": paths["full_skeletal_mesh"],
        "full_skeletal_mesh_preserved": True,
        "assembly": assembly.get_path_name(),
        "final_skeleton": full_skeleton.get_path_name(),
        "final_skeleton_bones": len(actual_bones),
        "manifest_skeleton_diagnostic": skeleton_snapshot_diagnostic,
        "production_skeleton_required": False,
        "parts": built_parts,
        "binding_count": sum(row["bindings"] for row in built_parts),
        "base_weighted_bone_count": len(base_weighted_bones),
        "base_weight_manifest_diagnostic": base_weight_manifest_diagnostic,
        "base_weights_in_final_wind": True,
        "reference_pose_sync": reference_pose_sync,
        "prototype_bounds_preflight": prototype_bounds_preflight,
        "bounds_completion": bounds_completion,
        "material_normalization": material_normalization,
        "wind_json_sha256": wind_file_record.get("sha256"),
        "dynamic_wind": wind_import,
        "provenance": provenance,
    }


__all__ = [
    "ClusterAssemblyBuildError",
    "MANIFEST_KIND",
    "SCHEMA_VERSION",
    "ancestor_chain",
    "build_blender_assembly_inputs",
    "build_unreal_ingest_plan",
    "build_unreal_nanite_assembly",
    "content_build_decision",
    "file_fingerprint",
    "fit_uniform_similarity_transform",
    "gate_assembly_transform_residuals",
    "lowest_common_ancestor",
    "make_skeleton_snapshot",
    "normalize_role_identity",
    "scope_material_pipeline_for_destination",
    "scope_material_pipeline_to_codex_tests",
    "snapshot_blender_armature",
    "validate_binding_hierarchy",
    "validate_file_fingerprint",
    "validate_manifest_artifacts",
    "validate_normalized_prototype_unit_contract",
    "validate_unreal_asset_contract",
    "validate_unreal_bounds_contract",
    "validate_unreal_normalized_prototype_bounds",
    "validate_wind_json_against_skeleton",
]
