"""Audit and force-refresh assets affected by native Leaf Mesh export changes."""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime
from pathlib import Path

import cluster_assembly_builder
import cluster_fleet_push


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


def deployment_receipt_path(target):
    spm = Path(target["spm"])
    return spm.parent / "fbx" / f"{target['stem']}.unreal_deployment_receipt.json"


def _sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _vertex_ranges_are_exact_and_ordered(row):
    """Mirror the receipt contract check in speedtree_native_receipt.

    Ranges must be ascending, non-overlapping and non-empty.  Geometry bounds
    are not re-checked here; the loader owns that.
    """
    ranges = row.get("vertex_ranges") or []
    if not ranges:
        return False
    previous_last = -1
    for value in ranges:
        try:
            first, last = int(value[0]), int(value[1])
        except (IndexError, TypeError, ValueError):
            return False
        if first <= previous_last or first < 0 or last < first:
            return False
        previous_last = last
    return True


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
    missing_source_object_identity_count = 0
    if receipt is not None:
        generated_instances = receipt.get("generated_instances") or []
        native_bone_count = len(receipt.get("bones") or [])
        leaf_instances = [
            row
            for row in generated_instances
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
        missing_source_object_identity_count = sum(
            "native_source_object_id" not in row
            for row in generated_instances
        )
        if missing_source_object_identity_count:
            # v15 records the serializer's actual runtime source-object
            # identity.  node_guid and native_instance_id both collide in real
            # receipts, so an older row cannot safely prove which per-bone
            # records belong to one node.
            reasons.append("native_source_object_identity_missing")
        if zero_bone_leaf_instances:
            # The permanent rule is that no exported Leaf Mesh keeps bone id 0.
            # A receipt that still carries them was written before the rule
            # existed, whatever its other counts look like.
            reasons.append("zero_bone_leaf_mesh_present")
        invalid_range_instances = [
            row
            for row in generated_instances
            if not _vertex_ranges_are_exact_and_ordered(row)
        ]
        if invalid_range_instances:
            # speedtree_native_receipt rejects the whole receipt when any
            # instance carries overlapping or unordered vertex ranges, so the
            # asset cannot be reassembled from it.  A receipt can otherwise look
            # completely current, which is how assets imported on top of an
            # invalid receipt escaped selection.
            reasons.append("native_vertex_ranges_not_exact_and_ordered")
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
    manifest_payload = None
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

    deployment_path = deployment_receipt_path(target)
    deployment_error = ""
    if receipt is not None and manifest_payload is not None:
        try:
            deployment = json.loads(deployment_path.read_text(encoding="utf-8"))
            if deployment.get("status") != "imported_ok":
                raise ValueError("deployment status is not imported_ok")
            if deployment.get("native_receipt_sha256") != _sha256(receipt_path):
                raise ValueError("native receipt hash changed after Unreal import")
            if deployment.get("assembly_manifest_sha256") != _sha256(manifest_path):
                raise ValueError("Assembly manifest hash changed after Unreal import")
        except (OSError, ValueError, TypeError) as exc:
            deployment_error = str(exc)
            reasons.append("unreal_deployment_receipt_missing_or_stale")

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
        "deployment_receipt": str(deployment_path.resolve()),
        "deployment_receipt_error": deployment_error,
        "manifest_status": manifest_status,
        "manifest_placement_contract_version": manifest_placement_version,
        "expected_placement_contract_version": PLACEMENT_CONTRACT_VERSION,
        "expected_native_receipt_schema_version": NATIVE_RECEIPT_SCHEMA_VERSION,
        "native_bone_count": native_bone_count,
        "missing_native_source_object_identity_count": (
            missing_source_object_identity_count
        ),
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
        originally_selected = inventory.get("selected") or []
        resume_audits = [audit_target(target) for target in originally_selected]
        selected = [row for row in resume_audits if row["selected"]]
        inventory["resume_skipped_current"] = [
            row["spm"] for row in resume_audits if not row["selected"]
        ]
        inventory["selected"] = selected
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
                "native_source_object_identity_missing",
                "zero_bone_leaf_mesh_present",
                "native_vertex_ranges_not_exact_and_ordered",
                "assembly_manifest_missing_or_unreadable",
                "assembly_placement_contract_stale",
                "parsed_native_bone_count_zero",
                "baseref_branch_leaf_mesh_refresh_scope",
                "unreal_deployment_receipt_missing_or_stale",
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

    # Use the dependency-aware fleet for the actual work.  It prepares every
    # distinct Cluster provider first, then every consuming root, and finally
    # submits one ordered Unreal manifest.  The former direct exact-push loop
    # skipped providers, leaving a stale provider FBX/receipt behind a newly
    # exported root and causing the Assembly validator to reject authored bone
    # weights.  Cluster providers retain their established single-axis-bone
    # policy; force-native-export only refreshes that policy's receipt/artifact.
    fleet_args = [
        "--root", str(args.root.expanduser().resolve()),
        "--log-dir", str(args.log_dir.expanduser().resolve()),
        "--p4-client", args.p4_client,
        "--run-id", run_id,
        "--force-native-export",
        "--push-pass-through-roots",
        "--fail-fast",
    ]
    for target in selected:
        fleet_args.extend(["--target-spm", target["spm"]])
    if args.dry_run:
        fleet_args.append("--dry-run")
    if args.reset_item_retries:
        fleet_args.append("--reset-item-retries")
    if args.resume_run_id:
        fleet_args.append("--resume-prepared")
    returncode = cluster_fleet_push.main(fleet_args)
    fleet_report_path = args.log_dir / f"cluster_fleet_push_{run_id}.json"
    fleet = json.loads(fleet_report_path.read_text(encoding="utf-8"))
    selected_by_spm = {
        str(Path(row["spm"]).resolve()).casefold(): row for row in selected
    }
    for result in fleet.get("results") or []:
        if result.get("status") != "verified_in_unreal":
            continue
        target = selected_by_spm.get(
            str(Path(result["spm"]).resolve()).casefold()
        )
        if target is None:
            continue
        native_path = native_receipt_path(target)
        manifest_path = Path(target["manifest"])
        marker_path = deployment_receipt_path(target)
        marker_path.parent.mkdir(parents=True, exist_ok=True)
        marker = {
            "schema_version": 1,
            "status": "imported_ok",
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "spm": str(Path(target["spm"]).resolve()),
            "native_receipt": str(native_path.resolve()),
            "native_receipt_sha256": _sha256(native_path),
            "assembly_manifest": str(manifest_path.resolve()),
            "assembly_manifest_sha256": _sha256(manifest_path),
            "cluster_fleet_report": str(fleet_report_path.resolve()),
            "exact_push_report": result.get("report"),
        }
        temporary = marker_path.with_suffix(marker_path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(marker, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        temporary.replace(marker_path)
    inventory["execution_policy"] = (
        "dependency_aware_cluster_providers_then_roots_single_unreal_batch"
    )
    inventory["cluster_fleet_report"] = str(fleet_report_path.resolve())
    inventory["provider_dependencies"] = fleet.get("provider_dependencies") or []
    inventory["provider_results"] = fleet.get("provider_results") or []
    inventory["results"] = fleet.get("results") or []
    inventory["failed_count"] = int(fleet.get("failed_count") or 0)
    inventory["provider_failed_count"] = int(
        fleet.get("provider_failed_count") or 0
    )
    inventory["status"] = "dry_run" if args.dry_run else fleet.get("status")
    inventory_path.write_text(
        json.dumps(inventory, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"SK_AFFECTED_HEADLESS_VERIFIED={fleet.get('verified_count', 0)}")
    print(f"SK_AFFECTED_HEADLESS_FAILED={inventory['failed_count']}")
    print(
        "SK_AFFECTED_HEADLESS_PROVIDER_FAILED="
        f"{inventory['provider_failed_count']}"
    )
    return returncode


if __name__ == "__main__":
    raise SystemExit(main())
