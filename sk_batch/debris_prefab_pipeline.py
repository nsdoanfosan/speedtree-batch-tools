"""Optional, source-preserving terrain prefabs after an ordinary successful Push.

The independent per-SPM table button requests this static derivative. Source
geometry contracts validate the result; original Push receipts stay intact.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
from process_lifecycle import owned_run
from sk_batch.debris_prefab_policy import eligibility
from sk_batch.sk_common import DEFAULT_CONFIG, send2ue_export_cache_root, unreal_remote_execution_settings


class DebrisPrefabPipelineError(RuntimeError):
    def __init__(self, message, report=None):
        super().__init__(message)
        self.report = report or {}


def _write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def _cancelled(event):
    if event is not None and event.is_set():
        raise DebrisPrefabPipelineError("Terrain prefab generation cancelled before native publication")


def source_mesh_from_push(report_path):
    """Resolve actual exported identity, never invent a folder or SK name."""
    report = json.loads(Path(report_path).read_text(encoding="utf-8-sig"))
    if report.get("status") not in {"ok", "imported_ok"}:
        raise DebrisPrefabPipelineError("Terrain prefab requires a successful original Push receipt")
    path = report.get("mesh_path")
    manifest_path = report.get("manifest")
    if manifest_path and Path(manifest_path).is_file():
        manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8-sig"))
        items = manifest.get("items", [])
        if report.get("queue_id"):
            items = [item for item in items if item.get("queue_id") == report["queue_id"]]
            if len(items) != 1:
                raise DebrisPrefabPipelineError("Original item receipt does not identify exactly one manifest item")
        if len(items) == 1:
            path = path or items[0].get("mesh_path")
        candidates = {(row.get("asset_data") or {}).get("asset_path")
                      for item in items for row in item.get("assets", [])
                      if (row.get("asset_data") or {}).get("_asset_type") == "SkeletalMesh"}
        candidates.discard(None)
        if path in candidates:
            return path
        if len(candidates) == 1:
            return candidates.pop()
        raise DebrisPrefabPipelineError("Original export does not identify one source skeletal mesh")
    if isinstance(path, str) and path.startswith("/Game/"):
        return path  # Native preflight verifies the actual class and wind data.
    raise DebrisPrefabPipelineError("Original Push receipt has no exported mesh identity")


def _remote_call(project, editor_cmd, code, timeout=30):
    """One uniquely selected editor, using the project's existing RPC settings."""
    python_dir = editor_cmd.parent.parent.parent / "Plugins/Experimental/PythonScriptPlugin/Content/Python"
    spec = importlib.util.spec_from_file_location("debris_pipeline_remote_execution", python_dir / "remote_execution.py")
    if spec is None or spec.loader is None:
        raise DebrisPrefabPipelineError("Engine remote execution provider is unavailable")
    remote_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(remote_module)
    settings = unreal_remote_execution_settings(project)
    config = remote_module.RemoteExecutionConfig()
    bind = settings.get("multicast_bind_address") or config.multicast_bind_address
    config.multicast_bind_address = bind
    config.multicast_ttl = int(settings.get("multicast_ttl", config.multicast_ttl))
    if settings.get("multicast_group_endpoint"):
        group = settings["multicast_group_endpoint"]
        config.multicast_group_endpoint = tuple(group) if isinstance(group, (list, tuple)) else (group.rsplit(":", 1)[0], int(group.rsplit(":", 1)[1]))
    advertised = bind if bind != "0.0.0.0" else "127.0.0.1"
    with socket.socket() as port_probe:
        port_probe.bind((advertised, 0))
        port = port_probe.getsockname()[1]
    config.command_endpoint = (advertised, port)
    remote = remote_module.RemoteExecution(config)
    remote.start()
    try:
        deadline = time.monotonic() + timeout
        candidates = []
        while time.monotonic() < deadline:
            candidates = [node for node in remote.remote_nodes
                          if str(node.get("project_name", "")).casefold() == project.stem.casefold()]
            if len(candidates) == 1:
                break
            time.sleep(0.2)
        if len(candidates) != 1:
            raise DebrisPrefabPipelineError("Expected exactly one editor for " + project.stem)
        remote.open_command_connection(candidates[0]["node_id"])
        result = remote.run_command(code, unattended=True, exec_mode=remote_module.MODE_EXEC_FILE,
                                    raise_on_failure=False)
        if not result.get("success"):
            raise DebrisPrefabPipelineError("Native terrain prefab stage failed: " + str(result.get("result")))
        return result
    finally:
        remote.stop()


def run_pipeline(spm, *, blender, unreal_project, send2ue_dir=None, material_contract,
                 log_dir=None, transport="rpc", unreal_editor_cmd=None, changelist=None,
                 cancel_event=None, log=None, source_push_report=None, source_skeletal_mesh=None):
    spm = Path(spm).expanduser().resolve()
    decision = eligibility(spm, enabled=True)
    if not decision["eligible"]:
        return {"status": "skipped", "eligibility": decision}
    project = Path(unreal_project).expanduser().resolve()
    project_dir = project.parent
    blender = Path(blender).expanduser().resolve()
    editor_cmd = Path(unreal_editor_cmd or DEFAULT_CONFIG["unreal_editor_cmd"]).expanduser().resolve()
    source_mesh = source_skeletal_mesh or source_mesh_from_push(source_push_report)
    log_dir = Path(log_dir or REPO / "sk_batch/logs").resolve()
    key = hashlib.sha256(str(spm).casefold().encode("utf-8")).hexdigest()[:16]
    folder = log_dir / "debris_prefab" / key
    folder.mkdir(parents=True, exist_ok=True)
    paths = {name: folder / (name + ".json") for name in
             ("pipeline", "build", "push", "manifest", "checkpoint", "ingest", "unit_import", "payload", "request", "preflight", "verified", "geometry_comparison", "authoring")}
    if changelist is None and paths["pipeline"].is_file():
        try:
            previous = json.loads(paths["pipeline"].read_text(encoding="utf-8"))
            candidate = previous.get("changelist")
            if previous.get("kind") == "sk_batch_optional_debris_prefab_pipeline" and previous.get("spm") == str(spm) and type(candidate) is int:
                from sk_batch.jobs.debris_prefab_ingest_job import _p4
                records = _p4("change", "-o", str(candidate))
                if any(row.get("Status") == "pending" and row.get("Client") == "UnrealProjects" for row in records):
                    changelist = candidate
        except (ValueError, OSError, EOFError):
            pass
    report = {"schema_version": 1, "kind": "sk_batch_optional_debris_prefab_pipeline",
              "status": "running", "spm": str(spm), "eligibility": decision,
              "source_skeletal_mesh": source_mesh, "transport": transport,
              "original_push_report": str(source_push_report) if source_push_report else None,
              "original_push_preserved": True, "source_saved": False,
              "report": str(paths["pipeline"])}
    started = time.monotonic()
    try:
        _cancelled(cancel_event)
        if transport not in {"rpc", "headless"}:
            raise DebrisPrefabPipelineError("Unsupported terrain prefab transport")
        required = [blender, project, project_dir / "Scripts/PCG/debris_prefab_pipeline.py",
                    project_dir / "Scripts/PCGTests/dump_debris_group_expected.py",
                    project_dir / "Scripts/PCGTests/verify_debris_group_import.py"]
        if not all(path.is_file() for path in required):
            raise DebrisPrefabPipelineError("Terrain prefab requires the installed project pipeline and Blender providers")
        # Reuse one bounded proof directory. These are this pipeline's generated
        # dumps/checkpoints only; source FBXs and production files are untouched.
        for directory, filenames in ((folder / "expected", ("expected_group_geometry.json", "expected_group_vertices.f64bin")),
                                     (folder / "native", ("import_geometry.json", "imported_vertices.f64.bin", "native_geometry_request.json"))):
            for filename in filenames:
                (directory / filename).unlink(missing_ok=True)
        for name in ("checkpoint", "ingest", "unit_import", "preflight", "verified", "geometry_comparison", "authoring"):
            paths[name].unlink(missing_ok=True)
        export_root = send2ue_export_cache_root() / "debris_terrain" / key
        command = [str(blender), "--background", "--factory-startup", str(spm.with_suffix(".blend")),
                   "--python", str(REPO / "sk_batch/jobs/debris_terrain_group_job.py"), "--",
                   "--source-path", str(spm.with_suffix(".blend")), "--group-report", str(paths["build"]),
                   "--source-skeletal-mesh", source_mesh, "--prefab-manifest", str(paths["payload"]),
                   "--reuse-existing-materials", "--expected-output-dir", str(folder / "expected"),
                   "--expected-dump-script", str(project_dir / "Scripts/PCGTests/dump_debris_group_expected.py"),
                   "--report", str(paths["push"]), "--spm", str(spm), "--material-contract", str(material_contract),
                   "--transport", "headless_export", "--skip-wind", "--export-root", str(export_root),
                   "--queue-id", "debris_prefab:" + key, "--manifest", str(paths["manifest"]),
                   "--checkpoint", str(paths["checkpoint"]), "--batch-report", str(paths["ingest"]),
                   "--item-import-report", str(paths["unit_import"]),
                   "--unreal-ingest", str(REPO / "sk_batch/unreal_ingest.py")]
        report["stage"] = "source_preserving_send2ue_export"
        _write(paths["pipeline"], report)
        if log:
            log("지형 그룹 프리팹: " + spm.stem)
        completed = owned_run(command, source="sk_batch.debris_prefab.blender_export", run_factory=subprocess.run,
                              check=False, capture_output=True, text=True, encoding="utf-8", errors="replace")
        report["blender_returncode"] = completed.returncode
        if completed.returncode:
            raise DebrisPrefabPipelineError("Terrain group export failed; see " + str(paths["build"]))
        _cancelled(cancel_event)
        request = {"project": str(project), "repo": str(REPO), "paths": {k: str(v) for k, v in paths.items()},
                   "expected_dir": str(folder / "expected"), "native_dir": str(folder / "native"),
                   "verification_python": sys.executable, "changelist": changelist}
        _write(paths["request"], request)
        job = REPO / "sk_batch/jobs/debris_prefab_ingest_job.py"
        report["stage"] = "native_import_verify_author_bind"
        _write(paths["pipeline"], report)
        if transport == "rpc":
            _remote_call(project, editor_cmd, "import runpy; runpy.run_path(" + repr(str(job)) + ")[\"run\"](" + repr(str(paths["request"])) + ")")
        else:
            from sk_batch.sk_common import UNREAL_COMMANDLET_BASE_ARGS
            environment = os.environ.copy()
            environment["SK_BATCH_DEBRIS_PREFAB_REQUEST"] = str(paths["request"])
            native = owned_run([str(editor_cmd), str(project), "-run=pythonscript", "-script=" + str(job),
                                *UNREAL_COMMANDLET_BASE_ARGS], source="sk_batch.debris_prefab.native_publish",
                               run_factory=subprocess.run, check=False, env=environment)
            if native.returncode:
                raise DebrisPrefabPipelineError("Native terrain prefab publication failed")
        result = json.loads(paths["authoring"].read_text(encoding="utf-8"))
        if result.get("status") != "ok" or result.get("success") is not True or result.get("guard_closed", {}).get("success") is not True:
            raise DebrisPrefabPipelineError("Native prefab did not complete: " + str(result.get("error")))
        report.update({"status": "ok", "stage": "completed", "authoring": result,
                       "group_count": result.get("group_count"), "changelist": result.get("changelist")})
        return report
    except Exception as exc:
        report.update({"status": "failed", "error": str(exc)})
        if paths["authoring"].is_file():
            try:
                native_failure = json.loads(paths["authoring"].read_text(encoding="utf-8"))
                report["authoring"] = native_failure
                report["changelist"] = native_failure.get("changelist")
            except (OSError, ValueError):
                pass
        raise DebrisPrefabPipelineError(str(exc), report=report) from exc
    finally:
        report["elapsed_seconds"] = time.monotonic() - started
        _write(paths["pipeline"], report)
