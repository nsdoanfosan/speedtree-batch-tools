import json
import sys
from pathlib import Path
from unittest import mock

import pytest


SK_BATCH_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SK_BATCH_DIR))

import push_dependency_schedule as schedule  # noqa: E402


def write_assembly(root_spm, manifest_path, source_blends):
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps(
            {
                "kind": schedule.MANIFEST_KIND,
                "parts": [
                    {
                        "prototype_id": f"rendered-{index}",
                        "external_source": {
                            "source_blend": {"path": str(source_blend)}
                        },
                    }
                    for index, source_blend in enumerate(source_blends)
                ],
            }
        ),
        encoding="utf-8",
    )
    report = schedule.repair_pipeline_report_path(root_spm)
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(
        json.dumps(
            {
                "cluster_assembly_manifest": {
                    "status": "ready",
                    "manifest": {"path": str(manifest_path)},
                }
            }
        ),
        encoding="utf-8",
    )


def no_validation():
    return mock.patch.multiple(
        schedule,
        validate_file_fingerprint=mock.DEFAULT,
        validate_manifest_artifacts=mock.DEFAULT,
    )


def test_tree_selection_adds_exact_rendered_cluster_sources_first(tmp_path):
    cluster_dir = tmp_path / "Tree_custom" / "Cluster"
    cluster_dir.mkdir(parents=True)
    root_spm = tmp_path / "Tree_custom" / "SK_Tree_custom_77.spm"
    root_spm.write_bytes(b"root")
    cluster_spms = [
        cluster_dir / "SK_twigs_custom_alpha.spm",
        cluster_dir / "SK_cards_custom_beta.spm",
    ]
    for spm in cluster_spms:
        spm.write_bytes(b"cluster")
        spm.with_suffix(".blend").write_bytes(b"blend")
    write_assembly(
        root_spm,
        root_spm.parent / "assembly" / "custom_bindings.json",
        [spm.with_suffix(".blend") for spm in cluster_spms],
    )
    all_items = {
        str(path): {"spm": path, "checked": False}
        for path in [root_spm, *cluster_spms]
    }

    with no_validation():
        ordered, dependencies, auto_added = schedule.expand_push_targets(
            [{"spm": root_spm, "checked": True}],
            all_items,
        )

    assert [item["spm"] for item in ordered] == [*cluster_spms, root_spm]
    assert dependencies[str(root_spm)] == tuple(
        str(path) for path in cluster_spms
    )
    assert auto_added == {str(path) for path in cluster_spms}


def test_sibling_rollback_never_enters_dependency_schedule(tmp_path):
    owner = tmp_path / "Weed_reed"
    owner.mkdir()
    live = owner / "SK_weed_reed_02.spm"
    rollback = (
        owner
        / "SK_weed_reed_02.texture_slot_backup_20260801_010203_123456.spm"
    )
    live.write_bytes(b"live")
    rollback.write_bytes(b"rollback")
    live_item = {"spm": live, "checked": True}
    rollback_item = {"spm": rollback, "checked": True}

    with mock.patch.object(
        schedule,
        "load_current_cluster_assembly_manifest",
        return_value={
            "kind": schedule.MANIFEST_KIND,
            "status": "pass_through",
            "parts": [],
        },
    ):
        ordered, dependencies, auto_added = schedule.expand_push_targets(
            [rollback_item, live_item],
            {str(rollback): rollback_item, str(live): live_item},
        )

    assert ordered == [live_item]
    assert dependencies == {str(live): ()}
    assert auto_added == set()


def test_shared_dependency_is_deduplicated_and_explicit_selection_is_preserved(
    tmp_path,
):
    cluster_dir = tmp_path / "Tree_shared" / "Cluster"
    cluster_dir.mkdir(parents=True)
    cluster_spm = cluster_dir / "SK_any_render_source.spm"
    cluster_spm.write_bytes(b"cluster")
    cluster_spm.with_suffix(".blend").write_bytes(b"blend")
    roots = [
        tmp_path / "Tree_shared" / "SK_Tree_shared_A.spm",
        tmp_path / "Tree_shared" / "SK_Tree_shared_B.spm",
    ]
    for index, root in enumerate(roots):
        root.write_bytes(b"root")
        write_assembly(
            root,
            root.parent / "assembly" / f"bindings_{index}.json",
            [cluster_spm.with_suffix(".blend")],
        )
    items = {
        str(path): {"spm": path, "checked": True}
        for path in [cluster_spm, *roots]
    }

    with no_validation(), mock.patch.object(
        schedule,
        "discover_cluster_blend_relations",
        return_value=[],
    ) as discover:
        ordered, dependencies, auto_added = schedule.expand_push_targets(
            [items[str(roots[0])], items[str(cluster_spm)], items[str(roots[1])]],
            items,
        )

    assert [item["spm"] for item in ordered] == [cluster_spm, *roots]
    assert dependencies[str(roots[0])] == (str(cluster_spm),)
    assert dependencies[str(roots[1])] == (str(cluster_spm),)
    assert auto_added == set()
    discover.assert_not_called()


def test_dependency_must_be_present_in_current_scan(tmp_path):
    cluster_dir = tmp_path / "Tree_missing_scan" / "Cluster"
    cluster_dir.mkdir(parents=True)
    cluster_spm = cluster_dir / "SK_unscanned_source.spm"
    cluster_spm.write_bytes(b"cluster")
    cluster_spm.with_suffix(".blend").write_bytes(b"blend")
    root_spm = tmp_path / "Tree_missing_scan" / "SK_Tree_missing_scan.spm"
    root_spm.write_bytes(b"root")
    write_assembly(
        root_spm,
        root_spm.parent / "assembly" / "bindings.json",
        [cluster_spm.with_suffix(".blend")],
    )

    with no_validation(), pytest.raises(
        schedule.PushDependencyError, match="current SK Batch scan"
    ):
        schedule.expand_push_targets(
            [{"spm": root_spm, "checked": True}],
            {str(root_spm): {"spm": root_spm, "checked": True}},
        )


def test_explicit_cluster_relation_schedules_source_without_prior_assembly(
    tmp_path,
):
    owner = tmp_path / "Tree_relation"
    cluster_dir = owner / "Cluster"
    cluster_dir.mkdir(parents=True)
    root_spm = owner / "SK_Tree_relation_01.spm"
    source_spm = cluster_dir / "SK_leaf_relation_01.spm"
    root_spm.write_bytes(b"root")
    source_spm.write_bytes(b"cluster")
    source_spm.with_suffix(".blend").write_bytes(b"blend")
    relation = {
        "source_spm": source_spm,
        "registry_error": None,
        "targets": [{
            "target_spm": root_spm,
            "relation_on": True,
            "owner_target": True,
        }],
    }
    items = {
        str(path): {"spm": path, "checked": path == root_spm}
        for path in (root_spm, source_spm)
    }

    with mock.patch.object(
        schedule,
        "discover_cluster_blend_relations",
        return_value=[relation],
    ):
        ordered, dependencies, auto_added = schedule.expand_push_targets(
            [items[str(root_spm)]],
            items,
        )

    assert [item["spm"] for item in ordered] == [source_spm, root_spm]
    assert dependencies[str(root_spm)] == (str(source_spm),)
    assert auto_added == {str(source_spm)}


def test_current_pass_through_contract_suppresses_relation_dependencies(
    tmp_path,
):
    owner = tmp_path / "Tree_pass_through"
    cluster_dir = owner / "Cluster"
    cluster_dir.mkdir(parents=True)
    root_spm = owner / "SK_Tree_pass_through_01.spm"
    source_spm = cluster_dir / "SK_leaf_pass_through_01.spm"
    root_spm.write_bytes(b"root")
    source_spm.write_bytes(b"cluster")
    report = schedule.repair_pipeline_report_path(root_spm)
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(
        json.dumps({
            "cluster_assembly_manifest": {"status": "pass_through"}
        }),
        encoding="utf-8",
    )
    items = {
        str(path): {"spm": path, "checked": path == root_spm}
        for path in (root_spm, source_spm)
    }

    with mock.patch.object(
        schedule,
        "discover_cluster_blend_relations",
    ) as discover:
        ordered, dependencies, auto_added = schedule.expand_push_targets(
            [items[str(root_spm)]],
            items,
        )

    assert [item["spm"] for item in ordered] == [root_spm]
    assert dependencies[str(root_spm)] == ()
    assert auto_added == set()
    discover.assert_not_called()


def test_stale_assembly_contract_fails_without_relation_fallback(
    tmp_path,
):
    owner = tmp_path / "Tree_stale_relation"
    cluster_dir = owner / "Cluster"
    cluster_dir.mkdir(parents=True)
    root_spm = owner / "SK_Tree_stale_relation_01.spm"
    source_spm = cluster_dir / "SK_leaf_stale_relation_01.spm"
    root_spm.write_bytes(b"root")
    source_spm.write_bytes(b"cluster")
    source_spm.with_suffix(".blend").write_bytes(b"blend")
    items = {
        str(path): {"spm": path, "checked": path == root_spm}
        for path in (root_spm, source_spm)
    }

    with mock.patch.object(
        schedule,
        "load_current_cluster_assembly_manifest",
        side_effect=schedule.PushDependencyError("stale assembly"),
    ), mock.patch.object(
        schedule,
        "discover_cluster_blend_relations",
        return_value=[{
            "source_spm": source_spm,
            "registry_error": None,
            "targets": [{
                "target_spm": root_spm,
                "relation_on": True,
                "owner_target": True,
            }],
        }],
    ) as discover, pytest.raises(
        schedule.PushDependencyError,
        match="stale assembly",
    ):
        schedule.expand_push_targets(
            [items[str(root_spm)]],
            items,
        )

    discover.assert_not_called()


def test_same_pipeline_contract_skips_persisted_and_relation_discovery(
    tmp_path,
):
    owner = tmp_path / "Tree_stage"
    cluster_dir = owner / "Cluster"
    cluster_dir.mkdir(parents=True)
    root_spm = owner / "SK_Tree_stage_01.spm"
    source_spm = cluster_dir / "SK_leaf_stage_01.spm"
    root_spm.write_bytes(b"root")
    source_spm.write_bytes(b"cluster")
    source_blend = source_spm.with_suffix(".blend")
    source_blend.write_bytes(b"blend")
    contract = schedule.exact_dependency_contract_from_validated_manifest(
        root_spm,
        {
            "kind": schedule.MANIFEST_KIND,
            "status": "ready",
            "parts": [{
                "prototype_id": "rendered-1",
                "external_source": {
                    "source_blend": {"path": str(source_blend)}
                },
            }],
        },
    )
    items = {
        str(path): {"spm": path, "checked": path == root_spm}
        for path in (root_spm, source_spm)
    }

    with mock.patch.object(
        schedule,
        "load_current_cluster_assembly_manifest",
        side_effect=AssertionError("same-job manifest must not be reloaded"),
    ), mock.patch.object(
        schedule,
        "discover_cluster_blend_relations",
        side_effect=AssertionError("same-job relations must not be rediscovered"),
    ):
        ordered, dependencies, auto_added = schedule.expand_push_targets(
            [items[str(root_spm)]],
            items,
            stage_dependency_contracts={str(root_spm): contract},
        )

    assert [item["spm"] for item in ordered] == [source_spm, root_spm]
    assert dependencies[str(root_spm)] == (str(source_spm),)
    assert auto_added == {str(source_spm)}


def test_incomplete_stage_contract_uses_existing_manifest_fallback(tmp_path):
    root_spm = tmp_path / "Tree_fallback" / "SK_Tree_fallback_01.spm"
    root_spm.parent.mkdir(parents=True)
    root_spm.write_bytes(b"root")
    item = {"spm": root_spm, "checked": True}

    with mock.patch.object(
        schedule,
        "load_current_cluster_assembly_manifest",
        return_value={
            "kind": schedule.MANIFEST_KIND,
            "status": "pass_through",
            "parts": [],
        },
    ) as load_manifest, mock.patch.object(
        schedule,
        "discover_cluster_blend_relations",
    ) as discover:
        ordered, dependencies, auto_added = schedule.expand_push_targets(
            [item],
            {str(root_spm): item},
            stage_dependency_contracts={
                str(root_spm): {
                    "kind": schedule.STAGE_DEPENDENCY_CONTRACT_KIND,
                    "schema_version": schedule.STAGE_DEPENDENCY_CONTRACT_VERSION,
                },
            },
        )

    assert ordered == [item]
    assert dependencies[str(root_spm)] == ()
    assert auto_added == set()
    load_manifest.assert_called_once_with(root_spm)
    discover.assert_not_called()


def test_complete_stage_contract_missing_dependency_fails_closed(tmp_path):
    owner = tmp_path / "Tree_missing_stage"
    owner.mkdir()
    root_spm = owner / "SK_Tree_missing_stage_01.spm"
    root_spm.write_bytes(b"root")
    missing_spm = owner / "Cluster" / "SK_missing_stage_01.spm"
    contract = {
        "kind": schedule.STAGE_DEPENDENCY_CONTRACT_KIND,
        "schema_version": schedule.STAGE_DEPENDENCY_CONTRACT_VERSION,
        "root_spm": str(root_spm.resolve()),
        "assembly_status": "ready",
        "dependency_spms": [str(missing_spm.resolve())],
        "evidence": "validated_cluster_assembly_manifest",
    }

    with mock.patch.object(
        schedule,
        "load_current_cluster_assembly_manifest",
        side_effect=AssertionError("complete stage evidence must not fallback"),
    ), mock.patch.object(
        schedule,
        "discover_cluster_blend_relations",
        side_effect=AssertionError("complete stage evidence must not fallback"),
    ), pytest.raises(
        schedule.PushDependencyError,
        match="verified Cluster dependency SPM is missing",
    ):
        schedule.expand_push_targets(
            [{"spm": root_spm, "checked": True}],
            {str(root_spm): {"spm": root_spm, "checked": True}},
            stage_dependency_contracts={str(root_spm): contract},
        )


def test_stage_contract_inventory_mismatch_fails_without_fallback(tmp_path):
    owner = tmp_path / "Tree_inventory"
    cluster_dir = owner / "Cluster"
    cluster_dir.mkdir(parents=True)
    root_spm = owner / "SK_Tree_inventory_01.spm"
    source_spm = cluster_dir / "SK_leaf_inventory_01.spm"
    root_spm.write_bytes(b"root")
    source_spm.write_bytes(b"cluster")
    contract = {
        "kind": schedule.STAGE_DEPENDENCY_CONTRACT_KIND,
        "schema_version": schedule.STAGE_DEPENDENCY_CONTRACT_VERSION,
        "root_spm": str(root_spm.resolve()),
        "assembly_status": "ready",
        "dependency_spms": [str(source_spm.resolve())],
        "evidence": "validated_cluster_assembly_manifest",
    }

    with mock.patch.object(
        schedule,
        "load_current_cluster_assembly_manifest",
        side_effect=AssertionError("complete stage evidence must not fallback"),
    ), mock.patch.object(
        schedule,
        "discover_cluster_blend_relations",
        side_effect=AssertionError("complete stage evidence must not fallback"),
    ), pytest.raises(
        schedule.PushDependencyError,
        match="current SK Batch scan",
    ):
        schedule.expand_push_targets(
            [{"spm": root_spm, "checked": True}],
            {str(root_spm): {"spm": root_spm, "checked": True}},
            stage_dependency_contracts={str(root_spm): contract},
        )


def test_pass_through_stage_contract_is_root_bound_and_skips_disk(tmp_path):
    roots = [
        tmp_path / "Tree_bound" / "SK_Tree_bound_A.spm",
        tmp_path / "Tree_bound" / "SK_Tree_bound_B.spm",
    ]
    roots[0].parent.mkdir(parents=True)
    for root in roots:
        root.write_bytes(b"root")
    contract = schedule.exact_dependency_contract_from_validated_manifest(
        roots[0],
        {
            "kind": schedule.MANIFEST_KIND,
            "status": "pass_through",
            "parts": [],
        },
    )
    item = {"spm": roots[1], "checked": True}

    with mock.patch.object(
        schedule,
        "load_current_cluster_assembly_manifest",
        return_value={
            "kind": schedule.MANIFEST_KIND,
            "status": "pass_through",
            "parts": [],
        },
    ) as load_manifest:
        ordered, dependencies, _auto_added = schedule.expand_push_targets(
            [item],
            {str(roots[1]): item},
            stage_dependency_contracts={str(roots[1]): contract},
        )

    assert ordered == [item]
    assert dependencies[str(roots[1])] == ()
    load_manifest.assert_called_once_with(roots[1])
