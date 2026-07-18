"""One-time migration for legacy Cluster Generator colors in live SK SPMs.

The normal material preflight remains read-only.  This command scans the same
backup-pruned live SK set as the SK Batch GUI and records a permanent receipt
for every applicable SPM.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path


TOOL_DIR = Path(__file__).resolve().parent
REPO_DIR = TOOL_DIR.parent
PCG_DIR = REPO_DIR / "pcg_st9_texture_batch"
for candidate in (REPO_DIR, TOOL_DIR, PCG_DIR):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from pcg_texture_audit import legacy_cluster_generator_candidates  # noqa: E402
from sk_common import scan_sk_spms  # noqa: E402
from spm_legacy_cluster_marker import mark_generator_guids_once  # noqa: E402


def migrate_existing_sk_markers(root, *, dry_run=True):
    root = Path(root).resolve()
    results = []
    for spm in scan_sk_spms(root):
        try:
            candidates = legacy_cluster_generator_candidates(spm)
            result = mark_generator_guids_once(
                spm, candidates, dry_run=dry_run
            )
            result["visible_generator_count"] = sum(
                int(bool(row.get("visible"))) for row in candidates
            )
            result["export_generator_count"] = sum(
                int(bool(row.get("export_participates")))
                for row in candidates
            )
        except Exception as exc:
            result = {
                "spm": str(spm),
                "status": "error",
                "changed": False,
                "generator_count": 0,
                "error": str(exc),
            }
        results.append(result)

    statuses = Counter(row["status"] for row in results)
    return {
        "kind": "skbatch_legacy_cluster_marker_migration",
        "version": 1,
        "mode": "dry_run" if dry_run else "apply",
        "root": str(root),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "summary": {
            "live_sk_spms": len(results),
            "applicable_spms": sum(
                int(row.get("generator_count", 0) > 0) for row in results
            ),
            "generator_count": sum(
                int(row.get("generator_count", 0)) for row in results
            ),
            "visible_generator_count": sum(
                int(row.get("visible_generator_count", 0)) for row in results
            ),
            "export_generator_count": sum(
                int(row.get("export_generator_count", 0)) for row in results
            ),
            "changed_spms": sum(
                int(bool(row.get("changed"))) for row in results
            ),
            "statuses": dict(sorted(statuses.items())),
            "errors": statuses.get("error", 0),
        },
        "results": results,
    }


def parse_args():
    parser = argparse.ArgumentParser(
        description="Mark legacy Cluster Generators in live SK SPMs once."
    )
    parser.add_argument("--root", required=True, help="SK data tree root")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="write SPM backups, marker colors and permanent receipts",
    )
    parser.add_argument("--report", help="optional JSON report path")
    return parser.parse_args()


def main():
    args = parse_args()
    report = migrate_existing_sk_markers(args.root, dry_run=not args.apply)
    text = json.dumps(report, indent=2, ensure_ascii=False)
    if args.report:
        report_path = Path(args.report).resolve()
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(text, encoding="utf-8")
    print(text)
    return 1 if report["summary"]["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
