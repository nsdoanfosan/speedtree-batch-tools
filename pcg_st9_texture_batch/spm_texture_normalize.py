"""Normalize active SpeedTree materials to the managed six-map SBS outputs.

The SBS images are not repacked or edited here.  SpeedTree's per-map
``TexSource`` selector is used to read AO and roughness from the R/G channels
of the shared ``extra`` image.
"""
from __future__ import annotations

import gzip
import os
import re
import shutil
from datetime import datetime
from pathlib import Path

from pcg_texture_audit import (
    MATERIAL_BLOCK_RE,
    MATERIAL_ID_RE,
    MATERIAL_RE,
    active_material_ids,
    active_sbs_files,
    canonical_material_name,
    read_maybe_gzip_text,
    texture_base_for_material,
    visible_material_ids,
)


MAP_BLOCK_RE = re.compile(
    r'<Map\s+Name="([^"]+)"[^>]*>.*?(?:</Map>|<\\Map>)',
    re.IGNORECASE | re.DOTALL,
)

# Map name -> (render role, TexSource, TexEnabled policy, TexInvert)
# TexSource: 0=RGB, 1=R, 2=G.  The extra map contains AO in R and
# roughness in G; SpeedTree consumes gloss, so only the G read is inverted.
SLOT_SPECS = (
    ("Color", "color", 0, True, False),
    ("Opacity", "opacity", 1, False, False),
    ("Normal", "normal", 0, True, False),
    ("Gloss", "extra", 2, True, True),
    ("SubsurfaceColor", "subsurface", 0, "source", False),
    ("SubsurfaceAmount", "subsurface", 1, False, False),
    ("AO", "extra", 1, True, False),
    ("Height", "height", 1, True, False),
)


def _field_pattern(name):
    escaped = re.escape(name)
    return re.compile(
        rf'(<{escaped}\b[^>]*>)(.*?)(</{escaped}>|<\\{escaped}>)',
        re.IGNORECASE | re.DOTALL,
    )


def _field_value(block, name):
    match = _field_pattern(name).search(block)
    return " ".join(match.group(2).split()) if match else ""


def _replace_field(block, name, value):
    pattern = _field_pattern(name)
    if pattern.search(block):
        return pattern.sub(
            lambda match: match.group(1) + str(value) + match.group(3),
            block,
            count=1,
        )
    self_closing = re.compile(
        rf'<{re.escape(name)}\b([^>]*)/\s*>', re.IGNORECASE)
    if self_closing.search(block):
        return self_closing.sub(
            lambda match: f"<{name}{match.group(1)}>{value}</{name}>",
            block,
            count=1,
        )
    raise RuntimeError(f"SpeedTree Map field is missing: {name}")


def _normalize_map(template, map_name, texture_ref, tex_source, enabled, invert,
                   subsurface_enabled=False):
    block = re.sub(
        r'(<Map\s+Name=")[^"]+("[^>]*>)',
        lambda match: match.group(1) + map_name + match.group(2),
        template,
        count=1,
        flags=re.IGNORECASE,
    )
    values = {
        "TexFilename": texture_ref,
        "TexSource": tex_source,
        "TexEnabled": str(bool(enabled)).lower(),
        "TexInvert": str(bool(invert)).lower(),
        "TexInvertRed": "false",
        "TexInvertGreen": "false",
        "TexInvertBlue": "false",
    }
    for field, value in values.items():
        block = _replace_field(block, field, value)
    if map_name == "Opacity":
        block = _replace_field(block, "ColorX", "1")
    if map_name == "SubsurfaceColor" and not subsurface_enabled:
        block = _replace_field(block, "ColorX", "0")
        block = _replace_field(block, "ColorY", "0")
        block = _replace_field(block, "ColorZ", "0")
    # With TexEnabled=false ColorX is the scalar amount.  Real leaf/stem
    # sources use the identity amount; disabled bark/no-source materials use 0.
    if map_name == "SubsurfaceAmount":
        block = _replace_field(block, "ColorX", "1" if subsurface_enabled else "0")
        block = _replace_field(block, "ColorY", "0")
        block = _replace_field(block, "ColorZ", "0")
    return block


def output_paths(texture_dir, texture_base):
    texture_dir = Path(texture_dir)
    return {
        role: texture_dir / f"{texture_base}_{role}.tga"
        for role in ("color", "normal", "extra", "height", "opacity", "subsurface")
    }


def _relative_texture_ref(spm, texture_path):
    relative = os.path.relpath(Path(texture_path), Path(spm).parent)
    return relative.replace("\\", "/")


def _material_name(block):
    match = MATERIAL_RE.search(block)
    return match.group(2) if match else ""


def _material_id(block):
    match = MATERIAL_ID_RE.search(block)
    return match.group(1) if match else ""


def _resolved_slot_specs(subsurface_enabled):
    return tuple(
        (name, role, source,
         bool(subsurface_enabled) if enabled == "source" else enabled,
         invert)
        for name, role, source, enabled, invert in SLOT_SPECS
    )


def normalize_material_block(block, material_name, role_refs, subsurface_enabled=False):
    maps = list(MAP_BLOCK_RE.finditer(block))
    if not maps:
        raise RuntimeError(f"{material_name}: SpeedTree Map blocks are missing")

    by_name = {match.group(1).lower(): match.group(0) for match in maps}
    scalar_template = next(
        (by_name[name] for name in ("height", "opacity", "custom", "gloss", "ao") if name in by_name),
        maps[0].group(0),
    )
    normalized = []
    for map_name, role, tex_source, enabled, invert in _resolved_slot_specs(subsurface_enabled):
        template = by_name.get(map_name.lower())
        if template is None:
            template = scalar_template if tex_source else maps[0].group(0)
        try:
            normalized.append(_normalize_map(
                template,
                map_name,
                role_refs[role],
                tex_source,
                enabled,
                invert,
                subsurface_enabled=subsurface_enabled,
            ))
        except Exception as exc:
            raise RuntimeError(f"{material_name} / {map_name}: {exc}") from exc

    start = maps[0].start()
    end = maps[-1].end()
    between = block[maps[0].end():maps[1].start()] if len(maps) > 1 else "\n"
    patched = block[:start] + between.join(normalized) + block[end:]
    patched = MATERIAL_RE.sub(
        lambda match: match.group(1) + material_name + match.group(3),
        patched,
        count=1,
    )
    return patched


def restore_material_maps(block, material_name, source_block):
    """Restore every SpeedTree Map slot from the authoritative non-SK SPM."""
    current_maps = list(MAP_BLOCK_RE.finditer(block))
    source_maps = list(MAP_BLOCK_RE.finditer(source_block))
    if not current_maps or not source_maps:
        raise RuntimeError(f"{material_name}: source/current SpeedTree Map blocks are missing")
    separator = (
        block[current_maps[0].end():current_maps[1].start()]
        if len(current_maps) > 1 else "\n"
    )
    replacement = separator.join(match.group(0) for match in source_maps)
    patched = (
        block[:current_maps[0].start()] + replacement + block[current_maps[-1].end():]
    )
    return MATERIAL_RE.sub(
        lambda match: match.group(1) + material_name + match.group(3),
        patched,
        count=1,
    )


def _source_material_block(source_spm, canonical_name):
    text = read_maybe_gzip_text(source_spm)
    for match in MATERIAL_BLOCK_RE.finditer(text):
        block = match.group(0)
        if canonical_material_name(_material_name(block)).lower() == canonical_name.lower():
            return block
    raise RuntimeError(
        f"{Path(source_spm).name}: source material is missing: {canonical_name}")


def build_spm_patch(spm, material_outputs, require_outputs=True):
    """Return a verified in-memory patch for one SPM.

    ``material_outputs`` can be keyed by exact ``@id:<Material_v8 ID>`` or by
    canonical material name.  Exact ID mappings take priority so duplicate
    material names can safely use different managed outputs.
    """
    spm = Path(spm)
    text = read_maybe_gzip_text(spm)
    if not text:
        raise RuntimeError(f"cannot read SPM: {spm}")
    referenced_ids = {
        str(value).lower() for value in active_material_ids(spm)
    }
    if referenced_ids:
        active_ids = {
            str(value).lower() for value in visible_material_ids(spm)
        }
    else:
        active_ids = {
            _material_id(match.group(0)).lower()
            for match in MATERIAL_BLOCK_RE.finditer(text)
            if _material_id(match.group(0))
        }
    changed = []
    missing = []
    chunks = []
    cursor = 0

    for match in MATERIAL_BLOCK_RE.finditer(text):
        block = match.group(0)
        material_id = _material_id(block).lower()
        if material_id not in active_ids:
            continue
        current_name = _material_name(block)
        canonical = canonical_material_name(current_name)
        output = material_outputs.get(f"@id:{material_id}")
        if output is None:
            output = material_outputs.get(canonical.lower())
        if not output:
            missing.append(f"{canonical}: no managed output mapping")
            continue
        if output.get("mode") == "preserve_source":
            source_block = _source_material_block(output["source_spm"], canonical)
            patched = restore_material_maps(block, canonical, source_block)
            expected_row = next(iter(inspect_material_slots(patched).values()))
            chunks.extend((text[cursor:match.start()], patched))
            cursor = match.end()
            changed.append({
                "mode": "preserve_source",
                "material_id": material_id,
                "old_name": current_name,
                "material_name": canonical,
                "source_spm": output["source_spm"],
                "expected_slots": expected_row["slots"],
            })
            continue
        paths = output_paths(output["texture_dir"], output["texture_base"])
        subsurface_enabled = bool(output.get("subsurface_enabled"))
        absent = [
            str(path) for role, path in paths.items()
            if require_outputs and not path.is_file()
            and (role != "subsurface" or subsurface_enabled)
        ]
        if absent:
            missing.append(f"{canonical}: missing outputs: {', '.join(absent)}")
            continue
        refs = {
            role: (_relative_texture_ref(spm, path) if path.is_file() else "")
            for role, path in paths.items()
        }
        patched = normalize_material_block(
            block, canonical, refs, subsurface_enabled=subsurface_enabled)
        chunks.extend((text[cursor:match.start()], patched))
        cursor = match.end()
        changed.append({
            "material_id": material_id,
            "old_name": current_name,
            "material_name": canonical,
            "texture_base": output["texture_base"],
            "refs": refs,
            "subsurface_enabled": subsurface_enabled,
        })

    if missing:
        raise RuntimeError(f"{spm.name}: " + " | ".join(missing))
    if not changed:
        raise RuntimeError(f"{spm.name}: no active materials were normalized")
    chunks.append(text[cursor:])
    patched_text = "".join(chunks)
    verify_spm_text(spm, patched_text, changed)
    return {"spm": str(spm), "text": patched_text, "materials": changed}


def inspect_material_slots(text):
    result = {}
    for material_match in MATERIAL_BLOCK_RE.finditer(text):
        block = material_match.group(0)
        name = _material_name(block)
        material_id = _material_id(block).lower()
        slots = {}
        for map_match in MAP_BLOCK_RE.finditer(block):
            map_block = map_match.group(0)
            slots[map_match.group(1).lower()] = {
                "name": map_match.group(1),
                "color_x": _field_value(map_block, "ColorX"),
                "color_y": _field_value(map_block, "ColorY"),
                "color_z": _field_value(map_block, "ColorZ"),
                "filename": _field_value(map_block, "TexFilename"),
                "source": _field_value(map_block, "TexSource"),
                "enabled": _field_value(map_block, "TexEnabled").lower(),
                "invert": _field_value(map_block, "TexInvert").lower(),
            }
        result[material_id] = {"name": name, "slots": slots}
    return result


def verify_spm_text(spm, text, materials):
    inspected = inspect_material_slots(text)
    for material in materials:
        row = inspected.get(material["material_id"])
        if not row:
            raise RuntimeError(f"{Path(spm).name}: material disappeared: {material['material_name']}")
        if row["name"] != material["material_name"]:
            raise RuntimeError(f"{Path(spm).name}: material name verification failed")
        if material.get("mode") == "preserve_source":
            if row["slots"] != material["expected_slots"]:
                raise RuntimeError(
                    f"{Path(spm).name}: preserved source slots differ for {row['name']}")
            continue
        expected_names = {spec[0].lower() for spec in SLOT_SPECS}
        if set(row["slots"]) != expected_names:
            raise RuntimeError(
                f"{Path(spm).name}: slot set verification failed for {row['name']}: "
                f"{sorted(row['slots'])}")
        for map_name, role, source, enabled, invert in _resolved_slot_specs(
                material.get("subsurface_enabled", False)):
            slot = row["slots"][map_name.lower()]
            expected = {
                "filename": material["refs"][role],
                "source": str(source),
                "enabled": str(bool(enabled)).lower(),
                "invert": str(bool(invert)).lower(),
            }
            for field, value in expected.items():
                if slot[field].replace("\\", "/").lower() != value.replace("\\", "/").lower():
                    raise RuntimeError(
                        f"{Path(spm).name}: {row['name']} {map_name} {field} "
                        f"expected {value!r}, got {slot[field]!r}")


def _is_gzip(path):
    with Path(path).open("rb") as handle:
        return handle.read(2) == b"\x1f\x8b"


def _write_spm(path, text, compressed):
    path = Path(path)
    if compressed:
        with gzip.open(path, "wb") as handle:
            handle.write(text.encode("utf-8"))
    else:
        path.write_text(text, encoding="utf-8")


def normalize_spms_transactionally(jobs, backup_root=None, require_outputs=True,
                                   skip_unbuildable=False):
    """Preflight, back up, write, and verify all SPMs as one transaction.

    With skip_unbuildable, an SPM whose patch cannot even be built (e.g. a
    legacy tree whose materials have no managed T_ outputs yet) is reported
    under "skipped" instead of aborting every other SPM in the folder.
    """
    patches = []
    skipped = []
    for job in jobs:
        try:
            patches.append(
                build_spm_patch(job["spm"], job["materials"], require_outputs=require_outputs)
            )
        except Exception as exc:
            if not skip_unbuildable:
                raise
            skipped.append({"spm": str(job["spm"]), "reason": str(exc)})
    if not patches:
        if skipped:
            raise RuntimeError(
                "no SPM could be normalized: "
                + " | ".join(entry["reason"] for entry in skipped)
            )
        return {"spms": [], "materials": 0, "backup_dir": None, "skipped": skipped}

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_root = Path(backup_root or (Path(patches[0]["spm"]).parent / "_spm_backups"))
    backup_dir = backup_root / f"texture_normalize_{stamp}"
    backup_dir.mkdir(parents=True, exist_ok=False)
    originals = []
    try:
        for index, patch in enumerate(patches, 1):
            spm = Path(patch["spm"])
            backup = backup_dir / f"{index:04d}_{spm.name}"
            shutil.copy2(spm, backup)
            originals.append((spm, backup, _is_gzip(spm)))
        for patch, (spm, _backup, compressed) in zip(patches, originals):
            _write_spm(spm, patch["text"], compressed)
        for patch in patches:
            verify_spm_text(patch["spm"], read_maybe_gzip_text(patch["spm"]), patch["materials"])
    except Exception:
        for spm, backup, _compressed in originals:
            if backup.exists():
                shutil.copy2(backup, spm)
        raise

    return {
        "spms": [patch["spm"] for patch in patches],
        "materials": sum(len(patch["materials"]) for patch in patches),
        "backup_dir": str(backup_dir),
        "skipped": skipped,
    }


def cleanup_preserved_cluster_outputs(plan):
    """Undo managed T_ artifacts for materials restored to cluster renders."""
    import sbs_auto

    cleaned = []
    conflicts = []
    seen = set()
    for row in plan.get("preserved_cluster_materials") or []:
        material = canonical_material_name(row.get("material_name"))
        key = (str(Path(row["spm"]).parent).lower(), material.lower())
        if key in seen:
            continue
        seen.add(key)
        folder = Path(row["spm"]).parent
        texture_base = texture_base_for_material(material)
        renamed = None
        for sbs in active_sbs_files(folder):
            t_graph = sbs_auto.find_m_graph_name(sbs, texture_base)
            m_graph = sbs_auto.find_m_graph_name(sbs, material)
            if t_graph and not m_graph:
                renamed = sbs_auto.rename_managed_graph(sbs, t_graph, material)
                break
            if t_graph and m_graph:
                conflicts.append({
                    "material": material,
                    "sbs": str(sbs),
                    "reason": "both M_ authoring and T_ managed graphs exist",
                })
        candidates = []
        for texture_dir in (folder / "texture", folder / "textures"):
            if not texture_dir.is_dir():
                continue
            for role in ("color", "normal", "extra", "height", "opacity", "subsurface"):
                candidates.extend(texture_dir.glob(f"{texture_base}_{role}.*"))
        files = [path for path in candidates if path.is_file()]
        if files:
            for path in files:
                path.unlink()
        cleaned.append({
            "material": material,
            "texture_base": texture_base,
            "graph_rename": renamed,
            "removed_outputs": [str(path) for path in files],
            "backup_dir": None,
        })
    return {"cleaned": cleaned, "conflicts": conflicts}


def jobs_from_texture_plan(plan, only_existing_sk=True, allowed_spms=None):
    """Build per-SPM material mappings from texture-plan rows.

    ``allowed_spms`` is an exact-path safety boundary for GUI selections. A
    folder can contain several SK variants; selecting one PCG target must not
    normalize every sibling merely because the audit plan mentions them.
    """
    allowed_keys = None
    if allowed_spms is not None:
        allowed_keys = {
            os.path.normcase(os.path.abspath(str(path)))
            for path in allowed_spms
        }

    def spm_is_allowed(spm):
        return allowed_keys is None or (
            os.path.normcase(os.path.abspath(str(spm))) in allowed_keys
        )

    def subsurface_is_real_source(
            row, target_spm=None, target_material_id=None):
        generated = re.compile(
            r"^[mt]_.*_subsurface\.(?:tga|png|tif|tiff|exr)$", re.IGNORECASE)
        if any(not generated.match(Path(str(ref)).name)
               for ref in row.get("source_subsurface") or []):
            return True
        # After normalization the SPM itself points at a managed T_ output,
        # which source inference intentionally excludes.  Preserve an already
        # enabled SubsurfaceColor slot so repeated audits/runs are idempotent.
        wanted = {
            canonical_material_name(name).lower()
            for name in (row.get("material_names") or [row.get("atlas_base")])
            if name
        }
        material_spms = (
            [target_spm] if target_spm is not None
            else (row.get("material_spms") or [])
        )
        for spm_value in material_spms:
            spm = Path(spm_value)
            if not spm.is_file():
                continue
            for material_id, material in inspect_material_slots(
                    read_maybe_gzip_text(spm)).items():
                if target_material_id is not None and material_id != str(
                        target_material_id).strip().lower():
                    continue
                if canonical_material_name(material["name"]).lower() not in wanted:
                    continue
                slot = material["slots"].get("subsurfacecolor")
                if slot and slot.get("enabled") == "true" and slot.get("filename"):
                    return True
        graph = row.get("m_graph")
        graph_sbs = row.get("m_graph_sbs")
        if graph and graph_sbs:
            try:
                import sbs_auto
                current = (sbs_auto.find_m_graph_name(graph_sbs, graph)
                           or sbs_auto.find_m_graph_name(
                               graph_sbs, row.get("texture_base") or "")
                           or graph)
                path = sbs_auto.parse_m_graph(graph_sbs, current)["inputs"].get("Subsurface")
                if path and "neutral_black" not in Path(path).name.lower():
                    return True
            except Exception:
                try:
                    current = (sbs_auto.find_m_graph_name(graph_sbs, graph)
                               or sbs_auto.find_m_graph_name(
                                   graph_sbs, row.get("texture_base") or "")
                               or graph)
                    source = sbs_auto.inspect_graph_sources(graph_sbs, current)
                    if any(any(token in Path(item["path"]).stem.lower()
                               for token in ("subsurface", "translucency"))
                           for item in source["bitmaps"] if item.get("path")):
                        return True
                except Exception:
                    pass
        return False

    def output_signature(output):
        mode = output.get("mode") or "managed"
        if mode == "preserve_source":
            return (
                mode,
                os.path.normcase(os.path.abspath(str(output.get("source_spm", "")))),
            )
        return (
            mode,
            os.path.normcase(os.path.abspath(str(output.get("texture_dir", "")))),
            str(output.get("texture_base", "")).lower(),
            bool(output.get("subsurface_enabled")),
        )

    def add_material_mapping(spm, key, output):
        material_map = by_spm.setdefault(str(spm), {})
        existing = material_map.get(key)
        if existing is not None and output_signature(existing) != output_signature(output):
            raise RuntimeError(
                f"{spm.name}: conflicting managed output mapping for {key}: "
                f"{existing.get('texture_base') or existing.get('source_spm')} vs "
                f"{output.get('texture_base') or output.get('source_spm')}")
        material_map[key] = output

    by_spm = {}
    for row in plan.get("items", []):
        texture_dir = row.get("texture_dir")
        texture_base = row.get("texture_base")
        if not texture_dir or not texture_base:
            continue
        material_targets = row.get("material_targets") or []
        if material_targets and not isinstance(material_targets, list):
            raise RuntimeError("material_targets must be a list")
        if material_targets:
            for target in material_targets:
                if not isinstance(target, dict) or not target.get("spm"):
                    raise RuntimeError(
                        "material_targets entry requires an SPM path")
                material_id = target.get("material_id")
                if material_id is None or not str(material_id).strip():
                    raise RuntimeError(
                        f"{Path(target['spm']).name}: material_targets entry "
                        "requires material_id")
                spm = Path(target["spm"])
                if not spm_is_allowed(spm):
                    continue
                if only_existing_sk and not spm.name.lower().startswith("sk_"):
                    continue
                if not spm.is_file():
                    continue
                key = f"@id:{str(material_id).strip().lower()}"
                add_material_mapping(spm, key, {
                    "texture_dir": texture_dir,
                    "texture_base": texture_base,
                    "subsurface_enabled": subsurface_is_real_source(
                        row,
                        target_spm=spm,
                        target_material_id=material_id,
                    ),
                })
            continue
        names = row.get("material_names") or [row.get("atlas_base")]
        for spm_value in row.get("material_spms") or []:
            spm = Path(spm_value)
            if not spm_is_allowed(spm):
                continue
            if only_existing_sk and not spm.name.lower().startswith("sk_"):
                continue
            if not spm.is_file():
                continue
            for name in names:
                canonical = canonical_material_name(name)
                add_material_mapping(spm, canonical.lower(), {
                    "texture_dir": texture_dir,
                    "texture_base": texture_base,
                    "subsurface_enabled": subsurface_is_real_source(
                        row, target_spm=spm
                    ),
                })
    for row in plan.get("preserved_cluster_materials") or []:
        spm = Path(row.get("spm", ""))
        if not spm_is_allowed(spm):
            continue
        if only_existing_sk and not spm.name.lower().startswith("sk_"):
            continue
        if not spm.is_file() or not Path(row.get("source_spm", "")).is_file():
            continue
        canonical = canonical_material_name(row.get("material_name"))
        add_material_mapping(spm, canonical.lower(), {
            "mode": "preserve_source",
            "source_spm": row["source_spm"],
        })
    return [
        {"spm": spm, "materials": materials}
        for spm, materials in sorted(by_spm.items(), key=lambda item: item[0].lower())
    ]
