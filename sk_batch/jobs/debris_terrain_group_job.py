"""Build source-owned static debris groups, then reuse the existing Push job.

Run inside a disposable Blender process against the canonical .blend. Group
objects stay outside Export when saved. Export isolation is in memory only;
the existing Send2UE Push job owns FBX, material handoff and deferred ingest.
"""
from __future__ import annotations

import argparse
from array import array
import hashlib
import json
from pathlib import Path
import runpy
import sys
import time
import traceback

import bpy

JOB_DIR = Path(__file__).resolve().parent
REPO_DIR = JOB_DIR.parent.parent
if str(REPO_DIR) not in sys.path:
    sys.path.insert(0, str(REPO_DIR))

from blender_addon_gateway import prepare_runtime
from sk_batch.debris_prefab_manifest import build_prefab_manifest


def parse_args():
    argv = sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else []
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--group-evidence")
    parser.add_argument("--asset-suffix")
    parser.add_argument("--source-path")
    parser.add_argument("--max-groups", type=int, default=20)
    parser.add_argument("--target-radius-cm", type=float, default=30.)
    parser.add_argument("--target-height-span-cm", type=float, default=10.)
    parser.add_argument("--group-report", required=True)
    parser.add_argument("--asset-base-name")
    parser.add_argument("--save-groups", action="store_true")
    parser.add_argument("--groups-only", action="store_true")
    parser.add_argument("--control-only", action="store_true")
    parser.add_argument("--control-asset-name")
    parser.add_argument("--reuse-existing-materials", action="store_true")
    parser.add_argument("--source-skeletal-mesh")
    parser.add_argument("--prefab-asset-path")
    parser.add_argument("--prefab-manifest")
    parser.add_argument("--prefab-materials-json")
    parser.add_argument("--expected-output-dir")
    parser.add_argument("--expected-dump-script")
    parser.add_argument("--prefab-floor-policy", choices=("group_min", "source_global"), default="group_min")
    args, push_args = parser.parse_known_args(argv)
    if not args.source_path and (not args.group_evidence or not args.asset_suffix):
        parser.error("Legacy evidence mode requires --group-evidence and --asset-suffix")
    if not 1 <= args.max_groups <= 256:
        parser.error("--max-groups must be in 1..256; 20 is the default work budget")
    if args.source_path and not args.groups_only and not args.control_only and not args.source_skeletal_mesh:
        parser.error("Generic prefab export requires --source-skeletal-mesh for native source verification")
    if (args.groups_only or args.control_only) and args.prefab_manifest:
        parser.error("Prefab manifest requires an exported group mesh set")
    if bool(args.expected_output_dir) != bool(args.expected_dump_script):
        parser.error("Expected geometry requires both output directory and dump script")
    if args.expected_output_dir and (args.save_groups or args.groups_only or args.control_only):
        parser.error("Expected geometry is only supported for unsaved prefab exports")
    if args.control_only:
        if args.save_groups:
            parser.error("Control export is memory-only; --save-groups is forbidden")
        if args.groups_only:
            parser.error("--control-only and --groups-only select different export modes")
        if not args.control_asset_name:
            parser.error("--control-only requires --control-asset-name")
    elif args.control_asset_name:
        parser.error("--control-asset-name requires --control-only")
    if not args.groups_only and "--skip-wind" not in push_args:
        parser.error("Static terrain-group Push requires explicit --skip-wind")
    return args, push_args


def _runtime_requirements(args):
    requirements = {"speedtree_bone_weight_repair": ("debris_terrain_groups_v1",)}
    if getattr(args, "source_path", None):
        requirements["speedtree_bone_weight_repair"] += ("debris_terrain_prefab_v1",)
    if args.control_only:
        requirements["speedtree_bone_weight_repair"] += ("debris_terrain_control_v1",)
    if not args.groups_only:
        requirements["speedtree_bone_weight_repair"] += ("material_handoff_v1",)
        requirements["send2ue"] = ("headless_export_v1", "unreal_rpc_v1")
        requirements["ue_unique_export_names_addon"] = ("unreal_handoff_json_v1",)
    elif args.reuse_existing_materials:
        requirements["send2ue"] = ("headless_export_v1",)
    return requirements


def _file_hash(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _data_hash(data, field, width, typecode):
    values = array(typecode, [0]) * (len(data) * width)
    data.foreach_get(field, values)
    return hashlib.sha256(values.tobytes()).hexdigest()


def _attribute_hash(attribute):
    if not attribute.data:
        return hashlib.sha256(b"").hexdigest()
    sample = attribute.data[0]
    fields = ("value", "vector", "color", "uv")
    field = next((name for name in fields if hasattr(sample, name)), None)
    if field is None:
        raise RuntimeError(f"Unsupported source attribute {attribute.name}: {attribute.data_type}")
    value = getattr(sample, field)
    if isinstance(value, str):
        payload = [getattr(item, field) for item in attribute.data]
        return hashlib.sha256(json.dumps(payload, ensure_ascii=False).encode("utf-8")).hexdigest()
    width = len(value) if hasattr(value, "__len__") else 1
    typecode = "i" if attribute.data_type.startswith("INT") or attribute.data_type == "BOOLEAN" else "f"
    return _data_hash(attribute.data, field, width, typecode)


def _source_snapshot(objects):
    bpy.context.view_layer.update()
    snapshot = {}
    for obj in objects:
        row = {
            "type": obj.type,
            "parent": obj.parent.name if obj.parent else None,
            "matrix_world": [list(item) for item in obj.matrix_world],
        }
        if obj.type == "MESH":
            mesh = obj.data
            row.update({
                "vertices": len(mesh.vertices),
                "polygons": len(mesh.polygons),
                "triangles": sum(len(poly.vertices) - 2 for poly in mesh.polygons),
                "positions_sha256": _data_hash(mesh.vertices, "co", 3, "f"),
                "edges_sha256": _data_hash(mesh.edges, "vertices", 2, "i"),
                "corners_sha256": _data_hash(mesh.loops, "vertex_index", 1, "i"),
                "polygon_sizes_sha256": _data_hash(mesh.polygons, "loop_total", 1, "i"),
                "polygon_materials_sha256": _data_hash(mesh.polygons, "material_index", 1, "i"),
                "materials": [material.name if material else None for material in mesh.materials],
                "attributes": [{
                    "name": attribute.name,
                    "domain": attribute.domain,
                    "data_type": attribute.data_type,
                    "count": len(attribute.data),
                    "sha256": _attribute_hash(attribute),
                } for attribute in mesh.attributes if not attribute.name.startswith(".select_")],
            })
        snapshot[obj.name] = row
    return snapshot


def _write_report(path, report):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


def _push_report_path(push_args):
    if "--report" not in push_args:
        raise RuntimeError("Existing Push arguments require --report")
    index = push_args.index("--report") + 1
    if index >= len(push_args):
        raise RuntimeError("--report is missing its output path")
    return Path(push_args[index]).resolve()


def _child_mesh_properties():
    properties = getattr(bpy.context.scene, "send2ue", None)
    extensions = getattr(properties, "extensions", None)
    combine = getattr(extensions, "combine_assets", None)
    if combine is None or not hasattr(combine, "combine"):
        raise RuntimeError("Active Send2UE has no combine_assets.combine property")
    options = combine.bl_rna.properties["combine"].enum_items
    if "child_meshes" not in {item.identifier for item in options}:
        raise RuntimeError("Active Send2UE cannot export Child meshes units")
    origin_rna = properties.bl_rna.properties.get("use_object_origin")
    if origin_rna is None or origin_rna.type != "BOOLEAN" or origin_rna.is_readonly:
        raise RuntimeError("Active Send2UE has no writable Boolean use_object_origin property")
    return properties, combine


def _reuse_material_properties():
    properties, combine = _child_mesh_properties()
    pipeline = getattr(properties.extensions, "material_pipeline", None)
    if not hasattr(properties, "import_materials_and_textures"):
        raise RuntimeError("Active Send2UE has no scene import_materials_and_textures property")
    if pipeline is None or not hasattr(pipeline, "enabled"):
        raise RuntimeError("Active Send2UE has no material_pipeline.enabled property")
    return properties, pipeline, combine


def _static_triangle_properties():
    properties = getattr(bpy.context.scene, "send2ue", None)
    unreal_properties = getattr(properties, "unreal", None)
    imports = getattr(getattr(unreal_properties, "import_method", None), "fbx", None)
    static_library = getattr(unreal_properties, "editor_static_mesh_library", None)
    owners = {
        "static_import": getattr(imports, "static_mesh_import_data", None),
        "static_lod": getattr(static_library, "lod_build_settings", None),
    }
    for name, owner in owners.items():
        rna = owner.bl_rna.properties.get("remove_degenerates") if owner is not None else None
        if rna is None or rna.type != "BOOLEAN" or rna.is_readonly:
            raise RuntimeError(f"Active Send2UE has no writable Boolean {name}.remove_degenerates")
    return owners


def main():
    args, push_args = parse_args()
    output = Path(args.group_report).resolve()
    source = Path(bpy.data.filepath).resolve()
    if not bpy.data.filepath or source.suffix.casefold() != ".blend" or not source.is_file():
        raise RuntimeError("Terrain groups require a saved canonical Blender source")
    if args.source_path and Path(args.source_path).resolve() != source:
        raise RuntimeError("--source-path must equal the currently open canonical source")
    evidence_path = Path(args.group_evidence).resolve() if args.group_evidence else output.with_name(output.stem + "_evidence.json")
    prefab_output = Path(args.prefab_manifest).resolve() if args.prefab_manifest else (
        output.with_name(output.stem + "_prefab_manifest.json") if args.source_path and not args.groups_only and not args.control_only else None)
    material_overrides = []
    if args.prefab_materials_json:
        material_overrides = json.loads(Path(args.prefab_materials_json).read_text(encoding="utf-8"))
        if not isinstance(material_overrides, list) or not all(isinstance(path, str) for path in material_overrides):
            raise RuntimeError("--prefab-materials-json must be a JSON list of exact existing MI package paths")
    written_paths = [output, evidence_path] if args.source_path else [output]
    if prefab_output:
        written_paths.append(prefab_output)
    if source in written_paths or len(set(written_paths)) != len(written_paths):
        raise RuntimeError("Source, group receipt, evidence and prefab payload paths must be distinct")
    if not args.groups_only:
        push_report_path = _push_report_path(push_args)
        if push_report_path == output:
            raise RuntimeError("Group report and existing Push report must use distinct paths")
        if push_report_path in written_paths:
            raise RuntimeError("Existing Push report cannot overwrite group/evidence/prefab output")
    report = {
        "schema_version": 1,
        "kind": "sk_batch_debris_terrain_group_worker",
        "status": "failed",
        "stage": "startup",
        "source": str(source),
        "source_file_sha256_before": _file_hash(source),
        "group_evidence": str(evidence_path),
        "asset_suffix": args.asset_suffix,
        "groups_saved": False,
        "groups_only": args.groups_only,
        "control_only": args.control_only,
        "control_asset_name": args.control_asset_name,
        "export_variant": "control" if args.control_only else "terrain_groups",
        "reuse_existing_materials": args.reuse_existing_materials,
        "generic_source_mode": bool(args.source_path),
        "max_groups": args.max_groups,
    }
    started = time.perf_counter()
    try:
        export = bpy.data.collections.get("Export")
        if export is None:
            raise RuntimeError("Canonical source has no original Export collection")
        original_objects = list(export.all_objects)
        original_direct_objects = list(export.objects)
        original_children = list(export.children)
        original_names = sorted(obj.name for obj in original_objects)
        report["original_export_before"] = _source_snapshot(original_objects)
        report["stage"] = "addon_setup"
        runtime = prepare_runtime(
            "sk_batch.jobs.debris_terrain_group_job",
            _runtime_requirements(args),
        )
        report["blender_addon_runtime"] = runtime.receipt
        if not args.groups_only:
            _child_mesh_properties()
            triangle_properties = _static_triangle_properties()
            report["triangle_preservation_settings"] = {
                "applied_in_memory": False,
                "before": {name: bool(owner.remove_degenerates) for name, owner in triangle_properties.items()},
            }
        if args.reuse_existing_materials:
            properties, pipeline, combine = _reuse_material_properties()
            report["material_reuse_settings"] = {
                "applied_in_memory": False,
                "before": {
                    "material_pipeline_enabled": bool(pipeline.enabled),
                    "import_materials_and_textures": bool(properties.import_materials_and_textures),
                    "combine_assets": str(combine.combine),
                },
            }
        build = runtime.operation("speedtree_bone_weight_repair",
                                  "build_generic_terrain_groups" if args.source_path else "build_terrain_groups")
        activate = runtime.operation(
            "speedtree_bone_weight_repair",
            "activate_terrain_control_export" if args.control_only else "activate_terrain_group_export",
        )
        report["stage"] = "build_groups"
        build_kwargs = {"evidence_path": str(evidence_path), "collection_name": "DebrisTerrainGroups",
                        "asset_base_name": args.asset_base_name, "max_groups": args.max_groups}
        if args.source_path:
            build_kwargs.update({"source_path": str(source), "target_radius_cm": args.target_radius_cm,
                                 "target_height_span_cm": args.target_height_span_cm})
        else:
            build_kwargs["source_asset_suffix"] = args.asset_suffix
        report["groups"] = build(bpy.context, **build_kwargs)
        report["original_export_after_build"] = _source_snapshot(original_objects)
        unchanged = report["original_export_before"] == report["original_export_after_build"]
        memberships_unchanged = original_names == sorted(obj.name for obj in export.all_objects)
        report["original_export_preserved"] = unchanged and memberships_unchanged
        if not report["original_export_preserved"]:
            raise RuntimeError("Terrain group construction changed the original Export geometry or membership")
        if report["groups"].get("status") != "built":
            raise RuntimeError("Terrain group helper did not return a built result")
        if args.save_groups:
            report["stage"] = "save_groups"
            if Path(bpy.data.filepath).resolve() != source:
                raise RuntimeError("Canonical source path changed before group save")
            previous_save_version = bpy.context.preferences.filepaths.save_version
            try:
                bpy.context.preferences.filepaths.save_version = 0
                result = bpy.ops.wm.save_mainfile()
            finally:
                bpy.context.preferences.filepaths.save_version = previous_save_version
            if "FINISHED" not in result:
                raise RuntimeError(f"Canonical group source save failed: {result}")
            report["groups_saved"] = True
        report["source_file_sha256_after_group_stage"] = _file_hash(source)
        if args.groups_only:
            report["stage"] = "completed_groups_only"
            report["status"] = "ok"
            return
        properties, combine = _child_mesh_properties()
        for owner in triangle_properties.values():
            owner.remove_degenerates = False
        triangle_after = {name: bool(owner.remove_degenerates) for name, owner in triangle_properties.items()}
        if any(triangle_after.values()):
            raise RuntimeError("Active Send2UE did not retain exact static triangle preservation")
        report["triangle_preservation_settings"].update({"applied_in_memory": True, "after": triangle_after})
        report["export_combine_settings"] = {"before": str(combine.combine)}
        report["export_origin_settings"] = {
            "applied_in_memory": False,
            "before": bool(properties.use_object_origin),
            "contract": "child_meshes_empty_object_name_world_translation",
        }
        combine.combine = "child_meshes"
        if combine.combine != "child_meshes":
            raise RuntimeError("Active Send2UE did not retain Child meshes export mode")
        report["export_combine_settings"]["after"] = str(combine.combine)
        properties.use_object_origin = True
        if properties.use_object_origin is not True:
            raise RuntimeError("Active Send2UE did not retain group Empty origin export mode")
        report["export_origin_settings"].update({"applied_in_memory": True, "after": True})
        if args.reuse_existing_materials:
            properties, pipeline, combine = _reuse_material_properties()
            pipeline.enabled = False
            properties.import_materials_and_textures = False
            current = {
                "material_pipeline_enabled": bool(pipeline.enabled),
                "import_materials_and_textures": bool(properties.import_materials_and_textures),
                "combine_assets": str(combine.combine),
            }
            if current != {
                "material_pipeline_enabled": False,
                "import_materials_and_textures": False,
                "combine_assets": "child_meshes",
            }:
                raise RuntimeError("Active Send2UE did not retain existing-material reuse settings")
            report["material_reuse_settings"]["applied_in_memory"] = True
            report["material_reuse_settings"]["after"] = current
        report["stage"] = "activate_export_in_memory"
        activation_kwargs = {"collection_name": "DebrisTerrainGroups"}
        if args.control_only:
            activation_kwargs["asset_name"] = args.control_asset_name
        report["export_activation"] = activate(bpy.context, **activation_kwargs)
        if report["export_activation"].get("status") != "activated":
            raise RuntimeError("Terrain group export isolation failed")
        expected_export_count = 1 if args.control_only else report["groups"]["group_count"]
        if report["export_activation"].get("group_count") != expected_export_count:
            raise RuntimeError("Terrain export activation produced an unexpected asset count")
        report["stage"] = "existing_send2ue_push_job"
        report["push_args"] = push_args
        _write_report(output, report)
        original_argv = sys.argv
        try:
            sys.argv = [str(JOB_DIR / "send2ue_push_job.py"), "--", *push_args]
            runpy.run_path(str(JOB_DIR / "send2ue_push_job.py"), run_name="__main__")
        finally:
            sys.argv = original_argv
        if not push_report_path.is_file():
            raise RuntimeError("Existing Send2UE Push job produced no result report")
        push_report = json.loads(push_report_path.read_text(encoding="utf-8"))
        report["push_report"] = str(push_report_path)
        report["push_status"] = push_report.get("status")
        report["push_stage"] = push_report.get("stage")
        if report["push_status"] not in {"ok", "exported_pending_unreal"}:
            raise RuntimeError(f"Existing Send2UE Push failed: {push_report.get('error') or report['push_status']}")
        report["source_file_sha256_after_push"] = _file_hash(source)
        if report["source_file_sha256_after_push"] != report["source_file_sha256_after_group_stage"]:
            raise RuntimeError("Push unexpectedly rewrote the canonical source")
        if prefab_output:
            report["stage"] = "prefab_manifest"
            manifest_path = Path(push_report.get("manifest") or "").resolve()
            if not manifest_path.is_file():
                raise RuntimeError("Actual Send2UE export manifest is required for prefab binding")
            evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
            rows = [row for row in evidence["sources"] if Path(row["source_file"]).resolve() == source]
            if len(rows) != 1:
                raise RuntimeError("Exactly one canonical source evidence row is required")
            payload = build_prefab_manifest(
                report["groups"], json.loads(manifest_path.read_text(encoding="utf-8")),
                source_skeletal_mesh=args.source_skeletal_mesh, prefab_asset_path=args.prefab_asset_path,
                source_evidence=rows[0], source_hash=report["source_file_sha256_after_push"],
                material_overrides=material_overrides, geometry_verified=False,
                floor_policy=args.prefab_floor_policy)
            _write_report(prefab_output, payload)
            report["prefab_manifest"] = str(prefab_output)
            report["prefab_payload_sha256"] = payload["payload_sha256"]
            report["prefab_import_geometry_verified"] = False
        if args.expected_output_dir:
            report["stage"] = "expected_geometry"
            # Restore only the captured collection links. The generated groups
            # remain in memory for exact Send2UE coordinate readback.
            for obj in list(export.objects):
                if obj not in original_direct_objects:
                    export.objects.unlink(obj)
            for obj in original_direct_objects:
                if obj.name not in export.objects:
                    export.objects.link(obj)
            for child in list(export.children):
                if child not in original_children:
                    export.children.unlink(child)
            for child in original_children:
                if child.name not in export.children:
                    export.children.link(child)
            if sorted(obj.name for obj in export.all_objects) != original_names:
                raise RuntimeError("Original Export links were not restored before readback")
            dump_script = Path(args.expected_dump_script).resolve()
            if not dump_script.is_file():
                raise RuntimeError("Expected geometry provider is missing")
            previous_argv = sys.argv
            try:
                sys.argv = [str(dump_script), "--", "--output-dir", args.expected_output_dir,
                            "--manifest", str(manifest_path)]
                runpy.run_path(str(dump_script), run_name="__main__")
            finally:
                sys.argv = previous_argv
            header = Path(args.expected_output_dir) / "expected_group_geometry.json"
            expected = json.loads(header.read_text(encoding="utf-8"))
            if expected["source_sha256_after"] != report["source_file_sha256_after_push"]:
                raise RuntimeError("Expected readback does not match the exported source")
            report["expected_geometry_header"] = str(header.resolve())
        report["status"] = report["push_status"]
        report["stage"] = "completed"
    except Exception as exc:
        report["error"] = str(exc)
        report["traceback"] = traceback.format_exc()
        raise
    finally:
        report["elapsed_seconds"] = time.perf_counter() - started
        _write_report(output, report)
        print("DEBRIS_TERRAIN_GROUP_WORKER=" + json.dumps({key: report.get(key) for key in (
            "status", "stage", "source", "groups_saved", "original_export_preserved", "push_status", "error",
        )}, ensure_ascii=False))


if __name__ == "__main__":
    main()
