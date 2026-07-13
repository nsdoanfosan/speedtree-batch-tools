"""Shared config/helpers for the SK batch pipeline tool.

Pure Python (no bpy). Used by the GUI and by spm_audit; the Blender-side job
scripts under jobs/ are self-contained on purpose (they run inside Blender).
"""
import hashlib
import json
import os
import re
import subprocess
import sys
import threading
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
    "probe_cache_enabled": True,
    # Stop after the first bad Relative/FBX verification. Restore the source
    # and mark it for manual handling instead of paying for more ~16s launches.
    "fast_skip_problem_spm": True,
    "rename_materials": True,    # checklist item 2: M_ prefix
    "backup_spm": True,
    # SpeedTree startup is expensive, so independent SPMs run concurrently.
    # A single slow export is bounded separately by spm_verify_timeout.
    "spm_parallel_jobs": 4,
    "check_parallel_jobs": 8,
    # resource limits (checklist "background + cpu limit")
    "priority": "belownormal",   # idle | belownormal | normal
    "cpu_cores": max(1, (os.cpu_count() or 8) // 2),
    "spm_verify_timeout": 120,
    "blender_job_timeout": 3600,
    "push_job_timeout": 1800,
    "process_poll_interval": 0.2,
}

PRIORITY_FLAGS = {
    "idle": 0x00000040,        # IDLE_PRIORITY_CLASS
    "belownormal": 0x00004000, # BELOW_NORMAL_PRIORITY_CLASS
    "normal": 0x00000020,      # NORMAL_PRIORITY_CLASS
}
CREATE_NO_WINDOW = 0x08000000
CALIBRATION_CACHE_VERSION = 1
_JSON_WRITE_LOCK = threading.RLock()


def _atomic_write_json(path, data):
    """Serialize JSON without exposing a partially-written state/config file."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(f".{target.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    with _JSON_WRITE_LOCK:
        try:
            payload = json.dumps(data, indent=2, ensure_ascii=False)
            temp.write_text(payload, encoding="utf-8")
            os.replace(temp, target)
        finally:
            if temp.exists():
                temp.unlink()


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
    _atomic_write_json(CONFIG_PATH, cfg)


def load_state():
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_state(state):
    _atomic_write_json(STATE_PATH, state)


def file_content_fingerprint(path, digest_size=16):
    """Fast stable content key; 74 current SPMs hash in roughly 0.1s serial."""
    hasher = hashlib.blake2b(digest_size=digest_size)
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def file_content_snapshot(path):
    """Content fingerprint paired with the exact stat observed while hashing."""
    candidate = Path(path)
    for _attempt in range(2):
        before = candidate.stat()
        fingerprint = file_content_fingerprint(candidate)
        after = candidate.stat()
        before_key = (before.st_size, before.st_mtime_ns)
        after_key = (after.st_size, after.st_mtime_ns)
        if before_key == after_key:
            return {
                "fingerprint": fingerprint,
                "size": after.st_size,
                "mtime_ns": after.st_mtime_ns,
            }
    raise RuntimeError(f"File changed while hashing: {candidate}")


def _dependency_identity(path, hash_content=False):
    candidate = Path(path) if path else None
    if not candidate or not candidate.exists():
        return {"path": str(candidate or ""), "missing": True}
    try:
        stat = candidate.stat()
        identity = {
            "path": str(candidate.resolve()),
            "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
        }
        if hash_content:
            identity["fingerprint"] = file_content_fingerprint(candidate)
        return identity
    except OSError as exc:
        return {"path": str(candidate), "error": str(exc)}


def calibration_settings_signature(cfg):
    """Invalidate result caches when behavior, presets, or target values change."""
    setting_keys = (
        "target_bones_per_branch",
        "max_total_bones",
        "total_window_low",
        "total_window_high",
        "seed_relative_value",
        "value_cap",
        "value_floor",
        "max_calibration_rounds",
        "probe_cache_enabled",
        "fast_skip_problem_spm",
        "spm_verify_timeout",
        "rename_materials",
    )
    payload = {
        "version": CALIBRATION_CACHE_VERSION,
        "settings": {key: cfg.get(key) for key in setting_keys},
        "spm_audit": _dependency_identity(TOOL_DIR / "spm_audit.py", hash_content=True),
        "xml_ini": _dependency_identity(cfg.get("xml_ini"), hash_content=True),
        "fbx_ini": _dependency_identity(cfg.get("fbx_ini"), hash_content=True),
        # Hashing the large executable would erase the speed win; size+mtime
        # changes whenever the installed SpeedTree build is replaced.
        "speedtree_exe": _dependency_identity(cfg.get("speedtree_exe")),
    }
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.blake2b(encoded, digest_size=16).hexdigest()


def calibration_cache_matches(cache, spm_fingerprint, settings_signature):
    return bool(
        isinstance(cache, dict)
        and cache.get("version") == CALIBRATION_CACHE_VERSION
        and cache.get("spm_fingerprint") == spm_fingerprint
        and cache.get("settings_signature") == settings_signature
    )


def load_job_report(path):
    """Best-effort job JSON loader; malformed/missing reports stay diagnosable."""
    report_path = Path(path)
    if not report_path.exists():
        return {"_report_error": "job report was not created"}
    try:
        data = json.loads(report_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"_report_error": f"job report could not be read: {exc}"}
    if isinstance(data, list):
        data = data[0] if data and isinstance(data[0], dict) else {}
    return data if isinstance(data, dict) else {"_report_error": "job report is not an object"}


def compact_error_message(message, max_chars=100):
    """One-line status text, without a long log path or traceback whitespace."""
    text = re.sub(r"\s+", " ", str(message or "")).strip()
    text = re.sub(r"\s+[—-]\s+로그:\s+.*$", "", text, flags=re.IGNORECASE)
    if not text:
        return "원인 확인 불가"
    if len(text) > max_chars:
        return text[: max(1, max_chars - 1)].rstrip() + "…"
    return text


def _read_log_tail(path, max_bytes=65536):
    try:
        log_path = Path(path)
        with log_path.open("rb") as handle:
            size = handle.seek(0, 2)
            handle.seek(max(0, size - max_bytes))
            return handle.read().decode("utf-8", errors="replace")
    except Exception:
        return ""


def summarize_job_failure(report=None, log_path=None, max_chars=100):
    """Extract a short actionable cause from Blender/Unreal reports and logs."""
    report = report if isinstance(report, dict) else {}
    sources = []
    for key in ("error", "reason", "message", "traceback", "trace", "_report_error"):
        value = report.get(key)
        if value:
            sources.append(str(value))
    wind = report.get("wind")
    if isinstance(wind, dict):
        for key in ("error", "trace"):
            if wind.get(key):
                sources.append(str(wind[key]))
    if log_path:
        sources.append(_read_log_tail(log_path))
    text = "\n".join(sources)
    lowered = text.lower()

    if "unreal editor crashed or exited during push" in lowered:
        return "Unreal Editor 크래시 — Push 중 에디터 종료"

    if any(
        token in lowered
        for token in (
            "could not find an open unreal editor instance",
            "rpc not reachable",
            "unreal editor is not running",
            "connectionreseterror",
            "winerror 10054",
        )
    ):
        return "Unreal 연결 실패 — 에디터/RPC 응답 없음"

    mesh_match = re.search(r"mesh not found:\s*([^\r\n(]+)", text, re.IGNORECASE)
    if mesh_match:
        return compact_error_message(f"Unreal 메시를 찾지 못함: {mesh_match.group(1).strip()}", max_chars)

    if "codexdynamicwindimportlibrary missing" in lowered:
        return "Unreal Codex 플러그인 미로드"
    if "unreal wind import: no result file" in lowered or "wind import" in lowered and "timed out" in lowered:
        return "Unreal wind 처리 시간 초과"
    if "unreal wind import failed" in lowered:
        match = re.search(r"unreal wind import failed:\s*([^\r\n]+)", text, re.IGNORECASE)
        detail = match.group(1).strip() if match else "상세 결과는 로그 확인"
        return compact_error_message(f"Unreal wind 적용 실패: {detail}", max_chars)
    if "armature-only" in lowered or "contains no mesh geometry" in lowered:
        return "FBX 메시 지오메트리 없음"
    if ".crash.txt" in lowered or "writing:" in lowered and "blender" in lowered:
        return "Blender 백그라운드 크래시"
    if "speedtree" in lowered and "export" in lowered and "fail" in lowered:
        return "SpeedTree export 실패"
    if "not sk-ready" in lowered or "bones disabled" in lowered:
        return "SK 본 설정 미완료"
    if "no module named" in lowered or "addon_enable" in lowered and "error" in lowered:
        return "Blender add-on 로드 실패"
    if "send2ue returned" in lowered:
        match = re.search(r"send2ue returned\s*([^\r\n]+)", text, re.IGNORECASE)
        return compact_error_message(f"Send to Unreal 실행 실패: {(match.group(1) if match else '').strip()}", max_chars)
    if "export_from_speedtree returned" in lowered:
        match = re.search(r"export_from_speedtree returned\s*([^\r\n]+)", text, re.IGNORECASE)
        return compact_error_message(f"Blender repair 실행 실패: {(match.group(1) if match else '').strip()}", max_chars)

    exception_lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if re.match(r"^[A-Za-z_][\w.]*?(?:Error|Exception):\s*.+", stripped):
            exception_lines.append(stripped)
    if exception_lines:
        return compact_error_message(exception_lines[-1], max_chars)

    primary = report.get("error") or report.get("reason") or report.get("message")
    if primary:
        first_line = next((line.strip() for line in str(primary).splitlines() if line.strip()), primary)
        return compact_error_message(first_line, max_chars)
    return compact_error_message(report.get("_report_error") or "원인 확인 불가", max_chars)


BACKUP_RE = re.compile(r"\.(codex_backup|skbatch_backup|pcgtex_backup)", re.IGNORECASE)
BACKUP_SUBDIR = "_spm_backups"
MANUAL_BONES_SUFFIX = ".skbatch_manual_bones.json"
# Older runs used a per-tool folder name; still skip it so stragglers never
# reappear in the working list.
LEGACY_BACKUP_SUBDIRS = ("_skbatch_backup",)


def manual_bones_marker_path(spm_path):
    """Persistent marker stored beside the SPM backups, outside the scan list."""
    spm = Path(spm_path)
    return spm.parent / BACKUP_SUBDIR / f"{spm.stem}{MANUAL_BONES_SUFFIX}"


def is_manual_bones_locked(spm_path, state_entry=None):
    """State is the fast local cache; the marker survives GUI/repo moves."""
    if state_entry and state_entry.get("manual_bones_locked", False):
        return True
    return manual_bones_marker_path(spm_path).exists()


def set_manual_bones_marker(spm_path, locked):
    marker = manual_bones_marker_path(spm_path)
    if locked:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(
            json.dumps(
                {
                    "version": 1,
                    "spm": str(Path(spm_path)),
                    "manual_bones_locked": True,
                    "note": "Preserve user-authored SpeedTree bone settings; skip automatic calibration.",
                },
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    elif marker.exists():
        marker.unlink()
    return marker


def scan_sk_spms(root):
    """All live SK_*.spm while pruning backup trees before descending."""
    out = []
    skip_dirs = {BACKUP_SUBDIR, *LEGACY_BACKUP_SUBDIRS}
    root = Path(root)
    if not root.exists():
        return out
    for current, dirs, files in os.walk(root, topdown=True):
        dirs[:] = [name for name in dirs if name not in skip_dirs]
        for name in files:
            if not name.lower().startswith("sk_") or not name.lower().endswith(".spm"):
                continue
            if BACKUP_RE.search(name):
                continue
            out.append(Path(current) / name)
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


def terminate_process_tree(proc, wait_seconds=5.0):
    """Terminate a managed process and all of its descendants.

    Killing only the Python wrapper leaves SpeedTree_Modeler.exe orphaned on
    Windows. Repeated stop/retry cycles then accumulate multi-GB workers and
    make later calibration batches appear hung.

    Returns True when Windows confirms the tree kill (or the process was already
    gone). A direct-process kill remains as a last resort, but returns False
    because descendants could not be confirmed terminated.
    """
    if proc.poll() is not None:
        return True

    tree_confirmed = False
    if os.name == "nt":
        try:
            result = subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=max(1.0, float(wait_seconds)),
                creationflags=CREATE_NO_WINDOW,
            )
            tree_confirmed = result.returncode == 0
        except (OSError, subprocess.SubprocessError):
            tree_confirmed = False
    else:
        try:
            proc.terminate()
            tree_confirmed = True
        except OSError:
            tree_confirmed = proc.poll() is not None

    if proc.poll() is None:
        try:
            proc.wait(timeout=max(0.1, float(wait_seconds)))
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
                proc.wait(timeout=max(0.1, float(wait_seconds)))
            except (OSError, subprocess.SubprocessError):
                pass
    return tree_confirmed and proc.poll() is not None
