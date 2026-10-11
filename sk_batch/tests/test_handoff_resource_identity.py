"""Exercise production handoff resource guards with fake bridges only."""

import ast
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest import mock

from sk_batch import prepared_rpc_resume as resume
from sk_batch import normalized_assembly_bend as bend


JOB = Path(__file__).resolve().parents[1] / "jobs/send2ue_push_job.py"
tree = ast.parse(JOB.read_text(encoding="utf-8"))
begin_node = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                  and node.name == "begin_batch_handoff")
scope = {"json": json}
exec(compile(ast.Module(body=[begin_node], type_ignores=[]), str(JOB), "exec"), scope)
begin_handoff = scope["begin_batch_handoff"]


class HandoffResourceIdentityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "handoff.json"
        self.metadata = {"phase_id": "p_exact", "ticket_id": "ticket_exact", "provider": "Codex",
                         "session_id": "owner_exact", "resource": "unreal:myproject2",
                         "pipeline": "speedtree_batch_send2ue", "request_id": "request_exact",
                         "target": "main.blend", "project": "unreal:MyProject2",
                         "action": "send2ue_manifest_export_rpc_ingest"}
        self.bridge = SimpleNamespace(
            require_active_phase=mock.Mock(return_value={"workload": "heavy", "provider": "Codex",
                                                        "session_id": "owner_exact"}),
            require_scopes=mock.Mock(),
            can_execute_handoff=mock.Mock(side_effect=lambda metadata: metadata["request_id"] == "request_exact"),
            heartbeat_handoff=mock.Mock(return_value=True), bind_handoff=mock.Mock())
        send2ue = ModuleType("send2ue")
        send2ue.coordination = SimpleNamespace(load_bridge=lambda: self.bridge)
        self.module_patch = mock.patch.dict(sys.modules, {"send2ue": send2ue})
        self.module_patch.start()
        self.addCleanup(self.module_patch.stop)

    def begin(self, metadata):
        self.path.write_text(json.dumps(metadata), encoding="utf-8")
        args = SimpleNamespace(workstation_phase_id="p_exact", transport="rpc",
                               workstation_handoff_json=self.path)
        return begin_handoff(args, "main.blend")

    def test_actual_export_begin_accepts_canonical_lowercase_without_rewriting_binding(self):
        for resource in ("unreal:myproject2", "unreal:MyProject2"):
            metadata = {**self.metadata, "resource": resource}
            bridge, actual = self.begin(metadata)
            self.assertIs(bridge, self.bridge)
            self.assertEqual(actual, metadata)
        self.bridge.require_active_phase.assert_called_with("p_exact", "unreal:myproject2")
        self.bridge.require_scopes.assert_called_with("p_exact", ["editor"])
        self.bridge.bind_handoff.assert_not_called()

    def test_export_begin_rejects_wrong_resource_and_exact_binding_changes(self):
        changes = ({"resource": "unreal:OtherProject"}, {"resource": "painter:main"},
                   {"phase_id": "p_other"}, {"request_id": "request_other"},
                   {"pipeline": "other_pipeline"}, {"action": "other_action"},
                   {"project": "unreal:myproject2"})
        for change in changes:
            with self.subTest(change=change), self.assertRaises(RuntimeError):
                self.begin({**self.metadata, **change})
        self.bridge.bind_handoff.assert_not_called()

    def test_prepared_resume_accepts_canonical_resource_without_changing_metadata(self):
        for resource in ("unreal:myproject2", "unreal:MyProject2"):
            metadata = {**self.metadata, "resource": resource}
            before = deepcopy(metadata)
            resume._active_binding(self.bridge, metadata)
            self.assertEqual(metadata, before)
        self.bridge.require_active_phase.assert_called_with("p_exact", "unreal:myproject2")
        self.bridge.require_scopes.assert_called_with("p_exact", ["editor"])
        self.bridge.heartbeat_handoff.assert_called()

    def test_prepared_resume_rejects_wrong_resource_and_exact_request_action_project(self):
        for change in ({"resource": "unreal:OtherProject"}, {"resource": "painter:main"},
                       {"request_id": "request_other"}, {"action": "other_action"},
                       {"project": "unreal:myproject2"}, {"pipeline": "other_pipeline"}):
            with self.subTest(change=change), self.assertRaises(RuntimeError):
                resume._active_binding(self.bridge, {**self.metadata, **change})

    def test_prepared_resume_still_checks_phase_owner_and_heavy_admission(self):
        for phase in ({"workload": "light", "provider": "Codex", "session_id": "owner_exact"},
                      {"workload": "heavy", "provider": "Other", "session_id": "owner_exact"},
                      {"workload": "heavy", "provider": "Codex", "session_id": "other_owner"}):
            self.bridge.require_active_phase.return_value = phase
            with self.subTest(phase=phase), self.assertRaises(RuntimeError):
                resume._active_binding(self.bridge, self.metadata)

    def test_dependency_admission_uses_canonical_resource_and_preserves_exact_asset_scopes(self):
        queue = Path(self.temporary.name) / "Documents/GitHub/workstation-queue"
        queue.mkdir(parents=True)
        (queue / "pipeline_bridge.py").write_text("# inert test fixture", encoding="utf-8")
        bridge = ModuleType("pipeline_bridge")
        bridge.require_active_phase = mock.Mock(return_value={"workload": "heavy"})
        bridge.require_scopes = mock.Mock()
        with mock.patch.object(bend.Path, "home", return_value=Path(self.temporary.name)), \
                mock.patch.dict(sys.modules, {"pipeline_bridge": bridge}), \
                mock.patch.object(bend.sys, "path", list(sys.path)):
            bend.require_active_dependency_phase("p_exact", ["/Game/A", "/Game/B"])
        bridge.require_active_phase.assert_called_once_with("p_exact", "unreal:myproject2")
        bridge.require_scopes.assert_called_once_with("p_exact", ["asset:/Game/A", "asset:/Game/B"])


if __name__ == "__main__":
    unittest.main()
