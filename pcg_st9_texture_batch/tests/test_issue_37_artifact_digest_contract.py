"""Pin the producer/consumer disagreement that blocks every live Cluster audit.

Two rules in this repository contradict each other, and #37 is the result:

* ``pcg_cluster_assembly_contract._material_rows()`` records material texture
  refs with ``file_fingerprint(..., hash_content=False)``, so those records
  carry ``sha256: None``.
* ``sk_batch_gui.App._cluster_receipt_live_artifacts_match()`` rejects any
  record that declares neither ``sha256`` nor ``fingerprint`` with
  ``content digest missing``, which makes the surrounding stability check treat
  a healthy audit as churning inputs.

Observed together on ``bush_blackgum``: 27 of 221 artifact records had no
digest -- exactly the 50.3 MB and 67.1 MB leaf/atlas TGAs -- while the audit
itself reported ``status=ok`` and ``RECEIPT_UNCHANGED``.

These are characterization tests. They assert today's behaviour on both sides
so the contradiction is visible in the suite rather than only in a job log.
**Fixing #37 must change this file**: whichever side is changed, the assertion
for that side is expected to be updated in the same commit, and the two must
end up agreeing.
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
    """Producer side: material texture refs are recorded without a digest."""

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

    def test_material_texture_refs_carry_no_content_digest(self):
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
        # Size and mtime are recorded; the content digest deliberately is not.
        self.assertTrue(record["exists"])
        self.assertEqual(record["size"], 4096)
        self.assertIsNotNone(record["mtime_ns"])
        self.assertIsNone(
            record["sha256"],
            "producer stopped omitting the digest -- update the consumer "
            "assertion below and close #37",
        )
        self.assertNotIn("fingerprint", record)


class ArtifactStabilityContractTests(unittest.TestCase):
    """Consumer side: a record with no digest is judged unstable."""

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

    def test_producer_record_is_rejected_by_the_stability_check(self):
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

        self.assertFalse(
            matches,
            "the two sides now agree -- #37 is fixed and this file should be "
            "rewritten to assert the agreed contract",
        )
        self.assertTrue(
            any("content digest missing" in error for error in errors),
            f"expected a digest-missing rejection, got {errors!r}",
        )

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
