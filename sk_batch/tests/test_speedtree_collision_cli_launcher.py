import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[2]
LAUNCHER_SOURCE = REPO / "speedtree_collision_cli" / "launcher.cpp"
HOOK_SOURCE = REPO / "speedtree_collision_cli" / "hook.cpp"
INTEGRATED_BAT = REPO / "SpeedTree_Batch_Tools.bat"
SK_BATCH_BAT = REPO / "sk_batch" / "SK_Batch.bat"


class SpeedTreeCollisionCliLauncherTests(unittest.TestCase):
    def test_gui_bake_uses_an_active_private_windows_desktop(self):
        source = LAUNCHER_SOURCE.read_text(encoding="utf-8")

        self.assertIn("CreateDesktopW", source)
        self.assertIn("startup.lpDesktop = isolatedDesktopName.data();", source)
        self.assertIn("startup.wShowWindow = SW_SHOW;", source)
        self.assertIn("startup.wShowWindow = SW_SHOWNOACTIVATE;", source)
        self.assertNotIn("startup.wShowWindow = SW_SHOWMINNOACTIVE;", source)
        self.assertIn("STARTF_USESHOWWINDOW", source)
        self.assertNotIn("SetWindowPos", source)
        self.assertIn('value == L"--interactive-window"', source)
        self.assertIn("if (isolateWindow && persistent)", source)
        self.assertIn("persistent mode is disabled for this export", source)

    def test_batch_launchers_choose_one_shot_for_private_desktop_mode(self):
        for launcher in (INTEGRATED_BAT, SK_BATCH_BAT):
            source = launcher.read_text(encoding="utf-8")
            self.assertIn(
                'if /I "%SPEEDTREE_COLLISION_ISOLATED_WINDOW%"=="1"',
                source,
            )
            self.assertIn('set "SPEEDTREE_COLLISION_PERSISTENT=0"', source)

    def test_disappeared_persistent_pipe_starts_a_replacement(self):
        source = LAUNCHER_SOURCE.read_text(encoding="utf-8")

        self.assertIn("IsRestartableSessionPipeError", source)
        self.assertIn("ERROR_BROKEN_PIPE", source)
        self.assertIn("ERROR_PIPE_NOT_CONNECTED", source)
        self.assertIn("starting a replacement", source)

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
