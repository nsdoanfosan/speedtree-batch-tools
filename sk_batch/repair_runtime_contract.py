"""Saved-output contract plus diagnostic producer hashes for Blender Repair."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from pathlib import Path

from cluster_export_handoff_contract import validate_pending_source_contract


REPAIR_RUNTIME_RECEIPT_VERSION = 2
# Increment this only when an existing saved Blender Repair result is no
# longer semantically valid.  Source-file hashes below are diagnostics, not a
# cache-invalidation contract.
REPAIR_OUTPUT_CONTRACT_VERSION = 2
# Runtime receipt schemas that predate ``output_contract_version`` all
# describe this original saved-output contract.  Receipt schema revisions and
# producer hashes are deliberately not cache invalidators.
LEGACY_REPAIR_OUTPUT_CONTRACT_VERSION = 1
UNASSIGNED_GEOMETRY_CLEANUP_POLICY = (
    "discard_unassigned_geometry_before_repair"
)
UNASSIGNED_GEOMETRY_CLEANUP_CONTRACT_VERSION = 2
UNASSIGNED_GEOMETRY_CLEANUP_OUTPUT_CONTRACT_VERSION = 2
TOOL_DIR = Path(__file__).resolve().parent
REPO_DIR = TOOL_DIR.parent


class RepairPipelineEvidenceError(ValueError):
    """A pipeline report cannot prove the current saved-output contract."""


def _normalized_path(value):
    if not value:
        return ""
    return os.path.normcase(
        os.path.abspath(os.path.normpath(str(value)))
    ).casefold()


def _nonnegative_int(value, label):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RepairPipelineEvidenceError(
            f"{label} must be a non-negative integer"
        )
    return value


def _require_same_path(actual, expected, label):
    if not str(actual or "").strip():
        raise RepairPipelineEvidenceError(f"{label} is missing")
    if expected is not None and _normalized_path(actual) != _normalized_path(
        expected
    ):
        raise RepairPipelineEvidenceError(
            f"{label} does not match the expected path"
        )


def _normalized_cleanup_live_identity(value, label):
    if not isinstance(value, dict):
        raise RepairPipelineEvidenceError(f"{label} is missing")

    def normalized_row(row, row_label):
        if not isinstance(row, dict):
            raise RepairPipelineEvidenceError(f"{row_label} is invalid")
        canonical_path = str(row.get("canonical_path") or "").strip()
        digest = str(row.get("sha256") or "").strip().casefold()
        size = _nonnegative_int(row.get("size"), f"{row_label}.size")
        if (
            not canonical_path
            or len(digest) != 64
            or any(
                character not in "0123456789abcdef"
                for character in digest
            )
        ):
            raise RepairPipelineEvidenceError(f"{row_label} is invalid")
        return {
            "canonical_path": canonical_path,
            "sha256": digest,
            "size": size,
        }

    spm = normalized_row(value.get("spm"), f"{label}.spm")
    raw_stmat = value.get("stmat")
    if not isinstance(raw_stmat, list) or not raw_stmat:
        raise RepairPipelineEvidenceError(f"{label}.stmat is missing")
    stmat = [
        normalized_row(row, f"{label}.stmat[{index}]")
        for index, row in enumerate(raw_stmat)
    ]
    stmat.sort(
        key=lambda row: (
            row["canonical_path"].casefold(),
            row["sha256"],
            row["size"],
        )
    )
    return {"spm": spm, "stmat": stmat}


def _validate_cleanup_record(
    cleanup,
    *,
    label,
    expected_spm=None,
    expected_fbx=None,
):
    if not isinstance(cleanup, dict):
        raise RepairPipelineEvidenceError(f"{label} is missing")
    if cleanup.get("policy") != UNASSIGNED_GEOMETRY_CLEANUP_POLICY:
        raise RepairPipelineEvidenceError(f"{label} policy is invalid")
    try:
        cleanup_version = int(cleanup.get("cleanup_contract_version"))
    except (TypeError, ValueError):
        cleanup_version = 0
    if cleanup_version < UNASSIGNED_GEOMETRY_CLEANUP_CONTRACT_VERSION:
        raise RepairPipelineEvidenceError(f"{label} version is obsolete")
    if cleanup.get("status") not in {"applied", "not_applicable"}:
        raise RepairPipelineEvidenceError(f"{label} status is incomplete")
    if cleanup.get("strict_speedtree_pipeline_contract") is not True:
        raise RepairPipelineEvidenceError(
            f"{label} did not run under the strict SpeedTree contract"
        )
    if (
        cleanup.get("cleanup_authorized") is not True
        or cleanup.get("live_source_identity_validated") is not True
    ):
        raise RepairPipelineEvidenceError(
            f"{label} has no validated cleanup authority"
        )
    _require_same_path(
        cleanup.get("source_identity"), expected_spm, f"{label}.source_identity"
    )
    _require_same_path(
        cleanup.get("source_fbx"), expected_fbx, f"{label}.source_fbx"
    )
    live_identity = _normalized_cleanup_live_identity(
        cleanup.get("live_source_identity"),
        f"{label}.live_source_identity",
    )
    expected_stmat = Path(cleanup["source_fbx"]).with_suffix(".stmat")
    if not any(
        _normalized_path(row["canonical_path"])
        == _normalized_path(expected_stmat)
        for row in live_identity["stmat"]
    ):
        raise RepairPipelineEvidenceError(
            f"{label} live STMAT identity does not match its source FBX"
        )
    fingerprint = hashlib.sha256(
        json.dumps(
            live_identity,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
    ).hexdigest()
    if cleanup.get("live_source_identity_fingerprint") != fingerprint:
        raise RepairPipelineEvidenceError(
            f"{label} live source fingerprint is invalid"
        )

    inspected_count = _nonnegative_int(
        cleanup.get("inspected_mesh_object_count"),
        f"{label}.inspected_mesh_object_count",
    )
    aggregate_fields = (
        "changed_object_count",
        "removed_object_count",
        "removed_face_count",
        "removed_edge_count",
        "removed_vertex_count",
        "removed_material_slot_count",
    )
    aggregates = {
        field: _nonnegative_int(cleanup.get(field), f"{label}.{field}")
        for field in aggregate_fields
    }
    rows = cleanup.get("objects")
    removed_objects = cleanup.get("removed_objects")
    if not isinstance(rows, list) or not isinstance(removed_objects, list):
        raise RepairPipelineEvidenceError(
            f"{label} object evidence is incomplete"
        )
    if aggregates["changed_object_count"] != len(rows):
        raise RepairPipelineEvidenceError(
            f"{label} changed-object aggregate is inconsistent"
        )
    if inspected_count < len(rows):
        raise RepairPipelineEvidenceError(
            f"{label} inspected-object aggregate is inconsistent"
        )

    row_totals = {
        "removed_object_count": 0,
        "removed_face_count": 0,
        "removed_edge_count": 0,
        "removed_vertex_count": 0,
        "removed_material_slot_count": 0,
    }
    removed_names = []
    seen_names = set()
    for index, row in enumerate(rows):
        row_label = f"{label}.objects[{index}]"
        if not isinstance(row, dict):
            raise RepairPipelineEvidenceError(f"{row_label} is invalid")
        name = str(row.get("object") or "").strip()
        if not name or name in seen_names:
            raise RepairPipelineEvidenceError(
                f"{row_label} has a missing or duplicate object name"
            )
        seen_names.add(name)
        removed_object = row.get("removed_object")
        if not isinstance(removed_object, bool):
            raise RepairPipelineEvidenceError(
                f"{row_label}.removed_object must be boolean"
            )
        if removed_object:
            row_totals["removed_object_count"] += 1
            removed_names.append(name)

        for noun, plural in (
            ("face", "faces"),
            ("edge", "edges"),
            ("vertex", "vertices"),
        ):
            before = _nonnegative_int(
                row.get(f"{plural}_before"),
                f"{row_label}.{plural}_before",
            )
            after = _nonnegative_int(
                row.get(f"{plural}_after"),
                f"{row_label}.{plural}_after",
            )
            removed = _nonnegative_int(
                row.get(f"removed_{noun}_count"),
                f"{row_label}.removed_{noun}_count",
            )
            if before - after != removed:
                raise RepairPipelineEvidenceError(
                    f"{row_label} {noun} counts are inconsistent"
                )
            row_totals[f"removed_{noun}_count"] += removed

        slots_before = _nonnegative_int(
            row.get("material_slots_before"),
            f"{row_label}.material_slots_before",
        )
        slots_after = _nonnegative_int(
            row.get("material_slots_after"),
            f"{row_label}.material_slots_after",
        )
        removed_slots = row.get("removed_material_slots")
        if not isinstance(removed_slots, list):
            raise RepairPipelineEvidenceError(
                f"{row_label}.removed_material_slots is invalid"
            )
        if slots_before - slots_after != len(removed_slots):
            raise RepairPipelineEvidenceError(
                f"{row_label} material-slot counts are inconsistent"
            )
        row_totals["removed_material_slot_count"] += len(removed_slots)
        reasons = row.get("removed_face_reasons")
        if not isinstance(reasons, dict):
            raise RepairPipelineEvidenceError(
                f"{row_label}.removed_face_reasons is invalid"
            )
        reason_total = sum(
            _nonnegative_int(value, f"{row_label}.removed_face_reasons")
            for value in reasons.values()
        )
        if reason_total != row.get("removed_face_count"):
            raise RepairPipelineEvidenceError(
                f"{row_label} removed-face reasons are inconsistent"
            )

    if removed_names != removed_objects:
        raise RepairPipelineEvidenceError(
            f"{label} removed-object names are inconsistent"
        )
    for field, total in row_totals.items():
        if aggregates[field] != total:
            raise RepairPipelineEvidenceError(
                f"{label}.{field} aggregate is inconsistent"
            )
    changed = aggregates["changed_object_count"] > 0
    if (cleanup.get("status") == "applied") != changed:
        raise RepairPipelineEvidenceError(
            f"{label} status disagrees with its changed-object count"
        )
    return cleanup


def _validate_renderable_geometry(evidence, label):
    if not isinstance(evidence, dict) or evidence.get("status") != "ok":
        raise RepairPipelineEvidenceError(f"{label} is not ready")
    if _nonnegative_int(
        evidence.get("mesh_object_count"),
        f"{label}.mesh_object_count",
    ) < 1:
        raise RepairPipelineEvidenceError(f"{label} has no mesh object")
    if _nonnegative_int(
        evidence.get("face_count"), f"{label}.face_count"
    ) < 1:
        raise RepairPipelineEvidenceError(f"{label} has no renderable face")


def validate_unassigned_geometry_cleanup_evidence(
    payload,
    *,
    expected_spm=None,
    expected_fbx=None,
    require_recheck=True,
    missing_is_diagnostic=False,
):
    """Validate exact pre-repair cleanup and its final material postcondition.

    This function is intentionally pure: callers can use it both inside the
    Blender producer and while inspecting a committed report.  It rejects
    partial telemetry so a diagnostic runtime receipt cannot bless an older
    Blend that never ran the Default/empty-face cleanup.
    """
    if not isinstance(payload, dict) or payload.get("status") != "done":
        raise RepairPipelineEvidenceError(
            "pipeline did not complete the Blender repair"
        )
    if (
        missing_is_diagnostic
        and not isinstance(payload.get("unassigned_geometry_cleanup"), dict)
    ):
        return {
            "status": "diagnostic_only",
            "policy": "optional_addon_cleanup_telemetry_v1",
            "telemetry_present": False,
            "cleanup_applied": None,
            "message": (
                "The active Blender add-on did not emit optional unassigned "
                "geometry cleanup telemetry; completed Repair output remains "
                "authoritative."
            ),
        }
    cleanup = _validate_cleanup_record(
        payload.get("unassigned_geometry_cleanup"),
        label="unassigned_geometry_cleanup",
        expected_spm=expected_spm,
        expected_fbx=expected_fbx,
    )
    cleanup_live_identity = _normalized_cleanup_live_identity(
        cleanup.get("live_source_identity"),
        "unassigned_geometry_cleanup.live_source_identity",
    )
    material_handoff = payload.get("speedtree_material_handoff_contract")
    pipeline_contract = payload.get("speedtree_pipeline_contract")
    identity_candidates = [payload.get("speedtree_live_source_identity")]
    for contract in (material_handoff, pipeline_contract):
        if isinstance(contract, dict):
            identity_candidates.append(contract.get("source"))
    identity_matches = False
    for index, candidate in enumerate(identity_candidates):
        try:
            normalized_candidate = _normalized_cleanup_live_identity(
                candidate,
                f"pipeline live source candidate {index}",
            )
        except RepairPipelineEvidenceError:
            continue
        if normalized_candidate == cleanup_live_identity:
            identity_matches = True
            break
    if not identity_matches:
        raise RepairPipelineEvidenceError(
            "cleanup identity is not bound to the validated material input"
        )
    imported = payload.get("import")
    if not isinstance(imported, dict):
        raise RepairPipelineEvidenceError("import evidence is missing")
    _require_same_path(
        imported.get("source_fbx"),
        cleanup.get("source_fbx"),
        "import.source_fbx",
    )
    _require_same_path(
        imported.get("source_identity"),
        cleanup.get("source_identity"),
        "import.source_identity",
    )
    if imported.get("unassigned_geometry_cleanup") != cleanup:
        raise RepairPipelineEvidenceError(
            "import cleanup evidence disagrees with the pipeline cleanup"
        )
    _validate_renderable_geometry(
        imported.get("renderable_geometry"),
        "import.renderable_geometry",
    )
    _validate_renderable_geometry(
        payload.get("renderable_geometry_after_cleanup"),
        "renderable_geometry_after_cleanup",
    )

    if require_recheck:
        recheck = _validate_cleanup_record(
            payload.get("unassigned_geometry_cleanup_recheck"),
            label="unassigned_geometry_cleanup_recheck",
            expected_spm=expected_spm,
            expected_fbx=expected_fbx,
        )
        if recheck.get("status") != "not_applicable":
            raise RepairPipelineEvidenceError(
                "cleanup recheck found geometry introduced after import"
            )
        if recheck.get("live_source_identity") != cleanup.get(
            "live_source_identity"
        ):
            raise RepairPipelineEvidenceError(
                "cleanup recheck used a different live source identity"
            )

    material_validation = payload.get("material_slot_validation")
    assigned_slots = (
        material_validation.get("assigned_slot_indices")
        if isinstance(material_validation, dict)
        else None
    )
    material_count = (
        material_validation.get("material_count")
        if isinstance(material_validation, dict)
        else None
    )
    if (
        not isinstance(material_validation, dict)
        or material_validation.get("status") != "ok"
        or material_validation.get("placeholder_cleanup_authorized") is not True
        or material_validation.get("canonical_default_slot_indices") != []
        or not isinstance(assigned_slots, list)
        or not assigned_slots
        or isinstance(material_count, bool)
        or not isinstance(material_count, int)
        or material_count < 1
        or any(
            isinstance(slot, bool)
            or not isinstance(slot, int)
            or slot < 0
            or slot >= material_count
            for slot in assigned_slots
        )
        or assigned_slots != sorted(set(assigned_slots))
    ):
        raise RepairPipelineEvidenceError(
            "final material-slot postcondition is incomplete"
        )
    return cleanup


def _stable_sha256(path):
    candidate = Path(path)
    for _attempt in range(2):
        before = candidate.stat()
        digest = hashlib.sha256()
        with candidate.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        after = candidate.stat()
        if (before.st_size, before.st_mtime_ns) == (
            after.st_size,
            after.st_mtime_ns,
        ):
            return digest.hexdigest(), after
    raise OSError(f"file changed while hashing: {candidate}")


def _validate_source_identity(identity, expected_spm=None):
    if not isinstance(identity, dict):
        raise RepairPipelineEvidenceError("live SPM identity is missing")
    path_value = identity.get("canonical_path")
    _require_same_path(path_value, expected_spm, "live SPM identity path")
    candidate = Path(path_value)
    if not candidate.is_file():
        raise RepairPipelineEvidenceError("live SPM identity file is missing")
    try:
        digest, stat = _stable_sha256(candidate)
    except OSError as exc:
        raise RepairPipelineEvidenceError(str(exc)) from exc
    recorded_size = _nonnegative_int(
        identity.get("size"), "live SPM identity size"
    )
    _nonnegative_int(
        identity.get("mtime_ns"), "live SPM identity mtime_ns"
    )
    if (
        recorded_size != stat.st_size
        or str(identity.get("sha256") or "").casefold()
        != digest.casefold()
    ):
        raise RepairPipelineEvidenceError(
            "live SPM identity does not match the current source"
        )


def _validate_blend_identity(identity, expected_blend=None):
    if not isinstance(identity, dict) or identity.get("exists") is not True:
        raise RepairPipelineEvidenceError("committed Blend identity is missing")
    path_value = identity.get("path")
    _require_same_path(path_value, expected_blend, "committed Blend path")
    candidate = Path(path_value)
    try:
        stat = candidate.stat()
    except OSError as exc:
        raise RepairPipelineEvidenceError(
            "committed Blend file is missing"
        ) from exc
    recorded_size = _nonnegative_int(
        identity.get("size"), "committed Blend size"
    )
    recorded_mtime = _nonnegative_int(
        identity.get("mtime_ns"), "committed Blend mtime_ns"
    )
    recorded_digest = str(identity.get("sha256") or "").casefold()
    fingerprint_policy = str(
        identity.get("fingerprint_policy") or "content_sha256_v1"
    )
    digest_invalid = (
        fingerprint_policy != "path_size_mtime_v1"
        and (
            len(recorded_digest) != 64
            or any(
                character not in "0123456789abcdef"
                for character in recorded_digest
            )
        )
    )
    if (
        recorded_size != stat.st_size
        or recorded_mtime != stat.st_mtime_ns
        or fingerprint_policy not in {
            "content_sha256_v1",
            "path_size_mtime_v1",
        }
        or digest_invalid
    ):
        raise RepairPipelineEvidenceError(
            "committed Blend fingerprint does not match the saved file"
        )


def _validate_export_postcondition(payload, handoff_status):
    if handoff_status == "cluster_export_pending":
        try:
            validate_pending_source_contract(payload)
        except (TypeError, ValueError) as exc:
            raise RepairPipelineEvidenceError(
                "Cluster source Blend commit evidence is incomplete"
            ) from exc
        return
    postcondition = payload.get("repair_push_export_postcondition")
    if not isinstance(postcondition, dict):
        raise RepairPipelineEvidenceError(
            "final Export object postcondition is missing"
        )
    recorded = str(postcondition.get("content_sha256") or "").casefold()
    unsigned = dict(postcondition)
    unsigned.pop("content_sha256", None)
    actual = hashlib.sha256(
        json.dumps(
            unsigned,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    objects = postcondition.get("objects")
    if (
        postcondition.get("kind")
        != "sk_batch_export_object_postcondition"
        or postcondition.get("schema_version") != 2
        or postcondition.get("coverage")
        != "exact_export_collection_all_objects"
        or recorded != actual
        or not isinstance(objects, list)
        or not objects
        or postcondition.get("empty_material_slots") != []
    ):
        raise RepairPipelineEvidenceError(
            "final Export object postcondition is invalid"
        )
    renderable_mesh = False
    for row in objects:
        mesh = row.get("mesh") if isinstance(row, dict) else None
        if not isinstance(mesh, dict):
            continue
        polygon_count = _nonnegative_int(
            mesh.get("polygon_count"),
            "final Export mesh polygon_count",
        )
        materials = mesh.get("materials")
        index_counts = mesh.get("material_index_counts")
        if not isinstance(materials, list) or not isinstance(
            index_counts, list
        ):
            raise RepairPipelineEvidenceError(
                "final Export mesh material evidence is incomplete"
            )
        counted_polygons = 0
        seen_indices = set()
        for index, count_row in enumerate(index_counts):
            if not isinstance(count_row, dict):
                raise RepairPipelineEvidenceError(
                    "final Export mesh material-index evidence is invalid"
                )
            material_index = count_row.get("material_index")
            assigned_count = count_row.get("polygon_count")
            if (
                isinstance(material_index, bool)
                or not isinstance(material_index, int)
                or material_index in seen_indices
                or material_index < 0
                or material_index >= len(materials)
                or isinstance(assigned_count, bool)
                or not isinstance(assigned_count, int)
                or assigned_count < 1
            ):
                raise RepairPipelineEvidenceError(
                    "final Export mesh has an invalid material assignment"
                )
            seen_indices.add(material_index)
            counted_polygons += assigned_count
        if counted_polygons != polygon_count:
            raise RepairPipelineEvidenceError(
                "final Export mesh material assignments do not cover its faces"
            )
        renderable_mesh = renderable_mesh or polygon_count > 0
        for material in materials:
            if not isinstance(material, dict):
                raise RepairPipelineEvidenceError(
                    "final Export mesh contains an empty material slot"
                )
            name = re.sub(
                r"(\.\d{3})$", "", str(material.get("name") or "").strip()
            )
            if name.casefold().endswith("_mat"):
                name = name[:-4]
            key = re.sub(r"[^a-z0-9]+", "", name.casefold())
            if key == "default":
                raise RepairPipelineEvidenceError(
                    "final Export mesh contains canonical Default material"
                )
    if not renderable_mesh:
        raise RepairPipelineEvidenceError(
            "final Export postcondition has no renderable mesh"
        )


def addon_dir_from_config(cfg):
    """Resolve the installed BWR add-on folder exactly as SK Batch does."""
    value = str((cfg or {}).get("fbx_ini") or "")
    if not value:
        return None
    try:
        return Path(value).resolve().parents[2]
    except (IndexError, OSError):
        return None


def repair_runtime_code_paths(addon_dir):
    """Return producer modules recorded for diagnostics after a build."""
    addon_dir = Path(addon_dir)
    paths = list(addon_dir.rglob("*.py"))
    paths.extend([
        REPO_DIR / "speedtree_pipeline_contract.py",
        REPO_DIR / "cluster_spm_pair_contract.py",
        REPO_DIR / "cluster_blend_sync.py",
        REPO_DIR / "cluster_bark_source_resolution.py",
        REPO_DIR / "cluster_normalization_sync.py",
        REPO_DIR / "spm_generator_sync" / "jobs"
        / "cluster_relation_job.py",
        REPO_DIR / "pcg_st9_texture_batch"
        / "pcg_cluster_assembly_contract.py",
        REPO_DIR / "pcg_st9_texture_batch"
        / "pcg_cluster_bark_normalization.py",
        REPO_DIR / "pcg_st9_texture_batch" / "pcg_texture_audit.py",
        TOOL_DIR / "jobs" / "bwr_headless_job.py",
        TOOL_DIR / "jobs" / "speedtree_material_preflight.py",
        TOOL_DIR / "cluster_assembly_builder.py",
        TOOL_DIR / "cluster_assembly_handoff_contract.py",
        TOOL_DIR / "nanite_assembly_materials.py",
        TOOL_DIR / "repair_runtime_contract.py",
    ])
    unique = {}
    for path in paths:
        candidate = Path(path)
        if candidate.is_file():
            unique[os.path.normcase(str(candidate.resolve())).casefold()] = (
                candidate.resolve()
            )
    return [unique[key] for key in sorted(unique)]


def repair_runtime_code_state(addon_dir, modules=None):
    """Return diagnostic content hashes independent of source timestamps."""
    addon_dir = Path(addon_dir).resolve()
    modules = list(
        repair_runtime_code_paths(addon_dir)
        if modules is None
        else modules
    )
    if not modules:
        return None

    def source_snapshot():
        rows = []
        for module in modules:
            candidate = Path(module).resolve()
            stat = candidate.stat()
            rows.append((
                str(candidate),
                stat.st_size,
                stat.st_mtime_ns,
            ))
        return tuple(rows)

    for _attempt in range(2):
        before = source_snapshot()
        state = {}
        for module in modules:
            module = Path(module).resolve()
            try:
                key = "addon/" + module.relative_to(addon_dir).as_posix()
            except ValueError:
                try:
                    key = (
                        "repo/"
                        + module.relative_to(REPO_DIR.resolve()).as_posix()
                    )
                except ValueError:
                    key = "external/" + os.path.normcase(str(module))
            state[key] = hashlib.sha256(module.read_bytes()).hexdigest()
        after = source_snapshot()
        if after == before:
            return state
    raise OSError("Repair runtime source changed while hashing")


def repair_runtime_receipt_path(spm):
    spm = Path(spm)
    return spm.parent / "reports" / f"{spm.stem}_repair_runtime_codex.json"


def repair_runtime_output_contract(payload):
    """Return the semantic saved-output contract recorded by *payload*.

    Old runtime receipts did not have an explicit semantic contract field.
    They all belong to the original contract, regardless of their diagnostic
    receipt schema version or producer-code hash layout.  Unknown/corrupt
    payloads return ``None`` so callers can rely on the real artifact contract
    and replace the diagnostic receipt when that contract is independently
    proven current.
    """
    if not isinstance(payload, dict):
        return None
    if payload.get("kind") != "sk_repair_runtime":
        return None
    value = payload.get("output_contract_version")
    if value is None:
        return LEGACY_REPAIR_OUTPUT_CONTRACT_VERSION
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def repair_pipeline_output_contract(
    payload,
    *,
    spm=None,
    blend=None,
    source_fbx=None,
    source_identity_already_validated=False,
):
    """Infer the saved-output contract from positive pipeline evidence.

    Contract v2 requires raw Default/empty-material geometry cleanup before
    bounds, skinning, weight repair, and merge.  A missing or corrupt runtime
    receipt must never upgrade a legacy blend merely because its older content
    hashes are otherwise current; only the new cleanup report proves v2.
    """
    try:
        validate_unassigned_geometry_cleanup_evidence(
            payload,
            expected_spm=spm,
            expected_fbx=source_fbx,
            require_recheck=True,
        )
        live_identity = (
            (payload.get("speedtree_live_source_identity") or {}).get(
                "spm"
            )
        )
        if not source_identity_already_validated:
            _validate_source_identity(live_identity, expected_spm=spm)
        _validate_blend_identity(
            payload.get("source_blend_identity"),
            expected_blend=blend,
        )
        handoff = payload.get("handoff_preflight")
        if not isinstance(handoff, dict) or handoff.get("status") not in {
            "ok",
            "source_review",
            "cluster_export_pending",
        }:
            raise RepairPipelineEvidenceError(
                "final handoff preflight did not commit a usable Blend"
            )
        _validate_export_postcondition(payload, handoff.get("status"))
    except (OSError, RepairPipelineEvidenceError, TypeError, ValueError):
        return LEGACY_REPAIR_OUTPUT_CONTRACT_VERSION
    return UNASSIGNED_GEOMETRY_CLEANUP_OUTPUT_CONTRACT_VERSION


def repair_runtime_receipt_needs_migration(payload):
    """Whether a compatible/current artifact should rewrite this receipt."""
    if not isinstance(payload, dict):
        return True
    if payload.get("kind") != "sk_repair_runtime":
        return True
    try:
        schema_version = int(payload.get("version"))
    except (TypeError, ValueError):
        return True
    if schema_version != REPAIR_RUNTIME_RECEIPT_VERSION:
        return True
    if repair_runtime_output_contract(payload) != REPAIR_OUTPUT_CONTRACT_VERSION:
        return True
    if "output_contract_version" not in payload:
        return True
    if not isinstance(payload.get("code"), dict):
        return True
    return False


def _atomic_write_receipt(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        delete=False,
        dir=str(path.parent),
        prefix=f".{path.name}.",
        suffix=".tmp",
    ) as handle:
        temporary = Path(handle.name)
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def migrate_repair_runtime_receipt(spm, payload, *, addon_dir=None):
    """Rewrite compatible diagnostic metadata without re-running Repair.

    This is called only after the caller independently validates the live
    content-addressed artifact contract.  Preserve old producer hashes when
    present so scanning many legacy assets does not repeatedly hash the whole
    add-on; a later successful Repair will refresh those diagnostics normally.
    """
    if (
        isinstance(payload, dict)
        and payload.get("kind") == "sk_repair_runtime"
    ):
        migrated = dict(payload)
    else:
        migrated = {}
    migrated.update({
        "kind": "sk_repair_runtime",
        "version": REPAIR_RUNTIME_RECEIPT_VERSION,
        "output_contract_version": REPAIR_OUTPUT_CONTRACT_VERSION,
    })
    if addon_dir is not None:
        migrated["addon_dir"] = str(addon_dir)
    if not isinstance(migrated.get("code"), dict):
        migrated["code"] = {}
    path = repair_runtime_receipt_path(spm)
    _atomic_write_receipt(path, migrated)
    return path


def write_repair_runtime_receipt(
    spm,
    cfg,
    *,
    addon_dir=None,
    code_state=None,
    pipeline=None,
    blend=None,
):
    """Record compatibility only for a positively proven current output.

    Generator/Cluster sync paths also call this helper, so a successful
    no-op must not bless a legacy Blend as the current BWR output contract.
    Contract v2 is recorded only when the committed pipeline report contains
    the pre-repair Default/empty-material cleanup evidence.
    """
    spm = Path(spm)
    if pipeline is None:
        pipeline_path = spm.parent / "reports" / (
            f"{spm.stem}_speedtree_repair_pipeline_report_codex.json"
        )
        try:
            pipeline = json.loads(pipeline_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pipeline = None
    if repair_pipeline_output_contract(
        pipeline,
        spm=spm,
        blend=blend or spm.with_suffix(".blend"),
    ) != REPAIR_OUTPUT_CONTRACT_VERSION:
        return None
    addon_dir = addon_dir or addon_dir_from_config(cfg)
    if addon_dir is None:
        return None
    state = (
        code_state
        if code_state is not None
        else repair_runtime_code_state(addon_dir)
    )
    if not state:
        return None
    path = repair_runtime_receipt_path(spm)
    payload = {
        "kind": "sk_repair_runtime",
        "version": REPAIR_RUNTIME_RECEIPT_VERSION,
        "output_contract_version": REPAIR_OUTPUT_CONTRACT_VERSION,
        "addon_dir": str(addon_dir),
        "code": state,
    }
    _atomic_write_receipt(path, payload)
    return path
