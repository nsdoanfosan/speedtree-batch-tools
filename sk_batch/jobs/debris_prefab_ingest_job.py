"""Native import, strict proof, and final publication in one guarded editor stage."""
from __future__ import annotations

import importlib.util
import io
import json
import marshal
import os
from pathlib import Path
import subprocess
import sys
import traceback


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _p4(*args, form=None):
    result = subprocess.run(["p4", "-c", "UnrealProjects", "-G", *args],
                            input=marshal.dumps(form, 0) if form is not None else None,
                            capture_output=True, check=False)
    stream = io.BytesIO(result.stdout)
    rows = []
    while stream.tell() < len(result.stdout):
        rows.append({(k.decode("utf-8", "replace") if isinstance(k, bytes) else k):
                     (v.decode("utf-8", "replace") if isinstance(v, bytes) else v)
                     for k, v in marshal.load(stream).items()})
    return rows


def _checkout_scope(project, asset_paths, changelist):
    files = [project.parent / "Content" / (path.split(".", 1)[0][6:] + ".uasset") for path in asset_paths]
    if not files:
        raise RuntimeError("Prefab publication has no declared asset scope")
    mappings = _p4("where", *(str(path) for path in files))
    expected_files = {str(path.resolve()).casefold() for path in files}
    mapped_files = [str(Path(row['path']).resolve()).casefold() for row in mappings
                    if row.get('path') and not row.get('unmap')]
    if len(mapped_files) != len(files) or set(mapped_files) != expected_files:
        raise RuntimeError("Prefab asset scope is not mapped by UnrealProjects")
    records = _p4("fstat", *(str(path) for path in files))
    opened = [row for row in records if row.get("action")]
    conflicts = [row for row in opened if str(row.get("change")) != str(changelist)]
    if conflicts:
        raise RuntimeError("Existing changelist conflict; preserve and review: " + json.dumps(
            [{key: row.get(key) for key in ("clientFile", "change", "action")} for row in conflicts]))
    if changelist is None:
        form = {b"Change": b"new", b"Client": b"UnrealProjects",
                b"Description": b"SpeedTree terrain group prefabs: explicit per-SPM activation, verified derivatives.\n"}
        created = _p4("change", "-i", form=form)
        import re
        text = " ".join(str(row.get("data", "")) for row in created)
        match = re.search(r"Change (\d+) created", text)
        if not match:
            raise RuntimeError("Could not create the prefab numbered changelist: " + text)
        changelist = int(match.group(1))
    if not isinstance(changelist, int) or isinstance(changelist, bool) or changelist <= 0:
        raise RuntimeError("A positive numbered changelist is required")
    forms = _p4("change", "-o", str(changelist))
    if not any(row.get('Status') == 'pending' and row.get('Client') == 'UnrealProjects' for row in forms):
        raise RuntimeError("Prefab changelist must be pending in UnrealProjects")
    tracked = {str(Path(row["clientFile"]).resolve()).casefold() for row in records if row.get("clientFile") and row.get("headRev")}
    edit_files = [str(path) for path in files if str(path.resolve()).casefold() in tracked and
                  not any(str(Path(row.get("clientFile", "")).resolve()).casefold() == str(path.resolve()).casefold() for row in opened)]
    if edit_files:
        rows = _p4("edit", "-c", str(changelist), *edit_files)
        if any(row.get("code") == "error" for row in rows):
            raise RuntimeError("Scoped Perforce checkout failed: " + json.dumps(rows))
    return changelist, list(asset_paths), files


def run(request_path):
    import unreal as u
    request = json.loads(Path(request_path).read_text(encoding="utf-8"))
    project = Path(request["project"])
    repo = Path(request["repo"])
    paths = {key: Path(value) for key, value in request["paths"].items()}
    sys.path.insert(0, str(repo))
    sys.path.insert(0, str(repo / "sk_batch"))
    editor = _load("debris_editor_pipeline", project.parent / "Scripts/PCG/debris_prefab_pipeline.py")
    receipt = {"schema_version": 1, "status": "failed", "request": str(request_path),
               "source_wind_modified": False, "map_saved": False, "pcg_generated": False,
               "activation_policy": "explicit_per_spm_selection"}
    guard = None
    graph = None
    try:
        preview = editor.preflight(str(paths["payload"]), str(paths["preflight"]))
        cl, checked, files = _checkout_scope(project, preview["checkout_asset_paths"], request.get("changelist"))
        receipt["changelist"] = cl
        graph = u.load_asset('/Game/PCG/PCG_01')
        guard = json.loads(u.CodexDebrisPrefabEditorLibrary.begin_native_authoring_guard(graph))
        if not guard.get("success"):
            raise RuntimeError("Cannot guard PCG publication: " + json.dumps(guard))
        ingest = _load("debris_static_ingest", repo / "sk_batch/unreal_ingest.py")
        result = ingest.run_manifest(str(paths["manifest"]), str(paths["checkpoint"]), str(paths["ingest"]))
        if not result.get("items") or any(row.get("status") != "imported_ok" for row in result["items"].values()):
            raise RuntimeError("Static group ingest did not complete")
        receipt["ingest"] = {"status": "imported_ok", "items": len(result["items"])}
        editor.dump_geometry(str(paths["payload"]), request["native_dir"])
        expected = Path(request["expected_dir"]) / "expected_group_geometry.json"
        actual = Path(request["native_dir"]) / "import_geometry.json"
        command = [request["verification_python"], "-X", "utf8",
                   str(project.parent / "Scripts/PCGTests/verify_debris_group_import.py"),
                   "--expected-header", str(expected), "--actual-header", str(actual),
                   "--output", str(paths["geometry_comparison"])]
        proof_process = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False)
        if proof_process.returncode:
            raise RuntimeError("Independent SOURCE_MODEL geometry verification failed: " + proof_process.stdout[-2000:])
        from sk_batch.debris_prefab_manifest import finalize_import_verified_manifest
        payload = json.loads(paths["payload"].read_text(encoding="utf-8"))
        proof = json.loads(paths["geometry_comparison"].read_text(encoding="utf-8"))
        verified = finalize_import_verified_manifest(payload, proof, expected_geometry_header_bytes=expected.read_bytes(),
                    actual_geometry_header_bytes=actual.read_bytes(), material_bindings=preview["material_bindings"])
        paths["verified"].write_text(json.dumps(verified, ensure_ascii=False, indent=2), encoding="utf-8")
        finalized = editor.finalize(str(paths["verified"]), str(paths["preflight"]), str(paths["authoring"]),
                     geometry_receipt_path=str(paths["geometry_comparison"]),
                     checked_out_asset_paths=checked, changelist=cl, guard_token=guard["token"])
        receipt.update(finalized)
        if finalized.get('status') != 'ok' or finalized.get('success') is not True:
            raise RuntimeError("Verified prefab publication did not complete: " + str(finalized.get('error')))
        # New derivatives are registered only after actual native saves succeed.
        unknown = [str(path) for path in files if path.is_file() and not any(
                   row.get("clientFile") and str(Path(row["clientFile"]).resolve()).casefold() == str(path.resolve()).casefold()
                   for row in _p4("fstat", str(path)))]
        if unknown:
            added = _p4("add", "-c", str(cl), *unknown)
            if any(row.get("code") == "error" for row in added):
                raise RuntimeError("Generated prefab asset registration failed")
        receipt["status"] = "ok"
    except Exception as exc:
        receipt.update({"status": "failed", "success": False, "error": str(exc), "traceback": traceback.format_exc()})
        raise
    finally:
        if guard and guard.get("success"):
            closed = json.loads(u.CodexDebrisPrefabEditorLibrary.end_native_authoring_guard(graph, guard["token"]))
            receipt["guard_closed"] = closed
            if not closed.get("success"):
                receipt.update({"status": "failed", "success": False, "error": "Native guard could not confirm state preservation"})
        paths["authoring"].write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8")
    return receipt


if __name__ == "__main__":
    run(os.environ["SK_BATCH_DEBRIS_PREFAB_REQUEST"])
