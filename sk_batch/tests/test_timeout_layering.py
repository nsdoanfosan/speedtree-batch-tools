import importlib.util
import re
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[2]
MODULE_PATH = REPO / "sk_batch" / "child_progress_contract.py"
SPEC = importlib.util.spec_from_file_location("child_progress_contract_test", MODULE_PATH)
CONTRACT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CONTRACT)


class TimeoutLayeringTests(unittest.TestCase):
    def test_material_speedtree_phase_has_independent_queue_and_execution_bounds(self):
        rules = CONTRACT.material_preflight_inactivity_rules(180, 3600, 930)
        self.assertEqual(rules[CONTRACT.SPEEDTREE_SLOT_WAIT_MARKER], 3600)
        self.assertEqual(rules[CONTRACT.SPEEDTREE_SLOT_ACQUIRED_MARKER], 930)
        self.assertEqual(rules[CONTRACT.MATERIAL_PREFLIGHT_EXPORT_DONE_MARKER], 180)

    def test_send2ue_rpc_phase_has_one_timeout_owner(self):
        rules = CONTRACT.send2ue_inactivity_rules(180, 1800)
        self.assertEqual(rules[CONTRACT.SEND2UE_JOB_START_MARKER], 1800)
        self.assertEqual(rules[CONTRACT.SEND2UE_DISK_EXPORT_START_MARKER], 1800)
        self.assertIsNone(rules[CONTRACT.SEND2UE_RPC_OWNED_START_MARKER])
        self.assertEqual(rules[CONTRACT.SEND2UE_RPC_OWNED_DONE_MARKER], 180)

    def test_gui_uses_marker_watchdog_with_native_cleanup_grace(self):
        source = (REPO / "sk_batch" / "sk_batch_gui.pyw").read_text(encoding="utf-8")
        self.assertIn("material_preflight_inactivity_rules(", source)
        self.assertIn("execution_timeout = export_timeout +", source)
        self.assertIn("speedtree_material_preflight_cleanup_grace", source)
        self.assertGreaterEqual(source.count("send2ue_inactivity_rules("), 2)
        self.assertGreaterEqual(
            len(re.findall(r"_run_limited\([\s\S]{0,900}?\n\s*None,", source)),
            2,
        )
        self.assertGreaterEqual(source.count("inactivity_timeout=disk_export_timeout"), 2)

    def test_rpc_child_has_no_post_rpc_grace_poll_watchdog(self):
        source = (REPO / "sk_batch" / "jobs" / "send2ue_push_job.py").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("rpc_timeout + 60", source)
        self.assertNotIn("def wait_for_json", source)
        self.assertIn("SEND2UE_RPC_OWNED_START_MARKER", source)
        self.assertIn("SEND2UE_RPC_OWNED_DONE_MARKER", source)


if __name__ == "__main__":
    unittest.main()
