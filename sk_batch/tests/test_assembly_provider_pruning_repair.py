import gzip
import hashlib
import json
from pathlib import Path

import pytest

from sk_batch.assembly_provider_pruning_repair import (
    ProviderPruningRepairError,
    apply_repair_plan,
    build_repair_plan,
)


GUID_A = "2A1cLK//QU6WQn0W/mYuUQ=="
GUID_B = "GPgIAQVJqUOnsFFIWUYiyA=="


def _generator(guid, *, generator_type="Frond", threshold="1"):
    return f"""
        <Generator Type="{generator_type}">
          <GUID>{guid}</GUID>
          <Properties>
            <Property><Name>Shade Pruning:Style</Name><Value>1</Value></Property>
            <Property><Name>Shade Pruning:Threshold</Name><Value>{threshold}</Value></Property>
          </Properties>
        </Generator>"""


def _write_spm(path, *, threshold="1", second_type="Frond"):
    text = (
        "<SpeedTreeModel><Generators>"
        + _generator(GUID_A, threshold=threshold)
        + _generator(GUID_B, generator_type=second_type, threshold=threshold)
        + _generator("unrelated==", threshold="0.75")
        + "</Generators></SpeedTreeModel>"
    )
    path.write_bytes(gzip.compress(text.encode("utf-8"), mtime=0))
    return text


def _write_report(path, spm):
    path.write_text(
        json.dumps(
            {
                "items": [
                    {
                        "cluster_assembly": {
                            "dependencies": [
                                {
                                    "name": "branch_03",
                                    "normalized_variants": {
                                        "target_deliveries": [
                                            {
                                                "spm": str(spm),
                                                "live_generator_bindings": [
                                                    {"generator_guid": GUID_A},
                                                    {"generator_guid": GUID_B},
                                                ],
                                            }
                                        ]
                                    },
                                }
                            ]
                        }
                    }
                ]
            }
        ),
        encoding="utf-8",
    )


def test_apply_repairs_only_exact_active_provider_generators(tmp_path):
    spm = tmp_path / "SK_tree_31.spm"
    report = tmp_path / "fleet.json"
    before_text = _write_spm(spm)
    before_hash = hashlib.sha256(spm.read_bytes()).hexdigest()
    _write_report(report, spm)

    plan = build_repair_plan(spm, report, {"branch_03"})
    assert plan["status"] == "ready"
    assert plan["change_count"] == 2

    result = apply_repair_plan(plan, backup_root=tmp_path / "backups")
    assert result["status"] == "repaired"
    assert result["applied"] is True
    backup = Path(result["backup"])
    assert hashlib.sha256(backup.read_bytes()).hexdigest() == before_hash
    assert gzip.decompress(backup.read_bytes()).decode("utf-8") == before_text

    after = gzip.decompress(spm.read_bytes()).decode("utf-8")
    assert after.count("0.20000000298023224") == 2
    assert "<Value>0.75</Value>" in after


def test_already_repaired_is_idempotent(tmp_path):
    spm = tmp_path / "SK_tree_31.spm"
    report = tmp_path / "fleet.json"
    _write_spm(spm, threshold="0.20000000298023224")
    _write_report(report, spm)

    plan = build_repair_plan(spm, report, {"branch_03"})
    assert plan["status"] == "already_repaired"
    result = apply_repair_plan(plan, backup_root=tmp_path / "backups")
    assert result["applied"] is False
    assert not (tmp_path / "backups").exists()


def test_refuses_non_frond_provider_generator(tmp_path):
    spm = tmp_path / "SK_tree_31.spm"
    report = tmp_path / "fleet.json"
    _write_spm(spm, second_type="Leaf Mesh")
    _write_report(report, spm)

    with pytest.raises(ProviderPruningRepairError, match="is not Frond"):
        build_repair_plan(spm, report, {"branch_03"})


def test_refuses_report_for_another_target(tmp_path):
    spm = tmp_path / "SK_tree_31.spm"
    other = tmp_path / "SK_tree_22.spm"
    report = tmp_path / "fleet.json"
    _write_spm(spm)
    _write_spm(other)
    _write_report(report, other)

    with pytest.raises(
        ProviderPruningRepairError,
        match="no active Generator GUIDs",
    ):
        build_repair_plan(spm, report, {"branch_03"})
