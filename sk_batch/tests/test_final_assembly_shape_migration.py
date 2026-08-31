import json
import sys
import types
from pathlib import Path

import pytest

TOOL_DIR = Path(__file__).resolve().parents[1]
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from final_assembly_shape_migration import (  # noqa: E402
    _migrate_target,
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


class _FakeNaniteSettings:
    def __init__(self, shape_preservation):
        self.shape_preservation = shape_preservation

    def get_editor_property(self, name):
        assert name == "shape_preservation"
        return self.shape_preservation

    def set_editor_property(self, name, value):
        assert name == "shape_preservation"
        self.shape_preservation = value


class _FakeSkeletalMesh:
    def __init__(self, shape_preservation):
        self.nanite_settings = _FakeNaniteSettings(shape_preservation)
        self.modify_calls = 0
        self.settings_assignments = 0

    def get_editor_property(self, name):
        assert name == "nanite_settings"
        return self.nanite_settings

    def set_editor_property(self, name, value):
        assert name == "nanite_settings"
        self.nanite_settings = value
        self.settings_assignments += 1

    def modify(self):
        self.modify_calls += 1


class _FakeAssetSubsystem:
    def __init__(self):
        self.checkout_calls = []

    def checkout_asset(self, path):
        self.checkout_calls.append(path)
        return True


def _fake_unreal(mesh, *, saver=None):
    subsystem = _FakeAssetSubsystem()

    class EditorAssetLibrary:
        @staticmethod
        def load_asset(path):
            assert path == "/Game/A/SK_A_NaniteAssembly"
            return mesh

        @staticmethod
        def save_loaded_asset(*_args, **_kwargs):
            raise AssertionError("thumbnail-rendering save path must not be used")

    inspector = types.SimpleNamespace(
        get_skeletal_mesh_assembly_overview_json=lambda _mesh: json.dumps(
            {
                "success": True,
                "provenance_present": True,
                "part_count": 4,
                "instance_count": 12,
            }
        )
    )
    unreal = types.SimpleNamespace(
        EditorAssetLibrary=EditorAssetLibrary,
        EditorAssetSubsystem=object(),
        SkeletalMesh=_FakeSkeletalMesh,
        NaniteAssemblyInspectorLibrary=inspector,
        get_editor_subsystem=lambda _kind: subsystem,
    )
    if saver is not None:
        unreal.CodexMaterialToolsLibrary = types.SimpleNamespace(
            save_asset_package_without_thumbnail=saver,
        )
    return unreal, subsystem


def test_migration_requires_thumbnail_free_saver_before_checkout_or_mutation():
    mesh = _FakeSkeletalMesh("voxelize")
    unreal, subsystem = _fake_unreal(mesh)

    with pytest.raises(RuntimeError, match="thumbnail-free package save API"):
        _migrate_target(
            unreal,
            {"asset_path": "/Game/A/SK_A_NaniteAssembly"},
            "preserve_area",
        )

    assert subsystem.checkout_calls == []
    assert mesh.modify_calls == 0
    assert mesh.settings_assignments == 0
    assert mesh.nanite_settings.shape_preservation == "voxelize"


def test_migration_saves_through_thumbnail_free_plugin_api():
    mesh = _FakeSkeletalMesh("voxelize")
    saved = []
    unreal, subsystem = _fake_unreal(
        mesh,
        saver=lambda asset: saved.append(asset) or True,
    )

    result = _migrate_target(
        unreal,
        {
            "asset_path": "/Game/A/SK_A_NaniteAssembly",
            "source_reports": ["report.json"],
        },
        "preserve_area",
    )

    assert result["status"] == "migrated"
    assert result["rebuild_trigger"] == "nanite_settings_property_assignment"
    assert subsystem.checkout_calls == ["/Game/A/SK_A_NaniteAssembly"]
    assert saved == [mesh]
    assert mesh.nanite_settings.shape_preservation == "preserve_area"
