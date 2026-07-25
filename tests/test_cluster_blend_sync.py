import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from atlas_target_registry import save_target_registry
from cluster_blend_sync import (
    ClusterBlendSyncError,
    discover_cluster_blend_relations,
    run_cluster_folder_relation_transaction,
    run_cluster_relation_transaction,
    set_cluster_relation_registry,
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
    if canonical_spm is not None:
        payload["normalized_prototype_receipt"] = {
            "workflow_mode": "PHYSICAL_DIRECT_CAPTURE",
            "physical_capture_contract_sha256": capture_contract_sha256,
            "variants": [{
                "plan_uv_transfer": {
                    "source_3d_contract": {
                        "source_spm": str(canonical_spm),
                        "source_spm_sha256": file_sha256(canonical_spm),
                    },
                },
            }],
        }
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


class ClusterBlendSyncTests(unittest.TestCase):
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
            canonical.write_bytes(b"canonical-v1")
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

            canonical.write_bytes(b"canonical-v2-with-different-size")
            changed = discover_cluster_blend_relations(owner)[0]

            self.assertEqual(changed["refresh_required_count"], 1)
            self.assertEqual(
                changed["targets"][0]["status"],
                "refresh_required",
            )
            self.assertIn(
                "canonical_source_changed",
                changed["targets"][0]["refresh_reasons"],
            )

    def test_new_blender_capture_contract_marks_target_for_refresh(self):
        with tempfile.TemporaryDirectory() as temporary:
            owner = Path(temporary) / "Tree_elm"
            cluster = owner / "Cluster"
            cluster.mkdir(parents=True)
            canonical = cluster / "SK_leaf_elm_01.spm"
            canonical.write_bytes(b"canonical")
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

            recipe = {
                "kind": "speedtree_cluster_sync_normalization_recipe",
                "normalization_required": True,
                "blend": str(blend),
                "receipt_path": str(receipt),
                "capture_output_dir": str(cluster),
                "capture_prefix": "branch_elm_01",
            }

            def half_write(*_args, **_kwargs):
                manifest.write_bytes(b"bad-manifest")
                color.write_bytes(b"bad-color")
                blend.write_bytes(b"bad-blend")
                receipt.write_bytes(b"bad-receipt")
                (cluster / "branch_elm_01_AO.tga").write_bytes(b"new-partial")
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
            self.assertFalse((cluster / "branch_elm_01_AO.tga").exists())

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
                    blend, enabled=True, blender_exe=blender
                )
            self.assertEqual(
                apply.call_args.args[1],
                [first.absolute(), second.absolute()],
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


if __name__ == "__main__":
    unittest.main()
