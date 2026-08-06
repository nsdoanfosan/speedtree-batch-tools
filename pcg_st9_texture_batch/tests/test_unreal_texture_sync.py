import ast
import hashlib
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock


TOOL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOL_DIR))

import unreal_texture_sync


class _FakeCommandSocket:
    def __init__(self):
        self.timeout = None

    def settimeout(self, timeout):
        self.timeout = timeout


class _FakeRemoteExecution:
    instances = []
    start_error = None
    run_error = None
    run_result = {"success": True}

    def __init__(self):
        self.remote_nodes = [{"node_id": "node", "project": "MyProject2"}]
        self._command_connection = None
        self.started = False
        self.stopped = False
        type(self).instances.append(self)

    def start(self):
        self.started = True
        if type(self).start_error is not None:
            raise type(self).start_error

    def open_command_connection(self, _node_id):
        self._command_connection = types.SimpleNamespace(
            _command_channel_socket=_FakeCommandSocket())

    def run_command(self, _command, **_kwargs):
        if type(self).run_error is not None:
            raise type(self).run_error
        return type(self).run_result

    def stop(self):
        self.stopped = True


def _fake_remote_execution_module():
    return types.SimpleNamespace(
        RemoteExecution=_FakeRemoteExecution,
        MODE_EXEC_FILE="ExecuteFile",
    )


class CanonicalTextureEntryTests(unittest.TestCase):
    def test_collects_canonical_texture_roles_and_hashes_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            color = root / "T_tree_color.tga"
            normal = root / "T_tree_normal.tga"
            ignored = root / "M_tree_color.tga"
            color.write_bytes(b"same pixels")
            normal.write_bytes(b"normal pixels")
            ignored.write_bytes(b"ignored")

            entries = unreal_texture_sync.canonical_texture_entries(
                [color, normal, color, ignored])

        self.assertEqual([row["role"] for row in entries], ["color", "normal"])
        self.assertEqual(entries[0]["asset_path"], "/Game/Textures/T_tree_color")
        self.assertEqual(entries[0]["md5"], hashlib.md5(b"same pixels").hexdigest())
        self.assertEqual(
            entries[0]["sha256"], hashlib.sha256(b"same pixels").hexdigest())

    def test_texture_extension_does_not_limit_unreal_sync(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            files = [
                root / "T_tree_color.jpg",
                root / "T_tree_normal.png",
                root / "T_tree_opacity.jpeg",
            ]
            for index, source in enumerate(files):
                source.write_bytes(f"pixels-{index}".encode())

            entries = unreal_texture_sync.canonical_texture_entries(files)

        self.assertEqual(
            [row["role"] for row in entries],
            ["color", "normal", "opacity"],
        )
        self.assertEqual(
            [row["asset_name"] for row in entries],
            ["T_tree_color", "T_tree_normal", "T_tree_opacity"],
        )

    def test_suffix_matching_is_case_insensitive(self):
        self.assertEqual(unreal_texture_sync.texture_role("T_Tree_SubSurface.TGA"), "subsurface")
        self.assertIsNone(unreal_texture_sync.texture_role("T_Tree_unknown.tga"))

    def test_asset_prefix_is_canonical_even_if_windows_path_is_lowercased(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "t_tree_extra.tga"
            source.write_bytes(b"extra")
            entries = unreal_texture_sync.canonical_texture_entries([source])
        self.assertEqual(entries[0]["asset_name"], "T_tree_extra")
        self.assertEqual(entries[0]["asset_path"], "/Game/Textures/T_tree_extra")

    def test_conflicting_sources_cannot_overwrite_the_same_unreal_asset(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = Path(tmp) / "first" / "T_tree_color.tga"
            second = Path(tmp) / "second" / "T_tree_color.tga"
            first.parent.mkdir()
            second.parent.mkdir()
            first.write_bytes(b"first")
            second.write_bytes(b"second")
            with self.assertRaisesRegex(RuntimeError, "conflicting sources"):
                unreal_texture_sync.canonical_texture_entries([first, second])


class UnrealPayloadTests(unittest.TestCase):
    def test_payload_uses_import_md5_and_scoped_source_control_operations(self):
        payload = unreal_texture_sync.unreal_payload(
            [{
                "source": r"D:\Tree\T_tree_color.tga",
                "asset_name": "T_tree_color",
                "asset_path": "/Game/Textures/T_tree_color",
                "role": "color",
                "md5": "abc",
            }],
            r"C:\tmp\report.json",
        )
        self.assertIn('get_tag_value(tag_name)', payload)
        self.assertIn('("AssetImportData", "SourceFile")', payload)
        self.assertIn('previous_md5 == entry["md5"].lower()', payload)
        self.assertIn("check_out_file", payload)
        self.assertIn("mark_file_for_add", payload)
        self.assertIn("revert_unchanged_files(owned_checkouts", payload)
        self.assertIn('"max_texture_size": 0', payload)

    def test_headless_is_used_only_when_no_editor_process_is_running(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "T_tree_color.tga"
            source.write_bytes(b"pixels")

            def fake_commandlet(script_path, _cfg):
                text = Path(script_path).read_text(encoding="utf-8")
                marker = "OUTPUT_PATH = "
                line = next(row for row in text.splitlines() if row.startswith(marker))
                output = Path(ast.literal_eval(line[len(marker):]))
                output.write_text(
                    '{"entries": [], "counts": {"unchanged": 1}, "errors": []}',
                    encoding="utf-8",
                )

            cfg = {
                "unreal_project": r"C:\UnrealProjects\MyProject2",
                "unreal_texture_sync_enabled": True,
                "unreal_texture_commandlet_fallback": True,
                "unreal_texture_destination": "/Game/Textures",
            }
            with mock.patch.object(unreal_texture_sync, "REPORT_DIR", Path(tmp)), \
                    mock.patch.object(unreal_texture_sync, "_editor_is_running", return_value=False), \
                    mock.patch.object(unreal_texture_sync, "_run_commandlet", side_effect=fake_commandlet) as commandlet, \
                    mock.patch.object(unreal_texture_sync, "_run_remote") as remote:
                result = unreal_texture_sync.sync_texture_files([source], cfg=cfg)

        self.assertEqual(result["mode"], "headless_commandlet")
        commandlet.assert_called_once()
        remote.assert_not_called()


class RemoteExecutionTimeoutTests(unittest.TestCase):
    def setUp(self):
        _FakeRemoteExecution.instances = []
        _FakeRemoteExecution.start_error = None
        _FakeRemoteExecution.run_error = None
        _FakeRemoteExecution.run_result = {"success": True}

    def test_response_timeout_is_finite_clear_and_stops_session(self):
        _FakeRemoteExecution.run_error = TimeoutError("timed out")
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "sync.py"
            script.write_text("print('sync')", encoding="utf-8")
            with mock.patch.dict(
                    sys.modules,
                    {"remote_execution": _fake_remote_execution_module()}):
                with self.assertRaisesRegex(
                        unreal_texture_sync.UnrealTextureSyncDeferred,
                        "12초 동안 응답하지 않았습니다"):
                    unreal_texture_sync._run_remote(
                        script,
                        r"C:\UnrealProjects\MyProject2",
                        discovery_timeout=0.1,
                        command_timeout=12,
                    )

        remote = _FakeRemoteExecution.instances[0]
        self.assertEqual(
            remote._command_connection._command_channel_socket.timeout, 12.0)
        self.assertTrue(remote.stopped)

    def test_session_is_stopped_when_start_fails(self):
        _FakeRemoteExecution.start_error = RuntimeError("start failed")
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "sync.py"
            script.write_text("print('sync')", encoding="utf-8")
            with mock.patch.dict(
                    sys.modules,
                    {"remote_execution": _fake_remote_execution_module()}):
                with self.assertRaisesRegex(RuntimeError, "start failed"):
                    unreal_texture_sync._run_remote(
                        script,
                        r"C:\UnrealProjects\MyProject2",
                        discovery_timeout=0.1,
                        command_timeout=12,
                    )

        self.assertTrue(_FakeRemoteExecution.instances[0].stopped)

    def test_sync_uses_configured_timeout_for_remote_response(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "T_tree_color.tga"
            source.write_bytes(b"pixels")
            cfg = {
                "unreal_project": r"C:\UnrealProjects\MyProject2",
                "unreal_texture_sync_enabled": True,
                "unreal_texture_destination": "/Game/Textures",
                "unreal_texture_sync_timeout": 321,
            }

            def fake_remote(script_path, _project, **_kwargs):
                text = Path(script_path).read_text(encoding="utf-8")
                marker = "OUTPUT_PATH = "
                line = next(
                    row for row in text.splitlines() if row.startswith(marker))
                output = Path(ast.literal_eval(line[len(marker):]))
                output.write_text(
                    '{"entries": [], "counts": {}, "errors": []}',
                    encoding="utf-8",
                )

            with mock.patch.object(
                    unreal_texture_sync, "REPORT_DIR", Path(tmp)), \
                    mock.patch.object(
                        unreal_texture_sync, "_editor_is_running",
                        return_value=True), \
                    mock.patch.object(
                        unreal_texture_sync, "_run_remote",
                        side_effect=fake_remote) as remote:
                unreal_texture_sync.sync_texture_files([source], cfg=cfg)

        self.assertEqual(remote.call_args.kwargs["command_timeout"], 321)


if __name__ == "__main__":
    unittest.main()
