import json
import sys
import tempfile
import unittest
from pathlib import Path


TOOL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOL_DIR))

from pcg_board_snapshot import (
    BOARD_SNAPSHOT_KIND,
    BOARD_SNAPSHOT_MAX_BYTES,
    BOARD_SNAPSHOT_RETENTION_COUNT,
    BOARD_SNAPSHOT_SCHEMA_VERSION,
    read_board_display_snapshot,
    write_board_display_snapshot,
)


class BoardDisplaySnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.snapshot = self.root / "cache" / "board_snapshot_v1.json"
        self.targets_path = self.root / "pcg_targets.json"
        self.source_path = self.root / "pcg_source.json"
        self.source_path.write_text('{"source":1}', encoding="utf-8")
        self.pcg_targets = {
            "source": "unreal_remote_python",
            "source_file": str(self.source_path),
            "generated_at": "2026-07-30T10:00:00",
            "graph": "/Game/PCG/PCG_01",
            "meshes": [{"static_mesh": "/Game/Meshes/tree_01"}],
        }
        self.targets_path.write_text(
            json.dumps(self.pcg_targets),
            encoding="utf-8",
        )
        self.cfg = {
            "tree_root": str(self.root / "Tree"),
            "atlas_root": str(self.root / "Tree" / "atlas"),
            "required_export_maps": ["color", "normal"],
        }

    def tearDown(self):
        self.temporary.cleanup()

    def _write(self, report=None, cfg=None, pcg_targets=None):
        return write_board_display_snapshot(
            report or {"items": []},
            cfg or self.cfg,
            pcg_targets=(
                self.pcg_targets if pcg_targets is None else pcg_targets
            ),
            path=self.snapshot,
            pcg_targets_path=self.targets_path,
        )

    def _read(self, cfg=None, pcg_targets=None):
        return read_board_display_snapshot(
            cfg or self.cfg,
            pcg_targets=(
                self.pcg_targets if pcg_targets is None else pcg_targets
            ),
            path=self.snapshot,
            pcg_targets_path=self.targets_path,
        )

    def test_round_trip_is_json_safe_compact_and_display_only(self):
        report = {
            "items": [{
                "folder": self.root / "Tree" / "tree_elm",
                "_gui_live_evidence": {
                    "sha256": "a" * 64,
                    "files": ["must-not-survive-display-snapshot"],
                },
                "roles": [{
                    "polygon_indices": list(range(1000)),
                    "vertex_indices": {3, 2, 1},
                    "component_polygon_indices": [8, 9],
                    "polygon_count": 1000,
                    "paths": (self.root / "a.spm", self.root / "b.spm"),
                }],
            }],
        }

        written = self._write(report=report)
        self.assertEqual(written, self.snapshot)
        payload = json.loads(self.snapshot.read_text(encoding="utf-8"))
        self.assertEqual(
            payload["schema_version"],
            BOARD_SNAPSHOT_SCHEMA_VERSION,
        )
        self.assertEqual(payload["kind"], BOARD_SNAPSHOT_KIND)
        self.assertIs(payload["display_only"], True)
        role = payload["display_report"]["items"][0]["roles"][0]
        self.assertNotIn(
            "_gui_live_evidence",
            payload["display_report"]["items"][0],
        )
        self.assertNotIn("polygon_indices", role)
        self.assertNotIn("vertex_indices", role)
        self.assertNotIn("component_polygon_indices", role)
        self.assertEqual(role["polygon_count"], 1000)
        self.assertEqual(
            role["paths"],
            [str(self.root / "a.spm"), str(self.root / "b.spm")],
        )

        loaded = self._read()
        self.assertEqual(loaded["cache_state"], "matching_inputs")
        self.assertTrue(loaded["can_display"])
        self.assertTrue(loaded["display_only"])
        self.assertEqual(loaded["mismatch_reasons"], [])
        self.assertNotIn("ready", loaded)
        self.assertNotIn("authority", loaded)

    def test_display_payload_tamper_is_marked_untrusted_and_not_painted(self):
        self._write(report={
            "items": [{
                "folder": str(self.root / "Tree" / "tree_elm"),
                "name": "tree_elm",
                "status": "ready",
            }],
        })
        payload = json.loads(self.snapshot.read_text(encoding="utf-8"))
        payload["display_report"]["items"][0]["status"] = "spoofed"
        self.snapshot.write_text(json.dumps(payload), encoding="utf-8")

        loaded = self._read()

        self.assertEqual(loaded["cache_state"], "untrusted_payload")
        self.assertEqual(loaded["context_state"], "unknown")
        self.assertFalse(loaded["can_display"])

    def test_projection_omits_actual_fleet_heavy_fields_but_keeps_ui_state(self):
        target = {
            "spm": str(self.root / "Tree" / "SK_tree.spm"),
            "generator_connection_complete": False,
            "generator_connection_update_needed": True,
            "source_material_names": ["M_leaf"],
            "source_material_ids": ["17"],
            "source_material_statuses": [
                {"name": "M_leaf", "diagnostic": "x" * 4096}
            ],
            "expected_generator_bindings": [{"rows": list(range(1000))}],
            "source_generator_bindings": [{"rows": list(range(1000))}],
            "generator_bindings": [{"rows": list(range(1000))}],
        }
        report = {
            "items": [{
                "folder": str(self.root / "Tree" / "tree_elm"),
                "name": "tree_elm",
                "status": "needs_atlas",
                "actions": ["② 아틀라스 생성"],
                "leaf_atlas_lineage": {"rows": list(range(1000))},
                "assembly_handoff": {"rows": list(range(1000))},
                "preserved_cluster_materials": [
                    {"payload": "x" * 4096}
                ],
                "cluster_assembly": {
                    "hierarchy": {
                        "name": "Cluster",
                        "path": str(self.root / "Tree" / "Cluster"),
                    },
                    "canonical_bark": {
                        "status": "ready",
                        "canonical_material": "M_bark_tree_01",
                    },
                    "handoff": {
                        "status": "pending_export",
                        "skeleton_wind_contract": {
                            "status": "ready",
                            "lod_count": 4,
                        },
                    },
                    "dependencies": [{
                        "spm": str(self.root / "Tree" / "Cluster" / "a.spm"),
                        "role": "leaf",
                        "decision": "pending_export",
                        "source_mesh_ids": [1, 2],
                        "source_materials": [{
                            "material_name": "M_leaf",
                            "normalized_variants": "x" * 8192,
                        }],
                        "normalized_variants": "x" * 32768,
                    }],
                    "normalized_delivery": "x" * 65536,
                },
                "leaf_mesh_sources": [{
                    "atlas_base": "M_leaf",
                    "atlas_blends": [],
                    "generator_connection_complete": False,
                    "targets": [target],
                }],
            }],
        }
        metrics = {}
        written = write_board_display_snapshot(
            report,
            self.cfg,
            pcg_targets=self.pcg_targets,
            path=self.snapshot,
            pcg_targets_path=self.targets_path,
            metrics=metrics,
        )

        self.assertEqual(written, self.snapshot)
        payload = json.loads(self.snapshot.read_text(encoding="utf-8"))
        item = payload["display_report"]["items"][0]
        projected_target = item["leaf_mesh_sources"][0]["targets"][0]
        self.assertNotIn("leaf_atlas_lineage", item)
        self.assertNotIn("assembly_handoff", item)
        self.assertNotIn("preserved_cluster_materials", item)
        projected_assembly = item["cluster_assembly"]
        self.assertEqual(
            projected_assembly["canonical_bark"]["status"], "ready"
        )
        self.assertEqual(
            projected_assembly["canonical_bark"]["canonical_material"],
            "M_bark_tree_01",
        )
        self.assertEqual(
            projected_assembly["handoff"]["skeleton_wind_contract"],
            {"status": "ready", "lod_count": 4},
        )
        self.assertEqual(
            projected_assembly["dependencies"][0]["role"], "leaf"
        )
        self.assertEqual(
            projected_assembly["dependencies"][0]["source_materials"],
            [{"material_name": "M_leaf"}],
        )
        self.assertNotIn("normalized_delivery", projected_assembly)
        self.assertNotIn(
            "normalized_variants",
            projected_assembly["dependencies"][0],
        )
        self.assertNotIn("source_material_statuses", projected_target)
        self.assertNotIn("expected_generator_bindings", projected_target)
        self.assertNotIn("source_generator_bindings", projected_target)
        self.assertNotIn("generator_bindings", projected_target)
        self.assertEqual(projected_target["spm"], target["spm"])
        self.assertIs(
            projected_target["generator_connection_complete"], False
        )
        self.assertEqual(projected_target["source_material_names"], ["M_leaf"])
        self.assertEqual(item["status"], "needs_atlas")
        self.assertEqual(item["actions"], ["② 아틀라스 생성"])
        self.assertTrue(metrics["written"])
        self.assertGreaterEqual(metrics["omitted_field_count"], 6)
        self.assertLessEqual(
            metrics["candidate_bytes"], BOARD_SNAPSHOT_MAX_BYTES
        )

    def test_canceled_generation_does_not_replace_last_good_snapshot(self):
        self._write(report={"items": [{"name": "last-good"}]})
        previous = self.snapshot.read_bytes()
        metrics = {}

        written = write_board_display_snapshot(
            {"items": [{"name": "superseded"}]},
            self.cfg,
            pcg_targets=self.pcg_targets,
            path=self.snapshot,
            pcg_targets_path=self.targets_path,
            metrics=metrics,
            publish_check=lambda: False,
        )

        self.assertIsNone(written)
        self.assertEqual(self.snapshot.read_bytes(), previous)
        self.assertEqual(metrics["reason"], "publication_canceled")

    def test_context_changes_are_returned_as_stale_display_metadata(self):
        self._write()

        changed_cfg = dict(self.cfg)
        changed_cfg["required_export_maps"] = ["color", "normal", "height"]
        changed = self._read(cfg=changed_cfg)
        self.assertEqual(changed["cache_state"], "stale_inputs")
        self.assertTrue(changed["can_display"])
        self.assertIn("config_fingerprint", changed["mismatch_reasons"])

        other_root = dict(self.cfg)
        other_root["tree_root"] = str(self.root / "OtherTree")
        changed = self._read(cfg=other_root)
        self.assertIn("tree_root", changed["mismatch_reasons"])
        self.assertIn("config_fingerprint", changed["mismatch_reasons"])

    def test_pcg_payload_target_file_and_declared_source_are_checked(self):
        self._write()

        changed_payload = dict(self.pcg_targets)
        changed_payload["meshes"] = [{"static_mesh": "/Game/Meshes/tree_02"}]
        changed = self._read(pcg_targets=changed_payload)
        self.assertIn("pcg_target_payload", changed["mismatch_reasons"])

        self.targets_path.write_text(
            json.dumps({**self.pcg_targets, "revision": 2}),
            encoding="utf-8",
        )
        changed = self._read()
        self.assertIn("pcg_target_file", changed["mismatch_reasons"])

        self.targets_path.write_text(
            json.dumps(self.pcg_targets),
            encoding="utf-8",
        )
        self.source_path.write_text('{"source":2}', encoding="utf-8")
        changed = self._read()
        self.assertIn("pcg_declared_source", changed["mismatch_reasons"])

    def test_missing_unreadable_and_incompatible_cache_cannot_display(self):
        missing = self._read()
        self.assertEqual(missing["cache_state"], "missing")
        self.assertFalse(missing["can_display"])

        self.snapshot.parent.mkdir(parents=True)
        self.snapshot.write_text("{not json", encoding="utf-8")
        unreadable = self._read()
        self.assertEqual(unreadable["cache_state"], "unreadable")
        self.assertFalse(unreadable["can_display"])

        self.snapshot.write_text(
            json.dumps({
                "schema_version": BOARD_SNAPSHOT_SCHEMA_VERSION + 1,
                "kind": BOARD_SNAPSHOT_KIND,
                "display_only": True,
                "display_report": {},
            }),
            encoding="utf-8",
        )
        incompatible = self._read()
        self.assertEqual(incompatible["cache_state"], "incompatible")
        self.assertFalse(incompatible["can_display"])

    def test_atomic_replace_leaves_only_the_latest_complete_snapshot(self):
        self._write(report={"items": [{"name": "first"}]})
        self._write(report={"items": [{"name": "second"}]})

        loaded = self._read()
        self.assertEqual(
            loaded["display_report"]["items"][0]["name"],
            "second",
        )
        self.assertEqual(
            list(self.snapshot.parent.glob(f".{self.snapshot.name}.*.tmp")),
            [],
        )

    def test_size_budget_discards_candidate_and_retains_one_last_good_file(self):
        self.assertEqual(BOARD_SNAPSHOT_RETENTION_COUNT, 1)
        self._write(report={"items": [{"name": "last-good"}]})
        previous = self.snapshot.read_bytes()
        self.assertLessEqual(len(previous), BOARD_SNAPSHOT_MAX_BYTES)

        written = write_board_display_snapshot(
            {"items": [{"diagnostic": "x" * (len(previous) + 1024)}]},
            self.cfg,
            pcg_targets=self.pcg_targets,
            path=self.snapshot,
            pcg_targets_path=self.targets_path,
            max_bytes=len(previous) + 64,
        )

        self.assertIsNone(written)
        self.assertEqual(self.snapshot.read_bytes(), previous)
        self.assertEqual(
            list(self.snapshot.parent.glob("board_snapshot_v1*.json")),
            [self.snapshot],
        )
        oversized = read_board_display_snapshot(
            self.cfg,
            pcg_targets=self.pcg_targets,
            path=self.snapshot,
            pcg_targets_path=self.targets_path,
            max_bytes=len(previous) - 1,
        )
        self.assertEqual(oversized["cache_state"], "oversized")
        self.assertFalse(oversized["can_display"])

    def test_all_asset_board_does_not_depend_on_unused_pcg_file(self):
        write_board_display_snapshot(
            {"items": []},
            self.cfg,
            pcg_targets=None,
            path=self.snapshot,
            pcg_targets_path=self.targets_path,
        )
        self.targets_path.write_text('{"changed":true}', encoding="utf-8")
        loaded = read_board_display_snapshot(
            self.cfg,
            pcg_targets=None,
            path=self.snapshot,
            pcg_targets_path=self.targets_path,
        )
        self.assertEqual(loaded["cache_state"], "matching_inputs")


if __name__ == "__main__":
    unittest.main()
