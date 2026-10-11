import sys
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import sk_common


class InstalledRoutingTests(unittest.TestCase):
    def test_installed_export_and_rpc_dependency_share_the_same_package(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            addon = root / "5.2/scripts/addons/send2ue"
            (addon / "dependencies").mkdir(parents=True)
            (addon / "dependencies/unreal.py").write_text("# native import provider", encoding="utf-8")
            self.assertEqual(sk_common.installed_send2ue_dir(scripts_root=root), addon.resolve())
            self.assertEqual(sk_common.installed_send2ue_dir(version="5.1", scripts_root=root), sk_common.LEGACY_SEND2UE_DIR)

    def test_old_default_migrates_but_explicit_custom_package_is_preserved(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "config.json"
            config.write_text('{"send2ue_dir":"C:/Custom/send2ue"}', encoding="utf-8")
            with patch.object(sk_common, "CONFIG_PATH", config):
                self.assertEqual(sk_common.load_config()["send2ue_dir"], "C:/Custom/send2ue")
                import json
                config.write_text(json.dumps({"send2ue_dir": str(sk_common.LEGACY_SEND2UE_DIR)}), encoding="utf-8")
                self.assertEqual(sk_common.load_config()["send2ue_dir"], str(sk_common.DEFAULT_SEND2UE_DIR))


if __name__ == "__main__":
    unittest.main()
