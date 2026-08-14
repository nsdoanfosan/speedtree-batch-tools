import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[2]
LAUNCHER_SOURCE = REPO / "speedtree_collision_cli" / "launcher.cpp"
HOOK_SOURCE = REPO / "speedtree_collision_cli" / "hook.cpp"


class SpeedTreeCollisionCliLauncherTests(unittest.TestCase):
    def test_gui_bake_window_is_visible_without_activation(self):
        source = LAUNCHER_SOURCE.read_text(encoding="utf-8")

        self.assertIn("startup.wShowWindow = SW_SHOWNOACTIVATE;", source)
        self.assertNotIn("startup.wShowWindow = SW_SHOWMINNOACTIVE;", source)
        self.assertIn("STARTF_USESHOWWINDOW", source)

    def test_recovery_question_is_skipped_for_every_gui_bake_mode(self):
        source = HOOK_SOURCE.read_text(encoding="utf-8")

        recovery_hook = source[
            source.index("void __fastcall HookedMainWindowRecoveryCheck"):
            source.index("bool BuildSessionTargetPathList")
        ]

        self.assertIn(
            "if (!InstallHook(\n"
            "                gMainWindowRecoveryCheckHook,",
            source,
        )
        self.assertNotIn(
            "if (gSessionServerMode && !InstallHook(\n"
            "                gMainWindowRecoveryCheckHook,",
            source,
        )
        self.assertIn("recovery-file question", recovery_hook)
        self.assertNotIn("gOriginalMainWindowRecoveryCheck(mainWindow)", recovery_hook)
        self.assertIn("autosave/backup file intact", recovery_hook)

    def test_persistent_session_host_and_busy_pipe_wait_are_available(self):
        source = LAUNCHER_SOURCE.read_text(encoding="utf-8")

        self.assertIn('value == L"--serve-session"', source)
        self.assertIn('value == L"--ping-session"', source)
        self.assertIn("WaitForSingleObject(process.hProcess, INFINITE)", source)
        self.assertIn("pipeError == ERROR_PIPE_BUSY", source)
        self.assertIn("timeoutMs + 5 * 60 * 1000", source)


if __name__ == "__main__":
    unittest.main()
