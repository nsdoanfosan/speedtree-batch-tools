"""Issue #21 regression coverage for the shared persistent analysis cache."""
import gzip
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock


TOOL_DIR = Path(__file__).resolve().parents[1]
REPO_DIR = TOOL_DIR.parent
for candidate in (TOOL_DIR, REPO_DIR):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from pcg_texture_common import (  # noqa: E402
    SHARED_CACHE_DIR_ENV,
    default_shared_cache_dir,
)


def write_spm(path, padding_size=0):
    payload = (
        '<?xml version="1.0"?><SpeedTree><Materials>'
        '<Material_v8 ID="1" Name="M_leaf">'
        '<TexFilename>leaf_color.tga</TexFilename></Material_v8>'
        '</Materials><Generators><Generator Type="Leaf Mesh">'
        '<Name>leaf</Name><GUID>leaf-guid</GUID><Hidden>false</Hidden>'
        '<Properties><Property><Name>Leaves:Type:0:Material</Name>'
        '<Value>1</Value></Property><Property>'
        '<Name>Leaves:Type:0:Mesh</Name><Value>7</Value></Property>'
        '</Properties></Generator></Generators><Meshes>'
        '<Mesh ID="7" /></Meshes><Padding>'
        + ("x" * padding_size)
        + '</Padding></SpeedTree>'
    ).encode("utf-8")
    path.write_bytes(gzip.compress(payload, mtime=0))


class SharedAnalysisCacheTests(unittest.TestCase):
    def test_default_path_is_per_user_and_does_not_create_it(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "platform-cache-root"
            with mock.patch.dict(
                os.environ,
                {
                    "LOCALAPPDATA": str(root),
                    "XDG_CACHE_HOME": str(root),
                },
                clear=True,
            ):
                path = default_shared_cache_dir()

            self.assertEqual(path, root / "SpeedTreeBatchTools" / "cache")
            self.assertFalse(root.exists())

    def test_documented_override_is_absolute_and_checkout_independent(self):
        with tempfile.TemporaryDirectory() as temporary:
            override = Path(temporary) / "shared-cache"
            with mock.patch.dict(
                os.environ,
                {SHARED_CACHE_DIR_ENV: str(override)},
            ):
                first = default_shared_cache_dir()
            with mock.patch.dict(
                os.environ,
                {SHARED_CACHE_DIR_ENV: str(override)},
            ):
                second = default_shared_cache_dir()

            self.assertEqual(first, override.resolve())
            self.assertEqual(second, first)
            self.assertNotIn(str(TOOL_DIR), str(first))

    def test_new_process_reuses_disk_cache_without_decoding(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            spm = root / "SK_tree_shared_cache.spm"
            write_spm(spm, padding_size=8 * 1024 * 1024)
            cache_dir = root / "per-user-cache"
            checkout_a = root / "checkout-a"
            checkout_b = root / "checkout-b"
            checkout_a.mkdir()
            checkout_b.mkdir()

            env = os.environ.copy()
            env[SHARED_CACHE_DIR_ENV] = str(cache_dir)
            env["PYTHONDONTWRITEBYTECODE"] = "1"
            env["PYTHONPATH"] = os.pathsep.join(
                [str(TOOL_DIR), str(REPO_DIR), env.get("PYTHONPATH", "")]
            )
            cold_code = textwrap.dedent("""
                import json
                import sys
                import time
                from pathlib import Path
                import pcg_texture_audit as audit

                spm = Path(sys.argv[1])
                started = time.perf_counter()
                bindings = audit.leaf_generator_bindings(spm)
                elapsed = time.perf_counter() - started
                audit.save_spm_analysis_cache()
                print(json.dumps({
                    "bindings": len(bindings),
                    "elapsed": elapsed,
                    "cache_path": str(audit.SPM_ANALYSIS_CACHE_PATH),
                }))
            """)
            warm_code = textwrap.dedent("""
                import json
                import sys
                import time
                from pathlib import Path
                import pcg_texture_audit as audit

                def fail_decode(_path):
                    raise AssertionError("warm process decoded the SPM")

                audit.read_maybe_gzip_text = fail_decode
                started = time.perf_counter()
                bindings = audit.leaf_generator_bindings(Path(sys.argv[1]))
                elapsed = time.perf_counter() - started
                print(json.dumps({
                    "bindings": len(bindings),
                    "elapsed": elapsed,
                    "cache_path": str(audit.SPM_ANALYSIS_CACHE_PATH),
                }))
            """)

            cold = subprocess.run(
                [sys.executable, "-c", cold_code, str(spm)],
                cwd=checkout_a,
                env=env,
                check=True,
                capture_output=True,
                text=True,
            )
            warm = subprocess.run(
                [sys.executable, "-c", warm_code, str(spm)],
                cwd=checkout_b,
                env=env,
                check=True,
                capture_output=True,
                text=True,
            )
            cold_result = json.loads(cold.stdout)
            warm_result = json.loads(warm.stdout)

            cache_path = cache_dir / "spm_analysis_v5.json"
            self.assertEqual(Path(cold_result["cache_path"]), cache_path)
            self.assertEqual(warm_result["cache_path"], cold_result["cache_path"])
            self.assertTrue(cache_path.is_file())
            self.assertEqual(warm_result["bindings"], cold_result["bindings"])
            self.assertLess(warm_result["elapsed"], cold_result["elapsed"])

            payload = json.loads(cache_path.read_text(encoding="utf-8"))
            entry = next(iter(payload["entries"].values()))
            stat = spm.stat()
            self.assertEqual(entry["size"], stat.st_size)
            self.assertEqual(entry["mtime_ns"], stat.st_mtime_ns)
            self.assertEqual(entry["leaf_binding_schema"], 5)


if __name__ == "__main__":
    unittest.main()
