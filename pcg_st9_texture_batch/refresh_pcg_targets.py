"""Refresh pcg_targets.json from a running Unreal Editor.

The script uses Unreal's Python Remote Execution bridge. It does not launch or
modify the project; it only reads PCG graph dependencies, PCG DataAsset section
arrays, and currently spawned instanced/static mesh components.
"""
import argparse
import json
import sys
import tempfile
import textwrap
import time
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from pcg_texture_common import TARGETS_PATH

ENGINE_PYTHON = r"C:\Program Files\Epic Games\UE_5.8\Engine\Plugins\Experimental\PythonScriptPlugin\Content\Python"


def payload(output_path, graph_path):
    code = r'''
import json
import traceback
import unreal
from datetime import datetime

OUTPUT_PATH = __OUTPUT_PATH__
GRAPH_PATH = __GRAPH_PATH__
DATA_SECTIONS = ["Tree", "Shrub", "Stone", "Stump", "Bush", "Weed", "Grass", "Debris", "Rock"]


def asset_name_from_package(package_name):
    return str(package_name).rsplit("/", 1)[-1]


def object_path(package_name):
    package_name = str(package_name)
    return package_name + "." + asset_name_from_package(package_name)


def path_name(obj):
    try:
        return obj.get_path_name()
    except Exception:
        return str(obj)


def dependency_options():
    opts = unreal.AssetRegistryDependencyOptions()
    opts.include_soft_package_references = True
    opts.include_hard_package_references = True
    opts.include_searchable_names = False
    opts.include_soft_management_references = False
    opts.include_hard_management_references = False
    return opts


def graph_dependencies(package_name):
    registry = unreal.AssetRegistryHelpers.get_asset_registry()
    deps = registry.get_dependencies(package_name, dependency_options())
    return sorted(str(dep) for dep in deps if str(dep).startswith("/Game/"))


def read_dynamic_mesh_data_asset(package_name):
    asset = unreal.EditorAssetLibrary.load_asset(object_path(package_name))
    result = {"asset": package_name, "class": None, "sections": {}, "errors": []}
    if not asset:
        result["errors"].append("load failed")
        return result
    try:
        result["class"] = asset.get_class().get_name()
    except Exception:
        result["class"] = str(type(asset))
    for section in DATA_SECTIONS:
        entries = []
        try:
            arr = asset.get_editor_property(section)
        except Exception:
            continue
        for index, item in enumerate(arr):
            mesh = None
            weight = None
            try:
                mesh = item.get_editor_property("StaticMesh")
            except Exception:
                pass
            try:
                weight = item.get_editor_property("Weight")
            except Exception:
                pass
            entries.append({
                "index": index,
                "static_mesh": path_name(mesh) if mesh else None,
                "weight": weight,
            })
        if entries:
            result["sections"][section] = entries
    return result


def get_component_mesh(component):
    for prop in ("static_mesh", "StaticMesh"):
        try:
            mesh = component.get_editor_property(prop)
            if mesh:
                return mesh
        except Exception:
            pass
    try:
        if hasattr(component, "get_static_mesh"):
            return component.get_static_mesh()
    except Exception:
        pass
    return None


def current_level_st9_meshes():
    out = []
    try:
        actors = unreal.EditorLevelLibrary.get_all_level_actors()
    except Exception:
        actors = []
    for actor in actors:
        try:
            label = actor.get_actor_label()
        except Exception:
            label = actor.get_name()
        comps = []
        try:
            comps = actor.get_components_by_class(unreal.StaticMeshComponent)
        except Exception:
            pass
        for comp in comps:
            mesh = get_component_mesh(comp)
            if not mesh:
                continue
            mesh_path = path_name(mesh)
            if "/Game/Meshes/Tree/st9/" not in mesh_path:
                continue
            out.append({
                "actor": label,
                "component": comp.get_name(),
                "static_mesh": mesh_path,
            })
    return out


def main():
    report = {
        "graph": GRAPH_PATH,
        "errors": [],
        "graph_dependencies": [],
        "data_assets": [],
        "meshes": [],
        "source": "unreal_remote_python",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }
    try:
        report["graph_dependencies"] = graph_dependencies(GRAPH_PATH)
        da_paths = [
            dep for dep in report["graph_dependencies"]
            if dep.startswith("/Game/PCG/DataBase/")
        ]
        # Some PCG graph instances expose only helper dependencies; include the
        # known user-facing PCG_01 landscape data assets if graph deps are sparse.
        for fallback in [
            "/Game/PCG/DataBase/landscape/DA_Base_05",
            "/Game/PCG/DataBase/landscape/DA_Base_06",
            "/Game/PCG/DataBase/landscape/DA_River_01",
            "/Game/PCG/DataBase/landscape/DA_cliff_01",
            "/Game/PCG/DataBase/landscape/DA_root_01",
        ]:
            if fallback not in da_paths and unreal.EditorAssetLibrary.does_asset_exist(fallback):
                da_paths.append(fallback)
        for da_path in sorted(set(da_paths)):
            report["data_assets"].append(read_dynamic_mesh_data_asset(da_path))
        mesh_map = {}
        for da in report["data_assets"]:
            for section, entries in da.get("sections", {}).items():
                for entry in entries:
                    mesh = entry.get("static_mesh")
                    if mesh:
                        mesh_map.setdefault(mesh, {"static_mesh": mesh, "sections": [], "data_assets": []})
                        mesh_map[mesh]["sections"].append(section)
                        mesh_map[mesh]["data_assets"].append(da["asset"])
        for item in current_level_st9_meshes():
            mesh = item["static_mesh"]
            mesh_map.setdefault(mesh, {"static_mesh": mesh, "sections": [], "data_assets": []})
            mesh_map[mesh].setdefault("level_instances", []).append(item)
        report["meshes"] = sorted(mesh_map.values(), key=lambda x: x["static_mesh"].lower())
    except Exception as exc:
        report["errors"].append(str(exc))
        report["traceback"] = traceback.format_exc()
    with open(OUTPUT_PATH, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(report, indent=2, ensure_ascii=False))


main()
'''
    return code.replace("__OUTPUT_PATH__", repr(str(output_path))).replace("__GRAPH_PATH__", repr(graph_path))


def run_remote(script_path):
    sys.path.insert(0, ENGINE_PYTHON)
    import remote_execution

    remote = remote_execution.RemoteExecution()
    remote.start()
    try:
        deadline = time.time() + 10
        while time.time() < deadline and not remote.remote_nodes:
            time.sleep(0.2)
        if not remote.remote_nodes:
            raise RuntimeError("No Unreal Python remote nodes discovered. Open MyProject2 and enable Python remote execution.")
        nodes = remote.remote_nodes
        if hasattr(nodes, "keys"):
            node_id = next(iter(nodes.keys()))
        else:
            first = nodes[0]
            node_id = first.get("node_id") if isinstance(first, dict) else first.node_id
        remote.open_command_connection(node_id)
        command = Path(script_path).read_text(encoding="utf-8")
        result = remote.run_command(command, unattended=True, exec_mode=remote_execution.MODE_EXEC_FILE)
        if not result.get("success"):
            raise RuntimeError("Unreal remote command failed: {}".format(result))
        return result
    finally:
        remote.stop()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=str(TARGETS_PATH))
    parser.add_argument("--graph", default="/Game/PCG/PCG_01")
    args = parser.parse_args()
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", suffix="_pcg_targets_payload.py", delete=False, encoding="utf-8") as handle:
        handle.write(payload(out_path, args.graph))
        payload_path = Path(handle.name)
    try:
        run_remote(payload_path)
    finally:
        try:
            payload_path.unlink()
        except OSError:
            pass
    data = json.loads(out_path.read_text(encoding="utf-8"))
    print(json.dumps({
        "out": str(out_path),
        "graph": data.get("graph"),
        "data_assets": len(data.get("data_assets", [])),
        "meshes": len(data.get("meshes", [])),
        "errors": data.get("errors", []),
    }, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
