"""Shared producer-code receipt for completed Blender Repair/source builds."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path


REPAIR_RUNTIME_RECEIPT_VERSION = 2
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
    """Return every producer module that can change a completed build."""
    addon_dir = Path(addon_dir)
    paths = list(addon_dir.rglob("*.py"))
    paths.extend([
        REPO_DIR / "speedtree_pipeline_contract.py",
        REPO_DIR / "cluster_spm_pair_contract.py",
        REPO_DIR / "cluster_blend_sync.py",
        REPO_DIR / "cluster_normalization_sync.py",
        REPO_DIR / "spm_generator_sync" / "jobs"
        / "cluster_relation_job.py",
        REPO_DIR / "pcg_st9_texture_batch"
        / "pcg_cluster_assembly_contract.py",
        REPO_DIR / "pcg_st9_texture_batch" / "pcg_texture_audit.py",
        TOOL_DIR / "jobs" / "bwr_headless_job.py",
        TOOL_DIR / "jobs" / "speedtree_material_preflight.py",
        TOOL_DIR / "cluster_assembly_builder.py",
        TOOL_DIR / "cluster_assembly_handoff_contract.py",
        TOOL_DIR / "nanite_assembly_materials.py",
    ])
    unique = {}
    for path in paths:
        candidate = Path(path)
        if candidate.is_file():
            unique[os.path.normcase(str(candidate.resolve())).casefold()] = (
                candidate.resolve()
            )
    return [unique[key] for key in sorted(unique)]


def repair_runtime_code_state(addon_dir):
    """Return content hashes independent of source timestamps."""
    addon_dir = Path(addon_dir).resolve()
    modules = repair_runtime_code_paths(addon_dir)
    if not modules:
        return None
    state = {}
    for module in modules:
        try:
            key = "addon/" + module.relative_to(addon_dir).as_posix()
        except ValueError:
            try:
                key = "repo/" + module.relative_to(REPO_DIR.resolve()).as_posix()
            except ValueError:
                key = "external/" + os.path.normcase(str(module))
        state[key] = hashlib.sha256(module.read_bytes()).hexdigest()
    return state


def repair_runtime_receipt_path(spm):
    spm = Path(spm)
    return spm.parent / "reports" / f"{spm.stem}_repair_runtime_codex.json"


def write_repair_runtime_receipt(spm, cfg):
    """Atomically record the code that produced a successful saved result."""
    addon_dir = addon_dir_from_config(cfg)
    if addon_dir is None:
        return None
    state = repair_runtime_code_state(addon_dir)
    if not state:
        return None
    path = repair_runtime_receipt_path(spm)
    payload = {
        "kind": "sk_repair_runtime",
        "version": REPAIR_RUNTIME_RECEIPT_VERSION,
        "addon_dir": str(addon_dir),
        "code": state,
    }
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
    return path
