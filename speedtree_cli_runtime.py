"""Shared resolution for the repository's collision-aware SpeedTree CLI."""

from __future__ import annotations

import os
from pathlib import Path


COLLISION_CLI_ENV = "SPEEDTREE_COLLISION_CLI_EXE"
COLLISION_CLI_NAME = "speedtree_collision_cli.exe"


def repository_collision_cli(repo_dir: Path | None = None) -> Path:
    root = Path(repo_dir or Path(__file__).resolve().parent)
    return (root / "speedtree_collision_cli" / "bin" / COLLISION_CLI_NAME).resolve()


def require_collision_cli(repo_dir: Path | None = None) -> Path:
    configured = str(os.environ.get(COLLISION_CLI_ENV) or "").strip()
    executable = (
        Path(configured).expanduser().resolve()
        if configured
        else repository_collision_cli(repo_dir)
    )
    source = COLLISION_CLI_ENV if configured else "repository collision CLI"
    if executable.name.casefold() != COLLISION_CLI_NAME:
        raise ValueError(
            f"{source} must point to {COLLISION_CLI_NAME}: {executable}"
        )
    if not executable.is_file():
        raise FileNotFoundError(
            "Collision-aware SpeedTree CLI is unavailable: "
            f"{executable}. Launch SpeedTree_Batch_Tools.bat so build.ps1 "
            "can build and verify the supported native CLI."
        )
    return executable


def verification_export_command(
    speedtree_exe: Path,
    spm_path: Path,
    xml_ini: Path,
    output: Path,
    *,
    repo_dir: Path | None = None,
) -> list[str]:
    """Build a verification command, preferring the shared custom CLI."""
    modeler = Path(speedtree_exe).expanduser().resolve()
    already_wrapper = modeler.name.casefold() == COLLISION_CLI_NAME
    stock_modeler = modeler.name.casefold() == "speedtree_modeler.exe"
    use_wrapper = already_wrapper or stock_modeler
    if not use_wrapper:
        return [
            str(modeler), str(spm_path),
            "-export_options", str(xml_ini),
            "-export", str(output),
        ]

    wrapper = modeler if already_wrapper else require_collision_cli(repo_dir)
    command = [str(wrapper), "--verification-only"]
    if not already_wrapper:
        command.extend(["--modeler", str(modeler)])
    command.extend([
        "--", str(spm_path),
        "-export_options", str(xml_ini),
        "-export", str(output),
    ])
    return command
