import os
import re
import sys
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SK_BATCH_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SK_BATCH_DIR))

import spm_audit
from spm_audit import bone_lengths_from_xml, estimate_relative_value_from_probe


def graph_generator(
    guid,
    name,
    generator_type,
    mode=0,
    style=0,
    hidden=False,
    skin_type=0,
    segment_mesh=False,
):
    bone_properties = ""
    if generator_type == "Branch":
        bone_properties = """
      <Property><Name>Physics:Bone style</Name><Value>0</Value></Property>
      <Property><Name>Physics:Bones</Name><Value>0</Value></Property>"""
    return f"""
  <Generator Type="{generator_type}">
    <Name>{name}</Name><GUID>{guid}</GUID><Hidden>{str(hidden).lower()}</Hidden>
    <Properties>
      <Property><Name>Generation:Mode</Name><Value>{mode}</Value></Property>
      <Property><Name>Generation:Style</Name><Value>{style}</Value></Property>
      <Property><Name>Skin:Type</Name><Value>{skin_type}</Value></Property>
      <Property><Name>Skin:Visibility</Name><Value>1</Value></Property>
      <Property><Name>Segments:Features:Mesh:Enabled</Name><Value>{str(segment_mesh).lower()}</Value></Property>
      {bone_properties}
    </Properties>
  </Generator>"""


def graph_node(guid, node_type, generator_guid, parent_guid):
    return f"""
  <Node Type="{node_type}">
    <GeneratorGUID>{generator_guid}</GeneratorGUID>
    <ParentGUID>{parent_guid}</ParentGUID><GUID>{guid}</GUID><Properties />
  </Node>"""


def mixed_base_graph_xml(leaf_style=0, root_type="Tree"):
    generators = [
        graph_generator("tree-gen", "Root Container", root_type),
        graph_generator("root", "Root", "Branch"),
        graph_generator("root-child", "Root Child", "Branch"),
        graph_generator("leaf-base", "Leaf Base", "Base", style=leaf_style),
        graph_generator("leaf-first", "Leaf First", "Branch"),
        graph_generator("leaf-internal", "Leaf Internal", "Branch"),
        graph_generator("branch-base", "Branch Base", "Base"),
        graph_generator("branch-first", "Branch First", "Branch"),
        graph_generator("branch-internal", "Branch Internal", "Branch"),
        graph_generator("branch-twig", "Branch Twig", "Branch"),
    ]
    nodes = [
        graph_node("tree-node", root_type, "tree-gen", ""),
        graph_node("root-node", "Branch", "root", "tree-node"),
        graph_node("root-child-node", "Branch", "root-child", "root-node"),
        graph_node("leaf-base-node", "Base", "leaf-base", "tree-node"),
        graph_node("leaf-first-node", "Branch", "leaf-first", "leaf-base-node"),
        graph_node("leaf-internal-node", "Branch", "leaf-internal", "leaf-first-node"),
        graph_node("branch-base-node", "Base", "branch-base", "tree-node"),
        graph_node("branch-first-node", "Branch", "branch-first", "branch-base-node"),
        graph_node(
            "branch-internal-node", "Branch", "branch-internal", "branch-first-node"
        ),
        graph_node(
            "branch-twig-node", "Branch", "branch-twig", "branch-internal-node"
        ),
    ]
    return "<SpeedTree>" + "".join(generators + nodes) + "</SpeedTree>"


def probe_xml(generator_counts):
    bones = []
    bone_id = 0
    for generator, count in generator_counts.items():
        for _ in range(count):
            bones.append(
                f'<Bone ID="{bone_id}" Generator="{generator}" '
                'StartX="0" StartY="0" StartZ="0" '
                'EndX="30.48" EndY="0" EndZ="0" />'
            )
            bone_id += 1
    return "<SpeedTreeRaw><Bones>" + "".join(bones) + "</Bones></SpeedTreeRaw>"


def cluster_graph_xml():
    generators = [
        graph_generator("tree-gen", "Tree", "Tree"),
        graph_generator(
            "hidden-root-a",
            "Visible Meshless Pivot A",
            "Branch",
            hidden=False,
            skin_type=3,
        ),
        graph_generator("structural-a", "Structural A", "Branch"),
        graph_generator("twig-a", "Twig A", "Branch"),
        graph_generator("needle-a", "Needle A", "Branch"),
        graph_generator(
            "hidden-root-b",
            "Hidden Root B",
            "Branch",
            hidden=True,
            skin_type=3,
        ),
        graph_generator(
            "hidden-middle-b",
            "Hidden Middle B",
            "Branch",
            hidden=True,
            skin_type=3,
        ),
        graph_generator("structural-b", "Structural B", "Branch"),
        graph_generator("needle-b", "Needle B", "Branch"),
    ]
    nodes = [
        graph_node("tree-node", "Tree", "tree-gen", ""),
        graph_node(
            "hidden-root-a-node",
            "Branch",
            "hidden-root-a",
            "tree-node",
        ),
        graph_node(
            "structural-a-node",
            "Branch",
            "structural-a",
            "hidden-root-a-node",
        ),
        graph_node("twig-a-node", "Branch", "twig-a", "structural-a-node"),
        graph_node("needle-a-1", "Branch", "needle-a", "twig-a-node"),
        graph_node("needle-a-2", "Branch", "needle-a", "twig-a-node"),
        graph_node(
            "hidden-root-b-node",
            "Branch",
            "hidden-root-b",
            "tree-node",
        ),
        graph_node(
            "hidden-middle-b-node",
            "Branch",
            "hidden-middle-b",
            "hidden-root-b-node",
        ),
        graph_node(
            "structural-b-node",
            "Branch",
            "structural-b",
            "hidden-middle-b-node",
        ),
        graph_node(
            "needle-b-node",
            "Branch",
            "needle-b",
            "structural-b-node",
        ),
    ]
    return "<SpeedTree>" + "".join(generators + nodes) + "</SpeedTree>"


def cluster_export_xml(generator_names):
    bones = []
    for bone_id, generator in enumerate(generator_names):
        bones.append(
            f'<Bone ID="{bone_id}" ParentID="-1" Generator="{generator}" '
            'StartX="0" StartY="0" StartZ="0" '
            'EndX="0" EndY="0" EndZ="30.48" />'
        )
    return "<SpeedTreeRaw><Bones>" + "".join(bones) + "</Bones></SpeedTreeRaw>"


class SpmCalibrationEstimateTests(unittest.TestCase):
    def test_cluster_plan_keeps_only_first_renderable_structural_roots(self):
        plan = spm_audit.plan_cluster_root_bones(cluster_graph_xml())

        self.assertTrue(plan["ready"])
        self.assertEqual(
            {item["name"] for item in plan["selected_generators"]},
            {"Structural A", "Structural B"},
        )
        self.assertEqual(plan["expected_root_bone_count"], 2)
        self.assertEqual(plan["disabled_generator_count"], 6)

        patched = spm_audit.apply_cluster_root_bone_plan(
            cluster_graph_xml(), plan
        )
        audit = spm_audit.audit_spm("unused.spm", text=patched)
        enabled = {
            item["name"]
            for item in audit["generators"]
            if item["style"] == 0.0 and item["bones"] == 1.0
        }
        disabled = {
            item["name"]
            for item in audit["generators"]
            if item["style"] == 0.0 and item["bones"] == 0.0
        }
        self.assertEqual(enabled, {"Structural A", "Structural B"})
        self.assertIn("Needle A", disabled)
        self.assertIn("Needle B", disabled)
        self.assertIn("Twig A", disabled)
        self.assertIn("Visible Meshless Pivot A", disabled)
        pivot = next(
            item
            for item in audit["generators"]
            if item["name"] == "Visible Meshless Pivot A"
        )
        self.assertTrue(pivot["hidden"])
        self.assertTrue(
            spm_audit.cluster_root_logical_postcondition(patched)["ok"]
        )
        broken = spm_audit.apply_branch_values(
            patched,
            [plan["selected_generator_indices"][0]],
            0.0,
            0.0,
        )
        self.assertFalse(
            spm_audit.cluster_root_logical_postcondition(broken)["ok"]
        )

    def test_cluster_calibration_writes_and_verifies_root_only_spm(self):
        source_xml = cluster_graph_xml()
        cfg = {
            "cluster_root_only_bones": True,
            "rename_materials": False,
            "tree_leaf_parent_red_gradient": False,
            "backup_spm": False,
        }
        with tempfile.TemporaryDirectory() as tmp:
            cluster_dir = Path(tmp) / "cluster"
            cluster_dir.mkdir()
            spm_path = cluster_dir / "SK_leaf_example_01.spm"
            spm_audit.write_spm(spm_path, source_xml)

            def fake_xml_export(_spm_path, _cfg, out_path):
                Path(out_path).write_text(
                    cluster_export_xml(["Structural A", "Structural B"]),
                    encoding="utf-8",
                )
                return out_path

            with mock.patch.object(
                spm_audit,
                "export_verify_xml",
                side_effect=fake_xml_export,
            ):
                with mock.patch.object(
                    spm_audit,
                    "export_verify_fbx_geometry",
                    return_value=True,
                ):
                    report = spm_audit.process_spm(
                        spm_path, cfg, log=lambda _message: None
                    )

            self.assertEqual(report["status"], "calibrated")
            self.assertEqual(report["total_bones"], 2)
            self.assertEqual(
                report["calibration"]["mode"],
                "cluster_first_renderable_root_absolute_1",
            )
            self.assertTrue(
                report["cluster_root_logical_postcondition"]["ok"]
            )
            self.assertEqual(
                report["final_spm_fingerprint"],
                spm_audit.file_content_fingerprint(spm_path),
            )
            final_audit = spm_audit.audit_spm(spm_path)
            enabled = {
                item["name"]
                for item in final_audit["generators"]
                if item["bones"] == 1.0
            }
            self.assertEqual(enabled, {"Structural A", "Structural B"})

    def test_cluster_fixed_point_skips_speedtree_without_prior_receipt(self):
        source_xml = cluster_graph_xml()
        fixed = spm_audit.apply_cluster_root_bone_plan(
            source_xml,
            spm_audit.plan_cluster_root_bones(source_xml),
        )
        cfg = {
            "cluster_root_only_bones": True,
            "rename_materials": False,
            "tree_leaf_parent_red_gradient": False,
            "backup_spm": False,
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cluster_dir = root / "cluster"
            cluster_dir.mkdir()
            spm_path = cluster_dir / "SK_leaf_example_01.spm"
            cfg["spm_calibration_receipt_dir"] = str(root / "receipts")
            spm_audit.write_spm(spm_path, fixed)

            with mock.patch.object(
                spm_audit,
                "export_verify_xml",
                side_effect=AssertionError("fixed point exported XML"),
            ), mock.patch.object(
                spm_audit,
                "export_verify_fbx_geometry",
                side_effect=AssertionError("fixed point exported FBX"),
            ):
                report = spm_audit.process_spm(
                    spm_path,
                    cfg,
                    log=lambda _message: None,
                )

        self.assertEqual(report["status"], "already-ok")
        self.assertEqual(report["bone_fast_path"]["status"], "hit")
        self.assertEqual(
            report["calibration"]["verification"],
            "live_logical_fixed_point",
        )
        self.assertTrue(
            report["calibration"]["speedtree_export_skipped"]
        )
        self.assertEqual(report["total_bones"], 2)

    def test_cluster_calibration_reaches_root_normalizer_when_generic_gate_is_off(self):
        source_xml = cluster_graph_xml()
        cfg = {
            "cluster_root_only_bones": True,
            "rename_materials": False,
            "tree_leaf_parent_red_gradient": False,
            "backup_spm": False,
        }
        generic_not_ready = {
            "ready": False,
            "mode": "all_bones_disabled",
            "error": "all visible Branch bone generators are disabled",
            "disabled_generators": [],
            "bone_graph": {},
        }
        with tempfile.TemporaryDirectory() as tmp:
            cluster_dir = Path(tmp) / "Cluster"
            cluster_dir.mkdir()
            spm_path = cluster_dir / "SK_branch_example_01.spm"
            spm_audit.write_spm(spm_path, source_xml)

            def fake_xml_export(_spm_path, _cfg, out_path):
                Path(out_path).write_text(
                    cluster_export_xml(["Structural A", "Structural B"]),
                    encoding="utf-8",
                )
                return out_path

            with mock.patch.object(
                spm_audit, "sk_readiness", return_value=generic_not_ready
            ), mock.patch.object(
                spm_audit,
                "export_verify_xml",
                side_effect=fake_xml_export,
            ), mock.patch.object(
                spm_audit,
                "export_verify_fbx_geometry",
                return_value=True,
            ):
                report = spm_audit.process_spm(
                    spm_path, cfg, log=lambda _message: None
                )

        self.assertEqual(report["status"], "calibrated")
        self.assertEqual(
            report["calibration"]["mode"],
            "cluster_first_renderable_root_absolute_1",
        )

    def test_speedtree_timeout_is_process_failure_and_restores_source(self):
        source_xml = mixed_base_graph_xml()
        cfg = {
            "target_bones_per_branch": 3.0,
            "max_total_bones": 2000,
            "total_window_low": 0.6,
            "total_window_high": 1.5,
            "value_cap": 64.0,
            "value_floor": 0.02,
            "max_calibration_rounds": 4,
            "probe_cache_enabled": False,
            "rename_materials": False,
            "backup_spm": False,
            "spm_verify_timeout": 120,
        }
        with tempfile.TemporaryDirectory() as tmp:
            spm_path = Path(tmp) / "SK_tree_timeout.spm"
            spm_audit.write_spm(spm_path, source_xml)
            original_bytes = spm_path.read_bytes()

            with mock.patch.object(
                spm_audit,
                "export_verify_xml",
                side_effect=spm_audit.SpeedTreeExportTimeout("XML", 120),
            ):
                report = spm_audit.process_spm(
                    spm_path, cfg, log=lambda _message: None
                )

            self.assertEqual(report["status"], "failed")
            self.assertEqual(
                report["calibration"]["mode"],
                "process_export_timeout",
            )
            self.assertEqual(report["calibration"]["timeout_seconds"], 120.0)
            self.assertEqual(report["failure_kind"], "process_timeout")
            self.assertEqual(spm_path.read_bytes(), original_bytes)

    def test_xml_subprocess_timeout_uses_export_timeout_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            options = Path(tmp) / "Options.ini"
            options.write_text(
                "[Options]\nTextureSkipWriting=true\n",
                encoding="utf-8",
            )
            cfg = {
                "speedtree_exe": "SpeedTree.exe",
                "xml_ini": str(options),
                "spm_verify_timeout": 120,
            }
            with mock.patch.object(
                spm_audit,
                "run_speedtree_export",
                side_effect=subprocess.TimeoutExpired("SpeedTree", 120),
            ):
                with self.assertRaises(
                    spm_audit.SpeedTreeExportTimeout
                ) as caught:
                    spm_audit.export_verify_xml(
                        "SK_test.spm", cfg, "out.xml"
                    )

        self.assertEqual(caught.exception.stage, "XML")
        self.assertEqual(caught.exception.timeout_seconds, 120.0)

    def test_speedtree_export_never_waits_on_an_inherited_pipe(self):
        """A Modeler descendant can hold a pipe open after the exe exits.

        Waiting on EOF there turns a finished export into a full-timeout
        failure, so the run helper must hand the child regular files and wait
        on the process handle only.
        """
        captured = {}
        events = []

        class FakeProcess:
            pid = 4321

            def wait(self, timeout=None):
                events.append("wait")
                captured["timeout"] = timeout
                return 0

        class FakeGate:
            def __enter__(self):
                events.append("gate-enter")

            def __exit__(self, *_args):
                events.append("gate-exit")

        def fake_popen(cmd, stdout=None, stderr=None, **kwargs):
            events.append("popen")
            captured["cmd"] = cmd
            captured["stdout"] = stdout
            captured["stderr"] = stderr
            captured["kwargs"] = kwargs
            stdout.write(b"exported\n")
            return FakeProcess()

        with mock.patch.object(
            spm_audit, "speedtree_export_gate", return_value=FakeGate()
        ), mock.patch.object(
            spm_audit.subprocess, "Popen", side_effect=fake_popen
        ):
            returncode, stdout, stderr = spm_audit.run_speedtree_export(
                ["SpeedTree.exe", "model.spm"], ".", 120
            )

        self.assertEqual((returncode, stdout, stderr), (0, "exported\n", ""))
        self.assertEqual(captured["timeout"], 0.5)
        self.assertIsNot(captured["stdout"], subprocess.PIPE)
        self.assertIsNot(captured["stderr"], subprocess.PIPE)
        # Real file objects expose fileno(); a pipe placeholder is a plain int.
        self.assertTrue(callable(getattr(captured["stdout"], "fileno", None)))
        self.assertTrue(callable(getattr(captured["stderr"], "fileno", None)))
        self.assertIs(captured["kwargs"].get("stdin"), subprocess.DEVNULL)
        self.assertEqual(
            events,
            ["gate-enter", "popen", "wait", "gate-exit"],
        )
        self.assertEqual(
            spm_audit.SPEEDTREE_EXPORT_MUTEX_DEFAULT,
            r"Local\PARK.SpeedTree.Modeler.Export.v1.slot0",
        )

    def test_speedtree_export_timeout_kills_the_whole_process_tree(self):
        killed = {}

        class HangingProcess:
            pid = 9876

            def wait(self, timeout=None):
                if "terminated" not in killed:
                    raise subprocess.TimeoutExpired("SpeedTree", timeout)
                return 1

        def fake_popen(cmd, stdout=None, stderr=None, **kwargs):
            return HangingProcess()

        def fake_run(cmd, **kwargs):
            killed["terminated"] = list(cmd)
            return mock.Mock(returncode=0)

        gate = mock.MagicMock()
        with mock.patch.object(
            spm_audit, "speedtree_export_gate", return_value=gate
        ), mock.patch.object(
            spm_audit.subprocess, "Popen", side_effect=fake_popen
        ):
            with mock.patch.object(spm_audit.subprocess, "run", side_effect=fake_run):
                with self.assertRaises(subprocess.TimeoutExpired):
                    spm_audit.run_speedtree_export(
                        ["SpeedTree.exe", "model.spm"], ".", 5
                    )

        if os.name == "nt":
            self.assertIn("/T", killed.get("terminated", []))
            self.assertIn("9876", killed.get("terminated", []))

    def test_active_child_cpu_extends_the_soft_deadline(self):
        class ProgressiveProcess:
            pid = 54321

            def __init__(self):
                self.wait_calls = 0

            def wait(self, timeout=None):
                self.wait_calls += 1
                if self.wait_calls < 6:
                    import time

                    time.sleep(timeout)
                    raise subprocess.TimeoutExpired("SpeedTree", timeout)
                return 0

        cpu = {"value": 0.0}

        def progressing_cpu(_process):
            cpu["value"] += 0.01
            return cpu["value"]

        with mock.patch.object(
            spm_audit.subprocess,
            "Popen",
            return_value=ProgressiveProcess(),
        ), mock.patch.object(
            spm_audit,
            "_process_cpu_seconds",
            side_effect=progressing_cpu,
        ), mock.patch.object(
            spm_audit,
            "speedtree_export_gate",
        ):
            result = spm_audit.run_speedtree_export(
                ["SpeedTree.exe", "model.spm"],
                ".",
                0.02,
                absolute_timeout=0.25,
                poll_interval=0.01,
            )

        self.assertEqual(result.returncode, 0)
        self.assertGreater(
            result.evidence["attempts"][0]["duration_seconds"],
            0.02,
        )
        self.assertGreater(
            result.evidence["attempts"][0]["progress_event_count"],
            0,
        )

    def test_idle_export_reports_stalled_evidence(self):
        class StalledProcess:
            pid = 54322

            def wait(self, timeout=None):
                import time

                time.sleep(timeout)
                raise subprocess.TimeoutExpired("SpeedTree", timeout)

        with mock.patch.object(
            spm_audit.subprocess,
            "Popen",
            return_value=StalledProcess(),
        ), mock.patch.object(
            spm_audit,
            "_process_cpu_seconds",
            return_value=0.0,
        ), mock.patch.object(
            spm_audit,
            "_terminate_speedtree_tree",
        ), mock.patch.object(
            spm_audit,
            "speedtree_export_gate",
        ):
            with self.assertRaises(subprocess.TimeoutExpired) as caught:
                spm_audit.run_speedtree_export(
                    ["SpeedTree.exe", "model.spm"],
                    ".",
                    0.025,
                    absolute_timeout=0.2,
                    poll_interval=0.01,
                )

        self.assertEqual(caught.exception.evidence["timeout_reason"], "stalled")
        self.assertEqual(
            caught.exception.evidence["failure_kind"],
            "internal_error",
        )

    def test_continuous_progress_still_stops_at_the_hard_cap(self):
        class BusyProcess:
            pid = 54323

            def wait(self, timeout=None):
                import time

                time.sleep(timeout)
                raise subprocess.TimeoutExpired("SpeedTree", timeout)

        cpu = {"value": 0.0}

        def progressing_cpu(_process):
            cpu["value"] += 0.01
            return cpu["value"]

        with mock.patch.object(
            spm_audit.subprocess,
            "Popen",
            return_value=BusyProcess(),
        ), mock.patch.object(
            spm_audit,
            "_process_cpu_seconds",
            side_effect=progressing_cpu,
        ), mock.patch.object(
            spm_audit,
            "_terminate_speedtree_tree",
        ), mock.patch.object(
            spm_audit,
            "speedtree_export_gate",
        ):
            with self.assertRaises(subprocess.TimeoutExpired) as caught:
                spm_audit.run_speedtree_export(
                    ["SpeedTree.exe", "model.spm"],
                    ".",
                    0.03,
                    absolute_timeout=0.075,
                    poll_interval=0.01,
                )

        self.assertEqual(caught.exception.evidence["timeout_reason"], "hard_cap")

    def test_access_violation_retries_twice_with_fresh_outputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "verify.xml"
            returncodes = [0xC0000005, -1073741819, 0]
            commands = []

            class FinishedProcess:
                pid = 54324

                def __init__(self, returncode):
                    self.returncode = returncode

                def wait(self, timeout=None):
                    return self.returncode

            def fake_popen(cmd, **_kwargs):
                commands.append(list(cmd))
                attempt = len(commands)
                staged = Path(cmd[cmd.index("-export") + 1])
                staged.write_bytes(f"attempt-{attempt}".encode("ascii"))
                return FinishedProcess(returncodes[attempt - 1])

            gate = mock.MagicMock()
            with mock.patch.object(
                spm_audit.subprocess,
                "Popen",
                side_effect=fake_popen,
            ), mock.patch.object(
                spm_audit,
                "_process_cpu_seconds",
                return_value=0.0,
            ), mock.patch.object(
                spm_audit,
                "speedtree_export_gate",
                return_value=gate,
            ):
                result = spm_audit.run_speedtree_export(
                    [
                        "SpeedTree.exe",
                        "model.spm",
                        "-export",
                        str(output),
                    ],
                    tmp,
                    1,
                    absolute_timeout=2,
                )

            self.assertEqual(result.returncode, 0)
            self.assertEqual(len(commands), 3)
            self.assertEqual(
                len({
                    command[command.index("-export") + 1]
                    for command in commands
                }),
                3,
            )
            self.assertEqual(output.read_bytes(), b"attempt-3")
            self.assertEqual(result.evidence["retry_count"], 2)
            self.assertEqual(gate.__enter__.call_count, 1)
            self.assertEqual(gate.__exit__.call_count, 1)

    def test_other_returncode_does_not_retry(self):
        class FailedProcess:
            pid = 54325

            def wait(self, timeout=None):
                return 7

        with mock.patch.object(
            spm_audit.subprocess,
            "Popen",
            return_value=FailedProcess(),
        ) as popen, mock.patch.object(
            spm_audit,
            "_process_cpu_seconds",
            return_value=0.0,
        ), mock.patch.object(
            spm_audit,
            "speedtree_export_gate",
        ):
            result = spm_audit.run_speedtree_export(
                ["SpeedTree.exe", "model.spm"],
                ".",
                1,
            )

        self.assertEqual(result.returncode, 7)
        self.assertEqual(result.evidence["result"], "non_retryable_returncode")
        self.assertEqual(popen.call_count, 1)

    def test_zero_returncode_without_staged_output_is_structured(self):
        class EmptySuccessProcess:
            pid = 54326

            def wait(self, timeout=None):
                return 0

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            spm_audit.subprocess,
            "Popen",
            return_value=EmptySuccessProcess(),
        ), mock.patch.object(
            spm_audit,
            "_process_cpu_seconds",
            return_value=0.0,
        ), mock.patch.object(
            spm_audit,
            "speedtree_export_gate",
        ):
            output = Path(tmp) / "verify.xml"
            result = spm_audit.run_speedtree_export(
                [
                    "SpeedTree.exe",
                    "model.spm",
                    "-export",
                    str(output),
                ],
                tmp,
                1,
            )

        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.evidence["result"], "output_missing")
        self.assertFalse(output.exists())

    def test_mtime_only_change_is_not_export_progress(self):
        self.assertFalse(
            spm_audit._content_signature_advanced(
                (4096, 100),
                (4096, 200),
            )
        )
        self.assertTrue(
            spm_audit._content_signature_advanced(
                (4096, 100),
                (8192, 100),
            )
        )

    def test_guid_graph_tree_uses_root_and_first_branch_base_stage(self):
        graph = spm_audit.analyze_branch_bone_graph(
            mixed_base_graph_xml(),
            spm_path="SK_tree_example.spm",
            base_categories={"Leaf Base": "leaf", "Branch Base": "branch"},
        )

        self.assertTrue(graph["ready"])
        self.assertEqual(graph["asset_kind"], "tree")
        self.assertEqual(
            {item["guid"] for item in graph["target_generators"]},
            {"root", "root-child", "branch-first"},
        )
        self.assertEqual(graph["root_target_generator_count"], 3)
        self.assertEqual(graph["base_excluded_generator_count"], 4)
        self.assertEqual(graph["base_generator_count"], 5)
        self.assertEqual(graph["activated_zero_bone_generator_count"], 3)
        self.assertEqual(graph["leaf_classic_any_excluded_count"], 1)

    def test_guid_graph_bush_includes_branch_base_internal_chain(self):
        graph = spm_audit.analyze_branch_bone_graph(
            mixed_base_graph_xml(),
            spm_path="SK_bush_example.spm",
            base_categories={"Leaf Base": "leaf", "Branch Base": "branch"},
        )

        self.assertTrue(graph["ready"])
        self.assertEqual(graph["asset_kind"], "bush")
        self.assertEqual(
            {item["guid"] for item in graph["target_generators"]},
            {"root", "root-child", "branch-first", "branch-internal"},
        )
        self.assertEqual(graph["base_internal_target_generator_count"], 1)
        self.assertNotIn(
            "branch-twig", {item["guid"] for item in graph["target_generators"]}
        )
        self.assertEqual(graph["base_excluded_generator_count"], 3)

    def test_weed_zone_parent_is_an_automatic_root(self):
        graph = spm_audit.analyze_branch_bone_graph(
            mixed_base_graph_xml(root_type="Zone"),
            spm_path="SK_weed_example.spm",
            base_categories={"Leaf Base": "leaf", "Branch Base": "branch"},
        )

        self.assertTrue(graph["ready"])
        targets = {item["guid"] for item in graph["target_generators"]}
        self.assertIn("root", targets)
        self.assertIn("root-child", targets)

    def test_nonclassic_leaf_base_first_stage_is_included(self):
        graph = spm_audit.analyze_branch_bone_graph(
            mixed_base_graph_xml(leaf_style=1),
            spm_path="SK_tree_example.spm",
            base_categories={"Leaf Base": "leaf", "Branch Base": "branch"},
        )

        targets = {item["guid"] for item in graph["target_generators"]}
        self.assertIn("leaf-first", targets)
        self.assertNotIn("leaf-internal", targets)
        self.assertEqual(graph["leaf_classic_any_excluded_count"], 0)

    def test_same_guid_from_tree_and_base_is_an_error(self):
        xml = mixed_base_graph_xml().replace(
            graph_node("branch-first-node", "Branch", "branch-first", "branch-base-node"),
            graph_node("branch-first-node", "Branch", "root-child", "branch-base-node"),
        )
        graph = spm_audit.analyze_branch_bone_graph(
            xml,
            spm_path="SK_tree_example.spm",
            base_categories={"Leaf Base": "leaf", "Branch Base": "branch"},
        )

        self.assertFalse(graph["ready"])
        self.assertEqual(
            {item["guid"] for item in graph["ambiguous_shared_guids"]},
            {"root-child", "branch-internal", "branch-twig"},
        )
        self.assertTrue(any("both Tree and Base" in error for error in graph["errors"]))

    def test_cap_disables_base_before_preserving_tree_relative_density(self):
        source_xml = mixed_base_graph_xml()
        graph = spm_audit.analyze_branch_bone_graph(
            source_xml,
            spm_path="SK_tree_priority.spm",
            base_categories={"Leaf Base": "leaf", "Branch Base": "branch"},
        )
        cfg = {
            "target_bones_per_branch": 2.0,
            "max_total_bones": 100,
            "total_window_low": 0.6,
            "total_window_high": 1.5,
            "seed_relative_value": 0.5,
            "value_cap": 64.0,
            "value_floor": 0.02,
            "max_calibration_rounds": 4,
            "fast_skip_problem_spm": True,
            "probe_cache_enabled": False,
            "rename_materials": False,
            "backup_spm": False,
        }

        with tempfile.TemporaryDirectory() as tmp:
            spm_path = Path(tmp) / "SK_tree_priority.spm"
            spm_audit.write_spm(spm_path, source_xml)
            exports = iter(
                (
                    probe_xml({"Root": 10, "Root Child": 10, "Branch First": 100}),
                    probe_xml({"Root": 10, "Root Child": 10}),
                    probe_xml({"Root": 20, "Root Child": 20}),
                )
            )
            written_settings = []

            def fake_export(current_spm, _cfg, out_path):
                written_settings.append(spm_audit.audit_spm(current_spm)["generators"])
                Path(out_path).write_text(next(exports), encoding="utf-8")
                return out_path

            with mock.patch.object(
                spm_audit, "export_verify_xml", side_effect=fake_export
            ) as export_mock:
                with mock.patch.object(
                    spm_audit,
                    "export_verify_fbx_geometry",
                    return_value=True,
                ):
                    _, rounds, total, meta, _, _, _ = spm_audit.calibrate_bones(
                        spm_path,
                        cfg,
                        log=lambda _message: None,
                        source_text=source_xml,
                        source_audit={
                            "bone_graph": graph,
                            "generators": spm_audit.audit_spm(
                                spm_path
                            )["generators"],
                        },
                    )

            self.assertEqual(export_mock.call_count, 3)
            self.assertEqual(total, 40)
            self.assertTrue(meta["capped"])
            self.assertTrue(meta["base_priority_applied"])
            self.assertEqual(meta["disabled_base_generator_count"], 5)
            self.assertFalse(meta["density_reduced_for_cap"])
            self.assertEqual(meta["calibration_branch_count"], 20)
            self.assertEqual(meta["target_total"], 40)
            self.assertEqual(
                rounds[1]["phase"], "probe(tree-only-after-base-disable)"
            )

            tree_only_probe = {item["name"]: item for item in written_settings[1]}
            self.assertEqual(tree_only_probe["Branch First"]["style"], 0.0)
            self.assertEqual(tree_only_probe["Branch First"]["bones"], 0.0)
            self.assertEqual(tree_only_probe["Root"]["style"], 0.0)
            self.assertEqual(tree_only_probe["Root"]["bones"], 1.0)

            final_settings = {item["name"]: item for item in written_settings[2]}
            self.assertEqual(final_settings["Branch First"]["style"], 0.0)
            self.assertEqual(final_settings["Branch First"]["bones"], 0.0)
            self.assertEqual(final_settings["Root"]["style"], 1.0)
            self.assertGreater(final_settings["Root"]["bones"], 0.0)

    def test_tree_relative_density_reduces_only_after_base_is_disabled(self):
        source_xml = mixed_base_graph_xml()
        graph = spm_audit.analyze_branch_bone_graph(
            source_xml,
            spm_path="SK_tree_priority_dense.spm",
            base_categories={"Leaf Base": "leaf", "Branch Base": "branch"},
        )
        cfg = {
            "target_bones_per_branch": 2.0,
            "max_total_bones": 100,
            "total_window_low": 0.6,
            "total_window_high": 1.5,
            "value_cap": 64.0,
            "value_floor": 0.02,
            "max_calibration_rounds": 4,
            "fast_skip_problem_spm": True,
            "probe_cache_enabled": False,
            "rename_materials": False,
            "backup_spm": False,
        }

        with tempfile.TemporaryDirectory() as tmp:
            spm_path = Path(tmp) / "SK_tree_priority_dense.spm"
            spm_audit.write_spm(spm_path, source_xml)
            exports = iter(
                (
                    probe_xml({"Root": 40, "Root Child": 40, "Branch First": 100}),
                    probe_xml({"Root": 40, "Root Child": 40}),
                    probe_xml({"Root": 50, "Root Child": 50}),
                )
            )

            def fake_export(_spm_path, _cfg, out_path):
                Path(out_path).write_text(next(exports), encoding="utf-8")
                return out_path

            with mock.patch.object(
                spm_audit, "export_verify_xml", side_effect=fake_export
            ):
                with mock.patch.object(
                    spm_audit,
                    "export_verify_fbx_geometry",
                    return_value=True,
                ):
                    _, _, total, meta, _, _, _ = spm_audit.calibrate_bones(
                        spm_path,
                        cfg,
                        log=lambda _message: None,
                        source_text=source_xml,
                        source_audit={
                            "bone_graph": graph,
                            "generators": spm_audit.audit_spm(
                                spm_path
                            )["generators"],
                        },
                    )

            self.assertEqual(total, 100)
            self.assertTrue(meta["base_priority_applied"])
            self.assertTrue(meta["density_reduced_for_cap"])
            self.assertEqual(meta["target_total"], 100)

    def test_probe_underflow_cannot_be_reported_as_success(self):
        generators = [
            graph_generator("tree-gen", "Tree", "Tree"),
            graph_generator("root", "Root", "Branch"),
        ]
        nodes = [graph_node("tree-node", "Tree", "tree-gen", "")]
        nodes.extend(
            graph_node(f"root-node-{index}", "Branch", "root", "tree-node")
            for index in range(4)
        )
        source_xml = "<SpeedTree>" + "".join(generators + nodes) + "</SpeedTree>"
        three_bone_probe = """\
<SpeedTreeRaw><Bones>
  <Bone ID="0" Generator="Root" StartX="0" StartY="0" StartZ="0" EndX="30.48" EndY="0" EndZ="0" />
  <Bone ID="1" Generator="Root" StartX="0" StartY="0" StartZ="0" EndX="30.48" EndY="0" EndZ="0" />
  <Bone ID="2" Generator="Root" StartX="0" StartY="0" StartZ="0" EndX="30.48" EndY="0" EndZ="0" />
</Bones></SpeedTreeRaw>
"""
        cfg = {
            "target_bones_per_branch": 3.0,
            "max_total_bones": 2000,
            "total_window_low": 0.6,
            "total_window_high": 1.5,
            "value_cap": 64.0,
            "value_floor": 0.02,
            "max_calibration_rounds": 4,
            "fast_skip_problem_spm": True,
            "probe_cache_enabled": False,
            "rename_materials": False,
            "backup_spm": False,
        }
        with tempfile.TemporaryDirectory() as tmp:
            spm_path = Path(tmp) / "SK_tree_underflow.spm"
            spm_audit.write_spm(spm_path, source_xml)
            original = spm_path.read_bytes()

            def fake_export(_spm_path, _cfg, out_path):
                Path(out_path).write_text(three_bone_probe, encoding="utf-8")
                return out_path

            with mock.patch.object(
                spm_audit, "export_verify_xml", side_effect=fake_export
            ) as export_mock:
                report = spm_audit.process_spm(
                    spm_path, cfg, log=lambda _message: None
                )

            self.assertEqual(export_mock.call_count, 1)
            self.assertEqual(report["status"], "manual-required")
            self.assertEqual(
                report["calibration"]["mode"], "manual_required_probe_underflow"
            )
            self.assertEqual(report["calibration"]["actual_probe_branch_count"], 3)
            self.assertEqual(spm_path.read_bytes(), original)

    def test_absolute_probe_bone_lengths_are_read_from_xml(self):
        xml = """\
<SpeedTreeRaw>
  <Bones>
    <Bone ID="0" ParentID="-1" StartX="0" StartY="0" StartZ="0"
          EndX="30.48" EndY="0" EndZ="0" Generator="Trunk" />
    <Bone ID="1" ParentID="0" StartX="0" StartY="0" StartZ="0"
          EndX="0" EndY="40" EndZ="0" Generator="Branch" />
  </Bones>
</SpeedTreeRaw>
"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "probe.xml"
            path.write_text(xml, encoding="utf-8")
            self.assertEqual(bone_lengths_from_xml(path), [30.48, 40.0])

    def test_estimate_uses_one_foot_speedtree_library_units(self):
        lengths_cm = [30.48] * 10
        value = estimate_relative_value_from_probe(lengths_cm, 20, 0.02, 64.0)
        self.assertAlmostEqual(value, 2.0, places=6)

    def test_estimate_respects_floor_cap_and_empty_probe(self):
        self.assertEqual(estimate_relative_value_from_probe([1.0], 1000, 0.02, 4.0), 4.0)
        self.assertIsNone(estimate_relative_value_from_probe([], 10, 0.02, 64.0))

    def test_empty_probe_lengths_use_one_shared_authored_relative_value(self):
        audit = {
            "generators": [
                {"style": 1.0, "bones": 3.7634},
                {"style": 0.0, "bones": 0.0},
                {"style": 1.0, "bones": 3.7634},
            ]
        }
        self.assertAlmostEqual(
            spm_audit.shared_authored_relative_value(audit, [0, 1, 2]),
            3.7634,
            places=4,
        )
        audit["generators"][2]["bones"] = 4.0
        self.assertIsNone(
            spm_audit.shared_authored_relative_value(audit, [0, 1, 2])
        )

    def test_relative_failure_modes_are_specific(self):
        self.assertEqual(
            spm_audit._relative_failure_mode(
                [(0.5, 0), (1.5, 0)],
                lo=100,
                hi=200,
                value_floor=0.02,
                value_cap=64.0,
            ),
            "manual_required_relative_zero",
        )
        self.assertEqual(
            spm_audit._relative_failure_mode(
                [(1.0, 50), (2.0, 50)],
                lo=100,
                hi=200,
                value_floor=0.02,
                value_cap=64.0,
            ),
            "manual_required_relative_plateau",
        )
        self.assertEqual(
            spm_audit._relative_failure_mode(
                [(1.0, 120), (2.0, 90)],
                lo=100,
                hi=200,
                value_floor=0.02,
                value_cap=64.0,
            ),
            "manual_required_relative_nonmonotonic",
        )

    def test_persisted_fast_skip_flag_cannot_disable_bounded_correction(self):
        source_xml = """\
<SpeedTree>
  <Generator Type="Tree">
    <Name>Tree</Name><GUID>tree-generator-guid</GUID><Properties />
  </Generator>
  <Generator Type="Branch">
    <Name>Trunk</Name>
    <GUID>trunk-guid</GUID>
    <Properties>
      <Property><Name>Physics:Bone style</Name><Value>1</Value></Property>
      <Property><Name>Physics:Bones</Name><Value>1</Value></Property>
    </Properties>
  </Generator>
  <Node Type="Tree">
    <GeneratorGUID>tree-generator-guid</GeneratorGUID>
    <ParentGUID></ParentGUID><GUID>tree-guid</GUID><Properties />
  </Node>
  <Node Type="Branch">
    <GeneratorGUID>trunk-guid</GeneratorGUID>
    <ParentGUID>tree-guid</ParentGUID>
    <Name>Trunk node</Name>
    <GUID>node-guid</GUID>
    <Hidden>false</Hidden>
    <Extra><m_bValidPosition>true</m_bValidPosition></Extra>
    <Properties />
  </Node>
</SpeedTree>
"""
        probe_xml = """\
<SpeedTreeRaw><Bones>
  <Bone ID="0" ParentID="-1" StartX="0" StartY="0" StartZ="0"
        EndX="30.48" EndY="0" EndZ="0" Generator="Trunk" />
</Bones></SpeedTreeRaw>
"""
        failed_relative_xml = "<SpeedTreeRaw><Bones /></SpeedTreeRaw>"
        corrected_relative_xml = """\
<SpeedTreeRaw><Bones>
  <Bone ID="0" ParentID="-1" Generator="Trunk" />
  <Bone ID="1" ParentID="0" Generator="Trunk" />
  <Bone ID="2" ParentID="1" Generator="Trunk" />
</Bones></SpeedTreeRaw>
"""
        cfg = {
            "target_bones_per_branch": 3.0,
            "max_total_bones": 2000,
            "total_window_low": 0.6,
            "total_window_high": 1.5,
            "seed_relative_value": 0.5,
            "value_cap": 64.0,
            "value_floor": 0.02,
            "max_calibration_rounds": 4,
            "fast_skip_problem_spm": True,
            "rename_materials": False,
            "backup_spm": True,
        }

        with tempfile.TemporaryDirectory() as tmp:
            spm_path = Path(tmp) / "SK_problem.spm"
            spm_audit.write_spm(spm_path, source_xml)
            exports = iter(
                (probe_xml, failed_relative_xml, corrected_relative_xml)
            )

            def fake_export(_spm_path, _cfg, out_path):
                Path(out_path).write_text(next(exports), encoding="utf-8")
                return out_path

            with mock.patch.object(
                spm_audit,
                "export_verify_xml",
                side_effect=fake_export,
            ) as export_mock, mock.patch.object(
                spm_audit,
                "export_verify_fbx_geometry",
                return_value=True,
            ):
                report = spm_audit.process_spm(
                    spm_path, cfg, log=lambda _message: None
                )

            self.assertEqual(export_mock.call_count, 3)
            self.assertEqual(report["status"], "calibrated")
            relative_rounds = [
                row
                for row in report["rounds"]
                if row["phase"].startswith("relative round")
            ]
            self.assertEqual(len(relative_rounds), 2)
            self.assertEqual(relative_rounds[0]["total_bones"], 0)
            self.assertEqual(relative_rounds[1]["total_bones"], 3)

    def test_armature_only_spm_runs_absolute_geometry_fallback(self):
        source_xml = """\
<SpeedTree>
  <Generator Type="Tree">
    <Name>Tree</Name><GUID>tree-generator-guid</GUID><Properties />
  </Generator>
  <Generator Type="Branch">
    <Name>Trunk</Name><GUID>trunk-guid</GUID>
    <Properties>
      <Property><Name>Physics:Bone style</Name><Value>1</Value></Property>
      <Property><Name>Physics:Bones</Name><Value>1</Value></Property>
    </Properties>
  </Generator>
  <Node Type="Tree">
    <GeneratorGUID>tree-generator-guid</GeneratorGUID>
    <ParentGUID></ParentGUID><GUID>tree-guid</GUID><Properties />
  </Node>
  <Node Type="Branch">
    <GeneratorGUID>trunk-guid</GeneratorGUID>
    <ParentGUID>tree-guid</ParentGUID><GUID>branch-node-guid</GUID><Properties />
  </Node>
</SpeedTree>
"""
        probe_xml = """\
<SpeedTreeRaw><Bones>
  <Bone ID="0" ParentID="-1" StartX="0" StartY="0" StartZ="0"
        EndX="30.48" EndY="0" EndZ="0" Generator="Trunk" />
</Bones></SpeedTreeRaw>
"""
        valid_relative_xml = """\
<SpeedTreeRaw><Bones>
  <Bone ID="0" ParentID="-1" Generator="Trunk" />
  <Bone ID="1" ParentID="0" Generator="Trunk" />
  <Bone ID="2" ParentID="1" Generator="Trunk" />
</Bones></SpeedTreeRaw>
"""
        cfg = {
            "target_bones_per_branch": 3.0,
            "max_total_bones": 2000,
            "total_window_low": 0.6,
            "total_window_high": 1.5,
            "seed_relative_value": 0.5,
            "value_cap": 64.0,
            "value_floor": 0.02,
            "max_calibration_rounds": 4,
            "fast_skip_problem_spm": True,
            "rename_materials": False,
            "backup_spm": True,
        }

        with tempfile.TemporaryDirectory() as tmp:
            spm_path = Path(tmp) / "SK_armature_only.spm"
            spm_audit.write_spm(spm_path, source_xml)
            exports = iter((probe_xml, valid_relative_xml, valid_relative_xml))

            def fake_export(_spm_path, _cfg, out_path):
                Path(out_path).write_text(next(exports), encoding="utf-8")
                return out_path

            with mock.patch.object(spm_audit, "export_verify_xml", side_effect=fake_export) as xml_mock:
                with mock.patch.object(
                    spm_audit,
                    "export_verify_fbx_geometry",
                    side_effect=(False, True),
                ) as fbx_mock:
                    report = spm_audit.process_spm(spm_path, cfg, log=lambda _message: None)

            self.assertEqual(xml_mock.call_count, 3)
            self.assertEqual(fbx_mock.call_count, 2)
            self.assertEqual(report["status"], "calibrated")
            self.assertEqual(
                report["calibration"]["mode"],
                "root_only_absolute_fallback",
            )
            self.assertEqual(
                report["calibration"]["absolute_bones_per_branch"], 1
            )

    def test_identical_final_bones_restore_exact_source_bytes_and_timestamp(self):
        source_xml = """\
<SpeedTree>
  <Generator Type="Tree">
    <Name>Tree</Name><GUID>tree-generator-guid</GUID><Properties />
  </Generator>
  <Generator Type="Branch">
    <Name>Trunk</Name><GUID>trunk-guid</GUID>
    <Properties>
      <Property><Name>Physics:Bone style</Name><Value>1</Value></Property>
      <Property><Name>Physics:Bones</Name><Value>1</Value></Property>
    </Properties>
  </Generator>
  <Node Type="Tree">
    <GeneratorGUID>tree-generator-guid</GeneratorGUID>
    <ParentGUID></ParentGUID><GUID>tree-guid</GUID><Properties />
  </Node>
  <Node Type="Branch">
    <GeneratorGUID>trunk-guid</GeneratorGUID>
    <ParentGUID>tree-guid</ParentGUID><GUID>branch-node-guid</GUID><Properties />
  </Node>
</SpeedTree>
"""
        one_bone_xml = """\
<SpeedTreeRaw><Bones>
  <Bone ID="0" ParentID="-1" StartX="0" StartY="0" StartZ="0"
        EndX="30.48" EndY="0" EndZ="0" Generator="Trunk" />
</Bones></SpeedTreeRaw>
"""
        cfg = {
            "target_bones_per_branch": 1.0,
            "max_total_bones": 2000,
            "total_window_low": 0.6,
            "total_window_high": 1.5,
            "seed_relative_value": 0.5,
            "value_cap": 64.0,
            "value_floor": 0.02,
            "max_calibration_rounds": 4,
            "fast_skip_problem_spm": True,
            "rename_materials": False,
            "backup_spm": True,
        }

        with tempfile.TemporaryDirectory() as tmp:
            spm_path = Path(tmp) / "SK_already_correct.spm"
            spm_audit.write_spm(spm_path, source_xml)
            original_bytes = spm_path.read_bytes()
            original_mtime_ns = spm_path.stat().st_mtime_ns
            exports = iter((one_bone_xml, one_bone_xml))

            def fake_export(_spm_path, _cfg, out_path):
                Path(out_path).write_text(next(exports), encoding="utf-8")
                return out_path

            with mock.patch.object(spm_audit, "export_verify_xml", side_effect=fake_export):
                with mock.patch.object(spm_audit, "export_verify_fbx_geometry", return_value=True):
                    report = spm_audit.process_spm(
                        spm_path, cfg, log=lambda _message: None
                    )

            self.assertEqual(report["status"], "already-ok")
            self.assertTrue(report["source_restored_unchanged"])
            self.assertEqual(spm_path.read_bytes(), original_bytes)
            self.assertEqual(spm_path.stat().st_mtime_ns, original_mtime_ns)

    def test_texture_only_edit_uses_bone_receipt_but_bone_edit_reexports(self):
        source_xml = """\
<SpeedTree>
  <Generator Type="Tree">
    <Name>Tree</Name><GUID>tree-generator-guid</GUID><Properties />
  </Generator>
  <Generator Type="Branch">
    <Name>Trunk</Name><GUID>trunk-guid</GUID>
    <Properties>
      <Property><Name>Physics:Bone style</Name><Value>1</Value></Property>
      <Property><Name>Physics:Bones</Name><Value>1</Value></Property>
    </Properties>
  </Generator>
  <Node Type="Tree">
    <GeneratorGUID>tree-generator-guid</GeneratorGUID>
    <ParentGUID></ParentGUID><GUID>tree-guid</GUID><Properties />
  </Node>
  <Node Type="Branch">
    <GeneratorGUID>trunk-guid</GeneratorGUID>
    <ParentGUID>tree-guid</ParentGUID><GUID>branch-node-guid</GUID><Properties />
  </Node>
</SpeedTree>
"""
        one_bone_xml = """\
<SpeedTreeRaw><Bones>
  <Bone ID="0" ParentID="-1" StartX="0" StartY="0" StartZ="0"
        EndX="30.48" EndY="0" EndZ="0" Generator="Trunk" />
</Bones></SpeedTreeRaw>
"""
        cfg = {
            "target_bones_per_branch": 1.0,
            "max_total_bones": 2000,
            "total_window_low": 0.6,
            "total_window_high": 1.5,
            "seed_relative_value": 0.5,
            "value_cap": 64.0,
            "value_floor": 0.02,
            "max_calibration_rounds": 4,
            "cluster_root_only_bones": True,
            "rename_materials": False,
            "tree_leaf_parent_red_gradient": False,
            "backup_spm": True,
        }

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            spm_path = root / "SK_receipt_fast_path.spm"
            cfg["spm_calibration_receipt_dir"] = str(root / "central-cache")
            spm_audit.write_spm(spm_path, source_xml)
            exports = iter((one_bone_xml, one_bone_xml))

            def fake_export(_spm_path, _cfg, out_path):
                Path(out_path).write_text(next(exports), encoding="utf-8")
                return out_path

            with mock.patch.object(
                spm_audit,
                "export_verify_xml",
                side_effect=fake_export,
            ), mock.patch.object(
                spm_audit,
                "export_verify_fbx_geometry",
                return_value=True,
            ):
                first = spm_audit.process_spm(
                    spm_path,
                    cfg,
                    log=lambda _message: None,
                )
            self.assertEqual(first["status"], "already-ok")
            self.assertTrue(Path(first["bone_receipt"]).is_file())

            texture_only = spm_audit.read_spm(spm_path).replace(
                "</SpeedTree>",
                (
                    '<Assets><Material_v8 ID="1" Name="Raw">'
                    '<Map Name="Color"><TexFilename>'
                    r"D:\canonical\T_leaf_color.tga"
                    "</TexFilename></Map></Material_v8></Assets>"
                    "</SpeedTree>"
                ),
            )
            spm_audit.write_spm(spm_path, texture_only)
            cfg["rename_materials"] = True
            with mock.patch.object(
                spm_audit,
                "export_verify_xml",
                side_effect=AssertionError("texture edit re-exported XML"),
            ), mock.patch.object(
                spm_audit,
                "export_verify_fbx_geometry",
                side_effect=AssertionError("texture edit re-exported FBX"),
            ):
                second = spm_audit.process_spm(
                    spm_path,
                    cfg,
                    log=lambda _message: None,
                )
            self.assertEqual(second["bone_fast_path"]["status"], "hit")
            self.assertEqual(second["status"], "calibrated")
            self.assertIn('Name="M_Raw"', spm_audit.read_spm(spm_path))

            changed_bone = re.sub(
                (
                    r"(<Name>Physics:Bones</Name>\s*"
                    r"<Value>)[^<]+(</Value>)"
                ),
                r"\g<1>2\g<2>",
                spm_audit.read_spm(spm_path),
                count=1,
            )
            spm_audit.write_spm(spm_path, changed_bone)
            with mock.patch.object(
                spm_audit,
                "export_verify_xml",
                side_effect=RuntimeError("semantic bone miss reached export"),
            ) as export_xml:
                with self.assertRaisesRegex(
                    RuntimeError,
                    "semantic bone miss reached export",
                ):
                    spm_audit.process_spm(
                        spm_path,
                        cfg,
                        log=lambda _message: None,
                    )
            export_xml.assert_called()


class ClusterOwnerClassificationTests(unittest.TestCase):
    def test_exact_cluster_owner_supplies_generic_part_kind(self):
        cases = (
            (Path("Tree_elm/Cluster/SK_branch_elm_01.spm"), "tree"),
            (Path("bush_Silky_Dogwood/Cluster/SK_cluster_01.spm"), "bush"),
            (Path("weed_ladyfern/Cluster/SK_cluster_01.spm"), "weed"),
        )
        for path, expected in cases:
            with self.subTest(path=path):
                self.assertEqual(spm_audit.classify_asset_kind(path), expected)

    def test_filename_kind_wins_and_arbitrary_ancestors_are_not_used(self):
        self.assertEqual(
            spm_audit.classify_asset_kind(
                Path("bush_owner/Cluster/SK_tree_explicit_01.spm")
            ),
            "tree",
        )
        self.assertEqual(
            spm_audit.classify_asset_kind(
                Path("Tree/shared/not_cluster/SK_branch_generic_01.spm")
            ),
            "other",
        )


if __name__ == "__main__":
    unittest.main()
