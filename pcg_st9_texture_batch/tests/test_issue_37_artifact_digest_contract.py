"""Hold the producer and the consumer of artifact identity to one contract.

``sk_batch_gui.App._cluster_receipt_live_artifacts_match()`` rejects any record
that declares neither ``sha256`` nor ``fingerprint``, and that rejection is
deliberate -- ``test_cluster_live_audit_memo_rejects_size_mtime_only_proof``
pins size+mtime alone as insufficient on purpose.  Three producers in
``pcg_cluster_assembly_contract`` used to opt out of hashing
(``file_fingerprint(..., hash_content=False)``) for the FBX/XML/STMAT paths,
material texture refs, and ``texture_dependencies``, so every healthy live
audit was discarded as churning inputs (#37).

Observed before the fix on ``bush_blackgum``: 27 of 221 artifact records had no
digest -- exactly the 50.3 MB and 67.1 MB leaf/atlas TGAs -- while the audit
itself reported ``status=ok`` and ``RECEIPT_UNCHANGED``.

These tests exist so the two sides cannot drift apart again.  A producer that
stops hashing, or a consumer that starts accepting size+mtime, breaks one of
them.
"""

import sys
import tempfile
import unittest
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parents[2]
for candidate in (REPO_DIR, REPO_DIR / "pcg_st9_texture_batch", REPO_DIR / "sk_batch"):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

import pcg_cluster_assembly_contract as contract  # noqa: E402


class MaterialRowDigestContractTests(unittest.TestCase):
    """Producer side: material texture refs carry a content digest."""

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

    def test_material_texture_refs_carry_a_content_digest(self):
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
        import hashlib

        self.assertTrue(record["exists"])
        self.assertEqual(record["size"], 4096)
        self.assertIsNotNone(record["mtime_ns"])
        self.assertEqual(
            record["sha256"],
            hashlib.sha256(b"x" * 4096).hexdigest(),
            "material texture refs must carry a content digest; the stability "
            "check rejects a record without one (#37)",
        )


class ArtifactStabilityContractTests(unittest.TestCase):
    """Consumer side: producer records are accepted; digest-less ones are not."""

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

    def test_producer_record_is_accepted_by_the_stability_check(self):
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

        self.assertTrue(
            matches,
            f"producer output must satisfy the stability check, got {errors!r}",
        )
        self.assertEqual(list(errors), [])

    def test_a_digest_less_record_is_still_rejected(self):
        """The guarantee the fix must not weaken."""
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
                }],
            })

        self.assertFalse(matches)
        self.assertTrue(any(
            "content digest missing" in error for error in errors
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
