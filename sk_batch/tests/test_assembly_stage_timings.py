import ast
import copy
from pathlib import Path


JOB_PATH = Path(__file__).resolve().parents[1] / "jobs" / "assembly_headless_job.py"


def load_record_stage_duration(now):
    tree = ast.parse(JOB_PATH.read_text(encoding="utf-8"))
    function = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "record_stage_duration"
    )
    namespace = {"perf_counter": lambda: now}
    exec(
        compile(ast.Module(body=[function], type_ignores=[]), str(JOB_PATH), "exec"),
        namespace,
    )
    return namespace["record_stage_duration"]


def load_reusable_contracts_helper():
    tree = ast.parse(JOB_PATH.read_text(encoding="utf-8"))
    function = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "reusable_preflight_spm_contracts"
    )
    namespace = {"copy": copy}
    exec(
        compile(ast.Module(body=[function], type_ignores=[]), str(JOB_PATH), "exec"),
        namespace,
    )
    return namespace["reusable_preflight_spm_contracts"]


def test_stage_duration_uses_monotonic_elapsed_seconds():
    report = {}
    load_record_stage_duration(15.125)(report, "speedtree_export_bundle", 12.0)
    assert report["stage_timings_seconds"] == {
        "speedtree_export_bundle": 3.125
    }


def test_assembly_reports_export_and_total_stage_boundaries():
    source = JOB_PATH.read_text(encoding="utf-8")
    for stage in (
        "input_preflight",
        "addon_runtime_prepare",
        "blend_open",
        "speedtree_export_bundle",
        "blender_import_and_assemble",
        "post_assembly_spm_contracts",
        "vertex_payload_finalize",
        "blend_save",
        "blend_identity_fingerprint",
        "pipeline_report_write",
        "total_job",
    ):
        assert f'"{stage}"' in source


def test_exact_preflight_export_reuses_leaf_and_material_contracts():
    helper = load_reusable_contracts_helper()
    artifact = {
        "relative_path": "Tree.fbx",
        "size": 123,
        "sha256": "abc",
    }
    preflight = {
        "status": "ok",
        "speedtree_export": {
            "input_fingerprint": "input-1",
            "artifacts": [artifact],
        },
        "leaf_reference_contract": {"status": "source_only"},
        "material_export_contract": {"status": "ok"},
    }
    current = {
        "input_fingerprint": "input-1",
        "artifacts": [dict(artifact)],
    }

    result = helper(preflight, current)

    assert result == (
        {"status": "source_only"},
        {"status": "ok"},
    )
    assert result[0] is not preflight["leaf_reference_contract"]


def test_changed_exact_export_falls_back_to_live_spm_inspection():
    helper = load_reusable_contracts_helper()
    preflight = {
        "status": "ok",
        "speedtree_export": {
            "input_fingerprint": "old",
            "artifacts": [{
                "relative_path": "Tree.fbx",
                "size": 123,
                "sha256": "abc",
            }],
        },
        "leaf_reference_contract": {"status": "source_only"},
        "material_export_contract": {"status": "ok"},
    }
    current = {
        "input_fingerprint": "new",
        "artifacts": [{
            "relative_path": "Tree.fbx",
            "size": 123,
            "sha256": "abc",
        }],
    }

    assert helper(preflight, current) is None
