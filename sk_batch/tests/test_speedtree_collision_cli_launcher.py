import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[2]
LAUNCHER_SOURCE = REPO / "speedtree_collision_cli" / "launcher.cpp"


class SpeedTreeCollisionCliLauncherTests(unittest.TestCase):
    def test_gui_bake_window_is_visible_without_activation(self):
        source = LAUNCHER_SOURCE.read_text(encoding="utf-8")

        self.assertIn("startup.wShowWindow = SW_SHOWNOACTIVATE;", source)
        self.assertNotIn("startup.wShowWindow = SW_SHOWMINNOACTIVE;", source)
        self.assertIn("STARTF_USESHOWWINDOW", source)


if __name__ == "__main__":
    unittest.main()
