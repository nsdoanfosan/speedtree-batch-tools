from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


SK_BATCH = Path(__file__).resolve().parents[1]
if str(SK_BATCH) not in sys.path:
    sys.path.insert(0, str(SK_BATCH))

from cluster_fleet_push import (  # noqa: E402
    discover_current_cluster_targets,
    validate_live_result,
)


class ClusterFleetPushTests(unittest.TestCase):
    def _manifest(self, root, asset, stem, *, birch=False):
        asset_dir = root / asset
        assembly = asset_dir / "assembly"
        assembly.mkdir(parents=True)
        spm = asset_dir / f"{stem}.spm"
        blend = spm.with_suffix(".blend")
        wind = asset_dir / "JSON" / f"{stem}_wind.json"
        wind.parent.mkdir()
        for path in (spm, blend, wind):
            path.write_bytes(b"current")
        manifest = assembly / f"{stem}_cluster_assembly_bindings.json"
        manifest.write_text(json.dumps({
            "status": "ready",
            "content_decision": "build",
            "full_asset_stem": stem,
            "parts": [{"bindings": [{"id": 1}]}],
            "wind_contract": {"wind_json": {"path": str(wind)}},
        }), encoding="utf-8")
        return manifest

    def test_discovers_only_direct_current_manifests_and_sorts_birch_last(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._manifest(root, "tree_birch_paper", "SK_tree_birch_paper_01")
            self._manifest(root, "tree_willow", "SK_tree_Weeping_Willow_01")
            backup = root / "tree_willow" / "backup" / "assembly"
            backup.mkdir(parents=True)
            (backup / "SK_old_cluster_assembly_bindings.json").write_text("{}")

            targets, missing = discover_current_cluster_targets(root)

        self.assertFalse(missing)
        self.assertEqual(
            [row["stem"] for row in targets],
            ["SK_tree_Weeping_Willow_01", "SK_tree_birch_paper_01"],
        )

    def test_live_result_requires_parts_bindings_wind_and_provenance(self):
        target = {"expected_parts": 2, "expected_bindings": 3}
        report = {
            "status": "ok",
            "unreal_result": {
                "status": "imported_ok",
                "cluster_assembly": {"build": {
                    "status": "ok",
                    "assembly": "/Game/Test/Assembly",
                    "parts": [{"bindings": 1}, {"bindings": 2}],
                    "binding_count": 3,
                    "dynamic_wind": {"success": True},
                    "provenance": {"success": True},
                }},
            },
        }

        result = validate_live_result(report, target)

        self.assertTrue(result["ok"])


if __name__ == "__main__":
    unittest.main()
