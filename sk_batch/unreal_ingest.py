"""Shared Unreal-side ingest for SK Batch RPC and headless transports.

This module is imported inside Unreal's embedded Python.  It deliberately uses
Send2UE's own ``dependencies/unreal.py`` importer so the serialized property
data drives the same FBX, skeleton, PhysicsAsset, LOD, socket, and vertex-color
rules as the interactive add-on.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import traceback
from datetime import datetime
from pathlib import Path

import unreal


SCHEMA_VERSION = 1
TERMINAL_STATES = {"imported_ok", "data_error", "manual_required", "not_run"}
_SEND2UE_UNREAL_MODULES = {}


def _now():
    return datetime.now().isoformat(timespec="seconds")


def _atomic_write_json(path, data):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(data, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    os.replace(temporary, target)


def _load_json(path, default=None):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {} if default is None else default
    return value


def _load_send2ue_unreal(file_path):
    resolved = str(Path(file_path).resolve())
    module = _SEND2UE_UNREAL_MODULES.get(resolved)
    if module is not None:
        return module
    module_name = "sk_batch_send2ue_unreal_" + str(abs(hash(resolved)))
    spec = importlib.util.spec_from_file_location(module_name, resolved)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load Send2UE Unreal importer: {resolved}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    _SEND2UE_UNREAL_MODULES[resolved] = module
    return module


def _execute_command_groups(command_groups, label):
    for index, commands in enumerate(command_groups or []):
        if not isinstance(commands, list):
            raise TypeError(f"{label}[{index}] is not a command list")
        namespace = {
            "__name__": f"sk_batch_{label}_{index}",
            "unreal": unreal,
        }
        exec("\n".join(str(line) for line in commands), namespace, namespace)


def _material_pipeline_checkouts():
    paths = set()
    for module in tuple(sys.modules.values()):
        module_file = str(getattr(module, "__file__", "")).replace("\\", "/")
        if not module_file.endswith("/ue_material_setup.py"):
            continue
        values = getattr(module, "_CHECKED_OUT_MATERIAL_PIPELINE_ASSETS", ())
        paths.update(str(path) for path in values if path)
    return sorted(paths)


def _checkout_existing_assets(item):
    candidates = list(item.get("checkout_asset_paths") or [])
    existing = [
        path
        for path in candidates
        if path and unreal.EditorAssetLibrary.does_asset_exist(path)
    ]
    if not existing:
        return {"existing": [], "checked_out": []}
    subsystem = unreal.get_editor_subsystem(unreal.EditorAssetSubsystem)
    failed = [path for path in existing if not subsystem.checkout_asset(path)]
    if failed:
        raise RuntimeError("source-control checkout failed: " + ", ".join(failed))
    return {"existing": existing, "checked_out": existing}


def _run_lod_and_socket_operations(send2ue_unreal, manifest_asset, asset_data, property_data):
    operations = manifest_asset.get("operations") or {}
    asset_path = asset_data.get("asset_path")
    asset_type = asset_data.get("_asset_type")
    lods = asset_data.get("lods") or {}
    calls = send2ue_unreal.UnrealRemoteCalls

    if operations.get("reset_and_import_lods") and lods:
        if asset_type == "SkeletalMesh":
            calls.reset_skeletal_mesh_lods(asset_path, property_data)
        else:
            calls.reset_static_mesh_lods(asset_path)
        for index in range(1, len(lods) + 1):
            lod_file_path = lods.get(str(index))
            if asset_type == "SkeletalMesh":
                calls.import_skeletal_mesh_lod(asset_path, lod_file_path, index)
            else:
                calls.import_static_mesh_lod(asset_path, lod_file_path, index)
        for index in range(0, len(lods) + 1):
            if asset_type == "SkeletalMesh":
                calls.set_skeletal_mesh_lod_build_settings(
                    asset_path,
                    index,
                    property_data,
                )
            else:
                calls.set_static_mesh_lod_build_settings(
                    asset_path,
                    index,
                    property_data,
                )

    if operations.get("create_static_mesh_sockets") and asset_data.get("sockets"):
        calls.set_static_mesh_sockets(asset_path, asset_data)


def _import_manifest_asset(send2ue_unreal, manifest_asset):
    asset_data = dict(manifest_asset.get("asset_data") or {})
    property_data = manifest_asset.get("property_data") or {}
    if asset_data.get("skip"):
        return {"skipped": True, "asset_path": asset_data.get("asset_path")}

    file_path = asset_data.get("file_path")
    if not file_path or not Path(file_path).is_file():
        raise RuntimeError(f"exported source file missing: {file_path}")

    _execute_command_groups(
        manifest_asset.get("pre_import_commands"),
        "pre_import",
    )
    imported = send2ue_unreal.UnrealRemoteCalls.import_asset(
        file_path,
        asset_data,
        property_data,
    )
    if asset_data.get("_ue_groom_adapter") and isinstance(imported, dict):
        groom_path = imported.get("groom_asset_path")
        if groom_path:
            asset_data["asset_path"] = groom_path
    if asset_data.get("fcurve_file_path"):
        send2ue_unreal.UnrealRemoteCalls.import_animation_fcurves(
            asset_data.get("asset_path"),
            asset_data.get("fcurve_file_path"),
        )
    _execute_command_groups(
        manifest_asset.get("post_import_commands"),
        "post_import",
    )
    _run_lod_and_socket_operations(
        send2ue_unreal,
        manifest_asset,
        asset_data,
        property_data,
    )
    return {
        "asset_path": asset_data.get("asset_path"),
        "file_path": file_path,
        "imported": imported,
    }


def _apply_dynamic_wind(item):
    wind_json = item.get("wind_json")
    if not wind_json:
        return {"status": "skipped", "reason": "manifest has no wind JSON"}
    if not Path(wind_json).is_file():
        raise RuntimeError(f"dynamic wind JSON missing: {wind_json}")
    mesh_path = item.get("mesh_path")
    mesh = unreal.EditorAssetLibrary.load_asset(mesh_path)
    if not mesh:
        raise RuntimeError(f"mesh not found for dynamic wind: {mesh_path}")
    if not hasattr(unreal, "CodexDynamicWindImportLibrary"):
        raise RuntimeError("CodexDynamicWindImportLibrary missing")
    result = unreal.CodexDynamicWindImportLibrary.import_dynamic_wind_json_to_skeletal_mesh(
        mesh,
        str(wind_json),
    )
    return {"status": "ok", "mesh": mesh_path, "result": str(result)}


def _material_compile_and_slot_validation(mesh_path):
    mesh = unreal.EditorAssetLibrary.load_asset(mesh_path)
    if not mesh:
        raise RuntimeError(f"mesh not found: {mesh_path}")
    slots = list(mesh.get_editor_property("materials") or [])
    if not slots:
        raise RuntimeError("skeletal mesh has no material slots")

    details = []
    missing = []
    compiled_base_materials = set()
    compile_errors = []
    for index, slot in enumerate(slots):
        slot_name = str(slot.get_editor_property("material_slot_name"))
        material = slot.get_editor_property("material_interface")
        material_path = material.get_path_name() if material else ""
        details.append({"index": index, "slot": slot_name, "material": material_path})
        if not material:
            missing.append(f"{slot_name}[{index}]")
            continue
        try:
            base_material = material.get_base_material()
        except Exception:
            base_material = material
        if not base_material:
            continue
        base_path = base_material.get_path_name()
        if base_path in compiled_base_materials:
            continue
        compiled_base_materials.add(base_path)
        errors = unreal.MaterialEditingLibrary.recompile_material(base_material) or []
        compile_errors.extend(f"{base_path}: {error}" for error in errors)

    if missing:
        raise RuntimeError("unassigned material slots: " + ", ".join(missing))
    if compile_errors:
        raise RuntimeError("material compile failed: " + " | ".join(compile_errors))
    return {
        "mesh": mesh_path,
        "slots": details,
        "compiled_base_materials": sorted(compiled_base_materials),
    }


def _save_item_assets(item, imported_assets):
    saved = []
    for value in imported_assets:
        asset_path = value.get("asset_path") if isinstance(value, dict) else None
        if asset_path and unreal.EditorAssetLibrary.does_asset_exist(asset_path):
            unreal.EditorAssetLibrary.save_asset(asset_path, only_if_is_dirty=False)
            saved.append(asset_path)
    mesh_path = item.get("mesh_path")
    if mesh_path and unreal.EditorAssetLibrary.does_asset_exist(mesh_path):
        unreal.EditorAssetLibrary.save_asset(mesh_path, only_if_is_dirty=False)
        if mesh_path not in saved:
            saved.append(mesh_path)
    folder = item.get("unreal_folder")
    if folder:
        unreal.EditorAssetLibrary.save_directory(folder, only_if_is_dirty=True)
    return saved


def ingest_item(item):
    send2ue_unreal = _load_send2ue_unreal(item["send2ue_unreal_py"])
    checkout = _checkout_existing_assets(item)
    imported_assets = [
        _import_manifest_asset(send2ue_unreal, manifest_asset)
        for manifest_asset in item.get("assets") or []
    ]
    material_checkouts = _material_pipeline_checkouts()
    checkout["material_pipeline"] = material_checkouts
    checkout["checked_out"] = list(
        dict.fromkeys(list(checkout["checked_out"]) + material_checkouts)
    )
    wind = _apply_dynamic_wind(item)
    saved = _save_item_assets(item, imported_assets)
    materials = _material_compile_and_slot_validation(item["mesh_path"])
    saved = _save_item_assets(item, imported_assets)
    return {
        "status": "imported_ok",
        "checkout": checkout,
        "assets": imported_assets,
        "wind": wind,
        "materials": materials,
        "saved": saved,
    }


def _initial_checkpoint(manifest):
    return {
        "schema_version": SCHEMA_VERSION,
        "manifest": manifest.get("manifest_path"),
        "started_at": _now(),
        "updated_at": _now(),
        "complete": False,
        "current_item": None,
        "items": {},
    }


def _recover_interrupted_item(checkpoint, manifest_items, max_item_crash_retries):
    current_id = checkpoint.get("current_item")
    if not current_id:
        return
    state = checkpoint.setdefault("items", {}).get(current_id)
    if not state or state.get("status") != "importing":
        checkpoint["current_item"] = None
        return

    crash_count = int(state.get("crash_count", 0)) + 1
    state.update(
        {
            "status": "unreal_crash",
            "crash_count": crash_count,
            "updated_at": _now(),
            "message": "UnrealEditor-Cmd exited while this item was importing",
        }
    )
    if crash_count > max_item_crash_retries:
        state.update(
            {
                "status": "manual_required",
                "message": (
                    "Unreal commandlet crash retry limit exceeded "
                    f"({max_item_crash_retries})"
                ),
            }
        )
    checkpoint["current_item"] = None


def run_manifest(manifest_path, checkpoint_path=None, report_path=None):
    manifest_path = str(Path(manifest_path).resolve())
    manifest = _load_json(manifest_path)
    if manifest.get("schema_version") != SCHEMA_VERSION:
        raise RuntimeError(
            f"unsupported SK Batch manifest schema: {manifest.get('schema_version')}"
        )
    manifest["manifest_path"] = manifest_path
    checkpoint_path = str(
        Path(checkpoint_path or manifest["checkpoint_path"]).resolve()
    )
    report_path = str(Path(report_path or manifest["report_path"]).resolve())
    checkpoint = _load_json(checkpoint_path, default=None) or _initial_checkpoint(manifest)
    max_retries = int(manifest.get("max_item_crash_retries", 2))
    manifest_items = manifest.get("items") or []
    _recover_interrupted_item(checkpoint, manifest_items, max_retries)
    _atomic_write_json(checkpoint_path, checkpoint)

    for item in manifest_items:
        queue_id = str(item["queue_id"])
        fingerprint = item["fingerprint"]
        previous = checkpoint.setdefault("items", {}).get(queue_id, {})
        if (
            previous.get("fingerprint") == fingerprint
            and previous.get("status") in TERMINAL_STATES
        ):
            continue

        crash_count = int(previous.get("crash_count", 0))
        state = {
            "status": "importing",
            "fingerprint": fingerprint,
            "crash_count": crash_count,
            "started_at": _now(),
            "updated_at": _now(),
            "manifest": manifest_path,
            "report": item.get("report_path"),
        }
        checkpoint["items"][queue_id] = state
        checkpoint["current_item"] = queue_id
        checkpoint["updated_at"] = _now()
        _atomic_write_json(checkpoint_path, checkpoint)

        try:
            result = ingest_item(item)
            state.update(result)
            state["status"] = "imported_ok"
            state["completed_at"] = _now()
            state["updated_at"] = _now()
        except Exception as exc:
            state.update(
                {
                    "status": "data_error",
                    "message": str(exc),
                    "traceback": traceback.format_exc(),
                    "completed_at": _now(),
                    "updated_at": _now(),
                }
            )
        finally:
            checkpoint["current_item"] = None
            checkpoint["updated_at"] = _now()
            _atomic_write_json(checkpoint_path, checkpoint)
            item_report = dict(state)
            item_report["queue_id"] = queue_id
            item_report["checkpoint"] = checkpoint_path
            if item.get("report_path"):
                _atomic_write_json(item["report_path"], item_report)

    checkpoint["complete"] = True
    checkpoint["completed_at"] = _now()
    checkpoint["updated_at"] = _now()
    counts = {}
    for state in checkpoint.get("items", {}).values():
        status = state.get("status", "not_run")
        counts[status] = counts.get(status, 0) + 1
    report = {
        "schema_version": SCHEMA_VERSION,
        "status": "complete",
        "manifest": manifest_path,
        "checkpoint": checkpoint_path,
        "completed_at": checkpoint["completed_at"],
        "counts": counts,
        "items": checkpoint.get("items", {}),
    }
    _atomic_write_json(checkpoint_path, checkpoint)
    _atomic_write_json(report_path, report)
    return report


def main():
    manifest_path = os.environ.get("SK_BATCH_MANIFEST_PATH")
    if not manifest_path:
        raise RuntimeError("SK_BATCH_MANIFEST_PATH is not set")
    run_manifest(
        manifest_path,
        checkpoint_path=os.environ.get("SK_BATCH_CHECKPOINT_PATH"),
        report_path=os.environ.get("SK_BATCH_REPORT_PATH"),
    )


if __name__ == "__main__":
    main()
