from __future__ import annotations

import json
import hashlib
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


SK_BATCH = Path(__file__).resolve().parents[1]
if str(SK_BATCH) not in sys.path:
    sys.path.insert(0, str(SK_BATCH))

import finalize_affected_refresh as finalizer  # noqa: E402


def _write_json(path: Path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def _write_inventory(root: Path, count=80, *, duplicate=False):
    rows = []
    for index in range(1, count + 1):
        stem = f"SK_tree_{index:03d}"
        asset = root / f"asset_{index:03d}"
        spm = asset / f"{stem}.spm"
        if duplicate and index == count:
            spm = Path(rows[0]["spm"])
        manifest = asset / "assembly" / f"{stem}_cluster_assembly_bindings.json"
        native = asset / "fbx" / f"{stem}.speedtree_native_receipt.json"
        deployment = asset / "fbx" / f"{stem}.unreal_deployment_receipt.json"
        spm.parent.mkdir(parents=True, exist_ok=True)
        spm.write_bytes(f"spm-{index}".encode())
        _write_json(manifest, {"status": "ready", "index": index})
        _write_json(native, {"schema_version": 5, "index": index})
        rows.append({
            "stem": stem,
            "spm": str(spm),
            "manifest": str(manifest),
            "receipt": str(native),
            "deployment_receipt": str(deployment),
            # Deliberately alternate this mutable resume diagnostic.
            "selected": index % 2 == 0,
        })
    path = root / "affected.json"
    _write_json(path, {"schema_version": 1, "selected": rows})
    return path, rows


def _fresh_item(spm: Path, *, built_assembly=True):
    mesh_path = f"/Game/Test/{spm.stem}"
    contract = {
        "schema_version": 1,
        "source_fingerprint": "source",
        "blend": str(spm.with_suffix(".blend")),
        "unit_name": spm.stem,
        "unreal_folder": "/Game/Test/",
        "mesh_path": mesh_path,
        "assets": [],
        "exported_files": [],
        "handoff_files": [],
        "wind_file": None,
        "wind_policy": {"required": True},
        "code_files": [],
        "cluster_assembly": ({"manifest": {}} if built_assembly else None),
        "dependency_orchestrated": True,
        "material_asset_scope": {"mode": "exact"},
        "export_contracts": {"skeleton_root": {"status": "ok"}},
    }
    return {
        **contract,
        "queue_id": str(spm),
        "fingerprint": finalizer.stable_fingerprint(contract),
        "report_path": str(spm.with_name(f"{spm.stem}_unreal.json")),
        "export_report_path": str(spm.with_name(f"{spm.stem}_exact.json")),
    }


def _unreal_state(item, started_at, completed_at):
    skeleton_hash = "a" * 40
    mesh = item["mesh_path"]
    return {
        "status": "imported_ok",
        "fingerprint": item["fingerprint"],
        "started_at": started_at.isoformat(),
        "updated_at": completed_at.isoformat(),
        "completed_at": completed_at.isoformat(),
        "materials": {
            "mesh": mesh,
            "section_material_validation": {"status": "ok"},
        },
        "skeleton": {
            "final_skeleton_contract": {
                "hash": skeleton_hash,
                "bone_count": 10,
            }
        },
        "final_skeleton_saved": {
            "mesh": mesh,
            "skeleton": f"{mesh}_Skeleton_{skeleton_hash[:12]}",
        },
        "cluster_assembly": {
            "status": "ok",
            "build": {
                "status": "ok",
                "parts": [{"bindings": 2}],
                "binding_count": 2,
                "final_skeleton_bones": 10,
                "dynamic_wind": {
                    "success": True,
                    "skeleton_hash": skeleton_hash,
                },
                "provenance": {"success": True},
            },
        },
    }


def _write_bundle(
    log_dir: Path,
    run_id: str,
    ordinal: int,
    target,
    *,
    success=True,
    exact_report=True,
    started_at=None,
):
    spm = Path(target["spm"])
    item = _fresh_item(spm)
    base = f"{target['stem']}_exact_push_fleet_{run_id}_{ordinal:03d}"
    started_at = started_at or datetime(2026, 8, 30, 1, tzinfo=timezone.utc)
    completed_at = started_at + timedelta(minutes=1)
    manifest = log_dir / f"{base}_manifest.json"
    checkpoint = log_dir / f"{base}_checkpoint.json"
    batch = log_dir / f"{base}_batch.json"
    item_report = log_dir / f"{base}_unreal.json"
    report = log_dir / f"{base}.json"
    assembly = log_dir / (
        base.replace("_exact_push_fleet_", "_fleet_assembly_", 1) + ".json"
    )
    _write_json(manifest, {
        "schema_version": 1,
        "checkpoint_path": str(checkpoint),
        "report_path": str(batch),
        "items": [item],
    })
    if success:
        state = _unreal_state(item, started_at, completed_at)
        state["manifest"] = str(manifest)
        state["report"] = str(item_report)
        _write_json(checkpoint, {
            "schema_version": 1,
            "manifest": str(manifest),
            "complete": True,
            "current_item": None,
            "items": {str(spm): state},
        })
        _write_json(batch, {
            "schema_version": 1,
            "status": "complete",
            "manifest": str(manifest),
            "checkpoint": str(checkpoint),
            "completed_at": completed_at.isoformat(),
            "counts": {"imported_ok": 1},
            "items": {str(spm): state},
        })
        _write_json(item_report, {
            **state,
            "queue_id": str(spm),
            "checkpoint": str(checkpoint),
        })
        if exact_report:
            _write_json(report, {
                "status": "ok",
                "manifest_fingerprint": item["fingerprint"],
                "unreal_result": state,
            })
    else:
        state = {
            "status": "importing",
            "fingerprint": item["fingerprint"],
            "started_at": started_at.isoformat(),
            "updated_at": completed_at.isoformat(),
            "manifest": str(manifest),
            "report": str(item_report),
        }
        _write_json(checkpoint, {
            "schema_version": 1,
            "manifest": str(manifest),
            "complete": False,
            "current_item": str(spm),
            "items": {str(spm): state},
        })
        if exact_report:
            _write_json(report, {"status": "failed", "stage": "rpc_ingest"})
    _write_json(assembly, {"status": "ok", "speedtree_spm": str(spm)})
    return finalizer.ExactBundle(
        run_id=run_id,
        bundle_id=base,
        manifest=manifest,
        checkpoint=checkpoint,
        batch=batch,
        item_report=item_report,
        exact_report=report,
        assembly_report=assembly,
        fleet_report=log_dir / f"cluster_fleet_push_{run_id}.json",
    )


def _target_from_inventory(inventory, index=0):
    return inventory.targets[index]


def _bind_handoff_artifact(bundle, artifact: Path):
    payload = artifact.read_bytes()
    record = {
        "path": str(artifact.resolve()),
        "size": len(payload),
        "mtime_ns": artifact.stat().st_mtime_ns,
        "fingerprint": hashlib.blake2b(payload, digest_size=16).hexdigest(),
    }
    manifest = json.loads(bundle.manifest.read_text(encoding="utf-8"))
    item = manifest["items"][0]
    item["handoff_files"] = [record]
    contract = {
        key: item.get(key) for key in finalizer.FRESH_PUSH_CONTRACT_KEYS
    }
    fingerprint = finalizer.stable_fingerprint(contract)
    item["fingerprint"] = fingerprint
    _write_json(bundle.manifest, manifest)
    for path in (bundle.checkpoint, bundle.batch, bundle.item_report):
        if not path.is_file():
            continue
        evidence = json.loads(path.read_text(encoding="utf-8"))
        if path == bundle.item_report:
            evidence["fingerprint"] = fingerprint
        else:
            states = evidence.get("items") or {}
            for state in states.values():
                state["fingerprint"] = fingerprint
        _write_json(path, evidence)
    if bundle.exact_report.is_file():
        report = json.loads(bundle.exact_report.read_text(encoding="utf-8"))
        if report.get("status") == "ok":
            report["manifest_fingerprint"] = fingerprint
            if isinstance(report.get("unreal_result"), dict):
                report["unreal_result"]["fingerprint"] = fingerprint
        _write_json(bundle.exact_report, report)
    return fingerprint


def _validate_fixture_artifacts(item):
    for record in item.get("handoff_files") or []:
        path = Path(record["path"])
        payload = path.read_bytes()
        if len(payload) != record["size"]:
            raise finalizer.PushUnrealRecoveryError("handoff file size changed")
        actual = hashlib.blake2b(payload, digest_size=16).hexdigest()
        if actual != record["fingerprint"]:
            raise finalizer.PushUnrealRecoveryError("handoff file content changed")


def test_inventory_uses_selected_array_order_not_mutated_flags(tmp_path):
    path, rows = _write_inventory(tmp_path, count=3)
    inventory = finalizer.load_ordered_inventory(path, expected_count=3)

    assert [row.stem for row in inventory.targets] == [
        row["stem"] for row in rows
    ]
    assert [row.ordinal for row in inventory.targets] == [1, 2, 3]


def test_inventory_requires_exact_unique_canonical_spms(tmp_path):
    path, _ = _write_inventory(tmp_path, count=3, duplicate=True)

    with pytest.raises(finalizer.FinalizationError, match="duplicate inventory SPM"):
        finalizer.load_ordered_inventory(path, expected_count=3)


def test_stable_source_snapshot_detects_change_after_audit(tmp_path):
    path = _write_json(tmp_path / "source.json", {"status": "complete"})
    snapshot = finalizer.read_stable_json(path)
    _write_json(path, {"status": "changed"})

    with pytest.raises(finalizer.FinalizationError, match="changed after audit"):
        finalizer.verify_snapshot_unchanged(snapshot)


def test_active_running_fleet_writer_is_blocked(tmp_path):
    _write_json(
        tmp_path / "cluster_fleet_push_tail054.json",
        {"status": "running", "results": []},
    )

    with pytest.raises(finalizer.FinalizationError, match="ACTIVE_SOURCE_WRITER"):
        finalizer.load_fleet_sources(tmp_path, ["tail054"])


def test_explicit_abandoned_running_fleet_can_be_audited(tmp_path):
    _write_json(
        tmp_path / "cluster_fleet_push_original.json",
        {"status": "running", "results": []},
    )

    sources = finalizer.load_fleet_sources(
        tmp_path,
        ["original"],
        allow_abandoned_run_ids=["original"],
    )
    assert "original" in sources


def test_exact_bundle_recomputes_internal_fingerprint(tmp_path):
    inventory_path, rows = _write_inventory(tmp_path / "inventory", count=1)
    inventory = finalizer.load_ordered_inventory(inventory_path, expected_count=1)
    target = _target_from_inventory(inventory)
    bundle = _write_bundle(tmp_path / "logs", "run", 1, rows[0])
    manifest = json.loads(bundle.manifest.read_text(encoding="utf-8"))
    manifest["items"][0]["fingerprint"] = "wrong"
    _write_json(bundle.manifest, manifest)

    verdict = finalizer.validate_exact_bundle(
        bundle, target, verify_artifacts=False
    )
    assert verdict.classification == finalizer.INVALID
    assert "fingerprint" in verdict.errors[0]


def test_discovery_does_not_treat_tail_run_as_parent_run(tmp_path):
    parent = tmp_path / "SK_tree_exact_push_fleet_run_001_manifest.json"
    tail = tmp_path / "SK_tree_exact_push_fleet_run_tail054_001_manifest.json"
    provider = tmp_path / "SK_branch_exact_push_fleet_run_provider_001_manifest.json"
    for path in (parent, tail, provider):
        _write_json(path, {"schema_version": 1, "items": []})

    parent_bundles = finalizer.discover_exact_bundles(tmp_path, ["run"])
    tail_bundles = finalizer.discover_exact_bundles(tmp_path, ["run_tail054"])

    assert [row.manifest for row in parent_bundles] == [parent.resolve()]
    assert [row.manifest for row in tail_bundles] == [tail.resolve()]


def test_exact_bundle_rejects_checkpoint_batch_fingerprint_mismatch(tmp_path):
    inventory_path, rows = _write_inventory(tmp_path / "inventory", count=1)
    inventory = finalizer.load_ordered_inventory(inventory_path, expected_count=1)
    target = _target_from_inventory(inventory)
    bundle = _write_bundle(tmp_path / "logs", "run", 1, rows[0])
    batch = json.loads(bundle.batch.read_text(encoding="utf-8"))
    batch["items"][str(target.spm)]["fingerprint"] = "different"
    _write_json(bundle.batch, batch)

    verdict = finalizer.validate_exact_bundle(
        bundle, target, verify_artifacts=False
    )
    assert verdict.classification == finalizer.INVALID
    assert "batch fingerprint" in verdict.errors[0]


def test_root035_missing_exact_report_derives_terminal_success(tmp_path):
    inventory_path, rows = _write_inventory(tmp_path / "inventory", count=1)
    inventory = finalizer.load_ordered_inventory(inventory_path, expected_count=1)
    target = _target_from_inventory(inventory)
    bundle = _write_bundle(
        tmp_path / "logs", "original", 35, rows[0], exact_report=False
    )

    verdict = finalizer.validate_exact_bundle(
        bundle, target, verify_artifacts=False
    )
    assert verdict.classification == finalizer.SUCCESS
    assert verdict.verification_basis == "derived_atomic_ingest_bundle_v1"


def test_incomplete_checkpoint_is_not_success(tmp_path):
    inventory_path, rows = _write_inventory(tmp_path / "inventory", count=1)
    inventory = finalizer.load_ordered_inventory(inventory_path, expected_count=1)
    target = _target_from_inventory(inventory)
    bundle = _write_bundle(
        tmp_path / "logs", "failed", 1, rows[0], success=False
    )

    verdict = finalizer.validate_exact_bundle(
        bundle, target, verify_artifacts=False
    )
    assert verdict.classification == finalizer.AMBIGUOUS_FAILURE


def test_later_validated_success_seals_earlier_failure_with_new_fingerprint(
    tmp_path,
):
    inventory_path, rows = _write_inventory(tmp_path / "inventory", count=1)
    inventory = finalizer.load_ordered_inventory(inventory_path, expected_count=1)
    target = _target_from_inventory(inventory)
    first = datetime(2026, 8, 30, 1, tzinfo=timezone.utc)
    failed_bundle = _write_bundle(
        tmp_path / "logs",
        "tail036",
        19,
        rows[0],
        success=False,
        started_at=first,
    )
    # Rewriting the source timestamp changes the fresh contract fingerprint.
    successful_bundle = _write_bundle(
        tmp_path / "logs",
        "tail054",
        1,
        rows[0],
        success=True,
        started_at=first + timedelta(minutes=10),
    )
    success_manifest = json.loads(
        successful_bundle.manifest.read_text(encoding="utf-8")
    )
    success_manifest["items"][0]["source_fingerprint"] = "fresh-second-run"
    contract = {
        key: success_manifest["items"][0].get(key)
        for key in finalizer.FRESH_PUSH_CONTRACT_KEYS
    }
    new_fingerprint = finalizer.stable_fingerprint(contract)
    old_fingerprint = json.loads(
        failed_bundle.manifest.read_text(encoding="utf-8")
    )["items"][0]["fingerprint"]
    success_manifest["items"][0]["fingerprint"] = new_fingerprint
    _write_json(successful_bundle.manifest, success_manifest)
    for path, is_batch in (
        (successful_bundle.checkpoint, False),
        (successful_bundle.batch, True),
    ):
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["items"][str(target.spm)]["fingerprint"] = new_fingerprint
        _write_json(path, payload)
    item_report = json.loads(
        successful_bundle.item_report.read_text(encoding="utf-8")
    )
    item_report["fingerprint"] = new_fingerprint
    _write_json(successful_bundle.item_report, item_report)
    exact_report = json.loads(
        successful_bundle.exact_report.read_text(encoding="utf-8")
    )
    exact_report["manifest_fingerprint"] = new_fingerprint
    exact_report["unreal_result"]["fingerprint"] = new_fingerprint
    _write_json(successful_bundle.exact_report, exact_report)

    failed = finalizer.validate_exact_bundle(
        failed_bundle, target, verify_artifacts=False
    )
    success = finalizer.validate_exact_bundle(
        successful_bundle, target, verify_artifacts=False
    )
    winner = finalizer.reduce_attempt_history(target, [failed, success])

    assert old_fingerprint != new_fingerprint
    assert winner.candidate.bundle.run_id == "tail054"
    assert winner.sealed_attempts == (failed_bundle.bundle_id,)


def test_later_success_seals_failed_attempt_after_shared_handoff_was_rewritten(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr(
        finalizer, "validate_item_artifacts", _validate_fixture_artifacts
    )
    inventory_path, rows = _write_inventory(tmp_path / "inventory", count=1)
    inventory = finalizer.load_ordered_inventory(inventory_path, expected_count=1)
    target = _target_from_inventory(inventory)
    first = datetime(2026, 8, 30, 12, 30, tzinfo=timezone.utc)
    handoff = tmp_path / "shared" / "megaplant_tree_groups.json"
    handoff.parent.mkdir(parents=True)
    handoff.write_text("failed-attempt-bytes", encoding="utf-8")
    failed_bundle = _write_bundle(
        tmp_path / "logs",
        "tail036",
        19,
        rows[0],
        success=False,
        started_at=first,
    )
    _bind_handoff_artifact(failed_bundle, handoff)

    # A later forced-fresh run rewrites the same mutable handoff path before
    # producing its own exact success evidence.
    handoff.write_text("later-success-bytes", encoding="utf-8")
    success_bundle = _write_bundle(
        tmp_path / "logs",
        "tail054",
        1,
        rows[0],
        success=True,
        started_at=first + timedelta(minutes=12),
    )
    _bind_handoff_artifact(success_bundle, handoff)
    failed_fleet = {
        "spm": str(target.spm),
        "stem": target.stem,
        "status": "failed",
        "report": str(failed_bundle.exact_report),
    }
    success_fleet = {
        "spm": str(target.spm),
        "stem": target.stem,
        "status": "verified_in_unreal",
        "report": str(success_bundle.exact_report),
    }

    failed = finalizer.validate_exact_bundle(
        failed_bundle, target, fleet_result=failed_fleet, verify_artifacts=True
    )
    success = finalizer.validate_exact_bundle(
        success_bundle, target, fleet_result=success_fleet, verify_artifacts=True
    )
    winner = finalizer.reduce_attempt_history(target, [failed, success])

    assert failed.classification == finalizer.AMBIGUOUS_FAILURE
    assert failed.event_at < success.started_at
    assert success.classification == finalizer.SUCCESS
    assert winner.candidate.bundle.run_id == "tail054"
    assert winner.sealed_attempts == (failed_bundle.bundle_id,)


def test_successful_fleet_attempt_with_handoff_drift_remains_invalid(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr(
        finalizer, "validate_item_artifacts", _validate_fixture_artifacts
    )
    inventory_path, rows = _write_inventory(tmp_path / "inventory", count=1)
    inventory = finalizer.load_ordered_inventory(inventory_path, expected_count=1)
    target = _target_from_inventory(inventory)
    handoff = tmp_path / "shared" / "megaplant_tree_groups.json"
    handoff.parent.mkdir(parents=True)
    handoff.write_text("success-bytes", encoding="utf-8")
    bundle = _write_bundle(tmp_path / "logs", "success", 1, rows[0])
    _bind_handoff_artifact(bundle, handoff)
    handoff.write_text("drifted-after-success", encoding="utf-8")
    fleet_result = {
        "spm": str(target.spm),
        "stem": target.stem,
        "status": "verified_in_unreal",
        "report": str(bundle.exact_report),
    }

    verdict = finalizer.validate_exact_bundle(
        bundle, target, fleet_result=fleet_result, verify_artifacts=True
    )

    assert verdict.classification == finalizer.INVALID
    assert "content changed" in verdict.errors[0] or "size changed" in verdict.errors[0]


def test_later_ambiguous_rpc_invalidates_earlier_success(tmp_path):
    inventory_path, rows = _write_inventory(tmp_path / "inventory", count=1)
    inventory = finalizer.load_ordered_inventory(inventory_path, expected_count=1)
    target = _target_from_inventory(inventory)
    first = datetime(2026, 8, 30, 1, tzinfo=timezone.utc)
    success_bundle = _write_bundle(
        tmp_path / "logs", "success", 1, rows[0], started_at=first
    )
    failed_bundle = _write_bundle(
        tmp_path / "logs",
        "later_failure",
        1,
        rows[0],
        success=False,
        started_at=first + timedelta(minutes=10),
    )
    success = finalizer.validate_exact_bundle(
        success_bundle, target, verify_artifacts=False
    )
    failed = finalizer.validate_exact_bundle(
        failed_bundle, target, verify_artifacts=False
    )

    with pytest.raises(finalizer.FinalizationError, match="unsealed"):
        finalizer.reduce_attempt_history(target, [success, failed])


def _synthetic_ready_audit(root: Path):
    inventory_path, _ = _write_inventory(root / "inventory", count=80)
    inventory = finalizer.load_ordered_inventory(inventory_path)
    source = finalizer.read_stable_json(inventory_path)
    completed = datetime(2026, 8, 30, 1, 1, tzinfo=timezone.utc)
    started = completed - timedelta(minutes=1)
    winners = []
    for target in inventory.targets:
        bundle = finalizer.ExactBundle(
            run_id="fixture",
            bundle_id=f"fixture_{target.ordinal:03d}",
            manifest=inventory_path,
            checkpoint=inventory_path,
            batch=inventory_path,
            item_report=inventory_path,
            exact_report=root / "missing.json",
            assembly_report=inventory_path,
            fleet_report=inventory_path,
        )
        candidate = finalizer.CandidateVerdict(
            target=target,
            bundle=bundle,
            classification=finalizer.SUCCESS,
            fingerprint=f"fp-{target.ordinal:03d}",
            started_at=started,
            event_at=completed,
            verification_basis="fixture",
            snapshots=(source,),
        )
        winners.append(finalizer.Winner(target, candidate, ()))
    return finalizer.AuditResult(
        inventory=inventory,
        winners=tuple(winners),
        missing=(),
        source_snapshots=(source,),
    )


def test_80_winner_gate_writes_nothing_for_79(tmp_path):
    audit = _synthetic_ready_audit(tmp_path)
    incomplete = finalizer.AuditResult(
        inventory=audit.inventory,
        winners=audit.winners[:-1],
        missing=(audit.inventory.targets[-1].stem,),
        source_snapshots=audit.source_snapshots,
    )
    journal = tmp_path / "journal.json"

    with pytest.raises(finalizer.FinalizationError, match="80-winner gate"):
        finalizer.build_finalization_plan(
            incomplete, commit_ledger=tmp_path / "commit.json"
        )
    assert not journal.exists()
    assert not any(tmp_path.rglob("*.unreal_deployment_receipt.json"))


def test_schema2_receipts_need_global_commit_and_crash_resume(tmp_path):
    audit = _synthetic_ready_audit(tmp_path)
    ledger = tmp_path / "commit.json"
    journal = tmp_path / "journal.json"
    plan = finalizer.build_finalization_plan(
        audit,
        commit_ledger=ledger,
        created_at="2026-08-30T12:00:00+09:00",
    )
    finalizer.stage_deployment_receipts(plan, journal)

    first_receipt = plan.receipts[0].target.deployment_receipt
    assert not first_receipt.exists()

    def crash_after_seven(index, _path):
        if index == 7:
            raise RuntimeError("simulated crash")

    with pytest.raises(RuntimeError, match="simulated crash"):
        finalizer.commit_deployment_receipts(
            journal, after_install=crash_after_seven
        )
    assert first_receipt.is_file()
    assert not ledger.exists()
    assert finalizer.schema2_receipt_is_committed(first_receipt) is False

    resumed_ledger = finalizer.resume_deployment_commit(journal)
    assert resumed_ledger == ledger.resolve()
    assert ledger.is_file()
    assert finalizer.schema2_receipt_is_committed(first_receipt) is True
    assert all(
        receipt.target.deployment_receipt.is_file()
        for receipt in plan.receipts
    )
    journal_payload = json.loads(journal.read_text(encoding="utf-8"))
    assert journal_payload["state"] == "committed"
    assert len(journal_payload["receipts"]) == 80


def test_source_change_between_stage_and_commit_aborts(tmp_path):
    audit = _synthetic_ready_audit(tmp_path)
    journal = tmp_path / "journal.json"
    plan = finalizer.build_finalization_plan(
        audit, commit_ledger=tmp_path / "commit.json"
    )
    finalizer.stage_deployment_receipts(plan, journal)
    _write_json(audit.inventory.path, {"selected": [], "changed": True})

    with pytest.raises(finalizer.FinalizationError, match="source changed"):
        finalizer.commit_deployment_receipts(journal)
    assert not any(
        receipt.target.deployment_receipt.exists()
        for receipt in plan.receipts
    )


def test_native_or_assembly_change_between_stage_and_commit_aborts(tmp_path):
    audit = _synthetic_ready_audit(tmp_path)
    journal = tmp_path / "journal.json"
    plan = finalizer.build_finalization_plan(
        audit, commit_ledger=tmp_path / "commit.json"
    )
    finalizer.stage_deployment_receipts(plan, journal)
    changed_native = plan.receipts[0].target.native_receipt
    _write_json(changed_native, {"schema_version": 5, "changed": True})

    with pytest.raises(finalizer.FinalizationError, match="source changed"):
        finalizer.commit_deployment_receipts(journal)
    assert not any(
        receipt.target.deployment_receipt.exists()
        for receipt in plan.receipts
    )
