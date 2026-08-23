import json
import sys
from pathlib import Path

import pytest

TOOL_DIR = Path(__file__).resolve().parents[1]
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from final_assembly_shape_migration import (  # noqa: E402
    _package_path,
    discover_generated_final_assemblies,
)


def _write_report(path, *, status="imported_ok", build_status="ok", assembly=""):
    path.write_text(
        json.dumps(
            {
                "status": status,
                "cluster_assembly": {
                    "build": {
                        "status": build_status,
                        "assembly": assembly,
                    }
                },
            }
        ),
        encoding="utf-8",
    )


def test_package_path_strips_object_suffix():
    assert _package_path("/Game/A/SK_A_NaniteAssembly.SK_A_NaniteAssembly") == (
        "/Game/A/SK_A_NaniteAssembly"
    )


def test_discovery_deduplicates_only_successful_canonical_final_assemblies(
    tmp_path,
):
    assembly = "/Game/A/SK_A_NaniteAssembly.SK_A_NaniteAssembly"
    _write_report(tmp_path / "SK_A_unreal_1.json", assembly=assembly)
    _write_report(tmp_path / "SK_A_waiting_unreal_2.json", assembly=assembly)
    _write_report(
        tmp_path / "SK_B_unreal_1.json",
        status="failed",
        assembly="/Game/A/SK_B_NaniteAssembly.SK_B_NaniteAssembly",
    )

    result = discover_generated_final_assemblies(tmp_path)

    assert not result["rejected_reports"]
    assert len(result["targets"]) == 1
    assert result["targets"][0]["asset_path"] == "/Game/A/SK_A_NaniteAssembly"
    assert len(result["targets"][0]["source_reports"]) == 2


@pytest.mark.parametrize(
    "assembly",
    [
        "/Game/A/SK_A_NA_Base.SK_A_NA_Base",
        "/Game/A/SK_A_Assembly.SK_A_Assembly",
        "/Plugin/A/SK_A_NaniteAssembly.SK_A_NaniteAssembly",
    ],
)
def test_discovery_rejects_noncanonical_successful_targets(tmp_path, assembly):
    _write_report(tmp_path / "SK_A_unreal_1.json", assembly=assembly)

    result = discover_generated_final_assemblies(tmp_path)

    assert not result["targets"]
    assert result["rejected_reports"][0]["assembly"] == assembly
