from sk_batch.stage_batch_policy import (
    policy_comparison,
    run_memory_bounded_stage,
    stage_worker_policy,
)


GIB = 1024 ** 3


def test_stage_workers_keep_requested_parallelism_inside_ram_envelope():
    policy = stage_worker_policy(
        "assembly",
        4,
        10,
        available_bytes=40 * GIB,
        reserve_bytes=8 * GIB,
        per_worker_peak_bytes=6 * GIB,
    )

    assert policy["selected_workers"] == 4
    assert policy["memory_limited"] is False


def test_stage_workers_reduce_peak_concurrency_before_memory_pressure():
    policy = stage_worker_policy(
        "assembly",
        4,
        10,
        available_bytes=19 * GIB,
        reserve_bytes=8 * GIB,
        per_worker_peak_bytes=6 * GIB,
    )

    assert policy["selected_workers"] == 1
    assert policy["memory_worker_limit"] == 1
    assert policy["memory_limited"] is True


def test_stage_workers_use_commit_headroom_when_it_is_tighter_than_ram():
    policy = stage_worker_policy(
        "assembly",
        4,
        10,
        available_bytes=40 * GIB,
        available_commit_bytes=14 * GIB,
        reserve_bytes=8 * GIB,
        per_worker_peak_bytes=6 * GIB,
    )

    assert policy["selected_workers"] == 1
    assert policy["available_memory_bytes"] == 14 * GIB
    assert policy["limiting_resource"] == "commit"


def test_memory_bounded_stage_rechecks_memory_and_expands_after_recovery():
    snapshots = iter((10, 10, 40, 40, 40))

    def sample():
        value = next(snapshots, 40)
        return {
            "available_physical_bytes": value * GIB,
            "available_commit_bytes": 80 * GIB,
            "effective_available_bytes": value * GIB,
            "limiting_resource": "physical",
        }

    completed = []
    results, report = run_memory_bounded_stage(
        "assembly",
        [1, 2, 3],
        3,
        lambda value: value * 10,
        on_complete=lambda _index, item, result: completed.append(
            (item, result)
        ),
        memory_snapshot_fn=sample,
        reserve_bytes=8 * GIB,
        per_worker_peak_bytes=6 * GIB,
    )

    assert results == [10, 20, 30]
    assert sorted(completed) == [(1, 10), (2, 20), (3, 30)]
    assert report["max_concurrent_workers"] == 2
    assert report["memory_limited"] is True
    assert any(not row["admitted"] for row in report["admission_checks"])


def test_stage_batch_comparison_counts_worker_waves_and_unreal_recycles():
    comparison = policy_comparison(
        item_count=27,
        assembly_seconds=30,
        blender_export_seconds=12,
        unreal_ingest_seconds=40,
        assembly_workers=2,
        blender_export_workers=2,
        unreal_process_capacity=6,
        unreal_startup_seconds=20,
    )

    assert comparison["item_serial"]["unreal_process_launches"] == 27
    assert comparison["stage_batched"]["unreal_process_launches"] == 5
    assert comparison["estimated_wall_reduction_seconds"] > 0
    assert comparison["estimated_speedup"] > 1
