import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace


REPO = Path(__file__).resolve().parents[2]
SK_BATCH = REPO / "sk_batch"
for path in (REPO, SK_BATCH):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from bwr_atlas_manifest_bridge import (  # noqa: E402
    install_bwr_atlas_manifest_resolver,
)


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
                bwr_core,
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
        repair_at = source.index("bwr_core.run_import_and_repair(")
        self.assertLess(install_at, repair_at)


if __name__ == "__main__":
    unittest.main()
