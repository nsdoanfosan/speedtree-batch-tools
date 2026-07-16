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
