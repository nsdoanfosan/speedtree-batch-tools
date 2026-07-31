"""Final status helpers shared by headless SK Batch jobs."""


_AMBIGUOUS_DEFAULT_BARK_PREFIX = (
    "SpeedTree merged Default placeholder requires exactly one actual "
    "managed bark material slot with a ready binding; found "
)


def _ready_managed_bark_candidates(report):
    envelope = report.get("speedtree_pipeline_contract")
    if not isinstance(envelope, dict):
        return []
    candidates = []
    for intent in envelope.get("material_intents") or []:
        if (
            str(intent.get("tree_part") or "") != "bark"
            or str(intent.get("texture_source_mode") or "")
            != "managed_texture_set"
        ):
            continue
        binding = intent.get("texture_binding")
        if not isinstance(binding, dict) or binding.get("status") != "ok":
            continue
        candidates.append({
            "material_name": str(intent.get("material_name") or ""),
            "stmat_material_id": str(
                intent.get("stmat_material_id") or ""
            ),
            "texture_base": str(binding.get("texture_base") or ""),
            "texture_dir": str(binding.get("texture_dir") or ""),
        })
    return candidates


def annotate_known_failure(report, error):
    """Attach actionable asset/process ownership without changing the error."""
    message = str(error)
    if not message.startswith(_AMBIGUOUS_DEFAULT_BARK_PREFIX):
        return report
    candidates = _ready_managed_bark_candidates(report)
    report["failure_classification"] = (
        "asset_speedtree_default_material_assignment_ambiguous"
    )
    report["asset_issue"] = {
        "code": "SPM_VISIBLE_DEFAULT_MATERIAL_AMBIGUOUS",
        "stage": "blender_merge_material_resolution",
        "reason": (
            "The exported SpeedTree mesh contains an authored Default material "
            "used by rendered geometry, but more than one distinct ready bark "
            "material is present. Blender cannot prove which bark belongs on "
            "those Default faces without changing the asset's authored intent."
        ),
        "candidates": candidates,
        "remediation": (
            "Open the listed SPM in SpeedTree Modeler, replace every visible "
            "generator/cap assignment that still uses Default_Mat with the "
            "intended authored bark or bark-end material, save the SPM, and "
            "rerun the batch. Do not resolve this by choosing a candidate in "
            "Blender."
        ),
        "automatic_repair_safe": False,
    }
    return report


def mark_job_failed(report, error, traceback_text):
    """Make a terminal job failure authoritative over earlier preflight state."""
    report["status"] = "failed"
    report["unreal_push_ready"] = False
    report["final_handoff_status"] = "failed"
    report["error"] = str(error)
    report["traceback"] = str(traceback_text or "")
    annotate_known_failure(report, error)
    return report
