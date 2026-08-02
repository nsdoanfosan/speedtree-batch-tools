import unittest
import tempfile
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]


class ExactTargetLauncherTests(unittest.TestCase):
    def test_no_arg_gui_and_arg_headless_contracts_are_both_present(self):
        for relative in (
            "pcg_st9_texture_batch/PCG_ST9_Texture_Batch.bat",
            "spm_generator_sync/SPM_Generator_Sync.bat",
        ):
            text = (ROOT / relative).read_text(encoding="utf-8").casefold()
            self.assertIn('if not "%~1"=="" goto headless', text)
            self.assertIn('start "" /d "%~dp0" pythonw "%guard%" "%launcher%"', text)
            self.assertIn('pythonw "%guard%" "%headless%" %*', text)
            self.assertIn('exit /b %errorlevel%', text)

    def test_public_cli_options_and_repeated_targets_are_documented_in_parsers(self):
        for relative in (
            "pcg_st9_texture_batch/exact_target_repair.py",
            "spm_generator_sync/exact_target_repair.py",
        ):
            text = (ROOT / relative).read_text(encoding="utf-8")
            for option in (
                "--repair-action",
                "--target-spm",
                "--parent-retry-id",
                "--request-id",
                "--receipt",
            ):
                self.assertIn(option, text)
            self.assertIn('action="append"', text)

    def test_pcg_cli_preserves_quoted_unicode_target_and_exit_status(self):
        from pcg_st9_texture_batch import exact_target_repair as cli

        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "공백 target.spm"
            receipt = Path(folder) / "영수증.json"
            with mock.patch.object(
                cli,
                "run_exact_target_request",
                return_value={"status": "completed"},
            ) as run:
                code = cli.main([
                    "--repair-action", "step3-standard",
                    "--target-spm", str(target),
                    "--parent-retry-id", "부모-118",
                    "--request-id", "요청-1",
                    "--receipt", str(receipt),
                ])
        self.assertEqual(code, 0)
        request = run.call_args.args[0]
        self.assertEqual(request["target_spms"], [str(target)])
        self.assertEqual(request["parent_retry_id"], "부모-118")

    def test_generator_cli_preserves_repeated_targets_and_failed_exit(self):
        from spm_generator_sync import exact_target_repair as cli

        with tempfile.TemporaryDirectory() as folder:
            first = Path(folder) / "첫 target.spm"
            second = Path(folder) / "둘 target.spm"
            with mock.patch.object(
                cli,
                "run_exact_target_request",
                return_value={"status": "failed"},
            ) as run:
                code = cli.main([
                    "--repair-action", "generator-sync-and-cluster",
                    "--target-spm", str(first),
                    "--target-spm", str(second),
                    "--parent-retry-id", "parent",
                    "--request-id", "request",
                    "--receipt", str(Path(folder) / "receipt.json"),
                ])
        self.assertEqual(code, 1)
        self.assertEqual(
            run.call_args.args[0]["target_spms"],
            [str(first), str(second)],
        )

    def test_public_cli_preserves_cancel_exit_130(self):
        from pcg_st9_texture_batch import exact_target_repair as cli

        with tempfile.TemporaryDirectory() as folder, mock.patch.object(
            cli,
            "run_exact_target_request",
            return_value={"status": "cancelled", "exit_code": 130},
        ):
            code = cli.main([
                "--repair-action", "step3-standard",
                "--target-spm", str(Path(folder) / "target.spm"),
                "--parent-retry-id", "parent",
                "--request-id", "request",
                "--receipt", str(Path(folder) / "receipt.json"),
            ])
        self.assertEqual(code, 130)


if __name__ == "__main__":
    unittest.main()
