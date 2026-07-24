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

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from atlas_target_registry import (
    TargetRegistryError,
    load_target_registry,
    registry_path_for_blend,
    save_target_registry,
)
from cluster_spm_pair_contract import (
    ClusterSpmPairPathError,
    resolve_cluster_spm_pair,
)


BACKUP_NAME_TOKENS = (
    "backup",
    "codex_backup",
    "pcgtex_backup",
    "skbatch_backup",
    ".pre_",
)


class ClusterBlendSyncError(RuntimeError):
    """Actionable Cluster blend relationship or apply failure."""


_FILE_HASH_CACHE = {}


def normalized_path_key(path):
    return os.path.normcase(os.path.abspath(str(Path(path)))).casefold()


def _is_live_file(path, suffix):
    path = Path(path)
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


def _restore_spm_files(snapshots):
    """Undo partial SPM writes; keep a rescue copy of anything unrestorable."""
    restored = []
    failed = []
    for path, copy in snapshots:
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


def _rollback_detail(restored, failed):
    lines = []
    if restored:
        lines.append("Rolled back SPM(s): " + ", ".join(restored))
    if failed:
        lines.append("COULD NOT roll back: " + "; ".join(failed))
    return ("\n" + "\n".join(lines)) if lines else ""


def _sha256_file(path):
    path = Path(path).expanduser().absolute()
    stat = path.stat()
    key = normalized_path_key(path)
    signature = (int(stat.st_size), int(stat.st_mtime_ns))
    cached = _FILE_HASH_CACHE.get(key)
    if cached and cached["signature"] == signature:
        return cached["sha256"]
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    value = digest.hexdigest()
    _FILE_HASH_CACHE[key] = {"signature": signature, "sha256": value}
    return value


def _physical_capture_manifest_for_blend(blend):
    blend = Path(blend).expanduser().absolute()
    stem = blend.stem
    if stem.casefold().startswith("sk_"):
        stem = stem[3:]
    path = blend.with_name(f"{stem}_auto_capture_manifest.json")
    payload = _read_json(path)
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


def _recorded_source_hashes(receipt, canonical_spm):
    canonical_key = normalized_path_key(canonical_spm)
    hashes = set()
    for row in _walk_dicts(receipt):
        source_path = str(row.get("source_spm") or "").strip()
        source_hash = str(row.get("source_spm_sha256") or "").strip().casefold()
        if (
            source_path
            and source_hash
            and normalized_path_key(source_path) == canonical_key
        ):
            hashes.add(source_hash)
    return hashes


def _physical_refresh_state(payload, canonical_spm, blend):
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
    canonical = Path(canonical_spm).expanduser().absolute()
    recorded_hashes = _recorded_source_hashes(receipt, canonical)
    current_source_sha256 = _sha256_file(canonical) if canonical.is_file() else None
    if not current_source_sha256:
        reasons.append("canonical_source_missing")
    elif len(recorded_hashes) > 1:
        reasons.append("recorded_source_conflict")
    elif recorded_hashes and recorded_hashes != {current_source_sha256}:
        reasons.append("canonical_source_changed")

    capture = _physical_capture_manifest_for_blend(blend)
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
        "recorded_source_sha256": (
            next(iter(recorded_hashes)) if len(recorded_hashes) == 1 else None
        ),
        "capture_manifest": str(capture["path"]),
        "capture_contract_sha256": capture["contract_sha256"],
        "recorded_capture_contract_sha256": recorded_capture_sha256,
    }


def _matching_scope_manifest(blend, target_spm):
    """Return the newest Atlas scope manifest for this exact blend/target."""
    blend = Path(blend).expanduser().absolute()
    target = Path(target_spm).expanduser().absolute()
    scope_dir = target.parent / ".atlas_leaf_speedtree_scopes"
    if not scope_dir.is_dir():
        return None
    matches = []
    for path in scope_dir.glob(f"*__{target.stem}.json"):
        payload = _read_json(path)
        if payload is None:
            continue
        payload_blend = str(payload.get("blend_file") or "").strip()
        payload_spm = str(payload.get("spm") or "").strip()
        if not payload_blend or normalized_path_key(payload_blend) != normalized_path_key(blend):
            continue
        if payload_spm and normalized_path_key(payload_spm) != normalized_path_key(target):
            continue
        matches.append((path.stat().st_mtime_ns, path, payload))
    if not matches:
        return None
    _mtime, path, payload = max(matches, key=lambda row: row[0])
    return {"path": str(path), "payload": payload}


def inspect_cluster_target(
    blend,
    target_spm,
    relation_on,
    *,
    canonical_spm=None,
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
    canonical = (
        Path(canonical_spm).expanduser().absolute()
        if canonical_spm
        else Path(blend).expanduser().absolute().with_suffix(".spm")
    )
    refresh = _physical_refresh_state(payload, canonical, blend)
    refresh_required = complete and refresh["refresh_required"]
    return {
        "status": (
            "refresh_required"
            if refresh_required
            else "synced" if complete
            else "attention"
        ),
        "connected": complete,
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
        "mesh_ids": list(payload.get("mesh_ids") or ()),
        **refresh,
    }


def discover_cluster_blend_relations(owner_folder):
    """Discover normalized Cluster blends and their owner-folder ON/OFF state.

    The target rows remain available as audit evidence for Blender/PCG, but the
    user-facing relationship is one state per blend across the complete set of
    owner-folder ``SK_*.spm`` files.
    """
    owner = Path(owner_folder).expanduser().absolute()
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
            "refresh_reasons": sorted({
                reason
                for relation in owner_relations
                for reason in relation.get("refresh_reasons") or ()
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


def run_cluster_relation_transaction(
    blend,
    target_spms,
    *,
    enabled,
    blender_exe,
    timeout=1800,
):
    """Apply ON through Atlas build, or OFF through reversible Atlas removal.

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
    blender = Path(blender_exe).expanduser().absolute()
    if not blender.is_file():
        raise ClusterBlendSyncError(f"Blender executable does not exist: {blender}")

    job = Path(__file__).resolve().parent / "spm_generator_sync" / "jobs" / "cluster_relation_job.py"
    if not job.is_file():
        raise ClusterBlendSyncError(f"Cluster relationship Blender job is missing: {job}")
    registry_path = registry_path_for_blend(blend)
    registry_before = registry_path.read_bytes() if registry_path.is_file() else None

    with tempfile.TemporaryDirectory(prefix="cluster_relation_") as temporary:
        report_path = Path(temporary) / "report.json"

        if enabled:
            for target in targets:
                set_cluster_relation_registry(blend, target, True)

        # The Blender job rebuilds every registered target, not only the selected
        # ones, so the whole registry is at risk - not just ``targets``.
        at_risk = {normalized_path_key(path): path for path in targets}
        for path in _registry_target_spms(blend):
            at_risk.setdefault(normalized_path_key(path), path)
        snapshots = _snapshot_spm_files(
            at_risk.values(), Path(temporary) / "spm_snapshots"
        )

        def rollback():
            restored, failed = _restore_spm_files(snapshots)
            if enabled:
                if registry_before is None:
                    registry_path.unlink(missing_ok=True)
                else:
                    registry_path.write_bytes(registry_before)
            return _rollback_detail(restored, failed)

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
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=int(timeout),
                creationflags=(0x08000000 if os.name == "nt" else 0),
            )
        except (subprocess.SubprocessError, OSError) as exc:
            raise ClusterBlendSyncError(
                f"Cluster relationship {'ON' if enabled else 'OFF'} apply did not "
                f"finish: {exc}" + rollback()
            ) from exc
        report = _read_json(report_path) if report_path.is_file() else None
        if result.returncode != 0 or not report or report.get("status") != "ok":
            detail = (report or {}).get("error") or (result.stderr or result.stdout)[-1200:]
            raise ClusterBlendSyncError(
                f"Cluster relationship {'ON' if enabled else 'OFF'} apply failed: "
                f"{detail}" + rollback()
            )
        return report


def run_cluster_folder_relation_transaction(
    blend,
    *,
    enabled,
    blender_exe,
    timeout=1800,
):
    """Normalize one Cluster blend relationship across every owner SK SPM."""
    blend = Path(blend).expanduser().absolute()
    owner = blend.parent.parent
    discovered = discover_cluster_blend_relations(owner)
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
        timeout=timeout,
    )
    result["folder_target_count"] = len(owner_targets)
    return result


__all__ = [
    "ClusterBlendSyncError",
    "cluster_authoring_sources",
    "discover_cluster_blend_relations",
    "normalized_blend_for_source",
    "normalized_path_key",
    "owner_sk_spms",
    "run_cluster_relation_transaction",
    "run_cluster_folder_relation_transaction",
    "set_cluster_relation_registry",
]
