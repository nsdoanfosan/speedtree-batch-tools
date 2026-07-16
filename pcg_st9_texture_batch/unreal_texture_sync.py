"""Synchronize canonical SpeedTree TGA outputs into Unreal without P4 churn.

The source TGA MD5 is compared with Unreal's saved ``AssetImportData.FileMD5``.
An identical texture is never checked out, reimported, or saved.  New assets are
explicitly marked for add, while only checkouts owned by the current run are
eligible for ``revert unchanged`` cleanup.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from pcg_texture_common import REPORT_DIR, load_config


POLICY_VERSION = 1
ROLE_SUFFIXES = (
    ("_subsurface", "subsurface"),
    ("_opacity", "opacity"),
    ("_normal", "normal"),
    ("_height", "height"),
    ("_extra", "extra"),
    ("_color", "color"),
)


class UnrealTextureSyncDeferred(RuntimeError):
    """The safe sync path is temporarily unavailable; local output is intact."""


def texture_role(path):
    stem = Path(path).stem.lower()
    for suffix, role in ROLE_SUFFIXES:
        if stem.endswith(suffix):
            return role
    return None


def file_hashes(path, chunk_size=1024 * 1024):
    md5 = hashlib.md5()
    sha256 = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            md5.update(chunk)
            sha256.update(chunk)
    return md5.hexdigest(), sha256.hexdigest()


def canonical_texture_entries(files, destination="/Game/Textures"):
    """Return de-duplicated canonical T_ TGA entries with content hashes."""
    destination = "/" + str(destination or "/Game/Textures").strip("/")
    entries = []
    seen = set()
    seen_assets = {}
    for value in files or []:
        path = Path(value)
        role = texture_role(path)
        key = os.path.normcase(os.path.abspath(str(path)))
        if key in seen:
            continue
        seen.add(key)
        if path.suffix.lower() != ".tga" or not path.stem.lower().startswith("t_"):
            continue
        if role is None or not path.is_file() or path.stat().st_size <= 0:
            continue
        md5, sha256 = file_hashes(path)
        asset_name = "T_" + path.stem[2:]
        entry = {
            "source": str(path.resolve()),
            "asset_name": asset_name,
            "asset_path": f"{destination}/{asset_name}",
            "role": role,
            "size": path.stat().st_size,
            "md5": md5,
            "sha256": sha256,
            "policy_version": POLICY_VERSION,
        }
        asset_key = entry["asset_path"].casefold()
        previous = seen_assets.get(asset_key)
        if previous is not None:
            if previous["md5"] != entry["md5"]:
                raise RuntimeError(
                    "conflicting sources target the same Unreal texture: "
                    f"{previous['source']} != {entry['source']} -> {entry['asset_path']}")
            continue
        seen_assets[asset_key] = entry
        entries.append(entry)
    return sorted(entries, key=lambda item: item["asset_path"].casefold())


def unreal_payload(entries, output_path, dry_run=False):
    """Build the self-contained Python payload executed inside Unreal Editor."""
    code = r'''
import json
import traceback
import unreal

ENTRIES = __ENTRIES__
OUTPUT_PATH = __OUTPUT_PATH__
DRY_RUN = __DRY_RUN__


def state_value(state, name, default=False):
    if state is None:
        return default
    try:
        return state.get_editor_property(name)
    except Exception:
        return getattr(state, name, default)


def query_state(path):
    try:
        return unreal.SourceControl.query_file_state(path, True)
    except Exception:
        return None


def imported_md5(asset_path):
    try:
        data = unreal.EditorAssetLibrary.find_asset_data(asset_path)
    except Exception:
        return None
    for tag_name in ("AssetImportData", "SourceFile"):
        try:
            raw = data.get_tag_value(tag_name)
        except Exception:
            raw = None
        if not raw:
            continue
        try:
            parsed = json.loads(str(raw))
        except Exception:
            continue
        rows = parsed if isinstance(parsed, list) else parsed.get("SourceFiles", [])
        for row in rows or []:
            value = row.get("FileMD5") if isinstance(row, dict) else None
            if value:
                return str(value).lower()
    return None


def desired_settings(role):
    compression = {
        "normal": unreal.TextureCompressionSettings.TC_NORMALMAP,
        "extra": unreal.TextureCompressionSettings.TC_MASKS,
        "height": unreal.TextureCompressionSettings.TC_GRAYSCALE,
        "opacity": unreal.TextureCompressionSettings.TC_GRAYSCALE,
        "color": unreal.TextureCompressionSettings.TC_DEFAULT,
        "subsurface": unreal.TextureCompressionSettings.TC_DEFAULT,
    }[role]
    return {
        "srgb": role in ("color", "subsurface"),
        "compression_settings": compression,
        # Virtual Texture Streaming owns residency; keep the imported source
        # resolution unrestricted instead of applying a per-role hard cap.
        "max_texture_size": 0,
        "virtual_texture_streaming": True,
    }


def settings_diff(texture, role):
    differences = []
    for name, wanted in desired_settings(role).items():
        try:
            current = texture.get_editor_property(name)
        except Exception:
            continue
        if current != wanted:
            differences.append(name)
    return differences


def apply_settings(texture, role):
    changed = []
    for name, wanted in desired_settings(role).items():
        try:
            current = texture.get_editor_property(name)
        except Exception:
            continue
        if current == wanted:
            continue
        if name == "virtual_texture_streaming" and hasattr(texture, "set_virtual_texture_streaming"):
            texture.set_virtual_texture_streaming(bool(wanted))
        else:
            texture.set_editor_property(name, wanted)
        changed.append(name)
    return changed


def import_texture(entry, replace_existing):
    task = unreal.AssetImportTask()
    task.set_editor_property("filename", entry["source"])
    task.set_editor_property("destination_path", entry["asset_path"].rsplit("/", 1)[0])
    task.set_editor_property("destination_name", entry["asset_name"])
    task.set_editor_property("automated", True)
    task.set_editor_property("replace_existing", bool(replace_existing))
    try:
        task.set_editor_property("replace_existing_settings", False)
    except Exception:
        pass
    task.set_editor_property("save", False)
    unreal.AssetToolsHelpers.get_asset_tools().import_asset_tasks([task])
    texture = unreal.EditorAssetLibrary.load_asset(entry["asset_path"])
    if texture is None:
        raise RuntimeError("import returned no texture asset")
    return texture


def main():
    report = {
        "dry_run": DRY_RUN,
        "entries": [],
        "errors": [],
        "owned_checkouts": [],
        "revert_unchanged_requested": [],
        "source_control": {
            "enabled": bool(unreal.SourceControl.is_enabled()),
            "available": bool(unreal.SourceControl.is_available()),
            "provider": str(unreal.SourceControl.current_provider()),
        },
    }
    source_control_ready = (
        report["source_control"]["enabled"]
        and report["source_control"]["available"]
    )
    owned_checkouts = []
    for entry in ENTRIES:
        item = {
            "source": entry["source"],
            "asset_path": entry["asset_path"],
            "role": entry["role"],
            "source_md5": entry["md5"],
        }
        report["entries"].append(item)
        try:
            exists = unreal.EditorAssetLibrary.does_asset_exist(entry["asset_path"])
            item["existed"] = bool(exists)
            texture = unreal.EditorAssetLibrary.load_asset(entry["asset_path"]) if exists else None
            previous_md5 = imported_md5(entry["asset_path"]) if exists else None
            differences = settings_diff(texture, entry["role"]) if texture else []
            item["imported_md5_before"] = previous_md5
            item["settings_diff_before"] = differences
            if exists and previous_md5 == entry["md5"].lower() and not differences:
                item["status"] = "unchanged"
                continue
            intended = "create" if not exists else ("configure" if previous_md5 == entry["md5"].lower() else "reimport")
            if DRY_RUN:
                item["status"] = "would_" + intended
                continue
            if not source_control_ready:
                raise RuntimeError("Perforce source-control provider is not available")

            pre_state = query_state(entry["asset_path"])
            preopened = bool(
                state_value(pre_state, "is_checked_out")
                or state_value(pre_state, "is_added")
            )
            item["preopened"] = preopened
            if state_value(pre_state, "is_checked_out_other"):
                raise RuntimeError(
                    "checked out by another user: "
                    + str(state_value(pre_state, "checked_out_other", "unknown")))
            if preopened and state_value(pre_state, "is_modified"):
                raise RuntimeError("pre-existing modified checkout; automatic overwrite skipped")

            if exists and not preopened:
                if not unreal.SourceControl.check_out_file(entry["asset_path"], True):
                    raise RuntimeError(
                        "checkout failed: " + str(unreal.SourceControl.last_error_msg()))
                owned_checkouts.append(entry["asset_path"])
                item["checkout_owned"] = True

            if intended == "configure":
                changed_settings = apply_settings(texture, entry["role"])
            else:
                texture = import_texture(entry, replace_existing=exists)
                changed_settings = apply_settings(texture, entry["role"])
            item["settings_changed"] = changed_settings
            if not unreal.EditorAssetLibrary.save_asset(
                    entry["asset_path"], only_if_is_dirty=True):
                raise RuntimeError("save failed")

            if not exists:
                added_state = query_state(entry["asset_path"])
                if not state_value(added_state, "is_added"):
                    if not unreal.SourceControl.mark_file_for_add(entry["asset_path"], True):
                        raise RuntimeError(
                            "mark for add failed: " + str(unreal.SourceControl.last_error_msg()))
                item["marked_for_add"] = True

            item["imported_md5_after"] = imported_md5(entry["asset_path"])
            item["status"] = (
                "created" if not exists
                else {"configure": "configured", "reimport": "reimported"}[intended]
            )
        except Exception as exc:
            item["status"] = "error"
            item["error"] = str(exc)
            item["traceback"] = traceback.format_exc()
            report["errors"].append(entry["asset_path"] + ": " + str(exc))

    report["owned_checkouts"] = list(owned_checkouts)
    if owned_checkouts and not DRY_RUN:
        try:
            unreal.SourceControl.revert_unchanged_files(owned_checkouts, True)
            report["revert_unchanged_requested"] = list(owned_checkouts)
        except Exception as exc:
            report["errors"].append("revert unchanged failed: " + str(exc))
    counts = {}
    for item in report["entries"]:
        status = item.get("status", "unknown")
        counts[status] = counts.get(status, 0) + 1
    report["counts"] = counts
    with open(OUTPUT_PATH, "w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2, ensure_ascii=False)


try:
    main()
except Exception as exc:
    with open(OUTPUT_PATH, "w", encoding="utf-8") as handle:
        json.dump({"entries": [], "errors": [str(exc)], "traceback": traceback.format_exc()}, handle, indent=2, ensure_ascii=False)
'''
    return (code.replace("__ENTRIES__", repr(list(entries)))
            .replace("__OUTPUT_PATH__", repr(str(output_path)))
            .replace("__DRY_RUN__", repr(bool(dry_run))))


def _editor_is_running():
    if os.name != "nt":
        return False
    try:
        result = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq UnrealEditor.exe", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            timeout=10,
            creationflags=0x08000000,
        )
    except Exception:
        return False
    return '"unrealeditor.exe"' in (result.stdout or "").lower()


def _node_for_project(nodes, project_path):
    if not nodes:
        return None
    project = Path(project_path)
    needles = {
        os.path.normcase(os.path.abspath(str(project))),
        project.name.casefold(),
        project.stem.casefold(),
    }
    for node in nodes:
        haystack = json.dumps(node, ensure_ascii=False).replace("/", os.sep)
        haystack_folded = os.path.normcase(haystack)
        if any(needle and needle in haystack_folded for needle in needles):
            return node
    return nodes[0] if len(nodes) == 1 else None


def _run_remote(script_path, project_path, discovery_timeout=10):
    engine_python = (
        r"C:\Program Files\Epic Games\UE_5.8\Engine\Plugins\Experimental"
        r"\PythonScriptPlugin\Content\Python"
    )
    if engine_python not in sys.path:
        sys.path.insert(0, engine_python)
    import remote_execution

    remote = remote_execution.RemoteExecution()
    remote.start()
    try:
        deadline = time.time() + discovery_timeout
        node = None
        while time.time() < deadline and node is None:
            node = _node_for_project(remote.remote_nodes, project_path)
            if node is None:
                time.sleep(0.2)
        if node is None:
            raise UnrealTextureSyncDeferred(
                "MyProject2 Unreal Python remote node를 찾지 못했습니다")
        node_id = node.get("node_id") if isinstance(node, dict) else node.node_id
        remote.open_command_connection(node_id)
        command = Path(script_path).read_text(encoding="utf-8")
        result = remote.run_command(
            command, unattended=True, exec_mode=remote_execution.MODE_EXEC_FILE)
        if not result.get("success"):
            raise RuntimeError("Unreal remote command failed: {}".format(result))
        return result
    finally:
        remote.stop()


def _run_commandlet(script_path, cfg):
    editor_cmd = cfg.get("unreal_editor_cmd") or (
        r"C:\Program Files\Epic Games\UE_5.8\Engine\Binaries\Win64"
        r"\UnrealEditor-Cmd.exe"
    )
    project = Path(cfg["unreal_project"]) / "MyProject2.uproject"
    command = [
        str(editor_cmd), str(project),
        "-ExecutePythonScript={}".format(script_path),
        "-unattended", "-nosplash", "-nullrhi", "-DDC-ForceMemoryCache",
        "-stdout", "-FullStdOutLogOutput", "-log",
    ]
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=int(cfg.get("unreal_texture_sync_timeout", 1800)),
        creationflags=0x08000000 if os.name == "nt" else 0,
    )
    if result.returncode != 0:
        tail = (result.stderr or result.stdout or "")[-4000:]
        raise RuntimeError("Unreal headless texture sync failed: " + tail)
    return result


def sync_texture_files(files, cfg=None, dry_run=False):
    """Synchronize one batch and return the Unreal-side JSON report."""
    cfg = dict(cfg or load_config())
    if not cfg.get("unreal_texture_sync_enabled", True):
        return {"mode": "disabled", "entries": [], "counts": {"disabled": 1}, "errors": []}
    entries = canonical_texture_entries(
        files, destination=cfg.get("unreal_texture_destination", "/Game/Textures"))
    if not entries:
        return {"mode": "no_candidates", "entries": [], "counts": {}, "errors": []}

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    report_path = REPORT_DIR / f"unreal_texture_sync_{stamp}.json"
    with tempfile.NamedTemporaryFile(
            "w", suffix="_unreal_texture_sync.py", delete=False,
            encoding="utf-8", dir=str(REPORT_DIR)) as handle:
        handle.write(unreal_payload(entries, report_path, dry_run=dry_run))
        script_path = Path(handle.name)
    mode = None
    try:
        if _editor_is_running():
            mode = "remote_editor"
            _run_remote(script_path, cfg["unreal_project"])
        elif cfg.get("unreal_texture_commandlet_fallback", True):
            mode = "headless_commandlet"
            _run_commandlet(script_path, cfg)
        else:
            raise UnrealTextureSyncDeferred(
                "Unreal Editor가 꺼져 있고 headless 동기화가 비활성화되어 있습니다")
    finally:
        try:
            script_path.unlink()
        except OSError:
            pass
    if not report_path.is_file():
        raise RuntimeError("Unreal texture sync produced no report")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["mode"] = mode
    report["report_path"] = str(report_path)
    return report


__all__ = [
    "POLICY_VERSION",
    "UnrealTextureSyncDeferred",
    "canonical_texture_entries",
    "file_hashes",
    "sync_texture_files",
    "texture_role",
    "unreal_payload",
]
