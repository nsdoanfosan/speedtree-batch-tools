"""The SK Batch list must show shipping SPMs only.

Capture staging, verify candidates and timestamped safety copies all reuse the
same ``SK_<name>.spm`` filename deeper in the tree, so a name-only filter
cannot separate them.  Only ``<owner>/SK_x.spm`` and ``<owner>/Cluster/x.spm``
are production identities.
"""
import sys
import tempfile
import unittest
from pathlib import Path


SK_BATCH_DIR = Path(__file__).resolve().parents[1]
REPO_DIR = SK_BATCH_DIR.parent
for candidate in (str(SK_BATCH_DIR), str(REPO_DIR)):
    if candidate not in sys.path:
        sys.path.insert(0, candidate)

from sk_common import scan_sk_spms  # noqa: E402
from speedtree_pipeline_contract import (  # noqa: E402
    is_production_spm_location,
    production_spm_folders,
)


PRODUCTION = (
    "Tree_elm/SK_Tree_elm_01.spm",
    "Tree_elm/SK_Tree_elm_02.spm",
    "Weed_ivy/SK_Weed_Ivy_roof_40degree_01.spm",
)

# Every one of these was observed in the live vegetation root.
WORK_ARTIFACTS = (
    "Tree_elm/_physical_10cm_backups/20260724_before_production/TreeRoot/SK_Tree_elm_01.spm",
    "Tree_elm/_codex_backups/speedtree_cluster_20260723_173611/SK_Tree_elm_01.spm",
    "Tree_elm/Cluster/_auto_capture_backups/branch_elm_01/20260724_124321/SK_Tree_elm_01.spm",
    "Tree_elm/Cluster/card_pipeline_outputs/branch_elm_01/speedtree/SK_Tree_elm_01.spm",
    "Tree_elm/Cluster/card_pipeline_outputs/branch_elm_01/speedtree_verify/SK_Tree_elm_01_mesh_1_candidate.spm",
    "Tree_elm/Cluster/card_pipeline_outputs/leaf_elm_01/blender_capture_staging/20260724_120513/SK_Tree_elm_01_auto_capture_QA.spm",
    "Tree_elm/Cluster/card_pipeline_outputs/leaf_elm_01/blender_capture_staging/20260724_120513/spm_qa_exact_scope/SK_Tree_elm_01.spm",
    "Tree_elm/_spm_backups/SK_Tree_elm_01.skbatch_backup_20260724_101010.spm",
)

# Cluster rows come from the pair inventory, not from scan_sk_spms.
CLUSTER = ("Tree_elm/Cluster/SK_branch_elm_01.spm",)


class ProductionLocationScanTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        for relative in PRODUCTION + WORK_ARTIFACTS + CLUSTER:
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"<SpeedTreeRaw/>")

    def tearDown(self):
        self._tmp.cleanup()

    def test_only_owner_level_sk_files_are_listed(self):
        found = {
            path.relative_to(self.root).as_posix() for path in scan_sk_spms(self.root)
        }
        self.assertEqual(found, set(PRODUCTION))

    def test_no_work_artifact_survives_the_scan(self):
        found = {
            path.relative_to(self.root).as_posix() for path in scan_sk_spms(self.root)
        }
        for artifact in WORK_ARTIFACTS:
            with self.subTest(artifact=artifact):
                self.assertNotIn(artifact, found)

    def test_cluster_files_are_left_to_the_pair_inventory(self):
        found = {
            path.relative_to(self.root).as_posix() for path in scan_sk_spms(self.root)
        }
        for cluster in CLUSTER:
            self.assertNotIn(cluster, found)

    def test_production_folders_are_owner_and_owner_cluster_only(self):
        folders = {
            folder.relative_to(self.root).as_posix()
            for folder in production_spm_folders(self.root)
        }
        self.assertIn(".", folders)
        self.assertIn("Tree_elm", folders)
        self.assertIn("Tree_elm/Cluster", folders)
        self.assertIn("Weed_ivy", folders)
        self.assertNotIn("Tree_elm/_codex_backups", folders)
        self.assertNotIn("Tree_elm/Cluster/card_pipeline_outputs", folders)

    def test_location_predicate_matches_the_scan(self):
        for relative in PRODUCTION + CLUSTER:
            with self.subTest(relative=relative):
                self.assertTrue(
                    is_production_spm_location(self.root / relative, self.root)
                )
        for relative in WORK_ARTIFACTS:
            with self.subTest(relative=relative):
                self.assertFalse(
                    is_production_spm_location(self.root / relative, self.root)
                )

    def test_a_root_pointed_at_one_vegetation_folder_still_works(self):
        found = {
            path.name for path in scan_sk_spms(self.root / "Tree_elm")
        }
        self.assertEqual(found, {"SK_Tree_elm_01.spm", "SK_Tree_elm_02.spm"})

    def test_missing_root_returns_nothing_instead_of_raising(self):
        self.assertEqual(scan_sk_spms(self.root / "no_such_root"), [])


if __name__ == "__main__":
    unittest.main()
