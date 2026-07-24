import sys
import tempfile
import unittest
from pathlib import Path


REPO_DIR = Path(__file__).resolve().parents[1]
PCG_DIR = REPO_DIR / "pcg_st9_texture_batch"
SK_DIR = REPO_DIR / "sk_batch"
for path in (REPO_DIR, PCG_DIR, SK_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from pcg_cluster_assembly_contract import (  # noqa: E402
    ClusterAssemblyReceiptStaleError,
    file_fingerprint,
    load_cluster_assembly_receipt,
    persist_cluster_assembly_receipt,
)
from cluster_assembly_handoff_contract import (  # noqa: E402
    _dependency_artifact_validation,
)


class ClusterAssemblyPairReceiptTests(unittest.TestCase):
    def _fixture(self, root):
        folder = root / "Tree_elm"
        cluster = folder / "Cluster"
        canonical = cluster / "SK_branch_elm_01.spm"
        output = cluster / "branch_elm_01.spm"
        target = folder / "SK_Tree_elm_01.spm"
        source = folder / "Tree_elm_01.spm"
        for path, payload in (
            (canonical, b"cluster-pair"),
            (output, b"cluster-pair"),
            (target, b"full-target"),
            (source, b"tree-output"),
        ):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(payload)
        dependency = {
            "role": "branch",
            "name": "branch_elm_01",
            "spm": str(canonical),
            "authoring_spm": str(canonical),
            "output_spm": str(output),
            "spm_fingerprint": file_fingerprint(canonical),
            "authoring_spm_fingerprint": file_fingerprint(canonical),
            "output_spm_fingerprint": file_fingerprint(output),
            "texture_dependencies": [],
        }
        contract = {
            "folder": str(folder),
            "tree_source_identities": [{
                "target_spm": file_fingerprint(target),
                "authoritative_tree_source": file_fingerprint(source),
            }],
            "dependencies": [dependency],
            "handoff": {"cluster_dependencies": [dict(dependency)]},
        }
        return contract, target, canonical, output

    def test_persisted_receipt_rejects_changed_canonical_authoring_spm(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            contract, target, canonical, _output = self._fixture(root)
            receipt = persist_cluster_assembly_receipt(
                contract, root / "receipts"
            )
            load_cluster_assembly_receipt(receipt, requested_spm=target)
            canonical.write_bytes(b"edited-authoring")
            with self.assertRaises(ClusterAssemblyReceiptStaleError):
                load_cluster_assembly_receipt(receipt, requested_spm=target)

    def test_persisted_receipt_rejects_changed_speedtree_output_mirror(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            contract, target, _canonical, output = self._fixture(root)
            receipt = persist_cluster_assembly_receipt(
                contract, root / "receipts"
            )
            output.write_bytes(b"independent-output-edit")
            with self.assertRaises(ClusterAssemblyReceiptStaleError):
                load_cluster_assembly_receipt(receipt, requested_spm=target)

    def test_bwr_dependency_gate_checks_both_pair_members(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            contract, target, _canonical, output = self._fixture(root)
            output.write_bytes(b"independent-output-edit")
            validations = _dependency_artifact_validation(contract, target)
            by_artifact = {row["artifact"]: row for row in validations}
            self.assertTrue(by_artifact["cluster_authoring_spm"]["ok"])
            self.assertFalse(by_artifact["cluster_output_spm"]["ok"])


if __name__ == "__main__":
    unittest.main()
