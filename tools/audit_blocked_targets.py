"""Ask production whether a change actually unblocked anything.

Every claim in this repository's recent history was reported as a suite count,
and a suite count cannot fail while a batch is stuck.  This asks the only
question that settles it: run the real blocked SPMs through the audit on two
revisions and diff the verdicts.

    # from a checkout of the revision under test
    python tools/audit_blocked_targets.py --out before.json      # on main
    python tools/audit_blocked_targets.py --out after.json       # on the branch
    python tools/audit_blocked_targets.py --compare before.json after.json

Targets come from the live shared job queue, so the set is whatever is
genuinely stuck right now.  `--targets` takes an explicit list instead.

Read-only by construction: every SPM's SHA-256 is compared before and after the
audit and a mismatch is a hard error.  No queue, receipt, or asset is written,
and no application is launched.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parents[1]
for _candidate in (REPO_DIR, REPO_DIR / "sk_batch", REPO_DIR / "pcg_st9_texture_batch"):
    if str(_candidate) not in sys.path:
        sys.path.insert(0, str(_candidate))

SUCCESS_TOKENS = frozenset({
    "imported_ok", "ready", "exported_pending_unreal", "preflight_skip",
})


def default_queue_path() -> Path:
    root = os.environ.get("LOCALAPPDATA") or str(Path.home())
    return Path(root) / "SpeedTreeBatchTools" / "shared_job_queue.json"


def queue_targets(path: Path) -> dict[str, list[str]]:
    """Every SPM in the queue that carries a non-success reason token."""
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    rows: dict[str, set[str]] = {}

    def walk(node, target=None):
        if isinstance(node, dict):
            here = node.get("target") or node.get("target_spm") or target
            token = node.get("reason_token")
            if (
                isinstance(here, str)
                and here.casefold().endswith(".spm")
                and isinstance(token, str)
            ):
                rows.setdefault(here, set()).add(token.casefold())
            for value in node.values():
                walk(value, here)
        elif isinstance(node, list):
            for value in node:
                walk(value, target)

    walk(payload)
    return {
        target: sorted(tokens)
        for target, tokens in rows.items()
        if tokens - SUCCESS_TOKENS
    }


def audit(target: Path) -> dict:
    from spm_leaf_handoff_contract import inspect_spm_mesh_file_references
    import jobs.speedtree_material_preflight as preflight

    before = hashlib.sha256(target.read_bytes()).hexdigest()
    report = inspect_spm_mesh_file_references(target)
    if hashlib.sha256(target.read_bytes()).hexdigest() != before:
        raise RuntimeError(f"audit mutated {target}; this tool must be read-only")
    integrity = report.get("atlas_consumer_integrity") or {}
    codes = sorted({
        issue["code"]
        for issue in preflight.preflight_contract_issues({
            "spm": str(target),
            "mesh_file_reference_contract": report,
        })
    })
    return {
        "status": report.get("status"),
        "blocking": bool(integrity.get("blocking")),
        "orphan_mesh": integrity.get("managed_orphan_mesh_count"),
        "orphan_material": integrity.get("managed_orphan_material_count"),
        "active_mesh": integrity.get("active_managed_mesh_count"),
        "classes": integrity.get("classification_counts"),
        "integrity_issue_codes": sorted({
            str(issue.get("code") or "")
            for issue in (integrity.get("integrity_issues") or [])
        }),
        "preflight_issue_codes": codes,
    }


def run(targets, out_path):
    results = {}
    for index, target in enumerate(sorted(targets), start=1):
        path = Path(target)
        print(f"  [{index}/{len(targets)}] {path.name}", flush=True)
        if not path.is_file():
            results[target] = {"status": "missing_file"}
            continue
        try:
            results[target] = audit(path)
        except Exception as exc:  # noqa: BLE001 - one bad SPM must not stop the sweep
            results[target] = {"status": "audit_error", "error": repr(exc)[:300]}
    clean = sum(
        1 for row in results.values()
        if row.get("status") == "ok" and not row.get("blocking")
    )
    blocking = sum(1 for row in results.values() if row.get("blocking"))
    print(f"\ntargets audited : {len(results)}")
    print(f"clean           : {clean}")
    print(f"blocking        : {blocking}")
    if out_path:
        Path(out_path).write_text(
            json.dumps(results, indent=1, ensure_ascii=False), encoding="utf-8"
        )
        print(f"written         : {out_path}")
    return results


def compare(before_path, after_path):
    before = json.loads(Path(before_path).read_text(encoding="utf-8"))
    after = json.loads(Path(after_path).read_text(encoding="utf-8"))
    shared = sorted(set(before) & set(after))
    unblocked = [t for t in shared if before[t].get("blocking") and not after[t].get("blocking")]
    regressed = [t for t in shared if not before[t].get("blocking") and after[t].get("blocking")]
    print(f"targets compared : {len(shared)}")
    print(f"blocked -> ok    : {len(unblocked)}")
    print(f"ok -> blocked    : {len(regressed)}")
    for label, rows in (("unblocked", unblocked), ("REGRESSED", regressed)):
        for target in rows:
            print(f"  {label}: {Path(target).name}")
    if not unblocked and not regressed:
        print("\nNo verdict changed. Whatever else is true, production did not move.")
    return 1 if regressed else 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--queue", default=None, help="shared_job_queue.json path")
    parser.add_argument("--targets", nargs="*", help="explicit SPM paths")
    parser.add_argument("--out", default=None, help="write the verdict JSON here")
    parser.add_argument(
        "--compare", nargs=2, metavar=("BEFORE", "AFTER"),
        help="diff two verdict files instead of auditing",
    )
    args = parser.parse_args(argv)

    if args.compare:
        return compare(*args.compare)

    if args.targets:
        targets = list(args.targets)
    else:
        queue = Path(args.queue) if args.queue else default_queue_path()
        if not queue.is_file():
            print(f"queue not found: {queue}", file=sys.stderr)
            return 2
        rows = queue_targets(queue)
        targets = sorted(rows)
        print(f"queue           : {queue}")
    print(f"targets         : {len(targets)}\n")
    run(targets, args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
