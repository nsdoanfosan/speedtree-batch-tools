"""Pure policy helpers for renderer-sensitive Unreal ingest.

The GUI, manifest writers, and Unreal-side runner all import this module so a
persisted transport preference cannot silently bypass the production guard.
"""

from __future__ import annotations


TRANSPORT_POLICY_VERSION = 1
MAX_HEAVY_ITEMS_PER_PROCESS = 6
PROVIDER_INGEST_WAVE = "provider_part"
ASSEMBLY_INGEST_WAVE = "final_assembly"
LEGACY_EXTERNAL_INGEST_WAVE = "legacy_external"


def bounded_heavy_process_item_limit(value):
    """Allow stricter test/operator limits without disabling the hard ceiling."""
    try:
        requested = int(value)
    except (TypeError, ValueError):
        requested = MAX_HEAVY_ITEMS_PER_PROCESS
    if requested <= 0:
        return MAX_HEAVY_ITEMS_PER_PROCESS
    return min(requested, MAX_HEAVY_ITEMS_PER_PROCESS)


def _asset_data_rows(item):
    for asset in item.get("assets") or []:
        if isinstance(asset, dict):
            yield asset.get("asset_data") or {}
    assembly = item.get("cluster_assembly") or {}
    plan = assembly.get("ingest_plan") or {}
    for asset in plan.get("assets") or []:
        if isinstance(asset, dict):
            yield asset.get("asset_data") or {}


def heavy_ingest_reasons(item):
    """Return stable reasons that require an isolated NullRHI commandlet."""
    if not isinstance(item, dict):
        return []
    reasons = []
    if item.get("mesh_path") or any(
        str(data.get("_asset_type") or "").casefold() == "skeletalmesh"
        for data in _asset_data_rows(item)
    ):
        reasons.append("generated_nanite_skeletal_mesh")
    wind_policy = item.get("wind_policy") or {}
    if bool(wind_policy.get("requires_json")) or item.get("wind_json"):
        reasons.append("dynamic_wind_provider")
    assembly = item.get("cluster_assembly") or {}
    plan = assembly.get("ingest_plan") or {}
    if plan.get("status") == "ready":
        reasons.append("final_nanite_assembly")
    return reasons


def ingest_wave(item):
    """Classify providers/parts before final Assembly consumers."""
    assembly = (item.get("cluster_assembly") or {}) if isinstance(item, dict) else {}
    plan = assembly.get("ingest_plan") or {}
    return (
        ASSEMBLY_INGEST_WAVE
        if plan.get("status") == "ready"
        else PROVIDER_INGEST_WAVE
    )


def manifest_item_policy_metadata(item):
    assembly = (item.get("cluster_assembly") or {}) if isinstance(item, dict) else {}
    plan = assembly.get("ingest_plan") or {}
    asset_contract = plan.get("asset_contract") or {}
    return {
        "ingest_wave": ingest_wave(item),
        "heavy_ingest_reasons": heavy_ingest_reasons(item),
        "final_assembly_asset_path": (
            str(asset_contract.get("assembly") or "")
            if plan.get("status") == "ready"
            else ""
        ),
    }


def external_reference_policy_metadata(item_ref):
    """Read v2 index metadata, failing closed for pre-policy manifests."""
    reasons = item_ref.get("heavy_ingest_reasons")
    if not isinstance(reasons, list):
        reasons = ["legacy_external_manifest_missing_policy_metadata"]
    return {
        "ingest_wave": str(
            item_ref.get("ingest_wave") or LEGACY_EXTERNAL_INGEST_WAVE
        ),
        "heavy_ingest_reasons": [str(value) for value in reasons if value],
        "final_assembly_asset_path": str(
            item_ref.get("final_assembly_asset_path") or ""
        ),
    }


def resolve_heavy_push_transport(requested_transport, *, unreal_running):
    """Keep generated vegetation ingest out of a live renderer process."""
    requested = str(requested_transport or "headless")
    if requested != "rpc":
        return {
            "requested": requested,
            "transport": requested,
            "changed": False,
            "reason": None,
        }
    resolved = "unreal_wait" if unreal_running else "headless"
    return {
        "requested": requested,
        "transport": resolved,
        "changed": True,
        "reason": (
            "generated Nanite skeletal/DynamicWind ingest is isolated from "
            "live-editor RPC"
        ),
    }


def migrate_saved_push_transport(config, stored_config=None):
    """Never let a persisted RPC preference become the next startup default."""
    normalized = dict(config)
    stored = stored_config if isinstance(stored_config, dict) else {}
    try:
        stored_version = int(stored.get("push_transport_policy_version", 0))
    except (TypeError, ValueError):
        stored_version = 0
    changed = str(stored.get("push_transport") or "") == "rpc"
    if changed:
        normalized["push_transport"] = "unreal_wait"
    normalized["push_transport_policy_version"] = TRANSPORT_POLICY_VERSION
    return normalized, {
        "changed": changed,
        "from": "rpc" if changed else None,
        "to": "unreal_wait" if changed else None,
        "policy_version": TRANSPORT_POLICY_VERSION,
        "stored_policy_version": stored_version,
    }
