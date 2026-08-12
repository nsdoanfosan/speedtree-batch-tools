"""Fail-closed PCG Cluster -> Blender/SK Assembly handoff contract.

The PCG audit proves the authored SPM/FBX dependency graph.  This module is the
downstream half of that contract: it inspects the *actual Blender import* of the
same FBX and accepts a branch/leaf role only when the role material is assigned
to one or more real polygons.  It deliberately has no ``bpy`` import so the
decision and receipt reconciliation can be unit-tested with small fakes.

This is not an Assembly switch.  Each role is classified from content as one
of complete-pair, absent (legacy pass-through), or partial (blocked).
"""
from __future__ import annotations

import functools
import hashlib
import json
import os
import re
from copy import deepcopy
from pathlib import Path


SCHEMA_VERSION = 1
CONTRACT_KIND = "pcg_cluster_blender_assembly_handoff"
CURRENT_ASSEMBLY_MANIFEST_KIND = "sk_batch_cluster_nanite_assembly_inputs"
ROLE_ORDER = ("branch", "cluster", "leaf", "leaf_side")
ROLE_PREFIX_RE = re.compile(
    r"^(leaf_side|leaf(?:_[^_]+)*_side|branch|cluster|leaf)(?:_|$)",
    re.IGNORECASE,
)
BLENDER_DUPLICATE_SUFFIX_RE = re.compile(r"\.\d{3}$")


def normalize_export_name(value):
    """Strip exporter wrappers while preserving the authored role identity."""
    name = str(value or "").split("\x00", 1)[0]
    name = name.strip().replace("\\", "/").rsplit("/", 1)[-1]
    if "::" in name:
        name = name.rsplit("::", 1)[-1]
    while BLENDER_DUPLICATE_SUFFIX_RE.search(name):
        name = BLENDER_DUPLICATE_SUFFIX_RE.sub("", name)
    if name.casefold().endswith("_mat"):
        name = name[:-4]
    if name.casefold().startswith("m_"):
        name = name[2:]
    if name.casefold().startswith("sk_"):
        name = name[3:]
    return name.strip().casefold()


def dependency_role(value):
    match = ROLE_PREFIX_RE.match(normalize_export_name(value))
    if not match:
        return None
    token = match.group(1).casefold()
    return "leaf_side" if token.startswith("leaf") and token != "leaf" else token


def _normalized_path(value):
    if not value:
        return ""
    return os.path.normcase(os.path.abspath(os.path.normpath(str(value))))


@functools.lru_cache(maxsize=512)
def _sha256_cached(path_text, size, mtime_ns):
    digest = hashlib.sha256()
    with Path(path_text).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def file_fingerprint(path, *, hash_content=True):
    candidate = Path(path)
    try:
        stat = candidate.stat()
    except OSError:
        return {
            "path": str(candidate),
            "exists": False,
            "size": None,
            "mtime_ns": None,
            "sha256": None,
        }
    absolute = _normalized_path(candidate)
    return {
        "path": str(candidate.resolve()),
        "exists": True,
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "sha256": (
            _sha256_cached(absolute, stat.st_size, stat.st_mtime_ns)
            if hash_content else None
        ),
    }


def _refresh_current_artifact_fingerprints(value):
    """Refresh persisted artifact rows without changing logical contracts."""
    if isinstance(value, list):
        return [_refresh_current_artifact_fingerprints(item) for item in value]
    if not isinstance(value, dict):
        return deepcopy(value)
    refreshed = {
        key: _refresh_current_artifact_fingerprints(item)
        for key, item in value.items()
    }
    path = str(value.get("path") or "").strip()
    if path and any(
        key in value for key in ("exists", "size", "mtime_ns", "sha256")
    ):
        current = file_fingerprint(path)
        if current.get("exists"):
            refreshed.update(current)
    return refreshed


def current_assembly_manifest_repair_handoff(spm_path, full_fbx_path):
    """Recover a live Repair handoff from the current production manifest.

    PCG receipts are discovery-time evidence and can become stale after a
    legitimate source Repair.  The manifest directly under the asset's
    ``assembly`` folder is the current production Assembly contract.  Reuse
    only that file, refresh its concrete artifact fingerprints, and never
    consult historical logs or backups.
    """
    spm = Path(spm_path).expanduser().resolve()
    manifest_path = (
        spm.parent
        / "assembly"
        / f"{spm.stem}_cluster_assembly_bindings.json"
    )
    if not manifest_path.is_file() or not Path(full_fbx_path).is_file():
        return None
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(
            "current production Assembly manifest is unreadable: "
            + str(manifest_path)
        ) from exc
    if (
        manifest.get("kind") != CURRENT_ASSEMBLY_MANIFEST_KIND
        or manifest.get("status") != "ready"
        or manifest.get("content_decision") != "build"
        or not list(manifest.get("parts") or [])
    ):
        return None
    evidence = manifest.get("handoff_evidence") or {}
    roles = evidence.get("roles") or {}
    if not isinstance(roles, dict) or not roles:
        raise ValueError(
            "current production Assembly manifest has no persisted role inputs"
        )
    part_builder_inputs = []
    for provider_key, saved_role in sorted(roles.items()):
        if not isinstance(saved_role, dict):
            raise ValueError(
                "current production Assembly manifest has an invalid role: "
                + str(provider_key)
            )
        role = _refresh_current_artifact_fingerprints(saved_role)
        polygons = list(role.get("polygon_indices") or [])
        normalized = role.get("normalized_variants") or {}
        if not polygons or not list(normalized.get("variants") or []):
            raise ValueError(
                "current production Assembly role is incomplete: "
                + str(provider_key)
            )
        role["provider_key"] = str(
            role.get("provider_key") or provider_key
        )
        role["rendered_provider_expansion_covered"] = True
        role["current_manifest_authority"] = {
            "status": "current",
            "source": "production_assembly_manifest",
            "manifest": file_fingerprint(manifest_path),
        }
        part_builder_inputs.append(role)
    return {
        "status": "ready",
        "issues": [],
        "spm": file_fingerprint(spm),
        "actual_fbx": file_fingerprint(full_fbx_path),
        "pcg_receipt": evidence.get("pcg_receipt"),
        "full_skeletal_mesh": {"preserved": True},
        "assembly": {
            "requested": True,
            "part_builder_inputs": part_builder_inputs,
        },
        "current_manifest_authority": {
            "status": "current",
            "source": "production_assembly_manifest",
            "manifest": file_fingerprint(manifest_path),
            "historical_receipts_consulted": False,
        },
    }


def _json_safe_matrix(matrix):
    if matrix is None:
        return None
    try:
        return [[float(value) for value in row] for row in matrix]
    except (TypeError, ValueError):
        return None


def _material_name(material):
    return str(getattr(material, "name", "") or "") if material else ""


def _object_source_fbx(obj):
    try:
        return str(obj.get("codex_source_fbx", "") or "")
    except (AttributeError, TypeError):
        return ""


def _component_rows(mesh, polygons):
    """Return disconnected polygon islands for one material assignment."""
    if not polygons:
        return []
    parent = list(range(len(polygons)))

    def find(index):
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left, right):
        left = find(left)
        right = find(right)
        if left != right:
            parent[right] = left

    vertex_owner = {}
    polygon_vertices = []
    for local_index, polygon in enumerate(polygons):
        vertices = tuple(int(value) for value in getattr(polygon, "vertices", ()))
        polygon_vertices.append(vertices)
        for vertex in vertices:
            previous = vertex_owner.setdefault(vertex, local_index)
            union(local_index, previous)

    groups = {}
    for local_index, polygon in enumerate(polygons):
        root = find(local_index)
        row = groups.setdefault(root, {"polygon_indices": [], "vertex_indices": set()})
        row["polygon_indices"].append(int(getattr(polygon, "index", local_index)))
        row["vertex_indices"].update(polygon_vertices[local_index])

    vertices = getattr(mesh, "vertices", ())
    result = []
    for row in groups.values():
        vertex_indices = sorted(row["vertex_indices"])
        bounds = None
        coordinates = []
        for vertex_index in vertex_indices:
            try:
                co = vertices[vertex_index].co
                coordinates.append(tuple(float(value) for value in co[:3]))
            except (IndexError, AttributeError, TypeError, ValueError):
                coordinates = []
                break
        if coordinates:
            bounds = {
                "min": [min(values) for values in zip(*coordinates)],
                "max": [max(values) for values in zip(*coordinates)],
            }
        result.append({
            "polygon_indices": sorted(row["polygon_indices"]),
            "vertex_indices": vertex_indices,
            "polygon_count": len(row["polygon_indices"]),
            "vertex_count": len(vertex_indices),
            "local_bounds": bounds,
        })
    result.sort(key=lambda row: row["polygon_indices"][0])
    return result


def build_blender_fbx_inventory(objects, source_fbx_path, role_identities):
    """Inspect real FBX-imported mesh polygons through bpy-compatible objects.

    ``objects`` should be the original objects in BWR's ``SpeedTree_Source``
    collection, not later merged copies.  Only objects tagged with the exact
    source FBX are admitted.
    """
    source_key = _normalized_path(source_fbx_path)
    expected = {}
    for role, identities in (role_identities or {}).items():
        if role not in ROLE_ORDER:
            continue
        values = (
            identities
            if isinstance(identities, (list, tuple, set))
            else [identities]
        )
        normalized = {
            normalize_export_name(identity)
            for identity in values
            if identity
        }
        if normalized:
            expected[role] = normalized
    rows = []
    declared_materials = []
    for obj in objects or []:
        if getattr(obj, "type", "") != "MESH" or not getattr(obj, "data", None):
            continue
        tagged = _normalized_path(_object_source_fbx(obj))
        if source_key and tagged != source_key:
            continue
        mesh = obj.data
        polygons = list(getattr(mesh, "polygons", ()) or ())
        materials = list(getattr(mesh, "materials", ()) or ())
        material_names = [_material_name(material) for material in materials]
        declared_materials.extend(name for name in material_names if name)
        polygons_by_slot = {}
        for polygon in polygons:
            polygons_by_slot.setdefault(
                int(getattr(polygon, "material_index", 0)), []
            ).append(polygon)
        usages = []
        for slot_index, material_name in enumerate(material_names):
            used = polygons_by_slot.get(slot_index, [])
            role = next(
                (
                    candidate
                    for candidate, identities in expected.items()
                    if normalize_export_name(material_name) in identities
                ),
                None,
            )
            usage = {
                "slot_index": slot_index,
                "material": material_name,
                "used_polygon_count": len(used),
            }
            if role and used:
                usage["role"] = role
                usage["polygon_indices"] = [
                    int(getattr(polygon, "index", index))
                    for index, polygon in enumerate(used)
                ]
                usage["components"] = _component_rows(mesh, used)
            usages.append(usage)
        rows.append({
            "object": str(getattr(obj, "name", "") or ""),
            "mesh": str(getattr(mesh, "name", "") or ""),
            "vertex_count": len(getattr(mesh, "vertices", ()) or ()),
            "polygon_count": len(polygons),
            "matrix_world": _json_safe_matrix(getattr(obj, "matrix_world", None)),
            "material_usages": usages,
        })
    return {
        "source_fbx": file_fingerprint(source_fbx_path),
        "objects": rows,
        "mesh_names": sorted({
            value
            for row in rows for value in (row["object"], row["mesh"])
            if value
        }),
        "materials": sorted(set(declared_materials)),
    }


def classify_inventory_role(
    inventory,
    role,
    role_identity,
    role_identity_aliases=(),
):
    """Apply the independent complete/absent/partial content gate."""
    identities = [role_identity, *(role_identity_aliases or ())]
    expected = {
        normalize_export_name(identity)
        for identity in identities
        if identity
    }
    material_matches = [
        value for value in inventory.get("materials") or []
        if normalize_export_name(value) in expected
    ]
    mesh_name_matches = [
        value for value in inventory.get("mesh_names") or []
        if normalize_export_name(value) in expected
    ]
    assignments = []
    for mesh_row in inventory.get("objects") or []:
        for usage in mesh_row.get("material_usages") or []:
            if normalize_export_name(usage.get("material")) not in expected:
                continue
            if int(usage.get("used_polygon_count") or 0) <= 0:
                continue
            assignments.append({
                "object": mesh_row.get("object"),
                "mesh": mesh_row.get("mesh"),
                "matrix_world": mesh_row.get("matrix_world"),
                **usage,
            })
    if assignments:
        status, decision = "complete_pair", "normalize_part"
    elif material_matches:
        status, decision = "material_without_mesh", "blocked"
    elif mesh_name_matches:
        status, decision = "mesh_without_material", "blocked"
    else:
        status, decision = "absent", "pass_through"
    return {
        "role": role,
        "role_identity": role_identity,
        "role_identity_aliases": [
            identity
            for identity in identities
            if identity and identity != role_identity
        ],
        "status": status,
        "decision": decision,
        "material_matches": material_matches,
        "mesh_name_matches": mesh_name_matches,
        "assignments": assignments,
    }


def _contract_candidates(payload):
    if not isinstance(payload, dict):
        return []
    candidates = []
    if isinstance(payload.get("handoff"), dict) and isinstance(
        payload.get("dependencies"), list
    ):
        candidates.append(payload)
    for key in ("cluster_assembly", "assembly_handoff_contract"):
        value = payload.get(key)
        if isinstance(value, dict):
            candidates.extend(_contract_candidates(value))
    for key in ("items", "targets", "results"):
        values = payload.get(key)
        if isinstance(values, dict):
            values = values.values()
        if isinstance(values, (list, tuple)) or type(values) is type({}.values()):
            for value in values:
                if isinstance(value, dict):
                    candidates.extend(_contract_candidates(value))
    return candidates


def _contract_spm_paths(contract):
    paths = set()
    for row in contract.get("tree_source_identities") or []:
        for key in ("target_spm", "authoritative_tree_source"):
            fingerprint = row.get(key) or {}
            if fingerprint.get("path"):
                paths.add(_normalized_path(fingerprint["path"]))
    for role in (contract.get("handoff") or {}).get("roles") or []:
        for target in role.get("targets") or []:
            if target.get("spm"):
                paths.add(_normalized_path(target["spm"]))
    return paths


def select_cluster_contract(payload, spm_path):
    candidates = _contract_candidates(payload)
    if not candidates:
        raise ValueError("PCG receipt contains no cluster_assembly contract")
    spm_key = _normalized_path(spm_path)
    matching = [
        candidate for candidate in candidates
        if spm_key in _contract_spm_paths(candidate)
    ]
    if len(matching) == 1:
        return matching[0]
    if not matching and len(candidates) == 1:
        return candidates[0]
    if not matching:
        raise ValueError("PCG receipt does not identify the requested SPM")
    raise ValueError("PCG receipt has multiple Cluster contracts for the requested SPM")


def load_cluster_contract(receipt_path, spm_path):
    receipt = Path(receipt_path)
    payload = json.loads(receipt.read_text(encoding="utf-8"))
    return payload, select_cluster_contract(payload, spm_path)


def resolve_cluster_receipt_path(
    spm_path,
    embedded_contract_path=None,
    *,
    include_resolution=False,
):
    """Resolve the additive PCG receipt without adding a new batch argument.

    PCG persists the Cluster contract independently from the existing SK
    material preflight report.  A current, hash-validated persisted receipt is
    usable cache evidence.  Only a run-specific embedded contract explicitly
    marked ``live_audit_complete`` can supersede that cache; an older material
    report is never a fallback authority.
    """
    from pcg_st9_texture_batch.pcg_cluster_assembly_contract import (
        ClusterAssemblyReceiptStaleError,
        cluster_assembly_receipt_resolution,
        locate_cluster_assembly_receipt,
    )

    embedded_resolved = None
    embedded_live_audit = False
    if embedded_contract_path:
        embedded = Path(embedded_contract_path)
        if embedded.is_file():
            try:
                embedded_payload = json.loads(
                    embedded.read_text(encoding="utf-8")
                )
                select_cluster_contract(embedded_payload, spm_path)
            except (OSError, json.JSONDecodeError, ValueError):
                pass
            else:
                embedded_resolved = embedded.resolve()
                persistence = (
                    embedded_payload.get(
                        "cluster_assembly_receipt_persistence"
                    )
                    or {}
                )
                embedded_live_audit = (
                    persistence.get("live_audit_complete") is True
                )

    # The caller writes this marker only after the run-specific PCG audit has
    # completed and the exact selected contract has been embedded.  A persisted
    # receipt is cache evidence and must not override that newer semantic
    # decision merely because its historical artifact hashes still match.
    if embedded_live_audit:
        if include_resolution:
            return embedded_resolved, {
                "policy": "embedded_live_audit_authoritative",
                "requested_spm": str(spm_path),
                "selected_receipt": str(embedded_resolved),
                "current_candidates": [{"path": str(embedded_resolved)}],
                "superseded_current_receipts": [],
                "ignored_stale_candidates": [],
            }
        return embedded_resolved

    cache_error = None
    try:
        if include_resolution:
            resolution = cluster_assembly_receipt_resolution(spm_path)
            return Path(resolution["selected_receipt"]).resolve(), resolution
        return Path(locate_cluster_assembly_receipt(spm_path)).resolve()
    except FileNotFoundError as exc:
        cache_error = str(exc)
    except ClusterAssemblyReceiptStaleError as exc:
        cache_error = str(exc)

    if include_resolution:
        return None, {
            "policy": "no_cluster_assembly_receipt",
            "requested_spm": str(spm_path),
            "selected_receipt": None,
            "current_candidates": [],
            "superseded_current_receipts": [],
            "ignored_stale_candidates": (
                [{"path": "", "error": cache_error}]
                if cache_error
                else []
            ),
        }
    return None


def _role_receipt_entries(contract):
    rows = []
    for row in (contract.get("handoff") or {}).get("roles") or []:
        role = str(row.get("role") or dependency_role(row.get("name")) or "").casefold()
        if role in ROLE_ORDER:
            rows.append((role, row))
    return rows


def _role_receipt_rows(contract):
    """Return the first provider for legacy one-row-per-role consumers."""
    rows = {}
    for role, row in _role_receipt_entries(contract):
        rows.setdefault(role, row)
    return rows


def _provider_key(role, receipt_row):
    identity = str(
        (receipt_row or {}).get("name")
        or (receipt_row or {}).get("output_spm")
        or (receipt_row or {}).get("spm")
        or role
    )
    return f"{role}:{normalize_export_name(Path(identity).stem)}"


def _contract_species(contract):
    name = Path(str(contract.get("folder") or "")).name
    name = re.sub(
        r"^(?:tree|bush|shrub|weed|grass)_",
        "",
        name,
        flags=re.IGNORECASE,
    )
    return name.strip("_-").casefold()


def _role_identity(role, receipt_row, contract=None):
    normalized = (receipt_row or {}).get("normalized_variants") or {}
    material_identity = str(normalized.get("material") or "").strip()
    if material_identity:
        return material_identity
    identity = str((receipt_row or {}).get("name") or "")
    if dependency_role(identity) == role:
        return identity
    species = _contract_species(contract or {}) or "asset"
    if role == "leaf_side":
        return f"leaf_{species}_side_01"
    return f"{role}_{species}_01"


def _normalized_delivery_for_target(value, spm_path=None, contract=None):
    if not isinstance(value, dict):
        return None
    rows = [
        row for row in value.get("target_deliveries") or []
        if isinstance(row, dict)
    ]
    if rows and spm_path is not None:
        target_paths = {_normalized_path(spm_path)}
        if contract is not None:
            target_paths.add(
                _normalized_path(
                    _authoritative_spm_for_requested(contract, spm_path)
                )
            )
        matches = [
            row for row in rows
            if _normalized_path(row.get("spm")) in target_paths
        ]
        return matches[0] if len(matches) == 1 else None
    if rows and len(rows) == 1:
        return rows[0]
    return {
        "delivery_mode": value.get("delivery_mode"),
        "generator_bindings": list(
            value.get("generator_bindings") or []
        ),
        "binding_mismatches": list(
            value.get("binding_mismatches") or []
        ),
    }


def _normalized_variants_ready(
    value,
    spm_path=None,
    contract=None,
    *,
    allow_actual_pair=False,
):
    if not isinstance(value, dict) or value.get("status") != "ready":
        return False
    delivery = _normalized_delivery_for_target(
        value,
        spm_path=spm_path,
        contract=contract,
    )
    if not isinstance(delivery, dict):
        return False
    delivery_mode = str(delivery.get("delivery_mode") or "")
    if allow_actual_pair:
        if delivery_mode not in {
            "render_connected",
            "connection_incomplete",
            "asset_registration_only",
        }:
            return False
    elif (
        delivery_mode != "render_connected"
        or not list(delivery.get("generator_bindings") or [])
        or list(delivery.get("missing_live_bindings") or [])
        or list(delivery.get("binding_mismatches") or [])
    ):
        return False
    variants = list(value.get("variants") or [])
    if not variants:
        return False
    try:
        ordinals = [int(row.get("ordinal") or 0) for row in variants]
    except (AttributeError, TypeError, ValueError):
        return False
    if ordinals != list(range(1, len(variants) + 1)):
        return False
    for variant in variants:
        mode = str(variant.get("source_partition_mode") or "")
        composite_parts = variant.get("composite_parts") or []
        if mode == "COMPOSITE_PER_DEFORM_ROOT":
            if not isinstance(composite_parts, list) or not composite_parts:
                return False
            try:
                subparts = [
                    int(row.get("subpart_index") or 0)
                    for row in composite_parts
                ]
            except (AttributeError, TypeError, ValueError):
                return False
            if subparts != list(range(1, len(composite_parts) + 1)):
                return False
            if any(
                not str(row.get("skeletal_asset_name") or "")
                .casefold().startswith("sk_")
                for row in composite_parts
            ):
                return False
            if any(
                not str(row.get("source_bone") or "").strip()
                for row in composite_parts
            ):
                return False
        elif composite_parts:
            return False
        elif mode and not str(variant.get("source_bone") or "").strip():
            return False
    return True


def _actual_assignment_matches_provider_identity(actual, provider_identity):
    """Require a live provider name before overriding incomplete delivery.

    A target material can reuse a provider's textures without containing that
    provider's generated card geometry.  ``asset_registration_only`` and
    ``connection_incomplete`` therefore cannot be upgraded from an arbitrary
    target alias alone.  The current Blender import must retain the provider
    name on its material, object, or mesh.
    """
    expected = normalize_export_name(provider_identity)
    if not expected:
        return False
    for assignment in (actual or {}).get("assignments") or []:
        if any(
            normalize_export_name(assignment.get(field)) == expected
            for field in ("material", "object", "mesh")
        ):
            return True
    return False


def role_identities_from_contract(contract):
    """Return the exact role identities authored by the selected PCG receipt."""
    rows = _role_receipt_rows(contract)
    return {
        role: _role_identity(role, rows.get(role), contract)
        for role in ROLE_ORDER
    }


def _role_identity_aliases(role, receipt_row, contract, spm_path):
    """Return receipt-authored names that identify the same rendered role.

    Legacy provider names can differ from the authoritative general-tree
    material name.  The target SPM material/mesh pair is already content
    audited by PCG, so its complete records are valid aliases; no species or
    ordinal naming guess is needed.
    """
    if receipt_row is None:
        # A general-tree FBX can legitimately contain ordinary materials such
        # as ``M_leaf_*`` without declaring an Assembly leaf role.  Inferring a
        # role from that generic token turns pass-through content into a false
        # dependency.  Only a receipt-authored row can provide role identity.
        return []
    receipt_identity = _role_identity(role, receipt_row, contract)
    identities = []
    authoritative_spm = _authoritative_spm_for_requested(contract, spm_path)
    target = _target_for_spm(receipt_row, authoritative_spm)
    pair = (target or {}).get("spm_material_mesh_pair") or {}
    for record in pair.get("records") or []:
        if record.get("complete") is not True:
            continue
        material_name = str(record.get("material_name") or "").strip()
        if material_name:
            identities.append(material_name)
    # The rendered target SPM is authoritative for the material identity used
    # to cut the base mesh.  Keep a provider/receipt spelling only as an alias;
    # otherwise a legacy provider identity leaks into every newly generated
    # assembly binding even when the actual target material has the current
    # authoritative name.
    identities.append(receipt_identity)
    unique = {}
    for identity in identities:
        normalized = normalize_export_name(identity)
        if normalized and normalized not in unique:
            unique[normalized] = identity
    return list(unique.values())


def role_identity_aliases_from_contract(contract, spm_path):
    result = {role: [] for role in ROLE_ORDER}
    for role, row in _role_receipt_entries(contract):
        for identity in _role_identity_aliases(
            role, row, contract, spm_path
        ):
            normalized = normalize_export_name(identity)
            if normalized and not any(
                normalize_export_name(value) == normalized
                for value in result[role]
            ):
                result[role].append(identity)
    return result


def _target_for_spm(receipt_row, spm_path):
    key = _normalized_path(spm_path)
    targets = list((receipt_row or {}).get("targets") or [])
    matches = [row for row in targets if _normalized_path(row.get("spm")) == key]
    if len(matches) == 1:
        return matches[0]
    if not matches and len(targets) == 1:
        return targets[0]
    return None


def _authoritative_spm_for_requested(contract, spm_path):
    """Resolve an SK batch item to the recorded general-tree source."""
    key = _normalized_path(spm_path)
    matches = []
    for row in contract.get("tree_source_identities") or []:
        target = row.get("target_spm") or {}
        source = row.get("authoritative_tree_source") or {}
        if key not in {
            _normalized_path(target.get("path")),
            _normalized_path(source.get("path")),
        }:
            continue
        source_path = source.get("path")
        if source_path:
            matches.append(str(source_path))
    normalized = {
        _normalized_path(value): value
        for value in matches
    }
    if len(normalized) == 1:
        return Path(next(iter(normalized.values())))
    if len(normalized) > 1:
        raise ValueError(
            "PCG receipt maps the requested SPM to multiple authoritative sources"
        )
    return Path(spm_path)


def assembly_source_fbx_resolution(contract, full_spm_path):
    """Resolve the Assembly FBX or an explicit legacy pass-through decision.

    The Full SK SPM identifies the batch item, but its derived ``SK_*.fbx`` is
    not the role-pair evidence.  PCG records the general-tree source target in
    every role row; branch and leaf must agree on that exact FBX.

    A persisted ``pending_export`` target whose receipt and disk both say the
    source FBX does not exist is incomplete optional Assembly evidence, not a
    broken Full SK input.  Preserve the Full SK repair and report that legacy
    pass-through instead of asking Blender to import a known-missing file.
    """
    paths = {}
    role_rows = _role_receipt_entries(contract)
    if not role_rows:
        return {
            "status": "not_applicable",
            "reason": "no_cluster_assembly_roles",
            "source_spm": None,
            "source_fbx": None,
            "roles": [],
        }
    authoritative_spm = _authoritative_spm_for_requested(
        contract, full_spm_path
    )
    requested_spm = Path(full_spm_path)
    if any(
        _normalized_path(target.get("spm"))
        == _normalized_path(requested_spm)
        for _role, receipt_row in role_rows
        for target in (receipt_row.get("targets") or [])
    ):
        authoritative_spm = requested_spm
    targets = []
    for role, receipt_row in role_rows:
        target = _target_for_spm(receipt_row, authoritative_spm)
        expected = ((target or {}).get("export_bundle") or {}).get("fbx") or {}
        path = expected.get("path")
        target_gate = (target or {}).get("fbx_material_mesh_pair") or {}
        decision = str(
            target_gate.get("decision")
            or (target or {}).get("decision")
            or receipt_row.get("decision")
            or ""
        )
        actual_exists = bool(path and Path(path).is_file())
        targets.append({
            "role": role,
            "decision": decision,
            "expected_fbx": expected,
            "actual_exists": actual_exists,
        })
        if path:
            paths[_normalized_path(path)] = str(path)

    pending_missing = [
        row for row in targets
        if row["decision"] == "pending_export"
        and row["expected_fbx"].get("exists") is False
        and not row["actual_exists"]
    ]
    if targets and len(pending_missing) == len(targets):
        pending_paths = sorted({
            str(row["expected_fbx"].get("path") or "")
            for row in pending_missing
            if row["expected_fbx"].get("path")
        })
        return {
            "status": "legacy_pass_through",
            "reason": "assembly_source_fbx_pending_export",
            "source_spm": str(authoritative_spm),
            "source_fbx": pending_paths[0] if len(pending_paths) == 1 else None,
            "roles": targets,
        }
    if not paths:
        raise ValueError("PCG receipt contains no Assembly source FBX")
    if len(paths) != 1:
        raise ValueError("PCG branch/leaf receipts disagree on Assembly source FBX")
    source_fbx = Path(next(iter(paths.values())))
    return {
        "status": "ready",
        "reason": "hash_validated_cluster_assembly_source",
        "source_spm": str(authoritative_spm),
        "source_fbx": str(source_fbx),
        "roles": targets,
    }


def assembly_source_fbx_from_contract(contract, full_spm_path):
    """Return the ready Assembly FBX, or None for a recorded pass-through."""
    resolution = assembly_source_fbx_resolution(contract, full_spm_path)
    source_fbx = resolution.get("source_fbx")
    if resolution.get("status") != "ready" or not source_fbx:
        return None
    return Path(source_fbx)


def _compare_artifact(expected, actual, *, allow_pending=False):
    if not expected:
        return {"status": "missing_receipt_fingerprint", "ok": False}
    expected_path = _normalized_path(expected.get("path"))
    actual_path = _normalized_path(actual.get("path"))
    if expected_path != actual_path:
        return {
            "status": "path_mismatch",
            "ok": False,
            "expected": expected,
            "actual": actual,
        }
    if not expected.get("exists") and not actual.get("exists"):
        return {
            "status": "consistent_missing",
            "ok": True,
            "expected": expected,
            "actual": actual,
        }
    if not expected.get("exists") and allow_pending and actual.get("exists"):
        return {
            "status": "pending_export_resolved",
            "ok": True,
            "expected": expected,
            "actual": actual,
        }
    expected_hash = expected.get("sha256")
    if expected_hash is not None:
        if expected_hash != actual.get("sha256"):
            return {
                "status": "sha256_mismatch",
                "ok": False,
                "expected": expected,
                "actual": actual,
            }
        metadata_drift = [
            field
            for field in ("size", "mtime_ns")
            if (
                expected.get(field) is not None
                and expected.get(field) != actual.get(field)
            )
        ]
        result = {
            "status": (
                "content_exact_metadata_drift"
                if metadata_drift
                else "exact"
            ),
            "ok": bool(expected.get("exists") and actual.get("exists")),
            "expected": expected,
            "actual": actual,
        }
        if metadata_drift:
            result["metadata_drift"] = metadata_drift
        return result
    for field in ("size", "mtime_ns"):
        value = expected.get(field)
        if value is not None and value != actual.get(field):
            return {
                "status": f"{field}_mismatch",
                "ok": False,
                "expected": expected,
                "actual": actual,
            }
    return {
        "status": "metadata_exact",
        "ok": bool(expected.get("exists") and actual.get("exists")),
        "expected": expected,
        "actual": actual,
    }


def _export_artifact_validation(contract, role_rows, spm_path, inventory):
    validations = []
    seen = set()
    authoritative_spm = _authoritative_spm_for_requested(contract, spm_path)
    for role, receipt_row in role_rows:
        target = _target_for_spm(receipt_row, authoritative_spm)
        if not target:
            continue
        bundle = target.get("export_bundle") or {}
        for artifact in ("fbx", "xml", "stmat"):
            expected = bundle.get(artifact)
            if not expected or not expected.get("path"):
                continue
            key = (artifact, _normalized_path(expected["path"]))
            if key in seen:
                continue
            seen.add(key)
            actual = (
                inventory.get("source_fbx")
                if artifact == "fbx"
                else file_fingerprint(expected["path"])
            )
            validations.append({
                "artifact": artifact,
                **_compare_artifact(expected, actual, allow_pending=True),
            })
    return validations


def _dependency_artifact_validation(contract, spm_path):
    """Validate the source SPM, Cluster SPMs, and texture receipt identities."""
    expected_rows = []
    spm_key = _normalized_path(spm_path)
    for row in contract.get("tree_source_identities") or []:
        target = row.get("target_spm") or {}
        source = row.get("authoritative_tree_source") or {}
        if spm_key in {
            _normalized_path(target.get("path")),
            _normalized_path(source.get("path")),
        }:
            expected_rows.append(("tree_spm", target))
            expected_rows.append(("authoritative_tree_spm", source))
    handoff = contract.get("handoff") or {}
    dependencies = (
        handoff.get("cluster_dependencies")
        or contract.get("dependencies")
        or []
    )
    for dependency in dependencies:
        pair_rows = [
            ("cluster_authoring_spm", dependency.get("authoring_spm_fingerprint")),
            ("cluster_output_spm", dependency.get("output_spm_fingerprint")),
        ]
        if any(expected for _artifact, expected in pair_rows):
            expected_rows.extend(
                (artifact, expected)
                for artifact, expected in pair_rows
                if isinstance(expected, dict) and expected.get("exists")
            )
        else:
            expected_rows.append(
                ("cluster_spm", dependency.get("spm_fingerprint") or {})
            )
        for texture in dependency.get("texture_dependencies") or []:
            expected_rows.append(("cluster_texture", texture))

    validations = []
    seen = set()
    for artifact, expected in expected_rows:
        path = expected.get("path")
        if not path:
            validations.append({
                "artifact": artifact,
                "status": "missing_receipt_fingerprint",
                "ok": False,
            })
            continue
        key = (artifact, _normalized_path(path))
        if key in seen:
            continue
        seen.add(key)
        actual = file_fingerprint(
            path,
            hash_content=expected.get("sha256") is not None,
        )
        validations.append({
            "artifact": artifact,
            **_compare_artifact(expected, actual),
        })
    if not any(row["artifact"] == "tree_spm" for row in validations):
        validations.append({
            "artifact": "tree_spm",
            "status": "requested_spm_missing_from_receipt",
            "ok": False,
        })
    return validations


def _reconcile_role(receipt_row, actual):
    receipt_decision = str((receipt_row or {}).get("decision") or "")
    actual_decision = actual["decision"]
    if actual_decision == "blocked":
        return "blocked", "actual_fbx_partial_pair"
    if receipt_row is None:
        if actual_decision == "pass_through":
            return "pass_through", "receipt_and_fbx_absent"
        return actual_decision, "current_fbx_authoritative_receipt_absent"
    if receipt_decision == "blocked":
        return actual_decision, "current_fbx_overrides_stale_receipt_block"
    if receipt_decision in {"pending_export", ""}:
        return actual_decision, "pending_receipt_resolved_by_actual_fbx"
    if receipt_decision != actual_decision:
        return actual_decision, "current_fbx_overrides_receipt_decision"
    return actual_decision, "pcg_receipt_and_actual_fbx_agree"


def _artifact_row(value):
    value = value if isinstance(value, dict) else {}
    row = {
        **value,
        "path": value.get("path") or value.get("canonical_path"),
    }
    if "exists" not in row and row.get("path"):
        row["exists"] = True
    return row


def _artifact_is_concretely_missing(validation):
    expected = (validation or {}).get("expected") or {}
    actual = (validation or {}).get("actual") or {}
    return bool(expected.get("exists") and not actual.get("exists"))


def _concrete_pcg_handoff_issue(issue):
    """Keep only current file/pair failures from an upstream handoff."""
    code = str((issue or {}).get("code") or "")
    if code == "FBX_ROLE_MATERIAL_MESH_PARTIAL":
        return True
    if code not in {
        "CLUSTER_TEXTURE_REFERENCE_MISSING",
        "CLUSTER_TGA_BASENAME_INVALID",
    }:
        return False
    details = (issue or {}).get("details") or {}
    return bool(details.get("missing")) and all(
        row.get("status") == "path_alias_missing"
        for row in details.get("unresolved_aliases") or []
    )


def _validated_isolated_bark_capture(provider_spm, canonical_material):
    provider = Path(provider_spm)
    report_path = (
        provider.parent
        / "reports"
        / f"{provider.stem}_speedtree_repair_pipeline_report_codex.json"
    )
    result = {
        "provider_spm": str(provider),
        "canonical_material": str(canonical_material or ""),
        "pipeline_report": file_fingerprint(report_path),
        "status": "missing_pipeline_report",
        "validations": [],
    }
    try:
        pipeline = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return result
    resolution = pipeline.get("cluster_bark_source_resolution") or {}
    if resolution.get("status") != "ready":
        result["status"] = "bark_resolution_not_ready"
        return result
    if resolution.get("production_source_mutated") is not False:
        result["status"] = "production_source_mutation_not_disproven"
        return result
    if (
        normalize_export_name(resolution.get("canonical_material"))
        != normalize_export_name(canonical_material)
    ):
        result["status"] = "canonical_material_mismatch"
        return result

    source_expected = _artifact_row(resolution.get("source_spm"))
    isolated_expected = _artifact_row(resolution.get("speedtree_spm"))
    manifest_expected = _artifact_row(resolution.get("manifest"))
    validations = [
        {
            "artifact": "provider_source_spm",
            **_compare_artifact(
                source_expected,
                file_fingerprint(provider),
            ),
        },
        {
            "artifact": "isolated_bark_spm",
            **_compare_artifact(
                isolated_expected,
                file_fingerprint(isolated_expected.get("path") or ""),
            ),
        },
        {
            "artifact": "isolated_bark_manifest",
            **_compare_artifact(
                manifest_expected,
                file_fingerprint(manifest_expected.get("path") or ""),
            ),
        },
    ]
    handoff = pipeline.get("speedtree_material_handoff_contract") or {}
    handoff_source = _artifact_row(
        (handoff.get("source") or {}).get("spm")
    )
    validations.append({
        "artifact": "material_handoff_source_spm",
        **_compare_artifact(
            handoff_source,
            file_fingerprint(handoff_source.get("path") or ""),
        ),
    })
    if handoff.get("outcome") != "ok":
        result["status"] = "material_handoff_not_ready"
    elif not all(row.get("ok") for row in validations):
        result["status"] = "bark_capture_artifact_mismatch"
    elif (
        _normalized_path(handoff_source.get("path"))
        != _normalized_path(isolated_expected.get("path"))
    ):
        result["status"] = "material_handoff_not_bound_to_isolated_spm"
    else:
        result["status"] = "ready"
    result["validations"] = validations
    return result


def build_assembly_handoff(receipt_path, spm_path, inventory):
    """Reconcile one PCG receipt with the exact imported FBX inventory."""
    _payload, contract = load_cluster_contract(receipt_path, spm_path)
    role_rows = _role_receipt_entries(contract)
    roles = []
    issues = []
    receipt_roles = {role for role, _row in role_rows}
    work_rows = list(role_rows) + [
        (role, None) for role in ROLE_ORDER if role not in receipt_roles
    ]
    work_rows.sort(key=lambda item: (
        ROLE_ORDER.index(item[0]),
        _provider_key(item[0], item[1]),
    ))
    for role, receipt_row in work_rows:
        identities = _role_identity_aliases(
            role,
            receipt_row,
            contract,
            spm_path,
        )
        identity = identities[0] if identities else ""
        actual = classify_inventory_role(
            inventory,
            role,
            identity,
            identities[1:],
        )
        decision, evidence = _reconcile_role(receipt_row, actual)
        normalized_variants = (
            deepcopy(receipt_row.get("normalized_variants"))
            if receipt_row and receipt_row.get("normalized_variants")
            else None
        )
        delivery = _normalized_delivery_for_target(
            normalized_variants,
            spm_path=spm_path,
            contract=contract,
        )
        delivery_mode = str(
            (delivery or {}).get("delivery_mode") or ""
        )
        provider_identity = _role_identity(role, receipt_row, contract)
        provider_identity_matches_actual = (
            _actual_assignment_matches_provider_identity(
                actual,
                provider_identity,
            )
        )
        normalized_ready = _normalized_variants_ready(
            normalized_variants,
            spm_path=spm_path,
            contract=contract,
            allow_actual_pair=(
                actual.get("status") == "complete_pair"
                and provider_identity_matches_actual
            ),
        )
        row = {
            **actual,
            "provider_key": _provider_key(role, receipt_row),
            "decision": decision,
            "receipt_decision": (
                str(receipt_row.get("decision") or "") if receipt_row else "absent"
            ),
            "reconciliation": evidence,
            "normalized_variants": normalized_variants,
            "normalized_delivery_mode": delivery_mode or None,
            "provider_identity": provider_identity or None,
            "provider_identity_matches_actual": (
                provider_identity_matches_actual
            ),
            "rendered_provider_expansion_covered": bool(
                (receipt_row or {}).get(
                    "rendered_provider_expansion_covered"
                )
            ),
        }
        if decision == "normalize_part" and normalized_ready:
            if delivery_mode != "render_connected":
                row["reconciliation"] = (
                    "current_spm_pair_and_normalized_topology_override_"
                    + delivery_mode
                    if (receipt_row or {}).get(
                        "rendered_provider_expansion_covered"
                    )
                    else "current_fbx_pair_overrides_" + delivery_mode
                )
                evidence = row["reconciliation"]
        elif delivery_mode == "asset_registration_only":
            row["decision"] = "pass_through"
            evidence = "asset_registration_only"
            if (
                actual.get("status") == "complete_pair"
                and not provider_identity_matches_actual
            ):
                evidence = "asset_registration_only_provider_name_mismatch"
            row["reconciliation"] = evidence
            decision = "pass_through"
        elif delivery_mode == "connection_incomplete" and decision != "blocked":
            row["decision"] = "pass_through"
            evidence = "generator_connection_metadata_incomplete_nonblocking"
            if (
                actual.get("status") == "complete_pair"
                and not provider_identity_matches_actual
            ):
                evidence = "connection_incomplete_provider_name_mismatch"
            row["reconciliation"] = evidence
            decision = "pass_through"
        elif decision == "normalize_part" and not normalized_ready:
            row["decision"] = "pass_through"
            row["reconciliation"] = (
                "normalized_variants_metadata_missing_nonblocking"
            )
            decision = "pass_through"
            evidence = "normalized_variants_metadata_missing_nonblocking"
        roles.append(row)
        if decision == "blocked":
            issues.append({
                "code": "CLUSTER_ROLE_HANDOFF_BLOCKED",
                "role": role,
                "reason": evidence,
                "actual_status": actual["status"],
                "receipt_decision": row["receipt_decision"],
            })

    artifacts = (
        _dependency_artifact_validation(contract, spm_path)
        + _export_artifact_validation(
            contract, role_rows, spm_path, inventory
        )
    )
    for artifact in artifacts:
        if _artifact_is_concretely_missing(artifact):
            issues.append({
                "code": "CLUSTER_EXPORT_ARTIFACT_MISMATCH",
                "artifact": artifact.get("artifact"),
                "reason": artifact.get("status"),
            })

    pcg_handoff = contract.get("handoff") or {}
    pcg_handoff_status = str(pcg_handoff.get("status") or "")
    bark = pcg_handoff.get("canonical_bark") or {}
    bark_mutation_requested = bool(bark.get("mutation_requested"))
    canonical_bark_captures = []
    if pcg_handoff_status in {"blocked", "needs_bark_normalization"}:
        detailed = []
        if (
            pcg_handoff_status == "needs_bark_normalization"
            and bark_mutation_requested
        ):
            dependency_roles = {}
            for dependency in (
                pcg_handoff.get("cluster_dependencies")
                or contract.get("dependencies")
                or []
            ):
                role = str(dependency.get("role") or "")
                for key in (
                    "spm",
                    "source_spm",
                    "authoring_spm",
                    "output_spm",
                ):
                    value = dependency.get(key)
                    if value:
                        dependency_roles[
                            os.path.normcase(os.path.abspath(str(value)))
                        ] = role
            for source in bark.get("cluster_bark_sources") or []:
                if source.get("replacement") != "required":
                    continue
                provider = str(source.get("cluster_spm") or "")
                capture = _validated_isolated_bark_capture(
                    provider,
                    bark.get("canonical_material"),
                )
                canonical_bark_captures.append(capture)
                if capture.get("status") == "ready":
                    continue
                detailed.append({
                    "code": "CANONICAL_BARK_NORMALIZATION_REQUIRED",
                    "role": dependency_roles.get(
                        os.path.normcase(os.path.abspath(provider)),
                        "",
                    ),
                    "spm": provider,
                    "material": source.get("material_name"),
                    "canonical_material": bark.get(
                        "canonical_material"
                    ),
                    "reason": capture.get("status"),
                    "texture_refs": list(
                        source.get("texture_refs") or []
                    ),
                })
        else:
            detailed = [
                deepcopy(row)
                for row in (
                    pcg_handoff.get("errors")
                    or pcg_handoff.get("issues")
                    or []
                )
                if isinstance(row, dict)
                and _concrete_pcg_handoff_issue(row)
            ]
        if pcg_handoff_status == "blocked" and detailed:
            issues.extend(detailed)

    normalize_roles = [row for row in roles if row["decision"] == "normalize_part"]
    if issues:
        status = "blocked"
    elif normalize_roles:
        status = "ready"
    else:
        status = "pass_through"
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": CONTRACT_KIND,
        "status": status,
        "pcg_receipt": file_fingerprint(receipt_path),
        "spm": file_fingerprint(spm_path),
        "actual_fbx": inventory.get("source_fbx") or {},
        "artifact_validation": artifacts,
        "pcg_handoff_status": pcg_handoff_status,
        "canonical_bark_delivery": {
            "status": bark.get("status"),
            "mutation_requested": bark_mutation_requested,
            "normalization_gate_applied": bool(
                bark_mutation_requested
                and pcg_handoff_status in {
                    "blocked", "needs_bark_normalization"
                }
            ),
        },
        "canonical_bark_captures": canonical_bark_captures,
        "roles": roles,
        "full_skeletal_mesh": {
            "preserved": True,
            "source_spm": str(Path(spm_path).resolve()),
            "output_contract": "existing_sk_batch_full_mesh_unchanged",
        },
        "assembly": {
            "requested": bool(normalize_roles) and not issues,
            "mode": "separate_skeletal_nanite_assembly",
            "role_pair_source_fbx": (
                inventory.get("source_fbx") or {}
            ).get("path"),
            "base_geometry_contract": "full_mesh_minus_role_polygon_components",
            "part_builder_inputs": [
                {
                    "role": row["role"],
                    "provider_key": row["provider_key"],
                    "role_identity": row["role_identity"],
                    "role_identity_aliases": deepcopy(
                        row.get("role_identity_aliases") or []
                    ),
                    "assignments": row["assignments"],
                    "rendered_provider_expansion_covered": row.get(
                        "rendered_provider_expansion_covered", False
                    ),
                    "normalized_variants": row.get("normalized_variants"),
                }
                for row in normalize_roles
            ],
        },
        "skeleton_wind_contract": {
            "mode": "regenerate_from_final_skeleton",
            "shared_by": ["full_skeletal_mesh", "nanite_assembly"],
            "production_310_bone_hard_gate": False,
            "validate_wind_bone_names_and_indices": True,
            "validate_part_binding_hierarchy": True,
        },
        "issues": issues,
    }


__all__ = [
    "CONTRACT_KIND",
    "ROLE_ORDER",
    "assembly_source_fbx_from_contract",
    "build_assembly_handoff",
    "build_blender_fbx_inventory",
    "classify_inventory_role",
    "dependency_role",
    "file_fingerprint",
    "load_cluster_contract",
    "normalize_export_name",
    "resolve_cluster_receipt_path",
    "role_identities_from_contract",
    "role_identity_aliases_from_contract",
    "select_cluster_contract",
]
