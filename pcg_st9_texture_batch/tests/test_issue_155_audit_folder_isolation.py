"""One unauditable folder must not end the whole-tree audit.

Before this, an `AtlasManifestResolutionError` raised inside a worker
propagated out of `future.result()` and terminated `make_report`. Three
production targets currently carry a conflicted Atlas manifest, so the
whole-tree audit could not complete, no Cluster Assembly receipt could be
regenerated for any asset, and every downstream assembly build was skipped.
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO_DIR = Path(__file__).resolve().parents[2]
TOOL_DIR = REPO_DIR / "pcg_st9_texture_batch"
for _candidate in (REPO_DIR, TOOL_DIR):
    if str(_candidate) not in sys.path:
        sys.path.insert(0, str(_candidate))

import pcg_texture_audit as audit  # noqa: E402
from atlas_manifest_resolver import AtlasManifestResolutionError  # noqa: E402


class AuditFolderIsolationTests(unittest.TestCase):
    def _run(self, failing_folder, error):
        folders = ["alpha", "beta", "gamma"]

        def audit_one(folder):
            if folder == failing_folder:
                raise error
            return {"folder": folder, "name": folder, "status": "ok"}

        with mock.patch.object(
            audit, "_audit_one_with_handoff_scope",
            side_effect=lambda fn, folder, **kwargs: fn(folder),
        ):
            return audit._audit_report_folders(folders, audit_one)

    def test_one_failing_folder_does_not_end_the_report(self):
        items = self._run(
            "beta",
            AtlasManifestResolutionError(
                "Atlas manifest ownership conflict for SK_Tree_elm_03.spm: "
                "operational_candidate_disagreement",
                {"target_spm": "SK_Tree_elm_03.spm"},
            ),
        )
        self.assertEqual(len(items), 3)
        self.assertEqual(
            [row["status"] for row in items],
            ["ok", "audit_failed", "ok"],
            "the other folders must still be audited",
        )

    def test_the_failed_folder_carries_a_named_registered_reason(self):
        from repair_reason_registry import REASON_REGISTRY

        items = self._run(
            "beta",
            AtlasManifestResolutionError(
                "ownership conflict", {"target_spm": "SK_Tree_elm_03.spm"}
            ),
        )
        failed = items[1]
        self.assertEqual(
            failed["reason_token"], "atlas_manifest_ownership_conflict"
        )
        self.assertIn(failed["reason_token"], REASON_REGISTRY)
        self.assertEqual(failed["evidence"]["folder"], "beta")
        self.assertIn("ownership conflict", failed["evidence"]["error"])
        self.assertTrue(failed["actions"])

    def test_an_unexpected_error_is_reported_generically(self):
        items = self._run("gamma", RuntimeError("disk went away"))
        failed = items[2]
        self.assertEqual(failed["status"], "audit_failed")
        self.assertEqual(failed["reason_token"], "asset_audit_failed")
        self.assertEqual(failed["evidence"]["error_type"], "RuntimeError")

    def test_the_failed_item_has_the_fields_make_report_reads(self):
        items = self._run("alpha", RuntimeError("boom"))
        failed = items[0]
        for key in (
            "folder", "name", "status", "actions", "target_spm_statuses",
            "target_mesh_names", "pcg_target_mesh_names", "pcg_target_meshes",
            "pcg_mesh_names", "pcg_data_assets", "level_mesh_names",
            "level_placements", "duplicate_target_mesh_names",
            "duplicate_pcg_target_mesh_names",
        ):
            self.assertIn(key, failed, f"make_report reads item[{key!r}]")

    def test_cancellation_still_stops_the_run(self):
        """Isolation must not swallow an operator cancel."""
        def audit_one(folder):
            return {"folder": folder, "name": folder, "status": "ok"}

        with mock.patch.object(
            audit, "_audit_one_with_handoff_scope",
            side_effect=lambda fn, folder, **kwargs: fn(folder),
        ):
            with self.assertRaises(RuntimeError):
                audit._audit_report_folders(
                    ["alpha", "beta"], audit_one, cancel_check=lambda: True
                )


if __name__ == "__main__":
    unittest.main()
