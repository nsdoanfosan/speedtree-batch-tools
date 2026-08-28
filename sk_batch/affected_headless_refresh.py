"""Audit and force-refresh assets affected by native Leaf Mesh export changes."""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

import cluster_assembly_builder
import cluster_fleet_push
import exact_push


DEFAULT_ROOT = cluster_fleet_push.DEFAULT_ROOT
LOG_DIR = cluster_fleet_push.LOG_DIR
LEAF_MESH_RTTI = ".?AVCLeafMeshNode@@"
BRANCH_RTTI = ".?AVCBranchNode@@"
BASE_RTTI = ".?AVCBaseNode@@"

# The Assembly builder refuses any manifest that is not on the current
# placement contract, so an older manifest can never describe the geometry the
# current code would produce.  Sourced from the builder itself to stop drift.
PLACEMENT_CONTRACT_VERSION = cluster_assembly_builder.PLACEMENT_CONTRACT_VERSION

# Emitted by the native hook (``speedtree_collision_cli/hook.cpp``, the
# ``"schema_version": 5`` receipt literal).  A receipt written by an older hook
# predates the current bone rules, so its bone records cannot be trusted even
# when they look well formed.  ``test_native_receipt_schema_version_matches_hook``
# fails if the hook and this constant ever disagree.
NATIVE_RECEIPT_SCHEMA_VERSION = 5

# Assembly manifests come in two shapes.  ``ready`` manifests describe cluster
# placement and carry ``placement_contract``.  ``pass_through`` manifests keep
# the full Skeletal Mesh and never carry one.  Only the former can be judged
# against PLACEMENT_CONTRACT_VERSION.
PLACEMENT_MANIFEST_STATUS = "ready"
PASS_THROUGH_MANIFEST_STATUS = "pass_through"


def native_receipt_path(target):
    spm = Path(target["spm"])
    return spm.parent / "fbx" / f"{target['stem']}.speedtree_native_receipt.json"


def audit_target(target):
    receipt_path = native_receipt_path(target)
    reasons = []
    receipt = None
    error = ""
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        error = str(exc)
        reasons.append("native_receipt_missing_or_unreadable")

    leaf_instances = []
    zero_bone_leaf_instances = []
    base_ref_branch_leaf_instances = []
    base_ref_branch_zero_bone_leaf_instances = []
    native_bone_count = None
    if receipt is not None:
        native_bone_count = len(receipt.get("bones") or [])
        leaf_instances = [
            row
            for row in receipt.get("generated_instances") or []
            if row.get("source_rtti") == LEAF_MESH_RTTI
        ]
        zero_bone_leaf_instances = [
            row for row in leaf_instances if row.get("source_bone_id") == 0
        ]
        base_ref_branch_leaf_instances = [
            row
            for row in leaf_instances
            if any(
                ancestor.get("source_rtti") == BRANCH_RTTI
                for ancestor in row.get("ancestor_chain") or []
            )
            and any(
                ancestor.get("source_rtti") == BASE_RTTI
                for ancestor in row.get("ancestor_chain") or []
            )
        ]
        base_ref_branch_zero_bone_leaf_instances = [
            row
            for row in base_ref_branch_leaf_instances
            if row.get("source_bone_id") == 0
        ]
        if receipt.get("schema_version") != NATIVE_RECEIPT_SCHEMA_VERSION:
            reasons.append("native_receipt_schema_stale")
        if zero_bone_leaf_instances:
            # The permanent rule is that no exported Leaf Mesh keeps bone id 0.
            # A receipt that still carries them was written before the rule
            # existed, whatever its other counts look like.
            reasons.append("zero_bone_leaf_mesh_present")
        if native_bone_count == 0:
            reasons.append("parsed_native_bone_count_zero")
        if base_ref_branch_zero_bone_leaf_instances:
            # Narrowed from "has any BaseRef branch leaf" to "still has one on
            # bone id 0".  The broad form reselected an asset that had already
            # been corrected, so the refresh never converged.
            reasons.append("baseref_branch_leaf_mesh_refresh_scope")
        # A single synthetic bone at id SYNTHETIC_BONE_ID_BASE is the *result*
        # of the zero-bone rule, not a defect: selecting on it reselected every
        # already-corrected boneless asset forever.  The pre-fix condition is
        # ``parsed_native_bone_count_zero`` above.

    # Grass used to be selected by stem name because an old receipt could not
    # expose a Leaf Mesh row at all.  ``native_receipt_schema_stale`` now covers
    # exactly that case from the receipt itself, so the name filter is both
    # redundant and non-converging: it reselected correct grass on every run.
    # Selection is structural only, matching the permanent SPM-parsed rules.

    manifest_path = Path(target["manifest"])
    manifest_placement_version = None
    manifest_status = None
    manifest_error = ""
    try:
        manifest_payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        manifest_error = str(exc)
        reasons.append("assembly_manifest_missing_or_unreadable")
    else:
        manifest_status = manifest_payload.get("status")
        placement = manifest_payload.get("placement_contract")
        if isinstance(placement, dict):
            manifest_placement_version = placement.get("version")
        # Only a ``ready`` manifest describes cluster placement, and only it
        # carries ``placement_contract``.  A ``pass_through`` manifest preserves
        # the full Skeletal Mesh instead, so it has no placement contract by
        # design and must not be judged against the placement version -- doing
        # so would reselect it on every run and never converge.  Its staleness
        # is covered by the receipt schema and zero-bone Leaf Mesh reasons.
        if manifest_status == PLACEMENT_MANIFEST_STATUS:
            if manifest_placement_version != PLACEMENT_CONTRACT_VERSION:
                # cluster_assembly_builder rejects a manifest off the current
                # placement contract, so the shipped Assembly cannot match what
                # the current code builds: prototypes, placement and the
                # material table are all described by the older contract.
                reasons.append("assembly_placement_contract_stale")

    return {
        "stem": target["stem"],
        "spm": str(Path(target["spm"]).resolve()),
        "manifest": str(Path(target["manifest"]).resolve()),
        "receipt": str(receipt_path.resolve()),
        "receipt_schema_version": (
            receipt.get("schema_version") if receipt is not None else None
        ),
        "receipt_error": error,
        "manifest_error": manifest_error,
        "manifest_status": manifest_status,
        "manifest_placement_contract_version": manifest_placement_version,
        "expected_placement_contract_version": PLACEMENT_CONTRACT_VERSION,
        "expected_native_receipt_schema_version": NATIVE_RECEIPT_SCHEMA_VERSION,
        "native_bone_count": native_bone_count,
        "leaf_mesh_instance_count": len(leaf_instances),
        "zero_bone_leaf_mesh_instance_count": len(zero_bone_leaf_instances),
        "baseref_branch_zero_bone_leaf_mesh_instance_count": len(
            base_ref_branch_zero_bone_leaf_instances
        ),
        "baseref_branch_leaf_mesh_instance_count": len(
            base_ref_branch_leaf_instances
        ),
        "selected": bool(reasons),
        "reasons": reasons,
    }


def discover_affected_targets(root: Path):
    targets, missing = cluster_fleet_push.discover_current_cluster_targets(root)
    audited = [audit_target(target) for target in targets]
    selected = [row for row in audited if row["selected"]]
    return selected, audited, missing


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description=(
            "Audit current production Assemblies and force-refresh every asset "
            "affected by the native root-zone Leaf Mesh bone fix, including "
            "the complete grass verification scope, without GUI control"
        )
    )
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--log-dir", type=Path, default=LOG_DIR)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--resume-run-id",
        help=(
            "Reuse prepared reports and the Unreal checkpoint from an interrupted "
            "run instead of rebuilding Blender/FBX outputs"
        ),
    )
    parser.add_argument(
        "--reset-item-retries",
        action="store_true",
        help=(
            "Before resuming, clear the crash budget of items left in "
            "unreal_crash/manual_required so a deliberate operator stop does "
            "not permanently abandon them; imported_ok items stay skipped"
        ),
    )
    parser.add_argument("--p4-client", default=cluster_fleet_push.DEFAULT_P4_CLIENT)
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    args.log_dir.mkdir(parents=True, exist_ok=True)
    run_id = args.resume_run_id or datetime.now().strftime("%Y%m%d_%H%M%S")
    inventory_path = args.log_dir / f"affected_headless_refresh_{run_id}.json"
    if args.reset_item_retries and not args.resume_run_id:
        raise SystemExit("--reset-item-retries requires --resume-run-id")
    if args.resume_run_id:
        if args.dry_run:
            raise SystemExit("--resume-run-id cannot be combined with --dry-run")
        if not inventory_path.is_file():
            raise SystemExit(f"resume inventory does not exist: {inventory_path}")
        inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
        selected = inventory.get("selected") or []
        audited = inventory.get("audited") or []
        missing = inventory.get("discovery_missing") or []
        inventory["status"] = "resuming"
        inventory["resumed_at"] = datetime.now().isoformat(timespec="seconds")
    else:
        selected, audited, missing = discover_affected_targets(args.root)
        inventory = {
            "schema_version": 1,
            "status": "dry_run" if args.dry_run else "selected",
            "root": str(args.root.expanduser().resolve()),
            "selection_policy": [
                "native_receipt_missing_or_unreadable",
                "native_receipt_schema_stale",
                "zero_bone_leaf_mesh_present",
                "assembly_manifest_missing_or_unreadable",
                "assembly_placement_contract_stale",
                "parsed_native_bone_count_zero",
                "baseref_branch_leaf_mesh_refresh_scope",
            ],
            "selection_is_converging": True,
            "force_native_export": True,
            "selected": selected,
            "audited": audited,
            "discovery_missing": missing,
        }
    inventory_path.write_text(
        json.dumps(inventory, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"SK_AFFECTED_HEADLESS_AUDITED={len(audited)}")
    print(f"SK_AFFECTED_HEADLESS_TARGETS={len(selected)}")
    print(f"SK_AFFECTED_HEADLESS_DISCOVERY_MISSING={len(missing)}")
    print(f"SK_AFFECTED_HEADLESS_INVENTORY={inventory_path.resolve()}")
    if not selected:
        print("No stale or explicit verification targets were found.")
        return 0

    results = []
    failed = 0
    prepared = []
    if args.resume_run_id:
        for target in selected:
            prepared_report = (
                args.log_dir
                / f"{target['stem']}_affected_prepared_{run_id}.json"
            )
            ok = prepared_report.is_file()
            results.append({
                "stem": target["stem"],
                "spm": target["spm"],
                "returncode": 0 if ok else 1,
                "status": "prepared" if ok else "failed",
            })
            if ok:
                prepared.append(json.loads(
                    prepared_report.read_text(encoding="utf-8")
                ))
            else:
                failed += 1
        print(
            f"SK_AFFECTED_HEADLESS_RESUMED_PREPARED={len(prepared)}",
            flush=True,
        )
    for index, target in enumerate(selected, 1):
        if args.resume_run_id:
            break
        print(f"[{index}/{len(selected)}] EXACT_PUSH {target['stem']}", flush=True)
        push_args = [
            "--spm",
            target["spm"],
            "--log-dir",
            str(args.log_dir.expanduser().resolve()),
            "--transport",
            "headless",
        ]
        prepared_report = (
            args.log_dir / f"{target['stem']}_affected_prepared_{run_id}.json"
        )
        if args.dry_run:
            push_args.append("--dry-run")
        else:
            push_args.extend([
                "--defer-unreal",
                "--prepared-report",
                str(prepared_report.resolve()),
            ])
        returncode = exact_push.main(push_args)
        results.append({
            "stem": target["stem"],
            "spm": target["spm"],
            "returncode": returncode,
            "status": "ok" if returncode == 0 else "failed",
        })
        failed += int(returncode != 0)
        if returncode == 0 and not args.dry_run:
            prepared.append(json.loads(
                prepared_report.read_text(encoding="utf-8")
            ))

    if prepared and not args.dry_run:
        batch_manifest_path = args.log_dir / (
            f"affected_exact_push_batch_{run_id}_manifest.json"
        )
        batch_checkpoint_path = args.log_dir / (
            f"affected_exact_push_batch_{run_id}_checkpoint.json"
        )
        batch_report_path = args.log_dir / (
            f"affected_exact_push_batch_{run_id}.json"
        )
        items = []
        for row in prepared:
            source_manifest = Path(row["outputs"]["manifest"])
            payload = json.loads(source_manifest.read_text(encoding="utf-8"))
            items.extend(payload.get("items") or [])
        batch_manifest_path.write_text(
            json.dumps({
                "schema_version": 1,
                "created_at": datetime.now().isoformat(timespec="seconds"),
                "checkpoint_path": str(batch_checkpoint_path.resolve()),
                "report_path": str(batch_report_path.resolve()),
                "max_item_crash_retries": 2,
                "items": items,
            }, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(
            f"SK_AFFECTED_HEADLESS_UNREAL_BATCH_ITEMS={len(items)}",
            flush=True,
        )
        if args.reset_item_retries and batch_checkpoint_path.is_file():
            retry_reset = exact_push.reset_checkpoint_item_retries(
                batch_checkpoint_path
            )
            inventory["unreal_batch_retry_reset"] = retry_reset
            print(
                "SK_AFFECTED_HEADLESS_RETRY_RESET="
                f"{len(retry_reset['reset'])}",
                flush=True,
            )
        batch_result = exact_push.run_headless_manifest(
            batch_manifest_path,
            batch_checkpoint_path,
            batch_report_path,
        )
        by_stem = {row["stem"]: row for row in results}
        for row in prepared:
            outputs = row["outputs"]
            merged = exact_push.merge_unreal_result(outputs, batch_result)
            stem = Path(outputs["queue_id"]).stem
            ok = merged.get("status") == "ok"
            by_stem[stem]["status"] = "ok" if ok else "failed"
            by_stem[stem]["returncode"] = 0 if ok else 1
            failed += int(not ok)
        inventory["unreal_batch"] = {
            "manifest": str(batch_manifest_path.resolve()),
            "checkpoint": str(batch_checkpoint_path.resolve()),
            "report": str(batch_report_path.resolve()),
            "item_count": len(items),
        }

    inventory["status"] = (
        "dry_run" if args.dry_run else ("ok" if failed == 0 else "failed")
    )
    inventory["results"] = results
    inventory["failed_count"] = failed
    inventory_path.write_text(
        json.dumps(inventory, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"SK_AFFECTED_HEADLESS_VERIFIED={len(results) - failed}")
    print(f"SK_AFFECTED_HEADLESS_FAILED={failed}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
