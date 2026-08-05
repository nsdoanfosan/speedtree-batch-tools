"""Final status helpers shared by headless SK Batch jobs."""


def mark_job_failed(report, error, traceback_text):
    """Make a terminal job failure authoritative over earlier preflight state."""
    report["status"] = "failed"
    report["unreal_push_ready"] = False
    report["final_handoff_status"] = "failed"
    report["error"] = str(error)
    report["traceback"] = str(traceback_text or "")
    return report
