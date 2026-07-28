"""Resolve and audit automatic Cluster Normalizer inputs for Generator Sync.

The raw ``Cluster/SK_*.blend`` is produced by SK Batch.  Generator Sync owns
the next transition: build the physical-capture prototypes/plans in that same
blend, persist a content-addressed receipt, then let Atlas update the owner
``SK_*.spm`` targets.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from cluster_card_pipeline.contract import _read_spm_root


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


def normalization_receipt_path(blend):
    blend = Path(blend).expanduser().absolute()
    return (
        blend.parent
        / "reports"
        / f"{blend.stem}_cluster_normalization_sync_receipt.json"
    )


def _role_contract(blend):
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
    }


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


def _source_binding_repairs(target_spm, material_record):
    """Return only exact, backup-proven sentinel repairs for one target.

    This does not infer from a Generator name or index. A repair is emitted
    only when the live Generator has a stable GUID, the exact material/slot
    currently contains ``-9``, and every authoritative backup that contains
    that same GUID/material/slot agrees that the authored value was ``-10``.
    """
    target = Path(target_spm).expanduser().absolute()
    root = _read_spm_root(target)
    material_name = material_record["material_name"]
    material_id = material_record["material_id"]
    live_pairs = [
        pair
        for pair in _generator_property_pairs(root)
        if (
            pair["material_id"] == material_id
            and pair["mesh_id"] == -9
            and pair["generator_guid"]
        )
    ]
    if not live_pairs:
        return []
    candidates = _authoritative_binding_backup_paths(target)
    repairs = []
    for pair in live_pairs:
        evidence = []
        conflicting_values = []
        for backup in candidates:
            try:
                backup_root = _read_spm_root(backup)
            except Exception:
                continue
            backup_material_id = _material_id_by_exact_name(
                backup_root, material_name
            )
            if backup_material_id is None:
                continue
            matches = [
                row
                for row in _generator_property_pairs(backup_root)
                if (
                    row["generator_guid"] == pair["generator_guid"]
                    and row["generator_type"].casefold().replace(" ", "")
                    == pair["generator_type"].casefold().replace(" ", "")
                    and row["slot_prefix"] == pair["slot_prefix"]
                    and row["material_id"] == backup_material_id
                )
            ]
            if len(matches) != 1:
                continue
            backup_mesh_id = matches[0]["mesh_id"]
            if backup_mesh_id != -10:
                conflicting_values.append(
                    {
                        "path": str(backup),
                        "mesh_id": backup_mesh_id,
                    }
                )
                continue
            evidence.append(
                {
                    "path": str(backup),
                    "sha256": _sha256_file(backup),
                }
            )
        if evidence and not conflicting_values:
            repairs.append(
                {
                    "generator_name": pair["generator_name"],
                    "generator_guid": pair["generator_guid"],
                    "generator_type": pair["generator_type"],
                    "slot_prefix": pair["slot_prefix"],
                    "source_material_name": material_name,
                    "source_material_id": material_id,
                    "from_mesh_id": -9,
                    "to_mesh_id": -10,
                    "evidence": evidence,
                }
            )
    return repairs


def _resolve_target_role_material(target_spm, generated_material_name):
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
    if len(exact) > 1:
        raise ClusterNormalizationSyncError(
            "Automatic Cluster normalization found duplicate output materials "
            f"{generated_material_name!r}: {target_spm}"
        )
    if exact:
        record = _material_record(exact[0], target_spm)
        connect_generators = record["material_id"] in referenced_ids
        return {
            "target_spm": str(Path(target_spm).expanduser().absolute()),
            # SpeedTree name matching above is intentionally case-insensitive,
            # but Atlas in-place adoption is exact.  Once an existing output
            # material wins, its authored spelling is authoritative for both
            # the normalized Blender material and the target-local update.
            "generated_material_name": record["material_name"],
            "source_material_name": record["material_name"],
            "source_material_id": record["material_id"],
            "adopt_source_material": connect_generators,
            "connect_generators": connect_generators,
            "resolution": (
                "overwrite_connected_output_material"
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


def _bwr_report(blend, canonical_spm):
    blend = Path(blend).expanduser().absolute()
    canonical_spm = Path(canonical_spm).expanduser().absolute()
    report_path = (
        blend.parent
        / "reports"
        / f"{blend.stem}_speedtree_repair_pipeline_report_codex.json"
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
    if (
        reported_path != Path(canonical_spm).expanduser().absolute()
        or str(identity.get("sha256") or "").casefold() != current_hash.casefold()
    ):
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
    return report_path, report, current_hash, merged_name


def _receipt_is_current(recipe):
    receipt = _read_json(recipe["receipt_path"])
    if (
        not receipt
        or receipt.get("kind") != "speedtree_cluster_sync_normalization"
        or receipt.get("status") != "ready"
        or receipt.get("source_spm_sha256") != recipe["source_spm_sha256"]
        or receipt.get("unit_probe_sha256") != recipe["unit_probe_sha256"]
    ):
        return False
    blend = Path(recipe["blend"])
    if (
        not blend.is_file()
        or receipt.get("output_blend_sha256") != _sha256_file(blend)
    ):
        return False
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

    # Version-1 receipts included the mutable owner-target list in their recipe
    # hash. Adding another ON target therefore forced an unrelated Blender
    # recapture/rebuild even though the canonical Cluster SPM, XML, merged
    # source geometry, and capture contract were unchanged. Accept those
    # receipts only when their embedded build evidence proves every actual
    # normalization dependency still matches.
    build = receipt.get("build") or {}
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
):
    """Build a fail-closed Blender recipe from current BWR and SPM evidence."""
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
        / f"{blend.stem}_speedtree_repair_pipeline_report_codex.json"
    )
    if not blend.is_file():
        raise ClusterSourceBuildRequiredError(
            f"Canonical Cluster source blend is missing: {blend}",
            blend=blend,
            canonical_spm=canonical,
            report_path=report_path,
            reason="blend_missing",
        )
    report_path, _report, source_hash, merged_name = _bwr_report(
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
    role = _role_contract(blend)
    target_material_bindings = [
        _resolve_target_role_material(target, role["material_name"])
        for target in targets
    ]
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
    bwr_semantic_identity = {
        "status": str(_report.get("status") or ""),
        "source_spm_sha256": source_hash,
        "merged_name": merged_name,
        "handoff_preflight_status": str(
            (_report.get("handoff_preflight") or {}).get("status") or ""
        ),
    }
    normalization_contract = {
        "version": 3,
        "blend": str(blend),
        "canonical_spm": str(canonical),
        "source_spm_sha256": source_hash,
        "bwr_report": str(report_path),
        "bwr_semantic_sha256": _canonical_sha256(
            bwr_semantic_identity
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
    normalization_contract_hash = _canonical_sha256(normalization_contract)
    recipe = {
        "kind": "speedtree_cluster_sync_normalization_recipe",
        **normalization_contract,
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
    "normalization_receipt_path",
    "resolve_normalization_recipe",
]
