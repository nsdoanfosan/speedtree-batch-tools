import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pcg_st9_texture_batch import pcg_texture_common
from sk_batch import sk_common


BLENDER_52_EXE = (
    r"C:\Program Files\Blender Foundation\Blender 5.2\blender.exe"
)


class Blender52RuntimeConfigTests(unittest.TestCase):
    def _assert_legacy_config_is_normalized(self, module):
        with tempfile.TemporaryDirectory() as temporary:
            config_path = Path(temporary) / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "blender_exe": (
                            r"C:\Program Files\Blender Foundation"
                            r"\Blender 5.1\blender.exe"
                        )
                    }
                ),
                encoding="utf-8",
            )
            with mock.patch.object(module, "CONFIG_PATH", config_path):
                loaded = module.load_config()
                self.assertEqual(loaded["blender_exe"], BLENDER_52_EXE)
                module.save_config(loaded | {"blender_exe": "legacy.exe"})
            saved = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["blender_exe"], BLENDER_52_EXE)

    def test_sk_batch_config_is_blender_52_only(self):
        self._assert_legacy_config_is_normalized(sk_common)

    def test_pcg_texture_config_is_blender_52_only(self):
        self._assert_legacy_config_is_normalized(pcg_texture_common)


if __name__ == "__main__":
    unittest.main()
