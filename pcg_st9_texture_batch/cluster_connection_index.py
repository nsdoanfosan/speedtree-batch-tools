"""Bounded, content-validated Cluster connection inventory for SK Batch.

The PCG board's full SPM analysis intentionally computes far more than the SK
Batch list needs.  This module preserves the exact Cluster-link semantics but
projects only material, visibility, leaf-lineage, and optional marker evidence.
Every persisted projection is keyed by a full-file SHA-256 captured from the
current bounded inventory; referenced texture existence and marker receipts are
revalidated live on every scan.
"""
from __future__ import annotations

import gzip
import hashlib
import html
import os
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

from cluster_spm_pair_contract import (
    inspect_cluster_spm_pair,
    resolve_cluster_spm_pair,
)
from speedtree_legacy_cluster_contract import (
    HANDOFF_STAT_MISMATCH_REASON,
    SNAPSHOT_SCAN_FAILED_REASON,
    generator_foregrounds_from_decoded_text,
    inspect_legacy_cluster_state,
    marker_receipt_path,
)

from . import pcg_texture_audit as audit
from .pcg_startup_cache import (
    BoundedDiscoveryError,
    ContentAddressedJsonCache,
    ContentIdentityError,
    canonical_json_sha256,
    content_identity,
    path_key,
)
from .pcg_texture_common import SHARED_CACHE_DIR


CLUSTER_CONNECTION_PROJECTION_SCHEMA = 1
CLUSTER_CONNECTION_CACHE_PATH = (
    SHARED_CACHE_DIR / "sk_cluster_connection_projection_v1.json"
)
CLUSTER_CONNECTION_CACHE_KIND = "sk_cluster_connection_projection"
DEFAULT_MAX_CONNECTION_FILES = 2_048
DEFAULT_MAX_PROJECTION_WORKERS = 16
PARALLEL_PROJECTION_MIN_BYTES = 4 * 1024 * 1024

_SOURCE_MODE = "source_material_refs"
_TARGET_MODE = "target_connection_semantics"
_TARGET_FOREGROUNDS_MODE = "target_connection_semantics_with_foregrounds"
_MODE_STRENGTH = {
    _SOURCE_MODE: 0,
    _TARGET_MODE: 1,
    _TARGET_FOREGROUNDS_MODE: 2,
}


def _stable_bytes(path):
    candidate = Path(path)
    for _attempt in range(2):
        before = candidate.stat()
        raw = candidate.read_bytes()
        after = candidate.stat()
        if (
            (before.st_size, before.st_mtime_ns)
            == (after.st_size, after.st_mtime_ns)
            and len(raw) == after.st_size
        ):
            return raw, after
    raise ContentIdentityError(
        f"Cluster connection input changed while reading: {candidate}"
    )


def _material_rows_from_text(text):
    rows = []
    for block_match in audit.MATERIAL_BLOCK_RE.finditer(text):
        block = block_match.group(0)
        name_match = audit.MATERIAL_RE.search(block)
        id_match = audit.MATERIAL_ID_RE.search(block)
        refs = []
        for ref_match in audit.TEX_FILENAME_RE.finditer(block):
            value = html.unescape(ref_match.group(1).strip())
            if value and Path(value).suffix.lower() in audit.IMAGE_EXTS:
                refs.append(value.replace("/", "\\"))
        rows.append({
            "material_id": id_match.group(1) if id_match else None,
            "material_name": (
                html.unescape(name_match.group(2)) if name_match else ""
            ),
            "refs": audit.unique(refs),
            "managed_leaf_output": audit._material_is_managed_leaf_output(
                block
            ),
        })
    return rows


def _target_connection_semantics(text):
    """Compute the two PCG connection projections in one Generator walk.

    This is the shared subset of ``_visible_material_ids_from_text`` and
    ``_leaf_generator_bindings_from_text``.  The rules are intentionally the
    same; only PCG report-only binding diagnostics are omitted.
    """
    referenced_material_ids = audit._referenced_material_ids_from_text(text)
    export_node_counts, total_nodes = audit._export_node_counts_from_text(text)
    generators = []
    hidden_by_guid = {}
    generator_material_ids = set()
    for generator_index, block_match in enumerate(
        audit.GENERATOR_BLOCK_RE.finditer(text)
    ):
        block = block_match.group(0)
        material_ids = {
            html.unescape(match.group(2).strip())
            for match in audit.ACTIVE_MATERIAL_VALUE_RE.finditer(block)
        }
        generator_material_ids.update(material_ids)
        guid_match = audit.ELEMENT_GUID_RE.search(block)
        hidden_match = audit.ELEMENT_HIDDEN_RE.search(block)
        guid = guid_match.group(1).strip() if guid_match else ""
        guid_key = audit._generator_guid_key(guid)
        own_hidden = bool(
            hidden_match
            and hidden_match.group(1).strip().casefold() == "true"
        )
        if guid_key:
            hidden_by_guid[guid_key] = own_hidden
        type_match = audit.GENERATOR_TYPE_RE.search(block)
        generator_type = (
            html.unescape(type_match.group(1).strip())
            if type_match else ""
        )
        normalized_type = audit._normalized_generator_type(generator_type)
        properties = []
        if normalized_type in {"branch", "trunk"} \
                or audit._is_leaf_mesh_generator_type(generator_type):
            for property_match in audit.PROPERTY_BLOCK_RE.finditer(block):
                property_block = property_match.group(0)
                property_name = audit.PROPERTY_NAME_RE.search(property_block)
                property_value = audit.PROPERTY_VALUE_RE.search(property_block)
                if not property_name or not property_value:
                    continue
                properties.append((
                    html.unescape(property_name.group(1).strip()),
                    html.unescape(property_value.group(1).strip()),
                ))
        contributes_render_geometry = True
        if normalized_type in {"branch", "trunk"}:
            property_map = dict(properties)
            if (
                "Skin:Type" in property_map
                or "Segments:Features:Mesh:Enabled" in property_map
            ):
                contributes_render_geometry = (
                    audit.branch_generator_has_render_geometry(property_map)
                )
        generators.append({
            "generator_index": generator_index,
            "guid": guid,
            "guid_key": guid_key,
            "own_hidden": own_hidden,
            "generator_type": generator_type,
            "material_ids": material_ids,
            "properties": properties,
            "contributes_render_geometry": contributes_render_geometry,
        })

    parent = {}
    for link_match in audit.GENERATOR_LINK_RE.finditer(text):
        block = link_match.group(0)
        source = audit.LINK_SOURCE_GUID_RE.search(block)
        target = audit.LINK_TARGET_GUID_RE.search(block)
        if source and target:
            source_key = audit._generator_guid_key(source.group(1))
            target_key = audit._generator_guid_key(target.group(1))
            if source_key and target_key:
                parent[target_key] = source_key

    def effectively_hidden(guid_key, own_hidden=False):
        if own_hidden:
            return True
        guid = guid_key
        seen = set()
        while guid and guid not in seen:
            seen.add(guid)
            if hidden_by_guid.get(guid):
                return True
            guid = parent.get(guid, "")
        return False

    visible_material_ids = referenced_material_ids - generator_material_ids
    leaf_bindings = []
    for generator in generators:
        guid_key = generator["guid_key"]
        graph_visible = not effectively_hidden(
            guid_key, generator["own_hidden"]
        )
        has_export_nodes = (
            not total_nodes
            or not guid_key
            or bool(export_node_counts.get(guid_key, 0))
        )
        if (
            graph_visible
            and has_export_nodes
            and generator["contributes_render_geometry"]
        ):
            visible_material_ids.update(generator["material_ids"])
        if not audit._is_leaf_mesh_generator_type(
            generator["generator_type"]
        ):
            continue
        export_participates = bool(graph_visible and has_export_nodes)
        for property_name, material_id in generator["properties"]:
            if not property_name.casefold().endswith(":material"):
                continue
            leaf_bindings.append({
                "generator_guid": generator["guid"],
                "material_id": material_id,
                "export_participates": export_participates,
            })
    return {
        "referenced_material_ids": sorted(referenced_material_ids),
        "visible_material_ids": sorted(visible_material_ids),
        "leaf_generator_bindings": leaf_bindings,
    }


def _build_projection(task):
    """Process-safe exact projection builder for one already-identified SPM."""
    path_text, mode, expected_sha256 = task
    path = Path(path_text)
    raw, stat = _stable_bytes(path)
    actual_sha256 = hashlib.sha256(raw).hexdigest()
    if actual_sha256 != expected_sha256:
        raise ContentIdentityError(
            f"Cluster connection input changed after inventory: {path}"
        )
    decoded = gzip.decompress(raw) if raw[:2] == b"\x1f\x8b" else raw
    text = decoded.decode("utf-8", errors="replace")
    projection = {
        "schema_version": CLUSTER_CONNECTION_PROJECTION_SCHEMA,
        "mode": mode,
        "material_rows": _material_rows_from_text(text),
    }
    if mode != _SOURCE_MODE:
        projection.update(_target_connection_semantics(text))
        if mode == _TARGET_FOREGROUNDS_MODE:
            foregrounds, duplicate_guids = (
                generator_foregrounds_from_decoded_text(text)
            )
            projection["generator_foregrounds_snapshot"] = [
                foregrounds,
                sorted(duplicate_guids),
            ]
    return path_text, mode, actual_sha256, stat.st_size, stat.st_mtime_ns, projection


def _merge_requirement(requirements, path, mode):
    candidate = Path(path).absolute()
    key = path_key(candidate)
    existing = requirements.get(key)
    if existing is None or _MODE_STRENGTH[mode] > _MODE_STRENGTH[
        existing["mode"]
    ]:
        requirements[key] = {"path": candidate, "mode": mode}


def _projection_namespace(path, mode):
    return (
        f"{path_key(path)}|schema={CLUSTER_CONNECTION_PROJECTION_SCHEMA}"
        f"|mode={mode}"
    )


def _projection_identity(content_sha256, mode):
    return canonical_json_sha256({
        "schema_version": CLUSTER_CONNECTION_PROJECTION_SCHEMA,
        "mode": mode,
        "content_sha256": str(content_sha256).casefold(),
        "content_identity_algorithm": "sha256-full-v1",
    })


def _projection_is_valid(value, mode):
    if (
        not isinstance(value, dict)
        or value.get("schema_version")
        != CLUSTER_CONNECTION_PROJECTION_SCHEMA
        or value.get("mode") != mode
        or not isinstance(value.get("material_rows"), list)
    ):
        return False
    if mode == _SOURCE_MODE:
        return True
    return all(
        isinstance(value.get(field), list)
        for field in (
            "referenced_material_ids",
            "visible_material_ids",
            "leaf_generator_bindings",
        )
    ) and (
        mode != _TARGET_FOREGROUNDS_MODE
        or isinstance(value.get("generator_foregrounds_snapshot"), list)
    )


def _projection_from_shared_analysis(requirement, identity_row):
    """Reuse PCG's compact semantic row after the current full-SHA proof."""
    persistent = audit._persistent_spm_analysis()
    entry = persistent.get(str(requirement["path"]).lower())
    if (
        not isinstance(entry, dict)
        or entry.get("content_identity_algorithm")
        != audit.SPM_CONTENT_IDENTITY_ALGORITHM
        or entry.get("content_identity_sha256")
        != identity_row["fingerprint"]
        or not isinstance(entry.get("material_rows"), list)
    ):
        return None
    mode = requirement["mode"]
    projection = {
        "schema_version": CLUSTER_CONNECTION_PROJECTION_SCHEMA,
        "mode": mode,
        "material_rows": entry["material_rows"],
    }
    if mode == _SOURCE_MODE:
        return projection
    if (
        entry.get("leaf_binding_schema") != 6
        or not isinstance(entry.get("referenced_material_ids"), list)
        or not isinstance(entry.get("visible_material_ids"), list)
        or not isinstance(entry.get("leaf_generator_bindings"), list)
    ):
        return None
    projection.update({
        "referenced_material_ids": entry["referenced_material_ids"],
        "visible_material_ids": entry["visible_material_ids"],
        "leaf_generator_bindings": [
            {
                "generator_guid": row.get("generator_guid"),
                "material_id": row.get("material_id"),
                "export_participates": bool(row.get("export_participates")),
            }
            for row in entry["leaf_generator_bindings"]
            if isinstance(row, dict)
        ],
    })
    if mode == _TARGET_FOREGROUNDS_MODE:
        if (
            entry.get("legacy_marker_schema") != 1
            or entry.get("generator_foregrounds") is None
        ):
            return None
        projection["generator_foregrounds_snapshot"] = [
            entry.get("generator_foregrounds") or {},
            entry.get("duplicate_generator_guids") or [],
        ]
    return projection


def _projection_worker_count(misses, identity_by_path, max_workers):
    if len(misses) < 2:
        return 1
    total_bytes = sum(
        int(identity_by_path[key].get("size") or 0) for key in misses
    )
    if total_bytes < PARALLEL_PROJECTION_MIN_BYTES:
        return 1
    available = os.cpu_count() or 1
    return min(
        len(misses),
        max(1, int(max_workers)),
        max(1, int(available)),
    )


def _load_projection_batch(
    requirements,
    *,
    cache,
    max_files,
    max_workers,
    metrics,
):
    if not requirements:
        return {}, {}
    if len(requirements) > int(max_files):
        raise BoundedDiscoveryError(
            "Cluster connection inventory needs "
            f"{len(requirements)} SPMs; bound is {max_files}"
        )
    ordered = [requirements[key] for key in sorted(requirements)]
    identity_started = time.perf_counter()
    identity = content_identity(
        [row["path"] for row in ordered],
        membership=(
            f"{key}|{requirements[key]['mode']}" for key in sorted(requirements)
        ),
        max_files=max_files,
        exact=True,
        workers=min(max_workers, len(ordered)),
    )
    metrics["content_identity_seconds"] += (
        time.perf_counter() - identity_started
    )
    identity_by_path = {row["path"]: row for row in identity["files"]}
    for key, requirement in requirements.items():
        identity_by_path[key]["_source_path"] = requirement["path"]
    cache_requests = []
    cache_keys = {}
    for key, requirement in requirements.items():
        row = identity_by_path[key]
        namespace = _projection_namespace(requirement["path"], requirement["mode"])
        identity_sha256 = _projection_identity(
            row["fingerprint"], requirement["mode"]
        )
        cache_keys[key] = (namespace, identity_sha256)
        cache_requests.append((namespace, identity_sha256))
    cached = cache.get_many(cache_requests)
    projections = {}
    misses = []
    cache_updates = []
    for key, requirement in requirements.items():
        namespace, _identity_sha256 = cache_keys[key]
        value = cached.get(namespace)
        if _projection_is_valid(value, requirement["mode"]):
            projections[key] = value
            metrics["projection_cache_hits"] += 1
        else:
            metrics["projection_cache_misses"] += 1
            shared = _projection_from_shared_analysis(
                requirement, identity_by_path[key]
            )
            if _projection_is_valid(shared, requirement["mode"]):
                projections[key] = shared
                metrics["shared_analysis_hits"] += 1
                namespace, identity_sha256 = cache_keys[key]
                cache_updates.append((namespace, identity_sha256, shared))
            else:
                metrics["shared_analysis_misses"] += 1
                misses.append(key)

    worker_count = _projection_worker_count(
        misses, identity_by_path, max_workers
    )
    metrics["projection_parse_workers"] = max(
        metrics["projection_parse_workers"], worker_count if misses else 0
    )
    tasks = [
        (
            str(requirements[key]["path"]),
            requirements[key]["mode"],
            identity_by_path[key]["fingerprint"],
        )
        for key in misses
    ]
    parse_started = time.perf_counter()
    if worker_count > 1:
        with ProcessPoolExecutor(max_workers=worker_count) as executor:
            built = list(executor.map(_build_projection, tasks))
    else:
        built = [_build_projection(task) for task in tasks]
    metrics["projection_seconds"] += time.perf_counter() - parse_started

    for path_text, mode, digest, size, mtime_ns, projection in built:
        key = path_key(path_text)
        expected = identity_by_path[key]
        if (
            digest != expected["fingerprint"]
            or size != expected["size"]
            or mtime_ns != expected["mtime_ns"]
        ):
            raise ContentIdentityError(
                f"Cluster connection projection changed during build: {path_text}"
            )
        projections[key] = projection
        namespace, identity_sha256 = cache_keys[key]
        cache_updates.append((namespace, identity_sha256, projection))
    if cache_updates:
        cache.put_many(cache_updates)
    return projections, identity_by_path


def _unique_paths(values):
    result = []
    seen = set()
    for value in values:
        candidate = Path(value)
        key = path_key(candidate)
        if key in seen:
            continue
        seen.add(key)
        result.append(candidate)
    return result


def _owner_plan(owner, clusters):
    owner = Path(owner).absolute()
    roots = audit.root_spms(owner)
    preferred = [
        path for path in roots if path.name.casefold().startswith("sk_")
    ]
    loose = [
        path for path in roots if path.name.casefold().startswith("sk")
    ]
    sources = [
        path for path in roots if not path.name.casefold().startswith("sk")
    ]
    targets = preferred or loose or sources
    dependencies, _assembly_sources = audit.cluster_dependency_spms(targets)
    source_candidates = {}
    for target in targets:
        target = Path(target)
        if not target.name.casefold().startswith("sk_"):
            continue
        tail = target.stem[3:]
        sm_expected = owner / f"SM_{tail}.spm"
        expected = owner / target.name[3:]
        source_candidates[path_key(target)] = _unique_paths(
            [
                path
                for path in (sm_expected, expected)
                if path.is_file()
            ]
            + sources
        )
    return {
        "owner": owner,
        "clusters": _unique_paths(clusters),
        "targets": [Path(path) for path in targets],
        "dependencies": [Path(path) for path in dependencies],
        "sources": sources,
        "source_candidates": source_candidates,
    }


def _legacy_only_material_ids(path, projection):
    snapshot = projection.get("generator_foregrounds_snapshot")
    legacy = inspect_legacy_cluster_state(
        path,
        foregrounds_snapshot=snapshot,
    )
    fatal_reasons = [
        reason for reason in legacy.get("reason_tokens", [])
        if reason in {
            HANDOFF_STAT_MISMATCH_REASON,
            SNAPSHOT_SCAN_FAILED_REASON,
        }
    ]
    if fatal_reasons:
        reason = fatal_reasons[0]
        evidence = (
            legacy.get("handoff_evidence")
            if reason == HANDOFF_STAT_MISMATCH_REASON
            else legacy.get("failure_evidence")
        ) or {}
        raise RuntimeError(f"{reason}: {evidence}")
    legacy_guids = set(legacy.get("classified_generator_guids") or ())
    grouped = {}
    for binding in projection.get("leaf_generator_bindings") or ():
        if not binding.get("export_participates"):
            continue
        material_id = str(binding.get("material_id") or "").strip()
        if not material_id:
            continue
        grouped.setdefault(material_id, []).append(
            str(binding.get("generator_guid") or "") in legacy_guids
        )
    return {
        material_id for material_id, legacy_flags in grouped.items()
        if legacy_flags and all(legacy_flags)
    }


def _needed_original_ref_keys(target, projection, legacy_only_ids):
    if not Path(target).name.casefold().startswith("sk_"):
        return set()
    referenced = set(projection.get("referenced_material_ids") or ())
    visible = set(projection.get("visible_material_ids") or ())
    needed = set()
    for material in projection.get("material_rows") or ():
        material_id = material.get("material_id")
        if referenced and material_id not in visible:
            continue
        if str(material_id or "") in legacy_only_ids:
            continue
        if material.get("managed_leaf_output"):
            continue
        refs = material.get("refs") or ()
        if audit._refs_are_only_managed_outputs(refs):
            key = audit.canonical_material_name(
                material.get("material_name")
            ).casefold()
            if key:
                needed.add(key)
    return needed


def _source_ref_map(candidates, needed, projections):
    result = {}
    for source in candidates:
        projection = projections.get(path_key(source))
        if projection is None:
            continue
        for row in projection.get("material_rows") or ():
            key = audit.canonical_material_name(
                row.get("material_name")
            ).casefold()
            refs = row.get("refs") or ()
            if key in needed and refs and key not in result:
                result[key] = list(refs)
        if needed.issubset(result):
            break
    return result


def _cluster_usage(plan, projections, original_refs_by_target):
    by_stem = {}
    for cluster in plan["clusters"]:
        pair = resolve_cluster_spm_pair(cluster)
        canonical = Path(pair["canonical_spm"])
        mirror = Path(pair["mirror_spm"])
        dependency = canonical if canonical.is_file() else mirror
        by_stem[mirror.stem.casefold()] = (dependency, mirror)
        by_stem[canonical.stem.casefold()] = (dependency, canonical)
    found = {}
    for spm in plan["dependencies"]:
        projection = projections[path_key(spm)]
        referenced_ids = set(
            projection.get("referenced_material_ids") or ()
        )
        visible_ids = set(projection.get("visible_material_ids") or ())
        legacy_only_ids = _legacy_only_material_ids(spm, projection)
        original_refs = original_refs_by_target.get(path_key(spm), {})
        for material in projection.get("material_rows") or ():
            material_id = material.get("material_id")
            if referenced_ids and material_id not in visible_ids:
                continue
            if str(material_id or "") in legacy_only_ids:
                continue
            if material.get("managed_leaf_output"):
                continue
            refs = list(material.get("refs") or ())
            canonical_name = audit.canonical_material_name(
                material.get("material_name")
            ).casefold()
            if (
                audit._refs_are_only_managed_outputs(refs)
                and original_refs.get(canonical_name)
            ):
                refs = original_refs[canonical_name]
            matched = {
                path_key(by_stem[Path(ref).stem.casefold()][0]):
                    by_stem[Path(ref).stem.casefold()]
                for ref in refs
                if Path(ref).stem.casefold() in by_stem
            }
            for cluster, output_mirror in matched.values():
                key = path_key(cluster)
                usage = found.setdefault(key, {
                    "spms": [],
                    "material_names": [],
                    "connected_refs": [],
                    "missing_source_refs": [],
                })
                if str(spm) not in usage["spms"]:
                    usage["spms"].append(str(spm))
                name = material.get("material_name")
                if name not in usage["material_names"]:
                    usage["material_names"].append(name)
                for ref in refs:
                    resolved = audit.resolve_spm_image_ref(spm, ref)
                    text = str(resolved)
                    if text.casefold() not in {
                        value.casefold() for value in usage["connected_refs"]
                    }:
                        usage["connected_refs"].append(text)
                    if not audit.path_exists(resolved) and text.casefold() not in {
                        value.casefold()
                        for value in usage["missing_source_refs"]
                    }:
                        usage["missing_source_refs"].append(text)
    return found


def _connection_rows(plan, usage):
    rows = []
    for source in plan["clusters"]:
        pair = inspect_cluster_spm_pair(source)
        canonical = Path(pair["canonical_spm"])
        mirror = Path(pair["mirror_spm"])
        connection = (
            usage.get(path_key(source))
            or usage.get(path_key(canonical))
            or usage.get(path_key(mirror))
            or {}
        )
        connected = audit.unique(connection.get("connected_refs") or ())
        if not connected:
            continue
        rows.append({
            "kind": "cluster_spm",
            "source_spm": str(canonical),
            "authoring_spm": str(canonical),
            "output_spm": str(canonical),
            "mirror_spm": str(mirror),
            "legacy_output_spm": str(mirror),
            "referenced": True,
            "referenced_by_spms": list(connection.get("spms") or ()),
            "target_material_names": list(
                connection.get("material_names") or ()
            ),
            "cluster_output_textures": connected,
            "missing_cluster_output_textures": audit.unique(
                connection.get("missing_source_refs") or ()
            ),
        })
    return sorted(rows, key=lambda row: row["source_spm"].casefold())


def _stats_still_match(identity_rows):
    for row in identity_rows.values():
        try:
            stat = Path(row["_source_path"]).stat()
        except OSError:
            return False
        if (
            stat.st_size != row["size"]
            or stat.st_mtime_ns != row["mtime_ns"]
        ):
            return False
    return True


def _scan_once(
    owner_clusters,
    *,
    cache_path,
    max_files,
    max_workers,
    metrics,
):
    plans = [
        _owner_plan(owner, clusters)
        for owner, clusters in owner_clusters.items()
    ]
    if len(plans) > int(max_files):
        raise BoundedDiscoveryError(
            f"Cluster connection owner count exceeds bound {max_files}"
        )
    requirements = {}
    for plan in plans:
        for dependency in plan["dependencies"]:
            mode = (
                _TARGET_FOREGROUNDS_MODE
                if marker_receipt_path(dependency).is_file()
                else _TARGET_MODE
            )
            _merge_requirement(requirements, dependency, mode)
        for candidates in plan["source_candidates"].values():
            for candidate in candidates[:2]:
                if candidate.name.casefold().startswith("sm_"):
                    _merge_requirement(requirements, candidate, _SOURCE_MODE)
    cache = ContentAddressedJsonCache(
        cache_path,
        CLUSTER_CONNECTION_CACHE_KIND,
        max_entries=max_files,
    )
    projections, identities = _load_projection_batch(
        requirements,
        cache=cache,
        max_files=max_files,
        max_workers=max_workers,
        metrics=metrics,
    )

    needed_by_target = {}
    fallback_requirements = {}
    for plan in plans:
        for target in plan["targets"]:
            if not target.name.casefold().startswith("sk_"):
                continue
            projection = projections[path_key(target)]
            legacy_only = _legacy_only_material_ids(target, projection)
            needed = _needed_original_ref_keys(
                target, projection, legacy_only
            )
            needed_by_target[path_key(target)] = needed
            if not needed:
                continue
            candidates = plan["source_candidates"].get(
                path_key(target), []
            )
            current = _source_ref_map(candidates, needed, projections)
            if needed.issubset(current):
                continue
            for candidate in candidates:
                if path_key(candidate) not in projections:
                    _merge_requirement(
                        fallback_requirements, candidate, _SOURCE_MODE
                    )
    if len(set(requirements) | set(fallback_requirements)) > int(max_files):
        raise BoundedDiscoveryError(
            "Cluster connection inventory exceeded the shared file bound "
            f"({max_files})"
        )
    fallback, fallback_identities = _load_projection_batch(
        fallback_requirements,
        cache=cache,
        max_files=max_files,
        max_workers=max_workers,
        metrics=metrics,
    )
    projections.update(fallback)
    identities.update(fallback_identities)

    result = {}
    for plan in plans:
        original_refs = {}
        for target in plan["targets"]:
            key = path_key(target)
            needed = needed_by_target.get(key, set())
            if needed:
                original_refs[key] = _source_ref_map(
                    plan["source_candidates"].get(key, []),
                    needed,
                    projections,
                )
        usage = _cluster_usage(plan, projections, original_refs)
        result[plan["owner"]] = _connection_rows(plan, usage)
    if not _stats_still_match(identities):
        raise ContentIdentityError(
            "Cluster connection inventory changed before publication"
        )
    metrics.update({
        "owner_count": len(plans),
        "cluster_count": sum(len(plan["clusters"]) for plan in plans),
        "primary_file_count": len(requirements),
        "fallback_file_count": len(fallback_requirements),
        "inventory_file_count": len(identities),
        "content_identity_algorithm": "sha256-full-v1",
        "max_files": int(max_files),
    })
    return result


def cluster_connection_rows_by_owner(
    owner_clusters,
    *,
    cache_path=None,
    max_files=DEFAULT_MAX_CONNECTION_FILES,
    max_workers=DEFAULT_MAX_PROJECTION_WORKERS,
    metrics=None,
):
    """Return exact connected Cluster rows from one shared bounded inventory."""
    started = time.perf_counter()
    destination_metrics = metrics if metrics is not None else {}
    destination_metrics.update({
        "projection_cache_hits": 0,
        "projection_cache_misses": 0,
        "shared_analysis_hits": 0,
        "shared_analysis_misses": 0,
        "projection_parse_workers": 0,
        "content_identity_seconds": 0.0,
        "projection_seconds": 0.0,
    })
    normalized = {
        Path(owner).absolute(): list(clusters)
        for owner, clusters in owner_clusters.items()
    }
    if not normalized:
        destination_metrics.update({
            "owner_count": 0,
            "cluster_count": 0,
            "primary_file_count": 0,
            "fallback_file_count": 0,
            "inventory_file_count": 0,
            "cache_state": "empty",
            "wall_seconds": 0.0,
        })
        return {}
    cache_path = Path(cache_path or CLUSTER_CONNECTION_CACHE_PATH)
    last_error = None
    for _attempt in range(2):
        try:
            result = _scan_once(
                normalized,
                cache_path=cache_path,
                max_files=max_files,
                max_workers=max_workers,
                metrics=destination_metrics,
            )
            break
        except ContentIdentityError as exc:
            last_error = exc
    else:
        raise last_error
    misses = destination_metrics["projection_cache_misses"]
    destination_metrics["cache_state"] = (
        "warm" if misses == 0 else "cold_or_invalidated"
    )
    destination_metrics["wall_seconds"] = time.perf_counter() - started
    return result


__all__ = [
    "CLUSTER_CONNECTION_CACHE_PATH",
    "CLUSTER_CONNECTION_PROJECTION_SCHEMA",
    "DEFAULT_MAX_CONNECTION_FILES",
    "cluster_connection_rows_by_owner",
]
