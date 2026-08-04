import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from repair_orchestration import (
    ATLAS_MANIFEST_MIRROR_REPAIR,
    CLUSTER_REFRESH,
    GENERATOR_SYNC,
    GENERATOR_SYNC_AND_CLUSTER,
    MODELER_NODE_TABLE_RECOVERY,
    MODELER_RECOVERY_TOOL,
    REPAIR_UI_AUTOMATIC,
    REPAIR_UI_BLOCKED,
    STATUS_COMPLETED,
    STATUS_FINAL_FAILED,
    STATUS_PENDING,
    STEP3_STANDARD,
    build_exact_target_repair_plan,
    compact_success_message,
    final_failure_filter,
    fresh_repair_receipt_authoritative,
    repair_progress_payload,
    repair_ui_decision,
)


class RepairOrchestrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.target = self.root / "SK exact 자산.spm"
        self.cluster = self.root / "cluster" / "SK_cluster.spm"
        self.cluster.parent.mkdir()
        self.target.write_bytes(b"target")
        self.cluster.write_bytes(b"cluster")
        self.inventory = [self.target, self.cluster]

    def tearDown(self):
        self.temp.cleanup()

    def plan(self, evidence):
        return build_exact_target_repair_plan(
            self.target,
            evidence,
            inventory_paths=self.inventory,
            parent_retry_id="retry-118",
            request_id="request-1",
        )

    def modeler_scope(self):
        scope = {
            "schema_version": 2,
            "available": True,
            "mode": "owned_semantic_uia_modeler_save_watch",
            "scope_policy": "explicit_sealed_delivery_scopes_v1",
            "target_spm": str(self.target),
            "target_preimage_raw_sha256": "a" * 64,
            "authoring_mesh_ids": [1, 2],
            "required_live_mesh_ids": [1],
        }
        scope["scope_sha256"] = hashlib.sha256((json.dumps(
            scope,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ) + "\n").encode("utf-8")).hexdigest()
        return scope

    def test_texture_availability_reason_does_not_schedule_repair(self):
        plan = self.plan({
            "reason_code": "texture_set_incomplete",
        })
        self.assertFalse(plan.supported)
        self.assertEqual(plan.stages, ())

    def test_repairable_atlas_mirror_conflict_routes_exact_bat(self):
        plan = self.plan({
            "current_atlas_manifest_repair": {
                "status": "repairable",
                "reason_code": "atlas_manifest_mirror_conflict_repairable",
                "target_spm": str(self.target),
            },
        })
        self.assertTrue(plan.supported)
        self.assertEqual(len(plan.stages), 1)
        self.assertEqual(
            plan.stages[0]["repair_action"],
            ATLAS_MANIFEST_MIRROR_REPAIR,
        )
        self.assertEqual(
            plan.stages[0]["target_spms"], [str(self.target)]
        )

    def test_ambiguous_atlas_ownership_conflict_is_friendly_unsupported(self):
        plan = self.plan({
            "reason_code": "atlas_manifest_ownership_conflict",
            "reason": "different source identity",
        })
        self.assertFalse(plan.supported)
        self.assertIn("ownership", plan.friendly_reason)
        self.assertIn("덮어쓸 수 없습니다", plan.remaining_action)

    def test_operator_message_has_one_korean_atlas_disposition(self):
        repairable = repair_ui_decision({
            "reason_code": "atlas_manifest_mirror_conflict_repairable",
        })
        blocked = repair_ui_decision({
            "reason_code": "atlas_manifest_ownership_conflict",
        })

        self.assertEqual(repairable["status"], REPAIR_UI_AUTOMATIC)
        self.assertIn("낡은 Atlas manifest 미러", repairable["reason"])
        self.assertIn("exact BAT", repairable["action"])
        self.assertEqual(blocked["status"], REPAIR_UI_BLOCKED)
        self.assertIn("서로 다른 원본", blocked["reason"])
        self.assertIn("임의로 덮어쓰지 않습니다", blocked["action"])

    def test_registered_unsupported_reason_has_owner_cause_and_action(self):
        decision = repair_ui_decision({
            "reason_code": "managed_mesh_owner_ambiguous",
        })

        self.assertEqual(decision["status"], REPAIR_UI_BLOCKED)
        self.assertIn("owner scope", decision["reason"])
        self.assertIn("Material/Mesh/Generator ID", decision["action"])
        self.assertTrue(any("가" <= ch <= "힣" for ch in decision["reason"]))
        self.assertTrue(any("가" <= ch <= "힣" for ch in decision["action"]))

    def test_unsealed_stale_node_table_is_explicit_final_block(self):
        evidence = {
            "issue_codes": ["NORMALIZED_GENERATOR_NODE_TABLE_STALE"],
            "stale_node_table_recovery": {
                "available": False,
                "reason_token": "target_delivery_scope_not_explicit",
            },
        }
        decision = repair_ui_decision(evidence)
        plan = self.plan(evidence)

        self.assertEqual(decision["status"], REPAIR_UI_BLOCKED)
        self.assertIn("복구에 필요한 exact 대상 범위", decision["reason"])
        self.assertIn("Mesh ID 범위", decision["action"])
        self.assertFalse(plan.supported)
        self.assertEqual(plan.initial_status, STATUS_FINAL_FAILED)
        self.assertEqual(plan.friendly_reason, decision["reason"])

    def test_scope_only_failure_has_one_exact_korean_action(self):
        decision = repair_ui_decision({
            "reason_code": "stale_target_mesh_scope_missing",
        })

        self.assertEqual(decision["status"], REPAIR_UI_BLOCKED)
        self.assertEqual(
            decision["reason"],
            "SpeedTree Node table 복구에 필요한 exact 대상 범위 증거가 없거나 손상되었습니다.",
        )
        self.assertIn("대상 SPM, provider, Mesh ID 범위", decision["action"])

    def test_failure_wrapper_requires_actual_reason_before_retry(self):
        decision = repair_ui_decision({
            "reason_code": "automatic_repair_reaudit_failed",
        })

        self.assertEqual(decision["status"], REPAIR_UI_BLOCKED)
        self.assertIn("구체 원인이 영수증에 남지 않았습니다", decision["reason"])
        self.assertIn("실제 원인 코드", decision["action"])

    def test_visible_generator_pair_has_exact_korean_manual_action(self):
        decision = repair_ui_decision({
            "reason_code": "generator_cross_group_pair",
        })
        plan = self.plan({"reason_code": "generator_cross_group_pair"})

        self.assertEqual(decision["status"], REPAIR_UI_BLOCKED)
        self.assertEqual(
            decision["reason"],
            "표시되는 Generator가 해당 Material이 소유하지 않는 Mesh를 참조합니다.",
        )
        self.assertIn("Material ID, Mesh ID", decision["action"])
        self.assertFalse(plan.supported)
        self.assertEqual(plan.friendly_reason, decision["reason"])

    def test_generator_delivery_is_explicit_automatic_repair(self):
        decision = repair_ui_decision({
            "delivery_reason": "generator_connection_contract_incomplete",
            "issues": [{"code": "NORMALIZED_GENERATOR_DELIVERY_INCOMPLETE"}],
        })

        self.assertEqual(decision["status"], REPAIR_UI_AUTOMATIC)
        self.assertIn("Generator", decision["reason"])
        self.assertIn("Cluster 갱신", decision["action"])

    def test_sealed_stale_node_table_has_one_automatic_disposition(self):
        evidence = {
            "issue_codes": ["NORMALIZED_GENERATOR_NODE_TABLE_STALE"],
            "stale_node_table_recovery": self.modeler_scope(),
            "producer_spm": str(self.cluster),
        }
        decision = repair_ui_decision(evidence)
        plan = self.plan(evidence)

        self.assertEqual(decision["status"], REPAIR_UI_AUTOMATIC)
        self.assertTrue(plan.supported)
        self.assertEqual(
            plan.stages[0]["repair_action"],
            MODELER_NODE_TABLE_RECOVERY,
        )
        self.assertEqual(plan.stages[0]["tool"], MODELER_RECOVERY_TOOL)
        self.assertEqual(
            plan.stages[0]["producer_spm"], str(self.cluster)
        )

    def test_live_export_stale_alias_uses_same_sealed_modeler_repair(self):
        evidence = {
            "reason_code": "live_export_evidence_unavailable_stale_node_table",
            "stale_node_table_recovery": self.modeler_scope(),
            "producer_spm": str(self.cluster),
        }

        decision = repair_ui_decision(evidence)
        plan = self.plan(evidence)

        self.assertEqual(decision["status"], REPAIR_UI_AUTOMATIC)
        self.assertTrue(plan.supported)
        self.assertEqual(
            plan.stages[0]["repair_action"],
            MODELER_NODE_TABLE_RECOVERY,
        )

    def test_malformed_modeler_scope_fails_before_exact_execution(self):
        scope = self.modeler_scope()
        scope["required_live_mesh_ids"] = [99]
        sealed = {
            key: value for key, value in scope.items()
            if key != "scope_sha256"
        }
        scope["scope_sha256"] = hashlib.sha256((json.dumps(
            sealed,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ) + "\n").encode("utf-8")).hexdigest()

        plan = self.plan({
            "issue_codes": ["NORMALIZED_GENERATOR_NODE_TABLE_STALE"],
            "stale_node_table_recovery": scope,
            "producer_spm": str(self.cluster),
        })

        self.assertFalse(plan.supported)
        self.assertEqual(plan.initial_status, STATUS_FINAL_FAILED)
        self.assertEqual(plan.stages, ())
        self.assertIn("exact target 범위", plan.friendly_reason)

    def test_restored_node_table_evidence_resolves_one_nested_provider(self):
        plan = self.plan({
            "selected_failure": {
                "reason_token": "normalized_generator_node_table_stale",
                "target_spm": str(self.target),
                "producer_spm": str(self.cluster),
                "stale_node_table_recovery": self.modeler_scope(),
            },
        })

        self.assertTrue(plan.supported)
        self.assertEqual(len(plan.stages), 1)
        self.assertEqual(
            plan.stages[0]["repair_action"],
            MODELER_NODE_TABLE_RECOVERY,
        )
        self.assertEqual(plan.stages[0]["producer_spm"], str(self.cluster))

    def test_missing_normalized_variant_routes_to_exact_cluster_refresh(self):
        evidence = {
            "issues": [{
                "code": "NORMALIZED_VARIANTS_REQUIRED",
                "spm": str(self.target),
            }],
        }
        decision = repair_ui_decision(evidence)
        plan = self.plan(evidence)

        self.assertEqual(decision["status"], REPAIR_UI_AUTOMATIC)
        self.assertIn("필수 정규화 Cluster variant", decision["reason"])
        self.assertTrue(plan.supported)
        self.assertEqual(plan.stages[0]["repair_action"], CLUSTER_REFRESH)

    def test_atlas_receipt_and_lineage_reasons_route_cluster_refresh(self):
        cases = {
            "atlas_manifest_authority_missing": "producer 영수증이 없습니다",
            "atlas_manifest_resolution_conflict": "영수증들이 서로 충돌합니다",
            "lineage_unproven": "producer 계보가 증명되지 않았습니다",
        }
        for reason_code, korean_reason in cases.items():
            with self.subTest(reason_code=reason_code):
                evidence = {"reason_code": reason_code}
                decision = repair_ui_decision(evidence)
                plan = self.plan(evidence)

                self.assertEqual(decision["status"], REPAIR_UI_AUTOMATIC)
                self.assertIn(korean_reason, decision["reason"])
                self.assertTrue(plan.supported)
                self.assertEqual(
                    [stage["repair_action"] for stage in plan.stages],
                    [CLUSTER_REFRESH],
                )
                self.assertEqual(
                    plan.stages[0]["target_spms"],
                    [str(self.target)],
                )

    def test_missing_cluster_tga_is_exact_korean_final_block(self):
        evidence = {
            "issues": [{
                "code": "CLUSTER_TGA_BASENAME_INVALID",
                "details": {
                    "status": "missing",
                    "missing": ["missing.tga"],
                    "expected_base": "SK_cluster_elm_01",
                },
            }],
        }
        decision = repair_ui_decision(evidence)
        plan = self.plan(evidence)

        self.assertEqual(decision["status"], REPAIR_UI_BLOCKED)
        self.assertIn("TGA 파일이 없습니다", decision["reason"])
        self.assertIn("missing.tga", decision["reason"])
        self.assertFalse(plan.supported)
        self.assertEqual(plan.friendly_reason, decision["reason"])

    def test_deadleaves_missing_t_roles_do_not_schedule_repair(self):
        plan = self.plan({
            "status": "blocked",
            "issues": [{
                "code": "TEXTURE_SET_INCOMPLETE",
                "missing_roles": ["normal", "opacity"],
                "expected_texture_base": "T_Leaf_deadleaves_01",
            }],
        })
        self.assertFalse(plan.supported)
        self.assertEqual(plan.stages, ())

    def test_nonblocking_attempt_wrapper_cannot_mask_repairable_root(self):
        evidence = {
            "reason_token": "generator_connection_contract_incomplete",
            "repair_attempt": {
                "status": "not_started",
                "reason_token": "initiating_job_context_missing",
            },
        }

        decision = repair_ui_decision(evidence)

        self.assertEqual(decision["status"], REPAIR_UI_AUTOMATIC)
        self.assertIn(
            "initiating_job_context_missing",
            decision["reason_codes"],
        )

    def test_mixed_reasons_are_ordered_and_deduplicated(self):
        plan = self.plan({
            "reason_codes": [
                "texture_set_incomplete",
                "texture_set_incomplete",
                "generator_connection_contract_incomplete",
                "normalized_variants_stale",
            ],
            "producer_spm": str(self.cluster),
        })
        self.assertEqual(
            [stage["repair_action"] for stage in plan.stages],
            [GENERATOR_SYNC_AND_CLUSTER],
        )
        self.assertEqual(
            plan.stages[-1]["target_spms"],
            [str(self.target), str(self.cluster)],
        )

    def test_generator_delivery_routes_sync_then_evidence_cluster(self):
        plan = self.plan({
            "delivery_reason": "generator_connection_contract_incomplete",
            "issues": [{"code": "NORMALIZED_GENERATOR_DELIVERY_INCOMPLETE"}],
            "producer_spm": str(self.cluster),
            "normalization_postcondition": "not_run",
            "sync_outcome_authoritative": False,
        })
        self.assertTrue(plan.supported)
        self.assertEqual(
            [stage["repair_action"] for stage in plan.stages],
            [GENERATOR_SYNC_AND_CLUSTER],
        )

    def test_inventory_report_sibling_relation_cannot_expand_exact_plan(self):
        sibling = self.root / "SK_sibling.spm"
        sibling_cluster = self.cluster.parent / "SK_sibling_cluster.spm"
        sibling.write_bytes(b"sibling")
        sibling_cluster.write_bytes(b"sibling-cluster")
        self.inventory.extend((sibling, sibling_cluster))
        plan = self.plan({
            "queue_id": str(self.target),
            "selected_failure": {
                "reason_token": "generator_connection_contract_incomplete",
                "evidence": {
                    "target_spm": str(self.target),
                    "producer_spm": str(self.cluster),
                },
            },
            "current_report_payloads": [{
                "payload": {
                    "relations": [{
                        "target_spm": str(self.target),
                        "producer_spm": str(self.cluster),
                    }, {
                        "target_spm": str(sibling),
                        "producer_spm": str(sibling_cluster),
                    }],
                },
            }],
        })
        self.assertTrue(plan.supported)
        self.assertEqual(
            plan.stages[0]["target_spms"],
            [str(self.target), str(self.cluster)],
        )

    def test_generator_delivery_without_canonical_relation_fails_closed(self):
        plan = self.plan({
            "reason_token": "generator_connection_contract_incomplete",
        })
        self.assertFalse(plan.supported)
        self.assertEqual(plan.initial_status, STATUS_FINAL_FAILED)
        self.assertIn("canonical Cluster", plan.friendly_reason)

    def test_cluster_bake_mismatch_requires_authoritative_recipe(self):
        blocked = self.plan({
            "classification": "asset_cluster_bake_texture_contract_invalid",
            "material_ids": ["4", "7"],
            "roles": ["base_color", "normal"],
        })
        self.assertFalse(blocked.supported)
        self.assertIn("material IDs 4, 7", blocked.friendly_reason)
        self.assertIn("roles base_color, normal", blocked.friendly_reason)

        repaired = self.plan({
            "classification": "asset_cluster_bake_texture_contract_invalid",
            "cluster_material_binding_recipe": {
                "status": "validated",
                "authoritative": True,
                "target_spm": str(self.cluster),
            },
        })
        self.assertTrue(repaired.supported)
        self.assertEqual(repaired.stages[0]["repair_action"], CLUSTER_REFRESH)

    def test_historical_alias_fails_closed_without_text_dispatch(self):
        plan = self.plan({
            "reason_code": "atlas_strict_material_binding_conflict",
            "error": "texture_set_incomplete generator_connection_contract_incomplete",
        })
        self.assertFalse(plan.supported)
        self.assertEqual(plan.stages, ())
        self.assertIn("등록되지 않은 차단 코드", plan.friendly_reason)
        self.assertNotIn("PCG", plan.friendly_reason)

    def test_export_material_and_access_violation_are_friendly_unsupported(self):
        material = self.plan({
            "classification": "asset_export_material_missing",
            "missing_export_materials": ["M_leaf", "M_branch"],
        })
        self.assertFalse(material.supported)
        self.assertIn("내보내기에 재질이 없습니다", material.friendly_reason)
        self.assertIn("M_leaf", material.remaining_action)
        self.assertNotIn("obsolete", material.remaining_action.casefold())

        crash = self.plan({
            "result": "access_violation_exhausted",
            "attempt_count": 3,
        })
        self.assertFalse(crash.supported)
        self.assertIn("3회", crash.friendly_reason)

    def test_texture_telemetry_adds_no_repair_progress_stage(self):
        plan = self.plan({
            "reason_code": "texture_set_incomplete",
        })
        progress = repair_progress_payload(
            plan,
            status=STATUS_PENDING,
            completed_stages=0,
        )
        self.assertEqual(progress["remaining_stages"], 0)
        rows = [
            {"status": STATUS_PENDING, "target": "pending"},
            {"status": STATUS_COMPLETED, "target": "done"},
            {"status": STATUS_FINAL_FAILED, "target": "unsupported"},
        ]
        self.assertEqual(
            [row["target"] for row in final_failure_filter(rows)],
            ["unsupported"],
        )
        self.assertEqual(
            compact_success_message(plan.stages),
            "자동 복구: Blender → Unreal, 통과",
        )

    def test_only_current_request_receipt_is_terminal_authority(self):
        plan = {
            "request_id": "new-request",
            "parent_retry_id": "parent-118",
            "exact_spm": str(self.target),
        }
        current = {
            **plan,
            "status": STATUS_COMPLETED,
        }
        july = {
            **current,
            "request_id": "july-stale-request",
        }
        self.assertTrue(fresh_repair_receipt_authoritative(current, plan))
        self.assertFalse(fresh_repair_receipt_authoritative(july, plan))


if __name__ == "__main__":
    unittest.main()
