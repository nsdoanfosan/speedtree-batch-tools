"""Fail-closed checks for rendered provider geometry in ``NA_Base``.

The Assembly role list is an output of upstream reconciliation, so it cannot
also be the authority for deciding whether the generated Base is clean.  This
module derives the expected provider-material inventory from the PCG receipt
and intersects it with the materials that the actual target SPM renders.
"""
from __future__ import annotations

import json
from collections import Counter, defaultdict


class NaBaseProviderContaminationError(RuntimeError):
    """Raised when rendered provider polygons remain in the generated Base."""


def material_identity(value):
    """Return the comparison identity shared by SPM and Blender materials."""
    identity = str(value or "").strip().casefold()
    if identity.startswith("m_"):
        identity = identity[2:]
    return identity


def rendered_provider_inventory(receipt, visible_spm_material_names):
    """Build provider inventory independently of selected Assembly roles."""
    cluster_assembly = (receipt or {}).get("cluster_assembly") or {}
    dependencies = list(cluster_assembly.get("dependencies") or [])
    if not dependencies:
        raise NaBaseProviderContaminationError(
            "PCG receipt has no cluster dependency inventory"
        )

    visible_by_identity = {}
    for name in visible_spm_material_names or []:
        identity = material_identity(name)
        if identity:
            visible_by_identity.setdefault(identity, str(name))
    if not visible_by_identity:
        raise NaBaseProviderContaminationError(
            "target SPM has no visible material inventory"
        )

    providers_by_material = defaultdict(list)
    for dependency in dependencies:
        role = str(dependency.get("role") or "").strip().casefold()
        if not role:
            continue
        provider = str(dependency.get("name") or "").strip()
        for target_material_name in (
            dependency.get("target_material_names") or []
        ):
            identity = material_identity(target_material_name)
            actual_material_name = visible_by_identity.get(identity)
            if not actual_material_name:
                continue
            providers_by_material[identity].append({
                "provider": provider,
                "role": role,
                "receipt_target_material_name": str(target_material_name),
            })

    rows = []
    for identity, providers in sorted(providers_by_material.items()):
        unique_providers = {
            (
                row["provider"],
                row["role"],
                row["receipt_target_material_name"],
            ): row
            for row in providers
        }
        rows.append({
            "material_identity": identity,
            "spm_material_name": visible_by_identity[identity],
            "providers": [
                unique_providers[key]
                for key in sorted(unique_providers)
            ],
        })
    if not rows:
        raise NaBaseProviderContaminationError(
            "PCG receipt and target SPM prove no rendered provider materials"
        )
    return rows


def base_material_polygon_counts(base_object):
    """Count Base polygons by normalized Blender material identity."""
    mesh = getattr(base_object, "data", None)
    if mesh is None:
        raise NaBaseProviderContaminationError(
            "generated NA_Base has no mesh data"
        )
    slots = list(getattr(mesh, "materials", None) or [])
    counts = Counter()
    display_names = {}
    for polygon in list(getattr(mesh, "polygons", None) or []):
        material_index = int(getattr(polygon, "material_index", -1))
        if material_index < 0 or material_index >= len(slots):
            continue
        material = slots[material_index]
        material_name = str(getattr(material, "name", "") or "")
        identity = material_identity(material_name)
        if not identity:
            continue
        counts[identity] += 1
        display_names.setdefault(identity, material_name)
    return {
        identity: {
            "material_name": display_names[identity],
            "polygon_count": int(count),
        }
        for identity, count in sorted(counts.items())
    }


def validate_base_provider_contamination(base_object, provider_inventory):
    """Return a clean report or fail with exact residual material counts."""
    counts = base_material_polygon_counts(base_object)
    residuals = []
    for provider_material in provider_inventory or []:
        identity = material_identity(
            provider_material.get("material_identity")
        )
        base_section = counts.get(identity)
        if not base_section or base_section["polygon_count"] <= 0:
            continue
        residuals.append({
            "material_identity": identity,
            "spm_material_name": provider_material.get("spm_material_name"),
            "base_material_name": base_section["material_name"],
            "polygon_count": base_section["polygon_count"],
            "providers": list(provider_material.get("providers") or []),
        })

    report = {
        "contract": "na_base_rendered_provider_contamination_v1",
        "status": "contaminated" if residuals else "clean",
        "rendered_provider_material_count": len(provider_inventory or []),
        "residual_material_count": len(residuals),
        "residual_polygon_count": sum(
            row["polygon_count"] for row in residuals
        ),
        "residuals": residuals,
    }
    if residuals:
        raise NaBaseProviderContaminationError(
            "generated NA_Base retains rendered provider geometry: "
            + json.dumps(report, ensure_ascii=False, sort_keys=True)
        )
    return report
