"""Sanitized adapter and path-equivalence tests for issue #114."""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace


REPO_DIR = Path(__file__).resolve().parents[2]
if str(REPO_DIR) not in sys.path:
    sys.path.insert(0, str(REPO_DIR))

from pcg_st9_texture_batch.speedtree_modeler_uia import (  # noqa: E402
    PowerShellUIABridge,
    SEMANTIC_UIA_CONTRACT,
    SemanticModelerUIAError,
    SpeedTreeModelerRecoverySession,
)
from pcg_st9_texture_batch.stale_node_table_recovery import (  # noqa: E402
    _authoring_graph_core_projection_for_version,
)


class FakeProcess:
    def __init__(self, pid=4242):
        self.pid = pid
        self.returncode = None

    def poll(self):
        return self.returncode


class FakeBridge:
    def __init__(self):
        self.calls = []

    def invoke(self, **kwargs):
        self.calls.append(dict(kwargs))
        operation = kwargs["operation"]
        return {
            "contract": SEMANTIC_UIA_CONTRACT,
            "owned_process_id": kwargs["owned_process_id"],
            "document_accessible_name": kwargs["document_name"],
            "operation": operation,
            "menu_path": ["File", "Save" if operation == "save" else "Close"],
            "semantic_pattern": "InvokePattern",
            "bridge_exit_code": 0,
        }


class PowerShellBridgeReceiptTests(unittest.TestCase):
    def make_bridge(self, payload, *, returncode=0, captured=None):
        captured = captured if captured is not None else []

        def runner(command, **kwargs):
            captured.append((list(command), dict(kwargs)))
            return SimpleNamespace(
                returncode=returncode,
                stdout=json.dumps(payload),
                stderr="",
            )

        return PowerShellUIABridge(
            runner=runner,
            timeout=2,
            platform_name="nt",
        )

    def test_exact_owned_pid_document_and_save_receipt_are_required(self):
        payload = {
            "ok": True,
            "contract": SEMANTIC_UIA_CONTRACT,
            "owned_process_id": 4242,
            "document_accessible_name": "tree.spm",
            "operation": "save",
            "menu_path": ["File", "Save"],
            "semantic_pattern": "InvokePattern",
        }
        captured = []
        bridge = self.make_bridge(payload, captured=captured)

        receipt = bridge.invoke(
            owned_process_id=4242,
            executable=Path("SpeedTree_Modeler.exe"),
            document_name="tree.spm",
            operation="save",
        )

        self.assertEqual(receipt["menu_path"], ["File", "Save"])
        command = captured[0][0]
        self.assertIn("-OwnedProcessId", command)
        self.assertEqual(command[command.index("-OwnedProcessId") + 1], "4242")
        self.assertEqual(command[command.index("-DocumentName") + 1], "tree.spm")
        self.assertNotIn("Save As", command)
        self.assertIs(captured[0][1]["stdin"], subprocess.DEVNULL)

    def test_ambiguous_uia_and_mismatched_receipt_fail_closed(self):
        ambiguous = {
            "ok": False,
            "reason_token": "uia_document_ambiguous",
        }
        with self.assertRaises(SemanticModelerUIAError) as caught:
            self.make_bridge(ambiguous, returncode=1).invoke(
                owned_process_id=4242,
                executable="SpeedTree_Modeler.exe",
                document_name="tree.spm",
                operation="save",
            )
        self.assertEqual(caught.exception.reason_token, "uia_document_ambiguous")

        mismatched = {
            "ok": True,
            "contract": SEMANTIC_UIA_CONTRACT,
            "owned_process_id": 9999,
            "document_accessible_name": "tree.spm",
            "operation": "save",
            "menu_path": ["File", "Save"],
            "semantic_pattern": "InvokePattern",
        }
        with self.assertRaises(SemanticModelerUIAError) as caught:
            self.make_bridge(mismatched).invoke(
                owned_process_id=4242,
                executable="SpeedTree_Modeler.exe",
                document_name="tree.spm",
                operation="save",
            )
        self.assertEqual(
            caught.exception.reason_token,
            "uia_bridge_receipt_identity_mismatch",
        )


class OwnedSessionReuseTests(unittest.TestCase):
    def test_two_documents_reuse_one_owned_pid_and_close_exactly(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            executable = folder / "SpeedTree_Modeler.exe"
            first = folder / "first.spm"
            second = folder / "second.spm"
            for path in (executable, first, second):
                path.write_bytes(b"fixture")
            process = FakeProcess()
            launches = []
            forwards = []
            bridge = FakeBridge()

            def launch(exe, spm):
                launches.append((Path(exe), Path(spm)))
                return process

            def forward(exe, spm):
                forwards.append((Path(exe), Path(spm)))
                return SimpleNamespace(pid=5000)

            session = SpeedTreeModelerRecoverySession(
                executable,
                bridge=bridge,
                launcher=launch,
                document_opener=forward,
            )

            first_save = session.save_document(executable, first)
            first_close = session.close_document(first)
            second_save = session.save_document(executable, second)
            second_close = session.close_document(second)

            self.assertFalse(first_save["session_reused"])
            self.assertTrue(second_save["session_reused"])
            self.assertTrue(first_close["exact_document_closed"])
            self.assertTrue(second_close["owned_process_alive_after_close"])
            self.assertEqual(len(launches), 1)
            self.assertEqual(len(forwards), 1)
            self.assertEqual(
                [(row["document_name"], row["operation"]) for row in bridge.calls],
                [
                    ("first.spm", "save"),
                    ("first.spm", "close"),
                    ("second.spm", "save"),
                    ("second.spm", "close"),
                ],
            )
            self.assertEqual({row["owned_process_id"] for row in bridge.calls}, {4242})

    def test_close_cannot_target_a_different_document(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            executable = folder / "SpeedTree_Modeler.exe"
            first = folder / "first.spm"
            second = folder / "second.spm"
            for path in (executable, first, second):
                path.write_bytes(b"fixture")
            session = SpeedTreeModelerRecoverySession(
                executable,
                bridge=FakeBridge(),
                launcher=lambda *_args: FakeProcess(),
            )
            session.save_document(executable, first)

            with self.assertRaises(SemanticModelerUIAError) as caught:
                session.close_document(second)

            self.assertEqual(
                caught.exception.reason_token,
                "uia_close_document_identity_mismatch",
            )

    def test_failed_save_poisons_reuse_until_exact_document_is_resolved(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            executable = folder / "SpeedTree_Modeler.exe"
            first = folder / "first.spm"
            second = folder / "second.spm"
            for path in (executable, first, second):
                path.write_bytes(b"fixture")
            forwards = []

            class AmbiguousBridge:
                def invoke(inner_self, **_kwargs):
                    raise SemanticModelerUIAError(
                        "uia_document_ambiguous",
                        "fixture ambiguity",
                    )

            session = SpeedTreeModelerRecoverySession(
                executable,
                bridge=AmbiguousBridge(),
                launcher=lambda *_args: FakeProcess(),
                document_opener=lambda *args: forwards.append(args),
            )
            with self.assertRaises(SemanticModelerUIAError) as caught:
                session.save_document(executable, first)
            self.assertEqual(caught.exception.reason_token, "uia_document_ambiguous")

            with self.assertRaises(SemanticModelerUIAError) as caught:
                session.save_document(executable, second)
            self.assertEqual(
                caught.exception.reason_token,
                "uia_previous_document_not_closed",
            )
            self.assertEqual(forwards, [])


class BoundedPathEquivalenceTests(unittest.TestCase):
    @staticmethod
    def spm_text(texture_path, *, authored_name="leaf"):
        return (
            "<SpeedTree><Assets><Material_v8 ID=\"10\">"
            "<Name>" + authored_name + "</Name>"
            "<Map Name=\"Color\"><TexFilename>"
            + str(texture_path)
            + "</TexFilename></Map>"
            "</Material_v8></Assets></SpeedTree>"
        )

    def test_absolute_and_relative_same_target_match_only_with_own_spm_base(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            spm = folder / "tree.spm"
            texture = folder / "textures" / "leaf.png"
            texture.parent.mkdir()
            texture.write_bytes(b"fixture")
            absolute = self.spm_text(texture)
            relative = self.spm_text(Path("textures") / "leaf.png")

            absolute_v6 = _authoring_graph_core_projection_for_version(
                absolute,
                6,
                spm_path=spm,
            )
            relative_v6 = _authoring_graph_core_projection_for_version(
                relative,
                6,
                spm_path=spm,
            )
            self.assertEqual(
                absolute_v6["fingerprint"],
                relative_v6["fingerprint"],
            )
            self.assertNotEqual(
                _authoring_graph_core_projection_for_version(
                    absolute,
                    5,
                )["fingerprint"],
                _authoring_graph_core_projection_for_version(
                    relative,
                    5,
                )["fingerprint"],
            )
            self.assertNotEqual(
                _authoring_graph_core_projection_for_version(
                    absolute,
                    6,
                )["fingerprint"],
                _authoring_graph_core_projection_for_version(
                    relative,
                    6,
                )["fingerprint"],
            )

    def test_relative_retarget_and_nonpath_authored_change_remain_strict(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            spm = folder / "tree.spm"
            original = self.spm_text(Path("textures") / "leaf.png")
            retarget = self.spm_text(Path("other") / "leaf.png")
            renamed = self.spm_text(
                Path("textures") / "leaf.png",
                authored_name="different",
            )

            fingerprints = {
                _authoring_graph_core_projection_for_version(
                    text,
                    6,
                    spm_path=spm,
                )["fingerprint"]
                for text in (original, retarget, renamed)
            }
            self.assertEqual(len(fingerprints), 3)


if __name__ == "__main__":
    unittest.main()
