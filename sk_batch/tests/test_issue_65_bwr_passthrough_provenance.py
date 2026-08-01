import copy
import json
import queue
import sys
import threading
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path
from unittest import mock

SK_BATCH_DIR = Path(__file__).resolve().parents[1]
if str(SK_BATCH_DIR) not in sys.path:
    sys.path.insert(0, str(SK_BATCH_DIR))

from cluster_assembly_builder import (  # noqa: E402
    PASS_THROUGH_PROVENANCE_REASON,
    build_blender_assembly_inputs,
    file_fingerprint,
)
from cluster_assembly_handoff_contract import (  # noqa: E402
    select_cluster_contract,
)


FIXTURE_PATH = (
    Path(__file__).resolve().parent
    / "fixtures"
    / "issue65_bwr_passthrough_provenance.json"
)


def load_gui_module():
    path = SK_BATCH_DIR / "sk_batch_gui.pyw"
    loader = SourceFileLoader(
        "sk_batch_gui_issue65_test",
        str(path),
    )
    spec = spec_from_loader(loader.name, loader)
    module = module_from_spec(spec)
    loader.exec_module(module)
    return module


def replace_tokens(value, replacements):
    if isinstance(value, dict):
        return {
            key: replace_tokens(child, replacements)
            for key, child in value.items()
        }
    if isinstance(value, list):
        return [replace_tokens(child, replacements) for child in value]
    if isinstance(value, str):
        return replacements.get(value, value)
    return value


def replace_fingerprint(value, artifact_path, fingerprint):
    if isinstance(value, dict):
        if value == {"path": str(artifact_path)}:
            value.clear()
            value.update(copy.deepcopy(fingerprint))
            return
        for child in value.values():
            replace_fingerprint(child, artifact_path, fingerprint)
    elif isinstance(value, list):
        for child in value:
            replace_fingerprint(child, artifact_path, fingerprint)


def make_scenario(tmp_path):
    raw = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    target_folder = tmp_path / "Weed_sanitized"
    other_folder = tmp_path / "Tree_other"
    target_folder.mkdir()
    other_folder.mkdir()
    target_spm = target_folder / "SK_Weed_sanitized_01.spm"
    other_spm = other_folder / "SK_Tree_other_01.spm"
    dependency_spm = target_folder / "Cluster" / "SK_cluster_sanitized_01.spm"
    dependency_spm.parent.mkdir()
    target_spm.write_bytes(b"sanitized-target")
    other_spm.write_bytes(b"sanitized-other")
    dependency_spm.write_bytes(b"old")
    payload = replace_tokens(
        raw["receipt"],
        {
            "$TARGET_FOLDER": str(target_folder),
            "$OTHER_FOLDER": str(other_folder),
            "$TARGET_SPM": str(target_spm),
            "$OTHER_TARGET_SPM": str(other_spm),
            "$DEPENDENCY_SPM": str(dependency_spm),
        },
    )
    for artifact in (target_spm, other_spm, dependency_spm):
        replace_fingerprint(payload, artifact, file_fingerprint(artifact))
    receipt = tmp_path / "sanitized_live_audit.json"
    receipt.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    target_contract = select_cluster_contract(payload, target_spm)
    manifest = build_blender_assembly_inputs(
        target_contract["handoff"],
        None,
        None,
        target_folder / "assembly",
        "",
        target_folder / "wind.json",
        pass_through_receipt_path=receipt,
        pass_through_target_contract=target_contract,
        pass_through_target_spm=target_spm,
    )
    report = target_folder / "reports" / (
        f"{target_spm.stem}_speedtree_repair_pipeline_report_codex.json"
    )
    report.parent.mkdir()
    report.write_text(
        json.dumps({
            "cluster_assembly_receipt_resolution": {
                "policy": "embedded_live_audit_authoritative",
                "requested_spm": str(target_spm),
                "selected_receipt": str(receipt),
            },
            "cluster_assembly_manifest": manifest,
        }),
        encoding="utf-8",
    )
    return {
        "payload": payload,
        "target_spm": target_spm,
        "other_spm": other_spm,
        "dependency_spm": dependency_spm,
        "receipt": receipt,
        "report": report,
        "manifest": manifest,
    }


def make_app(gui):
    app = gui.App.__new__(gui.App)
    app.state = {}
    app.state_lock = threading.RLock()
    app.ui_queue = queue.Queue()
    return app


def test_same_embedded_receipt_with_pass_through_roles_survives_post_check(
    tmp_path,
):
    gui = load_gui_module()
    app = make_app(gui)
    scenario = make_scenario(tmp_path)

    ready, reason = app._cluster_assembly_inputs_current(
        scenario["target_spm"]
    )

    assert ready, reason
    assert reason == ""
    manifest = scenario["manifest"]
    assert manifest["content_decision"] == "pass_through"
    assert manifest["reason"] == PASS_THROUGH_PROVENANCE_REASON
    assert manifest["rendered_role_count"] == 0
    assert manifest["handoff"]["roles"]
    assert manifest["handoff"]["cluster_dependencies"]
    assert manifest["handoff_evidence"]["pcg_receipt"]["sha256"]
    assert (
        manifest["pass_through_provenance"]["target_contract"]
        == select_cluster_contract(
            scenario["payload"], scenario["target_spm"]
        )
    )


def test_multi_target_receipt_never_projects_the_other_target(tmp_path):
    scenario = make_scenario(tmp_path)

    requested = select_cluster_contract(
        scenario["payload"], scenario["target_spm"]
    )
    other = select_cluster_contract(
        scenario["payload"], scenario["other_spm"]
    )

    assert requested["handoff"]["status"] == "pass_through"
    assert other["handoff"]["status"] == "ready"
    assert (
        scenario["manifest"]["pass_through_provenance"][
            "target_contract"
        ]["folder"]
        == str(scenario["target_spm"].parent)
    )
    assert scenario["other_spm"].name not in json.dumps(
        scenario["manifest"]["pass_through_provenance"][
            "target_contract"
        ]
    )


def test_real_pass_through_to_actionable_transition_stays_blocked(tmp_path):
    gui = load_gui_module()
    app = make_app(gui)
    scenario = make_scenario(tmp_path)
    changed_payload = copy.deepcopy(scenario["payload"])
    changed_contract = select_cluster_contract(
        changed_payload, scenario["target_spm"]
    )
    changed_contract["handoff"]["status"] = "ready"
    scenario["receipt"].write_text(
        json.dumps(changed_payload, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    ready, reason = app._cluster_assembly_inputs_current(
        scenario["target_spm"]
    )

    assert not ready
    assert "target_contract.handoff.status" in reason
    assert "expected=pass_through actual=ready" in reason


def test_changed_pass_through_artifact_stays_blocked_with_exact_path(
    tmp_path,
):
    gui = load_gui_module()
    app = make_app(gui)
    scenario = make_scenario(tmp_path)
    scenario["dependency_spm"].write_bytes(b"new")

    ready, reason = app._cluster_assembly_inputs_current(
        scenario["target_spm"]
    )

    assert not ready
    assert (
        "target_contract.dependencies[0].spm_fingerprint.sha256"
        in reason
        or (
            "target_contract.handoff.cluster_dependencies[0]."
            "spm_fingerprint.sha256"
        ) in reason
    )
    assert str(scenario["dependency_spm"]) in reason
    assert len(reason) > 100


def test_operator_state_keeps_full_mismatch_before_display_truncation(
    tmp_path,
):
    gui = load_gui_module()
    app = make_app(gui)
    scenario = make_scenario(tmp_path)
    scenario["dependency_spm"].write_bytes(b"new")
    ready, full_reason = app._cluster_assembly_inputs_current(
        scenario["target_spm"]
    )
    assert not ready
    assert len(full_reason) > 100

    target = {"spm": scenario["target_spm"]}
    app.stop_flag = threading.Event()
    app.cfg = {"blender_parallel_jobs": 1}
    app.log = mock.Mock()
    app._job_blender = mock.Mock(
        side_effect=gui.BatchItemError(
            full_reason,
            kind="data_error",
        )
    )
    app._publish_repair_stage_contract = mock.Mock()
    app._batch_job_inventory = mock.Mock(return_value={})

    with mock.patch.object(
        gui,
        "expand_blender_repair_targets",
        return_value=([target], {}, set()),
    ), mock.patch.object(gui, "save_state"):
        app._run_batch("blender", [target], emit_done=False)

    state = app.state[str(scenario["target_spm"])]
    assert state["blend_status"].endswith("…")
    assert len(state["blend_status"]) <= 104
    assert state["blend_status_error"]["message"] == full_reason
    app._publish_repair_stage_contract.assert_called_once_with(
        scenario["target_spm"],
        ready=False,
        reason=full_reason,
        kind="data_error",
    )
