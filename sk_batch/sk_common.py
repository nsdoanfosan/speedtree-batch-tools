"""Shared config/helpers for the SK batch pipeline tool.

Pure Python (no bpy). Used by the GUI and by spm_audit; the Blender-side job
scripts under jobs/ are self-contained on purpose (they run inside Blender).
"""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parent
REPO_ROOT = TOOL_DIR.parent


def _default_addon_dir():
    """Locate the separately checked-out Blender add-on repository."""
    override = os.environ.get("SPEEDTREE_BWR_ADDON_DIR")
    if override:
        return Path(override).expanduser()
    return (
        REPO_ROOT.parent
        / "speedtree-bone-weight-repair-addon"
        / "addons"
        / "speedtree_bone_weight_repair"
    )


ADDON_DIR = _default_addon_dir()
PRESET_DIR = ADDON_DIR / "presets" / "speedtree_10_1"

CONFIG_PATH = TOOL_DIR / "sk_batch_config.json"
STATE_PATH = TOOL_DIR / "sk_batch_state.json"
LOG_DIR = TOOL_DIR / "logs"

DEFAULT_CONFIG = {
    "root": r"D:\OneDrive\Forestportfolio\02_nature\Tree",
    "blender_exe": r"C:\Program Files\Blender Foundation\Blender 5.1\blender.exe",
    "speedtree_exe": r"C:\Program Files\SpeedTree\SpeedTree Modeler v10.1.0\win64\SpeedTree_Modeler.exe",
    "fbx_ini": str(PRESET_DIR / "Options_MA_Fbx.ini"),
    "xml_ini": str(PRESET_DIR / "Options_HI_Xml.ini"),
    # SPM bone calibration (size-aware, total-budget):
    # probe(Absolute/1) counts total branches, then ONE Relative value is solved
    # so total bones ~= min(branches x per_branch, max_total). Small plants land
    # near per_branch; big trees hit the cap, leaving tiny twigs at 0-1 bone.
    "target_bones_per_branch": 2.0,   # small-plant target (bones per branch)
    "max_total_bones": 1500,          # hard cap on a tree's total bones
    "total_window_low": 0.6,          # accept total in [low, high] x target
    "total_window_high": 1.5,
    "seed_relative_value": 0.5,       # first Relative value tried
    "value_cap": 64.0,
    "value_floor": 0.02,
    "max_calibration_rounds": 4,
    "rename_materials": True,    # checklist item 2: M_ prefix
    "backup_spm": True,
    # SpeedTree CLI export is cold-start bound (~16s regardless of tree size),
    # and runs scale ~linearly across cores, so process several SPMs at once.
    "spm_parallel_jobs": 4,
    # resource limits (checklist "background + cpu limit")
    "priority": "belownormal",   # idle | belownormal | normal
    "cpu_cores": max(1, (os.cpu_count() or 8) // 2),
    "spm_verify_timeout": 900,
    "blender_job_timeout": 3600,
    "push_job_timeout": 1800,
}

PRIORITY_FLAGS = {
    "idle": 0x00000040,        # IDLE_PRIORITY_CLASS
    "belownormal": 0x00004000, # BELOW_NORMAL_PRIORITY_CLASS
    "normal": 0x00000020,      # NORMAL_PRIORITY_CLASS
}
CREATE_NO_WINDOW = 0x08000000


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        try:
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            # Only accept keys we still use, so options removed in a redesign
            # (e.g. the old per-branch-average knobs) don't linger in the file.
            cfg.update({k: v for k, v in data.items() if k in DEFAULT_CONFIG})
        except Exception:
            pass
    return cfg


def save_config(cfg):
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")


def load_state():
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_state(state):
    STATE_PATH.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


BACKUP_RE = re.compile(r"\.(codex_backup|skbatch_backup|pcgtex_backup)", re.IGNORECASE)
BACKUP_SUBDIR = "_spm_backups"
# Older runs used a per-tool folder name; still skip it so stragglers never
# reappear in the working list.
LEGACY_BACKUP_SUBDIRS = ("_skbatch_backup",)


def scan_sk_spms(root):
    """All SK_*.spm under root, excluding backup copies and the backup folder."""
    out = []
    skip_dirs = {BACKUP_SUBDIR, *LEGACY_BACKUP_SUBDIRS}
    for path in Path(root).rglob("SK_*.spm"):
        if BACKUP_RE.search(path.name):
            continue
        if skip_dirs.intersection(path.parts):
            continue
        out.append(path)
    return sorted(out)


# Wind preset from the file name (checklist item 4). Dead vegetation must not
# sway at all, so it wins over every other token.
def wind_preset_for(stem):
    s = stem.lower()
    if "deadleave" in s or "deadbranch" in s:
        return "NONE"
    if "tree" in s:
        return "TREE"
    if "bush" in s:
        return "BUSH"
    if "weed" in s or "grass" in s:
        return "GRASS"
    return "GRASS"


def blend_path_for(spm_path):
    """One .blend per SPM, next to it (matches SK_tree_elm_01.blend convention)."""
    spm = Path(spm_path)
    return spm.with_suffix(".blend")


def set_process_affinity(pid, cores):
    """Limit a process to the first `cores` logical CPUs (inherited by children)."""
    import ctypes

    total = os.cpu_count() or 1
    cores = max(1, min(int(cores), total))
    if cores >= total:
        return False
    mask = (1 << cores) - 1
    PROCESS_SET_INFORMATION = 0x0200
    PROCESS_QUERY_INFORMATION = 0x0400
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(PROCESS_SET_INFORMATION | PROCESS_QUERY_INFORMATION, False, pid)
    if not handle:
        return False
    try:
        return bool(kernel32.SetProcessAffinityMask(handle, mask))
    finally:
        kernel32.CloseHandle(handle)


def launch_limited(cmd, cfg, log_file=None, cwd=None, affinity=True):
    """Start a background child at reduced priority + optional CPU affinity.

    Priority class and affinity are inherited by grandchildren (Blender ->
    SpeedTree CLI), so one launch covers the whole job tree. Returns Popen.

    affinity=False leaves the child free to use every core: use this when the
    caller runs several children at once, where the whole point is to spread
    the (cold-start-bound) SpeedTree exports across all cores. Priority alone
    keeps the machine responsive.
    """
    flags = PRIORITY_FLAGS.get(cfg.get("priority", "belownormal"), PRIORITY_FLAGS["belownormal"])
    flags |= CREATE_NO_WINDOW
    handle = open(log_file, "w", encoding="utf-8", errors="replace") if log_file else None
    proc = subprocess.Popen(
        cmd,
        stdout=handle if handle else subprocess.DEVNULL,
        stderr=subprocess.STDOUT,
        cwd=cwd,
        creationflags=flags,
    )
    proc.sk_log_handle = handle  # caller closes after wait (see GUI _run_limited)
    try:
        if affinity:
            set_process_affinity(proc.pid, cfg.get("cpu_cores", os.cpu_count()))
    except Exception:
        pass
    return proc
