import base64
import gzip
import hashlib
import math
import struct
import tempfile
import unittest
from pathlib import Path

from cluster_card_pipeline.contract import (
    ContractError,
    _decode_expected_base64,
    _generator_pairs,
    _material_snapshot,
    _read_spm_root,
    _rotate_axis_angle,
    _write_spm_root,
    build_normalization_contract,
    read_uv_template_contract,
    write_handoff_spm_copies,
)
from cluster_card_pipeline.cli import _build_assembly_parts


def encoded(values, fmt):
    return base64.b64encode(struct.pack(fmt, *values)).decode("ascii")


def vertex_payload(vertices):
    values = []
    for x, y, u, v in vertices:
        row = [0.0] * 37
        row[0:3] = [x, y, 0.0]
        row[3:6] = [0.0, 0.0, 1.0]
        row[12:14] = [u, v]
        values.extend(row)
    return encoded(values, "<" + "f" * len(values))


def fixture(
    camera_path,
    tree_path,
    *,
    camera_name="Dropped XY plane camera 2",
    rotation_axis=(0.0, 0.0, 1.0),
    rotation_angle_degrees=0.0,
):
    camera_xml = f'''<SpeedTree><Assets></Assets><SceneCameras><SceneCamera><Name>{camera_name}</Name><GUID>camera</GUID><Properties>
<Property><Name>Settings:Type</Name><Value>1</Value></Property><Property><Name>Settings:Width</Name><Value>20</Value></Property>
<Property><Name>Settings:Height</Name><Value>20</Value></Property><Property><Name>Settings:Depth</Name><Value>5</Value></Property>
<Property><Name>Settings:Near</Name><Value>0.01</Value></Property><Property><Name>Settings:Far</Name><Value>100</Value></Property>
<Property><Name>Export:Resolution</Name><Value>1</Value></Property><Property><Name>Export:Custom resolution</Name><Value>512</Value></Property>
<Property><Name>Transform:Translation:X</Name><Value>0</Value></Property><Property><Name>Transform:Translation:Y</Name><Value>0</Value></Property>
<Property><Name>Transform:Translation:Z</Name><Value>5</Value></Property><Property><Name>Transform:Rotation:Axis X</Name><Value>{rotation_axis[0]}</Value></Property>
<Property><Name>Transform:Rotation:Axis Y</Name><Value>{rotation_axis[1]}</Value></Property><Property><Name>Transform:Rotation:Axis Z</Name><Value>{rotation_axis[2]}</Value></Property>
<Property><Name>Transform:Rotation:Angle</Name><Value>{rotation_angle_degrees}</Value></Property></Properties></SceneCamera></SceneCameras></SpeedTree>'''
    vertices = [(-0.5, 0.0, 0.0, 0.0), (0.5, 0.0, 1.0, 0.0), (0.5, 1.0, 1.0, 1.0), (-0.5, 1.0, 0.0, 1.0)]
    mesh_nodes = []
    for mesh_id in (1, 2, 9):
        mesh_nodes.append(f'''<Mesh ID="{mesh_id}" Name="Cutout {mesh_id}"><Embedded>true</Embedded><Scale>1</Scale>
<EmbeddedData_v7 NumVertices="4" NumIndices="6"><Vertices>{vertex_payload(vertices)}</Vertices>
<Indices>{encoded([0,1,2,0,2,3], '<6I')}</Indices></EmbeddedData_v7>
<Cutout><LOD0 PivotX="0.5" PivotY="0" Angle="0"><Points /></LOD0></Cutout></Mesh>''')
    def write_tga(name, marker):
        atlas = tree_path.parent / name
        tga_header = bytearray(18)
        tga_header[2] = 2
        tga_header[12:14] = (2048).to_bytes(2, "little")
        tga_header[14:16] = (2048).to_bytes(2, "little")
        tga_header[16] = 24
        atlas.write_bytes(bytes(tga_header) + marker)

    write_tga("branch_elm_01.tga", b"color-fixture")
    write_tga("branch_elm_01_Opacity.tga", b"opacity-fixture")
    write_tga("branch_elm_01_Normal.tga", b"normal-fixture")
    tree_xml = f'''<SpeedTree><Assets><Material_v8 ID="8" Name="M_branch_elm_01"><CutoutMeshID>1</CutoutMeshID>
<SupplementalCutoutMeshIDs><CutoutMesh ID="2"/><CutoutMesh ID="9"/></SupplementalCutoutMeshIDs>
<Width>2048</Width><Height>2048</Height><Map Name="Color"><TexFilename>branch_elm_01.tga</TexFilename><TexSizeX>2048</TexSizeX><TexSizeY>2048</TexSizeY></Map>
<Map Name="Opacity"><TexFilename>branch_elm_01_Opacity.tga</TexFilename><TexSizeX>2048</TexSizeX><TexSizeY>2048</TexSizeY></Map>
<Map Name="Normal"><TexFilename>branch_elm_01_Normal.tga</TexFilename><TexSizeX>2048</TexSizeX><TexSizeY>2048</TexSizeY></Map>
</Material_v8>{''.join(mesh_nodes)}</Assets><Generators><Generator Type="Frond"><Name>Frond fixture</Name><GUID>generator-guid</GUID><Properties>
<Property><Name>Material:Frond:0:Material</Name><Value>8</Value></Property>
<Property><Name>Material:Frond:0:Mesh</Name><Value>1</Value></Property>
</Properties></Generator></Generators></SpeedTree>'''
    camera_path.write_bytes(gzip.compress(camera_xml.encode("utf-8")))
    tree_path.write_bytes(gzip.compress(tree_xml.encode("utf-8")))


class ContractTests(unittest.TestCase):
    def test_missing_zero_sextet_repair_is_size_guarded(self):
        raw = b"\x00" * 148
        normal = base64.b64encode(raw).decode("ascii")
        damaged = normal.rstrip("=")[:-1] + normal[-2:]
        self.assertEqual(_decode_expected_base64(damaged, 148, "fixture"), raw)
        with self.assertRaises(ContractError):
            _decode_expected_base64(damaged, 147, "fixture")

    def test_xy_camera_contract_preserves_origin_uv_and_topology(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            camera = root / "branch_elm_01.spm"
            tree = root / "SK_Tree_elm_01.spm"
            fixture(camera, tree)
            contract = build_normalization_contract(camera, tree)
            self.assertEqual([row["source_mesh_id"] for row in contract["planes"]], [1, 2, 9])
            self.assertEqual(contract["camera"]["right"], [1.0, 0.0, 0.0])
            self.assertEqual(contract["active_cutout_mesh_ids"], [1])
            self.assertEqual(contract["tree_generator_bindings"][0]["generator_guid"], "generator-guid")
            self.assertTrue(all(row["attachment"]["normalized_local"] == [0.0, 0.0, 0.0] for row in contract["planes"]))
            self.assertTrue(all(row["validation"]["uv_preserved"] for row in contract["planes"]))
            self.assertEqual(contract["validation"]["max_reprojection_pixel_error"], 0.0)

    def test_yz_dropped_camera_uses_degrees_and_resolves_plane(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            camera = root / "branch_elm_01.spm"
            tree = root / "SK_Tree_elm_01.spm"
            axis = (1.0 / math.sqrt(3.0),) * 3
            fixture(
                camera,
                tree,
                camera_name="Dropped YZ plane camera 2",
                rotation_axis=axis,
                rotation_angle_degrees=120.0,
            )

            contract = read_uv_template_contract(
                camera,
                tree,
                material_name="M_branch_elm_01",
                material_id=8,
                camera_name="Dropped YZ plane camera 2",
            )
            resolved = contract["camera"]
            self.assertEqual(resolved["declared_plane"], "YZ")
            self.assertEqual(resolved["resolved_plane"], "YZ")
            self.assertEqual(resolved["rotation_angle_degrees"], 120.0)
            self.assertAlmostEqual(
                resolved["rotation_angle_radians"], 2.0 * math.pi / 3.0
            )
            for actual, expected in zip(resolved["right"], (0.0, 1.0, 0.0)):
                self.assertAlmostEqual(actual, expected, places=6)
            for actual, expected in zip(resolved["up"], (0.0, 0.0, 1.0)):
                self.assertAlmostEqual(actual, expected, places=6)
            for actual, expected in zip(
                resolved["view_direction"], (-1.0, 0.0, 0.0)
            ):
                self.assertAlmostEqual(actual, expected, places=6)

    def test_xz_dropped_camera_resolves_rotated_canonical_basis(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            camera = root / "branch_elm_01.spm"
            tree = root / "SK_Tree_elm_01.spm"
            fixture(
                camera,
                tree,
                camera_name="Dropped XZ plane camera",
                rotation_axis=(1.0, 0.0, 0.0),
                rotation_angle_degrees=90.0,
            )

            contract = read_uv_template_contract(
                camera,
                tree,
                material_name="M_branch_elm_01",
                material_id=8,
                camera_name="Dropped XZ plane camera",
            )
            resolved = contract["camera"]
            self.assertEqual(resolved["resolved_plane"], "XZ")
            self.assertEqual(resolved["right"], [1.0, 0.0, 0.0])
            for actual, expected in zip(resolved["up"], (0.0, 0.0, 1.0)):
                self.assertAlmostEqual(actual, expected, places=6)
            for actual, expected in zip(resolved["plane_normal"], (0.0, -1.0, 0.0)):
                self.assertAlmostEqual(actual, expected, places=6)

    def test_generic_ortho_camera_resolves_from_transform(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            camera = root / "branch_elm_01.spm"
            tree = root / "SK_Tree_elm_01.spm"
            fixture(camera, tree, camera_name="Ortho camera")

            contract = read_uv_template_contract(
                camera,
                tree,
                material_name="M_branch_elm_01",
                material_id=8,
                camera_name="Ortho camera",
            )
            resolved = contract["camera"]
            self.assertEqual(resolved["name_kind"], "generic_ortho")
            self.assertIsNone(resolved["declared_plane"])
            self.assertEqual(resolved["resolved_plane"], "XY")
            self.assertIsNone(resolved["declared_plane_matches_resolved"])

    def test_explicit_dropped_plane_must_match_resolved_transform(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            camera = root / "branch_elm_01.spm"
            tree = root / "SK_Tree_elm_01.spm"
            fixture(
                camera,
                tree,
                camera_name="Dropped XY plane camera 2",
                rotation_axis=(1.0, 0.0, 0.0),
                rotation_angle_degrees=90.0,
            )
            with self.assertRaisesRegex(
                ContractError, "declares XY plane.*resolves to XZ plane"
            ):
                read_uv_template_contract(
                    camera,
                    tree,
                    material_name="M_branch_elm_01",
                    material_id=8,
                )

    def test_non_axis_aligned_ortho_camera_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            camera = root / "branch_elm_01.spm"
            tree = root / "SK_Tree_elm_01.spm"
            fixture(
                camera,
                tree,
                camera_name="Ortho camera",
                rotation_axis=(1.0, 0.0, 0.0),
                rotation_angle_degrees=45.0,
            )
            with self.assertRaisesRegex(ContractError, "axis-aligned"):
                read_uv_template_contract(
                    camera,
                    tree,
                    material_name="M_branch_elm_01",
                    material_id=8,
                    camera_name="Ortho camera",
                )

    def test_uv_template_contract_ignores_current_generator_material(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            camera = root / "branch_elm_01.spm"
            tree = root / "SK_Tree_elm_01.spm"
            fixture(camera, tree)
            tree_root = _read_spm_root(tree)
            material_value = next(
                item.find("Value")
                for item in tree_root.findall(".//Property")
                if item.findtext("Name") == "Material:Frond:0:Material"
            )
            material_value.text = "99"
            _write_spm_root(tree, tree_root)

            contract = read_uv_template_contract(
                camera, tree, material_name="M_branch_elm_01", material_id=8
            )

            self.assertEqual(contract["material"]["id"], 8)
            self.assertEqual(contract["material"]["ordered_cutout_mesh_ids"], [1, 2, 9])
            self.assertEqual(contract["camera"]["resolved_export_resolution_pixels"], [2048, 2048])
            self.assertEqual(len(contract["planes"]), 3)
            self.assertTrue(contract["validation"]["strict_vertex_uv_topology"])
            self.assertNotIn("tree_generator_bindings", contract)

    def test_uv_template_contract_fingerprints_every_material_map(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            camera = root / "branch_elm_01.spm"
            tree = root / "SK_Tree_elm_01.spm"
            fixture(camera, tree)

            contract = read_uv_template_contract(
                camera, tree, material_name="M_branch_elm_01", material_id=8
            )

            maps = contract["material"]["maps"]
            self.assertEqual(set(maps), {"Color", "Opacity", "Normal"})
            for atlas_map in maps.values():
                atlas_path = Path(atlas_map["path"])
                payload = atlas_path.read_bytes()
                self.assertEqual(atlas_map["file_size"], len(payload))
                self.assertEqual(
                    atlas_map["sha256"], hashlib.sha256(payload).hexdigest()
                )
                self.assertEqual(atlas_map["size"], [2048, 2048])

    def test_uv_template_contract_rejects_wrong_camera_texture_stem(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            camera = root / "branch_elm_01.spm"
            tree = root / "SK_Tree_elm_01.spm"
            fixture(camera, tree)
            wrong_atlas = root / "other_capture.tga"
            wrong_atlas.write_bytes((root / "branch_elm_01.tga").read_bytes())
            tree_root = _read_spm_root(tree)
            tree_root.find(".//Map[@Name='Color']/TexFilename").text = wrong_atlas.name
            _write_spm_root(tree, tree_root)
            with self.assertRaisesRegex(ContractError, "camera SPM stem"):
                read_uv_template_contract(
                    camera, tree, material_name="M_branch_elm_01", material_id=8
                )

    def test_uv_template_contract_rejects_dimension_drift(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            camera = root / "branch_elm_01.spm"
            tree = root / "SK_Tree_elm_01.spm"
            fixture(camera, tree)
            tree_root = _read_spm_root(tree)
            tree_root.find(".//Map[@Name='Color']/TexSizeX").text = "1024"
            _write_spm_root(tree, tree_root)
            with self.assertRaisesRegex(ContractError, "map size"):
                read_uv_template_contract(
                    camera, tree, material_name="M_branch_elm_01", material_id=8
                )

    def test_uv_template_contract_requires_explicit_metadata_repair(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            camera = root / "branch_elm_01.spm"
            tree = root / "SK_Tree_elm_01.spm"
            fixture(camera, tree)
            tree_root = _read_spm_root(tree)
            normal = tree_root.find(".//Map[@Name='Normal']")
            normal.find("TexSizeX").text = "0"
            normal.find("TexSizeY").text = "0"
            _write_spm_root(tree, tree_root)
            with self.assertRaisesRegex(
                ContractError,
                r"map size \[0, 0\].*file header is \[2048, 2048\].*"
                r"Metadata repair is required; implicit repair is forbidden",
            ):
                read_uv_template_contract(
                    camera, tree, material_name="M_branch_elm_01", material_id=8
                )

    def test_uv_template_contract_rejects_wrong_material_id(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            camera = root / "branch_elm_01.spm"
            tree = root / "SK_Tree_elm_01.spm"
            fixture(camera, tree)
            with self.assertRaises(ContractError):
                read_uv_template_contract(
                    camera, tree, material_name="M_branch_elm_01", material_id=9
                )

    def test_uv_template_contract_rejects_invalid_embedded_blob(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            camera = root / "branch_elm_01.spm"
            tree = root / "SK_Tree_elm_01.spm"
            fixture(camera, tree)
            tree_root = _read_spm_root(tree)
            tree_root.find(".//Mesh[@ID='2']/EmbeddedData_v7/Vertices").text = "not-base64!"
            _write_spm_root(tree, tree_root)
            with self.assertRaisesRegex(ContractError, "required .* bytes"):
                read_uv_template_contract(
                    camera, tree, material_name="M_branch_elm_01", material_id=8
                )

    def test_material_mesh_drift_blocks(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            camera = root / "branch_elm_01.spm"
            tree = root / "SK_Tree_elm_01.spm"
            fixture(camera, tree)
            with self.assertRaisesRegex(ContractError, "Material cutout drift"):
                build_normalization_contract(camera, tree, mesh_ids=(1, 9, 2))

    def test_axis_angle_is_applied_to_camera_basis(self):
        result = _rotate_axis_angle((1.0, 0.0, 0.0), (0.0, 0.0, 1.0), 3.141592653589793 / 2)
        self.assertAlmostEqual(result[0], 0.0, places=7)
        self.assertAlmostEqual(result[1], 1.0, places=7)

    def test_handoff_preserves_tree_ids_guid_and_binding(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            camera = root / "branch_elm_01.spm"
            tree = root / "SK_Tree_elm_01.spm"
            fixture(camera, tree)
            contract = build_normalization_contract(camera, tree)
            output = root / "output"
            mesh_dir = output / "meshes"
            mesh_dir.mkdir(parents=True)
            for plane in contract["planes"]:
                (mesh_dir / f"{plane['name']}.fbx").write_bytes(b"fixture fbx")
            handoff = write_handoff_spm_copies(contract, output)
            candidate = _read_spm_root(handoff["tree_handoff_spm"])
            self.assertEqual(_material_snapshot(candidate, "M_branch_elm_01")["id"], 8)
            self.assertEqual(_material_snapshot(candidate, "M_branch_elm_01")["cutout_mesh_ids"], [1, 2, 9])
            self.assertEqual(_generator_pairs(candidate), contract["tree_generator_bindings"])
            self.assertEqual(handoff["tree_mesh_ids_preserved"], [1, 2, 9])

    def test_assembly_placement_is_derived_from_generator_contract(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            camera = root / "branch_elm_01.spm"
            tree = root / "SK_Tree_elm_01.spm"
            fixture(camera, tree)
            contract = build_normalization_contract(camera, tree)
            report = {
                "planes": [
                    {
                        "name": plane["name"],
                        "fbx": {"path": str(root / f"{plane['name']}.fbx")},
                        "obj": {"path": str(root / f"{plane['name']}.obj")},
                    }
                    for plane in contract["planes"]
                ]
            }
            for row in report["planes"]:
                Path(row["fbx"]["path"]).write_bytes(b"fbx")
                Path(row["obj"]["path"]).write_bytes(b"obj")
            assembly = _build_assembly_parts(contract, report)
            self.assertEqual(assembly["active_cutout_mesh_ids"], [1])
            self.assertEqual(assembly["placement"]["missing_mesh_ids"], [2, 9])
            self.assertEqual(assembly["tree_generator_bindings"][0]["generator_guid"], "generator-guid")


if __name__ == "__main__":
    unittest.main()
