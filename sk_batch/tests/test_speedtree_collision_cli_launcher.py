import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[2]
LAUNCHER_SOURCE = REPO / "speedtree_collision_cli" / "launcher.cpp"
HOOK_SOURCE = REPO / "speedtree_collision_cli" / "hook.cpp"
INTEGRATED_BAT = REPO / "SpeedTree_Batch_Tools.bat"
SK_BATCH_BAT = REPO / "sk_batch" / "SK_Batch.bat"
SK_EXACT_PUSH_BAT = REPO / "sk_batch" / "SK_Exact_Push.bat"
BUILD_SCRIPT = REPO / "speedtree_collision_cli" / "build.ps1"


class SpeedTreeCollisionCliLauncherTests(unittest.TestCase):
    def test_native_cli_is_hidden_and_gui_bake_remains_diagnostic_only(self):
        source = LAUNCHER_SOURCE.read_text(encoding="utf-8")

        self.assertIn("CreateDesktopW", source)
        self.assertIn("startup.lpDesktop = isolatedDesktopName.data();", source)
        self.assertIn("startup.wShowWindow = SW_SHOW;", source)
        self.assertIn("nativeCli ? SW_HIDE : SW_SHOWNOACTIVATE", source)
        self.assertNotIn("startup.wShowWindow = SW_SHOWMINNOACTIVE;", source)
        self.assertIn("STARTF_USESHOWWINDOW", source)
        self.assertNotIn("SetWindowPos", source)
        self.assertIn('value == L"--interactive-window"', source)
        self.assertIn('value == L"--gui-bake"', source)
        self.assertIn('L"SPEEDTREE_COLLISION_NATIVE_CLI"', source)
        self.assertIn("if (isolateWindow && persistent)", source)
        self.assertIn("persistent mode is disabled for this export", source)

    def test_batch_launchers_force_the_headless_native_cli(self):
        for launcher in (INTEGRATED_BAT, SK_BATCH_BAT, SK_EXACT_PUSH_BAT):
            source = launcher.read_text(encoding="utf-8")
            self.assertIn('set "SPEEDTREE_COLLISION_NATIVE_CLI=1"', source)
            self.assertIn('set "SPEEDTREE_COLLISION_PERSISTENT=0"', source)
            self.assertNotIn("SPEEDTREE_COLLISION_SESSION_ANCHOR", source)
            self.assertIn('build.ps1" -IfNeeded', source)

    def test_build_freshness_covers_every_cli_source_input(self):
        source = BUILD_SCRIPT.read_text(encoding="utf-8")

        self.assertIn("[switch]$IfNeeded", source)
        self.assertIn('$PSCommandPath,', source)
        self.assertIn('$hookSource,', source)
        self.assertIn('$launcherSource,', source)
        self.assertIn('Join-Path $sourceDirectory "session_protocol.h"', source)
        self.assertIn("$oldestOutput -ge $latestInput", source)
        self.assertIn("$diagnoseOutput -contains $capabilityContract", source)

    def test_batch_launchers_reject_a_stale_cli_feature_contract(self):
        launcher = LAUNCHER_SOURCE.read_text(encoding="utf-8")
        contract = (
            "SPEEDTREE_COLLISION_CLI_CONTRACT="
            "native-bundle-single-bake-v2"
        )

        self.assertIn(contract, launcher)
        for batch_launcher in (INTEGRATED_BAT, SK_BATCH_BAT, SK_EXACT_PUSH_BAT):
            source = batch_launcher.read_text(encoding="utf-8")
            self.assertIn(contract, source)
            self.assertIn("findstr.exe /x", source)

    def test_disappeared_persistent_pipe_starts_a_replacement(self):
        source = LAUNCHER_SOURCE.read_text(encoding="utf-8")

        self.assertIn("IsRestartableSessionPipeError", source)
        self.assertIn("ERROR_BROKEN_PIPE", source)
        self.assertIn("ERROR_PIPE_NOT_CONNECTED", source)
        self.assertIn("starting a replacement", source)

    def test_entire_recovery_check_is_bypassed_for_every_gui_bake_mode(self):
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
        self.assertIn("entire recovery-check logic", recovery_hook)
        self.assertIn("its .sbk lookup, recovery decision, and QMessageBox", recovery_hook)
        self.assertNotIn("gOriginalMainWindowRecoveryCheck(mainWindow)", recovery_hook)
        self.assertIn("backup file itself remains intact", recovery_hook)

    def test_qt_question_modal_is_the_no_semantics_fallback(self):
        source = HOOK_SOURCE.read_text(encoding="utf-8")
        dialog_hook = source[
            source.index("int __fastcall HookedQDialogExec"):
            source.index("bool BuildSessionTargetPathList")
        ]

        self.assertIn('gQObjectInherits(dialog, "QMessageBox")', dialog_hook)
        self.assertIn("kQMessageBoxQuestionIcon", dialog_hook)
        self.assertIn("return kQMessageBoxNoButton;", dialog_hook)
        self.assertIn("suppressed a Qt Question modal", dialog_hook)
        self.assertIn('GetProcAddress(qtWidgets, "?exec@QDialog@@UEAAHXZ")', source)

    def test_one_shot_process_wait_has_a_progress_watchdog(self):
        source = LAUNCHER_SOURCE.read_text(encoding="utf-8")

        self.assertIn("SPEEDTREE_COLLISION_STALL_TIMEOUT_MS", source)
        self.assertIn("ReadProcessActivity", source)
        self.assertIn("GetProcessMemoryInfo", source)
        self.assertIn("FileWriteTime(logPath)", source)
        self.assertIn("kProgressStallExitCode", source)
        self.assertIn("no meaningful CPU, I/O, memory, or hook-log", source)

    def test_native_cli_can_bundle_a_second_export_in_one_process(self):
        launcher = LAUNCHER_SOURCE.read_text(encoding="utf-8")
        hook = HOOK_SOURCE.read_text(encoding="utf-8")

        self.assertIn('value == L"--secondary-export-options"', launcher)
        self.assertIn('value == L"--secondary-export"', launcher)
        self.assertIn("SPEEDTREE_COLLISION_CLI_SECONDARY_OUTPUT", launcher)
        self.assertIn("SPEEDTREE_COLLISION_CLI_SECONDARY_OPTIONS", launcher)
        self.assertIn("RunSecondaryNativeExport", hook)
        self.assertIn("native CLI bundled secondary export completed", hook)
        self.assertIn("gSecondaryNativeSerializationActive", hook)
        self.assertIn(
            "bundled secondary model update suppressed",
            hook,
        )
        self.assertIn(
            "bundled secondary collision refresh suppressed",
            hook,
        )

    def test_verification_exports_skip_only_the_expensive_post_bake(self):
        launcher = LAUNCHER_SOURCE.read_text(encoding="utf-8")
        hook = HOOK_SOURCE.read_text(encoding="utf-8")

        self.assertIn('value == L"--verification-only"', launcher)
        self.assertIn("SPEEDTREE_COLLISION_CLI_VERIFICATION_ONLY", launcher)
        self.assertIn("SPEEDTREE_COLLISION_CLI_VERIFICATION_ONLY", hook)
        self.assertIn(
            "native CLI verification-only export skips Collision/Prune bake",
            hook,
        )
        self.assertIn("gOriginalSpeedTreeExport(arg1, arg2, arg3, gameExport);", hook)
        self.assertIn("RunSecondaryNativeExport(arg1, gameExport);", hook)

    def test_persistent_session_host_and_busy_pipe_wait_are_available(self):
        source = LAUNCHER_SOURCE.read_text(encoding="utf-8")

        self.assertIn('value == L"--serve-session"', source)
        self.assertIn('value == L"--ping-session"', source)
        self.assertIn("WaitForSingleObject(process.hProcess, INFINITE)", source)
        self.assertIn("pipeError == ERROR_PIPE_BUSY", source)
        self.assertIn("timeoutMs + 5 * 60 * 1000", source)

if __name__ == "__main__":
    unittest.main()
