import importlib.util
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock


JOB_PATH = (
    Path(__file__).resolve().parents[1]
    / "spm_generator_sync"
    / "jobs"
    / "cluster_relation_job.py"
)


def load_job_module():
    name = "cluster_relation_job_contract_runtime"
    spec = importlib.util.spec_from_file_location(name, JOB_PATH)
    module = importlib.util.module_from_spec(spec)
    with mock.patch.dict(sys.modules, {
        "addon_utils": types.SimpleNamespace(),
        "bpy": types.SimpleNamespace(),
    }):
        spec.loader.exec_module(module)
    return module


class ClusterRelationJobContractTests(unittest.TestCase):
    def test_physical_handoff_reads_the_cluster_key_with_legacy_fallback(self):
        source = JOB_PATH.read_text(encoding="utf-8")
        self.assertIn('build.get("cluster_handoff")', source)
        self.assertIn('build.get("atlas_handoff")', source)

    def test_cluster_export_preserves_the_explicit_cluster_material_name(self):
        source = JOB_PATH.read_text(encoding="utf-8")
        self.assertIn(
            "preserve_explicit_material_name=True",
            source,
        )

    def test_existing_relation_is_normalized_in_place(self):
        source = JOB_PATH.read_text(encoding="utf-8")
        sync_source = source[
            source.index("def sync_targets("):
            source.index("\ndef remove_targets(")
        ]
        self.assertIn(
            "Normalize each requested relation in place.",
            sync_source,
        )
        self.assertNotIn("pre_export_relation_cleanup", sync_source)
        self.assertNotIn("cleanup_existing_relation_for_rebuild", sync_source)
        self.assertNotIn("remove_blend_target_from_spm", sync_source)

    def test_current_receipt_rehydrates_the_plan_collection_before_export(self):
        source = JOB_PATH.read_text(encoding="utf-8")
        self.assertIn(
            "configure_cluster_export_properties(",
            source,
        )
        self.assertIn(
            'collection_name=recipe["plan_collection"]',
            source,
        )
        self.assertIn(
            'first_binding.get("connect_generators") is True',
            source,
        )

    def test_requested_live_slice_requires_one_explicit_binding_per_target(self):
        source = JOB_PATH.read_text(encoding="utf-8")
        self.assertIn("validate_recipe_registry_contract(", source)
        self.assertIn(
            "not set(effective).issubset(set(recipe_targets))",
            source,
        )
        self.assertIn(
            'binding.get("connect_generators") not in {True, False}',
            source,
        )
        self.assertIn(
            "for path in requested",
            source[source.index("props.speedtree_spm_items.clear()"):],
        )
        self.assertNotIn(
            "save_spm_target_registry(props)",
            source,
        )
        self.assertNotIn(
            'first_binding.get("connect_generators", True)',
            source,
        )

    def test_final_export_is_verified_before_pending_handoff_is_promoted(self):
        source = JOB_PATH.read_text(encoding="utf-8")
        contract_source = (
            JOB_PATH.parents[2] / "cluster_export_handoff_contract.py"
        ).read_text(encoding="utf-8")
        self.assertIn("cluster_export_contract_issues(", source)
        self.assertIn("finalize_cluster_source_pipeline(", source)
        self.assertIn("finalize_cluster_pipeline_payload(", source)
        self.assertIn('handoff["status"] = final_status', contract_source)
        self.assertIn('"cluster_export_pending"', contract_source)

    def test_source_index_is_bound_before_receipt_is_persisted(self):
        source = JOB_PATH.read_text(encoding="utf-8")
        sync_source = source[
            source.index("def sync_targets("):
            source.index("\ndef remove_targets(")
        ]
        bind_position = sync_source.index(
            "bind_index_to_export_results("
        )
        persist_position = sync_source.index(
            "persist_normalization_source_index("
        )
        finalize_position = sync_source.index(
            "finalize_cluster_source_pipeline("
        )
        self.assertLess(bind_position, persist_position)
        self.assertLess(persist_position, finalize_position)
        self.assertIn(
            "if source_index is not None:",
            sync_source[:bind_position],
        )

    def test_persisted_source_index_supersedes_legacy_only_after_binding(self):
        job = load_job_module()
        with tempfile.TemporaryDirectory() as temporary:
            receipt_path = Path(temporary) / "normalization.json"
            receipt_path.write_text(json.dumps({
                "kind": "speedtree_cluster_sync_normalization",
                "status": "ready",
                "source_blend_content_identity": {
                    "kind": "superseded-self-projection",
                },
            }), encoding="utf-8")
            recipe = {
                "receipt_path": str(receipt_path),
            }
            bound = {
                "kind": "speedtree_cluster_atlas_blender_source_index",
                "status": "ok",
                "publication": {
                    "status": "bound",
                    "target_count": 1,
                    "targets": [{"target_spm": "SK_tree.spm"}],
                },
            }

            result = job.persist_normalization_source_index(
                recipe, bound
            )
            first_bytes = receipt_path.read_bytes()
            receipt = json.loads(first_bytes.decode("utf-8"))
            second = job.persist_normalization_source_index(
                recipe, bound
            )

            self.assertTrue(result["changed"])
            self.assertFalse(second["changed"])
            self.assertEqual(receipt_path.read_bytes(), first_bytes)
            self.assertEqual(receipt["source_blender_index"], bound)
            self.assertNotIn(
                "source_blend_content_identity", receipt
            )
            self.assertEqual(
                receipt["superseded_source_blend_content_identity"],
                {"kind": "superseded-self-projection"},
            )

    def test_capture_reuses_prior_scope_to_follow_collection_rename(self):
        job = load_job_module()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            blend = root / "SK_cluster.blend"
            blend.write_bytes(b"blend")
            receipt_path = root / "normalization.json"
            receipt_path.write_text(json.dumps({
                "source_blender_index": {
                    "authoritative_collection": {
                        "collection_name": "Previous_Name",
                        "export_scope_id": "scope-stable",
                    },
                },
            }), encoding="utf-8")
            recipe = {
                "receipt_path": str(receipt_path),
                "blend": str(blend),
                "plan_collection": "Previous_Name",
                "material_name": "M_cluster",
            }
            expected = {
                "authoritative_collection": {
                    "collection_name": "Renamed_Collection",
                },
            }
            with mock.patch.object(
                job,
                "build_current_atlas_source_index",
                return_value=expected,
            ) as build:
                result = job.capture_normalization_source_index(recipe)

            self.assertEqual(result, expected)
            build.assert_called_once_with(
                blend.absolute(),
                "Previous_Name",
                atlas_asset_name="M_cluster",
                expected_scope_id="scope-stable",
            )

    def test_unbound_source_index_cannot_mutate_receipt(self):
        job = load_job_module()
        with tempfile.TemporaryDirectory() as temporary:
            receipt_path = Path(temporary) / "normalization.json"
            original = json.dumps({
                "kind": "speedtree_cluster_sync_normalization",
                "status": "ready",
            }).encode("utf-8")
            receipt_path.write_bytes(original)

            with self.assertRaises(RuntimeError):
                job.persist_normalization_source_index(
                    {"receipt_path": str(receipt_path)},
                    {
                        "kind": "speedtree_cluster_atlas_blender_source_index",
                        "status": "ok",
                    },
                )

            self.assertEqual(receipt_path.read_bytes(), original)

    def test_worker_failure_persists_traceback_before_parent_rollback(self):
        source = JOB_PATH.read_text(encoding="utf-8")
        self.assertIn(
            "_persist_cluster_relation_failure(",
            source,
        )
        self.assertIn(
            'phase="blender_worker_exception"',
            source,
        )
        self.assertIn(
            '"persistent_failure_report"',
            source,
        )

    def test_exact_isolated_bark_bundle_is_revalidated_before_map_bake(self):
        source = JOB_PATH.read_text(encoding="utf-8")
        normalize_source = source[
            source.index("def normalize_cluster_blend("):
            source.index("\ndef configure_cluster_export_properties(")
        ]
        validation = normalize_source.index(
            "validate_isolated_bark_recipe_bundle(recipe)"
        )
        bake = normalize_source.index(
            "bpy.ops.speedtree_cluster.bake_capture_maps()"
        )
        self.assertLess(validation, bake)

    def test_explicit_generator_delivery_scope_requires_producer_echo(self):
        source = JOB_PATH.read_text(encoding="utf-8")
        mapping_source = source[
            source.index("def apply_recipe_source_material_mappings("):
            source.index("\ndef sync_targets(")
        ]
        sync_source = source[
            source.index("def sync_targets("):
            source.index("\ndef remove_targets(")
        ]
        self.assertIn(
            'request["generator_delivery_scope_intent"]',
            mapping_source,
        )
        self.assertIn(
            "validate_resolved_delivery_scope(",
            mapping_source,
        )
        self.assertIn(
            "validate_producer_delivery_scope_results(results, bindings_by_key)",
            sync_source,
        )


if __name__ == "__main__":
    unittest.main()
