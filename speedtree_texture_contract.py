"""Shared SpeedTree 10.1 managed-texture readiness contract.

The contract deliberately binds an exported STMAT material to the managed
``T_<set>_<role>`` paths referenced by that material.  Material display names
are not used to guess a texture set: differently named materials such as Green
and Dead may intentionally reference the same managed set.

All public return values contain only JSON-serializable standard-library types.
"""
from __future__ import annotations

import gzip
import hashlib
import html
import json
import os
import re
import xml.etree.ElementTree as ET
from pathlib import Path


REQUIRED_TEXTURE_ROLES = (
    "color",
    "normal",
    "extra",
    "height",
    "opacity",
    "subsurface",
)
TEXTURE_EXTENSIONS = (".tga", ".png", ".tif", ".tiff", ".exr")
CANONICAL_OUTPUT_MANIFEST_NAME = "pcg_st9_canonical_outputs.json"
CANONICAL_OUTPUT_MANIFEST_KIND = "pcg_st9_canonical_output_manifest"
CANONICAL_OUTPUT_MANIFEST_SCHEMA_VERSION = 1
TEXTURE_ORIGIN_CANONICAL_T = "canonical_t"
TEXTURE_ORIGIN_BLENDER_CLUSTER_BAKE = "blender_cluster_bake"
TEXTURE_ORIGIN_NEEDS_PCG_GENERATION = (
    "source_fallback_needs_pcg_generation"
)
PCG_ST9_REMEDIATION = (
    "PCG ST9 Texture에서 누락된 canonical T_* output을 생성한 뒤 "
    "다시 실행하십시오."
)

_DUPLICATE_SUFFIX_RE = re.compile(r"\.\d{3}$")
_ROLE_SUFFIX_RE = re.compile(
    r"^(?P<base>T_.+)_(?P<role>" + "|".join(REQUIRED_TEXTURE_ROLES) + r")$",
    re.IGNORECASE,
)
_MATERIAL_BLOCK_RE = re.compile(
    r"<Material_v\d+\b[^>]*>.*?</Material_v\d+>",
    re.IGNORECASE | re.DOTALL,
)
_MATERIAL_ID_RE = re.compile(
    r'<Material_v\d+\b[^>]*?\bID="([^"]*)"', re.IGNORECASE
)
_MATERIAL_NAME_RE = re.compile(
    r'<Material_v\d+\b[^>]*?\bName="([^"]*)"', re.IGNORECASE
)
_MAP_BLOCK_RE = re.compile(
    r'<Map\b[^>]*?\bName="([^"]*)"[^>]*>.*?(?:</Map>|<\\Map>)',
    re.IGNORECASE | re.DOTALL,
)
_TEX_FILENAME_RE = re.compile(
    r"(<TexFilename\b(?![^>]*?/\s*>)[^>]*>)(.*?)"
    r"(</TexFilename>|<\\TexFilename>)",
    re.IGNORECASE | re.DOTALL,
)
_FORBIDDEN_DERIVED_SEGMENTS = {
    ".sk_batch_isolated_bark",
    ".cache",
    "_cache",
    "cache",
    ".temp",
    "_temp",
    ".tmp",
    "_tmp",
    ".copied",
    "_copied",
    "copied",
    ".copy",
    "_copy",
    "copy",
    "_canonical_textures",
    "_external_textures",
}
_SLOT_ROLE_MAP = {
    "color": "color",
    "basecolor": "color",
    "base_color": "color",
    "diffuse": "color",
    "albedo": "color",
    "opacity": "opacity",
    "alpha": "opacity",
    "normal": "normal",
    "gloss": "extra",
    "roughness": "extra",
    "ao": "extra",
    "ambientocclusion": "extra",
    "ambient_occlusion": "extra",
    "height": "height",
    "displacement": "height",
    "subsurfacecolor": "subsurface",
    "subsurface_color": "subsurface",
    "subsurfaceamount": "subsurface",
    "subsurface_amount": "subsurface",
    "subsurface": "subsurface",
    "translucency": "subsurface",
}
_BLENDER_BAKE_ROLE_FOR_MAP = {
    "color": "color",
    "basecolor": "color",
    "opacity": "opacity",
    "alpha": "opacity",
    "normal": "normal",
    "gloss": "gloss",
    "subsurface": "subsurfacecolor",
    "subsurfacecolor": "subsurfacecolor",
    "subsurfaceamount": "subsurfaceamount",
    "ao": "ao",
    "ambientocclusion": "ao",
    "height": "height",
}


class CanonicalTextureContractError(RuntimeError):
    """A production SpeedTree texture manifest or binding is unsafe."""

    def __init__(self, message, issues=None):
        super().__init__(message)
        self.issues = list(issues or [])


def _absolute_path(value, relative_to=None):
    path = Path(str(value or "")).expanduser()
    if not path.is_absolute() and relative_to is not None:
        path = Path(relative_to) / path
    try:
        return path.resolve(strict=False)
    except (OSError, RuntimeError):
        return path.absolute()


def _path_key(value):
    return os.path.normcase(str(_absolute_path(value))).casefold()


def _local_name(tag):
    return str(tag or "").rsplit("}", 1)[-1]


def _attribute(node, name):
    expected = str(name).casefold()
    for key, value in node.attrib.items():
        if _local_name(key).casefold() == expected:
            return str(value or "").strip()
    return ""


def normalize_material_key(value):
    """Return a stable comparison key for a SpeedTree material name."""
    name = _DUPLICATE_SUFFIX_RE.sub("", str(value or "").strip())
    if name.casefold().endswith("_mat"):
        name = name[:-4]
    return re.sub(r"[^a-z0-9]+", "", name.casefold())


def normalize_texture_set_key(value):
    """Return a stable set key for a material, texture base, or texture path."""
    text = str(value or "").strip()
    if not text:
        return ""
    name = Path(text).stem if Path(text).suffix else Path(text).name
    parsed = parse_managed_texture_path(name)
    if parsed is not None:
        name = parsed["texture_base"]
    name = _DUPLICATE_SUFFIX_RE.sub("", name)
    if name[:2].casefold() in {"m_", "t_"}:
        name = name[2:]
    return re.sub(r"[^a-z0-9]+", "", name.casefold())


def parse_managed_texture_path(value):
    """Parse a managed ``T_<set>_<role>`` filename or path.

    ``None`` is returned for non-managed names.  Existence is intentionally not
    required because an STMAT reference can identify a set even when the
    referenced candidate directory is incomplete.
    """
    text = str(value or "").strip()
    if not text:
        return None
    path = Path(text)
    if path.suffix and path.suffix.casefold() not in TEXTURE_EXTENSIONS:
        return None
    match = _ROLE_SUFFIX_RE.match(path.stem)
    if match is None:
        return None
    texture_base = match.group("base")
    return {
        "path": text,
        "texture_base": texture_base,
        "set_key": normalize_texture_set_key(texture_base),
        "role": match.group("role").casefold(),
    }


def _read_spm_text(path):
    payload = Path(path).read_bytes()
    if payload.startswith(b"\x1f\x8b"):
        payload = gzip.decompress(payload)
    return payload.decode("utf-8")


def _write_spm_text(path, text, compressed):
    payload = text.encode("utf-8")
    if compressed:
        payload = gzip.compress(payload)
    target = Path(path)
    temporary = target.with_name(target.name + ".canonical-textures.tmp")
    try:
        temporary.write_bytes(payload)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def _is_within(path, root):
    try:
        _absolute_path(path).relative_to(_absolute_path(root))
        return True
    except ValueError:
        return False


def _forbidden_derived_segment(path):
    for part in Path(str(path or "")).parts:
        low = part.casefold()
        if (
            low in _FORBIDDEN_DERIVED_SEGMENTS
            or low.startswith(".bark-")
            or low.startswith(".sk_batch_")
        ):
            return part
    return ""


def _file_sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _blender_bake_map_role(value):
    compact = re.sub(
        r"[^a-z0-9]+", "", str(value or "").casefold()
    )
    return _BLENDER_BAKE_ROLE_FOR_MAP.get(compact, "")


def resolve_blender_cluster_bake_origin(
    spm,
    material,
    output,
    asset_root,
):
    """Return one normalized, live-proven Blender bake origin receipt.

    A stored receipt is only a shortcut to the physical manifest.  When it is
    absent, the current material's exact map slots are re-evaluated live.
    Downstream code consumes the normalized result instead of repeating
    filename or folder heuristics.
    """
    slots = list(material.get("slots") or [])
    provided = list(output.get("slot_files") or [])
    slot_files = []
    for slot in slots:
        matches = [
            row
            for row in provided
            if (
                int(row.get("map_index", -1)) == slot["map_index"]
                and str(row.get("map") or "") == slot["map"]
            )
        ]
        if provided and len(matches) != 1:
            return {}, "blender_cluster_bake_slot_contract_incomplete"
        row = matches[0] if matches else slot
        path = _absolute_path(
            row.get("path")
            or row.get("resolved_ref")
            or slot.get("resolved_ref")
        )
        slot_files.append({
            "map_index": int(slot["map_index"]),
            "map": str(slot["map"]),
            "role": str(slot.get("role") or ""),
            "path": str(path),
        })
    if not slot_files:
        return {}, "blender_cluster_bake_slot_contract_incomplete"
    parents = {
        _path_key(Path(row["path"]).parent)
        for row in slot_files
    }
    capture_dir = Path(slot_files[0]["path"]).parent
    if (
        len(parents) != 1
        or capture_dir.name.casefold() != "cluster"
        or not _is_within(capture_dir, asset_root)
        or _forbidden_derived_segment(capture_dir)
    ):
        return {}, "blender_cluster_bake_capture_boundary_mismatch"

    candidates = []
    stored = output.get("origin_receipt")
    if isinstance(stored, dict):
        explicit = _absolute_path(
            stored.get("physical_capture_manifest")
        )
        if explicit.is_file():
            candidates.append(explicit)
    color_rows = [
        row for row in slot_files
        if _blender_bake_map_role(row["map"]) == "color"
    ]
    if color_rows:
        color = Path(color_rows[0]["path"])
        exact = capture_dir / (
            f"{color.stem}_auto_capture_manifest.json"
        )
        if exact.is_file() and exact not in candidates:
            candidates.append(exact)
    candidates.extend(
        path
        for path in sorted(
            capture_dir.glob("*_auto_capture_manifest.json")
        )
        if path not in candidates
    )
    if not candidates:
        return {}, "blender_cluster_bake_capture_manifest_missing"

    mismatch = "blender_cluster_bake_map_role_mismatch"
    for manifest_path in candidates:
        if (
            manifest_path.parent != capture_dir
            or not _is_within(manifest_path, asset_root)
            or _forbidden_derived_segment(manifest_path)
        ):
            continue
        try:
            payload = json.loads(
                manifest_path.read_text(encoding="utf-8")
            )
        except (OSError, ValueError, json.JSONDecodeError):
            mismatch = "blender_cluster_bake_capture_manifest_invalid"
            continue
        if (
            not isinstance(payload, dict)
            or payload.get("kind")
            != "speedtree_cluster_blender_auto_capture"
            or int(payload.get("version") or 0) < 2
            or payload.get("workflow_mode")
            != "PHYSICAL_DIRECT_CAPTURE"
            or payload.get("direct_uv_source")
            != "same_blender_physical_capture_projection"
        ):
            mismatch = "blender_cluster_bake_capture_manifest_invalid"
            continue
        contract_hash = str(
            payload.get("physical_capture_contract_sha256")
            or (payload.get("physical_capture_contract") or {}).get(
                "contract_sha256"
            )
            or ""
        ).strip().casefold()
        if not re.fullmatch(r"[0-9a-f]{64}", contract_hash):
            mismatch = "blender_cluster_bake_contract_hash_invalid"
            continue

        declared = {}
        for row in payload.get("maps") or []:
            if not isinstance(row, dict):
                continue
            role = _blender_bake_map_role(row.get("role"))
            path_text = str(row.get("path") or "").strip()
            sha256 = str(row.get("sha256") or "").strip().casefold()
            if not role or not path_text or not re.fullmatch(
                r"[0-9a-f]{64}", sha256
            ):
                continue
            path = _absolute_path(path_text, manifest_path.parent)
            if (
                path.is_file()
                and path.parent == manifest_path.parent
                and not _forbidden_derived_segment(path)
            ):
                declared.setdefault(role, []).append((path, sha256))

        normalized_slots = []
        for row in slot_files:
            expected_role = _blender_bake_map_role(row["map"])
            expected_path = _absolute_path(row["path"])
            match = next(
                (
                    (path, sha256)
                    for path, sha256 in declared.get(
                        expected_role, []
                    )
                    if _path_key(path) == _path_key(expected_path)
                ),
                None,
            )
            if match is None:
                mismatch = "blender_cluster_bake_map_role_mismatch"
                break
            if _file_sha256(expected_path).casefold() != match[1]:
                mismatch = (
                    "blender_cluster_bake_file_fingerprint_mismatch"
                )
                break
            normalized_slots.append({
                **row,
                "capture_role": expected_role,
                "sha256": match[1],
            })
        else:
            return {
                "kind": "blender_cluster_bake_texture_origin_receipt",
                "version": 1,
                "origin_state": "blender_cluster_bake",
                "source_origin": "blender_cluster_bake",
                "source_spm": str(_absolute_path(spm)),
                "material_id": str(
                    material.get("material_id") or ""
                ),
                "material_name": str(
                    material.get("material_name") or ""
                ),
                "physical_capture_manifest": str(
                    manifest_path.resolve()
                ),
                "physical_capture_contract_sha256": contract_hash,
                "source_refs": [
                    row["path"] for row in normalized_slots
                ],
                "slot_files": normalized_slots,
            }, ""
    return {}, mismatch


def validate_blender_cluster_bake_override(
    spm,
    material,
    output,
    asset_root,
):
    """Compatibility wrapper returning only the normalized error code."""
    _receipt, issue = resolve_blender_cluster_bake_origin(
        spm, material, output, asset_root
    )
    return issue


def texture_role_for_slot(map_name, authored_ref=""):
    """Return the canonical output role for one SpeedTree Map slot.

    Slot semantics are authoritative.  A filename suffix is used only as a
    compatibility aid for old SPMs whose map label is non-semantic (for
    example ``Map0``); it never selects a texture set or a fallback file.
    """
    key = re.sub(r"[^a-z0-9_]+", "", str(map_name or "").casefold())
    role = _SLOT_ROLE_MAP.get(key)
    if role:
        return role
    compact = key.replace("_", "")
    role = _SLOT_ROLE_MAP.get(compact)
    if role:
        return role
    parsed = parse_managed_texture_path(authored_ref)
    if parsed is not None:
        return parsed["role"]
    stem = Path(str(authored_ref or "")).stem.casefold()
    suffixes = (
        ("subsurface", "subsurface"),
        ("translucency", "subsurface"),
        ("displacement", "height"),
        ("height", "height"),
        ("ambient_occlusion", "extra"),
        ("roughness", "extra"),
        ("gloss", "extra"),
        ("normal", "normal"),
        ("opacity", "opacity"),
        ("alpha", "opacity"),
        ("albedo", "color"),
        ("basecolor", "color"),
        ("diffuse", "color"),
        ("color", "color"),
    )
    for suffix, inferred in suffixes:
        if re.search(r"(?:^|[_\-.])" + re.escape(suffix) + r"$", stem):
            return inferred
    return ""


def inspect_spm_texture_slots(spm_path):
    """Return non-empty material-owned TexFilename slots from one SPM."""
    spm = _absolute_path(spm_path)
    text = _read_spm_text(spm)
    materials = []
    for material_index, material_match in enumerate(
        _MATERIAL_BLOCK_RE.finditer(text)
    ):
        block = material_match.group(0)
        id_match = _MATERIAL_ID_RE.search(block)
        name_match = _MATERIAL_NAME_RE.search(block)
        material_id = html.unescape(id_match.group(1)) if id_match else ""
        material_name = html.unescape(name_match.group(1)) if name_match else ""
        slots = []
        for map_index, map_match in enumerate(_MAP_BLOCK_RE.finditer(block)):
            map_block = map_match.group(0)
            texture_match = _TEX_FILENAME_RE.search(map_block)
            if texture_match is None:
                continue
            authored = html.unescape(" ".join(texture_match.group(2).split()))
            if not authored:
                continue
            slots.append({
                "map_index": map_index,
                "map": html.unescape(map_match.group(1)),
                "role": texture_role_for_slot(map_match.group(1), authored),
                "authored_ref": authored,
                "resolved_ref": str(_absolute_path(authored, spm.parent)),
            })
        materials.append({
            "material_index": material_index,
            "material_id": material_id,
            "material_name": material_name,
            "slots": slots,
        })
    return {
        "spm": str(spm),
        "materials": materials,
        "texture_slot_count": sum(
            len(material["slots"]) for material in materials
        ),
    }


def canonical_output_manifest_candidates(spm_path):
    """Return only the production asset's ``texture`` manifest candidates."""
    spm = _absolute_path(spm_path)
    asset_root = spm.parent
    cluster_root = next(
        (
            parent
            for parent in spm.parents
            if parent.name.casefold() == "cluster"
        ),
        None,
    )
    if cluster_root is not None:
        asset_root = cluster_root.parent
    candidates = []
    seen = set()
    for directory_name in ("texture", "textures"):
        candidate = (
            asset_root / directory_name / CANONICAL_OUTPUT_MANIFEST_NAME
        )
        key = _path_key(candidate)
        if key in seen:
            continue
        seen.add(key)
        candidates.append(candidate)
    return candidates


def _manifest_issue(reason, **details):
    return {
        "reason": reason,
        "remediation": PCG_ST9_REMEDIATION,
        **details,
    }


def _manifest_path(value, base):
    path = Path(str(value or "")).expanduser()
    if path.is_absolute():
        return _absolute_path(path)
    return _absolute_path(path, base)


def load_canonical_output_manifest(spm_path, manifest_path=None):
    """Load and strictly validate the asset-local PCG canonical manifest."""
    production_spm = _absolute_path(spm_path)
    if manifest_path is None:
        candidates = canonical_output_manifest_candidates(production_spm)
        manifest = next((path for path in candidates if path.is_file()), None)
    else:
        manifest = _absolute_path(manifest_path)
        candidates = [manifest]
    if manifest is None or not manifest.is_file():
        expected = str(candidates[0]) if candidates else ""
        issues = [_manifest_issue(
            "canonical_output_manifest_missing",
            spm=str(production_spm),
            material="*",
            material_id="",
            role="*",
            expected_output=expected,
        )]
        raise CanonicalTextureContractError(
            f"{production_spm.name}: canonical output manifest is missing: "
            f"{expected}. {PCG_ST9_REMEDIATION}",
            issues,
        )
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        issues = [_manifest_issue(
            "canonical_output_manifest_invalid_json",
            spm=str(production_spm),
            material="*",
            material_id="",
            role="*",
            expected_output=str(manifest),
            error=str(exc),
        )]
        raise CanonicalTextureContractError(
            f"{production_spm.name}: canonical output manifest is invalid: "
            f"{manifest}: {exc}",
            issues,
        ) from exc
    if (
        payload.get("kind") != CANONICAL_OUTPUT_MANIFEST_KIND
        or payload.get("schema_version")
        != CANONICAL_OUTPUT_MANIFEST_SCHEMA_VERSION
    ):
        issues = [_manifest_issue(
            "canonical_output_manifest_schema_mismatch",
            spm=str(production_spm),
            material="*",
            material_id="",
            role="*",
            expected_output=str(manifest),
            kind=payload.get("kind"),
            schema_version=payload.get("schema_version"),
        )]
        raise CanonicalTextureContractError(
            f"{production_spm.name}: unsupported canonical output manifest "
            f"contract: {manifest}",
            issues,
        )

    asset_root = _manifest_path(payload.get("asset_root"), manifest.parent)
    texture_root = _manifest_path(payload.get("texture_root"), manifest.parent)
    declared_target_keys = {
        _path_key(_manifest_path(target.get("spm"), asset_root))
        for output in payload.get("outputs") or []
        if isinstance(output, dict)
        for target in output.get("material_targets") or []
        if isinstance(target, dict) and target.get("spm")
    }
    issues = []
    if (
        not _is_within(production_spm, asset_root)
        and _path_key(production_spm) not in declared_target_keys
    ):
        issues.append(_manifest_issue(
            "production_spm_outside_manifest_asset",
            spm=str(production_spm),
            material="*",
            material_id="",
            role="*",
            expected_output=str(asset_root),
        ))
    if not _is_within(manifest, texture_root):
        issues.append(_manifest_issue(
            "manifest_outside_texture_root",
            spm=str(production_spm),
            material="*",
            material_id="",
            role="*",
            expected_output=str(texture_root),
        ))
    if _forbidden_derived_segment(production_spm):
        issues.append(_manifest_issue(
            "production_spm_is_derived_cache",
            spm=str(production_spm),
            material="*",
            material_id="",
            role="*",
            expected_output=str(asset_root),
        ))

    normalized_outputs = []
    for output_index, raw_output in enumerate(payload.get("outputs") or []):
        texture_base = str(raw_output.get("texture_base") or "").strip()
        required_roles = [
            str(role or "").strip().casefold()
            for role in raw_output.get("required_roles") or []
        ]
        files = {}
        output_issues = []
        if (
            not texture_base.casefold().startswith("t_")
            or not normalize_texture_set_key(texture_base)
        ):
            output_issues.append(_manifest_issue(
                "invalid_texture_base",
                spm=str(production_spm),
                material="*",
                material_id="",
                role="*",
                expected_output=str(texture_root),
                texture_base=texture_base,
            ))
        if (
            set(required_roles) != set(REQUIRED_TEXTURE_ROLES)
            or len(required_roles) != len(REQUIRED_TEXTURE_ROLES)
        ):
            output_issues.append(_manifest_issue(
                "invalid_required_roles",
                spm=str(production_spm),
                material="*",
                material_id="",
                role="*",
                expected_output=str(texture_root / f"{texture_base}_<role>.tga"),
                texture_base=texture_base,
                required_roles=required_roles,
            ))
        raw_files = raw_output.get("files") or {}
        for role in required_roles:
            raw_value = str(raw_files.get(role) or "").strip()
            expected = (
                _manifest_path(raw_value, manifest.parent)
                if raw_value
                else texture_root / f"{texture_base}_{role}.tga"
            )
            parsed = parse_managed_texture_path(expected)
            reason = ""
            if not raw_value:
                reason = "canonical_output_role_undeclared"
            elif Path(raw_value).is_absolute():
                reason = "canonical_output_must_be_manifest_relative"
            elif not _is_within(expected, texture_root):
                reason = "canonical_output_outside_texture_root"
            elif _forbidden_derived_segment(expected):
                reason = "canonical_output_uses_derived_cache"
            elif (
                parsed is None
                or parsed["texture_base"].casefold() != texture_base.casefold()
                or parsed["role"] != role
            ):
                reason = "canonical_output_role_mismatch"
            elif not expected.is_file():
                reason = "canonical_output_missing"
            if reason:
                output_issues.append(_manifest_issue(
                    reason,
                    spm=str(production_spm),
                    material="*",
                    material_id="",
                    role=role,
                    expected_output=str(expected),
                    texture_base=texture_base,
                ))
            else:
                files[role] = str(expected)
        targets = []
        for target in raw_output.get("material_targets") or []:
            target_spm = _manifest_path(
                target.get("spm"), asset_root
            )
            targets.append({
                "spm": str(target_spm),
                "material_id": str(target.get("material_id") or "").strip(),
                "material_name": str(
                    target.get("material_name") or ""
                ).strip(),
            })
        if not targets:
            output_issues.append(_manifest_issue(
                "canonical_output_has_no_material_targets",
                spm=str(production_spm),
                material="*",
                material_id="",
                role="*",
                expected_output=str(texture_root / f"{texture_base}_<role>.tga"),
                texture_base=texture_base,
            ))
        elif output_issues:
            scoped_targets = [
                target for target in targets
                if _path_key(target["spm"]) == _path_key(production_spm)
            ] or targets
            scoped_issues = []
            for issue in output_issues:
                if issue.get("material") != "*":
                    scoped_issues.append(issue)
                    continue
                for target in scoped_targets:
                    scoped_issues.append({
                        **issue,
                        "spm": target["spm"],
                        "material": target["material_name"],
                        "material_id": target["material_id"],
                    })
            output_issues = scoped_issues
        issues.extend(output_issues)
        normalized_outputs.append({
            "output_index": output_index,
            "texture_base": texture_base,
            "required_roles": required_roles,
            "files": files,
            "material_targets": targets,
            "producer": raw_output.get("producer") or {},
        })
    if not normalized_outputs:
        issues.append(_manifest_issue(
            "canonical_output_manifest_empty",
            spm=str(production_spm),
            material="*",
            material_id="",
            role="*",
            expected_output=str(texture_root),
        ))
    if issues:
        raise CanonicalTextureContractError(
            f"{production_spm.name}: canonical output manifest is not ready "
            f"({len(issues)} issue(s)). {PCG_ST9_REMEDIATION}",
            issues,
        )
    return {
        "kind": CANONICAL_OUTPUT_MANIFEST_KIND,
        "schema_version": CANONICAL_OUTPUT_MANIFEST_SCHEMA_VERSION,
        "manifest": str(manifest),
        "asset_root": str(asset_root),
        "texture_root": str(texture_root),
        "shared_owner": not _is_within(production_spm, asset_root),
        "outputs": normalized_outputs,
    }


def resolve_manifest_material_output(
    manifest,
    production_spm,
    material_id="",
    material_name="",
):
    """Resolve one manifest output, preferring exact material ID."""
    spm_key = _path_key(production_spm)
    material_id = str(material_id or "").strip()
    material_name_key = str(material_name or "").strip().casefold()
    id_matches = []
    name_matches = []
    for output in manifest.get("outputs") or []:
        for target in output.get("material_targets") or []:
            if _path_key(target.get("spm") or "") != spm_key:
                continue
            target_id = str(target.get("material_id") or "").strip()
            target_name = str(target.get("material_name") or "").strip()
            if material_id and target_id and target_id == material_id:
                id_matches.append(output)
            elif (
                material_name_key
                and not target_id
                and target_name.casefold() == material_name_key
            ):
                name_matches.append(output)
    matches = id_matches or name_matches
    unique = {
        (
            row["texture_base"].casefold(),
            tuple(
                (role, _path_key(path))
                for role, path in sorted(row["files"].items())
            ),
        ): row
        for row in matches
    }
    if len(unique) == 1:
        output = next(iter(unique.values()))
        return {
            **output,
            "match_policy": "material_id" if id_matches else "material_name",
        }
    return None


def build_spm_canonical_texture_plan(
    production_spm,
    manifest_path=None,
    material_output_overrides=None,
):
    """Bind every material-owned texture ref to one manifest output set."""
    spm = _absolute_path(production_spm)
    inspection = inspect_spm_texture_slots(spm)
    overrides = {
        str(key): value
        for key, value in (material_output_overrides or {}).items()
    }
    referenced_materials = [
        material for material in inspection["materials"]
        if material["slots"]
    ]
    needs_manifest = any(
        str(material["material_id"]) not in overrides
        for material in referenced_materials
    )
    if needs_manifest:
        try:
            manifest = load_canonical_output_manifest(spm, manifest_path)
        except CanonicalTextureContractError as exc:
            return {
                "status": "blocked",
                "spm": str(spm),
                "manifest": "",
                "asset_root": "",
                "texture_root": "",
                "bindings": [],
                "issues": list(exc.issues),
                "error": str(exc),
            }
    else:
        asset_root = next(
            (
                parent for parent in spm.parents
                if parent.name.casefold() == "cluster"
            ),
            spm.parent,
        )
        if asset_root.name.casefold() == "cluster":
            asset_root = asset_root.parent
        manifest = {
            "manifest": "",
            "asset_root": str(asset_root),
            "texture_root": str(asset_root / "texture"),
            "outputs": [],
        }
    bindings = []
    issues = []
    for material in inspection["materials"]:
        if not material["slots"]:
            continue
        material_id = material["material_id"]
        output = overrides.get(material_id)
        match_policy = "override"
        if output is None:
            output = resolve_manifest_material_output(
                manifest,
                spm,
                material_id,
                material["material_name"],
            )
            match_policy = (
                str(output.get("match_policy") or "manifest")
                if output
                else ""
            )
        if output is None:
            for slot in material["slots"]:
                role = slot["role"] or "unknown"
                issues.append(_manifest_issue(
                    "material_canonical_output_unmapped",
                    spm=str(spm),
                    material=material["material_name"],
                    material_id=material_id,
                    role=role,
                    expected_output=str(
                        Path(manifest["texture_root"])
                        / f"<manifest texture_base>_{role}.tga"
                    ),
                    authored_ref=slot["authored_ref"],
                ))
            continue
        origin_kind = str(
            output.get("origin_kind") or "pcg_sbs"
        ).strip().casefold()
        if origin_kind not in {"pcg_sbs", "blender_cluster_bake"}:
            for slot in material["slots"]:
                issues.append(_manifest_issue(
                    "material_texture_origin_invalid",
                    spm=str(spm),
                    material=material["material_name"],
                    material_id=material_id,
                    role=slot["role"] or "unknown",
                    expected_output=str(
                        (output.get("files") or {}).get(slot["role"]) or ""
                    ),
                    origin_kind=origin_kind,
                ))
            continue
        if origin_kind == "blender_cluster_bake":
            normalized_receipt, receipt_issue = (
                resolve_blender_cluster_bake_origin(
                    spm,
                    material,
                    output,
                    manifest["asset_root"],
                )
            )
            if receipt_issue:
                for slot in material["slots"]:
                    issues.append(_manifest_issue(
                        receipt_issue,
                        spm=str(spm),
                        material=material["material_name"],
                        material_id=material_id,
                        role=slot["role"] or "unknown",
                        expected_output="",
                        map=slot["map"],
                        authored_ref=slot["authored_ref"],
                        origin_kind=origin_kind,
                    ))
                continue
            output = {
                **output,
                "origin_receipt": normalized_receipt,
                "slot_files": list(
                    normalized_receipt.get("slot_files") or []
                ),
            }
        slot_rows = []
        for slot in material["slots"]:
            role = slot["role"]
            if origin_kind == "blender_cluster_bake":
                slot_matches = [
                    row
                    for row in output.get("slot_files") or []
                    if (
                        int(row.get("map_index", -1)) == slot["map_index"]
                        and str(row.get("map") or "") == slot["map"]
                    )
                ]
                expected = (
                    str(slot_matches[0].get("path") or "")
                    if len(slot_matches) == 1
                    else ""
                )
            else:
                expected = str(
                    (output.get("files") or {}).get(role) or ""
                )
            if not role:
                issues.append(_manifest_issue(
                    "speedtree_texture_role_unknown",
                    spm=str(spm),
                    material=material["material_name"],
                    material_id=material_id,
                    role="unknown",
                    expected_output=str(
                        Path(manifest["texture_root"])
                        / (
                            f"{output.get('texture_base') or '<preserved>'}"
                            "_<role>.tga"
                        )
                    ),
                    map=slot["map"],
                    authored_ref=slot["authored_ref"],
                ))
                continue
            if not expected:
                issues.append(_manifest_issue(
                    "material_canonical_role_unmapped",
                    spm=str(spm),
                    material=material["material_name"],
                    material_id=material_id,
                    role=role,
                    expected_output=str(
                        Path(manifest["texture_root"])
                        / (
                            f"{output.get('texture_base') or '<preserved>'}"
                            f"_{role}.tga"
                        )
                    ),
                    map=slot["map"],
                    authored_ref=slot["authored_ref"],
                ))
                continue
            expected_path = _absolute_path(expected)
            forbidden = _forbidden_derived_segment(expected_path)
            if forbidden or not expected_path.is_file():
                issues.append(_manifest_issue(
                    (
                        "blender_cluster_bake_uses_derived_cache"
                        if forbidden
                        else "blender_cluster_bake_output_missing"
                    ) if origin_kind == "blender_cluster_bake"
                    else (
                        "canonical_output_uses_derived_cache"
                        if forbidden
                        else "canonical_output_missing"
                    ),
                    spm=str(spm),
                    material=material["material_name"],
                    material_id=material_id,
                    role=role,
                    expected_output=str(expected_path),
                    map=slot["map"],
                    authored_ref=slot["authored_ref"],
                    origin_kind=origin_kind,
                    forbidden_segment=forbidden,
                ))
                continue
            slot_rows.append({
                **slot,
                "expected_output": str(expected_path),
            })
        bindings.append({
            "material_index": material["material_index"],
            "material_id": material_id,
            "material_name": material["material_name"],
            "texture_base": str(output.get("texture_base") or ""),
            "required_roles": list(
                output.get("required_roles")
                or (output.get("files") or {}).keys()
            ),
            "files": dict(output["files"]),
            "slot_files": list(output.get("slot_files") or []),
            "match_policy": match_policy,
            "origin_kind": origin_kind,
            "origin_state": (
                TEXTURE_ORIGIN_BLENDER_CLUSTER_BAKE
                if origin_kind == "blender_cluster_bake"
                else TEXTURE_ORIGIN_CANONICAL_T
            ),
            "origin_receipt": dict(output.get("origin_receipt") or {}),
            "slots": slot_rows,
        })
    return {
        "status": (
            "blocked" if issues else
            "not_applicable" if not bindings else
            "ok"
        ),
        "spm": str(spm),
        "manifest": manifest["manifest"],
        "asset_root": manifest["asset_root"],
        "texture_root": manifest["texture_root"],
        "bindings": bindings,
        "issues": issues,
        "remediation": PCG_ST9_REMEDIATION if issues else "",
    }


def inspect_production_spm_texture_contract(
    production_spm,
    manifest_path=None,
    material_output_overrides=None,
):
    """Fail-closed classification of production SPM TexFilename references."""
    plan = build_spm_canonical_texture_plan(
        production_spm,
        manifest_path,
        material_output_overrides,
    )
    if plan["status"] == "blocked":
        return plan
    issues = []
    checked = []
    for binding in plan["bindings"]:
        for slot in binding["slots"]:
            resolved = _absolute_path(
                slot["authored_ref"], Path(plan["spm"]).parent
            )
            forbidden = (
                _forbidden_derived_segment(slot["authored_ref"])
                or _forbidden_derived_segment(resolved)
            )
            reason = ""
            if forbidden:
                reason = "production_texture_uses_derived_cache"
            elif _path_key(resolved) != _path_key(slot["expected_output"]):
                reason = "production_texture_not_canonical_output"
            if reason:
                issues.append(_manifest_issue(
                    reason,
                    spm=plan["spm"],
                    material=binding["material_name"],
                    material_id=binding["material_id"],
                    role=slot["role"],
                    expected_output=slot["expected_output"],
                    authored_ref=slot["authored_ref"],
                    resolved_ref=str(resolved),
                    forbidden_segment=forbidden,
                ))
            checked.append({
                "material": binding["material_name"],
                "material_id": binding["material_id"],
                "role": slot["role"],
                "authored_ref": slot["authored_ref"],
                "resolved_ref": str(resolved),
                "expected_output": slot["expected_output"],
                "classification": reason or "canonical_output",
            })
    return {
        **plan,
        "status": "blocked" if issues else plan["status"],
        "issues": issues,
        "references": checked,
        "remediation": PCG_ST9_REMEDIATION if issues else "",
    }


def rebase_spm_copy_to_canonical_outputs(isolated_spm, production_spm, plan):
    """Rewrite only an isolated copy to its production manifest T_* paths."""
    isolated = _absolute_path(isolated_spm)
    production = _absolute_path(production_spm)
    if _path_key(isolated) == _path_key(production):
        raise CanonicalTextureContractError(
            f"production SPM cannot be a canonical rebase target: {production}"
        )
    if plan.get("status") != "ok":
        issues = list(plan.get("issues") or [])
        raise CanonicalTextureContractError(
            f"{production.name}: canonical texture rebase is blocked. "
            f"{PCG_ST9_REMEDIATION}",
            issues,
        )
    by_id = {
        str(row.get("material_id") or ""): row
        for row in plan.get("bindings") or []
        if str(row.get("material_id") or "")
    }
    by_name = {}
    for row in plan.get("bindings") or []:
        by_name.setdefault(
            str(row.get("material_name") or "").casefold(), []
        ).append(row)
    before_bytes = isolated.read_bytes()
    compressed = before_bytes.startswith(b"\x1f\x8b")
    before = _read_spm_text(isolated)
    rewritten = []
    issues = []

    def replace_material(material_match):
        block = material_match.group(0)
        id_match = _MATERIAL_ID_RE.search(block)
        name_match = _MATERIAL_NAME_RE.search(block)
        material_id = html.unescape(id_match.group(1)) if id_match else ""
        material_name = (
            html.unescape(name_match.group(1)) if name_match else ""
        )
        binding = by_id.get(material_id)
        if binding is None:
            candidates = by_name.get(material_name.casefold()) or []
            binding = candidates[0] if len(candidates) == 1 else None
        if binding is None:
            return block

        map_index = -1

        def replace_map(map_match):
            nonlocal map_index
            map_index += 1
            map_block = map_match.group(0)
            texture_match = _TEX_FILENAME_RE.search(map_block)
            if texture_match is None:
                return map_block
            authored = html.unescape(" ".join(texture_match.group(2).split()))
            if not authored:
                return map_block
            role = texture_role_for_slot(map_match.group(1), authored)
            if binding.get("origin_kind") == "blender_cluster_bake":
                slot_matches = [
                    row
                    for row in binding.get("slot_files") or []
                    if (
                        int(row.get("map_index", -1)) == map_index
                        and str(row.get("map") or "")
                        == html.unescape(map_match.group(1))
                    )
                ]
                expected = (
                    str(slot_matches[0].get("path") or "")
                    if len(slot_matches) == 1
                    else ""
                )
            else:
                expected = str(
                    (binding.get("files") or {}).get(role) or ""
                )
            if (
                (not role and binding.get("origin_kind") != "blender_cluster_bake")
                or not expected
            ):
                issues.append(_manifest_issue(
                    "isolated_texture_role_unmapped",
                    spm=str(production),
                    material=material_name,
                    material_id=material_id,
                    role=role or "unknown",
                    expected_output=(
                        expected
                        or str(
                            Path(plan["texture_root"])
                            / (
                                f"{binding.get('texture_base') or '<preserved>'}"
                                "_<role>.tga"
                            )
                        )
                    ),
                    map=map_match.group(1),
                    authored_ref=authored,
                ))
                return map_block
            relative = os.path.relpath(expected, isolated.parent).replace(
                "\\", "/"
            )
            rewritten.append({
                "material": material_name,
                "material_id": material_id,
                "map": html.unescape(map_match.group(1)),
                "role": role,
                "before": authored,
                "after": relative,
                "expected_output": expected,
            })
            return _TEX_FILENAME_RE.sub(
                lambda match: (
                    match.group(1)
                    + html.escape(relative, quote=False)
                    + match.group(3)
                ),
                map_block,
                count=1,
            )

        return _MAP_BLOCK_RE.sub(replace_map, block)

    after = _MATERIAL_BLOCK_RE.sub(replace_material, before)
    if issues:
        raise CanonicalTextureContractError(
            f"{production.name}: isolated canonical texture rebase is "
            f"incomplete. {PCG_ST9_REMEDIATION}",
            issues,
        )
    _write_spm_text(isolated, after, compressed)

    verification = inspect_spm_texture_slots(isolated)
    expected_by_material_role = {
        (
            str(row.get("material_id") or ""),
            role,
        ): path
        for row in plan.get("bindings") or []
        if row.get("origin_kind") != "blender_cluster_bake"
        for role, path in (row.get("files") or {}).items()
    }
    expected_bake_slots = {
        (
            str(row.get("material_id") or ""),
            int(slot.get("map_index", -1)),
            str(slot.get("map") or ""),
        ): str(slot.get("path") or "")
        for row in plan.get("bindings") or []
        if row.get("origin_kind") == "blender_cluster_bake"
        for slot in row.get("slot_files") or []
    }
    verification_issues = []
    for material in verification["materials"]:
        if str(material["material_id"]) not in by_id:
            continue
        binding = by_id[str(material["material_id"])]
        for slot in material["slots"]:
            if binding.get("origin_kind") == "blender_cluster_bake":
                expected = expected_bake_slots.get((
                    str(material["material_id"]),
                    int(slot["map_index"]),
                    str(slot["map"]),
                ))
            else:
                expected = expected_by_material_role.get(
                    (str(material["material_id"]), slot["role"])
                )
            resolved = _absolute_path(slot["authored_ref"], isolated.parent)
            forbidden = (
                _forbidden_derived_segment(slot["authored_ref"])
                or _forbidden_derived_segment(resolved)
            )
            if (
                not expected
                or forbidden
                or _path_key(resolved) != _path_key(expected)
            ):
                verification_issues.append(_manifest_issue(
                    "isolated_texture_rebase_verification_failed",
                    spm=str(production),
                    material=material["material_name"],
                    material_id=material["material_id"],
                    role=slot["role"] or "unknown",
                    expected_output=str(expected or ""),
                    authored_ref=slot["authored_ref"],
                    resolved_ref=str(resolved),
                    forbidden_segment=forbidden,
                ))
    if verification_issues:
        _write_spm_text(isolated, before, compressed)
        raise CanonicalTextureContractError(
            f"{production.name}: isolated canonical texture verification "
            f"failed. {PCG_ST9_REMEDIATION}",
            verification_issues,
        )
    return {
        "status": "rebased",
        "isolated_spm": str(isolated),
        "production_spm": str(production),
        "manifest": plan["manifest"],
        "rewritten_reference_count": len(rewritten),
        "references": rewritten,
    }


def _canonical_texture_base(spelling_counts):
    """Pick one canonical spelling among case-variant texture base names.

    Windows filesystems treat differently cased spellings as one managed set,
    so the spelling used by the most role files wins; a single stray file such
    as ``T_leaf_x_atlas_02_extra`` cannot rename or split the set.
    """
    return min(spelling_counts, key=lambda name: (-spelling_counts[name], name))


def _coerce_directories(texture_dirs):
    if texture_dirs is None:
        return []
    if isinstance(texture_dirs, (str, os.PathLike)):
        texture_dirs = [texture_dirs]
    by_key = {}
    for value in texture_dirs:
        directory = _absolute_path(value)
        by_key.setdefault(_path_key(directory), directory)
    return [by_key[key] for key in sorted(by_key)]


def index_texture_sets(texture_dirs):
    """Index managed texture files in one or more directories.

    The result maps each normalized set key to a sorted list of directory-local
    candidates.  Keeping candidates separate prevents same-named sets in two
    folders from being silently merged.  Base names that differ only by case
    are one set; ``texture_bases`` reports one canonical spelling per set.
    """
    extension_rank = {
        extension: rank for rank, extension in enumerate(TEXTURE_EXTENSIONS)
    }
    indexed = {}
    for directory in _coerce_directories(texture_dirs):
        try:
            files = sorted(
                (path for path in directory.iterdir() if path.is_file()),
                key=lambda path: (path.name.casefold(), path.name),
            )
        except OSError:
            continue
        local_rows = {}
        for path in files:
            parsed = parse_managed_texture_path(path)
            if parsed is None:
                continue
            set_key = parsed["set_key"]
            role = parsed["role"]
            row = local_rows.setdefault(
                set_key,
                {
                    "directory": str(directory),
                    "set_key": set_key,
                    "texture_bases": {},
                    "files": {},
                    "file_ranks": {},
                },
            )
            base = parsed["texture_base"]
            spelling_counts = row["texture_bases"].setdefault(base.casefold(), {})
            spelling_counts[base] = spelling_counts.get(base, 0) + 1
            rank = extension_rank[path.suffix.casefold()]
            previous = row["file_ranks"].get(role)
            candidate_rank = (rank, path.name.casefold(), path.name)
            if previous is None or candidate_rank < previous:
                row["files"][role] = str(_absolute_path(path))
                row["file_ranks"][role] = candidate_rank

        for set_key, mutable in local_rows.items():
            bases = sorted(
                (
                    _canonical_texture_base(spelling_counts)
                    for spelling_counts in mutable["texture_bases"].values()
                ),
                key=lambda item: (item.casefold(), item),
            )
            files_by_role = {
                role: mutable["files"][role]
                for role in REQUIRED_TEXTURE_ROLES
                if role in mutable["files"]
            }
            missing_roles = [
                role for role in REQUIRED_TEXTURE_ROLES if role not in files_by_role
            ]
            row = {
                "directory": mutable["directory"],
                "set_key": set_key,
                "texture_base": bases[0] if len(bases) == 1 else "",
                "texture_bases": bases,
                "files": files_by_role,
                "missing_roles": missing_roles,
                "ambiguous_bases": len(bases) != 1,
                "complete": len(bases) == 1 and not missing_roles,
            }
            indexed.setdefault(set_key, []).append(row)

    result = {}
    for set_key in sorted(indexed):
        result[set_key] = sorted(
            indexed[set_key],
            key=lambda row: (
                _path_key(row["directory"]),
                row["texture_base"].casefold(),
                row["texture_base"],
            ),
        )
    return result


def read_stmat_material_sources(stmat_path):
    """Read material/map ``Source`` rows from a SpeedTree 10.1 STMAT file."""
    stmat = _absolute_path(stmat_path)
    result = {
        "status": "missing_stmat",
        "stmat": str(stmat),
        "materials": [],
        "error": "",
    }
    if not stmat.is_file():
        return result
    try:
        root = ET.parse(stmat).getroot()
    except (OSError, ET.ParseError) as exc:
        result["status"] = "invalid_stmat"
        result["error"] = str(exc)
        return result

    materials = []
    for node in root.iter():
        if _local_name(node.tag).casefold() != "material":
            continue
        material_name = _attribute(node, "Name")
        sources = []
        map_index = -1
        for map_node in node.iter():
            if map_node is node or _local_name(map_node.tag).casefold() != "map":
                continue
            map_index += 1
            source = _attribute(map_node, "Source")
            if not source:
                continue
            resolved = _absolute_path(source, stmat.parent)
            sources.append(
                {
                    "map_index": map_index,
                    "map": _attribute(map_node, "Name"),
                    "source": source,
                    "resolved_source": str(resolved),
                }
            )
        materials.append(
            {
                "material": material_name,
                "material_key": normalize_material_key(material_name),
                "material_index": len(materials),
                "sources": sources,
            }
        )
    result["materials"] = materials
    result["status"] = "ok" if materials else "empty_stmat"
    return result


def _flatten_index(texture_index):
    return [
        row
        for set_key in sorted(texture_index)
        for row in texture_index[set_key]
    ]


def _referenced_sets(material):
    referenced = {}
    for source in material["sources"]:
        parsed = parse_managed_texture_path(source["resolved_source"])
        if parsed is None:
            continue
        row = referenced.setdefault(
            parsed["set_key"],
            {"roles": set(), "directories": {}, "texture_bases": set()},
        )
        row["roles"].add(parsed["role"])
        directory = str(Path(source["resolved_source"]).parent)
        directory_key = _path_key(directory)
        directory_row = row["directories"].setdefault(
            directory_key, {"directory": directory, "roles": set()}
        )
        directory_row["roles"].add(parsed["role"])
        row["texture_bases"].add(parsed["texture_base"])
    return referenced


def _required_roles(required_roles):
    values = REQUIRED_TEXTURE_ROLES if required_roles is None else required_roles
    roles = []
    seen = set()
    for value in values:
        role = str(value or "").strip().casefold()
        if role and role not in seen:
            seen.add(role)
            roles.append(role)
    return roles


def _directory_preference(texture_dirs):
    if texture_dirs is None:
        return {}
    if isinstance(texture_dirs, (str, os.PathLike)):
        texture_dirs = [texture_dirs]
    result = {}
    for value in texture_dirs:
        key = _path_key(value)
        if key not in result:
            result[key] = len(result)
    return result


def _texture_base_name(value):
    text = str(value or "").strip()
    if not text:
        return ""
    parsed = parse_managed_texture_path(text)
    return parsed["texture_base"] if parsed is not None else Path(text).name


def _resolve_indexed_texture_set(
    texture_index,
    texture_base,
    required_roles=None,
    directory_preference=None,
):
    roles = _required_roles(required_roles)
    requested_base = _texture_base_name(texture_base)
    set_key = normalize_texture_set_key(requested_base)
    empty = {
        "status": "invalid_texture_base",
        "set_key": set_key,
        "texture_base": requested_base,
        "texture_dir": "",
        "files": {},
        "missing_roles": list(roles),
    }
    # This API resolves a literal managed T_ base.  Refusing M_/material names
    # prevents Green/Yellow or other authoring labels from becoming implicit
    # texture/instance rules.
    if not requested_base.casefold().startswith("t_") or not set_key:
        return empty

    candidates = []
    for candidate in texture_index.get(set_key, []):
        exact_bases = [
            base for base in candidate["texture_bases"]
            if base.casefold() == requested_base.casefold()
        ]
        if not exact_bases:
            continue
        missing_roles = [
            role for role in roles if role not in candidate["files"]
        ]
        candidates.append((candidate, missing_roles))
    if not candidates:
        empty["status"] = "missing_texture_set"
        return empty

    preference = directory_preference or {}

    def candidate_rank(row):
        candidate, missing_roles = row
        directory_key = _path_key(candidate["directory"])
        return (
            len(missing_roles),
            candidate["ambiguous_bases"],
            preference.get(directory_key, len(preference)),
            directory_key,
            candidate["texture_base"].casefold(),
            candidate["texture_base"],
        )

    candidate, missing_roles = min(candidates, key=candidate_rank)
    complete = not missing_roles and not candidate["ambiguous_bases"]
    return {
        "status": "ok" if complete else "incomplete_texture_set",
        "set_key": set_key,
        # Prefer the on-disk canonical spelling so a differently cased STMAT
        # reference cannot leak into downstream asset naming.
        "texture_base": candidate["texture_base"] or requested_base,
        "texture_dir": candidate["directory"],
        "files": {
            role: candidate["files"][role]
            for role in roles
            if role in candidate["files"]
        },
        "missing_roles": list(missing_roles),
    }


def resolve_texture_set(texture_dirs, texture_base, required_roles=None):
    """Resolve one literal ``T_`` texture base across candidate directories.

    A directory containing every configured role always wins over an earlier
    partial directory.  The selected files and missing roles use the same
    deterministic lower-case role keys as :func:`resolve_texture_bindings`.
    Material names are intentionally rejected; callers must provide the actual
    managed texture base.
    """
    texture_index = index_texture_sets(texture_dirs)
    return _resolve_indexed_texture_set(
        texture_index,
        texture_base,
        required_roles,
        _directory_preference(texture_dirs),
    )


def _missing_binding(material, reason, set_keys, missing_roles):
    return {
        "material": material["material"],
        "material_key": material["material_key"],
        "material_index": material["material_index"],
        "status": reason,
        "set_key": "",
        "texture_base": "",
        "texture_dir": "",
        "stmat_roles": [],
        "files": {},
        "referenced_set_keys": list(set_keys),
        "missing_roles": list(missing_roles),
    }


def resolve_texture_bindings(stmat_path, texture_dirs=None):
    """Resolve every STMAT material to one complete managed texture set.

    Source paths supply the authoritative set keys.  ``texture_dirs`` may add
    alternate managed-output locations; a complete candidate is selected even
    if an earlier/directly referenced candidate is partial.  Bindings remain
    one row per STMAT material, so two slots sharing a set are never collapsed.
    """
    parsed_stmat = read_stmat_material_sources(stmat_path)
    result = {
        "status": parsed_stmat["status"],
        "stmat": parsed_stmat["stmat"],
        "required_roles": list(REQUIRED_TEXTURE_ROLES),
        "sets": [],
        "bindings": [],
        "missing": [],
    }
    if parsed_stmat.get("error"):
        result["error"] = parsed_stmat["error"]
    if parsed_stmat["status"] not in {"ok", "empty_stmat"}:
        return result

    source_directories = []
    for material in parsed_stmat["materials"]:
        for source in material["sources"]:
            if parse_managed_texture_path(source["resolved_source"]) is not None:
                source_directories.append(Path(source["resolved_source"]).parent)
    source_directories.extend(_coerce_directories(texture_dirs))
    texture_index = index_texture_sets(source_directories)
    result["sets"] = _flatten_index(texture_index)

    for material in parsed_stmat["materials"]:
        references = _referenced_sets(material)
        set_keys = sorted(references)
        if not set_keys:
            binding = _missing_binding(
                material,
                "not_managed",
                [],
                [],
            )
            result["bindings"].append(binding)
            continue

        complete_resolutions = []
        partial_resolutions = []
        for set_key in set_keys:
            reference = references[set_key]
            preferred_directories = [
                row["directory"] for row in reference["directories"].values()
            ] + source_directories
            seen_bases = set()
            for texture_base in sorted(
                reference["texture_bases"], key=lambda value: (value.casefold(), value)
            ):
                base_key = texture_base.casefold()
                if base_key in seen_bases:
                    continue
                seen_bases.add(base_key)
                resolution = _resolve_indexed_texture_set(
                    texture_index,
                    texture_base,
                    REQUIRED_TEXTURE_ROLES,
                    _directory_preference(preferred_directories),
                )
                if resolution["status"] == "ok":
                    complete_resolutions.append((set_key, resolution))
                else:
                    partial_resolutions.append(resolution)

        if len(complete_resolutions) == 1:
            set_key, resolution = complete_resolutions[0]
            reference = references[set_key]
            binding = {
                "material": material["material"],
                "material_key": material["material_key"],
                "material_index": material["material_index"],
                "status": "ok",
                "set_key": set_key,
                "texture_base": resolution["texture_base"],
                "texture_dir": resolution["texture_dir"],
                "stmat_roles": sorted(reference["roles"]),
                "files": dict(resolution["files"]),
                "referenced_set_keys": set_keys,
                "missing_roles": [],
            }
            result["bindings"].append(binding)
            continue

        if len(complete_resolutions) > 1:
            reason = "ambiguous_complete_sets"
            missing_roles = []
        else:
            reason = "incomplete_texture_set"
            if partial_resolutions:
                best_partial = min(
                    partial_resolutions,
                    key=lambda resolution: (
                        len(resolution["missing_roles"]),
                        _path_key(resolution["texture_dir"])
                        if resolution["texture_dir"] else "",
                    ),
                )
                missing_roles = list(best_partial["missing_roles"])
            else:
                missing_roles = list(REQUIRED_TEXTURE_ROLES)

        binding = _missing_binding(material, reason, set_keys, missing_roles)
        result["bindings"].append(binding)
        result["missing"].append(
            {
                "material": material["material"],
                "material_index": material["material_index"],
                "reason": reason,
                "set_keys": set_keys,
                "missing_roles": missing_roles,
            }
        )

    managed_count = sum(
        1 for binding in result["bindings"]
        if binding["status"] != "not_managed"
    )
    result["managed_material_count"] = managed_count
    result["status"] = (
        "empty_stmat"
        if parsed_stmat["status"] == "empty_stmat"
        else "not_applicable"
        if not managed_count
        else "incomplete"
        if result["missing"]
        else "ok"
    )
    return result


__all__ = [
    "CANONICAL_OUTPUT_MANIFEST_KIND",
    "CANONICAL_OUTPUT_MANIFEST_NAME",
    "CANONICAL_OUTPUT_MANIFEST_SCHEMA_VERSION",
    "CanonicalTextureContractError",
    "PCG_ST9_REMEDIATION",
    "REQUIRED_TEXTURE_ROLES",
    "TEXTURE_ORIGIN_BLENDER_CLUSTER_BAKE",
    "TEXTURE_ORIGIN_CANONICAL_T",
    "TEXTURE_ORIGIN_NEEDS_PCG_GENERATION",
    "build_spm_canonical_texture_plan",
    "canonical_output_manifest_candidates",
    "inspect_production_spm_texture_contract",
    "inspect_spm_texture_slots",
    "load_canonical_output_manifest",
    "normalize_material_key",
    "normalize_texture_set_key",
    "parse_managed_texture_path",
    "index_texture_sets",
    "read_stmat_material_sources",
    "rebase_spm_copy_to_canonical_outputs",
    "resolve_blender_cluster_bake_origin",
    "resolve_manifest_material_output",
    "resolve_texture_set",
    "resolve_texture_bindings",
    "texture_role_for_slot",
    "validate_blender_cluster_bake_override",
]
