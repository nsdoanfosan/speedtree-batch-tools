import importlib.machinery
import importlib.util
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


TOOL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOL_DIR))

import migrate_current_sk_textures
import sbs_auto


def load_gui_module():
    path = TOOL_DIR / "pcg_texture_gui.pyw"
    loader = importlib.machinery.SourceFileLoader(
        "pcg_texture_gui_manual_rerender_test", str(path)
    )
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class ManualFullRerenderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.gui = load_gui_module()

    def test_force_job_selection_keeps_complete_sets_and_marks_fresh_cook(self):
        app = self.gui.App.__new__(self.gui.App)
        item = {"name": "tree_test", "folder": r"D:\Trees\tree_test"}
        row = {
            "atlas_base": "M_tree_test",
            "texture_base": "T_tree_test",
        }
        app.items = {}
        app.texplan_errors = {}
        app._checked_texplan_rows = lambda: [(item, row)]
        app._all_texplan_rows = lambda: [(item, row)]

        def build_job(_row):
            return {
                "base": "M_tree_test",
                "texture_base": "T_tree_test",
                "mode": "render_only",
                "out_dir": r"D:\Trees\tree_test\texture",
                "inputs": {},
            }

        with mock.patch.object(
            self.gui, "build_texture_job", side_effect=build_job
        ), mock.patch.object(
            self.gui, "expected_job_size", return_value=(1, 1)
        ), mock.patch.object(
            self.gui, "complete_output_set", return_value=True
        ), mock.patch.object(
            self.gui, "job_needs_source_repair", return_value=False
        ), mock.patch.object(
            self.gui,
            "step3_existing_output_freshness",
            return_value={"fresh": True, "reason": "outputs_current"},
        ):
            normal_jobs, normal_skipped = app._step3_jobs()
            forced_jobs, forced_skipped = app._step3_jobs(
                force_rerender=True, all_rows=True
            )

        self.assertEqual(normal_jobs, [])
        self.assertEqual(normal_skipped, [])
        self.assertEqual(forced_skipped, [])
        self.assertEqual(len(forced_jobs), 1)
        self.assertTrue(forced_jobs[0]["force_cluster_recook"])

    def test_force_button_counts_unchecked_rows_across_the_whole_board(self):
        state = self.gui.step3_force_selection_state({
            "tree_a": {
                "checked": False,
                "item": {"cluster_items": [{"missing_export_maps": []}]},
            },
            "tree_b": {
                "checked": True,
                "item": {"cluster_items": [{"missing_export_maps": []}]},
            },
        })

        self.assertEqual(state, {
            "text": "③ 전체 다시 뽑기 (2세트)",
            "state": "normal",
        })

    def test_force_plan_normalizes_root_and_canonical_cluster_sk(self):
        app = self.gui.App.__new__(self.gui.App)
        folder = r"D:\Trees\bush_dogwood"
        root_sk = folder + r"\SK_bush_dogwood_01.spm"
        cluster_sk = folder + r"\cluster\SK_cluster_dogwood_01.spm"
        item = {
            "name": "bush_dogwood",
            "folder": folder,
            "target_spm_statuses": [{"sk_spm": root_sk}],
        }
        row = {
            "atlas_base": "M_leaf_dogwood_atlas_01",
            "texture_base": "T_leaf_dogwood_atlas_01",
            "material_targets": [
                {"spm": cluster_sk, "material_id": "6"},
            ],
        }
        app.items = {
            "dogwood": {
                "checked": False,
                "item": item,
            },
        }
        app._all_texplan_rows = lambda: [(item, row)]
        app._step3_jobs = mock.Mock(return_value=([{
            "base": row["atlas_base"],
            "texture_base": row["texture_base"],
            "item": item,
        }], []))
        app._step3_unreal_name_skips = mock.Mock(return_value=[])

        plan = app._build_step3_force_execution_plan()

        self.assertEqual(
            {value.lower() for value in plan["exact_step3_spms"]},
            {root_sk.lower(), cluster_sk.lower()},
        )
        self.assertEqual(
            plan["eligible_row_keys"],
            {
                self.gui.step3_texture_row_key(
                    item,
                    row["atlas_base"],
                ),
            },
        )

    def test_force_worker_syncs_once_after_all_renders_without_spm_processing(self):
        app = self.gui.App.__new__(self.gui.App)
        app.cfg = {
            "sbsrender_timeout": 12,
            "unreal_texture_sync_enabled": True,
        }
        app.status_var = mock.Mock()
        app.tree = mock.Mock()
        app.log = mock.Mock()
        app._step3_finished = mock.Mock()
        app._ui = lambda callback: callback()
        jobs = [
            {
                "base": f"M_tree_{index}",
                "texture_base": f"T_tree_{index}",
                "item": {"folder": fr"D:\Trees\tree_{index}"},
            }
            for index in (1, 2)
        ]
        results = [
            {
                "texture_base": job["texture_base"],
                "files": [
                    fr"D:\Trees\texture\{job['texture_base']}_color.tga"
                ],
            }
            for job in jobs
        ]
        sync_report = {
            "mode": "remote",
            "counts": {"reimported": 2},
            "errors": [],
        }

        with mock.patch.object(
            self.gui, "run_texture_job", side_effect=results
        ) as render, mock.patch.object(
            self.gui, "make_report",
            side_effect=AssertionError("force rerender must not normalize SPMs"),
        ):
            app._sync_pending_texture_files = mock.Mock(
                return_value=sync_report
            )
            baseline = self.gui.seal_exact_mutation_baseline(
                [], action="unit_test_force_rerender"
            )
            app._run_step3(
                jobs,
                affected_spms=[],
                sync_files=[],
                force_unreal_verify=False,
                require_all_renders_for_sync=True,
                exact_mutation_baseline=baseline,
            )

        self.assertEqual(render.call_count, 2)
        app._sync_pending_texture_files.assert_called_once()
        synced_files = app._sync_pending_texture_files.call_args.args[0]
        self.assertEqual(len(synced_files), 2)
        self.assertEqual(
            app._sync_pending_texture_files.call_args.kwargs["force_verify"],
            False,
        )

    def test_force_worker_skips_batch_sync_when_any_render_fails(self):
        app = self.gui.App.__new__(self.gui.App)
        app.cfg = {
            "sbsrender_timeout": 12,
            "unreal_texture_sync_enabled": True,
        }
        app.status_var = mock.Mock()
        app.tree = mock.Mock()
        app.log = mock.Mock()
        app._step3_finished = mock.Mock()
        app._ui = lambda callback: callback()
        app._sync_pending_texture_files = mock.Mock()
        jobs = [
            {
                "base": f"M_tree_{index}",
                "texture_base": f"T_tree_{index}",
                "item": {"folder": fr"D:\Trees\tree_{index}"},
            }
            for index in (1, 2)
        ]
        first_result = {
            "texture_base": "T_tree_1",
            "files": [r"D:\Trees\texture\T_tree_1_color.tga"],
        }

        with mock.patch.object(
            self.gui,
            "run_texture_job",
            side_effect=[first_result, RuntimeError("render failed")],
        ), mock.patch.object(
            self.gui, "make_report",
            side_effect=AssertionError("force rerender must not normalize SPMs"),
        ):
            baseline = self.gui.seal_exact_mutation_baseline(
                [], action="unit_test_force_rerender_failure"
            )
            app._run_step3(
                jobs,
                affected_spms=[],
                sync_files=[],
                force_unreal_verify=False,
                require_all_renders_for_sync=True,
                exact_mutation_baseline=baseline,
            )

        app._sync_pending_texture_files.assert_not_called()
        self.assertTrue(any(
            "Unreal 동기화 건너뜀" in call.args[0]
            for call in app.log.call_args_list
        ))

    def test_direct_job_forwards_manual_recook_to_graph_renderer(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            files = [root / f"T_test_{role}.tga" for role in sbs_auto.RENDER_MAPS]
            job = {
                "base": "M_test",
                "texture_base": "T_test",
                "mode": "direct",
                "graph": "T_test",
                "sbs": str(root / "test.sbs"),
                "out_dir": str(root),
                "normal_opengl": True,
                "direct_maps": list(sbs_auto.RENDER_MAPS),
                "normalize_cluster": False,
                "size_log2": (1, 1),
                "force_cluster_recook": True,
                "row": {
                    "texture_dir": str(root),
                    "texture_base": "T_test",
                    "legacy_export_maps": {},
                },
            }
            rendered = {
                "files": files,
                "size_log2": (1, 1),
                "pixel_size": (2, 2),
                "backup_dir": None,
                "changed_files": files,
                "unchanged_files": [],
                "created_files": files,
            }

            with mock.patch.object(
                sbs_auto, "set_managed_graph_resolution", return_value={
                    "changed": False,
                    "backup": None,
                    "size_log2": (1, 1),
                }
            ), mock.patch.object(
                sbs_auto, "render_sbs_graph_maps", return_value=rendered
            ) as render, mock.patch.object(
                migrate_current_sk_textures,
                "verify_complete_output_set",
                return_value=files,
            ), mock.patch.object(
                sbs_auto, "delete_legacy_m_outputs", return_value=[]
            ):
                migrate_current_sk_textures.run_job(job, {}, timeout=10)

        self.assertTrue(render.call_args.kwargs["force_recook"])


class ManualCookCacheTests(unittest.TestCase):
    def test_manual_recook_bypasses_an_existing_graph_cache(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            sbs = root / "authoring.sbs"
            sbs.write_text(
                "<package><graph><identifier v=\"T_Test\" /></graph></package>",
                encoding="utf-8",
            )
            cache = root / "cache"
            calls = []

            def fake_run(command, **_kwargs):
                calls.append(command)
                input_path = Path(command[command.index("--inputs") + 1])
                output_dir = Path(command[command.index("--output-path") + 1])
                output_dir.mkdir(parents=True, exist_ok=True)
                (output_dir / f"{input_path.stem}.sbsar").write_bytes(
                    f"cook-{len(calls)}".encode("ascii")
                )
                return SimpleNamespace(returncode=0, stdout="", stderr="")

            cfg = {"designer_dir": r"C:\Designer"}
            with mock.patch.object(subprocess, "run", side_effect=fake_run):
                first = sbs_auto.cook_sbs_graph_package(
                    sbs, ["T_Test"], cache, cfg=cfg
                )
                cached = sbs_auto.cook_sbs_graph_package(
                    sbs, ["T_Test"], cache, cfg=cfg
                )
                forced = sbs_auto.cook_sbs_graph_package(
                    sbs, ["T_Test"], cache, cfg=cfg, force_recook=True
                )

        self.assertEqual(first, cached)
        self.assertNotEqual(first, forced)
        self.assertEqual(len(calls), 2)


if __name__ == "__main__":
    unittest.main()
