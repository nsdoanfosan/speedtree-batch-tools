import importlib.util
import json
import sys
import types
from pathlib import Path


RUNNER = Path(__file__).resolve().parents[1] / "unreal_ingest.py"


def load_runner(monkeypatch):
    monkeypatch.setitem(sys.modules, "unreal", types.ModuleType("unreal"))
    spec = importlib.util.spec_from_file_location("test_sk_batch_unreal_ingest", RUNNER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
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
