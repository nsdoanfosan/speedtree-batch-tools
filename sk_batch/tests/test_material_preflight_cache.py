import gzip
import hashlib
import os
import sys
import tempfile
import unittest
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path
from unittest import mock


SK_BATCH_DIR = Path(__file__).resolve().parents[1]
REPO_DIR = SK_BATCH_DIR.parent
sys.path.insert(0, str(REPO_DIR))
sys.path.insert(0, str(SK_BATCH_DIR))

from material_preflight_cache import (  # noqa: E402
    load_material_preflight_cache,
    material_preflight_runtime_signature,
    store_material_preflight_cache,
)
from speedtree_pipeline_contract import build_preflight_envelope  # noqa: E402


def write_spm(path, payload=b"<SpeedTreeModel><Assets /></SpeedTreeModel>"):
    path.write_bytes(gzip.compress(payload))


def load_gui_module():
    path = SK_BATCH_DIR / "sk_batch_gui.pyw"
    loader = SourceFileLoader("sk_batch_gui_preflight_cache_test", str(path))
    spec = spec_from_loader(loader.name, loader)
    module = module_from_spec(spec)
    loader.exec_module(module)
    return module


class MaterialPreflightCacheTests(unittest.TestCase):
    def fixture(self, root):
        spm = root / "SK_tree_cache_01.spm"
        write_spm(spm)
        stmat = root / "fbx" / "SK_tree_cache_01.stmat"
        stmat.parent.mkdir()
        stmat.write_text("<SpeedTreeMaterials />", encoding="utf-8")
        fbx = stmat.with_suffix(".fbx")
        fbx.write_bytes(b"fbx fixture")

        def artifact(path):
            stat = path.stat()
            return {
                "relative_path": path.name,
                "size": stat.st_size,
                "mtime_ns": stat.st_mtime_ns,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }

        report = {
            "status": "ok",
            "spm": str(spm.resolve()),
            "speedtree_spm": str(spm.resolve()),
            "speedtree_export": {
                "path": str(fbx.resolve()),
                "artifacts": [artifact(fbx), artifact(stmat)],
            },
            "speedtree_pipeline_contract": build_preflight_envelope(
                spm,
                outcome="ok",
                texture_readiness={"status": "not_applicable"},
            ),
            "cluster_assembly_receipt_persistence": {
                "status": "ok",
                "live_audit_complete": True,
                "unchanged": [str(root / "current-receipt.json")],
            },
        }
        return spm, stmat, report

    def test_successful_exact_report_is_reused_and_live_marker_is_demoted(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cache = root / "cache"
            spm, _stmat, report = self.fixture(root)
            store_material_preflight_cache(
                cache,
                spm,
                spm,
                report,
                runtime_signature="runtime-v1",
            )

            reused = load_material_preflight_cache(
                cache,
                spm,
                spm,
                runtime_signature="runtime-v1",
            )

            self.assertIsNotNone(reused)
            self.assertEqual(reused["report"]["status"], "ok")
            self.assertFalse(
                reused["report"]["cluster_assembly_receipt_persistence"]
                ["live_audit_complete"]
            )
            self.assertTrue(
                reused["report"]["cluster_assembly_receipt_persistence"]
                ["cache_reused"]
            )

    def test_touch_only_source_and_stmat_do_not_invalidate_content_receipt(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cache = root / "cache"
            spm, stmat, report = self.fixture(root)
            store_material_preflight_cache(
                cache,
                spm,
                spm,
                report,
                runtime_signature="runtime-v1",
            )
            for path in (spm, stmat):
                stat = path.stat()
                os.utime(
                    path,
                    ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000),
                )

            self.assertIsNotNone(
                load_material_preflight_cache(
                    cache,
                    spm,
                    spm,
                    runtime_signature="runtime-v1",
                )
            )

    def test_source_content_change_is_an_ordinary_cache_miss(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cache = root / "cache"
            spm, _stmat, report = self.fixture(root)
            store_material_preflight_cache(
                cache,
                spm,
                spm,
                report,
                runtime_signature="runtime-v1",
            )
            write_spm(
                spm,
                b"<SpeedTreeModel><Assets /><Changed /></SpeedTreeModel>",
            )

            self.assertIsNone(
                load_material_preflight_cache(
                    cache,
                    spm,
                    spm,
                    runtime_signature="runtime-v1",
                )
            )

    def test_export_fbx_content_change_is_an_ordinary_cache_miss(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cache = root / "cache"
            spm, stmat, report = self.fixture(root)
            store_material_preflight_cache(
                cache,
                spm,
                spm,
                report,
                runtime_signature="runtime-v1",
            )
            fbx = stmat.with_suffix(".fbx")
            fbx.write_bytes(b"changed fbx content")

            self.assertIsNone(
                load_material_preflight_cache(
                    cache,
                    spm,
                    spm,
                    runtime_signature="runtime-v1",
                )
            )

    def test_runtime_contract_change_is_an_ordinary_cache_miss(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cache = root / "cache"
            spm, _stmat, report = self.fixture(root)
            store_material_preflight_cache(
                cache,
                spm,
                spm,
                report,
                runtime_signature="runtime-v1",
            )

            self.assertIsNone(
                load_material_preflight_cache(
                    cache,
                    spm,
                    spm,
                    runtime_signature="runtime-v2",
                )
            )

    def test_identical_runtime_files_share_signature_across_checkout_paths(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = root / "first" / "Options_MA_Fbx.ini"
            second = root / "second" / "Options_MA_Fbx.ini"
            exe = root / "SpeedTree_Modeler.exe"
            first.parent.mkdir()
            second.parent.mkdir()
            first.write_bytes(b"[Export]\nTextureSkipWriting=true\n")
            second.write_bytes(first.read_bytes())
            exe.write_bytes(b"speedtree-binary-fixture")
            stat = second.stat()
            os.utime(
                second,
                ns=(stat.st_atime_ns, stat.st_mtime_ns + 2_000_000_000),
            )

            one = material_preflight_runtime_signature(
                semantic_files=[first],
                speedtree_exe=exe,
            )
            two = material_preflight_runtime_signature(
                semantic_files=[second],
                speedtree_exe=exe,
            )

            self.assertEqual(one, two)

    def test_gui_code_edit_does_not_invalidate_material_export_cache(self):
        gui = load_gui_module()
        app = gui.App.__new__(gui.App)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fbx_ini = root / "Options_MA_Fbx.ini"
            speedtree_cli = root / "speedtree_cli.py"
            speedtree_exe = root / "SpeedTree_Modeler.exe"
            fbx_ini.write_text("[Export]\nTextureSkipWriting=true\n", encoding="utf-8")
            speedtree_cli.write_text("# implementation v1\n", encoding="utf-8")
            speedtree_exe.write_bytes(b"speedtree-binary-fixture")
            app.cfg = {
                "speedtree_exe": str(speedtree_exe),
                "material_preflight_cache_dir": str(root / "cache"),
            }

            first = app._material_preflight_cache_context(
                fbx_ini,
                speedtree_cli,
            )["runtime_signature"]
            app.__dict__.pop(
                "_material_preflight_cache_context_value",
                None,
            )
            speedtree_cli.write_text(
                "# implementation v2\n",
                encoding="utf-8",
            )
            after_code_edit = app._material_preflight_cache_context(
                fbx_ini,
                speedtree_cli,
            )["runtime_signature"]

            app.__dict__.pop(
                "_material_preflight_cache_context_value",
                None,
            )
            fbx_ini.write_text(
                "[Export]\nTextureSkipWriting=false\n",
                encoding="utf-8",
            )
            after_export_setting_edit = (
                app._material_preflight_cache_context(
                    fbx_ini,
                    speedtree_cli,
                )["runtime_signature"]
            )

        self.assertEqual(first, after_code_edit)
        self.assertNotEqual(first, after_export_setting_edit)

    def test_gui_cache_hit_does_not_launch_material_preflight_child(self):
        gui = load_gui_module()
        app = gui.App.__new__(gui.App)
        app.force_rerun = False
        app.log = mock.Mock()
        app._run_limited = mock.Mock(
            side_effect=AssertionError("cache hit must not launch child")
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            addon = root / "addon"
            fbx_ini = addon / "presets" / "speedtree_10_1" / "Options.ini"
            fbx_ini.parent.mkdir(parents=True)
            fbx_ini.write_text("options", encoding="utf-8")
            (addon / "speedtree_cli.py").write_text("# cli", encoding="utf-8")
            spm = root / "SK_tree_cache_01.spm"
            spm.touch()
            app.cfg = {
                "fbx_ini": str(fbx_ini),
                "speedtree_exe": str(root / "SpeedTree.exe"),
            }
            app._material_preflight_cache_context = mock.Mock(return_value={
                "cache_dir": root / "cache",
                "runtime_signature": "runtime-v1",
            })
            cached_report = root / "cached.material.json"
            cached_receipt = root / "cached.receipt.json"
            cached = {
                "report": {"status": "ok"},
                "report_path": cached_report,
                "receipt_path": cached_receipt,
            }
            with mock.patch.object(
                gui,
                "load_material_preflight_cache",
                return_value=cached,
            ) as load:
                result = app._execute_material_preflight(
                    spm,
                    spm,
                    "20260807_030000",
                )

            self.assertTrue(result["cache_hit"])
            self.assertEqual(result["report"], cached_report)
            app._run_limited.assert_not_called()
            load.assert_called_once()

    def test_gui_adopts_existing_success_report_before_launching_child(self):
        gui = load_gui_module()
        app = gui.App.__new__(gui.App)
        app.log = mock.Mock()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            spm = root / "SK_tree_cache_01.spm"
            candidate = root / "SK_tree_cache_01_material_preflight_old.json"
            spm.touch()
            candidate.write_text('{"status":"ok"}', encoding="utf-8")
            app._material_preflight_seed_candidates = mock.Mock(
                return_value=[candidate]
            )
            cached = {
                "report": {"status": "ok"},
                "report_path": root / "cached.material.json",
                "receipt_path": root / "cached.receipt.json",
            }
            context = {
                "cache_dir": root / "cache",
                "runtime_signature": "runtime-v1",
            }
            with mock.patch.object(
                gui,
                "load_material_preflight_cache",
                side_effect=[None, cached],
            ) as load, mock.patch.object(
                gui,
                "load_job_report",
                return_value={"status": "ok"},
            ), mock.patch.object(
                gui,
                "store_material_preflight_cache",
            ) as store:
                result = app._load_or_seed_material_preflight_cache(
                    context,
                    spm,
                    spm,
                )

            self.assertEqual(result, cached)
            self.assertEqual(load.call_count, 2)
            store.assert_called_once()
            app.log.assert_called_once()

    def test_explicit_force_rebuild_bypasses_success_cache(self):
        gui = load_gui_module()
        app = gui.App.__new__(gui.App)
        app.force_rerun = True
        app.log = mock.Mock()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            addon = root / "addon"
            fbx_ini = addon / "presets" / "speedtree_10_1" / "Options.ini"
            fbx_ini.parent.mkdir(parents=True)
            fbx_ini.write_text("options", encoding="utf-8")
            (addon / "speedtree_cli.py").write_text("# cli", encoding="utf-8")
            spm = root / "SK_tree_cache_01.spm"
            spm.touch()
            app.cfg = {
                "fbx_ini": str(fbx_ini),
                "speedtree_exe": str(root / "SpeedTree.exe"),
                "speedtree_material_preflight_timeout": 900,
                "child_stage_inactivity_timeout": 180,
                "speedtree_material_preflight_queue_timeout": 3600,
            }
            app._material_preflight_cache_context = mock.Mock(return_value={
                "cache_dir": root / "cache",
                "runtime_signature": "runtime-v1",
            })
            app._run_limited = mock.Mock(return_value=(0, root / "run.log"))
            with mock.patch.object(
                gui,
                "load_material_preflight_cache",
                side_effect=AssertionError("force must bypass cache lookup"),
            ) as load, mock.patch.object(
                gui,
                "load_job_report",
                return_value={"status": "ok"},
            ), mock.patch.object(
                gui,
                "store_material_preflight_cache",
            ) as store:
                result = app._execute_material_preflight(
                    spm,
                    spm,
                    "20260807_030001",
                )

            self.assertFalse(result["cache_hit"])
            load.assert_not_called()
            app._run_limited.assert_called_once()
            store.assert_called_once()


if __name__ == "__main__":
    unittest.main()
