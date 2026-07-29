import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SK_BATCH_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SK_BATCH_DIR))

from spm_calibration_receipt import (
    SPM_BONE_SEMANTIC_PROJECTION_VERSION,
    bone_semantic_fingerprint,
    calibration_receipt_path,
    legacy_bone_semantic_fingerprint,
    load_positive_calibration_receipt,
    write_positive_calibration_receipt,
)


SOURCE = """\
<SpeedTree>
  <Assets>
    <Material_v8 ID="1" Name="Raw">
      <CutoutMeshID>4</CutoutMeshID><Width>16</Width><Height>16</Height>
      <Map Name="Color"><TexFilename>source_a.tga</TexFilename></Map>
    </Material_v8>
  </Assets>
  <Thumbnail>old-thumbnail</Thumbnail>
  <Generator Type="Branch">
    <Name>Authored Name</Name><GUID>branch-guid</GUID><Hidden>false</Hidden>
    <Properties>
      <Property><Name>Physics:Bone style</Name><Value>0</Value></Property>
      <Property><Name>Physics:Bones</Name><Value>1</Value></Property>
      <Property><Name>Vertex Color:Red:Value</Name><Value>0</Value></Property>
      <Property><Name>Materials:Branch:0:Material</Name><Value>12</Value></Property>
      <Property><Name>Skin:Type</Name><Value>0</Value></Property>
    </Properties>
  </Generator>
  <Node Type="Branch">
    <GeneratorGUID>branch-guid</GeneratorGUID>
    <ParentGUID>tree-node</ParentGUID><GUID>branch-node</GUID>
    <Properties><Seed>10</Seed></Properties>
  </Node>
</SpeedTree>
"""


class SpmCalibrationReceiptTests(unittest.TestCase):
    def test_projection_ignores_texture_material_preview_and_vertex_color(self):
        first = bone_semantic_fingerprint(SOURCE, context={"asset_kind": "tree"})
        cosmetic = (
            SOURCE.replace('Name="Raw"', 'Name="M_Canonical"')
            .replace("source_a.tga", r"D:\canonical\T_leaf_color.tga")
            .replace("old-thumbnail", "new-thumbnail")
            .replace(
                "<Name>Vertex Color:Red:Value</Name><Value>0</Value>",
                "<Name>Vertex Color:Red:Value</Name><Value>1</Value>",
            )
        )
        second = bone_semantic_fingerprint(
            cosmetic,
            context={"asset_kind": "tree"},
        )
        self.assertEqual(first, second)

    def test_projection_ignores_real_vertex_color_spline_properties(self):
        source = SOURCE.replace(
            (
                "<Name>Vertex Color:Red:Value</Name><Value>0</Value>"
                "</Property>"
            ),
            (
                "<Name>Vertex Color:Red:Value</Name><Value>0</Value>"
                "</Property>"
                "<SplineProperty>"
                "<Name>Vertex Color:Green:Value</Name><Value>-0.5</Value>"
                "<ProfileSpline><ControlPoint><X>0</X><Y>1</Y>"
                "<TangentX>1</TangentX><TangentY>0</TangentY>"
                "</ControlPoint></ProfileSpline>"
                "</SplineProperty>"
            ),
        )
        changed = (
            source.replace("<Value>-0.5</Value>", "<Value>0</Value>")
            .replace("<Y>1</Y>", "<Y>0</Y>")
            .replace("<TangentX>1</TangentX>", "<TangentX>0</TangentX>")
        )
        self.assertEqual(
            bone_semantic_fingerprint(source),
            bone_semantic_fingerprint(changed),
        )
        self.assertNotEqual(
            legacy_bone_semantic_fingerprint(source),
            legacy_bone_semantic_fingerprint(changed),
        )

    def test_material_slot_identity_is_ignored_but_assignment_state_is_not(self):
        first = bone_semantic_fingerprint(SOURCE)
        other_id = SOURCE.replace(
            "<Name>Materials:Branch:0:Material</Name><Value>12</Value>",
            "<Name>Materials:Branch:0:Material</Name><Value>47</Value>",
        )
        path_binding = SOURCE.replace(
            "<Name>Materials:Branch:0:Material</Name><Value>12</Value>",
            (
                "<Name>Materials:Branch:0:Material</Name>"
                r"<Value>D:\canonical\M_Bark.stmat</Value>"
            ),
        )
        unassigned = SOURCE.replace(
            "<Name>Materials:Branch:0:Material</Name><Value>12</Value>",
            "<Name>Materials:Branch:0:Material</Name><Value>-1</Value>",
        )
        self.assertEqual(first, bone_semantic_fingerprint(other_id))
        self.assertEqual(first, bone_semantic_fingerprint(path_binding))
        self.assertNotEqual(first, bone_semantic_fingerprint(unassigned))

    def test_projection_changes_for_bone_geometry_or_graph_changes(self):
        first = bone_semantic_fingerprint(SOURCE)
        changed_bone = SOURCE.replace(
            "<Name>Physics:Bones</Name><Value>1</Value>",
            "<Name>Physics:Bones</Name><Value>2</Value>",
        )
        changed_geometry = SOURCE.replace("<Seed>10</Seed>", "<Seed>11</Seed>")
        changed_cutout_geometry = SOURCE.replace(
            "<CutoutMeshID>4</CutoutMeshID>",
            "<CutoutMeshID>5</CutoutMeshID>",
        )
        changed_graph = SOURCE.replace(
            "<ParentGUID>tree-node</ParentGUID>",
            "<ParentGUID>other-tree</ParentGUID>",
        )
        self.assertNotEqual(first, bone_semantic_fingerprint(changed_bone))
        self.assertNotEqual(first, bone_semantic_fingerprint(changed_geometry))
        self.assertNotEqual(
            first,
            bone_semantic_fingerprint(changed_cutout_geometry),
        )
        self.assertNotEqual(first, bone_semantic_fingerprint(changed_graph))

    def test_positive_receipt_is_exact_and_corruption_is_only_a_cache_miss(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            spm = root / "SK_tree.spm"
            spm.write_bytes(b"spm")
            cache = root / "central-cache"
            report = {
                "status": "already-ok",
                "total_bones": 42,
                "calibration": {"mode": "relative"},
            }
            path = write_positive_calibration_receipt(
                spm,
                cache,
                bone_semantic_fingerprint_value="semantic-a",
                settings_signature="settings-a",
                bone_contract_version=3,
                report=report,
            )
            self.assertEqual(path, calibration_receipt_path(spm, cache))
            self.assertIsNotNone(
                load_positive_calibration_receipt(
                    spm,
                    cache,
                    bone_semantic_fingerprint_value="semantic-a",
                    settings_signature="settings-a",
                    bone_contract_version=3,
                )
            )
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(
                payload["bone_semantic_projection_version"],
                SPM_BONE_SEMANTIC_PROJECTION_VERSION,
            )
            self.assertIsNone(
                load_positive_calibration_receipt(
                    spm,
                    cache,
                    bone_semantic_fingerprint_value="changed",
                    settings_signature="settings-a",
                    bone_contract_version=3,
                )
            )
            path.write_text("{broken", encoding="utf-8")
            self.assertIsNone(
                load_positive_calibration_receipt(
                    spm,
                    cache,
                    bone_semantic_fingerprint_value="semantic-a",
                    settings_signature="settings-a",
                    bone_contract_version=3,
                )
            )

    def test_v1_positive_receipt_migrates_only_on_exact_legacy_fingerprint(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            spm = root / "SK_tree.spm"
            spm.write_bytes(b"spm")
            cache = root / "central-cache"
            path = calibration_receipt_path(spm, cache)
            path.parent.mkdir(parents=True)
            path.write_text(
                json.dumps(
                    {
                        "kind": "spm_bone_calibration_positive_receipt",
                        "version": 1,
                        "spm_identity": str(spm.resolve()).lower(),
                        "bone_semantic_projection_version": 1,
                        "bone_semantic_fingerprint": "legacy-a",
                        "settings_signature": "settings-a",
                        "bone_contract_version": 3,
                        "status": "already-ok",
                        "summary": {},
                    }
                ),
                encoding="utf-8",
            )
            migrated = load_positive_calibration_receipt(
                spm,
                cache,
                bone_semantic_fingerprint_value="current-a",
                legacy_bone_semantic_fingerprint_values=("legacy-a",),
                settings_signature="settings-a",
                bone_contract_version=3,
            )
            self.assertTrue(
                migrated["legacy_bone_semantic_receipt_migrated"]
            )
            self.assertIsNone(
                load_positive_calibration_receipt(
                    spm,
                    cache,
                    bone_semantic_fingerprint_value="current-a",
                    legacy_bone_semantic_fingerprint_values=("other",),
                    settings_signature="settings-a",
                    bone_contract_version=3,
                )
            )

    def test_failed_result_never_creates_positive_receipt(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            spm = root / "SK_tree.spm"
            spm.write_bytes(b"spm")
            cache = root / "central-cache"
            result = write_positive_calibration_receipt(
                spm,
                cache,
                bone_semantic_fingerprint_value="semantic-a",
                settings_signature="settings-a",
                bone_contract_version=3,
                report={"status": "failed"},
            )
            self.assertIsNone(result)
            self.assertFalse(calibration_receipt_path(spm, cache).exists())

    def test_spm_audit_supports_package_import_from_repo_root(self):
        repo_root = SK_BATCH_DIR.parent
        code = (
            "import sys; "
            f"sys.path.insert(0, {str(repo_root)!r}); "
            "from sk_batch.spm_audit import read_spm; "
            "print(read_spm.__name__)"
        )
        completed = subprocess.run(
            [sys.executable, "-I", "-c", code],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout.strip(), "read_spm")


if __name__ == "__main__":
    unittest.main()
