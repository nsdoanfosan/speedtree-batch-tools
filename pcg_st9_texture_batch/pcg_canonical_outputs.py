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
REQUIRED_ROLES = (
    "color",
    "opacity",
    "normal",
    "height",
    "extra",
    "subsurface",
)
GENERATED_DIR_NAME = "_pcgtex_generated"


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


def _atomic_write_json(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (
        json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True)
        + "\n"
    ).encode("utf-8")
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


def _manifest_relative_path(path, texture_root, *, role, texture_base):
    path = Path(path).resolve()
    texture_root = Path(texture_root).resolve()
    if not _is_relative_to(path, texture_root):
        raise CanonicalOutputManifestError(
            f"{texture_base}/{role}: output is outside asset texture root: {path}"
        )
    relative = path.relative_to(texture_root)
    if role in REQUIRED_ROLES:
        expected = f"{texture_base}_{role}.tga"
        if relative.parent != Path(".") or relative.name.casefold() != expected.casefold():
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
    return manifest_path
