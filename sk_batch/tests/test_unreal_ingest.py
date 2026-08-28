import importlib.util
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path


RUNNER = Path(__file__).resolve().parents[1] / "unreal_ingest.py"


def load_runner(monkeypatch=None):
    unreal_module = types.ModuleType("unreal")
    previous_unreal = sys.modules.get("unreal")
    if monkeypatch is None:
        sys.modules["unreal"] = unreal_module
    else:
        monkeypatch.setitem(sys.modules, "unreal", unreal_module)

    try:
        spec = importlib.util.spec_from_file_location(
            "test_sk_batch_unreal_ingest",
            RUNNER,
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    finally:
        if monkeypatch is None:
            if previous_unreal is None:
                sys.modules.pop("unreal", None)
            else:
                sys.modules["unreal"] = previous_unreal
    return module


def write_manifest(tmp_path, items, max_retries=2):
    manifest = tmp_path / "manifest.json"
    checkpoint = tmp_path / "checkpoint.json"
    report = tmp_path / "report.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "checkpoint_path": str(checkpoint),
                "report_path": str(report),
                "max_item_crash_retries": max_retries,
                "items": items,
            }
        ),
        encoding="utf-8",
    )
    return manifest, checkpoint, report


def item(queue_id, fingerprint):
    return {
        "queue_id": queue_id,
        "fingerprint": fingerprint,
        "report_path": "",
    }


def test_retry_metadata_is_copied_from_manifest_to_batch_report(tmp_path):
    runner = load_runner()
    manifest, _checkpoint, report = write_manifest(tmp_path, [])
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["retry"] = {
        "kind": "failed_blender_export_and_unreal_retry",
        "partition": "blender_export",
    }
    payload["recovery"] = {
        "kind": "failed_results_retry_unreal_only",
    }
    manifest.write_text(json.dumps(payload), encoding="utf-8")

    result = runner.run_manifest(manifest)

    persisted = json.loads(report.read_text(encoding="utf-8"))
    assert result["retry"] == payload["retry"]
    assert persisted["retry"] == payload["retry"]
    assert persisted["recovery"] == payload["recovery"]


class DynamicWindFinalSkeletonContractTests(unittest.TestCase):
    def test_dependency_orchestrated_tree_can_skip_missing_assembly_payload(self):
        runner = load_runner()
        result = runner._ingest_cluster_assembly(
            None,
            {
                "dependency_orchestrated": True,
                "cluster_assembly": None,
            },
            {"status": "ok"},
        )
        self.assertEqual(result["status"], "skipped")

    def test_ordinary_non_cluster_asset_can_skip_missing_assembly(self):
        runner = load_runner()
        result = runner._ingest_cluster_assembly(
            None,
            {"cluster_assembly": None},
            {"status": "ok"},
        )
        self.assertEqual(result["status"], "skipped")

    def test_content_absent_assembly_plan_passes_through_without_a_toggle(self):
        runner = load_runner()
        result = runner._ingest_cluster_assembly(
            None,
            {
                "cluster_assembly": {
                    "ingest_plan": {"status": "pass_through"}
                }
            },
            {"status": "ok"},
        )
        self.assertEqual(result["status"], "pass_through")

    def test_ready_assembly_requires_full_final_skeleton_wind_success(self):
        runner = load_runner()
        with self.assertRaisesRegex(RuntimeError, "final-skeleton wind"):
            runner._ingest_cluster_assembly(
                None,
                {
                    "cluster_assembly": {
                        "manifest": {"status": "ready"},
                        "ingest_plan": {"status": "ready"},
                    }
                },
                {"status": "skipped"},
            )

    @staticmethod
    def _runner_with_result(result):
        runner = load_runner()
        runner.unreal.EditorAssetLibrary = types.SimpleNamespace(
            load_asset=lambda _path: object()
        )
        runner.unreal.CodexDynamicWindImportLibrary = types.SimpleNamespace(
            import_dynamic_wind_json_to_skeletal_mesh=lambda _mesh, _json: result
        )
        return runner

    def test_confirmed_final_skeleton_contract_passes(self):
        result = json.dumps(
            {
                "success": True,
                "skeleton_contract": "final_skeleton_v2",
                "skeleton_hash": "1" * 40,
                "final_bones": 1688,
                "resolved_joints": 1688,
            }
        )
        runner = self._runner_with_result(result)
        with tempfile.TemporaryDirectory() as temporary:
            wind_json = Path(temporary) / "wind.json"
            wind_json.write_text("{}", encoding="utf-8")
            report = runner._apply_dynamic_wind(
                {"wind_json": str(wind_json), "mesh_path": "/Game/Test/SK_Tree"}
            )

        self.assertEqual(report["status"], "ok")
        self.assertEqual(
            report["result"]["skeleton_contract"], "final_skeleton_v2"
        )

    def test_disabled_wind_contract_requires_confirmed_disabled_asset_data(self):
        result = json.dumps(
            {
                "success": True,
                "skeleton_contract": "final_skeleton_v2",
                "skeleton_hash": "1" * 40,
                "final_bones": 1688,
                "resolved_joints": 1688,
                "is_enabled": False,
                "disabled_coefficients_zeroed": True,
            }
        )
        runner = self._runner_with_result(result)
        with tempfile.TemporaryDirectory() as temporary:
            wind_json = Path(temporary) / "wind.json"
            wind_json.write_text(
                json.dumps({"bIsEnabled": False}),
                encoding="utf-8",
            )
            report = runner._apply_dynamic_wind(
                {"wind_json": str(wind_json), "mesh_path": "/Game/Test/SK_Dead"}
            )

        self.assertEqual(report["status"], "ok")
        self.assertIs(report["result"]["is_enabled"], False)
        self.assertIs(
            report["result"]["disabled_coefficients_zeroed"],
            True,
        )

    def test_disabled_wind_coefficient_proof_is_diagnostic(self):
        result = json.dumps(
            {
                "success": True,
                "skeleton_contract": "final_skeleton_v2",
                "skeleton_hash": "1" * 40,
                "final_bones": 1688,
                "resolved_joints": 1688,
                "is_enabled": False,
            }
        )
        runner = self._runner_with_result(result)
        with tempfile.TemporaryDirectory() as temporary:
            wind_json = Path(temporary) / "wind.json"
            wind_json.write_text(
                json.dumps({"bIsEnabled": False}),
                encoding="utf-8",
            )
            report = runner._apply_dynamic_wind(
                {
                    "wind_json": str(wind_json),
                    "mesh_path": "/Game/Test/SK_Dead",
                }
            )

        self.assertEqual(report["status"], "ok")
        self.assertIs(report["result"]["contract_fields_are_diagnostic"], True)

    def test_disabled_wind_enabled_state_is_diagnostic(self):
        result = json.dumps(
            {
                "success": True,
                "skeleton_contract": "final_skeleton_v2",
                "skeleton_hash": "1" * 40,
                "final_bones": 1688,
                "resolved_joints": 1688,
                "is_enabled": True,
            }
        )
        runner = self._runner_with_result(result)
        with tempfile.TemporaryDirectory() as temporary:
            wind_json = Path(temporary) / "wind.json"
            wind_json.write_text(
                json.dumps({"bIsEnabled": False}),
                encoding="utf-8",
            )
            report = runner._apply_dynamic_wind(
                {
                    "wind_json": str(wind_json),
                    "mesh_path": "/Game/Test/SK_Dead",
                }
            )

        self.assertEqual(report["status"], "ok")
        self.assertIs(report["result"]["contract_fields_are_diagnostic"], True)

    def test_shared_none_preset_accepts_effective_state_from_unreal_profile(self):
        result = json.dumps(
            {
                "success": True,
                "skeleton_contract": "final_skeleton_v2",
                "skeleton_hash": "1" * 40,
                "final_bones": 1688,
                "resolved_joints": 1688,
                "response_preset_contract": "shared_response_v1",
                "response_preset": "NONE",
                "response_profile_applied": True,
                "source_default_is_enabled": False,
                "effective_is_enabled": True,
                "is_enabled": True,
                "disabled_coefficients_zeroed": False,
                "production_provider_contract": "shared_provider_v1",
                "production_provider": "/Game/Codex/DynamicWind/DW_Vegetation_WindProvider.DW_Vegetation_WindProvider",
                "pcg_provider_sync": {
                    "success": True,
                    "contract": "shared_provider_v1",
                    "provider": "/Game/Codex/DynamicWind/DW_Vegetation_WindProvider.DW_Vegetation_WindProvider",
                    "matched_rows": 3,
                    "changed_rows": 2,
                    "target_assets": ["/Game/PCG/DataBase/DA_Test_SK"],
                    "changed_assets": ["/Game/PCG/DataBase/DA_Test_SK"],
                },
            }
        )
        runner = self._runner_with_result(result)
        with tempfile.TemporaryDirectory() as temporary:
            wind_json = Path(temporary) / "wind.json"
            wind_json.write_text(
                json.dumps(
                    {
                        "bIsEnabled": False,
                        "WindResponsePresetContract": {
                            "SchemaVersion": 1,
                            "Preset": "NONE",
                            "SimulationGroupBases": [],
                        },
                    }
                ),
                encoding="utf-8",
            )
            report = runner._apply_dynamic_wind(
                {"wind_json": str(wind_json), "mesh_path": "/Game/Test/SK_Dead"}
            )

        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["result"]["response_preset"], "NONE")
        self.assertIs(report["result"]["effective_is_enabled"], True)

    def test_shared_response_provider_sync_is_diagnostic(self):
        result = json.dumps(
            {
                "success": True,
                "skeleton_contract": "final_skeleton_v2",
                "skeleton_hash": "1" * 40,
                "final_bones": 1688,
                "resolved_joints": 1688,
                "response_preset_contract": "shared_response_v1",
                "response_preset": "WEED",
                "response_profile_applied": True,
                "effective_is_enabled": True,
            }
        )
        runner = self._runner_with_result(result)
        with tempfile.TemporaryDirectory() as temporary:
            wind_json = Path(temporary) / "wind.json"
            wind_json.write_text(
                json.dumps(
                    {
                        "WindResponsePresetContract": {
                            "SchemaVersion": 1,
                            "Preset": "WEED",
                            "SimulationGroupBases": [],
                        }
                    }
                ),
                encoding="utf-8",
            )
            report = runner._apply_dynamic_wind(
                {
                    "wind_json": str(wind_json),
                    "mesh_path": "/Game/Test/SK_Weed",
                }
            )

        self.assertEqual(report["status"], "ok")
        self.assertIs(report["result"]["contract_fields_are_diagnostic"], True)

    def test_provider_checkout_preview_adds_exact_pcg_targets(self):
        runner = load_runner()
        checked_out = []
        runner.unreal.EditorAssetLibrary = types.SimpleNamespace(
            does_asset_exist=lambda _path: True
        )
        runner.unreal.EditorAssetSubsystem = object()
        runner.unreal.get_editor_subsystem = lambda _kind: types.SimpleNamespace(
            checkout_asset=lambda path: checked_out.append(path) or True
        )
        provider = "/Game/Codex/DynamicWind/DW_Vegetation_WindProvider.DW_Vegetation_WindProvider"
        runner.unreal.CodexDynamicWindResponseLibrary = types.SimpleNamespace(
            get_pcg_provider_binding_targets_for_mesh_path=lambda _path: json.dumps(
                {
                    "success": True,
                    "contract": "shared_provider_v1",
                    "provider": provider,
                    "target_assets": ["/Game/PCG/DataBase/DA_Test_SK"],
                }
            )
        )
        with tempfile.TemporaryDirectory() as temporary:
            wind_json = Path(temporary) / "wind.json"
            wind_json.write_text(
                json.dumps(
                    {
                        "WindResponsePresetContract": {
                            "SchemaVersion": 1,
                            "Preset": "WEED",
                            "SimulationGroupBases": [],
                        }
                    }
                ),
                encoding="utf-8",
            )
            item = {
                "wind_json": str(wind_json),
                "mesh_path": "/Game/Test/SK_Weed",
                "checkout_asset_paths": ["/Game/Test/SK_Weed"],
            }
            report = runner._checkout_existing_assets(item)

        self.assertEqual(
            checked_out,
            ["/Game/Test/SK_Weed", "/Game/PCG/DataBase/DA_Test_SK"],
        )
        self.assertEqual(report["provider_binding"]["provider"], provider)
        self.assertEqual(
            item["_pcg_provider_binding_checkout"]["target_assets"],
            ["/Game/PCG/DataBase/DA_Test_SK"],
        )

    def test_shared_response_confirmation_is_diagnostic(self):
        result = json.dumps(
            {
                "success": True,
                "skeleton_contract": "final_skeleton_v2",
                "skeleton_hash": "1" * 40,
                "final_bones": 1688,
                "resolved_joints": 1688,
            }
        )
        runner = self._runner_with_result(result)
        with tempfile.TemporaryDirectory() as temporary:
            wind_json = Path(temporary) / "wind.json"
            wind_json.write_text(
                json.dumps(
                    {
                        "WindResponsePresetContract": {
                            "SchemaVersion": 1,
                            "Preset": "TREE",
                            "SimulationGroupBases": [],
                        }
                    }
                ),
                encoding="utf-8",
            )
            report = runner._apply_dynamic_wind(
                {
                    "wind_json": str(wind_json),
                    "mesh_path": "/Game/Test/SK_Tree",
                }
            )

        self.assertEqual(report["status"], "ok")
        self.assertIs(report["result"]["contract_fields_are_diagnostic"], True)

    def test_normalized_cluster_prototype_skips_source_rig_wind(self):
        runner = load_runner()
        report = runner._apply_dynamic_wind(
            {
                "wind_json": None,
                "wind_policy": {
                    "mode": "deferred_to_final_assembly",
                    "requires_json": False,
                    "reason": "apply after final Assembly rebinding",
                },
            }
        )

        self.assertEqual(report["status"], "skipped")
        self.assertEqual(
            report["policy"]["mode"],
            "deferred_to_final_assembly",
        )

    def test_required_wind_contract_without_path_stops_ingest(self):
        runner = load_runner()
        with self.assertRaisesRegex(RuntimeError, "requires final-skeleton"):
            runner._apply_dynamic_wind(
                {
                    "wind_json": None,
                    "wind_policy": {
                        "mode": "final_skeleton",
                        "requires_json": True,
                    },
                }
            )

    def test_failed_name_index_contract_stops_ingest(self):
        runner = self._runner_with_result(
            json.dumps(
                {
                    "success": False,
                    "error": "wind name/index/parent does not match final skeleton",
                }
            )
        )
        with tempfile.TemporaryDirectory() as temporary:
            wind_json = Path(temporary) / "wind.json"
            wind_json.write_text("{}", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "final-skeleton contract failed"):
                runner._apply_dynamic_wind(
                    {
                        "wind_json": str(wind_json),
                        "mesh_path": "/Game/Test/SK_Tree",
                    }
                )

    def test_success_without_skeleton_hash_is_accepted(self):
        runner = self._runner_with_result(
            json.dumps(
                {
                    "success": True,
                    "skeleton_contract": "final_skeleton_v2",
                }
            )
        )
        with tempfile.TemporaryDirectory() as temporary:
            wind_json = Path(temporary) / "wind.json"
            wind_json.write_text("{}", encoding="utf-8")
            report = runner._apply_dynamic_wind(
                {
                    "wind_json": str(wind_json),
                    "mesh_path": "/Game/Test/SK_Tree",
                }
            )

        self.assertEqual(report["status"], "ok")
        self.assertIs(report["result"]["contract_fields_are_diagnostic"], True)

    def test_transient_instanced_dynamic_wind_runtime_probe_is_required(self):
        runner = load_runner()

        class FakeMesh:
            pass

        mesh = FakeMesh()
        runner.unreal.SkeletalMesh = FakeMesh
        runner.unreal.EditorAssetLibrary = types.SimpleNamespace(
            load_asset=lambda _path: mesh
        )
        runner._editor_world = lambda: object()
        runner.unreal.CodexDynamicWindDebugLibrary = types.SimpleNamespace(
            run_instanced_dynamic_wind_runtime_probe=lambda _world, _mesh: json.dumps(
                {
                    "success": True,
                    "transient_component": True,
                    "level_actor_created": False,
                    "validation": {
                        "render_proxy_created": True,
                        "provider_is_dynamic_wind_data": True,
                    },
                }
            )
        )

        report = runner._validate_instanced_dynamic_wind_runtime(
            "/Game/Codex/Tests/Assembly"
        )
        self.assertTrue(report["success"])
        self.assertTrue(report["transient_component"])
        self.assertFalse(report["level_actor_created"])

    def test_failed_instanced_dynamic_wind_runtime_probe_stops_ingest(self):
        runner = load_runner()

        class FakeMesh:
            pass

        runner.unreal.SkeletalMesh = FakeMesh
        runner.unreal.EditorAssetLibrary = types.SimpleNamespace(
            load_asset=lambda _path: FakeMesh()
        )
        runner._editor_world = lambda: object()
        runner.unreal.CodexDynamicWindDebugLibrary = types.SimpleNamespace(
            run_instanced_dynamic_wind_runtime_probe=lambda _world, _mesh: json.dumps(
                {"success": False, "error": "render proxy missing"}
            )
        )

        with self.assertRaisesRegex(RuntimeError, "runtime probe failed"):
            runner._validate_instanced_dynamic_wind_runtime(
                "/Game/Codex/Tests/Assembly"
            )

    def test_two_phase_runtime_probe_returns_pending_token_then_passes(self):
        runner = load_runner()

        class FakeMesh:
            pass

        mesh = FakeMesh()
        runner.unreal.SkeletalMesh = FakeMesh
        runner.unreal.EditorAssetLibrary = types.SimpleNamespace(
            load_asset=lambda _path: mesh
        )
        runner._editor_world = lambda: object()
        finish_results = iter(
            [
                {"success": True, "status": "pending"},
                {
                    "success": True,
                    "status": "passed",
                    "render_frame_residency_verified": True,
                },
            ]
        )
        runner.unreal.CodexDynamicWindDebugLibrary = types.SimpleNamespace(
            begin_instanced_dynamic_wind_runtime_probe=(
                lambda _world, _mesh: json.dumps(
                    {
                        "success": True,
                        "status": "pending",
                        "probe_token": "probe-1",
                    }
                )
            ),
            finish_instanced_dynamic_wind_runtime_probe=(
                lambda _world, _token: json.dumps(next(finish_results))
            ),
        )

        begin = runner._begin_instanced_dynamic_wind_runtime(
            "/Game/Codex/Tests/Assembly"
        )
        self.assertEqual(begin["probe_token"], "probe-1")
        self.assertEqual(
            runner._finish_instanced_dynamic_wind_runtime("probe-1")["status"],
            "pending",
        )
        finish = runner._finish_instanced_dynamic_wind_runtime("probe-1")
        self.assertEqual(finish["status"], "passed")
        self.assertTrue(finish["render_frame_residency_verified"])


def test_data_error_is_item_local_and_queue_continues(tmp_path, monkeypatch):
    runner = load_runner(monkeypatch)
    manifest, checkpoint, _report = write_manifest(
        tmp_path,
        [item("bad", "bad-v1"), item("good", "good-v1")],
    )

    def ingest(current):
        if current["queue_id"] == "bad":
            raise ValueError("bad handoff data")
        return {"status": "imported_ok", "saved": ["/Game/good"]}

    monkeypatch.setattr(runner, "ingest_item", ingest)
    result = runner.run_manifest(manifest)

    assert result["items"]["bad"]["status"] == "data_error"
    assert result["items"]["good"]["status"] == "imported_ok"
    assert json.loads(checkpoint.read_text(encoding="utf-8"))["complete"] is True


def test_headless_queue_collects_transient_objects_after_each_item(
    tmp_path, monkeypatch
):
    runner = load_runner(monkeypatch)
    manifest, _checkpoint, _report = write_manifest(
        tmp_path,
        [item("first", "first-v1"), item("second", "second-v1")],
    )
    unreal_collections = []
    python_collections = []
    runner.unreal.collect_garbage = lambda: unreal_collections.append(True)
    monkeypatch.setattr(
        runner.gc,
        "collect",
        lambda: python_collections.append(True),
    )
    monkeypatch.setattr(
        runner,
        "ingest_item",
        lambda _current: {"status": "imported_ok"},
    )

    result = runner.run_manifest(manifest)

    assert result["counts"] == {"imported_ok": 2}
    assert unreal_collections == [True, True]
    assert python_collections == [True, True]


def test_manifest_dependencies_run_provider_before_tree(tmp_path, monkeypatch):
    runner = load_runner(monkeypatch)
    root = item("tree", "tree-v1")
    root["depends_on_queue_ids"] = ["cluster"]
    manifest, _checkpoint, _report = write_manifest(
        tmp_path,
        [root, item("cluster", "cluster-v1")],
    )
    calls = []
    monkeypatch.setattr(
        runner,
        "ingest_item",
        lambda current: calls.append(current["queue_id"])
        or {"status": "imported_ok"},
    )

    result = runner.run_manifest(manifest)

    assert calls == ["cluster", "tree"]
    assert result["items"]["tree"]["status"] == "imported_ok"


def test_failed_cluster_marks_dependent_tree_not_run(tmp_path, monkeypatch):
    runner = load_runner(monkeypatch)
    root = item("tree", "tree-v1")
    root["depends_on_queue_ids"] = ["cluster"]
    manifest, _checkpoint, _report = write_manifest(
        tmp_path,
        [root, item("cluster", "cluster-v1"), item("other", "other-v1")],
    )
    calls = []

    def ingest(current):
        calls.append(current["queue_id"])
        if current["queue_id"] == "cluster":
            raise RuntimeError("cluster failed")
        return {"status": "imported_ok"}

    monkeypatch.setattr(runner, "ingest_item", ingest)
    result = runner.run_manifest(manifest)

    assert "tree" not in calls
    assert "other" in calls
    assert result["items"]["cluster"]["status"] == "data_error"
    assert result["items"]["tree"]["status"] == "not_run"
    assert "required Cluster Push dependency" in result["items"]["tree"]["message"]


def test_cached_cluster_is_verified_in_unreal_before_import(
    tmp_path, monkeypatch
):
    runner = load_runner(monkeypatch)
    provider = item("cluster", "cluster-v1")
    provider.update(
        {
            "verify_existing_assets": True,
            "assets": [
                {
                    "asset_data": {
                        "asset_path": "/Game/Tree/Cluster/SK_cluster_01"
                    }
                }
            ],
        }
    )
    manifest, _checkpoint, _report = write_manifest(
        tmp_path, [provider]
    )
    runner.unreal.EditorAssetLibrary = types.SimpleNamespace(
        does_asset_exist=lambda _path: True
    )
    monkeypatch.setattr(
        runner,
        "ingest_item",
        lambda _current: (_ for _ in ()).throw(
            AssertionError("current cached asset must not be reimported")
        ),
    )

    result = runner.run_manifest(manifest)

    assert result["items"]["cluster"]["status"] == "imported_ok"
    assert result["items"]["cluster"]["asset_cache"]["status"] == "current"


def test_missing_cached_cluster_asset_is_reimported(tmp_path, monkeypatch):
    runner = load_runner(monkeypatch)
    provider = item("cluster", "cluster-v1")
    provider.update(
        {
            "verify_existing_assets": True,
            "assets": [
                {
                    "asset_data": {
                        "asset_path": "/Game/Tree/Cluster/SK_cluster_01"
                    }
                }
            ],
        }
    )
    manifest, _checkpoint, _report = write_manifest(
        tmp_path, [provider]
    )
    runner.unreal.EditorAssetLibrary = types.SimpleNamespace(
        does_asset_exist=lambda _path: False
    )
    calls = []
    monkeypatch.setattr(
        runner,
        "ingest_item",
        lambda current: calls.append(current["queue_id"])
        or {"status": "imported_ok"},
    )

    result = runner.run_manifest(manifest)

    assert calls == ["cluster"]
    assert (
        result["items"]["cluster"]["asset_cache_preflight"]["status"]
        == "missing"
    )


def test_cached_tree_verifies_every_ready_assembly_contract_asset(monkeypatch):
    runner = load_runner(monkeypatch)
    tree = item("tree", "tree-v1")
    tree.update(
        {
            "assets": [{
                "asset_data": {
                    "asset_path": "/Game/Tree/SK_Tree_01"
                }
            }],
            "cluster_assembly": {
                "ingest_plan": {
                    "status": "ready",
                    "asset_contract": {
                        "full_skeletal_mesh": "/Game/Tree/SK_Tree_01",
                        "base_skeletal_mesh": (
                            "/Game/Tree/Assembly/SK_Tree_01_NA_Base"
                        ),
                        "parts": {
                            "branch": (
                                "/Game/Tree/Assembly/"
                                "SK_Tree_01_NA_Branch_01"
                            ),
                            "leaf": (
                                "/Game/Tree/Assembly/"
                                "SK_Tree_01_NA_Leaf_01"
                            ),
                        },
                        "assembly": (
                            "/Game/Tree/Assembly/"
                            "SK_Tree_01_NaniteAssembly"
                        ),
                    },
                }
            },
        }
    )
    runner.unreal.EditorAssetLibrary = types.SimpleNamespace(
        does_asset_exist=lambda _path: True
    )

    result = runner._verify_manifest_assets_exist(tree)

    assert result["complete"] is True
    assert result["status"] == "current"
    assert len(result["asset_paths"]) == 5
    assert any(
        path.endswith("NaniteAssembly")
        for path in result["asset_path_groups"]["cluster_assembly"]
    )


def test_missing_cached_assembly_part_reimports_tree(tmp_path, monkeypatch):
    runner = load_runner(monkeypatch)
    missing_part = "/Game/Tree/Assembly/SK_Tree_01_NA_Leaf_01"
    tree = item("tree", "tree-v1")
    tree.update(
        {
            "verify_existing_assets": True,
            "assets": [{
                "asset_data": {
                    "asset_path": "/Game/Tree/SK_Tree_01"
                }
            }],
            "cluster_assembly": {
                "ingest_plan": {
                    "status": "ready",
                    "asset_contract": {
                        "full_skeletal_mesh": "/Game/Tree/SK_Tree_01",
                        "base_skeletal_mesh": (
                            "/Game/Tree/Assembly/SK_Tree_01_NA_Base"
                        ),
                        "parts": {"leaf": missing_part},
                        "assembly": (
                            "/Game/Tree/Assembly/"
                            "SK_Tree_01_NaniteAssembly"
                        ),
                    },
                }
            },
        }
    )
    manifest, _checkpoint, _report = write_manifest(tmp_path, [tree])
    runner.unreal.EditorAssetLibrary = types.SimpleNamespace(
        does_asset_exist=lambda path: path != missing_part
    )
    calls = []
    monkeypatch.setattr(
        runner,
        "ingest_item",
        lambda current: calls.append(current["queue_id"])
        or {"status": "imported_ok"},
    )

    result = runner.run_manifest(manifest)

    assert calls == ["tree"]
    preflight = result["items"]["tree"]["asset_cache_preflight"]
    assert preflight["missing_asset_paths"] == [missing_part]
    assert preflight["missing_asset_paths_by_group"][
        "cluster_assembly"
    ] == [missing_part]


def test_runtime_pending_checkpoint_finishes_without_reimport(tmp_path, monkeypatch):
    runner = load_runner(monkeypatch)
    manifest, checkpoint, report = write_manifest(
        tmp_path,
        [item("elm", "elm-v1")],
    )
    import_calls = []

    def ingest(current):
        import_calls.append(current["queue_id"])
        return {
            "status": "runtime_pending",
            "cluster_assembly": {
                "status": "runtime_pending",
                "runtime": {
                    "success": True,
                    "status": "pending",
                    "probe_token": "probe-elm",
                },
            },
        }

    monkeypatch.setattr(runner, "ingest_item", ingest)
    first = runner.run_manifest(manifest)
    assert first["status"] == "runtime_pending"
    assert first["items"]["elm"]["status"] == "runtime_pending"
    assert json.loads(checkpoint.read_text(encoding="utf-8"))["complete"] is False

    monkeypatch.setattr(
        runner,
        "_finish_instanced_dynamic_wind_runtime",
        lambda _token: {
            "success": True,
            "status": "passed",
            "render_frame_residency_verified": True,
        },
    )
    finished = runner.finish_runtime_probe(checkpoint, report, "elm")
    assert finished["status"] == "imported_ok"
    assert finished["cluster_assembly"]["runtime"]["status"] == "passed"
    assert import_calls == ["elm"]
    assert json.loads(checkpoint.read_text(encoding="utf-8"))["complete"] is True


def test_headless_runtime_validation_is_deferred_without_starting_probe(monkeypatch):
    runner = load_runner(monkeypatch)
    monkeypatch.setenv("SK_BATCH_MANIFEST_PATH", "headless-manifest.json")
    monkeypatch.setattr(
        runner,
        "_begin_instanced_dynamic_wind_runtime",
        lambda _path: (_ for _ in ()).throw(AssertionError("probe started")),
    )
    assembly = {
        "status": "ready_for_runtime",
        "build": {"assembly": "/Game/Meshes/Tree/Assembly"},
    }

    status = runner._prepare_assembly_runtime_validation(assembly)

    assert status == "imported_ok"
    assert assembly["status"] == "ok"
    assert assembly["runtime"]["status"] == "headless_deferred"
    assert assembly["runtime"]["render_frame_validation_performed"] is False


def test_headless_restart_recovers_pending_without_reimport(tmp_path, monkeypatch):
    runner = load_runner(monkeypatch)
    manifest, checkpoint, report = write_manifest(
        tmp_path,
        [item("elm", "elm-v1")],
    )
    import_calls = []

    def ingest(current):
        import_calls.append(current["queue_id"])
        return {
            "status": "runtime_pending",
            "cluster_assembly": {
                "status": "runtime_pending",
                "runtime": {
                    "success": True,
                    "status": "pending",
                    "probe_token": "probe-from-previous-commandlet",
                },
            },
        }

    monkeypatch.delenv("SK_BATCH_MANIFEST_PATH", raising=False)
    monkeypatch.setattr(runner, "ingest_item", ingest)
    first = runner.run_manifest(manifest)
    assert first["status"] == "runtime_pending"

    monkeypatch.setenv("SK_BATCH_MANIFEST_PATH", str(manifest))
    recovered = runner.run_manifest(manifest, checkpoint, report)

    state = recovered["items"]["elm"]
    assert recovered["status"] == "complete"
    assert state["status"] == "imported_ok"
    assert state["cluster_assembly"]["status"] == "ok"
    assert state["cluster_assembly"]["runtime"]["status"] == "headless_deferred"
    assert state["cluster_assembly"]["runtime"]["recovered_from_pending"] is True
    assert import_calls == ["elm"]


def test_runtime_pending_checkpoint_cancel_is_terminal(tmp_path, monkeypatch):
    runner = load_runner(monkeypatch)
    manifest, checkpoint, report = write_manifest(
        tmp_path,
        [item("elm", "elm-v1")],
    )
    monkeypatch.setattr(
        runner,
        "ingest_item",
        lambda _current: {
            "status": "runtime_pending",
            "cluster_assembly": {
                "status": "runtime_pending",
                "runtime": {
                    "success": True,
                    "status": "pending",
                    "probe_token": "probe-elm",
                },
            },
        },
    )
    runner.run_manifest(manifest)
    monkeypatch.setattr(
        runner,
        "_cancel_instanced_dynamic_wind_runtime",
        lambda token: {"success": True, "status": "cancelled", "token": token},
    )

    cancelled = runner.cancel_runtime_probe(
        checkpoint,
        report,
        "elm",
        "cross-frame timeout",
    )

    assert cancelled["status"] == "data_error"
    assert cancelled["message"] == "cross-frame timeout"
    assert cancelled["runtime_cancel"]["status"] == "cancelled"
    assert json.loads(checkpoint.read_text(encoding="utf-8"))["complete"] is True


def test_runtime_cancel_failure_preserves_terminal_report(tmp_path, monkeypatch):
    runner = load_runner(monkeypatch)
    manifest, checkpoint, report = write_manifest(
        tmp_path,
        [item("elm", "elm-v1")],
    )
    monkeypatch.setattr(
        runner,
        "ingest_item",
        lambda _current: {
            "status": "runtime_pending",
            "cluster_assembly": {
                "status": "runtime_pending",
                "runtime": {
                    "success": True,
                    "status": "pending",
                    "probe_token": "probe-elm",
                },
            },
        },
    )
    runner.run_manifest(manifest)

    def fail_cancel(_token):
        raise RuntimeError("token mismatch")

    monkeypatch.setattr(
        runner,
        "_cancel_instanced_dynamic_wind_runtime",
        fail_cancel,
    )
    cancelled = runner.cancel_runtime_probe(
        checkpoint,
        report,
        "elm",
        "cross-frame timeout",
    )

    assert cancelled["status"] == "data_error"
    assert cancelled["message"] == "cross-frame timeout"
    assert cancelled["cleanup_confirmed"] is False
    assert cancelled["cancel_error"] == "token mismatch"
    assert json.loads(checkpoint.read_text(encoding="utf-8"))["complete"] is True


def test_interrupted_item_is_retried_then_queue_resumes(tmp_path, monkeypatch):
    runner = load_runner(monkeypatch)
    manifest, checkpoint, _report = write_manifest(
        tmp_path,
        [item("first", "first-v1"), item("second", "second-v1")],
        max_retries=1,
    )
    checkpoint.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "complete": False,
                "current_item": "first",
                "items": {
                    "first": {
                        "status": "importing",
                        "fingerprint": "first-v1",
                        "crash_count": 0,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    calls = []
    monkeypatch.setattr(
        runner,
        "ingest_item",
        lambda current: calls.append(current["queue_id"])
        or {"status": "imported_ok"},
    )

    result = runner.run_manifest(manifest)

    assert calls == ["first", "second"]
    assert result["items"]["first"]["status"] == "imported_ok"
    assert result["items"]["first"]["crash_count"] == 1


def test_crash_retry_limit_moves_only_item_to_manual_required(tmp_path, monkeypatch):
    runner = load_runner(monkeypatch)
    manifest, checkpoint, _report = write_manifest(
        tmp_path,
        [item("crashy", "crashy-v1"), item("next", "next-v1")],
        max_retries=1,
    )
    checkpoint.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "complete": False,
                "current_item": "crashy",
                "items": {
                    "crashy": {
                        "status": "importing",
                        "fingerprint": "crashy-v1",
                        "crash_count": 1,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    calls = []
    monkeypatch.setattr(
        runner,
        "ingest_item",
        lambda current: calls.append(current["queue_id"])
        or {"status": "imported_ok"},
    )

    result = runner.run_manifest(manifest)

    assert calls == ["next"]
    assert result["items"]["crashy"]["status"] == "manual_required"
    assert result["items"]["next"]["status"] == "imported_ok"


def test_matching_terminal_fingerprint_is_not_reimported(tmp_path, monkeypatch):
    runner = load_runner(monkeypatch)
    manifest, checkpoint, _report = write_manifest(
        tmp_path,
        [item("cached", "same-v1")],
    )
    checkpoint.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "complete": False,
                "current_item": None,
                "items": {
                    "cached": {
                        "status": "imported_ok",
                        "fingerprint": "same-v1",
                        "crash_count": 0,
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    def unexpected(_current):
        raise AssertionError("cached item must not be imported again")

    monkeypatch.setattr(runner, "ingest_item", unexpected)
    result = runner.run_manifest(manifest)

    assert result["items"]["cached"]["status"] == "imported_ok"


class NaniteVoxelMaterialUsageTests(unittest.TestCase):
    class FakeMaterial:
        def __init__(self, values):
            self.values = dict(values)

        def get_path_name(self):
            return "/Game/Material/M_TreeMaster.M_TreeMaster"

        def get_editor_property(self, name):
            return self.values[name]

        def set_editor_property(self, name, value):
            self.values[name] = value

    def test_missing_voxel_usage_is_checked_out_and_set(self):
        runner = load_runner()
        material = self.FakeMaterial({
            "used_with_skeletal_mesh": True,
            "used_with_nanite": True,
            "used_with_voxels": False,
        })
        checkouts = []
        runner.unreal.EditorAssetSubsystem = object()
        runner.unreal.get_editor_subsystem = lambda _type: types.SimpleNamespace(
            checkout_asset=lambda path: checkouts.append(path) or True
        )

        result = runner._ensure_nanite_voxel_material_usage(material)

        self.assertTrue(material.values["used_with_voxels"])
        self.assertEqual(checkouts, [material.get_path_name()])
        self.assertTrue(result["changed"])

    def test_current_usage_is_read_only(self):
        runner = load_runner()
        material = self.FakeMaterial({
            "used_with_skeletal_mesh": True,
            "used_with_nanite": True,
            "used_with_voxels": True,
        })
        runner.unreal.EditorAssetSubsystem = object()
        runner.unreal.get_editor_subsystem = lambda _type: self.fail(
            "current material must not be checked out"
        )

        result = runner._ensure_nanite_voxel_material_usage(material)

        self.assertFalse(result["changed"])

    def test_unchanged_usage_does_not_recompile_assembly_referencers(self):
        runner = load_runner()
        base = self.FakeMaterial({
            "used_with_skeletal_mesh": True,
            "used_with_nanite": True,
            "used_with_voxels": True,
        })

        class FakeInterface:
            def get_path_name(self):
                return "/Game/Material/MI_Tree.MI_Tree"

            def get_base_material(self):
                return base

        class FakeSlot:
            def get_editor_property(self, name):
                return "M_Tree" if name == "material_slot_name" else FakeInterface()

        class FakeMesh:
            def get_editor_property(self, name):
                assert name == "materials"
                return [FakeSlot()]

        runner.unreal.EditorAssetSubsystem = object()
        runner.unreal.get_editor_subsystem = lambda _type: self.fail(
            "unchanged material must not be checked out"
        )
        runner.unreal.EditorAssetLibrary = types.SimpleNamespace(
            load_asset=lambda _path: FakeMesh()
        )
        runner.unreal.MaterialEditingLibrary = types.SimpleNamespace(
            recompile_material=lambda _material: self.fail(
                "unchanged master material must not be recompiled"
            )
        )
        runner.audit_unreal_skeletal_mesh_material_sections = (
            lambda *_args: {"status": "ok"}
        )

        result = runner._material_compile_and_slot_validation(
            "/Game/Meshes/SK_Tree"
        )

        self.assertEqual(
            result["nanite_voxel_material_usage"][0]["compile"],
            "skipped_unchanged",
        )


class UnrealIngestSaveTests(unittest.TestCase):
    @staticmethod
    def _configure_ingest_runner(runner, events, mesh_path):
        runner._load_send2ue_unreal = lambda _path: object()
        runner._checkout_existing_assets = lambda _item: {
            "existing": [],
            "checked_out": [],
        }
        runner._import_manifest_asset = (
            lambda _send2ue, _asset: {"asset_path": mesh_path}
        )
        runner._material_pipeline_checkouts = lambda: []
        runner._default_physics_asset_preexisting = lambda _path: False
        runner._prepare_speedtree_skeletal_optimization = (
            lambda _path, _preexisting: {
                "status": "ok",
                "_delete_physics_asset_path": "",
            }
        )
        runner._finalize_speedtree_skeletal_optimization = lambda value: value
        runner._clear_placeholder_skeleton_before_import = lambda _item: {"status": "ok"}
        runner._apply_dynamic_wind = lambda _item: {"status": "ok"}
        runner._save_item_assets = (
            lambda _item, _assets: events.append("save") or [mesh_path]
        )

    def test_save_item_assets_deduplicates_imported_mesh_path(self):
        runner = load_runner()
        save_calls = []
        directory_calls = []

        class FakeEditorAssetLibrary:
            @staticmethod
            def does_asset_exist(_path):
                return True

            @staticmethod
            def save_asset(path, only_if_is_dirty=False):
                save_calls.append((path, only_if_is_dirty))
                return True

            @staticmethod
            def save_directory(path, only_if_is_dirty=True):
                directory_calls.append((path, only_if_is_dirty))
                return True

        runner.unreal.EditorAssetLibrary = FakeEditorAssetLibrary
        mesh_path = "/Game/Meshes/Trees/SK_Test"

        saved = runner._save_item_assets(
            {"mesh_path": mesh_path, "unreal_folder": "/Game/Meshes/Trees/"},
            [{"asset_path": mesh_path}, {"asset_path": mesh_path}],
        )

        self.assertEqual(saved, [mesh_path])
        self.assertEqual(save_calls, [(mesh_path, False)])
        self.assertEqual(directory_calls, [("/Game/Meshes/Trees/", True)])

    def test_ingest_item_saves_once_before_material_validation(self):
        runner = load_runner()
        events = []
        mesh_path = "/Game/Meshes/Trees/SK_Test"
        self._configure_ingest_runner(runner, events, mesh_path)
        runner._material_compile_and_slot_validation = (
            lambda _path: events.append("validate") or {"mesh": mesh_path}
        )

        result = runner.ingest_item(
            {
                "send2ue_unreal_py": "send2ue_unreal.py",
                "assets": [{}],
                "mesh_path": mesh_path,
            }
        )

        self.assertEqual(events, ["save", "validate"])
        self.assertEqual(result["saved"], [mesh_path])

    @staticmethod
    def _skeleton_fixture(
        runner,
        skeleton_name,
        exists=True,
        *,
        default_skeleton_exists=False,
        metadata=None,
        referencers=None,
    ):
        calls = {"deleted": []}

        class FakeSkeleton:
            def __init__(self, name):
                self._name = name

            def get_name(self):
                return self._name

            def get_path_name(self):
                folder = (
                    "/Game/Meshes/_Placeholder"
                    if self._name == "SK_PlaceholderCube_Skeleton"
                    else "/Game/Meshes/Trees"
                )
                return f"{folder}/{self._name}.{self._name}"

        class FakeMesh:
            def __init__(self):
                self.skeleton = (
                    FakeSkeleton(skeleton_name) if skeleton_name else None
                )

            def get_editor_property(self, name):
                assert name == "skeleton"
                return self.skeleton

        mesh = FakeMesh() if exists else None

        class FakeEditorAssetLibrary:
            @staticmethod
            def does_asset_exist(path):
                if str(path).endswith("_Skeleton"):
                    return default_skeleton_exists
                return exists

            @staticmethod
            def load_asset(_path):
                return mesh

            @staticmethod
            def get_metadata_tag(_asset, key):
                return (metadata or {}).get(str(key), "")

            @staticmethod
            def find_package_referencers_for_asset(_path, _load_assets=True):
                return list(referencers or [])

            @staticmethod
            def delete_asset(path):
                calls["deleted"].append(path)
                return True

        runner.unreal.EditorAssetLibrary = FakeEditorAssetLibrary
        return calls

    def test_clear_skeleton_noop_when_slot_is_empty(self):
        runner = load_runner()
        calls = self._skeleton_fixture(runner, None, exists=False)

        result = runner._clear_placeholder_skeleton_before_import(
            {"mesh_path": "/Game/Meshes/Trees/SK_Test.SK_Test"}
        )

        self.assertEqual(result["status"], "fresh")
        self.assertEqual(calls["deleted"], [])

    def test_clear_orphan_canonical_skeleton_uses_fresh_staging_import(self):
        runner = load_runner()
        calls = self._skeleton_fixture(
            runner,
            None,
            exists=False,
            default_skeleton_exists=True,
        )

        result = runner._clear_placeholder_skeleton_before_import(
            {"mesh_path": "/Game/Meshes/Trees/SK_Test"}
        )

        self.assertEqual(result["status"], "fresh_publish_required")
        self.assertTrue(result["requires_fresh_publish"])
        self.assertEqual(calls["deleted"], [])

    def test_clear_skeleton_left_alone_when_dedicated(self):
        runner = load_runner()
        calls = self._skeleton_fixture(runner, "SK_Test_Skeleton")

        result = runner._clear_placeholder_skeleton_before_import(
            {"mesh_path": "/Game/Meshes/Trees/SK_Test.SK_Test"}
        )

        self.assertEqual(result["status"], "ok")
        self.assertEqual(calls["deleted"], [])

    def test_clear_existing_mesh_without_skeleton_requires_fresh_publish(self):
        runner = load_runner()
        calls = self._skeleton_fixture(
            runner,
            None,
            exists=True,
            default_skeleton_exists=True,
        )

        result = runner._clear_placeholder_skeleton_before_import(
            {"mesh_path": "/Game/Meshes/Trees/SK_Test.SK_Test"}
        )

        self.assertEqual(result["status"], "fresh_publish_required")
        self.assertTrue(result["requires_fresh_publish"])
        self.assertEqual(
            result["reason"],
            "existing SkeletalMesh has no Skeleton",
        )
        self.assertEqual(
            result["canonical_assets"],
            [
                "/Game/Meshes/Trees/SK_Test",
                "/Game/Meshes/Trees/SK_Test_Skeleton",
            ],
        )
        self.assertEqual(calls["deleted"], [])

    def test_clear_skeleton_plans_placeholder_mesh_transaction(self):
        runner = load_runner()
        calls = self._skeleton_fixture(runner, "SK_PlaceholderCube_Skeleton")
        collected = []
        runner.unreal.SystemLibrary = types.SimpleNamespace(
            collect_garbage=lambda: collected.append(True)
        )

        result = runner._clear_placeholder_skeleton_before_import(
            {"mesh_path": "/Game/Meshes/Trees/SK_Test.SK_Test"}
        )

        self.assertEqual(result["status"], "fresh_publish_required")
        self.assertTrue(result["requires_fresh_publish"])
        self.assertEqual(calls["deleted"], [])
        self.assertIn("SK_PlaceholderCube_Skeleton", result["shared_skeleton"])
        self.assertEqual(collected, [])

    def test_clear_stale_final_skeleton_plans_owned_pair_transaction(self):
        runner = load_runner()
        calls = self._skeleton_fixture(
            runner,
            "SK_Test_Skeleton",
            default_skeleton_exists=True,
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            wind_json = Path(temp_dir) / "wind.json"
            wind_json.write_text(
                json.dumps(
                    {
                        "SkeletonContract": {
                            "BoneCount": 3,
                            "BoneNameIndexParentSha1": "expected",
                        }
                    }
                ),
                encoding="utf-8",
            )
            result = runner._clear_placeholder_skeleton_before_import(
                {
                    "mesh_path": "/Game/Meshes/Trees/SK_Test",
                    "wind_json": str(wind_json),
                    "wind_policy": {"requires_json": True},
                }
            )

        self.assertEqual(
            result["status"],
            "fresh_publish_required",
        )
        self.assertTrue(result["requires_fresh_publish"])
        self.assertEqual(calls["deleted"], [])

    def test_clear_stale_final_skeleton_preserves_foreign_referencers(self):
        runner = load_runner()
        mesh_path = "/Game/Meshes/Trees/SK_Test"
        skeleton_path = mesh_path + "_Skeleton"
        assembly_path = (
            "/Game/Meshes/Trees/Assembly/SK_Test_NaniteAssembly"
        )
        calls = self._skeleton_fixture(
            runner,
            "SK_Test_Skeleton",
            default_skeleton_exists=True,
            referencers=[mesh_path, assembly_path],
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            wind_json = Path(temp_dir) / "wind.json"
            wind_json.write_text(
                json.dumps(
                    {
                        "SkeletonContract": {
                            "BoneCount": 3,
                            "BoneNameIndexParentSha1": "expected",
                        }
                    }
                ),
                encoding="utf-8",
            )
            result = runner._clear_placeholder_skeleton_before_import(
                {
                    "mesh_path": mesh_path,
                    "wind_json": str(wind_json),
                    "wind_policy": {"requires_json": True},
                }
            )

        self.assertEqual(
            result["status"],
            "fresh_publish_required",
        )
        self.assertEqual(calls["deleted"], [])
        self.assertEqual(
            result["preserved_referenced_skeletons"],
            {skeleton_path: [assembly_path]},
        )

    def test_content_addressed_sk_batch_skeleton_is_owned_for_refresh(self):
        runner = load_runner()
        self.assertTrue(
            runner._is_owned_final_skeleton_path(
                "/Game/Meshes/Trees/SK_Test_Skeleton_0123456789ab",
                "/Game/Meshes/Trees/SK_Test_Skeleton",
            )
        )
        self.assertFalse(
            runner._is_owned_final_skeleton_path(
                "/Game/Meshes/Trees/SK_Test_Skeleton_Custom",
                "/Game/Meshes/Trees/SK_Test_Skeleton",
            )
        )

    def test_referenced_canonical_skeleton_is_relocated_for_fresh_import(self):
        runner = load_runner()
        canonical = "/Game/Meshes/Trees/SK_Test_Skeleton"
        class FakeAsset:
            def __init__(self, path):
                self.path = path

            def get_path_name(self):
                return self.path + "." + self.path.rsplit("/", 1)[-1]

        assets = {canonical: FakeAsset(canonical)}
        renamed = []

        class FakeEditorAssetLibrary:
            @staticmethod
            def does_asset_exist(path):
                return path in assets

            @staticmethod
            def load_asset(path):
                return assets.get(path)

            @staticmethod
            def find_package_referencers_for_asset(
                _path,
                _load_assets=True,
            ):
                return [
                    "/Game/Meshes/Trees/Assembly/"
                    "SK_Test_NaniteAssembly"
                ]

            @staticmethod
            def rename_asset(source, target):
                asset = assets.pop(source, None)
                if asset is None or target in assets:
                    return False
                asset.path = target
                assets[target] = asset
                assets[source] = {"class": "ObjectRedirector"}
                renamed.append((source, target))
                return True

            @staticmethod
            def rename_loaded_asset(source_asset, target):
                source = next(
                    (
                        path
                        for path, value in assets.items()
                        if value is source_asset
                    ),
                    None,
                )
                if source is None:
                    return False
                return FakeEditorAssetLibrary.rename_asset(source, target)

            @staticmethod
            def delete_asset(path):
                return assets.pop(path, None) is not None

        runner.unreal.EditorAssetLibrary = FakeEditorAssetLibrary

        result = runner._vacate_canonical_skeleton_path(
            canonical,
            legacy_token="0123456789abcdef",
        )

        self.assertEqual(result["status"], "relocated")
        self.assertEqual(
            renamed,
            [(canonical, canonical + "_Legacy_0123456789ab")],
        )
        self.assertNotIn(canonical, assets)

    def test_clear_current_final_skeleton_preserves_owned_assets(self):
        runner = load_runner()
        calls = self._skeleton_fixture(
            runner,
            "SK_Test_Skeleton",
            default_skeleton_exists=True,
            metadata={
                runner.FINAL_SKELETON_HASH_METADATA: "expected",
                runner.FINAL_SKELETON_BONE_COUNT_METADATA: "3",
            },
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            wind_json = Path(temp_dir) / "wind.json"
            wind_json.write_text(
                json.dumps(
                    {
                        "SkeletonContract": {
                            "BoneCount": 3,
                            "BoneNameIndexParentSha1": "expected",
                        }
                    }
                ),
                encoding="utf-8",
            )
            result = runner._clear_placeholder_skeleton_before_import(
                {
                    "mesh_path": "/Game/Meshes/Trees/SK_Test",
                    "wind_json": str(wind_json),
                    "wind_policy": {"requires_json": True},
                }
            )

        self.assertEqual(result["status"], "ok")
        self.assertTrue(
            result["final_skeleton_contract"]["asset_metadata_current"]
        )
        self.assertEqual(calls["deleted"], [])

    def test_clear_skeleton_preflight_never_calls_delete(self):
        runner = load_runner()
        self._skeleton_fixture(runner, "SK_PlaceholderCube_Skeleton")
        runner.unreal.EditorAssetLibrary.delete_asset = staticmethod(
            lambda _path: (_ for _ in ()).throw(
                AssertionError("preflight must not delete")
            )
        )

        result = runner._clear_placeholder_skeleton_before_import(
            {"mesh_path": "/Game/Meshes/Trees/SK_Test.SK_Test"}
        )

        self.assertTrue(result["requires_fresh_publish"])

    def test_ingest_item_clears_skeleton_before_import(self):
        runner = load_runner()
        events = []
        mesh_path = "/Game/Meshes/Trees/SK_Test"
        self._configure_ingest_runner(runner, events, mesh_path)
        runner._checkout_existing_assets = (
            lambda _item: events.append("checkout")
            or {"existing": [], "checked_out": []}
        )
        runner._clear_placeholder_skeleton_before_import = (
            lambda _item: events.append("skeleton") or {"status": "ok"}
        )
        runner._import_manifest_asset = (
            lambda _send2ue, _asset: events.append("import")
            or {"asset_path": mesh_path}
        )
        runner._apply_dynamic_wind = (
            lambda _item: events.append("wind") or {"status": "ok"}
        )
        runner._material_compile_and_slot_validation = lambda _path: {}

        result = runner.ingest_item(
            {
                "send2ue_unreal_py": "send2ue_unreal.py",
                "assets": [{}],
                "mesh_path": mesh_path,
            }
        )

        self.assertEqual(events[:2], ["checkout", "skeleton"])
        self.assertLess(events.index("checkout"), events.index("skeleton"))
        self.assertLess(events.index("skeleton"), events.index("import"))
        self.assertEqual(result["skeleton"], {"status": "ok"})

    def test_ingest_item_routes_refresh_plan_to_transactional_publish(self):
        runner = load_runner()
        events = []
        mesh_path = "/Game/Meshes/Trees/SK_Test"
        self._configure_ingest_runner(runner, events, mesh_path)
        runner._clear_placeholder_skeleton_before_import = (
            lambda _item: {
                "status": "fresh_publish_required",
                "requires_fresh_publish": True,
            }
        )
        runner._import_manifest_asset = (
            lambda *_args: (_ for _ in ()).throw(
                AssertionError("refresh must not import in place")
            )
        )
        runner._import_manifest_asset_with_fresh_skeleton = (
            lambda _send2ue, _asset, _item: events.append(
                "transactional_publish"
            )
            or {"asset_path": mesh_path}
        )
        runner._material_compile_and_slot_validation = lambda _path: {}

        runner.ingest_item(
            {
                "send2ue_unreal_py": "send2ue_unreal.py",
                "assets": [
                    {
                        "asset_data": {
                            "asset_path": mesh_path,
                        }
                    }
                ],
                "mesh_path": mesh_path,
            }
        )

        self.assertIn("transactional_publish", events)

    def test_ingest_item_refreshes_each_broken_skeletal_manifest_asset(self):
        runner = load_runner()
        events = []
        primary = "/Game/Meshes/Trees/SK_Cluster_01"
        companion = "/Game/Meshes/Trees/SK_Cluster_02"
        self._configure_ingest_runner(runner, events, primary)

        def skeleton_plan(item):
            path = item["mesh_path"]
            events.append(("preflight", path))
            return {
                "status": "fresh_publish_required",
                "reason": "existing SkeletalMesh has no Skeleton",
                "requires_fresh_publish": True,
            }

        runner._clear_placeholder_skeleton_before_import = skeleton_plan
        runner._import_manifest_asset = (
            lambda *_args: (_ for _ in ()).throw(
                AssertionError("broken assets must not import in place")
            )
        )
        runner._import_manifest_asset_with_fresh_skeleton = (
            lambda _send2ue, asset, refresh_item: events.append(
                (
                    "transactional_publish",
                    asset["asset_data"]["asset_path"],
                    refresh_item["mesh_path"],
                )
            )
            or {"asset_path": asset["asset_data"]["asset_path"]}
        )
        runner._material_compile_and_slot_validation = lambda _path: {}

        result = runner.ingest_item(
            {
                "send2ue_unreal_py": "send2ue_unreal.py",
                "assets": [
                    {
                        "asset_data": {
                            "_asset_type": "SkeletalMesh",
                            "asset_path": primary,
                        }
                    },
                    {
                        "asset_data": {
                            "_asset_type": "SkeletalMesh",
                            "asset_path": companion,
                        }
                    },
                ],
                "mesh_path": primary,
            }
        )

        self.assertIn(("preflight", primary), events)
        self.assertIn(("preflight", companion), events)
        self.assertIn(
            ("transactional_publish", primary, primary),
            events,
        )
        self.assertIn(
            ("transactional_publish", companion, companion),
            events,
        )
        self.assertEqual(
            set(result["skeleton_refresh_plans"]),
            {primary, companion},
        )

    def test_speedtree_import_disables_physics_asset_generation_temporarily(self):
        runner = load_runner()

        class Options:
            create_physics_asset = True
            physics_asset = object()

        class Importer:
            def __init__(self):
                self._options = Options()

            def set_physics_asset(self):
                self._options.create_physics_asset = True

        module = types.SimpleNamespace(UnrealImportAsset=Importer)
        original = Importer.set_physics_asset
        with runner._without_generated_physics_assets(module) as disabled:
            instance = Importer()
            instance.set_physics_asset()
            self.assertTrue(disabled)
            self.assertFalse(instance._options.create_physics_asset)
            self.assertIsNone(instance._options.physics_asset)

        self.assertIs(Importer.set_physics_asset, original)

    def test_primary_speedtree_import_clears_retained_skeleton_binding(self):
        runner = load_runner()

        class Options:
            skeleton = object()

            def set_editor_property(self, name, value):
                setattr(self, name, value)

        class Importer:
            def __init__(self):
                self._options = Options()

            def set_skeleton(self):
                self._options.skeleton = object()

        module = types.SimpleNamespace(UnrealImportAsset=Importer)
        original = Importer.set_skeleton
        with runner._without_existing_skeleton_binding(module) as disabled:
            instance = Importer()
            instance.set_skeleton()
            self.assertTrue(disabled)
            self.assertIsNone(instance._options.skeleton)

        self.assertIs(Importer.set_skeleton, original)

    def test_final_speedtree_import_binds_incoming_skeleton_explicitly(self):
        runner = load_runner()
        skeleton_path = "/Game/Meshes/Trees/SK_Test_Skeleton"
        incoming_skeleton = object()

        class Options:
            skeleton = None

            def set_editor_property(self, name, value):
                setattr(self, name, value)

        class Importer:
            def __init__(self):
                self._options = Options()

            def set_skeleton(self):
                self._options.skeleton = object()

        module = types.SimpleNamespace(UnrealImportAsset=Importer)
        original = Importer.set_skeleton
        runner.unreal.EditorAssetLibrary = types.SimpleNamespace(
            load_asset=lambda path: (
                incoming_skeleton if path == skeleton_path else None
            )
        )

        with runner._with_explicit_skeleton_binding(
            module,
            skeleton_path,
        ):
            instance = Importer()
            instance.set_skeleton()
            self.assertIs(
                instance._options.skeleton,
                incoming_skeleton,
            )

        self.assertIs(Importer.set_skeleton, original)

    def _in_place_publish_fixture(
        self,
        *,
        existing_canonical_skeleton=True,
        mesh_referencers=None,
        fail_skeleton_copy=False,
    ):
        runner = load_runner()
        final_mesh_path = "/Game/Meshes/Trees/SK_Test"
        final_skeleton_path = final_mesh_path + "_Skeleton"
        assets = {}
        calls = {
            "imported_paths": [],
            "duplicated": [],
            "renamed": [],
            "events": [],
            "explicit_bindings": [],
            "manifest_pre_commands": [],
            "skeleton_bindings": [],
        }

        class FakeAsset:
            def __init__(self, path):
                self.path = path

            def get_name(self):
                return self.path.rsplit("/", 1)[-1]

            def get_path_name(self):
                return self.path + "." + self.get_name()

        class FakeSkeleton(FakeAsset):
            pass

        class FakeMesh(FakeAsset):
            def __init__(self, path, skeleton, referencers=None):
                super().__init__(path)
                self.skeleton = skeleton
                self.external_referencers = list(referencers or [])

            def get_editor_property(self, name):
                if name != "skeleton":
                    raise AssertionError(name)
                return self.skeleton

            def set_editor_property(self, name, value):
                if name != "skeleton":
                    raise AssertionError(name)
                self.skeleton = value

            def set_skeleton(self, value):
                self.skeleton = value

        if existing_canonical_skeleton:
            old_skeleton = FakeSkeleton(final_skeleton_path)
            assets[final_skeleton_path] = old_skeleton
        else:
            old_skeleton = FakeSkeleton(
                "/Game/Shared/SK_Placeholder_Skeleton"
            )
            assets[old_skeleton.path] = old_skeleton
        old_mesh = FakeMesh(
            final_mesh_path,
            old_skeleton,
            mesh_referencers,
        )
        assets[final_mesh_path] = old_mesh

        class FakeEditorAssetLibrary:
            @staticmethod
            def does_asset_exist(path):
                return path in assets

            @staticmethod
            def load_asset(path):
                return assets.get(path)

            @staticmethod
            def duplicate_loaded_asset(source_asset, target):
                calls["duplicated"].append((source_asset.path, target))
                if fail_skeleton_copy or target in assets:
                    return None
                duplicate = FakeSkeleton(target)
                assets[target] = duplicate
                return duplicate

            @staticmethod
            def rename_asset(source, target):
                calls["renamed"].append(("path", source, target))
                raise AssertionError(
                    "canonical publish must not rename live assets"
                )

            @staticmethod
            def rename_loaded_asset(source_asset, target):
                calls["renamed"].append(
                    ("loaded", source_asset.path, target)
                )
                raise AssertionError(
                    "canonical publish must not rename live assets"
                )

            @staticmethod
            def find_package_referencers_for_asset(
                path,
                _load_assets=True,
            ):
                asset = assets.get(path)
                return list(
                    getattr(asset, "external_referencers", [])
                )

            @staticmethod
            def does_directory_exist(path):
                prefix = path.rstrip("/") + "/"
                return any(key.startswith(prefix) for key in assets)

            @staticmethod
            def delete_directory(path):
                prefix = path.rstrip("/") + "/"
                for key in list(assets):
                    if key.startswith(prefix):
                        assets.pop(key)
                return True

        runner.unreal.EditorAssetLibrary = FakeEditorAssetLibrary

        class FakeCodexMaterialToolsLibrary:
            @staticmethod
            def bind_skeletal_mesh_skeleton(
                mesh,
                skeleton,
                require_exact_reference_skeleton,
            ):
                calls["skeleton_bindings"].append(
                    (
                        mesh.path,
                        skeleton.path,
                        require_exact_reference_skeleton,
                    )
                )
                mesh.set_skeleton(skeleton)
                return (
                    True,
                    json.dumps({"success": True, "bound": True}),
                    [],
                )

        runner.unreal.CodexMaterialToolsLibrary = (
            FakeCodexMaterialToolsLibrary
        )

        class Options:
            def __init__(self):
                self.skeleton = None

            def set_editor_property(self, name, value):
                if name != "skeleton":
                    raise AssertionError(name)
                self.skeleton = value

        class Importer:
            def __init__(self):
                self._options = Options()

            def set_skeleton(self):
                self._options.skeleton = None

        send2ue = types.SimpleNamespace(UnrealImportAsset=Importer)

        def fake_import(_send2ue, manifest_asset):
            path = manifest_asset["asset_data"]["asset_path"]
            calls["imported_paths"].append(path)
            calls["manifest_pre_commands"].append(
                list(manifest_asset.get("pre_import_commands") or [])
            )
            if "__SKBatchStaging_" in path:
                skeleton = FakeSkeleton(path + "_Skeleton")
                assets[path] = FakeMesh(path, skeleton)
                assets[skeleton.path] = skeleton
            else:
                importer = _send2ue.UnrealImportAsset()
                importer.set_skeleton()
                skeleton = importer._options.skeleton
                calls["explicit_bindings"].append(
                    runner._asset_package_path(skeleton)
                )
                assets[path].set_skeleton(skeleton)
            return {
                "asset_path": path,
                "file_path": manifest_asset["asset_data"]["file_path"],
                "imported": [path],
            }

        runner._import_manifest_asset = fake_import
        runner._execute_command_groups = (
            lambda commands, label: calls["events"].append(
                (label, commands)
            )
        )
        runner._run_lod_and_socket_operations = (
            lambda _send2ue, _manifest, data, _properties:
            calls["events"].append(("operations", data["asset_path"]))
        )
        manifest_asset = {
            "asset_data": {
                "_asset_type": "SkeletalMesh",
                "asset_path": final_mesh_path,
                "asset_folder": "/Game/Meshes/Trees/",
                "file_path": "C:/exports/SK_Test.fbx",
            },
            "property_data": {},
            "pre_import_commands": [["pre"]],
            "post_import_commands": [["post"]],
            "operations": {"reset_and_import_lods": True},
        }
        return (
            runner,
            send2ue,
            manifest_asset,
            assets,
            calls,
            old_mesh,
            old_skeleton,
        )

    def test_fresh_skeleton_import_reimports_canonical_in_place(self):
        (
            runner,
            send2ue,
            manifest_asset,
            assets,
            calls,
            old_mesh,
            old_skeleton,
        ) = self._in_place_publish_fixture(
            mesh_referencers=[
                "/Game/Blueprints/BP_Test",
                "/Game/Maps/Test",
            ],
        )

        result = runner._import_manifest_asset_with_fresh_skeleton(
            send2ue,
            manifest_asset,
            {},
        )

        final_mesh_path = "/Game/Meshes/Trees/SK_Test"
        final_skeleton_path = final_mesh_path + "_Skeleton"
        published_skeleton_path = result["staged_import"][
            "final_skeleton"
        ]
        self.assertEqual(len(calls["imported_paths"]), 2)
        self.assertIn(
            "/__SKBatchStaging_",
            calls["imported_paths"][0],
        )
        self.assertEqual(calls["imported_paths"][1], final_mesh_path)
        self.assertEqual(
            calls["manifest_pre_commands"],
            [[], [["pre"]]],
        )
        self.assertIs(assets[final_mesh_path], old_mesh)
        self.assertIs(
            old_mesh.get_editor_property("skeleton"),
            assets[published_skeleton_path],
        )
        self.assertIs(assets[final_skeleton_path], old_skeleton)
        self.assertEqual(calls["renamed"], [])
        self.assertEqual(
            calls["explicit_bindings"],
            [published_skeleton_path],
        )
        self.assertEqual(
            calls["skeleton_bindings"],
            [
                (final_mesh_path, published_skeleton_path, False),
                (final_mesh_path, published_skeleton_path, True),
            ],
        )
        self.assertEqual(
            result["staged_import"]["publish_mode"],
            "in_place_explicit_skeleton",
        )
        self.assertEqual(
            result["staged_import"]["canonical_mesh_referencers"],
            [
                "/Game/Blueprints/BP_Test",
                "/Game/Maps/Test",
            ],
        )
        self.assertEqual(
            result["staged_import"]["relocated_previous_assets"],
            [],
        )
        self.assertFalse(
            any("__SKBatchStaging_" in path for path in assets)
        )
        self.assertEqual(
            calls["events"],
            [
                ("post_import", [["post"]]),
                ("operations", final_mesh_path),
            ],
        )

    def test_fresh_skeleton_import_uses_canonical_skeleton_when_free(self):
        (
            runner,
            send2ue,
            manifest_asset,
            assets,
            calls,
            old_mesh,
            _old_skeleton,
        ) = self._in_place_publish_fixture(
            existing_canonical_skeleton=False,
        )

        result = runner._import_manifest_asset_with_fresh_skeleton(
            send2ue,
            manifest_asset,
            {},
        )

        final_skeleton_path = (
            "/Game/Meshes/Trees/SK_Test_Skeleton"
        )
        self.assertEqual(
            result["staged_import"]["skeleton_publish_mode"],
            "canonical_copy",
        )
        self.assertEqual(
            result["staged_import"]["final_skeleton"],
            final_skeleton_path,
        )
        self.assertIs(
            old_mesh.get_editor_property("skeleton"),
            assets[final_skeleton_path],
        )
        self.assertEqual(calls["renamed"], [])

    def test_fresh_skeleton_import_preserves_occupied_skeleton_path(self):
        (
            runner,
            send2ue,
            manifest_asset,
            assets,
            calls,
            old_mesh,
            old_skeleton,
        ) = self._in_place_publish_fixture()

        result = runner._import_manifest_asset_with_fresh_skeleton(
            send2ue,
            manifest_asset,
            {},
        )

        canonical_skeleton_path = (
            "/Game/Meshes/Trees/SK_Test_Skeleton"
        )
        published_skeleton_path = result["staged_import"][
            "final_skeleton"
        ]
        self.assertEqual(
            result["staged_import"]["skeleton_publish_mode"],
            "content_addressed_copy",
        )
        self.assertTrue(
            published_skeleton_path.startswith(
                canonical_skeleton_path + "_"
            )
        )
        self.assertIs(
            assets[canonical_skeleton_path],
            old_skeleton,
        )
        self.assertIs(
            old_mesh.get_editor_property("skeleton"),
            assets[published_skeleton_path],
        )
        self.assertFalse(
            any("_Legacy_" in path for path in assets)
        )
        self.assertEqual(calls["renamed"], [])

    def test_skeleton_copy_failure_does_not_move_canonical_assets(self):
        (
            runner,
            send2ue,
            manifest_asset,
            assets,
            calls,
            old_mesh,
            old_skeleton,
        ) = self._in_place_publish_fixture(
            fail_skeleton_copy=True,
        )

        with self.assertRaisesRegex(
            RuntimeError,
            "could not be copied",
        ):
            runner._import_manifest_asset_with_fresh_skeleton(
                send2ue,
                manifest_asset,
                {},
            )

        self.assertIs(
            assets["/Game/Meshes/Trees/SK_Test"],
            old_mesh,
        )
        self.assertIs(
            assets["/Game/Meshes/Trees/SK_Test_Skeleton"],
            old_skeleton,
        )
        self.assertIs(
            old_mesh.get_editor_property("skeleton"),
            old_skeleton,
        )
        self.assertEqual(calls["renamed"], [])

    def test_speedtree_skeletal_optimization_enforces_render_and_collision_settings(self):
        runner = load_runner()
        mesh_path = "/Game/Meshes/Trees/SK_Test"
        physics_asset_path = mesh_path + "_PhysicsAsset"
        deleted = []

        class FakeAsset:
            def __init__(self, path):
                self.path = path

            def get_path_name(self):
                return self.path + "." + self.path.rsplit("/", 1)[-1]

        class FakeSettings:
            def __init__(self):
                self.values = {"enabled": False, "shape_preservation": "KEEP"}

            def get_editor_property(self, name):
                return self.values[name]

            def set_editor_property(self, name, value):
                self.values[name] = value

        class FakeMesh:
            def __init__(self):
                self.values = {
                    "support_ray_tracing": True,
                    "enable_per_poly_collision": True,
                    "physics_asset": FakeAsset(physics_asset_path),
                    "nanite_settings": FakeSettings(),
                }
                self.nanite_notifications = 0

            def modify(self):
                return True

            def get_editor_property(self, name):
                return self.values[name]

            def set_editor_property(self, name, value):
                self.values[name] = value

            def notify_nanite_settings_changed(self):
                self.nanite_notifications += 1

        mesh = FakeMesh()

        class FakeEditorAssetLibrary:
            @staticmethod
            def load_asset(path):
                return mesh if path == mesh_path else None

            @staticmethod
            def does_asset_exist(path):
                return path == physics_asset_path

            @staticmethod
            def find_package_referencers_for_asset(_path, _confirm=True):
                return [mesh_path]

            @staticmethod
            def delete_asset(path):
                deleted.append(path)
                return True

        runner.unreal.SkeletalMesh = FakeMesh
        runner.unreal.EditorAssetLibrary = FakeEditorAssetLibrary
        runner.unreal.NaniteShapePreservation = types.SimpleNamespace(
            VOXELIZE="VOXELIZE"
        )

        optimization = runner._prepare_speedtree_skeletal_optimization(
            mesh_path,
            default_physics_asset_preexisting=True,
        )

        self.assertFalse(mesh.values["support_ray_tracing"])
        self.assertFalse(mesh.values["enable_per_poly_collision"])
        self.assertIsNone(mesh.values["physics_asset"])
        self.assertTrue(mesh.values["nanite_settings"].values["enabled"])
        self.assertEqual(
            mesh.values["nanite_settings"].values["shape_preservation"],
            "VOXELIZE",
        )
        self.assertEqual(mesh.nanite_notifications, 1)

        finalized = runner._finalize_speedtree_skeletal_optimization(
            optimization
        )
        self.assertEqual(deleted, [physics_asset_path])
        self.assertTrue(finalized["physics_asset_deleted"])

        mesh.values["physics_asset"] = FakeAsset(physics_asset_path)
        runner._physics_asset_referencers = lambda _path: [
            mesh_path,
            "/Game/Meshes/Other/SK_Shared",
        ]
        shared = runner._prepare_speedtree_skeletal_optimization(
            mesh_path,
            default_physics_asset_preexisting=True,
        )
        shared = runner._finalize_speedtree_skeletal_optimization(shared)
        self.assertEqual(deleted, [physics_asset_path])
        self.assertFalse(shared["physics_asset_deleted"])
        self.assertEqual(
            shared["default_physics_asset_foreign_referencers"],
            ["/Game/Meshes/Other/SK_Shared"],
        )

    def test_ingest_item_still_saves_before_material_validation_failure(self):
        runner = load_runner()
        events = []
        mesh_path = "/Game/Meshes/Trees/SK_Test"
        self._configure_ingest_runner(runner, events, mesh_path)

        def fail_validation(_path):
            events.append("validate")
            raise RuntimeError("material compile failed")

        runner._material_compile_and_slot_validation = fail_validation

        try:
            runner.ingest_item(
                {
                    "send2ue_unreal_py": "send2ue_unreal.py",
                    "assets": [{}],
                    "mesh_path": mesh_path,
                }
            )
        except RuntimeError as exc:
            self.assertIn("material compile failed", str(exc))
        else:
            self.fail("material validation failure must propagate")

        self.assertEqual(events, ["save", "validate"])


class PreImportMaterialSlotNormalizationTests(unittest.TestCase):
    def test_missing_imported_slot_names_are_normalized_before_reimport(self):
        runner = load_runner()
        calls = []

        class FakeMaterial:
            def get_path_name(self):
                return "/Game/MI/MI_Bark.MI_Bark"

        class FakeSlot:
            def get_editor_property(self, name):
                return {
                    "imported_material_slot_name": "None",
                    "material_slot_name": "M_Bark",
                    "material_interface": FakeMaterial(),
                }[name]

        class FakeMesh:
            def get_editor_property(self, name):
                self.assert_materials = name
                return [FakeSlot()]

        mesh = FakeMesh()
        runner.unreal.SkeletalMesh = FakeMesh
        runner.unreal.Name = lambda value: value
        runner.unreal.EditorAssetLibrary = types.SimpleNamespace(
            does_asset_exist=lambda _path: True,
            load_asset=lambda _path: mesh
        )

        def normalize(*args):
            calls.append(args)
            return json.dumps({"desired_is_set": True, "changed": True})

        runner.unreal.CodexMaterialToolsLibrary = types.SimpleNamespace(
            normalize_skeletal_mesh_material_slot=normalize
        )

        result = runner._normalize_existing_skeletal_mesh_imported_slot_names(
            "/Game/Trees/SK_Branch"
        )

        self.assertEqual(result["status"], "normalized")
        self.assertEqual(
            calls,
            [
                (
                    "/Game/Trees/SK_Branch",
                    0,
                    "None",
                    "M_Bark",
                    "/Game/MI/MI_Bark.MI_Bark",
                    True,
                )
            ],
        )

    def test_missing_asset_is_a_fresh_import(self):
        runner = load_runner()
        loaded = []
        runner.unreal.EditorAssetLibrary = types.SimpleNamespace(
            does_asset_exist=lambda _path: False,
            load_asset=lambda path: loaded.append(path),
        )
        result = runner._normalize_existing_skeletal_mesh_imported_slot_names(
            "/Game/Trees/SK_New"
        )
        self.assertEqual(result["status"], "fresh")
        self.assertEqual(loaded, [])

    def test_vacating_referenced_skeleton_clears_source_redirector(self):
        runner = load_runner()
        canonical = "/Game/Meshes/Trees/SK_Test_Skeleton"
        class FakeAsset:
            def __init__(self, path):
                self.path = path

            def get_path_name(self):
                return self.path + "." + self.path.rsplit("/", 1)[-1]

        assets = {canonical: FakeAsset(canonical)}

        class FakeEditorAssetLibrary:
            @staticmethod
            def does_asset_exist(path):
                return path in assets

            @staticmethod
            def load_asset(path):
                return assets.get(path)

            @staticmethod
            def find_package_referencers_for_asset(path, _load_assets=True):
                return (
                    ["/Game/Meshes/Trees/SK_Test"]
                    if path == canonical else []
                )

            @staticmethod
            def rename_asset(source, target):
                asset = assets.pop(source, None)
                if asset is None or target in assets:
                    return False
                asset.path = target
                assets[target] = asset
                assets[source] = {"class": "ObjectRedirector"}
                return True

            @staticmethod
            def rename_loaded_asset(source_asset, target):
                source = next(
                    (
                        path
                        for path, value in assets.items()
                        if value is source_asset
                    ),
                    None,
                )
                if source is None:
                    return False
                return FakeEditorAssetLibrary.rename_asset(source, target)

            @staticmethod
            def delete_asset(path):
                return assets.pop(path, None) is not None

        runner.unreal.EditorAssetLibrary = FakeEditorAssetLibrary

        result = runner._vacate_canonical_skeleton_path(
            canonical,
            legacy_token="abc123",
        )

        self.assertEqual(result["status"], "relocated")
        self.assertTrue(result["redirector_cleared"])
        self.assertNotIn(canonical, assets)
        self.assertIn(canonical + "_Legacy_abc123", assets)

    def test_unreferenced_canonical_redirector_is_removed_for_publish(self):
        runner = load_runner()
        canonical = "/Game/Meshes/Trees/SK_Test_Skeleton"
        assets = {canonical: {"class": "ObjectRedirector"}}

        class FakeEditorAssetLibrary:
            @staticmethod
            def does_asset_exist(path):
                return path in assets

            @staticmethod
            def load_asset(path):
                return assets.get(path)

            @staticmethod
            def find_package_referencers_for_asset(path, _load_assets=True):
                return []

            @staticmethod
            def delete_asset(path):
                return assets.pop(path, None) is not None

        runner.unreal.EditorAssetLibrary = FakeEditorAssetLibrary
        runner._is_headless_manifest_runtime = lambda: True

        result = runner._clear_unreferenced_canonical_redirector(canonical)

        self.assertTrue(result["deleted"])
        self.assertEqual(result["referencers"], [])
        self.assertNotIn(canonical, assets)

    def test_redirector_delete_is_collected_before_registry_recheck(self):
        runner = load_runner()
        canonical = "/Game/Meshes/Trees/SK_Test_Skeleton"
        assets = {canonical: {"class": "ObjectRedirector"}}
        pending_delete = set()

        class FakeEditorAssetLibrary:
            @staticmethod
            def does_asset_exist(path):
                return path in assets

            @staticmethod
            def load_asset(path):
                return assets.get(path)

            @staticmethod
            def find_package_referencers_for_asset(path, _load_assets=True):
                return []

            @staticmethod
            def delete_asset(path):
                pending_delete.add(path)
                return True

        runner.unreal.EditorAssetLibrary = FakeEditorAssetLibrary
        runner.unreal.collect_garbage = lambda: [
            assets.pop(path, None)
            for path in tuple(pending_delete)
        ]
        runner._is_headless_manifest_runtime = lambda: True

        result = runner._clear_unreferenced_canonical_redirector(canonical)

        self.assertTrue(result["deleted"])
        self.assertNotIn(canonical, assets)

    def test_persistent_redirector_package_is_quarantined_and_rescanned(self):
        runner = load_runner()
        canonical = "/Game/Meshes/Trees/SK_Test_Skeleton"
        assets = {canonical: {"class": "ObjectRedirector"}}
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            content = project / "Content"
            saved = project / "Saved"
            package = (
                content
                / "Meshes"
                / "Trees"
                / "SK_Test_Skeleton.uasset"
            )
            package.parent.mkdir(parents=True)
            package.write_bytes(b"redirector")

            class FakeEditorAssetLibrary:
                @staticmethod
                def does_asset_exist(path):
                    return path in assets

                @staticmethod
                def load_asset(path):
                    return assets.get(path)

                @staticmethod
                def find_package_referencers_for_asset(
                    path,
                    _load_assets=True,
                ):
                    return []

                @staticmethod
                def delete_asset(path):
                    return path in assets

            class FakeRegistry:
                @staticmethod
                def scan_modified_asset_files(_paths):
                    if not package.exists():
                        assets.pop(canonical, None)

                @staticmethod
                def wait_for_completion():
                    return None

            runner.unreal.EditorAssetLibrary = FakeEditorAssetLibrary
            runner.unreal.Paths = types.SimpleNamespace(
                project_content_dir=lambda: str(content),
                project_saved_dir=lambda: str(saved),
            )
            runner.unreal.AssetRegistryHelpers = types.SimpleNamespace(
                get_asset_registry=lambda: FakeRegistry(),
            )
            runner.unreal.collect_garbage = lambda: None
            runner._is_headless_manifest_runtime = lambda: True

            result = runner._clear_unreferenced_canonical_redirector(
                canonical
            )

            backup = Path(result["quarantine"]["backup_file"])
            self.assertTrue(result["deleted"])
            self.assertFalse(package.exists())
            self.assertTrue(backup.is_file())
            self.assertEqual(backup.read_bytes(), b"redirector")
            self.assertNotIn(canonical, assets)

    def test_referenced_canonical_redirector_remains_blocked(self):
        runner = load_runner()
        canonical = "/Game/Meshes/Trees/SK_Test_Skeleton"
        assets = {canonical: {"class": "ObjectRedirector"}}

        class FakeEditorAssetLibrary:
            @staticmethod
            def does_asset_exist(path):
                return path in assets

            @staticmethod
            def load_asset(path):
                return assets.get(path)

            @staticmethod
            def find_package_referencers_for_asset(path, _load_assets=True):
                return ["/Game/Meshes/Trees/SK_Other"]

        runner.unreal.EditorAssetLibrary = FakeEditorAssetLibrary
        runner._is_headless_manifest_runtime = lambda: True

        with self.assertRaisesRegex(
            RuntimeError,
            "referenced redirector.*SK_Other",
        ):
            runner._clear_unreferenced_canonical_redirector(canonical)

        self.assertIn(canonical, assets)

    def test_canonical_redirector_cleanup_requires_headless_and_referencer_api(
        self,
    ):
        runner = load_runner()
        canonical = "/Game/Meshes/Trees/SK_Test_Skeleton"
        assets = {canonical: {"class": "ObjectRedirector"}}

        runner.unreal.EditorAssetLibrary = types.SimpleNamespace(
            does_asset_exist=lambda path: path in assets,
            load_asset=lambda path: assets.get(path),
        )
        runner._is_headless_manifest_runtime = lambda: False
        with self.assertRaisesRegex(
            RuntimeError,
            "only in a fresh headless",
        ):
            runner._clear_unreferenced_canonical_redirector(canonical)

        runner._is_headless_manifest_runtime = lambda: True
        with self.assertRaisesRegex(
            RuntimeError,
            "referencer API is unavailable",
        ):
            runner._clear_unreferenced_canonical_redirector(canonical)

        self.assertIn(canonical, assets)


class UnrealIngestCrashBudgetTests(unittest.TestCase):
    """A crash budget must gate reproducible crashes, not fresh work."""

    def test_crash_count_carries_while_the_queued_work_is_unchanged(self):
        runner = load_runner()

        self.assertEqual(
            runner._inherited_crash_count(
                {"fingerprint": "abc", "crash_count": 2}, "abc"
            ),
            2,
        )

    def test_rebuilt_inputs_start_with_a_clean_crash_budget(self):
        runner = load_runner()

        self.assertEqual(
            runner._inherited_crash_count(
                {"fingerprint": "old", "crash_count": 2}, "new"
            ),
            0,
        )

    def test_missing_previous_state_starts_at_zero(self):
        runner = load_runner()

        self.assertEqual(runner._inherited_crash_count({}, "new"), 0)
