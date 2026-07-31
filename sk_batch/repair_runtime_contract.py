"""Saved-output contract plus diagnostic producer hashes for Blender Repair."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path


REPAIR_RUNTIME_RECEIPT_VERSION = 2
# Increment this only when an existing saved Blender Repair result is no
# longer semantically valid.  Source-file hashes below are diagnostics, not a
# cache-invalidation contract.
REPAIR_OUTPUT_CONTRACT_VERSION = 1
# Runtime receipt schemas that predate ``output_contract_version`` all
# describe this original saved-output contract.  Receipt schema revisions and
# producer hashes are deliberately not cache invalidators.
LEGACY_REPAIR_OUTPUT_CONTRACT_VERSION = 1
TOOL_DIR = Path(__file__).resolve().parent
REPO_DIR = TOOL_DIR.parent


def addon_dir_from_config(cfg):
    """Resolve the installed BWR add-on folder exactly as SK Batch does."""
    value = str((cfg or {}).get("fbx_ini") or "")
    if not value:
        return None
    try:
        return Path(value).resolve().parents[2]
    except (IndexError, OSError):
        return None


def repair_runtime_code_paths(addon_dir):
    """Return producer modules recorded for diagnostics after a build."""
    addon_dir = Path(addon_dir)
    paths = list(addon_dir.rglob("*.py"))
    paths.extend([
        REPO_DIR / "speedtree_pipeline_contract.py",
        REPO_DIR / "cluster_spm_pair_contract.py",
        REPO_DIR / "cluster_blend_sync.py",
        REPO_DIR / "cluster_bark_source_resolution.py",
        REPO_DIR / "cluster_normalization_sync.py",
        REPO_DIR / "spm_generator_sync" / "jobs"
        / "cluster_relation_job.py",
        REPO_DIR / "pcg_st9_texture_batch"
        / "pcg_cluster_assembly_contract.py",
        REPO_DIR / "pcg_st9_texture_batch"
        / "pcg_cluster_bark_normalization.py",
        REPO_DIR / "pcg_st9_texture_batch" / "pcg_texture_audit.py",
        TOOL_DIR / "jobs" / "bwr_headless_job.py",
        TOOL_DIR / "jobs" / "speedtree_material_preflight.py",
        TOOL_DIR / "cluster_assembly_builder.py",
        TOOL_DIR / "cluster_assembly_handoff_contract.py",
        TOOL_DIR / "nanite_assembly_materials.py",
        TOOL_DIR / "repair_runtime_contract.py",
    ])
    unique = {}
    for path in paths:
        candidate = Path(path)
        if candidate.is_file():
            unique[os.path.normcase(str(candidate.resolve())).casefold()] = (
                candidate.resolve()
            )
    return [unique[key] for key in sorted(unique)]


def repair_runtime_code_state(addon_dir, modules=None):
    """Return diagnostic content hashes independent of source timestamps."""
    addon_dir = Path(addon_dir).resolve()
    modules = list(
        repair_runtime_code_paths(addon_dir)
        if modules is None
        else modules
    )
    if not modules:
        return None

    def source_snapshot():
        rows = []
        for module in modules:
            candidate = Path(module).resolve()
            stat = candidate.stat()
            rows.append((
                str(candidate),
                stat.st_size,
                stat.st_mtime_ns,
            ))
        return tuple(rows)

    for _attempt in range(2):
        before = source_snapshot()
        state = {}
        for module in modules:
            module = Path(module).resolve()
            try:
                key = "addon/" + module.relative_to(addon_dir).as_posix()
            except ValueError:
                try:
                    key = (
                        "repo/"
                        + module.relative_to(REPO_DIR.resolve()).as_posix()
                    )
                except ValueError:
                    key = "external/" + os.path.normcase(str(module))
            state[key] = hashlib.sha256(module.read_bytes()).hexdigest()
        after = source_snapshot()
        if after == before:
            return state
    raise OSError("Repair runtime source changed while hashing")


def repair_runtime_receipt_path(spm):
    spm = Path(spm)
    return spm.parent / "reports" / f"{spm.stem}_repair_runtime_codex.json"


def repair_runtime_output_contract(payload):
    """Return the semantic saved-output contract recorded by *payload*.

    Old runtime receipts did not have an explicit semantic contract field.
    They all belong to the original contract, regardless of their diagnostic
    receipt schema version or producer-code hash layout.  Unknown/corrupt
    payloads return ``None`` so callers can rely on the real artifact contract
    and replace the diagnostic receipt when that contract is independently
    proven current.
    """
    if not isinstance(payload, dict):
        return None
    if payload.get("kind") != "sk_repair_runtime":
        return None
    value = payload.get("output_contract_version")
    if value is None:
        return LEGACY_REPAIR_OUTPUT_CONTRACT_VERSION
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def repair_runtime_receipt_needs_migration(payload):
    """Whether a compatible/current artifact should rewrite this receipt."""
    if not isinstance(payload, dict):
        return True
    if payload.get("kind") != "sk_repair_runtime":
        return True
    try:
        schema_version = int(payload.get("version"))
    except (TypeError, ValueError):
        return True
    if schema_version != REPAIR_RUNTIME_RECEIPT_VERSION:
        return True
    if repair_runtime_output_contract(payload) != REPAIR_OUTPUT_CONTRACT_VERSION:
        return True
    if "output_contract_version" not in payload:
        return True
    if not isinstance(payload.get("code"), dict):
        return True
    return False


def _atomic_write_receipt(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        delete=False,
        dir=str(path.parent),
        prefix=f".{path.name}.",
        suffix=".tmp",
    ) as handle:
        temporary = Path(handle.name)
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def migrate_repair_runtime_receipt(spm, payload, *, addon_dir=None):
    """Rewrite compatible diagnostic metadata without re-running Repair.

    This is called only after the caller independently validates the live
    content-addressed artifact contract.  Preserve old producer hashes when
    present so scanning many legacy assets does not repeatedly hash the whole
    add-on; a later successful Repair will refresh those diagnostics normally.
    """
    if (
        isinstance(payload, dict)
        and payload.get("kind") == "sk_repair_runtime"
    ):
        migrated = dict(payload)
    else:
        migrated = {}
    migrated.update({
        "kind": "sk_repair_runtime",
        "version": REPAIR_RUNTIME_RECEIPT_VERSION,
        "output_contract_version": REPAIR_OUTPUT_CONTRACT_VERSION,
    })
    if addon_dir is not None:
        migrated["addon_dir"] = str(addon_dir)
    if not isinstance(migrated.get("code"), dict):
        migrated["code"] = {}
    path = repair_runtime_receipt_path(spm)
    _atomic_write_receipt(path, migrated)
    return path


def write_repair_runtime_receipt(
    spm,
    cfg,
    *,
    addon_dir=None,
    code_state=None,
):
    """Atomically record output compatibility and diagnostic producer hashes."""
    addon_dir = addon_dir or addon_dir_from_config(cfg)
    if addon_dir is None:
        return None
    state = (
        code_state
        if code_state is not None
        else repair_runtime_code_state(addon_dir)
    )
    if not state:
        return None
    path = repair_runtime_receipt_path(spm)
    payload = {
        "kind": "sk_repair_runtime",
        "version": REPAIR_RUNTIME_RECEIPT_VERSION,
        "output_contract_version": REPAIR_OUTPUT_CONTRACT_VERSION,
        "addon_dir": str(addon_dir),
        "code": state,
    }
    _atomic_write_receipt(path, payload)
    return path
