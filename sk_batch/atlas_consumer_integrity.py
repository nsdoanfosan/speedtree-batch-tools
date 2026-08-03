"""Read-only Atlas Leaf Builder consumer-integrity audit.

This module deliberately emits evidence only.  It never rewrites an SPM,
manifest, or external mesh file; the content-addressed repair input is for a
separate reviewed recovery workflow.
"""
from __future__ import annotations

import hashlib
import json
import os
from collections import Counter, defaultdict
from pathlib import Path

from atlas_manifest_resolver import (
    AtlasManifestResolutionError,
    resolve_atlas_manifests,
    resolution_evidence,
)


ATLAS_LEAF_GENERATOR = "Atlas Leaf Mesh Builder"
INTEGRITY_CONTRACT_KIND = "atlas_consumer_managed_asset_integrity"
INTEGRITY_SCHEMA_VERSION = 1
REPAIR_INPUT_KIND = "atlas_consumer_integrity_repair_input"
SEMANTIC_GENERATOR_TYPES = {"frond", "leafmesh"}


def _integer(value):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _path_key(path):
    try:
        value = Path(path).expanduser().resolve()
    except (OSError, TypeError, ValueError):
        value = Path(str(path or "")).expanduser().absolute()
    return os.path.normcase(str(value)).casefold()


def _canonical_json(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _marker_info(node, expected_kind):
    raw = str(node.findtext("UserData") or "").strip()
    try:
        value = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return {
            "claimed": False,
            "valid": False,
            "payload": {},
            "reason": "userdata_not_json" if raw else "userdata_missing",
        }
    if not isinstance(value, dict):
        return {
            "claimed": False,
            "valid": False,
            "payload": {},
            "reason": "userdata_not_object",
        }
    claimed = (
        str(value.get("generator") or "").casefold()
        == ATLAS_LEAF_GENERATOR.casefold()
    )
    actual_kind = str(value.get("kind") or "").strip().casefold()
    valid = claimed and actual_kind == expected_kind
    return {
        "claimed": claimed,
        "valid": valid,
        "payload": value if claimed else {},
        "reason": (
            "valid"
            if valid
            else "atlas_marker_kind_mismatch"
            if claimed
            else "foreign_or_manual_userdata"
        ),
    }


def _material_mesh_ids(material):
    values = []
    primary = _integer(material.findtext("CutoutMeshID"))
    if primary not in {None, -1}:
        values.append(primary)
    for node in material.findall("./SupplementalCutoutMeshIDs/CutoutMesh"):
        mesh_id = _integer(node.attrib.get("ID"))
        if mesh_id not in {None, -1}:
            values.append(mesh_id)
    return list(dict.fromkeys(values))


def _generator_reachability(root):
    material_ids = set()
    mesh_ids = set()
    default_cutout_material_ids = set()
    slots = []
    for generator_index, generator in enumerate(root.iter("Generator")):
        generator_type = str(
            generator.attrib.get("Type") or generator.findtext("Type") or ""
        ).strip()
        normalized_type = "".join(
            character
            for character in generator_type.casefold()
            if character.isalnum()
        )
        if normalized_type not in SEMANTIC_GENERATOR_TYPES:
            continue
        properties = generator.find("Properties")
        if properties is None:
            continue
        by_name = defaultdict(list)
        actual_names = {}
        for prop in list(properties):
            name = str(prop.findtext("Name") or "").strip()
            if not name:
                continue
            key = name.casefold()
            by_name[key].append(prop)
            actual_names.setdefault(key, name)
        prefixes = {
            name.rsplit(":", 1)[0]
            for name in by_name
            if name.endswith(":material") or name.endswith(":mesh")
        }
        for prefix_key in sorted(prefixes):
            material_properties = by_name.get(f"{prefix_key}:material") or []
            mesh_properties = by_name.get(f"{prefix_key}:mesh") or []
            material_property = (
                material_properties[0] if len(material_properties) == 1 else None
            )
            mesh_property = mesh_properties[0] if len(mesh_properties) == 1 else None
            material_id = (
                _integer(material_property.findtext("Value"))
                if material_property is not None
                else None
            )
            mesh_id = (
                _integer(mesh_property.findtext("Value"))
                if mesh_property is not None
                else None
            )
            if material_id is not None and material_id > 0:
                material_ids.add(material_id)
                if mesh_id == -10:
                    default_cutout_material_ids.add(material_id)
                elif mesh_id is not None and mesh_id > 0:
                    mesh_ids.add(mesh_id)
            prefix = (
                actual_names.get(f"{prefix_key}:material")
                or actual_names.get(f"{prefix_key}:mesh")
                or prefix_key
            ).rsplit(":", 1)[0]
            slots.append({
                "generator_index": generator_index,
                "generator_guid": str(
                    generator.attrib.get("GUID")
                    or generator.attrib.get("Guid")
                    or generator.findtext("GUID")
                    or generator.findtext("Guid")
                    or ""
                ).strip(),
                "generator_name": str(generator.findtext("Name") or "").strip(),
                "generator_type": generator_type,
                "hidden": str(generator.findtext("Hidden") or "").strip().casefold()
                in {"1", "true", "yes"},
                "slot_prefix": prefix,
                "material_id": material_id,
                "mesh_id": mesh_id,
                "material_property_count": len(material_properties),
                "mesh_property_count": len(mesh_properties),
            })
    return {
        "material_ids": material_ids,
        "mesh_ids": mesh_ids,
        "default_cutout_material_ids": default_cutout_material_ids,
        "slots": slots,
    }


def _manifest_spm_path(value, target):
    candidate = Path(str(value or "")).expanduser()
    if candidate.is_absolute():
        return candidate
    return target.parent / candidate


def _source_identity(payload, resolver_identity=None):
    resolver_identity = resolver_identity or {}
    blend_file = str(resolver_identity.get("blend_file") or "").strip()
    if not blend_file:
        blend_file = str(payload.get("blend_file") or "").strip()
    if not blend_file:
        receipt = payload.get("normalized_prototype_receipt") or {}
        physical = receipt.get("physical_capture_contract") or {}
        blend_file = str(physical.get("source_blend") or "").strip()
    collection = str(
        resolver_identity.get("source_collection")
        or payload.get("source_collection")
        or ""
    ).strip()
    scope_id = str(
        resolver_identity.get("export_scope_id")
        or payload.get("export_scope_id")
        or ""
    ).strip()
    identity = {
        "blend_file": (
            str(Path(blend_file).expanduser().absolute())
            if blend_file
            else ""
        ),
        "source_collection": collection,
        "export_scope_id": scope_id,
    }
    producer_projection = {
        "blend_file": identity["blend_file"].replace("\\", "/").casefold(),
        "source_collection": collection.casefold(),
    }
    identity["complete"] = bool(
        identity["blend_file"] and collection and scope_id
    )
    identity["producer_identity_sha256"] = hashlib.sha256(
        _canonical_json(producer_projection)
    ).hexdigest()
    identity["identity_sha256"] = hashlib.sha256(
        _canonical_json({**producer_projection, "export_scope_id": scope_id.casefold()})
    ).hexdigest()
    return identity


def _receipt_groups(payload):
    groups = []
    raw_groups = payload.get("speedtree_material_groups") or []
    if not raw_groups:
        raw_groups = payload.get("material_groups") or []
    if not raw_groups and payload.get("material_id"):
        raw_groups = [{
            "material": payload.get("material_name") or payload.get("material"),
            "material_id": payload.get("material_id"),
            "mesh_ids": payload.get("mesh_ids") or [],
        }]
    for group in raw_groups:
        if not isinstance(group, dict):
            continue
        material_id = _integer(group.get("material_id"))
        if material_id is None or material_id <= 0:
            continue
        groups.append({
            "material_id": material_id,
            "material_name": str(group.get("material") or ""),
            "material_collection": str(group.get("collection") or ""),
            "mesh_ids": sorted({
                mesh_id
                for mesh_id in (
                    _integer(value) for value in group.get("mesh_ids") or []
                )
                if mesh_id is not None and mesh_id > 0
            }),
        })
    return groups


def _receipt_record(path, payload, kind, resolver_identity=None, reason=""):
    identity = _source_identity(payload, resolver_identity)
    return {
        "kind": kind,
        "manifest_path": str(Path(path).expanduser().absolute()),
        "scope_id": identity["export_scope_id"],
        "source_identity": identity,
        "material_groups": _receipt_groups(payload),
        "generator_connection_complete": bool(
            (payload.get("generator_connection") or {}).get("complete")
        ),
        "atlas_scope_lifecycle": dict(
            payload.get("atlas_scope_lifecycle") or {}
        ),
        "declared_spm": str(payload.get("spm") or ""),
        "reason": reason,
    }


def _shadow_receipts(target, evidence):
    rows = []
    for record in evidence.get("shadowed") or []:
        path = Path(str(record.get("path") or ""))
        if not path.is_file():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        receipt = _receipt_record(
            path,
            payload,
            str(record.get("kind") or "shadowed"),
            record.get("source_identity") or {},
            str(record.get("reason") or ""),
        )
        declared = receipt["declared_spm"]
        receipt["target_matches"] = bool(
            declared
            and _path_key(_manifest_spm_path(declared, target))
            == _path_key(target)
        )
        rows.append(receipt)
    return rows


def resolve_canonical_atlas_producer_receipts(target_spm):
    """Adapt the issue #58 canonical resolver into ownership records."""
    target = Path(target_spm).expanduser().absolute()
    error = ""
    try:
        resolution = resolve_atlas_manifests(target)
        evidence = resolution_evidence(resolution)
        selected_rows = resolution.get("selected") or []
        status = "resolved" if selected_rows else "missing"
    except AtlasManifestResolutionError as exc:
        resolution = None
        evidence = dict(exc.resolution or {})
        selected_rows = []
        status = "conflict"
        error = str(exc)

    selected = [
        _receipt_record(
            row["path"],
            row.get("payload") or {},
            row.get("kind") or "",
            row.get("source_identity") or {},
            row.get("reason") or "",
        )
        for row in selected_rows
    ]
    return {
        "status": status,
        "error": error,
        "selected": selected,
        "shadowed": _shadow_receipts(target, evidence),
        "resolution_evidence": evidence,
    }


def _receipt_claims(receipt, asset_kind, asset_id):
    if asset_kind == "material":
        return any(
            group["material_id"] == asset_id
            for group in receipt.get("material_groups") or []
        )
    return any(
        asset_id in group.get("mesh_ids", [])
        for group in receipt.get("material_groups") or []
    )


def _same_scope(first, second):
    return bool(first and second and str(first).casefold() == str(second).casefold())


def _successor_scope(lifecycle):
    for key in (
        "successor_export_scope_id",
        "successor_scope_id",
        "replacement_export_scope_id",
    ):
        value = str((lifecycle or {}).get(key) or "").strip()
        if value:
            return value
    return ""


def _preferred_receipt(receipts):
    precedence = {
        "exact_per_target": 0,
        "exact_target_scope": 1,
        "exact_global_target": 2,
    }
    return min(
        receipts,
        key=lambda row: (
            precedence.get(row.get("kind"), 99),
            row.get("manifest_path", "").casefold(),
        ),
    )


def _classification_for_asset(
    asset_kind,
    scope_id,
    asset_id,
    reachable,
    default_cutout,
    receipts,
):
    selected_claims = [
        receipt for receipt in receipts.get("selected") or []
        if _receipt_claims(receipt, asset_kind, asset_id)
    ]
    exact_claims = [
        receipt for receipt in selected_claims
        if _same_scope(receipt.get("scope_id"), scope_id)
    ]
    if exact_claims:
        receipt = _preferred_receipt(exact_claims)
        paths = sorted({row["manifest_path"] for row in exact_claims})
        if default_cutout:
            return (
                "current_default_cutout", "current_default_cutout",
                receipt, paths, False,
            )
        if reachable:
            return (
                "current_reachable", "generator_reachable",
                receipt, paths, False,
            )
        return (
            "ambiguous", "authoritative_current_unreferenced",
            receipt, paths, False,
        )

    if selected_claims:
        receipt = _preferred_receipt(selected_claims)
        return (
            "ambiguous", "selected_authority_scope_mismatch",
            receipt,
            sorted({row["manifest_path"] for row in selected_claims}),
            False,
        )

    retired = []
    for receipt in receipts.get("shadowed") or []:
        lifecycle = receipt.get("atlas_scope_lifecycle") or {}
        successor_scope = _successor_scope(lifecycle)
        if (
            receipt.get("reason") == "retired_scope_record"
            and receipt.get("target_matches")
            and _same_scope(receipt.get("scope_id"), scope_id)
            and _receipt_claims(receipt, asset_kind, asset_id)
            and successor_scope
        ):
            successor = [
                row for row in receipts.get("selected") or []
                if _same_scope(row.get("scope_id"), successor_scope)
                and (
                    row.get("source_identity") or {}
                ).get("producer_identity_sha256")
                == (
                    receipt.get("source_identity") or {}
                ).get("producer_identity_sha256")
            ]
            if successor:
                retired.append(receipt)
    if retired:
        receipt = _preferred_receipt(retired)
        return (
            "superseded_with_proven_successor",
            "retired_scope_with_proven_successor",
            receipt,
            sorted({row["manifest_path"] for row in retired}),
            True,
        )

    foreign = [
        receipt for receipt in receipts.get("shadowed") or []
        if not receipt.get("target_matches")
        and _same_scope(receipt.get("scope_id"), scope_id)
        and _receipt_claims(receipt, asset_kind, asset_id)
    ]
    if foreign:
        receipt = _preferred_receipt(foreign)
        return (
            "protected_foreign", "different_target_authority",
            receipt,
            sorted({row["manifest_path"] for row in foreign}),
            False,
        )

    same_scope_authority = [
        receipt for receipt in receipts.get("selected") or []
        if _same_scope(receipt.get("scope_id"), scope_id)
    ]
    receipt = _preferred_receipt(same_scope_authority) if same_scope_authority else None
    return (
        "ambiguous",
        "selected_scope_asset_unclaimed" if receipt else "lineage_unproven",
        receipt,
        sorted({row["manifest_path"] for row in same_scope_authority}),
        False,
    )


def _asset_source_identity(receipt):
    return dict((receipt or {}).get("source_identity") or {})


def _asset_authority_key(row):
    """Return the exact selected authority that classified one asset."""
    identity = row.get("source_identity") or {}
    scope_id = str(row.get("scope_id") or "").strip().casefold()
    producer = str(identity.get("producer_identity_sha256") or "").strip()
    if not scope_id or not producer or identity.get("complete") is not True:
        return None
    return scope_id, producer


def _selected_authorities(receipts):
    grouped = defaultdict(list)
    for receipt in receipts.get("selected") or []:
        identity = receipt.get("source_identity") or {}
        grouped[(
            str(receipt.get("scope_id") or "").casefold(),
            str(identity.get("producer_identity_sha256") or ""),
        )].append(receipt)
    authorities = []
    for (_scope_key, _producer_key), rows in sorted(grouped.items()):
        preferred = _preferred_receipt(rows)
        groups = [
            group
            for row in rows
            for group in row.get("material_groups") or []
        ]
        authorities.append({
            "scope_id": preferred.get("scope_id") or "",
            "source_identity": _asset_source_identity(preferred),
            "manifest_paths": sorted({
                row["manifest_path"] for row in rows
            }),
            "material_ids": sorted({
                group["material_id"] for group in groups
            }),
            "mesh_ids": sorted({
                mesh_id
                for group in groups
                for mesh_id in group.get("mesh_ids") or []
            }),
        })
    return authorities


def audit_atlas_consumer_integrity(target_spm, root):
    """Inventory and classify Atlas-managed assets without mutating inputs."""
    target = Path(target_spm).expanduser().absolute()
    receipts = resolve_canonical_atlas_producer_receipts(target)
    reachability = _generator_reachability(root)
    integrity_issues = []
    invalid_material_ids = set()
    invalid_mesh_ids = set()

    def add_asset_issue(code, reason, *, asset_kind, asset_id, **details):
        issue = {
            "code": code,
            "reason": reason,
            "asset_kind": asset_kind,
            "asset_id": asset_id,
        }
        issue.update(details)
        integrity_issues.append(issue)
        if asset_kind == "material" and asset_id is not None:
            invalid_material_ids.add(asset_id)
        if asset_kind == "mesh" and asset_id is not None:
            invalid_mesh_ids.add(asset_id)

    material_rows_by_id = defaultdict(list)
    material_nodes = []
    for material in root.iter("Material_v8"):
        material_id = _integer(material.attrib.get("ID"))
        if material_id is None or material_id <= 0:
            continue
        marker_info = _marker_info(material, "material")
        marker = marker_info["payload"]
        row = {
            "asset_kind": "material",
            "material_id": material_id,
            "material_name": str(material.attrib.get("Name") or ""),
            "mesh_ids": _material_mesh_ids(material),
            "scope_id": str(marker.get("scope") or ""),
            "group": str(marker.get("group") or ""),
            "managed": marker_info["claimed"],
            "marker_valid": marker_info["valid"],
            "ownership_basis": "material_marker" if marker_info["claimed"] else "manual",
        }
        material_rows_by_id[material_id].append(row)
        material_nodes.append(row)
        if marker_info["claimed"] and not marker_info["valid"]:
            add_asset_issue(
                "atlas_ownership_marker_invalid",
                "Atlas Material ownership marker has the wrong or missing kind",
                asset_kind="material",
                asset_id=material_id,
            )

    for material_id, rows in sorted(material_rows_by_id.items()):
        if len(rows) > 1:
            add_asset_issue(
                "duplicate_material_id",
                "SPM contains more than one Material with the same ID",
                asset_kind="material",
                asset_id=material_id,
                duplicate_count=len(rows),
            )

    materials = {
        material_id: rows[0]
        for material_id, rows in material_rows_by_id.items()
        if len(rows) == 1
    }

    raw_mesh_rows = []
    for mesh in root.iter("Mesh"):
        mesh_id = _integer(mesh.attrib.get("ID"))
        if mesh_id is None or mesh_id <= 0:
            continue
        marker_info = _marker_info(mesh, "mesh")
        marker = marker_info["payload"]
        raw_mesh_rows.append({
            "asset_kind": "mesh",
            "mesh_id": mesh_id,
            "mesh_name": str(mesh.attrib.get("Name") or ""),
            "direct_scope_id": str(marker.get("scope") or ""),
            "direct_group": str(marker.get("group") or ""),
            "direct_marker_claimed": marker_info["claimed"],
            "direct_marker_valid": marker_info["valid"],
        })

    owners = defaultdict(list)
    for material in material_nodes:
        for mesh_id in material["mesh_ids"]:
            owners[mesh_id].append(material)

    mesh_rows_by_id = defaultdict(list)
    for raw in raw_mesh_rows:
        mesh_id = raw["mesh_id"]
        owner_rows = owners.get(mesh_id, [])
        managed_owners = [row for row in owner_rows if row["managed"]]
        manual_owners = [row for row in owner_rows if not row["managed"]]
        inherited_scopes = sorted({
            row["scope_id"] for row in managed_owners if row["scope_id"]
        })
        inherited_groups = sorted({
            row["group"] for row in managed_owners if row["group"]
        })
        protected_manual = bool(manual_owners)
        managed = bool(
            not protected_manual
            and (raw["direct_marker_claimed"] or managed_owners)
        )
        scope_id = raw["direct_scope_id"]
        group = raw["direct_group"]
        if not scope_id and len(inherited_scopes) == 1:
            scope_id = inherited_scopes[0]
        if not group and len(inherited_groups) == 1:
            group = inherited_groups[0]
        row = {
            **raw,
            "scope_id": scope_id,
            "group": group,
            "managed": managed,
            "marker_valid": (
                raw["direct_marker_valid"]
                if raw["direct_marker_claimed"]
                else bool(managed_owners)
            ),
            "ownership_basis": (
                "manual_co_owner"
                if protected_manual
                else "mesh_marker"
                if raw["direct_marker_claimed"]
                else "managed_material"
                if managed_owners
                else "manual"
            ),
            "owner_material_ids": sorted({
                row["material_id"] for row in owner_rows
            }),
        }
        mesh_rows_by_id[mesh_id].append(row)

        if raw["direct_marker_claimed"] and not raw["direct_marker_valid"]:
            add_asset_issue(
                "atlas_ownership_marker_invalid",
                "Atlas Mesh ownership marker has the wrong or missing kind",
                asset_kind="mesh",
                asset_id=mesh_id,
            )
        if managed and len(inherited_scopes) > 1:
            add_asset_issue(
                "managed_mesh_owner_ambiguous",
                "Builder-managed Mesh is owned by Materials from multiple scopes",
                asset_kind="mesh",
                asset_id=mesh_id,
                owner_scope_ids=inherited_scopes,
            )
        if (
            managed
            and raw["direct_scope_id"]
            and inherited_scopes
            and any(
                not _same_scope(raw["direct_scope_id"], value)
                for value in inherited_scopes
            )
        ):
            add_asset_issue(
                "managed_mesh_scope_mismatch",
                "Mesh marker scope disagrees with its managed Material owner",
                asset_kind="mesh",
                asset_id=mesh_id,
                marker_scope_id=raw["direct_scope_id"],
                owner_scope_ids=inherited_scopes,
            )

    for mesh_id, rows in sorted(mesh_rows_by_id.items()):
        if len(rows) > 1:
            add_asset_issue(
                "duplicate_mesh_id",
                "SPM contains more than one Mesh with the same ID",
                asset_kind="mesh",
                asset_id=mesh_id,
                duplicate_count=len(rows),
            )

    meshes = {
        mesh_id: rows[0]
        for mesh_id, rows in mesh_rows_by_id.items()
        if len(rows) == 1
    }

    selected_groups = [
        group
        for receipt in receipts.get("selected") or []
        for group in receipt.get("material_groups") or []
    ]
    selected_material_ids = {
        group["material_id"] for group in selected_groups
    }
    selected_mesh_ids = {
        mesh_id
        for group in selected_groups
        for mesh_id in group.get("mesh_ids") or []
    }
    current_material_mesh_pairs = {
        (group["material_id"], mesh_id)
        for group in selected_groups
        for mesh_id in group.get("mesh_ids") or []
    }

    generator_material_references = defaultdict(list)
    generator_mesh_references = defaultdict(list)
    for slot in reachability["slots"]:
        material_id = slot.get("material_id")
        mesh_id = slot.get("mesh_id")
        material = materials.get(material_id)
        mesh = meshes.get(mesh_id)
        relevant = bool(
            (material or {}).get("managed")
            or (mesh or {}).get("managed")
        )
        if not relevant:
            continue
        public_slot = dict(slot)
        if material_id is not None and material_id > 0:
            generator_material_references[material_id].append(public_slot)
        if mesh_id is not None and mesh_id > 0:
            generator_mesh_references[mesh_id].append(public_slot)

        def add_generator_issue(code, reason):
            integrity_issues.append({
                "code": code,
                "reason": reason,
                "generator_index": slot["generator_index"],
                "generator_guid": slot["generator_guid"],
                "generator_name": slot["generator_name"],
                "slot_prefix": slot["slot_prefix"],
                "material_id": material_id,
                "mesh_id": mesh_id,
                "hidden": slot["hidden"],
            })
            if material_id is not None and material_id > 0:
                invalid_material_ids.add(material_id)
            if mesh_id is not None and mesh_id > 0:
                invalid_mesh_ids.add(mesh_id)

        if not slot["generator_guid"]:
            add_generator_issue(
                "generator_guid_missing",
                "Managed Material/Mesh slot has no stable Generator GUID provenance",
            )
        if (
            slot["material_property_count"] != 1
            or slot["mesh_property_count"] != 1
        ):
            add_generator_issue(
                "generator_slot_pair_incomplete",
                "Managed Generator slot does not have exactly one Material/Mesh pair",
            )
        if mesh_id is None or mesh_id <= 0:
            continue
        pair_reasons = []
        if material is None or mesh_id not in material.get("mesh_ids", []):
            pair_reasons.append("SPM Material does not own the referenced Mesh")
        if material and mesh and (
            material.get("scope_id")
            and mesh.get("scope_id")
            and material["scope_id"] != mesh["scope_id"]
        ):
            pair_reasons.append("Material and Mesh ownership scopes differ")
        if (
            material_id in selected_material_ids
            and mesh_id in selected_mesh_ids
            and (material_id, mesh_id) not in current_material_mesh_pairs
        ):
            pair_reasons.append(
                "Selected manifests assign Material and Mesh to different groups"
            )
        if pair_reasons:
            add_generator_issue(
                "generator_cross_group_pair",
                "; ".join(pair_reasons),
            )

    managed_candidate_count = sum(row["managed"] for row in material_nodes) + sum(
        row["managed"] for rows in mesh_rows_by_id.values() for row in rows
    )
    if managed_candidate_count and receipts["status"] != "resolved":
        integrity_issues.append({
            "code": (
                "atlas_manifest_resolution_conflict"
                if receipts["status"] == "conflict"
                else "atlas_manifest_authority_missing"
            ),
            "reason": receipts.get("error") or (
                "No canonical Atlas producer receipt selected for managed assets"
            ),
            "asset_kind": "manifest",
            "asset_id": None,
        })

    managed_materials = []
    protected_manual_materials = []
    for row in sorted(material_nodes, key=lambda item: item["material_id"]):
        if not row["managed"]:
            protected_manual_materials.append({
                **row,
                "classification": "protected_manual",
                "manifest_paths": [],
                "source_identity": {},
                "orphan_reason": "manual_or_unmarked_material",
                "automatic_action_eligible": False,
            })
            continue
        material_id = row["material_id"]
        classification, orphan_reason, receipt, paths, eligible = (
            (
                "ambiguous", "asset_integrity_issue", None, [], False,
            )
            if material_id in invalid_material_ids
            else _classification_for_asset(
                "material",
                row["scope_id"],
                material_id,
                material_id in reachability["material_ids"],
                material_id in reachability["default_cutout_material_ids"],
                receipts,
            )
        )
        managed_materials.append({
            **row,
            "classification": classification,
            "orphan_reason": orphan_reason,
            "manifest_paths": paths,
            "source_identity": _asset_source_identity(receipt),
            "generator_references": generator_material_references[material_id],
            "automatic_action_eligible": eligible,
        })

    managed_meshes = []
    protected_manual_meshes = []
    default_mesh_ids = {
        mesh_id
        for material_id in reachability["default_cutout_material_ids"]
        for mesh_id in (materials.get(material_id) or {}).get("mesh_ids", [])
    }
    for mesh_id, row in sorted(meshes.items()):
        base = dict(row)
        if not row["managed"]:
            protected_manual_meshes.append({
                **base,
                "classification": "protected_manual",
                "manifest_paths": [],
                "source_identity": {},
                "orphan_reason": row.get("ownership_basis") or "manual_mesh",
                "automatic_action_eligible": False,
            })
            continue
        classification, orphan_reason, receipt, paths, eligible = (
            (
                "ambiguous", "asset_integrity_issue", None, [], False,
            )
            if mesh_id in invalid_mesh_ids
            else _classification_for_asset(
                "mesh",
                row["scope_id"],
                mesh_id,
                mesh_id in reachability["mesh_ids"],
                mesh_id in default_mesh_ids,
                receipts,
            )
        )
        managed_meshes.append({
            **base,
            "classification": classification,
            "orphan_reason": orphan_reason,
            "manifest_paths": paths,
            "source_identity": _asset_source_identity(receipt),
            "generator_references": generator_mesh_references[mesh_id],
            "automatic_action_eligible": eligible,
        })

    # Generator reachability is not an age signal. Current Atlas producers may
    # publish more Materials/Mesh variants than an exact SpeedTree target
    # selects, both across whole groups and within one group. Calling those
    # unselected variants "stale" blocked coherent targets such as blackgum
    # before Blender could start.
    #
    # Preserve current unreferenced variants per asset. A SpeedTree target can
    # legitimately consume several disjoint Atlas producers (for example leaf
    # and bark); counting authorities for the whole target discarded the exact
    # claim already proved for each asset and falsely blocked those consumers.
    #
    # The target must still contain a live current binding and have no integrity
    # damage. Each preserved asset must carry a complete exact selected claim;
    # a Mesh is preserved only when every owning Material has that same exact
    # authority. Lineage-unproven, scope-mismatched, mixed-owner, and damaged
    # assets remain ambiguous and blocking.
    live_current_asset = any(
        row.get("classification") in {
            "current_reachable",
            "current_default_cutout",
        }
        for row in managed_materials + managed_meshes
    )
    if live_current_asset and not integrity_issues:
        for material in managed_materials:
            if (
                material.get("classification") == "ambiguous"
                and material.get("orphan_reason")
                == "authoritative_current_unreferenced"
                and not material.get("generator_references")
                and _asset_authority_key(material) is not None
            ):
                material["classification"] = (
                    "current_preserved_unreferenced"
                )
                material["orphan_reason"] = (
                    "authoritative_current_variant_not_selected"
                )
        materials_by_id = {
            row.get("material_id"): row for row in managed_materials
        }
        for row in managed_meshes:
            authority_key = _asset_authority_key(row)
            owner_ids = list(row.get("owner_material_ids") or ())
            owners = [materials_by_id.get(value) for value in owner_ids]
            if (
                row.get("classification") == "ambiguous"
                and row.get("orphan_reason")
                == "authoritative_current_unreferenced"
                and not row.get("generator_references")
                and authority_key is not None
                and owner_ids
                and all(
                    owner is not None
                    and _asset_authority_key(owner) == authority_key
                    for owner in owners
                )
            ):
                row["classification"] = (
                    "current_preserved_unreferenced"
                )
                row["orphan_reason"] = (
                    "authoritative_current_variant_not_selected"
                )

    managed_assets = managed_materials + managed_meshes
    classification_counts = dict(sorted(Counter(
        row["classification"] for row in managed_assets
    ).items()))
    # Older builder Mesh nodes may lack direct UserData.  A uniquely owned Mesh
    # inherits the explicit builder marker from its Material, so it still
    # participates in the generation-level set difference.  Material-only
    # evidence retains the legacy marker warning path until a managed Mesh is
    # present; that avoids turning unrelated historical material markers into
    # destructive cleanup evidence.
    integrity_applicable = bool(managed_meshes)
    blocking_assets = [
        row for row in managed_assets
        if row["classification"] in {
            "superseded_with_proven_successor",
            "ambiguous",
        }
    ] if integrity_applicable else []

    generation_rows = defaultdict(list)
    for row in managed_assets:
        generation_rows[(
            row.get("scope_id") or "",
            (row.get("source_identity") or {}).get("identity_sha256") or "",
            tuple(row.get("manifest_paths") or []),
        )].append(row)
    generations = []
    priority = (
        "ambiguous",
        "superseded_with_proven_successor",
        "current_reachable",
        "current_default_cutout",
        "current_preserved_unreferenced",
        "protected_foreign",
    )
    for (scope_id, _identity_hash, manifest_paths), rows in sorted(
        generation_rows.items(), key=lambda item: item[0]
    ):
        row_classes = {row["classification"] for row in rows}
        classification = next(
            value for value in priority if value in row_classes
        )
        generations.append({
            "scope_id": scope_id,
            "classification": classification,
            "orphan_reasons": sorted({
                row.get("orphan_reason") or "" for row in rows
            }),
            "automatic_action_eligible": all(
                row.get("automatic_action_eligible") for row in rows
            ),
            "material_ids": sorted(
                row["material_id"] for row in rows if row["asset_kind"] == "material"
            ),
            "mesh_ids": sorted(
                row["mesh_id"] for row in rows if row["asset_kind"] == "mesh"
            ),
            "manifest_paths": list(manifest_paths),
            "source_identity": next(
                (row["source_identity"] for row in rows if row.get("source_identity")),
                {},
            ),
            "classification_counts": dict(sorted(Counter(
                row["classification"] for row in rows
            ).items())),
        })

    selected = receipts.get("selected") or []
    selected_paths = sorted({row["manifest_path"] for row in selected})
    selected_scope_ids = sorted({
        row["scope_id"] for row in selected if row.get("scope_id")
    })
    authorities = _selected_authorities(receipts)
    preferred_current = _preferred_receipt(selected) if selected else {}
    repair_payload = {
        "kind": REPAIR_INPUT_KIND,
        "schema_version": INTEGRITY_SCHEMA_VERSION,
        "spm": str(target),
        "spm_sha256": _sha256_file(target),
        "selected_manifest_paths": selected_paths,
        "selected_authorities": authorities,
        "manifest_resolution": receipts["resolution_evidence"],
        "candidates": [
            {
                key: value for key, value in row.items()
                if key not in {"managed"}
            }
            for row in blocking_assets
        ],
        "integrity_issues": integrity_issues,
        "review_policy": (
            "No mutation is authorized by this evidence. Only an explicit "
            "retired scope with a same-producer successor is marked eligible; "
            "authoritative-current, foreign/manual, and lineage-unproven "
            "assets must be preserved."
        ),
    }
    repair_input = {
        **repair_payload,
        "content_sha256": hashlib.sha256(_canonical_json(repair_payload)).hexdigest(),
    }
    return {
        "kind": INTEGRITY_CONTRACT_KIND,
        "schema_version": INTEGRITY_SCHEMA_VERSION,
        "status": "blocked" if blocking_assets or integrity_issues else "ok",
        "blocking": bool(blocking_assets or integrity_issues),
        "applicable": integrity_applicable,
        "spm": str(target),
        "receipt_resolution": receipts["status"],
        "receipt_resolution_error": receipts.get("error") or "",
        "manifest_resolution": receipts["resolution_evidence"],
        "current_manifest_path": str(
            preferred_current.get("manifest_path") or ""
        ),
        "selected_manifest_paths": selected_paths,
        "current_scope_id": (
            selected_scope_ids[0] if len(selected_scope_ids) == 1 else ""
        ),
        "selected_scope_ids": selected_scope_ids,
        "current_source_identity": (
            authorities[0]["source_identity"] if len(authorities) == 1 else {}
        ),
        "selected_authorities": authorities,
        "classification_counts": classification_counts,
        "active_managed_mesh_count": sum(
            row["classification"] in {
                "current_reachable",
                "current_default_cutout",
            }
            for row in managed_meshes
        ),
        "managed_orphan_mesh_count": sum(
            row["classification"] in {
                "superseded_with_proven_successor",
                "ambiguous",
            }
            for row in managed_meshes
        ),
        "managed_orphan_material_count": sum(
            row["classification"] in {
                "superseded_with_proven_successor",
                "ambiguous",
            }
            for row in managed_materials
        ),
        "managed_materials": managed_materials,
        "managed_meshes": managed_meshes,
        "protected_manual_materials": protected_manual_materials,
        "protected_manual_meshes": protected_manual_meshes,
        "generations": generations,
        "generator_slots": reachability["slots"],
        "integrity_issues": integrity_issues,
        "repair_input": repair_input,
    }
