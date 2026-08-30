"""Canonical material-slot/remap handling for Unreal Nanite Assemblies.

The Nanite Assembly keeps its own global material table and a local-to-global
``MaterialRemap`` for every part.  Reimporting a referenced part can update the
geometry without updating either structure.  This module makes that boundary
explicit, deterministic, and testable without importing ``unreal`` at module
load time.
"""

from __future__ import annotations

import json
import re
from copy import deepcopy


class NaniteAssemblyMaterialError(RuntimeError):
    """Raised when material identity or a Nanite part remap is not provable."""


_PART_PATTERN = re.compile(
    r'\(MeshObjectPath="(?P<mesh>[^"]+)",\s*'
    r'MaterialRemap=\((?P<remap>[^)]*)\)\)'
)


def _clean_name(value):
    text = str(value or "").strip()
    return "" if text.casefold() == "none" else text


def _slot_identity(row, label):
    slot_name = _clean_name(
        row.get("slot_name") or row.get("slot") or row.get("imported_slot_name")
    )
    material_path = str(row.get("material") or "").strip()
    if not slot_name:
        raise NaniteAssemblyMaterialError(f"{label} has no material slot name")
    if not material_path:
        raise NaniteAssemblyMaterialError(
            f"{label} slot {slot_name!r} has no assigned material interface"
        )
    return slot_name.casefold(), material_path.casefold()


def plan_nanite_assembly_material_normalization(global_slots, parts):
    """Return a stable union table and exact local-to-global remaps.

    Slot name *and* material path form the identity.  Matching by slot name
    alone can silently keep a stale texture, while matching by material path
    alone loses slot semantics.  Existing global indices are never removed or
    reordered; missing identities are appended so fallback sections remain
    valid on legacy assets.
    """

    canonical_slots = [deepcopy(row) for row in list(global_slots or [])]
    key_to_index = {}
    duplicate_global_keys = []
    for index, row in enumerate(canonical_slots):
        key = _slot_identity(row, f"global material {index}")
        if key in key_to_index:
            duplicate_global_keys.append(
                {"index": index, "canonical_index": key_to_index[key], "key": key}
            )
        else:
            key_to_index[key] = index

    planned_parts = []
    appended_slots = []
    for part_index, part in enumerate(list(parts or [])):
        mesh = str(part.get("mesh") or part.get("mesh_object_path") or "").strip()
        if not mesh:
            raise NaniteAssemblyMaterialError(
                f"Nanite Assembly part {part_index} has no mesh object path"
            )
        local_slots = list(part.get("slots") or [])
        if not local_slots:
            raise NaniteAssemblyMaterialError(
                f"Nanite Assembly part {mesh} has no material slots"
            )
        desired_remap = []
        local_key_to_index = {}
        for local_index, row in enumerate(local_slots):
            key = _slot_identity(row, f"part {mesh} material {local_index}")
            duplicate_index = local_key_to_index.get(key)
            if duplicate_index is not None:
                raise NaniteAssemblyMaterialError(
                    "Nanite Assembly part has duplicate material identity after "
                    "reimport: "
                    f"mesh={mesh}, slots={duplicate_index},{local_index}, key={key}. "
                    "The material sidecar must compact the part before Assembly build."
                )
            local_key_to_index[key] = local_index
            global_index = key_to_index.get(key)
            if global_index is None:
                global_index = len(canonical_slots)
                appended = deepcopy(row)
                appended["index"] = global_index
                appended["source_part_index"] = part_index
                appended["source_slot_index"] = local_index
                canonical_slots.append(appended)
                key_to_index[key] = global_index
                appended_slots.append(deepcopy(appended))
            desired_remap.append(global_index)
        planned_parts.append(
            {
                "index": part_index,
                "mesh": mesh,
                "existing_remap": [int(value) for value in part.get("remap") or []],
                "desired_remap": desired_remap,
                "slot_count": len(local_slots),
            }
        )

    if not planned_parts:
        raise NaniteAssemblyMaterialError("Nanite Assembly has no parts")
    return {
        "global_slots": canonical_slots,
        "parts": planned_parts,
        "appended_slots": appended_slots,
        "duplicate_global_keys": duplicate_global_keys,
    }


def parse_nanite_assembly_part_remaps(settings_text):
    """Parse protected part paths/remaps from ``MeshNaniteSettings.export_text``."""

    text = str(settings_text or "")
    rows = []
    for index, match in enumerate(_PART_PATTERN.finditer(text)):
        raw_values = match.group("remap").strip()
        try:
            remap = (
                [int(value.strip()) for value in raw_values.split(",")]
                if raw_values
                else []
            )
        except ValueError as exc:
            raise NaniteAssemblyMaterialError(
                f"Nanite Assembly part {index} has a non-integer MaterialRemap"
            ) from exc
        rows.append(
            {
                "index": index,
                "mesh": match.group("mesh"),
                "remap": remap,
                "remap_span": match.span("remap"),
            }
        )
    if not rows:
        raise NaniteAssemblyMaterialError(
            "MeshNaniteSettings contains no readable Assembly part remaps"
        )
    return rows


def rewrite_nanite_assembly_part_remaps(settings_text, desired_remaps):
    """Replace only the protected remap integer lists, preserving all other settings."""

    text = str(settings_text or "")
    rows = parse_nanite_assembly_part_remaps(text)
    desired = [list(map(int, values)) for values in desired_remaps]
    if len(rows) != len(desired):
        raise NaniteAssemblyMaterialError(
            "Nanite Assembly part/remap count changed during normalization: "
            f"{len(rows)} != {len(desired)}"
        )
    rewritten = text
    for row, values in reversed(list(zip(rows, desired))):
        start, end = row["remap_span"]
        rewritten = rewritten[:start] + ",".join(map(str, values)) + rewritten[end:]
    check = parse_nanite_assembly_part_remaps(rewritten)
    if [row["remap"] for row in check] != desired:
        raise NaniteAssemblyMaterialError(
            "Nanite Assembly remap text did not round-trip exactly"
        )
    return rewritten


def _unreal_slot_record(slot, index):
    interface = slot.get_editor_property("material_interface")
    return {
        "index": index,
        "slot_name": str(slot.get_editor_property("material_slot_name")),
        "imported_slot_name": str(
            slot.get_editor_property("imported_material_slot_name")
        ),
        "material": interface.get_path_name() if interface else None,
    }


def _json_result(raw):
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


MATERIAL_SECTION_AUDIT_SCHEMA_VERSION = 1
MATERIAL_SECTION_AUDIT_KIND = "skeletal_mesh_lod0_material_sections"
MATERIAL_SECTION_FIELDS = (
    "section",
    "material_index",
    "base_index",
    "num_triangles",
    "base_vertex_index",
    "num_vertices",
)


def _audit_integer(result, field, *, minimum=None):
    value = result.get(field)
    if isinstance(value, bool) or not isinstance(value, int):
        raise NaniteAssemblyMaterialError(
            f"skeletal mesh material-section audit has invalid {field}: {value!r}"
        )
    if minimum is not None and value < minimum:
        raise NaniteAssemblyMaterialError(
            f"skeletal mesh material-section audit has invalid {field}: {value!r}"
        )
    return value


def audit_unreal_skeletal_mesh_material_sections(unreal, mesh_path, slot_count=None):
    """Fail closed when a render section points outside the material table."""

    library = getattr(unreal, "CodexMaterialToolsLibrary", None)
    audit = getattr(
        library,
        "audit_skeletal_mesh_lod0_material_sections",
        None,
    )
    if not callable(audit):
        raise NaniteAssemblyMaterialError(
            "CodexMaterialToolsLibrary lightweight material-section audit is "
            "unavailable"
        )

    raw = audit(str(mesh_path))
    values = raw if isinstance(raw, tuple) else (raw,)
    native_success = next(
        (value for value in values if isinstance(value, bool)),
        None,
    )
    try:
        result = _json_result(raw)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise NaniteAssemblyMaterialError(
            f"skeletal mesh material-section audit returned invalid JSON for "
            f"{mesh_path}: {exc}"
        ) from exc
    if (
        native_success is not True
        or result.get("returned_errors")
        or result.get("ok") is not True
    ):
        raise NaniteAssemblyMaterialError(
            f"skeletal mesh material-section audit failed for {mesh_path}: "
            f"native_success={native_success!r}, result={result}"
        )

    expected_mesh = str(mesh_path)
    if result.get("schema_version") != MATERIAL_SECTION_AUDIT_SCHEMA_VERSION:
        raise NaniteAssemblyMaterialError(
            "skeletal mesh material-section audit schema mismatch: "
            f"expected={MATERIAL_SECTION_AUDIT_SCHEMA_VERSION}, "
            f"actual={result.get('schema_version')!r}"
        )
    if result.get("audit") != MATERIAL_SECTION_AUDIT_KIND:
        raise NaniteAssemblyMaterialError(
            "skeletal mesh material-section audit kind mismatch: "
            f"expected={MATERIAL_SECTION_AUDIT_KIND!r}, "
            f"actual={result.get('audit')!r}"
        )
    if result.get("skeletal_mesh") != expected_mesh:
        raise NaniteAssemblyMaterialError(
            "skeletal mesh material-section audit target mismatch: "
            f"expected={expected_mesh!r}, "
            f"actual={result.get('skeletal_mesh')!r}"
        )
    if not isinstance(result.get("target_compiling_before"), bool):
        raise NaniteAssemblyMaterialError(
            "skeletal mesh material-section audit is missing the target compile state"
        )
    if result.get("target_compiling_after") is not False:
        raise NaniteAssemblyMaterialError(
            "skeletal mesh material-section audit target is still compiling"
        )
    if _audit_integer(result, "lod_index", minimum=0) != 0:
        raise NaniteAssemblyMaterialError(
            "skeletal mesh material-section audit did not inspect LOD0"
        )
    _audit_integer(result, "lod_count", minimum=1)

    materials = result.get("materials")
    sections = result.get("sections")
    if not isinstance(materials, list) or not isinstance(sections, list):
        raise NaniteAssemblyMaterialError(
            "skeletal mesh material-section audit arrays are malformed"
        )
    material_count = _audit_integer(result, "material_count", minimum=0)
    section_count = _audit_integer(result, "section_count", minimum=0)
    if material_count != len(materials) or section_count != len(sections):
        raise NaniteAssemblyMaterialError(
            "skeletal mesh material-section audit count mismatch: "
            f"materials={material_count}/{len(materials)}, "
            f"sections={section_count}/{len(sections)}"
        )
    if slot_count is not None:
        if isinstance(slot_count, bool) or not isinstance(slot_count, int):
            raise NaniteAssemblyMaterialError(
                f"skeletal mesh slot count is invalid: {slot_count!r}"
            )
        if slot_count != material_count:
            raise NaniteAssemblyMaterialError(
                "skeletal mesh material count changed across validation: "
                f"loaded={slot_count}, audited={material_count}"
            )

    for material_index, row in enumerate(materials):
        if not isinstance(row, dict):
            raise NaniteAssemblyMaterialError(
                "skeletal mesh material-section audit material row is malformed"
            )
        if row.get("index") != material_index:
            raise NaniteAssemblyMaterialError(
                "skeletal mesh material-section audit material order changed: "
                f"expected={material_index}, actual={row.get('index')!r}"
            )
        for field in ("slot_name", "imported_slot_name", "material"):
            if not isinstance(row.get(field), str):
                raise NaniteAssemblyMaterialError(
                    "skeletal mesh material-section audit material row is missing "
                    f"{field}: index={material_index}"
                )

    normalized_sections = []
    invalid = []
    for section_index, row in enumerate(sections):
        if not isinstance(row, dict):
            raise NaniteAssemblyMaterialError(
                "skeletal mesh material-section audit section row is malformed"
            )
        missing = [field for field in MATERIAL_SECTION_FIELDS if field not in row]
        if missing:
            raise NaniteAssemblyMaterialError(
                "skeletal mesh material-section audit section row is missing "
                f"fields: section={section_index}, missing={missing}"
            )
        values = {}
        for field in MATERIAL_SECTION_FIELDS:
            value = row[field]
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise NaniteAssemblyMaterialError(
                    "skeletal mesh material-section audit section row has invalid "
                    f"{field}: section={section_index}, value={value!r}"
                )
            values[field] = value
        if values["section"] != section_index:
            raise NaniteAssemblyMaterialError(
                "skeletal mesh material-section audit section order changed: "
                f"expected={section_index}, actual={values['section']}"
            )
        if values["material_index"] >= material_count:
            invalid.append(
                {
                    "section": values["section"],
                    "material_index": values["material_index"],
                }
            )
        normalized_sections.append(
            {
                "section": values["section"],
                "material_index": values["material_index"],
                "num_triangles": values["num_triangles"],
            }
        )
    if invalid:
        raise NaniteAssemblyMaterialError(
            f"skeletal mesh has invalid section material indices: "
            f"mesh={mesh_path}, slots={material_count}, invalid={invalid}"
        )
    return {
        "status": "ok",
        "mesh": str(mesh_path),
        "slot_count": material_count,
        "sections": normalized_sections,
    }


def normalize_unreal_nanite_assembly_materials(
    unreal,
    assembly,
    *,
    apply=False,
    allow_dirty=False,
):
    """Audit or normalize one loaded ``unreal.SkeletalMesh`` Assembly.

    ``apply=False`` is read-only.  ``apply=True`` updates the global table and
    every protected part remap transactionally, but deliberately does not save;
    the pipeline or migration caller owns persistence.
    """

    if not isinstance(assembly, unreal.SkeletalMesh):
        raise NaniteAssemblyMaterialError("target is not an Unreal SkeletalMesh")
    assembly_path = assembly.get_path_name()
    package_path = assembly.get_outermost().get_path_name()
    if apply and not allow_dirty:
        dirty = {
            package.get_path_name()
            for package in unreal.EditorLoadingAndSavingUtils.get_dirty_content_packages()
        }
        if package_path in dirty:
            raise NaniteAssemblyMaterialError(
                f"Assembly already has unsaved user changes: {package_path}"
            )

    settings = assembly.get_editor_property("nanite_settings")
    settings_text = settings.export_text()
    parsed_parts = parse_nanite_assembly_part_remaps(settings_text)
    original_materials = list(assembly.get_editor_property("materials") or [])
    global_records = [
        _unreal_slot_record(slot, index)
        for index, slot in enumerate(original_materials)
    ]
    source_slot_objects = []
    part_rows = []
    part_section_audits = []
    for part_index, parsed in enumerate(parsed_parts):
        object_path = parsed["mesh"]
        package_asset_path = object_path.split(".", 1)[0]
        part_mesh = unreal.EditorAssetLibrary.load_asset(package_asset_path)
        if not isinstance(part_mesh, unreal.SkeletalMesh):
            raise NaniteAssemblyMaterialError(
                f"Assembly part does not load as SkeletalMesh: {object_path}"
            )
        slots = list(part_mesh.get_editor_property("materials") or [])
        records = [_unreal_slot_record(slot, index) for index, slot in enumerate(slots)]
        part_section_audits.append(
            audit_unreal_skeletal_mesh_material_sections(
                unreal,
                package_asset_path,
                len(slots),
            )
        )
        source_slot_objects.append(slots)
        part_rows.append(
            {
                "mesh": object_path,
                "remap": parsed["remap"],
                "slots": records,
            }
        )

    plan = plan_nanite_assembly_material_normalization(global_records, part_rows)
    desired_remaps = [row["desired_remap"] for row in plan["parts"]]
    rewritten_text = rewrite_nanite_assembly_part_remaps(
        settings_text,
        desired_remaps,
    )
    remaps_changed = [row["remap"] for row in parsed_parts] != desired_remaps
    would_change = bool(plan["appended_slots"] or remaps_changed)
    report = {
        "assembly": assembly_path,
        "apply_requested": bool(apply),
        "would_change": would_change,
        "changed": False,
        "before_material_count": len(original_materials),
        "after_material_count": len(plan["global_slots"]),
        "global_slots": plan["global_slots"],
        "appended_slots": plan["appended_slots"],
        "duplicate_global_keys": plan["duplicate_global_keys"],
        "parts": plan["parts"],
        "part_section_audits": part_section_audits,
    }
    if not apply or not would_change:
        report["assembly_section_audit"] = (
            audit_unreal_skeletal_mesh_material_sections(
                unreal,
                assembly_path,
                len(original_materials),
            )
        )
        return report

    updated_materials = list(original_materials)
    for appended in plan["appended_slots"]:
        updated_materials.append(
            source_slot_objects[appended["source_part_index"]][
                appended["source_slot_index"]
            ]
        )
    original_settings = type(settings)()
    original_settings.import_text(settings_text)
    candidate_settings = type(settings)()
    candidate_settings.import_text(rewritten_text)
    if [
        row["remap"]
        for row in parse_nanite_assembly_part_remaps(candidate_settings.export_text())
    ] != desired_remaps:
        raise NaniteAssemblyMaterialError(
            "candidate MeshNaniteSettings lost the desired remaps"
        )

    try:
        assembly.modify()
        assembly.set_editor_property("materials", updated_materials)
        assembly.set_editor_property("nanite_settings", candidate_settings)
        after_records = [
            _unreal_slot_record(slot, index)
            for index, slot in enumerate(assembly.get_editor_property("materials") or [])
        ]
        for planned in plan["parts"]:
            local_records = part_rows[planned["index"]]["slots"]
            for local_index, global_index in enumerate(planned["desired_remap"]):
                if _slot_identity(
                    after_records[global_index],
                    f"normalized global material {global_index}",
                ) != _slot_identity(
                    local_records[local_index],
                    f"normalized part material {local_index}",
                ):
                    raise NaniteAssemblyMaterialError(
                        "normalized Assembly material identity does not match its part"
                    )
        saved_remaps = [
            row["remap"]
            for row in parse_nanite_assembly_part_remaps(
                assembly.get_editor_property("nanite_settings").export_text()
            )
        ]
        if saved_remaps != desired_remaps:
            raise NaniteAssemblyMaterialError(
                "normalized Assembly remaps did not persist on the loaded asset"
            )
        report["assembly_section_audit"] = (
            audit_unreal_skeletal_mesh_material_sections(
                unreal,
                assembly_path,
                len(updated_materials),
            )
        )
        report["changed"] = True
        return report
    except Exception:
        assembly.set_editor_property("materials", original_materials)
        assembly.set_editor_property("nanite_settings", original_settings)
        raise


__all__ = [
    "NaniteAssemblyMaterialError",
    "audit_unreal_skeletal_mesh_material_sections",
    "normalize_unreal_nanite_assembly_materials",
    "parse_nanite_assembly_part_remaps",
    "plan_nanite_assembly_material_normalization",
    "rewrite_nanite_assembly_part_remaps",
]
