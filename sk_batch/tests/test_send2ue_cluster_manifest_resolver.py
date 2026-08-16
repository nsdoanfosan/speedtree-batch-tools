import ast
import json
from pathlib import Path


JOB_PATH = Path(__file__).resolve().parents[1] / "jobs" / "send2ue_push_job.py"


def load_resolver():
    tree = ast.parse(JOB_PATH.read_text(encoding="utf-8"))
    function = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "load_cluster_assembly_manifest"
    )

    def reject_ready_validation(*_args, **_kwargs):
        raise AssertionError("pass-through entered ready artifact validation")

    namespace = {
        "Path": Path,
        "json": json,
        "cluster_file_fingerprint": lambda path: {"path": str(path)},
        "validate_file_fingerprint": reject_ready_validation,
        "validate_manifest_artifacts": reject_ready_validation,
    }
    module = ast.Module(body=[function], type_ignores=[])
    exec(compile(module, str(JOB_PATH), "exec"), namespace)
    return namespace["load_cluster_assembly_manifest"]


def test_repair_pass_through_supersedes_direct_bindings_file(tmp_path):
    resolver = load_resolver()
    spm = tmp_path / "SK_tree_pass_through.spm"
    spm.write_bytes(b"spm")
    assembly = tmp_path / "assembly"
    reports = tmp_path / "reports"
    assembly.mkdir()
    reports.mkdir()
    assembly.joinpath(
        "SK_tree_pass_through_cluster_assembly_bindings.json"
    ).write_text(
        json.dumps({
            "kind": "sk_batch_cluster_nanite_assembly_inputs",
            "status": "ready",
            "parts": [],
        }),
        encoding="utf-8",
    )
    reports.joinpath(
        "SK_tree_pass_through_speedtree_repair_pipeline_report_codex.json"
    ).write_text(
        json.dumps({
            "cluster_assembly_manifest": {
                "kind": "sk_batch_cluster_nanite_assembly_inputs",
                "status": "pass_through",
                "reason": "normalized roles are unused",
            }
        }),
        encoding="utf-8",
    )

    manifest = resolver(tmp_path, spm)

    assert manifest["status"] == "pass_through"


def test_legacy_direct_pass_through_bypasses_ready_artifact_validation(tmp_path):
    resolver = load_resolver()
    spm = tmp_path / "SK_tree_legacy_pass_through.spm"
    spm.write_bytes(b"spm")
    assembly = tmp_path / "assembly"
    assembly.mkdir()
    assembly.joinpath(
        "SK_tree_legacy_pass_through_cluster_assembly_bindings.json"
    ).write_text(
        json.dumps({
            "kind": "sk_batch_cluster_nanite_assembly_inputs",
            "status": "pass_through",
            "parts": [{"diagnostic_only": True}],
        }),
        encoding="utf-8",
    )

    manifest = resolver(tmp_path, spm)

    assert manifest["status"] == "pass_through"


def test_report_without_assembly_decision_uses_legacy_direct_fallback(tmp_path):
    resolver = load_resolver()
    spm = tmp_path / "SK_tree_legacy_report.spm"
    spm.write_bytes(b"spm")
    assembly = tmp_path / "assembly"
    reports = tmp_path / "reports"
    assembly.mkdir()
    reports.mkdir()
    reports.joinpath(
        "SK_tree_legacy_report_speedtree_repair_pipeline_report_codex.json"
    ).write_text(json.dumps({"status": "done"}), encoding="utf-8")
    assembly.joinpath(
        "SK_tree_legacy_report_cluster_assembly_bindings.json"
    ).write_text(
        json.dumps({
            "kind": "sk_batch_cluster_nanite_assembly_inputs",
            "status": "pass_through",
        }),
        encoding="utf-8",
    )

    manifest = resolver(tmp_path, spm)

    assert manifest["status"] == "pass_through"
