import importlib.machinery
import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image


TOOL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOL_DIR))

import sbs_auto


def load_gui_module():
    path = TOOL_DIR / "pcg_texture_gui.pyw"
    loader = importlib.machinery.SourceFileLoader(
        "pcg_texture_gui_step3_freshness_test", str(path)
    )
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def write_empty_sbs(path):
    path.write_text(
        """<?xml version="1.0"?>
<package>
  <dependencies>
    <dependency><filename v="?himself"/><uid v="1"/>
      <type v="package"/><fileUID v="0"/><versionUID v="0"/></dependency>
  </dependencies>
  <content>
    <group><identifier v="Resources"/><uid v="2"/><content/></group>
  </content>
</package>""",
        encoding="utf-8",
    )


class Step3FreshnessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.gui = load_gui_module()

    def _app_and_row(self, root, source, *, structural=False):
        texture_dir = root / "texture"
        texture_dir.mkdir()
        item = {"name": "tree_test", "folder": str(root)}
        row = {
            "folder": str(root),
            "atlas_base": "M_leaf_test",
            "texture_base": "T_leaf_test",
            "texture_dir": str(texture_dir),
        }
        app = self.gui.App.__new__(self.gui.App)
        app.items = {}
        app.texplan_errors = {}
        app._checked_texplan_rows = lambda: [(item, row)]
        app._all_texplan_rows = lambda: [(item, row)]
        job = {
            "base": row["atlas_base"],
            "texture_base": row["texture_base"],
            "mode": "render_only",
            "out_dir": str(texture_dir),
            "inputs": {"Base_Color": source},
        }
        if structural:
            job["normalize_cluster"] = True
        return app, item, row, job

    def _write_outputs(self, job, mtime_ns):
        paths = self.gui.output_paths(
            job["out_dir"], job["texture_base"]
        )
        for path in paths.values():
            path.write_bytes(b"output")
            os.utime(path, ns=(mtime_ns, mtime_ns))
        return paths

    def test_newer_external_input_requeues_complete_output_set(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source.png"
            source.write_bytes(b"new source")
            app, _item, _row, job = self._app_and_row(root, source)
            old_ns = source.stat().st_mtime_ns - 10_000_000
            self._write_outputs(job, old_ns)

            with mock.patch.object(
                self.gui, "build_texture_job", return_value=job
            ), mock.patch.object(
                self.gui, "expected_job_size", return_value=(1, 1)
            ), mock.patch.object(
                self.gui, "complete_output_set", return_value=True
            ), mock.patch.object(
                self.gui, "job_needs_source_repair", return_value=False
            ):
                jobs, skipped = app._step3_jobs()
                sync_files = app._step3_sync_files()

        self.assertEqual(skipped, [])
        self.assertEqual(len(jobs), 1)
        self.assertEqual(
            jobs[0]["existing_output_freshness"]["reason"],
            "current_source_newer_than_outputs",
        )
        self.assertEqual(sync_files, [])
        self.assertEqual(app._pending_step3_manifest_rows, [])

    def test_fresh_complete_outputs_can_be_manifested_without_render(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source.png"
            source.write_bytes(b"source")
            app, _item, _row, job = self._app_and_row(root, source)
            new_ns = source.stat().st_mtime_ns + 10_000_000
            self._write_outputs(job, new_ns)

            with mock.patch.object(
                self.gui, "build_texture_job", return_value=job
            ), mock.patch.object(
                self.gui, "expected_job_size", return_value=(1, 1)
            ), mock.patch.object(
                self.gui, "complete_output_set", return_value=True
            ), mock.patch.object(
                self.gui, "job_needs_source_repair", return_value=False
            ):
                jobs, skipped = app._step3_jobs()
                sync_files = app._step3_sync_files()

        self.assertEqual(jobs, [])
        self.assertEqual(skipped, [])
        self.assertEqual(len(sync_files), len(sbs_auto.RENDER_MAPS))
        self.assertEqual(len(app._pending_step3_manifest_rows), 1)

    def test_structural_graph_repair_requeues_even_fresh_outputs(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source.png"
            source.write_bytes(b"source")
            app, _item, _row, job = self._app_and_row(
                root, source, structural=True
            )
            new_ns = source.stat().st_mtime_ns + 10_000_000
            self._write_outputs(job, new_ns)

            with mock.patch.object(
                self.gui, "build_texture_job", return_value=job
            ), mock.patch.object(
                self.gui, "expected_job_size", return_value=(1, 1)
            ), mock.patch.object(
                self.gui, "complete_output_set", return_value=True
            ), mock.patch.object(
                self.gui, "job_needs_source_repair", return_value=False
            ):
                jobs, skipped = app._step3_jobs()

        self.assertEqual(skipped, [])
        self.assertEqual(len(jobs), 1)

    def test_graph_input_content_changes_cook_fingerprint(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            sbs = root / "test.sbs"
            write_empty_sbs(sbs)
            source = root / "Original_Leaf_Albedo.png"
            Image.new("RGBA", (4, 4), (20, 40, 60, 255)).save(source)
            sbs_auto.insert_m_graph(
                sbs,
                "T_leaf_test",
                {"Base_Color": source, "Opacity": source},
            )
            before = sbs_auto.graph_external_input_fingerprint(
                sbs, ["T_leaf_test"]
            )
            Image.new("RGBA", (4, 4), (80, 20, 10, 255)).save(source)
            os.utime(source, None)
            after = sbs_auto.graph_external_input_fingerprint(
                sbs, ["T_leaf_test"]
            )

        self.assertNotEqual(before, after)


if __name__ == "__main__":
    unittest.main()
