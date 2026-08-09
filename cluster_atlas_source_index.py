"""Blender-authored source identity for Cluster-to-Atlas publication.

The host can validate the saved ``.blend`` SHA without importing Blender.  The
authoritative collection/object projection itself is always produced inside
Blender from Atlas' own source index and export grouping functions.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import re
import struct
from pathlib import Path

from blender_addon_gateway import prepare_runtime


SOURCE_INDEX_KIND = "speedtree_cluster_atlas_blender_source_index"
SOURCE_INDEX_VERSION = 1
COLLECTION_PROJECTION_VERSION = 1
COLLECTION_CONTENT_KEY_ALGORITHM = "sha256-canonical-json-v1"
MESH_CONTENT_KEY_ALGORITHM = "sha256-evaluated-mesh-v1"
ATLAS_COLLECTION_SCOPE_KEY = "atlas_leaf_speedtree_scope_id"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class ClusterAtlasSourceIndexError(RuntimeError):
    """Blender could not prove one unambiguous Atlas export source."""


def canonical_sha256(value):
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def file_sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def normalized_path_key(value):
    try:
        return os.path.normcase(
            os.path.abspath(os.fspath(value))
        ).casefold()
    except (OSError, TypeError, ValueError):
        return ""


def _custom_property(item, name, default=None):
    getter = getattr(item, "get", None)
    if not callable(getter):
        return default
    try:
        return getter(name, default)
    except TypeError:
        value = getter(name)
        return default if value is None else value


def _json_property(item, name):
    raw = _custom_property(item, name)
    if raw in (None, ""):
        return None
    if isinstance(raw, dict):
        return copy.deepcopy(raw)
    try:
        parsed = json.loads(str(raw))
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def stable_source_object_identity(obj):
    """Return a content-addressed identity independent of display names.

    New producers persist a direct prototype identity.  Existing normalized
    Cluster blends can be upgraded without rebuilding because their prototype
    index/asset and immutable 3D source contract provide the same stable
    lineage.  Generic Atlas objects with neither proof fail closed.
    """
    for name in (
        "speedtree_cluster_prototype_identity",
        "speedtree_prototype_identity",
    ):
        identity = _json_property(obj, name)
        digest = str((identity or {}).get("digest") or "").casefold()
        if identity and _SHA256_RE.fullmatch(digest):
            return {
                "kind": name,
                "digest": digest,
                "identity": identity,
            }

    source_contract = _json_property(
        obj, "speedtree_cluster_source_3d_contract"
    )
    prototype_index = _custom_property(
        obj, "speedtree_cluster_prototype_index"
    )
    prototype_asset = str(
        _custom_property(obj, "speedtree_cluster_prototype_asset", "") or ""
    ).strip()
    source_object = str(
        _custom_property(obj, "speedtree_cluster_source_object", "") or ""
    ).strip()
    source_partition_mode = str(
        _custom_property(
            obj, "speedtree_cluster_source_partition_mode", ""
        )
        or ""
    ).strip()
    source_bone = str(
        _custom_property(obj, "speedtree_cluster_source_bone", "") or ""
    ).strip()
    try:
        prototype_index = int(prototype_index)
    except (TypeError, ValueError):
        prototype_index = 0
    if (
        source_contract
        and prototype_index > 0
        and prototype_asset
        and source_object
        and source_partition_mode
    ):
        projection = {
            "kind": "speedtree_cluster_legacy_prototype_lineage",
            "version": 1,
            "prototype_index": prototype_index,
            "prototype_asset": prototype_asset,
            "source_object": source_object,
            "source_partition_mode": source_partition_mode,
            "source_bone": source_bone,
            "source_3d_contract": source_contract,
        }
        return {
            "kind": projection["kind"],
            "digest": canonical_sha256(projection),
            "identity": projection,
        }
    return None


def _hash_text(digest, value):
    encoded = str(value or "").encode("utf-8")
    digest.update(struct.pack("<Q", len(encoded)))
    digest.update(encoded)


def _hash_ints(digest, values):
    values = [int(value) for value in values]
    digest.update(struct.pack("<Q", len(values)))
    for value in values:
        digest.update(struct.pack("<q", value))


def _hash_floats(digest, values):
    values = [float(value) for value in values]
    digest.update(struct.pack("<Q", len(values)))
    for value in values:
        if not math.isfinite(value):
            raise ClusterAtlasSourceIndexError(
                "Atlas source mesh contains a non-finite numeric value"
            )
        digest.update(struct.pack("<d", value))


def _iter_components(rows):
    for row in rows:
        try:
            for value in row:
                yield value
        except TypeError:
            yield row


def _mesh_content_key(obj, mesh):
    digest = hashlib.sha256()
    _hash_text(digest, MESH_CONTENT_KEY_ALGORITHM)
    matrix_world = getattr(obj, "matrix_world", ())
    _hash_floats(digest, _iter_components(matrix_world))
    vertices = list(getattr(mesh, "vertices", ()))
    edges = list(getattr(mesh, "edges", ()))
    loops = list(getattr(mesh, "loops", ()))
    polygons = list(getattr(mesh, "polygons", ()))
    _hash_ints(
        digest,
        (len(vertices), len(edges), len(loops), len(polygons)),
    )
    _hash_floats(
        digest,
        (
            component
            for vertex in vertices
            for component in getattr(vertex, "co", ())
        ),
    )
    _hash_ints(
        digest,
        (
            vertex_index
            for edge in edges
            for vertex_index in getattr(edge, "vertices", ())
        ),
    )
    _hash_ints(
        digest,
        (
            value
            for loop in loops
            for value in (
                getattr(loop, "vertex_index", 0),
                getattr(loop, "edge_index", 0),
            )
        ),
    )
    _hash_ints(
        digest,
        (
            value
            for polygon in polygons
            for value in (
                getattr(polygon, "loop_start", 0),
                getattr(polygon, "loop_total", 0),
                getattr(polygon, "material_index", 0),
                int(bool(getattr(polygon, "use_smooth", False))),
            )
        ),
    )
    material_slots = list(getattr(obj, "material_slots", ()))
    _hash_ints(digest, (len(material_slots),))
    for slot in material_slots:
        material = getattr(slot, "material", None)
        _hash_text(digest, getattr(material, "name", "") if material else "")
    uv_layers = list(getattr(mesh, "uv_layers", ()))
    _hash_ints(digest, (len(uv_layers),))
    for layer in uv_layers:
        _hash_text(digest, getattr(layer, "name", ""))
        _hash_floats(
            digest,
            (
                component
                for item in getattr(layer, "data", ())
                for component in getattr(item, "uv", ())
            ),
        )
    color_attributes = list(getattr(mesh, "color_attributes", ()))
    _hash_ints(digest, (len(color_attributes),))
    for attribute in color_attributes:
        _hash_text(digest, getattr(attribute, "name", ""))
        _hash_text(digest, getattr(attribute, "domain", ""))
        _hash_text(digest, getattr(attribute, "data_type", ""))
        for item in getattr(attribute, "data", ()):
            color = getattr(item, "color", None)
            if color is not None:
                _hash_floats(digest, color)
    corner_normals = list(getattr(mesh, "corner_normals", ()))
    _hash_ints(digest, (len(corner_normals),))
    _hash_floats(
        digest,
        (
            component
            for normal in corner_normals
            for component in getattr(normal, "vector", ())
        ),
    )
    return digest.hexdigest()


def _evaluated_mesh(obj, bpy_module, depsgraph):
    meshes = getattr(getattr(bpy_module, "data", None), "meshes", None)
    factory = getattr(meshes, "new_from_object", None)
    evaluated_get = getattr(obj, "evaluated_get", None)
    if callable(factory) and callable(evaluated_get):
        evaluated = evaluated_get(depsgraph)
        mesh = factory(evaluated, depsgraph=depsgraph)
        return mesh, True
    return getattr(obj, "data", None), False


def _remove_temporary_mesh(mesh, bpy_module):
    meshes = getattr(getattr(bpy_module, "data", None), "meshes", None)
    remove = getattr(meshes, "remove", None)
    if callable(remove):
        remove(mesh)


def _collection_scope(collection):
    return str(
        _custom_property(collection, ATLAS_COLLECTION_SCOPE_KEY, "") or ""
    ).strip()


def resolve_authoritative_collection(
    bpy_module,
    expected_name,
    *,
    expected_scope_id=None,
):
    collections = getattr(getattr(bpy_module, "data", None), "collections", ())
    by_name = getattr(collections, "get", None)
    exact = by_name(expected_name) if callable(by_name) else next(
        (
            collection
            for collection in collections
            if str(getattr(collection, "name", "")) == expected_name
        ),
        None,
    )
    expected_scope_id = str(expected_scope_id or "").strip()
    if exact is not None:
        exact_scope = _collection_scope(exact)
        if expected_scope_id and exact_scope != expected_scope_id:
            raise ClusterAtlasSourceIndexError(
                "Atlas collection name exists with a foreign export scope: "
                f"{expected_name}"
            )
        return exact
    if not expected_scope_id:
        raise ClusterAtlasSourceIndexError(
            f"Atlas authoritative collection is missing: {expected_name}"
        )
    matches = [
        collection
        for collection in collections
        if _collection_scope(collection) == expected_scope_id
    ]
    if len(matches) != 1:
        raise ClusterAtlasSourceIndexError(
            "Atlas export scope does not identify exactly one collection: "
            f"{expected_scope_id} (matches={len(matches)})"
        )
    return matches[0]


def build_current_atlas_source_index(
    blend,
    collection_name,
    *,
    atlas_asset_name=None,
    expected_scope_id=None,
    bpy_module=None,
    addon_runtime=None,
):
    """Build the exact Atlas collection index for Blender's saved main file."""
    if bpy_module is None:
        import bpy as bpy_module  # type: ignore

    if addon_runtime is None:
        addon_runtime = prepare_runtime(
            "cluster_atlas_source_index.build_current_atlas_source_index",
            {"atlas_leaf_mesh_builder": ("source_index_v1",)},
        )
    current_blend_source_index = addon_runtime.operation(
        "atlas_leaf_mesh_builder", "current_blend_source_index"
    )
    grouped_source_objects = addon_runtime.operation(
        "atlas_leaf_mesh_builder", "grouped_source_objects"
    )

    blend = Path(blend).expanduser().absolute()
    atlas_index = current_blend_source_index(
        expected_blend_path=blend,
        bpy_module=bpy_module,
    )
    if (
        atlas_index.get("status") != "ok"
        or atlas_index.get("indexed_by_blender") is not True
        or not _SHA256_RE.fullmatch(
            str(atlas_index.get("blend_sha256") or "").casefold()
        )
    ):
        raise ClusterAtlasSourceIndexError(
            "Atlas did not return an exact saved Blender source index"
        )
    collection = resolve_authoritative_collection(
        bpy_module,
        str(collection_name or ""),
        expected_scope_id=expected_scope_id,
    )
    scope_id = _collection_scope(collection)
    if not scope_id:
        raise ClusterAtlasSourceIndexError(
            "Atlas authoritative collection has no persistent export scope"
        )
    groups = grouped_source_objects(
        collection,
        atlas_asset_name,
        preserve_explicit_material_name=True,
    )
    context = getattr(bpy_module, "context", None)
    depsgraph_get = getattr(context, "evaluated_depsgraph_get", None)
    depsgraph = depsgraph_get() if callable(depsgraph_get) else None
    rows = []
    ambiguous = []
    seen_objects = set()
    stable_identity_owners = {}
    for group in groups:
        group_name = str(group.get("collection") or "")
        material_name = str(group.get("material") or "")
        for obj in group.get("objects") or ():
            object_key = id(obj)
            if object_key in seen_objects:
                raise ClusterAtlasSourceIndexError(
                    "Atlas grouped source projection repeated an object: "
                    f"{getattr(obj, 'name', '')}"
                )
            seen_objects.add(object_key)
            stable_identity = stable_source_object_identity(obj)
            if stable_identity is None:
                ambiguous.append(
                    "stable_source_object_identity_missing:"
                    + str(getattr(obj, "name", ""))
                )
            else:
                stable_digest = stable_identity["digest"]
                previous_owner = stable_identity_owners.get(stable_digest)
                if previous_owner is not None:
                    ambiguous.append(
                        "stable_source_object_identity_duplicate:"
                        + stable_digest
                        + ":"
                        + previous_owner
                        + ":"
                        + str(getattr(obj, "name", ""))
                    )
                else:
                    stable_identity_owners[stable_digest] = str(
                        getattr(obj, "name", "")
                    )
            mesh, temporary = _evaluated_mesh(
                obj, bpy_module, depsgraph
            )
            if mesh is None:
                raise ClusterAtlasSourceIndexError(
                    "Atlas export source has no evaluated mesh: "
                    f"{getattr(obj, 'name', '')}"
                )
            try:
                content_key = _mesh_content_key(obj, mesh)
                mesh_counts = {
                    "vertices": len(getattr(mesh, "vertices", ())),
                    "edges": len(getattr(mesh, "edges", ())),
                    "loops": len(getattr(mesh, "loops", ())),
                    "polygons": len(getattr(mesh, "polygons", ())),
                }
            finally:
                if temporary:
                    _remove_temporary_mesh(mesh, bpy_module)
            user_collections = sorted(
                str(getattr(item, "name", ""))
                for item in getattr(obj, "users_collection", ())
            )
            rows.append(
                {
                    "object_name": str(getattr(obj, "name", "")),
                    "mesh_data_name": str(
                        getattr(getattr(obj, "data", None), "name", "")
                    ),
                    "group_collection": group_name,
                    "group_material": material_name,
                    "user_collections": user_collections,
                    "stable_source_identity": stable_identity,
                    "mesh_content_key": {
                        "algorithm": MESH_CONTENT_KEY_ALGORITHM,
                        "digest": content_key,
                    },
                    **mesh_counts,
                }
            )
    rows.sort(
        key=lambda row: (
            row["group_collection"].casefold(),
            row["object_name"].casefold(),
            row["mesh_data_name"].casefold(),
        )
    )
    state = "empty" if not rows else "populated"
    projection = {
        "projection_version": COLLECTION_PROJECTION_VERSION,
        "collection_name": str(getattr(collection, "name", "")),
        "export_scope_id": scope_id,
        "state": state,
        "mesh_object_count": len(rows),
        "mesh_objects": rows,
    }
    reasons = sorted(set(ambiguous))
    return {
        "kind": SOURCE_INDEX_KIND,
        "version": SOURCE_INDEX_VERSION,
        "status": "ok" if not reasons else "ambiguous",
        "indexed_by_blender": True,
        "blend": str(blend),
        "blend_sha256": str(atlas_index["blend_sha256"]).casefold(),
        "atlas_source_index": copy.deepcopy(atlas_index),
        "authoritative_collection": {
            **projection,
            "content_key": {
                "algorithm": COLLECTION_CONTENT_KEY_ALGORITHM,
                "digest": canonical_sha256(projection),
            },
        },
        "refresh_reasons": reasons,
    }


def bind_index_to_export_results(source_index, results):
    """Prove Atlas exported the same object/group set that was indexed."""
    index = copy.deepcopy(source_index)
    if index.get("status") != "ok":
        raise ClusterAtlasSourceIndexError(
            "Ambiguous Atlas source identity cannot be published"
        )
    collection = index["authoritative_collection"]
    expected = sorted(
        (
            row["object_name"],
            row["group_collection"],
            row["group_material"],
        )
        for row in collection.get("mesh_objects") or ()
    )
    results = list(results or ())
    if not results:
        raise ClusterAtlasSourceIndexError(
            "Atlas source identity cannot be bound without a target result"
        )
    publications = []
    for result in results:
        manifest_path = result.get("manifest_path")
        manifest = result.get("manifest")
        if manifest is None and manifest_path:
            try:
                manifest = json.loads(
                    Path(manifest_path).read_text(encoding="utf-8")
                )
            except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
                raise ClusterAtlasSourceIndexError(
                    "Atlas export result manifest could not be read: "
                    f"{manifest_path}"
                ) from exc
        if manifest is None:
            manifest = {}
        if not isinstance(manifest, dict):
            raise ClusterAtlasSourceIndexError(
                "Atlas export result manifest is not an object"
            )
        mesh_rows = result.get("meshes")
        if mesh_rows is None:
            mesh_rows = result.get("exported_meshes")
        if not isinstance(mesh_rows, (list, tuple)):
            raise ClusterAtlasSourceIndexError(
                "Atlas export result has no explicit exported mesh inventory"
            )
        actual = sorted(
            (
                str(row.get("source_object") or ""),
                str(row.get("source_collection") or ""),
                str(row.get("material") or ""),
            )
            for row in mesh_rows
        )
        manifest_actual = sorted(
            (
                str(row.get("source_object") or ""),
                str(row.get("source_collection") or ""),
                str(row.get("material") or ""),
            )
            for row in manifest.get("meshes") or ()
        )
        if manifest and manifest_actual != actual:
            raise ClusterAtlasSourceIndexError(
                "Atlas committed manifest differs from the export result mesh inventory"
            )
        tombstone = (
            result.get("collection_tombstone")
            or manifest.get("collection_tombstone")
            or {}
        )
        if expected != actual:
            raise ClusterAtlasSourceIndexError(
                "Atlas export result does not match the indexed Blender "
                f"collection for {result.get('spm') or result.get('spm_path')}"
            )
        if not expected and not (
            (
                result.get("texture_contract_status")
                or manifest.get("texture_contract_status")
            )
            == "collection_tombstone"
            and tombstone.get("state") == "empty"
        ):
            raise ClusterAtlasSourceIndexError(
                "Empty Atlas collection did not publish an explicit tombstone"
            )
        result_scope = str(
            result.get("export_scope_id")
            or manifest.get("export_scope_id")
            or ""
        )
        if result_scope != collection["export_scope_id"]:
            raise ClusterAtlasSourceIndexError(
                "Atlas export result scope differs from the indexed collection"
            )
        result_target = str(
            result.get("spm") or result.get("spm_path") or ""
        )
        manifest_target = str(manifest.get("spm") or "")
        if result_target and manifest_target and (
            normalized_path_key(result_target)
            != normalized_path_key(manifest_target)
        ):
            raise ClusterAtlasSourceIndexError(
                "Atlas committed manifest target differs from the export result"
            )
        target = result_target or manifest_target
        if not target:
            raise ClusterAtlasSourceIndexError(
                "Atlas export result has no target SPM identity"
            )
        manifest_collection = str(
            manifest.get("source_collection")
            or result.get("source_collection")
            or collection["collection_name"]
        )
        if manifest_collection != collection["collection_name"]:
            raise ClusterAtlasSourceIndexError(
                "Atlas committed manifest collection differs from the indexed collection"
            )
        manifest_blend = str(manifest.get("blend_file") or "")
        if manifest_blend and (
            normalized_path_key(manifest_blend)
            != normalized_path_key(index.get("blend"))
        ):
            raise ClusterAtlasSourceIndexError(
                "Atlas committed manifest blend differs from the indexed source"
            )
        publications.append(
            {
                "target_spm": target,
                "export_scope_id": result_scope,
                "mesh_count": len(actual),
                "state": collection["state"],
                "manifest": str(manifest_path or "") or None,
            }
        )
    index["publication"] = {
        "status": "bound",
        "target_count": len(publications),
        "targets": publications,
    }
    return index


def inspect_persisted_source_index(blend, identity):
    """Validate a previously Blender-authored index from the host process."""
    blend = Path(blend).expanduser().absolute()
    reasons = []
    current_sha256 = file_sha256(blend) if blend.is_file() else None
    if not blend.is_file():
        reasons.append("blender_source_missing")
    if not isinstance(identity, dict):
        reasons.append("blender_source_identity_missing")
        identity = {}
    elif (
        identity.get("kind") != SOURCE_INDEX_KIND
        or identity.get("version") != SOURCE_INDEX_VERSION
        or identity.get("indexed_by_blender") is not True
    ):
        reasons.append("blender_source_identity_invalid")
    if identity.get("status") != "ok":
        reasons.append("blender_source_identity_ambiguous")
        reasons.extend(identity.get("refresh_reasons") or ())

    recorded_blend = str(identity.get("blend") or "")
    if not recorded_blend:
        reasons.append("blender_source_path_identity_missing")
    elif normalized_path_key(recorded_blend) != normalized_path_key(blend):
        reasons.append("blender_source_path_changed")
    recorded_sha256 = str(identity.get("blend_sha256") or "").casefold()
    if not _SHA256_RE.fullmatch(recorded_sha256):
        reasons.append("blender_source_content_key_missing")
    elif current_sha256 is None or recorded_sha256 != current_sha256.casefold():
        reasons.append("blender_source_content_changed")

    atlas_index = identity.get("atlas_source_index") or {}
    if not isinstance(atlas_index, dict) or (
        atlas_index.get("status") != "ok"
        or atlas_index.get("indexed_by_blender") is not True
        or str(atlas_index.get("blend_sha256") or "").casefold()
        != recorded_sha256
        or normalized_path_key(atlas_index.get("blend"))
        != normalized_path_key(blend)
    ):
        reasons.append("atlas_blender_source_index_invalid")

    collection = identity.get("authoritative_collection") or {}
    if not isinstance(collection, dict):
        collection = {}
    collection_name = str(collection.get("collection_name") or "")
    export_scope_id = str(collection.get("export_scope_id") or "")
    state = str(collection.get("state") or "")
    rows = collection.get("mesh_objects")
    if not collection_name:
        reasons.append("blender_source_collection_identity_missing")
    if not export_scope_id:
        reasons.append("blender_source_scope_identity_missing")
    if state not in {"populated", "empty"} or not isinstance(rows, list):
        reasons.append("blender_source_object_inventory_invalid")
        rows = []
    if state == "empty" and rows:
        reasons.append("blender_source_empty_tombstone_invalid")
    if state == "populated" and not rows:
        reasons.append("blender_source_object_inventory_invalid")
    try:
        mesh_count = int(collection.get("mesh_object_count"))
    except (TypeError, ValueError):
        mesh_count = -1
    if mesh_count != len(rows):
        reasons.append("blender_source_object_inventory_invalid")
    for row in rows:
        stable = row.get("stable_source_identity") or {}
        content_key = row.get("mesh_content_key") or {}
        if not _SHA256_RE.fullmatch(
            str(stable.get("digest") or "").casefold()
        ):
            reasons.append("blender_source_object_identity_missing")
        if (
            content_key.get("algorithm") != MESH_CONTENT_KEY_ALGORITHM
            or not _SHA256_RE.fullmatch(
                str(content_key.get("digest") or "").casefold()
            )
        ):
            reasons.append("blender_source_mesh_content_key_missing")
    content_key = collection.get("content_key") or {}
    projection = {
        "projection_version": collection.get("projection_version"),
        "collection_name": collection_name,
        "export_scope_id": export_scope_id,
        "state": state,
        "mesh_object_count": collection.get("mesh_object_count"),
        "mesh_objects": rows,
    }
    if (
        content_key.get("algorithm") != COLLECTION_CONTENT_KEY_ALGORITHM
        or str(content_key.get("digest") or "").casefold()
        != canonical_sha256(projection)
    ):
        reasons.append("blender_source_collection_content_key_invalid")
    publication = identity.get("publication") or {}
    if publication.get("status") != "bound":
        reasons.append("blender_source_publication_binding_missing")
    publication_targets = publication.get("targets")
    try:
        publication_target_count = int(publication.get("target_count"))
    except (TypeError, ValueError):
        publication_target_count = -1
    if (
        not isinstance(publication_targets, list)
        or publication_target_count <= 0
        or publication_target_count != len(publication_targets)
    ):
        reasons.append("blender_source_publication_binding_invalid")
        publication_targets = []
    for target in publication_targets:
        if (
            not str(target.get("target_spm") or "")
            or str(target.get("export_scope_id") or "") != export_scope_id
            or target.get("state") != state
            or target.get("mesh_count") != len(rows)
        ):
            reasons.append("blender_source_publication_binding_invalid")

    reasons = sorted(set(str(reason) for reason in reasons if reason))
    return {
        "status": "current" if not reasons else "refresh_required",
        "current": not reasons,
        "refresh_reasons": reasons,
        "blend_file": str(blend),
        "current_blend_sha256": current_sha256,
        "recorded_blend_sha256": recorded_sha256 or None,
        "authoritative_collection": collection_name or None,
        "export_scope_id": export_scope_id or None,
        "collection_state": state or None,
        "mesh_object_count": mesh_count if mesh_count >= 0 else None,
        "collection_content_key": str(content_key.get("digest") or "") or None,
        "publication": publication or None,
    }


__all__ = [
    "ATLAS_COLLECTION_SCOPE_KEY",
    "COLLECTION_CONTENT_KEY_ALGORITHM",
    "COLLECTION_PROJECTION_VERSION",
    "ClusterAtlasSourceIndexError",
    "MESH_CONTENT_KEY_ALGORITHM",
    "SOURCE_INDEX_KIND",
    "SOURCE_INDEX_VERSION",
    "bind_index_to_export_results",
    "build_current_atlas_source_index",
    "canonical_sha256",
    "file_sha256",
    "inspect_persisted_source_index",
    "resolve_authoritative_collection",
    "stable_source_object_identity",
]
