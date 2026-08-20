"""Shared normalized Cluster blend -> owner SK SPM relationship contract.

``Cluster/SK_*.spm`` is the authoritative 3D source.  The matching
``SK_*.blend`` contains the Blender physical capture, plan, and direct UV
delivery; an unprefixed SPM is legacy pair evidence only.  SpeedTree camera UV
data is never a production input here.

A relationship is ON when the owner-folder SK SPM is listed in the blend's
Atlas target sidecar; OFF is the absence of that exact target.  ON targets are
also audited against the canonical Cluster SPM and Blender physical-capture
contract recorded by their most recent Atlas scope manifest.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

from process_lifecycle import owned_run

from atlas_target_registry import (
    TargetRegistryError,
    load_target_registry,
    registry_path_for_blend,
    save_target_registry,
)
from atlas_manifest_resolver import (
    AtlasManifestResolutionError,
    resolution_evidence,
    resolve_atlas_manifests,
)
from cluster_spm_pair_contract import (
    ClusterSpmPairPathError,
    resolve_cluster_spm_pair,
)
from cluster_normalization_sync import (
    ClusterNormalizationSyncError,
    ClusterSourceBuildRequiredError,
    inspect_bwr_material_assignment_freshness,
    inspect_normalization_source_identity,
    resolve_normalization_recipe,
)
from cluster_source_prepare import (
    ClusterSourcePreparationError,
    prepare_cluster_source_if_required,
)
from speedtree_pipeline_contract import (
    SPM_STRUCTURAL_SEMANTIC_PROJECTION_VERSION,
    is_live_spm,
    prove_legacy_texture_normalize_semantic_migration,
    spm_file_structural_semantic_fingerprint,
)


BACKUP_NAME_TOKENS = (
    "backup",
    "codex_backup",
    "pcgtex_backup",
    "skbatch_backup",
    ".pre_",
)

CLUSTER_RELATION_HEARTBEAT_SECONDS = 5.0

CAPTURE_TEXTURE_REFRESH_REASONS = {
    "physical_capture_manifest_missing",
    "physical_capture_changed",
}


class ClusterBlendSyncError(RuntimeError):
    """Actionable Cluster blend relationship or apply failure."""


class RelationValidationCache:
    """One relation generation's thread-safe exact validation memo."""

    def __init__(self):
        self._values = {}
        self._guard = threading.RLock()
        self._key_locks = {}

    def get_or_compute(self, key, factory):
        with self._guard:
            if key in self._values:
                return self._values[key]
            key_lock = self._key_locks.setdefault(key, threading.RLock())
        with key_lock:
            with self._guard:
                if key in self._values:
                    return self._values[key]
            value = factory()
            with self._guard:
                self._values[key] = value
            return value

    def seed(self, key, value):
        """Install exact evidence produced earlier in the same refresh."""
        with self._guard:
            self._values.setdefault(key, value)


def _validation_cache_value(validation_cache, key, factory):
    if validation_cache is None:
        return factory()
    get_or_compute = getattr(validation_cache, "get_or_compute", None)
    if callable(get_or_compute):
        return get_or_compute(key, factory)
    if key not in validation_cache:
        validation_cache[key] = factory()
    return validation_cache[key]


def normalized_path_key(path):
    return os.path.normcase(os.path.abspath(str(Path(path)))).casefold()


def _is_live_file(path, suffix):
    path = Path(path)
    if suffix.casefold() == ".spm":
        return is_live_spm(path)
    name = path.name.casefold()
    return (
        path.is_file()
        and path.suffix.casefold() == suffix.casefold()
        and not name.startswith("~")
        and not name.endswith(".sbk")
        and not any(token in name for token in BACKUP_NAME_TOKENS)
    )


def cluster_folder(owner_folder):
    owner = Path(owner_folder).expanduser().absolute()
    matches = [
        child for child in owner.iterdir()
        if child.is_dir() and child.name.casefold() == "cluster"
    ] if owner.is_dir() else []
    if len(matches) > 1:
        raise ClusterBlendSyncError(
            f"Cluster folder is ambiguous under {owner}: "
            + ", ".join(str(path) for path in matches)
        )
    return matches[0] if matches else None


def owner_sk_spms(owner_folder):
    owner = Path(owner_folder).expanduser().absolute()
    if not owner.is_dir():
        return []
    return sorted(
        (
            path for path in owner.iterdir()
            if _is_live_file(path, ".spm")
            and path.name.casefold().startswith("sk_")
        ),
        key=lambda path: path.name.casefold(),
    )


def cluster_authoring_sources(owner_folder):
    """Return one authoritative member per live Cluster SPM pair.

    The canonical ``SK_`` file wins whenever it exists.  An unprefixed member
    is returned only as a legacy fallback for folders that have not yet been
    normalized.
    """
    directory = cluster_folder(owner_folder)
    if directory is None:
        return []
    pairs = {}
    for path in directory.iterdir():
        if not _is_live_file(path, ".spm"):
            continue
        try:
            pair = resolve_cluster_spm_pair(path)
        except ClusterSpmPairPathError:
            continue
        row = pairs.setdefault(pair["pair_id"], pair)
        if path.name.casefold().startswith("sk_"):
            row["live_canonical"] = path
        else:
            row["live_mirror"] = path
    return sorted(
        (
            row.get("live_canonical") or row.get("live_mirror")
            for row in pairs.values()
            if row.get("live_canonical") or row.get("live_mirror")
        ),
        key=lambda path: path.name.casefold(),
    )


def normalized_blend_for_source(source_spm):
    source = Path(source_spm).expanduser().absolute()
    if source.stem.casefold().startswith("sk_"):
        return source.with_suffix(".blend")
    return source.with_name(f"SK_{source.stem}.blend")


def _read_json(path):
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _registry_target_spms(blend):
    registry = load_target_registry(blend)
    if registry is None:
        return []
    return [
        Path(value).expanduser().absolute()
        for value in registry.get("target_spms") or ()
    ]


def _restore_registry_snapshot(registry_path, original_bytes):
    registry_path = Path(registry_path)
    if original_bytes is None:
        if registry_path.exists():
            registry_path.unlink()
            return True
        return False
    current = (
        registry_path.read_bytes()
        if registry_path.is_file()
        else None
    )
    if current == original_bytes:
        return False
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="wb",
        delete=False,
        dir=str(registry_path.parent),
        prefix=f".{registry_path.name}.",
        suffix=".rollback.tmp",
    ) as handle:
        temporary_registry = Path(handle.name)
        handle.write(original_bytes)
    try:
        os.replace(temporary_registry, registry_path)
    finally:
        temporary_registry.unlink(missing_ok=True)
    return True


def _merge_target_spms(*target_groups):
    """Return one stable, canonical target set without losing registry order."""
    merged = {}
    for group in target_groups:
        for value in group or ():
            target = Path(value).expanduser().absolute()
            merged.setdefault(normalized_path_key(target), target)
    return list(merged.values())


def _snapshot_spm_files(paths, directory):
    """Copy every SPM an apply may rewrite so a failure can be rolled back."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    snapshots = []
    for index, path in enumerate(paths):
        path = Path(path)
        if not path.is_file():
            continue
        copy = directory / f"{index:03d}_{path.name}"
        shutil.copy2(path, copy)
        snapshots.append((path, copy))
    return snapshots


def _restore_spm_files(snapshots, preserve_paths=()):
    """Undo partial SPM writes; keep a rescue copy of anything unrestorable."""
    preserved_keys = {
        normalized_path_key(path) for path in preserve_paths
    }
    restored = []
    failed = []
    for path, copy in snapshots:
        if normalized_path_key(path) in preserved_keys:
            continue
        try:
            if path.is_file() and path.read_bytes() == copy.read_bytes():
                continue
            shutil.copy2(copy, path)
            restored.append(str(path))
        except OSError as exc:
            rescue = path.with_name(
                f"{path.stem}.apply_rollback_backup{path.suffix}"
            )
            try:
                shutil.copy2(copy, rescue)
            except OSError:
                rescue = None
            detail = f"{path}: {exc}"
            if rescue is not None:
                detail += f" (snapshot kept at {rescue})"
            failed.append(detail)
    return restored, failed


def _worker_transaction_conflict_preserve_paths(report, snapshots):
    """Trust only a fully hash-bound, pre-commit Atlas conflict contract."""
    contract = (report or {}).get("failure_contract")
    if not isinstance(contract, dict) or (
        contract.get("kind") != "atlas_speedtree_transaction_failure"
        or contract.get("version") != 1
        or contract.get("reason") != "production_changed_while_staging"
        or contract.get("commit_started") is not False
        or contract.get("preserve_external_changes") is not True
    ):
        return []
    snapshot_by_key = {
        normalized_path_key(path): (Path(path), Path(copy))
        for path, copy in snapshots
    }
    preserve = []
    conflicts = contract.get("conflicts") or []
    if not conflicts:
        return []
    for conflict in conflicts:
        if not isinstance(conflict, dict):
            return []
        key = normalized_path_key(conflict.get("path") or "")
        pair = snapshot_by_key.get(key)
        if pair is None:
            return []
        path, snapshot = pair
        if not path.is_file() or not snapshot.is_file():
            return []
        if (
            _sha256_file(snapshot)
            != str(conflict.get("expected_sha256") or "").casefold()
            or _sha256_file(path)
            != str(conflict.get("actual_sha256") or "").casefold()
        ):
            return []
        preserve.append(path)
    return preserve


def _normalization_artifact_paths(recipe):
    """Files the Blender/Atlas stages may overwrite before Sync commits."""
    if not recipe:
        return []
    paths = []
    if recipe.get("capture_output_dir") and recipe.get("capture_prefix"):
        output_dir = Path(
            recipe["capture_output_dir"]
        ).expanduser().absolute()
        prefix = str(recipe["capture_prefix"])
        suffixes = (
            "",
            "_Opacity",
            "_Normal",
            "_Gloss",
            "_Subsurface",
            "_SubsurfaceAmount",
            "_AO",
            "_Height",
        )
        paths.extend(
            output_dir / f"{prefix}{suffix}.tga"
            for suffix in suffixes
        )
        paths.append(
            output_dir / f"{prefix}_auto_capture_manifest.json"
        )
    if recipe.get("normalization_required"):
        # Normalizer saves both the rebuilt blend and its current-content
        # receipt before Atlas starts writing owner SPMs.
        paths.append(
            Path(recipe["blend"]).expanduser().absolute()
        )
    # A current Normalizer can still advance its persisted Blender source
    # index after Atlas publication.  Snapshot that receipt on every Sync, not
    # only when the capture itself rebuilds.
    if recipe.get("receipt_path"):
        paths.append(
            Path(recipe["receipt_path"]).expanduser().absolute()
        )

    blend = Path(recipe["blend"]).expanduser().absolute()
    report_dir = blend.parent / "reports"
    paths.extend([
        report_dir / (
            blend.stem
            + "_speedtree_repair_pipeline_report_codex.json"
        ),
        report_dir / f"{blend.stem}_repair_runtime_codex.json",
    ])

    first_target = Path(
        recipe["first_target_spm"]
    ).expanduser().absolute()
    owner = first_target.parent
    paths.extend(
        [
            owner / "speedtree_import_manifest.json",
            owner / "README_SPEEDTREE_IMPORT.md",
        ]
    )
    target_receipts = owner / ".atlas_leaf_speedtree_targets"
    paths.extend(
        target_receipts / f"{Path(target).stem}.json"
        for target in recipe.get("target_spms") or []
    )

    # Existing per-scope manifests are content-addressed by a UUID that lives
    # inside the blend. Discover them semantically instead of guessing the ID.
    blend_key = normalized_path_key(recipe["blend"])
    scope_dir = owner / ".atlas_leaf_speedtree_scopes"
    if scope_dir.is_dir():
        for candidate in scope_dir.glob("*.json"):
            payload = _read_json(candidate)
            if (
                isinstance(payload, dict)
                and normalized_path_key(payload.get("blend_file") or "")
                == blend_key
            ):
                paths.append(candidate)

    # Atlas rewrites the external plan FBXs even when normalization itself is
    # current. Existing files must therefore participate in every relationship
    # transaction, not only a capture rebuild.
    mesh_dir = owner / "meshes"
    material_names = list(recipe.get("material_names") or ())
    if recipe.get("material_name"):
        material_names.append(recipe["material_name"])
    mesh_patterns = {
        str(name).casefold() + "__*.fbx"
        for name in material_names
        if str(name or "").strip()
    }
    if recipe.get("snapshot_all_plan_meshes"):
        mesh_patterns.add("*.fbx")
    if mesh_dir.is_dir():
        for mesh_pattern in sorted(mesh_patterns):
            paths.extend(mesh_dir.glob(mesh_pattern))

    result = []
    seen = set()
    for path in paths:
        path = Path(path).expanduser().absolute()
        key = normalized_path_key(path)
        if key in seen:
            continue
        seen.add(key)
        result.append(path)
    return result


def _normalization_artifact_globs(recipe):
    if not recipe:
        return []
    owner = Path(
        recipe["first_target_spm"]
    ).expanduser().absolute().parent
    globs = []
    material_names = list(recipe.get("material_names") or ())
    if recipe.get("material_name"):
        material_names.append(recipe["material_name"])
    mesh_patterns = {
        str(name).casefold() + "__*.fbx"
        for name in material_names
        if str(name or "").strip()
    }
    if recipe.get("snapshot_all_plan_meshes"):
        mesh_patterns.add("*.fbx")
    for pattern in sorted(mesh_patterns):
        globs.append({
            "directory": owner / "meshes",
            "pattern": pattern,
            "kind": "path",
        })
    globs.append({
        "directory": owner / ".atlas_leaf_speedtree_scopes",
        "pattern": "*.json",
        "kind": "scope",
        "blend": str(Path(recipe["blend"]).expanduser().absolute()),
    })
    return globs


def _atlas_transaction_artifact_recipe(
    blend,
    target_spms,
    normalization_recipe,
):
    """Describe every non-SPM Atlas output at risk in this transaction."""

    blend = Path(blend).expanduser().absolute()
    targets = [
        Path(target).expanduser().absolute()
        for target in target_spms
    ]
    recipe = dict(normalization_recipe or {})
    recipe.update({
        "blend": str(blend),
        "normalization_required": bool(
            recipe.get("normalization_required")
        ),
        "first_target_spm": str(targets[0]),
        "target_spms": [str(target) for target in targets],
    })
    material_names = {
        str(recipe.get("material_name") or "").strip()
    }
    for target in targets:
        match = _matching_scope_manifest(blend, target)
        if (
            match is None
            or match.get("mutation_authorized") is False
        ):
            continue
        payload = match["payload"]
        adoption = payload.get("source_material_adoption") or {}
        material_names.add(str(
            adoption.get("material_name")
            or payload.get("material")
            or payload.get("material_name")
            or ""
        ).strip())
    material_names.discard("")
    recipe["material_names"] = sorted(
        material_names,
        key=str.casefold,
    )
    # A first-time/manual Atlas sync has no external receipt from which the
    # configured output material can be discovered without opening Blender.
    # Snapshot the owner plan directory in that exceptional path so a crashed
    # background process still cannot leave or overwrite a partial FBX.
    recipe["snapshot_all_plan_meshes"] = not material_names
    return recipe


def _snapshot_normalization_artifacts(recipe, directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    file_snapshots = []
    for index, path in enumerate(_normalization_artifact_paths(recipe)):
        path = Path(path)
        copy = None
        if path.is_file():
            copy = directory / f"{index:02d}_{path.name}"
            shutil.copy2(path, copy)
        file_snapshots.append((path, copy))
    glob_snapshots = []
    for spec in _normalization_artifact_globs(recipe):
        path = Path(spec["directory"])
        before = {
            normalized_path_key(candidate)
            for candidate in (
                path.glob(spec["pattern"]) if path.is_dir() else ()
            )
        }
        glob_snapshots.append({**spec, "before": before})
    return {
        "files": file_snapshots,
        "globs": glob_snapshots,
    }


def _restore_normalization_artifacts(snapshots):
    restored = []
    failed = []
    for path, copy in (snapshots or {}).get("files") or []:
        try:
            if copy is None:
                if path.exists():
                    path.unlink()
                    restored.append(str(path))
                continue
            if path.is_file() and path.read_bytes() == copy.read_bytes():
                continue
            shutil.copy2(copy, path)
            restored.append(str(path))
        except OSError as exc:
            failed.append(f"{path}: {exc}")
    for spec in (snapshots or {}).get("globs") or []:
        directory = Path(spec["directory"])
        if not directory.is_dir():
            continue
        for path in directory.glob(spec["pattern"]):
            if normalized_path_key(path) in spec["before"]:
                continue
            if spec.get("kind") == "scope":
                payload = _read_json(path)
                if (
                    not isinstance(payload, dict)
                    or normalized_path_key(payload.get("blend_file") or "")
                    != normalized_path_key(spec.get("blend") or "")
                ):
                    continue
            try:
                path.unlink()
                restored.append(str(path))
            except OSError as exc:
                failed.append(f"{path}: {exc}")
    return restored, failed


def _rollback_detail(restored, failed):
    lines = []
    if restored:
        lines.append("Rolled back SPM(s): " + ", ".join(restored))
    if failed:
        lines.append("COULD NOT roll back: " + "; ".join(failed))
    return ("\n" + "\n".join(lines)) if lines else ""


def _atlas_scope_from_user_data(node):
    try:
        payload = json.loads(str(node.findtext("UserData") or ""))
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if payload.get("generator") != "atlas_leaf_mesh_builder":
        return None
    return str(payload.get("scope") or "") or None


def _spm_failure_inventory(path):
    """Read the small ownership/ID subset needed to diagnose a failed apply."""
    path = Path(path)
    try:
        raw = path.read_bytes()
        if raw.startswith(b"\x1f\x8b"):
            raw = gzip.decompress(raw)
        root = ET.fromstring(raw)
    except (OSError, EOFError, gzip.BadGzipFile, ET.ParseError) as exc:
        return {"read_error": f"{type(exc).__name__}: {exc}"}
    assets = root.find("Assets")
    if assets is None:
        return {"read_error": "Assets node is missing"}
    materials = []
    for material in assets.findall("Material_v8"):
        mesh_ids = []
        try:
            cutout = int(str(material.findtext("CutoutMeshID") or ""))
        except ValueError:
            cutout = None
        if cutout is not None:
            mesh_ids.append(cutout)
        supplemental = material.find("SupplementalCutoutMeshIDs")
        if supplemental is not None:
            for child in supplemental.findall("CutoutMesh"):
                try:
                    mesh_id = int(str(child.attrib.get("ID") or ""))
                except ValueError:
                    continue
                if mesh_id not in mesh_ids:
                    mesh_ids.append(mesh_id)
        try:
            material_id = int(str(material.attrib.get("ID") or ""))
        except ValueError:
            material_id = None
        materials.append({
            "id": material_id,
            "name": str(material.attrib.get("Name") or ""),
            "mesh_ids": mesh_ids,
            "atlas_scope": _atlas_scope_from_user_data(material),
        })
    meshes = []
    for mesh in assets.findall("Mesh"):
        try:
            mesh_id = int(str(mesh.attrib.get("ID") or ""))
        except ValueError:
            mesh_id = None
        meshes.append({
            "id": mesh_id,
            "filename": str(
                mesh.findtext("Filename")
                or mesh.findtext("FileName")
                or ""
            ),
            "atlas_scope": _atlas_scope_from_user_data(mesh),
        })
    return {
        "materials": materials,
        "meshes": meshes,
    }


def _scope_failure_diagnostics(blend, target):
    try:
        resolution = resolve_atlas_manifests(
            target,
            expected_blend=blend,
        )
    except AtlasManifestResolutionError as exc:
        return [{
            "error": str(exc),
            "atlas_manifest_resolution": exc.resolution,
        }]
    rows = []
    for selected in resolution["selected"]:
        payload = selected["payload"]
        adoption = payload.get("source_material_adoption") or {}
        connection = payload.get("generator_connection") or {}
        rows.append({
            "path": selected["path"],
            "kind": selected["kind"],
            "export_scope_id": payload.get("export_scope_id"),
            "spm": payload.get("spm"),
            "material_groups": (
                payload.get("speedtree_material_groups")
                or payload.get("material_groups")
                or []
            ),
            "source_material_adoption": {
                "version": adoption.get("version"),
                "scope": adoption.get("scope"),
                "material_name": adoption.get("material_name"),
                "material_id": adoption.get("material_id"),
                "original_mesh_ids": adoption.get("original_mesh_ids"),
                "generated_mesh_ids": adoption.get("generated_mesh_ids"),
                "removed_original_mesh_ids": adoption.get(
                    "removed_original_mesh_ids"
                ),
                "preserved_original_mesh_ids": adoption.get(
                    "preserved_original_mesh_ids"
                ),
                "final_material_mesh_ids": adoption.get(
                    "final_material_mesh_ids"
                ),
                "baseline_kind": adoption.get("baseline_kind"),
                "reused_original_snapshot": adoption.get(
                    "reused_original_snapshot"
                ),
            },
            "generator_connection": {
                "complete": connection.get("complete"),
                "bindings": connection.get("bindings") or [],
            },
            "atlas_manifest_resolution": resolution_evidence(resolution),
        })
    return rows


def _persist_cluster_relation_failure(
    *,
    blend,
    targets,
    enabled,
    phase,
    command,
    snapshots,
    artifact_recipe,
    report=None,
    result=None,
    launch_error=None,
    destination=None,
):
    """Persist exact pre-rollback evidence outside the temporary job folder."""
    blend = Path(blend).expanduser().absolute()
    snapshot_by_target = {
        normalized_path_key(path): copy
        for path, copy in snapshots or []
    }
    target_rows = []
    for target in targets:
        target = Path(target).expanduser().absolute()
        snapshot = snapshot_by_target.get(normalized_path_key(target))
        before_sha256 = (
            _sha256_file(snapshot)
            if snapshot is not None and snapshot.is_file()
            else None
        )
        failed_sha256 = _sha256_file(target) if target.is_file() else None
        target_rows.append({
            "path": str(target),
            "before_sha256": before_sha256,
            "failed_sha256": failed_sha256,
            "changed_before_rollback": (
                before_sha256 is not None
                and failed_sha256 is not None
                and before_sha256 != failed_sha256
            ),
            "before_spm": (
                _spm_failure_inventory(snapshot)
                if snapshot is not None and snapshot.is_file()
                else None
            ),
            "failed_spm": (
                _spm_failure_inventory(target)
                if target.is_file()
                else None
            ),
            "scope_manifests": _scope_failure_diagnostics(blend, target),
        })
    process = None
    if result is not None:
        process = {
            "returncode": getattr(result, "returncode", None),
            "stdout": str(getattr(result, "stdout", "") or ""),
            "stderr": str(getattr(result, "stderr", "") or ""),
        }
    payload = {
        "kind": "cluster_relation_failure_diagnostic",
        "version": 1,
        "recorded_at": datetime.now().astimezone().isoformat(
            timespec="microseconds"
        ),
        "phase": str(phase),
        "mode": "sync" if enabled else "remove",
        "blend": str(blend),
        "targets": target_rows,
        "command": [str(value) for value in command or []],
        "worker_report": report,
        "process": process,
        "launch_error": (
            f"{type(launch_error).__name__}: {launch_error}"
            if launch_error is not None
            else None
        ),
        "failure_contract": (
            _cluster_relation_failure_contract(launch_error)
            or (
                report.get("failure_contract")
                if isinstance(report, dict)
                and isinstance(report.get("failure_contract"), dict)
                else None
            )
        ),
        "artifact_recipe": artifact_recipe,
    }
    report_dir = blend.parent / "reports"
    report_dir.mkdir(parents=True, exist_ok=True)
    if destination is not None:
        destination = Path(destination).expanduser().absolute()
        if destination.parent != report_dir.absolute():
            raise RuntimeError(
                "Worker failure diagnostic is outside the Cluster reports "
                f"directory: {destination}"
            )
        previous = _read_json(destination)
        if previous:
            payload["worker_diagnostic"] = {
                "phase": previous.get("phase"),
                "recorded_at": previous.get("recorded_at"),
            }
    else:
        stamp = datetime.now().strftime("%Y%m%dT%H%M%S_%f")
        destination = (
            report_dir
            / f"{blend.stem}_cluster_relation_failure_{stamp}.json"
        )
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        delete=False,
        dir=str(report_dir),
        prefix=f".{destination.name}.",
        suffix=".tmp",
    ) as handle:
        temporary = Path(handle.name)
        json.dump(payload, handle, ensure_ascii=False, indent=2, default=str)
        handle.write("\n")
    try:
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def _cluster_relation_failure_contract(error):
    """Return structured remediation data without guessing asset culpability."""
    if isinstance(error, ClusterSourceBuildRequiredError):
        return {
            "failure_kind": "process_precondition",
            "reason": error.reason,
            "canonical_spm": str(error.canonical_spm),
            "source_blend": str(error.blend),
            "source_report": str(error.report_path),
            "remediation": "automatic_cluster_source_rebuild",
        }
    if isinstance(error, ClusterSourcePreparationError):
        return {
            "failure_kind": "cluster_source_preparation_failed",
            "stage": error.stage,
            "log_file": (
                str(error.log_file) if error.log_file is not None else None
            ),
            "report_file": (
                str(error.report_file)
                if error.report_file is not None
                else None
            ),
            "stage_report": error.report,
        }
    return None


def _resolve_normalization_recipe_with_source_rebuild(
    blend,
    targets,
    *,
    blender_exe,
    unit_probe_path,
    capture_resolution,
    progress_callback,
):
    """Resolve once, rebuilding a proven-stale source and reusing validation."""
    try:
        recipe = resolve_normalization_recipe(
            blend,
            targets,
            canonical_spm=Path(blend).with_suffix(".spm"),
            unit_probe_path=unit_probe_path,
            capture_resolution=capture_resolution,
        )
        return recipe, None
    except ClusterSourceBuildRequiredError as required:
        _emit_cluster_relation_progress(
            progress_callback,
            "cluster_source_rebuild",
            "Canonical Cluster SPM changed; rebuilding its source blend...",
        )
        preparation = prepare_cluster_source_if_required(
            blend,
            targets,
            blender_exe=blender_exe,
            unit_probe_path=unit_probe_path,
            capture_resolution=capture_resolution,
            progress_callback=progress_callback,
            known_required=required,
        )
        recipe = preparation.pop(
            "validated_normalization_recipe",
            None,
        )
        if not isinstance(recipe, dict):
            raise ClusterSourcePreparationError(
                "source_contract_validation",
                "Cluster source rebuild did not return its validated "
                "normalization recipe.",
                report=preparation,
            )
        return recipe, preparation


def _sha256_file(path):
    path = Path(path).expanduser().absolute()
    for _attempt in range(2):
        before = path.stat()
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        after = path.stat()
        if (
            before.st_size,
            before.st_mtime_ns,
        ) == (
            after.st_size,
            after.st_mtime_ns,
        ):
            return digest.hexdigest()
    raise ClusterBlendSyncError(
        f"File changed while its content fingerprint was calculated: {path}"
    )


def _physical_capture_manifest_for_blend(blend, validation_cache=None):
    blend = Path(blend).expanduser().absolute()
    stem = blend.stem
    if stem.casefold().startswith("sk_"):
        stem = stem[3:]
    path = blend.with_name(f"{stem}_auto_capture_manifest.json")
    cache_key = ("capture_manifest", normalized_path_key(path))
    payload = _validation_cache_value(
        validation_cache,
        cache_key,
        lambda: _read_json(path),
    )
    if not payload:
        return {"path": path, "active": False, "contract_sha256": None}
    active = (
        payload.get("workflow_mode") == "PHYSICAL_DIRECT_CAPTURE"
        and payload.get("direct_uv_source")
        == "same_blender_physical_capture_projection"
    )
    return {
        "path": path,
        "active": active,
        "contract_sha256": (
            str(payload.get("physical_capture_contract_sha256") or "")
            or str(
                (payload.get("physical_capture_contract") or {}).get(
                    "contract_sha256"
                )
                or ""
            )
            or None
        ),
    }


def _walk_dicts(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_dicts(child)


def _recorded_source_spm_rows(receipt, canonical_spm):
    canonical_key = normalized_path_key(canonical_spm)
    rows = {}
    for row in _walk_dicts(receipt):
        source_path = str(row.get("source_spm") or "").strip()
        source_hash = str(row.get("source_spm_sha256") or "").strip().casefold()
        if (
            source_path
            and source_hash
            and normalized_path_key(source_path) == canonical_key
        ):
            recorded = {
                "path": str(Path(source_path).expanduser().absolute()),
                "sha256": source_hash,
                "semantic_projection_version": row.get(
                    "source_spm_semantic_projection_version"
                ),
                "semantic_fingerprint": str(
                    row.get("source_spm_semantic_fingerprint") or ""
                ).strip().casefold() or None,
            }
            identity = json.dumps(
                recorded,
                sort_keys=True,
                separators=(",", ":"),
            )
            rows[identity] = recorded
    return list(rows.values())


def _recorded_source_fbx_rows(receipt):
    rows = {}
    for row in _walk_dicts(receipt):
        source_path = str(row.get("source_fbx") or "").strip()
        source_hash = str(
            row.get("source_fbx_sha256") or ""
        ).strip().casefold()
        if not source_path or not source_hash:
            continue
        rows.setdefault(normalized_path_key(source_path), {
            "path": str(Path(source_path).expanduser().absolute()),
            "sha256": source_hash,
        })
    return list(rows.values())


def _target_scope_refresh_reasons(payload, target_spm):
    """Compare a scope receipt with the material/mesh registry it committed."""
    try:
        from pcg_st9_texture_batch import pcg_texture_audit as audit
    except ImportError:
        import pcg_texture_audit as audit

    adoption = payload.get("source_material_adoption") or {}
    target_rows = audit.extract_material_image_refs(target_spm)
    reasons = []
    for group in payload.get("material_groups") or ():
        material = str(group.get("material") or "")
        material_id = int(group.get("material_id") or 0)
        expected_mesh_ids = [
            int(value) for value in group.get("mesh_ids") or ()
        ]
        if (
            str(adoption.get("material_name") or "") == material
            and int(adoption.get("material_id") or 0) == material_id
            and adoption.get("final_material_mesh_ids") is not None
        ):
            expected_mesh_ids = [
                int(value)
                for value in adoption.get("final_material_mesh_ids") or ()
            ]
        matches = [
            row for row in target_rows
            if str(row.get("material_name") or "") == material
            and int(row.get("material_id") or 0) == material_id
            and [
                int(value) for value in row.get("cutout_mesh_ids") or ()
            ] == expected_mesh_ids
        ]
        if len(matches) != 1:
            reasons.append("target_scope_changed")
            break
    return reasons


def _physical_refresh_state(
    payload, canonical_spm, blend, *, validation_cache=None,
):
    receipt = payload.get("normalized_prototype_receipt")
    if not isinstance(receipt, dict):
        return {
            "physical": False,
            "refresh_required": False,
            "refresh_reasons": [],
        }
    if receipt.get("workflow_mode") != "PHYSICAL_DIRECT_CAPTURE":
        return {
            "physical": False,
            "refresh_required": False,
            "refresh_reasons": [],
        }

    reasons = []
    source_content_identity = inspect_normalization_source_identity(blend)
    reasons.extend(source_content_identity.get("refresh_reasons") or ())
    canonical = Path(canonical_spm).expanduser().absolute()
    recorded_source_rows = _recorded_source_spm_rows(receipt, canonical)
    def current_sha256(path):
        candidate = Path(path).expanduser().absolute()
        if not candidate.is_file():
            return None
        cache_key = ("sha256", normalized_path_key(candidate))
        return _validation_cache_value(
            validation_cache,
            cache_key,
            lambda: _sha256_file(candidate),
        )

    current_source_sha256 = current_sha256(canonical)
    current_source_semantic = None
    legacy_semantic_migration = None
    if not current_source_sha256:
        reasons.append("canonical_source_missing")
    else:
        try:
            semantic_key = (
                "spm_structural_semantic",
                normalized_path_key(canonical),
                current_source_sha256,
            )
            current_source_semantic = _validation_cache_value(
                validation_cache,
                semantic_key,
                lambda: (
                    spm_file_structural_semantic_fingerprint(
                        canonical,
                        raw_sha256=current_source_sha256,
                    )
                ),
            )
        except (OSError, ValueError, ET.ParseError):
            reasons.append("canonical_source_semantic_unavailable")
    if not recorded_source_rows:
        reasons.append("recorded_source_missing")
    elif len(recorded_source_rows) > 1:
        reasons.append("recorded_source_conflict")
    elif len(recorded_source_rows) == 1 and current_source_semantic:
        recorded_source = recorded_source_rows[0]
        recorded_semantic = recorded_source.get("semantic_fingerprint")
        if recorded_semantic:
            if (
                recorded_source.get("semantic_projection_version")
                != SPM_STRUCTURAL_SEMANTIC_PROJECTION_VERSION
                or recorded_semantic != current_source_semantic.casefold()
            ):
                reasons.append("canonical_source_structural_changed")
        elif recorded_source["sha256"] != current_source_sha256.casefold():
            legacy_semantic_migration = (
                prove_legacy_texture_normalize_semantic_migration(
                    canonical,
                    recorded_source["sha256"],
                )
            )
            if legacy_semantic_migration is None:
                reasons.append("canonical_source_changed")

    recorded_fbx_rows = _recorded_source_fbx_rows(receipt)
    for recorded in recorded_fbx_rows:
        source_fbx = Path(recorded["path"])
        current_fbx_sha256 = current_sha256(source_fbx)
        if not current_fbx_sha256:
            reasons.append("source_fbx_missing")
        elif current_fbx_sha256.casefold() != recorded["sha256"]:
            reasons.append("source_fbx_changed")

    capture = _physical_capture_manifest_for_blend(
        blend,
        validation_cache=validation_cache,
    )
    recorded_capture_sha256 = str(
        receipt.get("physical_capture_contract_sha256") or ""
    ) or None
    if not capture["active"] or not capture["contract_sha256"]:
        reasons.append("physical_capture_manifest_missing")
    elif (
        recorded_capture_sha256
        and capture["contract_sha256"] != recorded_capture_sha256
    ):
        reasons.append("physical_capture_changed")

    return {
        "physical": True,
        "refresh_required": bool(reasons),
        "refresh_reasons": reasons,
        "canonical_source_sha256": current_source_sha256,
        "canonical_source_semantic_projection_version":
            SPM_STRUCTURAL_SEMANTIC_PROJECTION_VERSION,
        "canonical_source_semantic_fingerprint": current_source_semantic,
        "recorded_source_sha256": (
            recorded_source_rows[0]["sha256"]
            if len(recorded_source_rows) == 1
            else None
        ),
        "recorded_source_semantic_fingerprint": (
            recorded_source_rows[0]["semantic_fingerprint"]
            if len(recorded_source_rows) == 1
            else None
        ),
        "legacy_semantic_migration": legacy_semantic_migration,
        "capture_manifest": str(capture["path"]),
        "capture_contract_sha256": capture["contract_sha256"],
        "recorded_capture_contract_sha256": recorded_capture_sha256,
        "source_fbx_artifacts": recorded_fbx_rows,
        "source_content_identity": source_content_identity,
    }


def _refresh_reason_summary(reasons):
    unique = sorted(set(reasons or ()))
    capture_texture = [
        reason for reason in unique
        if reason in CAPTURE_TEXTURE_REFRESH_REASONS
    ]
    geometry_ownership = [
        reason for reason in unique
        if reason not in CAPTURE_TEXTURE_REFRESH_REASONS
    ]
    categories = []
    if geometry_ownership:
        categories.append("geometry_ownership")
    if capture_texture:
        categories.append("capture_texture")
    return {
        "refresh_reasons": unique,
        "refresh_reason_categories": categories,
        "geometry_ownership_refresh_reasons": geometry_ownership,
        "capture_texture_refresh_reasons": capture_texture,
    }


def _matching_scope_manifest(blend, target_spm):
    """Return read-only Atlas evidence for this blend/target.

    Provider metadata disagreement can revoke authority to rewrite a manifest,
    but it cannot veto inspection of an already-live Cluster relationship.  A
    projected diagnostic selection also prevents a disagreement in an
    unrelated Provider claim from hiding this exact target's disjoint claims.
    """
    blend = Path(blend).expanduser().absolute()
    target = Path(target_spm).expanduser().absolute()
    try:
        # Resolve ownership before applying the caller's Provider filter.  The
        # filter is useful for selecting the exact live payload to inspect, but
        # it must not hide another claimant and thereby recreate write
        # authority for the preferred Provider.
        ownership_resolution = resolve_atlas_manifests(
            target,
            diagnostic_only=True,
        )
        resolution = resolve_atlas_manifests(
            target,
            expected_blend=blend,
            diagnostic_only=True,
        )
    except AtlasManifestResolutionError as exc:
        raise ClusterBlendSyncError(str(exc)) from exc
    selected = resolution["selected"]
    if not selected:
        return None
    match = selected[0]
    return {
        "path": match["path"],
        "payload": match["payload"],
        "resolution": resolution_evidence(ownership_resolution),
        "selection_resolution": resolution_evidence(resolution),
        "mutation_authorized": bool(
            ownership_resolution.get("mutation_authorized", True)
            and resolution.get("mutation_authorized", True)
        ),
    }


def inspect_cluster_target(
    blend,
    target_spm,
    relation_on,
    *,
    canonical_spm=None,
    verify_physical=True,
    validation_cache=None,
):
    target = Path(target_spm).expanduser().absolute()
    if not relation_on:
        return {
            "status": "off",
            "connected": None,
            "material": None,
            "manifest": None,
        }
    if not target.is_file():
        return {
            "status": "missing",
            "connected": False,
            "material": None,
            "manifest": None,
        }
    match = _matching_scope_manifest(blend, target)
    if match is None:
        return {
            "status": "pending",
            "connected": False,
            "material": None,
            "manifest": None,
        }
    payload = match["payload"]
    connection = payload.get("generator_connection") or {}
    adoption = payload.get("source_material_adoption") or {}
    complete = connection.get("complete") is True
    requested = connection.get("requested")
    satisfied = complete or requested is False
    canonical = (
        Path(canonical_spm).expanduser().absolute()
        if canonical_spm
        else Path(blend).expanduser().absolute().with_suffix(".spm")
    )
    if verify_physical:
        refresh = _physical_refresh_state(
            payload,
            canonical,
            blend,
            validation_cache=validation_cache,
        )
        for reason in _target_scope_refresh_reasons(payload, target):
            if reason not in refresh["refresh_reasons"]:
                refresh["refresh_reasons"].append(reason)
        refresh["refresh_required"] = bool(refresh["refresh_reasons"])
    else:
        normalized_receipt = payload.get("normalized_prototype_receipt")
        refresh = {
            "physical": (
                isinstance(normalized_receipt, dict)
                and normalized_receipt.get("workflow_mode")
                == "PHYSICAL_DIRECT_CAPTURE"
            ),
            "refresh_required": False,
            "refresh_reasons": [],
            "refresh_deferred": True,
        }
    refresh_required = satisfied and refresh["refresh_required"]
    refresh.update(_refresh_reason_summary(refresh["refresh_reasons"]))
    return {
        "status": (
            "refresh_required"
            if refresh_required
            else "registered"
            if not verify_physical and satisfied
            else "synced" if satisfied
            else "attention"
        ),
        "connected": complete,
        "connection_requested": requested,
        "connection_satisfied": satisfied,
        "material": (
            adoption.get("material_name")
            or payload.get("material")
            or payload.get("material_name")
        ),
        "material_id": (
            adoption.get("material_id")
            or payload.get("material_id")
        ),
        "manifest": match["path"],
        "atlas_manifest_resolution": match["resolution"],
        "mesh_ids": list(payload.get("mesh_ids") or ()),
        **refresh,
    }


def discover_cluster_blend_relations(
    owner_folder,
    *,
    verify_physical=True,
    validation_cache=None,
):
    """Discover normalized Cluster blends and their owner-folder ON/OFF state.

    The target rows remain available as audit evidence for Blender/PCG, but the
    user-facing relationship is one state per blend across the complete set of
    owner-folder ``SK_*.spm`` files.
    """
    owner = Path(owner_folder).expanduser().absolute()
    if validation_cache is None:
        validation_cache = RelationValidationCache()
    targets = owner_sk_spms(owner)
    rows = []
    for source in cluster_authoring_sources(owner):
        pair = resolve_cluster_spm_pair(source)
        canonical = Path(pair["canonical_spm"]).expanduser().absolute()
        mirror = Path(pair["mirror_spm"]).expanduser().absolute()
        blend = normalized_blend_for_source(source)
        if not _is_live_file(blend, ".blend"):
            continue
        registry_error = None
        try:
            registry = load_target_registry(blend)
        except TargetRegistryError as exc:
            registry = None
            registry_error = str(exc)
        registered = [] if registry is None else [
            Path(value).expanduser().absolute()
            for value in registry.get("target_spms") or ()
            if is_live_spm(value, require_file=False)
        ]
        registered_by_key = {
            normalized_path_key(path): path for path in registered
        }
        candidate_by_key = {
            normalized_path_key(path): path for path in targets
        }
        # Preserve unexpected/external listed targets as visible evidence. The
        # mutation API below still refuses to create new external relations.
        candidate_by_key.update(registered_by_key)
        relation_rows = []
        for key, target in candidate_by_key.items():
            relation_on = key in registered_by_key
            state = inspect_cluster_target(
                blend,
                target,
                relation_on,
                canonical_spm=canonical,
                verify_physical=verify_physical,
                validation_cache=validation_cache,
            )
            relation_rows.append({
                "target_spm": target,
                "relation_on": relation_on,
                "owner_target": target.parent == owner,
                "exists": target.is_file(),
                **state,
            })
        owner_relations = [
            relation for relation in relation_rows
            if relation.get("owner_target")
        ]
        on_count = sum(
            bool(relation.get("relation_on"))
            for relation in owner_relations
        )
        if not owner_relations:
            folder_relation = "empty"
        elif on_count == 0:
            folder_relation = "off"
        elif on_count == len(owner_relations):
            folder_relation = "on"
        else:
            folder_relation = "partial"
        rows.append({
            "kind": "cluster_normalized_blend",
            "owner_folder": owner,
            "cluster_folder": source.parent,
            "source_spm": canonical if canonical.is_file() else source,
            "canonical_spm": canonical,
            "mirror_spm": mirror,
            "blend": blend,
            "registry_path": registry_path_for_blend(blend),
            "registry_managed": registry is not None,
            "registry_error": registry_error,
            "folder_relation": folder_relation,
            "owner_target_count": len(owner_relations),
            "owner_on_count": on_count,
            "owner_off_count": len(owner_relations) - on_count,
            "refresh_required_count": sum(
                relation.get("status") == "refresh_required"
                for relation in owner_relations
            ),
            "refresh_deferred_count": sum(
                relation.get("refresh_deferred") is True
                for relation in owner_relations
                if relation.get("relation_on")
            ),
            "refresh_reasons": sorted({
                reason
                for relation in owner_relations
                for reason in relation.get("refresh_reasons") or ()
            }),
            "refresh_reason_categories": sorted({
                category
                for relation in owner_relations
                for category in (
                    relation.get("refresh_reason_categories") or ()
                )
            }),
            "geometry_ownership_refresh_reasons": sorted({
                reason
                for relation in owner_relations
                for reason in (
                    relation.get("geometry_ownership_refresh_reasons") or ()
                )
            }),
            "capture_texture_refresh_reasons": sorted({
                reason
                for relation in owner_relations
                for reason in (
                    relation.get("capture_texture_refresh_reasons") or ()
                )
            }),
            "targets": sorted(
                relation_rows,
                key=lambda row: (
                    row["target_spm"].name.casefold(),
                    str(row["target_spm"]).casefold(),
                ),
            ),
        })
    return sorted(rows, key=lambda row: row["blend"].name.casefold())


def _current_on_relation_state(
    blend,
    targets,
    *,
    validation_cache=None,
):
    """Return current physical proof plus fail-closed refresh diagnostics.

    The registry alone is not enough to skip work.  A no-op is safe only when
    every effective target is local, still has an exact synced scope, and that
    scope proves the current canonical source/capture/material state.
    """
    blend = Path(blend).expanduser().absolute()
    owner = blend.parent.parent
    canonical = blend.with_suffix(".spm")
    if validation_cache is None:
        validation_cache = RelationValidationCache()
    reasons = []
    target_states = []
    if not canonical.is_file():
        reasons.append("canonical_source_missing")

    evidence = []
    for target in targets:
        target = Path(target).expanduser().absolute()
        if target.parent != owner:
            reasons.append("target_outside_owner")
            continue
        if not target.is_file():
            reasons.append("target_missing")
            continue
        state = inspect_cluster_target(
            blend,
            target,
            True,
            canonical_spm=canonical,
            verify_physical=True,
            validation_cache=validation_cache,
        )
        target_states.append({
            "target_spm": str(target),
            "status": state.get("status"),
            "refresh_reasons": list(state.get("refresh_reasons") or ()),
            "refresh_reason_categories": list(
                state.get("refresh_reason_categories") or ()
            ),
            "source_content_identity": state.get(
                "source_content_identity"
            ),
        })
        reasons.extend(state.get("refresh_reasons") or ())
        is_current = (
            state.get("status") == "synced"
            and state.get("connection_satisfied") is True
            and state.get("physical") is True
            and state.get("refresh_required") is False
            and state.get("refresh_deferred") is not True
        )
        if not is_current and not state.get("refresh_reasons"):
            reasons.append("relation_physical_proof_incomplete")
        evidence.append({
            "target_spm": str(target),
            "manifest": state.get("manifest"),
            "material": state.get("material"),
            "material_id": state.get("material_id"),
            "source_content_identity": state.get(
                "source_content_identity"
            ),
        })
    summary = _refresh_reason_summary(reasons)
    return {
        "current": not summary["refresh_reasons"],
        "verification": evidence,
        "targets": target_states,
        **summary,
    }


def _current_on_relation_evidence(blend, targets):
    """Compatibility wrapper returning proof only for a strict current state."""
    state = _current_on_relation_state(blend, targets)
    return state["verification"] if state["current"] else None


def _emit_cluster_relation_progress(callback, stage, message):
    if callback is None:
        print(message, flush=True)
        return
    try:
        callback(stage, message)
    except Exception:
        # Progress reporting must never turn a successful transaction into a
        # rollback. The authoritative result still comes from the worker.
        return


def _write_shared_repair_runtime_receipt(blend, repair_runtime_config):
    if not repair_runtime_config:
        return None
    from sk_batch.repair_runtime_contract import write_repair_runtime_receipt

    return write_repair_runtime_receipt(
        Path(blend).expanduser().absolute().with_suffix(".spm"),
        repair_runtime_config,
    )


def _validate_local_relation(blend, target_spm):
    blend = Path(blend).expanduser().absolute()
    target = Path(target_spm).expanduser().absolute()
    if blend.suffix.casefold() != ".blend" or not blend.name.casefold().startswith("sk_"):
        raise ClusterBlendSyncError(f"Cluster normalized blend must be SK_*.blend: {blend}")
    if blend.parent.name.casefold() != "cluster":
        raise ClusterBlendSyncError(f"Cluster blend must be directly inside Cluster: {blend}")
    owner = blend.parent.parent
    if target.parent != owner or target.suffix.casefold() != ".spm" or not target.name.casefold().startswith("sk_"):
        raise ClusterBlendSyncError(
            f"Cluster relationship target must be an owner-folder SK_*.spm: {target}"
        )
    if not blend.is_file():
        raise ClusterBlendSyncError(f"Cluster normalized blend does not exist: {blend}")
    if not target.is_file():
        raise ClusterBlendSyncError(f"Cluster relationship target does not exist: {target}")
    return blend, target


def set_cluster_relation_registry(blend, target_spm, enabled):
    """Change one exact ON/OFF sidecar row without modifying an SPM."""
    blend, target = _validate_local_relation(blend, target_spm)
    registry = load_target_registry(blend)
    current = [] if registry is None else [Path(value) for value in registry["target_spms"]]
    target_key = normalized_path_key(target)
    current_by_key = {normalized_path_key(path): Path(path) for path in current}
    if enabled:
        current_by_key[target_key] = target
    else:
        current_by_key.pop(target_key, None)
    return save_target_registry(blend, list(current_by_key.values()))


def _inspect_current_cluster_relation_state(
    blend,
    targets,
    *,
    auto_normalize=True,
    validation_cache=None,
):
    """Read the exact selected relation state without publishing anything."""

    registered_keys = {
        normalized_path_key(path) for path in _registry_target_spms(blend)
    }
    requested_keys = {normalized_path_key(path) for path in targets}
    if not requested_keys.issubset(registered_keys):
        return {
            "current": False,
            "registered": False,
            "targets": [],
            "verification": None,
            "refresh_reasons": [
                "Cluster relationship is not registered ON for every "
                "selected target"
            ],
            "refresh_reason_categories": ["relationship_registry"],
        }

    state = _current_on_relation_state(
        blend,
        targets,
        validation_cache=validation_cache,
    )
    state["registered"] = True
    material_freshness = (
        inspect_bwr_material_assignment_freshness(blend)
        if auto_normalize
        else None
    )
    if (
        state["current"]
        and material_freshness is not None
        and not material_freshness["current"]
    ):
        state["current"] = False
        state["refresh_reasons"] = list(dict.fromkeys(
            list(state.get("refresh_reasons") or [])
            + list(material_freshness["refresh_reasons"])
        ))
        state["refresh_reason_categories"] = list(dict.fromkeys(
            list(state.get("refresh_reason_categories") or [])
            + ["geometry_ownership"]
        ))
        state["bwr_material_assignment_freshness"] = material_freshness
    return state


def inspect_cluster_relation_current_state(
    blend,
    target_spms,
    *,
    auto_normalize=True,
    validation_cache=None,
):
    """Return a mutation-free current/stale result for selected SPMs only."""

    blend = Path(blend).expanduser().absolute()
    targets = []
    seen = set()
    for raw_target in target_spms:
        checked_blend, target = _validate_local_relation(blend, raw_target)
        if checked_blend != blend:
            raise ClusterBlendSyncError(
                "Selected Cluster relationships use different blends"
            )
        key = normalized_path_key(target)
        if key not in seen:
            seen.add(key)
            targets.append(target)
    if not targets:
        raise ClusterBlendSyncError(
            "No Cluster relationship target was selected"
        )
    return _inspect_current_cluster_relation_state(
        blend,
        targets,
        auto_normalize=auto_normalize,
        validation_cache=validation_cache,
    )


def run_cluster_relation_transaction(
    blend,
    target_spms,
    *,
    enabled,
    blender_exe,
    unit_probe_path=None,
    capture_resolution=1024,
    auto_normalize=True,
    repair_runtime_config=None,
    force_refresh=False,
    progress_callback=None,
    timeout=1800,
):
    """Apply ON through automatic Normalizer + Atlas, or reversible OFF.

    The Atlas addon writes each SPM as it walks the target list, so a failure on
    one target used to leave the earlier ones carrying generated meshes and
    rewired Generator slots with no adoption or scope manifest behind them.  The
    next apply then rejected that half-written state outright.  Every SPM the run
    can touch is therefore snapshotted up front and restored on any failure,
    alongside the existing Atlas target sidecar rollback.
    """
    blend = Path(blend).expanduser().absolute()
    targets = []
    seen = set()
    for raw_target in target_spms:
        checked_blend, target = _validate_local_relation(blend, raw_target)
        if checked_blend != blend:
            raise ClusterBlendSyncError("Selected Cluster relationships use different blends")
        key = normalized_path_key(target)
        if key not in seen:
            seen.add(key)
            targets.append(target)
    if not targets:
        raise ClusterBlendSyncError("No Cluster relationship target was selected")
    registered_before = _registry_target_spms(blend)
    # Registry ON/OFF is persistent user intent, not the execution scope.
    # The caller already selected the exact live content relations owned by
    # this producer. Expanding the worker back to every registered target
    # makes an unused/stale registry entry mutate or block an unrelated Tree.
    effective_targets = list(targets)
    registered_contract = (
        _merge_target_spms(registered_before, targets)
        if enabled
        else list(registered_before)
    )
    preflight_state = None
    if enabled and not force_refresh:
        _emit_cluster_relation_progress(
            progress_callback,
            "verify_existing_relation",
            "Verifying the existing Cluster ON relationship...",
        )
        preflight_state = _inspect_current_cluster_relation_state(
            blend,
            effective_targets,
            auto_normalize=auto_normalize,
        )
        if preflight_state["current"]:
            report = {
                "status": "ok",
                "mode": "sync",
                "blend": str(blend),
                "target_spms": [
                    str(path) for path in effective_targets
                ],
                "folder_relation": "on",
                "no_change": True,
                "already_on": True,
                "skip_reason": "already_on_up_to_date",
                "verification": preflight_state["verification"],
                "refresh_reasons": [],
                "refresh_reason_categories": [],
                "source_content_identity": (
                    preflight_state["targets"][0].get(
                        "source_content_identity"
                    )
                    if preflight_state["targets"]
                    else None
                ),
            }
            try:
                runtime_receipt = _write_shared_repair_runtime_receipt(
                    blend,
                    repair_runtime_config,
                )
            except OSError as exc:
                try:
                    diagnostic = _persist_cluster_relation_failure(
                        blend=blend,
                        targets=effective_targets,
                        enabled=True,
                        phase="no_op_runtime_receipt_failed",
                        command=[],
                        snapshots=[],
                        artifact_recipe=None,
                        launch_error=exc,
                    )
                    diagnostic_detail = (
                        f"\nFailure diagnostic log: {diagnostic}"
                    )
                except Exception as diagnostic_error:
                    diagnostic_detail = (
                        "\nCOULD NOT write failure diagnostic log: "
                        f"{type(diagnostic_error).__name__}: "
                        f"{diagnostic_error}"
                    )
                raise ClusterBlendSyncError(
                    "The existing Cluster relationship is current, but the "
                    "shared Blender/Normalizer completion receipt could not "
                    f"be committed: {exc}"
                    + diagnostic_detail
                ) from exc
            if runtime_receipt is not None:
                report["repair_runtime_receipt"] = str(runtime_receipt)
            _emit_cluster_relation_progress(
                progress_callback,
                "already_on_up_to_date",
                "Cluster relationship is already ON and up to date; "
                "Blender bake/export was skipped.",
            )
            return report
        _emit_cluster_relation_progress(
            progress_callback,
            "existing_relation_refresh_required",
            "Existing Cluster relationship requires refresh: "
            + ", ".join(preflight_state["refresh_reasons"]),
        )

    blender = Path(blender_exe).expanduser().absolute()
    if not blender.is_file():
        raise ClusterBlendSyncError(
            f"Blender executable does not exist: {blender}"
        )
    normalization_recipe = None
    source_preparation = None
    if enabled and auto_normalize:
        try:
            (
                normalization_recipe,
                source_preparation,
            ) = _resolve_normalization_recipe_with_source_rebuild(
                blend,
                effective_targets,
                blender_exe=blender,
                unit_probe_path=unit_probe_path,
                capture_resolution=capture_resolution,
                progress_callback=progress_callback,
            )
        except (
            ClusterNormalizationSyncError,
            ClusterSourcePreparationError,
        ) as exc:
            phase = (
                "source_preparation_failed"
                if isinstance(exc, ClusterSourcePreparationError)
                else "normalization_recipe_failed"
            )
            try:
                diagnostic = _persist_cluster_relation_failure(
                    blend=blend,
                    targets=effective_targets,
                    enabled=enabled,
                    phase=phase,
                    command=[],
                    snapshots=[],
                    artifact_recipe=None,
                    launch_error=exc,
                )
                diagnostic_detail = f"\nFailure diagnostic log: {diagnostic}"
            except Exception as diagnostic_error:
                diagnostic_detail = (
                    "\nCOULD NOT write failure diagnostic log: "
                    f"{type(diagnostic_error).__name__}: {diagnostic_error}"
                )
            raise ClusterBlendSyncError(
                str(exc) + diagnostic_detail
            ) from exc

    job = Path(__file__).resolve().parent / "spm_generator_sync" / "jobs" / "cluster_relation_job.py"
    if not job.is_file():
        raise ClusterBlendSyncError(f"Cluster relationship Blender job is missing: {job}")
    registry_path = registry_path_for_blend(blend)
    registry_before = registry_path.read_bytes() if registry_path.is_file() else None

    with tempfile.TemporaryDirectory(prefix="cluster_relation_") as temporary:
        report_path = Path(temporary) / "report.json"
        recipe_path = None
        if normalization_recipe is not None:
            recipe_path = Path(temporary) / "normalization_recipe.json"
            recipe_path.write_text(
                json.dumps(
                    normalization_recipe,
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )

        at_risk_targets = effective_targets if enabled else targets
        at_risk = {
            normalized_path_key(path): path
            for path in at_risk_targets
        }
        snapshots = []
        normalization_snapshots = []
        artifact_recipe = None
        command = [str(blender), "--factory-startup"]
        if enabled:
            command.extend(["--background", str(blend)])
        else:
            command.append("--background")
        command.extend([
            "--python", str(job), "--",
            "--mode", "sync" if enabled else "remove",
            "--blend", str(blend),
            "--report", str(report_path),
        ])
        for target in targets:
            command.extend(["--target", str(target)])
        if recipe_path is not None:
            command.extend(["--normalization-recipe", str(recipe_path)])

        def persist_failure(phase, *, report=None, result=None, error=None):
            existing_diagnostic = (
                str((report or {}).get("persistent_failure_report") or "")
                or None
            )
            try:
                diagnostic = _persist_cluster_relation_failure(
                    blend=blend,
                    targets=list(at_risk.values()),
                    enabled=enabled,
                    phase=phase,
                    command=command,
                    snapshots=snapshots,
                    artifact_recipe=artifact_recipe,
                    report=report,
                    result=result,
                    launch_error=error,
                    destination=existing_diagnostic,
                )
            except Exception as diagnostic_error:
                return (
                    "\nCOULD NOT write failure diagnostic log: "
                    f"{type(diagnostic_error).__name__}: {diagnostic_error}"
                )
            if existing_diagnostic:
                return ""
            return f"\nFailure diagnostic log: {diagnostic}"

        try:
            if enabled:
                for target in targets:
                    set_cluster_relation_registry(blend, target, True)
                registered_after = _registry_target_spms(blend)
                if {
                    normalized_path_key(path) for path in registered_after
                } != {
                    normalized_path_key(path) for path in registered_contract
                }:
                    raise ClusterBlendSyncError(
                        "Cluster target registry changed outside the requested "
                        "ON relation update."
                    )

            # The Blender job owns exactly the requested live relation slice.
            # Registered-but-unrequested targets are persistent intent only
            # and must remain outside this transaction.
            snapshots = _snapshot_spm_files(
                at_risk.values(), Path(temporary) / "spm_snapshots"
            )
            artifact_recipe = _atlas_transaction_artifact_recipe(
                blend,
                list(at_risk.values()),
                normalization_recipe,
            )
            normalization_snapshots = _snapshot_normalization_artifacts(
                artifact_recipe,
                Path(temporary) / "normalization_artifacts",
            )
        except Exception as preparation_error:
            diagnostic_detail = persist_failure(
                "preparation_failed",
                error=preparation_error,
            )
            if enabled:
                try:
                    _restore_registry_snapshot(
                        registry_path, registry_before
                    )
                except OSError as restore_error:
                    raise ClusterBlendSyncError(
                        "Cluster relationship preparation failed and the "
                        "target registry snapshot could not be restored: "
                        f"{restore_error}"
                        + diagnostic_detail
                    ) from preparation_error
                retry_contract = getattr(
                    preparation_error,
                    "connected_retry_contract",
                    None,
                )
                if isinstance(retry_contract, dict):
                    retry_contract["rollback_succeeded"] = True
            original_args = tuple(preparation_error.args)
            if original_args:
                preparation_error.args = (
                    str(original_args[0]) + diagnostic_detail,
                    *original_args[1:],
                )
            else:
                preparation_error.args = (diagnostic_detail.lstrip(),)
            raise

        def rollback(*, preserve_spm_paths=()):
            restored, failed = _restore_spm_files(
                snapshots,
                preserve_paths=preserve_spm_paths,
            )
            capture_restored, capture_failed = (
                _restore_normalization_artifacts(normalization_snapshots)
            )
            registry_restored = []
            registry_failed = []
            try:
                if _restore_registry_snapshot(
                    registry_path, registry_before
                ):
                    registry_restored.append(str(registry_path))
            except OSError as exc:
                registry_failed.append(f"{registry_path}: {exc}")
            detail = _rollback_detail(restored, failed)
            if preserve_spm_paths:
                detail += (
                    "\nPreserved concurrently modified SPM(s): "
                    + ", ".join(str(path) for path in preserve_spm_paths)
                )
            if registry_restored:
                detail += (
                    "\nRestored Cluster target registry: "
                    + ", ".join(registry_restored)
                )
            if registry_failed:
                detail += (
                    "\nCOULD NOT restore Cluster target registry: "
                    + "; ".join(registry_failed)
                )
            if capture_restored:
                detail += (
                    "\nRestored Cluster/Atlas transaction artifact(s): "
                    + ", ".join(capture_restored)
                )
            if capture_failed:
                detail += (
                    "\nCOULD NOT restore Cluster/Atlas transaction artifact(s): "
                    + "; ".join(capture_failed)
                )
            return detail

        heartbeat_stop = threading.Event()
        worker_started_at = time.monotonic()

        def heartbeat():
            while not heartbeat_stop.wait(
                CLUSTER_RELATION_HEARTBEAT_SECONDS
            ):
                elapsed = max(
                    1, int(time.monotonic() - worker_started_at)
                )
                _emit_cluster_relation_progress(
                    progress_callback,
                    "blender_worker_running",
                    "Blender background Cluster bake/export is still "
                    f"running ({elapsed}s elapsed)...",
                )

        heartbeat_thread = threading.Thread(
            target=heartbeat,
            name="cluster-relation-heartbeat",
            daemon=True,
        )
        _emit_cluster_relation_progress(
            progress_callback,
            "blender_worker_started",
            "Starting Blender background Cluster bake/export...",
        )
        heartbeat_thread.start()
        try:
            result = owned_run(
                command,
                source="cluster_blend_sync.apply_cluster_relationship",
                run_factory=subprocess.run,
                capture_output=True,
                text=True,
                timeout=int(timeout),
                creationflags=(0x08000000 if os.name == "nt" else 0),
            )
        except (subprocess.SubprocessError, OSError) as exc:
            raise ClusterBlendSyncError(
                f"Cluster relationship {'ON' if enabled else 'OFF'} apply did not "
                f"finish: {exc}"
                + persist_failure("launch_failed", error=exc)
                + rollback()
            ) from exc
        finally:
            heartbeat_stop.set()
            heartbeat_thread.join(timeout=1.0)
        _emit_cluster_relation_progress(
            progress_callback,
            "blender_worker_finished",
            "Blender background Cluster bake/export finished; "
            "validating its receipt...",
        )
        report = _read_json(report_path) if report_path.is_file() else None
        if result.returncode != 0 or not report or report.get("status") != "ok":
            detail = (report or {}).get("error") or (result.stderr or result.stdout)[-1200:]
            preserve_spm_paths = (
                _worker_transaction_conflict_preserve_paths(
                    report,
                    snapshots,
                )
            )
            raise ClusterBlendSyncError(
                f"Cluster relationship {'ON' if enabled else 'OFF'} apply failed: "
                f"{detail}"
                + persist_failure(
                    "worker_failed",
                    report=report,
                    result=result,
                )
                + rollback(preserve_spm_paths=preserve_spm_paths)
            )
        if enabled and repair_runtime_config:
            try:
                runtime_receipt = _write_shared_repair_runtime_receipt(
                    blend,
                    repair_runtime_config,
                )
            except OSError as exc:
                raise ClusterBlendSyncError(
                    "Cluster Sync completed but could not commit the shared "
                    f"Blender/Normalizer completion receipt: {exc}"
                    + persist_failure(
                        "runtime_receipt_failed",
                        report=report,
                        error=exc,
                    )
                    + rollback()
                ) from exc
            if runtime_receipt is not None:
                report["repair_runtime_receipt"] = str(runtime_receipt)
        if source_preparation is not None:
            report["source_preparation"] = source_preparation
        if preflight_state is not None and not preflight_state["current"]:
            report["preflight_refresh_required"] = True
            report["preflight_refresh_reasons"] = preflight_state[
                "refresh_reasons"
            ]
            report["preflight_refresh_reason_categories"] = (
                preflight_state["refresh_reason_categories"]
            )
            report["refresh_reasons"] = list(
                preflight_state["refresh_reasons"]
            )
            report["refresh_reason_categories"] = list(
                preflight_state["refresh_reason_categories"]
            )
            report["preflight_target_states"] = preflight_state["targets"]
        return report


def run_cluster_folder_relation_transaction(
    blend,
    *,
    enabled,
    blender_exe,
    unit_probe_path=None,
    capture_resolution=1024,
    auto_normalize=True,
    repair_runtime_config=None,
    force_refresh=False,
    progress_callback=None,
    timeout=1800,
):
    """Normalize one Cluster blend relationship across every owner SK SPM."""
    blend = Path(blend).expanduser().absolute()
    owner = blend.parent.parent
    # This pass only selects the folder's targets. The transaction below owns
    # the single authoritative physical receipt/hash verification, so hashing
    # the same source once here and again there only adds latency.
    discovered = discover_cluster_blend_relations(
        owner,
        verify_physical=False,
    )
    row = next(
        (
            candidate for candidate in discovered
            if normalized_path_key(candidate["blend"]) == normalized_path_key(blend)
        ),
        None,
    )
    if row is None:
        raise ClusterBlendSyncError(
            f"Cluster blend has no same-stem canonical or legacy SPM: {blend}"
        )
    owner_targets = [
        Path(target["target_spm"])
        for target in row.get("targets") or ()
        if target.get("owner_target")
    ]
    if enabled:
        selected = owner_targets
    else:
        selected = [
            Path(target["target_spm"])
            for target in row.get("targets") or ()
            if target.get("owner_target") and target.get("relation_on")
        ]
    if not selected:
        return {
            "status": "ok",
            "mode": "sync" if enabled else "remove",
            "blend": str(blend),
            "target_spms": [],
            "folder_relation": row.get("folder_relation"),
            "no_change": True,
        }
    result = run_cluster_relation_transaction(
        blend,
        selected,
        enabled=enabled,
        blender_exe=blender_exe,
        unit_probe_path=unit_probe_path,
        capture_resolution=capture_resolution,
        auto_normalize=auto_normalize,
        repair_runtime_config=repair_runtime_config,
        force_refresh=force_refresh,
        progress_callback=progress_callback,
        timeout=timeout,
    )
    result["folder_target_count"] = len(owner_targets)
    return result


__all__ = [
    "ClusterBlendSyncError",
    "cluster_authoring_sources",
    "discover_cluster_blend_relations",
    "inspect_cluster_relation_current_state",
    "normalized_blend_for_source",
    "normalized_path_key",
    "owner_sk_spms",
    "run_cluster_relation_transaction",
    "run_cluster_folder_relation_transaction",
    "set_cluster_relation_registry",
]
