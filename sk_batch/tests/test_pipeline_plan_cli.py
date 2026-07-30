from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from sk_batch import __main__ as cli
from sk_batch import pipeline_plan
from cluster_assembly_builder import MANIFEST_KIND


def _tree_item(path, index=0):
    return {
        "index": index,
        "spm": Path(path),
        "authoring_spm": Path(path),
        "output_spm": Path(path),
        "kind": "tree",
    }


def _cluster_item(path, index=0):
    return {
        "index": index,
        "spm": Path(path),
        "authoring_spm": Path(path),
        "output_spm": Path(path),
        "kind": "cluster",
        "cluster_pair_status": "current",
    }


def _root_snapshot(root):
    result = {}
    for path in sorted(
        (value for value in Path(root).rglob("*") if value.is_file()),
        key=lambda value: str(value).casefold(),
    ):
        stat = path.stat()
        result[str(path.relative_to(root))] = {
            "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
    return result


def _forbid(name):
    def fail(*_args, **_kwargs):
        raise AssertionError(f"planner called forbidden mutation: {name}")

    return fail


def test_plan_only_scan_preserves_every_production_file_and_runs_no_process(
    tmp_path,
    monkeypatch,
):
    root = tmp_path / "Tree"
    owner = root / "tree_oak"
    owner.mkdir(parents=True)
    spm = owner / "SK_tree_oak_01.spm"
    spm.write_bytes(b"production-spm")
    before = _root_snapshot(root)

    monkeypatch.setattr(subprocess, "run", _forbid("subprocess.run"))
    monkeypatch.setattr(subprocess, "Popen", _forbid("subprocess.Popen"))

    import sk_common

    for name in (
        "save_config",
        "save_state",
        "atomic_write_bytes",
        "atomic_write_json",
        "prepare_cluster_spm_pair_for_job",
    ):
        monkeypatch.setattr(sk_common, name, _forbid(name))

    from pcg_st9_texture_batch import pcg_canonical_outputs

    monkeypatch.setattr(
        pcg_canonical_outputs,
        "refresh_atlas_manifests_for_spm",
        _forbid("refresh_atlas_manifests_for_spm"),
    )
    monkeypatch.setattr(Path, "write_bytes", _forbid("Path.write_bytes"))
    monkeypatch.setattr(Path, "write_text", _forbid("Path.write_text"))

    plan = pipeline_plan.build_pipeline_plan(
        root,
        phase="spm",
    )

    after = _root_snapshot(root)
    assert before == after
    assert plan["status"] == "ready"
    assert plan["ordered_targets"] == [str(spm.resolve())]
    assert plan["inventory"][0]["bone_setting"] == "skipped_non_cluster"
    assert plan["stages"] == []


def test_real_plan_only_cli_preserves_production_root(tmp_path):
    root = tmp_path / "Tree"
    owner = root / "tree_oak"
    owner.mkdir(parents=True)
    spm = owner / "SK_tree_oak_01.spm"
    spm.write_bytes(b"production-spm")
    before = _root_snapshot(root)

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "sk_batch",
            "--plan-only",
            "--root",
            str(root),
            "--phase",
            "spm",
            "--compact",
        ],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["mode"] == "plan_only"
    assert payload["status"] == "ready"
    assert payload["ordered_targets"] == [str(spm.resolve())]
    assert payload["stages"] == []
    assert before == _root_snapshot(root)


def test_exact_dependency_plan_orders_cluster_first_and_only_bones_cluster(
    tmp_path,
    monkeypatch,
):
    owner = tmp_path / "tree_oak"
    cluster_dir = owner / "Cluster"
    cluster_dir.mkdir(parents=True)
    root_spm = owner / "SK_tree_oak_01.spm"
    cluster_spm = cluster_dir / "SK_cluster_oak_01.spm"
    cluster_blend = cluster_spm.with_suffix(".blend")
    root_spm.write_bytes(b"root")
    cluster_spm.write_bytes(b"cluster")
    cluster_blend.write_bytes(b"blend")
    inventory = [
        _cluster_item(cluster_spm, index=0),
        _tree_item(root_spm, index=1),
    ]
    monkeypatch.setattr(
        pipeline_plan,
        "scan_pipeline_inventory",
        lambda _root: inventory,
    )
    monkeypatch.setattr(
        pipeline_plan,
        "load_current_cluster_assembly_manifest",
        lambda _spm: {
            "kind": MANIFEST_KIND,
            "status": "ready",
            "parts": [{
                "prototype_id": "cluster-oak",
                "external_source": {
                    "source_blend": {"path": str(cluster_blend)}
                },
            }],
        },
    )

    plan = pipeline_plan.build_pipeline_plan(
        tmp_path,
        phase="push",
        targets=[root_spm],
    )

    assert plan["status"] == "ready"
    assert plan["ordered_targets"] == [
        str(cluster_spm.resolve()),
        str(root_spm.resolve()),
    ]
    assert plan["dependencies_by_root"] == {
        str(root_spm.resolve()): [str(cluster_spm.resolve())]
    }
    assert plan["auto_added_dependencies"] == [
        str(cluster_spm.resolve())
    ]
    assert [
        (row["phase"], row["wave"])
        for row in plan["stages"]
    ] == [
        ("spm", "cluster"),
        ("blender", "cluster"),
        ("blender", "tree"),
        ("push", "cluster"),
        ("push", "tree"),
    ]
    assert plan["stages"][0]["bone_setting_targets"] == [
        str(cluster_spm.resolve())
    ]
    assert all(
        not row["bone_setting_targets"]
        for row in plan["stages"][1:]
    )


def test_noncurrent_cluster_pair_is_blocked_without_normalization(
    tmp_path,
    monkeypatch,
):
    cluster_spm = (
        tmp_path / "tree_oak" / "Cluster" / "SK_cluster_oak_01.spm"
    )
    inventory = [{
        **_cluster_item(cluster_spm),
        "cluster_pair_status": "normalization_ready",
    }]
    monkeypatch.setattr(
        pipeline_plan,
        "scan_pipeline_inventory",
        lambda _root: inventory,
    )

    plan = pipeline_plan.build_pipeline_plan(
        tmp_path,
        phase="push",
        targets=[cluster_spm],
    )

    assert plan["status"] == "blocked"
    assert plan["ordered_targets"] == []
    assert plan["stages"] == []
    assert plan["blocked"][0]["code"] == "exact_cluster_pair_not_current"


def test_auto_added_noncurrent_cluster_pair_blocks_tree_plan(
    tmp_path,
    monkeypatch,
):
    owner = tmp_path / "tree_oak"
    cluster_dir = owner / "Cluster"
    cluster_dir.mkdir(parents=True)
    root_spm = owner / "SK_tree_oak_01.spm"
    cluster_spm = cluster_dir / "SK_cluster_oak_01.spm"
    cluster_blend = cluster_spm.with_suffix(".blend")
    root_spm.write_bytes(b"root")
    cluster_spm.write_bytes(b"cluster")
    cluster_blend.write_bytes(b"blend")
    inventory = [
        {
            **_cluster_item(cluster_spm, index=0),
            "cluster_pair_status": "normalization_ready",
        },
        _tree_item(root_spm, index=1),
    ]
    monkeypatch.setattr(
        pipeline_plan,
        "scan_pipeline_inventory",
        lambda _root: inventory,
    )
    monkeypatch.setattr(
        pipeline_plan,
        "load_current_cluster_assembly_manifest",
        lambda _spm: {
            "kind": MANIFEST_KIND,
            "status": "ready",
            "parts": [{
                "prototype_id": "cluster-oak",
                "external_source": {
                    "source_blend": {"path": str(cluster_blend)}
                },
            }],
        },
    )

    plan = pipeline_plan.build_pipeline_plan(
        tmp_path,
        phase="push",
        targets=[root_spm],
    )

    assert plan["status"] == "blocked"
    assert plan["ordered_targets"] == []
    assert plan["stages"] == []
    assert plan["blocked"][0]["code"] == "exact_cluster_pair_not_current"


@pytest.mark.parametrize(
    ("manifest_result", "code"),
    [
        (None, "exact_cluster_dependency_contract_missing"),
        (
            pipeline_plan.PushDependencyError("stale receipt"),
            "exact_cluster_dependency_contract_stale",
        ),
    ],
)
def test_missing_or_stale_exact_contract_is_blocked_without_refresh(
    tmp_path,
    monkeypatch,
    manifest_result,
    code,
):
    root_spm = tmp_path / "tree_oak" / "SK_tree_oak_01.spm"
    root_spm.parent.mkdir(parents=True)
    root_spm.write_bytes(b"root")
    inventory = [_tree_item(root_spm)]
    monkeypatch.setattr(
        pipeline_plan,
        "scan_pipeline_inventory",
        lambda _root: inventory,
    )

    def load_manifest(_spm):
        if isinstance(manifest_result, Exception):
            raise manifest_result
        return manifest_result

    monkeypatch.setattr(
        pipeline_plan,
        "load_current_cluster_assembly_manifest",
        load_manifest,
    )
    monkeypatch.setattr(
        pipeline_plan,
        "expand_push_targets",
        _forbid("dependency fallback"),
    )

    from pcg_st9_texture_batch import pcg_canonical_outputs

    monkeypatch.setattr(
        pcg_canonical_outputs,
        "refresh_atlas_manifests_for_spm",
        _forbid("refresh_atlas_manifests_for_spm"),
    )

    plan = pipeline_plan.build_pipeline_plan(
        tmp_path,
        phase="push",
        targets=[root_spm],
    )

    assert plan["status"] == "blocked"
    assert plan["ordered_targets"] == []
    assert plan["stages"] == []
    assert plan["blocked"][0]["code"] == code


@pytest.mark.parametrize(
    "argv",
    [
        ["--root", "X", "--phase", "spm"],
        ["--execute", "--root", "X", "--phase", "spm"],
        ["--plan-only", "--root", "X", "--phase", "unknown"],
        ["--plan-only", "--root", "X", "--phase", "spm", "--index", "-1"],
        ["--plan-only", "--root", "X", "--phase", "spm", "--unknown"],
    ],
)
def test_invalid_mode_phase_or_index_fails_before_gate_or_plan(
    monkeypatch,
    argv,
):
    monkeypatch.setattr(cli, "run_gate", _forbid("compile gate"))
    monkeypatch.setattr(
        cli,
        "build_pipeline_plan",
        _forbid("pipeline plan"),
    )

    with pytest.raises(SystemExit) as caught:
        cli.main(argv)

    assert caught.value.code == 2


def test_unknown_scanned_index_fails_without_writing(tmp_path, monkeypatch):
    root_spm = tmp_path / "tree_oak" / "SK_tree_oak_01.spm"
    root_spm.parent.mkdir(parents=True)
    root_spm.write_bytes(b"root")
    monkeypatch.setattr(
        pipeline_plan,
        "scan_pipeline_inventory",
        lambda _root: [_tree_item(root_spm)],
    )

    with pytest.raises(
        pipeline_plan.PipelinePlanInputError,
        match="outside the production scan",
    ):
        pipeline_plan.build_pipeline_plan(
            tmp_path,
            phase="spm",
            indexes=[99],
        )


def test_cli_emits_compile_revision_and_never_imports_gui(
    tmp_path,
    monkeypatch,
    capsys,
):
    revision = "1" * 64
    gate = SimpleNamespace(
        source_count=10,
        contract_count=3,
        production_source_manifest=SimpleNamespace(
            content_hash=revision,
        ),
    )
    monkeypatch.setattr(cli, "run_gate", lambda *_args, **_kwargs: gate)
    monkeypatch.setattr(
        cli,
        "build_pipeline_plan",
        lambda *_args, **_kwargs: {
            "kind": pipeline_plan.PLAN_KIND,
            "schema_version": pipeline_plan.PLAN_SCHEMA_VERSION,
            "mode": "plan_only",
            "status": "ready",
        },
    )

    assert cli.main([
        "--plan-only",
        "--root",
        str(tmp_path),
        "--phase",
        "spm",
        "--compact",
    ]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["compile_gate"]["production_source_revision"] == revision
    source = Path(pipeline_plan.__file__).read_text(encoding="utf-8")
    assert "sk_batch_gui" not in source
    assert "SharedQueueRuntime" not in source
    assert "tkinter" not in source
