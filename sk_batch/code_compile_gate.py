"""Fast, side-effect-free compile gate for the SK Batch pipeline.

This gate intentionally does not import production modules or inspect assets.
It compiles Python source in memory, then checks the few orchestration
contracts whose violation otherwise appears only after a long batch run.
"""

from __future__ import annotations

import ast
import copy
import hashlib
import io
import json
import os
import subprocess
import sys
import time
import tokenize
from dataclasses import dataclass
from pathlib import Path, PurePosixPath


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from process_lifecycle import ProcessLifecycleError, owned_run  # noqa: E402


GUI_PATH = Path(__file__).resolve().with_name("sk_batch_gui.pyw")
PUSH_JOB_PATH = (
    Path(__file__).resolve().parent / "jobs" / "send2ue_push_job.py"
)
SOURCE_SUFFIXES = {".py", ".pyw"}
IGNORED_DIRECTORY_NAMES = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "logs",
    "node_modules",
    "work",
}
IGNORED_REPO_RELATIVE_DIRECTORY_PREFIXES = {
    (".claude", "worktrees"),
}
VCS_ROOT_MARKER_NAMES = {".git", ".hg", ".svn"}
PRODUCTION_SOURCE_MANIFEST_VERSION = 1
CODE_REVISION_RESTART_ROUTE = "code_revision_restart_required"
RUNTIME_COMPILE_NAMES = {
    "_batch_compile_enabled",
    "_compile_blender_wave",
    "_compiled_material_preflight_for",
    "_initialize_compiled_plan",
}


class CompileGateError(RuntimeError):
    """Raised when syntax or an SK Batch orchestration contract is invalid."""

    def __init__(self, message, *, details=None):
        super().__init__(message)
        self.details = copy.deepcopy(details or {})


@dataclass(frozen=True)
class ProductionSourceFile:
    path: str
    size: int
    sha256: str


@dataclass(frozen=True)
class ProductionSourceManifest:
    schema_version: int
    source_count: int
    content_hash: str
    files: tuple

    def as_dict(self):
        return {
            "schema_version": self.schema_version,
            "source_count": self.source_count,
            "content_hash": self.content_hash,
            "files": [
                {
                    "path": record.path,
                    "size": record.size,
                    "sha256": record.sha256,
                }
                for record in self.files
            ],
        }


@dataclass(frozen=True)
class CompileGateResult:
    source_count: int
    contract_count: int
    elapsed_seconds: float
    production_source_manifest: ProductionSourceManifest


def _read_python_source(path: Path) -> str:
    with tokenize.open(path) as handle:
        return handle.read()


def _normalized_relative_parts(path):
    return tuple(part.casefold() for part in path.parts)


def _has_prefix(parts, prefixes):
    return any(
        len(parts) >= len(prefix) and parts[: len(prefix)] == prefix
        for prefix in prefixes
    )


def _git_stdout(repo_root, *arguments):
    try:
        result = owned_run(
            ["git", "-C", str(repo_root), *arguments],
            source="sk_batch.code_compile_gate.git_scope_observation",
            check=False,
            capture_output=True,
            timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError, ProcessLifecycleError):
        return None
    return result.stdout if result.returncode == 0 else None


def _git_ignored_paths(repo_root):
    """Return ignored untracked directories/files relative to a Git root."""
    output = _git_stdout(
        repo_root,
        "ls-files",
        "--full-name",
        "--others",
        "--ignored",
        "--exclude-standard",
        "--directory",
        "-z",
    )
    if output is None:
        return frozenset(), frozenset()

    directories = set()
    files = set()
    for raw_path in output.split(b"\0"):
        if not raw_path:
            continue
        value = os.fsdecode(raw_path).replace("\\", "/")
        relative = PurePosixPath(value.rstrip("/"))
        if (
            relative.is_absolute()
            or not relative.parts
            or any(part in {"", ".", ".."} for part in relative.parts)
        ):
            continue
        parts = _normalized_relative_parts(relative)
        if value.endswith("/"):
            directories.add(parts)
        else:
            files.add(parts)
    return frozenset(directories), frozenset(files)


def _is_nested_vcs_root(path):
    return any((path / marker).exists() for marker in VCS_ROOT_MARKER_NAMES)


def _production_sources(repo_root: Path):
    repo_root = Path(repo_root).resolve()
    ignored_directories, ignored_files = _git_ignored_paths(repo_root)
    structural_prefixes = frozenset(
        tuple(part.casefold() for part in prefix)
        for prefix in IGNORED_REPO_RELATIVE_DIRECTORY_PREFIXES
    )
    for current_root, directories, filenames in os.walk(repo_root):
        current = Path(current_root)
        current_parts = _normalized_relative_parts(
            current.relative_to(repo_root)
        )
        retained_directories = []
        for name in sorted(directories):
            relative_parts = current_parts + (name.casefold(),)
            child = current / name
            if (
                name.casefold() in IGNORED_DIRECTORY_NAMES
                or name.casefold() in VCS_ROOT_MARKER_NAMES
                or _has_prefix(relative_parts, structural_prefixes)
                or _has_prefix(relative_parts, ignored_directories)
                or _is_nested_vcs_root(child)
            ):
                continue
            retained_directories.append(name)
        directories[:] = retained_directories
        for filename in sorted(filenames):
            path = current / filename
            relative_parts = current_parts + (filename.casefold(),)
            if (
                path.suffix.casefold() in SOURCE_SUFFIXES
                and not _has_prefix(relative_parts, structural_prefixes)
                and not _has_prefix(relative_parts, ignored_directories)
                and relative_parts not in ignored_files
            ):
                yield path


def _decode_python_source(payload: bytes, path: Path) -> str:
    try:
        encoding, _ = tokenize.detect_encoding(io.BytesIO(payload).readline)
        return payload.decode(encoding)
    except (SyntaxError, UnicodeDecodeError) as exc:
        raise CompileGateError(
            f"Python source decode failed: {path}: {exc}"
        ) from exc


def _manifest_hash(schema_version, records):
    payload = {
        "schema_version": schema_version,
        "source_count": len(records),
        "files": [
            {
                "path": record.path,
                "size": record.size,
                "sha256": record.sha256,
            }
            for record in records
        ],
    }
    return hashlib.sha256(
        json.dumps(
            payload,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _production_source_snapshot(repo_root):
    repo_root = Path(repo_root).resolve()
    snapshots = []
    records = []
    for path in _production_sources(repo_root):
        try:
            payload = path.read_bytes()
        except OSError as exc:
            raise CompileGateError(
                f"Python source read failed: {path.relative_to(repo_root)}: {exc}"
            ) from exc
        relative = path.relative_to(repo_root).as_posix()
        snapshots.append((path, _decode_python_source(payload, Path(relative))))
        records.append(
            ProductionSourceFile(
                path=relative,
                size=len(payload),
                sha256=hashlib.sha256(payload).hexdigest(),
            )
        )
    if not records:
        raise CompileGateError(f"No Python sources found under {repo_root}")
    records = tuple(records)
    return tuple(snapshots), ProductionSourceManifest(
        schema_version=PRODUCTION_SOURCE_MANIFEST_VERSION,
        source_count=len(records),
        content_hash=_manifest_hash(
            PRODUCTION_SOURCE_MANIFEST_VERSION,
            records,
        ),
        files=records,
    )


def _compile_repository_sources(repo_root=REPO_ROOT):
    repo_root = Path(repo_root).resolve()
    snapshots, manifest = _production_source_snapshot(repo_root)
    for path, source in snapshots:
        try:
            compile(source, str(path), "exec", dont_inherit=True)
        except (SyntaxError, ValueError) as exc:
            raise CompileGateError(
                f"Python compile failed: {path.relative_to(repo_root)}: {exc}"
            ) from exc
    return manifest


def production_source_manifest(repo_root=REPO_ROOT):
    """Return the exact path/content manifest used as the worker revision."""
    return _production_source_snapshot(repo_root)[1]


def compile_repository_sources(repo_root=REPO_ROOT) -> int:
    """Compile the exact byte snapshot represented by the source manifest."""
    return _compile_repository_sources(repo_root).source_count


def _manifest_from_payload(payload, label):
    if isinstance(payload, ProductionSourceManifest):
        return payload
    try:
        schema_version = int(payload["schema_version"])
        source_count = int(payload["source_count"])
        content_hash = str(payload["content_hash"]).casefold()
        records = tuple(
            ProductionSourceFile(
                path=str(row["path"]),
                size=int(row["size"]),
                sha256=str(row["sha256"]).casefold(),
            )
            for row in payload["files"]
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise CompileGateError(f"{label} is malformed") from exc
    if (
        schema_version != PRODUCTION_SOURCE_MANIFEST_VERSION
        or source_count != len(records)
        or not records
        or len({record.path for record in records}) != len(records)
        or any(
            not record.path
            or record.size < 0
            or len(record.sha256) != 64
            or any(
                character not in "0123456789abcdef"
                for character in record.sha256
            )
            for record in records
        )
        or content_hash != _manifest_hash(schema_version, records)
    ):
        raise CompileGateError(f"{label} content hash is invalid")
    return ProductionSourceManifest(
        schema_version=schema_version,
        source_count=source_count,
        content_hash=content_hash,
        files=records,
    )


def _source_file_identity(record):
    if record is None:
        return None
    return {
        "path": record.path,
        "size": record.size,
        "sha256": record.sha256,
    }


def production_source_revision_difference(
    expected,
    actual,
    *,
    label="Production source",
):
    """Describe every exact path changed between two production revisions."""

    expected = _manifest_from_payload(
        expected,
        "Expected production manifest",
    )
    actual = _manifest_from_payload(actual, label)
    expected_files = {record.path: record for record in expected.files}
    actual_files = {record.path: record for record in actual.files}
    changed_paths = []
    for path in sorted(set(expected_files) | set(actual_files)):
        expected_record = expected_files.get(path)
        actual_record = actual_files.get(path)
        if expected_record == actual_record:
            continue
        if expected_record is None:
            status = "added"
        elif actual_record is None:
            status = "removed"
        else:
            status = "modified"
        changed_paths.append({
            "path": path,
            "status": status,
            "expected": _source_file_identity(expected_record),
            "actual": _source_file_identity(actual_record),
        })
    return {
        "route": CODE_REVISION_RESTART_ROUTE,
        "status": "revision_mismatch",
        "label": str(label),
        "expected_revision": expected.content_hash,
        "actual_revision": actual.content_hash,
        "expected": {
            "schema_version": expected.schema_version,
            "source_count": expected.source_count,
            "content_hash": expected.content_hash,
        },
        "actual": {
            "schema_version": actual.schema_version,
            "source_count": actual.source_count,
            "content_hash": actual.content_hash,
        },
        "changed_paths": changed_paths,
    }


def format_production_source_revision_difference(details):
    """Render a complete, non-truncated restart diagnostic."""

    details = details if isinstance(details, dict) else {}
    lines = [
        f"{details.get('label') or 'Production source'} revision mismatch: "
        f"expected {details.get('expected_revision') or '<unknown>'}, "
        f"actual {details.get('actual_revision') or '<unknown>'}",
        "exact changed paths:",
    ]
    changed_paths = details.get("changed_paths") or []
    if not changed_paths:
        lines.append(
            "- <manifest metadata> [metadata_changed] "
            f"expected={json.dumps(details.get('expected'), sort_keys=True)} "
            f"actual={json.dumps(details.get('actual'), sort_keys=True)}"
        )
    for row in changed_paths:
        lines.append(
            f"- {row.get('path')} [{row.get('status')}] "
            "expected="
            + json.dumps(
                row.get("expected"),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + " actual="
            + json.dumps(
                row.get("actual"),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
    return "\n".join(lines)


def validate_production_source_manifest(
    expected,
    actual,
    label="Production source",
):
    expected = _manifest_from_payload(expected, "Expected production manifest")
    actual = _manifest_from_payload(actual, label)
    if expected != actual:
        details = production_source_revision_difference(
            expected,
            actual,
            label=label,
        )
        raise CompileGateError(
            format_production_source_revision_difference(details),
            details=details,
        )
    return actual


def production_source_revision_state(
    expected_content_hash,
    started_manifest,
    finished_manifest=None,
):
    expected = str(expected_content_hash or "").strip().casefold()
    started = _manifest_from_payload(
        started_manifest,
        "Started production manifest",
    )
    finished = _manifest_from_payload(
        finished_manifest or started,
        "Finished production manifest",
    )
    return {
        "manifest_schema_version": PRODUCTION_SOURCE_MANIFEST_VERSION,
        "expected_content_hash": expected,
        "started": started.as_dict(),
        "finished": finished.as_dict(),
        "matches_expected": bool(
            expected
            and started.content_hash == expected
            and finished.content_hash == expected
        ),
        "stable": started == finished,
    }


def validate_production_source_revision_report(report, expected_manifest):
    expected = _manifest_from_payload(
        expected_manifest,
        "Expected production manifest",
    )
    state = (
        report.get("production_source_revision")
        if isinstance(report, dict)
        else None
    )
    if not isinstance(state, dict):
        raise CompileGateError(
            "Child report has no production_source_revision assertion"
        )
    if (
        state.get("manifest_schema_version")
        != PRODUCTION_SOURCE_MANIFEST_VERSION
        or str(state.get("expected_content_hash") or "").casefold()
        != expected.content_hash
    ):
        reported_expected_revision = str(
            state.get("expected_content_hash") or ""
        ).casefold()
        details = {
            "route": CODE_REVISION_RESTART_ROUTE,
            "status": "revision_metadata_mismatch",
            "label": "Child production source metadata",
            "expected_revision": expected.content_hash,
            "actual_revision": reported_expected_revision,
            "expected": {
                "schema_version": expected.schema_version,
                "source_count": expected.source_count,
                "content_hash": expected.content_hash,
            },
            "actual": {
                "schema_version": state.get("manifest_schema_version"),
                "content_hash": reported_expected_revision,
            },
            "changed_paths": [],
        }
        try:
            reported_started = _manifest_from_payload(
                state.get("started"),
                "Child-start production source",
            )
        except CompileGateError:
            reported_started = None
        if reported_started is not None:
            details = production_source_revision_difference(
                expected,
                reported_started,
                label="Child-start production source",
            )
            details["status"] = "revision_metadata_mismatch"
            details["reported_started_revision"] = details[
                "actual_revision"
            ]
            details["reported_expected_revision"] = (
                reported_expected_revision
            )
            if reported_expected_revision != expected.content_hash:
                details["actual_revision"] = reported_expected_revision
        raise CompileGateError(
            format_production_source_revision_difference(details),
            details=details,
        )
    started = validate_production_source_manifest(
        expected,
        state.get("started"),
        label="Child-start production source",
    )
    finished = validate_production_source_manifest(
        expected,
        state.get("finished"),
        label="Child-finish production source",
    )
    if (
        state.get("matches_expected") is not True
        or state.get("stable") is not True
        or started != finished
    ):
        details = production_source_revision_difference(
            expected,
            finished,
            label="Child production source assertion",
        )
        details["status"] = "revision_assertion_mismatch"
        expected_assertions = {
            "matches_expected": True,
            "stable": True,
            "started_equals_finished": True,
        }
        actual_assertions = {
            "matches_expected": state.get("matches_expected"),
            "stable": state.get("stable"),
            "started_equals_finished": started == finished,
        }
        details["expected"]["assertions"] = expected_assertions
        details["actual"]["assertions"] = actual_assertions
        details["assertion_deltas"] = {
            key: {
                "expected": expected_assertions[key],
                "actual": actual_assertions[key],
            }
            for key in expected_assertions
            if expected_assertions[key] != actual_assertions[key]
        }
        raise CompileGateError(
            format_production_source_revision_difference(details),
            details=details,
        )
    return state


def _function(module: ast.Module, name: str) -> ast.FunctionDef:
    for node in module.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise CompileGateError(f"Required function is missing: {name}")


def _app_methods(module: ast.Module):
    for node in module.body:
        if isinstance(node, ast.ClassDef) and node.name == "App":
            return {
                child.name: child
                for child in node.body
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
            }
    raise CompileGateError("Required class is missing: App")


def _call_name(call: ast.Call):
    function = call.func
    if isinstance(function, ast.Name):
        return function.id
    if isinstance(function, ast.Attribute):
        return function.attr
    return None


def _calls(node: ast.AST, name: str):
    return [
        child
        for child in ast.walk(node)
        if isinstance(child, ast.Call) and _call_name(child) == name
    ]


def _compile_isolated_function(function: ast.FunctionDef, namespace):
    isolated = ast.Module(body=[copy.deepcopy(function)], type_ignores=[])
    ast.fix_missing_locations(isolated)
    scope = dict(namespace)
    exec(compile(isolated, "<contract-probe>", "exec"), scope)
    return scope[function.name]


def _require_owner_atlas_scope(module: ast.Module, methods) -> None:
    policy = _function(module, "should_refresh_canonical_atlas_manifests")
    probe = _compile_isolated_function(
        policy,
        {"is_cluster_source_spm": lambda value: value == "cluster"},
    )
    if probe("owner") is not False or probe("cluster") is not True:
        raise CompileGateError(
            "Atlas scope contract failed: owner SPM must be excluded and "
            "Cluster SPM must be included"
        )

    refresh = methods.get("_refresh_canonical_atlas_manifests")
    if refresh is None:
        raise CompileGateError(
            "Atlas scope contract failed: App._refresh_canonical_atlas_manifests "
            "is missing"
        )
    producer_calls = _calls(refresh, "refresh_atlas_manifests_for_spm")
    if not producer_calls:
        raise CompileGateError(
            "Atlas scope contract failed: canonical Atlas producer call is missing"
        )
    first_producer_line = min(call.lineno for call in producer_calls)
    guarded_return = False
    for node in ast.walk(refresh):
        if not isinstance(node, ast.If):
            continue
        if not _calls(node.test, "should_refresh_canonical_atlas_manifests"):
            continue
        if not isinstance(node.test, ast.UnaryOp) or not isinstance(
            node.test.op, ast.Not
        ):
            continue
        if node.lineno >= first_producer_line:
            continue
        if any(isinstance(statement, ast.Return) for statement in node.body):
            guarded_return = True
            break
    if not guarded_return:
        raise CompileGateError(
            "Atlas scope contract failed: non-Cluster rows must return before "
            "refresh_atlas_manifests_for_spm"
        )


def _require_cluster_only_calibration(module: ast.Module) -> None:
    policy = _function(module, "should_calibrate_spm")
    probe = _compile_isolated_function(
        policy,
        {"is_cluster_source_spm": lambda value: value == "cluster"},
    )
    cases = (
        ({"spm": "owner"}, False, "owner"),
        ({"spm": "cluster"}, True, "cluster"),
        ({"spm": "cluster", "source_read_only": True}, False, "read-only cluster"),
        ({"spm": None}, False, "missing SPM"),
    )
    for item, expected, label in cases:
        if bool(probe(item)) is not expected:
            raise CompileGateError(
                "Bone calibration contract failed: "
                f"{label} expected {expected}"
            )


def _is_none_test(node: ast.AST, variable: str) -> bool:
    return (
        isinstance(node, ast.Compare)
        and isinstance(node.left, ast.Name)
        and node.left.id == variable
        and len(node.ops) == 1
        and isinstance(node.ops[0], ast.Is)
        and len(node.comparators) == 1
        and isinstance(node.comparators[0], ast.Constant)
        and node.comparators[0].value is None
    )


def _reads_mapping_key(node: ast.AST, variable: str, key: str) -> bool:
    for child in ast.walk(node):
        if not isinstance(child, ast.Subscript):
            continue
        if not isinstance(child.value, ast.Name) or child.value.id != variable:
            continue
        slice_node = child.slice
        if isinstance(slice_node, ast.Constant) and slice_node.value == key:
            return True
    return False


def _require_repair_push_reuse(methods) -> None:
    required = {
        "_run_full_pipeline",
        "_publish_assembly_stage_contract",
        "_assembly_stage_contract",
        "_job_blender",
        "_push_preflight",
        "_export_manifest_item",
        "_job_push",
    }
    missing = sorted(required.difference(methods))
    if missing:
        raise CompileGateError(
            "Repair-to-Push reuse contract failed: missing " + ", ".join(missing)
        )
    obsolete_guards = sorted({
        "_build_repair_stage_evidence",
        "_repair_stage_evidence_if_active",
        "_validate_assembly_stage_contract",
        "_repair_evidence_path_for_push",
    }.intersection(methods))
    if obsolete_guards:
        raise CompileGateError(
            "Obsolete Repair-to-Push evidence guard returned: "
            + ", ".join(obsolete_guards)
        )

    pipeline = methods["_run_full_pipeline"]
    initialized = any(
        isinstance(node, ast.Assign)
        and isinstance(node.value, ast.Dict)
        and any(
            isinstance(target, ast.Attribute)
            and target.attr == "_active_assembly_stage_contracts"
            for target in node.targets
        )
        for node in ast.walk(pipeline)
    )
    if not initialized:
        raise CompileGateError(
            "Repair-to-Push reuse contract failed: full pipeline does not "
            "initialize its job-scoped Repair result store"
        )
    cleanup_found = False
    for node in ast.walk(pipeline):
        if not isinstance(node, ast.Try) or not node.finalbody:
            continue
        constants = {
            child.value
            for statement in node.finalbody
            for child in ast.walk(statement)
            if isinstance(child, ast.Constant)
            and isinstance(child.value, str)
        }
        if (
            "_active_assembly_stage_contracts" in constants
            and any(_calls(statement, "pop") for statement in node.finalbody)
        ):
            cleanup_found = True
            break
    if not cleanup_found:
        raise CompileGateError(
            "Repair-to-Push reuse contract failed: full pipeline does not "
            "clear its job-scoped Repair result in finally"
        )
    if not _calls(methods["_job_blender"], "_publish_assembly_stage_contract"):
        raise CompileGateError(
            "Repair-to-Push reuse contract failed: Blender Repair does not "
            "publish its final result"
        )
    handoff_calls = _calls(methods["_job_blender"], "_handoff_ready")
    if not any(
        any(keyword.arg == "state_out" for keyword in call.keywords)
        for call in handoff_calls
    ):
        raise CompileGateError(
            "Repair-to-Push reuse contract failed: Blender Repair repeats "
            "the final Repair state instead of sharing one computed result"
        )
    if _calls(methods["_job_blender"], "_read_assembly_pipeline_json"):
        raise CompileGateError(
            "Repair status efficiency contract failed: Blender job must "
            "reuse the Repair report projection instead of reading it again"
        )

    push_preflight = methods["_push_preflight"]
    if not _calls(push_preflight, "_assembly_stage_contract"):
        raise CompileGateError(
            "Repair-to-Push reuse contract failed: Push does not read the "
            "job-scoped Repair result"
        )
    reuse_branch = None
    for node in ast.walk(push_preflight):
        if isinstance(node, ast.If) and _is_none_test(node.test, "repair_contract"):
            reuse_branch = node
            break
    if reuse_branch is None:
        raise CompileGateError(
            "Repair-to-Push reuse contract failed: persisted handoff fallback "
            "is not isolated to a missing job-scoped result"
        )
    if not any(_calls(statement, "_handoff_ready") for statement in reuse_branch.body):
        raise CompileGateError(
            "Repair-to-Push reuse contract failed: missing standalone Push "
            "handoff fallback"
        )
    if not reuse_branch.orelse:
        raise CompileGateError(
            "Repair-to-Push reuse contract failed: Push has no branch that "
            "reuses the Repair result"
        )
    if not all(
        any(
            _reads_mapping_key(statement, "repair_contract", key)
            for statement in reuse_branch.orelse
        )
        for key in ("ready", "reason")
    ):
        raise CompileGateError(
            "Repair-to-Push reuse contract failed: Push does not consume "
            "Repair ready/reason values"
        )

    repair_state = methods.get("_assembly_output_state_scoped")
    if repair_state is None:
        raise CompileGateError(
            "Repair status efficiency contract failed: "
            "_assembly_output_state_scoped is missing"
        )
    if len(_calls(repair_state, "_cluster_assembly_inputs_current")) != 1:
        raise CompileGateError(
            "Repair status efficiency contract failed: Cluster Assembly "
            "inputs must be validated exactly once per decision"
        )
    assembly_inputs = methods.get("_cluster_assembly_inputs_current")
    if assembly_inputs is None or not _calls(
        assembly_inputs,
        "_read_assembly_pipeline_json",
    ):
        raise CompileGateError(
            "Repair status efficiency contract failed: the large Repair "
            "report must use the scoped JSON reader"
        )
    for name in ("_export_manifest_item", "_job_push"):
        strings = {
            node.value
            for node in ast.walk(methods[name])
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str)
        }
        if "--repair-evidence" in strings:
            raise CompileGateError(
                "Obsolete Repair evidence forwarding returned: " + name
            )


def validate_gui_contracts(source: str, filename=str(GUI_PATH)) -> int:
    """Validate orchestration contracts against GUI source held in memory."""
    try:
        module = ast.parse(source, filename=filename)
    except SyntaxError as exc:
        raise CompileGateError(f"Python compile failed: {filename}: {exc}") from exc
    methods = _app_methods(module)

    present_runtime_compile_names = sorted(
        RUNTIME_COMPILE_NAMES.intersection(methods)
    )
    if present_runtime_compile_names:
        raise CompileGateError(
            "Runtime asset-wave compiler is forbidden: "
            + ", ".join(present_runtime_compile_names)
        )

    _require_owner_atlas_scope(module, methods)
    _require_cluster_only_calibration(module)
    _require_repair_push_reuse(methods)
    return 3


def validate_push_job_contracts(
    source: str,
    filename=str(PUSH_JOB_PATH),
) -> int:
    """Prevent mutable Repair evidence from becoming a Push gate again."""
    try:
        module = ast.parse(source, filename=filename)
    except SyntaxError as exc:
        raise CompileGateError(
            f"Python compile failed: {filename}: {exc}"
        ) from exc
    parse_args = _function(module, "parse_args")
    main = _function(module, "main")
    strings = {
        node.value
        for node in ast.walk(parse_args)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
    }
    if "--repair-evidence" in strings:
        raise CompileGateError(
            "Push worker exposes obsolete Repair evidence CLI"
        )
    for function_name in (
        "validate_assembly_export_evidence_bundle",
        "validate_export_object_postcondition",
    ):
        if _calls(main, function_name):
            raise CompileGateError(
                "Push worker calls obsolete evidence API: "
                + function_name
            )
    for function_name in (
        "consolidate_speedtree_group_materials",
        "normalize_speedtree_material_textures",
        "remove_unused_empty_material_slots",
        "pack_speedtree_vertex_payload",
    ):
        if not _calls(main, function_name):
            raise CompileGateError(
                "Push worker does not run current export normalization: "
                + function_name
            )
    return 1


def run_gate(
    repo_root=REPO_ROOT,
    gui_path=GUI_PATH,
    push_job_path=PUSH_JOB_PATH,
) -> CompileGateResult:
    started = time.perf_counter()
    repo_root = Path(repo_root).resolve()
    manifest = _compile_repository_sources(repo_root)
    contract_count = validate_gui_contracts(
        _read_python_source(Path(gui_path)),
        filename=str(gui_path),
    )
    contract_count += validate_push_job_contracts(
        _read_python_source(Path(push_job_path)),
        filename=str(push_job_path),
    )
    validate_production_source_manifest(
        manifest,
        production_source_manifest(repo_root),
        label="Compile-gate final production source",
    )
    return CompileGateResult(
        source_count=manifest.source_count,
        contract_count=contract_count,
        elapsed_seconds=time.perf_counter() - started,
        production_source_manifest=manifest,
    )


def main() -> int:
    try:
        result = run_gate()
    except CompileGateError as exc:
        print(f"SK Batch code compile gate FAILED: {exc}", file=sys.stderr)
        return 1
    print(
        "SK Batch code compile gate OK: "
        f"{result.source_count} Python sources, "
        f"{result.contract_count} contract groups, "
        "revision "
        f"{result.production_source_manifest.content_hash[:16]}, "
        f"{result.elapsed_seconds:.3f}s"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
