import tempfile
import unittest
from pathlib import Path
import xml.etree.ElementTree as ET

from cluster_card_pipeline.contract import ContractError, _read_spm_root, _write_spm_root
from cluster_card_pipeline.metadata_repair import repair_texture_size_metadata


def _write_tga(path: Path, width: int, height: int):
    header = bytearray(18)
    header[2] = 2
    header[12:14] = width.to_bytes(2, "little")
    header[14:16] = height.to_bytes(2, "little")
    header[16] = 24
    path.write_bytes(bytes(header) + bytes(width * height * 3))


def _write_spm(path: Path, texture_name: str, declared=(0, 0)):
    root = ET.Element("SpeedTree")
    assets = ET.SubElement(root, "Assets")
    material = ET.SubElement(assets, "Material_v8", ID="6", Name="M_leaf")
    ET.SubElement(material, "Width").text = "4"
    ET.SubElement(material, "Height").text = "4"
    map_node = ET.SubElement(material, "Map", Name="SubsurfaceAmount")
    ET.SubElement(map_node, "TexFilename").text = texture_name
    ET.SubElement(map_node, "TexSizeX").text = str(declared[0])
    ET.SubElement(map_node, "TexSizeY").text = str(declared[1])
    _write_spm_root(path, root)


class TextureMetadataRepairTests(unittest.TestCase):
    def test_dry_run_then_apply_with_hash_matched_backup(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            texture = root / "leaf.tga"
            spm = root / "tree.spm"
            _write_tga(texture, 4, 4)
            _write_spm(spm, texture.name)
            dry = repair_texture_size_metadata(spm, "M_leaf")
            self.assertEqual(dry["status"], "repair_required")
            self.assertFalse(dry["applied"])
            applied = repair_texture_size_metadata(spm, "M_leaf", apply=True)
            self.assertEqual(applied["status"], "complete")
            self.assertEqual(
                applied["backup"]["sha256"], applied["spm_before"]["sha256"]
            )
            material = _read_spm_root(spm).find(".//Material_v8")
            map_node = material.find("Map")
            self.assertEqual(map_node.findtext("TexSizeX"), "4")
            self.assertEqual(map_node.findtext("TexSizeY"), "4")

    def test_refuses_header_that_disagrees_with_material(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            texture = root / "leaf.tga"
            spm = root / "tree.spm"
            _write_tga(texture, 8, 4)
            _write_spm(spm, texture.name)
            with self.assertRaisesRegex(ContractError, "does not match material size"):
                repair_texture_size_metadata(spm, "M_leaf", apply=True)


if __name__ == "__main__":
    unittest.main()
