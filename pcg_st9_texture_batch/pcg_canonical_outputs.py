"""Canonical PCG ST9/SBS texture-output manifest.

Source textures (for example TCom or Megascans files) are authoring inputs.
They may be recorded as producer provenance, but they are never canonical
SpeedTree output files.  A production material consumes only the verified
asset-local ``T_*`` files described by this manifest.
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime
from pathlib import Path


MANIFEST_NAME = "pcg_st9_canonical_outputs.json"
MANIFEST_KIND = "pcg_st9_canonical_output_manifest"
SCHEMA_VERSION = 1
ATLAS_CANONICAL_TEXTURE_STATUS = "canonical_pcg_output"
ATLAS_SOURCE_FALLBACK_STATUS = "source_fallback_needs_pcg_generation"
REQUIRED_ROLES = (
    "color",
    "opacity",
    "normal",
    "height",
    "extra",
    "subsurface",
)
GENERATED_DIR_NAME = "_pcgtex_generated"
FORBIDDEN_OUTPUT_DIR_NAMES = {
    "cache",
    "temp",
    "tmp",
    ".sk_batch_isolated_bark",
}


class CanonicalOutputManifestError(RuntimeError):
    """The asset-local PCG output contract is incomplete or unsafe."""


def canonical_texture_root(asset_root):
    """Return the only directory where new production outputs are written."""
    return Path(asset_root).resolve() / "texture"


def manifest_candidates(asset_root):
    """Return the canonical manifest followed by the legacy plural location."""
    root = Path(asset_root).resolve()
    return [
        root / "texture" / MANIFEST_NAME,
        root / "textures" / MANIFEST_NAME,
    ]


def _is_relative_to(path, root):
    try:
        Path(path).resolve().relative_to(Path(root).resolve())
        return True
    except (OSError, ValueError):
        return False


def _atomic_write_bytes(path, encoded):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        if path.read_bytes() == encoded:
            return False
    except OSError:
        pass
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(str(temporary), str(path))
    except Exception:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise
    return True


def _atomic_write_json(path, payload):
    encoded = (
        json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True)
        + "\n"
    ).encode("utf-8")
    return _atomic_write_bytes(path, encoded)


def _path_key(path):
    try:
        resolved = Path(path).expanduser().resolve(strict=False)
    except (OSError, RuntimeError):
        resolved = Path(path).expanduser().absolute()
    return os.path.normcase(str(resolved)).casefold()


def _atlas_manifest_candidates(target_spm):
    target = Path(target_spm).expanduser().resolve(strict=False)
    candidates = [target.parent / "speedtree_import_manifest.json"]
    scope_dir = target.parent / ".atlas_leaf_speedtree_scopes"
    if scope_dir.is_dir():
        candidates.extend(sorted(
            path
            for path in scope_dir.glob("*.json")
            if path.is_file()
        ))
    return candidates


def _canonical_atlas_output_row(
    canonical_manifest,
    output,
    target_spm,
    material_name,
    material_id,
):
    return {
        "kind": MANIFEST_KIND,
        "texture_contract_status": ATLAS_CANONICAL_TEXTURE_STATUS,
        "material_name": str(material_name or ""),
        "material_id": str(material_id or ""),
        "target_spm": str(
            Path(target_spm).expanduser().resolve(strict=False)
        ),
        "manifest": str(canonical_manifest["manifest"]),
        "asset_root": str(canonical_manifest["asset_root"]),
        "texture_root": str(canonical_manifest["texture_root"]),
        "texture_base": str(output["texture_base"]),
        "required_roles": list(output["required_roles"]),
        "files": {
            role: str(path)
            for role, path in sorted(output["files"].items())
        },
        "producer": dict(output.get("producer") or {}),
    }


def _promote_atlas_material_groups(groups, rows_by_identity):
    promoted = []
    for group in groups:
        if not isinstance(group, dict):
            promoted.append(group)
            continue
        identity = (
            str(group.get("material") or "").casefold(),
            str(group.get("material_id") or ""),
        )
        canonical = rows_by_identity.get(identity)
        if canonical is None:
            promoted.append(group)
            continue
        updated = dict(group)
        updated["texture_contract_status"] = (
            ATLAS_CANONICAL_TEXTURE_STATUS
        )
        updated["canonical_texture_output"] = canonical
        for field in (
            "source_texture_fallback",
            "blender_cluster_bake_texture",
            "texture_source_origin",
            "texture_origin_receipt",
            "texture_provisional_receipt",
            "texture_warning",
            "texture_remediation",
        ):
            updated.pop(field, None)
        promoted.append(updated)
    return promoted


def _promote_atlas_manifest_payload(
    payload,
    path,
    target_spm,
    canonical_manifest,
):
    """Return a canonical Atlas payload only when every group resolves."""
    from speedtree_texture_contract import resolve_manifest_material_output

    declared_spm = str(payload.get("spm") or "").strip()
    if (
        not declared_spm
        or _path_key(declared_spm) != _path_key(target_spm)
    ):
        return None, "different_target"
    groups = [
        group
        for group in payload.get("material_groups") or []
        if isinstance(group, dict)
    ]
    if not groups:
        return None, "no_material_groups"

    rows = []
    rows_by_identity = {}
    unresolved = []
    for group in groups:
        material_name = str(group.get("material") or "")
        material_id = str(group.get("material_id") or "")
        output = resolve_manifest_material_output(
            canonical_manifest,
            target_spm,
            material_id,
            material_name,
        )
        if output is None:
            unresolved.append({
                "material": material_name,
                "material_id": material_id,
            })
            continue
        row = _canonical_atlas_output_row(
            canonical_manifest,
            output,
            target_spm,
            material_name,
            material_id,
        )
        identity = (material_name.casefold(), material_id)
        rows_by_identity[identity] = row
        rows.append(row)
    if unresolved:
        return None, {
            "reason": "canonical_material_mapping_incomplete",
            "manifest": str(path),
            "materials": unresolved,
        }

    promoted = dict(payload)
    promoted["texture_contract_status"] = (
        ATLAS_CANONICAL_TEXTURE_STATUS
    )
    promoted["canonical_texture_outputs"] = rows
    promoted["source_texture_fallbacks"] = []
    promoted["blender_cluster_bake_textures"] = []
    promoted["source_texture_origins"] = {}
    promoted["material_groups"] = _promote_atlas_material_groups(
        payload.get("material_groups") or [],
        rows_by_identity,
    )
    if isinstance(payload.get("speedtree_material_groups"), list):
        promoted["speedtree_material_groups"] = (
            _promote_atlas_material_groups(
                payload["speedtree_material_groups"],
                rows_by_identity,
            )
        )

    unique_outputs = {
        (
            row["texture_base"].casefold(),
            tuple(
                (role, _path_key(value))
                for role, value in sorted(row["files"].items())
            ),
        ): row
        for row in rows
    }
    promoted["textures"] = (
        dict(next(iter(unique_outputs.values()))["files"])
        if len(unique_outputs) == 1
        else {}
    )
    notes = []
    for note in payload.get("notes") or []:
        text = str(note)
        lowered = text.casefold()
        if (
            "canonical t_* output is absent" in lowered
            or "pcg st9 texture" in lowered
            and "export" in lowered
        ):
            continue
        notes.append(note)
    if isinstance(payload.get("notes"), list):
        promoted["notes"] = notes
    return promoted, None


def refresh_atlas_manifests_for_spm(
    target_spm,
    manifest_path=None,
    *,
    require_complete=False,
):
    """Regenerate derived Atlas texture contracts from current PCG outputs.

    This is an exact producer-to-consumer synchronization.  It does not infer
    texture sets from material names and never writes a mixed provisional /
    canonical Atlas manifest.
    """
    from speedtree_texture_contract import (
        CanonicalTextureContractError,
        canonical_output_manifest_candidates,
        load_canonical_output_manifest,
    )

    target = Path(target_spm).expanduser().resolve(strict=False)
    if manifest_path is None:
        canonical_candidates = canonical_output_manifest_candidates(target)
        manifest_path = next(
            (path for path in canonical_candidates if path.is_file()),
            None,
        )
    else:
        manifest_path = Path(manifest_path).expanduser().resolve(
            strict=False
        )
    if manifest_path is None:
        return {
            "status": "not_applicable",
            "target_spm": str(target),
            "canonical_manifest": None,
            "updated": [],
            "current": [],
            "pending": [],
        }
    try:
        canonical = load_canonical_output_manifest(
            target,
            manifest_path,
        )
    except CanonicalTextureContractError as exc:
        raise CanonicalOutputManifestError(str(exc)) from exc

    updated = []
    current = []
    pending = []
    matched = []
    planned = []
    for path in _atlas_manifest_candidates(target):
        if not path.is_file():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise CanonicalOutputManifestError(
                f"Atlas manifest is unreadable: {path}: {exc}"
            ) from exc
        if not isinstance(payload, dict):
            raise CanonicalOutputManifestError(
                f"Atlas manifest root must be an object: {path}"
            )
        promoted, issue = _promote_atlas_manifest_payload(
            payload,
            path,
            target,
            canonical,
        )
        if issue == "different_target" or issue == "no_material_groups":
            continue
        matched.append(str(path))
        if promoted is None:
            pending.append(issue)
            continue
        planned.append((path, promoted))

    if require_complete and pending:
        details = "; ".join(
            (
                f"{Path(row['manifest']).name}: "
                + ", ".join(
                    f"{item['material']}#{item['material_id']}"
                    for item in row["materials"]
                )
            )
            for row in pending
        )
        raise CanonicalOutputManifestError(
            "Canonical PCG outputs cannot fully regenerate the Atlas "
            f"material contract for {target.name}: {details}"
        )
    if pending:
        planned = []
    originals = {}
    committed = []
    try:
        for path, promoted in planned:
            originals[path] = path.read_bytes()
            if _atomic_write_json(path, promoted):
                committed.append(path)
                updated.append(str(path))
            else:
                current.append(str(path))
    except Exception:
        for path in reversed(committed):
            try:
                _atomic_write_bytes(path, originals[path])
            except OSError:
                pass
        raise
    return {
        "status": (
            "updated"
            if updated
            else "pending"
            if pending
            else "current"
            if matched
            else "no_atlas_manifest"
        ),
        "target_spm": str(target),
        "canonical_manifest": str(manifest_path),
        "updated": updated,
        "current": current,
        "pending": pending,
    }


def refresh_atlas_manifests_from_canonical_outputs(
    asset_root,
    manifest_path,
):
    """Refresh every exact material target declared by one canonical manifest."""
    root = Path(asset_root).expanduser().resolve(strict=False)
    path = Path(manifest_path).expanduser().resolve(strict=False)
    payload = json.loads(path.read_text(encoding="utf-8"))
    targets = {}
    for output in payload.get("outputs") or []:
        for target in output.get("material_targets") or []:
            raw_spm = str(target.get("spm") or "").strip()
            if not raw_spm:
                continue
            spm = Path(raw_spm).expanduser()
            if not spm.is_absolute():
                spm = root / spm
            spm = spm.resolve(strict=False)
            targets.setdefault(_path_key(spm), spm)
    return [
        refresh_atlas_manifests_for_spm(
            target,
            path,
            require_complete=False,
        )
        for target in targets.values()
    ]


def _manifest_relative_path(path, texture_root, *, role, texture_base):
    path = Path(path).resolve()
    texture_root = Path(texture_root).resolve()
    if not _is_relative_to(path, texture_root):
        raise CanonicalOutputManifestError(
            f"{texture_base}/{role}: output is outside asset texture root: {path}"
        )
    relative = path.relative_to(texture_root)
    forbidden = next(
        (
            part
            for part in relative.parts[:-1]
            if (
                part.casefold() in FORBIDDEN_OUTPUT_DIR_NAMES
                or part.casefold().startswith(".sk_batch_")
            )
        ),
        "",
    )
    if forbidden:
        raise CanonicalOutputManifestError(
            f"{texture_base}/{role}: output uses derived cache directory "
            f"{forbidden}: {relative}"
        )
    if role in REQUIRED_ROLES:
        expected = f"{texture_base}_{role}.tga"
        if relative.name.casefold() != expected.casefold():
            raise CanonicalOutputManifestError(
                f"{texture_base}/{role}: expected asset-local {expected}, got {relative}"
            )
    elif role == "ao":
        expected_stem = f"{texture_base}_ao_from_height"
        if (
            relative.parent != Path(GENERATED_DIR_NAME)
            or relative.stem.casefold() != expected_stem.casefold()
        ):
            raise CanonicalOutputManifestError(
                f"{texture_base}/ao: expected "
                f"{GENERATED_DIR_NAME}/{expected_stem}.*, got {relative}"
            )
    else:
        raise CanonicalOutputManifestError(
            f"{texture_base}: unsupported output role: {role}"
        )
    if not path.is_file() or path.stat().st_size <= 0:
        raise CanonicalOutputManifestError(
            f"{texture_base}/{role}: canonical output is missing or empty: {path}"
        )
    return relative.as_posix()


def _material_targets(row, asset_root):
    result = []
    seen = set()
    raw_targets = list(row.get("material_targets") or [])
    if not raw_targets:
        spms = list(row.get("material_spms") or [])
        names = list(row.get("material_names") or [])
        for spm in spms:
            for name in names or [None]:
                raw_targets.append({
                    "spm": spm,
                    "material_id": None,
                    "material_name": name,
                })
    for target in raw_targets:
        spm = target.get("spm")
        if spm:
            resolved_spm = Path(spm).resolve()
            try:
                spm_value = resolved_spm.relative_to(asset_root).as_posix()
            except ValueError:
                spm_value = str(resolved_spm)
        else:
            spm_value = ""
        material_id = target.get("material_id")
        material_id = (
            str(material_id) if material_id not in {None, ""} else None
        )
        material_name = target.get("material_name")
        if not material_name:
            names = list(row.get("material_names") or [])
            material_name = names[0] if len(names) == 1 else None
        key = (spm_value.casefold(), material_id, str(material_name or "").casefold())
        if key in seen:
            continue
        seen.add(key)
        result.append({
            "spm": spm_value,
            "material_id": material_id,
            "material_name": material_name or None,
        })
    return sorted(
        result,
        key=lambda value: (
            value["spm"].casefold(),
            value["material_id"] or "",
            str(value["material_name"] or "").casefold(),
        ),
    )


def validate_manifest(payload, manifest_path, require_files=True):
    """Validate schema, role names, paths, and optional on-disk existence."""
    manifest_path = Path(manifest_path).resolve()
    texture_root = manifest_path.parent
    if not isinstance(payload, dict):
        raise CanonicalOutputManifestError("manifest root must be an object")
    if payload.get("kind") != MANIFEST_KIND:
        raise CanonicalOutputManifestError("unsupported canonical manifest kind")
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise CanonicalOutputManifestError("unsupported canonical manifest schema")
    recorded_root = Path(payload.get("texture_root") or "").resolve()
    if recorded_root != texture_root:
        raise CanonicalOutputManifestError(
            f"manifest texture_root mismatch: {recorded_root} != {texture_root}"
        )
    outputs = payload.get("outputs")
    if not isinstance(outputs, list):
        raise CanonicalOutputManifestError("manifest outputs must be a list")
    for output in outputs:
        if not isinstance(output, dict):
            raise CanonicalOutputManifestError("manifest output must be an object")
        texture_base = str(output.get("texture_base") or "")
        if not texture_base.casefold().startswith("t_"):
            raise CanonicalOutputManifestError(
                f"canonical texture_base must start with T_: {texture_base}"
            )
        required = tuple(output.get("required_roles") or ())
        if set(required) != set(REQUIRED_ROLES):
            raise CanonicalOutputManifestError(
                f"{texture_base}: required_roles must be the canonical six roles"
            )
        files = output.get("files")
        if not isinstance(files, dict):
            raise CanonicalOutputManifestError(
                f"{texture_base}: files must be an object"
            )
        missing = [role for role in REQUIRED_ROLES if role not in files]
        if missing:
            raise CanonicalOutputManifestError(
                f"{texture_base}: missing roles in manifest: {', '.join(missing)}"
            )
        for role, value in files.items():
            relative = Path(str(value))
            if relative.is_absolute() or ".." in relative.parts:
                raise CanonicalOutputManifestError(
                    f"{texture_base}/{role}: file path must be manifest-relative"
                )
            resolved = texture_root / relative
            if require_files:
                _manifest_relative_path(
                    resolved,
                    texture_root,
                    role=role,
                    texture_base=texture_base,
                )
    return payload


def record_canonical_output(
    row,
    output_files,
    *,
    producer_source,
    producer_tool="PCG ST9 Texture",
):
    """Verify one six-map set and atomically upsert its manifest entry."""
    asset_root_value = row.get("folder")
    if not asset_root_value:
        raise CanonicalOutputManifestError(
            "canonical output row has no asset folder"
        )
    asset_root = Path(asset_root_value).resolve()
    texture_root = canonical_texture_root(asset_root)
    texture_base = str(
        row.get("texture_base") or row.get("atlas_base") or ""
    )
    if not texture_base.casefold().startswith("t_"):
        raise CanonicalOutputManifestError(
            f"canonical texture_base must start with T_: {texture_base}"
        )
    resolved_outputs = [Path(path).resolve() for path in output_files]
    role_paths = {}
    for role in REQUIRED_ROLES:
        expected = f"{texture_base}_{role}.tga".casefold()
        matches = [
            path for path in resolved_outputs
            if path.name.casefold() == expected
        ]
        if len(matches) != 1:
            raise CanonicalOutputManifestError(
                f"{texture_base}/{role}: expected exactly one "
                f"{texture_base}_{role}.tga output, found {len(matches)}"
            )
        role_paths[role] = matches[0]
    if len(resolved_outputs) != len(REQUIRED_ROLES):
        raise CanonicalOutputManifestError(
            f"{texture_base}: expected {len(REQUIRED_ROLES)} output files"
        )
    files = {
        role: _manifest_relative_path(
            role_paths[role],
            texture_root,
            role=role,
            texture_base=texture_base,
        )
        for role in REQUIRED_ROLES
    }
    generated = texture_root / GENERATED_DIR_NAME
    ao_candidates = sorted(
        path for path in generated.glob(f"{texture_base}_ao_from_height.*")
        if path.is_file() and path.stat().st_size > 0
    )
    if len(ao_candidates) > 1:
        raise CanonicalOutputManifestError(
            f"{texture_base}: ambiguous generated AO outputs: "
            + ", ".join(str(path) for path in ao_candidates)
        )
    if ao_candidates:
        files["ao"] = _manifest_relative_path(
            ao_candidates[0],
            texture_root,
            role="ao",
            texture_base=texture_base,
        )

    manifest_path = texture_root / MANIFEST_NAME
    if manifest_path.is_file():
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        validate_manifest(payload, manifest_path, require_files=True)
    else:
        payload = {
            "kind": MANIFEST_KIND,
            "schema_version": SCHEMA_VERSION,
            "asset_root": str(asset_root),
            "texture_root": str(texture_root),
            "outputs": [],
        }
    if Path(payload.get("asset_root") or "").resolve() != asset_root:
        raise CanonicalOutputManifestError(
            "manifest asset_root does not match the current asset"
        )
    now = datetime.now().isoformat(timespec="seconds")
    entry = {
        "texture_base": texture_base,
        "required_roles": list(REQUIRED_ROLES),
        "files": files,
        "material_targets": _material_targets(row, asset_root),
        "producer": {
            "tool": producer_tool,
            "source": str(producer_source or ""),
        },
        "generated_at": now,
    }
    outputs = [
        output for output in payload.get("outputs") or []
        if str(output.get("texture_base") or "").casefold()
        != texture_base.casefold()
    ]
    outputs.append(entry)
    payload["outputs"] = sorted(
        outputs,
        key=lambda output: str(output.get("texture_base") or "").casefold(),
    )
    payload["generated_at"] = now
    _atomic_write_json(manifest_path, payload)
    validate_manifest(payload, manifest_path, require_files=True)
    refresh_atlas_manifests_from_canonical_outputs(
        asset_root,
        manifest_path,
    )
    return manifest_path
