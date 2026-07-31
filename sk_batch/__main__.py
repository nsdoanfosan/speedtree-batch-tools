"""Planner-only command line for SK Batch.

Execution is intentionally unavailable here.  The production GUI remains the
only runtime orchestrator until its mutation and process-launch boundaries are
extracted into independently testable services.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .code_compile_gate import (
    CompileGateError,
    production_source_manifest,
    run_gate,
    validate_production_source_manifest,
)
from .pipeline_plan import (
    PLAN_PHASES,
    PipelinePlanInputError,
    build_pipeline_plan,
)


def _nonnegative_index(value):
    try:
        index = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"index must be an integer: {value}"
        ) from exc
    if index < 0:
        raise argparse.ArgumentTypeError(
            f"index must be zero or greater: {value}"
        )
    return index


def build_parser():
    parser = argparse.ArgumentParser(
        prog="python -m sk_batch",
        description=(
            "Compile a read-only SK Batch plan. Pipeline execution is not "
            "available from this command line."
        ),
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--plan-only",
        action="store_true",
        help="scan and print a read-only plan",
    )
    mode.add_argument(
        "--execute",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument("--root", required=True)
    parser.add_argument(
        "--phase",
        required=True,
        choices=PLAN_PHASES,
    )
    parser.add_argument(
        "--target",
        action="append",
        default=[],
        help=(
            "exact production SPM path, absolute or relative to --root; "
            "repeatable"
        ),
    )
    parser.add_argument(
        "--index",
        action="append",
        default=[],
        type=_nonnegative_index,
        help="zero-based index from the deterministic production scan",
    )
    parser.add_argument(
        "--compact",
        action="store_true",
        help="emit one-line JSON",
    )
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.execute:
        parser.error(
            "--execute is not supported; use explicit --plan-only"
        )
    if not args.plan_only:
        parser.error("explicit --plan-only is required")

    try:
        repo_root = Path(__file__).resolve().parents[1]
        started_manifest = production_source_manifest(repo_root)
        gate = run_gate(
            repo_root,
            Path(__file__).resolve().with_name("sk_batch_gui.pyw"),
        )
        validate_production_source_manifest(
            started_manifest,
            gate.production_source_manifest,
            label="Planner compile-gate production source",
        )
        plan = build_pipeline_plan(
            args.root,
            phase=args.phase,
            targets=args.target,
            indexes=args.index,
        )
        validate_production_source_manifest(
            started_manifest,
            production_source_manifest(repo_root),
            label="Planner final production source",
        )
    except (CompileGateError, PipelinePlanInputError) as exc:
        print(f"SK Batch plan failed: {exc}", file=sys.stderr)
        return 2

    plan["compile_gate"] = {
        "source_count": gate.source_count,
        "contract_count": gate.contract_count,
        "production_source_revision":
            gate.production_source_manifest.content_hash,
    }
    print(json.dumps(
        plan,
        ensure_ascii=False,
        sort_keys=True,
        indent=None if args.compact else 2,
    ))
    return 3 if plan["status"] == "blocked" else 0


if __name__ == "__main__":
    raise SystemExit(main())
