from __future__ import annotations

import ast
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


SK_BATCH = Path(__file__).resolve().parents[1]
if str(SK_BATCH) not in sys.path:
    sys.path.insert(0, str(SK_BATCH))

from cluster_fleet_push import (  # noqa: E402
    ExactPushError,
    PushDependencyError,
    _completed_process_metrics,
    build_spm_bone_policy_command,
    build_receipt_refresh_command,
    build_assembly_command,
    checkout_headless_manifest_assets,
    discover_provider_dependencies,
    discover_current_cluster_targets,
    main,
    parse_args,
    validate_spm_bone_policy_report,
    validate_provider_live_result,
    validate_provider_assembly_result,
    validate_assembly_result,
    validate_live_result,
    write_combined_lazy_manifest,
)


def exact_identity_contract(binding_count):
    source_spm = {
        "path": "C:/target.spm",
        "sha256": "a" * 64,
    }
    return {
        "placement_contract": {
            "version": 9,
            "identity_policy": "exact_fbx_vertex_or_native_clipped_origin_v1",
            "translation_source": (
                "exact_fbx_attachment_vertex_else_native_receipt"
            ),
            "rotation_uniform_scale_source": (
                "exact_modeler_runtime_tangent_and_uv_plan_line_length_v1"
            ),
            "exact_plan_line": {
                "selection_policy": (
                    "unique_source_and_target_uv_triangles_containing_exact_"
                    "authored_line_endpoint_v1"
                ),
                "frame_policy": (
                    "runtime_pose_tangent_preserve_plan_roll_and_exact_uv_length"
                ),
                "nearest_or_farthest_search": False,
            },
            "exact_attachment_binding_count": binding_count,
            "exact_fbx_attachment_binding_count": binding_count,
            "native_clipped_origin_attachment_binding_count": 0,
            "source_spm": source_spm,
        },
        "attachment_bone_contract": {
            "status": "ready",
            "policy": (
                "native_modeler_runtime_receipt_v5_exact_pose_"
                "skeleton_index_zero"
            ),
            "source_spm": source_spm,
            "receipt": {
                "path": "C:/target.speedtree_native_receipt.json",
                "sha256": "b" * 64,
            },
            "bone_count": 1,
            "generated_instance_count": 1,
        },
    }


class ClusterFleetPushTests(unittest.TestCase):
    def test_completed_process_metrics_promotes_exact_tree_usage(self):
        completed = SimpleNamespace(
            resource_usage={
                "peak_job_memory_bytes": 123,
                "user_cpu_seconds": 4.5,
            }
        )
        with patch("cluster_fleet_push.perf_counter", return_value=12.25):
            metrics = _completed_process_metrics(completed, 10.0)

        self.assertEqual(metrics["wall_seconds"], 2.25)
        self.assertEqual(metrics["resource_usage"], completed.resource_usage)

    def test_root_execution_has_assembly_barrier_before_export_wave(self):
        source = (SK_BATCH / "cluster_fleet_push.py").read_text(
            encoding="utf-8"
        )
        assembly_loop = source.index(
            "    for index, target in enumerate(targets, 1):"
        )
        prepared_append = source.index(
            "            prepared_roots.append({",
            assembly_loop,
        )
        export_loop = source.index(
            "    for export_index, prepared in enumerate(prepared_roots, 1):",
            prepared_append,
        )

        self.assertLess(assembly_loop, prepared_append)
        self.assertLess(prepared_append, export_loop)
        self.assertIn(
            "all_root_assembly_then_all_root_blender_export_then_combined_unreal",
            source,
        )

    def test_policy_batch_command_uses_factory_startup_and_exact_targets(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            blender = root / "blender.exe"
            spm = root / "SK_tree_01.spm"
            request = root / "request.json"
            report = root / "report.json"
            blender.write_bytes(b"exe")
            spm.write_bytes(b"spm")

            command = build_spm_bone_policy_command(
                [{"spm": spm}], blender, request, report
            )
            payload = json.loads(request.read_text(encoding="utf-8"))

        self.assertIn("--factory-startup", command)
        self.assertIn("--background", command)
        self.assertEqual(payload["targets"], [str(spm.resolve())])

    def test_policy_batch_report_requires_exact_ordered_identities(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = root / "SK_tree_01.spm"
            second = root / "SK_tree_02.spm"
            report = root / "report.json"
            first.write_bytes(b"one")
            second.write_bytes(b"two")
            def sealed(path):
                stat_result = path.stat()
                return {
                    "path": str(path.resolve()),
                    "size": stat_result.st_size,
                    "mtime_ns": stat_result.st_mtime_ns,
                    "sha256": __import__("hashlib").sha256(
                        path.read_bytes()
                    ).hexdigest(),
                }
            report.write_text(json.dumps({
                "status": "ok",
                "results": [
                    {
                        "spm": str(first),
                        "status": "already_compliant",
                        "sealed_source_identity": sealed(first),
                    },
                    {
                        "spm": str(second),
                        "status": "updated",
                        "sealed_source_identity": sealed(second),
                    },
                ],
            }), encoding="utf-8")
            payload = validate_spm_bone_policy_report(
                report,
                [{"spm": first}, {"spm": second}],
            )
            self.assertEqual(len(payload["results"]), 2)

            report.write_text(json.dumps({
                "status": "ok",
                "results": [
                    {
                        "spm": str(second),
                        "status": "updated",
                        "sealed_source_identity": sealed(second),
                    },
                    {
                        "spm": str(first),
                        "status": "already_compliant",
                        "sealed_source_identity": sealed(first),
                    },
                ],
            }), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "identity mismatch"):
                validate_spm_bone_policy_report(
                    report,
                    [{"spm": first}, {"spm": second}],
                )

    def test_fleet_policy_failure_aborts_before_provider_or_root_processes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            blender = root / "blender.exe"
            spm = root / "tree" / "SK_tree_01.spm"
            manifest = spm.parent / "assembly" / "current.json"
            logs = root / "logs"
            blender.write_bytes(b"exe")
            spm.parent.mkdir()
            spm.write_bytes(b"spm")
            manifest.parent.mkdir()
            manifest.write_text("{}", encoding="utf-8")
            calls = []

            def fake_owned_run(_command, **kwargs):
                calls.append(kwargs.get("source"))
                return SimpleNamespace(returncode=1)

            with patch(
                "cluster_fleet_push.discover_current_cluster_targets",
                return_value=([{
                    "spm": spm,
                    "stem": spm.stem,
                    "manifest": manifest,
                    "birch_paper": False,
                }], []),
            ), patch(
                "cluster_fleet_push.discover_provider_dependencies",
                return_value=([], {}, {}, {}),
            ), patch(
                "cluster_fleet_push.owned_run",
                side_effect=fake_owned_run,
            ):
                from cluster_fleet_push import main

                result = main([
                    "--root", str(root),
                    "--blender", str(blender),
                    "--log-dir", str(logs),
                    "--run-id", "policy_failure",
                ])

        self.assertEqual(result, 1)
        self.assertEqual(
            calls,
            ["sk_batch.cluster_fleet_push.spm_bone_policy"],
        )

    def test_prepare_only_flag_parses(self):
        args = parse_args(["--prepare-only"])

        self.assertTrue(args.prepare_only)

    def test_provider_dependencies_are_ordered_before_roots_and_deduplicated(self):
        root_a = Path("D:/trees/tree_a/SK_tree_a_01.spm")
        root_b = Path("D:/trees/tree_b/SK_tree_b_01.spm")
        shared = Path("D:/trees/tree_a/cluster/SK_leaf_shared.spm")
        branch = Path("D:/trees/tree_a/cluster/SK_branch_a.spm")

        def dependencies(spm):
            if Path(spm).stem == root_a.stem:
                return [shared, branch]
            return [shared]

        with patch(
            "cluster_fleet_push.cluster_dependency_spms",
            side_effect=dependencies,
        ):
            ordered, by_root, issues, resolution = (
                discover_provider_dependencies([
                {"spm": root_a},
                {"spm": root_b},
                ])
            )

        self.assertFalse(issues)
        self.assertEqual(ordered, [shared.resolve(), branch.resolve()])
        self.assertEqual(
            by_root[str(root_a.resolve())],
            [str(shared.resolve()), str(branch.resolve())],
        )
        self.assertEqual(
            by_root[str(root_b.resolve())],
            [str(shared.resolve())],
        )
        self.assertEqual(
            resolution[str(root_a.resolve())]["policy"],
            "current_validated_manifest_or_relation",
        )

    def test_stale_root_manifest_is_only_a_provider_execution_hint(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            root_spm = root / "tree" / "SK_tree_01.spm"
            provider = root / "tree" / "cluster" / "SK_leaf_01.spm"
            manifest = root / "tree" / "assembly" / "root.json"
            provider.parent.mkdir(parents=True)
            manifest.parent.mkdir()
            root_spm.write_bytes(b"root")
            provider.write_bytes(b"provider")
            manifest.write_text(json.dumps({
                "status": "ready",
                "content_decision": "build",
                "parts": [{
                    "external_source": {
                        "source_blend": {
                            "path": str(provider.with_suffix(".blend")),
                        },
                    },
                }],
            }), encoding="utf-8")

            with patch(
                "cluster_fleet_push.cluster_dependency_spms",
                side_effect=PushDependencyError("stale fingerprint"),
            ):
                ordered, by_root, issues, resolution = (
                    discover_provider_dependencies([{
                        "spm": root_spm,
                        "manifest": manifest,
                    }])
                )

        self.assertFalse(issues)
        self.assertEqual(ordered, [provider.resolve()])
        self.assertEqual(
            by_root[str(root_spm.resolve())], [str(provider.resolve())]
        )
        self.assertEqual(
            resolution[str(root_spm.resolve())]["policy"],
            "saved_manifest_execution_hint_only",
        )

    def test_provider_assembly_requires_push_ready_blend_and_export_objects(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pipeline = root / "pipeline.json"
            report = root / "assembly_report.json"
            pipeline.write_text(json.dumps({
                "status": "done",
                "cluster_source_build_contract": {
                    "status": "ready",
                    "source_blend_committed": True,
                    "source_object": "SK_leaf_sample_Merged",
                },
                "assembly_export_postcondition": {
                    "objects": [{"name": "SK_leaf_sample_01"}],
                },
                "import": {"material_consolidation": {
                    "status": "applied",
                    "groups": [{
                        "mode": "production_group_suffix",
                        "target_material": "M_leaf_sample_atlas_01",
                        "source_materials": [
                            "M_leaf_sample_atlas_01_green"
                        ],
                    }],
                }},
                "steps": [{
                    "name": "consolidate_speedtree_group_materials",
                    "status": "skipped",
                    "groups": [],
                }],
            }), encoding="utf-8")
            report.write_text(json.dumps({
                "status": "ok",
                "unreal_push_ready": True,
                "blend": str(root / "provider.blend"),
                "pipeline_report": str(pipeline),
            }), encoding="utf-8")
            (root / "provider.blend").write_bytes(b"blend")

            result = validate_provider_assembly_result(report)

        self.assertTrue(result["ok"])
        self.assertEqual(result["export_objects"], ["SK_leaf_sample_01"])
        self.assertEqual(
            result["material_consolidation"]["status"], "applied"
        )

    def test_provider_resume_requires_current_producer_code_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pipeline = root / "pipeline.json"
            report = root / "assembly_report.json"
            blend = root / "provider.blend"
            blend.write_bytes(b"blend")
            payload = {
                "status": "done",
                "cluster_source_build_contract": {},
                "assembly_export_postcondition": {
                    "objects": [{"name": "SK_leaf_sample_01"}],
                },
            }
            pipeline.write_text(json.dumps(payload), encoding="utf-8")
            report_payload = {
                "status": "ok",
                "unreal_push_ready": True,
                "blend": str(blend),
                "pipeline_report": str(pipeline),
                "blender_addon_runtime": {
                    "addons": [{
                        "id": "speedtree_bone_weight_repair",
                        "source_root": str(root / "addon"),
                    }],
                },
            }
            report.write_text(
                json.dumps(report_payload), encoding="utf-8"
            )

            missing = validate_provider_assembly_result(
                report,
                require_current_producer=True,
            )
            self.assertIn(
                "provider_producer_code_state_missing",
                missing["problems"],
            )

            report_payload["assembly_producer_code_state"] = {
                "addon/core.py": "a" * 64,
            }
            report.write_text(
                json.dumps(report_payload), encoding="utf-8"
            )
            with patch(
                "cluster_fleet_push.assembly_runtime_code_state",
                return_value=report_payload["assembly_producer_code_state"],
            ):
                current = validate_provider_assembly_result(
                    report,
                    require_current_producer=True,
                )
            self.assertTrue(current["ok"])
            self.assertEqual(current["producer_code_state"], "current")

            with patch(
                "cluster_fleet_push.assembly_runtime_code_state",
                return_value={"addon/core.py": "b" * 64},
            ):
                stale = validate_provider_assembly_result(
                    report,
                    require_current_producer=True,
                )
            self.assertIn(
                "provider_producer_code_state_stale",
                stale["problems"],
            )

    def test_provider_live_result_requires_actual_unreal_import(self):
        self.assertTrue(validate_provider_live_result({
            "status": "ok",
            "unreal_result": {
                "status": "imported_ok",
                "asset_paths": ["/Game/Trees/SK_leaf_sample_01"],
            },
        })["ok"])
        self.assertFalse(validate_provider_live_result({
            "status": "exported_pending_unreal",
            "unreal_result": {},
        })["ok"])

    def test_headless_manifest_checkout_uses_exact_existing_read_only_packages(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = root / "MyProject.uproject"
            package = root / "Content" / "Trees" / "Assembly" / "SK_Tree.uasset"
            package.parent.mkdir(parents=True)
            project.write_text("{}", encoding="utf-8")
            package.write_bytes(b"asset")
            package.chmod(stat.S_IREAD)
            calls = []

            class Completed:
                returncode = 0
                stdout = ""
                stderr = ""

            def run_factory(command, **_kwargs):
                calls.append(command)
                package.chmod(stat.S_IREAD | stat.S_IWRITE)
                return Completed()

            result = checkout_headless_manifest_assets(
                {"items": [{"checkout_asset_paths": [
                    "/Game/Trees/Assembly/SK_Tree",
                    "/Game/Trees/Assembly/SK_Tree.SK_Tree",
                    "/Game/Trees/Assembly/NewPart",
                    "/Engine/EngineAsset",
                ]}]},
                project,
                p4_client="UnrealProjects",
                run_factory=run_factory,
            )

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][:4], ["p4", "-c", "UnrealProjects", "edit"])
        self.assertEqual(calls[0][4:], [str(package)])
        self.assertEqual(result["checked_out"], [str(package)])

    def test_headless_manifest_checkout_fails_if_p4_leaves_file_read_only(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = root / "MyProject.uproject"
            package = root / "Content" / "Trees" / "SK_Tree.uasset"
            package.parent.mkdir(parents=True)
            project.write_text("{}", encoding="utf-8")
            package.write_bytes(b"asset")
            package.chmod(stat.S_IREAD)

            class Completed:
                returncode = 0
                stdout = ""
                stderr = ""

            with self.assertRaisesRegex(
                ExactPushError, "packages remain read-only"
            ):
                checkout_headless_manifest_assets(
                    {"items": [{"checkout_asset_paths": [
                        "/Game/Trees/SK_Tree",
                    ]}]},
                    project,
                    run_factory=lambda *_args, **_kwargs: Completed(),
                )

    def test_combined_manifest_v2_externalizes_items_and_preserves_index_fields(
        self,
    ):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest_path = root / "combined.json"
            report_path = root / "item_report.json"
            item = {
                "schema_version": 1,
                "queue_id": "provider.spm",
                "fingerprint": "provider-v1",
                "depends_on_queue_ids": ["base.spm"],
                "report_path": str(report_path),
                "checkout_asset_paths": ["/Game/Trees/SK_Provider"],
                "cluster_assembly": {"large": "x" * 10000},
            }

            manifest = write_combined_lazy_manifest(
                manifest_path,
                {
                    "checkpoint_path": str(root / "checkpoint.json"),
                    "report_path": str(root / "batch.json"),
                    "max_item_crash_retries": 2,
                },
                [{"item": item}],
            )

            persisted = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest, persisted)
            self.assertEqual(persisted["schema_version"], 2)
            self.assertEqual(
                persisted["item_storage"]["kind"], "external_json"
            )
            item_ref = persisted["items"][0]
            self.assertEqual(item_ref["queue_id"], item["queue_id"])
            self.assertEqual(item_ref["fingerprint"], item["fingerprint"])
            self.assertEqual(
                item_ref["depends_on_queue_ids"],
                item["depends_on_queue_ids"],
            )
            self.assertEqual(
                item_ref["checkout_asset_paths"],
                item["checkout_asset_paths"],
            )
            payload_path = root / Path(item_ref["payload_relpath"])
            self.assertEqual(
                json.loads(payload_path.read_text(encoding="utf-8")),
                item,
            )
            self.assertLess(
                manifest_path.stat().st_size,
                payload_path.stat().st_size,
            )

    def test_receipt_refresh_is_scoped_to_exact_current_spm(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            asset = root / "tree_Weeping_Willow"
            asset.mkdir()
            spm = asset / "SK_tree_Weeping_Willow_01.spm"
            report = root / "receipt.json"
            spm.write_bytes(b"current")

            command = build_receipt_refresh_command(
                {"spm": spm},
                report,
            )

            self.assertIn("pcg_texture_audit.py", " ".join(command))
            self.assertEqual(
                command[command.index("--target") + 1],
                str(asset.resolve()),
            )
            self.assertEqual(
                command[command.index("--target-mesh") + 1],
                "SK_tree_Weeping_Willow_01",
            )
            self.assertIn("--cluster-assembly-only", command)
            self.assertEqual(
                command[command.index("--json") + 1],
                str(report.resolve()),
            )

    def test_fleet_assembly_command_precedes_push_with_current_inputs(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            spm = root / "Tree_01.spm"
            blend = spm.with_suffix(".blend")
            blender = root / "blender.exe"
            contract = root / "material.json"
            cluster_contract = root / "cluster_assembly_live.json"
            report = root / "assembly_report.json"
            for path in (spm, blend, blender, contract, cluster_contract):
                path.write_bytes(b"current")

            command = build_assembly_command(
                {"spm": spm},
                blender,
                contract,
                report,
                cluster_assembly_contract=cluster_contract,
                force_native_export=True,
                force_cluster_assembly_rebuild=True,
            )

            self.assertIn("assembly_headless_job.py", " ".join(command))
            self.assertEqual(command[command.index("--spm") + 1], str(spm))
            self.assertEqual(
                command[command.index("--material-contract") + 1],
                str(contract),
            )
            self.assertEqual(
                command[command.index("--cluster-assembly-contract") + 1],
                str(cluster_contract),
            )
            self.assertIn("--force-native-export", command)
            self.assertIn("--force-cluster-assembly-rebuild", command)
            self.assertNotIn("--provider-no-owner-receipt", command)

    def test_provider_assembly_command_has_only_no_owner_receipt_authority(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            spm = root / "tree" / "cluster" / "SK_branch_01.spm"
            blender = root / "blender.exe"
            contract = root / "material.json"
            report = root / "assembly_report.json"
            spm.parent.mkdir(parents=True)
            for path in (spm, blender, contract):
                path.write_bytes(b"current")

            command = build_assembly_command(
                {"spm": spm},
                blender,
                contract,
                report,
                provider_no_owner_receipt=True,
            )

            self.assertEqual(command.count("--provider-no-owner-receipt"), 1)
            self.assertNotIn("--cluster-source-build-only", command)
            self.assertNotIn("--cluster-assembly-contract", command)
            with self.assertRaisesRegex(
                ExactPushError,
                "cannot carry a root Cluster Assembly contract",
            ):
                build_assembly_command(
                    {"spm": spm},
                    blender,
                    contract,
                    report,
                    cluster_assembly_contract=root / "live.json",
                    provider_no_owner_receipt=True,
                )

    def test_fresh_verification_flag_is_explicit_and_command_parity_is_exact(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            spm = root / "Tree_01.spm"
            blender = root / "blender.exe"
            contract = root / "material.json"
            report = root / "assembly.json"
            for path in (spm, blender, contract):
                path.write_bytes(b"current")

            baseline = build_assembly_command(
                {"spm": spm},
                blender,
                contract,
                report,
                force_native_export=True,
            )
            direct = build_assembly_command(
                {"spm": spm},
                blender,
                contract,
                report,
                force_native_export=True,
                fresh_verification_only_export=True,
            )

            self.assertNotIn("--fresh-collision-prune-export", baseline)
            self.assertEqual(
                direct.count("--fresh-collision-prune-export"),
                1,
            )
            self.assertEqual(
                [
                    value
                    for value in direct
                    if value != "--fresh-collision-prune-export"
                ],
                baseline,
            )
            with self.assertRaisesRegex(
                ExactPushError,
                "requires force native export",
            ):
                build_assembly_command(
                    {"spm": spm},
                    blender,
                    contract,
                    report,
                    fresh_verification_only_export=True,
                )

    def test_fleet_rejects_fresh_verification_without_force_before_discovery(self):
        with self.assertRaisesRegex(
            SystemExit,
            "requires --force-native-export",
        ):
            main(["--fresh-verification-only-export"])

        with self.assertRaisesRegex(
            SystemExit,
            "requires --force-native-export",
        ):
            main(["--fresh-collision-prune-export"])

    def test_fresh_verification_opt_in_is_forwarded_to_both_call_sites(self):
        source = Path(__file__).resolve().parents[1] / "cluster_fleet_push.py"
        tree = ast.parse(source.read_text(encoding="utf-8"))
        calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "build_assembly_command"
        ]
        self.assertEqual(len(calls), 2)
        for call in calls:
            keyword = next(
                row
                for row in call.keywords
                if row.arg == "fresh_verification_only_export"
            )
            self.assertIsInstance(keyword.value, ast.Attribute)
            self.assertEqual(keyword.value.attr, "fresh_verification_only_export")

    def test_only_provider_fleet_callsite_can_request_no_owner_receipt(self):
        source = Path(__file__).resolve().parents[1] / "cluster_fleet_push.py"
        tree = ast.parse(source.read_text(encoding="utf-8"))
        calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "build_assembly_command"
        ]
        self.assertEqual(len(calls), 2)
        provider_calls = [
            node
            for node in calls
            if any(
                keyword.arg == "provider_no_owner_receipt"
                and isinstance(keyword.value, ast.Constant)
                and keyword.value.value is True
                for keyword in node.keywords
            )
        ]
        root_calls = [node for node in calls if node not in provider_calls]

        self.assertEqual(len(provider_calls), 1)
        self.assertEqual(len(root_calls), 1)
        self.assertFalse(any(
            keyword.arg == "cluster_assembly_contract"
            for keyword in provider_calls[0].keywords
        ))
        self.assertTrue(any(
            keyword.arg == "cluster_assembly_contract"
            for keyword in root_calls[0].keywords
        ))
        self.assertFalse(any(
            keyword.arg == "provider_no_owner_receipt"
            for keyword in root_calls[0].keywords
        ))

    def test_assembly_result_requires_exact_attachment_bindings(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            report = root / "assembly_report.json"
            manifest = root / "assembly.json"
            report.write_text(json.dumps({"status": "ok"}), encoding="utf-8")
            manifest.write_text(json.dumps({
                **exact_identity_contract(2),
                "base": {
                    "unmatched_role_components_removed_from_base": 0,
                },
                "preserved_render_components": [{"polygon_count": 3}],
                "parts": [{"bindings": [{"id": 1}, {"id": 2}]}],
            }), encoding="utf-8")

            result = validate_assembly_result(
                report, {"manifest": manifest}
            )

            self.assertTrue(result["ok"])
            self.assertEqual(result["bindings"], 2)
            self.assertEqual(result["preserved_role_polygons_kept"], 3)

    def test_assembly_result_rejects_legacy_node_assignment_contract(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            report = root / "assembly_report.json"
            manifest = root / "assembly.json"
            report.write_text(json.dumps({"status": "ok"}), encoding="utf-8")
            manifest.write_text(json.dumps({
                "placement_contract": {
                    "version": 1,
                    "authored_node_assignment": {
                        "assigned_count": 1,
                    },
                    "degraded_authored_card_binding_count": 0,
                },
                "base": {"unmatched_role_components_removed_from_base": 0},
                "parts": [{"bindings": [{"id": 1}]}],
            }), encoding="utf-8")

            result = validate_assembly_result(report, {"manifest": manifest})

            self.assertFalse(result["ok"])
            self.assertIn(
                "legacy_attachment_identity_contract_present",
                result["problems"],
            )

    def test_assembly_result_accepts_current_pass_through_without_stale_manifest(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            report = root / "assembly_report.json"
            report.write_text(json.dumps({
                "status": "ok",
                "cluster_assembly_manifest": {
                    "status": "pass_through",
                    "content_decision": "pass_through",
                },
            }), encoding="utf-8")

            result = validate_assembly_result(
                report,
                {"manifest": root / "stale_ready_manifest.json"},
            )

            self.assertTrue(result["ok"])
            self.assertTrue(result["pass_through"])
            self.assertEqual(result["parts"], 0)

    def test_saved_pass_through_without_current_summary_fails_closed_once(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            report = root / "assembly_report.json"
            manifest = root / "assembly.json"
            report.write_text(
                json.dumps({"status": "ok"}),
                encoding="utf-8",
            )
            manifest.write_text(json.dumps({
                "status": "pass_through",
                "content_decision": "pass_through",
                "parts": [],
            }), encoding="utf-8")

            result = validate_assembly_result(
                report,
                {"manifest": manifest},
            )

            self.assertFalse(result["ok"])
            self.assertFalse(result["pass_through"])
            self.assertEqual(result["problems"], [
                "current_pass_through_decision_missing_from_assembly_report"
            ])
            self.assertEqual(
                result["policy"],
                "saved_pass_through_not_current_run_authority",
            )

    def test_pass_through_with_preserved_build_is_actionable(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            report = root / "assembly_report.json"
            report.write_text(json.dumps({
                "cluster_assembly_manifest": {
                    "status": "pass_through",
                    "content_decision": "pass_through",
                    "existing_assembly_assets_orphaned": {
                        "status": "action_required",
                        "asset_names": ["Base", "Assembly"],
                    },
                },
            }), encoding="utf-8")

            result = validate_assembly_result(report, {})

            self.assertTrue(result["ok"])
            self.assertEqual(result["problems"], [])
            self.assertEqual(
                result["existing_assembly_assets_orphaned"]["asset_names"],
                ["Base", "Assembly"],
            )

    def test_assembly_result_reports_role_demotion_as_diagnostic(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            report = root / "assembly_report.json"
            manifest = root / "assembly.json"
            report.write_text(json.dumps({
                "status": "ok",
                "cluster_assembly_handoff": {
                    "role_demotions": [{
                        "provider_key": "leaf_side:leaf_willow_side_01",
                        "role": "leaf_side",
                    }],
                },
            }), encoding="utf-8")
            manifest.write_text(json.dumps({
                **exact_identity_contract(1),
                "base": {
                    "unmatched_role_components_removed_from_base": 0,
                },
                "parts": [{"bindings": [{"id": 1}]}],
            }), encoding="utf-8")

            result = validate_assembly_result(
                report,
                {"manifest": manifest},
            )

            self.assertTrue(result["ok"])
            self.assertEqual(result["problems"], [])
            self.assertEqual(
                result["role_demotions"][0]["provider_key"],
                "leaf_side:leaf_willow_side_01",
            )

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

    def test_discovers_current_pass_through_for_live_receipt_revalidation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            asset = root / "tree_blackgum"
            assembly = asset / "assembly"
            assembly.mkdir(parents=True)
            stem = "SK_tree_blackgum_01"
            spm = asset / f"{stem}.spm"
            blend = spm.with_suffix(".blend")
            wind = (
                asset
                / "JSON"
                / f"{stem}_dynamic_wind_import_from_megaplant_groups.json"
            )
            wind.parent.mkdir()
            for path in (spm, blend, wind):
                path.write_bytes(b"current")
            manifest = (
                assembly / f"{stem}_cluster_assembly_bindings.json"
            )
            manifest.write_text(json.dumps({
                "status": "pass_through",
                "content_decision": "pass_through",
                "pass_through_provenance": {
                    "requested_spm": str(spm),
                },
            }), encoding="utf-8")

            targets, missing = discover_current_cluster_targets(root)

        self.assertFalse(missing)
        self.assertEqual([row["stem"] for row in targets], [stem])
        self.assertEqual(
            targets[0]["selection_policy"],
            "current_pass_through_receipt_revalidation",
        )
        self.assertEqual(targets[0]["expected_parts"], 0)

    def test_explicit_current_spm_without_manifest_is_revalidated(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            asset = root / "tree_lauraceae"
            asset.mkdir()
            stem = "SK_tree_Lauraceae_11"
            spm = asset / f"{stem}.spm"
            blend = spm.with_suffix(".blend")
            wind = (
                asset
                / "JSON"
                / f"{stem}_dynamic_wind_import_from_megaplant_groups.json"
            )
            wind.parent.mkdir()
            for path in (spm, blend, wind):
                path.write_bytes(b"current")

            targets, missing = discover_current_cluster_targets(
                root, only=[stem]
            )

        self.assertFalse(missing)
        self.assertEqual([row["stem"] for row in targets], [stem])
        self.assertEqual(
            targets[0]["selection_policy"],
            "explicit_current_spm_revalidation",
        )
        self.assertEqual(
            targets[0]["manifest"].name,
            f"{stem}_cluster_assembly_bindings.json",
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

    def test_live_result_accepts_current_pass_through_without_assembly_build(self):
        target = {
            "pass_through": True,
            "expected_parts": 0,
            "expected_bindings": 0,
        }
        report = {
            "status": "ok",
            "unreal_result": {
                "status": "imported_ok",
                "cluster_assembly": {
                    "status": "skipped",
                    "reason": "no content-driven Assembly manifest",
                },
            },
        }

        result = validate_live_result(report, target)

        self.assertTrue(result["ok"])
        self.assertTrue(result["pass_through"])
        self.assertEqual(result["assembly_status"], "skipped")

    def test_live_result_rejects_pass_through_that_built_an_assembly(self):
        target = {"pass_through": True}
        report = {
            "status": "ok",
            "unreal_result": {
                "status": "imported_ok",
                "cluster_assembly": {"status": "ok", "build": {}},
            },
        }

        result = validate_live_result(report, target)

        self.assertFalse(result["ok"])
        self.assertIn("pass_through_assembly_not_skipped", result["problems"])

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
