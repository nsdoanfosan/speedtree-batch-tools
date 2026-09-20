import copy
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

SK_BATCH_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SK_BATCH_DIR))

from wind_structure_freshness import require_current_structure_contract


class StructureFreshnessTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "wind.json"
        self.expected = dict(schema_version=1, recipe_id="generic", recipe_sha256="a" * 64)
        hashes = {"spm": "b" * 64}
        digest = hashlib.sha256(json.dumps(hashes, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        self.contract = {
            "SchemaVersion": 1, "RecipeId": "generic", "RecipeSha256": "a" * 64,
            "Mode": "BoundedArtApproximation", "ValidationOnly": True,
            "BendRateScale": 1., "TorsionGain": 1., "FlutterGain": 1.,
            "SourceHashes": hashes, "SourceDigest": digest,
            "BoneNameIndexParentSha1": "c" * 40,
            "Bones": [{"BoneName": "Root", "BoneIndex": 0, "ParentIndex": -1,
                       "BendGain": 1., "BindPositionCm": [0., 0., 0.]}],
        }
        self.document = {"SkeletonContract": {
            "BoneNameIndexParentSha1": "c" * 40, "Bones": copy.deepcopy(self.contract["Bones"])},
            "WindStructureModifierContract": self.contract}

    def check(self):
        self.path.write_text(json.dumps(self.document), encoding="utf-8")
        return require_current_structure_contract(self.path, **self.expected)

    def test_current_contract_passes_without_editing(self):
        receipt = self.check()
        self.assertEqual(receipt["status"], "current")
        self.assertEqual(json.loads(self.path.read_text()), self.document)

    def test_old_cached_json_requires_repair(self):
        del self.document["WindStructureModifierContract"]
        with self.assertRaisesRegex(RuntimeError, "Run Repair/export"):
            self.check()

    def test_recipe_or_schema_change_requires_repair(self):
        for key, value in (("SchemaVersion", 0), ("SchemaVersion", True),
                           ("RecipeId", "old"), ("RecipeSha256", "d" * 64)):
            with self.subTest(key=key, value=value):
                original = self.contract[key]
                self.contract[key] = value
                with self.assertRaises(RuntimeError): self.check()
                self.contract[key] = original

    def test_validation_metadata_required(self):
        del self.contract["ValidationOnly"]
        with self.assertRaises(RuntimeError): self.check()

    def test_unvalidated_channels_rejected(self):
        for value in (1.1, True, None):
            self.contract["BendRateScale"] = value
            with self.assertRaises(RuntimeError): self.check()

    def test_source_digest_cannot_be_stale(self):
        self.contract["SourceHashes"]["spm"] = "d" * 64
        with self.assertRaisesRegex(RuntimeError, "source digest"): self.check()

    def test_partial_contract_rejected(self):
        self.contract["Bones"] = []
        with self.assertRaises(RuntimeError): self.check()

    def test_malformed_json_rejected_with_repair_guidance(self):
        self.path.write_text("{broken", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "Run Repair/export"):
            require_current_structure_contract(self.path, **self.expected)


if __name__ == "__main__": unittest.main()
