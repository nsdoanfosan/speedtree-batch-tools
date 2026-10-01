"""Pure, source-generic payload for a DA-selected terrain prefab Blueprint.

This module never imports bpy/unreal, spawns actors, saves packages or edits the
existing DataAsset. Mesh paths come from the actual Send2UE manifest; class and
material paths are explicit authoring inputs. Import verification is separate.
"""
from __future__ import annotations

import hashlib
import json
import math
import copy
from pathlib import PurePosixPath
import re

SAFETY_GROUP_LIMIT = 256


def _canonical_sha256(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
        ensure_ascii=False, allow_nan=False).encode("utf-8")).hexdigest()


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def _asset_path(value, field):
    _require(isinstance(value,str) and value.startswith("/Game/") and
             not any(c.isspace() for c in value) and "." not in value and
             all(re.fullmatch(r"[A-Za-z0-9_]+",part) for part in value.split("/")[2:]),
             f"{field} requires an exact /Game package path")
    return value


def _vec(values, field):
    _require(isinstance(values,(list,tuple)) and len(values)==3 and
             all(isinstance(v,(int,float)) and not isinstance(v,bool) and math.isfinite(v) for v in values),
             f"{field} requires finite XYZ values")
    return [float(v) for v in values]


def source_to_ue_cm(point):
    x,y,z=_vec(point,"source world metres")
    return [100*x,-100*y,100*z]


def _bounds_for_group(group, evidence):
    if "world_aabb_min_m" in group and "world_aabb_max_m" in group:
        return _vec(group["world_aabb_min_m"],"group lower bounds"),_vec(group["world_aabb_max_m"],"group upper bounds")
    _require(evidence is not None,"Whole-part evidence is required for group footprint probes")
    by_index={p["part_index"]:p for p in evidence["parts"]}
    indexes=group["part_indexes"]
    _require(indexes and len(indexes)==len(set(indexes)) and all(i in by_index for i in indexes),
             "Group part identities do not match source evidence")
    return ([min(by_index[i]["world_aabb_min_m"][a] for i in indexes) for a in range(3)],
            [max(by_index[i]["world_aabb_max_m"][a] for i in indexes) for a in range(3)])


def build_prefab_manifest(build_receipt, export_manifest, *, source_skeletal_mesh,
                          prefab_asset_path=None, source_evidence=None,
                          source_hash=None, material_overrides=(), geometry_verified=False,
                          floor_policy="group_min"):
    """Bind validated Blender groups to unique actual exported StaticMesh paths."""
    _require(geometry_verified is False,
             "Export cannot assert native import geometry; use the receipt-bound finalizer")
    _require(build_receipt.get("status")=="built","Validated BWR build receipt is required")
    groups=build_receipt.get("groups") or []
    _require(1<=len(groups)<=SAFETY_GROUP_LIMIT and build_receipt.get("group_count")==len(groups),
             "Actual prefab group count must be in 1..256")
    checks=build_receipt.get("checks") or {}
    required=("whole_components_preserved","all_named_attributes_subset_equal","material_slots_preserved",
              "uv_layers_preserved","world_geometry_preserved","corner_normals_preserved",
              "geometry_counts_conserved","source_datablock_unchanged","original_export_unchanged")
    _require(all(checks.get(key) is True for key in required),"Source/group preservation checks failed")
    _require(checks.get("requested_budget_respected",checks.get("maximum_20_groups")) is True,
             "Requested adaptive work budget was not respected")
    wind=build_receipt.get("source_wind_contract") or build_receipt.get("wind_none_contract") or {}
    saved=wind.get("saved_contracts") or []
    _asset_path(source_skeletal_mesh,"source_skeletal_mesh")
    source=build_receipt.get("source") or {}
    source_hash=source_hash or source.get("baseline_file_sha256")
    _require(isinstance(source_hash,str) and re.fullmatch(r"[0-9a-f]{64}",source_hash),
             "Canonical source SHA-256 is required")
    fingerprints=source.get("fingerprint_before_after") or []
    _require(len(fingerprints)==2 and fingerprints[0]==fingerprints[1],"Source geometry fingerprint changed")
    floor=float(build_receipt["source_global_min_z_m"])
    _require(math.isfinite(floor),"Source virtual floor must be finite")
    _require(floor_policy in ("group_min", "source_global"), "Unknown source floor policy")
    assets=[asset["asset_data"] for item in export_manifest.get("items",[])
            for asset in item.get("assets",[]) if (asset.get("asset_data") or {}).get("_asset_type")=="StaticMesh"]
    _require(len(assets)==len(groups),"Exported StaticMesh count differs from actual groups")
    by_root={}
    for asset in assets:
        root=asset.get("empty_object_name")
        _require(root and root not in by_root,"Send2UE parent identities must be unique")
        _asset_path(asset.get("asset_path"),"exported mesh")
        _require(PurePosixPath(asset["asset_path"]).name==root,"Exported asset name differs from its authored parent")
        by_root[root]=asset
    group_ids=[g["group_id"] for g in groups]
    _require(all(isinstance(i,int) and not isinstance(i,bool) for i in group_ids)
             and sorted(group_ids)==list(range(len(groups))),"Build group IDs must be unique contiguous zero-based values")
    all_parts=[i for g in groups for i in g.get("part_indexes",[])]
    _require(all_parts and len(all_parts)==len(set(all_parts)),"Whole source leaf membership overlaps or is empty")
    if source_evidence:
        _require(set(all_parts)=={p["part_index"] for p in source_evidence["parts"]},
                 "Some whole source leaves were lost or added")
    _require(sum(g["triangles"] for g in groups)==source["triangles"],"Source triangle count is not conserved")
    materials=[_asset_path(path,"material_overrides") for path in material_overrides]
    output=[]
    for group in sorted(groups,key=lambda row:row["group_id"]):
        asset=by_root.pop(group["root_object"],None)
        _require(asset is not None and asset.get("_mesh_object_name")==group["mesh_object"],
                 "Exported child/parent binding differs from the BWR receipt")
        pivot=_vec(group["pivot_world_m"],"group pivot")
        low,high=_bounds_for_group(group,source_evidence)
        _require(all(a<=b for a,b in zip(low,high)) and low[2]>=floor-1e-6,
                 "Group bounds/virtual floor are inconsistent")
        group_floor=low[2] if floor_policy == "group_min" else floor
        offset=100*(pivot[2]-group_floor)
        _require(offset>=-0.001,"Group pivot is below the source floor")
        offset=max(0.,offset)  # Float32 parent translation may differ by sub-micrometres.
        # Five component-local virtual-floor probes: center and XY mid-edges.
        # They are not claims of measured terrain hits or per-leaf contact.
        z=-offset
        probes=[[0.,0.,z],[100*(low[0]-pivot[0]),0.,z],[100*(high[0]-pivot[0]),0.,z],
                [0.,100*(pivot[1]-high[1]),z],[0.,100*(pivot[1]-low[1]),z]]
        identifier=group["group_id"]+1
        output.append({"group_id":identifier,"component_name":f"Group{identifier:02d}",
                       "mesh_asset_path":asset["asset_path"],
                       "relative_location_cm":source_to_ue_cm(pivot),
                       "relative_rotation_degrees":[0.,0.,0.],"relative_scale":[1.,1.,1.],
                       "contact_offset_cm":offset,"source_floor_z_cm":100*group_floor,
                       "source_part_count":len(group["part_indexes"]),
                       "probes_local_cm":probes,"material_overrides":materials[:]})
    _require(not by_root,"Export contains unmatched terrain StaticMeshes")
    first=PurePosixPath(output[0]["mesh_asset_path"])
    if prefab_asset_path is None:
        stem=PurePosixPath(str(source["file"]).replace("\\","/")).stem
        for prefix in ("SK_","SM_"):
            if stem.startswith(prefix):
                stem=stem[len(prefix):]
                break
        prefab_asset_path=str(first.parent/f"BP_{stem}_TerrainPrefab")
    _asset_path(prefab_asset_path,"prefab_asset_path")
    payload={"schema_version":1,"kind":"sk_batch_debris_terrain_prefab",
             "prefab_asset_path":prefab_asset_path,"source_skeletal_mesh":source_skeletal_mesh,
             "source_hash":source_hash,"contact_policy_version":2 if floor_policy == "group_min" else 1,
             "source_floor_policy":floor_policy,
             "activation_policy":"explicit_source_request_independent_of_wind",
             "output_mode":"static_rest_pose","group_count":len(output),"groups":output,
             "import_geometry_verified":bool(geometry_verified),
             "coordinate_contract":{"units":"centimetres","source_axis":"Blender world metres",
                "axis_conversion":"(x,y,z)->(100x,-100y,100z)",
                "relative_rotation_degrees_order":["pitch","yaw","roll"],
                "probe_frame":"component-local UE cm selected source floor; not actual terrain contact"},
             "provenance":{"source_file":source["file"],"source_geometry_sha256":fingerprints[0],
                           "source_vertices":source["vertices"],"source_triangles":source["triangles"],
                           "source_part_count":len(all_parts),"wind_contracts":saved,
                           "source_global_min_z_cm":100*floor,
                           "adaptive_grouping":build_receipt.get("adaptive_grouping")},
             "validation":{"unique_exact_export_bindings":True,"whole_parts_once":True,
                           "source_geometry_preserved":True,"requested_budget_respected":True}}
    payload["payload_sha256"]=_canonical_sha256(payload)
    return payload


def finalize_import_verified_manifest(payload, geometry_receipt, *, expected_geometry_header_bytes,
                                      actual_geometry_header_bytes=None, material_bindings=None):
    """Finalize only the exact source/assets proved by native geometry readback.

    The caller reads the existing expected header as bytes. This function does
    no IO; its byte hash binds the source hash, asset paths and parent pivots to
    the independent geometry receipt before the editor may author a Blueprint.
    """
    _require(payload.get("kind") == "sk_batch_debris_terrain_prefab" and
             payload.get("schema_version") == 1, "Expected a versioned prefab payload")
    content = {key: value for key, value in payload.items() if key != "payload_sha256"}
    _require(payload.get("payload_sha256") == _canonical_sha256(content), "Prefab payload checksum differs")
    _require(payload.get("import_geometry_verified") is False, "Only an unverified export payload may be finalized")
    _require(isinstance(expected_geometry_header_bytes, bytes), "Expected geometry header must be exact file bytes")
    expected_hash = hashlib.sha256(expected_geometry_header_bytes).hexdigest()
    _require(geometry_receipt.get("expected_header_sha256") == expected_hash,
             "Geometry receipt does not bind the provided expected header")
    expected = json.loads(expected_geometry_header_bytes.decode("utf-8"))
    _require(expected.get("source_sha256_before") == payload["source_hash"] and
             expected.get("source_sha256_after") == payload["source_hash"],
             "Geometry proof belongs to another or changed canonical source")
    _require(geometry_receipt.get("kind") == "bidirectional_debris_group_source_model_verification" and
             geometry_receipt.get("status") == "passed" and geometry_receipt.get("passed") is True and
             geometry_receipt.get("group_geometry_passed") is True and
             geometry_receipt.get("triangle_total_equal") is True and
             geometry_receipt.get("no_coordinate_transform_inferred") is True,
             "Independent native group geometry verification did not pass")
    tolerance = geometry_receipt.get("tolerance_cm")
    _require(isinstance(tolerance, (int, float)) and not isinstance(tolerance, bool) and
             math.isfinite(tolerance) and 0 < tolerance <= 0.001,
             "Geometry verification tolerance must be at most 0.001 cm")
    for key in ("expected_binary_sha256", "actual_header_sha256", "actual_binary_sha256"):
        _require(isinstance(geometry_receipt.get(key), str) and
                 re.fullmatch(r"[0-9a-f]{64}", geometry_receipt[key]), "Geometry proof hashes are missing")
    _require(expected.get("binary_sha256") == geometry_receipt["expected_binary_sha256"],
             "Expected geometry binary fingerprint differs")
    def package(path):
        # Native readback may return the exact object path Package.Asset.
        value = path.split(".", 1)[0] if isinstance(path, str) else path
        return _asset_path(value, "geometry mesh")
    def rows_by_package(rows):
        result = {}
        for row in rows:
            key = package(row["asset_path"])
            _require(key not in result, "Geometry asset bindings are duplicated")
            result[key] = row
        return result
    expected_rows = rows_by_package(expected.get("groups") or [])
    verified_rows = rows_by_package(geometry_receipt.get("groups") or [])
    groups = payload.get("groups") or []
    paths = [group["mesh_asset_path"] for group in groups]
    count = len(groups)
    _require(1 <= count <= SAFETY_GROUP_LIMIT and len(set(paths)) == count and
             payload.get("group_count") == count and geometry_receipt.get("group_count") == count and
             set(paths) == set(expected_rows) == set(verified_rows),
             "Geometry proof assets differ from actual prefab groups")
    triangles = payload["provenance"]["source_triangles"]
    _require(expected.get("group_triangle_count") == triangles and
             all(geometry_receipt.get(key) == triangles for key in
                 ("required_triangle_total", "expected_group_triangles", "actual_group_triangles")),
             "Geometry proof triangle total differs from the canonical source")
    for group in groups:
        row = verified_rows[group["mesh_asset_path"]]
        source_row = expected_rows[group["mesh_asset_path"]]
        pivot = source_to_ue_cm(source_row["pivot_source_world_m"])
        _require(all(abs(a-b) <= 1e-6 for a, b in zip(pivot, group["relative_location_cm"])),
                 "Geometry proof parent pivot differs from the prefab transform")
        _require(row.get("passed") is True and row.get("triangle_count_equal") is True and
                 row.get("expected_triangles") == source_row["triangle_count"] and
                 row.get("actual_triangles") == source_row["triangle_count"] and
                 (row.get("expected_to_actual") or {}).get("passed") is True and
                 (row.get("actual_to_expected") or {}).get("passed") is True,
                 "A prefab group failed exact native geometry verification")
    result = copy.deepcopy(payload)
    material_proof = None
    if actual_geometry_header_bytes is not None or material_bindings is not None:
        _require(isinstance(actual_geometry_header_bytes, bytes),
                 "Material binding requires exact native geometry header bytes")
        _require(hashlib.sha256(actual_geometry_header_bytes).hexdigest() == geometry_receipt["actual_header_sha256"],
                 "Native material header differs from the verified geometry receipt")
        native = json.loads(actual_geometry_header_bytes.decode("utf-8"))
        _require(native.get("binary_sha256") == geometry_receipt["actual_binary_sha256"],
                 "Native geometry binary fingerprint differs")
        native_rows = rows_by_package(native.get("groups") or [])
        _require(set(native_rows) == set(paths), "Native material assets differ from the prefab group set")
        if material_bindings is not None:
            _require(isinstance(material_bindings, dict) and material_bindings and
                     all(isinstance(name, str) and name for name in material_bindings),
                     "Canonical material bindings require exact slot names")
            material_bindings = {name: _asset_path(path, "canonical material")
                                 for name, path in material_bindings.items()}
        for group in result["groups"]:
            slots = native_rows[group["mesh_asset_path"]].get("material_slots") or []
            _require(slots and sorted(slot.get("index") for slot in slots) == list(range(len(slots))),
                     "Native material slots must have unique contiguous indices")
            slots = sorted(slots, key=lambda slot: slot["index"])
            names = [slot.get("material_slot_name") for slot in slots]
            _require(all(isinstance(name, str) and name for name in names) and len(set(names)) == len(names),
                     "Native material slot identities are ambiguous")
            if material_bindings is not None:
                _require(all(name in material_bindings for name in names),
                         "An imported material slot has no exact canonical binding")
                group["material_overrides"] = [material_bindings[name] for name in names]
            else:
                group["material_overrides"] = [_asset_path(package(slot.get("material_path")), "native material")
                                                for slot in slots]
            group["material_slot_names"] = names
        material_proof = {"binding": "Exact imported slot name and ordered native slot index",
                          "canonical_bindings_sha256": _canonical_sha256(material_bindings) if material_bindings else None,
                          "native_header_sha256": geometry_receipt["actual_header_sha256"]}
    result["import_geometry_verified"] = True
    result["import_geometry_verification"] = {
        "kind": "native_source_model_bidirectional_cloud",
        "geometry_receipt_canonical_sha256": _canonical_sha256(geometry_receipt),
        "expected_header_path": geometry_receipt.get("expected_header_path"),
        "expected_header_sha256": expected_hash,
        "expected_binary_sha256": geometry_receipt["expected_binary_sha256"],
        "actual_header_path": geometry_receipt.get("actual_header_path"),
        "actual_header_sha256": geometry_receipt["actual_header_sha256"],
        "actual_binary_sha256": geometry_receipt["actual_binary_sha256"],
        "source_hash": payload["source_hash"], "group_count": count,
        "tolerance_cm": tolerance,
        "material_slots": material_proof,
    }
    del result["payload_sha256"]
    result["payload_sha256"] = _canonical_sha256(result)
    return result
