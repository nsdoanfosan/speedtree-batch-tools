import sys
import tempfile
import unittest
from pathlib import Path


SK_BATCH_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SK_BATCH_DIR))

from sk_common import (
    classify_push_failure,
    compact_error_message,
    summarize_job_failure,
)


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

    def test_empty_material_slot_wins_over_generic_speedtree_export_error(self):
        report = {
            "error": (
                "Unreal handoff validation failed before Send to Unreal. "
                "Mesh 'SK_tree' slot 1 has no material. SpeedTree export failed"
            )
        }
        summary = summarize_job_failure(report)
        self.assertIn("머티리얼 빈 슬롯", summary)
        self.assertIn("SK_tree slot 1", summary)
        self.assertNotEqual(summary, "SpeedTree export 실패")

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

    def test_structured_item_error_wins_over_blender_shutdown_noise(self):
        report = {
            "error": "PCG ST9 Texture Batch 산출물 누락: M_leaf -> T_leaf",
            "traceback": (
                "Traceback...\nRuntimeError: PCG ST9 Texture Batch 산출물 누락: "
                "M_leaf -> T_leaf"
            ),
        }
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "push.log"
            log_path.write_text(
                "RuntimeError: unregister_class(...): missing bl_rna attribute",
                encoding="utf-8",
            )
            summary = summarize_job_failure(report, log_path)
        self.assertIn("PCG ST9 Texture Batch", summary)
        self.assertNotIn("unregister_class", summary)

    def test_push_failure_classification_separates_queue_impact(self):
        self.assertEqual(
            classify_push_failure({"status": "manual_required"}),
            "manual_required",
        )
        self.assertEqual(
            summarize_job_failure(
                {
                    "status": "manual_required",
                    "failure_kind": "manual_required",
                    "error": "Unreal Push 수동 처리 필요: 본 1,800 > 1,500",
                    "traceback": "RuntimeError: cleanup noise",
                }
            ),
            "Unreal Push 수동 처리 필요: 본 1,800 > 1,500",
        )
        self.assertEqual(
            classify_push_failure(
                {"error": 'The call "import_asset" timed out after 900 seconds'}
            ),
            "rpc_timeout",
        )
        self.assertEqual(
            classify_push_failure(
                {"error": "Could not find an open Unreal Editor instance!"}
            ),
            "unreal_unavailable",
        )
        self.assertEqual(
            classify_push_failure({"error": "unassigned material slots: M_leaf[0]"}),
            "data_error",
        )


if __name__ == "__main__":
    unittest.main()
