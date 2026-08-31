import json
import sys
from pathlib import Path

SK_BATCH_DIR = Path(__file__).resolve().parents[1]
if str(SK_BATCH_DIR) not in sys.path:
    sys.path.insert(0, str(SK_BATCH_DIR))

import sk_common
from unreal_ingest_policy import (
    ASSEMBLY_INGEST_WAVE,
    PROVIDER_INGEST_WAVE,
    bounded_heavy_process_item_limit,
    heavy_ingest_reasons,
    manifest_item_policy_metadata,
    migrate_saved_push_transport,
    resolve_heavy_push_transport,
)


def skeletal_item(**overrides):
    item = {
        "assets": [{"asset_data": {
            "_asset_type": "SkeletalMesh",
            "asset_path": "/Game/Trees/SK_Test",
        }}],
        "wind_policy": {"requires_json": True},
        "cluster_assembly": None,
    }
    item.update(overrides)
    return item


def test_generated_skeletal_and_dynamic_wind_require_isolation():
    assert heavy_ingest_reasons(skeletal_item()) == [
        "generated_nanite_skeletal_mesh",
        "dynamic_wind_provider",
    ]


def test_final_assembly_is_in_second_wave_with_exact_publish_target():
    item = skeletal_item(cluster_assembly={
        "ingest_plan": {
            "status": "ready",
            "asset_contract": {"assembly": "/Game/Trees/SK_Test_Assembly"},
        }
    })

    metadata = manifest_item_policy_metadata(item)

    assert metadata["ingest_wave"] == ASSEMBLY_INGEST_WAVE
    assert metadata["final_assembly_asset_path"] == "/Game/Trees/SK_Test_Assembly"
    assert "final_nanite_assembly" in metadata["heavy_ingest_reasons"]
    assert manifest_item_policy_metadata(skeletal_item())["ingest_wave"] == (
        PROVIDER_INGEST_WAVE
    )


def test_rpc_resolves_to_wait_with_editor_and_headless_without_editor():
    assert resolve_heavy_push_transport(
        "rpc", unreal_running=True
    )["transport"] == "unreal_wait"
    assert resolve_heavy_push_transport(
        "rpc", unreal_running=False
    )["transport"] == "headless"
    assert resolve_heavy_push_transport(
        "headless", unreal_running=False
    )["changed"] is False


def test_heavy_process_limit_cannot_be_disabled_or_raised_above_six():
    assert bounded_heavy_process_item_limit(0) == 6
    assert bounded_heavy_process_item_limit(100) == 6
    assert bounded_heavy_process_item_limit(2) == 2


def test_saved_rpc_is_migrated_even_at_the_current_policy_version():
    config = {"push_transport": "rpc"}

    migrated, receipt = migrate_saved_push_transport(config, config)
    current, current_receipt = migrate_saved_push_transport(
        config,
        {"push_transport": "rpc", "push_transport_policy_version": 1},
    )

    assert migrated["push_transport"] == "unreal_wait"
    assert receipt["changed"] is True
    assert current["push_transport"] == "unreal_wait"
    assert current_receipt["changed"] is True


def test_load_config_cannot_restore_legacy_rpc_override(tmp_path, monkeypatch):
    config_path = tmp_path / "sk_batch_config.json"
    config_path.write_text(
        json.dumps({"push_transport": "rpc"}),
        encoding="utf-8",
    )
    monkeypatch.setattr(sk_common, "CONFIG_PATH", config_path)

    loaded = sk_common.load_config()

    assert loaded["push_transport"] == "unreal_wait"
    assert loaded["push_transport_policy_version"] == 1


def test_save_config_never_persists_rpc_as_startup_transport(tmp_path, monkeypatch):
    config_path = tmp_path / "sk_batch_config.json"
    monkeypatch.setattr(sk_common, "CONFIG_PATH", config_path)

    sk_common.save_config({"push_transport": "rpc"})

    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert saved["push_transport"] == "unreal_wait"
    assert saved["push_transport_policy_version"] == 1
