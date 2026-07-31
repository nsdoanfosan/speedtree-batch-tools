"""Regression coverage for the shared live-artifact content-key contract.

Material textures intentionally omit a full SHA-256 because the blackgum audit
contains multi-hundred-MB binaries.  They must still carry the bounded sampled
fingerprint defined in ``artifact_content_key``.  The GUI interprets that same
algorithm identifier, while continuing to reject a size+mtime-only record.
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_DIR = Path(__file__).resolve().parents[2]
for candidate in (REPO_DIR, REPO_DIR / "pcg_st9_texture_batch", REPO_DIR / "sk_batch"):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

import pcg_cluster_assembly_contract as contract  # noqa: E402
import artifact_content_key as content_key  # noqa: E402


class MaterialRowDigestContractTests(unittest.TestCase):
    """Producer side: large texture refs use the bounded content key."""

    class _Audit:
        def __init__(self, refs):
            self._refs = refs

        def extract_material_image_refs(self, _spm):
            return [{
                "material_id": "4",
                "material_name": "M_Leaf_test_01",
                "cutout_mesh_ids": ["130"],
                "refs": list(self._refs),
            }]

    def test_material_texture_refs_carry_bounded_key_without_full_digest(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            spm = root / "SK_test_01.spm"
            spm.write_bytes(b"<SpeedTree/>")
            texture_dir = root / "texture"
            texture_dir.mkdir()
            texture = texture_dir / "T_Leaf_test_01_color.tga"
            texture.write_bytes(b"x" * 4096)

            rows = contract._material_rows(
                self._Audit([r"texture\T_Leaf_test_01_color.tga"]), spm
            )

        self.assertEqual(len(rows), 1)
        refs = rows[0]["textures"]
        self.assertEqual(len(refs), 1)
        record = refs[0]
        # Full SHA-256 stays absent, but a shared, bounded content key is not.
        self.assertTrue(record["exists"])
        self.assertEqual(record["size"], 4096)
        self.assertIsNotNone(record["mtime_ns"])
        self.assertIsNone(record["sha256"])
        self.assertRegex(record["fingerprint"], r"^[0-9a-f]{32}$")
        self.assertEqual(
            record["fingerprint_algorithm"],
            content_key.SAMPLED_FINGERPRINT_ALGORITHM,
        )

    def test_bounded_key_never_reads_the_whole_large_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            texture = Path(temporary) / "large-texture.tga"
            with texture.open("wb") as handle:
                handle.seek(64 * 1024 * 1024 - 1)
                handle.write(b"x")

            original_open = Path.open
            bytes_read = {"count": 0}

            class TrackingReader:
                def __init__(self, handle):
                    self.handle = handle

                def __enter__(self):
                    self.handle.__enter__()
                    return self

                def __exit__(self, *args):
                    return self.handle.__exit__(*args)

                def read(self, size=-1):
                    payload = self.handle.read(size)
                    bytes_read["count"] += len(payload)
                    return payload

                def seek(self, *args):
                    return self.handle.seek(*args)

            def tracking_open(path, *args, **kwargs):
                return TrackingReader(original_open(path, *args, **kwargs))

            with mock.patch.object(
                content_key.Path,
                "open",
                new=tracking_open,
            ):
                snapshot = content_key.sampled_file_content_snapshot(texture)

        self.assertEqual(
            bytes_read["count"],
            content_key.SAMPLED_MAX_READ_BYTES,
        )
        self.assertEqual(
            snapshot["fingerprint_algorithm"],
            content_key.SAMPLED_FINGERPRINT_ALGORITHM,
        )

    def test_bounded_key_rejects_a_file_changing_while_sampled(self):
        with tempfile.TemporaryDirectory() as temporary:
            texture = Path(temporary) / "mid-write.tga"
            texture.write_bytes(b"x" * 4096)
            original_digest = content_key._digest_open_file

            def digest_then_touch(handle, algorithm, size):
                result = original_digest(handle, algorithm, size)
                stat = texture.stat()
                os.utime(
                    texture,
                    ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000),
                )
                return result

            with mock.patch.object(
                content_key,
                "_digest_open_file",
                side_effect=digest_then_touch,
            ):
                with self.assertRaises(
                    content_key.ArtifactContentKeyChangedError
                ):
                    content_key.sampled_file_content_snapshot(texture)


class ArtifactStabilityContractTests(unittest.TestCase):
    """Consumer side: producer and checker use the same key definition."""

    @staticmethod
    def _load_gui():
        import importlib.machinery
        import importlib.util

        path = REPO_DIR / "sk_batch" / "sk_batch_gui.pyw"
        loader = importlib.machinery.SourceFileLoader("_issue37_gui", str(path))
        spec = importlib.util.spec_from_loader("_issue37_gui", loader)
        module = importlib.util.module_from_spec(spec)
        sys.modules["_issue37_gui"] = module
        loader.exec_module(module)
        return module

    def test_digestless_producer_record_is_accepted_by_stability_check(self):
        gui = self._load_gui()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            spm = root / "SK_test_01.spm"
            spm.write_bytes(b"<SpeedTree/>")
            texture_dir = root / "texture"
            texture_dir.mkdir()
            texture = texture_dir / "T_Leaf_test_01_color.tga"
            texture.write_bytes(b"x" * 4096)

            rows = contract._material_rows(
                MaterialRowDigestContractTests._Audit(
                    [r"texture\T_Leaf_test_01_color.tga"]
                ),
                spm,
            )
            # The exact shape the audit payload carries for material textures.
            matches, errors = gui.App._cluster_receipt_live_artifacts_match({
                "source_materials": rows,
            })

        self.assertTrue(matches, errors)
        self.assertEqual(list(errors), [])

    def test_size_mtime_only_record_remains_fail_closed(self):
        gui = self._load_gui()
        with tempfile.TemporaryDirectory() as temporary:
            texture = Path(temporary) / "T_Leaf_test_01_color.tga"
            texture.write_bytes(b"x" * 4096)
            stat = texture.stat()
            matches, errors = gui.App._cluster_receipt_live_artifacts_match({
                "texture_dependencies": [{
                    "path": str(texture),
                    "exists": True,
                    "size": stat.st_size,
                    "mtime_ns": stat.st_mtime_ns,
                    "sha256": None,
                }],
            })

        self.assertFalse(matches)
        self.assertTrue(any(
            "content digest missing" in error for error in errors
        ))

    def test_same_size_mtime_content_change_is_rejected(self):
        gui = self._load_gui()
        with tempfile.TemporaryDirectory() as temporary:
            texture = Path(temporary) / "T_Leaf_test_01_color.tga"
            texture.write_bytes(
                b"x" * (content_key.SAMPLED_MAX_READ_BYTES + 1)
            )
            record = contract.file_fingerprint(texture, hash_content=False)
            original_mtime = record["mtime_ns"]
            with texture.open("r+b") as handle:
                handle.write(b"y")
            os.utime(texture, ns=(original_mtime, original_mtime))

            matches, errors = gui.App._cluster_receipt_live_artifacts_match({
                "texture_dependencies": [record],
            })

        self.assertFalse(matches)
        self.assertTrue(any(
            "fingerprint changed" in error for error in errors
        ))

    def test_unknown_fingerprint_algorithm_remains_fail_closed(self):
        gui = self._load_gui()
        with tempfile.TemporaryDirectory() as temporary:
            texture = Path(temporary) / "T_Leaf_test_01_color.tga"
            texture.write_bytes(b"x" * 4096)
            record = contract.file_fingerprint(texture, hash_content=False)
            record["fingerprint_algorithm"] = "unknown-sampler-v99"

            matches, errors = gui.App._cluster_receipt_live_artifacts_match({
                "texture_dependencies": [record],
            })

        self.assertFalse(matches)
        self.assertTrue(any(
            "Unsupported artifact fingerprint algorithm" in error
            for error in errors
        ))

    def test_a_digest_bearing_record_is_accepted(self):
        """The check is not blanket-strict: a real digest passes."""
        gui = self._load_gui()
        with tempfile.TemporaryDirectory() as temporary:
            texture = Path(temporary) / "T_Leaf_test_01_color.tga"
            texture.write_bytes(b"x" * 4096)
            stat = texture.stat()
            import hashlib

            digest = hashlib.sha256(texture.read_bytes()).hexdigest()
            matches, errors = gui.App._cluster_receipt_live_artifacts_match({
                "texture_dependencies": [{
                    "path": str(texture),
                    "exists": True,
                    "size": stat.st_size,
                    "mtime_ns": stat.st_mtime_ns,
                    "sha256": digest,
                }],
            })

        self.assertTrue(matches, errors)
        self.assertEqual(list(errors), [])


if __name__ == "__main__":
    unittest.main()
