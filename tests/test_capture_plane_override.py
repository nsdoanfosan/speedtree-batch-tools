"""An explicit projection preserves independent Atlas and bend semantics."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import cluster_blend_sync as sync
import cluster_normalization_sync as normalization
import cluster_source_prepare as preparation
from tests import test_cluster_normalization_sync as fixtures


class CapturePlaneOverrideTests(unittest.TestCase):
    def fixture(self, directory, **kwargs):
        blend, source, target, probe = fixtures.ClusterNormalizationSyncTests().fixture(
            directory, "SK_cluster_tree_NothofagusSolandri_side_01")
        recipe = normalization.resolve_normalization_recipe(
            blend, [target], unit_probe_path=probe, part_bend_role="leaf_side", **kwargs)
        return blend, target, probe, recipe

    def receipt(self, blend, recipe, *, matching_hash=True):
        capture = fixtures.write_capture_manifest(recipe)
        receipt = {
            "kind": "speedtree_cluster_sync_normalization", "status": "ready",
            "unit_probe_sha256": recipe["unit_probe_sha256"],
            "source_spm_sha256": recipe["source_spm_sha256"],
            "canonical_spm": recipe["canonical_spm"], "source_xml": recipe["source_xml"],
            "capture_manifest": str(capture.absolute()),
            "capture_manifest_sha256": fixtures.sha256(capture),
            "normalization_contract_sha256": (
                recipe["normalization_contract_sha256"] if matching_hash else "old-target-list-hash"),
            "material": recipe["material_name"],
            "build": {
                "workflow_mode": "PHYSICAL_DIRECT_CAPTURE", "source_object": recipe["source_object"],
                "source_partition_mode": recipe["source_partition_mode"],
                "plan_collection": recipe["plan_collection"],
                "plan_refinement_levels": recipe["plan_refinement_levels"],
                "source_3d_contract": {"xml_sha256": recipe["source_xml_sha256"]},
                "physical_capture_contract": {
                    "workflow_mode": "PHYSICAL_DIRECT_CAPTURE",
                    "capture_resolution": [recipe["capture_resolution"]] * 2,
                    "frame": {"plane": recipe["capture_plane"],
                              "padding_ratio": recipe["capture_padding_ratio"],
                              "target_meters": [recipe["capture_target_meters"]] * 2},
                },
            },
        }
        receipt_path = Path(recipe["receipt_path"])
        receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
        fixtures.seal_receipt_source_identity(blend, receipt_path, recipe)
        return receipt_path, capture

    def test_none_preserves_default_capture_role_material_and_collection(self):
        for name, role, plane in (("custom.blend", "branch", "XY"),
                                  ("SK_leaf_any.blend", "leaf", "XY"),
                                  ("SK_leaf_any_side.blend", "leaf_side", "YZ")):
            default = normalization.cluster_role_contract(name)
            self.assertEqual(normalization.cluster_role_contract(name, capture_plane_override=None), default)
            self.assertEqual((default["role"], default["capture_plane"]), (role, plane))

    def test_plane_override_only_changes_projection(self):
        name = "SK_cluster_tree_NothofagusSolandri_side_01.blend"
        default = normalization.cluster_role_contract(name)
        for plane in ("XY", "XZ", "YZ", " yz "):
            expected = {**default, "capture_plane": plane.strip().upper()}
            self.assertEqual(normalization.cluster_role_contract(name, capture_plane_override=plane), expected)
            self.assertEqual((expected["role"], expected["plan_collection"]), ("branch", "Atlas_Branch_Plans"))

    def test_invalid_plane_fails_before_recipe_source_inspection(self):
        with patch.object(normalization, "_assembly_report") as inspect:
            for value in (True, 1, {}, [], "", "AUTO", "ZZ", "leaf_side"):
                with self.assertRaisesRegex(normalization.ClusterNormalizationSyncError, "Capture plane"):
                    normalization.resolve_normalization_recipe(
                        "nonexistent.blend", [], unit_probe_path="unused", capture_plane_override=value)
        inspect.assert_not_called()

    def test_recipe_hash_seals_plane_but_keeps_independent_bend_and_atlas_roles(self):
        with tempfile.TemporaryDirectory() as temporary:
            blend, target, probe, default = self.fixture(temporary)
            changed = normalization.resolve_normalization_recipe(
                blend, [target], unit_probe_path=probe, part_bend_role="leaf_side", capture_plane_override=" yz ")
            self.assertEqual((changed["capture_plane"], changed["capture_plane_override"]), ("YZ", "YZ"))
            self.assertEqual((changed["role"], changed["plan_collection"]), ("branch", "Atlas_Branch_Plans"))
            self.assertEqual(changed["part_bend_policy"]["role_code"], 2)
            self.assertEqual(changed["part_bend_role"], "leaf_side")
            for key in ("part_bend_policy", "material_name", "target_material_bindings"):
                self.assertEqual(changed[key], default[key])
            self.assertNotEqual(changed["normalization_contract_sha256"], default["normalization_contract_sha256"])

    def test_same_effective_plane_keeps_hash_and_none_recipe_unchanged(self):
        with tempfile.TemporaryDirectory() as temporary:
            blend, target, probe, default = self.fixture(temporary)
            options = dict(unit_probe_path=probe, part_bend_role="leaf_side")
            same = normalization.resolve_normalization_recipe(blend, [target], capture_plane_override="xy", **options)
            unset = normalization.resolve_normalization_recipe(blend, [target], capture_plane_override=None, **options)
            self.assertEqual(unset, default)
            self.assertEqual(same["normalization_contract_sha256"], default["normalization_contract_sha256"])
            self.assertNotIn("capture_plane_override", default)

    def test_default_legacy_cache_migration_stays_valid_but_explicit_override_requires_exact_hash(self):
        with tempfile.TemporaryDirectory() as temporary:
            blend, target, probe, default = self.fixture(temporary)
            self.receipt(blend, default, matching_hash=False)
            self.assertTrue(normalization._receipt_is_current(default))
            explicit = normalization.resolve_normalization_recipe(
                blend, [target], unit_probe_path=probe, part_bend_role="leaf_side", capture_plane_override="XY")
            self.assertTrue(explicit["normalization_required"])
            self.assertFalse(normalization._receipt_is_current(explicit))

    def test_exact_override_cache_is_reused_and_changed_capture_bytes_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            blend, target, probe, recipe = self.fixture(temporary, capture_plane_override="YZ")
            _, capture = self.receipt(blend, recipe)
            current = normalization.resolve_normalization_recipe(
                blend, [target], unit_probe_path=probe, part_bend_role="leaf_side", capture_plane_override="yz")
            self.assertFalse(current["normalization_required"])
            capture.write_bytes(b"changed-capture")
            self.assertFalse(normalization._receipt_is_current(current))

    def test_source_prepare_current_and_rebuild_forward_plane_and_bend_selector(self):
        blend = Path(tempfile.gettempdir()) / "Owner/Cluster/SK_CustomSide.blend"
        target = blend.parent.parent / "SK_Owner.spm"
        with patch.object(preparation, "resolve_normalization_recipe", return_value={}) as resolver:
            preparation.prepare_cluster_source_if_required(blend, [target], blender_exe="unused",
                unit_probe_path="unused", part_bend_role="leaf_side", capture_plane_override=" yz ")
        self.assertEqual(resolver.call_args.kwargs["capture_plane_override"], "YZ")
        self.assertEqual(resolver.call_args.kwargs["part_bend_role"], "leaf_side")
        required = normalization.ClusterSourceBuildRequiredError("stale", blend=blend,
            canonical_spm=blend.with_suffix(".spm"), report_path=blend.parent / "report.json", reason="source_identity_stale")
        with patch.object(preparation, "resolve_normalization_recipe", side_effect=[required, {}]) as resolver, \
             patch.object(preparation, "prepare_cluster_spm_pair_for_job", return_value={"canonical_spm": blend.with_suffix(".spm")}), \
             patch.object(preparation, "load_config", return_value={}), \
             patch.object(preparation, "_build_cluster_source", return_value={"status": "rebuilt"}):
            preparation.prepare_cluster_source_if_required(blend, [target], blender_exe="unused",
                unit_probe_path="unused", part_bend_role="leaf_side", capture_plane_override="YZ")
        self.assertEqual(resolver.call_count, 2)
        for call in resolver.call_args_list:
            self.assertEqual((call.kwargs["capture_plane_override"], call.kwargs["part_bend_role"]), ("YZ", "leaf_side"))

    def test_known_stale_source_invalid_plane_is_rejected_before_build(self):
        blend = Path(tempfile.gettempdir()) / "SK_CustomSide.blend"
        required = normalization.ClusterSourceBuildRequiredError("stale", blend=blend,
            canonical_spm=blend.with_suffix(".spm"), report_path=blend.parent / "report.json", reason="source_identity_stale")
        with patch.object(preparation, "prepare_cluster_spm_pair_for_job") as pair, \
             patch.object(preparation, "_build_cluster_source") as build:
            with self.assertRaises(normalization.ClusterNormalizationSyncError):
                preparation.prepare_cluster_source_if_required(blend, [], blender_exe="unused",
                    unit_probe_path="unused", known_required=required, capture_plane_override="invalid")
        pair.assert_not_called()
        build.assert_not_called()

    def test_recipe_rebuild_wrapper_preserves_explicit_options(self):
        blend = Path(tempfile.gettempdir()) / "SK_CustomSide.blend"
        required = normalization.ClusterSourceBuildRequiredError("stale", blend=blend,
            canonical_spm=blend.with_suffix(".spm"), report_path=blend.parent / "report.json", reason="source_identity_stale")
        with patch.object(sync, "resolve_normalization_recipe", side_effect=required) as resolver, \
             patch.object(sync, "prepare_cluster_source_if_required", return_value={"validated_normalization_recipe": {}}) as prepare:
            sync._resolve_normalization_recipe_with_source_rebuild(blend, [], blender_exe="unused",
                unit_probe_path="unused", capture_resolution=1024, progress_callback=None,
                part_bend_role="leaf_side", capture_plane_override="YZ")
        for call in (resolver.call_args, prepare.call_args):
            self.assertEqual((call.kwargs["capture_plane_override"], call.kwargs["part_bend_role"]), ("YZ", "leaf_side"))

    def test_public_folder_forwards_plane_without_changing_role(self):
        blend = Path(tempfile.gettempdir()) / "Owner/Cluster/SK_CustomSide.blend"
        target = blend.parent.parent / "SK_Owner.spm"
        rows = [{"blend": str(blend), "targets": [{"target_spm": str(target), "owner_target": True}]}]
        with patch.object(sync, "discover_cluster_blend_relations", return_value=rows), \
             patch.object(sync, "run_cluster_relation_transaction", return_value={"status": "ok"}) as operation:
            sync.run_cluster_folder_relation_transaction(blend, enabled=True, blender_exe="unused",
                part_bend_role="leaf_side", capture_plane_override="YZ")
        self.assertEqual((operation.call_args.kwargs["capture_plane_override"], operation.call_args.kwargs["part_bend_role"]), ("YZ", "leaf_side"))

    def test_public_transaction_cannot_skip_requested_plane_when_already_on(self):
        class ReachedRecipe(Exception):
            pass
        blend = Path(tempfile.gettempdir()) / "Owner/Cluster/SK_CustomSide.blend"
        target = blend.parent.parent / "SK_Owner.spm"
        with patch.object(sync, "_validate_local_relation", return_value=(blend.absolute(), target)), \
             patch.object(sync, "_registry_target_spms", return_value=[]), \
             patch.object(sync, "_inspect_current_cluster_relation_state") as inspect, \
             patch.object(Path, "is_file", return_value=True), \
             patch.object(sync, "_resolve_normalization_recipe_with_source_rebuild", side_effect=ReachedRecipe) as resolver:
            with self.assertRaises(ReachedRecipe):
                sync.run_cluster_relation_transaction(blend, [target], enabled=True, blender_exe="unused",
                    part_bend_role="leaf_side", capture_plane_override=" yz ")
        inspect.assert_not_called()
        self.assertEqual(resolver.call_args.kwargs["capture_plane_override"], "YZ")

    def test_override_invalid_or_outside_automatic_on_fails_before_relation_mutations(self):
        for enabled, automatic, plane in ((True, True, "ZZ"), (False, True, "YZ"), (True, False, "YZ")):
            with patch.object(sync, "_validate_local_relation") as validate:
                with self.assertRaises((normalization.ClusterNormalizationSyncError, sync.ClusterBlendSyncError)):
                    sync.run_cluster_relation_transaction("unused.blend", [], enabled=enabled,
                        auto_normalize=automatic, blender_exe="unused", capture_plane_override=plane)
            validate.assert_not_called()


if __name__ == "__main__":
    unittest.main()
