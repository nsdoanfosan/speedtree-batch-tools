import json
import tempfile
import unittest
from pathlib import Path

from atlas_producer_rebind import (
    AtlasProducerRebindProofError,
    apply_atlas_producer_registry_rebind,
    build_atlas_producer_rebind_proof,
    plan_atlas_producer_registry_rebind,
    validate_atlas_producer_rebind_proof,
    validate_atlas_producer_refresh_receipt,
)
from atlas_target_registry import (
    TargetRegistryPublishError,
    load_target_registry,
    save_target_registry,
)
from cluster_spm_pair_contract import (
    bootstrap_cluster_authoring,
    cluster_spm_pair_receipt_path,
)


class AtlasProducerRebindTests(unittest.TestCase):
    def fixture(self, root, *, divergent=False, group_count=5):
        owner = Path(root) / "Tree_elm"
        cluster = owner / "cluster"
        cluster.mkdir(parents=True)
        legacy = cluster / "cluster_elm_01.spm"
        legacy.write_bytes(b"normalized-generation")
        bootstrap_cluster_authoring(legacy)
        canonical = cluster / "SK_cluster_elm_01.spm"
        if divergent:
            canonical.write_bytes(b"canonical-current")
            legacy.write_bytes(b"legacy-later-drift")

        atlas = Path(root) / "atlas"
        atlas.mkdir()
        blend = atlas / "M_cluster_elm_atlas_01.blend"
        blend.write_bytes(b"blend-producer")
        unrelated = owner / "SK_Tree_elm_01.spm"
        unrelated.write_bytes(b"tree")
        save_target_registry(blend, [unrelated, legacy])

        manifest_path = (
            cluster
            / ".atlas_leaf_speedtree_targets"
            / "cluster_elm_01.json"
        )
        manifest_path.parent.mkdir()
        material_groups = [
            {
                "collection": f"Atlas_Group_{index}",
                "material": f"M_Atlas_{index}",
                "material_id": index + 1,
                "mesh_ids": list(range(index * 3 + 1, index * 3 + 4)),
            }
            for index in range(group_count)
        ]
        manifest_path.write_text(json.dumps({
            "spm": str(legacy),
            "blend_file": str(blend),
            "source_collection": "Atlas_Cluster_Cards",
            "export_scope_id": "scope-exact-producer",
            "speedtree_material_groups": material_groups,
            "generator_connection": {
                "requested": False,
                "complete": False,
                "bindings": [],
            },
        }), encoding="utf-8")
        return {
            "canonical": canonical,
            "legacy": legacy,
            "blend": blend,
            "manifest": manifest_path,
            "unrelated": unrelated,
            "groups": material_groups,
        }

    def proof(self, fixture):
        return build_atlas_producer_rebind_proof(
            fixture["canonical"],
            fixture["manifest"],
            inventory_paths=[fixture["canonical"]],
        )

    def test_pair_receipt_proves_exact_producer_without_type_count_ceiling(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = self.fixture(temporary, group_count=7)

            proof = self.proof(fixture)

            self.assertEqual(proof["status"], "validated")
            self.assertTrue(proof["authoritative"])
            self.assertEqual(proof["producer"]["connection_mode"], "assets_only")
            self.assertEqual(len(proof["producer"]["material_groups"]), 7)
            self.assertEqual(
                proof["producer"]["material_groups"][-1]["mesh_ids"],
                [19, 20, 21],
            )
            self.assertEqual(proof["pair"]["generation"], 1)
            self.assertEqual(
                proof["registry"]["legacy_target_index"], 1
            )

    def test_historical_pair_receipt_accepts_later_file_divergence(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = self.fixture(temporary, divergent=True)

            proof = self.proof(fixture)

            self.assertNotEqual(
                proof["canonical_spm"]["sha256"],
                proof["legacy_spm"]["sha256"],
            )
            self.assertEqual(
                validate_atlas_producer_rebind_proof(proof)["proof_sha256"],
                proof["proof_sha256"],
            )

    def test_apply_preserves_unrelated_targets_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = self.fixture(temporary)
            plan = plan_atlas_producer_registry_rebind(self.proof(fixture))

            first = apply_atlas_producer_registry_rebind(plan)
            second = apply_atlas_producer_registry_rebind(plan)

            self.assertEqual(first["status"], "applied")
            self.assertTrue(first["committed"])
            self.assertEqual(second["status"], "up_to_date")
            self.assertFalse(second["committed"])
            self.assertEqual(
                load_target_registry(fixture["blend"])["target_spms"],
                [
                    str(fixture["unrelated"].absolute()),
                    str(fixture["canonical"].absolute()),
                ],
            )

    def test_compare_and_swap_failure_preserves_concurrent_registry_edit(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = self.fixture(temporary)
            plan = plan_atlas_producer_registry_rebind(self.proof(fixture))
            concurrent = Path(temporary) / "Tree_elm" / "SK_Tree_elm_02.spm"
            concurrent.write_bytes(b"second-tree")
            concurrent_targets = [
                fixture["unrelated"], fixture["legacy"], concurrent,
            ]
            save_target_registry(fixture["blend"], concurrent_targets)

            with self.assertRaises(TargetRegistryPublishError):
                apply_atlas_producer_registry_rebind(plan)

            self.assertEqual(
                load_target_registry(fixture["blend"])["target_spms"],
                [str(path.absolute()) for path in concurrent_targets],
            )

    def test_missing_pair_receipt_cannot_be_reinterpreted_as_authority(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = self.fixture(temporary)
            cluster_spm_pair_receipt_path(fixture["canonical"]).unlink()

            with self.assertRaisesRegex(
                AtlasProducerRebindProofError,
                "pair normalization receipt",
            ):
                self.proof(fixture)

    def test_proof_tampering_breaks_the_seal(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = self.fixture(temporary)
            proof = self.proof(fixture)
            proof["producer"]["export_scope_id"] = "different-scope"

            with self.assertRaisesRegex(
                AtlasProducerRebindProofError,
                "seal is invalid",
            ):
                validate_atlas_producer_rebind_proof(proof)

    def test_canonical_assets_only_receipt_postconditions_are_exact(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = self.fixture(temporary)
            proof = self.proof(fixture)
            apply_atlas_producer_registry_rebind(
                plan_atlas_producer_registry_rebind(proof)
            )
            payload = json.loads(
                fixture["manifest"].read_text(encoding="utf-8")
            )
            canonical_manifest = (
                fixture["canonical"].parent
                / ".atlas_leaf_speedtree_targets"
                / f"{fixture['canonical'].stem}.json"
            )
            payload["spm"] = str(fixture["canonical"])
            payload["target_manifest"] = str(canonical_manifest)
            canonical_manifest.write_text(
                json.dumps(payload), encoding="utf-8"
            )

            receipt = validate_atlas_producer_refresh_receipt(
                proof,
                manifest_path=canonical_manifest,
            )

            self.assertEqual(receipt["status"], "validated")
            self.assertEqual(receipt["connection_mode"], "assets_only")
            self.assertEqual(
                receipt["canonical_spm"], str(fixture["canonical"])
            )


if __name__ == "__main__":
    unittest.main()
