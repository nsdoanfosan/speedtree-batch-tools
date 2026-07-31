"""Fail-closed SpeedTree export-option policy for production/validation jobs."""
from __future__ import annotations

import configparser
from pathlib import Path


class SpeedTreeExportOptionsError(RuntimeError):
    """An export preset can write copied/generated textures."""


def inspect_speedtree_export_options(path):
    preset = Path(path).expanduser().resolve()
    result = {
        "status": "missing",
        "path": str(preset),
        "texture_skip_writing": None,
        "error": "",
    }
    if not preset.is_file():
        result["error"] = f"SpeedTree export options are missing: {preset}"
        return result
    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read_string(preset.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, configparser.Error) as exc:
        result["status"] = "invalid"
        result["error"] = str(exc)
        return result
    if not parser.has_section("Options"):
        result["status"] = "invalid"
        result["error"] = "SpeedTree export options have no [Options] section"
        return result
    raw = parser.get("Options", "TextureSkipWriting", fallback="").strip()
    if raw.casefold() not in {"true", "false"}:
        result["status"] = "invalid"
        result["error"] = (
            "SpeedTree export options must explicitly declare "
            "TextureSkipWriting=true"
        )
        return result
    result["texture_skip_writing"] = raw.casefold() == "true"
    result["status"] = (
        "ok" if result["texture_skip_writing"] else "texture_writing_enabled"
    )
    if not result["texture_skip_writing"]:
        result["error"] = (
            "TextureSkipWriting=false would let SpeedTree create copied PNG "
            "textures beside an FBX/XML export"
        )
    return result


def require_texture_skip_writing(path, purpose="SpeedTree export"):
    """Return the inspected preset or raise before launching SpeedTree."""
    contract = inspect_speedtree_export_options(path)
    if contract["status"] != "ok":
        raise SpeedTreeExportOptionsError(
            f"{purpose} blocked: {contract['error']}: {contract['path']}. "
            "Use a preset with TextureSkipWriting=true; exported/copied "
            "textures are never a production material fallback."
        )
    return contract


__all__ = [
    "SpeedTreeExportOptionsError",
    "inspect_speedtree_export_options",
    "require_texture_skip_writing",
]
