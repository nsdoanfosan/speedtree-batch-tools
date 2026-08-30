from sk_batch.stage_batch_policy import policy_comparison, stage_worker_policy


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
