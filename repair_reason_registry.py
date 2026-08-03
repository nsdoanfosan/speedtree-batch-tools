"""One disposition per reason code the pipeline can block on.

Before this registry existed the repair planner owned 31 codes in five private
frozensets, the runtime-normalized scan found 226 production codes, and nothing
compared the two lists.  The original lowercase-only scan undercounted that
surface by 27 codes -- so `has_repair_contract_evidence()` skipped
almost every blocked target before a plan was ever built, and the block reached
the operator as a terminal failure with no recovery path and no record that one
could have existed.

The registry exists so that set can never drift unobserved again:

* `repairable`   -- the exact-target repair planner owns a recovery for it.
* `unsupported`  -- no automatic path; the operator gets a friendly action.
* `fatal`        -- real data damage; automatic recovery would hide it.
* `informational`-- a fact/wrapper that cannot itself terminate a target.
* `unclassified` -- **not decided yet.**  Seeded from the codes that were
  invisible to the contract on 2026-08-03.  A debt marker, not an answer:
  `UNCLASSIFIED_CEILING` may only ever move down.

`tests/test_repair_reason_registry.py` fails when a module emits a code that is
absent here, when a `repairable` code is never emitted (dead vocabulary -- the
defect this registry was written to expose), and when the unclassified count
grows.  A new block therefore forces a decision at review time instead of at
3am in front of a stuck batch.
"""
from __future__ import annotations

from typing import NamedTuple


REPAIRABLE = "repairable"
UNSUPPORTED = "unsupported"
FATAL = "fatal"
UNCLASSIFIED = "unclassified"
INFORMATIONAL = "informational"

DISPOSITIONS = frozenset({
    REPAIRABLE,
    UNSUPPORTED,
    FATAL,
    UNCLASSIFIED,
    INFORMATIONAL,
})


class ReasonRow(NamedTuple):
    """How the pipeline must treat one reason code, and who emits it."""

    disposition: str
    owner: str
    note: str = ""


REASON_REGISTRY: dict[str, ReasonRow] = {
    "access_violation_exhausted": ReasonRow(
        UNSUPPORTED, "sk_batch/spm_audit.py", "exporter_crash",
    ),
    "all_export_inspection_error": ReasonRow(
        UNSUPPORTED, "sk_batch/jobs/speedtree_material_preflight.py",
        "export_inspection",
    ),
    "actionable_role_has_no_current_atlas_normalized_variants": ReasonRow(
        UNCLASSIFIED, "pcg_st9_texture_batch/pcg_cluster_assembly_contract.py", "",
    ),
    "all_consumers_planned_excluded": ReasonRow(
        UNCLASSIFIED, "sk_batch/sk_batch_gui.pyw", "",
    ),
    "assembly_source_fbx_pending_export": ReasonRow(
        UNCLASSIFIED, "sk_batch/cluster_assembly_handoff_contract.py", "",
    ),
    "asset_audit_failed": ReasonRow(
        UNCLASSIFIED, "pcg_st9_texture_batch/pcg_texture_audit.py", "",
    ),
    "asset_cluster_bake_texture_contract_invalid": ReasonRow(
        UNSUPPORTED, "sk_batch/jobs/speedtree_material_preflight.py", "recipe_gated",
    ),
    "asset_export_material_missing": ReasonRow(
        UNSUPPORTED, "sk_batch/jobs/speedtree_material_preflight.py", "export_material",
    ),
    "asset_external_mesh_path_missing": ReasonRow(
        UNCLASSIFIED, "sk_batch/jobs/speedtree_material_preflight.py", "",
    ),
    "asset_texture_source_path_missing": ReasonRow(
        UNCLASSIFIED, "sk_batch/spm_leaf_handoff_contract.py", "",
    ),
    "asset_texture_source_undeclared": ReasonRow(
        UNCLASSIFIED, "sk_batch/spm_leaf_handoff_contract.py", "",
    ),
    "atlas_blend_missing": ReasonRow(
        UNCLASSIFIED, "pcg_st9_texture_batch/pcg_texture_audit.py", "",
    ),
    "atlas_blender_source_index_invalid": ReasonRow(
        UNCLASSIFIED, "cluster_atlas_source_index.py", "",
    ),
    "atlas_managed_asset_integrity_stale": ReasonRow(
        UNCLASSIFIED, "sk_batch/jobs/speedtree_material_preflight.py", "",
    ),
    "atlas_manifest_current": ReasonRow(
        UNCLASSIFIED, "atlas_manifest_resolver.py", "",
    ),
    "atlas_manifest_candidate_conflict": ReasonRow(
        UNSUPPORTED, "sk_batch/jobs/speedtree_material_preflight.py",
        "atlas_ownership",
    ),
    "atlas_manifest_mirror_conflict_repairable": ReasonRow(
        REPAIRABLE, "atlas_manifest_resolver.py", "atlas_manifest",
    ),
    "atlas_manifest_ownership_conflict": ReasonRow(
        UNSUPPORTED, "atlas_manifest_resolver.py", "atlas_ownership",
    ),
    "atlas_ownership_marker_invalid": ReasonRow(
        UNCLASSIFIED, "sk_batch/atlas_consumer_integrity.py", "",
    ),
    "atlas_ownership_provenance_mismatch": ReasonRow(
        FATAL, "sk_batch/jobs/speedtree_material_preflight.py",
        "atlas_integrity",
    ),
    "authoritative_property_pair_missing": ReasonRow(
        UNCLASSIFIED, "pcg_st9_texture_batch/spm_generator_reference_repair.py", "",
    ),
    "automatic_repair_cancelled": ReasonRow(
        UNCLASSIFIED, "sk_batch/sk_batch_gui.pyw", "",
    ),
    "automatic_retry_cancelled": ReasonRow(
        UNCLASSIFIED, "sk_batch/sk_batch_gui.pyw", "",
    ),
    "before_marker_restore": ReasonRow(
        UNCLASSIFIED, "sk_batch/spm_problem_node_marker.py", "",
    ),
    "blend_missing": ReasonRow(
        UNCLASSIFIED, "cluster_normalization_sync.py", "",
    ),
    "blend_source_index_cancelled": ReasonRow(
        UNCLASSIFIED, "pcg_st9_texture_batch/pcg_texture_audit.py", "",
    ),
    "blend_source_index_error": ReasonRow(
        UNCLASSIFIED, "pcg_st9_texture_batch/pcg_texture_audit.py", "",
    ),
    "blend_source_index_error_root_exit": ReasonRow(
        UNCLASSIFIED, "pcg_st9_texture_batch/pcg_texture_audit.py", "",
    ),
    "blend_source_index_root_exit": ReasonRow(
        UNCLASSIFIED, "pcg_st9_texture_batch/pcg_texture_audit.py", "",
    ),
    "blend_source_index_timeout": ReasonRow(
        UNCLASSIFIED, "pcg_st9_texture_batch/pcg_texture_audit.py", "",
    ),
    "blender_cluster_bake_origin_invalid": ReasonRow(
        UNCLASSIFIED, "sk_batch/jobs/speedtree_material_preflight.py", "",
    ),
    "blender_source_collection_content_key_invalid": ReasonRow(
        UNCLASSIFIED, "cluster_atlas_source_index.py", "",
    ),
    "blender_source_collection_identity_missing": ReasonRow(
        UNCLASSIFIED, "cluster_atlas_source_index.py", "",
    ),
    "blender_source_content_changed": ReasonRow(
        UNCLASSIFIED, "cluster_atlas_source_index.py", "",
    ),
    "blender_source_content_key_missing": ReasonRow(
        UNCLASSIFIED, "cluster_atlas_source_index.py", "",
    ),
    "blender_source_empty_tombstone_invalid": ReasonRow(
        UNCLASSIFIED, "cluster_atlas_source_index.py", "",
    ),
    "blender_source_identity_ambiguous": ReasonRow(
        UNCLASSIFIED, "cluster_atlas_source_index.py", "",
    ),
    "blender_source_identity_invalid": ReasonRow(
        UNCLASSIFIED, "cluster_atlas_source_index.py", "",
    ),
    "blender_source_identity_missing": ReasonRow(
        UNCLASSIFIED, "cluster_atlas_source_index.py", "",
    ),
    "blender_source_mesh_content_key_missing": ReasonRow(
        UNCLASSIFIED, "cluster_atlas_source_index.py", "",
    ),
    "blender_source_missing": ReasonRow(
        UNCLASSIFIED, "cluster_atlas_source_index.py", "",
    ),
    "blender_source_object_identity_missing": ReasonRow(
        UNCLASSIFIED, "cluster_atlas_source_index.py", "",
    ),
    "blender_source_object_inventory_invalid": ReasonRow(
        UNCLASSIFIED, "cluster_atlas_source_index.py", "",
    ),
    "blender_source_path_changed": ReasonRow(
        UNCLASSIFIED, "cluster_atlas_source_index.py", "",
    ),
    "blender_source_path_identity_missing": ReasonRow(
        UNCLASSIFIED, "cluster_atlas_source_index.py", "",
    ),
    "blender_source_publication_binding_invalid": ReasonRow(
        UNCLASSIFIED, "cluster_atlas_source_index.py", "",
    ),
    "blender_source_publication_binding_missing": ReasonRow(
        UNCLASSIFIED, "cluster_atlas_source_index.py", "",
    ),
    "blender_source_scope_identity_missing": ReasonRow(
        UNCLASSIFIED, "cluster_atlas_source_index.py", "",
    ),
    "callback_error": ReasonRow(
        UNCLASSIFIED, "spm_generator_sync/process_stream.py", "",
    ),
    "cancelled": ReasonRow(
        UNCLASSIFIED, "spm_generator_sync/process_stream.py", "",
    ),
    "candidate_file_missing": ReasonRow(
        UNCLASSIFIED, "atlas_manifest_resolver.py", "",
    ),
    "canonical_bark_manifest_target_missing": ReasonRow(
        UNCLASSIFIED, "cluster_bark_source_resolution.py", "",
    ),
    "canonical_bark_ambiguous": ReasonRow(
        UNSUPPORTED, "sk_batch/cluster_assembly_handoff_contract.py",
        "cluster_authoring",
    ),
    "canonical_bark_missing": ReasonRow(
        UNSUPPORTED,
        "pcg_st9_texture_batch/pcg_cluster_assembly_contract.py",
        "cluster_authoring",
    ),
    "canonical_bark_material_ambiguous": ReasonRow(
        UNCLASSIFIED, "cluster_bark_source_resolution.py", "",
    ),
    "canonical_bark_material_id_missing": ReasonRow(
        UNCLASSIFIED, "cluster_bark_source_resolution.py", "",
    ),
    "canonical_bark_production_ref_not_manifest_output": ReasonRow(
        UNCLASSIFIED, "cluster_bark_source_resolution.py", "",
    ),
    "canonical_bark_normalization_required": ReasonRow(
        REPAIRABLE, "sk_batch/cluster_assembly_handoff_contract.py",
        "cluster_refresh",
    ),
    "canonical_material_mapping_incomplete": ReasonRow(
        UNCLASSIFIED, "pcg_st9_texture_batch/pcg_canonical_outputs.py", "",
    ),
    "canonical_output_has_no_material_targets": ReasonRow(
        UNCLASSIFIED, "speedtree_texture_contract.py", "",
    ),
    "canonical_output_manifest_empty": ReasonRow(
        UNCLASSIFIED, "speedtree_texture_contract.py", "",
    ),
    "canonical_output_manifest_invalid_json": ReasonRow(
        UNCLASSIFIED, "speedtree_texture_contract.py", "",
    ),
    "canonical_output_manifest_missing": ReasonRow(
        UNCLASSIFIED, "speedtree_texture_contract.py", "",
    ),
    "canonical_output_manifest_schema_mismatch": ReasonRow(
        UNCLASSIFIED, "speedtree_texture_contract.py", "",
    ),
    "canonical_source_changed": ReasonRow(
        UNCLASSIFIED, "cluster_blend_sync.py", "",
    ),
    "canonical_source_missing": ReasonRow(
        UNCLASSIFIED, "cluster_blend_sync.py", "",
    ),
    "canonical_source_semantic_unavailable": ReasonRow(
        UNCLASSIFIED, "cluster_blend_sync.py", "",
    ),
    "canonical_source_structural_changed": ReasonRow(
        UNCLASSIFIED, "cluster_blend_sync.py", "",
    ),
    "cleanup": ReasonRow(
        UNCLASSIFIED, "spm_generator_sync/process_stream.py", "",
    ),
    "cluster_missing_normalized_export_pivot": ReasonRow(
        UNCLASSIFIED, "cluster_export_handoff_contract.py", "",
    ),
    "cluster_canonical_spm_missing": ReasonRow(
        FATAL, "pcg_st9_texture_batch/pcg_cluster_assembly_contract.py",
        "cluster_identity",
    ),
    "cluster_export_artifact_mismatch": ReasonRow(
        FATAL, "sk_batch/cluster_assembly_handoff_contract.py",
        "cluster_integrity",
    ),
    "cluster_role_conflict": ReasonRow(
        FATAL, "pcg_st9_texture_batch/pcg_cluster_assembly_contract.py",
        "cluster_integrity",
    ),
    "cluster_role_handoff_blocked": ReasonRow(
        UNSUPPORTED, "sk_batch/cluster_assembly_handoff_contract.py",
        "cluster_handoff",
    ),
    "cluster_tga_basename_invalid": ReasonRow(
        UNSUPPORTED,
        "pcg_st9_texture_batch/pcg_cluster_assembly_contract.py",
        "cluster_data",
    ),
    "communication_error": ReasonRow(
        UNCLASSIFIED, "process_lifecycle.py", "",
    ),
    "compatibility_unversioned_v1": ReasonRow(
        UNCLASSIFIED, "atlas_manifest_resolver.py", "",
    ),
    "copied_artifacts": ReasonRow(
        UNCLASSIFIED, "cluster_bark_source_resolution.py", "",
    ),
    "current_atlas_material_mesh_connected": ReasonRow(
        UNCLASSIFIED, "pcg_st9_texture_batch/pcg_texture_audit.py", "",
    ),
    "current_material_mesh_connection_incomplete": ReasonRow(
        UNCLASSIFIED, "pcg_st9_texture_batch/pcg_texture_audit.py", "",
    ),
    "current_unused_group": ReasonRow(
        UNCLASSIFIED, "sk_batch/atlas_consumer_integrity.py", "",
    ),
    "current_preserved_unreferenced": ReasonRow(
        INFORMATIONAL, "sk_batch/atlas_consumer_integrity.py",
        "current_authority_variant",
    ),
    "dependency_root_reason_missing": ReasonRow(
        FATAL, "sk_batch/sk_batch_gui.pyw", "dependency_provenance",
    ),
    "declared_generator_slot_not_declared_exactly_once": ReasonRow(
        UNCLASSIFIED, "pcg_st9_texture_batch/pcg_cluster_assembly_contract.py", "",
    ),
    "different_target_spm": ReasonRow(
        UNCLASSIFIED, "atlas_manifest_resolver.py", "",
    ),
    "duplicate_material_id": ReasonRow(
        UNCLASSIFIED, "sk_batch/atlas_consumer_integrity.py", "",
    ),
    "duplicate_mesh_id": ReasonRow(
        UNCLASSIFIED, "sk_batch/atlas_consumer_integrity.py", "",
    ),
    "explicit_supported_schema": ReasonRow(
        UNCLASSIFIED, "atlas_manifest_resolver.py", "",
    ),
    "explicit_target_relation_off": ReasonRow(
        UNCLASSIFIED, "pcg_st9_texture_batch/pcg_cluster_assembly_contract.py", "",
    ),
    "exact_relation_repair_failed": ReasonRow(
        UNSUPPORTED, "sk_batch/sk_batch_gui.pyw", "repair_execution",
    ),
    "exact_target_plan_invalid": ReasonRow(
        UNSUPPORTED, "sk_batch/sk_batch_gui.pyw", "repair_plan",
    ),
    "fresh_live_export_nonparticipation": ReasonRow(
        UNCLASSIFIED, "sk_batch/jobs/speedtree_material_preflight.py", "",
    ),
    "fbx_role_material_mesh_partial": ReasonRow(
        FATAL, "pcg_st9_texture_batch/pcg_cluster_assembly_contract.py",
        "cluster_integrity",
    ),
    "generator_connection_all_bindings_planned_inactive": ReasonRow(
        UNCLASSIFIED, "pcg_st9_texture_batch/pcg_cluster_assembly_contract.py", "",
    ),
    "generator_connection_contract_incomplete": ReasonRow(
        REPAIRABLE, "pcg_st9_texture_batch/pcg_cluster_assembly_contract.py", "generator_cluster",
    ),
    "generator_connection_incomplete": ReasonRow(
        UNCLASSIFIED, "atlas_manifest_resolver.py", "",
    ),
    "generator_connection_matches_live_export": ReasonRow(
        UNCLASSIFIED, "pcg_st9_texture_batch/pcg_cluster_assembly_contract.py", "",
    ),
    "generator_connection_not_requested": ReasonRow(
        UNCLASSIFIED, "pcg_st9_texture_batch/pcg_cluster_assembly_contract.py", "",
    ),
    "generator_cross_group_pair": ReasonRow(
        UNCLASSIFIED, "sk_batch/atlas_consumer_integrity.py", "",
    ),
    "generator_guid_missing": ReasonRow(
        UNCLASSIFIED, "sk_batch/atlas_consumer_integrity.py", "",
    ),
    "generator_material_scope_unreadable": ReasonRow(
        UNCLASSIFIED, "speedtree_texture_contract.py", "",
    ),
    "generator_slot_pair_incomplete": ReasonRow(
        UNCLASSIFIED, "sk_batch/atlas_consumer_integrity.py", "",
    ),
    "hash_validated_cluster_assembly_source": ReasonRow(
        UNCLASSIFIED, "sk_batch/cluster_assembly_handoff_contract.py", "",
    ),
    "initiating_job_cancelled": ReasonRow(
        UNCLASSIFIED, "sk_batch/sk_batch_gui.pyw", "",
    ),
    "initiating_job_context_missing": ReasonRow(
        UNCLASSIFIED, "sk_batch/sk_batch_gui.pyw", "",
    ),
    "inactive_material_provisional_source": ReasonRow(
        INFORMATIONAL, "sk_batch/jobs/speedtree_material_preflight.py",
        "nonparticipating_material",
    ),
    "instance_profile_invalid": ReasonRow(
        FATAL, "speedtree_pipeline_contract.py", "pipeline_contract",
    ),
    "invalid_required_roles": ReasonRow(
        UNCLASSIFIED, "speedtree_texture_contract.py", "",
    ),
    "invalid_texture_base": ReasonRow(
        UNCLASSIFIED, "speedtree_texture_contract.py", "",
    ),
    "isolated_spm_sha256": ReasonRow(
        UNCLASSIFIED, "cluster_bark_source_resolution.py", "",
    ),
    "isolated_texture_rebase_verification_failed": ReasonRow(
        UNCLASSIFIED, "speedtree_texture_contract.py", "",
    ),
    "isolated_texture_role_unmapped": ReasonRow(
        UNCLASSIFIED, "speedtree_texture_contract.py", "",
    ),
    "kind": ReasonRow(
        UNCLASSIFIED, "cluster_bark_source_resolution.py", "",
    ),
    "live_export_evidence_contract_ambiguous": ReasonRow(
        UNCLASSIFIED, "sk_batch/jobs/speedtree_material_preflight.py", "",
    ),
    "live_export_evidence_node_table_empty": ReasonRow(
        UNCLASSIFIED, "sk_batch/jobs/speedtree_material_preflight.py", "",
    ),
    "live_export_evidence_source_mismatch": ReasonRow(
        UNCLASSIFIED, "sk_batch/jobs/speedtree_material_preflight.py", "",
    ),
    "live_export_evidence_stale_node_table": ReasonRow(
        UNCLASSIFIED, "sk_batch/jobs/speedtree_material_preflight.py", "",
    ),
    "live_export_evidence_unavailable": ReasonRow(
        UNCLASSIFIED, "sk_batch/jobs/speedtree_material_preflight.py", "",
    ),
    "live_generator_slot_not_declared_exactly_once": ReasonRow(
        UNCLASSIFIED, "pcg_st9_texture_batch/pcg_cluster_assembly_contract.py", "",
    ),
    "managed_mesh_owner_ambiguous": ReasonRow(
        UNCLASSIFIED, "sk_batch/atlas_consumer_integrity.py", "",
    ),
    "managed_mesh_scope_mismatch": ReasonRow(
        UNCLASSIFIED, "sk_batch/atlas_consumer_integrity.py", "",
    ),
    "manifest_outside_texture_root": ReasonRow(
        UNCLASSIFIED, "speedtree_texture_contract.py", "",
    ),
    "material_absent_from_rendered_mesh": ReasonRow(
        UNCLASSIFIED, "sk_batch/cluster_assembly_builder.py", "",
    ),
    "material_canonical_output_unmapped": ReasonRow(
        UNCLASSIFIED, "speedtree_texture_contract.py", "",
    ),
    "material_canonical_role_unmapped": ReasonRow(
        UNCLASSIFIED, "speedtree_texture_contract.py", "",
    ),
    "material_export_scope_evidence_ambiguous": ReasonRow(
        UNCLASSIFIED, "sk_batch/jobs/speedtree_material_preflight.py", "",
    ),
    "material_has_no_rendered_polygons": ReasonRow(
        UNCLASSIFIED, "sk_batch/cluster_assembly_builder.py", "",
    ),
    "material_id_unavailable": ReasonRow(
        UNCLASSIFIED, "sk_batch/jobs/speedtree_material_preflight.py", "",
    ),
    "material_canonical_name_divergence": ReasonRow(
        FATAL, "speedtree_pipeline_contract.py", "material_contract",
    ),
    "material_identity_not_unique": ReasonRow(
        UNCLASSIFIED, "sk_batch/jobs/speedtree_material_preflight.py", "",
    ),
    "material_is_expected_to_export": ReasonRow(
        UNCLASSIFIED, "sk_batch/jobs/speedtree_material_preflight.py", "",
    ),
    "material_intent_parse_error": ReasonRow(
        FATAL, "speedtree_pipeline_contract.py", "material_contract",
    ),
    "material_live_binding_evidence_missing": ReasonRow(
        UNCLASSIFIED, "sk_batch/jobs/speedtree_material_preflight.py", "",
    ),
    "material_live_binding_evidence_stale_or_ambiguous": ReasonRow(
        UNCLASSIFIED, "sk_batch/jobs/speedtree_material_preflight.py", "",
    ),
    "material_live_binding_state_ambiguous": ReasonRow(
        UNCLASSIFIED, "sk_batch/jobs/speedtree_material_preflight.py", "",
    ),
    "material_live_binding_visible_or_exporting": ReasonRow(
        UNCLASSIFIED, "sk_batch/jobs/speedtree_material_preflight.py", "",
    ),
    "material_texture_origin_invalid": ReasonRow(
        UNCLASSIFIED, "speedtree_texture_contract.py", "",
    ),
    "missing_export_collection": ReasonRow(
        UNCLASSIFIED, "cluster_export_handoff_contract.py", "",
    ),
    "missing_pipeline_report": ReasonRow(
        UNCLASSIFIED, "sk_batch/jobs/bwr_headless_job.py", "",
    ),
    "missing_spm_identity": ReasonRow(
        UNCLASSIFIED, "atlas_manifest_resolver.py", "",
    ),
    "modeler_launch_or_recovery_io_failed": ReasonRow(
        UNCLASSIFIED, "sk_batch/sk_batch_gui.pyw", "",
    ),
    "multiple_unsuffixed_export_roots": ReasonRow(
        UNCLASSIFIED, "cluster_export_handoff_contract.py", "",
    ),
    "new_exact_bwr_source_hierarchy": ReasonRow(
        UNCLASSIFIED, "cluster_export_handoff_contract.py", "",
    ),
    "no_cluster_assembly_roles": ReasonRow(
        UNCLASSIFIED, "sk_batch/cluster_assembly_handoff_contract.py", "",
    ),
    "no_explicit_owner_relation": ReasonRow(
        UNCLASSIFIED, "sk_batch/sk_batch_gui.pyw", "",
    ),
    "no_unsuffixed_export_root": ReasonRow(
        UNCLASSIFIED, "cluster_export_handoff_contract.py", "",
    ),
    "not_referenced_by_generator_material_property": ReasonRow(
        UNCLASSIFIED, "speedtree_texture_contract.py", "",
    ),
    "normalized_generator_delivery_incomplete": ReasonRow(
        REPAIRABLE,
        "pcg_st9_texture_batch/pcg_cluster_assembly_contract.py",
        "generator_cluster",
    ),
    "normalized_generator_node_table_stale": ReasonRow(
        REPAIRABLE,
        "pcg_st9_texture_batch/pcg_cluster_assembly_contract.py",
        "modeler_node_table",
    ),
    "normalized_variants_required": ReasonRow(
        REPAIRABLE,
        "pcg_st9_texture_batch/pcg_cluster_assembly_contract.py",
        "cluster_refresh",
    ),
    "normalized_variants_stale": ReasonRow(
        REPAIRABLE,
        "pcg_st9_texture_batch/pcg_cluster_assembly_contract.py",
        "cluster_refresh",
    ),
    "operational_candidate": ReasonRow(
        UNCLASSIFIED, "atlas_manifest_resolver.py", "",
    ),
    "operational_candidate_disagreement": ReasonRow(
        UNCLASSIFIED, "atlas_manifest_resolver.py", "",
    ),
    "operator_cancelled": ReasonRow(
        UNCLASSIFIED, "exact_target_command.py", "",
    ),
    "operator_release_requested": ReasonRow(
        UNCLASSIFIED, "shared_job_queue.py", "",
    ),
    "output_filename": ReasonRow(
        UNCLASSIFIED, "cluster_bark_source_resolution.py", "",
    ),
    "output_identity_missing": ReasonRow(
        UNCLASSIFIED, "pcg_st9_texture_batch/pcg_texture_gui.pyw", "",
    ),
    "output_missing": ReasonRow(
        UNCLASSIFIED, "sk_batch/spm_audit.py", "",
    ),
    "output_set_incomplete": ReasonRow(
        UNCLASSIFIED, "pcg_st9_texture_batch/pcg_texture_gui.pyw", "",
    ),
    "over_budget": ReasonRow(
        UNCLASSIFIED, "pcg_st9_texture_batch/pcg_board_snapshot.py", "",
    ),
    "owner_release_acknowledged": ReasonRow(
        UNCLASSIFIED, "shared_job_queue.py", "",
    ),
    "owner_spm_is_not_an_atlas_producer": ReasonRow(
        UNCLASSIFIED, "sk_batch/sk_batch_gui.pyw", "",
    ),
    "persisted_unsuffixed_export_root": ReasonRow(
        UNCLASSIFIED, "cluster_export_handoff_contract.py", "",
    ),
    "physical_capture_changed": ReasonRow(
        UNCLASSIFIED, "cluster_blend_sync.py", "",
    ),
    "physical_capture_manifest_missing": ReasonRow(
        UNCLASSIFIED, "cluster_blend_sync.py", "",
    ),
    "pcg_cluster_handoff_not_ready": ReasonRow(
        UNSUPPORTED, "sk_batch/cluster_assembly_handoff_contract.py",
        "cluster_handoff",
    ),
    "pipeline_retry_result_missing": ReasonRow(
        UNCLASSIFIED, "sk_batch/sk_batch_gui.pyw", "",
    ),
    "production_source_mutated": ReasonRow(
        UNCLASSIFIED, "cluster_bark_source_resolution.py", "",
    ),
    "production_spm_is_derived_cache": ReasonRow(
        UNCLASSIFIED, "speedtree_texture_contract.py", "",
    ),
    "production_spm_outside_manifest_asset": ReasonRow(
        UNCLASSIFIED, "speedtree_texture_contract.py", "",
    ),
    "preflight_error": ReasonRow(
        UNSUPPORTED, "sk_batch/jobs/speedtree_material_preflight.py",
        "preflight_error",
    ),
    "protected_manual": ReasonRow(
        UNCLASSIFIED, "sk_batch/atlas_consumer_integrity.py", "",
    ),
    "provider_identity": ReasonRow(
        UNCLASSIFIED, "cluster_bark_source_resolution.py", "",
    ),
    "provisional_source_blocked": ReasonRow(
        UNCLASSIFIED, "sk_batch/jobs/speedtree_material_preflight.py", "",
    ),
    "publication_canceled": ReasonRow(
        UNCLASSIFIED, "pcg_st9_texture_batch/pcg_board_snapshot.py", "",
    ),
    "receipt_not_requested": ReasonRow(
        INFORMATIONAL, "pcg_st9_texture_batch/pcg_texture_audit.py",
        "receipt_status",
    ),
    "receipt_persistence_failed": ReasonRow(
        UNSUPPORTED, "pcg_st9_texture_batch/pcg_texture_audit.py",
        "receipt_persistence",
    ),
    "registered_reason_has_no_exact_action": ReasonRow(
        UNSUPPORTED, "sk_batch/sk_batch_gui.pyw", "repair_plan",
    ),
    "recorded_source_conflict": ReasonRow(
        UNCLASSIFIED, "cluster_blend_sync.py", "",
    ),
    "recorded_source_missing": ReasonRow(
        UNCLASSIFIED, "cluster_blend_sync.py", "",
    ),
    "recovery_continuation_result_missing": ReasonRow(
        UNCLASSIFIED, "sk_batch/sk_batch_gui.pyw", "",
    ),
    "recovery_live_export_target_scope_empty": ReasonRow(
        UNCLASSIFIED, "pcg_st9_texture_batch/stale_node_table_recovery.py", "",
    ),
    "recovery_target_material_scope_missing": ReasonRow(
        UNCLASSIFIED, "pcg_st9_texture_batch/stale_node_table_recovery.py", "",
    ),
    "relation_physical_proof_incomplete": ReasonRow(
        UNCLASSIFIED, "cluster_blend_sync.py", "",
    ),
    "relationship_continuity_only": ReasonRow(
        UNCLASSIFIED, "pcg_st9_texture_batch/pcg_cluster_assembly_contract.py", "",
    ),
    "repair_inventory_target_missing": ReasonRow(
        UNCLASSIFIED, "sk_batch/sk_batch_gui.pyw", "",
    ),
    "required_cluster_repair_cancelled": ReasonRow(
        UNCLASSIFIED, "sk_batch/sk_batch_gui.pyw", "",
    ),
    "required_cluster_repair_failed": ReasonRow(
        UNCLASSIFIED, "sk_batch/sk_batch_gui.pyw", "",
    ),
    "required_cluster_repaired_resume": ReasonRow(
        UNCLASSIFIED, "sk_batch/sk_batch_gui.pyw", "",
    ),
    "resumed_pipeline_result_missing": ReasonRow(
        UNCLASSIFIED, "sk_batch/sk_batch_gui.pyw", "",
    ),
    "retired_scope_record": ReasonRow(
        UNCLASSIFIED, "atlas_manifest_resolver.py", "",
    ),
    "root_exit": ReasonRow(
        UNCLASSIFIED, "process_lifecycle.py", "",
    ),
    "runtime_shutdown": ReasonRow(
        UNCLASSIFIED, "shared_queue_runtime.py", "",
    ),
    "same_role_reference_provider_is_not_an_assembly_part": ReasonRow(
        UNCLASSIFIED, "pcg_st9_texture_batch/pcg_cluster_assembly_contract.py", "",
    ),
    "schema_version_not_integer": ReasonRow(
        UNCLASSIFIED, "atlas_manifest_resolver.py", "",
    ),
    "sealed_policy_has_no_required_live_targets": ReasonRow(
        UNCLASSIFIED, "pcg_st9_texture_batch/stale_node_table_recovery.py", "",
    ),
    "shared_dependency_failed": ReasonRow(
        INFORMATIONAL, "sk_batch/sk_batch_gui.pyw", "dependency_wrapper",
    ),
    "shared_queue_lease_owner_lost": ReasonRow(
        UNSUPPORTED, "sk_batch/sk_batch_gui.pyw", "lifecycle_owner_lost",
    ),
    "signature": ReasonRow(
        UNCLASSIFIED, "cluster_bark_source_resolution.py", "",
    ),
    "sk_batch_local_queue_cancelled": ReasonRow(
        UNCLASSIFIED, "sk_batch/sk_batch_gui.pyw", "",
    ),
    "sk_stop": ReasonRow(
        UNCLASSIFIED, "sk_batch/sk_common.py", "",
    ),
    "sk_worker_complete": ReasonRow(
        UNCLASSIFIED, "sk_batch/sk_common.py", "",
    ),
    "source_already_repaired_after_blocking_audit": ReasonRow(
        UNCLASSIFIED, "sk_batch/sk_batch_gui.pyw", "",
    ),
    "source_fbx_changed": ReasonRow(
        UNCLASSIFIED, "cluster_blend_sync.py", "",
    ),
    "source_fbx_missing": ReasonRow(
        UNCLASSIFIED, "cluster_blend_sync.py", "",
    ),
    "source_spm_missing": ReasonRow(
        UNCLASSIFIED, "cluster_bark_source_resolution.py", "",
    ),
    "source_spm_path": ReasonRow(
        UNCLASSIFIED, "cluster_bark_source_resolution.py", "",
    ),
    "source_spm_sha256": ReasonRow(
        UNCLASSIFIED, "cluster_bark_source_resolution.py", "",
    ),
    "source_xml_missing": ReasonRow(
        UNCLASSIFIED, "cluster_normalization_sync.py", "",
    ),
    "speedtree_root_exit": ReasonRow(
        UNCLASSIFIED, "sk_batch/spm_audit.py", "",
    ),
    "speedtree_spm_filename": ReasonRow(
        UNCLASSIFIED, "cluster_bark_source_resolution.py", "",
    ),
    "speedtree_spm_missing": ReasonRow(
        UNCLASSIFIED, "cluster_bark_source_resolution.py", "",
    ),
    "speedtree_spm_path": ReasonRow(
        UNCLASSIFIED, "cluster_bark_source_resolution.py", "",
    ),
    "speedtree_texture_role_unknown": ReasonRow(
        UNCLASSIFIED, "speedtree_texture_contract.py", "",
    ),
    "speedtree_timeout": ReasonRow(
        UNCLASSIFIED, "sk_batch/spm_audit.py", "",
    ),
    "spm_mesh_file_missing": ReasonRow(
        FATAL, "sk_batch/jobs/speedtree_material_preflight.py",
        "asset_missing",
    ),
    "spm_visible_default_material_ambiguous": ReasonRow(
        FATAL, "sk_batch/job_report_contract.py", "material_contract",
    ),
    "status": ReasonRow(
        UNCLASSIFIED, "cluster_bark_source_resolution.py", "",
    ),
    "target_material_lineage_missing": ReasonRow(
        UNCLASSIFIED, "pcg_st9_texture_batch/pcg_texture_audit.py", "",
    ),
    "target_mesh_asset_missing": ReasonRow(
        UNCLASSIFIED, "pcg_st9_texture_batch/pcg_cluster_assembly_contract.py", "",
    ),
    "target_missing": ReasonRow(
        UNCLASSIFIED, "cluster_blend_sync.py", "",
    ),
    "target_outside_owner": ReasonRow(
        UNCLASSIFIED, "cluster_blend_sync.py", "",
    ),
    "target_scope_candidate_missing": ReasonRow(
        UNCLASSIFIED, "atlas_manifest_resolver.py", "",
    ),
    "target_scope_changed": ReasonRow(
        UNCLASSIFIED, "cluster_blend_sync.py", "",
    ),
    "timed_out": ReasonRow(
        UNCLASSIFIED, "spm_generator_sync/process_stream.py", "",
    ),
    "texture_set_incomplete": ReasonRow(
        REPAIRABLE, "sk_batch/jobs/speedtree_material_preflight.py",
        "pcg_texture",
    ),
    "timeout": ReasonRow(
        UNCLASSIFIED, "process_lifecycle.py", "",
    ),
    "unreal_dependency_full_rebuild_fallback": ReasonRow(
        UNCLASSIFIED, "sk_batch/sk_batch_gui.pyw", "",
    ),
    "unsupported_schema_version": ReasonRow(
        UNCLASSIFIED, "atlas_manifest_resolver.py", "",
    ),
    "userdata_not_object": ReasonRow(
        UNCLASSIFIED, "sk_batch/atlas_consumer_integrity.py", "",
    ),
    "worker_wait_failed": ReasonRow(
        UNCLASSIFIED, "spm_generator_sync/spm_generator_sync_gui.pyw", "",
    ),
    "written": ReasonRow(
        UNCLASSIFIED, "pcg_st9_texture_batch/pcg_board_snapshot.py", "",
    ),
}

# Seeded on 2026-08-03.  Lower it in the same commit that classifies a code;
# never raise it.  A new block ships with a disposition or it does not ship.
UNCLASSIFIED_CEILING = 193

# The planner now derives its vocabulary exclusively from emitted registry
# rows.  Historical aliases with no production emitter were deleted rather
# than preserved as unreachable repair promises.
UNEMITTED_PLANNER_CODES = frozenset()


def disposition_of(code: str) -> str:
    """Return the registered disposition, or UNCLASSIFIED for an unknown code."""

    row = REASON_REGISTRY.get(str(code).strip().casefold())
    return row.disposition if row else UNCLASSIFIED


def codes_with(disposition: str) -> tuple[str, ...]:
    return tuple(sorted(
        code for code, row in REASON_REGISTRY.items()
        if row.disposition == disposition
    ))
