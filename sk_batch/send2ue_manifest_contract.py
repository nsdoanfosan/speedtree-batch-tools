"""Normalize Send2UE's exported manifest into an Unreal-ready handoff.

The Blender-side material sidecar is authored for the owning Export Empty.
Send2UE may export its child mesh to an ``*_Mesh.fbx`` file, but that internal
object/file suffix must not replace the public ``SK_*_01`` Empty name.  Unreal's
SpeedTree contract intentionally requires the sidecar identity and destination
asset path to agree.

This module resolves that boundary from the sidecar's authored unit identity
and the manifest's destination folder.  It does not infer a part count, a
suffix, or a vegetation type.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
from pathlib import Path


MESH_ASSET_TYPES = {"SkeletalMesh", "StaticMesh"}
MATERIAL_PIPELINE_JSON_PATH_KEY = "_material_pipeline_json_path"


def is_actionable_cluster_assembly_manifest(manifest):
    """A content-driven pass-through has no additive assets to orchestrate."""
    return bool(
        isinstance(manifest, dict)
        and manifest.get("status") != "pass_through"
    )


def strict_material_handoff_source_paths(envelope):
    """Return the exact SPM/FBX pair fingerprinted by a strict handoff."""
    source = envelope.get("source") if isinstance(envelope, dict) else None
    source = source if isinstance(source, dict) else {}
    spm_record = source.get("spm") or {}
    spm_path = str(spm_record.get("canonical_path") or "").strip()
    stmat_records = source.get("stmat") or []
    if not spm_path or len(stmat_records) != 1:
        raise ValueError(
            "strict material handoff must identify one SPM and one STMAT"
        )
    stmat_path = str(
        (stmat_records[0] or {}).get("canonical_path") or ""
    ).strip()
    if not stmat_path:
        raise ValueError(
            "strict material handoff STMAT identity has no canonical path"
        )
    resolved_spm = Path(spm_path).resolve()
    resolved_stmat = Path(stmat_path).resolve()
    expected_stmat = (
        resolved_spm.parent
        / "fbx"
        / f"{resolved_spm.stem}.stmat"
    ).resolve()
    if os.path.normcase(str(resolved_stmat)).casefold() != os.path.normcase(
        str(expected_stmat)
    ).casefold():
        raise ValueError(
            "strict material handoff SPM and STMAT belong to different "
            "export bundles"
        )
    return {
        "spm": str(resolved_spm),
        "fbx": str(resolved_stmat.with_suffix(".fbx")),
        "stmat": str(resolved_stmat),
    }


def validate_material_handoff_wrapper(payload, expected_canonical_spm):
    """Bind a generated Push wrapper to its requested canonical SPM."""
    if not isinstance(payload, dict):
        raise ValueError("material handoff wrapper is not an object")
    source = strict_material_handoff_source_paths(
        payload.get("speedtree_pipeline_contract")
    )

    def path_key(value):
        return os.path.normcase(
            os.path.abspath(str(value or ""))
        ).casefold()

    if (
        not payload.get("canonical_spm")
        or path_key(payload["canonical_spm"])
        != path_key(expected_canonical_spm)
    ):
        raise ValueError(
            "material handoff wrapper belongs to a different canonical SPM"
        )
    if (
        not payload.get("material_source_spm")
        or path_key(payload["material_source_spm"])
        != path_key(source["spm"])
    ):
        raise ValueError(
            "material handoff wrapper source does not match its "
            "content-addressed envelope"
        )
    return source


def _asset_data(manifest_asset):
    value = manifest_asset.get("asset_data") if isinstance(manifest_asset, dict) else None
    return value if isinstance(value, dict) else {}


def _canonical_asset_path(value):
    return str(value or "").split(".", 1)[0].rstrip("/")


def _mesh_manifest_assets(manifest_assets):
    for manifest_asset in manifest_assets or []:
        asset_data = _asset_data(manifest_asset)
        if asset_data.get("_asset_type") not in MESH_ASSET_TYPES:
            continue
        asset_path = _canonical_asset_path(asset_data.get("asset_path"))
        if not asset_path or "/" not in asset_path:
            continue
        yield manifest_asset, asset_data, asset_path


def primary_mesh_asset_path(manifest_assets, preferred_path=""):
    """Return the real primary mesh path recorded by Send2UE.

    A pre-export Empty name is only a preference.  If Send2UE did not emit an
    asset at that path, the first emitted mesh asset is the authoritative
    primary.  Manifest order is stable and comes directly from Send2UE.
    """

    candidates = [
        asset_path
        for _manifest_asset, _asset_data_value, asset_path
        in _mesh_manifest_assets(manifest_assets)
    ]
    if not candidates:
        raise RuntimeError("Send2UE manifest contains no mesh asset path")
    preferred = _canonical_asset_path(preferred_path).casefold()
    if preferred:
        for candidate in candidates:
            if candidate.casefold() == preferred:
                return candidate
    return candidates[0]


def manifest_checkout_asset_paths(manifest_assets):
    """Return every imported mesh and generated skeletal companion path."""

    paths = []
    seen = set()

    def append(value):
        key = str(value or "").casefold()
        if value and key not in seen:
            seen.add(key)
            paths.append(value)

    for _manifest_asset, asset_data, asset_path in _mesh_manifest_assets(
        manifest_assets
    ):
        append(asset_path)
        if asset_data.get("_asset_type") == "SkeletalMesh":
            append(asset_path + "_Skeleton")
            append(asset_path + "_PhysicsAsset")
    return paths


def _replace_sidecar_path(line, source_path, target_path):
    value = str(line)
    source_variants = {
        str(source_path),
        str(source_path).replace("\\", "/"),
        str(source_path).replace("/", "\\"),
        str(source_path).replace("\\", "\\\\"),
        str(source_path).replace("/", "\\\\"),
    }
    target = str(target_path).replace("\\", "/")
    for source in source_variants:
        value = value.replace(source, target)
    return value


def _normalize_command_groups(
    manifest_asset,
    *,
    source_asset_path,
    target_asset_path,
    source_sidecar,
    normalized_sidecar,
):
    for group_key in ("pre_import_commands", "post_import_commands"):
        groups = manifest_asset.get(group_key)
        if not isinstance(groups, list):
            continue
        normalized_groups = []
        for commands in groups:
            if not isinstance(commands, list):
                normalized_groups.append(commands)
                continue
            normalized_commands = []
            for line in commands:
                value = _replace_sidecar_path(
                    line,
                    source_sidecar,
                    normalized_sidecar,
                )
                value = value.replace(
                    str(source_asset_path),
                    str(target_asset_path),
                )
                if value.lstrip().startswith("_asset_path ="):
                    indent = value[: len(value) - len(value.lstrip())]
                    value = (
                        f'{indent}_asset_path = r"{target_asset_path}"'
                        '.split(".")[0]'
                    )
                normalized_commands.append(value)
            normalized_groups.append(normalized_commands)
        manifest_asset[group_key] = normalized_groups


def _normalized_sidecar_path(export_root, source_path, asset_path):
    identity = "\0".join(
        (
            os.path.normcase(str(Path(source_path).resolve())).casefold(),
            asset_path.casefold(),
        )
    )
    digest = hashlib.blake2b(
        identity.encode("utf-8"),
        digest_size=8,
    ).hexdigest()
    asset_name = asset_path.rsplit("/", 1)[-1]
    return Path(export_root) / "handoff" / f"{asset_name}_{digest}.json"


def _normalize_exported_mesh_file(asset_data, export_root, authored_name):
    """Give Unreal a public-name FBX without renaming the Blender child mesh.

    Unreal's FBX importer derives the created asset name from the FBX filename,
    not from ``asset_data.asset_path``.  Send2UE legitimately exports the child
    mesh as ``*_Mesh.fbx`` while the owning Export Empty is the public asset
    identity.  Materialize a cache-local public-name copy and import that copy.
    """

    source_value = asset_data.get("file_path")
    if not source_value:
        return None
    source_path = Path(source_value).resolve()
    if not source_path.is_file():
        raise RuntimeError(
            "Send2UE exported mesh file is missing: " + str(source_path)
        )
    export_root = Path(export_root).resolve()
    try:
        source_path.relative_to(export_root)
    except ValueError as exc:
        raise RuntimeError(
            "Send2UE exported mesh file is outside the export cache: "
            + str(source_path)
        ) from exc

    normalized_path = source_path.with_name(
        authored_name + source_path.suffix
    )
    if normalized_path != source_path:
        temporary = normalized_path.with_name(
            f".{normalized_path.name}.{os.getpid()}.tmp"
        )
        shutil.copy2(source_path, temporary)
        os.replace(temporary, normalized_path)
    asset_data["file_path"] = str(normalized_path)
    return {
        "source_file": str(source_path),
        "normalized_file": str(normalized_path),
        "file_path_changed": normalized_path != source_path,
    }


def normalize_manifest_handoff_sidecars(
    manifest_assets,
    export_root,
    *,
    sidecar_descriptor_builder,
):
    """Align each manifest destination with its authored Export Empty identity.

    The child mesh object may retain ``_Mesh``.  Public Unreal asset paths and
    the cache-local FBX filename are taken from the sidecar ``mesh_name`` so
    Unreal creates the authored Export Empty identity.  A sidecar copy is
    created only when its own descriptor is missing or incomplete; source
    sidecars remain untouched.
    """

    results = []
    claimed_asset_paths = {}
    for manifest_asset, asset_data, asset_path in _mesh_manifest_assets(
        manifest_assets
    ):
        source_value = asset_data.get(MATERIAL_PIPELINE_JSON_PATH_KEY)
        if not source_value:
            continue
        source_path = Path(source_value)
        if not source_path.is_file():
            raise RuntimeError(
                "Send2UE material sidecar is missing: " + str(source_path)
            )
        try:
            source_data = json.loads(source_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise RuntimeError(
                f"Send2UE material sidecar could not be read: "
                f"{source_path} ({exc})"
            ) from exc
        if not isinstance(source_data, dict):
            raise RuntimeError(
                "Send2UE material sidecar is not an object: "
                + str(source_path)
            )

        expected_name = asset_path.rsplit("/", 1)[-1]
        saved_name = str(source_data.get("mesh_name") or "").strip()
        descriptor = source_data.get("speedtree_handoff_contract")
        descriptor_name = (
            str(descriptor.get("mesh_name") or "").strip()
            if isinstance(descriptor, dict)
            else ""
        )
        if saved_name and descriptor_name and (
            saved_name.casefold() != descriptor_name.casefold()
        ):
            raise RuntimeError(
                "Send2UE material sidecar identity is inconsistent: "
                f"{saved_name!r} != {descriptor_name!r} ({source_path})"
            )
        authored_name = saved_name or descriptor_name or expected_name
        destination_folder = asset_path.rsplit("/", 1)[0]
        target_asset_path = destination_folder + "/" + authored_name
        target_key = target_asset_path.casefold()
        previous_source = claimed_asset_paths.get(target_key)
        if previous_source is not None and previous_source != str(
            source_path.resolve()
        ).casefold():
            raise RuntimeError(
                "Send2UE authored Export Empty identities collide: "
                + target_asset_path
            )
        claimed_asset_paths[target_key] = str(source_path.resolve()).casefold()

        needs_identity_normalization = (
            not saved_name
            or (
                isinstance(descriptor, dict)
                and not descriptor_name
            )
        )

        normalized_path = source_path
        if needs_identity_normalization:
            normalized_data = copy.deepcopy(source_data)
            normalized_data["mesh_name"] = authored_name
            if isinstance(descriptor, dict):
                normalized_data["speedtree_handoff_contract"] = (
                    sidecar_descriptor_builder(
                        authored_name,
                        source=descriptor.get("source"),
                    )
                )
            normalized_path = _normalized_sidecar_path(
                export_root,
                source_path,
                target_asset_path,
            )
            normalized_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = normalized_path.with_name(
                f".{normalized_path.name}.{os.getpid()}.tmp"
            )
            temporary.write_text(
                json.dumps(
                    normalized_data,
                    indent=2,
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            os.replace(temporary, normalized_path)

        asset_data["asset_path"] = target_asset_path
        exported_mesh = _normalize_exported_mesh_file(
            asset_data,
            export_root,
            authored_name,
        )
        asset_data[MATERIAL_PIPELINE_JSON_PATH_KEY] = str(
            normalized_path.resolve()
        ).replace("\\", "/")
        _normalize_command_groups(
            manifest_asset,
            source_asset_path=asset_path,
            target_asset_path=target_asset_path,
            source_sidecar=source_path,
            normalized_sidecar=normalized_path,
        )
        results.append(
            {
                "manifest_asset_path_before": asset_path,
                "asset_path": target_asset_path,
                "source_sidecar": str(source_path.resolve()),
                "normalized_sidecar": str(normalized_path.resolve()),
                "identity_changed": needs_identity_normalization,
                "asset_path_changed": (
                    asset_path.casefold() != target_asset_path.casefold()
                ),
                **(exported_mesh or {}),
            }
        )
    return results


__all__ = (
    "manifest_checkout_asset_paths",
    "normalize_manifest_handoff_sidecars",
    "primary_mesh_asset_path",
)
