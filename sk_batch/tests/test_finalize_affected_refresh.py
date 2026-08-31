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
    assembly_manifest = {
        "kind": "sk_batch_cluster_nanite_assembly_inputs",
        "status": "ready",
        "content_decision": "build",
        "full_skeletal_mesh_preserved": True,
        "base": {
            "weighted_bone_count": 10,
            "all_weighted_bones_in_final_wind": True,
        },
        "parts": [
            {
                "external_source": {
                    "kind": "send_to_unreal_normalized_skeletal_part",
                    "source_bone": "Bone_1_Start",
                    "pivot_contract": "normalized_attachment_origin_0_0_0",
                }
            }
        ],
        "placement_contract": {
            "status": "ready",
            "identity_policy": "exact_fbx_vertex_or_native_clipped_origin_v1",
            "translation_source": "exact_fbx_attachment_vertex_else_native_receipt",
            "exact_plan_line": {
                "geometric_fitting": False,
                "nearest_or_farthest_search": False,
                "asset_special_cases": False,
                "binding_count": 2,
            },
        },
        "attachment_bone_contract": {
            "status": "ready",
            "policy": (
                "native_modeler_runtime_receipt_v5_exact_pose_skeleton_index_zero"
            ),
            "receipt": {"sha256": "b" * 64},
        },
    }
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
        "code_files": [
            {
                "path": str(SK_BATCH / "unreal_ingest.py"),
                "fingerprint": next(
                    iter(finalizer.LEGACY_UNREAL_INGEST_FINGERPRINTS)
                ),
            }
        ],
        "cluster_assembly": (
            {"manifest": assembly_manifest} if built_assembly else None
        ),
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
                "assembly": f"{mesh}_NaniteAssembly",
                "parts": [{"bindings": 2}],
                "binding_count": 2,
                "final_skeleton_bones": 10,
                "manifest_skeleton_diagnostic": {
                    "status": "match",
                    "exact_order_match": True,
                    "current_unreal_skeleton_is_authoritative": True,
                    "missing_from_current": [],
                    "added_in_current": [],
                },
                "unreal_bone_name_map": {
                    "status": "exact_constant_index_offset",
                    "index_offset": 0,
                    "approximation_used": False,
                    "renamed_by_unreal_import": [],
                    "unmapped_authored_prefix_or_suffix": [],
                },
                "native_binding_contract": {
                    "construction": "direct_exact_reference_skeleton_indices",
                    "all_authored_influences_preserved": True,
                    "weights_sum_to_one": True,
                },
                "base_weights_in_final_wind": True,
                "final_nanite_shape_preservation": {
                    "policy": "preserve_area",
                    "applied_before_finish": True,
                    "base_and_parts_unchanged": True,
                    "preserved_through_finish": True,
                },
                "dynamic_wind": {
                    "success": True,
                    "skeleton_hash": skeleton_hash,
                    "manifest_skeleton_identity_matches": True,
                    "current_skeleton_is_authoritative": True,
                    "skeleton_asset_matches_final_mesh": True,
                    "skeleton_bind_pose_matches": True,
                    "missing_current_joints": 0,
                    "remapped_joint_records": 0,
                    "bone_group_mapping_matches_json": True,
                },
                "provenance": {"success": True},
            },
            "runtime": {
                "success": True,
                "assembly_static_checks": {
                    name: True
                    for name in finalizer.REQUIRED_ASSEMBLY_STATIC_CHECKS
                },
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
    built_assembly=True,
):
    spm = Path(target["spm"])
    item = _fresh_item(spm, built_assembly=built_assembly)
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
        if not built_assembly:
            state["cluster_assembly"] = {
                "status": "skipped",
                "reason": "no content-driven Assembly manifest",
            }
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
    export_rows = {
        kind: {
            "exists": True,
            "returncode": 0,
            "cache_hit": False,
            "force_reexport_requested": True,
            "verification_only": False,
            "bundled_process": True,
            "bundle_fallback": False,
            "export_attempts": [
                {"attempt": 1, "returncode": 0}
            ],
        }
        for kind in ("fbx", "xml")
    }
    _write_json(assembly, {
        "status": "ok",
        "speedtree_spm": str(spm),
        "speedtree_export_source": "forced_export_helper",
        "speedtree_export": {
            "force_reexport_requested": True,
            "exports": export_rows,
        },
    })
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


def _mutate_success_state(bundle, mutator):
    for path in (bundle.checkpoint, bundle.batch):
        payload = json.loads(path.read_text(encoding="utf-8"))
        state = next(iter(payload["items"].values()))
        mutator(state)
        _write_json(path, payload)
    item_report = json.loads(bundle.item_report.read_text(encoding="utf-8"))
    mutator(item_report)
    _write_json(bundle.item_report, item_report)
    if bundle.exact_report.is_file():
        report = json.loads(bundle.exact_report.read_text(encoding="utf-8"))
        mutator(report["unreal_result"])
        _write_json(bundle.exact_report, report)


def _add_current_durable_save_evidence(state):
    saved = state["final_skeleton_saved"]
    assembly = state["cluster_assembly"]
    rows = [
        (saved["skeleton"], "final_skeleton_contract", "skeleton", "editor_asset"),
        (saved["mesh"], "final_skeleton_contract", "mesh", "editor_asset"),
    ]
    if assembly.get("status") == "ok":
        rows.append((
            assembly["build"]["assembly"],
            "final_nanite_assembly",
            "assembly",
            "thumbnail_free",
        ))
    state["durable_saves"] = {
        "schema_version": 1,
        "records": [
            {
                "sequence": sequence,
                "package": package,
                "asset": package,
                "owner": owner,
                "role": role,
                "save_mode": save_mode,
                "saved": True,
                "dirty_after_save": False,
                "package_file": f"C:/Project/Content/{sequence}.uasset",
                "size": 100 + sequence,
                "mtime_ns": 1000 + sequence,
            }
            for sequence, (package, owner, role, save_mode) in enumerate(rows)
        ],
    }


def _promote_assembly_report_to_current(bundle):
    payload = json.loads(bundle.assembly_report.read_text(encoding="utf-8"))
    payload["speedtree_export_execution_policy"] = {
        "status": "validated",
        "policy": "normal_collision_export_fail_closed_v1",
        "explicit_opt_in": False,
        "verification_fallback_allowed": False,
    }
    _write_json(bundle.assembly_report, payload)


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
    validated = []
    for record in item.get("handoff_files") or []:
        path = Path(record["path"])
        payload = path.read_bytes()
        if len(payload) != record["size"]:
            raise finalizer.PushUnrealRecoveryError("handoff file size changed")
        actual = hashlib.blake2b(payload, digest_size=16).hexdigest()
        if actual != record["fingerprint"]:
            raise finalizer.PushUnrealRecoveryError("handoff file content changed")
        validated.append(record)
    return validated


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


def test_stable_artifact_read_binds_both_validator_hash_algorithms(
    tmp_path, monkeypatch,
):
    path = tmp_path / "artifact.fbx"
    payload = b"exact-artifact-bytes" * 150_000
    path.write_bytes(payload)
    monkeypatch.setattr(
        Path,
        "read_bytes",
        lambda _path: (_ for _ in ()).throw(
            AssertionError("artifact hashing must remain streaming")
        ),
    )

    snapshot = finalizer.read_stable_file(
        path,
        expected_size=len(payload),
        expected_sha256=hashlib.sha256(payload).hexdigest(),
        expected_fingerprint=hashlib.blake2b(payload, digest_size=16).hexdigest(),
    )

    assert snapshot.sha256 == hashlib.sha256(payload).hexdigest()
    with pytest.raises(finalizer.FinalizationError, match="SHA-256 changed"):
        finalizer.read_stable_file(path, expected_sha256="0" * 64)
    with pytest.raises(finalizer.FinalizationError, match="fingerprint changed"):
        finalizer.read_stable_file(path, expected_fingerprint="0" * 32)


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
    assert verdict.evidence_schema == finalizer.LEGACY_EVIDENCE_SCHEMA


def test_missing_durable_saves_requires_exact_20260830_legacy_runtime(
    tmp_path, monkeypatch,
):
    inventory_path, rows = _write_inventory(tmp_path / "inventory", count=1)
    inventory = finalizer.load_ordered_inventory(inventory_path, expected_count=1)
    target = _target_from_inventory(inventory)
    bundle = _write_bundle(tmp_path / "logs", "future", 1, rows[0])
    monkeypatch.setattr(finalizer, "LEGACY_UNREAL_INGEST_FINGERPRINTS", frozenset())

    verdict = finalizer.validate_exact_bundle(
        bundle, target, verify_artifacts=False
    )

    assert verdict.classification == finalizer.INVALID
    assert "legacy runtime" in verdict.errors[0]


def test_current_evidence_requires_and_accepts_durable_save_ownership(tmp_path):
    inventory_path, rows = _write_inventory(tmp_path / "inventory", count=1)
    inventory = finalizer.load_ordered_inventory(inventory_path, expected_count=1)
    target = _target_from_inventory(inventory)
    bundle = _write_bundle(tmp_path / "logs", "current", 1, rows[0])
    _mutate_success_state(bundle, _add_current_durable_save_evidence)
    _promote_assembly_report_to_current(bundle)

    verdict = finalizer.validate_exact_bundle(
        bundle, target, verify_artifacts=False
    )

    assert verdict.classification == finalizer.SUCCESS
    assert verdict.evidence_schema == finalizer.CURRENT_EVIDENCE_SCHEMA


@pytest.mark.parametrize(
    ("case", "expected"),
    [
        ("approximation", "approximation"),
        ("authored_influences", "influences"),
        ("missing_joint", "bone mapping"),
        ("remapped_joint", "bone mapping"),
        ("preserve_area", "preserve area"),
        ("runtime_static", "runtime/static"),
        ("baseref_nearest", "BaseRef/placement"),
        ("cluster_axis", "reference-axis"),
    ],
)
def test_exact_assembly_contract_regressions_fail_closed(tmp_path, case, expected):
    inventory_path, rows = _write_inventory(tmp_path / "inventory", count=1)
    inventory = finalizer.load_ordered_inventory(inventory_path, expected_count=1)
    target = _target_from_inventory(inventory)
    bundle = _write_bundle(tmp_path / "logs", case, 1, rows[0])

    if case in {"baseref_nearest", "cluster_axis"}:
        manifest = json.loads(bundle.manifest.read_text(encoding="utf-8"))
        assembly_manifest = manifest["items"][0]["cluster_assembly"]["manifest"]
        if case == "baseref_nearest":
            assembly_manifest["placement_contract"]["exact_plan_line"][
                "nearest_or_farthest_search"
            ] = True
        else:
            assembly_manifest["parts"][0]["external_source"]["source_bone"] = (
                "Bone_2_End"
            )
        contract = {
            key: manifest["items"][0].get(key)
            for key in finalizer.FRESH_PUSH_CONTRACT_KEYS
        }
        fingerprint = finalizer.stable_fingerprint(contract)
        manifest["items"][0]["fingerprint"] = fingerprint
        _write_json(bundle.manifest, manifest)

        def update_fingerprint(state):
            state["fingerprint"] = fingerprint

        _mutate_success_state(bundle, update_fingerprint)
        exact = json.loads(bundle.exact_report.read_text(encoding="utf-8"))
        exact["manifest_fingerprint"] = fingerprint
        _write_json(bundle.exact_report, exact)
    else:
        def mutate(state):
            build = state["cluster_assembly"]["build"]
            if case == "approximation":
                build["unreal_bone_name_map"]["approximation_used"] = True
            elif case == "authored_influences":
                build["native_binding_contract"][
                    "all_authored_influences_preserved"
                ] = False
            elif case == "missing_joint":
                build["dynamic_wind"]["missing_current_joints"] = 1
            elif case == "remapped_joint":
                build["dynamic_wind"]["remapped_joint_records"] = 1
            elif case == "preserve_area":
                build["final_nanite_shape_preservation"][
                    "preserved_through_finish"
                ] = False
            elif case == "runtime_static":
                state["cluster_assembly"]["runtime"]["assembly_static_checks"][
                    "native_binding_exact"
                ] = False

        _mutate_success_state(bundle, mutate)

    verdict = finalizer.validate_exact_bundle(
        bundle, target, verify_artifacts=False
    )

    assert verdict.classification == finalizer.INVALID
    assert expected.casefold() in verdict.errors[0].casefold()


def test_durable_save_owner_regression_fails_closed(tmp_path):
    inventory_path, rows = _write_inventory(tmp_path / "inventory", count=1)
    inventory = finalizer.load_ordered_inventory(inventory_path, expected_count=1)
    target = _target_from_inventory(inventory)
    bundle = _write_bundle(tmp_path / "logs", "durable", 1, rows[0])

    def mutate(state):
        _add_current_durable_save_evidence(state)
        state["durable_saves"]["records"][-1]["owner"] = "terminal_item_assets"

    _mutate_success_state(bundle, mutate)
    verdict = finalizer.validate_exact_bundle(
        bundle, target, verify_artifacts=False
    )

    assert verdict.classification == finalizer.INVALID
    assert "Nanite Assembly" in verdict.errors[0]


def test_verification_only_split_bundle_fallback_is_never_legacy_success(tmp_path):
    inventory_path, rows = _write_inventory(tmp_path / "inventory", count=1)
    inventory = finalizer.load_ordered_inventory(inventory_path, expected_count=1)
    target = _target_from_inventory(inventory)
    bundle = _write_bundle(tmp_path / "logs", "verification_fallback", 1, rows[0])
    assembly = json.loads(bundle.assembly_report.read_text(encoding="utf-8"))
    for row in assembly["speedtree_export"]["exports"].values():
        row["verification_only"] = True
        row["bundled_process"] = False
        row["bundle_fallback"] = True
    _write_json(bundle.assembly_report, assembly)

    verdict = finalizer.validate_exact_bundle(
        bundle, target, verify_artifacts=False
    )

    assert verdict.classification == finalizer.AMBIGUOUS_FAILURE
    assert "verification_only=True" in verdict.errors[0]


@pytest.mark.parametrize("missing_field", ["verification_only", "bundle_fallback"])
def test_current_normal_bundle_rejects_missing_false_fields(tmp_path, missing_field):
    inventory_path, rows = _write_inventory(tmp_path / "inventory", count=1)
    inventory = finalizer.load_ordered_inventory(inventory_path, expected_count=1)
    target = _target_from_inventory(inventory)
    bundle = _write_bundle(tmp_path / "logs", "current_missing", 1, rows[0])
    _mutate_success_state(bundle, _add_current_durable_save_evidence)
    _promote_assembly_report_to_current(bundle)
    assembly = json.loads(bundle.assembly_report.read_text(encoding="utf-8"))
    for row in assembly["speedtree_export"]["exports"].values():
        row.pop(missing_field)
    _write_json(bundle.assembly_report, assembly)

    verdict = finalizer.validate_exact_bundle(
        bundle, target, verify_artifacts=False
    )

    assert verdict.classification == finalizer.AMBIGUOUS_FAILURE
    assert f"{missing_field}=None" in verdict.errors[0]


def test_fingerprint_bound_legacy_bundle_accepts_only_absent_new_false_fields(tmp_path):
    inventory_path, rows = _write_inventory(tmp_path / "inventory", count=1)
    inventory = finalizer.load_ordered_inventory(inventory_path, expected_count=1)
    target = _target_from_inventory(inventory)
    bundle = _write_bundle(tmp_path / "logs", "legacy_absent", 1, rows[0])
    assembly = json.loads(bundle.assembly_report.read_text(encoding="utf-8"))
    for row in assembly["speedtree_export"]["exports"].values():
        row.pop("verification_only")
        row.pop("bundle_fallback")
    _write_json(bundle.assembly_report, assembly)

    verdict = finalizer.validate_exact_bundle(
        bundle, target, verify_artifacts=False
    )

    assert verdict.classification == finalizer.SUCCESS
    assert verdict.evidence_schema == finalizer.LEGACY_EVIDENCE_SCHEMA


def test_later_normal_bundle_success_seals_rejected_verification_fallback(tmp_path):
    inventory_path, rows = _write_inventory(tmp_path / "inventory", count=1)
    inventory = finalizer.load_ordered_inventory(inventory_path, expected_count=1)
    target = _target_from_inventory(inventory)
    started = datetime(2026, 8, 30, 13, tzinfo=timezone.utc)
    fallback = _write_bundle(
        tmp_path / "logs", "fallback", 1, rows[0], started_at=started
    )
    payload = json.loads(fallback.assembly_report.read_text(encoding="utf-8"))
    for row in payload["speedtree_export"]["exports"].values():
        row["verification_only"] = True
        row["bundled_process"] = False
        row["bundle_fallback"] = True
    _write_json(fallback.assembly_report, payload)
    normal = _write_bundle(
        tmp_path / "logs",
        "normal_rerun",
        1,
        rows[0],
        started_at=started + timedelta(minutes=10),
    )

    rejected = finalizer.validate_exact_bundle(
        fallback, target, verify_artifacts=False
    )
    success = finalizer.validate_exact_bundle(
        normal, target, verify_artifacts=False
    )
    winner = finalizer.reduce_attempt_history(target, [rejected, success])

    assert rejected.classification == finalizer.AMBIGUOUS_FAILURE
    assert winner.candidate.bundle.run_id == "normal_rerun"
    assert winner.sealed_attempts == (fallback.bundle_id,)


def test_latest_success_alone_seals_mutable_handoff_after_fallback(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr(
        finalizer, "validate_item_artifacts", _validate_fixture_artifacts
    )
    inventory_path, rows = _write_inventory(tmp_path / "inventory", count=1)
    inventory = finalizer.load_ordered_inventory(inventory_path, expected_count=1)
    target = _target_from_inventory(inventory)
    first = datetime(2026, 8, 30, 13, tzinfo=timezone.utc)
    handoff = tmp_path / "shared" / "root.fbx"
    handoff.parent.mkdir(parents=True)
    handoff.write_text("fallback-bytes", encoding="utf-8")
    fallback = _write_bundle(
        tmp_path / "logs", "fallback", 1, rows[0], started_at=first
    )
    fallback_payload = json.loads(
        fallback.assembly_report.read_text(encoding="utf-8")
    )
    for row in fallback_payload["speedtree_export"]["exports"].values():
        row["verification_only"] = True
        row["bundled_process"] = False
        row["bundle_fallback"] = True
    _write_json(fallback.assembly_report, fallback_payload)
    _bind_handoff_artifact(fallback, handoff)

    handoff.write_text("latest-normal-bytes", encoding="utf-8")
    normal = _write_bundle(
        tmp_path / "logs",
        "normal_rerun",
        1,
        rows[0],
        started_at=first + timedelta(minutes=10),
    )
    _bind_handoff_artifact(normal, handoff)
    fallback_structural = finalizer.validate_exact_bundle(
        fallback, target, verify_artifacts=False
    )
    normal_structural = finalizer.validate_exact_bundle(
        normal, target, verify_artifacts=False
    )

    verified = finalizer._verify_latest_success_artifacts(
        target,
        [fallback_structural, normal_structural],
        {"fallback": None, "normal_rerun": None},
        verify_artifacts=True,
    )
    winner = finalizer.reduce_attempt_history(target, verified)

    assert verified[0].classification == finalizer.AMBIGUOUS_FAILURE
    assert verified[1].classification == finalizer.SUCCESS
    artifact_snapshots = [
        snapshot for snapshot in verified[1].snapshots
        if snapshot.path == handoff.resolve()
    ]
    assert len(artifact_snapshots) == 1
    assert artifact_snapshots[0].sha256 == hashlib.sha256(
        handoff.read_bytes()
    ).hexdigest()
    assert winner.candidate.bundle.run_id == "normal_rerun"
    assert winner.sealed_attempts == (fallback.bundle_id,)


def test_current_pass_through_evidence_needs_no_assembly_save_owner(tmp_path):
    inventory_path, rows = _write_inventory(tmp_path / "inventory", count=1)
    inventory = finalizer.load_ordered_inventory(inventory_path, expected_count=1)
    target = _target_from_inventory(inventory)
    bundle = _write_bundle(
        tmp_path / "logs", "pass_through", 1, rows[0], built_assembly=False
    )
    _mutate_success_state(bundle, _add_current_durable_save_evidence)
    _promote_assembly_report_to_current(bundle)

    verdict = finalizer.validate_exact_bundle(
        bundle, target, verify_artifacts=False
    )

    assert verdict.classification == finalizer.SUCCESS
    assert verdict.evidence_schema == finalizer.CURRENT_EVIDENCE_SCHEMA


def test_artifact_validator_must_return_a_content_bound_identity(
    tmp_path, monkeypatch,
):
    inventory_path, rows = _write_inventory(tmp_path / "inventory", count=1)
    inventory = finalizer.load_ordered_inventory(inventory_path, expected_count=1)
    target = _target_from_inventory(inventory)
    bundle = _write_bundle(tmp_path / "logs", "unbound", 1, rows[0])
    artifact = tmp_path / "artifact.fbx"
    artifact.write_bytes(b"bytes")
    monkeypatch.setattr(
        finalizer,
        "validate_item_artifacts",
        lambda _item: [{"path": str(artifact), "size": artifact.stat().st_size}],
    )

    verdict = finalizer.validate_exact_bundle(
        bundle, target, verify_artifacts=True
    )

    assert verdict.classification == finalizer.INVALID
    assert "not content-bound" in verdict.errors[0]


def test_current_nested_artifact_requires_recorded_identity_match(
    tmp_path, monkeypatch,
):
    inventory_path, rows = _write_inventory(tmp_path / "inventory", count=1)
    inventory = finalizer.load_ordered_inventory(inventory_path, expected_count=1)
    target = _target_from_inventory(inventory)
    bundle = _write_bundle(tmp_path / "logs", "current_mismatch", 1, rows[0])
    _mutate_success_state(bundle, _add_current_durable_save_evidence)
    _promote_assembly_report_to_current(bundle)
    artifact = tmp_path / "artifact.fbx"
    payload = b"current-bytes"
    artifact.write_bytes(payload)
    monkeypatch.setattr(
        finalizer,
        "validate_item_artifacts",
        lambda _item: [{
            "path": str(artifact),
            "size": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
            "identity_scope": "cluster_assembly_artifact",
            "recorded_identity_matches_current": False,
        }],
    )

    verdict = finalizer.validate_exact_bundle(
        bundle, target, verify_artifacts=True
    )

    assert verdict.classification == finalizer.INVALID
    assert "differs from its recorded identity" in verdict.errors[0]


def test_current_artifact_identity_requires_an_explicit_scope(
    tmp_path, monkeypatch,
):
    inventory_path, rows = _write_inventory(tmp_path / "inventory", count=1)
    inventory = finalizer.load_ordered_inventory(inventory_path, expected_count=1)
    target = _target_from_inventory(inventory)
    bundle = _write_bundle(tmp_path / "logs", "current_scope", 1, rows[0])
    _mutate_success_state(bundle, _add_current_durable_save_evidence)
    _promote_assembly_report_to_current(bundle)
    artifact = tmp_path / "artifact.fbx"
    payload = b"current-bytes"
    artifact.write_bytes(payload)
    monkeypatch.setattr(
        finalizer,
        "validate_item_artifacts",
        lambda _item: [{
            "path": str(artifact),
            "size": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
        }],
    )

    verdict = finalizer.validate_exact_bundle(
        bundle, target, verify_artifacts=True
    )

    assert verdict.classification == finalizer.INVALID
    assert "identity scope is missing or invalid" in verdict.errors[0]


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


def test_source_change_during_receipt_install_blocks_global_commit(tmp_path):
    audit = _synthetic_ready_audit(tmp_path)
    plan = finalizer.build_finalization_plan(
        audit, commit_ledger=tmp_path / "commit.json"
    )
    journal = tmp_path / "journal.json"
    finalizer.stage_deployment_receipts(plan, journal)
    source = plan.receipts[0].target.native_receipt

    def mutate_after_last_install(index, _path):
        if index == finalizer.INVENTORY_COUNT:
            source.write_text('{"changed":"during-install"}', encoding="utf-8")

    with pytest.raises(finalizer.FinalizationError, match="source changed"):
        finalizer.commit_deployment_receipts(
            journal,
            after_install=mutate_after_last_install,
        )

    assert not plan.commit_ledger.exists()
    assert finalizer.schema2_receipt_is_committed(
        plan.receipts[0].target.deployment_receipt
    ) is False


def test_committed_receipt_rejects_later_immutable_source_drift(tmp_path):
    audit = _synthetic_ready_audit(tmp_path)
    plan = finalizer.build_finalization_plan(
        audit, commit_ledger=tmp_path / "commit.json"
    )
    journal = tmp_path / "journal.json"
    finalizer.stage_deployment_receipts(plan, journal)
    finalizer.commit_deployment_receipts(journal)
    first = plan.receipts[0]

    assert finalizer.schema2_receipt_is_committed(
        first.target.deployment_receipt
    ) is True
    first.target.manifest.write_text('{"changed":"after-commit"}', encoding="utf-8")
    assert finalizer.schema2_receipt_is_committed(
        first.target.deployment_receipt
    ) is False
