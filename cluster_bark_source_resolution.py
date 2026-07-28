"""Resolve immutable canonical-bark SPM copies for Cluster source builds.

Cluster Assembly receipts can require a provider's rendered bark slots to use
the owner Tree's canonical bark before Blender/Atlas captures the provider.
Production SPMs remain untouched: this module creates one content-addressed
isolated Tree layout and returns the normalized provider copy to SK Batch.
"""
from __future__ import annotations

import copy
import functools
import gzip
import hashlib
import json
import os
import re
import shutil
import tempfile
import threading
import time
from pathlib import Path

from pcg_st9_texture_batch.pcg_cluster_assembly_contract import (
    DEFAULT_RECEIPT_DIR,
    ClusterAssemblyReceiptError,
    ClusterAssemblyReceiptStaleError,
    load_cluster_assembly_receipt,
    locate_cluster_assembly_receipt,
    normalize_export_name,
)
from pcg_st9_texture_batch.pcg_cluster_bark_normalization import (
    BarkNormalizationError,
    apply_isolated_bark_normalization,
    build_isolated_bark_normalization_plan,
)
from pcg_st9_texture_batch.pcg_texture_audit import (
    extract_material_image_refs,
)
from sk_batch.spm_leaf_handoff_contract import (
    inspect_spm_mesh_file_references,
)


class ClusterBarkSourceResolutionError(RuntimeError):
    """A required isolated bark source is missing, stale, or ambiguous."""


_BARK_CACHE_PREPARE_LOCK = threading.RLock()


def _serialized_bark_cache_prepare(function):
    @functools.wraps(function)
    def wrapper(*args, **kwargs):
        with _BARK_CACHE_PREPARE_LOCK:
            return function(*args, **kwargs)

    return wrapper


def _publish_cache_directory(staging, cache_dir):
    """Publish a prepared directory despite transient OneDrive scan locks."""
    for attempt in range(8):
        try:
            os.replace(staging, cache_dir)
            return
        except PermissionError as exc:
            if (
                getattr(exc, "winerror", None) != 5
                or attempt == 7
                or Path(cache_dir).exists()
            ):
                raise
            time.sleep(0.25 * (attempt + 1))


def _remove_incomplete_cache_directory(cache_dir):
    """Remove only a derived incomplete cache, retrying transient scan locks."""
    cache_dir = Path(cache_dir)
    for attempt in range(8):
        try:
            shutil.rmtree(cache_dir)
            return
        except FileNotFoundError:
            return
        except PermissionError as exc:
            if (
                getattr(exc, "winerror", None) != 5
                or attempt == 7
            ):
                raise
            time.sleep(0.25 * (attempt + 1))


def _path_key(path):
    return os.path.normcase(os.path.abspath(str(path))).casefold()


def _sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_json(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _resolve_ref(spm, value):
    text = str(value or "").strip().replace("\\", os.sep).replace("/", os.sep)
    path = Path(text)
    if not path.is_absolute():
        path = Path(spm).parent / path
    return Path(os.path.abspath(os.path.normpath(str(path))))


def _atomic_write_json(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    try:
        temporary.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _dependency_for_source(contract, source_spm):
    wanted = _path_key(source_spm)
    for dependency in contract.get("dependencies") or []:
        values = [
            dependency.get(field)
            for field in (
                "spm",
                "source_spm",
                "authoring_spm",
                "output_spm",
            )
            if dependency.get(field)
        ]
        if any(_path_key(value) == wanted for value in values):
            return dependency
    return None


def _fingerprint_is_current(record, expected_path):
    if not isinstance(record, dict):
        return False
    path = Path(str(record.get("path") or ""))
    expected = Path(expected_path)
    return (
        path.is_file()
        and _path_key(path) == _path_key(expected)
        and bool(record.get("sha256"))
        and str(record["sha256"]).casefold()
        == _sha256_file(path).casefold()
    )


def _stale_receipt_bark_contract(target_spm, source_spm):
    """Recover only current source facts from a stale derivative receipt.

    Normalizer/Atlas FBX or blend outputs can become stale precisely because
    SK Batch is rebuilding this provider.  That derivative staleness must not
    hide a still-current Tree/provider/canonical-texture normalization input.
    """
    target = Path(target_spm).resolve()
    source = Path(source_spm).resolve()
    candidates = []
    for receipt in DEFAULT_RECEIPT_DIR.glob("*.json"):
        try:
            payload = json.loads(receipt.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        contract = payload.get("cluster_assembly") or {}
        dependency = _dependency_for_source(contract, source)
        if dependency is None:
            continue
        target_rows = [
            row.get("target_spm") or {}
            for row in contract.get("tree_source_identities") or []
        ]
        if not any(
            _fingerprint_is_current(row, target)
            for row in target_rows
        ):
            continue
        source_records = [
            dependency.get(field)
            for field in (
                "spm_fingerprint",
                "authoring_spm_fingerprint",
                "output_spm_fingerprint",
            )
            if isinstance(dependency.get(field), dict)
            and _path_key(dependency[field].get("path") or "")
            == _path_key(source)
        ]
        if not any(
            _fingerprint_is_current(row, source)
            for row in source_records
        ):
            continue
        required_rows = _required_bark_rows(contract, dependency)
        if not required_rows:
            continue
        # This also verifies the current canonical source SPM and every
        # canonical texture before the candidate can be selected.
        try:
            signature, _identity = _normalization_identity(
                contract, source, required_rows
            )
        except ClusterBarkSourceResolutionError:
            continue
        candidates.append({
            "mtime_ns": receipt.stat().st_mtime_ns,
            "receipt": receipt,
            "contract": contract,
            "required_rows": required_rows,
            "signature": signature,
        })
    if not candidates:
        return None
    candidates.sort(key=lambda row: row["mtime_ns"], reverse=True)
    selected = candidates[0]
    return {
        "receipt": selected["receipt"],
        "contract": selected["contract"],
        "required_rows": selected["required_rows"],
        "signature": selected["signature"],
        "policy": "stale_derivative_receipt_current_source_slice",
    }


def _required_bark_rows(contract, dependency):
    bark = ((contract.get("handoff") or {}).get("canonical_bark") or {})
    provider_paths = {
        _path_key(dependency.get(field))
        for field in (
            "spm",
            "source_spm",
            "authoring_spm",
            "output_spm",
        )
        if dependency.get(field)
    }
    return [
        copy.deepcopy(row)
        for row in bark.get("cluster_bark_sources") or []
        if row.get("replacement") == "required"
        and row.get("cluster_spm")
        and _path_key(row["cluster_spm"]) in provider_paths
    ]


def _source_tree_texture_inventory(source_spm):
    source = Path(source_spm).resolve()
    tree_root = source.parent.parent.resolve()
    rows = []
    seen = set()
    for material in extract_material_image_refs(source):
        for value in material.get("refs") or []:
            path = _resolve_ref(source, value)
            try:
                relative = path.resolve().relative_to(tree_root)
            except ValueError:
                # References outside the Tree stay authored exactly as-is.
                # Required non-canonical bark rows are replaced by the
                # normalization plan before export.
                continue
            key = _path_key(path)
            if key in seen:
                continue
            seen.add(key)
            if not path.is_file():
                raise ClusterBarkSourceResolutionError(
                    f"Cluster source Tree texture is missing: {path}"
                )
            rows.append({
                "source": str(path),
                "relative": relative.as_posix(),
                "sha256": _sha256_file(path),
            })
    return sorted(rows, key=lambda row: row["relative"].casefold())


def _source_external_mesh_inventory(source_spm):
    source = Path(source_spm).resolve()
    contract = inspect_spm_mesh_file_references(source)
    if contract.get("status") == "inspection_error":
        raise ClusterBarkSourceResolutionError(
            "Cluster source external mesh inspection failed: "
            + str(contract.get("error") or source)
        )
    missing = list(contract.get("missing") or [])
    if missing:
        raise ClusterBarkSourceResolutionError(
            "Cluster source external mesh is missing: "
            + ", ".join(
                str(row.get("resolved_path") or row.get("filename") or "?")
                for row in missing[:8]
            )
        )
    rows = []
    seen = set()
    for reference in contract.get("references") or []:
        resolved = Path(str(reference.get("resolved_path") or "")).resolve()
        key = _path_key(resolved)
        if key in seen:
            continue
        seen.add(key)
        row = {
            "source": str(resolved),
            "sha256": _sha256_file(resolved),
        }
        filename = str(reference.get("filename") or "")
        if filename and not os.path.isabs(filename):
            row["relative_to_spm"] = Path(filename).as_posix()
        else:
            row["absolute_reference"] = True
        rows.append(row)
    return sorted(rows, key=lambda row: row["source"].casefold())


def _source_external_texture_inventory(source_spm):
    source = Path(source_spm).resolve()
    tree_root = source.parent.parent.resolve()
    rows = []
    seen = set()
    for material in extract_material_image_refs(source):
        for value in material.get("refs") or []:
            path = _resolve_ref(source, value)
            try:
                path.resolve().relative_to(tree_root)
                continue
            except ValueError:
                pass
            if not path.is_file():
                raise ClusterBarkSourceResolutionError(
                    "Cluster source external texture is missing: "
                    + str(path)
                )
            key = _path_key(path)
            if key in seen:
                continue
            seen.add(key)
            rows.append({
                "source": str(path),
                "sha256": _sha256_file(path),
            })
    return sorted(rows, key=lambda row: row["source"].casefold())


def _normalization_identity(contract, source_spm, required_rows):
    bark = ((contract.get("handoff") or {}).get("canonical_bark") or {})
    canonical_sources = list(bark.get("canonical_sources") or [])
    if not canonical_sources:
        raise ClusterBarkSourceResolutionError(
            "Cluster bark normalization has no canonical Tree bark source"
        )
    textures = []
    for row in canonical_sources:
        canonical_spm = Path(str(row.get("spm") or ""))
        if not canonical_spm.is_file():
            raise ClusterBarkSourceResolutionError(
                f"canonical bark SPM is missing: {canonical_spm}"
            )
        for value in row.get("refs") or []:
            path = _resolve_ref(canonical_spm, value)
            if not path.is_file():
                raise ClusterBarkSourceResolutionError(
                    f"canonical bark texture is missing: {path}"
                )
            textures.append({
                "name": path.name.casefold(),
                "sha256": _sha256_file(path),
            })
    identity = {
        "source_spm_sha256": _sha256_file(source_spm),
        "source_material_name_preserved": True,
        "source_tree_textures": _source_tree_texture_inventory(
            source_spm
        ),
        "canonical_material": normalize_export_name(
            bark.get("canonical_material")
        ),
        "canonical_textures": sorted(
            textures,
            key=lambda row: (row["name"], row["sha256"]),
        ),
        "required_materials": sorted(
            [
                {
                    "material_id": str(row.get("material_id") or ""),
                    "material_name": str(
                        row.get("material_name") or ""
                    ).casefold(),
                }
                for row in required_rows
            ],
            key=lambda row: (row["material_id"], row["material_name"]),
        ),
    }
    external_meshes = _source_external_mesh_inventory(source_spm)
    if external_meshes:
        # Keep existing cache signatures stable for providers with no external
        # mesh assets, while making every referenced FBX part of the immutable
        # isolated-source identity when those assets do exist.
        identity["source_external_meshes"] = external_meshes
    external_textures = _source_external_texture_inventory(source_spm)
    if external_textures:
        identity["source_external_textures"] = external_textures
    signature = hashlib.sha256(
        _canonical_json(identity).encode("utf-8")
    ).hexdigest()
    return signature, identity


def _copy_canonical_textures(contract, isolated_tree_root):
    bark = ((contract.get("handoff") or {}).get("canonical_bark") or {})
    copied = []
    seen = set()
    for row in bark.get("canonical_sources") or []:
        canonical_spm = Path(str(row.get("spm") or "")).resolve()
        tree_root = canonical_spm.parent
        for value in row.get("refs") or []:
            source = _resolve_ref(canonical_spm, value)
            try:
                relative = source.resolve().relative_to(tree_root)
            except ValueError as exc:
                raise ClusterBarkSourceResolutionError(
                    "canonical bark texture escapes its Tree folder: "
                    + str(source)
                ) from exc
            destination = Path(isolated_tree_root) / relative
            key = _path_key(destination)
            if key in seen:
                continue
            seen.add(key)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
            copied.append({
                "source": str(source),
                "isolated": str(destination),
                "sha256": _sha256_file(destination),
            })
    return copied


def _copy_source_tree_textures(identity, isolated_tree_root):
    copied = []
    for row in identity.get("source_tree_textures") or []:
        source = Path(row["source"])
        destination = Path(isolated_tree_root) / Path(row["relative"])
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        actual_hash = _sha256_file(destination)
        if actual_hash != row["sha256"]:
            raise ClusterBarkSourceResolutionError(
                "isolated source texture hash mismatch: "
                + str(destination)
            )
        copied.append({
            "source": str(source),
            "isolated": str(destination),
            "sha256": actual_hash,
        })
    return copied


def _copy_source_external_meshes(identity, staged_source, staging_root):
    copied = []
    seen = set()
    staging_root = Path(staging_root).resolve()
    staged_source = Path(staged_source).resolve()
    for row in identity.get("source_external_meshes") or []:
        relative = row.get("relative_to_spm")
        if not relative:
            # Absolute references remain valid without being rewritten.
            continue
        source = Path(row["source"])
        destination = (staged_source.parent / Path(relative)).resolve()
        try:
            destination.relative_to(staging_root)
        except ValueError as exc:
            raise ClusterBarkSourceResolutionError(
                "relative external mesh escapes the isolated workspace: "
                + str(relative)
            ) from exc
        key = _path_key(destination)
        if key in seen:
            continue
        seen.add(key)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        actual_hash = _sha256_file(destination)
        if actual_hash != row["sha256"]:
            raise ClusterBarkSourceResolutionError(
                "isolated source external mesh hash mismatch: "
                + str(destination)
            )
        copied.append({
            "source": str(source),
            "isolated": str(destination),
            "sha256": actual_hash,
            "relative_to_spm": relative,
        })
    return copied


def _copy_source_external_textures(
    identity,
    staged_source,
    isolated_tree_root,
):
    copied = []
    staged_source = Path(staged_source)
    for row in identity.get("source_external_textures") or []:
        source = Path(row["source"])
        destination = (
            Path(isolated_tree_root)
            / "_external_textures"
            / str(row["sha256"])[:16]
            / source.name
        )
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        actual_hash = _sha256_file(destination)
        if actual_hash != row["sha256"]:
            raise ClusterBarkSourceResolutionError(
                "isolated source external texture hash mismatch: "
                + str(destination)
            )
        spm_ref = os.path.relpath(destination, staged_source.parent).replace(
            "\\", "/"
        )
        copied.append({
            "source": str(source),
            "isolated": str(destination),
            "sha256": actual_hash,
            "spm_ref": spm_ref,
        })
    return copied


_TEX_FILENAME_RE = re.compile(
    r"(<TexFilename\b(?![^>]*?/\s*>)[^>]*>)(.*?)"
    r"(</TexFilename>|<\\TexFilename>)",
    re.IGNORECASE | re.DOTALL,
)


def _rebase_external_texture_refs(
    isolated_spm,
    production_spm,
    copied_external_textures,
):
    if not copied_external_textures:
        return {
            "status": "not_required",
            "rewritten_reference_count": 0,
            "textures": [],
        }
    isolated_spm = Path(isolated_spm)
    before_bytes = isolated_spm.read_bytes()
    compressed = before_bytes.startswith(b"\x1f\x8b")
    before = (
        gzip.decompress(before_bytes).decode("utf-8")
        if compressed
        else before_bytes.decode("utf-8")
    )
    by_source = {
        _path_key(row["source"]): row
        for row in copied_external_textures
    }
    rewritten = []

    def replace(match):
        authored = " ".join(match.group(2).split())
        row = by_source.get(_path_key(_resolve_ref(production_spm, authored)))
        if row is None:
            return match.group(0)
        rewritten.append(row)
        return match.group(1) + row["spm_ref"] + match.group(3)

    after = _TEX_FILENAME_RE.sub(replace, before)
    if not rewritten:
        return {
            "status": "not_required",
            "rewritten_reference_count": 0,
            "textures": copied_external_textures,
        }
    payload = after.encode("utf-8")
    if compressed:
        payload = gzip.compress(payload)
    temporary = isolated_spm.with_name(
        isolated_spm.name + ".external-textures.tmp"
    )
    try:
        temporary.write_bytes(payload)
        os.replace(temporary, isolated_spm)
    finally:
        temporary.unlink(missing_ok=True)
    for row in rewritten:
        destination = _resolve_ref(isolated_spm, row["spm_ref"])
        if (
            not destination.is_file()
            or _sha256_file(destination) != row["sha256"]
        ):
            raise ClusterBarkSourceResolutionError(
                "rebased isolated external texture is missing or stale: "
                + str(destination)
            )
    return {
        "status": "rebased",
        "rewritten_reference_count": len(rewritten),
        "textures": copied_external_textures,
    }


def _copied_manifest_artifacts_current(manifest):
    for key in (
        "copied_source_tree_textures",
        "copied_canonical_textures",
        "copied_source_external_meshes",
        "copied_source_external_textures",
    ):
        for row in manifest.get(key) or []:
            path = Path(str(row.get("isolated") or ""))
            if (
                not path.is_file()
                or not row.get("sha256")
                or _sha256_file(path) != str(row["sha256"])
            ):
                return False
    return True


def _rebase_paths(value, old_root, new_root):
    if isinstance(value, dict):
        return {
            key: _rebase_paths(item, old_root, new_root)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [
            _rebase_paths(item, old_root, new_root) for item in value
        ]
    if isinstance(value, str):
        old = str(old_root)
        if value == old or value.startswith(old + os.sep):
            return str(new_root) + value[len(old):]
    return value


@_serialized_bark_cache_prepare
def prepare_isolated_bark_source(
    source_spm,
    contract,
    required_rows,
    *,
    cache_parent=None,
):
    """Create or reuse one hash-addressed normalized Cluster provider copy."""
    source = Path(source_spm).resolve()
    if not source.is_file():
        raise ClusterBarkSourceResolutionError(
            f"Cluster bark source SPM is missing: {source}"
        )
    signature, identity = _normalization_identity(
        contract, source, required_rows
    )
    parent = Path(
        cache_parent
        or source.parent / ".sk_batch_isolated_bark"
    ).resolve()
    cache_dir = parent / signature[:24]
    tree_name = source.parent.parent.name
    isolated = cache_dir / tree_name / source.parent.name / source.name
    manifest_path = cache_dir / "bark_normalization_manifest.json"
    if manifest_path.is_file() and isolated.is_file():
        try:
            manifest = json.loads(
                manifest_path.read_text(encoding="utf-8")
            )
        except (OSError, ValueError):
            manifest = {}
        if (
            manifest.get("signature") == signature
            and manifest.get("source_spm_sha256")
            == identity["source_spm_sha256"]
            and manifest.get("isolated_spm_sha256")
            == _sha256_file(isolated)
            and _copied_manifest_artifacts_current(manifest)
        ):
            return {
                "status": "cached",
                "source_spm": str(source),
                "speedtree_spm": str(isolated),
                "signature": signature,
                "manifest": str(manifest_path),
                "production_source_mutated": False,
                "normalization": manifest.get("normalization") or {},
            }
    if cache_dir.exists():
        _remove_incomplete_cache_directory(cache_dir)

    parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(
        prefix=f".bark-{signature[:12]}-",
        dir=parent,
    ))
    try:
        isolated_tree_root = staging / tree_name
        staged_source = (
            isolated_tree_root / source.parent.name / source.name
        )
        staged_source.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, staged_source)
        copied_source_textures = _copy_source_tree_textures(
            identity, isolated_tree_root
        )
        copied_source_external_meshes = _copy_source_external_meshes(
            identity,
            staged_source,
            staging,
        )
        copied_source_external_textures = _copy_source_external_textures(
            identity,
            staged_source,
            isolated_tree_root,
        )
        copied_textures = _copy_canonical_textures(
            contract, isolated_tree_root
        )
        bark = copy.deepcopy(
            (contract.get("handoff") or {}).get("canonical_bark") or {}
        )
        bark["cluster_bark_sources"] = copy.deepcopy(required_rows)
        bark["status"] = "replacement_required"
        sliced_handoff = {"canonical_bark": bark}
        try:
            plan = build_isolated_bark_normalization_plan(
                sliced_handoff,
                {str(source): str(staged_source)},
                staging,
                preserve_source_material_name=True,
            )
            normalization = apply_isolated_bark_normalization(plan)
        except BarkNormalizationError as exc:
            raise ClusterBarkSourceResolutionError(str(exc)) from exc
        external_texture_rebase = _rebase_external_texture_refs(
            staged_source,
            source,
            copied_source_external_textures,
        )
        final_staged_hash = _sha256_file(staged_source)
        for output in normalization.get("outputs") or []:
            if _path_key(output.get("isolated_spm") or "") == _path_key(
                staged_source
            ):
                output["output_sha256"] = final_staged_hash
                output["external_texture_rebase"] = copy.deepcopy(
                    external_texture_rebase
                )
        final_source = (
            cache_dir / tree_name / source.parent.name / source.name
        )
        normalization = _rebase_paths(
            normalization, staging, cache_dir
        )
        copied_textures = _rebase_paths(
            copied_textures, staging, cache_dir
        )
        copied_source_textures = _rebase_paths(
            copied_source_textures, staging, cache_dir
        )
        copied_source_external_meshes = _rebase_paths(
            copied_source_external_meshes, staging, cache_dir
        )
        copied_source_external_textures = _rebase_paths(
            copied_source_external_textures, staging, cache_dir
        )
        external_texture_rebase = _rebase_paths(
            external_texture_rebase, staging, cache_dir
        )
        manifest = {
            "schema_version": 1,
            "kind": "cluster_isolated_canonical_bark_source",
            "status": "ready",
            "signature": signature,
            "source_spm": str(source),
            "source_spm_sha256": identity["source_spm_sha256"],
            "speedtree_spm": str(final_source),
            "isolated_spm_sha256": final_staged_hash,
            "identity": identity,
            "copied_source_tree_textures": copied_source_textures,
            "copied_source_external_meshes": copied_source_external_meshes,
            "copied_source_external_textures": (
                copied_source_external_textures
            ),
            "copied_canonical_textures": copied_textures,
            "external_texture_rebase": external_texture_rebase,
            "normalization": normalization,
            "production_source_mutated": False,
        }
        _atomic_write_json(
            staging / manifest_path.name,
            manifest,
        )
        _publish_cache_directory(staging, cache_dir)
        return {
            "status": "prepared",
            "source_spm": str(source),
            "speedtree_spm": str(final_source),
            "signature": signature,
            "manifest": str(manifest_path),
            "production_source_mutated": False,
            "normalization": normalization,
        }
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)


def resolve_cluster_bark_source_spm(
    source_spm,
    target_spms,
    *,
    cache_parent=None,
    live_target_contracts=None,
):
    """Resolve owner contracts and return an isolated provider when required.

    ``live_target_contracts`` is the authoritative result of the current PCG
    audit.  Persisted receipts remain a cache fallback for callers that do not
    have a live audit, but they must not turn a clean current audit into a
    stale-receipt false negative.
    """
    source = Path(source_spm).resolve()
    actionable = []
    receipt_rows = []
    live_by_target = {}
    for row in live_target_contracts or []:
        if not isinstance(row, dict) or not row.get("target_spm"):
            continue
        live_by_target[_path_key(row["target_spm"])] = row
    for target in target_spms or []:
        target = Path(target).resolve()
        live_row = live_by_target.get(_path_key(target))
        if live_row is not None:
            receipt = str(
                live_row.get("report")
                or live_row.get("receipt")
                or ""
            )
            receipt_policy = str(
                live_row.get("policy") or "live_audit_authoritative"
            )
            contract = live_row.get("contract")
            if not isinstance(contract, dict):
                receipt_rows.append({
                    "target_spm": str(target),
                    "receipt": receipt,
                    "policy": receipt_policy,
                    "dependency_matched": False,
                    "required_material_count": 0,
                })
                continue
            dependency = _dependency_for_source(contract, source)
            required_rows = (
                _required_bark_rows(contract, dependency)
                if dependency is not None
                else []
            )
        else:
            receipt_policy = "hash_current_receipt"
            try:
                receipt = locate_cluster_assembly_receipt(target)
                payload = load_cluster_assembly_receipt(
                    receipt,
                    requested_spm=target,
                )
                contract = payload.get("cluster_assembly") or {}
                dependency = _dependency_for_source(contract, source)
                required_rows = (
                    _required_bark_rows(contract, dependency)
                    if dependency is not None
                    else []
                )
            except (FileNotFoundError, ClusterAssemblyReceiptStaleError):
                fallback = _stale_receipt_bark_contract(target, source)
                if fallback is None:
                    continue
                receipt = fallback["receipt"]
                contract = fallback["contract"]
                dependency = _dependency_for_source(contract, source)
                required_rows = fallback["required_rows"]
                receipt_policy = fallback["policy"]
            except ClusterAssemblyReceiptError as exc:
                raise ClusterBarkSourceResolutionError(
                    f"Cluster Assembly receipt is invalid for {target}: {exc}"
                ) from exc
        if dependency is None:
            if live_row is not None:
                receipt_rows.append({
                    "target_spm": str(target),
                    "receipt": str(receipt),
                    "policy": receipt_policy,
                    "dependency_matched": False,
                    "required_material_count": 0,
                })
            continue
        receipt_rows.append({
            "target_spm": str(target),
            "receipt": str(receipt),
            "policy": receipt_policy,
            "dependency_matched": True,
            "required_material_count": len(required_rows),
        })
        if not required_rows:
            continue
        signature, _identity = _normalization_identity(
            contract, source, required_rows
        )
        actionable.append((signature, contract, required_rows))
    if not actionable:
        return {
            "status": "not_required",
            "source_spm": str(source),
            "speedtree_spm": str(source),
            "target_receipts": receipt_rows,
            "production_source_mutated": False,
        }
    signatures = {row[0] for row in actionable}
    if len(signatures) != 1:
        raise ClusterBarkSourceResolutionError(
            "Cluster owner targets disagree on the canonical bark source"
        )
    _signature, contract, required_rows = actionable[0]
    prepared = prepare_isolated_bark_source(
        source,
        contract,
        required_rows,
        cache_parent=cache_parent,
    )
    prepared["target_receipts"] = receipt_rows
    return prepared


__all__ = [
    "ClusterBarkSourceResolutionError",
    "prepare_isolated_bark_source",
    "resolve_cluster_bark_source_spm",
]
