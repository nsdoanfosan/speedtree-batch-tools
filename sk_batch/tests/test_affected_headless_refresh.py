import json
import sys
from pathlib import Path


SK_BATCH = Path(__file__).resolve().parents[1]
if str(SK_BATCH) not in sys.path:
    sys.path.insert(0, str(SK_BATCH))

import affected_headless_refresh as refresh


def _target(root, stem):
    asset = root / "asset"
    manifest = asset / "assembly" / f"{stem}_cluster_assembly_bindings.json"
    return {"stem": stem, "spm": asset / f"{stem}.spm", "manifest": manifest}


def test_audit_selects_zero_bone_spm_and_ignores_healthy_grass(tmp_path):
    """Selection is structural: a healthy asset named grass is left alone."""
    stale = _target(tmp_path, "SK_tree_stale_01")
    current = _target(tmp_path, "SK_tree_current_01")
    grass = _target(tmp_path, "SK_weed_velvet_grass_01")
    for target, bone_ids in ((stale, []), (current, [42]), (grass, [42])):
        _write_receipt(
            target,
            schema_version=refresh.NATIVE_RECEIPT_SCHEMA_VERSION,
            bones=[{"id": bone_id} for bone_id in bone_ids],
            instances=[{
                "source_rtti": refresh.LEAF_MESH_RTTI,
                "source_bone_id": bone_ids[0] if bone_ids else 0,
            }],
        )
        # Keep the Assembly manifest current so this case only exercises the
        # bone-shape reasons and not manifest staleness.
        _write_manifest(
            target, placement_version=refresh.PLACEMENT_CONTRACT_VERSION
        )

    assert refresh.audit_target(stale)["selected"] is True
    assert "parsed_native_bone_count_zero" in refresh.audit_target(stale)["reasons"]
    assert refresh.audit_target(current)["selected"] is False
    grass_audit = refresh.audit_target(grass)
    assert grass_audit["reasons"] == []
    assert grass_audit["selected"] is False


def test_audit_selects_missing_receipt(tmp_path):
    target = _target(tmp_path, "SK_tree_missing_01")
    _write_manifest(target, placement_version=refresh.PLACEMENT_CONTRACT_VERSION)

    audit = refresh.audit_target(target)
    assert audit["selected"] is True
    assert audit["reasons"] == ["native_receipt_missing_or_unreadable"]


def test_audit_selects_zero_bone_baseref_branch_leaf(tmp_path):
    target = _target(tmp_path, "SK_tree_baseref_leaf_01")
    receipt = refresh.native_receipt_path(target)
    receipt.parent.mkdir(parents=True, exist_ok=True)
    receipt.write_text(
        json.dumps({
            "schema_version": refresh.NATIVE_RECEIPT_SCHEMA_VERSION,
            "bones": [{"id": 1}],
            "generated_instances": [{
                "source_rtti": refresh.LEAF_MESH_RTTI,
                "source_bone_id": 0,
                "native_source_object_id": 1001,
                "vertex_ranges": [[0, 2]],
                "ancestor_chain": [
                    {"source_rtti": refresh.BRANCH_RTTI},
                    {"source_rtti": refresh.BASE_RTTI},
                ],
            }],
        }),
        encoding="utf-8",
    )
    _write_manifest(target, placement_version=refresh.PLACEMENT_CONTRACT_VERSION)

    audit = refresh.audit_target(target)
    assert audit["selected"] is True
    assert "baseref_branch_leaf_mesh_refresh_scope" in audit["reasons"]


def test_main_uses_dependency_fleet_for_every_selected_target(tmp_path, monkeypatch):
    selected = [
        {"stem": "SK_tree_a", "spm": str(tmp_path / "SK_tree_a.spm"), "selected": True},
        {"stem": "SK_weed_grass_b", "spm": str(tmp_path / "SK_weed_grass_b.spm"), "selected": True},
    ]
    monkeypatch.setattr(
        refresh,
        "discover_affected_targets",
        lambda _root: (selected, selected, []),
    )
    captured = {}

    def fake_fleet(argv):
        captured["call"] = argv
        run_id = argv[argv.index("--run-id") + 1]
        (tmp_path / f"cluster_fleet_push_{run_id}.json").write_text(
            json.dumps({"status": "dry_run", "provider_dependencies": []}),
            encoding="utf-8",
        )
        return 0

    monkeypatch.setattr(refresh.cluster_fleet_push, "main", fake_fleet)
    result = refresh.main([
        "--root", str(tmp_path), "--log-dir", str(tmp_path), "--dry-run"
    ])

    assert result == 0
    call = captured["call"]
    assert "--force-native-export" in call
    assert "--push-pass-through-roots" in call
    assert "--dry-run" in call
    assert call.count("--target-spm") == 2
    assert "SK_tree_a" in " ".join(call)
    assert "SK_weed_grass_b" in " ".join(call)


def test_main_resumes_prepared_run_without_reexport(tmp_path, monkeypatch):
    run_id = "20260829_000138"
    stem = "SK_tree_resume"
    spm = tmp_path / f"{stem}.spm"
    item_report = tmp_path / f"{stem}_unreal.json"
    source_manifest = tmp_path / f"{stem}_manifest.json"
    source_manifest.write_text(
        json.dumps({"items": [{"queue_id": str(spm), "report_path": str(item_report)}]}),
        encoding="utf-8",
    )
    outputs = {
        "manifest": str(source_manifest),
        "queue_id": str(spm),
        "report": str(tmp_path / f"{stem}.json"),
        "item_import_report": str(item_report),
    }
    (tmp_path / f"{stem}_affected_prepared_{run_id}.json").write_text(
        json.dumps({"outputs": outputs}), encoding="utf-8"
    )
    (tmp_path / f"affected_headless_refresh_{run_id}.json").write_text(
        json.dumps({
            "selected": [{"stem": stem, "spm": str(spm)}],
            "audited": [],
            "discovery_missing": [],
        }),
        encoding="utf-8",
    )

    def fake_fleet(argv):
        assert "--run-id" in argv
        assert run_id in argv
        (tmp_path / f"cluster_fleet_push_{run_id}.json").write_text(
            json.dumps({
                "status": "ok",
                "verified_count": 1,
                "failed_count": 0,
                "provider_failed_count": 0,
            }),
            encoding="utf-8",
        )
        return 0

    monkeypatch.setattr(refresh.cluster_fleet_push, "main", fake_fleet)
    assert refresh.main([
        "--log-dir", str(tmp_path), "--resume-run-id", run_id
    ]) == 0
    inventory = json.loads(
        (tmp_path / f"affected_headless_refresh_{run_id}.json").read_text(
            encoding="utf-8"
        )
    )
    assert inventory["status"] == "ok"
    assert inventory["failed_count"] == 0


def test_reset_item_retries_requires_resume_run_id():
    import pytest

    with pytest.raises(SystemExit):
        refresh.main(["--reset-item-retries"])


def test_headless_commandlet_disables_local_ddc_cleanup():
    """The per-launch DDC maintenance scan is pure overhead for batch ingest."""
    import exact_push
    from sk_common import UNREAL_COMMANDLET_BASE_ARGS

    assert "-NoDDCCleanup" in UNREAL_COMMANDLET_BASE_ARGS
    assert "-unattended" in UNREAL_COMMANDLET_BASE_ARGS
    assert "-NoDDCCleanup" in exact_push.UNREAL_COMMANDLET_BASE_ARGS


def test_reset_checkpoint_item_retries_requeues_only_stopped_items(tmp_path):
    import exact_push

    checkpoint = tmp_path / "checkpoint.json"
    checkpoint.write_text(
        json.dumps({
            "complete": True,
            "current_item": "b.spm",
            "items": {
                "a.spm": {"status": "imported_ok", "crash_count": 0},
                "b.spm": {"status": "manual_required", "crash_count": 3},
                "c.spm": {"status": "unreal_crash", "crash_count": 1},
                "d.spm": {"status": "data_error", "crash_count": 0},
                "e.spm": {"status": "not_run", "crash_count": 0},
            },
        }),
        encoding="utf-8",
    )

    result = exact_push.reset_checkpoint_item_retries(checkpoint)
    assert result["reset"] == ["b.spm", "c.spm"]

    written = json.loads(checkpoint.read_text(encoding="utf-8"))
    items = written["items"]
    assert items["a.spm"]["status"] == "imported_ok"
    assert items["d.spm"]["status"] == "data_error"
    assert items["e.spm"]["status"] == "not_run"
    for queue_id in ("b.spm", "c.spm"):
        assert items[queue_id]["status"] == "operator_retry_pending"
        assert items[queue_id]["crash_count"] == 0
    assert written["complete"] is False
    assert written["current_item"] is None


def test_reset_checkpoint_item_retries_leaves_clean_checkpoint_untouched(tmp_path):
    import exact_push

    checkpoint = tmp_path / "checkpoint.json"
    payload = {
        "complete": True,
        "current_item": None,
        "items": {"a.spm": {"status": "imported_ok", "crash_count": 0}},
    }
    checkpoint.write_text(json.dumps(payload), encoding="utf-8")

    result = exact_push.reset_checkpoint_item_retries(checkpoint)
    assert result["reset"] == []
    assert json.loads(checkpoint.read_text(encoding="utf-8")) == payload


def _write_receipt(target, *, schema_version, bones, instances):
    receipt = refresh.native_receipt_path(target)
    receipt.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    for offset, instance in enumerate(instances):
        row = dict(instance)
        row.setdefault("native_source_object_id", 1000 + offset)
        # Every real instance carries ranges; default to a valid one so a case
        # that is not about ranges does not trip the range reason.
        row.setdefault("vertex_ranges", [[offset * 4, offset * 4 + 2]])
        rows.append(row)
    receipt.write_text(
        json.dumps({
            "schema_version": schema_version,
            "bones": bones,
            "generated_instances": rows,
        }),
        encoding="utf-8",
    )


def test_missing_native_source_object_identity_is_selected(tmp_path):
    target = _target(tmp_path, "SK_tree_old_runtime_identity_01")
    receipt = refresh.native_receipt_path(target)
    receipt.parent.mkdir(parents=True, exist_ok=True)
    receipt.write_text(
        json.dumps({
            "schema_version": refresh.NATIVE_RECEIPT_SCHEMA_VERSION,
            "bones": [{"id": 42}],
            "generated_instances": [{
                "source_rtti": ".?AVCBranchNode@@",
                "source_bone_id": 42,
                "vertex_ranges": [[0, 2]],
            }],
        }),
        encoding="utf-8",
    )
    _write_manifest(target, placement_version=refresh.PLACEMENT_CONTRACT_VERSION)

    audit = refresh.audit_target(target)
    assert "native_source_object_identity_missing" in audit["reasons"]
    assert audit["missing_native_source_object_identity_count"] == 1


def _write_manifest(target, *, placement_version, status=None):
    manifest = Path(target["manifest"])
    manifest.parent.mkdir(parents=True, exist_ok=True)
    if status is None:
        status = (
            refresh.PLACEMENT_MANIFEST_STATUS
            if placement_version is not None
            else refresh.PASS_THROUGH_MANIFEST_STATUS
        )
    payload = {
        "schema_version": 1,
        "kind": "sk_batch_cluster_nanite_assembly_inputs",
        "status": status,
    }
    if placement_version is not None:
        payload["placement_contract"] = {
            "version": placement_version,
            "status": "ready",
        }
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    native = refresh.native_receipt_path(target)
    if native.is_file():
        deployment = refresh.deployment_receipt_path(target)
        deployment.write_text(json.dumps({
            "status": "imported_ok",
            "native_receipt_sha256": refresh._sha256(native),
            "assembly_manifest_sha256": refresh._sha256(manifest),
        }), encoding="utf-8")


def _write_pass_through_manifest(target):
    """The shape the pipeline writes when the full Skeletal Mesh is preserved."""
    manifest = Path(target["manifest"])
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        json.dumps({
            "schema_version": 1,
            "kind": "sk_batch_cluster_nanite_assembly_inputs",
            "status": refresh.PASS_THROUGH_MANIFEST_STATUS,
            "full_skeletal_mesh_preserved": True,
            "target_contract_disposition": "selected_receipt_contract",
        }),
        encoding="utf-8",
    )
    native = refresh.native_receipt_path(target)
    if native.is_file():
        deployment = refresh.deployment_receipt_path(target)
        deployment.write_text(json.dumps({
            "status": "imported_ok",
            "native_receipt_sha256": refresh._sha256(native),
            "assembly_manifest_sha256": refresh._sha256(manifest),
        }), encoding="utf-8")


def _current_target(tmp_path, stem):
    """A target that the audit must consider fully up to date."""
    target = _target(tmp_path, stem)
    _write_receipt(
        target,
        schema_version=refresh.NATIVE_RECEIPT_SCHEMA_VERSION,
        bones=[{"id": 42}],
        instances=[{
            "source_rtti": refresh.LEAF_MESH_RTTI,
            "source_bone_id": 42,
        }],
    )
    _write_manifest(target, placement_version=refresh.PLACEMENT_CONTRACT_VERSION)
    return target


def test_native_receipt_schema_version_matches_hook():
    """The audit constant must track the literal the native hook emits."""
    hook = (
        Path(refresh.__file__).resolve().parents[1]
        / "speedtree_collision_cli"
        / "hook.cpp"
    )
    text = hook.read_text(encoding="utf-8", errors="replace")
    needle = f'\\"schema_version\\": {refresh.NATIVE_RECEIPT_SCHEMA_VERSION},'
    assert needle in text, (
        "hook.cpp no longer emits receipt schema "
        f"{refresh.NATIVE_RECEIPT_SCHEMA_VERSION}; update "
        "NATIVE_RECEIPT_SCHEMA_VERSION"
    )


def test_placement_contract_version_tracks_the_assembly_builder():
    import cluster_assembly_builder

    assert (
        refresh.PLACEMENT_CONTRACT_VERSION
        == cluster_assembly_builder.PLACEMENT_CONTRACT_VERSION
    )


def test_fully_current_asset_is_not_reselected(tmp_path):
    audit = refresh.audit_target(_current_target(tmp_path, "SK_tree_current_02"))
    assert audit["reasons"] == []
    assert audit["selected"] is False


def test_locally_current_asset_without_unreal_deployment_is_selected(tmp_path):
    target = _current_target(tmp_path, "SK_tree_not_deployed_01")
    refresh.deployment_receipt_path(target).unlink()

    audit = refresh.audit_target(target)

    assert "unreal_deployment_receipt_missing_or_stale" in audit["reasons"]
    assert audit["selected"] is True


def test_native_change_after_unreal_deployment_is_selected(tmp_path):
    target = _current_target(tmp_path, "SK_tree_changed_after_deploy_01")
    receipt = refresh.native_receipt_path(target)
    payload = json.loads(receipt.read_text(encoding="utf-8"))
    payload["post_deployment_change"] = True
    receipt.write_text(json.dumps(payload), encoding="utf-8")

    audit = refresh.audit_target(target)

    assert "unreal_deployment_receipt_missing_or_stale" in audit["reasons"]
    assert "native receipt hash changed" in audit["deployment_receipt_error"]


def test_stale_placement_contract_is_selected(tmp_path):
    target = _current_target(tmp_path, "SK_tree_stale_placement_01")
    _write_manifest(target, placement_version=4)

    audit = refresh.audit_target(target)
    assert "assembly_placement_contract_stale" in audit["reasons"]
    assert audit["manifest_placement_contract_version"] == 4
    assert audit["selected"] is True


def test_stale_receipt_schema_is_selected(tmp_path):
    target = _current_target(tmp_path, "SK_tree_stale_schema_01")
    _write_receipt(
        target,
        schema_version=2,
        bones=[{"id": 42}],
        instances=[{
            "source_rtti": refresh.LEAF_MESH_RTTI,
            "source_bone_id": 42,
        }],
    )

    audit = refresh.audit_target(target)
    assert "native_receipt_schema_stale" in audit["reasons"]
    assert audit["selected"] is True


def test_surviving_zero_bone_leaf_mesh_is_selected(tmp_path):
    """The Fagus class of defect: healthy bone count, but leaves still on id 0."""
    target = _current_target(tmp_path, "SK_tree_zero_bone_leaf_01")
    _write_receipt(
        target,
        schema_version=refresh.NATIVE_RECEIPT_SCHEMA_VERSION,
        bones=[{"id": 1}, {"id": 2}, {"id": 3}],
        instances=[
            {"source_rtti": refresh.LEAF_MESH_RTTI, "source_bone_id": 0},
            {"source_rtti": refresh.LEAF_MESH_RTTI, "source_bone_id": 2},
        ],
    )

    audit = refresh.audit_target(target)
    assert "zero_bone_leaf_mesh_present" in audit["reasons"]
    assert audit["zero_bone_leaf_mesh_instance_count"] == 1
    assert audit["selected"] is True


def test_missing_assembly_manifest_is_selected(tmp_path):
    target = _current_target(tmp_path, "SK_tree_no_manifest_01")
    Path(target["manifest"]).unlink()

    audit = refresh.audit_target(target)
    assert "assembly_manifest_missing_or_unreadable" in audit["reasons"]
    assert audit["selected"] is True


def test_pass_through_manifest_is_not_judged_on_placement_contract(tmp_path):
    """Regression: a pass-through manifest has no placement contract by design.

    Flagging its absence made every freshly regenerated pass-through asset stay
    selected forever, so the refresh could never converge.
    """
    target = _target(tmp_path, "SK_tree_pass_through_01")
    _write_receipt(
        target,
        schema_version=refresh.NATIVE_RECEIPT_SCHEMA_VERSION,
        bones=[{"id": 7}],
        instances=[{
            "source_rtti": refresh.LEAF_MESH_RTTI,
            "source_bone_id": 7,
        }],
    )
    _write_pass_through_manifest(target)

    audit = refresh.audit_target(target)
    assert audit["manifest_status"] == refresh.PASS_THROUGH_MANIFEST_STATUS
    assert audit["manifest_placement_contract_version"] is None
    assert "assembly_placement_contract_stale" not in audit["reasons"]
    assert audit["reasons"] == []
    assert audit["selected"] is False


def test_pass_through_manifest_still_caught_by_bone_reasons(tmp_path):
    """Dropping the placement check must not make pass-through assets immune."""
    target = _target(tmp_path, "SK_tree_pass_through_stale_01")
    _write_receipt(
        target,
        schema_version=2,
        bones=[{"id": 7}],
        instances=[{
            "source_rtti": refresh.LEAF_MESH_RTTI,
            "source_bone_id": 0,
        }],
    )
    _write_pass_through_manifest(target)

    audit = refresh.audit_target(target)
    assert "assembly_placement_contract_stale" not in audit["reasons"]
    assert "native_receipt_schema_stale" in audit["reasons"]
    assert "zero_bone_leaf_mesh_present" in audit["reasons"]
    assert audit["selected"] is True


def test_ready_manifest_on_current_placement_contract_is_not_selected(tmp_path):
    target = _current_target(tmp_path, "SK_tree_ready_current_01")
    _write_manifest(
        target,
        placement_version=refresh.PLACEMENT_CONTRACT_VERSION,
        status=refresh.PLACEMENT_MANIFEST_STATUS,
    )

    audit = refresh.audit_target(target)
    assert audit["manifest_status"] == refresh.PLACEMENT_MANIFEST_STATUS
    assert audit["reasons"] == []
    assert audit["selected"] is False


SYNTHETIC_BONE_ID_BASE = 10000


def test_repaired_boneless_asset_is_not_reselected(tmp_path):
    """A lone synthetic bone is the fix for a boneless SPM, not a defect."""
    target = _target(tmp_path, "SK_weed_repaired_boneless_01")
    _write_receipt(
        target,
        schema_version=refresh.NATIVE_RECEIPT_SCHEMA_VERSION,
        bones=[{"id": SYNTHETIC_BONE_ID_BASE}],
        instances=[{
            "source_rtti": refresh.LEAF_MESH_RTTI,
            "source_bone_id": SYNTHETIC_BONE_ID_BASE,
        }],
    )
    _write_pass_through_manifest(target)

    audit = refresh.audit_target(target)
    assert audit["native_bone_count"] == 1
    assert audit["reasons"] == []
    assert audit["selected"] is False


def test_boneless_receipt_is_still_selected(tmp_path):
    target = _target(tmp_path, "SK_weed_boneless_01")
    _write_receipt(
        target,
        schema_version=refresh.NATIVE_RECEIPT_SCHEMA_VERSION,
        bones=[],
        instances=[],
    )
    _write_pass_through_manifest(target)

    audit = refresh.audit_target(target)
    assert audit["reasons"] == ["parsed_native_bone_count_zero"]
    assert audit["selected"] is True


def test_repaired_grass_is_not_reselected_by_name(tmp_path):
    """Selection is structural; a correct asset named grass must not requeue."""
    target = _target(tmp_path, "SK_Weed_Common_grass_zz_01")
    _write_receipt(
        target,
        schema_version=refresh.NATIVE_RECEIPT_SCHEMA_VERSION,
        bones=[{"id": SYNTHETIC_BONE_ID_BASE + i} for i in range(3)],
        instances=[
            {
                "source_rtti": refresh.LEAF_MESH_RTTI,
                "source_bone_id": SYNTHETIC_BONE_ID_BASE + i,
            }
            for i in range(3)
        ],
    )
    _write_pass_through_manifest(target)

    audit = refresh.audit_target(target)
    assert audit["reasons"] == []
    assert audit["selected"] is False


def test_repaired_baseref_branch_leaf_is_not_reselected(tmp_path):
    target = _target(tmp_path, "SK_tree_repaired_baseref_01")
    _write_receipt(
        target,
        schema_version=refresh.NATIVE_RECEIPT_SCHEMA_VERSION,
        bones=[{"id": SYNTHETIC_BONE_ID_BASE}],
        instances=[{
            "source_rtti": refresh.LEAF_MESH_RTTI,
            "source_bone_id": SYNTHETIC_BONE_ID_BASE,
            "ancestor_chain": [
                {"source_rtti": refresh.BRANCH_RTTI},
                {"source_rtti": refresh.BASE_RTTI},
            ],
        }],
    )
    _write_pass_through_manifest(target)

    audit = refresh.audit_target(target)
    assert audit["baseref_branch_leaf_mesh_instance_count"] == 1
    assert audit["baseref_branch_zero_bone_leaf_mesh_instance_count"] == 0
    assert audit["reasons"] == []
    assert audit["selected"] is False


def test_overlapping_vertex_ranges_are_selected(tmp_path):
    """The defect that let assets import on top of an unusable receipt.

    The native hook emitted per-instance ranges in triangle submission order,
    producing entries like [0,0],[0,1],[1,2] that the receipt contract rejects,
    while every other field looked current.
    """
    target = _target(tmp_path, "SK_tree_overlapping_ranges_01")
    _write_receipt(
        target,
        schema_version=refresh.NATIVE_RECEIPT_SCHEMA_VERSION,
        bones=[{"id": SYNTHETIC_BONE_ID_BASE}],
        instances=[{
            "source_rtti": refresh.LEAF_MESH_RTTI,
            "source_bone_id": SYNTHETIC_BONE_ID_BASE,
            "vertex_ranges": [[0, 0], [0, 1], [1, 2], [2, 2]],
        }],
    )
    _write_pass_through_manifest(target)

    audit = refresh.audit_target(target)
    assert "native_vertex_ranges_not_exact_and_ordered" in audit["reasons"]
    assert audit["selected"] is True


def test_coalesced_vertex_ranges_are_not_selected(tmp_path):
    target = _target(tmp_path, "SK_tree_coalesced_ranges_01")
    _write_receipt(
        target,
        schema_version=refresh.NATIVE_RECEIPT_SCHEMA_VERSION,
        bones=[{"id": SYNTHETIC_BONE_ID_BASE}],
        instances=[{
            "source_rtti": refresh.LEAF_MESH_RTTI,
            "source_bone_id": SYNTHETIC_BONE_ID_BASE,
            "vertex_ranges": [[0, 2], [56, 58]],
        }],
    )
    _write_pass_through_manifest(target)

    audit = refresh.audit_target(target)
    assert audit["reasons"] == []
    assert audit["selected"] is False


def test_missing_vertex_ranges_are_selected(tmp_path):
    target = _target(tmp_path, "SK_tree_no_ranges_01")
    _write_receipt(
        target,
        schema_version=refresh.NATIVE_RECEIPT_SCHEMA_VERSION,
        bones=[{"id": SYNTHETIC_BONE_ID_BASE}],
        instances=[{
            "source_rtti": refresh.LEAF_MESH_RTTI,
            "source_bone_id": SYNTHETIC_BONE_ID_BASE,
            "vertex_ranges": [],
        }],
    )
    _write_pass_through_manifest(target)

    audit = refresh.audit_target(target)
    assert "native_vertex_ranges_not_exact_and_ordered" in audit["reasons"]
    assert audit["selected"] is True


def test_vertex_range_helper_matches_receipt_contract():
    ok = refresh._vertex_ranges_are_exact_and_ordered
    assert ok({"vertex_ranges": [[0, 2], [4, 9]]}) is True
    assert ok({"vertex_ranges": [[0, 0]]}) is True
    assert ok({"vertex_ranges": [[0, 0], [0, 1]]}) is False
    assert ok({"vertex_ranges": [[4, 9], [0, 2]]}) is False
    assert ok({"vertex_ranges": [[2, 1]]}) is False
    assert ok({"vertex_ranges": [[-1, 3]]}) is False
    assert ok({"vertex_ranges": []}) is False
