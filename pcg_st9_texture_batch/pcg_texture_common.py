"""Shared helpers for the PCG ST9 texture batch status board."""
import json
import os
import re
from pathlib import Path

from speedtree_pipeline_contract import is_live_spm

TOOL_DIR = Path(__file__).resolve().parent
CONFIG_PATH = TOOL_DIR / "pcg_texture_config.json"
STATE_PATH = TOOL_DIR / "pcg_texture_state.json"
TARGETS_PATH = TOOL_DIR / "pcg_targets.json"
REPORT_DIR = TOOL_DIR / "reports"
SHARED_CACHE_DIR_ENV = "SPEEDTREE_BATCH_TOOLS_CACHE_DIR"


def default_shared_cache_dir():
    """Return the per-user cache shared by every checkout/worktree."""
    override = os.environ.get(SHARED_CACHE_DIR_ENV)
    if override:
        return Path(override).expanduser().resolve(strict=False)
    if os.name == "nt":
        local_app_data = os.environ.get("LOCALAPPDATA")
        root = (
            Path(local_app_data)
            if local_app_data
            else Path.home() / "AppData" / "Local"
        )
    else:
        xdg_cache_home = os.environ.get("XDG_CACHE_HOME")
        root = (
            Path(xdg_cache_home)
            if xdg_cache_home
            else Path.home() / ".cache"
        )
    return root / "SpeedTreeBatchTools" / "cache"


SHARED_CACHE_DIR = default_shared_cache_dir()

DEFAULT_CONFIG = {
    "tree_root": r"D:\OneDrive\Forestportfolio\02_nature\Tree",
    "atlas_root": r"D:\OneDrive\Forestportfolio\02_nature\Tree\atlas",
    "unreal_project": r"C:\UnrealProjects\MyProject2",
    "unreal_editor_cmd": (
        r"C:\Program Files\Epic Games\UE_5.8\Engine\Binaries\Win64"
        r"\UnrealEditor-Cmd.exe"),
    "unreal_texture_sync_enabled": True,
    "unreal_texture_destination": "/Game/Textures",
    "unreal_texture_commandlet_fallback": True,
    "unreal_texture_sync_timeout": 1800,
    "pcg_target_refresh_timeout": 120,
    "pcg_database_content": r"C:\UnrealProjects\MyProject2\Content\PCG\DataBase",
    "unreal_levels": ["/Game/Level/Cliff_final_01"],
    "pcg_focus_data_assets": [
        "/Game/PCG/DataBase/landscape/DA_Base_05",
        "/Game/PCG/DataBase/landscape/DA_Base_06",
    ],
    "pcg_positive_weight_only": True,
    "source_texture_roots": [r"D:\OneDrive\Forestportfolio\Texture"],
    "required_export_maps": ["color", "normal", "extra", "height", "opacity", "subsurface"],
    "blender_exe": r"C:\Program Files\Blender Foundation\Blender 5.2\blender.exe",
    "designer_dir": r"C:\Program Files\Adobe\Adobe Substance 3D Designer",
    "cluster_sbsar": r"D:\OneDrive\Forestportfolio\substanceDesigner\Cluster_System_01.sbsar",
    "cluster_sbsar_normal_behavior": "opengl_to_directx",
    "atlas_job_timeout": 1800,
    "sbsrender_timeout": 1800,
}

# " - 복사본"/" - Copy" are Windows Explorer duplicates: manual scratch copies,
# not pipeline sources. Counting them can split otherwise-unique cluster
# definitions and silently disable preserve-source handling for a folder.
BACKUP_RE = re.compile(
    r"(codex_backup|skbatch_backup|pcgtex_backup|\.sbk$|^~|\.blend1$"
    r"|^__spm_sync_(?:preflight|verify)_|^\.__spm_pass_repair_"
    r"|\.texture_slot_backup_[^.]*\.spm$"
    r"|pre_xml_root_fix|(?:^|[_\-.])probe(?:[_\-.]|$)"
    r"|\s-\s(?:복사본|copy)(?:\s\(\d+\))?\.)",
    re.IGNORECASE,
)
IMAGE_EXTS = {".png", ".tga", ".tif", ".tiff", ".jpg", ".jpeg", ".exr", ".bmp"}
MODEL_EXTS = {".spm", ".st9"}


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        try:
            cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
        except Exception:
            pass
    cfg["blender_exe"] = DEFAULT_CONFIG["blender_exe"]
    return cfg


def save_config(cfg):
    normalized = dict(cfg)
    normalized["blender_exe"] = DEFAULT_CONFIG["blender_exe"]
    CONFIG_PATH.write_text(
        json.dumps(normalized, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def load_state():
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_state(state):
    STATE_PATH.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


def is_backup_path(path):
    candidate = Path(path)
    return bool(BACKUP_RE.search(candidate.name)) or (
        candidate.suffix.casefold() == ".spm"
        and not is_live_spm(candidate, require_file=False)
    )


def is_image_path(path):
    return Path(str(path)).suffix.lower() in IMAGE_EXTS


def json_safe_path(path):
    return str(Path(path))


def load_pcg_targets(path=None):
    target_path = Path(path) if path else TARGETS_PATH
    if not target_path.exists():
        return None
    try:
        return json.loads(target_path.read_text(encoding="utf-8"))
    except Exception:
        return None


def save_pcg_targets(data, path=None):
    target_path = Path(path) if path else TARGETS_PATH
    target_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    return target_path
