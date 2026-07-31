"""Safe interactive recovery for a stale saved SpeedTree Node table.

The Modeler is opened with the affected SPM, but this module never edits the
SPM and never simulates UI input.  A user performs the save.  The watcher then
requires a stable content-hash change, re-audits the live document, and only
invokes an optional retry callback after the stale and target-binding gates
pass.
"""

import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path

try:
    from .pcg_texture_audit import live_generator_delivery_snapshot
except ImportError:  # Direct script execution from the tool directory.
    from pcg_texture_audit import live_generator_delivery_snapshot


SHA256_RE = re.compile(r"[0-9a-f]{64}")
RECOVERY_CONTRACT = "speedtree_stale_node_table_interactive_recovery_v1"


class StaleNodeTableRecoveryError(RuntimeError):
    """The interactive recovery could not produce verified live evidence."""


class StaleNodeTableRecoveryTimeout(StaleNodeTableRecoveryError):
    """The watched SPM did not reach a stable, valid changed state in time."""


def _mesh_id(value):
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def validate_repaired_snapshot(snapshot, expected_mesh_ids=()):
    """Return a fail-closed verdict for one fresh delivery snapshot."""
    expected = sorted({_mesh_id(value) for value in expected_mesh_ids} - {None})
    errors = []
    node_table = snapshot.get("node_table") or {}
    if node_table.get("stale") is not False:
        errors.append("node_table_still_stale")
    if int(node_table.get("orphan_node_count") or 0):
        errors.append("orphan_nodes_remain")
    if node_table.get("orphan_generator_guids"):
        errors.append("orphan_owners_remain")

    target_rows = []
    if expected:
        expected_set = set(expected)
        target_rows = [
            dict(row)
            for row in snapshot.get("leaf_generator_bindings") or []
            if _mesh_id(row.get("mesh_id")) in expected_set
        ]
        observed = sorted({_mesh_id(row.get("mesh_id")) for row in target_rows})
        live = sorted({
            _mesh_id(row.get("mesh_id"))
            for row in target_rows
            if row.get("export_participates") is True
        } - {None})
        if observed != expected:
            errors.append("required_target_binding_missing")
        if live != expected:
            errors.append("live_target_mesh_set_incomplete")
        for row in target_rows:
            if row.get("graph_visible") is not True:
                errors.append("target_binding_not_graph_visible")
            if int(row.get("generated_node_count") or 0) <= 0:
                errors.append("target_binding_has_no_eligible_nodes")
            if row.get("export_participates") is not True:
                errors.append("target_binding_not_export_participating")
            if row.get("export_evidence") != "node_table":
                errors.append("target_binding_evidence_not_current_node_table")
            if row.get("node_table_stale") is not False:
                errors.append("target_binding_reports_stale_node_table")
    else:
        observed = []
        live = []

    return {
        "contract": RECOVERY_CONTRACT,
        "valid": not errors,
        "errors": sorted(set(errors)),
        "spm_text_sha256": snapshot.get("spm_text_sha256"),
        "node_table": {
            "generator_count": node_table.get("generator_count"),
            "node_table_generator_count": node_table.get(
                "node_table_generator_count"
            ),
            "orphan_generator_guid_count": len(
                node_table.get("orphan_generator_guids") or []
            ),
            "orphan_node_count": int(node_table.get("orphan_node_count") or 0),
            "total_node_count": node_table.get("total_node_count"),
            "stale": node_table.get("stale"),
        },
        "expected_target_mesh_ids": expected,
        "observed_target_mesh_ids": observed,
        "live_export_participating_target_mesh_ids": live,
        "target_binding_count": len(target_rows),
    }


def launch_modeler_for_manual_save(speedtree_exe, spm_path):
    """Open a visible Modeler session without shell or input automation."""
    process = subprocess.Popen(
        [str(speedtree_exe), str(spm_path)],
        cwd=str(Path(spm_path).parent),
        stdin=subprocess.DEVNULL,
    )
    return process.pid


def wait_for_valid_resave(
    spm_path,
    baseline_sha256,
    expected_mesh_ids=(),
    *,
    timeout=7200,
    poll_interval=2.0,
    stable_reads=3,
    snapshot_fn=live_generator_delivery_snapshot,
    sleep_fn=time.sleep,
    monotonic_fn=time.monotonic,
):
    """Wait for a changed hash that remains stable and passes re-audit."""
    if not SHA256_RE.fullmatch(str(baseline_sha256 or "")):
        raise StaleNodeTableRecoveryError("baseline snapshot SHA-256 is invalid")
    if timeout <= 0 or poll_interval <= 0 or stable_reads <= 0:
        raise StaleNodeTableRecoveryError("watch timing values must be positive")

    deadline = monotonic_fn() + float(timeout)
    candidate_sha = None
    candidate_reads = 0
    last_errors = ["file_content_not_changed"]
    transient_error = None
    while monotonic_fn() < deadline:
        try:
            snapshot = snapshot_fn(spm_path)
            transient_error = None
        except (OSError, ValueError) as exc:
            transient_error = f"{type(exc).__name__}: {exc}"
            sleep_fn(poll_interval)
            continue
        sha256 = str(snapshot.get("spm_text_sha256") or "").casefold()
        if sha256 == str(baseline_sha256).casefold():
            candidate_sha = None
            candidate_reads = 0
            last_errors = ["file_content_not_changed"]
            sleep_fn(poll_interval)
            continue
        if not SHA256_RE.fullmatch(sha256):
            last_errors = ["changed_snapshot_sha256_invalid"]
            sleep_fn(poll_interval)
            continue
        if sha256 == candidate_sha:
            candidate_reads += 1
        else:
            candidate_sha = sha256
            candidate_reads = 1
        verdict = validate_repaired_snapshot(snapshot, expected_mesh_ids)
        last_errors = verdict["errors"]
        if candidate_reads >= stable_reads and verdict["valid"]:
            return snapshot, verdict
        sleep_fn(poll_interval)

    detail = ", ".join(last_errors)
    if transient_error:
        detail = f"{detail}; last read error: {transient_error}"
    raise StaleNodeTableRecoveryTimeout(
        f"SPM did not reach a stable valid resave before timeout: {detail}"
    )


def recover_stale_node_table(
    spm_path,
    speedtree_exe,
    expected_mesh_ids=(),
    *,
    timeout=7200,
    poll_interval=2.0,
    stable_reads=3,
    retry=None,
    snapshot_fn=live_generator_delivery_snapshot,
    launch_fn=launch_modeler_for_manual_save,
    sleep_fn=time.sleep,
    monotonic_fn=time.monotonic,
):
    """Open, watch, re-audit, and optionally retry after a verified save."""
    spm = Path(spm_path).expanduser().resolve(strict=False)
    executable = Path(speedtree_exe).expanduser().resolve(strict=False)
    if not spm.is_file():
        raise StaleNodeTableRecoveryError(f"SPM does not exist: {spm}")
    if spm.suffix.casefold() != ".spm":
        raise StaleNodeTableRecoveryError(f"recovery target is not an SPM: {spm}")
    if not executable.is_file():
        raise StaleNodeTableRecoveryError(
            f"SpeedTree Modeler executable does not exist: {executable}"
        )

    baseline = snapshot_fn(spm)
    baseline_sha256 = str(baseline.get("spm_text_sha256") or "").casefold()
    baseline_verdict = validate_repaired_snapshot(baseline, expected_mesh_ids)
    node_table = baseline.get("node_table") or {}
    if node_table.get("stale") is not True:
        if not baseline_verdict["valid"]:
            raise StaleNodeTableRecoveryError(
                "node table is not stale, but the requested delivery gate fails: "
                + ", ".join(baseline_verdict["errors"])
            )
        return {
            "contract": RECOVERY_CONTRACT,
            "status": "already_repaired",
            "spm": str(spm),
            "baseline_sha256": baseline_sha256,
            "after_sha256": baseline_sha256,
            "modeler_launched": False,
            "reaudit": baseline_verdict,
            "retry_invoked": False,
            "retry_result": None,
        }

    process_id = launch_fn(executable, spm)
    after, verdict = wait_for_valid_resave(
        spm,
        baseline_sha256,
        expected_mesh_ids,
        timeout=timeout,
        poll_interval=poll_interval,
        stable_reads=stable_reads,
        snapshot_fn=snapshot_fn,
        sleep_fn=sleep_fn,
        monotonic_fn=monotonic_fn,
    )
    retry_result = retry(after) if retry is not None else None
    return {
        "contract": RECOVERY_CONTRACT,
        "status": (
            "repaired_reaudited_and_retried"
            if retry is not None
            else "repaired_reaudit_valid"
        ),
        "spm": str(spm),
        "baseline_sha256": baseline_sha256,
        "after_sha256": after.get("spm_text_sha256"),
        "modeler_launched": True,
        "modeler_process_id": process_id,
        "reaudit": verdict,
        "retry_invoked": retry is not None,
        "retry_result": retry_result,
    }


def configured_speedtree_exe():
    """Use the existing SK Batch SpeedTree configuration."""
    from sk_batch.sk_common import load_config

    return Path(load_config().get("speedtree_exe") or "")


def build_parser():
    parser = argparse.ArgumentParser(
        description=(
            "Open stale SPMs in SpeedTree Modeler, wait for a manual save, "
            "and re-audit without editing XML or simulating UI input."
        )
    )
    parser.add_argument("spm", nargs="+", help="Affected operating SPM path")
    parser.add_argument(
        "--speedtree-exe",
        help="Modeler executable; defaults to the existing SK Batch config",
    )
    parser.add_argument(
        "--expected-mesh-id",
        action="append",
        type=int,
        default=[],
        help="Required live target Mesh ID; repeat for each target",
    )
    parser.add_argument("--timeout", type=float, default=7200)
    parser.add_argument("--poll-interval", type=float, default=2.0)
    parser.add_argument("--stable-reads", type=int, default=3)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    speedtree_exe = Path(args.speedtree_exe) if args.speedtree_exe else (
        configured_speedtree_exe()
    )
    results = []
    try:
        for spm in args.spm:
            results.append(
                recover_stale_node_table(
                    spm,
                    speedtree_exe,
                    args.expected_mesh_id,
                    timeout=args.timeout,
                    poll_interval=args.poll_interval,
                    stable_reads=args.stable_reads,
                )
            )
    except StaleNodeTableRecoveryError as exc:
        print(json.dumps({"status": "blocked", "error": str(exc)}, indent=2))
        return 2
    print(json.dumps({"status": "ok", "results": results}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
