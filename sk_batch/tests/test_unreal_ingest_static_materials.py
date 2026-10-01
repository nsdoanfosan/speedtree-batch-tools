"""Static imports must not inherit skeletal material mutation or folder saves."""
import types

import pytest

from test_unreal_ingest import _DurableSaveFake, load_runner


class FakeStaticMesh:
    def __init__(self, slots):
        self.slots = slots

    def get_editor_property(self, name):
        assert name == "static_materials", "StaticMesh.materials is protected"
        return self.slots


class FakeMaterial:
    def __init__(self, path):
        self.path = path

    def get_path_name(self):
        return self.path

    def get_base_material(self):
        return self

    def get_editor_property(self, name):
        raise AssertionError("Static audit must not inspect skeletal/voxel usage: " + name)

    def set_editor_property(self, name, value):
        raise AssertionError("Shared master mutation is forbidden")


class FakeSlot:
    def __init__(self, material):
        self.material = material

    def get_editor_property(self, name):
        return {"material_slot_name": "Leaf", "material_interface": self.material}[name]


@pytest.mark.parametrize("materials,needs_assignment,missing,placeholder", [
    ([None], True, ["Leaf[0]"], []),
    ([FakeMaterial("/Engine/EngineMaterials/DefaultMaterial.DefaultMaterial")], True, [], ["Leaf[0]"]),
    ([FakeMaterial("/Game/Material/MI_Leaf.MI_Leaf")], False, [], []),
    ([], True, [], []),
])
def test_static_pre_and_post_are_passive_and_report_unassigned_slots(materials, needs_assignment, missing, placeholder):
    runner = load_runner()
    mesh = FakeStaticMesh([FakeSlot(material) for material in materials])
    runner.unreal.StaticMesh = FakeStaticMesh
    def forbidden(*args, **kwargs):
        raise AssertionError("Static audit invoked a skeletal material mutation or section audit")
    runner._ensure_nanite_voxel_material_usage = forbidden
    runner.audit_unreal_skeletal_mesh_material_sections = forbidden
    runner.unreal.EditorAssetLibrary = types.SimpleNamespace(load_asset=lambda path: mesh, save_asset=forbidden)
    runner.unreal.MaterialEditingLibrary = types.SimpleNamespace(recompile_material=forbidden)
    runner.unreal.get_editor_subsystem = forbidden
    for audit in (runner._material_prebuild_compile_and_usage_normalization, runner._material_postbuild_slot_audit):
        result = audit("/Game/Meshes/SM_Leaf")
        assert result["status"] == "static_material_audit"
        assert result["no_master_mutation"] is True
        assert result["needs_assignment"] is needs_assignment
        assert result["missing_slots"] == missing
        assert result["placeholder_slots"] == placeholder
        assert result["slot_count"] == len(materials)
        assert result["section_material_validation"]["skeletal_section_audit_called"] is False


def test_static_twenty_assets_save_exact_packages_and_keep_unrelated_dirty_material(tmp_path):
    runner = load_runner()
    environment = _DurableSaveFake(runner, tmp_path / "Content")
    runner.unreal.StaticMesh = FakeStaticMesh
    runner.unreal.SkeletalMesh = type("FakeSkeletalMesh", (), {})
    paths = [f"/Game/Meshes/Leaves/SM_Leaf_G{index:02d}" for index in range(1, 21)]
    for path in paths:
        environment.add_asset(path, FakeStaticMesh([]))
        environment.dirty.add(path.casefold())
    unrelated = "/Game/Meshes/Leaves/MI_ExistingLeaf"
    environment.add_asset(unrelated)
    environment.dirty.add(unrelated.casefold())
    item = {"mesh_path": paths[0], "unreal_folder": "/Game/Meshes/Leaves/", "assets": [{"asset_data": {"_asset_type": "StaticMesh", "asset_path": path}} for path in paths]}
    def forbidden(*args, **kwargs):
        raise AssertionError("Static imports must not call the skeletal thumbnail-free saver")
    runner._save_skeletal_mesh_owned_without_thumbnail = forbidden
    ledger = runner._new_durable_save_ledger()
    saved = runner._save_item_assets(item, [{"asset_path": path} for path in paths], durable_saves=ledger)
    assert saved == paths
    assert environment.save_directory_calls == []
    assert [path for path, dirty_only in environment.save_asset_calls] == paths
    assert environment.dirty == {unrelated.casefold()}
    assert len(ledger["records"]) == 20


def test_static_checkout_excludes_unrelated_material_candidates():
    runner = load_runner()
    path = "/Game/Meshes/Leaves/SM_Leaf_G01"
    unrelated = "/Game/Meshes/Leaves/MI_ExistingLeaf"
    item = {"assets": [{"asset_data": {"_asset_type": "StaticMesh", "asset_path": path}}], "checkout_asset_paths": [path, unrelated]}
    calls = []
    runner.unreal.EditorAssetLibrary = types.SimpleNamespace(does_asset_exist=lambda path: True)
    runner.unreal.EditorAssetSubsystem = object()
    runner.unreal.get_editor_subsystem = lambda kind: types.SimpleNamespace(checkout_asset=lambda path: calls.append(path) or True)
    result = runner._checkout_existing_assets(item)
    assert calls == [path]
    assert result["checked_out"] == [path]


def test_existing_twenty_static_reimports_never_read_skeleton_or_enter_skeletal_stages(tmp_path):
    runner = load_runner()
    environment = _DurableSaveFake(runner, tmp_path / "Content")
    runner.unreal.StaticMesh = FakeStaticMesh
    runner.unreal.SkeletalMesh = type("FakeSkeletalMesh", (), {})
    paths = [f"/Game/Meshes/Leaves/SM_Leaf_G{index:02d}" for index in range(1, 21)]
    imported = []
    assets = []
    for index, path in enumerate(paths):
        environment.add_asset(path, FakeStaticMesh([FakeSlot(None)]))
        source = tmp_path / f"group{index}.fbx"
        source.write_bytes(b"owned fixture FBX")
        assets.append({"asset_data": {"_asset_type": "StaticMesh", "asset_path": path, "file_path": str(source)}, "property_data": {}})
    existing_material = "/Game/Meshes/Leaves/MI_Preexisting"
    environment.add_asset(existing_material)
    environment.dirty.add(existing_material.casefold())
    item = {"send2ue_unreal_py": "existing_importer.py", "mesh_path": paths[0], "assets": assets, "unreal_folder": "/Game/Meshes/Leaves/", "checkout_asset_paths": paths, "wind_policy": {"mode": "disabled", "requires_json": False}, "wind_json": None}
    calls = types.SimpleNamespace(import_asset=lambda file_path, asset_data, property_data: imported.append(asset_data["asset_path"]) or asset_data["asset_path"])
    runner._load_send2ue_unreal = lambda path: types.SimpleNamespace(UnrealRemoteCalls=calls)
    runner.unreal.EditorAssetSubsystem = object()
    checkouts = []
    runner.unreal.get_editor_subsystem = lambda kind: types.SimpleNamespace(checkout_asset=lambda path: checkouts.append(path) or True)
    def forbidden(*args, **kwargs):
        raise AssertionError("Static-only ingest entered an unrelated skeletal stage")
    for name in ("_clear_placeholder_skeleton_before_import", "_default_physics_asset_preexisting", "_prepare_speedtree_skeletal_optimization", "_apply_dynamic_wind", "_ingest_cluster_assembly", "_prepare_assembly_runtime_validation", "_save_skeletal_mesh_owned_without_thumbnail"):
        setattr(runner, name, forbidden)
    result = runner.ingest_item(item)
    assert result["status"] == "imported_ok"
    assert imported == paths
    assert checkouts == paths
    assert len(result["prebuild_materials"]) == len(result["postbuild_materials"]) == 20
    assert result["material_assignment_pending"] is True
    assert all(report["needs_assignment"] and report["no_master_mutation"] for report in result["postbuild_materials"])
    assert result["skeleton"]["status"] == result["optimization"]["status"] == "skipped"
    assert environment.save_directory_calls == []
    assert environment.dirty == {existing_material.casefold()}


def test_static_only_route_rejects_a_skeletal_wind_contract_before_import():
    runner = load_runner()
    def forbidden(*args, **kwargs):
        raise AssertionError("Invalid static wind contract must not import")
    runner._import_manifest_asset = forbidden
    with pytest.raises(RuntimeError, match="disabled DynamicWind"):
        runner._ingest_static_mesh_item(object(), {"wind_policy": {"requires_json": True}}, {}, {})


class FakeStaticImportData:
    def __init__(self, remove_degenerates=True):
        self.remove_degenerates = remove_degenerates
        self.writes = []

    def get_editor_property(self, name):
        assert name == "remove_degenerates"
        return self.remove_degenerates

    def set_editor_property(self, name, value):
        assert name == "remove_degenerates", "Do not alter any other import option"
        self.writes.append((name, value))
        self.remove_degenerates = value


class FakeStaticReimportMesh(FakeStaticMesh):
    def __init__(self):
        super().__init__([FakeSlot(None)])
        self.import_data = FakeStaticImportData()

    def get_editor_property(self, name):
        return self.import_data if name == "asset_import_data" else super().get_editor_property(name)


def test_static_reimport_syncs_declared_option_before_factory_reads_existing_data_and_only_owned_assets(tmp_path):
    runner = load_runner()
    environment = _DurableSaveFake(runner, tmp_path / "Content")
    runner.unreal.StaticMesh = FakeStaticReimportMesh
    runner.unreal.FbxStaticMeshImportData = FakeStaticImportData
    runner.unreal.SkeletalMesh = type("FakeSkeletalMesh", (), {})
    paths = ["/Game/Leaves/SM_GroupA", "/Game/Leaves/SM_GroupB"]
    meshes = {path: FakeStaticReimportMesh() for path in paths}
    unrelated = "/Game/Leaves/SM_OriginalSource"
    meshes[unrelated] = FakeStaticReimportMesh()
    for path, mesh in meshes.items():
        environment.add_asset(path, mesh)
    assets = []
    for index,path in enumerate(paths):
        source = tmp_path / f"group{index}.fbx"
        source.write_bytes(b"owned exported FBX")
        assets.append({"asset_data":{"_asset_type":"StaticMesh","asset_path":path,"file_path":str(source)},
                       "property_data":{"unreal":{"import_method":{"fbx":{"static_mesh_import_data":{
                           "remove_degenerates":{"value":False}}}}}}})
    imported = []
    checked_out = []
    def factory(file_path,asset_data,property_data):
        path=asset_data["asset_path"]
        assert path in checked_out
        # UE's unattended factory deliberately ignores the newly supplied UI
        # setting and reads the old asset import data instead.
        assert meshes[path].import_data.remove_degenerates is False
        imported.append(path)
        return path
    runner._load_send2ue_unreal=lambda path: types.SimpleNamespace(UnrealRemoteCalls=types.SimpleNamespace(import_asset=factory))
    runner.unreal.EditorAssetSubsystem=object()
    runner.unreal.get_editor_subsystem=lambda kind:types.SimpleNamespace(checkout_asset=lambda path:checked_out.append(path) or True)
    item={"send2ue_unreal_py":"canonical.py","mesh_path":paths[0],"assets":assets,
          "checkout_asset_paths":paths,"wind_policy":{"requires_json":False},"wind_json":None}
    result=runner.ingest_item(item)
    assert imported==checked_out==paths
    assert meshes[unrelated].import_data.remove_degenerates is True
    assert meshes[unrelated].import_data.writes==[]
    assert [path for path,_ in environment.save_asset_calls]==paths
    assert all(report["preimport"]["before"] is True and report["preimport"]["after"] is False and
               report["postimport"]["actual_import_data_value"] is False
               for report in result["static_import_settings"])


def test_static_reimport_setting_requires_explicit_bool_and_postimport_readback(tmp_path):
    runner=load_runner()
    runner.unreal.StaticMesh=FakeStaticReimportMesh
    runner.unreal.FbxStaticMeshImportData=FakeStaticImportData
    path="/Game/Leaves/SM_Owned"
    mesh=FakeStaticReimportMesh()
    runner.unreal.EditorAssetLibrary=types.SimpleNamespace(load_asset=lambda value:mesh)
    asset={"asset_data":{"_asset_type":"StaticMesh","asset_path":path,"file_path":"owned.fbx"},
           "property_data":{"unreal":{"import_method":{"fbx":{"static_mesh_import_data":{
               "remove_degenerates":{"value":"False"}}}}}}}
    with pytest.raises(RuntimeError,match="explicit Boolean"):
        runner._sync_static_reimport_remove_degenerates(asset)
    assert mesh.import_data.writes==[]
    asset["property_data"]["unreal"]["import_method"]["fbx"]["static_mesh_import_data"]["remove_degenerates"]["value"]=False
    with pytest.raises(RuntimeError,match="do not honor"):
        runner._audit_static_import_remove_degenerates(asset,mesh)
    asset["asset_data"]["_asset_type"]="SkeletalMesh"
    assert runner._sync_static_reimport_remove_degenerates(asset)["status"]=="skipped"
    assert mesh.import_data.writes==[]
