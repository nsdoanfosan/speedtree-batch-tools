import gzip
import hashlib
import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import cluster_blend_sync as cluster_sync
from atlas_target_registry import save_target_registry
from cluster_blend_sync import (
    ClusterBlendSyncError,
    discover_cluster_blend_relations,
    run_cluster_folder_relation_transaction,
    run_cluster_relation_transaction,
    set_cluster_relation_registry,
)
from speedtree_pipeline_contract import (
    SPM_STRUCTURAL_SEMANTIC_PROJECTION_VERSION,
    spm_file_structural_semantic_fingerprint,
)


def file_sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_capture_manifest(blend, contract_sha256):
    stem = blend.stem.removeprefix("SK_")
    path = blend.with_name(f"{stem}_auto_capture_manifest.json")
    path.write_text(json.dumps({
        "workflow_mode": "PHYSICAL_DIRECT_CAPTURE",
        "direct_uv_source": "same_blender_physical_capture_projection",
        "physical_capture_contract_sha256": contract_sha256,
    }), encoding="utf-8")
    return path


def write_scope_manifest(
    blend,
    target,
    *,
    complete=True,
    material="M_branch_elm_01",
    canonical_spm=None,
    capture_contract_sha256=None,
    material_groups=None,
    source_fbx=None,
    connection_requested=None,
    semantic_source=True,
):
    scope = target.parent / ".atlas_leaf_speedtree_scopes"
    scope.mkdir(exist_ok=True)
    path = scope / f"scope__{target.stem}.json"
    payload = {
        "blend_file": str(blend),
        "spm": str(target),
        "material": material,
        "mesh_ids": [10, 11, 12],
        "generator_connection": {"complete": complete},
        "source_material_adoption": {"material_name": material, "material_id": 8},
    }
    if connection_requested is not None:
        payload["generator_connection"]["requested"] = connection_requested
    if material_groups is not None:
        payload["material_groups"] = material_groups
    if canonical_spm is not None:
        source_contract = {
            "source_spm": str(canonical_spm),
            "source_spm_sha256": file_sha256(canonical_spm),
        }
        if semantic_source:
            source_contract.update({
                "source_spm_semantic_projection_version":
                    SPM_STRUCTURAL_SEMANTIC_PROJECTION_VERSION,
                "source_spm_semantic_fingerprint":
                    spm_file_structural_semantic_fingerprint(canonical_spm),
            })
        if source_fbx is not None:
            source_contract.update({
                "source_fbx": str(source_fbx),
                "source_fbx_sha256": file_sha256(source_fbx),
            })
        payload["normalized_prototype_receipt"] = {
            "workflow_mode": "PHYSICAL_DIRECT_CAPTURE",
            "physical_capture_contract_sha256": capture_contract_sha256,
            "variants": [{
                "plan_uv_transfer": {
                    "source_3d_contract": source_contract,
                },
            }],
        }
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def write_material_spm(path, material, material_id, mesh_ids):
    mesh_ids = list(mesh_ids)
    primary = (
        f"<CutoutMeshID>{mesh_ids[0]}</CutoutMeshID>"
        if mesh_ids else "<CutoutMeshID>-1</CutoutMeshID>"
    )
    supplemental = "".join(
        f'<CutoutMesh ID="{value}"/>' for value in mesh_ids[1:]
    )
    payload = (
        "<SpeedTree><Materials>"
        f'<Material_v8 ID="{material_id}" Name="{material}">'
        f"{primary}<SupplementalCutoutMeshIDs>{supplemental}"
        "</SupplementalCutoutMeshIDs></Material_v8>"
        "</Materials></SpeedTree>"
    ).encode("utf-8")
    path.write_bytes(gzip.compress(payload))


class ClusterBlendSyncTests(unittest.TestCase):
    def test_shallow_discovery_does_not_hash_physical_sources(self):
        with tempfile.TemporaryDirectory() as temporary:
            owner = Path(temporary) / "Tree_elm"
            cluster = owner / "Cluster"
            cluster.mkdir(parents=True)
            target = owner / "SK_Tree_elm_01.spm"
            write_material_spm(target, "M_branch_elm_01", 8, [10, 11, 12])
            canonical = cluster / "SK_branch_elm_01.spm"
            write_material_spm(
                canonical,
                "M_branch_elm_01",
                8,
                [10, 11, 12],
            )
            blend = canonical.with_suffix(".blend")
            blend.write_bytes(b"blend")
            set_cluster_relation_registry(blend, target, True)
            write_scope_manifest(
                blend,
                target,
                canonical_spm=canonical,
                capture_contract_sha256="capture",
                material_groups=[{
                    "material": "M_branch_elm_01",
                    "material_id": 8,
                    "mesh_ids": [10, 11, 12],
                }],
            )

            with mock.patch.object(
                cluster_sync,
                "_sha256_file",
                side_effect=AssertionError("shallow scan hashed a source"),
            ):
                row = discover_cluster_blend_relations(
                    owner,
                    verify_physical=False,
                )[0]

            self.assertEqual(row["targets"][0]["status"], "registered")
            self.assertTrue(row["targets"][0]["refresh_deferred"])
            self.assertEqual(row["refresh_deferred_count"], 1)

    def test_intentionally_unrequested_generator_connection_is_synced(self):
        with tempfile.TemporaryDirectory() as temporary:
            owner = Path(temporary) / "bush_blackgum"
            cluster = owner / "cluster"
            cluster.mkdir(parents=True)
            target = owner / "SK_bush_blackgum_01.spm"
            target.write_bytes(b"target")
            canonical = cluster / "SK_cluster_blackgum_01.spm"
            canonical.write_bytes(b"canonical")
            blend = canonical.with_suffix(".blend")
            blend.write_bytes(b"blend")
            set_cluster_relation_registry(blend, target, True)
            write_scope_manifest(
                blend,
                target,
                complete=False,
                connection_requested=False,
            )

            row = discover_cluster_blend_relations(owner)[0]["targets"][0]

            self.assertEqual(row["status"], "synced")
            self.assertFalse(row["connected"])
            self.assertFalse(row["connection_requested"])
            self.assertTrue(row["connection_satisfied"])

    def test_incomplete_requested_generator_connection_needs_attention(self):
        with tempfile.TemporaryDirectory() as temporary:
            owner = Path(temporary) / "bush_blackgum"
            cluster = owner / "cluster"
            cluster.mkdir(parents=True)
            target = owner / "SK_bush_blackgum_01.spm"
            target.write_bytes(b"target")
            canonical = cluster / "SK_cluster_blackgum_01.spm"
            canonical.write_bytes(b"canonical")
            blend = canonical.with_suffix(".blend")
            blend.write_bytes(b"blend")
            set_cluster_relation_registry(blend, target, True)
            write_scope_manifest(
                blend,
                target,
                complete=False,
                connection_requested=True,
            )

            row = discover_cluster_blend_relations(owner)[0]["targets"][0]

            self.assertEqual(row["status"], "attention")
            self.assertFalse(row["connection_satisfied"])

    def test_effective_target_merge_preserves_registry_and_adds_selection_once(self):
        owner = Path("C:/Tree")
        first = owner / "SK_Tree_01.spm"
        second = owner / "SK_Tree_02.spm"
        third = owner / "SK_Tree_03.spm"

        merged = cluster_sync._merge_target_spms(
            [first, second],
            [second, third],
        )

        self.assertEqual(merged, [
            first.absolute(),
            second.absolute(),
            third.absolute(),
        ])

    def test_content_hash_does_not_trust_size_and_mtime_alone(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "source.spm"
            path.write_bytes(b"source-v1")
            before = path.stat()
            first = cluster_sync._sha256_file(path)

            path.write_bytes(b"source-v2")
            os.utime(
                path,
                ns=(before.st_atime_ns, before.st_mtime_ns),
            )
            second = cluster_sync._sha256_file(path)

            self.assertNotEqual(first, second)

    def test_discovers_only_same_stem_sk_blend_and_lists_owner_targets_on_off(self):
        with tempfile.TemporaryDirectory() as temporary:
            owner = Path(temporary) / "Tree_elm"
            cluster = owner / "Cluster"
            cluster.mkdir(parents=True)
            source = cluster / "branch_elm_01.spm"
            source.touch()
            (cluster / "SK_branch_elm_01.spm").touch()
            blend = cluster / "SK_branch_elm_01.blend"
            blend.touch()
            (cluster / "unrelated.blend").touch()
            first = owner / "SK_Tree_elm_01.spm"
            second = owner / "SK_Tree_elm_02.spm"
            first.touch()
            second.touch()
            save_target_registry(blend, [first])
            manifest = write_scope_manifest(blend, first)

            rows = discover_cluster_blend_relations(owner)

            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["folder_relation"], "partial")
            self.assertEqual(rows[0]["owner_target_count"], 2)
            self.assertEqual(rows[0]["owner_on_count"], 1)
            self.assertEqual(
                rows[0]["source_spm"],
                (cluster / "SK_branch_elm_01.spm").absolute(),
            )
            self.assertEqual(rows[0]["blend"], blend.absolute())
            by_name = {row["target_spm"].name: row for row in rows[0]["targets"]}
            self.assertTrue(by_name[first.name]["relation_on"])
            self.assertEqual(by_name[first.name]["status"], "synced")
            self.assertEqual(by_name[first.name]["material"], "M_branch_elm_01")
            self.assertEqual(by_name[first.name]["manifest"], str(manifest))
            self.assertFalse(by_name[second.name]["relation_on"])
            self.assertEqual(by_name[second.name]["status"], "off")

    def test_canonical_cluster_spm_change_marks_every_on_target_for_refresh(self):
        with tempfile.TemporaryDirectory() as temporary:
            owner = Path(temporary) / "Tree_elm"
            cluster = owner / "Cluster"
            cluster.mkdir(parents=True)
            canonical = cluster / "SK_branch_elm_01.spm"
            write_material_spm(
                canonical, "M_branch_elm_01", 8, [10, 11, 12]
            )
            blend = cluster / "SK_branch_elm_01.blend"
            blend.touch()
            target = owner / "SK_Tree_elm_01.spm"
            target.touch()
            save_target_registry(blend, [target])
            write_capture_manifest(blend, "capture-v1")
            write_scope_manifest(
                blend,
                target,
                canonical_spm=canonical,
                capture_contract_sha256="capture-v1",
            )

            current = discover_cluster_blend_relations(owner)[0]
            self.assertEqual(current["targets"][0]["status"], "synced")

            write_material_spm(
                canonical, "M_branch_elm_01", 8, [10, 11, 13]
            )
            changed = discover_cluster_blend_relations(owner)[0]

            self.assertEqual(changed["refresh_required_count"], 1)
            self.assertEqual(
                changed["targets"][0]["status"],
                "refresh_required",
            )
            self.assertIn(
                "canonical_source_structural_changed",
                changed["targets"][0]["refresh_reasons"],
            )

    def test_texture_only_spm_drift_keeps_semantic_physical_scope_current(self):
        with tempfile.TemporaryDirectory() as temporary:
            owner = Path(temporary) / "Tree_elm"
            cluster = owner / "Cluster"
            cluster.mkdir(parents=True)
            canonical = cluster / "SK_branch_elm_01.spm"
            write_material_spm(
                canonical, "M_branch_elm_01", 8, [10, 11, 12]
            )
            blend = cluster / "SK_branch_elm_01.blend"
            blend.touch()
            target = owner / "SK_Tree_elm_01.spm"
            target.touch()
            save_target_registry(blend, [target])
            write_capture_manifest(blend, "capture-v1")
            write_scope_manifest(
                blend,
                target,
                canonical_spm=canonical,
                capture_contract_sha256="capture-v1",
            )
            recorded_raw = file_sha256(canonical)

            write_material_spm(
                canonical, "M_branch_elm_texture_rebound", 8, [10, 11, 12]
            )
            row = discover_cluster_blend_relations(owner)[0]["targets"][0]

            self.assertNotEqual(file_sha256(canonical), recorded_raw)
            self.assertEqual(row["status"], "synced")
            self.assertFalse(row["refresh_required"])
            self.assertEqual(row["refresh_reasons"], [])

    def test_legacy_scope_migrates_only_from_exact_texture_normalize_backup(self):
        with tempfile.TemporaryDirectory() as temporary:
            owner = Path(temporary) / "Tree_elm"
            cluster = owner / "Cluster"
            cluster.mkdir(parents=True)
            canonical = cluster / "SK_branch_elm_01.spm"
            write_material_spm(
                canonical, "M_branch_elm_01", 8, [10, 11, 12]
            )
            blend = cluster / "SK_branch_elm_01.blend"
            blend.touch()
            target = owner / "SK_Tree_elm_01.spm"
            target.touch()
            save_target_registry(blend, [target])
            write_capture_manifest(blend, "capture-v1")
            write_scope_manifest(
                blend,
                target,
                canonical_spm=canonical,
                capture_contract_sha256="capture-v1",
                semantic_source=False,
            )
            backup_dir = (
                cluster
                / "reports"
                / "texture_normalize_backups"
                / "texture_normalize_20260729_030505"
            )
            backup_dir.mkdir(parents=True)
            (backup_dir / f"0001_{canonical.name}").write_bytes(
                canonical.read_bytes()
            )
            (cluster / "reports" / "normalize.json").write_text(
                json.dumps(
                    {
                        "status": "ok",
                        "normalization": {
                            "backup_dir": str(backup_dir),
                            "spms": [str(canonical)],
                        },
                    }
                ),
                encoding="utf-8",
            )

            write_material_spm(
                canonical, "M_branch_elm_texture_rebound", 8, [10, 11, 12]
            )
            row = discover_cluster_blend_relations(owner)[0]["targets"][0]

            self.assertEqual(row["status"], "synced")
            self.assertEqual(
                row["legacy_semantic_migration"]["status"],
                "legacy_texture_normalize_migrated",
            )

    def test_new_blender_capture_contract_marks_target_for_refresh(self):
        with tempfile.TemporaryDirectory() as temporary:
            owner = Path(temporary) / "Tree_elm"
            cluster = owner / "Cluster"
            cluster.mkdir(parents=True)
            canonical = cluster / "SK_leaf_elm_01.spm"
            write_material_spm(
                canonical, "M_leaf_elm_01", 8, [10, 11, 12]
            )
            blend = cluster / "SK_leaf_elm_01.blend"
            blend.touch()
            target = owner / "SK_Tree_elm_01.spm"
            target.touch()
            save_target_registry(blend, [target])
            capture = write_capture_manifest(blend, "capture-v1")
            write_scope_manifest(
                blend,
                target,
                canonical_spm=canonical,
                capture_contract_sha256="capture-v1",
            )

            capture.write_text(json.dumps({
                "workflow_mode": "PHYSICAL_DIRECT_CAPTURE",
                "direct_uv_source": (
                    "same_blender_physical_capture_projection"
                ),
                "physical_capture_contract_sha256": "capture-v2",
            }), encoding="utf-8")
            changed = discover_cluster_blend_relations(owner)[0]

            self.assertEqual(changed["refresh_required_count"], 1)
            self.assertIn(
                "physical_capture_changed",
                changed["refresh_reasons"],
            )

    def test_registry_toggle_preserves_other_targets_and_never_mutates_source_spm(self):
        with tempfile.TemporaryDirectory() as temporary:
            owner = Path(temporary) / "bush_blackgum"
            cluster = owner / "cluster"
            cluster.mkdir(parents=True)
            source = cluster / "cluster_blackgum_01.spm"
            source.write_bytes(b"atlas-source")
            blend = cluster / "SK_cluster_blackgum_01.blend"
            blend.touch()
            first = owner / "SK_bush_blackgum_01.spm"
            second = owner / "SK_bush_blackgum_02.spm"
            first.touch()
            second.touch()

            set_cluster_relation_registry(blend, first, True)
            set_cluster_relation_registry(blend, second, True)
            payload = set_cluster_relation_registry(blend, first, False)

            self.assertEqual(payload["target_spms"], [str(second.absolute())])
            self.assertEqual(source.read_bytes(), b"atlas-source")

    def test_rejects_target_outside_owner_folder(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            owner = root / "Tree_elm"
            cluster = owner / "Cluster"
            cluster.mkdir(parents=True)
            blend = cluster / "SK_branch_elm_01.blend"
            blend.touch()
            external = root / "Other" / "SK_Other_01.spm"
            external.parent.mkdir()
            external.touch()
            with self.assertRaises(ClusterBlendSyncError):
                set_cluster_relation_registry(blend, external, True)

    def test_on_runner_uses_factory_startup_and_rolls_back_json_on_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            owner = Path(temporary) / "Tree_elm"
            cluster = owner / "Cluster"
            cluster.mkdir(parents=True)
            blend = cluster / "SK_branch_elm_01.blend"
            blend.touch()
            target = owner / "SK_Tree_elm_01.spm"
            target.touch()
            blender = Path(temporary) / "blender.exe"
            blender.touch()

            with mock.patch(
                "cluster_blend_sync.subprocess.run",
                return_value=SimpleNamespace(
                    returncode=1, stdout="", stderr="expected failure"
                ),
            ) as run:
                with self.assertRaises(ClusterBlendSyncError):
                    run_cluster_relation_transaction(
                        blend,
                        [target],
                        enabled=True,
                        blender_exe=blender,
                        auto_normalize=False,
                    )

            command = run.call_args.args[0]
            self.assertEqual(command[1:3], ["--factory-startup", "--background"])
            self.assertFalse(blend.with_suffix(".atlas_leaf_targets.json").exists())

    def test_registry_is_restored_when_multi_target_registration_fails(self):
        with tempfile.TemporaryDirectory() as temporary:
            owner = Path(temporary) / "Tree_elm"
            cluster = owner / "Cluster"
            cluster.mkdir(parents=True)
            blend = cluster / "SK_branch_elm_01.blend"
            blend.touch()
            first = owner / "SK_Tree_elm_01.spm"
            second = owner / "SK_Tree_elm_02.spm"
            third = owner / "SK_Tree_elm_03.spm"
            for target in (first, second, third):
                target.touch()
            blender = Path(temporary) / "blender.exe"
            blender.touch()
            set_cluster_relation_registry(blend, first, True)
            registry_path = blend.with_suffix(".atlas_leaf_targets.json")
            registry_before = registry_path.read_bytes()
            original = set_cluster_relation_registry

            def fail_on_third(blend_path, target, enabled):
                if Path(target) == third:
                    raise PermissionError("simulated OneDrive access denial")
                return original(blend_path, target, enabled)

            with mock.patch(
                "cluster_blend_sync.set_cluster_relation_registry",
                side_effect=fail_on_third,
            ):
                with self.assertRaises(PermissionError):
                    run_cluster_relation_transaction(
                        blend,
                        [second, third],
                        enabled=True,
                        blender_exe=blender,
                        auto_normalize=False,
                    )

            self.assertEqual(registry_path.read_bytes(), registry_before)

    def test_registry_is_restored_when_spm_snapshot_fails(self):
        with tempfile.TemporaryDirectory() as temporary:
            owner = Path(temporary) / "Tree_elm"
            cluster = owner / "Cluster"
            cluster.mkdir(parents=True)
            blend = cluster / "SK_branch_elm_01.blend"
            blend.touch()
            first = owner / "SK_Tree_elm_01.spm"
            second = owner / "SK_Tree_elm_02.spm"
            first.touch()
            second.touch()
            blender = Path(temporary) / "blender.exe"
            blender.touch()
            set_cluster_relation_registry(blend, first, True)
            registry_path = blend.with_suffix(".atlas_leaf_targets.json")
            registry_before = registry_path.read_bytes()

            with mock.patch(
                "cluster_blend_sync._snapshot_spm_files",
                side_effect=PermissionError(
                    "simulated OneDrive snapshot access denial"
                ),
            ):
                with self.assertRaises(PermissionError):
                    run_cluster_relation_transaction(
                        blend,
                        [second],
                        enabled=True,
                        blender_exe=blender,
                        auto_normalize=False,
                    )

            self.assertEqual(registry_path.read_bytes(), registry_before)

    def test_failed_apply_persists_pre_rollback_diagnostic_report(self):
        with tempfile.TemporaryDirectory() as temporary:
            owner = Path(temporary) / "Tree_elm"
            cluster = owner / "Cluster"
            cluster.mkdir(parents=True)
            blend = cluster / "SK_branch_elm_01.blend"
            blend.touch()
            target = owner / "SK_Tree_elm_01.spm"
            original_xml = (
                "<SpeedTreeModel><Assets>"
                '<Material_v8 ID="3" Name="M_branch">'
                "<CutoutMeshID>3</CutoutMeshID>"
                "</Material_v8>"
                '<Mesh ID="3"><UserData /></Mesh>'
                "</Assets></SpeedTreeModel>"
            )
            original_spm = gzip.compress(
                original_xml.encode("utf-8"),
                mtime=0,
            )
            target.write_bytes(original_spm)
            blender = Path(temporary) / "blender.exe"
            blender.touch()

            def fail_with_worker_report(command, **_kwargs):
                failed_xml = (
                    "<SpeedTreeModel><Assets>"
                    '<Material_v8 ID="3" Name="M_branch">'
                    "<CutoutMeshID>3</CutoutMeshID>"
                    '<UserData>{"generator":"atlas_leaf_mesh_builder",'
                    '"scope":"scope-a","kind":"material"}</UserData>'
                    "</Material_v8>"
                    '<Mesh ID="3"><UserData>{"generator":'
                    '"atlas_leaf_mesh_builder","scope":"scope-a",'
                    '"kind":"mesh"}</UserData></Mesh>'
                    "</Assets></SpeedTreeModel>"
                )
                target.write_bytes(
                    gzip.compress(
                        failed_xml.encode("utf-8"),
                        mtime=0,
                    )
                )
                report_path = Path(
                    command[command.index("--report") + 1]
                )
                worker_diagnostic = (
                    cluster
                    / "reports"
                    / (
                        f"{blend.stem}_cluster_relation_failure_"
                        "worker.json"
                    )
                )
                worker_diagnostic.parent.mkdir(parents=True)
                worker_diagnostic.write_text(
                    json.dumps({
                        "kind": "cluster_relation_failure_diagnostic",
                        "version": 1,
                        "recorded_at": "worker-time",
                        "phase": "blender_worker_exception",
                    }),
                    encoding="utf-8",
                )
                report_path.write_text(
                    json.dumps({
                        "status": "error",
                        "error": (
                            "Adopted plans must use fresh Mesh IDs; "
                            "source IDs cannot be reused.\n"
                            "Failure diagnostic log: "
                            + str(worker_diagnostic)
                        ),
                        "traceback": "worker traceback sentinel",
                        "persistent_failure_report": str(
                            worker_diagnostic
                        ),
                    }),
                    encoding="utf-8",
                )
                return SimpleNamespace(
                    returncode=1,
                    stdout="worker stdout sentinel",
                    stderr="worker stderr sentinel",
                )

            with mock.patch(
                "cluster_blend_sync.subprocess.run",
                side_effect=fail_with_worker_report,
            ):
                with self.assertRaises(ClusterBlendSyncError) as caught:
                    run_cluster_relation_transaction(
                        blend,
                        [target],
                        enabled=True,
                        blender_exe=blender,
                        auto_normalize=False,
                    )

            reports = list(
                (cluster / "reports").glob(
                    f"{blend.stem}_cluster_relation_failure_*.json"
                )
            )
            self.assertEqual(len(reports), 1)
            diagnostic = json.loads(
                reports[0].read_text(encoding="utf-8")
            )
            self.assertEqual(diagnostic["phase"], "worker_failed")
            self.assertEqual(
                diagnostic["worker_diagnostic"]["phase"],
                "blender_worker_exception",
            )
            self.assertEqual(
                diagnostic["worker_report"]["traceback"],
                "worker traceback sentinel",
            )
            self.assertEqual(
                diagnostic["process"]["stderr"],
                "worker stderr sentinel",
            )
            self.assertTrue(
                diagnostic["targets"][0]["changed_before_rollback"]
            )
            self.assertEqual(
                diagnostic["targets"][0]["failed_spm"]["materials"][0],
                {
                    "id": 3,
                    "name": "M_branch",
                    "mesh_ids": [3],
                    "atlas_scope": "scope-a",
                },
            )
            self.assertEqual(target.read_bytes(), original_spm)
            self.assertIn("Failure diagnostic log:", str(caught.exception))
            self.assertIn(str(reports[0]), str(caught.exception))
            self.assertEqual(
                str(caught.exception).count("Failure diagnostic log:"),
                1,
            )

    def test_preparation_failure_persists_diagnostic_and_restores_registry(self):
        with tempfile.TemporaryDirectory() as temporary:
            owner = Path(temporary) / "Tree_elm"
            cluster = owner / "Cluster"
            cluster.mkdir(parents=True)
            blend = cluster / "SK_branch_elm_01.blend"
            blend.touch()
            target = owner / "SK_Tree_elm_01.spm"
            target.write_bytes(
                gzip.compress(
                    (
                        "<SpeedTreeModel><Assets>"
                        '<Material_v8 ID="3" Name="M_branch">'
                        "<CutoutMeshID>3</CutoutMeshID>"
                        "</Material_v8>"
                        '<Mesh ID="3"><UserData /></Mesh>'
                        "</Assets></SpeedTreeModel>"
                    ).encode("utf-8"),
                    mtime=0,
                )
            )
            blender = Path(temporary) / "blender.exe"
            blender.touch()
            registry = cluster_sync.registry_path_for_blend(blend)

            with mock.patch(
                "cluster_blend_sync._atlas_transaction_artifact_recipe",
                side_effect=RuntimeError("artifact snapshot sentinel"),
            ):
                with self.assertRaises(RuntimeError) as caught:
                    run_cluster_relation_transaction(
                        blend,
                        [target],
                        enabled=True,
                        blender_exe=blender,
                        auto_normalize=False,
                    )

            self.assertFalse(registry.exists())
            reports = list(
                (cluster / "reports").glob(
                    f"{blend.stem}_cluster_relation_failure_*.json"
                )
            )
            self.assertEqual(len(reports), 1)
            diagnostic = json.loads(
                reports[0].read_text(encoding="utf-8")
            )
            self.assertEqual(diagnostic["phase"], "preparation_failed")
            self.assertEqual(
                diagnostic["launch_error"],
                "RuntimeError: artifact snapshot sentinel",
            )
            self.assertEqual(
                diagnostic["targets"][0]["before_sha256"],
                diagnostic["targets"][0]["failed_sha256"],
            )
            self.assertIn(
                "Failure diagnostic log:",
                str(caught.exception),
            )

    def test_normalization_recipe_failure_persists_diagnostic(self):
        with tempfile.TemporaryDirectory() as temporary:
            owner = Path(temporary) / "Tree_elm"
            cluster = owner / "Cluster"
            cluster.mkdir(parents=True)
            blend = cluster / "SK_branch_elm_01.blend"
            blend.touch()
            target = owner / "SK_Tree_elm_01.spm"
            target.write_bytes(
                gzip.compress(
                    (
                        "<SpeedTreeModel><Assets>"
                        '<Material_v8 ID="3" Name="M_branch">'
                        "<CutoutMeshID>3</CutoutMeshID>"
                        "</Material_v8>"
                        '<Mesh ID="3"><UserData /></Mesh>'
                        "</Assets></SpeedTreeModel>"
                    ).encode("utf-8"),
                    mtime=0,
                )
            )
            blender = Path(temporary) / "blender.exe"
            blender.touch()

            with mock.patch(
                "cluster_blend_sync.resolve_normalization_recipe",
                side_effect=cluster_sync.ClusterNormalizationSyncError(
                    "normalization recipe sentinel"
                ),
            ):
                with self.assertRaises(ClusterBlendSyncError) as caught:
                    run_cluster_relation_transaction(
                        blend,
                        [target],
                        enabled=True,
                        blender_exe=blender,
                        auto_normalize=True,
                    )

            reports = list(
                (cluster / "reports").glob(
                    f"{blend.stem}_cluster_relation_failure_*.json"
                )
            )
            self.assertEqual(len(reports), 1)
            diagnostic = json.loads(
                reports[0].read_text(encoding="utf-8")
            )
            self.assertEqual(
                diagnostic["phase"],
                "normalization_recipe_failed",
            )
            self.assertEqual(
                diagnostic["launch_error"],
                (
                    "ClusterNormalizationSyncError: "
                    "normalization recipe sentinel"
                ),
            )
            self.assertEqual(diagnostic["command"], [])
            self.assertIn(
                "Failure diagnostic log:",
                str(caught.exception),
            )

    def test_stale_source_is_rebuilt_once_and_reuses_validated_recipe(self):
        blend = Path(r"D:\Trees\Tree\Cluster\SK_branch_01.blend")
        target = Path(r"D:\Trees\Tree\SK_Tree_01.spm")
        required = cluster_sync.ClusterSourceBuildRequiredError(
            "stale source",
            blend=blend,
            canonical_spm=blend.with_suffix(".spm"),
            report_path=blend.parent / "reports" / "source.json",
            reason="source_identity_stale",
        )
        validated = {"normalization_required": True}

        with mock.patch.object(
            cluster_sync,
            "resolve_normalization_recipe",
            side_effect=required,
        ) as resolve, mock.patch.object(
            cluster_sync,
            "prepare_cluster_source_if_required",
            return_value={
                "status": "rebuilt",
                "reason": "source_identity_stale",
                "validated_normalization_recipe": validated,
            },
        ) as prepare:
            recipe, preparation = (
                cluster_sync._resolve_normalization_recipe_with_source_rebuild(
                    blend,
                    [target],
                    blender_exe=Path(r"C:\Blender\blender.exe"),
                    unit_probe_path=Path(r"C:\probe.json"),
                    capture_resolution=1024,
                    progress_callback=None,
                )
            )

        self.assertEqual(recipe, validated)
        self.assertEqual(
            preparation,
            {
                "status": "rebuilt",
                "reason": "source_identity_stale",
            },
        )
        self.assertEqual(resolve.call_count, 1)
        prepare.assert_called_once_with(
            blend,
            [target],
            blender_exe=Path(r"C:\Blender\blender.exe"),
            unit_probe_path=Path(r"C:\probe.json"),
            capture_resolution=1024,
            progress_callback=None,
            known_required=required,
        )

    def test_stale_source_failure_contract_is_process_precondition(self):
        blend = Path(r"D:\Trees\Tree\Cluster\SK_branch_01.blend")
        required = cluster_sync.ClusterSourceBuildRequiredError(
            "stale source",
            blend=blend,
            canonical_spm=blend.with_suffix(".spm"),
            report_path=blend.parent / "reports" / "source.json",
            reason="source_identity_stale",
        )

        contract = cluster_sync._cluster_relation_failure_contract(required)

        self.assertEqual(contract["failure_kind"], "process_precondition")
        self.assertEqual(contract["reason"], "source_identity_stale")
        self.assertEqual(
            contract["remediation"],
            "automatic_cluster_source_rebuild",
        )

    def test_successful_sync_commits_shared_repair_runtime_receipt(self):
        with tempfile.TemporaryDirectory() as temporary:
            owner = Path(temporary) / "Tree_elm"
            cluster = owner / "Cluster"
            cluster.mkdir(parents=True)
            blend = cluster / "SK_branch_elm_01.blend"
            blend.touch()
            target = owner / "SK_Tree_elm_01.spm"
            target.touch()
            blender = Path(temporary) / "blender.exe"
            blender.touch()
            runtime_receipt = (
                cluster / "reports"
                / "SK_branch_elm_01_repair_runtime_codex.json"
            )

            def complete(command, **_kwargs):
                report_path = Path(
                    command[command.index("--report") + 1]
                )
                report_path.write_text(
                    json.dumps({"status": "ok"}),
                    encoding="utf-8",
                )
                return SimpleNamespace(returncode=0, stdout="", stderr="")

            with mock.patch(
                "cluster_blend_sync.subprocess.run",
                side_effect=complete,
            ), mock.patch(
                "sk_batch.repair_runtime_contract."
                "write_repair_runtime_receipt",
                return_value=runtime_receipt,
            ) as write_receipt:
                result = run_cluster_relation_transaction(
                    blend,
                    [target],
                    enabled=True,
                    blender_exe=blender,
                    auto_normalize=False,
                    repair_runtime_config={"fbx_ini": "configured.ini"},
                )

            write_receipt.assert_called_once_with(
                blend.with_suffix(".spm"),
                {"fbx_ini": "configured.ini"},
            )
            self.assertEqual(
                result["repair_runtime_receipt"],
                str(runtime_receipt),
            )

    def test_already_on_and_physically_current_skips_blender_worker(self):
        with tempfile.TemporaryDirectory() as temporary:
            owner = Path(temporary) / "Tree_elm"
            cluster = owner / "Cluster"
            cluster.mkdir(parents=True)
            canonical = cluster / "SK_branch_elm_01.spm"
            write_material_spm(
                canonical, "M_branch_elm_01", 8, [10, 11, 12]
            )
            blend = canonical.with_suffix(".blend")
            blend.write_bytes(b"blend")
            target = owner / "SK_Tree_elm_01.spm"
            write_material_spm(
                target, "M_branch_elm_01", 8, [10, 11, 12]
            )
            set_cluster_relation_registry(blend, target, True)
            write_capture_manifest(blend, "capture")
            write_scope_manifest(
                blend,
                target,
                canonical_spm=canonical,
                capture_contract_sha256="capture",
                material_groups=[{
                    "material": "M_branch_elm_01",
                    "material_id": 8,
                    "mesh_ids": [10, 11, 12],
                }],
            )

            with mock.patch(
                "cluster_blend_sync.resolve_normalization_recipe"
            ) as normalize, mock.patch(
                "cluster_blend_sync.subprocess.run"
            ) as worker:
                result = run_cluster_relation_transaction(
                    blend,
                    [target],
                    enabled=True,
                    blender_exe=Path(temporary) / "missing-blender.exe",
                )

            normalize.assert_not_called()
            worker.assert_not_called()
            self.assertTrue(result["no_change"])
            self.assertTrue(result["already_on"])
            self.assertEqual(
                result["skip_reason"],
                "already_on_up_to_date",
            )

            with mock.patch(
                "cluster_blend_sync._write_shared_repair_runtime_receipt",
                side_effect=OSError("receipt sentinel"),
            ):
                with self.assertRaises(ClusterBlendSyncError) as caught:
                    run_cluster_relation_transaction(
                        blend,
                        [target],
                        enabled=True,
                        blender_exe=(
                            Path(temporary) / "missing-blender.exe"
                        ),
                        repair_runtime_config={"configured": True},
                    )
            failure_reports = list(
                (cluster / "reports").glob(
                    f"{blend.stem}_cluster_relation_failure_*.json"
                )
            )
            self.assertEqual(len(failure_reports), 1)
            failure = json.loads(
                failure_reports[0].read_text(encoding="utf-8")
            )
            self.assertEqual(
                failure["phase"],
                "no_op_runtime_receipt_failed",
            )
            self.assertIn(
                "Failure diagnostic log:",
                str(caught.exception),
            )

    def test_already_on_but_changed_source_runs_blender_worker(self):
        with tempfile.TemporaryDirectory() as temporary:
            owner = Path(temporary) / "Tree_elm"
            cluster = owner / "Cluster"
            cluster.mkdir(parents=True)
            canonical = cluster / "SK_branch_elm_01.spm"
            write_material_spm(
                canonical, "M_branch_elm_01", 8, [10, 11, 12]
            )
            blend = canonical.with_suffix(".blend")
            blend.write_bytes(b"blend")
            target = owner / "SK_Tree_elm_01.spm"
            write_material_spm(
                target, "M_branch_elm_01", 8, [10, 11, 12]
            )
            blender = Path(temporary) / "blender.exe"
            blender.touch()
            set_cluster_relation_registry(blend, target, True)
            write_capture_manifest(blend, "capture")
            write_scope_manifest(
                blend,
                target,
                canonical_spm=canonical,
                capture_contract_sha256="capture",
                material_groups=[{
                    "material": "M_branch_elm_01",
                    "material_id": 8,
                    "mesh_ids": [10, 11, 12],
                }],
            )
            write_material_spm(
                canonical, "M_branch_elm_01", 8, [10, 11, 13]
            )

            def complete(command, **_kwargs):
                report_path = Path(
                    command[command.index("--report") + 1]
                )
                report_path.write_text(
                    json.dumps({"status": "ok"}),
                    encoding="utf-8",
                )
                return SimpleNamespace(
                    returncode=0, stdout="", stderr=""
                )

            with mock.patch(
                "cluster_blend_sync.subprocess.run",
                side_effect=complete,
            ) as worker:
                result = run_cluster_relation_transaction(
                    blend,
                    [target],
                    enabled=True,
                    blender_exe=blender,
                    auto_normalize=False,
                )

            worker.assert_called_once()
            self.assertNotIn("no_change", result)

    def test_already_on_without_physical_proof_runs_blender_worker(self):
        with tempfile.TemporaryDirectory() as temporary:
            owner = Path(temporary) / "Tree_elm"
            cluster = owner / "Cluster"
            cluster.mkdir(parents=True)
            canonical = cluster / "SK_branch_elm_01.spm"
            write_material_spm(
                canonical, "M_branch_elm_01", 8, [10, 11, 12]
            )
            blend = canonical.with_suffix(".blend")
            blend.write_bytes(b"blend")
            target = owner / "SK_Tree_elm_01.spm"
            write_material_spm(
                target, "M_branch_elm_01", 8, [10, 11, 12]
            )
            blender = Path(temporary) / "blender.exe"
            blender.touch()
            set_cluster_relation_registry(blend, target, True)

            def complete(command, **_kwargs):
                report_path = Path(
                    command[command.index("--report") + 1]
                )
                report_path.write_text(
                    json.dumps({"status": "ok"}),
                    encoding="utf-8",
                )
                return SimpleNamespace(
                    returncode=0, stdout="", stderr=""
                )

            with mock.patch(
                "cluster_blend_sync.subprocess.run",
                side_effect=complete,
            ) as worker:
                result = run_cluster_relation_transaction(
                    blend,
                    [target],
                    enabled=True,
                    blender_exe=blender,
                    auto_normalize=False,
                )

            worker.assert_called_once()
            self.assertNotIn("no_change", result)

    def test_force_refresh_bypasses_current_on_noop(self):
        with tempfile.TemporaryDirectory() as temporary:
            owner = Path(temporary) / "Tree_elm"
            cluster = owner / "Cluster"
            cluster.mkdir(parents=True)
            canonical = cluster / "SK_branch_elm_01.spm"
            write_material_spm(
                canonical, "M_branch_elm_01", 8, [10, 11, 12]
            )
            blend = canonical.with_suffix(".blend")
            blend.write_bytes(b"blend")
            target = owner / "SK_Tree_elm_01.spm"
            write_material_spm(
                target, "M_branch_elm_01", 8, [10, 11, 12]
            )
            blender = Path(temporary) / "blender.exe"
            blender.touch()
            set_cluster_relation_registry(blend, target, True)
            write_capture_manifest(blend, "capture")
            write_scope_manifest(
                blend,
                target,
                canonical_spm=canonical,
                capture_contract_sha256="capture",
                material_groups=[{
                    "material": "M_branch_elm_01",
                    "material_id": 8,
                    "mesh_ids": [10, 11, 12],
                }],
            )

            def complete(command, **_kwargs):
                report_path = Path(
                    command[command.index("--report") + 1]
                )
                report_path.write_text(
                    json.dumps({"status": "ok"}),
                    encoding="utf-8",
                )
                return SimpleNamespace(
                    returncode=0, stdout="", stderr=""
                )

            with mock.patch(
                "cluster_blend_sync.subprocess.run",
                side_effect=complete,
            ) as worker:
                run_cluster_relation_transaction(
                    blend,
                    [target],
                    enabled=True,
                    blender_exe=blender,
                    auto_normalize=False,
                    force_refresh=True,
                )

            worker.assert_called_once()

    def test_long_blender_worker_emits_progress_heartbeat(self):
        with tempfile.TemporaryDirectory() as temporary:
            owner = Path(temporary) / "Tree_elm"
            cluster = owner / "Cluster"
            cluster.mkdir(parents=True)
            blend = cluster / "SK_branch_elm_01.blend"
            blend.write_bytes(b"blend")
            target = owner / "SK_Tree_elm_01.spm"
            target.write_bytes(b"target")
            blender = Path(temporary) / "blender.exe"
            blender.touch()
            progress = []

            def complete(command, **_kwargs):
                time.sleep(0.04)
                report_path = Path(
                    command[command.index("--report") + 1]
                )
                report_path.write_text(
                    json.dumps({"status": "ok"}),
                    encoding="utf-8",
                )
                return SimpleNamespace(
                    returncode=0, stdout="", stderr=""
                )

            with mock.patch(
                "cluster_blend_sync.CLUSTER_RELATION_HEARTBEAT_SECONDS",
                0.01,
            ), mock.patch(
                "cluster_blend_sync.subprocess.run",
                side_effect=complete,
            ):
                run_cluster_relation_transaction(
                    blend,
                    [target],
                    enabled=True,
                    blender_exe=blender,
                    auto_normalize=False,
                    progress_callback=lambda stage, message: progress.append(
                        (stage, message)
                    ),
                )

            stages = [stage for stage, _message in progress]
            self.assertIn("blender_worker_started", stages)
            self.assertIn("blender_worker_running", stages)
            self.assertIn("blender_worker_finished", stages)

    def test_on_failure_restores_every_spm_the_job_half_wrote(self):
        with tempfile.TemporaryDirectory() as temporary:
            owner = Path(temporary) / "Tree_elm"
            cluster = owner / "Cluster"
            cluster.mkdir(parents=True)
            blend = cluster / "SK_branch_elm_01.blend"
            blend.touch()
            already_on = owner / "SK_Tree_elm_01.spm"
            selected = owner / "SK_Tree_elm_02.spm"
            already_on.write_bytes(b"clean-01")
            selected.write_bytes(b"clean-02")
            blender = Path(temporary) / "blender.exe"
            blender.touch()
            set_cluster_relation_registry(blend, already_on, True)
            registry_before = blend.with_suffix(".atlas_leaf_targets.json").read_bytes()

            def half_write(*_args, **_kwargs):
                # The addon rewrites every registered target, not just the
                # selected one, before the failing target aborts the run.
                already_on.write_bytes(b"half-written-01")
                selected.write_bytes(b"half-written-02")
                return SimpleNamespace(
                    returncode=1, stdout="", stderr="expected failure"
                )

            with mock.patch(
                "cluster_blend_sync.subprocess.run", side_effect=half_write
            ):
                with self.assertRaises(ClusterBlendSyncError) as caught:
                    run_cluster_relation_transaction(
                        blend,
                        [selected],
                        enabled=True,
                        blender_exe=blender,
                        auto_normalize=False,
                    )

            self.assertEqual(already_on.read_bytes(), b"clean-01")
            self.assertEqual(selected.read_bytes(), b"clean-02")
            self.assertEqual(
                blend.with_suffix(".atlas_leaf_targets.json").read_bytes(),
                registry_before,
            )
            self.assertIn("Rolled back SPM(s)", str(caught.exception))

    def test_on_failure_restores_capture_manifest_and_maps(self):
        with tempfile.TemporaryDirectory() as temporary:
            owner = Path(temporary) / "Tree_elm"
            cluster = owner / "Cluster"
            reports = cluster / "reports"
            xml_dir = cluster / "xml"
            reports.mkdir(parents=True)
            xml_dir.mkdir()
            blend = cluster / "SK_branch_elm_01.blend"
            blend.write_bytes(b"blend")
            source = cluster / "SK_branch_elm_01.spm"
            source.write_bytes(b"source")
            (xml_dir / "SK_branch_elm_01.xml").write_bytes(b"xml")
            target = owner / "SK_Tree_elm_01.spm"
            target.write_bytes(b"target")
            blender = Path(temporary) / "blender.exe"
            blender.touch()
            manifest = cluster / "branch_elm_01_auto_capture_manifest.json"
            color = cluster / "branch_elm_01.tga"
            manifest.write_bytes(b"good-manifest")
            color.write_bytes(b"good-color")
            receipt = (
                reports
                / "SK_branch_elm_01_cluster_normalization_sync_receipt.json"
            )
            receipt.write_bytes(b"good-receipt")
            pipeline_report = (
                reports
                / "SK_branch_elm_01_"
                "speedtree_repair_pipeline_report_codex.json"
            )
            pipeline_report.write_bytes(b"good-pipeline")
            import_manifest = owner / "speedtree_import_manifest.json"
            readme = owner / "README_SPEEDTREE_IMPORT.md"
            import_manifest.write_bytes(b"good-import-manifest")
            readme.write_bytes(b"good-readme")
            target_receipts = owner / ".atlas_leaf_speedtree_targets"
            target_receipts.mkdir()
            target_receipt = target_receipts / "SK_Tree_elm_01.json"
            target_receipt.write_bytes(b"good-target-receipt")
            scopes = owner / ".atlas_leaf_speedtree_scopes"
            scopes.mkdir()
            scope = scopes / "scope-old.json"
            scope.write_text(
                json.dumps({"blend_file": str(blend), "state": "good"}),
                encoding="utf-8",
            )
            meshes = owner / "meshes"
            meshes.mkdir()
            plan_fbx = (
                meshes
                / "m_branch_elm_01__01_branch_elm_01_01.fbx"
            )
            plan_fbx.write_bytes(b"good-plan")

            recipe = {
                "kind": "speedtree_cluster_sync_normalization_recipe",
                "normalization_required": True,
                "blend": str(blend),
                "receipt_path": str(receipt),
                "capture_output_dir": str(cluster),
                "capture_prefix": "branch_elm_01",
                "first_target_spm": str(target),
                "target_spms": [str(target)],
                "material_name": "M_branch_elm_01",
            }

            def half_write(*_args, **_kwargs):
                manifest.write_bytes(b"bad-manifest")
                color.write_bytes(b"bad-color")
                blend.write_bytes(b"bad-blend")
                receipt.write_bytes(b"bad-receipt")
                pipeline_report.write_bytes(b"bad-pipeline")
                (cluster / "branch_elm_01_AO.tga").write_bytes(b"new-partial")
                import_manifest.write_bytes(b"bad-import-manifest")
                readme.write_bytes(b"bad-readme")
                target_receipt.write_bytes(b"bad-target-receipt")
                scope.write_text(
                    json.dumps(
                        {"blend_file": str(blend), "state": "bad"}
                    ),
                    encoding="utf-8",
                )
                (scopes / "scope-new.json").write_text(
                    json.dumps({"blend_file": str(blend)}),
                    encoding="utf-8",
                )
                plan_fbx.write_bytes(b"bad-plan")
                (
                    meshes
                    / "m_branch_elm_01__02_branch_elm_01_02.fbx"
                ).write_bytes(b"new-plan")
                return SimpleNamespace(
                    returncode=1,
                    stdout="",
                    stderr="expected failure",
                )

            with mock.patch(
                "cluster_blend_sync.resolve_normalization_recipe",
                return_value=recipe,
            ), mock.patch(
                "cluster_blend_sync.subprocess.run",
                side_effect=half_write,
            ):
                with self.assertRaises(ClusterBlendSyncError):
                    run_cluster_relation_transaction(
                        blend,
                        [target],
                        enabled=True,
                        blender_exe=blender,
                        unit_probe_path=Path(temporary) / "unit.json",
                    )

            self.assertEqual(manifest.read_bytes(), b"good-manifest")
            self.assertEqual(color.read_bytes(), b"good-color")
            self.assertEqual(blend.read_bytes(), b"blend")
            self.assertEqual(receipt.read_bytes(), b"good-receipt")
            self.assertEqual(
                pipeline_report.read_bytes(),
                b"good-pipeline",
            )
            self.assertFalse((cluster / "branch_elm_01_AO.tga").exists())
            self.assertEqual(
                import_manifest.read_bytes(),
                b"good-import-manifest",
            )
            self.assertEqual(readme.read_bytes(), b"good-readme")
            self.assertEqual(
                target_receipt.read_bytes(),
                b"good-target-receipt",
            )
            self.assertEqual(
                json.loads(scope.read_text(encoding="utf-8"))["state"],
                "good",
            )
            self.assertFalse((scopes / "scope-new.json").exists())
            self.assertEqual(plan_fbx.read_bytes(), b"good-plan")
            self.assertFalse(
                (
                    meshes
                    / "m_branch_elm_01__02_branch_elm_01_02.fbx"
                ).exists()
            )

    def test_on_timeout_restores_spms_and_registry(self):
        with tempfile.TemporaryDirectory() as temporary:
            owner = Path(temporary) / "Tree_elm"
            cluster = owner / "Cluster"
            cluster.mkdir(parents=True)
            blend = cluster / "SK_branch_elm_01.blend"
            blend.touch()
            target = owner / "SK_Tree_elm_01.spm"
            target.write_bytes(b"clean")
            blender = Path(temporary) / "blender.exe"
            blender.touch()

            def hang(*_args, **_kwargs):
                target.write_bytes(b"half-written")
                raise subprocess.TimeoutExpired(cmd="blender", timeout=1)

            with mock.patch(
                "cluster_blend_sync.subprocess.run", side_effect=hang
            ):
                with self.assertRaises(ClusterBlendSyncError):
                    run_cluster_relation_transaction(
                        blend,
                        [target],
                        enabled=True,
                        blender_exe=blender,
                        auto_normalize=False,
                    )

            self.assertEqual(target.read_bytes(), b"clean")
            self.assertFalse(blend.with_suffix(".atlas_leaf_targets.json").exists())

    def test_off_failure_restores_registry_and_atlas_artifacts(self):
        with tempfile.TemporaryDirectory() as temporary:
            owner = Path(temporary) / "Tree_elm"
            cluster = owner / "Cluster"
            cluster.mkdir(parents=True)
            blend = cluster / "SK_branch_elm_01.blend"
            blend.touch()
            target = owner / "SK_Tree_elm_01.spm"
            target.write_bytes(b"clean-spm")
            blender = Path(temporary) / "blender.exe"
            blender.touch()
            save_target_registry(blend, [target])
            registry_path = blend.with_suffix(".atlas_leaf_targets.json")
            registry_before = registry_path.read_bytes()

            import_manifest = owner / "speedtree_import_manifest.json"
            import_manifest.write_bytes(b"clean-import")
            target_receipts = owner / ".atlas_leaf_speedtree_targets"
            target_receipts.mkdir()
            target_receipt = target_receipts / f"{target.stem}.json"
            target_receipt.write_bytes(b"clean-target-receipt")
            scope = write_scope_manifest(
                blend,
                target,
                material="M_branch_elm_01",
            )
            scope_before = scope.read_bytes()
            meshes = owner / "meshes"
            meshes.mkdir()
            plan = meshes / "m_branch_elm_01__part.fbx"
            plan.write_bytes(b"clean-plan")
            reports = cluster / "reports"
            reports.mkdir()
            pipeline_report = reports / (
                f"{blend.stem}_"
                "speedtree_repair_pipeline_report_codex.json"
            )
            pipeline_report.write_bytes(b"clean-pipeline")
            runtime_receipt = reports / (
                f"{blend.stem}_repair_runtime_codex.json"
            )
            runtime_receipt.write_bytes(b"clean-runtime")

            def half_remove(*_args, **_kwargs):
                target.write_bytes(b"half-written-spm")
                save_target_registry(blend, [])
                import_manifest.write_bytes(b"half-written-import")
                target_receipt.write_bytes(b"half-written-target-receipt")
                scope.write_bytes(b"half-written-scope")
                plan.write_bytes(b"half-written-plan")
                pipeline_report.write_bytes(b"half-written-pipeline")
                runtime_receipt.write_bytes(b"half-written-runtime")
                (
                    scope.parent / f"new__{target.stem}.json"
                ).write_text(
                    json.dumps({
                        "blend_file": str(blend),
                        "spm": str(target),
                    }),
                    encoding="utf-8",
                )
                (meshes / "m_branch_elm_01__new.fbx").write_bytes(
                    b"new-plan"
                )
                return SimpleNamespace(
                    returncode=1,
                    stdout="",
                    stderr="expected OFF failure",
                )

            with mock.patch(
                "cluster_blend_sync.subprocess.run",
                side_effect=half_remove,
            ):
                with self.assertRaises(ClusterBlendSyncError):
                    run_cluster_relation_transaction(
                        blend,
                        [target],
                        enabled=False,
                        blender_exe=blender,
                    )

            self.assertEqual(target.read_bytes(), b"clean-spm")
            self.assertEqual(registry_path.read_bytes(), registry_before)
            self.assertEqual(import_manifest.read_bytes(), b"clean-import")
            self.assertEqual(
                target_receipt.read_bytes(),
                b"clean-target-receipt",
            )
            self.assertEqual(scope.read_bytes(), scope_before)
            self.assertEqual(plan.read_bytes(), b"clean-plan")
            self.assertEqual(
                pipeline_report.read_bytes(),
                b"clean-pipeline",
            )
            self.assertEqual(
                runtime_receipt.read_bytes(),
                b"clean-runtime",
            )
            self.assertFalse(
                (scope.parent / f"new__{target.stem}.json").exists()
            )
            self.assertFalse(
                (meshes / "m_branch_elm_01__new.fbx").exists()
            )

    def test_folder_on_targets_every_owner_sk_and_off_targets_every_current_on(self):
        with tempfile.TemporaryDirectory() as temporary:
            owner = Path(temporary) / "Tree_elm"
            cluster = owner / "Cluster"
            cluster.mkdir(parents=True)
            (cluster / "branch_elm_01.spm").touch()
            blend = cluster / "SK_branch_elm_01.blend"
            blend.touch()
            first = owner / "SK_Tree_elm_01.spm"
            second = owner / "SK_Tree_elm_02.spm"
            first.touch()
            second.touch()
            blender = Path(temporary) / "blender.exe"
            blender.touch()

            with mock.patch(
                "cluster_blend_sync.run_cluster_relation_transaction",
                return_value={"status": "ok", "mode": "sync"},
            ) as apply:
                run_cluster_folder_relation_transaction(
                    blend,
                    enabled=True,
                    blender_exe=blender,
                    repair_runtime_config={"fbx_ini": "configured.ini"},
                )
            self.assertEqual(
                apply.call_args.args[1],
                [first.absolute(), second.absolute()],
            )
            self.assertEqual(
                apply.call_args.kwargs["repair_runtime_config"],
                {"fbx_ini": "configured.ini"},
            )

            save_target_registry(blend, [first])
            with mock.patch(
                "cluster_blend_sync.run_cluster_relation_transaction",
                return_value={"status": "ok", "mode": "remove"},
            ) as remove:
                run_cluster_folder_relation_transaction(
                    blend, enabled=False, blender_exe=blender
                )
            self.assertEqual(remove.call_args.args[1], [first.absolute()])


    def test_target_scope_mesh_change_marks_relation_for_refresh(self):
        with tempfile.TemporaryDirectory() as temporary:
            owner = Path(temporary) / "Tree_elm"
            cluster = owner / "Cluster"
            cluster.mkdir(parents=True)
            canonical = cluster / "SK_branch_elm_01.spm"
            write_material_spm(
                canonical, "M_branch_elm_01", 8, [10, 11, 12]
            )
            blend = cluster / "SK_branch_elm_01.blend"
            blend.touch()
            target = owner / "SK_Tree_elm_01.spm"
            write_material_spm(
                target, "M_branch_elm_01", 8, [10, 11, 12]
            )
            save_target_registry(blend, [target])
            write_capture_manifest(blend, "capture-v1")
            write_scope_manifest(
                blend,
                target,
                canonical_spm=canonical,
                capture_contract_sha256="capture-v1",
                material_groups=[{
                    "material": "M_branch_elm_01",
                    "material_id": 8,
                    "mesh_ids": [10, 11, 12],
                }],
            )
            self.assertEqual(
                discover_cluster_blend_relations(owner)[0]["targets"][0][
                    "status"
                ],
                "synced",
            )

            write_material_spm(
                target, "M_branch_elm_01", 8, [10, 11, 12, 13]
            )
            changed = discover_cluster_blend_relations(owner)[0]

            self.assertEqual(
                changed["targets"][0]["status"], "refresh_required"
            )
            self.assertIn(
                "target_scope_changed",
                changed["targets"][0]["refresh_reasons"],
            )

    def test_source_fbx_change_marks_relation_for_refresh(self):
        with tempfile.TemporaryDirectory() as temporary:
            owner = Path(temporary) / "Tree_elm"
            cluster = owner / "Cluster"
            cluster.mkdir(parents=True)
            canonical = cluster / "SK_branch_elm_01.spm"
            write_material_spm(
                canonical, "M_branch_elm_01", 8, [10, 11, 12]
            )
            source_fbx = cluster / "fbx" / "SK_branch_elm_01.fbx"
            source_fbx.parent.mkdir()
            source_fbx.write_bytes(b"fbx-v1")
            blend = cluster / "SK_branch_elm_01.blend"
            blend.touch()
            target = owner / "SK_Tree_elm_01.spm"
            target.touch()
            save_target_registry(blend, [target])
            write_capture_manifest(blend, "capture-v1")
            write_scope_manifest(
                blend,
                target,
                canonical_spm=canonical,
                source_fbx=source_fbx,
                capture_contract_sha256="capture-v1",
            )
            self.assertEqual(
                discover_cluster_blend_relations(owner)[0]["targets"][0][
                    "status"
                ],
                "synced",
            )

            source_fbx.write_bytes(b"fbx-v2")
            changed = discover_cluster_blend_relations(owner)[0]

            self.assertIn(
                "source_fbx_changed",
                changed["targets"][0]["refresh_reasons"],
            )


if __name__ == "__main__":
    unittest.main()
