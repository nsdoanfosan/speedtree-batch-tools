"""Explicit normalized source roles survive user-selected human names."""
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import json
import tempfile
import unittest
from unittest.mock import patch

import cluster_blend_sync as sync
import cluster_source_prepare as preparation
from sk_batch.assembly_part_bend_policy import normalization_part_bend_policy
from tests.test_cluster_normalization_sync import ClusterNormalizationSyncTests, sha256, write_capture_manifest, seal_receipt_source_identity
from tests.test_cluster_relation_job_contract import load_job_module


class ExplicitBendRoleTests(unittest.TestCase):
    def test_noncanonical_source_flows_through_recipe_and_worker_metadata(self):
        with tempfile.TemporaryDirectory() as temporary:
            blend, source, target, probe = ClusterNormalizationSyncTests().fixture(temporary, "SK_CustomFlow")
            for explicit in ("cluster", "leaf", "leaf_side"):
                recipe, rebuilt = sync._resolve_normalization_recipe_with_source_rebuild(
                    blend, [target], blender_exe="unused", unit_probe_path=probe,
                    capture_resolution=1024, progress_callback=None, part_bend_role=explicit)
                self.assertIsNone(rebuilt)
                self.assertEqual(recipe["part_bend_role"], explicit)
                self.assertEqual(recipe["part_bend_policy"], normalization_part_bend_policy("Any human name", role=explicit))
                self.assertEqual(recipe["role"], "branch")
                self.assertEqual(recipe["plan_collection"], "Atlas_Branch_Plans")
                props = SimpleNamespace(part_bend_role="NONE")
                self.assertEqual(load_job_module().configure_normalized_part_bend_role(props, recipe), explicit)
                self.assertEqual(props.part_bend_role, explicit)
                stale = deepcopy(recipe)
                stale["part_bend_policy"]["schema_version"] = 1
                with self.assertRaisesRegex(RuntimeError, "stale"):
                    load_job_module().configure_normalized_part_bend_role(SimpleNamespace(part_bend_role="NONE"), stale)

    def test_unknown_auto_is_disabled_and_explicit_branch_none_override_leaf_name(self):
        self.assertIsNone(normalization_part_bend_policy("CustomFlow"))
        self.assertIsNone(normalization_part_bend_policy("SK_leaf_willow_01", role="branch"))
        self.assertIsNone(normalization_part_bend_policy("SK_leaf_willow_01", role="NONE"))
        with self.assertRaises(ValueError):
            normalization_part_bend_policy("CustomFlow", role="unknown")
        job = load_job_module()
        for recipe in ({"skeletal_base": "CustomFlow"},
                       {"skeletal_base": "SK_leaf_willow_01", "part_bend_role": "branch"},
                       {"skeletal_base": "SK_leaf_willow_01", "part_bend_role": "NONE"}):
            props = SimpleNamespace(part_bend_role="leaf")
            self.assertEqual(job.configure_normalized_part_bend_role(props, recipe), "NONE")

    def test_fresh_policy_role_accepts_renamed_source_and_conflicting_disabled_role_fails(self):
        policy = normalization_part_bend_policy("CustomFlow", role="cluster")
        job = load_job_module()
        self.assertEqual(job.configure_normalized_part_bend_role(SimpleNamespace(part_bend_role="NONE"), {
            "skeletal_base": "CustomFlow", "part_bend_policy": policy}), "cluster")
        with self.assertRaisesRegex(RuntimeError, "stale"):
            job.configure_normalized_part_bend_role(SimpleNamespace(part_bend_role="NONE"), {
                "skeletal_base": "CustomFlow", "part_bend_role": "branch", "part_bend_policy": policy})

    def test_public_folder_api_passes_role_and_source_preparation_retains_it(self):
        blend = Path(tempfile.gettempdir()) / "Owner/Cluster/SK_CustomFlow.blend"
        target = blend.parent.parent / "SK_Owner.spm"
        rows = [{"blend": str(blend), "targets": [{"target_spm": str(target), "owner_target": True}]}]
        with patch.object(sync, "discover_cluster_blend_relations", return_value=rows), patch.object(sync, "run_cluster_relation_transaction", return_value={"status": "ok"}) as operation:
            sync.run_cluster_folder_relation_transaction(blend, enabled=True, blender_exe="unused", part_bend_role="cluster")
        self.assertEqual(operation.call_args.kwargs["part_bend_role"], "cluster")
        with patch.object(preparation, "resolve_normalization_recipe", return_value={}) as resolver:
            preparation.prepare_cluster_source_if_required(blend, [target], blender_exe="unused", unit_probe_path="unused", part_bend_role="cluster")
        self.assertEqual(resolver.call_args.kwargs["part_bend_role"], "cluster")

    def test_default_auto_recipe_hash_and_existing_leaf_policy_are_unchanged(self):
        with tempfile.TemporaryDirectory() as temporary:
            blend, source, target, probe = ClusterNormalizationSyncTests().fixture(temporary)
            args = dict(blender_exe="unused", unit_probe_path=probe, capture_resolution=1024, progress_callback=None)
            first, _ = sync._resolve_normalization_recipe_with_source_rebuild(blend, [target], **args)
            second, _ = sync._resolve_normalization_recipe_with_source_rebuild(blend, [target], part_bend_role="AUTO", **args)
            self.assertNotIn("part_bend_role", first)
            self.assertEqual(first["normalization_contract_sha256"], second["normalization_contract_sha256"])
            self.assertEqual(first["part_bend_policy"], normalization_part_bend_policy("Any name", role="leaf"))
            capture = write_capture_manifest(first)
            receipt = Path(first["receipt_path"])
            receipt.write_text(json.dumps({"kind": "speedtree_cluster_sync_normalization", "status": "ready",
                "recipe_sha256": first["recipe_sha256"], "source_spm_sha256": first["source_spm_sha256"],
                "unit_probe_sha256": first["unit_probe_sha256"], "output_blend_sha256": sha256(blend),
                "capture_manifest": str(capture.absolute()), "capture_manifest_sha256": sha256(capture)}), encoding="utf-8")
            seal_receipt_source_identity(blend, receipt, first)
            selected, _ = sync._resolve_normalization_recipe_with_source_rebuild(blend, [target], part_bend_role="leaf", **args)
            self.assertEqual(selected["normalization_contract_sha256"], first["normalization_contract_sha256"])
            self.assertFalse(selected["normalization_required"])
            stale = json.loads(receipt.read_text(encoding="utf-8"))
            stale["build"]["prototypes"][0]["part_bend_payload"]["schema_version"] = 1
            receipt.write_text(json.dumps(stale), encoding="utf-8")
            rejected, _ = sync._resolve_normalization_recipe_with_source_rebuild(blend, [target], part_bend_role="leaf", **args)
            self.assertTrue(rejected["normalization_required"])


if __name__ == "__main__":
    unittest.main()
