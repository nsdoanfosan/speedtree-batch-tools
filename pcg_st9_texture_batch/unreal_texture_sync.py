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
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent.parent
if str(REPO_DIR) not in sys.path:
    sys.path.insert(0, str(REPO_DIR))

from process_lifecycle import owned_run

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from pcg_texture_common import REPORT_DIR, load_config


POLICY_VERSION = 1
SYNC_STATE_VERSION = 2
SYNC_STATE_FILENAME = "unreal_texture_sync_state.json"
SUCCESS_STATUSES = frozenset(("unchanged", "configured", "reimported", "created"))
UNREAL_OBJECT_NAME_RE = re.compile(r"^[A-Za-z0-9_]+$")
_STATE_LOCK = threading.RLock()
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


def _source_key(path):
    if path is None or not str(path).strip():
        return ""
    return os.path.normcase(os.path.abspath(str(path)))


def _sync_state_path():
    return Path(REPORT_DIR) / SYNC_STATE_FILENAME


def _empty_sync_state():
    return {
        "version": SYNC_STATE_VERSION,
        "updated_at": None,
        "migration_complete": False,
        "entries": {},
    }


def _atomic_write_json(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
                "w", suffix=".tmp", prefix=path.name + ".",
                delete=False, encoding="utf-8", dir=str(path.parent)) as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
            temporary = Path(handle.name)
        os.replace(str(temporary), str(path))
    finally:
        if temporary is not None and temporary.exists():
            try:
                temporary.unlink()
            except OSError:
                pass


def _receipt_from_report_entry(entry, report_path=None, mode=None):
    source = Path(entry.get("source") or "")
    asset_path = entry.get("asset_path")
    source_md5 = str(entry.get("source_md5") or "").lower()
    imported_md5 = str(
        entry.get("imported_md5_after")
        or (
            entry.get("imported_md5_before")
            if entry.get("status") == "unchanged" else ""
        )
        or ""
    ).lower()
    if (
        not source.is_file()
        or not asset_path
        or not source_md5
        or imported_md5 != source_md5
    ):
        return None
    before = source.stat()
    if before.st_size <= 0:
        return None
    md5, sha256 = file_hashes(source)
    after = source.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        return None
    if md5.lower() != source_md5:
        return None
    return {
        "source": str(source.resolve()),
        "size": after.st_size,
        "mtime_ns": after.st_mtime_ns,
        "md5": md5,
        "sha256": sha256,
        # Reports written before policy receipts existed used policy v1.
        "policy_version": entry.get("policy_version", 1),
        "status": entry.get("status"),
        "asset_path": str(asset_path),
        "synced_at": datetime.now().isoformat(timespec="seconds"),
        "report_path": str(report_path) if report_path else None,
        "mode": mode,
    }


def _migrate_existing_reports():
    state = _empty_sync_state()
    report_dir = Path(REPORT_DIR)
    if not report_dir.is_dir():
        state["migration_complete"] = True
        return state
    reports = sorted(
        report_dir.glob("unreal_texture_sync_*.json"),
        key=lambda path: path.name,
        reverse=True,
    )
    # Newest successful evidence wins.  A receipt is migrated only when the
    # current source bytes still match the MD5 recorded by Unreal.
    seen = set()
    for report_path in reports:
        if report_path.name == SYNC_STATE_FILENAME:
            continue
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if report.get("dry_run") is True:
            continue
        mode = report.get("mode")
        for entry in report.get("entries") or []:
            key = _source_key(entry.get("source") or "")
            if not key or key in seen:
                continue
            # The newest report mentioning a source is authoritative even when
            # it failed. Never resurrect an older success receipt after a newer
            # error or incomplete attempt.
            seen.add(key)
            if entry.get("status") not in SUCCESS_STATUSES:
                continue
            try:
                receipt = _receipt_from_report_entry(
                    entry, report_path=report_path, mode=mode)
            except OSError:
                receipt = None
            if receipt is not None:
                state["entries"][key] = receipt
    state["migration_complete"] = True
    state["updated_at"] = datetime.now().isoformat(timespec="seconds")
    return state


def load_sync_state(migrate=True):
    """Load verified Unreal texture receipts, migrating old reports once."""
    path = _sync_state_path()
    with _STATE_LOCK:
        if path.is_file():
            try:
                state = json.loads(path.read_text(encoding="utf-8"))
                if state.get("version") == SYNC_STATE_VERSION \
                        and isinstance(state.get("entries"), dict):
                    return state
            except Exception:
                pass
        if not migrate:
            return _empty_sync_state()
        state = _migrate_existing_reports()
        _atomic_write_json(path, state)
        return state


def _record_sync_receipts(report, canonical_entries, report_path=None):
    """Persist successful per-source evidence after one Unreal sync run."""
    canonical = {
        _source_key(entry["source"]): entry for entry in canonical_entries
    }
    with _STATE_LOCK:
        # Recording the current run must stay fast.  Legacy-report migration
        # remains available through load_sync_state(migrate=True), but is not
        # forced while the user is waiting for a sync to finish.
        state = load_sync_state(migrate=False)
        receipts = state.setdefault("entries", {})
        # Every requested source must earn a fresh success receipt in this
        # report.  If Unreal omits one result, an older receipt must not make
        # that texture look current.
        for key in canonical:
            receipts.pop(key, None)
        for result in report.get("entries") or []:
            key = _source_key(result.get("source") or "")
            expected = canonical.get(key)
            if expected is None:
                continue
            if result.get("status") not in SUCCESS_STATUSES:
                continue
            try:
                stat = Path(expected["source"]).stat()
            except OSError:
                continue
            verified = (
                stat.st_size == expected.get("size")
                and stat.st_mtime_ns == expected.get("mtime_ns")
                and str(result.get("source_md5") or "").lower()
                == str(expected.get("md5") or "").lower()
                and str(
                    result.get("imported_md5_after")
                    or (
                        result.get("imported_md5_before")
                        if result.get("status") == "unchanged" else ""
                    )
                    or ""
                ).lower() == str(expected.get("md5") or "").lower()
                and result.get("asset_path") == expected.get("asset_path")
            )
            if not verified:
                continue
            receipts[key] = {
                "source": expected["source"],
                "size": expected["size"],
                "mtime_ns": expected["mtime_ns"],
                "md5": expected["md5"],
                "sha256": expected["sha256"],
                "policy_version": expected.get("policy_version", POLICY_VERSION),
                "status": result["status"],
                "asset_path": result["asset_path"],
                "synced_at": datetime.now().isoformat(timespec="seconds"),
                "report_path": str(report_path) if report_path else None,
                "mode": report.get("mode"),
            }
        state["version"] = SYNC_STATE_VERSION
        state["migration_complete"] = True
        state["updated_at"] = datetime.now().isoformat(timespec="seconds")
        _atomic_write_json(_sync_state_path(), state)
    return state


def validate_unreal_texture_name(asset_name):
    if not UNREAL_OBJECT_NAME_RE.fullmatch(str(asset_name or "")):
        raise ValueError(
            "Unreal texture name contains invalid characters: "
            f"{asset_name!r}. Rename the SpeedTree material before sync.")
    return str(asset_name)


def _asset_path_for_source(path, destination="/Game/Textures"):
    path = Path(path)
    if path.suffix.lower() != ".tga" or not path.stem.lower().startswith("t_"):
        return None
    if texture_role(path) is None:
        return None
    asset_name = validate_unreal_texture_name("T_" + path.stem[2:])
    destination = "/" + str(destination or "/Game/Textures").strip("/")
    return f"{destination}/{asset_name}"


def is_texture_synced(path, state=None, destination="/Game/Textures"):
    """Return True only while the current file matches a successful receipt."""
    path = Path(path)
    state = state if state is not None else load_sync_state()
    receipt = (state.get("entries") or {}).get(_source_key(path))
    if not receipt or receipt.get("status") not in SUCCESS_STATUSES:
        return False
    try:
        asset_path = _asset_path_for_source(path, destination=destination)
        stat = path.stat()
    except (OSError, ValueError):
        return False
    if not asset_path or stat.st_size <= 0:
        return False
    receipt_md5 = receipt.get("md5")
    receipt_sha256 = receipt.get("sha256")
    if (
        receipt.get("asset_path") != asset_path
        or receipt.get("policy_version", 1) != POLICY_VERSION
        or not receipt_md5
        or not receipt_sha256
    ):
        return False
    if (
        receipt.get("size") == stat.st_size
        and receipt.get("mtime_ns") == stat.st_mtime_ns
    ):
        return True

    # File timestamps can change when OneDrive or another copy operation
    # restores identical bytes. In that case the receipt hashes, rather than
    # mutable filesystem metadata, are the authoritative sync signature.
    try:
        md5, sha256 = file_hashes(path)
        after = path.stat()
    except OSError:
        return False
    if (stat.st_size, stat.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        return False
    return bool(
        md5.lower() == str(receipt_md5).lower()
        and sha256.lower() == str(receipt_sha256).lower()
    )


def _emit_progress(progress, phase, current, total, message):
    if progress is not None:
        progress({
            "phase": phase,
            "current": current,
            "total": total,
            "message": message,
        })


def canonical_texture_entries(files, destination="/Game/Textures", progress=None):
    """Return de-duplicated canonical T_ TGA entries with content hashes."""
    destination = "/" + str(destination or "/Game/Textures").strip("/")
    entries = []
    candidates = []
    seen = set()
    seen_assets = {}
    for value in files or []:
        path = Path(value)
        role = texture_role(path)
        key = _source_key(path)
        if key in seen:
            continue
        seen.add(key)
        if path.suffix.lower() != ".tga" or not path.stem.lower().startswith("t_"):
            continue
        if role is None or not path.is_file() or path.stat().st_size <= 0:
            continue
        asset_name = validate_unreal_texture_name("T_" + path.stem[2:])
        candidates.append((path, role, asset_name))

    total = len(candidates)
    for index, (path, role, asset_name) in enumerate(candidates, 1):
        _emit_progress(
            progress, "hashing", index, total,
            f"로컬 텍스처 해시 {index}/{total}: {path.name}",
        )
        before = path.stat()
        md5, sha256 = file_hashes(path)
        after = path.stat()
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise RuntimeError(f"source changed while hashing: {path}")
        entry = {
            "source": str(path.resolve()),
            "asset_name": asset_name,
            "asset_path": f"{destination}/{asset_name}",
            "role": role,
            "size": after.st_size,
            "mtime_ns": after.st_mtime_ns,
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
            "policy_version": entry["policy_version"],
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
                item["imported_md5_after"] = previous_md5
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
            if item["imported_md5_after"] != entry["md5"].lower():
                raise RuntimeError(
                    "imported source MD5 mismatch after save: "
                    f"expected {entry['md5'].lower()}, "
                    f"got {item['imported_md5_after'] or 'none'}"
                )
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
        result = owned_run(
            ["tasklist", "/FI", "IMAGENAME eq UnrealEditor.exe", "/FO", "CSV", "/NH"],
            source="pcg_st9_texture_batch.unreal_texture_sync.tasklist_observation",
            run_factory=subprocess.run,
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


def _run_remote(
        script_path, project_path, discovery_timeout=10, command_timeout=1800):
    engine_python = (
        r"C:\Program Files\Epic Games\UE_5.8\Engine\Plugins\Experimental"
        r"\PythonScriptPlugin\Content\Python"
    )
    if engine_python not in sys.path:
        sys.path.insert(0, engine_python)
    import remote_execution

    try:
        command_timeout = float(command_timeout)
    except (TypeError, ValueError) as exc:
        raise ValueError("Unreal remote command timeout must be a number") from exc
    if command_timeout <= 0:
        raise ValueError("Unreal remote command timeout must be greater than zero")

    remote = remote_execution.RemoteExecution()
    run_error = None
    try:
        remote.start()
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
        command_connection = getattr(remote, "_command_connection", None)
        command_socket = getattr(
            command_connection, "_command_channel_socket", None)
        if command_socket is None or not hasattr(command_socket, "settimeout"):
            raise UnrealTextureSyncDeferred(
                "Unreal 원격 명령 응답 제한시간을 설정할 수 없습니다. "
                "Unreal Editor 상태를 확인한 뒤 다시 동기화하세요")
        command_socket.settimeout(command_timeout)
        command = Path(script_path).read_text(encoding="utf-8")
        try:
            result = remote.run_command(
                command, unattended=True,
                exec_mode=remote_execution.MODE_EXEC_FILE)
        except (socket.timeout, TimeoutError) as exc:
            raise UnrealTextureSyncDeferred(
                "Unreal 원격 텍스처 동기화가 "
                "{:g}초 동안 응답하지 않았습니다. Unreal Editor 상태를 "
                "확인한 뒤 다시 동기화하세요".format(command_timeout)
            ) from exc
        if not result.get("success"):
            raise RuntimeError("Unreal remote command failed: {}".format(result))
        return result
    except BaseException as exc:
        run_error = exc
        raise
    finally:
        try:
            remote.stop()
        except Exception:
            # Preserve the actionable command/start error if cleanup also
            # fails. On a successful run, a cleanup failure remains visible.
            if run_error is None:
                raise


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
    result = owned_run(
        command,
        source="pcg_st9_texture_batch.unreal_texture_sync.commandlet",
        run_factory=subprocess.run,
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


def sync_texture_files(files, cfg=None, dry_run=False, progress=None):
    """Synchronize one batch and return the Unreal-side JSON report."""
    started = time.time()
    cfg = dict(cfg or load_config())
    if not cfg.get("unreal_texture_sync_enabled", True):
        return {"mode": "disabled", "entries": [], "counts": {"disabled": 1}, "errors": []}
    entries = canonical_texture_entries(
        files,
        destination=cfg.get("unreal_texture_destination", "/Game/Textures"),
        progress=progress,
    )
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
            _emit_progress(
                progress, "unreal", 0, len(entries),
                f"Unreal에서 텍스처 {len(entries)}장 확인 중...",
            )
            _run_remote(
                script_path,
                cfg["unreal_project"],
                command_timeout=int(
                    cfg.get("unreal_texture_sync_timeout", 1800)),
            )
        elif cfg.get("unreal_texture_commandlet_fallback", True):
            mode = "headless_commandlet"
            _emit_progress(
                progress, "unreal", 0, len(entries),
                f"Unreal 헤드리스에서 텍스처 {len(entries)}장 확인 중...",
            )
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
    report["duration_seconds"] = round(time.time() - started, 3)
    if not dry_run:
        state = _record_sync_receipts(
            report, entries, report_path=report_path)
        report["receipt_state_path"] = str(_sync_state_path())
        report["receipt_count"] = len(state.get("entries") or {})
    _atomic_write_json(report_path, report)
    return report


__all__ = [
    "POLICY_VERSION",
    "SUCCESS_STATUSES",
    "SYNC_STATE_FILENAME",
    "SYNC_STATE_VERSION",
    "UnrealTextureSyncDeferred",
    "canonical_texture_entries",
    "file_hashes",
    "is_texture_synced",
    "load_sync_state",
    "sync_texture_files",
    "texture_role",
    "unreal_payload",
    "validate_unreal_texture_name",
]
