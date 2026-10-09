import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from sk_batch.tests.test_unreal_ingest import load_runner


class ElmBendDependencySaveTests(unittest.TestCase):
    def setUp(self):
        self.runner = load_runner()
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.content = Path(self.temporary.name)
        from assembly_part_bend_policy import ELM_ASSEMBLIES, production_assembly_bend_policy
        self.policy = production_assembly_bend_policy(ELM_ASSEMBLIES[0])
        self.paths = self.policy["prototypes"]
        self.dirty = set()
        self.saved = []
        self.fail_at = None
        self.assets = {path: SimpleNamespace(path=path) for path in self.paths}

        def save_without_thumbnail(asset):
            self.saved.append(asset.path)
            if len(self.saved) == self.fail_at:
                return False
            target = self.runner._durable_package_file(asset.path)
            target.write_bytes(target.read_bytes() + b"native-material-and-metadata")
            self.dirty.discard(asset.path)
            return True

        self.runner.unreal.Paths = SimpleNamespace(
            project_content_dir=lambda: str(self.content),
            convert_relative_path_to_full=lambda path: path,
        )
        self.runner.unreal.EditorAssetLibrary = SimpleNamespace(
            load_asset=lambda path: self.assets.get(path),
        )
        self.runner.unreal.EditorLoadingAndSavingUtils = SimpleNamespace(
            get_dirty_content_packages=lambda: [
                SimpleNamespace(get_path_name=lambda path=path: path)
                for path in sorted(self.dirty)
            ],
        )
        self.runner.unreal.CodexMaterialToolsLibrary = SimpleNamespace(
            save_asset_package_without_thumbnail=save_without_thumbnail,
        )
        self.runner._refresh_headless_saved_asset_registry = lambda path: None
        self.ledger = self.runner._new_durable_save_ledger()
        for path in self.paths:
            package = self.runner._durable_package_file(path)
            package.parent.mkdir(parents=True, exist_ok=True)
            package.write_bytes(b"provider-prebuild")
            self.runner._record_durable_save(
                self.ledger, path, owner="assembly_prototype_prebuild",
                role="prototype", save_mode="thumbnail_free",
            )
        self.dirty.update(self.paths)
        self.dirty.add("/Game/Unrelated/UserUnsaved")
        self.item = {"checkout_asset_paths": list(self.paths)}
        self.report = {
            "assembly": self.policy["assembly"], "apply_requested": True,
            "elm_part_bend_policy": self.policy,
            "dependent_packages_to_save": list(self.paths),
            "native_material_remap": {
                "payload_checked_prototype_count": 4,
                "payload_verified_before_material_mutation": True,
                "production_prototype_materials_directly_applied": True,
                "root_source_mesh_description_preserved": True,
            },
        }

    def test_exact_four_saved_and_prebuild_ownership_refreshed(self):
        result = self.runner._save_elm_part_bend_dependencies(
            self.item, self.report, durable_saves=self.ledger,
        )
        self.assertEqual(result["status"], "persisted_verified_clean")
        self.assertEqual(self.saved, self.paths)
        self.assertEqual(len(result["saved_dependencies"]), 4)
        self.assertEqual(self.dirty, {"/Game/Unrelated/UserUnsaved"})
        self.assertEqual(len(self.ledger["records"]), 4)
        self.assertTrue(self.runner._validate_durable_save_ledger(self.ledger))
        for record in self.ledger["records"]:
            self.assertEqual(record["owner"], "assembly_prototype_prebuild")
            self.assertEqual(record["role"], "prototype")
            self.assertEqual(len(record["postbuild_refreshes"]), 1)
        self.report["dependent_packages_to_save"] = []
        clean = self.runner._save_elm_part_bend_dependencies(
            self.item, self.report, durable_saves=self.ledger,
        )
        self.assertEqual(clean["status"], "verified_clean")
        self.assertEqual(self.saved, self.paths)

    def test_unrelated_or_undeclared_target_fails_before_save(self):
        self.report["dependent_packages_to_save"].append("/Game/Unrelated/UserUnsaved")
        with self.assertRaisesRegex(RuntimeError, "unrelated"):
            self.runner._save_elm_part_bend_dependencies(
                self.item, self.report, durable_saves=self.ledger,
            )
        self.assertEqual(self.saved, [])
        self.report["dependent_packages_to_save"].pop()
        self.item["checkout_asset_paths"].pop()
        with self.assertRaisesRegex(RuntimeError, "immutable checkout"):
            self.runner._save_elm_part_bend_dependencies(
                self.item, self.report, durable_saves=self.ledger,
            )
        self.assertEqual(self.saved, [])

    def test_native_failure_prevents_root_audit_save_and_ready_status(self):
        # The initial provider-wave ledger is clean before the builder invokes
        # its native postbuild hook and makes exactly these four leaves dirty.
        self.dirty.difference_update(self.paths)
        self.fail_at = 2
        root_calls = []

        def build(_unreal, _manifest, _contract):
            self.dirty.update(self.paths)
            return {"assembly": self.policy["assembly"], "material_normalization": self.report}

        self.runner.validate_manifest_artifacts = lambda manifest: None
        self.runner.build_unreal_nanite_assembly = build
        self.runner._material_postbuild_slot_audit = lambda path: root_calls.append("audit")
        self.runner._save_large_assembly_without_thumbnail = lambda *args, **kwargs: root_calls.append("save")
        item = dict(self.item, cluster_assembly={"manifest": {}, "ingest_plan": {"status": "ready"}})
        with self.assertRaisesRegex(RuntimeError, "failed to persist Elm bend dependency"):
            self.runner._finalize_prepared_cluster_assembly(
                item, {"status": "prepared_for_build"}, durable_saves=self.ledger,
            )
        self.assertEqual(self.saved, self.paths[:2])
        self.assertEqual(root_calls, [])
        self.assertNotIn(self.paths[0], self.dirty)
        self.assertIn(self.paths[1], self.dirty)
        self.assertEqual(len(self.ledger["records"][0]["postbuild_refreshes"]), 1)
        self.assertNotIn("postbuild_refreshes", self.ledger["records"][1])

    def test_missing_native_capability_and_non_elm_contract(self):
        self.runner.unreal.CodexMaterialToolsLibrary = SimpleNamespace()
        with self.assertRaisesRegex(RuntimeError, "save API is unavailable"):
            self.runner._save_elm_part_bend_dependencies(
                self.item, self.report, durable_saves=self.ledger,
            )
        self.assertEqual(self.saved, [])
        result = self.runner._save_elm_part_bend_dependencies(
            self.item, {}, durable_saves=self.ledger,
        )
        self.assertEqual(result["status"], "not_requested")


if __name__ == "__main__":
    unittest.main()
