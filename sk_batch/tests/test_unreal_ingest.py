import importlib.util
import json
import sys
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
