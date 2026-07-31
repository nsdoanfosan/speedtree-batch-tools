"""Export the full PCG ST9 audit, dry-run plans, and handoff queues."""
import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from export_atlas_handoff_queue import build_atlas_queue_from_report, write_csv as write_atlas_queue_csv
from export_prepare_apply_queue import build_prepare_apply_queue_from_report, write_csv as write_prepare_apply_queue_csv
from export_prepare_plan import build_plan_from_report, write_csv as write_prepare_plan_csv
from export_review_brief import build_review_brief_from_report
from export_review_queue import build_review_queue_from_report, write_csv as write_review_queue_csv
from export_sbs_handoff_queue import build_sbs_queue_from_report, write_csv as write_sbs_queue_csv
from export_texture_plan import build_texture_plan_from_report, write_csv as write_texture_plan_csv
from pcg_texture_audit import make_report, write_csv as write_audit_csv
from pcg_texture_common import REPORT_DIR, TARGETS_PATH, load_config, load_pcg_targets


def _prefix_suffix(prefix, stamp, no_stamp=False):
    parts = []
    if prefix:
        parts.append(prefix)
    if stamp and not no_stamp:
        parts.append(stamp)
    return "_" + "_".join(parts) if parts else ""


def _write_json(path, data):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def _prepare_summary(plan):
    summary = {
        "folders": plan.get("summary", {}).get("folders", 0),
        "targets": plan.get("summary", {}).get("targets", 0),
        "would_create": 0,
        "patch_existing": 0,
        "skipped": 0,
        "rename_count": 0,
    }
    for item in plan.get("items", []):
        for target in item.get("targets", []):
            if target.get("status") == "skipped":
                summary["skipped"] += 1
                continue
            if target.get("would_create"):
                summary["would_create"] += 1
            else:
                summary["patch_existing"] += 1
            patch = target.get("patch") or {}
            summary["rename_count"] += len(patch.get("renames", []))
    return summary


def export_all_from_report(report, out_dir=None, prefix="", stamp=None, no_stamp=False):
    """Write every current report type from an already-built audit report."""
    out_dir = Path(out_dir or REPORT_DIR)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = stamp or datetime.now().strftime("%Y%m%d_%H%M%S")
    suffix = _prefix_suffix(prefix, stamp, no_stamp=no_stamp)

    paths = {
        "audit_json": out_dir / f"pcg_texture_audit{suffix}.json",
        "audit_csv": out_dir / f"pcg_texture_audit{suffix}.csv",
        "prepare_plan_json": out_dir / f"pcg_prepare_plan{suffix}.json",
        "prepare_plan_csv": out_dir / f"pcg_prepare_plan{suffix}.csv",
        "prepare_apply_queue_json": out_dir / f"pcg_prepare_apply_queue{suffix}.json",
        "prepare_apply_queue_csv": out_dir / f"pcg_prepare_apply_queue{suffix}.csv",
        "texture_plan_json": out_dir / f"pcg_texture_work_plan{suffix}.json",
        "texture_plan_csv": out_dir / f"pcg_texture_work_plan{suffix}.csv",
        "atlas_queue_json": out_dir / f"pcg_atlas_handoff_queue{suffix}.json",
        "atlas_queue_csv": out_dir / f"pcg_atlas_handoff_queue{suffix}.csv",
        "sbs_queue_json": out_dir / f"pcg_sbs_handoff_queue{suffix}.json",
        "sbs_queue_csv": out_dir / f"pcg_sbs_handoff_queue{suffix}.csv",
        "review_queue_json": out_dir / f"pcg_review_queue{suffix}.json",
        "review_queue_csv": out_dir / f"pcg_review_queue{suffix}.csv",
        "review_brief_md": out_dir / f"pcg_review_brief{suffix}.md",
        "summary_json": out_dir / f"pcg_all_queues{suffix}_summary.json",
    }

    _write_json(paths["audit_json"], report)
    write_audit_csv(report, paths["audit_csv"])

    prepare_plan = build_plan_from_report(report, paths["audit_json"])
    _write_json(paths["prepare_plan_json"], prepare_plan)
    write_prepare_plan_csv(prepare_plan, paths["prepare_plan_csv"])

    prepare_apply_queue = build_prepare_apply_queue_from_report(report, paths["audit_json"])
    _write_json(paths["prepare_apply_queue_json"], prepare_apply_queue)
    write_prepare_apply_queue_csv(prepare_apply_queue, paths["prepare_apply_queue_csv"])

    texture_plan = build_texture_plan_from_report(report, paths["audit_json"])
    _write_json(paths["texture_plan_json"], texture_plan)
    write_texture_plan_csv(texture_plan, paths["texture_plan_csv"])

    atlas_queue = build_atlas_queue_from_report(report, paths["audit_json"])
    _write_json(paths["atlas_queue_json"], atlas_queue)
    write_atlas_queue_csv(atlas_queue, paths["atlas_queue_csv"])

    sbs_queue = build_sbs_queue_from_report(report, paths["audit_json"])
    _write_json(paths["sbs_queue_json"], sbs_queue)
    write_sbs_queue_csv(sbs_queue, paths["sbs_queue_csv"])

    review_queue = build_review_queue_from_report(report, paths["audit_json"])
    _write_json(paths["review_queue_json"], review_queue)
    write_review_queue_csv(review_queue, paths["review_queue_csv"])

    review_brief = build_review_brief_from_report(report, paths["audit_json"])
    Path(paths["review_brief_md"]).write_text(review_brief, encoding="utf-8")

    summary = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "out_dir": str(out_dir),
        "audit": report.get("summary", {}),
        "pcg_targets": report.get("pcg_targets", {}),
        "prepare_plan": _prepare_summary(prepare_plan),
        "prepare_apply_queue": prepare_apply_queue.get("summary", {}),
        "texture_plan": texture_plan.get("summary", {}),
        "atlas_queue": atlas_queue.get("summary", {}),
        "sbs_queue": sbs_queue.get("summary", {}),
        "review_queue": review_queue.get("summary", {}),
        "files": {key: str(path) for key, path in paths.items() if key != "summary_json"},
    }
    _write_json(paths["summary_json"], summary)
    return {"paths": {key: str(path) for key, path in paths.items()}, "summary": summary}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pcg-targets", default=str(TARGETS_PATH), help="PCG target JSON. Use an empty string to scan all folders.")
    parser.add_argument("--out-dir", default=str(REPORT_DIR))
    parser.add_argument("--prefix", default="")
    parser.add_argument("--no-stamp", action="store_true", help="Use stable names without a timestamp.")
    args = parser.parse_args()

    cfg = load_config()
    pcg_targets = load_pcg_targets(args.pcg_targets) if args.pcg_targets else None
    report = make_report(cfg, pcg_targets=pcg_targets)
    result = export_all_from_report(
        report,
        out_dir=args.out_dir,
        prefix=args.prefix,
        no_stamp=args.no_stamp,
    )
    print(json.dumps(result["summary"], indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
