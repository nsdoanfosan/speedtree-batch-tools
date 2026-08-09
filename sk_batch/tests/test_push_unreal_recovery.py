import copy
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SK_BATCH_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SK_BATCH_DIR))

from push_unreal_recovery import (  # noqa: E402
    PushUnrealRecoveryError,
    code_file_identity,
    dependency_closure,
    load_parent_manifest,
    recover_manifest_item,
)
import push_unreal_recovery as recovery_module  # noqa: E402


class PushUnrealRecoveryTests(unittest.TestCase):
    @staticmethod
    def snapshot(blend, dependencies):
        def record(path):
            stat = Path(path).stat()
            return {
                "path": str(Path(path).resolve()),
                "size": stat.st_size,
                "mtime_ns": stat.st_mtime_ns,
            }

        return {
            "blend": record(blend),
            "dependencies": [record(path) for path in dependencies],
        }

    @staticmethod
    def parent_item(blend, exported, handoff, runtime_code):
        return {
            "schema_version": 1,
            "source_fingerprint": "parent-source",
            "blend": str(blend.resolve()),
            "unit_name": "SK_test",
            "unreal_folder": "/Game/Test/",
            "mesh_path": "/Game/Test/SK_test",
            "assets": [],
            "exported_files": [code_file_identity(exported)],
            "handoff_files": [code_file_identity(handoff)],
            "wind_file": None,
            "wind_policy": {"requires_json": False},
            "code_files": [code_file_identity(runtime_code)],
            "cluster_assembly": None,
            "dependency_orchestrated": False,
            "material_asset_scope": None,
            "queue_id": "SK_test.spm",
            "fingerprint": "parent-item",
            "checkout_asset_paths": ["/Game/Test/SK_test"],
            "report_path": "old-item-report.json",
            "export_report_path": "old-export-report.json",
        }

    def test_retry_manifest_rejects_tool_owned_backup_queue_item(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            rollback = (
                root
                / (
                    "SK_weed_reed_02.texture_slot_backup_"
                    "20260801_010203_123456.spm"
                )
            )
            rollback.write_bytes(b"rollback")
            manifest = root / "failed_push_manifest.json"
            manifest.write_text(
                json.dumps({
                    "schema_version": 1,
                    "items": [{
                        "schema_version": 1,
                        "queue_id": str(rollback),
                    }],
                }),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                PushUnrealRecoveryError,
                "ineligible SPM artifact",
            ):
                load_parent_manifest(manifest)

    def test_runtime_code_change_reuses_verified_artifacts_in_new_item(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            blend = root / "source.blend"
            immutable = root / "export_contract.py"
            runtime = root / "unreal_ingest.py"
            exported = root / "mesh.fbx"
            handoff = root / "material.json"
            for path, content in (
                (blend, b"blend-v1"),
                (immutable, b"immutable-v1"),
                (runtime, b"runtime-v1"),
                (exported, b"fbx-v1"),
                (handoff, b"json-v1"),
            ):
                path.write_bytes(content)
            parent_snapshot = self.snapshot(blend, [immutable, runtime])
            parent_item = self.parent_item(blend, exported, handoff, runtime)
            untouched_parent = copy.deepcopy(parent_item)

            runtime.write_bytes(b"runtime-v2-current")
            current_snapshot = self.snapshot(blend, [immutable, runtime])
            result = recover_manifest_item(
                parent_item,
                parent_manifest_path=root / "parent.json",
                parent_report_path=root / "parent_report.json",
                parent_source_record={
                    "fingerprint": "parent-source",
                    "snapshot": parent_snapshot,
                },
                current_source_record={
                    "fingerprint": "current-source",
                    "snapshot": current_snapshot,
                },
                current_source_fingerprint="current-source",
                runtime_code_paths=[runtime],
                rebindable_code_paths=[runtime],
                report_path=root / "retry_report.json",
                selected=True,
                recovered_at="2026-08-01T12:00:00",
            )

            self.assertEqual(parent_item, untouched_parent)
            self.assertEqual(result["source_fingerprint"], "current-source")
            self.assertNotEqual(result["fingerprint"], "parent-item")
            self.assertNotEqual(
                result["recovery"]["old_code_revision"],
                result["recovery"]["new_code_revision"],
            )
            self.assertEqual(
                result["recovery"]["parent_item_fingerprint"], "parent-item"
            )
            self.assertFalse(result["verify_existing_assets"])
            self.assertEqual(
                result["exported_files"], untouched_parent["exported_files"]
            )

    def test_export_time_dependency_change_requires_full_push(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            blend = root / "source.blend"
            immutable = root / "send2ue_push_job.py"
            runtime = root / "unreal_ingest.py"
            exported = root / "mesh.fbx"
            handoff = root / "material.json"
            for path in (blend, immutable, runtime, exported, handoff):
                path.write_bytes(path.name.encode("utf-8"))
            parent_snapshot = self.snapshot(blend, [immutable, runtime])
            parent_item = self.parent_item(blend, exported, handoff, runtime)
            immutable.write_bytes(b"changed export contract")

            with self.assertRaisesRegex(
                PushUnrealRecoveryError, "export-time dependency changed"
            ):
                recover_manifest_item(
                    parent_item,
                    parent_manifest_path=root / "parent.json",
                    parent_report_path="",
                    parent_source_record={
                        "fingerprint": "parent-source",
                        "snapshot": parent_snapshot,
                    },
                    current_source_record={
                        "fingerprint": "current-source",
                        "snapshot": self.snapshot(blend, [immutable, runtime]),
                    },
                    current_source_fingerprint="current-source",
                    runtime_code_paths=[runtime],
                    rebindable_code_paths=[runtime],
                    report_path=root / "retry.json",
                    selected=True,
                )

    def test_exporter_code_change_reuses_verified_immutable_artifacts(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            blend = root / "source.blend"
            exporter = root / "send2ue_material_pipeline.py"
            runtime = root / "unreal_ingest.py"
            repair_report = root / "repair_report.json"
            exported = root / "mesh.fbx"
            handoff = root / "material.json"
            for path, content in (
                (blend, b"blend-v1"),
                (exporter, b"exporter-v1"),
                (runtime, b"runtime-v1"),
                (repair_report, b"repair-v1"),
                (exported, b"fbx-v1"),
                (handoff, b"json-v1"),
            ):
                path.write_bytes(content)
            parent_snapshot = self.snapshot(
                blend, [exporter, runtime, repair_report]
            )
            parent_item = self.parent_item(blend, exported, handoff, runtime)

            exporter.write_bytes(b"exporter-v2-after-complete-export")
            current_snapshot = self.snapshot(
                blend, [exporter, runtime, repair_report]
            )
            result = recover_manifest_item(
                parent_item,
                parent_manifest_path=root / "parent.json",
                parent_report_path=root / "parent_report.json",
                parent_source_record={
                    "fingerprint": "parent-source",
                    "snapshot": parent_snapshot,
                },
                current_source_record={
                    "fingerprint": "current-source",
                    "snapshot": current_snapshot,
                },
                current_source_fingerprint="current-source",
                runtime_code_paths=[runtime],
                rebindable_code_paths=[runtime, exporter],
                report_path=root / "retry.json",
                selected=True,
            )

            self.assertEqual(
                result["recovery"]["rebound_source_code_paths"],
                [str(exporter.resolve())],
            )
            self.assertEqual(
                result["exported_files"], parent_item["exported_files"]
            )

    def test_source_data_change_still_requires_full_push(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            blend = root / "source.blend"
            exporter = root / "send2ue_material_pipeline.py"
            runtime = root / "unreal_ingest.py"
            repair_report = root / "repair_report.json"
            exported = root / "mesh.fbx"
            handoff = root / "material.json"
            for path in (
                blend,
                exporter,
                runtime,
                repair_report,
                exported,
                handoff,
            ):
                path.write_bytes(path.name.encode("utf-8"))
            parent_snapshot = self.snapshot(
                blend, [exporter, runtime, repair_report]
            )
            parent_item = self.parent_item(blend, exported, handoff, runtime)
            repair_report.write_bytes(b"changed per-asset repair data")

            with self.assertRaisesRegex(
                PushUnrealRecoveryError, "export-time dependency changed"
            ):
                recover_manifest_item(
                    parent_item,
                    parent_manifest_path=root / "parent.json",
                    parent_report_path="",
                    parent_source_record={
                        "fingerprint": "parent-source",
                        "snapshot": parent_snapshot,
                    },
                    current_source_record={
                        "fingerprint": "current-source",
                        "snapshot": self.snapshot(
                            blend, [exporter, runtime, repair_report]
                        ),
                    },
                    current_source_fingerprint="current-source",
                    runtime_code_paths=[runtime],
                    rebindable_code_paths=[runtime, exporter],
                    report_path=root / "retry.json",
                    selected=True,
                )

    def test_artifact_content_change_is_rejected_even_when_stat_is_restored(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            blend = root / "source.blend"
            runtime = root / "unreal_ingest.py"
            exported = root / "mesh.fbx"
            handoff = root / "material.json"
            for path, content in (
                (blend, b"blend"),
                (runtime, b"runtime"),
                (exported, b"AAAA"),
                (handoff, b"json"),
            ):
                path.write_bytes(content)
            parent_snapshot = self.snapshot(blend, [runtime])
            parent_item = self.parent_item(blend, exported, handoff, runtime)
            original_stat = exported.stat()
            exported.write_bytes(b"BBBB")
            os.utime(
                exported,
                ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns),
            )

            with self.assertRaisesRegex(
                PushUnrealRecoveryError, "exported file #1 content changed"
            ):
                recover_manifest_item(
                    parent_item,
                    parent_manifest_path=root / "parent.json",
                    parent_report_path="",
                    parent_source_record={
                        "fingerprint": "parent-source",
                        "snapshot": parent_snapshot,
                    },
                    current_source_record={
                        "fingerprint": "current-source",
                        "snapshot": self.snapshot(blend, [runtime]),
                    },
                    current_source_fingerprint="current-source",
                    runtime_code_paths=[runtime],
                    rebindable_code_paths=[runtime],
                    report_path=root / "retry.json",
                    selected=True,
                )

    def test_parent_schema_mismatch_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "parent.json"
            path.write_text(
                json.dumps({"schema_version": 99, "items": [{}]}),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                PushUnrealRecoveryError, "schema is incompatible"
            ):
                load_parent_manifest(path)

    def test_assembly_plan_sidecars_and_checkout_paths_are_regenerated(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            blend = root / "source.blend"
            runtime = root / "cluster_assembly_builder.py"
            exported = root / "full.fbx"
            handoff = root / "full.json"
            generated = root / "part.json"
            for path in (blend, runtime, exported, handoff, generated):
                path.write_bytes(path.name.encode("utf-8"))
            parent_snapshot = self.snapshot(blend, [runtime])
            parent_item = self.parent_item(blend, exported, handoff, runtime)
            parent_item.update({
                "mesh_path": "/Game/Test/SK_test",
                "assets": [{
                    "asset_data": {
                        "_asset_type": "SkeletalMesh",
                        "asset_path": "/Game/Test/SK_test",
                    }
                }],
                "cluster_assembly": {
                    "manifest": {"status": "ready"},
                    "ingest_plan": {"status": "old"},
                },
            })
            new_plan = {
                "status": "ready",
                "assets": [{
                    "asset_data": {
                        "_material_pipeline_json_path": str(generated),
                    }
                }],
                "asset_contract": {
                    "base_skeletal_mesh": "/Game/Test/Assembly/SK_Base",
                    "assembly": "/Game/Test/Assembly/SK_test_NaniteAssembly",
                    "parts": {"leaf": "/Game/Test/Assembly/SK_Leaf"},
                },
            }
            source_record = {
                "fingerprint": "parent-source",
                "snapshot": parent_snapshot,
            }

            with mock.patch.object(
                recovery_module, "validate_manifest_artifacts"
            ), mock.patch.object(
                recovery_module,
                "build_unreal_ingest_plan",
                return_value=new_plan,
            ) as build_plan:
                result = recover_manifest_item(
                    parent_item,
                    parent_manifest_path=root / "parent.json",
                    parent_report_path=root / "parent_report.json",
                    parent_source_record=source_record,
                    current_source_record=source_record,
                    current_source_fingerprint="parent-source",
                    runtime_code_paths=[runtime],
                    rebindable_code_paths=[runtime],
                    report_path=root / "retry.json",
                    selected=True,
                )

            build_plan.assert_called_once()
            self.assertEqual(
                result["cluster_assembly"]["ingest_plan"], new_plan
            )
            self.assertIn(
                str(generated.resolve()),
                [record["path"] for record in result["handoff_files"]],
            )
            self.assertIn(
                "/Game/Test/Assembly/SK_Leaf_Skeleton",
                result["checkout_asset_paths"],
            )

    def test_dependency_closure_adds_only_required_providers(self):
        items = {
            "cluster": {"queue_id": "cluster", "depends_on_queue_ids": []},
            "tree": {
                "queue_id": "tree",
                "depends_on_queue_ids": ["cluster"],
            },
            "other": {"queue_id": "other", "depends_on_queue_ids": []},
        }
        self.assertEqual(dependency_closure(items, ["tree"]), {"tree", "cluster"})


if __name__ == "__main__":
    unittest.main()
