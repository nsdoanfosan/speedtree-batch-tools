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


def test_heavy_process_limit_cannot_be_disabled_or_raised_above_six():
    assert bounded_heavy_process_item_limit(0) == 6
    assert bounded_heavy_process_item_limit(100) == 6
    assert bounded_heavy_process_item_limit(2) == 2


def test_load_config_preserves_explicit_rpc_transport(tmp_path, monkeypatch):
    config_path = tmp_path / "sk_batch_config.json"
    config_path.write_text(
        json.dumps({"push_transport": "rpc"}),
        encoding="utf-8",
    )
    monkeypatch.setattr(sk_common, "CONFIG_PATH", config_path)

    loaded = sk_common.load_config()

    assert loaded["push_transport"] == "rpc"


def test_save_config_preserves_explicit_rpc_transport(tmp_path, monkeypatch):
    config_path = tmp_path / "sk_batch_config.json"
    monkeypatch.setattr(sk_common, "CONFIG_PATH", config_path)

    sk_common.save_config({"push_transport": "rpc"})

    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert saved["push_transport"] == "rpc"
