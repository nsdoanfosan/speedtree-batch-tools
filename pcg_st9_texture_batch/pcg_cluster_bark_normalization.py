"""Fail-closed canonical bark normalization for isolated Cluster exports.

This is a library step of PCG ST9 Texture Batch, not a standalone tool.  The
read-only Cluster audit supplies the handoff receipt.  A caller maps each
receipt-owned source SPM to an isolated copy, normalizes only the bark
``Material_v8`` block in that copy, then exports it through the existing
SpeedTree/Blender handoff.  Production SPMs are rejected as write targets.
"""
from __future__ import annotations

import gzip
import hashlib
import os
import re
import xml.etree.ElementTree as ET
from pathlib import Path

try:
    from .pcg_cluster_assembly_contract import (
        display_export_name,
        inspect_fbx_material_mesh_pairs,
        normalize_export_name,
    )
    from .pcg_texture_audit import (
        MATERIAL_BLOCK_RE,
        MATERIAL_ID_RE,
        MATERIAL_RE,
        active_material_ids,
        extract_material_image_refs,
        mesh_asset_ids,
        read_maybe_gzip_text,
    )
except ImportError:
    from pcg_cluster_assembly_contract import (
        display_export_name,
        inspect_fbx_material_mesh_pairs,
        normalize_export_name,
    )
    from pcg_texture_audit import (
        MATERIAL_BLOCK_RE,
        MATERIAL_ID_RE,
        MATERIAL_RE,
        active_material_ids,
        extract_material_image_refs,
        mesh_asset_ids,
        read_maybe_gzip_text,
    )


SCHEMA_VERSION = 1
MAP_BLOCK_RE = re.compile(
    r'<Map\s+Name="([^"]+)"[^>]*>.*?(?:</Map>|<\\Map>)',
    re.IGNORECASE | re.DOTALL,
)
TEX_FILENAME_RE = re.compile(
    r'(<TexFilename\b(?![^>]*?/\s*>)[^>]*>)(.*?)(</TexFilename>|<\\TexFilename>)',
    re.IGNORECASE | re.DOTALL,
)
TEX_ENABLED_RE = re.compile(
    r'<TexEnabled\b[^>]*>(.*?)</TexEnabled>|<\\TexEnabled>',
    re.IGNORECASE | re.DOTALL,
)


class BarkNormalizationError(RuntimeError):
    """The isolated bark handoff is incomplete or ambiguous."""


def _path_key(path):
    return os.path.normcase(os.path.abspath(str(path)))


def _is_within(path, root):
    try:
        Path(path).resolve().relative_to(Path(root).resolve())
    except (OSError, ValueError):
        return False
    return True


def _sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _material_id(block):
    match = MATERIAL_ID_RE.search(block)
    return match.group(1) if match else ""


def _material_name(block):
    match = MATERIAL_RE.search(block)
    return match.group(2) if match else ""


def _export_material_base(value):
    name = display_export_name(value)
    if name.casefold().endswith("_mat"):
        name = name[:-4]
    return name.strip()


def _material_blocks(text, authored_name=None, material_id=None):
    wanted_name = normalize_export_name(authored_name) if authored_name else None
    wanted_id = str(material_id).casefold() if material_id is not None else None
    rows = []
    for match in MATERIAL_BLOCK_RE.finditer(text):
        block = match.group(0)
        if wanted_name is not None \
                and normalize_export_name(_material_name(block)) != wanted_name:
            continue
        if wanted_id is not None and _material_id(block).casefold() != wanted_id:
            continue
        rows.append((match, block))
    return rows


def _receipt_material_blocks(text, row):
    """Resolve a receipt bark slot by durable ID, then exact authored name.

    Export-name normalization intentionally treats ``Bark_x`` and ``M_Bark_x``
    as the same external identity.  Inside one SPM those can coexist while the
    detached legacy slot is retained as ``*_old``.  Selecting by normalized
    name would therefore mutate the wrong slot or report a false ambiguity.
    """
    material_id = str((row or {}).get("material_id") or "").strip()
    if material_id:
        return _material_blocks(text, material_id=material_id)
    authored_name = str((row or {}).get("material_name") or "").strip()
    if not authored_name:
        return []
    return [
        (match, block)
        for match, block in _material_blocks(text)
        if _material_name(block).casefold() == authored_name.casefold()
    ]


def _resolve_ref(spm, value):
    value = str(value or "").strip().replace("\\", os.sep).replace("/", os.sep)
    path = Path(value)
    if not path.is_absolute():
        path = Path(spm).parent / path
    return Path(os.path.abspath(os.path.normpath(str(path))))


def _tree_root_for_cluster_copy(spm):
    path = Path(spm).resolve()
    for parent in path.parents:
        if parent.name.casefold() == "cluster":
            return parent.parent
    raise BarkNormalizationError(
        f"isolated Cluster layout is required: {spm}")


def _canonical_texture_destination(source, canonical_spm, isolated_tree_root,
                                   explicit_map):
    source = Path(source).resolve()
    mapped = explicit_map.get(_path_key(source))
    if mapped:
        return Path(mapped).resolve()
    canonical_tree_root = Path(canonical_spm).resolve().parent
    try:
        relative = source.relative_to(canonical_tree_root)
    except ValueError as exc:
        raise BarkNormalizationError(
            "canonical bark texture is outside the Tree folder; an explicit "
            f"isolated texture mapping is required: {source}") from exc
    return (Path(isolated_tree_root).resolve() / relative).resolve()


def _rebased_canonical_maps(canonical_block, canonical_spm, isolated_spm,
                            isolated_tree_root, explicit_map):
    map_matches = list(MAP_BLOCK_RE.finditer(canonical_block))
    if not map_matches:
        raise BarkNormalizationError("canonical bark material has no Map blocks")
    rewritten = []
    texture_evidence = []
    for map_match in map_matches:
        block = map_match.group(0)
        tex_match = TEX_FILENAME_RE.search(block)
        authored = " ".join(tex_match.group(2).split()) if tex_match else ""
        if authored:
            enabled_match = TEX_ENABLED_RE.search(block)
            export_enabled = not enabled_match or (
                " ".join((enabled_match.group(1) or "").split()).casefold()
                not in {"false", "0"}
            )
            source = _resolve_ref(canonical_spm, authored)
            if not source.is_file():
                raise BarkNormalizationError(
                    f"canonical bark texture is missing: {source}")
            destination = _canonical_texture_destination(
                source, canonical_spm, isolated_tree_root, explicit_map)
            if not _is_within(destination, isolated_tree_root):
                raise BarkNormalizationError(
                    f"isolated bark texture escapes the Tree copy: {destination}")
            if not destination.is_file():
                raise BarkNormalizationError(
                    f"isolated canonical bark texture is missing: {destination}")
            source_hash = _sha256_file(source)
            destination_hash = _sha256_file(destination)
            if source_hash != destination_hash:
                raise BarkNormalizationError(
                    f"isolated canonical bark texture hash mismatch: {destination}")
            relative = os.path.relpath(destination, Path(isolated_spm).parent)
            relative = relative.replace("\\", "/")
            block = TEX_FILENAME_RE.sub(
                lambda match: match.group(1) + relative + match.group(3),
                block,
                count=1,
            )
            texture_evidence.append({
                "map": map_match.group(1),
                "source": str(source),
                "isolated": str(destination),
                "sha256": source_hash,
                "spm_ref": relative,
                "export_enabled": export_enabled,
            })
        rewritten.append(block)
    return rewritten, texture_evidence


def _replace_material_maps(target_block, canonical_maps, canonical_name):
    target_maps = list(MAP_BLOCK_RE.finditer(target_block))
    if not target_maps:
        raise BarkNormalizationError("Cluster bark material has no Map blocks")
    separator = (
        target_block[target_maps[0].end():target_maps[1].start()]
        if len(target_maps) > 1 else "\n"
    )
    replacement = separator.join(canonical_maps)
    patched = (
        target_block[:target_maps[0].start()]
        + replacement
        + target_block[target_maps[-1].end():]
    )
    return MATERIAL_RE.sub(
        lambda match: match.group(1) + canonical_name + match.group(3),
        patched,
        count=1,
    )


def _outside_material_hash(text, authored_name, material_id):
    matches = _material_blocks(text, authored_name, material_id)
    if len(matches) != 1:
        raise BarkNormalizationError(
            f"expected one bark material block, found {len(matches)}: "
            f"{authored_name} / ID {material_id}")
    match, _block = matches[0]
    marker = f"<CANONICAL_BARK_SLOT ID={material_id}>"
    value = text[:match.start()] + marker + text[match.end():]
    return _sha256_bytes(value.encode("utf-8"))


def _canonical_source(receipt):
    bark = receipt.get("canonical_bark") or {}
    expected = bark.get("canonical_material")
    if not expected:
        raise BarkNormalizationError("handoff receipt has no canonical bark name")
    candidates = []
    for row in bark.get("canonical_sources") or []:
        spm = Path(row.get("spm") or "")
        if not spm.is_file():
            raise BarkNormalizationError(f"canonical bark SPM is missing: {spm}")
        text = read_maybe_gzip_text(spm)
        blocks = _material_blocks(
            text,
            row.get("material_name") or expected,
            row.get("material_id"),
        )
        if len(blocks) != 1:
            raise BarkNormalizationError(
                f"canonical bark source is ambiguous: {spm}")
        block = blocks[0][1]
        candidates.append({
            "spm": spm,
            "block": block,
            "material_id": _material_id(block),
            "material_name": _material_name(block),
        })
    if not candidates:
        raise BarkNormalizationError("handoff receipt has no canonical bark source")
    # Multiple selected SK targets are acceptable only when their bark slot is
    # materially identical.  Paths are compared by resolved texture content.
    signatures = []
    for candidate in candidates:
        rows = extract_material_image_refs(candidate["spm"])
        row = next(
            (value for value in rows
             if str(value.get("material_id")) == candidate["material_id"]),
            None,
        )
        refs = []
        for value in (row or {}).get("refs") or []:
            path = _resolve_ref(candidate["spm"], value)
            refs.append((path.name.casefold(), _sha256_file(path) if path.is_file() else None))
        structural = TEX_FILENAME_RE.sub(
            lambda match: match.group(1) + "<TEXTURE>" + match.group(3),
            candidate["block"],
        )
        structural = MATERIAL_RE.sub(
            lambda match: match.group(1) + expected + match.group(3),
            structural,
            count=1,
        )
        signatures.append((_sha256_bytes(structural.encode("utf-8")), tuple(refs)))
    if len(set(signatures)) != 1:
        raise BarkNormalizationError(
            "selected final-SK targets disagree on the canonical bark slot")
    return expected, candidates[0]


def build_isolated_bark_normalization_plan(
        contract_or_handoff, isolated_spms, isolation_root,
        canonical_texture_map=None,
        preserve_source_material_name=False):
    """Build verified in-memory SPM patches from a PCG Cluster handoff.

    ``isolated_spms`` maps each receipt ``cluster_spm`` to its copy.  Only
    copies below ``isolation_root`` are accepted.  The function performs no
    writes; :func:`apply_isolated_bark_normalization` commits the patches.
    """
    handoff = contract_or_handoff.get("handoff", contract_or_handoff)
    bark = handoff.get("canonical_bark") or {}
    if bark.get("status") not in {"replacement_required", "canonical"}:
        raise BarkNormalizationError(
            f"canonical bark receipt is not actionable: {bark.get('status')}")
    if bark.get("status") == "canonical":
        return {
            "schema_version": SCHEMA_VERSION,
            "status": "already_canonical",
            "canonical_material": bark.get("canonical_material"),
            "isolation_root": str(Path(isolation_root).resolve()),
            "patches": [],
        }

    canonical_name, canonical = _canonical_source(handoff)
    mapping = {_path_key(key): Path(value) for key, value in isolated_spms.items()}
    explicit_textures = {
        _path_key(key): Path(value)
        for key, value in (canonical_texture_map or {}).items()
    }
    root = Path(isolation_root).resolve()
    patches = []
    required = [
        row for row in bark.get("cluster_bark_sources") or []
        if row.get("replacement") == "required"
    ]
    if not required:
        raise BarkNormalizationError(
            "replacement_required receipt has no bark replacement rows")

    for row in required:
        source_spm = Path(row.get("cluster_spm") or "")
        isolated_spm = mapping.get(_path_key(source_spm))
        if isolated_spm is None:
            raise BarkNormalizationError(
                f"isolated Cluster SPM mapping is missing: {source_spm}")
        isolated_spm = isolated_spm.resolve()
        if _path_key(isolated_spm) == _path_key(source_spm):
            raise BarkNormalizationError(
                f"production/source SPM cannot be a normalization target: {source_spm}")
        if not _is_within(isolated_spm, root):
            raise BarkNormalizationError(
                f"normalization target is outside the isolation root: {isolated_spm}")
        if not source_spm.is_file() or not isolated_spm.is_file():
            raise BarkNormalizationError(
                f"source/copy SPM is missing: {source_spm} / {isolated_spm}")
        source_hash = _sha256_file(source_spm)
        input_bytes = isolated_spm.read_bytes()
        if _sha256_bytes(input_bytes) != source_hash:
            raise BarkNormalizationError(
                f"isolated SPM is not an exact source copy: {isolated_spm}")

        before = read_maybe_gzip_text(isolated_spm)
        target_blocks = _receipt_material_blocks(before, row)
        if len(target_blocks) != 1:
            raise BarkNormalizationError(
                f"Cluster bark material is ambiguous in {isolated_spm}: "
                f"{row.get('material_name')}")
        target_match, target_block = target_blocks[0]
        material_id = _material_id(target_block)
        cutout_before = next(
            (
                value.get("cutout_mesh_ids") or []
                for value in extract_material_image_refs(isolated_spm)
                if str(value.get("material_id")) == material_id
            ),
            [],
        )
        tree_root = _tree_root_for_cluster_copy(isolated_spm)
        canonical_maps, textures = _rebased_canonical_maps(
            canonical["block"], canonical["spm"], isolated_spm,
            tree_root, explicit_textures,
        )
        output_material = (
            str(row.get("material_name") or "")
            if preserve_source_material_name
            else canonical_name
        )
        patched_block = _replace_material_maps(
            target_block, canonical_maps, output_material)
        after = before[:target_match.start()] + patched_block + before[target_match.end():]

        if _outside_material_hash(before, row.get("material_name"), material_id) \
                != _outside_material_hash(after, output_material, material_id):
            raise BarkNormalizationError(
                f"non-bark SPM payload changed unexpectedly: {isolated_spm}")
        before_active = sorted(str(value) for value in active_material_ids(isolated_spm))
        before_meshes = sorted(str(value) for value in mesh_asset_ids(isolated_spm))
        # Inspect post-patch structure from a temporary logical view without a
        # write: material/cutout IDs are parsed directly from the patched block.
        patched_id = _material_id(patched_block)
        cutout_after = re.findall(
            r"<CutoutMeshID\b[^>]*>([^<]*)</CutoutMeshID>",
            patched_block,
            re.IGNORECASE,
        )
        cutout_after.extend(re.findall(
            r'<CutoutMesh\b[^>]*\bID="([^"]+)"',
            patched_block,
            re.IGNORECASE,
        ))
        cutout_after = [
            value.strip() for value in cutout_after
            if value.strip() not in {"", "-1"}
        ]
        if patched_id != material_id or list(cutout_after) != list(cutout_before):
            raise BarkNormalizationError(
                f"bark material ID or cutout mesh binding changed: {isolated_spm}")

        patches.append({
            "source_spm": str(source_spm),
            "isolated_spm": str(isolated_spm),
            "input_sha256": _sha256_bytes(input_bytes),
            "input_compressed": input_bytes.startswith(b"\x1f\x8b"),
            "canonical_source_spm": str(canonical["spm"]),
            "canonical_material": canonical_name,
            "source_material": row.get("material_name"),
            "output_material": output_material,
            "source_material_name_preserved": bool(
                preserve_source_material_name
            ),
            "material_id": material_id,
            "active_material_ids": before_active,
            "mesh_asset_ids": before_meshes,
            "cutout_mesh_ids": list(cutout_before),
            "canonical_textures": textures,
            "uv_mesh_generator_payload_preserved": True,
            "_patched_text": after,
        })
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "ready",
        "canonical_material": canonical_name,
        "source_material_name_preserved": bool(
            preserve_source_material_name
        ),
        "isolation_root": str(root),
        "patches": patches,
    }


def _encoded_spm(text, compressed):
    raw = text.encode("utf-8")
    return gzip.compress(raw) if compressed else raw


def apply_isolated_bark_normalization(plan):
    """Atomically apply and re-read a preflighted isolated normalization plan."""
    if plan.get("status") == "already_canonical":
        return {**plan, "applied": False, "outputs": []}
    if plan.get("status") != "ready" or not plan.get("patches"):
        raise BarkNormalizationError("bark normalization plan is not ready")
    root = Path(plan["isolation_root"]).resolve()
    originals = {}
    temps = []
    try:
        # One provider SPM can render more than one bark material slot.  The
        # preflight deliberately emits one material patch per receipt row, but
        # every patch for the same SPM fingerprints the same immutable input.
        # Compose those disjoint Material_v8 replacements in memory and commit
        # the SPM once; applying the first patch before validating the second
        # would make our own write look like an external preflight race.
        grouped = {}
        for patch in plan["patches"]:
            path = Path(patch["isolated_spm"]).resolve()
            if not _is_within(path, root):
                raise BarkNormalizationError(
                    f"normalization target escaped the isolation root: {path}")
            original = path.read_bytes()
            if _sha256_bytes(original) != patch["input_sha256"]:
                raise BarkNormalizationError(
                    f"isolated SPM changed after preflight: {path}")
            group = grouped.get(path)
            if group is None:
                originals[path] = original
                group = {
                    "text": read_maybe_gzip_text(path),
                    "compressed": patch["input_compressed"],
                }
                grouped[path] = group
            elif group["compressed"] != patch["input_compressed"]:
                raise BarkNormalizationError(
                    f"isolated SPM compression contract disagrees: {path}")

            desired = _material_blocks(
                patch["_patched_text"],
                patch["output_material"],
                patch["material_id"],
            )
            current = _material_blocks(
                group["text"],
                material_id=patch["material_id"],
            )
            if len(desired) != 1 or len(current) != 1:
                raise BarkNormalizationError(
                    f"preflighted bark material patch is ambiguous: {path} "
                    f"(material ID {patch['material_id']})")
            match, _block = current[0]
            group["text"] = (
                group["text"][:match.start()]
                + desired[0][1]
                + group["text"][match.end():]
            )

        for path, group in grouped.items():
            payload = _encoded_spm(group["text"], group["compressed"])
            temp = path.with_name(path.name + ".canonical-bark.tmp")
            temp.write_bytes(payload)
            temps.append(temp)
            os.replace(temp, path)

        outputs = []
        for patch in plan["patches"]:
            path = Path(patch["isolated_spm"])
            text = read_maybe_gzip_text(path)
            blocks = _material_blocks(
                text, patch["output_material"], patch["material_id"])
            if len(blocks) != 1:
                raise BarkNormalizationError(
                    f"canonical bark write verification failed: {path}")
            if sorted(str(value) for value in active_material_ids(path)) \
                    != patch["active_material_ids"]:
                raise BarkNormalizationError(
                    f"active Generator material IDs changed: {path}")
            if sorted(str(value) for value in mesh_asset_ids(path)) \
                    != patch["mesh_asset_ids"]:
                raise BarkNormalizationError(f"mesh asset IDs changed: {path}")
            refs = {
                item.group(2).strip().replace("\\", "/").casefold()
                for item in TEX_FILENAME_RE.finditer(blocks[0][1])
                if item.group(2).strip()
            }
            expected = {
                item["spm_ref"].casefold()
                for item in patch["canonical_textures"]
            }
            if refs != expected:
                raise BarkNormalizationError(
                    f"canonical bark texture set verification failed: {path}")
            outputs.append({
                key: value for key, value in patch.items()
                if not key.startswith("_")
            } | {
                "output_sha256": _sha256_file(path),
                "status": "normalized",
            })
    except Exception:
        for path, data in originals.items():
            path.write_bytes(data)
        raise
    finally:
        for temp in temps:
            if temp.exists():
                temp.unlink()
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "normalized",
        "canonical_material": plan["canonical_material"],
        "isolation_root": str(root),
        "applied": True,
        "outputs": outputs,
        "production_sources_mutated": False,
        "next_gate": "export_and_validate_fbx_stmat_xml",
    }


def normalize_isolated_canonical_bark_name(
        contract_or_handoff, isolated_spm, isolation_root):
    """Case-normalize the final isolated SK bark slot to the receipt identity.

    PCG ③ may already have normalized the final SK texture connections while
    preserving an authored case variant such as ``M_Bark_elm_01``.  The
    Assembly base must carry the receipt's exact ``M_bark_elm_01`` name.  This
    operation changes only that Material_v8 Name attribute and refuses the
    receipt's authoring source as a target.
    """
    handoff = contract_or_handoff.get("handoff", contract_or_handoff)
    bark = handoff.get("canonical_bark") or {}
    canonical = str(bark.get("canonical_material") or "")
    sources = [
        Path(row.get("spm") or "").resolve()
        for row in bark.get("canonical_sources") or []
        if row.get("spm")
    ]
    target = Path(isolated_spm).resolve()
    root = Path(isolation_root).resolve()
    if not canonical or not sources:
        raise BarkNormalizationError("canonical bark receipt is incomplete")
    if not _is_within(target, root):
        raise BarkNormalizationError(
            f"canonical bark name target is outside isolation: {target}")
    if any(_path_key(target) == _path_key(source) for source in sources):
        raise BarkNormalizationError(
            f"production/source SPM cannot be a normalization target: {target}")
    if not target.is_file():
        raise BarkNormalizationError(f"isolated final SK SPM is missing: {target}")

    before_bytes = target.read_bytes()
    before = read_maybe_gzip_text(target)
    matches = [
        (match, block)
        for match, block in _material_blocks(before)
        if normalize_export_name(_material_name(block))
        == normalize_export_name(canonical)
    ]
    if len(matches) != 1:
        raise BarkNormalizationError(
            f"isolated final SK canonical bark slot is ambiguous: {target}")
    match, block = matches[0]
    authored = _material_name(block)
    if authored == canonical:
        return {
            "schema_version": SCHEMA_VERSION,
            "status": "already_canonical",
            "spm": str(target),
            "canonical_material": canonical,
            "sha256": _sha256_bytes(before_bytes),
            "production_sources_mutated": False,
        }
    material_id = _material_id(block)
    refs_before = sorted(
        value.replace("\\", "/")
        for row in extract_material_image_refs(target)
        if str(row.get("material_id")) == material_id
        for value in row.get("refs") or []
    )
    patched_block = MATERIAL_RE.sub(
        lambda value: value.group(1) + canonical + value.group(3),
        block,
        count=1,
    )
    after = before[:match.start()] + patched_block + before[match.end():]
    if _outside_material_hash(before, authored, material_id) != _outside_material_hash(
            after, canonical, material_id):
        raise BarkNormalizationError("non-bark final SK payload changed")
    payload = _encoded_spm(after, before_bytes.startswith(b"\x1f\x8b"))
    temporary = target.with_name(target.name + ".canonical-name.tmp")
    try:
        temporary.write_bytes(payload)
        os.replace(temporary, target)
        rows = extract_material_image_refs(target)
        final = [
            row for row in rows
            if str(row.get("material_id")) == material_id
        ]
        if len(final) != 1 or final[0].get("material_name") != canonical:
            raise BarkNormalizationError("canonical final SK bark name write failed")
        refs_after = sorted(
            value.replace("\\", "/") for value in final[0].get("refs") or []
        )
        if refs_after != refs_before:
            raise BarkNormalizationError("final SK bark texture references changed")
    except Exception:
        target.write_bytes(before_bytes)
        raise
    finally:
        if temporary.exists():
            temporary.unlink()
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "normalized",
        "spm": str(target),
        "source_material": authored,
        "canonical_material": canonical,
        "material_id": material_id,
        "input_sha256": _sha256_bytes(before_bytes),
        "output_sha256": _sha256_file(target),
        "texture_references_preserved": True,
        "production_sources_mutated": False,
    }


def _xml_material_evidence(path, canonical_name, expected_basenames):
    path = Path(path)
    if not path.is_file():
        raise BarkNormalizationError(f"export metadata is missing: {path}")
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError as exc:
        raise BarkNormalizationError(f"invalid export metadata XML: {path}") from exc
    materials = [
        element for element in root.iter()
        if element.tag.rsplit("}", 1)[-1].casefold() in {"material", "material_v8"}
        and _export_material_base(element.attrib.get("Name")).casefold()
        == str(canonical_name).casefold()
    ]
    if not materials:
        raise BarkNormalizationError(
            f"expected canonical bark material in {path.name}, found 0")
    canonical_identity = normalize_export_name(canonical_name)
    species_identity = canonical_identity
    if species_identity.startswith("bark_"):
        species_identity = species_identity[5:]
    if species_identity.endswith("_01"):
        species_identity = species_identity[:-3]
    species_token = re.sub(r"[^a-z0-9]+", "", species_identity.casefold())
    rows = []
    exact_set_found = False
    for material in materials:
        sources = {
            Path(element.attrib.get("Source") or "").name.casefold()
            for element in material.iter()
            if element.attrib.get("Source")
        }
        exact = set(expected_basenames).issubset(sources)
        exact_set_found = exact_set_found or exact
        same_species = bool(
            sources
            and species_token
            and all(
                species_token in re.sub(
                    r"[^a-z0-9]+", "", value.casefold()
                )
                for value in sources
            )
        )
        if not exact and not same_species:
            raise BarkNormalizationError(
                f"canonical bark alias in {path.name} uses another "
                f"texture family: {sorted(sources)}"
            )
        rows.append({
            "material": material.attrib.get("Name"),
            "texture_basenames": sorted(sources),
            "exact_normalized_set": exact,
            "same_species_alias": same_species,
        })
    if not exact_set_found:
        raise BarkNormalizationError(
            f"canonical bark textures did not propagate to {path.name}: "
            f"{sorted(expected_basenames)}")
    return {
        "path": str(path),
        "material": materials[0].attrib.get("Name"),
        "materials": rows,
        "material_count": len(rows),
        "texture_basenames": sorted({
            value for row in rows for value in row["texture_basenames"]
        }),
    }


def validate_canonical_bark_export_bundle(
        fbx, stmat, xml, normalization_report):
    """Fail closed unless FBX, STMAT and XML all carry the canonical bark.

    The FBX must contain a canonical material-to-mesh connection and UV data.
    STMAT/XML must reference the exact isolated canonical texture basenames.
    """
    if normalization_report.get("status") != "normalized":
        raise BarkNormalizationError(
            "an applied bark normalization report is required")
    canonical = normalization_report.get("canonical_material")
    output_materials = {
        str(output.get("output_material") or canonical)
        for output in normalization_report.get("outputs") or []
        if str(output.get("output_material") or canonical)
    }
    texture_rows = [
        texture
        for output in normalization_report.get("outputs") or []
        for texture in output.get("canonical_textures") or []
    ]
    expected_basenames = {
        Path(row["isolated"]).name.casefold()
        for row in texture_rows
        if row.get("export_enabled", True)
    }
    if not canonical or not output_materials or not expected_basenames:
        raise BarkNormalizationError(
            "normalization report has no canonical material/texture evidence")

    fbx = Path(fbx)
    fbx_report = inspect_fbx_material_mesh_pairs(fbx)
    if fbx_report.get("status") != "ok":
        raise BarkNormalizationError(
            f"FBX inspection failed: {fbx_report.get('error')}")
    pairs = [
        row for row in fbx_report.get("material_mesh_pairs") or []
        if _export_material_base(row.get("material")).casefold() in {
            value.casefold() for value in output_materials
        }
    ]
    if not pairs:
        raise BarkNormalizationError(
            "FBX canonical bark material is not connected to a mesh")
    with fbx.open("rb") as handle:
        has_uv = b"LayerElementUV" in handle.read()
    if not has_uv:
        raise BarkNormalizationError("FBX has no UV layer evidence")

    stmat_reports = [
        _xml_material_evidence(
            stmat, material, expected_basenames
        )
        for material in sorted(output_materials, key=str.casefold)
    ]
    xml_reports = [
        _xml_material_evidence(
            xml, material, expected_basenames
        )
        for material in sorted(output_materials, key=str.casefold)
    ]
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "ready_for_downstream_blender_mapping",
        "canonical_material": canonical,
        "output_materials": sorted(output_materials, key=str.casefold),
        "fbx": {
            "path": str(fbx),
            "format": fbx_report.get("format"),
            "material_mesh_pairs": pairs,
            "uv_layer_evidence": True,
        },
        "stmat": (
            stmat_reports[0]
            if len(stmat_reports) == 1
            else {"materials": stmat_reports}
        ),
        "xml": (
            xml_reports[0]
            if len(xml_reports) == 1
            else {"materials": xml_reports}
        ),
        "material_slot_propagated": True,
        "texture_set_propagated": True,
        "uv_preserved": True,
        "production_sources_mutated": False,
    }


__all__ = [
    "BarkNormalizationError",
    "apply_isolated_bark_normalization",
    "build_isolated_bark_normalization_plan",
    "normalize_isolated_canonical_bark_name",
    "validate_canonical_bark_export_bundle",
]
