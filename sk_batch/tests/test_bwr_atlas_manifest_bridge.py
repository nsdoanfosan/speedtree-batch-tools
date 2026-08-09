import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


REPO = Path(__file__).resolve().parents[2]
SK_BATCH = REPO / "sk_batch"
for path in (REPO, SK_BATCH):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from bwr_atlas_manifest_bridge import (  # noqa: E402
    install_bwr_atlas_manifest_resolver,
)


class FakeAddonRuntime:
    def __init__(self, core):
        self.core = core

    def operation(self, addon_id, operation_name):
        if addon_id != "speedtree_bone_weight_repair":
            raise AssertionError(addon_id)
        if operation_name != "speedtree_manifest_paths":
            raise AssertionError(operation_name)
        return self.core._speedtree_manifest_paths

    def replace_operation(self, addon_id, operation_name, replacement):
        previous = self.operation(addon_id, operation_name)
        self.core._speedtree_manifest_paths = replacement
        return previous


class BwrAtlasManifestBridgeTests(unittest.TestCase):
    def test_foreign_rolling_global_is_not_exposed_to_exact_bwr_target(self):
        fixture_path = (
            REPO
            / "tests"
            / "fixtures"
            / "issue58_ivy_foreign_global_manifest.json"
        )
        fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / fixture["asset_folder"]
            root.mkdir()
            target = root / fixture["target_spm"]
            foreign_target = root / fixture["foreign_spm"]
            target.write_bytes(b"target")
            foreign_target.write_bytes(b"foreign")

            def materialize(value):
                if isinstance(value, dict):
                    return {
                        key: materialize(item) for key, item in value.items()
                    }
                if isinstance(value, list):
                    return [materialize(item) for item in value]
                if isinstance(value, str):
                    return value.replace("{root}", str(root)).replace(
                        "{foreign_target}", str(foreign_target)
                    )
                return value

            global_path = root / "speedtree_import_manifest.json"
            global_path.write_text(
                json.dumps(materialize(fixture["foreign_global"])),
                encoding="utf-8",
            )
            legacy_calls = []

            def legacy_paths(source_fbx_path, stmat_material=None):
                legacy_calls.append((source_fbx_path, stmat_material))
                return [global_path]

            bwr_core = SimpleNamespace(
                _speedtree_manifest_paths=legacy_paths
            )
            evidence = install_bwr_atlas_manifest_resolver(
                FakeAddonRuntime(bwr_core),
                target,
            )

            self.assertEqual(evidence["selected"], [])
            self.assertEqual(evidence["selected_manifest_paths"], [])
            self.assertIn(
                (str(global_path.resolve()), "different_target_spm"),
                {
                    (row["path"], row["reason"])
                    for row in evidence["rejected"]
                },
            )
            self.assertEqual(
                bwr_core._speedtree_manifest_paths(
                    root / "fbx" / f"{target.stem}.fbx"
                ),
                [],
            )
            self.assertEqual(legacy_calls, [])

            unrelated = root / "fbx" / "SK_unrelated.fbx"
            self.assertEqual(
                bwr_core._speedtree_manifest_paths(unrelated),
                [global_path],
            )
            self.assertEqual(len(legacy_calls), 1)

    def test_headless_job_installs_bridge_before_repair(self):
        source = (
            SK_BATCH / "jobs" / "bwr_headless_job.py"
        ).read_text(encoding="utf-8")
        install_at = source.index("install_bwr_atlas_manifest_resolver(")
        repair_at = source.index("run_import_and_repair(repair_settings)")
        self.assertLess(install_at, repair_at)

    def test_provider_disagreement_is_diagnostic_and_export_remains_reachable(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "SK_leaf_test_01.spm"
            target.write_bytes(b"spm")
            payload = {
                "atlas_manifest_schema_version": 1,
                "spm": str(target),
                "blend_file": str(root / "atlas.blend"),
                "source_collection": "Leaf A",
                "export_scope_id": "leaf-a",
                "material_groups": [{
                    "material": "M_leaf_a",
                    "material_id": 7,
                    "mesh_ids": [20],
                }],
                "generator_connection": {"complete": True, "bindings": []},
            }
            target_dir = root / ".atlas_leaf_speedtree_targets"
            target_dir.mkdir()
            (target_dir / f"{target.stem}.json").write_text(
                json.dumps(payload), encoding="utf-8"
            )
            conflicting = json.loads(json.dumps(payload))
            conflicting["material_groups"][0]["mesh_ids"] = [99]
            scope_dir = root / ".atlas_leaf_speedtree_scopes"
            scope_dir.mkdir()
            (scope_dir / f"leaf-a__{target.stem}.json").write_text(
                json.dumps(conflicting), encoding="utf-8"
            )
            export = mock.Mock(return_value={"status": "ok"})
            bwr_core = SimpleNamespace(
                _speedtree_manifest_paths=lambda *_args: [],
                run_speedtree_cli_export=export,
            )

            evidence = install_bwr_atlas_manifest_resolver(
                FakeAddonRuntime(bwr_core), target
            )
            result = bwr_core.run_speedtree_cli_export(target)

            self.assertEqual(result, {"status": "ok"})
            export.assert_called_once_with(target)
            self.assertTrue(evidence["diagnostic_only"])
            self.assertFalse(evidence["mutation_authorized"])
            self.assertEqual(evidence["selected_manifest_paths"], [])
            self.assertTrue(evidence["conflicting"])

    def test_projected_disjoint_claim_never_reenters_bwr_through_original_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "SK_leaf_disjoint_test_01.spm"
            target.write_bytes(b"spm")
            authority = {
                "atlas_manifest_schema_version": 1,
                "spm": str(target),
                "blend_file": str(root / "provider_a.blend"),
                "source_collection": "Provider A",
                "export_scope_id": "provider-a",
                "material_groups": [{
                    "material": "M_shared",
                    "material_id": 7,
                    "mesh_ids": [20],
                }],
                "generator_connection": {
                    "complete": True,
                    "bindings": [],
                },
            }
            target_dir = root / ".atlas_leaf_speedtree_targets"
            target_dir.mkdir()
            (target_dir / f"{target.stem}.json").write_text(
                json.dumps(authority),
                encoding="utf-8",
            )
            mixed = json.loads(json.dumps(authority))
            mixed["blend_file"] = str(root / "provider_b.blend")
            mixed["source_collection"] = "Provider B"
            mixed["export_scope_id"] = "provider-b"
            mixed["material_groups"] = [
                {
                    "material": "M_shared",
                    "material_id": 7,
                    "mesh_ids": [99],
                },
                {
                    "material": "M_unique_b",
                    "material_id": 8,
                    "mesh_ids": [30],
                },
            ]
            scope_dir = root / ".atlas_leaf_speedtree_scopes"
            scope_dir.mkdir()
            scope_path = scope_dir / f"provider-b__{target.stem}.json"
            scope_path.write_text(json.dumps(mixed), encoding="utf-8")
            bwr_core = SimpleNamespace(
                _speedtree_manifest_paths=lambda *_args: [],
            )

            evidence = install_bwr_atlas_manifest_resolver(
                FakeAddonRuntime(bwr_core), target
            )

            self.assertIn(
                str(scope_path.resolve()),
                evidence["projected_manifest_paths_withheld"],
            )
            self.assertNotIn(
                str(scope_path.resolve()),
                evidence["selected_manifest_paths"],
            )
            self.assertEqual(
                bwr_core._speedtree_manifest_paths(
                    root / "fbx" / f"{target.stem}.fbx"
                ),
                [],
            )


if __name__ == "__main__":
    unittest.main()
