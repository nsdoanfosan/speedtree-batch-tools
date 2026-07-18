"""Plan and apply connection-only SpeedTree Generator reference repairs.

This module deliberately does not import Blender or the Atlas Leaf Mesh Builder
add-on.  It repairs ``Leaf Mesh``/``Frond`` Generator Material and Mesh
references from an exact authoritative non-SK SPM.  A complete Property pair
that disappeared may be copied back only into the same Generator GUID. Existing
Material, Mesh, FBX, blend, and texture assets are validation evidence and are
never created, deleted, or rewritten.
"""
from __future__ import annotations

import argparse
import codecs
import copy
import gzip
import hashlib
import html
import json
import os
import re
import shutil
import tempfile
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path


PLAN_VERSION = 3
PLAN_KIND = "speedtree_generator_reference_repair"
ATLAS_LEAF_GENERATOR = "Atlas Leaf Mesh Builder"
OUTPUT_POLICIES = {"managed", "restore_source"}

GENERATOR_BLOCK_RE = re.compile(
    r"<Generator\b[^>]*>.*?</Generator>", re.IGNORECASE | re.DOTALL)
PROPERTY_BLOCK_RE = re.compile(
    r"<Property\b[^>]*>.*?</Property>", re.IGNORECASE | re.DOTALL)
NAME_RE = re.compile(
    r"<Name\b[^>]*>(.*?)</Name>", re.IGNORECASE | re.DOTALL)
VALUE_RE = re.compile(
    r"(<Value\b[^>]*>)(.*?)(</Value>)", re.IGNORECASE | re.DOTALL)
PROPERTIES_CLOSE_RE = re.compile(r"</Properties\s*>", re.IGNORECASE)
LEAF_ORDINAL_RE = re.compile(
    r"(?:^|[^a-z0-9])leaf[_ -]*0*(\d+)(?=$|[^0-9])", re.IGNORECASE)


class ReferenceRepairError(RuntimeError):
    """Raised when a reference repair cannot be proven safe."""


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _int_value(value, label):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise ReferenceRepairError(f"{label} is not an integer: {value!r}") from exc


def _positive_id(value, label):
    result = _int_value(value, label)
    if result <= 0:
        raise ReferenceRepairError(f"{label} must be a positive ID: {value!r}")
    return result


def _load_spm(path):
    path = Path(path).resolve()
    if path.suffix.lower() != ".spm":
        raise ReferenceRepairError(f"target must be an .spm file: {path}")
    if not path.is_file():
        raise ReferenceRepairError(f"target SPM does not exist: {path}")
    raw = path.read_bytes()
    if not raw.startswith(b"\x1f\x8b"):
        raise ReferenceRepairError(f"target SPM is not gzip-compressed: {path}")
    try:
        xml_bytes = gzip.decompress(raw)
    except (OSError, EOFError) as exc:
        raise ReferenceRepairError(f"cannot decompress SPM: {path}: {exc}") from exc
    had_bom = xml_bytes.startswith(codecs.BOM_UTF8)
    try:
        text = xml_bytes.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ReferenceRepairError(
            f"SPM XML is not UTF-8 and cannot be patched losslessly: {path}") from exc
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as exc:
        raise ReferenceRepairError(f"invalid SPM XML: {path}: {exc}") from exc
    return {
        "path": path,
        "raw": raw,
        "sha256": hashlib.sha256(raw).hexdigest(),
        "xml_bytes": xml_bytes,
        "text": text,
        "had_bom": had_bom,
        "root": root,
    }


def _user_data(node):
    text = node.findtext("UserData") or ""
    try:
        value = json.loads(text)
    except (TypeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _cutout_mesh_ids(material):
    material_id = material.attrib.get("ID", "?")
    result = []
    primary = str(material.findtext("CutoutMeshID") or "").strip()
    if primary and primary != "-1":
        result.append(_positive_id(primary, f"Material {material_id} CutoutMeshID"))
    supplemental = material.find("SupplementalCutoutMeshIDs")
    if supplemental is not None:
        for child in supplemental.findall("CutoutMesh"):
            value = child.attrib.get("ID")
            if value not in {None, "", "-1"}:
                result.append(_positive_id(
                    value, f"Material {material_id} supplemental CutoutMesh ID"))
    unique = []
    duplicates = []
    for mesh_id in result:
        if mesh_id in unique:
            duplicates.append(mesh_id)
        else:
            unique.append(mesh_id)
    return unique, sorted(set(duplicates))


def _asset_index(root):
    assets = root.find("Assets")
    if assets is None:
        raise ReferenceRepairError("SPM has no Assets node")
    materials = {}
    materials_by_name = {}
    for node in assets.findall("Material_v8"):
        material_id = _positive_id(node.attrib.get("ID"), "Material_v8 ID")
        if material_id in materials:
            raise ReferenceRepairError(f"duplicate Material_v8 ID: {material_id}")
        mesh_ids, duplicate_mesh_ids = _cutout_mesh_ids(node)
        record = {
            "id": material_id,
            "name": str(node.attrib.get("Name") or ""),
            "node": node,
            "mesh_ids": mesh_ids,
            "duplicate_mesh_ids": duplicate_mesh_ids,
            "user_data": _user_data(node),
        }
        materials[material_id] = record
        materials_by_name.setdefault(record["name"], []).append(record)
    meshes = {}
    for node in assets.findall("Mesh"):
        mesh_id = _positive_id(node.attrib.get("ID"), "Mesh ID")
        if mesh_id in meshes:
            raise ReferenceRepairError(f"duplicate Mesh ID: {mesh_id}")
        meshes[mesh_id] = {
            "id": mesh_id,
            "name": str(node.attrib.get("Name") or ""),
            "filename": str(node.findtext("Filename") or "").strip(),
            "embedded": str(node.findtext("Embedded") or "").strip().lower(),
            "node": node,
            "user_data": _user_data(node),
        }
    return {
        "assets": assets,
        "materials": materials,
        "materials_by_name": materials_by_name,
        "meshes": meshes,
    }


def _normalized_generator_type(value):
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def _generator_records(root):
    """Return semantic Generators indexed by their stable GUID.

    Property pairs are retained as XML nodes because an authoritative source
    may provide the exact pair structure that has disappeared from the target.
    A pair is either complete or explicitly recorded as unpaired; callers can
    then allow only the special case where *both* properties are absent.
    """
    records = []
    by_guid = {}
    for generator_index, generator in enumerate(root.iter("Generator")):
        generator_type = str(
            generator.attrib.get("Type") or generator.findtext("Type") or "").strip()
        if _normalized_generator_type(generator_type) not in {"leafmesh", "frond"}:
            continue
        guid = str(
            generator.attrib.get("GUID")
            or generator.attrib.get("Guid")
            or generator.findtext("GUID")
            or generator.findtext("Guid")
            or ""
        ).strip()
        if not guid:
            raise ReferenceRepairError(
                f"semantic Generator {generator_index} has no GUID")
        guid_key = guid.lower()
        if guid_key in by_guid:
            raise ReferenceRepairError(
                f"duplicate semantic Generator GUID {guid!r}: "
                f"{by_guid[guid_key]['generator_index']} and {generator_index}")
        properties = generator.find("Properties")
        if properties is None:
            raise ReferenceRepairError(
                f"Generator {generator_index} GUID {guid!r} has no Properties node")
        by_name = {}
        actual_names = {}
        for prop in properties.findall("Property"):
            name = str(prop.findtext("Name") or "").strip()
            if not name:
                continue
            key = name.lower()
            if key in by_name:
                raise ReferenceRepairError(
                    f"Generator {generator_index} has duplicate Property name: {name}")
            by_name[key] = prop
            actual_names[key] = name
        material_prefixes = set()
        mesh_prefixes = set()
        for key, name in actual_names.items():
            suffix = name.rsplit(":", 1)[-1].lower()
            prefix = name.rsplit(":", 1)[0]
            if suffix == "material":
                material_prefixes.add(prefix.lower())
            elif suffix == "mesh":
                mesh_prefixes.add(prefix.lower())
        pairs = {}
        unpaired = {}
        for prefix_key in sorted(material_prefixes | mesh_prefixes):
            has_material = prefix_key in material_prefixes
            has_mesh = prefix_key in mesh_prefixes
            material_key = prefix_key + ":material"
            mesh_key = prefix_key + ":mesh"
            slot_name = actual_names.get(material_key) or actual_names.get(mesh_key)
            slot_name = slot_name.rsplit(":", 1)[0]
            if not (has_material and has_mesh):
                unpaired[prefix_key] = {
                    "slot": slot_name,
                    "has_material": has_material,
                    "has_mesh": has_mesh,
                }
                continue
            material_prop = by_name[prefix_key + ":material"]
            mesh_prop = by_name[prefix_key + ":mesh"]
            pairs[prefix_key] = {
                "generator_index": generator_index,
                "generator_guid": guid,
                "generator_name": str(generator.findtext("Name") or ""),
                "generator_type": generator_type,
                "slot": slot_name,
                "material_id": _int_value(
                    material_prop.findtext("Value"),
                    f"Generator {generator_index} {slot_name}:Material"),
                "mesh_id": _int_value(
                    mesh_prop.findtext("Value"),
                    f"Generator {generator_index} {slot_name}:Mesh"),
                "material_property": material_prop,
                "mesh_property": mesh_prop,
            }
        record = {
            "generator_index": generator_index,
            "generator_guid": guid,
            "generator_name": str(generator.findtext("Name") or ""),
            "generator_type": generator_type,
            "generator_type_key": _normalized_generator_type(generator_type),
            "node": generator,
            "properties": properties,
            "pairs": pairs,
            "unpaired": unpaired,
        }
        records.append(record)
        by_guid[guid_key] = record
    return records, by_guid


def _generator_slots(root):
    records, _ = _generator_records(root)
    slots = []
    for record in records:
        if record["unpaired"]:
            details = [
                f"{row['slot']} (Material={row['has_material']}, Mesh={row['has_mesh']})"
                for row in record["unpaired"].values()
            ]
            raise ReferenceRepairError(
                f"Generator {record['generator_index']} GUID "
                f"{record['generator_guid']!r} has unpaired Material/Mesh "
                f"properties: {details}")
        slots.extend(record["pairs"].values())
    return slots


def _validate_slot_assets(slots, index, ignored_keys=None):
    materials = index["materials"]
    meshes = index["meshes"]
    ignored_keys = set(ignored_keys or [])
    for slot in slots:
        slot_key = (
            str(slot.get("generator_guid") or "").lower(),
            str(slot.get("slot") or "").lower(),
        )
        if slot_key in ignored_keys:
            continue
        material_id = slot["material_id"]
        mesh_id = slot["mesh_id"]
        label = (
            f"Generator {slot['generator_index']} {slot['generator_name']!r} "
            f"slot {slot['slot']}")
        if material_id <= 0:
            if mesh_id > 0:
                raise ReferenceRepairError(
                    f"{label} has Mesh {mesh_id} without a valid Material")
            continue
        material = materials.get(material_id)
        if material is None:
            raise ReferenceRepairError(f"{label} references missing Material {material_id}")
        if mesh_id == -10:
            continue
        if mesh_id <= 0:
            raise ReferenceRepairError(
                f"{label} has invalid Mesh sentinel {mesh_id} for Material {material_id}")
        if mesh_id not in meshes:
            raise ReferenceRepairError(f"{label} references missing Mesh {mesh_id}")
        if mesh_id not in material["mesh_ids"]:
            raise ReferenceRepairError(
                f"{label} Mesh {mesh_id} is not owned by Material {material_id} "
                f"cutout list {material['mesh_ids']}")


def _resolve_external_mesh_path(spm_path, mesh):
    filename = mesh["filename"]
    if not filename:
        return None
    path = Path(filename.replace("/", os.sep))
    if not path.is_absolute():
        path = Path(spm_path).parent / path
    return Path(os.path.abspath(os.path.normpath(str(path))))


def _validate_managed_mesh(spm_path, material, mesh, validation_cache=None):
    material_data = material["user_data"]
    mesh_data = mesh["user_data"]
    if mesh_data.get("generator") != ATLAS_LEAF_GENERATOR \
            or mesh_data.get("kind") != "mesh":
        raise ReferenceRepairError(
            f"managed Material {material['id']} references unowned Mesh {mesh['id']}")
    scope = str(material_data.get("scope") or "")
    if not scope or str(mesh_data.get("scope") or "") != scope:
        raise ReferenceRepairError(
            f"managed Material {material['id']} and Mesh {mesh['id']} scope mismatch")
    material_group = str(material_data.get("group") or "")
    mesh_group = str(mesh_data.get("group") or "")
    if material_group and mesh_group != material_group:
        raise ReferenceRepairError(
            f"managed Material {material['id']} and Mesh {mesh['id']} group mismatch")
    external_path = _resolve_external_mesh_path(spm_path, mesh)
    if mesh["embedded"] == "false" and external_path is None:
        raise ReferenceRepairError(
            f"managed external Mesh {mesh['id']} has no Filename")
    exists = True
    if external_path is not None:
        cache_key = os.path.normcase(os.path.abspath(str(external_path)))
        if validation_cache is not None and cache_key in validation_cache:
            exists = validation_cache[cache_key]
        else:
            exists = external_path.is_file()
            if validation_cache is not None:
                validation_cache[cache_key] = exists
    if external_path is not None and not exists:
        raise ReferenceRepairError(
            f"managed Mesh {mesh['id']} FBX is missing: {external_path}")


def _mesh_leaf_ordinal(mesh):
    ordinals = set()
    for value in (mesh["name"], Path(mesh["filename"]).name if mesh["filename"] else ""):
        ordinals.update(int(match.group(1)) for match in LEAF_ORDINAL_RE.finditer(value))
    if not ordinals:
        raise ReferenceRepairError(
            f"managed Mesh {mesh['id']} has no unambiguous leaf_NN ordinal in "
            f"Name/Filename: {mesh['name']!r}, {mesh['filename']!r}")
    if len(ordinals) != 1:
        raise ReferenceRepairError(
            f"managed Mesh {mesh['id']} has conflicting leaf ordinals: {sorted(ordinals)}")
    ordinal = next(iter(ordinals))
    if ordinal <= 0:
        raise ReferenceRepairError(
            f"managed Mesh {mesh['id']} has invalid leaf ordinal {ordinal}")
    return ordinal


def _resolve_source_materials(
        source_set, index, ids_key="source_material_ids",
        names_key="source_material_names", label="source"):
    raw_ids = source_set.get(ids_key) or []
    raw_names = source_set.get(names_key) or []
    if not isinstance(raw_ids, list) or not isinstance(raw_names, list):
        raise ReferenceRepairError(
            f"{ids_key} and {names_key} must be lists")
    if not raw_ids or not raw_names:
        raise ReferenceRepairError(
            f"{label} requires explicit {ids_key} and {names_key}")
    ids = [_positive_id(value, f"{label} Material ID") for value in raw_ids]
    if len(ids) != len(set(ids)):
        raise ReferenceRepairError(f"duplicate {label} Material IDs: {ids}")
    names = [str(value).strip() for value in raw_names]
    if any(not name for name in names):
        raise ReferenceRepairError(f"{label} Material names cannot be empty")
    if len(ids) != len(names):
        raise ReferenceRepairError(
            f"{label} Material ID/name counts differ: {len(ids)} != {len(names)}")

    materials = index["materials"]
    # Names are descriptive evidence, never the identity key. SpeedTree files
    # can legally contain two Material_v8 assets with the same Name while their
    # IDs and atlas outputs differ.
    for material_id, name in zip(ids, names):
        material = materials.get(material_id)
        if material is None:
            raise ReferenceRepairError(
                f"{label} Material asset is missing: {material_id}")
        if material["name"] != name:
            raise ReferenceRepairError(
                f"{label} Material ID {material_id} is named "
                f"{material['name']!r}, expected {name!r}")
        if not material["mesh_ids"]:
            raise ReferenceRepairError(
                f"{label} Material {material_id} has no cutout Mesh IDs")
        if material["duplicate_mesh_ids"]:
            raise ReferenceRepairError(
                f"{label} Material {material_id} has duplicate cutout Mesh IDs: "
                f"{material['duplicate_mesh_ids']}")
        for mesh_id in material["mesh_ids"]:
            if mesh_id not in index["meshes"]:
                raise ReferenceRepairError(
                    f"{label} Material {material_id} references missing Mesh {mesh_id}")
    return ids


def _managed_atlas_outputs(spm_path, atlas_base, index, material_ids,
                           material_names, validation_cache=None):
    base = str(atlas_base or "").strip()
    if not base:
        raise ReferenceRepairError("source set requires atlas_base")
    base_lower = base.lower()
    if not material_ids or len(material_ids) != len(material_names):
        raise ReferenceRepairError(
            "source set requires explicit managed_material_ids and "
            "managed_material_names")
    managed = []
    for material_id, expected_name in zip(material_ids, material_names):
        material = index["materials"].get(material_id)
        if material is None:
            raise ReferenceRepairError(
                f"managed Material asset is missing: {material_id}")
        if material["name"] != expected_name:
            raise ReferenceRepairError(
                f"managed Material ID {material_id} is named "
                f"{material['name']!r}, expected {expected_name!r}")
        name_lower = material["name"].lower()
        if (name_lower != base_lower
                and not name_lower.startswith(base_lower + "_")):
            raise ReferenceRepairError(
                f"managed Material {material_id} {material['name']!r} does "
                f"not belong to atlas_base {base!r}")
        data = material["user_data"]
        if data.get("generator") != ATLAS_LEAF_GENERATOR \
                or data.get("kind") != "material":
            raise ReferenceRepairError(
                f"Material {material_id} is not an Atlas Leaf Builder output")
        if not material["mesh_ids"]:
            raise ReferenceRepairError(
                f"managed Material {material['id']} has no cutout Mesh IDs")
        managed.append(material)
    if not managed:
        raise ReferenceRepairError(
            f"no Atlas Leaf Builder managed Material matches atlas_base {base!r}")

    by_ordinal = {}
    ordinal_by_mesh = {}
    for material in managed:
        for mesh_id in material["mesh_ids"]:
            mesh = index["meshes"].get(mesh_id)
            if mesh is None:
                raise ReferenceRepairError(
                    f"managed Material {material['id']} references missing Mesh {mesh_id}")
            _validate_managed_mesh(
                spm_path, material, mesh, validation_cache=validation_cache)
            ordinal = _mesh_leaf_ordinal(mesh)
            target = {
                "ordinal": ordinal,
                "material_id": material["id"],
                "material_name": material["name"],
                "mesh_id": mesh_id,
                "mesh_name": mesh["name"],
            }
            if ordinal in by_ordinal:
                previous = by_ordinal[ordinal]
                raise ReferenceRepairError(
                    f"atlas {base!r} has duplicate leaf ordinal {ordinal}: "
                    f"Mesh {previous['mesh_id']} and Mesh {mesh_id}")
            by_ordinal[ordinal] = target
            ordinal_by_mesh[mesh_id] = ordinal
    return {
        "atlas_base": base,
        "materials": managed,
        "material_ids": {material["id"] for material in managed},
        "by_ordinal": by_ordinal,
        "ordinal_by_mesh": ordinal_by_mesh,
    }


def _normalize_source_sets(source_sets, spm_path, index,
                           validation_cache=None):
    if isinstance(source_sets, dict):
        source_sets = [source_sets]
    if not isinstance(source_sets, list) or not source_sets:
        raise ReferenceRepairError("source_sets must contain at least one source set")
    normalized = []
    used_source_ids = set()
    used_authoritative_ids = set()
    used_managed_ids = set()
    for raw in source_sets:
        if not isinstance(raw, dict):
            raise ReferenceRepairError("each source set must be an object")
        output_policy = str(
            raw.get("output_policy") or "managed").strip().lower()
        if output_policy not in OUTPUT_POLICIES:
            raise ReferenceRepairError(
                f"unsupported output_policy {output_policy!r}; expected one "
                f"of {sorted(OUTPUT_POLICIES)}")
        source_ids = _resolve_source_materials(raw, index)
        managed_ids = _resolve_source_materials(
            raw,
            index,
            ids_key="managed_material_ids",
            names_key="managed_material_names",
            label="managed",
        )
        managed = _managed_atlas_outputs(
            spm_path,
            raw.get("atlas_base"),
            index,
            managed_ids,
            list(raw.get("managed_material_names") or []),
            validation_cache=validation_cache,
        )
        authoritative_path = Path(
            str(raw.get("authoritative_source_spm") or "")).resolve()
        if not str(raw.get("authoritative_source_spm") or "").strip():
            raise ReferenceRepairError(
                "source set requires authoritative_source_spm")
        authoritative = _load_spm(authoritative_path)
        expected_authoritative_hash = str(
            raw.get("authoritative_source_sha256") or "")
        if (expected_authoritative_hash
                and authoritative["sha256"] != expected_authoritative_hash):
            raise ReferenceRepairError(
                f"authoritative source hash guard failed for {authoritative_path}: "
                f"expected {expected_authoritative_hash}, got "
                f"{authoritative['sha256']}")
        authoritative_index = _asset_index(authoritative["root"])
        authoritative_ids = _resolve_source_materials(
            raw,
            authoritative_index,
            ids_key="authoritative_source_material_ids",
            names_key="authoritative_source_material_names",
            label="authoritative source",
        )
        if len(source_ids) != len(authoritative_ids):
            raise ReferenceRepairError(
                "target and authoritative source Material counts differ: "
                f"{len(source_ids)} != {len(authoritative_ids)}")
        authoritative_to_target_source = dict(zip(
            authoritative_ids, source_ids))
        if output_policy == "restore_source":
            for authoritative_id, source_id in zip(
                    authoritative_ids, source_ids):
                authoritative_meshes = authoritative_index["materials"][
                    authoritative_id]["mesh_ids"]
                target_meshes = index["materials"][source_id]["mesh_ids"]
                if authoritative_meshes != target_meshes:
                    raise ReferenceRepairError(
                        "restore_source cutout ID order differs for authoritative "
                        f"Material {authoritative_id} and target Material "
                        f"{source_id}: {authoritative_meshes} != "
                        f"{target_meshes}")
                for mesh_id in authoritative_meshes:
                    authoritative_mesh = authoritative_index["meshes"][mesh_id]
                    target_mesh = index["meshes"][mesh_id]
                    authoritative_identity = (
                        authoritative_mesh["name"],
                        authoritative_mesh["embedded"],
                        authoritative_mesh["filename"].replace("\\", "/"),
                    )
                    target_identity = (
                        target_mesh["name"],
                        target_mesh["embedded"],
                        target_mesh["filename"].replace("\\", "/"),
                    )
                    if authoritative_identity != target_identity:
                        raise ReferenceRepairError(
                            "restore_source Mesh identity differs for Mesh "
                            f"{mesh_id}: {authoritative_identity!r} != "
                            f"{target_identity!r}")
        authoritative_records, authoritative_by_guid = _generator_records(
            authoritative["root"])
        raw_guid_map = raw.get("authoritative_generator_guid_map") or {}
        if not isinstance(raw_guid_map, dict):
            raise ReferenceRepairError(
                "authoritative_generator_guid_map must be an object")
        guid_map = {
            str(source_guid).strip().lower(): str(target_guid).strip().lower()
            for source_guid, target_guid in raw_guid_map.items()
            if str(source_guid).strip() and str(target_guid).strip()
        }
        if len(guid_map) != len(raw_guid_map):
            raise ReferenceRepairError(
                "authoritative_generator_guid_map contains an empty GUID")
        if len(set(guid_map.values())) != len(guid_map):
            raise ReferenceRepairError(
                "authoritative_generator_guid_map is not one-to-one")
        source_overlap = used_source_ids.intersection(source_ids)
        managed_overlap = used_managed_ids.intersection(managed["material_ids"])
        authoritative_overlap = used_authoritative_ids.intersection(
            authoritative_ids)
        if source_overlap:
            raise ReferenceRepairError(
                f"source Material IDs belong to more than one source set: {sorted(source_overlap)}")
        if managed_overlap:
            raise ReferenceRepairError(
                f"managed Material IDs match more than one atlas_base: {sorted(managed_overlap)}")
        if authoritative_overlap:
            raise ReferenceRepairError(
                "authoritative source Material IDs belong to more than one "
                f"source set: {sorted(authoritative_overlap)}")
        if set(source_ids).intersection(managed["material_ids"]):
            raise ReferenceRepairError(
                "source and managed Material IDs overlap in one source set")
        used_source_ids.update(source_ids)
        used_authoritative_ids.update(authoritative_ids)
        used_managed_ids.update(managed["material_ids"])
        normalized.append({
            "output_policy": output_policy,
            "atlas_base": managed["atlas_base"],
            "source_material_ids": list(source_ids),
            "source_material_names": [
                index["materials"][material_id]["name"] for material_id in source_ids
            ],
            "managed_material_ids": list(managed_ids),
            "managed_material_names": [
                index["materials"][material_id]["name"]
                for material_id in managed_ids
            ],
            "authoritative_source_spm": str(authoritative["path"]),
            "authoritative_source_sha256": authoritative["sha256"],
            "authoritative_source_material_ids": list(authoritative_ids),
            "authoritative_source_material_names": [
                authoritative_index["materials"][material_id]["name"]
                for material_id in authoritative_ids
            ],
            "authoritative_index": authoritative_index,
            "authoritative_records": authoritative_records,
            "authoritative_by_guid": authoritative_by_guid,
            "authoritative_generator_guid_map": guid_map,
            "authoritative_to_target_source": (
                authoritative_to_target_source),
            "managed": managed,
        })
    return normalized


def _source_slot_ordinal(slot, source_index, label):
    material = source_index["materials"].get(slot["material_id"])
    if material is None:
        raise ReferenceRepairError(
            f"{label} slot references missing Material {slot['material_id']}")
    mesh_id = slot["mesh_id"]
    if mesh_id == -10:
        # SpeedTree uses -10 for non-specific/random cutout selection.  It is
        # not evidence for ordinal 1 and must never be coerced into one.
        return None
    if mesh_id not in material["mesh_ids"]:
        raise ReferenceRepairError(
            f"{label} Mesh {mesh_id} is not in Material {material['id']} "
            f"cutout list {material['mesh_ids']}")
    if mesh_id not in source_index["meshes"]:
        raise ReferenceRepairError(
            f"{label} references missing Mesh asset {mesh_id}")
    return material["mesh_ids"].index(mesh_id) + 1


def _property_xml_with_value(node, value):
    cloned = copy.deepcopy(node)
    value_node = cloned.find("Value")
    if value_node is None:
        raise ReferenceRepairError("authoritative Property has no Value node")
    value_node.text = str(value)
    return ET.tostring(cloned, encoding="unicode")


def _public_source_sets(normalized_sets):
    keys = (
        "output_policy",
        "atlas_base",
        "source_material_ids",
        "source_material_names",
        "managed_material_ids",
        "managed_material_names",
        "authoritative_source_spm",
        "authoritative_source_sha256",
        "authoritative_source_material_ids",
        "authoritative_source_material_names",
        "authoritative_generator_guid_map",
    )
    return [
        {key: list(row[key]) if isinstance(row[key], list) else row[key]
         for key in keys}
        for row in normalized_sets
    ]


def build_repair_plan(spm, source_sets, validation_cache=None):
    """Build an authoritative, read-only, hash-guarded reference plan."""
    document = _load_spm(spm)
    index = _asset_index(document["root"])
    target_records, target_by_guid = _generator_records(document["root"])
    if not target_records:
        raise ReferenceRepairError("SPM has no Leaf Mesh/Frond Material/Mesh slots")
    for record in target_records:
        if record["unpaired"]:
            raise ReferenceRepairError(
                f"target Generator GUID {record['generator_guid']!r} has an "
                f"unpaired Material/Mesh Property: {list(record['unpaired'])}")
    normalized_sets = _normalize_source_sets(
        source_sets, document["path"], index,
        validation_cache=validation_cache)

    changes = []
    expected_keys = set()
    matched_source_slots = 0
    already_managed_slots = 0
    already_source_slots = 0
    restored_source_slots = 0
    authoritative_sentinel_slots = 0
    inserted_slot_pairs = 0
    for source_set in normalized_sets:
        target_source_ids = set(source_set["source_material_ids"])
        authoritative_ids = set(
            source_set["authoritative_source_material_ids"])
        managed = source_set["managed"]
        authoritative_index = source_set["authoritative_index"]
        for source_guid_key, target_guid_key in source_set[
                "authoritative_generator_guid_map"].items():
            source_record = source_set["authoritative_by_guid"].get(
                source_guid_key)
            target_record = target_by_guid.get(target_guid_key)
            if source_record is None or target_record is None:
                raise ReferenceRepairError(
                    f"Generator GUID map references a missing record: "
                    f"{source_guid_key!r} -> {target_guid_key!r}")
            if (source_record["generator_type_key"]
                    != target_record["generator_type_key"]
                    or source_record["generator_name"]
                    != target_record["generator_name"]):
                raise ReferenceRepairError(
                    f"Generator GUID map identity mismatch: "
                    f"{source_record['generator_name']!r}/"
                    f"{source_record['generator_type']!r} -> "
                    f"{target_record['generator_name']!r}/"
                    f"{target_record['generator_type']!r}")
        matched_authoritative_ids = set()
        set_expected_keys = set()
        for source_record in source_set["authoritative_records"]:
            source_guid_key = source_record["generator_guid"].lower()
            target_guid_key = source_set[
                "authoritative_generator_guid_map"].get(
                    source_guid_key, source_guid_key)
            target_record = target_by_guid.get(target_guid_key)
            source_pairs = [
                (prefix, pair)
                for prefix, pair in source_record["pairs"].items()
                if pair["material_id"] in authoritative_ids
            ]
            if not source_pairs:
                continue
            if target_record is None:
                raise ReferenceRepairError(
                    f"target is missing authoritative Generator GUID "
                    f"{source_record['generator_guid']!r} (mapped target GUID "
                    f"{target_guid_key!r})")
            if (target_record["generator_type_key"]
                    != source_record["generator_type_key"]):
                raise ReferenceRepairError(
                    f"Generator GUID {source_record['generator_guid']!r} type "
                    f"mismatch: target {target_record['generator_type']!r}, "
                    f"source {source_record['generator_type']!r}")
            for prefix, source_pair in source_pairs:
                matched_authoritative_ids.add(source_pair["material_id"])
                ordinal = _source_slot_ordinal(
                    source_pair,
                    authoritative_index,
                    f"authoritative Generator {source_record['generator_guid']!r} "
                    f"slot {source_pair['slot']}",
                )
                key = (target_record["generator_guid"].lower(), prefix)
                if key in expected_keys:
                    raise ReferenceRepairError(
                        f"Generator GUID {target_record['generator_guid']!r} "
                        f"slot {source_pair['slot']} belongs to more than one "
                        "source set")
                expected_keys.add(key)
                set_expected_keys.add(key)
                current = target_record["pairs"].get(prefix)

                if ordinal is None:
                    authoritative_sentinel_slots += 1
                    if source_set["output_policy"] == "managed":
                        if (current is not None
                                and current["material_id"] in (
                                    target_source_ids
                                    | managed["material_ids"])
                                and current["mesh_id"] == -10):
                            if current["material_id"] in managed["material_ids"]:
                                already_managed_slots += 1
                            else:
                                already_source_slots += 1
                            continue
                        raise ReferenceRepairError(
                            f"authoritative Generator GUID "
                            f"{source_record['generator_guid']!r} slot "
                            f"{source_pair['slot']} uses Mesh -10 and does not "
                            "prove a managed leaf ordinal")
                    target_source_id = source_set[
                        "authoritative_to_target_source"][
                            source_pair["material_id"]]
                    target_source = index["materials"][target_source_id]
                    output = {
                        "ordinal": None,
                        "material_id": target_source_id,
                        "material_name": target_source["name"],
                        "mesh_id": -10,
                        "mesh_name": "",
                    }
                elif source_set["output_policy"] == "managed":
                    output = managed["by_ordinal"].get(ordinal)
                    if output is None:
                        raise ReferenceRepairError(
                            f"Generator GUID "
                            f"{source_record['generator_guid']!r} slot "
                            f"{source_pair['slot']} needs leaf ordinal "
                            f"{ordinal}, but atlas "
                            f"{managed['atlas_base']!r} has none")
                else:
                    target_source_id = source_set[
                        "authoritative_to_target_source"][
                            source_pair["material_id"]]
                    target_source = index["materials"][target_source_id]
                    if ordinal > len(target_source["mesh_ids"]):
                        raise ReferenceRepairError(
                            f"target source Material {target_source_id} has no "
                            f"cutout ordinal {ordinal}")
                    target_mesh_id = target_source["mesh_ids"][ordinal - 1]
                    target_mesh = index["meshes"].get(target_mesh_id)
                    if target_mesh is None:
                        raise ReferenceRepairError(
                            f"target source Material {target_source_id} "
                            f"references missing Mesh {target_mesh_id}")
                    output = {
                        "ordinal": ordinal,
                        "material_id": target_source_id,
                        "material_name": target_source["name"],
                        "mesh_id": target_mesh_id,
                        "mesh_name": target_mesh["name"],
                    }
                after = {
                    "material_id": output["material_id"],
                    "material_name": output["material_name"],
                    "mesh_id": output["mesh_id"],
                    "mesh_name": output["mesh_name"],
                }
                common = {
                    "generator_index": target_record["generator_index"],
                    "generator_guid": target_record["generator_guid"],
                    "generator_name": target_record["generator_name"],
                    "generator_type": target_record["generator_type"],
                    "slot": source_pair["slot"],
                    "after": after,
                    "ordinal": ordinal,
                    "output_policy": source_set["output_policy"],
                }
                if current is None:
                    if prefix in target_record["unpaired"]:
                        raise ReferenceRepairError(
                            f"target Generator GUID "
                            f"{target_record['generator_guid']!r} slot "
                            f"{source_pair['slot']} is only partially present")
                    changes.append({
                        **common,
                        "operation": "insert_pair",
                        "before": None,
                        "reason": "authoritative_property_pair_missing",
                        "insert_xml": {
                            "material": _property_xml_with_value(
                                source_pair["material_property"],
                                output["material_id"],
                            ),
                            "mesh": _property_xml_with_value(
                                source_pair["mesh_property"],
                                output["mesh_id"],
                            ),
                        },
                    })
                    inserted_slot_pairs += 1
                    matched_source_slots += 1
                    continue

                before_material = current["material_id"]
                before_mesh = current["mesh_id"]
                if (before_material not in target_source_ids
                        and before_material not in managed["material_ids"]):
                    raise ReferenceRepairError(
                        f"target Generator GUID "
                        f"{target_record['generator_guid']!r} slot "
                        f"{current['slot']} references unrelated Material "
                        f"{before_material}")
                if (before_material, before_mesh) == (
                        output["material_id"], output["mesh_id"]):
                    if source_set["output_policy"] == "managed":
                        already_managed_slots += 1
                    else:
                        already_source_slots += 1
                    continue
                if source_set["output_policy"] == "restore_source":
                    if before_material in managed["material_ids"]:
                        reason = "authoritative_managed_to_source_restore"
                    elif before_mesh == -10:
                        reason = "authoritative_source_mesh_sentinel_restore"
                    elif before_mesh not in index["meshes"]:
                        reason = "authoritative_source_missing_mesh_restore"
                    else:
                        reason = "authoritative_source_ordinal_restore"
                    restored_source_slots += 1
                elif before_material in target_source_ids:
                    reason = "authoritative_source_to_managed"
                    matched_source_slots += 1
                elif before_mesh == -10:
                    reason = "authoritative_managed_mesh_sentinel"
                elif before_mesh not in index["meshes"]:
                    reason = "authoritative_missing_mesh_reference"
                elif before_mesh not in index["materials"][
                        before_material]["mesh_ids"]:
                    reason = "authoritative_material_mesh_mismatch"
                else:
                    reason = "authoritative_managed_ordinal_mismatch"
                changes.append({
                    **common,
                    "operation": "replace_values",
                    "before": {
                        "material_id": before_material,
                        "mesh_id": before_mesh,
                    },
                    "reason": reason,
                })

        if matched_authoritative_ids != authoritative_ids:
            raise ReferenceRepairError(
                f"authoritative source set {source_set['atlas_base']!r} did "
                "not resolve every Material ID; expected "
                f"{sorted(authoritative_ids)}, matched "
                f"{sorted(matched_authoritative_ids)}")
        # Extra target slots using this source/output scope have no proven
        # authoritative ordinal and therefore make the set ambiguous.
        for record in target_records:
            for prefix, pair in record["pairs"].items():
                if (pair["material_id"] in target_source_ids
                        or pair["material_id"] in managed["material_ids"]):
                    key = (record["generator_guid"].lower(), prefix)
                    if key not in set_expected_keys:
                        raise ReferenceRepairError(
                            f"target Generator GUID "
                            f"{record['generator_guid']!r} slot {pair['slot']} "
                            f"uses atlas {managed['atlas_base']!r} without an "
                            "authoritative source slot")

    changes.sort(key=lambda row: (
        row["generator_index"], row["slot"].lower(), row["operation"]))
    all_target_slots = [
        pair for record in target_records for pair in record["pairs"].values()
    ]
    _validate_slot_assets(all_target_slots, index, ignored_keys=expected_keys)
    return {
        "version": PLAN_VERSION,
        "kind": PLAN_KIND,
        "spm": str(document["path"]),
        "original_sha256": document["sha256"],
        "source_sets": _public_source_sets(normalized_sets),
        "changes": changes,
        "change_count": len(changes),
        "expected_slot_count": len(expected_keys),
        "inserted_slot_pair_count": inserted_slot_pairs,
        "matched_source_slots": matched_source_slots,
        "already_managed_slots": already_managed_slots,
        "already_source_slots": already_source_slots,
        "restored_source_slots": restored_source_slots,
        "authoritative_sentinel_slots": authoritative_sentinel_slots,
    }


def _replace_property_value(block, property_name, expected, replacement):
    matches = []
    for match in PROPERTY_BLOCK_RE.finditer(block):
        property_block = match.group(0)
        name_match = NAME_RE.search(property_block)
        if not name_match:
            continue
        name = html.unescape(name_match.group(1)).strip()
        if name.lower() == property_name.lower():
            matches.append(match)
    if len(matches) != 1:
        raise ReferenceRepairError(
            f"Property {property_name!r} resolves to {len(matches)} XML blocks")
    property_match = matches[0]
    property_block = property_match.group(0)
    value_match = VALUE_RE.search(property_block)
    if not value_match:
        raise ReferenceRepairError(f"Property {property_name!r} has no Value")
    current = html.unescape(value_match.group(2)).strip()
    if current != str(expected):
        raise ReferenceRepairError(
            f"Property {property_name!r} expected {expected}, found {current!r}")
    inner = value_match.group(2)
    leading = inner[:len(inner) - len(inner.lstrip())]
    trailing = inner[len(inner.rstrip()):]
    new_value = leading + str(replacement) + trailing
    new_property = (
        property_block[:value_match.start(2)]
        + new_value
        + property_block[value_match.end(2):]
    )
    return (
        block[:property_match.start()]
        + new_property
        + block[property_match.end():]
    )


def _insert_property_pair(block, change):
    existing_names = {
        html.unescape(match.group(1)).strip().lower()
        for property_match in PROPERTY_BLOCK_RE.finditer(block)
        for match in [NAME_RE.search(property_match.group(0))]
        if match
    }
    material_name = (change["slot"] + ":Material").lower()
    mesh_name = (change["slot"] + ":Mesh").lower()
    if material_name in existing_names or mesh_name in existing_names:
        raise ReferenceRepairError(
            f"cannot insert {change['slot']!r}; one Property already exists")
    closes = list(PROPERTIES_CLOSE_RE.finditer(block))
    if len(closes) != 1:
        raise ReferenceRepairError(
            f"Generator block has {len(closes)} Properties closing tags")
    insertion = (
        str(change["insert_xml"]["material"])
        + str(change["insert_xml"]["mesh"])
    )
    close = closes[0]
    return block[:close.start()] + insertion + block[close.start():]


def _patch_document(document, changes):
    changes_by_generator = {}
    for change in changes:
        changes_by_generator.setdefault(change["generator_index"], []).append(change)
    generator_matches = list(GENERATOR_BLOCK_RE.finditer(document["text"]))
    parsed_generator_count = len(list(document["root"].iter("Generator")))
    if len(generator_matches) != parsed_generator_count:
        raise ReferenceRepairError(
            f"Generator XML block count {len(generator_matches)} does not match "
            f"parsed count {parsed_generator_count}")
    chunks = []
    cursor = 0
    for generator_index, match in enumerate(generator_matches):
        block = match.group(0)
        for change in changes_by_generator.get(generator_index, []):
            if change.get("operation") == "insert_pair":
                block = _insert_property_pair(block, change)
                continue
            material_property = change["slot"] + ":Material"
            mesh_property = change["slot"] + ":Mesh"
            if change["before"]["material_id"] != change["after"]["material_id"]:
                block = _replace_property_value(
                    block,
                    material_property,
                    change["before"]["material_id"],
                    change["after"]["material_id"],
                )
            if change["before"]["mesh_id"] != change["after"]["mesh_id"]:
                block = _replace_property_value(
                    block,
                    mesh_property,
                    change["before"]["mesh_id"],
                    change["after"]["mesh_id"],
                )
        chunks.extend((document["text"][cursor:match.start()], block))
        cursor = match.end()
    chunks.append(document["text"][cursor:])
    xml_bytes = "".join(chunks).encode("utf-8")
    if document["had_bom"]:
        xml_bytes = codecs.BOM_UTF8 + xml_bytes
    try:
        ET.fromstring(xml_bytes)
    except ET.ParseError as exc:
        raise ReferenceRepairError(
            f"patched XML failed in-memory reparse: {exc}") from exc
    return gzip.compress(xml_bytes, mtime=0)


def _verify_after_values(document, changes):
    slots = {
        (row["generator_guid"].lower(), row["slot"].lower()): row
        for row in _generator_slots(document["root"])
    }
    index = _asset_index(document["root"])
    _validate_slot_assets(list(slots.values()), index)
    for change in changes:
        slot = slots.get((
            change["generator_guid"].lower(), change["slot"].lower()))
        if slot is None:
            raise ReferenceRepairError(
                f"patched Generator slot disappeared: {change['generator_index']} "
                f"{change['slot']}")
        actual = (slot["material_id"], slot["mesh_id"])
        expected = (
            change["after"]["material_id"], change["after"]["mesh_id"])
        if actual != expected:
            raise ReferenceRepairError(
                f"patched Generator slot mismatch for {change['generator_index']} "
                f"{change['slot']}: {actual} != {expected}")


def _write_atomic_json(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix="." + path.name + ".", suffix=".tmp", dir=str(path.parent))
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    except Exception:
        try:
            temp_path.unlink()
        except OSError:
            pass
        raise


def _make_validated_temp(plan):
    document = _load_spm(plan["spm"])
    patched = _patch_document(document, plan["changes"])
    fd, temp_name = tempfile.mkstemp(
        prefix="." + document["path"].stem + ".reference_repair.",
        suffix=".spm",
        dir=str(document["path"].parent),
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(patched)
            handle.flush()
            os.fsync(handle.fileno())
        temp_document = _load_spm(temp_path)
        _verify_after_values(temp_document, plan["changes"])
        second_plan = build_repair_plan(temp_path, plan["source_sets"])
        if second_plan["changes"]:
            raise ReferenceRepairError(
                "patched temporary SPM is not idempotent; a second plan still has changes")
        return temp_path, temp_document["sha256"]
    except Exception:
        try:
            temp_path.unlink()
        except OSError:
            pass
        raise


def _commit_spm(temp_path, target_path):
    os.replace(temp_path, target_path)


def _restore_backup(backup_path, target_path):
    target_path = Path(target_path)
    fd, temp_name = tempfile.mkstemp(
        prefix="." + target_path.stem + ".rollback.",
        suffix=".spm",
        dir=str(target_path.parent),
    )
    os.close(fd)
    temp_path = Path(temp_name)
    try:
        shutil.copy2(backup_path, temp_path)
        os.replace(temp_path, target_path)
    except Exception:
        try:
            temp_path.unlink()
        except OSError:
            pass
        raise


def _backup_location(spm_path, transaction_id, backup_root, index):
    if backup_root is None:
        directory = (
            Path(spm_path).parent
            / "_spm_backups"
            / transaction_id
            / f"{index:04d}_{Path(spm_path).stem}"
        )
    else:
        directory = (
            Path(backup_root).resolve()
            / transaction_id
            / f"{index:04d}_{Path(spm_path).stem}"
        )
    directory.mkdir(parents=True, exist_ok=False)
    return directory


def _validate_input_plan(plan):
    if not isinstance(plan, dict):
        raise ReferenceRepairError("repair plan must be an object")
    if plan.get("version") != PLAN_VERSION or plan.get("kind") != PLAN_KIND:
        raise ReferenceRepairError("unsupported reference repair plan version/kind")
    if not plan.get("spm") or not plan.get("original_sha256"):
        raise ReferenceRepairError("repair plan is missing spm/original_sha256")
    document = _load_spm(plan["spm"])
    if document["sha256"] != plan["original_sha256"]:
        raise ReferenceRepairError(
            f"hash guard failed for {document['path']}: expected "
            f"{plan['original_sha256']}, got {document['sha256']}")
    fresh = build_repair_plan(document["path"], plan.get("source_sets"))
    if fresh["source_sets"] != plan.get("source_sets") \
            or fresh["changes"] != plan.get("changes"):
        raise ReferenceRepairError(
            f"repair plan no longer matches current SPM evidence: {document['path']}")
    return fresh


def validate_repair_plans(plans):
    """Fully validate plans through temporary patched SPMs without committing.

    This exercises the same hash guards, fresh evidence rebuild, XML patch,
    gzip write, reparse, asset-reference validation, and idempotency checks as
    :func:`apply_repair_plans`.  Target SPM bytes are verified again before the
    temporary files are removed, and no backup directory or manifest is made.
    """
    if isinstance(plans, dict):
        plans = [plans]
    if not isinstance(plans, list) or not plans:
        raise ReferenceRepairError(
            "validation requires at least one repair plan")

    prepared = []
    seen_targets = set()
    try:
        for raw_plan in plans:
            plan = _validate_input_plan(raw_plan)
            target = Path(plan["spm"])
            target_key = os.path.normcase(os.path.abspath(str(target)))
            if target_key in seen_targets:
                raise ReferenceRepairError(
                    f"duplicate SPM in validation: {target}")
            seen_targets.add(target_key)

            temp_path = None
            patched_sha256 = plan["original_sha256"]
            status = "unchanged"
            if plan["changes"]:
                temp_path, patched_sha256 = _make_validated_temp(plan)
                status = "validated"
            prepared.append({
                "plan": plan,
                "target": target,
                "temp": temp_path,
                "status": status,
                "patched_sha256": patched_sha256,
            })

        for entry in prepared:
            current_sha256 = sha256_file(entry["target"])
            if current_sha256 != entry["plan"]["original_sha256"]:
                raise ReferenceRepairError(
                    f"target changed during validation: {entry['target']}")
        return {
            "status": "validated",
            "spms": [str(entry["target"]) for entry in prepared],
            "change_count": sum(
                entry["plan"]["change_count"] for entry in prepared),
            "results": [{
                "spm": str(entry["target"]),
                "status": entry["status"],
                "change_count": entry["plan"]["change_count"],
                "original_sha256": entry["plan"]["original_sha256"],
                "patched_sha256": entry["patched_sha256"],
            } for entry in prepared],
        }
    finally:
        for entry in prepared:
            temp_path = entry.get("temp")
            if temp_path is not None:
                try:
                    Path(temp_path).unlink()
                except OSError:
                    pass


def apply_repair_plans(plans, backup_root=None):
    """Apply one or more plans as a single all-or-nothing transaction."""
    if isinstance(plans, dict):
        plans = [plans]
    if not isinstance(plans, list) or not plans:
        raise ReferenceRepairError("apply requires at least one repair plan")

    prepared = []
    seen_targets = set()
    try:
        for raw_plan in plans:
            plan = _validate_input_plan(raw_plan)
            target = Path(plan["spm"])
            target_key = os.path.normcase(os.path.abspath(str(target)))
            if target_key in seen_targets:
                raise ReferenceRepairError(f"duplicate SPM in transaction: {target}")
            seen_targets.add(target_key)
            if not plan["changes"]:
                prepared.append({
                    "plan": plan,
                    "target": target,
                    "temp": None,
                    "patched_sha256": plan["original_sha256"],
                    "backup": None,
                    "manifest": None,
                    "status": "unchanged",
                })
                continue
            temp_path, patched_sha256 = _make_validated_temp(plan)
            prepared.append({
                "plan": plan,
                "target": target,
                "temp": temp_path,
                "patched_sha256": patched_sha256,
                "backup": None,
                "manifest": None,
                "status": "prepared",
            })

        changed = [entry for entry in prepared if entry["temp"] is not None]
        if not changed:
            return {
                "status": "unchanged",
                "transaction_id": None,
                "spms": [str(entry["target"]) for entry in prepared],
                "manifests": [],
            }

        transaction_id = (
            "generator_reference_repair_"
            + datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            + "_"
            + uuid.uuid4().hex[:8]
        )
        for index, entry in enumerate(changed, 1):
            backup_dir = _backup_location(
                entry["target"], transaction_id, backup_root, index)
            backup_path = backup_dir / entry["target"].name
            shutil.copy2(entry["target"], backup_path)
            if sha256_file(backup_path) != entry["plan"]["original_sha256"]:
                raise ReferenceRepairError(
                    f"backup hash verification failed: {backup_path}")
            manifest_path = backup_dir / (
                entry["target"].stem + ".generator_reference_repair.json")
            manifest = {
                "version": PLAN_VERSION,
                "kind": PLAN_KIND,
                "transaction_id": transaction_id,
                "status": "prepared",
                "spm": str(entry["target"]),
                "backup": str(backup_path),
                "original_sha256": entry["plan"]["original_sha256"],
                "patched_sha256": entry["patched_sha256"],
                "changes": entry["plan"]["changes"],
            }
            _write_atomic_json(manifest_path, manifest)
            entry["backup"] = backup_path
            entry["manifest"] = manifest_path
            entry["manifest_data"] = manifest

        for entry in changed:
            _commit_spm(entry["temp"], entry["target"])
            entry["temp"] = None
            entry["status"] = "committed"
            if sha256_file(entry["target"]) != entry["patched_sha256"]:
                raise ReferenceRepairError(
                    f"post-commit hash verification failed: {entry['target']}")
            committed_document = _load_spm(entry["target"])
            _verify_after_values(committed_document, entry["plan"]["changes"])

        for entry in changed:
            entry["manifest_data"]["status"] = "applied"
            _write_atomic_json(entry["manifest"], entry["manifest_data"])
            entry["status"] = "applied"
        return {
            "status": "applied",
            "transaction_id": transaction_id,
            "spms": [str(entry["target"]) for entry in prepared],
            "manifests": [str(entry["manifest"]) for entry in changed],
        }
    except Exception as exc:
        rollback_errors = []
        for entry in prepared:
            if entry.get("backup") is None:
                continue
            try:
                _restore_backup(entry["backup"], entry["target"])
                if sha256_file(entry["target"]) != entry["plan"]["original_sha256"]:
                    raise ReferenceRepairError("restored hash does not match original")
                if entry.get("manifest_data") is not None:
                    entry["manifest_data"]["status"] = "rolled_back"
                    _write_atomic_json(entry["manifest"], entry["manifest_data"])
                entry["status"] = "rolled_back"
            except Exception as rollback_exc:
                rollback_errors.append(f"{entry['target']}: {rollback_exc}")
        detail = f"reference repair transaction failed: {exc}"
        if rollback_errors:
            detail += " | rollback failures: " + " | ".join(rollback_errors)
        raise ReferenceRepairError(detail) from exc
    finally:
        for entry in prepared:
            temp_path = entry.get("temp")
            if temp_path is not None:
                try:
                    Path(temp_path).unlink()
                except OSError:
                    pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spm", required=True)
    parser.add_argument(
        "--source-sets-json", required=True,
        help="JSON file containing one source-set object or a list of source sets")
    parser.add_argument("--plan-out")
    args = parser.parse_args()
    source_sets = json.loads(
        Path(args.source_sets_json).read_text(encoding="utf-8"))
    plan = build_repair_plan(args.spm, source_sets)
    text = json.dumps(plan, indent=2, ensure_ascii=False)
    if args.plan_out:
        Path(args.plan_out).write_text(text, encoding="utf-8")
    else:
        print(text)


if __name__ == "__main__":
    main()
