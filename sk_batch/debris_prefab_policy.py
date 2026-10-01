"""Pure admission for an explicitly selected SPM's terrain prefab.

Source name, category and Wind are metadata, never activation conditions.
Generated groups use static rest pose; the original source/Wind data is untouched.
"""
from __future__ import annotations

from collections.abc import Mapping


def eligibility(spm_path, enabled, effective_wind=None):
    """Only an explicit boolean selection authorizes candidate preparation.

    ``spm_path`` is caller-owned identity and is deliberately not inspected.
    Optional Wind readbacks are copied into the report without qualifying the
    source. Actual whole-part and import geometry proofs remain separate gates.
    """
    selected = enabled is True
    preset = active = None
    if selected and isinstance(effective_wind, Mapping):
        preset = effective_wind.get("preset", effective_wind.get("wind_preset"))
        active = effective_wind.get("enabled", effective_wind.get("wind_enabled"))
    elif selected and isinstance(effective_wind, str):
        preset = effective_wind
    return {
        "enabled": selected,
        "eligible": selected,
        "reason": "eligible_explicit_selection" if selected else
                  ("disabled" if type(enabled) is bool else "invalid_enabled_flag"),
        "wind_preset": preset,
        "wind_enabled": active,
        "category": None,
        "mode": "static_rest_pose",
        "activation_policy": "explicit_per_spm_selection",
        "wind_audit_only": True,
        "requires_actual_wind_verification": False,
        "gate_stage": "candidate" if selected else "disabled",
    }


def native_eligibility(spm_path, enabled, effective_wind=None, *,
                       source_parts_verified=False, import_geometry_verified=False):
    """Fail closed on whole-part preservation and strict native geometry proof.

    Wind never grants or denies admission. The source's rest-pose geometry is
    grouped only after the provider verifies its ownership/identity contract.
    Blueprint authoring additionally requires the strict native import proof.
    """
    result = eligibility(spm_path, enabled, effective_wind)
    result.update(source_parts_verified=source_parts_verified is True,
                  import_geometry_verified=import_geometry_verified is True)
    if not result["enabled"]:
        return result
    if source_parts_verified is not True:
        result.update(eligible=False, reason="source_parts_unverified")
    elif import_geometry_verified is not True:
        result.update(eligible=False, reason="import_geometry_unverified")
    else:
        result.update(reason="eligible_native_verified", gate_stage="native")
    return result
