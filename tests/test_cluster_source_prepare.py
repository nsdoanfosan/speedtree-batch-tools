import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import cluster_source_prepare as source_prepare
from cluster_normalization_sync import ClusterSourceBuildRequiredError


class ClusterSourcePrepareTests(unittest.TestCase):
    def test_current_source_skips_every_build_stage(self):
        blend = Path(r"D:\Trees\Tree\Cluster\SK_branch_01.blend")
        target = Path(r"D:\Trees\Tree\SK_Tree_01.spm")

        with mock.patch.object(
            source_prepare,
            "resolve_normalization_recipe",
            return_value={"normalization_required": False},
        ), mock.patch.object(source_prepare, "_build_cluster_source") as build:
            result = source_prepare.prepare_cluster_source_if_required(
                blend,
                [target],
                blender_exe=Path(r"C:\Blender\blender.exe"),
                unit_probe_path=Path(r"C:\probe.json"),
            )

        self.assertEqual(result["status"], "current")
        build.assert_not_called()

    def test_stale_source_runs_pair_setup_build_and_contract_revalidation(self):
        blend = Path(r"D:\Trees\Tree\Cluster\SK_branch_01.blend")
        spm = blend.with_suffix(".spm")
        target = Path(r"D:\Trees\Tree\SK_Tree_01.spm")
        required = ClusterSourceBuildRequiredError(
            "stale",
            blend=blend,
            canonical_spm=spm,
            report_path=blend.parent / "reports" / "report.json",
            reason="source_identity_stale",
        )
        built = {
            "status": "rebuilt",
            "spm": str(spm),
            "blend": str(blend),
        }

        with mock.patch.object(
            source_prepare,
            "resolve_normalization_recipe",
            side_effect=[required, {"normalization_required": True}],
        ) as resolve, mock.patch.object(
            source_prepare,
            "prepare_cluster_spm_pair_for_job",
            return_value={"canonical_spm": spm},
        ) as pair, mock.patch.object(
            source_prepare,
            "load_config",
            return_value={"blender_exe": "old"},
        ), mock.patch.object(
            source_prepare,
            "_build_cluster_source",
            return_value=built,
        ) as build:
            result = source_prepare.prepare_cluster_source_if_required(
                blend,
                [target],
                blender_exe=Path(r"C:\Blender\blender.exe"),
                unit_probe_path=Path(r"C:\probe.json"),
            )

        self.assertEqual(result["status"], "rebuilt")
        self.assertEqual(result["reason"], "source_identity_stale")
        self.assertEqual(resolve.call_count, 2)
        pair.assert_called_once_with(spm)
        self.assertEqual(
            build.call_args.kwargs["cfg"]["blender_exe"],
            str(Path(r"C:\Blender\blender.exe").absolute()),
        )
        self.assertEqual(
            result["validated_normalization_recipe"],
            {"normalization_required": True},
        )

    def test_known_required_skips_duplicate_stale_source_preflight(self):
        blend = Path(r"D:\Trees\Tree\Cluster\SK_branch_01.blend")
        spm = blend.with_suffix(".spm")
        target = Path(r"D:\Trees\Tree\SK_Tree_01.spm")
        required = ClusterSourceBuildRequiredError(
            "stale",
            blend=blend,
            canonical_spm=spm,
            report_path=blend.parent / "reports" / "report.json",
            reason="source_identity_stale",
        )

        with mock.patch.object(
            source_prepare,
            "resolve_normalization_recipe",
            return_value={"normalization_required": True},
        ) as resolve, mock.patch.object(
            source_prepare,
            "prepare_cluster_spm_pair_for_job",
            return_value={"canonical_spm": spm},
        ), mock.patch.object(
            source_prepare,
            "load_config",
            return_value={"blender_exe": "old"},
        ), mock.patch.object(
            source_prepare,
            "_build_cluster_source",
            return_value={
                "status": "rebuilt",
                "spm": str(spm),
                "blend": str(blend),
            },
        ):
            result = source_prepare.prepare_cluster_source_if_required(
                blend,
                [target],
                blender_exe=Path(r"C:\Blender\blender.exe"),
                unit_probe_path=Path(r"C:\probe.json"),
                known_required=required,
            )

        self.assertEqual(resolve.call_count, 1)
        self.assertEqual(result["reason"], "source_identity_stale")
        self.assertEqual(
            result["validated_normalization_recipe"],
            {"normalization_required": True},
        )

    def test_build_runs_bone_setup_before_material_and_blender(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            spm = root / "Cluster" / "SK_branch_01.spm"
            blend = spm.with_suffix(".blend")
            spm.parent.mkdir(parents=True)
            spm.write_bytes(b"spm")
            blend.write_bytes(b"blend")
            fbx_ini = (
                root
                / "addon"
                / "presets"
                / "speedtree_10_1"
                / "Options_MA_Fbx.ini"
            )
            fbx_ini.parent.mkdir(parents=True)
            fbx_ini.write_text("ini", encoding="utf-8")
            speedtree_cli = fbx_ini.parents[2] / "speedtree_cli.py"
            speedtree_cli.write_text("# helper", encoding="utf-8")
            collision_cli = root / "speedtree_collision_cli.exe"
            collision_cli.write_bytes(b"collision cli")
            cfg = {
                "fbx_ini": str(fbx_ini),
                "speedtree_exe": str(root / "SpeedTree.exe"),
                "blender_exe": str(root / "blender.exe"),
                "spm_verify_timeout": 1,
                "speedtree_material_preflight_timeout": 1,
                "blender_job_timeout": 1,
            }
            stages = []
            commands = []

            def run_stage(command, log_file, *, timeout, stage):
                del log_file, timeout
                stages.append(stage)
                commands.append((stage, [str(value) for value in command]))
                report_path = Path(
                    str(command[command.index("--report") + 1])
                )
                report_path.parent.mkdir(parents=True, exist_ok=True)
                payload = (
                    [{
                        "status": "already-ok",
                        "final_spm_fingerprint":
                            source_prepare.file_content_fingerprint(spm),
                        "cluster_root_logical_postcondition": {"ok": True},
                    }]
                    if stage == "spm_bone_setup"
                    else {
                        "status": "ok",
                        **(
                            {
                                "cluster_source_build_contract": {
                                    "status": "ready"
                                }
                            }
                            if stage == "cluster_source_build"
                            else {}
                        ),
                    }
                )
                report_path.write_text(
                    json.dumps(payload),
                    encoding="utf-8",
                )
                return SimpleNamespace(returncode=0)

            with mock.patch.dict(
                os.environ,
                {source_prepare.COLLISION_CLI_ENV: str(collision_cli)},
                clear=False,
            ), mock.patch.object(
                source_prepare,
                "blender_open_file_window_titles",
                return_value=[],
            ), mock.patch.object(
                source_prepare,
                "LOG_DIR",
                root / "logs",
            ), mock.patch.object(
                source_prepare,
                "_run_stage",
                side_effect=run_stage,
            ), mock.patch.object(
                source_prepare,
                "write_repair_runtime_receipt",
                return_value=root / "runtime.json",
            ):
                result = source_prepare._build_cluster_source(
                    spm,
                    blend,
                    cfg=cfg,
                )

        self.assertEqual(
            stages,
            [
                "spm_bone_setup",
                "material_preflight",
                "cluster_source_build",
            ],
        )
        self.assertEqual(result["spm_bone_setup"]["status"], "already-ok")
        self.assertEqual(result["cluster_source_build"]["status"], "ok")
        material_command = next(
            command for stage, command in commands
            if stage == "material_preflight"
        )
        self.assertEqual(
            material_command[material_command.index("--speedtree-exe") + 1],
            str(collision_cli.resolve()),
        )
        cluster_command = next(
            command for stage, command in commands
            if stage == "cluster_source_build"
        )
        self.assertIn("--cluster-source-build-only", cluster_command)

    def test_failed_bone_setup_stops_before_material_or_blender(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            spm = root / "Cluster" / "SK_branch_01.spm"
            blend = spm.with_suffix(".blend")
            spm.parent.mkdir(parents=True)
            spm.write_bytes(b"spm")
            blend.write_bytes(b"blend")
            cfg = {
                "spm_verify_timeout": 1,
            }

            def fail_bones(command, log_file, *, timeout, stage):
                del log_file, timeout
                self.assertEqual(stage, "spm_bone_setup")
                report_path = Path(
                    str(command[command.index("--report") + 1])
                )
                report_path.parent.mkdir(parents=True, exist_ok=True)
                report_path.write_text(
                    json.dumps([
                        {
                            "status": "manual-required",
                            "error": "bone setup needs authoring",
                        }
                    ]),
                    encoding="utf-8",
                )
                return SimpleNamespace(returncode=0)

            with mock.patch.object(
                source_prepare,
                "blender_open_file_window_titles",
                return_value=[],
            ), mock.patch.object(
                source_prepare,
                "LOG_DIR",
                root / "logs",
            ), mock.patch.object(
                source_prepare,
                "_run_stage",
                side_effect=fail_bones,
            ) as run:
                with self.assertRaises(
                    source_prepare.ClusterSourcePreparationError
                ) as caught:
                    source_prepare._build_cluster_source(
                        spm,
                        blend,
                        cfg=cfg,
                    )

        self.assertEqual(caught.exception.stage, "spm_bone_setup")
        self.assertEqual(run.call_count, 1)

    def test_stale_bone_report_fingerprint_stops_before_blender(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            spm = root / "Cluster" / "SK_branch_01.spm"
            blend = spm.with_suffix(".blend")
            spm.parent.mkdir(parents=True)
            spm.write_bytes(b"spm")
            blend.write_bytes(b"blend")

            def stale_report(command, log_file, *, timeout, stage):
                del log_file, timeout
                self.assertEqual(stage, "spm_bone_setup")
                report_path = Path(
                    str(command[command.index("--report") + 1])
                )
                report_path.parent.mkdir(parents=True, exist_ok=True)
                report_path.write_text(
                    json.dumps([{
                        "status": "already-ok",
                        "final_spm_fingerprint": "stale",
                        "cluster_root_logical_postcondition": {"ok": True},
                    }]),
                    encoding="utf-8",
                )
                return SimpleNamespace(returncode=0)

            with mock.patch.object(
                source_prepare,
                "blender_open_file_window_titles",
                return_value=[],
            ), mock.patch.object(
                source_prepare,
                "LOG_DIR",
                root / "logs",
            ), mock.patch.object(
                source_prepare,
                "_run_stage",
                side_effect=stale_report,
            ) as run:
                with self.assertRaises(
                    source_prepare.ClusterSourcePreparationError
                ) as caught:
                    source_prepare._build_cluster_source(
                        spm,
                        blend,
                        cfg={"spm_verify_timeout": 1},
                    )

        self.assertEqual(caught.exception.stage, "spm_bone_setup")
        self.assertIn("SPM changed", str(caught.exception))
        self.assertEqual(run.call_count, 1)

    def test_blocked_source_build_restores_previous_pipeline_report(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            spm = root / "Cluster" / "SK_branch_01.spm"
            blend = spm.with_suffix(".blend")
            spm.parent.mkdir(parents=True)
            spm.write_bytes(b"spm")
            blend.write_bytes(b"blend")
            pipeline_report = (
                spm.parent
                / "reports"
                / "SK_branch_01_speedtree_assembly_pipeline_report_codex.json"
            )
            pipeline_report.parent.mkdir(parents=True)
            previous = b'{"status":"done","handoff_preflight":{"status":"ok"}}'
            pipeline_report.write_bytes(previous)
            fbx_ini = (
                root
                / "addon"
                / "presets"
                / "speedtree_10_1"
                / "Options_MA_Fbx.ini"
            )
            fbx_ini.parent.mkdir(parents=True)
            fbx_ini.write_text("ini", encoding="utf-8")
            (fbx_ini.parents[2] / "speedtree_cli.py").write_text(
                "# helper", encoding="utf-8"
            )
            collision_cli = root / "speedtree_collision_cli.exe"
            collision_cli.write_bytes(b"collision cli")
            cfg = {
                "fbx_ini": str(fbx_ini),
                "speedtree_exe": str(root / "SpeedTree.exe"),
                "blender_exe": str(root / "blender.exe"),
                "spm_verify_timeout": 1,
                "speedtree_material_preflight_timeout": 1,
                "blender_job_timeout": 1,
            }

            def run_stage(command, log_file, *, timeout, stage):
                del log_file, timeout
                report_path = Path(
                    str(command[command.index("--report") + 1])
                )
                report_path.parent.mkdir(parents=True, exist_ok=True)
                if stage == "spm_bone_setup":
                    report_path.write_text(
                        json.dumps([{
                            "status": "already-ok",
                            "final_spm_fingerprint":
                                source_prepare.file_content_fingerprint(spm),
                            "cluster_root_logical_postcondition": {"ok": True},
                        }]),
                        encoding="utf-8",
                    )
                    return SimpleNamespace(returncode=0)
                if stage == "material_preflight":
                    report_path.write_text(
                        json.dumps({"status": "ok"}),
                        encoding="utf-8",
                    )
                    return SimpleNamespace(returncode=0)
                pipeline_report.write_text(
                    json.dumps({
                        "status": "done",
                        "handoff_preflight": {"status": "blocked"},
                    }),
                    encoding="utf-8",
                )
                report_path.write_text(
                    json.dumps({
                        "status": "blocked",
                        "error": "missing_export_collection",
                    }),
                    encoding="utf-8",
                )
                return SimpleNamespace(returncode=1)

            with mock.patch.dict(
                os.environ,
                {source_prepare.COLLISION_CLI_ENV: str(collision_cli)},
                clear=False,
            ), mock.patch.object(
                source_prepare,
                "blender_open_file_window_titles",
                return_value=[],
            ), mock.patch.object(
                source_prepare,
                "LOG_DIR",
                root / "logs",
            ), mock.patch.object(
                source_prepare,
                "_run_stage",
                side_effect=run_stage,
            ):
                with self.assertRaises(
                    source_prepare.ClusterSourcePreparationError
                ):
                    source_prepare._build_cluster_source(
                        spm,
                        blend,
                        cfg=cfg,
                    )

            self.assertEqual(pipeline_report.read_bytes(), previous)


if __name__ == "__main__":
    unittest.main()
