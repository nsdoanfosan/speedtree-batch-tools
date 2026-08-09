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

    def test_default_wait_and_subprocess_caps_are_separate_and_operation_specific(self):
        captured = []

        def runner(command, **kwargs):
            operation = command[command.index("-Operation") + 1]
            action = "Save" if operation == "save" else "Close"
            captured.append((operation, list(command), dict(kwargs)))
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps(
                    {
                        "ok": True,
                        "contract": SEMANTIC_UIA_CONTRACT,
                        "owned_process_id": 4242,
                        "document_accessible_name": "tree.spm",
                        "operation": operation,
                        "menu_path": ["File", action],
                        "semantic_pattern": "InvokePattern",
                        "pending_phase": "complete",
                        "elapsed_seconds": 1.25,
                    }
                ),
                stderr="",
            )

        bridge = PowerShellUIABridge(runner=runner, platform_name="nt")
        for operation in ("save", "close"):
            bridge.invoke(
                owned_process_id=4242,
                executable="SpeedTree_Modeler.exe",
                document_name="tree.spm",
                operation=operation,
            )

        by_operation = {row[0]: row for row in captured}
        save_command = by_operation["save"][1]
        close_command = by_operation["close"][1]
        self.assertEqual(
            save_command[save_command.index("-OperationTimeoutSeconds") + 1],
            "300",
        )
        self.assertEqual(
            close_command[close_command.index("-OperationTimeoutSeconds") + 1],
            "120",
        )
        self.assertEqual(by_operation["save"][2]["timeout"], 315.0)
        self.assertEqual(by_operation["close"][2]["timeout"], 135.0)
        self.assertNotIn("45", save_command)

    def test_timeout_reports_last_pending_phase_and_elapsed_time(self):
        ticks = iter((10.0, 18.5))
        progress = json.dumps(
            {
                "kind": "uia_bridge_progress",
                "phase": "resolving_save_menu",
                "elapsed_seconds": 4.25,
                "phase_elapsed_seconds": 0.0,
            }
        )

        def runner(command, **kwargs):
            raise subprocess.TimeoutExpired(
                command,
                kwargs["timeout"],
                output=progress,
            )

        bridge = PowerShellUIABridge(
            runner=runner,
            platform_name="nt",
            element_wait_timeouts={"save": 20},
            subprocess_timeouts={"save": 25},
            monotonic_fn=lambda: next(ticks),
        )
        with self.assertRaises(SemanticModelerUIAError) as caught:
            bridge.invoke(
                owned_process_id=4242,
                executable="SpeedTree_Modeler.exe",
                document_name="tree.spm",
                operation="save",
            )

        self.assertEqual(caught.exception.reason_token, "uia_bridge_timed_out")
        self.assertEqual(
            caught.exception.evidence["pending_phase"],
            "resolving_save_menu",
        )
        self.assertEqual(caught.exception.evidence["elapsed_seconds"], 8.5)
        self.assertEqual(caught.exception.evidence["phase_elapsed_seconds"], 4.25)
        self.assertEqual(caught.exception.evidence["operation_timeout_seconds"], 20.0)
        self.assertEqual(caught.exception.evidence["subprocess_timeout_seconds"], 25.0)

    def test_spawn_failure_has_distinct_token_and_phase(self):
        def runner(_command, **_kwargs):
            raise FileNotFoundError(2, "missing powershell")

        bridge = PowerShellUIABridge(
            runner=runner,
            timeout=2,
            platform_name="nt",
            monotonic_fn=lambda: 4.0,
        )
        with self.assertRaises(SemanticModelerUIAError) as caught:
            bridge.invoke(
                owned_process_id=4242,
                executable="SpeedTree_Modeler.exe",
                document_name="tree.spm",
                operation="save",
            )

        self.assertEqual(caught.exception.reason_token, "uia_bridge_spawn_failed")
        self.assertEqual(caught.exception.evidence["pending_phase"], "bridge_spawn")


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

    def test_failed_save_cleanup_is_exact_and_never_forces_the_owned_process(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            executable = folder / "SpeedTree_Modeler.exe"
            document = folder / "tree.spm"
            executable.write_bytes(b"fixture")
            document.write_bytes(b"fixture")

            class Process(FakeProcess):
                def wait(self, timeout=None):
                    self.wait_timeout = timeout
                    return self.returncode

                def terminate(self):
                    raise AssertionError("forced termination is forbidden")

                def kill(self):
                    raise AssertionError("forced kill is forbidden")

            process = Process()
            calls = []

            class Bridge:
                def invoke(inner_self, **kwargs):
                    calls.append(dict(kwargs))
                    if kwargs["operation"] == "save":
                        raise SemanticModelerUIAError(
                            "uia_bridge_timed_out",
                            "fixture timeout",
                            {"pending_phase": "resolving_save_menu"},
                        )
                    return {
                        "contract": SEMANTIC_UIA_CONTRACT,
                        "operation": "close",
                    }

            def graceful_close(observed, start_identity):
                self.assertIs(observed, process)
                self.assertEqual(start_identity, "creation-100")
                observed.returncode = 0
                return {
                    "graceful_close_requested": True,
                    "graceful_close_reason": "fixture",
                }

            session = SpeedTreeModelerRecoverySession(
                executable,
                bridge=Bridge(),
                launcher=lambda *_args: process,
                process_start_identity_fn=lambda _process: "creation-100",
                graceful_process_closer=graceful_close,
            )
            with self.assertRaises(SemanticModelerUIAError):
                session.save_document(executable, document)

            cleanup = session.cleanup_after_failure(document)

            self.assertEqual(
                [(call["document_name"], call["operation"]) for call in calls],
                [("tree.spm", "save"), ("tree.spm", "close")],
            )
            self.assertTrue(cleanup["exact_document_closed"])
            self.assertTrue(cleanup["graceful_process_exit_requested"])
            self.assertFalse(cleanup["force_termination_used"])
            self.assertFalse(cleanup["owned_process_alive_after_cleanup"])
            self.assertEqual(cleanup["cleanup_status"], "owned_process_exited_gracefully")


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
