import ast
from pathlib import Path


JOB_PATH = Path(__file__).resolve().parents[1] / "jobs" / "bwr_headless_job.py"


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


def test_stage_duration_uses_monotonic_elapsed_seconds():
    report = {}
    load_record_stage_duration(15.125)(report, "speedtree_export_bundle", 12.0)
    assert report["stage_timings_seconds"] == {
        "speedtree_export_bundle": 3.125
    }


def test_bwr_reports_export_repair_and_total_stage_boundaries():
    source = JOB_PATH.read_text(encoding="utf-8")
    for stage in (
        "input_preflight",
        "addon_runtime_prepare",
        "spm_skeleton_readiness",
        "blend_open",
        "speedtree_export_bundle",
        "blender_import_and_repair",
        "total_job",
    ):
        assert f'"{stage}"' in source
