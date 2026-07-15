import importlib.machinery
import importlib.util
from pathlib import Path
import sys
import unittest


REPO_DIR = Path(__file__).resolve().parents[1]
LAUNCHER_PATH = REPO_DIR / "speedtree_batch_tools_gui.pyw"


def load_launcher_module():
    loader = importlib.machinery.SourceFileLoader(
        "_speedtree_batch_tools_test_launcher", str(LAUNCHER_PATH)
    )
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[loader.name] = module
    loader.exec_module(module)
    return module


class IntegratedLauncherTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.launcher = load_launcher_module()

    def test_all_tool_entry_points_exist(self):
        self.assertEqual(
            [tool.label for tool in self.launcher.TOOLS],
            ["SK Batch", "SPM Generator Sync", "PCG ST9 Texture"],
        )
        for tool in self.launcher.TOOLS:
            with self.subTest(tool=tool.label):
                self.assertTrue(tool.script.is_file())
                self.assertTrue(tool.launcher.is_file())

    def test_each_tool_module_exposes_app(self):
        for tool in self.launcher.TOOLS:
            with self.subTest(tool=tool.label):
                module = self.launcher.load_tool_module(tool)
                self.assertTrue(callable(module.App))

    def test_tab_adapter_absorbs_window_setters(self):
        adapter = self.launcher.ToolTab
        dummy = object()
        self.assertIsNone(adapter.title(dummy, "ignored"))
        self.assertEqual(adapter.geometry(dummy, "100x100"), "")
        self.assertIsNone(adapter.minsize(dummy, 800, 600))

    def test_batch_file_launches_integrated_gui(self):
        text = (REPO_DIR / "SpeedTree_Batch_Tools.bat").read_text(encoding="utf-8")
        self.assertIn("speedtree_batch_tools_gui.pyw", text)
        self.assertNotIn("call \"SK_Batch.bat\"", text)


if __name__ == "__main__":
    unittest.main()
