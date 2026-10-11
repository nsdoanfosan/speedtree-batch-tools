"""Exercise native plan staging without Blender or production file writes."""

import ast
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SK_BATCH = Path(__file__).resolve().parents[1]
if str(SK_BATCH) not in sys.path:
    sys.path.insert(0, str(SK_BATCH))

from cluster_assembly_builder import (
    ClusterAssemblyBuildError,
    build_unreal_ingest_plan,
    file_fingerprint,
)


JOB = Path(__file__).resolve().parents[1] / "jobs" / "send2ue_push_job.py"


def native_job_plan(manifest, template, folder, export_root):
    """Execute the actual native job's plan call with real temporary inputs."""
    tree = ast.parse(JOB.read_text(encoding="utf-8"))
    calls = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "build_unreal_ingest_plan"
    ]
    if len(calls) != 1:
        raise AssertionError("native job must have exactly one Assembly plan call")
    return eval(
        compile(ast.Expression(calls[0]), str(JOB), "eval"),
        {
            "build_unreal_ingest_plan": build_unreal_ingest_plan,
            "cluster_manifest": manifest,
            "full_templates": [template],
            "mesh_path": template["asset_data"]["asset_path"],
            "folder": folder,
            "export_root": export_root,
        },
    )


def make_inputs(root, full_name):
    assembly = root / "managed" / "assembly"
    assembly.mkdir(parents=True)
    full = assembly / f"{full_name}.fbx"
    base_name = f"{full_name}_NA_Base"
    part_name = f"{full_name}_NA_Branch_01"
    base = assembly / f"{base_name}.fbx"
    part = assembly / f"{part_name}.fbx"
    wind = assembly / "wind.json"
    for path in (full, base, part):
        path.write_bytes(path.name.encode("utf-8"))
    wind.write_text("{}", encoding="utf-8")
    full_payload = {
        "mesh_name": full_name,
        "speedtree_handoff_contract": {
            "kind": "speedtree",
            "version": 1,
            "asset_kind": "speedtree",
            "mesh_name": full_name,
            "fingerprint": "unchanged-material-content-identity",
            "source": {"stmat": [{
                "canonical_path": str(root / "managed" / "tree.stmat"),
                "sha256": "a" * 64,
                "size": 15736,
                "mtime_ns": 200,
            }]},
        },
        "materials": [
            {"slot_name": name, "slot_index": index,
             "speedtree_intent": {"material_instance_base": name}}
            for index, name in enumerate(("M_bark", "M_leaf"))
        ],
        "validation_children": {"asset_unit": full_name, "json_name": full_name},
    }
    source_sidecar = assembly / "Full_handoff.json"
    source_sidecar.write_text(json.dumps(full_payload), encoding="utf-8")
    source_fp = file_fingerprint(source_sidecar)
    managed_sidecars = assembly / "material_sidecars"
    managed_sidecars.mkdir()
    for name in (base_name, part_name):
        previous = deepcopy(full_payload)
        previous["mesh_name"] = name
        previous["speedtree_handoff_contract"]["mesh_name"] = name
        previous["speedtree_handoff_contract"]["source"]["stmat"][0]["mtime_ns"] = 100
        (managed_sidecars / f"{name}.json").write_text(
            json.dumps(previous), encoding="utf-8"
        )
    folder = "/Game/Trees/GenericSpecies"
    full_path = f"{folder}/{full_name}"
    manifest = {
        "status": "ready",
        "full_asset_stem": full_name,
        "full_fbx": file_fingerprint(full),
        "base": {
            "asset_name": base_name,
            "export_stem": base_name,
            "fbx": file_fingerprint(base),
            "material_slots": ["M_bark"],
        },
        "wind_contract": {"wind_json": file_fingerprint(wind)},
        "parts": [{
            "prototype_id": "branch_generic_signature",
            "asset_name": part_name,
            "export_stem": part_name,
            "role": "branch",
            "role_identity": "branch_generic_01",
            "fbx": file_fingerprint(part),
            "material_slots": ["M_leaf"],
        }],
    }
    template = {
        "asset_id": "full",
        "asset_data": {
            "_asset_type": "SkeletalMesh",
            "asset_path": full_path,
            "file_path": str(full),
            "_material_pipeline_json_path": str(source_sidecar),
            "_material_pipeline_json_sha256": source_fp["sha256"],
            "_material_pipeline_expected_mesh_name": full_name,
        },
        "property_data": {"unreal": {"import_method": {"fbx": {
            "skeletal_mesh_import_data": {},
        }}}},
    }
    for key, method in (("pre_import_commands", "preflight_mesh_materials"),
                        ("post_import_commands", "process_mesh")):
        template[key] = [[
            f"_asset_path = '{full_path}'",
            f"_p.{method}(_asset_path, json_path='{source_sidecar.as_posix()}', "
            f"expected_mesh_name='{full_name}', sidecar_sha256='{source_fp['sha256']}')",
        ]]
    return manifest, template, folder, assembly


class GeneratedSidecarStagingTests(unittest.TestCase):
    def test_actual_native_job_preserves_managed_bytes_and_binds_staged_files(self):
        for full_name in ("SK_tree_Weeping_Willow_01", "SK_weed_reed_02",
                          "SK_arbitrary_species_03"):
            with self.subTest(full_name=full_name), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                manifest, template, folder, assembly = make_inputs(root, full_name)
                source_bytes = {path: path.read_bytes() for path in assembly.rglob("*")
                                if path.is_file()}
                source_template = deepcopy(template)
                export_root = root / "native_runs" / "fresh_run"
                plan = native_job_plan(manifest, template, folder, export_root)
                self.assertEqual(plan["status"], "ready")
                self.assertEqual(template, source_template)
                self.assertEqual({path: path.read_bytes() for path in source_bytes},
                                 source_bytes)
                self.assertEqual({path for path in assembly.rglob("*") if path.is_file()},
                                 set(source_bytes))
                for asset, expected_slot in zip(plan["assets"], ("M_bark", "M_leaf")):
                    data = asset["asset_data"]
                    sidecar = Path(data["_material_pipeline_json_path"])
                    self.assertEqual(sidecar.parent, (export_root / "material_sidecars").resolve())
                    actual_sha = hashlib.sha256(sidecar.read_bytes()).hexdigest()
                    self.assertEqual(data["_material_pipeline_json_sha256"], actual_sha)
                    self.assertEqual(data["_material_pipeline_json_fingerprint"],
                                     file_fingerprint(sidecar))
                    payload = json.loads(sidecar.read_text(encoding="utf-8"))
                    self.assertEqual(payload["mesh_name"], data["_cluster_assembly_asset_name"])
                    self.assertEqual(payload["generated_material_slot_contract"]["slot_names"],
                                     [expected_slot])
                    self.assertEqual(payload["materials"][0]["slot_name"], expected_slot)
                    self.assertEqual(payload["speedtree_handoff_contract"]["source"]["stmat"][0]["mtime_ns"],
                                     200)
                    commands = json.dumps(asset["pre_import_commands"] + asset["post_import_commands"])
                    self.assertIn(sidecar.as_posix(), commands)
                    self.assertIn(actual_sha, commands)
                    self.assertIn(f"expected_mesh_name='{payload['mesh_name']}'", commands)
                    self.assertNotIn(template["asset_data"]["_material_pipeline_json_sha256"], commands)
                    self.assertNotIn(Path(template["asset_data"]["_material_pipeline_json_path"]).as_posix(), commands)

    def test_separate_native_runs_do_not_reuse_previous_generated_sidecar_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest, template, folder, _ = make_inputs(root, "SK_other_species_01")
            plans = [native_job_plan(manifest, template, folder, root / run)
                     for run in ("native_run_1", "native_run_2")]
            first_paths, second_paths = [
                {asset["asset_data"]["_material_pipeline_json_path"] for asset in plan["assets"]}
                for plan in plans
            ]
            self.assertFalse(first_paths & second_paths)
            for path in first_paths | second_paths:
                self.assertTrue(Path(path).is_file())

    def test_explicit_managed_output_root_is_rejected_before_source_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            manifest, template, folder, assembly = make_inputs(Path(tmp), "SK_generic_01")
            source_bytes = {path: path.read_bytes() for path in assembly.rglob("*") if path.is_file()}
            with self.assertRaisesRegex(ClusterAssemblyBuildError, "managed Assembly source"):
                build_unreal_ingest_plan(
                    manifest, template, template["asset_data"]["asset_path"], folder,
                    generated_material_output_dir=assembly,
                )
            self.assertEqual({path: path.read_bytes() for path in source_bytes}, source_bytes)

    def test_other_callers_keep_existing_default_output_location(self):
        with tempfile.TemporaryDirectory() as tmp:
            manifest, template, folder, assembly = make_inputs(Path(tmp), "SK_legacy_species_01")
            plan = build_unreal_ingest_plan(
                manifest, template, template["asset_data"]["asset_path"], folder,
            )
            for asset in plan["assets"]:
                self.assertEqual(Path(asset["asset_data"]["_material_pipeline_json_path"]).parent,
                                 (assembly / "material_sidecars").resolve())

    def test_material_directory_junction_cannot_redirect_native_write_to_managed_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest, template, folder, assembly = make_inputs(root, "SK_generic_link_01")
            source_bytes = {path: path.read_bytes() for path in assembly.rglob("*") if path.is_file()}
            export_root = root / "native_link_run"
            export_root.mkdir()
            link = export_root / "material_sidecars"
            managed_sidecars = assembly / "material_sidecars"
            if os.name == "nt":
                subprocess.run(
                    ["cmd", "/c", "mklink", "/J", str(link), str(managed_sidecars)],
                    check=True, capture_output=True,
                )
            else:
                link.symlink_to(managed_sidecars, target_is_directory=True)
            try:
                self.assertEqual(link.resolve(), managed_sidecars.resolve())
                with self.assertRaisesRegex(ClusterAssemblyBuildError, "staging path escapes"):
                    native_job_plan(manifest, template, folder, export_root)
                self.assertEqual({path: path.read_bytes() for path in source_bytes}, source_bytes)
            finally:
                if os.name == "nt":
                    os.rmdir(link)  # Remove the junction itself; its target remains intact.
                else:
                    link.unlink()

    def test_final_json_symlink_outside_staging_is_rejected_before_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest, template, folder, assembly = make_inputs(root, "SK_generic_file_link_01")
            protected = assembly / "material_sidecars" / f"{manifest['base']['asset_name']}.json"
            protected_bytes = protected.read_bytes()
            export_root = root / "native_file_link_run"
            staged_dir = export_root / "material_sidecars"
            staged_dir.mkdir(parents=True)
            link = staged_dir / protected.name
            try:
                link.symlink_to(protected)
            except OSError as exc:
                self.skipTest(f"file symlink creation unavailable: {exc}")
            with self.assertRaisesRegex(ClusterAssemblyBuildError, "staging path escapes"):
                native_job_plan(manifest, template, folder, export_root)
            self.assertEqual(protected.read_bytes(), protected_bytes)
            self.assertTrue(link.is_symlink())
            self.assertEqual(list(staged_dir.iterdir()), [link])

    def test_atomic_temporary_symlink_cannot_write_outside_staging(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest, template, folder, assembly = make_inputs(root, "SK_generic_temp_link_01")
            protected = assembly / "material_sidecars" / f"{manifest['base']['asset_name']}.json"
            protected_bytes = protected.read_bytes()
            export_root = root / "native_temporary_link_run"
            staged_dir = export_root / "material_sidecars"
            staged_dir.mkdir(parents=True)
            link = staged_dir / f".{protected.name}.{os.getpid()}.tmp"
            try:
                link.symlink_to(protected)
            except OSError as exc:
                self.skipTest(f"file symlink creation unavailable: {exc}")
            with self.assertRaisesRegex(ClusterAssemblyBuildError, "staging path escapes"):
                native_job_plan(manifest, template, folder, export_root)
            self.assertEqual(protected.read_bytes(), protected_bytes)
            self.assertTrue(link.is_symlink())
            self.assertEqual(list(staged_dir.iterdir()), [link])


if __name__ == "__main__":
    unittest.main()
