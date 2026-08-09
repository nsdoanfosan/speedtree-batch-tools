from sk_batch.job_report_contract import mark_job_failed


def test_failure_revokes_preflight_push_readiness():
    report = {
        "status": "ok",
        "unreal_push_ready": True,
        "handoff_preflight": {"status": "ok"},
        "cluster_assembly_manifest": {"status": "ready"},
    }

    mark_job_failed(report, RuntimeError("assembly failed"), "traceback-text")

    assert report["status"] == "failed"
    assert report["unreal_push_ready"] is False
    assert report["final_handoff_status"] == "failed"
    assert report["error"] == "assembly failed"
    assert report["traceback"] == "traceback-text"
    # Preflight remains evidence of what passed before the later failure.
    assert report["handoff_preflight"] == {"status": "ok"}


def test_legacy_default_bark_error_does_not_create_asset_authority():
    report = {
        "speedtree_pipeline_contract": {
            "material_intents": [
                {
                    "material_name": "M_bark_black_locast_02_Mat",
                    "stmat_material_id": "1",
                    "tree_part": "bark",
                    "texture_source_mode": "managed_texture_set",
                    "texture_binding": {
                        "status": "ok",
                        "texture_base": "T_bark_black_locast_02",
                        "texture_dir": "D:/Tree/texture",
                    },
                },
                {
                    "material_name": "M_bark_common_end_01_Mat",
                    "stmat_material_id": "7",
                    "tree_part": "bark",
                    "texture_source_mode": "managed_texture_set",
                    "texture_binding": {
                        "status": "ok",
                        "texture_base": "T_bark_common_end_01",
                        "texture_dir": "D:/Texture/bark/common_end",
                    },
                },
            ],
        },
    }
    error = RuntimeError(
        "SpeedTree merged Default placeholder requires exactly one actual "
        "managed bark material slot with a ready binding; found 2"
    )

    mark_job_failed(report, error, "traceback")

    assert report["status"] == "failed"
    assert report["error"] == str(error)
    assert "failure_classification" not in report
    assert "asset_issue" not in report
