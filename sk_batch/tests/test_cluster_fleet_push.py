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
    build_repair_command,
    discover_current_cluster_targets,
    validate_repair_result,
    validate_live_result,
)


class ClusterFleetPushTests(unittest.TestCase):
    def test_fleet_repair_command_precedes_push_with_current_inputs(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            spm = root / "Tree_01.spm"
            blend = spm.with_suffix(".blend")
            blender = root / "blender.exe"
            contract = root / "material.json"
            report = root / "repair.json"
            for path in (spm, blend, blender, contract):
                path.write_bytes(b"current")

            command = build_repair_command(
                {"spm": spm}, blender, contract, report
            )

            self.assertIn("bwr_headless_job.py", " ".join(command))
            self.assertEqual(command[command.index("--spm") + 1], str(spm))
            self.assertEqual(
                command[command.index("--material-contract") + 1],
                str(contract),
            )

    def test_repair_result_requires_complete_v3_binding_and_plan_free_base(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            report = root / "repair.json"
            manifest = root / "assembly.json"
            report.write_text(json.dumps({"status": "ok"}), encoding="utf-8")
            manifest.write_text(json.dumps({
                "placement_contract": {
                    "authored_node_assignment": {
                        "policy": (
                            "deterministic_state_mesh_then_global_position_"
                            "recovery_one_to_one_v3"
                        ),
                        "assigned_count": 2,
                        "unmatched_count": 0,
                    },
                    "degraded_authored_card_binding_count": 0,
                },
                "base": {
                    "unmatched_role_components_removed_from_base": 3,
                },
                "preserved_render_components": [{"polygon_count": 3}],
                "parts": [{"bindings": [{"id": 1}, {"id": 2}]}],
            }), encoding="utf-8")

            result = validate_repair_result(
                report, {"manifest": manifest}
            )

            self.assertTrue(result["ok"])
            self.assertEqual(result["bindings"], 2)

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

    def test_incomplete_current_manifest_is_reported_as_diagnostic(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            assembly = root / "tree_sample" / "assembly"
            assembly.mkdir(parents=True)
            manifest = assembly / "SK_tree_sample_01_cluster_assembly_bindings.json"
            manifest.write_text(json.dumps({
                "status": "ready",
                "content_decision": "build",
                "full_asset_stem": "SK_tree_sample_01",
                "parts": [],
            }), encoding="utf-8")

            targets, missing = discover_current_cluster_targets(root)

        self.assertFalse(targets)
        self.assertEqual(
            missing[0]["diagnostic"],
            "ready_build_manifest_has_no_parts_or_stem",
        )
        self.assertNotIn("reason", missing[0])


if __name__ == "__main__":
    unittest.main()
