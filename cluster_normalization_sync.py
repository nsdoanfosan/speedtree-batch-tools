"""Resolve and audit automatic Cluster Normalizer inputs for Generator Sync.

The raw ``Cluster/SK_*.blend`` is produced by SK Batch.  Generator Sync owns
the next transition: build the physical-capture prototypes/plans in that same
blend, persist a content-addressed receipt, then let Atlas update the owner
``SK_*.spm`` targets.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path

from atlas_manifest_resolver import (
    resolve_atlas_manifests,
    selected_manifest_payload,
)
from cluster_atlas_source_index import (
    inspect_persisted_source_index,
    normalized_path_key,
)
from cluster_card_pipeline.contract import _read_spm_root
from cluster_bark_source_resolution import (
    ClusterBarkSourceResolutionError,
    load_current_isolated_bark_manifest,
)
from generator_delivery_scope import (
    GeneratorDeliveryScopeError,
    validate_delivery_scope_intent,
)
from speedtree_pipeline_contract import (
    SPM_STRUCTURAL_SEMANTIC_PROJECTION_VERSION,
    generator_guid_key,
    prove_legacy_texture_normalize_semantic_migration,
    spm_file_structural_semantic_fingerprint,
)


class ClusterNormalizationSyncError(RuntimeError):
    """Actionable automatic-normalization preflight failure."""


class ClusterSourceBuildRequiredError(ClusterNormalizationSyncError):
    """The canonical Cluster blend/report must be rebuilt from its SPM."""

    def __init__(self, message, *, blend, canonical_spm, report_path, reason):
        super().__init__(message)
        self.blend = Path(blend).expanduser().absolute()
        self.canonical_spm = Path(canonical_spm).expanduser().absolute()
        self.report_path = Path(report_path).expanduser().absolute()
        self.reason = str(reason)


def _sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _canonical_sha256(value):
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _read_json(path):
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _normalized_path(path):
    return str(Path(path).expanduser().absolute()).casefold()


def _current_report_artifact(row, label):
    if not isinstance(row, dict) or row.get("exists") is not True:
        raise ClusterNormalizationSyncError(
            f"Cluster isolated bark report has no ready {label} artifact."
        )
    try:
        path = Path(row.get("path") or "").expanduser().absolute()
    except (OSError, TypeError, ValueError) as exc:
        raise ClusterNormalizationSyncError(
            f"Cluster isolated bark report has an invalid {label} path."
        ) from exc
    if not path.is_file():
        raise ClusterNormalizationSyncError(
            f"Cluster isolated bark {label} is missing: {path}"
        )
    current_hash = _sha256_file(path)
    if str(row.get("sha256") or "").casefold() != current_hash.casefold():
        raise ClusterNormalizationSyncError(
            f"Cluster isolated bark {label} is stale: {path}"
        )
    try:
        if int(row.get("size")) != path.stat().st_size:
            raise ClusterNormalizationSyncError(
                f"Cluster isolated bark {label} size is stale: {path}"
            )
    except (TypeError, ValueError) as exc:
        raise ClusterNormalizationSyncError(
            f"Cluster isolated bark report has no valid {label} size."
        ) from exc
    return path, current_hash


def _xml_source_tree_path(source_xml):
    source_xml = Path(source_xml).expanduser().absolute()
    try:
        root = ET.parse(source_xml).getroot()
    except (OSError, ET.ParseError) as exc:
        raise ClusterNormalizationSyncError(
            f"Cluster source XML is unreadable: {source_xml}: {exc}"
        ) from exc
    values = []
    for node in root.iter():
        tag = str(node.tag).rsplit("}", 1)[-1].casefold()
        if tag == "sourcetree":
            value = str(node.text or "").strip()
            if not value:
                for key, attribute in node.attrib.items():
                    if str(key).rsplit("}", 1)[-1].casefold() not in {
                        "file",
                        "path",
                        "filename",
                        "value",
                        "name",
                        "source",
                    }:
                        continue
                    value = str(attribute or "").strip()
                    if value:
                        break
            if not value:
                for child in node.iter():
                    if child is node:
                        continue
                    child_tag = str(child.tag).rsplit("}", 1)[-1].casefold()
                    if child_tag not in {
                        "file",
                        "path",
                        "filename",
                        "value",
                        "name",
                        "source",
                    }:
                        continue
                    value = str(child.text or "").strip()
                    if value:
                        break
            if value:
                values.append(value)
        for key, value in node.attrib.items():
            if str(key).rsplit("}", 1)[-1].casefold() == "sourcetree":
                value = str(value or "").strip()
                if value:
                    values.append(value)
    resolved = {}
    for value in values:
        candidate = Path(value.replace("\\", "/"))
        if not candidate.is_absolute():
            candidate = source_xml.parent / candidate
        candidate = candidate.expanduser().absolute()
        resolved[_normalized_path(candidate)] = candidate
    if len(resolved) != 1:
        detail = "missing" if not resolved else "ambiguous"
        raise ClusterNormalizationSyncError(
            "Cluster source XML SourceTree is "
            f"{detail}; expected one exact provider path: {source_xml}"
        )
    return next(iter(resolved.values()))


def _isolated_bark_bundle_from_report(report, source_xml, canonical_spm):
    resolution = report.get("cluster_bark_source_resolution") or {}
    if not resolution:
        return None
    if resolution.get("status") != "ready":
        raise ClusterNormalizationSyncError(
            "Cluster isolated bark source report is not ready."
        )
    manifest_path, manifest_hash = _current_report_artifact(
        resolution.get("manifest"), "manifest"
    )
    source_spm, source_hash = _current_report_artifact(
        resolution.get("source_spm"), "production provider SPM"
    )
    isolated_spm, isolated_hash = _current_report_artifact(
        resolution.get("speedtree_spm"), "exact isolated provider SPM"
    )
    canonical_spm = Path(canonical_spm).expanduser().absolute()
    if _normalized_path(source_spm) != _normalized_path(canonical_spm):
        raise ClusterNormalizationSyncError(
            "Cluster isolated bark report belongs to a different production "
            f"provider: {source_spm}"
        )
    try:
        manifest = load_current_isolated_bark_manifest(
            manifest_path,
            source_spm=source_spm,
            speedtree_spm=isolated_spm,
        )
    except ClusterBarkSourceResolutionError as exc:
        raise ClusterNormalizationSyncError(str(exc)) from exc
    source_tree = _xml_source_tree_path(source_xml)
    if _normalized_path(source_tree) != _normalized_path(isolated_spm):
        raise ClusterNormalizationSyncError(
            "Cluster source XML SourceTree does not name the exact "
            f"hash-current isolated provider SPM: {source_tree} != "
            f"{isolated_spm}"
        )
    bundle = {
        "kind": "cluster_isolated_bark_recipe_bundle",
        "manifest": str(manifest_path),
        "manifest_sha256": manifest_hash,
        "source_spm": str(source_spm),
        "source_spm_sha256": source_hash,
        "speedtree_spm": str(isolated_spm),
        "isolated_spm_sha256": isolated_hash,
        "source_tree": str(source_tree),
        "source_xml_sha256": _sha256_file(source_xml),
        "provider_identity": manifest["provider_identity"],
        "output_filename": manifest["output_filename"],
    }
    bundle["bundle_sha256"] = _canonical_sha256(bundle)
    return bundle


def validate_isolated_bark_recipe_bundle(recipe):
    """Revalidate the sealed XML/cache bundle immediately before map bake."""
    if not isinstance(recipe, dict):
        return None
    bundle = recipe.get("isolated_bark_bundle")
    if bundle is None:
        return None
    if not isinstance(bundle, dict) or bundle.get("kind") != (
        "cluster_isolated_bark_recipe_bundle"
    ):
        raise ClusterNormalizationSyncError(
            "Cluster normalization recipe has an invalid isolated bark bundle."
        )
    sealed = dict(bundle)
    recorded_seal = sealed.pop("bundle_sha256", None)
    if not recorded_seal or recorded_seal != _canonical_sha256(sealed):
        raise ClusterNormalizationSyncError(
            "Cluster normalization recipe isolated bark bundle seal is stale."
        )
    source_xml = Path(recipe.get("source_xml") or "").expanduser().absolute()
    if (
        not source_xml.is_file()
        or recipe.get("source_xml_sha256") != _sha256_file(source_xml)
        or bundle.get("source_xml_sha256") != recipe.get("source_xml_sha256")
    ):
        raise ClusterNormalizationSyncError(
            "Cluster normalization recipe source XML is not hash-current."
        )
    source_tree = _xml_source_tree_path(source_xml)
    if (
        _normalized_path(source_tree)
        != _normalized_path(bundle.get("source_tree") or "")
        or _normalized_path(source_tree)
        != _normalized_path(bundle.get("speedtree_spm") or "")
    ):
        raise ClusterNormalizationSyncError(
            "Cluster normalization recipe SourceTree differs from its exact "
            "isolated provider SPM."
        )
    manifest_path = Path(bundle.get("manifest") or "").expanduser().absolute()
    if (
        not manifest_path.is_file()
        or _sha256_file(manifest_path) != bundle.get("manifest_sha256")
    ):
        raise ClusterNormalizationSyncError(
            "Cluster normalization recipe bark manifest is not hash-current."
        )
    try:
        manifest = load_current_isolated_bark_manifest(
            manifest_path,
            source_spm=bundle.get("source_spm"),
            speedtree_spm=bundle.get("speedtree_spm"),
        )
    except ClusterBarkSourceResolutionError as exc:
        raise ClusterNormalizationSyncError(str(exc)) from exc
    if (
        manifest.get("source_spm_sha256")
        != bundle.get("source_spm_sha256")
        or manifest.get("isolated_spm_sha256")
        != bundle.get("isolated_spm_sha256")
        or manifest.get("provider_identity")
        != bundle.get("provider_identity")
        or manifest.get("output_filename")
        != bundle.get("output_filename")
    ):
        raise ClusterNormalizationSyncError(
            "Cluster normalization recipe and bark manifest identities differ."
        )
    return bundle


def normalization_receipt_path(blend):
    blend = Path(blend).expanduser().absolute()
    return (
        blend.parent
        / "reports"
        / f"{blend.stem}_cluster_normalization_sync_receipt.json"
    )


def inspect_normalization_source_identity(blend):
    """Validate the Blender-authored Atlas source index without opening Blender."""
    blend = Path(blend).expanduser().absolute()
    receipt_path = normalization_receipt_path(blend)
    receipt = _read_json(receipt_path)
    if not receipt:
        inspected = inspect_persisted_source_index(blend, None)
        return {**inspected, "receipt": str(receipt_path)}
    identity = receipt.get("source_blender_index")
    if identity is None:
        # Fail closed on the superseded self-projected receipt field.  It is
        # diagnostic history only and cannot authorize an already-ON no-op.
        identity = receipt.get("source_blend_content_identity")
    inspected = inspect_persisted_source_index(blend, identity)
    reasons = list(inspected.get("refresh_reasons") or ())
    if (
        receipt.get("kind") != "speedtree_cluster_sync_normalization"
        or receipt.get("status") != "ready"
    ):
        reasons.append("blender_source_identity_invalid")
    receipt_blend = str(receipt.get("blend") or "")
    if not receipt_blend:
        reasons.append("blender_source_path_identity_missing")
    elif normalized_path_key(receipt_blend) != normalized_path_key(blend):
        reasons.append("blender_source_path_changed")
    reasons = sorted(set(reasons))
    return {
        **inspected,
        "status": "current" if not reasons else "refresh_required",
        "current": not reasons,
        "refresh_reasons": reasons,
        "receipt": str(receipt_path),
    }


def cluster_role_contract(blend):
    stem = Path(blend).stem
    base = stem[3:] if stem.casefold().startswith("sk_") else stem
    tokens = {token for token in base.casefold().split("_") if token}
    if "leaf" in tokens and "side" in tokens:
        role = "leaf_side"
        plane = "YZ"
    elif "leaf" in tokens:
        role = "leaf"
        plane = "XY"
    else:
        role = "branch"
        plane = "XY"
    return {
        "role": role,
        "capture_plane": plane,
        "plan_base": base,
        "skeletal_base": "SK_" + base,
        "plan_collection": (
            "Atlas_Branch_Plans"
            if role == "branch"
            else "Atlas_Cluster_Cards"
        ),
        "material_name": "M_" + base,
    }


def _material_record(node, target_spm):
    try:
        material_id = int(node.get("ID"))
    except (TypeError, ValueError) as exc:
        raise ClusterNormalizationSyncError(
            "Target material has an invalid ID: "
            f"{node.get('Name') or '<unnamed>'}: {target_spm}"
        ) from exc
    if material_id <= 0:
        raise ClusterNormalizationSyncError(
            "Target material has a non-positive ID: "
            f"{node.get('Name') or '<unnamed>'}: {target_spm}"
        )
    return {
        "material_id": material_id,
        "material_name": str(node.get("Name") or ""),
        "mesh_ids": _material_mesh_ids(node),
    }


def _atlas_managed_material_scope(node):
    """Return the owning Atlas scope for an explicitly managed material."""
    try:
        marker = json.loads(str(node.findtext("UserData") or "").strip())
    except (TypeError, ValueError, json.JSONDecodeError):
        return ""
    if not isinstance(marker, dict):
        return ""
    if (
        str(marker.get("generator") or "") != "Atlas Leaf Mesh Builder"
        or str(marker.get("kind") or "") != "material"
    ):
        return ""
    return str(marker.get("scope") or "").strip()


def _material_mesh_ids(node):
    values = []
    primary = _integer_text(node.findtext("CutoutMeshID"))
    if primary is not None and primary >= 0:
        values.append(primary)
    for item in node.findall(
        "./SupplementalCutoutMeshIDs/CutoutMesh"
    ):
        mesh_id = _integer_text(item.get("ID"))
        if mesh_id is not None and mesh_id >= 0:
            values.append(mesh_id)
    return values


def _material_family_name(value):
    name = str(value or "").strip()
    if name.casefold().startswith("m_"):
        name = name[2:]
    match = re.fullmatch(r"(?P<family>.+_)\d+", name, re.IGNORECASE)
    return match.group("family").casefold() if match else ""


def _referenced_material_ids(root):
    referenced = set()
    for generator in root.findall(".//Generator"):
        generator_type = str(
            generator.get("Type") or generator.findtext("Type") or ""
        ).strip().casefold()
        if generator_type not in {"frond", "leaf mesh"}:
            continue
        if str(generator.findtext("Hidden") or "").strip().casefold() in {
            "1",
            "true",
            "yes",
        }:
            continue
        for prop in generator.findall("./Properties/Property"):
            name = str(prop.findtext("Name") or "")
            if not name.endswith(":Material"):
                continue
            try:
                referenced.add(int(prop.findtext("Value")))
            except (TypeError, ValueError):
                continue
    return referenced


def _generator_property_pairs(root):
    pairs = []
    for generator in root.findall(".//Generator"):
        generator_type = str(
            generator.get("Type") or generator.findtext("Type") or ""
        ).strip()
        if generator_type.casefold().replace(" ", "") not in {
            "frond",
            "leafmesh",
        }:
            continue
        properties = generator.find("Properties")
        if properties is None:
            continue
        nodes = {
            str(node.findtext("Name") or "").strip(): node
            for node in list(properties)
            if str(node.findtext("Name") or "").strip()
        }
        for name, material_property in nodes.items():
            if not name.casefold().endswith(":material"):
                continue
            slot_prefix = name.rsplit(":", 1)[0]
            mesh_property = nodes.get(f"{slot_prefix}:Mesh")
            if mesh_property is None:
                continue
            pairs.append(
                {
                    "generator_name": str(
                        generator.findtext("Name") or ""
                    ).strip(),
                    "generator_guid": str(
                        generator.findtext("GUID")
                        or generator.get("GUID")
                        or ""
                    ).strip(),
                    "generator_type": generator_type,
                    "slot_prefix": slot_prefix,
                    "material_id": _integer_text(
                        material_property.findtext("Value")
                    ),
                    "mesh_id": _integer_text(
                        mesh_property.findtext("Value")
                    ),
                }
            )
    return pairs


def _integer_text(value):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _authoritative_binding_backup_paths(target_spm):
    target = Path(target_spm).expanduser().absolute()
    owner = target.parent
    tree_root = owner.parent
    candidates = []
    candidates.extend(
        owner.glob(
            f"_spm_backups/generator_sync_*/*{target.name}"
        )
    )
    candidates.extend(
        tree_root.glob(
            f"_spm_backups/texture_normalize_*/*{target.name}"
        )
    )
    candidates.extend(
        tree_root.glob(
            "_atlas_cluster_normalization_backups/"
            f"final_*/files/{owner.name}/{target.name}"
        )
    )
    unique = {}
    for candidate in candidates:
        if (
            candidate.is_file()
            and candidate.name.casefold().endswith(target.name.casefold())
        ):
            unique[str(candidate.resolve()).casefold()] = candidate.resolve()
    return [unique[key] for key in sorted(unique)]


def _material_id_by_exact_name(root, material_name):
    matches = [
        node
        for node in root.findall(".//Material_v8")
        if str(node.get("Name") or "") == material_name
    ]
    if len(matches) != 1:
        return None
    return _integer_text(matches[0].get("ID"))


def _atlas_target_relation_manifest(target_spm):
    target = Path(target_spm).expanduser().absolute()
    # This metadata is used only to prove optional GUID/slot repairs.  A
    # Provider disagreement must disable the disputed metadata-guided repair,
    # not abort the otherwise exact normalization operation.
    resolution = resolve_atlas_manifests(
        target,
        require_generator_complete=True,
        diagnostic_only=True,
    )
    if resolution.get("mutation_authorized") is False:
        return {}
    return selected_manifest_payload(resolution)


def _source_binding_repairs(target_spm, material_record):
    """Return only exact GUID/slot repairs proven by authored backups.

    This does not infer from a Generator name or index. A repair is emitted
    only when the live Generator has a stable GUID, its Mesh is not an authored
    cutout (or is the legacy ``-9`` sentinel), and every authoritative backup
    containing that exact GUID/material/slot agrees on one authored cutout or
    ``-10``. Backups that still carry the current Atlas-managed target Mesh are
    ignored when the live target manifest proves that relationship.
    """
    target = Path(target_spm).expanduser().absolute()
    root = _read_spm_root(target)
    material_name = material_record["material_name"]
    material_id = material_record["material_id"]
    material_mesh_ids = set(material_record.get("mesh_ids") or [])
    atlas_manifest = _atlas_target_relation_manifest(target)
    atlas_bindings = {}
    ambiguous_binding_keys = set()
    for item in (
        atlas_manifest.get("generator_connection") or {}
    ).get("bindings") or []:
        if not isinstance(item, dict):
            continue
        key = (
            generator_guid_key(item.get("generator_guid")),
            str(item.get("slot_prefix") or "").strip(),
        )
        if not all(key) or key in ambiguous_binding_keys:
            continue
        previous = atlas_bindings.get(key)
        if previous is not None and previous != item:
            # Conflicting metadata cannot authorize a binding rewrite.  Drop
            # only this key; the live SPM and other exact repairs remain valid.
            atlas_bindings.pop(key, None)
            ambiguous_binding_keys.add(key)
            continue
        atlas_bindings[key] = item
    live_pairs = [
        pair
        for pair in _generator_property_pairs(root)
        if (
            pair["material_id"] == material_id
            and (
                pair["mesh_id"] == -9
                or (
                    pair["mesh_id"] != -10
                    and pair["mesh_id"] not in material_mesh_ids
                )
            )
            and pair["generator_guid"]
        )
    ]
    if not live_pairs:
        return []
    candidates = _authoritative_binding_backup_paths(target)
    repairs = []
    for pair in live_pairs:
        atlas_binding = atlas_bindings.get(
            (
                generator_guid_key(pair["generator_guid"]),
                pair["slot_prefix"],
            )
        )
        if (
            atlas_binding is not None
            and _integer_text(atlas_binding.get("source_mesh_id"))
            is not None
        ):
            # Relationship removal already owns this exact restoration.
            continue
        managed_target_mesh_id = (
            _integer_text(atlas_binding.get("target_mesh_id"))
            if atlas_binding is not None
            else None
        )
        evidence_by_value = {}
        conflicting_values = []
        for backup in candidates:
            try:
                backup_root = _read_spm_root(backup)
            except Exception:
                continue
            backup_materials = [
                node
                for node in backup_root.findall(".//Material_v8")
                if str(node.get("Name") or "") == material_name
            ]
            if len(backup_materials) != 1:
                continue
            backup_material = backup_materials[0]
            backup_material_id = _integer_text(
                backup_material.get("ID")
            )
            if backup_material_id is None:
                continue
            matches = [
                row
                for row in _generator_property_pairs(backup_root)
                if (
                    generator_guid_key(row["generator_guid"])
                    == generator_guid_key(pair["generator_guid"])
                    and row["generator_type"].casefold().replace(" ", "")
                    == pair["generator_type"].casefold().replace(" ", "")
                    and row["slot_prefix"] == pair["slot_prefix"]
                    and row["material_id"] == backup_material_id
                )
            ]
            if len(matches) != 1:
                continue
            backup_mesh_id = matches[0]["mesh_id"]
            if (
                managed_target_mesh_id == pair["mesh_id"]
                and backup_mesh_id == managed_target_mesh_id
            ):
                continue
            backup_cutouts = set(_material_mesh_ids(backup_material))
            if (
                backup_mesh_id != -10
                and backup_mesh_id not in backup_cutouts
            ):
                conflicting_values.append(
                    {
                        "path": str(backup),
                        "mesh_id": backup_mesh_id,
                    }
                )
                continue
            evidence_by_value.setdefault(backup_mesh_id, []).append(
                {
                    "path": str(backup),
                    "sha256": _sha256_file(backup),
                }
            )
        if (
            len(evidence_by_value) == 1
            and not conflicting_values
        ):
            to_mesh_id, evidence = next(
                iter(evidence_by_value.items())
            )
            if to_mesh_id == pair["mesh_id"]:
                continue
            repairs.append(
                {
                    "generator_name": pair["generator_name"],
                    "generator_guid": pair["generator_guid"],
                    "generator_type": pair["generator_type"],
                    "slot_prefix": pair["slot_prefix"],
                    "source_material_name": material_name,
                    "source_material_id": material_id,
                    "from_mesh_id": pair["mesh_id"],
                    "to_mesh_id": to_mesh_id,
                    "evidence": evidence,
                }
            )
    return repairs


def _provider_authored_source_material(
    target_spm,
    root,
    output_record,
    provider_blend,
):
    """Recover an exact source material behind a live generated output.

    A refreshed Cluster output can already be live on many Generator slots.
    In that state the output material is not its own authored source: the
    provider's per-target receipt preserves the original source material and
    exact GUID/slot handoff.  Use that lineage only when it seals the complete
    current live slice and both endpoint materials still exist exactly.
    """
    if not provider_blend:
        return None
    resolution = resolve_atlas_manifests(
        target_spm,
        expected_blend=provider_blend,
        diagnostic_only=True,
    )
    if resolution.get("mutation_authorized") is False:
        return None
    payload = selected_manifest_payload(resolution)
    if not payload:
        return None
    output_id = output_record["material_id"]
    output_mesh_ids = set(output_record.get("mesh_ids") or [])
    groups = [
        group
        for group in payload.get("speedtree_material_groups") or []
        if isinstance(group, dict)
        and _integer_text(group.get("material_id")) == output_id
        and set(
            value
            for value in (
                _integer_text(item)
                for item in group.get("mesh_ids") or []
            )
            if value is not None
        )
        == output_mesh_ids
    ]
    if len(groups) != 1:
        return None
    connection = payload.get("generator_connection") or {}
    authored = connection.get("authored_bindings")
    if authored is None:
        authored = connection.get("bindings")
    authored = [item for item in authored or [] if isinstance(item, dict)]
    if not authored:
        return None
    authored_rows = [
        item
        for item in authored
        if _integer_text(item.get("target_material_id")) == output_id
        and _integer_text(item.get("target_mesh_id")) in output_mesh_ids
    ]
    if len(authored_rows) != len(authored):
        return None
    live_rows = [
        pair
        for pair in _generator_property_pairs(root)
        if pair["material_id"] == output_id
        and pair["mesh_id"] in output_mesh_ids
        and pair["generator_guid"]
    ]
    live_keys = {
        (
            generator_guid_key(pair["generator_guid"]),
            pair["slot_prefix"],
            pair["material_id"],
            pair["mesh_id"],
        )
        for pair in live_rows
    }
    authored_keys = {
        (
            generator_guid_key(item.get("generator_guid")),
            str(item.get("slot_prefix") or "").strip(),
            _integer_text(item.get("target_material_id")),
            _integer_text(item.get("target_mesh_id")),
        )
        for item in authored_rows
    }
    if not live_keys or live_keys != authored_keys:
        return None
    sources = {
        (
            _integer_text(item.get("source_material_id")),
            str(item.get("source_material_name") or "").strip(),
        )
        for item in authored_rows
    }
    if len(sources) != 1:
        return None
    source_id, source_name = next(iter(sources))
    if source_id is None or not source_name:
        return None
    source_nodes = [
        node
        for node in root.findall(".//Material_v8")
        if _integer_text(node.get("ID")) == source_id
        and str(node.get("Name") or "") == source_name
    ]
    if len(source_nodes) != 1:
        return None
    source_record = _material_record(source_nodes[0], target_spm)
    source_mesh_ids = set(source_record.get("mesh_ids") or [])
    if any(
        _integer_text(item.get("source_mesh_id")) not in (
            source_mesh_ids | {-10}
        )
        for item in authored_rows
    ):
        return None
    return {
        "source": source_record,
        "manifest": str(
            (resolution.get("selected") or [{}])[0].get("path") or ""
        ),
        "binding_count": len(authored_rows),
    }


def _resolve_target_role_material(
    target_spm,
    generated_material_name,
    *,
    provider_blend=None,
):
    """Resolve overwrite/adoption or a compatible source for material creation."""
    root = _read_spm_root(target_spm)
    materials = list(root.findall(".//Material_v8"))
    referenced_ids = _referenced_material_ids(root)
    exact = [
        node
        for node in materials
        if str(node.get("Name") or "").casefold()
        == generated_material_name.casefold()
    ]
    legacy_managed_duplicate = None
    if len(exact) > 1:
        referenced_exact = [
            node
            for node in exact
            if _integer_text(node.get("ID")) in referenced_ids
        ]
        managed_unreferenced = [
            node
            for node in exact
            if _integer_text(node.get("ID")) not in referenced_ids
            and _atlas_managed_material_scope(node)
        ]
        # Older Atlas runs could inject a separate, unconnected output using
        # the same name as the authored Tree material.  That state is
        # unambiguous when exactly one material is live on a Generator and the
        # only other material carries an explicit Atlas ownership marker.  The
        # Atlas adoption transaction can then absorb the managed output into
        # the connected source material without a per-asset exception.
        if (
            len(exact) == 2
            and len(referenced_exact) == 1
            and len(managed_unreferenced) == 1
        ):
            exact = referenced_exact
            managed = managed_unreferenced[0]
            legacy_managed_duplicate = {
                "material_id": _integer_text(managed.get("ID")),
                "material_name": str(managed.get("Name") or ""),
                "atlas_scope": _atlas_managed_material_scope(managed),
                "resolution": "migrate_managed_output_into_connected_source",
            }
        else:
            duplicate_ids = [
                _integer_text(node.get("ID"))
                for node in exact
            ]
            raise ClusterNormalizationSyncError(
                "Automatic Cluster normalization found ambiguous duplicate "
                f"output materials {generated_material_name!r} "
                f"(IDs {duplicate_ids}): {target_spm}"
            )
    if exact:
        record = _material_record(exact[0], target_spm)
        connect_generators = record["material_id"] in referenced_ids
        authored_lineage = (
            _provider_authored_source_material(
                target_spm,
                root,
                record,
                provider_blend,
            )
            if connect_generators
            else None
        )
        source = (
            authored_lineage["source"]
            if authored_lineage is not None
            else record
        )
        same_material_adoption = (
            authored_lineage is not None
            and source["material_id"] == record["material_id"]
            and source["material_name"] == record["material_name"]
        )
        resolved = {
            "target_spm": str(Path(target_spm).expanduser().absolute()),
            # SpeedTree name matching above is intentionally case-insensitive,
            # but Atlas in-place adoption is exact.  Once an existing output
            # material wins, its authored spelling is authoritative for both
            # the normalized Blender material and the target-local update.
            "generated_material_name": record["material_name"],
            "source_material_name": source["material_name"],
            "source_material_id": source["material_id"],
            "adopt_source_material": (
                connect_generators
                and (
                    authored_lineage is None
                    or same_material_adoption
                )
            ),
            "connect_generators": connect_generators,
            "resolution": (
                "refresh_connected_output_from_authored_lineage"
                if (
                    authored_lineage is not None
                    and not same_material_adoption
                )
                else "overwrite_connected_output_material"
                if connect_generators
                else "update_output_assets_only"
            ),
            "generator_variant_policy": "ensure_all_material_cutouts",
            "source_binding_repairs": (
                _source_binding_repairs(target_spm, record)
                if connect_generators
                else []
            ),
        }
        if legacy_managed_duplicate is not None:
            resolved["legacy_managed_duplicate"] = (
                legacy_managed_duplicate
            )
        if authored_lineage is not None:
            resolved["authored_source_lineage"] = authored_lineage
        return resolved

    family = _material_family_name(generated_material_name)
    family_candidates = [
        _material_record(node, target_spm)
        for node in materials
        if family
        and _material_family_name(node.get("Name")) == family
    ]
    referenced_candidates = [
        row
        for row in family_candidates
        if row["material_id"] in referenced_ids
    ]
    candidates = referenced_candidates or family_candidates
    source = (
        candidates[0]
        if len(candidates) == 1
        else {
            "material_name": generated_material_name,
            "material_id": 0,
        }
    )
    return {
        "target_spm": str(Path(target_spm).expanduser().absolute()),
        "generated_material_name": generated_material_name,
        "source_material_name": source["material_name"],
        "source_material_id": source["material_id"],
        "adopt_source_material": False,
        "connect_generators": False,
        "resolution": "create_output_assets_only",
        "generator_variant_policy": "ensure_all_material_cutouts",
        "source_binding_repairs": [],
    }


def _validate_unit_probe(path):
    candidate = Path(path or "").expanduser().absolute()
    payload = _read_json(candidate)
    if (
        not candidate.is_file()
        or not payload
        or payload.get("kind") != "speedtree_fbx_spm_unit_probe"
        or str(payload.get("status") or "").casefold() != "verified"
    ):
        raise ClusterNormalizationSyncError(
            "Verified common SpeedTree unit-probe receipt is missing or invalid: "
            f"{candidate}"
        )
    selected = payload.get("selected") or {}
    if (
        float(payload.get("physical_target_meters") or 0.0) != 0.1
        or float(selected.get("mesh_geometry_scale") or 0.0) <= 0.0
        or float(selected.get("mesh_asset_scale") or 0.0) <= 0.0
    ):
        raise ClusterNormalizationSyncError(
            "Cluster unit-probe receipt does not contain the verified 0.1m scale "
            f"contract: {candidate}"
        )
    return candidate


def _assembly_report(blend, canonical_spm):
    blend = Path(blend).expanduser().absolute()
    canonical_spm = Path(canonical_spm).expanduser().absolute()
    report_path = (
        blend.parent
        / "reports"
        / f"{blend.stem}_speedtree_assembly_pipeline_report_codex.json"
    )
    report = _read_json(report_path)
    if not report or str(report.get("status") or "").casefold() != "done":
        reason = "missing_or_incomplete_report"
        raise ClusterSourceBuildRequiredError(
            "Current Cluster source-build completion report is missing: "
            f"{report_path}",
            blend=blend,
            canonical_spm=canonical_spm,
            report_path=report_path,
            reason=reason,
        )
    handoff_status = str(
        (report.get("handoff_preflight") or {}).get("status") or ""
    ).casefold()
    if handoff_status and handoff_status not in {
        "ok",
        "source_review",
        "cluster_export_pending",
    }:
        reason = "source_handoff_blocked"
        raise ClusterSourceBuildRequiredError(
            "Cluster source-build report is not eligible for Normalizer reuse "
            f"(handoff={handoff_status or 'missing'}): {report_path}",
            blend=blend,
            canonical_spm=canonical_spm,
            report_path=report_path,
            reason=reason,
        )
    if handoff_status == "cluster_export_pending":
        source_build = report.get("cluster_source_build_contract") or {}
        if (
            source_build.get("status") != "ready"
            or source_build.get("mode")
            != "raw_source_for_cluster_normalizer"
            or not source_build.get("final_export_required")
            or not source_build.get("deferred_export_issues")
            or source_build.get("source_blend_committed") is not True
            or source_build.get("source_object")
            != str((report.get("paths") or {}).get("merged_name") or "")
        ):
            reason = "cluster_source_build_contract_invalid"
            raise ClusterSourceBuildRequiredError(
                "Cluster source-build report deferred Export without a valid "
                f"Normalizer handoff contract: {report_path}",
                blend=blend,
                canonical_spm=canonical_spm,
                report_path=report_path,
                reason=reason,
            )
    identity = (report.get("speedtree_live_source_identity") or {}).get("spm") or {}
    reported_path = Path(
        str(identity.get("canonical_path") or canonical_spm)
    ).expanduser().absolute()
    current_hash = _sha256_file(canonical_spm)
    try:
        current_semantic = spm_file_structural_semantic_fingerprint(
            canonical_spm,
            raw_sha256=current_hash,
        )
    except (OSError, ValueError, ET.ParseError) as exc:
        reason = "source_semantic_unavailable"
        raise ClusterSourceBuildRequiredError(
            "Cluster SPM structural semantic fingerprint is unavailable. "
            f"Rebuild the Cluster source blend first: {canonical_spm}",
            blend=blend,
            canonical_spm=canonical_spm,
            report_path=report_path,
            reason=reason,
        ) from exc
    reported_hash = str(identity.get("sha256") or "").casefold()
    reported_semantic = str(
        identity.get("source_spm_semantic_fingerprint")
        or identity.get("structural_semantic_fingerprint")
        or identity.get("bone_semantic_fingerprint")
        or identity.get("semantic_fingerprint")
        or ""
    ).casefold()
    reported_projection = (
        identity.get("source_spm_semantic_projection_version")
        if identity.get("source_spm_semantic_projection_version") is not None
        else identity.get("structural_semantic_projection_version")
        if identity.get("structural_semantic_projection_version") is not None
        else identity.get("bone_semantic_projection_version")
        if identity.get("bone_semantic_projection_version") is not None
        else identity.get("semantic_projection_version")
    )
    identity_current = (
        reported_path == Path(canonical_spm).expanduser().absolute()
    )
    if reported_semantic:
        identity_current = bool(
            identity_current
            and reported_projection
            == SPM_STRUCTURAL_SEMANTIC_PROJECTION_VERSION
            and reported_semantic == current_semantic.casefold()
        )
    elif reported_hash == current_hash.casefold():
        identity_current = bool(identity_current)
    else:
        identity_current = bool(
            identity_current
            and prove_legacy_texture_normalize_semantic_migration(
                canonical_spm,
                reported_hash,
            )
        )
    if not identity_current:
        reason = "source_identity_stale"
        raise ClusterSourceBuildRequiredError(
            "Cluster blend/report is stale for the current Cluster SPM. "
            f"Rebuild the Cluster source blend first: {canonical_spm}",
            blend=blend,
            canonical_spm=canonical_spm,
            report_path=report_path,
            reason=reason,
        )
    merged_name = str((report.get("paths") or {}).get("merged_name") or "").strip()
    if not merged_name:
        reason = "merged_source_missing"
        raise ClusterSourceBuildRequiredError(
            f"Cluster source-build report has no merged render object: {report_path}",
            blend=blend,
            canonical_spm=canonical_spm,
            report_path=report_path,
            reason=reason,
        )
    return (
        report_path,
        report,
        current_hash,
        current_semantic,
        merged_name,
    )


def _assembly_material_assignment_identity(report):
    """Return stable evidence for the materials assigned to the Assembly source.

    The raw Cluster FBX can stay byte-identical while a Assembly repair changes the
    material datablocks used by the merged source.  Normalized prototypes copy
    those datablocks, so the final Assembly material intent is a physical
    Normalizer dependency even though texture-only edits to the saved Atlas
    plan collection are not.
    """
    intents = report.get("speedtree_material_intents") or {}
    materials = []
    for row in intents.get("materials") or []:
        if not isinstance(row, dict):
            continue
        materials.append({
            "material_name": str(row.get("material_name") or ""),
            "material_key": str(row.get("material_key") or ""),
            "material_instance_base": str(
                row.get("material_instance_base") or ""
            ),
            "tree_part": str(row.get("tree_part") or ""),
            "tree_shading": str(row.get("tree_shading") or ""),
            "match_mode": str(row.get("match_mode") or ""),
            "source_materials": sorted(
                str(value)
                for value in row.get("source_materials") or []
                if str(value).strip()
            ),
        })
    materials.sort(
        key=lambda row: (
            row["material_name"].casefold(),
            row["material_key"].casefold(),
        )
    )
    consolidation = (
        (report.get("import") or {}).get("material_consolidation") or {}
    )
    production_groups = []
    for row in consolidation.get("groups") or []:
        if (
            not isinstance(row, dict)
            or row.get("mode") != "production_group_suffix"
        ):
            continue
        production_groups.append({
            "target_material": str(row.get("target_material") or ""),
            "source_materials": sorted(
                str(value)
                for value in row.get("source_materials") or []
                if str(value).strip()
            ),
            "group_tokens": sorted(
                str(value)
                for value in row.get("group_tokens") or []
                if str(value).strip()
            ),
            "provenance_type": str(row.get("provenance_type") or ""),
            "readiness_mode": str(row.get("readiness_mode") or ""),
        })
    production_groups.sort(
        key=lambda row: (
            row["target_material"].casefold(),
            tuple(value.casefold() for value in row["source_materials"]),
        )
    )
    return {
        "intent_status": str(intents.get("status") or ""),
        "materials": materials,
        "unmatched_materials": sorted(
            str(value)
            for value in intents.get("unmatched_materials") or []
            if str(value).strip()
        ),
        "production_groups": production_groups,
    }


def inspect_assembly_material_assignment_freshness(blend):
    """Compare the latest Assembly material assignment with the Normalizer receipt."""
    blend = Path(blend).expanduser().absolute()
    report_path = (
        blend.parent
        / "reports"
        / f"{blend.stem}_speedtree_assembly_pipeline_report_codex.json"
    )
    report = _read_json(report_path)
    if not report or str(report.get("status") or "").casefold() != "done":
        return {
            "status": "not_available",
            "current": True,
            "required": False,
            "report": str(report_path),
            "receipt": str(normalization_receipt_path(blend)),
            "refresh_reasons": [],
        }
    identity = _assembly_material_assignment_identity(report)
    required = bool(
        identity["materials"]
        or identity["unmatched_materials"]
        or identity["production_groups"]
    )
    expected = _canonical_sha256(identity)
    receipt_path = normalization_receipt_path(blend)
    receipt = _read_json(receipt_path)
    recorded = str(
        (receipt or {}).get("assembly_material_assignment_sha256") or ""
    ).casefold()
    current = bool(not required or recorded == expected.casefold())
    return {
        "status": "current" if current else "refresh_required",
        "current": current,
        "required": required,
        "report": str(report_path),
        "receipt": str(receipt_path),
        "expected_sha256": expected,
        "recorded_sha256": recorded,
        "refresh_reasons": (
            [] if current else ["assembly_material_assignment_changed"]
        ),
    }


def _receipt_is_current(recipe):
    receipt = _read_json(recipe["receipt_path"])
    if (
        not receipt
        or receipt.get("kind") != "speedtree_cluster_sync_normalization"
        or receipt.get("status") != "ready"
        or receipt.get("unit_probe_sha256") != recipe["unit_probe_sha256"]
    ):
        return False
    build = receipt.get("build") or {}
    if (
        recipe.get("assembly_material_assignment_required")
        and receipt.get("assembly_material_assignment_sha256")
        != recipe.get("assembly_material_assignment_sha256")
    ):
        return False
    current_source_fbx = recipe.get("source_fbx_identity")
    if isinstance(current_source_fbx, dict):
        recorded_source_fbx = build.get("source_3d_contract") or {}
        if (
            _normalized_path(recorded_source_fbx.get("source_fbx") or "")
            != _normalized_path(current_source_fbx.get("path") or "")
            or str(
                recorded_source_fbx.get("source_fbx_sha256") or ""
            ).casefold()
            != str(current_source_fbx.get("sha256") or "").casefold()
        ):
            # The physical plan receipt is derived from this exact FBX.  Assembly can
            # legitimately rewrite it while preserving the SPM semantic graph
            # (for example by removing a zero-face object).  Reusing the older
            # receipt in that case publishes a brand-new target manifest whose
            # embedded source hash is already stale.  Invalidate only this
            # changed source generation so the existing Normalizer run rebuilds
            # it once; an unchanged FBX continues to use the current receipt.
            return False
    recorded_semantic = str(
        receipt.get("source_spm_semantic_fingerprint") or ""
    ).casefold()
    if recorded_semantic:
        if (
            receipt.get("source_spm_semantic_projection_version")
            != SPM_STRUCTURAL_SEMANTIC_PROJECTION_VERSION
            or recorded_semantic
            != str(
                recipe.get("source_spm_semantic_fingerprint") or ""
            ).casefold()
        ):
            return False
    elif (
        receipt.get("source_spm_sha256")
        != recipe["source_spm_sha256"]
        and prove_legacy_texture_normalize_semantic_migration(
            recipe["canonical_spm"],
            receipt.get("source_spm_sha256"),
        )
        is None
    ):
        return False
    # The Normalizer dependency is the canonical Cluster SPM/XML/capture
    # contract below, not every subsequent edit to the Atlas plan collection
    # saved in the same blend.  Atlas source freshness is validated separately
    # by ``inspect_normalization_source_identity`` before an already-ON no-op.
    capture_manifest = (
        Path(recipe["capture_output_dir"]).expanduser().absolute()
        / f"{recipe['capture_prefix']}_auto_capture_manifest.json"
    )
    try:
        recorded_capture_manifest = (
            Path(receipt.get("capture_manifest") or "").expanduser().absolute()
        )
    except (OSError, TypeError, ValueError):
        return False
    if (
        not capture_manifest.is_file()
        or recorded_capture_manifest != capture_manifest
        or receipt.get("capture_manifest_sha256")
        != _sha256_file(capture_manifest)
    ):
        return False
    recorded_contract_hash = (
        receipt.get("normalization_contract_sha256")
        or receipt.get("recipe_sha256")
    )
    if recorded_contract_hash == recipe["normalization_contract_sha256"]:
        return True

    # Isolated bark recipes are sealed to an exact provider filename, cache
    # manifest, XML SourceTree, and SPM hash. They have no safe legacy hash
    # migration path; any contract mismatch requires a fresh normalization.
    if recipe.get("isolated_bark_bundle") is not None:
        return False

    # Version-1 receipts included the mutable owner-target list in their recipe
    # hash. Adding another ON target therefore forced an unrelated Blender
    # recapture/rebuild even though the canonical Cluster SPM, XML, merged
    # source geometry, and capture contract were unchanged. Accept those
    # receipts only when their embedded build evidence proves every actual
    # normalization dependency still matches.
    source_contract = build.get("source_3d_contract") or {}
    capture_contract = build.get("physical_capture_contract") or {}
    capture_frame = capture_contract.get("frame") or {}
    capture_resolution = capture_contract.get("capture_resolution") or []
    target_meters = capture_frame.get("target_meters")
    if isinstance(target_meters, (list, tuple)):
        target_meters = target_meters[0] if target_meters else None

    def same_path(first, second):
        try:
            return (
                Path(first).expanduser().absolute()
                == Path(second).expanduser().absolute()
            )
        except (OSError, TypeError, ValueError):
            return False

    def same_float(first, second, tolerance=1.0e-7):
        try:
            return abs(float(first) - float(second)) <= tolerance
        except (TypeError, ValueError):
            return False

    return bool(
        same_path(receipt.get("canonical_spm"), recipe["canonical_spm"])
        and same_path(receipt.get("source_xml"), recipe["source_xml"])
        and source_contract.get("xml_sha256") == recipe["source_xml_sha256"]
        and build.get("workflow_mode") == "PHYSICAL_DIRECT_CAPTURE"
        and build.get("source_object") == recipe["source_object"]
        and build.get("source_partition_mode") == recipe["source_partition_mode"]
        and build.get("plan_collection") == recipe["plan_collection"]
        and receipt.get("material") == recipe["material_name"]
        and int(build.get("plan_refinement_levels", -1))
        == int(recipe["plan_refinement_levels"])
        and capture_contract.get("workflow_mode") == "PHYSICAL_DIRECT_CAPTURE"
        and capture_frame.get("plane") == recipe["capture_plane"]
        and same_float(
            capture_frame.get("padding_ratio"),
            recipe["capture_padding_ratio"],
        )
        and same_float(target_meters, recipe["capture_target_meters"])
        and len(capture_resolution) == 2
        and int(capture_resolution[0]) == int(recipe["capture_resolution"])
        and int(capture_resolution[1]) == int(recipe["capture_resolution"])
    )


def resolve_normalization_recipe(
    blend,
    target_spms,
    *,
    canonical_spm=None,
    unit_probe_path,
    capture_resolution=1024,
    delivery_scope_intents=None,
):
    """Build a fail-closed Blender recipe from current Assembly and SPM evidence."""
    blend = Path(blend).expanduser().absolute()
    canonical = Path(canonical_spm or blend.with_suffix(".spm")).expanduser().absolute()
    targets = [Path(path).expanduser().absolute() for path in target_spms]
    if not canonical.is_file():
        raise ClusterNormalizationSyncError(
            f"Canonical Cluster SPM is missing: {canonical}"
        )
    if not targets:
        raise ClusterNormalizationSyncError(
            "Automatic Cluster normalization has no owner target SPM."
        )
    if any(not target.is_file() for target in targets):
        missing = [str(target) for target in targets if not target.is_file()]
        raise ClusterNormalizationSyncError(
            "Automatic Cluster normalization target is missing: "
            + ", ".join(missing)
        )
    unit_probe = _validate_unit_probe(unit_probe_path)
    report_path = (
        blend.parent
        / "reports"
        / f"{blend.stem}_speedtree_assembly_pipeline_report_codex.json"
    )
    if not blend.is_file():
        raise ClusterSourceBuildRequiredError(
            f"Canonical Cluster source blend is missing: {blend}",
            blend=blend,
            canonical_spm=canonical,
            report_path=report_path,
            reason="blend_missing",
        )
    (
        report_path,
        _report,
        source_hash,
        source_semantic,
        merged_name,
    ) = _assembly_report(
        blend, canonical
    )
    source_xml = blend.parent / "xml" / f"{blend.stem}.xml"
    if not source_xml.is_file():
        raise ClusterSourceBuildRequiredError(
            f"Cluster source XML is missing: {source_xml}",
            blend=blend,
            canonical_spm=canonical,
            report_path=report_path,
            reason="source_xml_missing",
        )
    isolated_bark_bundle = _isolated_bark_bundle_from_report(
        _report,
        source_xml,
        canonical,
    )
    role = cluster_role_contract(blend)
    target_material_bindings = [
        _resolve_target_role_material(
            target,
            role["material_name"],
            provider_blend=blend,
        )
        for target in targets
    ]
    if delivery_scope_intents is not None:
        if not isinstance(delivery_scope_intents, dict):
            raise ClusterNormalizationSyncError(
                "Generator delivery scope intents must be keyed by target SPM."
            )
        supplied_by_target = {
            str(Path(path).expanduser().absolute()).casefold(): intent
            for path, intent in delivery_scope_intents.items()
        }
        if len(supplied_by_target) != len(delivery_scope_intents):
            raise ClusterNormalizationSyncError(
                "Generator delivery scope intents contain duplicate target paths."
            )
        connected_targets = {
            str(Path(row["target_spm"]).expanduser().absolute()).casefold()
            for row in target_material_bindings
            if row.get("connect_generators") is True
        }
        if set(supplied_by_target) != connected_targets:
            raise ClusterNormalizationSyncError(
                "Explicit Generator delivery scope must cover exactly every "
                "Generator-connected target in the recipe."
            )
        for binding in target_material_bindings:
            if binding.get("connect_generators") is not True:
                continue
            target_key = str(
                Path(binding["target_spm"]).expanduser().absolute()
            ).casefold()
            intent = supplied_by_target[target_key]
            try:
                validate_delivery_scope_intent(
                    intent,
                    target_spm=binding["target_spm"],
                    material_id=binding["source_material_id"],
                    provider_blend=blend,
                )
            except GeneratorDeliveryScopeError as exc:
                raise ClusterNormalizationSyncError(
                    "Explicit Generator delivery scope is invalid for "
                    f"{binding['target_spm']}: {exc}"
                ) from exc
            # Target-local delivery intent stays outside the physical Blender
            # normalization hash.  It is passed through verbatim and sealed by
            # the SPM producer after the write; this caller never derives it
            # from current live Generator evidence.
            binding["generator_delivery_scope_intent"] = copy.deepcopy(intent)
    first_binding = target_material_bindings[0]
    role = {
        **role,
        "material_name": first_binding["generated_material_name"],
    }
    try:
        resolution = int(capture_resolution)
    except (TypeError, ValueError) as exc:
        raise ClusterNormalizationSyncError(
            "Cluster capture resolution must be an integer."
        ) from exc
    if resolution < 256 or resolution > 8192:
        raise ClusterNormalizationSyncError(
            "Cluster capture resolution must be between 256 and 8192."
        )

    receipt = normalization_receipt_path(blend)
    assembly_material_assignment = _assembly_material_assignment_identity(_report)
    assembly_material_assignment_required = bool(
        assembly_material_assignment["materials"]
        or assembly_material_assignment["unmatched_materials"]
        or assembly_material_assignment["production_groups"]
    )
    assembly_material_assignment_sha256 = _canonical_sha256(
        assembly_material_assignment
    )
    bwr_semantic_identity = {
        "status": str(_report.get("status") or ""),
        "source_spm_semantic_projection_version":
            SPM_STRUCTURAL_SEMANTIC_PROJECTION_VERSION,
        "source_spm_semantic_fingerprint": source_semantic,
        "merged_name": merged_name,
        "handoff_preflight_status": str(
            (_report.get("handoff_preflight") or {}).get("status") or ""
        ),
        "material_assignment": assembly_material_assignment,
    }
    normalization_contract = {
        "version": 6,
        "blend": str(blend),
        "canonical_spm": str(canonical),
        "source_spm_semantic_projection_version":
            SPM_STRUCTURAL_SEMANTIC_PROJECTION_VERSION,
        "source_spm_semantic_fingerprint": source_semantic,
        "bwr_report": str(report_path),
        "bwr_semantic_sha256": _canonical_sha256(
            bwr_semantic_identity
        ),
        "assembly_material_assignment_sha256": (
            assembly_material_assignment_sha256
        ),
        "assembly_material_assignment_required": (
            assembly_material_assignment_required
        ),
        "source_object": merged_name,
        "source_xml": str(source_xml),
        "source_xml_sha256": _sha256_file(source_xml),
        "material_name": role["material_name"],
        "unit_probe": str(unit_probe),
        "unit_probe_sha256": _sha256_file(unit_probe),
        "capture_output_dir": str(blend.parent),
        "capture_prefix": role["plan_base"],
        "capture_resolution": resolution,
        "capture_source_collection": "SpeedTree_Source",
        "capture_padding_ratio": 0.04,
        "capture_target_meters": 0.1,
        "source_partition_mode": "PER_CONNECTED_DEFORM_CLUSTER",
        "plan_margin_ratio": 0.01,
        "plan_refinement_levels": 1,
        **role,
    }
    source_fbx = blend.parent / "fbx" / f"{blend.stem}.fbx"
    if source_fbx.is_file():
        normalization_contract["source_fbx_identity"] = {
            "path": str(source_fbx),
            "sha256": _sha256_file(source_fbx),
        }
    if isolated_bark_bundle is not None:
        normalization_contract["isolated_bark_bundle"] = (
            isolated_bark_bundle
        )
    normalization_contract_hash = _canonical_sha256(normalization_contract)
    recipe = {
        "kind": "speedtree_cluster_sync_normalization_recipe",
        **normalization_contract,
        # Byte identity is retained for diagnostics and provenance, but it is
        # deliberately outside the authoritative normalization hash.
        "source_spm_sha256": source_hash,
        # Owner targets affect only Atlas material/mesh insertion and optional
        # Generator wiring. They are deliberately outside the Blender
        # normalization contract.
        "target_spms": [str(target) for target in targets],
        "first_target_spm": str(targets[0]),
        "normalization_contract_sha256": normalization_contract_hash,
        "recipe_sha256": normalization_contract_hash,
        "receipt_path": str(receipt),
        # Target-local source IDs are intentionally kept outside recipe_contract
        # so an Atlas target rewrite does not force an otherwise unnecessary
        # Blender normalization rebuild.
        "target_material_bindings": target_material_bindings,
        "source_material_name": first_binding["source_material_name"],
        "source_material_id": first_binding["source_material_id"],
        "adopt_source_material": first_binding["adopt_source_material"],
    }
    recipe["normalization_required"] = not _receipt_is_current(recipe)
    return recipe


__all__ = [
    "ClusterNormalizationSyncError",
    "ClusterSourceBuildRequiredError",
    "inspect_assembly_material_assignment_freshness",
    "inspect_normalization_source_identity",
    "normalization_receipt_path",
    "resolve_normalization_recipe",
    "validate_isolated_bark_recipe_bundle",
]
