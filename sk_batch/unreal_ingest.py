"""Shared Unreal-side ingest for SK Batch RPC and headless transports.

This module is imported inside Unreal's embedded Python.  It deliberately uses
Send2UE's own ``dependencies/unreal.py`` importer so the serialized property
data drives the same FBX, skeleton, LOD, socket, and vertex-color rules as the
interactive add-on.  SpeedTree-only post-processing disables PhysicsAssets,
per-poly collision, and ray-tracing geometry while enforcing Nanite Voxelize.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import traceback
from copy import deepcopy
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

import unreal

MODULE_DIR = str(Path(__file__).resolve().parent)
if MODULE_DIR not in sys.path:
    sys.path.insert(0, MODULE_DIR)

from cluster_assembly_builder import (  # noqa: E402
    build_unreal_nanite_assembly,
    file_fingerprint,
    validate_file_fingerprint,
    validate_manifest_artifacts,
)
from nanite_assembly_materials import (  # noqa: E402
    audit_unreal_skeletal_mesh_material_sections,
)


SCHEMA_VERSION = 1
TERMINAL_STATES = {"imported_ok", "data_error", "manual_required", "not_run"}
_SEND2UE_UNREAL_MODULES = {}
PLACEHOLDER_SKELETON_NAME = "SK_PlaceholderCube_Skeleton"


def _now():
    return datetime.now().isoformat(timespec="seconds")


def _is_headless_manifest_runtime():
    """Return whether this module is running as the batch commandlet script."""
    return bool(os.environ.get("SK_BATCH_MANIFEST_PATH"))


def _defer_headless_runtime_validation(assembly, recovered_from_pending=False):
    """Record that GPU frame validation must run later in a live editor/RPC."""
    previous = assembly.get("runtime") if recovered_from_pending else None
    assembly["status"] = "ok"
    assembly["runtime"] = {
        "success": True,
        "status": "headless_deferred",
        "render_frame_validation_performed": False,
        "reason": (
            "GPU render-frame validation requires a live editor/RPC; "
            "headless commandlet import contracts passed"
        ),
        "recovered_from_pending": bool(recovered_from_pending),
    }
    if isinstance(previous, dict):
        assembly["runtime"]["previous_begin"] = previous
    return assembly


def _prepare_assembly_runtime_validation(assembly):
    if assembly.get("status") != "ready_for_runtime":
        return "imported_ok"
    if _is_headless_manifest_runtime():
        _defer_headless_runtime_validation(assembly)
        return "imported_ok"
    assembly_path = (assembly.get("build") or {}).get("assembly")
    runtime = _begin_instanced_dynamic_wind_runtime(assembly_path)
    assembly["runtime"] = runtime
    assembly["status"] = "runtime_pending"
    return "runtime_pending"


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


@contextmanager
def _without_generated_physics_assets(send2ue_unreal):
    """Disable Send2UE PhysicsAsset generation for this SpeedTree import only."""
    importer_class = getattr(send2ue_unreal, "UnrealImportAsset", None)
    original = getattr(importer_class, "set_physics_asset", None)
    if importer_class is None or not callable(original):
        yield False
        return

    def disable_physics_asset(self):
        options = getattr(self, "_options", None)
        if options is None:
            raise RuntimeError("Send2UE import options are not initialized")
        options.create_physics_asset = False
        options.physics_asset = None

    importer_class.set_physics_asset = disable_physics_asset
    try:
        yield True
    finally:
        importer_class.set_physics_asset = original


def _asset_package_path(asset):
    if asset is None:
        return ""
    try:
        return str(asset.get_path_name()).split(".", 1)[0]
    except Exception:
        return ""


def _manifest_asset_paths(item):
    paths = []
    seen = set()
    for manifest_asset in item.get("assets") or []:
        asset_data = manifest_asset.get("asset_data") or {}
        if asset_data.get("skip"):
            continue
        path = str(asset_data.get("asset_path") or "").split(".", 1)[0]
        if path and path not in seen:
            seen.add(path)
            paths.append(path)
    return paths


def _verify_manifest_assets_exist(item):
    """Validate a local import receipt against the active Unreal project."""
    paths = _manifest_asset_paths(item)
    missing = [
        path
        for path in paths
        if not unreal.EditorAssetLibrary.does_asset_exist(path)
    ]
    return {
        "status": (
            "current"
            if paths and not missing
            else "missing"
            if missing
            else "no_assets"
        ),
        "complete": bool(paths) and not missing,
        "asset_paths": paths,
        "missing_asset_paths": missing,
    }


def _manifest_items_dependency_order(items):
    """Return a stable topological order from explicit queue dependencies."""
    items = list(items)
    by_id = {}
    order = []
    for item in items:
        queue_id = str(item["queue_id"])
        if queue_id in by_id:
            raise RuntimeError(f"duplicate manifest queue_id: {queue_id}")
        by_id[queue_id] = item
        order.append(queue_id)

    remaining = list(order)
    result = []
    completed = set()
    while remaining:
        progressed = False
        for queue_id in list(remaining):
            dependencies = {
                str(value)
                for value in (
                    by_id[queue_id].get("depends_on_queue_ids") or []
                )
                if str(value) in by_id
            }
            if not dependencies.issubset(completed):
                continue
            result.append(by_id[queue_id])
            completed.add(queue_id)
            remaining.remove(queue_id)
            progressed = True
        if not progressed:
            raise RuntimeError(
                "manifest dependency cycle: " + ", ".join(remaining)
            )
    return result


def _dependency_block_message(item, checkpoint):
    missing = []
    failed = []
    states = checkpoint.get("items") or {}
    for value in item.get("depends_on_queue_ids") or []:
        dependency = str(value)
        state = states.get(dependency)
        if state is None:
            missing.append(dependency)
            continue
        status = str(state.get("status") or "not_run")
        if status != "imported_ok":
            failed.append(f"{dependency} ({status})")
    details = []
    if missing:
        details.append("missing provider: " + ", ".join(missing))
    if failed:
        details.append("provider did not complete: " + ", ".join(failed))
    if not details:
        return ""
    return "required Cluster Push dependency unavailable; " + "; ".join(details)


def _nanite_shape_preservation_voxelize():
    for enum_name in ("NaniteShapePreservation", "ENaniteShapePreservation"):
        enum_type = getattr(unreal, enum_name, None)
        if enum_type is None:
            continue
        for value_name in ("VOXELIZE", "Voxelize", "voxelize"):
            value = getattr(enum_type, value_name, None)
            if value is not None:
                return value
        for value_name in dir(enum_type):
            if "voxel" in value_name.casefold():
                value = getattr(enum_type, value_name, None)
                if value is not None:
                    return value
    return None


def _notify_nanite_settings_changed(mesh):
    for method_name in ("notify_nanite_settings_changed", "post_edit_change"):
        method = getattr(mesh, method_name, None)
        if callable(method):
            method()
            return


def _physics_asset_referencers(asset_path):
    finder = getattr(
        unreal.EditorAssetLibrary,
        "find_package_referencers_for_asset",
        None,
    )
    if not callable(finder):
        return None
    try:
        referencers = finder(asset_path, True)
    except TypeError:
        referencers = finder(asset_path)
    return sorted(
        {
            str(path).split(".", 1)[0]
            for path in (referencers or [])
            if path
        }
    )


def _default_physics_asset_preexisting(mesh_path):
    return unreal.EditorAssetLibrary.does_asset_exist(
        f"{mesh_path}_PhysicsAsset"
    )


def _prepare_speedtree_skeletal_optimization(
    mesh_path,
    default_physics_asset_preexisting=False,
):
    """Apply the asset-level settings shared by every SpeedTree placement."""
    mesh = unreal.EditorAssetLibrary.load_asset(mesh_path)
    skeletal_mesh_class = getattr(unreal, "SkeletalMesh", None)
    if mesh is None:
        raise RuntimeError(f"SpeedTree skeletal mesh not found: {mesh_path}")
    if skeletal_mesh_class is None or not isinstance(mesh, skeletal_mesh_class):
        return {
            "status": "skipped",
            "reason": "asset is not a SkeletalMesh",
            "mesh": mesh_path,
            "_delete_physics_asset_path": "",
        }

    modify = getattr(mesh, "modify", None)
    if callable(modify):
        modify()

    changed = []
    if bool(mesh.get_editor_property("support_ray_tracing")):
        mesh.set_editor_property("support_ray_tracing", False)
        changed.append("support_ray_tracing")

    if bool(mesh.get_editor_property("enable_per_poly_collision")):
        mesh.set_editor_property("enable_per_poly_collision", False)
        changed.append("enable_per_poly_collision")

    physics_asset = mesh.get_editor_property("physics_asset")
    physics_asset_before = _asset_package_path(physics_asset)
    if physics_asset is not None:
        mesh.set_editor_property("physics_asset", None)
        changed.append("physics_asset")

    nanite = mesh.get_editor_property("nanite_settings")
    nanite_changed = False
    if not bool(nanite.get_editor_property("enabled")):
        nanite.set_editor_property("enabled", True)
        nanite_changed = True

    voxelize = _nanite_shape_preservation_voxelize()
    if voxelize is None:
        raise RuntimeError("UE 5.8 Nanite Shape Preservation Voxelize enum missing")
    if nanite.get_editor_property("shape_preservation") != voxelize:
        nanite.set_editor_property("shape_preservation", voxelize)
        nanite_changed = True
    if nanite_changed:
        mesh.set_editor_property("nanite_settings", nanite)
        _notify_nanite_settings_changed(mesh)
        changed.append("nanite_settings")

    default_physics_asset_path = f"{mesh_path}_PhysicsAsset"
    default_exists = unreal.EditorAssetLibrary.does_asset_exist(
        default_physics_asset_path
    )
    referencers = None
    foreign_referencers = []
    delete_physics_asset_path = ""
    if default_exists and (
        physics_asset_before == default_physics_asset_path
        or not default_physics_asset_preexisting
    ):
        referencers = _physics_asset_referencers(default_physics_asset_path)
        if referencers is not None:
            foreign_referencers = [
                path for path in referencers if path != mesh_path
            ]
        if not default_physics_asset_preexisting or (
            referencers is not None and not foreign_referencers
        ):
            delete_physics_asset_path = default_physics_asset_path

    support_ray_tracing = bool(
        mesh.get_editor_property("support_ray_tracing")
    )
    per_poly_collision = bool(
        mesh.get_editor_property("enable_per_poly_collision")
    )
    physics_asset_after = _asset_package_path(
        mesh.get_editor_property("physics_asset")
    )
    nanite_after = mesh.get_editor_property("nanite_settings")
    nanite_enabled = bool(nanite_after.get_editor_property("enabled"))
    shape_preservation = nanite_after.get_editor_property("shape_preservation")
    failures = []
    if support_ray_tracing:
        failures.append("Support Ray Tracing is still enabled")
    if per_poly_collision:
        failures.append("per-poly collision is still enabled")
    if physics_asset_after:
        failures.append(f"PhysicsAsset is still assigned: {physics_asset_after}")
    if not nanite_enabled:
        failures.append("Nanite is still disabled")
    if shape_preservation != voxelize:
        failures.append("Nanite Shape Preservation is not Voxelize")
    if failures:
        raise RuntimeError(
            "SpeedTree skeletal optimization failed: " + "; ".join(failures)
        )

    return {
        "status": "ok",
        "mesh": mesh_path,
        "changed": changed,
        "support_ray_tracing": support_ray_tracing,
        "enable_per_poly_collision": per_poly_collision,
        "physics_asset_before": physics_asset_before,
        "physics_asset_after": physics_asset_after,
        "nanite_enabled": nanite_enabled,
        "nanite_shape_preservation": str(shape_preservation),
        "default_physics_asset": default_physics_asset_path,
        "default_physics_asset_preexisting": bool(
            default_physics_asset_preexisting
        ),
        "default_physics_asset_referencers": referencers,
        "default_physics_asset_foreign_referencers": foreign_referencers,
        "physics_asset_deleted": False,
        "_delete_physics_asset_path": delete_physics_asset_path,
    }


def _finalize_speedtree_skeletal_optimization(optimization):
    delete_path = optimization.pop("_delete_physics_asset_path", "")
    if not delete_path:
        return optimization
    if not unreal.EditorAssetLibrary.does_asset_exist(delete_path):
        return optimization
    if not unreal.EditorAssetLibrary.delete_asset(delete_path):
        raise RuntimeError(
            f"generated SpeedTree PhysicsAsset delete failed: {delete_path}"
        )
    optimization["physics_asset_deleted"] = True
    return optimization


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
    slot_normalization = _normalize_existing_skeletal_mesh_imported_slot_names(
        asset_data.get("asset_path")
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
        "pre_import_material_slot_normalization": slot_normalization,
    }


def _parse_codex_material_tool_result(raw):
    values = raw if isinstance(raw, tuple) else (raw,)
    payload = next(
        (
            value
            for value in values
            if isinstance(value, str) and value.lstrip().startswith("{")
        ),
        "{}",
    )
    errors = next((value for value in values if isinstance(value, list)), [])
    result = json.loads(payload)
    result["returned_errors"] = [str(error) for error in errors]
    return result


def _normalize_existing_skeletal_mesh_imported_slot_names(asset_path):
    """Make existing SpeedTree slots match FBX names before a reimport.

    A legacy slot with ``ImportedMaterialSlotName=None`` is not matched by the
    FBX importer.  UE then appends duplicate slots and points new sections at
    those unassigned duplicates.  Normalize only missing imported names; an
    explicit non-empty imported name is preserved as user/importer intent.
    """

    path = str(asset_path or "").split(".", 1)[0]
    if not path:
        return {"status": "skipped", "reason": "no asset path", "changes": []}
    mesh = unreal.EditorAssetLibrary.load_asset(path)
    if mesh is None:
        return {"status": "fresh", "asset": path, "changes": []}
    if not isinstance(mesh, unreal.SkeletalMesh):
        return {"status": "skipped", "asset": path, "reason": "not skeletal", "changes": []}

    slots = list(mesh.get_editor_property("materials") or [])
    missing = []
    for index, slot in enumerate(slots):
        imported_name = str(slot.get_editor_property("imported_material_slot_name"))
        if imported_name and imported_name.casefold() != "none":
            continue
        slot_name = str(slot.get_editor_property("material_slot_name"))
        material = slot.get_editor_property("material_interface")
        material_path = material.get_path_name() if material else ""
        if not slot_name or slot_name.casefold() == "none" or not material_path:
            raise RuntimeError(
                "cannot normalize a preexisting skeletal material slot before reimport: "
                f"asset={path}, index={index}, slot={slot_name!r}, material={material_path!r}"
            )
        missing.append((index, slot_name, material_path))
    if not missing:
        return {"status": "canonical", "asset": path, "changes": []}

    library = getattr(unreal, "CodexMaterialToolsLibrary", None)
    normalize = getattr(library, "normalize_skeletal_mesh_material_slot", None)
    if not callable(normalize):
        raise RuntimeError(
            "CodexMaterialToolsLibrary is required to normalize imported material slot names"
        )
    changes = []
    for index, slot_name, material_path in missing:
        result = _parse_codex_material_tool_result(
            normalize(
                path,
                index,
                unreal.Name("None"),
                unreal.Name(slot_name),
                material_path,
                True,
            )
        )
        if result.get("returned_errors") or not result.get("desired_is_set"):
            raise RuntimeError(
                f"pre-import material slot normalization failed: {result}"
            )
        changes.append(result)
    return {"status": "normalized", "asset": path, "changes": changes}


def _clear_placeholder_skeleton_before_import(item):
    """Force a fresh, dedicated ``<mesh>_Skeleton`` on reimport.

    The migration seeded every not-yet-converted slot with a dummy that shares a
    single ``SK_PlaceholderCube_Skeleton``.  Reimporting the real FBX *in place*
    keeps whatever skeleton the existing asset already references, so the real
    mesh silently inherits that shared placeholder skeleton.  DynamicWind batches
    instanced skinning per skeleton, so meshes with different bone counts sharing
    one skeleton assert-crash the renderer the instant a wind provider attaches.

    Deleting only the placeholder *mesh* (never the shared skeleton, which the
    other dummies still reference) makes the subsequent Send2UE import a fresh
    import, which creates a new dedicated skeleton the same way cleanly-imported
    siblings got theirs.  A mesh that already has its own skeleton, or a slot with
    nothing imported yet, is left untouched.
    """
    mesh_path = item.get("mesh_path")
    if not mesh_path:
        return {"status": "skipped", "reason": "item has no mesh_path"}
    asset_path = mesh_path.split(".")[0]
    if not unreal.EditorAssetLibrary.does_asset_exist(asset_path):
        return {"status": "fresh"}
    mesh = unreal.EditorAssetLibrary.load_asset(asset_path)
    skeleton = mesh.get_editor_property("skeleton") if mesh else None
    if skeleton is None or skeleton.get_name() != PLACEHOLDER_SKELETON_NAME:
        return {
            "status": "ok",
            "skeleton": skeleton.get_path_name() if skeleton else None,
        }
    if not unreal.EditorAssetLibrary.delete_asset(asset_path):
        raise RuntimeError(
            f"failed to delete placeholder mesh for fresh skeleton: {asset_path}"
        )
    return {
        "status": "cleared_placeholder",
        "deleted": asset_path,
        "shared_skeleton": skeleton.get_path_name(),
    }


def _apply_dynamic_wind(item):
    policy = item.get("wind_policy") or {}
    wind_json = item.get("wind_json")
    if not wind_json:
        if policy.get("requires_json"):
            raise RuntimeError(
                "manifest requires final-skeleton dynamic wind JSON but has no path"
            )
        return {
            "status": "skipped",
            "reason": policy.get("reason") or "manifest has no wind JSON",
            "policy": policy,
        }
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
    try:
        payload = json.loads(str(result))
    except (TypeError, ValueError) as exc:
        raise RuntimeError(
            f"dynamic wind import returned invalid result: {result!r}"
        ) from exc
    if not isinstance(payload, dict) or not payload.get("success"):
        error = payload.get("error") if isinstance(payload, dict) else None
        raise RuntimeError(
            "dynamic wind final-skeleton contract failed: "
            + str(error or result)
        )
    if payload.get("skeleton_contract") != "final_skeleton_v2":
        raise RuntimeError(
            "dynamic wind importer did not confirm final_skeleton_v2 contract"
        )
    if not payload.get("skeleton_hash"):
        raise RuntimeError("dynamic wind importer returned no skeleton hash")
    return {
        "status": "ok",
        "mesh": mesh_path,
        "wind_json": file_fingerprint(wind_json),
        "result": payload,
    }


def _save_final_skeleton_contract_assets(full_mesh):
    """Persist the Full mesh, generated USkeleton, and imported wind together.

    Send2UE creates the dedicated Skeleton as a separate package. Saving only the
    mesh can leave a valid in-session reference whose Skeleton reloads empty after
    an editor restart, so Assembly import and runtime probing must not begin until
    both packages have been written.
    """
    if full_mesh is None:
        raise RuntimeError("cannot save a missing Full SK final mesh")
    skeleton = full_mesh.get_editor_property("skeleton")
    if skeleton is None:
        raise RuntimeError("cannot save Full SK without its final Skeleton")
    # Save the referenced dependency first, then the referring mesh package.
    asset_paths = [skeleton.get_path_name(), full_mesh.get_path_name()]
    saved = []
    for object_path in asset_paths:
        asset_path = str(object_path).split(".")[0]
        if not unreal.EditorAssetLibrary.save_asset(
            asset_path,
            only_if_is_dirty=False,
        ):
            raise RuntimeError(
                f"failed to persist final Skeleton contract asset: {asset_path}"
            )
        saved.append(asset_path)
    return {
        "mesh": saved[1],
        "skeleton": saved[0],
        "saved": saved,
    }


def _clear_generated_mesh_with_mismatched_skeleton(asset_path, expected_skeleton):
    """Make generated Assembly meshes fresh-import when reimport kept stale Skeletons.

    Send2UE/FBX reimport may retain the Skeleton already assigned to an existing
    skeletal mesh even when ``unreal_skeleton_asset_path`` names the new Full SK
    Skeleton. These meshes are generated Assembly-owned outputs, so delete only
    the mismatched mesh package and leave every Skeleton package intact.
    """
    candidate = str(asset_path or "").split(".")[0]
    if not candidate or not unreal.EditorAssetLibrary.does_asset_exist(candidate):
        return {"status": "fresh", "asset": candidate}
    mesh = unreal.EditorAssetLibrary.load_asset(candidate)
    current_skeleton = (
        mesh.get_editor_property("skeleton")
        if isinstance(mesh, unreal.SkeletalMesh)
        else None
    )
    expected_path = expected_skeleton.get_path_name() if expected_skeleton else ""
    current_path = current_skeleton.get_path_name() if current_skeleton else ""
    if current_path == expected_path:
        return {
            "status": "matched",
            "asset": candidate,
            "skeleton": current_path,
        }
    if not unreal.EditorAssetLibrary.delete_asset(candidate):
        raise RuntimeError(
            "failed to replace generated Assembly mesh with stale Skeleton: "
            + candidate
        )
    return {
        "status": "cleared_mismatch",
        "asset": candidate,
        "previous_skeleton": current_path or None,
        "expected_skeleton": expected_path,
    }


def _editor_world():
    if hasattr(unreal, "UnrealEditorSubsystem") and hasattr(
        unreal, "get_editor_subsystem"
    ):
        subsystem = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem)
        if subsystem is not None:
            world = subsystem.get_editor_world()
            if world is not None:
                return world
    if hasattr(unreal, "EditorLevelLibrary"):
        return unreal.EditorLevelLibrary.get_editor_world()
    return None


def _validate_instanced_dynamic_wind_runtime(mesh_path):
    mesh = unreal.EditorAssetLibrary.load_asset(mesh_path)
    if not isinstance(mesh, unreal.SkeletalMesh):
        raise RuntimeError(f"runtime DynamicWind mesh is missing: {mesh_path}")
    library = getattr(unreal, "CodexDynamicWindDebugLibrary", None)
    method = getattr(
        library,
        "run_instanced_dynamic_wind_runtime_probe",
        None,
    )
    if not callable(method):
        raise RuntimeError(
            "latest CodexDynamicWind runtime probe is unavailable; "
            "rebuild and restart the editor"
        )
    world = _editor_world()
    if world is None:
        raise RuntimeError("no editor world for transient DynamicWind runtime probe")
    raw = method(world, mesh)
    try:
        payload = json.loads(str(raw))
    except (TypeError, ValueError) as exc:
        raise RuntimeError(
            f"DynamicWind runtime probe returned invalid JSON: {raw!r}"
        ) from exc
    if not isinstance(payload, dict) or not payload.get("success"):
        raise RuntimeError(
            "UInstancedSkinnedMeshComponent + UDynamicWindData runtime probe failed: "
            + str(payload.get("error") if isinstance(payload, dict) else raw)
        )
    return payload


def _parse_dynamic_wind_probe_result(raw, label):
    try:
        payload = json.loads(str(raw))
    except (TypeError, ValueError) as exc:
        raise RuntimeError(
            f"DynamicWind {label} probe returned invalid JSON: {raw!r}"
        ) from exc
    if not isinstance(payload, dict) or not payload.get("success"):
        raise RuntimeError(
            f"DynamicWind {label} probe failed: "
            + (
                json.dumps(payload, ensure_ascii=False, sort_keys=True)
                if isinstance(payload, dict)
                else str(raw)
            )
        )
    return payload


def _begin_instanced_dynamic_wind_runtime(mesh_path):
    mesh = unreal.EditorAssetLibrary.load_asset(mesh_path)
    if not isinstance(mesh, unreal.SkeletalMesh):
        raise RuntimeError(f"runtime DynamicWind mesh is missing: {mesh_path}")
    library = getattr(unreal, "CodexDynamicWindDebugLibrary", None)
    method = getattr(
        library,
        "begin_instanced_dynamic_wind_runtime_probe",
        None,
    )
    if not callable(method):
        raise RuntimeError(
            "latest two-phase CodexDynamicWind runtime probe is unavailable; "
            "rebuild and restart the editor"
        )
    world = _editor_world()
    if world is None:
        raise RuntimeError("no editor world for transient DynamicWind runtime probe")
    payload = _parse_dynamic_wind_probe_result(method(world, mesh), "begin")
    if payload.get("status") != "pending" or not payload.get("probe_token"):
        raise RuntimeError("DynamicWind begin probe did not return a pending token")
    return payload


def _finish_instanced_dynamic_wind_runtime(probe_token):
    library = getattr(unreal, "CodexDynamicWindDebugLibrary", None)
    method = getattr(
        library,
        "finish_instanced_dynamic_wind_runtime_probe",
        None,
    )
    if not callable(method):
        raise RuntimeError("latest two-phase DynamicWind finish probe is unavailable")
    world = _editor_world()
    if world is None:
        raise RuntimeError("no editor world for DynamicWind finish probe")
    payload = _parse_dynamic_wind_probe_result(
        method(world, str(probe_token)),
        "finish",
    )
    if payload.get("status") not in {"pending", "passed"}:
        raise RuntimeError("DynamicWind finish probe returned an invalid status")
    return payload


def _cancel_instanced_dynamic_wind_runtime(probe_token):
    library = getattr(unreal, "CodexDynamicWindDebugLibrary", None)
    method = getattr(
        library,
        "cancel_instanced_dynamic_wind_runtime_probe",
        None,
    )
    if callable(method):
        return _parse_dynamic_wind_probe_result(
            method(str(probe_token or "")),
            "cancel",
        )
    return {"success": False, "cancelled": False, "unavailable": True}


def _best_effort_cancel_instanced_dynamic_wind_runtime(probe_token):
    """Never let a cleanup failure hide the original runtime failure."""
    try:
        result = _cancel_instanced_dynamic_wind_runtime(probe_token)
        return {
            "success": bool(result.get("success")),
            "cleanup_confirmed": bool(result.get("success")),
            "result": result,
            "cancel_error": None,
        }
    except Exception as exc:
        return {
            "success": False,
            "cleanup_confirmed": False,
            "result": None,
            "cancel_error": str(exc),
            "cancel_traceback": traceback.format_exc(),
        }


def _ingest_cluster_assembly(send2ue_unreal, item, full_wind):
    payload = item.get("cluster_assembly")
    if not payload:
        return {"status": "skipped", "reason": "no content-driven Assembly manifest"}
    plan = payload.get("ingest_plan") or {}
    if plan.get("status") == "pass_through":
        return {"status": "pass_through", "assets": []}
    if plan.get("status") != "ready":
        raise RuntimeError("Cluster Assembly ingest plan is not ready")
    if (full_wind or {}).get("status") != "ok":
        raise RuntimeError(
            "Cluster Assembly requires successful Full SK final-skeleton wind import"
        )
    manifest = payload.get("manifest") or {}
    validate_manifest_artifacts(manifest)
    expected_wind = (manifest.get("wind_contract") or {}).get("wind_json") or {}
    full_wind_file = (full_wind or {}).get("wind_json") or {}
    if (
        int(full_wind_file.get("size") or -1)
        != int(expected_wind.get("size") or -2)
        or str(full_wind_file.get("sha256") or "").casefold()
        != str(expected_wind.get("sha256") or "").casefold()
    ):
        raise RuntimeError(
            "Full SK and Assembly did not consume the same final Skeleton wind JSON"
        )
    expected_skeleton_hash = str(
        (manifest.get("final_skeleton") or {}).get(
            "bone_name_index_parent_sha1"
        )
        or ""
    ).casefold()
    full_skeleton_hash = str(
        ((full_wind or {}).get("result") or {}).get("skeleton_hash") or ""
    ).casefold()
    if not expected_skeleton_hash or full_skeleton_hash != expected_skeleton_hash:
        raise RuntimeError(
            "Full SK DynamicWind Skeleton hash does not match the BWR final Skeleton"
        )
    asset_contract = plan.get("asset_contract") or {}
    full_mesh = unreal.EditorAssetLibrary.load_asset(
        asset_contract.get("full_skeletal_mesh")
    )
    if full_mesh is None:
        raise RuntimeError("Cluster Assembly Full SK asset is missing after import")
    full_skeleton = full_mesh.get_editor_property("skeleton")
    if full_skeleton is None:
        raise RuntimeError("Cluster Assembly Full SK has no final Skeleton")
    full_skeleton_path = full_skeleton.get_path_name()
    persisted_final_contract = _save_final_skeleton_contract_assets(full_mesh)

    generated_assets = []
    optimizations = []
    skeleton_reimports = []
    with _without_generated_physics_assets(send2ue_unreal):
        for source in plan.get("assets") or []:
            manifest_asset = deepcopy(source)
            data = manifest_asset.get("asset_data") or {}
            validate_file_fingerprint(
                data.get("_material_pipeline_json_fingerprint"),
                "generated Assembly material sidecar",
            )
            uses_full_final_skeleton = (
                data.get("skeleton_asset_path") == "__FULL_FINAL_SKELETON__"
            )
            if uses_full_final_skeleton:
                data["skeleton_asset_path"] = full_skeleton_path
                property_data = manifest_asset.get("property_data") or {}
                skeleton_setting = property_data.get("unreal_skeleton_asset_path")
                if isinstance(skeleton_setting, dict):
                    skeleton_setting["value"] = full_skeleton_path
            asset_path = data.get("asset_path")
            if uses_full_final_skeleton:
                skeleton_reimports.append(
                    _clear_generated_mesh_with_mismatched_skeleton(
                        asset_path,
                        full_skeleton,
                    )
                )
            else:
                skeleton_reimports.append(
                    {
                        "status": "dedicated_part_skeleton_preserved",
                        "asset": asset_path,
                    }
                )
            preexisting = _default_physics_asset_preexisting(asset_path)
            imported = _import_manifest_asset(send2ue_unreal, manifest_asset)
            generated_assets.append(imported)
            optimization = _prepare_speedtree_skeletal_optimization(
                asset_path,
                preexisting,
            )
            optimization["physics_asset_generation_disabled"] = True
            optimizations.append(optimization)

    for optimization in optimizations:
        _finalize_speedtree_skeletal_optimization(optimization)
    persisted_generated_assets = []
    for imported in generated_assets:
        asset_path = imported.get("asset_path") if isinstance(imported, dict) else None
        if asset_path:
            if not unreal.EditorAssetLibrary.save_asset(
                asset_path,
                only_if_is_dirty=False,
            ):
                raise RuntimeError(
                    f"failed to persist Assembly prototype before build: {asset_path}"
                )
            persisted_generated_assets.append(asset_path)
    result = build_unreal_nanite_assembly(unreal, manifest, asset_contract)
    assembly_path = result.get("assembly")
    if assembly_path and not unreal.EditorAssetLibrary.save_asset(
        assembly_path,
        only_if_is_dirty=False,
    ):
        raise RuntimeError(
            f"failed to persist Cluster Assembly before runtime probe: {assembly_path}"
        )
    materials = (
        _material_compile_and_slot_validation(assembly_path)
        if assembly_path
        else None
    )
    return {
        "status": "ready_for_runtime",
        "assets": generated_assets,
        "optimizations": optimizations,
        "persisted_generated_assets": persisted_generated_assets,
        "skeleton_reimports": skeleton_reimports,
        "build": result,
        "materials": materials,
        "full_final_skeleton": full_skeleton_path,
        "persisted_final_contract": persisted_final_contract,
    }


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
    section_validation = audit_unreal_skeletal_mesh_material_sections(
        unreal,
        mesh_path,
        len(slots),
    )
    return {
        "mesh": mesh_path,
        "slots": details,
        "compiled_base_materials": sorted(compiled_base_materials),
        "section_material_validation": section_validation,
    }


def _save_item_assets(item, imported_assets):
    asset_paths = []
    seen_paths = set()
    for value in imported_assets:
        asset_path = value.get("asset_path") if isinstance(value, dict) else None
        if asset_path and asset_path not in seen_paths:
            seen_paths.add(asset_path)
            asset_paths.append(asset_path)
    mesh_path = item.get("mesh_path")
    if mesh_path and mesh_path not in seen_paths:
        seen_paths.add(mesh_path)
        asset_paths.append(mesh_path)

    saved = []
    for asset_path in asset_paths:
        if asset_path and unreal.EditorAssetLibrary.does_asset_exist(asset_path):
            if not unreal.EditorAssetLibrary.save_asset(
                asset_path,
                only_if_is_dirty=False,
            ):
                raise RuntimeError(f"failed to save imported asset: {asset_path}")
            saved.append(asset_path)
    folder = item.get("unreal_folder")
    if folder and not unreal.EditorAssetLibrary.save_directory(
        folder,
        only_if_is_dirty=True,
    ):
        raise RuntimeError(f"failed to save imported asset directory: {folder}")
    return saved


def ingest_item(item):
    send2ue_unreal = _load_send2ue_unreal(item["send2ue_unreal_py"])
    skeleton = _clear_placeholder_skeleton_before_import(item)
    checkout = _checkout_existing_assets(item)
    mesh_path = item["mesh_path"]
    default_physics_asset_preexisting = _default_physics_asset_preexisting(
        mesh_path
    )
    with _without_generated_physics_assets(send2ue_unreal) as generation_disabled:
        imported_assets = [
            _import_manifest_asset(send2ue_unreal, manifest_asset)
            for manifest_asset in item.get("assets") or []
        ]
    optimization = _prepare_speedtree_skeletal_optimization(
        mesh_path,
        default_physics_asset_preexisting,
    )
    optimization["physics_asset_generation_disabled"] = generation_disabled
    material_checkouts = _material_pipeline_checkouts()
    checkout["material_pipeline"] = material_checkouts
    checkout["checked_out"] = list(
        dict.fromkeys(list(checkout["checked_out"]) + material_checkouts)
    )
    wind = _apply_dynamic_wind(item)
    assembly = _ingest_cluster_assembly(send2ue_unreal, item, wind)
    imported_assets.extend(assembly.get("assets") or [])
    saved = _save_item_assets(item, imported_assets)
    optimization = _finalize_speedtree_skeletal_optimization(optimization)
    materials = _material_compile_and_slot_validation(mesh_path)
    item_status = _prepare_assembly_runtime_validation(assembly)
    return {
        "status": item_status,
        "checkout": checkout,
        "assets": imported_assets,
        "skeleton": skeleton,
        "wind": wind,
        "cluster_assembly": assembly,
        "materials": materials,
        "optimization": optimization,
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
    manifest_items = _manifest_items_dependency_order(
        manifest.get("items") or []
    )
    _recover_interrupted_item(checkpoint, manifest_items, max_retries)
    _atomic_write_json(checkpoint_path, checkpoint)

    for item in manifest_items:
        queue_id = str(item["queue_id"])
        fingerprint = item["fingerprint"]
        previous = checkpoint.setdefault("items", {}).get(queue_id, {})
        if (
            previous.get("fingerprint") == fingerprint
            and previous.get("status") == "runtime_pending"
            and _is_headless_manifest_runtime()
        ):
            assembly = previous.get("cluster_assembly") or {}
            _defer_headless_runtime_validation(
                assembly,
                recovered_from_pending=True,
            )
            previous["cluster_assembly"] = assembly
            previous["status"] = "imported_ok"
            previous["completed_at"] = _now()
            previous["updated_at"] = _now()
            checkpoint["current_item"] = None
            checkpoint["updated_at"] = _now()
            _atomic_write_json(checkpoint_path, checkpoint)
            if previous.get("report"):
                item_report = dict(previous)
                item_report["queue_id"] = queue_id
                item_report["checkpoint"] = checkpoint_path
                _atomic_write_json(previous["report"], item_report)
            continue
        dependency_message = _dependency_block_message(item, checkpoint)
        if dependency_message:
            state = {
                "status": "not_run",
                "fingerprint": fingerprint,
                "crash_count": int(previous.get("crash_count", 0)),
                "message": dependency_message,
                "completed_at": _now(),
                "updated_at": _now(),
                "manifest": manifest_path,
                "report": item.get("report_path"),
            }
            checkpoint["items"][queue_id] = state
            checkpoint["current_item"] = None
            checkpoint["updated_at"] = _now()
            _atomic_write_json(checkpoint_path, checkpoint)
            if item.get("report_path"):
                item_report = dict(state)
                item_report["queue_id"] = queue_id
                item_report["checkpoint"] = checkpoint_path
                _atomic_write_json(item["report_path"], item_report)
            continue
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

        result = None
        try:
            asset_cache = None
            if item.get("verify_existing_assets"):
                asset_cache = _verify_manifest_assets_exist(item)
            if asset_cache and asset_cache["complete"]:
                result = {
                    "status": "imported_ok",
                    "asset_cache": asset_cache,
                }
            else:
                result = ingest_item(item)
                if asset_cache is not None:
                    result["asset_cache_preflight"] = asset_cache
            state.update(result)
            state["status"] = result.get("status", "imported_ok")
            if state["status"] == "imported_ok":
                state["completed_at"] = _now()
            state["updated_at"] = _now()
        except Exception as exc:
            original_traceback = traceback.format_exc()
            assembly = (result or {}).get("cluster_assembly") or {}
            begin = assembly.get("runtime") or {}
            token = begin.get("probe_token")
            runtime_cancel = (
                _best_effort_cancel_instanced_dynamic_wind_runtime(token)
                if token
                else None
            )
            state.update(
                {
                    "status": "data_error",
                    "message": str(exc),
                    "traceback": original_traceback,
                    "runtime_cancel": (
                        (runtime_cancel or {}).get("result") or runtime_cancel
                    ),
                    "runtime_cancel_cleanup": runtime_cancel,
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

    checkpoint["complete"] = all(
        state.get("status") in TERMINAL_STATES
        for state in checkpoint.get("items", {}).values()
    )
    if checkpoint["complete"]:
        checkpoint["completed_at"] = _now()
    checkpoint["updated_at"] = _now()
    counts = {}
    for state in checkpoint.get("items", {}).values():
        status = state.get("status", "not_run")
        counts[status] = counts.get(status, 0) + 1
    report = {
        "schema_version": SCHEMA_VERSION,
        "status": "complete" if checkpoint["complete"] else "runtime_pending",
        "manifest": manifest_path,
        "checkpoint": checkpoint_path,
        "completed_at": checkpoint.get("completed_at"),
        "counts": counts,
        "items": checkpoint.get("items", {}),
    }
    _atomic_write_json(checkpoint_path, checkpoint)
    _atomic_write_json(report_path, report)
    return report


def finish_runtime_probe(checkpoint_path, report_path, queue_id):
    checkpoint_path = str(Path(checkpoint_path).resolve())
    report_path = str(Path(report_path).resolve())
    checkpoint = _load_json(checkpoint_path, default=None)
    if not checkpoint:
        raise RuntimeError("runtime checkpoint is missing")
    state = checkpoint.get("items", {}).get(str(queue_id))
    if not state:
        raise RuntimeError("runtime checkpoint item is missing")
    if state.get("status") in TERMINAL_STATES:
        return state
    if state.get("status") != "runtime_pending":
        raise RuntimeError(
            f"runtime checkpoint is not pending: {state.get('status')}"
        )

    assembly = state.get("cluster_assembly") or {}
    begin = assembly.get("runtime") or {}
    token = begin.get("probe_token")
    if not token:
        raise RuntimeError("runtime checkpoint has no probe token")
    try:
        finish = _finish_instanced_dynamic_wind_runtime(token)
        assembly["runtime_finish"] = finish
        state["cluster_assembly"] = assembly
        state["updated_at"] = _now()
        if finish.get("status") == "passed":
            assembly["status"] = "ok"
            assembly["runtime"] = {
                "status": "passed",
                "begin": begin,
                "finish": finish,
            }
            state["status"] = "imported_ok"
            state["completed_at"] = _now()
    except Exception as exc:
        original_traceback = traceback.format_exc()
        cancel = _best_effort_cancel_instanced_dynamic_wind_runtime(token)
        state.update(
            {
                "status": "data_error",
                "message": str(exc),
                "original_error": str(exc),
                "traceback": original_traceback,
                "runtime_cancel": cancel.get("result") or cancel,
                "runtime_cancel_cleanup": cancel,
                "cancel_error": cancel.get("cancel_error"),
                "cleanup_confirmed": cancel.get("cleanup_confirmed", False),
                "completed_at": _now(),
                "updated_at": _now(),
            }
        )

    checkpoint["updated_at"] = _now()
    checkpoint["complete"] = all(
        item.get("status") in TERMINAL_STATES
        for item in checkpoint.get("items", {}).values()
    )
    if checkpoint["complete"]:
        checkpoint["completed_at"] = _now()
    _atomic_write_json(checkpoint_path, checkpoint)

    if state.get("report"):
        item_report = dict(state)
        item_report["queue_id"] = str(queue_id)
        item_report["checkpoint"] = checkpoint_path
        _atomic_write_json(state["report"], item_report)
    counts = {}
    for item_state in checkpoint.get("items", {}).values():
        status = item_state.get("status", "not_run")
        counts[status] = counts.get(status, 0) + 1
    report = {
        "schema_version": SCHEMA_VERSION,
        "status": "complete" if checkpoint["complete"] else "runtime_pending",
        "manifest": checkpoint.get("manifest"),
        "checkpoint": checkpoint_path,
        "completed_at": checkpoint.get("completed_at"),
        "counts": counts,
        "items": checkpoint.get("items", {}),
    }
    _atomic_write_json(report_path, report)
    return state


def cancel_runtime_probe(checkpoint_path, report_path, queue_id, reason):
    checkpoint_path = str(Path(checkpoint_path).resolve())
    report_path = str(Path(report_path).resolve())
    checkpoint = _load_json(checkpoint_path, default=None)
    if not checkpoint:
        raise RuntimeError("runtime checkpoint is missing")
    state = checkpoint.get("items", {}).get(str(queue_id))
    if not state:
        raise RuntimeError("runtime checkpoint item is missing")
    if state.get("status") in TERMINAL_STATES:
        return state
    if state.get("status") != "runtime_pending":
        raise RuntimeError(
            f"runtime checkpoint is not pending: {state.get('status')}"
        )

    assembly = state.get("cluster_assembly") or {}
    begin = assembly.get("runtime") or {}
    token = begin.get("probe_token")
    if not token:
        raise RuntimeError("runtime checkpoint has no probe token")
    cancel = _best_effort_cancel_instanced_dynamic_wind_runtime(token)
    assembly["runtime_cancel"] = cancel.get("result") or cancel
    assembly["runtime_cancel_cleanup"] = cancel
    state.update(
        {
            "status": "data_error",
            "message": str(reason or "runtime probe cancelled"),
            "runtime_cancel": cancel.get("result") or cancel,
            "runtime_cancel_cleanup": cancel,
            "cancel_error": cancel.get("cancel_error"),
            "cleanup_confirmed": cancel.get("cleanup_confirmed", False),
            "cluster_assembly": assembly,
            "completed_at": _now(),
            "updated_at": _now(),
        }
    )

    checkpoint["updated_at"] = _now()
    checkpoint["complete"] = all(
        item.get("status") in TERMINAL_STATES
        for item in checkpoint.get("items", {}).values()
    )
    if checkpoint["complete"]:
        checkpoint["completed_at"] = _now()
    _atomic_write_json(checkpoint_path, checkpoint)

    if state.get("report"):
        item_report = dict(state)
        item_report["queue_id"] = str(queue_id)
        item_report["checkpoint"] = checkpoint_path
        _atomic_write_json(state["report"], item_report)
    counts = {}
    for item_state in checkpoint.get("items", {}).values():
        status = item_state.get("status", "not_run")
        counts[status] = counts.get(status, 0) + 1
    report = {
        "schema_version": SCHEMA_VERSION,
        "status": "complete" if checkpoint["complete"] else "runtime_pending",
        "manifest": checkpoint.get("manifest"),
        "checkpoint": checkpoint_path,
        "completed_at": checkpoint.get("completed_at"),
        "counts": counts,
        "items": checkpoint.get("items", {}),
    }
    _atomic_write_json(report_path, report)
    return state


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
