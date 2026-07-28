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
