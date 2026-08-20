"""Issue #155: keep exact Atlas failures local to their audit folder."""

from __future__ import annotations

import csv
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pcg_st9_texture_batch import pcg_texture_audit as audit


def _healthy_item(folder: Path) -> dict:
    return {
        "folder": str(folder),
        "name": folder.name,
        "status": "ready",
        "actions": [],
    }


def _resolution(target_spm: Path) -> dict:
    return {
        "schema_version": 1,
        "contract": "atlas_speedtree_manifest_resolution_v1",
        "target_spm": str(target_spm),
        "selected": [],
        "rejected": [],
        "shadowed": [],
        "conflicting": [{
            "path": str(target_spm.parent / "conflicting.json"),
            "kind": "exact_target_scope",
            "precedence": 1,
            "reason": "operational_candidate_disagreement",
        }],
        "missing": [],
    }


def _conflict(target_spm: Path) -> audit.AtlasManifestResolutionError:
    return audit.AtlasManifestResolutionError(
        "Atlas manifest ownership conflict for "
        f"{target_spm.name}: operational_candidate_disagreement",
        _resolution(target_spm),
    )


def _repair_plan(target_spm: Path, *, repairable: bool) -> dict:
    if repairable:
        return {
            "schema_version": 1,
            "status": "repairable",
            "reason_code": "atlas_manifest_mirror_conflict_repairable",
            "target_spm": str(target_spm),
            "authority": str(target_spm.parent / "authority.json"),
            "mirrors": [str(target_spm.parent / "stale-mirror.json")],
            "resolution": _resolution(target_spm),
        }
    return {
        "schema_version": 1,
        "status": "unrepairable",
        "reason_code": "atlas_manifest_ownership_conflict",
        "reason": "conflicting manifests claim different owners",
        "target_spm": str(target_spm),
        "authority": None,
        "mirrors": [],
        "resolution": _resolution(target_spm),
    }


class AuditFolderIsolationTests(unittest.TestCase):
    def test_single_folder_manifest_conflict_becomes_a_failed_item(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary) / "Tree_alpha"
            folder.mkdir()
            target = folder / "SK_Tree_alpha_01.spm"
            plan = _repair_plan(target, repairable=False)

            def audit_one(_folder):
                raise _conflict(target)

            with mock.patch.object(
                audit,
                "atlas_manifest_mirror_repair_plan",
                return_value=plan,
            ):
                items = audit._audit_report_folders([folder], audit_one)

        self.assertEqual(len(items), 1)
        failed = items[0]
        self.assertEqual(failed["folder"], str(folder))
        self.assertEqual(failed["name"], folder.name)
        self.assertEqual(failed["status"], "audit_failed")
        self.assertIs(failed["audit_complete"], False)
        self.assertEqual(
            failed["reason_token"], "atlas_manifest_ownership_conflict"
        )
        for key, value in plan.items():
            self.assertEqual(failed["evidence"][key], value)
        self.assertEqual(failed["evidence"]["target_spm"], str(target))
        self.assertEqual(
            failed["evidence"]["error_type"],
            "AtlasManifestResolutionError",
        )
        self.assertEqual(failed["failure"]["scope"], "folder")
        self.assertEqual(
            failed["failure"]["reason_token"],
            "atlas_manifest_ownership_conflict",
        )
        self.assertEqual(failed["failure"]["evidence"], failed["evidence"])

    def test_parallel_manifest_conflict_keeps_other_folders_and_order(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            folders = [root / name for name in ("alpha", "beta", "gamma")]
            for folder in folders:
                folder.mkdir()
            target = folders[1] / "SK_beta_01.spm"
            plan = _repair_plan(target, repairable=False)
            callbacks = []
            progress = []

            def audit_one(folder):
                if folder == folders[1]:
                    raise _conflict(target)
                return _healthy_item(folder)

            with mock.patch.object(
                audit,
                "atlas_manifest_mirror_repair_plan",
                return_value=plan,
            ):
                items = audit._audit_report_folders(
                    folders,
                    audit_one,
                    item_callback=lambda item, folder: callbacks.append(
                        (item["status"], folder)
                    ),
                    progress_callback=lambda completed, total, folder: (
                        progress.append((completed, total, folder))
                    ),
                )

        self.assertEqual(
            [item["status"] for item in items],
            ["ready", "audit_failed", "ready"],
        )
        self.assertCountEqual(
            callbacks,
            [("ready", folders[0]), ("audit_failed", folders[1]),
             ("ready", folders[2])],
        )
        self.assertEqual(len(progress), 3)
        self.assertEqual(
            sorted(completed for completed, _total, _folder in progress),
            [1, 2, 3],
        )
        self.assertTrue(all(total == 3 for _completed, total, _folder in progress))

    def test_unexpected_errors_remain_fail_closed_in_single_and_parallel_runs(self):
        for folders in ((Path("single"),), (Path("first"), Path("second"))):
            with self.subTest(folder_count=len(folders)):
                def audit_one(folder):
                    if folder == folders[0]:
                        raise RuntimeError("shared audit invariant failed")
                    return _healthy_item(folder)

                with self.assertRaisesRegex(
                    RuntimeError, "shared audit invariant failed"
                ):
                    audit._audit_report_folders(list(folders), audit_one)

    def test_manifest_error_for_target_outside_folder_is_not_localized(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            folder = root / "Tree_alpha"
            outside = root / "Tree_shared"
            folder.mkdir()
            outside.mkdir()
            target = outside / "SK_Tree_shared_01.spm"

            def audit_one(_folder):
                raise _conflict(target)

            with self.assertRaises(audit.AtlasManifestResolutionError):
                audit._audit_report_folders([folder], audit_one)

    def test_manifest_error_without_an_absolute_target_is_not_localized(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary) / "Tree_alpha"
            folder.mkdir()
            for resolution in ({}, {"target_spm": "SK_relative_01.spm"}):
                with self.subTest(resolution=resolution):
                    error = audit.AtlasManifestResolutionError(
                        "manifest resolution lacks an exact local target",
                        resolution,
                    )

                    def audit_one(_folder):
                        raise error

                    with self.assertRaises(audit.AtlasManifestResolutionError):
                        audit._audit_report_folders([folder], audit_one)

    def test_manifest_failure_preserves_repairable_and_unrepairable_evidence(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary) / "Tree_alpha"
            folder.mkdir()
            target = folder / "SK_Tree_alpha_01.spm"

            for repairable, expected_reason in (
                (True, "atlas_manifest_mirror_conflict_repairable"),
                (False, "atlas_manifest_ownership_conflict"),
            ):
                with self.subTest(repairable=repairable):
                    plan = _repair_plan(target, repairable=repairable)

                    def audit_one(_folder):
                        raise _conflict(target)

                    with mock.patch.object(
                        audit,
                        "atlas_manifest_mirror_repair_plan",
                        return_value=plan,
                    ) as classify:
                        failed = audit._audit_report_folders(
                            [folder], audit_one
                        )[0]

                    self.assertEqual(failed["reason_token"], expected_reason)
                    for key, value in plan.items():
                        self.assertEqual(failed["evidence"][key], value)
                    self.assertEqual(
                        failed["failure"],
                        {
                            "scope": "folder",
                            "reason_token": expected_reason,
                            "evidence": failed["evidence"],
                        },
                    )
                    classify.assert_called_once()
                    call = classify.call_args
                    self.assertEqual(Path(call.args[0]), target)
                    self.assertEqual(
                        call.kwargs["resolution"], _resolution(target)
                    )

    def test_failed_item_has_the_public_csv_shape(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary) / "Tree_alpha"
            folder.mkdir()
            target = folder / "SK_Tree_alpha_01.spm"
            plan = _repair_plan(target, repairable=False)

            def audit_one(_folder):
                raise _conflict(target)

            with mock.patch.object(
                audit,
                "atlas_manifest_mirror_repair_plan",
                return_value=plan,
            ):
                failed = audit._audit_report_folders([folder], audit_one)[0]

            csv_path = Path(temporary) / "audit.csv"
            audit.write_csv({"items": [failed]}, csv_path)
            with csv_path.open(newline="", encoding="utf-8-sig") as handle:
                rows = list(csv.DictReader(handle))

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["name"], folder.name)
        self.assertEqual(rows[0]["status"], "audit_failed")
        self.assertEqual(
            rows[0]["reason_token"], "atlas_manifest_ownership_conflict"
        )
        self.assertEqual(rows[0]["failure_target_spm"], str(target))
        self.assertTrue(rows[0]["actions"])
        for key in (
            "cluster_items",
            "chosen_spm",
            "sbs_files",
            "materials_missing_m_prefix",
            "normal_convention",
            "actions",
        ):
            self.assertIn(key, failed)

    def test_make_report_marks_single_failure_and_mixed_fleet_honestly(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            failed_folder = root / "Tree_failed"
            healthy_folder = root / "Tree_healthy"
            failed_folder.mkdir()
            healthy_folder.mkdir()
            target = failed_folder / "SK_Tree_failed_01.spm"
            plan = _repair_plan(target, repairable=False)

            def audit_folder(folder, _cfg, **_kwargs):
                if Path(folder) == failed_folder:
                    raise _conflict(target)
                return _healthy_item(Path(folder))

            cfg = {
                "tree_root": str(root),
                "source_texture_roots": [],
                "required_export_maps": [],
                "pcg_focus_data_assets": [],
                "pcg_positive_weight_only": True,
            }
            cases = (
                ([failed_folder], "failed", 1, 0),
                ([failed_folder, healthy_folder], "partial", 1, 1),
                ([healthy_folder], "ok", 0, 1),
            )
            for folders, status, failed_count, completed_count in cases:
                with self.subTest(status=status), mock.patch.object(
                    audit, "candidate_folders", return_value=folders
                ), mock.patch.object(
                    audit, "canonical_cluster_provider_map", return_value={}
                ), mock.patch.object(
                    audit, "audit_folder", side_effect=audit_folder
                ), mock.patch.object(
                    audit, "atlas_manifest_mirror_repair_plan",
                    return_value=plan,
                ), mock.patch.object(
                    audit, "attach_global_m_graphs", return_value={}
                ), mock.patch.object(
                    audit, "resolve_shared_atlas_entries", return_value=[]
                ), mock.patch.object(
                    audit, "refresh_texture_output_contract_states",
                    return_value=None,
                ), mock.patch.object(
                    audit, "local_target_mesh_names", return_value=[]
                ):
                    report = audit.make_report(cfg)

                self.assertEqual(report["status"], status)
                self.assertEqual(
                    report["summary"]["failed_folder_count"], failed_count
                )
                self.assertEqual(
                    report["summary"]["completed_folder_count"],
                    completed_count,
                )
                if failed_count:
                    failed = next(
                        item for item in report["items"]
                        if item["status"] == "audit_failed"
                    )
                    self.assertFalse(failed["audit_complete"])
                    self.assertEqual(failed["target_spm_statuses"], [])
                    self.assertEqual(
                        failed["reason_token"],
                        "atlas_manifest_ownership_conflict",
                    )
                    self.assertEqual(
                        report["failures"][0], failed["failure"]
                    )
                    if status == "failed":
                        self.assertEqual(report["stage"], "asset_audit")
                        self.assertEqual(
                            report["failure"], failed["failure"]
                        )

    def test_partial_report_persists_healthy_receipts_without_claiming_complete(self):
        partial = {
            "status": "partial",
            "summary": {"failed_folder_count": 1},
            "items": [
                {"name": "healthy", "status": "ready"},
                {
                    "name": "failed",
                    "status": "audit_failed",
                    "audit_complete": False,
                },
            ],
        }
        with mock.patch.object(
            audit,
            "persist_cluster_assembly_receipts",
            return_value=[Path("healthy-receipt.json")],
        ) as persist:
            state = audit.persist_cluster_assembly_receipts_safely(partial)

        persist.assert_called_once_with(partial, unchanged_out=[])
        self.assertEqual(state["written"], [Path("healthy-receipt.json")])
        self.assertFalse(state["live_audit_complete"])
        self.assertEqual(state["status"], "ok")

    def test_all_failed_folders_keep_ordered_atlas_failure_evidence(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            folders = [root / "Tree_alpha", root / "Tree_beta"]
            for folder in folders:
                folder.mkdir()
            targets = {
                folder: folder / f"SK_{folder.name}_01.spm"
                for folder in folders
            }

            def audit_folder(folder, _cfg, **_kwargs):
                raise _conflict(targets[Path(folder)])

            cfg = {
                "tree_root": str(root),
                "source_texture_roots": [],
                "required_export_maps": [],
            }
            with mock.patch.object(
                audit, "candidate_folders", return_value=folders
            ), mock.patch.object(
                audit, "canonical_cluster_provider_map", return_value={}
            ), mock.patch.object(
                audit, "audit_folder", side_effect=audit_folder
            ), mock.patch.object(
                audit,
                "atlas_manifest_mirror_repair_plan",
                side_effect=lambda target, **_kwargs: _repair_plan(
                    Path(target), repairable=False
                ),
            ), mock.patch.object(
                audit, "attach_global_m_graphs", return_value={}
            ), mock.patch.object(
                audit, "resolve_shared_atlas_entries", return_value=[]
            ), mock.patch.object(
                audit, "refresh_texture_output_contract_states",
                return_value=None,
            ), mock.patch.object(
                audit, "local_target_mesh_names", return_value=[]
            ):
                report = audit.make_report(cfg, mutation_authority=True)

        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["summary"]["failed_folder_count"], 2)
        self.assertEqual(
            report["failure"]["reason_token"],
            "atlas_manifest_ownership_conflict",
        )
        aggregate = report["failure"]["evidence"]
        self.assertEqual(aggregate["status"], "unrepairable")
        self.assertEqual(aggregate["failed_folder_count"], 2)
        self.assertEqual(
            [row["evidence"]["target_spm"] for row in aggregate["failures"]],
            [str(targets[folder]) for folder in folders],
        )

    def test_single_folder_second_pass_conflict_is_also_localized(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            folder = root / "Tree_alpha"
            folder.mkdir()
            target = folder / "SK_Tree_alpha_01.spm"
            blend = folder / "leaf.blend"
            blend.write_bytes(b"blend-current")
            plan = _repair_plan(target, repairable=False)
            calls = []

            def audit_folder(current, _cfg, **_kwargs):
                calls.append(Path(current))
                if len(calls) == 1:
                    audit.lookup_blend_source_images(blend, {})
                    return _healthy_item(Path(current))
                raise _conflict(target)

            def install(_cfg, session, **kwargs):
                requests = session.pending_requests()
                session.install_report(
                    {
                        "schema_version": 1,
                        "status": "ok",
                        "rows": [
                            {
                                "schema_version": 1,
                                "status": "ok",
                                "indexed_by_blender": True,
                                "blend": request["blend"],
                                "blend_sha256": request["blend_sha256"],
                                "images": [],
                            }
                            for request in requests
                        ],
                    },
                    requests,
                )
                metrics = kwargs.get("metrics")
                if metrics is not None:
                    metrics.update({
                        "request_count": len(requests),
                        "cache_hit": False,
                        "status": "ok",
                    })

            cfg = {
                "tree_root": str(root),
                "source_texture_roots": [],
                "required_export_maps": [],
            }
            with mock.patch.object(
                audit, "candidate_folders", return_value=[folder]
            ), mock.patch.object(
                audit, "canonical_cluster_provider_map", return_value={}
            ), mock.patch.object(
                audit, "audit_folder", side_effect=audit_folder
            ), mock.patch.object(
                audit, "ensure_blend_source_index", side_effect=install
            ), mock.patch.object(
                audit, "atlas_manifest_mirror_repair_plan", return_value=plan
            ), mock.patch.object(
                audit, "attach_global_m_graphs", return_value={}
            ), mock.patch.object(
                audit, "resolve_shared_atlas_entries", return_value=[]
            ), mock.patch.object(
                audit, "refresh_texture_output_contract_states",
                return_value=None,
            ), mock.patch.object(
                audit, "local_target_mesh_names", return_value=[]
            ):
                report = audit.make_report(cfg, mutation_authority=True)

        self.assertEqual(calls, [folder, folder])
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["summary"]["failed_folder_count"], 1)
        self.assertEqual(report["items"][0]["status"], "audit_failed")
        self.assertEqual(
            report["startup_timing"]["revalidated_folder_count"], 1
        )


if __name__ == "__main__":
    unittest.main()
