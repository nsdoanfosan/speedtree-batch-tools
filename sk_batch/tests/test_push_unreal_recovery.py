import copy
import hashlib
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


class FakeSidecarContract:
    @staticmethod
    def build_sidecar_descriptor(mesh_name, source=None):
        result = {
            "kind": "speedtree",
            "version": 3,
            "fingerprint": "current-test-contract",
            "asset_kind": "speedtree",
            "mesh_name": mesh_name,
        }
        if source:
            result["source"] = copy.deepcopy(source)
        return result

    @classmethod
    def validate_sidecar_descriptor(cls, value, expected_mesh_name):
        expected = cls.build_sidecar_descriptor(
            expected_mesh_name,
            source=(value.get("source") if isinstance(value, dict) else None),
        )
        if value != expected:
            raise ValueError("descriptor does not match current contract")
        return value


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

    @staticmethod
    def authoritative_pipeline(mesh_name="SK_test"):
        source = {
            "spm": {
                "canonical_path": "C:/source/SK_test.spm",
                "sha256": "a" * 64,
                "size": 10,
            },
            "stmat": [],
        }
        return {
            "source": source,
            "speedtree_handoff_contract": (
                FakeSidecarContract.build_sidecar_descriptor(
                    mesh_name,
                    source=source,
                )
            ),
        }

    @staticmethod
    def material_asset(sidecar, mesh_name):
        sidecar_sha256 = hashlib.sha256(sidecar.read_bytes()).hexdigest()
        sidecar_argument = sidecar.as_posix()
        asset_path = "/Game/Test/" + mesh_name
        command = (
            "_p.process_mesh("
            f"_asset_path, json_path={sidecar_argument!r}, "
            f"expected_mesh_name={mesh_name!r}, "
            f"sidecar_sha256={sidecar_sha256!r})"
        )
        return {
            "asset_data": {
                "_asset_type": "SkeletalMesh",
                "asset_path": asset_path,
                "_material_pipeline_expected_mesh_name": mesh_name,
                "_material_pipeline_json_path": str(sidecar),
                "_material_pipeline_json_sha256": sidecar_sha256,
            },
            "pre_import_commands": [[command]],
            "post_import_commands": [[command]],
        }

    def recover_current_item(
        self,
        root,
        parent_item,
        blend,
        runtime,
        *,
        authoritative_pipeline_contract=None,
    ):
        source_record = {
            "fingerprint": "parent-source",
            "snapshot": self.snapshot(blend, [runtime]),
        }
        return recover_manifest_item(
            parent_item,
            parent_manifest_path=root / "parent.json",
            parent_report_path=root / "parent_report.json",
            parent_source_record=source_record,
            current_source_record=source_record,
            current_source_fingerprint="parent-source",
            runtime_code_paths=[runtime],
            rebindable_code_paths=[runtime],
            report_path=root / "retry_report.json",
            selected=True,
            sidecar_contract_api=FakeSidecarContract,
            authoritative_pipeline_contract=(
                self.authoritative_pipeline()
                if authoritative_pipeline_contract is None
                else authoritative_pipeline_contract
            ),
        )

    @staticmethod
    def write_recovery_inputs(
        root,
        *,
        descriptor,
        mesh_name="SK_test",
        tree_material=False,
    ):
        blend = root / "source.blend"
        runtime = root / "unreal_ingest.py"
        exported = root / "mesh.fbx"
        sidecar = root / "material.json"
        blend.write_bytes(b"blend")
        runtime.write_bytes(b"runtime")
        exported.write_bytes(b"fbx")
        payload = {
            "schema_version": 3,
            "mesh_name": mesh_name,
            "material_master": "tree" if tree_material else "prop",
            "materials": [{
                "name": "M_test",
                **({"master_preset": "tree"} if tree_material else {}),
            }],
        }
        if descriptor is not None:
            payload["speedtree_handoff_contract"] = descriptor
        sidecar.write_text(json.dumps(payload), encoding="utf-8")
        return blend, runtime, exported, sidecar

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

    def test_current_non_assembly_sidecar_is_preserved(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            pipeline = self.authoritative_pipeline()
            descriptor = pipeline["speedtree_handoff_contract"]
            blend, runtime, exported, sidecar = self.write_recovery_inputs(
                root,
                descriptor=descriptor,
            )
            parent_item = self.parent_item(
                blend, exported, sidecar, runtime
            )
            parent_item["assets"] = [
                self.material_asset(sidecar, "SK_test")
            ]

            result = self.recover_current_item(
                root,
                parent_item,
                blend,
                runtime,
                authoritative_pipeline_contract=pipeline,
            )

            self.assertEqual(
                result["assets"][0]["asset_data"][
                    "_material_pipeline_json_path"
                ],
                str(sidecar),
            )
            self.assertEqual(result["recovery"]["regenerated_sidecars"], [])
            self.assertEqual(
                result["recovery"]["sidecar_contracts"][0][
                    "descriptor_status"
                ],
                "current",
            )

    def test_missing_descriptor_is_rebound_without_parent_mutation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            pipeline = self.authoritative_pipeline(
                "SK_weed_deadbranches_a_01"
            )
            blend, runtime, exported, sidecar = self.write_recovery_inputs(
                root,
                descriptor=None,
                mesh_name="SK_weed_deadbranches_a_01",
            )
            parent_item = self.parent_item(
                blend, exported, sidecar, runtime
            )
            material_asset = self.material_asset(
                sidecar,
                "SK_weed_deadbranches_a_01",
            )
            material_asset["asset_data"]["file_path"] = str(exported)
            parent_item["assets"] = [material_asset]
            parent_before = copy.deepcopy(parent_item)
            sidecar_before = sidecar.read_bytes()

            result = self.recover_current_item(
                root,
                parent_item,
                blend,
                runtime,
                authoritative_pipeline_contract=pipeline,
            )

            recovered_data = result["assets"][0]["asset_data"]
            recovered_sidecar = Path(
                recovered_data["_material_pipeline_json_path"]
            )
            recovered_bytes = recovered_sidecar.read_bytes()
            recovered_sha256 = hashlib.sha256(recovered_bytes).hexdigest()
            recovered_payload = json.loads(recovered_bytes)
            self.assertEqual(parent_item, parent_before)
            self.assertEqual(sidecar.read_bytes(), sidecar_before)
            self.assertNotEqual(recovered_sidecar, sidecar)
            self.assertEqual(
                recovered_payload["speedtree_handoff_contract"],
                FakeSidecarContract.build_sidecar_descriptor(
                    "SK_weed_deadbranches_a_01",
                    source=pipeline["source"],
                ),
            )
            self.assertEqual(
                recovered_data["_material_pipeline_json_sha256"],
                recovered_sha256,
            )
            self.assertEqual(recovered_data["file_path"], str(exported))
            self.assertEqual(
                result["recovery"]["regenerated_sidecars"],
                [str(recovered_sidecar.resolve())],
            )
            self.assertEqual(
                result["recovery"]["sidecar_derivations"][0][
                    "source_sidecar"
                ],
                str(sidecar.resolve()),
            )
            derivation_names = {
                Path(record["path"]).name
                for record in result["recovery"]["derivation_code_files"]
            }
            self.assertIn("push_unreal_recovery.py", derivation_names)
            self.assertIn("send2ue_manifest_contract.py", derivation_names)
            commands = sum(
                result["assets"][0]["pre_import_commands"]
                + result["assets"][0]["post_import_commands"],
                [],
            )
            self.assertTrue(
                all(
                    recovered_sidecar.as_posix() in line
                    and recovered_sha256 in line
                    and "expected_mesh_name=" in line
                    and "SK_weed_deadbranches_a_01" in line
                    for line in commands
                )
            )

    def test_complete_legacy_descriptor_is_rebound_from_strict_wrapper(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            pipeline = self.authoritative_pipeline()
            descriptor = copy.deepcopy(
                pipeline["speedtree_handoff_contract"]
            )
            descriptor["version"] = 2
            blend, runtime, exported, sidecar = self.write_recovery_inputs(
                root,
                descriptor=descriptor,
            )
            parent_item = self.parent_item(
                blend, exported, sidecar, runtime
            )
            parent_item["assets"] = [
                self.material_asset(sidecar, "SK_test")
            ]

            result = self.recover_current_item(
                root,
                parent_item,
                blend,
                runtime,
                authoritative_pipeline_contract=pipeline,
            )

            recovered_sidecar = Path(
                result["assets"][0]["asset_data"][
                    "_material_pipeline_json_path"
                ]
            )
            recovered = json.loads(
                recovered_sidecar.read_text(encoding="utf-8")
            )
            self.assertEqual(
                result["recovery"]["sidecar_contracts"][0][
                    "descriptor_status"
                ],
                "legacy",
            )
            self.assertEqual(
                recovered["speedtree_handoff_contract"],
                pipeline["speedtree_handoff_contract"],
            )

    def test_legacy_tree_material_without_intent_requires_blender_rebuild(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            blend, runtime, exported, sidecar = self.write_recovery_inputs(
                root,
                descriptor=None,
                tree_material=True,
            )
            parent_item = self.parent_item(
                blend, exported, sidecar, runtime
            )
            parent_item["assets"] = [
                self.material_asset(sidecar, "SK_test")
            ]

            with self.assertRaisesRegex(
                PushUnrealRecoveryError,
                "tree material.*speedtree_intent",
            ):
                self.recover_current_item(
                    root, parent_item, blend, runtime
                )

    def test_incomplete_command_binding_requires_blender_rebuild(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            blend, runtime, exported, sidecar = self.write_recovery_inputs(
                root,
                descriptor=None,
            )
            parent_item = self.parent_item(
                blend, exported, sidecar, runtime
            )
            asset = self.material_asset(sidecar, "SK_test")
            for group_key in ("pre_import_commands", "post_import_commands"):
                asset[group_key][0][0] = asset[group_key][0][0].replace(
                    "sidecar_sha256=",
                    "legacy_sidecar_digest=",
                )
            parent_item["assets"] = [asset]

            with self.assertRaisesRegex(
                PushUnrealRecoveryError,
                "full Blender rebuild required.*sidecar_sha256",
            ):
                self.recover_current_item(
                    root, parent_item, blend, runtime
                )

    def test_non_assembly_sidecar_fingerprint_drift_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            blend, runtime, exported, sidecar = self.write_recovery_inputs(
                root,
                descriptor=None,
            )
            parent_item = self.parent_item(
                blend, exported, sidecar, runtime
            )
            parent_item["assets"] = [
                self.material_asset(sidecar, "SK_test")
            ]
            original_stat = sidecar.stat()
            sidecar.write_text(
                sidecar.read_text(encoding="utf-8").replace(
                    "SK_test", "SK_tast"
                ),
                encoding="utf-8",
            )
            os.utime(
                sidecar,
                ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns),
            )

            with self.assertRaisesRegex(
                PushUnrealRecoveryError,
                "handoff file #1 content changed",
            ):
                self.recover_current_item(
                    root, parent_item, blend, runtime
                )

    def test_incompatible_non_assembly_descriptor_requires_blender_rebuild(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            blend, runtime, exported, sidecar = self.write_recovery_inputs(
                root,
                descriptor={
                    "kind": "speedtree",
                    "version": 0,
                    "mesh_name": "SK_test",
                },
            )
            parent_item = self.parent_item(
                blend, exported, sidecar, runtime
            )
            parent_item["assets"] = [
                self.material_asset(sidecar, "SK_test")
            ]

            with self.assertRaisesRegex(
                PushUnrealRecoveryError,
                "descriptor.*incompatible",
            ):
                self.recover_current_item(
                    root, parent_item, blend, runtime
                )

    def test_missing_authoritative_wrapper_requires_blender_rebuild(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            blend, runtime, exported, sidecar = self.write_recovery_inputs(
                root,
                descriptor=None,
            )
            parent_item = self.parent_item(
                blend, exported, sidecar, runtime
            )
            parent_item["export_report_path"] = str(
                root / "missing_export_report.json"
            )
            parent_item["assets"] = [
                self.material_asset(sidecar, "SK_test")
            ]
            source_record = {
                "fingerprint": "parent-source",
                "snapshot": self.snapshot(blend, [runtime]),
            }

            with self.assertRaisesRegex(
                PushUnrealRecoveryError,
                "authoritative export report could not be",
            ):
                recover_manifest_item(
                    parent_item,
                    parent_manifest_path=root / "parent.json",
                    parent_report_path=root / "parent_report.json",
                    parent_source_record=source_record,
                    current_source_record=source_record,
                    current_source_fingerprint="parent-source",
                    runtime_code_paths=[runtime],
                    rebindable_code_paths=[runtime],
                    report_path=root / "retry_report.json",
                    selected=True,
                    sidecar_contract_api=FakeSidecarContract,
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
