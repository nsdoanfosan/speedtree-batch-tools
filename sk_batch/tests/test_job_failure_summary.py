import sys
import unittest
from pathlib import Path


SK_BATCH_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SK_BATCH_DIR))

from sk_common import compact_error_message, summarize_job_failure


class JobFailureSummaryTests(unittest.TestCase):
    def test_unreal_editor_crash_is_not_misreported_as_rpc_failure(self):
        report = {
            "error": (
                "Unreal Editor crashed or exited during push: "
                "ConnectionResetError: [WinError 10054]"
            ),
            "unreal_editor_running_after_failure": False,
        }
        summary = summarize_job_failure(report)
        self.assertIn("Unreal Editor", summary)
        self.assertIn("Push", summary)

    def test_unreal_connection_traceback_becomes_actionable_reason(self):
        report = {
            "error": (
                "Error: Python: Traceback\n"
                "ConnectionResetError: [WinError 10054]\n"
                "ConnectionError: Could not find an open Unreal Editor instance!"
            )
        }
        self.assertEqual(
            summarize_job_failure(report),
            "Unreal 연결 실패 — 에디터/RPC 응답 없음",
        )

    def test_unreal_mesh_and_plugin_failures_are_classified(self):
        self.assertEqual(
            summarize_job_failure({"error": "mesh not found: /Game/Trees/SK_Tree (folder has: [])"}),
            "Unreal 메시를 찾지 못함: /Game/Trees/SK_Tree",
        )
        self.assertEqual(
            summarize_job_failure({"error": "CodexDynamicWindImportLibrary missing (plugin not loaded)"}),
            "Unreal Codex 플러그인 미로드",
        )

    def test_blender_geometry_and_fallback_exception_are_short(self):
        self.assertEqual(
            summarize_job_failure({"error": "SpeedTree FBX contains no mesh geometry"}),
            "FBX 메시 지오메트리 없음",
        )
        self.assertEqual(
            summarize_job_failure({"traceback": "Traceback...\nValueError: invalid armature"}),
            "ValueError: invalid armature",
        )

    def test_background_blender_crash_is_classified(self):
        report = {
            "_report_error": "job report was not created",
            "traceback": "Blender 5.1 Writing: C:/Temp/SK_tree.crash.txt",
        }
        self.assertEqual(
            summarize_job_failure(report),
            "Blender 백그라운드 크래시",
        )

    def test_status_message_drops_log_path_and_limits_length(self):
        message = "시간 초과(3600s) — 로그: C:\\very\\long\\path\\job.log"
        self.assertEqual(compact_error_message(message), "시간 초과(3600s)")
        self.assertEqual(compact_error_message("x" * 20, max_chars=10), "xxxxxxxxx…")


if __name__ == "__main__":
    unittest.main()
