"""Read-only SpeedTree leaf-reference and material-export preflight.

The SK handoff is complete only when semantic ``Leaf Mesh``/``Frond``
Generator slots reference an existing Material and a Mesh owned by that same
Material.  Atlas Leaf Mesh Builder assets that merely exist inside the SPM are
not completion evidence.

This module is intentionally stdlib-only so the GUI and a factory-startup
Blender process can use the exact same contract.
"""
from __future__ import annotations

import copy
from collections import Counter
import functools
import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path


ATLAS_LEAF_GENERATOR = "Atlas Leaf Mesh Builder"
SEMANTIC_GENERATOR_TYPES = {"frond", "leafmesh"}
REPO_DIR = Path(__file__).resolve().parent.parent
if str(REPO_DIR) not in sys.path:
    sys.path.insert(0, str(REPO_DIR))

from pcg_st9_texture_batch.pcg_texture_audit import (  # noqa: E402
    active_material_ids,
    extract_material_image_refs,
    leaf_generator_bindings,
    mesh_asset_ids,
    save_spm_analysis_cache,
    visible_material_ids,
)
from speedtree_pipeline_contract import open_spm_binary  # noqa: E402
from speedtree_texture_contract import normalize_material_key  # noqa: E402


def _normalized_generator_type(value):
    return re.sub(r"[^a-z0-9]+", "", str(value or "").casefold())


def _atlas_family_key(value):
    """Return the source/atlas family key without exposing naming to users."""
    name = str(value or "").strip().casefold()
    if name.endswith("_mat"):
        name = name[:-4]
    name = re.sub(r"(^|_)atlas(?=_|$)", r"\1", name)
    return re.sub(r"_+", "_", name).strip("_")


def _replacement_source_slot(slot, managed_family_keys):
    """Whether a visible source slot is part of the authored leaf-atlas job.

    A tree can contain several independent card families at once.  The mere
    presence of one managed Atlas material must not turn every other Leaf Mesh
    or Frond family into a replacement target.  Require the source slot and
    managed output to share the same normalized family identity.
    """
    generator_type = _normalized_generator_type(slot.get("generator_type"))
    return bool(
        generator_type in SEMANTIC_GENERATOR_TYPES
        and _atlas_family_key(slot.get("material_name")) in managed_family_keys
    )


def _replacement_counts(slots, managed_materials):
    managed_family_keys = {
        _atlas_family_key(item.get("name")) for item in managed_materials
    }
    source = 0
    connected = 0
    for slot in slots:
        candidate = bool(
            slot.get("visible")
            and slot.get("valid")
            and (
                slot.get("managed")
                or _replacement_source_slot(slot, managed_family_keys)
            )
        )
        slot["atlas_replacement_candidate"] = candidate
        if not candidate:
            continue
        if slot.get("managed"):
            connected += 1
        else:
            source += 1
    return source, connected


def _atlas_replacement_required(
    managed_materials,
    blocking_issues,
    replacement_source_slot_count,
):
    """Require Atlas handoff only when an exported source slot proves intent.

    A managed Atlas material can remain in an SPM as an unused asset definition.
    Its marker alone is not evidence that this model exports Atlas leaf cards.
    In particular, zero semantic slots must stay nonblocking: there is no
    generator connection for the artist or the batch to repair.
    """
    return bool(
        managed_materials
        and not blocking_issues
        and replacement_source_slot_count > 0
    )


def _managed_ownership_provenance(managed_materials):
    """Expose that the fast status path has marker evidence, not repair proof."""
    names = [str(item.get("name") or "") for item in managed_materials]
    if not names:
        return {"status": "not_applicable", "material_names": []}
    return {
        "status": "marker_only",
        "material_names": names,
        "issue_code": "ATLAS_OWNERSHIP_PROVENANCE_MISMATCH",
        "reason": (
            "Atlas builder material marker was found, but strict material/mesh "
            "ownership and authoritative source_signature were not proven by "
            "the fast read-only status path"
        ),
    }


def _integer(value):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _user_data(node):
    try:
        value = json.loads(node.findtext("UserData") or "")
    except (TypeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _is_managed_material(node):
    data = _user_data(node)
    return (
        data.get("generator") == ATLAS_LEAF_GENERATOR
        and data.get("kind") == "material"
    )


def _cutout_mesh_ids(material):
    result = []
    primary = _integer(material.findtext("CutoutMeshID"))
    if primary not in {None, -1}:
        result.append(primary)
    supplemental = material.find("SupplementalCutoutMeshIDs")
    if supplemental is not None:
        for child in supplemental.findall("CutoutMesh"):
            mesh_id = _integer(child.attrib.get("ID"))
            if mesh_id not in {None, -1}:
                result.append(mesh_id)
    return list(dict.fromkeys(result))


def _guid(node):
    return str(
        node.attrib.get("GUID")
        or node.attrib.get("Guid")
        or node.findtext("GUID")
        or node.findtext("Guid")
        or ""
    ).strip()


def _is_hidden(node):
    return str(node.findtext("Hidden") or "").strip().casefold() in {
        "1", "true", "yes",
    }


def _file_key(path):
    candidate = Path(path)
    stat = candidate.stat()
    return os.path.normcase(os.path.abspath(str(candidate))), stat.st_size, stat.st_mtime_ns


def _load_spm_root(path):
    path = Path(path)
    try:
        with open_spm_binary(path) as handle:
            return ET.parse(handle).getroot()
    except (OSError, EOFError, ET.ParseError) as exc:
        raise ValueError(f"SPM XML 파싱 실패: {exc}") from exc


def _effective_visibility(root):
    hidden_by_guid = {}
    for generator in root.iter("Generator"):
        guid = _guid(generator)
        if guid:
            hidden_by_guid[guid] = _is_hidden(generator)

    parent_by_guid = {}
    for link in root.iter("Link"):
        source = str(link.findtext("SourceGUID") or "").strip()
        target = str(link.findtext("TargetGUID") or "").strip()
        if source and target:
            parent_by_guid[target] = source

    def visible(guid, own_hidden):
        if own_hidden:
            return False
        seen = set()
        while guid and guid not in seen:
            seen.add(guid)
            if hidden_by_guid.get(guid):
                return False
            guid = parent_by_guid.get(guid, "")
        return True

    return visible


@functools.lru_cache(maxsize=512)
def _inspect_spm_cached(path_text, _size, _mtime_ns):
    path = Path(path_text)
    try:
        root = _load_spm_root(path)
    except (OSError, ValueError) as exc:
        return {
            "status": "inspection_error",
            "spm": str(path),
            "error": str(exc),
            "issues": [str(exc)],
            "replacement_needed": False,
            "expected_visible_material_names": [],
        }

    materials = {}
    duplicate_material_ids = []
    for material in root.iter("Material_v8"):
        material_id = _integer(material.attrib.get("ID"))
        if material_id is None or material_id <= 0:
            continue
        if material_id in materials:
            duplicate_material_ids.append(material_id)
            continue
        materials[material_id] = {
            "id": material_id,
            "name": str(material.attrib.get("Name") or ""),
            "managed": _is_managed_material(material),
            "mesh_ids": _cutout_mesh_ids(material),
            "user_data": _user_data(material),
        }

    mesh_asset_ids = set()
    duplicate_mesh_ids = []
    for mesh in root.iter("Mesh"):
        mesh_id = _integer(mesh.attrib.get("ID"))
        if mesh_id is None or mesh_id <= 0:
            continue
        if mesh_id in mesh_asset_ids:
            duplicate_mesh_ids.append(mesh_id)
        mesh_asset_ids.add(mesh_id)

    issues = []
    blocking_issues = []
    if duplicate_material_ids:
        issues.append(
            "중복 Material ID: " + ", ".join(map(str, sorted(set(duplicate_material_ids))))
        )
    if duplicate_mesh_ids:
        issues.append(
            "중복 Mesh ID: " + ", ".join(map(str, sorted(set(duplicate_mesh_ids))))
        )

    visible = _effective_visibility(root)
    slots = []
    managed_slot_count = 0
    source_slot_count = 0
    visible_managed_slot_count = 0
    visible_source_slot_count = 0
    expected_visible_material_names = []

    for generator_index, generator in enumerate(root.iter("Generator")):
        generator_type = str(
            generator.attrib.get("Type") or generator.findtext("Type") or ""
        ).strip()
        if _normalized_generator_type(generator_type) not in SEMANTIC_GENERATOR_TYPES:
            continue
        guid = _guid(generator)
        generator_name = str(generator.findtext("Name") or "")
        is_visible = visible(guid, _is_hidden(generator))
        properties = generator.find("Properties")
        by_name = {}
        if properties is not None:
            for prop in properties.findall("Property"):
                name = str(prop.findtext("Name") or "").strip()
                if name:
                    by_name[name.casefold()] = (name, prop.findtext("Value"))

        material_prefixes = {}
        mesh_prefixes = {}
        for name_key, (name, value) in by_name.items():
            if name_key.endswith(":material"):
                material_prefixes[name_key[:-len(":material")]] = (name, value)
            elif name_key.endswith(":mesh"):
                mesh_prefixes[name_key[:-len(":mesh")]] = (name, value)

        for prefix_key in sorted(set(material_prefixes) | set(mesh_prefixes)):
            material_pair = material_prefixes.get(prefix_key)
            mesh_pair = mesh_prefixes.get(prefix_key)
            slot_name = (material_pair or mesh_pair)[0].rsplit(":", 1)[0]
            material_id = _integer(material_pair[1]) if material_pair else None
            mesh_id = _integer(mesh_pair[1]) if mesh_pair else None
            row_issues = []
            if material_pair is None or mesh_pair is None:
                row_issues.append("Material/Mesh Property 쌍이 불완전함")
            elif material_id is None or mesh_id is None:
                row_issues.append("Material/Mesh ID가 정수가 아님")
            elif material_id <= 0:
                if mesh_id > 0:
                    row_issues.append("유효 Material 없이 Mesh만 연결됨")
            else:
                material = materials.get(material_id)
                if material is None:
                    row_issues.append(f"Material {material_id} 없음")
                elif mesh_id == -10:
                    pass
                elif mesh_id is None or mesh_id <= 0:
                    row_issues.append(f"Mesh sentinel {mesh_id}가 유효하지 않음")
                elif mesh_id not in mesh_asset_ids:
                    row_issues.append(f"Mesh {mesh_id} 자산 없음")
                elif mesh_id not in material["mesh_ids"]:
                    row_issues.append(
                        f"Mesh {mesh_id}가 Material {material_id} 소유 cutout이 아님"
                    )

            material = materials.get(material_id) if material_id else None
            active = bool(material_id and material_id > 0)
            valid = active and not row_issues
            managed = bool(valid and material and material["managed"])
            source = bool(valid and material and not material["managed"])
            if managed:
                managed_slot_count += 1
                visible_managed_slot_count += int(is_visible)
            elif source:
                source_slot_count += 1
                visible_source_slot_count += int(is_visible)
            if valid and is_visible and material["name"] \
                    and material["name"] not in expected_visible_material_names:
                expected_visible_material_names.append(material["name"])

            label = (
                f"{generator_type} {generator_name or generator_index} / {slot_name}"
            )
            slot_issues = [f"{label}: {problem}" for problem in row_issues]
            issues.extend(slot_issues)
            if is_visible:
                blocking_issues.extend(slot_issues)
            slots.append({
                "generator_index": generator_index,
                "generator_guid": guid,
                "generator_type": generator_type,
                "generator_name": generator_name,
                "slot_prefix": slot_name,
                "material_id": material_id,
                "material_name": material["name"] if material else "",
                "mesh_id": mesh_id,
                "visible": is_visible,
                "valid": valid,
                "managed": managed,
                "issues": row_issues,
            })

    managed_materials = [
        material for material in materials.values() if material["managed"]
    ]
    replacement_source_slot_count, replacement_connected_slot_count = (
        _replacement_counts(slots, managed_materials)
    )
    active_slot_count = sum(
        1 for slot in slots if slot.get("material_id") and slot["material_id"] > 0
    )
    invalid_slot_count = sum(
        1 for slot in slots if slot.get("material_id") and slot["material_id"] > 0
        and not slot["valid"]
    )

    replacement_needed = _atlas_replacement_required(
        managed_materials,
        blocking_issues,
        replacement_source_slot_count,
    )
    if blocking_issues:
        status = "invalid_references"
    elif replacement_needed:
        status = "replacement_needed"
    elif visible_managed_slot_count:
        status = "managed_connected"
    elif visible_source_slot_count:
        status = "source_only"
    else:
        status = "no_leaf_slots"

    return {
        "status": status,
        "spm": str(path),
        "issues": issues,
        "blocking_issues": blocking_issues,
        "nonblocking_issues": [
            issue for issue in issues if issue not in set(blocking_issues)
        ],
        "replacement_needed": replacement_needed,
        "semantic_slot_count": len(slots),
        "active_slot_count": active_slot_count,
        "invalid_slot_count": invalid_slot_count,
        "managed_slot_count": managed_slot_count,
        "source_slot_count": source_slot_count,
        "visible_managed_slot_count": visible_managed_slot_count,
        "visible_source_slot_count": visible_source_slot_count,
        "replacement_source_slot_count": replacement_source_slot_count,
        "replacement_connected_slot_count": replacement_connected_slot_count,
        "managed_material_count": len(managed_materials),
        "managed_material_names": [item["name"] for item in managed_materials],
        "managed_ownership_provenance": _managed_ownership_provenance(
            managed_materials
        ),
        "expected_visible_material_names": expected_visible_material_names,
        "slots": slots,
    }


@functools.lru_cache(maxsize=512)
def _inspect_spm_fast_cached(path_text, _size, _mtime_ns):
    """Build the contract from the shared compact PCG SPM analysis.

    The PCG parser uses bounded regex extraction rather than a full XML DOM.
    Embedded SpeedTree geometry makes a DOM several times slower and can use
    gigabytes for the larger trees, while this metadata is exactly the subset
    both tools need.
    """
    path = Path(path_text)
    try:
        material_rows = extract_material_image_refs(path)
        bindings = leaf_generator_bindings(path)
        mesh_ids = {_integer(value) for value in mesh_asset_ids(path)}
        mesh_ids.discard(None)
    except (OSError, ValueError) as exc:
        return {
            "status": "inspection_error",
            "spm": str(path),
            "error": str(exc),
            "issues": [str(exc)],
            "replacement_needed": False,
            "expected_visible_material_names": [],
        }

    materials = {}
    issues = []
    blocking_issues = []
    for row in material_rows:
        material_id = _integer(row.get("material_id"))
        if material_id is None or material_id <= 0:
            continue
        if material_id in materials:
            issues.append(f"중복 Material ID: {material_id}")
            continue
        cutout_ids = []
        for value in row.get("cutout_mesh_ids") or []:
            mesh_id = _integer(value)
            if mesh_id is not None and mesh_id not in cutout_ids:
                cutout_ids.append(mesh_id)
        materials[material_id] = {
            "id": material_id,
            "name": str(row.get("material_name") or ""),
            "managed": bool(row.get("managed_leaf_output")),
            "mesh_ids": cutout_ids,
        }

    slots = []
    managed_slot_count = 0
    source_slot_count = 0
    visible_managed_slot_count = 0
    visible_source_slot_count = 0
    expected_visible_material_names = []
    for binding in bindings:
        material_id = _integer(binding.get("material_id"))
        mesh_id = _integer(binding.get("mesh_id"))
        row_issues = []
        if not binding.get("mesh_property"):
            row_issues.append("Material/Mesh Property 쌍이 불완전함")
        elif material_id is None or mesh_id is None:
            row_issues.append("Material/Mesh ID가 정수가 아님")
        elif material_id <= 0:
            if mesh_id > 0:
                row_issues.append("유효 Material 없이 Mesh만 연결됨")
        else:
            material = materials.get(material_id)
            if material is None:
                row_issues.append(f"Material {material_id} 없음")
            elif mesh_id == -10:
                pass
            elif mesh_id <= 0:
                row_issues.append(f"Mesh sentinel {mesh_id}가 유효하지 않음")
            elif mesh_id not in mesh_ids:
                row_issues.append(f"Mesh {mesh_id} 자산 없음")
            elif mesh_id not in material["mesh_ids"]:
                row_issues.append(
                    f"Mesh {mesh_id}가 Material {material_id} 소유 cutout이 아님"
                )

        material = materials.get(material_id) if material_id else None
        active = bool(material_id and material_id > 0)
        valid = active and not row_issues
        managed = bool(valid and material and material["managed"])
        is_visible = bool(binding.get("visible", True))
        if managed:
            managed_slot_count += 1
            visible_managed_slot_count += int(is_visible)
        elif valid and material:
            source_slot_count += 1
            visible_source_slot_count += int(is_visible)
        if valid and is_visible and material["name"] \
                and material["name"] not in expected_visible_material_names:
            expected_visible_material_names.append(material["name"])

        label = (
            f"{binding.get('generator_type', '?')} "
            f"{binding.get('generator_name') or binding.get('generator_index', '?')} / "
            f"{binding.get('slot_prefix', '?')}"
        )
        slot_issues = [f"{label}: {problem}" for problem in row_issues]
        issues.extend(slot_issues)
        if is_visible:
            blocking_issues.extend(slot_issues)
        slots.append({
            "generator_index": binding.get("generator_index"),
            "generator_guid": binding.get("generator_guid", ""),
            "generator_type": binding.get("generator_type", ""),
            "generator_name": binding.get("generator_name", ""),
            "slot_prefix": binding.get("slot_prefix", ""),
            "material_id": material_id,
            "material_name": material["name"] if material else "",
            "mesh_id": mesh_id,
            "visible": is_visible,
            "graph_visible": bool(binding.get("graph_visible", is_visible)),
            "generated_node_count": int(
                binding.get("generated_node_count") or 0
            ),
            "export_participates": bool(
                binding.get("export_participates", is_visible)
            ),
            "valid": valid,
            "managed": managed,
            "issues": row_issues,
        })

    managed_materials = [
        material for material in materials.values() if material["managed"]
    ]
    replacement_source_slot_count, replacement_connected_slot_count = (
        _replacement_counts(slots, managed_materials)
    )
    active_slot_count = sum(
        1 for slot in slots if slot.get("material_id") and slot["material_id"] > 0
    )
    invalid_slot_count = sum(
        1 for slot in slots if slot.get("material_id") and slot["material_id"] > 0
        and not slot["valid"]
    )
    replacement_needed = _atlas_replacement_required(
        managed_materials,
        blocking_issues,
        replacement_source_slot_count,
    )
    if blocking_issues:
        status = "invalid_references"
    elif replacement_needed:
        status = "replacement_needed"
    elif visible_managed_slot_count:
        status = "managed_connected"
    elif visible_source_slot_count:
        status = "source_only"
    else:
        status = "no_leaf_slots"

    return {
        "status": status,
        "spm": str(path),
        "issues": issues,
        "blocking_issues": blocking_issues,
        "nonblocking_issues": [
            issue for issue in issues if issue not in set(blocking_issues)
        ],
        "replacement_needed": replacement_needed,
        "semantic_slot_count": len(slots),
        "active_slot_count": active_slot_count,
        "invalid_slot_count": invalid_slot_count,
        "managed_slot_count": managed_slot_count,
        "source_slot_count": source_slot_count,
        "visible_managed_slot_count": visible_managed_slot_count,
        "visible_source_slot_count": visible_source_slot_count,
        "replacement_source_slot_count": replacement_source_slot_count,
        "replacement_connected_slot_count": replacement_connected_slot_count,
        "managed_material_count": len(managed_materials),
        "managed_material_names": [item["name"] for item in managed_materials],
        "managed_ownership_provenance": _managed_ownership_provenance(
            managed_materials
        ),
        "expected_visible_material_names": expected_visible_material_names,
        "slots": slots,
    }


def inspect_spm_leaf_contract(spm_path):
    """Return a cached, caller-safe SPM leaf-reference contract."""
    path = Path(spm_path)
    try:
        path_text, size, mtime_ns = _file_key(path)
    except OSError as exc:
        return {
            "status": "inspection_error",
            "spm": str(path),
            "error": str(exc),
            "issues": [str(exc)],
            "replacement_needed": False,
            "expected_visible_material_names": [],
        }
    return copy.deepcopy(_inspect_spm_fast_cached(path_text, size, mtime_ns))


def leaf_contract_user_message(contract):
    """Translate internal contract counters into an artist-facing message."""
    status = str(contract.get("status") or "")
    if status == "inspection_error":
        return False, (
            "SPM의 잎 재질 연결을 읽지 못함 — "
            + str(contract.get("error") or "검사 보고서 확인")
        )
    if status == "invalid_references":
        issues = contract.get("blocking_issues") or contract.get("issues") or []
        detail = str(issues[0]) if issues else "Material/Mesh 연결 오류"
        return False, (
            "현재 내보내는 잎 노드의 재질 연결이 끊어짐 — "
            + detail + " → SPM 연결 복구 필요"
        )
    if status == "replacement_needed":
        existing = int(
            contract.get("replacement_source_slot_count")
            if contract.get("replacement_source_slot_count") is not None
            else contract.get("visible_source_slot_count")
            or contract.get("source_slot_count")
            or 0
        )
        connected = int(
            contract.get("replacement_connected_slot_count")
            if contract.get("replacement_connected_slot_count") is not None
            else contract.get("visible_managed_slot_count")
            or contract.get("managed_slot_count")
            or 0
        )
        if existing and connected:
            detail = (
                f"현재 내보내는 잎 카드 중 새 Atlas 연결 {connected}개, "
                f"기존 재질 사용 {existing}개"
            )
        elif existing:
            detail = (
                f"현재 내보내는 잎 카드 {existing}개가 아직 기존 재질을 사용 중"
            )
        else:
            detail = (
                "새 Atlas 재질은 만들어졌지만 현재 내보내는 잎 노드에 연결되지 않음"
            )
        return False, f"Atlas 연결 확인 필요 — {detail} → 연결 후 ② Blender Repair"
    if status == "no_leaf_slots":
        return True, "Atlas 연결 검사 비적용 — 현재 내보내는 Atlas 대상 잎 슬롯 없음"
    return True, "현재 내보내는 잎 재질 연결 정상"


def _atlas_managed_material_node(material):
    try:
        marker = json.loads(str(material.findtext("UserData") or ""))
    except (TypeError, ValueError, json.JSONDecodeError):
        return False
    return bool(
        isinstance(marker, dict)
        and str(marker.get("generator") or "").casefold()
        == ATLAS_LEAF_GENERATOR.casefold()
        and str(marker.get("kind") or "").casefold() == "material"
    )


def _spm_mesh_reference_states(root):
    """Separate live references from removable managed Atlas library residue."""
    material_references = {}
    for material in root.iter("Material_v8"):
        mesh_ids = set()
        cutout = _integer(material.findtext("CutoutMeshID"))
        if cutout is not None and cutout >= 0:
            mesh_ids.add(cutout)
        supplemental = material.find("SupplementalCutoutMeshIDs")
        if supplemental is not None:
            for node in supplemental.iter():
                value = _integer(
                    node.attrib.get("ID")
                    if node.attrib.get("ID") is not None
                    else node.text
                )
                if value is not None and value >= 0:
                    mesh_ids.add(value)
        managed = _atlas_managed_material_node(material)
        for mesh_id in mesh_ids:
            material_references.setdefault(mesh_id, []).append(
                (managed, len(mesh_ids))
            )
    generator_references = set()
    for prop in root.iter("Property"):
        name = str(prop.findtext("Name") or "").strip().casefold()
        if not name.endswith(":mesh"):
            continue
        value = _integer(prop.findtext("Value"))
        if value is not None and value >= 0:
            generator_references.add(value)
    live = set(generator_references)
    managed_orphans = set()
    for mesh_id, material_states in material_references.items():
        removable_from_all_materials = all(
            managed and mesh_count > 1
            for managed, mesh_count in material_states
        )
        if (
            mesh_id in generator_references
            or not removable_from_all_materials
        ):
            live.add(mesh_id)
        else:
            managed_orphans.add(mesh_id)
    return {
        "live": live,
        "managed_orphans": managed_orphans,
        "generator": generator_references,
        "material": set(material_references),
    }


@functools.lru_cache(maxsize=512)
def _mesh_file_references_cached(path_text, _size, _mtime_ns):
    path = Path(path_text)
    try:
        root = _load_spm_root(path)
    except (OSError, ValueError) as exc:
        return {
            "status": "inspection_error",
            "spm": str(path),
            "error": str(exc),
            "checked_references": 0,
            "missing": [],
        }

    checked = 0
    references = []
    missing = []
    orphan_missing = []
    reference_states = _spm_mesh_reference_states(root)
    for mesh in root.iter("Mesh"):
        if mesh.attrib.get("ID") is None:
            continue
        mesh_id = _integer(mesh.attrib.get("ID"))
        # Embedded meshes carry their geometry inside the SPM; their Filename
        # is provenance only and must not block the export.
        embedded = str(mesh.findtext("Embedded") or "").strip().casefold() in {
            "1", "true", "yes",
        }
        if embedded:
            continue
        filenames = [str(mesh.findtext("Filename") or "").strip()]
        for lod_tag in ("Lod_1", "Lod_2"):
            lod = mesh.find(lod_tag)
            if lod is not None:
                filenames.append(str(lod.findtext("Filename") or "").strip())
        for filename in filenames:
            if not filename:
                continue
            checked += 1
            resolved = (
                Path(filename)
                if os.path.isabs(filename)
                else path.parent / filename
            )
            usage = (
                "active"
                if mesh_id in reference_states["live"]
                else "managed_orphan"
                if mesh_id in reference_states["managed_orphans"]
                else "orphan"
            )
            row = {
                "mesh_id": mesh_id,
                "mesh_name": str(mesh.attrib.get("Name") or ""),
                "filename": filename,
                "resolved_path": str(resolved),
                "exists": resolved.is_file(),
                "referenced": usage == "active",
                "usage": usage,
                "generator_referenced": (
                    mesh_id in reference_states["generator"]
                ),
                "material_referenced": (
                    mesh_id in reference_states["material"]
                ),
            }
            references.append(row)
            if not row["exists"]:
                if row["referenced"]:
                    missing.append(row)
                else:
                    orphan_missing.append(row)
    status = (
        "missing_mesh_files"
        if missing
        else "orphan_missing_mesh_assets"
        if orphan_missing
        else "ok"
    )
    return {
        "status": status,
        "spm": str(path),
        "checked_references": checked,
        "references": references,
        "missing": missing,
        "orphan_missing": orphan_missing,
    }


def inspect_spm_mesh_file_references(spm_path):
    """Verify that every external mesh FBX the SPM references exists on disk.

    Dead references (renamed or deleted leaf-plate meshes) stall the SpeedTree
    CLI export until its timeout, so the preflight checks them before ever
    launching SpeedTree.
    """
    path = Path(spm_path)
    try:
        path_text, size, mtime_ns = _file_key(path)
    except OSError as exc:
        return {
            "status": "inspection_error",
            "spm": str(path),
            "error": str(exc),
            "checked_references": 0,
            "missing": [],
        }
    return copy.deepcopy(_mesh_file_references_cached(path_text, size, mtime_ns))


def save_leaf_contract_cache():
    """Persist shared compact SPM metadata after a background status pass."""
    return save_spm_analysis_cache()


def speedtree_stmat_path(spm_path):
    spm = Path(spm_path)
    return spm.parent / "fbx" / f"{spm.stem}.stmat"


def _normalized_material_name(value):
    return normalize_material_key(value)


@functools.lru_cache(maxsize=512)
def _stmat_names_cached(path_text, _size, _mtime_ns):
    try:
        root = ET.parse(path_text).getroot()
    except (OSError, ET.ParseError) as exc:
        return None, str(exc)
    names = [
        str(node.attrib.get("Name") or "").strip()
        for node in root.iter("Material")
        if str(node.attrib.get("Name") or "").strip()
    ]
    return names, ""


@functools.lru_cache(maxsize=512)
def _stmat_texture_sources_cached(path_text, _size, _mtime_ns):
    try:
        root = ET.parse(path_text).getroot()
    except (OSError, ET.ParseError) as exc:
        return None, str(exc)
    rows = []
    for material in root.iter("Material"):
        material_name = str(material.attrib.get("Name") or "").strip()
        for map_node in material.findall("./Map"):
            source = str(map_node.attrib.get("Source") or "").strip()
            if not source:
                continue
            rows.append({
                "material": material_name,
                "map": str(map_node.attrib.get("Name") or "").strip(),
                "source": source,
            })
    return rows, ""


def inspect_speedtree_material_export(spm_path, leaf_contract=None):
    """Compare visible semantic leaf materials with the exported ``.stmat``."""
    spm = Path(spm_path)
    leaf_contract = leaf_contract or inspect_spm_leaf_contract(spm)
    expected = list(leaf_contract.get("expected_visible_material_names") or [])
    stmat = speedtree_stmat_path(spm)
    result = {
        "status": "not_applicable" if not expected else "missing_stmat",
        "spm": str(spm),
        "stmat": str(stmat),
        "expected_materials": expected,
        "actual_materials": [],
        "missing_materials": [],
    }
    if not expected:
        return result
    try:
        path_text, size, mtime_ns = _file_key(stmat)
    except OSError:
        result["missing_materials"] = expected
        return result
    names, error = _stmat_names_cached(path_text, size, mtime_ns)
    if names is None:
        result["status"] = "invalid_stmat"
        result["error"] = error
        result["missing_materials"] = expected
        return result
    result["actual_materials"] = list(names)
    available = Counter(_normalized_material_name(name) for name in names)
    missing = []
    for name in expected:
        key = _normalized_material_name(name)
        if available[key] > 0:
            available[key] -= 1
        else:
            missing.append(name)
    result["missing_materials"] = missing
    if missing:
        result["status"] = "missing_materials"
    else:
        try:
            result["status"] = (
                "stale" if stmat.stat().st_mtime_ns < spm.stat().st_mtime_ns else "ok"
            )
        except OSError:
            result["status"] = "ok"
    return result


def inspect_all_speedtree_material_export(spm_path):
    """Compare every export-participating SPM material with the STMAT.

    Leaf replacement remains a separate structural contract.  This broader
    coverage prevents a visible stem/bark material from disappearing merely
    because the leaf-only preflight happened to be complete.
    """
    spm = Path(spm_path)
    try:
        rows = extract_material_image_refs(spm)
        active_ids = {str(value).casefold() for value in active_material_ids(spm)}
        visible_ids = {str(value).casefold() for value in visible_material_ids(spm)}
    except (OSError, ValueError) as exc:
        return {
            "status": "inspection_error",
            "scope": "all_export_participating",
            "spm": str(spm),
            "stmat": str(speedtree_stmat_path(spm)),
            "error": str(exc),
            "expected_material_records": [],
            "expected_materials": [],
            "actual_materials": [],
            "missing_material_records": [],
            "missing_materials": [],
        }
    # Only a visible Generator reference is authoritative export evidence.
    # PCG texture discovery historically falls back to every material when no
    # references are parsed, but using that fallback in this blocking gate
    # would make unused Material_v8 definitions falsely mandatory.
    expected_rows = [
        {
            "material_id": row.get("material_id"),
            "material_name": str(row.get("material_name") or ""),
        }
        for row in rows
        if row.get("material_name")
        and str(row.get("material_id") or "").casefold() in visible_ids
    ]
    missing_reference_evidence = bool(rows and not active_ids)
    stmat = speedtree_stmat_path(spm)
    result = {
        "status": "not_applicable" if not expected_rows else "missing_stmat",
        "scope": "all_export_participating",
        "spm": str(spm),
        "stmat": str(stmat),
        "expected_material_records": expected_rows,
        "expected_materials": [row["material_name"] for row in expected_rows],
        "actual_materials": [],
        "missing_material_records": [],
        "missing_materials": [],
        "coverage_confidence": (
            "no_generator_material_references"
            if missing_reference_evidence
            else "visible_generator_references"
        ),
    }
    if missing_reference_evidence:
        result["warning_code"] = "ALL_EXPORT_REFERENCE_EVIDENCE_MISSING"
        result["warning"] = (
            "No Generator material references were found; unused material "
            "definitions were not treated as export-participating"
        )
    if not expected_rows:
        return result
    try:
        path_text, size, mtime_ns = _file_key(stmat)
    except OSError:
        result["missing_material_records"] = expected_rows
        result["missing_materials"] = result["expected_materials"]
        return result
    names, error = _stmat_names_cached(path_text, size, mtime_ns)
    if names is None:
        result["status"] = "invalid_stmat"
        result["error"] = error
        result["missing_material_records"] = expected_rows
        result["missing_materials"] = result["expected_materials"]
        return result

    result["actual_materials"] = list(names)
    available = Counter(_normalized_material_name(name) for name in names)
    missing = []
    for row in expected_rows:
        key = _normalized_material_name(row["material_name"])
        if available[key] > 0:
            available[key] -= 1
        else:
            missing.append(row)
    result["missing_material_records"] = missing
    result["missing_materials"] = [row["material_name"] for row in missing]
    if missing:
        result["status"] = "missing_materials"
    else:
        try:
            result["status"] = (
                "stale" if stmat.stat().st_mtime_ns < spm.stat().st_mtime_ns else "ok"
            )
        except OSError:
            result["status"] = "ok"
    return result


def inspect_speedtree_texture_sources(spm_path):
    """Validate every texture Source declared by the current SpeedTree STMAT."""
    spm = Path(spm_path)
    stmat = speedtree_stmat_path(spm)
    result = {
        "status": "missing_stmat",
        "spm": str(spm),
        "stmat": str(stmat),
        "source_count": 0,
        "missing_sources": [],
    }
    try:
        path_text, size, mtime_ns = _file_key(stmat)
    except OSError:
        return result
    rows, error = _stmat_texture_sources_cached(path_text, size, mtime_ns)
    if rows is None:
        result["status"] = "invalid_stmat"
        result["error"] = error
        return result
    result["source_count"] = len(rows)
    if not rows:
        names, names_error = _stmat_names_cached(path_text, size, mtime_ns)
        if names is None:
            result["status"] = "invalid_stmat"
            result["error"] = names_error
            return result
        if names:
            result["status"] = "missing_sources"
            result["classification"] = "asset_texture_source_undeclared"
            result["error"] = (
                "Exported STMAT materials declare no texture Source entries"
            )
            result["failure_reason"] = (
                "The source SPM exported one or more materials, but those "
                "materials do not declare any texture Source path"
            )
            result["remediation"] = (
                "Assign the intended texture maps to the material in "
                "SpeedTree Modeler, then save and export the SPM again"
            )
            result["missing_sources"] = [
                {
                    "material": name,
                    "map": "<none>",
                    "source": "",
                    "resolved": "",
                }
                for name in names
            ]
            return result
    missing = []
    for row in rows:
        candidate = Path(row["source"]).expanduser()
        if not candidate.is_absolute():
            candidate = stmat.parent / candidate
        try:
            resolved = candidate.resolve()
            ready = resolved.is_file() and resolved.stat().st_size > 0
        except OSError:
            resolved = candidate
            ready = False
        if not ready:
            missing.append({**row, "resolved": str(resolved)})
    result["missing_sources"] = missing
    if missing:
        result["status"] = "missing_sources"
        result["classification"] = "asset_texture_source_path_missing"
        result["error"] = (
            f"{len(missing)} texture Source path(s) declared by the "
            "exported STMAT do not resolve to a non-empty file"
        )
        result["failure_reason"] = (
            "The source SPM material stores stale or missing texture file "
            "paths; Blender Repair cannot prove the intended texture content"
        )
        result["remediation"] = (
            "Relink the listed Source paths in SpeedTree Modeler to the "
            "intended files, then save and export the SPM again"
        )
    else:
        try:
            result["status"] = (
                "stale" if stmat.stat().st_mtime_ns < spm.stat().st_mtime_ns else "ok"
            )
        except OSError:
            result["status"] = "ok"
    return result
