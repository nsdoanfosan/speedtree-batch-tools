"""Read and write Atlas Leaf target-SPM sidecars without importing Blender."""

import json
import os
from datetime import datetime, timezone
from pathlib import Path


REGISTRY_KIND = "atlas_leaf_spm_targets"
REGISTRY_VERSION = 1
REGISTRY_SUFFIX = ".atlas_leaf_targets.json"


class TargetRegistryError(RuntimeError):
    pass


def registry_path_for_blend(blend_path):
    blend = Path(blend_path).expanduser().absolute()
    if blend.suffix.lower() != ".blend":
        raise TargetRegistryError("Atlas target registry requires a .blend path")
    return blend.with_suffix(REGISTRY_SUFFIX)


def _normalized_spm_paths(values):
    paths = []
    seen = set()
    for value in values or ():
        text = str(value or "").strip()
        if not text:
            continue
        path = Path(text).expanduser().absolute()
        if path.suffix.lower() != ".spm":
            raise TargetRegistryError(f"Target is not an SPM file: {path}")
        key = os.path.normcase(str(path)).casefold()
        if key in seen:
            continue
        seen.add(key)
        paths.append(path)
    return paths


def load_target_registry(blend_path):
    registry_path = registry_path_for_blend(blend_path)
    if not registry_path.is_file():
        return None
    try:
        payload = json.loads(registry_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise TargetRegistryError(
            f"Cannot read Atlas target registry {registry_path}: {exc}"
        ) from exc
    if not isinstance(payload, dict):
        raise TargetRegistryError(f"Atlas target registry must contain an object: {registry_path}")
    if payload.get("version") != REGISTRY_VERSION:
        raise TargetRegistryError(
            f"Unsupported Atlas target registry version in {registry_path}: "
            f"{payload.get('version')!r}"
        )
    if payload.get("kind") != REGISTRY_KIND:
        raise TargetRegistryError(f"Unexpected Atlas target registry kind: {registry_path}")
    if not isinstance(payload.get("target_spms"), list):
        raise TargetRegistryError(f"Atlas target registry target_spms must be a list: {registry_path}")
    payload = dict(payload)
    payload["registry_path"] = str(registry_path)
    payload["target_spms"] = [str(path) for path in _normalized_spm_paths(payload["target_spms"])]
    return payload


def save_target_registry(blend_path, target_spms):
    blend = Path(blend_path).expanduser().absolute()
    registry_path = registry_path_for_blend(blend)
    targets = _normalized_spm_paths(target_spms)
    payload = {
        "version": REGISTRY_VERSION,
        "kind": REGISTRY_KIND,
        "atlas_blend": str(blend),
        "target_spms": [str(path) for path in targets],
        "updated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = registry_path.with_name(registry_path.name + ".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, registry_path)
    payload["registry_path"] = str(registry_path)
    return payload
