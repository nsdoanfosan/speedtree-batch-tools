"""Publish command-line Push receipts to the same state consumed by the GUI."""

from __future__ import annotations

import copy
import json
from datetime import datetime
from pathlib import Path

from sk_common import (
    atomic_write_json,
    cached_push_source_fingerprint,
    load_state,
    push_source_cache_matches_snapshot,
    push_source_snapshot,
    save_state,
)
from send2ue_manifest_contract import manifest_item_has_current_skeleton_root_export


def capture_push_source(spm):
    spm = Path(spm).resolve()
    dependency = spm.parent / "reports" / f"{spm.stem}_speedtree_assembly_pipeline_report_codex.json"
    _, record, _ = cached_push_source_fingerprint(spm.with_suffix(".blend"), [dependency])
    return record


def prepare_push_state(command, outputs):
    record = capture_push_source(outputs["queue_id"])
    outputs["push_source_fingerprint_cache"] = record
    if "--source-fingerprint" in command:
        command[command.index("--source-fingerprint") + 1] = record["fingerprint"]
    else:
        command.extend(["--source-fingerprint", record["fingerprint"]])


def publish_push_result(outputs, report):
    """Publish only evidence tied to the exact source captured before export.

    Old deferred commands without that source proof cannot establish freshness.
    Their reports remain readable, but are not promoted into a current GUI row.
    """
    source = outputs.get("push_source_fingerprint_cache") or report.get("push_source_fingerprint_cache")
    if not source:
        return False
    iid = str(outputs["queue_id"])
    spm = Path(iid)
    dependency = spm.parent / "reports" / f"{spm.stem}_speedtree_assembly_pipeline_report_codex.json"
    if not push_source_cache_matches_snapshot(
        source, push_source_snapshot(spm.with_suffix(".blend"), [dependency]),
    ):
        raise ValueError("Push source changed after export; cannot publish a current result")
    manifest_path = Path(outputs["manifest"])
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    matching = [row for row in manifest.get("items", []) if row.get("queue_id") == iid]
    if len(matching) != 1:
        raise ValueError("Push manifest must contain exactly one matching queue ID")
    item = matching[0]
    fingerprint = item.get("fingerprint")
    if (
        not fingerprint
        or report.get("manifest_fingerprint") != fingerprint
        or item.get("source_fingerprint") != source.get("fingerprint")
        or not manifest_item_has_current_skeleton_root_export(item)
    ):
        raise ValueError("Push report/manifest/source evidence does not match")
    status = report.get("status")
    unreal_result = report.get("unreal_result") or {}
    imported = status == "ok" and unreal_result.get("status") == "imported_ok"
    if imported and unreal_result.get("fingerprint", fingerprint) != fingerprint:
        raise ValueError("Unreal result belongs to a different export fingerprint")
    if not imported and status != "exported_pending_unreal":
        raise ValueError("Push receipt does not prove export or successful Unreal import")
    # Preserve pre-export proof for deferred/fleet resume across processes.
    report["push_source_fingerprint_cache"] = copy.deepcopy(source)
    atomic_write_json(Path(outputs["report"]), report)
    state = load_state()
    entry = state.setdefault(iid, {})
    entry["push_source_fingerprint_cache"] = copy.deepcopy(source)
    entry["push_export_cache"] = {
        "source_fingerprint": source["fingerprint"],
        "fingerprint": fingerprint,
        "manifest": str(manifest_path),
    }
    entry["push_status_kind"] = "imported_ok" if imported else "exported_pending_unreal"
    entry["push_status"] = (
        f"완료 {datetime.now():%m-%d %H:%M}" if imported else "export 완료 · Unreal 대기"
    )
    if imported:
        entry["push_import_fingerprint"] = fingerprint
    else:
        entry.pop("push_import_fingerprint", None)
    entry["push_paths"] = {
        key: str(outputs[value])
        for key, value in (
            ("manifest", "manifest"), ("checkpoint", "checkpoint"),
            ("report", "report"), ("import_report", "item_import_report"),
            ("batch_report", "batch_report"),
        )
    }
    entry.pop("push_status_error", None)
    entry.pop("push_status_result", None)
    save_state(state)
    return True
