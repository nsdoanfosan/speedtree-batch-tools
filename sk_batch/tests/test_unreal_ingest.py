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


class DynamicWindFinalSkeletonContractTests(unittest.TestCase):
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

    def test_success_without_skeleton_hash_stops_ingest(self):
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
            with self.assertRaisesRegex(RuntimeError, "no skeleton hash"):
                runner._apply_dynamic_wind(
                    {
                        "wind_json": str(wind_json),
                        "mesh_path": "/Game/Test/SK_Tree",
                    }
                )

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
    def _skeleton_fixture(runner, skeleton_name, exists=True):
        calls = {"deleted": []}

        class FakeSkeleton:
            def __init__(self, name):
                self._name = name

            def get_name(self):
                return self._name

            def get_path_name(self):
                return f"/Game/Meshes/_Placeholder/{self._name}.{self._name}"

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
            def does_asset_exist(_path):
                return exists

            @staticmethod
            def load_asset(_path):
                return mesh

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

    def test_clear_skeleton_left_alone_when_dedicated(self):
        runner = load_runner()
        calls = self._skeleton_fixture(runner, "SK_Test_Skeleton")

        result = runner._clear_placeholder_skeleton_before_import(
            {"mesh_path": "/Game/Meshes/Trees/SK_Test.SK_Test"}
        )

        self.assertEqual(result["status"], "ok")
        self.assertEqual(calls["deleted"], [])

    def test_clear_skeleton_deletes_placeholder_mesh(self):
        runner = load_runner()
        calls = self._skeleton_fixture(runner, "SK_PlaceholderCube_Skeleton")

        result = runner._clear_placeholder_skeleton_before_import(
            {"mesh_path": "/Game/Meshes/Trees/SK_Test.SK_Test"}
        )

        self.assertEqual(result["status"], "cleared_placeholder")
        self.assertEqual(calls["deleted"], ["/Game/Meshes/Trees/SK_Test"])
        self.assertIn("SK_PlaceholderCube_Skeleton", result["shared_skeleton"])

    def test_clear_skeleton_raises_when_delete_fails(self):
        runner = load_runner()
        self._skeleton_fixture(runner, "SK_PlaceholderCube_Skeleton")
        runner.unreal.EditorAssetLibrary.delete_asset = staticmethod(
            lambda _path: False
        )

        with self.assertRaises(RuntimeError):
            runner._clear_placeholder_skeleton_before_import(
                {"mesh_path": "/Game/Meshes/Trees/SK_Test.SK_Test"}
            )

    def test_ingest_item_clears_skeleton_before_import(self):
        runner = load_runner()
        events = []
        mesh_path = "/Game/Meshes/Trees/SK_Test"
        self._configure_ingest_runner(runner, events, mesh_path)
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

        self.assertEqual(events[0], "skeleton")
        self.assertLess(events.index("skeleton"), events.index("import"))
        self.assertEqual(result["skeleton"], {"status": "ok"})

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
